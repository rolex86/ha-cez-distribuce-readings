"""Data coordinator for ČEZ Distribuce Readings."""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import CezDistribuceClient, CezDistribuceError
from .archive import build_archive, save_archive
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


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

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from ČEZ portal."""
        try:
            return await self.hass.async_add_executor_job(self._fetch_sync)
        except CezDistribuceError as err:
            raise UpdateFailed(str(err)) from err
        except Exception as err:
            raise UpdateFailed(f"Unexpected ČEZ update error: {err}") from err

    def _fetch_sync(self) -> dict[str, Any]:
        """Synchronous fetch run in executor."""
        raw_points = self.client.get_supply_points()
        points = extract_supply_points(raw_points)

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
                if isinstance(detail_data, dict):
                    detail = detail_data
                    details_by_uid[uid] = detail
            except Exception as err:
                _LOGGER.warning("Unable to fetch supply point detail for uid=%s: %s", uid, err)

            ean = extract_ean(detail, point)

            try:
                readings = self.client.get_meter_reading_history(
                    uid,
                    detailed=self.detailed_history,
                )
                readings_by_uid[uid] = readings

                archive = build_archive(readings)
                archive_key = ean or uid
                archive_paths = save_archive(archive, archive_dir, archive_key)
                archive.update(archive_paths)
                archives_by_uid[uid] = archive

                _LOGGER.debug(
                    "ČEZ archive saved for uid=%s ean=%s readings=%s periods=%s",
                    uid,
                    ean,
                    archive.get("readings_count"),
                    archive.get("periods_count"),
                )

            except Exception as err:
                _LOGGER.warning("Unable to fetch/archive readings for uid=%s: %s", uid, err)
                readings_by_uid[uid] = []
                archives_by_uid[uid] = build_archive([])

            if ean:
                try:
                    signals_by_uid[uid] = self.client.get_signals(ean)
                except Exception as err:
                    _LOGGER.warning(
                        "Unable to fetch HDO signals for uid=%s ean=%s: %s",
                        uid,
                        ean,
                        err,
                    )
                    signals_by_uid[uid] = None
            else:
                _LOGGER.debug("No EAN found for uid=%s, skipping HDO signals", uid)
                signals_by_uid[uid] = None

        return {
            "points": points,
            "details_by_uid": details_by_uid,
            "readings_by_uid": readings_by_uid,
            "signals_by_uid": signals_by_uid,
            "archives_by_uid": archives_by_uid,
        }
