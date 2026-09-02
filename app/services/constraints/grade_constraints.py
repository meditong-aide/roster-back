"""Grade(역량 등급) 제약 모듈.

- Team 알고리즘(목적함수)과 충돌 가능성이 있으므로, 상위 로직에서 전략(grade_strategy)에 따라
  이 제약을 완전히 ON/OFF 할 수 있도록 함수 형태로 분리합니다.
- 기존 CP-SAT 전체 방법론은 유지하고, Grade 관련 제약만 '추가'합니다.
"""

from __future__ import annotations

import math
from typing import Any

from services.roster_system import RosterSystem
from services.cp_sat.allowed_shift_types import is_n_only_profile


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
    _impact_modes = getattr(rs, "_constraint_impact_constraint_modes", None)
    if _impact_modes is None:
        _impact_modes = []
        setattr(rs, "_constraint_impact_constraint_modes", _impact_modes)
    if grade_config is None:
        return []

    constraints_map, max_constraints_map, scaling = _parse_grade_config(grade_config)
    # [GradeCascade] config 의 grade 인원이 0이면 다음 상위(존재) grade 로 '시니어 ≥N' 요구 이양.
    #   grade 1 없으면 2, 2 없으면 3... — '설정 grade 부재'만으로 hard 가 헛 infeasible 나는 것 방지.
    #   min 만 적용(max=anti-pair 상한은 이양하면 의미 왜곡).
    constraints_map = _cascade_constraints_to_existing_grades(rs, constraints_map)
    _impact_modes.append({
        "family": "grade_min",
        "key": "grade_min:global",
        "configured_mode": "soft" if scaling["allow_soft_fallback"] else "hard",
        "effective_mode": "soft_fallback" if scaling["allow_soft_fallback"] else "enforced",
        "source_file": "app/services/constraints/grade_constraints.py",
        "reason": "grade constraints active",
        "evidence": {"strategy": str(grade_strategy or "").upper()},
    })
    _impact_modes.append({
        "family": "grade_max",
        "key": "grade_max:global",
        "configured_mode": "soft" if scaling["allow_soft_fallback"] else "hard",
        "effective_mode": "soft_fallback" if scaling["allow_soft_fallback"] else "enforced",
        "source_file": "app/services/constraints/grade_constraints.py",
        "reason": "grade constraints active",
        "evidence": {"strategy": str(grade_strategy or "").upper()},
    })
    print('constraints_map', constraints_map)
    print('max_constraints_map', max_constraints_map)
    grade_values = _extract_grade_values_from_constraints(constraints_map, max_constraints_map)
    print('grade_values', grade_values)
    if not grade_values:
        return []

    by_grade, is_night_only = _build_grade_groups(
        rs=rs,
        grade_values=grade_values,
    )
    print('by_grade', by_grade)
    print('is_night_only', is_night_only)
    cfg = rs.config
    ds_by_day = getattr(cfg, "daily_shift_requirements_by_day", None)
    apply_shifts = {"D", "E", "N"}
    if bool(getattr(cfg, "use_mid", False)):
        apply_shifts.add("M")

    obj_terms: list = []
    for d in range(rs.num_days):
        need_map = _get_need_map_for_day(cfg, ds_by_day, d)
        # (1) Minimum 제약 처리
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

            target = _compute_targets(
                base_min=base_min,
                req=req,
                grade_values=grade_values,
                use_dynamic_scaling=scaling["use_dynamic_scaling"],
                min_ratio_floor=scaling["min_ratio_floor"],
                min_leader_keep=scaling["min_leader_keep"],
                by_grade=by_grade,
            )

            # hard grade 제약 보장을 위해 target을 가용치로 내리는 clamp를 비활성화한다.

            obj_terms.extend(
                _add_minimum_constraints(
                    m=m,
                    X=X,
                    by_grade=by_grade,
                    day_idx=d,
                    shift_idx=s_idx,
                    shift_code=s_code,
                    target=target,
                    allow_soft_fallback=scaling["allow_soft_fallback"],
                    penalty_weight=scaling["grade_penalty_weight"],
                )
            )

        # (2) Maximum 제약 처리 (anti-pair 등)
        for shift_code, base in (max_constraints_map or {}).items():
            s_code = str(shift_code or "").upper()
            if s_code not in apply_shifts:
                continue
            if s_code not in rs.config.shift_types:
                continue
            max_by_grade = _parse_base_max(base, grade_values)
            if not any(v >= 0 and k in by_grade for k, v in max_by_grade.items()):
                continue
            s_idx = rs.config.shift_types.index(s_code)
            obj_terms.extend(
                _add_maximum_constraints(
                    m=m,
                    X=X,
                    by_grade=by_grade,
                    day_idx=d,
                    shift_idx=s_idx,
                    shift_code=s_code,
                    max_by_grade=max_by_grade,
                    allow_soft_fallback=scaling["allow_soft_fallback"],
                    penalty_weight=scaling["grade_penalty_weight"],
                )
            )

    # ── 월 합계 hard(near) 제약 ──
    # per-day는 soft penalty(KLD 자유도 유지)로 두되, 월 합계는 10× weight로 사실상 강제.
    # 시화병원처럼 grade 인원 vs demand가 빡센 케이스에서 grade 비율 100% 보장 목적.
    if bool(getattr(cfg, "grade_monthly_hard", False)):
        monthly_target_min: dict[tuple[str, int], int] = {}
        for d in range(rs.num_days):
            need_map = _get_need_map_for_day(cfg, ds_by_day, d)
            for shift_code, base in (constraints_map or {}).items():
                s_code = str(shift_code or "").upper()
                if s_code not in apply_shifts or s_code not in rs.config.shift_types:
                    continue
                req = _safe_int(need_map.get(s_code, 0))
                if req <= 0:
                    continue
                base_min = _parse_base_min(base, grade_values)
                if sum(base_min.values()) <= 0:
                    continue
                t_d = _compute_targets(
                    base_min=base_min,
                    req=req,
                    grade_values=grade_values,
                    use_dynamic_scaling=scaling["use_dynamic_scaling"],
                    min_ratio_floor=scaling["min_ratio_floor"],
                    min_leader_keep=scaling["min_leader_keep"],
                    by_grade=by_grade,
                )
                for g, t in t_d.items():
                    if t > 0:
                        key = (s_code, g)
                        monthly_target_min[key] = monthly_target_min.get(key, 0) + int(t)

        monthly_weight = scaling["grade_penalty_weight"] * 10
        for (s_code, g), m_t in monthly_target_min.items():
            s_idx = rs.config.shift_types.index(s_code)
            nurses_g = by_grade.get(g, [])
            if not nurses_g:
                continue
            monthly_sum = sum(
                X(n, d, s_idx)
                for n in nurses_g
                for d in range(rs.num_days)
                if join[n] <= d <= leave[n]
            )
            slack = m.NewIntVar(0, int(m_t), f"grade_monthly_slack_{s_code}_g{g}")
            m.Add(monthly_sum + slack >= int(m_t))
            obj_terms.append(-monthly_weight * slack)
        print(
            f"[GradeConstraints] 월 합계 soft(near-hard) 추가: "
            f"{len(monthly_target_min)} 조합, weight={monthly_weight}"
        )

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
        - 야간전담(allowed_shifts==3)은 D/E 불가로 제외
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


