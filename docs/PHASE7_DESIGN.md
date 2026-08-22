# Phase 7 — Historical Date Reconstruction (design brief)

Status: DESIGN, for review. Recommended to follow real-collection testing of the
Phase 6 stack (per adversarial review), which will shape the reconstruction rules
more than synthetic cases can.

## Where this sits

```
Phase 6  diagnosis:  "Is this date credible?"  -> TRUSTED / … / LIKELY_WRONG
Phase 7  recovery:   "If 2001 is wrong, WHEN was it actually taken?"
```

Phase 6 answers credibility well. It deliberately stops at a `corrected_hint`
and never adopts a corrected date. Phase 7 owns that step: producing an
*interpreted* capture date/range for photos Phase 6 flagged, with provenance and
confidence — and, crucially, never overwriting the original observations.

## Non-negotiable stance (inherited)

- **Reconstruction is interpretation, not observation.** A reconstructed date
  lives in its own layer/table, separate from `metadata_observations`. Originals
  and their EXIF are never modified. The recorded (wrong) date is preserved as
  evidence.
- **Represent uncertainty.** A reconstruction is a date OR a range, with a
  confidence and the evidence chain that produced it — never a bare overwrite.
- **Human confirmation is the top of the ladder.** The engine proposes; a person
  (or a strong independent anchor/GPS) confirms. Confirmed reconstructions are
  authoritative; proposed ones are clearly provisional.

## Reconstruction mechanisms (strongest first)

1. **Direct independent evidence.** A frame with an exact user anchor or a GPS
   date already HAS its true date. Reconstruction = that date, high confidence.
   (Phase 6 already surfaces these as `corrected_hint` / TRUSTED.)

2. **Clock-offset propagation across a confirmed reset run (the big one).** For a
   confirmed single-device reset run, the clock is wrong but *monotonic* — it
   ticks forward correctly, just from the wrong epoch. If ONE frame's true date
   is known (GPS/anchor), the offset `true - recorded` applies to the whole run,
   so every frame's true instant = its recorded instant + offset. This
   reconstructs the entire session's real timeline from a single known point.
   Requires: confirmed single device (Slice 2.3/3.2.1 identity strength), a
   contiguous run (Slice 2 segmentation), and one independent datum.

3. **Neighbour bracketing / interpolation.** A wrong frame between two
   trusted-dated neighbours (in filename order) reconstructs to the RANGE between
   them; if the neighbours are close, the range is tight. Never a point estimate
   unless the bracket collapses to one.

4. **Directory / event anchors.** A range anchor ("this folder is Dec 2004")
   reconstructs affected frames to that range; combined with filename order it
   can order-within-range but not pin exact dates.

## Confidence model

- `CONFIRMED` — human-confirmed, or direct independent evidence (anchor/GPS).
- `STRONG` — clock-offset propagation from a confirmed datum in a confirmed
  single-device run.
- `RANGE` — bracketed/anchored to an interval, not a point.
- `PROPOSED` — weaker inference; shown, never treated as fact.

## Data model (sketch)

`reconstructions(id, file_id, kind, start_date, end_date, confidence, status,
method, evidence, created_at)` where `status ∈ {proposed, confirmed, rejected}`
and `method` records the mechanism (direct/offset/bracket/anchor). Never joined
into observations; resolved at read time. A confirmed reconstruction can feed a
`TRUSTED` date back into views without touching EXIF.

## Slice breakdown

- **7.0**: pure reconstruction engine — offset propagation + bracketing over
  Phase-6 output + evidence; produces proposals with confidence. Storage-agnostic,
  tested, read-only.
- **7.1.2** (DONE): evidence fingerprint; staleness = revision OR evidence
  mismatch; confirmation refused when either is stale.
- **7.1.1** (DONE): reconstructions bound to source revision; stale once bytes
  change; terminal decisions + reopen; created/updated split; engine version.
- **7.1** (DONE): `reconstructions` table + migration; wiring; `ppa reconstruct`
  run/list/confirm/reject flow; sticky human decisions.
- **7.2**: UI to review/confirm proposals; timeline view using confirmed dates.

## Non-goals

- No overwriting of EXIF/observations, ever.
- No ML/guessing beyond rule-based inference from independent evidence + order.
- No point estimate where only a range is justified.

## Open questions for review

1. Offset propagation: require the known datum to be INSIDE the contiguous run,
   or may a same-device datum just outside it anchor the offset?
2. How to represent and surface a reconstruction that conflicts with a later
   human confirmation (supersession history)?
3. Should bracketing use only TRUSTED neighbours, or also PROBABLY_VALID ones
   (with lower confidence)?
4. Timezone: EXIF is local and tz-less; offset propagation is robust to that
   (same clock), but absolute reconstructed instants may still need a tz policy.
