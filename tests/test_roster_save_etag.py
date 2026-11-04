"""근무표 저장 내용 지문(etag) — 자동 저장(디바운싱)이 옛 요청으로 최신 근무표를 덮지 않게(11-04 결정 D8).

검증 대상(`app/routers/roster.py` `save_roster` · `app/services/schedule_etag.py`):
- 조회 지문 = 저장 직전 지문 · 저장 응답 지문 = 저장 뒤 조회 지문(연속 자동 저장이 이어진다)
- `base_etag` 가 지금과 다르면 409 `ROSTER_STALE`(칸은 그대로) — 근무표 행을 안 건드리는 칸 수정·
  잠금 전에 다른 저장이 바꾼 메모도 잡는다
- 바뀐 것이 없으면 쓰지 않는다(`unchanged`) · 본문에 없는 간호사 칸은 보존(`remove_nurse_ids` 로만 지움)
- 모양 검사 400(연월 불일치·월 일수를 넘는 날의 근무·같은 간호사 행 중복) · 지문 확인 실패 503
- `base_etag` 없는 기존 요청은 예전처럼 전량 교체 · memo 키가 없으면 메모를 지우지 않는다
- AIDE 칸 수정(`schedule_tools.update_schedule_entry`)은 저장과 같은 근무표 행 잠금에 줄을 선다(11-04 Codex)
"""
from __future__ import annotations

from datetime import date, datetime

import pytest
import routers.roster as roster_router
import services.schedule_history_service as history
from agents_v2.tools import schedule_tools
from db.models import Group, Nurse, Office, Schedule, ScheduleEntry, Shift
from fastapi import HTTPException
from routers.roster import get_roster_by_schedule_id, save_roster
from schemas.auth_schema import User as UserSchema

SID = "S1"
DAYS = 30                                   # 2026-11
IDS = {"D": 101, "N": 102, "O": 103}        # shifts.id


def _user():
    return UserSchema(
        nurse_id="HN", account_id="acc_HN", office_id="o1", group_id="W1",
        is_head_nurse=True, is_master_admin=False, name="수간",
        EmpSeqNo="", EmpAuthGbn="", mb_part="", office_name="병원", mb_part_name="",
        official_title_name=None, is_nurse_registered=True,
        hn_auth="HN", original_group_id="W1", gw_useYN="Y", qpis_useYN="Y",
    )


def _entry(db, nurse_id, day, code):
    db.add(ScheduleEntry(entry_id=f"{nurse_id}-{day:02d}-{code}", schedule_id=SID, nurse_id=nurse_id,
                         work_date=datetime(2026, 11, day), shift_id=code, id=IDS[code]))


@pytest.fixture
def ward(db, monkeypatch):
    # 저장의 commit/rollback 이 conftest 단일 트랜잭션(롤백 격리)을 깨지 않게 바꿔 둔다.
    db.commit = db.flush  # type: ignore[assignment]
    rollbacks: list = []
    monkeypatch.setattr(db, "rollback", lambda: rollbacks.append(1))
    logged: list = []

    def _log(_db, **kw):  # 이력 표는 테스트 DB 에 없다 — 바뀐 칸으로 무엇을 넘겼는지만 본다
        before, after = kw["before"], kw["after"]
        logged.append({c for c in set(before) | set(after) if before.get(c) != after.get(c)})
        return len(logged[-1])

    monkeypatch.setattr(history, "log_manual_changes", _log)
    db.add(Office(office_id="o1", office_name="병원"))
    db.add(Group(group_id="W1", group_name="1병동", office_id="o1"))
    for nid in ("HN", "N1", "N2", "N3"):
        db.add(Nurse(nurse_id=nid, account_id=f"acc_{nid}", group_id="W1", office_id="o1", name=nid,
                     active=1, is_head_nurse=(nid == "HN"), hn_auth="HN" if nid == "HN" else None,
                     allowed_shifts=[]))
    for seq, (code, int_id) in enumerate(IDS.items(), start=1):
        db.add(Shift(shift_id=code, id=int_id, office_id="o1", group_id="W1", name=code,
                     color="#000000", sequence=seq))
    db.add(Schedule(schedule_id=SID, group_id="W1", office_id="o1", year=2026, month=11, version=1,
                    status="draft", dropped=False, name="11월 근무표 VER1", memo="메모0"))
    for nid, day, code in (("N1", 1, "D"), ("N1", 2, "N"), ("N2", 1, "O"), ("N2", 3, "D")):
        _entry(db, nid, day, code)
    db.flush()
    return {"db": db, "rollbacks": rollbacks, "logged": logged}


