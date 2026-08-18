"""Content hashing (Phase 2).

SHA-256 is the archive's notion of *content identity*. Two files with the
same SHA-256 are, for our purposes, the same photograph's bytes — whether
they're a move, an exact backup copy, or a redundant download. This is what
turns Phase 1's filename+size *guess* into a fact.

Design notes:
  * Streaming, chunked reads. A 24-year collection contains large TIFFs and
    RAWs; we never load a whole file into memory to hash it.
  * Read-only. Hashing opens the file for reading only and never touches
    mtime/atime semantics beyond what a plain read does (see the safety
    contract — reading bytes is explicitly permitted).
  * The hash is lowercase hex, stored verbatim in files.sha256.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

# 1 MiB. Big enough to keep syscall overhead negligible on large files,
# small enough to stay memory-friendly when many scans run back to back.
_CHUNK_SIZE = 1024 * 1024


def sha256_file(path: Path, chunk_size: int = _CHUNK_SIZE) -> str:
    """Return the lowercase hex SHA-256 of the file at ``path``.

    Reads the file in chunks so memory use is constant regardless of file
    size. Raises OSError (the caller decides whether that means
    "inaccessible" or "corrupt") — this function deliberately does not
    swallow read errors, because a file we cannot hash is a file whose
    identity we cannot vouch for, and that is a fact the caller must record.
    """
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()
