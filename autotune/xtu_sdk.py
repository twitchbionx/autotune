"""
PowerShell-bridged wrapper for Intel.Overclocking.SDK.

Why PowerShell instead of pythonnet:
  * pythonnet has no prebuilt wheels for Python 3.13+ as of April 2026.
    Source compilation routes through NuGet which fails on this user's
    box. Python 3.14 simply has no working pythonnet path right now.
  * PowerShell is on every Windows since Win7. The bundled .exe just
    has to find powershell.exe; no .NET interop runtime needed.
  * The same SDK calls work the same way -- we just spawn a long-running
    PS process (sdk_bridge.ps1), exchange JSON over stdin/stdout, and
    keep it alive across many calls so SDK init pays once.

Verified end-to-end on a 12900K (April 2026):
  TuningLibrary.Instance + Initialize() boot cleanly.
  GetControl(34).ActiveValue reads live Core Voltage Offset.
  Tune(34, -25, false) + ApplyChanges(false) writes -25 mV; read-back
    confirms; revert lands cleanly back at 0 mV.
  TuningResult.GeneralCode == "Success" indicates the write took.

This module exposes the same Python API the in-process pythonnet version
did, so sdk_backend.py and the CLI subcommands don't change.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)


_DEFAULT_SDK_DIRS = [
    r"C:\Program Files\Intel\Intel(R) Extreme Tuning Utility\Client",
    r"C:\Program Files (x86)\Intel\Intel(R) Extreme Tuning Utility\Client",
]


class XtuSdkError(RuntimeError):
    """Raised when the SDK is unreachable or rejects an operation."""


def _find_sdk_dir(override: Optional[str] = None) -> Optional[Path]:
    if override:
        p = Path(override)
        if (p / "IntelOverclockingSDK.dll").is_file():
            return p
        raise XtuSdkError(f"No IntelOverclockingSDK.dll under {override!r}")
    for d in _DEFAULT_SDK_DIRS:
        p = Path(d)
        if (p / "IntelOverclockingSDK.dll").is_file():
            return p
    return None  # let the bridge decide / report


def _bridge_script_path() -> Path:
    """Find sdk_bridge.ps1. Lives next to this module in dev; bundled as a
    PyInstaller data file in the .exe (resolved via sys._MEIPASS)."""
    # PyInstaller-bundled path
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        p = Path(meipass) / "autotune" / "sdk_bridge.ps1"
        if p.is_file():
            return p
    # Source-checkout path
    p = Path(__file__).parent / "sdk_bridge.ps1"
    if p.is_file():
        return p
    raise XtuSdkError(
        f"sdk_bridge.ps1 not found near {__file__!r} or in PyInstaller bundle.")


class XtuSdk:
    """Subprocess façade over sdk_bridge.ps1.

    Spawns a long-running PowerShell process and keeps it warm. Methods
    are synchronous JSON request/response over the bridge's stdin/stdout.
    Use as a context manager, or call .close() when done.
    """

    def __init__(self, sdk_dir: Optional[str] = None,
                 powershell: Optional[str] = None,
                 log_path: Optional[str] = None):
        self._closed = False
        env = os.environ.copy()
        sdk_path = _find_sdk_dir(sdk_dir)
        if sdk_path:
            env["AUTOTUNE_XTU_SDK_DIR"] = str(sdk_path)

        # Always have a bridge log -- even when the user doesn't ask for one
        # -- so silent crashes are debuggable. Default to %TEMP%.
        if log_path is None:
            log_path = str(Path(os.environ.get("TEMP", ".")) /
                           "autotune_sdk_bridge.log")
        env["AUTOTUNE_BRIDGE_LOG"] = log_path
        self._bridge_log = log_path

        bridge = _bridge_script_path()
        ps_exe = powershell or "powershell.exe"

        # -NoProfile           : skip user/system profile to keep startup fast
        # -ExecutionPolicy Bypass : let our script run without being signed
        # -NonInteractive      : never prompt for input
        # -File <path>         : run the bridge script
        cmd = [ps_exe, "-NoProfile", "-ExecutionPolicy", "Bypass",
               "-NonInteractive", "-File", str(bridge)]
        log.debug("Launching SDK bridge: %s", " ".join(cmd))

        # CREATE_NO_WINDOW so we don't flash a console box when running from
        # a GUI'd .exe. Has no effect on console parents.
        creationflags = 0
        if os.name == "nt":
            creationflags = 0x08000000  # CREATE_NO_WINDOW

        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                creationflags=creationflags,
            )
        except FileNotFoundError as e:
            raise XtuSdkError(
                "powershell.exe not found. Pass `powershell=` or ensure "
                "PowerShell 5.1+ is on PATH."
            ) from e

        # Start a background thread that drains stderr so the PS process
        # never blocks on a full pipe. We log it at DEBUG.
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()

        # Read the ready signal.
        ready = self._read_response()
        if not ready.get("ok") or not ready.get("ready"):
            raise XtuSdkError(
                f"SDK bridge did not signal ready: {ready}")
        self._sdk_dir = ready.get("sdk_dir")
        log.info("SDK bridge ready (sdk_dir=%s)", self._sdk_dir)

    # ---- lifecycle ----

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._proc.poll() is None:
                self._send({"op": "quit"})
                try:
                    self._proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self._proc.terminate()
        except Exception:  # noqa: BLE001
            try: self._proc.kill()
            except Exception: pass

    # ---- I/O ----

    def _drain_stderr(self) -> None:
        try:
            for line in self._proc.stderr:  # type: ignore[union-attr]
                line = line.rstrip()
                if line:
                    log.debug("[ps-bridge stderr] %s", line)
        except Exception:  # noqa: BLE001
            pass

    def _send(self, req: dict) -> None:
        if self._proc.stdin is None or self._proc.stdin.closed:
            raise XtuSdkError("bridge stdin is closed")
        payload = json.dumps(req)
        self._proc.stdin.write(payload + "\n")
        self._proc.stdin.flush()

    def _read_response(self) -> dict:
        if self._proc.stdout is None:
            raise XtuSdkError("bridge stdout missing")
        line = self._proc.stdout.readline()
        if not line:
            # Bridge died -- pull any stderr we have and surface it,
            # plus any tail of the bridge log.
            err = ""
            try:
                err = self._proc.stderr.read() or ""  # type: ignore[union-attr]
            except Exception:
                pass
            tail = ""
            try:
                if self._bridge_log and Path(self._bridge_log).is_file():
                    with open(self._bridge_log, "r", encoding="utf-8",
                              errors="replace") as f:
                        lines = f.readlines()
                    tail = "".join(lines[-15:])
            except Exception:
                pass
            raise XtuSdkError(
                f"SDK bridge exited unexpectedly (rc={self._proc.poll()}).\n"
                f"stderr: {err.strip()[:500]}\n"
                f"bridge log ({self._bridge_log}) tail:\n{tail}")
        try:
            return json.loads(line)
        except json.JSONDecodeError as e:
            raise XtuSdkError(
                f"bad JSON from bridge: {e}: {line!r}") from e

    def _call(self, op: str, **kwargs) -> dict:
        req = {"op": op}
        req.update(kwargs)
        self._send(req)
        resp = self._read_response()
        if not resp.get("ok"):
            raise XtuSdkError(f"bridge error on {op}: {resp.get('error')}")
        return resp

    # ---- public API ----

    def ping(self) -> bool:
        return bool(self._call("ping").get("pong"))

    def get_control(self, control_id: int) -> "ControlSnapshot":
        r = self._call("get_control", id=int(control_id))
        return ControlSnapshot(
            id=int(r["id"]),
            name=str(r["name"]),
            active=float(r["active"]),
            boot=float(r["boot"]),
            default=float(r["default"]),
            proposed=float(r["proposed"]),
            units=str(r.get("units") or ""),
            control_type=str(r.get("control_type") or ""),
            read_only=bool(r["read_only"]),
            supported_count=0,  # bridge doesn't ship this; tuner doesn't need it
        )

    def is_tunable(self, control_id: int) -> bool:
        try:
            return bool(self._call("is_tunable", id=int(control_id))
                        .get("tunable"))
        except XtuSdkError as e:
            log.debug("is_tunable(%d) errored: %s", control_id, e)
            return False

    def tune(self, control_id: int, value: float | int | Decimal,
             requires_reboot: bool = False) -> bool:
        # Send as a string so bridge's [decimal]::Parse gets exact value.
        s = format(Decimal(str(value)), "f")
        r = self._call("tune", id=int(control_id), value=s,
                       requires_reboot=bool(requires_reboot))
        return self._tune_result_ok(r, f"Tune({control_id}, {value})")

    def apply(self, force_restart: bool = False) -> bool:
        r = self._call("apply", force_restart=bool(force_restart))
        return self._tune_result_ok(r, "ApplyChanges")

    def _tune_result_ok(self, r: dict, what: str) -> bool:
        """Map a TuningResult.GeneralCode to a Python bool.

        Success         -> True (write happened)
        DidNotAttempt   -> True (chip is already at requested value -- no-op
                                 is a desired outcome from the caller's POV)
        anything else   -> False, logged as warning
        """
        code = str(r.get("general_code", ""))
        if r.get("success"):
            return True
        if code == "DidNotAttempt":
            log.debug("%s -> DidNotAttempt (no change needed)", what)
            return True
        log.warning("%s -> %s", what, code or "<unknown>")
        return False

    def discard(self) -> None:
        try:
            self._call("discard")
        except XtuSdkError as e:
            log.warning("DiscardChanges errored (ignored): %s", e)


# ---------------------------------------------------------------------------
# Plain-Python view of a ClientTuningControl.
# ---------------------------------------------------------------------------

class ControlSnapshot:
    """Read-only snapshot of a ClientTuningControl's relevant fields."""

    __slots__ = ("id", "name", "active", "boot", "default", "proposed",
                 "units", "control_type", "read_only", "supported_count")

    def __init__(self, *, id: int, name: str, active: float, boot: float,
                 default: float, proposed: float, units: str,
                 control_type: str, read_only: bool, supported_count: int):
        self.id = id
        self.name = name
        self.active = active
        self.boot = boot
        self.default = default
        self.proposed = proposed
        self.units = units
        self.control_type = control_type
        self.read_only = read_only
        self.supported_count = supported_count

    def __repr__(self) -> str:  # pragma: no cover
        return (f"<ControlSnapshot id={self.id} {self.name!r} "
                f"active={self.active} boot={self.boot} "
                f"units={self.units!r} ro={self.read_only}>")
