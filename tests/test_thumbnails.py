from pathlib import Path

from PIL import Image

from ppa.thumbnails import THUMBNAIL_CACHE_MARKER, ThumbnailAuthorityPolicy, ThumbnailCache


_NO_LIBRARIES = ThumbnailAuthorityPolicy(frozenset(), ())


def _cache(path: Path, size: int = 256) -> ThumbnailCache:
    return ThumbnailCache(path, size=size, authority_policy=_NO_LIBRARIES)


def _img(path: Path, size=(800, 600), color="red") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color=color).save(path)


def test_thumbnail_is_generated_and_bounded(tmp_path: Path) -> None:
    src = tmp_path / "big.jpg"
    _img(src, size=(800, 600))
    cache = _cache(tmp_path / "cache", size=128)

    out = cache.get_or_create(src, sha256="a" * 64)
    assert out is not None and out.exists()
    with Image.open(out) as im:
        assert max(im.size) <= 128


def test_cache_hit_does_not_regenerate(tmp_path: Path) -> None:
    src = tmp_path / "img.jpg"
    _img(src)
    cache = _cache(tmp_path / "cache", size=64)

    out1 = cache.get_or_create(src, sha256="b" * 64)
    # Overwrite the cached file with a sentinel; a regeneration would clobber it.
    out1.write_bytes(b"SENTINEL")
    out2 = cache.get_or_create(src, sha256="b" * 64)
    assert out1 == out2
    assert out2.read_bytes() == b"SENTINEL"  # untouched -> served from cache


def test_identical_content_shares_thumbnail_key(tmp_path: Path) -> None:
    a = tmp_path / "a.jpg"
    b = tmp_path / "b.jpg"
    _img(a)
    _img(b)
    cache = _cache(tmp_path / "cache", size=64)
    # Same sha256 (as Phase 2 would assign to byte-identical files) -> one file.
    pa = cache.get_or_create(a, sha256="c" * 64)
    pb = cache.get_or_create(b, sha256="c" * 64)
    assert pa == pb


def test_orientation_is_applied(tmp_path: Path) -> None:
    # A 200x100 image tagged as rotated 90deg should come out portrait-ish.
    src = tmp_path / "rot.jpg"
    img = Image.new("RGB", (200, 100), "green")
    exif = img.getexif()
    exif[274] = 6  # Orientation: rotate 90 CW
    img.save(src, exif=exif)

    cache = _cache(tmp_path / "cache", size=256)
    out = cache.get_or_create(src, sha256="d" * 64)
    with Image.open(out) as im:
        w, h = im.size
    assert h > w  # transposed to portrait


def test_undecodable_source_returns_none(tmp_path: Path) -> None:
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"not an image")
    cache = _cache(tmp_path / "cache")
    assert cache.get_or_create(bad, sha256="e" * 64) is None


def test_attested_thumbnail_requires_matching_source_hash(tmp_path: Path) -> None:
    from ppa.hashing import sha256_file

    src = tmp_path / "img.jpg"
    _img(src, color="red")
    expected = sha256_file(src)
    cache = _cache(tmp_path / "cache", size=96)

    out = cache.get_or_create_attested(src, expected)
    assert out is not None
    assert cache.attested_cached_path(src, expected) == out

    # A source that no longer has the expected bytes cannot create/refresh an
    # attested derivative under the old catalogue identity.
    _img(src, color="blue")
    out.unlink()
    cache._attestation_path(out).unlink()
    assert cache.get_or_create_attested(src, expected) is None
    assert cache.cached_path_only(src, expected) is None


def test_attested_thumbnail_detects_derivative_tampering(tmp_path: Path) -> None:
    from ppa.hashing import sha256_file

    src = tmp_path / "img.jpg"
    _img(src)
    expected = sha256_file(src)
    cache = _cache(tmp_path / "cache", size=96)
    out = cache.get_or_create_attested(src, expected)
    assert out is not None

    out.write_bytes(b"tampered derivative")
    assert cache.attested_cached_path(src, expected) is None


