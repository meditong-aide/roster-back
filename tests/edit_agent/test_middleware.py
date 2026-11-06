"""AIDE 와 같은 도구 형식(사용자 지시 11-06) — 정의 한 목록 · 이름 등록 레지스트리 · 권한 → 문맥 주입 → 이름 확정 → 실행 →
기록 단계 · 등록 목록은 수정 도우미 전용(AIDE 스킬을 열지 않는다) · 예시는 LLM 없이 그 도구를 부른다(토큰 0)."""

from __future__ import annotations

import json
from dataclasses import fields
from datetime import date

import pytest

from agent_kit.base import TurnInput
from agent_kit.runtime import TurnError
from schedule_edit_agent import registry
from schedule_edit_agent.descriptions import BUTTON_TOOLS, SKILL_MODES, SKILL_TOOLS
from schedule_edit_agent.examples import example_calls
from schedule_edit_agent.grounding import ground_changes
from schedule_edit_agent.middleware import UNKNOWN_ACTION, UNKNOWN_TOOL, MiddlewareStep, SkillResult, execute_skill
from schedule_edit_agent.prompt import SYSTEM, build_messages
from services.schedule_edit.requests import first_open_day
from tests.edit_agent.test_agent import A_OFF, USER, _agent, call, say
from tests.edit_agent.test_router import api  # noqa: F401 — 경로 시험 틀(fixture)
from tests.schedule_edit.test_solver import BEFORE_MONTH, make_ctx


def _stages(payload):
    return [(s["name"], s["status"]) for s in payload["steps"]]


# ── 형식 ────────────────────────────────────────────────────────────────────


def test_steps_and_results_have_the_aide_shape():
    # 복제한 단계 기록이 AIDE 와 같은 칸·같은 dict 인지 — AIDE 쪽이 바뀌면 여기서 알린다(import 대신 복제한 근거)
    from agents_v2 import middleware as aide
    assert [f.name for f in fields(MiddlewareStep)] == [f.name for f in fields(aide.MiddlewareStep)]
    assert [f.name for f in fields(SkillResult)] == [f.name for f in fields(aide.SkillResult)]
    for args in (("permission", "pass"), ("grounding", "grounded", "nurse=1개", {"a": 1}, {"b": 2})):
        assert MiddlewareStep(*args).to_dict() == aide.MiddlewareStep(*args).to_dict()


def test_registry_is_edit_only_and_matches_the_definitions():
    from agents_v2.skills import registry as aide_registry
    aide_registry._ensure_loaded()
    ours = set(registry.SKILL_REGISTRY)
    assert registry.definition_gaps() == []
    assert not ours & set(aide_registry.SKILL_REGISTRY)                 # AIDE 등록 목록에 섞지 않는다
    assert not {"query_schedule", "bulk_mutation", "update_constraint", "generate_schedule",
                "update_person_attr", "repair_schedule"} & ours        # AIDE 스킬은 열지 않는다
    assert [t["name"] for t in SKILL_TOOLS] == ["propose_edit", "revise_edit", "reply_only", "answer_confirm", "lookup",
                                                "check_rules", "run_postprocess", "compare_versions", "revert_version"]
    assert [n for n, m in SKILL_MODES.items() if m == "write"] == ["apply_edit"]     # 쓰기는 적용 하나 — 버튼
    assert "apply_edit" in {b["name"] for b in BUTTON_TOOLS}
    assert registry.resolve("propose-edit") == "propose_edit" and registry.resolve("nope") is None   # AIDE 정규화


def test_revise_add_reuses_the_change_shape_without_descriptions():
    tools = {t["name"]: t for t in SKILL_TOOLS}
    add = tools["revise_edit"]["parameters"]["properties"]["add"]
    change = tools["propose_edit"]["parameters"]["properties"]["changes"]["items"]

    def bare(x):
        if isinstance(x, dict):
            return {k: bare(v) for k, v in x.items() if k != "description"}
        return [bare(v) for v in x] if isinstance(x, list) else x

    assert add["description"] == "더할 요청 — propose_edit 의 changes 항목과 같은 모양·뜻"
    assert add["items"] == bare(change) and "description" not in json.dumps(add["items"])
    assert add["items"]["required"] == ["kind", "nurse"] and add["items"]["properties"]["kind"]["enum"]


