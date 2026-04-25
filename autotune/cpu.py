"""
Hardware discovery: wraps msr.py with named constants and decodes raw
MSR values into human-readable fields.

Everything here is read-only. The returned dataclasses are snapshots,
not live views -- call each function again for fresh values.
"""

from __future__ import annotations

import ctypes
import logging
import platform
from dataclasses import dataclass, field
from typing import Optional

from .msr import MsrClient, MsrError

log = logging.getLogger(__name__)


# MSR addresses (Intel SDM Vol 4)
MSR_PLATFORM_INFO           = 0x0CE
MSR_OC_MAILBOX              = 0x150
MSR_FLEX_RATIO              = 0x194
IA32_PERF_STATUS            = 0x198
IA32_PERF_CTL               = 0x199
IA32_THERM_STATUS           = 0x19C
MSR_TEMPERATURE_TARGET      = 0x1A2
MSR_TURBO_RATIO_LIMIT       = 0x1AD
MSR_TURBO_RATIO_LIMIT_CORES = 0x1AE
IA32_PACKAGE_THERM_STATUS   = 0x1B1
MSR_RAPL_POWER_UNIT         = 0x606
MSR_PKG_POWER_LIMIT         = 0x610
MSR_PKG_ENERGY_STATUS       = 0x611
MSR_PKG_POWER_INFO          = 0x614


# Known CPU families we care about. cpuid signature masked to family+model.
# Raptor Lake (13th gen): 0x90672 (family 6 model 0xB7)
# Raptor Lake Refresh (14th gen): same 0xB7 stepping 1
# Alder Lake (12th gen): 0x90672 / 0x906A0 (family 6 model 0x97 / 0x9A)
_KNOWN_CPUS = {
    # Family 6 + (Model << 4) + Stepping. Match your actual chip's
    # CPUID signature output from `msr-probe`.
    0x60970: "Alder Lake-S (12th gen, stepping 0)",
    0x60971: "Alder Lake-S (12th gen, stepping 1)",
    0x60972: "Alder Lake-S 12900K-class (12th gen, stepping 2)",  # Elijah's stream PC
    0x906A0: "Alder Lake-P (12th gen mobile)",
    0x60B70: "Raptor Lake (13th gen, stepping 0)",
    0x60B71: "Raptor Lake / Raptor Lake Refresh (13th/14th gen)",
    0x60BA0: "Raptor Lake (13th gen alt model)",
    0x60BF0: "Raptor Lake Refresh (14th gen)",  # some 14900K stepping
    # If your CPU shows up as "unknown", add its signature here. The
    # known-chip table only affects display; reads still work either way.
}


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class CpuIdentity:
    signature: int
    family: int
    model: int
    stepping: int
    brand: str
    name_guess: str


@dataclass
class PlatformInfo:
    max_non_turbo_ratio: int       # base ratio, e.g. 32 for a 3.2 GHz base
    max_efficiency_ratio: int      # minimum ratio (slowest idle state)
    programmable_turbo: bool        # true if turbo headroom can be extended
    programmable_tdp: bool
    raw: int


@dataclass
class TurboRatios:
    """Per-active-count turbo ratio limits. Index = (active cores - 1)."""
    ratios: list[int]               # length 8 on classic, up to 64 on newer
    raw_1ad: int
    raw_1ae: int


@dataclass
class ThermalStatus:
    tjmax: int                      # deg C
    current: int                    # deg C
    throttling: bool
    raw_therm: int
    raw_tjmax: int


@dataclass
class PowerLimits:
    pl1_watts: Optional[float]
    pl2_watts: Optional[float]
    pl1_time_window_s: Optional[float]
    pl1_enabled: bool
    pl2_enabled: bool
    power_unit_watts: float         # usually 0.125 (1/8)
    energy_unit_joules: float       # usually 6.1e-5 (1/16384)
    raw_610: int


@dataclass
class CpuSnapshot:
    identity: CpuIdentity
    platform: PlatformInfo
    turbo: TurboRatios
    thermal: ThermalStatus
    power: PowerLimits
    validated_for_writes: bool = False


# ---------------------------------------------------------------------------
# CPUID (does not need the driver)
# ---------------------------------------------------------------------------

