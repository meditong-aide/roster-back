"""검증층(L1/L2) e2e — 여러 스킬 + 복합쿼리에서 오탐 없이 도는지 라이브 확인."""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

import pytest
from agents_v2.agent_v3 import SchedulingAgent
from agents_v2.llm_client import get_llm_client, get_router_llm_client
from agents_v2.schemas.session_context import SessionContext

_HAS_KEY = bool(os.getenv("OPENAI_API_KEY"))
pytestmark = pytest.mark.skipif(not _HAS_KEY, reason="라이브 LLM 필요")


def _ctx():
    return SessionContext(office_id="OFF001", group_id="GRP001", year=2026, month=4,
                          nurse_id="N001", nurse_name="김민지", user_role="HN")


def _agent():
    return SchedulingAgent(get_llm_client("openai"),
                           router_llm=get_router_llm_client("openai"))


def _l2_flagged(res):
    for s in (res.trace or []):
        if getattr(s, "name", "") == "answer_consistency" and getattr(s, "status", "") == "block":
            return True, next((getattr(s, "data", {}) or {}).get("reason", "")
                              for s in res.trace
                              if getattr(s, "name", "") == "answer_consistency")
    return False, ""


QUERIES = [
    ("read·근무표", "김민지 4월 근무표 알려줘"),
    ("read·간호사정보", "김민지 정보 알려줘"),
    ("read·원티드", "4월 원티드 미제출자 누구야?"),
    ("read·분석", "4월 야간 분포 분석해줘"),
    ("복합·2조회", "김민지랑 박지은 각각 4월 야간 몇 개인지 알려줘"),
]


@pytest.mark.parametrize("label,q", QUERIES)
def test_no_false_verification(db, seed_data, label, q):
    res = _agent().run(db, q, _ctx())
    flagged, reason = _l2_flagged(res)
    print(f"\n[{label}] {q}\n  → answer: {(res.answer or '')[:90]}\n  → L2 flagged: {flagged} {reason[:80]}")
    assert res.answer, f"{label}: 빈 답변"
    # 정상 작업이 L2 에 잘못 걸리면(오탐) 실패 — 특히 복합쿼리 주목
    assert not flagged, f"{label}: L2 오탐 — {reason}"
