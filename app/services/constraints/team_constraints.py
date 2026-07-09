"""팀(Team) 기반 최소 시프트 커버리지 제약 모듈.

`team_min_by_team[team_id]` 맵에 따라 **팀마다 개별**로 일일 최소 시프트 커버리지를
보장한다. 예: 팀 "1" = {D≥1, E≥1, N≥0}, 팀 "2" = {D≥2, E≥1, N≥1}.
use_mid=True 일 때는 'M' 키도 사용 가능.

활성 조건:
    - grade_strategy ∈ {"TEAM", "COMBINED"}
    - cfg.team_min_by_team 이 비어있지 않고, 1개 이상 팀이 최소값을 가짐
    - 해당 팀에 배정된 간호사가 존재

소프트 폴백(team_min_soft_fallback=True, 기본):
    각 (팀, 일자, 시프트)에 슬랙을 두고 penalty_weight * slack 을 목적함수에서 차감.

하드(team_min_soft_fallback=False):
    `sum(X(n,d,s) for n in active team members) >= team_min[s]` 를 직접 강제.
"""

from __future__ import annotations

from services.roster_system import RosterSystem


def _clean_team_min(raw, shift_types: list[str], use_mid: bool) -> dict[str, int]:
    """한 팀의 min_shift dict 를 정제: 유효한 시프트 키 + 양수 값만 남긴다."""
    out: dict[str, int] = {}
    if not isinstance(raw, dict):
        return out
    for code, v in raw.items():
        try:
            iv = int(v or 0)
        except (TypeError, ValueError):
            continue
        if iv <= 0:
            continue
        if code == "O":
            continue
        if code == "M" and not use_mid:
            continue
        if code not in shift_types:
            continue
        out[code] = iv
    return out