def test_thumbnail_temp_hardlink_substitution_cannot_rewrite_source(tmp_path: Path, monkeypatch) -> None:
    """Thumbnail generation writes through a descriptor, never a predictable temp path."""
    import os
    import pytest
    import ppa.thumbnails as thumbs_mod

    src = tmp_path / "source.jpg"
    _img(src, color="purple")
    before = src.read_bytes()
    cache = _cache(tmp_path / "cache", size=64)
    real_create = thumbs_mod.BoundTemporaryFile.create

    def substituted_create(parent, *, prefix="ppa.", suffix=".tmp", mode=0o600, **kwargs):
        temp = real_create(parent, prefix=prefix, suffix=suffix, mode=mode, **kwargs)
        try:
            temp.path.unlink()
            os.link(src, temp.path)
        except (OSError, NotImplementedError):
            temp.cleanup()
            pytest.skip("hard-link substitution unavailable in this environment")
        return temp

    monkeypatch.setattr(thumbs_mod.BoundTemporaryFile, "create", staticmethod(substituted_create))
    assert cache.get_or_create(src, sha256="f" * 64) is None
    assert src.read_bytes() == before
    with Image.open(src) as image:
        image.verify()


def test_thumbnail_cache_real_directory_substitution_cannot_redirect_write(tmp_path: Path, monkeypatch) -> None:
    """The cache identity captured at construction must be the writer's parent authority."""
    import os
    import ppa.thumbnails as thumbs_mod

    library = tmp_path / "library"
    library.mkdir()
    src = library / "source.jpg"
    _img(src, color="navy")
    sidecar = library / "user.png"
    sidecar.write_bytes(b"USER-CACHE-COLLISION")
    source_before = src.read_bytes()
    sidecar_before = sidecar.read_bytes()

    cache_dir = tmp_path / "cache"
    cache = _cache(cache_dir, size=64)
    parked = tmp_path / "cache.parked"
    real_create = thumbs_mod.BoundTemporaryFile.create
    attacked = {"done": False}

    def swapping_create(parent, *args, **kwargs):
        if not attacked["done"]:
            attacked["done"] = True
            os.rename(cache_dir, parked)
            os.rename(library, cache_dir)
        return real_create(parent, *args, **kwargs)

    monkeypatch.setattr(thumbs_mod.BoundTemporaryFile, "create", staticmethod(swapping_create))
    try:
        assert cache.get_or_create(src, sha256="a" * 64) is None
    finally:
        if cache_dir.exists() and not library.exists():
            os.rename(cache_dir, library)
        if parked.exists():
            os.rename(parked, cache_dir)

    assert attacked["done"] is True
    assert src.read_bytes() == source_before
    assert sidecar.read_bytes() == sidecar_before


def _registered_library_case(tmp_path: Path, *, with_photo: bool = True):
    from ppa.db import connect
    from ppa.scanner import scan_library

    library = tmp_path / "library"
    library.mkdir()
    photo = None
    if with_photo:
        photo = library / "source.jpg"
        _img(photo, color="orange")
    conn = connect(tmp_path / "catalogue.sqlite3")
    scan_library(conn, library)
    row = conn.execute(
        "SELECT root_fs_device_id,root_fs_object_id FROM libraries"
    ).fetchone()
    assert row[0] is not None and row[1] is not None
    return conn, library, photo


def test_thumbnail_cache_requires_explicit_library_authority_context(tmp_path: Path) -> None:
    import pytest

    cache_dir = tmp_path / "cache"
    with pytest.raises(ValueError, match="registered-Library authority context"):
        ThumbnailCache(cache_dir, size=64)
    assert not cache_dir.exists()


def test_thumbnail_cache_bootstrap_rejects_library_substitution_before_authority(tmp_path: Path, monkeypatch) -> None:
    """A registered Library swapped onto cache pathname is rejected by object identity."""
    import os
    import pytest
    import ppa.thumbnails as thumbs_mod

    conn, library, src = _registered_library_case(tmp_path, with_photo=True)
    assert src is not None
    user = library / "user.txt"
    user.write_bytes(b"USER-DATA")
    before_names = {p.name for p in library.iterdir()}
    source_before = src.read_bytes()
    user_before = user.read_bytes()

    cache_dir = tmp_path / "cache"
    parked = tmp_path / "cache.parked"
    real_ensure = thumbs_mod.ensure_directory_authority
    attacked = {"done": False}

    def swapping_bootstrap(path, *args, **kwargs):
        if not attacked["done"]:
            attacked["done"] = True
            cache_dir.mkdir()
            os.rename(cache_dir, parked)
            os.rename(library, cache_dir)
        return real_ensure(path, *args, **kwargs)

    monkeypatch.setattr(thumbs_mod, "ensure_directory_authority", swapping_bootstrap)
    try:
        with pytest.raises(ValueError, match="registered source Library|authority"):
            ThumbnailCache(cache_dir, size=64, conn=conn)
    finally:
        if cache_dir.exists() and not library.exists():
            os.rename(cache_dir, library)
        if parked.exists():
            os.rename(parked, cache_dir)

    assert attacked["done"] is True
    assert src.read_bytes() == source_before
    assert user.read_bytes() == user_before
    assert {p.name for p in library.iterdir()} == before_names


