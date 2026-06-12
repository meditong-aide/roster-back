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


def test_apply_records_team_period_before_flush(ward):
    """[B3 회귀] apply 가 nurse_team_period 에 기록 → flush(발효) 전에도 resolve_team
    이 새 팀 반환. classify 가 set_team_period 를 빠뜨려 단일 group 팀변경이
    nurse_team_period 에 안 들어가던 버그(화면/생성기 미반영) 방지. 재분배와 동일 SSOT."""
    from db.models import NurseTeamPeriod
    from services.team_period import resolve_team

    db = ward
    pv = preview_team_classification(db, group_id="A", year=2026, month=8)
    flat = [{"nurse_id": nid, "team_id": int(t)}
            for t, members in pv["teams"].items() for nid in members]
    res = apply_team_classification(
        db, group_id="A", office_id="o1", year=2026, month=8, assignments=flat,
    )
    # 변경분(created)이 nurse_team_period(발효일=2026-08-01)에 기록됨
    period_cnt = db.query(NurseTeamPeriod).filter(
        NurseTeamPeriod.group_id == "A",
        NurseTeamPeriod.valid_from == date(2026, 8, 1),
    ).count()
    assert period_cnt == res["created"]
    # flush 전인데도 resolve_team(발효일)이 제안 팀과 일치(화면·생성기 즉시 반영)
    proposed = {nid: int(t) for t, members in pv["teams"].items() for nid in members}
    for nid, tid in proposed.items():
        assert resolve_team(db, nid, "A", date(2026, 8, 1)) == tid


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


def _set_preceptor(db, preceptee, preceptor):
    db.query(Nurse).filter(Nurse.nurse_id == preceptee).update(
        {"preceptor_id": preceptor}, synchronize_session=False
    )
    db.flush()


def test_pair_together_no_split_warning(ward):
    """둘 다 참여하면 hard follow 로 같은 팀 → 갈라짐 경고 없음."""
    db = ward
    _set_preceptor(db, "n3", "g1a")  # n3=프리셉티, g1a=프리셉터
    pv = preview_team_classification(db, group_id="A", year=2026, month=8)
    pair = next(p for p in pv["pairs"] if p["preceptee_id"] == "n3")
    assert pair["status"] == "together"
    assert not any("갈라" in w for w in pv["warnings"])


def test_pair_split_warns_when_preceptor_non_participant(ward):
    """프리셉터가 미참여(미지정)면 프리셉티가 따라갈 수 없어 짝이 갈라짐 → 경고."""
    db = ward
    _set_preceptor(db, "n3", "g1a")
    # g1b 를 시드로 남기고 g1a(프리셉터) 미참여 → 단 팀 시니어 수 확보 위해 g1b 외 G1 필요.
    # num_teams=2 이므로 G1 2명 필요 → g1a 빠지면 부족. 대신 프리셉티 n3 를 미참여로.
    participants = ["g1a", "g1b", "n1", "n2", "n4", "n5", "n6"]  # n3 제외
    pv = preview_team_classification(
        db, group_id="A", year=2026, month=8, participant_ids=participants,
    )
    pair = next(p for p in pv["pairs"] if p["preceptee_id"] == "n3")
    assert pair["status"] == "split"
    assert any("갈라" in w for w in pv["warnings"])


def test_pair_release_emits_event_and_flush_clears(ward):
    """해제(release): apply 시 프리셉터십 종료 이벤트 → flush 후 preceptor_id None."""
    db = ward
    _set_preceptor(db, "n3", "g1a")
    res = apply_team_classification(
        db, group_id="A", office_id="o1", year=2026, month=8,
        assignments=[], pair_decisions={"n3": "release"},
    )
    assert res["released"] == 1
    flush_pending_permanent_changes(db, as_of=date(2026, 8, 1))
    n3 = db.query(Nurse).filter(Nurse.nurse_id == "n3").first()
    assert n3.preceptor_id is None


def test_pair_release_drops_follow_in_preview(ward):
    """해제 선택 시 preview 에서 status=released (강제 follow 대상 아님)."""
    db = ward
    _set_preceptor(db, "n3", "g1a")
    pv = preview_team_classification(
        db, group_id="A", year=2026, month=8, pair_decisions={"n3": "release"},
    )
    pair = next(p for p in pv["pairs"] if p["preceptee_id"] == "n3")
    assert pair["status"] == "released"


def test_preview_pool_roster_has_shift_grade_preceptor(ward):
    """pool_roster: 근무코드(is_night []=전체) + grade + 프리셉터 정보."""
    db = ward
    _set_preceptor(db, "n3", "g1a")  # n3=프리셉티, g1a=프리셉터
    pv = preview_team_classification(db, group_id="A", year=2026, month=8)
    roster = {r["nurse_id"]: r for r in pv["pool_roster"]}
    assert "night" in roster and "g1a" in roster
    # 근무코드
    assert roster["night"]["shift"] == "N" and roster["night"]["is_night"] is True
    assert roster["g1a"]["shift"] == "전체" and roster["g1a"]["is_night"] is False
    # grade
    assert roster["g1a"]["grade"] == 1 and roster["n3"]["grade"] == 3
    # 프리셉터: n3 는 g1a(수간A)의 프리셉티, g1a 는 프리셉터
    assert roster["n3"]["preceptor_name"] == "수간A"
    assert roster["g1a"]["is_preceptor"] is True
    assert roster["n3"]["is_preceptor"] is False
    assert all(r.get("name") for r in pv["pool_roster"])
