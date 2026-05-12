"""
간호사 정보 관리 관련 서비스 로직 모듈
- DB 쿼리, 데이터 가공 등 라우터에서 분리
- 모든 함수는 한글 docstring, 한글 print/logging, PEP8 스타일 적용
"""

from sqlalchemy.orm import Session
from sqlalchemy import or_, select
from fastapi import HTTPException
from db.models import (
    Nurse as NurseModel,
    Group,
    DeletedNurseHistory,
    NurseAssignment,
    RosterConfig as RosterConfigModel,
    Shift,
)
from services.assignment_service import _SOURCE_TO_TARGET_FIELD_MAP
from schemas.roster_schema import NurseProfile, NurseProfileUpdate
from schemas.auth_schema import User as UserSchema
from typing import List, Optional, Dict, Any, Tuple
from pprint import pprint
from datetime import date, datetime
from dateutil.parser import parse as parse_date
from db.client2 import msdb_manager
from datalayer.member import Member
import logging
import os
import secrets
import boto3
from urllib.parse import quote


def _build_profile_image_url(image_key: Optional[str]) -> Optional[str]:
    if not image_key:
        return None
    bucket_name = (
        os.getenv("AWS_SHARE_S3_BUCKET")
        or os.getenv("SHARE_S3_BUCKET")
        or os.getenv("S3_SHARE_BUCKET")
    )
    if not bucket_name:
        return None
    region = os.getenv("AWS_REGION", "ap-northeast-2")
    try:
        s3_client = _build_s3_client(region)
        return s3_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket_name, "Key": str(image_key)},
            ExpiresIn=86400,
        )
    except Exception:
        encoded_key = quote(str(image_key), safe="/")
        return f"https://{bucket_name}.s3.{region}.amazonaws.com/{encoded_key}"


def get_profile_image_url(image_key: Optional[str]) -> Optional[str]:
    return _build_profile_image_url(image_key)


def _build_s3_client(region: str):
    access_key = os.getenv("AWS_ACCESS_KEY_ID") or os.getenv("AWS_ACCESS_KEY")
    secret_key = os.getenv("AWS_SECRET_ACCESS_KEY") or os.getenv("AWS_SECRET_KEY")
    session_token = os.getenv("AWS_SESSION_TOKEN")
    profile_name = os.getenv("AWS_PROFILE")

    if access_key and secret_key:
        return boto3.client(
            "s3",
            region_name=region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            aws_session_token=session_token,
        )

    if profile_name:
        session = boto3.Session(profile_name=profile_name, region_name=region)
        return session.client("s3")

    session = boto3.Session(region_name=region)
    return session.client("s3")


def _get_display_flags(db: Session, group_id: str) -> dict:
    """그룹의 최신 roster_config에서 show_level, show_preceptor 플래그 조회"""
    config = (
        db.query(RosterConfigModel)
        .filter(RosterConfigModel.group_id == group_id)
        .order_by(RosterConfigModel.created_at.desc())
        .first()
    )
    return {
        "show_level": getattr(config, "show_level", True) if config else True,
        "show_preceptor": getattr(config, "show_preceptor", True) if config else True,
    }


def get_role_group(role: str) -> str:
    """role 값을 RN/AN/ETC 그룹으로 분류"""
    if role == "RN":
        return "RN"
    elif role == "AN":
        return "AN"
    else:
        return "ETC"

def _role_group_filter(role_group: str):
    """role 그룹에 해당하는 SQLAlchemy 필터 조건 반환"""
    if role_group == "RN":
        return NurseModel.role == "RN"
    elif role_group == "AN":
        return NurseModel.role == "AN"
    else:
        return NurseModel.role.not_in(["RN", "AN"])

def get_personnel_basic_info_service(current_user, db: Session):
    """
    간호사 기본 정보 조회 서비스 함수
    """

    try:
        if not current_user:
            raise Exception("Not authenticated")

        nurse = (
            db.query(NurseModel)
            .filter(
                NurseModel.group_id == current_user.group_id,
                NurseModel.nurse_id == current_user.nurse_id,
            )
            .first()
        )
        return nurse
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"간호사 기본 정보 조회 실패: {str(e)}"
        )


def get_nurses_in_group_service(
    current_user, db: Session, nurse_id: Optional[str] = None,
    skip_group_filter: bool = False,
    override_group_id: Optional[str] = None,
):
    """
    그룹 내 간호사 목록 조회 서비스 함수
    특정 nurse_id가 제공되면 해당 간호사만 반환, 그렇지 않으면 그룹 내 모든 간호사 반환
    birth_date (VARCHAR)를 파싱하여 만 나이를 age로 추가
    skip_group_filter: True면 group_id 필터 스킵 (파견/병동이동 인바운드 조회용)
    override_group_id: HN 등이 query param 으로 다른 그룹 조회 시 사용. 권한 검증은 라우터 책임.
    """
    if not current_user:
        raise Exception("Not authenticated")

    # 다른 그룹 view 강제 시 override_group_id 우선, 아니면 토큰 group_id
    effective_group_id = override_group_id or current_user.group_id

    query = db.query(NurseModel)

    # 그룹 ID 필터링: source(nurses.group_id) + inbound(assignment.target_group_id)
    # 정책: status='active' 인 inbound assignment 가 있는 nurse 는 미래 시작 여부와 무관하게 노출.
    #   - source 측: Nurse.group_id 매칭으로 outbound nurse 도 자연 노출 (양쪽 노출).
    #   - 실근무 일자가 아닌 셀은 솔버의 active_window/blocked_days 로 제외되므로 안전.
    if not skip_group_filter and effective_group_id:
        _inbound_subq = (
            db.query(NurseAssignment.nurse_id)
            .filter(
                NurseAssignment.target_group_id == effective_group_id,
                NurseAssignment.status == "active",
                NurseAssignment.reason.in_(_INBOUND_REASONS),
            )
            .subquery()
        )
        query = query.filter(
            or_(
                NurseModel.group_id == effective_group_id,
                NurseModel.nurse_id.in_(select(_inbound_subq.c.nurse_id)),
            )
        )

    # 특정 nurse_id 필터링
    if nurse_id:
        query = query.filter(NurseModel.nurse_id == nurse_id)

    # 정렬: active DESC, role DESC(RN→AN), sequence ASC, experience DESC, nurse_id ASC
    nurses = query.order_by(
        NurseModel.active.desc(),
        NurseModel.role.desc(),
        NurseModel.sequence.asc(),
        NurseModel.experience.desc(),
        NurseModel.nurse_id.asc(),
    ).all()

    # nurse_id로 필터링했는데 결과가 없으면 예외
    if nurse_id and not nurses:
        raise Exception(f"Nurse with nurse_id {nurse_id} not found")

    # roster_config에서 표시 설정 플래그 조회 (override 그룹이면 그쪽 flag)
    display_flags = _get_display_flags(db, effective_group_id)

    # 만 나이 계산
    current_date = date.today()

    def calculate_age(birth_date_str: str) -> Optional[int]:
        if not birth_date_str:
            return None
        try:
            birth_date = parse_date(birth_date_str).date()
            current_year = current_date.year
            birth_year = birth_date.year
            age = current_year - birth_year
            if (current_date.month, current_date.day) < (
                birth_date.month,
                birth_date.day,
            ):
                age -= 1
            return age
        except (ValueError, TypeError) as e:
            logging.warning(f"Invalid birth_date format: {birth_date_str}, error: {e}")
            return None

    # inbound assignment 배치 로드 (응답 overlay용; skip_group_filter 시에도 표시 일관성 위해 조회)
    inbound_map: Dict[str, NurseAssignment] = {}
    inbound_blocks: Dict[str, Dict[str, Any]] = {}
    if nurses:
        _nids = [n.nurse_id for n in nurses]
        if effective_group_id:
            inbound_map = _load_inbound_map(db, effective_group_id, _nids)
        inbound_blocks = _build_inbound_blocks(db, _nids)

    # 결과 변환: NurseProfile과 호환
    result = []
    for nurse in nurses:
        is_night_nurse = nurse.is_night_nurse or []

        nurse_dict = {
            "nurse_id": nurse.nurse_id,
            "group_id": nurse.group_id,
            "office_id": nurse.office_id,
            "emp_num": nurse.emp_num,
            "account_id": nurse.account_id,
            "name": nurse.name,
            "experience": nurse.experience,
            "role": nurse.role,
            "level_": nurse.level_,
            "is_head_nurse": nurse.is_head_nurse,
            "is_night_nurse": is_night_nurse,
            "personal_off_adjustment": nurse.personal_off_adjustment,
            "preceptor_id": nurse.preceptor_id,
            "joining_date": nurse.joining_date.isoformat() if nurse.joining_date else None,
            "resignation_date": nurse.resignation_date.isoformat() if nurse.resignation_date else None,
            "resignation_reason": nurse.resignation_reason,
            "resignation_reason_memo": nurse.resignation_reason_memo,
            "sequence": nurse.sequence,
            "active": nurse.active,
            "birth_date": nurse.birth_date,
            "phone_number": nurse.phone_number,
            "nurse_memo": nurse.nurse_memo,
            "grade": nurse.grade,
            "team_id": nurse.team_id,
            "fixed_shift": nurse.fixed_shift,
            "weekly_off_enabled": nurse.weekly_off_enabled,
            "weekly_off_weekday": nurse.weekly_off_weekday,
            "age": calculate_age(nurse.birth_date),
            "gender": nurse.gender,
            "is_weekend_off": nurse.is_weekend_off,
            "work_shifts": nurse.work_shifts
            or [],  # JSON 컬럼이므로 None일 수 있음 → []로 변환
            # 원티드 설정 (간호사별 개별 설정)
            "enable_nurse_pair_preference": nurse.enable_nurse_pair_preference,
            "enable_aide": nurse.enable_aide,
            "wanted_max_requests": nurse.wanted_max_requests,
            "is_inbound": False,
            # 근무표 설정 메타 플래그
            **display_flags,
        }
        inbound_row = inbound_map.get(nurse.nurse_id)
        if inbound_row is not None:
            _overlay_inbound_fields(nurse_dict, inbound_row)
        _inbound_block = inbound_blocks.get(nurse.nurse_id)
        if _inbound_block:
            nurse_dict["inbound"] = list(_inbound_block.get("inbound_list", []))
            nurse_dict["current_assignment"] = _inbound_block.get("current_assignment")
        else:
            nurse_dict["inbound"] = []
            nurse_dict["current_assignment"] = None
        result.append(nurse_dict)

    return result


