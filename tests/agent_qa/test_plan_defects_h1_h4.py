"""AIDE 계획 실행 결함 H1~H4 (S6) — 계획기 허용 목록·kind 를 코드가 정함·미리보기 지문·프로필별 폴백 도구."""
from __future__ import annotations

import logging

import pytest

from agents_v2.agent_v3 import SchedulingAgent
from agents_v2.llm_client import LLMResponse
from agents_v2.planning.executor import execute_plan
from agents_v2.planning.orchestrate import preview_fingerprint
from agents_v2.planning.plan import Plan, PlanTask, code_kind
from agents_v2.planning.planner import build_plan
from agents_v2.profiles import profile_tool_names, profile_tools
from agents_v2.router import ALL_TOOL_NAMES, route
from agents_v2.schemas.session_context import SessionContext
from agents_v2.skills.descriptions import SKILL_TOOLS
from agents_v2.skills.manifest import SKILL_SPECS, load_manifest_skills, skill
from agents_v2.skills.registry import SKILL_REGISTRY
from tests.agent_qa.harness import AgentTestSession, ScriptedClient, ScriptedResponse

load_manifest_skills()


class _PlannerLLM:
    def __init__(self, text):
        self.text = text

    def chat(self, messages, tools=None, **kw):
        return LLMResponse(type="text", text=self.text)


def _tools(*names):
    return [{"name": n, "description": n, "parameters": {"type": "object", "properties": {}}} for n in names]


def _plan_json(*tasks):
    import json
    return json.dumps({"tasks": [
        {"id": f"t{i + 1}", "skill": s, "args": {}, "deps": ([f"t{i}"] if i else []), "kind": k}
        for i, (s, k) in enumerate(tasks)]})


def _fn(returns=None):
    calls = []

    def fn(db, skill_name, args, ctx):
        calls.append({"skill": skill_name, "args": args})
        return (returns or {}).get(skill_name, {"ok": True})
    fn.calls = calls
    return fn


# ── H1 ──
def test_h1_validate_allowed_rejects_outside_and_normalizes_names():
    plan = Plan([PlanTask("t1", "query-schedule"), PlanTask("t2", "evil_skill", deps=["t1"])])
    with pytest.raises(ValueError):
        plan.validate(allowed={"query_schedule"})
    Plan([PlanTask("t1", "query-schedule")]).validate(allowed={"query_schedule"})  # 하이픈·밑줄 같은 이름
    plan.validate()  # allowed 없으면 지금처럼


def test_h1_build_plan_none_for_unshown_or_missing_skill(caplog):
    shown = _tools("query_schedule", "recommend_candidates")
    ok = _PlannerLLM(_plan_json(("recommend_candidates", "read"), ("query_schedule", "read")))
    assert build_plan(ok, "x", shown) is not None
    # 보여 주지 않은(그러나 있는) 스킬 · 아예 없는 스킬
    for name in ("bulk_mutation", "no_such_skill"):
        bad = _PlannerLLM(_plan_json(("recommend_candidates", "read"), (name, "read")))
        with caplog.at_level(logging.WARNING):
            assert build_plan(bad, "x", shown) is None


def test_h1_executor_denies_out_of_profile_node_and_runs_nothing():
    # schedule-edit 프로필에서 AIDE 전용 스킬(query_schedule) — execute_fn 이 execute_skill 이 아니어도 막힌다
    ctx = SessionContext(office_id="O", group_id="G", year=2026, month=8, nurse_id="N", agent_id="schedule-edit")
    plan = Plan([PlanTask("t1", "query_schedule"), PlanTask("t2", "query_schedule")])
    fn = _fn()
    res = execute_plan(None, plan, ctx, fn)
    assert fn.calls == []
    assert res.failed and res.failed["task"] == "t1"
    assert res.failed["data"]["permission_denied"] is True
    # aide 는 그대로 실행된다
    ctx2 = SessionContext(office_id="O", group_id="G", year=2026, month=8, nurse_id="N")
    fn2 = _fn()
    assert execute_plan(None, plan, ctx2, fn2).failed is None and len(fn2.calls) == 2


