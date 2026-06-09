"""선택 기반 팀분류 — 경우의수 매트릭스 (40 케이스).

계열:
  A. 알고리즘(auto_assign_teams, pure)      — 18
  B. 옵션1 선택 서비스(DB)                  — 14
  C. 속성변경/flush + kind 매핑(DB+pure)    — 8
"""

from __future__ import annotations

from datetime import date

import pytest

from db.models import (
    FixedWantedEntry, Group, Nurse, NurseAssignment, Office, Shift, Team,
)
from services.assignment_service import (
    ATTRIBUTE_CHANGE_KINDS,
    cancel_assignment,
    create_permanent_change,
    flush_pending_permanent_changes,
    kind_for_reason,
    _raise_if_overlap,
)
from services.team_auto_assign import NurseInput, auto_assign_teams
from services.team_classify_service import (
    apply_team_classification,
    preview_team_classification,
)


def _n(nid, grade=2, preceptor_id=None, off=(), fb=()):
    return NurseInput(nurse_id=nid, grade=grade, preceptor_id=preceptor_id,
                      off_days=frozenset(off), fb_days=frozenset(fb))


def _ids(res):
    return sorted(x for m in res.teams.values() for x in m)


# ──────────────────────────────────────────────────────────────────────────
# A. 알고리즘 (pure)
# ──────────────────────────────────────────────────────────────────────────

def test_A01_g1_per_team():
    ns = [_n("s0", 1), _n("s1", 1)] + [_n(f"p{i}") for i in range(6)]
    res = auto_assign_teams(ns, num_teams=2, min_size=2, max_size=6)
    for m in res.teams.values():
        assert any(x in ("s0", "s1") for x in m)


def test_A02_size_within_bounds():
    ns = [_n("s0", 1), _n("s1", 1)] + [_n(f"p{i}") for i in range(6)]
    res = auto_assign_teams(ns, num_teams=2, min_size=3, max_size=5)
    for m in res.teams.values():
        assert 3 <= len(m) <= 5


def test_A03_all_assigned_once():
    ns = [_n("s0", 1), _n("s1", 1)] + [_n(f"p{i}") for i in range(6)]
    res = auto_assign_teams(ns, num_teams=2, min_size=2, max_size=6)
    assert _ids(res) == sorted(n.nurse_id for n in ns)


def test_A04_preceptee_follows_seed_preceptor():
    ns = [_n("s0", 1), _n("s1", 1), _n("pe", 0, preceptor_id="s0"),
          _n("p1"), _n("p2"), _n("p3")]
    res = auto_assign_teams(ns, num_teams=2, min_size=2, max_size=6)
    t_of = {x: t for t, m in res.teams.items() for x in m}
    assert t_of["pe"] == t_of["s0"]


def test_A05_preceptee_follows_nonseed_preceptor():
    ns = [_n("s0", 1), _n("s1", 1), _n("pr"), _n("pe", 0, preceptor_id="pr"),
          _n("p1"), _n("p2")]
    res = auto_assign_teams(ns, num_teams=2, min_size=2, max_size=6)
    t_of = {x: t for t, m in res.teams.items() for x in m}
    assert t_of["pe"] == t_of["pr"]


def test_A06_seed_not_g1_raises():
    ns = [_n("s0", 2), _n("s1", 1), _n("p1"), _n("p2")]
    with pytest.raises(ValueError):
        auto_assign_teams(ns, seed_ids=["s0", "s1"], min_size=1, max_size=4)


def test_A07_g1_lt_numteams_raises():
    ns = [_n("s0", 1)] + [_n(f"p{i}") for i in range(5)]
    with pytest.raises(ValueError):
        auto_assign_teams(ns, num_teams=3, min_size=1, max_size=4)


def test_A08_fixed_pinned():
    ns = [_n("s0", 1), _n("s1", 1), _n("f0"), _n("f1"),
          _n("p1"), _n("p2"), _n("p3"), _n("p4")]
    res = auto_assign_teams(ns, seed_ids=["s0", "s1"], fixed={"f0": 0, "f1": 1},
                            min_size=2, max_size=6)
    assert "f0" in res.teams[0] and "f1" in res.teams[1]


def test_A09_fixed_not_moved_under_overlap():
    ns = [_n("s0", 1, off=(1, 2, 3)), _n("s1", 1), _n("f0", off=(1, 2, 3)),
          _n("p1"), _n("p2")]
    res = auto_assign_teams(ns, seed_ids=["s0", "s1"], fixed={"f0": 0},
                            min_size=2, max_size=6)
    assert "f0" in res.teams[0]


