"""
간호사 정보 관리 관련 서비스 로직 모듈
- DB 쿼리, 데이터 가공 등 라우터에서 분리
- 모든 함수는 한글 docstring, 한글 print/logging, PEP8 스타일 적용
"""
from sqlalchemy.orm import Session
from db.models import Nurse as NurseModel, Group, DeletedNurseHistory, RosterConfig as RosterConfigModel
from schemas.roster_schema import NurseProfile
from schemas.auth_schema import User as UserSchema
from typing import List, Optional, Dict, Any
from pprint import pprint
from datetime import date
from dateutil.parser import parse as parse_date
from db.client2 import msdb_manager
from datalayer.member import Member
import logging

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


def get_personnel_basic_info_service(current_user, db: Session):
    """
    간호사 기본 정보 조회 서비스 함수
    """

    try:
        if not current_user:
            raise Exception("Not authenticated")

        nurse = (
            db.query(NurseModel)
            .filter(NurseModel.group_id == current_user.group_id, NurseModel.nurse_id == current_user.nurse_id)
            .first()
        )
        return nurse
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"간호사 기본 정보 조회 실패: {str(e)}")

def get_nurses_in_group_service(
    current_user,
    db: Session,
    nurse_id: Optional[str] = None
):
    """
    그룹 내 간호사 목록 조회 서비스 함수
    특정 nurse_id가 제공되면 해당 간호사만 반환, 그렇지 않으면 그룹 내 모든 간호사 반환
    birth_date (VARCHAR)를 파싱하여 만 나이를 age로 추가
    """
    if not current_user:
        raise Exception("Not authenticated")

    query = db.query(NurseModel)

    # 그룹 ID 필터링
    if current_user.group_id:
        query = query.filter(NurseModel.group_id == current_user.group_id)

    # 특정 nurse_id 필터링
    if nurse_id:
        query = query.filter(NurseModel.nurse_id == nurse_id)

    # 정렬: active DESC, role DESC(RN→AN), sequence ASC, experience DESC, nurse_id ASC
    nurses = (
        query.order_by(
            NurseModel.active.desc(),
            NurseModel.role.desc(),
            NurseModel.sequence.asc(),
            NurseModel.experience.desc(),
            NurseModel.nurse_id.asc()
        ).all()
    )

    # nurse_id로 필터링했는데 결과가 없으면 예외
    if nurse_id and not nurses:
        raise Exception(f"Nurse with nurse_id {nurse_id} not found")

    # roster_config에서 표시 설정 플래그 조회
    display_flags = _get_display_flags(db, current_user.group_id)

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
            if (current_date.month, current_date.day) < (birth_date.month, birth_date.day):
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
            "work_shifts": nurse.work_shifts or [],  # JSON 컬럼이므로 None일 수 있음 → []로 변환
            # 원티드 설정 (간호사별 개별 설정)
            "enable_nurse_pair_preference": nurse.enable_nurse_pair_preference,
            "enable_aide": nurse.enable_aide,
            "wanted_max_requests": nurse.wanted_max_requests,
            # 근무표 설정 메타 플래그
            **display_flags,
        }
        result.append(nurse_dict)

    return result

def get_nurses_filtered_service(
    current_user,
    db: Session,
    office_id: Optional[str] = None,
    group_id: Optional[str] = None,
    nurse_id: Optional[str] = None
):
    """ADM 전용 필터 조회: office_id, group_id, nurse_id 기준으로 간호사 목록 조회"""
    if not current_user:
        raise Exception("Not authenticated")
    if not getattr(current_user, 'is_master_admin', False):
        raise Exception("Permission denied")

    q = db.query(NurseModel)

    # 필터링: nurse_id
    if nurse_id is not None:
        q = q.filter(NurseModel.nurse_id == nurse_id)

    # 필터링: group_id
    if group_id is not None:
        q = q.filter(NurseModel.group_id == group_id, NurseModel.office_id == current_user.office_id)

    # 필터링: office_id
    elif current_user.office_id is not None:
        q = q.join(Group, Group.group_id == NurseModel.group_id).filter(Group.office_id == current_user.office_id)

    # 정렬: active DESC, role DESC(RN→AN), sequence ASC, experience DESC, nurse_id ASC
    nurses = q.order_by(
        NurseModel.active.desc(),
        NurseModel.role.desc(),
        NurseModel.sequence.asc(),
        NurseModel.experience.desc(),
        NurseModel.nurse_id.asc()
    ).all()
    # nurse_id로 필터링했는데 결과가 없으면 예외
    if nurse_id is not None and not nurses:
        raise Exception(f"Nurse with nurse_id {nurse_id} not found")

    # roster_config에서 표시 설정 플래그 조회 (ADM: group_id 파라미터 우선, 없으면 첫 번째 간호사의 group_id 사용)
    target_gid = group_id or (nurses[0].group_id if nurses else None)
    display_flags = _get_display_flags(db, target_gid) if target_gid else {"show_level": True, "show_preceptor": True}

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
            if (current_date.month, current_date.day) < (birth_date.month, birth_date.day):
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
            "work_shifts": nurse.work_shifts or [],  # JSON 컬럼이므로 None일 수 있음 → []로 변환
            # 원티드 설정 (간호사별 개별 설정)
            "enable_nurse_pair_preference": nurse.enable_nurse_pair_preference,
            "enable_aide": nurse.enable_aide,
            "wanted_max_requests": nurse.wanted_max_requests,
            # 근무표 설정 메타 플래그
            **display_flags,
        }
        result.append(nurse_dict)

    return result


