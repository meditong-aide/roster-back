"""B9: apply_hint 흐름 corpus 테스트.

pending_approval[type=apply_hint] 분기는 agent_v3._run_impl 에서:
  - _is_denial → "취소했습니다." (실행 안 함)
  - _is_confirmation → _execute_apply_hint 실행
  - 둘 다 아님 → 재질의 (pending_approval 유지)

이 테스트는 apply_hint 컨텍스트에서 자연스럽게 나오는 한국어 발화가
올바른 분기로 라우팅되는지를 corpus 회귀로 보장한다.

회귀 위험: 옵션 제안 응답("그 옵션으로", "soft 로 바꿔") 같이 일반 confirm/deny
패턴이 아닌 표현이 누락되어 LLM 추론에 떠넘겨지면 비결정적.
"""

from __future__ import annotations

import pytest

from agents_v2.agent_v3 import _is_confirmation, _is_denial
from tests.agent_qa.corpus.queries_approval import (
    APPLY_HINT_AMBIGUOUS_PHRASES,
    APPLY_HINT_CONFIRM_PHRASES,
    APPLY_HINT_DENY_PHRASES,
)


# ── CONFIRM: apply_hint 적용 분기로 라우팅돼야 함 ─────────────


@pytest.mark.parametrize("phrase", APPLY_HINT_CONFIRM_PHRASES)
def test_apply_hint_confirm_routes_to_execute(phrase: str) -> None:
    assert _is_confirmation(phrase), (
        f"apply_hint confirm 누락: {phrase!r} → False. _CONFIRM_WORDS 보강 필요."
    )
    # confirm 인 발화가 동시에 deny 이면 분기 우선순위 충돌 — agent_v3 는
    # deny 를 먼저 검사하므로 deny 가 False 여야 confirm 분기로 도달.
    assert not _is_denial(phrase), (
        f"apply_hint confirm 발화 {phrase!r} 가 deny 로도 분류됨 — 분기 충돌."
    )


# ── DENY: 취소 분기로 라우팅돼야 함 ─────────────────────────


@pytest.mark.parametrize("phrase", APPLY_HINT_DENY_PHRASES)
def test_apply_hint_deny_routes_to_cancel(phrase: str) -> None:
    assert _is_denial(phrase), (
        f"apply_hint deny 누락: {phrase!r} → False. _DENY_WORDS 보강 필요."
    )


# ── AMBIGUOUS: 둘 다 False — 재질의 분기로 라우팅돼야 함 ──────


@pytest.mark.parametrize("phrase", APPLY_HINT_AMBIGUOUS_PHRASES)
def test_apply_hint_ambiguous_falls_through_to_re_ask(phrase: str) -> None:
    """모호 발화는 confirm/deny 둘 다 False 여서 재질의 분기로 떨어져야 한다."""
    assert not _is_confirmation(phrase), (
        f"apply_hint ambiguous 가 confirm 으로 잘못 분류: {phrase!r}"
    )
    assert not _is_denial(phrase), (
        f"apply_hint ambiguous 가 deny 로 잘못 분류: {phrase!r}"
    )