# ── H2 ──
def test_h2_code_kind_table():
    assert code_kind("bulk_mutation") == "mutate"      # 옛 @register 스킬 — _MUTATION_SKILLS
    assert code_kind("bulk-mutation") == "mutate"
    assert code_kind("query_schedule") == "read"
    assert code_kind("manage_daily_shift") == "mutate"  # 매니페스트 write
    assert code_kind("no_such_skill") == "read"


def test_h2_executor_forces_preview_only_for_write_skill_written_as_read():
    plan = Plan([PlanTask("t1", "bulk_mutation", kind="read", args={"x": 1})])
    fn = _fn({"bulk_mutation": {"preview": True, "summary": "s"}})
    res = execute_plan(None, plan, None, fn)
    assert fn.calls[0]["args"]["preview_only"] is True
    assert len(res.previews) == 1
    assert plan.tasks[0].kind == "mutate"


def test_h2_planner_overrides_llm_kind_and_logs(caplog):
    shown = _tools("recommend_candidates", "bulk_mutation")
    llm = _PlannerLLM(_plan_json(("recommend_candidates", "mutate"), ("bulk_mutation", "read")))
    with caplog.at_level(logging.INFO):
        plan = build_plan(llm, "x", shown)
    assert [t.kind for t in plan.tasks] == ["read", "mutate"]
    over = [r for r in caplog.records if "덮어씀" in r.message]
    assert len(over) == 2
    # 쓰기로 적었는데 읽기면 warning, 읽기로 적었는데 쓰기(쓰기 스킬의 조회 op 포함)면 info
    assert sorted(r.levelno for r in over) == [logging.INFO, logging.WARNING]


# ── H3 ──
def _prev(data, task="t1"):
    return [{"task": task, "skill": "s", "args": {}, "data": data}]


def test_h3_fingerprint_changes_when_only_details_change():
    a = _prev({"preview": True, "summary": "같음", "changes": [{"after": "A"}]})
    b = _prev({"preview": True, "summary": "같음", "changes": [{"after": "B"}]})
    assert preview_fingerprint(a) != preview_fingerprint(b)


def test_h3_fingerprint_same_state_twice_ignores_volatile_only():
    base = {"preview": True, "summary": "s", "changes": [{"nurse": "김", "ids": {3, 1, 2}}]}
    a = _prev({**base, "elapsed_ms": 12, "generated_at": "t1", "usage": {"input": 1}, "job_id": "a"})
    b = _prev({**base, "elapsed_ms": 99, "generated_at": "t2", "usage": {"input": 7}, "job_id": "b"})
    assert preview_fingerprint(a) == preview_fingerprint(b)
    # DB 행 id(request_id)는 상태이므로 지문에 든다
    c = _prev({**base, "request_id": 1})
    d = _prev({**base, "request_id": 2})
    assert preview_fingerprint(c) != preview_fingerprint(d)


def _agent():
    from unittest.mock import MagicMock
    return SchedulingAgent(MagicMock(), enable_user_memory=False)


_BTN = "h3_button_skill"
_BTN_SCHEMA = {"name": _BTN, "parameters": {"type": "object", "properties": {}}}


@pytest.fixture
def button_skill():
    calls = {"commit": 0, "preview": 0}

    @skill(_BTN, _BTN_SCHEMA, mutation=True, auto_safe=True, commit="button", postcondition=lambda d: True)
    def _h(db, params):
        if params.get("preview_only"):
            calls["preview"] += 1
            return {"preview": True, "summary": "버튼 전용 변경"}
        calls["commit"] += 1
        return {"ok": True}
    yield calls
    SKILL_SPECS.pop(_BTN, None)
    SKILL_REGISTRY.pop(_BTN, None)


def _pending(*tasks):
    return {"type": "plan", "user_message": "m", "plan": {"tasks": list(tasks)}}


def test_h3_button_node_not_run_by_text_approval(db, seed_data, button_skill):
    ctx = SessionContext(office_id="OFF001", group_id="GRP001", year=2026, month=8,
                         nurse_id="N001", nurse_name="김민지", user_role="HN")
    ctx.pending_approval = _pending({"id": "t1", "skill": _BTN, "kind": "mutate", "args": {}})
    res = _agent().run(db, "응", ctx)
    assert button_skill == {"commit": 0, "preview": 0}      # 다시 실행도 안 한다
    assert "버튼" in res.answer
    assert ctx.pending_approval is None


