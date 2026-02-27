"""룩어헤드 구간 전용 제약 추가 모듈.

- 일별 OFF 상한(고정 OFF vs 선택 OFF 분리)
- (선택) 룩어헤드 OFF 분산 패널티

cp_sat_basic에서 호출하며, m, X 등은 호출 측에서 생성한 것을 넘긴다.
"""

from __future__ import annotations

import logging
from typing import Callable

logger = logging.getLogger(__name__)


def add_lookahead_off_cap_constraints(
    m,
    X,
    N: int,
    D_phys: int,
    D_ext: int,
    join: list[int],
    leave_ext: list[int],
    off_idx: int,
    get_need_for_day: Callable[[int], dict],
    fixed_off_cells_lookahead: set[tuple[int, int]],
    shift_codes: list[str],
    logger_prefix: str = "[Lookahead]",
) -> None:
    """룩어헤드 구간 일별 OFF 상한을 추가한다.

    고정 OFF(주말휴무/주휴)와 선택 OFF를 분리해,
    selectable_off_cnt[d] <= max(0, total_off_cap[d] - fixed_off_cnt[d]) 로 둔다.

    Args:
        m: CpModel.
        X: (n, d, s) 변수.
        N: 간호사 수.
        D_phys: 당월 물리 일수.
        D_ext: 확장 일수.
        join: join[n] (inclusive).
        leave_ext: leave_ext[n] (inclusive).
        off_idx: O shift 인덱스.
        get_need_for_day: d -> { code: need } (해당 일자 D/E/N 요구치).
        fixed_off_cells_lookahead: 룩어헤드 구간에서 이미 O로 고정된 (n, d) 집합.
        shift_codes: D/E/N 등 코드 리스트 (need 키와 매칭).
        logger_prefix: 로그 접두사.
    """
    if D_ext <= D_phys:
        return
    try:
        work_codes = [c for c in shift_codes if c in ("D", "E", "N")]
        for t in range(D_ext - D_phys):
            d = D_phys + t
            need_map = get_need_for_day(d)
            need_total = sum(int(need_map.get(c, 0) or 0) for c in work_codes)
            fixed_cnt = sum(1 for n in range(N) if (n, d) in fixed_off_cells_lookahead)
            # 해당 일자에 변수가 있는 인원 수 (근무 가능 인원)
            active_n = sum(1 for n in range(N) if join[n] <= d <= leave_ext[n])
            total_cap = max(0, active_n - need_total)
            selectable_cap = max(0, total_cap - fixed_cnt)
            # 선택 OFF 합 <= selectable_cap
            selectable_off = [
                X(n, d, off_idx)
                for n in range(N)
                if join[n] <= d <= leave_ext[n] and (n, d) not in fixed_off_cells_lookahead
            ]
            if selectable_off:
                m.Add(sum(selectable_off) <= selectable_cap)
        logger.info(
            "%s 룩어헤드 일별 OFF 상한 적용: %s일, 고정 OFF 반영",
            logger_prefix,
            D_ext - D_phys,
        )
    except Exception as e:
        logger.warning("%s 룩어헤드 OFF 상한 적용 실패: %s", logger_prefix, e)


def add_lookahead_distribution_penalty_terms(
    m,
    X,
    N: int,
    D_phys: int,
    D_ext: int,
    join: list[int],
    leave_ext: list[int],
    off_idx: int,
    fixed_off_cells_lookahead: set[tuple[int, int]],
    weight: int = 1,
) -> list:
    """룩어헤드 구간 OFF 분산 패널티 항을 만들어 반환한다.

    max_off_in_lookahead 를 최소화하는 형태로, 쏠림을 완화한다.
    가중치는 작게 두어 당월 품질을 희생하지 않도록 한다.

    Args:
        m, X, N, D_phys, D_ext, join, leave_ext, off_idx, fixed_off_cells_lookahead: 상동.
        weight: 패널티 가중치 (기본 1, 매우 작게 권장).

    Returns:
        목적함수에 더할 항 리스트 (음수 * max_off 변수 등).
    """
    if D_ext <= D_phys or weight <= 0:
        return []
    obj_terms = []
    try:
        K = D_ext - D_phys
        # 일별 OFF 수 (선택+고정) 상한을 최소화하는 대신, 간단히 일별 OFF 합의 최대를 최소화
        max_var = m.NewIntVar(0, N, "lookahead_max_off")
        for t in range(K):
            d = D_phys + t
            day_off = [
                X(n, d, off_idx)
                for n in range(N)
                if join[n] <= d <= leave_ext[n]
            ]
            if day_off:
                m.Add(max_var >= sum(day_off))
        obj_terms.append(-weight * max_var)
    except Exception as e:
        logger.warning("[Lookahead] 분산 패널티 항 생성 실패: %s", e)
    return obj_terms
