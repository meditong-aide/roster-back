"""
간호사별 근무 가능일 계산 모듈
- nurse_assignment 기반 불연속 구간 처리
- CP-SAT 솔버에서 range(join[n], leave[n]+1) 대체용
"""

from datetime import date, timedelta
from typing import Optional
from db.models import NurseAssignment
import logging

logger = logging.getLogger(__name__)


def is_full_month_assignment(
    a_start: date, a_end_actual: date | None, month_start: date, days_in_month: int
) -> bool:
    """assignment가 해당 월 전체를 커버하는지 판별.
    True = target 독립 생성 (전달 없음)
    """
    if a_end_actual is None:
        return False
    month_end = month_start + timedelta(days=days_in_month - 1)
    return a_start <= month_start and a_end_actual >= month_end


def is_source_generated_month(
    a_start: date, a_end_actual: date | None, month_start: date, days_in_month: int
) -> bool:
    """source가 생성하고 target에 전달하는 월인지 판별.

    True  = source 생성 + 전달 (월 일부만 커버: mid-start, end month)
    False = target 독립 생성 (full month, 중간 월)
    """
    _is_start = (a_start.year == month_start.year and a_start.month == month_start.month)
    _is_end = (
        a_end_actual is not None
        and a_end_actual.year == month_start.year
        and a_end_actual.month == month_start.month
    )
    if not (_is_start or _is_end):
        return False  # 중간 월 → target 독립
    if is_full_month_assignment(a_start, a_end_actual, month_start, days_in_month):
        return False  # full month → target 독립
    return True  # 월 일부만 커버 → source 생성


def build_blocked_days(
    assignments: list[NurseAssignment],
    nurse_db_id: str,
    group_id: str,
    month_start: date,
    days_in_month: int,
    is_inbound: bool = False,
) -> set[int]:
    """특정 간호사의 해당 월 blocked day index 집합을 반환한다.

    blocked 조건:
    - reason이 파견/휴직/퇴사/병동이동 이고 source_group_id == group_id → 해당 기간 blocked (아웃바운드)
    - is_inbound=True: 인바운드 간호사 — assignment 기간 외의 모든 날이 blocked
    """
    month_end = month_start + timedelta(days=days_in_month - 1)
    blocked: set[int] = set()

    if is_inbound:
        # 인바운드: assignment 기간만 active, 나머지 blocked
        active_days: set[int] = set()
        for a in assignments:
            if a.nurse_id != nurse_db_id:
                continue
            if a.status == "cancelled":
                continue
            if a.reason not in ("파견", "병동이동"):
                continue
            if a.target_group_id != group_id:
                continue

            a_start = a.start_date
            a_end = a.end_date or a.expected_end_date or month_end

            if a_end < month_start or a_start > month_end:
                continue

            overlap_start = max(a_start, month_start)
            overlap_end = min(a_end, month_end)
            start_idx = (overlap_start - month_start).days
            end_idx = (overlap_end - month_start).days
            for d in range(start_idx, end_idx + 1):
                active_days.add(d)

        # active_days 외의 모든 날 → blocked
        for d in range(days_in_month):
            if d not in active_days:
                blocked.add(d)
        return blocked

    # 아웃바운드 (기존 로직)
    for a in assignments:
        if a.nurse_id != nurse_db_id:
            continue
        if a.status == "cancelled":
            continue

        a_start = a.start_date
        a_end = a.end_date or a.expected_end_date or month_end

        if a_end < month_start or a_start > month_end:
            continue

        overlap_start = max(a_start, month_start)
        overlap_end = min(a_end, month_end)
        start_idx = (overlap_start - month_start).days
        end_idx = (overlap_end - month_start).days

        if a.reason in ("파견", "휴직", "퇴사", "병동이동") and a.source_group_id == group_id:
            # 파견/병동이동: source가 생성하는 월은 blocking 안 함, 그 외(full/중간)는 blocked
            if a.reason in ("파견", "병동이동"):
                _a_end_actual = a.end_date or a.expected_end_date
                if is_source_generated_month(a_start, _a_end_actual, month_start, days_in_month):
                    continue  # source 생성 → blocking 안 함
                # full month / 중간 월 → blocked
            for d in range(start_idx, end_idx + 1):
                blocked.add(d)

        elif a.reason == "프리셉티":
            pass

    return blocked


def iter_nurse_days(
    n: int,
    join: list[int],
    leave: list[int],
    blocked_by_nurse: Optional[dict[int, set[int]]] = None,
):
    """range(join[n], leave[n]+1)을 대체하는 iterator.
    blocked_by_nurse가 있으면 blocked day를 제외한다.
    """
    blocked = blocked_by_nurse.get(n, set()) if blocked_by_nurse else set()
    for d in range(join[n], leave[n] + 1):
        if d not in blocked:
            yield d


def build_active_days(
    N: int,
    join: list[int],
    leave: list[int],
    blocked_by_nurse: Optional[dict[int, set[int]]] = None,
) -> set[tuple[int, int]]:
    """기존 active_days 셋 빌드를 대체한다.
    {(n, d) for n in range(N) for d in range(join[n], leave[n]+1)} 대체
    """
    days: set[tuple[int, int]] = set()
    for n in range(N):
        for d in iter_nurse_days(n, join, leave, blocked_by_nurse):
            days.add((n, d))
    return days
