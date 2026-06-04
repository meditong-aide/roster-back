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

from math import ceil
from typing import Optional

from sqlalchemy.orm import Session

from db.models import FixedWantedEntry, Group, Nurse as NurseModel, Shift
from services.team_auto_assign import NurseInput, auto_assign_teams

_OFF_SHIFT_CODES = frozenset({"O", "OFF", "주"})


def _load_pool(
    db: Session, group_ids: list[str], year: int, month: int
) -> tuple[list[NurseModel], list[NurseInput], dict[str, str]]:
    """풀(N병동) 간호사 + NurseInput + nurse_id→현재 group_id 맵.

    N전담 제외. off_days=확정 원티드 OFF, fb_days=확정 원티드 연차(휴가 type).
    원티드는 각 간호사의 현재 소속 병동 기준으로 조회된다.
    """
    nurses = (
        db.query(NurseModel)
        .filter(NurseModel.group_id.in_(group_ids), NurseModel.active == 1)
        .all()
    )
    pool = [n for n in nurses if (n.is_night_nurse or []) != ["N"]]
    current_group = {n.nurse_id: n.group_id for n in pool}

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
    return pool, inputs, current_group


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
) -> dict:
    """병동 간 재분배 미리보기 (read-only, DB 변경 없음).

    capacity_mode:
      - "even": 균등분할(총원/N) ± tolerance
      - "explicit": 그룹별 목표 인원(target_sizes) ± tolerance.
        시드는 각 그룹의 현재 grade-1 1명으로 고정(cluster i ↔ ward i), 정원 밴드 적용.
        풀 인원이 [Σmin, Σmax] 안에 들어야 함(여유 흡수).

    Returns: {target_month, ward_ids, num_wards, num_pool, num_excluded_night,
              capacity_mode, size_bounds, warnings, wards, moves, num_moved, stats}
    """
    ward_ids = sorted(set(group_ids))
    if len(ward_ids) < 2:
        raise ValueError("재분배는 2개 이상의 병동이 필요합니다.")
    pool, inputs, current_group = _load_pool(db, ward_ids, year, month)
    if not pool:
        raise ValueError("재분배 대상 간호사가 없습니다.")

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

    if capacity_mode == "explicit":
        if not target_sizes:
            raise ValueError("explicit 모드는 target_sizes(그룹별 목표 인원)가 필요합니다.")
        missing = [w for w in ward_ids if w not in target_sizes]
        if missing:
            raise ValueError(f"target_sizes 에 누락된 그룹: {missing}")
        # 각 그룹의 현재 grade-1 1명을 앵커 시드로 (cluster i ↔ ward i 고정).
        # 사전 검증(_wards_missing_g1)을 통과했으므로 모든 병동에 G1이 보장된다.
        seeds = [
            next(n.nurse_id for n in pool
                 if current_group[n.nurse_id] == w and n.grade == 1)
            for w in ward_ids
        ]
        min_sizes = [max(1, target_sizes[w] - size_tolerance) for w in ward_ids]
        max_sizes = [target_sizes[w] + size_tolerance for w in ward_ids]
        if not (sum(min_sizes) <= total <= sum(max_sizes)):
            raise ValueError(
                f"정원 밴드로 풀({total}명)을 담을 수 없습니다. "
                f"Σmin={sum(min_sizes)}, Σmax={sum(max_sizes)}, 목표합={sum(target_sizes.values())}. "
                f"인원 또는 허용치(±{size_tolerance})를 조정하세요."
            )
        # cluster i ↔ ward_ids[i] 고정 → 현재 병동 유지 보상(churn 억제)
        ward_index = {w: i for i, w in enumerate(ward_ids)}
        home_cluster = {
            nid: ward_index[current_group[nid]]
            for nid in current_group if current_group[nid] in ward_index
        }
        result = auto_assign_teams(
            inputs, seed_ids=seeds, min_sizes=min_sizes, max_sizes=max_sizes,
            home_cluster=home_cluster, w_churn=churn_weight,
        )
        idx_to_ward = {i: ward_ids[i] for i in range(num_wards)}
        size_bounds = {"mode": "explicit", "tolerance": size_tolerance,
                       "targets": {w: target_sizes[w] for w in ward_ids}}
    else:  # even
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

    excluded = [
        {"nurse_id": n.nurse_id, "name": n.name, "group_id": n.group_id}
        for n in db.query(NurseModel)
        .filter(NurseModel.group_id.in_(ward_ids), NurseModel.active == 1)
        if (n.is_night_nurse or []) == ["N"]
    ]
    return {
        "target_month": f"{year}-{month:02d}",
        "ward_ids": ward_ids,
        "num_wards": num_wards,
        "num_pool": total,
        "num_excluded_night": len(excluded),
        "excluded_night": excluded,
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
        "stats": {
            "objective": result.objective,
            "overlap_total": result.overlap_total,
            "grade_dev_total": result.grade_dev_total,
        },
    }
