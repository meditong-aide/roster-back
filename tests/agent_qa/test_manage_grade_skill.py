"""manage_grade skill — 등급 정책 조회/수정 + grounding + preview/confirm + 권한."""

from __future__ import annotations

from agents_v2.middleware import _check_permission
from agents_v2.schemas.session_context import SessionContext
from agents_v2.skills.manage_grade import manage_grade
from agents_v2.skills.registry import SKILL_REGISTRY, _ensure_loaded
from schemas.grade_schema import GradeConfigUpsert
from services.grade_service import get_grade_config_service, upsert_grade_config_service

from tests.agent_qa.harness import AgentTestSession, ScriptedClient, ScriptedResponse

_ensure_loaded()

_GRADE_NAMES = {"1": "주니어", "2": "시니어", "3": "수석", "4": "수간호사"}


def _seed_grade_config(db, *, constraints=None, constraints_max=None, names=True, soft=False):
    """seed_data 그룹에 baseline RosterGradeConfig 를 만든다."""
    kwargs = {"allow_soft_fallback": soft}
    if names:
        kwargs["grade_names"] = dict(_GRADE_NAMES)
    if constraints is not None:
        kwargs["constraints"] = constraints
    if constraints_max is not None:
        kwargs["constraints_max"] = constraints_max
    upsert_grade_config_service(
        db, "OFF001", "GRP001", GradeConfigUpsert(**kwargs), "N001"
    )


def _cell(constraints: dict, shift: str, grade: int):
    """문자/정수 키 혼재에 안전하게 (shift, grade) 셀 값을 읽는다."""
    gmap = constraints.get(shift) or {}
    for k, v in gmap.items():
        try:
            if int(k) == grade:
                return v
        except (TypeError, ValueError):
            continue
    return None


# ── registry ─────────────────────────────────────────────


def test_manage_grade_registered():
    assert "manage-grade" in SKILL_REGISTRY or "manage_grade" in SKILL_REGISTRY


# ── read ─────────────────────────────────────────────────


def test_read_empty_returns_structure(db, seed_data):
    res = manage_grade(db, {"operation": "read", "group_id": "GRP001"})
    assert res["soft_fallback_enabled"] is False
    assert res["min_requirements"] == []
    assert res["max_requirements"] == []


def test_read_uses_names_not_ids_or_json(db, seed_data):
    _seed_grade_config(db, constraints={"D": {"1": 1}, "N": {"2": 2}})
    res = manage_grade(db, {"operation": "read", "group_id": "GRP001"})
    # grade 번호(id)나 raw JSON 키가 아니라 등급 '이름'으로 표현
    assert {"shift": "데이", "grade": "주니어", "min_count": 1} in res["min_requirements"]
    assert {"shift": "나이트", "grade": "시니어", "min_count": 2} in res["min_requirements"]
    assert "constraints_json" not in res
    assert "주니어" in res["grades"] and "시니어" in res["grades"]


# ── set_requirement: preview / merge ─────────────────────


def test_set_requirement_preview_does_not_persist(db, seed_data):
    _seed_grade_config(db, constraints={"D": {"1": 1}})
    res = manage_grade(db, {
        "operation": "set_requirement", "group_id": "GRP001", "office_id": "OFF001",
        "acting_user_id": "N001", "shift_name": "나이트", "grade_name": "시니어",
        "min_count": 2, "preview_only": True,
    })
    assert res["preview"] is True
    assert res["changes"][0]["after"] == 2
    # DB 미반영 — N/시니어 셀이 없어야 함
    cfg = get_grade_config_service(db, "GRP001")
    assert _cell(cfg.constraints, "N", 2) is None


def test_set_requirement_apply_merges_existing(db, seed_data):
    _seed_grade_config(db, constraints={"D": {"1": 1}})
    manage_grade(db, {
        "operation": "set_requirement", "group_id": "GRP001", "office_id": "OFF001",
        "acting_user_id": "N001", "shift_name": "나이트", "grade_name": "시니어",
        "min_count": 2, "preview_only": False,
    })
    cfg = get_grade_config_service(db, "GRP001")
    # 기존 D/주니어 보존 + N/시니어 추가
    assert _cell(cfg.constraints, "D", 1) == 1
    assert _cell(cfg.constraints, "N", 2) == 2


def test_set_requirement_max_anti_pair(db, seed_data):
    _seed_grade_config(db)
    manage_grade(db, {
        "operation": "set_requirement", "group_id": "GRP001", "office_id": "OFF001",
        "acting_user_id": "N001", "shift_name": "나이트", "grade_name": "주니어",
        "max_count": 1, "preview_only": False,
    })
    cfg = get_grade_config_service(db, "GRP001")
    assert _cell(cfg.constraints_max, "N", 1) == 1


# ── grade_name grounding ─────────────────────────────────


def test_grade_name_unset_clarifies(db, seed_data):
    # grade_names 미설정 (names=False) → "시니어" 해석 불가 → 재질의
    _seed_grade_config(db, constraints={"D": {"1": 1}}, names=False)
    res = manage_grade(db, {
        "operation": "set_requirement", "group_id": "GRP001", "office_id": "OFF001",
        "acting_user_id": "N001", "shift_name": "나이트", "grade_name": "시니어",
        "min_count": 2, "preview_only": True,
    })
    assert res.get("needs_clarification") is True


