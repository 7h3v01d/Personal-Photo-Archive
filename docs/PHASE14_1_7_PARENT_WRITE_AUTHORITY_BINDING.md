# Phase 14.1.7 — Parent Write-Authority Binding

Phase 14.1.6 satisfied the native Windows/NTFS conditional gate, but adversarial review found a lower shared write-authority gap: higher-level code could validate directory object A, then later `BoundTemporaryFile.create(path)` could securely bind whichever real directory object B occupied that pathname at creation time. No symlink, junction, reparse point or hard link was required. This could redirect normal Phase-14 manifests and `safe_export` into a registered source Library before later re-attestation noticed the stage had changed.

## Authority model

A filesystem write may no longer establish fresh parent authority from a pathname when higher-level code already authorised a directory object. `BoundTemporaryFile.create()` accepts `expected_parent_identity` and the write is permitted only inside that exact parent object.

- POSIX opens `BoundDirectory` with the expected identity and creates the temporary child through the directory descriptor. Child creation, installation and cleanup therefore remain in the exact authorised namespace.
- Windows originally attempted to use native handles opened without `FILE_SHARE_DELETE` to prevent rename/delete substitution while pathname-based creation/install remained active. The native Windows 10/NTFS gate later proved that assumption insufficient: the tested directory rename was still permitted. This Windows mechanism is therefore superseded by Phase 14.1.8 handle-relative namespace operations. The expected-parent identity propagation and POSIX `BoundDirectory` design from this phase remain valid.
- A parent identity mismatch fails before the first output byte or temporary directory entry is created in the substituted directory.

## Bound call sites

The following authority-bearing writers now carry their previously established parent identity into the shared primitive:

- Phase 14.0 suspect-byte preservation temp;
- Phase 14.0 preservation manifest temp;
- Phase 14.1 expected-donor materialisation temp;
- Phase 14.1 donor manifest temp;
- `ppa.safe_export` output temp;
- thumbnail PNG temp and attestation sidecar.

`safe_export` captures the output-parent identity and revalidates the destination before creating a temporary output. Thumbnail writers use the cache-directory identity captured when the cache is constructed.

## Permanent regressions

Phase 14.1.7 includes ordinary-real-directory substitution attacks, not only symlink/junction attacks:

1. Phase 14.0 stage replaced by the registered source Library before suspect-byte temp creation.
2. Phase 14.0 stage replaced before preservation manifest creation.
3. Phase 14.1 stage replaced before expected-donor temp creation.
4. Phase 14.1 stage replaced before donor manifest creation.
5. Validated safe-export parent replaced by the registered source Library before temp creation.
6. Thumbnail cache replaced by a source directory before temp creation.
7. The original native Windows rename-blocking test failed on real Windows 10/NTFS and is retained historically as the evidence that share-mode pinning was not a sufficient primitive. Phase 14.1.8 replaces it with a stronger handle-relative child-creation substitution test.

Every attack requires source files/sidecars to remain byte-for-byte unchanged and the operation to fail rather than re-authorise the substituted directory.

## Authority remains unchanged

Phase 14.1.7 does not add target-replacement authority, source-photo write authority, or Windows orphan deletion authority. The Phase 14.1.5 catalogue-embedded orphan-manifest design and Phase 14.1.6 platform-correct cleanup policy remain unchanged. Catalogue schema remains v36.
