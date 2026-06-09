from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from db.client2 import get_db
from routers.auth import get_current_user_from_cookie
from schemas.auth_schema import User as UserSchema
from schemas.team_schema import TeamBulkOpsRequest, TeamWithMembers
from services.team_service import list_teams_with_members, apply_team_ops
from services.team_classify_service import (
    preview_team_classification,
    apply_team_classification,
)
from services.ward_redistribute_service import (
    preview_ward_redistribution,
    apply_ward_redistribution,
    WardSetupError,
)
from services.group_access import (
    resolve_managed_group_ids,
    resolve_effective_group,
    caller_is_manager,
    assert_caller_can_access_group,
)


router = APIRouter(prefix="/teams", tags=["teams"])


class TeamClassifyPreviewRequest(BaseModel):
    year: int
    month: int
    group_id: str | None = None  # master_admin 만 타 그룹 지정 가능
    participant_ids: list[str] | None = None  # 참여 인원(미지정 시 전원). 미참여=미지정
    pair_decisions: dict[str, str] | None = None  # {preceptee_id: "keep"|"release"}


class TeamAssignmentItem(BaseModel):
    nurse_id: str
    team_id: int


class TeamClassifyApplyRequest(BaseModel):
    year: int
    month: int
    assignments: list[TeamAssignmentItem]
    group_id: str | None = None
    pair_decisions: dict[str, str] | None = None  # {preceptee_id: "release"} 프리셉터십 종료
    note: str | None = None


class WardRedistributePreviewRequest(BaseModel):
    group_ids: list[str]
    year: int
    month: int
    capacity_mode: str = "even"           # "even" | "explicit"
    target_sizes: dict[str, int] | None = None
    size_tolerance: int = 2
    churn_weight: float = 500.0
    participant_ids: list[str] | None = None  # 참여(이동 대상). 미지정=전원, 미참여=현재병동 고정


class WardAssignmentItem(BaseModel):
    nurse_id: str
    to_group_id: str
    team_id: int | None = None


class WardRedistributeApplyRequest(BaseModel):
    group_ids: list[str]
    year: int
    month: int
    assignments: list[WardAssignmentItem]
    note: str | None = None


def _assert_groups_managed(
    db: Session, current_user: UserSchema, group_ids: list[str]
) -> None:
    """병동 간 재분배: 관리자 + 모든 선택 그룹이 관리 그룹에 포함돼야 함."""
    if not caller_is_manager(db, current_user):
        raise HTTPException(status_code=403, detail="재분배 권한이 없습니다.")
    if len(set(group_ids)) < 2:
        raise HTTPException(status_code=400, detail="재분배는 2개 이상의 병동이 필요합니다.")
    managed = set(resolve_managed_group_ids(db, current_user))
    bad = [g for g in group_ids if g not in managed]
    if bad:
        raise HTTPException(status_code=403, detail=f"관리 권한이 없는 그룹: {bad}")


def _resolve_managed_target(
    db: Session, current_user: UserSchema, group_id: str | None
) -> str:
    """관리 가능한 그룹으로 대상 group_id 해소.

    그룹관리자는 home 외에 Group.hn_id 에 본인이 등록된 그룹도 관리한다
    (resolve_managed_group_ids). 지정한 group_id 가 관리 목록에 없으면 403.
    관리 그룹이 여럿인데 미지정이면 모호하므로 400.
    """
    if not caller_is_manager(db, current_user):
        raise HTTPException(status_code=403, detail="팀 분류 권한이 없습니다.")
    managed = resolve_managed_group_ids(db, current_user)
    if not managed:
        raise HTTPException(status_code=403, detail="관리 권한이 있는 그룹이 없습니다.")
    if group_id:
        if group_id not in managed:
            raise HTTPException(status_code=403, detail="해당 그룹 관리 권한이 없습니다.")
        return group_id
    if len(managed) == 1:
        return managed[0]
    raise HTTPException(
        status_code=400, detail="관리 그룹이 여러 개입니다. group_id 를 지정하세요."
    )