def _row(nurse_id, codes: dict):
    return {"nurse_id": nurse_id, "schedule": [codes.get(d, "-") for d in range(1, DAYS + 1)]}


def _seed_rows():
    """시드와 같은 내용의 저장 본문(화면처럼 칸 없는 간호사 행도 싣는다)."""
    return [_row("N1", {1: "D", 2: "N"}), _row("N2", {1: "O", 3: "D"}), _row("N3", {})]


def _body(roster, *, etag=None, **extra):
    body = {"year": 2026, "month": 11, "schedule_id": SID, "memo": "메모0", "roster": roster, **extra}
    if etag is not None:
        body["base_etag"] = etag
    return body


def _save(db, body):
    return save_roster(body, None, _user(), db)


def _get_etag(db):
    return get_roster_by_schedule_id(SID, None, _user(), db)["etag"]


def _cells(db):
    return sorted((e.nurse_id, e.work_date.day, e.shift_id, e.id)
                  for e in db.query(ScheduleEntry).filter(ScheduleEntry.schedule_id == SID).all())


def _memo(db):
    return db.query(Schedule.memo).filter(Schedule.schedule_id == SID).scalar()


def test_save_etag_chains_with_get(ward):
    db = ward["db"]
    first = _get_etag(db)
    res = _save(db, _body([_row("N1", {1: "N", 2: "N"}), _row("N2", {1: "O", 3: "D"})], etag=first))
    assert res["saved"] is True and res["unchanged"] is False and res["etag"] != first
    assert res["etag"] == _get_etag(db)              # 저장 응답 지문 = 다음 조회 지문
    res2 = _save(db, _body([_row("N1", {1: "N", 2: "D"})], etag=res["etag"]))  # 이어지는 자동 저장
    assert res2["etag"] == _get_etag(db)
    assert ("N1", 2, "D", IDS["D"]) in _cells(db)


def test_stale_base_etag_is_rejected_and_newer_cells_stay(ward):
    db = ward["db"]
    old = _get_etag(db)
    _save(db, _body([_row("N1", {1: "N", 2: "N"})], etag=old))
    after_first = _cells(db)
    with pytest.raises(HTTPException) as ei:
        _save(db, _body([_row("N1", {1: "O"})], etag=old))     # 늦게 도착한 옛 요청
    assert ei.value.status_code == 409
    assert ei.value.detail["code"] == "ROSTER_STALE"
    assert ei.value.detail["current_etag"] == _get_etag(db)
    assert _cells(db) == after_first
    assert ward["rollbacks"]


def test_cell_edit_without_schedule_row_makes_etag_stale(ward):
    """AIDE 칸 수정(`schedule_tools`)처럼 근무표 행(updated_at)을 안 건드리는 변경도 지문이 잡는다."""
    db = ward["db"]
    old = _get_etag(db)
    _entry(db, "N3", 5, "D")
    db.flush()
    with pytest.raises(HTTPException) as ei:
        _save(db, _body(_seed_rows(), etag=old))
    assert ei.value.status_code == 409
    assert ("N3", 5, "D", IDS["D"]) in _cells(db)


