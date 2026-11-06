"""플랜 답변에 L2(answer-consistency) 적용 — 불일치 시 데이터 근거로 재생성."""
from unittest.mock import MagicMock
from agents_v2.agent_v3 import SchedulingAgent
from agents_v2.planning import orchestrate
from agents_v2 import verify
from agents_v2.planning.orchestrate import PlanRun
from agents_v2.planning.plan import Plan, PlanTask
from agents_v2.planning.executor import PlanExecResult


def _run():
    return PlanRun(Plan([PlanTask("t1", "query_schedule")]),
                  PlanExecResult(outputs={"t1": {"nights": 3}}))


def test_l2_catches_and_regenerates(monkeypatch):
    agent = SchedulingAgent(MagicMock(), enable_user_memory=False, router_llm=MagicMock())
    calls = {"join": 0}

    def fake_join(llm, msg, run, correction=None):
        calls["join"] += 1
        return "야간 5개입니다" if correction is None else "야간 3개입니다"
    monkeypatch.setattr(orchestrate, "join_answer", fake_join)
    monkeypatch.setattr(verify, "judge_answer_consistency",
                        lambda l, q, d, a: verify.ConsistencyResult(False, "데이터는 3인데 5라 함"))
    ans = agent._plan_answer("야간 몇 개?", _run())
    assert calls["join"] == 2, "재생성 안 함"       # 원본 + correction 재생성
    assert ans == "야간 3개입니다"                   # 재생성본 채택


def test_l2_consistent_no_regen(monkeypatch):
    agent = SchedulingAgent(MagicMock(), enable_user_memory=False, router_llm=MagicMock())
    calls = {"join": 0}

    def fake_join(llm, msg, run, correction=None):
        calls["join"] += 1
        return "야간 3개입니다"
    monkeypatch.setattr(orchestrate, "join_answer", fake_join)
    monkeypatch.setattr(verify, "judge_answer_consistency",
                        lambda l, q, d, a: verify.ConsistencyResult(True))
    ans = agent._plan_answer("야간 몇 개?", _run())
    assert calls["join"] == 1, "정합인데 재생성함"
    assert ans == "야간 3개입니다"
