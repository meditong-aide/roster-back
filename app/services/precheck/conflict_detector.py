"""Conflict core detector — 다중 hard 제약이 결합해 infeasible을 만들 때
    그 충돌 코어를 구조화된 형식으로 추출한다.

ontology dashboard는 이 결과를 ConflictCoreNode + member constraints +
DerivedCellNode + RESOLUTION_HINT 엣지로 시각화한다.

출력 형식 (each core):
    {
        "core_id":       "conflict:nurse_<idx>:<pattern>",
        "scope":         "nurse" | "day" | "month" | "global",
        "pattern":       "n_only_vs_caps" | "n_exact_1_no_n_tail" | ...,
        "nurse_id":      str | None,
        "members": [
            {"node_id", "type", "label", "value", "human_message_ko"}
        ],
        "derivation": [   # 추론 단계
            {"step": int, "from": str, "infer": str}
        ],
        "conclusion":    str,
        "resolution_hints": [
            {"action": str, "params": dict, "human_message_ko": str}
        ],
        "human_message_ko": str,
    }

확장 방법:
    새 detector 함수를 추가하고 `run_conflict_detectors`의 리스트에 등록.
    detector는 (roster_system) → list[dict] 반환.
"""

from __future__ import annotations

from typing import Any, List, Dict


def _safe_attr(obj: Any, name: str, default: Any = None) -> Any:
    try:
        return getattr(obj, name, default)
    except Exception:
        return default


def _forbidden_shift_set(rs: Any, nurse_idx: int) -> set[str]:
    """nurse_idx 가 전 기간 동안 절대 못 박는 shift 코드 집합 반환.

    initial_forbidden[(n, d)] = set/list of forbidden shifts (e.g., {"D","E"}).
    nurse_idx가 31일 내내 동일 shift code를 금지받으면 그 shift는 절대 못 박음.
    """
    initial_forbidden = _safe_attr(rs, "initial_forbidden", {}) or {}
    num_days = int(_safe_attr(rs, "num_days", 0) or 0)
    if not isinstance(initial_forbidden, dict) or num_days <= 0:
        return set()
    per_day_forbidden = {}
    for (n, d), forb in initial_forbidden.items():
        if n != nurse_idx:
            continue
        per_day_forbidden[d] = set(forb) if hasattr(forb, "__iter__") else {forb}
    # n번 nurse가 31일 내내 같은 shift가 금지된 코드 집합
    if not per_day_forbidden:
        return set()
    # 모든 day에 공통으로 금지된 shift만 추출
    common = None
    for d in range(num_days):
        forb_d = per_day_forbidden.get(d, set())
        common = forb_d if common is None else (common & forb_d)
        if not common:
            return set()
    return common or set()