def test_the_fixed_part_goes_first_and_holds_no_date(world):
    # 도구 정의·SYSTEM 은 매 턴 같은 글자로 맨 앞 — 오늘·상태는 대화 뒤 상태 요약에만(프롬프트 캐시가 깨지지 않게)
    agent, _ = _agent()
    session = agent.session_for(None, world.conv)
    first = build_messages(world.conv, session, "x", date(2026, 11, 2))
    later = build_messages(world.conv, session, "y", date(2026, 11, 20))
    assert first[0] == later[0] == {"role": "system", "content": SYSTEM}
    assert "2026" not in SYSTEM and "2026-" not in json.dumps(SKILL_TOOLS, ensure_ascii=False)
    assert "2026-11-02" in first[-2]["content"] and first[-1] == {"role": "user", "content": "x"}


# ── 단계 ────────────────────────────────────────────────────────────────────


def test_each_stage_is_recorded_with_the_first_result(world):
    agent, _ = _agent(call("propose_edit", changes=[A_OFF], edit_from_day=1, schedule_id="다른 근무표"))
    out = say(agent, world.conv, "A 1일 오프")
    assert out.preview["status"] == "ok"
    message, lock, proposed = world.events
    assert (message.event, lock.event, proposed.event) == ("message", "lock", "propose")
    assert "steps" not in message.payload and "steps" not in lock.payload          # 첫날 기록은 첫날만
    assert _stages(proposed.payload) == [("permission", "pass"), ("context_injection", "injected"),
                                         ("grounding", "grounded"), ("execution", "ok")]
    context = proposed.payload["steps"][1]["detail"]
    assert "schedule_id=s1" in context and "고칠 수 있는 첫날=1일" in context
    assert "버린 인자=schedule_id" in context            # 세션이 아는 값은 LLM 인자로 받지 않는다
    assert proposed.payload["steps"][2]["detail"] == "requests=1개"


def test_an_unknown_name_stops_at_grounding(world):
    agent, _ = _agent(call("propose_edit", changes=[{"kind": "set", "nurse": "없는사람", "day": 1, "shift": "OFF"}]))
    say(agent, world.conv, "없는사람 1일 오프")
    clarify = world.events[-1]
    assert clarify.event == "clarify" and world.conv.state.last is None
    assert _stages(clarify.payload)[-1] == ("grounding", "clarification_needed")


def test_the_llm_cannot_pick_a_button_or_write(world):
    agent, _ = _agent(call("apply_edit", rank=1))
    out = say(agent, world.conv, "1안 적용해")
    assert out.answer == UNKNOWN_TOOL and out.trail == "keep" and [e.event for e in world.events] == ["message"]


@pytest.mark.parametrize("name,source,answer,failed", [
    ("apply_edit", "example", UNKNOWN_TOOL, False),      # 예시는 LLM 이 고를 수 있는 도구만 — 쓰기 불가
    ("propose_edit", "button", UNKNOWN_ACTION, True),    # 버튼 경로로 LLM 도구를 부르지 못한다
    ("query_schedule", "llm", UNKNOWN_TOOL, False),      # AIDE 스킬 이름은 없다
])
def test_permission_blocks_by_source_and_mode(world, name, source, answer, failed):
    agent, _ = _agent()
    turn = agent._turn(None, USER, world.conv, BEFORE_MONTH, None)
    result = execute_skill(turn, name, {"rank": 1}, source=source)
    assert result.blocked and _stage_of(result) == ("permission", "block")
    assert (result.data.answer, result.data.failed) == (answer, failed) and world.events == []


def _stage_of(result):
    step = result.middleware_steps[-1]
    return step.name, step.status


# ── 예시 = 해석이 붙은 말(LLM 없음) ─────────────────────────────────────────


