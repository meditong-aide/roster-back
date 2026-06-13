"""B1 + B2 보안 검열 단위 테스트.

- Layer A (input_classifier): 정상 SAFE / 적대적 MALICIOUS / 회색지대 SUSPICIOUS
- Layer B (output_check): sentinel·내부 경로·cross-tenant group_id 누출 감지
"""

from __future__ import annotations

import pytest

from agents_v2.security import (
    InputVerdict,
    check_output,
    classify_input,
)
from agents_v2.security.output_check import OutputVerdict
from tests.agent_qa.corpus.injection_phrases import (
    CLEAN_OUTPUTS,
    LEAK_OUTPUTS,
    LENGTH_EXCEEDED_INPUT,
    MALICIOUS_INJECTION,
    SAFE_PHRASES,
    SUSPICIOUS_PHRASES,
)


# ── Layer A: 정상 발화 SAFE ─────────────────────────────────


@pytest.mark.parametrize("text", SAFE_PHRASES)
def test_safe_phrases_pass(text: str) -> None:
    res = classify_input(text)
    assert res.verdict is InputVerdict.SAFE, (
        f"정상 발화가 SAFE 가 아님: {text!r} → {res.verdict} ({res.reason_codes})"
    )
    assert res.reason_codes == ()


# ── Layer A: 적대적 발화 MALICIOUS ──────────────────────────


@pytest.mark.parametrize("text,expected_code", MALICIOUS_INJECTION)
def test_malicious_blocked(text: str, expected_code: str) -> None:
    res = classify_input(text)
    assert res.verdict is InputVerdict.MALICIOUS, (
        f"악성 발화가 MALICIOUS 가 아님: {text!r} → {res.verdict} ({res.reason_codes})"
    )
    assert expected_code in res.reason_codes
    assert res.block_message is not None


def test_length_exceeded_blocked() -> None:
    res = classify_input(LENGTH_EXCEEDED_INPUT)
    assert res.verdict is InputVerdict.MALICIOUS
    assert "LENGTH_EXCEEDED" in res.reason_codes


# ── Layer A: 회색지대 SUSPICIOUS ────────────────────────────


@pytest.mark.parametrize("text,expected_code", SUSPICIOUS_PHRASES)
def test_suspicious_flagged(text: str, expected_code: str) -> None:
    res = classify_input(text)
    # SUSPICIOUS 단독이거나, 더 강한 MALICIOUS 가 동시 매칭될 수도 있음.
    assert res.verdict in (InputVerdict.SUSPICIOUS, InputVerdict.MALICIOUS), (
        f"의심 발화가 그대로 SAFE: {text!r}"
    )
    assert expected_code in res.reason_codes


def test_empty_string_safe() -> None:
    assert classify_input("").verdict is InputVerdict.SAFE


# ── Layer B: 깨끗한 응답 OK ─────────────────────────────────


@pytest.mark.parametrize("answer,user_gid", CLEAN_OUTPUTS)
def test_clean_outputs_ok(answer: str, user_gid: str) -> None:
    res = check_output(answer, user_group_id=user_gid)
    assert res.verdict is OutputVerdict.OK, (
        f"정상 응답이 OK 아님: {answer!r} → {res.verdict} ({res.reason_codes})"
    )
    assert res.answer == answer
    assert res.reason_codes == ()


# ── Layer B: 누출 감지 ─────────────────────────────────────


@pytest.mark.parametrize("answer,expected_code,user_gid", LEAK_OUTPUTS)
def test_leak_detected(answer: str, expected_code: str, user_gid: str) -> None:
    res = check_output(answer, user_group_id=user_gid)
    assert res.verdict is not OutputVerdict.OK, (
        f"누출 감지 실패: {answer!r}"
    )
    assert expected_code in res.reason_codes


def test_cross_tenant_redacted_others_only() -> None:
    """다른 그룹 ID 만 마스킹, 자기 그룹 ID 는 그대로."""
    answer = "내 그룹 GRP001 과 다른 그룹 GRP999 비교 결과입니다."
    res = check_output(answer, user_group_id="GRP001")
    assert res.verdict is OutputVerdict.REDACTED
    assert "GRP001" in res.answer  # 자기 그룹 보존
    assert "GRP999" not in res.answer  # 타 그룹 마스킹
    assert "CROSS_TENANT_GROUP_ID" in res.reason_codes


def test_sentinel_blocks_entire_answer() -> None:
    """SYSTEM_SENTINEL 노출은 답변 전체를 generic 으로 치환."""
    answer = "참고: SECURITY_BOUNDARY 안내가 적용됩니다."
    res = check_output(answer, user_group_id="GRP001")
    assert res.verdict is OutputVerdict.BLOCKED
    assert "SECURITY_BOUNDARY" not in res.answer
    assert "SYSTEM_PROMPT_LEAK" in res.reason_codes


def test_no_user_group_skips_cross_tenant() -> None:
    """user_group_id=None 이면 cross-tenant 검사 우회 (다른 검사는 정상)."""
    res = check_output("GRP999 정보입니다.", user_group_id=None)
    assert res.verdict is OutputVerdict.OK


def test_empty_answer_ok() -> None:
    res = check_output("", user_group_id="GRP001")
    assert res.verdict is OutputVerdict.OK
