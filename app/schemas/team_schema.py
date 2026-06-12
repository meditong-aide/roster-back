from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class TeamOps(BaseModel):
    """팀 단위 증분 오퍼레이션(생성/이름변경/멤버 추가/해제/최소 시프트/인계 정책 설정)."""
    team_id: Optional[int] = None
    team_name: Optional[str] = None
    add: List[str] = Field(default_factory=list)
    remove: List[str] = Field(default_factory=list)
    # 팀별 일일 최소 시프트 커버리지(옵셔널). 예: {"D":1,"E":1,"N":0,"M":0}
    # None → 변경 없음, {} → 클리어(제약 없음)
    min_shift: Optional[Dict[str, int]] = None
    # 팀 내 인계 제한 정책(옵셔널). 예:
    #   {"restrictions":[{"grades":[6,7,8],"block_same_shift":true,"block_adjacent":true}]}
    # None → 변경 없음, {} → 클리어
    handoff_policy: Optional[Dict[str, Any]] = None


class TeamBulkOpsRequest(BaseModel):
    """팀 증분 동기화 요청 바디.
    - teams: 팀별 add/remove/rename/create
    - delete_team_ids: 팀 삭제(soft) 목록
    """
    teams: List[TeamOps] = Field(default_factory=list)
    delete_team_ids: List[int] = Field(default_factory=list)
    # 선택 월(있으면 멤버 add/remove 를 nurse_team_period 에 valid_from=그 달 1일로 기록).
    #   UI 는 일자별 미지원 → 월 단위만. 없으면(레거시) 캐시(nurse.team_id)만 갱신.
    year: Optional[int] = None
    month: Optional[int] = None


class TeamWithMembers(BaseModel):
    """팀 + 멤버 목록 응답 DTO."""
    team_id: int
    team_name: str
    team_members: List[str]
    min_shift: Optional[Dict[str, int]] = None
    handoff_policy: Optional[Dict[str, Any]] = None


