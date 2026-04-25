# Reverse-engineering Intel OC Mailbox commands

We need per-core ratio writes via OC Mailbox (MSR 0x150). Intel's
documented commands cover only flat FIVR voltage offsets (0x10/0x11).
The per-core ratio commands are real but the codes are under NDA.
This doc tracks the experiment.

## What we know (verified)

| cmd  | meaning                          | source                         |
|------|----------------------------------|--------------------------------|
| 0x10 | Read FIVR voltage offset (plane) | Plundervolt paper, undervolt.py|
| 0x11 | Write FIVR voltage offset        | same                           |
| 0x02 | Read favored-core priority       | Linux intel_turbo_max_3        |

Envelope (universal across all generations):
```
bit 63    : BUSY (set on write, cleared by P-code on completion)
bits 42:40: DOMAIN
bits 39:32: COMMAND
bits 31:0 : DATA
```

## Test machine

- **Stream PC** -- Intel i9-12900K, BSOD-tolerant. THIS is where we
  reverse-engineer. Do NOT do this on the gaming PC.

## Safety rules

1. **Read-only first.** Run `mailbox-scan` to identify which command
   numbers respond at all. The scan only sends `(cmd, domain=0, data=0)`
   and never writes payloads.
2. **Even commands are reads, odd are writes (Intel convention).**
   Our scan defaults to even-only. The CLI refuses odd-numbered
   commands without `--i-know-this-may-write`.
3. **Compare to known.** When a command returns data, see if the
   response shape matches a known structure (voltage, ratio, etc.).
4. **Verify via XTU.** If we think cmd 0xXX returns "current per-core
   ratio for core N", change that ratio in XTU's GUI and re-read.
   The response should change. That's our ground truth.
5. **Never write speculative cmds without a rollback target.** If we
   try a write to test, we MUST first read+save the current value via
   the matching read command. Then write back to revert.
6. **BSOD recovery.** If any test BSODs the machine, just power-cycle.
   No state survives a hard reset because we never persist anything.
   Settings are MSR-state-only and reset on power-loss.

## Workflow (this session)

### Step 1: Calibration -- prove our envelope works

Read the documented FIVR Core-plane voltage offset:
```
.\autotune.exe mailbox-cmd 0x10 --pawnio-module C:\Tools\IntelMSR.bin
```

