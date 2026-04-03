"""폴백(서열) 최적화 3단계 목적함수(선호/공정성) 모듈."""

from __future__ import annotations

from ortools.sat.python import cp_model

from services.cp_sat.hardcoded_weights import (
    FALLBACK_EXPERIENCE_SHORT_PENALTY,
    N_ONLY_NIGHT_BONUS,
    PREFERENCE_SCORE_SCALE,
)
from services.cp_sat.allowed_shift_types import is_n_only_profile
from services.day_windows import iter_nurse_days
from services.cp_sat.objective_terms import (
    add_even_mid_distribution_terms,
    add_even_night_minmax_distribution_terms,
)


def build_fallback_stage3_objective_terms(
    *,
    m: cp_model.CpModel,
    roster_system,
    X,
    join: list[int],
    leave: list[int],
    fixed_cnt: list[list[int]],
    over_vars_by_day: dict[int, dict[str, cp_model.IntVar]],
    forced_off_cells: set[tuple[int, int]],
    off_exception_cells: set[tuple[int, int]],
    weekly_off_by_idx: dict[int, list[int]],
    logger_prefix: str,
    add_preceptor_terms_fn,
    add_team_balance_terms_fn,
    add_grade_constraints_fn,
    blocked_by_nurse: dict[int, set[int]] | None = None,
) -> list:
    """폴백 3단계(선호/공정성) 목적함수 항을 생성한다.

    Args:
        m: CP-SAT 모델
        roster_system: RosterSystem
        X: (n, d, s) → BoolVar 함수
        join: 간호사별 입사 인덱스
        leave: 간호사별 퇴사 인덱스
        fixed_cnt: 날짜×교대별 고정 셀 카운트
        over_vars_by_day: 일자별 과잉 배정 변수(교대별)
        forced_off_cells: 강제 OFF 셀 집합
        off_exception_cells: 예외 OFF 셀 집합
        weekly_off_by_idx: 주휴 day_idx 맵
        logger_prefix: 로그 접두사
        add_preceptor_terms_fn: 프리셉터 보너스 항 생성 함수
        add_team_balance_terms_fn: 팀 밸런스 항 생성 함수
        add_grade_constraints_fn: Grade 제약 항 생성 함수

    Returns:
        목적함수 항 리스트
    """
    cfg = roster_system.config
    N, D, S = len(roster_system.nurses), roster_system.num_days, cfg.num_shifts
    obj: list = []

    # 선호 점수 + N 전담 보너스
    P = roster_system.preference_matrix
    for n in range(N):
        nu = roster_system.nurses[n]
        raw = getattr(nu, "is_night_nurse", None)
        is_n_only = is_n_only_profile(raw, use_mid=bool(getattr(cfg, "use_mid", False)))
        for d in iter_nurse_days(n, join, leave, blocked_by_nurse):
            for s in range(S):
                base_score = int(P[n, d, s] * PREFERENCE_SCORE_SCALE)
                if is_n_only and s == cfg.shift_types.index("N"):
                    base_score += N_ONLY_NIGHT_BONUS
                obj.append(base_score * X(n, d, s))

    # 추가 OFF 기피
    try:
        off_penalty = int(getattr(cfg, "extra_off_penalty_weight", 0) or 0)
        if off_penalty > 0:
            off_idx = cfg.shift_types.index("O")
            for n in range(N):
                for d in iter_nurse_days(n, join, leave, blocked_by_nurse):
                    obj.append(-off_penalty * X(n, d, off_idx))
    except Exception:
        pass

    # 그림자 커버리지(소프트)
    try:
        shadow_days = int(getattr(cfg, "shadow_coverage_lookback_days", 0) or 0)
        shadow_ratio = float(getattr(cfg, "shadow_coverage_need_ratio", 0.0) or 0.0)
        shadow_weight = int(getattr(cfg, "shadow_coverage_penalty_weight", 0) or 0)
        if shadow_days > 0 and shadow_ratio > 0.0 and shadow_weight > 0:
            start_day = max(0, D - shadow_days)
            if (
                hasattr(cfg, "daily_shift_requirements_by_day")
                and isinstance(cfg.daily_shift_requirements_by_day, list)
                and len(cfg.daily_shift_requirements_by_day) > 0
            ):
                shadow_need_map = cfg.daily_shift_requirements_by_day[0] or {}
            else:
                shadow_need_map = cfg.daily_shift_requirements or {}
            for code in ("D", "E", "N"):
                s_idx = cfg.shift_types.index(code)
                need_val = int((shadow_need_map or {}).get(code, 0) or 0)
                target = int(need_val * shadow_ratio + 0.999)
                if target <= 0:
                    continue
                fixed_base = sum(fixed_cnt[d][s_idx] for d in range(start_day, D))
                terms = []
                for d in range(start_day, D):
                    for n in range(N):
                        if d < join[n] or d > leave[n]:
                            continue
                        terms.append(X(n, d, s_idx))
                if not terms and fixed_base >= target:
                    continue
                total_expr = sum(terms) + fixed_base if terms else fixed_base
                slack = m.NewIntVar(0, target, f"shadow_cov_{code}_{start_day}")
                m.Add(slack >= target - total_expr)
                obj.append(-shadow_weight * slack)
    except Exception as exc:
        print(f"{logger_prefix} [WARN] shadow coverage penalty 적용 실패: {exc}")

    # 주휴 양옆 낭비 O 패널티
    try:
        w_adj = int(getattr(cfg, "weekly_off_adjacent_off_penalty", 14) or 0)
        if w_adj > 0 and weekly_off_by_idx:
            off_idx = cfg.shift_types.index("O")
            for n, day_list in weekly_off_by_idx.items():
                if n >= len(join):
                    continue
                T0, T1 = join[n], leave[n]
                for d in day_list or []:
                    if d < T0 or d > T1:
                        continue
                    left = d - 1
                    right = d + 1
                    if left < T0 or right > T1:
                        continue
                    if (n, left) in forced_off_cells or (n, left) in off_exception_cells:
                        continue
                    if (n, right) in forced_off_cells or (n, right) in off_exception_cells:
                        continue
                    v = m.NewBoolVar(f"wo_adj_{n}_{d}")
                    m.Add(v <= X(n, left, off_idx))
                    m.Add(v <= X(n, right, off_idx))
                    m.Add(v >= X(n, left, off_idx) + X(n, right, off_idx) - 1)
                    obj.append(-w_adj * v)
    except Exception as exc:
        print(f"{logger_prefix} [WARN] weekly_off_adjacent_off_penalty 적용 실패: {exc}")

    # 주휴 이후 꼬리 O 패널티
    try:
        w_tail = int(getattr(cfg, "weekly_off_tail_penalty", 8) or 0)
        w_tail_trip = int(getattr(cfg, "weekly_off_tail_triplet_penalty", 12) or 0)
        if (w_tail > 0 or w_tail_trip > 0) and weekly_off_by_idx:
            forced_off_cells_set = set(forced_off_cells)
            off_idx = cfg.shift_types.index("O")
            for n, day_list in weekly_off_by_idx.items():
                if n >= len(join):
                    continue
                T0, T1 = join[n], leave[n]
                for d in day_list or []:
                    if d < T0 or d > T1:
                        continue
                    if w_tail > 0:
                        t1, t2 = d + 1, d + 2
                        if t2 <= T1:
                            cells = {(n, t1), (n, t2)}
                            if not (cells & forced_off_cells_set or cells & off_exception_cells):
                                v = m.NewBoolVar(f"wo_tail2_{n}_{d}")
                                m.Add(v <= X(n, t1, off_idx))
                                m.Add(v <= X(n, t2, off_idx))
                                m.Add(v >= X(n, t1, off_idx) + X(n, t2, off_idx) - 1)
                                obj.append(-w_tail * v)
                    if w_tail_trip > 0:
                        t1, t2, t3 = d + 1, d + 2, d + 3
                        if t3 <= T1:
                            cells3 = {(n, t1), (n, t2), (n, t3)}
                            if not (cells3 & forced_off_cells_set or cells3 & off_exception_cells):
                                v3 = m.NewBoolVar(f"wo_tail3_{n}_{d}")
                                m.Add(v3 <= X(n, t1, off_idx))
                                m.Add(v3 <= X(n, t2, off_idx))
                                m.Add(v3 <= X(n, t3, off_idx))
                                m.Add(
                                    v3
                                    >= X(n, t1, off_idx)
                                    + X(n, t2, off_idx)
                                    + X(n, t3, off_idx)
                                    - 2
                                )
                                obj.append(-w_tail_trip * v3)
    except Exception as exc:
        print(f"{logger_prefix} [WARN] weekly_off_tail penalties 적용 실패: {exc}")

    # 자유 O 연속 패널티
    try:
        w_free_pair = int(getattr(cfg, "free_off_pair_penalty", 6) or 0)
        w_free_trip = int(getattr(cfg, "free_off_triplet_penalty", 10) or 0)
        if w_free_pair > 0 or w_free_trip > 0:
            forced_off_cells_set = set(forced_off_cells)
            off_idx = cfg.shift_types.index("O")
            for n in range(N):
                T0, T1 = join[n], leave[n]
                if w_free_pair > 0:
                    for d in range(T0, T1):
                        if d + 1 > T1:
                            continue
                        cells = {(n, d), (n, d + 1)}
                        if cells & forced_off_cells_set or cells & off_exception_cells:
                            continue
                        v = m.NewBoolVar(f"free_o2_{n}_{d}")
                        m.Add(v <= X(n, d, off_idx))
                        m.Add(v <= X(n, d + 1, off_idx))
                        m.Add(v >= X(n, d, off_idx) + X(n, d + 1, off_idx) - 1)
                        obj.append(-w_free_pair * v)
                if w_free_trip > 0:
                    for d in range(T0, T1 - 1):
                        if d + 2 > T1:
                            continue
                        cells3 = {(n, d), (n, d + 1), (n, d + 2)}
                        if cells3 & forced_off_cells_set or cells3 & off_exception_cells:
                            continue
                        v3 = m.NewBoolVar(f"free_o3_{n}_{d}")
                        m.Add(v3 <= X(n, d, off_idx))
                        m.Add(v3 <= X(n, d + 1, off_idx))
                        m.Add(v3 <= X(n, d + 2, off_idx))
                        m.Add(
                            v3
                            >= X(n, d, off_idx)
                            + X(n, d + 1, off_idx)
                            + X(n, d + 2, off_idx)
                            - 2
                        )
                        obj.append(-w_free_trip * v3)
    except Exception as exc:
        print(f"{logger_prefix} [WARN] free_off penalties 적용 실패: {exc}")

    # 월단위 선호(개인 입력) 유도
    try:
        msp = getattr(roster_system, "monthly_shift_preferences", None)
        base_w = int(getattr(cfg, "monthly_preference_weight", 0) or 0)
        if base_w > 0 and isinstance(msp, dict) and msp:
            for n, nu in enumerate(roster_system.nurses):
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
                for d in iter_nurse_days(n, join, leave, blocked_by_nurse):
                    obj.append(w * X(n, d, s_idx))
    except Exception:
        pass

    try:
        obj.extend(
            add_even_night_minmax_distribution_terms(
                m=m,
                rs=roster_system,
                X=X,
                join=join,
                leave=leave,
                fixed_cnt=fixed_cnt,
                logger_prefix=logger_prefix,
                stage_label="폴백 Stage3",
                blocked_by_nurse=blocked_by_nurse,
            )
        )
        obj.extend(
            add_even_mid_distribution_terms(
                m=m,
                rs=roster_system,
                X=X,
                join=join,
                leave=leave,
                fixed_cnt=fixed_cnt,
            )
        )
    except Exception as exc:
        print(f"{logger_prefix} [WARN] even_nights penalty 적용 실패: {exc}")

    # 연속근무 소프트 상한
    try:
        soft_k = int(getattr(cfg, "soft_max_consecutive_work_days", 0) or 0)
        w_soft = int(getattr(cfg, "soft_consecutive_work_penalty_weight", 0) or 0)
        if soft_k > 0 and w_soft > 0:
            off_idx = cfg.shift_types.index("O")
            for n in range(N):
                T0, T1 = join[n], leave[n]
                for d0 in range(T0, T1 - soft_k + 1):
                    sum_off = sum(X(n, d0 + t, off_idx) for t in range(soft_k + 1))
                    miss = m.NewIntVar(0, 1, f"soft_cwork_miss_fb_{n}_{d0}")
                    m.Add(miss >= 1 - sum_off)
                    obj.append(-w_soft * miss)
    except Exception:
        pass

    # 경력자 부족 약벌
    for d in range(D):
        for code in ("D", "E", "N"):
            s = roster_system.config.shift_types.index(code)
            exp_assigned = sum(
                X(n, d, s)
                for n, nu in enumerate(roster_system.nurses)
                if join[n] <= d <= leave[n] and (nu.experience_years or 0) >= cfg.min_experience_per_shift
            )
            shortage = m.NewIntVar(0, cfg.required_experienced_nurses, f"expShort_fb_{d}_{code}")
            m.Add(shortage >= cfg.required_experienced_nurses - exp_assigned)
            obj.append(-FALLBACK_EXPERIENCE_SHORT_PENALTY * shortage)

    # 여유 인원 L1 균등화(일별 D/E/N)
    try:
        if bool(getattr(cfg, "oversupply_equalize_enable", True)):
            w_eq = int(getattr(cfg, "oversupply_equalize_weight", 120))
            for d, code2ov in over_vars_by_day.items():
                work_codes = [
                    code
                    for code in code2ov.keys()
                    if code in roster_system.config.daily_shift_requirements.keys()
                ]
                for i in range(len(work_codes)):
                    for j in range(i + 1, len(work_codes)):
                        c1, c2 = work_codes[i], work_codes[j]
                        ov1, ov2 = code2ov[c1], code2ov[c2]
                        diff = m.NewIntVar(0, N, f"ov_diff_fb_{d}_{c1}_{c2}")
                        m.Add(diff >= ov1 - ov2)
                        m.Add(diff >= ov2 - ov1)
                        obj.append(-w_eq * diff)
    except Exception:
        pass

    # 팀/프리셉터 보너스
    try:
        obj.extend(add_preceptor_terms_fn(m, roster_system, X, join, leave))
    except Exception as e:
        print("preceptor_objective_terms 예외 발생")
        print("e", e)
        pass
    try:
        grade_strategy = str(getattr(roster_system, "grade_strategy", "BASE") or "BASE").upper()
        print("grade_strategy", grade_strategy)
        if grade_strategy == "TEAM":
            obj.extend(add_team_balance_terms_fn(m, roster_system, X, join, leave))
    except Exception as e:
        print("team_balance_objective_terms 예외 발생")
        print("e", e)
        pass

    # Grade 제약을 soft penalty로 추가 (distribution 전용)
    try:
        grade_terms = add_grade_constraints_fn(
            m=m,
            rs=roster_system,
            X=X,
            join=join,
            leave=leave,
            grade_strategy=str(getattr(roster_system, "grade_strategy", "BASE")),
            grade_config=getattr(roster_system, "grade_config", None),
        )
        obj.extend(grade_terms or [])
    except Exception as e:
        print("grade_constraints 예외 발생")
        print("e", e)
        pass

    return obj