def test_A10_no_fixed_backward_compat():
    ns = [_n("s0", 1), _n("s1", 1)] + [_n(f"p{i}") for i in range(4)]
    res = auto_assign_teams(ns, seed_ids=["s0", "s1"], min_size=2, max_size=6)
    assert _ids(res) == sorted(n.nurse_id for n in ns)


def test_A11_churn_reduces_moves():
    ns = [_n("s0", 1), _n("s1", 1)] + [_n(f"p{i}") for i in range(6)]
    home = {n.nurse_id: (0 if n.nurse_id in ("s0", "p0", "p1", "p2") else 1)
            for n in ns}
    hi = auto_assign_teams(ns, seed_ids=["s0", "s1"], min_size=2, max_size=6,
                           home_cluster=home, w_churn=1000)
    lo = auto_assign_teams(ns, seed_ids=["s0", "s1"], min_size=2, max_size=6,
                           home_cluster=home, w_churn=0)

    def moved(res):
        return sum(1 for t, m in res.teams.items() for x in m
                   if home.get(x) is not None and home[x] != t)
    assert moved(hi) <= moved(lo)


def test_A12_per_cluster_max_sizes():
    ns = [_n("s0", 1), _n("s1", 1)] + [_n(f"p{i}") for i in range(6)]
    res = auto_assign_teams(ns, seed_ids=["s0", "s1"], min_size=1,
                            max_sizes=[6, 3])
    assert len(res.teams[1]) <= 3


def test_A13_per_cluster_min_sizes_minfill():
    ns = [_n("s0", 1), _n("s1", 1)] + [_n(f"p{i}") for i in range(6)]
    res = auto_assign_teams(ns, seed_ids=["s0", "s1"], min_size=1, max_size=8,
                            min_sizes=[3, 3], max_sizes=[6, 6])
    assert len(res.teams[0]) >= 3 and len(res.teams[1]) >= 3


def test_A14_large_input_no_crash():
    ns = [_n("s0", 1), _n("s1", 1), _n("s2", 1)]
    ns += [_n(f"p{i}", off=(i % 5,)) for i in range(21)]
    res = auto_assign_teams(ns, num_teams=3, min_size=4, max_size=10)
    assert len(_ids(res)) == 24


def test_A15_empty_wanted_partitions():
    ns = [_n("s0", 1), _n("s1", 1)] + [_n(f"p{i}") for i in range(4)]
    res = auto_assign_teams(ns, num_teams=2, min_size=2, max_size=4)
    assert len(_ids(res)) == 6


def test_A16_single_team():
    ns = [_n("s0", 1)] + [_n(f"p{i}") for i in range(3)]
    res = auto_assign_teams(ns, num_teams=1, min_size=1, max_size=10)
    assert len(res.teams) == 1 and len(res.teams[0]) == 4


def test_A17_two_preceptees_same_preceptor():
    ns = [_n("s0", 1), _n("s1", 1), _n("pe1", 0, preceptor_id="s0"),
          _n("pe2", 0, preceptor_id="s0"), _n("p1"), _n("p2")]
    res = auto_assign_teams(ns, num_teams=2, min_size=2, max_size=6)
    t_of = {x: t for t, m in res.teams.items() for x in m}
    assert t_of["pe1"] == t_of["s0"] and t_of["pe2"] == t_of["s0"]


def test_A18_overlap_pushed_apart():
    # 같은 OFF 두 명은 가능하면 다른 팀
    ns = [_n("s0", 1), _n("s1", 1), _n("a", off=(1, 2, 3, 4, 5)),
          _n("b", off=(1, 2, 3, 4, 5)), _n("c"), _n("d")]
    res = auto_assign_teams(ns, num_teams=2, min_size=3, max_size=3, w_overlap=1000)
    t_of = {x: t for t, m in res.teams.items() for x in m}
    assert t_of["a"] != t_of["b"]


# ──────────────────────────────────────────────────────────────────────────
# B. 옵션1 선택 서비스 (DB)
# ──────────────────────────────────────────────────────────────────────────

def _mk(db, nid, team_id, grade, night=False, gid="A"):
    db.add(Nurse(nurse_id=nid, account_id=f"acc_{nid}", group_id=gid,
                 office_id="o1", name=nid, active=1, team_id=team_id,
                 grade=grade, is_night_nurse=["N"] if night else []))