def test_an_example_runs_its_tool_without_the_llm(world):
    agent, llm = _agent()                              # 답을 넣지 않았다 — LLM 을 부르면 꺼낼 답이 없어 터진다
    out = say(agent, world.conv, "A 1일 오프로", call=("propose_edit", {"changes": [A_OFF]}))
    assert out.preview["status"] == "ok" and llm.calls == []
    message = world.events[0]
    assert message.event == "message" and message.payload["text"] == "A 1일 오프로"       # 사용자가 한 말로
    assert (message.payload["tool"], message.payload["source"]) == ("propose_edit", "example")
    assert out.trail == "new" and out.request_line == "A 1일 OFF"
    assert _stages(world.events[-1].payload)[0] == ("permission", "pass")


def test_examples_carry_a_call_that_fits_the_tool_and_grounds():
    ctx = make_ctx({"A": "DDEENNOO", "B": "EEDDOONN", "C": "OONNDDEE"})
    ctx.names.update({"A": "김가람", "B": "이나래", "C": "박다솜"})
    ctx.leave_codes = {"연": "연차"}
    today = date(2026, 11, 2)
    tools = {t["name"]: t["parameters"] for t in SKILL_TOOLS}
    pairs = example_calls(ctx, today)
    assert [tool for _t, (tool, _a) in pairs].count("propose_edit") == 8
    for text, (tool, args) in pairs:
        assert registry.kind_of(tool) == "tool" and SKILL_MODES[tool] != "write"
        _fits(args, tools[tool])
        if tool == "propose_edit":
            grounded = ground_changes(ctx, args["changes"], first_open_day(ctx, today), today=today)
            assert grounded.asks == [] and grounded.requests, text


def _fits(value, schema):
    """도구 정의의 모양(칸 이름·필수·enum·종류)에 맞는지 — 작은 검사기(jsonschema 가 없다)."""
    kind = schema.get("type")
    if kind == "object":
        assert isinstance(value, dict) and set(value) <= set(schema.get("properties") or {}), value
        assert set(schema.get("required") or []) <= set(value), value
        for key, sub in value.items():
            _fits(sub, schema["properties"][key])
    elif kind == "array":
        assert isinstance(value, list) and all(_fits(v, schema["items"]) is None for v in value)
    elif kind == "integer":
        assert isinstance(value, int)
    elif kind == "string":
        assert isinstance(value, str)
    if "enum" in schema:
        assert value in schema["enum"], value


def test_text_with_a_button_or_unknown_action_is_refused_before_the_turn():
    from schedule_edit_agent.spec import ScheduleEditAgent
    agent = ScheduleEditAgent(lambda: None)
    agent.check_input(TurnInput(kind="text", text="A 1일 오프로", action="propose_edit", params={}))
    for action in ("apply_edit", "query_schedule"):
        with pytest.raises(TurnError) as err:
            agent.check_input(TurnInput(kind="text", text="x", action=action, params={}))
        assert err.value.code == "bad_action"