def get_nurses_filtered_service(
    current_user,
    db: Session,
    office_id: Optional[str] = None,
    group_id: Optional[str] = None,
    nurse_id: Optional[str] = None,
):
    """ADM 전용 필터 조회: office_id, group_id, nurse_id 기준으로 간호사 목록 조회"""
    if not current_user:
        raise Exception("Not authenticated")
    if not getattr(current_user, "is_master_admin", False):
        raise Exception("Permission denied")

    q = db.query(NurseModel)

    # 필터링: nurse_id
    if nurse_id is not None:
        q = q.filter(NurseModel.nurse_id == nurse_id)

    # 필터링: group_id
    if group_id is not None:
        q = q.filter(
            NurseModel.group_id == group_id,
            NurseModel.office_id == current_user.office_id,
        )

    # 필터링: office_id
    elif current_user.office_id is not None:
        q = q.join(Group, Group.group_id == NurseModel.group_id).filter(
            Group.office_id == current_user.office_id
        )

    # 정렬: active DESC, role DESC(RN→AN), sequence ASC, experience DESC, nurse_id ASC
    nurses = q.order_by(
        NurseModel.active.desc(),
        NurseModel.role.desc(),
        NurseModel.sequence.asc(),
        NurseModel.experience.desc(),
        NurseModel.nurse_id.asc(),
    ).all()
    # nurse_id로 필터링했는데 결과가 없으면 예외
    if nurse_id is not None and not nurses:
        raise Exception(f"Nurse with nurse_id {nurse_id} not found")

    # roster_config에서 표시 설정 플래그 조회 (ADM: group_id 파라미터 우선, 없으면 첫 번째 간호사의 group_id 사용)
    target_gid = group_id or (nurses[0].group_id if nurses else None)
    display_flags = (
        _get_display_flags(db, target_gid)
        if target_gid
        else {"show_level": True, "show_preceptor": True}
    )

    # 만 나이 계산
    current_date = date.today()

    def calculate_age(birth_date_str: str) -> Optional[int]:
        if not birth_date_str:
            return None
        try:
            birth_date = parse_date(birth_date_str).date()
            current_year = current_date.year
            birth_year = birth_date.year
            age = current_year - birth_year
            if (current_date.month, current_date.day) < (
                birth_date.month,
                birth_date.day,
            ):
                age -= 1
            return age
        except (ValueError, TypeError) as e:
            logging.warning(f"Invalid birth_date format: {birth_date_str}, error: {e}")
            return None

    # 결과 변환: NurseProfile과 호환
    result = []
    for nurse in nurses:
        is_night_nurse = nurse.is_night_nurse or []

        nurse_dict = {
            "nurse_id": nurse.nurse_id,
            "group_id": nurse.group_id,
            "office_id": nurse.office_id,
            "emp_num": nurse.emp_num,
            "account_id": nurse.account_id,
            "name": nurse.name,
            "experience": nurse.experience,
            "role": nurse.role,
            "level_": nurse.level_,
            "is_head_nurse": nurse.is_head_nurse,
            "is_night_nurse": is_night_nurse,
            "personal_off_adjustment": nurse.personal_off_adjustment,
            "preceptor_id": nurse.preceptor_id,
            "joining_date": nurse.joining_date.isoformat() if nurse.joining_date else None,
            "resignation_date": nurse.resignation_date.isoformat() if nurse.resignation_date else None,
            "resignation_reason": nurse.resignation_reason,
            "resignation_reason_memo": nurse.resignation_reason_memo,
            "sequence": nurse.sequence,
            "active": nurse.active,
            "birth_date": nurse.birth_date,
            "phone_number": nurse.phone_number,
            "nurse_memo": nurse.nurse_memo,
            "grade": nurse.grade,
            "team_id": nurse.team_id,
            "weekly_off_enabled": nurse.weekly_off_enabled,
            "weekly_off_weekday": nurse.weekly_off_weekday,
            "age": calculate_age(nurse.birth_date),
            "gender": nurse.gender,
            "is_weekend_off": nurse.is_weekend_off,
            "work_shifts": nurse.work_shifts
            or [],  # JSON 컬럼이므로 None일 수 있음 → []로 변환
            # 원티드 설정 (간호사별 개별 설정)
            "enable_nurse_pair_preference": nurse.enable_nurse_pair_preference,
            "enable_aide": nurse.enable_aide,
            "wanted_max_requests": nurse.wanted_max_requests,
            # 근무표 설정 메타 플래그
            **display_flags,
        }
        result.append(nurse_dict)

    return result


def get_next_sequence_for_active_status(group_id: str, active_status: int, db: Session, role: str) -> int:
    """
    특정 active 상태 + role 그룹에서 다음 sequence 번호 반환
    """
    role_group = get_role_group(role)
    max_sequence = db.query(NurseModel.sequence).filter(
        NurseModel.group_id == group_id,
        NurseModel.active == active_status,
        _role_group_filter(role_group)
    ).order_by(NurseModel.sequence.desc()).first()

    return (max_sequence[0] + 1) if max_sequence and max_sequence[0] is not None else 1

def _get_nurses_by_active(group_id: str, active_status: int, db: Session, role_group: str = None) -> List[NurseModel]:
    q = (
        db.query(NurseModel)
        .filter(NurseModel.group_id == group_id, NurseModel.active == active_status)
    )
    if role_group:
        q = q.filter(_role_group_filter(role_group))
    return (
        q.order_by(NurseModel.role.desc(), NurseModel.sequence.asc(), NurseModel.nurse_id.asc())
        .all()
    )


def _reindex_contiguously(nurses: List[NurseModel]) -> None:
    """sequence를 1부터 연속되게 재부여"""
    for idx, n in enumerate(nurses, start=1):
        if n.sequence != idx:
            n.sequence = idx


def move_nurse_with_active_service(
    nurse_id: str,
    new_sequence: int,
    target_active: Optional[int],
    current_user,
    db: Session,
):
    """
    간호사 이동/상태변경을 단일 트랜잭션으로 처리.
    - target_active가 None이면 같은 active 안에서 재배치
    - target_active가 0/1이면 해당 상태 리스트로 이동 후 삽입
    """
    if not current_user:
        raise Exception("Not authenticated")
    # 수간호사 또는 마스터관리자만 허용
    if not (
        getattr(current_user, "is_head_nurse", False)
        or getattr(current_user, "is_master_admin", False)
    ):
        raise Exception("Permission denied")

    # ADM은 group_id가 없을 수 있으므로 nurse_id만으로 조회 후 대상 그룹을 결정
    if getattr(current_user, "is_master_admin", False) and not getattr(
        current_user, "group_id", None
    ):
        nurse = (
            db.query(NurseModel)
            .select_from(NurseModel)
            .filter(NurseModel.nurse_id == nurse_id)
            .first()
        )
        target_group_id = nurse.group_id if nurse else None
    else:
        nurse = (
            db.query(NurseModel)
            .filter(
                NurseModel.group_id == current_user.group_id,
                NurseModel.nurse_id == nurse_id,
            )
            .first()
        )
        target_group_id = getattr(current_user, "group_id", None)
    if not nurse:
        raise Exception("간호사를 찾을 수 없습니다.")

    group_id = target_group_id
    old_active = nurse.active
    role_group = get_role_group(nurse.role)

    if target_active is None or target_active == old_active:
        # 같은 상태 내 재배치 (같은 role 그룹 내에서만)
        lst = _get_nurses_by_active(group_id, old_active, db, role_group)
        # 해당 간호사 제외
        lst = [n for n in lst if n.nurse_id != nurse.nurse_id]
        # 경계 보정 (1-based)
        insert_idx = max(0, min(new_sequence - 1, len(lst)))
        lst.insert(insert_idx, nurse)
        _reindex_contiguously(lst)
    else:
        # 상태 변경: 기존 리스트에서 제거하고 reindex (같은 role 그룹 내)
        old_list = _get_nurses_by_active(group_id, old_active, db, role_group)
        old_list = [n for n in old_list if n.nurse_id != nurse.nurse_id]
        _reindex_contiguously(old_list)
        # 새 리스트로 이동하여 삽입 (같은 role 그룹 내)
        nurse.active = target_active
        new_list = _get_nurses_by_active(group_id, target_active, db, role_group)
        # autoflush로 인해 nurse가 이미 new_list에 포함될 수 있으므로 제거 후 삽입
        new_list = [n for n in new_list if n.nurse_id != nurse.nurse_id]
        insert_idx = max(0, min(new_sequence - 1, len(new_list)))
        new_list.insert(insert_idx, nurse)
        _reindex_contiguously(new_list)

    db.commit()
    return {"message": "순서/상태 변경 완료"}


