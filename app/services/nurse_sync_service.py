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
_GROUP_ROLE_MAP: dict[tuple[str, str], tuple[str, str]] = {
    ("42병동파트", "0008"): ("42병동-RN", "RN"),
    ("42병동파트", "0014"): ("42병동-AN", "AN"),
    ("52병동파트", "0008"): ("52병동-RN", "RN"),
    ("52병동파트", "0014"): ("52병동-AN", "AN"),
    ("중환자간호파트", "0008"): ("중환자실", "RN"),
    ("중환자간호파트", "0014"): ("중환자실", "AN"),
}

# 환경별 DB명: 운영=eun_roster, 개발=eun_roster_dev
_ROSTER_DB = os.getenv("EUN_DB_NAME", "eun_roster")

_QUERY_NEW_NURSES = f"""
WITH temp AS (
    SELECT a.employeename
         , a.orgnm
         , a.memberid
      FROM eun_gw.bizwiz20db.icmc_member_org_info a
      LEFT OUTER JOIN (
            SELECT n.nurse_id, n.name, n.account_id
              FROM {_ROSTER_DB}.dbo.nurses n
              JOIN {_ROSTER_DB}.dbo.groups g ON n.group_id = g.group_id
             WHERE g.office_id = %s
           ) b
        ON a.memberid = b.account_id
     WHERE a.orgnm IN (N'42병동파트', N'52병동파트', N'중환자간호파트')
       AND b.name IS NULL
)
SELECT m.EmpSeqNo
     , t.memberid          AS account_id
     , m.EmployeeName
     , m.OfficeEmpNum
     , m.OfficialTitleCode
     , t.orgnm
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
  JOIN temp t
    ON m.EmployeeName = t.employeename
   AND (m.OfficeEmpNum = t.memberid
    OR  m.OfficeEmpNum = SUBSTRING(t.memberid, 5, LEN(t.memberid)))
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

    Returns:
        {"added": int, "skipped": int, "errors": list[dict]}
    """
    rows = msdb_manager.fetch_all(_QUERY_NEW_NURSES, params=(OFFICE_ID,), charset='UTF-8')
    if not rows:
        return {"added": 0, "skipped": 0, "errors": []}

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

    for row in rows:
        account_id = str(row.get("account_id", "")).strip()
        emp_name = str(row.get("EmployeeName", "")).strip()

        if not account_id:
            skipped += 1
            continue

        if account_id in existing_accounts:
            skipped += 1
            continue

        orgnm = str(row.get("orgnm", "")).strip()
        title_code = str(row.get("OfficialTitleCode", "")).strip()
        mapping = _GROUP_ROLE_MAP.get((orgnm, title_code))
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
