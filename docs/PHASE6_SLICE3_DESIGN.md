# Phase 6 Slice 3 — Independent Calendar Evidence (design brief)

Status: DESIGN / first-cut engine for adversarial review. No schema migration or
CLI yet — persistence and wiring are deferred to 3.1 on purpose, so the
epistemics can be reviewed before anything is committed to disk.

## Where this sits

```
Slice 1  what does THIS photo claim, on its own?          -> intrinsic rating
Slice 2  what does its SEQUENCE say (order, reset pattern)? -> adds doubt only
Slice 3  is there evidence about the CALENDAR DATE itself?  -> earns escalation
         + the first TRUSTED dates
Phase 7  reconstruct the corrected capture date            -> "probably Dec 2004"
```

Slices 1–2 were deliberately forbidden from concluding a date is *wrong* from
suspicion or order alone. Slice 3 is where an escalation to `LIKELY_WRONG`
becomes *earned*, and where the first `TRUSTED` dates appear — but only from
evidence that actually addresses the calendar claim, and is independent of the
camera clock we are trying to judge.

## The one rule that governs everything

> Evidence may escalate a claim only if it is INDEPENDENT of that claim and
> ADDRESSES it. Order is not calendar truth. Repetition is not a second witness.

So the camera's own DateTimeOriginal/Digitized/DateTime are all the *same clock*
and can never independently confirm each other's calendar date (Slice 1). Slice
3 admits only sources that come from *outside* that clock.

## Independent calendar evidence sources

| Source | Why independent of the camera clock | Strength | Yields |
|---|---|---|---|
| **User anchor (exact)** | A human states the real date ("this is Christmas 2004"). | Ground truth | `TRUSTED`; contradiction -> `LIKELY_WRONG` |
| **User anchor (range/event)** | Human bounds it ("this folder is Dec 2004"). | Strong bound | contradiction -> `LIKELY_WRONG`; does NOT alone give TRUSTED |
| **GPS date** (`exif-gps:GPSDateStamp`) | Written from the satellite fix, not the internal clock. | Strong (machine) | corroboration -> `TRUSTED`; contradiction -> `LIKELY_WRONG` |
| **Camera manufacture floor** | The model did not exist before year M — a fact about the hardware, not its clock. | Strong (if data reliable) | date < floor -> `LIKELY_WRONG` |

Notes on independence caveats (for the reviewer):

- GPS date is machine data and can, rarely, be wrong (bad fix, firmware bug).
  We therefore treat GPS **corroboration** (agrees with EXIF within tolerance) as
  strong enough for `TRUSTED`, but a *lone* GPS date that merely contradicts EXIF
  escalates the EXIF claim to `LIKELY_WRONG` **without** itself being promoted to
  a `TRUSTED` reconstruction — adopting the GPS date as the corrected capture
  date is Phase 7, not here.
