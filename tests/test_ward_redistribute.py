"""병동 간 재분배(옵션2) preview — read-only, 풀 클러스터링 + 병동 매핑.

참조: app/services/ward_redistribute_service.py, docs/NURSE_GROUP_CHANGE_MODEL.md (옵션2).
"""

from __future__ import annotations

from datetime import date

import pytest

from db.models import (
    FixedWantedEntry, Group, Nurse, NurseAssignment, Office, Shift, Team,
)
from db.models import NurseAssignment
from services.ward_redistribute_service import (
    WardSetupError,
    apply_ward_redistribution,
    preview_ward_redistribution,
)


def _mk_nurse(db, nid, gid, grade):
    db.add(Nurse(nurse_id=nid, account_id=f"acc_{nid}", group_id=gid, office_id="o1",
                 name=nid, active=1, team_id=1, grade=grade, is_night_nurse=[]))


@pytest.fixture
def pool(db):
    db.add(Office(office_id="o1", office_name="병원"))
    db.add(Group(group_id="A", group_name="A병동", office_id="o1"))
    db.add(Group(group_id="B", group_name="B병동", office_id="o1"))
    db.add(Team(office_id="o1", group_id="A", team_id=1, team_name="1팀"))
    db.add(Team(office_id="o1", group_id="B", team_id=1, team_name="1팀"))
    db.add(Shift(shift_id="V", id=1, office_id="o1", group_id="A", name="연차",
                 color="#fff", type="휴가"))
    # A병동 5명 (G1 1명 + G2 4명), B병동 5명 (G1 1명 + G2 4명)
    _mk_nurse(db, "a_g1", "A", 1)
    for i in range(4):
        _mk_nurse(db, f"a{i}", "A", 2)
    _mk_nurse(db, "b_g1", "B", 1)
    for i in range(4):
        _mk_nurse(db, f"b{i}", "B", 2)
    # N전담 1명 (풀 제외)
    db.add(Nurse(nurse_id="night", account_id="acc_night", group_id="A", office_id="o1",
                 name="야간", active=1, team_id=1, grade=2, is_night_nurse=["N"]))
    # 확정 원티드 OFF 몇 건
    for nid, day in [("a0", 5), ("a1", 5), ("b0", 20), ("b1", 20)]:
        db.add(FixedWantedEntry(group_id="A" if nid.startswith("a") else "B",
                                year=2026, month=7, nurse_id=nid,
                                shift_date=date(2026, 7, day), shift_id="O",
                                source_type="original"))
    db.flush()
    return db


def test_preview_readonly_covers_pool(pool):
    db = pool
    pv = preview_ward_redistribution(db, group_ids=["A", "B"], year=2026, month=7)
    assert pv["target_month"] == "2026-07"
    assert pv["num_wards"] == 2
    assert pv["num_pool"] == 10          # N전담 제외
    assert pv["num_excluded_night"] == 1
    # 풀 전원이 정확히 한 병동에 배정
    assigned = [nid for w in pv["wards"].values() for nid in w["nurse_ids"]]
    assert sorted(assigned) == sorted(
        ["a_g1", "a0", "a1", "a2", "a3", "b_g1", "b0", "b1", "b2", "b3"]
    )
    # read-only
    assert db.query(NurseAssignment).count() == 0


def test_preview_moves_have_direction(pool):
    db = pool
    pv = preview_ward_redistribution(db, group_ids=["A", "B"], year=2026, month=7)
    for m in pv["moves"]:
        assert m["from"] != m["to"]
        assert m["from"] in ("A", "B") and m["to"] in ("A", "B")
    assert pv["num_moved"] == len(pv["moves"])
    # 각 병동에 G1 ≥ 1 (auto_assign hard 제약)
    assert all(w["size"] >= 1 for w in pv["wards"].values())


def test_preview_requires_two_wards(pool):
    with pytest.raises(ValueError):
        preview_ward_redistribution(pool, group_ids=["A"], year=2026, month=7)


def test_ward_without_g1_blocks_with_setup_error(db):
    """G1 미지정 병동이 있으면 차출로 얼버무리지 않고 WardSetupError로 막아야 함."""
    db.add(Office(office_id="o1", office_name="병원"))
    db.add(Group(group_id="A", group_name="A병동", office_id="o1"))
    db.add(Group(group_id="B", group_name="B병동", office_id="o1"))
    # A: G1 있음, B: G1 없음(전원 grade 2)
    _mk_nurse(db, "a_g1", "A", 1)
    for i in range(4):
        _mk_nurse(db, f"a{i}", "A", 2)
    for i in range(5):
        _mk_nurse(db, f"b{i}", "B", 2)
    db.flush()
    with pytest.raises(WardSetupError) as ei:
        preview_ward_redistribution(db, group_ids=["A", "B"], year=2026, month=7)
    assert any(w["group_id"] == "B" for w in ei.value.wards)