def reorder_nurses_service(
    active_order: List[str], inactive_order: List[str], current_user, db: Session
):
    """
    드래그앤드롭 완료 시점에 한 번 호출하여
    - active_order에 포함된 간호사들은 active=1로 설정하고 순서를 1..N 부여
    - inactive_order는 active=0으로 설정하고 순서를 1..M 부여
    - 전달되지 않은 간호사는 상태/순서 변경하지 않음(React 측에서 전체 보냄을 권장)
    """
    if not current_user:
        raise Exception("Not authenticated")
    if not current_user.is_head_nurse:
        raise Exception("Permission denied")

    group_id = current_user.group_id
    id_to_nurse = {
        n.nurse_id: n
        for n in db.query(NurseModel).filter(NurseModel.group_id == group_id).all()
    }

    # role 그룹별로 분리하여 sequence 부여
    def _assign_sequences_by_role_group(order_list: List[str], active_val: int):
        role_counters = {}  # role_group별 sequence 카운터
        for nid in order_list:
            n = id_to_nurse.get(nid)
            if not n:
                continue
            n.active = active_val
            rg = get_role_group(n.role)
            role_counters[rg] = role_counters.get(rg, 0) + 1
            n.sequence = role_counters[rg]

    _assign_sequences_by_role_group(active_order, 1)
    _assign_sequences_by_role_group(inactive_order, 0)

    db.commit()
    return {
        "message": "일괄 재정렬 완료",
        "active_count": len(active_order),
        "inactive_count": len(inactive_order),
    }


def bulk_update_nurses_service(
    nurses_data: List[NurseProfile],
    current_user: UserSchema,
    db: Session,
    override_group_id: Optional[str] = None,
):
    """
    간호사 일괄 업데이트 서비스 함수
    """
    if not current_user:
        raise Exception("Not authenticated")

    # 권한 체크: 수간호사(HDN) 또는 마스터 관리자(ADM)
    if current_user.is_master_admin:
        target_group_id = override_group_id
    else:
        target_group_id = current_user.group_id

    if not target_group_id:
        raise Exception("그룹 ID를 확인할 수 없습니다.")

    # 그룹 내 모든 간호사 + inbound 간호사까지 미리 로드 (target 모드 지원)
    _inbound_ids = [
        row.nurse_id
        for row in db.query(NurseAssignment.nurse_id)
        .filter(
            NurseAssignment.target_group_id == target_group_id,
            NurseAssignment.status == "active",
            NurseAssignment.reason.in_(_INBOUND_REASONS),
        )
        .all()
    ]
    db_nurses_dict = {
        n.nurse_id: n
        for n in db.query(NurseModel)
        .filter(
            or_(
                NurseModel.group_id == target_group_id,
                NurseModel.nurse_id.in_(_inbound_ids) if _inbound_ids else False,
            )
        )
        .all()
    }

    # source/target 분기 판단 1회로 dict 확보
    _nurse_ids = [p.nurse_id for p in nurses_data]
    _edit_modes = resolve_edit_targets_batch(db, target_group_id, _nurse_ids)

    # use_mid 여부 조회 (최신 roster_config 기준)
    latest_config = (
        db.query(RosterConfigModel)
        .filter(RosterConfigModel.group_id == target_group_id)
        .order_by(RosterConfigModel.created_at.desc())
        .first()
    )
    use_mid = bool(getattr(latest_config, "use_mid", False)) if latest_config else False
    ALL_SHIFTS = {"D", "E", "N", "M"} if use_mid else {"D", "E", "N"}

    # shift_id → 상위 그룹(D/E/N/M) 매핑 (shift_gb 기준)
    SHIFT_GB_MAP = {"데이": "D", "이브닝": "E", "나이트": "N", "미드": "M"}
    shift_to_group = {}
    group_shifts = (
        db.query(Shift.shift_id, Shift.shift_gb)
        .filter(Shift.group_id == target_group_id)
        .all()
    )
    for sid, sgb in group_shifts:
        if sgb and sgb in SHIFT_GB_MAP:
            shift_to_group[sid] = SHIFT_GB_MAP[sgb]

    updated_count = 0
    # source 경로에서 preceptor_id 변경된 간호사 기록 (commit 후 프리셉티 assignment 동기화용)
    preceptor_changes: List[Tuple[NurseModel, Optional[str], Optional[str]]] = []

    for profile in nurses_data:
        db_nurse = db_nurses_dict.get(profile.nurse_id)
        if not db_nurse:
            continue

        update_data = profile.dict(exclude_unset=True)

        # === 배정(파견/병동이동/휴직/퇴사/프리셉티) payload 먼저 처리 ===
        # 단건(assignment) + 다건(assignments) 모두 지원. 다건이 먼저 적용된 후 단건이 뒤따른다.
        assignment_payload = update_data.pop("assignment", None)
        assignments_payload = update_data.pop("assignments", None)
        _payloads_to_apply: List[Dict[str, Any]] = []
        if assignments_payload:
            _payloads_to_apply.extend([p for p in assignments_payload if p])
        if assignment_payload is not None:
            _payloads_to_apply.append(assignment_payload)
        # 처리 순서 정합: cancel → update → create (단건 PATCH 와 동일)
        _op_order = {"cancel": 0, "update": 1, "create": 2}
        _payloads_to_apply.sort(key=lambda p: _op_order.get(p.get("operation"), 99))
        if _payloads_to_apply:
            for _p in _payloads_to_apply:
                _dispatch_assignment_payload(
                    db, profile.nurse_id, _p, current_user
                )
            db.refresh(db_nurse)
            # 배정 dispatch로 edit mode가 바뀔 수 있음 → 재평가
            mode, assign_row = resolve_edit_target(db, target_group_id, profile.nurse_id)
        else:
            mode, assign_row = _edit_modes.get(profile.nurse_id, ("none", None))

        if mode == "target" and assign_row is not None:
            # Target 병동 모드: nurse_assignment.target_* 저장
            _apply_target_update(assign_row, update_data)
            updated_count += 1
            continue

        if mode != "source":
            # 배정 payload만 처리하고 프로필 필드 업데이트 권한은 없을 수 있음
            if _payloads_to_apply and not update_data:
                updated_count += 1
                continue
            logging.warning(
                "[bulk_update_nurses] 권한 밖 간호사 skip: nurse_id=%s", profile.nurse_id
            )
            continue

        # === Source 모드: nurses.* 직접 저장 (기존 경로) ===
        # preceptor_id 변경 탐지 (commit 후 프리셉티 assignment 자동 동기화용)
        _preceptor_field_present = "preceptor_id" in update_data
        _preceptor_before = db_nurse.preceptor_id if _preceptor_field_present else None

        # active 또는 role 변경 시 sequence 자동 조정
        old_active = db_nurse.active
        old_role = db_nurse.role
        new_active = update_data.get('active', old_active)
        new_role = update_data.get('role', old_role)

        if (old_active != new_active and 'active' in update_data) or \
           (old_role != new_role and 'role' in update_data):
            # role 그룹이 바뀌거나 active가 바뀌면 새 그룹의 마지막 sequence로 이동
            update_data['sequence'] = get_next_sequence_for_active_status(
                target_group_id, new_active, db, role=new_role
            )

        if 'email' in update_data:
            email_value = update_data.get('email')
            if (email_value is None or (isinstance(email_value, str) and not email_value.strip())) and db_nurse.email:
                update_data.pop('email')

        # === 프론트에서 보내준 값으로 일괄 업데이트 ===
        for key, value in update_data.items():
            if hasattr(db_nurse, key):
                setattr(db_nurse, key, value)

        # === 후처리: is_night_nurse (전체 선택 시 빈 배열) ===
        # 각 shift_id를 상위 그룹(D/E/N/M)으로 정규화 후 중복 제거
        # use_mid=False: D,E,N 3종 전부 선택 시 빈 배열
        # use_mid=True:  D,E,N,M 4종 전부 선택 시 빈 배열
        if "is_night_nurse" in update_data:
            night_shifts = update_data["is_night_nurse"]
            if isinstance(night_shifts, list) and night_shifts:
                normalized = {shift_to_group.get(s, s) for s in night_shifts}
                if normalized == ALL_SHIFTS:
                    db_nurse.is_night_nurse = []
            elif night_shifts is None:
                db_nurse.is_night_nurse = []

        # === 후처리: work_shifts (None → 빈 배열) ===
        if "work_shifts" in update_data and update_data["work_shifts"] is None:
            db_nurse.work_shifts = []

        # === source 경로에서 preceptor_id 필드 수신분 기록 (commit 후 동기화) ===
        # 값 변경이 없어도 nurse_assignment 비대칭이 있을 수 있으므로 sync 호출 대상에 포함.
        if _preceptor_field_present:
            preceptor_changes.append(
                (db_nurse, _preceptor_before, db_nurse.preceptor_id)
            )

        updated_count += 1

    # === 클라이언트에서 제외된 간호사 확인 ===
    client_nurse_ids = {profile.nurse_id for profile in nurses_data}
    for db_nurse_id, db_nurse in list(db_nurses_dict.items()):
        if db_nurse_id not in client_nurse_ids:
            print("제외된 간호사 for test:", db_nurse_id)

    # === active 상태 × role 그룹별 sequence 재정렬 (갭/중복 방지) ===
    # inbound(파견/병동이동) 간호사는 제외 — source 병동 기준으로만 재정렬
    _source_nurses = [
        n for n in db_nurses_dict.values() if n.group_id == target_group_id
    ]
    for active_status in (1, 0):
        for rg in ("RN", "AN", "ETC"):
            nurses_in_group = sorted(
                [n for n in _source_nurses
                 if n.active == active_status and get_role_group(n.role) == rg],
                key=lambda n: (n.sequence, n.nurse_id),
            )
            _reindex_contiguously(nurses_in_group)

    db.commit()

    # === commit 후: preceptor_id 변경분에 대해 프리셉티 assignment 동기화 ===
    # (None → 값): 신규 프리셉티 assignment 생성 / (값 → None): 기존 active assignment cancel
    for _nurse, _before, _after in preceptor_changes:
        try:
            _sync_preceptee_assignment(
                db, _nurse,
                previous_preceptor_id=_before,
                new_preceptor_id=_after,
                current_user=current_user,
            )
        except HTTPException:
            raise
        except Exception as e:
            logging.warning(
                "[bulk_preceptee_sync] 실패 nurse=%s: %s",
                _nurse.nurse_id, e,
            )

    return {
        "message": "간호사 정보가 성공적으로 업데이트되었습니다.",
        "updated": updated_count,
    }


