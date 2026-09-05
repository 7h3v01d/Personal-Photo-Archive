# Phase 14.3 — Target-Replacement Execution

## Status

**Active implementation / adversarial review required.**

Phase 14.3 is the first recovery boundary allowed to intentionally mutate the
registered source-photo namespace. It therefore does **not** inherit execution
authority from Phase 14.2 merely because a readiness row exists. Every attempt
starts from one immutable recorded Phase-14.2 checkpoint, rebuilds that complete
readiness chain, produces a non-authoritative preview, and requires one exact
plan-derived confirmation before a durable one-attempt execution intent exists.

The desktop UI withheld Phase-14.3 execution until adversarial acceptance. Phase 14.3 is now frozen at 14.3.5 after the native Windows/NTFS gate; Phase 14.4 may expose this exact backend without changing its authority model.
The initial execution surface is CLI-only.

## Non-negotiable authority sequence

```text
recorded immutable Phase-14.2 readiness
        ↓
fresh complete Phase-14.2 rebuild
        ↓
non-authoritative Phase-14.3 preview
        ↓
previewed execution UUID + exact confirmation phrase
        ↓
BEGIN IMMEDIATE
        ↓
rebuild exact preview again
        ↓
commit immutable one-attempt execution intent
        ↓
BEGIN IMMEDIATE
        ↓
fresh readiness/execution revalidation
        ↓
physical recovery operation
        ↓
post-placement exact-object verification
        ↓
immutable execution result
        ↓
COMMIT
```

The attempt is committed **before** filesystem mutation. If the process dies
between authorization and result checkpoint, the catalogue therefore contains an
unresolved durable attempt instead of silently allowing the same readiness to be
replayed. A readiness checkpoint and execution UUID are both one-shot.

## Preview is still zero authority

`RecoveryTargetExecutionPlan` always carries:

```text
target_replacement_authorized = false
recovery_execution_authorized = false
```

The preview's confirmation phrase is derived from the exact execution UUID and
execution-plan fingerprint. CLI preview prints both. `--apply` must repeat the
same `--execution-id` and the exact displayed `--confirm` phrase. Generating a
fresh hidden plan at apply time is not permitted.

## Schema v41 — immutable execution audit

Migration 041 adds two append-only tables.

### `archive_recovery_target_execution_attempts`

This is the durable one-attempt authorization record. It binds:

- execution/readiness/materialization IDs;
- target File/Library/expected revision/SHA;
- current human recovery-intent resolution;
- replacement mode and target path;
- exact reviewed target state/object/link topology;
- exact registered Library-root identity;
- exact target-parent identity;
- materialized donor path/SHA/size;
- Phase-14.2 evidence fingerprint;
- Phase-14.3 execution-plan fingerprint;
- SHA-256 of the exact confirmation phrase;
- `authorization_state='confirmed_one_attempt'`;
- one-valued target-replacement and recovery-execution authority flags.

Attempt rows cannot be updated or deleted.

### `archive_recovery_target_execution_results`

A separate immutable result row is written only when PPA can truthfully prove one
of these finite outcomes:

- `expected_target_placed_verified`;
- `aborted_before_target_transition`;
- `aborted_exact_target_restored`.

An attempt without a result is intentionally **unresolved** and blocks replay.
The read-only execution-status surface exposes that state but does not fabricate
success or authorize a retry.

## Missing-target restore

For `restore_missing_recorded_target`, Phase 14.3 supports reviewed local
filesystems where the hardened bound-directory and atomic no-replace primitives
are available.

The execution path:

1. binds the exact registered Library root;
2. binds the exact target parent;
3. creates a secured temporary child inside that exact parent object;
4. copies the committed materialized donor while descriptor-hashing the source;
5. hashes the secured temporary itself;
6. installs it with atomic **no-replace** semantics;
7. descriptor-opens the installed pathname and proves it is the exact temporary
   object and exact expected SHA/size with `st_nlink == 1`;
8. re-verifies root and parent pathname freshness;
9. records the immutable success checkpoint.

A file arriving at the target pathname at any point before installation wins.
PPA does not overwrite it. The attempt is recorded as aborted before target
transition when that no-replace refusal is provable.

### Target-transition provenance

A generic secure-write exception is not evidence that the target namespace was
untouched. Phase 14.3.3 distinguishes the atomic no-replace acquisition itself
from every later durability and identity step. If the atomic target-name
acquisition succeeds, any subsequent failure leaves the durable execution
attempt **unresolved** and writes no execution-result row. Only a positively
proven no-acquisition outcome (for example `EEXIST` from the no-replace syscall)
may be recorded as `aborted_before_target_transition`. See
`PHASE14_3_3_TARGET_TRANSITION_PROVENANCE.md`.

Known remote/network mount semantics are refused. Windows execution additionally
requires a local fixed NTFS Library for this first source-mutating phase.

## Existing-target replacement — native Windows only

Phase 14.3 deliberately does **not** claim general POSIX existing-object rename
authority. POSIX can atomically acquire a new name with no-replace, but the
project does not currently have a general primitive equivalent to "rename this
exact already-open inode and no substituted pathname object". Existing-target
replacement therefore fails closed outside native Windows.

On Windows/NTFS, the already-hardened `WindowsDirectoryPin` provides the required
handle-relative namespace authority.

```text
bind exact registered Library root
        ↓
bind exact target parent
        ↓
open exact target child relative to parent with READ + DELETE authority
        ↓
hash THAT SAME open handle
        ↓
prove reviewed SHA / size / mtime / identity / nlink == 1
        ↓
rename THAT SAME handle → deterministic hidden .suspect child, no replace
        ↓
re-hash same parked handle
        ↓
copy + hash expected donor into secured temporary
        ↓
atomic no-replace install at target name
        ↓
descriptor-hash installed exact object
        ↓
re-hash retained suspect handle
        ↓
reverify root + parent pathname freshness
        ↓
commit immutable result
```

