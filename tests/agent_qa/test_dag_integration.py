"""agent_v3 DAG 통합 배선 (결정적 — 엔진 mock, 라우팅 로직만 검증)."""
from unittest.mock import MagicMock
from agents_v2.agent_v3 import SchedulingAgent
from agents_v2.schemas.session_context import SessionContext
from agents_v2.planning import orchestrate, executor
from agents_v2.planning.plan import Plan, PlanTask
from agents_v2.planning.executor import PlanExecResult
from agents_v2.planning.orchestrate import PlanRun
from agents_v2 import verify


def _agent():
    a = SchedulingAgent(MagicMock(), enable_user_memory=False, router_llm=None)
    a._dag_planning = True
    return a


def _ctx():
    return SessionContext(office_id="OFF001", group_id="GRP001", year=2026, month=8,
                          nurse_id="N001", nurse_name="김민지", user_role="HN")


def _readonly_run():
    return PlanRun(plan=Plan([PlanTask("t1", "query_schedule", kind="read")]),
                  exec=PlanExecResult(outputs={"t1": {"x": 1}}, previews=[], order=["t1"]))


def _mutate_run():
    return PlanRun(
        plan=Plan([PlanTask("t1", "recommend_candidates", kind="read"),
                   PlanTask("t2", "bulk_mutation", kind="mutate", deps=["t1"])]),
        exec=PlanExecResult(outputs={"t1": {"candidates": []}, "t2": {"preview": True}},
                            previews=[{"task": "t2", "skill": "bulk_mutation", "data": {"preview": True}}],
                            order=["t1", "t2"]))


def test_readonly_plan_answers(db, seed_data, monkeypatch):
    monkeypatch.setattr(orchestrate, "try_plan_run", lambda *a, **k: _readonly_run())
    monkeypatch.setattr(orchestrate, "join_answer", lambda llm, msg, run, correction=None: "합성 답변")
    monkeypatch.setattr(verify, "judge_answer_consistency", lambda *a, **k: verify.ConsistencyResult(True))
    res = _agent().run(db, "복합 조회 질의", _ctx())
    assert res.answer == "합성 답변" and not res.awaiting_approval


def test_mutation_plan_awaits_approval(db, seed_data, monkeypatch):
    monkeypatch.setattr(orchestrate, "try_plan_run", lambda *a, **k: _mutate_run())
    monkeypatch.setattr(orchestrate, "join_answer", lambda llm, msg, run, correction=None: "이수정 배정안")
    monkeypatch.setattr(verify, "judge_answer_consistency", lambda *a, **k: verify.ConsistencyResult(True))
    ctx = _ctx()
    res = _agent().run(db, "대체자 찾아 배정", ctx)
    assert res.awaiting_approval is True
    assert ctx.pending_approval and ctx.pending_approval["type"] == "plan"
    assert "진행할까요" in res.answer


def test_plan_commit_on_confirm(db, seed_data, monkeypatch):
    # 승인 대기 상태에서 '응' → _commit_plan (execute_plan dry_run=False) → 답변
    committed = {}
    def fake_exec(db_, plan, ctx_, fn, dry_run_mutations=True, session_factory=None, **kw):
        committed["dry"] = dry_run_mutations
        return PlanExecResult(outputs={"t2": {"ok": True}}, previews=[], order=["t1", "t2"])
    monkeypatch.setattr(executor, "execute_plan", fake_exec)
    monkeypatch.setattr(orchestrate, "join_answer", lambda llm, msg, run, correction=None: "배정 완료")
    monkeypatch.setattr(verify, "judge_answer_consistency", lambda *a, **k: verify.ConsistencyResult(True))
    ctx = _ctx()
    ctx.pending_approval = {"type": "plan", "user_message": "대체자 배정",
                            "plan": {"tasks": [{"id": "t1", "skill": "recommend_candidates", "kind": "read"},
                                               {"id": "t2", "skill": "bulk_mutation", "kind": "mutate", "deps": ["t1"]}]}}
    res = _agent().run(db, "응", ctx)
    assert committed["dry"] is False        # 실제 commit
    assert res.answer == "배정 완료"
    assert ctx.pending_approval is None      # 소진됨