def test_thumbnail_cache_path_inside_registered_library_is_rejected_before_creation(tmp_path: Path) -> None:
    """A missing cache child inside a registered Library is never created."""
    import pytest

    conn, library, _ = _registered_library_case(tmp_path, with_photo=False)
    cache_dir = library / "derived" / "thumbnails"
    with pytest.raises(ValueError, match="registered source Library"):
        ThumbnailCache(cache_dir, size=64, conn=conn)
    assert not (library / "derived").exists()
    assert list(library.iterdir()) == []


def test_thumbnail_cache_empty_registered_library_cannot_receive_marker(tmp_path: Path) -> None:
    """An empty registered Library is not authorised merely because it looks cache-clean."""
    import os
    import pytest

    conn, library, _ = _registered_library_case(tmp_path, with_photo=False)
    assert list(library.iterdir()) == []
    cache_dir = tmp_path / "cache"
    parked = tmp_path / "cache.parked"
    cache_dir.mkdir()
    os.rename(cache_dir, parked)
    os.rename(library, cache_dir)
    try:
        with pytest.raises(ValueError, match="registered source Library"):
            ThumbnailCache(cache_dir, size=64, conn=conn)
    finally:
        os.rename(cache_dir, library)
        os.rename(parked, cache_dir)
    assert list(library.iterdir()) == []


def test_thumbnail_cache_cache_shaped_catalogued_photo_cannot_be_overwritten(tmp_path: Path) -> None:
    """Cache-shaped user filenames are data, never authority credentials."""
    import os
    import pytest
    from ppa.db import connect
    from ppa.hashing import sha256_file
    from ppa.scanner import scan_library

    external = tmp_path / "external.jpg"
    _img(external, color="blue")
    expected_sha = sha256_file(external)

    library = tmp_path / "library"
    library.mkdir()
    victim = library / f"{expected_sha}-64.png"
    _img(victim, size=(40, 30), color="green")
    victim_before = victim.read_bytes()
    victim_sha_before = sha256_file(victim)

    conn = connect(tmp_path / "catalogue.sqlite3")
    scan_library(conn, library)
    assert conn.execute("SELECT COUNT(*) FROM files WHERE path=?", (str(victim),)).fetchone()[0] == 1

    cache_dir = tmp_path / "cache"
    parked = tmp_path / "cache.parked"
    cache_dir.mkdir()
    os.rename(cache_dir, parked)
    os.rename(library, cache_dir)
    try:
        with pytest.raises(ValueError, match="registered source Library"):
            cache = ThumbnailCache(cache_dir, size=64, conn=conn)
            cache.get_or_create_attested(external, expected_sha)
    finally:
        os.rename(cache_dir, library)
        os.rename(parked, cache_dir)

    assert victim.read_bytes() == victim_before
    assert sha256_file(victim) == victim_sha_before



def test_thumbnail_cache_moved_library_subdirectory_cannot_become_authority(tmp_path: Path) -> None:
    """A catalogued source subdirectory remains source authority after rename."""
    import os
    import pytest
    from ppa.db import connect
    from ppa.hashing import sha256_file
    from ppa.scanner import scan_library

    external = tmp_path / "external.jpg"
    _img(external, color="blue")
    expected_sha = sha256_file(external)

    library = tmp_path / "library"
    source_dir = library / "family-photos"
    source_dir.mkdir(parents=True)
    victim = source_dir / f"{expected_sha}-64.png"
    _img(victim, size=(40, 30), color="green")
    before_bytes = victim.read_bytes()
    before_sha = sha256_file(victim)
    before_names = {p.name for p in source_dir.iterdir()}

    conn = connect(tmp_path / "catalogue.sqlite3")
    scan_library(conn, library)
    assert conn.execute("SELECT COUNT(*) FROM files WHERE path=?", (str(victim),)).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM library_directory_identities WHERE canonical_path=?",
        (os.path.normcase(os.path.realpath(str(source_dir))),),
    ).fetchone()[0] == 1

    cache_dir = tmp_path / "cache"
    parked = tmp_path / "cache.parked"
    cache_dir.mkdir()
    os.rename(cache_dir, parked)
    os.rename(source_dir, cache_dir)
    try:
        with pytest.raises(ValueError, match="source Library tree|source-tree"):
            ThumbnailCache(cache_dir, size=64, conn=conn)
    finally:
        os.rename(cache_dir, source_dir)
        os.rename(parked, cache_dir)

    assert victim.read_bytes() == before_bytes
    assert sha256_file(victim) == before_sha
    assert {p.name for p in source_dir.iterdir()} == before_names
    assert not (source_dir / THUMBNAIL_CACHE_MARKER).exists()
    assert not (source_dir / f"{expected_sha}-64.png.attestation.json").exists()


