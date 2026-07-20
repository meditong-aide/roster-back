"""_compute_coverage_gaps 원인 분류: 부족 셀에 eligible 수 + reason 태그.

사용자 요구: infeasible 대신 부족인원으로 표를 내보내되, 각 부족 셀의 '이유'를 정확히
알린다 — 그 시프트 정책상 가능 인원(allowed_shifts)이 요구보다 적으면 eligibility_shortage
(예: 야간 불가 인원 과다 → N 가능 인원 < 요구), 아니면 capacity_shortage.
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np

from services.roster_create_service import _compute_coverage_gaps


def _rs():
    shift_types = ["D", "E", "N", "O"]
    nurses = [
        SimpleNamespace(allowed_shifts=["N"]),        # N 전담
        SimpleNamespace(allowed_shifts=[]),           # 전 시프트 가능
        SimpleNamespace(allowed_shifts=["D", "E"]),   # 주간전담
        SimpleNamespace(allowed_shifts=["D", "E"]),
        SimpleNamespace(allowed_shifts=["D", "E"]),
    ]
    cfg = SimpleNamespace(shift_types=shift_types,
                          daily_shift_requirements={"D": 2, "E": 0, "N": 3},
                          daily_shift_requirements_by_day=None)
    roster = np.zeros((5, 1, 4))
    # N 요구 3, N 가능 2명(전담+전가능) 둘 다 배정 → 배정 2 < 3
    roster[0, 0, 2] = 1
    roster[1, 0, 2] = 1
    # D 요구 2, 1명만 배정 → 배정 1 < 2 (D 가능 4명 = 전원 − N전담)
    roster[2, 0, 0] = 1
    return SimpleNamespace(config=cfg, nurses=nurses, num_days=1, roster=roster)


def test_eligibility_shortage_reason():
    gaps = _compute_coverage_gaps(_rs())
    n_gap = next(g for g in gaps if g["shift"] == "N")
    assert (n_gap["need"], n_gap["assigned"], n_gap["short"]) == (3, 2, 1)
    assert n_gap["eligible"] == 2                    # N 가능 2명
    assert n_gap["reason"] == "eligibility_shortage"  # 2 < 3 → 자격 부족


def test_capacity_shortage_reason():
    gaps = _compute_coverage_gaps(_rs())
    d_gap = next(g for g in gaps if g["shift"] == "D")
    assert d_gap["eligible"] == 4                    # D 가능 4명(N전담 제외)
    assert d_gap["short"] == 1
    assert d_gap["reason"] == "capacity_shortage"    # 4 >= 2 → 자격은 충분, 배치 문제


def test_no_gap_when_fully_covered():
    rs = _rs()
    rs.roster[2, 0, 0] = 1  # D 2명 채움
    rs.roster[3, 0, 0] = 1
    rs.config.daily_shift_requirements = {"D": 2, "E": 0, "N": 2}  # N 요구 2로 낮춤
    gaps = _compute_coverage_gaps(rs)
    assert all(g["shift"] != "D" for g in gaps)      # D 부족 없음