def move_nurse_service(req, current_user, db: Session):
    """
    간호사 순서 이동 서비스 함수 (같은 active 상태 내에서만 이동)
    """
    if not current_user:
        raise Exception("Not authenticated")
    if not current_user.is_head_nurse:
        raise Exception("Permission denied")

    nurse_to_move = (
        db.query(NurseModel)
        .filter(
            NurseModel.nurse_id == req.nurse_id,
            NurseModel.group_id == current_user.group_id,
        )
        .first()
    )

    if not nurse_to_move:
        raise Exception("해당 간호사를 찾을 수 없습니다.")

    old_sequence = nurse_to_move.sequence
    new_sequence = req.new_sequence
    active_status = nurse_to_move.active
    role_grp_filter = _role_group_filter(get_role_group(nurse_to_move.role))

    print(f"[DEBUG] 순서 이동: {nurse_to_move.name} (active={active_status}, role={nurse_to_move.role}) {old_sequence} → {new_sequence}")

    if old_sequence == new_sequence:
        return {"message": "변경사항이 없습니다."}

    # 같은 active 상태 + 같은 role 그룹의 간호사들만 대상으로 sequence 재조정
    if old_sequence < new_sequence:
        affected_nurses = db.query(NurseModel).filter(
            NurseModel.group_id == current_user.group_id,
            NurseModel.active == active_status,
            role_grp_filter,
            NurseModel.sequence > old_sequence,
            NurseModel.sequence <= new_sequence
        ).all()

        for nurse in affected_nurses:
            nurse.sequence -= 1
            print(
                f"[DEBUG] {nurse.name} sequence: {nurse.sequence + 1} → {nurse.sequence}"
            )
    else:
        affected_nurses = db.query(NurseModel).filter(
            NurseModel.group_id == current_user.group_id,
            NurseModel.active == active_status,
            role_grp_filter,
            NurseModel.sequence >= new_sequence,
            NurseModel.sequence < old_sequence
        ).all()

        for nurse in affected_nurses:
            nurse.sequence += 1
            print(
                f"[DEBUG] {nurse.name} sequence: {nurse.sequence - 1} → {nurse.sequence}"
            )

    # 이동할 간호사의 sequence 업데이트
    nurse_to_move.sequence = new_sequence
    print(f"[DEBUG] {nurse_to_move.name} 최종 sequence: {new_sequence}")

    db.commit()
    return {"message": "간호사 순서 변경 완료"}


def get_available_members_service(
    office_id: str, group_id: str, db: Session
) -> List[Dict[str, Any]]:
    """
    동일 오피스의 전체 멤버 중 현재 그룹에 속하지 않은 멤버 목록 반환.
    - MSSQL Member 테이블에서 오피스 전체 멤버 조회
    - MySQL nurses 테이블에서 해당 group_id에 이미 등록된 nurse_id 조회
    - 이미 등록된 멤버를 제외한 나머지 반환
    """
    # 1. MSSQL에서 오피스 전체 멤버 조회 (export-members와 동일 쿼리)
    all_members = (
        msdb_manager.fetch_all(
            Member.member_export_by_office(), params=(str(office_id),)
        )
        or []
    )

    # 2. 현재 group_id에 이미 등록된 간호사의 nurse_id 집합
    existing_nurse_ids = set(
        row[0]
        for row in db.query(NurseModel.nurse_id)
        .filter(NurseModel.group_id == group_id)
        .all()
    )

    # 3. 이미 등록된 멤버 제외
    available = []
    for row in all_members:
        emp_seq_no = (
            row.get("EmpSeqNo")
            if isinstance(row, dict)
            else getattr(row, "EmpSeqNo", None)
        )
        if emp_seq_no and str(emp_seq_no) not in existing_nurse_ids:
            member_dict = {
                "nurse_id": str(emp_seq_no),
                "emp_num": row.get("OfficeEmpNum")
                if isinstance(row, dict)
                else getattr(row, "OfficeEmpNum", None),
                "account_id": row.get("MemberID")
                if isinstance(row, dict)
                else getattr(row, "MemberID", None),
                "name": row.get("EmployeeName")
                if isinstance(row, dict)
                else getattr(row, "EmployeeName", None),
                "duty": row.get("duty")
                if isinstance(row, dict)
                else getattr(row, "duty", None),
                "career": row.get("career")
                if isinstance(row, dict)
                else getattr(row, "career", None),
                "is_head_nurse": row.get("headnurse")
                if isinstance(row, dict)
                else getattr(row, "headnurse", None),
                "joining_date": row.get("joindate")
                if isinstance(row, dict)
                else getattr(row, "joindate", None),
                "birth_date": row.get("DateOfBirth")
                if isinstance(row, dict)
                else getattr(row, "DateOfBirth", None),
                "phone_number": row.get("PortableTel")
                if isinstance(row, dict)
                else getattr(row, "PortableTel", None),
                "gender": row.get("Gender")
                if isinstance(row, dict)
                else getattr(row, "Gender", None),
                "big_kind_name": row.get("big_kind_name")
                if isinstance(row, dict)
                else getattr(row, "big_kind_name", None),
                "middle_kind_name": row.get("middle_kind_name")
                if isinstance(row, dict)
                else getattr(row, "middle_kind_name", None),
                "small_kind_name": row.get("small_kind_name")
                if isinstance(row, dict)
                else getattr(row, "small_kind_name", None),
                "mb_part_name": row.get("mb_part_name")
                if isinstance(row, dict)
                else getattr(row, "mb_part_name", None),
            }
            available.append(member_dict)

    return available


def add_nurses_to_group_service(
    nurse_ids: List[str], group_id: str, office_id: str, db: Session
) -> Dict[str, Any]:
    """
    선택된 멤버를 현재 그룹에 추가.
    - nurses 테이블에 이미 존재하는 경우: group_id만 변경
    - nurses 테이블에 없는 경우: MSSQL에서 멤버 정보 조회 후 신규 생성
    """
    if not nurse_ids:
        return {"added": 0, "updated": 0, "errors": []}

    added = 0
    updated = 0
    errors = []

    # MSSQL에서 오피스 전체 멤버 조회 (신규 생성 시 참조)
    all_members = (
        msdb_manager.fetch_all(
            Member.member_export_by_office(), params=(str(office_id),)
        )
        or []
    )
    member_map = {}
    for row in all_members:
        emp_seq = (
            row.get("EmpSeqNo")
            if isinstance(row, dict)
            else getattr(row, "EmpSeqNo", None)
        )
        if emp_seq:
            member_map[str(emp_seq)] = row

    for nid in nurse_ids:
        try:
            # nurses 테이블에서 해당 nurse_id 조회 (group_id 무관)
            existing_nurse = (
                db.query(NurseModel).filter(NurseModel.nurse_id == str(nid)).first()
            )

            if existing_nurse:
                # 이미 nurses 테이블에 존재 → group_id만 변경
                existing_nurse.group_id = group_id
                # sequence는 현재 그룹 + role 그룹 활성 목록의 마지막으로 배치
                next_seq = get_next_sequence_for_active_status(group_id, existing_nurse.active, db, role=existing_nurse.role)
                existing_nurse.sequence = next_seq
                updated += 1
            else:
                # nurses 테이블에 없음 → MSSQL 멤버 정보로 신규 생성
                member_data = member_map.get(str(nid))
                if not member_data:
                    errors.append(
                        {
                            "nurse_id": nid,
                            "reason": "MSSQL 멤버 정보를 찾을 수 없습니다.",
                        }
                    )
                    continue

                _get = (
                    lambda key: member_data.get(key)
                    if isinstance(member_data, dict)
                    else getattr(member_data, key, None)
                )

                # 경력 변환
                career_val = _get("career")
                experience = None
                if career_val is not None and str(career_val).strip():
                    try:
                        experience = int(career_val)
                    except (ValueError, TypeError):
                        experience = None

                # 수간호사 여부 변환
                headnurse_val = _get("headnurse")
                is_head_nurse = False
                if headnurse_val is not None:
                    is_head_nurse = str(headnurse_val).strip().upper() in (
                        "Y",
                        "1",
                        "TRUE",
                    )

                # 입사일 변환
                join_val = _get("joindate")
                joining_date = None
                if join_val:
                    try:
                        joining_date = parse_date(str(join_val))
                    except (ValueError, TypeError):
                        joining_date = None

                nurse_role = str(_get('duty') or 'RN') if _get('duty') else 'RN'
                next_seq = get_next_sequence_for_active_status(group_id, 1, db, role=nurse_role)

                new_nurse = NurseModel(
                    nurse_id=str(nid),
                    group_id=group_id,
                    office_id=office_id,
                    account_id=str(_get("MemberID") or ""),
                    emp_num=str(_get("OfficeEmpNum") or "")
                    if _get("OfficeEmpNum")
                    else None,
                    name=str(_get("EmployeeName") or ""),
                    experience=experience,
                    role=nurse_role,
                    is_head_nurse=is_head_nurse,
                    joining_date=joining_date,
                    birth_date=str(_get("DateOfBirth") or "")
                    if _get("DateOfBirth")
                    else None,
                    phone_number=str(_get("PortableTel") or "")
                    if _get("PortableTel")
                    else None,
                    email=str(_get("Email") or "") if _get("Email") else None,
                    gender=str(_get("Gender") or "") if _get("Gender") else None,
                    sequence=next_seq,
                    active=1,
                )
                db.add(new_nurse)
                added += 1

        except Exception as e:
            logging.error(f"[add_nurses_to_group] nurse_id={nid} 처리 실패: {e}")
            errors.append({"nurse_id": nid, "reason": str(e)})
            continue

    db.commit()
    return {"added": added, "updated": updated, "errors": errors}


