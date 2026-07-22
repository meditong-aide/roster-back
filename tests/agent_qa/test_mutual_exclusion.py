"""manage_mutual_exclusion — 두 간호사 상호배제 설정/해제 (preview→apply, 1:1)."""
from agents_v2.middleware import execute_skill
from agents_v2.schemas.session_context import SessionContext
from db.models import NurseMutualExclusionPeriod as MEP


def _hn():
    return SessionContext(office_id="OFF001", group_id="GRP001", year=2026, month=8,
                          nurse_id="N001", nurse_name="김민지", user_role="HN")


def test_set_preview_no_write(db, seed_data):
    res = execute_skill(db, "manage_mutual_exclusion",
                        {"operation": "set", "nurse_name": "김민지", "partner_name": "이수정",
                         "preview_only": True}, _hn())
    assert res.data.get("preview") is True
    assert res.data["summary"] == {"nurse_name": "김민지", "partner_name": "이수정"}
    assert db.query(MEP).count() == 0


def test_set_apply_creates_bidirectional(db, seed_data):
    res = execute_skill(db, "manage_mutual_exclusion",
                        {"operation": "set", "nurse_name": "김민지", "partner_name": "이수정",
                         "preview_only": False}, _hn())
    assert res.data.get("ok") is True, res.data
    rows = db.query(MEP).filter(MEP.valid_to.is_(None)).all()
    pairs = {(r.nurse_id, r.partner_id) for r in rows}
    # 양방향 대칭 (N001=김민지, N003=이수정)
    assert ("N001", "N003") in pairs and ("N003", "N001") in pairs


def test_release(db, seed_data):
    execute_skill(db, "manage_mutual_exclusion",
                  {"operation": "set", "nurse_name": "김민지", "partner_name": "이수정",
                   "preview_only": False}, _hn())
    res = execute_skill(db, "manage_mutual_exclusion",
                        {"operation": "release", "nurse_name": "김민지", "preview_only": False}, _hn())
    assert res.data.get("ok") is True
    # 활성(valid_to=None) 상호배제 남지 않아야
    active = db.query(MEP).filter(MEP.valid_to.is_(None)).count()
    assert active == 0, f"해제 후 활성 {active}건 남음"


def test_self_pair_rejected(db, seed_data):
    res = execute_skill(db, "manage_mutual_exclusion",
                        {"operation": "set", "nurse_name": "김민지", "partner_name": "김민지",
                         "preview_only": False}, _hn())
    assert res.data.get("error")
