"""원티드 기반 병동 간 재분배 (옵션 2: 특정 N개 병동 풀).

관리자가 관리하는 N개 병동의 간호사를 한 풀로 모아, 확정 원티드 OFF/연차 겹침으로
클러스터링한 뒤 각 클러스터를 병동에 매핑한다. 병동이 바뀌는 간호사는 병동이동(transfer)
이벤트로 발행한다 (group_id 변경 — 옵션1의 team_id 변경과 구분).

- 풀: 관리 병동들(resolve_managed_group_ids로 산출, 엔드포인트에서 주입)
- 정원: 균등 분할(총원/N), 약간의 tolerance
- 엔진: team_auto_assign.auto_assign_teams (num_teams = 병동 수)
- preview(read-only): 제안 병동 + 현재병동 대비 이동 diff + 통계
- apply: 병동이 바뀌는 간호사 → 병동이동(transfer) 이벤트 (Phase B)

N전담(is_night_nurse==['N'])은 풀에서 제외.
참조: docs/NURSE_GROUP_CHANGE_MODEL.md (옵션2), team_classify_service.py (옵션1).
"""

from __future__ import annotations

from datetime import date
from math import ceil
from typing import Optional

from sqlalchemy.orm import Session

from db.models import (
    FixedWantedEntry, Group, Nurse as NurseModel, NurseAssignment, Shift,
)
from schemas.roster_schema import NurseAssignmentCreate
from services.assignment_service import create_assignment, create_permanent_change
from services.team_auto_assign import NurseInput, auto_assign_teams
from services.team_classify_service import _build_pool_roster

_OFF_SHIFT_CODES = frozenset({"O", "OFF", "주"})


