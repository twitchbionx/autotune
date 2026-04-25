# autotune v2: competitive-with-manual Intel overclocker

## Status: Phase 0 (foundation)

## Goal

Build an Intel desktop CPU auto-tuner that reaches ~80-85% of what a
skilled manual overclocker achieves on a 12th/13th/14th gen K-SKU chip,
measured against y-cruncher-stable all-core ratio at a safe voltage cap.

The v1 attempt (autotune/) was built around Intel XTU's command-line
tool and hit a dead end: modern XTU (7.14+) ships no CLI binary. This
rebuild swaps the backend to direct MSR (Model-Specific Register) access
so we don't depend on any Intel userspace tooling.

## Why not match manual?

The last 15-20% of manual's edge comes from:

  - Per-V/F-point voltage offsets (8 points per core, individually tunable)
  - Load Line Calibration / AC/DC loadline tuning
  - Memory subtiming tuning (a whole separate multi-week project)
  - Domain-specific judgment about when not to push further

Per-core ratios and V/F curve shaping are reachable; memory is out of
scope; LLC tuning needs BIOS access we don't have from userspace.

## Phased plan

### Phase 0 -- foundation (THIS SESSION)
  - Project charter (this file)
  - Read-only MSR access via the already-loaded LibreHardwareMonitor
    kernel driver (no new driver to ship/sign)
  - Hardware discovery module with named MSR decoding
  - `msr-probe` subcommand that dumps current CPU state
  - SAFETY.md documenting the MSR allow/deny list
  - Validation: user runs `msr-probe` on their 14900K and pastes the
    output so we confirm reads work end-to-end

  Deliverable: a tool that reads the hardware and proves it. No writes
  anywhere. If this works, we proceed.

### Phase 1 -- guarded writes
  - MSR write wrapper with per-write guardrails:
      * every writable MSR is in an explicit allow-list
      * every write is preceded by a read of the current value
      * every write is followed by a read-back to verify
      * a "panic revert" path restores all touched MSRs
  - Dry-run simulator that logs every write without executing
  - `set-ratio` and `set-voltage` one-shot commands for manual testing
  - Port the existing algorithm layer (tuner.py) to the MSR backend
  - Validation: user makes small, known-safe changes (e.g., +1 ratio on
    one core) and confirms behavior matches expectation

### Phase 2 -- per-core tuning
  - Identify favored cores from effective-frequency telemetry under
    light load (cores that naturally boost highest are best silicon)
  - Sweep each core independently instead of all-core
  - State representation with per-core ratio + per-core offset
  - Multi-hour stability harness that exercises all cores

### Phase 3 -- V/F curve shaping
  - Per-V/F-point voltage offsets via OC Mailbox (MSR 0x150 cmd 0x10/0x11)
  - Curve optimization: lower voltage at low frequencies, more at high
  - This is the biggest single lever toward "competitive with manual"

### Phase 4 -- multi-workload validation
  - Rotate y-cruncher + Prime95 Small FFT + Linpack + OCCT
  - Pass = pass on all
  - WHEA / minidump parser to classify failure modes (core vs ring vs IMC)
  - Route each failure type to the correct knob

### Phase 5 -- polish
  - Intelligent early stopping (marginal MHz vs voltage cost)
  - Ring + SA voltage tuning (VCCSA, VCCIO_MEM)
  - Per-P-core-cluster boost profiles (what Intel's TVB does, scripted)

## Safety model (non-negotiable)

1. Every writable MSR must be in an explicit allow-list in SAFETY.md.
   No write to any MSR not on the list, ever.
2. Before any write path executes, a user-set config flag
   `allow_msr_writes: true` must be present. Default false.
3. Before tuning begins, the tool reads and persists every MSR it
   intends to touch. These are the rollback targets.
4. A Ctrl-C / SIGTERM handler always attempts full rollback.
5. The boot-time watchdog (inherited from v1) rolls back at next boot
   if a staged profile didn't commit.
6. We test against chips I can't actually observe, so the user owns
   validation at each phase gate. Session N+1 does not begin until
   session N was validated on hardware.

## What we explicitly don't ship yet

  - Our own kernel driver. We piggyback on LibreHardwareMonitor's.
    If LHM isn't running, the tool reports the issue and exits.
  - MSR writes of any kind. Phase 1 adds those, behind a flag, only
    after Phase 0 is validated.
  - Memory tuning, BIOS writes, overclocking-by-BCLK. All out of scope.

## Open technical questions (to resolve before Phase 1)

  - Which device name does current LibreHardwareMonitor expose?
    (`\\.\WinRing0_1_2_0` vs `\\.\LibreHardwareMonitor` vs other)
  - What's the OC Mailbox (MSR 0x150) command number for writing V/F
    point offsets on Raptor Lake Refresh (14900K)?
  - Do we need per-logical-CPU affinity when reading per-core MSRs,
    or does the driver handle it?

## Explicit non-goals

  - AMD / Ryzen support (different MSRs, different tooling, different
    degradation model)
  - Laptop / non-K CPU support (locked multiplier, no OC headroom)
  - Matching enthusiast world-record overclocking (LN2, exotic cooling,
    binning across 50 chips). We target best you can get on a single
    chip with air/water cooling at safe 24/7 voltages.
