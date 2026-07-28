"""ReAct 승인 staleness 가드(P2) — 승인 시점 미리보기와 커밋 직전이 다르면 재확인.

이전엔 DAG 경로(_commit_plan)만 staleness 를 봤고 ReAct 승인은 **무검사로 stale commit** 가능했다.
공유 게이트 P2 로 ReAct 단일 mutation 승인에도 대칭 적용(docs/AGENT_SHARED_GATE_REFACTOR.md).
"""
from unittest.mock import MagicMock

from agents_v2.agent_v3 import SchedulingAgent
from agents_v2.middleware import execute_skill
from agents_v2.schemas.session_context import SessionContext
from db.models import RosterConfig


def _ctx():
    return SessionContext(office_id="OFF001", group_id="GRP001", year=2026, month=5,
                          nurse_id="N001", nurse_name="김민지", user_role="HN")


def _agent():
    return SchedulingAgent(MagicMock(), enable_user_memory=False)


def _cfg(db):
    return db.query(RosterConfig).filter_by(config_id=1, group_id="GRP001").first()


def _stored_preview(db, ctx):
    # 턴N 미리보기(old=현재값). update_constraint 는 settable·비잠금 필드로.
    args = {"updates": {"max_conseq_work": 4}}
    pv = execute_skill(db, "update_constraint", {**args, "preview_only": True}, ctx)
    return {**pv.data, "skill_name": "update_constraint", "args": args}


def test_reconfirm_when_state_drifted(db, seed_data):
    ctx = _ctx()
    assert _cfg(db).max_conseq_work == 5
    ctx.pending_approval = _stored_preview(db, ctx)  # old=5, new=4

    # 승인 전 상태 드리프트: 그 사이 5→3 으로 바뀜
    _cfg(db).max_conseq_work = 3
    db.flush()

    res = _agent().run(db, "응", ctx)

    assert res.awaiting_approval is True, "stale 인데 재확인 안 함"
    assert any(getattr(s, "name", "") == "approval_staleness" for s in (res.trace or []))
    # 커밋 안 됨 → 여전히 3 (4로 안 감)
    assert _cfg(db).max_conseq_work == 3, "재확인인데 커밋됨"


def test_commits_when_fresh(db, seed_data):
    ctx = _ctx()
    ctx.pending_approval = _stored_preview(db, ctx)  # old=5, new=4 — 드리프트 없음

    res = _agent().run(db, "응", ctx)

    assert res.awaiting_approval is not True, "fresh 인데 재확인함(오탐)"
    assert _cfg(db).max_conseq_work == 4, "정상 커밋 안 됨"