def test_h3_auto_mode_does_not_autocommit_button_skill(db, seed_data, button_skill):
    sess = AgentTestSession(db, client=ScriptedClient([
        ScriptedResponse(tool_calls=[{"name": _BTN, "args": {"preview_only": True}}]),
    ]))
    sess.ctx.autonomy_mode = "auto"
    res = sess.send("변경해줘")
    assert res.awaiting_approval is True
    assert button_skill["commit"] == 0


# ── H4 ──
class _FailingRouter:
    def chat(self, messages, tools=None, **kw):
        raise RuntimeError("down")


def test_h4_schedule_edit_fallback_is_edit_tools_only():
    names = route(_FailingRouter(), "아무 말", agent_id="schedule-edit").tool_names
    assert names and names == profile_tool_names("schedule-edit")
    assert not set(names) & set(ALL_TOOL_NAMES)
    assert profile_tools("schedule-edit") == [t for t in profile_tools("schedule-edit") if t["name"] in names]


def test_h4_aide_fallback_is_unchanged():
    r1 = route(_FailingRouter(), "아무 말")
    r2 = route(_FailingRouter(), "아무 말", agent_id="aide")
    assert r1.fallback_used and r1.tool_names == list(ALL_TOOL_NAMES) == r2.tool_names
    assert profile_tools("aide") is SKILL_TOOLS
    assert profile_tools("nope") == [] and route(_FailingRouter(), "x", agent_id="nope").tool_names == []


class _SeeMain:
    def __init__(self):
        self.counts = []

    def chat(self, messages, tools=None, *, tool_choice="auto", **kw):
        self.counts.append(len(tools or []))
        return LLMResponse(type="text", text="확인")


class _Router:
    def chat(self, messages, tools=None, **kw):
        return LLMResponse(type="text", text="[]")


def test_h4_agent_empty_scope_falls_back_to_profile_tools(db, seed_data):
    for agent_id, expected in (("aide", len(SKILL_TOOLS)), ("schedule-edit", len(profile_tools("schedule-edit")))):
        main = _SeeMain()
        agent = SchedulingAgent(main, enable_user_memory=False, router_llm=_Router())
        ctx = SessionContext(office_id="OFF001", group_id="GRP001", year=2026, month=8,
                             nurse_id="N001", nurse_name="김민지", user_role="HN", agent_id=agent_id)
        ctx.awaited_reply = True
        ctx.last_route = {"categories": ["read"], "tool_names": ["존재하지_않는_도구"]}
        agent.run(db, "응", ctx)
        assert main.counts and main.counts[-1] == expected, agent_id


# ── 검토 지적 반영(S6 재검토) ──
def test_h2_read_op_of_write_skill_is_not_a_preview():
    # 쓰기 스킬의 조회 op 는 kind=read 로 적어도 코드 판정은 mutate(preview_only 주입) — 결과가 미리보기가 아니면 승인 대상이 아니다
    plan = Plan([PlanTask("t1", "manage_teams", args={"operation": "read"}, kind="read"),
                 PlanTask("t2", "query_schedule", deps=["t1"])])
    fn = _fn({"manage_teams": {"teams": ["A"]}})
    res = execute_plan(None, plan, None, fn)
    assert fn.calls[0]["args"]["preview_only"] is True
    assert res.previews == [] and res.outputs["t1"] == {"teams": ["A"]}
    fn2 = _fn({"manage_teams": {"preview": True, "summary": "개명"}})
    res2 = execute_plan(None, Plan([PlanTask("t1", "manage_teams", args={"operation": "rename"})]), None, fn2)
    assert len(res2.previews) == 1


def test_h2_kind_overwritten_before_validate():
    plan = Plan.from_dict({"tasks": [{"id": "t1", "skill": "bulk_mutation", "kind": "write"}]})
    res = execute_plan(None, plan, None, _fn({"bulk_mutation": {"preview": True}}))
    assert res.failed is None and plan.tasks[0].kind == "mutate"


