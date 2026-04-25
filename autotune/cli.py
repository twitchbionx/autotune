"""CLI logic for autotune.

Kept in its own module (rather than __main__.py) so PyInstaller-frozen
binaries can import it via the package namespace. __main__.py is a thin
shim that just calls main() from here.
"""

from __future__ import annotations

import argparse
import ctypes
import logging
import signal
import sys
from pathlib import Path

from .config import load_tuner_config
from .monitor import Monitor
from .state import State
from .tuner import Tuner
from .watchdog import (
    install as watchdog_install,
    uninstall as watchdog_uninstall,
    run as watchdog_run,
)
from .xtu import XTU, XTUUnsupported


def _install_revert_on_signal(xtu: XTU, state: State) -> None:
    def _handler(signum, frame):
        try:
            logging.warning("Signal %d received; reverting to safe profile.", signum)
            safe = state.load_lkg() or state.load_baseline()
            if safe is not None:
                xtu.revert_to(safe)
            state.clear_pending()
        except Exception:
            logging.exception("Revert during signal handler failed.")
        finally:
            sys.exit(130)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            pass


def _is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _setup_logging(state_dir: Path, verbose: bool) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    handlers = [
        logging.FileHandler(str(state_dir / "autotune.log"), encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=handlers,
    )


def _frozen() -> bool:
    return getattr(sys, "frozen", False)


def cmd_run(args: argparse.Namespace) -> int:
    cfg = load_tuner_config(args.config)
    state = State(args.state_dir)
    xtu = XTU(cli_path=args.xtu_cli_path, dry_run=args.dry_run)
    monitor = Monitor(lhm_url=args.lhm_url)

    print("========================================")
    print(" Intel auto-tuner -- READ THIS BEFORE 'y'")
    print("========================================")
    print(f"  CPU baseline will be captured to:   {state.baseline_path}")
    print(f"  Caps (from {args.config}):")
    print(f"    Max Vcore offset:  {cfg.max_vcore_offset_mv:+d} mV")
    print(f"    Max core temp:     {cfg.max_temp_c:.0f} C")
    print(f"    Max pkg power:     {cfg.max_power_w:.0f} W")
    print(f"    Vcore kill-switch: {cfg.max_vcore_v:.3f} V")
    print(f"    P-core ratio cap:  {cfg.max_pcore_ratio}")
    suffix = " (sweep SKIPPED)" if cfg.skip_ecore_sweep else ""
    print(f"    E-core ratio cap:  {cfg.max_ecore_ratio}{suffix}")
    if cfg.tune_ring_ratio:
        print(f"    Ring ratio cap:    {cfg.max_ring_ratio}")
    print(f"  Per-step stress: {cfg.per_step_minutes:.0f} min")
    print(f"  Final confirm:   {cfg.final_confirm_minutes:.0f} min")
    print()
    print("If the machine BSODs mid-run the boot-time watchdog will revert")
    print("to last-known-good IF you installed it (autotune watchdog install).")
    print()
    if not args.yes:
        resp = input("Proceed? [y/N] ").strip().lower()
        if resp != "y":
            print("Aborted.")
            return 1

    _install_revert_on_signal(xtu, state)
    tuner = Tuner(xtu=xtu, monitor=monitor, state=state, cfg=cfg,
                  workdir=args.state_dir / "work")
    try:
        final = tuner.run()
    except XTUUnsupported as e:
        print(f"\nXTU reports this platform is LOCKED: {e}")
        print("Likely causes: non-K CPU, BIOS 'Overclocking Lock' enabled,")
        print("or OEM-locked board. No changes were made.")
        return 2
    except RuntimeError as e:
        print(f"\nTuner aborted: {e}")
        print("Your system has been reverted; check history.csv for details.")
        return 3

    print("\n== FINAL PROFILE ==")
    print(final.to_json())
    print(f"\nHistory of every attempt: {state.history_path}")
    return 0


def cmd_revert(args: argparse.Namespace) -> int:
    state = State(args.state_dir)
    xtu = XTU(cli_path=args.xtu_cli_path, dry_run=args.dry_run)
    baseline = state.load_baseline()
    if baseline is None:
        print("No baseline on file. Nothing to revert to.")
        return 1
    xtu.revert_to(baseline)
    state.clear_pending()
    print("Reverted to baseline:", baseline)
    return 0


def cmd_watchdog(args: argparse.Namespace) -> int:
    if args.wd_cmd == "install":
        if _frozen():
            command = sys.executable
            arg_prefix = "watchdog run"
            working_dir = str(Path(sys.executable).parent)
        else:
            command = args.python_exe or sys.executable
            arg_prefix = "-m autotune watchdog run"
            working_dir = args.module_root or str(Path(__file__).resolve().parent.parent)
        watchdog_install(
            state_dir=args.state_dir,
            python_exe=command,
            module_root=working_dir,
            arg_prefix=arg_prefix,
        )
        print(f"Installed scheduled task. Command: {command} {arg_prefix} "
              f"--state-dir \"{args.state_dir}\"")
        return 0
    if args.wd_cmd == "uninstall":
        watchdog_uninstall()
        print("Watchdog scheduled task removed.")
        return 0
    if args.wd_cmd == "run":
        return watchdog_run(args.state_dir, args.xtu_cli_path)
    return 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="autotune")
    ap.add_argument("--state-dir", type=Path,
                    default=Path(r"C:\ProgramData\autotune"))
    ap.add_argument("--xtu-cli-path", type=Path, default=None)
    ap.add_argument("--lhm-url", default="http://localhost:8085/data.json")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")

    sub = ap.add_subparsers(dest="cmd")

    p_run = sub.add_parser("run", help="Run the auto-tuner.")
    p_run.add_argument("--config", type=Path, required=True)
    p_run.add_argument("--yes", action="store_true",
                       help="Skip the confirmation prompt.")

    sub.add_parser("revert", help="Revert to the captured baseline.")

    p_wd = sub.add_parser("watchdog", help="Manage the boot-time watchdog.")
    wd_sub = p_wd.add_subparsers(dest="wd_cmd", required=True)
    wd_install = wd_sub.add_parser("install",
                                   help="Install boot-time Scheduled Task.")
    wd_install.add_argument("--python-exe", default=None,
                            help="(Dev only) Python to invoke.")
    wd_install.add_argument("--module-root", default=None,
                            help="(Dev only) Directory containing autotune.")
    wd_sub.add_parser("uninstall", help="Remove the scheduled task.")
    wd_sub.add_parser("run", help="Manual watchdog invocation.")

    args = ap.parse_args(argv)
    if args.cmd is None:
        ap.print_help()
        return 1

    needs_admin = (args.cmd in ("run", "revert", "watchdog")) and not args.dry_run
    if needs_admin and not _is_admin():
        print("ERROR: This tool must be run as Administrator.", file=sys.stderr)
        return 4

    _setup_logging(args.state_dir, args.verbose)

    if args.cmd == "run":
        return cmd_run(args)
    if args.cmd == "revert":
        return cmd_revert(args)
    if args.cmd == "watchdog":
        return cmd_watchdog(args)
    ap.print_help()
    return 1