def update_nurse_profile_service(
    nurse_id: str,
    update_data: NurseProfileUpdate,
    current_user: UserSchema,
    db: Session,
    view_group_id: Optional[str] = None,
):
    """
    nurse_id 기반 단건 프로필 업데이트 서비스 (근무자 관리 사이드 프로필용)
    - 수간호사(HDN) 또는 관리자(ADM)만 수정 가능
    - 수간호사는 같은 그룹 내만 수정 가능
    - email 변경 시 bizwiz20db.Member에도 dual write
    - `assignment` payload 포함 시 NurseAssignment CRUD를 먼저 처리 (create/update/cancel)
    - view_group_id: 호출 view 의 group_id (target view 에서 inbound nurse 수정 시 필수).
      미지정 시 current_user.group_id 사용.
    """
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    is_admin = current_user.is_master_admin
    is_head = current_user.is_head_nurse

    if not (is_admin or is_head):
        raise HTTPException(status_code=403, detail="수간호사 또는 관리자만 수정할 수 있습니다.")

    nurse = db.query(NurseModel).filter(NurseModel.nurse_id == nurse_id).first()
    if not nurse:
        raise HTTPException(status_code=404, detail="간호사 정보를 찾을 수 없습니다.")

    fields = update_data.dict(exclude_unset=True)
    # assignment payload는 별도 브랜치로 분리 후 source/target 저장 단계에서 제외.
    # 단건(assignment) + 다건(assignments) 모두 지원 — 다건 먼저 적용 후 단건.
    assignment_payload = fields.pop("assignment", None)
    assignments_payload = fields.pop("assignments", None)
    _payloads_to_apply: List[Dict[str, Any]] = []
    if assignments_payload:
        _payloads_to_apply.extend([p for p in assignments_payload if p])
    if assignment_payload is not None:
        _payloads_to_apply.append(assignment_payload)
    # 처리 순서 정합: cancel → update → create.
    # 휴지통 삭제와 신규 추가가 같은 payload 에 함께 올 때, 먼저 cancel/update 가
    # 적용되어 active 행 상태를 정리한 뒤 create 의 chain/overlap 검증을 통과하도록.
    _op_order = {"cancel": 0, "update": 1, "create": 2}
    _payloads_to_apply.sort(key=lambda p: _op_order.get(p.get("operation"), 99))
    if _payloads_to_apply:
        for _p in _payloads_to_apply:
            _dispatch_assignment_payload(db, nurse_id, _p, current_user)
        # assignment 저장 직후 resolve_edit_target 재평가 위해 세션 refresh
        db.refresh(nurse)

    email_changed = "email" in fields
    new_email = fields.get("email")
    applied_source = False

    # preceptor_id 변경 탐지 (source 경로에서만 유효; assignment payload 반영 후 값 기준)
    _preceptor_field_present = "preceptor_id" in fields
    _preceptor_before = nurse.preceptor_id if _preceptor_field_present else None

    if not fields:
        return {
            "message": "배정 정보가 성공적으로 반영되었습니다.",
            "nurse_id": nurse_id,
        }

    # 호출 view 의 group_id 결정 (target view 에서 inbound nurse 수정 시 명시 필요).
    # 권한 검증 (사이드프로필 / source 모두 동일):
    #   - 본인 group 과 동일하면 OK
    #   - 다른 group 인 경우 ADM 또는 HN multi-group 의 managed groups 안에 있어야 함
    _caller_view_group = view_group_id or current_user.group_id
    if view_group_id and view_group_id != current_user.group_id and not is_admin:
        is_hn_multi = str(getattr(current_user, "hn_auth", "") or "").upper() == "HN"
        if not is_hn_multi:
            raise HTTPException(
                status_code=403,
                detail="다른 그룹 view 에서 nurse 수정 권한이 없습니다 (HN/admin 필요).",
            )
        from services.group_access import resolve_managed_group_ids
        _managed = {str(g) for g in resolve_managed_group_ids(db, current_user)}
        if str(view_group_id) not in _managed:
            raise HTTPException(
                status_code=403,
                detail="해당 view 그룹은 본인이 관리하는 그룹이 아닙니다.",
            )

    # HN multi-group 자동 보정: view_group_id 미전송 시 nurse 의 home/inbound 가
    # managed groups 안에 있으면 _caller_view_group 자동 매핑 (통합보기 / 프론트가
    # view_group_id 안 보내는 경우 대응).
    if (
        not view_group_id
        and not is_admin
        and str(getattr(current_user, "hn_auth", "") or "").upper() == "HN"
        and str(nurse.group_id) != str(current_user.group_id)
    ):
        from services.group_access import resolve_managed_group_ids
        _managed = {str(g) for g in resolve_managed_group_ids(db, current_user)}
        if str(nurse.group_id) in _managed:
            _caller_view_group = nurse.group_id  # source view 자동
        else:
            _inbound_match = (
                db.query(NurseAssignment)
                .filter(
                    NurseAssignment.nurse_id == nurse_id,
                    NurseAssignment.target_group_id.in_(list(_managed)),
                    NurseAssignment.status == "active",
                    NurseAssignment.reason.in_(_INBOUND_REASONS),
                )
                .order_by(NurseAssignment.start_date.desc())
                .first()
            )
            if _inbound_match is not None:
                _caller_view_group = _inbound_match.target_group_id  # target view 자동
    # ADM는 기존 경로 (권한 체크만 통과시키면 nurses.*에 직접 저장)
    if is_admin:
        _validate_team_grade_change_or_raise(
            db, nurse, fields, scope="source", group_id=nurse.group_id, assign_row=None,
        )
        _apply_source_nurse_update(nurse, fields)
        db.commit()
        db.refresh(nurse)
        applied_source = True
    else:
        mode, assign_row = resolve_edit_target(db, _caller_view_group, nurse_id)
        if mode == "none":
            raise HTTPException(
                status_code=403, detail="해당 간호사를 수정할 권한이 없습니다."
            )
        if mode == "target" and assign_row is not None:
            _validate_team_grade_change_or_raise(
                db, nurse, fields, scope="target",
                group_id=assign_row.target_group_id,
                assign_row=assign_row,
            )
            _apply_target_update(assign_row, fields)
            db.commit()
            db.refresh(assign_row)
        else:
            _validate_team_grade_change_or_raise(
                db, nurse, fields, scope="source", group_id=nurse.group_id, assign_row=None,
            )
            _apply_source_nurse_update(nurse, fields)
            # source 변경 시 active inbound assignment 의 target_* 도 자동 cascade.
            # 프론트가 view_group_id 안 보내도 source view 호출 한 번으로 inbound 모두 동기화.
            _cascade_inbounds = (
                db.query(NurseAssignment)
                .filter(
                    NurseAssignment.nurse_id == nurse_id,
                    NurseAssignment.status == "active",
                    NurseAssignment.reason.in_(_INBOUND_REASONS),
                )
                .all()
            )
            for _ia in _cascade_inbounds:
                _apply_target_update(_ia, fields)
            db.commit()
            db.refresh(nurse)
            applied_source = True

    # source 경로에서 preceptor_id 필드를 받으면 nurse_assignment(reason='프리셉티') 자동 동기화.
    # 값 변경이 없는 경우(둘 다 NULL 등)에도 active row 비대칭이 있으면 reconcile 한다.
    if applied_source and _preceptor_field_present:
        try:
            _sync_preceptee_assignment(
                db, nurse,
                previous_preceptor_id=_preceptor_before,
                new_preceptor_id=nurse.preceptor_id,
                current_user=current_user,
            )
            db.refresh(nurse)
        except HTTPException:
            raise
        except Exception as e:
            logging.warning(
                "[preceptee_sync] 전체 실패 nurse=%s: %s",
                nurse_id, e,
            )

    # email 변경 시 MSSQL dual write는 source/admin 경로에서만 수행
    # (target 모드는 nurses.email을 건드리지 않으므로 Member.Email과의 정합성을 깨뜨리지 않기 위해 skip)
    if applied_source and email_changed and new_email is not None:
        try:
            msdb_manager.execute(
                "UPDATE bizwiz20db.Member SET Email = %s WHERE EmpSeqNo = %s",
                (new_email, nurse_id),
            )
        except Exception as e:
            logging.warning(f"[update_nurse_profile] MSSQL email 동기화 실패 nurse_id={nurse_id}: {e}")

    return {"message": "간호사 정보가 성공적으로 수정되었습니다.", "nurse_id": nurse_id}