def _load_pool(
    db: Session, group_ids: list[str], year: int, month: int,
    participant_ids: Optional[list[str]] = None,
) -> tuple[
    list[NurseModel], list[NurseInput], dict[str, str],
    list[NurseModel], list[NurseModel], set[str],
]:
    """풀(N병동) 간호사 + NurseInput + nurse_id→현재 group_id 맵 + 제외/미참여.

    off_days=확정 원티드 OFF, fb_days=확정 원티드 연차(휴가 type).
    원티드는 각 간호사의 현재 소속 병동 기준으로 조회된다.

    participant_ids:
      - None → 비 N전담·비겹침 전원 재분배(미참여 없음).
      - 지정 → 그 집합만 자유 이동, 나머지 적격자는 풀에 포함하되 '미참여'(현재 병동 고정).
        N전담은 기본 제외이나 participant_ids 에 있으면 참여(override, apply 시 해제).

    옵션1과 달리 미참여를 '미지정'으로 빼지 않고 현재 병동에 고정하는 이유:
      (1) group_id(병동)는 null 불가 — 간호사는 반드시 어떤 병동에 속한다. team_id 는
          null 가능이라 옵션1은 '미지정'이 성립하지만 병동은 아니다.
      (2) 정원 점유 — 잔류자를 풀에서 빼면 정원 계산이 틀어져 참여자가 이미 찬 병동에
          과배치된다. 풀에 두되 fixed 로 핀 고정해 자리는 점유하되 못 움직이게 한다.

    Returns: (pool, inputs, current_group, excluded_night, excluded_overlap,
              non_participant_ids) — non_participant_ids 는 풀에 있으나 현재 병동에
              고정될(앵커) 대상. participant_ids None 이면 빈 set.
    """
    pset: Optional[set[str]] = set(participant_ids) if participant_ids is not None else None
    nurses = (
        db.query(NurseModel)
        .filter(NurseModel.group_id.in_(group_ids), NurseModel.active == 1)
        .all()
    )
    if pset is None:
        night = [n for n in nurses if (n.is_night_nurse or []) == ["N"]]
    else:
        # 참여로 명시된 N전담은 풀에 포함(override) — 나머지 N전담만 제외
        night = [
            n for n in nurses
            if (n.is_night_nurse or []) == ["N"] and n.nurse_id not in pset
        ]

    # 기간 겹침 제외: 대상 발효일(month-1)부터와 겹치는 active 파견/병동이동 보유자.
    # (새 transfer 는 발효일부터 open-ended → 기존 presence 의 effective_end 가
    #  None 또는 >= 발효일이면 겹쳐서 transfer 생성 불가 → 재분배 대상에서 제외)
    target_date = date(year, month, 1)
    all_ids = [n.nurse_id for n in nurses]
    overlap_ids: set[str] = set()
    if all_ids:
        for a in db.query(NurseAssignment).filter(
            NurseAssignment.nurse_id.in_(all_ids),
            NurseAssignment.status == "active",
            NurseAssignment.reason.in_(["파견", "병동이동"]),
        ):
            eff_end = a.end_date or a.expected_end_date
            if eff_end is None or eff_end >= target_date:
                overlap_ids.add(a.nurse_id)

    night_ids = {n.nurse_id for n in night}
    pool = [
        n for n in nurses
        if n.nurse_id not in night_ids and n.nurse_id not in overlap_ids
    ]
    excluded_overlap = [n for n in nurses if n.nurse_id in overlap_ids]
    current_group = {n.nurse_id: n.group_id for n in pool}

    # 미참여 = 풀에 있으나 참여로 선택되지 않은 적격자 → 현재 병동에 고정(앵커)
    if pset is None:
        non_participant_ids: set[str] = set()
    else:
        non_participant_ids = {n.nurse_id for n in pool if n.nurse_id not in pset}

    # 휴가 type shift_id 집합 (풀 병동 전체)
    vacation_codes = {
        s.shift_id
        for s in db.query(Shift).filter(
            Shift.group_id.in_(group_ids), Shift.type == "휴가"
        )
    }

    pool_ids = set(current_group.keys())
    rows = (
        db.query(FixedWantedEntry)
        .filter(
            FixedWantedEntry.group_id.in_(group_ids),
            FixedWantedEntry.year == year,
            FixedWantedEntry.month == month,
            # MSSQL BIT 컬럼은 IS 1 구문오류 → '= 1' 로 비교
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

    # 강제 follow 는 '참여자끼리'만 (옵션1과 동일 규칙). 프리셉티나 그 프리셉터가
    # fixed(미참여) 면 follow 를 끊어 fixed 핀이 끌려가지 않게 한다.
    def _eff_preceptor(n: NurseModel):
        if n.preceptor_id is None:
            return None
        if n.nurse_id in non_participant_ids or n.preceptor_id in non_participant_ids:
            return None
        return n.preceptor_id

    inputs = [
        NurseInput(
            nurse_id=n.nurse_id,
            grade=n.grade,
            preceptor_id=_eff_preceptor(n),
            off_days=frozenset(off_map.get(n.nurse_id, set())),
            fb_days=frozenset(fb_map.get(n.nurse_id, set())),
        )
        for n in pool
    ]
    return pool, inputs, current_group, night, excluded_overlap, non_participant_ids


def _map_clusters_to_wards(
    clusters: dict[int, list[str]],
    current_group: dict[str, str],
    ward_ids: list[str],
) -> dict[int, str]:
    """cluster_idx → 실제 group_id 매핑을 '현재 소속 중복 최대' 그리디로 결정(이동 최소화)."""
    triples: list[tuple[int, int, str]] = []
    for ci, members in clusters.items():
        cnt: dict[str, int] = {}
        for m in members:
            g = current_group.get(m)
            if g is not None:
                cnt[g] = cnt.get(g, 0) + 1
        for w in ward_ids:
            triples.append((cnt.get(w, 0), ci, w))
    triples.sort(key=lambda t: t[0], reverse=True)
    result: dict[int, str] = {}
    used_clusters: set[int] = set()
    used_wards: set[str] = set()
    for _ov, ci, w in triples:
        if ci in used_clusters or w in used_wards:
            continue
        result[ci] = w
        used_clusters.add(ci)
        used_wards.add(w)
    leftover = [w for w in ward_ids if w not in used_wards]
    for ci in clusters:
        if ci not in result and leftover:
            result[ci] = leftover.pop(0)
    return result


class WardSetupError(ValueError):
    """병동 사전 설정 누락(예: G1 미지정). 프론트가 설정 유도하도록 ward 목록을 담는다."""

    def __init__(self, message: str, wards: list[dict]):
        super().__init__(message)
        self.wards = wards  # [{group_id, name}]


def _wards_missing_g1(
    pool: list[NurseModel], current_group: dict[str, str], ward_ids: list[str],
    name_map: dict[str, str],
) -> list[dict]:
    """풀 기준 grade-1(시니어)이 한 명도 없는 병동 목록. (N전담은 풀에서 이미 제외됨)"""
    has_g1 = {w: False for w in ward_ids}
    for n in pool:
        if n.grade == 1 and current_group.get(n.nurse_id) in has_g1:
            has_g1[current_group[n.nurse_id]] = True
    return [{"group_id": w, "name": name_map.get(w)} for w in ward_ids if not has_g1[w]]


def _ward_seed(
    pool: list[NurseModel], current_group: dict[str, str], ward: str,
    non_participant_ids: set[str],
) -> str:
    """병동의 grade-1 앵커 시드 1명. 미참여(고정) G1 을 우선 — 참여자는 이동 자유를 유지.

    (_wards_missing_g1 통과 전제 → 각 병동에 G1 ≥ 1 보장)
    """
    cands = [n.nurse_id for n in pool
             if current_group.get(n.nurse_id) == ward and n.grade == 1]
    for nid in cands:
        if nid in non_participant_ids:
            return nid
    return cands[0]


def _team_breakdown(
    db: Session,
    ward_id: str,
    member_ids: list[str],
    by_input: dict[str, NurseInput],
    nurse_name: dict[str, str],
) -> dict[str, list[dict]]:
    """재분배된 병동 안에서 팀(team_id) 분해 — 옵션1 로직 재사용.

    팀 수 = 병동의 현재 distinct team_id 수. G1 부족 등으로 분해 불가 시 단일 '전체' 폴백.
    """
    members = [by_input[nid] for nid in member_ids if nid in by_input]
    team_ids = sorted({
        t for (t,) in db.query(NurseModel.team_id)
        .filter(NurseModel.group_id == ward_id, NurseModel.team_id.isnot(None))
        .distinct()
    })
    k = len(team_ids) if team_ids else 1

    def _flat(ms):
        return [{"nurse_id": m.nurse_id, "name": nurse_name.get(m.nurse_id)} for m in ms]

    if k <= 1 or len(members) < 2:
        return {"전체": _flat(members)}
    try:
        res = auto_assign_teams(members, num_teams=k)
        out: dict[str, list[dict]] = {}
        for i, ids in res.teams.items():
            label = str(team_ids[i]) if i < len(team_ids) else f"팀{i + 1}"
            out[label] = [{"nurse_id": nid, "name": nurse_name.get(nid)} for nid in ids]
        return out
    except ValueError:
        return {"전체": _flat(members)}


def _detect_ward_pairs(
    all_nurses: list[NurseModel],
    nurse_to_ward: dict[str, Optional[str]],
) -> list[dict]:
    """원래 같은 병동이던 프리셉티→프리셉터 짝의 재분배 후 상태.

    status: together(결과 같은 병동) / split(다른 병동).
    moved: 둘 중 하나라도 병동이 바뀌면 True — 병동이동 발효 시 프리셉터십이 자동
      종료되므로(_apply_target_profile_reset) together 라도 관계는 끊긴다.
    """
    name = {n.nurse_id: n.name for n in all_nurses}
    cur = {n.nurse_id: n.group_id for n in all_nurses}
    pairs: list[dict] = []
    for n in all_nurses:
        if not n.preceptor_id or n.preceptor_id not in name:
            continue
        if cur.get(n.nurse_id) != cur.get(n.preceptor_id):
            continue  # 원래 같은 병동이던 짝만 대상
        pe, pr = n.nurse_id, n.preceptor_id
        pe_w, pr_w = nurse_to_ward.get(pe), nurse_to_ward.get(pr)
        status = "together" if (pe_w is not None and pe_w == pr_w) else "split"
        moved = (pe_w is not None and pe_w != cur.get(pe)) or \
                (pr_w is not None and pr_w != cur.get(pr))
        pairs.append({
            "preceptee_id": pe, "preceptee_name": name.get(pe),
            "preceptor_id": pr, "preceptor_name": name.get(pr),
            "preceptee_to": pe_w, "preceptor_to": pr_w,
            "status": status, "moved": moved,
        })
    return pairs


def preview_ward_redistribution(
    db: Session,
    *,
    group_ids: list[str],
    year: int,
    month: int,
    capacity_mode: str = "even",
    target_sizes: Optional[dict[str, int]] = None,
    size_tolerance: int = 2,
    churn_weight: float = 500.0,
    participant_ids: Optional[list[str]] = None,
) -> dict:
    """병동 간 재분배 미리보기 (read-only, DB 변경 없음).

    capacity_mode:
      - "even": 균등분할(총원/N) ± tolerance
      - "explicit": 그룹별 목표 인원(target_sizes) ± tolerance.
        시드는 각 그룹의 현재 grade-1 1명으로 고정(cluster i ↔ ward i), 정원 밴드 적용.
        풀 인원이 [Σmin, Σmax] 안에 들어야 함(여유 흡수).

    participant_ids 지정 시 '선택 재분배': 그 집합만 자유 이동, 미참여 적격자는 현재
    병동에 고정(fixed). 고정을 위해 cluster↔ward 를 결정적으로 묶어야 하므로 even/explicit
    공통으로 병동별 G1 앵커 시드를 사용한다(앵커 모드).

    Returns: {target_month, ward_ids, num_wards, num_pool, num_participants,
              num_excluded_night, num_excluded_overlap, fixed_stay, capacity_mode,
              size_bounds, warnings, wards, moves, num_moved, stats}
    """
    ward_ids = sorted(set(group_ids))
    if len(ward_ids) < 2:
        raise ValueError("재분배는 2개 이상의 병동이 필요합니다.")
    pool, inputs, current_group, night, excluded_overlap, non_participant_ids = (
        _load_pool(db, ward_ids, year, month, participant_ids)
    )
    if not pool:
        raise ValueError("재분배 대상 간호사가 없습니다.")
    anchored = participant_ids is not None
    num_participants = len(pool) - len(non_participant_ids)
    if anchored and num_participants < 1:
        raise ValueError("참여(이동 대상) 인원이 없습니다. 한 명 이상 선택하세요.")

    num_wards = len(ward_ids)
    total = len(pool)
    warnings: list[str] = []

    name_map = {g.group_id: g.group_name for g in
                db.query(Group).filter(Group.group_id.in_(ward_ids))}

    # 사전 검증: 모든 선택 병동에 시니어(G1)가 있어야 함.
    # 없으면 alg가 차출로 얼버무리지 않고 막아, 프론트가 '시니어 지정'을 유도하게 한다.
    missing_g1 = _wards_missing_g1(pool, current_group, ward_ids, name_map)
    if missing_g1:
        names = ", ".join(w["name"] or w["group_id"] for w in missing_g1)
        raise WardSetupError(
            f"다음 병동에 시니어(grade-1)가 지정되어 있지 않습니다: {names}. "
            f"재분배 전에 각 병동에 시니어를 먼저 지정하세요.",
            wards=missing_g1,
        )

    # 역할 혼합 경고 (AN/RN 등 직역이 섞이면 운영상 위험 — 선택이 곧 안전장치이나 경고)
    roles = {getattr(n, "role", None) for n in pool}
    roles.discard(None)
    if len(roles) > 1:
        warnings.append(
            f"선택 그룹에 역할이 섞여 있습니다({sorted(roles)}). 같은 직역끼리 재분배를 권장합니다."
        )

    # cluster i ↔ ward_ids[i] 결정적 바인딩 (explicit 또는 앵커 모드에서 사용).
    # 미참여자는 현재 병동 클러스터에 고정(fixed) → 참여자만 자유 이동.
    ward_index = {w: i for i, w in enumerate(ward_ids)}
    home_cluster = {
        nid: ward_index[current_group[nid]]
        for nid in current_group if current_group[nid] in ward_index
    }
    fixed = (
        {nid: ward_index[current_group[nid]] for nid in non_participant_ids}
        if anchored else None
    )

    if capacity_mode == "explicit":
        if not target_sizes:
            raise ValueError("explicit 모드는 target_sizes(그룹별 목표 인원)가 필요합니다.")
        missing = [w for w in ward_ids if w not in target_sizes]
        if missing:
            raise ValueError(f"target_sizes 에 누락된 그룹: {missing}")
        # 각 그룹의 현재 grade-1 1명을 앵커 시드로 (미참여 G1 우선).
        # 사전 검증(_wards_missing_g1)을 통과했으므로 모든 병동에 G1이 보장된다.
        seeds = [_ward_seed(pool, current_group, w, non_participant_ids) for w in ward_ids]
        min_sizes = [max(1, target_sizes[w] - size_tolerance) for w in ward_ids]
        max_sizes = [target_sizes[w] + size_tolerance for w in ward_ids]
        if not (sum(min_sizes) <= total <= sum(max_sizes)):
            raise ValueError(
                f"정원 밴드로 풀({total}명)을 담을 수 없습니다. "
                f"Σmin={sum(min_sizes)}, Σmax={sum(max_sizes)}, 목표합={sum(target_sizes.values())}. "
                f"인원 또는 허용치(±{size_tolerance})를 조정하세요."
            )
        result = auto_assign_teams(
            inputs, seed_ids=seeds, min_sizes=min_sizes, max_sizes=max_sizes,
            home_cluster=home_cluster, w_churn=churn_weight, fixed=fixed,
        )
        idx_to_ward = {i: ward_ids[i] for i in range(num_wards)}
        size_bounds = {"mode": "explicit", "tolerance": size_tolerance,
                       "targets": {w: target_sizes[w] for w in ward_ids}}
    elif anchored:  # even 분할이되 cluster↔ward 결정적 바인딩(미참여 고정 위해)
        avg = total // num_wards
        min_size = max(1, avg - size_tolerance)
        max_size = ceil(total / num_wards) + size_tolerance
        seeds = [_ward_seed(pool, current_group, w, non_participant_ids) for w in ward_ids]
        result = auto_assign_teams(
            inputs, seed_ids=seeds, min_size=min_size, max_size=max_size,
            home_cluster=home_cluster, w_churn=churn_weight, fixed=fixed,
        )
        idx_to_ward = {i: ward_ids[i] for i in range(num_wards)}
        size_bounds = {"mode": "even-anchored", "min": min_size, "max": max_size, "avg": avg}
    else:  # even, 전원 재분배 (cluster→ward 사후 매핑)
        avg = total // num_wards
        min_size = max(1, avg - size_tolerance)
        max_size = ceil(total / num_wards) + size_tolerance
        result = auto_assign_teams(
            inputs, num_teams=num_wards, min_size=min_size, max_size=max_size
        )
        idx_to_ward = _map_clusters_to_wards(result.teams, current_group, ward_ids)
        size_bounds = {"mode": "even", "min": min_size, "max": max_size, "avg": avg}

    nurse_name = {n.nurse_id: n.name for n in pool}
    by_input = {n.nurse_id: n for n in inputs}

    wards: dict[str, dict] = {}
    moves: list[dict] = []
    for ci, members in result.teams.items():
        wid = idx_to_ward[ci]
        wards[wid] = {
            "name": name_map.get(wid),
            "nurse_ids": members,
            "teams": _team_breakdown(db, wid, members, by_input, nurse_name),
        }
        for nid in members:
            cur = current_group.get(nid)
            if cur != wid:
                moves.append({
                    "nurse_id": nid, "name": nurse_name.get(nid),
                    "from": cur, "from_name": name_map.get(cur),
                    "to": wid, "to_name": name_map.get(wid),
                })

    # 프리셉터 짝 탐지/경고 — 병동을 넘으면 발효 시 프리셉터십 자동 종료
    nurse_to_ward: dict[str, Optional[str]] = {}
    for wid, w in wards.items():
        for nid in w["nurse_ids"]:
            nurse_to_ward[nid] = wid
    pairs = _detect_ward_pairs(pool + night + excluded_overlap, nurse_to_ward)
    for p in pairs:
        if p["status"] == "split":
            warnings.append(
                f"프리셉티 {p['preceptee_name']}–프리셉터 {p['preceptor_name']} 짝이 "
                f"다른 병동으로 갈라집니다(병동이동 발효 시 프리셉터십 자동 종료)."
            )
        elif p["moved"]:
            warnings.append(
                f"프리셉티 {p['preceptee_name']}–프리셉터 {p['preceptor_name']}는 함께 "
                f"이동하지만, 병동이동 발효 시 프리셉터십이 자동 종료됩니다."
            )

    excluded = [
        {"nurse_id": n.nurse_id, "name": n.name, "group_id": n.group_id}
        for n in night
    ]
    excluded_overlap_out = [
        {"nurse_id": n.nurse_id, "name": n.name, "group_id": n.group_id}
        for n in excluded_overlap
    ]
    # 미참여(현재 병동 고정) — 풀에 있으나 이동 대상이 아닌 적격자
    name_all = {n.nurse_id: n.name for n in pool}
    fixed_stay = [
        {"nurse_id": nid, "name": name_all.get(nid),
         "group_id": current_group.get(nid),
         "group_name": name_map.get(current_group.get(nid))}
        for nid in sorted(non_participant_ids)
    ]
    return {
        "target_month": f"{year}-{month:02d}",
        "ward_ids": ward_ids,
        "num_wards": num_wards,
        "num_pool": total,
        "num_participants": num_participants,
        "num_excluded_night": len(excluded),
        "excluded_night": excluded,
        "num_excluded_overlap": len(excluded_overlap_out),
        "excluded_overlap": excluded_overlap_out,
        "num_fixed_stay": len(fixed_stay),
        "fixed_stay": fixed_stay,
        "capacity_mode": capacity_mode,
        "size_bounds": size_bounds,
        "warnings": warnings,
        "wards": {
            wid: {"name": w["name"], "size": len(w["nurse_ids"]),
                  "nurse_ids": w["nurse_ids"], "teams": w["teams"]}
            for wid, w in wards.items()
        },
        "moves": moves,
        "num_moved": len(moves),
        "pairs": pairs,
        "pool_roster": _build_pool_roster(pool, include_group=True),
        "stats": {
            "objective": result.objective,
            "overlap_total": result.overlap_total,
            "grade_dev_total": result.grade_dev_total,
        },
    }


def _same_team(a, b) -> bool:
    """team_id 비교 — varchar/int 혼재 대비 str() 정규화."""
    if a is None or b is None:
        return a is None and b is None
    return str(a) == str(b)


def apply_ward_redistribution(
    db: Session,
    *,
    group_ids: list[str],
    year: int,
    month: int,
    assignments: list[dict],
    current_user=None,
    note: Optional[str] = None,
) -> dict:
    """승인된 병동 간 재분배를 이벤트로 발행 (대상월 1일 발효).

    assignments: [{nurse_id, to_group_id, team_id?}] — team_id 는 정수만 적용(없으면 무시).
      - 병동이 바뀌면 → 병동이동(transfer) 이벤트(target_team_id 동반). 기존 create_assignment
        흐름 재사용(FixedWanted 재배치·정합성·알림 포함).
      - 같은 병동인데 team_id 만 바뀌면 → permanent_change(team_id).
      - 둘 다 동일 → skip.
    Returns: {transfers, team_changes, skipped, failed, effective_date}

    per-nurse graceful: 한 명이 기간겹침(409) 등으로 실패해도 그 사람만 건너뛰고
    failed 에 사유와 함께 담는다 (배치 전체가 중단되지 않음).
    """
    effective = date(year, month, 1)
    nurses = {
        n.nurse_id: n
        for n in db.query(NurseModel).filter(NurseModel.group_id.in_(group_ids))
    }
    _note = note or f"원티드 병동재분배 {year}-{month:02d}"
    transfers = team_changes = skipped = 0
    failed: list[dict] = []
    for a in assignments:
        nid = a.get("nurse_id")
        to_g = a.get("to_group_id")
        team = a.get("team_id")
        team = team if isinstance(team, int) else None  # '전체'/None 등은 팀 미적용
        n = nurses.get(nid)
        if n is None or not to_g:
            skipped += 1
            continue
        cur_g = n.group_id
        try:
            if cur_g != to_g:
                req = NurseAssignmentCreate(
                    nurse_id=nid, source_group_id=cur_g, target_group_id=to_g,
                    office_id=n.office_id, start_date=effective, reason="병동이동",
                    target_team_id=team, note=_note,
                )
                create_assignment(req, db, current_user)
                transfers += 1
            elif team is not None and not _same_team(n.team_id, team):
                create_permanent_change(
                    db, nurse_id=nid, group_id=cur_g, office_id=n.office_id,
                    start_date=effective, new_team_id=team, note=_note,
                )
                team_changes += 1
            else:
                skipped += 1
        except Exception as e:  # 기간겹침(409) 등 — 그 사람만 스킵
            # create_assignment 의 검증(_raise_if_overlap 등)은 db.add 이전에 raise 하므로
            # 부분 커밋이 없다 → 세션 무효화(rollback) 없이 다음 사람 진행.
            detail = getattr(e, "detail", None) or str(e)
            failed.append({"nurse_id": nid, "reason": str(detail)})
    return {
        "transfers": transfers,
        "team_changes": team_changes,
        "skipped": skipped,
        "failed": failed,
        "effective_date": effective.isoformat(),
    }