def get_next_sequence_for_active_status(group_id: str, active_status: int, db: Session) -> int:
    """
    특정 active 상태(활성/비활성)에서 다음 sequence 번호 반환
    """
    max_sequence = db.query(NurseModel.sequence).filter(
        NurseModel.group_id == group_id,
        NurseModel.active == active_status
    ).order_by(NurseModel.sequence.desc()).first()

    return (max_sequence[0] + 1) if max_sequence and max_sequence[0] is not None else 1

def _get_nurses_by_active(group_id: str, active_status: int, db: Session) -> List[NurseModel]:
    return (
        db.query(NurseModel)
        .filter(NurseModel.group_id == group_id, NurseModel.active == active_status)
        .order_by(NurseModel.role.desc(), NurseModel.sequence.asc(), NurseModel.nurse_id.asc())
        .all()
    )

def _reindex_contiguously(nurses: List[NurseModel]) -> None:
    """sequence를 1부터 연속되게 재부여"""
    for idx, n in enumerate(nurses, start=1):
        if n.sequence != idx:
            n.sequence = idx

def move_nurse_with_active_service(nurse_id: str, new_sequence: int, target_active: Optional[int], current_user, db: Session):
    """
    간호사 이동/상태변경을 단일 트랜잭션으로 처리.
    - target_active가 None이면 같은 active 안에서 재배치
    - target_active가 0/1이면 해당 상태 리스트로 이동 후 삽입
    """
    if not current_user:
        raise Exception("Not authenticated")
    # 수간호사 또는 마스터관리자만 허용
    if not (getattr(current_user, 'is_head_nurse', False) or getattr(current_user, 'is_master_admin', False)):
        raise Exception("Permission denied")

    # ADM은 group_id가 없을 수 있으므로 nurse_id만으로 조회 후 대상 그룹을 결정
    if getattr(current_user, 'is_master_admin', False) and not getattr(current_user, 'group_id', None):
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
            .filter(NurseModel.group_id == current_user.group_id, NurseModel.nurse_id == nurse_id)
            .first()
        )
        target_group_id = getattr(current_user, 'group_id', None)
    if not nurse:
        raise Exception("간호사를 찾을 수 없습니다.")

    group_id = target_group_id
    old_active = nurse.active

    if target_active is None or target_active == old_active:
        # 같은 상태 내 재배치
        lst = _get_nurses_by_active(group_id, old_active, db)
        # 해당 간호사 제외
        lst = [n for n in lst if n.nurse_id != nurse.nurse_id]
        # 경계 보정 (1-based)
        insert_idx = max(0, min(new_sequence - 1, len(lst)))
        lst.insert(insert_idx, nurse)
        _reindex_contiguously(lst)
    else:
        # 상태 변경: 기존 리스트에서 제거하고 reindex
        old_list = _get_nurses_by_active(group_id, old_active, db)
        old_list = [n for n in old_list if n.nurse_id != nurse.nurse_id]
        _reindex_contiguously(old_list)
        # 새 리스트로 이동하여 삽입
        nurse.active = target_active
        new_list = _get_nurses_by_active(group_id, target_active, db)
        # autoflush로 인해 nurse가 이미 new_list에 포함될 수 있으므로 제거 후 삽입
        new_list = [n for n in new_list if n.nurse_id != nurse.nurse_id]
        insert_idx = max(0, min(new_sequence - 1, len(new_list)))
        new_list.insert(insert_idx, nurse)
        _reindex_contiguously(new_list)

    db.commit()
    return {"message": "순서/상태 변경 완료"}