def detect_nurse_overconstrained(rs: Any) -> List[Dict[str, Any]]:
    """N-only / D-only 등 시프트 가용성이 좁은 nurse가 OFF cap, night cap 등과 충돌하는 케이스 감지.

    수식:
        - allowed_shifts = shift_types - forbidden(전 기간 공통)
        - 31 = D + E + N + OFF
        - allowed에 D/E가 없으면 D=E=0 → N + OFF = 31
        - OFF ≤ max_off_per_nurse, N ≤ max_night_shifts_per_month
        - n_exact 가 있다면 N = n_exact
        - 셋이 만족할 N 구간이 빈집합이면 infeasible
    """
    cores: List[Dict[str, Any]] = []
    cfg = _safe_attr(rs, "config", None)
    nurses = list(_safe_attr(rs, "nurses", []) or [])
    if cfg is None or not nurses:
        return cores

    num_days = int(_safe_attr(rs, "num_days", 0) or 0)
    if num_days <= 0:
        return cores

    shift_types = list(_safe_attr(cfg, "shift_types", []) or [])
    work_shifts = [s for s in shift_types if str(s).upper() != "O"]

    max_off_arr = _safe_attr(rs, "max_off_per_nurse", None) or []
    max_night_global = int(_safe_attr(cfg, "max_night_shifts_per_month", 0) or 0)

    for idx, nu in enumerate(nurses):
        forbidden_all = _forbidden_shift_set(rs, idx)
        allowed_work = [s for s in work_shifts if s not in forbidden_all]

        # 시프트 1개로 제한된 nurse만 본 detector 대상 (광범위 충돌 발생 환경).
        if len(allowed_work) >= 2:
            continue
        # work shift 0개면 OFF만 가능 — 이건 다른 detector 영역
        if not allowed_work:
            continue
        sole_shift = allowed_work[0]

        # CP-SAT 솔버의 실제 OFF 상한 규칙:
        #   - 일반 nurse: max_off_per_nurse[idx] (= max_off_arr 값)
        #   - N-only nurse: max(0, avail_days - max_night_shifts_per_month)
        #                   (cp_sat_basic.py:3583 의 "N전담 예외" 분기와 정합)
        is_night_only = sole_shift.upper() == "N"
        raw_max_off = int(max_off_arr[idx]) if idx < len(max_off_arr) else None
        if is_night_only and max_night_global > 0:
            max_off = max(0, num_days - max_night_global)
            max_off_source = f"avail_days({num_days}) - max_night({max_night_global})"
        else:
            max_off = raw_max_off
            max_off_source = "max_off_per_nurse"

        n_exact = _safe_attr(nu, "n_exact", None)
        n_min = _safe_attr(nu, "n_min", None)
        n_max_nurse = _safe_attr(nu, "n_max", None)
        # work 가능 일수 = num_days - OFF
        min_work_days = num_days - (max_off if max_off is not None else num_days)
        max_work_days = num_days  # OFF 0일 가정

        if is_night_only:
            # N + OFF = 31
            n_lower = min_work_days
            n_upper = num_days if max_night_global <= 0 else min(num_days, max_night_global)
            if n_max_nurse is not None:
                n_upper = min(n_upper, int(n_max_nurse))
            if n_min is not None:
                n_lower = max(n_lower, int(n_min))
            if n_exact is not None:
                n_lower = n_upper = int(n_exact)

            # 추가 검산: OFF 범위 — N + OFF = num_days, OFF ≤ max_off
            # 최소 OFF = num_days - n_upper. 이 값이 max_off보다 크면 또한 모순.
            min_off_required = num_days - n_upper
            off_conflict = (max_off is not None) and (min_off_required > int(max_off))

            if n_lower > n_upper or off_conflict:
                # 모순
                members = [
                    {
                        "node_id": f"nurse_role:N_only:nurse_{idx}",
                        "type": "NurseRoleNode",
                        "label": "N-only role",
                        "value": "N-only",
                        "human_message_ko": f"이 간호사는 31일 내내 D/E를 박을 수 없는 N 전담 인력입니다 (initial_forbidden에서 derive).",
                    },
                    {
                        "node_id": f"off_cap:nurse_{idx}",
                        "type": "OffCapNode",
                        "label": "max_off (effective)",
                        "value": max_off,
                        "human_message_ko": (
                            f"이 간호사 최대 OFF {max_off}일 ({max_off_source}). "
                            f"→ N ≥ {min_work_days} 필요."
                        ),
                    },
                ]
                if max_night_global > 0:
                    members.append({
                        "node_id": "night_cap:global",
                        "type": "NightCapNode",
                        "label": "max_night_shifts_per_month",
                        "value": max_night_global,
                        "human_message_ko": f"월간 N 상한 {max_night_global}일 → N ≤ {max_night_global}.",
                    })
                if n_exact is not None:
                    members.append({
                        "node_id": f"monthly_n_exact:nurse_{idx}",
                        "type": "MonthlyNExactNode",
                        "label": "n_exact",
                        "value": n_exact,
                        "human_message_ko": f"월 N 정확히 {n_exact}회 강제.",
                    })

                derivation = [
                    {"step": 1, "from": "N-only role", "infer": f"D=0, E=0 (over {num_days} days)"},
                    {"step": 2, "from": f"max_off={max_off} ({max_off_source})", "infer": f"OFF ≤ {max_off} → N ≥ {min_work_days}"},
                ]
                if max_night_global > 0:
                    derivation.append({"step": 3, "from": f"max_night_shifts_per_month={max_night_global}", "infer": f"N ≤ {max_night_global}"})
                if n_exact is not None:
                    derivation.append({"step": 4, "from": f"n_exact={n_exact}", "infer": f"N = {n_exact} (강제)"})
                if off_conflict:
                    derivation.append({"step": 90, "from": "OFF = num_days - N", "infer": f"min_OFF = {num_days} - {n_upper} = {min_off_required}"})
                    derivation.append({"step": 99, "conclusion": f"min_OFF({min_off_required}) > max_off_per_nurse({max_off}) — 빈집합"})
                else:
                    derivation.append({"step": 99, "conclusion": f"필요 N 구간 [{n_lower}, {n_upper}] — 빈집합"})

                hints: List[Dict[str, Any]] = []
                if max_night_global > 0:
                    hints.append({
                        "action": "increase_max_night_shifts_per_month",
                        "params": {"from": max_night_global, "to_min": min_work_days},
                        "human_message_ko": f"월간 N 상한을 {max_night_global} → {min_work_days}+ 로 늘리세요.",
                    })
                if max_off is not None:
                    needed_off = num_days - n_upper
                    hints.append({
                        "action": "increase_max_off_per_nurse",
                        "params": {"nurse_idx": idx, "from": max_off, "to_min": needed_off},
                        "human_message_ko": f"이 간호사 OFF 상한을 {max_off} → {needed_off}+ 로 늘리세요.",
                    })
                hints.append({
                    "action": "expand_nurse_role",
                    "params": {"nurse_idx": idx, "current": "N-only", "to": "N + (D or E)"},
                    "human_message_ko": "이 간호사 role을 D 또는 E도 가능한 다중 시프트로 변경하세요.",
                })
                if n_exact is not None:
                    hints.append({
                        "action": "relax_n_exact",
                        "params": {"nurse_idx": idx, "current": n_exact},
                        "human_message_ko": f"이 간호사 월간 N exact={n_exact} 제약을 풀거나 범위로 바꾸세요.",
                    })

                if off_conflict:
                    _conclusion = (
                        f"{sole_shift}-only + n_exact={n_exact} → OFF 필요 {min_off_required}일 "
                        f"> 상한 {max_off}일"
                    )
                else:
                    _conclusion = (
                        f"31 = N + OFF (work_shifts = {sole_shift}만) — N ∈ [{n_lower}, {n_upper}] = ∅"
                    )
                _nurse_id_str = str(_safe_attr(nu, "nurse_id", idx))
                cores.append({
                    "core_id": f"conflict:nurse_{idx}:n_only_vs_caps",
                    "scope": "nurse",
                    "pattern": "n_only_vs_caps",
                    "nurse_id": _nurse_id_str,
                    "affected_count": 1,
                    "affected_nurse_ids": [_nurse_id_str],
                    "members": members,
                    "derivation": derivation,
                    "conclusion": _conclusion,
                    "resolution_hints": hints,
                    "human_message_ko": (
                        f"간호사 idx={idx}: N 전담이면서 OFF/N 상한이 동시 너무 좁아 가능한 배정이 없습니다."
                    ),
                })
    return cores