def test_clicking_an_example_is_a_said_turn_without_the_llm(api, monkeypatch):  # noqa: F811 — 가져온 fixture
    from schedule_edit_agent import spec as spec_mod
    from tests.edit_agent.test_router import URL, block, code
    made = []
    monkeypatch.setattr(spec_mod, "live_example_calls", lambda db, s, today: made.append(s) or [
        ("A 1일 오프로", ("propose_edit", {"changes": [A_OFF]})), ("○○○ 14일 오프로", None)])
    intro = api.client.get(f"{URL}/availability", params={"schedule_id": "s1"}).json()["intro"]
    examples = block(intro, "examples")
    # 옛 화면 호환(검토 M1) — items 는 늘 문장 목록, 도구 호출은 같은 자리의 calls 칸(틀 예시는 null)
    assert examples["items"] == ["A 1일 오프로", "○○○ 14일 오프로"]
    assert examples["calls"] == [{"action": "propose_edit", "params": {"changes": [A_OFF]}}, None]
    said = {"kind": "text", "text": examples["items"][0], **examples["calls"][0]}       # 새 화면이 보내는 모양
    resp = api.client.post(f"{URL}/turn", json={"conversation_id": None, "context": {"schedule_id": "s1"},
                                                "input": said})
    body = resp.json()
    assert resp.status_code == 200 and api.agent.core.llm.calls == []
    assert body["said"] == {"kind": "text", "text": "A 1일 오프로"}
    assert block(body, "trail")["request"] == "A 1일 OFF"
    log = api.client.get(f"{URL}/conversations/{body['conversation_id']}").json()
    message = log["turns"][0]["payload"]
    assert (message["source"], message["text"], message["example_verified"]) == ("example", "A 1일 오프로", True)
    assert len(made) == 1                     # 처음 화면에 낸 목록으로 확인했다(다시 만들지 않는다)
    # 서버가 만든 예시와 다른 호출 — 실행은 같게(LLM 인자와 같은 신뢰), 기록은 사실대로 false
    tampered = {**said, "params": {"changes": [{**A_OFF, "day": 2}]}}
    again = api.client.post(f"{URL}/turn", json={"conversation_id": body["conversation_id"],
                                                 "context": {"schedule_id": "s1"}, "input": tampered})
    assert again.status_code == 200
    log = api.client.get(f"{URL}/conversations/{body['conversation_id']}").json()
    messages = [t["payload"] for t in log["turns"] if t["event"] == "message"]
    assert [m["example_verified"] for m in messages] == [True, False]
    refused = api.client.post(f"{URL}/turn", json={"conversation_id": body["conversation_id"],
                                                   "context": {"schedule_id": "s1"},
                                                   "input": {"kind": "text", "text": "적용", "action": "apply_edit",
                                                             "params": {"rank": 1}}})
    assert refused.status_code == 400 and code(refused) == "bad_action"


def test_an_example_is_checked_against_a_rebuilt_list_after_a_restart(api, monkeypatch):  # noqa: F811
    # 처음 화면 목록이 없으면(재기동·다른 서버) 한 번 다시 만들어 확인한다 — 그 뒤로는 다시 만들지 않는다
    from schedule_edit_agent import spec as spec_mod
    from tests.edit_agent.test_router import URL
    made = []
    monkeypatch.setattr(spec_mod, "live_example_calls", lambda db, s, today: made.append(s.schedule_id) or [
        ("A 1일 오프로", ("propose_edit", {"changes": [A_OFF]}))])
    said = {"kind": "text", "text": "A 1일 오프로", "action": "propose_edit", "params": {"changes": [A_OFF]}}
    first = api.client.post(f"{URL}/turn", json={"conversation_id": None, "context": {"schedule_id": "s1"},
                                                 "input": said}).json()
    api.client.post(f"{URL}/turn", json={"conversation_id": first["conversation_id"], "context": {"schedule_id": "s1"},
                                         "input": said})
    log = api.client.get(f"{URL}/conversations/{first['conversation_id']}").json()
    assert [t["payload"]["example_verified"] for t in log["turns"] if t["event"] == "message"] == [True, True]
    assert made == ["s1"]


def test_a_refused_skill_records_the_path_it_came_from(world, monkeypatch):
    # 스킬이 인자를 거절하면(SkillError) 오류 기록의 단계는 부른 곳 그대로(검토 L2 — 늘 'button' 이었다)
    from schedule_edit_agent.skills import SkillError

    def refuse(turn, params):
        raise SkillError("지금 상태와 안 맞습니다.")

    monkeypatch.setitem(registry.SKILL_REGISTRY, "lookup", refuse)
    agent, _ = _agent(call("lookup", kind="day_shift", day=1, shift="D"))
    out = say(agent, world.conv, "1일 데이 누구야")
    assert out.answer == "지금 상태와 안 맞습니다." and world.events[-1].event == "error"
    assert world.events[-1].payload["stage"] == "llm" and _stages(world.events[-1].payload)[-1] == ("execution", "error")
    say(agent, world.conv, "1일 데이 누구야", call=("lookup", {"kind": "day_shift", "day": 1, "shift": "D"}))
    assert world.events[-1].payload["stage"] == "example"
