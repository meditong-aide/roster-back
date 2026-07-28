"""verify_answer — 공유 L2 정합 게이트(P1). 가드(None=skip)와 판정 위임을 결정론으로 고정.

두 실행 경로(_plan_answer / ReAct 인라인)가 이 헬퍼로 단일화됐으므로, 가드 계약이 회귀하면
한쪽 경로의 L2 가 조용히 꺼질 수 있다 → 여기서 잠근다.
"""
from agents_v2.llm_client import LLMResponse
from agents_v2.verify import verify_answer


class _JudgeLLM:
    def __init__(self, verdict: str):
        self._v = verdict

    def chat(self, messages, tools, *, tool_choice="auto"):  # noqa: ANN001
        return LLMResponse(type="text", text=self._v)


_DATA = {"nurses": ["김민지"], "count": 1}


def test_skip_when_no_answer():
    assert verify_answer(_JudgeLLM("CONSISTENT"), "q", _DATA, "") is None
    assert verify_answer(_JudgeLLM("CONSISTENT"), "q", _DATA, "   ") is None


def test_skip_when_no_data():
    assert verify_answer(_JudgeLLM("CONSISTENT"), "q", None, "답변") is None
    assert verify_answer(_JudgeLLM("CONSISTENT"), "q", {}, "답변") is None
    assert verify_answer(_JudgeLLM("CONSISTENT"), "q", [], "답변") is None


def test_skip_when_data_too_big():
    big = {"x": "가" * 4000}  # l2_data_fits=False → 오탐 방지로 skip
    assert verify_answer(_JudgeLLM("INCONSISTENT: 무엇"), "q", big, "답변") is None


def test_judges_when_eligible_consistent():
    r = verify_answer(_JudgeLLM("CONSISTENT"), "q", _DATA, "김민지 1명")
    assert r is not None and r.consistent is True


def test_judges_when_eligible_inconsistent():
    r = verify_answer(_JudgeLLM("INCONSISTENT: 5명이라 했지만 데이터는 1명"), "q", _DATA, "5명입니다")
    assert r is not None and r.consistent is False and "5명" in r.reason
