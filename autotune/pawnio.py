"""
PawnIO backend: talk to the signed namazso/PawnIO kernel driver.

Architecture:
  * PawnIO is a signed Microsoft-blessed kernel driver that runs Pawn
    bytecode "modules" in kernel mode. We DON'T ship our own module --
    the signed driver rejects unsigned bytecode (RSA-4096 over SHA256,
    namazso's 2023 key, hardcoded in the driver).
  * Instead we use the official signed IntelMSR module from
    https://github.com/namazso/PawnIO.Modules -- it already exposes the
    exact MSRs we need (MSR_PLATFORM_INFO, MSR_TURBO_RATIO_LIMIT,
    MSR_OC_MAILBOX, thermal, power, etc.) via ioctl_read_msr /
    ioctl_write_msr publics.
  * Phase 0 = reads only. Writes wait for Phase 1.

Wire protocol (verbatim from PawnIO/include/pawnio_um.h and vm.cpp on
master, commit range 2026-04):

  Device: \\.\PawnIO  (deprecated DOS path but functional; real NT path
                      is \Device\PawnIO)

  IOCTL codes (CTL_CODE with device_type=0xA1B2, METHOD_BUFFERED,
  FILE_ANY_ACCESS):
    IOCTL_PIO_LOAD_BINARY = 0xA1B22084
    IOCTL_PIO_EXECUTE_FN  = 0xA1B22104
    IOCTL_PIO_VERSION     = 0xA1B22184

  LOAD_BINARY input:
    uint32_le  sig_len
    uint8[sig_len]  signature
    uint8[*]   amx_bytecode
  Output: empty. NTSTATUS = either the signature-check / load error, or
  the return value of the Pawn main() function.

  EXECUTE_FN input:
    char[32]   function_name   (NUL-terminated; must start with "ioctl_")
    uint64[]   args
  Output:
    uint64[]   results
  NTSTATUS = return value of the Pawn public, or standard error.

The whole thing is buffered I/O -- input and output share the same
system buffer kernel-side, but the SDK copies safely via DeviceIoControl.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import platform
import struct
from pathlib import Path
from typing import Iterable, Optional

log = logging.getLogger(__name__)


# ---------- Win32 ----------

GENERIC_READ  = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ  = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

IOCTL_PIO_LOAD_BINARY = 0xA1B22084
IOCTL_PIO_EXECUTE_FN  = 0xA1B22104
IOCTL_PIO_VERSION     = 0xA1B22184

DEVICE_PATH = r"\\.\PawnIO"
FN_NAME_LEN = 32


# NT-namespace structs for fallback open via NtOpenFile (used when the
# Win32 \\.\\PawnIO symlink isn't created -- newer PawnIO releases ship
# without it).
class UNICODE_STRING(ctypes.Structure):
    _fields_ = [
        ("Length",         wt.USHORT),
        ("MaximumLength",  wt.USHORT),
        ("Buffer",         wt.LPWSTR),
    ]

class OBJECT_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("Length",                  wt.ULONG),
        ("RootDirectory",           wt.HANDLE),
        ("ObjectName",              ctypes.POINTER(UNICODE_STRING)),
        ("Attributes",              wt.ULONG),
        ("SecurityDescriptor",      ctypes.c_void_p),
        ("SecurityQualityOfService",ctypes.c_void_p),
    ]

class IO_STATUS_BLOCK(ctypes.Structure):
    _fields_ = [
        ("Status",      wt.LONG),
        ("Information", ctypes.c_size_t),
    ]

OBJ_CASE_INSENSITIVE = 0x00000040
FILE_OPEN = 1
FILE_NON_DIRECTORY_FILE = 0x00000040
SYNCHRONIZE = 0x00100000
FILE_READ_DATA  = 0x0001
FILE_WRITE_DATA = 0x0002


def _bind_win32():
    if platform.system() != "Windows":
        return None, None
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wt.HANDLE
    k32.CreateFileW.argtypes = [
        wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p,
        wt.DWORD, wt.DWORD, wt.HANDLE,
    ]
    k32.DeviceIoControl.restype = wt.BOOL
    k32.DeviceIoControl.argtypes = [
        wt.HANDLE, wt.DWORD, ctypes.c_void_p, wt.DWORD,
        ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.c_void_p,
    ]
    k32.CloseHandle.restype = wt.BOOL
    k32.CloseHandle.argtypes = [wt.HANDLE]

    nt = ctypes.WinDLL("ntdll", use_last_error=True)
    nt.NtOpenFile.restype = wt.LONG  # NTSTATUS
    nt.NtOpenFile.argtypes = [
        ctypes.POINTER(wt.HANDLE),
        wt.ULONG,
        ctypes.POINTER(OBJECT_ATTRIBUTES),
        ctypes.POINTER(IO_STATUS_BLOCK),
        wt.ULONG,
        wt.ULONG,
    ]
    nt.RtlInitUnicodeString.restype = None
    nt.RtlInitUnicodeString.argtypes = [
        ctypes.POINTER(UNICODE_STRING), wt.LPCWSTR,
    ]
    return k32, nt


_K32, _NT = _bind_win32()


# ---------- Exceptions ----------

class PawnIOError(RuntimeError):
    """Any PawnIO failure -- invalid handle, load error, execute error."""


class PawnIODriverMissing(PawnIOError):
    """\\.\PawnIO doesn't open. The PawnIO driver is not installed."""


