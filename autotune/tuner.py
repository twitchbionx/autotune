"""
The search loop: ratio sweep with a voltage shmoo at each step.

Rough algorithm:

    capture baseline; save as rollback target
    assert chip is unlocked
    run pre-flight stress at stock -- if that fails, bail
    for each core cluster in (pcore, ecore):
        current_ratio = baseline.ratio
        current_voltage_offset = baseline.vcore_offset (or 0)
        while current_ratio < user.max_ratio:
            candidate = current_ratio + 1
            # find min-stable voltage for this ratio
            voltage_result = voltage_shmoo(
                start_offset = current_voltage_offset,
                candidate_ratio = candidate,
            )
            if voltage_result is None:
                # couldn't stabilize even at max vcore; we're done
                break
            current_ratio, current_voltage_offset = candidate, voltage_result
    run a long final-confirmation stress
    if that passes, commit. Otherwise revert.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .monitor import Monitor, whea_errors_since
from .state import State
from .stress import Caps, StressResult, run_ycruncher
from .xtu import Profile, XTU, XTUUnsupported

log = logging.getLogger(__name__)


@dataclass
class TunerConfig:
    # --- user caps (enforced in software; we never write beyond these) ---
    max_vcore_offset_mv: int        # upper bound on the offset we'll ADD.
                                    # e.g. 150 means we won't push Vcore
                                    # offset beyond +150 mV.
    min_vcore_offset_mv: int        # lower bound on the offset we'll SUBTRACT.
                                    # e.g. -150 for undervolt floor.
    max_temp_c: float
    max_power_w: float
    max_vcore_v: float              # live monitor kill-switch

    max_pcore_ratio: int            # don't try ratios above this
    max_ecore_ratio: int

    # --- E-core / ring extras ---
    skip_ecore_sweep: bool = False  # set true if E-cores are BIOS-disabled.
                                    # Also suppresses XTU writes to ecore_ratio
                                    # (some BIOSes report a phantom ecore ratio).
    tune_ring_ratio: bool = False   # run a ring-ratio sweep after the core sweeps
    max_ring_ratio: int = 50        # ceiling for ring sweep

    # --- stress settings ---
    preflight_minutes: float = 10.0
    per_step_minutes: float = 10.0
    final_confirm_minutes: float = 60.0
    # Ring tuning: ring responds to memory-subsystem pressure more than compute,
    # but component-stress already includes memory-heavy tests. We use a shorter
    # time per step since ring fails fast (WHEA or hang, rarely a slow drift).
    ring_per_step_minutes: float = 5.0

    # --- shmoo step sizes ---
    voltage_step_down_mv: int = 5   # on pass, try lowering by this much
    voltage_step_up_mv: int = 10    # on fail, raise by this much
    voltage_shmoo_max_iterations: int = 20

    # --- commit / safe-boot ---
    commit_grace_seconds: float = 120.0

    # --- paths ---
    ycruncher_path: Path = Path(r"C:\Tools\y-cruncher\y-cruncher.exe")


class Tuner:
    def __init__(
        self,
        xtu: XTU,
        monitor: Monitor,
        state: State,
        cfg: TunerConfig,
        workdir: Path,
    ):
        self.xtu = xtu
        self.monitor = monitor
        self.state = state
        self.cfg = cfg
        self.workdir = workdir
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.caps = Caps(
            max_temp_c=cfg.max_temp_c,
            max_power_w=cfg.max_power_w,
            max_vcore_v=cfg.max_vcore_v,
        )

    # ---------- top-level ----------

    def run(self) -> Profile:
        log.info("== Auto-tuner starting ==")
        self.xtu.assert_unlocked()

        baseline = self.xtu.read()
        log.info("Baseline profile: %s", baseline)
        self.state.save_baseline(baseline)
        if self.state.load_lkg() is None:
            self.state.save_lkg(baseline)

        # ---- pre-flight ----
        log.info("Pre-flight stress at stock for %.1f min",
                 self.cfg.preflight_minutes)
        pre = self._stress(baseline, self.cfg.preflight_minutes, phase="preflight")
        if not pre.passed:
            log.error("Pre-flight at stock FAILED (%s). Aborting -- baseline "
                      "is unstable, don't tune on top of it.", pre.reason)
            self.xtu.revert_to(baseline)
            raise RuntimeError(f"preflight_failed:{pre.reason}")

        # ---- clamp power envelope to user's cap before tuning ----
        # If baseline PL2 is above max_power_w, every trial would fail the
        # cap check. Clamp down so we tune WITHIN the user's envelope.
        pl1 = baseline.pl1_watts
        pl2 = baseline.pl2_watts
        if pl1 is not None and pl1 > self.cfg.max_power_w:
            log.info("Clamping PL1 %dW -> %dW (user cap)",
                     pl1, int(self.cfg.max_power_w))
            pl1 = int(self.cfg.max_power_w)
        if pl2 is not None and pl2 > self.cfg.max_power_w:
            log.info("Clamping PL2 %dW -> %dW (user cap)",
                     pl2, int(self.cfg.max_power_w))
            pl2 = int(self.cfg.max_power_w)

        # If E-cores are BIOS-disabled, drop the ecore_ratio entirely so we
        # never try to write it even if the baseline captured a phantom value.
        starting_ecore = None if self.cfg.skip_ecore_sweep else baseline.ecore_ratio

        # ---- P-core sweep ----
        current = Profile(
            pcore_ratio=baseline.pcore_ratio,
            ecore_ratio=starting_ecore,
            ring_ratio=baseline.ring_ratio,
            vcore_offset_mv=baseline.vcore_offset_mv or 0,
            pl1_watts=pl1,
            pl2_watts=pl2,
        )
        current = self._sweep_cluster(current, cluster="pcore")
        self.state.save_lkg(current)

        # ---- E-core sweep (skippable) ----
        if self.cfg.skip_ecore_sweep:
            log.info("Skipping E-core sweep (skip_ecore_sweep=true).")
        else:
            current = self._sweep_cluster(current, cluster="ecore")
            self.state.save_lkg(current)

        # ---- Ring/uncore sweep (opt-in) ----
        if self.cfg.tune_ring_ratio:
            current = self._sweep_ring(current)
            self.state.save_lkg(current)
        else:
            log.info("Skipping ring sweep (tune_ring_ratio=false).")

        # ---- long final confirmation ----
        log.info("Final confirmation stress for %.1f min",
                 self.cfg.final_confirm_minutes)
        final = self._stress(current, self.cfg.final_confirm_minutes, phase="final")
        if not final.passed:
            log.warning("Final confirm failed (%s). Reverting to baseline.",
                        final.reason)
            self.xtu.revert_to(baseline)
            self.state.clear_pending()
            raise RuntimeError(f"final_confirm_failed:{final.reason}")

        # Commit: final survived; move pending -> LKG, delete pending.
        self.state.commit_pending()
        log.info("== Tuner done. Final profile: %s ==", current)
        return current

    # ---------- cluster sweep ----------

    def _sweep_cluster(self, current: Profile, cluster: str) -> Profile:
        assert cluster in ("pcore", "ecore")
        ratio_field = "pcore_ratio" if cluster == "pcore" else "ecore_ratio"
        ratio_cap = (self.cfg.max_pcore_ratio if cluster == "pcore"
                     else self.cfg.max_ecore_ratio)

        start_ratio = getattr(current, ratio_field)
        if start_ratio is None:
            log.info("%s ratio unavailable on this CPU; skipping cluster.",
                     cluster.upper())
            return current

        log.info("=== Sweeping %s from %d up to %d ===",
                 cluster.upper(), start_ratio, ratio_cap)

        best = current
        while getattr(best, ratio_field) < ratio_cap:
            candidate_ratio = getattr(best, ratio_field) + 1
            log.info("%s: trying ratio %d", cluster.upper(), candidate_ratio)

            found = self._voltage_shmoo(
                base=best,
                cluster_field=ratio_field,
                candidate_ratio=candidate_ratio,
            )
            if found is None:
                log.info("%s ratio %d could not be stabilized within caps; "
                         "stopping cluster sweep.",
                         cluster.upper(), candidate_ratio)
                break
            best = found
            log.info("%s: ratio %d stable at offset %+d mV",
                     cluster.upper(), candidate_ratio, best.vcore_offset_mv)

        return best

    # ---------- ring / uncore sweep ----------

    def _sweep_ring(self, current: Profile) -> Profile:
        """Find the max stable ring ratio given the already-tuned core settings.

        Unlike core ratios, ring doesn't benefit much from extra core voltage
        (it has its own VID that XTU doesn't expose), so there's no voltage
        shmoo -- just step up, stress, back off on fail. Failures are usually
        WHEA-loud (cache/LLC errors) or machine hangs -- both handled by the
        normal stress runner and revert path.
        """
        start = current.ring_ratio
        if start is None:
            log.info("Ring ratio unavailable on this CPU/BIOS; skipping ring sweep.")
            return current
        if start >= self.cfg.max_ring_ratio:
            log.info("Ring already at or above cap (%d >= %d); skipping.",
                     start, self.cfg.max_ring_ratio)
            return current

        log.info("=== Sweeping RING from %d up to %d ===",
                 start, self.cfg.max_ring_ratio)

        best = current
        while (best.ring_ratio or 0) < self.cfg.max_ring_ratio:
            candidate = (best.ring_ratio or start) + 1
            log.info("RING: trying ratio %d", candidate)

            trial = Profile(
                pcore_ratio=best.pcore_ratio,
                ecore_ratio=best.ecore_ratio,
                ring_ratio=candidate,
                vcore_offset_mv=best.vcore_offset_mv,
                pl1_watts=best.pl1_watts,
                pl2_watts=best.pl2_watts,
            )
            if not self._within_caps(trial):
                log.info("Ring trial violates caps; stopping. %s", trial)
                break

            result = self._stress(trial, self.cfg.ring_per_step_minutes,
                                  phase=f"ring_ratio{candidate}")
            if not result.passed:
                log.info("Ring ratio %d failed (%s). Stopping ring sweep at %d.",
                         candidate, result.reason, best.ring_ratio)
                break
            best = trial
            log.info("Ring ratio %d stable.", candidate)

        return best

    # ---------- voltage shmoo ----------

    def _voltage_shmoo(
        self,
        base: Profile,
        cluster_field: str,
        candidate_ratio: int,
    ) -> Optional[Profile]:
        """Find the min-stable voltage offset for `candidate_ratio`.

        Strategy: start from the currently-known-good offset. Try it. If it
        passes, try a lower offset. If it fails, try a higher offset. Stop
        when we've bracketed the min-stable point or hit a voltage cap.
        Returns the applied+stable profile, or None if unreachable.
        """
        cfg = self.cfg
        offset = base.vcore_offset_mv if base.vcore_offset_mv is not None else 0

        # Bounds check on the starting offset
        if offset > cfg.max_vcore_offset_mv:
            offset = cfg.max_vcore_offset_mv
        if offset < cfg.min_vcore_offset_mv:
            offset = cfg.min_vcore_offset_mv

        last_stable: Optional[Profile] = None
        last_failed_offset: Optional[int] = None
        tried: set[int] = set()

        for i in range(cfg.voltage_shmoo_max_iterations):
            if offset in tried:
                log.debug("Shmoo: already tried %+d mV; stopping.", offset)
                break
            tried.add(offset)

            trial = Profile(
                pcore_ratio=base.pcore_ratio,
                ecore_ratio=base.ecore_ratio,
                ring_ratio=base.ring_ratio,
                vcore_offset_mv=offset,
                pl1_watts=base.pl1_watts,
                pl2_watts=base.pl2_watts,
            )
            setattr(trial, cluster_field, candidate_ratio)

            # Enforce caps before we write anything
            if not self._within_caps(trial):
                log.info("Trial profile violates caps; not applying: %s", trial)
                return last_stable

            result = self._stress(trial, cfg.per_step_minutes,
                                  phase=f"shmoo_ratio{candidate_ratio}")
            if result.passed:
                last_stable = trial
                # Try lowering voltage
                next_offset = offset - cfg.voltage_step_down_mv
                if (last_failed_offset is not None
                        and next_offset <= last_failed_offset):
                    # We've bracketed: last_failed < new <= last_stable
                    log.info("Shmoo converged for ratio %d at offset %+d mV",
                             candidate_ratio, offset)
                    return last_stable
                if next_offset < cfg.min_vcore_offset_mv:
                    log.info("Shmoo hit undervolt floor at offset %+d mV",
                             cfg.min_vcore_offset_mv)
                    return last_stable
                offset = next_offset
            else:
                last_failed_offset = offset
                # Certain failure modes mean we should stop the shmoo outright,
                # not just raise voltage. Cap hits mean "don't push harder."
                if result.reason in ("temp_cap", "power_cap", "vcore_cap"):
                    log.info("Shmoo hit a physical cap (%s); returning last "
                             "stable profile.", result.reason)
                    return last_stable
                # Otherwise, y-cruncher error or crash -> need more voltage
                next_offset = offset + cfg.voltage_step_up_mv
                if next_offset > cfg.max_vcore_offset_mv:
                    log.info("Shmoo hit voltage cap +%d mV without stability; "
                             "giving up on ratio %d.",
                             cfg.max_vcore_offset_mv, candidate_ratio)
                    return last_stable  # may be None
                offset = next_offset

        return last_stable

    # ---------- cap enforcement ----------

    def _within_caps(self, p: Profile) -> bool:
        cfg = self.cfg
        if p.vcore_offset_mv is not None and p.vcore_offset_mv > cfg.max_vcore_offset_mv:
            return False
        if p.vcore_offset_mv is not None and p.vcore_offset_mv < cfg.min_vcore_offset_mv:
            return False
        if p.pcore_ratio is not None and p.pcore_ratio > cfg.max_pcore_ratio:
            return False
        if p.ecore_ratio is not None and p.ecore_ratio > cfg.max_ecore_ratio:
            return False
        if p.ring_ratio is not None and p.ring_ratio > cfg.max_ring_ratio:
            return False
        if p.pl2_watts is not None and p.pl2_watts > cfg.max_power_w:
            return False
        return True

    # ---------- apply + stress + record ----------

    def _stress(self, profile: Profile, minutes: float, phase: str) -> StressResult:
        """Apply `profile` (staged), stress for `minutes`, monitor, record."""
        baseline = self.state.load_baseline()
        assert baseline is not None

        # Stage and apply
        self.state.stage_pending(
            profile, commit_in_seconds=minutes * 60 + self.cfg.commit_grace_seconds
        )
        try:
            self.xtu.apply(profile)
        except XTUUnsupported as e:
            log.error("XTU refused to apply profile: %s", e)
            self.state.clear_pending()
            from .monitor import RunStats as _RS
            return StressResult(False, "xtu_unsupported", 0.0, _RS())

        ts = time.time()
        result = run_ycruncher(
            ycruncher_path=self.cfg.ycruncher_path,
            workdir=self.workdir / "stress",
            minutes=minutes,
            caps=self.caps,
            monitor=self.monitor,
        )
        whea = whea_errors_since(ts)

        # Upgrade to FAIL if WHEA reported errors even though y-cruncher
        # didn't notice. Do this BEFORE logging so reason/passed agree.
        if result.passed and whea > 0:
            log.warning("y-cruncher said pass, but %d WHEA errors logged. "
                        "Treating as FAIL.", whea)
            result = StressResult(False, "whea_errors",
                                  result.duration_s, result.stats, result.log_tail)

        # Log to CSV
        self.state.log_attempt(
            phase=phase,
            profile=profile,
            passed=result.passed,
            reason=result.reason,
            peak_temp_c=result.stats.peak_temp(),
            peak_power_w=result.stats.peak_power(),
            peak_vcore_v=result.stats.peak_vcore(),
            avg_eff_mhz=result.stats.avg_effective_mhz(),
            whea_errors=whea,
            duration_s=result.duration_s,
        )

        # Roll back on failure -- never leave a failed profile applied.
        if not result.passed:
            lkg = self.state.load_lkg() or baseline
            log.info("Reverting to LKG after fail: %s", lkg)
            try:
                self.xtu.revert_to(lkg)
            except Exception as e:
                log.error("Revert FAILED (%s). Reverting to baseline.", e)
                self.xtu.revert_to(baseline)
            self.state.clear_pending()
        else:
            self.state.commit_pending()

        return result