def test_explicit_mode_respects_target_bands(pool):
    db = pool
    pv = preview_ward_redistribution(
        db, group_ids=["A", "B"], year=2026, month=7,
        capacity_mode="explicit", target_sizes={"A": 6, "B": 4}, size_tolerance=2,
    )
    assert pv["capacity_mode"] == "explicit"
    sizes = {w: pv["wards"][w]["size"] for w in pv["wards"]}
    assert sum(sizes.values()) == 10
    assert 4 <= sizes["A"] <= 8   # 6 ± 2
    assert 2 <= sizes["B"] <= 6   # 4 ± 2


def test_explicit_requires_target_sizes(pool):
    with pytest.raises(ValueError):
        preview_ward_redistribution(pool, group_ids=["A", "B"], year=2026, month=7,
                                    capacity_mode="explicit")


def test_explicit_band_cannot_absorb_pool(pool):
    # 목표 너무 작고 tolerance 0 → Σmax < 풀 → 에러
    with pytest.raises(ValueError):
        preview_ward_redistribution(
            pool, group_ids=["A", "B"], year=2026, month=7,
            capacity_mode="explicit", target_sizes={"A": 1, "B": 1}, size_tolerance=0,
        )


def test_preview_includes_nested_team_breakdown(pool):
    db = pool
    pv = preview_ward_redistribution(db, group_ids=["A", "B"], year=2026, month=7)
    for wid, w in pv["wards"].items():
        assert "teams" in w
        # 팀 분해 안의 전체 인원 = 병동 인원
        flat = [m["nurse_id"] for ms in w["teams"].values() for m in ms]
        assert sorted(flat) == sorted(w["nurse_ids"])


def test_churn_weight_reduces_moves(db):
    db.add(Office(office_id="o1", office_name="병원"))
    db.add(Group(group_id="A", group_name="A", office_id="o1"))
    db.add(Group(group_id="B", group_name="B", office_id="o1"))
    db.add(Team(office_id="o1", group_id="A", team_id=1, team_name="1팀"))
    db.add(Team(office_id="o1", group_id="B", team_id=1, team_name="1팀"))
    for i in range(6):
        _mk_nurse(db, f"a{i}", "A", 1 if i == 0 else 2)
    for i in range(6):
        _mk_nurse(db, f"b{i}", "B", 1 if i == 0 else 2)
    db.flush()
    tgt = {"A": 6, "B": 6}
    hi = preview_ward_redistribution(db, group_ids=["A", "B"], year=2026, month=7,
                                     capacity_mode="explicit", target_sizes=tgt,
                                     churn_weight=1000.0)
    lo = preview_ward_redistribution(db, group_ids=["A", "B"], year=2026, month=7,
                                     capacity_mode="explicit", target_sizes=tgt,
                                     churn_weight=0.0)
    # 현재 정원과 동일 목표 → churn 높으면 이동 0에 수렴, 낮으면 더 많음
    assert hi["num_moved"] <= lo["num_moved"]


def test_apply_creates_transfer_and_team_events(pool):
    db = pool
    assignments = [
        {"nurse_id": "a0", "to_group_id": "B", "team_id": 1},   # 이동 → transfer
        {"nurse_id": "a1", "to_group_id": "A", "team_id": 2},   # 잔류+팀변경 → permanent_change
        {"nurse_id": "a2", "to_group_id": "A", "team_id": 1},   # 동일 → skip
    ]
    res = apply_ward_redistribution(
        db, group_ids=["A", "B"], year=2026, month=7, assignments=assignments,
    )
    assert res["transfers"] == 1
    assert res["team_changes"] == 1
    assert res["skipped"] == 1
    assert res["effective_date"] == "2026-07-01"

    rows = {(r.nurse_id, r.kind): r for r in db.query(NurseAssignment).all()}
    # 이동 = 병동이동(transfer), target=B, target_team_id=1
    mv = rows[("a0", "transfer")]
    assert mv.reason == "병동이동" and mv.target_group_id == "B" and mv.target_team_id == 1
    # 팀변경 = permanent_change, target_team_id=2, source==target==A
    tc = rows[("a1", "permanent_change")]
    assert tc.target_team_id == 2 and tc.source_group_id == tc.target_group_id == "A"


