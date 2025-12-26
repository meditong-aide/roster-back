"""Grade(역량 등급) 제약 모듈.

- Team 알고리즘(목적함수)과 충돌 가능성이 있으므로, 상위 로직에서 전략(grade_strategy)에 따라
  이 제약을 완전히 ON/OFF 할 수 있도록 함수 형태로 분리합니다.
- 기존 CP-SAT 전체 방법론은 유지하고, Grade 관련 제약만 '추가'합니다.
"""

from __future__ import annotations

import hashlib
import math
from typing import Any

from services.roster_system import RosterSystem


def add_grade_constraints(
    m,
    rs: RosterSystem,
    X,
    join: list[int],
    leave: list[int],
    grade_strategy: str | None,
    grade_config: dict[str, Any] | None,
) -> list:
    """Grade 분배 소프트 제약을 추가한다.

    - 커버리지(need)는 하드/강력 소프트에서 이미 충족.
    - Grade는 분배 목적(soft): 목표 = req * base/sum_base, 초과/부족을 패널티로 처리.
    - NULL Grade는 정책에 따라 결정적으로 매핑.
    """
    print('grade_strategy', grade_strategy)
    print('grade_config', grade_config)
    if str(grade_strategy or "").upper() != "GRADE":
        return []
    if grade_config is None:
        return []

    constraints_map, policy, scaling = _parse_grade_config(grade_config)
    print('constraints_map', constraints_map)
    grade_values = _extract_grade_values_from_constraints(constraints_map)
    print('grade_values', grade_values)
    if not grade_values:
        return []

    by_grade, is_night_only = _build_grade_groups(
        rs=rs,
        grade_values=grade_values,
        null_grade_policy=policy["null_grade_policy"],
    )
    print('by_grade', by_grade)
    print('is_night_only', is_night_only)
    cfg = rs.config
    ds_by_day = getattr(cfg, "daily_shift_requirements_by_day", None)
    apply_shifts = {"D", "E", "N"}

    obj_terms: list = []
    for d in range(rs.num_days):
        need_map = _get_need_map_for_day(cfg, ds_by_day, d)
        for shift_code, base in (constraints_map or {}).items():
            s_code = str(shift_code or "").upper()
            if s_code not in apply_shifts:
                continue
            if s_code not in rs.config.shift_types:
                continue

            req = _safe_int(need_map.get(s_code, 0))
            if req <= 0:
                continue

            base_min = _parse_base_min(base, grade_values)
            sum_base = sum(base_min.values())
            if sum_base <= 0:
                continue

            s_idx = rs.config.shift_types.index(s_code)

            # 1) 목표분배 계산: target_g = ceil(req * base_g / sum_base)
            target = {}
            for g in grade_values:
                t = math.ceil(req * base_min.get(g, 0) / sum_base)
                target[g] = min(t, req)

            # 2) 리더 최소 보존
            if (
                scaling["min_leader_keep"]
                and 1 in grade_values
                and base_min.get(1, 0) > 0
                and req > 0
            ):
                target[1] = min(max(1, target.get(1, 0)), req)

            # 3) 배정/가용 계산
            assigned_vars = {g: [X(n, d, s_idx) for n in by_grade.get(g, [])] for g in grade_values}
            assigned_sum = {g: sum(assigned_vars.get(g, [])) for g in grade_values}
            available = _available_by_grade_for_day_shift(
                rs=rs,
                by_grade=by_grade,
                is_night_only=is_night_only,
                day_idx=d,
                shift_code=s_code,
                join=join,
                leave=leave,
            )

            # 4) 분배 패널티: |assigned - target| (over/under)
            for g in grade_values:
                tgt = int(target.get(g, 0))
                if tgt <= 0:
                    continue
                # 목표가 가용보다 크면 목표를 가용으로 클램프 (단, 소프트이므로 크게 문제 없음)
                if tgt > available.get(g, 0):
                    tgt = available.get(g, 0)
                over = m.NewIntVar(0, req, f"grade_over_{d}_{s_code}_{g}")
                under = m.NewIntVar(0, req, f"grade_under_{d}_{s_code}_{g}")
                m.Add(assigned_sum[g] - tgt <= over)
                m.Add(tgt - assigned_sum[g] <= under)
                obj_terms.append(-scaling["grade_penalty_weight"] * over)
                obj_terms.append(-scaling["grade_penalty_weight"] * under)
    return obj_terms
    return obj_terms