def read_cpu_identity() -> CpuIdentity:
    """Read CPUID via a Windows-friendly path; fall back to /proc on Linux.

    Returns an approximate name via the EAX 0x80000002..4 leaves.
    """
    if platform.system() == "Windows":
        # Use GetNativeSystemInfo + processor brand from registry.
        try:
            import winreg
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
            ) as k:
                brand, _ = winreg.QueryValueEx(k, "ProcessorNameString")
        except Exception:
            brand = "unknown"
        # CPUID signature from registry
        try:
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
            ) as k:
                ident_str, _ = winreg.QueryValueEx(k, "Identifier")
                # "Intel64 Family 6 Model 183 Stepping 1"
                import re
                m = re.search(r"Family\s+(\d+)\s+Model\s+(\d+)\s+Stepping\s+(\d+)",
                              ident_str)
                family = int(m.group(1)) if m else 0
                model = int(m.group(2)) if m else 0
                stepping = int(m.group(3)) if m else 0
        except Exception:
            family = model = stepping = 0
        sig = (family << 16) | (model << 4) | stepping
    else:
        brand = "non-Windows host"
        family = model = stepping = sig = 0
    name_guess = _KNOWN_CPUS.get(sig & 0xFFFFFF, f"unknown (sig 0x{sig:X})")
    return CpuIdentity(
        signature=sig, family=family, model=model, stepping=stepping,
        brand=brand, name_guess=name_guess,
    )


# ---------------------------------------------------------------------------
# MSR decoders
# ---------------------------------------------------------------------------

def _safe_read(m, msr: int, label: str) -> int | None:
    try:
        return m.read(msr)
    except Exception as e:
        log.warning("MSR 0x%X (%s) read failed: %s", msr, label, e)
        return None


def read_platform_info(m: MsrClient) -> PlatformInfo:
    raw = _safe_read(m, MSR_PLATFORM_INFO, "MSR_PLATFORM_INFO")
    if raw is None:
        return PlatformInfo(
            max_non_turbo_ratio=0, max_efficiency_ratio=0,
            programmable_turbo=False, programmable_tdp=False, raw=0,
        )
    return PlatformInfo(
        max_non_turbo_ratio=(raw >> 8) & 0xFF,
        max_efficiency_ratio=(raw >> 40) & 0xFF,
        programmable_turbo=bool((raw >> 28) & 1),
        programmable_tdp=bool((raw >> 29) & 1),
        raw=raw,
    )


def read_turbo_ratios(m: MsrClient) -> TurboRatios:
    raw_1ad = _safe_read(m, MSR_TURBO_RATIO_LIMIT, "TURBO_RATIO_LIMIT") or 0
    raw_1ae = _safe_read(m, MSR_TURBO_RATIO_LIMIT_CORES, "TURBO_RATIO_LIMIT_CORES") or 0
    # 0x1AD packs 8 one-byte ratios for 1..8 active cores. Newer chips use
    # a different layout; we return all 8 for now and the full Raptor Lake
    # layout in a later pass.
    ratios = [(raw_1ad >> (8 * i)) & 0xFF for i in range(8)]
    return TurboRatios(ratios=ratios, raw_1ad=raw_1ad, raw_1ae=raw_1ae)


def read_thermal(m: MsrClient) -> ThermalStatus:
    raw_tjmax = _safe_read(m, MSR_TEMPERATURE_TARGET, "TEMPERATURE_TARGET") or 0
    tjmax = (raw_tjmax >> 16) & 0xFF
    raw_therm = _safe_read(m, IA32_THERM_STATUS, "IA32_THERM_STATUS") or 0
    # delta from tjmax in bits 22:16
    delta = (raw_therm >> 16) & 0x7F
    current = tjmax - delta
    throttling = bool(raw_therm & 1) or bool((raw_therm >> 2) & 1)
    return ThermalStatus(
        tjmax=tjmax, current=current, throttling=throttling,
        raw_therm=raw_therm, raw_tjmax=raw_tjmax,
    )