@pytest.mark.parametrize("name", ["generate_schedule", "generate-schedule"])
def test_h2_deferred_write_node_is_previewed_not_run(name):
    plan = Plan([PlanTask("t1", "query_schedule"), PlanTask("t2", name, deps=["t1"])])
    fn = _fn({name: {"preview": True, "summary": "생성"}})
    res = execute_plan(None, plan, None, fn)
    assert [t.id for t in res.deferred] == ["t2"]
    assert [p["task"] for p in res.previews] == ["t2"]
    assert fn.calls[-1]["args"]["preview_only"] is True
    fn2 = _fn()
    res2 = execute_plan(None, plan, None, fn2, dry_run_mutations=False)
    assert [c["skill"] for c in fn2.calls] == ["query_schedule"] and [t.id for t in res2.deferred] == ["t2"]


def test_h1_precheck_rejects_ctx_without_agent_id_but_skips_no_ctx():
    plan = Plan([PlanTask("t1", "query_schedule")])
    ctx = SessionContext(office_id="O", group_id="G", year=2026, month=8, nurse_id="N")
    ctx.agent_id = None
    fn = _fn()
    res = execute_plan(None, plan, ctx, fn)
    assert fn.calls == [] and res.failed["data"]["permission_denied"] is True
    fn2 = _fn()
    assert execute_plan(None, plan, None, fn2).failed is None and len(fn2.calls) == 1


def test_h3_fingerprint_survives_non_str_keys():
    from datetime import date
    a = _prev({"preview": True, "by_day": {1: "D", "total": 3, date(2026, 8, 1): "N", (1, 2): "E"}})
    assert preview_fingerprint(a) == preview_fingerprint(a)
    assert len(preview_fingerprint(a)) == 64


def test_h3_button_skill_not_run_by_react_text_approval(db, seed_data, button_skill):
    for mode in ("manual", "auto"):
        sess = AgentTestSession(db, client=ScriptedClient([
            ScriptedResponse(tool_calls=[{"name": _BTN, "args": {"preview_only": True}}]),
        ]))
        sess.ctx.autonomy_mode = mode
        assert sess.send("변경해줘").awaiting_approval is True
        res = sess.send("응")
        assert button_skill["commit"] == 0, mode
        assert "버튼" in res.answer


def test_h3_button_skill_in_batch_blocks_whole_batch(db, seed_data, button_skill):
    ctx = SessionContext(office_id="OFF001", group_id="GRP001", year=2026, month=8,
                         nurse_id="N001", nurse_name="김민지", user_role="HN")
    ctx.pending_approval = {"type": "batch", "items": [
        {"skill_name": _BTN, "args": {}}, {"skill_name": "bulk_mutation", "args": {}}]}
    res = _agent().run(db, "응", ctx)
    assert button_skill["commit"] == 0 and "버튼" in res.answer and "다른 변경도" in res.answer


_PLAIN = "h3_plain_skill"


def test_h3_mixed_plan_no_false_staleness_and_reports_dropped(db, seed_data, button_skill):
    from agents_v2.middleware import execute_skill

    done = {"commit": 0}

    @skill(_PLAIN, {"name": _PLAIN, "parameters": {"type": "object", "properties": {}}},
           mutation=True, postcondition=lambda d: True)
    def _p(db, params):
        if params.get("preview_only"):
            return {"preview": True, "summary": "일반 변경"}
        done["commit"] += 1
        return {"ok": True}

    try:
        ctx = SessionContext(office_id="OFF001", group_id="GRP001", year=2026, month=8,
                             nurse_id="N001", nurse_name="김민지", user_role="HN")
        tasks = [{"id": "t1", "skill": _PLAIN, "kind": "mutate", "args": {}},
                 {"id": "t2", "skill": _BTN, "kind": "mutate", "args": {}}]
        from agents_v2.planning.orchestrate import preview_fingerprint as fp
        run = execute_plan(db, Plan.from_dict({"tasks": tasks}), ctx, execute_skill)
        ctx.pending_approval = {"type": "plan", "user_message": "m", "plan": {"tasks": tasks},
                                "preview_fp": fp(run.previews)}
        res = SchedulingAgent(_SeeMain(), enable_user_memory=False).run(db, "응", ctx)
        assert [st.name for st in res.trace] == ["plan_commit"]
        assert done["commit"] == 1 and button_skill["commit"] == 0
        assert _BTN in res.answer and "제외" in res.answer
    finally:
        SKILL_SPECS.pop(_PLAIN, None)
        SKILL_REGISTRY.pop(_PLAIN, None)