def _normalize_grade_int(value: Any) -> int | None:
    """DB/입력에서 읽은 grade 값을 정수로 정규화한다(실패 시 None).

    주의:
        - Grade의 범위(예: 1~3)는 제약 정의역(constraints)의 키로 결정한다.
          여기서는 단순히 정수 변환만 수행한다.
    """
    if value is None:
        return None
    try:
        v = int(value)
    except Exception:
        return None
    return v


def _average_grade_or_lowest(grades: list[int | None], fallback: int) -> int:
    """주어진 grade 목록의 평균(반올림)을 계산하고, 전부 None이면 fallback을 반환한다."""
    vals = [g for g in grades if g is not None]
    if not vals:
        return int(fallback)
    avg = sum(vals) / float(len(vals))
    return int(math.floor(avg + 0.5))


def _resolve_null_or_unknown_grade(
    policy: str,
    nurse_db_id: str,
    avg_grade: int,
    grade_values: list[int],
) -> int:
    """NULL 또는 정의역 밖 Grade를 정책에 따라 결정적으로 변환한다."""
    p = str(policy or "LOWEST").upper()
    if p == "LOWEST":
        return int(max(grade_values))
    if p == "AVERAGE":
        # 평균값이 정의역 밖이면 가장 가까운 값으로 스냅한다.
        return _snap_to_domain(avg_grade, grade_values)
    if p == "RANDOM":
        # 해시 기반 결정적 매핑: md5(nurse_id) % len(grade_values)
        h = hashlib.md5(nurse_db_id.encode("utf-8")).hexdigest()
        idx = int(h[:8], 16) % len(grade_values)
        return int(grade_values[idx])
    return int(max(grade_values))


def _shrink_targets_to_req_dynamic(target: dict[int, int], req: int) -> None:
    """target 합계가 req를 초과하면 감소시켜 req 이하로 맞춘다(가드)."""
    total = sum(target.values())
    if total <= req:
        return
    # 큰 값부터 1씩 줄이는 단순 방식(안정/예측 가능)
    while total > req:
        # 감소 우선순위: 현재 값이 큰 grade부터(동률이면 grade 큰 쪽부터 감소)
        g = max(target.keys(), key=lambda x: (target[x], x))
        if target[g] <= 0:
            break
        target[g] -= 1
        total -= 1


def _fill_targets_to_req_dynamic(target: dict[int, int], base_min: dict[int, int], req: int) -> None:
    """target 합계가 req보다 작으면 base_min이 큰 grade부터 채운다."""
    total = sum(target.values())
    if total >= req:
        return
    # base_min 큰 순으로 반복 증가(동률이면 grade 낮은 쪽 우선)
    order = sorted(target.keys(), key=lambda g: (-base_min.get(g, 0), g))
    i = 0
    while total < req:
        g = order[i % len(order)]
        target[g] += 1
        total += 1
        i += 1


def _available_by_grade_for_day_shift(
    rs: RosterSystem,
    by_grade: dict[int, list[int]],
    is_night_only: list[bool],
    day_idx: int,
    shift_code: str,
    join: list[int],
    leave: list[int],
) -> dict[int, int]:
    """해당 날짜/교대에서 grade별 가용 인원을 계산한다.

    정의(간단 버전):
        - join/leave 범위 내에 있는 간호사만 후보
        - 야간전담(is_night_nurse==3)은 D/E 불가로 제외
        - 고정셀은 변수로 이미 고정되어 있으므로, 여기서는 '후보 가능 여부'만 본다

    Returns:
        {grade: cnt, ...}
    """
    sc = str(shift_code or "").upper()
    avail = {g: 0 for g in by_grade.keys()}
    for g in by_grade.keys():
        cnt = 0
        for n in by_grade.get(g, []):
            if not (join[n] <= day_idx <= leave[n]):
                continue
            if sc in ("D", "E") and is_night_only[n]:
                continue
            cnt += 1
        avail[g] = cnt
    return avail


