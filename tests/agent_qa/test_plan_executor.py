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


# ── read 병렬(세션 격리) + mutate 순차 + plain-dict 병합 ──
class _FakeSession:
    def __init__(self): self.closed = False
    def close(self): self.closed = True


def test_reads_parallel_isolated_and_merge():
    passed_db = object()
    seen = {}
    def fn(db, skill, args, ctx):
        seen[skill] = db
        if skill == "recommend_candidates":
            return {"candidates": [{"name": "이수정"}]}
        if skill == "query_schedule":
            return {"count": 6}
        if skill == "bulk_mutation":
            return {"preview": True, "got_nurse": args.get("nurse"), "got_count": args.get("cnt")}
        return {"ok": True}
    sessions = []
    def factory():
        s = _FakeSession(); sessions.append(s); return s

    # t1,t2 병렬 read → t3 mutate 가 둘 다 참조
    plan = Plan([
        PlanTask("t1", "recommend_candidates", kind="read"),
        PlanTask("t2", "query_schedule", kind="read"),
        PlanTask("t3", "bulk_mutation", kind="mutate", deps=["t1", "t2"],
                 args={"nurse": "$t1.candidates[0].name", "cnt": "$t2.count"}),
    ])
    res = execute_plan(passed_db, plan, None, fn, session_factory=factory)

    # 병렬 read 는 fresh·서로 다른 세션(공유 db 아님)
    assert seen["recommend_candidates"] is not passed_db
    assert seen["query_schedule"] is not passed_db
    assert seen["recommend_candidates"] is not seen["query_schedule"]
    # mutate 는 공유 db 순차
    assert seen["bulk_mutation"] is passed_db
    # plain-dict 병합 + 참조 치환(양쪽 read 출력이 t3 로 합쳐짐)
    assert res.outputs["t3"]["got_nurse"] == "이수정"
    assert res.outputs["t3"]["got_count"] == 6
    # 병렬 세션 전부 닫힘(누수 없음)
    assert sessions and all(s.closed for s in sessions)
    assert res.failed is None and len(res.previews) == 1


def test_no_factory_stays_sequential():
    # session_factory 없으면 전부 공유 db(하위호환)
    passed_db = object()
    seen = []
    def fn(db, skill, args, ctx):
        seen.append(db); return {"ok": True}
    plan = Plan([PlanTask("t1", "query_schedule", kind="read"),
                 PlanTask("t2", "analyze_report", kind="read")])
    execute_plan(passed_db, plan, None, fn)  # factory 없음
    assert all(d is passed_db for d in seen)


# ── async(generate) defer — 트랜잭션 밖·커밋 후 ──
def test_async_skill_deferred_not_executed():
    calls = []
    def fn(db, skill, args, ctx):
        calls.append(skill)
        return {"ok": True}
    plan = Plan([PlanTask("t1", "manage_daily_shift", kind="mutate"),
                 PlanTask("t2", "generate_schedule", kind="mutate", deps=["t1"])])
    res = execute_plan(None, plan, None, fn, dry_run_mutations=False)
    # generate 는 실행 안 되고 deferred 로 수집
    assert "generate_schedule" not in calls
    assert "manage_daily_shift" in calls
    assert [t.id for t in res.deferred] == ["t2"]
    assert res.failed is None
