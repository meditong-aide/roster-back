"""
Grade-aware Local Repair

목표:
- 커버리지를 깨지 않고, 하드 제약(법규/야간/고정셀)을 지키면서
  Grade 분배 부족을 한 명씩 이동하여 완화한다.
"""
from __future__ import annotations

import math
import hashlib
from datetime import timedelta
from typing import Any, Dict, List, Tuple
import numpy as np


def grade_local_repair(
    rs,
    grade_config: dict[str, Any],
    max_iterations: int = 100,
    max_moves_per_nurse: int = 1,
):
    """
    Args:
        rs: RosterSystem (solver 결과가 반영된 상태)
        grade_config: grade 설정 (constraints_json/constraints, null_grade_policy, min_leader_keep 등)
    Returns:
        (updated_roster, repair_log)
    """
    roster = rs.roster
    N, D, S = roster.shape
    shift_types = rs.config.shift_types
    grade_log: List[Dict[str, Any]] = []

    constraints_map = grade_config.get("constraints") or grade_config.get("constraints_json") or {}
    if not constraints_map:
        return roster, [{"summary": "NO_CONSTRAINTS"}]

    grade_values = _extract_grade_values(constraints_map)
    null_policy = str(grade_config.get("null_grade_policy") or "LOWEST").upper()
    min_leader_keep = bool(grade_config.get("min_leader_keep", True))
    min_ratio_floor = grade_config.get("min_ratio_floor", None)
    try:
        min_ratio_floor = float(min_ratio_floor) if min_ratio_floor is not None else None
    except Exception:
        min_ratio_floor = None

    # nurse grade mapping
    nurse_grades = _map_nurse_grades(rs.nurses, grade_values, null_policy)

    # track moves per nurse
    move_count = {i: 0 for i in range(N)}

    # join/leave 계산 (로스터 시스템에 저장되지 않으므로 여기서 계산)
    join, leave = _compute_join_leave(rs)

    iter_count = 0
    while iter_count < max_iterations:
        shortages = _compute_shortages(rs, roster, nurse_grades, constraints_map, grade_values, min_leader_keep, min_ratio_floor, join, leave)
        if not shortages:
            break

        # 우선순위 정렬
        shortages.sort(key=lambda x: (-x["short"], -x["scarcity"], _shift_priority(x["shift"])))
        progress = False

        for item in shortages:
            d = item["day"]
            s_code = item["shift"]
            g = item["grade"]
            target = item["target"]

            # 후보 간호사 찾기
            candidates = _find_candidates(rs, roster, nurse_grades, d, s_code, g, move_count, max_moves_per_nurse, join, leave)
            best_action = None
            best_score = 0

            for n_idx, src_shift_idx in candidates:
                # 가상의 이동 적용
                new_roster = roster.copy()
                s_idx = shift_types.index(s_code)
                if src_shift_idx is not None:
                    new_roster[n_idx, d, src_shift_idx] = 0
                new_roster[n_idx, d, s_idx] = 1

                # 커버리지 깨지면 reject
                if _breaks_coverage(rs, new_roster, d, shift_types):
                    continue

                # 하드 제약 위반 검사: 기존 validator 재사용
                saved = rs.roster
                rs.roster = new_roster
                violations = rs._find_violations()
                rs.roster = saved
                if violations:
                    continue

                # 1N/NONON 금지 시: N 관련 이동 후 해당 간호사에게 1N 또는 NONON 생기면 reject
                if getattr(rs.config, "not_one_night", False) and (
                    s_code == "N" or (src_shift_idx is not None and shift_types[src_shift_idx] == "N")
                ):
                    if _has_1n_or_nonon(rs, new_roster, n_idx, join, leave):
                        continue

                # Grade 개선도 평가
                before_short = item["short"]
                after_short = max(0, target - (_assigned_grade(new_roster, nurse_grades, d, s_idx, g)))
                gain = before_short - after_short
                score = gain
                if score > best_score:
                    best_score = score
                    best_action = (n_idx, src_shift_idx, s_idx, before_short, after_short)

            if best_action and best_score > 0:
                n_idx, src_shift_idx, tgt_idx, before_short, after_short = best_action
                if src_shift_idx is not None:
                    roster[n_idx, d, src_shift_idx] = 0
                roster[n_idx, d, tgt_idx] = 1
                move_count[n_idx] += 1
                grade_log.append({
                    "day": d + 1,
                    "shift": s_code,
                    "grade": g,
                    "nurse": getattr(rs.nurses[n_idx], "db_id", n_idx),
                    "from": shift_types[src_shift_idx] if src_shift_idx is not None else None,
                    "to": s_code,
                    "before_short": before_short,
                    "after_short": after_short,
                })
                progress = True
                break  # 한 번에 한 명만 이동

        iter_count += 1
        if not progress:
            # 더 이상 개선 불가
            break

    if shortages := _compute_shortages(rs, roster, nurse_grades, constraints_map, grade_values, min_leader_keep, min_ratio_floor, join, leave):
        for item in shortages:
            grade_log.append({
                "day": item["day"] + 1,
                "shift": item["shift"],
                "grade": item["grade"],
                "short": item["short"],
                "reason": "NO_VALID_CANDIDATE",
            })

    return roster, grade_log


