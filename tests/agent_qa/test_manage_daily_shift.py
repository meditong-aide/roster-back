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