def add_team_min_constraints(
    m,
    rs: RosterSystem,
    X,
    join,
    leave,
    *,
    grade_strategy: str = "BASE",
    blocked_by_nurse=None,
) -> list:
    obj_terms: list = []
    cfg = rs.config
    _impact_modes = getattr(rs, "_constraint_impact_constraint_modes", None)
    if _impact_modes is None:
        _impact_modes = []
        setattr(rs, "_constraint_impact_constraint_modes", _impact_modes)

    team_min_by_team = dict(getattr(cfg, "team_min_by_team", {}) or {})
    if not team_min_by_team:
        print("[TeamMin] skip: cfg.team_min_by_team is empty")
        return obj_terms

    use_mid = bool(getattr(cfg, "use_mid", False))
    allow_soft = bool(getattr(cfg, "team_min_soft_fallback", False))
    base_penalty_weight = int(getattr(cfg, "team_min_penalty_weight", 80000) or 0)
    # grade_strategy=TEAM 이면 team_min weight 가중치를 끌어올림(선호 신호).
    gs = str(grade_strategy or "").upper()
    penalty_weight = base_penalty_weight * 4 if gs == "TEAM" else base_penalty_weight

    # 팀별 멤버 인덱스 집합
    team_members: dict[str, list[int]] = {}
    for idx, nurse in enumerate(rs.nurses):
        tid = getattr(nurse, "team_id", None)
        if tid in (None, "", 0):
            continue
        team_members.setdefault(str(tid), []).append(idx)
    if not team_members:
        print("[TeamMin] skip: no nurses have team_id")
        return obj_terms

    shift_types = list(rs.config.shift_types)
    added_cnt = 0
    skipped_capacity: list[tuple[str, int, str, int, int]] = []  # (tid, day, code, active, min_t)

    # MUS 추출용 hard assumption registry — 모델에 attach 된 경우만 wrap.
    _registry = getattr(m, "_cpsat_assumption_registry", None)
    _add_hard_fn = None
    if _registry is not None and not allow_soft:
        try:
            from services.cp_sat.hard_assumption import add_hard as _add_hard_fn
        except Exception:
            _add_hard_fn = None

    for tid, members in team_members.items():
        if not members:
            continue
        raw_min = team_min_by_team.get(tid)
        team_min = _clean_team_min(raw_min, shift_types, use_mid)
        if not team_min:
            continue

        shift_indices = [(code, shift_types.index(code)) for code in team_min.keys()]

        for d in range(rs.num_days):
            active = [n for n in members if join[n] <= d <= leave[n]]
            if not active:
                continue
            for code, s_idx in shift_indices:
                min_t = team_min[code]
                upper = len(active)
                if upper < min_t and not allow_soft:
                    # 하드 모드에서 데이터 부족으로 강제 불가 → 스킵(INFEASIBLE 방지)
                    skipped_capacity.append((tid, d, code, upper, min_t))
                    _impact_modes.append({
                        "family": "team_min",
                        "key": f"team_min:{tid}:{d}:{code}",
                        "configured_mode": "hard",
                        "effective_mode": "skipped_by_capacity",
                        "source_file": "app/services/constraints/team_constraints.py",
                        "reason": "active team members < min_t in hard mode",
                        "evidence": {"team_id": tid, "day": d + 1, "shift": code, "active": upper, "min_t": min_t},
                    })
                    continue
                vars_sum = sum(X(n, d, s_idx) for n in active)
                if allow_soft:
                    slack = m.NewIntVar(0, min_t, f"tmin_slack_t{tid}_d{d}_s{s_idx}")
                    m.Add(vars_sum + slack >= min_t)
                    if penalty_weight > 0:
                        obj_terms.append(-penalty_weight * slack)
                    _impact_modes.append({
                        "family": "team_min",
                        "key": f"team_min:{tid}:{d}:{code}",
                        "configured_mode": "soft",
                        "effective_mode": "soft_fallback",
                        "source_file": "app/services/constraints/team_constraints.py",
                        "reason": "team_min soft fallback active",
                        "evidence": {"team_id": tid, "day": d + 1, "shift": code, "min_t": min_t},
                    })
                else:
                    if _add_hard_fn is not None and _registry is not None:
                        # 같은 (team, shift) 의 모든 day 를 하나의 literal 로 묶음.
                        _name = f"TeamMin:team_{tid}:{code}"
                        _meta = {
                            "node_id": f"team_min:team_{tid}:{code.lower()}",
                            "type": "TeamMinNode",
                            "label": f"Team {tid} {code} min ≥ {int(min_t)}",
                            "value": {"team_id": tid, "shift": code, "min": int(min_t)},
                            "scope": "team",
                            "scope_key": f"team_{tid}_{code.lower()}_min",
                            "pattern": "team_min",
                            "human_message_ko": f"팀 {tid} 의 {code} 시프트 최소 인원 정책",
                            "resolution_hint": (
                                f"팀 {tid} 의 {code} 최소치({int(min_t)}) 를 낮추거나 "
                                "team_min 을 soft fallback 으로 전환하세요."
                            ),
                        }
                        _add_hard_fn(m, _registry, name=_name,
                                     constraint_expr=(vars_sum >= min_t), meta=_meta)
                    else:
                        m.Add(vars_sum >= min_t)
                    _impact_modes.append({
                        "family": "team_min",
                        "key": f"team_min:{tid}:{d}:{code}",
                        "configured_mode": "hard",
                        "effective_mode": "enforced",
                        "source_file": "app/services/constraints/team_constraints.py",
                        "reason": "team_min hard constraint added",
                        "evidence": {"team_id": tid, "day": d + 1, "shift": code, "min_t": min_t},
                    })
                added_cnt += 1

    mode = "soft" if allow_soft else "hard"
    print(
        f"[TeamMin] mode={mode} teams={len(team_members)} added={added_cnt} "
        f"skipped_capacity={len(skipped_capacity)}"
    )
    if skipped_capacity:
        sample = skipped_capacity[:10]
        for tid, d, code, upper, min_t in sample:
            print(
                f"[TeamMin][skip-capacity] team={tid} day={d+1} code={code} "
                f"active={upper} < min={min_t} (hard mode)"
            )
        if len(skipped_capacity) > len(sample):
            print(f"[TeamMin][skip-capacity] ...and {len(skipped_capacity) - len(sample)} more")

    return obj_terms
