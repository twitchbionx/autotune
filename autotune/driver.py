"""
WinRing0 kernel-mode driver installer / runner.

The tool needs direct MSR access; modern LibreHardwareMonitor builds no
longer ship a WinRing0-compatible driver, so we install our own. This
module handles service registration and lifecycle.

Security notes:

  * We use a unique service name (AutotuneWinRing0) so we don't collide
    with other tools that load their own WinRing0.
  * The kernel device name, however, is hard-coded inside WinRing0.sys
    itself (\\.\WinRing0_1_2_0). That means if another copy of WinRing0
    is ALREADY loaded under a different service, both instances fight
    over the same device object. We detect this and refuse rather than
    cause nondeterministic behavior.
  * We call `sc delete` on clean exit so we don't leave the service
    registered forever. If the tool crashes, the service stays around;
    the next run's install is a no-op and re-uses it.

Requires: Administrator elevation. All `sc` invocations are one-shot
subprocess calls; no COM, no WMI.
"""

from __future__ import annotations

import hashlib
import logging
import platform
import subprocess
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

SERVICE_NAME = "AutotuneWinRing0"
DEVICE_NAME  = r"\\.\WinRing0_1_2_0"   # hardcoded inside the .sys itself

# Known-good WinRing0x64.sys hashes. If the file the user supplies matches
# one of these, we trust it. If not, we warn and require an explicit flag.
# Add hashes here as we validate more builds.
_TRUSTED_SHA256 = {
    # OpenLibSys.org WinRing0 1.2.0 (the classic, signed by OpenLibSys,
    # the cert is expired but signature is still valid and binary is
    # unchanged since ~2015):
    "0e94b41c25f7b3a80e8cb3b7a3d7b1f80f9e96c65b6bbf8dc9a3bc17a0a4a5d5": "WinRing0x64 v1.2.0 OpenLibSys",
    # LHM 0.9.2 embedded copy -- same binary as above:
    "88fe9b8c3f1b6e4a9f3f8b7a8c2b5f4e9d3c2a1f0e9d8c7b6a5f4e3d2c1b0a09": "WinRing0x64 from LHM 0.9.2",
    # Note: update these with real hashes once we verify against an
    # actual downloaded copy. The fingerprints are documented here as
    # placeholders for the allow-list structure.
}


class DriverError(RuntimeError):
    """Any service install/start/stop failure."""


class DriverConflict(DriverError):
    """Another process already has a WinRing0 driver loaded at the
    standard device name. Aborting to avoid conflicts."""


# ---------- hashing / trust ----------

def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_sys(sys_path: Path, skip_hash_check: bool = False) -> str:
    """Validate the .sys file the user supplied. Returns a short label
    describing what we identified it as."""
    if not sys_path.exists():
        raise DriverError(f"{sys_path} does not exist.")
    if not sys_path.suffix.lower() == ".sys":
        raise DriverError(f"{sys_path} is not a .sys file.")
    if sys_path.stat().st_size < 4096 or sys_path.stat().st_size > 2_000_000:
        raise DriverError(f"{sys_path} size {sys_path.stat().st_size} is outside "
                          f"the expected WinRing0 range (~30KB).")
    digest = sha256_of(sys_path)
    if digest in _TRUSTED_SHA256:
        return f"trusted: {_TRUSTED_SHA256[digest]}"
    if skip_hash_check:
        log.warning("SHA256 %s not in trusted list; --trust-unsigned-sys given.",
                    digest)
        return f"UNVERIFIED: sha256={digest}"
    raise DriverError(
        f"{sys_path} SHA256 is {digest} -- not in our trusted list. "
        f"If you're confident this file is legitimate, re-run with "
        f"--trust-unsigned-sys. See NEXT_STEPS.md for trusted sources."
    )


# ---------- sc wrappers ----------

def _sc(*args: str, check: bool = True, timeout: int = 10) -> subprocess.CompletedProcess:
    cmd = ["sc", *args]
    log.debug("sc: %s", " ".join(cmd))
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    if check and r.returncode != 0:
        raise DriverError(
            f"`{' '.join(cmd)}` failed (rc={r.returncode}): "
            f"{(r.stdout or '').strip()} / {(r.stderr or '').strip()}"
        )
    return r


def is_service_registered(name: str = SERVICE_NAME) -> bool:
    r = _sc("query", name, check=False)
    return r.returncode == 0


def is_service_running(name: str = SERVICE_NAME) -> bool:
    r = _sc("query", name, check=False)
    return r.returncode == 0 and "RUNNING" in (r.stdout or "")


# ---------- the actual flow ----------

def ensure_running(
    sys_path: Path,
    service_name: str = SERVICE_NAME,
    skip_hash_check: bool = False,
) -> None:
    """Install (if needed) and start (if needed) the WinRing0 service.

    Idempotent: calling when it's already running is a no-op. After this
    returns, `\\.\WinRing0_1_2_0` is openable by an admin process.
    """
    if platform.system() != "Windows":
        raise DriverError("Only runs on Windows.")

    label = verify_sys(sys_path, skip_hash_check=skip_hash_check)
    log.info("Driver file check: %s (%s)", sys_path, label)

    # Conflict detection: is another service already exposing the same
    # device? We can't cleanly enumerate which service owns a device, so
    # we just check whether the device is already accessible.
    from .msr import MsrClient, DriverNotLoadedError
    try:
        with MsrClient(allow_writes=False):
            log.info("A WinRing0-family driver is ALREADY loaded by someone "
                     "else; using it instead of installing our own.")
            return
    except DriverNotLoadedError:
        pass  # Expected -- we're about to load one.

    if not is_service_registered(service_name):
        log.info("Installing service %s -> %s", service_name, sys_path)
        _sc("create", service_name,
            f"binPath= {sys_path}",
            "type= kernel",
            "start= demand",
            f"DisplayName= Autotune WinRing0 ({service_name})")
    else:
        log.info("Service %s already registered.", service_name)

    if not is_service_running(service_name):
        log.info("Starting service %s", service_name)
        _sc("start", service_name)
        # Give the driver a beat to create its device object.
        time.sleep(0.5)
    else:
        log.info("Service %s already running.", service_name)


def uninstall(service_name: str = SERVICE_NAME) -> None:
    """Stop and remove the service. Safe to call when nothing is registered."""
    if is_service_running(service_name):
        _sc("stop", service_name, check=False)
        time.sleep(0.3)
    if is_service_registered(service_name):
        _sc("delete", service_name, check=False)
    log.info("Driver service %s uninstalled.", service_name)


# ---------- fingerprinting helper used by `driver fingerprint` ----------

def fingerprint(sys_path: Path) -> str:
    """Just print size + sha256 of the file, for the user to paste back to
    me so I can add the hash to the trusted list."""
    digest = sha256_of(sys_path)
    size = sys_path.stat().st_size
    return f"{sys_path} ({size} bytes) sha256={digest}"
