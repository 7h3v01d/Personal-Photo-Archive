# Phase 14.1.6 — Windows Native Gate Corrections

Phase 14.1.5 received adversarial conditional acceptance pending execution of the native NTFS/Windows tests. The first real Windows full-suite run executed 672 tests and exposed twelve failures. Review of the tracebacks showed no reproduced source-write, source-delete, orphan-liveness, manifest-redirection, or junction-authority bypass. The failures were primarily cross-platform test-contract errors: POSIX descriptor-bound cleanup expectations were being asserted on Windows even though the Archive Safety Contract intentionally forbids pathname-based destructive cleanup there.

## Corrections

- Rollback/interruption regressions are now capability-aware. POSIX systems with descriptor-relative directory mutation must clean owned operational debris; Windows must leave uncheckpointed debris untouched and use the verified orphan-forward path when the final donor is adoptable.
- Descriptor-bound directory-swap tests are skipped where descriptor-bound directory mutation is intentionally unavailable. They are POSIX authority tests, not Windows requirements.
- Ambiguous/temp orphan debris on Windows is explicitly expected to require manual intervention; tests no longer demand unsafe automatic deletion.
- Windows junction rejection diagnostics now classify reparse traversal as unsafe before canonical-path containment checks can obscure the reason.
- The wheel regression invokes the declared `setuptools.build_meta` backend directly and offline instead of depending on optional `pip wheel --no-build-isolation` tooling in the developer venv. A local setuptools version below the project's declared `>=68` build requirement is an environment skip.
- The PySide6 competing-identity smoke expectation is corrected from P0 to P1. That fixture supplies coherent current FileRevisions, so the competing current identity is verified rather than blocked as unknown.

## Authority remains unchanged

Phase 14.1.6 grants no new Windows deletion authority, no source-photo write authority, and no target-replacement authority. Windows still fails closed when safe directory-object mutation is unavailable. Missing-manifest orphan adoption still uses the catalogue-embedded canonical manifest introduced in migration 036 and performs no filesystem manifest write.

## Native Windows gate

Run on the exact packaged candidate:

```bat
python -m pytest -q tests/test_recovery_donor_materialization.py tests/test_windows_reparse_hardening.py
python -m pytest -q
```

The three native tests that must execute rather than skip are:

- `test_windows_native_interrupted_donor_can_be_adopted`
- `test_windows_native_stage_junction_substitution_is_rejected`
- `test_windows_native_junction_is_rejected_as_recovery_stage`

A Phase-14.1.x freeze is not claimed until this native gate is green.
