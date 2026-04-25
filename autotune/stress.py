"""
Stress-test runner.

Documented CLI for y-cruncher v0.8.7 (from Command Lines.txt):

    y-cruncher [startup options] stress [-M:N] [-D:N] [-TL:N] [algorithm ...]

We invoke:

    y-cruncher.exe skip-warnings pause:-2 priority:2 stress
                   -D:{seconds_per_test} -TL:{total_seconds}
                   BKT BBP SFTv4 SNT SVT

  skip-warnings  -- no ENTER prompt at startup
  pause:-2       -- NEVER pause, not even on error (kills the "Press any
                    key to continue" hang we kept hitting before)
  priority:2     -- High priority. (Docs say max valid is 3 / Realtime.
                    Our previous "priority:5" was invalid and silently
                    fell through to Below-Normal default.)
  stress         -- Component Stress Tester
  -D:N           -- N seconds per test (algorithm rotation interval)
  -TL:N          -- total test time limit in seconds (y-cruncher exits
                    cleanly when reached -- our deadline is a safety net)
  BKT BBP SFTv4 SNT SVT
                 -- ONLY cache-resident algorithms. Specifying any
                    algorithm in v0.8.x disables the rest, so the
                    memory-heavy FFTv4 / NTT63 / VSTv3 (each allocates
                    246-703 MiB per thread) never run. With 32 threads
                    those would total 16-22 GiB of constant memory
                    bandwidth pressure -- enough to hard-reset DDR5
                    XMP/EXPO platforms running above Intel IMC spec.

What didn't work in earlier iterations:
  * `bench stress-test -t N -l file.log`: rejected as Invalid Parameter,
    binary then hung at "Press any key" while our deadline wall-clocked
    out and falsely reported PASS. (Source of the original phantom
    "stable at -10 mV" tuning result.)
  * Driving the menu via piped stdin: Kurumi binary CRASHES (mini-dump)
    trying to read menu input from a pipe.
  * `stress` with no algorithm filter: hard-reset the platform on both
    the 12900K and 14900K boxes due to memory-heavy algorithm pressure.

We also explicitly VERIFY the run entered stress mode within ~20s; if
it didn't (CLI mismatch, missing binary, etc.) we return reason=
"ycruncher_setup_error" so the tuner aborts cleanly instead of silently
passing on stale wall clock.
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .monitor import Monitor, RunStats

log = logging.getLogger(__name__)


@dataclass
class Caps:
    max_temp_c: float
    max_power_w: float
    max_vcore_v: float


@dataclass
class StressResult:
    passed: bool
    reason: str                  # ok, ycruncher_error, ycruncher_setup_error,
                                 # temp_cap, power_cap, vcore_cap, crashed,
                                 # killed_by_user
    duration_s: float
    stats: RunStats
    log_tail: str = ""


# y-cruncher hardware-error patterns (algorithm-level mismatches).
_YC_ERROR_PATTERNS = [
    re.compile(r"computation error", re.I),
    re.compile(r"hardware.*error", re.I),
    re.compile(r"incorrect result", re.I),
    re.compile(r"verification failed", re.I),
    re.compile(r"mismatch", re.I),
]

# Markers that indicate the stress test actually entered the running
# state. Any one of these in stdout means y-cruncher accepted the menu
# walk and is now under load.
_RUNNING_MARKERS = [
    re.compile(r"Stress.{0,10}Test.{0,40}(starting|running|started)", re.I),
    re.compile(r"Press 'q'", re.I),
    re.compile(r"Pass Status", re.I),
    # Mid-run output prefixes algorithm tags differently than the menu listing
    re.compile(r"\b(BKT|BBP|SFTv4|SNT|SVT|FFTv4|N63|VT3)\b.*(running|pass|test)", re.I),
    re.compile(r"Running:\s*[A-Za-z]", re.I),
]

# Markers that indicate the menu walk failed.
_SETUP_FAIL_MARKERS = [
    re.compile(r"Invalid Parameter", re.I),
    re.compile(r"Press any key to continue", re.I),
]


def run_ycruncher(
    ycruncher_path: Path,
    workdir: Path,
    minutes: float,
    caps: Caps,
    monitor: Monitor,
    poll_interval_s: float = 2.0,
    seconds_per_test: int = 10,
    setup_timeout_s: float = 20.0,
) -> StressResult:
    """Run y-cruncher Component Stress Tester for `minutes` total."""
    workdir.mkdir(parents=True, exist_ok=True)
    log_path = workdir / "ycruncher.log"
    if log_path.exists():
        log_path.unlink()

    # Documented CLI per y-cruncher v0.8.7 Command Lines.txt.
    total_seconds = max(int(seconds_per_test) + 1, int(round(minutes * 60)))
    cmd = [
        str(ycruncher_path),
        # ---- startup options ----
        "skip-warnings",
        "pause:-2",
        "priority:2",
        # ---- mode ----
        "stress",
        # ---- per-test rotation duration ----
        f"-D:{int(seconds_per_test)}",
        # ---- total time limit (y-cruncher exits cleanly when hit) ----
        f"-TL:{total_seconds}",
        # ---- algorithm filter: only cache-resident tests, NO memory-heavy
        # ---- algorithms (FFTv4 / NTT63 / VSTv3) which thrash DDR5 IMC.
        "BKT", "BBP", "SFTv4", "SNT", "SVT",
    ]
    log.info("y-cruncher: %s", " ".join(cmd))

    started = time.time()
    stats = RunStats()
    log_file = log_path.open("w", encoding="utf-8", errors="replace")

    # No stdin. y-cruncher in `priority:5 stress` mode doesn't read stdin
    # under normal operation, only at end-of-run "Press any key" which we
    # never reach because our deadline terminates it first.
    proc = subprocess.Popen(
        cmd,
        cwd=str(workdir),
        stdin=subprocess.DEVNULL,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    deadline = started + minutes * 60
    setup_check_deadline = started + setup_timeout_s
    reason: Optional[str] = None
    setup_verified = False

    def _scan_log() -> tuple[bool, bool]:
        """Read live stdout log; return (saw_running_marker, saw_setup_fail)."""
        try:
            log_file.flush()
        except Exception:
            pass
        if not log_path.exists():
            return False, False
        try:
            with log_path.open("r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except Exception:
            return False, False
        running = any(p.search(content) for p in _RUNNING_MARKERS)
        failed = any(p.search(content) for p in _SETUP_FAIL_MARKERS)
        return running, failed

    try:
        while True:
            rc = proc.poll()
            if rc is not None:
                break
            now = time.time()
            if now > deadline:
                log.info("Stress time budget reached; terminating y-cruncher.")
                reason = "ok"
                break

            # Setup verification: confirm we actually entered stress mode.
            if not setup_verified:
                running, failed = _scan_log()
                if failed:
                    log.error("y-cruncher menu walk failed (Invalid Parameter "
                              "or Press-any-key in stdout). The CLI build may "
                              "differ from the one this code was written for. "
                              "Tail of log:\n%s", _tail(log_path, 30))
                    reason = "ycruncher_setup_error"
                    break
                if running:
                    setup_verified = True
                    log.info("y-cruncher stress test confirmed running.")
                elif now > setup_check_deadline:
                    log.error("y-cruncher did not enter stress test within "
                              "%.0fs. Tail of log:\n%s",
                              setup_timeout_s, _tail(log_path, 30))
                    reason = "ycruncher_setup_error"
                    break

            # Sensor caps
            s = monitor.sample()
            stats.add(s)
            if s.max_core_temp_c is not None and s.max_core_temp_c >= caps.max_temp_c:
                log.warning("TEMP CAP: %.1f C >= %.1f C",
                            s.max_core_temp_c, caps.max_temp_c)
                reason = "temp_cap"
                break
            if s.pkg_power_w is not None and s.pkg_power_w >= caps.max_power_w:
                log.warning("POWER CAP: %.1f W >= %.1f W",
                            s.pkg_power_w, caps.max_power_w)
                reason = "power_cap"
                break
            if s.vcore_v is not None and s.vcore_v >= caps.max_vcore_v:
                log.warning("VCORE CAP: %.3f V >= %.3f V",
                            s.vcore_v, caps.max_vcore_v)
                reason = "vcore_cap"
                break
            time.sleep(poll_interval_s)
    except KeyboardInterrupt:
        reason = "killed_by_user"
    finally:
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        try:
            log_file.flush()
            log_file.close()
        except Exception:
            pass

    duration = time.time() - started
    log_tail = _tail(log_path, 500) if log_path.exists() else ""

    # Hard fails first.
    if reason == "ycruncher_setup_error":
        return StressResult(False, "ycruncher_setup_error",
                            duration, stats, log_tail)
    if reason in ("temp_cap", "power_cap", "vcore_cap", "killed_by_user"):
        return StressResult(False, reason, duration, stats, log_tail)

    # Defense-in-depth: if we somehow exited the loop without verifying
    # the stress test was running, treat it as a setup error rather than
    # silently reporting PASS on stale wall clock.
    if not setup_verified:
        return StressResult(False, "ycruncher_setup_error",
                            duration, stats, log_tail)

    # Time-budget hit cleanly: scan log for hardware errors.
    if reason == "ok":
        for pat in _YC_ERROR_PATTERNS:
            if pat.search(log_tail):
                return StressResult(False, "ycruncher_error",
                                    duration, stats, log_tail)
        return StressResult(True, "ok", duration, stats, log_tail)

    # Process exited on its own (rare in menu mode; usually a crash or a
    # hardware error tripped y-cruncher's internal checks).
    rc = proc.returncode
    if rc is None or rc < 0:
        return StressResult(False, "crashed", duration, stats, log_tail)
    for pat in _YC_ERROR_PATTERNS:
        if pat.search(log_tail):
            return StressResult(False, "ycruncher_error",
                                duration, stats, log_tail)
    if rc != 0:
        return StressResult(False, "ycruncher_error",
                            duration, stats, log_tail)
    return StressResult(True, "ok", duration, stats, log_tail)


def _tail(path: Path, n_lines: int) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return "".join(lines[-n_lines:])
    except Exception:
        return ""
