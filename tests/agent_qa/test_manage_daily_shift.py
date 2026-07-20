"""manage_daily_shift — 특정일/월 필요인원(DailyShift) 설정 + read-back."""
from agents_v2.middleware import execute_skill
from agents_v2.errors import classify, ErrorType
from agents_v2.schemas.session_context import SessionContext
from db.models import DailyShift
from services import daily_shift_service


def _hn():
    return SessionContext(office_id="OFF001", group_id="GRP001", year=2026, month=8,
                          nurse_id="N001", nurse_name="김민지", user_role="HN")


def _day_params(preview=True):
    return {"scope": "day", "date": "2026-08-07", "d_count": 8, "e_count": 7, "n_count": 3,
            "preview_only": preview}


def test_day_preview_shape(db, seed_data):
    res = execute_skill(db, "manage_daily_shift", _day_params(True), _hn())
    d = res.data
    assert d.get("preview") is True and d["scope"] == "day"
    assert d["summary"]["date"] == "2026-08-07" and d["summary"]["to"] == {"D": 8, "E": 7, "N": 3}
    assert classify(d) is ErrorType.PREVIEW


def test_day_apply_writes_dailyshift(db, seed_data):
    res = execute_skill(db, "manage_daily_shift", _day_params(False), _hn())
    assert res.data.get("ok") is True and res.data.get("verification_failed") is not True
    row = db.query(DailyShift).filter_by(group_id="GRP001", year=2026, month=8, day=7).first()
    assert row and (row.d_count, row.e_count, row.n_count) == (8, 7, 3)


def test_readback_catches_false_complete(db, seed_data, monkeypatch):
    # update_daily 를 no-op → ok 보고하지만 DailyShift 미반영 → VERIFICATION_FAILED
    monkeypatch.setattr(daily_shift_service, "update_daily", lambda *a, **k: None)
    res = execute_skill(db, "manage_daily_shift", _day_params(False), _hn())
    assert res.data.get("verification_failed") is True
    assert classify(res.data) is ErrorType.VERIFICATION_FAILED


def test_month_apply_bulk(db, seed_data):
    res = execute_skill(db, "manage_daily_shift",
                        {"scope": "month", "n_count": 3, "preview_only": False}, _hn())
    assert res.data.get("ok") is True and res.data["scope"] == "month"
    # 표본 여러 날 N=3 확인
    rows = db.query(DailyShift).filter_by(group_id="GRP001", year=2026, month=8).all()
    day_rows = [r for r in rows if int(r.day) > 0]
    assert day_rows and all(r.n_count == 3 for r in day_rows)


def test_scope_inference_no_counts_clarifies(db, seed_data):
    res = execute_skill(db, "manage_daily_shift", {"date": "2026-08-07"}, _hn())
    assert res.data.get("needs_clarification") is True


# ── 요일 패턴 grounding (weekend/weekday) ──
import calendar
from agents_v2.grounding.internal import resolve_pattern_days


def test_resolve_pattern_days_deterministic():
    # 2026-08 주말 = 결정적
    truth = [d for d in range(1, 32) if calendar.weekday(2026, 8, d) in (5, 6)]
    assert resolve_pattern_days("weekend", 2026, 8) == truth == [1, 2, 8, 9, 15, 16, 22, 23, 29, 30]
    wk = resolve_pattern_days("weekday", 2026, 8)
    assert set(wk).isdisjoint(truth) and len(wk) + len(truth) == 31
    assert resolve_pattern_days("격주", 2026, 8) == []  # 미지원 → 빈 리스트


def test_weekend_apply_only_weekends(db, seed_data):
    res = execute_skill(db, "manage_daily_shift",
                        {"scope": "weekend", "d_count": 3, "e_count": 2, "n_count": 2,
                         "preview_only": False}, _hn())
    assert res.data.get("ok") is True and res.data["scope"] == "weekend"
    assert res.data["days"] == [1, 2, 8, 9, 15, 16, 22, 23, 29, 30]
    rows = {int(r.day): r for r in
            db.query(DailyShift).filter_by(group_id="GRP001", year=2026, month=8).all()
            if int(r.day) > 0}
    # 주말(8/2 일)은 322, 평일(8/3 월)은 그대로(≠322 이어야)
    assert (rows[2].d_count, rows[2].e_count, rows[2].n_count) == (3, 2, 2)
    assert (rows[3].d_count, rows[3].e_count, rows[3].n_count) != (3, 2, 2)


def test_weekend_preview_shows_days(db, seed_data):
    res = execute_skill(db, "manage_daily_shift",
                        {"scope": "weekend", "d_count": 3, "e_count": 2, "n_count": 2}, _hn())
    d = res.data
    assert d.get("preview") is True and d["scope"] == "weekend"
    assert d["summary"]["days"] == [1, 2, 8, 9, 15, 16, 22, 23, 29, 30]
