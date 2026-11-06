"""ReAct 배치 승인 원자성(P4) — 중간 실패 시 전체 롤백(부분 반영 없음).

이전엔 배치가 항목을 순차 커밋해, 2번째가 실패해도 1번째는 이미 반영됐다(부분 커밋).
공유 원자 경계(commit_gate.atomic_commit)로 all-or-nothing 보장.
AIDE_DAG_PLANNING 기본 OFF → 의존 복합도 이 배치로 오므로 반쯤 적용된 의존 변경을 막는다.

주: conftest db fixture 의 rollback 은 seed 행까지 되돌리므로(StaticPool debt), test_dag_atomic
과 동일하게 **테스트가 만든 행의 부재**로 롤백을 판정한다(seed 생존 가정 안 함).
"""
from unittest.mock import MagicMock

from agents_v2.agent_v3 import SchedulingAgent
from agents_v2.schemas.session_context import SessionContext
from db.models import DailyShift


def _ctx():
    return SessionContext(office_id="OFF001", group_id="GRP001", year=2026, month=8,
                          nurse_id="N001", nurse_name="김민지", user_role="HN")


def _agent():
    return SchedulingAgent(MagicMock(), enable_user_memory=False)


def _day_rows(db):
    return [r for r in db.query(DailyShift).filter_by(group_id="GRP001", year=2026, month=8).all()
            if int(r.day) > 0]


def test_batch_rolls_back_all_on_midfailure(db, seed_data):
    # item1 성공(월 데이6, 행 생성) → item2 실패(비허용 필드 error) → 전체 롤백 → item1 도 미반영
    ctx = _ctx()
    ctx.pending_approval = {"type": "batch", "items": [
        {"skill_name": "manage_daily_shift", "args": {"scope": "month", "d_count": 6}},
        {"skill_name": "update_constraint", "args": {"updates": {"__nope__": 1}}},  # 비허용 → error
    ]}
    res = _agent().run(db, "응", ctx)

    assert "롤백" in res.answer or "취소" in res.answer, res.answer
    assert all(int(r.d_count) != 6 for r in _day_rows(db)), "부분 반영됨 — item1 롤백 실패"


def test_batch_commits_all_on_success(db, seed_data):
    # 둘 다 성공(월 데이6 + 주말 데이2 override) → 원자 커밋 → 평일6·주말2
    ctx = _ctx()
    ctx.pending_approval = {"type": "batch", "items": [
        {"skill_name": "manage_daily_shift", "args": {"scope": "month", "d_count": 6}},
        {"skill_name": "manage_daily_shift", "args": {"scope": "weekend", "d_count": 2}},
    ]}
    _agent().run(db, "응", ctx)

    rows = {int(r.day): r for r in _day_rows(db)}
    assert rows[3].d_count == 6, f"평일 8/3 D={rows[3].d_count}"   # 평일
    assert rows[2].d_count == 2, f"주말 8/2 D={rows[2].d_count}"   # 주말(override)
