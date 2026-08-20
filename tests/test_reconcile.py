"""Phase 6 Slice 3.0 — independent calendar evidence (pure reconciliation).

Read-only, deterministic. Escalation and TRUSTED come only from evidence that is
independent of the camera clock AND addresses the calendar date.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ppa.dating import Reliability
from ppa.reconcile import (
    CalendarEvidence,
    ReconcilablePhoto,
    reconcile,
)


def _dt(y, m, d):
    return datetime(y, m, d, tzinfo=timezone.utc)


def _photo(fid, cand, rel, *, reset_group=None, ev=None, reset_group_strong=True):
    return ReconcilablePhoto(fid, cand, rel, reset_group, ev or CalendarEvidence(),
                             [], reset_group_strong)


# --- anchoring -> the first TRUSTED ------------------------------------------


def test_exact_user_anchor_yields_trusted():
    p = _photo("a", _dt(2001, 1, 1), Reliability.QUESTIONABLE,
               ev=CalendarEvidence(anchor_start=_dt(2004, 12, 25), anchor_exact=True))
    r = reconcile([p])["a"]
    assert r.reliability is Reliability.TRUSTED
    assert r.date == _dt(2004, 12, 25)          # human date wins
    assert r.changed


def test_gps_corroboration_yields_trusted():
    p = _photo("g", _dt(2010, 6, 1), Reliability.PROBABLY_VALID,
               ev=CalendarEvidence(gps_date=_dt(2010, 6, 1)))
    r = reconcile([p])["g"]
    assert r.reliability is Reliability.TRUSTED


def test_range_anchor_alone_does_not_trust():
    p = _photo("r", _dt(2004, 12, 25), Reliability.PROBABLY_VALID,
               ev=CalendarEvidence(anchor_start=_dt(2004, 12, 20), anchor_end=_dt(2004, 12, 31)))
    r = reconcile([p])["r"]
    assert r.reliability is Reliability.PROBABLY_VALID    # consistent, but not trusted


# --- escalation -> earned LIKELY_WRONG ---------------------------------------


def test_manufacture_floor_violation_is_likely_wrong():
    p = _photo("m", _dt(2001, 1, 1), Reliability.QUESTIONABLE,
               ev=CalendarEvidence(manufacture_floor=_dt(2003, 1, 1)))
    r = reconcile([p])["m"]
    assert r.reliability is Reliability.LIKELY_WRONG
    assert any("did not exist" in x for x in r.reasons)


def test_gps_contradiction_is_likely_wrong_but_not_trusted():
    p = _photo("g", _dt(2001, 1, 1), Reliability.QUESTIONABLE,
               ev=CalendarEvidence(gps_date=_dt(2004, 12, 25)))
    r = reconcile([p])["g"]
    assert r.reliability is Reliability.LIKELY_WRONG
    assert r.corrected_hint == _dt(2004, 12, 25)   # hint only; reconstruction is Phase 7


def test_range_anchor_contradiction_is_likely_wrong():
    p = _photo("r", _dt(2001, 1, 1), Reliability.QUESTIONABLE,
               ev=CalendarEvidence(anchor_start=_dt(2004, 12, 20), anchor_end=_dt(2004, 12, 31)))
    r = reconcile([p])["r"]
    assert r.reliability is Reliability.LIKELY_WRONG


def test_no_evidence_leaves_slice2_result_unchanged():
    p = _photo("n", _dt(2001, 1, 1), Reliability.QUESTIONABLE)
    r = reconcile([p])["n"]
    assert r.reliability is Reliability.QUESTIONABLE
    assert not r.changed


# --- reset-run propagation (the marquee payoff) ------------------------------


def test_one_gps_contradiction_condemns_the_whole_reset_run():
    # 6 frames all claim 2001-01-01 (one reset event); one has a GPS date of 2004.
    photos = [_photo(f"f{i}", _dt(2001, 1, 1), Reliability.QUESTIONABLE, reset_group="R1")
              for i in range(6)]
    photos[3] = _photo("f3", _dt(2001, 1, 1), Reliability.QUESTIONABLE, reset_group="R1",
                       ev=CalendarEvidence(gps_date=_dt(2004, 12, 25)))
    res = reconcile(photos)
    assert all(res[f"f{i}"].reliability is Reliability.LIKELY_WRONG for i in range(6))


def test_exact_anchor_on_one_frame_trusts_it_and_condemns_the_rest():
    photos = [_photo(f"f{i}", _dt(2001, 1, 1), Reliability.QUESTIONABLE, reset_group="R1")
              for i in range(5)]
    photos[0] = _photo("f0", _dt(2001, 1, 1), Reliability.QUESTIONABLE, reset_group="R1",
                       ev=CalendarEvidence(anchor_start=_dt(2004, 12, 25), anchor_exact=True))
    res = reconcile(photos)
    assert res["f0"].reliability is Reliability.TRUSTED               # anchored frame
    assert all(res[f"f{i}"].reliability is Reliability.LIKELY_WRONG for i in range(1, 5))


def test_reset_run_without_independent_evidence_is_not_condemned():
    # Slice 3 must not escalate a reset pattern on its own — that was Slice 2's
    # whole correction. Stays QUESTIONABLE.
    photos = [_photo(f"f{i}", _dt(2001, 1, 1), Reliability.QUESTIONABLE, reset_group="R1")
              for i in range(6)]
    res = reconcile(photos)
    assert all(res[f"f{i}"].reliability is Reliability.QUESTIONABLE for i in range(6))


def test_consistent_gps_within_questionable_run_does_not_upgrade():
    # GPS agreeing with a QUESTIONABLE (reset-epoch) frame does NOT upgrade it:
    # Slice 3 doesn't know why it was questionable, so it must not erase doubt.
    photos = [_photo(f"f{i}", _dt(2001, 1, 1), Reliability.QUESTIONABLE, reset_group="R1")
              for i in range(4)]
    photos[1] = _photo("f1", _dt(2001, 1, 1), Reliability.QUESTIONABLE, reset_group="R1",
                       ev=CalendarEvidence(gps_date=_dt(2001, 1, 1)))
    res = reconcile(photos)
    assert all(res[f"f{i}"].reliability is Reliability.QUESTIONABLE for i in range(4))


# --- 3.0.1: layer-boundary correctness ---------------------------------------


def test_likely_wrong_is_not_laundered_to_trusted_by_agreeing_gps():
    p = _photo("x", _dt(2099, 1, 1), Reliability.LIKELY_WRONG,
               ev=CalendarEvidence(gps_date=_dt(2099, 1, 1)))
    r = reconcile([p])["x"]
    assert r.reliability is Reliability.LIKELY_WRONG          # not upgraded
    assert r.evidence_conflicts                                # conflict recorded


def test_questionable_plus_agreeing_gps_stays_questionable():
    p = _photo("q", _dt(2001, 1, 1), Reliability.QUESTIONABLE,
               ev=CalendarEvidence(gps_date=_dt(2001, 1, 1)))
    r = reconcile([p])["q"]
    assert r.reliability is Reliability.QUESTIONABLE


def test_probably_valid_plus_agreeing_gps_is_trusted():
    p = _photo("p", _dt(2010, 6, 1), Reliability.PROBABLY_VALID,
               ev=CalendarEvidence(gps_date=_dt(2010, 6, 1)))
    assert reconcile([p])["p"].reliability is Reliability.TRUSTED


def test_reset_propagation_requires_real_slice3_contradiction():
    # A frame that arrived LIKELY_WRONG from Slice 2 (no Slice-3 evidence) must
    # NOT trigger propagation across its reset group.
    photos = [_photo("A", _dt(2001, 1, 1), Reliability.LIKELY_WRONG, reset_group="R1"),
              _photo("B", _dt(2001, 1, 1), Reliability.QUESTIONABLE, reset_group="R1"),
              _photo("C", _dt(2001, 1, 1), Reliability.QUESTIONABLE, reset_group="R1")]
    res = reconcile(photos)
    assert res["A"].reliability is Reliability.LIKELY_WRONG    # unchanged
    assert res["B"].reliability is Reliability.QUESTIONABLE    # NOT condemned
    assert res["C"].reliability is Reliability.QUESTIONABLE


def test_exact_anchor_records_conflicting_evidence():
    p = _photo("h", _dt(2001, 1, 1), Reliability.QUESTIONABLE,
               ev=CalendarEvidence(anchor_start=_dt(2004, 12, 25), anchor_exact=True,
                                   gps_date=_dt(2010, 1, 1), manufacture_floor=_dt(2005, 1, 1)))
    r = reconcile([p])["h"]
    assert r.reliability is Reliability.TRUSTED and r.date == _dt(2004, 12, 25)
    assert len(r.evidence_conflicts) == 2     # GPS and floor conflicts recorded, not hidden


def test_calendar_evidence_validates_ranges():
    import pytest
    with pytest.raises(ValueError):
        CalendarEvidence(anchor_exact=True)                       # missing anchor_start
    with pytest.raises(ValueError):
        CalendarEvidence(anchor_end=_dt(2004, 1, 1))              # end without start
    with pytest.raises(ValueError):
        CalendarEvidence(anchor_start=_dt(2005, 1, 1), anchor_end=_dt(2004, 1, 1))  # end < start


def test_naive_datetimes_do_not_crash():
    from datetime import datetime as _d
    p = _photo("n", _d(2001, 1, 1), Reliability.QUESTIONABLE,      # naive candidate
               ev=CalendarEvidence(gps_date=_dt(2004, 1, 1)))
    assert reconcile([p])["n"].reliability is Reliability.LIKELY_WRONG


# --- 3.2: doubt-specific resolution ------------------------------------------

from ppa.dating import DoubtReason  # noqa: E402


def _q(fid, cand, doubts, ev):
    return ReconcilablePhoto(fid, cand, Reliability.QUESTIONABLE, None, ev, doubts)


def test_gps_resolves_reset_epoch_only_doubt_to_trusted():
    p = _q("a", _dt(2001, 1, 1), [DoubtReason.RESET_EPOCH],
           CalendarEvidence(gps_date=_dt(2001, 1, 1)))
    assert reconcile([p])["a"].reliability is Reliability.TRUSTED


def test_gps_does_not_resolve_when_unrelated_doubt_present():
    p = _q("b", _dt(2001, 1, 1),
           [DoubtReason.RESET_EPOCH, DoubtReason.FIELD_DISAGREEMENT],
           CalendarEvidence(gps_date=_dt(2001, 1, 1)))
    assert reconcile([p])["b"].reliability is Reliability.QUESTIONABLE


def test_gps_does_not_resolve_questionable_with_no_coded_doubts():
    p = _q("c", _dt(2001, 1, 1), [], CalendarEvidence(gps_date=_dt(2001, 1, 1)))
    assert reconcile([p])["c"].reliability is Reliability.QUESTIONABLE


def test_only_filesystem_doubt_resolved_by_confirming_gps():
    p = _q("d", _dt(2015, 3, 4), [DoubtReason.ONLY_FILESYSTEM],
           CalendarEvidence(gps_date=_dt(2015, 3, 4)))
    assert reconcile([p])["d"].reliability is Reliability.TRUSTED


def test_field_disagreement_alone_is_not_resolved_by_gps():
    # An internal inconsistency is not settled just because an outside date
    # agrees with the chosen reading.
    p = _q("e", _dt(2010, 6, 1), [DoubtReason.FIELD_DISAGREEMENT],
           CalendarEvidence(gps_date=_dt(2010, 6, 1)))
    assert reconcile([p])["e"].reliability is Reliability.QUESTIONABLE


# --- 3.2.1: propagation gated on device identity & group-evidence coherence ---


def test_model_only_group_does_not_propagate_condemnation():
    # A reset group that is NOT a confirmed single device (serial-less) may be
    # two bodies; one contradiction condemns only its own frame.
    photos = [_photo(f"f{i}", _dt(2001, 1, 1), Reliability.QUESTIONABLE,
                     reset_group="R1", reset_group_strong=False) for i in range(6)]
    photos[1] = _photo("f1", _dt(2001, 1, 1), Reliability.QUESTIONABLE, reset_group="R1",
                       reset_group_strong=False, ev=CalendarEvidence(gps_date=_dt(2004, 12, 25)))
    res = reconcile(photos)
    assert res["f1"].reliability is Reliability.LIKELY_WRONG
    assert all(res[f"f{i}"].reliability is Reliability.QUESTIONABLE for i in (0, 2, 3, 4, 5))


def test_confirmed_device_group_propagates_condemnation():
    photos = [_photo(f"f{i}", _dt(2001, 1, 1), Reliability.QUESTIONABLE,
                     reset_group="R1", reset_group_strong=True) for i in range(6)]
    photos[1] = _photo("f1", _dt(2001, 1, 1), Reliability.QUESTIONABLE, reset_group="R1",
                       reset_group_strong=True, ev=CalendarEvidence(gps_date=_dt(2004, 12, 25)))
    res = reconcile(photos)
    assert all(res[f"f{i}"].reliability is Reliability.LIKELY_WRONG for i in range(6))


def test_conflicting_group_evidence_withholds_propagation():
    # One frame's GPS confirms 2001, another's GPS disproves it: the group's
    # independent evidence conflicts. Frames without their own evidence stay
    # QUESTIONABLE; frames with evidence keep individual results.
    photos = [
        _photo("A", _dt(2001, 1, 1), Reliability.QUESTIONABLE, reset_group="R1",
               reset_group_strong=True, ev=CalendarEvidence(gps_date=_dt(2001, 1, 1))),
        _photo("B", _dt(2001, 1, 1), Reliability.QUESTIONABLE, reset_group="R1",
               reset_group_strong=True, ev=CalendarEvidence(gps_date=_dt(2004, 12, 25))),
        _photo("C", _dt(2001, 1, 1), Reliability.QUESTIONABLE, reset_group="R1",
               reset_group_strong=True),
    ]
    # A needs its reset-epoch doubt to be resolvable for GPS to trust it.
    photos[0] = ReconcilablePhoto("A", _dt(2001, 1, 1), Reliability.QUESTIONABLE, "R1",
                                  CalendarEvidence(gps_date=_dt(2001, 1, 1)),
                                  [DoubtReason.RESET_EPOCH], True)
    res = reconcile(photos)
    assert res["A"].reliability is Reliability.TRUSTED        # its GPS confirms 2001
    assert res["B"].reliability is Reliability.LIKELY_WRONG   # its GPS disproves 2001
    assert res["C"].reliability is Reliability.QUESTIONABLE   # withheld, not condemned
    assert any("conflict" in r.lower() for r in res["C"].reasons)
