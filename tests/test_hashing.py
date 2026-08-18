import io
from pathlib import Path

import pytest

from ppa.hashing import sha256_file


def test_sha256_matches_known_vector(tmp_path: Path) -> None:
    # sha256("") is a well-known constant.
    empty = tmp_path / "empty.bin"
    empty.write_bytes(b"")
    assert (
        sha256_file(empty)
        == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )


def test_sha256_is_content_addressed(tmp_path: Path) -> None:
    a = tmp_path / "a.bin"
    b = tmp_path / "b.bin"
    a.write_bytes(b"identical bytes")
    b.write_bytes(b"identical bytes")
    assert sha256_file(a) == sha256_file(b)

    b.write_bytes(b"different bytes!")
    assert sha256_file(a) != sha256_file(b)


def test_chunking_does_not_change_result(tmp_path: Path) -> None:
    payload = bytes(range(256)) * 5000  # ~1.28 MB, crosses default chunk size
    f = tmp_path / "big.bin"
    f.write_bytes(payload)
    assert sha256_file(f, chunk_size=7) == sha256_file(f, chunk_size=1024 * 1024)


def test_unreadable_path_raises(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        sha256_file(tmp_path / "does-not-exist.bin")
