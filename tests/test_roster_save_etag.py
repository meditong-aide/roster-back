"""근무표 저장 내용 지문(etag) — 자동 저장(디바운싱)이 옛 요청으로 최신 근무표를 덮지 않게(11-04 결정 D8).

검증 대상(`app/routers/roster.py` `autosave_roster`·`save_roster` · `app/services/schedule_etag.py`):
- 조회 지문 = 저장 직전 지문 · 저장 응답 지문 = 저장 뒤 조회 지문(연속 자동 저장이 이어진다)
- `base_etag` 가 지금과 다르면 409 `ROSTER_STALE`(칸은 그대로) — 근무표 행을 안 건드리는 칸 수정·
  잠금 전에 다른 저장이 바꾼 메모도 잡는다
- 바뀐 것이 없으면 쓰지 않는다(`unchanged`) · 본문에 없는 간호사 칸은 보존(`remove_nurse_ids` 로만 지움)
- 모양 검사 400(연월 불일치·월 일수를 넘는 날의 근무·같은 간호사 행 중복) · 지문 확인 실패 503
- 경로 분리(11-04 점검): 지문 저장은 `/roster/autosave` 만(base_etag 필수) · `/roster/save` 는 전량 교체이고
  base_etag 를 보내면 400 · memo 키가 없으면 메모를 지우지 않는다 · 잠금 뒤 삭제된 근무표는 404(숨김·물리 삭제 둘 다)
- AIDE 칸 수정(`schedule_tools.update_schedule_entry`)은 저장과 같은 근무표 행 잠금에 줄을 선다(11-04 Codex)
- 상세 조회 근무코드 색·counts 는 그 병동 코드만(11-04 점검)
"""
from __future__ import annotations

import logging
from datetime import date, datetime

import pytest
import routers.roster as roster_router
import services.schedule_history_service as history
from agents_v2.tools import schedule_tools
from db.models import Group, Nurse, Office, Schedule, ScheduleEntry, Shift
from fastapi import HTTPException
from routers.roster import autosave_roster, get_roster_by_schedule_id, save_roster
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
    """지문을 실은 저장은 자동 저장 경로, 아니면 전량 교체 경로 — 화면이 보내는 그대로(11-04 경로 분리)."""
    handler = autosave_roster if "base_etag" in body else save_roster
    return handler(body, None, _user(), db)


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


def test_stale_base_etag_is_rejected_and_newer_cells_stay(ward, caplog):
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
    assert f"ROSTER_STALE schedule={SID} group=W1" in caplog.text    # 배포 뒤 충돌 횟수를 센다


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


def test_same_content_writes_nothing(ward, caplog):
    db = ward["db"]
    etag = _get_etag(db)
    before = _cells(db)
    caplog.set_level(logging.INFO, logger="routers.roster")
    res = _save(db, _body(_seed_rows(), etag=etag))
    assert res["unchanged"] is True and res["etag"] == etag
    assert ward["rollbacks"]                          # 잠금용 updated_at 갱신까지 되돌린다
    assert ward["logged"] == []
    assert _cells(db) == before
    # 쓰지 않은 자동 저장 비율을 셀 수 있게 남긴다(응답 본문은 호출 기록에 남지 않는다)
    assert f"[roster.autosave] unchanged schedule={SID} group=W1" in caplog.text


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


# ── 경로 분리·잠금 뒤 삭제·상세 조회 근무코드(11-04 점검) ─────────────────────


def test_save_path_rejects_base_etag_and_autosave_requires_it(ward):
    """지문 저장은 `/roster/autosave` 만 받는다 — `/roster/save` 로 새면 전량 교체돼 본문에 없는 간호사 칸이 지워진다.
    옛 서버에는 autosave 경로가 없어(405) 프론트가 먼저 나가도 아무것도 쓰지 않는다."""
    db = ward["db"]
    before = _cells(db)
    with pytest.raises(HTTPException) as ei:
        save_roster(_body([_row("N1", {1: "O"})], etag=_get_etag(db)), None, _user(), db)
    assert ei.value.status_code == 400 and "/roster/autosave" in ei.value.detail
    with pytest.raises(HTTPException) as ei:
        autosave_roster(_body([_row("N1", {1: "O"})]), None, _user(), db)
    assert ei.value.status_code == 400 and "base_etag" in ei.value.detail
    assert _cells(db) == before


def test_autosave_route_is_reachable_over_http(ward):
    """POST /roster/autosave 가 이 핸들러에 닿는다 — `DELETE /roster/{schedule_id}` 같은 경로에 가려 405 가 되지 않게."""
    from db.client2 import get_db
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers.auth import get_current_user_from_cookie

    db = ward["db"]
    app = FastAPI()
    app.include_router(roster_router.router)
    app.dependency_overrides[get_current_user_from_cookie] = _user
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app)
    missing = client.post("/roster/autosave", json=_body(_seed_rows()))
    assert missing.status_code == 400 and "base_etag" in missing.json()["detail"]
    same = client.post("/roster/autosave", json=_body(_seed_rows(), etag=_get_etag(db)))
    assert same.status_code == 200 and same.json()["unchanged"] is True
    leaked = client.post("/roster/save", json=_body(_seed_rows(), etag=_get_etag(db)))
    assert leaked.status_code == 400


