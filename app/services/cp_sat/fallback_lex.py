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
    effective_night_cap,
    is_n_only_profile,
    normalize_allowed_shift_codes,
)
from services.cp_sat.fallback_objectives import build_fallback_stage3_objective_terms
from services.cp_sat.m_coverage import compute_main_bucket_indices
from services.cp_sat.night_distribution_log import log_n_even_distribution
from services.constraints.team_constraints import add_team_min_constraints
from services.day_windows import iter_nurse_days, build_active_days

# lex Stage 2 에서 team_min 커버 부족(slack) 1건당 패널티. safety(off-quota 10~30만) 위에
# 두어 팀 커버를 상위 co-priority 로 만든다(단, 커버리지/안전은 이미 상위 stage 에서 고정).
TEAM_COVER_LEX_WEIGHT = 300_000


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
    import os as _os_tl3
    _tl3_override = int(_os_tl3.environ.get("AIDE_FB_TL3", 0) or 0)
    if _tl3_override > 0:
        tl3 = _tl3_override

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
    fixed_source_by_cell: dict[tuple[int, int], str] = {}
    fixed_wanted_cells: set[tuple[int, int]] = set()

    def _normalize_fixed_source(
        raw_source: object,
        shift_type: Optional[str],
        shift_main: str,
    ) -> str:
        src = str(raw_source or "").strip().lower()
        if src:
            if src == "fixed_wanted":
                return "fixed_wanted"
            if src == "2n2off_recovery":
                return "recovery_2n2off"
            if src == "3n2off_recovery":
                return "recovery_3n2off"
            if src == "recovery_off":
                return "recovery_off"
            if src in {"weekly_off", "weekoff", "weekly_off_fixed"}:
                return "weekly_off"
            if src in {"special_fixed", "special", "vacation", "leave"}:
                return "special"
            return src
        st = str(shift_type or "").strip()
        if st == "주휴":
            return "weekly_off"
        if st in {"휴가", "공가", "휴무"}:
            return "special"
        if shift_main == "O":
            return "off_fixed"
        return "manual"

    def _fixed_pattern_from_source(source: str) -> str:
        return f"fixed_assignment:{source or 'manual'}"

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
        fixed_source_by_cell[(n, d)] = _normalize_fixed_source(
            c.get("fixed_source"),
            fixed_type_by_cell[(n, d)],
            s_main,
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

    # ── 프리셉티 인덱스/기간 (cp_sat_basic 와 동일 정책 — nurse_preceptee_period SSOT) ──
    # 맵 있음=권위 모드(맵만 신뢰, default=follow 없음), 맵 없음=전환 폴백(캐시 기반 무회귀).
    # 설계: docs/NURSE_PRECEPTEE_PERIOD_DESIGN.md §6.
    preceptee_follow = bool(getattr(cfg, 'preceptee_on', False))
    _fb_id_to_idx = {nu.db_id: n for n, nu in enumerate(roster_system.nurses)}
    preceptee_follow_days: dict[int, set[int]] = getattr(roster_system, "preceptee_follow_days", {}) or {}
    _has_preceptee_period = bool(getattr(roster_system, "preceptee_period_authoritative", False))
    if _has_preceptee_period:
        preceptee_indices: set[int] = {n for n, days in preceptee_follow_days.items() if days}
    else:
        preceptee_indices = {n for n, nu in enumerate(roster_system.nurses) if getattr(nu, 'preceptor_id', None)}
    if preceptee_indices:
        print(f"{logger_prefix} [Fallback] 프리셉티 인덱스: {len(preceptee_indices)}명 "
              f"(follow={preceptee_follow}, period_map={_has_preceptee_period})")

    def _is_preceptee_at(n: int, d: int = -1) -> bool:
        """(n, d)가 프리셉티 follow 대상인지. 맵 없으면 전체월 follow(폴백), 맵 있으면 day별."""
        if not preceptee_follow or n not in preceptee_indices:
            return False
        if not _has_preceptee_period:
            return True  # 폴백(맵 없음): 전체월 follow
        days = preceptee_follow_days.get(n)
        if not days:
            return False  # 그 달 프리셉티 아님(종료/미겹침)
        if d < 0:
            return False  # nurse-level: day별 판별 필요
        return d in days

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
            raw = getattr(nu, "allowed_shifts", None)
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
        broad_soft: bool = False,
    ):
        m = cp_model.CpModel()
        # per-nurse OFF cap slack 추적: post-solve 시 어느 nurse 가 슬랙을 실제로
        # 사용했는지 로그하기 위함.
        m._off_slack_lower_by_n = {}  # type: ignore[attr-defined]
        m._off_slack_upper_by_n = {}  # type: ignore[attr-defined]
        # HardAssumptionRegistry — MUS (UNSAT core) 추출 인프라.
        # 모든 hard 식을 BoolVar + OnlyEnforceIf(lit) 로 reify 하므로 모델 사이즈와
        # search branching 이 늘어나 wall-time 비용이 상당하다. INFEASIBLE 케이스가
        # 드문 운영 환경에서는 비용 대비 효용이 낮아 **기본 OFF**.
        # MUS 추출이 필요하면 `AIDE_ENABLE_MUS_REGISTRY=1` 로 명시 활성화.
        _add_hard_fb = None
        _assume_registry_fb = None
        try:
            import os as _os_fb
            if _os_fb.environ.get("AIDE_ENABLE_MUS_REGISTRY") == "1":
                from services.cp_sat.hard_assumption import HardAssumptionRegistry, add_hard as _add_hard_fb
                _assume_registry_fb = HardAssumptionRegistry(m)
                m._cpsat_assumption_registry = _assume_registry_fb  # type: ignore[attr-defined]
                print(f"[FallbackLex] AIDE_ENABLE_MUS_REGISTRY=1 — registry wrapping ON (stage={stage})")
        except Exception as _ar_fb_exc:
            print(f"[FallbackLex] HardAssumptionRegistry init failed (ignore): {_ar_fb_exc}")
            _assume_registry_fb = None
        soft_coverage = bool(getattr(cfg, "soften_daily_coverage", False))
        coverage_soft_slack = int(getattr(cfg, "coverage_soft_slack", 0) or 0)
        # 정책 (2026-05-13, HanJongjun): max coverage / M min 은 어떤 단계든 HARD 유지.
        # 무너지면 환자 안전 영향 큼 → relax 시에는 다른 hard(team/grade) 를 먼저 풀어야 함.
        # ralph 의 broad_soft 트리거는 폐기 (사용자 정책 결정 2026-05-17).
        _relax_coverage = False
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
        if stage == 1 and not broad_soft:
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
                    raw = getattr(nu, "allowed_shifts", None)
                    is_n_only = is_n_only_profile(raw, use_mid=bool(getattr(cfg, "use_mid", False)))
                    # 디버그: 강제 OFF 개수 로그
                    print(
                        f"{logger_prefix} [HardCheck][ForcedOffCnt] "
                        f"nurse_idx={n}, id={getattr(nu, 'nurse_id', '?')}, "
                        f"name={getattr(nu, 'name', '?')}, forced_off_cnt={forced_off_cnt}, "
                        f"avail_days={avail_days}"
                    )
                    if is_n_only:
                        # off-cap = avail - 실효 N상한(min(글로벌 max_night, n_max/n_exact)).
                        # 활성 cap(아래 total_cap_effective)과 동일 공식 유지.
                        _global_mn = int(getattr(cfg, "max_night_shifts_per_month", 15) or 15)
                        max_off_allowed = max(0, avail_days - effective_night_cap(nu, _global_mn))
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
                        max_off_allowed = int(off_bounds["max_off_allowed"])
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
            _fixed_source = fixed_source_by_cell.get((n, d), "manual")
            _fixed_pattern = _fixed_pattern_from_source(_fixed_source)
            if (n, d) not in active_days:
                continue
            # day-aware: 팔로우 day의 프리셉티 고정셀만 스킵(팔로우가 지배). primary(cp_sat_basic:3089)와 동일.
            # day-less 는 authoritative 에서 항상 False→고정셀이 hard로 적용돼 팔로우와 모순(INFEASIBLE) 유발.
            if _is_preceptee_at(n, d):
                continue
            _fixed_expr = (X(n, d, s_idx) == 1)
            if _assume_registry_fb is not None and _add_hard_fb is not None:
                _add_hard_fb(
                    m,
                    _assume_registry_fb,
                    name=f"FixedCell:nurse_{n}:day_{d}",
                    constraint_expr=_fixed_expr,
                    meta={
                        "node_id": f"fixed_cell:nurse_{n}:day_{d}",
                        "type": "FixedWantedNode",
                        "label": "fixed_assignment",
                        "value": {"day": d + 1, "shift_idx": int(s_idx)},
                        "fixed_source": _fixed_source,
                        "scope": "nurse",
                        "scope_key": f"nurse_{n}",
                        "pattern": _fixed_pattern,
                        "nurse_id": str(getattr(roster_system.nurses[n], "nurse_id", n)),
                        "human_message_ko": f"{d + 1}일 고정 근무를 유지해야 합니다.",
                        "resolution_hint": "해당 날짜 고정 근무를 해제하거나 충돌 정책을 완화하세요.",
                    },
                )
            else:
                m.Add(_fixed_expr)
            for s in range(S):
                if s != s_idx:
                    _ban_expr = (X(n, d, s) == 0)
                    if _assume_registry_fb is not None and _add_hard_fb is not None:
                        _add_hard_fb(
                            m,
                            _assume_registry_fb,
                            name=f"FixedCellBan:nurse_{n}:day_{d}",
                            constraint_expr=_ban_expr,
                            meta={
                                "node_id": f"fixed_cell_ban:nurse_{n}:day_{d}",
                                "type": "FixedWantedNode",
                                "label": "fixed_assignment_exclusive",
                                "value": {"day": d + 1, "fixed_shift_idx": int(s_idx)},
                                "fixed_source": _fixed_source,
                                "scope": "nurse",
                                "scope_key": f"nurse_{n}",
                                "pattern": _fixed_pattern,
                                "nurse_id": str(getattr(roster_system.nurses[n], "nurse_id", n)),
                                "human_message_ko": f"{d + 1}일 고정 근무와 다른 시프트는 금지됩니다.",
                                "resolution_hint": "고정 근무 또는 다른 하드 제약 중 하나를 조정하세요.",
                            },
                        )
                    else:
                        m.Add(_ban_expr)
        # W(특별 근무)는 고정 셀 외에는 전부 금지
        if has_w and w_idx is not None:
            for n in range(N):
                if _is_preceptee_at(n):
                    continue
                for d in iter_nurse_days(n, join, leave, blocked_by_nurse):
                    if (n, d) in fixed and fixed[(n, d)] == w_idx:
                        continue
                    _w_ban_expr = (X(n, d, w_idx) == 0)
                    if _assume_registry_fb is not None and _add_hard_fb is not None:
                        _add_hard_fb(
                            m,
                            _assume_registry_fb,
                            name=f"SpecialShiftBanW:nurse_{n}:day_{d}",
                            constraint_expr=_w_ban_expr,
                            meta={
                                "node_id": f"special_shift_ban_w:nurse_{n}:day_{d}",
                                "type": "ForbiddenCellNode",
                                "label": "ban_unfixed_w_shift",
                                "value": {"day": d + 1, "shift": "W"},
                                "scope": "nurse",
                                "scope_key": f"nurse_{n}",
                                "pattern": "forbidden_shift",
                                "nurse_id": str(getattr(roster_system.nurses[n], "nurse_id", n)),
                                "human_message_ko": "고정되지 않은 W(특별 근무)는 배정할 수 없습니다.",
                                "resolution_hint": "W 배정이 필요하면 해당 셀을 고정 근무로 지정하세요.",
                            },
                        )
                    else:
                        m.Add(_w_ban_expr)
        # 순수 O 4연속 금지 (fixed로 이미 4O면 경고만 남기고 스킵)
        # cfg.skip_4o_hard_first_days: 월초 N일 구간에서는 4O Hard 미적용 (기본 3)
        # cfg.enforce_4o_hard=False 또는 env ROSTER_DISABLE_4O_HARD=1 이면 4O hard 전체 비활성(테스트/완화).
        import os as _os_4o_env_fb
        _enforce_4o_hard_eff_fb = bool(getattr(cfg, "enforce_4o_hard", True))
        if _os_4o_env_fb.environ.get("ROSTER_DISABLE_4O_HARD"):
            _enforce_4o_hard_eff_fb = False
        if not _enforce_4o_hard_eff_fb:
            print(f"{logger_prefix} [4O-hard] DISABLED (cfg.enforce_4o_hard={getattr(cfg, 'enforce_4o_hard', True)}, env={_os_4o_env_fb.environ.get('ROSTER_DISABLE_4O_HARD')})")
        if off_idx is not None and _enforce_4o_hard_eff_fb:
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
            if not _enforce_4o_hard_eff_fb:
                break
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
                # MUS용 per-nurse 주말휴무 리터럴 — 강제 OFF/평일 OFF금지가 병목이면
                # core 에 WeekendOffNode 로 뜬다. 기본 true=동작 무변경.
                _assume_weekend_off_fb = None
                if _assume_registry_fb is not None:
                    _assume_weekend_off_fb = _assume_registry_fb.create_literal(
                        f"WeekendOffOnly:nurse_{n}",
                        meta={
                            "node_id": f"weekend_off_only:nurse_{n}",
                            "type": "WeekendOffNode", "label": "주말휴무 전용",
                            "value": True, "scope": "nurse", "scope_key": f"nurse_{n}",
                            "pattern": "weekend_off_only",
                            "nurse_id": str(getattr(nu, "nurse_id", n)),
                            "human_message_ko": "주말 OFF 강제 / 평일 OFF 금지(주말휴무 전용자)",
                            "resolution_hint": "주말휴무 전용(weekend_off_only_enable)을 끄거나 이 간호사의 주말휴무 속성을 해제하세요.",
                        },
                    )
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
                        if _assume_weekend_off_fb is not None:
                            m.Add(X(n, d, off_idx) == 1).OnlyEnforceIf(_assume_weekend_off_fb)
                        else:
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
                        if _assume_weekend_off_fb is not None:
                            m.Add(X(n, d, off_idx) == 0).OnlyEnforceIf(_assume_weekend_off_fb)
                        else:
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
                #                 allow_shortage_off = broad_soft

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
                        _if_expr = (X(n, d, s_idx) == 0)
                        if _assume_registry_fb is not None and _add_hard_fb is not None:
                            _add_hard_fb(
                                m,
                                _assume_registry_fb,
                                name=f"InitialForbidden:nurse_{n}:day_{d}",
                                constraint_expr=_if_expr,
                                meta={
                                    "node_id": f"initial_forbidden:nurse_{n}:day_{d}",
                                    "type": "ForbiddenCellNode",
                                    "label": "initial_forbidden_shift",
                                    "value": {"day": d + 1, "shift": code},
                                    "scope": "nurse",
                                    "scope_key": f"nurse_{n}",
                                    "pattern": "initial_forbidden",
                                    "nurse_id": str(getattr(roster_system.nurses[n], "nurse_id", n)),
                                    "human_message_ko": f"초기 금지 규칙에 의해 {d + 1}일 {code} 배정이 금지됩니다.",
                                    "resolution_hint": "초기 금지 규칙을 해제하거나 다른 하드 제약을 조정하세요.",
                                },
                            )
                        else:
                            m.Add(_if_expr)
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
            _fb_pre_ptr_idx = getattr(roster_system, 'preceptee_preceptor_idx', {}) or {}  # period SSOT
            # Option C: 프리셉티 1급 시민화 — 등가는 (a)프리셉티 fixed일 제외 (b)프리셉터 특수코드일엔 OFF
            _pte_std = {'D', 'E', 'N', 'O'} | ({'M'} if mid_idx is not None else set())
            _pte_orig_map = getattr(roster_system, '_fixed_original_shift_map', {}) or {}
            _pte_work_sub = {str(x).upper() for x in (getattr(roster_system, '_work_sub_ids', set()) or set())}
            _pte_fw_map = getattr(roster_system, '_preceptee_fixed_wanted_map', {}) or {}
            for n in sorted(preceptee_indices):
                nu = roster_system.nurses[n]
                # 권위 모드: period SSOT 로 프리셉터 결정(캐시 미사용 — NULL 캐시 프리셉티도 solve 반영).
                if _has_preceptee_period:
                    p = _fb_pre_ptr_idx.get(n)
                    if p is None:
                        continue
                else:
                    pid = getattr(nu, 'preceptor_id', None)
                    if not pid or pid not in _fb_id_map:
                        continue
                    p = _fb_id_map[pid]
                d_start = max(join[n], join[p])
                d_end = min(leave[n], leave[p])
                # MUS용 per-preceptee 동반(팔로우) 리터럴 — follow/특수OFF가 병목이면
                # core 에 PrecepteeFollowNode 로 뜬다. 기본 true=동작 무변경.
                _assume_preceptee_fb = None
                if _assume_registry_fb is not None:
                    _assume_preceptee_fb = _assume_registry_fb.create_literal(
                        f"PrecepteeFollow:nurse_{n}",
                        meta={
                            "node_id": f"preceptee_follow:nurse_{n}",
                            "type": "PrecepteeFollowNode", "label": "프리셉티 동반(팔로우)",
                            "value": True, "scope": "nurse", "scope_key": f"nurse_{n}",
                            "pattern": "preceptee_follow",
                            "nurse_id": str(getattr(nu, "nurse_id", n)),
                            "human_message_ko": "프리셉티가 프리셉터와 동일 근무를 서야 함(교육 동반)",
                            "resolution_hint": "프리셉티 동반(preceptee_follow/preceptee_on)을 해제하세요.",
                        },
                    )
                for d in range(d_start, d_end + 1):
                    if not _is_preceptee_at(n, d):
                        continue
                    # (a) 프리셉티 fixed일: 등가 제외(본인 고정값은 아래 하드고정 블록이 처리)
                    if (n, d) in _pte_fw_map:
                        continue
                    # (b) 프리셉터가 비표준 fixed코드(휴가/공가/W 등)면 등가 대신 프리셉티 OFF
                    _p_special = False
                    if (p, d) in fixed:
                        _p_orig = _pte_orig_map.get((p, d))
                        if _p_orig:
                            _pou = str(_p_orig).upper()
                            _p_special = (_pou not in _pte_std and _pou not in _pte_work_sub)
                        else:
                            _p_special = fixed[(p, d)] not in (day_idx, eve_idx, night_idx, off_idx, mid_idx)
                    if _p_special:
                        _xo = X(n, d, off_idx)
                        if not isinstance(_xo, int):
                            if _assume_preceptee_fb is not None:
                                m.Add(_xo == 1).OnlyEnforceIf(_assume_preceptee_fb)
                            else:
                                m.Add(_xo == 1)
                        continue
                    for s in range(S):
                        xn = X(n, d, s)
                        xp = X(p, d, s)
                        if isinstance(xn, int) or isinstance(xp, int):
                            continue
                        if _assume_preceptee_fb is not None:
                            m.Add(xn == xp).OnlyEnforceIf(_assume_preceptee_fb)
                        else:
                            m.Add(xn == xp)
            # Option C: 프리셉티 fixed_wanted 프리솔브 하드고정 → fixed일 인접을 솔버가 조율/불가보고
            for (_pte_n, _pte_d), _pte_code in _pte_fw_map.items():
                if _pte_n not in preceptee_indices:
                    continue
                if not (join[_pte_n] <= _pte_d <= leave[_pte_n]):
                    continue
                _cu = str(_pte_code).strip().upper()
                if _cu not in roster_system.config.shift_types:
                    continue
                _ci = roster_system.config.shift_types.index(_cu)
                _xv = X(_pte_n, _pte_d, _ci)
                if not isinstance(_xv, int):
                    if _assume_registry_fb is not None and _add_hard_fb is not None:
                        _add_hard_fb(
                            m, _assume_registry_fb,
                            name=f"PrecepteeFixedWanted:nurse_{_pte_n}:day_{_pte_d}",
                            constraint_expr=(_xv == 1),
                            meta={
                                "node_id": f"preceptee_fixed_wanted:nurse_{_pte_n}:day_{_pte_d}",
                                "type": "FixedWantedNode", "label": "프리셉티 고정 요청",
                                "value": {"day": _pte_d + 1, "code": str(_pte_code)},
                                "scope": "nurse", "scope_key": f"nurse_{_pte_n}",
                                "pattern": "preceptee_fixed_wanted",
                                "nurse_id": str(getattr(roster_system.nurses[_pte_n], "nurse_id", _pte_n)),
                                "human_message_ko": "프리셉티 고정 근무 요청을 유지해야 합니다.",
                                "resolution_hint": "해당 프리셉티 고정 요청을 해제하세요.",
                            },
                        )
                    else:
                        m.Add(_xv == 1)

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
                    _raw_nn = getattr(_nu, "allowed_shifts", None) if _nu is not None else None
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

        # 전이 위반: 정확한 reification (iff) — Option C: 프리셉티도 적용(등가로 프리셉터에 전파)
        for n in range(N):
            T0, T1 = join[n], leave[n]
            for d in range(T0 + 1, T1 + 1):
                xn = X(n, d - 1, night_idx)
                xd = X(n, d, day_idx)
                if getattr(cfg, "ban_n_to_d", True):
                    # fixed_cells로 N→D가 명시적으로 고정된 경우 제약 면제
                    if not (fixed.get((n, d-1)) == night_idx and fixed.get((n, d)) == day_idx):
                        _n2d_expr = (xn + xd <= 1)
                        if _assume_registry_fb is not None and _add_hard_fb is not None:
                            _add_hard_fb(
                                m,
                                _assume_registry_fb,
                                name=f"TransitionBanN2D:nurse_{n}:day_{d}",
                                constraint_expr=_n2d_expr,
                                meta={
                                    "node_id": f"transition_ban_n2d:nurse_{n}:day_{d}",
                                    "type": "TransitionBanNode",
                                    "label": "ban_n_to_d",
                                    "value": {"day": d + 1, "transition": "N->D"},
                                    "scope": "nurse",
                                    "scope_key": f"nurse_{n}",
                                    "pattern": "transition_ban",
                                    "nurse_id": str(getattr(roster_system.nurses[n], "nurse_id", n)),
                                    "human_message_ko": "N 다음날 D 전이는 금지됩니다.",
                                    "resolution_hint": "전이 금지 설정을 완화하거나 해당 날짜 고정을 조정하세요.",
                                },
                            )
                        else:
                            m.Add(_n2d_expr)
                if getattr(cfg, "ban_e_to_d", True):
                    xe = X(n, d - 1, eve_idx)
                    # fixed_cells로 E→D가 명시적으로 고정된 경우 제약 면제
                    if not (fixed.get((n, d-1)) == eve_idx and fixed.get((n, d)) == day_idx):
                        _e2d_expr = (xe + xd <= 1)
                        if _assume_registry_fb is not None and _add_hard_fb is not None:
                            _add_hard_fb(
                                m,
                                _assume_registry_fb,
                                name=f"TransitionBanE2D:nurse_{n}:day_{d}",
                                constraint_expr=_e2d_expr,
                                meta={
                                    "node_id": f"transition_ban_e2d:nurse_{n}:day_{d}",
                                    "type": "TransitionBanNode",
                                    "label": "ban_e_to_d",
                                    "value": {"day": d + 1, "transition": "E->D"},
                                    "scope": "nurse",
                                    "scope_key": f"nurse_{n}",
                                    "pattern": "transition_ban",
                                    "nurse_id": str(getattr(roster_system.nurses[n], "nurse_id", n)),
                                    "human_message_ko": "E 다음날 D 전이는 금지됩니다.",
                                    "resolution_hint": "전이 금지 설정을 완화하거나 해당 날짜 고정을 조정하세요.",
                                },
                            )
                        else:
                            m.Add(_e2d_expr)
                if getattr(cfg, "ban_n_to_e", True):
                    xe2 = X(n, d, eve_idx)
                    # fixed_cells로 N→E가 명시적으로 고정된 경우 제약 면제
                    if not (fixed.get((n, d-1)) == night_idx and fixed.get((n, d)) == eve_idx):
                        _n2e_expr = (xn + xe2 <= 1)
                        if _assume_registry_fb is not None and _add_hard_fb is not None:
                            _add_hard_fb(
                                m,
                                _assume_registry_fb,
                                name=f"TransitionBanN2E:nurse_{n}:day_{d}",
                                constraint_expr=_n2e_expr,
                                meta={
                                    "node_id": f"transition_ban_n2e:nurse_{n}:day_{d}",
                                    "type": "TransitionBanNode",
                                    "label": "ban_n_to_e",
                                    "value": {"day": d + 1, "transition": "N->E"},
                                    "scope": "nurse",
                                    "scope_key": f"nurse_{n}",
                                    "pattern": "transition_ban",
                                    "nurse_id": str(getattr(roster_system.nurses[n], "nurse_id", n)),
                                    "human_message_ko": "N 다음날 E 전이는 금지됩니다.",
                                    "resolution_hint": "전이 금지 설정을 완화하거나 해당 날짜 고정을 조정하세요.",
                                },
                            )
                        else:
                            m.Add(_n2e_expr)
                if mid_idx is not None:
                    m.Add(X(n, d, mid_idx) <= X(n, d - 1, day_idx) + X(n, d - 1, off_idx))
                # if getattr(cfg, "ban_d_to_n", True):
                #     xd_prev = X(n, d - 1, day_idx)
                #     m.Add(xd_prev + xn <= 1)

        # 1N 금지 (day0 N 고정인 경우 해당일만 스킵)
        # nurse_monthly_limit.n_max==1 nurse 는 사용자 명시 의도 우선으로 면제.
        not_one_night_val = getattr(cfg, "not_one_night", False)
        print(f"{logger_prefix} [1N금지] not_one_night={not_one_night_val!r} (type={type(not_one_night_val).__name__})")
        try:
            from services.constraints.monthly_limit_constraints import (
                collect_single_n_allowed_nurse_indices,
            )
            _single_n_allowed_lex = collect_single_n_allowed_nurse_indices(roster_system)
        except Exception as _e_sn:
            print(f"{logger_prefix} [1N금지] single_n allowed set 계산 실패(무시): {_e_sn}")
            _single_n_allowed_lex = set()
        if bool(not_one_night_val):
            _pte_fw_1n = getattr(roster_system, '_preceptee_fixed_wanted_map', {}) or {}
            for n in range(N):
                if n in _single_n_allowed_lex:
                    continue
                # MUS용 per-nurse 1N 금지 리터럴 (primary _assume_no1n 대응)
                _assume_no1n_fb = None
                if _assume_registry_fb is not None:
                    _assume_no1n_fb = _assume_registry_fb.create_literal(
                        f"NotOneNight:nurse_{n}",
                        meta={
                            "node_id": f"not_one_night:nurse_{n}",
                            "type": "NotOneNightNode", "label": "1N 단독 금지",
                            "value": True, "scope": "nurse", "scope_key": f"nurse_{n}",
                            "pattern": "not_one_night",
                            "nurse_id": str(getattr(roster_system.nurses[n], "nurse_id", n)),
                            "human_message_ko": "야간(N) 단독 박힘 금지 (인접일 중 ≥1일 N 필요)",
                            "resolution_hint": "이 간호사의 n_max 한도를 1로 설정하면 1N 금지에서 면제됩니다.",
                        },
                    )
                T0, T1 = join[n], leave[n]
                for d in range(T0, T1 + 1):
                    # 프리셉티 fixed셀은 1N 강제 제외(사용자: 고정 단독N 존중, 복사된 단독N만 방지)
                    if (n, d) in _pte_fw_1n:
                        continue
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
                    if _assume_no1n_fb is not None:
                        m.Add(X(n, d, night_idx) <= sum(neighbors)).OnlyEnforceIf(_assume_no1n_fb)
                    else:
                        m.Add(X(n, d, night_idx) <= sum(neighbors))

        # 휴가/공가 fixed 셀의 직전일 N 금지 (하드, 휴가/공가 보호 정책).
        # fixed_wanted O / 휴무 / 주휴 등은 사용자 자발 OFF 또는 자동 OFF 라 대상 외.
        # 단 prev_d == T0(day 0) 자체는 cross-month 면제.
        _BAN_N_TYPES = {"휴가", "공가"}
        if bool(getattr(cfg, "ban_night_before_fixed_off", False)):
            for n in range(N):
                T0, T1 = join[n], leave[n]
                _ban_n_cnt = 0
                _assume_ban_n_fixed_fb = None  # lazy: 실제 ban 있는 간호사만 리터럴 생성
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
                    if _assume_ban_n_fixed_fb is None and _assume_registry_fb is not None:
                        _assume_ban_n_fixed_fb = _assume_registry_fb.create_literal(
                            f"BanNightBeforeFixedOff:nurse_{n}",
                            meta={
                                "node_id": f"ban_night_before_fixed_off:nurse_{n}",
                                "type": "BanNightBeforeFixedOffNode", "label": "휴가/공가 전날 야간 금지",
                                "value": True, "scope": "nurse", "scope_key": f"nurse_{n}",
                                "pattern": "ban_night_before_fixed_off",
                                "nurse_id": str(getattr(roster_system.nurses[n], "nurse_id", n)),
                                "human_message_ko": "휴가/공가 고정 전날 야간(N) 금지",
                                "resolution_hint": "휴가 직전 야간 금지(ban_night_before_fixed_off)를 끄세요.",
                            },
                        )
                    if _assume_ban_n_fixed_fb is not None:
                        m.Add(X(n, prev_d, night_idx) == 0).OnlyEnforceIf(_assume_ban_n_fixed_fb)
                    else:
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
                        _ow_expr_fb = (sum(X(n, d, off_idx) for d in free_days_w) >= 1)
                        if _assume_registry_fb is not None and _add_hard_fb is not None:
                            _add_hard_fb(
                                m,
                                _assume_registry_fb,
                                name=f"OffWindowRequirement:nurse_{n}:left_{left}:right_{right}",
                                constraint_expr=_ow_expr_fb,
                                meta={
                                    "node_id": f"off_window_requirement:nurse_{n}:left_{left}:right_{right}",
                                    "type": "OffWindowNode",
                                    "label": "off_window_min_off",
                                    "value": {"left_day": left + 1, "right_day": right + 1},
                                    "scope": "nurse",
                                    "scope_key": f"nurse_{n}",
                                    "pattern": "off_window_requirement",
                                    "nurse_id": str(getattr(roster_system.nurses[n], "nurse_id", n)),
                                    "human_message_ko": "월초 보정 구간 내 최소 1회 OFF가 필요합니다.",
                                    "resolution_hint": "해당 구간의 고정 근무를 일부 해제하거나 OFF 배정 여유를 확보하세요.",
                                },
                            )
                        else:
                            m.Add(_ow_expr_fb)
        except Exception as e:
            print(f"{logger_prefix} 월초 OFF 윈도우 적용 실패(fallback): err={e}")

        # 연속 근무 K+1 창에서 최소 1 OFF 필요 → HARD 제약 (fixed_wanted 포함, 우회 불가)
        # 정책:
        #   - blocked day 포함 윈도우: X 변수 부재 → 자동 중단 → 스킵
        #   - fixed OFF 포함 윈도우: 자동 만족 → 스킵
        #   - 그 외: 전체 윈도우에 대해 enforce. 유저가 K+1 연속 근무를 fixed_wanted로 지정했다면 INFEASIBLE로 보고.
        K = cfg.max_consecutive_work_days
        for n in range(N):
            T0, T1 = join[n], leave[n]
            _blocked = blocked_by_nurse.get(n, set()) if blocked_by_nurse else set()
            for d0 in range(T0, T1 - K + 1):
                window = [d0 + t for t in range(K + 1)]
                if any(d in _blocked for d in window):
                    continue
                if any((n, d) in fixed and fixed[(n, d)] == off_idx for d in window):
                    continue
                _mcw_expr_fb = (sum(X(n, d, off_idx) for d in window) >= 1)
                if _assume_registry_fb is not None and _add_hard_fb is not None:
                    _add_hard_fb(
                        m,
                        _assume_registry_fb,
                        name=f"MaxConsecutiveWorkWindow:nurse_{n}:start_{d0}:k_{K}",
                        constraint_expr=_mcw_expr_fb,
                        meta={
                            "node_id": f"max_consecutive_work:nurse_{n}:start_{d0}:k_{K}",
                            "type": "ConsecutiveWorkNode",
                            "label": "max_consecutive_work_min_off",
                            "value": {"start_day": d0 + 1, "window_size": K + 1},
                            "scope": "nurse",
                            "scope_key": f"nurse_{n}",
                            "pattern": "max_consecutive_work",
                            "nurse_id": str(getattr(roster_system.nurses[n], "nurse_id", n)),
                            "human_message_ko": "연속 근무 제한 구간(K+1)에는 최소 1회 OFF가 필요합니다.",
                            "resolution_hint": "연속 근무 구간의 고정 배정을 완화하거나 OFF를 추가하세요.",
                        },
                    )
                else:
                    m.Add(_mcw_expr_fb)

        # 연속 Night 상한 L → 초과량 정량화
        L = cfg.max_consecutive_nights
        for n in range(N):
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
                        _cn_edge_expr_fb = (sum(X(n, d, night_idx) for d in days_in_window) <= cap)
                        if _assume_registry_fb is not None and _add_hard_fb is not None:
                            _add_hard_fb(
                                m,
                                _assume_registry_fb,
                                name=f"ConsecutiveNightCapEdge:nurse_{n}:w_{w}",
                                constraint_expr=_cn_edge_expr_fb,
                                meta={
                                    "node_id": f"consecutive_night_cap_edge:nurse_{n}:w_{w}",
                                    "type": "ConsecutiveNightCapNode",
                                    "label": "consecutive_night_cap_edge",
                                    "value": {"window_len": len(days_in_window), "cap": int(cap)},
                                    "scope": "nurse",
                                    "scope_key": f"nurse_{n}",
                                    "pattern": "consecutive_night_cap",
                                    "nurse_id": str(getattr(roster_system.nurses[n], "nurse_id", n)),
                                    "human_message_ko": "월경계 연속 야간 상한을 초과할 수 없습니다.",
                                    "resolution_hint": "해당 구간의 N 고정을 완화하거나 야간 배정을 분산하세요.",
                                },
                            )
                        else:
                            m.Add(_cn_edge_expr_fb)
            for d0 in range(T0, T1 - L + 1):
                sum_n = sum(X(n, d0 + t, night_idx) for t in range(L + 1))
                exc = m.NewIntVar(0, L + 1, f"cnight_exc_{n}_{d0}")
                m.Add(exc >= sum_n - L)
                # 연속 N 상한 L 하드: three_seq_nig False면 L=2(3N 금지), True면 L=3(3N 허용)
                _cn_expr_fb = (sum_n <= L)
                if _assume_registry_fb is not None and _add_hard_fb is not None:
                    _add_hard_fb(
                        m,
                        _assume_registry_fb,
                        name=f"ConsecutiveNightCap:nurse_{n}:start_{d0}:L_{L}",
                        constraint_expr=_cn_expr_fb,
                        meta={
                            "node_id": f"consecutive_night_cap:nurse_{n}:start_{d0}:L_{L}",
                            "type": "ConsecutiveNightCapNode",
                            "label": "consecutive_night_cap",
                            "value": {"start_day": d0 + 1, "window_size": L + 1, "cap": int(L)},
                            "scope": "nurse",
                            "scope_key": f"nurse_{n}",
                            "pattern": "consecutive_night_cap",
                            "nurse_id": str(getattr(roster_system.nurses[n], "nurse_id", n)),
                            "human_message_ko": "연속 야간 상한을 초과할 수 없습니다.",
                            "resolution_hint": "연속 야간 구간의 고정을 완화하거나 다른 간호사로 분산하세요.",
                        },
                    )
                else:
                    m.Add(_cn_expr_fb)
                safety["cnight_excess"].append(exc)

        # 월 Night 상한 초과량 — MUS 추출용 assumption literal로 wrap
        for n in range(N):
            if _is_preceptee_at(n):
                continue
            T0, T1 = join[n], leave[n]
            sum_m = sum(X(n, d, night_idx) for d in range(T0, T1 + 1))
            _fb_mn_expr = (sum_m <= cfg.max_night_shifts_per_month)
            if _assume_registry_fb is not None and _add_hard_fb is not None:
                _fb_nu = roster_system.nurses[n] if n < len(roster_system.nurses) else None
                _add_hard_fb(
                    m, _assume_registry_fb,
                    name=f"MaxNight:nurse_{n}",
                    constraint_expr=_fb_mn_expr,
                    meta={
                        "node_id": f"max_night:nurse_{n}",
                        "type": "NightCapNode",
                        "label": "max_night_shifts_per_month",
                        "value": int(cfg.max_night_shifts_per_month or 0),
                        "scope": "nurse", "scope_key": f"nurse_{n}",
                        "pattern": "max_night",
                        "nurse_id": str(getattr(_fb_nu, "nurse_id", n)) if _fb_nu else str(n),
                        "human_message_ko": f"월간 N 상한 {int(cfg.max_night_shifts_per_month or 0)}일",
                        "resolution_hint": "월간 N 상한을 늘리거나 이 간호사의 N 부담을 다른 인력에 분산하세요.",
                    },
                )
            else:
                m.Add(_fb_mn_expr)

        # N 전담: D/E 하드 금지 (메인 모델과 동일)
        for n, nu in enumerate(roster_system.nurses):
            if _is_preceptee_at(n):
                continue
            raw = getattr(nu, "allowed_shifts", None)
            allowed = normalize_allowed_shift_codes(raw, use_mid=bool(getattr(cfg, "use_mid", False)))
            if not allowed:
                continue
            T0, T1 = join[n], leave[n]
            for d in range(T0, T1 + 1):
                if "D" not in allowed:
                    _allow_d_expr = (X(n, d, day_idx) == 0)
                    if _assume_registry_fb is not None and _add_hard_fb is not None:
                        _add_hard_fb(
                            m,
                            _assume_registry_fb,
                            name=f"AllowedShiftMaskBanD:nurse_{n}:day_{d}",
                            constraint_expr=_allow_d_expr,
                            meta={
                                "node_id": f"allowed_shift_mask:nurse_{n}:day_{d}:shift_D",
                                "type": "AllowedShiftMaskNode",
                                "label": "allowed_shift_mask_ban",
                                "value": {"day": d + 1, "shift": "D", "allowed": sorted(allowed)},
                                "scope": "nurse",
                                "scope_key": f"nurse_{n}",
                                "pattern": "allowed_shift_mask",
                                "nurse_id": str(getattr(roster_system.nurses[n], "nurse_id", n)),
                                "human_message_ko": "해당 간호사는 D 근무 허용 대상이 아닙니다.",
                                "resolution_hint": "간호사 허용 시프트 설정을 변경하거나 해당 배정을 제거하세요.",
                            },
                        )
                    else:
                        m.Add(_allow_d_expr)
                if "E" not in allowed:
                    _allow_e_expr = (X(n, d, eve_idx) == 0)
                    if _assume_registry_fb is not None and _add_hard_fb is not None:
                        _add_hard_fb(
                            m,
                            _assume_registry_fb,
                            name=f"AllowedShiftMaskBanE:nurse_{n}:day_{d}",
                            constraint_expr=_allow_e_expr,
                            meta={
                                "node_id": f"allowed_shift_mask:nurse_{n}:day_{d}:shift_E",
                                "type": "AllowedShiftMaskNode",
                                "label": "allowed_shift_mask_ban",
                                "value": {"day": d + 1, "shift": "E", "allowed": sorted(allowed)},
                                "scope": "nurse",
                                "scope_key": f"nurse_{n}",
                                "pattern": "allowed_shift_mask",
                                "nurse_id": str(getattr(roster_system.nurses[n], "nurse_id", n)),
                                "human_message_ko": "해당 간호사는 E 근무 허용 대상이 아닙니다.",
                                "resolution_hint": "간호사 허용 시프트 설정을 변경하거나 해당 배정을 제거하세요.",
                            },
                        )
                    else:
                        m.Add(_allow_e_expr)
                if "N" not in allowed:
                    _allow_n_expr = (X(n, d, night_idx) == 0)
                    if _assume_registry_fb is not None and _add_hard_fb is not None:
                        _add_hard_fb(
                            m,
                            _assume_registry_fb,
                            name=f"AllowedShiftMaskBanN:nurse_{n}:day_{d}",
                            constraint_expr=_allow_n_expr,
                            meta={
                                "node_id": f"allowed_shift_mask:nurse_{n}:day_{d}:shift_N",
                                "type": "AllowedShiftMaskNode",
                                "label": "allowed_shift_mask_ban",
                                "value": {"day": d + 1, "shift": "N", "allowed": sorted(allowed)},
                                "scope": "nurse",
                                "scope_key": f"nurse_{n}",
                                "pattern": "allowed_shift_mask",
                                "nurse_id": str(getattr(roster_system.nurses[n], "nurse_id", n)),
                                "human_message_ko": "해당 간호사는 N 근무 허용 대상이 아닙니다.",
                                "resolution_hint": "간호사 허용 시프트 설정을 변경하거나 해당 배정을 제거하세요.",
                            },
                        )
                    else:
                        m.Add(_allow_n_expr)
                if mid_idx is not None and "M" not in allowed:
                    _allow_m_expr = (X(n, d, mid_idx) == 0)
                    if _assume_registry_fb is not None and _add_hard_fb is not None:
                        _add_hard_fb(
                            m,
                            _assume_registry_fb,
                            name=f"AllowedShiftMaskBanM:nurse_{n}:day_{d}",
                            constraint_expr=_allow_m_expr,
                            meta={
                                "node_id": f"allowed_shift_mask:nurse_{n}:day_{d}:shift_M",
                                "type": "AllowedShiftMaskNode",
                                "label": "allowed_shift_mask_ban",
                                "value": {"day": d + 1, "shift": "M", "allowed": sorted(allowed)},
                                "scope": "nurse",
                                "scope_key": f"nurse_{n}",
                                "pattern": "allowed_shift_mask",
                                "nurse_id": str(getattr(roster_system.nurses[n], "nurse_id", n)),
                                "human_message_ko": "해당 간호사는 M 근무 허용 대상이 아닙니다.",
                                "resolution_hint": "간호사 허용 시프트 설정을 변경하거나 해당 배정을 제거하세요.",
                            },
                        )
                    else:
                        m.Add(_allow_m_expr)

        # 야간전담의 D/E 금지 위반(OR: D or E) — N전담은 하드로 처리하므로 소프트 미사용
        # for n, nu in enumerate(roster_system.nurses):
        #     if nu.allowed_shifts != 0:
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
                T0, T1 = join[n], leave[n]
                _blocked_3n = blocked_by_nurse.get(n, set()) if blocked_by_nurse else set()
                n_tail = prev_month_n_tail_by_idx.get(n, 0)
                n_offs_after_3n = _n_offs_after_map_3n.get(n, 0)
                # MUS용 per-nurse 3N2OFF 회복 리터럴 (within-month gate)
                _assume_3n2off_fb = None
                if _assume_registry_fb is not None:
                    _assume_3n2off_fb = _assume_registry_fb.create_literal(
                        f"Recovery3N2OFF:nurse_{n}",
                        meta={
                            "node_id": f"recovery_3n2off:nurse_{n}",
                            "type": "RecoveryOffNode", "label": "3N 후 2OFF 회복",
                            "value": "3N→2OFF", "scope": "nurse", "scope_key": f"nurse_{n}",
                            "pattern": "recovery_3n2off",
                            "nurse_id": str(getattr(roster_system.nurses[n], "nurse_id", n)),
                            "human_message_ko": "3N 연속 뒤 2OFF 회복 강제",
                            "resolution_hint": "3N 후 2OFF 회복 정책(two_offs_after_three_nig)을 끄거나 완화하세요.",
                        },
                    )
                _3n_rem = max(0, 2 - n_offs_after_3n) if n_tail >= 3 else 2
                if n_tail >= 3 and _3n_rem > 0 and (T0 + 1) <= T1 and T0 not in _blocked_3n and (T0 + 1) not in _blocked_3n:
                    end_prev_block = m.NewBoolVar(f"end_3n_prev_soft_{n}")
                    m.Add(end_prev_block == X(n, T0, night_idx).Not())
                    if not any((n, d2) in fixed_wanted_cells and fixed.get((n, d2)) not in (off_idx, night_idx, None) for d2 in (T0, T0 + 1)):
                        if _3n_rem >= 2:
                            _co_3n_expr_fb = (X(n, T0, off_idx) + X(n, T0 + 1, off_idx) == 2)
                            if _assume_registry_fb is not None:
                                _co_lit_fb = _assume_registry_fb.create_literal(
                                    f"CarryoverRecovery3N2OFF:nurse_{n}:day_{T0}",
                                    meta={
                                        "node_id": f"carryover_recovery_3n2off:nurse_{n}:day_{T0}",
                                        "type": "CarryoverTransitionNode",
                                        "label": "prev_month 3N2OFF boundary",
                                        "value": {"day": T0 + 1, "remaining_off_needed": 2},
                                        "scope": "nurse",
                                        "scope_key": f"nurse_{n}",
                                        "pattern": "carryover_boundary",
                                        "nurse_id": str(getattr(roster_system.nurses[n], "nurse_id", n)),
                                        "human_message_ko": "전월 3N 꼬리로 월초 2OFF 회복이 필요합니다.",
                                        "resolution_hint": "전월 경계 carryover 또는 월초 고정 배정을 조정하세요.",
                                    },
                                )
                                m.Add(_co_3n_expr_fb).OnlyEnforceIf([end_prev_block, _co_lit_fb])
                            else:
                                m.Add(_co_3n_expr_fb).OnlyEnforceIf([end_prev_block])
                        else:
                            # _3n_rem == 1: 전월 OFF가 T0 직전에 인접 → 남은 OFF는 T0에 강제(연속 2OFF 보장)
                            _co_3n_expr_fb = (X(n, T0, off_idx) >= 1)
                            if _assume_registry_fb is not None:
                                _co_lit_fb = _assume_registry_fb.create_literal(
                                    f"CarryoverRecovery3N2OFFPartial:nurse_{n}:day_{T0}",
                                    meta={
                                        "node_id": f"carryover_recovery_3n2off_partial:nurse_{n}:day_{T0}",
                                        "type": "CarryoverTransitionNode",
                                        "label": "prev_month 3N2OFF boundary partial",
                                        "value": {"day": T0 + 1, "remaining_off_needed": 1},
                                        "scope": "nurse",
                                        "scope_key": f"nurse_{n}",
                                        "pattern": "carryover_boundary",
                                        "nurse_id": str(getattr(roster_system.nurses[n], "nurse_id", n)),
                                        "human_message_ko": "전월 3N 꼬리 회복 OFF가 월초에 추가로 필요합니다.",
                                        "resolution_hint": "월초 OFF 슬롯 또는 전월 carryover 입력을 조정하세요.",
                                    },
                                )
                                m.Add(_co_3n_expr_fb).OnlyEnforceIf([_co_lit_fb])
                            else:
                                m.Add(_co_3n_expr_fb)
                    print(f"{logger_prefix} [3N2OFF-cross] nurse_idx={n}, n_tail={n_tail}, "
                          f"offs_after={n_offs_after_3n}, rem={_3n_rem}")
                elif n_tail >= 3 and _3n_rem == 0:
                    print(f"{logger_prefix} [3N2OFF-cross] nurse_idx={n}, n_tail={n_tail}, "
                          f"offs_after={n_offs_after_3n} → 전월 내 2OFF 충족, 현월 강제 OFF 스킵")
                if n_tail >= 2 and n_offs_after_3n < 2 and (T0 + 2) <= T1:
                    if not any((n, d2) in fixed_wanted_cells and fixed.get((n, d2)) not in (off_idx, night_idx, None) for d2 in (T0 + 1, T0 + 2)):
                        _expr_3n_tail2_fb = (X(n, T0 + 1, off_idx) + X(n, T0 + 2, off_idx) == 2)
                        if _assume_registry_fb is not None and _add_hard_fb is not None:
                            _co_lit_fb = _assume_registry_fb.create_literal(
                                f"CarryoverRecovery3N2OFFTail2:nurse_{n}:day_{T0}",
                                meta={
                                    "node_id": f"carryover_recovery_3n2off_tail2:nurse_{n}:day_{T0}",
                                    "type": "CarryoverTransitionNode",
                                    "label": "prev_month 3N2OFF boundary tail2",
                                    "value": {"day": T0 + 2, "remaining_off_needed": 2},
                                    "scope": "nurse",
                                    "scope_key": f"nurse_{n}",
                                    "pattern": "carryover_boundary",
                                    "nurse_id": str(getattr(roster_system.nurses[n], "nurse_id", n)),
                                    "human_message_ko": "전월 N 꼬리로 월초 회복 OFF 2일이 필요합니다.",
                                    "resolution_hint": "월초 OFF 슬롯 또는 전월 carryover 입력을 조정하세요.",
                                },
                            )
                            m.Add(_expr_3n_tail2_fb).OnlyEnforceIf([X(n, T0, night_idx), _co_lit_fb])
                        else:
                            m.Add(_expr_3n_tail2_fb).OnlyEnforceIf([X(n, T0, night_idx)])
                if n_tail == 1 and n_offs_after_3n < 2 and (T0 + 3) <= T1:
                    if not any((n, d2) in fixed_wanted_cells and fixed.get((n, d2)) not in (off_idx, night_idx, None) for d2 in (T0 + 2, T0 + 3)):
                        _expr_3n_tail1_fb = (X(n, T0 + 2, off_idx) + X(n, T0 + 3, off_idx) == 2)
                        if _assume_registry_fb is not None and _add_hard_fb is not None:
                            _co_lit_fb = _assume_registry_fb.create_literal(
                                f"CarryoverRecovery3N2OFFTail1:nurse_{n}:day_{T0}",
                                meta={
                                    "node_id": f"carryover_recovery_3n2off_tail1:nurse_{n}:day_{T0}",
                                    "type": "CarryoverTransitionNode",
                                    "label": "prev_month 3N2OFF boundary tail1",
                                    "value": {"day": T0 + 3, "remaining_off_needed": 2},
                                    "scope": "nurse",
                                    "scope_key": f"nurse_{n}",
                                    "pattern": "carryover_boundary",
                                    "nurse_id": str(getattr(roster_system.nurses[n], "nurse_id", n)),
                                    "human_message_ko": "전월 N 꼬리 회복을 위해 월초 OFF 2일이 필요합니다.",
                                    "resolution_hint": "월초 OFF 슬롯 또는 전월 carryover 입력을 조정하세요.",
                                },
                            )
                            m.Add(_expr_3n_tail1_fb).OnlyEnforceIf([X(n, T0, night_idx), X(n, T0 + 1, night_idx), _co_lit_fb])
                        else:
                            m.Add(_expr_3n_tail1_fb).OnlyEnforceIf([X(n, T0, night_idx), X(n, T0 + 1, night_idx)])
                for d in range(T0 + 2, T1 - 1):
                    if any((n, d2) in fixed_wanted_cells and fixed.get((n, d2)) not in (off_idx, night_idx, None) for d2 in (d + 1, d + 2)):
                        # 회복 OFF 슬롯에 non-OFF fixed_wanted → 3N 블록 자체를 금지
                        _guard_3n_expr_fb = (X(n, d, night_idx) + X(n, d - 1, night_idx) + X(n, d - 2, night_idx) <= 2)
                        if _assume_registry_fb is not None and _add_hard_fb is not None:
                            _add_hard_fb(
                                m,
                                _assume_registry_fb,
                                name=f"CarryoverRecovery3N2OFFGuard:nurse_{n}:day_{d}",
                                constraint_expr=_guard_3n_expr_fb,
                                meta={
                                    "node_id": f"carryover_recovery_3n2off_guard:nurse_{n}:day_{d}",
                                    "type": "CarryoverTransitionNode",
                                    "label": "carryover 3N block guard",
                                    "value": {"day": d + 1, "blocked_by_fixed": True},
                                    "scope": "nurse",
                                    "scope_key": f"nurse_{n}",
                                    "pattern": "carryover_boundary",
                                    "nurse_id": str(getattr(roster_system.nurses[n], "nurse_id", n)),
                                    "human_message_ko": "회복 OFF 슬롯 고정과 3N 블록이 충돌합니다.",
                                    "resolution_hint": "해당 고정 배정 또는 회복 규칙을 조정하세요.",
                                },
                            )
                        else:
                            m.Add(_guard_3n_expr_fb)
                        continue
                    xn0 = X(n, d, night_idx)
                    xn1 = X(n, d - 1, night_idx)
                    xn2 = X(n, d - 2, night_idx)
                    _enforce_3n_main_fb = [xn0, xn1, xn2]
                    if _assume_3n2off_fb is not None:
                        _enforce_3n_main_fb.append(_assume_3n2off_fb)
                    m.Add(
                        X(n, d + 1, off_idx) + X(n, d + 2, off_idx) == 2
                    ).OnlyEnforceIf(_enforce_3n_main_fb)
        if cfg.two_offs_after_two_nig:
            _n_offs_after_map = getattr(roster_system, "prev_month_n_offs_after_by_idx", {}) or {}
            for n in range(N):
                T0, T1 = join[n], leave[n]
                _blocked_2n = blocked_by_nurse.get(n, set()) if blocked_by_nurse else set()
                n_tail = prev_month_n_tail_by_idx.get(n, 0)
                n_offs_after = _n_offs_after_map.get(n, 0)
                # MUS용 per-nurse 2N2OFF 회복 리터럴 — within-month 회복 강제를 gate 해
                # 회복이 병목이면 core 에 RecoveryOffNode 로 뜬다(프로덕션 fallback 경로).
                _assume_2n2off_fb = None
                if _assume_registry_fb is not None:
                    _assume_2n2off_fb = _assume_registry_fb.create_literal(
                        f"Recovery2N2OFF:nurse_{n}",
                        meta={
                            "node_id": f"recovery_2n2off:nurse_{n}",
                            "type": "RecoveryOffNode", "label": "2N 후 2OFF 회복",
                            "value": "2N→2OFF", "scope": "nurse", "scope_key": f"nurse_{n}",
                            "pattern": "recovery_2n2off",
                            "nurse_id": str(getattr(roster_system.nurses[n], "nurse_id", n)),
                            "human_message_ko": "2N 연속 뒤 2OFF 회복 강제",
                            "resolution_hint": "2N 후 2OFF 회복 정책(two_offs_after_two_nig)을 끄거나 완화하세요.",
                        },
                    )
                # 전월 N tail 뒤 이미 소비된 OFF 수를 반영
                _2n_rem = max(0, 2 - n_offs_after) if n_tail >= 2 else 2
                if n_tail >= 2 and _2n_rem > 0 and (T0 + 1) <= T1 and T0 not in _blocked_2n and (T0 + 1) not in _blocked_2n:
                    end_prev_block = m.NewBoolVar(f"end_2n_prev_soft_{n}")
                    m.Add(end_prev_block == X(n, T0, night_idx).Not())
                    if not any((n, d2) in fixed_wanted_cells and fixed.get((n, d2)) not in (off_idx, night_idx, None) for d2 in (T0, T0 + 1)):
                        if _2n_rem >= 2:
                            _co_2n_expr_fb = (X(n, T0, off_idx) + X(n, T0 + 1, off_idx) == 2)
                            if _assume_registry_fb is not None:
                                _co_lit_fb = _assume_registry_fb.create_literal(
                                    f"CarryoverRecovery2N2OFF:nurse_{n}:day_{T0}",
                                    meta={
                                        "node_id": f"carryover_recovery_2n2off:nurse_{n}:day_{T0}",
                                        "type": "CarryoverTransitionNode",
                                        "label": "prev_month 2N2OFF boundary",
                                        "value": {"day": T0 + 1, "remaining_off_needed": 2},
                                        "scope": "nurse",
                                        "scope_key": f"nurse_{n}",
                                        "pattern": "carryover_boundary",
                                        "nurse_id": str(getattr(roster_system.nurses[n], "nurse_id", n)),
                                        "human_message_ko": "전월 2N 꼬리로 월초 2OFF 회복이 필요합니다.",
                                        "resolution_hint": "전월 경계 carryover 또는 월초 고정 배정을 조정하세요.",
                                    },
                                )
                                m.Add(_co_2n_expr_fb).OnlyEnforceIf([end_prev_block, _co_lit_fb])
                            else:
                                m.Add(_co_2n_expr_fb).OnlyEnforceIf([end_prev_block])
                        else:
                            # _2n_rem == 1: 전월 OFF가 T0 직전에 인접 → 남은 OFF는 T0에 강제(연속 2OFF 보장)
                            _co_2n_expr_fb = (X(n, T0, off_idx) >= 1)
                            if _assume_registry_fb is not None:
                                _co_lit_fb = _assume_registry_fb.create_literal(
                                    f"CarryoverRecovery2N2OFFPartial:nurse_{n}:day_{T0}",
                                    meta={
                                        "node_id": f"carryover_recovery_2n2off_partial:nurse_{n}:day_{T0}",
                                        "type": "CarryoverTransitionNode",
                                        "label": "prev_month 2N2OFF boundary partial",
                                        "value": {"day": T0 + 1, "remaining_off_needed": 1},
                                        "scope": "nurse",
                                        "scope_key": f"nurse_{n}",
                                        "pattern": "carryover_boundary",
                                        "nurse_id": str(getattr(roster_system.nurses[n], "nurse_id", n)),
                                        "human_message_ko": "전월 2N 꼬리 회복 OFF가 월초에 추가로 필요합니다.",
                                        "resolution_hint": "월초 OFF 슬롯 또는 전월 carryover 입력을 조정하세요.",
                                    },
                                )
                                m.Add(_co_2n_expr_fb).OnlyEnforceIf([_co_lit_fb])
                            else:
                                m.Add(_co_2n_expr_fb)
                    print(f"{logger_prefix} [2N2OFF-cross] nurse_idx={n}, n_tail={n_tail}, "
                          f"offs_after={n_offs_after}, rem={_2n_rem}")
                elif n_tail >= 2 and _2n_rem == 0:
                    print(f"{logger_prefix} [2N2OFF-cross] nurse_idx={n}, n_tail={n_tail}, "
                          f"offs_after={n_offs_after} → 전월 내 2OFF 충족, 현월 강제 OFF 스킵")
                if n_tail >= 1 and n_offs_after < 2 and (T0 + 2) <= T1:
                    end_block_b0 = m.NewBoolVar(f"end_2n_soft_b0_{n}")
                    m.Add(end_block_b0 == X(n, T0 + 1, night_idx).Not())
                    if not any((n, d2) in fixed_wanted_cells and fixed.get((n, d2)) not in (off_idx, night_idx, None) for d2 in (T0 + 1, T0 + 2)):
                        _expr_2n_b0_fb = (X(n, T0 + 1, off_idx) + X(n, T0 + 2, off_idx) == 2)
                        if _assume_registry_fb is not None and _add_hard_fb is not None:
                            _co_lit_fb = _assume_registry_fb.create_literal(
                                f"CarryoverRecovery2N2OFFBoundary:nurse_{n}:day_{T0}",
                                meta={
                                    "node_id": f"carryover_recovery_2n2off_boundary:nurse_{n}:day_{T0}",
                                    "type": "CarryoverTransitionNode",
                                    "label": "prev_month 2N2OFF boundary enforce",
                                    "value": {"day": T0 + 2, "remaining_off_needed": 2},
                                    "scope": "nurse",
                                    "scope_key": f"nurse_{n}",
                                    "pattern": "carryover_boundary",
                                    "nurse_id": str(getattr(roster_system.nurses[n], "nurse_id", n)),
                                    "human_message_ko": "전월 2N 꼬리 회복 OFF가 월초에 강제됩니다.",
                                    "resolution_hint": "월초 OFF 슬롯 또는 전월 carryover 입력을 조정하세요.",
                                },
                            )
                            m.Add(_expr_2n_b0_fb).OnlyEnforceIf([X(n, T0, night_idx), end_block_b0, _co_lit_fb])
                        else:
                            m.Add(_expr_2n_b0_fb).OnlyEnforceIf([X(n, T0, night_idx), end_block_b0])
                for d in range(T0 + 1, T1 - 1):
                    if any((n, d2) in fixed_wanted_cells and fixed.get((n, d2)) not in (off_idx, night_idx, None) for d2 in (d + 1, d + 2)):
                        # 회복 OFF 슬롯에 non-OFF fixed_wanted → 이 위치에서 2N 블록 종료 금지
                        xn_prev_fw = X(n, d - 1, night_idx)
                        xn_curr_fw = X(n, d, night_idx)
                        end_block_fw = m.NewBoolVar(f"end_2n_fw_{n}_{d}")
                        m.Add(end_block_fw == X(n, d + 1, night_idx).Not())
                        # 회복 정책의 fixed-wanted 예외 분기도 동일 리터럴로 gate(MUS 일관)
                        if _assume_2n2off_fb is not None:
                            m.Add(xn_prev_fw + xn_curr_fw + end_block_fw <= 2).OnlyEnforceIf(_assume_2n2off_fb)
                        else:
                            m.Add(xn_prev_fw + xn_curr_fw + end_block_fw <= 2)
                        continue
                    xn_prev = X(n, d - 1, night_idx)
                    xn_curr = X(n, d, night_idx)
                    xn_next = X(n, d + 1, night_idx)
                    end_block = m.NewBoolVar(f"end_2n_hard_{n}_{d}")
                    m.Add(end_block == xn_next.Not())
                    _enforce_2n_main_fb = [xn_prev, xn_curr, end_block]
                    if _assume_2n2off_fb is not None:
                        _enforce_2n_main_fb.append(_assume_2n2off_fb)
                    m.Add(
                        X(n, d + 1, off_idx) + X(n, d + 2, off_idx) == 2
                    ).OnlyEnforceIf(_enforce_2n_main_fb)

        # 금지 패턴 N-O-D/E
        if getattr(cfg, "nod_noe", True):
            for n in range(N):
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
                    raw = getattr(nu, "allowed_shifts", None)
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
                    # [OffLevelLock] off_first=False(=근무 oversupply 의도) 실효화:
                    # OFF 상한(target 초과=잉여)을 floor와 대칭으로 가중한다. 기존엔 excess만
                    # raw(weight 1)라, stage2 safety의 pre-scaled 항(isolated_off 300K,
                    # min_off_lower 100K 등)에 묻혀 OFF가 cap까지 드리프트했다. scaled 슬랙을
                    # safety에 넣어 stage2(OPTIMAL, KLD/grade 이전)에서 OFF 수준을 target에
                    # 고정 → stage3가 zero-lock으로 보존. "수준"만 잠그고 배치/예외는 soft라
                    # 유지(하드 고정 아님). 원복: off_quota_excess_weight=0.
                    import os as _os
                    _oqx_w = int(
                        _os.environ.get(
                            "OFF_QUOTA_EXCESS_WEIGHT",
                            getattr(cfg, "off_quota_excess_weight", 100000),
                        )
                        or 0
                    )
                    if _oqx_w > 1 and not bool(getattr(cfg, "off_first", False)):
                        _ex_scaled = m.NewIntVar(0, D * _oqx_w, f"off_quota_excess_cost_{n}")
                        m.Add(_ex_scaled == slack_excess * _oqx_w)
                        safety["off_quota_excess"].append(_ex_scaled)
                    else:
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
                raw = getattr(nu, "allowed_shifts", None)
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
                    hard_lower = int(min_off_required)
                    # per-nurse 1일 lower slack (페널티 무거움) — 솔버가 min_off 1일
                    # 부족한 nurse 만 자동 풀어줌. 글로벌 -1 (구 relax_level=1) 의
                    # 항상-ON per-nurse 버전. 9B 2026-07 등 min_off=11 hard 가
                    # combinatorial 로 막히는 케이스 회복.
                    _lower_slack_max = 1
                    _lower_slack_weight = 100000
                    if _lower_slack_max > 0:
                        min_off_lower_slack = m.NewIntVar(
                            0, _lower_slack_max, f"min_off_lower_slack_{n}"
                        )
                        m.Add(offs + min_off_lower_slack >= hard_lower)
                        _lower_weighted = m.NewIntVar(
                            0,
                            _lower_slack_max * _lower_slack_weight,
                            f"min_off_lower_slack_weighted_{n}",
                        )
                        m.Add(_lower_weighted == min_off_lower_slack * _lower_slack_weight)
                        safety["off_cap_bounded_slack"].append(_lower_weighted)
                        m._off_slack_lower_by_n[n] = min_off_lower_slack  # type: ignore[attr-defined]
                    else:
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
                        # OFF=avail-N 항등식 → off-cap = avail - 실효 N상한.
                        # 실효 N상한 = min(글로벌 max_night, per-nurse n_max/n_exact).
                        # n_exact 등으로 N이 낮게 고정되면 forced OFF(=avail-N)가 커지므로
                        # cap을 그만큼 확장(미반영 시 INFEASIBLE). n 한도 없으면 기존 동작 동일.
                        _global_mn = int(getattr(cfg, "max_night_shifts_per_month", 15) or 15)
                        _eff_ncap = effective_night_cap(nu, _global_mn)
                        total_cap_effective = max(0, avail_days - _eff_ncap)
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
                        # GRADE/coverage 충족을 위해 cap을 풀어주는 여유 일수.
                        # extra_off_penalty_weight 때문에 솔버는 필요할 때만 이 여유를 사용.
                        # baseline(off_days) 초과분은 후처리 OffSwap이 연차로 라벨링.
                        _off_cap_relax_extra = max(0, int(getattr(cfg, "off_cap_relax_extra", 0) or 0))
                        # off_first 분기: False=근무 oversupply(OFF tight) / True=OFF oversupply(dev HEAD)
                        _off_first_fb = bool(getattr(cfg, "off_first", False))
                        if _off_first_fb:
                            base_cap = max_off_allowed_from_policy
                            base_cap += _extra_off_fb
                            base_cap += _off_cap_relax_extra
                            if n in per_nurse_off_cap_override:
                                base_cap = max(base_cap, per_nurse_off_cap_override[n])
                            # 글로벌 relax 증가 없음 — per-nurse cap override 만 반영
                            total_cap_effective = min(base_cap, avail_days)
                            # max coverage 자동 조정: max_off cap
                            if _fb_auto_max_off is not None:
                                import math as _math
                                _ratio_fb = nonvac_active_days / max(1, D_phys)
                                _scaled_max_fb = max(min_off_required, int(_fb_auto_max_off * _ratio_fb))
                                total_cap_effective = min(total_cap_effective, _scaled_max_fb)
                        else:
                            # off_first=False: OFF tight clamp (min_off_required + HARD recovery buffer only)
                            base_cap = min_off_required + _extra_off_fb + _off_cap_relax_extra
                            if n in per_nurse_off_cap_override:
                                base_cap = max(base_cap, per_nurse_off_cap_override[n])
                            # 글로벌 relax 증가 없음 — per-nurse cap override 만 반영
                            total_cap_effective = min(base_cap, avail_days)
                            total_cap_effective = max(total_cap_effective, min_off_required)
                    # OFF cap slack 결정 정책:
                    # 1) cfg gate (off_cap_bounded_slack_enable=True) → 기존 cfg max/weight 사용
                    # 2) gate False → **per-nurse 1일 slack 기본 활성** (페널티 무거움)
                    #    → 솔버가 cap 부족한 nurse 만 자동으로 1일 풀어줌. 나머지는 0.
                    #    이전 relax_level=1 의 글로벌 retry 단계를 항상-ON 형태로 대체
                    #    (9B 2026-07 등 정적 cap 진단은 통과하지만 combinatorial 로 막히는
                    #    케이스 회복).
                    if off_cap_bounded_slack_enable and off_cap_bounded_slack_max > 0:
                        _slack_max = int(off_cap_bounded_slack_max)
                        _slack_weight = int(off_cap_bounded_slack_weight)
                    else:
                        _slack_max = 1
                        _slack_weight = 100000
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
                        m._off_slack_upper_by_n[n] = cap_slack  # type: ignore[attr-defined]
                    else:
                        # OFF cap hard branch — MUS 추출용 assumption literal로 wrap
                        _fb_oc_expr = (nonvac_offs <= total_cap_effective)
                        if _assume_registry_fb is not None and _add_hard_fb is not None:
                            _add_hard_fb(
                                m, _assume_registry_fb,
                                name=f"OffCap:nurse_{n}",
                                constraint_expr=_fb_oc_expr,
                                meta={
                                    "node_id": f"off_cap:nurse_{n}",
                                    "type": "OffCapNode",
                                    "label": "max_off (effective)",
                                    "value": int(total_cap_effective),
                                    "scope": "nurse", "scope_key": f"nurse_{n}",
                                    "pattern": "off_cap",
                                    "nurse_id": str(nurse_id) if nurse_id is not None else str(n),
                                    "human_message_ko": f"이 간호사 OFF 상한 {int(total_cap_effective)}일",
                                    "resolution_hint": "이 간호사 OFF 상한을 늘리세요.",
                                },
                            )
                        else:
                            m.Add(_fb_oc_expr)
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
        # team_min soft slack 을 stage 목적함수에 주입하기 위해 캡처.
        # soft 모드에선 add_team_min_constraints 가 slack 제약만 걸고 반환 패널티는
        # Maximize 용(-w*slack)이라 Minimize stage 에선 버려졌다 → 슬랙을 직접 재가중.
        _tm_cover_slacks: list = []
        if stage in (1, 2):
            try:
                _gs_tm = str(getattr(roster_system, "grade_strategy", "BASE") or "BASE").upper()
                add_team_min_constraints(
                    m, roster_system, X, join, leave,
                    grade_strategy=_gs_tm,
                    blocked_by_nurse=blocked_by_nurse,
                )
                _tm_cover_slacks = list(getattr(roster_system, "_team_min_cover_slacks", []) or [])
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
            # team_min soft: Stage 3 가 infeasible 이면 이 Stage 2 해가 commit 되므로,
            # 여기서 팀 커버 부족(slack)을 목적함수에 넣어야 실제로 최소화된다(안 그러면 무력).
            # 커버리지(coverage_eq)는 이미 고정 → 팀 스프레드는 동일 커버 내 재배치로 개선.
            # 가중치 TEAM_COVER_LEX_WEIGHT(30만)는 safety(off-quota 10~30만) 상위 co-priority.
            m.Minimize(
                sum(safety_sum)
                + TEAM_COVER_LEX_WEIGHT * sum(_tm_cover_slacks)
            )
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
            # 최종 lex 패스(옵션)용: stage3 목적식 항을 side-channel 에 보존.
            try:
                m._stage3_obj_terms = list(obj)  # type: ignore[attr-defined]
            except Exception:
                pass
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

    def _log_off_slack_used(stage_label, solver, model):
        """post-solve: 어느 nurse 가 OFF cap slack 을 실제 사용했는지 stdout 로그."""
        try:
            lo = getattr(model, "_off_slack_lower_by_n", {}) or {}
            hi = getattr(model, "_off_slack_upper_by_n", {}) or {}
            rows = []
            for n in sorted(set(list(lo.keys()) + list(hi.keys()))):
                lv = solver.Value(lo[n]) if n in lo else 0
                uv = solver.Value(hi[n]) if n in hi else 0
                if lv > 0 or uv > 0:
                    nu = roster_system.nurses[n] if 0 <= n < len(roster_system.nurses) else None
                    nid = getattr(nu, "nurse_id", "?") if nu else "?"
                    name = getattr(nu, "name", "?") if nu else "?"
                    rows.append((n, nid, name, int(lv), int(uv)))
            if rows:
                print(
                    f"{logger_prefix} [OffCapSlack][used][{stage_label}] "
                    f"count={len(rows)} (페널티 100K 강제 trigger — combinatorial 회피)"
                )
                for n, nid, name, lv, uv in rows:
                    parts = []
                    if lv > 0:
                        parts.append(f"min_off -{lv}")
                    if uv > 0:
                        parts.append(f"max_off +{uv}")
                    print(
                        f"{logger_prefix} [OffCapSlack][used][{stage_label}]   "
                        f"nurse_idx={n}, id={nid}, name={name}, {', '.join(parts)}"
                    )
        except Exception as _slack_exc:
            print(f"{logger_prefix} [OffCapSlack][log] 실패(무시): {_slack_exc}")

    # ───── 1단계: 커버리지 (hard 1회 → broad soft 1회) ─────
    m1, X1, short1, over1, safety1 = None, None, None, None, None
    short_map1 = {}
    over_map1 = {}
    s1 = None
    best_short, best_over = None, None
    used_broad_soft = False
    attempt_specs = [(False, "hard"), (True, "broad_soft")]
    time_per_attempt = max(5, tl1 // len(attempt_specs))

    for broad_soft, attempt_label in attempt_specs:
        with timer_cls(f"폴백 1단계: 커버리지 부족 최소화 ({attempt_label})"):
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
                stage=1, broad_soft=broad_soft
            )
            s1 = cp_model.CpSolver()
            s1.parameters.max_time_in_seconds = time_per_attempt
            s1.parameters.num_search_workers = 8
            s1.parameters.relative_gap_limit = 0.15
            # MUS 추출용 assumption registry attach
            _reg_s1 = getattr(m1, "_cpsat_assumption_registry", None)
            if _reg_s1 is not None:
                _reg_s1.attach_to_model()
            st = s1.Solve(m1)
            if st == cp_model.INFEASIBLE and _reg_s1 is not None:
                try:
                    _fb_cores = _reg_s1.extract_conflict_cores(s1, solver_phase="fallback")
                    if _fb_cores:
                        roster_system._cpsat_conflict_cores = (
                            list(getattr(roster_system, "_cpsat_conflict_cores", []) or []) + _fb_cores
                        )
                        print(f"[FallbackLex][stage1] MUS cores: {len(_fb_cores)}")
                except Exception as _mus_exc:
                    print(f"[FallbackLex][stage1] MUS 추출 실패(무시): {_mus_exc}")
            print(
                f"{logger_prefix} 폴백1 결과: attempt={attempt_label}, "
                f"status={_cp_sat_status_to_text(st)}"
            )
            if st in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                _log_off_slack_used(f"stage1:{attempt_label}", s1, m1)
                best_short = int(s1.Value(sum(short1)))
                best_over = int(s1.Value(sum(over1)))
                used_broad_soft = broad_soft
                if broad_soft:
                    print(f"{logger_prefix} 폴백1 성공: broad soft 적용 (max coverage soft, M min soft)")
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
            if not broad_soft:
                print(f"{logger_prefix} 폴백1 실패 ({attempt_label}): broad soft 1회 재시도...")
            else:
                print(f"{logger_prefix} 폴백1 최종 실패: hard/broad soft 모두 실패")

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
            broad_soft=used_broad_soft,
        )
        s2 = cp_model.CpSolver()
        s2.parameters.max_time_in_seconds = tl2
        s2.parameters.num_search_workers = 8
        s2.parameters.relative_gap_limit = 0.15
        _reg_s2 = getattr(m2, "_cpsat_assumption_registry", None)
        if _reg_s2 is not None:
            _reg_s2.attach_to_model()
        st2 = s2.Solve(m2)
        if st2 == cp_model.INFEASIBLE and _reg_s2 is not None:
            try:
                _fb_cores = _reg_s2.extract_conflict_cores(s2, solver_phase="fallback")
                if _fb_cores:
                    roster_system._cpsat_conflict_cores = (
                        list(getattr(roster_system, "_cpsat_conflict_cores", []) or []) + _fb_cores
                    )
                    print(f"[FallbackLex][stage2] MUS cores: {len(_fb_cores)}")
            except Exception as _mus_exc:
                print(f"[FallbackLex][stage2] MUS 추출 실패(무시): {_mus_exc}")
        print(f"{logger_prefix} 폴백2 결과: status={_cp_sat_status_to_text(st2)}")
        if st2 in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            _log_off_slack_used("stage2", s2, m2)
            # H1: Stage2 lex 3-pass — safety_sum → OFF range → N range 순차 minimize.
            # 각 단계는 이전 cost 동결로 다른 항 영향 0 보장.
            # lex 재solve 는 cold-start 라 큰 인스턴스에서 시간 내 incumbent 를 못 찾고
            # UNKNOWN(빈 해)으로 끝날 수 있다. 그러면 downstream(zero-lock/hint/stage3 실패
            # fallback)이 망가진 s2 를 읽어 빈 roster 를 만든다. → 성공한 마지막 해를
            # 스냅샷에 보존하고(downstream 은 이 스냅샷을 읽음), 재solve 전 warm-start hint 로
            # 직전 feasible 해를 주입해 빈 해 반환 자체를 막는다.
            lex_x2_val: dict = {}
            lex_safety_val: dict = {}

            def _capture_lex_solution() -> None:
                for _n in range(N):
                    for _d in iter_nurse_days(_n, join, leave, blocked_by_nurse):
                        for _s in range(S):
                            lex_x2_val[(_n, _d, _s)] = int(s2.Value(X2(_n, _d, _s)))
                for _k, _arr in safety2.items():
                    lex_safety_val[_k] = [int(s2.Value(_v)) for _v in _arr]

            def _hint_lex_solution() -> None:
                try:
                    m2.ClearHints()
                    for (_n, _d, _s), _val in lex_x2_val.items():
                        m2.AddHint(X2(_n, _d, _s), _val)
                except Exception:
                    pass

            _capture_lex_solution()  # stage2 해 보존: lex 전부 실패해도 이 해로 복원
            try:
                flat_safety = []
                for arr in safety2.values():
                    flat_safety.extend(arr)
                if flat_safety:
                    best_sum2 = sum(int(s2.Value(v)) for v in flat_safety)
                    m2.Add(sum(flat_safety) <= best_sum2)
                    use_mid_h1 = bool(getattr(cfg, "use_mid", False))
                    off_count_vars = []
                    nightonly_excluded_idx: set[int] = set()
                    for n_lex in range(N):
                        if leave[n_lex] < join[n_lex]:
                            continue
                        nu = roster_system.nurses[n_lex]
                        try:
                            is_n_only_lex = is_n_only_profile(
                                getattr(nu, "allowed_shifts", None), use_mid=use_mid_h1
                            )
                        except Exception:
                            is_n_only_lex = False
                        if is_n_only_lex:
                            nightonly_excluded_idx.add(n_lex)
                            continue
                        cnt = sum(
                            X2(n_lex, d, off_idx)
                            for d in iter_nurse_days(n_lex, join, leave, blocked_by_nurse)
                        )
                        off_count_vars.append((n_lex, cnt))
                    if off_count_vars:
                        max_off_lex = m2.NewIntVar(0, D, "lex_max_off")
                        min_off_lex = m2.NewIntVar(0, D, "lex_min_off")
                        for _, cnt in off_count_vars:
                            m2.Add(cnt <= max_off_lex)
                            m2.Add(cnt >= min_off_lex)
                        m2.Minimize(max_off_lex - min_off_lex)
                        s2.parameters.max_time_in_seconds = max(2.0, float(tl2) * 0.2)
                        _hint_lex_solution()
                        st2_off = s2.Solve(m2)
                        if st2_off in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                            _capture_lex_solution()
                            best_off_range = int(s2.Value(max_off_lex) - s2.Value(min_off_lex))
                            print(
                                f"{logger_prefix} 폴백2 lex 2-pass (OFF range): "
                                f"status={_cp_sat_status_to_text(st2_off)} range={best_off_range}"
                            )
                            m2.Add(max_off_lex - min_off_lex <= best_off_range)
                            night_idx_h1 = (
                                cfg.shift_types.index("N") if "N" in cfg.shift_types else None
                            )
                            if night_idx_h1 is not None:
                                n_count_vars = []
                                for n_lex in range(N):
                                    if leave[n_lex] < join[n_lex]:
                                        continue
                                    if n_lex in nightonly_excluded_idx:
                                        continue
                                    cnt_n = sum(
                                        X2(n_lex, d, night_idx_h1)
                                        for d in iter_nurse_days(n_lex, join, leave, blocked_by_nurse)
                                    )
                                    n_count_vars.append(cnt_n)
                                if n_count_vars:
                                    max_n_lex = m2.NewIntVar(0, D, "lex_max_n")
                                    min_n_lex = m2.NewIntVar(0, D, "lex_min_n")
                                    for cnt in n_count_vars:
                                        m2.Add(cnt <= max_n_lex)
                                        m2.Add(cnt >= min_n_lex)
                                    m2.Minimize(max_n_lex - min_n_lex)
                                    s2.parameters.max_time_in_seconds = max(2.0, float(tl2) * 0.2)
                                    _hint_lex_solution()
                                    st2_n = s2.Solve(m2)
                                    if st2_n in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                                        _capture_lex_solution()
                                        best_n_range = int(
                                            s2.Value(max_n_lex) - s2.Value(min_n_lex)
                                        )
                                        print(
                                            f"{logger_prefix} 폴백2 lex 3-pass (N range): "
                                            f"status={_cp_sat_status_to_text(st2_n)} "
                                            f"range={best_n_range}"
                                        )
                                        # H1 4-pass: N 블록 간 간격(n2n) deficit 최소화.
                                        # 안전·OFF range·N range 를 동결한 뒤, 그 품질을 깨지 않는
                                        # 범위에서만 야간 간격(target 미만)을 벌린다. soft 항은 KLD에
                                        # 눌려 무시되므로 lex 우선순위로 끌어올린다.
                                        _n2n_tgt = int(getattr(cfg, "n_to_n_interval_target", 0) or 0)
                                        if _n2n_tgt >= 2:
                                            try:
                                                m2.Add(max_n_lex - min_n_lex <= best_n_range)
                                                _n2n_terms = []
                                                for _n in range(N):
                                                    if leave[_n] < join[_n]:
                                                        continue
                                                    # N전담(N-only)은 야간 강제 → n2n 간격 벌점 제외
                                                    # (같은 시프트 연속 벌점과 동일 논리, 유령 페널티 방지).
                                                    if is_n_only_profile(
                                                        getattr(roster_system.nurses[_n], "allowed_shifts", None),
                                                        use_mid=bool(getattr(cfg, "use_mid", False)),
                                                    ):
                                                        continue
                                                    _aset = set(iter_nurse_days(_n, join, leave, blocked_by_nurse))
                                                    for _d1 in sorted(_aset):
                                                        for _gap in range(2, _n2n_tgt):
                                                            _d2 = _d1 + _gap
                                                            if _d2 not in _aset:
                                                                continue
                                                            _btw = [_d1 + _k for _k in range(1, _gap)]
                                                            if any(_b not in _aset for _b in _btw):
                                                                continue
                                                            _pair = m2.NewBoolVar(f"lex_n2n_{_n}_{_d1}_{_d2}")
                                                            m2.Add(_pair <= X2(_n, _d1, night_idx_h1))
                                                            m2.Add(_pair <= X2(_n, _d2, night_idx_h1))
                                                            for _b in _btw:
                                                                m2.Add(_pair <= 1 - X2(_n, _b, night_idx_h1))
                                                            _btw_sum = sum(X2(_n, _b, night_idx_h1) for _b in _btw)
                                                            m2.Add(_pair >= X2(_n, _d1, night_idx_h1) + X2(_n, _d2, night_idx_h1) - 1 - _btw_sum)
                                                            _n2n_terms.append((_n2n_tgt - _gap) * _pair)
                                                if _n2n_terms:
                                                    m2.Minimize(sum(_n2n_terms))
                                                    import os as _os
                                                    _n2n_frac = float(_os.getenv("N2N_LEX_TIME_FRAC", "0.5"))
                                                    s2.parameters.max_time_in_seconds = max(8.0, float(tl2) * _n2n_frac)
                                                    _hint_lex_solution()
                                                    st2_n2n = s2.Solve(m2)
                                                    if st2_n2n in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                                                        _capture_lex_solution()
                                                        print(
                                                            f"{logger_prefix} 폴백2 lex 4-pass (n2n deficit): "
                                                            f"status={_cp_sat_status_to_text(st2_n2n)} "
                                                            f"deficit={int(s2.ObjectiveValue())}"
                                                        )
                                                        # n2n 동결: 후속 D/E 패스가 n2n을 망가뜨리지 않도록 lex 잠금
                                                        try:
                                                            m2.Add(sum(_n2n_terms) <= int(s2.ObjectiveValue()))
                                                        except Exception:
                                                            pass
                                                    else:
                                                        print(
                                                            f"{logger_prefix} 폴백2 lex 4-pass (n2n deficit): "
                                                            f"status={_cp_sat_status_to_text(st2_n2n)} — 3rd 결과 유지"
                                                        )
                                            except Exception as _n2n_e:
                                                print(f"{logger_prefix} 폴백2 lex 4-pass 예외: {_n2n_e}")
                                        # H1 5-pass: D/E per-nurse 균등(X축). OFF/N/n2n 동결 후 잔여 자유도로만.
                                        # lex_x2_val(stage3 warm-start hint)이 D/E까지 균등해져 stage3가 균등 hint 상속.
                                        # config de_balance_enable(기본 ON)을 따르며, env DE_LEX_ENABLE 로 override 가능.
                                        # tol 초과분만 벌해 "완전동일" 아님(밴드).
                                        try:
                                            import os as _os_de
                                            _de_env = _os_de.getenv("DE_LEX_ENABLE")
                                            _de_on = (_de_env == "1") if _de_env is not None else bool(getattr(cfg, "de_balance_enable", True))
                                            if _de_on and "E" in cfg.shift_types:
                                                _de_tol = int(_os_de.getenv("DE_BALANCE_TOL", str(getattr(cfg, "de_balance_tolerance", 2))) or 0)
                                                _de_d_idx = cfg.shift_types.index("D")
                                                _de_e_idx = cfg.shift_types.index("E")
                                                _de_excs = []
                                                for _n in range(N):
                                                    if leave[_n] < join[_n] or _n in nightonly_excluded_idx:
                                                        continue
                                                    _ad = list(iter_nurse_days(_n, join, leave, blocked_by_nurse))
                                                    _td = sum(X2(_n, d, _de_d_idx) for d in _ad)
                                                    _te = sum(X2(_n, d, _de_e_idx) for d in _ad)
                                                    _df = m2.NewIntVar(0, D, f"lex_de_diff_{_n}")
                                                    m2.Add(_df >= _td - _te)
                                                    m2.Add(_df >= _te - _td)
                                                    _ex = m2.NewIntVar(0, D, f"lex_de_exc_{_n}")
                                                    m2.Add(_ex >= _df - _de_tol)
                                                    _de_excs.append(_ex)
                                                if _de_excs:
                                                    m2.Minimize(sum(_de_excs))
                                                    s2.parameters.max_time_in_seconds = max(5.0, float(tl2) * 0.3)
                                                    _hint_lex_solution()
                                                    st2_de = s2.Solve(m2)
                                                    if st2_de in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                                                        _capture_lex_solution()
                                                        print(
                                                            f"{logger_prefix} 폴백2 lex 5-pass (D/E balance): "
                                                            f"status={_cp_sat_status_to_text(st2_de)} excess={int(s2.ObjectiveValue())}"
                                                        )
                                                    else:
                                                        print(
                                                            f"{logger_prefix} 폴백2 lex 5-pass (D/E balance): "
                                                            f"status={_cp_sat_status_to_text(st2_de)} — 4th 결과 유지"
                                                        )
                                        except Exception as _de_e:
                                            print(f"{logger_prefix} 폴백2 lex 5-pass 예외: {_de_e}")
                                    else:
                                        print(
                                            f"{logger_prefix} 폴백2 lex 3-pass (N range): "
                                            f"status={_cp_sat_status_to_text(st2_n)} — 2nd 결과 유지"
                                        )
                        else:
                            print(
                                f"{logger_prefix} 폴백2 lex 2-pass (OFF range): "
                                f"status={_cp_sat_status_to_text(st2_off)} — 1st 결과 유지"
                            )
            except Exception as _h1_e:
                print(f"{logger_prefix} 폴백2 lex H1 예외: {_h1_e}")
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
            vals = lex_safety_val.get(k) or [int(s2.Value(v)) for v in arr]
            zeros = [v for v, val in zip(arr, vals) if val == 0]
            best_safe_sum += sum(int(val) for val in vals)
            stage2_zero_locks[k] = zeros
        print(f"{logger_prefix} 최소 안전 위반 합: {best_safe_sum}")
        try:
            for k, arr in safety2.items():
                total_k = sum(lex_safety_val.get(k) or [int(s2.Value(v)) for v in arr])
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

    # ── verify-mode: stage3(선호/공정성) 건너뛰고 stage2 해로 즉시 반환 ──
    # hard_viol(커버리지+안전)은 stage2 에서 확정되고, stage3 는 이를 동결한 채
    # (아래 m3.Add(sum(safety3)==sum(safety2))) 선호만 최적화하므로 "되나/안되나"
    # 검증에는 stage2 해로 충분하다. 가장 무거운 stage3 를 건너뛰어 지연을 제거한다.
    # 기본 OFF(env gate). 일반 생성 경로는 영향 없음.
    import os as _os_vfb
    if _os_vfb.getenv("FB_VERIFY_SKIP_STAGE3") == "1":
        roster_system.roster.fill(0)
        for n in range(N):
            for d in iter_nurse_days(n, join, leave, blocked_by_nurse):
                for s in range(S):
                    if lex_x2_val.get((n, d, s), 0):
                        roster_system.roster[n, d, s] = 1
        _log_weekend_work_assignments(
            roster_system=roster_system,
            weekend_days=weekend_days,
            off_idx=off_idx,
            logger_prefix=logger_prefix,
        )
        print(
            f"{logger_prefix} [verify-mode] stage3 skip → stage2 해 반환 "
            f"(best_short={best_short}, best_safe_sum={best_safe_sum})"
        )
        return best_short == 0 and best_safe_sum == 0

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
            broad_soft=used_broad_soft,
        )
        for k in safety3.keys():
            m3.Add(sum(safety3[k]) == sum(safety2[k]))
        for n in range(N):
            for d in iter_nurse_days(n, join, leave, blocked_by_nurse):
                for s in range(S):
                    try:
                        m3.AddHint(X3(n, d, s), lex_x2_val.get((n, d, s), 0))
                    except Exception:
                        pass
        s3 = cp_model.CpSolver()
        s3.parameters.max_time_in_seconds = tl3
        s3.parameters.num_search_workers = 8
        s3.parameters.relative_gap_limit = 0.05
        _reg_s3 = getattr(m3, "_cpsat_assumption_registry", None)
        if _reg_s3 is not None:
            _reg_s3.attach_to_model()
        st3 = s3.Solve(m3)
        if st3 == cp_model.INFEASIBLE and _reg_s3 is not None:
            try:
                _fb_cores = _reg_s3.extract_conflict_cores(s3, solver_phase="fallback")
                if _fb_cores:
                    roster_system._cpsat_conflict_cores = (
                        list(getattr(roster_system, "_cpsat_conflict_cores", []) or []) + _fb_cores
                    )
                    print(f"[FallbackLex][stage3] MUS cores: {len(_fb_cores)}")
            except Exception as _mus_exc:
                print(f"[FallbackLex][stage3] MUS 추출 실패(무시): {_mus_exc}")
        print(f"{logger_prefix} 폴백3 결과: status={_cp_sat_status_to_text(st3)}")
        if st3 in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            _log_off_slack_used("stage3", s3, m3)
        if st3 not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            print(f"{logger_prefix} 폴백3 실패: 선호 단계 불가능 → 2단계 해 사용")
            roster_system.roster.fill(0)
            for n in range(N):
                for d in iter_nurse_days(n, join, leave, blocked_by_nurse):
                    for s in range(S):
                        if lex_x2_val.get((n, d, s), 0):
                            roster_system.roster[n, d, s] = 1
            _log_weekend_work_assignments(
                roster_system=roster_system,
                weekend_days=weekend_days,
                off_idx=off_idx,
                logger_prefix=logger_prefix,
            )
            return best_short == 0 and best_safe_sum == 0
        # ── 최종 lex 패스: DDDDD 보장 강화 (기본 자동 ON, AIDE_D5_LEX=0 으로 끔) ──
        # stage3 목적값을 동결(무회귀)한 뒤 D5 viol 합만 최소화하는 별도 solve.
        # 자기-게이트: 잔여 D5=0 이면 스킵(무비용) → DDDDD 남은 병동에서만 자동 재-solve.
        # payload/사용자 입력 불필요. (주의) postprocess/preceptee 경로 재유입은 별도.
        #
        # 인원수 게이트: 대형 병동은 freeze 재-solve 가 시간 내 못 풀고(UNKNOWN) 헛돎 →
        # lex 가 실제로 잘 듣고 빠른 소인원 병동(기본 N<=15)에서만 자동 실행.
        # AIDE_D5_LEX_MAXN 으로 임계 조절, AIDE_D5_LEX=0 으로 완전 비활성.
        _lex_maxn = int(_os_tl3.environ.get("AIDE_D5_LEX_MAXN", 15) or 15)
        # ── mutex lex 패스(D5-lex 앞=상위 우선): "grade/team 바로 밑" ──
        # grade/team 소프트 품질을 동결(무회귀)한 뒤 상호배제 위반합만 최소화 →
        # mutex 가 선호/야간분포보다 위, grade/team·하드보다 아래. 동결 부등식이라 infeasible 불가(soft).
        # 자기게이트(mutex=0 skip)·인원게이트(N<=_lex_maxn)는 D5-lex 와 동일.
        # mutex-lex 는 grade/team 만 동결(총품질 아님)해 D5 보다 가벼움 + 자기게이트(위반 0 skip)
        # + 타임리밋(초과 시 원해 유지)이라, D5(15)보다 높은 40 을 기본으로 실병동(19·35명 등) 커버.
        _mx_lex_maxn = int(_os_tl3.environ.get("AIDE_MUTEX_LEX_MAXN", 40) or 40)
        _mx_lex_on = (_os_tl3.environ.get("AIDE_MUTEX_LEX", "1") != "0") and (N <= _mx_lex_maxn)
        if _mx_lex_on:
            _mx_vars = getattr(m3, "_mutex_lex_vars", None) or []
            _gt_terms = getattr(m3, "_grade_team_lex_terms", None)
            if _mx_vars and _gt_terms:
                class _MxSkipLex(Exception):
                    pass
                try:
                    _mx_before = sum(int(s3.Value(v)) for v in _mx_vars)
                    print(f"{logger_prefix} 폴백3 mutex-lex 진입: stage3 위반 {_mx_before}건 "
                          f"(페어변수 {len(_mx_vars)}개)")
                    if _mx_before == 0:
                        raise _MxSkipLex  # 위반 0 → 재-solve 불필요(무비용)
                    _gt_val = int(round(s3.Value(sum(_gt_terms))))
                    m3.Add(sum(_gt_terms) >= _gt_val)   # grade/team 무회귀(penalty=음수 → >= 로 최소보장)
                    m3.Minimize(sum(_mx_vars))
                    m3.ClearHints()
                    for _hn in range(N):
                        for _hd in iter_nurse_days(_hn, join, leave, blocked_by_nurse):
                            for _hs in range(S):
                                try:
                                    m3.AddHint(X3(_hn, _hd, _hs), int(s3.Value(X3(_hn, _hd, _hs))))
                                except Exception:
                                    pass
                    _s3m = cp_model.CpSolver()
                    _s3m.parameters.max_time_in_seconds = max(8, int(tl3))
                    _s3m.parameters.num_search_workers = 8
                    _st3m = _s3m.Solve(m3)
                    if _st3m in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                        _mx_after = sum(int(_s3m.Value(v)) for v in _mx_vars)
                        if _mx_after <= _mx_before:
                            m3.Add(sum(_mx_vars) <= _mx_after)  # 이후 D5 패스가 mutex 되돌리지 못하게 락
                            s3 = _s3m
                            print(f"{logger_prefix} 폴백3 mutex-lex 패스: "
                                  f"status={_cp_sat_status_to_text(_st3m)} mutex {_mx_before}→{_mx_after}")
                        else:
                            print(f"{logger_prefix} 폴백3 mutex-lex 패스 미개선 → 원 해 유지")
                    else:
                        print(f"{logger_prefix} 폴백3 mutex-lex 패스 실패("
                              f"{_cp_sat_status_to_text(_st3m)}) → 원 해 유지")
                except _MxSkipLex:
                    pass  # 위반 0 → 스킵
                except Exception as _mx_lex_e:
                    print(f"{logger_prefix} 폴백3 mutex-lex 패스 예외: {_mx_lex_e}")
        _lex_on = (_os_tl3.environ.get("AIDE_D5_LEX", "1") != "0") and (N <= _lex_maxn)
        if _lex_on:
            _d5_vars = getattr(m3, "_ms_d5_lex_vars", None) or []
            _obj_terms = getattr(m3, "_stage3_obj_terms", None)
            if _d5_vars and _obj_terms is not None:
                class _SkipLex(Exception):
                    pass
                try:
                    # mutex-lex 패스가 앞서 m3 목적을 바꿨을 수 있으므로 ObjectiveValue 대신 항 합으로 총품질 계산
                    _obj_val = int(round(s3.Value(sum(_obj_terms))))
                    _d5_before = sum(int(s3.Value(v)) for v in _d5_vars)
                    if _d5_before == 0:
                        raise _SkipLex  # 잔여 DDDDD 없음 → 재-solve 불필요(대형병동 비용 0)
                    m3.Add(sum(_obj_terms) >= _obj_val)  # maximize 라 품질 무회귀
                    m3.Minimize(sum(_d5_vars))
                    # 직전 stage3 해를 hint 로 seed → 재solve 가 feasible incumbent 에서 출발
                    # (없으면 tight freeze 로 짧은 시간에 UNKNOWN → 개선 실패).
                    m3.ClearHints()
                    for _hn in range(N):
                        for _hd in iter_nurse_days(_hn, join, leave, blocked_by_nurse):
                            for _hs in range(S):
                                try:
                                    m3.AddHint(X3(_hn, _hd, _hs), int(s3.Value(X3(_hn, _hd, _hs))))
                                except Exception:
                                    pass
                    _s3b = cp_model.CpSolver()
                    _s3b.parameters.max_time_in_seconds = max(8, int(tl3))
                    _s3b.parameters.num_search_workers = 8
                    _st3b = _s3b.Solve(m3)
                    if _st3b in (cp_model.OPTIMAL, cp_model.FEASIBLE) and \
                            sum(int(_s3b.Value(v)) for v in _d5_vars) <= _d5_before:
                        s3 = _s3b  # 개선(또는 동률)일 때만 채택
                        print(f"{logger_prefix} 폴백3 D5-lex 패스: "
                              f"status={_cp_sat_status_to_text(_st3b)} "
                              f"D5 {_d5_before}→{int(round(_s3b.ObjectiveValue()))}")
                    else:
                        print(f"{logger_prefix} 폴백3 D5-lex 패스 실패("
                              f"{_cp_sat_status_to_text(_st3b)}) → 원 해 유지")
                except _SkipLex:
                    pass  # 잔여 D5=0 → 스킵(무비용)
                except Exception as _lex_e:
                    print(f"{logger_prefix} 폴백3 D5-lex 패스 예외: {_lex_e}")
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
        _fb_pre_ptr_idx2 = getattr(roster_system, 'preceptee_preceptor_idx', {}) or {}  # period SSOT
        for pte_idx in preceptee_indices:
            # 권위 모드: period SSOT 로 프리셉터 결정(캐시 미사용 — NULL 캐시 프리셉티 누락 방지).
            if _has_preceptee_period:
                ptr_idx = _fb_pre_ptr_idx2.get(pte_idx)
                if ptr_idx is None:
                    continue
            else:
                pid = getattr(roster_system.nurses[pte_idx], 'preceptor_id', None)
                if not pid or pid not in _fb_id_to_idx:
                    continue
                ptr_idx = _fb_id_to_idx[pid]
            # 권위 모드면 nurse_preceptee_period 기간 내 day만, 폴백이면 전체월 복사.
            _fb_follow = preceptee_follow_days.get(pte_idx)
            _fb_days_iter = (sorted(_fb_follow) if (_has_preceptee_period and _fb_follow)
                             else list(range(roster_system.num_days)))
            for _cd in _fb_days_iter:
                roster_system.roster[pte_idx, _cd, :] = roster_system.roster[ptr_idx, _cd, :]
            # 특수코드 일자는 프리셉티를 OFF로 전환 (복사한 day 한정)
            # 단, type=근무 + shift_gb=D/E/N 계열 하위코드는 근무이므로 그대로 유지
            _fb_work_sub = getattr(roster_system, '_work_sub_ids', set())
            _fb_orig_map = getattr(roster_system, '_fixed_original_shift_map', {})
            if _fb_off_idx is not None:
                for d in _fb_days_iter:
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
