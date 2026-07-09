"""폴백(서열) 최적화 3단계 목적함수(선호/공정성) 모듈."""

from __future__ import annotations

from ortools.sat.python import cp_model

from services.cp_sat.hardcoded_weights import (
    FALLBACK_EXPERIENCE_SHORT_PENALTY,
    ISOLATED_WORK_PENALTY,
    N_ONLY_NIGHT_BONUS,
    PREFER_3N_BLOCK_PENALTY,
    PREFERENCE_SCORE_SCALE,
)
from services.cp_sat.allowed_shift_types import is_n_only_profile, normalize_allowed_shift_codes
from services.constraints.team_constraints import add_team_min_constraints
from services.constraints.team_grade_handoff_constraints import (
    add_team_grade_handoff_constraints,
)
from services.day_windows import iter_nurse_days
from services.cp_sat.objective_terms import (
    add_even_mid_distribution_terms,
    add_even_night_minmax_distribution_terms,
    add_kld_distribution_terms,
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
        raw = getattr(nu, "allowed_shifts", None)
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
            add_kld_distribution_terms(
                m=m,
                rs=roster_system,
                X=X,
                join=join,
                leave=leave,
                fixed_cnt=fixed_cnt,
                logger_prefix=logger_prefix,
                stage_label="폴백Stage3",
                blocked_by_nurse=blocked_by_nurse,
            )
        )
    except Exception as exc:
        print(f"{logger_prefix} [WARN] KLD distribution 적용 실패: {exc}")

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

    # 같은 시프트(D/E/N) 연속 soft — 4연속부터, 길이가 길수록 준지수 급증.
    # 겹치는 다중 길이 창(4..K)마다 길이별 급증 가중치 → 런 길이 L의 총비용 f(L)이 볼록.
    #   base=300, factor=4 → DDDD=300, DDDDD=1800, DDDDDD=8100 (4 현행 유지, 5+ 폭증).
    # D전담/N전담(단일 시프트 전담)은 해당 코드 연속을 강제당하므로 제외(유령 페널티 방지).
    try:
        if bool(getattr(cfg, "max_same_shift", True)):
            import os as _os_ms
            w_ms_base = int(_os_ms.environ.get(
                "AIDE_SAME_SHIFT_BASE",
                getattr(cfg, "max_same_shift_penalty_weight", 0)) or 0)
            if w_ms_base > 0:
                _use_mid_ms = bool(getattr(cfg, "use_mid", False))
                _ms_mode = _os_ms.environ.get("AIDE_SAME_SHIFT_MODE", "d5add")
                _ms_factor = float(_os_ms.environ.get(
                    "AIDE_SAME_SHIFT_FACTOR",
                    getattr(cfg, "max_same_shift_growth_factor", 4)) or 4)
                _ms_K = int(getattr(cfg, "max_consecutive_work_days", 5) or 5)
                _ms_kmax = int(_os_ms.environ.get("AIDE_SAME_SHIFT_KMAX", 0) or 0) or max(4, min(_ms_K, 6))
                # D5 가중치는 ISOLATED_WORK_PENALTY(1500) 아래로 유지 (고립근무 > 5연속D).
                _d5_w = int(_os_ms.environ.get(
                    "AIDE_D5_WEIGHT", getattr(cfg, "d5_penalty_weight", 1200)) or 0)
                _d5_lex_vars = []  # D 5연속 viol 핸들(옵션 최종 lex 패스용)

                def _allowed_ms(n):
                    return normalize_allowed_shift_codes(
                        getattr(roster_system.nurses[n], "allowed_shifts", None), use_mid=_use_mid_ms)

                if _ms_mode == "d5add":
                    # Case 2: 원래 단일 win4(균등 base) + D 전용 win5 고weight 1개 추가.
                    for code in ("D", "E", "N"):
                        if code not in cfg.shift_types:
                            continue
                        s_idx = cfg.shift_types.index(code)
                        for n in range(N):
                            if _allowed_ms(n) == {code}:
                                continue
                            T0, T1 = join[n], leave[n]
                            for d0 in range(T0, T1 - 3):
                                sum_s = sum(X(n, d0 + t, s_idx) for t in range(4))
                                viol = m.NewIntVar(0, 1, f"max_same_shift_fb_{code}_{n}_{d0}")
                                m.Add(viol >= sum_s - 3)
                                obj.append(-w_ms_base * viol)
                    if _d5_w > 0 and "D" in cfg.shift_types:
                        d_idx = cfg.shift_types.index("D")
                        for n in range(N):
                            if _allowed_ms(n) == {"D"}:
                                continue
                            T0, T1 = join[n], leave[n]
                            for d0 in range(T0, T1 - 4):
                                sum_d = sum(X(n, d0 + t, d_idx) for t in range(5))
                                v5 = m.NewIntVar(0, 1, f"d5_fb_{n}_{d0}")
                                m.Add(v5 >= sum_d - 4)
                                obj.append(-_d5_w * v5)
                                _d5_lex_vars.append(v5)
                else:
                    # escalate(기본): 겹치는 다중 길이 창(4..K) + 길이별 급증 가중치.
                    for code in ("D", "E", "N"):
                        if code not in cfg.shift_types:
                            continue
                        s_idx = cfg.shift_types.index(code)
                        for wlen in range(4, _ms_kmax + 1):
                            w_ms = int(round(w_ms_base * (_ms_factor ** (wlen - 4))))
                            if w_ms <= 0:
                                continue
                            for n in range(N):
                                if _allowed_ms(n) == {code}:
                                    continue  # 단일 시프트 전담: 해당 코드 연속 강제 → 제외
                                T0, T1 = join[n], leave[n]
                                for d0 in range(T0, T1 - (wlen - 1)):
                                    sum_s = sum(X(n, d0 + t, s_idx) for t in range(wlen))
                                    viol = m.NewIntVar(0, 1, f"max_same_shift_fb_{code}_{wlen}_{n}_{d0}")
                                    m.Add(viol >= sum_s - (wlen - 1))
                                    obj.append(-w_ms * viol)
                                    if code == "D" and wlen == 5:
                                        _d5_lex_vars.append(viol)
                # 최종 lex 패스(옵션)용 D5 viol 핸들을 모델 side-channel 에 stash.
                try:
                    m._ms_d5_lex_vars = _d5_lex_vars  # type: ignore[attr-defined]
                except Exception:
                    pass
    except Exception:
        pass

    # 고립 근무 (O W O: OFF 사이 단일 근무 "퐁당퐁당") soft — N 제외(not_one_night 별도 관리).
    # 경계일(d=T0/T1)은 양옆 OFF 확인 불가라 자연히 제외(range(T0+1, T1)).
    try:
        if bool(getattr(cfg, "sequential_offs", True)) and "O" in cfg.shift_types:
            _iw_off = cfg.shift_types.index("O")
            _iw_has_n = "N" in cfg.shift_types
            _iw_night = cfg.shift_types.index("N") if _iw_has_n else None
            for n in range(N):
                T0, T1 = join[n], leave[n]
                for d in range(T0 + 1, T1):
                    _iw_mid = 1 - X(n, d, _iw_off) - (X(n, d, _iw_night) if _iw_has_n else 0)
                    _iw = m.NewBoolVar(f"isow_fb_{n}_{d}")
                    m.Add(_iw <= X(n, d - 1, _iw_off))
                    m.Add(_iw <= X(n, d + 1, _iw_off))
                    m.Add(_iw <= _iw_mid)
                    m.Add(_iw >= X(n, d - 1, _iw_off) + X(n, d + 1, _iw_off) + _iw_mid - 2)
                    obj.append(-ISOLATED_WORK_PENALTY * _iw)
    except Exception:
        pass

    # 단일 E 패널티 (lone-E): E를 "쌍"으로 유도 → DDDE→DDEE, DDDDE→DDDEE.
    # 외톨이 E(양옆 둘다 E 아님)에 패널티. 단 E→N(정방향 로테이션)은 예외로 보존(옵션 B).
    # 경계일(T0/T1)은 양옆 확인 불가라 제외(range(T0+1, T1)). 원복: lone_e_penalty_weight=0.
    try:
        import os as _os_le
        _le_w = int(_os_le.environ.get("LONE_E_PENALTY_WEIGHT", getattr(cfg, "lone_e_penalty_weight", 500)) or 0)
        if _le_w > 0 and "E" in cfg.shift_types:
            _le_e = cfg.shift_types.index("E")
            _le_has_n = "N" in cfg.shift_types
            _le_n = cfg.shift_types.index("N") if _le_has_n else None
            for n in range(N):
                T0, T1 = join[n], leave[n]
                for d in range(T0 + 1, T1):
                    _le = m.NewBoolVar(f"lone_e_fb_{n}_{d}")
                    # E[d]=1, E[d-1]=0, E[d+1]=0, (E→N 로테이션이면 면제: -N[d+1])
                    _next_n = X(n, d + 1, _le_n) if _le_has_n else 0
                    m.Add(_le >= X(n, d, _le_e) - X(n, d - 1, _le_e) - X(n, d + 1, _le_e) - _next_n)
                    obj.append(-_le_w * _le)
    except Exception:
        pass

    # (D/E per-nurse 균등은 stage2 lex 5-pass(DE_LEX)로 처리 — 수렴된 hint가 stage3로
    #  상속되므로 stage3 페널티 항은 불필요. 제거됨.)

    # N 블록 종료 → 다음 N 블록 시작 간격 soft (한쪽, target=10일)
    # 간격이 target보다 *짧을* 때만 벌점. 더 멀면 휴식이 충분하므로 벌하지 않는다.
    try:
        n2n_target = int(getattr(cfg, "n_to_n_interval_target", 0) or 0)
        n2n_w = int(getattr(cfg, "n_to_n_interval_penalty_weight", 0) or 0)
        n2n_win = int(getattr(cfg, "n_to_n_interval_max_window", 0) or 0)
        if n2n_target > 0 and n2n_w > 0 and n2n_win >= 2 and "N" in cfg.shift_types:
            n_idx = cfg.shift_types.index("N")
            for n in range(N):
                T0, T1 = join[n], leave[n]
                for d1 in range(T0, T1):
                    for d2 in range(d1 + 2, min(d1 + n2n_win + 1, T1 + 1)):
                        gap = d2 - d1
                        deficit = n2n_target - gap
                        if deficit <= 0:
                            continue
                        pair = m.NewBoolVar(f"n2n_fb_{n}_{d1}_{d2}")
                        m.Add(pair <= X(n, d1, n_idx))
                        m.Add(pair <= X(n, d2, n_idx))
                        for k in range(d1 + 1, d2):
                            m.Add(pair <= 1 - X(n, k, n_idx))
                        between_sum = sum(X(n, k, n_idx) for k in range(d1 + 1, d2))
                        m.Add(pair >= X(n, d1, n_idx) + X(n, d2, n_idx) - 1 - between_sum)
                        obj.append(-n2n_w * deficit * pair)
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

    # 상호 근무 배제(mutual exclusion) soft: 같은 날 같은 근무조 co-assignment 페널티.
    # 프리셉터 together 보너스의 배반(부호 반대). grade/team 하드가 lexicographic 상위·등식락이라 침범 불가.
    # z >= X(a,d,s)+X(b,d,s)-1 : 둘 다 같은 근무조 배정 시에만 z=1 강제 → Maximize 라 음수계수로 회피 유도.
    try:
        _mx_pairs = getattr(roster_system, "mutual_exclusion_pairs", None)
        if _mx_pairs:
            _cfg_mx = roster_system.config
            # 기본 300,000: 실인스턴스(COMBINED)에서 grade soft(1.6M)·OFF균등(100k)와 경쟁해
            # 실효를 내되 grade/team 하드는 절대 못 이기는 수준. 라이브 튜닝(dev 2026-08): 8→4건.
            import os as _os_mx
            _w_mx = int(_os_mx.getenv("MUTEX_W")
                        or getattr(_cfg_mx, "mutual_exclusion_penalty_weight", 300000) or 0)
            if _w_mx > 0:
                _use_mid_mx = bool(getattr(_cfg_mx, "use_mid", False))
                _shift_types_mx = list(_cfg_mx.shift_types or [])
                _shift_idx_mx = [
                    _shift_types_mx.index(c)
                    for c in _cfg_mx.daily_shift_requirements.keys()
                    if c in _shift_types_mx
                ]

                def _is_dedicated_mx(_idx):
                    # N전담 등 '단일 근무조 전담'(allowed_shifts=1종) 은 배반 규칙 제외 —
                    # 특정 조에 묶여 있어 배제가 무의미/과도. 시점가변이라 생성 시점(as-of)에 판별.
                    _raw = getattr(roster_system.nurses[_idx], "allowed_shifts", None)
                    return len(normalize_allowed_shift_codes(_raw, use_mid=_use_mid_mx)) == 1

                _mutex_lex_z = []  # "grade/team 바로 밑" lex 패스용 위반변수 stash(전담 제외 통과분만)
                for (_a, _b, _days_mx) in _mx_pairs:
                    if _is_dedicated_mx(_a) or _is_dedicated_mx(_b):
                        print(f"[MutualExcl][SKIP] 전담(단일근무조) 포함 페어 제외: a={_a}, b={_b}")
                        continue
                    _d_lo = max(join[_a], join[_b])
                    _d_hi = min(leave[_a], leave[_b])
                    for _d in range(_d_lo, _d_hi + 1):
                        if _d not in _days_mx:
                            continue
                        for _s in _shift_idx_mx:
                            _z = m.NewBoolVar(f"mutex_viol_fb_{_a}_{_b}_{_d}_{_s}")
                            m.Add(_z >= X(_a, _d, _s) + X(_b, _d, _s) - 1)
                            obj.append(-_w_mx * _z)
                            _mutex_lex_z.append(_z)
                if _mutex_lex_z:
                    m._mutex_lex_vars = _mutex_lex_z  # type: ignore[attr-defined]
    except Exception as _e_mx:
        print("mutual_exclusion_objective_terms 예외 발생", _e_mx)

    grade_strategy = str(getattr(roster_system, "grade_strategy", "BASE") or "BASE").upper()
    print("grade_strategy", grade_strategy)
    # "grade/team 바로 밑" lex 패스용: 이 소프트 품질 항들을 동결한 뒤 mutex 위반만 최소화.
    # (team_min 은 하드 m.Add 로 이미 강제되므로 freeze 대상에서 제외 — team_balance/grade/handoff 만.)
    _gt_lex_terms = []
    try:
        if grade_strategy in ("TEAM", "COMBINED"):
            _tb_terms = add_team_balance_terms_fn(m, roster_system, X, join, leave)
            obj.extend(_tb_terms)
            _gt_lex_terms.extend(_tb_terms or [])
    except Exception as e:
        print("team_balance_objective_terms 예외 발생")
        print("e", e)
    # team_min 은 데이터(team_min_by_team) 존재 여부로만 활성. strategy는 weight tilt용.
    try:
        obj.extend(add_team_min_constraints(m, roster_system, X, join, leave, grade_strategy=grade_strategy, blocked_by_nurse=blocked_by_nurse))
    except Exception as e2:
        print("team_min_constraints(fallback) 예외 발생", e2)

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
        _gt_lex_terms.extend(grade_terms or [])
    except Exception as e:
        print("grade_constraints 예외 발생")
        print("e", e)
        pass

    # 팀×Grade handoff 제한 (COMBINED 전략에서만 활성)
    try:
        _gs_h = str(getattr(roster_system, "grade_strategy", "BASE") or "BASE").upper()
        if _gs_h == "COMBINED":
            _hf_terms = add_team_grade_handoff_constraints(
                m, roster_system, X, join, leave, grade_strategy=_gs_h
            )
            obj.extend(_hf_terms)
            _gt_lex_terms.extend(_hf_terms or [])
    except Exception as e:
        print("team_grade_handoff_constraints(fallback) 예외 발생", e)
    if _gt_lex_terms:
        m._grade_team_lex_terms = _gt_lex_terms  # type: ignore[attr-defined]

    # 3N 블록 유도: 2N2O+3N2O 동시 활성 시 2N 블록에 소프트 페널티
    try:
        cfg = roster_system.config
        _both_noff = (
            bool(getattr(cfg, "two_offs_after_two_nig", False))
            and bool(getattr(cfg, "two_offs_after_three_nig", False))
        )
        if _both_noff and PREFER_3N_BLOCK_PENALTY > 0:
            night_idx = cfg.shift_types.index("N")
            N = len(roster_system.nurses)
            n_forbid = set(getattr(roster_system, "n_forbid_n", set()) or set())
            for n in range(N):
                if n in n_forbid:
                    continue
                T0, T1 = join[n], leave[n]
                for d in range(T0 + 1, T1):
                    if blocked_by_nurse and d in blocked_by_nurse.get(n, set()):
                        continue
                    xn_prev = X(n, d - 1, night_idx)
                    xn_curr = X(n, d, night_idx)
                    if d - 2 >= T0 and not (blocked_by_nurse and (d - 2) in blocked_by_nurse.get(n, set())):
                        not_prev2 = X(n, d - 2, night_idx).Not()
                    else:
                        not_prev2 = None
                    if d + 1 <= T1 and not (blocked_by_nurse and (d + 1) in blocked_by_nurse.get(n, set())):
                        not_next = X(n, d + 1, night_idx).Not()
                    else:
                        not_next = None
                    is_2n = m.NewBoolVar(f"fb_is2n_{n}_{d}")
                    conds = [xn_prev, xn_curr]
                    if not_prev2 is not None:
                        conds.append(not_prev2)
                    if not_next is not None:
                        conds.append(not_next)
                    m.Add(sum(conds) - len(conds) + 1 <= is_2n)
                    m.Add(is_2n <= xn_prev)
                    m.Add(is_2n <= xn_curr)
                    if not_prev2 is not None:
                        m.Add(is_2n <= not_prev2)
                    if not_next is not None:
                        m.Add(is_2n <= not_next)
                    obj.append(-PREFER_3N_BLOCK_PENALTY * is_2n)
    except Exception:
        pass

    return obj