@pytest.fixture
def ward(db):
    db.add(Office(office_id="o1", office_name="병원"))
    db.add(Group(group_id="A", group_name="A병동", office_id="o1"))
    db.add(Team(office_id="o1", group_id="A", team_id=1, team_name="1팀"))
    db.add(Team(office_id="o1", group_id="A", team_id=2, team_name="2팀"))
    _mk(db, "g1a", 1, 1)
    _mk(db, "g1b", 2, 1)
    for nid, tid, g in [("n1", 1, 2), ("n2", 1, 2), ("n3", 1, 3),
                        ("n4", 2, 2), ("n5", 2, 2), ("n6", 2, 3)]:
        _mk(db, nid, tid, g)
    _mk(db, "night", 1, 2, night=True)
    db.flush()
    return db


def test_B01_full_recluster_all_non_night(ward):
    pv = preview_team_classification(ward, group_id="A", year=2026, month=8)
    assigned = [x for m in pv["teams"].values() for x in m]
    assert "night" not in assigned and len(assigned) == 8


def test_B02_subset_others_unassigned(ward):
    pv = preview_team_classification(
        ward, group_id="A", year=2026, month=8,
        participant_ids=["g1a", "g1b", "n1", "n4"])
    assigned = sorted(x for m in pv["teams"].values() for x in m)
    assert assigned == ["g1a", "g1b", "n1", "n4"]
    assert {u["nurse_id"] for u in pv["unassigned"]} == {"n2", "n3", "n5", "n6"}


def test_B03_night_default_excluded(ward):
    pv = preview_team_classification(ward, group_id="A", year=2026, month=8)
    assert {e["nurse_id"] for e in pv["excluded_night"]} == {"night"}


def test_B04_night_override_included(ward):
    pv = preview_team_classification(
        ward, group_id="A", year=2026, month=8,
        participant_ids=["g1a", "g1b", "n1", "n2", "n3", "n4", "n5", "n6", "night"])
    assigned = [x for m in pv["teams"].values() for x in m]
    assert "night" in assigned and pv["num_excluded_night"] == 0


def test_B05_night_override_released_on_flush(ward):
    parts = ["g1a", "g1b", "n1", "n2", "n3", "n4", "n5", "n6", "night"]
    pv = preview_team_classification(ward, group_id="A", year=2026, month=8,
                                     participant_ids=parts)
    flat = [{"nurse_id": nid, "team_id": int(t)}
            for t, m in pv["teams"].items() for nid in m]
    apply_team_classification(ward, group_id="A", office_id="o1", year=2026,
                              month=8, assignments=flat)
    flush_pending_permanent_changes(ward, as_of=date(2026, 8, 1))
    assert (ward.query(Nurse).filter(Nurse.nurse_id == "night").first()
            .is_night_nurse or []) == []


def test_B06_participant_g1_lt_teams_raises(ward):
    with pytest.raises(ValueError):
        preview_team_classification(ward, group_id="A", year=2026, month=8,
                                    participant_ids=["g1a", "n1", "n2"])  # G1 1명 < 2팀


def test_B07_no_teams_raises(db):
    db.add(Office(office_id="o1", office_name="병원"))
    db.add(Group(group_id="A", group_name="A", office_id="o1"))
    _mk(db, "x", None, 1)
    db.flush()
    with pytest.raises(ValueError):
        preview_team_classification(db, group_id="A", year=2026, month=8)


def test_B08_empty_participant_raises(ward):
    with pytest.raises(ValueError):
        preview_team_classification(ward, group_id="A", year=2026, month=8,
                                    participant_ids=[])


def test_B09_apply_skips_unchanged(ward):
    # 전원 현재 팀 그대로 주면 변경 0
    cur = {n.nurse_id: n.team_id for n in ward.query(Nurse).filter(
        Nurse.group_id == "A", Nurse.is_night_nurse == [])}
    flat = [{"nurse_id": nid, "team_id": int(t)} for nid, t in cur.items()
            if t is not None]
    res = apply_team_classification(ward, group_id="A", office_id="o1",
                                    year=2026, month=8, assignments=flat)
    assert res["created"] == 0


def test_B10_apply_effective_month1(ward):
    res = apply_team_classification(ward, group_id="A", office_id="o1",
                                    year=2026, month=8,
                                    assignments=[{"nurse_id": "n1", "team_id": 2}])
    assert res["effective_date"] == "2026-08-01" and res["created"] == 1


def test_B11_flush_before_noop(ward):
    apply_team_classification(ward, group_id="A", office_id="o1", year=2026,
                              month=8, assignments=[{"nurse_id": "n1", "team_id": 2}])
    assert flush_pending_permanent_changes(ward, as_of=date(2026, 7, 31)) == 0
    assert str(ward.query(Nurse).filter(Nurse.nurse_id == "n1").first().team_id) == "1"


