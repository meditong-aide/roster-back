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
from services.constraints.team_constraints import add_team_min_constraints
from services.day_windows import iter_nurse_days, build_active_days


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
    blocked_by_nurse: dict[int, set[int]] | None = None,
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
    prev_off_tail = getattr(roster_system, "prev_month_off_tail_by_idx", {}) or {}
    prev_month_n_tail_by_idx = getattr(roster_system, "prev_month_n_tail_by_idx", {}) or {}
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
    fixed_wanted_cells: set[tuple[int, int]] = set()
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
        # 빌더가 명시한 shift_type 을 우선 (예: weekly_off="주휴", special="휴가/휴무/공가").
        fixed_type_by_cell[(n, d)] = (
            (c.get("shift_type") or "").strip()
            or code2type.get(raw_code)
            or code2type.get(s_main)
        )
        if str(c.get("fixed_source") or "").strip().lower() == "fixed_wanted":
            fixed_wanted_cells.add((n, d))

        # print('이미 있음 fixed_type_by_cell', fixed_type_by_cell)
        # print('이미 있음 fixed_type_by_cell', fixed_type_by_cell)

    # 초기 금지(경계) 맵
    initial_forbidden = (
        getattr(roster_system, "initial_forbidden", {})
        if isinstance(getattr(roster_system, "initial_forbidden", {}), dict)
        else {}
    )

    # ── 프리셉티 인덱스 사전 계산 (preceptee_on 무관하게 항상 빌드 — 커버리지 제외에 필요) ──
    preceptee_follow = bool(getattr(cfg, 'preceptee_on', False))
    preceptee_indices: set[int] = set()
    _fb_id_to_idx = {nu.db_id: n for n, nu in enumerate(roster_system.nurses)}
    for n, nu in enumerate(roster_system.nurses):
        pid = getattr(nu, 'preceptor_id', None)
        if pid:
            preceptee_indices.add(n)
    # 프리셉티 기간 제한: assignment 기간 내에만 follow (기간 외 독립 배정)
    preceptee_follow_days: dict[int, set[int]] = getattr(roster_system, "preceptee_follow_days", {}) or {}
    # 안전망: 월 전체 cover entry 는 default 동작(전체 월 follow)과 동등 → 솔버 hard 제약
    # 인스턴스화 시 capacity 모순 회피를 위해 dict 에서 제거하고 default 분기로 위임한다.
    _full_month_set_fb = set(range(roster_system.num_days))
    _full_keys_fb = [n for n, days in preceptee_follow_days.items() if set(days) == _full_month_set_fb]
    for n in _full_keys_fb:
        del preceptee_follow_days[n]
    if _full_keys_fb:
        print(f"{logger_prefix} [Fallback] 프리셉티 전체월 follow → default 위임: solver_idx={_full_keys_fb}")
    _has_preceptee_period = bool(preceptee_follow_days)
    # dispatch(assignment) 기반 프리셉티도 인덱스에 포함
    if _has_preceptee_period:
        for n in preceptee_follow_days:
            if n not in preceptee_indices:
                preceptee_indices.add(n)
    if preceptee_indices:
        print(f"{logger_prefix} [Fallback] 프리셉티 인덱스: {len(preceptee_indices)}명 (follow={preceptee_follow})")
    # 기간이 빈 set인 프리셉티 = 해당 월에서 프리셉티 아님 → preceptee_indices에서 제거
    if _has_preceptee_period:
        _empty_period = {n for n, days in preceptee_follow_days.items() if len(days) == 0}
        if _empty_period:
            preceptee_indices -= _empty_period
            print(f"{logger_prefix} [Fallback] 프리셉티 기간 종료 → 인덱스 제거: {_empty_period}")

    def _is_preceptee_at(n: int, d: int = -1) -> bool:
        """(n, d)가 프리셉티 follow 대상인지 판별.
        d=-1: nurse-level. 기간 미설정이면 True(전체 follow), 기간 설정이면 False(day별 판별 필요)
        d>=0: day-level (해당 day가 기간 내인지)
        """
        if not preceptee_follow or n not in preceptee_indices:
            return False
        if not _has_preceptee_period:
            return True  # 기간 미설정 → 전체 월 follow (기존 동작)
        if n not in preceptee_follow_days:
            return True  # 이 간호사에 대한 기간 미설정 → 전체 월 follow
        if d < 0:
            return False  # nurse-level: 기간 설정됨 → 제약 skip 안 함 (day별 판별 필요)
        return d in preceptee_follow_days[n]

    exclude_preceptee_from_den = (not getattr(cfg, 'preceptee_shift_count', True)) and bool(preceptee_indices)
    coverage_exclude_cells: set[tuple[int, int]] = getattr(roster_system, "coverage_exclude_cells", set()) or set()
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
            # 2N2O/3N2O 활성 시 자동 확장분 반영
            _noff_extra = 0
            if bool(cfg.two_offs_after_two_nig) or bool(cfg.two_offs_after_three_nig):
                _noff_extra = 2
            effective_extra = extra_allowed + _noff_extra
            max_off_allowed_per_person = base_min_off + effective_extra
            if bool(cfg.two_offs_after_two_nig) and max_off_allowed_per_person < base_min_off + 5:
                est_extra_off_from_2n2o = (
                    int(total_need_n / len(n_allowed_indices) * 0.5)
                    if n_allowed_indices
                    else 0
                )
                if est_extra_off_from_2n2o > effective_extra:
                    print(
                        f"{logger_prefix} [FallbackFeasibility][WARN] "
                        f"2N→2OFF 하드가 예상 강제 OFF({est_extra_off_from_2n2o})가 월 최대 OFF 여유({effective_extra}, "
                        f"config={extra_allowed}+자동확장={_noff_extra})를 초과할 수 있습니다. "
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
        # relax_level >= 3: max coverage + M min hard → soft 전환 (인원 부족 시 생성 보장)
        _relax_coverage = relax_level >= 3
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

        _false_var = m.NewConstant(0)
        def X(n, d, s):
            return Xv.get((n, d, s), _false_var)

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
                    _blocked_set = blocked_by_nurse.get(n, set()) if blocked_by_nurse else set()
                    _n_blocked = len(_blocked_set)
                    avail_days = T1 - T0 + 1 - _n_blocked
                    forced_off_cnt = sum(
                        1
                        for d in range(T0, T1 + 1)
                        if (n, d) in structural_off_cells and d not in _blocked_set
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
            except Exception as e:
                print(f"{logger_prefix} [HardCheck] 강제 OFF 상한 초과 여부 실패: {e}")
                pass
        except Exception as exc:
            print(f"{logger_prefix} [HardCheck] precheck 실패: {exc}")

        for n in range(N):
            for d in iter_nurse_days(n, join, leave, blocked_by_nurse):
                for s in range(S):
                    Xv[n, d, s] = m.NewBoolVar(f"x_{n}_{d}_{s}")
        active_days = build_active_days(N, join, leave, blocked_by_nurse)
        # 고정 셀
        for (n, d), s_idx in fixed.items():
            if (n, d) not in active_days:
                continue
            if _is_preceptee_at(n):
                continue
            m.Add(X(n, d, s_idx) == 1)
            for s in range(S):
                if s != s_idx:
                    m.Add(X(n, d, s) == 0)
        # W(특별 근무)는 고정 셀 외에는 전부 금지
        if has_w and w_idx is not None:
            for n in range(N):
                if _is_preceptee_at(n):
                    continue
                for d in iter_nurse_days(n, join, leave, blocked_by_nurse):
                    if (n, d) in fixed and fixed[(n, d)] == w_idx:
                        continue
                    m.Add(X(n, d, w_idx) == 0)
        # 순수 O 4연속 금지 (fixed로 이미 4O면 경고만 남기고 스킵)
        # cfg.skip_4o_hard_first_days: 월초 N일 구간에서는 4O Hard 미적용 (기본 3)
        if off_idx is not None:
            vac_cells = set(off_exception_vacation_cells)
            skip_4o_hard_first_days = int(getattr(cfg, "skip_4o_hard_first_days", 3) or 0)
            for n in range(N):
                if _is_preceptee_at(n):
                    continue
                for d in range(join[n], leave[n] - 2):
                    if d + 3 > leave[n]:
                        continue
                    if any((n, d+k) not in active_days for k in range(4)):
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
        # ── 4O 월경계 제약: 전월 꼬리 연속 OFF + 현월 초 연속 OFF 합산 4 이상 금지 (하드) ──
        _4o_cross_affected_fb: set[int] = set()
        prev_off_tail = getattr(roster_system, "prev_month_off_tail_by_idx", {}) or {}
        print(f"{logger_prefix} [4O-cross-month-debug] prev_off_tail_by_idx keys={list(prev_off_tail.keys())}, "
              f"values={dict(prev_off_tail)}, N={N}")
        for n in range(N):
            if _is_preceptee_at(n):
                continue
            t = prev_off_tail.get(n, 0)
            if t <= 0 or t >= 4:
                continue
            if join[n] > 0:
                continue
            need = 4 - t
            window_days = list(range(0, min(need, leave[n] + 1)))
            if len(window_days) < need:
                continue
            free_vars = []
            effective_t = t
            _detail_per_day = []
            for wd in window_days:
                in_structural = (n, wd) in structural_off_cells
                in_fixed_off = (n, wd) in fixed and fixed[(n, wd)] == off_idx
                is_fixed_off = in_structural or in_fixed_off
                if is_fixed_off:
                    effective_t += 1
                    _detail_per_day.append(f"day{wd}=고정OFF(struct={in_structural},fixed={in_fixed_off})")
                else:
                    free_vars.append(is_pure_o(n, wd))
                    _detail_per_day.append(f"day{wd}=free")
            if effective_t >= 4:
                nu = roster_system.nurses[n] if n < len(roster_system.nurses) else None
                print(f"{logger_prefix} [4O-cross-month-SKIP] nurse_idx={n}, "
                      f"name={getattr(nu, 'name', '?')}, prev_tail={t}, "
                      f"effective_t={effective_t}>=4 → 제약 스킵 (이미 4O 불가피), "
                      f"detail={_detail_per_day}")
                continue
            if not free_vars:
                continue
            remaining = 3 - effective_t
            m.Add(sum(free_vars) <= remaining)
            _4o_cross_affected_fb.add(n)
            nu = roster_system.nurses[n] if n < len(roster_system.nurses) else None
            print(
                f"{logger_prefix} [4O-cross-month] nurse_idx={n}, "
                f"name={getattr(nu, 'name', '?')}, prev_tail={t}, "
                f"고정OFF={effective_t - t}, free={len(free_vars)}, OFF<={remaining}, "
                f"detail={_detail_per_day}"
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
                if _is_preceptee_at(n):
                    continue
                if not bool(getattr(nu, "is_weekend_off", False)):
                    continue
                for d in iter_nurse_days(n, join, leave, blocked_by_nurse):
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
                        if d <= 1 and getattr(roster_system, "prev_month_n_tail_by_idx", {}).get(n, 0) >= 2:
                            continue
                        # off_window 범위 내 평일: 전월 꼬리 연속근무 보정을 위해 OFF 허용 필요
                        _ow_ranges_fb = (getattr(roster_system, "off_window_constraints", {}) or {}).get(n, []) or []
                        if any(ws <= d <= we for (ws, we) in _ow_ranges_fb):
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
        #         if _is_preceptee_at(n):
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
                    if _is_preceptee_at(n):
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
            for d in iter_nurse_days(n, join, leave, blocked_by_nurse):
                if (n, d) in fixed and not _is_preceptee_at(n, d):
                    continue
                m.AddExactlyOne(X(n, d, s) for s in range(S))

        # 프리셉티 팔로우 제약 (fallback) — assignment 기간 내에만 적용
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
                    if not _is_preceptee_at(n, d):
                        continue
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
        _fb_max_by_day = getattr(cfg, "daily_shift_requirements_max_by_day", None)
        _fb_has_any_max = isinstance(_fb_max_by_day, list) and any(
            any(int(v or 0) > 0 for v in dm.values())
            for dm in _fb_max_by_day if isinstance(dm, dict)
        )
        # off_first=True: max coverage 미설정 코드/일에 대해 min을 max로 강제(=잔여 셀 OFF 회수)
        _fb_off_first_cfg = bool(getattr(cfg, "off_first", False))
        print(f"{logger_prefix} [OffFirstCoverage] off_first={_fb_off_first_cfg}, _fb_has_any_max={_fb_has_any_max} → force_min_as_max={_fb_off_first_cfg and not _fb_has_any_max}")
        short_terms, over_terms = [], []
        over_vars_by_day = {}
        short_vars_by_day_code: Dict[tuple[int, str], cp_model.IntVar] = {}
        over_vars_by_day_code: Dict[tuple[int, str], cp_model.IntVar] = {}
        zero_demand_block_codes = {"D", "E", "N", "M"}
        _fb_daily_assigned_by_code: dict[str, list] = {}  # 일자별 커버리지 균등화용
        for d in range(D):
            if (
                hasattr(cfg, "daily_shift_requirements_by_day")
                and isinstance(cfg.daily_shift_requirements_by_day, list)
                and d < len(cfg.daily_shift_requirements_by_day)
            ):
                need_map = cfg.daily_shift_requirements_by_day[d]
            else:
                need_map = cfg.daily_shift_requirements
            need_max_map = _fb_max_by_day[d] if isinstance(_fb_max_by_day, list) and d < len(_fb_max_by_day) else None
            for code, req in need_map.items():
                if code not in roster_system.config.shift_types:
                    continue
                s = roster_system.config.shift_types.index(code)
                req_raw = max(0, int(req or 0))
                need = req_raw - _fb_fixed_cnt_adj[d][s]
                req_max_raw = int((need_max_map or {}).get(code, 0) or 0)
                need_max = max(0, req_max_raw - _fb_fixed_cnt_adj[d][s]) if req_max_raw > 0 else 0
                assigned = sum(
                    X(n, d, s)
                    for n in range(N)
                    if join[n] <= d <= leave[n] and (n, d) not in fixed
                    and (not exclude_preceptee_from_den or not _is_preceptee_at(n, d))
                    and (n, d) not in coverage_exclude_cells
                )
                if code == "M":
                    if m_bucket_indices:
                        assigned_m_bucket = sum(
                            X(n, d, s2)
                            for n in range(N)
                            if join[n] <= d <= leave[n]
                            and (n, d) not in fixed
                            and (not exclude_preceptee_from_den or not _is_preceptee_at(n, d))
                            and (n, d) not in coverage_exclude_cells
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
                    m_need = max(0, int(req_raw - fixed_m_bucket))
                    m_cap_max = max(0, int(req_max_raw - fixed_m_bucket)) if req_max_raw > 0 else 0
                    # M min coverage: max coverage 있으면 hard, 없으면 soft
                    # _relax_coverage 활성 시: 항상 soft (인원 부족 대응)
                    if _fb_has_any_max and not _relax_coverage and m_need > 0:
                        m.Add(assigned_m_bucket >= m_need)
                        sh = m.NewIntVar(0, 0, f"short_{d}_{code}")
                    else:
                        sh = m.NewIntVar(0, m_need if m_need > 0 else 0, f"short_{d}_{code}")
                        if m_need > 0:
                            m.Add(assigned_m_bucket + sh >= m_need)
                    # M 상한: max coverage 있으면 hard, 없으면 min으로 hard cap
                    # _relax_coverage 활성 시: max도 soft
                    if m_cap_max > 0 and not _relax_coverage:
                        m.Add(assigned_m_bucket <= m_cap_max)
                        ov = m.NewIntVar(0, 0, f"over_{d}_{code}")
                    elif m_cap_max > 0 and _relax_coverage:
                        ov = m.NewIntVar(0, N, f"over_{d}_{code}")
                        m.Add(ov >= assigned_m_bucket - m_cap_max)
                    else:
                        m_cap_non_fixed = max(0, int(req_raw - fixed_m_bucket))
                        m.Add(assigned_m_bucket <= m_cap_non_fixed)
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
                # min 제약: assigned + shortage >= need
                if need <= 0:
                    sh = m.NewIntVar(0, 0, f"short_{d}_{code}")
                else:
                    sh = m.NewIntVar(0, N, f"short_{d}_{code}")
                    m.Add(assigned + sh >= need)
                # max 제약: hard (상한 초과 불가), _relax_coverage 시 soft
                if need_max > 0 and d < D_phys:
                    _fb_daily_assigned_by_code.setdefault(code, []).append((d, assigned, need))
                if need_max > 0 and not _relax_coverage:
                    m.Add(assigned <= need_max)
                    ov = m.NewIntVar(0, 0, f"over_{d}_{code}")
                elif need_max > 0 and _relax_coverage:
                    ov = m.NewIntVar(0, N, f"over_{d}_{code}")
                    m.Add(ov >= assigned - need_max)
                elif _fb_off_first_cfg:
                    # off_first=True 우선: max 미설정 시 assigned <= need 하드 (잔여 셀 OFF로 회수).
                    # relax_coverage / need=0 무관 강제 — fixed_wanted 가 min 다 채워도 추가 근무 차단.
                    m.Add(assigned <= max(0, need))
                    ov = m.NewIntVar(0, 0, f"over_{d}_{code}")
                elif need > 0:
                    ov = m.NewIntVar(0, N, f"over_{d}_{code}")
                    m.Add(assigned - ov <= need)
                else:
                    ov = m.NewIntVar(0, 0, f"over_{d}_{code}")
                short_terms.append(sh)
                over_terms.append(ov)
                over_vars_by_day.setdefault(d, {})[code] = ov
                short_vars_by_day_code[(d, code)] = sh
                over_vars_by_day_code[(d, code)] = ov

        # 1-B) Max coverage / off_first OFF 균등 분배
        # off_first=True: max coverage 미설정이라도 OFF가 잔여 셀로 회수되므로
        # 일반 간호사 사이에 균등 분배 유도 (전담/주말휴무/preceptee 제외)
        _fb_max_cov_off_equalize_terms = []
        if _fb_has_any_max or _fb_off_first_cfg:
            _fb_nurse_off_vars = []
            for n in range(N):
                if n in preceptee_indices:
                    continue
                if _fb_off_first_cfg:
                    # 주말휴무자·N 전담은 cap 관리 대상 아님 → 풀에서 제외
                    _nu = roster_system.nurses[n] if n < len(roster_system.nurses) else None
                    if _nu is not None and bool(getattr(_nu, "is_weekend_off", False)):
                        continue
                    _raw_nn = getattr(_nu, "is_night_nurse", None) if _nu is not None else None
                    if isinstance(_raw_nn, (set, list, tuple)) and set(_raw_nn) == {"N"}:
                        continue
                # off_first=False 경로의 nonvac_offs 식과 동일한 도메인:
                #   range(T0, T1+1) 중 vacation_off_cells 제외, 고정 OFF는 X(n,d,off)=1 자동
                T0, T1 = join[n], leave[n]
                # off_first=True HARD 풀 가드: 풀먼스 active window 아닌 간호사는 제외
                #   - 중도 가입자(T0>0) / 중도 퇴사자(T1<D_phys-1) → 최대 OFF 용량 상이
                #   - blocked_by_nurse 보유자 → 출장/연수 등으로 OFF 가용량 비대칭
                _fb_blk_set_n = blocked_by_nurse.get(n, set()) if blocked_by_nurse else set()
                if _fb_off_first_cfg:
                    if T0 > 0 or T1 < D_phys - 1 or _fb_blk_set_n:
                        continue
                _phys_days_n = [d for d in range(T0, T1 + 1) if (n, d) not in vacation_off_cells]
                if not _phys_days_n:
                    continue
                _total_off_n = m.NewIntVar(0, len(_phys_days_n), f"fb_mc_off_{n}")
                m.Add(_total_off_n == sum(X(n, d, off_idx) for d in _phys_days_n))
                _fb_nurse_off_vars.append(_total_off_n)
            if len(_fb_nurse_off_vars) >= 2:
                _fb_off_max = m.NewIntVar(0, D_phys, "fb_mc_off_max")
                _fb_off_min = m.NewIntVar(0, D_phys, "fb_mc_off_min")
                m.AddMaxEquality(_fb_off_max, _fb_nurse_off_vars)
                m.AddMinEquality(_fb_off_min, _fb_nurse_off_vars)
                _fb_off_range = m.NewIntVar(0, D_phys, "fb_mc_off_range")
                m.Add(_fb_off_range == _fb_off_max - _fb_off_min)
                # off_first=True: OFF range는 SOFT objective(가중치)로만 유도, HARD 제거.
                # (사용자 명세: off_days 무시 + daily 커버리지 우선 → OFF 균등은 차순위)
                if _fb_off_first_cfg:
                    print(f"{logger_prefix} [MaxCoverage/OffFirst] OFF range는 SOFT (off_first=True)")
                _fb_off_eq_w = -100000 if _fb_off_first_cfg else -200
                _fb_max_cov_off_equalize_terms.append(_fb_off_eq_w * _fb_off_range)
                if _fb_off_first_cfg and len(_fb_nurse_off_vars) >= 3:
                    _fbN = len(_fb_nurse_off_vars)
                    _fb_off_sum = m.NewIntVar(0, D_phys * _fbN, "fb_mc_off_sum")
                    m.Add(_fb_off_sum == sum(_fb_nurse_off_vars))
                    for _i, _ov in enumerate(_fb_nurse_off_vars):
                        _dev = m.NewIntVar(0, D_phys * _fbN, f"fb_mc_off_dev_{_i}")
                        m.Add(_dev * _fbN >= _ov * _fbN - _fb_off_sum)
                        m.Add(_dev * _fbN >= _fb_off_sum - _ov * _fbN)
                        _fb_max_cov_off_equalize_terms.append(-2000 * _dev)
                print(f"{logger_prefix} [MaxCoverage/OffFirst] OFF 균등 분배 제약 추가: 간호사 {len(_fb_nurse_off_vars)}명, range_weight={_fb_off_eq_w}")

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
                if _is_preceptee_at(n):
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
            if _is_preceptee_at(n):
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
                if _is_preceptee_at(n):
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

        # 휴가/공가 fixed 셀의 직전일 N 금지 (하드, 휴가/공가 보호 정책).
        # fixed_wanted O / 휴무 / 주휴 등은 사용자 자발 OFF 또는 자동 OFF 라 대상 외.
        # 단 prev_d == T0(day 0) 자체는 cross-month 면제.
        _BAN_N_TYPES = {"휴가", "공가"}
        if bool(getattr(cfg, "ban_night_before_fixed_off", False)):
            for n in range(N):
                if _is_preceptee_at(n):
                    continue
                T0, T1 = join[n], leave[n]
                _ban_n_cnt = 0
                for d in range(T0 + 1, T1 + 1):
                    if (n, d) not in fixed:
                        continue
                    _fw_type = fixed_type_by_cell.get((n, d))
                    if _fw_type not in _BAN_N_TYPES:
                        continue  # 휴가/휴무/공가 외 type 은 BanN 대상 아님
                    prev_d = d - 1
                    if prev_d < T0:
                        continue
                    if blocked_by_nurse and prev_d in blocked_by_nurse.get(n, set()):
                        continue
                    if (n, prev_d) in fixed:
                        continue  # 이미 고정된 셀은 변경 불가
                    m.Add(X(n, prev_d, night_idx) == 0)
                    _ban_n_cnt += 1
                if _ban_n_cnt > 0:
                    print(f"{logger_prefix} [BanNBeforeFixedOff] nurse_idx={n}: {_ban_n_cnt}건 N 금지")

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
                    if _is_preceptee_at(n):
                        continue
                    # 주말 휴무자도 월경계 연속근무 초과 가능 → 동일 적용
                    T0, T1 = join[n], leave[n]
                    _blocked_ow = blocked_by_nurse.get(n, set()) if blocked_by_nurse else set()
                    for (w_start, w_end) in off_windows.get(n, []) or []:
                        left = max(T0, w_start)
                        right = min(T1, w_end)
                        if left > right:
                            continue
                        # 유저 고정 우선: 윈도우 내 고정 비-OFF 셀은 제외하고 적용
                        # blocked day도 제외 (X 변수 없음 → sum=0 → INFEASIBLE 방지)
                        free_days_w = [d for d in range(left, right + 1) if d not in _blocked_ow and not ((n, d) in fixed and fixed[(n, d)] != off_idx)]
                        if not free_days_w:
                            print(f"{logger_prefix} off_window 무시 (유저 고정 우선, fallback): n={n}, window=[{left+1},{right+1}] 전체 고정")
                            continue
                        m.Add(sum(X(n, d, off_idx) for d in free_days_w) >= 1)
        except Exception as e:
            print(f"{logger_prefix} 월초 OFF 윈도우 적용 실패(fallback): err={e}")

        # 연속 근무 K+1 창에서 최소 1 OFF 필요 → HARD 제약 (fixed_wanted 포함, 우회 불가)
        # 정책:
        #   - blocked day 포함 윈도우: X 변수 부재 → 자동 중단 → 스킵
        #   - fixed OFF 포함 윈도우: 자동 만족 → 스킵
        #   - 그 외: 전체 윈도우에 대해 enforce. 유저가 K+1 연속 근무를 fixed_wanted로 지정했다면 INFEASIBLE로 보고.
        K = cfg.max_consecutive_work_days
        for n in range(N):
            if _is_preceptee_at(n):
                continue
            T0, T1 = join[n], leave[n]
            _blocked = blocked_by_nurse.get(n, set()) if blocked_by_nurse else set()
            for d0 in range(T0, T1 - K + 1):
                window = [d0 + t for t in range(K + 1)]
                if any(d in _blocked for d in window):
                    continue
                if any((n, d) in fixed and fixed[(n, d)] == off_idx for d in window):
                    continue
                m.Add(sum(X(n, d, off_idx) for d in window) >= 1)

        # 연속 Night 상한 L → 초과량 정량화
        L = cfg.max_consecutive_nights
        for n in range(N):
            if _is_preceptee_at(n):
                continue
            T0, T1 = join[n], leave[n]
            n_tail = prev_month_n_tail_by_idx.get(n, 0)
            _n_offs_after_cnight = (getattr(roster_system, "prev_month_n_offs_after_by_idx", {}) or {}).get(n, 0)
            # offs_after >= 1 이면 야간 연속이 이미 끊긴 상태 → 월경계 연속N 제약 스킵
            if n_tail > 0 and _n_offs_after_cnight == 0:
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
            if _is_preceptee_at(n):
                continue
            T0, T1 = join[n], leave[n]
            sum_m = sum(X(n, d, night_idx) for d in range(T0, T1 + 1))
            m.Add(sum_m <= cfg.max_night_shifts_per_month)

        # N 전담: D/E 하드 금지 (메인 모델과 동일)
        for n, nu in enumerate(roster_system.nurses):
            if _is_preceptee_at(n):
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
                if _is_preceptee_at(n):
                    continue
                for w in range(weeks):
                    d0, d1 = w * 7, min(w * 7 + 7, D)
                    offs = sum(X(n, d, off_idx) for d in range(d0, d1) if join[n] <= d <= leave[n])
                    miss = m.NewIntVar(0, 2, f"week_miss_{n}_{w}")
                    m.Add(miss >= 2 - offs)
                    safety["week_off_missing"].append(miss)

        # 회복 규칙: N3→2O, N2→2O 부족량
        if cfg.two_offs_after_three_nig:
            _n_offs_after_map_3n = getattr(roster_system, "prev_month_n_offs_after_by_idx", {}) or {}
            for n in range(N):
                if _is_preceptee_at(n):
                    continue
                T0, T1 = join[n], leave[n]
                _blocked_3n = blocked_by_nurse.get(n, set()) if blocked_by_nurse else set()
                n_tail = prev_month_n_tail_by_idx.get(n, 0)
                n_offs_after_3n = _n_offs_after_map_3n.get(n, 0)
                _3n_rem = max(0, 2 - n_offs_after_3n) if n_tail >= 3 else 2
                if n_tail >= 3 and _3n_rem > 0 and (T0 + 1) <= T1 and T0 not in _blocked_3n and (T0 + 1) not in _blocked_3n:
                    end_prev_block = m.NewBoolVar(f"end_3n_prev_soft_{n}")
                    m.Add(end_prev_block == X(n, T0, night_idx).Not())
                    if not any((n, d2) in fixed_wanted_cells and fixed.get((n, d2)) not in (off_idx, night_idx, None) for d2 in (T0, T0 + 1)):
                        if _3n_rem >= 2:
                            m.Add(
                                X(n, T0, off_idx) + X(n, T0 + 1, off_idx) == 2
                            ).OnlyEnforceIf([end_prev_block])
                        else:
                            m.Add(
                                X(n, T0, off_idx) + X(n, T0 + 1, off_idx) >= 1
                            ).OnlyEnforceIf([end_prev_block])
                    print(f"{logger_prefix} [3N2OFF-cross] nurse_idx={n}, n_tail={n_tail}, "
                          f"offs_after={n_offs_after_3n}, rem={_3n_rem}")
                elif n_tail >= 3 and _3n_rem == 0:
                    print(f"{logger_prefix} [3N2OFF-cross] nurse_idx={n}, n_tail={n_tail}, "
                          f"offs_after={n_offs_after_3n} → 전월 내 2OFF 충족, 현월 강제 OFF 스킵")
                if n_tail >= 2 and n_offs_after_3n < 2 and (T0 + 2) <= T1:
                    if not any((n, d2) in fixed_wanted_cells and fixed.get((n, d2)) not in (off_idx, night_idx, None) for d2 in (T0 + 1, T0 + 2)):
                        m.Add(
                            X(n, T0 + 1, off_idx) + X(n, T0 + 2, off_idx) == 2
                        ).OnlyEnforceIf([X(n, T0, night_idx)])
                if n_tail == 1 and n_offs_after_3n < 2 and (T0 + 3) <= T1:
                    if not any((n, d2) in fixed_wanted_cells and fixed.get((n, d2)) not in (off_idx, night_idx, None) for d2 in (T0 + 2, T0 + 3)):
                        m.Add(
                            X(n, T0 + 2, off_idx) + X(n, T0 + 3, off_idx) == 2
                        ).OnlyEnforceIf([X(n, T0, night_idx), X(n, T0 + 1, night_idx)])
                for d in range(T0 + 2, T1 - 1):
                    if any((n, d2) in fixed_wanted_cells and fixed.get((n, d2)) not in (off_idx, night_idx, None) for d2 in (d + 1, d + 2)):
                        # 회복 OFF 슬롯에 non-OFF fixed_wanted → 3N 블록 자체를 금지
                        m.Add(
                            X(n, d, night_idx) + X(n, d - 1, night_idx) + X(n, d - 2, night_idx) <= 2
                        )
                        continue
                    xn0 = X(n, d, night_idx)
                    xn1 = X(n, d - 1, night_idx)
                    xn2 = X(n, d - 2, night_idx)
                    m.Add(
                        X(n, d + 1, off_idx) + X(n, d + 2, off_idx) == 2
                    ).OnlyEnforceIf([xn0, xn1, xn2])
        if cfg.two_offs_after_two_nig:
            _n_offs_after_map = getattr(roster_system, "prev_month_n_offs_after_by_idx", {}) or {}
            for n in range(N):
                if _is_preceptee_at(n):
                    continue
                T0, T1 = join[n], leave[n]
                _blocked_2n = blocked_by_nurse.get(n, set()) if blocked_by_nurse else set()
                n_tail = prev_month_n_tail_by_idx.get(n, 0)
                n_offs_after = _n_offs_after_map.get(n, 0)
                # 전월 N tail 뒤 이미 소비된 OFF 수를 반영
                _2n_rem = max(0, 2 - n_offs_after) if n_tail >= 2 else 2
                if n_tail >= 2 and _2n_rem > 0 and (T0 + 1) <= T1 and T0 not in _blocked_2n and (T0 + 1) not in _blocked_2n:
                    end_prev_block = m.NewBoolVar(f"end_2n_prev_soft_{n}")
                    m.Add(end_prev_block == X(n, T0, night_idx).Not())
                    if not any((n, d2) in fixed_wanted_cells and fixed.get((n, d2)) not in (off_idx, night_idx, None) for d2 in (T0, T0 + 1)):
                        if _2n_rem >= 2:
                            m.Add(
                                X(n, T0, off_idx) + X(n, T0 + 1, off_idx) == 2
                            ).OnlyEnforceIf([end_prev_block])
                        else:
                            # _2n_rem == 1: 1개만 추가 필요
                            m.Add(
                                X(n, T0, off_idx) + X(n, T0 + 1, off_idx) >= 1
                            ).OnlyEnforceIf([end_prev_block])
                    print(f"{logger_prefix} [2N2OFF-cross] nurse_idx={n}, n_tail={n_tail}, "
                          f"offs_after={n_offs_after}, rem={_2n_rem}")
                elif n_tail >= 2 and _2n_rem == 0:
                    print(f"{logger_prefix} [2N2OFF-cross] nurse_idx={n}, n_tail={n_tail}, "
                          f"offs_after={n_offs_after} → 전월 내 2OFF 충족, 현월 강제 OFF 스킵")
                if n_tail >= 1 and n_offs_after < 2 and (T0 + 2) <= T1:
                    end_block_b0 = m.NewBoolVar(f"end_2n_soft_b0_{n}")
                    m.Add(end_block_b0 == X(n, T0 + 1, night_idx).Not())
                    if not any((n, d2) in fixed_wanted_cells and fixed.get((n, d2)) not in (off_idx, night_idx, None) for d2 in (T0 + 1, T0 + 2)):
                        m.Add(
                            X(n, T0 + 1, off_idx) + X(n, T0 + 2, off_idx) == 2
                        ).OnlyEnforceIf([X(n, T0, night_idx), end_block_b0])
                for d in range(T0 + 1, T1 - 1):
                    if any((n, d2) in fixed_wanted_cells and fixed.get((n, d2)) not in (off_idx, night_idx, None) for d2 in (d + 1, d + 2)):
                        # 회복 OFF 슬롯에 non-OFF fixed_wanted → 이 위치에서 2N 블록 종료 금지
                        xn_prev_fw = X(n, d - 1, night_idx)
                        xn_curr_fw = X(n, d, night_idx)
                        end_block_fw = m.NewBoolVar(f"end_2n_fw_{n}_{d}")
                        m.Add(end_block_fw == X(n, d + 1, night_idx).Not())
                        m.Add(xn_prev_fw + xn_curr_fw + end_block_fw <= 2)
                        continue
                    xn_prev = X(n, d - 1, night_idx)
                    xn_curr = X(n, d, night_idx)
                    xn_next = X(n, d + 1, night_idx)
                    end_block = m.NewBoolVar(f"end_2n_hard_{n}_{d}")
                    m.Add(end_block == xn_next.Not())
                    m.Add(
                        X(n, d + 1, off_idx) + X(n, d + 2, off_idx) == 2
                    ).OnlyEnforceIf([xn_prev, xn_curr, end_block])

        # 금지 패턴 N-O-D/E
        if getattr(cfg, "nod_noe", True):
            for n in range(N):
                if _is_preceptee_at(n):
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
        # max coverage 설정 시: min/max coverage 기반 OFF cap 자동 조정
        # off_first=True: max coverage 미설정이라도 동일한 자동 조정 수행 (단일 cap 소스)
        _fb_auto_min_off = None
        _fb_auto_max_off = None
        if _fb_has_any_max or _fb_off_first_cfg:
            import math as _math
            _fb_blocked_set = set(blocked_by_nurse.keys()) if blocked_by_nurse else set()
            _fb_total_capacity = 0
            _fb_total_required = 0
            for _dd in range(D_phys):
                _day_min_sum = 0
                _day_max_sum = 0
                if hasattr(cfg, "daily_shift_requirements_by_day") and isinstance(cfg.daily_shift_requirements_by_day, list) and _dd < len(cfg.daily_shift_requirements_by_day):
                    _day_min_sum = sum(int(v or 0) for v in cfg.daily_shift_requirements_by_day[_dd].values())
                elif hasattr(cfg, "daily_shift_requirements") and isinstance(cfg.daily_shift_requirements, dict):
                    _day_min_sum = sum(int(v or 0) for v in cfg.daily_shift_requirements.values())
                if isinstance(_fb_max_by_day, list) and _dd < len(_fb_max_by_day) and isinstance(_fb_max_by_day[_dd], dict):
                    _day_min_map = {}
                    if hasattr(cfg, "daily_shift_requirements_by_day") and isinstance(cfg.daily_shift_requirements_by_day, list) and _dd < len(cfg.daily_shift_requirements_by_day):
                        _day_min_map = cfg.daily_shift_requirements_by_day[_dd]
                    elif hasattr(cfg, "daily_shift_requirements") and isinstance(cfg.daily_shift_requirements, dict):
                        _day_min_map = cfg.daily_shift_requirements
                    _all_codes = set(list(_fb_max_by_day[_dd].keys()) + (list(_day_min_map.keys()) if isinstance(_day_min_map, dict) else []))
                    for _code in _all_codes:
                        if _code == 'O':
                            continue
                        _mv = int((_fb_max_by_day[_dd].get(_code) or 0))
                        _minv = int((_day_min_map.get(_code) or 0) if isinstance(_day_min_map, dict) else 0)
                        if _mv > 0 and _mv >= _minv:
                            _day_max_sum += _mv
                        else:
                            _day_max_sum += _minv
                _day_active = sum(
                    1 for nn in range(N)
                    if join[nn] <= _dd <= leave[nn]
                    and _dd not in (blocked_by_nurse.get(nn, set()) if blocked_by_nurse else set())
                )
                _fb_total_capacity += max(0, _day_active - _day_min_sum)
                if _day_max_sum > 0:
                    _fb_total_required += max(0, _day_active - _day_max_sum)
            _fb_n_full = max(1, sum(1 for nn in range(N) if nn not in _fb_blocked_set))
            _fb_auto_min_off = max(1, int(_math.ceil(_fb_total_required / _fb_n_full)))
            _fb_auto_max_off = max(_fb_auto_min_off, int(_fb_total_capacity / _fb_n_full))
            # 2N2O/3N2O 하드 제약 활성 시 회복 OFF 여유 확보
            if getattr(cfg, "two_offs_after_two_nig", False) or getattr(cfg, "two_offs_after_three_nig", False):
                _fb_auto_max_off += 2
            if _fb_total_required > _fb_total_capacity:
                print(
                    f"{logger_prefix} [OffCap][MaxCov] required({_fb_total_required}) > capacity({_fb_total_capacity})"
                    f" → auto 조정 비활성화"
                )
                _fb_auto_min_off = None
                _fb_auto_max_off = None
            print(
                f"{logger_prefix} [OffCap][MaxCov] 자동 조정: capacity={_fb_total_capacity}, "
                f"required={_fb_total_required}, N={_fb_n_full}, "
                f"auto_min={_fb_auto_min_off}, auto_max={_fb_auto_max_off}"
            )
        # N 전일 금지 간호사 집합 (2N2O/3N2O OFF 확장용)
        from services.cp_sat.objective_terms import _n_forbid_n_set
        _fb_n_forbid = _n_forbid_n_set(roster_system, join, leave)
        try:
            # 개인별 O 정량 할당(나이트 전담 제외, 주휴 제외한 순수 O 목표)
            if off_idx is not None and effective_off_days > 0:
                for n in range(N):
                    if _is_preceptee_at(n):
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
                        1 for d in iter_nurse_days(n, join, leave, blocked_by_nurse) if (n, d) in vacation_off_cells
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
                    _n_blocked_t = len(blocked_by_nurse.get(n, set())) if blocked_by_nurse else 0
                    off_bounds_for_target = compute_off_bounds(
                        source=cfg,
                        avail_days=(leave[n] - join[n] + 1 - _n_blocked_t),
                        vacation_cnt=vacation_cnt,
                        reference_days=D_phys,
                        weekend_only=bool(getattr(nu, "is_weekend_off", False)),
                        weekend_slots_nonvac=weekend_forced,
                    )
                    min_target = int(off_bounds_for_target["min_off_required"])
                    max_target = int(off_bounds_for_target["max_off_allowed"])
                    # blocked days 비례로 effective_off_days 조정
                    _eff_off = effective_off_days
                    if _n_blocked_t > 0:
                        _ratio = max(0, leave[n] - join[n] + 1 - _n_blocked_t) / max(1, D_phys)
                        _eff_off = max(0, round(effective_off_days * _ratio))
                    raw_target_o = max(0, _eff_off - (weekly_target + weekend_forced))
                    target_o = min(max(raw_target_o, min_target), max_target)
                    if target_o <= 0:
                        continue
                    # 휴가/공가는 개인 O 목표 충족에서 제외
                    assigned_o = sum(
                        X(n, d, off_idx)
                        for d in iter_nurse_days(n, join, leave, blocked_by_nurse)
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
                if _is_preceptee_at(n):
                    continue
                T0, T1 = join[n], leave[n]
                nu = roster_system.nurses[n]
                raw = getattr(nu, "is_night_nurse", None)
                is_n_only = is_n_only_profile(raw, use_mid=bool(getattr(cfg, "use_mid", False)))
                nurse_name = getattr(nu, "name", "?")
                nurse_id = getattr(nu, "nurse_id", "?")
                is_weekend_off = bool(getattr(nu, "is_weekend_off", False))

                _n_blocked = len(blocked_by_nurse.get(n, set())) if blocked_by_nurse else 0
                avail_days = T1 - T0 + 1 - _n_blocked
                vacation_cnt = sum(
                    1 for d in range(T0, T1 + 1) if (n, d) in vacation_off_cells and (n, d) in active_days
                )
                structural_cnt = sum(
                    1
                    for d in range(T0, T1 + 1)
                    if (n, d) in structural_off_cells and (n, d) not in vacation_off_cells and (n, d) in active_days
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
                # max coverage 자동 조정 적용
                # max coverage 기반 자동 조정: max_off만 제한, min_off는 기존 유지
                max_off_allowed_from_policy = int(off_bounds["max_off_allowed"])
                extra_allowed = int(off_bounds["max_extra_off_days"])
                # print(
                #     f"{logger_prefix} [OffCap][vac] n={n}, id={nurse_id}, name={nurse_name}, "
                #     f"base_min_off={base_min_off}, avail_days={avail_days}, "
                #     f"vacation={vacation_cnt}, min_off_required={min_off_required}"
                # )
                # N전담 예외: offcap 고정값 적용 제외 (max는 avail_days-15 공식)
                if is_n_only:
                    min_off_required = 0
                # off_first=True: 사용자 명세상 월 OFF 수(off_days) 무시 → min_off HARD 해제.
                if bool(getattr(cfg, "off_first", False)):
                    min_off_required = 0
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
                        # 글로벌 +relax_level 제거 — per-nurse cap_slack 으로 대체
                        total_cap_effective = max(0, avail_days - 15)
                    else:
                        # 2N2O/3N2O 하드 제약으로 인한 추가 OFF를 OffCap에 반영 (미반영 시 INFEASIBLE)
                        _extra_off_fb = 0
                        if n not in _fb_n_forbid and (
                            getattr(cfg, "two_offs_after_two_nig", False)
                            or getattr(cfg, "two_offs_after_three_nig", False)
                        ):
                            _extra_off_fb += 2
                        if is_weekend_off:
                            _ow_data_fb = (getattr(roster_system, "off_window_constraints", {}) or {}).get(n, []) or []
                            for (_ws, _we) in _ow_data_fb:
                                _wl = max(T0, _ws)
                                _wr = min(T1, _we)
                                if _wl <= _wr and not any(_d in weekend_days for _d in range(_wl, _wr + 1)):
                                    _extra_off_fb += 1
                        # 4O 월경계 제약으로 월초 OFF 배치 제한된 간호사는 max_off +1 보정
                        if n in _4o_cross_affected_fb:
                            _extra_off_fb += 1
                        # off_first 분기: False=근무 oversupply(OFF tight) / True=OFF oversupply(dev HEAD)
                        _off_first_fb = bool(getattr(cfg, "off_first", False))
                        if _off_first_fb:
                            base_cap = max_off_allowed_from_policy
                            base_cap += _extra_off_fb
                            if n in per_nurse_off_cap_override:
                                base_cap = max(base_cap, per_nurse_off_cap_override[n])
                            # 글로벌 +relax_level 제거 — per-nurse cap_slack 으로 대체
                            total_cap_effective = min(base_cap, avail_days)
                            # max coverage 자동 조정: max_off cap
                            if _fb_auto_max_off is not None:
                                import math as _math
                                _ratio_fb = nonvac_active_days / max(1, D_phys)
                                _scaled_max_fb = max(min_off_required, int(_fb_auto_max_off * _ratio_fb))
                                total_cap_effective = min(total_cap_effective, _scaled_max_fb)
                        else:
                            # off_first=False: OFF tight clamp (min_off_required + HARD recovery buffer only)
                            base_cap = min_off_required + _extra_off_fb
                            if n in per_nurse_off_cap_override:
                                base_cap = max(base_cap, per_nurse_off_cap_override[n])
                            # 글로벌 +relax_level 제거 — per-nurse cap_slack 으로 대체
                            total_cap_effective = min(base_cap, avail_days)
                            total_cap_effective = max(total_cap_effective, min_off_required)
                    # OFF cap slack 결정 정책:
                    # 1) cfg gate (off_cap_bounded_slack_enable=True) → 기존 cfg max/weight 사용
                    # 2) gate False AND relax_level > 0 → per-nurse fallback 슬랙
                    #    (글로벌 +relax_level 폐지 후 위반 nurse 만 cap 풀어주는 구조)
                    # 3) gate False AND relax_level == 0 → cap hard (slack 없음)
                    if off_cap_bounded_slack_enable and off_cap_bounded_slack_max > 0:
                        _slack_max = int(off_cap_bounded_slack_max)
                        _slack_weight = int(off_cap_bounded_slack_weight)
                    elif int(relax_level or 0) > 0:
                        _slack_max = int(relax_level)
                        _slack_weight = 100000
                    else:
                        _slack_max = 0
                        _slack_weight = 0
                    if _slack_max > 0:
                        cap_slack = m.NewIntVar(0, _slack_max, f"off_cap_slack_{n}")
                        weighted = m.NewIntVar(
                            0,
                            _slack_max * _slack_weight,
                            f"off_cap_slack_weighted_{n}",
                        )
                        m.Add(weighted == cap_slack * _slack_weight)
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

        # team_min hard 제약 — Stage 1, 2 에도 등록한다.
        # Stage 3 는 build_fallback_stage3_objective_terms 가 자체 호출하므로 중복 회피.
        # 누락 시 Stage 3 INFEASIBLE → Stage 2 해 commit 경로에서 team_min 위반이 새어나감.
        if stage in (1, 2):
            try:
                _gs_tm = str(getattr(roster_system, "grade_strategy", "BASE") or "BASE").upper()
                add_team_min_constraints(
                    m, roster_system, X, join, leave,
                    grade_strategy=_gs_tm,
                    blocked_by_nurse=blocked_by_nurse,
                )
            except Exception as e:
                print(f"{logger_prefix} [Stage{stage}] team_min hard 등록 실패: {e}")

        # Grade hard 제약은 fallback 모든 stage에서 동일하게 유지되어야 한다.
        # (기존에는 stage3 objective 경로에서만 add_grade_constraints가 호출되어,
        #  stage3 infeasible 시 stage2/1 해로 내려가며 grade hard가 빠질 수 있었다.)
        try:
            _gs_fb = str(getattr(roster_system, "grade_strategy", "BASE") or "BASE").upper()
            _gc_fb = getattr(roster_system, "grade_config", None)
            _allow_soft_fb = True
            if isinstance(_gc_fb, dict):
                _allow_soft_fb = bool(_gc_fb.get("allow_soft_fallback", False))
            if isinstance(_gc_fb, dict) and not _allow_soft_fb:
                add_grade_constraints_fn(
                    m=m,
                    rs=roster_system,
                    X=X,
                    join=join,
                    leave=leave,
                    grade_strategy=_gs_fb,
                    grade_config=_gc_fb,
                )
        except Exception as _grade_hard_exc:
            print(f"{logger_prefix} [GradeHard] fallback stage 공통 제약 추가 실패: {_grade_hard_exc}")

        # nurse-level 월간 D/E/N/O 한도 hard (모든 stage 공통).
        # primary cp_sat_basic이 INFEASIBLE 되어 fallback 진입 시 사용자 입력 한도가
        # 무시되던 회귀 fix. 같은 모듈을 primary와 공유하여 동작 일치성 확보.
        try:
            from services.constraints.monthly_limit_constraints import (
                add_monthly_limit_constraints,
            )
            _ml_added = add_monthly_limit_constraints(m, roster_system, X, join, leave)
            if _ml_added:
                print(f"{logger_prefix} [MonthlyLimit][stage{stage}] {_ml_added}건 hard 제약 추가")
        except Exception as _ml_exc:
            print(f"{logger_prefix} [MonthlyLimit][stage{stage}] 제약 추가 실패(무시): {_ml_exc}")

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
                for d in iter_nurse_days(n, join, leave, blocked_by_nurse)
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
                blocked_by_nurse=blocked_by_nurse,
            )
            # 일자별 커버리지 균등화 (min~max 범위 내 고른 배정)
            if _fb_has_any_max and _fb_daily_assigned_by_code:
                for _eq_code, _eq_entries in _fb_daily_assigned_by_code.items():
                    if len(_eq_entries) < 2:
                        continue
                    _eq_vars = []
                    for _eq_d, _eq_assigned, _eq_need in _eq_entries:
                        _eq_v = m.NewIntVar(0, N, f"fb_dcov_{_eq_code}_{_eq_d}")
                        m.Add(_eq_v == _eq_assigned)
                        _eq_vars.append(_eq_v)
                        # min 초과분 패널티: min에 가깝게 유도
                        if _eq_need > 0:
                            _eq_excess = m.NewIntVar(0, N, f"fb_dcov_excess_{_eq_code}_{_eq_d}")
                            m.Add(_eq_excess >= _eq_v - _eq_need)
                            obj.append(-80 * _eq_excess)
                    # 글로벌 range: 블록 쏠림 방지
                    _eq_max = m.NewIntVar(0, N, f"fb_dcov_max_{_eq_code}")
                    _eq_min = m.NewIntVar(0, N, f"fb_dcov_min_{_eq_code}")
                    m.AddMaxEquality(_eq_max, _eq_vars)
                    m.AddMinEquality(_eq_min, _eq_vars)
                    _eq_range = m.NewIntVar(0, N, f"fb_dcov_range_{_eq_code}")
                    m.Add(_eq_range == _eq_max - _eq_min)
                    obj.append(-150 * _eq_range)
                    # 인접일 평활화: 급변 방지
                    for _i in range(1, len(_eq_vars)):
                        _adj = m.NewIntVar(0, N, f"fb_dcov_adj_{_eq_code}_{_i}")
                        m.Add(_adj >= _eq_vars[_i] - _eq_vars[_i - 1])
                        m.Add(_adj >= _eq_vars[_i - 1] - _eq_vars[_i])
                        obj.append(-60 * _adj)
            if _fb_max_cov_off_equalize_terms:
                obj.extend(_fb_max_cov_off_equalize_terms)
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
                    _relax_desc = f"OFF상한+{relax_level}"
                    if relax_level >= 3:
                        _relax_desc += ", max coverage soft, M min soft"
                    print(
                        f"{logger_prefix} 폴백1 성공: 완화레벨 {relax_level} 적용 ({_relax_desc})"
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
                for d in iter_nurse_days(n, join, leave, blocked_by_nurse):
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
            for d in iter_nurse_days(n, join, leave, blocked_by_nurse):
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
                for d in iter_nurse_days(n, join, leave, blocked_by_nurse):
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
                        int(s3.Value(X3(n, d, off_idx))) for d in iter_nurse_days(n, join, leave, blocked_by_nurse)
                    )
                    vac_cnt = sum(
                        1
                        for d in iter_nurse_days(n, join, leave, blocked_by_nurse)
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
        for d in iter_nurse_days(n, join, leave, blocked_by_nurse):
            for s in range(S):
                if s3.Value(X3(n, d, s)):
                    roster_system.roster[n, d, s] = 1
    log_n_even_distribution(roster_system, logger_prefix, join=join, leave=leave)
    # NOTE: rebalance_off 후처리 비활성화(호출 무시)
    # try:
    #     print(f"{logger_prefix} [PostOff] 시작: 최종 stage3 해 기반 후처리 시도")
    #     before_viol = len(roster_system._find_violations())
    #     postprocess_rebalance_off_fn(roster_system)
    #     after_viol = len(roster_system._find_violations())
    #     print(
    #         f"{logger_prefix} [PostOff] 종료: viol {before_viol}->{after_viol} "
    #         f"(감소={before_viol - after_viol})"
    #     )
    # except Exception as exc:
    #     print(f"{logger_prefix} [PostOff] 후처리 실패: {exc}")

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
    # ── 후처리 완료 후 최종 커버리지 상태 로깅 ──
    try:
        final_viols = roster_system._find_violations()
        final_cov_viols = [v for v in final_viols if v.get('type') == 'shift_requirement']
        if final_cov_viols:
            print(f"{logger_prefix} [최종 커버리지 부족] 후처리 후 {len(final_cov_viols)}건 부족:")
            for v in sorted(final_cov_viols, key=lambda x: (x['day'], x['shift'])):
                print(
                    f"  day={v['day']+1}, shift={v['shift']}, "
                    f"required={v['required']}, actual={v['actual']}, "
                    f"gap={v['required'] - v['actual']}"
                )
        else:
            print(f"{logger_prefix} [최종 커버리지] 후처리 후 커버리지 부족 없음 ✓")
    except Exception as exc:
        print(f"{logger_prefix} [최종 커버리지 로깅 실패]: {exc}")
    print(f"{logger_prefix} 폴백 완료: 커버리지부족={best_short}, 안전위반합={best_safe_sum}")
    return best_short == 0 and best_safe_sum == 0
