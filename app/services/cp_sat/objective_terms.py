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
    PREFER_3N_BLOCK_PENALTY,
    PREFERENCE_SCORE_SCALE,
    WEEK_OFF_SHORT_PENALTY,
)
from services.constraints.grade_constraints import add_grade_constraints
from services.constraints.team_constraints import add_team_min_constraints
from services.constraints.team_grade_handoff_constraints import (
    add_team_grade_handoff_constraints,
)
from services.objectives.team_objective import add_team_balance_objective_terms
from services.cp_sat.allowed_shift_types import normalize_allowed_shift_codes, is_n_only_profile
from services.day_windows import iter_nurse_days, build_active_days


def _n_forbid_n_set(rs, join: list[int], leave: list[int]) -> set[int]:
    """N 전일 금지 간호사 인덱스 집합. initial_forbidden에서 모든 근무일에 N이 금지된 n만 반환."""
    n_forbid_n: set[int] = set()
    initial_forbidden = getattr(rs, "initial_forbidden", None)
    if not isinstance(initial_forbidden, dict):
        initial_forbidden = {}
    for n in range(len(rs.nurses)):
        t0, t1 = join[n], leave[n]
        if t0 > t1:
            continue
        raw = getattr(rs.nurses[n], "is_night_nurse", None)
        allowed = normalize_allowed_shift_codes(raw, use_mid=bool(getattr(rs.config, "use_mid", False)))
        if allowed and "N" not in allowed:
            n_forbid_n.add(n)
            continue
        n_days = t1 - t0 + 1
        forbid_cnt = sum(
            1 for d in range(t0, t1 + 1)
            if "N" in initial_forbidden.get((n, d), set())
        )
        if forbid_cnt == n_days:
            n_forbid_n.add(n)
    return n_forbid_n


