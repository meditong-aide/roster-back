"""원티드 기반 팀 분류 (옵션 1: 병동 내).

확정 원티드(FixedWantedEntry)의 OFF/연차 겹침으로 팀을 재구성한다.
- 입력: 병동 간호사 + 확정 원티드(OFF=shift_id∈{O,OFF,주}, 연차=Shift.type='휴가')
- 엔진: team_auto_assign.auto_assign_teams (Greedy + 2-opt)
- num_teams: 현재 병동의 distinct team_id 수 유지
- 출력(preview): 제안 팀 + 현재팀 대비 변경 diff + 통계 (read-only)
- 적용(apply): 변경되는 간호사마다 permanent_change 이벤트 생성 → 대상월 1일 발효

N전담(allowed_shifts==['N'])은 팀 배정 풀에서 제외한다.
참조: docs/NURSE_GROUP_CHANGE_MODEL.md (옵션1), team_auto_assign.py.
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from sqlalchemy.orm import Session

from db.models import FixedWantedEntry, Nurse as NurseModel, Shift, Team as TeamModel
from services.assignment_service import create_permanent_change
from services.team_period import resolve_teams_for_month, set_team_period
from services.team_auto_assign import NurseInput, auto_assign_teams

_OFF_SHIFT_CODES = frozenset({"O", "OFF", "주"})


def _is_night_only(n: NurseModel) -> bool:
    return (n.allowed_shifts or []) == ["N"]


def _build_pool_roster(
    nurses: list[NurseModel], include_group: bool = False
) -> list[dict]:
    """참여 선택 로스터 — 간호사별 grade/프리셉터/근무코드.

    shift: allowed_shifts []=전체, 그 외 근무코드(예: 'N', 'D E').
    preceptor_name: 프리셉티면 그 프리셉터 이름(같은 풀 내).
    is_preceptor: 이 간호사를 프리셉터로 둔 프리셉티가 풀에 있으면 True.
    옵션1/2 공용 — HN 이 /nurses 로 타 병동 nurse 를 못 읽어 preview 가 운반한다.
    """
    name_by = {n.nurse_id: n.name for n in nurses}
    preceptor_ids = {n.preceptor_id for n in nurses if n.preceptor_id}

    def _shift(n: NurseModel) -> str:
        s = n.allowed_shifts or []
        return "전체" if len(s) == 0 else " ".join(s)

    out: list[dict] = []
    for n in nurses:
        m = {
            "nurse_id": n.nurse_id,
            "name": n.name,
            "grade": n.grade,
            "shift": _shift(n),
            "is_night": (n.allowed_shifts or []) == ["N"],
            "preceptor_name": name_by.get(n.preceptor_id) if n.preceptor_id else None,
            "is_preceptor": n.nurse_id in preceptor_ids,
        }
        if include_group:
            m["group_id"] = n.group_id
        out.append(m)
    return out


def _load_pool_and_inputs(
    db: Session,
    group_id: str,
    year: int,
    month: int,
    participant_ids: Optional[list[str]] = None,
    pair_decisions: Optional[dict[str, str]] = None,
) -> tuple[list[NurseModel], list[NurseInput], list[NurseModel], list[NurseModel]]:
    """참여 풀 + NurseInput + 미지정(미참여 적격) + 제외(N전담) 구성.

    participant_ids 미지정 → 비 N전담 전원 참여(전체 재편), 미지정 없음.
    participant_ids 지정 → 그 집합만 참여(클러스터링), 나머지 적격은 '미지정',
      N전담도 participant_ids 에 있으면 참여(override, apply 시 해제 동반).
    off_days = 확정 원티드 OFF, fb_days = 확정 원티드 연차(휴가 type).
    """
    nurses = (
        db.query(NurseModel)
        .filter(NurseModel.group_id == group_id, NurseModel.active == 1)
        .all()
    )
    if participant_ids is None:
        pool = [n for n in nurses if not _is_night_only(n)]
        unassigned: list[NurseModel] = []
        excluded_night = [n for n in nurses if _is_night_only(n)]
    else:
        pset = set(participant_ids)
        pool = [n for n in nurses if n.nurse_id in pset]
        unassigned = [
            n for n in nurses
            if n.nurse_id not in pset and not _is_night_only(n)
        ]
        excluded_night = [
            n for n in nurses if _is_night_only(n) and n.nurse_id not in pset
        ]

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
            # MSSQL BIT 컬럼은 IS 1 구문 오류 → '= 1' 로 비교 (.is_(True) 금지)
            FixedWantedEntry.is_applied == True,  # noqa: E712
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

    # 해제(release)로 표시된 프리셉티는 강제 follow 대상에서 제외(preceptor_id 무시)
    released = {
        pid for pid, d in (pair_decisions or {}).items() if d == "release"
    }
    inputs = [
        NurseInput(
            nurse_id=n.nurse_id,
            grade=n.grade,
            preceptor_id=None if n.nurse_id in released else n.preceptor_id,
            off_days=frozenset(off_map.get(n.nurse_id, set())),
            fb_days=frozenset(fb_map.get(n.nurse_id, set())),
        )
        for n in pool
    ]
    return pool, inputs, unassigned, excluded_night


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


def _detect_pairs(
    all_nurses: list[NurseModel],
    pool_ids: set[str],
    nurse_to_team: dict[str, Optional[int]],
    pair_decisions: Optional[dict[str, str]],
) -> list[dict]:
    """현재 병동의 프리셉티→프리셉터 짝 + 분류 후 상태.

    status: released(해제 선택) / split(한쪽 미참여 또는 다른 팀) / together(같은 팀).
    split 은 경고 대상 — 짝이 갈라짐.
    """
    decisions = pair_decisions or {}
    name = {n.nurse_id: n.name for n in all_nurses}
    pairs: list[dict] = []
    for n in all_nurses:
        if not n.preceptor_id or n.preceptor_id not in name:
            continue  # preceptor 가 같은 병동 활성 간호사가 아니면 짝으로 보지 않음
        pe, pr = n.nurse_id, n.preceptor_id
        decision = decisions.get(pe, "keep")
        if decision == "release":
            status = "released"
        elif not (pe in pool_ids and pr in pool_ids):
            status = "split"  # 한쪽이라도 미참여 → 갈라짐
        elif nurse_to_team.get(pe) != nurse_to_team.get(pr):
            status = "split"
        else:
            status = "together"
        pairs.append({
            "preceptee_id": pe, "preceptee_name": name.get(pe),
            "preceptor_id": pr, "preceptor_name": name.get(pr),
            "decision": decision, "status": status,
        })
    return pairs


def preview_team_classification(
    db: Session,
    *,
    group_id: str,
    year: int,
    month: int,
    participant_ids: Optional[list[str]] = None,
    pair_decisions: Optional[dict[str, str]] = None,
) -> dict:
    """원티드 기반 팀 분류 미리보기 (read-only, DB 변경 없음).

    participant_ids 지정 시 그 집합만 팀에 배치, 미참여 적격은 '미지정'.
    N전담은 기본 제외(권장)이나 participant_ids 에 포함하면 참여(apply 시 해제 동반).
    pair_decisions {preceptee_id: "keep"|"release"} — release 면 프리셉티가 프리셉터를
    따라가지 않고 독립 분류(apply 시 프리셉터십 해제). 갈라지는 짝은 warnings 로 안내.
    """
    pool, inputs, unassigned, excluded_night = _load_pool_and_inputs(
        db, group_id, year, month, participant_ids, pair_decisions
    )
    if not pool:
        raise ValueError("분류(참여) 대상 간호사가 없습니다.")
    # 팀 목록은 그룹 전체 기준 (참여자만으로 좁히지 않음)
    # 팀 정의는 teams 테이블(정의된 팀)이 권위 — nurses.team_id(캐시)로 세지 않는다.
    #   (ward_redistribute 와 동일 원칙. nurses.team_id 일괄 NULL 이행 후에도 동작.)
    team_ids = sorted({
        int(t) for (t,) in db.query(TeamModel.team_id)
        .filter(TeamModel.group_id == group_id, TeamModel.active == 1)
        .distinct()
        if t is not None
    })
    if not team_ids:
        raise ValueError("병동에 설정된 팀(team_id)이 없습니다.")
    num_teams = len(team_ids)
    g1_count = sum(1 for n in pool if n.grade == 1)
    if g1_count < num_teams:
        raise ValueError(
            f"참여 인원 중 시니어(G1) {g1_count}명 < 팀 수 {num_teams}. "
            f"각 팀에 시니어가 가도록 참여 인원을 조정하세요."
        )

    result = auto_assign_teams(inputs, num_teams=num_teams)
    # 현재 팀 = 시점(period) 기준 (NULL 이행 후에도 정확). 클러스터→팀 라벨 안정화에 사용.
    _team_now = resolve_teams_for_month(db, group_id, date(year, month, 1))
    current_team = {n.nurse_id: _team_now.get(str(n.nurse_id)) for n in pool}
    name_map = {n.nurse_id: n.name for n in pool}
    idx_to_team = _map_clusters_to_team_ids(result.teams, current_team, team_ids)

    proposed: dict[int, list[str]] = {}
    changes: list[dict] = []
    nurse_to_team: dict[str, Optional[int]] = {}
    for ci, members in result.teams.items():
        tid = idx_to_team[ci]
        proposed[tid] = members
        for nid in members:
            nurse_to_team[nid] = tid
            cur = current_team.get(nid)
            if cur != tid:
                changes.append({
                    "nurse_id": nid, "name": name_map.get(nid),
                    "from": cur, "to": tid,
                })

    # 프리셉터 짝 탐지 — 전체 활성 = 풀 + 미지정 + N전담제외
    all_nurses = pool + unassigned + excluded_night
    pool_ids = {n.nurse_id for n in pool}
    pairs = _detect_pairs(all_nurses, pool_ids, nurse_to_team, pair_decisions)

    # 참여 선택 로스터 — 전체 활성 간호사 + grade/프리셉터/근무코드.
    # 프론트가 group_id 로 nurse 를 직접 못 읽는 경우(HN 권한)가 있어 preview 가 운반.
    pool_roster = _build_pool_roster(all_nurses)
    warnings: list[str] = []
    for p in pairs:
        if p["status"] == "split":
            warnings.append(
                f"프리셉티 {p['preceptee_name']}–프리셉터 {p['preceptor_name']} 짝이 "
                f"갈라집니다. 유지하려면 둘 다 참여시키고, 끊으려면 '해제'를 선택하세요."
            )

    return {
        "target_month": f"{year}-{month:02d}",
        "num_teams": num_teams,
        "num_pool": len(pool),
        "num_excluded_night": len(excluded_night),
        "excluded_night": [
            {"nurse_id": n.nurse_id, "name": n.name} for n in excluded_night
        ],
        "unassigned": [
            {"nurse_id": n.nurse_id, "name": n.name} for n in unassigned
        ],
        "teams": {str(t): members for t, members in proposed.items()},
        "changes": changes,
        "num_changed": len(changes),
        "pairs": pairs,
        "pool_roster": pool_roster,
        "warnings": warnings,
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
    pair_decisions: Optional[dict[str, str]] = None,
    note: Optional[str] = None,
) -> dict:
    """승인된 팀 분류를 permanent_change 이벤트로 발행 (대상월 1일 발효).

    assignments: [{nurse_id, team_id}] — 변경 대상만(또는 전체) 전달.
    현재 team_id 와 같으면 스킵. 이벤트 생성만, Nurse 즉시 변경은 flush 가 발효일에.
    pair_decisions {preceptee_id: "release"} — 해당 프리셉티의 프리셉터십 종료(발효일).
      팀변경과 같은 nurse 면 한 이벤트로 묶고, 아니면 해제 전용 이벤트를 추가 발행.
    Returns: {created, skipped, released, effective_date}

    주의: nurses.team_id 는 프로덕션에서 varchar(문자열), 제안 team_id 는 int 일 수 있어
    비교는 반드시 str() 정규화로 한다 (안 그러면 무변경도 이벤트 생성됨).
    """
    effective = date(year, month, 1)
    cur_nurses = {
        n.nurse_id: n
        for n in db.query(NurseModel).filter(NurseModel.group_id == group_id)
    }
    released = {pid for pid, d in (pair_decisions or {}).items() if d == "release"}

    def _same(a, b) -> bool:
        if a is None or b is None:
            return a is None and b is None
        return str(a) == str(b)

    created = 0
    skipped = 0
    handled_release: set[str] = set()
    _note = note or f"원티드 팀분류 {year}-{month:02d}"
    # 현재 팀 = 시점(period) 기준 — raw nurse.team_id 는 period 기록 후 갱신 안 돼 재실행 시
    #   skip 실패(non-idempotent). period 로 정확·멱등 비교(NULL 이행 후에도 동작).
    _team_now = resolve_teams_for_month(db, group_id, effective)
    for a in assignments:
        nid = a["nurse_id"]
        new_team = a["team_id"]
        nurse = cur_nurses.get(nid)
        if nurse is None:
            skipped += 1
            continue
        # N전담을 팀에 편입하면 N전담 해제 동반(allowed_shifts=[]) — 발효일 기반
        is_n_override = (nurse.allowed_shifts or []) == ["N"]
        rel = nid in released
        if _same(_team_now.get(str(nid)), new_team) and not is_n_override and not rel:
            skipped += 1
            continue
        create_permanent_change(
            db, nurse_id=nid, group_id=group_id, office_id=office_id,
            start_date=effective, new_team_id=new_team,
            new_shift_types=[] if is_n_override else None,
            release_preceptor=rel,
            note=_note,
        )
        # [B3] 재분배(ward_redistribute)와 동일: 팀을 시점 테이블(nurse_team_period)
        #   에도 기록 → flush(발효) 전에도 화면(resolve_team)·생성기(resolve_team_for_roster)
        #   에 새 팀이 즉시 반영된다. team 미지정(None)은 기록하지 않음(기존 거동 유지).
        if new_team is not None:
            set_team_period(
                db, nurse_id=nid, group_id=group_id, valid_from=effective,
                team_id=new_team, source="classify", note=_note,
            )
        if rel:
            handled_release.add(nid)
        created += 1

    # assignments 에 없던 해제 대상 → 해제 전용 이벤트
    for nid in released - handled_release:
        if cur_nurses.get(nid) is None:
            continue
        create_permanent_change(
            db, nurse_id=nid, group_id=group_id, office_id=office_id,
            start_date=effective, release_preceptor=True, note=_note,
        )
        handled_release.add(nid)
        created += 1

    return {
        "created": created,
        "skipped": skipped,
        "released": len(handled_release),
        "effective_date": effective.isoformat(),
    }