def test_clarification_lists_roster_grades_with_counts(db, seed_data):
    # 설정이 비어 있어도 실제 간호사 명부(grade 1~4)에서 후보를 뽑고 인원수를 보여준다.
    # seed_data 간호사 grade 분포: 1→1명, 2→2명, 3→2명, 4→1명.
    _seed_grade_config(db, names=False)  # 이름 미설정
    res = manage_grade(db, {
        "operation": "set_requirement", "group_id": "GRP001", "office_id": "OFF001",
        "acting_user_id": "N001", "shift_name": "나이트", "grade_name": "시니어",
        "min_count": 2, "preview_only": True,
    })
    assert res.get("needs_clarification") is True
    opts = res["options"]
    assert "2등급 (2명)" in opts
    assert "1등급 (1명)" in opts
    assert len(opts) == 4  # 명부의 grade 1~4 전부


def test_clarification_uses_names_when_set(db, seed_data):
    # 이름이 설정돼 있으면 후보를 이름으로(인원수 포함) 보여준다.
    _seed_grade_config(db)  # 주니어/시니어/수석/수간호사
    res = manage_grade(db, {
        "operation": "set_requirement", "group_id": "GRP001", "office_id": "OFF001",
        "acting_user_id": "N001", "shift_name": "나이트", "grade_name": "없는등급",
        "min_count": 2, "preview_only": True,
    })
    assert res.get("needs_clarification") is True
    assert "시니어 (2명)" in res["options"]


def test_echoed_option_label_resolves(db, seed_data):
    # 사용자가 옵션 라벨을 그대로 되돌려줘도("시니어 (2명)") 괄호 표기를 떼고 해석.
    _seed_grade_config(db)
    res = manage_grade(db, {
        "operation": "set_requirement", "group_id": "GRP001", "office_id": "OFF001",
        "acting_user_id": "N001", "shift_name": "나이트", "grade_name": "시니어 (2명)",
        "min_count": 2, "preview_only": True,
    })
    assert res["preview"] is True
    assert res["changes"][0]["grade"] == "시니어"


def test_read_includes_grade_distribution(db, seed_data):
    _seed_grade_config(db)
    res = manage_grade(db, {"operation": "read", "group_id": "GRP001"})
    dist = {d["grade"]: d["nurse_count"] for d in res["grade_distribution"]}
    assert dist["시니어"] == 2
    assert dist["수간호사"] == 1


def test_hierarchy_phrase_not_interpreted(db, seed_data):
    # 위계 없음 — "맨 위" 같은 순서 표현은 임의 해석 금지 → 재질의
    _seed_grade_config(db)
    res = manage_grade(db, {
        "operation": "set_requirement", "group_id": "GRP001", "office_id": "OFF001",
        "acting_user_id": "N001", "shift_name": "나이트", "grade_name": "맨 위",
        "min_count": 2, "preview_only": True,
    })
    assert res.get("needs_clarification") is True


def test_numeric_grade_accepted(db, seed_data):
    _seed_grade_config(db)
    res = manage_grade(db, {
        "operation": "set_requirement", "group_id": "GRP001", "office_id": "OFF001",
        "acting_user_id": "N001", "shift_name": "데이", "grade_name": "2",
        "min_count": 1, "preview_only": True,
    })
    assert res["preview"] is True
    assert res["changes"][0]["grade"] == "시니어"  # 번호→이름 라벨


# ── set_soft_fallback ────────────────────────────────────


def test_set_soft_fallback_apply(db, seed_data):
    _seed_grade_config(db, soft=False)
    manage_grade(db, {
        "operation": "set_soft_fallback", "group_id": "GRP001", "office_id": "OFF001",
        "acting_user_id": "N001", "soft_enabled": True, "preview_only": False,
    })
    cfg = get_grade_config_service(db, "GRP001")
    assert cfg.allow_soft_fallback is True


# ── set_grade_name ───────────────────────────────────────


def test_set_grade_name_apply_preserves_others(db, seed_data):
    _seed_grade_config(db)
    manage_grade(db, {
        "operation": "set_grade_name", "group_id": "GRP001", "office_id": "OFF001",
        "acting_user_id": "N001", "target_grade_name": "1", "new_name": "신규",
        "preview_only": False,
    })
    cfg = get_grade_config_service(db, "GRP001")
    assert cfg.grade_names["1"] == "신규"
    assert cfg.grade_names["2"] == "시니어"  # 기존 보존


# ── permission ───────────────────────────────────────────


def _ctx(role: str) -> SessionContext:
    return SessionContext(
        office_id="OFF001", group_id="GRP001", year=2026, month=5,
        nurse_id="N001", nurse_name="김민지", user_role=role,
    )


def test_permission_nurse_blocked_for_mutation():
    err = _check_permission("manage_grade", {"operation": "set_requirement"}, _ctx("nurse"))
    assert err is not None and ("수간호사" in err or "ADM" in err)


def test_permission_nurse_blocked_for_read():
    # 등급(역량) 정보는 일반 간호사에게 노출 금지 — read 도 HN/ADM 전용.
    err = _check_permission("manage_grade", {"operation": "read"}, _ctx("nurse"))
    assert err is not None and ("수간호사" in err or "ADM" in err)


def test_permission_hn_allowed_for_mutation():
    err = _check_permission("manage_grade", {"operation": "set_soft_fallback", "soft_enabled": True}, _ctx("HN"))
    assert err is None


# ── e2e dispatch ─────────────────────────────────────────


def test_agent_dispatches_manage_grade(db, seed_data):
    cli = ScriptedClient([
        ScriptedResponse(tool_calls=[{
            "name": "manage_grade",
            "args": {
                "operation": "set_requirement",
                "shift_name": "나이트",
                "grade_name": "시니어",
                "min_count": 2,
                "preview_only": True,
            },
        }])
    ])
    _seed_grade_config(db)
    sess = AgentTestSession(db, client=cli, user_role="HN")
    sess.send("나이트에 시니어 최소 2명은 꼭 넣어줘")
    sess.assert_tool_called("manage_grade", {"operation": "set_requirement"})
    sess.assert_awaiting_approval()
