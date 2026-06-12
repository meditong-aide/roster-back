"""approval/denial 분기 corpus 회귀 — _is_confirmation / _is_denial 결정성.

각 발화가 의도된 분기로 가는지 deterministic 하게 검증. LLM 추론에 의존 X.
"""

from __future__ import annotations

import pytest

from agents_v2.agent_v3 import _is_confirmation, _is_denial
from tests.agent_qa.corpus.queries_approval import (
    AMBIGUOUS_PHRASES,
    CONFIRM_PHRASES,
    DENY_PHRASES,
    FALSE_POSITIVE_TRAPS,
)


@pytest.mark.parametrize("msg", CONFIRM_PHRASES, ids=lambda s: s[:20])
def test_confirm_phrase_recognized(msg: str):
    assert _is_confirmation(msg) is True, f"_is_confirmation('{msg}') 가 False — 컨펌 누락"
    assert _is_denial(msg) is False, f"_is_denial('{msg}') 가 True — 컨펌이 거부로 잘못 분류"


@pytest.mark.parametrize("msg", DENY_PHRASES, ids=lambda s: s[:20])
def test_deny_phrase_recognized(msg: str):
    assert _is_denial(msg) is True, f"_is_denial('{msg}') 가 False — 거부 누락"
    assert _is_confirmation(msg) is False, (
        f"_is_confirmation('{msg}') 가 True — 거부가 컨펌으로 잘못 분류"
    )


@pytest.mark.parametrize("msg", AMBIGUOUS_PHRASES, ids=lambda s: s[:20] if s.strip() else "<blank>")
def test_ambiguous_phrase_neither(msg: str):
    """모호한 발화는 둘 다 False → agent 가 재질의 흐름으로 빠짐."""
    assert _is_confirmation(msg) is False, f"'{msg}' 가 컨펌으로 분류됨(모호해야 함)"
    assert _is_denial(msg) is False, f"'{msg}' 가 거부로 분류됨(모호해야 함)"


@pytest.mark.parametrize("msg", FALSE_POSITIVE_TRAPS, ids=lambda s: s[:30])
def test_false_positive_traps_not_confirm(msg: str):
    """substring 매칭 회귀 — '예전' '확인하지 못했어' 등이 컨펌으로 오분류 안 됨."""
    assert _is_confirmation(msg) is False, (
        f"'{msg}' 가 _is_confirmation True — substring 매칭 회귀 의심"
    )


def test_geurae_specifically_recognized():
    """S5.B 회귀 보호: 2026-06-01 에 _CONFIRM_WORDS 에 '그래' 누락 발견 → 추가."""
    assert _is_confirmation("그래") is True
    assert _is_confirmation("그래 진행해") is True