def test_apply_skips_unknown_or_no_target(pool):
    db = pool
    res = apply_ward_redistribution(
        db, group_ids=["A", "B"], year=2026, month=7,
        assignments=[{"nurse_id": "ghost", "to_group_id": "B"},
                     {"nurse_id": "a0", "to_group_id": None}],
    )
    assert res["transfers"] == 0 and res["skipped"] == 2


def test_role_mix_warning(db):
    db.add(Office(office_id="o1", office_name="병원"))
    db.add(Group(group_id="A", group_name="A-RN", office_id="o1"))
    db.add(Group(group_id="B", group_name="B-AN", office_id="o1"))
    # A=RN 5명(G1 1), B=AN 5명(G1 1)
    for i in range(5):
        db.add(Nurse(nurse_id=f"a{i}", account_id=f"acc_a{i}", group_id="A", office_id="o1",
                     name=f"a{i}", active=1, team_id=1, grade=1 if i == 0 else 2,
                     role="RN", is_night_nurse=[]))
    for i in range(5):
        db.add(Nurse(nurse_id=f"b{i}", account_id=f"acc_b{i}", group_id="B", office_id="o1",
                     name=f"b{i}", active=1, team_id=1, grade=1 if i == 0 else 2,
                     role="AN", is_night_nurse=[]))
    db.flush()
    pv = preview_ward_redistribution(db, group_ids=["A", "B"], year=2026, month=7)
    assert any("역할" in w for w in pv["warnings"]), pv["warnings"]


def test_period_overlap_excludes_and_past_included(db):
    from datetime import date as _d
    db.add(Office(office_id="o1", office_name="병원"))
    db.add(Group(group_id="A", group_name="A병동", office_id="o1"))
    db.add(Group(group_id="B", group_name="B병동", office_id="o1"))
    db.add(Team(office_id="o1", group_id="A", team_id=1, team_name="1팀"))
    db.add(Team(office_id="o1", group_id="B", team_id=1, team_name="1팀"))
    _mk_nurse(db, "a_g1", "A", 1)
    _mk_nurse(db, "a0", "A", 2)
    _mk_nurse(db, "b_g1", "B", 1)
    _mk_nurse(db, "b0", "B", 2)
    _mk_nurse(db, "disp", "A", 2)   # 진행중 파견(겹침) → 제외
    _mk_nurse(db, "past", "A", 2)   # 과거 파견(안겹침) → 포함
    db.add(NurseAssignment(nurse_id="disp", source_group_id="A", target_group_id="B",
                           office_id="o1", start_date=_d(2026, 7, 1), reason="파견",
                           status="active"))  # open-ended → 8월과 겹침
    db.add(NurseAssignment(nurse_id="past", source_group_id="A", target_group_id="B",
                           office_id="o1", start_date=_d(2026, 5, 1),
                           expected_end_date=_d(2026, 6, 30), reason="파견",
                           status="active"))  # 6/30 종료 → 8월 안겹침
    db.flush()
    pv = preview_ward_redistribution(db, group_ids=["A", "B"], year=2026, month=8)
    assert {e["nurse_id"] for e in pv["excluded_overlap"]} == {"disp"}
    assigned = [nid for w in pv["wards"].values() for nid in w["nurse_ids"]]
    assert "disp" not in assigned
    assert "past" in assigned  # 과거 파견은 재분배 대상


def test_participant_subset_fixes_non_participants(pool):
    """participant_ids 지정 시: 그 집합만 이동 자유, 미참여자는 현재 병동에 고정."""
    db = pool
    pv = preview_ward_redistribution(
        db, group_ids=["A", "B"], year=2026, month=7,
        participant_ids=["a0", "a1"],
    )
    assert pv["num_participants"] == 2
    # 미참여 = 풀(10) - 참여(2) = 8, 모두 fixed_stay 로 노출
    assert pv["num_fixed_stay"] == 8
    fixed_ids = {f["nurse_id"] for f in pv["fixed_stay"]}
    assert fixed_ids == {"a_g1", "a2", "a3", "b_g1", "b0", "b1", "b2", "b3"}
    # 미참여자는 전부 현재 병동에 그대로 남아야 함
    where = {nid: wid for wid, w in pv["wards"].items() for nid in w["nurse_ids"]}
    cur = {n.nurse_id: n.group_id for n in db.query(Nurse).all()}
    for nid in fixed_ids:
        assert where[nid] == cur[nid], f"{nid} 고정 위반: {where[nid]} != {cur[nid]}"