def test_h3_button_only_plan_with_reads_runs_nothing_and_guides(db, seed_data, button_skill):
    # 버튼 노드를 빼고 나면 읽기만 남는 계획 — 커밋할 것이 없어 읽기도 다시 돌리지 않고 버튼 안내만 한다(읽기 결과는 승인 전 미리보기 때 이미 보였다)
    reads = {"n": 0}
    _READ = "h3_read_counter"

    @skill(_READ, {"name": _READ, "parameters": {"type": "object", "properties": {}}}, postcondition=lambda d: True)
    def _r(db, params):
        reads["n"] += 1
        return {"rows": []}

    try:
        ctx = SessionContext(office_id="OFF001", group_id="GRP001", year=2026, month=8,
                             nurse_id="N001", nurse_name="김민지", user_role="HN")
        ctx.pending_approval = {"type": "plan", "user_message": "m", "preview_fp": "x", "plan": {"tasks": [
            {"id": "t1", "skill": _READ, "kind": "read", "args": {}},
            {"id": "t2", "skill": _BTN, "kind": "mutate", "args": {}, "deps": ["t1"]}]}}
        res = _agent().run(db, "응", ctx)
        assert reads["n"] == 0 and button_skill == {"commit": 0, "preview": 0}
        assert [(st.name, st.status) for st in res.trace] == [("plan_commit", "block")]
        assert "버튼" in res.answer and _BTN in res.answer
        assert ctx.pending_approval is None
    finally:
        SKILL_SPECS.pop(_READ, None)
        SKILL_REGISTRY.pop(_READ, None)


def test_h4_router_called_only_for_profiles_with_router():
    class _CountRouter:
        def __init__(self):
            self.n = 0

        def chat(self, messages, tools=None, **kw):
            self.n += 1
            return LLMResponse(type="text", text="[]")

    for agent_id, expected in (("aide", 1), ("schedule-edit", 0), ("nope", 0)):
        router = _CountRouter()
        agent = SchedulingAgent(_SeeMain(), enable_user_memory=False, router_llm=router)
        ctx = SessionContext(office_id="OFF001", group_id="GRP001", year=2026, month=8,
                             nurse_id="N001", nurse_name="김민지", user_role="HN", agent_id=agent_id)
        agent.run(None, "안녕", ctx)
        assert router.n == expected, agent_id


def test_h4_agent_main_tools_follow_profile_in_every_branch(db, seed_data):
    class _R:
        def __init__(self, text):
            self.text = text

        def chat(self, messages, tools=None, **kw):
            return LLMResponse(type="text", text=self.text)

    own = {t["name"] for t in profile_tools("schedule-edit")}
    for agent_id, rtext, use_router in (("schedule-edit", None, False), ("schedule-edit", "[]", True),
                                        ("schedule-edit", '["analyze"]', True)):
        main = _SeeMain()
        seen = []
        orig_chat = main.chat

        def chat(messages, tools=None, _o=orig_chat, _s=seen, **kw):
            _s.append({t["name"] for t in (tools or [])})
            return _o(messages, tools, **kw)
        main.chat = chat
        agent = SchedulingAgent(main, enable_user_memory=False, router_llm=_R(rtext) if use_router else None)
        ctx = SessionContext(office_id="OFF001", group_id="GRP001", year=2026, month=8,
                             nurse_id="N001", nurse_name="김민지", user_role="HN", agent_id=agent_id)
        agent.run(db, "안녕", ctx)
        assert seen and seen[-1] == own, (rtext, seen[-1] ^ own)
    # aide 는 그대로
    main = _SeeMain()
    SchedulingAgent(main, enable_user_memory=False).run(
        db, "안녕", SessionContext(office_id="OFF001", group_id="GRP001", year=2026, month=8,
                                 nurse_id="N001", nurse_name="김민지", user_role="HN"))
    assert main.counts[-1] == len(SKILL_TOOLS)
