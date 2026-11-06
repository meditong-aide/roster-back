"""planner 의존 판단 — 양방향(순서필요→deps / 독립→병렬). 과확장·과소 둘 다 검증. (라이브)"""
import os
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[2] / ".env")
import pytest
from agents_v2.planning.planner import build_plan
from agents_v2.skills.descriptions import SKILL_TOOLS
from agents_v2.llm_client import get_llm_client

pytestmark = pytest.mark.skipif(not os.getenv("OPENAI_API_KEY"), reason="라이브")


def _plan(q):
    return build_plan(get_llm_client("openai"), q, SKILL_TOOLS)


def test_override_and_settings_to_action_deps():
    # 8월 전체 322 → 주말 222(override) → off → 돌려줘(생성): 순서 의존 필요
    p = _plan("8월 전체 322 설정하고 주말만 222 설정하고, off 10으로 돌려줘")
    assert p is not None and len(p.tasks) >= 3
    print("\nPLAN:", [(t.id, t.skill, t.kind, t.deps) for t in p.tasks])
    # 최소 하나의 task 가 다른 task 에 의존해야(override 또는 설정→생성)
    assert any(t.deps for t in p.tasks), "순서 의존 놓침(과소)"
    # 생성 task 가 있으면 그건 앞 설정들에 의존해야
    gen = [t for t in p.tasks if "generate" in t.skill]
    if gen:
        assert gen[0].deps, "생성이 설정에 의존 안 함"


def test_independent_no_false_deps():
    # 서로 다른 대상 설정 → 억지 순서 금지(None 또는 deps 전부 빈)
    p = _plan("데이 필요인원 5명으로 하고, 월 오프 10개로 하고, 김민지 등급 3으로")
    if p is not None:
        print("\nINDEP PLAN:", [(t.id, t.skill, t.deps) for t in p.tasks])
        assert all(not t.deps for t in p.tasks), "독립인데 false 의존 만듦(과확장)"


def test_dataflow_dep_preserved():
    p = _plan("5월 3일 나이트 대체자 찾아서 그 사람으로 배정해줘")
    assert p is not None and any(t.deps for t in p.tasks)