def test_B12_flush_on_applies(ward):
    apply_team_classification(ward, group_id="A", office_id="o1", year=2026,
                              month=8, assignments=[{"nurse_id": "n1", "team_id": 2}])
    flush_pending_permanent_changes(ward, as_of=date(2026, 8, 1))
    assert str(ward.query(Nurse).filter(Nurse.nurse_id == "n1").first().team_id) == "2"


def test_B13_preview_readonly(ward):
    preview_team_classification(ward, group_id="A", year=2026, month=8)
    assert ward.query(NurseAssignment).count() == 0


def test_B14_changes_diff_present(ward):
    pv = preview_team_classification(ward, group_id="A", year=2026, month=8)
    for c in pv["changes"]:
        assert c["from"] != c["to"] and "name" in c


# ──────────────────────────────────────────────────────────────────────────
# C. 속성변경 / flush / kind 매핑
# ──────────────────────────────────────────────────────────────────────────

@pytest.fixture
def solo(db):
    db.add(Office(office_id="o1", office_name="병원"))
    db.add(Group(group_id="A", group_name="A", office_id="o1"))
    db.add(Nurse(nurse_id="n", account_id="acc_n", group_id="A", office_id="o1",
                 name="n", active=1, team_id=1, grade=2, is_night_nurse=["N"]))
    db.flush()
    return db


def test_C01_pc_team_only(solo):
    r = create_permanent_change(solo, nurse_id="n", group_id="A", office_id="o1",
                                start_date=date(2026, 8, 1), new_team_id=3)
    assert r.target_team_id == 3 and r.target_grade is None


def test_C02_pc_grade_only(solo):
    r = create_permanent_change(solo, nurse_id="n", group_id="A", office_id="o1",
                                start_date=date(2026, 8, 1), new_grade=1)
    assert r.target_grade == 1 and r.target_team_id is None


def test_C03_pc_shift_only_release(solo):
    r = create_permanent_change(solo, nurse_id="n", group_id="A", office_id="o1",
                                start_date=date(2026, 8, 1), new_shift_types=[])
    assert r.target_shift_types == []


def test_C04_pc_combined(solo):
    r = create_permanent_change(solo, nurse_id="n", group_id="A", office_id="o1",
                                start_date=date(2026, 8, 1), new_team_id=2,
                                new_grade=1, new_shift_types=[])
    assert r.target_team_id == 2 and r.target_grade == 1 and r.target_shift_types == []


def test_C05_pc_requires_some_attr(solo):
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        create_permanent_change(solo, nurse_id="n", group_id="A", office_id="o1",
                                start_date=date(2026, 8, 1))


def test_C06_flush_applies_shift_release(solo):
    create_permanent_change(solo, nurse_id="n", group_id="A", office_id="o1",
                            start_date=date(2026, 8, 1), new_shift_types=[])
    flush_pending_permanent_changes(solo, as_of=date(2026, 8, 1))
    assert (solo.query(Nurse).filter(Nurse.nurse_id == "n").first()
            .is_night_nurse or []) == []


def test_C07_cancel_pending_not_flushed(solo):
    r = create_permanent_change(solo, nurse_id="n", group_id="A", office_id="o1",
                                start_date=date(2026, 8, 1), new_team_id=9)
    cancel_assignment(r.id, solo, current_user=None)
    n_flushed = flush_pending_permanent_changes(solo, as_of=date(2026, 8, 1))
    assert n_flushed == 0
    assert str(solo.query(Nurse).filter(Nurse.nurse_id == "n").first().team_id) == "1"


def test_C08_payload_prev_captured(solo):
    r = create_permanent_change(solo, nurse_id="n", group_id="A", office_id="o1",
                                start_date=date(2026, 8, 1), new_team_id=3)
    assert r.payload["prev_team_id"] == 1 and r.payload["prev_shift_types"] == ["N"]


@pytest.mark.parametrize("reason,kind", [
    ("병동이동", "transfer"), ("파견", "dispatch"), ("프리셉티", "preceptee"),
    ("휴직", "leave"), ("복직", "return"), ("퇴사", "resign"),
    ("속성변경", "permanent_change"), ("무관한사유", "transfer"),
])
def test_C09_kind_for_reason(reason, kind):
    assert kind_for_reason(reason) == kind


def test_C10_overlap_excludes_attribute_kinds(solo):
    # permanent_change(active)가 있어도 존재 이벤트 생성 막지 않음
    create_permanent_change(solo, nurse_id="n", group_id="A", office_id="o1",
                            start_date=date(2026, 8, 1), new_team_id=3)
    assert "permanent_change" in ATTRIBUTE_CHANGE_KINDS
    _raise_if_overlap(solo, "n", date(2026, 8, 5), date(2026, 8, 20))  # no raise