- A manufacture floor is only as good as its data. It is user/curated config, and
  a conservative one (a safe lower bound like the model's announcement year) is
  fine. If we are unsure, we simply don't have a floor for that model.
- "TRUSTED" still means "the archive has strong independent evidence", not
  metaphysical certainty. An exact user anchor is the strongest we accept.

## The two outcomes

### 1. Escalation to LIKELY_WRONG (earned)

A photo's candidate date is escalated to `LIKELY_WRONG` when an independent
source **contradicts** it:

- `candidate < manufacture_floor` (camera didn't exist yet), or
- `candidate` falls outside a user anchor's exact date / range, or
- `|candidate - gps_date|` exceeds a day-scale tolerance.

**Reset-run propagation.** Slice 2 identified reset *patterns*: runs of adjacent
frames sharing one reset epoch — demonstrably a single clock-reset event. If ANY
frame in such a run is contradicted by independent evidence (or carries an exact
anchor whose date differs from the run's claimed epoch), then the shared claim is
disproved and **every** non-anchored frame in that run escalates to
`LIKELY_WRONG`. This is the marquee payoff: one GPS fix or one human anchor on a
single frame can correctly condemn the whole broken-clock session — without
inventing certainty for any single frame in isolation.

### 2. Anchoring to TRUSTED (the first trusted dates)

A photo becomes `TRUSTED` only via genuinely independent positive evidence:

- an **exact user anchor** (human ground truth) — date = the anchored date; if the
  EXIF candidate disagrees, the human wins and the EXIF is noted as wrong; or
- a **GPS date that corroborates** the EXIF candidate (same day within tolerance)
  — two independent witnesses agreeing.

Nothing else produces `TRUSTED`. Range anchors and lone contradicting GPS do not.

## Layering & safety

- Read-only and deterministic, like Slices 1–2. Slice 3 layers a *final*
  assessment over the Slice-2 combined rating; it never mutates stored data,
  observations, or the intrinsic/sequence assessments.
- Slice 3 may move a rating **up** (to TRUSTED) or **down** (to LIKELY_WRONG),
  but only on independent evidence. With no Slice-3 evidence, the Slice-2 result
  stands unchanged.
- Every change carries a human-readable reason naming the evidence.

## Data model (deferred to 3.1)

- **Anchors**: a new table (user-asserted, never derived), e.g.
  `anchors(id, scope, scope_ref, kind, start_date, end_date, note, created_at)`
  where `scope ∈ {file, directory, library, event}`. Anchors are *interpretation*,
  stored separately from observations — consistent with the archive's
  observation-vs-interpretation separation. Resolving an anchor to a photo is a
  read-time join; the photo's bytes/observations are never touched.
- **Manufacture floors**: curated config `(make, model) -> earliest_date`,
  shipped as data and user-extendable. No per-photo storage.
- **GPS date**: already present as `exif-gps:GPSDateStamp` observations; 3.1 wires
  a reader (`YYYY:MM:DD`) into the engine.

## Slice breakdown

- **3.0 (this cut)**: pure, storage-agnostic reconciliation engine + tests.
  Evidence is passed in as dataclasses. Proves escalation, propagation, and
  anchoring rules.
- **3.1**: anchors table + migration; manufacture-floor config; GPS reader;
  `analyse` integration and a read-only `ppa reconcile` report.
- **Phase 7**: capture-date *reconstruction* — turning "the 2001 claim is wrong,
  bracketed by Dec 2004 evidence" into an interpreted corrected date/range, and a
  UI to confirm anchors.

## Non-goals / explicitly deferred

- No reconstruction of corrected dates here (Phase 7).
- No ML/heuristic dating; only rule-based, independent evidence.
- Lone GPS is not promoted to a trusted corrected date (Phase 7).

## Open questions for review

1. Is GPS **corroboration** sufficient for `TRUSTED`, or should the first
   `TRUSTED` require a *human* anchor only, with GPS agreement capped at
   `PROBABLY_VALID`?
2. Reset-run **propagation**: is a single independent contradiction enough to
   condemn the whole run, or should we require the contradicting frame to be
   inside the run's contiguous segment (it is, by construction) AND the run to
   exceed a larger size threshold?
3. Manufacture-floor **granularity**: year vs exact date; how to treat unknown
   models (currently: no floor, no escalation).
4. Anchor **conflict**: two anchors disagree, or an exact anchor contradicts a
   corroborating GPS — precedence? (Proposed: human exact anchor wins, conflict
   recorded.)

---

## 3.0.1 — resolutions from adversarial review

The pure engine now records provenance rather than reading the prior enum:

- **`EvidenceEffect` on the result** (`NONE`/`SUPPORT`/`CONTRADICT`) plus an
  `independent_contradiction` flag. Reset-run propagation triggers ONLY from a
  real Slice-3 contradiction, never from a rating that arrived from Slice 2.
- **GPS→TRUSTED restricted** to a clean `PROBABLY_VALID` input (Q1 resolved: yes,
  but only for an otherwise-clean claim). `QUESTIONABLE` + agreeing GPS stays
  QUESTIONABLE; `LIKELY_WRONG` + agreeing GPS stays LIKELY_WRONG and records the
  evidence conflict — a repeated claim never erases a prior contradiction.
- **Exact anchors win but record conflicts** (`evidence_conflicts`) from GPS or
  manufacture-floor disagreement, rather than silently discarding them.
- **`CalendarEvidence` validates** on construction (exact⇒start; end⇒start;
  end≥start; exact excludes end) and all comparisons coerce naive→UTC.
- Reset propagation still needs only ONE strong contradiction inside a correctly
  established contiguous run (Q2 resolved: one is enough).

Deferred to 3.1: a structured *prior-doubt reason* passed into reconciliation so
GPS can specifically resolve a reset-epoch suspicion while leaving unrelated
contradictions intact; anchors table + migration; manufacture-floor config; GPS
reader; CLI. Anchors/GPS may move to `date` rather than `datetime` granularity.