def add_kld_distribution_terms(
    *,
    m: cp_model.CpModel,
    rs,
    X,
    join: list[int],
    leave: list[int],
    fixed_cnt: list[list[int]] | None = None,
    logger_prefix: str = "[objective_terms]",
    stage_label: str = "메인",
    blocked_by_nurse: dict[int, set[int]] | None = None,
) -> list:
    """KLD 이론 기반 D/E/N 균등 분배.

    핵심 원리:
    - 커버리지 비율(Q)에서 per-nurse 비례 target 산출
    - 역비율 가중(inverse-proportion): 희소 시프트 편차에 높은 페널티
    - 간호사 간 분포 거리(range) 최소화
    - 총 근무수(D+E+N) 균등화
    """
    import calendar

    cfg = rs.config
    D = rs.num_days
    S = cfg.num_shifts
    use_mid = bool(getattr(cfg, "use_mid", False))

    # ── 대상 시프트 코드 결정 ──
    work_codes = [c for c in ["D", "E", "N"] if c in cfg.shift_types]
    if use_mid and "M" in cfg.shift_types:
        work_codes.append("M")
    if not work_codes:
        return []
    work_indices = {c: cfg.shift_types.index(c) for c in work_codes}

    # ── 일별 수요 산출 ──
    daily_need: dict[str, list[int]] = {}
    for c in work_codes:
        if (
            hasattr(cfg, "daily_shift_requirements_by_day")
            and isinstance(cfg.daily_shift_requirements_by_day, list)
            and len(cfg.daily_shift_requirements_by_day) == D
        ):
            daily_need[c] = [
                int((cfg.daily_shift_requirements_by_day[d] or {}).get(c, 0) or 0)
                for d in range(D)
            ]
        else:
            base = int((cfg.daily_shift_requirements or {}).get(c, 0) or 0)
            daily_need[c] = [base] * D

    fc = fixed_cnt if fixed_cnt is not None else [[0] * S for _ in range(D)]
    total_need: dict[str, int] = {}
    for c in work_codes:
        ci = work_indices[c]
        total_need[c] = sum(
            max(0, daily_need[c][d] - int(fc[d][ci] if d < len(fc) else 0))
            for d in range(D)
        )
    total_all = sum(total_need.values())
    if total_all <= 0:
        return []

    # ── 비율 Q(s) = need(s) / total ──
    Q: dict[str, float] = {c: total_need[c] / total_all for c in work_codes}

    # ── 역비율 가중치: w_s = base / Q(s) ──
    # 희소 시프트(N)일수록 높은 가중치
    BASE_W = int(getattr(cfg, "kld_base_weight", 30000) or 30000)
    w_shift: dict[str, int] = {}
    for c in work_codes:
        if Q[c] > 0:
            w_shift[c] = int(BASE_W / Q[c])
        else:
            w_shift[c] = BASE_W

    # ── 글로벌 range 억제 가중치 ──
    W_RANGE = int(getattr(cfg, "kld_range_weight", 1200000) or 1200000)
    # ── 총근무 균등 가중치 ──
    W_TOTAL = int(getattr(cfg, "kld_total_weight", 100000) or 100000)

    # ── N 전일 금지 / 프로필 분류 ──
    n_forbid_n = _n_forbid_n_set(rs, join, leave)
    all_codes_set = set(work_codes)

    # ── 주말일수 / OFF 요청 수 (band 보정용) ──
    _year = int(getattr(cfg, "year", 2026) or 2026)
    _month = int(getattr(cfg, "month", 1) or 1)
    weekend_days_in_month = sum(
        1 for d in range(1, D + 1) if calendar.weekday(_year, _month, d) >= 5
    )
    off_request_cnt: dict[int, int] = {}
    pref = getattr(rs, "preference_matrix", None)
    if pref is not None:
        off_idx = cfg.shift_types.index("O") if "O" in cfg.shift_types else -1
        if off_idx >= 0:
            for n in range(len(rs.nurses)):
                cnt = sum(1 for d in range(D) if pref[n, d, off_idx] > 0)
                if cnt > 0:
                    off_request_cnt[n] = cnt

    # ── 간호사 분류 ──
    normals: list[int] = []
    for i, nu in enumerate(rs.nurses):
        raw = getattr(nu, "is_night_nurse", None)
        if not is_n_only_profile(raw, use_mid=use_mid):
            normals.append(i)
    if len(normals) < 2:
        return []

    def _nurse_work_days(n: int) -> int:
        """간호사 n의 유효 근무일수 추정."""
        _blocked = len(blocked_by_nurse.get(n, set())) if blocked_by_nurse else 0
        _active = leave[n] - join[n] + 1 - _blocked
        if bool(getattr(rs.nurses[n], "is_weekend_off", False)):
            _active -= weekend_days_in_month
        _off_req = off_request_cnt.get(n, 0)
        _active -= _off_req
        # OFF 일수 추정 (min_off 기준, 이미 차감된 주말/요청 제외)
        _base_off = int(getattr(cfg, "standard_personal_off_days", 8) or 8)
        _already_deducted = (
            (weekend_days_in_month if bool(getattr(rs.nurses[n], "is_weekend_off", False)) else 0)
            + _off_req
        )
        _remaining_off = max(0, _base_off - _already_deducted)
        return max(1, _active - _remaining_off)

    obj: list = []

    # ══════════════════════════════════════════════
    # Layer 1: Per-shift 비례 target + 역비율 가중 편차
    # ══════════════════════════════════════════════
    for c in work_codes:
        s_idx = work_indices[c]
        w_dev = w_shift[c]

        # 해당 시프트에 배정 가능한 간호사 필터링
        eligible: list[int] = []
        for n in normals:
            if c == "N" and n in n_forbid_n:
                continue
            allowed = normalize_allowed_shift_codes(
                getattr(rs.nurses[n], "is_night_nurse", None), use_mid=use_mid,
            ) or all_codes_set
            if c not in allowed:
                continue
            if len(allowed) <= 1:
                continue
            eligible.append(n)

        if not eligible:
            continue

        # 글로벌 range 변수
        max_var = m.NewIntVar(0, D, f"kld_{c}_max_{stage_label}")
        min_var = m.NewIntVar(0, D, f"kld_{c}_min_{stage_label}")

        # N 블록 단위 target 재계산: 2N→2OFF 구조에서 N은 2 or 3 블록 단위
        _use_n_block = (
            c == "N"
            and (
                bool(getattr(cfg, "two_offs_after_two_nig", False))
                or bool(getattr(cfg, "two_offs_after_three_nig", False))
            )
        )
        _prefer_3n = (
            bool(getattr(cfg, "two_offs_after_two_nig", False))
            and bool(getattr(cfg, "two_offs_after_three_nig", False))
        )
        # 평균 블록 크기: 3N 유도면 2.5N/block, 2N만이면 2N/block
        _avg_block_n = 2.5 if _prefer_3n else 2.0
        # 블록당 소요일수: NN+OO=4일 or NNN+OO=5일 → 평균 4.5 or 4.0
        _days_per_block = (_avg_block_n + 2)

        for n in eligible:
            work_d = _nurse_work_days(n)

            if _use_n_block:
                # N 블록 기반 target: 가용일수에서 가능한 블록 수 → N 수
                max_blocks = max(0, work_d / _days_per_block)
                # 전체 필요 블록 수
                total_blocks = total_need[c] / _avg_block_n
                # per-nurse 블록 수 = 전체 블록 / eligible
                nurse_blocks = total_blocks / max(1, len(eligible))
                target_n = nurse_blocks * _avg_block_n
                # 가용일수 비례 보정
                if max_blocks < nurse_blocks:
                    target_n = max_blocks * _avg_block_n
                # 2N 단위에 가까운 정수로 반올림
                target_low = max(0, round(target_n - 0.5))
                target_high = target_low + 1
                # 홀수 target이면 ±1 허용 (2+3=5 가능)
            else:
                target = work_d * Q[c]
                target_low = int(target)
                target_high = target_low + 1

            tot = sum(X(n, d, s_idx) for d in iter_nurse_days(n, join, leave, blocked_by_nurse))
            m.Add(max_var >= tot)
            m.Add(min_var <= tot)

            # U: 볼록(piecewise-linear) 편차 페널티 — KL divergence 근사
            # dev = d1 + d2 + d3 (d1 ≤ 1, d2 ≤ 2, d3 ≤ D)
            # penalty = w·d1 + 3w·d2 + 10w·d3 (평균에서 멀수록 급격히 증가)
            for side_tag, lb_expr in (
                ("L", target_low - tot),  # 미달 측
                ("H", tot - target_high), # 초과 측
            ):
                d_tot = m.NewIntVar(0, D, f"kld_{c}_d{side_tag}tot_{stage_label}_{n}")
                d1 = m.NewIntVar(0, 1, f"kld_{c}_d{side_tag}1_{stage_label}_{n}")
                d2 = m.NewIntVar(0, 2, f"kld_{c}_d{side_tag}2_{stage_label}_{n}")
                d3 = m.NewIntVar(0, D, f"kld_{c}_d{side_tag}3_{stage_label}_{n}")
                m.Add(d_tot >= lb_expr)
                m.Add(d_tot == d1 + d2 + d3)
                obj.append(-w_dev * d1)
                obj.append(-3 * w_dev * d2)
                obj.append(-10 * w_dev * d3)

        # range 억제: max - min 최소화 (U2: 3-tier 볼록 페널티)
        range_var = m.NewIntVar(0, D, f"kld_{c}_range_{stage_label}")
        m.Add(range_var >= max_var - min_var)
        r1 = m.NewIntVar(0, 1, f"kld_{c}_r1_{stage_label}")
        r2 = m.NewIntVar(0, 2, f"kld_{c}_r2_{stage_label}")
        r3 = m.NewIntVar(0, D, f"kld_{c}_r3_{stage_label}")
        m.Add(range_var == r1 + r2 + r3)
        obj.append(-W_RANGE * r1)
        obj.append(-3 * W_RANGE * r2)
        obj.append(-10 * W_RANGE * r3)
        # min 끌어올림
        obj.append(W_RANGE * min_var)

        _block_info = ""
        if _use_n_block:
            _block_info = (
                f", n_block=True, avg_block={_avg_block_n}, "
                f"days_per_block={_days_per_block}, "
                f"total_blocks={total_need[c]/_avg_block_n:.1f}"
            )
        print(
            f"{logger_prefix} [KLD-{c}] ({stage_label}): "
            f"eligible={len(eligible)}, Q={Q[c]:.3f}, "
            f"w_dev={w_dev}, w_range={W_RANGE}, total_need={total_need[c]}"
            f"{_block_info}"
        )

    # ══════════════════════════════════════════════
    # Layer 2: 총 근무수(D+E+N) 균등화
    # ══════════════════════════════════════════════
    total_work_need = sum(total_need[c] for c in work_codes)
    target_work = total_work_need // len(normals)
    target_work_high = target_work + (1 if total_work_need % len(normals) else 0)

    max_work = m.NewIntVar(0, D, f"kld_tw_max_{stage_label}")
    min_work = m.NewIntVar(0, D, f"kld_tw_min_{stage_label}")
    for n in normals:
        allowed = normalize_allowed_shift_codes(
            getattr(rs.nurses[n], "is_night_nurse", None), use_mid=use_mid,
        ) or all_codes_set
        if len(allowed) <= 1:
            continue
        tot = sum(
            X(n, d, work_indices[c])
            for c in work_codes
            if c in allowed
            for d in iter_nurse_days(n, join, leave, blocked_by_nurse)
        )
        m.Add(max_work >= tot)
        m.Add(min_work <= tot)

        # U: 총근무 편차도 3-tier 볼록 페널티 (KL 근사)
        for side_tag, lb_expr in (
            ("L", target_work - tot),
            ("H", tot - target_work_high),
        ):
            d_tot = m.NewIntVar(0, D, f"kld_tw_d{side_tag}tot_{stage_label}_{n}")
            d1 = m.NewIntVar(0, 1, f"kld_tw_d{side_tag}1_{stage_label}_{n}")
            d2 = m.NewIntVar(0, 2, f"kld_tw_d{side_tag}2_{stage_label}_{n}")
            d3 = m.NewIntVar(0, D, f"kld_tw_d{side_tag}3_{stage_label}_{n}")
            m.Add(d_tot >= lb_expr)
            m.Add(d_tot == d1 + d2 + d3)
            obj.append(-W_TOTAL * d1)
            obj.append(-3 * W_TOTAL * d2)
            obj.append(-10 * W_TOTAL * d3)

    # U2: 총근무 range에도 3-tier 볼록 페널티
    range_work = m.NewIntVar(0, D, f"kld_tw_range_{stage_label}")
    m.Add(range_work >= max_work - min_work)
    rw1 = m.NewIntVar(0, 1, f"kld_tw_r1_{stage_label}")
    rw2 = m.NewIntVar(0, 2, f"kld_tw_r2_{stage_label}")
    rw3 = m.NewIntVar(0, D, f"kld_tw_r3_{stage_label}")
    m.Add(range_work == rw1 + rw2 + rw3)
    obj.append(-W_TOTAL * 2 * rw1)
    obj.append(-3 * W_TOTAL * 2 * rw2)
    obj.append(-10 * W_TOTAL * 2 * rw3)

    print(
        f"{logger_prefix} [KLD-총근무] ({stage_label}): "
        f"nurses={len(normals)}, target=[{target_work},{target_work_high}], "
        f"total_need={total_work_need}, w={W_TOTAL}"
    )

    return obj


