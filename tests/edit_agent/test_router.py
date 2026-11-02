"""`/api/schedule-edit` 경로 — 스위치·권한·대화 주인·같은 대화 겹침·기록 조회(가짜 LLM · SQLite)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from db.client2 import get_db
from db.models import Group, Office, Schedule
from routers.auth import require_current_user
from schedule_edit_agent import agent as agent_mod
from schedule_edit_agent import router as router_mod
from schedule_edit_agent.agent import EditAgent
from schedule_edit_agent.store import ConversationStore
from schemas.auth_schema import User as UserSchema
from services.schedule_edit import journal
from services.schedule_edit.rules import build_rules
from services.schedule_edit.session import EditSession
from tests.edit_agent.test_agent import A_OFF, FakeLLM, call
from tests.schedule_edit.test_solver import BEFORE_MONTH, _two_nurse_ctx


def _user(account="acc_hn", group="A", admin=False):
    return UserSchema(
        nurse_id="HN", account_id=account, office_id="o1", group_id=group, is_head_nurse=True,
        is_master_admin=admin, name="수간", EmpSeqNo="", EmpAuthGbn="", mb_part="", office_name="병원",
        mb_part_name="", official_title_name=None, is_nurse_registered=True, hn_auth=None,
        original_group_id=group, gw_useYN="Y", qpis_useYN="Y")


@pytest.fixture
def api(db, monkeypatch):
    # 공용 시험 DB 에는 기록 표가 없다 — 이 시험 안에서만 만든다(트랜잭션째 되돌린다)
    db.execute(text(
        "CREATE TABLE IF NOT EXISTS schedule_edit_log (log_id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "conversation_id VARCHAR(64), turn_no INTEGER, schedule_id VARCHAR(50) NOT NULL, new_schedule_id VARCHAR(50), "
        "event VARCHAR(10) NOT NULL, payload JSON NOT NULL, elapsed_ms INTEGER, created_by VARCHAR(50), "
        "created_at DATETIME NOT NULL)"))
    monkeypatch.setattr(journal, "_ready", False)
    monkeypatch.setattr(journal, "_checked_at", None)
    db.add(Office(office_id="o1", office_name="병원"))
    db.add(Group(group_id="A", group_name="A병동", office_id="o1"))
    db.add(Group(group_id="B", group_name="B병동", office_id="o1"))
    db.add(Schedule(schedule_id="s1", office_id="o1", group_id="A", year=2026, month=11, version=1,
                    status="draft", dropped=False, name="11월"))
    db.add(Schedule(schedule_id="sB", office_id="o1", group_id="B", year=2026, month=11, version=1,
                    status="draft", dropped=False, name="B 11월"))
    db.flush()

    def fake_open(db_, schedule_id, **_kw):
        ctx = _two_nurse_ctx()
        return EditSession(schedule_id=schedule_id, ctx=ctx, rules=build_rules(ctx), screen=None, stamp="x")

    monkeypatch.setattr(agent_mod, "open_session", fake_open)
    monkeypatch.setattr(agent_mod, "is_stale", lambda db_, s: False)
    monkeypatch.setattr(router_mod, "_store", ConversationStore())
    monkeypatch.setattr(router_mod, "_today", lambda: BEFORE_MONTH)
    monkeypatch.setattr(router_mod, "caller_is_manager", lambda db_, u: u.account_id != "acc_nurse")
    monkeypatch.setenv("SCHEDULE_EDIT_AGENT_ENABLED", "1")
    llm = FakeLLM(*[call("propose_edit", changes=[A_OFF]) for _ in range(5)])
    monkeypatch.setattr(router_mod, "_agent", EditAgent(lambda: llm))

    app = FastAPI()
    app.include_router(router_mod.router)
    state = {"user": _user()}
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[require_current_user] = lambda: state["user"]
    return TestClient(app), state


def test_switch_off_hides_everything(api, monkeypatch):
    client, _ = api
    monkeypatch.setenv("SCHEDULE_EDIT_AGENT_ENABLED", "0")
    assert client.post("/api/schedule-edit/chat", json={"schedule_id": "s1", "message": "A 1일 오프"}).status_code == 404
    assert client.get("/api/schedule-edit/conversations").status_code == 404


def test_only_managers_of_that_ward(api):
    client, state = api
    state["user"] = _user(account="acc_nurse")
    assert client.post("/api/schedule-edit/chat", json={"schedule_id": "s1", "message": "x"}).status_code == 403
    state["user"] = _user()
    assert client.post("/api/schedule-edit/chat", json={"schedule_id": "sB", "message": "x"}).status_code == 403


def test_chat_then_action_and_conversation_log(api):
    client, state = api
    first = client.post("/api/schedule-edit/chat", json={"schedule_id": "s1", "message": "A 1일 오프"})
    assert first.status_code == 200, first.text
    body = first.json()
    cid = body["conversation_id"]
    assert body["preview"]["status"] == "ok" and body["data"]["turn_no"] == 1 and body["data"]["turns_left"] > 0
    kept = client.post("/api/schedule-edit/action", json={"conversation_id": cid, "action": "keep_candidate", "rank": 1})
    assert kept.status_code == 200 and kept.json()["data"]["turn_no"] == 2
    bad = client.post("/api/schedule-edit/action", json={"conversation_id": cid, "action": "keep_candidate", "rank": 9})
    assert bad.status_code == 200 and "다시 찾아 주세요" in bad.json()["answer"]

    resp = client.get(f"/api/schedule-edit/conversation/{cid}")
    assert resp.status_code == 200, resp.text
    log = resp.json()
    assert [t["event"] for t in log["turns"]] == ["message", "propose", "keep", "error"]   # 없는 후보 번호도 남는다
    assert log["turns"][0]["payload"]["text"] == "A 1일 오프" and log["conversation"]["in_memory"]

    state["user"] = _user(account="acc_other")
    assert client.post("/api/schedule-edit/chat", json={"schedule_id": "s1", "message": "x",
                                                        "conversation_id": cid}).status_code == 403
    assert client.get(f"/api/schedule-edit/conversation/{cid}").status_code == 403

    state["user"] = _user()
    assert client.post(f"/api/schedule-edit/conversation/{cid}/close").status_code == 204
    router_mod._store.close(cid)          # 메모리에서 내려도 기록의 close 때문에 다시 서지 않는다
    again = client.post("/api/schedule-edit/chat", json={"schedule_id": "s1", "message": "x", "conversation_id": cid})
    assert again.status_code == 410 and again.json()["detail"]["code"] == "closed"


def test_same_conversation_cannot_run_twice_at_once(api):
    client, _ = api
    cid = client.post("/api/schedule-edit/chat", json={"schedule_id": "s1", "message": "A 1일 오프"}).json()["conversation_id"]
    conv = router_mod._store.active()[0]
    assert conv.id == cid
    conv.lock.acquire()
    try:
        busy = client.post("/api/schedule-edit/action", json={"conversation_id": cid, "action": "keep_candidate", "rank": 1})
        assert busy.status_code == 409
    finally:
        conv.lock.release()


def test_unknown_conversation_is_404(api):
    client, _ = api
    r = client.post("/api/schedule-edit/chat", json={"schedule_id": "s1", "message": "x", "conversation_id": "nope"})
    assert r.status_code == 404 and r.json()["detail"]["code"] == "not_found"


def test_ward_less_schedule_and_lost_permission_are_refused(api, db):
    client, state = api
    db.add(Schedule(schedule_id="s0", office_id="o1", group_id=None, year=2026, month=11, version=1,
                    status="draft", dropped=False, name="병동 없음"))
    db.flush()
    assert client.post("/api/schedule-edit/chat", json={"schedule_id": "s0", "message": "x"}).status_code == 403
    cid = client.post("/api/schedule-edit/chat", json={"schedule_id": "s1", "message": "A 1일 오프"}).json()["conversation_id"]
    state["user"] = _user(account="acc_nurse")     # 같은 사람이 관리 권한을 잃은 경우와 같다
    assert client.get(f"/api/schedule-edit/conversation/{cid}").status_code == 403


def test_close_waits_for_the_running_turn(api):
    client, _ = api
    cid = client.post("/api/schedule-edit/chat", json={"schedule_id": "s1", "message": "A 1일 오프"}).json()["conversation_id"]
    conv = router_mod._store.active()[0]
    conv.lock.acquire()
    try:
        assert client.post(f"/api/schedule-edit/conversation/{cid}/close").status_code == 409
    finally:
        conv.lock.release()
    assert client.post(f"/api/schedule-edit/conversation/{cid}/close").status_code == 204


def test_new_conversation_refused_for_no_slot_is_not_kept(api, monkeypatch):
    client, _ = api
    monkeypatch.setattr(router_mod, "SLOT_WAIT_SECONDS", 0.01)
    taken = [router_mod._slots.acquire(timeout=1) for _ in range(int(router_mod._slots._initial_value))]
    try:
        busy = client.post("/api/schedule-edit/chat", json={"schedule_id": "s1", "message": "A 1일 오프"})
        assert busy.status_code == 429 and router_mod._store.active() == []
    finally:
        for ok in taken:
            if ok:
                router_mod._slots.release()
