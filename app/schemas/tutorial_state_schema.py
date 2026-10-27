from typing import Optional

from pydantic import BaseModel, Field, field_validator

#: 계정별 안내 상태로 저장할 수 있는 키 → 허용 값. 2차(자동 제안)는 키만 더한다(DDL 재작성 없음).
TUTORIAL_STATE_RULES: dict[str, frozenset[str]] = {
    "welcome_shown": frozenset({"1"}),           # '1' = 첫 접속 카드 레이어를 보여 줌
    "next_month_guide": frozenset({"0", "1"}),   # '1' = 다음 달 근무표 만들 때도 안내 보기, '0' = 보지 않음
}


class TutorialStatePutRequest(BaseModel):
    """튜토리얼(사용 안내) 상태 저장 요청 (PUT /tutorial/state).
    - values: 바꿀 키만 보낸다. 값이 null 이면 그 키를 지운다(없음 = 화면 기본값).
    - 허용 키·값은 TUTORIAL_STATE_RULES. 모르는 키·값, 빈 values, 키 10개 초과는 422.
    """
    values: dict[str, Optional[str]] = Field(..., description="바꿀 키만. null 이면 그 키를 지운다")

    @field_validator("values")
    @classmethod
    def _allowed(cls, v: dict[str, Optional[str]]) -> dict[str, Optional[str]]:
        if not v or len(v) > 10:
            raise ValueError("values 는 1~10개 키여야 합니다")
        for key, value in v.items():
            allowed = TUTORIAL_STATE_RULES.get(key)
            if allowed is None:
                raise ValueError(f"알 수 없는 키: {key}")
            if value is not None and value not in allowed:
                raise ValueError(f"{key} 값이 올바르지 않습니다")
        return v


class TutorialStateResponse(BaseModel):
    """계정의 튜토리얼(사용 안내) 상태 전체. 저장된 키만 담긴다 — 없는 키의 기본값은 화면이 정한다."""
    values: dict[str, str]