_DISPATCH_REASON_ALIASES: frozenset[str] = frozenset({"파견", "병동이동", "부서이동"})


def _sanitize_resignation_fields(fields: Dict[str, Any]) -> None:
    """파견/병동이동 reason이 들어오면 resignation_* 3종을 null로 강제.

    파견·병동이동은 진짜 퇴사 사유가 아니라 nurse_assignment로만 추적해야 한다.
    프론트 구버전·엑셀 업로드·벌크 업데이트 등 다른 경로로 endDate가 새어들어
    nurses.resignation_date 로 저장되면 _active_range_in_month가 active 구간을 잘라
    해당 월 근무표 전체가 블랭크가 되는 회귀를 유발한다(443 이슈).
    """
    reason = fields.get("resignation_reason")
    if isinstance(reason, str) and reason.strip() in _DISPATCH_REASON_ALIASES:
        fields["resignation_reason"] = None
        fields["resignation_date"] = None
        fields["resignation_reason_memo"] = None


def _apply_source_nurse_update(nurse: NurseModel, fields: Dict[str, Any]) -> None:
    """Source 모드 nurses.* 직접 저장 (update_nurse_profile_service 전용 분리)"""
    _sanitize_resignation_fields(fields)
    for key, value in fields.items():
        if hasattr(nurse, key):
            setattr(nurse, key, value)


def _validate_team_grade_change_or_raise(
    db: Session,
    nurse: NurseModel,
    fields: Dict[str, Any],
    *,
    scope: str,  # 'source' | 'target'
    group_id: str,
    assign_row: Optional[NurseAssignment] = None,
) -> None:
    """fields 에 team_id/grade 변경 포함 시 인원 정합성 검증. 위반 시 422 raise.

    scope='source': nurses.* 직접 수정 (변경 전 값 = nurse.team_id / nurse.grade)
    scope='target': nurse_assignment.target_* 수정 (변경 전 값 = assign_row.target_team_id / .target_grade)
    """
    has_team = "team_id" in fields
    has_grade = "grade" in fields
    if not has_team and not has_grade:
        return
    if scope == "source":
        old_team = nurse.team_id
        old_grade = nurse.grade
    else:
        if assign_row is None:
            return
        old_team = assign_row.target_team_id
        old_grade = assign_row.target_grade
    new_team = fields["team_id"] if has_team else old_team
    new_grade = fields["grade"] if has_grade else old_grade

    from services.precheck.nurse_change_validators import validate_nurse_change

    result = validate_nurse_change(
        db,
        group_id=group_id,
        swap_nurse_id=str(nurse.nurse_id),
        old_team_id=old_team,
        new_team_id=new_team,
        old_grade=old_grade,
        new_grade=new_grade,
        scope=scope,
    )
    if not result.get("saveable", True):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "TEAM_GRADE_VALIDATION_FAILED",
                "message": "팀/Grade 변경이 인원 정합성을 충족하지 못합니다.",
                "issues": result.get("issues", []),
            },
        )


_ASSIGNMENT_CREATE_FIELDS: Tuple[str, ...] = (
    "source_group_id", "target_group_id", "office_id",
    "start_date", "expected_end_date", "end_date", "reason",
    "note",
    "target_weekly_off_type", "target_weekly_off_enabled", "target_weekly_off_weekday",
    "target_shift_types", "target_team_id", "target_grade",
    "target_fixed_shift", "target_wanted_max_requests",
)

_ASSIGNMENT_UPDATE_FIELDS: Tuple[str, ...] = (
    "start_date", "expected_end_date", "end_date", "status",
    "reason", "target_group_id", "note",
    "target_weekly_off_type", "target_weekly_off_enabled", "target_weekly_off_weekday",
    "target_shift_types", "target_team_id", "target_grade",
    "target_fixed_shift", "target_wanted_max_requests",
)


def _dispatch_assignment_payload(
    db: Session,
    nurse_id: str,
    payload: Dict[str, Any],
    current_user: UserSchema,
) -> None:
    """NurseProfileUpdate.assignment payload를 operation별로 assignment_service에 위임.

    편의를 위해 create 시 source_group_id / office_id 가 비어있으면
    nurses.group_id / nurses.office_id 값을 자동으로 채운다.
    (프론트가 nurse_id 만으로 assignment 생성 요청을 할 수 있도록 함)
    """
    from schemas.roster_schema import (
        NurseAssignmentCreate as _CreateReq,
        NurseAssignmentUpdate as _UpdateReq,
    )
    from services.assignment_service import (
        create_assignment as _create_assignment,
        update_assignment as _update_assignment,
        cancel_assignment as _cancel_assignment,
    )

    op = (payload or {}).get("operation")
    if op == "create":
        data = {k: payload.get(k) for k in _ASSIGNMENT_CREATE_FIELDS}
        data["nurse_id"] = nurse_id
        # 프론트 호환: end_date 키로 들어온 값을 expected_end_date 로 fold
        if data.get("expected_end_date") is None and data.get("end_date") is not None:
            data["expected_end_date"] = data["end_date"]
        data.pop("end_date", None)
        # source_group_id / office_id 자동 보정 (프론트 전송 필수항목 최소화)
        _src_gid = data.get("source_group_id")
        _office = data.get("office_id")
        if not _src_gid or not _office:
            _n = db.query(NurseModel).filter(NurseModel.nurse_id == nurse_id).first()
            if _n is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"assignment.create: 간호사({nurse_id}) 조회 실패",
                )
            if not _src_gid:
                data["source_group_id"] = _n.group_id
            if not _office:
                data["office_id"] = _n.office_id
        try:
            req = _CreateReq(**{k: v for k, v in data.items() if v is not None})
        except Exception as e:
            raise HTTPException(status_code=422, detail=f"assignment.create payload 오류: {e}")
        _create_assignment(req, db, current_user=current_user)
        return
    if op == "update":
        aid = payload.get("assignment_id")
        if aid is None:
            raise HTTPException(status_code=422, detail="assignment.update: assignment_id 필수")
        data = {k: payload.get(k) for k in _ASSIGNMENT_UPDATE_FIELDS if payload.get(k) is not None}
        try:
            req = _UpdateReq(**data)
        except Exception as e:
            raise HTTPException(status_code=422, detail=f"assignment.update payload 오류: {e}")
        _update_assignment(aid, req, db, current_user=current_user)
        return
    if op == "cancel":
        aid = payload.get("assignment_id")
        if aid is None:
            raise HTTPException(status_code=422, detail="assignment.cancel: assignment_id 필수")
        _cancel_assignment(aid, db, current_user=current_user)
        return
    raise HTTPException(
        status_code=422,
        detail=f"assignment.operation 값이 올바르지 않습니다: {op!r}",
    )


def _sync_preceptee_assignment(
    db: Session,
    nurse: NurseModel,
    previous_preceptor_id: Optional[str],
    new_preceptor_id: Optional[str],
    current_user: UserSchema,
) -> None:
    """nurses.preceptor_id 변경에 맞춰 nurse_assignment(reason='프리셉티')를 자동 동기화.

    - 해제 (new=None): nurses.preceptor_id가 NULL이면 active 프리셉티 row 전부 cancel.
      이전 값이 None이었더라도 (state 비대칭 reconcile) active row가 떠있으면 정리한다.
    - 신규 (previous=None, new=값): active row가 없으면 새로 생성.
    - 변경 (valueA → valueB): assignment row에 preceptor_id 저장 필드가 없으므로 no-op.
    """
    from schemas.roster_schema import NurseAssignmentCreate as _CreateReq
    from services.assignment_service import (
        create_assignment as _create_assignment,
        cancel_assignment as _cancel_assignment,
    )

    existings = (
        db.query(NurseAssignment)
        .filter(
            NurseAssignment.nurse_id == nurse.nurse_id,
            NurseAssignment.reason == "프리셉티",
            NurseAssignment.status == "active",
        )
        .order_by(NurseAssignment.start_date.desc())
        .all()
    )

    # 해제: nurses.preceptor_id가 NULL인 모든 케이스에서 active row 정리.
    # (값→None) 정상 해제 + (None→None) state reconcile 둘 다 처리.
    if new_preceptor_id is None:
        for existing in existings:
            try:
                _cancel_assignment(existing.id, db, current_user=current_user)
            except HTTPException:
                raise
            except Exception as e:
                logging.warning(
                    "[preceptee_sync] cancel 실패 nurse=%s id=%s: %s",
                    nurse.nurse_id, existing.id, e,
                )
        return

    if previous_preceptor_id == new_preceptor_id:
        return

    # (None → 값): 신규 생성 (이미 active면 no-op)
    if previous_preceptor_id is None:
        if existings:
            return
        try:
            req = _CreateReq(
                nurse_id=nurse.nurse_id,
                source_group_id=nurse.group_id,
                target_group_id=None,
                office_id=nurse.office_id,
                start_date=date.today(),
                expected_end_date=None,
                reason="프리셉티",
            )
            _create_assignment(req, db, current_user=current_user)
        except HTTPException:
            raise
        except Exception as e:
            logging.warning(
                "[preceptee_sync] create 실패 nurse=%s: %s",
                nurse.nurse_id, e,
            )
        return
    # (valueA → valueB): 현 스키마상 변경 기록 없음 — assignment row는 유지


