"""Descriptor-bound temporary writes for archive-safe operational output.

A pathname is never treated as continuing write authority.  POSIX temporary
children are created and installed relative to an open ``BoundDirectory``.
Windows temporary children are created with ``NtCreateFile`` relative to the
authorised native directory handle and installed/rolled back through handle-
relative namespace operations.  Callers write only through descriptors bound to
the exact created file object.

This closes both temporary-file substitution and parent-directory substitution
windows, including ordinary real-directory rename/replacement races.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import io
import os
import ctypes
import errno
from ctypes import wintypes
from pathlib import Path
import stat
import uuid
from typing import BinaryIO, Iterator, TextIO


class SecureWriteError(RuntimeError):
    """A temporary/output file lost its proven filesystem identity."""


class SecureWriteTransitionError(SecureWriteError):
    """A secure-write operation failed after a namespace transition occurred.

    Callers must not interpret this exception as proof that the destination
    namespace remained untouched.  ``target_name_acquired`` is true when the
    exact secured temporary object successfully acquired the requested target
    name before a later durability/attestation step failed.
    """

    def __init__(self, message: str, *, target_name_acquired: bool = False) -> None:
        super().__init__(message)
        self.target_name_acquired = bool(target_name_acquired)


# Capture platform capability once.  Adversarial tests intentionally monkeypatch
# os.rename/os.unlink to inject races; capability detection must not accidentally
# change merely because the function object was wrapped after import.
_DESCRIPTOR_BOUND_DIR_MUTATION_AVAILABLE = (
    os.name != "nt"
    and all(func in os.supports_dir_fd for func in (os.open, os.stat, os.unlink, os.rename, os.rmdir))
)


def _identity_from_stat(st) -> tuple[int, int]:
    return int(st.st_dev), int(st.st_ino)


_RENAME_NOREPLACE = 1


def _renameat2_noreplace(
    source_name: str, destination_name: str, *, source_dir_fd: int, destination_dir_fd: int
) -> None:
    """Atomically rename without replacing an existing destination.

    Linux ``renameat2(RENAME_NOREPLACE)`` makes destination non-existence part
    of the namespace mutation itself.  There is deliberately no fallback to
    ordinary ``rename()``: a check-then-rename sequence can destroy an object
    that arrives between the check and the syscall.
    """
    if os.name == "nt":
        raise SecureWriteError("POSIX atomic no-replace rename is unavailable on Windows")
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise SecureWriteError(
            "atomic no-replace rename is unavailable; refusing unsafe ordinary rename fallback"
        )
    renameat2.argtypes = [
        ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    rc = int(renameat2(
        int(source_dir_fd), os.fsencode(source_name),
        int(destination_dir_fd), os.fsencode(destination_name),
        _RENAME_NOREPLACE,
    ))
    if rc == 0:
        return
    err = int(ctypes.get_errno())
    if err == errno.EEXIST:
        raise FileExistsError(err, os.strerror(err), destination_name)
    if err in {errno.ENOSYS, errno.EINVAL, getattr(errno, "ENOTSUP", errno.EINVAL)}:
        raise SecureWriteError(
            "atomic no-replace rename is unsupported by this filesystem/platform"
        )
    raise OSError(err, os.strerror(err), destination_name)


def is_windows_reparse_point_stat(st, *, platform_name: str | None = None) -> bool:
    """Return whether *st* identifies a Windows reparse-point object.

    ``Path.is_symlink()`` is not sufficient on Windows because directory
    junctions and other name-surrogate objects are reparse points while still
    presenting as directories.  Archive-safety boundaries reject all reparse
    points rather than trying to infer which tags are harmless.  ``platform_name``
    exists so the predicate can be regression-tested on non-Windows runners.
    """
    platform_name = os.name if platform_name is None else str(platform_name)
    if platform_name != "nt":
        return False
    attributes = int(getattr(st, "st_file_attributes", 0) or 0)
    flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400) or 0x400)
    if attributes & flag:
        return True
    # Some Python/Windows combinations expose the reparse tag even when callers
    # provide a reduced stat-like object.  A non-zero tag is also fail-closed.
    return bool(int(getattr(st, "st_reparse_tag", 0) or 0))


def windows_path_has_reparse_component(path: str | Path) -> bool:
    """Return True when any existing Windows path component is a reparse point.

    A safe leaf is not sufficient if an ancestor is a junction.  Recovery and
    output authority therefore reject existing reparse components across the
    lexical path, while non-Windows platforms return False immediately.
    """
    if os.name != "nt":
        return False
    absolute = Path(os.path.abspath(os.fspath(path)))
    anchor = Path(absolute.anchor)
    current = anchor
    parts = absolute.parts
    start = 1 if absolute.anchor else 0
    for part in parts[start:]:
        current = current / part
        if not os.path.lexists(os.fspath(current)):
            break
        try:
            st = current.lstat()
        except OSError:
            return True
        if is_windows_reparse_point_stat(st):
            return True
    return False


def _path_regular_identity(path: Path) -> tuple[int, int]:
    if windows_path_has_reparse_component(path):
        raise SecureWriteError("secured temporary path traverses a Windows reparse point")
    try:
        st = path.lstat()
    except OSError as exc:
        raise SecureWriteError("secured temporary path disappeared") from exc
    if (
        stat.S_ISLNK(st.st_mode)
        or is_windows_reparse_point_stat(st)
        or not stat.S_ISREG(st.st_mode)
    ):
        raise SecureWriteError("secured temporary path is no longer the created regular file")
    return _identity_from_stat(st)


def _directory_identity(path: Path) -> tuple[int, int]:
    if windows_path_has_reparse_component(path):
        raise SecureWriteError("secured temporary directory traverses a Windows reparse point")
    try:
        st = path.lstat()
    except OSError as exc:
        raise SecureWriteError("secured temporary parent directory disappeared") from exc
    if (
        stat.S_ISLNK(st.st_mode)
        or is_windows_reparse_point_stat(st)
        or not stat.S_ISDIR(st.st_mode)
    ):
        raise SecureWriteError("secured temporary parent directory identity is unsafe")
    return _identity_from_stat(st)


def directory_identity(path: str | Path) -> tuple[int, int]:
    """Return the fail-closed filesystem identity of one safe directory object."""
    return _directory_identity(Path(path))


class WindowsDirectoryPin:
    """Handle-bound Windows authority over one exact directory object.

    Windows/Python 3.11 does not expose ``dir_fd`` mutation.  A share-mode-only
    "pin" is also insufficient on modern NTFS: an open directory handle may not
    reliably prevent every rename pattern used by higher-level Win32 APIs.

    The safe Windows strategy is therefore *handle-relative namespace access*.
    The authorised leaf directory is opened and its native file identity is
    matched to the higher-level expected identity.  Temporary children are then
    created with ``NtCreateFile`` using that directory handle as
    ``OBJECT_ATTRIBUTES.RootDirectory``.  Installation/rollback renames use
    ``SetFileInformationByHandle(FILE_RENAME_INFO)`` with the same root handle.

    Consequently, even if the original pathname is renamed away and replaced by
    another ordinary directory, no child write/rename is redirected into that
    replacement directory.  ``verify_pathname`` remains a *liveness/freshness*
    check, not the source of write authority.
    """

    # NT / Win32 constants used by the handle-relative implementation.
    _FILE_READ_DATA = 0x0001
    _FILE_WRITE_DATA = 0x0002
    _FILE_APPEND_DATA = 0x0004
    _FILE_READ_EA = 0x0008
    _FILE_WRITE_EA = 0x0010
    _FILE_EXECUTE = 0x0020
    _FILE_READ_ATTRIBUTES = 0x0080
    _FILE_WRITE_ATTRIBUTES = 0x0100
    _DELETE = 0x00010000
    _SYNCHRONIZE = 0x00100000

    _FILE_SHARE_READ = 0x00000001
    _FILE_SHARE_WRITE = 0x00000002
    _FILE_SHARE_DELETE = 0x00000004

    _FILE_OPEN = 0x00000001
    _FILE_CREATE = 0x00000002

    _FILE_DIRECTORY_FILE = 0x00000001
    _FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
    _FILE_NON_DIRECTORY_FILE = 0x00000040
    _FILE_OPEN_REPARSE_POINT = 0x00200000

    _OBJ_CASE_INSENSITIVE = 0x00000040
    _FILE_ATTRIBUTE_NORMAL = 0x00000080
    _FILE_ATTRIBUTE_DIRECTORY = 0x00000010
    _FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400

    # Win32 FILE_INFO_BY_HANDLE_CLASS value retained only for historical
    # compatibility comments; native rename uses NT FileRenameInformation (10).
    _FILE_RENAME_INFO_CLASS = 3
    _FILE_DISPOSITION_INFO_CLASS = 4

    def __init__(self, path: Path, identity: tuple[int, int], handle: int) -> None:
        self.path = path
        self.identity = identity
        self._handle = handle
        self._closed = False
        # Creation provenance is set only by the exact exclusive creator that
        # produced this bound object.  Binding an existing pathname never earns
        # operational ownership merely because a caller previously saw absence.
        self.final_component_created_by_this_operation = False

    @staticmethod
    def _kernel32():
        return ctypes.WinDLL("kernel32", use_last_error=True)

    @staticmethod
    def _ntdll():
        return ctypes.WinDLL("ntdll", use_last_error=True)

    @staticmethod
    def _close_native_handle(handle: int) -> None:
        if os.name != "nt" or not handle:
            return
        kernel32 = WindowsDirectoryPin._kernel32()
        close = kernel32.CloseHandle
        close.argtypes = [wintypes.HANDLE]
        close.restype = wintypes.BOOL
        close(wintypes.HANDLE(handle))

    @staticmethod
    def _native_info(handle: int):
        if os.name != "nt":
            raise SecureWriteError("native Windows file information is unavailable")

        class BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("dwFileAttributes", wintypes.DWORD),
                ("ftCreationTime", wintypes.FILETIME),
                ("ftLastAccessTime", wintypes.FILETIME),
                ("ftLastWriteTime", wintypes.FILETIME),
                ("dwVolumeSerialNumber", wintypes.DWORD),
                ("nFileSizeHigh", wintypes.DWORD),
                ("nFileSizeLow", wintypes.DWORD),
                ("nNumberOfLinks", wintypes.DWORD),
                ("nFileIndexHigh", wintypes.DWORD),
                ("nFileIndexLow", wintypes.DWORD),
            ]

        kernel32 = WindowsDirectoryPin._kernel32()
        get_info = kernel32.GetFileInformationByHandle
        get_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(BY_HANDLE_FILE_INFORMATION)]
        get_info.restype = wintypes.BOOL
        info = BY_HANDLE_FILE_INFORMATION()
        if not get_info(wintypes.HANDLE(handle), ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        return info

    @classmethod
    def _native_identity(cls, handle: int) -> tuple[int, int]:
        info = cls._native_info(handle)
        file_index = (int(info.nFileIndexHigh) << 32) | int(info.nFileIndexLow)
        return int(info.dwVolumeSerialNumber), file_index

    @classmethod
    def _native_is_unsafe_entry(cls, handle: int, *, allow_directory: bool) -> bool:
        attrs = int(cls._native_info(handle).dwFileAttributes)
        if attrs & cls._FILE_ATTRIBUTE_REPARSE_POINT:
            return True
        if not allow_directory and attrs & cls._FILE_ATTRIBUTE_DIRECTORY:
            return True
        return False

    @staticmethod
    def _open_directory_handle(path: Path) -> int:
        kernel32 = WindowsDirectoryPin._kernel32()
        create = kernel32.CreateFileW
        create.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        create.restype = wintypes.HANDLE
        FILE_LIST_DIRECTORY = 0x0001
        FILE_TRAVERSE = 0x0020
        FILE_READ_ATTRIBUTES = 0x0080
        SYNCHRONIZE = 0x00100000
        FILE_SHARE_READ = 0x00000001
        FILE_SHARE_WRITE = 0x00000002
        FILE_SHARE_DELETE = 0x00000004
        OPEN_EXISTING = 3
        FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
        FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
        handle = create(
            os.fspath(path),
            FILE_LIST_DIRECTORY | FILE_TRAVERSE | FILE_READ_ATTRIBUTES | SYNCHRONIZE,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            None,
            OPEN_EXISTING,
            FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        invalid = ctypes.c_void_p(-1).value
        value = int(handle) if handle is not None else invalid
        if value == invalid:
            raise ctypes.WinError(ctypes.get_last_error())
        return value

    @classmethod
    def bind(cls, path: str | Path) -> "WindowsDirectoryPin":
        """Bind the exact directory object first, before any safety decision.

        This is the authority-bootstrap primitive.  The native handle selects the
        object; only after that selection do callers validate its identity and
        policy.  A pathname swap before binding therefore cannot make a later
        ``directory_identity(path)`` silently bless the replacement object.
        """
        if os.name != "nt":
            raise SecureWriteError("native Windows directory authority is unavailable on this platform")
        absolute = Path(os.path.abspath(os.fspath(path)))
        handle = cls._open_directory_handle(absolute)
        try:
            if cls._native_is_unsafe_entry(handle, allow_directory=True):
                raise SecureWriteError("native Windows write-authority directory is unsafe")
            native_identity = cls._native_identity(handle)
            pin = cls(absolute, native_identity, handle)
            # Prove the lexical entry still names this exact object and that no
            # Windows reparse component is present.  This is freshness only; the
            # handle itself remains the authority.
            pin.verify_pathname()
            return pin
        except BaseException:
            cls._close_native_handle(handle)
            raise

    @classmethod
    def open(
        cls, path: str | Path, *, expected_identity: tuple[int, int]
    ) -> "WindowsDirectoryPin":
        pin = cls.bind(path)
        try:
            if pin.identity != tuple(expected_identity):
                raise SecureWriteError("native Windows directory is not the expected filesystem object")
            return pin
        except BaseException:
            pin.close()
            raise

    @staticmethod
    def _validate_child_name(name: str) -> str:
        name = str(name)
        if not name or name in (".", "..") or "\x00" in name:
            raise SecureWriteError("unsafe Windows directory child name")
        if "\\" in name or "/" in name or ":" in name:
            raise SecureWriteError("Windows directory child name must be a single relative component")
        return name

    @classmethod
    def _raise_ntstatus(cls, status: int, operation: str) -> None:
        # NT_SUCCESS is status >= 0 when interpreted as signed LONG.
        signed = ctypes.c_int32(status).value
        if signed >= 0:
            return
        ntdll = cls._ntdll()
        convert = ntdll.RtlNtStatusToDosError
        convert.argtypes = [wintypes.LONG]
        convert.restype = wintypes.ULONG
        winerr = int(convert(wintypes.LONG(status)))
        err = ctypes.WinError(winerr)
        err.strerror = f"{operation}: {err.strerror}"
        raise err

    def _nt_open_relative(
        self,
        name: str,
        *,
        desired_access: int,
        disposition: int,
        create_options: int,
        file_attributes: int | None = None,
    ) -> int:
        """Open/create one child relative to this exact directory handle."""
        if self._closed:
            raise SecureWriteError("Windows directory authority is closed")
        name = self._validate_child_name(name)

        class UNICODE_STRING(ctypes.Structure):
            _fields_ = [
                ("Length", wintypes.USHORT),
                ("MaximumLength", wintypes.USHORT),
                ("Buffer", wintypes.LPWSTR),
            ]

        class OBJECT_ATTRIBUTES(ctypes.Structure):
            _fields_ = [
                ("Length", wintypes.ULONG),
                ("RootDirectory", wintypes.HANDLE),
                ("ObjectName", ctypes.POINTER(UNICODE_STRING)),
                ("Attributes", wintypes.ULONG),
                ("SecurityDescriptor", wintypes.LPVOID),
                ("SecurityQualityOfService", wintypes.LPVOID),
            ]

        class IO_STATUS_BLOCK(ctypes.Structure):
            _fields_ = [
                ("Status", wintypes.LONG),
                ("Information", ctypes.c_size_t),
            ]

        name_buf = ctypes.create_unicode_buffer(name)
        byte_len = len(name.encode("utf-16-le"))
        us = UNICODE_STRING(byte_len, byte_len + 2, ctypes.cast(name_buf, wintypes.LPWSTR))
        oa = OBJECT_ATTRIBUTES(
            ctypes.sizeof(OBJECT_ATTRIBUTES),
            wintypes.HANDLE(self._handle),
            ctypes.pointer(us),
            self._OBJ_CASE_INSENSITIVE,
            None,
            None,
        )
        iosb = IO_STATUS_BLOCK()
        out_handle = wintypes.HANDLE()

        ntdll = self._ntdll()
        nt_create = ntdll.NtCreateFile
        nt_create.argtypes = [
            ctypes.POINTER(wintypes.HANDLE),
            wintypes.ULONG,
            ctypes.POINTER(OBJECT_ATTRIBUTES),
            ctypes.POINTER(IO_STATUS_BLOCK),
            ctypes.c_void_p,
            wintypes.ULONG,
            wintypes.ULONG,
            wintypes.ULONG,
            wintypes.ULONG,
            ctypes.c_void_p,
            wintypes.ULONG,
        ]
        nt_create.restype = wintypes.LONG
        status = int(nt_create(
            ctypes.byref(out_handle),
            desired_access,
            ctypes.byref(oa),
            ctypes.byref(iosb),
            None,
            self._FILE_ATTRIBUTE_NORMAL if file_attributes is None else int(file_attributes),
            self._FILE_SHARE_READ | self._FILE_SHARE_WRITE | self._FILE_SHARE_DELETE,
            disposition,
            create_options,
            None,
            0,
        ))
        self._raise_ntstatus(status, f"NtCreateFile relative child {name}")
        value = int(out_handle.value)
        if not value:
            raise SecureWriteError("NtCreateFile returned an invalid child handle")
        return value

    def create_temp_child(
        self, *, prefix: str, suffix: str, mode: int = 0o600
    ) -> tuple[int, str]:
        """Create a new regular temporary child relative to this handle."""
        if os.name != "nt":
            raise SecureWriteError("Windows handle-relative child creation is unavailable")
        import msvcrt

        desired = (
            self._FILE_READ_DATA
            | self._FILE_WRITE_DATA
            | self._FILE_READ_ATTRIBUTES
            | self._FILE_WRITE_ATTRIBUTES
            | self._DELETE
            | self._SYNCHRONIZE
        )
        options = (
            self._FILE_NON_DIRECTORY_FILE
            | self._FILE_SYNCHRONOUS_IO_NONALERT
            | self._FILE_OPEN_REPARSE_POINT
        )
        for _ in range(64):
            name = self._validate_child_name(f"{prefix}{uuid.uuid4().hex}{suffix}")
            handle = 0
            try:
                handle = self._nt_open_relative(
                    name,
                    desired_access=desired,
                    disposition=self._FILE_CREATE,
                    create_options=options,
                )
            except OSError as exc:
                # Name collision is extraordinarily unlikely; a collision is the
                # only reason to retry.  Other NT errors are fail-closed.
                if getattr(exc, "winerror", None) in (80, 183):
                    continue
                raise SecureWriteError("could not create Windows handle-relative temporary child") from exc
            try:
                if self._native_is_unsafe_entry(handle, allow_directory=False):
                    raise SecureWriteError("Windows temporary child is not a safe regular file")
                fd = msvcrt.open_osfhandle(handle, os.O_RDWR | getattr(os, "O_BINARY", 0))
                handle = 0  # CRT fd now owns the native handle.
                try:
                    os.fchmod(fd, mode)
                except (OSError, AttributeError):
                    pass
                return fd, name
            except BaseException:
                if handle:
                    self._close_native_handle(handle)
                raise
        raise SecureWriteError("could not allocate unique Windows handle-relative temporary child")

    def create_directory_child(self, name: str) -> "WindowsDirectoryPin":
        """Create and bind one child directory relative to this exact handle."""
        name = self._validate_child_name(name)
        desired = self._FILE_READ_DATA | self._FILE_EXECUTE | self._FILE_READ_ATTRIBUTES | self._SYNCHRONIZE
        options = self._FILE_DIRECTORY_FILE | self._FILE_SYNCHRONOUS_IO_NONALERT | self._FILE_OPEN_REPARSE_POINT
        handle = 0
        try:
            handle = self._nt_open_relative(
                name,
                desired_access=desired,
                disposition=self._FILE_CREATE,
                create_options=options,
                file_attributes=self._FILE_ATTRIBUTE_DIRECTORY,
            )
            if self._native_is_unsafe_entry(handle, allow_directory=True):
                raise SecureWriteError("new Windows directory child is unsafe")
            identity = self._native_identity(handle)
            child = WindowsDirectoryPin(self.path / name, identity, handle)
            child.final_component_created_by_this_operation = True
            handle = 0
            try:
                child.verify_pathname()
            except BaseException:
                child.close()
                raise
            return child
        except BaseException:
            if handle:
                self._close_native_handle(handle)
            raise

    def _open_existing_child_handle(self, name: str, *, delete_access: bool = False) -> int:
        desired = self._FILE_READ_ATTRIBUTES | self._SYNCHRONIZE
        if delete_access:
            desired |= self._DELETE
        options = self._FILE_SYNCHRONOUS_IO_NONALERT | self._FILE_OPEN_REPARSE_POINT
        return self._nt_open_relative(
            name,
            desired_access=desired,
            disposition=self._FILE_OPEN,
            create_options=options,
        )

    def child_info_or_none(self, name: str):
        name = self._validate_child_name(name)
        try:
            handle = self._open_existing_child_handle(name)
        except OSError as exc:
            if getattr(exc, "winerror", None) in (2, 3):
                return None
            raise SecureWriteError(f"cannot inspect Windows bound child: {name}") from exc
        try:
            return self._native_info(handle)
        finally:
            self._close_native_handle(handle)

    def child_identity_or_none(self, name: str) -> tuple[int, int] | None:
        name = self._validate_child_name(name)
        try:
            handle = self._open_existing_child_handle(name)
        except OSError as exc:
            if getattr(exc, "winerror", None) in (2, 3):
                return None
            raise SecureWriteError(f"cannot inspect Windows bound child: {name}") from exc
        try:
            if self._native_is_unsafe_entry(handle, allow_directory=False):
                raise SecureWriteError(f"Windows bound child is unsafe: {name}")
            return self._native_identity(handle)
        finally:
            self._close_native_handle(handle)

    def open_child_for_rename(self, name: str) -> int:
        name = self._validate_child_name(name)
        handle = self._open_existing_child_handle(name, delete_access=True)
        if self._native_is_unsafe_entry(handle, allow_directory=False):
            self._close_native_handle(handle)
            raise SecureWriteError(f"refusing to rename unsafe Windows child: {name}")
        return handle

    @staticmethod
    def _build_nt_rename_information(
        destination_name: str, *, root_handle: int, replace: bool
    ) -> tuple[ctypes.Array, int]:
        """Build a correctly sized ``FILE_RENAME_INFORMATION`` buffer.

        The Windows 10+ structure begins with a 4-byte union
        (``BOOLEAN ReplaceIfExists`` / ``ULONG Flags``), not a standalone
        one-byte field.  The trailing ``FileName[1]`` also means the caller
        must allocate enough storage for the complete UTF-16 name while
        retaining the structure's native alignment/padding.

        Fixed-width Windows field types keep the buffer layout testable even on
        the non-Windows CI runner; the syscall itself remains Windows-only.
        """
        destination_name = WindowsDirectoryPin._validate_child_name(destination_name)

        class FILE_RENAME_INFORMATION(ctypes.Structure):
            _fields_ = [
                # Windows ULONG is always 32-bit and WCHAR always 16-bit,
                # regardless of the host C ABI used by this test runner.
                ("ReplaceOrFlags", ctypes.c_uint32),
                ("RootDirectory", ctypes.c_void_p),
                ("FileNameLength", ctypes.c_uint32),
                ("FileName", ctypes.c_uint16 * 1),
            ]

        encoded = destination_name.encode("utf-16-le")
        offset = FILE_RENAME_INFORMATION.FileName.offset
        wchar_size = 2
        # sizeof(FILE_RENAME_INFORMATION) already contains FileName[1] and
        # trailing native alignment.  Add only the remaining filename bytes,
        # plus one NUL WCHAR as conservative user-mode backing storage.
        size = max(
            ctypes.sizeof(FILE_RENAME_INFORMATION),
            offset + len(encoded) + wchar_size,
        )
        raw = ctypes.create_string_buffer(size)
        info = FILE_RENAME_INFORMATION.from_buffer(raw)
        info.ReplaceOrFlags = ctypes.c_uint32(1 if replace else 0)
        info.RootDirectory = ctypes.c_void_p(root_handle)
        info.FileNameLength = len(encoded)
        ctypes.memmove(ctypes.addressof(raw) + offset, encoded, len(encoded))
        return raw, size

    def rename_handle(self, handle: int, destination_name: str, *, replace: bool) -> None:
        """Rename *handle* relative to this exact directory with NT semantics.

        ``NtCreateFile`` already establishes child authority relative to the
        authorised directory handle.  Use the matching NT information syscall
        for rename as well; this avoids re-entering Win32 pathname semantics and
        keeps ``RootDirectory`` authoritative through installation/rollback.
        """
        destination_name = self._validate_child_name(destination_name)
        raw, size = self._build_nt_rename_information(
            destination_name, root_handle=self._handle, replace=replace
        )

        class IO_STATUS_BLOCK(ctypes.Structure):
            _fields_ = [
                ("Status", wintypes.LONG),
                ("Information", ctypes.c_size_t),
            ]

        iosb = IO_STATUS_BLOCK()
        ntdll = self._ntdll()
        nt_set = ntdll.NtSetInformationFile
        nt_set.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(IO_STATUS_BLOCK),
            wintypes.LPVOID,
            wintypes.ULONG,
            wintypes.ULONG,
        ]
        nt_set.restype = wintypes.LONG
        # FILE_INFORMATION_CLASS::FileRenameInformation == 10.
        status = int(nt_set(
            wintypes.HANDLE(handle),
            ctypes.byref(iosb),
            ctypes.cast(raw, wintypes.LPVOID),
            wintypes.ULONG(size),
            wintypes.ULONG(10),
        ))
        self._raise_ntstatus(status, f"NtSetInformationFile rename to {destination_name}")

    def rename_fd(self, fd: int, destination_name: str, *, replace: bool) -> None:
        if os.name != "nt":
            raise SecureWriteError("Windows handle-relative rename is unavailable")
        import msvcrt
        handle = int(msvcrt.get_osfhandle(fd))
        if handle == -1:
            raise SecureWriteError("Windows temporary descriptor has no native handle")
        self.rename_handle(handle, destination_name, replace=replace)

    def delete_handle(self, handle: int) -> None:
        class FILE_DISPOSITION_INFO(ctypes.Structure):
            _fields_ = [("DeleteFile", wintypes.BOOLEAN)]

        info = FILE_DISPOSITION_INFO(wintypes.BOOLEAN(1))
        kernel32 = self._kernel32()
        set_info = kernel32.SetFileInformationByHandle
        set_info.argtypes = [wintypes.HANDLE, wintypes.INT, wintypes.LPVOID, wintypes.DWORD]
        set_info.restype = wintypes.BOOL
        if not set_info(
            wintypes.HANDLE(handle),
            self._FILE_DISPOSITION_INFO_CLASS,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            raise ctypes.WinError(ctypes.get_last_error())

    def delete_fd(self, fd: int) -> None:
        if os.name != "nt":
            raise SecureWriteError("Windows handle-relative delete is unavailable")
        import msvcrt
        handle = int(msvcrt.get_osfhandle(fd))
        if handle == -1:
            raise SecureWriteError("Windows temporary descriptor has no native handle")
        self.delete_handle(handle)

    def delete_child(self, name: str) -> bool:
        name = self._validate_child_name(name)
        try:
            handle = self.open_child_for_rename(name)
        except OSError as exc:
            if getattr(exc, "winerror", None) in (2, 3):
                return False
            raise
        try:
            self.delete_handle(handle)
            return True
        finally:
            self._close_native_handle(handle)

    def verify_pathname(self) -> None:
        """Verify the original lexical pathname still names this directory object."""
        if self._closed:
            raise SecureWriteError("Windows directory authority is closed")
        if self._native_identity(self._handle) != self.identity:
            raise SecureWriteError("Windows directory handle identity changed")
        if windows_path_has_reparse_component(self.path):
            raise SecureWriteError("bound write-authority path became a Windows reparse point")
        if _directory_identity(self.path) != self.identity:
            raise SecureWriteError("bound write-authority directory pathname changed")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._close_native_handle(self._handle)
        self._handle = 0

def descriptor_bound_directory_mutation_available() -> bool:
    """Whether Python can mutate children relative to an open directory object.

    Windows intentionally returns False: the Python 3.11 APIs used by this
    project do not expose the ``dir_fd`` mutation guarantees required by the
    archive-safety contract.
    """
    return bool(_DESCRIPTOR_BOUND_DIR_MUTATION_AVAILABLE)


def fsync_directory(path: Path) -> None:
    """Best-effort directory durability on platforms supporting directory fsync."""
    if os.name == "nt":
        return
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


@dataclass
class BoundDirectory:
    """Descriptor-bound authority over one exact directory object.

    Destructive child operations are performed relative to the open directory
    descriptor rather than by resolving ``path / child`` again.  On platforms
    where Python cannot provide descriptor-relative directory mutation, callers
    must fail closed and leave operational debris for explicit handling.
    """

    path: Path
    fd: int
    identity: tuple[int, int]
    parent: Path
    parent_fd: int
    parent_identity: tuple[int, int]
    entry_name: str
    final_component_created_by_this_operation: bool = False
    _closed: bool = False

    @staticmethod
    def _dir_flags() -> int:
        flags = os.O_RDONLY
        flags |= getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        return flags

    @classmethod
    def open(
        cls,
        path: str | Path,
        *,
        expected_identity: tuple[int, int] | None = None,
    ) -> "BoundDirectory":
        path = Path(os.path.abspath(os.fspath(path)))
        if path.name in (".", ".."):
            raise SecureWriteError("cannot bind an unsafe directory pathname")
        if path == path.parent:
            if not descriptor_bound_directory_mutation_available():
                raise SecureWriteError(
                    "descriptor-bound directory mutation is unavailable on this platform"
                )
            fd = os.open(path, cls._dir_flags())
            try:
                st = os.fstat(fd)
                if not stat.S_ISDIR(st.st_mode):
                    raise SecureWriteError("bound root object is not a directory")
                identity = _identity_from_stat(st)
                if expected_identity is not None and identity != tuple(expected_identity):
                    raise SecureWriteError("bound directory is not the expected filesystem object")
                parent_fd = os.dup(fd)
                return cls(
                    path=path, fd=fd, identity=identity, parent=path,
                    parent_fd=parent_fd, parent_identity=identity, entry_name="",
                )
            except BaseException:
                os.close(fd)
                raise
        parent = path.parent
        parent_fd = -1
        fd = -1
        try:
            if not descriptor_bound_directory_mutation_available():
                raise SecureWriteError(
                    "descriptor-bound directory mutation is unavailable on this platform"
                )
            parent_fd = os.open(parent, cls._dir_flags())
            pst = os.fstat(parent_fd)
            if not stat.S_ISDIR(pst.st_mode):
                raise SecureWriteError("bound directory parent is not a directory")
            parent_identity = _identity_from_stat(pst)

            fd = os.open(path.name, cls._dir_flags(), dir_fd=parent_fd)
            st = os.fstat(fd)
            if not stat.S_ISDIR(st.st_mode):
                raise SecureWriteError("bound stage object is not a directory")
            identity = _identity_from_stat(st)
            if expected_identity is not None and identity != expected_identity:
                raise SecureWriteError("bound directory is not the expected filesystem object")

            entry = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            if (
                stat.S_ISLNK(entry.st_mode)
                or is_windows_reparse_point_stat(entry)
                or not stat.S_ISDIR(entry.st_mode)
            ):
                raise SecureWriteError("bound directory pathname is unsafe")
            if _identity_from_stat(entry) != identity:
                raise SecureWriteError("bound directory pathname was substituted during open")
            return cls(
                path=path,
                fd=fd,
                identity=identity,
                parent=parent,
                parent_fd=parent_fd,
                parent_identity=parent_identity,
                entry_name=path.name,
            )
        except BaseException:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if parent_fd >= 0:
                try:
                    os.close(parent_fd)
                except OSError:
                    pass
            raise

    def _ensure_open(self) -> None:
        if self._closed:
            raise SecureWriteError("bound directory descriptor is closed")

    @staticmethod
    def validate_child_name(name: str) -> str:
        name = str(name)
        if not name or name in (".", "..") or "\x00" in name:
            raise SecureWriteError("unsafe bound-directory child name")
        if os.sep in name or (os.altsep and os.altsep in name):
            raise SecureWriteError("bound-directory child name must not contain path separators")
        return name

    def verify_handle(self) -> None:
        self._ensure_open()
        st = os.fstat(self.fd)
        if not stat.S_ISDIR(st.st_mode) or _identity_from_stat(st) != self.identity:
            raise SecureWriteError("bound directory handle identity changed")
        pst = os.fstat(self.parent_fd)
        if not stat.S_ISDIR(pst.st_mode) or _identity_from_stat(pst) != self.parent_identity:
            raise SecureWriteError("bound directory parent handle identity changed")

    def pathname_still_bound(self) -> bool:
        """Return whether the original parent entry still names this directory.

        Child mutation authority never depends on this check; it is used only for
        optional cleanup of the directory entry itself.
        """
        self.verify_handle()
        if not self.entry_name:
            try:
                return _directory_identity(self.path) == self.identity
            except SecureWriteError:
                return False
        try:
            st = os.stat(self.entry_name, dir_fd=self.parent_fd, follow_symlinks=False)
        except OSError:
            return False
        return (
            stat.S_ISDIR(st.st_mode)
            and not stat.S_ISLNK(st.st_mode)
            and not is_windows_reparse_point_stat(st)
            and _identity_from_stat(st) == self.identity
        )

    def list_names(self) -> list[str]:
        self.verify_handle()
        try:
            names = os.listdir(self.fd)
        except (OSError, TypeError) as exc:
            raise SecureWriteError("cannot enumerate bound directory by descriptor") from exc
        return [self.validate_child_name(name) for name in names]

    def lstat_child(self, name: str):
        self.verify_handle()
        name = self.validate_child_name(name)
        try:
            return os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        except OSError as exc:
            raise SecureWriteError(f"cannot inspect bound child: {name}") from exc

    def lstat_child_or_none(self, name: str):
        self.verify_handle()
        name = self.validate_child_name(name)
        try:
            return os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise SecureWriteError(f"cannot inspect bound child: {name}") from exc

    def rename_child(self, source_name: str, destination_name: str) -> None:
        """Rename one child entry to another inside this exact directory."""
        self.verify_handle()
        source_name = self.validate_child_name(source_name)
        destination_name = self.validate_child_name(destination_name)
        try:
            os.rename(
                source_name, destination_name,
                src_dir_fd=self.fd, dst_dir_fd=self.fd,
            )
            self.fsync()
        except (OSError, TypeError, NotImplementedError) as exc:
            raise SecureWriteError(
                f"could not rename bound child {source_name} -> {destination_name}"
            ) from exc

    def rename_child_noreplace_atomic(self, source_name: str, destination_name: str) -> None:
        """Perform only the atomic no-replace namespace transition.

        Successful return proves that ``destination_name`` has been acquired.
        Directory durability is deliberately *not* bundled into this method so
        callers can record transition provenance before any later fsync or
        identity verification can fail.
        """
        self.verify_handle()
        source_name = self.validate_child_name(source_name)
        destination_name = self.validate_child_name(destination_name)
        try:
            _renameat2_noreplace(
                source_name, destination_name,
                source_dir_fd=self.fd, destination_dir_fd=self.fd,
            )
        except FileExistsError:
            raise
        except SecureWriteError:
            raise
        except (OSError, TypeError, NotImplementedError) as exc:
            raise SecureWriteError(
                f"could not atomically rename bound child without replacement: {source_name} -> {destination_name}"
            ) from exc

    def rename_child_noreplace(self, source_name: str, destination_name: str) -> None:
        """Atomically rename one child only when the destination is absent.

        The no-replace condition is enforced by the filesystem operation itself.
        If durability fails after the rename, a transition-aware exception is
        raised so callers cannot mistake that failure for a pre-transition abort.
        """
        self.rename_child_noreplace_atomic(source_name, destination_name)
        try:
            self.fsync()
        except BaseException as exc:
            raise SecureWriteTransitionError(
                f"bound child acquired destination name but directory durability failed: "
                f"{source_name} -> {destination_name}",
                target_name_acquired=True,
            ) from exc

    def verify_pathname(self) -> None:
        self.verify_handle()
        if not self.pathname_still_bound():
            raise SecureWriteError("bound directory pathname changed")

    def create_directory_child(self, name: str, *, mode: int = 0o700) -> "BoundDirectory":
        """Create and bind one directory child through this exact descriptor."""
        self.verify_handle()
        name = self.validate_child_name(name)
        if os.mkdir not in os.supports_dir_fd:
            raise SecureWriteError("descriptor-bound directory creation is unavailable")
        child_fd = -1
        parent_dup = -1
        try:
            os.mkdir(name, mode=mode, dir_fd=self.fd)
            child_fd = os.open(name, self._dir_flags(), dir_fd=self.fd)
            st = os.fstat(child_fd)
            if not stat.S_ISDIR(st.st_mode):
                raise SecureWriteError("new bound child is not a directory")
            identity = _identity_from_stat(st)
            entry = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
            if stat.S_ISLNK(entry.st_mode) or _identity_from_stat(entry) != identity:
                raise SecureWriteError("new bound directory child was substituted")
            parent_dup = os.dup(self.fd)
            try:
                os.fsync(self.fd)
            except OSError:
                pass
            child = BoundDirectory(
                path=self.path / name,
                fd=child_fd,
                identity=identity,
                parent=self.path,
                parent_fd=parent_dup,
                parent_identity=self.identity,
                entry_name=name,
                final_component_created_by_this_operation=True,
            )
            child_fd = -1
            parent_dup = -1
            try:
                child.verify_pathname()
            except BaseException:
                child.close()
                raise
            return child
        except BaseException:
            if child_fd >= 0:
                os.close(child_fd)
            if parent_dup >= 0:
                os.close(parent_dup)
            # Do not attempt POSIX failure cleanup with ``rmdir(name)``.  Even
            # when this operation created the original child, that directory can
            # be renamed away and a source directory substituted under ``name``
            # before cleanup.  Without exact-object deletion authority, retain
            # the operational directory as diagnosable debris.
            raise

    def create_temp_child(
        self, *, prefix: str, suffix: str, mode: int = 0o600
    ) -> tuple[int, str]:
        """Create a new regular child through this exact directory descriptor."""
        self.verify_handle()
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        for _ in range(64):
            name = self.validate_child_name(f"{prefix}{uuid.uuid4().hex}{suffix}")
            try:
                fd = os.open(name, flags, mode, dir_fd=self.fd)
            except FileExistsError:
                continue
            except (OSError, TypeError, NotImplementedError) as exc:
                raise SecureWriteError("could not create descriptor-bound temporary child") from exc
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                os.close(fd)
                raise SecureWriteError("descriptor-bound temporary child is not a regular file")
            entry = self.lstat_child(name)
            if _identity_from_stat(entry) != _identity_from_stat(st):
                os.close(fd)
                raise SecureWriteError("temporary child pathname was substituted during creation")
            return fd, name
        raise SecureWriteError("could not allocate unique descriptor-bound temporary child")

    def fsync(self) -> None:
        self.verify_handle()
        try:
            os.fsync(self.fd)
        except OSError:
            pass

    def child_exists(self, name: str) -> bool:
        try:
            self.lstat_child(name)
            return True
        except SecureWriteError as exc:
            # Distinguish ordinary absence from unsafe/unsupported state.
            try:
                os.stat(self.validate_child_name(name), dir_fd=self.fd, follow_symlinks=False)
            except FileNotFoundError:
                return False
            except OSError:
                raise exc
            raise

    def unlink_child(self, name: str) -> bool:
        """Retain a POSIX child rather than deleting by a checked name.

        A directory descriptor binds the parent namespace, but POSIX does not
        provide a general portable primitive that says "unlink this exact inode".
        Any implementation based on ``stat(name)`` followed by ``unlink(name)``
        can delete a different object substituted between those syscalls.

        Phase 14.1.17 therefore removes automatic destructive child cleanup on
        POSIX safety-critical paths.  Callers may inspect/record the candidate,
        but deletion requires an exact-object primitive (for example the native
        Windows handle path) or explicit manual intervention.
        """
        self.verify_handle()
        self.validate_child_name(name)
        return False

    def remove_self_if_still_named(self) -> bool:
        """Retain the POSIX directory entry instead of check-then-rmdir cleanup.

        ``pathname_still_bound()`` is useful for observation, but it cannot
        authorise a later ``rmdir(name)`` because the parent entry can be swapped
        after the check.  Operational debris is preferable to deleting a source
        directory substituted into that namespace slot.
        """
        self.verify_handle()
        return False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            os.close(self.fd)
        finally:
            os.close(self.parent_fd)

    def __enter__(self) -> "BoundDirectory":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def bind_directory_authority(path: str | Path):
    """Return an open authority for the exact existing directory object.

    Callers must validate ``authority.identity`` *after* this function returns
    and before any mutation.  The returned object exposes ``identity``, ``path``,
    ``verify_pathname()``, ``create_directory_child()`` and ``close()`` on both
    POSIX and Windows.
    """
    path = Path(path)
    if os.name == "nt":
        return WindowsDirectoryPin.bind(path)
    return BoundDirectory.open(path)


def ensure_directory_authority(path: str | Path, *, validator=None):
    """Bind or exclusively create a directory and retain creation provenance.

    ``final_component_created_by_this_operation`` is true only when the exact
    final directory object was produced by this call's handle/descriptor-relative
    exclusive creation primitive.  Observing pathname absence before this call is
    deliberately *not* evidence of ownership.

    The nearest existing ancestor is bound first and optionally validated.
    Missing components are then created relative to that exact authority and
    each newly selected child is validated before it can create the next one.
    The caller owns the returned authority and must close it.
    """
    absolute = Path(os.path.abspath(os.fspath(Path(path).expanduser())))
    missing: list[str] = []
    current = absolute
    while not os.path.lexists(os.fspath(current)):
        if current == current.parent:
            raise SecureWriteError("cannot locate an existing ancestor for directory authority")
        missing.append(current.name)
        current = current.parent

    authority = bind_directory_authority(current)
    # A raw bind can never prove creation, even when a caller observed this path
    # absent earlier.  Exclusive child creation below is the only provenance source.
    authority.final_component_created_by_this_operation = False
    try:
        if validator is not None:
            validator(authority)
        for name in reversed(missing):
            child = authority.create_directory_child(name)
            try:
                if validator is not None:
                    validator(child)
            except BaseException:
                # Windows can delete the exact created object through its native
                # handle.  POSIX cannot safely turn an earlier identity check
                # into pathname deletion, so a failed validation deliberately
                # leaves the created directory as operational debris.
                try:
                    if isinstance(child, WindowsDirectoryPin):
                        child.delete_handle(child._handle)
                except Exception:
                    pass
                child.close()
                raise
            authority.close()
            authority = child
        authority.verify_pathname()
        return authority
    except BaseException:
        authority.close()
        raise


@dataclass
class BoundTemporaryFile:
    """A secure temporary file whose write authority is parent-object-bound.

    The higher-level caller may supply the exact directory identity it already
    authorised.  POSIX creates the child through that directory descriptor;
    Windows creates and installs children relative to the authorised native
    directory handle, so later pathname substitution cannot redirect writes.
    """

    path: Path
    fd: int
    identity: tuple[int, int]
    parent: Path
    parent_identity: tuple[int, int]
    _bound_parent: BoundDirectory | None = None
    _windows_pin: WindowsDirectoryPin | None = None
    _closed: bool = False
    _installed: bool = False

    @classmethod
    def create(
        cls,
        parent: str | Path,
        *,
        prefix: str = "ppa.",
        suffix: str = ".tmp",
        mode: int = 0o600,
        expected_parent_identity: tuple[int, int] | None = None,
    ) -> "BoundTemporaryFile":
        parent_path = Path(parent)
        expected = (
            _directory_identity(parent_path)
            if expected_parent_identity is None
            else tuple(expected_parent_identity)
        )
        bound_parent: BoundDirectory | None = None
        windows_pin: WindowsDirectoryPin | None = None
        fd = -1
        path: Path | None = None
        identity: tuple[int, int] | None = None
        try:
            if os.name == "nt":
                windows_pin = WindowsDirectoryPin.open(
                    parent_path, expected_identity=expected
                )
                fd, child_name = windows_pin.create_temp_child(
                    prefix=prefix, suffix=suffix, mode=mode
                )
                path = parent_path / child_name
            else:
                bound_parent = BoundDirectory.open(
                    parent_path, expected_identity=expected
                )
                fd, child_name = bound_parent.create_temp_child(
                    prefix=prefix, suffix=suffix, mode=mode
                )
                path = parent_path / child_name

            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise SecureWriteError("secured temporary creation did not create a regular file")
            identity = _identity_from_stat(st)
            if bound_parent is not None:
                child = bound_parent.lstat_child(path.name)
                if _identity_from_stat(child) != identity:
                    raise SecureWriteError("new temporary child does not identify its open descriptor")
                if not bound_parent.pathname_still_bound():
                    raise SecureWriteError("authorised temporary parent pathname changed during creation")
            else:
                assert windows_pin is not None
                windows_pin.verify_pathname()
                child_identity = windows_pin.child_identity_or_none(path.name)
                if child_identity != identity:
                    raise SecureWriteError("new temporary child does not identify its open descriptor")

            try:
                os.fchmod(fd, mode)
            except (OSError, AttributeError):
                pass
            return cls(
                path, fd, identity, parent_path, expected,
                _bound_parent=bound_parent, _windows_pin=windows_pin,
            )
        except BaseException:
            if path is not None and identity is not None:
                try:
                    if bound_parent is not None:
                        # POSIX cannot compare-and-unlink an inode atomically.
                        # A partially created temporary is therefore retained as
                        # operational debris instead of risking deletion of a
                        # source object swapped into its pathname during cleanup.
                        pass
                    elif windows_pin is not None and fd >= 0:
                        # The control fd is still bound to the exact child object;
                        # deletion is handle-relative and never re-derived from path.
                        windows_pin.delete_fd(fd)
                except Exception:
                    pass
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if bound_parent is not None:
                bound_parent.close()
            if windows_pin is not None:
                windows_pin.close()
            raise

    def _ensure_open(self) -> None:
        if self._closed:
            raise SecureWriteError("secured temporary descriptor is closed")

    def _verify_parent_authority(self, *, require_pathname: bool = True) -> None:
        if self._bound_parent is not None:
            self._bound_parent.verify_handle()
            if self._bound_parent.identity != self.parent_identity:
                raise SecureWriteError("descriptor-bound parent identity changed")
            if require_pathname and not self._bound_parent.pathname_still_bound():
                raise SecureWriteError("authorised parent pathname was substituted")
            return
        if self._windows_pin is not None:
            self._windows_pin.verify_pathname()
            if self._windows_pin.identity != self.parent_identity:
                raise SecureWriteError("pinned Windows parent identity changed")
            return
        if _directory_identity(self.parent) != self.parent_identity:
            raise SecureWriteError("secured temporary parent directory changed")

    def verify_identity(self) -> None:
        self._ensure_open()
        st = os.fstat(self.fd)
        if not stat.S_ISREG(st.st_mode) or _identity_from_stat(st) != self.identity:
            raise SecureWriteError("secured temporary descriptor identity changed")
        self._verify_parent_authority(require_pathname=True)
        if self.path.parent != self.parent:
            raise SecureWriteError("secured temporary path escaped its parent")
        if self._bound_parent is not None:
            child = self._bound_parent.lstat_child_or_none(self.path.name)
            if child is None or _identity_from_stat(child) != self.identity:
                raise SecureWriteError("secured temporary pathname was substituted")
        elif self._windows_pin is not None:
            child_identity = self._windows_pin.child_identity_or_none(self.path.name)
            if child_identity != self.identity:
                raise SecureWriteError("secured temporary child identity changed")
        elif _path_regular_identity(self.path) != self.identity:
            raise SecureWriteError("secured temporary pathname was substituted")

    @contextmanager
    def binary_writer(self) -> Iterator[BinaryIO]:
        """Yield a writable/seekable duplicate handle to the created object."""
        self._ensure_open()
        dup = os.dup(self.fd)
        fh = os.fdopen(dup, "w+b", buffering=0)
        try:
            # Duplicates share file offset on POSIX; make the intended start explicit.
            fh.seek(0)
            yield fh
            if not fh.closed:
                fh.flush()
        finally:
            try:
                fh.close()
            except OSError:
                pass

    @contextmanager
    def text_writer(
        self,
        *,
        encoding: str = "utf-8",
        newline: str | None = "",
    ) -> Iterator[TextIO]:
        self._ensure_open()
        dup = os.dup(self.fd)
        raw = os.fdopen(dup, "w+b", buffering=0)
        text = io.TextIOWrapper(raw, encoding=encoding, newline=newline, write_through=True)
        try:
            raw.seek(0)
            yield text
            if not text.closed:
                text.flush()
        finally:
            try:
                text.close()
            except OSError:
                pass

    def write_bytes(self, data: bytes) -> None:
        with self.binary_writer() as fh:
            fh.write(data)
        self.sync_and_verify()

    def write_text(self, text: str, *, encoding: str = "utf-8", newline: str | None = "") -> None:
        with self.text_writer(encoding=encoding, newline=newline) as fh:
            fh.write(text)
        self.sync_and_verify()

    def sync_and_verify(self) -> None:
        self._ensure_open()
        os.fsync(self.fd)
        self.verify_identity()

    def hash_and_size(self) -> tuple[str, int]:
        """Hash/read back the exact descriptor-bound object, never its pathname."""
        self.sync_and_verify()
        dup = os.dup(self.fd)
        digest = hashlib.sha256()
        total = 0
        try:
            os.lseek(dup, 0, os.SEEK_SET)
            while True:
                chunk = os.read(dup, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                total += len(chunk)
        finally:
            os.close(dup)
        self.verify_identity()
        return digest.hexdigest(), total

    def _install_windows(
        self, destination: Path, *, replace: bool,
        expected_existing_identity: tuple[int, int] | None = None,
    ) -> Path:
        """Install inside the exact Windows directory handle, never by parent path.

        The temporary child was created relative to ``WindowsDirectoryPin``.
        Existing-destination parking, final installation, rollback and backup
        deletion all remain relative to that same directory handle.  The lexical
        pathname is revalidated as a liveness condition, but a pathname swap can
        no longer redirect any namespace mutation into the replacement directory.
        """
        if self._windows_pin is None or os.name != "nt":
            raise SecureWriteError("Windows handle-relative installation is unavailable")
        if destination.parent != self.parent:
            raise SecureWriteError(
                "secured temporary installation must remain in its parent directory"
            )
        if self._closed:
            raise SecureWriteError("secured temporary descriptor is closed")

        import msvcrt

        pin = self._windows_pin
        backup_handle = 0
        backup_name: str | None = None
        backup_moved = False
        installed = False
        temp_handle = int(msvcrt.get_osfhandle(self.fd))
        if temp_handle == -1:
            raise SecureWriteError("Windows temporary descriptor has no native handle")

        def destination_info():
            return pin.child_info_or_none(destination.name)

        def allocate_and_park_existing() -> None:
            nonlocal backup_handle, backup_name, backup_moved
            info = destination_info()
            if info is None:
                if expected_existing_identity is not None:
                    raise SecureWriteError("expected owned destination disappeared before secured replacement")
                return
            attrs = int(info.dwFileAttributes)
            if attrs & pin._FILE_ATTRIBUTE_DIRECTORY:
                raise SecureWriteError("destination is a directory")
            if attrs & pin._FILE_ATTRIBUTE_REPARSE_POINT:
                raise SecureWriteError("destination is a Windows reparse point")
            if not replace:
                raise SecureWriteError("destination already exists")

            # Open the exact object that would be parked, then compare its native
            # identity before any namespace mutation.  A source object substituted
            # after the caller's ownership check is therefore never blessed merely
            # because it occupies the owned pathname.
            backup_handle = pin.open_child_for_rename(destination.name)
            if expected_existing_identity is not None:
                actual = tuple(pin._native_identity(backup_handle))
                if actual != tuple(expected_existing_identity):
                    raise SecureWriteError(
                        "destination is not the exact positively owned object authorised for replacement"
                    )
            for _ in range(32):
                candidate = pin._validate_child_name(
                    f".{destination.name}.ppa-rollback-{uuid.uuid4().hex}.bak"
                )
                try:
                    pin.rename_handle(backup_handle, candidate, replace=False)
                    backup_name = candidate
                    backup_moved = True
                    return
                except OSError as exc:
                    if getattr(exc, "winerror", None) in (80, 183):
                        continue
                    raise
            raise SecureWriteError("could not allocate a unique Windows rollback destination")

        def restore_previous_destination() -> None:
            nonlocal installed, backup_moved
            # Delete only the exact PPA-installed object through its already-bound
            # native handle.  Never delete/replace whatever merely occupies the
            # destination pathname during rollback.
            if installed and not self._closed:
                pin.delete_fd(self.fd)
                installed = False
            if backup_moved and backup_handle:
                if destination_info() is not None:
                    raise SecureWriteError(
                        "rollback destination is occupied by an unexpected object; "
                        "leaving the previous PPA object parked as recovery debris"
                    )
                try:
                    pin.rename_handle(backup_handle, destination.name, replace=False)
                except OSError as exc:
                    if getattr(exc, "winerror", None) in (80, 183):
                        raise SecureWriteError(
                            "rollback destination became occupied; previous PPA object remains parked"
                        ) from exc
                    raise
                backup_moved = False

        try:
            self.sync_and_verify()
            allocate_and_park_existing()

            # Re-check freshness after parking any old destination.  A swap here
            # fails closed, and the parked destination is restored by handle.
            self.sync_and_verify()
            if destination_info() is not None:
                raise SecureWriteError("destination appeared during secured installation")

            try:
                pin.rename_fd(self.fd, destination.name, replace=False)
            except OSError as exc:
                # Native no-replace did exactly what we require: a late-arriving
                # destination must survive untouched.  Normalize the Windows
                # sharing/name-collision error into the secure-write contract so
                # higher layers report a safety refusal rather than leaking a raw
                # FileExistsError/WinError 183.
                if getattr(exc, "winerror", None) in (80, 183):
                    raise SecureWriteError(
                        "destination appeared during secured installation; refusing replacement"
                    ) from exc
                raise
            installed = True
            if pin._native_identity(temp_handle) != self.identity:
                raise SecureWriteError("installed Windows file handle identity changed")

            # The write/install itself is already safe regardless of pathname
            # substitution.  This check prevents reporting success for a stale
            # lexical destination after the authorised directory was renamed.
            pin.verify_pathname()

            if backup_moved and backup_handle:
                pin.delete_handle(backup_handle)
                backup_moved = False
            self._installed = True
            self.close_control()
            return destination
        except BaseException as exc:
            target_name_acquired = bool(installed)
            try:
                if installed or backup_moved:
                    restore_previous_destination()
            except BaseException as restore_exc:
                if target_name_acquired:
                    raise SecureWriteTransitionError(
                        "Windows secured installation acquired the destination name and rollback could not prove recovery",
                        target_name_acquired=True,
                    ) from restore_exc
                raise SecureWriteError(
                    "Windows secured installation failed and previous destination could not be restored"
                ) from restore_exc
            if target_name_acquired:
                if isinstance(exc, SecureWriteTransitionError):
                    raise
                raise SecureWriteTransitionError(
                    "Windows secured installation acquired the destination name before a later operation failed",
                    target_name_acquired=True,
                ) from exc
            if isinstance(exc, SecureWriteError):
                raise
            raise
        finally:
            if backup_handle:
                try:
                    pin._close_native_handle(backup_handle)
                except Exception:
                    pass

    def install(
        self, destination: str | Path, *, replace: bool = True,
        expected_existing_identity: tuple[int, int] | None = None,
    ) -> Path:
        """Atomically install the exact descriptor-bound file object.

        On POSIX, destination namespace mutations are bound to an open directory
        descriptor and new-name acquisition uses atomic no-replace semantics.
        Parent pathname substitution therefore cannot redirect installation, and
        late-arriving destination objects are never replaced. Windows uses the
        native handle-relative rename/delete authority established in Phase 14.1.8.
        """
        destination = Path(destination)
        if self._windows_pin is not None and os.name == "nt":
            return self._install_windows(
                destination, replace=replace,
                expected_existing_identity=expected_existing_identity,
            )

        self.sync_and_verify()
        if destination.parent != self.parent:
            raise SecureWriteError(
                "secured temporary installation must remain in its parent directory"
            )
        self._verify_parent_authority(require_pathname=True)

        parent_bound: BoundDirectory | None = self._bound_parent
        owns_parent_bound = False
        if parent_bound is None and os.name != "nt":
            parent_bound = BoundDirectory.open(
                destination.parent, expected_identity=self.parent_identity
            )
            owns_parent_bound = True

        backup: Path | None = None
        installed = False

        def lexists(path: Path) -> bool:
            if parent_bound is not None and path.parent == destination.parent:
                return parent_bound.lstat_child_or_none(path.name) is not None
            return os.path.lexists(os.fspath(path))

        def lstat_entry(path: Path):
            if parent_bound is not None and path.parent == destination.parent:
                st = parent_bound.lstat_child_or_none(path.name)
                if st is None:
                    raise FileNotFoundError(path)
                return st
            return path.lstat()

        def rename_entry_noreplace(source: Path, dest: Path) -> None:
            if (
                parent_bound is not None
                and source.parent == destination.parent
                and dest.parent == destination.parent
            ):
                parent_bound.rename_child_noreplace(source.name, dest.name)
                return
            raise SecureWriteError(
                "atomic no-replace namespace mutation is unavailable without bound parent authority"
            )

        def fresh_backup_path() -> Path:
            return destination.parent / (
                f".{destination.name}.ppa-rollback-{uuid.uuid4().hex}.bak"
            )

        def park_existing(expected_identity: tuple[int, int]) -> Path:
            # Parking itself is no-replace: a hostile collision at the randomly
            # selected backup name can never be overwritten.  If the destination
            # object was substituted after the ownership check, renameat2 may move
            # that unexpected object to the backup name, but the subsequent exact
            # identity check detects it and deliberately leaves it intact as debris.
            for _ in range(32):
                candidate = fresh_backup_path()
                try:
                    rename_entry_noreplace(destination, candidate)
                except FileExistsError:
                    continue
                parked_st = lstat_entry(candidate)
                if _identity_from_stat(parked_st) != tuple(expected_identity):
                    raise SecureWriteError(
                        "destination changed during secured replacement; unexpected object "
                        "was preserved as rollback debris"
                    )
                return candidate
            raise SecureWriteError("could not allocate a unique rollback destination")

        def restore_previous_destination() -> None:
            nonlocal backup
            if backup is None or not lexists(backup):
                return
            # Rollback is intentionally non-destructive.  An object that appears
            # in the destination slot after parking is never unlinked or replaced.
            if lexists(destination):
                raise SecureWriteError(
                    "rollback destination is occupied by an unexpected object; "
                    "previous PPA object remains parked as recovery debris"
                )
            try:
                rename_entry_noreplace(backup, destination)
            except FileExistsError as exc:
                raise SecureWriteError(
                    "rollback destination became occupied; previous PPA object remains parked"
                ) from exc
            backup = None
            if parent_bound is not None:
                parent_bound.fsync()

        try:
            if lexists(destination):
                if not replace:
                    raise SecureWriteError("destination already exists")
                st = lstat_entry(destination)
                if stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode):
                    raise SecureWriteError("destination is a directory")
                actual_identity = _identity_from_stat(st)
                authorised_identity = (
                    tuple(expected_existing_identity)
                    if expected_existing_identity is not None
                    else actual_identity
                )
                if actual_identity != authorised_identity:
                    raise SecureWriteError(
                        "destination is not the exact positively owned object authorised for replacement"
                    )
                backup = park_existing(authorised_identity)
            elif expected_existing_identity is not None:
                raise SecureWriteError("expected owned destination disappeared before secured replacement")

            self.verify_identity()
            if parent_bound is not None:
                temp_st = parent_bound.lstat_child_or_none(self.path.name)
                if temp_st is None or _identity_from_stat(temp_st) != self.identity:
                    raise SecureWriteError(
                        "secured temporary pathname was substituted before bound installation"
                    )
            if os.name == "nt":
                self.close_control()
                if _path_regular_identity(self.path) != self.identity:
                    raise SecureWriteError(
                        "secured temporary pathname was substituted after descriptor close"
                    )

            # No check-then-rename.  Destination absence is enforced atomically by
            # renameat2(RENAME_NOREPLACE).  If a source object arrives after the
            # last observation, the syscall fails with EEXIST and does not replace it.
            try:
                rename_entry_noreplace(self.path, destination)
            except FileExistsError as exc:
                raise SecureWriteError(
                    "destination appeared during secured installation; no object was replaced"
                ) from exc
            except SecureWriteTransitionError:
                # rename_child_noreplace() reports that its atomic rename succeeded
                # before a later directory-durability failure.  Preserve that fact
                # across this install boundary even though the helper did not return.
                installed = True
                raise
            # Successful return means both atomic acquisition and the helper's
            # immediate parent-directory durability step completed.
            installed = True

            if parent_bound is not None:
                installed_st = parent_bound.lstat_child_or_none(destination.name)
                if installed_st is None or _identity_from_stat(installed_st) != self.identity:
                    raise SecureWriteError(
                        "installed destination is not the secured temporary object"
                    )
            elif _path_regular_identity(destination) != self.identity:
                raise SecureWriteError("installed destination is not the secured temporary object")
            if not self._closed:
                st = os.fstat(self.fd)
                if not stat.S_ISREG(st.st_mode) or _identity_from_stat(st) != self.identity:
                    raise SecureWriteError("installed descriptor identity changed")

            if parent_bound is not None:
                parent_bound.fsync()
            elif _directory_identity(destination.parent) != self.parent_identity:
                raise SecureWriteError("destination parent directory changed during installation")
            else:
                fsync_directory(destination.parent)

            # POSIX does not provide a general inode-bound unlink primitive.
            # Deleting a rollback backup with stat(name)->unlink(name) would reopen
            # the exact race this phase is closing, so successful replacement may
            # deliberately retain the previous owned object as rollback debris.
            if not self._closed:
                self.close_control()
            return destination
        except BaseException as exc:
            transition_occurred = bool(installed)
            try:
                if installed or backup is not None:
                    restore_previous_destination()
            except BaseException as restore_exc:
                if transition_occurred:
                    raise SecureWriteTransitionError(
                        "secured installation acquired the destination name and rollback could not prove recovery",
                        target_name_acquired=True,
                    ) from restore_exc
                raise SecureWriteError(
                    "secured installation failed and previous destination could not be restored"
                ) from restore_exc
            if transition_occurred:
                if isinstance(exc, SecureWriteTransitionError):
                    raise
                raise SecureWriteTransitionError(
                    "secured installation acquired the destination name before a later operation failed",
                    target_name_acquired=True,
                ) from exc
            if isinstance(exc, SecureWriteError):
                raise
            raise
        finally:
            if owns_parent_bound and parent_bound is not None:
                parent_bound.close()

    def close_control(self) -> None:
        if self._closed:
            return
        try:
            os.close(self.fd)
        finally:
            self._closed = True

    def cleanup(self) -> None:
        """Best-effort cleanup without widening parent-directory authority."""
        if self._windows_pin is not None:
            try:
                if not self._closed:
                    if not self._installed:
                        try:
                            self._windows_pin.delete_fd(self.fd)
                        except (OSError, SecureWriteError):
                            # Leave exact operational debris rather than fall
                            # back to pathname deletion.
                            pass
                    self.close_control()
            finally:
                try:
                    self._windows_pin.close()
                except Exception:
                    pass
                self._windows_pin = None
            return

        # POSIX has no general inode-bound unlink-by-descriptor primitive.
        # Even a successful identity check followed by unlink(name) reopens a
        # check/use race, so failed or aborted temporaries are retained as PPA
        # operational debris.  Normal successful installation consumes the temp
        # name through atomic renameat2(RENAME_NOREPLACE).
        self.close_control()
        if self._bound_parent is not None:
            try:
                self._bound_parent.close()
            except Exception:
                pass
            self._bound_parent = None

    def __enter__(self) -> "BoundTemporaryFile":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.cleanup()


def atomic_write_bytes(
    destination: str | Path,
    data: bytes,
    *,
    prefix: str | None = None,
    suffix: str = ".tmp",
    replace: bool = True,
    expected_parent_identity: tuple[int, int] | None = None,
) -> Path:
    destination = Path(destination)
    if expected_parent_identity is None:
        destination.parent.mkdir(parents=True, exist_ok=True)
    elif not destination.parent.is_dir():
        raise SecureWriteError(
            "expected parent authority was supplied but the parent directory is unavailable"
        )
    temp = BoundTemporaryFile.create(
        destination.parent,
        prefix=prefix or (destination.name + "."),
        suffix=suffix,
        expected_parent_identity=expected_parent_identity,
    )
    try:
        temp.write_bytes(data)
        return temp.install(destination, replace=replace)
    finally:
        temp.cleanup()


def atomic_write_text(
    destination: str | Path,
    text: str,
    *,
    encoding: str = "utf-8",
    prefix: str | None = None,
    suffix: str = ".tmp",
    replace: bool = True,
    expected_parent_identity: tuple[int, int] | None = None,
) -> Path:
    return atomic_write_bytes(
        destination,
        text.encode(encoding),
        prefix=prefix,
        suffix=suffix,
        replace=replace,
        expected_parent_identity=expected_parent_identity,
    )
