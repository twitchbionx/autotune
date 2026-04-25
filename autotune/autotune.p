// autotune.p -- custom PawnIO module for the auto-tuner.
//
// This module exposes the FULL read/write surface we need:
//   * All MSRs we read in cpu.py
//   * Writes to:
//       MSR_TURBO_RATIO_LIMIT (0x1AD)       -- per-active-count turbo ratios
//       MSR_TURBO_RATIO_LIMIT_CORES (0x1AE) -- activation count (Raptor Lake+)
//       MSR_OC_MAILBOX (0x150)              -- voltage offsets, V/F points
//       MSR_PKG_POWER_LIMIT (0x610)         -- PL1/PL2
//
// Philosophy on allow-lists: in an "unrestricted" PawnIO build, the driver
// doesn't check signatures. WE still check what's being written so a
// runaway Python bug doesn't push garbage into arbitrary MSRs.
//
// Platform safety: main() rejects non-Intel / non-x64.
//
// Compile with:
//   pawncc autotune.p -iinclude -C64 -;+ -(+ -p

#include <pawnio.inc>

// ---- MSR addresses we allow ----

#define MSR_PLATFORM_INFO               0x000000ce
#define MSR_IA32_PERF_STATUS            0x00000198
#define MSR_IA32_PERF_CTL               0x00000199
#define MSR_IA32_THERM_STATUS           0x0000019c
#define MSR_IA32_TEMPERATURE_TARGET     0x000001a2
#define MSR_TURBO_RATIO_LIMIT           0x000001ad
#define MSR_TURBO_RATIO_LIMIT_CORES     0x000001ae
#define MSR_IA32_PACKAGE_THERM_STATUS   0x000001b1
#define MSR_RAPL_POWER_UNIT             0x00000606
#define MSR_PKG_POWER_LIMIT             0x00000610
#define MSR_PKG_ENERGY_STATUS           0x00000611
#define MSR_PKG_POWER_INFO              0x00000614
#define MSR_OC_MAILBOX                  0x00000150
#define MSR_FLEX_RATIO                  0x00000194
#define MSR_IA32_MPERF                  0x000000e7
#define MSR_IA32_APERF                  0x000000e8

// ---- allow-lists ----

bool:is_allowed_msr_read(msr) {
    switch (msr) {
        case MSR_PLATFORM_INFO, MSR_IA32_PERF_STATUS, MSR_IA32_PERF_CTL,
             MSR_IA32_THERM_STATUS, MSR_IA32_TEMPERATURE_TARGET,
             MSR_TURBO_RATIO_LIMIT, MSR_TURBO_RATIO_LIMIT_CORES,
             MSR_IA32_PACKAGE_THERM_STATUS,
             MSR_RAPL_POWER_UNIT, MSR_PKG_POWER_LIMIT,
             MSR_PKG_ENERGY_STATUS, MSR_PKG_POWER_INFO,
             MSR_OC_MAILBOX, MSR_FLEX_RATIO,
             MSR_IA32_MPERF, MSR_IA32_APERF:
            return true;
        default:
            return false;
    }
    return false;
}

bool:is_allowed_msr_write(msr) {
    switch (msr) {
        // Ratio control
        case MSR_TURBO_RATIO_LIMIT, MSR_TURBO_RATIO_LIMIT_CORES:
            return true;
        // Power limits
        case MSR_PKG_POWER_LIMIT:
            return true;
        // OC Mailbox for voltage control
        case MSR_OC_MAILBOX:
            return true;
        default:
            return false;
    }
    return false;
}

// ---- public IOCTLs ----

/// Read MSR.
/// @param in  [0] = MSR address
/// @param out [0] = Value read
DEFINE_IOCTL_SIZED(ioctl_read_msr, 1, 1) {
    new msr = in[0] & 0xFFFFFFFF;
    if (!is_allowed_msr_read(msr))
        return STATUS_ACCESS_DENIED;

    new value = 0;
    new NTSTATUS:status = msr_read(msr, value);
    out[0] = value;
    return status;
}

