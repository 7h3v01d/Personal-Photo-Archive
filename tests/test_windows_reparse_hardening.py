"""Windows reparse/junction safety regressions for recovery authority boundaries."""
from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess
from types import SimpleNamespace

import pytest

from ppa.db import connect
from ppa.recovery_preservation import RecoveryPreservationError, _directory_identity
from ppa.secure_write import is_windows_reparse_point_stat


def test_windows_reparse_stat_predicate_is_testable_cross_platform() -> None:
    fake = SimpleNamespace(
        st_file_attributes=0x400,
        st_reparse_tag=0,
    )
    assert is_windows_reparse_point_stat(fake, platform_name="nt") is True
    assert is_windows_reparse_point_stat(fake, platform_name="posix") is False


def test_recovery_directory_identity_rejects_reparse_semantics(tmp_path: Path, monkeypatch) -> None:
    import ppa.recovery_preservation as rp

    directory = tmp_path / "stage"
    directory.mkdir()
    monkeypatch.setattr(rp, "is_windows_reparse_point_stat", lambda _st: True)
    with pytest.raises(RecoveryPreservationError, match="unsafe"):
        _directory_identity(directory)


def test_windows_nt_rename_information_layout_is_fixed_width() -> None:
    """The user-mode rename buffer must match Windows ABI widths off-platform too."""
    import ctypes
    from ppa.secure_write import WindowsDirectoryPin

    name = "installed.txt"
    encoded = name.encode("utf-16-le")
    root = 0x1234
    raw, size = WindowsDirectoryPin._build_nt_rename_information(
        name, root_handle=root, replace=True
    )
    blob = bytes(raw[:size])

    pointer_size = ctypes.sizeof(ctypes.c_void_p)
    root_offset = 8 if pointer_size == 8 else 4
    length_offset = root_offset + pointer_size
    name_offset = length_offset + 4

    assert int.from_bytes(blob[0:4], "little") == 1
    assert int.from_bytes(blob[root_offset:root_offset + pointer_size], "little") == root
    assert int.from_bytes(blob[length_offset:length_offset + 4], "little") == len(encoded)
    assert blob[name_offset:name_offset + len(encoded)] == encoded
    assert size >= name_offset + len(encoded) + 2


@pytest.mark.skipif(os.name != "nt", reason="native Windows junction test")
def test_windows_native_junction_is_rejected_as_recovery_stage(tmp_path: Path) -> None:
    target = tmp_path / "real-stage"
    target.mkdir()
    junction = tmp_path / "junction-stage"
    proc = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(target)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        pytest.skip(f"could not create NTFS junction: {proc.stderr or proc.stdout}")
    st = junction.lstat()
    assert is_windows_reparse_point_stat(st)
    with pytest.raises(RecoveryPreservationError, match="unsafe|reparse|junction"):
        _directory_identity(junction)


@pytest.mark.skipif(os.name != "nt", reason="native Windows handle-relative write authority")
def test_windows_native_directory_handle_relative_child_survives_path_substitution(tmp_path: Path) -> None:
    """A renamed/replaced pathname cannot redirect child creation away from the authorised directory."""
    from ppa.secure_write import WindowsDirectoryPin, directory_identity

    authorised = tmp_path / "authority-root" / "stage"
    authorised.mkdir(parents=True)
    identity = directory_identity(authorised)
    parked = tmp_path / "authority-root" / "stage.parked"

    replacement = tmp_path / "replacement-library"
    replacement.mkdir()
    user_sidecar = replacement / "ppa-test.tmp"
    user_sidecar.write_bytes(b"USER-SOURCE-DATA")
    before = user_sidecar.read_bytes()

    authority = WindowsDirectoryPin.open(authorised, expected_identity=identity)
    fd = -1
    child_name = ""
    try:
        # Modern Windows may permit rename despite an open directory handle.  That
        # is acceptable: namespace mutation below must remain relative to the
        # original handle rather than relying on rename blocking.
        os.rename(authorised, parked)
        os.rename(replacement, authorised)

        fd, child_name = authority.create_temp_child(prefix="ppa-native-", suffix=".tmp")
        os.write(fd, b"HANDLE-RELATIVE")
        os.fsync(fd)

        # The native create handle may intentionally deny a second pathname open
        # while it is live.  Verify the exact bytes through the authorised file
        # descriptor instead of reopening the child by pathname.
        os.lseek(fd, 0, os.SEEK_SET)
        assert os.read(fd, len(b"HANDLE-RELATIVE")) == b"HANDLE-RELATIVE"
        assert authority.child_info_or_none(child_name) is not None
        assert not (authorised / child_name).exists()
        assert (authorised / "ppa-test.tmp").read_bytes() == before

        # Installation is handle-relative too: even while the lexical pathname
        # names the replacement directory, the open temp is renamed only inside
        # the original authorised directory object.
        authority.rename_fd(fd, "installed.txt", replace=False)
        assert authority.child_info_or_none("installed.txt") is not None
        assert authority.child_info_or_none(child_name) is None
        assert not (authorised / "installed.txt").exists()
        assert (authorised / "ppa-test.tmp").read_bytes() == before

        with pytest.raises(Exception, match="pathname changed|write-authority|directory"):
            authority.verify_pathname()

        # Close the native child before reopening it by pathname; Windows sharing
        # semantics may otherwise reject the second open even though placement is
        # correct.
        os.close(fd)
        fd = -1
        assert (parked / "installed.txt").read_bytes() == b"HANDLE-RELATIVE"
    finally:
        if fd >= 0:
            try:
                authority.delete_fd(fd)
            except Exception:
                pass
            os.close(fd)
        authority.close()
        # Restore test namespace for pytest cleanup.
        if authorised.exists() and not authorised.is_symlink():
            os.rename(authorised, replacement)
        if parked.exists():
            os.rename(parked, authorised)

    assert user_sidecar.read_bytes() == before



@pytest.mark.skipif(os.name != "nt", reason="native Windows handle-relative directory creation")
def test_windows_native_directory_child_creation_cannot_be_redirected_by_parent_substitution(tmp_path: Path) -> None:
    """Stage-directory creation stays in the original root object after pathname substitution."""
    from ppa.secure_write import SecureWriteError, WindowsDirectoryPin, directory_identity

    root = tmp_path / "recovery-preservation"
    root.mkdir()
    parked = tmp_path / "recovery-preservation.parked"
    replacement = tmp_path / "source-library"
    replacement.mkdir()
    user_file = replacement / "user.txt"
    user_file.write_bytes(b"USER-SOURCE")
    before = user_file.read_bytes()

    authority = WindowsDirectoryPin.open(root, expected_identity=directory_identity(root))
    try:
        os.rename(root, parked)
        os.rename(replacement, root)
        # The child is created through the original root handle.  The method may
        # then fail its lexical freshness check, which is correct; what matters is
        # that the replacement/source directory receives no child entry.
        with pytest.raises(SecureWriteError, match="pathname|directory|authority"):
            authority.create_directory_child("00000000-0000-4000-8000-000000000001")
        assert not (root / "00000000-0000-4000-8000-000000000001").exists()
        assert (root / "user.txt").read_bytes() == before
        assert (parked / "00000000-0000-4000-8000-000000000001").is_dir()
    finally:
        authority.close()
        if root.exists():
            os.rename(root, replacement)
        if parked.exists():
            os.rename(parked, root)

    assert user_file.read_bytes() == before
