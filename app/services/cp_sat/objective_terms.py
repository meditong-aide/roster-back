"""CP-SAT 목적 함수(soft objective) 구성 모듈."""

from __future__ import annotations

from typing import Callable, Iterable

from ortools.sat.python import cp_model

from services.cp_sat.hardcoded_weights import (
    EXPERIENCE_SHORT_PENALTY,
    FALLBACK_COVERAGE_SHORT_WEIGHT,
    ISOLATED_OFF_PENALTY,
    NIGHT_DEVIATION_PENALTY,
    NOD_NOE_PENALTY,
    N_ONLY_NIGHT_BONUS,
    PREFERENCE_SCORE_SCALE,
    WEEK_OFF_SHORT_PENALTY,
)
from services.objectives.surplus_target_objective import append_surplus_target_direction_terms
from services.constraints.grade_constraints import add_grade_constraints
from services.objectives.team_objective import add_team_balance_objective_terms


def _n_forbid_n_set(rs, join: list[int], leave: list[int]) -> set[int]:
    """N 전일 금지 간호사 인덱스 집합. initial_forbidden에서 모든 근무일에 N이 금지된 n만 반환."""
    n_forbid_n: set[int] = set()
    initial_forbidden = getattr(rs, "initial_forbidden", None)
    if not isinstance(initial_forbidden, dict):
        return n_forbid_n
    for n in range(len(rs.nurses)):
        t0, t1 = join[n], leave[n]
        if t0 > t1:
            continue
        n_days = t1 - t0 + 1
        forbid_cnt = sum(
            1 for d in range(t0, t1 + 1)
            if "N" in initial_forbidden.get((n, d), set())
        )
        if forbid_cnt == n_days:
            n_forbid_n.add(n)
    return n_forbid_n


def _get_surplus_override_mode_by_nurse(rs) -> dict[int, str]:
    raw = getattr(rs.config, "surplus_overrides_json", None)
    if not isinstance(raw, dict) or not raw:
        return {}
    allowed = {"avoid", "neutral", "prefer"}
    out: dict[int, str] = {}
    for n, nu in enumerate(rs.nurses):
        key = str(getattr(nu, "db_id", "")).strip()
        if not key:
            continue
        mode = str(raw.get(key, "")).strip().lower()
        if mode in allowed and mode != "neutral":
            out[n] = mode
    return out


def _adaptive_surplus_scaling(cfg, join: list[int], leave: list[int], fixed_cnt, D: int) -> tuple[float, float]:
    if not bool(getattr(cfg, "oversupply_adaptive_enable", True)):
        return 1.0, 1.0

    req_map = getattr(cfg, "daily_shift_requirements", {}) or {}
    work_codes = [str(code) for code in req_map.keys() if str(code) != "O"]
    if not work_codes:
        return 1.0, 1.0

    shift_idx = {code: int(cfg.shift_types.index(code)) for code in work_codes if code in cfg.shift_types}
    if not shift_idx:
        return 1.0, 1.0

    ds_by_day = getattr(cfg, "daily_shift_requirements_by_day", None)
    ratios: list[float] = []
    for d in range(D):
        available = sum(1 for n in range(len(join)) if join[n] <= d <= leave[n])
        if available <= 0:
            continue

        if isinstance(ds_by_day, list) and d < len(ds_by_day) and isinstance(ds_by_day[d], dict):
            need_map = ds_by_day[d]
        else:
            need_map = req_map

        required = sum(max(0, int((need_map or {}).get(code, req_map.get(code, 0)) or 0)) for code in shift_idx.keys())

        fixed_work = 0
        if fixed_cnt is not None and d < len(fixed_cnt):
            for code, s_idx in shift_idx.items():
                if s_idx < len(fixed_cnt[d]):
                    fixed_work += max(0, int(fixed_cnt[d][s_idx] or 0))

        effective_required = max(0, required - fixed_work)
        surplus = max(0, available - effective_required)
        ratios.append(float(surplus) / float(max(1, available)))

    if not ratios:
        return 1.0, 1.0

    avg_ratio = sum(ratios) / len(ratios)
    if avg_ratio >= 0.60:
        day_mult, bias_mult = 2.0, 0.35
    elif avg_ratio >= 0.45:
        day_mult, bias_mult = 2.2, 0.25
    elif avg_ratio >= 0.30:
        day_mult, bias_mult = 1.5, 0.6
    else:
        day_mult, bias_mult = 1.0, 1.0

    profile = str(getattr(cfg, "oversupply_adaptive_profile", "auto") or "auto").lower()
    if profile == "conservative":
        day_mult *= 0.85
        bias_mult *= 0.9
    elif profile == "aggressive":
        day_mult *= 1.2
        bias_mult *= 0.8

    day_mult = max(0.5, min(3.0, day_mult))
    bias_mult = max(0.2, min(1.5, bias_mult))
    return day_mult, bias_mult


