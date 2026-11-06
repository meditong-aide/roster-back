"""agent.run e2e — DAG override 가 커밋까지 순서대로(주말이 전체 override). 라이브."""
import os
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[2] / ".env")
import pytest
from agents_v2.agent_v3 import SchedulingAgent
from agents_v2.schemas.session_context import SessionContext
from agents_v2.llm_client import get_llm_client, get_router_llm_client
from db.models import DailyShift

pytestmark = pytest.mark.skipif(not os.getenv("OPENAI_API_KEY"), reason="라이브")


def _ctx():
    return SessionContext(office_id="OFF001", group_id="GRP001", year=2026, month=8,
                          nurse_id="N001", nurse_name="김민지", user_role="HN")


def test_override_commits_in_order(db, seed_data):
    agent = SchedulingAgent(get_llm_client("openai"), enable_user_memory=False,
                            router_llm=get_router_llm_client("openai"))
    agent._dag_planning = True
    ctx = _ctx()

    # 전체 데이5 → 주말만 데이2 (override). 순서 안 지키면 전체5가 주말을 덮어씀.
    res1 = agent.run(db, "8월 전체 데이 5명으로 하고 주말은 데이 2명으로 설정해줘", ctx)
    print("\nR1 awaiting:", res1.awaiting_approval, "| pending:", (ctx.pending_approval or {}).get("type"))
    print("R1 answer:", (res1.answer or "")[:100])
    assert res1.awaiting_approval is True
    assert ctx.pending_approval and ctx.pending_approval["type"] == "plan"

    res2 = agent.run(db, "응", ctx)
    print("R2 answer:", (res2.answer or "")[:100])
    assert ctx.pending_approval is None  # 소진

    rows = {int(r.day): r for r in
            db.query(DailyShift).filter_by(group_id="GRP001", year=2026, month=8).all()
            if int(r.day) > 0}
    # 8/2=토(주말)=2, 8/3=월(평일)=5 → override 순서 정확
    assert rows[2].d_count == 2, f"주말 8/2 D={rows[2].d_count} (기대 2)"
    assert rows[3].d_count == 5, f"평일 8/3 D={rows[3].d_count} (기대 5)"
    print(f"검증: 평일 8/3 D={rows[3].d_count}, 주말 8/2 D={rows[2].d_count}")
