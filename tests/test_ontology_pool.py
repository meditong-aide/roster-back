"""Pool graph builder 회귀 테스트.

7월 N전담 시나리오 (엄애란 G1 무소속 / 장세현 G1 Team2 / 이서연 G0 Team3)
이 어떤 pool shortage 를 만들어내는지 deterministic 하게 검증한다.
"""

from __future__ import annotations

from services.ontology_pool import build_pool_snapshot, PoolSnapshot
from services.precheck.team_grade_precheck import PrecheckInput, PrecheckNurse


def _mk_input() -> PrecheckInput:
    """7월 시나리오 축소판 — 4팀 일부 + 3 N전담 + 31일."""
    return PrecheckInput(
        num_days=31,
        nurses=[
            # Team 1
            PrecheckNurse(nurse_id="박지연",   grade=1, team_id=1, join_day=0, leave_day=30),
            PrecheckNurse(nurse_id="강유빈",   grade=0, team_id=1, join_day=0, leave_day=30),
            PrecheckNurse(nurse_id="박지은",   grade=3, team_id=1, join_day=0, leave_day=30),
            PrecheckNurse(nurse_id="김근영",   grade=0, team_id=1, join_day=0, leave_day=30),
            # Team 2 — 장세현 N전담
            PrecheckNurse(nurse_id="김한별",   grade=1, team_id=2, join_day=0, leave_day=30),
            PrecheckNurse(nurse_id="최지수",   grade=0, team_id=2, join_day=0, leave_day=30),
            PrecheckNurse(nurse_id="이은채B",  grade=3, team_id=2, join_day=0, leave_day=30),
            PrecheckNurse(nurse_id="이도이",   grade=0, team_id=2, join_day=0, leave_day=30),
            PrecheckNurse(nurse_id="장세현",   grade=1, team_id=2, allowed_shifts=["N"], join_day=0, leave_day=30),
            # Team 3 — 이서연 N전담
            PrecheckNurse(nurse_id="이유림",   grade=1, team_id=3, join_day=0, leave_day=30),
            PrecheckNurse(nurse_id="김예빈",   grade=0, team_id=3, join_day=0, leave_day=30),
            PrecheckNurse(nurse_id="김원아",   grade=1, team_id=3, join_day=0, leave_day=30),
            PrecheckNurse(nurse_id="표유진",   grade=3, team_id=3, join_day=0, leave_day=30),
            PrecheckNurse(nurse_id="이서연",   grade=0, team_id=3, allowed_shifts=["N"], join_day=0, leave_day=30),
            # 무소속 — 엄애란 N전담
            PrecheckNurse(nurse_id="엄애란",   grade=1, team_id=None, allowed_shifts=["N"], join_day=0, leave_day=30),
        ],
        teams=[1, 2, 3],
        roster_config={
            "use_mid": False,
            "daily_shift_requirements": {"D": 1, "E": 1, "N": 1},
            "global_monthly_off_days": 8,
            "standard_personal_off_days": 2,
        },
        team_coverage={
            "1": {"D": 1, "E": 1, "N": 1},
            "2": {"D": 1, "E": 1, "N": 1},
            "3": {"D": 1, "E": 1, "N": 1},
        },
        grade_constraints={
            "minimum_by_shift": {"D": {"1": 1}, "E": {"1": 1}, "N": {"1": 1}},
            "max_by_shift": {},
        },
    )


def test_team_pool_excludes_n_only_from_de():
    snap = build_pool_snapshot(_mk_input())
    pools = {p.pool_id: p for p in snap.pools}

    # Team 2 D 풀: 장세현 (N전담) 빠지고 4명 남아야 함
    t2_d = pools["team_pool:team_2:D"]
    assert "장세현" not in t2_d.allowed_nurse_ids
    assert t2_d.allowed_count == 4
    # Team 2 N 풀: 장세현 포함 5명
    t2_n = pools["team_pool:team_2:N"]
    assert "장세현" in t2_n.allowed_nurse_ids
    assert t2_n.allowed_count == 5


def test_team_3_d_excludes_n_only_iseoyeon():
    snap = build_pool_snapshot(_mk_input())
    pools = {p.pool_id: p for p in snap.pools}
    t3_d = pools["team_pool:team_3:D"]
    assert "이서연" not in t3_d.allowed_nurse_ids
    assert t3_d.allowed_count == 4


def test_grade_1_d_pool_shrunken_by_n_only_g1_nurses():
    snap = build_pool_snapshot(_mk_input())
    pools = {p.pool_id: p for p in snap.pools}
    g1_d = pools["grade_pool:grade_1:D"]
    # 엄애란, 장세현 모두 N전담 → G1 D 가능자 = 박지연, 김한별, 이유림, 김원아 = 4
    assert g1_d.allowed_count == 4
    assert "엄애란" not in g1_d.allowed_nurse_ids
    assert "장세현" not in g1_d.allowed_nurse_ids


def test_common_pool_d_is_empty_when_eomaeran_is_n_only():
    snap = build_pool_snapshot(_mk_input())
    pools = {p.pool_id: p for p in snap.pools}
    cp_d = pools["common_pool:D"]
    # 엄애란이 N전담 → 공통풀 D 가용 = 0
    assert cp_d.allowed_count == 0


def test_reduces_pool_edge_emitted_for_n_only_on_d():
    snap = build_pool_snapshot(_mk_input())
    # 장세현 → team_pool:team_2:D 로 향하는 REDUCES_POOL edge 가 있어야 함
    reduce_edges = [
        e for e in snap.nurse_pool_edges
        if e.get("rel") == "REDUCES_POOL"
        and e.get("src") == "nurse:장세현"
        and e.get("dst") == "team_pool:team_2:D"
    ]
    assert reduce_edges, "장세현 의 team_2 D 풀 감소 edge 가 없음"
    # 이서연 → team_pool:team_3:D 도 마찬가지
    assert any(
        e.get("rel") == "REDUCES_POOL"
        and e.get("src") == "nurse:이서연"
        and e.get("dst") == "team_pool:team_3:D"
        for e in snap.nurse_pool_edges
    )


def test_shortage_emits_policy_layer_core():
    """Shortage 가 1개라도 잡히면 conflict-core shape 으로 변환 가능해야 함."""
    # Team 3 D 정책을 빡세게 (min=5) 하면 인원 4명 미달
    inp = _mk_input()
    inp.team_coverage["3"]["D"] = 5
    snap = build_pool_snapshot(inp)
    assert snap.shortages, "team 3 D min=5 인데 shortage 가 안 잡힘"
    core = snap.shortages[0].to_core()
    assert core["causal_layer"] == "policy"
    assert "pool_shortage" in core["core_id"]
    assert core["source"] == "pool_graph"


def test_snapshot_to_dict_round_trip():
    snap = build_pool_snapshot(_mk_input())
    d = snap.to_dict()
    assert "pools" in d and "shortages" in d and "nurse_pool_edges" in d
    assert isinstance(d["pools"], list)
    assert all("attrs" in p and "pool_type" in p for p in d["pools"])
