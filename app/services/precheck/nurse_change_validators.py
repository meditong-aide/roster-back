"""nurse 프로필/배정 단건 update 시 team_id / grade 변경의 가벼운 정합성 검증.

검증 대상:
    - team_id 변경: validate_team_min_shift_capacity 재사용 (TEAM_EMPTY_BUT_MIN_SET,
      TEAM_ACTIVE_LT_DAY_MIN_SUM, TEAM_MONTHLY_CAPACITY_LT_MIN_TOTAL)
    - grade 변경: roster_grade_config.constraints_json 의 (shift, grade) min 합산 vs
      변경 후 group 내 grade 별 인원 수 비교 (GRADE_MIN_AVAILABLE_SHORTAGE_AFTER_CHANGE)

scope:
    - 'source': nurses.* 직접 수정 (일반 근무자, 본인 group 의 source nurse)
    - 'target': nurse_assignment.target_* 수정 (inbound 근무자 — 사이드프로필)

projected_member_ids 는 native(nurses) + inbound(nurse_assignment) 를 합산하고,
swap_nurse_id 의 (이전 → 새) 변경을 반영한다.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy.orm import Session

from db.models import (
    Nurse,
    NurseAssignment,
    RosterGradeConfig,
    Team,
)
from services.precheck.team_min_shift_capacity_validator import (
    validate_team_min_shift_capacity,
)


_INBOUND_REASONS: Tuple[str, ...] = ("파견", "병동이동")


def _team_members_after_swap(
    db: Session,
    *,
    group_id: str,
    team_id: int,
    swap_nurse_id: Optional[str],
    swap_new_team_id: Optional[int],
) -> List[str]:
    """team_id 의 변경 후 멤버 nurse_id 리스트 (native + inbound 합산).

    swap_nurse_id 가:
      - new_team_id == team_id → team_id 의 멤버에 추가
      - new_team_id != team_id → team_id 의 멤버에서 제거
    """
    # 멤버십은 시점(period) 기준 — nurses.team_id·nurse_assignment.target_team_id 는 NULL
    #   이행 대상이라 캐시로 세면 0명. resolve_teams_for_month 가 home + 전입(이 그룹 period)
    #   을 합산해 그 시점 팀 소속을 준다(전입자도 [B3] period 가 이 그룹에 기록됨).
    from datetime import date as _d
    from services.team_period import resolve_teams_for_month as _rtfm
    members = {
        nid for nid, tid in _rtfm(db, group_id, _d.today()).items()
        if tid is not None and int(tid) == int(team_id)
    }
    if swap_nurse_id is not None:
        if swap_new_team_id == team_id:
            members.add(swap_nurse_id)
        else:
            members.discard(swap_nurse_id)
    return list(members)


def validate_team_change(
    db: Session,
    *,
    group_id: str,
    swap_nurse_id: str,
    old_team_id: Optional[int],
    new_team_id: Optional[int],
) -> Dict[str, Any]:
    """nurse 의 team_id 변경이 group 의 team min_shift 정합성을 깨는지 검증.

    변경 전 team 과 변경 후 team 양쪽에 대해
    validate_team_min_shift_capacity 호출하여 hard issue 합산.
    """
    if old_team_id == new_team_id:
        return {"saveable": True, "issues": []}

    issues: List[Dict[str, Any]] = []
    seen_team_ids: set = set()
    for tid in (old_team_id, new_team_id):
        if tid is None:
            continue
        try:
            tid_int = int(tid)
        except (TypeError, ValueError):
            continue
        if tid_int <= 0 or tid_int in seen_team_ids:
            continue
        seen_team_ids.add(tid_int)
        team = (
            db.query(Team)
            .filter(Team.group_id == group_id, Team.team_id == tid_int)
            .first()
        )
        if team is None:
            continue
        min_shift = getattr(team, "min_shift", None) or {}
        if not min_shift:
            continue
        members = _team_members_after_swap(
            db,
            group_id=group_id,
            team_id=tid_int,
            swap_nurse_id=swap_nurse_id,
            swap_new_team_id=int(new_team_id) if new_team_id is not None else None,
        )
        result = validate_team_min_shift_capacity(
            db,
            office_id=team.office_id,
            group_id=group_id,
            team_id=tid_int,
            new_min_shift=min_shift,
            projected_member_ids=members,
            team_name_hint=getattr(team, "team_name", None),
        )
        for it in result.get("issues", []):
            it.setdefault("scope", "team")
            issues.append(it)

    saveable = not any(i.get("severity") == "hard" for i in issues)
    return {"saveable": saveable, "issues": issues}


def _grade_counts_after_swap(
    db: Session,
    *,
    group_id: str,
    swap_nurse_id: str,
    swap_new_grade: Optional[int],
    scope: str,  # 'source' | 'target'
) -> Dict[int, int]:
    """변경 후 group 내 grade 별 활성 nurse 수.

    scope='source': nurses.grade 의 swap_nurse_id 값을 swap_new_grade 로 치환
    scope='target': inbound (nurse_assignment.target_grade) 의 swap_nurse_id 값을 치환
    """
    native_rows = (
        db.query(Nurse.nurse_id, Nurse.grade)
        .filter(Nurse.group_id == group_id, Nurse.active == 1)
        .all()
    )
    inbound_rows = (
        db.query(NurseAssignment.nurse_id, NurseAssignment.target_grade)
        .join(Nurse, Nurse.nurse_id == NurseAssignment.nurse_id)
        .filter(
            NurseAssignment.target_group_id == group_id,
            NurseAssignment.status == "active",
            NurseAssignment.reason.in_(_INBOUND_REASONS),
            Nurse.active == 1,
        )
        .all()
    )

    counts: Dict[int, int] = {}
    seen_native_ids: set = set()

    def _bump(g_val: Any) -> None:
        if g_val is None:
            return
        try:
            gi = int(g_val)
        except (TypeError, ValueError):
            return
        counts[gi] = counts.get(gi, 0) + 1

    for nid, g in native_rows:
        seen_native_ids.add(nid)
        if scope == "source" and str(nid) == str(swap_nurse_id):
            _bump(swap_new_grade)
        else:
            _bump(g)

    for nid, t_grade in inbound_rows:
        if nid in seen_native_ids:
            # 동일 nurse 가 native 와 inbound 둘 다 있는 비정상 케이스 — native 한 번만 카운트
            continue
        if scope == "target" and str(nid) == str(swap_nurse_id):
            _bump(swap_new_grade)
        else:
            _bump(t_grade)
    return counts


def validate_grade_change(
    db: Session,
    *,
    group_id: str,
    swap_nurse_id: str,
    old_grade: Optional[int],
    new_grade: Optional[int],
    scope: str,  # 'source' | 'target'
) -> Dict[str, Any]:
    """nurse 의 grade 변경이 group 의 grade min 정합성을 깨는지 검증.

    roster_grade_config.constraints_json 의 (shift, grade) 별 min 인원 vs
    변경 후 group 내 grade 별 활성 nurse 수 비교.
    """
    if old_grade == new_grade:
        return {"saveable": True, "issues": []}

    cfg = (
        db.query(RosterGradeConfig)
        .filter(RosterGradeConfig.group_id == group_id)
        .first()
    )
    constraints = (cfg.constraints_json if cfg else None) or {}
    if not constraints:
        return {"saveable": True, "issues": []}

    counts = _grade_counts_after_swap(
        db,
        group_id=group_id,
        swap_nurse_id=swap_nurse_id,
        swap_new_grade=new_grade,
        scope=scope,
    )

    grade_names = (cfg.grade_names_json if cfg else None) or {}

    issues: List[Dict[str, Any]] = []
    for shift, per_grade_min in (constraints or {}).items():
        if not isinstance(per_grade_min, dict):
            continue
        for g_raw, req_raw in per_grade_min.items():
            try:
                g = int(g_raw)
                req = int(req_raw or 0)
            except (TypeError, ValueError):
                continue
            if req <= 0:
                continue
            avail = counts.get(g, 0)
            if req > avail:
                g_label = str(grade_names.get(str(g)) or grade_names.get(g) or f"Grade {g}")
                issues.append(
                    {
                        "severity": "hard",
                        "scope": "grade",
                        "code": "GRADE_MIN_AVAILABLE_SHORTAGE_AFTER_CHANGE",
                        "title": "Grade 별 최소 인원을 채울 수 없어요",
                        "message": (
                            f"{shift} 근무의 {g_label} 최소 인원이 {req}명인데 "
                            f"변경 후 해당 grade 의 활성 간호사가 {avail}명만 있어요."
                        ),
                        "suggestion": (
                            "다른 간호사의 grade 를 조정하거나 grade 설정에서 최소 인원을 낮춰 주세요."
                        ),
                        "details": {
                            "shift": shift,
                            "grade": g,
                            "grade_label": g_label,
                            "required": req,
                            "available_after_change": avail,
                        },
                    }
                )

    # grade 별 일별 동시 필요 인원 합 (D+E+N(+M) min 합) vs 변경 후 인원 비교.
    # 한 사람이 같은 날 두 시프트를 동시에 못 하므로, grade 별 시프트 min 의 합이
    # grade 인원보다 크면 일별 cover 불가 → infeasible.
    grade_daily_sum: Dict[int, int] = {}
    shift_breakdown: Dict[int, Dict[str, int]] = {}
    for shift, per_grade_min in (constraints or {}).items():
        if not isinstance(per_grade_min, dict):
            continue
        for g_raw, req_raw in per_grade_min.items():
            try:
                g = int(g_raw)
                req = int(req_raw or 0)
            except (TypeError, ValueError):
                continue
            if req <= 0:
                continue
            grade_daily_sum[g] = grade_daily_sum.get(g, 0) + req
            shift_breakdown.setdefault(g, {})[str(shift)] = req

    for g, daily_sum in grade_daily_sum.items():
        avail = counts.get(g, 0)
        if daily_sum > avail:
            g_label = str(grade_names.get(str(g)) or grade_names.get(g) or f"Grade {g}")
            issues.append(
                {
                    "severity": "hard",
                    "scope": "grade",
                    "code": "GRADE_DAILY_SUM_EXCEEDS_AVAILABLE_AFTER_CHANGE",
                    "title": "Grade 별 일별 동시 필요 인원을 채울 수 없어요",
                    "message": (
                        f"{g_label} 의 일별 동시 필요 인원이 {daily_sum}명(D+E+N min 합)인데 "
                        f"변경 후 해당 grade 의 활성 간호사가 {avail}명만 있어요. "
                        f"한 사람이 같은 날 두 시프트를 동시에 할 수 없어 인원이 부족해요."
                    ),
                    "suggestion": (
                        "다른 간호사의 grade 를 조정하거나 grade 설정에서 시프트 별 최소 인원을 낮춰 주세요."
                    ),
                    "details": {
                        "grade": g,
                        "grade_label": g_label,
                        "daily_sum_required": daily_sum,
                        "available_after_change": avail,
                        "min_by_shift": shift_breakdown.get(g, {}),
                    },
                }
            )

    saveable = not any(i.get("severity") == "hard" for i in issues)
    return {"saveable": saveable, "issues": issues}


def validate_nurse_change(
    db: Session,
    *,
    group_id: str,
    swap_nurse_id: str,
    old_team_id: Optional[int],
    new_team_id: Optional[int],
    old_grade: Optional[int],
    new_grade: Optional[int],
    scope: str,  # 'source' | 'target'
) -> Dict[str, Any]:
    """team/grade 변경을 묶어서 검증. issues 합산."""
    issues: List[Dict[str, Any]] = []
    if old_team_id != new_team_id:
        t = validate_team_change(
            db,
            group_id=group_id,
            swap_nurse_id=swap_nurse_id,
            old_team_id=old_team_id,
            new_team_id=new_team_id,
        )
        issues.extend(t.get("issues", []))
    if old_grade != new_grade:
        g = validate_grade_change(
            db,
            group_id=group_id,
            swap_nurse_id=swap_nurse_id,
            old_grade=old_grade,
            new_grade=new_grade,
            scope=scope,
        )
        issues.extend(g.get("issues", []))
    saveable = not any(i.get("severity") == "hard" for i in issues)
    return {"saveable": saveable, "issues": issues}