def test_thumbnail_cache_moved_empty_library_subdirectory_is_rejected(tmp_path: Path) -> None:
    """An empty observed source directory is source authority independent of contents."""
    import os
    import pytest
    from ppa.db import connect
    from ppa.scanner import scan_library

    library = tmp_path / "library"
    source_dir = library / "empty-folder"
    source_dir.mkdir(parents=True)
    conn = connect(tmp_path / "catalogue.sqlite3")
    scan_library(conn, library)
    before_names = list(source_dir.iterdir())

    cache_dir = tmp_path / "cache"
    parked = tmp_path / "cache.parked"
    cache_dir.mkdir()
    os.rename(cache_dir, parked)
    os.rename(source_dir, cache_dir)
    try:
        with pytest.raises(ValueError, match="source Library tree|source-tree"):
            ThumbnailCache(cache_dir, size=64, conn=conn)
    finally:
        os.rename(cache_dir, source_dir)
        os.rename(parked, cache_dir)

    assert list(source_dir.iterdir()) == before_names == []


def test_thumbnail_postscan_unknown_source_child_cannot_replace_enrolled_cache(tmp_path: Path) -> None:
    """A source directory created after the last scan cannot inherit cache ownership."""
    import os
    import pytest
    from ppa.db import connect
    from ppa.hashing import sha256_file
    from ppa.scanner import scan_library

    library = tmp_path / "library"; library.mkdir()
    conn = connect(tmp_path / "catalogue.sqlite3"); scan_library(conn, library)
    external = tmp_path / "external.jpg"; _img(external, color="blue")
    expected_sha = sha256_file(external)
    cache_dir = tmp_path / "cache"
    ThumbnailCache(cache_dir, size=64, conn=conn)  # PPA creates + enrolls exact cache object.
    parked = tmp_path / "cache.parked"; os.rename(cache_dir, parked)

    source_dir = library / "new-after-scan"; source_dir.mkdir()
    victim = source_dir / f"{expected_sha}-64.png"; _img(victim, color="green")
    before = victim.read_bytes(); before_names = {p.name for p in source_dir.iterdir()}
    assert conn.execute("SELECT COUNT(*) FROM library_directory_identities WHERE canonical_path LIKE ?", ("%new-after-scan",)).fetchone()[0] == 0
    os.rename(source_dir, cache_dir)
    try:
        with pytest.raises(ValueError, match="enrolled PPA operational object|moved or was replaced"):
            ThumbnailCache(cache_dir, size=64, conn=conn)
    finally:
        os.rename(cache_dir, source_dir); os.rename(parked, cache_dir)
    assert victim.read_bytes() == before
    assert {p.name for p in source_dir.iterdir()} == before_names
    assert not (source_dir / THUMBNAIL_CACHE_MARKER).exists()
    assert not (source_dir / f"{expected_sha}-64.png.attestation.json").exists()


def test_thumbnail_creation_provenance_rejects_postscan_source_inserted_after_absence(tmp_path: Path, monkeypatch) -> None:
    """14.1.15: stale pathname absence cannot bless a post-scan source directory."""
    import os
    import pytest
    import ppa.thumbnails as thumbs_mod

    conn, library, _ = _registered_library_case(tmp_path, with_photo=False)
    cache_dir = tmp_path / "cache"
    new_source_dir = library / "new-after-scan"
    new_source_dir.mkdir()
    user = new_source_dir / "user.txt"
    user.write_bytes(b"POSTSCAN-SOURCE")
    before = user.read_bytes()
    real_ensure = thumbs_mod.ensure_directory_authority
    attacked = {"done": False}

    def insert_before_creator(path, *args, **kwargs):
        if not attacked["done"]:
            attacked["done"] = True
            os.rename(new_source_dir, cache_dir)
        return real_ensure(path, *args, **kwargs)

    monkeypatch.setattr(thumbs_mod, "ensure_directory_authority", insert_before_creator)
    try:
        with pytest.raises(ValueError, match="not an enrolled PPA operational object|not created by this secure operation"):
            ThumbnailCache(cache_dir, size=64, conn=conn)
    finally:
        if cache_dir.exists() and not new_source_dir.exists():
            os.rename(cache_dir, new_source_dir)

    assert attacked["done"] is True
    assert user.read_bytes() == before
    assert {p.name for p in new_source_dir.iterdir()} == {"user.txt"}
    assert conn.execute(
        "SELECT COUNT(*) FROM operational_directories WHERE purpose='thumbnail_cache'"
    ).fetchone()[0] == 0