def _extract_grade_values_from_constraints(*constraints_maps: Any) -> list[int]:
    """여러 constraints 맵(min/max 등)에서 등장하는 grade 키를 합쳐 정수 오름차순으로 반환한다."""
    grades: set[int] = set()
    for cmap in constraints_maps:
        if not isinstance(cmap, dict):
            continue
        for _, grade_map in cmap.items():
            if not isinstance(grade_map, dict):
                continue
            for k in grade_map.keys():
                try:
                    grades.add(int(k))
                except Exception:
                    continue
    return sorted(grades)


def _cascade_constraints_to_existing_grades(rs: RosterSystem, constraints_map: Any) -> Any:
    """min constraints_map({shift:{grade:count}}) 에서 '실제 인원이 0인 grade' 의 요구를
    다음 상위(숫자 큰) '인원이 있는' grade 로 이양(cascade)한다.

    정책: grade 1 = 최상위(시니어). grade 1 인원이 없으면 2, 2도 없으면 3... 으로
    '시니어 ≥ N' 요구가 내려간다. 더 내려갈 등급이 없으면 원래 grade 유지(진짜 불가는 노출).
      - 인원이 있는 grade 는 그대로.
      - 같은 목표 grade 로 겹치면 max 로 합쳐 demand 부풀림 방지('1씩' 의미 유지).
      - max constraints(anti-pair 상한)에는 적용하지 않는다(상한 이양은 의미 왜곡).

    '설정한 grade 가 그 병동에 없다'는 이유만으로 hard 제약이 헛 infeasible 나는 것을 막는다.
    """
    if not isinstance(constraints_map, dict) or not constraints_map:
        return constraints_map
    pop: dict[int, int] = {}
    for n in rs.nurses:
        g = _normalize_grade_int(getattr(n, "grade", None))
        if g is not None:
            pop[g] = pop.get(g, 0) + 1
    existing = sorted(g for g, c in pop.items() if c > 0)
    if not existing:
        return constraints_map

    def _target_grade(g: int) -> int:
        if pop.get(g, 0) > 0:
            return g
        higher = [e for e in existing if e > g]
        return higher[0] if higher else g

    out: dict = {}
    changed = False
    for shift, grade_map in constraints_map.items():
        if not isinstance(grade_map, dict):
            out[shift] = grade_map
            continue
        new_gm: dict = {}
        for gk, cnt in grade_map.items():
            gi = _normalize_grade_int(gk)
            if gi is None:
                new_gm[gk] = cnt
                continue
            tg = _target_grade(gi)
            if tg != gi:
                changed = True
            try:
                c = int(cnt)
            except Exception:
                new_gm[tg] = cnt
                continue
            new_gm[tg] = max(new_gm[tg], c) if isinstance(new_gm.get(tg), int) else c
        out[shift] = new_gm
    if changed:
        print(f"[GradeCascade] 인원 0 grade → 상위 존재 grade 이양: {constraints_map} → {out}")
    return out