def detect_capacity_shortage(rs: Any) -> List[Dict[str, Any]]:
    """월 총 work 수요 vs 공급 산술 검증 — _build_infeasible_diagnosis의
    CAPACITY_TOTAL_SHORTAGE / N_CAPACITY_SHORTAGE 로직을 ConflictCore 형식으로 재산출.
    """
    cores: List[Dict[str, Any]] = []
    cfg = _safe_attr(rs, "config", None)
    nurses = list(_safe_attr(rs, "nurses", []) or [])
    if cfg is None or not nurses:
        return cores

    num_days = int(_safe_attr(rs, "num_days", 0) or 0)
    if num_days <= 0:
        return cores
    nurse_count = len(nurses)

    shift_types = list(_safe_attr(cfg, "shift_types", []) or [])
    by_day = _safe_attr(cfg, "daily_shift_requirements_by_day", None)
    base_req = _safe_attr(cfg, "daily_shift_requirements", None) or {}

    def _required_by_day(day_idx: int) -> Dict[str, int]:
        if isinstance(by_day, list) and day_idx < len(by_day) and isinstance(by_day[day_idx], dict):
            return {str(k).upper(): int(v or 0) for k, v in by_day[day_idx].items()}
        if isinstance(base_req, dict):
            return {str(k).upper(): int(v or 0) for k, v in base_req.items()}
        return {}

    total_required = 0
    n_required = 0
    for d in range(num_days):
        for code, val in _required_by_day(d).items():
            if code == "O":
                continue
            if shift_types and code not in shift_types:
                continue
            v = int(val or 0)
            if v <= 0:
                continue
            total_required += v
            if code == "N":
                n_required += v

    max_off_arr = _safe_attr(rs, "max_off_per_nurse", None) or []
    avg_max_off = sum(int(x or 0) for x in max_off_arr) / max(1, len(max_off_arr))
    max_work_per_nurse = max(0, num_days - int(avg_max_off))
    total_capacity = nurse_count * max_work_per_nurse

    if total_required > total_capacity:
        cores.append({
            "core_id": "conflict:capacity:total",
            "scope": "global",
            "pattern": "capacity_total_shortage",
            "affected_count": nurse_count,
            "affected_nurse_ids": [str(_safe_attr(nu, "nurse_id", i)) for i, nu in enumerate(nurses)],
            "members": [
                {"node_id": "demand:total_work", "type": "ConstraintNode",
                 "label": "월 총 work 요구", "value": total_required,
                 "human_message_ko": f"월간 총 work 요구 {total_required} cells"},
                {"node_id": "supply:total_work", "type": "ConstraintNode",
                 "label": "월 총 work 공급", "value": total_capacity,
                 "human_message_ko": f"공급 가능 work {total_capacity} cells ({nurse_count}명 × {max_work_per_nurse}일)"},
            ],
            "derivation": [
                {"step": 1, "from": "daily_shift_requirements", "infer": f"총 work 요구 = {total_required}"},
                {"step": 2, "from": "nurse_count × (num_days - avg_max_off)", "infer": f"공급 = {nurse_count} × {max_work_per_nurse} = {total_capacity}"},
                {"step": 99, "conclusion": f"요구({total_required}) > 공급({total_capacity}) — 산술 부족"},
            ],
            "conclusion": f"월 work 요구 {total_required} > 공급 상한 {total_capacity}",
            "resolution_hints": [
                {"action": "reduce_daily_shift_requirements",
                 "human_message_ko": "daily_shift_requirements 를 낮추세요."},
                {"action": "add_nurses",
                 "human_message_ko": "간호사를 추가하거나 OFF 상한을 늘리세요."},
            ],
            "human_message_ko": "산술적으로 work 공급이 부족합니다.",
        })

    max_night_global = int(_safe_attr(cfg, "max_night_shifts_per_month", 0) or 0)
    if max_night_global > 0 and n_required > (nurse_count * max_night_global):
        n_cap = nurse_count * max_night_global
        cores.append({
            "core_id": "conflict:capacity:n_shortage",
            "scope": "global",
            "pattern": "n_capacity_shortage",
            "affected_count": nurse_count,
            "affected_nurse_ids": [str(_safe_attr(nu, "nurse_id", i)) for i, nu in enumerate(nurses)],
            "members": [
                {"node_id": "demand:total_n", "type": "ConstraintNode",
                 "label": "월 N 요구", "value": n_required,
                 "human_message_ko": f"월간 N 요구 {n_required} cells"},
                {"node_id": "supply:total_n_cap", "type": "NightCapNode",
                 "label": "월 N 공급 (cap × nurse_count)", "value": n_cap,
                 "human_message_ko": f"N 공급 상한 {n_cap} ({nurse_count}명 × {max_night_global}일)"},
            ],
            "derivation": [
                {"step": 1, "from": "daily N requirements", "infer": f"월간 N 요구 = {n_required}"},
                {"step": 2, "from": "nurse_count × max_night", "infer": f"N 공급 상한 = {nurse_count} × {max_night_global} = {n_cap}"},
                {"step": 99, "conclusion": f"N 요구({n_required}) > N 공급({n_cap}) — N 산술 부족"},
            ],
            "conclusion": f"월 N 요구 {n_required} > 공급 상한 {n_cap}",
            "resolution_hints": [
                {"action": "reduce_daily_n_requirement",
                 "human_message_ko": "일별 N 요구를 줄이세요."},
                {"action": "increase_max_night",
                 "human_message_ko": f"max_night_shifts_per_month를 {max_night_global} → {max(1, (n_required + nurse_count - 1) // nurse_count)} 이상으로 늘리세요."},
            ],
            "human_message_ko": "야간 인력 산술 부족.",
        })

    return cores


