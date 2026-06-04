"""원티드 기반 팀 분류 (옵션 1: 병동 내).

확정 원티드(FixedWantedEntry)의 OFF/연차 겹침으로 팀을 재구성한다.
- 입력: 병동 간호사 + 확정 원티드(OFF=shift_id∈{O,OFF,주}, 연차=Shift.type='휴가')
- 엔진: team_auto_assign.auto_assign_teams (Greedy + 2-opt)
- num_teams: 현재 병동의 distinct team_id 수 유지
- 출력(preview): 제안 팀 + 현재팀 대비 변경 diff + 통계 (read-only)
- 적용(apply): 변경되는 간호사마다 permanent_change 이벤트 생성 → 대상월 1일 발효

N전담(is_night_nurse==['N'])은 팀 배정 풀에서 제외한다.
참조: docs/NURSE_GROUP_CHANGE_MODEL.md (옵션1), team_auto_assign.py.
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from sqlalchemy.orm import Session

from db.models import FixedWantedEntry, Nurse as NurseModel, Shift
from services.assignment_service import create_permanent_change
from services.team_auto_assign import NurseInput, auto_assign_teams

_OFF_SHIFT_CODES = frozenset({"O", "OFF", "주"})


def _load_pool_and_inputs(
    db: Session, group_id: str, year: int, month: int
) -> tuple[list[NurseModel], list[NurseInput]]:
    """병동 간호사(풀) + auto_assign 입력 NurseInput 구성.

    N전담(is_night_nurse==['N'])은 풀에서 제외.
    off_days = 확정 원티드 OFF 일자, fb_days = 확정 원티드 연차(휴가 type) 일자.
    """
    nurses = (
        db.query(NurseModel)
        .filter(NurseModel.group_id == group_id, NurseModel.active == 1)
        .all()
    )
    # N전담 제외 (is_night_nurse 가 N 전용 리스트면 일반 팀 배정 대상 아님)
    pool = [n for n in nurses if (n.is_night_nurse or []) != ["N"]]

    # 휴가 type shift_id 집합 (연차/FB 판별용) — 그룹 기준
    vacation_codes = {
        s.shift_id
        for s in db.query(Shift).filter(
            Shift.group_id == group_id, Shift.type == "휴가"
        )
    }

    # 확정 원티드 일괄 로드 (해당 월)
    pool_ids = {n.nurse_id for n in pool}
    rows = (
        db.query(FixedWantedEntry)
        .filter(
            FixedWantedEntry.group_id == group_id,
            FixedWantedEntry.year == year,
            FixedWantedEntry.month == month,
            FixedWantedEntry.is_applied.is_(True),
        )
        .all()
    )
    off_map: dict[str, set[int]] = {}
    fb_map: dict[str, set[int]] = {}
    for r in rows:
        if r.nurse_id not in pool_ids or not r.shift_date:
            continue
        day = r.shift_date.day
        if r.shift_id in _OFF_SHIFT_CODES:
            off_map.setdefault(r.nurse_id, set()).add(day)
        elif r.shift_id in vacation_codes:
            fb_map.setdefault(r.nurse_id, set()).add(day)

    inputs = [
        NurseInput(
            nurse_id=n.nurse_id,
            grade=n.grade,
            preceptor_id=n.preceptor_id,
            off_days=frozenset(off_map.get(n.nurse_id, set())),
            fb_days=frozenset(fb_map.get(n.nurse_id, set())),
        )
        for n in pool
    ]
    return pool, inputs


def _current_team_ids(pool: list[NurseModel]) -> list[int]:
    """풀의 현재 distinct team_id (None 제외, 정렬)."""
    return sorted({n.team_id for n in pool if n.team_id is not None})


def _map_clusters_to_team_ids(
    clusters: dict[int, list[str]],
    current_team: dict[str, Optional[int]],
    team_ids: list[int],
) -> dict[int, int]:
    """team_idx(0..k-1) → 실제 team_id 매핑을 '현재 소속 중복 최대' 그리디로 결정(churn 최소화)."""
    triples: list[tuple[int, int, int]] = []
    for ci, members in clusters.items():
        cnt: dict[int, int] = {}
        for m in members:
            t = current_team.get(m)
            if t is not None:
                cnt[t] = cnt.get(t, 0) + 1
        for t in team_ids:
            triples.append((cnt.get(t, 0), ci, t))
    triples.sort(reverse=True)  # overlap desc
    result: dict[int, int] = {}
    used_clusters: set[int] = set()
    used_teams: set[int] = set()
    for _ov, ci, t in triples:
        if ci in used_clusters or t in used_teams:
            continue
        result[ci] = t
        used_clusters.add(ci)
        used_teams.add(t)
    # num_teams == len(team_ids) 이므로 전단사. 혹시 남은 cluster 는 잔여 team 으로.
    leftover_teams = [t for t in team_ids if t not in used_teams]
    for ci in clusters:
        if ci not in result and leftover_teams:
            result[ci] = leftover_teams.pop(0)
    return result


def preview_team_classification(
    db: Session, *, group_id: str, year: int, month: int
) -> dict:
    """원티드 기반 팀 분류 미리보기 (read-only, DB 변경 없음).

    Returns: {
        target_month, num_teams, num_pool, num_excluded_night,
        teams: {team_id: [nurse_id]}, changes: [{nurse_id, name, from, to}],
        num_changed, stats: {objective, overlap_total, grade_dev_total},
    }
    """
    pool, inputs = _load_pool_and_inputs(db, group_id, year, month)
    if not pool:
        raise ValueError("분류 대상 간호사가 없습니다.")
    team_ids = _current_team_ids(pool)
    if not team_ids:
        raise ValueError("병동에 설정된 팀(team_id)이 없습니다.")
    num_teams = len(team_ids)

    result = auto_assign_teams(inputs, num_teams=num_teams)
    current_team = {n.nurse_id: n.team_id for n in pool}
    name_map = {n.nurse_id: n.name for n in pool}
    idx_to_team = _map_clusters_to_team_ids(result.teams, current_team, team_ids)

    proposed: dict[int, list[str]] = {}
    changes: list[dict] = []
    for ci, members in result.teams.items():
        tid = idx_to_team[ci]
        proposed[tid] = members
        for nid in members:
            cur = current_team.get(nid)
            if cur != tid:
                changes.append({
                    "nurse_id": nid, "name": name_map.get(nid),
                    "from": cur, "to": tid,
                })

    excluded = [
        {"nurse_id": n.nurse_id, "name": n.name}
        for n in db.query(NurseModel)
        .filter(NurseModel.group_id == group_id, NurseModel.active == 1)
        if (n.is_night_nurse or []) == ["N"]
    ]
    return {
        "target_month": f"{year}-{month:02d}",
        "num_teams": num_teams,
        "num_pool": len(pool),
        "num_excluded_night": len(excluded),
        "excluded_night": excluded,
        "teams": {str(t): members for t, members in proposed.items()},
        "changes": changes,
        "num_changed": len(changes),
        "stats": {
            "objective": result.objective,
            "overlap_total": result.overlap_total,
            "grade_dev_total": result.grade_dev_total,
        },
    }


def apply_team_classification(
    db: Session,
    *,
    group_id: str,
    office_id: str,
    year: int,
    month: int,
    assignments: list[dict],
    note: Optional[str] = None,
) -> dict:
    """승인된 팀 분류를 permanent_change 이벤트로 발행 (대상월 1일 발효).

    assignments: [{nurse_id, team_id}] — 변경 대상만(또는 전체) 전달.
    현재 team_id 와 같으면 스킵. 이벤트 생성만, Nurse 즉시 변경은 flush 가 발효일에.
    Returns: {created, skipped, effective_date}
    """
    effective = date(year, month, 1)
    cur = {
        n.nurse_id: n.team_id
        for n in db.query(NurseModel).filter(NurseModel.group_id == group_id)
    }
    created = 0
    skipped = 0
    for a in assignments:
        nid = a["nurse_id"]
        new_team = a["team_id"]
        if nid not in cur:
            skipped += 1
            continue
        if cur[nid] == new_team:
            skipped += 1
            continue
        create_permanent_change(
            db, nurse_id=nid, group_id=group_id, office_id=office_id,
            start_date=effective, new_team_id=new_team,
            note=note or f"원티드 팀분류 {year}-{month:02d}",
        )
        created += 1
    return {
        "created": created,
        "skipped": skipped,
        "effective_date": effective.isoformat(),
    }
