from pydantic import BaseModel, Field
from typing import List, Dict, Optional


class DailyShiftMonthSummary(BaseModel):
    """월 일괄값(day=0 row 권위).
    - 인자: D/E/N/M_count(필수) + *_count_max(선택) + max_enabled
    - 의미: 월 전체에 대한 대표/일괄 manpower. 일자별 row(day=1..말일) 와 별개로 day=0 PK 로 저장.
    - 응답 시 day=0 row 가 있으면 이 값으로, 없으면 day=1 row 미러로 노출됨.
    """
    D_count: int = 0
    E_count: int = 0
    N_count: int = 0
    M_count: int = 0
    D_count_max: int = 0
    E_count_max: int = 0
    N_count_max: int = 0
    M_count_max: int = 0
    max_enabled: bool = False


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
    - 두 가지 모드:
      1) 일자별 저장 (apply_summary_to_days=False, 기본):
         - date 배열로 day=1..말일 upsert.
         - month_summary 가 함께 오면 day=0 row 도 upsert.
      2) 일괄적용 (apply_summary_to_days=True):
         - day=0 row 값으로 day=1..말일 모두에 fan-out (date 배열 무시 가능, 생략 가능).
         - month_summary 가 함께 오면 day=0 row 를 먼저 그 값으로 갱신 후 fan-out.
         - month_summary 생략 시 기존 day=0 row 사용. day=0 row 가 없으면 400.
    - apply_globally=True 시 shift_manage 템플릿(RN 슬롯 1/2/3/5)도 day=1 값으로 동기화.
    - max_enabled 는 일자별 모드에서만 date 의 *_count_max 정규화에 사용. 일괄적용 모드에서는 day=0 row 의 max_enabled 가 그대로 전파.
    """
    office_id: str
    group_id: str
    year: int
    month: int
    date: Optional[DailyShiftDateArrays] = Field(default=None, description="일자별 배열. apply_summary_to_days=True 시 생략 가능")
    max_enabled: bool = Field(default=False, description="일자별 모드 한정: date 의 *_max 사용 여부")
    apply_globally: bool = Field(default=False, description="True=shift_manage 템플릿(RN 1/2/3/5) day=1 값으로 동기화")
    month_summary: Optional[DailyShiftMonthSummary] = Field(default=None, description="월 일괄값(day=0 row 저장). 일괄적용 모드의 권위 자료")
    apply_summary_to_days: bool = Field(default=False, description="True=day=0 값을 day=1..말일에 fan-out (일괄적용)")


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


class DailyTeamEntry(BaseModel):
    """그날 그 팀의 최소 인원. 0 = 미지정(팀 수 규칙에 위임)."""

    team_id: int
    d_count: int = 0
    e_count: int = 0
    n_count: int = 0
    m_count: int = 0


class DailyTeamDay(BaseModel):
    """하루치 가동 팀.

    teams=None  미설정 → 전 팀 가동(현행 동작). 그날 설정을 지울 때도 이 값.
    teams=[]    거부(400). 저장하면 행 0개라 미설정과 구분이 안 된다.
    """

    day: int
    teams: Optional[List[DailyTeamEntry]] = None


class DailyTeamShiftReplace(BaseModel):
    """일자별 가동 팀 부분 저장. days 에 없는 날짜는 건드리지 않는다."""

    group_id: str
    year: int
    month: int
    days: List[DailyTeamDay] = Field(default_factory=list)
