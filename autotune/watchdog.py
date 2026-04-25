"""
Safe-boot watchdog.

Runs automatically at machine startup via a Scheduled Task. Its job:

    1. Check `pending.json`. If it exists and its `_commit_by` timestamp
       has expired, the machine almost certainly rebooted unexpectedly
       while a risky profile was applied. Revert to last_known_good.

    2. If `last_known_good.json` is missing but `baseline.json` exists,
       revert to baseline. This is the "I have no idea what's applied"
       fallback.

    3. If nothing is pending, do nothing -- the normal boot path.

Install the scheduled task with `python -m autotune.watchdog install`.
Uninstall with `... uninstall`. Manual invocation: `... run`.

The scheduled task is registered under SYSTEM with /RL HIGHEST so it
runs before any user login, minimizing the window in which a bad
profile is applied to a desktop session.
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from pathlib import Path

from .state import State
from .xtu import XTU, Profile

log = logging.getLogger(__name__)


_TASK_NAME = "AutotuneWatchdog"


def _task_xml(
    command: str, arguments: str, working_dir: str,
) -> str:
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>
    <BootTrigger><Enabled>true</Enabled></BootTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>S-1-5-18</UserId>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>false</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <Arguments>{arguments}</Arguments>
      <WorkingDirectory>{working_dir}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def install(
    state_dir: Path,
    python_exe: str,
    module_root: str,
    arg_prefix: str = "-m autotune.watchdog run",
) -> None:
    """Register the boot-time Scheduled Task.

    arg_prefix: the arguments that should run the watchdog. For a Python
    source install this is "-m autotune.watchdog run" (or the new
    "-m autotune watchdog run"). For a frozen .exe this is just
    "watchdog run". The --state-dir argument is appended automatically.
    """
    args = f'{arg_prefix} --state-dir "{state_dir}"'
    xml = _task_xml(python_exe, args, module_root)
    xml_path = state_dir / "_watchdog_task.xml"
    state_dir.mkdir(parents=True, exist_ok=True)
    # Scheduled tasks want UTF-16 LE with BOM
    xml_path.write_text(xml, encoding="utf-16")
    r = subprocess.run(
        ["schtasks", "/Create", "/TN", _TASK_NAME,
         "/XML", str(xml_path), "/F"],
        capture_output=True, text=True, check=False,
    )
    if r.returncode != 0:
        raise RuntimeError(f"schtasks install failed: {r.stdout} {r.stderr}")
    log.info("Installed scheduled task '%s'", _TASK_NAME)


def uninstall() -> None:
    subprocess.run(
        ["schtasks", "/Delete", "/TN", _TASK_NAME, "/F"],
        capture_output=True, text=True, check=False,
    )
    log.info("Uninstalled scheduled task '%s'", _TASK_NAME)


def run(state_dir: Path, xtu_cli_path: Path | None = None) -> int:
    """The actual at-boot logic. Returns 0 on success, 1 on error."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s watchdog %(levelname)s %(message)s",
        filename=str(state_dir / "watchdog.log"),
        filemode="a",
    )
    try:
        state = State(state_dir)
        pending = state.load_pending()
        if pending is None:
            log.info("No pending profile; nothing to do.")
            return 0

        log.warning(
            "Pending profile found at boot. Machine likely rebooted before "
            "commit. Pending=%s", pending,
        )
        xtu = XTU(cli_path=xtu_cli_path)
        lkg = state.load_lkg() or state.load_baseline()
        if lkg is None:
            log.error("No LKG or baseline available; cannot safely revert.")
            return 1
        xtu.revert_to(lkg)
        state.clear_pending()
        log.info("Reverted to LKG/baseline at boot: %s", lkg)
        return 0
    except Exception as e:  # noqa: BLE001
        log.exception("Watchdog crashed: %s", e)
        return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="autotune.watchdog")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_install = sub.add_parser("install")
    p_install.add_argument("--state-dir", type=Path, required=True)
    p_install.add_argument("--python-exe", default=sys.executable)
    p_install.add_argument("--module-root", required=True,
                           help="Directory containing the `autotune` package.")

    sub.add_parser("uninstall")

    p_run = sub.add_parser("run")
    p_run.add_argument("--state-dir", type=Path, required=True)
    p_run.add_argument("--xtu-cli-path", type=Path, default=None)

    args = ap.parse_args(argv)
    if args.cmd == "install":
        install(args.state_dir, args.python_exe, args.module_root)
        return 0
    if args.cmd == "uninstall":
        uninstall()
        return 0
    if args.cmd == "run":
        return run(args.state_dir, args.xtu_cli_path)
    return 2


if __name__ == "__main__":
    sys.exit(main())
