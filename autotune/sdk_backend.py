"""
Backend implementation that drives Intel XTU via the .NET SDK.

This is the third backend in autotune's evolution:
  Phase 0: XtuBackend       -- broken (XTU 7.14+ has no CLI). Removed.
  Phase 1: PawnIOBackend    -- works for reads + voltage offsets, but turbo
                               ratio writes (cmd 0x03) bounce off P-code with
                               error 0x1111E. OC Lock + microcode mitigation.
  Phase 2: XtuSdkBackend    -- this file. Drives XTU's signed driver via
                               Intel.Overclocking.SDK directly. Verified to
                               write Core Voltage Offset end-to-end.

What changes vs. PawnIO:
  * No raw MSR access. Everything goes through SDK control IDs.
  * No driver of our own to install/sign. We piggyback on XTU's signed driver.
  * Per-active-count turbo ratios are individual control IDs, not a single
    register. We write them as a list, one Tune call per slot, then a single
    ApplyChanges() to commit atomically.
  * Power limits are SDK controls (PL1=48, PL2=47), not MSR 0x610 bit-fiddling.
  * Read-back is built into the SDK -- ApplyChanges returns a TuningResult
    with per-control success/failure -- but we additionally re-fetch ActiveValue
    via GetControl() to confirm the chip latched the value.
"""

from __future__ import annotations

import logging
from typing import Optional

from .backend import Profile
from .sdk_ids import (
    ACTIVE_PCORE_RATIO_IDS,
    CORE_VOLTAGE_OFFSET,
    OVERCLOCKING_LOCK,
    TURBO_BOOST_POWER_MAX,
    TURBO_BOOST_SHORT_POWER_MAX,
)
from .xtu_sdk import XtuSdk, XtuSdkError

log = logging.getLogger(__name__)


