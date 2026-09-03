"""Phase 13.0.1 archive-safe user-directed output regressions."""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from PIL import Image

from ppa.config import Config
from ppa.db import connect
from ppa.safe_export import ArchiveOutputSafetyError, enroll_export_root, safe_export_text
from ppa.scanner import scan_library


def _case(tmp_path: Path):
    library = tmp_path / "library"
    source = library / "source.jpg"
    library.mkdir()
    Image.new("RGB", (40, 30), "red").save(source)
    db = tmp_path / "data" / "catalogue.sqlite3"
    conn = connect(db)
    scan_library(conn, library)
    config = Config(
        db_path=db,
        log_level="INFO",
        log_path=tmp_path / "data" / "logs" / "ppa.log",
        library_directories=[library],
    )
    return conn, config, library, source


def test_export_inside_registered_library_is_rejected_without_source_change(tmp_path: Path) -> None:
    conn, config, library, source = _case(tmp_path)
    before = source.read_bytes()
    with pytest.raises(ArchiveOutputSafetyError, match="source Library"):
        safe_export_text(source, "NOT AN IMAGE", conn=conn, config=config)
    assert source.read_bytes() == before
    with pytest.raises(ArchiveOutputSafetyError, match="source Library"):
        safe_export_text(library / "report.json", "{}", conn=conn, config=config)


def test_export_rejects_hardlink_alias_to_catalogued_source(tmp_path: Path) -> None:
    conn, config, _library, source = _case(tmp_path)
    alias = tmp_path / "outside-source-alias.jpg"
    try:
        os.link(source, alias)
    except (OSError, NotImplementedError):
        pytest.skip("hard links unavailable in this environment")
    before = source.read_bytes()
    with pytest.raises(ArchiveOutputSafetyError, match="filesystem object"):
        safe_export_text(alias, "NOT AN IMAGE", conn=conn, config=config)
    assert source.read_bytes() == before
    assert alias.read_bytes() == before


def test_export_rejects_symlink_alias_into_library(tmp_path: Path) -> None:
    conn, config, _library, source = _case(tmp_path)
    alias = tmp_path / "outside-source-link.json"
    try:
        alias.symlink_to(source)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable in this environment")
    before = source.read_bytes()
    with pytest.raises(ArchiveOutputSafetyError, match="source Library|source File"):
        safe_export_text(alias, "{}", conn=conn, config=config)
    assert source.read_bytes() == before


def test_export_rejects_operational_paths_and_allows_normal_external_destination(tmp_path: Path) -> None:
    conn, config, _library, _source = _case(tmp_path)
    with pytest.raises(ArchiveOutputSafetyError, match="operational"):
        safe_export_text(config.db_path, "bad", conn=conn, config=config)
    with pytest.raises(ArchiveOutputSafetyError, match="operational"):
        safe_export_text(config.db_path.parent / "thumbnails" / "bad.json", "bad", conn=conn, config=config)
    out = safe_export_text(tmp_path / "exports" / "report.json", "{\"ok\": true}\n", conn=conn, config=config)
    assert out.read_text(encoding="utf-8") == '{"ok": true}\n'


def test_export_temp_hardlink_substitution_cannot_receive_source_write(tmp_path: Path, monkeypatch) -> None:
    """Regression for the Phase-14.1 adversarial mkstemp→reopen bypass."""
    conn, config, _library, source = _case(tmp_path)
    before = source.read_bytes()
    import ppa.safe_export as safe_mod

    real_create = safe_mod.BoundTemporaryFile.create

    def substituted_create(parent, *, prefix="ppa.", suffix=".tmp", mode=0o600, **kwargs):
        temp = real_create(parent, prefix=prefix, suffix=suffix, mode=mode, **kwargs)
        try:
            temp.path.unlink()
            os.link(source, temp.path)
        except (OSError, NotImplementedError):
            temp.cleanup()
            pytest.skip("hard-link substitution unavailable in this environment")
        return temp

    monkeypatch.setattr(safe_mod.BoundTemporaryFile, "create", staticmethod(substituted_create))
    with pytest.raises(ArchiveOutputSafetyError, match="temporary|substituted|identity"):
        safe_export_text(tmp_path / "exports" / "report.json", "DESTROY SOURCE", conn=conn, config=config)
    assert source.read_bytes() == before
    with Image.open(source) as image:
        image.verify()


