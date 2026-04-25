# Safety model and MSR allow/deny list

This document governs which MSRs the tool reads and writes, which it
refuses to touch, and why. The code enforces every rule below: a write
to a non-allow-listed MSR raises and aborts the run.

## Read allow-list (Phase 0)

All of these are safe to read. Reading has no hardware side effects.

| MSR    | Name                          | Purpose                     |
|--------|-------------------------------|-----------------------------|
| 0x0CE  | MSR_PLATFORM_INFO             | Max non-turbo ratio, min ratio |
| 0x150  | MSR_OC_MAILBOX (read-response)| Mailbox status / response   |
| 0x194  | MSR_FLEX_RATIO                | Flex max ratio              |
| 0x198  | IA32_PERF_STATUS              | Current effective frequency |
| 0x199  | IA32_PERF_CTL                 | Current target P-state      |
| 0x19C  | IA32_THERM_STATUS             | Per-core temperature        |
| 0x1A2  | MSR_TEMPERATURE_TARGET        | Tjmax                       |
| 0x1AD  | MSR_TURBO_RATIO_LIMIT         | Per-active-core turbo ratios|
| 0x1AE  | MSR_TURBO_RATIO_LIMIT_CORES   | Activation counts (13th+)   |
| 0x1B1  | IA32_PACKAGE_THERM_STATUS     | Package temp                |
| 0x606  | MSR_RAPL_POWER_UNIT           | Power/energy unit scale     |
| 0x610  | MSR_PKG_POWER_LIMIT           | PL1 / PL2                   |
| 0x611  | MSR_PKG_ENERGY_STATUS         | Cumulative package energy   |
| 0x614  | MSR_PKG_POWER_INFO            | Thermal / min / max power   |

## Write allow-list (Phase 1+)

Every entry requires explicit justification and a rollback target
captured before first write. No write path is enabled in Phase 0.

| MSR    | Name                          | Written for                 |
|--------|-------------------------------|-----------------------------|
| 0x1AD  | MSR_TURBO_RATIO_LIMIT         | Set per-count turbo ratios  |
| 0x150  | MSR_OC_MAILBOX (write-command)| V/F point offset writes     |
| 0x610  | MSR_PKG_POWER_LIMIT           | PL1 / PL2 caps              |

## Hard-deny list (NEVER WRITE)

Writing any of these can brick the chip, destabilize the OS boot
path, or interact with firmware in undefined ways. The code raises
immediately if a write is attempted.

| MSR    | Name                          | Why not                     |
|--------|-------------------------------|-----------------------------|
| 0x0C0000080 | IA32_EFER                | Feature enables; OS relies on |
| 0x01A0 | IA32_MISC_ENABLE              | Many bits interact with firmware |
| 0x017D | MSR_MCG_STATUS                | Machine-check state         |
| 0x02FF | IA32_MTRR_DEF_TYPE            | Memory typing; corrupts OS  |
| 0x0200-0x026F | IA32_MTRR_*            | Same                        |
| 0x0277 | IA32_PAT                      | Page attribute table        |
| 0x01D9 | IA32_DEBUGCTL                 | Debug / branch tracing      |
| 0x0174-0x0176 | MSR_*STAR              | System-call entry points    |

Also denied: anything in the undocumented 0x2000+ range (proprietary
microcode state), anything with "FIVR" in its known name (fully-integrated
voltage regulator; firmware-managed), and anything the CPU reports as
read-only via feature bits.

## Pre-write gates

Every write request passes through these checks in order; failing any
check aborts the write and reverts in-flight changes:

1. **Allow-list**: MSR number is in the write allow-list above.
2. **Mask**: The bits being changed are in the documented writable mask
   for that MSR on this CPU family. Undocumented bits are zeroed.
3. **Bound**: Numeric fields (ratios, voltages, power) are within
   known-safe ranges for the CPU model. 14900K P-core ratio cap is 63;
   Vcore offset range is -500 to +500 mV; PL2 cap is 350W.
4. **Staged**: The target value is written to `pending.json` BEFORE
   hitting the MSR, with the current-value rollback.
5. **Verify**: After writing, we re-read the MSR. If the read-back
   doesn't match the write (accounting for mask), we revert and raise.

## Rollback path

  - On any exception during Phase 1+ operations: attempt rollback.
  - Rollback walks `pending.json` in reverse order and writes each
    captured original value back to its MSR.
  - If a rollback write also fails, we log loudly but continue; a
    later BSOD will trigger the boot watchdog for fallback recovery.
  - On Ctrl-C / SIGTERM: same rollback path.
  - On BSOD: watchdog (inherited from v1) runs at next boot, reads
    `pending.json`, sees it didn't commit, writes rollback values
    before user login.

## Chip support matrix

We only enable writes on CPUs we've validated. As of Phase 0:

  - Validated for reads: (none yet; pending user hardware test)
  - Validated for writes: (none; Phase 1 will begin this list)

If the tool runs on a CPU not in the validated-for-writes list, writes
are refused regardless of the config flag. Reads are always allowed
on any Intel chip.

## Known-degrading voltage regime

Independent of any MSR-level safety: Intel 13th/14th gen have
documented voltage degradation above sustained 1.40 V Vcore. The tool's
default voltage cap is 1.40 V; raising it is possible via config but
the tool will log a persistent warning and require explicit
`--i-know-this-degrades-the-chip` on every run above 1.42 V.

## User responsibility

  - You run this on your hardware. We do our part with gates and
    rollbacks; you do yours with good caps and a way to clear CMOS.
  - Keep a recovery USB ready before you enable writes.
  - Read your motherboard manual's "clear CMOS" procedure before
    starting.
  - After each phase, paste the tool's output here so we can spot
    issues before they compound.
