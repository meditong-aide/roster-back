"""실제 execute_skill + SessionLocal 격리로 병렬 read e2e (StaticPool 공유엔진)."""
import time
from agents_v2.planning.plan import Plan, PlanTask
from agents_v2.planning.executor import execute_plan
from agents_v2.middleware import execute_skill
from agents_v2.schemas.session_context import SessionContext
from db.client2 import SessionLocal


def _ctx():
    return SessionContext(office_id="OFF001", group_id="GRP001", year=2026, month=4,
                          nurse_id="N001", nurse_name="김민지", user_role="HN")


def test_parallel_reads_real_sessions(db, seed_data):
    plan = Plan([
        PlanTask("t1", "query_schedule", kind="read", args={"scope": "nurse_info"}),
        PlanTask("t2", "query_schedule", kind="read", args={"scope": "constraint_config"}),
    ])
    res = execute_plan(db, plan, _ctx(), execute_skill, session_factory=SessionLocal)
    assert res.failed is None, f"병렬 read 실패: {res.failed}"
    assert res.outputs.get("t1") is not None and res.outputs.get("t2") is not None
    assert set(res.order) == {"t1", "t2"}
