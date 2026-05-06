"""Data coordinator for ČEZ Distribuce Readings."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    CezDistribuceAuthError,
    CezDistribuceClient,
    CezDistribuceError,
    CezDistribuceNetworkError,
    CezDistribuceUnexpectedResponseError,
)
from .archive import build_archive, save_archive
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# When a refresh fails, keep the last good data and retry sooner than the normal
# scan interval. With the default 6 hour scan interval this gives the ČEZ portal
# a chance to recover without leaving the dashboard stale for half a day.
_FAILURE_RETRY_INTERVALS = (
    timedelta(minutes=30),
    timedelta(hours=1),
    timedelta(hours=2),
    timedelta(hours=6),
)


def _utcnow() -> datetime:
    """Return timezone-aware UTC now."""
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    """Return ISO formatted datetime or None."""
    return value.isoformat() if value else None


def _short_error(error: Exception) -> str:
    """Return compact error text suitable for entity attributes."""
    text = str(error).strip()
    return text or error.__class__.__name__


def extract_supply_points(raw: Any) -> list[dict[str, Any]]:
    """Extract supply point records from ČEZ portal response."""
    points: list[dict[str, Any]] = []

    if isinstance(raw, dict):
        blocks = raw.get("vstelleBlocks", {}).get("blocks", [])

        for block in blocks:
            for item in block.get("vstelles", []) or []:
                if isinstance(item, dict) and item.get("uid"):
                    points.append(item)

        if raw.get("uid"):
            points.append(raw)

    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and item.get("uid"):
                points.append(item)

    seen: set[str] = set()
    unique_points: list[dict[str, Any]] = []

    for point in points:
        uid = point.get("uid")
        if uid and uid not in seen:
            unique_points.append(point)
            seen.add(uid)

    return unique_points


def extract_ean(*sources: dict[str, Any] | None) -> str | None:
    """Extract EAN from point/detail dictionaries."""
    preferred_keys = (
        "ean",
        "ean18",
        "eanKod",
        "eanCode",
        "eanSpotreby",
        "eanOdbernehoMista",
    )

    for source in sources:
        if not isinstance(source, dict):
            continue

        for key in preferred_keys:
            value = source.get(key)
            if value:
                return str(value)

        for key, value in source.items():
            if "ean" in str(key).lower() and value:
                return str(value)

    return None


class CezDistribuceCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator that fetches all ČEZ data in one executor job."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: CezDistribuceClient,
        scan_interval: timedelta,
        detailed_history: bool,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=scan_interval,
        )
        self.client = client
        self.detailed_history = detailed_history
        self._base_update_interval = scan_interval
        self._consecutive_failures = 0
        self._last_error_type: str | None = None
        self._last_error_detail: str | None = None
        self._last_attempt_at: datetime | None = None
        self._last_success_at: datetime | None = None
        self._last_failure_at: datetime | None = None
        self._last_attempt_success: bool | None = None
        self._using_stale_data = False

    def _set_failure_retry_interval(self) -> None:
        """Set a short retry interval after a failed refresh."""
        index = min(self._consecutive_failures - 1, len(_FAILURE_RETRY_INTERVALS) - 1)
        retry_interval = _FAILURE_RETRY_INTERVALS[max(0, index)]

        # Never retry slower than the configured base interval. If the user has a
        # very short scan interval, keep it. If the default is 6 hours, retry in
        # 30 minutes after the first failure.
        new_interval = min(retry_interval, self._base_update_interval)

        if self.update_interval != new_interval:
            _LOGGER.warning(
                "Adjusting ČEZ update interval after refresh failure. "
                "failures=%s new_interval=%s base_interval=%s",
                self._consecutive_failures,
                new_interval,
                self._base_update_interval,
            )
            self.update_interval = new_interval

    def _reset_failure_state(self) -> None:
        """Reset refresh error diagnostics after a fully successful refresh."""
        if self._consecutive_failures != 0:
            _LOGGER.debug("Resetting ČEZ refresh failure state after successful refresh")

        self._consecutive_failures = 0
        self._last_error_type = None
        self._last_error_detail = None
        self._last_attempt_success = True
        self._using_stale_data = False
        self._last_success_at = self._last_attempt_at or _utcnow()

        if self.update_interval != self._base_update_interval:
            self.update_interval = self._base_update_interval

    def _set_error(self, error_type: str, error: Exception | str) -> None:
        """Store last refresh error diagnostics for entity attributes."""
        self._last_error_type = error_type
        self._last_error_detail = str(error).strip() or str(type(error).__name__)
        self._last_failure_at = self._last_attempt_at or _utcnow()
        self._last_attempt_success = False

    def _has_cached_data(self) -> bool:
        """Return true if the coordinator already has usable data."""
        return isinstance(self.data, dict) and bool(self.data.get("points"))

    def _stale_cached_data(self) -> dict[str, Any] | None:
        """Return the last good data, if it exists."""
        if self._has_cached_data():
            return self.data
        return None

    def _record_full_failure(self, error_type: str, error: Exception) -> None:
        """Record a full refresh failure and update retry interval."""
        self._consecutive_failures += 1
        self._set_error(error_type, error)
        self._using_stale_data = True
        self._set_failure_retry_interval()

    def _record_partial_failure(self, partial_errors: list[str]) -> None:
        """Record that a refresh succeeded only by reusing cached sub-data."""
        self._consecutive_failures += 1
        self._set_error("partial", "; ".join(partial_errors[:5]))
        self._using_stale_data = True
        self._set_failure_retry_interval()

    def refresh_status_attributes(self) -> dict[str, Any]:
        """Return lightweight refresh diagnostics for entities."""
        now = _utcnow()
        data_age_min: int | None = None

        if self._last_success_at is not None:
            data_age_min = max(0, int((now - self._last_success_at).total_seconds() // 60))

        return {
            "refresh_last_attempt_success": self._last_attempt_success,
            "refresh_using_stale_data": self._using_stale_data,
            "refresh_error_type": self._last_error_type,
            "refresh_error_detail": self._last_error_detail,
            "refresh_consecutive_failures": self._consecutive_failures,
            "refresh_last_attempt_at": _iso(self._last_attempt_at),
            "refresh_last_success_at": _iso(self._last_success_at),
            "refresh_last_failure_at": _iso(self._last_failure_at),
            "refresh_data_age_min": data_age_min,
            "refresh_effective_interval_min": (
                int(self.update_interval.total_seconds() // 60) if self.update_interval else None
            ),
            "refresh_base_interval_min": int(self._base_update_interval.total_seconds() // 60),
        }

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from ČEZ portal.

        Important behavior: once we have one successful dataset, later portal or
        authentication failures do not make entities unavailable. Instead, we
        return the cached data and expose the failure through refresh_* entity
        attributes. This keeps dashboards useful while clearly showing that the
        last refresh attempt failed.
        """
        self._last_attempt_at = _utcnow()

        try:
            result = await self.hass.async_add_executor_job(self._fetch_sync)
        except CezDistribuceAuthError as err:
            return self._handle_failed_refresh("auth", err, f"Authentication problem: {err}")
        except CezDistribuceNetworkError as err:
            return self._handle_failed_refresh("network", err, f"Network problem: {err}")
        except CezDistribuceUnexpectedResponseError as err:
            return self._handle_failed_refresh(
                "schema",
                err,
                f"Unexpected portal response: {err}",
            )
        except CezDistribuceError as err:
            return self._handle_failed_refresh("portal", err, f"Portal error: {err}")
        except Exception as err:
            return self._handle_failed_refresh(
                "unknown",
                err,
                f"Unexpected update error: {err}",
            )

        partial_errors = result.pop("_partial_refresh_errors", [])

        if partial_errors:
            self._record_partial_failure(partial_errors)
            _LOGGER.warning(
                "ČEZ refresh completed with cached fallback data. failures=%s errors=%s",
                self._consecutive_failures,
                partial_errors,
            )
        else:
            self._reset_failure_state()

        return result

    def _handle_failed_refresh(
        self,
        error_type: str,
        error: Exception,
        update_failed_message: str,
    ) -> dict[str, Any]:
        """Return stale data after a failed refresh, or raise on first startup."""
        self._record_full_failure(error_type, error)

        cached = self._stale_cached_data()
        if cached is not None:
            _LOGGER.warning(
                "ČEZ refresh failed, keeping last good data. failures=%s type=%s error=%s",
                self._consecutive_failures,
                error_type,
                _short_error(error),
            )
            return cached

        raise UpdateFailed(update_failed_message) from error

    def _fetch_sync(self) -> dict[str, Any]:
        """Synchronous fetch run in executor."""
        old_data = self.data if isinstance(self.data, dict) else {}
        old_details_by_uid = old_data.get("details_by_uid", {}) if isinstance(old_data, dict) else {}
        old_readings_by_uid = old_data.get("readings_by_uid", {}) if isinstance(old_data, dict) else {}
        old_signals_by_uid = old_data.get("signals_by_uid", {}) if isinstance(old_data, dict) else {}
        old_archives_by_uid = old_data.get("archives_by_uid", {}) if isinstance(old_data, dict) else {}

        partial_errors: list[str] = []

        raw_points = self.client.get_supply_points()
        points = extract_supply_points(raw_points)

        if not points and old_data.get("points"):
            raise CezDistribuceUnexpectedResponseError(
                "ČEZ portal returned no supply points while cached supply points exist"
            )

        readings_by_uid: dict[str, list[dict[str, Any]]] = {}
        details_by_uid: dict[str, dict[str, Any]] = {}
        signals_by_uid: dict[str, Any] = {}
        archives_by_uid: dict[str, dict[str, Any]] = {}

        archive_dir = Path(self.hass.config.path("cez_distribuce_readings"))

        for point in points:
            uid = point.get("uid")
            if not uid:
                continue

            detail: dict[str, Any] = {}

            try:
                detail_data = self.client.get_supply_point_detail(uid)
                if not isinstance(detail_data, dict):
                    raise CezDistribuceUnexpectedResponseError(
                        f"Unexpected supply point detail payload type: {type(detail_data).__name__}"
                    )
                detail = detail_data
                details_by_uid[uid] = detail
            except Exception as err:
                old_detail = old_details_by_uid.get(uid, {})
                if isinstance(old_detail, dict) and old_detail:
                    detail = old_detail
                    details_by_uid[uid] = detail
                    partial_errors.append(f"detail uid={uid}: {_short_error(err)}")
                    _LOGGER.warning(
                        "Unable to fetch supply point detail for uid=%s, using cached detail: %s",
                        uid,
                        err,
                    )
                else:
                    partial_errors.append(f"detail uid={uid}: {_short_error(err)}")
                    _LOGGER.warning("Unable to fetch supply point detail for uid=%s: %s", uid, err)

            ean = extract_ean(detail, point)

            try:
                readings = self.client.get_meter_reading_history(
                    uid,
                    detailed=self.detailed_history,
                )

                if not readings and old_readings_by_uid.get(uid):
                    raise CezDistribuceUnexpectedResponseError(
                        "Meter reading history is empty while cached readings exist"
                    )

                readings_by_uid[uid] = readings

                archive = build_archive(readings)
                archive_key = ean or uid

                try:
                    archive_paths = save_archive(archive, archive_dir, archive_key)
                    archive.update(archive_paths)
                except Exception as save_err:
                    _LOGGER.warning(
                        "Unable to save ČEZ archive for uid=%s ean=%s, keeping in-memory archive: %s",
                        uid,
                        ean,
                        save_err,
                    )

                archives_by_uid[uid] = archive

                _LOGGER.debug(
                    "ČEZ archive saved for uid=%s ean=%s readings=%s periods=%s",
                    uid,
                    ean,
                    archive.get("readings_count"),
                    archive.get("periods_count"),
                )

            except Exception as err:
                old_readings = old_readings_by_uid.get(uid)
                old_archive = old_archives_by_uid.get(uid)

                if isinstance(old_readings, list) and old_readings:
                    readings_by_uid[uid] = old_readings

                    if isinstance(old_archive, dict) and old_archive:
                        archives_by_uid[uid] = old_archive
                    else:
                        archives_by_uid[uid] = build_archive(old_readings)

                    partial_errors.append(f"readings uid={uid}: {_short_error(err)}")
                    _LOGGER.warning(
                        "Unable to fetch/archive readings for uid=%s, using cached readings: %s",
                        uid,
                        err,
                    )
                else:
                    readings_by_uid[uid] = []
                    archives_by_uid[uid] = build_archive([])
                    partial_errors.append(f"readings uid={uid}: {_short_error(err)}")
                    _LOGGER.warning("Unable to fetch/archive readings for uid=%s: %s", uid, err)

            if ean:
                try:
                    signal_data = self.client.get_signals(ean)

                    if signal_data in (None, [], {}) and old_signals_by_uid.get(uid) is not None:
                        raise CezDistribuceUnexpectedResponseError(
                            "HDO signals response is empty while cached signals exist"
                        )

                    signals_by_uid[uid] = signal_data
                except Exception as err:
                    if uid in old_signals_by_uid and old_signals_by_uid.get(uid) is not None:
                        signals_by_uid[uid] = old_signals_by_uid.get(uid)
                        partial_errors.append(f"signals uid={uid}: {_short_error(err)}")
                        _LOGGER.warning(
                            "Unable to fetch HDO signals for uid=%s ean=%s, using cached signals: %s",
                            uid,
                            ean,
                            err,
                        )
                    else:
                        signals_by_uid[uid] = None
                        partial_errors.append(f"signals uid={uid}: {_short_error(err)}")
                        _LOGGER.warning(
                            "Unable to fetch HDO signals for uid=%s ean=%s: %s",
                            uid,
                            ean,
                            err,
                        )
            else:
                _LOGGER.debug("No EAN found for uid=%s, skipping HDO signals", uid)
                signals_by_uid[uid] = old_signals_by_uid.get(uid) if uid in old_signals_by_uid else None

        return {
            "points": points,
            "details_by_uid": details_by_uid,
            "readings_by_uid": readings_by_uid,
            "signals_by_uid": signals_by_uid,
            "archives_by_uid": archives_by_uid,
            "_partial_refresh_errors": partial_errors,
        }
