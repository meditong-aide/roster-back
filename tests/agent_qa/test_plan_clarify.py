"""다중 clarify 배칭 — collect 모드로 독립 브랜치 되물음까지 한 번에 수집 + clarify_form.

mock execute_fn (LLM 무관). needs_clarification=True → CLARIFICATION 분류.
"""
from agents_v2.planning.executor import execute_plan
from agents_v2.planning.orchestrate import apply_clarify_answers, build_clarify_form
from agents_v2.planning.plan import Plan, PlanTask
from agents_v2.skills.descriptions import SKILL_TOOLS


def _fn(returns):
    def fn(db, skill, args, ctx):
        return returns.get(skill, {"ok": True})
    return fn


def test_collect_two_independent_clarifications():
    # 독립 두 mutate 가 각각 되물음 → 첫 것에서 안 멈추고 둘 다 수집
    plan = Plan([
        PlanTask("t1", "manage_assignment", kind="mutate", args={"operation": "transfer"}),
        PlanTask("t2", "update_monthly_limit", kind="mutate", args={}),
    ])
    fn = _fn({
        "manage_assignment": {"needs_clarification": True, "question": "파견 시작일?", "options": []},
        "update_monthly_limit": {"needs_clarification": True, "question": "누구?", "options": ["김민지", "박혜미"]},
    })
    res = execute_plan(None, plan, None, fn, collect_clarifications=True)
    assert res.failed is None, "clarify 는 halt 아님"
    assert len(res.clarifications) == 2
    skills = {c["skill"] for c in res.clarifications}
    assert skills == {"manage_assignment", "update_monthly_limit"}


def test_dependent_of_clarify_is_skipped():
    # t1 되물음 → t2(deps t1) 는 스킵(출력 없음)
    plan = Plan([
        PlanTask("t1", "recommend_candidates", kind="read"),
        PlanTask("t2", "bulk_mutation", kind="mutate", deps=["t1"]),
    ])
    fn = _fn({"recommend_candidates": {"needs_clarification": True, "question": "어느 날?"}})
    res = execute_plan(None, plan, None, fn, collect_clarifications=True)
    assert len(res.clarifications) == 1
    assert "t1" in res.outputs and "t2" not in res.outputs  # t2 스킵


def test_collect_off_stops_at_first_clarification():
    # 기본(collect off) = 기존 동작: 첫 clarify 에서 halt(failed), 미수집
    plan = Plan([PlanTask("t1", "x", kind="read"), PlanTask("t2", "y", kind="read")])
    fn = _fn({"x": {"needs_clarification": True, "question": "q"}})
    res = execute_plan(None, plan, None, fn)  # collect_clarifications=False
    assert res.failed is not None and res.failed["task"] == "t1"
    assert res.clarifications == []


def test_build_clarify_form_shapes():
    # plan/skill_tools 없으면 스킬이 준 options 로만(fallback)
    form = build_clarify_form([
        {"task": "t1", "skill": "s", "question": "q1", "options": ["a", "b"]},
        {"task": "t2", "skill": "s2", "question": "q2", "options": []},
    ])
    assert form["type"] == "clarify_form"
    q1, q2 = form["questions"]
    assert q1["type"] == "select" and q1["options"] == ["a", "b"]
    assert q2["type"] == "input" and q2["options"] == []


def test_form_enriches_enum_from_schema():
    # 스키마 기반 보강: 누락된 required enum 파라미터 → select + enum 옵션(스킬 코드 수정 없이)
    plan = Plan([PlanTask("t1", "manage_assignment", kind="mutate", args={})])  # operation 누락
    clar = [{"task": "t1", "skill": "manage_assignment", "question": "뭘 할까요?", "options": []}]
    form = build_clarify_form(clar, plan=plan, ctx=None, skill_tools=SKILL_TOOLS, db=None)
    q = form["questions"][0]
    assert q["param"] == "operation" and q["type"] == "select"
    assert "create" in q["options"] and "cancel" in q["options"]


def test_form_ctx_injected_params_not_asked():
    # year/month 는 ctx 주입이라 안 물음 → operation 만
    plan = Plan([PlanTask("t1", "manage_wanted_deadline", kind="mutate", args={})])
    clar = [{"task": "t1", "skill": "manage_wanted_deadline", "question": "?", "options": []}]
    form = build_clarify_form(clar, plan=plan, ctx=None, skill_tools=SKILL_TOOLS, db=None)
    params = {q["param"] for q in form["questions"]}
    assert "operation" in params and "year" not in params and "month" not in params


def test_apply_clarify_answers_merges():
    pd = {"tasks": [{"id": "t1", "skill": "manage_assignment", "args": {}}]}
    apply_clarify_answers(pd, [{"task": "t1", "param": "operation", "value": "create"}])
    assert pd["tasks"][0]["args"]["operation"] == "create"
    # param=None(자유입력 fallback)은 무시
    apply_clarify_answers(pd, [{"task": "t1", "param": None, "value": "x"}])
    assert "operation" in pd["tasks"][0]["args"] and len(pd["tasks"][0]["args"]) == 1
