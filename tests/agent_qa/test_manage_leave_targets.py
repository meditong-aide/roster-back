"""manage_leave_targets — 휴가 자동부여 대상 3-state 조회·설정 + read-back.

dev 신규 서비스(nurse_leave_period)의 에이전트 노출. 검증 축:
  ① 3-state 저장/되읽기 · ② 미전송과 '자동'(None) 구분 · ③ 자동판정 실효 결과 노출
  ④ read-back 이 거짓완료를 잡는가 · ⑤ 매니페스트 배선(스키마/카테고리/권한).
"""
from datetime import date

from agents_v2.errors import ErrorType, classify
from agents_v2.middleware import execute_skill
from agents_v2.schemas.session_context import SessionContext
from db.models import Nurse, NurseLeavePeriod
from services.leave import leave_eligibility


def _hn():
    return SessionContext(office_id="OFF001", group_id="GRP001", year=2026, month=8,
                          nurse_id="N001", nurse_name="김민지", user_role="HN")


def _set(**over):
    p = {"operation": "set", "nurse_name": "박지은", "preview_only": False}
    p.update(over)
    return p


def _row_of(data: dict, name: str) -> dict:
    return next(r for r in data["rows"] if r["nurse"] == name)


# ── set / list 왕복 ─────────────────────────────────────


def test_set_writes_period_and_reads_back(db, seed_data):
    res = execute_skill(db, "manage_leave_targets", _set(health_leave="제외"), _hn())
    assert res.data.get("ok") is True and res.data.get("verification_failed") is not True

    rows = db.query(NurseLeavePeriod).filter_by(nurse_id="N002").all()
    assert len(rows) == 1
    assert rows[0].health_leave_eligible is False
    assert rows[0].valid_from == date(2026, 8, 1)  # 판정 단위가 '월' → 대상월 1일 발효


def test_set_preview_does_not_persist(db, seed_data):
    res = execute_skill(db, "manage_leave_targets",
                        _set(health_leave="포함", preview_only=True), _hn())
    assert res.data.get("preview") is True
    assert classify(res.data) is ErrorType.PREVIEW
    assert res.data["summary"]["changes"]["보건휴가"] == {"old": "자동", "new": "포함"}
    assert db.query(NurseLeavePeriod).filter_by(nurse_id="N002").count() == 0


def test_unsent_fields_are_not_reset(db, seed_data):
    """미전송과 '자동'(None) 을 가른다 — 보건휴가만 바꿔도 임신 설정이 살아있어야."""
    execute_skill(db, "manage_leave_targets", _set(pregnant="포함"), _hn())
    execute_skill(db, "manage_leave_targets", _set(health_leave="제외"), _hn())

    flags = leave_eligibility.fetch_leave_flags(db, ["N002"], 2026, 8)["N002"]
    assert flags["pregnant"] is True          # 안 보낸 항목은 승계
    assert flags["health_leave_eligible"] is False


def test_auto_returns_to_rule(db, seed_data):
    """'자동' 은 명시적 None — 예외를 걷어내 규칙 판정으로 되돌린다."""
    execute_skill(db, "manage_leave_targets", _set(sleep_off="제외"), _hn())
    assert leave_eligibility.fetch_leave_flags(db, ["N002"], 2026, 8)["N002"]["sleep_off_eligible"] is False

    execute_skill(db, "manage_leave_targets", _set(sleep_off="자동"), _hn())
    flags = leave_eligibility.fetch_leave_flags(db, ["N002"], 2026, 8).get("N002") or {}
    assert flags.get("sleep_off_eligible") is None


def test_bad_state_word_rejected(db, seed_data):
    res = execute_skill(db, "manage_leave_targets", _set(health_leave="켜줘"), _hn())
    assert "error" in res.data


def test_set_without_values_asks_for_field(db, seed_data):
    res = execute_skill(db, "manage_leave_targets", _set(), _hn())
    assert "error" in res.data and "hint" in res.data


def test_set_without_nurse_clarifies(db, seed_data):
    res = execute_skill(db, "manage_leave_targets",
                        {"operation": "set", "health_leave": "제외", "preview_only": False}, _hn())
    assert res.data.get("needs_clarification") is True


# ── list: 자동판정 실효 결과 ────────────────────────────


def test_list_shows_effective_result_not_just_state(db, seed_data):
    """'자동' 만 보여주면 결국 대상인지 모른다 — 실효 결과가 붙어야."""
    db.query(Nurse).filter_by(nurse_id="N002").update({"gender": "여"})
    db.flush()
    res = execute_skill(db, "manage_leave_targets", {"operation": "list"}, _hn())
    row = _row_of(res.data, "박지은")
    assert row["보건휴가"] == "자동 → 대상"      # 여성 · N전담 아님 · 고정근무 아님
    assert row["수면오프"] == "자동 → 대상"      # 수면OFF 자동판정은 전원
    assert row["임신"] == "미설정"


def test_list_auto_reflects_gender_rule(db, seed_data):
    """성별 미설정/남성이면 자동판정상 보건휴가 비대상."""
    res = execute_skill(db, "manage_leave_targets", {"operation": "list"}, _hn())
    assert _row_of(res.data, "박지은")["보건휴가"] == "자동 → 비대상"


def test_manual_include_overrides_auto(db, seed_data):
    """고정근무자(자동 비대상)라도 '포함' 이면 대상 — 실무 49.3% 케이스."""
    db.query(Nurse).filter_by(nurse_id="N002").update({"gender": "여", "fixed_shift": "M"})
    db.flush()
    res = execute_skill(db, "manage_leave_targets", {"operation": "list"}, _hn())
    assert _row_of(res.data, "박지은")["보건휴가"] == "자동 → 비대상"

    execute_skill(db, "manage_leave_targets", _set(health_leave="포함"), _hn())
    res = execute_skill(db, "manage_leave_targets", {"operation": "list"}, _hn())
    assert _row_of(res.data, "박지은")["보건휴가"] == "포함(수동) → 대상"