@router.get("", response_model=list[TeamWithMembers])
async def get_teams(
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    group_id: str | None = None,
    db: Session = Depends(get_db),
):
    try:
        # 토큰 group_id 대신 nurse_id→DB + groups.hn_id 로 해석(ADM 무지정 시 office-wide None).
        target_group_id = resolve_effective_group(
            db, current_user, group_id, require_group=False
        )
        return list_teams_with_members(db, current_user.office_id, target_group_id)
    except HTTPException:
        raise
    except Exception as e:
        print('[DEBUG] [teams.py - get_teams] office_id', current_user.office_id)
        print('[DEBUG] [teams.py - get_teams] group_id', group_id)
        print('[DEBUG] [teams.py - get_teams] current_user', current_user.__dict__)
        print('[DEBUG] [teams.py - get_teams] error', e)
        raise HTTPException(status_code=500, detail=f"팀 목록 조회 실패: {e}")


@router.put("", response_model=list[TeamWithMembers])
async def put_teams(
    body: TeamBulkOpsRequest,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    group_id: str | None = None,
    db: Session = Depends(get_db),
):
    # if not current_user or (not current_user.is_head_nurse and not current_user.is_master_admin):
    #     raise HTTPException(status_code=403, detail="권한 없음")
    # 토큰 group_id 대신 nurse_id→DB + groups.hn_id 로 해석(비ADM 은 managed 검증).
    target_group_id = resolve_effective_group(
        db, current_user, group_id, require_group=False
    )
    try:
        payload = [t.model_dump() for t in body.teams]
        return apply_team_ops(
            db,
            current_user.office_id,
            target_group_id,
            payload,
            delete_team_ids=body.delete_team_ids,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        print('[DEBUG] [teams.py - put_teams] office_id', current_user.office_id)
        print('[DEBUG] [teams.py - put_teams] group_id', current_user.group_id)
        print('[DEBUG] [teams.py - put_teams] current_user', current_user.__dict__)
        print('[DEBUG] [teams.py - put_teams] body.teams', body.teams)
        print('[DEBUG] [teams.py - put_teams] error', e)
        raise HTTPException(status_code=500, detail=f"팀 동기화 실패: {e}")


@router.post("/classify/preview")
async def classify_preview(
    body: TeamClassifyPreviewRequest,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """원티드 기반 팀 분류 미리보기 (read-only). 확정 원티드로 제안 팀+변경 diff 반환."""
    target_group_id = _resolve_managed_target(db, current_user, body.group_id)
    try:
        return preview_team_classification(
            db, group_id=target_group_id, year=body.year, month=body.month,
            participant_ids=body.participant_ids,
            pair_decisions=body.pair_decisions,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/classify/apply")
async def classify_apply(
    body: TeamClassifyApplyRequest,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """승인된 팀 분류를 permanent_change 이벤트로 발행 (대상월 1일 발효)."""
    target_group_id = _resolve_managed_target(db, current_user, body.group_id)
    try:
        return apply_team_classification(
            db,
            group_id=target_group_id,
            office_id=current_user.office_id,
            year=body.year,
            month=body.month,
            assignments=[a.model_dump() for a in body.assignments],
            pair_decisions=body.pair_decisions,
            note=body.note,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/redistribute/preview")
async def redistribute_preview(
    body: WardRedistributePreviewRequest,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """병동 간 재분배 미리보기 (read-only). 그룹→팀→간호사 + 이동 diff 반환.

    G1 미지정 병동이 있으면 422 + needs_g1_setup(병동 목록) — 프론트가 '시니어 지정' 유도.
    """
    _assert_groups_managed(db, current_user, body.group_ids)
    try:
        return preview_ward_redistribution(
            db, group_ids=body.group_ids, year=body.year, month=body.month,
            capacity_mode=body.capacity_mode, target_sizes=body.target_sizes,
            size_tolerance=body.size_tolerance, churn_weight=body.churn_weight,
            participant_ids=body.participant_ids,
        )
    except WardSetupError as e:
        raise HTTPException(
            status_code=422,
            detail={"message": str(e), "needs_g1_setup": e.wards},
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/redistribute/apply")
async def redistribute_apply(
    body: WardRedistributeApplyRequest,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """승인된 병동 간 재분배 발행: 이동→병동이동(transfer), 팀변경→permanent_change."""
    _assert_groups_managed(db, current_user, body.group_ids)
    try:
        return apply_ward_redistribution(
            db, group_ids=body.group_ids, year=body.year, month=body.month,
            assignments=[a.model_dump() for a in body.assignments],
            current_user=current_user, note=body.note,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
