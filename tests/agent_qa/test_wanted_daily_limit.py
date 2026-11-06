"""manage_wanted_limits set_daily_limit — 특정일 원티드 신청 제한 설정 (preview→apply, merge).

upsert_wanted_config 는 월 replace 라, 단일 날짜 설정이 다른 날짜를 지우지 않는지(merge) 검증.
"""
from agents_v2.middleware import execute_skill
from agents_v2.schemas.session_context import SessionContext
from db.models import WantedConfig


def _hn():
    return SessionContext(office_id="OFF001", group_id="GRP001", year=2026, month=8,
                          nurse_id="N001", nurse_name="김민지", user_role="HN")


def _p(**kw):
    return {"operation": "set_daily_limit", "group_id": "GRP001", "year": 2026, "month": 8, **kw}


def test_set_daily_limit_preview(db, seed_data):
    res = execute_skill(db, "manage_wanted_limits",
                        _p(target_date="2026-08-15", shift_type="휴무", max_requests=3, preview_only=True), _hn())
    d = res.data
    assert d.get("preview") is True
    assert d["summary"] == {"target_date": "2026-08-15", "shift_type": "휴무", "from": None, "to": 3}
    # DB 미반영
    assert db.query(WantedConfig).filter_by(group_id="GRP001").count() == 0


def test_set_daily_limit_apply(db, seed_data):
    res = execute_skill(db, "manage_wanted_limits",
                        _p(target_date="2026-08-15", shift_type="휴무", max_requests=3, preview_only=False), _hn())
    assert res.data.get("ok") is True
    rows = db.query(WantedConfig).filter_by(group_id="GRP001").all()
    assert len(rows) == 1
    assert str(rows[0].target_date) == "2026-08-15" and int(rows[0].max_requests) == 3


def test_set_two_dates_merge_not_wipe(db, seed_data):
    # 15일 설정 → 20일 설정 시 15일이 안 지워져야(merge)
    execute_skill(db, "manage_wanted_limits",
                  _p(target_date="2026-08-15", max_requests=3, preview_only=False), _hn())
    execute_skill(db, "manage_wanted_limits",
                  _p(target_date="2026-08-20", max_requests=2, preview_only=False), _hn())
    dates = {str(r.target_date): int(r.max_requests) for r in db.query(WantedConfig).filter_by(group_id="GRP001").all()}
    assert dates == {"2026-08-15": 3, "2026-08-20": 2}, dates


def test_missing_date_clarifies(db, seed_data):
    res = execute_skill(db, "manage_wanted_limits", _p(max_requests=3), _hn())
    assert res.data.get("needs_clarification") is True
