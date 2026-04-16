from datetime import datetime
from typing import Dict, Optional

from pydantic import BaseModel, Field


class GradeConfigBase(BaseModel):
    """Grade 설정 기본 스키마"""

    null_grade_policy: str = Field(
        default="LOWEST",
        description="NULL Grade 처리 정책 (LOWEST | AVERAGE | RANDOM)",
    )
    use_dynamic_scaling: bool = Field(
        default=True,
        description="일자별 필요 인원 감소 시 비율 축소 적용 여부",
    )
    constraints: Dict[str, Dict[int, int]] = Field(
        default_factory=dict,
        description="Shift별 Grade 최소 인원 예: {'D': {1:1, 2:2}}",
    )
    grade_names: Optional[Dict[str, str]] = Field(
        default=None,
        description="Grade 번호별 표시 이름 예: {'1': '주니어', '2': '시니어'}. null이면 숫자 그대로 표시.",
    )
    use_mid: bool = Field(
        default=False,
        description="해당 그룹의 MID 근무코드 사용 여부 (roster_config 기반)",
    )


class GradeConfigUpsert(GradeConfigBase):
    """Grade 설정 저장 요청 스키마"""


class GradeConfigResponse(GradeConfigBase):
    """Grade 설정 응답 스키마"""

    config_id: Optional[int] = None
    office_id: Optional[str] = None
    group_id: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    updated_by: Optional[str] = None
    class Config:
        from_attributes = True

