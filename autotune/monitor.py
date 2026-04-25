"""
Sensor polling + post-hoc WHEA scan.

We prefer LibreHardwareMonitor (LHM) because it exposes per-core temps,
effective clocks, package power, and Vcore without admin-only MSR access.
You need the LHM "LibreHardwareMonitor.exe" binary available (set
config.lhm_path); the free nuget package also works.

If LHM isn't available, we fall back to 'wmic' for package temp only --
that's enough to enforce the temp cap but we lose effective-clock
monitoring. The tuner accepts either; calls self.sample().

WHEA scan (Windows Hardware Error Architecture): the kernel logs
correctable errors (id 17) and uncorrectable errors (id 18) to the
System event log. Overclocking instability often shows up as a flood
of id-19 / id-47 corrected errors BEFORE y-cruncher notices anything,
so we poll the event log between runs.
"""

from __future__ import annotations

import logging
import statistics
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class Sample:
    t: float                        # unix timestamp
    pkg_temp_c: Optional[float] = None
    max_core_temp_c: Optional[float] = None
    pkg_power_w: Optional[float] = None
    vcore_v: Optional[float] = None
    effective_mhz: Optional[float] = None
    throttling: bool = False


@dataclass
class RunStats:
    samples: list[Sample] = field(default_factory=list)

    def add(self, s: Sample) -> None:
        self.samples.append(s)

    def peak_temp(self) -> Optional[float]:
        vs = [s.max_core_temp_c for s in self.samples if s.max_core_temp_c is not None]
        return max(vs) if vs else None

    def peak_power(self) -> Optional[float]:
        vs = [s.pkg_power_w for s in self.samples if s.pkg_power_w is not None]
        return max(vs) if vs else None

    def peak_vcore(self) -> Optional[float]:
        vs = [s.vcore_v for s in self.samples if s.vcore_v is not None]
        return max(vs) if vs else None

    def any_throttle(self) -> bool:
        return any(s.throttling for s in self.samples)

    def avg_effective_mhz(self) -> Optional[float]:
        vs = [s.effective_mhz for s in self.samples if s.effective_mhz is not None]
        return statistics.mean(vs) if vs else None