def _extract_grade_values_from_constraints(constraints_map: Any) -> list[int]:
    """constraints에서 등장하는 grade 키들을 정수로 추출해 오름차순으로 반환한다."""
    if not isinstance(constraints_map, dict):
        return []
    grades: set[int] = set()
    for _, grade_map in constraints_map.items():
        if not isinstance(grade_map, dict):
            continue
        for k in grade_map.keys():
            try:
                grades.add(int(k))
            except Exception:
                continue
    return sorted(grades)


def _snap_to_domain(value: int, domain: list[int]) -> int:
    """정수 value를 domain 내 가장 가까운 값으로 스냅한다(동률이면 작은 값)."""
    if not domain:
        return value
    return min(domain, key=lambda d: (abs(d - value), d))


def _parse_grade_config(grade_config: dict[str, Any]) -> tuple[dict, dict, dict]:
    """grade_config 딕셔너리에서 제약/정책/스케일 파라미터를 추출한다.

    Returns:
        (constraints_map, policy, scaling)
    """
    constraints_map = grade_config.get("constraints") or grade_config.get("constraints_json") or {}
    policy = {
        "null_grade_policy": str(grade_config.get("null_grade_policy") or "LOWEST").upper(),
    }
    min_ratio_floor = grade_config.get("min_ratio_floor", None)
    if min_ratio_floor is not None:
        try:
            min_ratio_floor = float(min_ratio_floor)
        except Exception:
            min_ratio_floor = None
    scaling = {
        "use_dynamic_scaling": bool(grade_config.get("use_dynamic_scaling", True)),
        "min_leader_keep": bool(grade_config.get("min_leader_keep", True)),
        "min_ratio_floor": min_ratio_floor,
        "allow_soft_fallback": bool(grade_config.get("allow_soft_fallback", True)),
        "grade_penalty_weight": int(grade_config.get("grade_penalty_weight", 500)),
    }
    return constraints_map, policy, scaling


def _build_grade_groups(
    rs: RosterSystem,
    grade_values: list[int],
    null_grade_policy: str,
) -> tuple[dict[int, list[int]], list[bool]]:
    """간호사 grade를 정의역(grade_values)에 맞게 매핑하고, grade별 인덱스 그룹을 만든다."""
    raw_grades = [_normalize_grade_int(getattr(n, "grade", None)) for n in rs.nurses]
    print('raw_grades', raw_grades)
    avg_grade = _average_grade_or_lowest(raw_grades, fallback=max(grade_values))
    print('avg_grade', avg_grade)
    mapped: list[int] = []
    for i, g in enumerate(raw_grades):
        if g in grade_values:
            mapped.append(g)  # type: ignore[arg-type]
            continue
        mapped.append(
            _resolve_null_or_unknown_grade(
                policy=null_grade_policy,
                nurse_db_id=str(getattr(rs.nurses[i], "db_id", i)),
                avg_grade=avg_grade,
                grade_values=grade_values,
            )
        )

    by_grade: dict[int, list[int]] = {g: [] for g in grade_values}
    for idx, g in enumerate(mapped):
        by_grade.setdefault(g, []).append(idx)

    is_night_only = [bool(getattr(n, "is_night_nurse", 0) == 3) for n in rs.nurses]
    return by_grade, is_night_only


