"""manage_banned_wanted — 금지 원티드 조회·추가·해제 + 스냅샷 replace 함정 방어.

가장 중요한 축: 저장 서비스가 **스냅샷 replace** 라 델타만 넘기면 나머지 금지가
조용히 사라진다. 그 사고가 재발하지 않는지(다른 간호사·다른 날 금지 보존)를 못 박는다.
"""
from datetime import date

from agents_v2.errors import ErrorType, classify
from agents_v2.middleware import execute_skill
from agents_v2.schemas.session_context import SessionContext
from db.models import BannedWantedEntry


def _hn():
    return SessionContext(office_id="OFF001", group_id="GRP001", year=2026, month=8,
                          nurse_id="N001", nurse_name="김민지", user_role="HN")


def _add(nurse="박지은", dates=("2026-08-15",), shifts=("나이트",), preview=False, **over):
    p = {"operation": "add", "nurse_name": nurse, "dates": list(dates),
         "banned_shifts": list(shifts), "preview_only": preview}
    p.update(over)
    return p


def _seed_row(db, nurse_id, day, codes, source="hn"):
    db.add(BannedWantedEntry(group_id="GRP001", year=2026, month=8, nurse_id=nurse_id,
                             shift_date=date(2026, 8, day), banned_shift_ids=list(codes),
                             is_applied=True, source=source))
    db.flush()


def _codes_of(db, nurse_id, day):
    r = (db.query(BannedWantedEntry)
         .filter_by(group_id="GRP001", nurse_id=nurse_id, shift_date=date(2026, 8, day))
         .first())
    return sorted(r.banned_shift_ids) if r else None


# ── add ─────────────────────────────────────────────────


def test_add_writes_entry(db, seed_data):
    res = execute_skill(db, "manage_banned_wanted", _add(), _hn())
    assert res.data.get("ok") is True and res.data.get("verification_failed") is not True
    assert _codes_of(db, "N002", 15) == ["N"]


def test_add_preview_does_not_persist(db, seed_data):
    res = execute_skill(db, "manage_banned_wanted", _add(preview=True), _hn())
    assert res.data.get("preview") is True and classify(res.data) is ErrorType.PREVIEW
    assert db.query(BannedWantedEntry).count() == 0


def test_add_multiple_dates_and_shifts(db, seed_data):
    res = execute_skill(db, "manage_banned_wanted",
                        _add(dates=("2026-08-20", "2026-08-21"), shifts=("데이", "이브닝")), _hn())
    assert res.data.get("ok") is True
    assert _codes_of(db, "N002", 20) == ["D", "E"]
    assert _codes_of(db, "N002", 21) == ["D", "E"]


def test_add_preserves_other_entries(db, seed_data):
    """★ 스냅샷 replace 함정 — 델타만 넘기면 남의 금지가 지워진다."""
    _seed_row(db, "N003", 5, ["D"])
    _seed_row(db, "N002", 9, ["E"])

    execute_skill(db, "manage_banned_wanted", _add(dates=("2026-08-15",)), _hn())

    assert _codes_of(db, "N003", 5) == ["D"], "다른 간호사 금지가 지워졌다"
    assert _codes_of(db, "N002", 9) == ["E"], "같은 간호사 다른 날 금지가 지워졌다"
    assert _codes_of(db, "N002", 15) == ["N"]


def test_add_merges_into_existing_cell(db, seed_data):
    _seed_row(db, "N002", 15, ["D"])
    res = execute_skill(db, "manage_banned_wanted", _add(dates=("2026-08-15",)), _hn())
    assert res.data.get("ok") is True
    assert _codes_of(db, "N002", 15) == ["D", "N"]
    assert res.data["summary"]["기존_금지에_추가됨"] == ["2026-08-15"]


def test_add_preserves_nurse_owned_entries(db, seed_data):
    """간호사 본인이 낸 기피근무(source='nurse')는 HN 스냅샷 교체에 휩쓸리면 안 된다."""
    _seed_row(db, "N003", 7, ["N"], source="nurse")
    execute_skill(db, "manage_banned_wanted", _add(), _hn())
    row = (db.query(BannedWantedEntry)
           .filter_by(nurse_id="N003", shift_date=date(2026, 8, 7)).first())
    assert row is not None and row.source == "nurse"


def test_add_rejects_unknown_shift(db, seed_data):
    res = execute_skill(db, "manage_banned_wanted", _add(shifts=("잠자기",)), _hn())
    assert "error" in res.data and "모르겠습니다" in res.data["error"]


def test_add_rejects_out_of_month_date(db, seed_data):
    res = execute_skill(db, "manage_banned_wanted", _add(dates=("2026-09-03",)), _hn())
    assert "error" in res.data and "9월" not in res.data["error"].split(":")[0]
    assert "2026-09-03" in res.data["error"]


def test_add_blocks_when_no_option_left(db, seed_data):
    """모든 근무형 + OFF 를 금지하면 배정 가능한 게 없다 — 서비스 422 를 문장으로.

    ★ 'O' 를 빼면 안 된다 — 서비스는 OFF 를 항상 남는 옵션으로 치므로 그때는 통과한다.
    """
    res = execute_skill(db, "manage_banned_wanted",
                        _add(shifts=("D", "E", "N", "M", "OFF", "V", "O")), _hn())
    assert "error" in res.data
    assert db.query(BannedWantedEntry).count() == 0