def test_pregnant_suppresses_health_leave(db, seed_data):
    """임신 중이면 명시값이 없는 한 보건휴가 자동판정을 덮는다."""
    db.query(Nurse).filter_by(nurse_id="N002").update({"gender": "여"})
    db.flush()
    execute_skill(db, "manage_leave_targets", _set(pregnant="포함"), _hn())
    row = _row_of(execute_skill(db, "manage_leave_targets", {"operation": "list"}, _hn()).data, "박지은")
    assert row["보건휴가"] == "자동 → 비대상"
    assert row["임신"] == "임신 중"


def test_list_filter_manual_only(db, seed_data):
    execute_skill(db, "manage_leave_targets", _set(health_leave="제외"), _hn())
    res = execute_skill(db, "manage_leave_targets",
                        {"operation": "list", "filter": "수동설정"}, _hn())
    assert res.data["count"] == 1 and res.data["rows"][0]["nurse"] == "박지은"


def test_list_single_nurse_scope(db, seed_data):
    res = execute_skill(db, "manage_leave_targets",
                        {"operation": "list", "nurse_name": "김민지"}, _hn())
    assert res.data["count"] == 1 and res.data["rows"][0]["nurse"] == "김민지"


# ── as-of: 미래월 stale 컬럼 함정 ───────────────────────


def test_auto_uses_asof_period_not_stale_column(db, seed_data):
    """고정근무가 대상월부터 풀리면 그 달 자동판정은 '대상' 이어야 한다.

    allowed_shifts/fixed_shift 컬럼은 as-of-TODAY 캐시라 미래월에 stale 하다.
    """
    from services.nurse_period_resolver import upsert_period
    from db.models import NurseAllowedShiftPeriod

    db.query(Nurse).filter_by(nurse_id="N002").update({"gender": "여", "fixed_shift": "M"})
    db.flush()
    # 2026-08-01 부터 고정근무 해제 — 컬럼(오늘값)은 여전히 'M'
    upsert_period(db, NurseAllowedShiftPeriod, "N002", date(2026, 8, 1),
                  "fixed_shift", None, source="edited", carry_attrs=["allowed_shifts"])
    db.commit()

    res = execute_skill(db, "manage_leave_targets",
                        {"operation": "list", "year": 2026, "month": 8}, _hn())
    assert _row_of(res.data, "박지은")["보건휴가"] == "자동 → 대상"


# ── read-back ───────────────────────────────────────────


def test_readback_catches_false_complete(db, seed_data, monkeypatch):
    """upsert 를 no-op 로 만들면 ok 를 보고해도 VERIFICATION_FAILED 로 승격돼야."""
    import agents_v2.skills.manage_leave_targets as mlt

    monkeypatch.setattr(mlt, "upsert_leave_period", lambda *a, **k: None)
    res = execute_skill(db, "manage_leave_targets", _set(health_leave="제외"), _hn())
    assert res.data.get("verification_failed") is True
    assert classify(res.data) is ErrorType.VERIFICATION_FAILED


# ── 매니페스트 배선 ─────────────────────────────────────


def test_manifest_wiring():
    from agents_v2.router import CATEGORY_TOOLS
    from agents_v2.skills.descriptions import SKILL_TOOLS
    from agents_v2.skills.manifest import SKILL_SPECS, load_manifest_skills

    load_manifest_skills()
    spec = SKILL_SPECS["manage_leave_targets"]
    assert spec.mutation and spec.hn_only and spec.trigger_hint
    assert {"settings_people", "mutate"} <= set(spec.categories)
    names = {t["name"] if "name" in t else t["function"]["name"] for t in SKILL_TOOLS}
    assert "manage_leave_targets" in names
    for cat in ("settings_people", "mutate"):
        assert "manage_leave_targets" in CATEGORY_TOOLS[cat]


def test_general_nurse_blocked(db, seed_data):
    ctx = SessionContext(office_id="OFF001", group_id="GRP001", year=2026, month=8,
                         nurse_id="N002", nurse_name="박지은", user_role="RN")
    res = execute_skill(db, "manage_leave_targets", _set(health_leave="제외"), ctx)
    assert "error" in res.data
    assert db.query(NurseLeavePeriod).filter_by(nurse_id="N002").count() == 0


def test_missing_table_on_set_is_reported_not_swallowed(db, seed_data, monkeypatch):
    """조회는 테이블 부재를 '전원 자동판정'으로 삼키지만 저장은 삼키면 안 된다.

    저장했다고 답하고 아무 일도 안 일어나는 게 최악이다(거짓완료).
    """
    import agents_v2.skills.manage_leave_targets as mlt

    def _boom(*a, **k):
        raise RuntimeError("Invalid object name 'nurse_leave_period'.")

    monkeypatch.setattr(mlt, "upsert_leave_period", _boom)
    # 검증 대상은 '에러 메시지 매핑' 이다. 실제 session.rollback() 은 db fixture 의
    # 바깥 트랜잭션까지 끊어 teardown 을 깨므로 여기서만 무력화한다.
    monkeypatch.setattr(db, "rollback", lambda: None)
    res = execute_skill(db, "manage_leave_targets", _set(health_leave="제외"), _hn())
    assert "error" in res.data and "nurse_leave_period" in res.data["error"]
    assert res.data.get("ok") is not True