@pytest.mark.parametrize("with_etag", [True, False])
def test_schedule_dropped_while_waiting_for_lock_is_not_written(ward, monkeypatch, with_etag):
    """잠금을 기다리는 사이 근무표가 삭제(숨김)되면 쓰지 않는다(404) — 잠금 전에 읽은 값으로는 그 삭제를 못 본다."""
    db = ward["db"]
    before = _cells(db)
    etag = _get_etag(db) if with_etag else None
    load = roster_router._load_schedule_for_caller

    def _load_then_drop(*args, **kwargs):
        schedule = load(*args, **kwargs)              # 이 저장이 근무표를 읽은 뒤
        table = Schedule.__table__                    # 다른 창의 삭제가 먼저 커밋된다
        db.connection().execute(table.update().where(table.c.schedule_id == SID).values(dropped=True))
        return schedule

    monkeypatch.setattr(roster_router, "_load_schedule_for_caller", _load_then_drop)
    with pytest.raises(HTTPException) as ei:
        _save(db, _body([_row("N1", {1: "O"})], etag=etag))
    assert ei.value.status_code == 404
    assert _cells(db) == before


@pytest.mark.parametrize("with_etag", [True, False])
def test_schedule_deleted_outright_while_waiting_for_lock_is_404(ward, monkeypatch, with_etag):
    """생성 실패 draft 처럼 행째 지워지면(물리 삭제) 잠금 UPDATE 가 0행이다 — 500(StaleDataError)이 아니라 404."""
    db = ward["db"]
    etag = _get_etag(db) if with_etag else None
    load = roster_router._load_schedule_for_caller
    entries, schedules = ScheduleEntry.__table__, Schedule.__table__

    def _load_then_delete(*args, **kwargs):
        schedule = load(*args, **kwargs)              # 이 저장이 근무표를 읽은 뒤
        conn = db.connection()                        # 생성 실패 정리가 칸과 행을 지우고 먼저 커밋한다
        conn.execute(entries.delete().where(entries.c.schedule_id == SID))
        conn.execute(schedules.delete().where(schedules.c.schedule_id == SID))
        return schedule

    monkeypatch.setattr(roster_router, "_load_schedule_for_caller", _load_then_delete)
    with pytest.raises(HTTPException) as ei:
        _save(db, _body([_row("N1", {1: "O"})], etag=etag))
    assert ei.value.status_code == 404
    assert ward["rollbacks"]                          # 깨진 flush 를 되돌린 뒤 404


def test_detail_colors_and_counts_use_this_wards_codes_only(ward):
    """상세 조회의 근무코드 색·counts 는 이 병동 코드만 — 전 병원 코드를 읽으면 조회마다 공용 DB 부하가 들고,
    화면이 이 행을 되돌려 보내는 저장 본문도 커진다. 다른 병원의 같은 코드 색도 섞이지 않는다."""
    db = ward["db"]
    db.add(Group(group_id="W2", group_name="2병동", office_id="o1"))
    db.add(Shift(shift_id="X", id=201, office_id="o1", group_id="W2", name="X", color="#ffffff", sequence=1))
    db.add(Shift(shift_id="D", id=202, office_id="o1", group_id="W2", name="D", color="#ff0000", sequence=1))
    db.flush()
    res = get_roster_by_schedule_id(SID, None, _user(), db)
    assert res["shift_colors"] == {"D": "#000000", "N": "#000000", "O": "#000000"}
    assert all(set(n["counts"]) == {"D", "N", "O"} for n in res["nurses"])
    n1 = next(n for n in res["nurses"] if n["id"] == "N1")
    assert (n1["counts"]["D"], n1["counts"]["N"]) == (1, 1)


def test_detail_colors_use_first_row_by_sequence_then_id(ward):
    """같은 병동에 같은 코드 행이 여럿이면 (sequence, id) 첫 행의 색 — 저장의 대표 행 규칙과 같다."""
    db = ward["db"]
    db.add(Shift(shift_id="D", id=160, office_id="o1", group_id="W1", name="D", color="#654321", sequence=1))
    db.flush()
    assert get_roster_by_schedule_id(SID, None, _user(), db)["shift_colors"]["D"] == "#000000"  # 같은 sequence → 작은 id
    db.add(Shift(shift_id="D", id=170, office_id="o1", group_id="W1", name="D", color="#123456", sequence=0))
    db.flush()
    assert get_roster_by_schedule_id(SID, None, _user(), db)["shift_colors"]["D"] == "#123456"  # 작은 sequence 가 먼저
