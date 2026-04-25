"""
Typed OC Mailbox commands for Intel desktop K-SKU CPUs.

Built atop pawnio.PawnIOClient.oc_mailbox_via_msr() which uses the
signed IntelMSR PawnIO module's MSR 0x150 read/write allow-listed access.

Commands verified on i9-12900K (April 2026 RE session, see OC_MAILBOX_RE.md):
  cmd 0x02 read   - turbo ratio limits (active-count group), domain 0/1
  cmd 0x03 write  - same encoding (Intel convention; not yet hardware-verified)
  cmd 0x06 read   - protocol version (returns 2)
  cmd 0x10 read   - FIVR voltage offset by plane (verified end-to-end via XTU)
  cmd 0x11 write  - FIVR voltage offset (Plundervolt-paper-documented encoding)
  cmd 0x16 read   - ICCMax (domain=0, 0.25 A units), TDP (domain=1, watts)
  cmd 0x22 read   - BCLK in 0.01 MHz units

Voltage encoding (universal across generations):
  encoded = (round(mV * 1.024) & 0xFFF) << 21
  decoded = signed-extend ((raw >> 21) & 0x7FF), then / 1.024
"""

from __future__ import annotations

import logging
from typing import Optional

from .pawnio import PawnIOClient, PawnIOError

log = logging.getLogger(__name__)


# --- voltage offset encoding ---

def encode_voltage_mv(mv: int) -> int:
    """Convert signed millivolts to the 32-bit DATA payload Intel expects.

    Range: ±999 mV (the 11-bit signed encoding caps near ±1000 mV).
    Encoding: scale by 1.024, round, mask to 11 bits two's-complement,
    shift left by 21 to align with the upper bits of the DATA field.
    """
    if abs(mv) > 999:
        raise ValueError(f"Voltage offset {mv} mV out of range (±999)")
    scaled = round(mv * 1.024)
    return ((scaled & 0xFFF) << 21) & 0xFFE00000


def decode_voltage_mv(raw: int) -> int:
    """Inverse of encode_voltage_mv. raw is the 32-bit response payload."""
    val11 = (raw >> 21) & 0x7FF
    if val11 & 0x400:           # sign bit
        val11 -= 0x800
    return round(val11 / 1.024)


# --- FIVR plane indices (cmd 0x10/0x11 domain) ---
PLANE_CORE = 0          # CPU core voltage (most common)
PLANE_GPU = 1           # iGPU
PLANE_CACHE_RING = 2    # L3 / ring
PLANE_SYSTEM_AGENT = 3  # SA / IMC
PLANE_GPU_UNSLICE = 4


