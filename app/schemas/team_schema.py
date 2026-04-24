from typing import Dict, List, Optional
from pydantic import BaseModel, Field


class TeamOps(BaseModel):
    """팀 단위 증분 오퍼레이션(생성/이름변경/멤버 추가/해제/최소 시프트 설정)."""
    team_id: Optional[int] = None
    team_name: Optional[str] = None
    add: List[str] = Field(default_factory=list)
    remove: List[str] = Field(default_factory=list)
    # 팀별 일일 최소 시프트 커버리지(옵셔널). 예: {"D":1,"E":1,"N":0,"M":0}
    # None → 변경 없음, {} → 클리어(제약 없음)
    min_shift: Optional[Dict[str, int]] = None


class TeamBulkOpsRequest(BaseModel):
    """팀 증분 동기화 요청 바디.
    - teams: 팀별 add/remove/rename/create
    - delete_team_ids: 팀 삭제(soft) 목록
    """
    teams: List[TeamOps] = Field(default_factory=list)
    delete_team_ids: List[int] = Field(default_factory=list)


class TeamWithMembers(BaseModel):
    """팀 + 멤버 목록 응답 DTO."""
    team_id: int
    team_name: str
    team_members: List[str]
    min_shift: Optional[Dict[str, int]] = None


