"""시프트 자연어 → D/E/N/M 카테고리 그라운딩 (스킬 공용).

grade·team_min 등 '카테고리 단위'(특정 shift_id 가 아니라 D/E/N/M)로 동작하는
스킬이 공유한다. 미들웨어의 shift_name→shift_codes(DB shift_id) 그라운딩과는
별개 — 이쪽은 추상 카테고리가 필요하다.
"""

from __future__ import annotations

from typing import Any

SHIFT_CATEGORY: dict[str, str] = {
    "D": "D", "E": "E", "N": "N", "M": "M",
    "데이": "D", "주간": "D", "낮": "D", "낮번": "D",
    "이브닝": "E", "초번": "E", "저녁": "E",
    "나이트": "N", "야간": "N", "밤": "N",
    "미드": "M", "미들": "M", "중간": "M",
}

CATEGORY_LABEL: dict[str, str] = {"D": "데이", "E": "이브닝", "N": "나이트", "M": "미드"}


def resolve_shift_category(name: Any, use_mid: bool) -> str | None:
    """'나이트'/'D' 등 → 'N'/'D'. 못 찾거나 use_mid=False 인데 M 이면 None."""
    if not name:
        return None
    key = str(name).strip()
    code = SHIFT_CATEGORY.get(key) or SHIFT_CATEGORY.get(key.upper())
    if code == "M" and not use_mid:
        return None
    return code
