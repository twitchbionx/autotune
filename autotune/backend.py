"""
Backend abstraction for the auto-tuner.

The tuner's algorithm (voltage shmoo, ratio sweep, stress harness, watchdog)
is backend-agnostic. What changes between backends is HOW profiles get
applied to the chip:

  - Phase 0 had XtuBackend (broken: XTU 7.14+ has no CLI). xtu.py kept
    only as legacy reference.
  - Phase 1 introduces PawnIOBackend, which uses the signed IntelMSR
    PawnIO module + OC Mailbox commands we reverse-engineered.

Profile is defined here (not in xtu.py) so neither backend imports the
other. PawnIOBackend depends only on pawnio.py and oc_mailbox.py.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field, fields as _fields
from typing import Optional, Protocol

from .oc_mailbox import OCMailbox, PLANE_CORE
from .pawnio import PawnIOClient, PawnIOError

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Profile -- the writable chip state we actually control
# ---------------------------------------------------------------------------

@dataclass
class Profile:
    """A snapshot of what we can read and write via the verified OC Mailbox
    surface. Only includes fields we have a confirmed cmd for.

    None on a field means 'preserve baseline' -- the apply() path won't
    write that aspect of the chip.
    """
    # Per-active-count turbo ratios (8 ints for 1..8 active P-cores).
    # Alder Lake doesn't expose true per-core ratios via OC Mailbox in the
    # commands we identified; per-active-count is the available granularity.
    turbo_ratios: Optional[list[int]] = None

    # FIVR Core-plane voltage offset in millivolts. Signed, ±999 max.
    vcore_offset_mv: Optional[int] = None

    # Package power limits (watts).
    pl1_watts: Optional[int] = None
    pl2_watts: Optional[int] = None

    captured_at: float = field(default_factory=time.time)

    # ---- json round-trip ----

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, s: str) -> "Profile":
        d = json.loads(s)
        known = {f.name for f in _fields(cls)}
        return cls(**{k: v for k, v in d.items()
                      if k in known and k != "captured_at"})


# ---------------------------------------------------------------------------
# Backend protocol
# ---------------------------------------------------------------------------

class Backend(Protocol):
    """Minimal interface the tuner needs."""
    def read(self) -> Profile: ...
    def apply(self, p: Profile) -> Profile: ...
    def revert_to(self, p: Profile) -> None: ...
    def assert_unlocked(self) -> None: ...


# ---------------------------------------------------------------------------
# PawnIOBackend
# ---------------------------------------------------------------------------

# MSRs the IntelMSR PawnIO module allow-lists for direct read+write.
_MSR_RAPL_POWER_UNIT = 0x606
_MSR_PKG_POWER_LIMIT = 0x610


class PawnIOBackend:
    """Talk to the chip via PawnIO + OC Mailbox.

    Requires a PawnIOClient with a signed IntelMSR-class module loaded.
    Caller is responsible for opening/loading; this class never owns
    the client lifecycle.
    """

    def __init__(self, client: PawnIOClient):
        self.client = client
        self.mb = OCMailbox(client)

    # ---- preflight ----

    def assert_unlocked(self) -> None:
        """Probe whether reads work. We can't reliably detect OC-Lock until
        we attempt a write, so this only catches "the mailbox isn't
        responding at all" failures. Write-side rejections surface in
        apply() with a clear PawnIOError."""
        try:
            self.mb.read_protocol_version()
            self.mb.read_turbo_ratios()
            self.mb.read_fivr_offset_mv(PLANE_CORE)
        except PawnIOError as e:
            raise PawnIOError(
                f"OC Mailbox preflight failed: {e}. "
                "Check that PawnIO + IntelMSR.bin are loaded.")

    # ---- read ----

    def read(self) -> Profile:
        ratios = self.mb.read_turbo_ratios()
        vcore = self.mb.read_fivr_offset_mv(PLANE_CORE)
        pl1, pl2 = self._read_power_limits()
        return Profile(
            turbo_ratios=ratios,
            vcore_offset_mv=vcore,
            pl1_watts=pl1,
            pl2_watts=pl2,
        )

    # ---- apply ----

    def apply(self, p: Profile) -> Profile:
        """Write the non-None fields of `p`. Each write is verified by
        read-back inside the OCMailbox helpers. Returns the post-write
        observed Profile."""
        if p.turbo_ratios is not None:
            log.info("apply: turbo ratios -> %s", p.turbo_ratios)
            self.mb.write_turbo_ratios(p.turbo_ratios)

        if p.vcore_offset_mv is not None:
            log.info("apply: vcore offset -> %+d mV", p.vcore_offset_mv)
            self.mb.write_fivr_offset_mv(PLANE_CORE, p.vcore_offset_mv)

        if p.pl1_watts is not None or p.pl2_watts is not None:
            log.info("apply: PL1=%s PL2=%s", p.pl1_watts, p.pl2_watts)
            self._write_power_limits(p.pl1_watts, p.pl2_watts)

        return self.read()

    # ---- revert ----

    def revert_to(self, p: Profile) -> None:
        """Best-effort revert. Tries each field independently and logs
        failures rather than raising -- we want to apply as much of the
        rollback as we can even if something fails."""
        if p.turbo_ratios is not None:
            try:
                self.mb.write_turbo_ratios(p.turbo_ratios)
            except Exception as e:
                log.error("revert: turbo ratios FAILED: %s", e)
        if p.vcore_offset_mv is not None:
            try:
                self.mb.write_fivr_offset_mv(PLANE_CORE, p.vcore_offset_mv)
            except Exception as e:
                log.error("revert: vcore offset FAILED: %s", e)
        if p.pl1_watts is not None or p.pl2_watts is not None:
            try:
                self._write_power_limits(p.pl1_watts, p.pl2_watts)
            except Exception as e:
                log.error("revert: power limits FAILED: %s", e)

    # ---- power-limit helpers ----

    def _power_unit_w(self) -> float:
        raw = self.client.read_msr(_MSR_RAPL_POWER_UNIT)
        return 1.0 / (1 << (raw & 0xF))

    def _read_power_limits(self) -> tuple[int, int]:
        raw = self.client.read_msr(_MSR_PKG_POWER_LIMIT)
        unit = self._power_unit_w()
        pl1 = int(round((raw & 0x7FFF) * unit))
        pl2 = int(round(((raw >> 32) & 0x7FFF) * unit))
        return pl1, pl2

    def _write_power_limits(self, pl1: Optional[int], pl2: Optional[int]) -> None:
        """Modify only the PL1/PL2 numeric fields, preserving everything
        else in MSR 0x610 (enable bits, time windows, clamping flags)."""
        current = self.client.read_msr(_MSR_PKG_POWER_LIMIT)
        unit = self._power_unit_w()
        new_val = current
        if pl1 is not None:
            pl1_raw = int(round(pl1 / unit)) & 0x7FFF
            new_val = (new_val & ~0x7FFF) | pl1_raw
        if pl2 is not None:
            pl2_raw = int(round(pl2 / unit)) & 0x7FFF
            new_val = (new_val & ~(0x7FFF << 32)) | (pl2_raw << 32)
        # Verify-via-readback after writing
        self.client.execute("ioctl_write_msr",
                            [_MSR_PKG_POWER_LIMIT, new_val], out_count=0)
        readback = self.client.read_msr(_MSR_PKG_POWER_LIMIT)
        if readback != new_val:
            log.warning("PL write readback mismatch: wrote 0x%X, read 0x%X "
                        "(may indicate a locked field; non-fatal)",
                        new_val, readback)