def delete_nurse_service(nurse_id: str, current_user: UserSchema, db: Session):
    """
    특정 간호사 삭제 서비스 함수
    - HDN 또는 ADM만 허용
    - 삭제 전 deleted_nurse_history 테이블에 이력 저장
    - 삭제 후 재정렬(선택: 필요 시 _reindex_contiguously 호출)
    """
    if not current_user:
        raise Exception("Not authenticated")

    # 권한 체크
    if not (current_user.is_head_nurse or current_user.is_master_admin):
        raise Exception("Permission denied")

    # 대상 간호사 조회
    db_nurse = (
        db.query(NurseModel)
        .filter(
            NurseModel.nurse_id == nurse_id,
            NurseModel.group_id == current_user.group_id
            if not current_user.is_master_admin
            else True,
        )
        .first()
    )

    if not db_nurse:
        raise Exception(f"Nurse with nurse_id {nurse_id} not found")

    try:
        # 삭제 수행자 정보 조회
        deleter = (
            db.query(NurseModel)
            .filter(NurseModel.nurse_id == current_user.nurse_id)
            .first()
        )

        # 삭제 이력 저장 (deleted_nurse_history)
        history = DeletedNurseHistory(
            target_nurse_id=db_nurse.nurse_id,
            office_id=db_nurse.office_id,
            group_id=db_nurse.group_id,
            emp_num=db_nurse.emp_num,
            account_id=db_nurse.account_id,
            name=db_nurse.name,
            role=db_nurse.role,
            experience=db_nurse.experience,
            is_head_nurse=db_nurse.is_head_nurse,
            joining_date=db_nurse.joining_date,
            birth_date=db_nurse.birth_date,
            phone_number=db_nurse.phone_number,
            gender=db_nurse.gender,
            deleted_by_nurse_id=current_user.nurse_id,
            deleted_by_account_id=current_user.account_id,
            deleted_by_name=deleter.name if deleter else None,
            deleted_by_role=current_user.EmpAuthGbn,
        )
        db.add(history)

        # 삭제
        group_id = db_nurse.group_id
        deleted_role_group = get_role_group(db_nurse.role)
        db.delete(db_nurse)
        db.commit()

        # 삭제 후 해당 role 그룹만 재정렬
        for active_status in (1, 0):
            nurses_in_group = _get_nurses_by_active(group_id, active_status, db, deleted_role_group)
            _reindex_contiguously(nurses_in_group)
        db.commit()

        return {"message": f"Nurse {nurse_id} deleted successfully"}
    except Exception as e:
        db.rollback()
        raise Exception(f"Deletion failed: {str(e)}")


def flush_resigned_nurses(db: Session) -> int:
    """퇴사일+1일 경과 간호사 자동 삭제 (스케줄러용).

    조건: resignation_date IS NOT NULL AND resignation_date(date) < today
    처리: DeletedNurseHistory 이력 저장 + nurses 레코드 hard delete
    snapshot(nurses_json)에 이력이 보존되므로 기존 근무표 조회에는 영향 없음.
    Returns: 처리된 건수
    """
    today = date.today()
    candidates = (
        db.query(NurseModel)
        .filter(NurseModel.resignation_date.isnot(None))
        .all()
    )
    rows = []
    for n in candidates:
        _d = n.resignation_date
        _d = _d.date() if hasattr(_d, "date") else _d
        if _d and _d < today:
            rows.append(n)
    if not rows:
        return 0

    for n in rows:
        history = DeletedNurseHistory(
            target_nurse_id=n.nurse_id,
            office_id=n.office_id,
            group_id=n.group_id,
            emp_num=n.emp_num,
            account_id=n.account_id,
            name=n.name,
            role=n.role,
            experience=n.experience,
            is_head_nurse=n.is_head_nurse,
            joining_date=n.joining_date,
            birth_date=n.birth_date,
            phone_number=n.phone_number,
            gender=n.gender,
            deleted_by_nurse_id="SYSTEM",
            deleted_by_account_id="SYSTEM",
            deleted_by_name="[Scheduler]",
            deleted_by_role="SYS",
        )
        db.add(history)
        db.delete(n)

    db.commit()
    logging.info("[Scheduler] 퇴사자 자동 삭제: %d건", len(rows))
    return len(rows)


def get_profile_image_info_service(current_user, db: Session) -> Dict[str, Any]:
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    nurse = (
        db.query(NurseModel)
        .filter(NurseModel.nurse_id == current_user.nurse_id)
        .first()
    )
    if not nurse:
        raise HTTPException(status_code=404, detail="간호사 정보를 찾을 수 없습니다.")

    image_key = nurse.profile_image_key
    return {
        "nurse_id": nurse.nurse_id,
        "profile_image_key": image_key,
        "profile_image_url": _build_profile_image_url(image_key),
        "profile_image_updated_at": nurse.profile_image_updated_at.isoformat()
        if nurse.profile_image_updated_at
        else None,
    }


def upload_profile_image_service(
    current_user, db: Session, image_file
) -> Dict[str, Any]:
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    nurse = (
        db.query(NurseModel)
        .filter(NurseModel.nurse_id == current_user.nurse_id)
        .first()
    )
    if not nurse:
        raise HTTPException(status_code=404, detail="간호사 정보를 찾을 수 없습니다.")

    allowed_types = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/webp": ".webp",
    }
    content_type = str(getattr(image_file, "content_type", "") or "").lower()
    if content_type not in allowed_types:
        raise HTTPException(
            status_code=400,
            detail="지원하지 않는 이미지 형식입니다. (png, jpg, jpeg, webp)",
        )

    image_bytes = image_file.file.read()
    if not image_bytes:
        raise HTTPException(status_code=400, detail="image file is empty")
    if len(image_bytes) > 3 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="image file size must be <= 3MB")

    bucket_name = (
        os.getenv("AWS_SHARE_S3_BUCKET")
        or os.getenv("SHARE_S3_BUCKET")
        or os.getenv("S3_SHARE_BUCKET")
    )
    if not bucket_name:
        raise HTTPException(
            status_code=500, detail="share S3 bucket env is not configured"
        )
    region = os.getenv("AWS_REGION", "ap-northeast-2")

    ext = allowed_types[content_type]
    office_id = str(
        nurse.office_id or getattr(current_user, "office_id", None) or "unknown"
    )
    group_id = str(
        nurse.group_id or getattr(current_user, "group_id", None) or "unknown"
    )
    nurse_id = str(nurse.nurse_id)
    object_key = f"og-images/{office_id}/{group_id}/{nurse_id}/my-profile/{secrets.token_hex(16)}{ext}"

    s3_client = _build_s3_client(region)
    try:
        s3_client.put_object(
            Bucket=bucket_name,
            Key=object_key,
            Body=image_bytes,
            ContentType=content_type,
            CacheControl="max-age=31536000",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"S3 업로드 실패: {str(e)}")

    old_key = nurse.profile_image_key
    nurse.profile_image_key = object_key
    nurse.profile_image_updated_at = datetime.now()
    db.commit()
    db.refresh(nurse)

    if old_key and old_key != object_key:
        try:
            s3_client.delete_object(Bucket=bucket_name, Key=old_key)
        except Exception:
            pass

    return {
        "nurse_id": nurse.nurse_id,
        "profile_image_key": nurse.profile_image_key,
        "profile_image_url": _build_profile_image_url(nurse.profile_image_key),
        "profile_image_updated_at": nurse.profile_image_updated_at.isoformat()
        if nurse.profile_image_updated_at
        else None,
    }


def delete_profile_image_service(current_user, db: Session) -> Dict[str, Any]:
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    nurse = (
        db.query(NurseModel)
        .filter(NurseModel.nurse_id == current_user.nurse_id)
        .first()
    )
    if not nurse:
        raise HTTPException(status_code=404, detail="간호사 정보를 찾을 수 없습니다.")

    old_key = nurse.profile_image_key
    bucket_name = (
        os.getenv("AWS_SHARE_S3_BUCKET")
        or os.getenv("SHARE_S3_BUCKET")
        or os.getenv("S3_SHARE_BUCKET")
    )
    region = os.getenv("AWS_REGION", "ap-northeast-2")
    if old_key and bucket_name:
        try:
            s3_client = _build_s3_client(region)
            s3_client.delete_object(Bucket=bucket_name, Key=old_key)
        except Exception:
            pass

    nurse.profile_image_key = None
    nurse.profile_image_updated_at = datetime.now()
    db.commit()
    db.refresh(nurse)

    return {
        "nurse_id": nurse.nurse_id,
        "profile_image_key": None,
        "profile_image_url": None,
        "profile_image_updated_at": nurse.profile_image_updated_at.isoformat()
        if nurse.profile_image_updated_at
        else None,
    }


# ── source/target 수정 분기 헬퍼 ──────────────────────────────────────
# 관리자 수간호사가 inbound(파견/병동이동) 간호사 프로필을 수정할 때
# nurse 원본 대신 nurse_assignment.target_* 필드에 저장해야 한다.

_INBOUND_REASONS = ("파견", "병동이동")
# GET 응답 표시용(inbound_list + current_assignment): 5종 전부 노출.
# (transfer 의미의 _INBOUND_REASONS 와 분리 — 휴직/퇴사/프리셉티는 target_group_id 없음)
_STATUS_DISPLAY_REASONS: Tuple[str, ...] = _INBOUND_REASONS + (
    "휴직",
    "퇴사",
    "프리셉티",
)
# current_assignment 대표 1건 우선순위: 숫자 작을수록 우선.
_ASSIGNMENT_PRIORITY: Dict[str, int] = {
    "휴직": 0,
    "퇴사": 0,
    "프리셉티": 1,
    "파견": 2,
    "병동이동": 2,
}


