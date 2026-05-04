from pydantic import BaseModel, Field
from typing import List, Dict, Optional


class DailyShiftDateArrays(BaseModel):
    """일자별 인원·상한 배열. 길이는 해당 월의 일수와 동일해야 합니다.
    - 인자: D_count/E_count/N_count/M_count + *_count_max(선택)
    - 빈 리스트는 update_daily 내부에서 0 패딩 처리됨.
    """
    D_count: List[int]
    E_count: List[int]
    N_count: List[int]
    M_count: List[int] = Field(default_factory=list, description="M 배열(빈 리스트 → 0 패딩)")
    D_count_max: List[int] = Field(default_factory=list, description="D 일자별 상한(0=미설정)")
    E_count_max: List[int] = Field(default_factory=list, description="E 일자별 상한(0=미설정)")
    N_count_max: List[int] = Field(default_factory=list, description="N 일자별 상한(0=미설정)")
    M_count_max: List[int] = Field(default_factory=list, description="M 일자별 상한(0=미설정)")


class DailyShiftReplace(BaseModel):
    """월 데이터 일괄 교체 요청 (PUT /daily-shift).
    - body 모양은 GET /daily-shift 응답과 동형이며, 저장 권위는 date 배열이다.
    - month_summary 는 round-trip 편의로 수용하나 저장에는 사용하지 않는다.
    - apply_globally=True 시 shift_manage 템플릿(RN 슬롯 1/2/3/5)을 day=1 값으로 동기화한다.
    - max_enabled=False 시 *_count_max 입력은 모두 0 으로 reset.
    """
    office_id: str
    group_id: str
    year: int
    month: int
    date: DailyShiftDateArrays
    max_enabled: bool = Field(default=False, description="False=*_max 0 reset")
    apply_globally: bool = Field(default=False, description="True=shift_manage 템플릿 day=1 값으로 동기화")
    month_summary: Optional[Dict[str, int]] = Field(default=None, description="GET 응답 round-trip 수용용(저장에 사용 안 함)")


class DailyShiftMonthQuery(BaseModel):
    """월의 일자별 근무 인원 조회 요청 스키마.
    - 인자: office_id(str), group_id(str), year(int), month(int)
    - 반환 없음
    - 예시: year=2025, month=7
    """
    office_id: str = Field(..., description="오피스 ID")
    group_id: str = Field(..., description="그룹 ID")
    year: int
    month: int


class DailyShiftMonthResponse(BaseModel):
    """월 조회 응답 구조.
    - month_summary: 1일차 기준 요약 D/E/N (+ _max 상한)
    - date: D_count/E_count/N_count/M_count 및 *_max 리스트(1~말일)
    - 예시: date.D_count=[3,3,3], date.D_count_max=[5,5,5]
    """
    office_id: str
    group_id: str
    year: int
    month: int
    month_summary: Dict[str, int]
    date: Dict[str, List[int]]


class DailyShiftMonthlyUpdate(BaseModel):
    """월 전체 일괄 업데이트 요청.
    - 인자: office_id, group_id, year, month, day, evening, night, mid (+ *_max, max_enabled)
    - 예시: day=4, evening=3, night=2, day_max=5, evening_max=5, night_max=4, max_enabled=true
    - max_enabled=False 시 *_max 컬럼 모두 0 으로 reset (사용자 입력 *_max 값 무시).
    """
    office_id: str
    group_id: str
    year: int
    month: int
    day: int
    evening: int
    night: int
    mid: int = 0
    day_max: int = Field(default=0, ge=0, description="D 최대 인원(0=상한 미설정)")
    evening_max: int = Field(default=0, ge=0, description="E 최대 인원(0=상한 미설정)")
    night_max: int = Field(default=0, ge=0, description="N 최대 인원(0=상한 미설정)")
    mid_max: int = Field(default=0, ge=0, description="M 최대 인원(0=상한 미설정)")
    max_enabled: bool = Field(default=False, description="False=*_max 사용 안 함(0 reset)")
    apply_globally: bool = True


class DailyShiftDailyUpdate(BaseModel):
    """일자별 배열 업데이트 요청.
    - 인자: office_id, group_id, year, month, D/E/N/M 리스트 (+ *_max, max_enabled)
    - 예시: D=[1,3,3], D_max=[3,3,3], max_enabled=true
    - max_enabled=False 시 *_max 컬럼 모두 0 으로 reset.
    """
    office_id: str
    group_id: str
    year: int
    month: int
    D: List[int]
    E: List[int]
    N: List[int]
    M: List[int] = Field(default_factory=list)
    D_max: List[int] = Field(default_factory=list, description="D 일자별 상한(0=미설정)")
    E_max: List[int] = Field(default_factory=list, description="E 일자별 상한(0=미설정)")
    N_max: List[int] = Field(default_factory=list, description="N 일자별 상한(0=미설정)")
    M_max: List[int] = Field(default_factory=list, description="M 일자별 상한(0=미설정)")
    max_enabled: bool = Field(default=False, description="False=*_max 사용 안 함(0 reset)")


class CalendarUpdateRequest(BaseModel):
    office_id: str
    group_id: str
    # years[year][month] 의 inner dict 는 day, d_count, e_count, n_count, m_count 및
    # d_count_max, e_count_max, n_count_max, m_count_max 키를 모두 수용 (후자는 선택).
    years: Dict[str, Dict[str, List[Dict[str, int]]]]
    comment: str | None = None  # 선택적 필드 추가
