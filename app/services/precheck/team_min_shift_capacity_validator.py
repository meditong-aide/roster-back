"""팀 min_shift 저장 전 모순을 검증하는 validator.

검증 범위 (HARD only, 무거운 capacity 분석은 일절 안 함):
  1) 팀에 배정된 인원이 0명인데 일일 최소 인원 합 > 0
       → TEAM_EMPTY_BUT_MIN_SET
  2) 팀 인원이 일일 최소 인원 합보다 적음 (모두가 한 날 동시에 일해도 불가)
       → TEAM_ACTIVE_LT_DAY_MIN_SUM

추가 검증:
  3) 월별 개인 제한(특히 OFF exact/min) 기준으로 팀의 월 총 필요량이 불가능
       → TEAM_MONTHLY_CAPACITY_LT_MIN_TOTAL
"""

from __future__ import annotations

from calendar import monthrange
from typing import Any, Iterable

from sqlalchemy.orm import Session

from db.models import Nurse, NurseMonthlyLimit, Team


SHIFT_NAMES = {
    "D": "주간(D)",
    "E": "이브닝(E)",
    "N": "야간(N)",
    "M": "미들(M)",
}


def _to_non_neg_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _exclude_role_etc(nurse: Nurse) -> bool:
    return str(getattr(nurse, "role", "") or "").lower() == "etc"


def _normalize_id(nid: Any) -> str:
    return str(nid) if nid is not None else ""


def _projected_members(
    db: Session,
    *,
    group_id: str,
    team_id: int,
    projected_member_ids: Iterable[str] | None,
) -> list[Nurse]:
    """저장 후 예상 팀 소속 nurse 목록을 반환한다.

    projected_member_ids 가 주어지면 그 ID 집합으로 nurse 를 조회.
    None 이면 현재 DB 의 team_id 기반 소속을 조회.
    """
    if projected_member_ids is not None:
        ids = {_normalize_id(x) for x in projected_member_ids if _normalize_id(x)}
        if not ids:
            return []
        return (
            db.query(Nurse)
            .filter(Nurse.group_id == group_id, Nurse.nurse_id.in_(ids))
            .all()
        )
    return (
        db.query(Nurse)
        .filter(Nurse.group_id == group_id, Nurse.team_id == team_id)
        .all()
    )


