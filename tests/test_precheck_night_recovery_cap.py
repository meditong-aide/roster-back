"""precheck 월 야간 capacity 에 2N→2OFF / 3N→2OFF 회복 상한 반영 (blocking).

안전성 핵심: recovery 상한은 **정확한 최대 N**(DP)이어야 blocking 에 써도 false
positive 가 없다. 아래 test_dp_matches_bruteforce 가 DP == 독립 brute-force 임을
증명(=valid & tight upper bound). 나머지는 다양한 결합 케이스.
"""
from __future__ import annotations

import sys
from itertools import product
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from services.precheck.team_grade_precheck import (  # noqa: E402
    PrecheckInput,
    PrecheckNurse,
    check_monthly_night_capacity,
    max_nights_under_recovery,
)

SHORT = "MONTHLY_NIGHT_CAPACITY_SHORTAGE"


# ── 독립 brute-force (열거) : 회복규칙 유효 배열 중 최대 N ──
def _bf_max(span: int, block: int) -> int:
    def valid(seq) -> bool:
        i, n = 0, len(seq)
        while i < n:
            if seq[i] == 1:  # N
                run = 0
                while i < n and seq[i] == 1:
                    run += 1
                    i += 1
                if run > block:
                    return False
                if run == block:  # block 연속 → 이어서 2 OFF (월말이면 면제)
                    if i < n and seq[i] != 0:
                        return False
                    if i + 1 < n and seq[i + 1] != 0:
                        return False
            else:
                i += 1
        return True

    best = 0
    for combo in product((0, 1), repeat=span):
        if valid(combo):
            best = max(best, sum(combo))
    return best


def test_dp_matches_bruteforce():
    # DP(정확값)가 독립 brute-force 와 span 1..16, block 2/3 에서 완전 일치 → valid upper bound 증명
    for block in (2, 3):
        for span in range(0, 17):
            assert max_nights_under_recovery(span, block) == _bf_max(span, block), (span, block)


# ── precheck 입력 헬퍼 (N=1 을 앞 total_n 일에만) ──
def _inp(num_days, nurses, total_n, cfg_extra=None):
    # total_n 을 num_days 에 고르게 분산(일별 N 이 num_days 초과 수요도 표현 가능).
    base, rem = divmod(total_n, num_days)
    by_day = [{"D": 0, "E": 0, "N": base + (1 if d < rem else 0)} for d in range(num_days)]
    cfg = {
        "daily_shift_requirements_by_day": by_day,
        "daily_shift_requirements": {"D": 0, "E": 0, "N": 0},
        "max_night_shifts_per_month": 31,
    }
    cfg.update(cfg_extra or {})
    return PrecheckInput(
        num_days=num_days, nurses=nurses, teams=[], roster_config=cfg,
        team_coverage={}, grade_constraints={},
    )


def _nurse(i=0, join=0, leave=19, night_cap=None):
    return PrecheckNurse(
        nurse_id=f"n{i}", join_day=join, leave_day=leave,
        allowed_shifts=["N"], night_cap=night_cap,
    )


def _codes(inp):
    return {x.get("reason_code") for x in check_monthly_night_capacity(inp)}


def test_recovery_off_keeps_old_behavior():
    # 회복 OFF: cap=span=20 ≥ 수요 20 → shortage 없음 (기존 동작 유지)
    assert SHORT not in _codes(_inp(20, [_nurse(leave=19)], 20))


def test_2n2off_boundary_from_dp():
    cap = max_nights_under_recovery(20, 2)          # span20 정확 상한
    assert SHORT not in _codes(_inp(20, [_nurse(leave=19)], cap, {"two_offs_after_two_nig": True}))
    assert SHORT in _codes(_inp(20, [_nurse(leave=19)], cap + 1, {"two_offs_after_two_nig": True}))


def test_3n2off_boundary_from_dp():
    cap = max_nights_under_recovery(20, 3)          # 3N→2OFF 는 NN O 로 더 여유(정확 DP)
    assert SHORT not in _codes(_inp(20, [_nurse(leave=19)], cap, {"two_offs_after_three_nig": True}))
    assert SHORT in _codes(_inp(20, [_nurse(leave=19)], cap + 1, {"two_offs_after_three_nig": True}))


def test_2n2off_dominates_when_both_on():
    cap2 = max_nights_under_recovery(20, 2)
    both = {"two_offs_after_two_nig": True, "two_offs_after_three_nig": True}
    assert SHORT in _codes(_inp(20, [_nurse(leave=19)], cap2 + 1, both))


def test_multi_nurse_aggregation():
    # 2명, 각 span20 → 총 cap = 2 × MNR(20,2)
    cap = max_nights_under_recovery(20, 2) * 2
    nurses = [_nurse(0, leave=19), _nurse(1, leave=19)]
    assert SHORT not in _codes(_inp(20, nurses, cap, {"two_offs_after_two_nig": True}))
    assert SHORT in _codes(_inp(20, nurses, cap + 1, {"two_offs_after_two_nig": True}))


def test_partial_span():
    # 중도 입사(5일차)~24일차 = span20 → 회복 상한은 span 기준
    cap = max_nights_under_recovery(20, 2)
    n = _nurse(join=5, leave=24)
    assert SHORT not in _codes(_inp(30, [n], cap, {"two_offs_after_two_nig": True}))
    assert SHORT in _codes(_inp(30, [n], cap + 1, {"two_offs_after_two_nig": True}))


def test_max_night_binds_tighter_than_recovery():
    # max_night=3 < recovery cap → 최종 cap=3 (recovery 가 상한을 느슨하게 만들지 않음)
    cfg = {"two_offs_after_two_nig": True, "max_night_shifts_per_month": 3}
    assert SHORT not in _codes(_inp(20, [_nurse(leave=19)], 3, cfg))
    assert SHORT in _codes(_inp(20, [_nurse(leave=19)], 4, cfg))


def test_night_cap_binds_tighter():
    # per-nurse night_cap=2 → cap=2
    n = _nurse(leave=19, night_cap=2)
    assert SHORT not in _codes(_inp(20, [n], 2, {"two_offs_after_two_nig": True}))
    assert SHORT in _codes(_inp(20, [n], 3, {"two_offs_after_two_nig": True}))
