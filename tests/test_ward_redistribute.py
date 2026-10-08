"""병동 간 재분배(옵션2) preview — read-only, 풀 클러스터링 + 병동 매핑.

참조: app/services/ward_redistribute_service.py, docs/NURSE_GROUP_CHANGE_MODEL.md (옵션2).
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

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


# 재분배 발행은 로그인 사용자를 요구한다(None=미인증 → 401). 권한 판정이 아니라 동작을 보는
# 테스트라 마스터 관리자로 통과시킨다. 관리자도 자기 병원 병동만 다루므로(10-06) 픽스처 병원을 준다.
_ADMIN = SimpleNamespace(is_master_admin=True, office_id="o1")


def _mk_nurse(db, nid, gid, grade):
    db.add(Nurse(nurse_id=nid, account_id=f"acc_{nid}", group_id=gid, office_id="o1",
                 name=nid, active=1, team_id=1, grade=grade, allowed_shifts=[]))


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
                 name="야간", active=1, team_id=1, grade=2, allowed_shifts=["N"]))
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


def test_ward_without_g1_proceeds_when_allowed(db):
    """allow_missing_g1=True → G1 없는 병동도 막지 않고 경고만 + 정상 미리보기."""
    db.add(Office(office_id="o1", office_name="병원"))
    db.add(Group(group_id="A", group_name="A병동", office_id="o1"))
    db.add(Group(group_id="B", group_name="B병동", office_id="o1"))
    _mk_nurse(db, "a_g1", "A", 1)
    for i in range(4):
        _mk_nurse(db, f"a{i}", "A", 2)
    for i in range(5):
        _mk_nurse(db, f"b{i}", "B", 2)  # B: G1 없음
    db.flush()
    pv = preview_ward_redistribution(
        db, group_ids=["A", "B"], year=2026, month=7, allow_missing_g1=True
    )
    # 차단 안 됨: 풀 전원 배치 + 시니어 부재 경고 포함.
    assert pv["num_pool"] == 10
    assert any("시니어" in w for w in pv["warnings"])


def test_ward_without_g1_proceeds_when_allowed_anchored(db):
    """앵커(참여 선택) 모드도 G1 없는 병동에서 _ward_seed 폴백으로 크래시 없이 진행."""
    db.add(Office(office_id="o1", office_name="병원"))
    db.add(Group(group_id="A", group_name="A병동", office_id="o1"))
    db.add(Group(group_id="B", group_name="B병동", office_id="o1"))
    for i in range(5):
        _mk_nurse(db, f"a{i}", "A", 2)  # 양쪽 다 G1 없음
    for i in range(5):
        _mk_nurse(db, f"b{i}", "B", 2)
    db.flush()
    pv = preview_ward_redistribution(
        db, group_ids=["A", "B"], year=2026, month=7,
        participant_ids=["a0", "b0"], allow_missing_g1=True,  # 앵커 모드
    )
    assert pv["num_pool"] == 10


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
        # 버킷 = {label: {"team_id": int|None, "members": [...]}}. 전체 인원 = 병동 인원.
        flat = [m["nurse_id"] for b in w["teams"].values() for m in b["members"]]
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
        db, current_user=_ADMIN, group_ids=["A", "B"], year=2026, month=7, assignments=assignments,
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


def test_apply_writes_team_period_for_target_month(pool):
    """[B3] 재분배 apply 가 팀 변경을 nurse_team_period 에 기록한다(대상월 1일 발효).

    flush(start_date 도래) 전에도 resolve_team_for_roster 가 대상월의 새 팀을 읽어,
    미래 월 근무표에 재분배 팀이 반영되게 한다(= '재분배해도 팀 안 바뀐다' 해소).
    """
    from db.models import NurseTeamPeriod
    from services.team_period import resolve_team, resolve_team_for_roster

    db = pool
    apply_ward_redistribution(
        db, current_user=_ADMIN, group_ids=["A", "B"], year=2026, month=7,
        assignments=[
            {"nurse_id": "a0", "to_group_id": "B", "team_id": 1},   # 이동 → B, 팀1
            {"nurse_id": "a1", "to_group_id": "A", "team_id": 2},   # 잔류 팀변경 → 팀2
            {"nurse_id": "a2", "to_group_id": "A", "team_id": 1},   # 동일 → skip(기록X)
        ],
    )
    periods = {(p.nurse_id, p.group_id): p for p in db.query(NurseTeamPeriod).all()}
    # 이동: B 그룹 period, 팀1, 7/1 발효
    assert ("a0", "B") in periods
    assert periods[("a0", "B")].team_id == 1
    assert periods[("a0", "B")].valid_from == date(2026, 7, 1)
    assert periods[("a0", "B")].source == "redistribute"
    # 팀변경: A 그룹 period, 팀2
    assert periods[("a1", "A")].team_id == 2
    # skip(동일)은 기록 안 함
    assert ("a2", "A") not in periods

    # resolve: 7월(발효 후)엔 새 팀, 6월(발효 전)엔 캐시 폴백(팀1)
    assert resolve_team_for_roster(db, "a1", "A", 2026, 7) == 2
    assert resolve_team(db, "a1", "A", date(2026, 6, 15)) == 1   # gap → ward-aware 폴백
    assert resolve_team_for_roster(db, "a0", "B", 2026, 7) == 1


def test_apply_skips_unknown_or_no_target(pool):
    db = pool
    res = apply_ward_redistribution(
        db, current_user=_ADMIN, group_ids=["A", "B"], year=2026, month=7,
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
                     role="RN", allowed_shifts=[]))
    for i in range(5):
        db.add(Nurse(nurse_id=f"b{i}", account_id=f"acc_b{i}", group_id="B", office_id="o1",
                     name=f"b{i}", active=1, team_id=1, grade=1 if i == 0 else 2,
                     role="AN", allowed_shifts=[]))
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
        db, current_user=_ADMIN, group_ids=["A", "B"], year=2026, month=8,
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
                 allowed_shifts=[], preceptor_id="a0"))  # 프리셉티
    _mk_nurse(db, "b_g1", "B", 1)
    _mk_nurse(db, "b0", "B", 2)
    db.add(NurseAssignment(nurse_id="a0", source_group_id="A", target_group_id="B",
                           office_id="o1", start_date=_d(2026, 8, 1), reason="파견",
                           status="active"))  # 8월 겹침 → a0 제외
    # 로직은 period(SSOT) as-of 를 읽으므로 프리셉티 관계 period row 시드(2026-08 커버)
    from db.models import NursePrecepteePeriod
    db.add(NursePrecepteePeriod(nurse_id="a_pe", preceptor_id="a0", office_id="o1",
                                valid_from=_d(2026, 8, 1), valid_to=_d(2026, 9, 1), source="test"))
    db.flush()
    pv = preview_ward_redistribution(db, group_ids=["A", "B"], year=2026, month=8)
    pair = next(p for p in pv["pairs"] if p["preceptee_id"] == "a_pe")
    assert pair["status"] == "split"
    assert any("갈라" in w for w in pv["warnings"])


def test_preview_pool_roster_with_group_and_grade(pool):
    """옵션2 pool_roster: 풀(이동+고정) 간호사 + group_id/grade/근무코드."""
    db = pool
    pv = preview_ward_redistribution(db, group_ids=["A", "B"], year=2026, month=7)
    roster = {r["nurse_id"]: r for r in pv["pool_roster"]}
    assert "a_g1" in roster and "b_g1" in roster
    assert roster["a_g1"]["group_id"] == "A" and roster["a_g1"]["grade"] == 1
    assert roster["b0"]["group_id"] == "B" and roster["b0"]["grade"] == 2
    assert all("shift" in r and r.get("name") for r in pv["pool_roster"])
    # N전담은 풀에서 제외 → roster 에 없음
    assert "night" not in roster


def test_team_breakdown_splits_by_defined_teams_even_when_cache_null(db):
    """[운영 회귀 + 옵션A] 병동에 '정의된' 팀(Team) 수만큼 자동 분배된다 —
    멤버의 캐시 team_id 가 NULL 이고 period 배정이 없어도(= 빈 번호 팀만 만들어둔 상태).

    팀 관리(PUT /teams)가 period 모드면 캐시 team_id 는 NULL 이지만 Team 정의행은 생성된다.
    캐시 distinct 를 세던 과거 코드는 운영에서 k=1('전체')로 붕괴 → Team 정의 기준으로 수정.
    '팀 없는 병동'은 번호 팀(1,2,…)만 만들면 그 수만큼 자동 배치됨을 보장.
    """
    from services.ward_redistribute_service import _team_breakdown
    from services.team_auto_assign import NurseInput

    db.add(Office(office_id="o1", office_name="병원"))
    db.add(Group(group_id="A", group_name="A병동", office_id="o1"))
    # 번호 팀 2개 정의(멤버 배정/period 없음 — '빈 팀').
    db.add(Team(office_id="o1", group_id="A", team_id=1, team_name="1", active=1))
    db.add(Team(office_id="o1", group_id="A", team_id=2, team_name="2", active=1))
    ids = ["a0", "a1", "a2", "a3", "a4", "a5"]
    for nid in ids:
        # team_id=None → 캐시 비어 있음(운영 상태).
        db.add(Nurse(nurse_id=nid, account_id=f"acc_{nid}", group_id="A", office_id="o1",
                     name=nid, active=1, team_id=None,
                     grade=1 if nid in ("a0", "a1") else 2, allowed_shifts=[]))
    db.flush()

    by_input = {
        nid: NurseInput(nurse_id=nid, grade=1 if nid in ("a0", "a1") else 2,
                        preceptor_id=None, off_days=frozenset(), fb_days=frozenset())
        for nid in ids
    }
    res = _team_breakdown(db, "A", ids, by_input, {nid: nid for nid in ids})
    # 정의된 팀 2개 기준으로 갈라져야 함(붕괴 X). 라벨 = 실제 team_id, team_id 값도 실제.
    assert set(res.keys()) == {"1", "2"}, res
    assert res["1"]["team_id"] == 1 and res["2"]["team_id"] == 2
    flat = sorted(m["nurse_id"] for b in res.values() for m in b["members"])
    assert flat == sorted(ids)


def test_team_breakdown_forced_count_provisional_when_no_teams(db):
    """[옵션B preview] 팀 정의 0개여도 forced_count 로 가분할 — label "1".."N",
    team_id=None(미존재). 저장 시 team_label 로 팀 생성될 버킷.
    """
    from services.ward_redistribute_service import _team_breakdown
    from services.team_auto_assign import NurseInput

    db.add(Office(office_id="o1", office_name="병원"))
    db.add(Group(group_id="A", group_name="A병동", office_id="o1"))
    ids = ["a0", "a1", "a2", "a3", "a4", "a5"]
    for nid in ids:
        db.add(Nurse(nurse_id=nid, account_id=f"acc_{nid}", group_id="A", office_id="o1",
                     name=nid, active=1, team_id=None,
                     grade=1 if nid in ("a0", "a1") else 2, allowed_shifts=[]))
    db.flush()
    by_input = {
        nid: NurseInput(nurse_id=nid, grade=1 if nid in ("a0", "a1") else 2,
                        preceptor_id=None, off_days=frozenset(), fb_days=frozenset())
        for nid in ids
    }
    res = _team_breakdown(db, "A", ids, by_input, {nid: nid for nid in ids}, forced_count=2)
    assert set(res.keys()) == {"1", "2"}, res
    # 가분할이라 team_id 는 아직 None.
    assert res["1"]["team_id"] is None and res["2"]["team_id"] is None
    flat = sorted(m["nurse_id"] for b in res.values() for m in b["members"])
    assert flat == sorted(ids)


def test_team_breakdown_collapses_to_all_without_defined_teams(db):
    """대조군: 정의된 팀이 없으면 '전체' 단일 폴백 — '팀 없는 병동'에선 헛동작 안 함.
    → 옵션A: 번호 팀을 만들기 전까지는 전체 1개, 만들면 그 수만큼 분배.
    """
    from services.ward_redistribute_service import _team_breakdown
    from services.team_auto_assign import NurseInput

    db.add(Office(office_id="o1", office_name="병원"))
    db.add(Group(group_id="A", group_name="A병동", office_id="o1"))
    ids = ["a0", "a1", "a2", "a3"]
    for nid in ids:
        db.add(Nurse(nurse_id=nid, account_id=f"acc_{nid}", group_id="A", office_id="o1",
                     name=nid, active=1, team_id=None, grade=2, allowed_shifts=[]))
    db.flush()

    by_input = {
        nid: NurseInput(nurse_id=nid, grade=2, preceptor_id=None,
                        off_days=frozenset(), fb_days=frozenset())
        for nid in ids
    }
    res = _team_breakdown(db, "A", ids, by_input, {nid: nid for nid in ids})
    assert set(res.keys()) == {"전체"}, res
    assert res["전체"]["team_id"] is None


def test_apply_creates_numbered_teams_and_assigns_for_month(db):
    """[옵션B] team_label 배정 → 팀 없는 병동에 번호 팀(1,2) 생성 + 그 달 period 배정.
    저장 한 번에 '최종 팀 생성 + 그 달 할당'. 재실행해도 team_id 가 안 늘어남(멱등).
    """
    from db.models import NurseTeamPeriod
    from services.ward_redistribute_service import apply_ward_redistribution

    db.add(Office(office_id="o1", office_name="병원"))
    db.add(Group(group_id="A", group_name="A병동", office_id="o1"))
    # 팀 정의 0개 + 캐시 team_id None (= 9A 운영 상태)
    ids = ["a0", "a1", "a2", "a3"]
    for nid in ids:
        db.add(Nurse(nurse_id=nid, account_id=f"acc_{nid}", group_id="A", office_id="o1",
                     name=nid, active=1, team_id=None, grade=2, allowed_shifts=[]))
    db.flush()

    assignments = [
        {"nurse_id": "a0", "to_group_id": "A", "team_label": "1"},
        {"nurse_id": "a1", "to_group_id": "A", "team_label": "1"},
        {"nurse_id": "a2", "to_group_id": "A", "team_label": "2"},
        {"nurse_id": "a3", "to_group_id": "A", "team_label": "2"},
    ]
    res = apply_ward_redistribution(
        db, current_user=_ADMIN, group_ids=["A"], year=2026, month=7, assignments=assignments
    )

    # 번호 팀 2개 생성
    teams = db.query(Team).filter(Team.group_id == "A", Team.active == 1).all()
    assert {t.team_name for t in teams} == {"1", "2"}
    tid = {t.team_name: t.team_id for t in teams}
    assert res["created_teams"]["A"] == tid
    # 빈 병동이라 team_id 1,2 부터 부여
    assert set(tid.values()) == {1, 2}

    # 그 달(2026-07-01) period 에 배정
    periods = {p.nurse_id: p for p in
               db.query(NurseTeamPeriod).filter(NurseTeamPeriod.group_id == "A").all()}
    assert periods["a0"].team_id == tid["1"] and periods["a2"].team_id == tid["2"]
    assert periods["a0"].valid_from == date(2026, 7, 1)

    # 멱등: 같은 저장 재실행 → 팀 수 그대로(team_id 안 늘어남)
    apply_ward_redistribution(
        db, current_user=_ADMIN, group_ids=["A"], year=2026, month=7, assignments=assignments
    )
    teams2 = db.query(Team).filter(Team.group_id == "A", Team.active == 1).all()
    assert {t.team_name for t in teams2} == {"1", "2"} and len(teams2) == 2


# ── 발행된 달 가드(dev-7) — 미리보기 표시 · 적용 전체 거절 · 사람별 되돌리기 ──────────


def _admin_user():
    """실제 로그인 사용자 모양(UserSchema)의 관리자 — 같은 병원(o1) 병동만 다룬다."""
    from tests.agent_qa.harness import make_auth_user
    return make_auth_user("ADM1", "관리자", role="ADM", office_id="o1", group_id="A")


def _issue_ward(db, gid, year, month, meta="auto"):
    from db.models import IssuedRosterSnapshot, Schedule
    sid = f"ISS{gid}{year % 100:02d}{month:02d}"
    db.add(Schedule(schedule_id=sid, office_id="o1", group_id=gid, year=year, month=month,
                    version=1, status="issued", dropped=False))
    db.add(IssuedRosterSnapshot(office_id="o1", group_id=gid, schedule_id=sid, version=1,
                                is_active_issued=True, year=year, month=month,
                                meta_json={"year": year, "month": month} if meta == "auto" else meta))
    db.flush()


def _forced_moves_preview(db):
    """정원 7:3(허용 0) — A 로 2명이 반드시 옮겨 오는 미리보기."""
    return preview_ward_redistribution(db, group_ids=["A", "B"], year=2026, month=7,
                                       capacity_mode="explicit", target_sizes={"A": 7, "B": 3},
                                       size_tolerance=0)


def test_preview_flags_moves_touching_issued_ward(pool):
    db = pool
    _issue_ward(db, "A", 2026, 7)
    pv = _forced_moves_preview(db)
    assert pv["num_moved"] == 2 and pv["num_blocked"] == 2
    assert all(m["issued_blocked"] for m in pv["moves"])
    assert pv["blocked_moves"][0]["months"] == [{"group_id": "A", "year": 2026, "month": 7}]
    assert any("이미 발행된 병동(A병동)" in w and "2명" in w for w in pv["warnings"]), pv["warnings"]


def test_preview_issue_before_target_month_not_flagged(pool):
    db = pool
    _issue_ward(db, "A", 2026, 6)
    pv = _forced_moves_preview(db)
    assert pv["num_blocked"] == 0 and not any(m["issued_blocked"] for m in pv["moves"])
    assert not any("발행" in w for w in pv["warnings"])


def test_preview_flags_unknown_meta(pool):
    """연월을 읽을 수 없는 발행본이 있는 병동과 오가는 이동도 막힌 것으로 보인다(fail-closed)."""
    db = pool
    _issue_ward(db, "B", 2025, 1, meta=None)
    pv = _forced_moves_preview(db)
    assert pv["num_blocked"] == 2
    assert any("확인이 필요" in w and "B병동" in w for w in pv["warnings"]), pv["warnings"]


def test_apply_rejects_whole_batch_when_issued(pool):
    """발행된 달에 걸리는 이동이 하나라도 있으면 아무것도 쓰지 않고 409 — 조용한 실패 없음."""
    from fastapi import HTTPException
    db = pool
    _issue_ward(db, "B", 2026, 7)
    with pytest.raises(HTTPException) as ei:
        apply_ward_redistribution(
            db, current_user=_admin_user(), group_ids=["A", "B"], year=2026, month=7,
            assignments=[{"nurse_id": "a0", "to_group_id": "B", "team_label": "새팀"},
                         {"nurse_id": "a1", "to_group_id": "A", "team_id": 2}],
        )
    assert ei.value.status_code == 409
    assert ei.value.detail["code"] == "ISSUED_ROSTER_BLOCKS_TRANSFER"
    assert [m["nurse_id"] for m in ei.value.detail["blocked_moves"]] == ["a0"]
    assert "B병동 2026년 7월" in ei.value.detail["message"]
    assert db.query(NurseAssignment).count() == 0
    assert db.query(Team).filter(Team.team_name == "새팀").count() == 0


def test_apply_check_failure_503_before_writes(pool, monkeypatch):
    from fastapi import HTTPException
    from sqlalchemy import text
    from services import issued_month_guard
    db = pool
    monkeypatch.setattr(issued_month_guard, "_read_active_snapshots",
                        lambda _db, _gids: _db.execute(text("SELECT x FROM no_such_table")).all())
    with pytest.raises(HTTPException) as ei:
        apply_ward_redistribution(db, current_user=_admin_user(), group_ids=["A", "B"], year=2026, month=7,
                                  assignments=[{"nurse_id": "a0", "to_group_id": "B", "team_label": "새팀"}])
    assert ei.value.status_code == 503
    assert db.query(NurseAssignment).count() == 0
    assert db.query(Team).filter(Team.team_name == "새팀").count() == 0


def test_apply_failure_rolls_back_partial(pool, monkeypatch):
    """한 사람이 flush 뒤(팀 구간 쓰기)에서 실패해도 그 미완성 행이 다음 사람 커밋에 실리지 않는다."""
    from fastapi import HTTPException
    from services import team_period
    db = pool
    real = team_period.set_team_period

    def _flaky(_db, **kw):
        if kw["nurse_id"] == "a0":
            raise HTTPException(status_code=409, detail="같은 간호사의 팀 변경이 진행 중입니다.")
        return real(_db, **kw)

    monkeypatch.setattr(team_period, "set_team_period", _flaky)  # create_assignment 안의 호출만 바뀐다
    res = apply_ward_redistribution(
        db, current_user=_admin_user(), group_ids=["A", "B"], year=2026, month=7,
        assignments=[{"nurse_id": "a0", "to_group_id": "B", "team_id": 1},
                     {"nurse_id": "a1", "to_group_id": "B", "team_id": 1}],
    )
    assert res["transfers"] == 1 and [f["nurse_id"] for f in res["failed"]] == ["a0"]
    assert {r.nurse_id for r in db.query(NurseAssignment).all()} == {"a1"}


def test_apply_foreign_target_not_judged_or_leaked(pool):
    """다른 병원 병동으로 가는 이동은 선행 판정에 넣지 않는다 — 그 병원 발행본이 있어도 409·병동 이름 없이
    사람별 병원 경계 오류(400)로 실패 목록에 담긴다(dev-7 리뷰 · dev-7 전과 같은 결과)."""
    from db.models import IssuedRosterSnapshot, Schedule
    db = pool
    db.add(Office(office_id="o2", office_name="남의병원"))
    db.add(Group(group_id="X", group_name="타병원비밀병동", office_id="o2"))
    db.add(Schedule(schedule_id="ISSX2607", office_id="o2", group_id="X", year=2026, month=7,
                    version=1, status="issued", dropped=False))
    db.add(IssuedRosterSnapshot(office_id="o2", group_id="X", schedule_id="ISSX2607", version=1,
                                is_active_issued=True, meta_json={"year": 2026, "month": 7}))
    db.flush()
    res = apply_ward_redistribution(db, current_user=_admin_user(), group_ids=["A", "B"], year=2026, month=7,
                                    assignments=[{"nurse_id": "a0", "to_group_id": "X", "team_id": 1}])
    assert res["transfers"] == 0 and [f["nurse_id"] for f in res["failed"]] == ["a0"]
    assert "office 경계 위반" in res["failed"][0]["reason"]
    assert "타병원비밀병동" not in str(res) and db.query(NurseAssignment).count() == 0


def test_apply_takes_month_lock_before_team_creation(pool, monkeypatch):
    """마감 달 잠금을 0단계 팀 생성(INSERT teams)보다 먼저 잡는다 — 팀 행을 잠근 채 잠금을 기다리면
    그 잠금을 쥐고 그 병동 팀을 읽는 마감과 교착한다(잠금 → 행)."""
    from sqlalchemy import event
    from db.models import Schedule
    from services import schedule_versioning
    db = pool
    db.add(Schedule(schedule_id="SB2607", office_id="o1", group_id="B", year=2026, month=7,
                    version=1, status="draft", dropped=False))
    db.flush()
    events = []
    monkeypatch.setattr(schedule_versioning, "lock_month_publish",
                        lambda _db, g, y, m: events.append(("lock", g, y, m)))

    def _spy(conn, cursor, statement, params, context, executemany):
        if statement.strip().upper().startswith("INSERT INTO TEAMS"):
            events.append(("INSERT teams",))

    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", _spy)
    try:
        res = apply_ward_redistribution(db, current_user=_admin_user(), group_ids=["A", "B"], year=2026, month=7,
                                        assignments=[{"nurse_id": "a0", "to_group_id": "B", "team_label": "새팀"}])
    finally:
        event.remove(engine, "before_cursor_execute", _spy)
    assert res["transfers"] == 1, res
    assert events[0] == ("lock", "B", 2026, 7) and ("INSERT teams",) in events, events
