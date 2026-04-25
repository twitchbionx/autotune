"""
Resolved Intel XTU SDK control IDs.

Captured from `RescanAvailableControls()` on a 12900K with the April 2026
SDK (IntelOverclockingSDK.dll 7.14.2.71). These IDs match what XTU's UI
uses internally and have been verified live: GetControl(34) returns the
expected ActiveValue, IsControlTunable(34) returns True, and a write of
Tune(34, -25, false) + ApplyChanges(false) produced a -25 mV core voltage
offset on real hardware (read-back via GetControl(34).ActiveValue).

The IDs above 0xFFFFFF (e.g. PERFORMANCE_CORE_RATIO=3489660933) are
"composite" controls that the SDK fans out to multiple per-core controls
under the hood. Writing the composite is what XTU's UI does and what we
should do too; the SDK handles per-core coherence for us.

If you run on a different CPU and the resolved IDs differ, regenerate
this file with probe_tuninglib5.ps1 (output is paste-ready).
"""

from __future__ import annotations


# --- Voltage knobs (mV unless noted) ---
CORE_VOLTAGE_OFFSET            = 34          # FIVR core offset, mV (signed)
CORE_VOLTAGE                   = 2           # Vcore override, V
CORE_VOLTAGE_MODE              = 88          # 0=adaptive, 1=override (varies)
CACHE_VOLTAGE_OFFSET           = 79          # Ring/cache plane, mV
CACHE_VOLTAGE                  = 77          # Ring/cache override, V
SYSTEM_AGENT_VOLTAGE_OFFSET    = 85          # SA/IMC, mV

# --- Ratio knobs (multiplier units) ---
PERFORMANCE_CORE_RATIO         = 3489660933  # Composite: all-P-core ratio
PROCESSOR_CACHE_RATIO          = 76          # Ring/cache ratio
EFFICIENT_CORE_RATIO           = 3489660934  # Composite: all-E-core ratio
MAX_TURBO_BOOST_CPU_SPEED      = 3489660930  # Read-only effective Hz

# --- Per-active-count P-core ratios (for OC-Mailbox-style turbo curves) ---
# Index 0 = 1 active core, index 7 = 8 active cores.
ACTIVE_PCORE_RATIO_IDS = [29, 30, 31, 32, 42, 43, 96, 97]

# --- AVX ---
AVX2_RATIO_OFFSET              = 114         # Negative offset under AVX2 load
AVX2_VOLTAGE_GUARDBAND_SCALE   = 287         # Voltage scaling under AVX2

# --- Power / current limits ---
PROCESSOR_CORE_ICCMAX          = 102         # Amps
TURBO_BOOST_POWER_MAX          = 48          # PL1, watts
TURBO_BOOST_SHORT_POWER_MAX    = 47          # PL2, watts
TURBO_BOOST_SHORT_POWER_MAX_EN = 49          # PL2 enable, 0/1
TURBO_BOOST_POWER_TIME_WINDOW  = 66          # Tau, seconds

# --- BCLK / locks ---
REFERENCE_CLOCK                = 1           # MHz
OVERCLOCKING_LOCK              = 80          # 0/1, read-only on locked BIOS


# Convenience tables for the backend layer

PCORE_RATIO_BY_COUNT = {
    1: 29, 2: 30, 3: 31, 4: 32,
    5: 42, 6: 43, 7: 96, 8: 97,
}

# All control IDs the auto-overclocker writes to. Used for Profile snapshots
# and dry-run diffs.
WRITABLE_KNOBS = {
    "vcore_offset_mv":   CORE_VOLTAGE_OFFSET,
    "cache_offset_mv":   CACHE_VOLTAGE_OFFSET,
    "sa_offset_mv":      SYSTEM_AGENT_VOLTAGE_OFFSET,
    "cache_ratio":       PROCESSOR_CACHE_RATIO,
    "avx2_ratio_offset": AVX2_RATIO_OFFSET,
    "iccmax_a":          PROCESSOR_CORE_ICCMAX,
    "pl1_watts":         TURBO_BOOST_POWER_MAX,
    "pl2_watts":         TURBO_BOOST_SHORT_POWER_MAX,
    "tau_seconds":       TURBO_BOOST_POWER_TIME_WINDOW,
}
