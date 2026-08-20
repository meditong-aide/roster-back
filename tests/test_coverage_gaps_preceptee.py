"""_compute_coverage_gaps 의 프리셉티 카운트 일관성 테스트.

정책(사용자 지시): preceptee_shift_count=False 면 프리셉티는 커버리지상 '없는 인력'.
따라서 부족 리포트도 실인원만 세야 한다(솔버 need/supply 와 동일 기준). 이전에는
전원(프리셉티 포함)을 세어 솔버와 어긋났다 → phantom 부족/과소보고의 씨앗.

여기서는 솔버 없이 경량 roster_system 스텁으로 리포트 회계만 직접 검증한다.
"""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np

from services.roster_create_service import _compute_coverage_gaps


def _rs(roster, *, preceptee_shift_count, pte_days, authoritative, num_nurses):
    """최소 roster_system 스텁. roster[n,d,s] 인덱싱 = numpy."""
    cfg = SimpleNamespace(
        shift_types=["D", "E", "N"],
        daily_shift_requirements={"D": 3},
        daily_shift_requirements_by_day=None,
        preceptee_shift_count=preceptee_shift_count,
    )
    return SimpleNamespace(
        config=cfg,
        roster=roster,
        num_days=1,
        nurses=[SimpleNamespace(preceptor_id=None) for _ in range(num_nurses)],
        preceptee_follow_days=pte_days,
        preceptee_period_authoritative=authoritative,
    )


def _roster_all_D(num_nurses):
    """모든 간호사가 day0 에 D(shift idx 0) 근무."""
    r = np.zeros((num_nurses, 1, 3), dtype=int)
    r[:, 0, 0] = 1
    return r


def test_preceptee_excluded_reports_real_shortage():
    """req D=3, 실인원 2 + 프리셉티 1 이 D → count=False 면 실인원 2로 부족 1 보고."""
    r = _roster_all_D(3)  # nurse 0,1 = real, nurse 2 = preceptee
    rs = _rs(r, preceptee_shift_count=False, pte_days={2: {0}}, authoritative=True, num_nurses=3)
    gaps = _compute_coverage_gaps(rs)
    assert len(gaps) == 1
    assert gaps[0]["assigned"] == 2 and gaps[0]["short"] == 1


def test_preceptee_counted_when_flag_true():
    """동일 배치라도 count=True 면 프리셉티 포함 3명 → 부족 없음(기존 동작 보존)."""
    r = _roster_all_D(3)
    rs = _rs(r, preceptee_shift_count=True, pte_days={2: {0}}, authoritative=True, num_nurses=3)
    assert _compute_coverage_gaps(rs) == []


def test_no_phantom_when_real_meets_req():
    """실인원 3 + 프리셉티 1 이 D, req=3 → count=False 여도 실인원으로 충족 → 부족 0.

    '오프 넘쳐나는데 부족' phantom 방지의 핵심: 실인원이 req 를 채우면 프리셉티 유무와
    무관하게 부족이 없어야 한다.
    """
    r = _roster_all_D(4)  # nurse 0,1,2 = real, nurse 3 = preceptee
    rs = _rs(r, preceptee_shift_count=False, pte_days={3: {0}}, authoritative=True, num_nurses=4)
    assert _compute_coverage_gaps(rs) == []


def test_fallback_no_period_map_treats_preceptor_id_holders():
    """맵 없음(폴백) + preceptor_id 보유자 = 전체월 프리셉티로 제외."""
    r = _roster_all_D(3)
    rs = _rs(r, preceptee_shift_count=False, pte_days={}, authoritative=False, num_nurses=3)
    rs.nurses[2].preceptor_id = "someone"  # nurse 2 = 프리셉티(캐시 기반)
    gaps = _compute_coverage_gaps(rs)
    assert len(gaps) == 1 and gaps[0]["assigned"] == 2