def detect_initial_forbidden_concentration(rs: Any) -> List[Dict[str, Any]]:
    """특정 nurse가 전 기간 동안 같은 shift code(D, E 등) 차단된 패턴 감지.

    예: nurse_role이 "N-only"로 마스터에 등록되어 D/E가 모든 날 forbidden →
    이 nurse는 사실상 N 전담 → 다른 hard 제약과 결합해 충돌 가능.
    detect_nurse_overconstrained가 이미 수학적 충돌은 잡지만, 이 detector는
    "데이터 정합성" 차원에서 "이 nurse role이 N-only로 강제됐다"는 사실 자체를
    표면화 (false alarm 방지 — 항상 emit, 충돌 여부와 무관).
    """
    cores: List[Dict[str, Any]] = []
    nurses = list(_safe_attr(rs, "nurses", []) or [])
    num_days = int(_safe_attr(rs, "num_days", 0) or 0)
    if not nurses or num_days <= 0:
        return cores

    cfg = _safe_attr(rs, "config", None)
    shift_types = list(_safe_attr(cfg, "shift_types", []) or []) if cfg else []
    work_shifts = [s for s in shift_types if str(s).upper() != "O"]

    for idx, nu in enumerate(nurses):
        forbidden_all = _forbidden_shift_set(rs, idx)
        if not forbidden_all:
            continue
        allowed_work = [s for s in work_shifts if s not in forbidden_all]
        if not allowed_work or len(allowed_work) >= len(work_shifts):
            continue

        role_label = "+".join(sorted(allowed_work)) + "-only" if allowed_work else "no-work"
        forbidden_label = ",".join(sorted(forbidden_all))
        _nurse_id_str = str(_safe_attr(nu, "nurse_id", idx))
        cores.append({
            "core_id": f"data_quality:nurse_role_constrained:nurse_{idx}",
            "scope": "nurse",
            "pattern": "nurse_role_constrained",
            "nurse_id": _nurse_id_str,
            "affected_count": 1,
            "affected_nurse_ids": [_nurse_id_str],
            "members": [
                {
                    "node_id": f"nurse_role:{role_label}:nurse_{idx}",
                    "type": "NurseRoleNode",
                    "label": role_label,
                    "value": role_label,
                    "human_message_ko": (
                        f"이 간호사는 {num_days}일 내내 {forbidden_label} 시프트를 못 박습니다 — "
                        f"실질 role: {role_label}"
                    ),
                },
            ],
            "derivation": [
                {"step": 1, "from": "initial_forbidden",
                 "infer": f"이 간호사가 {num_days}일 모두 {forbidden_label} 금지"},
                {"step": 2, "from": "shift_types - forbidden",
                 "infer": f"가용 work shift = {sorted(allowed_work)}"},
                {"step": 99,
                 "conclusion": f"실질적으로 '{role_label}' 인력으로 동작"},
            ],
            "conclusion": f"nurse_{idx} 는 {num_days}일 전부 {forbidden_label} 금지 → 실질 {role_label} role",
            "resolution_hints": [
                {"action": "expand_nurse_shift_eligibility",
                 "human_message_ko": (
                    f"이 간호사가 추가 시프트 ({forbidden_label}) 도 가능하도록 role/master 데이터 확장."
                 )},
            ],
            "human_message_ko": (
                f"이 간호사는 데이터상 {role_label} 강제 — 다른 제약과 결합 시 충돌 위험."
            ),
        })

    return cores


def run_conflict_detectors(rs: Any) -> List[Dict[str, Any]]:
    """모든 detector를 실행하고 발견된 ConflictCore 리스트 반환."""
    out: List[Dict[str, Any]] = []
    try:
        out.extend(detect_nurse_overconstrained(rs))
    except Exception as e:
        print(f"[ConflictDetector] detect_nurse_overconstrained failed (ignore): {e}")
    try:
        out.extend(detect_capacity_shortage(rs))
    except Exception as e:
        print(f"[ConflictDetector] detect_capacity_shortage failed (ignore): {e}")
    try:
        out.extend(detect_initial_forbidden_concentration(rs))
    except Exception as e:
        print(f"[ConflictDetector] detect_initial_forbidden_concentration failed (ignore): {e}")
    return out
