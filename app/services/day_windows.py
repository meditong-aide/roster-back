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


def build_blocked_days(
    assignments: list[NurseAssignment],
    nurse_db_id: str,
    group_id: str,
    month_start: date,
    days_in_month: int,
) -> set[int]:
    """특정 간호사의 해당 월 blocked day index 집합을 반환한다.

    blocked 조건:
    - reason이 파견/휴직/퇴사 이고 source_group_id == group_id → 해당 기간 blocked
    - reason이 파견/병동이동 이고 target_group_id == group_id → 해당 기간 외 blocked (아직 미도착)
    """
    month_end = month_start + timedelta(days=days_in_month - 1)
    blocked: set[int] = set()

    for a in assignments:
        if a.nurse_id != nurse_db_id:
            continue
        if a.status == "cancelled":
            continue

        a_start = a.start_date
        a_end = a.end_date or a.expected_end_date or month_end

        # 기간이 해당 월과 겹치지 않으면 스킵
        if a_end < month_start or a_start > month_end:
            continue

        overlap_start = max(a_start, month_start)
        overlap_end = min(a_end, month_end)
        start_idx = (overlap_start - month_start).days
        end_idx = (overlap_end - month_start).days

        if a.reason in ("파견", "휴직", "퇴사") and a.source_group_id == group_id:
            # 이 그룹에서 빠지는 기간 → blocked
            for d in range(start_idx, end_idx + 1):
                blocked.add(d)

        elif a.reason == "프리셉티":
            # 프리셉티는 blocked가 아님 — 프리셉터 팔로우로 처리
            pass

        elif a.reason in ("파견", "병동이동") and a.target_group_id == group_id:
            # 이 그룹에 들어오는 간호사 — blocked 아님 (인바운드)
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
