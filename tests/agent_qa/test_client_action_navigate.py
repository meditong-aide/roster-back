"""Client-action tools (navigate/prefill) — 수간호사 화면 의도 질의 테스트.

spec: docs/AGENT_CLIENT_ACTION_TOOL_SPEC_2026-05-28.md

검증 축:
- navigate ui_action emit (target/sub 매핑이 spec route 와 일치)
- closed enum: 없는 화면은 ui_action 없이 텍스트
- role-aware: 일반 간호사는 HN 전용 화면 차단
- prefill ui_action
- /agent/test 표면(to_dict)에 ui_actions 노출
"""

from __future__ import annotations

from agents_v2.middleware import _check_permission
from agents_v2.schemas.session_context import SessionContext
from agents_v2.skills.client_actions import (
    build_ui_action,
    command_permission_error,
    is_client_action,
    target_permission_error,
)
from tests.agent_qa.harness import (
    AgentTestSession,
    ScriptedClient,
    ScriptedResponse,
)


# ── helpers ──────────────────────────────────────────────


def _ctx(role: str) -> SessionContext:
    return SessionContext(
        office_id="OFF001", group_id="GRP001", year=2026, month=5,
        nurse_id="N001", nurse_name="김민지", user_role=role,
    )


def _nav_session(db, *, role: str = "HN", name: str = "navigate", **args) -> AgentTestSession:
    """LLM 이 단일 client-action tool 을 부르도록 스크립트한 세션."""
    return AgentTestSession(
        db, user_role=role,
        client=ScriptedClient([
            ScriptedResponse(tool_calls=[{"name": name, "args": args}]),
        ]),
    )


# ── client_actions unit — closed enum / build ────────────


def test_is_client_action():
    assert is_client_action("navigate")
    assert is_client_action("prefill")
    assert is_client_action("switch_ward")
    assert is_client_action("invoke")
    assert not is_client_action("query_schedule")
    assert not is_client_action("manage_team_min")


def test_build_navigate_ok():
    action, err = build_ui_action("navigate", {"target": "nurse_management", "sub": "team_setting"})
    assert err is None
    assert action == {"action": "navigate", "target": "nurse_management", "sub": "team_setting"}


def test_build_navigate_with_query():
    action, err = build_ui_action("navigate", {"target": "roster_view_my", "query": {"month": 5}})
    assert err is None
    assert action == {"action": "navigate", "target": "roster_view_my", "query": {"month": 5}}


def test_build_unknown_target_errors():
    action, err = build_ui_action("navigate", {"target": "payroll"})
    assert action is None
    assert err and "없습니다" in err


def test_build_invalid_sub_errors():
    # wanted 화면엔 섹션이 없음 → team_setting sub 거부
    action, err = build_ui_action("navigate", {"target": "wanted", "sub": "team_setting"})
    assert action is None
    assert err


def test_build_prefill_values():
    action, err = build_ui_action(
        "prefill",
        {"target": "nurse_management", "sub": "team_setting",
         "values": {"team": "A팀", "shift": "나이트", "min_count": 1}},
    )
    assert err is None
    assert action["action"] == "prefill"
    assert action["values"]["team"] == "A팀"
    assert action["values"]["min_count"] == 1


# ── permission (middleware) — role-aware navigation ──────


def test_permission_nurse_blocked_hn_only_target():
    err = _check_permission("navigate", {"target": "nurse_management"}, _ctx("nurse"))
    assert err is not None and ("수간호사" in err or "ADM" in err)


def test_permission_nurse_blocked_config():
    err = _check_permission("navigate", {"target": "config", "sub": "weekoff"}, _ctx("nurse"))
    assert err is not None


def test_permission_nurse_allowed_open_target():
    assert _check_permission("navigate", {"target": "wanted"}, _ctx("nurse")) is None
    assert _check_permission("navigate", {"target": "roster_view_my"}, _ctx("nurse")) is None


def test_permission_hn_allowed_hn_only_target():
    assert _check_permission("navigate", {"target": "nurse_management"}, _ctx("HN")) is None
    assert _check_permission("navigate", {"target": "config"}, _ctx("ADM")) is None


def test_permission_unknown_target_not_a_permission_block():
    # 알 수 없는 target 은 권한 문제 아님(None) — build 단계에서 '화면 없음' 처리.
    assert target_permission_error("payroll", _ctx("nurse")) is None


# ── end-to-end: 수간호사 실제 질의 → navigate ui_action ────


