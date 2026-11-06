"""manage_grade 검증 — 수간호사가 실제 쓰는 자연어 쿼리로 설정이 바뀌는지 확인.

근무표 생성은 하지 않는다. 흐름: 자연어 쿼리 → preview → "응" 확인 → 적용,
그리고 적용 후 get_grade_config_service 로 DB 설정이 실제로 바뀌었는지 검증.

ScriptedClient 는 LLM 이 각 자연어 명령을 어떻게 tool_call 로 해석하는지를 미리 정의한다
(외부 LLM 없이 결정론적). 검증의 초점은 '설정 변화'다.
"""

from __future__ import annotations

from agents_v2.skills.manage_grade import manage_grade
from agents_v2.skills.registry import _ensure_loaded
from services.grade_service import get_grade_config_service

from tests.agent_qa.harness import AgentTestSession, ScriptedClient, ScriptedResponse
from tests.agent_qa.test_manage_grade_skill import _cell, _seed_grade_config

_ensure_loaded()


def _tc(args: dict) -> ScriptedResponse:
    return ScriptedResponse(tool_calls=[{"name": "manage_grade", "args": args}])


# preview tool_call 직후 agent 가 _generate_preview_answer 로 LLM 을 한 번 더
# 호출(요약 생성)하므로, mutation 마다 [tool_call, 요약 텍스트] 쌍이 필요하다.
_PREVIEW_TEXT = ScriptedResponse(text="진행하시겠습니까? (응 / 취소)")


def _preview_seq(*arg_dicts: dict) -> list[ScriptedResponse]:
    seq: list[ScriptedResponse] = []
    for args in arg_dicts:
        seq.append(_tc(args))
        seq.append(_PREVIEW_TEXT)
    return seq


def _confirm_apply(sess: AgentTestSession, query: str) -> None:
    """수간호사가 명령 → 미리보기 확인 → '응' 으로 승인하는 1 사이클."""
    r1 = sess.send(query)
    assert r1.awaiting_approval, f"미리보기 단계가 떠야 함: {query!r}"
    sess.send("응")
    # 테스트 harness 는 승인 후 pending_approval 을 자동 비우지 않으므로 수동 정리.
    sess.ctx.pending_approval = None


def test_hn_session_settings_actually_change(db, seed_data):
    """수간호사 한 세션 동안 여러 등급 정책을 바꾸고, 매번 DB 반영을 확인."""
    _seed_grade_config(db, constraints={"D": {"1": 1}})  # 이름(주니어..수간호사) + D/주니어=1 baseline

    cli = ScriptedClient(_preview_seq(
        {"operation": "set_requirement", "shift_name": "나이트", "grade_name": "시니어",
         "min_count": 2, "preview_only": True},
        {"operation": "set_requirement", "shift_name": "데이", "grade_name": "주니어",
         "min_count": 1, "preview_only": True},
        {"operation": "set_requirement", "shift_name": "나이트", "grade_name": "주니어",
         "max_count": 1, "preview_only": True},
        {"operation": "set_soft_fallback", "soft_enabled": True, "preview_only": True},
    ))
    sess = AgentTestSession(db, client=cli, user_role="HN")

    # 1) "나이트에 시니어 최소 2명은 꼭 넣어줘"
    _confirm_apply(sess, "나이트에 시니어 최소 2명은 꼭 넣어줘")
    cfg = get_grade_config_service(db, "GRP001")
    assert _cell(cfg.constraints, "N", 2) == 2
    assert _cell(cfg.constraints, "D", 1) == 1  # 기존 보존

    # 2) "데이에 주니어 한 명은 있어야 해"
    _confirm_apply(sess, "데이에 주니어 한 명은 있어야 해")
    cfg = get_grade_config_service(db, "GRP001")
    assert _cell(cfg.constraints, "D", 1) == 1
    assert _cell(cfg.constraints, "N", 2) == 2  # 앞 변경 누적 보존

    # 3) "야간에 주니어는 최대 1명만"
    _confirm_apply(sess, "야간에 주니어는 최대 1명만")
    cfg = get_grade_config_service(db, "GRP001")
    assert _cell(cfg.constraints_max, "N", 1) == 1

    # 4) "등급 때문에 근무표가 안 짜지면 좀 느슨하게 해줘"
    assert get_grade_config_service(db, "GRP001").allow_soft_fallback is False
    _confirm_apply(sess, "등급 때문에 근무표가 안 짜지면 좀 느슨하게 해줘")
    assert get_grade_config_service(db, "GRP001").allow_soft_fallback is True


