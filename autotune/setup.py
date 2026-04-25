"""Bootstrap / health-check for the tool's runtime dependencies.

Three pieces have to be on the machine before the tuner can actually
run:

    1. Intel XTU  -- contributes XTUCLI.exe, which the tuner drives to
       write overclock settings. Proprietary Intel software; we install
       via winget (official package: Intel.IntelExtremeTuningUtility).
       Intel's EULA does not permit us to redistribute it, so we never
       bundle the installer.

    2. y-cruncher -- the stress-test workload. Freeware; the author
       permits redistribution with attribution. We download the
       portable zip from numberworld.org and extract it.

    3. LibreHardwareMonitor (LHM) -- MIT-licensed sensor library. We
       fetch the latest release zip from its GitHub releases and
       launch it with its web server on port 8085 so we can scrape
       sensor JSON.

Every install path is optional: if the user already has one of these,
`setup` will detect it and skip.

Everything here is a no-op on non-Windows systems so the tool can
still be unit-tested on Linux.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


# Default install layout. Can be overridden via cli flags.
DEFAULT_TOOLS_DIR = Path(r"C:\Tools")

# URLs. These are pinned to known-stable versions; bump as needed.
LHM_RELEASE_URL = (
    "https://github.com/LibreHardwareMonitor/LibreHardwareMonitor"
    "/releases/latest/download/LibreHardwareMonitor-net472.zip"
)
YCRUNCHER_URL = (
    "http://www.numberworld.org/y-cruncher/y-cruncher%20v0.8.5.9532-static.zip"
)

# winget package IDs
XTU_WINGET_ID = "Intel.IntelExtremeTuningUtility"


# --------------------------------------------------------------------------
# Status data
# --------------------------------------------------------------------------

@dataclass
class DepStatus:
    name: str
    installed: bool
    path: Optional[Path] = None
    version: Optional[str] = None
    note: str = ""


@dataclass
class Report:
    xtu: DepStatus
    ycruncher: DepStatus
    lhm: DepStatus
    lhm_running: bool
    admin: bool
    windows: bool

    def all_ready(self) -> bool:
        return (self.windows and self.admin
                and self.xtu.installed
                and self.ycruncher.installed
                and self.lhm.installed and self.lhm_running)

    def to_text(self) -> str:
        def row(label: str, ok: bool, detail: str) -> str:
            mark = "[OK]   " if ok else "[MISS] "
            return f"  {mark}{label:<20} {detail}"
        lines = [
            "Auto-tuner readiness check:",
            row("Windows",        self.windows, platform.platform()),
            row("Administrator",  self.admin,   "elevated" if self.admin else "not elevated"),
            row("Intel XTU",      self.xtu.installed,
                f"{self.xtu.path} {self.xtu.version or ''} {self.xtu.note}"),
            row("y-cruncher",     self.ycruncher.installed,
                f"{self.ycruncher.path} {self.ycruncher.version or ''} {self.ycruncher.note}"),
            row("LibreHardwareMonitor", self.lhm.installed,
                f"{self.lhm.path} {self.lhm.note}"),
            row("LHM web server", self.lhm_running,
                "http://localhost:8085 reachable" if self.lhm_running else
                "port 8085 closed (enable: LHM -> Options -> Remote Web Server)"),
        ]
        lines.append("")
        lines.append("READY" if self.all_ready() else "NOT READY -- run `autotune setup` to install missing pieces")
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------

_XTU_SEARCH = [
    r"C:\Program Files (x86)\Intel\Intel(R) Extreme Tuning Utility\Client\XTUCLI.exe",
    r"C:\Program Files\Intel\Intel(R) Extreme Tuning Utility\Client\XTUCLI.exe",
]


def detect_xtu() -> DepStatus:
    for p in _XTU_SEARCH:
        pp = Path(p)
        if pp.exists():
            ver = _probe_version(pp, ["-?"])
            return DepStatus("xtu", True, pp, ver)
    return DepStatus("xtu", False, note="not installed")


def detect_ycruncher(search_root: Path = DEFAULT_TOOLS_DIR) -> DepStatus:
    # Look for y-cruncher.exe under Tools/
    candidates = list(search_root.glob("y-cruncher*/**/y-cruncher.exe"))
    if candidates:
        # Pick the newest
        p = max(candidates, key=lambda x: x.stat().st_mtime)
        return DepStatus("ycruncher", True, p)
    # Also check PATH
    on_path = shutil.which("y-cruncher")
    if on_path:
        return DepStatus("ycruncher", True, Path(on_path))
    return DepStatus("ycruncher", False, note="not installed")


def detect_lhm(search_root: Path = DEFAULT_TOOLS_DIR) -> DepStatus:
    # Common paths
    candidates = list(search_root.glob("LibreHardwareMonitor*/LibreHardwareMonitor.exe"))
    if candidates:
        p = max(candidates, key=lambda x: x.stat().st_mtime)
        return DepStatus("lhm", True, p)
    return DepStatus("lhm", False, note="not installed")


def lhm_reachable(url: str = "http://localhost:8085") -> bool:
    try:
        with urllib.request.urlopen(url, timeout=1) as r:
            return r.status == 200
    except Exception:
        # Fall back to raw TCP probe -- some LHM versions respond 404 on /
        try:
            with socket.create_connection(("localhost", 8085), timeout=1):
                return True
        except Exception:
            return False


def _probe_version(path: Path, args: list[str]) -> Optional[str]:
    try:
        r = subprocess.run([str(path), *args], capture_output=True,
                           text=True, timeout=5, check=False)
        out = (r.stdout or "") + (r.stderr or "")
        import re
        m = re.search(r"\b(\d+\.\d+[\.\d]*)", out)
        return m.group(1) if m else None
    except Exception:
        return None


def _is_admin() -> bool:
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def build_report(tools_dir: Path = DEFAULT_TOOLS_DIR) -> Report:
    return Report(
        xtu=detect_xtu(),
        ycruncher=detect_ycruncher(tools_dir),
        lhm=detect_lhm(tools_dir),
        lhm_running=lhm_reachable(),
        admin=_is_admin(),
        windows=(platform.system() == "Windows"),
    )


# --------------------------------------------------------------------------
# Installers
# --------------------------------------------------------------------------

def install_xtu(dry_run: bool = False) -> None:
    """Install Intel XTU via winget. We don't bundle the installer because
    Intel's EULA doesn't permit redistribution."""
    cmd = ["winget", "install", "--id", XTU_WINGET_ID, "-e",
           "--accept-package-agreements", "--accept-source-agreements",
           "--silent"]
    log.info("XTU install: %s", " ".join(cmd))
    if dry_run:
        print(f"[dry-run] Would run: {' '.join(cmd)}")
        return
    r = subprocess.run(cmd, check=False)
    if r.returncode != 0:
        raise RuntimeError(
            f"winget install failed (rc={r.returncode}). "
            f"If winget isn't available, install XTU manually from "
            f"https://www.intel.com/content/www/us/en/download/17881/"
        )


def install_ycruncher(tools_dir: Path, dry_run: bool = False) -> Path:
    """Download + extract the portable y-cruncher zip."""
    tools_dir.mkdir(parents=True, exist_ok=True)
    dest_zip = tools_dir / "y-cruncher.zip"
    extract_dir = tools_dir / "y-cruncher"
    log.info("y-cruncher: downloading %s", YCRUNCHER_URL)
    if dry_run:
        print(f"[dry-run] Would download {YCRUNCHER_URL} -> {dest_zip}")
        print(f"[dry-run] Would extract to {extract_dir}")
        return extract_dir / "y-cruncher.exe"
    urllib.request.urlretrieve(YCRUNCHER_URL, dest_zip)
    log.info("y-cruncher: extracting")
    if extract_dir.exists():
        shutil.rmtree(extract_dir)
    extract_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest_zip) as zf:
        zf.extractall(extract_dir)
    # y-cruncher's zip extracts to a versioned subfolder
    found = list(extract_dir.glob("**/y-cruncher.exe"))
    if not found:
        raise RuntimeError(f"y-cruncher.exe not found under {extract_dir}")
    return found[0]


def install_lhm(tools_dir: Path, dry_run: bool = False) -> Path:
    """Download + extract the LibreHardwareMonitor release zip."""
    tools_dir.mkdir(parents=True, exist_ok=True)
    dest_zip = tools_dir / "LibreHardwareMonitor.zip"
    extract_dir = tools_dir / "LibreHardwareMonitor"
    log.info("LHM: downloading %s", LHM_RELEASE_URL)
    if dry_run:
        print(f"[dry-run] Would download {LHM_RELEASE_URL} -> {dest_zip}")
        print(f"[dry-run] Would extract to {extract_dir}")
        return extract_dir / "LibreHardwareMonitor.exe"
    urllib.request.urlretrieve(LHM_RELEASE_URL, dest_zip)
    log.info("LHM: extracting")
    if extract_dir.exists():
        shutil.rmtree(extract_dir)
    extract_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest_zip) as zf:
        zf.extractall(extract_dir)
    exe = extract_dir / "LibreHardwareMonitor.exe"
    if not exe.exists():
        # Some release zips nest one level deep
        found = list(extract_dir.glob("**/LibreHardwareMonitor.exe"))
        if not found:
            raise RuntimeError(f"LibreHardwareMonitor.exe not found under {extract_dir}")
        exe = found[0]
    # Pre-configure LHM to enable the web server (port 8085, the default).
    # LHM reads LibreHardwareMonitor.config at startup; we write that.
    cfg = exe.parent / "LibreHardwareMonitor.config"
    _write_lhm_config(cfg, dry_run=dry_run)
    return exe


def _write_lhm_config(path: Path, dry_run: bool = False) -> None:
    content = """<?xml version="1.0" encoding="utf-8"?>
