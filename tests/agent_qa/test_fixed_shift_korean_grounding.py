"""update_person_attr: fixed_shift 필드의 한글 시프트 이름 내부 그라운딩 검증.

2026-06-01 (S6) 추가. 기존엔 'D'/'E'/'N'/'M'/'O' 코드만 받고 한글은 LLM 번역에 의존 →
[[skill-internal-grounding]] 원칙 위반. 이제 '데이'/'나이트'/'오프' 등 직접 수용.
"""

from __future__ import annotations

import pytest

from agents_v2.tools.nurse_tools import _normalize_fixed_shift


@pytest.mark.parametrize(
    "raw,expected",
    [
        # 한글 이름
        ("데이", "D"),
        ("주간", "D"),
        ("이브닝", "E"),
        ("저녁", "E"),
        ("나이트", "N"),
        ("야간", "N"),
        ("미드", "M"),
        # 영문 별칭
        ("day", "D"),
        ("night", "N"),
        ("evening", "E"),
        # 코드 (backward-compat)
        ("D", "D"),
        ("N", "N"),
        ("M", "M"),
        ("O", "O"),
        # 'O' 한글 별칭 (fixed_shift 전용)
        ("오프", "O"),
        ("휴무", "O"),
        # 해제
        ("", ""),
        ("해제", ""),
        ("없음", ""),
        (None, ""),
        # 모르는 값
        ("xyz", None),
        ("주말", None),
    ],
    ids=lambda v: repr(v),
)
def test_normalize_fixed_shift(raw, expected):
    assert _normalize_fixed_shift(raw) == expected
