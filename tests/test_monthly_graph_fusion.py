"""월별 max-flow shortage 를 통합 그래프에 융합 → recommender 가 월별 원인도 랭킹.

per-day flow 만으로는 하루 교차경쟁 병목만 본다. 월 총량·야간cap 부족(하루엔 인원이
충분해 per-day 는 조용)은 monthly_supply_demand 를 build_unified_graph 에 넘겨야 그래프에
실리고, recommend_actions 가 일수요 감축 복구를 랭킹한다.
"""

from __future__ import annotations

from services.ontology_graph.builder import UnifiedGraphInput, build_unified_graph
from services.ontology_graph.recommender import recommend_actions
from services.ontology_graph.supply_demand import (
    NurseSupply,
    compute_monthly_supply_demand,
)

DAYS = 5


def _gi():
    ids = [f"n{i}" for i in range(20)]
    return ids, UnifiedGraphInput(
        nurses=[{"nurse_id": i, "grade": 1, "team_id": None} for i in ids],
        num_days=DAYS, work_shifts=["D", "E", "N"],
        # 하루 수요 18 ≤ 20명 → per-day 병목 없음
        requirements_by_day={d: {"D": 6, "E": 6, "N": 6} for d in range(DAYS)},
    )


def _monthly(ids):
    # 근무가능일 2일뿐 → 총 capacity 40 < 월 수요 90 → 월 부족 (per-day 는 못 봄)
    sup = [NurseSupply(nurse_id=i, eligible_by_day={d: {"D", "E", "N"} for d in range(DAYS)}) for i in ids]
    return compute_monthly_supply_demand(
        sup, {"D": 6 * DAYS, "E": 6 * DAYS, "N": 6 * DAYS},
        workdays_by_nurse={i: 2 for i in ids})


def test_per_day_ok_no_action_without_monthly():
    ids, gi = _gi()
    # 월별 안 넘기면 per-day 만 → 병목 없음 → 추천 없음
    assert recommend_actions(build_unified_graph(gi)) == []


def test_monthly_shortage_surfaces_ranked_recovery():
    ids, gi = _gi()
    mon = _monthly(ids)
    assert mon.total_shortage() > 0
    g = build_unified_graph(gi, monthly_supply_demand=mon)
    acts = recommend_actions(g)
    assert acts, "월별 부족인데 추천 액션 없음"
    # CoverageMin(일수요) 완화가 후보에 있고 primary 로 랭크
    top = acts[0]
    assert top.target_family == "CoverageMin"
    assert top.is_primary
    assert top.delta.get("direction") == "decrease"
    # 감축량은 per-day 등가(월부족/일수)로 합리적 (전월수요보다 훨씬 작아야)
    assert 0 < int(top.delta.get("amount", 0)) < 6 * DAYS