def _fetch_inbound_assignment(
    db: Session, caller_group_id: str, nurse_id: str
) -> Optional[NurseAssignment]:
    """caller_group_id가 target인 간호사의 최신 active inbound assignment 반환."""
    return (
        db.query(NurseAssignment)
        .filter(
            NurseAssignment.nurse_id == nurse_id,
            NurseAssignment.target_group_id == caller_group_id,
            NurseAssignment.status == "active",
            NurseAssignment.reason.in_(_INBOUND_REASONS),
        )
        .order_by(NurseAssignment.start_date.desc())
        .first()
    )


def resolve_edit_target(
    db: Session, caller_group_id: str, nurse_id: str
) -> tuple[str, Optional[NurseAssignment]]:
    """간호사 수정 시 source/target 분기 판단.

    Returns:
        ('source', None): 호출자가 source 병동 소속 → nurses.* 에 저장
        ('target', row):  호출자가 target 병동 소속 → assignment.target_* 에 저장
        ('none', None):   양쪽 모두 아님 → 권한 없음
    """
    nurse = db.query(NurseModel).filter(NurseModel.nurse_id == nurse_id).first()
    if nurse and nurse.group_id == caller_group_id:
        return ("source", None)
    inbound = _fetch_inbound_assignment(db, caller_group_id, nurse_id)
    if inbound is not None:
        return ("target", inbound)
    return ("none", None)


def resolve_edit_targets_batch(
    db: Session, caller_group_id: str, nurse_ids: List[str]
) -> Dict[str, tuple]:
    """복수 간호사 대상 분기 판단 (N+1 회피용 배치 쿼리).

    Returns dict[nurse_id] = (mode, assignment|None)
    """
    if not nurse_ids:
        return {}

    source_ids: set[str] = {
        row.nurse_id
        for row in db.query(NurseModel.nurse_id)
        .filter(
            NurseModel.nurse_id.in_(nurse_ids),
            NurseModel.group_id == caller_group_id,
        )
        .all()
    }

    inbound_rows = (
        db.query(NurseAssignment)
        .filter(
            NurseAssignment.nurse_id.in_(nurse_ids),
            NurseAssignment.target_group_id == caller_group_id,
            NurseAssignment.status == "active",
            NurseAssignment.reason.in_(_INBOUND_REASONS),
        )
        .order_by(NurseAssignment.start_date.desc())
        .all()
    )
    inbound_map: Dict[str, NurseAssignment] = {}
    for row in inbound_rows:
        # 가장 최근 start_date만 유지 (order_by desc → 첫 발견이 최신)
        inbound_map.setdefault(row.nurse_id, row)

    result: Dict[str, tuple] = {}
    for nid in nurse_ids:
        if nid in source_ids:
            result[nid] = ("source", None)
        elif nid in inbound_map:
            result[nid] = ("target", inbound_map[nid])
        else:
            result[nid] = ("none", None)
    return result


def _apply_target_update(
    row: NurseAssignment, update_data: Dict[str, Any]
) -> None:
    """source 필드명 기준 update_data를 assignment.target_* 필드로 저장.

    매핑 테이블에 없는 키는 무시(나이/생일 등 target로 전달 불가 항목).
    """
    for src_key, value in update_data.items():
        target_key = _SOURCE_TO_TARGET_FIELD_MAP.get(src_key)
        if target_key is not None and hasattr(row, target_key):
            setattr(row, target_key, value)


def _load_inbound_map(
    db: Session, caller_group_id: str, nurse_ids: List[str]
) -> Dict[str, NurseAssignment]:
    """caller_group_id가 target인 간호사들의 최신 active inbound assignment 일괄 로드."""
    if not nurse_ids:
        return {}
    rows = (
        db.query(NurseAssignment)
        .filter(
            NurseAssignment.nurse_id.in_(nurse_ids),
            NurseAssignment.target_group_id == caller_group_id,
            NurseAssignment.status == "active",
            NurseAssignment.reason.in_(_INBOUND_REASONS),
        )
        .order_by(NurseAssignment.start_date.desc())
        .all()
    )
    inbound_map: Dict[str, NurseAssignment] = {}
    for row in rows:
        # 가장 최근 start_date만 유지
        inbound_map.setdefault(row.nurse_id, row)
    return inbound_map


def _build_inbound_blocks(
    db: Session,
    nurse_ids: List[str],
) -> Dict[str, Dict[str, Any]]:
    """간호사별 활성 파견/병동이동/휴직/퇴사/프리셉티 블록 구성.

    Returns: dict[nurse_id] = {
        "inbound_list": [InboundEntry dict, ...],
        "current_assignment": {CurrentAssignment dict} or None,
    }
    current_assignment: 휴직/퇴사 > 프리셉티 > 파견/병동이동 우선,
    동률 시 start_date DESC (최신).
    """
    if not nurse_ids:
        return {}
    rows = (
        db.query(NurseAssignment)
        .filter(
            NurseAssignment.nurse_id.in_(nurse_ids),
            NurseAssignment.status == "active",
            NurseAssignment.reason.in_(_STATUS_DISPLAY_REASONS),
        )
        .order_by(NurseAssignment.start_date.asc())
        .all()
    )
    if not rows:
        return {}
    gid_set: set[str] = set()
    for r in rows:
        if r.target_group_id:
            gid_set.add(r.target_group_id)
        if r.source_group_id:
            gid_set.add(r.source_group_id)
    name_map: Dict[str, str] = {}
    if gid_set:
        for gid, gname in (
            db.query(Group.group_id, Group.group_name)
            .filter(Group.group_id.in_(gid_set))
            .all()
        ):
            name_map[gid] = gname or ""
    blocks: Dict[str, Dict[str, Any]] = {}
    # 대표 1건 선정용: nurse_id → (priority, start_date, row)
    _best: Dict[str, Tuple[int, Any, Any]] = {}
    for r in rows:
        _end = r.end_date or r.expected_end_date
        entry = {
            "id": r.id,
            "status": r.status,
            "reason": r.reason,
            "startDate": r.start_date.isoformat() if r.start_date else None,
            "endDate": _end.isoformat() if _end else None,
            "expectedEndDate": (
                r.expected_end_date.isoformat() if r.expected_end_date else None
            ),
            "source_group_id": r.source_group_id,
            "source_group_name": name_map.get(r.source_group_id, ""),
            "target_group_id": r.target_group_id,
            "target_group_name": name_map.get(r.target_group_id, ""),
            "office_id": r.office_id,
            "note": getattr(r, "note", None),
            "target_weekly_off_type": r.target_weekly_off_type,
            "target_weekly_off_enabled": r.target_weekly_off_enabled,
            "target_weekly_off_weekday": r.target_weekly_off_weekday,
            "target_shift_types": r.target_shift_types,
            "target_team_id": r.target_team_id,
            "target_grade": r.target_grade,
            "target_fixed_shift": r.target_fixed_shift,
            "target_wanted_max_requests": r.target_wanted_max_requests,
        }
        block = blocks.setdefault(
            r.nurse_id,
            {"inbound_list": [], "current_assignment": None},
        )
        block["inbound_list"].append(entry)
        # 우선순위 산정: priority 작을수록, start_date 클수록 우선
        prio = _ASSIGNMENT_PRIORITY.get(r.reason, 99)
        cand = (prio, r.start_date, r)
        prev = _best.get(r.nurse_id)
        if prev is None:
            _best[r.nurse_id] = cand
        else:
            # (prio asc, start_date desc) 비교 — 작은 prio가 이기고, 동률이면 최신 start_date
            if (prio, -_ord_date(r.start_date)) < (prev[0], -_ord_date(prev[1])):
                _best[r.nurse_id] = cand
    # current_assignment dict 주입
    for nid, (_, _, row) in _best.items():
        _end = row.end_date or row.expected_end_date
        blocks[nid]["current_assignment"] = {
            "id": row.id,
            "status": row.status,
            "reason": row.reason,
            "startDate": row.start_date.isoformat() if row.start_date else None,
            "endDate": _end.isoformat() if _end else None,
            "expectedEndDate": (
                row.expected_end_date.isoformat() if row.expected_end_date else None
            ),
            "source_group_id": row.source_group_id,
            "source_group_name": name_map.get(row.source_group_id, "") or None,
            "target_group_id": row.target_group_id,
            "target_group_name": name_map.get(row.target_group_id, "") or None,
            "note": getattr(row, "note", None),
        }
    return blocks


def _ord_date(d: Any) -> int:
    """date → ordinal(int) 변환 (None이면 0)."""
    try:
        return d.toordinal() if d is not None else 0
    except AttributeError:
        return 0


def _overlay_inbound_fields(
    nurse_dict: Dict[str, Any], row: NurseAssignment
) -> None:
    """응답 dict에 target_* 값을 overlay (원본 ORM/nurses 테이블 불변).

    정책: Target 병동 시점에서는 매핑된 모든 컬럼을 assignment.target_* 값으로 교체.
    target_*가 NULL이어도 NULL 그대로 노출(= nurses.*로 fallback 하지 않음).
    is_night_nurse만 프론트 호환을 위해 None → [] 처리.
    """
    nurse_dict["is_inbound"] = True
    for src_key, tgt_key in _SOURCE_TO_TARGET_FIELD_MAP.items():
        nurse_dict[src_key] = getattr(row, tgt_key, None)
    if nurse_dict.get("is_night_nurse") is None:
        nurse_dict["is_night_nurse"] = []
