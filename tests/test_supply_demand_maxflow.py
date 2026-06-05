"""US-001 — supply/demand max-flow state 산출기 회귀 테스트.

핵심 검증: 단순 '가용>=수요' 산술이 못 보는 '교차 시프트 경쟁'을 max-flow 가
정확히 잡는다 (같은 날 한 간호사는 1시프트만).
"""

from services.ontology_graph.supply_demand import (
    NurseSupply,
    compute_supply_demand,
    to_state_nodes,
)


def _nurse(nid, day_elig):
    return NurseSupply(nurse_id=nid, eligible_by_day=day_elig)


def test_sufficient_supply_zero_shortage():
    # day0: D 1명 요구, 2명 가용 → shortage 0
    nurses = [
        _nurse("a", {0: {"D", "E", "N"}}),
        _nurse("b", {0: {"D", "E", "N"}}),
    ]
    res = compute_supply_demand(nurses, {0: {"D": 1}})
    cell = res.cells[0]
    assert cell.required == 1
    assert cell.filled == 1
    assert cell.shortage == 0
    assert res.total_shortage() == 0
    assert res.bottleneck_cells == []


def test_simple_shortage_detected():
    # day0: N 3명 요구, N 가능 2명 → shortage 1
    nurses = [
        _nurse("a", {0: {"N"}}),
        _nurse("b", {0: {"N"}}),
    ]
    res = compute_supply_demand(nurses, {0: {"N": 3}})
    cell = next(c for c in res.cells if c.shift_code == "N")
    assert cell.required == 3
    assert cell.available_eligible == 2
    assert cell.filled == 2
    assert cell.shortage == 1
    assert cell.is_bottleneck


def test_cross_shift_competition_maxflow_catches_what_arithmetic_misses():
    """단순 산술은 통과로 오판하지만 max-flow 는 부족을 잡는 케이스.

    day0: D 1, E 1, N 1 요구 (총 3). 간호사 2명, 둘 다 D/E/N 전부 가능.
    - 단순 산술(시프트별 available): D=2,E=2,N=2 → 각각 >=요구 → '부족 없음' 오판.
    - 실제: 2명이 하루 1시프트씩 → 최대 2슬롯만 채움 → 총 3 중 1 부족.
    """
    nurses = [
        _nurse("a", {0: {"D", "E", "N"}}),
        _nurse("b", {0: {"D", "E", "N"}}),
    ]
    res = compute_supply_demand(nurses, {0: {"D": 1, "E": 1, "N": 1}})
    # 단순 산술 관점: 각 시프트 available_eligible == 2 >= 1 (부족 0 으로 오판)
    for c in res.cells:
        assert c.available_eligible == 2
    # max-flow 관점: 총 채움 2, 총 요구 3 → 총 부족 1
    total_filled = sum(c.filled for c in res.cells)
    assert total_filled == 2
    assert res.total_shortage() == 1
    assert res.day_shortage[0] == 1


def test_monthly_shortage_aggregation():
    # 3일 모두 N 2요구, N 가능 1명 → 매일 1부족, 월 합 3
    nurses = [_nurse("a", {d: {"N"} for d in range(3)})]
    req = {d: {"N": 2} for d in range(3)}
    res = compute_supply_demand(nurses, req)
    assert res.monthly_shortage["N"] == 3
    assert all(res.day_shortage[d] == 1 for d in range(3))


def test_to_state_nodes_marks_bottleneck_hard():
    nurses = [_nurse("a", {0: {"N"}})]
    res = compute_supply_demand(nurses, {0: {"N": 2}})
    nodes = to_state_nodes(res)
    assert len(nodes) == 1
    n = nodes[0]
    assert n.kind == "state"
    assert n.state_type == "supply_demand"
    assert n.severity == "hard"            # shortage>0 → hard
    assert n.evidence["required"] == 2
    assert n.evidence["filled"] == 1
    assert n.evidence["shortage"] == 1
    assert n.node_id == "state:supply_demand:d0:N"


def test_eligibility_excludes_fixed_off():
    # 간호사가 day0 에 eligible 집합이 비면(=fixed off) 가용 0
    nurses = [
        NurseSupply(nurse_id="a", eligible_by_day={0: set()}),
        _nurse("b", {0: {"D"}}),
    ]
    res = compute_supply_demand(nurses, {0: {"D": 2}})
    cell = res.cells[0]
    assert cell.available_eligible == 1   # b 만
    assert cell.shortage == 1