def validate_team_min_shift_capacity(
    db: Session,
    *,
    office_id: str,
    group_id: str,
    team_id: int,
    new_min_shift: dict[str, int] | None,
    projected_member_ids: Iterable[str] | None = None,
    exclude_role_etc: bool = True,
    team_name_hint: str | None = None,
) -> dict[str, Any]:
    """팀 인원 수 모순만 검증한다 (HARD only).

    Args:
        db: SQLAlchemy 세션
        office_id, group_id, team_id: 검증 대상 팀
        new_min_shift: 저장 예정 min_shift (None/{} 이면 검증 skip)
        projected_member_ids: 저장 후 팀 소속이 될 nurse_id 목록 (None 이면 현재 DB 상태)
        exclude_role_etc: 'etc' role 인력을 인원 카운트에서 제외할지
        team_name_hint: 신규 팀 등 DB 조회로 이름을 못 찾을 때 사용할 표시명

    Returns:
        {"saveable": bool, "issues": [{"severity":"hard","code":..., ...}]}
    """
    ms = dict(new_min_shift or {})
    by_shift = {
        "D": _to_non_neg_int(ms.get("D")),
        "E": _to_non_neg_int(ms.get("E")),
        "N": _to_non_neg_int(ms.get("N")),
        "M": _to_non_neg_int(ms.get("M")),
    }
    day_min_sum = sum(by_shift.values())

    # 일일 최소 인원 합이 0 → 검증 대상 아님
    if day_min_sum == 0:
        return {"saveable": True, "issues": []}

    # 팀 정보 (표시명만 사용)
    team_row = (
        db.query(Team)
        .filter(
            Team.office_id == office_id,
            Team.group_id == group_id,
            Team.team_id == team_id,
        )
        .first()
    )
    team_name = (
        getattr(team_row, "team_name", None)
        or team_name_hint
        or f"#{team_id}"
    )

    # 인원 조회
    members = _projected_members(
        db,
        group_id=group_id,
        team_id=team_id,
        projected_member_ids=projected_member_ids,
    )
    if exclude_role_etc:
        eligible = [n for n in members if not _exclude_role_etc(n)]
        excluded_etc_names = [
            getattr(n, "name", None) or _normalize_id(getattr(n, "nurse_id", ""))
            for n in members
            if _exclude_role_etc(n)
        ]
    else:
        eligible = list(members)
        excluded_etc_names = []
    eligible_cnt = len(eligible)

    pretty_min = {SHIFT_NAMES[k]: v for k, v in by_shift.items() if v > 0}

    issues: list[dict[str, Any]] = []

    # 1) 팀 인원 0 + min > 0
    if eligible_cnt == 0 and day_min_sum > 0:
        details: dict[str, Any] = {
            "팀": team_name,
            "현재 팀 인원": 0,
            "설정된 최소 인원 합": day_min_sum,
            "각 근무 최소": pretty_min,
        }
        if excluded_etc_names:
            details["근무 산정에서 제외된 인력"] = excluded_etc_names
        issues.append(
            {
                "severity": "hard",
                "code": "TEAM_EMPTY_BUT_MIN_SET",
                "title": "팀에 배정된 간호사가 없어요",
                "message": (
                    f"{team_name}팀에 소속된 간호사가 없는데 일일 최소 인원이 설정되어 있어요."
                ),
                "suggestion": "팀에 간호사를 먼저 배정하거나 모든 근무 최소를 0으로 두세요.",
                "details": details,
            }
        )
    elif eligible_cnt < day_min_sum:
        details = {
            "팀": team_name,
            "근무 가능 간호사 수": eligible_cnt,
            "일일 최소 인원 합": day_min_sum,
            "각 근무 최소": pretty_min,
        }
        if excluded_etc_names:
            details["근무 산정에서 제외된 인력"] = excluded_etc_names
        issues.append(
            {
                "severity": "hard",
                "code": "TEAM_ACTIVE_LT_DAY_MIN_SUM",
                "title": "팀 인원으로 일일 최소를 채울 수 없어요",
                "message": (
                    f"{team_name}팀의 근무 가능 간호사가 {eligible_cnt}명인데 매일 필요한 인원 합이 "
                    f"{day_min_sum}명이에요. 팀원 모두가 한 날 동시에 일해도 충족할 수 없는 설정이에요."
                ),
                "suggestion": "팀에 간호사를 추가하거나 일일 최소 인원을 낮춰 주세요.",
                "details": details,
            }
        )

    # 3) 월별 개인 제한(OFF exact/min) 기반 월 총량 검증
    if eligible and day_min_sum > 0:
        nurse_ids = [_normalize_id(getattr(n, "nurse_id", "")) for n in eligible]
        nurse_ids = [x for x in nurse_ids if x]
        if nurse_ids:
            rows = (
                db.query(NurseMonthlyLimit)
                .filter(
                    NurseMonthlyLimit.group_id == group_id,
                    NurseMonthlyLimit.nurse_id.in_(nurse_ids),
                )
                .all()
            )
            by_month: dict[tuple[int, int], dict[str, NurseMonthlyLimit]] = {}
            for r in rows:
                ym = (int(r.year), int(r.month))
                by_month.setdefault(ym, {})[str(r.nurse_id)] = r

            for (yy, mm), per_nurse in sorted(by_month.items()):
                days_in_month = monthrange(int(yy), int(mm))[1]
                required_total = int(day_min_sum) * int(days_in_month)
                max_workable_total = 0
                for nid in nurse_ids:
                    rec = per_nurse.get(nid)
                    if rec is None:
                        max_workable_total += days_in_month
                        continue
                    off_floor = 0
                    if rec.o_exact is not None:
                        off_floor = int(rec.o_exact)
                    elif rec.o_min is not None:
                        off_floor = int(rec.o_min)
                    max_workable_total += max(0, days_in_month - off_floor)

                if max_workable_total < required_total:
                    issues.append(
                        {
                            "severity": "hard",
                            "code": "TEAM_MONTHLY_CAPACITY_LT_MIN_TOTAL",
                            "title": "월별 개인 제한으로 팀 최소 인원을 채울 수 없어요",
                            "message": (
                                f"{team_name}팀 {yy}-{mm:02d} 기준 월 총 필요량({required_total})이 "
                                f"팀 월 최대 근무 가능량({max_workable_total})을 초과해요."
                            ),
                            "suggestion": (
                                "팀 min_shift를 낮추거나, 개인 OFF exact/min 제한을 완화하거나, "
                                "팀 인원을 조정해 주세요."
                            ),
                            "details": {
                                "팀": team_name,
                                "year": yy,
                                "month": mm,
                                "days_in_month": days_in_month,
                                "일일 최소 인원 합": day_min_sum,
                                "월 총 필요량": required_total,
                                "팀 월 최대 근무 가능량": max_workable_total,
                                "대상 인원 수": len(nurse_ids),
                            },
                        }
                    )

    saveable = not any(i.get("severity") == "hard" for i in issues)
    return {"saveable": saveable, "issues": issues}
