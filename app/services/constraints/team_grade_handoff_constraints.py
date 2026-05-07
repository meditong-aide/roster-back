"""팀×Grade 교차 인계(handoff) 제한 제약 모듈.

같은 팀(=같은 병실 관리 단위) 안에서 지정된 grade 집합에 속한 간호사들끼리
- `block_same_shift`: 같은 시프트에 동시에 2명 이상 배치 금지
- `block_adjacent`: 인접 인계(D→E, E→N, N→D_{익일}) 금지
를 부여한다.

활성 조건:
    - grade_strategy ∈ {"COMBINED"}
    - cfg.team_handoff_policy_by_team 에 유효한 규칙이 1건 이상 존재
    - 해당 팀에 배정된 간호사가 존재

규칙 스키마(v1: 대칭 집합):
    {"grades": [6,7,8], "block_same_shift": true, "block_adjacent": true}

미래 확장(v2: 방향성):
    {"from": [7,8], "to": [5], "bidirectional": false,
     "block_same_shift": false, "block_adjacent": true}

CP-SAT 수식:
    b_L[t,d,s] = OR(X[n,d,s] for n in L_t)  (aux 바이너리, 링크 b >= X)
    b_R[t,d,s] = OR(X[n,d,s] for n in R_t)

    block_adjacent:
        b_L[t,d,D] + b_R[t,d,E]   <= 1
        b_L[t,d,E] + b_R[t,d,N]   <= 1
        b_L[t,d,N] + b_R[t,d+1,D] <= 1
    block_same_shift (L==R 인 경우 의미있음):
        sum(X[n,d,s] for n in L_t) <= 1
    soft fallback: 각 제약에 slack 추가 + penalty_weight * slack 차감.
"""

from __future__ import annotations

from typing import Any

from services.roster_system import RosterSystem


def _safe_int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _extract_lhs_rhs(rule: dict) -> list[tuple[list[int], list[int]]]:
    """규칙에서 (lhs, rhs) 페어 리스트를 반환. 대칭 규칙은 1개, 양방향 방향성은 2개."""
    if "grades" in rule:
        gs = sorted({g for g in (_safe_int(x) for x in rule.get("grades") or []) if g is not None})
        if not gs:
            return []
        return [(gs, gs)]
    if "from" in rule and "to" in rule:
        frs = sorted({g for g in (_safe_int(x) for x in rule.get("from") or []) if g is not None})
        tos = sorted({g for g in (_safe_int(x) for x in rule.get("to") or []) if g is not None})
        if not frs or not tos:
            return []
        pairs = [(frs, tos)]
        if rule.get("bidirectional"):
            pairs.append((tos, frs))
        return pairs
    return []


def _members_for_grades(rs: RosterSystem, team_id: str, grades: list[int]) -> list[int]:
    """팀과 grade 집합에 모두 속하는 간호사 인덱스 리스트."""
    gs = set(grades)
    out: list[int] = []
    for idx, n in enumerate(rs.nurses):
        tid = getattr(n, "team_id", None)
        if tid in (None, "", 0):
            continue
        if str(tid) != str(team_id):
            continue
        raw_grade = getattr(n, "grade", None)
        gi = _safe_int(raw_grade)
        if gi is None or gi not in gs:
            continue
        out.append(idx)
    return out


def _active_members_on_day(members: list[int], join: list[int], leave: list[int], d: int) -> list[int]:
    return [n for n in members if join[n] <= d <= leave[n]]


def _or_aux_var(m, X, day: int, shift_idx: int, members: list[int], tag: str):
    """b = OR(X[n,day,shift_idx] for n in members) 인 aux 바이너리를 생성한다.

    목적함수에서 slack 최소화 방향이므로, 링크 b >= X[n] 만으로도 solver는 필요한 경우 b=1 로 설정한다.
    members 가 비어있으면 b=0 고정(상수화).
    """
    b = m.NewBoolVar(f"hoff_or_{tag}_d{day}_s{shift_idx}")
    if not members:
        m.Add(b == 0)
        return b
    for n in members:
        m.Add(b >= X(n, day, shift_idx))
    return b


def _shift_index(rs: RosterSystem, code: str) -> int | None:
    try:
        return rs.config.shift_types.index(code)
    except (ValueError, AttributeError):
        return None