<configuration>
  <appSettings>
    <add key="startMinMenuItem" value="true" />
    <add key="minTrayMenuItem" value="true" />
    <add key="runWebServerMenuItem" value="true" />
    <add key="listenerPort" value="8085" />
  </appSettings>
</configuration>
"""
    if dry_run:
        print(f"[dry-run] Would write LHM config to {path}")
        return
    path.write_text(content, encoding="utf-8")


def start_lhm(lhm_exe: Path, dry_run: bool = False, wait_seconds: float = 10.0) -> bool:
    """Launch LHM in the background and wait for its web server."""
    if dry_run:
        print(f"[dry-run] Would launch {lhm_exe}")
        return True
    log.info("Starting LHM: %s", lhm_exe)
    # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP: don't tie to this shell.
    flags = 0x00000008 | 0x00000200 if os.name == "nt" else 0
    subprocess.Popen(
        [str(lhm_exe)],
        cwd=str(lhm_exe.parent),
        creationflags=flags,
        close_fds=True,
    )
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if lhm_reachable():
            return True
        time.sleep(0.5)
    return False


# --------------------------------------------------------------------------
# Orchestrator
# --------------------------------------------------------------------------

def run_setup(
    tools_dir: Path = DEFAULT_TOOLS_DIR,
    skip_xtu: bool = False,
    skip_ycruncher: bool = False,
    skip_lhm: bool = False,
    start_lhm_after: bool = True,
    dry_run: bool = False,
) -> dict:
    """Install whatever is missing. Returns a dict of discovered paths."""
    result: dict = {}
    report = build_report(tools_dir)

    # Pre-flight
    if not report.windows and not dry_run:
        raise RuntimeError("Setup only runs on Windows.")
    if not report.admin and not dry_run:
        raise RuntimeError("Setup requires Administrator elevation.")

    # XTU
    if skip_xtu:
        log.info("Skipping XTU install (--skip-xtu).")
    elif report.xtu.installed:
        print(f"[skip] XTU already installed at {report.xtu.path}")
        result["xtu"] = report.xtu.path
    else:
        install_xtu(dry_run=dry_run)
        # Re-detect
        if not dry_run:
            d = detect_xtu()
            if d.installed:
                result["xtu"] = d.path

    # y-cruncher
    if skip_ycruncher:
        log.info("Skipping y-cruncher install.")
    elif report.ycruncher.installed:
        print(f"[skip] y-cruncher already present at {report.ycruncher.path}")
        result["ycruncher"] = report.ycruncher.path
    else:
        p = install_ycruncher(tools_dir, dry_run=dry_run)
        result["ycruncher"] = p

    # LHM
    if skip_lhm:
        log.info("Skipping LHM install.")
    elif report.lhm.installed:
        print(f"[skip] LHM already present at {report.lhm.path}")
        result["lhm"] = report.lhm.path
    else:
        p = install_lhm(tools_dir, dry_run=dry_run)
        result["lhm"] = p

    # Launch LHM
    if start_lhm_after and result.get("lhm") and not dry_run:
        if not lhm_reachable():
            ok = start_lhm(result["lhm"])
            if not ok:
                log.warning("LHM didn't come up within timeout; "
                            "you may need to start it manually.")

    return result


def write_config_from_setup(
    config_path: Path,
    setup_result: dict,
    source_template: Path,
) -> None:
    """Populate config.yaml by taking the example template and rewriting
    the ycruncher_path line to point at where we actually installed it."""
    if not source_template.exists():
        raise FileNotFoundError(source_template)
    if config_path.exists():
        log.info("Config %s already exists; not overwriting.", config_path)
        return
    content = source_template.read_text()
    if "ycruncher" in setup_result:
        yc = str(setup_result["ycruncher"]).replace("\\", "\\\\")
        import re
        content = re.sub(
            r'^ycruncher_path:.*$',
            f'ycruncher_path: "{yc}"',
            content, flags=re.M,
        )
    config_path.write_text(content)
    log.info("Wrote %s", config_path)