def test_query_team_setting(db):
    # "팀 어디서 바꿔?" → 근무자 관리 / 팀 설정 모달
    res = _nav_session(db, target="nurse_management", sub="team_setting").send("팀 어디서 바꿔?")
    assert res.ui_actions == [{"action": "navigate", "target": "nurse_management", "sub": "team_setting"}]


# [NAV_FIRST_TEAMS 2026-06-19] 조건 없는 팀 목록 발화는 nav 으로 빠지는 carve 가드.
def test_team_list_query_routes_to_team_setting(db):
    res = _nav_session(db, target="nurse_management", sub="team_setting").send("팀 목록 보여줘")
    assert res.ui_actions == [{"action": "navigate", "target": "nurse_management", "sub": "team_setting"}]


def test_team_state_query_routes_to_team_setting(db):
    res = _nav_session(db, target="nurse_management", sub="team_setting").send("우리 병동 팀 어떻게 돼있어?")
    assert res.ui_actions == [{"action": "navigate", "target": "nurse_management", "sub": "team_setting"}]


def test_query_grade_setting(db):
    res = _nav_session(db, target="nurse_management", sub="grade_setting").send("등급 설정 화면 띄워줘")
    assert res.ui_actions == [{"action": "navigate", "target": "nurse_management", "sub": "grade_setting"}]


def test_query_wanted(db):
    res = _nav_session(db, target="wanted").send("원티드 어디서 봐?")
    assert res.ui_actions == [{"action": "navigate", "target": "wanted"}]


def test_query_my_roster(db):
    res = _nav_session(db, target="roster_view_my").send("내 근무표 보여줘")
    assert res.ui_actions == [{"action": "navigate", "target": "roster_view_my"}]


def test_query_roster_create(db):
    res = _nav_session(db, target="roster_create").send("근무표 새로 만들러 가자")
    assert res.ui_actions == [{"action": "navigate", "target": "roster_create"}]


def test_query_dashboard(db):
    res = _nav_session(db, role="nurse", target="dashboard").send("대시보드 보여줘")
    assert res.ui_actions == [{"action": "navigate", "target": "dashboard"}]


def test_navigate_recorded_in_trace(db):
    sess = _nav_session(db, target="wanted")
    sess.send("원티드 보러 가자")
    sess.assert_tool_called("navigate", {"target": "wanted"})


# ── end-to-end: 없는 화면 → ui_action 없이 텍스트 ─────────


def test_unknown_screen_no_ui_action(db):
    res = _nav_session(db, target="payroll").send("급여 정산은 어디서 봐?")
    assert res.ui_actions == []
    assert res.awaiting_approval is False
    assert (res.answer or "").strip() != ""


# ── end-to-end: 일반 간호사 차단 ──────────────────────────


def test_nurse_blocked_e2e(db):
    res = _nav_session(db, role="nurse", target="nurse_management", sub="team_setting").send("팀 설정 띄워줘")
    assert res.ui_actions == []


# ── end-to-end: prefill ───────────────────────────────────


def test_prefill_e2e(db):
    sess = _nav_session(
        db, name="prefill", target="nurse_management", sub="team_setting",
        values={"team": "A팀", "shift": "나이트", "min_count": 1},
    )
    res = sess.send("A팀 나이트 최소 1명으로 바꾸려고")
    assert len(res.ui_actions) == 1
    assert res.ui_actions[0]["action"] == "prefill"
    assert res.ui_actions[0]["values"]["min_count"] == 1


# ── /agent/test 표면(to_dict)에 ui_actions 노출 (US-006) ──


def test_to_dict_includes_ui_actions(db):
    res = _nav_session(db, target="wanted").send("원티드 보러 가자")
    d = res.to_dict()
    assert "ui_actions" in d
    assert d["ui_actions"] == [{"action": "navigate", "target": "wanted"}]


# ── switch_ward — 상단 셀렉터 컨텍스트 전환 ──────────────


def test_build_switch_ward_ok():
    action, err = build_ui_action("switch_ward", {"ward_name": "9A"})
    assert err is None
    assert action == {"action": "switch_ward", "ward_name": "9A"}


def test_build_switch_ward_strips_whitespace():
    action, err = build_ui_action("switch_ward", {"ward_name": "  9B병동  "})
    assert err is None
    assert action == {"action": "switch_ward", "ward_name": "9B병동"}


def test_build_switch_ward_missing_name_errors():
    action, err = build_ui_action("switch_ward", {})
    assert action is None
    assert err and "병동" in err