def add_team_grade_handoff_constraints(
    m,
    rs: RosterSystem,
    X,
    join: list[int],
    leave: list[int],
    *,
    grade_strategy: str = "BASE",
) -> list:
    """팀×grade 교차 handoff 제약을 추가한다. 목적함수 항 리스트를 반환."""
    obj_terms: list = []
    _impact_modes = getattr(rs, "_constraint_impact_constraint_modes", None)
    if _impact_modes is None:
        _impact_modes = []
        setattr(rs, "_constraint_impact_constraint_modes", _impact_modes)
    gs = str(grade_strategy or "BASE").upper()
    if gs not in ("COMBINED",):
        return obj_terms

    cfg = rs.config
    policy_by_team: dict[str, dict] = dict(getattr(cfg, "team_handoff_policy_by_team", {}) or {})
    if not policy_by_team:
        return obj_terms

    allow_soft = bool(getattr(cfg, "team_handoff_soft_fallback", True))
    penalty_weight = int(getattr(cfg, "team_handoff_penalty_weight", 80000) or 0)
    num_days = int(rs.num_days)
    _impact_modes.append({
        "family": "handoff",
        "key": "team_grade_handoff:global",
        "configured_mode": "soft" if allow_soft else "hard",
        "effective_mode": "soft_fallback" if allow_soft else "enforced",
        "source_file": "app/services/constraints/team_grade_handoff_constraints.py",
        "reason": "team grade handoff active",
        "evidence": {"strategy": gs, "penalty_weight": penalty_weight},
    })

    s_D = _shift_index(rs, "D")
    s_E = _shift_index(rs, "E")
    s_N = _shift_index(rs, "N")

    for team_id, policy in policy_by_team.items():
        if not isinstance(policy, dict):
            continue
        rules = policy.get("restrictions") or []
        if not isinstance(rules, list) or not rules:
            continue

        for r_idx, rule in enumerate(rules):
            if not isinstance(rule, dict):
                continue
            pairs = _extract_lhs_rhs(rule)
            if not pairs:
                continue
            block_same = bool(rule.get("block_same_shift", False))
            block_adj = bool(rule.get("block_adjacent", True))
            if not block_same and not block_adj:
                continue

            for lhs_grades, rhs_grades in pairs:
                lhs_all = _members_for_grades(rs, team_id, lhs_grades)
                rhs_all = _members_for_grades(rs, team_id, rhs_grades)
                if not lhs_all and not rhs_all:
                    continue
                symmetric = lhs_grades == rhs_grades

                # (A) 같은 시프트 동시 ≤ 1 (대칭 규칙에 한해 의미)
                if block_same and symmetric and s_D is not None:
                    for d in range(num_days):
                        active = _active_members_on_day(lhs_all, join, leave, d)
                        if len(active) < 2:
                            continue
                        for s_idx in (s_D, s_E, s_N):
                            if s_idx is None:
                                continue
                            vars_sum = sum(X(n, d, s_idx) for n in active)
                            if allow_soft:
                                slack = m.NewIntVar(
                                    0, max(0, len(active) - 1),
                                    f"hoff_same_t{team_id}_r{r_idx}_d{d}_s{s_idx}",
                                )
                                m.Add(vars_sum - slack <= 1)
                                if penalty_weight > 0:
                                    obj_terms.append(-penalty_weight * slack)
                            else:
                                m.Add(vars_sum <= 1)

                # (B) 인접 인계 금지: b_L + b_R <= 1
                if block_adj:
                    adj_shift_pairs = []
                    if s_D is not None and s_E is not None:
                        adj_shift_pairs.append(("DE", s_D, s_E, 0))  # 당일 D→E
                    if s_E is not None and s_N is not None:
                        adj_shift_pairs.append(("EN", s_E, s_N, 0))  # 당일 E→N
                    if s_N is not None and s_D is not None:
                        adj_shift_pairs.append(("ND", s_N, s_D, 1))  # 익일 N→D

                    for d in range(num_days):
                        lhs_active = _active_members_on_day(lhs_all, join, leave, d)
                        for tag, sL_idx, sR_idx, day_offset in adj_shift_pairs:
                            d2 = d + day_offset
                            if d2 >= num_days:
                                continue
                            rhs_active = _active_members_on_day(rhs_all, join, leave, d2)
                            if not lhs_active or not rhs_active:
                                continue
                            bL = _or_aux_var(
                                m, X, d, sL_idx, lhs_active,
                                f"L_t{team_id}_r{r_idx}_{tag}",
                            )
                            bR = _or_aux_var(
                                m, X, d2, sR_idx, rhs_active,
                                f"R_t{team_id}_r{r_idx}_{tag}",
                            )
                            if allow_soft:
                                slack = m.NewIntVar(
                                    0, 1,
                                    f"hoff_adj_t{team_id}_r{r_idx}_d{d}_{tag}",
                                )
                                m.Add(bL + bR - slack <= 1)
                                if penalty_weight > 0:
                                    obj_terms.append(-penalty_weight * slack)
                            else:
                                m.Add(bL + bR <= 1)

    return obj_terms
