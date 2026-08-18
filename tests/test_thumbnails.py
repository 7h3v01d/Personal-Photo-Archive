from pathlib import Path

from PIL import Image

from ppa.thumbnails import ThumbnailCache


def _img(path: Path, size=(800, 600), color="red") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color=color).save(path)


def test_thumbnail_is_generated_and_bounded(tmp_path: Path) -> None:
    src = tmp_path / "big.jpg"
    _img(src, size=(800, 600))
    cache = ThumbnailCache(tmp_path / "cache", size=128)

    out = cache.get_or_create(src, sha256="a" * 64)
    assert out is not None and out.exists()
    with Image.open(out) as im:
        assert max(im.size) <= 128


def test_cache_hit_does_not_regenerate(tmp_path: Path) -> None:
    src = tmp_path / "img.jpg"
    _img(src)
    cache = ThumbnailCache(tmp_path / "cache", size=64)

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
    cache = ThumbnailCache(tmp_path / "cache", size=64)
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

    cache = ThumbnailCache(tmp_path / "cache", size=256)
    out = cache.get_or_create(src, sha256="d" * 64)
    with Image.open(out) as im:
        w, h = im.size
    assert h > w  # transposed to portrait


def test_undecodable_source_returns_none(tmp_path: Path) -> None:
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"not an image")
    cache = ThumbnailCache(tmp_path / "cache")
    assert cache.get_or_create(bad, sha256="e" * 64) is None