def test_hn_soft_then_strict_roundtrip(db, seed_data):
    """완화 켰다가 다시 엄격으로 — 토글이 양방향으로 반영되는지."""
    _seed_grade_config(db, soft=True)
    cli = ScriptedClient(_preview_seq(
        {"operation": "set_soft_fallback", "soft_enabled": False, "preview_only": True},
    ))
    sess = AgentTestSession(db, client=cli, user_role="HN")
    _confirm_apply(sess, "등급 제약 다시 꼭 지켜줘")
    assert get_grade_config_service(db, "GRP001").allow_soft_fallback is False


def test_hn_rename_grade_reflected(db, seed_data):
    """'1등급을 신규로' → grade_names 반영 + 기존 이름 보존."""
    _seed_grade_config(db)
    cli = ScriptedClient(_preview_seq(
        {"operation": "set_grade_name", "target_grade_name": "1",
         "new_name": "신규", "preview_only": True},
    ))
    sess = AgentTestSession(db, client=cli, user_role="HN")
    _confirm_apply(sess, "1등급을 신규로 바꿔줘")
    cfg = get_grade_config_service(db, "GRP001")
    assert cfg.grade_names["1"] == "신규"
    assert cfg.grade_names["2"] == "시니어"


def test_hn_read_then_change_then_read(db, seed_data):
    """조회 → 변경 → 재조회로 사용자가 변화를 납득할 수 있는지(읽기에 반영)."""
    _seed_grade_config(db, constraints={"N": {"2": 1}})  # 나이트/시니어 최소 1
    # 변경 전 read (직접 호출 — 라벨 확인)
    before = manage_grade(db, {"operation": "read", "group_id": "GRP001"})
    assert {"shift": "나이트", "grade": "시니어", "min_count": 1} in before["min_requirements"]

    cli = ScriptedClient(_preview_seq(
        {"operation": "set_requirement", "shift_name": "나이트", "grade_name": "시니어",
         "min_count": 3, "preview_only": True},
    ))
    sess = AgentTestSession(db, client=cli, user_role="HN")
    _confirm_apply(sess, "나이트 시니어 최소 3명으로 올려줘")

    after = manage_grade(db, {"operation": "read", "group_id": "GRP001"})
    assert {"shift": "나이트", "grade": "시니어", "min_count": 3} in after["min_requirements"]
    # 읽기 출력에 등급 '번호'나 raw JSON 키가 섞이지 않는다.
    assert "constraints_json" not in after
    for item in after["min_requirements"]:
        assert isinstance(item["grade"], str) and item["grade"] != ""


def test_hn_remove_min_requirement(db, seed_data):
    """'나이트 시니어 최소 인원 제한 없애줘' → min_count=0 으로 해제 반영."""
    _seed_grade_config(db, constraints={"N": {"2": 2}})
    cli = ScriptedClient(_preview_seq(
        {"operation": "set_requirement", "shift_name": "나이트", "grade_name": "시니어",
         "min_count": 0, "preview_only": True},
    ))
    sess = AgentTestSession(db, client=cli, user_role="HN")
    _confirm_apply(sess, "나이트 시니어 최소 인원 제한 없애줘")
    cfg = get_grade_config_service(db, "GRP001")
    assert _cell(cfg.constraints, "N", 2) == 0
