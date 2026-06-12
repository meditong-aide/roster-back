from datetime import datetime
from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class DefaultShiftItem(BaseModel):
    """메인 근무코드(D/E/N/M)별 기본 매핑 shift 정보"""

    code: str = Field(..., description="메인 근무코드 (D | E | N | M)")
    shift_table_id: Optional[int] = Field(
        default=None,
        description="shifts.id 매핑 (없으면 None)",
    )


class GradeConfigBase(BaseModel):
    """Grade 설정 기본 스키마"""

    null_grade_policy: str = Field(
        default="LOWEST",
        description=(
            "NULL Grade 처리 정책 "
            "(LOWEST=최하위로 매핑 | AVERAGE=평균값 스냅 | RANDOM=해시 결정 | EXCLUDE=grade 제약에서 제외)"
        ),
    )
    use_dynamic_scaling: bool = Field(
        default=True,
        description="일자별 필요 인원 감소 시 비율 축소 적용 여부",
    )
    allow_soft_fallback: bool = Field(
        default=False,
        description="grade 최소/최대 제약을 soft(slack 허용)로 완화할지 여부. 기본값 False=hard.",
    )
    constraints: Dict[str, Dict[int, int]] = Field(
        default_factory=dict,
        description="Shift별 Grade 최소 인원 예: {'D': {1:1, 2:2}}",
    )
    constraints_max: Dict[str, Dict[int, int]] = Field(
        default_factory=dict,
        description="Shift별 Grade 최대 인원(anti-pair). 예: {'N': {1: 2}} → N에 grade 1 최대 2명. 값 -1/음수는 '제한 없음'으로 처리.",
    )
    grade_names: Optional[Dict[str, Optional[str]]] = Field(
        default=None,
        description=(
            "Grade 번호별 표시 이름. 응답은 constraints / grade_names 합집합의 모든 grade slot 키를 포함하며, "
            "사용자가 명시하지 않은 슬롯은 값이 null 로 내려옵니다. 예: {'1': '주니어', '2': null, '3': null}. "
            "전체가 null 매핑(슬롯 자체가 없음)이면 키 자체가 비어 옵니다."
        ),
    )
    use_mid: bool = Field(
        default=False,
        description="해당 그룹의 MID 근무코드 사용 여부 (roster_config 기반)",
    )
    default_shifts: Optional[List[DefaultShiftItem]] = Field(
        default=None,
        description=(
            "메인 근무코드별 기본 shift 매핑. 예: [{'code':'D','shift_table_id':1}]. "
            "upsert 시 None/미전송이면 기존 값 유지, 빈 배열이면 초기화."
        ),
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
