"""신규 간호사 자동 동기화 서비스.

eun_gw.bizwiz20db.icmc_member_org_info 기준으로
특정 파트(42병동, 52병동, 중환자간호)에 소속되어 있으나
eun_roster.dbo.nurses에 아직 등록되지 않은 간호사를 자동 추가한다.
"""

import logging
import os
from datetime import datetime

from sqlalchemy.orm import Session

from db.client2 import msdb_manager
from db.models import Nurse as NurseModel, Group as GroupModel
from services.nurse_service import get_next_sequence_for_active_status

logger = logging.getLogger("nurse_sync")

OFFICE_ID = "102243"

# 부서 + 직책코드 → (group_name, role)
# 0008=간호사(RN), 0014=간호조무사(AN). 그 외 직군(예: 0060 운송원)은
# 같은 부서의 -AN 그룹에 ETC role 로 fallback (아래 _AN_GROUP_BY_ORG 참조).
_GROUP_ROLE_MAP: dict[tuple[str, str], tuple[str, str]] = {
    ("42병동파트", "0008"): ("42병동-RN", "RN"),
    ("42병동파트", "0014"): ("42병동-AN", "AN"),
    ("52병동파트", "0008"): ("52병동-RN", "RN"),
    ("52병동파트", "0014"): ("52병동-AN", "AN"),
    ("중환자간호파트", "0008"): ("중환자실", "RN"),
    ("중환자간호파트", "0014"): ("중환자실", "AN"),
}

# 간호직군 title_code 화이트리스트
_KNOWN_NURSE_TITLE_CODES = {"0008", "0014"}

# 비-간호직군 (운송원 등) fallback 용 -AN 그룹명 매핑
_AN_GROUP_BY_ORG: dict[str, str] = {
    "42병동파트": "42병동-AN",
    "52병동파트": "52병동-AN",
    "중환자간호파트": "중환자실",
}

# 환경별 DB명: 운영=eun_roster, 개발=eun_roster_dev
_ROSTER_DB = os.getenv("EUN_DB_NAME", "eun_roster")

_TARGET_ORGS = ('42병동파트', '52병동파트', '중환자간호파트')

# 1단계: UTF-8로 미등록 memberid 목록 조회 (한글 WHERE 필요)
_QUERY_UNREGISTERED_IDS = f"""
SELECT a.memberid, a.orgnm
  FROM eun_gw.bizwiz20db.icmc_member_org_info a
  LEFT OUTER JOIN (
        SELECT n.account_id
          FROM {_ROSTER_DB}.dbo.nurses n
          JOIN {_ROSTER_DB}.dbo.groups g ON n.group_id = g.group_id
         WHERE g.office_id = %s
       ) b
    ON a.memberid = b.account_id
 WHERE a.orgnm IN (%s, %s, %s)
   AND b.account_id IS NULL
"""

# 2단계: EUC-KR로 상세 정보 조회 (memberid 영문 기반)
_QUERY_NURSE_DETAIL = f"""
SELECT m.EmpSeqNo
     , m.EmployeeName
     , m.OfficeEmpNum
     , m.OfficialTitleCode
     , ISNULL(m.career, 0) AS career
     , m.headnurse
     , m.JoinDate
     , LEFT(CONVERT(VARCHAR(10), m.DateOfBirth, 23), 10) AS DateOfBirth
     , m.PortableTel
     , CASE WHEN UPPER(TRIM(COALESCE(NULLIF(m.Gender, N' '), N'기타'))) IN (N'남', N'Y') THEN N'남'
            WHEN UPPER(TRIM(COALESCE(NULLIF(m.Gender, N' '), N'기타'))) IN (N'여', N'N') THEN N'여'
            ELSE TRIM(COALESCE(NULLIF(m.Gender, N' '), N'기타'))
       END AS Gender
     , m.Email
  FROM eun_gw.bizwiz20db.MEMBER m
 WHERE (m.OfficeEmpNum = %s OR m.OfficeEmpNum = %s)
   AND m.EmployeeName = %s
"""


