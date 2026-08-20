"""per-nurse 시퀀스 feasibility DP 검증.

핵심: DP(무완성=infeasible) 가 독립 brute-force(모든 D/E/N/O 배열 열거) 와 완전 일치 →
'증명된 불가능'만 반환(false positive 없음). + 구체 시나리오(회복상한 vs n_min,
고정셀 전이충돌).
"""
from __future__ import annotations

import sys
from itertools import product
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from services.precheck.per_nurse_sequence_feasibility import (  # noqa: E402
    nurse_sequence_infeasible,
)


def _bf_exists(num_days, allowed, fixed, K, L, ban_nd, ban_ed, ban_ne, rec_block, not_one, n_min, n_cap):
    """독립 brute-force: 규칙 다 지키는 완성이 하나라도 존재하면 True."""
    aset = set(allowed) if allowed else {"D", "E", "N"}

    def valid(seq):
        for d, s in fixed.items():
            if seq[d] != s:
                return False
        for s in seq:
            if s != "O" and s not in aset:
                return False
        for i in range(1, len(seq)):
            p, s = seq[i - 1], seq[i]
            if s == "D" and ((p == "N" and ban_nd) or (p == "E" and ban_ed)):
                return False
            if s == "E" and p == "N" and ban_ne:
                return False
        if K is not None:
            run = 0
            for s in seq:
                run = run + 1 if s != "O" else 0
                if run > K:
                    return False
        if L is not None:
            run = 0
            for s in seq:
                run = run + 1 if s == "N" else 0
                if run > L:
                    return False
        if rec_block:
            i, n = 0, len(seq)
            while i < n:
                if seq[i] == "N":
                    run = 0
                    while i < n and seq[i] == "N":
                        run += 1
                        i += 1
                    if run > rec_block:
                        return False
                    if run == rec_block:
                        if i < n and seq[i] != "O":
                            return False
                        if i + 1 < n and seq[i + 1] != "O":
                            return False
                else:
                    i += 1
        if not_one:
            for i in range(len(seq)):
                if seq[i] == "N":
                    left = i > 0 and seq[i - 1] == "N"
                    right = i < len(seq) - 1 and seq[i + 1] == "N"
                    if not (left or right):
                        return False
        nights = sum(1 for s in seq if s == "N")
        if nights < n_min or nights > n_cap:
            return False
        return True

    return any(valid(seq) for seq in product("DENO", repeat=num_days))


def test_dp_matches_bruteforce():
    # 여러 제약 조합 × 작은 num_days 에서 DP == brute-force
    configs = [
        dict(K=None, L=None, ban_nd=False, ban_ed=False, ban_ne=False, rec_block=0, not_one=False, n_min=0, n_cap=99),
        dict(K=3, L=2, ban_nd=True, ban_ed=True, ban_ne=True, rec_block=0, not_one=False, n_min=0, n_cap=99),
        dict(K=None, L=None, ban_nd=False, ban_ed=False, ban_ne=False, rec_block=2, not_one=False, n_min=0, n_cap=99),
        dict(K=None, L=None, ban_nd=False, ban_ed=False, ban_ne=False, rec_block=3, not_one=False, n_min=0, n_cap=99),
        dict(K=None, L=None, ban_nd=False, ban_ed=False, ban_ne=False, rec_block=0, not_one=True, n_min=0, n_cap=99),
        dict(K=4, L=3, ban_nd=True, ban_ed=False, ban_ne=True, rec_block=2, not_one=True, n_min=2, n_cap=4),
    ]
    for num_days in range(1, 8):
        for c in configs:
            dp_infeasible = nurse_sequence_infeasible(
                num_days=num_days, allowed=set(), fixed={},
                max_consecutive_work=c["K"], max_consecutive_nights=c["L"],
                ban_n_to_d=c["ban_nd"], ban_e_to_d=c["ban_ed"], ban_n_to_e=c["ban_ne"],
                two_offs_after_two_nig=(c["rec_block"] == 2),
                two_offs_after_three_nig=(c["rec_block"] == 3),
                not_one_night=c["not_one"], n_min=c["n_min"], n_max=c["n_cap"],
            )
            bf = not _bf_exists(num_days, set(), {}, c["K"], c["L"], c["ban_nd"], c["ban_ed"],
                                c["ban_ne"], c["rec_block"], c["not_one"], c["n_min"], c["n_cap"])
            assert dp_infeasible == bf, (num_days, c)


def test_dp_matches_bruteforce_with_fixed():
    # 고정셀 포함 조합도 일치
    for num_days in range(2, 7):
        fixed = {0: "N", num_days - 1: "D"}
        for ban in (True, False):
            dp = nurse_sequence_infeasible(
                num_days=num_days, allowed=set(), fixed=fixed,
                max_consecutive_work=None, max_consecutive_nights=None,
                ban_n_to_d=ban, ban_e_to_d=False, ban_n_to_e=False,
                two_offs_after_two_nig=False, two_offs_after_three_nig=False,
                not_one_night=False, n_min=0, n_max=99,
            )
            bf = not _bf_exists(num_days, set(), fixed, None, None, ban, False, False, 0, False, 0, 99)
            assert dp == bf, (num_days, ban, fixed)


def test_fixed_cell_transition_conflict():
    # day0=N(고정), day1=D(고정), N→D 금지 → 혼자서 불가능
    assert nurse_sequence_infeasible(
        num_days=3, allowed=set(), fixed={0: "N", 1: "D"},
        ban_n_to_d=True,
    ) is True
    # 금지 해제하면 가능
    assert nurse_sequence_infeasible(
        num_days=3, allowed=set(), fixed={0: "N", 1: "D"},
        ban_n_to_d=False,
    ) is False


def test_n_min_exceeds_recovery_cap():
    # 30일, 2N→2OFF 하 최대 N ≈ 16. n_min=20 이면 혼자서 불가능.
    assert nurse_sequence_infeasible(
        num_days=30, allowed={"N"}, fixed={},
        two_offs_after_two_nig=True, n_min=20,
    ) is True
    # n_min=15 는 회복상한 이하 → 가능
    assert nurse_sequence_infeasible(
        num_days=30, allowed={"N"}, fixed={},
        two_offs_after_two_nig=True, n_min=15,
    ) is False


def test_no_constraints_feasible():
    assert nurse_sequence_infeasible(num_days=30, allowed=set(), fixed={}) is False
