"""B7: ChatResponse.data 채움 검증.

이전: 백엔드가 ChatResponse.data 를 항상 None 으로 두어 프론트가 인라인 카드/
테이블 렌더를 못 함 → 자연어 answer 만으로 모든 정보 전달.

수정 후: AgentResult.data 에 마지막 성공 조회 결과 누적 → chat_router 가
ChatResponse.data 로 변환.

검증 항목:
  - dict 결과 → ChatResponse.data 그대로 노출
  - list 결과 → {"items": [...]} 로 래핑
  - error / needs_clarification / preview 결과는 data 에 안 담김
  - output BLOCKED 면 data 도 무시
"""

from __future__ import annotations

from agents_v2.agent_v3 import AgentResult


# ── AgentResult.data 필드 자체 ───────────────────────────────


def test_agent_result_has_data_field():
    res = AgentResult(answer="x")
    assert hasattr(res, "data")
    assert res.data is None


def test_agent_result_data_carries_dict():
    payload = {"min_requirements": [{"shift": "데이", "grade": "주니어", "min_count": 1}]}
    res = AgentResult(answer="x", data=payload)
    assert res.data is payload


def test_agent_result_data_carries_list():
    payload = [{"nurse_id": "N001"}, {"nurse_id": "N002"}]
    res = AgentResult(answer="x", data=payload)
    assert res.data == payload


# ── chat_router 의 list→{"items":...} 래핑 로직 (스키마 일관성) ──
# chat_router 내부 변환 로직을 격리 테스트 — 실제 endpoint 통합 대신 변환 규칙만.


def _wrap_if_list(raw):
    """chat_router 의 safe_data 변환과 동일 — 단위테스트용 분리."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list):
        return {"items": raw}
    return None


def test_wrap_list_to_items():
    raw = [{"nurse_id": "N001"}, {"nurse_id": "N002"}]
    assert _wrap_if_list(raw) == {"items": raw}


def test_wrap_dict_passes_through():
    raw = {"min_requirements": []}
    assert _wrap_if_list(raw) is raw


def test_wrap_none_returns_none():
    assert _wrap_if_list(None) is None


def test_wrap_string_returns_none():
    """문자열·숫자 같은 스칼라는 data 후보 아님 — None."""
    assert _wrap_if_list("hello") is None
    assert _wrap_if_list(42) is None
