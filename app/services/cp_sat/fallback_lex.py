"""폴백(서열) 최적화 로직 모듈.

이 모듈은 `cp_sat_basic.py`의 폴백(lexicographic) 최적화 블록을 분리한 것입니다.
동작/가중치/순서는 기존과 동일하게 유지합니다.
"""

from __future__ import annotations

import calendar
from datetime import timedelta
from typing import Dict, Optional

import numpy as np
from ortools.sat.python import cp_model

from services.cp_sat.hardcoded_weights import (
    FALLBACK_COVERAGE_SHORT_WEIGHT,
    FALLBACK_EXPERIENCE_SHORT_PENALTY,
    N_ONLY_NIGHT_BONUS,
    PREFERENCE_SCORE_SCALE,
)
from services.cp_sat.allowed_shift_types import (
    is_n_only_profile,
    normalize_allowed_shift_codes,
)
from services.cp_sat.fallback_objectives import build_fallback_stage3_objective_terms
from services.cp_sat.m_coverage import compute_main_bucket_indices
from services.cp_sat.night_distribution_log import log_n_even_distribution


def _cp_sat_status_to_text(status: int) -> str:
    """CP-SAT 상태 코드를 사람이 읽을 수 있는 문자열로 변환한다."""
    mapping = {
        cp_model.OPTIMAL: "OPTIMAL",
        cp_model.FEASIBLE: "FEASIBLE",
        cp_model.INFEASIBLE: "INFEASIBLE",
        cp_model.MODEL_INVALID: "MODEL_INVALID",
        cp_model.UNKNOWN: "UNKNOWN",
    }
    return mapping.get(status, f"UNKNOWN({status})")


def _load_off_policy_helpers():
    module = __import__("services.cp_sat.off_policy", fromlist=["*"])
    return (
        getattr(module, "build_off_partitions"),
        getattr(module, "compute_off_bounds"),
        getattr(module, "off_cap_semantics_label"),
        getattr(module, "resolve_effective_off_days"),
        getattr(module, "resolve_max_extra_off_days"),
    )


def _log_weekend_off_enforcement(
    roster_system,
    join: list[int],
    leave: list[int],
    weekend_days: set[int],
    fixed: dict[tuple[int, int], int],
    off_idx: int | None,
    logger_prefix: str,
) -> None:
    """주말 OFF 강제 제약 적용 내역을 간호사별로 출력한다."""
    if not getattr(roster_system.config, "weekend_off_only_enable", True):
        return
    if off_idx is None:
        return
    for n, nu in enumerate(roster_system.nurses):
        if not bool(getattr(nu, "is_weekend_off", False)):
            continue
        t0, t1 = join[n], leave[n]
        weekend_in_range = [d for d in sorted(weekend_days) if t0 <= d <= t1]
        weekend_days_1based = [d + 1 for d in weekend_in_range]
        forced_days = []
        skipped_fixed_days = []
        for d in weekend_in_range:
            if (n, d) in fixed and fixed[(n, d)] != off_idx:
                skipped_fixed_days.append(d + 1)
            else:
                forced_days.append(d + 1)
        nurse_id = getattr(nu, "nurse_id", "?")
        nurse_name = getattr(nu, "name", "?")
        print(
            f"{logger_prefix} [WeekendOff][Enforce] nurse_idx={n}, "
            f"nurse_id={nurse_id}, name={nurse_name}, "
            f"weekend_days={weekend_days_1based}, "
            f"forced_off_days={forced_days}, "
            f"skipped_fixed_days={skipped_fixed_days}"
        )


def _log_weekend_work_assignments(
    roster_system,
    weekend_days: set[int],
    off_idx: int | None,
    logger_prefix: str,
) -> None:
    """주말 근무 배정 여부를 간호사별로 출력한다."""
    shift_types = roster_system.config.shift_types
    off_code = shift_types[off_idx] if off_idx is not None else None
    for n, nu in enumerate(roster_system.nurses):
        weekend_work = []
        for d in sorted(weekend_days):
            if d < 0 or d >= roster_system.num_days:
                continue
            assigned_code = None
            for s_idx, code in enumerate(shift_types):
                if int(roster_system.roster[n, d, s_idx]) == 1:
                    assigned_code = code
                    break
            if assigned_code is None:
                continue
            if off_code is not None and assigned_code == off_code:
                continue
            weekend_work.append(f"{d + 1}:{assigned_code}")
        nurse_id = getattr(nu, "nurse_id", "?")
        nurse_name = getattr(nu, "name", "?")
        is_weekend_off = bool(getattr(nu, "is_weekend_off", False))
        # if weekend_work or is_weekend_off:
        #     print(
        #         f"{logger_prefix} [WeekendOff][Work] nurse_idx={n}, "
        #         f"nurse_id={nurse_id}, name={nurse_name}, "
        #         f"is_weekend_off={int(is_weekend_off)}, weekend_work={weekend_work}"
        #     )