def test_export_temp_symlink_substitution_cannot_receive_source_write(tmp_path: Path, monkeypatch) -> None:
    conn, config, _library, source = _case(tmp_path)
    before = source.read_bytes()
    import ppa.safe_export as safe_mod

    real_create = safe_mod.BoundTemporaryFile.create

    def substituted_create(parent, *, prefix="ppa.", suffix=".tmp", mode=0o600, **kwargs):
        temp = real_create(parent, prefix=prefix, suffix=suffix, mode=mode, **kwargs)
        try:
            temp.path.unlink()
            temp.path.symlink_to(source)
        except (OSError, NotImplementedError):
            temp.cleanup()
            pytest.skip("symlink substitution unavailable in this environment")
        return temp

    monkeypatch.setattr(safe_mod.BoundTemporaryFile, "create", staticmethod(substituted_create))
    with pytest.raises(ArchiveOutputSafetyError, match="temporary|substituted|identity"):
        safe_export_text(tmp_path / "exports" / "report.json", "DESTROY SOURCE", conn=conn, config=config)
    assert source.read_bytes() == before


def test_failed_install_substitution_preserves_existing_export_and_source_bytes(tmp_path: Path, monkeypatch) -> None:
    """A raced final install must preserve every object even when rollback cannot restore names.

    Phase 14.1.16 deliberately prefers recoverable debris over deleting or
    replacing an unexpected object during rollback.
    """
    conn, config, _library, source = _case(tmp_path)
    before = source.read_bytes()
    out = tmp_path / "exports" / "report.json"
    safe_export_text(out, "OLD EXPORT\n", conn=conn, config=config)
    old_bytes = out.read_bytes()

    import ppa.secure_write as sw

    attacked = {"done": False}
    if os.name == "nt":
        real_rename_fd = sw.WindowsDirectoryPin.rename_fd

        def failing_native_install(self, fd, destination_name, *, replace):
            if not attacked["done"] and destination_name == out.name:
                attacked["done"] = True
                raise sw.SecureWriteError("injected Windows secured final-install failure")
            return real_rename_fd(self, fd, destination_name, replace=replace)

        monkeypatch.setattr(sw.WindowsDirectoryPin, "rename_fd", failing_native_install)
    else:
        real_noreplace = sw.BoundDirectory.rename_child_noreplace

        def attacking_noreplace(self, source_name, destination_name):
            if (
                not attacked["done"]
                and destination_name == out.name
                and source_name != out.name
            ):
                attacked["done"] = True
                src_attack = self.path / source_name
                try:
                    src_attack.unlink()
                    os.link(source, src_attack)
                except (OSError, NotImplementedError):
                    pytest.skip("hard-link substitution unavailable in this environment")
            return real_noreplace(self, source_name, destination_name)

        monkeypatch.setattr(sw.BoundDirectory, "rename_child_noreplace", attacking_noreplace)

    with pytest.raises(ArchiveOutputSafetyError, match="install|secured|temporary|restored"):
        safe_export_text(out, "NEW EXPORT\n", conn=conn, config=config)

    assert attacked["done"] is True
    assert source.read_bytes() == before
    if os.name == "nt":
        assert out.read_bytes() == old_bytes
    else:
        # The raced source hard-link is never deleted merely to restore the old
        # pathname; the previous PPA object remains recoverable as rollback debris.
        assert out.read_bytes() == before
        backups = list(out.parent.glob(f".{out.name}.ppa-rollback-*.bak"))
        assert backups
        assert any(candidate.read_bytes() == old_bytes for candidate in backups)