def test_thumbnail_unowned_child_in_enrolled_cache_is_never_replaced(tmp_path: Path) -> None:
    """14.1.15: an enrolled cache root does not confer ownership of arbitrary children."""
    import os
    from ppa.hashing import sha256_file

    conn, library, _ = _registered_library_case(tmp_path, with_photo=False)
    external = tmp_path / "external.jpg"
    _img(external, color="blue")
    expected_sha = sha256_file(external)

    cache_dir = tmp_path / "cache"
    cache = ThumbnailCache(cache_dir, size=64, conn=conn)

    source_child = library / "new-after-scan.png"
    _img(source_child, size=(41, 31), color="green")
    before = source_child.read_bytes()
    cache_name = cache_dir / f"{expected_sha}-64.png"
    os.rename(source_child, cache_name)
    try:
        assert cache.get_or_create_attested(external, expected_sha) is None
        assert cache_name.read_bytes() == before
        assert not (cache_dir / f"{expected_sha}-64.png.attestation.json").exists()
        assert conn.execute(
            "SELECT COUNT(*) FROM operational_files WHERE purpose='thumbnail_cache_child'"
        ).fetchone()[0] == 0
    finally:
        if cache_name.exists() and not source_child.exists():
            os.rename(cache_name, source_child)

    assert source_child.read_bytes() == before


def test_thumbnail_post_parking_race_preserves_source_child(tmp_path: Path, monkeypatch) -> None:
    """14.1.16: shared installer rollback never deletes an arriving source child."""
    import os
    import pytest
    import ppa.secure_write as sw
    from ppa.hashing import sha256_file

    conn, library, photo = _registered_library_case(tmp_path, with_photo=True)
    assert photo is not None
    expected_sha = sha256_file(photo)
    cache_dir = tmp_path / "cache"
    cache = ThumbnailCache(cache_dir, size=64, conn=conn)

    out = cache.get_or_create_attested(photo, expected_sha)
    assert out is not None
    old_thumb = out.read_bytes()
    att = out.with_name(out.name + ".attestation.json")
    assert att.exists()
    att.unlink()  # force a legitimate regeneration/replacement attempt

    source_child = library / "new-after-scan.png"
    _img(source_child, size=(43, 37), color="green")
    source_before = source_child.read_bytes()
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
                os.rename(source_child, out)
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
                os.rename(source_child, out)
            return result

        monkeypatch.setattr(sw.BoundDirectory, "rename_child_noreplace", insert_after_posix_park)

    assert cache.get_or_create_attested(photo, expected_sha) is None
    assert attacked["done"] is True
    assert out.read_bytes() == source_before
    backups = list(cache_dir.glob(f".{out.name}.ppa-rollback-*.bak"))
    assert backups
    assert any(candidate.read_bytes() == old_thumb for candidate in backups)
    assert not att.exists()


def test_unverified_library_authority_is_distinct_and_creates_no_cache(tmp_path: Path) -> None:
    import pytest
    from ppa.db import connect
    from ppa.thumbnails import ThumbnailAuthorityUnavailable

    library = tmp_path / "library"
    library.mkdir()
    conn = connect(tmp_path / "catalogue.sqlite3")
    conn.execute(
        "INSERT INTO libraries (root_display_path, root_canonical_path) VALUES (?, ?)",
        (str(library), str(library.resolve())),
    )
    conn.commit()
    cache_dir = tmp_path / "thumbnails"

    with pytest.raises(ThumbnailAuthorityUnavailable, match="rescan"):
        ThumbnailCache(cache_dir, size=64, conn=conn)

    assert not cache_dir.exists()
    conn.close()