class OCMailbox:
    """High-level OC Mailbox interface.

    Wraps a PawnIOClient that already has a signed IntelMSR module loaded
    (or a compatible module that allow-lists MSR 0x150 read+write).

    Every WRITE method does a read-back-and-verify after the write. If the
    chip rejected the write (OC-Lock, microcode mitigation, monotonicity
    violation), the method raises PawnIOError so callers can revert.
    """

    def __init__(self, client: PawnIOClient):
        self.client = client

    # ------------------------------------------------------------------
    # Turbo ratio limits (per active core count)
    # ------------------------------------------------------------------

    def read_turbo_ratios(self) -> list[int]:
        """Return 8 ints: ratios for 1, 2, ..., 8 active P-cores."""
        lo, err, ok = self.client.oc_mailbox_via_msr(cmd=0x02, domain=0)
        if not ok or err:
            raise PawnIOError(
                f"OC mailbox read turbo ratios (low half) failed: "
                f"err=0x{err:08X} timed_out={not ok}")
        hi, err, ok = self.client.oc_mailbox_via_msr(cmd=0x02, domain=1)
        if not ok or err:
            raise PawnIOError(
                f"OC mailbox read turbo ratios (high half) failed: "
                f"err=0x{err:08X} timed_out={not ok}")
        return [(lo >> (8 * i)) & 0xFF for i in range(4)] + \
               [(hi >> (8 * i)) & 0xFF for i in range(4)]

    def write_turbo_ratios(self, ratios: list[int]) -> None:
        """Write 8 turbo ratios (one per active-core count, 1..8).

        Verifies via read-back. Raises if the chip refused the write
        (OC-Lock, BIOS lockdown, etc).
        """
        if len(ratios) != 8:
            raise ValueError(f"need 8 ratios, got {len(ratios)}")
        for r in ratios:
            if r < 8 or r > 80:
                raise ValueError(f"ratio {r} out of safe range (8..80)")

        data_lo = sum(ratios[i] << (8 * i) for i in range(4))
        data_hi = sum(ratios[i + 4] << (8 * i) for i in range(4))

        for domain, data in ((0, data_lo), (1, data_hi)):
            _, err, ok = self.client.oc_mailbox_via_msr(
                cmd=0x03, domain=domain, data=data)
            if not ok:
                raise PawnIOError(
                    f"OC mailbox write turbo ratios domain={domain} "
                    f"timed out (busy never cleared)")
            if err:
                raise PawnIOError(
                    f"OC mailbox write turbo ratios domain={domain} "
                    f"err=0x{err:08X}")

        # Read-back-and-verify
        readback = self.read_turbo_ratios()
        if readback != ratios:
            raise PawnIOError(
                f"Turbo ratio write rejected by P-code: wrote {ratios}, "
                f"read back {readback}. Likely OC-Lock engaged in BIOS, "
                f"or values violate platform constraints.")

    # ------------------------------------------------------------------
    # FIVR voltage offsets (per plane)
    # ------------------------------------------------------------------

    def read_fivr_offset_mv(self, plane: int = PLANE_CORE) -> int:
        """Read the FIVR voltage offset for the given plane, in mV."""
        if not 0 <= plane <= 4:
            raise ValueError(f"plane must be 0..4, got {plane}")
        resp, err, ok = self.client.oc_mailbox_via_msr(cmd=0x10, domain=plane)
        if not ok:
            raise PawnIOError(f"FIVR read plane={plane} timed out")
        if err:
            raise PawnIOError(
                f"FIVR read plane={plane} err=0x{err:08X}")
        return decode_voltage_mv(resp)

    def write_fivr_offset_mv(self, plane: int, mv: int) -> None:
        """Write the FIVR voltage offset for the given plane.

        Verifies via read-back. Raises on rejection.
        """
        if not 0 <= plane <= 4:
            raise ValueError(f"plane must be 0..4, got {plane}")
        encoded = encode_voltage_mv(mv)
        _, err, ok = self.client.oc_mailbox_via_msr(
            cmd=0x11, domain=plane, data=encoded)
        if not ok:
            raise PawnIOError(f"FIVR write plane={plane} timed out")
        if err:
            raise PawnIOError(f"FIVR write plane={plane} err=0x{err:08X}")
        got = self.read_fivr_offset_mv(plane)
        if got != mv:
            raise PawnIOError(
                f"FIVR write rejected: asked {mv} mV plane={plane}, "
                f"read back {got} mV. OC-Lock or Plundervolt mitigation.")

    # ------------------------------------------------------------------
    # Read-only chip info
    # ------------------------------------------------------------------

    def read_iccmax_amps(self) -> float:
        """ICC max in amperes. cmd 0x16 domain=0 returns the value in 0.25 A
        units; we convert to amps."""
        resp, err, ok = self.client.oc_mailbox_via_msr(cmd=0x16, domain=0)
        if not ok or err:
            raise PawnIOError(f"ICCMax read err=0x{err:08X}")
        return resp * 0.25

    def read_tdp_watts(self) -> int:
        """TDP-equivalent in watts. cmd 0x16 domain=1."""
        resp, err, ok = self.client.oc_mailbox_via_msr(cmd=0x16, domain=1)
        if not ok or err:
            raise PawnIOError(f"TDP read err=0x{err:08X}")
        return resp

    def read_bclk_mhz(self) -> float:
        """BCLK frequency in MHz. cmd 0x22 returns kHz, so divide by 1000.

        Verified: stock 12900K returns 100250 = 100.25 MHz BCLK.
        """
        resp, err, ok = self.client.oc_mailbox_via_msr(cmd=0x22, domain=0)
        if not ok or err:
            raise PawnIOError(f"BCLK read err=0x{err:08X}")
        return resp / 1000.0

    def read_protocol_version(self) -> int:
        """OC Mailbox protocol version. cmd 0x06."""
        resp, err, ok = self.client.oc_mailbox_via_msr(cmd=0x06, domain=0)
        if not ok or err:
            raise PawnIOError(f"version read err=0x{err:08X}")
        return resp
