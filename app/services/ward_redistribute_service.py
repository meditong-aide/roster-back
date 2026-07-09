"""원티드 기반 병동 간 재분배 (옵션 2: 특정 N개 병동 풀).

관리자가 관리하는 N개 병동의 간호사를 한 풀로 모아, 확정 원티드 OFF/연차 겹침으로
클러스터링한 뒤 각 클러스터를 병동에 매핑한다. 병동이 바뀌는 간호사는 병동이동(transfer)
이벤트로 발행한다 (group_id 변경 — 옵션1의 team_id 변경과 구분).

- 풀: 관리 병동들(resolve_managed_group_ids로 산출, 엔드포인트에서 주입)
- 정원: 균등 분할(총원/N), 약간의 tolerance
- 엔진: team_auto_assign.auto_assign_teams (num_teams = 병동 수)
- preview(read-only): 제안 병동 + 현재병동 대비 이동 diff + 통계
- apply: 병동이 바뀌는 간호사 → 병동이동(transfer) 이벤트 (Phase B)

N전담(allowed_shifts==['N'])은 풀에서 제외.
참조: docs/NURSE_GROUP_CHANGE_MODEL.md (옵션2), team_classify_service.py (옵션1).
"""

from __future__ import annotations

import logging
from datetime import date
from math import ceil
from typing import Optional

from sqlalchemy.orm import Session

from db.models import (
    FixedWantedEntry, Group, Nurse as NurseModel, NurseAssignment, Shift, Team,
)
from schemas.roster_schema import NurseAssignmentCreate
from services.assignment_service import create_assignment, create_permanent_change
from services.team_period import resolve_team, set_team_period
from services.team_auto_assign import NurseInput, auto_assign_teams
from services.team_classify_service import _build_pool_roster

logger = logging.getLogger(__name__)

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
        night = [n for n in nurses if (n.allowed_shifts or []) == ["N"]]
    else:
        # 참여로 명시된 N전담은 풀에 포함(override) — 나머지 N전담만 제외
        night = [
            n for n in nurses
            if (n.allowed_shifts or []) == ["N"] and n.nurse_id not in pset
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

    # 프리셉터 = nurse_preceptee_period(SSOT) as-of 대상월 (캐시 preceptor_id 안 봄).
    from services.preceptee_period import resolve_preceptor_asof
    _pre_asof = resolve_preceptor_asof(db, [n.nurse_id for n in pool], date(year, month, 1))
    # 강제 follow 는 '참여자끼리'만 (옵션1과 동일 규칙). 프리셉티나 그 프리셉터가
    # fixed(미참여) 면 follow 를 끊어 fixed 핀이 끌려가지 않게 한다.
    def _eff_preceptor(n: NurseModel):
        pid = _pre_asof.get(str(n.nurse_id))
        if pid is None:
            return None
        if n.nurse_id in non_participant_ids or pid in non_participant_ids:
            return None
        return pid

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
    """병동 앵커 시드 1명 — G1 우선, 없으면 그 병동 아무 인원으로 폴백.
    미참여(고정) 후보를 우선(참여자는 이동 자유 유지).

    (allow_missing_g1 로 G1 없는 병동도 진행 가능 → G1 부재 시 비-G1 앵커로 클러스터 바인딩만 유지.)
    """
    g1 = [n.nurse_id for n in pool
          if current_group.get(n.nurse_id) == ward and n.grade == 1]
    cands = g1 or [n.nurse_id for n in pool if current_group.get(n.nurse_id) == ward]
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
    forced_count: Optional[int] = None,
) -> dict[str, dict]:
    """재분배된 병동 안에서 팀 분해 — 옵션1 로직 재사용.

    반환 = `{label: {"team_id": int|None, "members": [{nurse_id, name}]}}`.
      - 정의된 활성 팀(Team)이 있으면 그 수만큼 분할, label=team_id, **team_id=실제 값**.
      - 정의된 팀이 없고 `forced_count>=2` 면 그 수만큼 '가분할', label="1".."N",
        **team_id=None**(아직 미존재 — 저장 시 team_label 로 생성). 옵션B.
      - 그 외(팀 0~1개 & 강제분할 없음, 또는 멤버<2) → 단일 '전체'(team_id=None).

    팀 수를 멤버의 현재 배정(`nurses.team_id`)으로 세지 않는다 — 재분배는 정의된 팀으로
    전원을 다시 균등배치하는 것이라 현재 누가 어느 팀인지는 무관하고, 무엇보다 팀 관리
    (PUT /teams)가 period 모드로만 기록해 캐시 team_id 가 NULL 이어도(team_service.
    apply_team_ops) Team 정의행은 항상 생성되므로 정의 기준이 정확하다.
    """
    members = [by_input[nid] for nid in member_ids if nid in by_input]
    team_ids = sorted(
        t for (t,) in db.query(Team.team_id)
        .filter(Team.group_id == ward_id, Team.active == 1)
        .all()
    )

    def _flat(ms):
        return [{"nurse_id": m.nurse_id, "name": nurse_name.get(m.nurse_id)} for m in ms]

    # (label, team_id) 쌍 — 정의된 팀 우선, 없으면 강제분할(가분할, team_id=None).
    if team_ids:
        labels: list[tuple[str, Optional[int]]] = [(str(t), t) for t in team_ids]
    elif forced_count and forced_count >= 2:
        labels = [(str(i + 1), None) for i in range(int(forced_count))]
    else:
        labels = []

    k = len(labels)
    if k <= 1 or len(members) < 2:
        return {"전체": {"team_id": None, "members": _flat(members)}}
    try:
        res = auto_assign_teams(members, num_teams=k)
        out: dict[str, dict] = {}
        for i, ids in res.teams.items():
            label, tid = labels[i] if i < len(labels) else (f"팀{i + 1}", None)
            out[label] = {
                "team_id": tid,
                "members": [{"nurse_id": nid, "name": nurse_name.get(nid)} for nid in ids],
            }
        return out
    except ValueError:
        return {"전체": {"team_id": None, "members": _flat(members)}}


