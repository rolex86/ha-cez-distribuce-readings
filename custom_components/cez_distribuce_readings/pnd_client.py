"""PND client bridge that executes the proven /config/pnd_probe_ha.py flow.

This is intentionally a pragmatic fallback for HA: the standalone probe succeeds
inside the Home Assistant Core container, while the in-process requests flow can
hit HTTP 500 on the PND OAuth callback. Running the exact probe in a fresh Python
process keeps the PND flow isolated and returns the same JSON payload to the
existing coordinator/parser.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from .api import CezDistribuceNetworkError, CezDistribuceUnexpectedResponseError

_LOGGER = logging.getLogger(__name__)

PND_DEBUG_DIR = Path("/config/cez_distribuce_readings_debug")
DEFAULT_PROBE_PATH = Path("/config/pnd_probe_ha.py")
SUBPROCESS_TIMEOUT = 180


class CezPndClient:
    """Run the proven standalone PND probe as an isolated one-shot fetch."""

    def __init__(
        self,
        username: str,
        password: str,
        base_url: str | None = None,
        client_id: str | None = None,
    ) -> None:
        self.username = username
        self.password = password
        # base_url/client_id are accepted for backward compatibility with older
        # coordinator code, but intentionally not used. The probe owns its exact
        # working CAS/DIP/PND URLs.
        self.last_warmup_status_code: int | None = None
        self.last_warmup_url: str | None = None
        self.last_data_status_code: int | None = None
        self.last_data_url: str | None = None

    def _latest_json(self, out_dir: Path, pattern: str) -> dict[str, Any] | None:
        files = sorted(out_dir.glob(pattern), key=lambda path: path.stat().st_mtime)
        if not files:
            return None

        try:
            return json.loads(files[-1].read_text(encoding="utf-8"))
        except Exception as err:
            _LOGGER.warning("Unable to read PND probe metadata %s: %s", files[-1], err)
            return None

    def _set_status_from_meta(self, out_dir: Path) -> None:
        warmup_meta = (
            self._latest_json(out_dir, "*_05_pnd_warmup.json")
            or self._latest_json(out_dir, "*_05_pnd_warmup_error.json")
        )
        data_meta = (
            self._latest_json(out_dir, "*_06_pnd_data.json")
            or self._latest_json(out_dir, "*_06_pnd_data_error.json")
        )

        if isinstance(warmup_meta, dict):
            self.last_warmup_status_code = warmup_meta.get("status_code")
            self.last_warmup_url = warmup_meta.get("final_url")

        if isinstance(data_meta, dict):
            self.last_data_status_code = data_meta.get("status_code")
            self.last_data_url = data_meta.get("final_url")

    def _read_payload(self, out_dir: Path) -> Any:
        response_files = sorted(
            out_dir.glob("*_06_pnd_data.response.json"),
            key=lambda path: path.stat().st_mtime,
        )
        if not response_files:
            raise CezDistribuceUnexpectedResponseError(
                f"PND probe finished without JSON response file in {out_dir}"
            )

        return json.loads(response_files[-1].read_text(encoding="utf-8"))

    def get_chart_data(
        self,
        id_device_set: str | int,
        interval_from: str,
        interval_to: str,
        id_assembly: int = -1001,
    ) -> Any:
        """Return PND chart data by running the verified standalone probe."""
        self.last_warmup_status_code = None
        self.last_warmup_url = None
        self.last_data_status_code = None
        self.last_data_url = None

        if not DEFAULT_PROBE_PATH.exists():
            raise CezDistribuceNetworkError(
                "PND probe bridge requires /config/pnd_probe_ha.py. "
                "Copy the working probe file there first."
            )

        PND_DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        out_dir = PND_DEBUG_DIR / f"probe_bridge_{stamp}"
        out_dir.mkdir(parents=True, exist_ok=True)

        env = os.environ.copy()
        env.update(
            {
                "CEZ_USER": self.username,
                "CEZ_PASS": self.password,
                "PND_DEVICE_SET": str(id_device_set),
                "PND_ASSEMBLY": str(id_assembly),
                "PND_FROM": interval_from,
                "PND_TO": interval_to,
                "PND_PROBE_OUT": str(out_dir),
            }
        )

        cmd = [sys.executable, str(DEFAULT_PROBE_PATH)]
        _LOGGER.warning("PND probe bridge start: out_dir=%s", out_dir)

        try:
            completed = subprocess.run(
                cmd,
                cwd="/config",
                env=env,
                text=True,
                capture_output=True,
                timeout=SUBPROCESS_TIMEOUT,
                check=False,
            )
        except subprocess.TimeoutExpired as err:
            raise CezDistribuceNetworkError(
                f"PND probe bridge timed out after {SUBPROCESS_TIMEOUT}s"
            ) from err
        except OSError as err:
            raise CezDistribuceNetworkError(f"Unable to start PND probe bridge: {err}") from err

        stdout_path = out_dir / "probe_stdout.log"
        stderr_path = out_dir / "probe_stderr.log"
        stdout_path.write_text(completed.stdout or "", encoding="utf-8")
        stderr_path.write_text(completed.stderr or "", encoding="utf-8")

        self._set_status_from_meta(out_dir)

        if completed.returncode != 0:
            tail = (completed.stdout or completed.stderr or "")[-2000:]
            _LOGGER.warning(
                "PND probe bridge failed: returncode=%s out_dir=%s tail=%r",
                completed.returncode,
                out_dir,
                tail,
            )
            raise CezDistribuceNetworkError(
                f"PND probe bridge failed with return code {completed.returncode}; "
                f"debug={out_dir}"
            )

        payload = self._read_payload(out_dir)
        _LOGGER.warning(
            "PND probe bridge success: warmup_status=%s data_status=%s out_dir=%s",
            self.last_warmup_status_code,
            self.last_data_status_code,
            out_dir,
        )
        return payload