# ───────────────────────────────────────── helpers ─────────────────────────────────────────

def _extract_grade_values(constraints_map: Any) -> List[int]:
    grades = set()
    for _, gmap in (constraints_map or {}).items():
        if not isinstance(gmap, dict):
            continue
        for k in gmap.keys():
            try:
                grades.add(int(k))
            except Exception:
                continue
    return sorted(grades) or [1, 2, 3]


def _map_nurse_grades(nurses, grade_values: List[int], null_policy: str) -> List[int]:
    vals = []
    raw = []
    for n in nurses:
        g = getattr(n, "grade", None)
        try:
            gi = int(g)
            raw.append(gi)
        except Exception:
            raw.append(None)
    avg = round(sum(x for x in raw if x is not None) / max(1, len([x for x in raw if x is not None]))) if any(x is not None for x in raw) else max(grade_values)
    for i, g in enumerate(raw):
        if g in grade_values:
            vals.append(g)
            continue
        if null_policy == "AVERAGE":
            vals.append(avg if avg in grade_values else max(grade_values))
        elif null_policy == "RANDOM":
            h = hashlib.md5(str(getattr(nurses[i], "db_id", i)).encode()).hexdigest()
            vals.append(grade_values[int(h[:8], 16) % len(grade_values)])
        else:  # LOWEST
            vals.append(max(grade_values))
    return vals


def _compute_shortages(rs, roster, nurse_grades, constraints_map, grade_values, min_leader_keep, min_ratio_floor, join, leave):
    shortages = []
    shift_types = rs.config.shift_types
    ds_by_day = getattr(rs.config, "daily_shift_requirements_by_day", None)
    for d in range(rs.num_days):
        if isinstance(ds_by_day, list) and d < len(ds_by_day):
            need_map = ds_by_day[d]
        else:
            need_map = rs.config.daily_shift_requirements
        for shift_code, base in (constraints_map or {}).items():
            s_code = str(shift_code or "").upper()
            if s_code not in shift_types:
                continue
            s_idx = shift_types.index(s_code)
            req = int(need_map.get(s_code, 0))
            if req <= 0:
                continue
            base_min = {g: int(base.get(str(g), 0) if isinstance(base, dict) else 0) for g in grade_values}
            sum_base = sum(base_min.values())
            if sum_base <= 0:
                continue
            target = {}
            for g in grade_values:
                t = math.ceil(req * base_min.get(g, 0) / sum_base)
                target[g] = min(t, req)
            if min_leader_keep and 1 in base_min and base_min.get(1, 0) > 0 and req > 0:
                target[1] = min(max(1, target.get(1, 0)), req)
            if min_ratio_floor is not None and sum_base > 0:
                ratio = req / sum_base
                ratio = max(min_ratio_floor, min(1.0, ratio))
                for g in grade_values:
                    t = math.ceil(req * base_min.get(g, 0) / sum_base * ratio)
                    target[g] = min(max(target.get(g, 0), t), req)

            assigned = {g: 0 for g in grade_values}
            for n_idx in range(len(nurse_grades)):
                if int(roster[n_idx, d, s_idx]) == 1:
                    g = nurse_grades[n_idx]
                    assigned[g] = assigned.get(g, 0) + 1

            for g in grade_values:
                short = max(0, target.get(g, 0) - assigned.get(g, 0))
                if short > 0:
                    # scarcity: target / available
                    avail = _available_grade(rs, nurse_grades, d, s_code, g, join, leave)
                    scarcity = (target.get(g, 0) / max(1, avail))
                    shortages.append({
                        "day": d,
                        "shift": s_code,
                        "grade": g,
                        "short": short,
                        "target": target.get(g, 0),
                        "assigned": assigned.get(g, 0),
                        "scarcity": scarcity,
                    })
    return shortages


def _is_night_only(nurse) -> bool:
    """N 전담 여부. 리스트 ['N'] 또는 레거시 3과 동일하게 판별."""
    raw = getattr(nurse, "is_night_nurse", None)
    if isinstance(raw, list):
        allowed = {str(x).strip().upper() for x in raw if str(x).strip()}
        return allowed == {"N"}
    return raw == 3 or (raw is not None and raw != 0 and raw is not False)


def _available_grade(rs, nurse_grades, day_idx, shift_code, g, join, leave):
    shift_types = rs.config.shift_types
    s_idx = shift_types.index(shift_code)
    cnt = 0
    for n_idx, ng in enumerate(nurse_grades):
        if ng != g:
            continue
        if not (join[n_idx] <= day_idx <= leave[n_idx]):
            continue
        if shift_code in ("D", "E") and _is_night_only(rs.nurses[n_idx]):
            continue
        # fixed 셀 제외
        if hasattr(rs, "fixed_cells"):
            if any(fc.get("nurse_index") == n_idx and fc.get("day_index") == day_idx for fc in (rs.fixed_cells or [])):
                continue
        cnt += 1
    return cnt


