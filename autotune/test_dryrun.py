"""End-to-end dry-run: simulates a chip where ratio 55 needs +20 mV,
ratio 56 needs +40 mV, ratio 57 needs +65 mV (above our 60 mV cap, so
we stop at 56). Exercises: pre-flight pass, shmoo converge, shmoo
hits voltage cap, final confirm pass, CSV log, pending/commit flow.

Run from the OC/ directory:

    python3 -m autotune.test_dryrun
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from .monitor import Monitor, RunStats, Sample
from .state import State
from .stress import Caps, StressResult
from .tuner import Tuner, TunerConfig
from .xtu import XTU, Profile


# ---- fake chip model ----
# min_stable_offset_for_ratio: voltage offset needed at each P-core ratio.
# Simulates a 14900K: stock 58, stable through 60 with rising mV,
# 61 needs +80 mV (above +75 cap), so we expect the tool to land at 60.
_MODEL: dict[int, int] = {58: 0, 59: 20, 60: 50, 61: 80, 62: 120}
# Ring model: stable up through 49, fails at 50. We expect tuner to land at 49.
_RING_MAX_STABLE: int = 49
_CURRENT = {"pcore_ratio": 58, "ecore_ratio": None, "ring_ratio": 45,
            "vcore_offset_mv": 0, "pl1_watts": 125, "pl2_watts": 253}


def fake_xtu_run(self, args, timeout=30):
    # minimally parse what apply() sends
    if args == ["-t", "-id", "all"]:
        # E-cores disabled == no "Efficient Core Ratio" line
        lines = [f"Performance Core Ratio: {_CURRENT['pcore_ratio']}"]
        if _CURRENT.get("ecore_ratio") is not None:
            lines.append(f"Efficient Core Ratio: {_CURRENT['ecore_ratio']}")
        if _CURRENT.get("ring_ratio") is not None:
            lines.append(f"Ring Ratio: {_CURRENT['ring_ratio']}")
        lines += [
            f"Core Voltage Offset: {_CURRENT['vcore_offset_mv']}",
            f"Turbo Boost Power Max: {_CURRENT['pl1_watts']}",
            f"Turbo Boost Short Power Max: {_CURRENT['pl2_watts']}",
        ]
        return "\n".join(lines) + "\n"
    # Otherwise it's a -id=N -v V style write
    flag, _, val = args[0].partition("=")
    if flag == "-id" and args[1] == "-v":
        idv = int(val)
        v = int(args[2])
        if idv == 89:
            _CURRENT["pcore_ratio"] = v
        elif idv == 125:
            _CURRENT["ecore_ratio"] = v
        elif idv == 102:
            _CURRENT["ring_ratio"] = v
        elif idv == 34:
            _CURRENT["vcore_offset_mv"] = v
        elif idv == 48:
            _CURRENT["pl1_watts"] = v
        elif idv == 49:
            _CURRENT["pl2_watts"] = v
        # Other IDs ignored.
    return ""


def fake_stress(ycruncher_path, workdir, minutes, caps, monitor, poll_interval_s=2.0):
    """Stability determined by the fake chip model."""
    r = _CURRENT["pcore_ratio"]
    v = _CURRENT["vcore_offset_mv"]
    ring = _CURRENT.get("ring_ratio") or 0
    needed = _MODEL.get(r, 999)
    stats = RunStats()
    # Simulate a couple of sensor samples (E-cores off -> lower package power)
    est_temp = 65 + (r - 58) * 4 + max(0, v) * 0.1 + max(0, ring - 45) * 1.0
    est_power = 200 + (r - 58) * 20 + max(0, v) * 0.3
    stats.add(Sample(t=time.time(), max_core_temp_c=est_temp,
                     pkg_power_w=est_power, vcore_v=1.2 + v/1000.0))
    if est_temp >= caps.max_temp_c:
        return StressResult(False, "temp_cap", 5.0, stats)
    if est_power >= caps.max_power_w:
        return StressResult(False, "power_cap", 5.0, stats)
    # Core stability
    if v < needed:
        return StressResult(False, "ycruncher_error", 5.0, stats)
    # Ring stability -- fails cleanly above the model's ring ceiling
    if ring > _RING_MAX_STABLE:
        return StressResult(False, "ycruncher_error", 5.0, stats)
    return StressResult(True, "ok", 5.0, stats)


def main():
    tmp = Path(tempfile.mkdtemp(prefix="autotune_dryrun_"))
    print(f"Using state dir: {tmp}")

    cfg = TunerConfig(
        max_vcore_offset_mv=75,
        min_vcore_offset_mv=-50,
        max_temp_c=95.0,
        max_power_w=300.0,
        max_vcore_v=1.40,
        max_pcore_ratio=60,
        max_ecore_ratio=44,
        skip_ecore_sweep=True,      # simulates user's E-cores-off setup
        tune_ring_ratio=True,
        max_ring_ratio=50,
        preflight_minutes=0.01,
        per_step_minutes=0.01,
        final_confirm_minutes=0.01,
        ring_per_step_minutes=0.01,
        voltage_step_down_mv=5,
        voltage_step_up_mv=10,
        voltage_shmoo_max_iterations=15,
        commit_grace_seconds=60,
        ycruncher_path=Path("/does/not/exist"),
    )

    xtu = XTU(cli_path=Path("/does/not/exist"), dry_run=True)
    # Override _run to use fake in-memory chip, since dry_run=True
    # short-circuits writes. We want to simulate reads too, so patch:
    with patch.object(XTU, "_run", fake_xtu_run), \
         patch("autotune.tuner.run_ycruncher", fake_stress), \
         patch("autotune.tuner.whea_errors_since", lambda ts: 0):
        state = State(tmp)
        monitor = Monitor()
        tuner = Tuner(xtu=xtu, monitor=monitor, state=state, cfg=cfg,
                      workdir=tmp / "work")
        try:
            final = tuner.run()
        except Exception as e:
            print(f"\n!! tuner raised: {e}")
            raise

    print("\nFinal profile:")
    print(final.to_json())
    print("\nHistory (last 20 lines):")
    lines = (tmp / "history.csv").read_text().splitlines()
    print("\n".join(lines[-20:]))

    # Assertions about expected behavior:
    assert final.ecore_ratio is None, \
        f"expected ecore_ratio=None (skipped), got {final.ecore_ratio}"
    assert final.pcore_ratio == 60, f"expected 60, got {final.pcore_ratio}"
    assert 45 <= final.vcore_offset_mv <= 60, \
        f"expected ~+50 mV, got {final.vcore_offset_mv}"
    assert final.ring_ratio == 49, f"expected ring 49, got {final.ring_ratio}"
    lkg_path = tmp / "last_known_good.json"
    assert lkg_path.exists()
    assert not (tmp / "pending.json").exists()

    print("\n*** All dry-run assertions passed ***")
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