def test_memo_saved_while_waiting_for_lock_is_stale(ward, monkeypatch):
    """잠금 전에 읽은 메모로 지문을 만들면, 잠금을 기다리는 사이 커밋된 메모 저장을 덮는다 — 잠근 뒤 다시 읽어야 잡힌다."""
    db = ward["db"]
    old = _get_etag(db)
    load = roster_router._load_schedule_for_caller

    def _load_then_other_save(*args, **kwargs):
        schedule = load(*args, **kwargs)              # 이 저장이 근무표를 읽은 뒤
        table = Schedule.__table__                    # 다른 탭의 메모 저장이 먼저 커밋된다(읽은 객체는 옛 메모 그대로)
        db.connection().execute(table.update().where(table.c.schedule_id == SID).values(memo="다른 탭 메모"))
        return schedule

    monkeypatch.setattr(roster_router, "_load_schedule_for_caller", _load_then_other_save)
    with pytest.raises(HTTPException) as ei:
        _save(db, _body(_seed_rows(), etag=old))
    assert ei.value.status_code == 409


def test_same_content_writes_nothing(ward):
    db = ward["db"]
    etag = _get_etag(db)
    before = _cells(db)
    res = _save(db, _body(_seed_rows(), etag=etag))
    assert res["unchanged"] is True and res["etag"] == etag
    assert ward["rollbacks"]                          # 잠금용 updated_at 갱신까지 되돌린다
    assert ward["logged"] == []
    assert _cells(db) == before


def test_blank_cells_past_month_end_pass_shape_check(ward):
    db = ward["db"]
    rows = _seed_rows()
    rows[0]["schedule"] = rows[0]["schedule"] + [None]          # 화면이 31칸으로 채워 보내도
    res = _save(db, _body(rows, etag=_get_etag(db)))
    assert res["unchanged"] is True


def test_nurse_missing_from_body_keeps_cells(ward):
    """화면이 간호사 행을 빼고 보내도 그 칸을 지우지 않는다 — 지우려면 `remove_nurse_ids` 로 밝힌다."""
    db = ward["db"]
    res = _save(db, _body([_row("N1", {1: "O"})], etag=_get_etag(db)))
    assert _cells(db) == [("N1", 1, "O", IDS["O"]), ("N2", 1, "O", IDS["O"]), ("N2", 3, "D", IDS["D"])]
    assert ward["logged"][-1] == {("N1", date(2026, 11, 1)), ("N1", date(2026, 11, 2))}  # N2 칸은 이력에 없다
    assert res["etag"] == _get_etag(db)
    _save(db, _body([_row("N1", {1: "O"})], etag=res["etag"], remove_nurse_ids=["N2"]))
    assert _cells(db) == [("N1", 1, "O", IDS["O"])]


def test_memo_only_autosave_sends_empty_roster(ward):
    """자동 저장은 바뀐 간호사 행만 보낸다 — 메모만 바뀐 저장은 roster 가 빈 목록이고 칸은 그대로다."""
    db = ward["db"]
    before = _cells(db)
    res = _save(db, _body([], etag=_get_etag(db), memo="메모1"))
    assert res["unchanged"] is False and _memo(db) == "메모1"
    assert _cells(db) == before and ward["logged"] == [set()]
    assert res["etag"] == _get_etag(db)


@pytest.mark.parametrize("bad", [None, ""])
def test_empty_base_etag_is_rejected(ward, bad):
    """base_etag 를 비워 보내면 400 — '안 보냄'으로 받으면 검사 없이 전량 교체돼 다른 간호사 칸을 지운다."""
    db = ward["db"]
    before = _cells(db)
    with pytest.raises(HTTPException) as ei:
        _save(db, {**_body([_row("N1", {1: "O"})]), "base_etag": bad})
    assert ei.value.status_code == 400
    assert _cells(db) == before


def test_request_without_base_etag_still_needs_rows(ward):
    with pytest.raises(HTTPException) as ei:
        _save(ward["db"], _body([]))
    assert ei.value.status_code == 400


