# Phase 14.1.17.4.1 — Windows Binary Descriptor Attestation

## Scope

This is a narrow Windows compatibility correction to Phase 14.1.17.4. It does not alter recovery authority, source-tree authority, schema, checkpoint semantics, or the final single-link evidence contract.

## Defect

The Phase 14.1.17.4 final evidence attestation hashes through an opened file descriptor. On Windows, CRT descriptors default to text mode unless `O_BINARY` is supplied. Binary evidence containing CRLF or CTRL-Z bytes can therefore be translated or truncated by `os.read()`, causing a false size/hash mismatch even though the file is unchanged.

The native Windows full suite exposed this as a broad cascade because Phase-14.1 donor tests first construct Phase-14.0 preservation evidence. The common failure was `preservation copy changed before checkpoint commit (size mismatch)`.

## Correction

Final evidence descriptors now use:

```python
os.O_RDONLY | getattr(os, "O_BINARY", 0)
```

with `O_NOFOLLOW` retained where supported. The bytes hashed by `os.read()` are therefore the exact physical evidence bytes on Windows.

## Regression

A native Windows regression writes binary evidence containing both CRLF and CTRL-Z and requires descriptor-bound final attestation to reproduce the exact byte count and SHA-256 without modifying the file.

## Invariants unchanged

- exact filesystem identity is still required;
- evidence must remain a regular non-reparse file;
- `st_nlink == 1` is checked before and after hashing;
- no post-commit recovery-evidence chmod exists;
- schema remains v39.