def test_export_parent_real_directory_substitution_cannot_redirect_write(tmp_path: Path, monkeypatch) -> None:
    """Validated export parent A cannot be replaced by a real source Library B before create."""
    conn, config, library, source = _case(tmp_path)
    sidecar = library / "report.txt"
    sidecar.write_text("USER-SIDECAR\n", encoding="utf-8")
    source_before = source.read_bytes()
    sidecar_before = sidecar.read_bytes()
    export_parent = tmp_path / "exports"
    export_parent.mkdir()
    enroll_export_root(export_parent, conn=conn, config=config)
    parked = tmp_path / "exports.parked"

    import ppa.safe_export as safe_mod
    real_create = safe_mod.BoundTemporaryFile.create
    attacked = {"done": False}

    def swapping_create(parent, *args, **kwargs):
        if not attacked["done"]:
            attacked["done"] = True
            os.rename(export_parent, parked)
            os.rename(library, export_parent)
        return real_create(parent, *args, **kwargs)

    monkeypatch.setattr(safe_mod.BoundTemporaryFile, "create", staticmethod(swapping_create))
    try:
        with pytest.raises(ArchiveOutputSafetyError, match="parent|directory|expected|authority|changed"):
            safe_export_text(export_parent / "report.txt", "PPA EXPORT\n", conn=conn, config=config)
    finally:
        if export_parent.exists() and not library.exists():
            os.rename(export_parent, library)
        if parked.exists():
            os.rename(parked, export_parent)

    assert attacked["done"] is True
    assert source.read_bytes() == source_before
    assert sidecar.read_bytes() == sidecar_before
    assert not (export_parent / "report.txt").exists()


def test_export_authority_bootstrap_rejects_library_substitution_before_binding(tmp_path: Path, monkeypatch) -> None:
    """A Library swapped in before parent identity capture must never become output authority."""
    conn, config, library, source = _case(tmp_path)
    export_parent = tmp_path / "exports"
    export_parent.mkdir()
    parked = tmp_path / "exports.parked"
    sidecar = library / "report.txt"
    sidecar.write_text("USER-SIDECAR\n", encoding="utf-8")
    source_before = source.read_bytes()
    sidecar_before = sidecar.read_bytes()

    import ppa.safe_export as safe_mod
    real_ensure = safe_mod.ensure_directory_authority
    attacked = {"done": False}

    def swapping_bootstrap(path, *args, **kwargs):
        if not attacked["done"]:
            attacked["done"] = True
            os.rename(export_parent, parked)
            os.rename(library, export_parent)
        return real_ensure(path, *args, **kwargs)

    monkeypatch.setattr(safe_mod, "ensure_directory_authority", swapping_bootstrap)
    try:
        with pytest.raises(ArchiveOutputSafetyError, match="filesystem object|Library|verified"):
            safe_export_text(export_parent / "report.txt", "PPA EXPORT\n", conn=conn, config=config)
    finally:
        if export_parent.exists() and not library.exists():
            os.rename(export_parent, library)
        if parked.exists():
            os.rename(parked, export_parent)

    assert attacked["done"] is True
    assert source.read_bytes() == source_before
    assert sidecar.read_bytes() == sidecar_before
    assert not (export_parent / "report.txt").exists()



def test_export_moved_library_subdirectory_cannot_become_parent_authority(tmp_path: Path) -> None:
    """Historical source-tree identity blocks a child directory moved outside the Library path."""
    conn, config, library, source = _case(tmp_path)
    source_dir = library / "family"
    source_dir.mkdir()
    sidecar = source_dir / "report.txt"
    sidecar.write_text("USER-SIDECAR\n", encoding="utf-8")
    # Re-scan so the child directory object becomes historical source authority.
    scan_library(conn, library)
    before = sidecar.read_bytes()
    source_before = source.read_bytes()
    before_names = {p.name for p in source_dir.iterdir()}

    export_parent = tmp_path / "exports"
    parked = tmp_path / "exports.parked"
    export_parent.mkdir()
    os.rename(export_parent, parked)
    os.rename(source_dir, export_parent)
    try:
        with pytest.raises(ArchiveOutputSafetyError, match="source Library tree|source-tree"):
            safe_export_text(export_parent / "report.txt", "PPA EXPORT\n", conn=conn, config=config)
    finally:
        os.rename(export_parent, source_dir)
        os.rename(parked, export_parent)

    assert sidecar.read_bytes() == before
    assert source.read_bytes() == source_before
    assert {p.name for p in source_dir.iterdir()} == before_names