The displaced suspect is **retained**, not deleted. Its deterministic name is:

```text
.<original-name>.ppa-recovery-<execution-uuid>.suspect
```

The `.suspect` suffix also prevents the retained object from masquerading as an
ordinary source image solely through its original extension.

## Rollback / interruption rule

Automatic rollback is intentionally narrow.

If Windows has parked the exact suspect but donor preparation/installation fails
**before** expected bytes are installed, PPA may restore the parked object only
when all of the following remain true immediately before the reverse rename:

- the same still-open handle has the same exact filesystem identity;
- a freshly opened child handle, resolved relative to the still-pinned target parent, names that same exact object;
- both handle views independently reproduce the reviewed suspect SHA-256 and size;
- mtime is unchanged in both views;
- `st_nlink == 1` in both views;
- the target pathname remains absent after both proofs.

The reverse rename is then performed through the original exact handle with
no-replace semantics.  This dual-view rule is intentional: rollback authority
does not rely solely on the descriptor that survived the forward parking rename.

If the parked object changed, gained an alias, or the target pathname became
occupied, PPA does **not** restore it for liveness. The object remains parked and
the durable attempt remains unresolved for manual review.

If expected bytes were already installed but final verification/result commit
cannot be proven, automatic destructive rollback is not attempted. The durable
attempt remains unresolved.

A hard process crash can always occur outside Python cleanup. That is why the
one-attempt authorization is committed before mutation and why unresolved
attempts block replay.

## Verify owns catalogue health reconciliation

Even after `expected_target_placed_verified`, Phase 14.3 does not set the File to
healthy and does not rewrite revision authority. The result carries:

```text
verify_reconciliation_required = 1
```

The ordinary `Verify` engine must independently observe the recovered target and
perform the existing catalogue reconciliation. Recovery code therefore does not
certify its own write.

## CLI

Preview:

```text
python -m ppa.cli recovery-target-execution <readiness-id>
```

The preview prints an execution UUID and exact confirmation phrase.

Apply the **same** preview:

```text
python -m ppa.cli recovery-target-execution <readiness-id> \
    --execution-id <previewed-execution-uuid> \
    --apply \
    --confirm "<exact phrase printed by preview>"
```

Optional:

```text
--note "review note"
--json execution.json
```

Read-only durable-attempt inspection:

```text
python -m ppa.cli recovery-target-execution-status <execution-id>
```

## Permanent regression boundary

Phase 14.3 tests permanently cover:

1. schema v41 and explicit execution-authority/result constraints;
2. preview read-only behavior and zero authority;
3. wrong confirmation creates no execution intent or source mutation;
4. missing-target restore places exact expected bytes;
5. successful recovery deliberately remains `hash_mismatch` until Verify;
6. attempt and result ledgers reject UPDATE and DELETE;
7. successful readiness/execution identities cannot be replayed;
8. interrupted attempt remains durable/unresolved and blocks retry;
9. target changes after preview fail before durable authority is created;
10. donor evidence changes after preview fail before durable authority is created;
11. a late-arriving missing-target occupant survives atomic no-replace install;
12. durable status accurately distinguishes resolved from unresolved attempts;
13. malformed "success" result rows are rejected by schema constraints;
14. native Windows existing-target execution retains the exact suspect;
15. native Windows in-place target edits after preview are rejected;
16. native Windows pre-install failure restores only the exact unchanged suspect;
17. a changed parked Windows suspect is not restored merely for liveness;
18. a late Windows target occupant is never overwritten and leaves the suspect parked;
19. a Windows target hard-link alias invalidates execution before authority is created;
20. post-acquisition parent-directory fsync failure leaves the attempt unresolved;
21. post-install exact-object verification failure leaves the attempt unresolved;
22. reverse-rename success followed by failed post-restore byte proof leaves the attempt unresolved;
23. native Windows mutation after both pre-restore proofs but before reverse rename cannot produce `aborted_exact_target_restored`.

(Windows-only cases are platform skips on non-Windows CI.)

## Adversarial-review focus

Phase 14.3 should not be frozen merely because ordinary regression tests pass.
Review should concentrate on:

- confirmation/plan substitution and replay;
- crash points between attempt commit, target parking, donor install and result commit;
- same-handle Windows target attestation;
- late target occupancy;
- retained-suspect alias/content changes;
- rollback exact-object authority;
- root/parent substitution while source mutation is in progress;
- donor substitution while copied;
- post-placement target substitution/aliasing;
- unsupported/network filesystem refusal;
- ensuring Verify remains the only health-reconciliation owner.

**Phase 14.3 is adversarially ACCEPTED/FROZEN at 14.3.5.** The native Windows/NTFS freeze gate completed at 752 passed / 13 skipped / 0 failed. Later UI integration must call this frozen backend rather than reimplement its authority logic.


## Phase 14.3.5 rollback finalization

Windows suspect rollback now treats the reverse rename as its own namespace
transition boundary. Pre-restore authority still requires the original exact
parked handle and a fresh parked-path handle to agree on the reviewed bytes and
identity. After the exact-handle reverse rename succeeds, Phase 14.3 re-hashes
the same original handle and independently reopens the restored target through
the pinned parent. Both post-transition views must again prove the reviewed
SHA-256, size, mtime, filesystem identity and `nlink == 1` before an
`aborted_exact_target_restored` result may be recorded. Any post-transition
proof failure leaves the durable attempt unresolved and writes no result row.
See `PHASE14_3_5_ROLLBACK_POST_TRANSITION_ATTESTATION.md`.
