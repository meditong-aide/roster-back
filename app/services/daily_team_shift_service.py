"""일자별 가동 팀 + 팀별 최소 인원 (daily_team_shift) 조회·저장.

teams.min_shift 는 월 전체 고정이라 "주중엔 4개 팀, 주말엔 4·3·2팀만" 을 담을 수
없다. 이 서비스가 날짜마다 도는 팀을 관리한다.

★ 저장 계약에서 제일 중요한 것: **행이 없는 날 = 미설정 = 전 팀 가동**이다.
  그래서 빈 목록(teams=[])은 **거부한다** — 저장하면 역시 행 0개가 되어 미설정과
  구분이 안 되고, 조회하면 None 으로 돌아와 의도가 조용히 사라진다.
  그날 병동을 통째로 비우려면 daily_shift 의 요구 인원을 0 으로 두면 된다.
"""

from __future__ import annotations

import calendar
from typing import Any, Dict, List

from sqlalchemy.orm import Session

from db.models import DailyTeamShift, Nurse, Team

_CODES = ("d", "e", "n", "m")


def _days_in_month(year: int, month: int) -> int:
    return calendar.monthrange(int(year), int(month))[1]


def _row_to_entry(row: DailyTeamShift) -> Dict[str, Any]:
    return {
        "team_id": int(getattr(row, "team_id", 0) or 0),
        "d_count": int(getattr(row, "d_count", 0) or 0),
        "e_count": int(getattr(row, "e_count", 0) or 0),
        "n_count": int(getattr(row, "n_count", 0) or 0),
        "m_count": int(getattr(row, "m_count", 0) or 0),
    }


def get_month_teams(
    db: Session, office_id: str, group_id: str, year: int, month: int
) -> Dict[str, Any]:
    """월 전체 일자별 가동 팀.

    date[i] 는 i+1 일. `teams` 가 None 이면 **미설정(전 팀 가동)**, 리스트면 그
    팀들만 가동한다. 저장이 빈 목록을 거부하므로 리스트는 항상 1개 이상이다.
    """
    dim = _days_in_month(year, month)
    rows = (
        db.query(DailyTeamShift)
        .filter(
            DailyTeamShift.office_id == office_id,
            DailyTeamShift.group_id == group_id,
            DailyTeamShift.year == year,
            DailyTeamShift.month == month,
        )
        .all()
    )

    by_day: Dict[int, List[Dict[str, Any]]] = {}
    for row in rows:
        d = int(getattr(row, "day", 0) or 0)
        if d < 1 or d > dim:
            continue
        by_day.setdefault(d, []).append(_row_to_entry(row))

    date_list: List[Dict[str, Any]] = []
    for d in range(1, dim + 1):
        entries = by_day.get(d)
        if entries is not None:
            entries.sort(key=lambda e: e["team_id"])
        date_list.append({"day": d, "teams": entries})

    return {
        "office_id": office_id,
        "group_id": group_id,
        "year": int(year),
        "month": int(month),
        "date": date_list,
        "configured_days": sum(1 for x in date_list if x["teams"] is not None),
    }


def _member_counts_by_team(db: Session, group_id: str) -> Dict[int, int]:
    """팀별 인원 수. 가동 지정이 실효가 있는지 경고할 때 쓴다."""
    counts: Dict[int, int] = {}
    for nurse in (
        db.query(Nurse).filter(Nurse.group_id == group_id, Nurse.active == 1).all()
    ):
        tid = getattr(nurse, "team_id", None)
        if tid in (None, "", 0):
            continue
        counts[int(tid)] = counts.get(int(tid), 0) + 1
    return counts


