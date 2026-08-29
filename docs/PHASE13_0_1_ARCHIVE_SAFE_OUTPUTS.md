# Phase 13.0.1 — Archive-Safe Outputs & Decision-Order Hardening

Phase 13.0 remains a dry-run recovery planner. The adversarial review found one
critical boundary bypass outside the recovery engine itself: a user could point
`--json` at a source photograph and the generic CLI exporter would overwrite it.
It also found that "latest human disposition" was ordered by wall-clock time
rather than append sequence.

## Archive-safe output boundary

All user-directed JSON/CSV/report exports now use `ppa.safe_export`.

A destination fails closed when it:

- resolves anywhere inside a registered source Library;
- resolves to a catalogued source File through a symlink;
- is an existing hard-link/same-filesystem-object alias of a catalogued source File;
- collides with the catalogue database, its WAL/SHM files, the thumbnail-cache tree,
  or known configured log files.

Writes are staged to a sibling temporary file and atomically installed. The
existing destination inode is never opened for writing. This is a second safety
layer beneath path validation: even an unexpected alias cannot be written through
in place.

The boundary is used by the Phase-13 recovery JSON outputs and the older CLI
JSON/CSV/report exports, diagnostics bundles, organisation reports, pilot review
reports, run transcripts, and durable pilot-session JSON writes.

The intentionally simple user contract is:

> Export outside the referenced photo archive. Never export into a registered
> source Library.

## Append sequence is causal authority

Mismatch-resolution and Verify mismatch-observation ledgers are append-only.
Queries asking for the *latest* decision/observation therefore order by their
monotonic SQLite `id`, not by timestamps.

`resolved_at` and `observed_at` remain audit metadata. They do not decide
causality, so RTC rollback, NTP correction, DST or a manually changed clock cannot
make an older decision supersede a later one.

For recovery planning this means a later `reviewed_unresolved` decision always
supersedes an earlier `retain_expected_recovery_needed` decision even if the later
row carries an earlier wall-clock timestamp.

## Regression boundary

Permanent tests cover:

- actual `ppa recovery-plan --json donor.jpg` refusal with donor bytes unchanged;
- direct export refusal anywhere under a registered Library;
- symlink and hard-link source aliases;
- protected operational paths;
- successful export to a normal external destination;
- clock rollback across two human mismatch dispositions;
- Phase-13 recovery planning dialog signal wiring and Plan recovery button smoke
  coverage when PySide6 is available.

No schema migration is required; the catalogue remains schema v32.
