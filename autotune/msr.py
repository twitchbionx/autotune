"""
Direct MSR (Model-Specific Register) access for Intel CPUs on Windows.

Piggybacks on the WinRing0-family kernel driver loaded by
LibreHardwareMonitor. No driver of our own is shipped in Phase 0.

CRITICAL IMPLEMENTATION NOTE
----------------------------
Every Win32 function used here has explicit .restype and .argtypes
declarations. Without them, ctypes on 64-bit Windows silently
truncates the returned HANDLE to 32 bits, CreateFileW "succeeds"
with garbage, and DeviceIoControl fails with ERROR_INVALID_HANDLE
(error 6) -- which is exactly the failure mode that tripped us up
in the first test. Do not remove these declarations.

References:
  * Intel SDM Vol 4 -- MSR reference
  * Microsoft docs -- CreateFileW, DeviceIoControl type signatures
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import platform
from typing import Optional

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Win32 constants
# ---------------------------------------------------------------------------

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3

# HANDLE is a pointer; INVALID_HANDLE_VALUE is (HANDLE)-1 which is all 1 bits.
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

# WinRing0 IOCTLs
IOCTL_OLS_READ_MSR  = 0x9C402084     # func 0x821 | METHOD_BUFFERED
IOCTL_OLS_WRITE_MSR = 0x9C402088     # Phase 1 only
IOCTL_OLS_READ_PCI  = 0x9C402110

_DEVICE_CANDIDATES = [
    r"\\.\WinRing0_1_2_0",
    r"\\.\WinRing0_1_3_0",
    r"\\.\LibreHardwareMonitor",
    r"\\.\OpenLibSys",
    r"\\.\HWiNFO",
]


# ---------------------------------------------------------------------------
# Properly-typed Win32 function bindings (the fix for the err=6 bug)
# ---------------------------------------------------------------------------

def _bind_win32():
    """Declare restype/argtypes for every Win32 call we use. Call once."""
    if platform.system() != "Windows":
        return None
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    # HANDLE CreateFileW(LPCWSTR, DWORD, DWORD, LPSECURITY_ATTRIBUTES,
    #                    DWORD, DWORD, HANDLE);
    k32.CreateFileW.restype = wt.HANDLE
    k32.CreateFileW.argtypes = [
        wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p,
        wt.DWORD, wt.DWORD, wt.HANDLE,
    ]

    # BOOL DeviceIoControl(HANDLE, DWORD, LPVOID, DWORD, LPVOID, DWORD,
    #                      LPDWORD, LPOVERLAPPED);
    k32.DeviceIoControl.restype = wt.BOOL
    k32.DeviceIoControl.argtypes = [
        wt.HANDLE, wt.DWORD, ctypes.c_void_p, wt.DWORD,
        ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.c_void_p,
    ]

    # BOOL CloseHandle(HANDLE);
    k32.CloseHandle.restype = wt.BOOL
    k32.CloseHandle.argtypes = [wt.HANDLE]

    # DWORD GetLastError(void);
    k32.GetLastError.restype = wt.DWORD
    k32.GetLastError.argtypes = []

    return k32


_K32 = _bind_win32()


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class MsrError(RuntimeError): ...
class DriverNotLoadedError(MsrError): ...
class MsrWriteForbidden(MsrError): ...


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class MsrClient:
    def __init__(self, allow_writes: bool = False, device: Optional[str] = None):
        self._linux_dev = (platform.system() != "Windows")
        self.handle: Optional[int] = None
        self.device_name: Optional[str] = None
        self._allow_writes = allow_writes
        if self._linux_dev:
            return
        self._open(device)

    def _open(self, device: Optional[str]) -> None:
        if _K32 is None:
            raise MsrError("Not on Windows.")
        candidates = [device] if device else _DEVICE_CANDIDATES
        last_err: Optional[str] = None
        for name in candidates:
            h = _K32.CreateFileW(
                name,
                GENERIC_READ | GENERIC_WRITE,
                FILE_SHARE_READ | FILE_SHARE_WRITE,
                None,
                OPEN_EXISTING,
                0,
                None,
            )
            # h is now a properly-typed HANDLE (c_void_p-sized int). Compare
            # to INVALID_HANDLE_VALUE as a plain int.
            h_int = int(h) if h is not None else 0
            if h_int != 0 and h_int != INVALID_HANDLE_VALUE:
                self.handle = h_int
                self.device_name = name
                log.info("Opened MSR driver: %s (handle=0x%X)", name, h_int)
                return
            err = ctypes.get_last_error()
            last_err = f"{name} -> err {err}"
            log.debug("Could not open %s: error %d", name, err)
        raise DriverNotLoadedError(
            "Could not open any WinRing0-family driver device. "
            "Is LibreHardwareMonitor running (with admin rights, in "
            "your user session)? "
            f"Last error: {last_err}"
        )

    def close(self) -> None:
        if self.handle and _K32 is not None:
            _K32.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self): return self
    def __exit__(self, *a): self.close()

    def read(self, msr: int) -> int:
        if self._linux_dev or self.handle is None:
            raise MsrError("MSR client not open on a Windows host.")

        in_buf  = (ctypes.c_uint32 * 1)(msr)
        out_buf = (ctypes.c_uint32 * 2)(0, 0)
        bytes_returned = wt.DWORD(0)

        ok = _K32.DeviceIoControl(
            self.handle,
            IOCTL_OLS_READ_MSR,
            ctypes.cast(in_buf, ctypes.c_void_p),
            ctypes.sizeof(in_buf),
            ctypes.cast(out_buf, ctypes.c_void_p),
            ctypes.sizeof(out_buf),
            ctypes.byref(bytes_returned),
            None,
        )
        if not ok:
            err = ctypes.get_last_error()
            raise MsrError(
                f"DeviceIoControl READ_MSR 0x{msr:X} failed (err={err}). "
                f"device={self.device_name}, handle=0x{self.handle:X}"
            )
        if bytes_returned.value < 8:
            raise MsrError(
                f"READ_MSR 0x{msr:X} returned {bytes_returned.value} bytes, "
                f"expected 8. Wrong IOCTL for this driver version?"
            )
        # out_buf[0] = EAX (low 32), out_buf[1] = EDX (high 32)
        return (int(out_buf[1]) << 32) | int(out_buf[0])

    def write(self, msr: int, value: int) -> None:
        if not self._allow_writes:
            raise MsrWriteForbidden(
                f"MSR writes disabled (attempted 0x{msr:X} = 0x{value:X})."
            )
        raise MsrWriteForbidden(
            "Phase 1 write path not implemented yet. See PROJECT_CHARTER.md."
        )

    def is_open(self) -> bool:
        return self.handle is not None