def _parse_grade_config(grade_config: dict[str, Any]) -> tuple[dict, dict, dict]:
    """grade_config 딕셔너리에서 제약/스케일 파라미터를 추출한다.

    Returns:
        (constraints_map, max_constraints_map, scaling)

    Notes:
        - `constraints` / `constraints_json`: shift별 grade 최소 인원(min)
        - `constraints_max` / `constraints_max_json`: shift별 grade 최대 인원(max, anti-pair 용)
        - max는 옵션(미설정 시 빈 dict). min만 있던 구조와 하위호환 유지.
    """
    constraints_map = grade_config.get("constraints") or grade_config.get("constraints_json") or {}
    max_constraints_map = (
        grade_config.get("constraints_max")
        or grade_config.get("constraints_max_json")
        or {}
    )
    min_ratio_floor = grade_config.get("min_ratio_floor", None)
    if min_ratio_floor is not None:
        try:
            min_ratio_floor = float(min_ratio_floor)
        except Exception:
            min_ratio_floor = None
    scaling = {
        "use_dynamic_scaling": _to_bool(grade_config.get("use_dynamic_scaling", True), True),
        "min_leader_keep": _to_bool(grade_config.get("min_leader_keep", True), True),
        "min_ratio_floor": min_ratio_floor,
        "allow_soft_fallback": _to_bool(grade_config.get("allow_soft_fallback", False), False),
        "grade_penalty_weight": int(grade_config.get("grade_penalty_weight", 160000)),
    }
    return constraints_map, max_constraints_map, scaling


