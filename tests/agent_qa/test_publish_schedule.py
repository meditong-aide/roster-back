"""publish_schedule — 근무표 확정/발행 (status→issued + IssuedRoster + 스냅샷). 스냅샷 박히는지 검증."""
from agents_v2.middleware import execute_skill
from agents_v2.schemas.session_context import SessionContext
from db.models import Schedule, IssuedRoster, IssuedRosterSnapshot


def _hn():
    return SessionContext(office_id="OFF001", group_id="GRP001", year=2026, month=4,
                          nurse_id="N001", nurse_name="김민지", user_role="HN")


def test_publish_preview_no_write(db, seed_data):
    res = execute_skill(db, "publish_schedule",
                        {"year": 2026, "month": 4, "preview_only": True}, _hn())
    assert res.data.get("preview") is True
    assert res.data["summary"]["year"] == 2026 and res.data["summary"]["month"] == 4
    assert db.query(IssuedRosterSnapshot).count() == 0  # 미발행


def test_publish_apply_creates_snapshot_and_issues(db, seed_data):
    res = execute_skill(db, "publish_schedule",
                        {"year": 2026, "month": 4, "preview_only": False}, _hn())
    assert res.data.get("ok") is True, res.data
    # 1) 스냅샷이 제대로 박혔나 (핵심)
    snaps = db.query(IssuedRosterSnapshot).filter_by(group_id="GRP001", year=2026, month=4).all()
    assert len(snaps) == 1
    snap = snaps[0]
    assert snap.is_active_issued is True
    assert snap.schedule_id == res.data["schedule_id"]
    assert snap.meta_json and snap.meta_json.get("issued_by_nurse_id") == "N001"
    assert snap.roster_json is not None  # 근무표 본문 스냅샷 존재
    # 2) IssuedRoster 기록
    ir = db.query(IssuedRoster).filter_by(group_id="GRP001").all()
    assert len(ir) == 1 and ir[0].nurse_id == "N001"
    # 3) 스케줄 status → issued
    sched = db.query(Schedule).filter_by(schedule_id=res.data["schedule_id"]).first()
    assert sched.status == "issued"


def test_republish_same_version_is_idempotent(db, seed_data):
    # 같은 버전 재발행 = 크래시 없이 멱등(already_issued), 중복 IssuedRoster/스냅샷 안 만듦.
    execute_skill(db, "publish_schedule", {"year": 2026, "month": 4, "preview_only": False}, _hn())
    res2 = execute_skill(db, "publish_schedule", {"year": 2026, "month": 4, "preview_only": False}, _hn())
    assert res2.data.get("already_issued") is True
    assert db.query(IssuedRoster).filter_by(group_id="GRP001").count() == 1
    assert db.query(IssuedRosterSnapshot).filter_by(group_id="GRP001", year=2026, month=4).count() == 1
