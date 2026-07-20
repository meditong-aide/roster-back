"""Planner (라이브) — 의존 복합은 DAG, 단순은 None."""
import os
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[2] / ".env")
import pytest
from agents_v2.planning.planner import build_plan, _parse_plan
from agents_v2.skills.descriptions import SKILL_TOOLS
from agents_v2.llm_client import get_llm_client

pytestmark = pytest.mark.skipif(not os.getenv("OPENAI_API_KEY"), reason="라이브")


def test_parse_plan_deterministic():
    txt = '{"tasks":[{"id":"t1","skill":"recommend_candidates","kind":"read","deps":[]},' \
          '{"id":"t2","skill":"bulk_mutation","kind":"mutate","deps":["t1"],' \
          '"args":{"nurse":"$t1.candidates[0].name"}}]}'
    p = _parse_plan(txt)
    assert len(p.tasks) == 2 and p.tasks[1].deps == ["t1"] and p.tasks[1].kind == "mutate"


def test_dependent_compound_makes_dag():
    llm = get_llm_client("openai")
    plan = build_plan(llm, "5월 3일 나이트 대체자 찾아서 그 사람으로 배정해줘", SKILL_TOOLS)
    assert plan is not None, "의존 복합인데 plan 안 나옴"
    assert len(plan.tasks) >= 2
    # 두번째 task 가 첫번째에 의존
    assert any(t.deps for t in plan.tasks), "의존성 없음"
    print("\nPLAN:", [(t.id, t.skill, t.kind, t.deps) for t in plan.tasks])


def test_simple_request_no_plan():
    llm = get_llm_client("openai")
    for q in ["5월 근무표 보여줘", "김민지 등급 3으로 올려줘"]:
        assert build_plan(llm, q, SKILL_TOOLS) is None, f"단순인데 plan 나옴: {q}"