def reorder_nurses_service(active_order: List[str], inactive_order: List[str], current_user, db: Session):
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

    # 활성 정렬 적용
    for idx, nid in enumerate(active_order, start=1):
        n = id_to_nurse.get(nid)
        if not n:
            continue
        n.active = 1
        n.sequence = idx

    # 비활성 정렬 적용
    for idx, nid in enumerate(inactive_order, start=1):
        n = id_to_nurse.get(nid)
        if not n:
            continue
        n.active = 0
        n.sequence = idx

    db.commit()
    return {"message": "일괄 재정렬 완료", "active_count": len(active_order), "inactive_count": len(inactive_order)}

def bulk_update_nurses_service(
    nurses_data: List[NurseProfile],
    current_user: UserSchema,
    db: Session,
    override_group_id: Optional[str] = None
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

    # 그룹 내 모든 간호사 미리 로드 (효율성)
    db_nurses_dict = {
        n.nurse_id: n for n in db.query(NurseModel)
        .filter(NurseModel.group_id == target_group_id)
        .all()
    }

    updated_count = 0
    ALL_SHIFTS = {"D","E","N"}

    for profile in nurses_data:
        db_nurse = db_nurses_dict.get(profile.nurse_id)

        # 기존 간호사 업데이트
        if db_nurse:
            if db_nurse.group_id != target_group_id:
                continue

            # 변경된 필드만 추출
            update_data = profile.dict(exclude_unset=True)

            # active 변경 시 sequence 자동 조정
            old_active = db_nurse.active
            new_active = update_data.get('active', old_active)
            if old_active != new_active and 'active' in update_data:
                update_data['sequence'] = get_next_sequence_for_active_status(
                    target_group_id, new_active, db
                )

            # === 프론트에서 보내준 값으로 일괄 업데이트 ===
            for key, value in update_data.items():
                if hasattr(db_nurse, key):
                    setattr(db_nurse, key, value)

            # === 후처리: is_night_nurse (3개 전체 선택 시 빈 배열) ===
            if 'is_night_nurse' in update_data:
                night_shifts = update_data['is_night_nurse']
                if isinstance(night_shifts, list) and set(night_shifts) == ALL_SHIFTS:
                    db_nurse.is_night_nurse = []
                elif night_shifts is None:
                    db_nurse.is_night_nurse = []

            # === 후처리: work_shifts (None → 빈 배열) ===
            if 'work_shifts' in update_data and update_data['work_shifts'] is None:
                db_nurse.work_shifts = []

            updated_count += 1

    # === 클라이언트에서 제외된 간호사 확인 ===
    client_nurse_ids = {profile.nurse_id for profile in nurses_data}
    for db_nurse_id, db_nurse in list(db_nurses_dict.items()):
        if db_nurse_id not in client_nurse_ids:
            print('제외된 간호사 for test:', db_nurse_id)

    # === active 상태별 sequence 재정렬 (갭/중복 방지) ===
    # autoflush=False 환경이므로 in-memory 변경사항을 DB에 반영 후 쿼리
    db.flush()
    for active_status in (1, 0):
        nurses_in_status = _get_nurses_by_active(target_group_id, active_status, db)
        _reindex_contiguously(nurses_in_status)

    db.commit()

    return {
        "message": "간호사 정보가 성공적으로 업데이트되었습니다.",
        "updated": updated_count
    }

def move_nurse_service(req, current_user, db: Session):
    """
    간호사 순서 이동 서비스 함수 (같은 active 상태 내에서만 이동)
    """
    if not current_user:
        raise Exception("Not authenticated")
    if not current_user.is_head_nurse:
        raise Exception("Permission denied")

    nurse_to_move = db.query(NurseModel).filter(
        NurseModel.nurse_id == req.nurse_id,
        NurseModel.group_id == current_user.group_id
    ).first()

    if not nurse_to_move:
        raise Exception("해당 간호사를 찾을 수 없습니다.")

    old_sequence = nurse_to_move.sequence
    new_sequence = req.new_sequence
    active_status = nurse_to_move.active

    print(f"[DEBUG] 순서 이동: {nurse_to_move.name} (active={active_status}) {old_sequence} → {new_sequence}")

    if old_sequence == new_sequence:
        return {"message": "변경사항이 없습니다."}

    # 같은 active 상태의 간호사들만 대상으로 sequence 재조정
    if old_sequence < new_sequence:
        affected_nurses = db.query(NurseModel).filter(
            NurseModel.group_id == current_user.group_id,
            NurseModel.active == active_status,
            NurseModel.sequence > old_sequence,
            NurseModel.sequence <= new_sequence
        ).all()

        for nurse in affected_nurses:
            nurse.sequence -= 1
            print(f"[DEBUG] {nurse.name} sequence: {nurse.sequence + 1} → {nurse.sequence}")
    else:
        affected_nurses = db.query(NurseModel).filter(
            NurseModel.group_id == current_user.group_id,
            NurseModel.active == active_status,
            NurseModel.sequence >= new_sequence,
            NurseModel.sequence < old_sequence
        ).all()

        for nurse in affected_nurses:
            nurse.sequence += 1
            print(f"[DEBUG] {nurse.name} sequence: {nurse.sequence - 1} → {nurse.sequence}")

    # 이동할 간호사의 sequence 업데이트
    nurse_to_move.sequence = new_sequence
    print(f"[DEBUG] {nurse_to_move.name} 최종 sequence: {new_sequence}")

    db.commit()
    return {"message": "간호사 순서 변경 완료"}


def get_available_members_service(
    office_id: str,
    group_id: str,
    db: Session
) -> List[Dict[str, Any]]:
    """
    동일 오피스의 전체 멤버 중 현재 그룹에 속하지 않은 멤버 목록 반환.
    - MSSQL Member 테이블에서 오피스 전체 멤버 조회
    - MySQL nurses 테이블에서 해당 group_id에 이미 등록된 nurse_id 조회
    - 이미 등록된 멤버를 제외한 나머지 반환
    """
    # 1. MSSQL에서 오피스 전체 멤버 조회 (export-members와 동일 쿼리)
    all_members = msdb_manager.fetch_all(
        Member.member_export_by_office(),
        params=(str(office_id),)
    ) or []

    # 2. 현재 group_id에 이미 등록된 간호사의 nurse_id 집합
    existing_nurse_ids = set(
        row[0] for row in db.query(NurseModel.nurse_id)
        .filter(NurseModel.group_id == group_id)
        .all()
    )

    # 3. 이미 등록된 멤버 제외
    available = []
    for row in all_members:
        emp_seq_no = row.get('EmpSeqNo') if isinstance(row, dict) else getattr(row, 'EmpSeqNo', None)
        if emp_seq_no and str(emp_seq_no) not in existing_nurse_ids:
            member_dict = {
                "nurse_id": str(emp_seq_no),
                "emp_num": row.get('OfficeEmpNum') if isinstance(row, dict) else getattr(row, 'OfficeEmpNum', None),
                "account_id": row.get('MemberID') if isinstance(row, dict) else getattr(row, 'MemberID', None),
                "name": row.get('EmployeeName') if isinstance(row, dict) else getattr(row, 'EmployeeName', None),
                "duty": row.get('duty') if isinstance(row, dict) else getattr(row, 'duty', None),
                "career": row.get('career') if isinstance(row, dict) else getattr(row, 'career', None),
                "is_head_nurse": row.get('headnurse') if isinstance(row, dict) else getattr(row, 'headnurse', None),
                "joining_date": row.get('joindate') if isinstance(row, dict) else getattr(row, 'joindate', None),
                "birth_date": row.get('DateOfBirth') if isinstance(row, dict) else getattr(row, 'DateOfBirth', None),
                "phone_number": row.get('PortableTel') if isinstance(row, dict) else getattr(row, 'PortableTel', None),
                "gender": row.get('Gender') if isinstance(row, dict) else getattr(row, 'Gender', None),
                "big_kind_name": row.get('big_kind_name') if isinstance(row, dict) else getattr(row, 'big_kind_name', None),
                "middle_kind_name": row.get('middle_kind_name') if isinstance(row, dict) else getattr(row, 'middle_kind_name', None),
                "small_kind_name": row.get('small_kind_name') if isinstance(row, dict) else getattr(row, 'small_kind_name', None),
                "mb_part_name": row.get('mb_part_name') if isinstance(row, dict) else getattr(row, 'mb_part_name', None),
            }
            available.append(member_dict)

    return available


def add_nurses_to_group_service(
    nurse_ids: List[str],
    group_id: str,
    office_id: str,
    db: Session
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
    all_members = msdb_manager.fetch_all(
        Member.member_export_by_office(),
        params=(str(office_id),)
    ) or []
    member_map = {}
    for row in all_members:
        emp_seq = row.get('EmpSeqNo') if isinstance(row, dict) else getattr(row, 'EmpSeqNo', None)
        if emp_seq:
            member_map[str(emp_seq)] = row

    for nid in nurse_ids:
        try:
            # nurses 테이블에서 해당 nurse_id 조회 (group_id 무관)
            existing_nurse = db.query(NurseModel).filter(
                NurseModel.nurse_id == str(nid)
            ).first()

            if existing_nurse:
                # 이미 nurses 테이블에 존재 → group_id만 변경
                existing_nurse.group_id = group_id
                # sequence는 현재 그룹 활성 목록의 마지막으로 배치
                next_seq = get_next_sequence_for_active_status(group_id, existing_nurse.active, db)
                existing_nurse.sequence = next_seq
                updated += 1
            else:
                # nurses 테이블에 없음 → MSSQL 멤버 정보로 신규 생성
                member_data = member_map.get(str(nid))
                if not member_data:
                    errors.append({"nurse_id": nid, "reason": "MSSQL 멤버 정보를 찾을 수 없습니다."})
                    continue

                _get = lambda key: member_data.get(key) if isinstance(member_data, dict) else getattr(member_data, key, None)

                # 경력 변환
                career_val = _get('career')
                experience = None
                if career_val is not None and str(career_val).strip():
                    try:
                        experience = int(career_val)
                    except (ValueError, TypeError):
                        experience = None

                # 수간호사 여부 변환
                headnurse_val = _get('headnurse')
                is_head_nurse = False
                if headnurse_val is not None:
                    is_head_nurse = str(headnurse_val).strip().upper() in ('Y', '1', 'TRUE')

                # 입사일 변환
                join_val = _get('joindate')
                joining_date = None
                if join_val:
                    try:
                        joining_date = parse_date(str(join_val))
                    except (ValueError, TypeError):
                        joining_date = None

                next_seq = get_next_sequence_for_active_status(group_id, 1, db)

                new_nurse = NurseModel(
                    nurse_id=str(nid),
                    group_id=group_id,
                    office_id=office_id,
                    account_id=str(_get('MemberID') or ''),
                    emp_num=str(_get('OfficeEmpNum') or '') if _get('OfficeEmpNum') else None,
                    name=str(_get('EmployeeName') or ''),
                    experience=experience,
                    role=str(_get('duty') or '') if _get('duty') else None,
                    is_head_nurse=is_head_nurse,
                    joining_date=joining_date,
                    birth_date=str(_get('DateOfBirth') or '') if _get('DateOfBirth') else None,
                    phone_number=str(_get('PortableTel') or '') if _get('PortableTel') else None,
                    gender=str(_get('Gender') or '') if _get('Gender') else None,
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
def delete_nurse_service(
    nurse_id: str,
    current_user: UserSchema,
    db: Session
):
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
    db_nurse = db.query(NurseModel).filter(
        NurseModel.nurse_id == nurse_id,
        NurseModel.group_id == current_user.group_id if not current_user.is_master_admin else True
    ).first()
    
    if not db_nurse:
        raise Exception(f"Nurse with nurse_id {nurse_id} not found")
    
    try:
        # 삭제 수행자 정보 조회
        deleter = db.query(NurseModel).filter(
            NurseModel.nurse_id == current_user.nurse_id
        ).first()

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
        db.delete(db_nurse)
        db.commit()

        # 삭제 후 active 상태별 재정렬
        active_nurses = _get_nurses_by_active(group_id, 1, db)
        _reindex_contiguously(active_nurses)
        inactive_nurses = _get_nurses_by_active(group_id, 0, db)
        _reindex_contiguously(inactive_nurses)
        db.commit()

        return {"message": f"Nurse {nurse_id} deleted successfully"}
    except Exception as e:
        db.rollback()
        raise Exception(f"Deletion failed: {str(e)}")