def _detect_ward_pairs(
    all_nurses: list[NurseModel],
    nurse_to_ward: dict[str, Optional[str]],
    preceptor_asof: Optional[dict[str, Optional[str]]] = None,
) -> list[dict]:
    """원래 같은 병동이던 프리셉티→프리셉터 짝의 재분배 후 상태.

    status: together(결과 같은 병동) / split(다른 병동).
    moved: 둘 중 하나라도 병동이 바뀌면 True — 병동이동 발효 시 프리셉터십이 자동
      종료되므로(_apply_target_profile_reset) together 라도 관계는 끊긴다.
    preceptor_asof: nurse_id→preceptor_id (period SSOT as-of). 캐시 preceptor_id 안 봄.
    """
    name = {n.nurse_id: n.name for n in all_nurses}
    cur = {n.nurse_id: n.group_id for n in all_nurses}
    _pre = preceptor_asof or {}
    pairs: list[dict] = []
    for n in all_nurses:
        pr = _pre.get(str(n.nurse_id))
        if not pr or pr not in name:
            continue
        if cur.get(n.nurse_id) != cur.get(pr):
            continue  # 원래 같은 병동이던 짝만 대상
        pe = n.nurse_id
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
    team_counts: Optional[dict[str, int]] = None,
    allow_missing_g1: bool = False,
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
    if missing_g1 and not allow_missing_g1:
        names = ", ".join(w["name"] or w["group_id"] for w in missing_g1)
        raise WardSetupError(
            f"다음 병동에 시니어(grade-1)가 지정되어 있지 않습니다: {names}. "
            f"재분배 전에 각 병동에 시니어를 먼저 지정하세요.",
            wards=missing_g1,
        )
    if missing_g1:  # allow_missing_g1 → 막지 않고 경고만(없는 대로 진행, 팀별 시니어 보장 안 됨)
        _names = ", ".join(w["name"] or w["group_id"] for w in missing_g1)
        warnings.append(
            f"시니어(G1) 없는 병동: {_names} — 시니어 없이 진행합니다(팀마다 시니어 배치가 보장되지 않음)."
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
            allow_non_g1_seed=allow_missing_g1,
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
            allow_non_g1_seed=allow_missing_g1,
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
            "teams": _team_breakdown(
                db, wid, members, by_input, nurse_name,
                forced_count=(team_counts or {}).get(wid),
            ),
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
    _pair_nurses = pool + night + excluded_overlap
    from services.preceptee_period import resolve_preceptor_asof
    _pre_asof_pairs = resolve_preceptor_asof(db, [n.nurse_id for n in _pair_nurses], date(year, month, 1))
    pairs = _detect_ward_pairs(_pair_nurses, nurse_to_ward, preceptor_asof=_pre_asof_pairs)
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


def _ensure_numbered_teams(
    db: Session, office_id: str, group_id: str, labels: list[str],
) -> dict[str, int]:
    """이름(번호) 팀들이 존재하도록 보장 — 없으면 생성, 활성 동명이면 재사용. {label: team_id}.

    멱등: 같은 이름은 재사용하므로 두 번 저장해도 team_id 가 늘지 않는다(결정 4).
    하드 삭제(다른 달 period 동반 소실)는 하지 않는다 — 팀 정의는 영구, 배정만 월-스코프(결정 3).
    """
    existing = {
        t.team_name: t.team_id
        for t in db.query(Team)
        .filter(Team.office_id == office_id, Team.group_id == group_id, Team.active == 1)
        .all()
    }
    max_row = (
        db.query(Team.team_id)
        .filter(Team.office_id == office_id, Team.group_id == group_id)
        .order_by(Team.team_id.desc())
        .first()
    )
    next_id = (max_row[0] + 1) if max_row else 1
    out: dict[str, int] = {}
    for label in labels:
        tid = existing.get(label)
        if tid is None:
            db.add(Team(office_id=office_id, group_id=group_id, team_id=next_id,
                        team_name=label, active=1))
            db.flush()
            existing[label] = tid = next_id
            next_id += 1
        out[label] = tid
    return out


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

    assignments: [{nurse_id, to_group_id, team_id?, team_label?}].
      - team_id(정수): 기존 팀에 적용. team_label("1"…): 그 병동에 동명 번호 팀을 생성/재사용
        (멱등)하고 team_id 로 해석 — '팀 없는 병동'을 저장 한 번에 N개로 나눠 배정(옵션B).
        team_id 가 오면 그대로 쓰고 team_label 은 무시. 둘 다 없으면 팀 미적용('전체').
      - 병동이 바뀌면 → 병동이동(transfer) 이벤트(target_team_id 동반). 기존 create_assignment
        흐름 재사용(FixedWanted 재배치·정합성·알림 포함).
      - 같은 병동인데 team 만 바뀌면 → permanent_change(team_id).
      - 둘 다 동일 → skip.
    Returns: {transfers, team_changes, skipped, failed, effective_date, created_teams}

    per-nurse graceful: 한 명이 기간겹침(409) 등으로 실패해도 그 사람만 건너뛰고
    failed 에 사유와 함께 담는다 (배치 전체가 중단되지 않음).
    """
    effective = date(year, month, 1)
    nurses = {
        n.nurse_id: n
        for n in db.query(NurseModel).filter(NurseModel.group_id.in_(group_ids))
    }
    _note = note or f"원티드 병동재분배 {year}-{month:02d}"

    # [옵션B] phase 0 — team_label 로 들어온 배정의 병동에 번호 팀을 생성/재사용(멱등).
    #   team_id(정수)가 직접 온 항목은 라벨 무시. 라벨→실제 team_id 해석 맵을 만든다.
    #   팀을 먼저 만들어 team_id 를 확보한 뒤(아래 루프의 transfer/period 가 참조)에 진행(결정 1·5).
    _label_team_id: dict[tuple[str, str], int] = {}
    _ward_labels: dict[str, list[str]] = {}
    for a in assignments:
        if isinstance(a.get("team_id"), int):
            continue
        _lbl = str(a.get("team_label") or "").strip()
        _tg = a.get("to_group_id")
        if _lbl and _tg:
            _ward_labels.setdefault(_tg, [])
            if _lbl not in _ward_labels[_tg]:
                _ward_labels[_tg].append(_lbl)
    created_teams: dict[str, dict[str, int]] = {}
    for _tg, _labels in _ward_labels.items():
        _oid = next(
            (nurses[a["nurse_id"]].office_id for a in assignments
             if a.get("to_group_id") == _tg and a.get("nurse_id") in nurses), None
        ) or getattr(current_user, "office_id", None)
        if _oid is None:
            continue
        _resolved = _ensure_numbered_teams(db, _oid, _tg, _labels)
        created_teams[_tg] = _resolved
        for _lbl, _tid in _resolved.items():
            _label_team_id[(_tg, _lbl)] = _tid

    transfers = team_changes = skipped = 0
    failed: list[dict] = []
    for a in assignments:
        nid = a.get("nurse_id")
        to_g = a.get("to_group_id")
        team = a.get("team_id")
        team = team if isinstance(team, int) else None  # '전체'/None 등은 팀 미적용
        if team is None:
            _lbl = str(a.get("team_label") or "").strip()
            if _lbl and to_g:
                team = _label_team_id.get((to_g, _lbl))
        n = nurses.get(nid)
        if n is None or not to_g:
            skipped += 1
            continue
        cur_g = n.group_id
        try:
            _tp_group: Optional[str] = None
            if cur_g != to_g:
                req = NurseAssignmentCreate(
                    nurse_id=nid, source_group_id=cur_g, target_group_id=to_g,
                    office_id=n.office_id, start_date=effective, reason="병동이동",
                    target_team_id=team, note=_note,
                )
                create_assignment(req, db, current_user, notify=False)
                transfers += 1
                _tp_group = to_g
            elif team is not None and not _same_team(
                resolve_team(db, nid, cur_g, effective), team
            ):
                create_permanent_change(
                    db, nurse_id=nid, group_id=cur_g, office_id=n.office_id,
                    start_date=effective, new_team_id=team, note=_note,
                )
                team_changes += 1
                _tp_group = cur_g
            else:
                skipped += 1
                continue
            # [B3] 팀 변경을 시점 테이블(nurse_team_period)에 기록 (close-before-open).
            #   생성기는 resolve_team_for_roster 로 대상월 시점 팀을 여기서 읽으므로(B2),
            #   start_date 도래 전(flush 전)에도 미래 월 근무표에 새 팀이 반영된다.
            #   team 미지정('전체'/None)은 기록하지 않는다(기존 거동 유지).
            if team is not None and _tp_group is not None:
                set_team_period(
                    db, nurse_id=nid, group_id=_tp_group, valid_from=effective,
                    team_id=team, source="redistribute", note=_note,
                )
        except Exception as e:  # 기간겹침(409) 등 — 그 사람만 스킵
            # create_assignment 의 검증(_raise_if_overlap 등)은 db.add 이전에 raise 하므로
            # 부분 커밋이 없다 → 세션 무효화(rollback) 없이 다음 사람 진행.
            detail = getattr(e, "detail", None) or str(e)
            failed.append({"nurse_id": nid, "reason": str(detail)})
    # 알림: 개별 N건(create_assignment 는 notify=False) 대신 관련 병동 수간호사에게 요약 1건.
    #   운영(ENVIRONMENT=production)에서만 실제 발송 — set_app_push 가 dev 를 자체 스킵.
    if transfers or team_changes:
        try:
            from services.assignment_service import _get_head_nurse_ids
            from utils.utils import set_app_push

            _hns: set[str] = set()
            for _g in set(group_ids):
                for _h in _get_head_nurse_ids(db, _g):
                    _hns.add(str(_h))
            _sender = str(getattr(current_user, "nurse_id", "") or "")
            _office = (
                getattr(current_user, "office_id", None)
                or next((n.office_id for n in nurses.values()), "")
            )
            if _hns and _sender and _office:
                _msg = (
                    f"{year}-{month:02d} 병동재분배: 이동 {transfers}명 · 팀변경 {team_changes}명 "
                    f"({month}월 발효 예약)"
                )
                set_app_push(
                    pushCode="P30", pushSubCode="S06", officeCode=_office,
                    sendEmpSeqNo=_sender, sendMemberId=_sender,
                    receiveEmpSeqNo=",".join(sorted(_hns)),
                    pushMessage=_msg, orgPushMessage=_msg, linkUrl="", linkCode="",
                )
        except Exception as e:
            logger.error("재분배 요약 알림 발송 실패: %s", e, exc_info=True)

    return {
        "transfers": transfers,
        "team_changes": team_changes,
        "skipped": skipped,
        "failed": failed,
        "effective_date": effective.isoformat(),
        "created_teams": created_teams,  # {group_id: {team_name: team_id}} (옵션B로 생성/재사용)
    }