def _parse_joining_date(val) -> datetime | None:
    """입사일 문자열을 datetime으로 변환."""
    if not val:
        return None
    try:
        from dateutil.parser import parse as parse_date
        return parse_date(str(val))
    except (ValueError, TypeError):
        return None


def _parse_experience(val) -> int:
    """경력 값을 int로 변환. 없으면 1."""
    if val is None:
        return 1
    try:
        result = int(val)
        return result if result >= 1 else 1
    except (ValueError, TypeError):
        return 1


def sync_new_nurses(db: Session) -> dict:
    """신규 간호사 자동 동기화 실행.

    1단계: UTF-8로 미등록 memberid+orgnm 조회 (한글 WHERE 필요)
    2단계: EUC-KR로 memberid 기반 상세 정보 조회 (한글 깨짐 방지)

    Returns:
        {"added": int, "skipped": int, "errors": list[dict]}
    """
    # 1단계: 미등록 대상 memberid 조회 (UTF-8)
    unregistered = msdb_manager.fetch_all(
        _QUERY_UNREGISTERED_IDS,
        params=(OFFICE_ID, *_TARGET_ORGS),
        charset='UTF-8',
    )
    if not unregistered:
        return {"added": 0, "skipped": 0, "errors": []}

    # orgnm도 UTF-8에서는 깨지므로, memberid → orgnm 매핑을 별도 조회
    # memberid 기반으로 EUC-KR에서 orgnm 재조회
    member_ids = [str(r["memberid"]).strip() for r in unregistered]
    placeholders = ",".join(["%s"] * len(member_ids))
    orgnm_rows = msdb_manager.fetch_all(
        f"SELECT memberid, orgnm FROM eun_gw.bizwiz20db.icmc_member_org_info WHERE memberid IN ({placeholders})",
        params=tuple(member_ids),
    )
    memberid_to_orgnm: dict[str, str] = {
        str(r["memberid"]).strip(): str(r["orgnm"]).strip() for r in orgnm_rows
    }

    # T_Part 코드 → 직군명 (ETC fallback 시 nurses.level_ 채울 때 사용)
    part_rows = msdb_manager.fetch_all(
        "SELECT code, name FROM eun_gw.bizwiz20db.T_Part WHERE OfficeCode = %s",
        params=(OFFICE_ID,),
    )
    part_code_to_name: dict[str, str] = {
        str(r["code"]).strip(): str(r["name"]).strip() for r in part_rows
    }

    # groups 테이블에서 group_name → group_id 매핑 캐시
    groups = (
        db.query(GroupModel.group_id, GroupModel.group_name)
        .filter(GroupModel.office_id == OFFICE_ID)
        .all()
    )
    group_name_to_id: dict[str, str] = {g.group_name: g.group_id for g in groups}

    # 기존 account_id 집합 (중복 방지)
    existing_accounts: set[str] = set(
        aid
        for (aid,) in db.query(NurseModel.account_id)
        .join(GroupModel, NurseModel.group_id == GroupModel.group_id)
        .filter(GroupModel.office_id == OFFICE_ID)
        .all()
    )

    added = 0
    skipped = 0
    errors: list[dict] = []

    for ur in unregistered:
        account_id = str(ur["memberid"]).strip()
        if not account_id or account_id in existing_accounts:
            skipped += 1
            continue

        orgnm = memberid_to_orgnm.get(account_id, "")
        # memberid에서 숫자 부분 추출 (icmc14120013 → 14120013)
        short_id = account_id[4:] if account_id.startswith("icmc") else account_id

        # 2단계: EUC-KR로 MEMBER 상세 조회
        details = msdb_manager.fetch_all(
            _QUERY_NURSE_DETAIL,
            params=(account_id, short_id, orgnm),
        )

        # employeename으로 조회해야 하므로 orgnm 대신 이름 필요 — 쿼리 수정
        # 실제로는 OfficeEmpNum 매칭 + employeename 조회
        if not details:
            # OfficeEmpNum 기반 단순 조회
            details = msdb_manager.fetch_all(
                f"""SELECT m.EmpSeqNo, m.EmployeeName, m.OfficeEmpNum, m.OfficialTitleCode,
                       ISNULL(m.career, 0) AS career, m.headnurse, m.JoinDate,
                       LEFT(CONVERT(VARCHAR(10), m.DateOfBirth, 23), 10) AS DateOfBirth,
                       m.PortableTel,
                       CASE WHEN UPPER(TRIM(COALESCE(NULLIF(m.Gender, N' '), N'기타'))) IN (N'남', N'Y') THEN N'남'
                            WHEN UPPER(TRIM(COALESCE(NULLIF(m.Gender, N' '), N'기타'))) IN (N'여', N'N') THEN N'여'
                            ELSE TRIM(COALESCE(NULLIF(m.Gender, N' '), N'기타'))
                       END AS Gender, m.Email
                  FROM eun_gw.bizwiz20db.MEMBER m
                 WHERE m.OfficeEmpNum IN (%s, %s)""",
                params=(account_id, short_id),
            )

        if not details:
            errors.append({"account_id": account_id, "name": "", "reason": "MEMBER 테이블에서 조회 불가"})
            continue

        row = details[0]
        emp_name = str(row.get("EmployeeName", "")).strip()
        title_code = str(row.get("OfficialTitleCode", "")).strip()
        mapping = _GROUP_ROLE_MAP.get((orgnm, title_code))
        title_level: str | None = None  # ETC fallback 시 직군명 (level_ 컬럼)
        if not mapping and title_code not in _KNOWN_NURSE_TITLE_CODES:
            # 비-간호직군 (예: 0060 운송원) → 같은 부서 -AN 그룹에 ETC role 로 등록
            an_group = _AN_GROUP_BY_ORG.get(orgnm)
            if an_group:
                mapping = (an_group, "ETC")
                title_level = part_code_to_name.get(title_code) or None
        if not mapping:
            errors.append({
                "account_id": account_id,
                "name": emp_name,
                "reason": f"매핑 불가: orgnm={orgnm}, titleCode={title_code}",
            })
            continue

        target_group_name, role = mapping
        group_id = group_name_to_id.get(target_group_name)
        if not group_id:
            errors.append({
                "account_id": account_id,
                "name": emp_name,
                "reason": f"그룹 미존재: {target_group_name}",
            })
            continue

        try:
            experience = _parse_experience(row.get("career"))
            is_head = str(row.get("headnurse", "")).strip().upper() in ("Y", "1", "TRUE")
            next_seq = get_next_sequence_for_active_status(group_id, 1, db, role=role)

            nurse = NurseModel(
                nurse_id=str(row.get("EmpSeqNo", "")),
                group_id=group_id,
                office_id=OFFICE_ID,
                account_id=account_id,
                emp_num=str(row.get("OfficeEmpNum", "")) or None,
                name=emp_name,
                experience=experience,
                role=role,
                level_=title_level,  # ETC 케이스만 채워짐 (예: '운송원')
                is_head_nurse=is_head,
                joining_date=_parse_joining_date(row.get("JoinDate")),
                birth_date=str(row.get("DateOfBirth", "")) or None,
                phone_number=str(row.get("PortableTel", "")) or None,
                email=str(row.get("Email", "")) or None,
                gender=str(row.get("Gender", "")) or None,
                sequence=next_seq,
                active=1,
            )
            db.add(nurse)
            existing_accounts.add(account_id)
            added += 1

        except Exception as e:
            logger.error(
                "[nurse_sync] account_id=%s 처리 실패: %s",
                account_id, e, exc_info=True,
            )
            errors.append({"account_id": account_id, "name": emp_name, "reason": str(e)})

    if added > 0:
        db.commit()

    return {"added": added, "skipped": skipped, "errors": errors}