def test_add_without_nurse_or_date_clarifies(db, seed_data):
    r1 = execute_skill(db, "manage_banned_wanted",
                       {"operation": "add", "dates": ["2026-08-15"],
                        "banned_shifts": ["N"], "preview_only": False}, _hn())
    assert r1.data.get("needs_clarification") is True
    r2 = execute_skill(db, "manage_banned_wanted",
                       {"operation": "add", "nurse_name": "박지은",
                        "banned_shifts": ["N"], "preview_only": False}, _hn())
    assert r2.data.get("needs_clarification") is True


# ── remove / clear ──────────────────────────────────────


def test_remove_one_date_keeps_others(db, seed_data):
    _seed_row(db, "N002", 15, ["N"])
    _seed_row(db, "N002", 16, ["D"])
    _seed_row(db, "N003", 15, ["E"])

    res = execute_skill(db, "manage_banned_wanted",
                        {"operation": "remove", "nurse_name": "박지은",
                         "dates": ["2026-08-15"], "preview_only": False}, _hn())
    assert res.data.get("ok") is True and res.data.get("verification_failed") is not True
    assert _codes_of(db, "N002", 15) is None
    assert _codes_of(db, "N002", 16) == ["D"]
    assert _codes_of(db, "N003", 15) == ["E"]


def test_remove_all_dates_for_nurse(db, seed_data):
    _seed_row(db, "N002", 15, ["N"])
    _seed_row(db, "N002", 16, ["D"])
    res = execute_skill(db, "manage_banned_wanted",
                        {"operation": "remove", "nurse_name": "박지은", "preview_only": False}, _hn())
    assert res.data.get("ok") is True
    assert _codes_of(db, "N002", 15) is None and _codes_of(db, "N002", 16) is None


def test_remove_nurse_owned_is_refused_with_reason(db, seed_data):
    _seed_row(db, "N002", 15, ["N"], source="nurse")
    res = execute_skill(db, "manage_banned_wanted",
                        {"operation": "remove", "nurse_name": "박지은",
                         "dates": ["2026-08-15"], "preview_only": False}, _hn())
    assert "error" in res.data and "기피근무" in res.data["error"]
    assert _codes_of(db, "N002", 15) == ["N"]  # 보존


def test_clear_removes_hn_only(db, seed_data):
    _seed_row(db, "N002", 15, ["N"])
    _seed_row(db, "N003", 7, ["D"], source="nurse")
    res = execute_skill(db, "manage_banned_wanted",
                        {"operation": "clear", "preview_only": False}, _hn())
    assert res.data.get("ok") is True
    assert _codes_of(db, "N002", 15) is None
    assert _codes_of(db, "N003", 7) == ["D"], "간호사 기피근무까지 지워졌다"


def test_clear_preview(db, seed_data):
    _seed_row(db, "N002", 15, ["N"])
    res = execute_skill(db, "manage_banned_wanted", {"operation": "clear"}, _hn())
    assert res.data.get("preview") is True and res.data["summary"]["count"] == 1
    assert _codes_of(db, "N002", 15) == ["N"]


# ── list ────────────────────────────────────────────────


def test_list_shows_source_and_applied(db, seed_data):
    _seed_row(db, "N002", 15, ["N"])
    _seed_row(db, "N003", 7, ["D"], source="nurse")
    res = execute_skill(db, "manage_banned_wanted", {"operation": "list"}, _hn())
    d = res.data
    assert d["count"] == 2
    by_nurse = {e["nurse"]: e for e in d["entries"]}
    assert by_nurse["박지은"]["출처"] == "수간호사 지정"
    assert by_nurse["이수정"]["출처"] == "간호사 기피근무"
    assert by_nurse["박지은"]["banned"] == ["N"]


def test_list_filters_by_nurse(db, seed_data):
    _seed_row(db, "N002", 15, ["N"])
    _seed_row(db, "N003", 7, ["D"])
    res = execute_skill(db, "manage_banned_wanted",
                        {"operation": "list", "nurse_name": "박지은"}, _hn())
    assert res.data["count"] == 1 and res.data["entries"][0]["nurse"] == "박지은"


def test_list_empty_message(db, seed_data):
    res = execute_skill(db, "manage_banned_wanted", {"operation": "list"}, _hn())
    assert res.data["count"] == 0 and "없어요" in res.data["message"]


# ── read-back / 권한 / 매니페스트 ───────────────────────


def test_readback_catches_false_complete(db, seed_data, monkeypatch):
    import agents_v2.skills.manage_banned_wanted as mbw

    monkeypatch.setattr(mbw, "_save", lambda *a, **k: ([], []))
    res = execute_skill(db, "manage_banned_wanted", _add(), _hn())
    assert res.data.get("verification_failed") is True
    assert classify(res.data) is ErrorType.VERIFICATION_FAILED


def test_general_nurse_blocked(db, seed_data):
    ctx = SessionContext(office_id="OFF001", group_id="GRP001", year=2026, month=8,
                         nurse_id="N002", nurse_name="박지은", user_role="RN")
    res = execute_skill(db, "manage_banned_wanted", _add(), ctx)
    assert "error" in res.data
    assert db.query(BannedWantedEntry).count() == 0


def test_manifest_wiring():
    from agents_v2.router import CATEGORY_TOOLS
    from agents_v2.skills.descriptions import SKILL_TOOLS
    from agents_v2.skills.manifest import SKILL_SPECS, load_manifest_skills

    load_manifest_skills()
    spec = SKILL_SPECS["manage_banned_wanted"]
    assert spec.mutation and spec.hn_only and spec.trigger_hint
    assert "manage_banned_wanted" in {t["name"] for t in SKILL_TOOLS}
    for cat in ("mutate", "read"):
        assert "manage_banned_wanted" in CATEGORY_TOOLS[cat]