def _build_grade_groups(
    rs: RosterSystem,
    grade_values: list[int],
) -> tuple[dict[int, list[int]], list[bool]]:
    """간호사 grade를 정의역(grade_values)에 맞게 매핑하고, grade별 인덱스 그룹을 만든다.

    정의역(grade_values) 밖이거나 NULL인 grade는 grade min/max 집계에서 완전히 빠진다.
    이들은 다른 제약(커버리지 등)에 의해 자유롭게 배정되며, grade 분배에는 영향이 없다.
    """
    raw_grades = [_normalize_grade_int(getattr(n, "grade", None)) for n in rs.nurses]
    by_grade: dict[int, list[int]] = {g: [] for g in grade_values}
    for idx, g in enumerate(raw_grades):
        if g in grade_values:
            by_grade.setdefault(g, []).append(idx)

    is_night_only = [is_n_only_profile(getattr(n, "allowed_shifts", None)) for n in rs.nurses]
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


def _to_bool(value: Any, default: bool = False) -> bool:
    """문자열/숫자/불리언 입력을 안전하게 bool로 변환한다."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "t", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "f", "no", "n", "off", ""}:
            return False
    return bool(value)


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


def _parse_base_max(base: Any, grade_values: list[int]) -> dict[int, int]:
    """shift별 constraints_max 항목에서 grade별 최대 인원(base_max)을 파싱한다.

    Notes:
        - 값이 -1 (설정 안 함)이면 제외.
        - grade_values에 포함되지 않은 grade 키는 무시.
    """
    base_max: dict[int, int] = {}
    grade_map = base or {}
    if not isinstance(grade_map, dict):
        return base_max
    for k, v in grade_map.items():
        try:
            gi = int(k)
        except Exception:
            continue
        if gi not in grade_values:
            continue
        mv = _safe_int(v)
        if mv < 0:
            continue
        base_max[gi] = mv
    return base_max


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


# [GRADE_CASCADE] ②: 누적 등급 cascade. 요구 시니어(grade g)를 못 채우면 한 단계씩
#   하위 등급으로 내려가며(grade g → g+1 → ...) "가능한 최고 등급"을 시프트마다 보장한다.
#   - off 0(요구 등급 부재): penalty_weight(호출자) = x/y '아래' → x/y 우선, 시니어 과로 안 함.
#   - off 1(차상위까지 부재): W_NEXT = x/y '위' → 한 단계 떨어지는 건 강하게 억제.
#   - off>=2(시니어 전무/인원부족): W_NONE = 사실상 hard.
#   → "x/y 우선 + 최고등급 최대 + 혼자 안 뜀 + infeasible 안전" 동시 충족(단일 패스).
#   원복: _GRADE_CASCADE_ENABLED=False 로 두면 아래 레거시(binary hard/soft) 경로로 복귀.
_GRADE_CASCADE_ENABLED = True
GRADE_CASCADE_W_NEXT = 2_000_000   # 차상위까지 부재 (x/y range 1.2M 위 → near-hard)
GRADE_CASCADE_W_NONE = 6_000_000   # 시니어 전무 (사실상 hard)

# ※ '같은 자리에 동일 등급이 목표 초과로 몰리는 것'에 페널티를 걸어 미달을 줄이려는
#   시도를 했다가 **되돌렸다**(2026-08-31). 실측에서 거꾸로 나빠졌다 —
#   시화 중환자실 2026-08 · 초과 40,000 부여 시 grade 미달 21.0 → 26.0.
#   총량이 고정이면 '뭉침 1건 = 다른 날 부족 1건' 이라 부족 페널티가 이미 그걸 벌하고 있고,
#   초과에 값을 더 매기면 solver 가 '뭉치지 않으려 미달을 감수하는' 쪽으로 기운다.
#   ★ 그렇다고 "탐색 시간이 원인" 이라고 단정하지도 말 것. tl3 를 45초로 늘린 3회 표본에서
#     미달 23.3 → 15.7 로 보였으나, 총 시간 20~240초 30회 스윕에서는 **재현되지 않았다**.
#     같은 조건이 15~27 로 흔들려 3회 평균 비교로는 판정이 불가능하다.
#     확실한 것은 stage3 가 늘 FEASIBLE 로 끝나고(기본 tl3=12초) 잔여 gap 이 시간에 따라
#     55%→8% 로 단조 감소한다는 것뿐이며, 그 gap 감소가 근무표 품질로 이어지는지는 미확정이다.


def _add_minimum_constraints(
    m,
    X,
    by_grade: dict[int, list[int]],
    day_idx: int,
    shift_idx: int,
    shift_code: str,
    target: dict[int, int],
    allow_soft_fallback: bool,
    penalty_weight: int,
) -> list:
    """모델에 grade 최소 인원 제약을 추가한다.

    기본(_GRADE_CASCADE_ENABLED): 누적 등급 cascade — 요구 등급을 못 채우면 하위 등급이
      대체하되 등급이 내려갈수록 패널티가 가팔라진다(off0<off1<off2). 단일 solve.
    레거시(flag False): allow_soft_fallback=True면 slack soft, False면 하드.
    """
    obj_terms: list = []
    if _GRADE_CASCADE_ENABLED:
        grade_domain = sorted(by_grade.keys())  # 오름차순: 1(최상위)..N
        for g, t in target.items():
            t = int(t)
            if t <= 0:
                continue
            ceilings = [c for c in grade_domain if c >= g]
            if not ceilings:
                # 요구 등급이 도메인에 없음 → 안전하게 그 등급 합으로 하드(기존 동작).
                m.Add(sum(X(n, day_idx, shift_idx) for n in by_grade.get(g, [])) >= t)
                continue
            for off, c in enumerate(ceilings):
                # ceiling c 까지(=grade ≤ c, 더 시니어 등급 포함) 누적 인원.
                members = [
                    n for j in grade_domain if j <= c for n in by_grade.get(j, [])
                ]
                if not members:
                    continue
                cum = sum(X(n, day_idx, shift_idx) for n in members)
                short = m.NewIntVar(
                    0, t, f"gcasc_d{day_idx}_s{shift_idx}_g{g}_c{c}"
                )
                m.Add(short >= t - cum)  # short = max(0, t - 누적)
                if off == 0:
                    w = penalty_weight
                    # [GradeOff0] "요구 등급 자체의 미달분"만 따로 모아 모델에 붙인다.
                    #   fallback lex 의 Stage 1/2 목적함수가 이걸 최소화한다. cascade 의
                    #   대체 단계(off≥1: 2M/6M)는 team(30만)·safety(10~30만)를 압도하므로
                    #   Stage 2 에 넣으면 lex 우선순위가 뒤집힌다 → off=0 만 노출한다.
                    _off0 = getattr(m, "_grade_off0_shorts", None)
                    if _off0 is None:
                        _off0 = []
                        setattr(m, "_grade_off0_shorts", _off0)
                    _off0.append(short)
                    # [GradeCellSpec] 같은 요구를 lex 재배치 패스가 별도 모델(m2)에서
                    #   다시 세울 수 있도록 (일자, 시프트, 목표, 대상 인덱스) 를 남긴다.
                    #   목표 계산(_compute_targets·need_map)을 lex 쪽에서 재현하면 로직이
                    #   갈라지므로 여기서 만든 값을 그대로 넘긴다.
                    _spec = getattr(m, "_grade_cell_spec", None)
                    if _spec is None:
                        _spec = []
                        setattr(m, "_grade_cell_spec", _spec)
                    _spec.append((day_idx, shift_idx, t, tuple(members)))
                elif off == 1:
                    w = GRADE_CASCADE_W_NEXT
                else:
                    w = GRADE_CASCADE_W_NONE
                obj_terms.append(-w * short)
        return obj_terms
    # MUS 추출용 hard assumption registry — 모델에 attach 된 경우만 wrap.
    _registry = getattr(m, "_cpsat_assumption_registry", None)
    _add_hard_fn = None
    if _registry is not None and not allow_soft_fallback:
        try:
            from services.cp_sat.hard_assumption import add_hard as _add_hard_fn
        except Exception:
            _add_hard_fn = None
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
            if _add_hard_fn is not None and _registry is not None:
                # 같은 (grade, shift) 의 모든 날짜를 하나의 assumption literal로 묶음.
                # → MUS 추출 시 "grade {g} {shift_code} 최소치"라는 단일 코어로 묶여 노출.
                _sc = str(shift_code or "").upper()
                _name = f"GradeMin:{_sc}:grade_{g}"
                _meta = {
                    "node_id": f"grade_min:{_sc.lower()}:grade_{g}",
                    "type": "GradeMinNode",
                    "label": f"Grade {g} {_sc} min ≥ {int(t)}",
                    "value": {"grade": g, "shift": _sc, "min": int(t)},
                    "scope": "grade",
                    "scope_key": f"grade_{g}_{_sc.lower()}_min",
                    "pattern": "grade_min",
                    "human_message_ko": f"Grade {g} 등급의 {_sc} 시프트 최소 인원 정책",
                    "resolution_hint": (
                        f"Grade {g} 의 {_sc} 최소치({int(t)})를 낮추거나 "
                        f"grade_min 을 soft fallback 으로 전환하세요."
                    ),
                }
                _add_hard_fn(m, _registry, name=_name, constraint_expr=(vars_sum >= int(t)), meta=_meta)
            else:
                m.Add(vars_sum >= int(t))
    return obj_terms


def _add_maximum_constraints(
    m,
    X,
    by_grade: dict[int, list[int]],
    day_idx: int,
    shift_idx: int,
    shift_code: str,
    max_by_grade: dict[int, int],
    allow_soft_fallback: bool,
    penalty_weight: int,
) -> list:
    """모델에 grade 최대 인원 제약을 추가한다 (anti-pair 등).

    각 grade g에 대해 해당 (day, shift)의 배정 수가 max_by_grade[g]를 넘지 않도록 제약한다.
    allow_soft_fallback=True 이면 초과분 slack을 허용하며 패널티로 억제,
    False이면 하드 제약 (`vars_sum <= max_t`).
    """
    obj_terms: list = []
    # MUS 추출용 hard assumption registry — 모델에 attach 된 경우만 wrap.
    _registry = getattr(m, "_cpsat_assumption_registry", None)
    _add_hard_fn = None
    if _registry is not None and not allow_soft_fallback:
        try:
            from services.cp_sat.hard_assumption import add_hard as _add_hard_fn
        except Exception:
            _add_hard_fn = None
    for g, max_t in max_by_grade.items():
        nurses = by_grade.get(g, [])
        if not nurses:
            continue
        upper = len(nurses)
        mt = int(max_t)
        if mt >= upper:
            # 이미 trivially 성립 — 제약 생략
            continue
        vars_sum = sum(X(n, day_idx, shift_idx) for n in nurses)
        if allow_soft_fallback:
            # 초과분만큼 slack 허용 (0~upper-mt). 패널티로 목적함수에서 억제.
            slack_over = m.NewIntVar(
                0, max(0, upper - mt), f"grade_max_slack_d{day_idx}_s{shift_idx}_g{g}"
            )
            m.Add(vars_sum - slack_over <= mt)
            obj_terms.append(-penalty_weight * slack_over)
        else:
            if _add_hard_fn is not None and _registry is not None:
                _sc = str(shift_code or "").upper()
                _name = f"GradeMax:{_sc}:grade_{g}"
                _meta = {
                    "node_id": f"grade_max:{_sc.lower()}:grade_{g}",
                    "type": "GradeMaxNode",
                    "label": f"Grade {g} {_sc} max ≤ {mt}",
                    "value": {"grade": g, "shift": _sc, "max": mt},
                    "scope": "grade",
                    "scope_key": f"grade_{g}_{_sc.lower()}_max",
                    "pattern": "grade_max",
                    "human_message_ko": f"Grade {g} 등급의 {_sc} 시프트 최대 인원 정책",
                    "resolution_hint": (
                        f"Grade {g} 의 {_sc} 최대치({mt})를 높이거나 "
                        f"grade_max 을 soft fallback 으로 전환하세요."
                    ),
                }
                _add_hard_fn(m, _registry, name=_name, constraint_expr=(vars_sum <= mt), meta=_meta)
            else:
                m.Add(vars_sum <= mt)
    return obj_terms