def test_no_participants_raises(pool):
    """참여 인원 0명(빈 선택) → 이동 대상이 없으므로 막아야 함."""
    with pytest.raises(ValueError):
        preview_ward_redistribution(
            pool, group_ids=["A", "B"], year=2026, month=7, participant_ids=[],
        )


def test_night_override_participates_when_selected(pool):
    """N전담도 participant_ids 에 넣으면 풀에 포함(override) — 제외 명단에서 빠짐."""
    db = pool
    pv = preview_ward_redistribution(
        db, group_ids=["A", "B"], year=2026, month=7,
        participant_ids=["a0", "night"],
    )
    assert pv["num_excluded_night"] == 0
    assigned = [nid for w in pv["wards"].values() for nid in w["nurse_ids"]]
    assert "night" in assigned


def test_apply_graceful_skips_conflict(db):
    from datetime import date as _d
    db.add(Office(office_id="o1", office_name="병원"))
    db.add(Group(group_id="A", group_name="A병동", office_id="o1"))
    db.add(Group(group_id="B", group_name="B병동", office_id="o1"))
    _mk_nurse(db, "x", "A", 2)   # 진행중 파견 → transfer 생성 시 409
    _mk_nurse(db, "ok", "A", 2)
    db.add(NurseAssignment(nurse_id="x", source_group_id="A", target_group_id="B",
                           office_id="o1", start_date=_d(2026, 7, 1), reason="파견",
                           status="active"))
    db.flush()
    res = apply_ward_redistribution(
        db, group_ids=["A", "B"], year=2026, month=8,
        assignments=[{"nurse_id": "x", "to_group_id": "B"},
                     {"nurse_id": "ok", "to_group_id": "B"}],
    )
    assert res["transfers"] == 1               # ok 성공
    assert any(f["nurse_id"] == "x" for f in res["failed"])  # x 만 실패


def test_fixed_preceptee_not_dragged_by_participant_preceptor(pool):
    """미참여(fixed) 프리셉티는 참여 프리셉터를 따라가지 않고 현재 병동에 남는다."""
    db = pool
    db.query(Nurse).filter(Nurse.nurse_id == "a1").update(
        {"preceptor_id": "a0"}, synchronize_session=False)
    db.flush()
    # a0 만 참여(이동 자유), a1(프리셉티) 포함 나머지는 미참여=현재 병동 고정
    pv = preview_ward_redistribution(
        db, group_ids=["A", "B"], year=2026, month=7, participant_ids=["a0"],
    )
    where = {nid: wid for wid, w in pv["wards"].items() for nid in w["nurse_ids"]}
    assert where["a1"] == "A"  # fixed → follow 에 끌려가지 않음


def test_ward_pair_split_warns_when_preceptor_excluded(db):
    """프리셉터가 풀에서 빠지면(파견 겹침) 프리셉티와 갈라짐 → 경고."""
    from datetime import date as _d
    db.add(Office(office_id="o1", office_name="병원"))
    db.add(Group(group_id="A", group_name="A병동", office_id="o1"))
    db.add(Group(group_id="B", group_name="B병동", office_id="o1"))
    db.add(Team(office_id="o1", group_id="A", team_id=1, team_name="1팀"))
    db.add(Team(office_id="o1", group_id="B", team_id=1, team_name="1팀"))
    _mk_nurse(db, "a_g1", "A", 1)
    _mk_nurse(db, "a0", "A", 2)      # 프리셉터 — 파견 겹침으로 제외 예정
    db.add(Nurse(nurse_id="a_pe", account_id="acc_a_pe", group_id="A",
                 office_id="o1", name="a_pe", active=1, team_id=1, grade=3,
                 is_night_nurse=[], preceptor_id="a0"))  # 프리셉티
    _mk_nurse(db, "b_g1", "B", 1)
    _mk_nurse(db, "b0", "B", 2)
    db.add(NurseAssignment(nurse_id="a0", source_group_id="A", target_group_id="B",
                           office_id="o1", start_date=_d(2026, 8, 1), reason="파견",
                           status="active"))  # 8월 겹침 → a0 제외
    db.flush()
    pv = preview_ward_redistribution(db, group_ids=["A", "B"], year=2026, month=8)
    pair = next(p for p in pv["pairs"] if p["preceptee_id"] == "a_pe")
    assert pair["status"] == "split"
    assert any("갈라" in w for w in pv["warnings"])
