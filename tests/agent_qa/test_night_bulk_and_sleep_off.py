"""① 나이트 한도 병동 일괄 + ② 수면OFF 적용 여부 조회(읽기 전용).

① 은 기존 night_bulk_apply_service 를 재사용한다 — 에이전트가 자체 루프를 돌면
   대상자 선정·검증 규칙이 두 벌이 되므로, 서비스를 타는지 자체를 검증한다.
② 는 조회만 연다. 쓰기 함수가 생기지 않았는지도 함께 못 박는다.
"""
from datetime import datetime

import pytest

from agents_v2.errors import ErrorType, classify
from agents_v2.middleware import execute_skill
from agents_v2.schemas.session_context import SessionContext
from agents_v2.tools import leave_tools
from db.models import Nurse, NurseMonthlyLimit, NurseNightCycle, RosterConfig, Shift


def _hn():
    return SessionContext(office_id="OFF001", group_id="GRP001", year=2026, month=8,
                          nurse_id="N001", nurse_name="김민지", user_role="HN")


def _bulk(preview=False, **over):
    p = {"scope": "전체", "year": 2026, "month": 8, "preview_only": preview}
    p.update(over)
    return p


# ── ① 나이트 일괄 ───────────────────────────────────────


def test_bulk_preview_lists_targets_without_writing(db, seed_data):
    res = execute_skill(db, "update_monthly_limit", _bulk(preview=True, n_exact=5), _hn())
    d = res.data
    assert d.get("preview") is True and classify(d) is ErrorType.PREVIEW
    assert d["summary"]["kind"] == "고정(정확히)" and d["summary"]["value"] == 5
    assert d["summary"]["대상_인원"] > 0
    assert db.query(NurseMonthlyLimit).count() == 0


def test_bulk_apply_writes_all_n_capable(db, seed_data):
    res = execute_skill(db, "update_monthly_limit", _bulk(n_exact=5), _hn())
    assert res.data.get("ok") is True, res.data
    rows = db.query(NurseMonthlyLimit).filter_by(year=2026, month=8).all()
    assert rows and all(r.n_exact == 5 for r in rows)
    assert len(rows) == res.data["summary"]["대상_인원"]


def test_bulk_max_maps_to_n_max(db, seed_data):
    res = execute_skill(db, "update_monthly_limit", _bulk(n_max=4), _hn())
    assert res.data.get("ok") is True
    rows = db.query(NurseMonthlyLimit).filter_by(year=2026, month=8).all()
    assert rows and all(r.n_max == 4 and r.n_exact is None for r in rows)


def test_bulk_kind_explicit_wins(db, seed_data):
    res = execute_skill(db, "update_monthly_limit",
                        _bulk(bulk_kind="최대", n_exact=6), _hn())
    assert res.data["summary"]["kind"] == "최대"


def test_bulk_without_value_asks(db, seed_data):
    res = execute_skill(db, "update_monthly_limit", _bulk(), _hn())
    assert "error" in res.data and "hint" in res.data


def test_bulk_excludes_non_night_capable(db, seed_data):
    """야간 불가(D 전담)는 대상에서 빠져야 — 서비스의 대상자 선정 기준을 그대로 따른다."""
    db.query(Nurse).filter_by(nurse_id="N002").update({"allowed_shifts": ["D"]})
    db.flush()
    res = execute_skill(db, "update_monthly_limit", _bulk(preview=True, n_exact=5), _hn())
    assert "박지은" not in res.data["summary"]["대상"]


def test_bulk_uses_service_not_own_loop(db, seed_data, monkeypatch):
    """서비스를 타지 않으면 검증 규칙이 갈라진다 — 실제 호출 여부를 못 박는다."""
    import services.nurse_monthly_limit_service as svc

    called = {}
    orig = svc.night_bulk_apply_service

    def _spy(*a, **kw):
        called["hit"] = kw
        return orig(*a, **kw)

    monkeypatch.setattr(svc, "night_bulk_apply_service", _spy)
    execute_skill(db, "update_monthly_limit", _bulk(n_exact=3), _hn())
    assert called.get("hit", {}).get("kind") == "fixed"
    assert called["hit"]["value"] == 3


def test_bulk_general_nurse_blocked(db, seed_data):
    ctx = SessionContext(office_id="OFF001", group_id="GRP001", year=2026, month=8,
                         nurse_id="N002", nurse_name="박지은", user_role="RN")
    res = execute_skill(db, "update_monthly_limit", _bulk(n_exact=5), ctx)
    assert "error" in res.data
    assert db.query(NurseMonthlyLimit).count() == 0