def test_export_postscan_unknown_source_child_cannot_replace_enrolled_export_root(tmp_path: Path) -> None:
    """A new unobserved source directory cannot inherit an enrolled export-root pathname."""
    conn, config, library, source = _case(tmp_path)
    export_parent = tmp_path / "exports"; export_parent.mkdir()
    enroll_export_root(export_parent, conn=conn, config=config)
    parked = tmp_path / "exports.parked"; os.rename(export_parent, parked)
    source_dir = library / "new-after-scan"; source_dir.mkdir()
    sidecar = source_dir / "report.txt"; sidecar.write_text("USER-SIDECAR\n", encoding="utf-8")
    before = sidecar.read_bytes(); source_before = source.read_bytes()
    os.rename(source_dir, export_parent)
    try:
        with pytest.raises(ArchiveOutputSafetyError, match="enrolled PPA operational object|replaced"):
            safe_export_text(export_parent / "report.txt", "PPA EXPORT\n", conn=conn, config=config)
    finally:
        os.rename(export_parent, source_dir); os.rename(parked, export_parent)
    assert sidecar.read_bytes() == before
    assert source.read_bytes() == source_before


def test_export_postscan_unknown_source_leaf_cannot_replace_existing_destination(tmp_path: Path) -> None:
    """An arbitrary existing leaf never becomes replaceable merely by occupying an export pathname."""
    conn, config, library, source = _case(tmp_path)
    export_parent = tmp_path / "exports"; export_parent.mkdir()
    enroll_export_root(export_parent, conn=conn, config=config)
    new_source = library / "new-after-scan.txt"; new_source.write_text("SOURCE DATA\n", encoding="utf-8")
    before = new_source.read_bytes(); out = export_parent / "report.txt"
    os.rename(new_source, out)
    try:
        with pytest.raises(ArchiveOutputSafetyError, match="not a positively owned PPA export"):
            safe_export_text(out, "PPA EXPORT\n", conn=conn, config=config)
        assert out.read_bytes() == before
    finally:
        os.rename(out, new_source)
    assert new_source.read_bytes() == before


def test_export_root_creation_provenance_rejects_postscan_source_inserted_after_absence(tmp_path: Path, monkeypatch) -> None:
    """14.1.15: only the secure creator may prove a new export-root object."""
    import os
    import ppa.safe_export as safe_mod

    conn, config, library, source = _case(tmp_path)
    export_parent = tmp_path / "exports"
    assert not export_parent.exists()

    new_source_dir = library / "new-after-scan"
    new_source_dir.mkdir()
    user = new_source_dir / "user.txt"
    user.write_bytes(b"POSTSCAN-SOURCE")
    before = user.read_bytes()
    assert conn.execute(
        "SELECT COUNT(*) FROM library_directory_identities WHERE canonical_path LIKE ?",
        ("%new-after-scan",),
    ).fetchone()[0] == 0

    real_ensure = safe_mod.ensure_directory_authority
    attacked = {"done": False}

    def insert_before_creator(path, *args, **kwargs):
        if not attacked["done"]:
            attacked["done"] = True
            os.rename(new_source_dir, export_parent)
        return real_ensure(path, *args, **kwargs)

    monkeypatch.setattr(safe_mod, "ensure_directory_authority", insert_before_creator)
    try:
        with pytest.raises(ArchiveOutputSafetyError, match="not an enrolled PPA operational object|not created by this secure operation"):
            safe_export_text(export_parent / "report.txt", "PPA EXPORT\n", conn=conn, config=config)
    finally:
        if export_parent.exists() and not new_source_dir.exists():
            os.rename(export_parent, new_source_dir)

    assert attacked["done"] is True
    assert user.read_bytes() == before
    assert {p.name for p in new_source_dir.iterdir()} == {"user.txt"}
    assert conn.execute(
        "SELECT COUNT(*) FROM operational_directories WHERE purpose='export_root'"
    ).fetchone()[0] == 0