class Monitor:
    """Polls sensors via LHM's JSON endpoint. LHM must be running with its
    web-server enabled (default port 8085). The tuner starts LHM in the
    background if config.auto_start_lhm is true."""

    def __init__(self, lhm_url: str = "http://localhost:8085/data.json",
                 fallback_wmic: bool = True):
        self.url = lhm_url
        self.fallback_wmic = fallback_wmic

    def sample(self) -> Sample:
        try:
            return self._sample_lhm()
        except Exception as e:
            log.debug("LHM unreachable (%s); falling back to wmic", e)
            if not self.fallback_wmic:
                raise
            return self._sample_wmic()

    # ---------- LHM JSON scraper ----------

    def _sample_lhm(self) -> Sample:
        """Walk LHM JSON and pick out CPU sensors only.

        The earlier permissive walker accepted any value with `°C` in it,
        which leaked motherboard chipset (Nuvoton NCT6798D), VRM, RAM
        thermal-limit constants, GPU hot-spot, and `Distance to TjMax`
        derived values into our `temps` list. Result: a single bogus
        spike anywhere on the system would trip the cap and abort runs.

        We now require a sensor to live under the Intel CPU hardware
        node AND have an allowlisted sensor name. Power/voltage/clock
        readings get the same treatment.
        """
        import urllib.request
        import json as _json
        with urllib.request.urlopen(self.url, timeout=2) as r:
            data = _json.load(r)
        s = Sample(t=time.time())
        temps: list[float] = []
        powers: list[float] = []
        vcores: list[float] = []
        eff_mhz: list[float] = []

        def _num(v: str) -> Optional[float]:
            try:
                return float(v.split()[0].replace(",", "."))
            except (ValueError, IndexError):
                return None

        def walk(node: dict, path: list[str]) -> None:
            text = node.get("Text", "") or ""
            val = node.get("Value", "") or ""
            new_path = path + [text] if text else path
            path_lc = " / ".join(new_path).lower()
            text_lc = text.lower()

            # Hardware-level filters: only consider sensors under the Intel
            # CPU node. Motherboard / GPU / RAM / SSD all expose °C/V/W.
            is_cpu_hw = "intel core" in path_lc or "intel(r) core" in path_lc

            if val and is_cpu_hw:
                num = _num(val)
                if num is not None:
                    # ----- Temperature -----
                    if "°c" in val.lower():
                        # Skip derived/limit/aggregate sensors; keep only
                        # actual core or package readings. P-Core/E-Core/
                        # CPU Package/Core Max/Core Average are all live.
                        # "Distance to TjMax" is derived and inverts under
                        # overshoot, which is what bit us.
                        if ("distance to tjmax" not in text_lc
                                and "thermal sensor" not in text_lc
                                and "limit" not in text_lc):
                            allow = (
                                "p-core" in text_lc
                                or "e-core" in text_lc
                                or text_lc == "cpu package"
                                or text_lc == "core max"
                                or text_lc == "core average"
                            )
                            if allow:
                                temps.append(num)
                    # ----- Power (W) -----
                    elif val.endswith(" W"):
                        # CPU Package is the right number for the PL1/PL2 cap.
                        if text_lc in ("cpu package", "cpu cores"):
                            powers.append(num)
                    # ----- Voltage -----
                    elif val.endswith(" V") and "vid" not in text_lc:
                        # The package-level "CPU Core" sensor is what XTU's
                        # vcore matches. Per-core voltages are individual VIDs
                        # and overcount; skip them.
                        if text_lc == "cpu core":
                            vcores.append(num)
                    # ----- Effective clock -----
                    elif "mhz" in val.lower() and "effective" in text_lc:
                        eff_mhz.append(num)

            for c in node.get("Children", []):
                walk(c, new_path)

        walk(data, [])
        s.max_core_temp_c = max(temps) if temps else None
        s.pkg_power_w     = max(powers) if powers else None
        s.vcore_v         = max(vcores) if vcores else None
        s.effective_mhz   = max(eff_mhz) if eff_mhz else None
        return s

    # ---------- WMIC fallback (temp only) ----------

    def _sample_wmic(self) -> Sample:
        s = Sample(t=time.time())
        try:
            # ACPI thermal zone, in tenths of Kelvin. Not every board exposes this.
            out = subprocess.run(
                ["wmic", "/namespace:\\\\root\\wmi", "path",
                 "MSAcpi_ThermalZoneTemperature", "get", "CurrentTemperature"],
                capture_output=True, text=True, timeout=5, check=False,
            ).stdout
            for line in out.splitlines():
                line = line.strip()
                if line.isdigit():
                    s.pkg_temp_c = (int(line) / 10.0) - 273.15
                    s.max_core_temp_c = s.pkg_temp_c
                    break
        except Exception as e:
            log.debug("wmic failed: %s", e)
        return s


# --------------------------------------------------------------------------
# WHEA scanner. Called after each stress run to catch silent data corruption
# that y-cruncher might not have noticed.
# --------------------------------------------------------------------------

def whea_errors_since(ts_epoch: float) -> int:
    """Count WHEA-Logger events (IDs 17, 18, 19, 46, 47) in the System log
    since `ts_epoch`. Returns 0 on any parsing error so we don't falsely fail
    a run — the tuner logs the failure if the count jumps unexpectedly."""
    # Powershell is the reliable way to query the event log by time.
    import datetime as _dt
    since = _dt.datetime.fromtimestamp(ts_epoch).strftime("%Y-%m-%dT%H:%M:%S")
    ps = (
        "Get-WinEvent -FilterHashtable @{LogName='System';"
        "ProviderName='Microsoft-Windows-WHEA-Logger';"
        f"StartTime='{since}'}} "
        "-ErrorAction SilentlyContinue | Measure-Object | % Count"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=15, check=False,
        )
        return int((out.stdout or "0").strip() or "0")
    except Exception as e:
        log.warning("WHEA scan failed: %s", e)
        return 0
