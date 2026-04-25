"""CLI logic for autotune."""
from __future__ import annotations

import argparse
import ctypes
import logging
import signal
import sys
from pathlib import Path
from typing import Optional

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
from .msr import MsrClient, DriverNotLoadedError, MsrError
from . import driver as drv
from . import pawnio as pio
from . import cpu as cpu_mod
from . import setup as deps
from . import backend as bk
from .oc_mailbox import OCMailbox, PLANE_CORE


def _install_revert_on_signal(xtu, state):
    def _h(signum, frame):
        try:
            logging.warning("Signal %d received; reverting.", signum)
            safe = state.load_lkg() or state.load_baseline()
            if safe is not None:
                xtu.revert_to(safe)
            state.clear_pending()
        except Exception:
            logging.exception("Signal-revert failed.")
        finally:
            sys.exit(130)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try: signal.signal(sig, _h)
        except (ValueError, OSError): pass


def _is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _setup_logging(state_dir, verbose):
    state_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            logging.FileHandler(str(state_dir / "autotune.log"), encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def _frozen():
    return getattr(sys, "frozen", False)


def _bundled_config_template() -> Path:
    """Return the path to the example config.yaml. When frozen, PyInstaller
    extracts bundled data to sys._MEIPASS; otherwise it's next to this file."""
    if _frozen():
        base = Path(getattr(sys, "_MEIPASS", "."))
        return base / "autotune" / "config.example.yaml"
    return Path(__file__).parent / "config.example.yaml"


def cmd_run(args):
    # Pre-flight: are deps present? Bail early with actionable message.
    report = deps.build_report()
    if not report.all_ready():
        print(report.to_text())
        print("\nRun `autotune setup` first, or `autotune doctor` for details.")
        return 5

    cfg = load_tuner_config(args.config)
    state = State(args.state_dir)
    xtu = XTU(cli_path=args.xtu_cli_path, dry_run=args.dry_run)
    monitor = Monitor(lhm_url=args.lhm_url)
    print("Intel auto-tuner starting. Caps:")
    print(f"  max Vcore offset {cfg.max_vcore_offset_mv:+d} mV, "
          f"max temp {cfg.max_temp_c:.0f} C, max power {cfg.max_power_w:.0f} W")
    print(f"  P-core ratio cap {cfg.max_pcore_ratio}, "
          f"E-core sweep {'SKIPPED' if cfg.skip_ecore_sweep else 'enabled'}, "
          f"ring tuning {'ON' if cfg.tune_ring_ratio else 'OFF'}")
    if not args.yes:
        if input("Proceed? [y/N] ").strip().lower() != "y":
            return 1
    _install_revert_on_signal(xtu, state)
    t = Tuner(xtu=xtu, monitor=monitor, state=state, cfg=cfg,
              workdir=args.state_dir / "work")
    try:
        final = t.run()
    except XTUUnsupported as e:
        print("XTU says platform LOCKED:", e); return 2
    except RuntimeError as e:
        print("Tuner aborted:", e); return 3
    print("\n== FINAL ==\n" + final.to_json())
    print("History:", state.history_path)
    return 0


def cmd_revert(args):
    state = State(args.state_dir)
    xtu = XTU(cli_path=args.xtu_cli_path, dry_run=args.dry_run)
    b = state.load_baseline()
    if b is None:
        print("No baseline on file."); return 1
    xtu.revert_to(b); state.clear_pending()
    print("Reverted to", b); return 0


def cmd_watchdog(args):
    if args.wd_cmd == "install":
        if _frozen():
            cmd, pfx, wd = sys.executable, "watchdog run", str(Path(sys.executable).parent)
        else:
            cmd = args.python_exe or sys.executable
            pfx = "-m autotune watchdog run"
            wd = args.module_root or str(Path(__file__).resolve().parent.parent)
        watchdog_install(state_dir=args.state_dir, python_exe=cmd,
                         module_root=wd, arg_prefix=pfx)
        print(f"Installed: {cmd} {pfx}"); return 0
    if args.wd_cmd == "uninstall":
        watchdog_uninstall(); return 0
    if args.wd_cmd == "run":
        return watchdog_run(args.state_dir, args.xtu_cli_path)
    return 1


def cmd_doctor(args):
    """Read-only status report."""
    report = deps.build_report(args.tools_dir)
    print(report.to_text())
    return 0 if report.all_ready() else 1


def cmd_setup(args):
    """Install any missing dependencies and autowire config.yaml."""
    print(f"Installing dependencies under {args.tools_dir} "
          f"{'(DRY RUN)' if args.dry_run else ''}")
    print("  NOTE: Intel XTU is proprietary; we invoke winget to install it")
    print("  from Intel's official package. We do not bundle XTU in this exe.")
    print()
    try:
        result = deps.run_setup(
            tools_dir=args.tools_dir,
            skip_xtu=args.skip_xtu,
            skip_ycruncher=args.skip_ycruncher,
            skip_lhm=args.skip_lhm,
            start_lhm_after=not args.no_start_lhm,
            dry_run=args.dry_run,
        )
    except RuntimeError as e:
        print(f"\nSetup failed: {e}")
        return 1

    # Autowire config.yaml if absent
    if args.write_config and not args.dry_run:
        template = _bundled_config_template()
        target = args.write_config
        deps.write_config_from_setup(target, result, template)
        print(f"Wrote starter config to {target}")

    print()
    print(deps.build_report(args.tools_dir).to_text())
    return 0



def cmd_msr_probe(args):
    """Read-only dump of everything we can observe via MSRs.

    Two possible backends:
      * --pawnio-module PATH : use the signed PawnIO driver (modern,
        Microsoft-blessed, works with HVCI). PATH should be IntelMSR.bin
        downloaded from github.com/namazso/PawnIO.Modules releases.
      * --winring0-sys PATH : legacy WinRing0 path (blocklisted on
        modern Windows). Kept for completeness.

    If neither is given, tries to open whatever driver is already loaded.
    """
    print("MSR probe (read-only).")
    print()

    client = None

    # --- PawnIO path (preferred) ---
    if args.pawnio_module or args.bundled_module:
        try:
            client = pio.PawnIOClient()
            if args.bundled_module:
                print("Opening PawnIO driver; loading BUNDLED autotune.amx")
                print("(requires the UNRESTRICTED PawnIO driver variant)")
                client.load_bundled_module()
            else:
                if not args.pawnio_module.exists():
                    print(f"ERROR: {args.pawnio_module} not found")
                    return 7
                print(f"Opened PawnIO driver; loading module "
                      f"{args.pawnio_module.name} ({args.pawnio_module.stat().st_size} bytes)")
                client.load_module_from_file(args.pawnio_module)
            print(f"Module loaded. PawnIO driver version: {client.driver_version()}")
            print()
        except pio.PawnIODriverMissing as e:
            print(f"ERROR: {e}")
            print("Install PawnIO from https://pawnio.eu/ first "
                  "(winget install namazso.PawnIO).")
            return 5
        except pio.PawnIOError as e:
            print(f"PawnIO setup failed: {e}")
            return 6

    # --- WinRing0 path (fallback) ---
    elif args.winring0_sys:
        try:
            drv.ensure_running(args.winring0_sys,
                               skip_hash_check=args.trust_unsigned_sys)
        except drv.DriverError as e:
            print(f"Driver setup failed: {e}")
            return 7
        try:
            client = MsrClient(allow_writes=False)
        except DriverNotLoadedError as e:
            print(f"ERROR: {e}")
            return 5

    # --- Neither flag: try MsrClient alone ---
    else:
        print("ERROR: need --pawnio-module PATH (preferred) or --winring0-sys PATH.")
        print()
        print("The PawnIO path is strongly recommended: modern signed driver,")
        print("works with Core Isolation. Get IntelMSR.bin from")
        print("https://github.com/namazso/PawnIO.Modules/releases then run:")
        print()
        print("  autotune.exe msr-probe --pawnio-module C:\\Tools\\IntelMSR.bin")
        return 1

    try:
        snap = cpu_mod.read_snapshot(client)
        print(cpu_mod.format_snapshot(snap))
        print()
        if snap.identity.name_guess.startswith("unknown"):
            print("WARNING: CPU not in the known-chip table.")
            print("Please paste this output so I can extend the table.")
    except Exception as e:
        print(f"MSR read failed: {e}")
        return 6
    finally:
        if client is not None:
            client.close()
    return 0


def cmd_msr_test(args):
    """Read one specific MSR via the loaded PawnIO module. Useful for
    figuring out which MSRs the module's allow-list permits."""
    if not args.pawnio_module:
        print("ERROR: --pawnio-module is required.")
        return 1
    if not args.pawnio_module.exists():
        print(f"ERROR: {args.pawnio_module} not found")
        return 1

    msr_int = int(args.msr, 0)
    try:
        client = pio.PawnIOClient()
        client.load_module_from_file(args.pawnio_module)
        print(f"Reading MSR 0x{msr_int:X} via ioctl_read_msr ...")
        try:
            value = client.read_msr(msr_int)
            print(f"OK: 0x{msr_int:X} = 0x{value:016X}  ({value} dec)")
            return 0
        except pio.PawnIOError as e:
            print(f"FAIL: {e}")
            return 2
    finally:
        try: client.close()
        except: pass



def cmd_mailbox(args):
    """Send a single OC Mailbox command. Always read-back the response.

    SAFE for read-class commands (cmd & 1 == 0 by Intel convention).
    Use --i-know-this-may-write to permit odd-numbered (write) cmd codes.
    """
    if not args.pawnio_module or not args.pawnio_module.exists():
        print("ERROR: --pawnio-module PATH (e.g. C:\\Tools\\IntelMSR.bin) is required.")
        return 1

    is_likely_write = (args.mb_cmd & 1) == 1
    if is_likely_write and not args.i_know_this_may_write:
        print(f"REFUSING: cmd 0x{args.mb_cmd:X} has its low bit set, suggesting it is a")
        print("WRITE command (Intel convention: read=even, write=odd). Pass")
        print("--i-know-this-may-write to override. Be aware: writing the wrong")
        print("data to an unknown mailbox cmd can change CPU state.")
        return 2

    client = pio.PawnIOClient()
    client.load_module_from_file(args.pawnio_module)
    try:
        result = client.mailbox_probe_command(
            cmd=args.mb_cmd, domain=args.domain, data=args.data, retries=args.retries
        )
        print()
        print(f"=== OC Mailbox cmd 0x{args.mb_cmd:02X} domain={args.domain} data=0x{args.data:08X} ===")
        if "exception" in result:
            print(f"EXCEPTION: {result['exception']}")
            return 3
        print(f"  response_low32:    0x{result['response']:08X}  ({result['response']} dec)")
        print(f"  error_flags:       0x{result['error_flags']:08X}")
        print(f"  raw 64-bit word:   0x{result['raw_response_word']:016X}")
        print(f"  completed:         {result['completed']}")
        print(f"  timed_out:         {result['timed_out']}")

        # Decode common interpretations
        v_signed11 = (result['response'] >> 21) & 0x7FF
        if v_signed11 & 0x400: v_signed11 -= 0x800
        if v_signed11 != 0:
            mv = round(v_signed11 / 1.024)
            print(f"  if voltage offset: {mv} mV (signed 11-bit shifted-21)")
        return 0
    finally:
        client.close()


def cmd_mailbox_scan(args):
    """Scan a range of OC Mailbox commands READ-ONLY.

    Sends each command with domain=0, data=0, captures the response,
    and prints a table. We restrict to even-numbered cmds by default
    because Intel convention has read=even / write=odd, and this
    keeps us off write paths during exploration.
    """
    if not args.pawnio_module or not args.pawnio_module.exists():
        print("ERROR: --pawnio-module PATH is required.")
        return 1

    client = pio.PawnIOClient()
    client.load_module_from_file(args.pawnio_module)
    print(f"Scanning OC Mailbox cmd range 0x{args.start:02X}..0x{args.end:02X}, "
          f"domain={args.domain}, every={'cmd' if args.include_writes else 'even cmd'}")
    print()
    print(f"{'cmd':>4}  {'response':>10}  {'errflags':>10}  {'completed':>9}  {'as_voltage_mV':>13}")
    print("-" * 60)
    found = []
    try:
        for cmd in range(args.start, args.end + 1):
            if not args.include_writes and (cmd & 1):
                continue
            r = client.mailbox_probe_command(cmd=cmd, domain=args.domain, data=0,
                                             retries=args.retries)
            if "exception" in r:
                print(f"  0x{cmd:02X}  EXCEPTION: {r['exception']}")
                continue
            v11 = (r['response'] >> 21) & 0x7FF
            if v11 & 0x400: v11 -= 0x800
            mv = round(v11 / 1.024) if v11 else 0
            print(f"  0x{cmd:02X}  0x{r['response']:08X}  0x{r['error_flags']:08X}  "
                  f"{str(r['completed']):>9}  {mv:>13}")
            if r['completed'] and (r['response'] != 0 or r['error_flags'] != 0):
                found.append(cmd)
        print()
        print(f"{len(found)} commands returned non-zero data: {[hex(c) for c in found]}")
        print()
        print("Next steps: re-run individual cmds with mailbox-cmd to inspect more.")
        print("Try varying --domain (0..7) on commands that returned data.")
        return 0
    finally:
        client.close()


def _open_pawnio_with_module(module_path: Path) -> pio.PawnIOClient:
    """Helper: open PawnIO + load a signed module, raise on failure."""
    if not module_path.exists():
        raise FileNotFoundError(f"PawnIO module not found: {module_path}")
    client = pio.PawnIOClient()
    client.load_module_from_file(module_path)
    return client


def cmd_show_profile(args):
    """Read current writable chip state via the PawnIO + OC Mailbox backend.

    Diagnostic for the new Phase 1 backend. Read-only -- never writes.
    """
    if not args.pawnio_module:
        print("ERROR: --pawnio-module is required.")
        return 1
    client = _open_pawnio_with_module(args.pawnio_module)
    try:
        backend = bk.PawnIOBackend(client)
        backend.assert_unlocked()
        profile = backend.read()

        # Also pull static info from the mailbox for context
        bclk = backend.mb.read_bclk_mhz()
        iccmax = backend.mb.read_iccmax_amps()
        tdp = backend.mb.read_tdp_watts()

        print("=== Current writable chip state ===")
        print(f"  BCLK:          {bclk:.2f} MHz")
        print(f"  ICCMax:        {iccmax:.0f} A")
        print(f"  TDP (cmd 0x16): {tdp} W")
        print()
        print(f"  Turbo ratios (per active-core count, 1..8):")
        if profile.turbo_ratios:
            for i, r in enumerate(profile.turbo_ratios, 1):
                freq = r * bclk
                print(f"    {i} core(s): {r}  ({freq:.0f} MHz)")
        print()
        print(f"  FIVR Core voltage offset: {profile.vcore_offset_mv:+d} mV")
        print(f"  PL1 (sustained): {profile.pl1_watts} W")
        print(f"  PL2 (burst):     {profile.pl2_watts} W")
        return 0
    finally:
        client.close()


def cmd_set_voltage(args):
    """Write a FIVR voltage offset, then verify by read-back.

    SAFE: caps at +50/-200 mV unless --i-know-this-can-degrade is set.
    Auto-saves the previous value to a rollback file so 'autotune revert-uv'
    can put it back.
    """
    if not args.pawnio_module:
        print("ERROR: --pawnio-module is required.")
        return 1
    if args.mv > 50 and not args.i_know_this_can_degrade:
        print(f"REFUSING: +{args.mv} mV exceeds the +50 mV safe ceiling.")
        print("Sustained positive Vcore offsets above +50 mV degrade the chip")
        print("over weeks. Pass --i-know-this-can-degrade to override.")
        return 2
    if args.mv < -200 and not args.i_know_this_can_degrade:
        print(f"REFUSING: {args.mv} mV exceeds the -200 mV safe floor.")
        print("Aggressive undervolts can cause silent corruption. Override with")
        print("--i-know-this-can-degrade.")
        return 2

    client = _open_pawnio_with_module(args.pawnio_module)
    try:
        backend = bk.PawnIOBackend(client)
        backend.assert_unlocked()
        before = backend.mb.read_fivr_offset_mv(PLANE_CORE)
        print(f"Current FIVR Core voltage offset: {before:+d} mV")

        # Save rollback
        rollback_path = args.state_dir / "vcore_rollback.txt"
        rollback_path.parent.mkdir(parents=True, exist_ok=True)
        rollback_path.write_text(str(before))
        print(f"Saved rollback ({before:+d} mV) to {rollback_path}")

        print(f"Writing {args.mv:+d} mV ...")
        backend.mb.write_fivr_offset_mv(PLANE_CORE, args.mv)
        after = backend.mb.read_fivr_offset_mv(PLANE_CORE)
        print(f"Read-back: {after:+d} mV")
        if after != args.mv:
            print(f"MISMATCH: wrote {args.mv:+d}, read back {after:+d}.")
            return 3
        print("OK -- write applied and verified.")
        return 0
    except pio.PawnIOError as e:
        print(f"FAILED: {e}")
        return 4
    finally:
        client.close()


def _find_ycruncher() -> Optional[Path]:
    """Look for y-cruncher.exe in common locations."""
    from pathlib import Path
    candidates = [
        Path(r"C:\Tools\y-cruncher\y-cruncher.exe"),
    ]
    user = Path.home() / "Downloads"
    if user.exists():
        for p in user.glob("y-cruncher*/y-cruncher.exe"):
            candidates.append(p)
        for p in user.glob("y-cruncher*/**/y-cruncher.exe"):
            candidates.append(p)
    for c in candidates:
        if c.exists():
            return c
    return None


def cmd_undervolt(args):
    """Voltage shmoo: find the minimum stable FIVR Core voltage offset.

    Algorithm:
      1. Capture baseline offset (current value, used as rollback target).
      2. Pre-flight stress at baseline -- must pass or we abort.
      3. Step voltage DOWN by --step-mv each iteration.
      4. Run y-cruncher for --step-minutes at each setting.
      5. On first failure (instability, temp cap, power cap), back off to
         "last_stable + safety_margin" and apply that as the final value.
      6. Stop early if min-mv floor reached (sweep completed at floor).

    Every step is logged to undervolt_history.csv. On Ctrl-C / unhandled
    failure, the baseline offset is restored.
    """
    from .stress import Caps, run_ycruncher, StressResult
    from .monitor import Monitor

    if not args.pawnio_module:
        print("ERROR: --pawnio-module is required.")
        return 1

    # Locate y-cruncher
    yc = args.ycruncher_path or _find_ycruncher()
    if yc is None or not yc.exists():
        print("ERROR: y-cruncher not found. Pass --ycruncher-path PATH or")
        print("install at C:\\Tools\\y-cruncher\\y-cruncher.exe")
        return 1
    print(f"Using y-cruncher at: {yc}")

    client = _open_pawnio_with_module(args.pawnio_module)
    backend = bk.PawnIOBackend(client)
    backend.assert_unlocked()

    baseline = backend.mb.read_fivr_offset_mv(PLANE_CORE)
    print(f"Baseline FIVR Core offset: {baseline:+d} mV")

    rollback_path = args.state_dir / "vcore_rollback.txt"
    rollback_path.parent.mkdir(parents=True, exist_ok=True)
    rollback_path.write_text(str(baseline))
    print(f"Saved rollback ({baseline:+d} mV) to {rollback_path}")

    history_path = args.state_dir / "undervolt_history.csv"
    if not history_path.exists():
        import csv
        with history_path.open("w", newline="") as f:
            csv.writer(f).writerow([
                "timestamp", "phase", "offset_mv", "passed", "reason",
                "duration_s", "peak_temp_c", "peak_power_w",
            ])

    def log_step(phase: str, offset: int, result):
        import csv, datetime
        row = [
            datetime.datetime.now().isoformat(timespec="seconds"),
            phase, offset, int(bool(result.passed)),
            result.reason, round(result.duration_s, 1),
            result.stats.peak_temp() if result.stats else None,
            result.stats.peak_power() if result.stats else None,
        ]
        with history_path.open("a", newline="") as f:
            csv.writer(f).writerow(row)

    def install_revert_handler():
        import signal as _signal
        def _h(signum, frame):
            print(f"\n[signal {signum}] Reverting to {baseline:+d} mV...")
            try:
                backend.mb.write_fivr_offset_mv(PLANE_CORE, baseline)
            except Exception as e:
                print(f"REVERT FAILED: {e}")
            sys.exit(130)
        try: _signal.signal(_signal.SIGINT, _h)
        except (ValueError, OSError): pass
        try: _signal.signal(_signal.SIGTERM, _h)
        except (ValueError, OSError): pass

    install_revert_handler()

    monitor = Monitor(lhm_url=args.lhm_url)
    caps = Caps(
        max_temp_c=args.max_temp,
        max_power_w=args.max_power,
        max_vcore_v=args.max_vcore_v,
    )
    workdir = args.state_dir / "stress"
    workdir.mkdir(parents=True, exist_ok=True)

    try:
        # ---- Pre-flight at baseline ----
        print(f"\n=== PRE-FLIGHT ({args.preflight_minutes:.0f} min at "
              f"{baseline:+d} mV) ===")
        pre = run_ycruncher(yc, workdir, args.preflight_minutes, caps, monitor)
        log_step("preflight", baseline, pre)
        if not pre.passed:
            print(f"\nPre-flight FAILED ({pre.reason}). Baseline is unstable; "
                  "fix the system before undervolting.")
            return 2
        print(f"Pre-flight PASSED in {pre.duration_s:.0f}s")

        # ---- Shmoo loop ----
        last_stable = baseline
        current = baseline

        while True:
            next_offset = current - args.step_mv
            if next_offset < args.min_mv:
                print(f"\nReached min floor {args.min_mv} mV. Stopping shmoo "
                      f"with {current:+d} mV stable.")
                break
            print(f"\n=== STEP: {next_offset:+d} mV "
                  f"({args.step_minutes:.0f} min) ===")
            try:
                backend.mb.write_fivr_offset_mv(PLANE_CORE, next_offset)
            except pio.PawnIOError as e:
                print(f"Voltage write rejected: {e}")
                break

            r = run_ycruncher(yc, workdir, args.step_minutes, caps, monitor)
            log_step("shmoo", next_offset, r)

            if r.passed:
                print(f"  PASSED at {next_offset:+d} mV "
                      f"(peak temp {r.stats.peak_temp()}°C "
                      f"power {r.stats.peak_power()}W)")
                last_stable = next_offset
                current = next_offset
            else:
                print(f"  FAILED at {next_offset:+d} mV ({r.reason})")
                break

        # ---- Apply final ----
        final_offset = last_stable + args.safety_margin_mv
        # Don't undo any progress: cap final at the most-conservative
        # baseline-or-last_stable value
        if final_offset > baseline:
            final_offset = baseline
        print(f"\n=== RESULT ===")
        print(f"  Last verified stable: {last_stable:+d} mV")
        print(f"  Safety margin:        +{args.safety_margin_mv} mV")
        print(f"  Final applied:        {final_offset:+d} mV")
        print(f"  Savings vs baseline:  {baseline - final_offset} mV undervolt")
        backend.mb.write_fivr_offset_mv(PLANE_CORE, final_offset)
        print(f"Applied {final_offset:+d} mV; CSV at {history_path}")
        return 0
    except Exception as e:
        print(f"\nUnhandled error: {e}. Reverting to baseline.")
        try:
            backend.mb.write_fivr_offset_mv(PLANE_CORE, baseline)
        except Exception as e2:
            print(f"REVERT FAILED: {e2}")
        return 3
    finally:
        client.close()


def cmd_overclock(args):
    """Auto-overclock: per-active-count ratio sweep with inner voltage shmoo.

    Algorithm (matches the design from autotune/tuner.py for the new backend):

      1. Capture baseline (turbo_ratios + vcore offset).
      2. Pre-flight stress at baseline -- must pass.
      3. Outer loop: bump all 8 per-active-count turbo ratios by +1.
         Stops when 1-core ratio hits --max-pcore-ratio.
      4. Inner loop (voltage shmoo) at each ratio step:
           a. Apply (new_ratios, current_voltage).
           b. Run y-cruncher.
           c. If pass: save as new last-stable, continue outer loop.
           d. If fail (instability): raise voltage by +step-up-mv, retry.
           e. Cap reached: can't stabilize at this ratio, fall back.
      5. Final = last-stable ratios + their min-stable voltage.

    Rollback on Ctrl-C / unhandled exception. CSV log every step.
    """
    from .stress import Caps, run_ycruncher
    from .monitor import Monitor

    yc = args.ycruncher_path or _find_ycruncher()
    if yc is None or not yc.exists():
        print("ERROR: y-cruncher not found.")
        return 1
    print(f"Using y-cruncher at: {yc}")

    backend, _backend_cleanup = _open_backend(args)
    print(f"Backend: {type(backend).__name__}")
    backend.assert_unlocked()

    baseline_profile = backend.read()
    baseline_ratios = list(baseline_profile.turbo_ratios)
    baseline_offset = baseline_profile.vcore_offset_mv
    print(f"\nBaseline:")
    print(f"  turbo_ratios: {baseline_ratios}")
    print(f"  vcore offset: {baseline_offset:+d} mV")

    # Save rollback
    rollback_path = args.state_dir / "oc_rollback.json"
    rollback_path.parent.mkdir(parents=True, exist_ok=True)
    rollback_path.write_text(baseline_profile.to_json())
    print(f"Saved rollback to {rollback_path}")

    # CSV log header
    history_path = args.state_dir / "overclock_history.csv"
    if not history_path.exists():
        import csv
        with history_path.open("w", newline="") as f:
            csv.writer(f).writerow([
                "timestamp", "phase", "ratio_1c", "ratio_2c", "ratio_3c",
                "ratio_4c", "ratio_5c", "ratio_6c", "ratio_7c", "ratio_8c",
                "vcore_offset_mv", "passed", "reason",
                "duration_s", "peak_temp_c", "peak_power_w",
            ])

    def log_step(phase: str, ratios, mv, result):
        import csv, datetime
        row = [datetime.datetime.now().isoformat(timespec="seconds"), phase] + \
              list(ratios) + [
                  mv, int(bool(result.passed)), result.reason,
                  round(result.duration_s, 1),
                  result.stats.peak_temp() if result.stats else None,
                  result.stats.peak_power() if result.stats else None,
              ]
        with history_path.open("a", newline="") as f:
            csv.writer(f).writerow(row)

    # Signal handler: revert to baseline on Ctrl-C
    def install_revert():
        import signal as _sig
        def _h(signum, frame):
            print(f"\n[signal {signum}] reverting to baseline...")
            try:
                backend.revert_to(baseline_profile)
            except Exception as e:
                print(f"REVERT FAILED: {e}")
            sys.exit(130)
        for s in (_sig.SIGINT, _sig.SIGTERM):
            try: _sig.signal(s, _h)
            except (ValueError, OSError): pass
    install_revert()

    monitor = Monitor(lhm_url=args.lhm_url)
    caps = Caps(max_temp_c=args.max_temp,
                max_power_w=args.max_power,
                max_vcore_v=args.max_vcore_v)
    workdir = args.state_dir / "stress"
    workdir.mkdir(parents=True, exist_ok=True)

    try:
        # ---- Pre-flight ----
        print(f"\n=== PRE-FLIGHT ({args.preflight_minutes:.0f} min) ===")
        pre = run_ycruncher(yc, workdir, args.preflight_minutes, caps, monitor)
        log_step("preflight", baseline_ratios, baseline_offset, pre)
        if not pre.passed:
            print(f"Pre-flight FAILED ({pre.reason}). Aborting.")
            return 2
        print(f"Pre-flight PASSED in {pre.duration_s:.0f}s")

        # ---- Outer loop: ratio sweep ----
        last_stable_ratios = baseline_ratios.copy()
        last_stable_offset = baseline_offset

        while max(last_stable_ratios) < args.max_pcore_ratio:
            # Bump all 8 ratios by +1 (uniform sweep)
            candidate_ratios = [r + 1 for r in last_stable_ratios]
            # Cap each entry at max_pcore_ratio
            candidate_ratios = [min(r, args.max_pcore_ratio) for r in candidate_ratios]
            if candidate_ratios == last_stable_ratios:
                print(f"\nAll ratios at cap {args.max_pcore_ratio}; done.")
                break

            print(f"\n=== RATIO STEP: trying {candidate_ratios} ===")

            # Inner voltage shmoo
            shmoo_voltage = _voltage_shmoo(
                backend, candidate_ratios,
                start_offset=last_stable_offset,
                args=args, workdir=workdir, caps=caps, monitor=monitor,
                yc=yc, log_step=log_step,
            )
            if shmoo_voltage is None:
                print(f"Could not stabilize ratios {candidate_ratios} within "
                      f"voltage cap +{args.max_vcore_offset_mv} mV. Stopping.")
                break

            # New stable point
            last_stable_ratios = candidate_ratios
            last_stable_offset = shmoo_voltage
            print(f"  -> stable at {candidate_ratios} with {shmoo_voltage:+d} mV")

        # ---- Apply final ----
        print(f"\n=== RESULT ===")
        print(f"  baseline_ratios:    {baseline_ratios}")
        print(f"  final_ratios:       {last_stable_ratios}")
        print(f"  vcore offset final: {last_stable_offset:+d} mV "
              f"(baseline {baseline_offset:+d} mV)")
        final = bk.Profile(
            turbo_ratios=last_stable_ratios,
            vcore_offset_mv=last_stable_offset,
        )
        backend.apply(final)
        print(f"Applied. CSV: {history_path}")
        return 0
    except Exception as e:
        print(f"\nUnhandled error: {e}. Reverting baseline.")
        try:
            backend.revert_to(baseline_profile)
        except Exception as e2:
            print(f"REVERT FAILED: {e2}")
        return 3
    finally:
        try: _backend_cleanup()
        except Exception: pass


def _voltage_shmoo(backend, ratios, start_offset, args,
                   workdir, caps, monitor, yc, log_step):
    """Inner shmoo at fixed ratios: find lowest vcore offset that passes.

    Strategy:
      - Apply (ratios, start_offset). Stress.
      - If pass: try to lower by --voltage-step-down-mv and confirm.
      - If fail: raise by --voltage-step-up-mv and retry.
      - Cap at +max_vcore_offset_mv. None if unreachable.
    """
    from .stress import run_ycruncher

    offset = start_offset
    last_stable: Optional[int] = None
    last_failed: Optional[int] = None
    tried = set()

    for _ in range(args.shmoo_max_iterations):
        if offset in tried:
            break
        tried.add(offset)

        # Bound check
        if offset > args.max_vcore_offset_mv:
            print(f"  voltage cap +{args.max_vcore_offset_mv} mV reached")
            break

        # Apply ratios + this offset (backend-agnostic via Profile API).
        try:
            backend.apply(bk.Profile(turbo_ratios=ratios,
                                     vcore_offset_mv=offset))
        except Exception as e:
            print(f"  apply failed: {e}")
            return last_stable

        print(f"  shmoo @ {offset:+d} mV ({args.step_minutes:.0f} min)...")
        r = run_ycruncher(yc, workdir, args.step_minutes, caps, monitor)
        log_step("shmoo", ratios, offset, r)

        if r.passed:
            print(f"    PASS")
            last_stable = offset
            # Try lower next iteration
            next_offset = offset - args.voltage_step_down_mv
            if last_failed is not None and next_offset <= last_failed:
                # We've bracketed; converged
                return last_stable
            offset = next_offset
        else:
            print(f"    FAIL ({r.reason})")
            if r.reason == "ycruncher_setup_error":
                # Tooling problem, not a chip problem. Bail out of the entire
                # tuning run rather than wasting time on more shmoo iterations.
                raise RuntimeError(
                    "y-cruncher menu walk failed -- check that y-cruncher's "
                    "build matches what stress.py expects (v0.8.x menu)")
            if r.reason in ("temp_cap", "power_cap", "vcore_cap"):
                # Hit a hardware cap: don't try to push voltage higher
                return last_stable
            last_failed = offset
            # Raise voltage
            offset += args.voltage_step_up_mv
            if offset > args.max_vcore_offset_mv:
                return last_stable

    return last_stable


def cmd_revert_voltage(args):
    """Restore the saved FIVR Core voltage offset (from previous set-voltage)."""
    rollback_path = args.state_dir / "vcore_rollback.txt"
    if not rollback_path.exists():
        print(f"No rollback file at {rollback_path}; setting offset to 0 mV.")
        target = 0
    else:
        target = int(rollback_path.read_text().strip())
        print(f"Reverting to {target:+d} mV (from {rollback_path})")

    if not args.pawnio_module:
        print("ERROR: --pawnio-module is required.")
        return 1
    client = _open_pawnio_with_module(args.pawnio_module)
    try:
        backend = bk.PawnIOBackend(client)
        backend.mb.write_fivr_offset_mv(PLANE_CORE, target)
        readback = backend.mb.read_fivr_offset_mv(PLANE_CORE)
        print(f"OK -- offset is now {readback:+d} mV")
        return 0
    finally:
        client.close()


# ---------------------------------------------------------------------------
# SDK-driven subcommands (Option B: drive XTU's signed driver via the .NET
# SDK instead of via OC Mailbox / our own kernel module).
# ---------------------------------------------------------------------------

def _open_sdk(args):
    """Lazy SDK construction with friendly error messages."""
    from .xtu_sdk import XtuSdk, XtuSdkError
    try:
        return XtuSdk(sdk_dir=getattr(args, "xtu_sdk_dir", None))
    except XtuSdkError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        print("Hint: ensure Intel XTU 7.14+ is installed and that you're "
              "running this elevated.", file=sys.stderr)
        sys.exit(2)


def _open_backend(args):
    """Open whichever backend (--backend pawnio | sdk) was selected.

    Returns (backend, cleanup_callable). The cleanup function MUST be called
    in a finally block -- for the SDK backend it terminates the PowerShell
    bridge subprocess; for the PawnIO backend it closes the device handle.
    """
    kind = getattr(args, "backend", "pawnio")
    if kind == "sdk":
        from .sdk_backend import XtuSdkBackend
        sdk = _open_sdk(args)
        backend = XtuSdkBackend(sdk=sdk)
        return backend, sdk.close
    if kind == "pawnio":
        if not getattr(args, "pawnio_module", None):
            print("ERROR: --pawnio-module is required for the pawnio backend.",
                  file=sys.stderr)
            sys.exit(1)
        client = _open_pawnio_with_module(args.pawnio_module)
        backend = bk.PawnIOBackend(client)
        return backend, client.close
    print(f"ERROR: unknown backend {kind!r}", file=sys.stderr)
    sys.exit(1)


def cmd_sdk_show(args):
    """Print every knob the auto-overclocker cares about, with live state
    fetched through the XTU SDK. Pure read; safe."""
    from . import sdk_ids
    sdk = _open_sdk(args)
    print(f"{'Knob':<35} {'Id':>10}  {'Active':>10}  {'Boot':>10}  "
          f"{'Default':>10}  {'Units':<8} {'RO'}")
    print("-" * 100)
    knobs = [
        ("Core Voltage Offset",          sdk_ids.CORE_VOLTAGE_OFFSET),
        ("Cache Voltage Offset",         sdk_ids.CACHE_VOLTAGE_OFFSET),
        ("System Agent Voltage Offset",  sdk_ids.SYSTEM_AGENT_VOLTAGE_OFFSET),
        ("Core Voltage",                 sdk_ids.CORE_VOLTAGE),
        ("Performance Core Ratio",       sdk_ids.PERFORMANCE_CORE_RATIO),
        ("Processor Cache Ratio",        sdk_ids.PROCESSOR_CACHE_RATIO),
        ("Efficient Core Ratio",         sdk_ids.EFFICIENT_CORE_RATIO),
        ("AVX2 Ratio Offset",            sdk_ids.AVX2_RATIO_OFFSET),
        ("Processor Core IccMax",        sdk_ids.PROCESSOR_CORE_ICCMAX),
        ("Turbo Boost Power Max (PL1)",  sdk_ids.TURBO_BOOST_POWER_MAX),
        ("Turbo Boost Short Power Max",  sdk_ids.TURBO_BOOST_SHORT_POWER_MAX),
        ("Turbo Power Time Window",      sdk_ids.TURBO_BOOST_POWER_TIME_WINDOW),
        ("Reference Clock",              sdk_ids.REFERENCE_CLOCK),
        ("Overclocking Lock",            sdk_ids.OVERCLOCKING_LOCK),
    ]
    for name, cid in knobs:
        try:
            c = sdk.get_control(cid)
            print(f"{name:<35} {c.id:>10}  {c.active:>10.4g}  {c.boot:>10.4g}  "
                  f"{c.default:>10.4g}  {c.units:<8} {'Y' if c.read_only else 'N'}")
        except Exception as e:  # noqa: BLE001
            print(f"{name:<35} {cid:>10}  <error: {e}>")
    # Per-active-count P-core ratios
    print("\nPer-active-count P-core ratios:")
    for n_active, cid in sdk_ids.PCORE_RATIO_BY_COUNT.items():
        try:
            c = sdk.get_control(cid)
            print(f"  {n_active} active core(s): id={cid:<3}  "
                  f"active={int(c.active)}x  boot={int(c.boot)}x  "
                  f"default={int(c.default)}x")
        except Exception as e:  # noqa: BLE001
            print(f"  {n_active} active core(s): id={cid}  <error: {e}>")
    return 0


def cmd_sdk_snapshot(args):
    """Print a Profile snapshot via XtuSdkBackend.read() -- the same shape
    the tuner consumes."""
    from .sdk_backend import XtuSdkBackend
    sdk = _open_sdk(args)
    backend = XtuSdkBackend(sdk=sdk)
    backend.assert_unlocked()
    p = backend.read()
    print(p.to_json())
    return 0


def cmd_sdk_set_voltage(args):
    """Apply a Core Voltage Offset via the SDK. Verifies via read-back."""
    from .sdk_backend import XtuSdkBackend
    from . import sdk_ids
    mv = int(args.mv)
    if abs(mv) > 200:
        print(f"ERROR: refusing offset of {mv:+d} mV (cap is +/-200 here).",
              file=sys.stderr)
        return 3
    sdk = _open_sdk(args)
    backend = XtuSdkBackend(sdk=sdk)
    backend.assert_unlocked()

    # Save rollback so cmd_sdk_revert_voltage can use it.
    pre = sdk.get_control(sdk_ids.CORE_VOLTAGE_OFFSET)
    rollback = args.state_dir / "sdk_vcore_rollback.txt"
    rollback.parent.mkdir(parents=True, exist_ok=True)
    rollback.write_text(str(int(round(pre.active))))

    if not sdk.tune(sdk_ids.CORE_VOLTAGE_OFFSET, mv):
        print("ERROR: Tune() rejected by SDK.", file=sys.stderr)
        sdk.discard()
        return 3
    if not sdk.apply():
        print("ERROR: ApplyChanges() reported failure.", file=sys.stderr)
        sdk.discard()
        return 3
    after = sdk.get_control(sdk_ids.CORE_VOLTAGE_OFFSET)
    print(f"Core Voltage Offset: {int(round(pre.active)):+d} -> "
          f"{int(round(after.active)):+d} mV "
          f"(rollback saved to {rollback})")
    return 0 if int(round(after.active)) == mv else 4


def cmd_stress_test(args):
    """Run y-cruncher's component-stress test for N minutes against the
    chip's CURRENT settings, with the same monitor + cap enforcement the
    auto-overclocker uses internally.

    Use this to verify a tuning result holds up over a longer window than
    the per-step shmoo allows. Exits non-zero if anything fails.
    """
    from .stress import Caps, run_ycruncher
    from .monitor import Monitor

    yc = args.ycruncher_path or _find_ycruncher()
    if yc is None or not yc.exists():
        print("ERROR: y-cruncher not found.", file=sys.stderr)
        return 1
    print(f"Using y-cruncher at: {yc}")

    monitor = Monitor(lhm_url=args.lhm_url)
    caps = Caps(max_temp_c=args.max_temp,
                max_power_w=args.max_power,
                max_vcore_v=args.max_vcore_v)
    workdir = args.state_dir / "stress"
    workdir.mkdir(parents=True, exist_ok=True)

    print(f"\nRunning y-cruncher stress-test for {args.minutes} min "
          f"(temp cap {args.max_temp}°C, power cap {args.max_power}W, "
          f"vcore cap {args.max_vcore_v}V)...\n")
    r = run_ycruncher(yc, workdir, args.minutes, caps, monitor)
    if r.passed:
        print(f"\nPASSED in {r.duration_s:.0f}s")
        if r.stats and r.stats.peak_temp() is not None:
            print(f"  peak temp:  {r.stats.peak_temp()}°C")
        if r.stats and r.stats.peak_power() is not None:
            print(f"  peak power: {r.stats.peak_power()}W")
        if r.stats and r.stats.peak_vcore() is not None:
            print(f"  peak vcore: {r.stats.peak_vcore()}V")
        return 0
    print(f"\nFAILED ({r.reason}) in {r.duration_s:.0f}s")
    if r.log_tail:
        print("\n--- y-cruncher log tail ---")
        print(r.log_tail[-2000:])
    return 2


def cmd_sdk_test_ratio(args):
    """Bump one per-active-count P-core ratio by `delta` bins, then revert.

    Purpose: definitively answer whether the SDK can push turbo ratio
    writes through despite OC Lock being engaged in BIOS. Voltage writes
    go through a different (Plundervolt-style) path that OC Lock doesn't
    gate; ratio writes might or might not be gated.

    Output tells you:
      * "wrote X -> Y -> reverted X" -- ratio writes work via SDK; the
        auto-overclocker can use them.
      * "wrote rejected" -- OC Lock blocks ratio writes via SDK too. The
        auto-overclocker has to stay in voltage / PL / AVX territory on
        this BIOS, or the user must disable OC Lock first.
    """
    from . import sdk_ids
    n_active = int(args.count)
    delta = int(args.delta)
    if n_active not in sdk_ids.PCORE_RATIO_BY_COUNT:
        print(f"ERROR: count must be 1..8 (got {n_active})", file=sys.stderr)
        return 3
    if abs(delta) > 3:
        print(f"ERROR: refusing delta of {delta:+d} (cap is +/-3 here)",
              file=sys.stderr)
        return 3

    cid = sdk_ids.PCORE_RATIO_BY_COUNT[n_active]
    sdk = _open_sdk(args)

    pre = sdk.get_control(cid)
    pre_ratio = int(round(pre.active))
    target = pre_ratio + delta
    print(f"Control id={cid} ({pre.name})")
    print(f"  current: {pre_ratio}x  read_only={pre.read_only}")
    print(f"  target:  {target}x  (delta {delta:+d})")

    if pre.read_only:
        print("Control is read-only -- skipping write attempt.",
              file=sys.stderr)
        return 4

    print("\nStaging Tune()...")
    tuned = sdk.tune(cid, target, requires_reboot=False)
    if not tuned:
        print("Tune() returned non-Success. Discarding.")
        sdk.discard()
        return 4

    print("ApplyChanges()...")
    applied = sdk.apply(force_restart=False)
    if not applied:
        print("ApplyChanges() reported failure. Discarding.")
        sdk.discard()
        return 4

    after = sdk.get_control(cid)
    after_ratio = int(round(after.active))
    print(f"After write: {after_ratio}x  (proposed={int(round(after.proposed))}x)")

    write_took = (after_ratio == target)
    if write_took:
        print(f"\n*** RATIO WRITE VERIFIED via SDK: {pre_ratio}x -> {after_ratio}x")
    else:
        print(f"\n!!! RATIO WRITE DID NOT TAKE: asked {target}x, observe {after_ratio}x")
        print("    OC Lock or another platform constraint is gating ratio "
              "writes on this BIOS.")

    print("\nReverting...")
    sdk.tune(cid, pre_ratio, requires_reboot=False)
    sdk.apply(force_restart=False)
    final = sdk.get_control(cid)
    print(f"Final: {int(round(final.active))}x")
    return 0 if write_took else 4


def cmd_sdk_revert_voltage(args):
    """Restore the saved CVO from the previous sdk-set-voltage."""
    from .sdk_backend import XtuSdkBackend
    from . import sdk_ids
    rollback = args.state_dir / "sdk_vcore_rollback.txt"
    if rollback.exists():
        target = int(rollback.read_text().strip())
        print(f"Reverting to {target:+d} mV (from {rollback})")
    else:
        target = 0
        print(f"No rollback file at {rollback}; reverting to 0 mV.")
    sdk = _open_sdk(args)
    backend = XtuSdkBackend(sdk=sdk)
    backend.assert_unlocked()
    if not sdk.tune(sdk_ids.CORE_VOLTAGE_OFFSET, target):
        print("ERROR: Tune() rejected.", file=sys.stderr); return 3
    if not sdk.apply():
        print("ERROR: ApplyChanges() failed.", file=sys.stderr); return 3
    after = sdk.get_control(sdk_ids.CORE_VOLTAGE_OFFSET)
    print(f"OK -- offset is now {int(round(after.active)):+d} mV")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="autotune")
    ap.add_argument("--state-dir", type=Path, default=Path(r"C:\ProgramData\autotune"))
    ap.add_argument("--xtu-cli-path", type=Path, default=None)
    ap.add_argument("--lhm-url", default="http://localhost:8085/data.json")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd")

    p_run = sub.add_parser("run", help="Run the auto-tuner.")
    p_run.add_argument("--config", type=Path, required=True)
    p_run.add_argument("--yes", action="store_true")

    sub.add_parser("revert", help="Revert to baseline.")

    p_mb = sub.add_parser("mailbox-cmd",
                          help="Send one OC Mailbox command (cmd/domain/data).")
    p_mb.add_argument("--pawnio-module", type=Path, required=True)
    p_mb.add_argument("mb_cmd", type=lambda x: int(x, 0), help="Command code (e.g. 0x10)")
    p_mb.add_argument("--domain", type=lambda x: int(x, 0), default=0)
    p_mb.add_argument("--data", type=lambda x: int(x, 0), default=0)
    p_mb.add_argument("--retries", type=int, default=10)
    p_mb.add_argument("--i-know-this-may-write", action="store_true")

    p_scan = sub.add_parser("mailbox-scan",
                            help="Read-only scan of OC Mailbox cmd range.")
    p_scan.add_argument("--pawnio-module", type=Path, required=True)
    p_scan.add_argument("--start", type=lambda x: int(x, 0), default=0x00)
    p_scan.add_argument("--end",   type=lambda x: int(x, 0), default=0x40)
    p_scan.add_argument("--domain", type=lambda x: int(x, 0), default=0)
    p_scan.add_argument("--retries", type=int, default=5)
    p_scan.add_argument("--include-writes", action="store_true",
                        help="Also probe odd-numbered (likely write) cmds. Risky.")

    p_test = sub.add_parser("msr-test",
                            help="Read one MSR (debug).")
    p_test.add_argument("msr", help="MSR address (e.g. 0xCE or 206).")
    p_test.add_argument("--pawnio-module", type=Path, required=True)

    p_wd = sub.add_parser("watchdog", help="Boot-time watchdog.")
    wd_sub = p_wd.add_subparsers(dest="wd_cmd", required=True)
    wd_i = wd_sub.add_parser("install")
    wd_i.add_argument("--python-exe", default=None)
    wd_i.add_argument("--module-root", default=None)
    wd_sub.add_parser("uninstall")
    wd_sub.add_parser("run")

    p_probe = sub.add_parser("msr-probe",
                             help="Read-only dump of current CPU MSR state.")
    p_probe.add_argument("--pawnio-module", type=Path, default=None,
                         help="Path to a signed PawnIO module (e.g. IntelMSR.bin).")
    p_probe.add_argument("--bundled-module", action="store_true",
                         help="Use our bundled autotune.amx (requires UNRESTRICTED PawnIO).")
    p_probe.add_argument("--winring0-sys", type=Path, default=None,
                         help="Path to WinRing0x64.sys; we register and start it. [legacy]")
    p_probe.add_argument("--trust-unsigned-sys", action="store_true",
                         help="Skip SHA256 allow-list check on the .sys.")

    p_dr = sub.add_parser("doctor", help="Report status of dependencies.")
    p_dr.add_argument("--tools-dir", type=Path, default=deps.DEFAULT_TOOLS_DIR)

    p_su = sub.add_parser("setup", help="Install missing dependencies.")
    p_su.add_argument("--tools-dir", type=Path, default=deps.DEFAULT_TOOLS_DIR)
    p_su.add_argument("--skip-xtu", action="store_true",
                      help="Don't touch XTU (assume already installed).")
    p_su.add_argument("--skip-ycruncher", action="store_true")
    p_su.add_argument("--skip-lhm", action="store_true")
    p_su.add_argument("--no-start-lhm", action="store_true",
                      help="Don't auto-launch LHM after install.")
    p_su.add_argument("--write-config", type=Path, default=None,
                      help="Path to write a starter config.yaml if absent. "
                           "E.g. --write-config C:\\Tools\\autotune\\config.yaml")

    # ---- Phase 1 backend commands (PawnIO + OC Mailbox) ----

    p_show = sub.add_parser("show-profile",
                            help="Read current writable chip state via OC Mailbox.")
    p_show.add_argument("--pawnio-module", type=Path, required=True,
                        help="Path to signed IntelMSR.bin")

    p_setv = sub.add_parser("set-voltage",
                            help="Write FIVR Core voltage offset (mV, signed). "
                                 "Saves rollback automatically.")
    p_setv.add_argument("mv", type=int, help="Offset in millivolts (e.g. -50)")
    p_setv.add_argument("--pawnio-module", type=Path, required=True)
    p_setv.add_argument("--i-know-this-can-degrade", action="store_true",
                        help="Bypass safety caps (>+50 mV / <-200 mV).")

    p_revv = sub.add_parser("revert-voltage",
                            help="Restore FIVR voltage offset from saved rollback.")
    p_revv.add_argument("--pawnio-module", type=Path, required=True)

    p_uv = sub.add_parser("undervolt",
                          help="Auto-undervolt: voltage shmoo to find min stable Vcore.")
    p_uv.add_argument("--pawnio-module", type=Path, required=True)
    p_uv.add_argument("--ycruncher-path", type=Path, default=None,
                      help="Path to y-cruncher.exe (auto-detects common locations).")
    p_uv.add_argument("--preflight-minutes", type=float, default=2.0,
                      help="y-cruncher minutes at baseline before shmoo. Default 2.")
    p_uv.add_argument("--step-minutes", type=float, default=2.0,
                      help="y-cruncher minutes per voltage step. Default 2.")
    p_uv.add_argument("--step-mv", type=int, default=5,
                      help="Voltage step in mV (decreases each iteration). Default 5.")
    p_uv.add_argument("--min-mv", type=int, default=-200,
                      help="Floor on the offset; stops sweep here. Default -200.")
    p_uv.add_argument("--safety-margin-mv", type=int, default=10,
                      help="mV to add back to last-stable for the final result. Default 10.")
    p_uv.add_argument("--max-temp", type=float, default=95.0,
                      help="Hard temp cap (°C). Default 95.")
    p_uv.add_argument("--max-power", type=float, default=300.0,
                      help="Hard package-power cap (W). Default 300.")
    p_uv.add_argument("--max-vcore-v", type=float, default=1.40,
                      help="Hard Vcore cap (V). Default 1.40.")

    p_oc = sub.add_parser("overclock",
                          help="Auto-overclock: turbo ratio sweep + voltage shmoo at each step.")
    p_oc.add_argument("--backend", choices=("pawnio", "sdk"), default="sdk",
                      help="Hardware backend. 'sdk' uses Intel XTU SDK (recommended; "
                           "writes ratios despite OC Lock). 'pawnio' uses our PawnIO "
                           "module + OC Mailbox (legacy; ratio writes blocked by P-code).")
    p_oc.add_argument("--xtu-sdk-dir", default=None,
                      help="Override XTU SDK location (default: auto-detect).")
    p_oc.add_argument("--pawnio-module", type=Path, default=None,
                      help="Path to signed PawnIO module (only used with --backend pawnio).")
    p_oc.add_argument("--ycruncher-path", type=Path, default=None)
    p_oc.add_argument("--preflight-minutes", type=float, default=5.0)
    p_oc.add_argument("--step-minutes", type=float, default=5.0,
                      help="y-cruncher minutes per shmoo step. Default 5.")
    p_oc.add_argument("--max-pcore-ratio", type=int, default=56,
                      help="Don't bump 1-core ratio above this. Default 56 (12900K-class).")
    p_oc.add_argument("--max-vcore-offset-mv", type=int, default=75,
                      help="Hard cap on POSITIVE vcore offset. Default +75 mV.")
    p_oc.add_argument("--voltage-step-up-mv", type=int, default=10,
                      help="On instability, raise voltage by this. Default 10.")
    p_oc.add_argument("--voltage-step-down-mv", type=int, default=5,
                      help="On stability, optionally try lower by this. Default 5.")
    p_oc.add_argument("--shmoo-max-iterations", type=int, default=15,
                      help="Hard cap on shmoo iterations per ratio step. Default 15.")
    p_oc.add_argument("--max-temp", type=float, default=95.0)
    p_oc.add_argument("--max-power", type=float, default=300.0)
    p_oc.add_argument("--max-vcore-v", type=float, default=1.40)

    # ---- SDK-driven subcommands (Option B) ----
    p_sdk_show = sub.add_parser("sdk-show",
        help="Live state of every SDK knob the tuner cares about.")
    p_sdk_show.add_argument("--xtu-sdk-dir", default=None,
        help="Override XTU SDK location (default: probe Program Files).")

    p_sdk_snap = sub.add_parser("sdk-snapshot",
        help="Print a Profile snapshot via XtuSdkBackend.read().")
    p_sdk_snap.add_argument("--xtu-sdk-dir", default=None)

    p_sdk_setv = sub.add_parser("sdk-set-voltage",
        help="Apply a Core Voltage Offset via the XTU SDK (verified read-back).")
    p_sdk_setv.add_argument("mv", type=int,
        help="Signed millivolts. Capped at +/-200 here for safety.")
    p_sdk_setv.add_argument("--xtu-sdk-dir", default=None)

    p_sdk_revv = sub.add_parser("sdk-revert-voltage",
        help="Restore the saved offset from the previous sdk-set-voltage.")
    p_sdk_revv.add_argument("--xtu-sdk-dir", default=None)

    p_st = sub.add_parser("stress-test",
        help="Run y-cruncher for N minutes against the CURRENT chip state.")
    p_st.add_argument("--minutes", type=float, required=True,
        help="Stress duration in minutes (e.g. 60 for a 1-hour soak).")
    p_st.add_argument("--ycruncher-path", type=Path, default=None)
    p_st.add_argument("--max-temp", type=float, default=95.0)
    p_st.add_argument("--max-power", type=float, default=300.0)
    p_st.add_argument("--max-vcore-v", type=float, default=1.45)

    p_sdk_tr = sub.add_parser("sdk-test-ratio",
        help="Probe whether SDK ratio writes work on this BIOS (vs. OC Lock).")
    p_sdk_tr.add_argument("count", type=int,
        help="Active P-core count (1..8) to test. Try 8 (typically lowest "
             "ratio, safest to bump).")
    p_sdk_tr.add_argument("--delta", type=int, default=1,
        help="Ratio bump in bins. Default +1. Capped at +/-3.")
    p_sdk_tr.add_argument("--xtu-sdk-dir", default=None)

    args = ap.parse_args(argv)
    if args.cmd is None:
        ap.print_help(); return 1

    # Admin needed for: run, revert, watchdog install/uninstall/run, setup.
    # NOT needed for: doctor (read-only).
    needs_admin = (args.cmd in ("run", "revert", "watchdog", "setup",
                                 "msr-probe", "msr-test",
                                 "mailbox-cmd", "mailbox-scan",
                                 "show-profile", "set-voltage",
                                 "revert-voltage", "undervolt", "overclock",
                                 "sdk-show", "sdk-snapshot",
                                 "sdk-set-voltage", "sdk-revert-voltage",
                                 "sdk-test-ratio", "stress-test")
                   and not args.dry_run)
    if needs_admin and not _is_admin():
        print("ERROR: Run as Administrator.", file=sys.stderr); return 4

    _setup_logging(args.state_dir, args.verbose)

    if args.cmd == "run":      return cmd_run(args)
    if args.cmd == "revert":   return cmd_revert(args)
    if args.cmd == "watchdog": return cmd_watchdog(args)
    if args.cmd == "doctor":   return cmd_doctor(args)
    if args.cmd == "setup":    return cmd_setup(args)
    if args.cmd == "msr-probe": return cmd_msr_probe(args)
    if args.cmd == "msr-test":   return cmd_msr_test(args)
    if args.cmd == "mailbox-cmd": return cmd_mailbox(args)
    if args.cmd == "mailbox-scan": return cmd_mailbox_scan(args)
    if args.cmd == "show-profile":   return cmd_show_profile(args)
    if args.cmd == "set-voltage":    return cmd_set_voltage(args)
    if args.cmd == "revert-voltage": return cmd_revert_voltage(args)
    if args.cmd == "undervolt":      return cmd_undervolt(args)
    if args.cmd == "overclock":      return cmd_overclock(args)
    if args.cmd == "sdk-show":           return cmd_sdk_show(args)
    if args.cmd == "sdk-snapshot":       return cmd_sdk_snapshot(args)
    if args.cmd == "sdk-set-voltage":    return cmd_sdk_set_voltage(args)
    if args.cmd == "sdk-revert-voltage": return cmd_sdk_revert_voltage(args)
    if args.cmd == "sdk-test-ratio":     return cmd_sdk_test_ratio(args)
    if args.cmd == "stress-test":        return cmd_stress_test(args)
    return 1