def test_export_install_binds_exact_owned_destination_identity(tmp_path: Path, monkeypatch) -> None:
    """14.1.15: a source object substituted after ownership check is never replaced."""
    import os
    import ppa.safe_export as safe_mod

    conn, config, library, _source = _case(tmp_path)
    out = tmp_path / "exports" / "report.txt"
    safe_export_text(out, "FIRST\n", conn=conn, config=config)
    owned_before = out.read_bytes()

    source = library / "new-after-scan.txt"
    source.write_bytes(b"SOURCE-MUST-SURVIVE")
    source_before = source.read_bytes()
    parked_owned = tmp_path / "owned-report.parked"

    real_install = safe_mod.BoundTemporaryFile.install
    attacked = {"done": False}

    def swap_before_install(self, destination, *args, **kwargs):
        destination = Path(destination)
        if destination == out and not attacked["done"]:
            attacked["done"] = True
            os.rename(out, parked_owned)
            os.rename(source, out)
        return real_install(self, destination, *args, **kwargs)

    monkeypatch.setattr(safe_mod.BoundTemporaryFile, "install", swap_before_install)
    try:
        with pytest.raises(ArchiveOutputSafetyError, match="exact positively owned object|destination"):
            safe_export_text(out, "SECOND\n", conn=conn, config=config)
        assert out.read_bytes() == source_before
    finally:
        if out.exists() and not source.exists():
            os.rename(out, source)
        if parked_owned.exists():
            os.rename(parked_owned, out)

    assert attacked["done"] is True
    assert source.read_bytes() == source_before
    assert out.read_bytes() == owned_before


def test_export_ownership_record_uses_installed_identity_not_postinstall_path(tmp_path: Path, monkeypatch) -> None:
    """14.1.15: pathname substitution before DB recording cannot bless source data."""
    import os
    import ppa.safe_export as safe_mod

    conn, config, library, _source = _case(tmp_path)
    out = tmp_path / "exports" / "report.txt"
    source = library / "new-after-scan.txt"
    source.write_bytes(b"SOURCE-IDENTITY")
    source_before = source.read_bytes()
    parked_export = tmp_path / "installed-export.parked"

    real_record = safe_mod.record_owned_file_identity
    attacked = {"done": False}

    def swap_before_record(conn_arg, purpose, path, expected_identity, **kwargs):
        path = Path(path)
        if path == out and not attacked["done"]:
            attacked["done"] = True
            os.rename(out, parked_export)
            os.rename(source, out)
        return real_record(conn_arg, purpose, path, expected_identity, **kwargs)

    monkeypatch.setattr(safe_mod, "record_owned_file_identity", swap_before_record)
    safe_export_text(out, "PPA-INSTALLED\n", conn=conn, config=config)
    assert attacked["done"] is True
    assert out.read_bytes() == source_before
    assert parked_export.read_bytes() == b"PPA-INSTALLED\n"

    row = conn.execute(
        "SELECT fs_device_id,fs_object_id FROM operational_files WHERE purpose='export'"
    ).fetchone()
    parked_st = parked_export.stat()
    source_st = out.stat()
    assert (str(row[0]), str(row[1])) == (str(parked_st.st_dev), str(parked_st.st_ino))
    assert (str(row[0]), str(row[1])) != (str(source_st.st_dev), str(source_st.st_ino))

    with pytest.raises(ArchiveOutputSafetyError, match="positively owned|not PPA-owned"):
        safe_export_text(out, "MUST-NOT-REPLACE\n", conn=conn, config=config)
    assert out.read_bytes() == source_before

    os.rename(out, source)
    os.rename(parked_export, out)
    assert source.read_bytes() == source_before


