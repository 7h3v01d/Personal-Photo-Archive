"""Phase 7.0.1 — historical date reconstruction (pure engine).

Read-only, deterministic. A reconstruction is never more precise or more certain
than the evidence supporting it: GPS (UTC, date-only) never anchors an exact
offset; filename-order bracketing needs a confirmed single-device ordering.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from ppa.dating import Reliability
from ppa.reconstruct import (
    Confidence,
    KnownTrueKind,
    ReconstructionInput,
    reconstruct,
)

R = Reliability
K = KnownTrueKind


def _dt(y, m, d, H=0, M=0):
    return datetime(y, m, d, H, M, tzinfo=timezone.utc)


def _run(n, group="R1", strong=True, epoch=(2001, 1, 1)):
    return [ReconstructionInput(f"f{i}", _dt(*epoch, 0, i * 5), R.LIKELY_WRONG,
                                reset_group=group, reset_group_strong=strong, seq=201 + i)
            for i in range(n)]


# --- direct ------------------------------------------------------------------


def test_human_exact_is_confirmed_point():
    i = ReconstructionInput("a", _dt(2001, 1, 1), R.QUESTIONABLE,
                            known_true=date(2004, 12, 25), known_true_kind=K.HUMAN_EXACT)
    r = reconstruct([i])["a"]
    assert r.start == date(2004, 12, 25) and r.end is None
    assert r.confidence is Confidence.CONFIRMED and r.method == "direct"


def test_gps_date_is_a_plus_minus_one_day_range():
    i = ReconstructionInput("a", _dt(2001, 1, 1), R.QUESTIONABLE,
                            known_true=date(2004, 12, 25), known_true_kind=K.GPS_DATE)
    r = reconstruct([i])["a"]
    assert (r.start, r.end) == (date(2004, 12, 24), date(2004, 12, 26))
    assert r.confidence is Confidence.RANGE and r.method == "direct_gps"


# --- offset propagation ------------------------------------------------------


def test_offset_propagates_from_human_exact_anchor():
    ins = _run(6)
    ins[3].known_true = date(2004, 12, 25); ins[3].known_true_kind = K.HUMAN_EXACT
    res = reconstruct(ins)
    assert all(res[f"f{i}"].start == date(2004, 12, 25) for i in range(6))
    assert res["f3"].method == "direct"
    assert all(res[f"f{i}"].method == "offset" and res[f"f{i}"].confidence is Confidence.STRONG
               for i in (0, 1, 2, 4, 5))


def test_gps_never_anchors_offset():
    # Brisbane local 25 Dec 00:30 reads as GPS UTC 24 Dec: must NOT push the run.
    ins = _run(4)
    ins[0].known_true = date(2004, 12, 24); ins[0].known_true_kind = K.GPS_DATE
    res = reconstruct(ins)
    assert set(res) == {"f0"}                        # only the GPS frame, as a range
    assert res["f0"].method == "direct_gps"


def test_offset_handles_multi_day_rollover():
    a = ReconstructionInput("a", _dt(2001, 1, 1, 23, 50), R.LIKELY_WRONG, "R2", True, 201,
                            known_true=date(2004, 12, 25), known_true_kind=K.HUMAN_EXACT)
    b = ReconstructionInput("b", _dt(2001, 1, 2, 0, 10), R.LIKELY_WRONG, "R2", True, 202)
    res = reconstruct([a, b])
    assert res["a"].start == date(2004, 12, 25)
    assert res["b"].start == date(2004, 12, 26)


def test_offset_not_applied_to_model_only_run():
    ins = _run(4, strong=False)
    ins[0].known_true = date(2004, 12, 25); ins[0].known_true_kind = K.HUMAN_EXACT
    assert set(reconstruct(ins)) == {"f0"}


def test_conflicting_offsets_withhold_propagation():
    ins = _run(3)
    ins[0].known_true = date(2004, 12, 25); ins[0].known_true_kind = K.HUMAN_EXACT
    ins[1].known_true = date(2010, 1, 1); ins[1].known_true_kind = K.HUMAN_EXACT
    res = reconstruct(ins)
    assert "f2" not in res
    assert "f0" in res and "f1" in res


def test_offset_only_revises_questionable_or_likely_wrong():
    a = ReconstructionInput("a", _dt(2001, 1, 1), R.LIKELY_WRONG, "R4", True, 201,
                            known_true=date(2004, 12, 25), known_true_kind=K.HUMAN_EXACT)
    b = ReconstructionInput("b", _dt(2001, 1, 1), R.PROBABLY_VALID, "R4", True, 202)
    res = reconstruct([a, b])
    assert "b" not in res                            # clean claim not overwritten


# --- range anchor & bracketing -----------------------------------------------


def test_range_anchor_reconstructs_to_interval():
    i = ReconstructionInput("r", _dt(2001, 1, 1), R.LIKELY_WRONG,
                            anchor_range=(date(2004, 12, 20), date(2004, 12, 31)))
    r = reconstruct([i])["r"]
    assert (r.start, r.end) == (date(2004, 12, 20), date(2004, 12, 31))
    assert r.confidence is Confidence.RANGE and r.method == "anchor_range"


def test_bracketing_between_point_dated_neighbours_strong_group():
    ins = _run(3, strong=True)
    ins[0].known_true = date(2004, 12, 24); ins[0].known_true_kind = K.HUMAN_EXACT
    ins[2].known_true = date(2004, 12, 26); ins[2].known_true_kind = K.HUMAN_EXACT
    r = reconstruct(ins)["f1"]
    assert r.method == "bracket"
    assert (r.start, r.end) == (date(2004, 12, 24), date(2004, 12, 26))


def test_bracketing_withheld_for_model_only_sequence():
    # Two serial-less same-model bodies may interleave; filename order can't place
    # the middle frame, so no bracket.
    ins = _run(3, strong=False)
    ins[0].known_true = date(2004, 12, 24); ins[0].known_true_kind = K.HUMAN_EXACT
    ins[2].known_true = date(2004, 12, 26); ins[2].known_true_kind = K.HUMAN_EXACT
    assert "f1" not in reconstruct(ins)


# --- trust boundary / validation ---------------------------------------------


def test_rejects_unknown_confirmation_source():
    with pytest.raises(ValueError):
        reconstruct([ReconstructionInput("x", _dt(2001, 1, 1), R.LIKELY_WRONG,
                                         known_true=date(2020, 1, 1), known_true_kind="ai_guess")])


def test_rejects_invalid_range():
    with pytest.raises(ValueError):
        reconstruct([ReconstructionInput("y", _dt(2001, 1, 1), R.LIKELY_WRONG,
                                         anchor_range=(date(2005, 1, 1), date(2004, 1, 1)))])


def test_rejects_duplicate_file_id():
    with pytest.raises(ValueError):
        reconstruct([ReconstructionInput("a", _dt(2001, 1, 1), R.LIKELY_WRONG),
                     ReconstructionInput("a", _dt(2001, 1, 1), R.LIKELY_WRONG)])


def test_never_reconstructs_without_a_basis():
    assert reconstruct(_run(3)) == {}