def add_even_mid_distribution_terms(
    *,
    m: cp_model.CpModel,
    rs,
    X,
    join: list[int],
    leave: list[int],
    fixed_cnt: list[list[int]] | None = None,
) -> list:
    cfg = rs.config
    if "M" not in cfg.shift_types:
        return []
    if not bool(getattr(cfg, "use_mid", False)):
        return []
    if not bool(getattr(cfg, "even_mids", True)):
        return []

    weight = int(getattr(cfg, "mid_deviation_penalty_weight", NIGHT_DEVIATION_PENALTY) or NIGHT_DEVIATION_PENALTY)
    if weight <= 0:
        return []

    N = len(rs.nurses)
    D = rs.num_days
    S = cfg.num_shifts
    mid_idx = cfg.shift_types.index("M")
    fc = fixed_cnt if fixed_cnt is not None else [[0] * S for _ in range(D)]
    initial_forbidden = getattr(rs, "initial_forbidden", None)
    if not isinstance(initial_forbidden, dict):
        initial_forbidden = {}

    candidates: list[int] = []
    for n, nu in enumerate(rs.nurses):
        raw = getattr(nu, "is_night_nurse", None)
        is_n_only = is_n_only_profile(raw, use_mid=bool(getattr(cfg, "use_mid", False)))
        if is_n_only:
            continue

        has_m_window = False
        for d in iter_nurse_days(n, join, leave, blocked_by_nurse=None):
            if "M" not in initial_forbidden.get((n, d), set()):
                has_m_window = True
                break
        if has_m_window:
            candidates.append(n)

    if not candidates:
        return []

    if (
        hasattr(cfg, "daily_shift_requirements_by_day")
        and isinstance(cfg.daily_shift_requirements_by_day, list)
        and len(cfg.daily_shift_requirements_by_day) == D
    ):
        daily_need_m = [
            int((cfg.daily_shift_requirements_by_day[d] or {}).get("M", 0) or 0)
            for d in range(D)
        ]
    else:
        base_m = int((cfg.daily_shift_requirements or {}).get("M", 0) or 0)
        daily_need_m = [base_m for _ in range(D)]

    total_need_m = 0
    for d in range(D):
        total_need_m += max(0, daily_need_m[d] - int(fc[d][mid_idx] if d < len(fc) else 0))
    if total_need_m <= 0:
        return []

    low = total_need_m // len(candidates)
    high = low + (1 if (total_need_m % len(candidates)) else 0)
    obj: list = []
    for n in candidates:
        tot_mid = sum(X(n, d, mid_idx) for d in iter_nurse_days(n, join, leave, blocked_by_nurse=None))
        dev_low = m.NewIntVar(0, D, f"devMlow_{n}")
        dev_high = m.NewIntVar(0, D, f"devMhigh_{n}")
        m.Add(dev_low >= low - tot_mid)
        m.Add(dev_high >= tot_mid - high)
        obj.append(-weight * dev_low)
        obj.append(-weight * dev_high)
    return obj