class XtuSdkBackend:
    """Drive the chip via Intel's signed XTU SDK.

    Implements the Backend Protocol from autotune.backend.
    """

    def __init__(self, sdk: Optional[XtuSdk] = None):
        self.sdk = sdk if sdk is not None else XtuSdk()

    # ---- preflight ----

    def assert_unlocked(self) -> None:
        """Verify the SDK is talking to the driver and the chip permits
        writes to the knobs we care about. Raises with a clear message if
        OC Lock is engaged on a knob we'd need to write."""
        # Anchor read: confirms the SDK->driver path works at all.
        try:
            cvo = self.sdk.get_control(CORE_VOLTAGE_OFFSET)
        except XtuSdkError as e:
            raise XtuSdkError(
                f"SDK preflight failed: cannot read Core Voltage Offset. "
                f"Ensure XTU is installed and its service+driver are running. "
                f"Underlying error: {e}") from e
        if cvo.read_only:
            raise XtuSdkError(
                "Core Voltage Offset is read-only on this system. "
                "OC Lock is engaged in BIOS -- nothing this tool does will "
                "stick. Disable 'Overclocking Lock' in BIOS first.")

        # Optional: log OC Lock state. read_only=True on the OC Lock control
        # itself is normal; the value (0/1) tells us whether the *system* is
        # locked.
        try:
            oc_lock = self.sdk.get_control(OVERCLOCKING_LOCK)
            if oc_lock.active >= 0.5:
                log.warning("Overclocking Lock = %s. Voltage writes still "
                            "work (FIVR offset path) but turbo ratio writes "
                            "may be rejected.", oc_lock.active)
        except XtuSdkError:
            pass

    # ---- read ----

    def read(self) -> Profile:
        """Snapshot the writable knobs we model in Profile."""
        cvo = self.sdk.get_control(CORE_VOLTAGE_OFFSET)
        pl1 = self.sdk.get_control(TURBO_BOOST_POWER_MAX)
        pl2 = self.sdk.get_control(TURBO_BOOST_SHORT_POWER_MAX)

        ratios = []
        for cid in ACTIVE_PCORE_RATIO_IDS:
            r = self.sdk.get_control(cid)
            ratios.append(int(r.active))

        return Profile(
            turbo_ratios=ratios,
            vcore_offset_mv=int(round(cvo.active)),
            pl1_watts=int(round(pl1.active)),
            pl2_watts=int(round(pl2.active)),
        )

    # ---- apply ----

    def apply(self, p: Profile) -> Profile:
        """Stage every non-None field in `p`, then commit with a single
        ApplyChanges() call. Re-reads to return the post-write state."""
        staged = 0

        if p.turbo_ratios is not None:
            if len(p.turbo_ratios) != len(ACTIVE_PCORE_RATIO_IDS):
                raise ValueError(
                    f"turbo_ratios must have {len(ACTIVE_PCORE_RATIO_IDS)} "
                    f"entries (got {len(p.turbo_ratios)})")
            for cid, ratio in zip(ACTIVE_PCORE_RATIO_IDS, p.turbo_ratios):
                if not self.sdk.tune(cid, int(ratio), requires_reboot=False):
                    self.sdk.discard()
                    raise XtuSdkError(
                        f"Tune ratio control {cid} -> {ratio} rejected by SDK")
                staged += 1

        if p.vcore_offset_mv is not None:
            if not self.sdk.tune(CORE_VOLTAGE_OFFSET,
                                 int(p.vcore_offset_mv),
                                 requires_reboot=False):
                self.sdk.discard()
                raise XtuSdkError(
                    f"Tune CVO -> {p.vcore_offset_mv} mV rejected by SDK")
            staged += 1

        if p.pl1_watts is not None:
            if not self.sdk.tune(TURBO_BOOST_POWER_MAX,
                                 int(p.pl1_watts),
                                 requires_reboot=False):
                self.sdk.discard()
                raise XtuSdkError(
                    f"Tune PL1 -> {p.pl1_watts} W rejected by SDK")
            staged += 1

        if p.pl2_watts is not None:
            if not self.sdk.tune(TURBO_BOOST_SHORT_POWER_MAX,
                                 int(p.pl2_watts),
                                 requires_reboot=False):
                self.sdk.discard()
                raise XtuSdkError(
                    f"Tune PL2 -> {p.pl2_watts} W rejected by SDK")
            staged += 1

        if staged == 0:
            log.debug("apply: nothing to write (Profile had no non-None fields)")
            return self.read()

        log.info("apply: %d controls staged; ApplyChanges()...", staged)
        if not self.sdk.apply(force_restart=False):
            # Try to leave the chip in a clean state.
            self.sdk.discard()
            raise XtuSdkError("ApplyChanges() reported failure -- staged "
                              "writes did not commit")

        post = self.read()
        self._verify_match(p, post)
        return post

    # ---- revert ----

    def revert_to(self, p: Profile) -> None:
        """Best-effort revert. We try each field independently, log failures,
        and finish with an ApplyChanges() if anything staged."""
        staged = 0
        if p.vcore_offset_mv is not None:
            try:
                if self.sdk.tune(CORE_VOLTAGE_OFFSET,
                                 int(p.vcore_offset_mv)):
                    staged += 1
            except Exception as e:  # noqa: BLE001
                log.error("revert CVO failed: %s", e)
        if p.turbo_ratios is not None:
            for cid, ratio in zip(ACTIVE_PCORE_RATIO_IDS, p.turbo_ratios):
                try:
                    if self.sdk.tune(cid, int(ratio)):
                        staged += 1
                except Exception as e:  # noqa: BLE001
                    log.error("revert ratio %d failed: %s", cid, e)
        if p.pl1_watts is not None:
            try:
                if self.sdk.tune(TURBO_BOOST_POWER_MAX, int(p.pl1_watts)):
                    staged += 1
            except Exception as e:  # noqa: BLE001
                log.error("revert PL1 failed: %s", e)
        if p.pl2_watts is not None:
            try:
                if self.sdk.tune(TURBO_BOOST_SHORT_POWER_MAX, int(p.pl2_watts)):
                    staged += 1
            except Exception as e:  # noqa: BLE001
                log.error("revert PL2 failed: %s", e)

        if staged == 0:
            return
        try:
            self.sdk.apply(force_restart=False)
        except Exception as e:  # noqa: BLE001
            log.error("revert ApplyChanges failed: %s", e)

    # ---- verify ----

    def _verify_match(self, wanted: Profile, observed: Profile) -> None:
        """Cross-check that what we asked for is what we observe. Logs
        warnings for mismatches; does not raise (the caller may have asked
        for a value the chip clamped)."""
        if (wanted.vcore_offset_mv is not None
                and observed.vcore_offset_mv != wanted.vcore_offset_mv):
            log.warning("CVO read-back mismatch: asked %+d mV, observed %+d mV",
                        wanted.vcore_offset_mv, observed.vcore_offset_mv)
        if wanted.turbo_ratios is not None and observed.turbo_ratios:
            for i, (w, o) in enumerate(
                    zip(wanted.turbo_ratios, observed.turbo_ratios)):
                if w != o:
                    log.warning("Ratio[%d active] mismatch: asked %d, "
                                "observed %d", i + 1, w, o)
        if (wanted.pl1_watts is not None
                and observed.pl1_watts != wanted.pl1_watts):
            log.warning("PL1 read-back mismatch: asked %d W, observed %d W",
                        wanted.pl1_watts, observed.pl1_watts)
        if (wanted.pl2_watts is not None
                and observed.pl2_watts != wanted.pl2_watts):
            log.warning("PL2 read-back mismatch: asked %d W, observed %d W",
                        wanted.pl2_watts, observed.pl2_watts)
