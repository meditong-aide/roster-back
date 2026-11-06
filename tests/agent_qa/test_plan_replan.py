"""Replan (LLMCompiler) — dry-run 실패 시 실패 관찰을 planner 에 피드백해 1회 교정.

mock LLM/execute_fn 으로 결정적 검증(라이브 무관). preview 단계라 mutate=preview_only → 안전.
"""
from agents_v2.planning.orchestrate import try_plan_run, _replan_feedback, PlanRun
from agents_v2.planning.executor import execute_plan
from agents_v2.planning.plan import Plan, PlanTask


class _Resp:
    def __init__(self, text): self.text = text


class _FakeLLM:
    """chat() 호출마다 미리 준 plan JSON 을 순서대로 반환. feedback 여부 기록."""
    def __init__(self, scripts):
        self._scripts = list(scripts)
        self.calls = []  # (had_feedback, user_content)

    def chat(self, messages, tools=None):
        user = messages[-1]["content"]
        self.calls.append(("직전 계획 실행 실패" in user, user))
        return _Resp(self._scripts.pop(0) if self._scripts else '{"tasks": []}')


# 두 스킬: query_schedule(read, 항상 OK), bulk_mutation(mutate).
# bulk_mutation 은 args 에 nurse 있으면 OK, 없으면 error → 실패 유도.
def _exec(db, skill, args, ctx):
    if skill == "bulk_mutation":
        if not args.get("nurse"):
            return {"error": "nurse 파라미터 누락"}
        return {"preview": True, "summary": {"nurse": args["nurse"]}}
    return {"ok": True, "candidates": [{"name": "이수정"}]}


_TOOLS = [
    {"name": "recommend_candidates", "description": "대체자 추천",
     "parameters": {"properties": {"date": {"description": "일자"}}}},
    {"name": "bulk_mutation", "description": "일괄 변경",
     "parameters": {"properties": {"nurse": {"description": "간호사명"}}}},
]

# 1차 plan: t2 가 nurse 없이 mutate → 실패. 2차(교정): nurse=$t1 참조 → 성공.
_BAD = ('{"tasks":[{"id":"t1","skill":"recommend_candidates","kind":"read","args":{"date":"2026-05-03"}},'
        '{"id":"t2","skill":"bulk_mutation","kind":"mutate","deps":["t1"],"args":{}}]}')
_GOOD = ('{"tasks":[{"id":"t1","skill":"recommend_candidates","kind":"read","args":{"date":"2026-05-03"}},'
         '{"id":"t2","skill":"bulk_mutation","kind":"mutate","deps":["t1"],'
         '"args":{"nurse":"$t1.candidates[0].name"}}]}')


def test_replan_corrects_failed_plan():
    llm = _FakeLLM([_BAD, _GOOD])
    run = try_plan_run(None, "5/3 대체자 찾아 배정", None, llm, _TOOLS, _exec, max_replans=1)
    assert run is not None
    assert run.failed is False, "교정 plan 이 성공해야"
    # planner 2번 호출: 1차(피드백X), 2차(피드백O)
    assert len(llm.calls) == 2
    assert llm.calls[0][0] is False and llm.calls[1][0] is True
    # 교정 plan 은 nurse 참조 치환돼 preview 생성
    assert run.exec.previews and run.exec.previews[0]["data"]["summary"]["nurse"] == "이수정"


def test_replan_bounded_then_gives_up():
    # 항상 BAD → max_replans=1 이면 planner 최대 2회(원본+1교정) 후 실패 run 반환
    llm = _FakeLLM([_BAD, _BAD, _BAD])
    run = try_plan_run(None, "x", None, llm, _TOOLS, _exec, max_replans=1)
    assert run is not None and run.failed is True  # 여전히 실패 → 호출부가 ReAct
    assert len(llm.calls) == 2, "원본1 + 교정1 = 2회로 bounded"


def test_replan_disabled_when_max_zero():
    llm = _FakeLLM([_BAD])
    run = try_plan_run(None, "x", None, llm, _TOOLS, _exec, max_replans=0)
    assert run is not None and run.failed is True
    assert len(llm.calls) == 1  # 재시도 안 함


def test_replan_feedback_contains_reason_and_task():
    plan = Plan([PlanTask("t1", "bulk_mutation", kind="mutate", args={})])
    res = execute_plan(None, plan, None, _exec)
    fb = _replan_feedback(PlanRun(plan=plan, exec=res))
    assert "t1" in fb and "nurse 파라미터 누락" in fb and "bulk_mutation" in fb
