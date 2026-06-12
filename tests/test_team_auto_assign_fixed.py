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


def test_sizes_are_balanced_max_minus_min_le_1():
    """인원 균등화: 14명 3팀 → max−min ≤ 1 (예: 5/5/4). 시드 G1 3명 + G2 11명."""
    nurses = [_n(f"g1_{i}", grade=1) for i in range(3)] + [
        _n(f"n{i}", grade=2) for i in range(11)
    ]
    res = auto_assign_teams(nurses, num_teams=3)
    sizes = sorted(len(v) for v in res.teams.values())
    assert sizes[-1] - sizes[0] <= 1, sizes
    assert sum(sizes) == 14
    # 각 팀 G1 ≥ 1 (시드 유지)
    g1 = {n.nurse_id for n in nurses if n.grade == 1}
    for members in res.teams.values():
        assert any(m in g1 for m in members)


def test_g1_shortage_degrades_gracefully_not_raises():
    """G1 < num_teams 면 막지 말고(과거 ValueError) 비-G1 앵커로 시드 보충 →
    전원 팀 배정. '팀마다 G1' 은 가능한 만큼만 충족."""
    # 팀 3개 요청인데 G1 1명뿐 + 정규(2) 3 + 신규(0) 2
    nurses = [
        _n("g1", grade=1),
        _n("r0", grade=2), _n("r1", grade=2), _n("r2", grade=2),
        _n("x0", grade=0), _n("x1", grade=0),
    ]
    res = auto_assign_teams(nurses, num_teams=3, min_size=1, max_size=4)
    # 3팀 모두 생성 + 전원 정확히 1회 배치(무팀 NULL 없음)
    assert len(res.teams) == 3
    allids = sorted(x for m in res.teams.values() for x in m)
    assert allids == ["g1", "r0", "r1", "r2", "x0", "x1"]
    # 비-G1 앵커는 정규(grade-2) 우선 — 신규(0/None)는 앵커로 안 뽑힘
    seeds_present = {m[0] if m else None for m in res.teams.values()}  # 참고용(순서 무관)
    # G1 1명은 반드시 어떤 팀의 멤버
    assert any("g1" in m for m in res.teams.values())
    # 신규 2명은 시드가 아니어야(정규가 앵커 우선) — 최소한 둘 다 시드가 되진 않음
    assert not ({"x0", "x1"} <= seeds_present)


def test_g1_shortage_with_preceptee_not_seeded():
    """프리셉티는 비-G1 앵커로 선발되지 않고 preceptor 팀을 따라간다."""
    nurses = [
        _n("g1", grade=1),
        _n("r0", grade=2), _n("r1", grade=2),
        NurseInput(nurse_id="pe", grade=0, preceptor_id="r0"),
    ]
    res = auto_assign_teams(nurses, num_teams=3, min_size=1, max_size=4)
    # pe 는 r0 와 같은 팀
    for members in res.teams.values():
        if "r0" in members:
            assert "pe" in members
    allids = sorted(x for m in res.teams.values() for x in m)
    assert allids == ["g1", "pe", "r0", "r1"]


def test_none_grade_nurses_are_balanced():
    """grade=None(미입력) 간호사도 균등 분포 → grade_dev 작게 (오판 0 카운트 회귀 방지)."""
    nurses = [_n(f"g1_{i}", grade=1) for i in range(3)] + [
        _n(f"x{i}", grade=None) for i in range(9)
    ]
    res = auto_assign_teams(nurses, num_teams=3)
    # None 9명이 3팀에 3/3/3 → grade_dev 는 0 에 가까움(예전엔 9로 부풀었음)
    assert res.grade_dev_total <= 2.0, res.grade_dev_total
    sizes = sorted(len(v) for v in res.teams.values())
    assert sizes[-1] - sizes[0] <= 1, sizes


def test_balanced_even_with_overlap_pressure():
    """겹침 압력이 있어도 인원 균등 유지(크기 하드 균형 우선)."""
    # 절반은 5일 OFF 동일(겹침 유도), 나머지 분산
    a = [_n(f"a{i}", grade=2, off=(5,)) for i in range(6)]
    b = [_n(f"b{i}", grade=2, off=(i + 10,)) for i in range(5)]
    seeds = [_n(f"g1_{i}", grade=1) for i in range(3)]
    res = auto_assign_teams(seeds + a + b, num_teams=3)
    sizes = sorted(len(v) for v in res.teams.values())
    assert sizes[-1] - sizes[0] <= 1, sizes
