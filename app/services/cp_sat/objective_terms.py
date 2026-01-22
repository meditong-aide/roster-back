"""CP-SAT 목적 함수(soft objective) 구성 모듈."""

from __future__ import annotations

from typing import Callable, Iterable

from ortools.sat.python import cp_model

from services.cp_sat.hardcoded_weights import (
    EXPERIENCE_SHORT_PENALTY,
    ISOLATED_OFF_PENALTY,
    NIGHT_DEVIATION_PENALTY,
    NOD_NOE_PENALTY,
    N_ONLY_NIGHT_BONUS,
    PREFERENCE_SCORE_SCALE,
    WEEK_OFF_SHORT_PENALTY,
)
from services.objectives.team_objective import add_team_balance_objective_terms


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
                base_score = int(P[n, d, s] * PREFERENCE_SCORE_SCALE)
                if is_n_only and s == night:
                    base_score += N_ONLY_NIGHT_BONUS
                obj.append(base_score * X(n, d, s))

    # (4-0) 추가 OFF(여유 OFF) 기피
    try:
        off_penalty = int(getattr(cfg, "extra_off_penalty_weight", 0) or 0)
        if off_penalty > 0:
            for n in range(N):
                for d in range(join[n], leave[n] + 1):
                    obj.append(-off_penalty * X(n, d, off))
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

    # (4-3) 야간 균등 (편차에 선형 패널티)
    if cfg.even_nights:
        normals = [i for i, nu in enumerate(rs.nurses) if nu.is_night_nurse != 3]
        if normals:
            total_req = sum(cfg.daily_shift_requirements["N"] for _ in range(D))
            target = total_req // len(normals)
            for n in normals:
                totN = sum(X(n, d, night) for d in range(join[n], leave[n] + 1))
                devP = m.NewIntVar(0, D, f"devP_{n}")
                devN = m.NewIntVar(0, D, f"devN_{n}")
                m.Add(devP - devN == totN - target)
                obj.extend(
                    [
                        -NIGHT_DEVIATION_PENALTY * devP,
                        -NIGHT_DEVIATION_PENALTY * devN,
                    ]
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

    # (4-5) 고립 OFF
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
    except Exception:
        pass

    return obj