def add_even_night_minmax_distribution_terms(
    *,
    m: cp_model.CpModel,
    rs,
    X,
    join: list[int],
    leave: list[int],
    fixed_cnt: list[list[int]] | None = None,
    logger_prefix: str = "[objective_terms]",
    stage_label: str = "메인",
    blocked_by_nurse: dict[int, set[int]] | None = None,
) -> list:
    cfg = rs.config
    if "N" not in cfg.shift_types:
        return []
    if not bool(getattr(cfg, "even_nights", False)):
        return []

    N = len(rs.nurses)
    D = rs.num_days
    S = cfg.num_shifts
    night_idx = cfg.shift_types.index("N")

    normals: list[int] = []
    for i, nu in enumerate(rs.nurses):
        raw = getattr(nu, "is_night_nurse", None)
        is_n_only = is_n_only_profile(raw, use_mid=bool(getattr(cfg, "use_mid", False)))
        if not is_n_only:
            normals.append(i)
    n_forbid_n = _n_forbid_n_set(rs, join, leave)
    normals_can_n = [n for n in normals if n not in n_forbid_n]
    if not normals_can_n:
        print(
            f"{logger_prefix} [N균등] even_nights 켜짐 but "
            "normals_can_N(비야간전담·N가능)=0 → 스킵"
        )
        return []

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
        need = max(0, daily_need_n[d] - int(fc[d][night_idx] if d < len(fc) else 0))
        total_need_n += need
    if total_need_n <= 0:
        print(f"{logger_prefix} [N균등] even_nights 켜짐 but total_need_n=0 → 패널티 미적용")
        return []

    low = total_need_n // len(normals_can_n)
    high = low + (1 if (total_need_n % len(normals_can_n)) else 0)
    w_primary = int(
        getattr(cfg, "night_minmax_primary_weight", NIGHT_DEVIATION_PENALTY * 20)
        or NIGHT_DEVIATION_PENALTY * 20
    )
    w_secondary = int(
        getattr(cfg, "night_minmax_secondary_weight", NIGHT_DEVIATION_PENALTY)
        or NIGHT_DEVIATION_PENALTY
    )

    print(
        f"{logger_prefix} [N균등] min-max 적용({stage_label}): "
        f"normals_can_N={len(normals_can_n)}, n_forbid_N={len(n_forbid_n)}, "
        f"band=[{low},{high}], total_need_n={total_need_n}, "
        f"w_primary={w_primary}, w_secondary={w_secondary}"
    )

    obj: list = []
    max_n = m.NewIntVar(0, D, f"night_max_{stage_label}")
    for n in normals_can_n:
        # blocked 간호사: active_days 비례로 N band 축소
        _n_blocked = len(blocked_by_nurse.get(n, set())) if blocked_by_nurse else 0
        _active = leave[n] - join[n] + 1 - _n_blocked
        if _n_blocked > 0 and _active < D:
            _ratio = max(0.0, _active / max(1, D))
            low_n = max(0, round(low * _ratio))
            high_n = max(low_n, round(high * _ratio))
            print(
                f"{logger_prefix} [N균등] nurse_idx={n} blocked={_n_blocked}, "
                f"active={_active}/{D}, band=[{low_n},{high_n}] (비례 축소)"
            )
        else:
            low_n, high_n = low, high
        tot_nights = sum(X(n, d, night_idx) for d in iter_nurse_days(n, join, leave, blocked_by_nurse))
        m.Add(max_n >= tot_nights)
        dev_low = m.NewIntVar(0, D, f"night_devL_{stage_label}_{n}")
        dev_high = m.NewIntVar(0, D, f"night_devH_{stage_label}_{n}")
        m.Add(dev_low >= low_n - tot_nights)
        m.Add(dev_high >= tot_nights - high_n)
        obj.append(-w_secondary * dev_low)
        obj.append(-w_secondary * dev_high)
    obj.append(-w_primary * max_n)
    return obj


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
    blocked_by_nurse: dict[int, set[int]] | None = None,
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
    active_days = build_active_days(N, join, leave, blocked_by_nurse)
    S = cfg.num_shifts
    idx = {c: cfg.shift_types.index(c) for c in ("D", "E", "N", "O")}
    day, eve, night, off = idx["D"], idx["E"], idx["N"], idx["O"]

    # (0) 커버리지 부족 패널티(강하게): shortage 변수에 큰 음수 가중치 적용
    for sh, code in coverage_shortage_vars:
        obj.append(-FALLBACK_COVERAGE_SHORT_WEIGHT * sh)

    for n in range(N):
        nu = rs.nurses[n]
        raw = getattr(nu, "is_night_nurse", None)
        is_n_only = is_n_only_profile(raw, use_mid=bool(getattr(cfg, "use_mid", False)))

        for d in iter_nurse_days(n, join, leave, blocked_by_nurse):
            for s in range(S):
                base_score = int(P[n, d, s] * PREFERENCE_SCORE_SCALE) if d < D else 0
                if is_n_only and s == night:
                    base_score += N_ONLY_NIGHT_BONUS
                obj.append(base_score * X(n, d, s))

    # (4-0) 추가 OFF(여유 OFF) 기피
    try:
        off_penalty = int(getattr(cfg, "extra_off_penalty_weight", 0) or 0)
        if off_penalty > 0:
            for n in range(N):
                for d in iter_nurse_days(n, join, leave, blocked_by_nurse):
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
                for d in iter_nurse_days(n, join, leave, blocked_by_nurse):
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
                if join[n] <= d <= leave[n] and (nu.experience_years or 0) >= cfg.min_experience_per_shift
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

    obj.extend(
        add_kld_distribution_terms(
            m=m,
            rs=rs,
            X=X,
            join=join,
            leave=leave,
            fixed_cnt=fixed_cnt,
            logger_prefix="[objective_terms]",
            stage_label="메인",
            blocked_by_nurse=blocked_by_nurse,
        )
    )

    # (4-4) N-O-D/E 패턴
    if getattr(cfg, "nod_noe", True):
        for n in range(N):
            for d in range(join[n], leave[n] - 2):
                if any((n, d + k) not in active_days for k in range(4)):
                    continue
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
            for d in iter_nurse_days(n, join, leave, blocked_by_nurse):
                iso = m.NewIntVar(0, 1, f"iso_{n}_{d}")
                m.Add(iso >= X(n, d, off) - X(n, d - 1, off) - X(n, d + 1, off))
                m.Add(iso <= X(n, d, off))
                m.Add(iso <= 1 - X(n, d - 1, off))
                m.Add(iso <= 1 - X(n, d + 1, off))
                obj.append(-ISOLATED_OFF_PENALTY * iso)

    # (4-5a) OFF 연속 배정 보너스 (sequential_offs)
    if getattr(cfg, "sequential_offs", True):
        SEQUENTIAL_OFF_BONUS = 50000  # 연속 휴무 보너스 가중치 (KLD 균등과 균형)
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
        if grade_strategy in ("TEAM", "COMBINED"):
            obj.extend(add_team_balance_objective_terms(m, rs, X, join, leave, blocked_by_nurse=blocked_by_nurse))

    # (4-6-tm) team_min 제약: 데이터(team_min_by_team) 존재 여부만으로 활성. strategy는 weight tilt용.
    try:
        _gs_tm = str(getattr(rs, "grade_strategy", "BASE") or "BASE").upper()
        obj.extend(add_team_min_constraints(m, rs, X, join, leave, grade_strategy=_gs_tm, blocked_by_nurse=blocked_by_nurse))
    except Exception as e:
        print("team_min_constraints 예외 발생", e)

    # (4-6a) Grade 분배 제약: grade_config 존재 여부만으로 활성. strategy는 weight tilt용.
    try:
        _gs = str(getattr(rs, "grade_strategy", "BASE") or "BASE").upper()
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

    # (4-6b) 팀×Grade handoff 제한 (COMBINED 전략에서만 활성)
    try:
        _gs2 = str(getattr(rs, "grade_strategy", "BASE") or "BASE").upper()
        if _gs2 == "COMBINED":
            obj.extend(
                add_team_grade_handoff_constraints(
                    m, rs, X, join, leave, grade_strategy=_gs2
                )
            )
    except Exception as e:
        print("team_grade_handoff_constraints 예외 발생", e)

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

    # (4-9) 3N 블록 유도: 2N2O+3N2O 동시 활성 시 2N 블록에 소프트 페널티
    try:
        _both_noff = (
            bool(getattr(cfg, "two_offs_after_two_nig", False))
            and bool(getattr(cfg, "two_offs_after_three_nig", False))
        )
        if _both_noff and PREFER_3N_BLOCK_PENALTY > 0:
            night_idx = cfg.shift_types.index("N")
            n_forbid = set(getattr(rs, "n_forbid_n", set()) or set())
            for n in range(N):
                if n in n_forbid:
                    continue
                T0, T1 = join[n], leave[n]
                for d in range(T0 + 1, T1):
                    if blocked_by_nurse and d in blocked_by_nurse.get(n, set()):
                        continue
                    xn_prev = X(n, d - 1, night_idx)
                    xn_curr = X(n, d, night_idx)
                    # !N(d-2): 블록 시작 확인
                    if d - 2 >= T0 and not (blocked_by_nurse and (d - 2) in blocked_by_nurse.get(n, set())):
                        not_prev2 = X(n, d - 2, night_idx).Not()
                    else:
                        not_prev2 = None
                    # !N(d+1): 블록 종료 확인
                    if d + 1 <= T1 and not (blocked_by_nurse and (d + 1) in blocked_by_nurse.get(n, set())):
                        not_next = X(n, d + 1, night_idx).Not()
                    else:
                        not_next = None
                    is_2n = m.NewBoolVar(f"is2n_{n}_{d}")
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