def _build_warnings(
    db: Session, group_id: str, days: List[Dict[str, Any]]
) -> List[str]:
    """저장 전 점검. 막지는 않고 알려만 준다."""
    warnings: List[str] = []
    member_counts = _member_counts_by_team(db, group_id)
    known = {
        int(t.team_id)
        for t in db.query(Team).filter(Team.group_id == group_id, Team.active == 1).all()
    }

    for item in days:
        day = int(item.get("day", 0) or 0)
        teams = item.get("teams")
        if not teams:
            continue  # 미설정(None). 빈 목록은 호출 전에 이미 거부된다.
        ids = [int(t.get("team_id", 0) or 0) for t in teams]
        unknown = [i for i in ids if i not in known]
        if unknown:
            warnings.append(f"{day}일: 없는 팀 {unknown} 이 지정됐습니다.")
        empty = [i for i in ids if member_counts.get(i, 0) == 0]
        if empty:
            warnings.append(
                f"{day}일: 팀 {empty} 에 인원이 없습니다. "
                "그날 근무 가능한 사람이 사라져 생성이 실패할 수 있습니다."
            )
    return warnings


def replace_month_teams(
    db: Session,
    office_id: str,
    group_id: str,
    year: int,
    month: int,
    days: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """일자별 가동 팀을 통째로 교체한다(delete-then-insert).

    days 항목:
        {"day": 1, "teams": [{"team_id": 1, "d_count": 2, "e_count": 1, ...}]}
        - teams=None  → 그날 설정 삭제(미설정 = 전 팀 가동)
        - teams=[]    → **거부**(ValueError). 미설정과 구분이 안 되기 때문.
        - *_count 0   → 인원 미지정(팀 수 규칙에 위임)

    days 에 없는 날짜는 **건드리지 않는다**(부분 저장). 월 전체를 비우려면 모든
    날짜를 teams=None 으로 보내야 한다.
    """
    dim = _days_in_month(year, month)
    target_days = sorted(
        {
            int(item.get("day", 0) or 0)
            for item in days
            if 1 <= int(item.get("day", 0) or 0) <= dim
        }
    )
    if not target_days:
        return get_month_teams(db, office_id, group_id, year, month)

    # ★ 빈 목록은 거부한다. 저장 구조상 "행 0개"라 **미설정과 구분할 수 없고**,
    #   조회하면 None 으로 돌아와 의도가 조용히 사라진다. 그날 병동을 통째로
    #   비우고 싶으면 daily_shift 의 요구 인원을 0 으로 두는 별도 수단이 있다.
    #   지우려는 의도면 teams=null 을 보내야 한다.
    for item in days:
        day = int(item.get("day", 0) or 0)
        if not (1 <= day <= dim):
            continue
        teams = item.get("teams")
        if teams is not None and len(teams) == 0:
            raise ValueError(
                f"{day}일: 가동 팀이 비어 있습니다. 최소 1개 팀을 지정하세요. "
                "그날 설정을 지우려면 teams 를 null 로 보내면 됩니다."
            )

    warnings = _build_warnings(db, group_id, days)

    # 대상 날짜만 지우고 다시 넣는다 — 부분 저장이라 다른 날짜 설정은 보존.
    (
        db.query(DailyTeamShift)
        .filter(
            DailyTeamShift.office_id == office_id,
            DailyTeamShift.group_id == group_id,
            DailyTeamShift.year == year,
            DailyTeamShift.month == month,
            DailyTeamShift.day.in_(target_days),
        )
        .delete(synchronize_session=False)
    )

    inserted = 0
    for item in days:
        day = int(item.get("day", 0) or 0)
        if day < 1 or day > dim:
            continue
        teams = item.get("teams")
        if teams is None:
            continue  # 미설정으로 남긴다(행 없음)
        for entry in teams:
            tid = int(entry.get("team_id", 0) or 0)
            if tid <= 0:
                continue
            db.add(
                DailyTeamShift(
                    office_id=office_id,
                    group_id=group_id,
                    year=int(year),
                    month=int(month),
                    day=day,
                    team_id=tid,
                    **{
                        f"{c}_count": max(0, int(entry.get(f"{c}_count", 0) or 0))
                        for c in _CODES
                    },
                )
            )
            inserted += 1

    db.commit()
    result = get_month_teams(db, office_id, group_id, year, month)
    result["warnings"] = warnings
    result["inserted"] = inserted
    return result
