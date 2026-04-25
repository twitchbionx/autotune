"""
Thin wrapper around Intel XTU's CLI (XTUCLI.exe).

Only the operations we actually need for auto-tuning are exposed:
- reading the current profile (baseline capture)
- writing per-core P-core / E-core ratios
- writing a core-voltage offset
- writing power limits (PL1 / PL2)
- applying / reverting a profile

Everything is keyed through a small typed `Profile` dataclass so the
tuner never touches XTUCLI.exe directly.

NOTE on XTU: CLI flag names have drifted between XTU 7.x and newer
releases. The constants in _FLAGS below are pinned to the current 7.14
flag set; if you upgrade XTU, re-run `XTUCLI.exe -?` and reconcile.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# Default install path. Override via config.xtu_cli_path if installed elsewhere.
_DEFAULT_XTU_CLI = Path(r"C:\Program Files (x86)\Intel\Intel(R) Extreme Tuning Utility\Client\XTUCLI.exe")

# XTU flag names, pinned to 7.14. See class docstring.
#
# VERIFY THESE AGAINST YOUR XTU VERSION. Intel renumbers knobs between
# releases. To list the current IDs on your machine:
#     XTUCLI.exe -t -id all
# and match by the human-readable label. For Raptor Lake Refresh (14900K)
# on XTU 7.14, the IDs below are correct as of Jan 2026.
_FLAGS = {
    "get_all":           ["-t", "-id", "all"],
    "pcore_ratio":       "-id=89",   # Performance core ratio (all P-cores)
    "ecore_ratio":       "-id=125",  # E-core ratio
    "ring_ratio":        "-id=102",  # Ring / Uncore / Cache ratio
    "vcore_offset":      "-id=34",   # Core voltage offset in mV (signed)
    "pl1":               "-id=48",   # Package power limit 1 (sustained) W
    "pl2":               "-id=49",   # Package power limit 2 (burst) W
    "avx_offset":        "-id=112",  # AVX ratio offset (negative; e.g. -2)
    "avx2_offset":       "-id=113",
    "avx512_offset":     "-id=114",
}


class XTUError(RuntimeError):
    """XTUCLI returned a non-zero exit code or a parseable error."""


class XTUUnsupported(XTUError):
    """The CPU or BIOS doesn't permit the requested change.

    Typical causes: non-K SKU (locked multiplier), BIOS 'Overclocking Lock'
    enabled, or OEM board with locked MSRs.
    """


@dataclass
class Profile:
    """A snapshot of the knobs we actually touch.

    All values are the *requested* values. XTU may silently clip them;
    call `read()` after `apply()` to verify.
    """
    pcore_ratio: Optional[int] = None           # multiplier, e.g. 54 = 5.4 GHz
    ecore_ratio: Optional[int] = None
    ring_ratio: Optional[int] = None            # ring/uncore/cache ratio
    vcore_offset_mv: Optional[int] = None       # signed, -200 .. +200 mV typical
    pl1_watts: Optional[int] = None
    pl2_watts: Optional[int] = None
    avx2_offset: Optional[int] = None           # negative, e.g. -2
    avx512_offset: Optional[int] = None
    # Not set by the tuner, just captured for logging:
    captured_at: float = field(default_factory=time.time)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, s: str) -> "Profile":
        """Tolerant loader: ignores unknown keys (forward-compat) and
        lets missing keys fall back to the dataclass default (back-compat)."""
        from dataclasses import fields as _fields
        d = json.loads(s)
        known = {f.name for f in _fields(cls)}
        d = {k: v for k, v in d.items() if k in known and k != "captured_at"}
        return cls(**d)


class XTU:
    def __init__(self, cli_path: Optional[Path] = None, dry_run: bool = False):
        self.cli_path = Path(cli_path) if cli_path else _DEFAULT_XTU_CLI
        self.dry_run = dry_run
        if not dry_run and not self.cli_path.exists():
            raise FileNotFoundError(
                f"XTUCLI.exe not found at {self.cli_path}. "
                f"Install Intel XTU or set config.xtu_cli_path."
            )

    # ---------- low-level ----------

    def _run(self, args: list[str], timeout: int = 30) -> str:
        """Invoke XTUCLI with the given args; return stdout; raise on error."""
        cmd = [str(self.cli_path), *args]
        log.debug("XTU exec: %s", " ".join(cmd))
        if self.dry_run:
            log.info("[dry-run] %s", " ".join(cmd))
            return ""
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout, check=False
            )
        except subprocess.TimeoutExpired as e:
            raise XTUError(f"XTUCLI timed out after {timeout}s: {e}") from e

        out = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode != 0:
            # XTU prints "not supported on this platform" for locked CPUs
            if re.search(r"not supported|locked|access denied", out, re.I):
                raise XTUUnsupported(out.strip())
            raise XTUError(f"XTUCLI exited {proc.returncode}: {out.strip()}")
        return out

    # ---------- high-level ----------

    def read(self) -> Profile:
        """Capture the current live settings."""
        raw = self._run(_FLAGS["get_all"])
        # XTU's `-t` output is key=value pairs separated by whitespace. We parse
        # loosely and tolerate missing keys (platform-dependent).
        def _grep_int(label: str) -> Optional[int]:
            m = re.search(rf"{re.escape(label)}\s*[:=]\s*(-?\d+)", raw, re.I)
            return int(m.group(1)) if m else None

        return Profile(
            pcore_ratio=_grep_int("Performance Core Ratio"),
            ecore_ratio=_grep_int("Efficient Core Ratio"),
            ring_ratio=_grep_int("Ring Ratio") or _grep_int("Cache Ratio"),
            vcore_offset_mv=_grep_int("Core Voltage Offset"),
            pl1_watts=_grep_int("Turbo Boost Power Max"),
            pl2_watts=_grep_int("Turbo Boost Short Power Max"),
            avx2_offset=_grep_int("AVX2 Ratio Offset"),
            avx512_offset=_grep_int("AVX-512 Ratio Offset"),
        )

    def apply(self, profile: Profile) -> None:
        """Write non-None fields from `profile`. Order matters: we set
        voltage BEFORE raising ratios, and power limits first of all, so the
        chip never runs a higher clock than its current voltage can support."""
        # 1. Power envelope first
        if profile.pl1_watts is not None:
            self._run([f"{_FLAGS['pl1']}", "-v", str(profile.pl1_watts)])
        if profile.pl2_watts is not None:
            self._run([f"{_FLAGS['pl2']}", "-v", str(profile.pl2_watts)])
        # 2. Voltage offset
        if profile.vcore_offset_mv is not None:
            self._run([f"{_FLAGS['vcore_offset']}", "-v", str(profile.vcore_offset_mv)])
        # 3. AVX offsets (ratio reductions under AVX load)
        if profile.avx2_offset is not None:
            self._run([f"{_FLAGS['avx2_offset']}", "-v", str(profile.avx2_offset)])
        if profile.avx512_offset is not None:
            self._run([f"{_FLAGS['avx512_offset']}", "-v", str(profile.avx512_offset)])
        # 4. Ratios last. Ring before cores so that if ring write is rejected
        # we haven't already bumped cores into a regime ring can't keep up with.
        if profile.ring_ratio is not None:
            self._run([f"{_FLAGS['ring_ratio']}", "-v", str(profile.ring_ratio)])
        if profile.pcore_ratio is not None:
            self._run([f"{_FLAGS['pcore_ratio']}", "-v", str(profile.pcore_ratio)])
        if profile.ecore_ratio is not None:
            self._run([f"{_FLAGS['ecore_ratio']}", "-v", str(profile.ecore_ratio)])
        log.info("Applied profile: %s", profile)

    def revert_to(self, baseline: Profile) -> None:
        """Restore a previously captured baseline. Order inverted from apply()."""
        # Drop ratios first so we don't run high clocks on dropping voltage.
        if baseline.pcore_ratio is not None:
            self._run([f"{_FLAGS['pcore_ratio']}", "-v", str(baseline.pcore_ratio)])
        if baseline.ecore_ratio is not None:
            self._run([f"{_FLAGS['ecore_ratio']}", "-v", str(baseline.ecore_ratio)])
        if baseline.ring_ratio is not None:
            self._run([f"{_FLAGS['ring_ratio']}", "-v", str(baseline.ring_ratio)])
        if baseline.vcore_offset_mv is not None:
            self._run([f"{_FLAGS['vcore_offset']}", "-v", str(baseline.vcore_offset_mv)])
        if baseline.pl1_watts is not None:
            self._run([f"{_FLAGS['pl1']}", "-v", str(baseline.pl1_watts)])
        if baseline.pl2_watts is not None:
            self._run([f"{_FLAGS['pl2']}", "-v", str(baseline.pl2_watts)])
        log.info("Reverted to baseline: %s", baseline)

    # ---------- sanity helpers ----------

    def assert_unlocked(self) -> None:
        """Probe whether the chip allows ratio writes. Raises XTUUnsupported
        if we're on a locked SKU. Does NOT change the running settings."""
        current = self.read()
        if current.pcore_ratio is None:
            raise XTUUnsupported(
                "XTU did not report a P-core ratio. This usually means a "
                "non-K CPU or an OEM board with OC locked in BIOS."
            )
        # Write the same value back; any locked SKU will reject this.
        try:
            self._run([f"{_FLAGS['pcore_ratio']}", "-v", str(current.pcore_ratio)])
        except XTUUnsupported:
            raise
        except XTUError as e:
            # Some OEM boards accept the identity write but reject deltas. We
            # can't distinguish here; defer to the tuner's per-step error.
            log.warning("Identity-ratio write warned: %s", e)
