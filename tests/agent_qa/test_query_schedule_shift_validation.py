"""query_schedule: shift_codes 검증 회귀.

2026-06-01 (B5). LLM 이 'XX' 같이 모르는 코드 보내면 silent skip → 사용자에겐 "데이터 없음".
정규화 + needs_clarification 보장.
"""

from __future__ import annotations

import pytest

from agents_v2.skills.query_schedule import _normalize_shift_codes


def test_none_passthrough():
    norm, clar = _normalize_shift_codes(None)
    assert norm is None and clar is None


def test_empty_list_passthrough():
    norm, clar = _normalize_shift_codes([])
    assert norm == [] and clar is None


@pytest.mark.parametrize(
    "raw,expected",
    [
        (["N"], ["N"]),
        (["D", "E"], ["D", "E"]),
        (["나이트"], ["N"]),
        (["야간", "데이"], ["N", "D"]),
        (["d", "n"], ["D", "N"]),  # lowercase
        ("N", ["N"]),  # 단일 문자열도 수용
    ],
    ids=lambda v: repr(v),
)
def test_recognized_codes_normalized(raw, expected):
    norm, clar = _normalize_shift_codes(raw)
    assert clar is None, f"unexpected clarification: {clar}"
    assert norm == expected


def test_unknown_code_returns_clarification():
    norm, clar = _normalize_shift_codes(["N", "XX"])
    assert norm is None
    assert clar is not None
    assert clar["needs_clarification"] is True
    assert "XX" in clar["question"]
    assert any("N" in opt for opt in clar["options"])


def test_dedupe_within_codes():
    norm, clar = _normalize_shift_codes(["N", "야간", "나이트"])
    assert clar is None
    assert norm == ["N"]  # 셋 다 'N' 으로 정규화 후 중복 제거


def test_all_unknown_returns_clarification():
    norm, clar = _normalize_shift_codes(["XX", "YY"])
    assert norm is None
    assert clar is not None
    assert "XX" in clar["question"]
    assert "YY" in clar["question"]
