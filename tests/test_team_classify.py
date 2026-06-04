"""원티드 기반 팀 분류(옵션1) — preview(read-only) → apply(permanent_change) → flush 전 과정.

참조: app/services/team_classify_service.py, docs/NURSE_GROUP_CHANGE_MODEL.md (옵션1).
"""

from __future__ import annotations

from datetime import date

import pytest

from db.models import (
    FixedWantedEntry, Group, Nurse, NurseAssignment, Office, Shift, Team,
)
from services.assignment_service import flush_pending_permanent_changes
from services.team_classify_service import (
    apply_team_classification,
    preview_team_classification,
)


def _mk_nurse(db, nid, team_id, grade, name):
    db.add(Nurse(
        nurse_id=nid, account_id=f"acc_{nid}", group_id="A", office_id="o1",
        name=name, active=1, team_id=team_id, grade=grade, is_night_nurse=[],
    ))


@pytest.fixture
def ward(db):
    db.add(Office(office_id="o1", office_name="병원"))
    db.add(Group(group_id="A", group_name="A병동", office_id="o1"))
    db.add(Team(office_id="o1", group_id="A", team_id=1, team_name="1팀"))
    db.add(Team(office_id="o1", group_id="A", team_id=2, team_name="2팀"))
    # 휴가 shift (연차/FB 판별)
    db.add(Shift(shift_id="V", id=1, office_id="o1", group_id="A", name="연차",
                 color="#fff", type="휴가"))
    # 8명: grade-1 시드 2명 + 나머지 6명, 현재 team 1/2 분산
    _mk_nurse(db, "g1a", 1, 1, "수간A")
    _mk_nurse(db, "g1b", 2, 1, "수간B")
    for i, (nid, tid, g) in enumerate([
        ("n1", 1, 2), ("n2", 1, 2), ("n3", 1, 3),
        ("n4", 2, 2), ("n5", 2, 2), ("n6", 2, 3),
    ]):
        _mk_nurse(db, nid, tid, g, f"간호{i}")
    # N전담 1명 (풀 제외 대상)
    db.add(Nurse(nurse_id="night", account_id="acc_night", group_id="A",
                 office_id="o1", name="야간전담", active=1, team_id=1,
                 grade=2, is_night_nurse=["N"]))
    # 확정 원티드 OFF 몇 건 (겹침 신호)
    for nid, day in [("n1", 5), ("n2", 5), ("n3", 12), ("n4", 20), ("n5", 20)]:
        db.add(FixedWantedEntry(
            group_id="A", year=2026, month=8, nurse_id=nid,
            shift_date=date(2026, 8, day), shift_id="O", source_type="original",
        ))
    # 연차(휴가) 1건
    db.add(FixedWantedEntry(
        group_id="A", year=2026, month=8, nurse_id="n6",
        shift_date=date(2026, 8, 7), shift_id="V", source_type="original",
    ))
    db.flush()
    return db


def test_preview_is_readonly_and_covers_pool(ward):
    db = ward
    pv = preview_team_classification(db, group_id="A", year=2026, month=8)
    assert pv["target_month"] == "2026-08"
    assert pv["num_teams"] == 2
    assert pv["num_pool"] == 8           # N전담 제외
    assert pv["num_excluded_night"] == 1
    # 모든 풀 간호사가 정확히 한 팀에 배정
    assigned = [nid for members in pv["teams"].values() for nid in members]
    assert sorted(assigned) == sorted(
        ["g1a", "g1b", "n1", "n2", "n3", "n4", "n5", "n6"]
    )
    # read-only: 이벤트 생성 없음
    assert db.query(NurseAssignment).count() == 0


def test_apply_creates_permanent_change_only_for_changed(ward):
    db = ward
    pv = preview_team_classification(db, group_id="A", year=2026, month=8)
    flat = [{"nurse_id": nid, "team_id": int(t)}
            for t, members in pv["teams"].items() for nid in members]
    res = apply_team_classification(
        db, group_id="A", office_id="o1", year=2026, month=8, assignments=flat,
    )
    assert res["effective_date"] == "2026-08-01"
    assert res["created"] == pv["num_changed"]
    assert res["created"] + res["skipped"] == len(flat)
    # 생성된 이벤트는 전부 permanent_change, 미래 발효
    rows = db.query(NurseAssignment).all()
    assert all(r.kind == "permanent_change" for r in rows)
    assert all(r.start_date == date(2026, 8, 1) for r in rows)
    assert len(rows) == res["created"]


def test_flush_applies_team_changes(ward):
    db = ward
    pv = preview_team_classification(db, group_id="A", year=2026, month=8)
    flat = [{"nurse_id": nid, "team_id": int(t)}
            for t, members in pv["teams"].items() for nid in members]
    apply_team_classification(
        db, group_id="A", office_id="o1", year=2026, month=8, assignments=flat,
    )
    # 발효 전: team_id 변동 없음
    flush_pending_permanent_changes(db, as_of=date(2026, 7, 31))
    # 발효일: 적용
    n = flush_pending_permanent_changes(db, as_of=date(2026, 8, 1))
    assert n == pv["num_changed"]
    # 발효 후 실제 team_id 가 제안과 일치
    proposed = {nid: int(t) for t, members in pv["teams"].items() for nid in members}
    for nid, tid in proposed.items():
        nurse = db.query(Nurse).filter(Nurse.nurse_id == nid).first()
        assert nurse.team_id == tid


def test_participant_subset_puts_others_in_unassigned(ward):
    db = ward
    # 일부만 참여(g1a,g1b 시드 + n1,n4) → 나머지 적격은 미지정, night는 제외
    participants = ["g1a", "g1b", "n1", "n4"]
    pv = preview_team_classification(
        db, group_id="A", year=2026, month=8, participant_ids=participants,
    )
    assigned = [nid for members in pv["teams"].values() for nid in members]
    assert sorted(assigned) == sorted(participants)
    unassigned_ids = {u["nurse_id"] for u in pv["unassigned"]}
    assert unassigned_ids == {"n2", "n3", "n5", "n6"}  # 미참여 적격
    assert {e["nurse_id"] for e in pv["excluded_night"]} == {"night"}


def test_n_nurse_override_includes_and_releases(ward):
    db = ward
    # night(N전담)을 참여에 포함(override) → 풀에 들어오고, apply 시 N전담 해제 동반
    participants = ["g1a", "g1b", "n1", "n2", "n3", "n4", "n5", "n6", "night"]
    pv = preview_team_classification(
        db, group_id="A", year=2026, month=8, participant_ids=participants,
    )
    assigned = [nid for members in pv["teams"].values() for nid in members]
    assert "night" in assigned                       # 팀에 편입됨
    assert pv["num_excluded_night"] == 0             # 더 이상 제외 아님

    flat = [{"nurse_id": nid, "team_id": int(t)}
            for t, members in pv["teams"].items() for nid in members]
    apply_team_classification(
        db, group_id="A", office_id="o1", year=2026, month=8, assignments=flat,
    )
    flush_pending_permanent_changes(db, as_of=date(2026, 8, 1))
    night = db.query(Nurse).filter(Nurse.nurse_id == "night").first()
    assert (night.is_night_nurse or []) == []        # N전담 해제됨