def test_build_switch_ward_empty_name_errors():
    action, err = build_ui_action("switch_ward", {"ward_name": "  "})
    assert action is None
    assert err


def test_permission_switch_ward_open_to_all_roles():
    # switch_ward 는 백엔드 권한 게이트 없음 — 프론트 셀렉터(접근 가능 ward 목록)가 SSOT.
    assert _check_permission("switch_ward", {"ward_name": "9A"}, _ctx("nurse")) is None
    assert _check_permission("switch_ward", {"ward_name": "9A"}, _ctx("HN")) is None
    assert _check_permission("switch_ward", {"ward_name": "9A"}, _ctx("ADM")) is None


def test_switch_ward_e2e(db):
    res = _nav_session(db, name="switch_ward", ward_name="9A").send("9A병동으로 이동해줘")
    assert res.ui_actions == [{"action": "switch_ward", "ward_name": "9A"}]


def test_switch_ward_recorded_in_trace(db):
    sess = _nav_session(db, name="switch_ward", ward_name="9B")
    sess.send("9B병동으로 바꿔줘")
    sess.assert_tool_called("switch_ward", {"ward_name": "9B"})


# ── roster_create 모달 서브 (2026-07 프론트 개편) ─────────


def test_build_roster_create_modal_sub_ok():
    # '인력 설정 열어줘' → roster_create / manpower 모달
    action, err = build_ui_action("navigate", {"target": "roster_create", "sub": "manpower"})
    assert err is None
    assert action == {"action": "navigate", "target": "roster_create", "sub": "manpower"}


def test_build_roster_create_all_modal_subs_valid():
    for sub in ("manpower", "wanted_config", "deadline", "off_request",
                "quick_config", "emergency", "version"):
        action, err = build_ui_action("navigate", {"target": "roster_create", "sub": sub})
        assert err is None, f"{sub}: {err}"
        assert action["sub"] == sub


def test_build_roster_create_invalid_sub_errors():
    action, err = build_ui_action("navigate", {"target": "roster_create", "sub": "payroll"})
    assert action is None
    assert err and "섹션이 없습니다" in err


def test_roster_create_manpower_e2e(db):
    res = _nav_session(db, target="roster_create", sub="manpower").send("인력 설정 열어줘")
    assert res.ui_actions == [{"action": "navigate", "target": "roster_create", "sub": "manpower"}]


def test_roster_create_sub_hn_only(db):
    # roster_create 는 HN 전용 → 일반 간호사는 모달 서브도 차단
    res = _nav_session(db, role="nurse", target="roster_create", sub="deadline").send("마감일 설정 열어줘")
    assert res.ui_actions == []


# ── invoke — 비파괴 UI 명령 대행 (엑셀 다운로드) ──────────


def test_build_invoke_excel_ok():
    action, err = build_ui_action("invoke", {"command": "excel_download"})
    assert err is None
    assert action == {"action": "invoke", "command": "excel_download"}


def test_build_invoke_with_params():
    action, err = build_ui_action("invoke", {"command": "excel_download", "params": {"month": 5}})
    assert err is None
    assert action["params"] == {"month": 5}


def test_build_invoke_unknown_command_errors():
    # 파괴적/미등록 명령은 emit 불가 (closed enum)
    action, err = build_ui_action("invoke", {"command": "publish"})
    assert action is None
    assert err and "실행 가능한 명령이 아닙니다" in err


def test_permission_invoke_excel_nurse_blocked():
    # excel_download 는 HN 전용
    err = command_permission_error("excel_download", _ctx("nurse"))
    assert err is not None and ("수간호사" in err or "ADM" in err)
    # middleware 경유도 동일
    assert _check_permission("invoke", {"command": "excel_download"}, _ctx("nurse")) is not None


def test_permission_invoke_excel_hn_allowed():
    assert command_permission_error("excel_download", _ctx("HN")) is None
    assert _check_permission("invoke", {"command": "excel_download"}, _ctx("ADM")) is None


def test_permission_invoke_unknown_command_not_a_block():
    # 미등록 command 는 권한 문제 아님(None) — build 단계에서 '실행 불가' 처리.
    assert command_permission_error("publish", _ctx("nurse")) is None


def test_invoke_excel_e2e(db):
    res = _nav_session(db, name="invoke", command="excel_download").send("근무표 엑셀로 다운로드해줘")
    assert res.ui_actions == [{"action": "invoke", "command": "excel_download"}]
