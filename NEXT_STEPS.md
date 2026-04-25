# Phase β: Unrestricted PawnIO + custom module

We compiled a custom Pawn module (`autotune.amx`) that exposes the FULL
read/write surface we need for Intel OC. It's bundled inside the .exe.

The catch: the default PawnIO driver you have installed is the SIGNED
variant, which rejects unsigned modules. To load our module, you need
the UNRESTRICTED variant instead.

## Risk acknowledgment

The unrestricted PawnIO driver:

  * Is built from the same source as the signed one, with the
    `PAWNIO_UNRESTRICTED` compile flag set.
  * Is NOT in Microsoft's vulnerable-driver blocklist (yet), because
    it's just a build flag of a properly-signed driver project.
  * Still runs in kernel mode and is just as capable of doing damage
    as any other kernel driver.
  * Is intended for development / OEM use, and namazso ships it as an
    alternative download specifically so people who need it don't have
    to patch-sign their own builds.

If you're on your stream PC, this is a reasonable tradeoff.
For the gaming PC, be more deliberate -- only install once the tool
has proven stable on the stream PC, and uninstall it when not actively
tuning.

## Installing the unrestricted driver

### Option 1 -- official unrestricted release (if namazso publishes one)

Check https://github.com/namazso/PawnIO/releases for assets labelled
"unrestricted" or "-U". If present, download the INF + .sys pair and
install:

    pnputil /add-driver PawnIO_unrestricted.inf /install

### Option 2 -- use the restricted driver but load our module anyway

The restricted driver will REJECT `autotune.amx` because it has no
signature. If you only need the reads (not writes), use IntelMSR.bin
as we already did -- that module is signed.

### Option 3 -- build the unrestricted driver from source

    git clone https://github.com/namazso/PawnIO
    cd PawnIO
    # Edit CMakeLists.txt, set PAWNIO_UNRESTRICTED=ON
    cmake -B build -DPAWNIO_UNRESTRICTED=ON
    cmake --build build --config Release
    # Install .inf + .sys via pnputil as above

This requires the Windows Driver Kit (WDK) and Visual Studio -- a
real build environment.

## Using the bundled module

Once the unrestricted driver is loaded and the `PawnIO` service is
running, invoke with `--bundled-module` instead of `--pawnio-module PATH`:

    .\autotune.exe msr-probe --bundled-module

That loads the embedded autotune.amx from inside the exe.

## What the custom module adds

Vs. IntelMSR.bin's allow-list, our module allows:

  * WRITE MSR_TURBO_RATIO_LIMIT (0x1AD)       -- per-active-count ratios
  * WRITE MSR_TURBO_RATIO_LIMIT_CORES (0x1AE) -- activation counts
  * WRITE MSR_PKG_POWER_LIMIT (0x610)         -- PL1/PL2
  * WRITE MSR_OC_MAILBOX (0x150)              -- voltage offsets
  * ioctl_oc_mailbox -- kernel-side wrapper that does the full
    write-poll-read handshake in one call
  * ioctl_set_affinity / ioctl_restore_affinity -- for per-core reads
  * ioctl_cpu_count

All of these plus every MSR read IntelMSR had.

## When NOT to use the unrestricted driver

  * Production / shared machines -- stick with IntelMSR for reads only
  * If your organization has device-guard policies restricting
    unsigned driver loads
  * If you've enabled Core Isolation / HVCI (uncommon on consumer)

For your stream PC, running unrestricted is fine and unlocks the full
Phase 1 work: per-core ratios + V/F-point voltage offsets via the
OC Mailbox.