Expected: a sane voltage offset (typically 0 mV on a stock chip,
or whatever you've set in BIOS / XTU). If the output looks bonkers
(e.g. -800 mV out of nowhere), our envelope is wrong and we stop
to debug.

### Step 2: Read-only scan

```
.\autotune.exe mailbox-scan --pawnio-module C:\Tools\IntelMSR.bin --start 0x00 --end 0x40
```

Capture the response table. Look for command numbers that:
  * Returned non-zero data (interesting candidates)
  * Returned distinct error_flags (different error = different code path)
  * Took longer than others (different code path on the P-code side)

### Step 3: Domain sweep on interesting candidates

For each interesting cmd, sweep domain 0..7:
```
for /L %d in (0,1,7) do .\autotune.exe mailbox-cmd 0xXX --domain %d --pawnio-module C:\Tools\IntelMSR.bin
```

Different responses across domains usually indicates a per-domain
read (which is what we want).

### Step 4: Cross-reference with XTU

For commands that look promising (per-core or per-domain reads),
correlate with XTU's GUI:
  1. Open XTU
  2. Note the current value of "Per-core ratio" or whatever
  3. Run the candidate cmd
  4. Decode the response, compare
  5. If match: we found it. If not: keep scanning.

### Step 5: Document and codify

For each command we successfully identify, add an entry to
`pawnio.py` with:
  * The cmd code
  * What it reads / writes
  * The DATA encoding (signed offset? raw ratio? bitfield?)
  * Calibration source (XTU value at time of capture)

## What we'll likely find

Educated guesses (these are SPECULATIVE):

| cmd | guess                              |
|-----|------------------------------------|
| 0x14| Read AVX2 ratio offset             |
| 0x15| Write AVX2 ratio offset            |
| 0x18| Read per-core ratio limit (P-core) |
| 0x19| Write per-core ratio limit         |
| 0x1A| Read per-V/F-point voltage offset  |
| 0x1B| Write per-V/F-point voltage offset |
| 0x1C| Read SVID voltage info             |

These are placeholders -- we verify each empirically.

## Out-of-band: monitor what XTU itself writes

If we get stuck guessing, the next step is hooking XTU and watching
what it does to MSR 0x150 when the user sets a per-core ratio.
Tools for this:
  * Microsoft's xperf / WPR with the Microsoft-Windows-Kernel-Trace
    provider (limited; doesn't always trace MSR access)
  * Hooking DeviceIoControl in XTU's process via Detours
  * Kernel debugger attached to Windows (cumbersome)

We try Steps 1-5 first. Hooking is a fallback if blind scanning
doesn't reveal the right commands.

## Discovered commands (verified via 12900K scans)

### cmd 0x02 — Read turbo ratio limit (active-count group)

DOMAIN selects which half of the per-count table to return:
- `domain=0` → ratios for 1, 2, 3, 4 active cores (packed in low 32 bits, byte-per-count)
- `domain=1` → ratios for 5, 6, 7, 8 active cores (same layout)
- `domain=2..7` → error_flags = 0x05 (invalid domain)

Response layout for domain=0 (12900K stock observed `0x32323334`):
```
bits  7:0  ratio for 1 active core   (e.g. 0x34 = 52)
bits 15:8  ratio for 2 active cores  (e.g. 0x33 = 51)
bits 23:16 ratio for 3 active cores  (e.g. 0x32 = 50)
bits 31:24 ratio for 4 active cores  (e.g. 0x32 = 50)
```

Verified equivalence: `cmd 0x02 (domain=0,1)` returns identical content to
the low/high halves of `MSR_TURBO_RATIO_LIMIT` (0x1AD).

By Intel convention (read=even, write=odd), **cmd 0x03 is almost certainly
the matching write**. Format: same DATA layout, same DOMAIN selector.
NOT YET VERIFIED -- verify on stream PC by writing back the current
ratios unchanged and checking they didn't break anything, then try a +1
on count-1 and confirm via XTU read-back.

### Error flag 0x05

Returned when DOMAIN is out of valid range for the command. Useful
signal that a command exists but we passed the wrong domain.

### Error flag 0x1F

Returned by un-implemented command codes. Half the cmds in 0x28..0x40
gave this. Not interesting.

### Pending: cmds 0x06, 0x14, 0x16, 0x18, 0x1A, 0x1C, 0x22

All returned non-zero responses on the first scan but the meaning is
not yet identified. Active investigation:
- 0x06 → 0x00000002 (could be version, core count, status)
- 0x14 → 0x00000000 (probably needs data param for AVX mode select)
- 0x16 → 0x000007D0 (2000 dec — power? ICCMax?)
- 0x18 → 0x00000000 (probably needs data or different domain)
- 0x1A → 0x00000000 (probably V/F point read, needs data = point index)
- 0x1C → 0x00003333 (bytes [51, 51] -- two ratios?)
- 0x22 → 0x0001879A (100250 dec)

## Discovered commands (continued) -- from data/domain sweeps

### cmd 0x16 -- Read hardware limits (domain-selected)

| domain | response     | decoded                          |
|--------|-------------|----------------------------------|
| 0      | `0x000007D0` (2000) | ICCMax in 0.25 A units = **500 A** |
| 1      | `0x00000078` (120)  | TDP-equivalent in watts (stock ~125 W) |
| >= 2   | errflag 0x06 | invalid domain |

Errflag 0x06 = invalid domain (different from 0x05 seen on cmd 0x02,
interesting -- two different "invalid domain" codes).

### cmd 0x22 -- Read BCLK frequency

Always returns the same value regardless of domain:
- `0x0001879A` (100250) = **BCLK in kHz**
- 100250 / 1000 = 100.25 MHz (stock BCLK for 12900K)
- Initial decoder used 0.01 MHz units (divided by 100), giving
  1002.5 MHz which was off by 10x. Fixed at Phase 1a validation
  against XTU's displayed BCLK.

### cmd 0x06 -- Read protocol version

Always `2` regardless of domain/data. Likely "OC Mailbox protocol version".

### cmd 0x18, 0x1A

Both return `response=0, errflags=0, completed=True` regardless of
domain 0..7 or data 0..15 (low byte) or data 0x01000000..0x0F000000
(high byte). Two hypotheses:

1. They are **write-only** (status reads via a different cmd)
2. They are reads of **user-configurable state** (stock machine has
   no V/F point or per-core offsets set, hence all zero)

Hypothesis 2 matches the behavior of cmd 0x10 (which we know is
"read FIVR voltage offset" and also returns 0 at stock). To test:
set an offset via XTU's GUI, re-read these commands, see if any
become non-zero.

## Summary: what we've identified enough for

Enough verified + documented-convention cmds to implement:

| Capability                        | Method                            |
|-----------------------------------|-----------------------------------|
| Read per-active-count turbo ratios| cmd 0x02 domain 0/1 (verified)    |
| Write per-active-count turbo ratios| cmd 0x03 (convention, not tested) |
| Read FIVR voltage offset          | cmd 0x10 domain (verified)        |
| Write FIVR voltage offset         | cmd 0x11 (documented)             |
| Read ICCMax / TDP                 | cmd 0x16 domain 0/1 (verified)    |
| Read BCLK                         | cmd 0x22 (verified)               |

## What we still need

- **V/F point voltage offsets**: read + write. Candidate cmds 0x18,
  0x1A likely involved. Need XTU correlation to verify.
- **AVX2 / AVX-512 ratio offsets**: candidate 0x14. Need XTU
  correlation.
- **Per-core ratio writes (vs. per-active-count)**: may not exist
  as a separate mechanism on 12900K; Alder Lake might use per-
  active-count only. Raptor Lake added per-core. Need to test on
  the 14900K later.

## Verification pattern

For each unverified cmd, the correlation workflow is:

1. Read current value: `autotune.exe mailbox-cmd 0xXX [--domain N] [--data D]`
2. Open XTU, change the corresponding setting (if it exists in GUI)
3. Click Apply in XTU
4. Re-read the cmd, see if response changed
5. If yes -> cmd identified, document the encoding
6. If no -> cmd does something else; try different parameters

## SAFETY LESSON: do not blindly scan odd-numbered commands

Discovered the hard way (BSOD'd a 12900K stream PC): some odd-numbered
OC Mailbox commands are real privileged operations -- FIVR plane state
changes, microcode interactions, undocumented unlocks. Scanning them
with `--include-writes` is unsafe even with `data=0`.

**Rule:** never run `mailbox-scan --include-writes` without a strict
allow-list of which specific cmds to test. The default scan stays
even-only for a reason.

If a write command needs to be probed, only do it AFTER:
  1. Confirming a matching read exists for the same MSR field
  2. Reading the current value first (so you can revert)
  3. Writing the SAME value back as a no-op test
  4. Only then trying a known-safe alternate value

## XTU bypasses the OC Mailbox for ratio writes

Verified empirically: setting a per-core turbo ratio in XTU's UI does
NOT update the value returned by our cmd 0x02 read. XTU writes directly
to MSR 0x1AD via its own signed driver -- not via OC Mailbox.

cmd 0x03 returns a constant `0x0001111E` regardless of input data and
does not modify the read value (cmd 0x02 still returns stock). Our
Intel-convention guess of "cmd 0x03 is the write paired with cmd 0x02
read" is WRONG for this CPU/microcode.

Implication: per-active-count turbo ratio writes via the OC Mailbox
appear to be disabled or rerouted on the 12900K stepping we tested.
The auto-overclocker (Phase 1c) cannot work through the existing
IntelMSR module's allow-list.

Three paths to enable ratio writes:
  1. PR upstream to namazso/PawnIO.Modules adding MSR 0x1AD (and
     0x1AE for Raptor Lake) to IntelMSR's `is_allowed_msr_write`.
     Then we write directly via the existing ioctl_write_msr instead
     of the OC Mailbox. ~4 hours of work to write the PR + tests +
     justification; merge timeline depends on namazso.
  2. Unrestricted PawnIO + custom module (blocked by Windows kernel
     signing -- see earlier session).
  3. Drive XTU's GUI as a backend (UI automation, fragile, brittle
     across XTU versions).

**Option 1 is the realistic path.** The auto-undervolter (Phase 1b)
ships as a working tool independent of this; only the auto-overclock
direction needs the PR.

The voltage write path (cmd 0x11 FIVR offset) DOES work fully and is
verified end-to-end (see Phase 1a/1b). That's enough for a useful
auto-undervolter even if ratio writes never become available.

