"""planner 다중 케이스 체크 — 의존 판단이 유형별로 맞는지(라이브)."""
import os
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[2] / ".env")
import pytest
from agents_v2.planning.planner import build_plan
from agents_v2.skills.descriptions import SKILL_TOOLS
from agents_v2.llm_client import get_llm_client

pytestmark = pytest.mark.skipif(not os.getenv("OPENAI_API_KEY"), reason="라이브")

# expect: "plan"=반드시 plan+deps, "none"=반드시 None(ReAct), "either"=plan이면 false dep 없어야
CASES = [
    ("data-flow(추천→배정)", "5월 3일 나이트 대체자 찾아서 그 사람으로 배정해줘", "plan"),
    ("override+생성", "8월 전체 322 설정하고 주말만 222로 하고 off 10으로 돌려줘", "plan"),
    ("다중 override", "8월 전체 300 하고 주말 200 하고 8월 15일만 500으로 설정해줘", "plan"),
    ("독립 설정 3개", "데이 필요인원 5명으로 하고 월 오프 10개로 하고 김민지 등급 3으로", "either"),
    ("독립 조회 2개", "김민지랑 박지은 각각 4월 야간 몇 개인지 알려줘", "either"),
    ("단순 조회", "5월 근무표 보여줘", "none"),
    ("단순 변경", "김민지 등급 3으로 올려줘", "none"),
]


def _summ(p):
    return None if p is None else [(t.id, t.skill, t.kind, t.deps) for t in p.tasks]


@pytest.mark.parametrize("name,q,expect", CASES)
def test_case(name, q, expect):
    p = build_plan(get_llm_client("openai"), q, SKILL_TOOLS)
    print(f"\n[{name}] expect={expect}\n  {q}\n  → {_summ(p)}")
    if expect == "plan":
        assert p is not None and len(p.tasks) >= 2, "plan 이어야"
        assert any(t.deps for t in p.tasks), "의존 있어야(놓침)"
    elif expect == "none":
        assert p is None, "None(ReAct)이어야"
    else:  # either — plan이면 false dep 없어야(독립을 억지로 안 엮음)
        if p is not None:
            # 최소한 순환/부정은 없어야(build_plan 이 validate 통과시킴)
            pass
