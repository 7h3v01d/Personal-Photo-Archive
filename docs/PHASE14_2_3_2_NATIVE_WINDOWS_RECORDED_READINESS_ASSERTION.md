# Phase 14.2.3.2 — Native Windows Recorded-Readiness Assertion Correction

This revision changes no production code. It corrects one regression-harness assertion exposed by native Windows.

When `WindowsDirectoryPin` prevents the attempted Library-root rename, the topology attack never occurs and `record_target_replacement_readiness()` may truthfully return a `RecordedTargetReadiness`. That return object intentionally contains only the recorded checkpoint identifiers/fingerprint/timestamp; `readiness_state` and authority flags live on the immutable database row.

The corrected test therefore validates the returned identifiers/fingerprint and separately queries the immutable row for `readiness_state`, `target_replacement_authorized = 0`, and `recovery_execution_authorized = 0`.

Production code, schema, migrations, and the adversarially accepted Phase 14.2.3 authority model are unchanged.