def add_per_nurse_target_distribution_terms(
    m,
    rs,
    X,
    join: list[int],
    leave: list[int],
    fixed: dict,
    weight: int = 10,
) -> list:
    """각 간호사의 avail 기반 D/E/N target 과 실제 count 간 편차 패널티.

    배정 가능 일수(avail) 비율로 나눈 target 에 가까워지도록 유도.
    전담 간호사(한 shift 가 월 40% 이상 고정)는 제외.
    Maximize 모델용 obj_terms(음수) 반환.
    """
    cfg = rs.config
    work_codes = [c for c in ["D", "E", "N"] if c in cfg.shift_types]
    if not work_codes:
        return []
    shift_idx = {c: cfg.shift_types.index(c) for c in work_codes}
    off_idx = cfg.shift_types.index("O") if "O" in cfg.shift_types else None
    N = len(rs.nurses)
    D_phys = rs.num_days

    fixed_shift = [{c: 0 for c in work_codes} for _ in range(N)]
    fixed_off_n = [0] * N
    fixed_other_n = [0] * N
    for (n, d), s in fixed.items():
        if d >= D_phys:
            continue
        if off_idx is not None and s == off_idx:
            fixed_off_n[n] += 1
            continue
        matched = False
        for c in work_codes:
            if s == shift_idx[c]:
                fixed_shift[n][c] += 1
                matched = True
                break
        if not matched:
            fixed_other_n[n] += 1

    dedi_threshold = 0.4 * D_phys
    is_dedi = [False] * N
    for n in range(N):
        for c in work_codes:
            if fixed_shift[n][c] > dedi_threshold:
                is_dedi[n] = True
                break

    avail = [0] * N
    for n in range(N):
        j_n = join[n] if n < len(join) else 0
        l_n = min(leave[n] if n < len(leave) else D_phys - 1, D_phys - 1)
        window = max(0, l_n - j_n + 1)
        used = fixed_off_n[n] + fixed_other_n[n] + sum(fixed_shift[n].values())
        avail[n] = max(0, window - used)

    mixed = [n for n in range(N) if not is_dedi[n] and avail[n] > 0]
    if not mixed:
        return []
    total_avail = sum(avail[n] for n in mixed)
    if total_avail <= 0:
        return []

    default_req = getattr(cfg, "daily_shift_requirements", {}) or {}
    by_day = getattr(cfg, "daily_shift_requirements_by_day", None)

    def _need(code: str, d: int) -> int:
        req = by_day[d] if isinstance(by_day, list) and d < len(by_day) else default_req
        return int(req.get(code, 0)) if isinstance(req, dict) else 0

    obj_terms: list = []
    for c in work_codes:
        total_demand = sum(_need(c, d) for d in range(D_phys))
        dedi_fixed = sum(fixed_shift[n][c] for n in range(N) if is_dedi[n])
        mixed_fixed = sum(fixed_shift[n][c] for n in mixed)
        mixed_demand = max(0, total_demand - dedi_fixed)
        remaining = max(0, mixed_demand - mixed_fixed)

        for n in mixed:
            share = remaining * avail[n] / total_avail if total_avail > 0 else 0
            target = fixed_shift[n][c] + int(round(share))
            j_n = join[n] if n < len(join) else 0
            l_n = min(leave[n] if n < len(leave) else D_phys - 1, D_phys - 1)
            if j_n > l_n:
                continue
            count_expr = sum(X(n, d, shift_idx[c]) for d in range(j_n, l_n + 1))
            dev = m.NewIntVar(0, D_phys, f"pnt_dev_{n}_{c}")
            diff = m.NewIntVar(-D_phys, D_phys, f"pnt_diff_{n}_{c}")
            m.Add(diff == count_expr - target)
            m.AddAbsEquality(dev, diff)
            obj_terms.append(-weight * dev)

    return obj_terms