def build_main_objective_terms(
    *,
    m: cp_model.CpModel,
    rs,
    X,
    join: list[int],
    leave: list[int],
    over_vars_by_day: dict[int, dict[str, cp_model.IntVar]],
    coverage_shortage_vars: list[tuple[cp_model.IntVar, str]],
    include_pair_objective: bool,
    preceptor_terms_fn: Callable[..., Iterable],
    fixed_cnt: list[list[int]] | None = None,
) -> list:
    """메인 모델의 목적 함수 항들을 생성한다.

    Args:
        m: CP-SAT 모델
        rs: RosterSystem
        X: (n, d, s) → BoolVar 함수
        join: 간호사별 입사 인덱스
        leave: 간호사별 퇴사 인덱스
        over_vars_by_day: 일자별 과잉 배정 변수(교대별)
        coverage_shortage_vars: (short_var, code) 목록
        include_pair_objective: 페어/팀 항 포함 여부
        preceptor_terms_fn: 프리셉터 보너스 항 생성 함수
        fixed_cnt: 날짜×교대별 고정 셀 카운트 (even_nights용, None이면 미사용)

    Returns:
        목적 함수 항 리스트

    Notes:
        - 선호 점수: base_score = pref * scale (예: pref=0.8, scale=100 → 80)
        - 야간 전담 보너스: base_score += N_ONLY_NIGHT_BONUS (예: +500)
    """
    cfg = rs.config
    obj: list = []
    P = rs.preference_matrix
    N = len(rs.nurses)
    D = rs.num_days
    S = cfg.num_shifts
    idx = {c: cfg.shift_types.index(c) for c in ("D", "E", "N", "O")}
    day, eve, night, off = idx["D"], idx["E"], idx["N"], idx["O"]

    # (0) 커버리지 부족 패널티(강하게): shortage 변수에 큰 음수 가중치 적용
    for sh, code in coverage_shortage_vars:
        obj.append(-FALLBACK_COVERAGE_SHORT_WEIGHT * sh)

    for n in range(N):
        nu = rs.nurses[n]
        is_n_only = False
        raw = getattr(nu, "is_night_nurse", None)
        if isinstance(raw, list):
            allowed = {str(x).strip().upper() for x in raw if str(x).strip()}
            is_n_only = (allowed == {"N"})
        elif raw == 3 or (raw is not None and raw != 0 and raw is not False):
            is_n_only = True

        for d in range(join[n], leave[n] + 1):
            for s in range(S):
                base_score = int(P[n, d, s] * PREFERENCE_SCORE_SCALE) if d < D else 0
                if is_n_only and s == night:
                    base_score += N_ONLY_NIGHT_BONUS
                obj.append(base_score * X(n, d, s))

    # (4-0) 추가 OFF(여유 OFF) 기피
    try:
        off_penalty = int(getattr(cfg, "extra_off_penalty_weight", 0) or 0)
        if off_penalty > 0:
            mode_by_nurse = _get_surplus_override_mode_by_nurse(rs)
            for n in range(N):
                mode = mode_by_nurse.get(n)
                if mode == "avoid":
                    n_penalty = max(0, int(round(off_penalty * 0.85)))
                elif mode == "prefer":
                    n_penalty = int(round(off_penalty * 1.15))
                else:
                    n_penalty = off_penalty
                if n_penalty <= 0:
                    continue
                for d in range(join[n], leave[n] + 1):
                    obj.append(-n_penalty * X(n, d, off))
    except Exception:
        pass

    # (4-0a) 월단위 선호(개인 입력)
    try:
        msp = getattr(rs, "monthly_shift_preferences", None)
        base_w = int(getattr(cfg, "monthly_preference_weight", 0) or 0)
        if base_w > 0 and isinstance(msp, dict) and msp:
            for n, nu in enumerate(rs.nurses):
                pref = msp.get(str(getattr(nu, "db_id", ""))) or msp.get(getattr(nu, "db_id", ""))
                if not isinstance(pref, dict):
                    continue
                code = str(pref.get("shift") or "").strip().upper()
                if code not in {"D", "E", "N"}:
                    continue
                try:
                    strength = int(pref.get("strength", 5) or 0)
                except Exception:
                    strength = 5
                strength = max(0, min(10, strength))
                w = int(round(base_w * (strength / 10.0)))
                if w <= 0:
                    continue
                s_idx = cfg.shift_types.index(code)
                for d in range(join[n], leave[n] + 1):
                    obj.append(w * X(n, d, s_idx))
    except Exception:
        pass

    # (4-0b) 연속근무 소프트 상한
    try:
        soft_k = int(getattr(cfg, "soft_max_consecutive_work_days", 0) or 0)
        w_soft = int(getattr(cfg, "soft_consecutive_work_penalty_weight", 0) or 0)
        if soft_k > 0 and w_soft > 0:
            for n in range(N):
                T0, T1 = join[n], leave[n]
                for d0 in range(T0, T1 - soft_k + 1):
                    sum_off = sum(X(n, d0 + t, off) for t in range(soft_k + 1))
                    miss = m.NewIntVar(0, 1, f"soft_cwork_miss_{n}_{d0}")
                    m.Add(miss >= 1 - sum_off)
                    obj.append(-w_soft * miss)
    except Exception:
        pass

    # (4-1) 경력자 부족
    for d in range(D):
        for code in ("D", "E", "N"):
            s = cfg.shift_types.index(code)
            exp_assigned = sum(
                X(n, d, s)
                for n, nu in enumerate(rs.nurses)
                if join[n] <= d <= leave[n] and nu.experience_years >= cfg.min_experience_per_shift
            )
            shortage = m.NewIntVar(0, cfg.required_experienced_nurses, f"expShort_{d}_{code}")
            m.Add(shortage >= cfg.required_experienced_nurses - exp_assigned)
            obj.append(-EXPERIENCE_SHORT_PENALTY * shortage)

    # (4-2) 주 2 OFF
    if cfg.enforce_two_offs_per_week:
        weeks = D // 7
        for n in range(N):
            for w in range(weeks):
                d0, d1 = w * 7, min(w * 7 + 7, D)
                offs = sum(X(n, d, off) for d in range(d0, d1) if join[n] <= d <= leave[n])
                slack = m.NewIntVar(0, 2, f"weekSlack_{n}_{w}")
                m.Add(slack >= 2 - offs)
                obj.append(-WEEK_OFF_SHORT_PENALTY * slack)

    # (4-3) 야간 균등 (편차에 선형 패널티) - N 전일 금지 간호사는 대상에서 제외
    if getattr(cfg, "even_nights", False):
        normals: list[int] = []
        for i, nu in enumerate(rs.nurses):
            is_n_only = False
            raw = getattr(nu, "is_night_nurse", None)
            if isinstance(raw, list):
                allowed = {str(x).strip().upper() for x in raw if str(x).strip()}
                is_n_only = allowed == {"N"}
            elif raw == 3 or (raw is not None and raw not in (0, False)):
                is_n_only = True
            if not is_n_only:
                normals.append(i)
        n_forbid_n = _n_forbid_n_set(rs, join, leave)
        normals_can_n = [n for n in normals if n not in n_forbid_n]
        if normals_can_n:
            if (
                hasattr(cfg, "daily_shift_requirements_by_day")
                and isinstance(cfg.daily_shift_requirements_by_day, list)
                and len(cfg.daily_shift_requirements_by_day) == D
            ):
                daily_need_n = [
                    int((cfg.daily_shift_requirements_by_day[d] or {}).get("N", 0) or 0)
                    for d in range(D)
                ]
            else:
                base_n = int((cfg.daily_shift_requirements or {}).get("N", 0) or 0)
                daily_need_n = [base_n for _ in range(D)]
            fc = fixed_cnt if fixed_cnt is not None else [[0] * S for _ in range(D)]
            total_need_n = 0
            for d in range(D):
                need = max(0, daily_need_n[d] - int(fc[d][night] if d < len(fc) else 0))
                total_need_n += need
            if total_need_n > 0:
                target = total_need_n // len(normals_can_n)
                print(
                    "[objective_terms] [N균등] even_nights 적용(메인): "
                    f"normals_can_N={len(normals_can_n)}, n_forbid_N={len(n_forbid_n)}, "
                    f"target_N_per_nurse={target}, total_need_n={total_need_n}, "
                    f"penalty_weight={NIGHT_DEVIATION_PENALTY}"
                )
                for n in normals_can_n:
                    tot_nights = sum(
                        X(n, d, night) for d in range(join[n], leave[n] + 1)
                    )
                    dev_pos = m.NewIntVar(0, D, f"devP_{n}")
                    dev_neg = m.NewIntVar(0, D, f"devN_{n}")
                    m.Add(dev_pos - dev_neg == tot_nights - target)
                    obj.extend(
                        [
                            -NIGHT_DEVIATION_PENALTY * dev_pos,
                            -NIGHT_DEVIATION_PENALTY * dev_neg,
                        ]
                    )
            else:
                print("[objective_terms] [N균등] even_nights 켜짐 but total_need_n=0 → 패널티 미적용")
        else:
            print(
                "[objective_terms] [N균등] even_nights 켜짐 but "
                "normals_can_N(비야간전담·N가능)=0 → 스킵"
            )

    # (4-4) N-O-D/E 패턴
    if getattr(cfg, "nod_noe", True):
        for n in range(N):
            for d in range(join[n], leave[n] - 2):
                pat = m.NewIntVar(0, 1, f"NOD_{n}_{d}")
                m.Add(pat >= X(n, d, night) + X(n, d + 1, off) + X(n, d + 2, day) - 2)
                obj.append(-NOD_NOE_PENALTY * pat)
                pat2 = m.NewIntVar(0, 1, f"NOE_{n}_{d}")
                m.Add(pat2 >= X(n, d, night) + X(n, d + 1, off) + X(n, d + 2, eve) - 2)
                obj.append(-NOD_NOE_PENALTY * pat2)
                pat3 = m.NewIntVar(0, 1, f"EOD_{n}_{d}")
                m.Add(pat3 >= X(n, d, eve) + X(n, d + 1, off) + X(n, d + 2, day) - 2)
                obj.append(-NOD_NOE_PENALTY * pat3)

    # (4-5) 고립 OFF (sequential_offs ON일 때만, fallback과 동일)
    if getattr(cfg, "sequential_offs", True):
        for n in range(N):
            for d in range(join[n], leave[n] + 1):
                iso = m.NewIntVar(0, 1, f"iso_{n}_{d}")
                m.Add(iso >= X(n, d, off) - X(n, d - 1, off) - X(n, d + 1, off))
                m.Add(iso <= X(n, d, off))
                m.Add(iso <= 1 - X(n, d - 1, off))
                m.Add(iso <= 1 - X(n, d + 1, off))
                obj.append(-ISOLATED_OFF_PENALTY * iso)

    # (4-5a) OFF 연속 배정 보너스 (sequential_offs)
    if getattr(cfg, "sequential_offs", True):
        SEQUENTIAL_OFF_BONUS = 150000  # 연속 휴무 보너스 가중치
        for n in range(N):
            T0, T1 = join[n], leave[n]
            for d in range(T0, T1):
                # 연속된 OFF에 보너스 부여
                consecutive_bonus = m.NewBoolVar(f"seq_off_{n}_{d}")
                # X(n, d, off) == 1 AND X(n, d+1, off) == 1 이면 consecutive_bonus == 1
                m.Add(consecutive_bonus <= X(n, d, off))
                m.Add(consecutive_bonus <= X(n, d + 1, off))
                m.Add(consecutive_bonus >= X(n, d, off) + X(n, d + 1, off) - 1)
                obj.append(SEQUENTIAL_OFF_BONUS * consecutive_bonus)

    # (4-6) 프리셉터/팀 보너스 항
    if include_pair_objective:
        try:
            obj.extend(preceptor_terms_fn(m, rs, X, join, leave))
        except Exception:
            pass
        grade_strategy = str(getattr(rs, "grade_strategy", "BASE") or "BASE").upper()
        print("grade_strategy", grade_strategy)
        if grade_strategy == "TEAM":
            obj.extend(add_team_balance_objective_terms(m, rs, X, join, leave))

    # (4-6a) Grade 분배 목적 항 (fallback Stage3와 동일)
    try:
        _gs = str(getattr(rs, "grade_strategy", "BASE") or "BASE").upper()
        if _gs == "GRADE":
            grade_terms = add_grade_constraints(
                m=m,
                rs=rs,
                X=X,
                join=join,
                leave=leave,
                grade_strategy=_gs,
                grade_config=getattr(rs, "grade_config", None),
            )
            obj.extend(grade_terms or [])
    except Exception:
        pass

    # (4-7) 커버리지 부족 패널티 (shift_requirement_priority 기반)
    try:
        pr = float(getattr(cfg, "shift_requirement_priority", 0.8))
        base = int(1000 * max(0.05, min(1.0, pr)))
        for sh, code in coverage_shortage_vars:
            w = base
            if code == "N":
                w = int(base * 1.2)
            obj.append(-w * sh)
    except Exception:
        pass

    # (4-8) 여유 인원 L1 균등화
    try:
        if bool(getattr(cfg, "oversupply_equalize_enable", True)):
            w_eq = int(getattr(cfg, "oversupply_equalize_weight", 120))
            day_mult, bias_mult = _adaptive_surplus_scaling(cfg, join, leave, fixed_cnt, D)
            for d, code2ov in over_vars_by_day.items():
                work_codes = [
                    code
                    for code in code2ov.keys()
                    if code in rs.config.daily_shift_requirements.keys()
                ]
                for i in range(len(work_codes)):
                    for j in range(i + 1, len(work_codes)):
                        c1, c2 = work_codes[i], work_codes[j]
                        ov1, ov2 = code2ov[c1], code2ov[c2]
                        diff = m.NewIntVar(0, N, f"ov_diff_{d}_{c1}_{c2}")
                        m.Add(diff >= ov1 - ov2)
                        m.Add(diff >= ov2 - ov1)
                        obj.append(-w_eq * diff)

            w_day_raw = getattr(cfg, "oversupply_day_dispersion_weight", None)
            w_day_base = int(round(w_eq * 0.25)) if w_day_raw is None else int(w_day_raw or 0)
            w_day = max(0, int(round(w_day_base * day_mult)))
            if w_day > 0 and over_vars_by_day:
                day_total_oversupply = {
                    d: sum(code2ov.values()) for d, code2ov in over_vars_by_day.items()
                }
                day_indices = sorted(day_total_oversupply.keys())
                consecutive_only = bool(getattr(cfg, "oversupply_day_dispersion_consecutive_only", False))
                if consecutive_only:
                    day_pairs = list(zip(day_indices, day_indices[1:]))
                else:
                    day_pairs = [
                        (day_indices[i], day_indices[j])
                        for i in range(len(day_indices))
                        for j in range(i + 1, len(day_indices))
                    ]
                for d1, d2 in day_pairs:
                    t1 = day_total_oversupply[d1]
                    t2 = day_total_oversupply[d2]
                    diff_day = m.NewIntVar(0, N, f"ov_day_diff_{d1}_{d2}")
                    m.Add(diff_day >= t1 - t2)
                    m.Add(diff_day >= t2 - t1)
                    obj.append(-w_day * diff_day)

            append_surplus_target_direction_terms(
                m=m,
                cfg=cfg,
                over_vars_by_day=over_vars_by_day,
                obj=obj,
                N=N,
                prefix="main",
            )

            mode_by_nurse = _get_surplus_override_mode_by_nurse(rs)
            if mode_by_nurse:
                work_codes = [
                    code
                    for code in cfg.shift_types
                    if code != "O" and code in cfg.daily_shift_requirements.keys()
                ]
                work_shift_indices = [cfg.shift_types.index(code) for code in work_codes]
                if work_shift_indices:
                    unit = max(1, int(round(w_eq * 0.03 * bias_mult)))
                    for n, mode in mode_by_nurse.items():
                        sign = 1 if mode == "prefer" else -1
                        for d in range(join[n], leave[n] + 1):
                            obj.append(sign * unit * sum(X(n, d, s_idx) for s_idx in work_shift_indices))
    except Exception:
        pass

    return obj