def _get_need_map_for_day(cfg, ds_by_day: Any, day_idx: int) -> dict:
    """해당 일자의 필요 인원 맵을 반환한다(일자별 요구치 우선)."""
    need_map = None
    if isinstance(ds_by_day, list) and day_idx < len(ds_by_day):
        need_map = ds_by_day[day_idx]
    if isinstance(need_map, dict):
        return need_map
    return getattr(cfg, "daily_shift_requirements", {}) or {}


def _safe_int(value: Any) -> int:
    """값을 int로 안전 변환한다(실패 시 0)."""
    try:
        return int(value or 0)
    except Exception:
        return 0


def _parse_base_min(base: Any, grade_values: list[int]) -> dict[int, int]:
    """shift별 constraints 항목에서 grade별 최소 인원(base_min)을 파싱한다."""
    base_min: dict[int, int] = {g: 0 for g in grade_values}
    grade_map = base or {}
    if not isinstance(grade_map, dict):
        return base_min
    for k, v in grade_map.items():
        try:
            gi = int(k)
        except Exception:
            continue
        if gi not in grade_values:
            continue
        base_min[gi] = max(0, _safe_int(v))
    return base_min


def _compute_targets(
    base_min: dict[int, int],
    req: int,
    grade_values: list[int],
    use_dynamic_scaling: bool,
    min_ratio_floor: float | None,
    min_leader_keep: bool,
    by_grade: dict[int, list[int]],
) -> dict[int, int]:
    """base_min과 필요인원(req)을 기반으로 최종 target_min을 계산한다."""
    sum_base = sum(base_min.values())
    ratio = 1.0
    if use_dynamic_scaling and sum_base > 0:
        ratio = min(1.0, req / float(sum_base))
        if min_ratio_floor is not None:
            ratio = max(min_ratio_floor, ratio)
            ratio = min(1.0, ratio)

    target = {g: int(math.floor(base_min[g] * ratio)) for g in grade_values}

    # min_leader_keep: 리더는 grade=1로 정의 (constraints에 1이 없으면 미적용)
    if (
        min_leader_keep
        and 1 in grade_values
        and base_min.get(1, 0) > 0
        and req > 0
        and target.get(1, 0) == 0
        and len(by_grade.get(1, [])) > 0
    ):
        target[1] = 1

    _shrink_targets_to_req_dynamic(target, req)
    _fill_targets_to_req_dynamic(target, base_min, req)
    return target


def _clamp_targets_to_available(
    target: dict[int, int],
    available: dict[int, int],
    day_idx: int,
    shift_code: str,
    req: int,
) -> None:
    """target이 가용 인원을 초과하면 가능한 범위로 축소한다."""
    for g, t in list(target.items()):
        a = int(available.get(g, 0))
        if t <= a:
            continue
        # print(
        #     "[GradeConstraints] 경고: Grade 제약 불가능 → 클램프 적용: "
        #     f"day={day_idx+1}, shift={shift_code}, grade={g}, need={req}, "
        #     f"target={t} -> {a} (available)"
        # )
        target[g] = a


def _add_minimum_constraints(
    m,
    X,
    by_grade: dict[int, list[int]],
    day_idx: int,
    shift_idx: int,
    target: dict[int, int],
    allow_soft_fallback: bool,
    penalty_weight: int,
) -> list:
    """모델에 grade 최소 인원 제약을 추가한다.

    allow_soft_fallback=True 이면 slack을 허용하여 infeasible을 방지하고,
    False이면 기존처럼 하드 제약을 유지한다.
    """
    obj_terms: list = []
    for g, t in target.items():
        if int(t) <= 0:
            continue
        vars_sum = sum(X(n, day_idx, shift_idx) for n in by_grade.get(g, []))
        if allow_soft_fallback:
            # 부족분만큼 slack 허용 (0~t). 패널티는 상위 목적함수에서 추가 가능.
            slack = m.NewIntVar(0, int(t), f"grade_slack_d{day_idx}_s{shift_idx}_g{g}")
            m.Add(vars_sum + slack >= int(t))
            obj_terms.append(-penalty_weight * slack)
        else:
            m.Add(vars_sum >= int(t))
    return obj_terms