class PawnIOLoadFailed(PawnIOError):
    """IOCTL_PIO_LOAD_BINARY returned a non-success NTSTATUS. Typically
    means the module's RSA signature failed verification, or main()
    rejected the host platform."""


class PawnIOCallFailed(PawnIOError):
    """IOCTL_PIO_EXECUTE_FN returned a non-success NTSTATUS. The NTSTATUS
    might be from the Pawn public itself (e.g. STATUS_ACCESS_DENIED for
    an MSR not on the allow-list) or from the driver-side IOCTL layer."""
    def __init__(self, msg: str, ntstatus: int):
        super().__init__(f"{msg} (NTSTATUS=0x{ntstatus & 0xFFFFFFFF:08X})")
        self.ntstatus = ntstatus & 0xFFFFFFFF


# ---------- Client ----------

class PawnIOClient:
    def __init__(self):
        self._linux_dev = (platform.system() != "Windows")
        self.handle: Optional[int] = None
        if self._linux_dev:
            return
        self._open()

    def _open(self) -> None:
        if _K32 is None:
            raise PawnIOError("Not on Windows.")
        # First try the Win32 DOS namespace path (older PawnIO builds).
        h = _K32.CreateFileW(
            DEVICE_PATH,
            GENERIC_READ | GENERIC_WRITE,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None, OPEN_EXISTING, 0, None,
        )
        h_int = int(h) if h is not None else 0
        if h_int != 0 and h_int != INVALID_HANDLE_VALUE:
            self.handle = h_int
            log.info("Opened PawnIO via Win32 path %s (handle=0x%X)",
                     DEVICE_PATH, h_int)
            return

        first_err = ctypes.get_last_error()
        log.info("CreateFile(%s) -> err=%d; trying NT path \Device\PawnIO",
                 DEVICE_PATH, first_err)

        # Fallback: open via NT namespace (\Device\PawnIO) using NtOpenFile.
        # This is what PawnIO's official SDK does; the DOS symlink is marked
        # deprecated and may be absent.
        if _NT is None:
            raise PawnIODriverMissing(
                f"CreateFile failed (err={first_err}) and ntdll is not "
                "available for fallback."
            )
        nt_path = "\\Device\\PawnIO"  # actual chars: \Device\PawnIO
        # Own the wide-char buffer so its lifetime exceeds the NtOpenFile call.
        # ctypes' auto-conversion of a Python str to LPCWSTR frees the temp
        # buffer when the call returns, which would leave UNICODE_STRING.Buffer
        # dangling -- caused STATUS_OBJECT_PATH_SYNTAX_BAD in our last run.
        wbuf = ctypes.create_unicode_buffer(nt_path)
        us = UNICODE_STRING()
        us.Length = len(nt_path) * ctypes.sizeof(ctypes.c_wchar)  # bytes, no NUL
        us.MaximumLength = us.Length + ctypes.sizeof(ctypes.c_wchar)
        us.Buffer = ctypes.cast(wbuf, wt.LPWSTR)
        oa = OBJECT_ATTRIBUTES(
            Length=ctypes.sizeof(OBJECT_ATTRIBUTES),
            RootDirectory=None,
            ObjectName=ctypes.pointer(us),
            Attributes=OBJ_CASE_INSENSITIVE,
            SecurityDescriptor=None,
            SecurityQualityOfService=None,
        )
        iosb = IO_STATUS_BLOCK()
        ntfile_handle = wt.HANDLE()
        status = _NT.NtOpenFile(
            ctypes.byref(ntfile_handle),
            FILE_READ_DATA | FILE_WRITE_DATA | SYNCHRONIZE,
            ctypes.byref(oa),
            ctypes.byref(iosb),
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            FILE_NON_DIRECTORY_FILE,
        )
        if status != 0:
            raise PawnIODriverMissing(
                f"NtOpenFile({nt_path}) failed with NTSTATUS=0x{status & 0xFFFFFFFF:08X}. "
                f"PawnIO driver service is reportedly running -- if this still "
                f"fails, the device may be access-controlled or the namespace "
                f"path differs from \\Device\\PawnIO."
            )
        h_int = int(ntfile_handle.value) if ntfile_handle.value else 0
        if h_int == 0:
            raise PawnIODriverMissing("NtOpenFile returned NULL handle")
        self.handle = h_int
        log.info("Opened PawnIO via NT path %s (handle=0x%X)", nt_path, h_int)

    def close(self) -> None:
        if self.handle and _K32 is not None:
            _K32.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self): return self
    def __exit__(self, *a): self.close()

    # ---------- low-level ----------

    def _ioctl(self, code: int, in_buf: bytes, out_size: int) -> tuple[bytes, int]:
        """Returns (output bytes, bytes returned count). Raises on IOCTL fail
        but NOT on NTSTATUS error inside the driver -- callers interpret
        NTSTATUS themselves."""
        if self._linux_dev or self.handle is None:
            raise PawnIOError("Client not open.")
        in_arr  = (ctypes.c_ubyte * len(in_buf)).from_buffer_copy(in_buf)
        out_arr = (ctypes.c_ubyte * out_size)()
        returned = wt.DWORD(0)
        ok = _K32.DeviceIoControl(
            self.handle, code,
            ctypes.cast(in_arr, ctypes.c_void_p), len(in_buf),
            ctypes.cast(out_arr, ctypes.c_void_p), out_size,
            ctypes.byref(returned), None,
        )
        if not ok:
            err = ctypes.get_last_error()
            # Windows maps most NTSTATUS-returned-from-kernel to an
            # equivalent Win32 error via RtlNtStatusToDosError. For IOCTL
            # we want the raw NTSTATUS which is tricky to recover; we
            # surface the Win32 error and leave NTSTATUS handling to the
            # execute path, which ALSO returns the status via result
            # buffer convention in the Pawn public itself.
            raise PawnIOError(f"IOCTL 0x{code:08X} failed; GetLastError={err}")
        return bytes(out_arr[:returned.value]), returned.value

    # ---------- high-level: LOAD ----------

    def load_module(self, amx_blob: bytes, signature: bytes = b"") -> None:
        """Upload a compiled Pawn module. For the signed driver, `amx_blob`
        must be the raw compiled AMX bytecode and `signature` is the
        RSA-4096 signature produced by namazso's CI. For official signed
        modules (like IntelMSR.bin) the downloaded .bin file already
        contains the complete framed blob -- pass the whole file as
        `amx_blob` with an EMPTY signature argument and we'll skip the
        framing.

        IntelMSR.bin (and all official PawnIO.Modules) are already wrapped
        in the u32 sig_len + sig + amx framing by their CI. So a user-
        supplied .bin that came from the releases page IS the complete
        IOCTL payload.
        """
        # Detect framing: if the .bin starts with a small u32 that, when
        # interpreted as sig_len, leaves >= 64 bytes after, assume it's
        # a framed module. Otherwise wrap it ourselves with the given
        # signature (which may be empty -- that only works on an
        # unrestricted build of the driver).
        payload = amx_blob
        looks_framed = False
        if len(amx_blob) >= 8:
            maybe_sig_len = int.from_bytes(amx_blob[:4], "little")
            if 0 <= maybe_sig_len <= len(amx_blob) - 4:
                looks_framed = True

        if not looks_framed or signature:
            # Wrap: [sig_len][sig][amx]
            payload = (len(signature).to_bytes(4, "little")
                       + signature + amx_blob)
            log.info("Framing module: sig_len=%d total=%d",
                     len(signature), len(payload))
        else:
            log.info("Module appears pre-framed (sig_len=%d total=%d); "
                     "passing through.",
                     int.from_bytes(amx_blob[:4], "little"), len(amx_blob))

        _, _ = self._ioctl(IOCTL_PIO_LOAD_BINARY, payload, 0)
        log.info("Module loaded (%d bytes).", len(payload))

    def load_module_from_file(self, path: Path) -> None:
        data = path.read_bytes()
        self.load_module(data)

    # ---------- high-level: EXECUTE ----------

    def execute(self, fn_name: str, args: Iterable[int],
                out_count: int) -> list[int]:
        """Call a Pawn public named `fn_name` (must start with 'ioctl_').
        Pass `args` as a sequence of ints; they're packed as uint64 LE.
        Returns a list of `out_count` ints (Pawn cells).
        """
        if not fn_name.startswith("ioctl_"):
            raise PawnIOError(
                f"PawnIO requires function names to start with 'ioctl_'; "
                f"got '{fn_name}'"
            )
        if len(fn_name) > FN_NAME_LEN - 1:
            raise PawnIOError(f"Function name too long: {fn_name}")

        name_bytes = fn_name.encode("ascii")
        name_buf = name_bytes + b"\x00" * (FN_NAME_LEN - len(name_bytes))

        args_list = list(args)
        args_buf = b"".join(int(a).to_bytes(8, "little", signed=False) for a in args_list)

        in_buf = name_buf + args_buf
        out_size = out_count * 8

        out_bytes, n = self._ioctl(IOCTL_PIO_EXECUTE_FN, in_buf, out_size)
        if n < out_size:
            # The driver sets IoStatus.Information to requested out size on
            # success, so getting back less means the IOCTL itself failed
            # partway. But _ioctl already raised on failure, so this should
            # not happen. Defensive anyway.
            raise PawnIOError(
                f"Short output: wanted {out_size} bytes, got {n}")

        cells = []
        for i in range(out_count):
            cell = int.from_bytes(out_bytes[i*8:(i+1)*8], "little", signed=False)
            cells.append(cell)
        return cells

    # ---------- Userspace OC Mailbox (works with SIGNED IntelMSR module) ----------

    # Bit positions inside the 64-bit mailbox command word:
    #   bit 63       BUSY / RUN -- set by us, cleared by P-code
    #   bits 42..40  DOMAIN
    #   bits 39..32  COMMAND
    #   bits 31..0   DATA payload
    OC_MB_BUSY    = 1 << 63
    OC_MB_MSR     = 0x150

    def oc_mailbox_via_msr(self, cmd: int, domain: int = 0, data: int = 0,
                           retries: int = 10, poll_us: int = 200) -> tuple[int, int, bool]:
        """Issue an OC Mailbox transaction using the signed IntelMSR module's
        bare ioctl_read_msr / ioctl_write_msr (both of which allow-list 0x150).

        Returns (response_low32, error_high32, completed).
        completed=False means the busy bit never cleared within retries
        -- typically because the command code is invalid/unimplemented.

        SAFE for any read command. Writing cmd codes whose effect is
        unknown can change CPU state; do not call this with arbitrary
        cmd/data without knowing what it does.
        """
        import time
        word = (
            self.OC_MB_BUSY
            | ((domain & 0x7) << 40)
            | ((cmd & 0xFF) << 32)
            | (data & 0xFFFFFFFF)
        )
        # Send the command
        self.execute("ioctl_write_msr", [self.OC_MB_MSR, word], out_count=0)

        # Poll for busy clear
        last = word
        for _ in range(retries):
            last = self.read_msr(self.OC_MB_MSR)
            if (last >> 63) == 0:
                return (last & 0xFFFFFFFF,
                        (last >> 32) & 0x7FFFFFFF,
                        True)
            time.sleep(poll_us / 1_000_000)
        # Timeout: busy never cleared. Return whatever we last saw.
        return (last & 0xFFFFFFFF,
                (last >> 32) & 0xFFFFFFFF,
                False)

    def fivr_read_voltage_offset_v2(self, plane: int) -> int:
        """Read the FIVR-plane voltage offset using the userspace mailbox path.
        Decoded to signed millivolts. Plane 0=Core, 1=iGPU, 2=Cache, 3=SA."""
        resp, err, ok = self.oc_mailbox_via_msr(cmd=0x10, domain=plane, data=0)
        if not ok:
            raise PawnIOError(f"OC mailbox cmd 0x10 timed out (busy never cleared)")
        if err != 0:
            raise PawnIOError(f"OC mailbox returned error 0x{err:X}")
        raw = (resp >> 21) & 0x7FF
        if raw & 0x400:
            raw -= 0x800
        return round(raw / 1.024)

    def mailbox_probe_command(self, cmd: int, domain: int = 0, data: int = 0,
                              retries: int = 5) -> dict:
        """Send a command and structure the response for diagnostic display.
        Read-only intent: caller is responsible for not passing destructive cmds."""
        try:
            resp, err, ok = self.oc_mailbox_via_msr(cmd, domain, data, retries=retries)
            return {
                "cmd": cmd, "domain": domain, "data_in": data,
                "response": resp, "error_flags": err, "completed": ok,
                "timed_out": not ok,
                "raw_response_word": (resp | (err << 32)),
            }
        except Exception as e:
            return {
                "cmd": cmd, "domain": domain, "data_in": data,
                "exception": str(e),
            }

    # ---------- OC Mailbox (uses the ioctl_oc_mailbox helper in autotune.amx) ----------

    def oc_mailbox(self, cmd: int, domain: int = 0, data: int = 0,
                   retries: int = 5) -> tuple[int, int]:
        """Issue one OC Mailbox transaction. Kernel-side wrapper does the
        write + poll + read-response sequence. Returns (response_low32,
        error_flags). Requires autotune.amx loaded (not IntelMSR.bin)."""
        out = self.execute("ioctl_oc_mailbox",
                           [cmd, domain, data, retries],
                           out_count=2)
        return out[0], out[1]

    def mailbox_read_voltage_offset(self, plane: int) -> int:
        """Read FIVR-plane voltage offset in mV. plane 0=Core, 1=iGPU,
        2=CacheRing, 3=SA, 4=iGPU-unslice. Verified protocol (undervolt.py
        / Plundervolt paper / VoltageShift). Returns signed mV."""
        resp, err = self.oc_mailbox(cmd=0x10, domain=plane, data=0)
        if err != 0:
            raise PawnIOError(f"OC mailbox read failed: err=0x{err:X}")
        # Decode: (resp >> 21) signed 11-bit -> mV = round(x / 1.024)
        raw = (resp >> 21) & 0x7FF
        if raw & 0x400:  # sign bit
            raw -= 0x800
        return round(raw / 1.024)

    def mailbox_write_voltage_offset(self, plane: int, mv: int) -> None:
        """Write FIVR-plane voltage offset (verified cmd 0x11).
        plane 0=Core, signed mV. Write-then-verify pattern."""
        if abs(mv) > 999:
            raise PawnIOError(f"Voltage offset {mv} mV out of range (+-999)")
        scaled = round(mv * 1.024)
        encoded = (scaled & 0xFFF) << 21
        encoded &= 0xFFE00000
        resp, err = self.oc_mailbox(cmd=0x11, domain=plane, data=encoded)
        if err != 0:
            raise PawnIOError(f"OC mailbox write failed: err=0x{err:X}")
        # Read-back verify
        got = self.mailbox_read_voltage_offset(plane)
        if got != mv:
            raise PawnIOError(
                f"Voltage write rejected: asked {mv} mV, read back {got} mV. "
                f"Likely OC-Lock / Plundervolt mitigation engaged.")

    def load_bundled_module(self) -> None:
        """Load the bundled autotune.amx from the PyInstaller bundle or
        from the source tree. Requires the unrestricted PawnIO driver."""
        import sys
        if getattr(sys, "frozen", False):
            base = Path(getattr(sys, "_MEIPASS", "."))
            amx = base / "autotune" / "autotune.amx"
        else:
            amx = Path(__file__).parent / "autotune.amx"
        if not amx.exists():
            raise PawnIOError(f"Bundled autotune.amx not found at {amx}")
        self.load_module_from_file(amx)

    # ---------- MSR convenience ----------

    def read_msr(self, msr: int) -> int:
        """Call ioctl_read_msr on the loaded IntelMSR module."""
        out = self.execute("ioctl_read_msr", [msr & 0xFFFFFFFF], out_count=1)
        return out[0]

    # Alias so cpu.py (which expects a .read(msr) method like MsrClient)
    # works unchanged against either backend.
    def read(self, msr: int) -> int:
        return self.read_msr(msr)

    def write_msr(self, msr: int, value: int) -> None:
        """Call ioctl_write_msr. Raises if the MSR isn't in the module's
        allow-list (returns STATUS_ACCESS_DENIED)."""
        # Phase 0: forbid at the Python layer even though IntelMSR allows it
        # for PKG_POWER_LIMIT / OC_MAILBOX. Phase 1 lifts this.
        raise PawnIOError(
            "PawnIO write_msr disabled in Phase 0. Enable in Phase 1 after "
            "the full guarded-write framework is wired up."
        )

    # ---------- version ----------

    def driver_version(self) -> int:
        out, _ = self._ioctl(IOCTL_PIO_VERSION, b"", 4)
        return int.from_bytes(out, "little")
