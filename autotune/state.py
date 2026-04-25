"""
Persistent state for the tuner.

Three files live in the state dir:

    baseline.json      -- the profile we captured before we started; the
                          ultimate rollback target. NEVER overwritten while
                          tuning is in progress.

    last_known_good.json
                       -- the most recent profile that survived a full
                          stress run AND N minutes of post-apply uptime.

    pending.json       -- the profile we just applied and are currently
                          stress-testing. Has a `commit_by` timestamp. If
                          the machine reboots unexpectedly, the watchdog
                          on startup sees pending.json still exists and
                          uncommitted, and reverts to last_known_good.

    history.csv        -- append-only log of every attempt.
"""

from __future__ import annotations

import csv
import json
import logging
import time
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from .xtu import Profile

log = logging.getLogger(__name__)


_HISTORY_FIELDS = [
    "timestamp", "phase", "pcore_ratio", "ecore_ratio", "ring_ratio",
    "vcore_offset_mv", "pl1", "pl2", "passed", "reason",
    "peak_temp_c", "peak_power_w", "peak_vcore_v", "avg_eff_mhz",
    "whea_errors", "duration_s",
]


class State:
    def __init__(self, state_dir: Path):
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.baseline_path = self.dir / "baseline.json"
        self.lkg_path = self.dir / "last_known_good.json"
        self.pending_path = self.dir / "pending.json"
        self.history_path = self.dir / "history.csv"
        if not self.history_path.exists():
            with self.history_path.open("w", newline="") as f:
                csv.DictWriter(f, fieldnames=_HISTORY_FIELDS).writeheader()

    # ---------- baseline ----------

    def save_baseline(self, p: Profile) -> None:
        """Write baseline ONCE. Refuses to overwrite an existing one."""
        if self.baseline_path.exists():
            log.info("Baseline already exists; not overwriting.")
            return
        self.baseline_path.write_text(p.to_json())
        log.info("Baseline saved to %s", self.baseline_path)

    def load_baseline(self) -> Optional[Profile]:
        if not self.baseline_path.exists():
            return None
        return Profile.from_json(self.baseline_path.read_text())

    # ---------- last-known-good ----------

    def save_lkg(self, p: Profile) -> None:
        self.lkg_path.write_text(p.to_json())

    def load_lkg(self) -> Optional[Profile]:
        if not self.lkg_path.exists():
            return None
        return Profile.from_json(self.lkg_path.read_text())

    # ---------- pending / commit ----------

    def stage_pending(self, p: Profile, commit_in_seconds: float) -> None:
        """Write `pending.json` containing the profile + a commit deadline.
        The watchdog reads this on boot."""
        payload = asdict(p)
        payload["_commit_by"] = time.time() + commit_in_seconds
        payload["_staged_at"] = time.time()
        self.pending_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
        log.info("Pending profile staged; must commit within %.0fs", commit_in_seconds)

    def commit_pending(self) -> Optional[Profile]:
        """Promote pending -> last_known_good and delete pending.
        Returns the committed profile (or None if nothing was pending)."""
        if not self.pending_path.exists():
            return None
        payload = json.loads(self.pending_path.read_text())
        payload.pop("_commit_by", None)
        payload.pop("_staged_at", None)
        payload.pop("captured_at", None)
        p = Profile(**payload)
        self.save_lkg(p)
        self.pending_path.unlink()
        log.info("Committed pending profile as last-known-good: %s", p)
        return p

    def clear_pending(self) -> None:
        if self.pending_path.exists():
            self.pending_path.unlink()

    def pending_expired(self) -> bool:
        if not self.pending_path.exists():
            return False
        payload = json.loads(self.pending_path.read_text())
        return time.time() > payload.get("_commit_by", 0)

    def load_pending(self) -> Optional[dict]:
        if not self.pending_path.exists():
            return None
        return json.loads(self.pending_path.read_text())

    # ---------- history.csv ----------

    def log_attempt(
        self,
        phase: str,
        profile: Profile,
        passed: bool,
        reason: str,
        peak_temp_c: Optional[float],
        peak_power_w: Optional[float],
        peak_vcore_v: Optional[float],
        avg_eff_mhz: Optional[float],
        whea_errors: int,
        duration_s: float,
    ) -> None:
        row = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "phase": phase,
            "pcore_ratio": profile.pcore_ratio,
            "ecore_ratio": profile.ecore_ratio,
            "ring_ratio": profile.ring_ratio,
            "vcore_offset_mv": profile.vcore_offset_mv,
            "pl1": profile.pl1_watts,
            "pl2": profile.pl2_watts,
            "passed": int(bool(passed)),
            "reason": reason,
            "peak_temp_c": peak_temp_c,
            "peak_power_w": peak_power_w,
            "peak_vcore_v": peak_vcore_v,
            "avg_eff_mhz": avg_eff_mhz,
            "whea_errors": whea_errors,
            "duration_s": round(duration_s, 1),
        }
        with self.history_path.open("a", newline="") as f:
            csv.DictWriter(f, fieldnames=_HISTORY_FIELDS).writerow(row)