def test_export_new_destination_race_is_atomic_noreplace(tmp_path: Path, monkeypatch) -> None:
    """14.1.16: a file arriving after the last absence check is never replaced."""
    conn, config, library, _source = _case(tmp_path)
    out = tmp_path / "exports" / "report.txt"
    source = library / "new-after-scan.txt"
    source.write_bytes(b"SOURCE-MUST-SURVIVE")
    source_before = source.read_bytes()

    import ppa.secure_write as sw
    attacked = {"done": False}

    if os.name == "nt":
        real_rename_fd = sw.WindowsDirectoryPin.rename_fd

        def insert_before_native_install(self, fd, destination_name, *, replace):
            if not attacked["done"] and destination_name == out.name:
                attacked["done"] = True
                os.rename(source, out)
            return real_rename_fd(self, fd, destination_name, replace=replace)

        monkeypatch.setattr(sw.WindowsDirectoryPin, "rename_fd", insert_before_native_install)
    else:
        real_noreplace = sw.BoundDirectory.rename_child_noreplace

        def insert_before_atomic_install(self, source_name, destination_name):
            if not attacked["done"] and destination_name == out.name:
                attacked["done"] = True
                os.rename(source, out)
            return real_noreplace(self, source_name, destination_name)

        monkeypatch.setattr(sw.BoundDirectory, "rename_child_noreplace", insert_before_atomic_install)

    with pytest.raises(ArchiveOutputSafetyError, match="destination|install|replace"):
        safe_export_text(out, "PPA EXPORT\n", conn=conn, config=config)

    assert attacked["done"] is True
    assert out.read_bytes() == source_before
    assert not source.exists()  # the test moved it; PPA must leave that object intact at out


def test_export_post_parking_race_is_non_destructive(tmp_path: Path, monkeypatch) -> None:
    """14.1.16: rollback never deletes a source object arriving after parking."""
    conn, config, library, _source = _case(tmp_path)
    out = tmp_path / "exports" / "report.txt"
    safe_export_text(out, "OLD EXPORT\n", conn=conn, config=config)
    old_bytes = out.read_bytes()

    source = library / "new-after-scan.txt"
    source.write_bytes(b"SOURCE-MUST-SURVIVE")
    source_before = source.read_bytes()

    import ppa.secure_write as sw
    attacked = {"done": False}

    if os.name == "nt":
        real_rename_handle = sw.WindowsDirectoryPin.rename_handle

        def insert_after_native_park(self, handle, destination_name, *, replace):
            result = real_rename_handle(self, handle, destination_name, replace=replace)
            if (
                not attacked["done"]
                and destination_name.startswith(f".{out.name}.ppa-rollback-")
            ):
                attacked["done"] = True
                os.rename(source, out)
            return result

        monkeypatch.setattr(sw.WindowsDirectoryPin, "rename_handle", insert_after_native_park)
    else:
        real_noreplace = sw.BoundDirectory.rename_child_noreplace

        def insert_after_posix_park(self, source_name, destination_name):
            result = real_noreplace(self, source_name, destination_name)
            if (
                not attacked["done"]
                and source_name == out.name
                and destination_name.startswith(f".{out.name}.ppa-rollback-")
            ):
                attacked["done"] = True
                os.rename(source, out)
            return result

        monkeypatch.setattr(sw.BoundDirectory, "rename_child_noreplace", insert_after_posix_park)

    with pytest.raises(ArchiveOutputSafetyError, match="destination|restore|rollback|install"):
        safe_export_text(out, "NEW EXPORT\n", conn=conn, config=config)

    assert attacked["done"] is True
    assert out.read_bytes() == source_before
    backups = list(out.parent.glob(f".{out.name}.ppa-rollback-*.bak"))
    assert backups
    assert any(candidate.read_bytes() == old_bytes for candidate in backups)
