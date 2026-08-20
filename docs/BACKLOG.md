# Backlog — deferred, non-blocking

Recorded from adversarial review at Archive Core Hardening sign-off (3.2.4).
None block Phase 6; they belong to later phases (forensics / Backup & Archive Health).

## 1. Ambiguous restoration among identical missing copies
If two byte-identical Files of one Photo both go missing and one reappears, the
hash proves the *content* returned but not *which* physical File it was. Today we
restore one arbitrarily. Better: model as `REAPPEARED — AMBIGUOUS ORIGIN`, or
create a new File under the same Photo and leave both originals missing —
"don't invent certainty." No evidence is lost either way.

## 2. Hard links look like independent duplicate copies
Two directory entries sharing one inode are reported as two duplicate copies,
but they are not two independently recoverable copies. Future: use device+inode
(POSIX) / file ID (NTFS) to distinguish a true second copy from a hard link.
Relevant to Backup/Archive Health accounting, not provenance.

## 3. Thumbnail vs current-bytes after a hash mismatch
After a verified `hash_mismatch`, the cached thumbnail still shows the trusted
(catalogued) image while the on-disk bytes differ. Health is clearly flagged, so
this isn't silently misleading, but a reconciliation UI could explicitly show
"expected/catalogued image" vs "current untrusted bytes" — a useful forensic view.
