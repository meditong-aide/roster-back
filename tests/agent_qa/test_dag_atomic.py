"""DAG 플랜 커밋 원자성 — 중간 실패 시 전체 롤백(부분 반영 없음)."""
from unittest.mock import MagicMock
from agents_v2.agent_v3 import SchedulingAgent
from agents_v2.schemas.session_context import SessionContext
from db.models import DailyShift


def _ctx():
    return SessionContext(office_id="OFF001", group_id="GRP001", year=2026, month=8,
                          nurse_id="N001", nurse_name="김민지", user_role="HN")


def _agent():
    a = SchedulingAgent(MagicMock(), enable_user_memory=False)
    a._dag_planning = True
    return a


def test_atomic_rollback_on_midplan_failure(db, seed_data):
    # t1: month D=9 (성공·flush) → t2: weekend 인데 counts 없음 → clarify(STOP) → 전체 롤백
    ctx = _ctx()
    ctx.pending_approval = {
        "type": "plan", "user_message": "8월 전체 데이9 주말 데이?",
        "plan": {"tasks": [
            {"id": "t1", "skill": "manage_daily_shift", "kind": "mutate",
             "args": {"scope": "month", "d_count": 9}},
            {"id": "t2", "skill": "manage_daily_shift", "kind": "mutate", "deps": ["t1"],
             "args": {"scope": "weekend"}},  # counts 없음 → clarify → STOP
        ]},
    }
    res = _agent().run(db, "응", ctx)
    assert "롤백" in res.answer or "취소" in res.answer, res.answer
    # t1 의 D=9 가 롤백돼 8월 DailyShift 어디에도 없어야(부분 반영 방지)
    rows = db.query(DailyShift).filter_by(group_id="GRP001", year=2026, month=8).all()
    day_rows = [r for r in rows if int(r.day) > 0]
    assert all(int(r.d_count) != 9 for r in day_rows), "부분 반영됨 — 롤백 실패"
    assert ctx.pending_approval is None


def test_atomic_commit_on_success(db, seed_data):
    # t1: month D=6 → t2: weekend D=2 (둘 다 성공) → 원자 커밋 → 평일6 주말2
    ctx = _ctx()
    ctx.pending_approval = {
        "type": "plan", "user_message": "8월 전체 데이6 주말 데이2",
        "plan": {"tasks": [
            {"id": "t1", "skill": "manage_daily_shift", "kind": "mutate",
             "args": {"scope": "month", "d_count": 6}},
            {"id": "t2", "skill": "manage_daily_shift", "kind": "mutate", "deps": ["t1"],
             "args": {"scope": "weekend", "d_count": 2}},
        ]},
    }
    _agent().run(db, "응", ctx)
    rows = {int(r.day): r for r in
            db.query(DailyShift).filter_by(group_id="GRP001", year=2026, month=8).all()
            if int(r.day) > 0}
    assert rows[3].d_count == 6, f"평일 8/3 D={rows[3].d_count}"   # 평일
    assert rows[2].d_count == 2, f"주말 8/2 D={rows[2].d_count}"   # 주말(override)