def read_power_limits(m: MsrClient) -> PowerLimits:
    raw_unit = _safe_read(m, MSR_RAPL_POWER_UNIT, "RAPL_POWER_UNIT") or 0
    power_unit_watts = 1.0 / (1 << (raw_unit & 0xF))
    energy_unit_joules = 1.0 / (1 << ((raw_unit >> 8) & 0x1F))

    raw_pl = _safe_read(m, MSR_PKG_POWER_LIMIT, "PKG_POWER_LIMIT") or 0
    pl1_raw = raw_pl & 0x7FFF
    pl2_raw = (raw_pl >> 32) & 0x7FFF
    pl1_enabled = bool((raw_pl >> 15) & 1)
    pl2_enabled = bool((raw_pl >> 47) & 1)
    # Time-window encoding: 2^Y * (1 + X/4) * time-unit. Simplify: report
    # approximate seconds using default time-unit of 1s (Intel default
    # for modern chips). Exact formula in SDM Vol 3B sec 14.9.
    time_y = (raw_pl >> 17) & 0x1F
    time_x = (raw_pl >> 22) & 0x3
    pl1_time_window_s = (1 << time_y) * (1.0 + time_x / 4.0)

    return PowerLimits(
        pl1_watts=pl1_raw * power_unit_watts,
        pl2_watts=pl2_raw * power_unit_watts,
        pl1_time_window_s=pl1_time_window_s,
        pl1_enabled=pl1_enabled,
        pl2_enabled=pl2_enabled,
        power_unit_watts=power_unit_watts,
        energy_unit_joules=energy_unit_joules,
        raw_610=raw_pl,
    )


# ---------------------------------------------------------------------------
# Top-level
# ---------------------------------------------------------------------------

def read_snapshot(m: MsrClient) -> CpuSnapshot:
    """One call, returns everything we can observe in Phase 0."""
    return CpuSnapshot(
        identity=read_cpu_identity(),
        platform=read_platform_info(m),
        turbo=read_turbo_ratios(m),
        thermal=read_thermal(m),
        power=read_power_limits(m),
        validated_for_writes=False,  # Phase 0: no chip is validated yet
    )


def format_snapshot(s: CpuSnapshot) -> str:
    """Human-readable dump. Used by msr-probe."""
    lines = [
        "=== CPU identity ===",
        f"  brand:      {s.identity.brand}",
        f"  family:     {s.identity.family}",
        f"  model:      {s.identity.model}",
        f"  stepping:   {s.identity.stepping}",
        f"  signature:  0x{s.identity.signature:X}",
        f"  guess:      {s.identity.name_guess}",
        "",
        "=== Platform ===",
        f"  max non-turbo ratio: {s.platform.max_non_turbo_ratio}  "
            f"({s.platform.max_non_turbo_ratio * 100} MHz base)",
        f"  min ratio:           {s.platform.max_efficiency_ratio}",
        f"  programmable turbo:  {s.platform.programmable_turbo}",
        f"  programmable TDP:    {s.platform.programmable_tdp}",
        f"  MSR 0x0CE raw:       0x{s.platform.raw:016X}",
        "",
        "=== Turbo ratio limits (per active count) ===",
    ]
    for i, r in enumerate(s.turbo.ratios):
        lines.append(f"  {i+1} active core(s): {r}  (~{r * 100} MHz)")
    lines += [
        f"  MSR 0x1AD raw: 0x{s.turbo.raw_1ad:016X}",
        f"  MSR 0x1AE raw: 0x{s.turbo.raw_1ae:016X}",
        "",
        "=== Thermal ===",
        f"  Tjmax:       {s.thermal.tjmax} C",
        f"  current:     {s.thermal.current} C",
        f"  throttling:  {s.thermal.throttling}",
        f"  MSR 0x1A2 raw: 0x{s.thermal.raw_tjmax:016X}",
        f"  MSR 0x19C raw: 0x{s.thermal.raw_therm:016X}",
        "",
        "=== Power limits ===",
        f"  PL1: {s.power.pl1_watts:.1f} W  "
            f"(enabled={s.power.pl1_enabled}, window={s.power.pl1_time_window_s:.1f}s)",
        f"  PL2: {s.power.pl2_watts:.1f} W  "
            f"(enabled={s.power.pl2_enabled})",
        f"  power unit:  {s.power.power_unit_watts} W",
        f"  energy unit: {s.power.energy_unit_joules} J",
        f"  MSR 0x610 raw: 0x{s.power.raw_610:016X}",
        "",
        f"Validated for writes: {s.validated_for_writes}",
    ]
    return "\n".join(lines)