/// Write MSR.
/// @param in [0] = MSR address, [1] = Value
DEFINE_IOCTL_SIZED(ioctl_write_msr, 2, 0) {
    new msr = in[0] & 0xFFFFFFFF;
    if (!is_allowed_msr_write(msr))
        return STATUS_ACCESS_DENIED;
    return msr_write(msr, in[1]);
}

/// OC Mailbox transaction (write + poll + read response).
///
/// This wraps the full mailbox protocol in one kernel-side call so we don't
/// have to burn 6 IOCTLs per command from userspace.
///
/// @param in [0] = command code (bits 7..0)
/// @param in [1] = domain (bits 2..0, bit 42..40 of MSR)
/// @param in [2] = data payload (low 32 bits)
/// @param in [3] = max retries (typically 5)
/// @param out [0] = response payload (low 32 bits of 0x150 after busy clears)
/// @param out [1] = error flags (high 32 bits); 0 on success
/// @return NTSTATUS. STATUS_TIMEOUT if busy bit never clears.
DEFINE_IOCTL_SIZED(ioctl_oc_mailbox, 4, 2) {
    new cmd     = in[0] & 0xFF;
    new domain  = in[1] & 0x7;
    new data    = in[2] & 0xFFFFFFFF;
    new retries = in[3];
    if (retries <= 0 || retries > 20) retries = 5;

    // Build the command word: (busy<<63) | (domain<<40) | (cmd<<32) | data
    new word = (1 << 63) | (domain << 40) | (cmd << 32) | data;

    new NTSTATUS:status = msr_write(MSR_OC_MAILBOX, word);
    if (status != STATUS_SUCCESS) {
        out[0] = 0;
        out[1] = 0;
        return status;
    }

    // Poll for busy-bit clear
    new response = 0;
    for (new i = 0; i < retries; i++) {
        status = msr_read(MSR_OC_MAILBOX, response);
        if (status != STATUS_SUCCESS) {
            out[0] = 0;
            out[1] = 0;
            return status;
        }
        // Bit 63 clear = command complete
        if ((response >> 63) == 0) {
            out[0] = response & 0xFFFFFFFF;
            out[1] = (response >> 32) & 0x7FFFFFFF;  // error flags (exclude busy bit)
            return STATUS_SUCCESS;
        }
        microsleep(1);
    }

    out[0] = response & 0xFFFFFFFF;
    out[1] = (response >> 32) & 0xFFFFFFFF;
    return STATUS_TIMEOUT;
}

/// Set CPU affinity to a specific logical CPU before a read.
/// Useful for per-core MSR operations (e.g. reading per-core temps).
/// @param in [0] = logical CPU number
/// @param out [0], [1] = saved affinity (pass to ioctl_restore_affinity)
DEFINE_IOCTL_SIZED(ioctl_set_affinity, 1, 2) {
    new which = in[0];
    new old[2];
    cpu_set_affinity(which, old);
    out[0] = old[0];
    out[1] = old[1];
    return STATUS_SUCCESS;
}

/// Restore previous affinity after a per-core operation.
DEFINE_IOCTL_SIZED(ioctl_restore_affinity, 2, 0) {
    new old[2];
    old[0] = in[0];
    old[1] = in[1];
    cpu_restore_affinity(old);
    return STATUS_SUCCESS;
}

/// Return the CPU count for enumeration.
DEFINE_IOCTL_SIZED(ioctl_cpu_count, 0, 1) {
    out[0] = cpu_count();
    return STATUS_SUCCESS;
}

// ---- platform check ----

NTSTATUS:main() {
    // Reject non-x64 platforms outright
    if (get_arch() != ARCH_X64)
        return STATUS_NOT_SUPPORTED;
    // Reject non-Intel -- this module targets Intel OC Mailbox semantics
    if (get_cpu_vendor() != CpuVendor_Intel)
        return STATUS_NOT_SUPPORTED;
    return STATUS_SUCCESS;
}

// ---- unload hook (called when handle closes) ----

public NTSTATUS:unload() {
    // Nothing to clean up -- all operations are synchronous.
    return STATUS_SUCCESS;
}
