"""근무표 생성 전 infeasibility precheck 엔드포인트.

POST /groups/{group_id}/roster/precheck

현재 구현은 프론트가 이미 보유한 데이터(nurses, teams, roster_config, team_coverage,
grade_constraints, fixed_assignments)를 payload로 직접 받아 precheck 로직을 수행한다.
백엔드에서 DB 조회를 일원화하려면 이 엔드포인트 내부에서 해당 서비스 함수들을 호출해
payload 를 조립하면 된다(후속 과제).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from db.client2 import get_db
from routers.auth import get_current_user_from_cookie
from schemas.auth_schema import User as UserSchema
from services.precheck.team_grade_precheck import (
    PrecheckInput,
    PrecheckNurse,
    run_precheck,
)


router = APIRouter(tags=["roster_precheck"])


class PrecheckNursePayload(BaseModel):
    nurse_id: str
    grade: Optional[int] = None
    team_id: Optional[Any] = None
    allowed_shifts: Optional[List[str]] = None
    join_day: int = 0
    leave_day: int
    personal_off_adjustment: int = 0
    fixed_off_days: List[int] = Field(default_factory=list)
    fixed_shift_assignments: Dict[str, str] = Field(default_factory=dict)


class PrecheckRequest(BaseModel):
    num_days: int
    nurses: List[PrecheckNursePayload]
    teams: List[Any] = Field(default_factory=list)
    roster_config: Dict[str, Any]
    team_coverage: Dict[str, Dict[str, int]] = Field(default_factory=dict)
    grade_constraints: Dict[str, Any] = Field(default_factory=dict)
    stop_on_config_error: bool = False


class PrecheckIssue(BaseModel):
    reason_code: str
    severity: str
    evidence: Dict[str, Any]


class PrecheckResponse(BaseModel):
    status: str
    issues: List[PrecheckIssue]


def _to_input(req: PrecheckRequest) -> PrecheckInput:
    nurses = [
        PrecheckNurse(
            nurse_id=n.nurse_id,
            grade=n.grade,
            team_id=n.team_id,
            allowed_shifts=n.allowed_shifts,
            join_day=int(n.join_day or 0),
            leave_day=int(n.leave_day),
            personal_off_adjustment=int(n.personal_off_adjustment or 0),
            fixed_off_days=set(int(d) for d in (n.fixed_off_days or [])),
            fixed_shift_assignments={int(k): str(v) for k, v in (n.fixed_shift_assignments or {}).items()},
        )
        for n in req.nurses
    ]
    return PrecheckInput(
        num_days=int(req.num_days),
        nurses=nurses,
        teams=list(req.teams or []),
        roster_config=dict(req.roster_config or {}),
        team_coverage=dict(req.team_coverage or {}),
        grade_constraints=dict(req.grade_constraints or {}),
    )


@router.post("/groups/{group_id}/roster/precheck", response_model=PrecheckResponse)
def precheck_roster(
    group_id: str,
    req: PrecheckRequest,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """Team × Grade × Common Pool infeasibility precheck."""
    try:
        inp = _to_input(req)
        result = run_precheck(inp, stop_on_config_error=bool(req.stop_on_config_error))
        return result
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"precheck failed: {e}")
