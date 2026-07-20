"""Plan Executor — 위상순서·참조치환·dry-run·실패중단 (mock execute_fn, LLM 무관)."""
from agents_v2.planning.plan import Plan, PlanTask
from agents_v2.planning.executor import execute_plan


def _mock(returns):
    """skill 이름/호출 기록 + 지정 반환값 주는 execute_fn."""
    calls = []
    def fn(db, skill, args, ctx):
        calls.append({"skill": skill, "args": args})
        return returns.get(skill, {"ok": True})
    fn.calls = calls
    return fn


def test_topo_order_and_ref_substitution():
    # t1(read) → 후보 반환, t2(mutate) 는 $t1 참조
    plan = Plan([
        PlanTask("t1", "recommend_candidates", kind="read"),
        PlanTask("t2", "bulk_mutation", kind="mutate",
                 deps=["t1"], args={"nurse": "$t1.candidates[0].name", "date": "2026-05-03"}),
    ])
    fn = _mock({"recommend_candidates": {"candidates": [{"name": "김민지"}]},
                "bulk_mutation": {"preview": True}})
    res = execute_plan(None, plan, None, fn)
    assert res.order == ["t1", "t2"]
    # t2 args 에 t1 출력이 치환됐고 dry-run 주입됨
    assert fn.calls[1]["args"]["nurse"] == "김민지"
    assert fn.calls[1]["args"]["preview_only"] is True
    # mutate 는 previews 로 수집(승인 대상)
    assert len(res.previews) == 1 and res.previews[0]["task"] == "t2"
    assert res.failed is None


def test_parallel_level_both_run():
    plan = Plan([PlanTask("t1", "query_schedule", kind="read"),
                 PlanTask("t2", "analyze_report", kind="read")])
    fn = _mock({})
    res = execute_plan(None, plan, None, fn)
    assert set(res.order) == {"t1", "t2"} and res.failed is None


def test_failure_short_circuits():
    # t1 실패 → t2 실행 안 됨
    plan = Plan([PlanTask("t1", "recommend_candidates", kind="read"),
                 PlanTask("t2", "bulk_mutation", kind="mutate", deps=["t1"])])
    fn = _mock({"recommend_candidates": {"error": "대상 없음"}})
    res = execute_plan(None, plan, None, fn)
    assert res.failed is not None and res.failed["task"] == "t1"
    assert res.order == ["t1"]  # t2 스킵
    assert len(fn.calls) == 1