def optimize_fallback_lex_hard_first(
    *,
    roster_system,
    time_limit_seconds: int,
    grouped: list[dict] | None,
    shift_type_map: dict[str, str] | None,
    logger_prefix: str,
    timer_cls,
    add_preceptor_terms_fn,
    add_team_balance_terms_fn,
    add_grade_constraints_fn,
    postprocess_rebalance_off_fn,
) -> bool:
    """하드 제약을 최우선으로 하는 서열(lexicographic) 폴백 최적화 수행.

    단계 개요:
    1단계(커버리지 우선): 일/교대 커버리지 부족(short) 최소화. 식: assigned + short - over == need.
    2단계(안전/법규): 1단계 최솟값(short 합)과 over 상한을 고정, 전이/연속/월간/주2OFF/회복/NOD/NOE/야간전담 위반을 정량 슬랙으로 최소화.
    3단계(품질/선호): 1,2단계 결과를 고정(특히 2단계에서 0이었던 위반 위치는 0으로 잠금)한 채 선호/공정성 최대화. 새 위반 생성 금지.

    Args:
        roster_system: 근무표 시스템 객체
        time_limit_seconds: 총 시간 제한(초)
        grouped: 교대 코드 매핑 정보(고정셀 main_code 정규화에 사용)
        shift_type_map: 근무 코드별 유형 매핑(예: 휴가/공가/교육 등)
        logger_prefix: 로그 접두사
        timer_cls: with 구문에 사용할 Timer 클래스
        add_preceptor_terms_fn: 프리셉터 목적함수 항 생성 함수
        add_team_balance_terms_fn: 팀 밸런스 목적함수 항 생성 함수
        add_grade_constraints_fn: Grade 제약 추가 함수
        postprocess_rebalance_off_fn: 후처리(OFF 재배치) 함수

    Returns:
        bool: 최종적으로 하드 위반 합이 0인 해를 달성했는지 여부
    """
    print(f"{logger_prefix} 폴백(서열) 최적화 시작…")

    # 동적 시간 배분(대략): 45% / 35% / 20%
    tl1 = max(5, int(time_limit_seconds * 0.45))
    tl2 = max(5, int(time_limit_seconds * 0.35))
    tl3 = max(3, time_limit_seconds - tl1 - tl2)

    N, D, S = len(roster_system.nurses), roster_system.num_days, roster_system.config.num_shifts
    cfg = roster_system.config
    (
        build_off_partitions,
        compute_off_bounds,
        off_cap_semantics_label,
        resolve_effective_off_days,
        resolve_max_extra_off_days,
    ) = _load_off_policy_helpers()
    effective_off_days, effective_off_source = resolve_effective_off_days(cfg)
    effective_max_extra = resolve_max_extra_off_days(cfg, 0)
    off_cap_semantics = off_cap_semantics_label()
    print(
        f"{logger_prefix} [OffPolicy][Fallback] effective_off_days={effective_off_days}, "
        f"source={effective_off_source}, max_extra_off_days={effective_max_extra}, "
        f"raw_off_days={getattr(cfg, 'off_days', None)}, cap_semantics={off_cap_semantics}"
    )

    # 공통 인덱스/구간
    idx = {c: roster_system.config.shift_types.index(c) for c in ("D", "E", "N", "O")}
    day_idx, eve_idx, night_idx, off_idx = idx["D"], idx["E"], idx["N"], idx["O"]
    mid_idx = roster_system.config.shift_types.index("M") if "M" in roster_system.config.shift_types else None
    has_w = "W" in roster_system.config.shift_types
    w_idx = roster_system.config.shift_types.index("W") if has_w else None                      # O 인덱스 e.g. 2
    off_exception_cells = set(getattr(roster_system.config, "off_exception_cells", []) or [])  # (n, d) 튜플 집합 e.g. {(0, 1), (1, 2)}
    off_exception_vacation_cells = set(
        getattr(roster_system.config, "off_exception_vacation_cells", []) or []              # (n, d) 튜플 집합 e.g. {(0, 1), (1, 2)}
    )
    vac_cells = set(off_exception_vacation_cells)
    # print('이미 있음 W', w_idx)
    # print('이미 있음 off_exception_cells', off_exception_cells)
    # print('이미 있음 off_exception_vacation_cells', off_exception_vacation_cells)
    first_day = roster_system.target_month
    D_phys = calendar.monthrange(first_day.year, first_day.month)[1]
    last_day = first_day + timedelta(days=D - 1)
    weekend_days = {d for d in range(D) if (first_day + timedelta(days=d)).weekday() >= 5}
    join, leave = [], []
    for nu in roster_system.nurses:
        j = (nu.joining_date - first_day).days if nu.joining_date else 0
        if nu.resignation_date:
            if nu.resignation_date < first_day:
                # 이번 달에 근무하지 않는 인원은 범위 밖으로 설정하여 변수 생성을 건너뛴다.
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

    # 고정셀(메인코드 정규화)
    code2main = {
        str(c).strip().upper(): str(r["main_code"]).strip().upper()
        for r in (grouped or [])
        for c in r["codes"]
    }
    code2type = {}
    if shift_type_map:
        code2type.update(shift_type_map)
    code2type.update(
        {
            str(c).strip().upper(): r.get("type")
            for r in (grouped or [])
            for c in r["codes"]
        }
    )
    shift_id_to_main_map = {
        str(k).strip().upper(): str(v).strip().upper()
        for k, v in (getattr(roster_system, "shift_id_to_main", {}) or {}).items()
        if str(k or "").strip() and str(v or "").strip()
    }

    def _normalize_fixed_to_main(raw_code: object) -> str:
        code = str(raw_code or "").strip().upper()
        if not code:
            return ""
        mapped = code2main.get(code) or shift_id_to_main_map.get(code) or code
        if mapped in {"OFF", "주"}:
            return "O"
        return mapped

    fixed, fixed_cnt = {}, [[0] * S for _ in range(D)]
    fixed_type_by_cell: dict[tuple[int, int], Optional[str]] = {}
    # print('이미 있음 fixed_type_by_cell', fixed_type_by_cell)
    for c in getattr(roster_system, "fixed_cells", []) or []:
        
        n, d = c["nurse_index"], c["day_index"]
        s_main = _normalize_fixed_to_main(c.get("shift"))
        if s_main not in roster_system.config.shift_types:
            print(
                f"{logger_prefix} fixed 셀 코드 스킵(미지원 메인코드): "
                f"n={n}, d={d + 1}, raw={c.get('shift')}, main={s_main}"
            )
            continue
        s_idx = roster_system.config.shift_types.index(s_main)
        # print('이미 있음 c', c, s_idx)
        fixed[(n, d)] = s_idx
        fixed_cnt[d][s_idx] += 1
        # print('fallback fixed', fixed)
        # 코드에 타입 매핑이 없으면 메인 코드 기준으로 재시도
        raw_code = str(c.get("shift") or "").strip().upper()
        fixed_type_by_cell[(n, d)] = code2type.get(raw_code) or code2type.get(s_main)

        # print('이미 있음 fixed_type_by_cell', fixed_type_by_cell)
        # print('이미 있음 fixed_type_by_cell', fixed_type_by_cell)

    # 초기 금지(경계) 맵
    initial_forbidden = (
        getattr(roster_system, "initial_forbidden", {})
        if isinstance(getattr(roster_system, "initial_forbidden", {}), dict)
        else {}
    )

    # ── 프리셉티 인덱스 사전 계산 (fallback에서도 follow 모드 지원) ──
    preceptee_follow = bool(getattr(cfg, 'preceptee_on', False))
    preceptee_indices: set[int] = set()
    if preceptee_follow:
        _fb_id_to_idx = {nu.db_id: n for n, nu in enumerate(roster_system.nurses)}
        for n, nu in enumerate(roster_system.nurses):
            pid = getattr(nu, 'preceptor_id', None)
            if pid and pid in _fb_id_to_idx:
                preceptee_indices.add(n)
        if preceptee_indices:
            print(f"{logger_prefix} [Fallback] 프리셉티 면제 대상: {len(preceptee_indices)}명")
    exclude_preceptee_from_den = preceptee_follow and not getattr(cfg, 'preceptee_shift_count', True)
    # 외부 스코프에서 로깅용으로 사용 (build_model 내부에서도 별도 정의)
    weekly_off_by_idx = (
        getattr(roster_system, "weekly_off_by_idx", {})
        if isinstance(getattr(roster_system, "weekly_off_by_idx", {}), dict)
        else {}
    )
    # print('이미 있음 initial_forbidden', initial_forbidden)
    # ── 폴백 사전 진단 로그(불가능 원인 빠른 파악용) ──
    try:
        # 월간 N 총 요구(고정셀로 이미 채워진 N은 제외)
        total_need_n = 0
        for d in range(D):
            if (
                hasattr(cfg, "daily_shift_requirements_by_day")
                and isinstance(cfg.daily_shift_requirements_by_day, list)
                and d < len(cfg.daily_shift_requirements_by_day)
            ):
                need_map = cfg.daily_shift_requirements_by_day[d]
                # print('1', need_map)
            else:
                need_map = cfg.daily_shift_requirements
                # print('2', need_map)
            need_n = int((need_map or {}).get("N", 0) or 0)
            # print('3', need_n)
            need_n = max(0, need_n - int(fixed_cnt[d][night_idx] or 0))
            # print('4', need_n)
            total_need_n += need_n
            # print('5', total_need_n)
        # 간호사별 N 가능 여부(허용 근무유형 기반: []=제한없음, ['N']=N전담)
        n_allowed_indices: list[int] = []
        n_only_cnt = 0
        for i, nu in enumerate(roster_system.nurses):
            raw = getattr(nu, "is_night_nurse", None)
            allowed = normalize_allowed_shift_codes(raw, use_mid=bool(getattr(cfg, "use_mid", False)))
            if not allowed:
                n_allowed_indices.append(i)
                continue
            if "N" in allowed:
                n_allowed_indices.append(i)
                if allowed == {"N"}:
                    n_only_cnt += 1

        # N 용량 상한(1) 단순: 개인 월 상한 + 재직일수(입/퇴사) 클램프
        cap_basic = 0
        cap_recovery = 0
        for n in n_allowed_indices:
            T0, T1 = join[n], leave[n]
            avail_days = max(0, int(T1 - T0 + 1))
            cap_basic += min(int(cfg.max_night_shifts_per_month), avail_days)
            # 2N→2OFF hard가 켜지면, 한 사람의 N은 대략 2일 중 1일 수준(최대 0.5 비율)로 제한되는 경향이 있다.
            # 예) avail_days=30 이면 (30+1)//2 = 15 가 상한 근사치.
            cap_recovery += min(
                int(cfg.max_night_shifts_per_month), int((avail_days + 1) // 2)
            )

        # 일별 N 요구 최대값(피크 일자 확인용)
        max_daily_need_n = 0
        for d in range(D):
            if (
                hasattr(cfg, "daily_shift_requirements_by_day")
                and isinstance(cfg.daily_shift_requirements_by_day, list)
                and d < len(cfg.daily_shift_requirements_by_day)
            ):
                need_map = cfg.daily_shift_requirements_by_day[d]
            else:
                need_map = cfg.daily_shift_requirements
            need_n = int((need_map or {}).get("N", 0) or 0)
            need_n = max(0, need_n - int(fixed_cnt[d][night_idx] or 0))
            max_daily_need_n = max(max_daily_need_n, need_n)

        print(
            f"{logger_prefix} [FallbackFeasibility] "
            f"need_N(total)={total_need_n}, need_N(daily_max)={max_daily_need_n}, "
            f"N_allowed_nurses={len(n_allowed_indices)}/{N}, N_only={n_only_cnt}, "
            f"cap_N_basic≈{cap_basic}, "
            f"cap_N_2N2OFF≈{cap_recovery if cfg.two_offs_after_two_nig else 'n/a'}, "
            f"maxN={cfg.max_night_shifts_per_month}, two_offs_after_two_nig={bool(cfg.two_offs_after_two_nig)}"
        )
        if total_need_n > cap_basic:
            print(
                f"{logger_prefix} [FallbackFeasibility][WARN] "
                f"월간 N 요구({total_need_n})가 단순 상한(cap≈{cap_basic})을 초과합니다. "
                f"→ 하드 상한을 강제하면 infeasible 가능성이 큽니다."
            )
        if bool(cfg.two_offs_after_two_nig) and total_need_n > cap_recovery:
            print(
                f"{logger_prefix} [FallbackFeasibility][WARN] "
                f"2N→2OFF 기준 상한(cap≈{cap_recovery})도 초과합니다. "
                f"→ 2N→2OFF를 hard로 두면 폴백1부터 infeasible 가능성이 큽니다."
            )
        # 핵심: 일별 피크 요구 vs N 가능 인원 비교(2N→2OFF 하드가 있으면 특정 날짜에서 N 배정 가능 인원이 급감할 수 있음)
        if bool(cfg.two_offs_after_two_nig) and max_daily_need_n > len(n_allowed_indices) * 0.5:
            print(
                f"{logger_prefix} [FallbackFeasibility][WARN] "
                f"일별 N 피크 요구({max_daily_need_n})가 N 가능 인원({len(n_allowed_indices)})의 절반 이상입니다. "
                f"→ 2N→2OFF 하드 + 다른 제약(주2OFF/연속근무K 등)과 겹치면 특정 날짜에서 N 배정 불가능할 수 있습니다."
            )
        # 월 최대 OFF 상한 vs 2N→2OFF 강제 OFF 충돌 확인
        try:
            base_min_off = effective_off_days
            extra_allowed = effective_max_extra
            # print('7', extra_allowed)
            max_off_allowed_per_person = base_min_off + extra_allowed
            # print('8', max_off_allowed_per_person)
            if bool(cfg.two_offs_after_two_nig) and max_off_allowed_per_person < base_min_off + 5:
                est_extra_off_from_2n2o = (
                    int(total_need_n / len(n_allowed_indices) * 0.5)
                    if n_allowed_indices
                    else 0
                )
                if est_extra_off_from_2n2o > extra_allowed:
                    print(
                        f"{logger_prefix} [FallbackFeasibility][WARN] "
                        f"2N→2OFF 하드가 예상 강제 OFF({est_extra_off_from_2n2o})가 월 최대 OFF 여유({extra_allowed})를 초과할 수 있습니다. "
                        f"(min_off={base_min_off}, max_allowed={max_off_allowed_per_person}) "
                        f"→ 2N→2OFF 하드 + 월 최대 OFF 상한 하드가 충돌하여 infeasible 가능성이 큽니다."
                    )
        except Exception:
            print(f"{logger_prefix} [FallbackFeasibility] 진단 로그 실패 후 pass: {e}")
            pass
    except Exception as e:
        print(f"{logger_prefix} [FallbackFeasibility] 진단 로그 실패: {e}")
    ############################################################## build model 시작 ##############################################################
    # 모델 빌더: stage에 따라 목적 및 고정 제약 선택, 안전 위반 변수 구조도 반환
    def build_model(
        stage: int,
        coverage_eq: Optional[int] = None,
        over_le: Optional[int] = None,
        stage2_zero_locks: Optional[Dict[str, list]] = None,
        relax_level: int = 0,
    ):
        m = cp_model.CpModel()
        soft_coverage = bool(getattr(cfg, "soften_daily_coverage", False))
        coverage_soft_slack = int(getattr(cfg, "coverage_soft_slack", 0) or 0)
        coverage_soft_weight = int(
            getattr(cfg, "coverage_soft_penalty_weight", 120000) or 120000
        )
        per_nurse_off_cap_override: dict[int, int] = {}
        # 강제 OFF 집합을 미리 구성 (프리체크에서 사용)
        # forced_off_cells: set[tuple[int, int]] = set()
        # forced_off_for_cap: set[tuple[int, int]] = set()
        # if off_idx is not None:
        #     forced_off_cells.update(
        #         (n_idx, d_idx)
        #         for (n_idx, d_idx), s_idx in fixed.items()
        #         if s_idx == off_idx
        #     )
        #     # cap 계산에서 휴가/공가는 제외
        #     vacation_types = {"휴가", "공가"}
        #     for (n_idx, d_idx), s_idx in fixed.items():
        #         if s_idx != off_idx:
        #             continue
        #         cell_type = fixed_type_by_cell.get((n_idx, d_idx))
        #         # print('이런 경우, cell_type', cell_type, s_idx)
        #         if cell_type in vacation_types:
        #             # print('이런 경우, vacation_types', cell_type)
        #             continue
        #         # off_exception_vacation_cells도 체크
        #         if (n_idx, d_idx) in off_exception_vacation_cells:
        #             continue
        #         forced_off_for_cap.add((n_idx, d_idx))
        # forced_off_cells.update(off_exception_cells)
        # # 휴가/공가는 상한 계산에서 제외
        # forced_off_for_cap.update(
        #     {
        #         (n_idx, d_idx)
        #         for (n_idx, d_idx) in off_exception_cells
        #         if (n_idx, d_idx) not in off_exception_vacation_cells
        #     }
        # )

        fixed_off_cells = {(n, d) for (n, d), s_idx in fixed.items() if s_idx == off_idx}
        fixed_vacation_off_cells = {
            (n, d)
            for (n, d), s_idx in fixed.items()
            if s_idx == off_idx and fixed_type_by_cell.get((n, d)) in {"휴가", "공가"}
        }
        fixed_non_off_cells = {(n, d) for (n, d), s_idx in fixed.items() if s_idx != off_idx}
        partition = build_off_partitions(
            nurses=roster_system.nurses,
            num_days=D,
            first_day=first_day,
            fixed_off_cells=fixed_off_cells,
            fixed_vacation_off_cells=fixed_vacation_off_cells,
            off_exception_cells=off_exception_cells,
            off_exception_vacation_cells=off_exception_vacation_cells,
            weekly_off_by_idx=None,
            weekend_off_only_enable=bool(cfg.weekend_off_only_enable),
            include_off_exception_cells=False,
            include_weekly_off_cells=False,
            include_weekend_off_cells=True,
            weekend_within_active_range=True,
            join=join,
            leave=leave,
            fixed_non_off_cells=fixed_non_off_cells,
        )
        structural_off_cells = set(partition["structural_off_cells"])
        vacation_off_cells = set(partition["vacation_off_cells"])
        weekend_days = set(partition["weekend_days"])

        # weekly_off_by_idx, cross-month 등
        # 👉 여기서만 structural_off_cells에 추가
        if stage == 1 and relax_level == 0:
            try:
                mapping_logs = []
                for idx, nu in enumerate(roster_system.nurses):
                    
                    mapping_logs.append(
                        f"{idx}:{getattr(nu, 'nurse_id', '?')}/"    # nurse_id가 없음
                        f"{getattr(nu, 'name', '?')}/"
                        f"{getattr(nu, 'account_id', '?')}"
                    )
                print("[NurseIndexMap] " + ", ".join(mapping_logs))
                if off_exception_cells:
                    exc_map = {}
                    for n_idx, d_idx in off_exception_cells:
                        exc_map.setdefault(n_idx, []).append(d_idx + 1)
                    exc_logs = []
                    for n_idx, days in sorted(exc_map.items()):
                        nu = roster_system.nurses[n_idx]
                        exc_logs.append(
                            f"{n_idx}:{getattr(nu, 'nurse_id', '?')}/"
                            f"{getattr(nu, 'name', '?')}/"
                            f"{getattr(nu, 'account_id', '?')} -> {sorted(days)}"
                        )
                    print("[OffExceptionCells] " + "; ".join(exc_logs))
            except Exception:
                pass
        Xv = {}

        def X(n, d, s):
            return Xv.get((n, d, s), 0)

        def is_pure_o(n: int, d: int):
            """휴가/공가(예외 휴무) 좌표는 제외한 순수 O만 반환합니다."""
            if (n, d) in vac_cells:
                return 0
            return X(n, d, off_idx)

        # ── 하드 모순 사전 점검: 커버리지 cap / 강제 OFF 상한 ──
        try:
            # (1) 교대별 최대 가능 인원(cap) 대비 need 초과 여부
            if hasattr(cfg, "daily_shift_requirements") and cfg.daily_shift_requirements:
                forbidden = initial_forbidden if initial_forbidden else {}
                shift_allow_map = getattr(roster_system, "shift_codes_by_nurse", None)
                # print('hahaha, shift_allow_map', shift_allow_map)       # None
                for d in range(D):
                    if (
                        hasattr(cfg, "daily_shift_requirements_by_day")
                        and isinstance(cfg.daily_shift_requirements_by_day, list)
                        and d < len(cfg.daily_shift_requirements_by_day)
                    ):
                        need_map = cfg.daily_shift_requirements_by_day[d]
                    else:
                        need_map = cfg.daily_shift_requirements
                    for code, req in (need_map or {}).items():
                        if code not in roster_system.config.shift_types:
                            continue
                        s_idx = roster_system.config.shift_types.index(code)
                        need = int(req) - fixed_cnt[d][s_idx]
                        if need <= 0:
                            continue
                        cap = 0
                        blocked = {
                            "forced_off": 0,
                            "weekend_off_only": 0,
                            "forbidden": 0,
                            "not_allowed": 0,
                        }
                        for n in range(N):
                            if not (join[n] <= d <= leave[n]):
                                continue
                            if (n, d) in fixed:
                                # print('고정:', fixed[(n, d)])
                                continue  # 다른 교대로 이미 고정
                            if (n, d) in structural_off_cells:
                                blocked["forced_off"] += 1
                                continue
                            if (
                                d in weekend_days
                                and getattr(cfg, "weekend_off_only_enable", True)
                                and bool(getattr(roster_system.nurses[n], "is_weekend_off", False))
                                and s_idx != off_idx
                            ):
                                blocked["weekend_off_only"] += 1
                                # print('weekend_off_only', roster_system.nurses[n] )
                                continue
                            if (n, d) in forbidden:
                                forbid_codes = [
                                    c
                                    for c in forbidden[(n, d)]
                                    if c in roster_system.config.shift_types
                                ]
                                forbid_idx = {
                                    roster_system.config.shift_types.index(c) for c in forbid_codes
                                }
                                if s_idx in forbid_idx:
                                    blocked["forbidden"] += 1
                                    continue
                            if shift_allow_map and isinstance(shift_allow_map, dict):
                                allowed_codes = shift_allow_map.get(n, None)
                                if allowed_codes and roster_system.config.shift_types[s_idx] not in allowed_codes:
                                    blocked["not_allowed"] += 1
                                    continue
                            cap += 1
                        if need > cap:
                            print(
                                f"[HardCheck] day={d+1}, shift={code}, need={need}, cap={cap}, blocked={blocked}"
                            )

            # (2) 강제/고정 OFF로 개인 OFF 상한 초과 여부
            try:
                for n in range(N):
                    T0, T1 = join[n], leave[n]
                    avail_days = T1 - T0 + 1
                    forced_off_cnt = sum(
                        1
                        for d in range(T0, T1 + 1)
                        if (n, d) in structural_off_cells
                    )
                    nu = roster_system.nurses[n]
                    raw = getattr(nu, "is_night_nurse", None)
                    is_n_only = is_n_only_profile(raw, use_mid=bool(getattr(cfg, "use_mid", False)))
                    # 디버그: 강제 OFF 개수 로그
                    print(
                        f"{logger_prefix} [HardCheck][ForcedOffCnt] "
                        f"nurse_idx={n}, id={getattr(nu, 'nurse_id', '?')}, "
                        f"name={getattr(nu, 'name', '?')}, forced_off_cnt={forced_off_cnt}, "
                        f"avail_days={avail_days}"
                    )
                    if is_n_only:
                        max_off_allowed = max(0, avail_days - 15) + relax_level
                        # print(f'is_n_only, 간호사 n: {n}, max_off_allowed: {max_off_allowed}')
                    else:
                        vacation_cnt = sum(
                            1 for d in range(T0, T1 + 1) if (n, d) in vacation_off_cells
                        )
                        weekend_slots_nonvac = sum(
                            1
                            for d in weekend_days
                            if T0 <= d <= T1 and (n, d) not in vacation_off_cells
                        )
                        off_bounds = compute_off_bounds(
                            source=cfg,
                            avail_days=avail_days,
                            vacation_cnt=vacation_cnt,
                            reference_days=D_phys,
                            weekend_only=bool(getattr(nu, "is_weekend_off", False)),
                            weekend_slots_nonvac=weekend_slots_nonvac,
                        )
                        max_off_allowed = int(off_bounds["max_off_allowed"]) + relax_level
                        # print(f'not is_n_only, 간호사 n: {n}, max_off_allowed: {max_off_allowed}')
                    if forced_off_cnt > max_off_allowed:
                        # print(f'forced_off_cnt > max_off_allowed, 간호사 n: {n}, forced_off_cnt: {forced_off_cnt}, max_off_allowed: {max_off_allowed}')
                        per_nurse_off_cap_override[n] = forced_off_cnt
                        forced_days = [
                            d_idx + 1
                            for d_idx in range(T0, T1 + 1)
                            if (n, d_idx) in structural_off_cells
                        ]
                        nurse_id = getattr(nu, "nurse_id", "?")
                        nurse_name = getattr(nu, "name", "?")
                        account_id = getattr(nu, "account_id", "?")
                        print(
                            "[HardCheck] "
                            f"nurse_idx={n}, nurse_id={nurse_id}, name={nurse_name}, "
                            f"account_id={account_id}, forced_off={forced_off_cnt}, "
                            f"max_off_allowed={max_off_allowed}, forced_off_days={forced_days} "
                            "→ OFF 상한 초과(모순 가능)"
                        )
            except Exception:
                print(f"{logger_prefix} [HardCheck] 강제 OFF 상한 초과 여부 실패: {e}")
                pass
        except Exception as exc:
            print(f"{logger_prefix} [HardCheck] precheck 실패: {exc}")

        for n in range(N):
            for d in range(join[n], leave[n] + 1):
                for s in range(S):
                    Xv[n, d, s] = m.NewBoolVar(f"x_{n}_{d}_{s}")
        active_days = {(n, d) for n in range(N) for d in range(join[n], leave[n] + 1)}
        # 고정 셀
        for (n, d), s_idx in fixed.items():
            if (n, d) not in active_days:
                continue
            if preceptee_follow and n in preceptee_indices:
                continue
            m.Add(X(n, d, s_idx) == 1)
            for s in range(S):
                if s != s_idx:
                    m.Add(X(n, d, s) == 0)
        # W(특별 근무)는 고정 셀 외에는 전부 금지
        if has_w and w_idx is not None:
            for n in range(N):
                if preceptee_follow and n in preceptee_indices:
                    continue
                for d in range(join[n], leave[n] + 1):
                    if (n, d) in fixed and fixed[(n, d)] == w_idx:
                        continue
                    m.Add(X(n, d, w_idx) == 0)
        # 순수 O 4연속 금지 (fixed로 이미 4O면 경고만 남기고 스킵)
        # cfg.skip_4o_hard_first_days: 월초 N일 구간에서는 4O Hard 미적용 (기본 3)
        if off_idx is not None:
            vac_cells = set(off_exception_vacation_cells)
            skip_4o_hard_first_days = int(getattr(cfg, "skip_4o_hard_first_days", 3) or 0)
            for n in range(N):
                if preceptee_follow and n in preceptee_indices:
                    continue
                for d in range(join[n], leave[n] - 2):
                    if d + 3 > leave[n]:
                        continue
                    # if skip_4o_hard_first_days > 0 and d < skip_4o_hard_first_days:
                    #     continue
                    fixed_o_cnt = sum(
                        1
                        for (fn, fd), fs_idx in fixed.items()
                        if fn == n
                        and fd in {d, d + 1, d + 2, d + 3}
                        and fs_idx == off_idx
                        and (fn, fd) not in vac_cells
                    )
                    # print('fixed.items()', fixed.items())
                    # print('이미 있음 fixed_o_cnt', fixed_o_cnt)
                    if fixed_o_cnt >= 4:
                        print(
                            f"{logger_prefix} [4O-skip-fixed] nurse_idx={n}, days={d+1},{d+2},{d+3},{d+4} (fixed O x{fixed_o_cnt})"
                        )
                        continue
                    m.Add(
                        is_pure_o(n, d)
                        + is_pure_o(n, d + 1)
                        + is_pure_o(n, d + 2)
                        + is_pure_o(n, d + 3)
                        <= 3
                    )
        # 주말 휴무 제약: is_weekend_off=True인 간호사는 주말(토/일)은 기본적으로 OFF를 강제하고,
        # 평일(월~금)에는 OFF를 금지한다.
        #
        # 예외:
        # - 특정 날짜가 '고정 셀(fixed_cells)'로 이미 근무(D/E/N/W 등)로 지정된 경우,
        #   기존 고정이 우선이며 주말 OFF 강제를 덮어쓰지 않는다.
        if getattr(cfg, "weekend_off_only_enable", True):
            if stage == 1:
                _log_weekend_off_enforcement(
                    roster_system=roster_system,
                    join=join,
                    leave=leave,
                    weekend_days=weekend_days,
                    fixed=fixed,
                    off_idx=off_idx,
                    logger_prefix=logger_prefix,
                )
            for n, nu in enumerate(roster_system.nurses):
                if preceptee_follow and n in preceptee_indices:
                    continue
                if not bool(getattr(nu, "is_weekend_off", False)):
                    continue
                for d in range(join[n], leave[n] + 1):
                    if d in weekend_days:
                        # 주말(토/일): 기본 OFF 강제
                        # 단, 고정 셀이 근무로 지정되어 있으면(예: 특수 근무/교육 등) 고정이 우선이다.
                        if (n, d) in fixed and fixed[(n, d)] != off_idx:
                            try:
                                fixed_code = cfg.shift_types[fixed[(n, d)]]
                            except Exception:
                                fixed_code = str(fixed.get((n, d)))
                            print(
                                f"{logger_prefix} [WeekendOff] 주말 OFF 강제 스킵(고정 우선): "
                                f"nurse_index={n}, day={d+1}, fixed_shift={fixed_code}"
                            )
                            continue
                        m.Add(X(n, d, off_idx) == 1)
                    else:
                        # 평일(월~금): OFF 금지(D/E/N만 가능)
                        # 단, 사용자 고정 OFF는 예외로 허용하고 별도 제약을 걸지 않는다.
                        if (n, d) in fixed and fixed[(n, d)] == off_idx:
                            continue
                        if d <= 2 and getattr(roster_system, "prev_month_n_tail_by_idx", {}).get(n, 0) > 0:
                            continue
                        m.Add(X(n, d, off_idx) == 0)

        # raw_off_placement_mode = int(getattr(cfg, "off_placement_mode", 0) or 0)
        # if raw_off_placement_mode != 0:
        #     print(f"{logger_prefix} [OffPlacementMode] deprecated: forcing off_placement_mode=0")
        # off_placement_mode = 0
        weekly_off_by_idx = (
            getattr(roster_system, "weekly_off_by_idx", {})
            if isinstance(getattr(roster_system, "weekly_off_by_idx", {}), dict)
            else {}
        )
        prev_month_last_is_off = (
            getattr(roster_system, "prev_month_last_is_off", {})
            if isinstance(getattr(roster_system, "prev_month_last_is_off", {}), dict)
            else {}
        )
        prev_month_n_tail_by_idx = (
            getattr(roster_system, "prev_month_n_tail_by_idx", {})
            if isinstance(getattr(roster_system, "prev_month_n_tail_by_idx", {}), dict)
            else {}
        )
        # print('hahaha, weekly_off_by_idx', weekly_off_by_idx)
        # print('hahaha, prev_month_last_is_off', prev_month_last_is_off)
        # forced_off_cells: set[tuple[int, int]] = set(
        #     (n_idx, d_idx) for (n_idx, d_idx), s_idx in fixed.items() if s_idx == off_idx
        # )
        # forced_off_cells.update(off_exception_cells)
        # 예상 커버리지 부족일 계산(단순 근사): 필요한 총 인원 > (활성 인원 - 고정 OFF)
        shortage_days: set[int] = set()
        try:
            for d in range(D):
                if (
                    hasattr(cfg, "daily_shift_requirements_by_day")
                    and isinstance(cfg.daily_shift_requirements_by_day, list)
                    and d < len(cfg.daily_shift_requirements_by_day)
                ):
                    need_map = cfg.daily_shift_requirements_by_day[d]
                else:
                    need_map = cfg.daily_shift_requirements
                total_need = sum(int(v) for v in (need_map or {}).values())
                active_cnt = sum(1 for n in range(N) if join[n] <= d <= leave[n])
                fixed_off_cnt = fixed_cnt[d][off_idx] if off_idx is not None else 0
                avail_eff = max(0, active_cnt - fixed_off_cnt)
                if avail_eff < total_need:
                    shortage_days.add(d)
        except Exception:
            shortage_days = set()
        # if off_placement_mode > 0 and weekly_off_by_idx:
        #     for n, day_list in weekly_off_by_idx.items():
        #         if n >= len(join):
        #             continue
        #         if preceptee_follow and n in preceptee_indices:
        #             continue
        #         T0, T1 = join[n], leave[n]
        #         for d_raw in day_list or []:
        #             try:
        #                 d = int(d_raw)
        #             except Exception:
        #                 continue
        #             if d < T0 or d > T1:
        #                 continue
        #             if d == D - 1:
        #                 continue
        #             if d == 0:
        #                 if bool(prev_month_last_is_off.get(n, False)):
        #                     continue
        #                 if d + 1 <= T1:
        #                     m.Add(X(n, d + 1, off_idx) == 1)
        #                     structural_off_cells.add((n, d + 1))
        #                 continue
        #             if off_placement_mode == 1:
        #                 neighbours = []
        #                 left_pos = d - 1
        #                 right_pos = d + 1
        #                 # if left_pos >= T0 and left_pos not in shortage_days:
        #                 allow_shortage_off = relax_level >= 3

        #                 if left_pos >= T0 and (allow_shortage_off or left_pos not in shortage_days):
        #                     neighbours.append(("left", X(n, left_pos, off_idx)))
        #                 # if right_pos <= T1 and right_pos not in shortage_days:
        #                 if right_pos <= T1 and (allow_shortage_off or right_pos not in shortage_days):
        #                     neighbours.append(("right", X(n, right_pos, off_idx)))
        #                 # 둘 다 부족일이면 스킵
        #                 if not neighbours:
        #                     continue
        #                 vars_only = [v for _, v in neighbours]
        #                 if len(vars_only) == 1:
        #                     m.Add(vars_only[0] == 1)
        #                 else:
        #                     m.Add(sum(vars_only) >= 1)
        #                 for direction, _var in neighbours:
        #                     if direction == "left":
        #                         structural_off_cells.add((n, left_pos))
        #                     else:
        #                         structural_off_cells.add((n, right_pos))
        #             else:
        #                 left_pos = d - 1
        #                 right_pos = d + 1
        #                 placed = False
        #                 if left_pos >= T0 and left_pos not in shortage_days:
        #                     m.Add(X(n, left_pos, off_idx) == 1)
        #                     structural_off_cells.add((n, left_pos))
        #                     placed = True
        #                 elif right_pos <= T1 and right_pos not in shortage_days:
        #                     m.Add(X(n, right_pos, off_idx) == 1)
        #                     structural_off_cells.add((n, right_pos))
        #                     placed = True
        #                 # 둘 다 부족일이면 스킵 (커버리지 우선)
        #                 if not placed:
        #                     continue

        # 초기 금지: 고정과 충돌하면 금지 무시(로그만)
        try:
            if initial_forbidden:
                for (n, d), code_list in initial_forbidden.items():
                    if preceptee_follow and n in preceptee_indices:
                        continue
                    for code in (code_list or []):
                        if code not in roster_system.config.shift_types:
                            continue
                        s_idx = roster_system.config.shift_types.index(code)
                        if (n, d) not in active_days:
                            continue
                        if (n, d) in fixed:
                            # 유저 고정 셀 우선: 해당 날 전체 금지 무시
                            continue
                        m.Add(X(n, d, s_idx) == 0)
        except Exception as e:
            print(f"{logger_prefix} 초기 금지 셀 적용 중 오류: {e}")

        # exactly-one
        for n in range(N):
            for d in range(join[n], leave[n] + 1):
                if (n, d) in fixed and not (preceptee_follow and n in preceptee_indices):
                    continue
                m.AddExactlyOne(X(n, d, s) for s in range(S))

        # 프리셉티 팔로우 제약 (fallback)
        if preceptee_follow and preceptee_indices:
            _fb_id_map = {nu.db_id: n for n, nu in enumerate(roster_system.nurses)}
            for n in sorted(preceptee_indices):
                nu = roster_system.nurses[n]
                pid = getattr(nu, 'preceptor_id', None)
                if not pid or pid not in _fb_id_map:
                    continue
                p = _fb_id_map[pid]
                d_start = max(join[n], join[p])
                d_end = min(leave[n], leave[p])
                for d in range(d_start, d_end + 1):
                    for s in range(S):
                        xn = X(n, d, s)
                        xp = X(p, d, s)
                        if isinstance(xn, int) or isinstance(xp, int):
                            continue
                        m.Add(xn == xp)

        # DEN 커버리지에서 프리셉티 제외 시 fixed_cnt 보정
        if exclude_preceptee_from_den:
            _fb_fixed_cnt_adj = [[0] * S for _ in range(D)]
            for (n2, d2), s_idx in fixed.items():
                if n2 not in preceptee_indices:
                    _fb_fixed_cnt_adj[d2][s_idx] += 1
        else:
            _fb_fixed_cnt_adj = fixed_cnt

        m_bucket_indices = compute_main_bucket_indices(
            roster_system.config.shift_types,
            target_main="M",
            code2main=code2main,
            shift_id_to_main_map=shift_id_to_main_map,
        )

        # 1) 커버리지 등식: assigned + short - over == need (날짜별 요구치 적용)
        short_terms, over_terms = [], []
        over_vars_by_day = {}
        short_vars_by_day_code: Dict[tuple[int, str], cp_model.IntVar] = {}
        over_vars_by_day_code: Dict[tuple[int, str], cp_model.IntVar] = {}
        zero_demand_block_codes = {"D", "E", "N", "M"}
        for d in range(D):
            if (
                hasattr(cfg, "daily_shift_requirements_by_day")
                and isinstance(cfg.daily_shift_requirements_by_day, list)
                and d < len(cfg.daily_shift_requirements_by_day)
            ):
                need_map = cfg.daily_shift_requirements_by_day[d]
            else:
                need_map = cfg.daily_shift_requirements
            for code, req in need_map.items():
                if code not in roster_system.config.shift_types:
                    continue
                s = roster_system.config.shift_types.index(code)
                req_raw = max(0, int(req or 0))
                need = req_raw - _fb_fixed_cnt_adj[d][s]
                assigned = sum(
                    X(n, d, s)
                    for n in range(N)
                    if join[n] <= d <= leave[n] and (n, d) not in fixed
                    and (not exclude_preceptee_from_den or n not in preceptee_indices)
                )
                if code == "M":
                    if m_bucket_indices:
                        assigned_m_bucket = sum(
                            X(n, d, s2)
                            for n in range(N)
                            if join[n] <= d <= leave[n]
                            and (n, d) not in fixed
                            and (not exclude_preceptee_from_den or n not in preceptee_indices)
                            for s2 in m_bucket_indices
                        )
                    else:
                        assigned_m_bucket = assigned
                    fixed_m_bucket = (
                        sum(int(_fb_fixed_cnt_adj[d][s2] or 0) for s2 in m_bucket_indices)
                        if m_bucket_indices
                        else int(_fb_fixed_cnt_adj[d][s] or 0)
                    )
                    if req_raw == 0:
                        m.Add(assigned_m_bucket == 0)
                        sh = m.NewIntVar(0, 0, f"short_{d}_{code}")
                        ov = m.NewIntVar(0, 0, f"over_{d}_{code}")
                        short_terms.append(sh)
                        over_terms.append(ov)
                        over_vars_by_day.setdefault(d, {})[code] = ov
                        short_vars_by_day_code[(d, code)] = sh
                        over_vars_by_day_code[(d, code)] = ov
                        continue
                    m_cap_non_fixed = max(0, int(req_raw - fixed_m_bucket))
                    m.Add(assigned_m_bucket <= m_cap_non_fixed)
                    sh = m.NewIntVar(0, m_cap_non_fixed, f"short_{d}_{code}")
                    m.Add(assigned_m_bucket + sh >= m_cap_non_fixed)
                    ov = m.NewIntVar(0, 0, f"over_{d}_{code}")
                    short_terms.append(sh)
                    over_terms.append(ov)
                    over_vars_by_day.setdefault(d, {})[code] = ov
                    short_vars_by_day_code[(d, code)] = sh
                    over_vars_by_day_code[(d, code)] = ov
                    continue
                if code in zero_demand_block_codes and req_raw == 0:
                    m.Add(assigned == 0)
                    sh = m.NewIntVar(0, 0, f"short_{d}_{code}")
                    ov = m.NewIntVar(0, 0, f"over_{d}_{code}")
                    short_terms.append(sh)
                    over_terms.append(ov)
                    over_vars_by_day.setdefault(d, {})[code] = ov
                    short_vars_by_day_code[(d, code)] = sh
                    over_vars_by_day_code[(d, code)] = ov
                    continue
                if need <= 0:
                    continue
                sh = m.NewIntVar(0, N, f"short_{d}_{code}")
                ov = m.NewIntVar(0, N, f"over_{d}_{code}")
                # Coverage 우선: assigned + shortage >= need (hard), oversupply 추적은 선택
                m.Add(assigned + sh >= need)
                m.Add(assigned - ov <= need)
                short_terms.append(sh)
                over_terms.append(ov)
                over_vars_by_day.setdefault(d, {})[code] = ov
                short_vars_by_day_code[(d, code)] = sh
                over_vars_by_day_code[(d, code)] = ov

        # 2) 안전/법규 위반(정량 슬랙) 구성
        safety = {
            "trans_nd": [],  # N→D 위반 (Bool)
            "trans_ed": [],  # E→D 위반 (Bool)
            "trans_ne": [],  # N→E 위반 (Bool)
            "cwork_missing": [],  # 연속근무 창에서 필요한 OFF 부족량(Int)
            "cnight_excess": [],  # 연속 N 초과(Int)
            "mnight_excess": [],  # 월간 N 초과(Int)
            "night_only_de": [],  # 야간전담의 D/E 배정 위반(Bool/Int)
            "week_off_missing": [],  # 주별 2OFF 부족(Int)
            "rec_3n2o": [],  # N3→2O 회복 부족(Int)
            "rec_2n2o": [],  # N2→2O 회복 부족(Int)
            "pattern_nod": [],  # N-O-D 패턴(Int)
            "pattern_noe": [],  # N-O-E 패턴(Int)
            "pattern_eod": [],  # E-O-D 패턴(Int)
            "min_off_missing": [],  # 월 최소 OFF 부족(Int)
            "off_quota_short": [],  # 개인별 O 할당(주휴 제외) 부족 슬랙(Int)
            "off_quota_excess": [],  # 개인별 O 초과 슬랙(Int)
            "off_cap_bounded_slack": [],
            "isolated_off_slack": [],  # 고립 OFF 허용 슬랙(가중치 포함)
        }
        off_quota_short_by_n: dict[int, cp_model.IntVar] = {}
        off_quota_excess_by_n: dict[int, cp_model.IntVar] = {}
        min_off_miss_by_n: dict[int, cp_model.IntVar] = {}
        target_o_by_n: dict[int, int] = {}
        off_cap_bounded_slack_enable = bool(
            getattr(cfg, "fallback_off_cap_bounded_slack_enable", False)
        )
        off_cap_bounded_slack_max = max(
            0,
            int(getattr(cfg, "fallback_off_cap_bounded_slack_max", 1) or 0),
        )
        off_cap_bounded_slack_weight = max(
            1,
            int(getattr(cfg, "fallback_off_cap_bounded_slack_weight", 10) or 1),
        )

        # 고립 OFF 금지(슬랙 허용): sequential_offs 활성 + 옵션 켜졌을 때만 적용
        if (
            bool(getattr(cfg, "sequential_offs", True))
        ):
            slack_penalty = int(getattr(cfg, "isolated_off_slack_penalty", 300000) or 0)
            for n in range(N):
                if preceptee_follow and n in preceptee_indices:
                    continue
                t0, t1 = join[n], leave[n]
                for d in range(t0, t1 + 1):
                    neighbours = []
                    if d - 1 >= t0:
                        neighbours.append(X(n, d - 1, off_idx))
                    if d + 1 <= t1:
                        neighbours.append(X(n, d + 1, off_idx))
                    slack = m.NewBoolVar(f"iso_off_slack_{n}_{d}")
                    if neighbours:
                        m.Add(X(n, d, off_idx) <= sum(neighbours) + slack)
                    else:
                        m.Add(X(n, d, off_idx) <= slack)
                    if slack_penalty > 0:
                        scaled = m.NewIntVar(0, slack_penalty, f"iso_off_cost_{n}_{d}")
                        m.Add(scaled == slack * slack_penalty)
                        safety["isolated_off_slack"].append(scaled)
                    else:
                        safety["isolated_off_slack"].append(slack)

        # 전이 위반: 정확한 reification (iff)
        for n in range(N):
            if preceptee_follow and n in preceptee_indices:
                continue
            T0, T1 = join[n], leave[n]
            for d in range(T0 + 1, T1 + 1):
                xn = X(n, d - 1, night_idx)
                xd = X(n, d, day_idx)
                if getattr(cfg, "ban_n_to_d", True):
                    # fixed_cells로 N→D가 명시적으로 고정된 경우 제약 면제
                    if not (fixed.get((n, d-1)) == night_idx and fixed.get((n, d)) == day_idx):
                        m.Add(xn + xd <= 1)
                if getattr(cfg, "ban_e_to_d", True):
                    xe = X(n, d - 1, eve_idx)
                    # fixed_cells로 E→D가 명시적으로 고정된 경우 제약 면제
                    if not (fixed.get((n, d-1)) == eve_idx and fixed.get((n, d)) == day_idx):
                        m.Add(xe + xd <= 1)
                if getattr(cfg, "ban_n_to_e", True):
                    xe2 = X(n, d, eve_idx)
                    # fixed_cells로 N→E가 명시적으로 고정된 경우 제약 면제
                    if not (fixed.get((n, d-1)) == night_idx and fixed.get((n, d)) == eve_idx):
                        m.Add(xn + xe2 <= 1)
                if mid_idx is not None:
                    m.Add(X(n, d, mid_idx) <= X(n, d - 1, day_idx) + X(n, d - 1, off_idx))
                # if getattr(cfg, "ban_d_to_n", True):
                #     xd_prev = X(n, d - 1, day_idx)
                #     m.Add(xd_prev + xn <= 1)

        # 1N 금지 (day0 N 고정인 경우 해당일만 스킵)
        not_one_night_val = getattr(cfg, "not_one_night", False)
        print(f"{logger_prefix} [1N금지] not_one_night={not_one_night_val!r} (type={type(not_one_night_val).__name__})")
        if bool(not_one_night_val):
            for n in range(N):
                if preceptee_follow and n in preceptee_indices:
                    continue
                T0, T1 = join[n], leave[n]
                for d in range(T0, T1 + 1):
                    if d == 0 and (n, 0) in fixed and fixed[(n, 0)] == night_idx:
                        continue
                    if d == 0 and prev_month_n_tail_by_idx.get(n, 0) > 0:
                        continue
                    neighbors = []
                    if d - 1 >= T0:
                        neighbors.append(X(n, d - 1, night_idx))
                    if d + 1 <= T1:
                        neighbors.append(X(n, d + 1, night_idx))
                    if not neighbors:
                        continue
                    m.Add(X(n, d, night_idx) <= sum(neighbors))

        # # 주말 휴무자 N 요일 제한: 2N 2O 켜진 경우 목금만 N 허용 (2O가 주말에 자연 달성)
        # if bool(getattr(cfg, "two_offs_after_two_nig", False)):
        #     allowed_wd = {3, 4}  # 목금 (weekday: Mon=0 .. Fri=4)
        #     for n in range(N):
        #         nu = roster_system.nurses[n]
        #         if not bool(getattr(nu, "is_weekend_off", False)):
        #             continue
        #         T0, T1 = join[n], leave[n]
        #         for d in range(T0, T1 + 1):
        #             if (n, d) in fixed and fixed[(n, d)] == night_idx:
        #                 continue
        #             wd = (first_day + timedelta(days=d)).weekday()
        #             if wd not in allowed_wd:
        #                 m.Add(X(n, d, night_idx) == 0)

        # 월초 OFF 윈도우 (전월 꼬리 연속근무 보정): 지정 구간에 OFF ≥ 1
        try:
            off_windows = getattr(roster_system, "off_window_constraints", {}) or {}
            if off_idx is not None:
                for n in range(N):
                    if preceptee_follow and n in preceptee_indices:
                        continue
                    nu = roster_system.nurses[n]
                    if bool(getattr(nu, "is_weekend_off", False)):
                        continue
                    T0, T1 = join[n], leave[n]
                    for (w_start, w_end) in off_windows.get(n, []) or []:
                        left = max(T0, w_start)
                        right = min(T1, w_end)
                        if left > right:
                            continue
                        # 유저 고정 우선: 윈도우 내 고정 비-OFF 셀은 제외하고 적용
                        free_days_w = [d for d in range(left, right + 1) if not ((n, d) in fixed and fixed[(n, d)] != off_idx)]
                        if not free_days_w:
                            print(f"{logger_prefix} off_window 무시 (유저 고정 우선, fallback): n={n}, window=[{left+1},{right+1}] 전체 고정")
                            continue
                        m.Add(sum(X(n, d, off_idx) for d in free_days_w) >= 1)
        except Exception as e:
            print(f"{logger_prefix} 월초 OFF 윈도우 적용 실패(fallback): err={e}")

        # 연속 근무 K+1 창에서 최소 1 OFF 필요 → 하드 제약 (주말 휴무자는 제외: 매 주말 OFF로 이미 휴식 보장)
        # 고정 셀 우선: D/E/N/O 불문하고 fixed인 날은 자유 일수에서 제외
        K = cfg.max_consecutive_work_days
        for n in range(N):
            if preceptee_follow and n in preceptee_indices:
                continue
            if bool(getattr(roster_system.nurses[n], "is_weekend_off", False)):
                continue
            T0, T1 = join[n], leave[n]
            for d0 in range(T0, T1 - K + 1):
                window = [d0 + t for t in range(K + 1)]
                # 고정 OFF가 하나라도 있으면 이미 만족 → 스킵
                if any((n, d) in fixed and fixed[(n, d)] == off_idx for d in window):
                    continue
                # 고정 셀(근무/OFF 불문)을 제외한 자유 일수만 합산
                free_days_w = [d for d in window if (n, d) not in fixed]
                if not free_days_w:
                    continue
                m.Add(sum(X(n, d, off_idx) for d in free_days_w) >= 1)

        # 연속 Night 상한 L → 초과량 정량화
        L = cfg.max_consecutive_nights
        for n in range(N):
            if preceptee_follow and n in preceptee_indices:
                continue
            T0, T1 = join[n], leave[n]
            n_tail = prev_month_n_tail_by_idx.get(n, 0)
            if n_tail > 0:
                for w in range(1, n_tail + 1):
                    april_window_end = L - w
                    cap = L - w
                    if april_window_end < 0 or cap < 0:
                        continue
                    days_in_window = list(range(T0, min(T0 + april_window_end + 1, T1 + 1)))
                    if days_in_window:
                        m.Add(sum(X(n, d, night_idx) for d in days_in_window) <= cap)
            for d0 in range(T0, T1 - L + 1):
                sum_n = sum(X(n, d0 + t, night_idx) for t in range(L + 1))
                exc = m.NewIntVar(0, L + 1, f"cnight_exc_{n}_{d0}")
                m.Add(exc >= sum_n - L)
                # 연속 N 상한 L 하드: three_seq_nig False면 L=2(3N 금지), True면 L=3(3N 허용)
                m.Add(sum_n <= L)
                safety["cnight_excess"].append(exc)

        # 월 Night 상한 초과량
        for n in range(N):
            if preceptee_follow and n in preceptee_indices:
                continue
            T0, T1 = join[n], leave[n]
            sum_m = sum(X(n, d, night_idx) for d in range(T0, T1 + 1))
            m.Add(sum_m <= cfg.max_night_shifts_per_month)

        # N 전담: D/E 하드 금지 (메인 모델과 동일)
        for n, nu in enumerate(roster_system.nurses):
            if preceptee_follow and n in preceptee_indices:
                continue
            raw = getattr(nu, "is_night_nurse", None)
            allowed = normalize_allowed_shift_codes(raw, use_mid=bool(getattr(cfg, "use_mid", False)))
            if not allowed:
                continue
            T0, T1 = join[n], leave[n]
            for d in range(T0, T1 + 1):
                if "D" not in allowed:
                    m.Add(X(n, d, day_idx) == 0)
                if "E" not in allowed:
                    m.Add(X(n, d, eve_idx) == 0)
                if "N" not in allowed:
                    m.Add(X(n, d, night_idx) == 0)
                if mid_idx is not None and "M" not in allowed:
                    m.Add(X(n, d, mid_idx) == 0)

        # 야간전담의 D/E 금지 위반(OR: D or E) — N전담은 하드로 처리하므로 소프트 미사용
        # for n, nu in enumerate(roster_system.nurses):
        #     if nu.is_night_nurse != 0:
        #         continue
        #     T0, T1 = join[n], leave[n]
        #     for d in range(T0, T1 + 1):
        #         v = m.NewIntVar(0, 1, f"nonly_de_{n}_{d}")
        #         m.Add(v >= X(n, d, day_idx))
        #         m.Add(v >= X(n, d, eve_idx))
        #         m.Add(v <= X(n, d, day_idx) + X(n, d, eve_idx))
        #         safety["night_only_de"].append(v)

        # 주별 2OFF 부족량
        if cfg.enforce_two_offs_per_week:
            weeks = D // 7
            for n in range(N):
                if preceptee_follow and n in preceptee_indices:
                    continue
                for w in range(weeks):
                    d0, d1 = w * 7, min(w * 7 + 7, D)
                    offs = sum(X(n, d, off_idx) for d in range(d0, d1) if join[n] <= d <= leave[n])
                    miss = m.NewIntVar(0, 2, f"week_miss_{n}_{w}")
                    m.Add(miss >= 2 - offs)
                    safety["week_off_missing"].append(miss)

        # 회복 규칙: N3→2O, N2→2O 부족량
        if cfg.two_offs_after_three_nig:
            for n in range(N):
                if preceptee_follow and n in preceptee_indices:
                    continue
                T0, T1 = join[n], leave[n]
                n_tail = prev_month_n_tail_by_idx.get(n, 0)
                if n_tail >= 2 and (T0 + 2) <= T1:
                    m.Add(
                        X(n, T0 + 1, off_idx) + X(n, T0 + 2, off_idx) == 2
                    ).OnlyEnforceIf([X(n, T0, night_idx)])
                if n_tail == 1 and (T0 + 3) <= T1:
                    m.Add(
                        X(n, T0 + 2, off_idx) + X(n, T0 + 3, off_idx) == 2
                    ).OnlyEnforceIf([X(n, T0, night_idx), X(n, T0 + 1, night_idx)])
                for d in range(T0 + 2, T1 - 1):
                    xn0 = X(n, d, night_idx)
                    xn1 = X(n, d - 1, night_idx)
                    xn2 = X(n, d - 2, night_idx)
                    need = X(n, d + 1, off_idx) + X(n, d + 2, off_idx)
                    miss = m.NewIntVar(0, 2, f"rec3n2o_{n}_{d}")
                    m.Add(miss == 0).OnlyEnforceIf(xn0.Not())
                    m.Add(miss == 0).OnlyEnforceIf(xn1.Not())
                    m.Add(miss == 0).OnlyEnforceIf(xn2.Not())
                    m.Add(miss == 2 - need).OnlyEnforceIf([xn0, xn1, xn2])
                    safety["rec_3n2o"].append(miss)
        if cfg.two_offs_after_two_nig:
            for n in range(N):
                if preceptee_follow and n in preceptee_indices:
                    continue
                T0, T1 = join[n], leave[n]
                n_tail = prev_month_n_tail_by_idx.get(n, 0)
                if n_tail >= 1 and (T0 + 2) <= T1:
                    end_block_b0 = m.NewBoolVar(f"end_2n_soft_b0_{n}")
                    m.Add(end_block_b0 == X(n, T0 + 1, night_idx).Not())
                    m.Add(
                        X(n, T0 + 1, off_idx) + X(n, T0 + 2, off_idx) == 2
                    ).OnlyEnforceIf([X(n, T0, night_idx), end_block_b0])
                for d in range(T0 + 1, T1 - 1):
                    xn_prev = X(n, d - 1, night_idx)
                    xn_curr = X(n, d, night_idx)
                    xn_next = X(n, d + 1, night_idx)
                    end_block = m.NewBoolVar(f"end_2n_soft_{n}_{d}")
                    m.Add(end_block == xn_next.Not())
                    m.Add(
                        X(n, d + 1, off_idx) + X(n, d + 2, off_idx) == 2
                    ).OnlyEnforceIf([xn_prev, xn_curr, end_block])

        # 금지 패턴 N-O-D/E
        if getattr(cfg, "nod_noe", True):
            for n in range(N):
                if preceptee_follow and n in preceptee_indices:
                    continue
                T0, T1 = join[n], leave[n]
                for d in range(T0, T1 - 2):
                    v1 = m.NewIntVar(0, 1, f"nod_{n}_{d}")
                    m.Add(
                        v1
                        >= X(n, d, night_idx)
                        + X(n, d + 1, off_idx)
                        + X(n, d + 2, day_idx)
                        - 2
                    )
                    safety["pattern_nod"].append(v1)
                    v2 = m.NewIntVar(0, 1, f"noe_{n}_{d}")
                    m.Add(
                        v2
                        >= X(n, d, night_idx)
                        + X(n, d + 1, off_idx)
                        + X(n, d + 2, eve_idx)
                        - 2
                    )
                    safety["pattern_noe"].append(v2)
                    v3 = m.NewIntVar(0, 1, f"eod_{n}_{d}")
                    m.Add(
                        v3
                        >= X(n, d, eve_idx)
                        + X(n, d + 1, off_idx)
                        + X(n, d + 2, day_idx)
                        - 2
                    )
                    safety["pattern_eod"].append(v3)

        # 월 최소 OFF 부족량(가능일수 클램프)
        try:
            # 개인별 O 정량 할당(나이트 전담 제외, 주휴 제외한 순수 O 목표)
            if off_idx is not None and effective_off_days > 0:
                for n in range(N):
                    if preceptee_follow and n in preceptee_indices:
                        continue
                    nu = roster_system.nurses[n]
                    raw = getattr(nu, "is_night_nurse", None)
                    is_n_only = is_n_only_profile(raw, use_mid=bool(getattr(cfg, "use_mid", False)))
                    if is_n_only:
                        continue
                    weekly_target = (
                        len(weekly_off_by_idx.get(n, []))
                        if isinstance(weekly_off_by_idx, dict)
                        else 0
                    )
                    vacation_cnt = sum(
                        1 for d in range(join[n], leave[n] + 1) if (n, d) in vacation_off_cells
                    )
                    weekend_forced = 0
                    if bool(getattr(nu, "is_weekend_off", False)):
                        try:
                            weekend_forced = sum(
                                1
                                for d in weekend_days
                                if join[n] <= d <= leave[n]
                                and not (
                                    (n, d) in fixed
                                    and fixed.get((n, d)) is not None
                                    and fixed.get((n, d)) != off_idx
                                )
                            )
                        except Exception:
                            weekend_forced = 0
                    off_bounds_for_target = compute_off_bounds(
                        source=cfg,
                        avail_days=(leave[n] - join[n] + 1),
                        vacation_cnt=vacation_cnt,
                        reference_days=D_phys,
                        weekend_only=bool(getattr(nu, "is_weekend_off", False)),
                        weekend_slots_nonvac=weekend_forced,
                    )
                    min_target = int(off_bounds_for_target["min_off_required"])
                    max_target = int(off_bounds_for_target["max_off_allowed"])
                    raw_target_o = max(0, effective_off_days - (weekly_target + weekend_forced))
                    target_o = min(max(raw_target_o, min_target), max_target)
                    if target_o <= 0:
                        continue
                    # 휴가/공가는 개인 O 목표 충족에서 제외
                    assigned_o = sum(
                        X(n, d, off_idx)
                        for d in range(join[n], leave[n] + 1)
                        if (n, d) not in vacation_off_cells
                    )
                    slack_short = m.NewIntVar(0, D, f"off_quota_short_{n}")
                    slack_excess = m.NewIntVar(0, D, f"off_quota_excess_{n}")
                    m.Add(target_o - assigned_o <= slack_short)
                    m.Add(assigned_o - target_o <= slack_excess)
                    safety["off_quota_short"].append(slack_short)
                    safety["off_quota_excess"].append(slack_excess)
                    off_quota_short_by_n[n] = slack_short
                    off_quota_excess_by_n[n] = slack_excess
                    target_o_by_n[n] = target_o
                    print(
                        f"{logger_prefix} [OffCap][force] n={n}, "
                        f"id={getattr(nu, 'nurse_id', '?')}, name={getattr(nu, 'name', '?')}, "
                        f"cap_semantics={off_cap_semantics}, target_O={target_o}, weekly_off_target={weekly_target}"
                    )
            for n in range(N):
                if preceptee_follow and n in preceptee_indices:
                    continue
                T0, T1 = join[n], leave[n]
                nu = roster_system.nurses[n]
                raw = getattr(nu, "is_night_nurse", None)
                is_n_only = is_n_only_profile(raw, use_mid=bool(getattr(cfg, "use_mid", False)))
                nurse_name = getattr(nu, "name", "?")
                nurse_id = getattr(nu, "nurse_id", "?")
                is_weekend_off = bool(getattr(nu, "is_weekend_off", False))

                avail_days = T1 - T0 + 1
                vacation_cnt = sum(
                    1 for d in range(T0, T1 + 1) if (n, d) in vacation_off_cells
                )
                structural_cnt = sum(
                    1
                    for d in range(T0, T1 + 1)
                    if (n, d) in structural_off_cells and (n, d) not in vacation_off_cells
                )
                nonvac_active_days = max(0, avail_days - vacation_cnt)
                off_bounds = compute_off_bounds(
                    source=cfg,
                    avail_days=avail_days,
                    vacation_cnt=vacation_cnt,
                    reference_days=D_phys,
                    weekend_only=is_weekend_off,
                    weekend_slots_nonvac=sum(
                        1
                        for d in weekend_days
                        if T0 <= d <= T1 and (n, d) not in vacation_off_cells
                    ),
                )
                min_off_required = int(off_bounds["min_off_required"])
                max_off_allowed_from_policy = int(off_bounds["max_off_allowed"])
                extra_allowed = int(off_bounds["max_extra_off_days"])
                # print(
                #     f"{logger_prefix} [OffCap][vac] n={n}, id={nurse_id}, name={nurse_name}, "
                #     f"base_min_off={base_min_off}, avail_days={avail_days}, "
                #     f"vacation={vacation_cnt}, min_off_required={min_off_required}"
                # )
                if min_off_required > 0 and not is_weekend_off:
                    # 휴가/공가는 최소 OFF 충족에서 제외
                    offs = sum(
                        X(n, d, off_idx)
                        for d in range(T0, T1 + 1)
                        if (n, d) not in vacation_off_cells
                    )
                    # relax_level에 따라 부분 하드: relax_level=0이면 완전 하드, 1이면 1일 부족 허용 …
                    hard_lower = max(0, min_off_required - relax_level)
                    m.Add(offs >= hard_lower)
                    miss = m.NewIntVar(0, D, f"min_off_miss_{n}")
                    m.Add(miss >= min_off_required - offs)
                    min_off_miss_by_n[n] = miss
                    safety["min_off_missing"].append(miss)
                if extra_allowed >= 0:
                    nonvac_offs = sum(
                        X(n, d, off_idx)
                        for d in range(T0, T1 + 1)
                        if (n, d) not in vacation_off_cells
                    )
                    if is_n_only:
                        total_cap_effective = max(0, avail_days - 15) + relax_level
                    else:
                        base_cap = max_off_allowed_from_policy
                        if n in per_nurse_off_cap_override:
                            base_cap = max(base_cap, per_nurse_off_cap_override[n])
                        total_cap_effective = min(base_cap + relax_level, avail_days)
                    if off_cap_bounded_slack_enable and off_cap_bounded_slack_max > 0:
                        cap_slack = m.NewIntVar(0, off_cap_bounded_slack_max, f"off_cap_slack_{n}")
                        weighted = m.NewIntVar(
                            0,
                            off_cap_bounded_slack_max * off_cap_bounded_slack_weight,
                            f"off_cap_slack_weighted_{n}",
                        )
                        m.Add(weighted == cap_slack * off_cap_bounded_slack_weight)
                        safety["off_cap_bounded_slack"].append(weighted)
                        m.Add(nonvac_offs <= total_cap_effective + cap_slack)
                    else:
                        m.Add(nonvac_offs <= total_cap_effective)
                    if stage == 3:
                        print(
                            f"{logger_prefix} [OffCap][total] n={n}, id={nurse_id}, name={nurse_name}, "
                            f"cap_semantics={off_cap_semantics}, nonvac_cap={total_cap_effective}, "
                            f"min_off={min_off_required}, vacation={vacation_cnt}, structural_nonvac={structural_cnt}, "
                            f"nonvac_active_days={nonvac_active_days}, "
                            f"is_n_only={int(is_n_only)}, weekend_off={int(is_weekend_off)}"
                        )
                    # print(
                    #     f"{logger_prefix} [OffCap][pure] n={n}, id={nurse_id}, name={nurse_name}, "
                    #     f"pure_off_cap={MAX_PURE_OFF}, effective_cap={pure_cap_effective}, "
                    #     f"extra_allowed={extra_allowed}, vacation={vacation_cnt}, "
                    #     f"is_n_only={int(is_n_only)}, weekend_off={int(is_weekend_off)}"
                    # )
        except Exception as e:
            print('예외데쇼!!', e)
            pass

        # stage별 목적/고정
        if stage == 1:
            # m.Minimize(FALLBACK_COVERAGE_SHORT_WEIGHT * sum(short_terms) + sum(over_terms))
            OFF_PENALTY=30
            m.Minimize(
            FALLBACK_COVERAGE_SHORT_WEIGHT * sum(short_terms)
            + sum(over_terms)
            + OFF_PENALTY * sum(
                X(n, d, off_idx)
                for n in range(N)
                for d in range(join[n], leave[n] + 1)
                if (n, d) not in structural_off_cells
                and (n, d) not in vacation_off_cells
                )
            )
        elif stage == 2:
            if coverage_eq is not None:
                m.Add(sum(short_terms) == coverage_eq)
            if over_le is not None:
                m.Add(sum(over_terms) <= over_le)
            safety_sum = []
            for k, arr in safety.items():
                safety_sum.extend(arr)
            m.Minimize(sum(safety_sum))
        else:
            if coverage_eq is not None:
                m.Add(sum(short_terms) == coverage_eq)
            if over_le is not None:
                m.Add(sum(over_terms) <= over_le)
            if stage2_zero_locks:
                for k, arr in stage2_zero_locks.items():
                    for v in arr:
                        m.Add(v == 0)
            obj = build_fallback_stage3_objective_terms(
                m=m,
                roster_system=roster_system,
                X=X,
                join=join,
                leave=leave,
                fixed_cnt=fixed_cnt,
                over_vars_by_day=over_vars_by_day,
                forced_off_cells=(structural_off_cells | vacation_off_cells),
                off_exception_cells=off_exception_cells,
                weekly_off_by_idx=weekly_off_by_idx,
                logger_prefix=logger_prefix,
                add_preceptor_terms_fn=add_preceptor_terms_fn,
                add_team_balance_terms_fn=add_team_balance_terms_fn,
                add_grade_constraints_fn=add_grade_constraints_fn,
            )
            m.Maximize(sum(obj))

        return (
            m,
            X,
            short_terms,
            over_terms,
            safety,
            short_vars_by_day_code,
            over_vars_by_day_code,
            target_o_by_n,
            off_quota_short_by_n,
            off_quota_excess_by_n,
            min_off_miss_by_n,
        )
    ############################################################## build model 끝 ##############################################################
    
    # ───── 1단계: 커버리지 (완화 재시도 포함) ─────
    m1, X1, short1, over1, safety1 = None, None, None, None, None
    short_map1 = {}
    over_map1 = {}
    s1 = None
    best_short, best_over = None, None
    used_relax_level = 0  # 1단계에서 성공한 완화 레벨
    max_relax_attempts = 10  # 최대 10회까지 완화 재시도
    time_per_attempt = max(3, tl1 // max_relax_attempts)  # 각 시도당 시간 (최소 3초)

    for relax_level in range(max_relax_attempts):
        with timer_cls(f"폴백 1단계: 커버리지 부족 최소화 (완화레벨={relax_level})"):
            (
                m1,
                X1,
                short1,
                over1,
                safety1,
                short_map1,
                over_map1,
                _,
                _,
                _,
                _,
            ) = build_model(
                stage=1, relax_level=relax_level
            )
            s1 = cp_model.CpSolver()
            s1.parameters.max_time_in_seconds = time_per_attempt
            s1.parameters.num_search_workers = 8
            s1.parameters.relative_gap_limit = 0.15
            st = s1.Solve(m1)
            print(
                f"{logger_prefix} 폴백1 결과: relax_level={relax_level}, "
                f"status={_cp_sat_status_to_text(st)}"
            )
            if st in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                best_short = int(s1.Value(sum(short1)))
                best_over = int(s1.Value(sum(over1)))
                used_relax_level = relax_level
                if relax_level > 0:
                    print(
                        f"{logger_prefix} 폴백1 성공: 완화레벨 {relax_level} 적용 (월 최대 OFF 상한 +{relax_level})"
                    )
                print(f"{logger_prefix} 최소 커버리지 부족: {best_short}, 과잉: {best_over}")
                try:
                    short_items = []
                    for (d, code), var in short_map1.items():
                        val = s1.Value(var)
                        if val > 0:
                            short_items.append((d, code, val))
                    if short_items:
                        short_items.sort()
                        print(
                            f"{logger_prefix} [Stage1 부족 상세] day,shift,shortage =",
                            short_items,
                        )
                    over_items = []
                    for (d, code), var in over_map1.items():
                        val = s1.Value(var)
                        if val > 0:
                            over_items.append((d, code, val))
                    if over_items:
                        over_items.sort()
                        print(
                            f"{logger_prefix} [Stage1 과잉 상세] day,shift,over =",
                            over_items,
                        )
                except Exception as exc:
                    print(f"{logger_prefix} [Stage1 상세로그 실패]: {exc}")
                break
            if relax_level < max_relax_attempts - 1:
                print(f"{logger_prefix} 폴백1 실패 (완화레벨={relax_level}): 재시도...")
            else:
                print(f"{logger_prefix} 폴백1 최종 실패: 모든 완화 시도 실패")

    if best_short is None or best_over is None:
        print(f"{logger_prefix} 폴백 중단: 1단계 해를 찾지 못함")
        return False

    # ───── 2단계: 안전/법규 ─────
    with timer_cls("폴백 2단계: 안전/법규 위반 최소화"):
        (
            m2,
            X2,
            short2,
            over2,
            safety2,
            short_map2,
            over_map2,
            _,
            _,
            _,
            _,
        ) = build_model(
            stage=2,
            coverage_eq=best_short,
            over_le=best_over,
            relax_level=used_relax_level,
        )
        s2 = cp_model.CpSolver()
        s2.parameters.max_time_in_seconds = tl2
        s2.parameters.num_search_workers = 8
        s2.parameters.relative_gap_limit = 0.15
        st2 = s2.Solve(m2)
        print(f"{logger_prefix} 폴백2 결과: status={_cp_sat_status_to_text(st2)}")
        if st2 not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            print(f"{logger_prefix} 폴백2 실패: 단계 불가능 → 1단계 해 사용")
            roster_system.roster.fill(0)
            for n in range(N):
                for d in range(join[n], leave[n] + 1):
                    for s in range(S):
                        if s1.Value(X1(n, d, s)):
                            roster_system.roster[n, d, s] = 1
            _log_weekend_work_assignments(
                roster_system=roster_system,
                weekend_days=weekend_days,
                off_idx=off_idx,
                logger_prefix=logger_prefix,
            )
            return best_short == 0
        stage2_zero_locks = {}
        best_safe_sum = 0
        for k, arr in safety2.items():
            zeros = []
            for v in arr:
                val = s2.Value(v)
                if val == 0:
                    zeros.append(v)
                best_safe_sum += int(val)
            stage2_zero_locks[k] = zeros
        print(f"{logger_prefix} 최소 안전 위반 합: {best_safe_sum}")
        try:
            for k, arr in safety2.items():
                total_k = sum(int(s2.Value(v)) for v in arr)
                if total_k > 0:
                    print(f"{logger_prefix} [Stage2 위반] {k} = {total_k}")
            short_items = [
                (d, code, int(s2.Value(var)))
                for (d, code), var in short_map2.items()
                if int(s2.Value(var)) > 0
            ]
            over_items = [
                (d, code, int(s2.Value(var)))
                for (d, code), var in over_map2.items()
                if int(s2.Value(var)) > 0
            ]
            if short_items:
                print(f"{logger_prefix} [Stage2 부족 참고] day,shift,shortage =", sorted(short_items))
            if over_items:
                print(f"{logger_prefix} [Stage2 과잉 참고] day,shift,over =", sorted(over_items))
        except Exception as exc:
            print(f"{logger_prefix} [Stage2 상세로그 실패]: {exc}")

    # ───── 3단계: 선호/공정성 ─────
    with timer_cls("폴백 3단계: 선호/공정성 최대화"):
        (
            m3,
            X3,
            short3,
            over3,
            safety3,
            short_map3,
            over_map3,
            target_o_by_n,
            off_quota_short_by_n,
            off_quota_excess_by_n,
            min_off_miss_by_n,
        ) = build_model(
            stage=3,
            coverage_eq=best_short,
            over_le=best_over,
            stage2_zero_locks=stage2_zero_locks,
            relax_level=used_relax_level,
        )
        for k in safety3.keys():
            m3.Add(sum(safety3[k]) == sum(safety2[k]))
        for n in range(N):
            for d in range(join[n], leave[n] + 1):
                for s in range(S):
                    try:
                        m3.AddHint(X3(n, d, s), s2.Value(X2(n, d, s)))
                    except Exception:
                        pass
        s3 = cp_model.CpSolver()
        s3.parameters.max_time_in_seconds = tl3
        s3.parameters.num_search_workers = 8
        s3.parameters.relative_gap_limit = 0.05
        st3 = s3.Solve(m3)
        print(f"{logger_prefix} 폴백3 결과: status={_cp_sat_status_to_text(st3)}")
        if st3 not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            print(f"{logger_prefix} 폴백3 실패: 선호 단계 불가능 → 2단계 해 사용")
            roster_system.roster.fill(0)
            for n in range(N):
                for d in range(join[n], leave[n] + 1):
                    for s in range(S):
                        if s2.Value(X2(n, d, s)):
                            roster_system.roster[n, d, s] = 1
            _log_weekend_work_assignments(
                roster_system=roster_system,
                weekend_days=weekend_days,
                off_idx=off_idx,
                logger_prefix=logger_prefix,
            )
            return best_short == 0 and best_safe_sum == 0
        try:
            short_items = [
                (d, code, int(s3.Value(var)))
                for (d, code), var in short_map3.items()
                if int(s3.Value(var)) > 0
            ]
            over_items = [
                (d, code, int(s3.Value(var)))
                for (d, code), var in over_map3.items()
                if int(s3.Value(var)) > 0
            ]
            if short_items:
                print(f"{logger_prefix} [Stage3 부족 참고] day,shift,shortage =", sorted(short_items))
            if over_items:
                print(f"{logger_prefix} [Stage3 과잉 참고] day,shift,over =", sorted(over_items))
            for k, arr in safety3.items():
                total_k = sum(int(s3.Value(v)) for v in arr)
                if total_k > 0:
                    print(f"{logger_prefix} [Stage3 위반] {k} = {total_k}")
            # 실제 배정된 휴무 카운트(O/주휴/휴가) 요약
            if off_idx is not None:
                for n, nu in enumerate(roster_system.nurses):
                    assigned_off = sum(
                        int(s3.Value(X3(n, d, off_idx))) for d in range(join[n], leave[n] + 1)
                    )
                    vac_cnt = sum(
                        1
                        for d in range(join[n], leave[n] + 1)
                        if (n, d) in off_exception_vacation_cells
                    )
                    weekly_target = len(weekly_off_by_idx.get(n, []) if isinstance(weekly_off_by_idx, dict) else [])
                    target_o = target_o_by_n.get(n)
                    slack_short_val = (
                        s3.Value(off_quota_short_by_n[n]) if n in off_quota_short_by_n else None
                    )
                    slack_excess_val = (
                        s3.Value(off_quota_excess_by_n[n]) if n in off_quota_excess_by_n else None
                    )
                    min_off_miss_val = (
                        s3.Value(min_off_miss_by_n[n]) if n in min_off_miss_by_n else None
                    )
                    print(
                        f"{logger_prefix} [OffCount][final] n={n}, "
                        f"id={getattr(nu, 'nurse_id', '?')}, name={getattr(nu, 'name', '?')}, "
                        f"cap_semantics={off_cap_semantics}, "
                        f"assigned_O={assigned_off}, vacation={vac_cnt}, weekly_off_target={weekly_target}, "
                        f"target_O={target_o}, slack_short={slack_short_val}, slack_excess={slack_excess_val}, "
                        f"min_off_miss={min_off_miss_val}"
                    )
        except Exception as exc:
            print(f"{logger_prefix} [Stage3 상세로그 실패]: {exc}")

    roster_system.roster.fill(0)
    for n in range(N):
        for d in range(join[n], leave[n] + 1):
            for s in range(S):
                if s3.Value(X3(n, d, s)):
                    roster_system.roster[n, d, s] = 1
    log_n_even_distribution(roster_system, logger_prefix, join=join, leave=leave)
    try:
        print(f"{logger_prefix} [PostOff] 시작: 최종 stage3 해 기반 후처리 시도")
        before_viol = len(roster_system._find_violations())
        postprocess_rebalance_off_fn(roster_system)
        after_viol = len(roster_system._find_violations())
        print(
            f"{logger_prefix} [PostOff] 종료: viol {before_viol}->{after_viol} "
            f"(감소={before_viol - after_viol})"
        )
    except Exception as exc:
        print(f"{logger_prefix} [PostOff] 후처리 실패: {exc}")

    # ── 후처리 완료 후 프리셉티 roster를 프리셉터와 동기화 ──
    # 규칙: 프리셉터의 DEN/O → 프리셉티 동일 복사
    #       프리셉터의 특수코드(W 등) → 프리셉티는 OFF
    if preceptee_follow and preceptee_indices:
        _fb_id_to_idx = {nu.db_id: n for n, nu in enumerate(roster_system.nurses)}
        _fb_shift_types = cfg.shift_types
        _fb_off_idx = _fb_shift_types.index('O') if 'O' in _fb_shift_types else None
        _fb_standard = {'D', 'E', 'N', 'O'}
        if bool(getattr(cfg, 'use_mid', False)):
            _fb_standard.add('M')
        _fb_pte_fw = getattr(roster_system, '_preceptee_fixed_wanted_map', {})
        synced = 0
        special_converted = 0
        _fb_fw_restored = 0
        for pte_idx in preceptee_indices:
            pid = getattr(roster_system.nurses[pte_idx], 'preceptor_id', None)
            if not pid or pid not in _fb_id_to_idx:
                continue
            ptr_idx = _fb_id_to_idx[pid]
            roster_system.roster[pte_idx] = roster_system.roster[ptr_idx].copy()
            # 특수코드 일자는 프리셉티를 OFF로 전환
            # 단, type=근무 + shift_gb=D/E/N 계열 하위코드는 근무이므로 그대로 유지
            _fb_work_sub = getattr(roster_system, '_work_sub_ids', set())
            _fb_orig_map = getattr(roster_system, '_fixed_original_shift_map', {})
            if _fb_off_idx is not None:
                for d in range(roster_system.num_days):
                    # 프리셉티 fixed_wanted 일자는 프리셉터 복사 대신 본인 값 적용
                    if (pte_idx, d) in _fb_pte_fw:
                        _fw_code = _fb_pte_fw[(pte_idx, d)].strip().upper()
                        if _fw_code in _fb_shift_types:
                            roster_system.roster[pte_idx, d, :] = 0
                            roster_system.roster[pte_idx, d, _fb_shift_types.index(_fw_code)] = 1
                            _fb_fw_restored += 1
                        continue
                    _fb_need = False
                    _fb_orig = _fb_orig_map.get((ptr_idx, d))
                    if _fb_orig:
                        _fb_ou = _fb_orig.upper()
                        if _fb_ou not in _fb_standard and _fb_ou not in _fb_work_sub:
                            _fb_need = True
                    else:
                        _idx_arr = np.where(roster_system.roster[ptr_idx, d] == 1)[0]
                        if len(_idx_arr) > 0:
                            _fb_sc = _fb_shift_types[int(_idx_arr[0])]
                            if _fb_sc not in _fb_standard and _fb_sc.upper() not in _fb_work_sub:
                                _fb_need = True
                    if _fb_need:
                        roster_system.roster[pte_idx, d, :] = 0
                        roster_system.roster[pte_idx, d, _fb_off_idx] = 1
                        special_converted += 1
            synced += 1
        if synced:
            msg = f"{logger_prefix} [PrecepteeSync] 후처리 후 프리셉티 roster 동기화: {synced}명"
            if special_converted:
                msg += f" (특수코드→OFF 전환: {special_converted}건)"
            if _fb_fw_restored:
                msg += f", fixed_wanted 재적용: {_fb_fw_restored}건"
            print(msg)
        # if bool(getattr(cfg, "ban_e_to_d", True)) and _fb_off_idx is not None:
        #     _fb_eve_idx = _fb_shift_types.index('E') if 'E' in _fb_shift_types else None
        #     _fb_day_idx = _fb_shift_types.index('D') if 'D' in _fb_shift_types else None
        #     _fixed_blocked = 0
        #     _repaired = 0
        #     if _fb_eve_idx is not None and _fb_day_idx is not None:
        #         for pte_idx in preceptee_indices:
        #             for d in range(1, roster_system.num_days):
        #                 if int(roster_system.roster[pte_idx, d - 1, _fb_eve_idx]) != 1:
        #                     continue
        #                 if int(roster_system.roster[pte_idx, d, _fb_day_idx]) != 1:
        #                     continue
        #                 cur_fixed = (pte_idx, d) in _fb_pte_fw
        #                 prev_fixed = (pte_idx, d - 1) in _fb_pte_fw
        #                 if not cur_fixed:
        #                     roster_system.roster[pte_idx, d, :] = 0
        #                     roster_system.roster[pte_idx, d, _fb_off_idx] = 1
        #                     _repaired += 1
        #                 elif not prev_fixed:
        #                     roster_system.roster[pte_idx, d - 1, :] = 0
        #                     roster_system.roster[pte_idx, d - 1, _fb_off_idx] = 1
        #                     _repaired += 1
        #                 else:
        #                     _fixed_blocked += 1
        #     if _repaired or _fixed_blocked:
        #         print(
        #             f"{logger_prefix} [PrecepteeSync][Repair-E->D] repaired={_repaired}, blocked_fixed={_fixed_blocked}"
        #         )

    _log_weekend_work_assignments(
        roster_system=roster_system,
        weekend_days=weekend_days,
        off_idx=off_idx,
        logger_prefix=logger_prefix,
    )
    print(f"{logger_prefix} 폴백 완료: 커버리지부족={best_short}, 안전위반합={best_safe_sum}")
    return best_short == 0 and best_safe_sum == 0