def _find_candidates(rs, roster, nurse_grades, day_idx, shift_code, g, move_count, max_moves_per_nurse, join, leave):
    """이동 가능한 후보 (n_idx, src_shift_idx) 리스트 반환."""
    shift_types = rs.config.shift_types
    s_idx = shift_types.index(shift_code)
    candidates = []
    _preceptee_on = bool(getattr(rs.config, 'preceptee_on', False))
    for n_idx, ng in enumerate(nurse_grades):
        if ng != g:
            continue
        # 프리셉티는 프리셉터를 따라가므로 독립 이동 금지
        if _preceptee_on and getattr(rs.nurses[n_idx], 'preceptor_id', None):
            continue
        if move_count.get(n_idx, 0) >= max_moves_per_nurse:
            continue
        if not (join[n_idx] <= day_idx <= leave[n_idx]):
            continue
        if shift_code in ("D", "E") and _is_night_only(rs.nurses[n_idx]):
            continue
        # 이미 해당 shift에 배정되어 있으면 skip
        if int(roster[n_idx, day_idx, s_idx]) == 1:
            continue
        # fixed 셀은 이동 불가
        if hasattr(rs, "fixed_cells"):
            if any(fc.get("nurse_index") == n_idx and fc.get("day_index") == day_idx for fc in (rs.fixed_cells or [])):
                continue
        # 현재 배정된 shift (있을 수도, 없을 수도)
        src_idx = None
        for s in range(roster.shape[2]):
            if int(roster[n_idx, day_idx, s]) == 1:
                src_idx = s
                break
        candidates.append((n_idx, src_idx))
    return candidates


def _has_1n_or_nonon(rs, roster, n_idx: int, join: List[int], leave: List[int]) -> bool:
    """해당 간호사의 roster에 1N(홀N) 또는 NONON(5일 창 N-O-N-O-N)이 있으면 True.

    not_one_night 설정일 때만 의미 있음; 미설정이면 False 반환.
    """
    if not getattr(rs.config, "not_one_night", False):
        return False
    shift_types = rs.config.shift_types
    if "N" not in shift_types:
        return False
    night_idx = shift_types.index("N")
    off_idx = shift_types.index("O") if "O" in shift_types else None
    T0, T1 = join[n_idx], leave[n_idx]

    # 1N: N인데 좌우 모두 N이 아님
    for d in range(T0, T1 + 1):
        if int(roster[n_idx, d, night_idx]) != 1:
            continue
        left_n = int(roster[n_idx, d - 1, night_idx]) == 1 if d - 1 >= T0 else False
        right_n = int(roster[n_idx, d + 1, night_idx]) == 1 if d + 1 <= T1 else False
        if not left_n and not right_n:
            return True

    # NONON: 5일 창 [d0..d0+4]가 N,O,N,O,N
    if off_idx is not None and T1 - T0 >= 4:
        for d0 in range(T0, T1 - 4 + 1):
            if (
                int(roster[n_idx, d0, night_idx]) == 1
                and int(roster[n_idx, d0 + 1, off_idx]) == 1
                and int(roster[n_idx, d0 + 2, night_idx]) == 1
                and int(roster[n_idx, d0 + 3, off_idx]) == 1
                and int(roster[n_idx, d0 + 4, night_idx]) == 1
            ):
                return True
    return False


def _breaks_coverage(rs, new_roster, day_idx, shift_types):
    """이동 후 coverage가 부족하면 True."""
    if hasattr(rs.config, "daily_shift_requirements_by_day") and isinstance(rs.config.daily_shift_requirements_by_day, list) and day_idx < len(rs.config.daily_shift_requirements_by_day):
        need_map = rs.config.daily_shift_requirements_by_day[day_idx]
    else:
        need_map = rs.config.daily_shift_requirements
    for code, need in need_map.items():
        if code not in shift_types:
            continue
        s_idx = shift_types.index(code)
        assigned = int(np.sum(new_roster[:, day_idx, s_idx]))
        if assigned < int(need):
            return True
    return False


def _assigned_grade(roster, nurse_grades, day_idx, s_idx, g):
    return sum(1 for n_idx, ng in enumerate(nurse_grades) if ng == g and int(roster[n_idx, day_idx, s_idx]) == 1)


def _shift_priority(code: str):
    if code == "D":
        return 3
    if code == "E":
        return 2
    if code == "N":
        return 1
    return 0


def _compute_join_leave(rs):
    """roster_system에서 join/leave 배열을 계산한다."""
    join, leave = [], []
    first_day = rs.target_month
    D = rs.num_days
    last_day = first_day + timedelta(days=D - 1)
    for nu in rs.nurses:
        j = (nu.joining_date - first_day).days if getattr(nu, "joining_date", None) else 0
        if getattr(nu, "resignation_date", None):
            if nu.resignation_date < first_day:
                # 이번 달 이전에 퇴사한 경우: 변수 생성을 막기 위해 범위를 비워둔다.
                join.append(1)
                leave.append(0)
                continue
            l = (nu.resignation_date - first_day).days
            if nu.resignation_date > last_day:
                l = D - 1
        else:
            l = D - 1
        j = max(j, 0)
        l = min(l, D - 1)
        join.append(j)
        leave.append(l)
    return join, leave

