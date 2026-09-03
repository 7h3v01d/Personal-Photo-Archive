# Phase 14.1.3 — Directory-Handle Cleanup Hardening

An adversarial review of Phase 14.1.2 confirmed the descriptor-bound temporary-file
repair and the recovery commit-boundary fixes, then demonstrated the directory-level
analogue of the original TOCTOU defect: rollback/reconciliation verified a stage
pathname and later deleted children by resolving that pathname again.  If the stage
entry was renamed and replaced with a symlink to a source Library between those two
operations, cleanup could delete a source photograph.

Phase 14.1.3 makes destructive directory authority handle-bound.

## BoundDirectory

`ppa.secure_write.BoundDirectory` opens and retains the exact operational directory
object plus its parent directory object.  On POSIX, child inspection/deletion uses
file-descriptor-relative operations:

```text
open exact stage directory
        ↓
fstat directory identity
        ↓
retain stage fd
        ↓
os.stat(name, dir_fd=stage_fd, follow_symlinks=False)
        ↓
os.unlink(name, dir_fd=stage_fd)
```

Cleanup receives validated child **names**, not complete child paths.  The original
stage pathname may subsequently be renamed or replaced; that pathname no longer
supplies child-deletion authority.

The stage's entry in its parent is rechecked only when optionally removing the empty
stage directory itself.  If the entry no longer names the bound directory object,
PPA leaves that operational directory in place for diagnosis.

Where the runtime cannot provide descriptor-relative destructive directory
operations, automatic cleanup/reconciliation fails closed and leaves operational
debris instead of falling back to pathname deletion.

## Converted destructive boundaries

The handle-bound primitive now protects:

- Phase-14.0 rollback cleanup;
- Phase-14.1 rollback cleanup;
- Phase-14.1 donor-orphan reconciliation;
- shared secured-temporary cleanup on POSIX;
- secured-install backup/rollback namespace mutation on POSIX;
- thumbnail failure cleanup within the cache directory.

Donor-orphan reconciliation still retains the Phase-14.1.2 `BEGIN IMMEDIATE`
serialization and checkpoint recheck.  Database writer authority and filesystem
namespace authority are therefore independent and both must succeed.

## Permanent adversarial regressions

The test suite now reproduces the source-destructive directory substitution cases:

- source target named `suspect-source.jpg`, Phase-14.0 failure, stage renamed, stage
  pathname replaced with a symlink to the source Library, rollback cleanup;
- trusted donor named `expected-donor.jpg`, Phase-14.1 failure, the same stage
  substitution during rollback cleanup;
- committed Phase-14 stage with orphan donor artifacts, stage substitution during
  donor-orphan reconciliation.

In every case, source files survive byte-for-byte unchanged.  Destructive cleanup
operates only on the original open operational directory object.

## Authority boundary

Phase 14.1.3 does not add target-replacement authority.  It remains filesystem
hardening beneath Phase 14.0/14.1 operational staging.

It does **not**:

- replace or create the source target;
- copy donor bytes into the source target;
- rename/move/delete source photographs;
- rewrite EXIF or timestamps;
- grant recovery execution authority.

Schema remains **v35**.
