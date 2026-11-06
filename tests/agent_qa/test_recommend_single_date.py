"""recommend_candidates 는 하루 한 자리만 — 여러 일자/범위는 차단(fan-out 방지)."""
from agents_v2.errors import ErrorType, classify
from agents_v2.skills.recommend_candidates import recommend_candidates

_BASE = {"group_id": "G", "year": 2026, "month": 5}


def test_date_range_blocked():
    res = recommend_candidates(None, {**_BASE, "date": "2026-05-01~2026-05-31", "shift_codes": ["N"]})
    assert res.get("needs_clarification") is True
    assert classify(res) is ErrorType.CLARIFICATION


def test_dates_list_blocked():
    res = recommend_candidates(None, {**_BASE, "dates": ["2026-05-03", "2026-05-04"]})
    assert res.get("needs_clarification") is True


def test_start_end_range_blocked():
    res = recommend_candidates(None, {**_BASE, "start_date": "2026-05-01", "end_date": "2026-05-10"})
    assert res.get("needs_clarification") is True


def test_missing_date_still_errors():
    res = recommend_candidates(None, {**_BASE})
    assert res.get("error") and "date" in res["error"]