def test_request_without_base_etag_replaces_all_and_keeps_memo(ward):
    """기존 요청(지문 없음)은 예전처럼 전량 교체 — 단 memo 키가 없으면 메모를 지우지 않는다."""
    db = ward["db"]
    res = _save(db, {"year": 2026, "month": 11, "schedule_id": SID, "roster": [_row("N1", {1: "O"})]})
    assert _cells(db) == [("N1", 1, "O", IDS["O"])]
    assert _memo(db) == "메모0"
    assert res["unchanged"] is False and res["etag"] == _get_etag(db)


@pytest.mark.parametrize("change, message", [
    ({"month": 12}, "year·month"),
    ({"roster": [{"nurse_id": "N1", "schedule": ["-"] * DAYS + ["D"]}]}, "31일"),
    ({"roster": [_row("N1", {1: "D"}), _row("N1", {2: "D"})]}, "두 번"),
])
def test_autosave_shape_errors_are_400(ward, change, message):
    db = ward["db"]
    before = _cells(db)
    with pytest.raises(HTTPException) as ei:
        _save(db, {**_body(_seed_rows(), etag=_get_etag(db)), **change})
    assert ei.value.status_code == 400 and message in ei.value.detail
    assert _cells(db) == before


def test_aide_cell_edit_rereads_after_schedule_lock(ward, monkeypatch):
    """AIDE 칸 수정은 근무표 행을 잠근 뒤 칸을 다시 읽는다 — 기다리는 사이 저장이 칸을 갈아 끼웠으면 고치지 않는다."""
    db = ward["db"]
    entry_id = next(e.entry_id for e in db.query(ScheduleEntry).filter_by(nurse_id="N1")
                    if e.work_date.day == 1)
    real_lock = schedule_tools._lock_schedule_row

    def _lock_after_save(db_, schedule_id):
        busy = real_lock(db_, schedule_id)
        if not ward.get("saved_while_waiting"):
            ward["saved_while_waiting"] = True     # 잠금을 기다리는 사이 자동 저장이 먼저 끝난다
            _save(db_, _body([_row("N1", {1: "O", 2: "N"})], etag=_get_etag(db_)))
        return busy

    monkeypatch.setattr(schedule_tools, "_lock_schedule_row", _lock_after_save)
    result = schedule_tools.update_schedule_entry(db, entry_id, "N", "W1")
    assert "error" in result
    assert ("N1", 1, "O", IDS["O"]) in _cells(db)


def test_cell_edit_lock_sql_has_updlock_and_wait_limit_on_mssql():
    """칸 수정 잠금이 MSSQL 에서 실제로 UPDLOCK 힌트로 나가고(`with_for_update` 는 pymssql 에서 빈 SQL),
    대기 상한(SET LOCK_TIMEOUT)을 걸었다가 되돌린다."""
    from sqlalchemy.dialects import mssql
    from sqlalchemy.orm import Query

    sent: list = []

    class _Query(Query):
        def first(self):
            sent.append(str(self.statement.compile(dialect=mssql.dialect())))

    class _MssqlDb:
        def get_bind(self):
            return type("Bind", (), {"dialect": mssql.dialect()})()

        def execute(self, stmt, *args, **kwargs):
            sent.append(str(stmt))

        def query(self, *entities):
            return _Query(entities)

    assert schedule_tools._lock_schedule_row(_MssqlDb(), SID) is None
    assert sent[0] == "SET LOCK_TIMEOUT 10000" and sent[-1] == "SET LOCK_TIMEOUT -1"
    assert "schedules WITH (UPDLOCK, ROWLOCK)" in sent[1]


def test_unverifiable_etag_is_not_saved(ward, monkeypatch):
    db = ward["db"]
    etag = _get_etag(db)

    def _snapshot_down(*_a, **_k):
        raise RuntimeError("snapshot down")

    monkeypatch.setattr(history, "snapshot_rows", _snapshot_down)
    with pytest.raises(HTTPException) as ei:
        _save(db, _body([_row("N1", {1: "O"})], etag=etag))
    assert ei.value.status_code == 503
    assert ("N1", 2, "N", IDS["N"]) in _cells(db)