def test_single_nurse_path_unaffected(db, seed_data):
    """scope 미지정은 기존 개인 경로 그대로(비회귀)."""
    res = execute_skill(db, "update_monthly_limit",
                        {"nurse_name": "박지은", "year": 2026, "month": 8,
                         "n_exact": 4, "preview_only": False}, _hn())
    assert res.data.get("preview") is False
    rows = db.query(NurseMonthlyLimit).filter_by(year=2026, month=8).all()
    assert len(rows) == 1 and rows[0].nurse_id == "N002"


# ── ② 수면OFF 적용 여부 (읽기 전용) ─────────────────────


@pytest.fixture
def sleep_cfg(db, seed_data):
    db.add(Shift(shift_id="SO", office_id="OFF001", group_id="GRP001", name="수면오프",
                 id=131, color="#0ff", shift_gb="오프", sequence=131,
                 health_leave_target=False, sleep_off_target=True))
    cfg = db.query(RosterConfig).filter_by(group_id="GRP001").first()
    cfg.sleep_off_enabled = True
    cfg.sleep_off_cycle = 15
    db.flush()
    return cfg


def test_status_reports_applied(db, sleep_cfg):
    db.add(NurseNightCycle(nurse_id="N002", group_id="GRP001", year=2026, month=8,
                           seq_at_end=7, pending_sleep=1, sleep_off_count=1, sleep_off_seq=3))
    db.flush()
    res = execute_skill(db, "query_schedule",
                        {"scope": "sleep_off_status", "year": 2026, "month": 8}, _hn())
    d = res.data
    assert d["적용"] is True and d["주기"] == 15 and d["코드"]["code"] == "SO"
    assert d["read_only"] is True
    row = d["nurses"][0]
    assert row["nurse"] == "박지은" and row["이월_미부여"] == 1 and row["누적_회차"] == 3
    assert "적용 중" in d["message"]


def test_status_off_when_disabled(db, sleep_cfg):
    sleep_cfg.sleep_off_enabled = False
    db.flush()
    res = execute_skill(db, "query_schedule",
                        {"scope": "sleep_off_status", "year": 2026, "month": 8}, _hn())
    assert res.data["적용"] is False and "적용되지 않습니다" in res.data["message"]


def test_status_off_when_code_missing(db, seed_data):
    """설정만 켜고 코드가 없으면 후처리가 아무것도 못 한다 — 그 함정을 드러내야."""
    cfg = db.query(RosterConfig).filter_by(group_id="GRP001").first()
    cfg.sleep_off_enabled = True
    db.flush()
    res = execute_skill(db, "query_schedule",
                        {"scope": "sleep_off_status", "year": 2026, "month": 8}, _hn())
    assert res.data["적용"] is False
    assert "근무코드가 지정돼 있지 않" in res.data["message"]


def test_cycle_falls_back_to_code_default(db, seed_data):
    cfg = db.query(RosterConfig).filter_by(group_id="GRP001").first()
    cfg.sleep_off_cycle = None
    db.flush()
    res = execute_skill(db, "query_schedule",
                        {"scope": "sleep_off_status", "year": 2026, "month": 8}, _hn())
    assert res.data["주기"] == 15 and "기본값" in res.data["주기_출처"]


def test_status_scoped_to_one_nurse(db, sleep_cfg):
    for nid, seq in (("N002", 5), ("N003", 9)):
        db.add(NurseNightCycle(nurse_id=nid, group_id="GRP001", year=2026, month=8,
                               seq_at_end=seq, pending_sleep=0, sleep_off_count=0,
                               sleep_off_seq=1))
    db.flush()
    res = execute_skill(db, "query_schedule",
                        {"scope": "sleep_off_status", "year": 2026, "month": 8,
                         "nurse_name": "이수정"}, _hn())
    assert res.data["간호사수"] == 1 and res.data["nurses"][0]["nurse"] == "이수정"


def test_sleep_off_module_exposes_no_writer():
    """방침: 수치 조작 불가. 쓰기 함수가 슬며시 생기면 이 테스트가 깨진다."""
    banned = [n for n in dir(leave_tools)
              if any(v in n for v in ("upsert", "update", "set_", "write", "rebuild", "delete"))]
    assert banned == [], f"leave_tools 에 쓰기 함수가 생겼다: {banned}"
