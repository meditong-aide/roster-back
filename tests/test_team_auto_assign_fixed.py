"""auto_assign_teams 의 fixed(미참여=현재 클러스터 고정) 지원 검증.

참조: app/services/team_auto_assign.py (선택 기반 분류 — 참여만 재배치, 미참여 고정).
"""

from __future__ import annotations

from services.team_auto_assign import NurseInput, auto_assign_teams


def _n(nid, grade=2, off=()):
    return NurseInput(nurse_id=nid, grade=grade, off_days=frozenset(off))


def test_fixed_members_pinned_to_their_cluster():
    nurses = [
        _n("s0", 1), _n("s1", 1),       # 시드(G1) → cluster 0,1
        _n("f0"), _n("f1"),             # 고정(미참여)
        _n("p1"), _n("p2"), _n("p3"), _n("p4"),  # 참여(분배)
    ]
    res = auto_assign_teams(
        nurses, seed_ids=["s0", "s1"], fixed={"f0": 0, "f1": 1},
        min_size=2, max_size=6,
    )
    assert "f0" in res.teams[0]
    assert "f1" in res.teams[1]
    # 전원 정확히 한 번 배치
    allids = sorted(x for m in res.teams.values() for x in m)
    assert allids == ["f0", "f1", "p1", "p2", "p3", "p4", "s0", "s1"]


def test_fixed_not_moved_even_with_overlap_pressure():
    # f0(고정,0번) 와 s0(0번)이 OFF 완전 겹침 → 알고리즘은 분리하고 싶지만 고정이라 못 옮김
    nurses = [
        _n("s0", 1, off=(1, 2, 3)),
        _n("s1", 1),
        _n("f0", off=(1, 2, 3)),   # s0와 겹침, 0번 고정
        _n("p1"), _n("p2"), _n("p3"),
    ]
    res = auto_assign_teams(
        nurses, seed_ids=["s0", "s1"], fixed={"f0": 0},
        min_size=2, max_size=6,
    )
    assert "f0" in res.teams[0]  # 겹침 페널티에도 고정 유지


def test_no_fixed_is_backward_compatible():
    # fixed 미지정 시 기존 동작 (전원 분배)
    nurses = [_n("s0", 1), _n("s1", 1), _n("p1"), _n("p2"), _n("p3"), _n("p4")]
    res = auto_assign_teams(nurses, seed_ids=["s0", "s1"], min_size=2, max_size=6)
    allids = sorted(x for m in res.teams.values() for x in m)
    assert allids == ["p1", "p2", "p3", "p4", "s0", "s1"]
