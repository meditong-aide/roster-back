"""`/api/agents/schedule-edit` 경로 — 스위치·권한·대화 주인·겹침·버전 이동·적용·기록 조회(가짜 LLM · SQLite)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from agent_kit import base as kit_base
from agent_kit.router import router as agents_router
from agent_kit.runtime import RENEWED
from db.client2 import get_db
from db.models import Group, Office, Schedule
from routers.auth import require_current_user
from schedule_edit_agent import agent as agent_mod
from schedule_edit_agent import skills as skills_mod
from schedule_edit_agent import spec as spec_mod
from schedule_edit_agent.spec import ScheduleEditAgent
from schemas.auth_schema import User as UserSchema
from services.schedule_edit import journal
from services.schedule_edit.rules import build_rules
from services.schedule_edit.session import EditSession
from tests.edit_agent.test_agent import A_OFF, FakeLLM, call
from tests.schedule_edit.test_solver import BEFORE_MONTH, _two_nurse_ctx

URL = "/api/agents/schedule-edit"


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
    db.add(Schedule(schedule_id="s1b", office_id="o1", group_id="A", year=2026, month=11, version=2,
                    status="draft", dropped=False, name="11월 다른 버전"))
    db.add(Schedule(schedule_id="sB", office_id="o1", group_id="B", year=2026, month=11, version=1,
                    status="draft", dropped=False, name="B 11월"))
    db.flush()

    def fake_open(db_, schedule_id, **_kw):
        ctx = _two_nurse_ctx()
        return EditSession(schedule_id=schedule_id, ctx=ctx, rules=build_rules(ctx), screen=None, stamp="x")

    monkeypatch.setattr(agent_mod, "open_session", fake_open)
    monkeypatch.setattr(agent_mod, "is_stale", lambda db_, s: False)
    monkeypatch.setattr(spec_mod, "caller_is_manager", lambda db_, u: u.account_id != "acc_nurse")
    monkeypatch.setenv("SCHEDULE_EDIT_AGENT_ENABLED", "1")
    llm = FakeLLM(*[call("propose_edit", changes=[A_OFF]) for _ in range(8)])
    agent = ScheduleEditAgent(lambda: llm)
    monkeypatch.setattr(agent, "today", lambda: BEFORE_MONTH)
    monkeypatch.setitem(kit_base.AGENTS, "schedule-edit", agent)

    app = FastAPI()
    app.include_router(agents_router)
    state = {"user": _user()}
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[require_current_user] = lambda: state["user"]
    return SimpleNamespace(client=TestClient(app), state=state, agent=agent)


def say(client, said, *, cid=None, schedule="s1"):
    return client.post(f"{URL}/turn", json={"conversation_id": cid, "context": {"schedule_id": schedule},
                                             "input": {"kind": "text", "text": said}})


def press(client, cid, action, *, schedule="s1", **params):
    return client.post(f"{URL}/turn", json={"conversation_id": cid, "context": {"schedule_id": schedule},
                                             "input": {"kind": "action", "action": action, "params": params}})


def block(body, kind):
    return next(b for b in body["blocks"] if b["type"] == kind)


def code(resp):
    return resp.json()["detail"]["code"]


def test_switch_off_hides_everything(api, monkeypatch):
    monkeypatch.setenv("SCHEDULE_EDIT_AGENT_ENABLED", "0")
    assert say(api.client, "A 1일 오프").status_code == 404
    assert api.client.get(f"{URL}/conversations").status_code == 404
    assert api.client.get(f"{URL}/availability", params={"schedule_id": "s1"}).status_code == 404
    assert api.client.get("/api/agents/nope/conversations").status_code == 404        # 없는 에이전트


def test_only_managers_of_that_ward(api):
    api.state["user"] = _user(account="acc_nurse")
    refused = say(api.client, "x")
    assert refused.status_code == 403 and code(refused) == "no_access" and "수간호사" in refused.json()["detail"]["message"]
    api.state["user"] = _user()
    other_ward = say(api.client, "x", schedule="sB")
    assert other_ward.status_code == 403 and code(other_ward) == "no_access"


def test_text_then_buttons_and_conversation_log(api):
    client = api.client
    first = say(client, "A 1일 오프")
    assert first.status_code == 200, first.text
    body = first.json()
    cid = body["conversation_id"]
    assert body["agent"] == "schedule-edit" and body["turn_no"] == 1 and body["status"]["turns_left"] > 0
    assert body["context"] == {"schedule_id": "s1"} and body["effects"] == []
    assert [b["type"] for b in body["blocks"]] == ["trail", "text", "cards"]
    assert block(body, "trail") == {"type": "trail", "request": "A 1일 오프", "steps": []}
    cards = block(body, "cards")
    top = cards["items"][0]
    assert cards["selected"] == "1" and top["title"].startswith("1안 · ")
    assert top["actions"][0]["input"] == {"kind": "action", "action": "apply_edit", "params": {"rank": 1}}
    assert [a["input"]["action"] for a in top["actions"]] == ["apply_edit", "keep_candidate", "reject_candidate"]
    assert top["overlay"]["kind"] == "schedule_cells" and top["overlay"]["cells"]

    kept = press(client, cid, "keep_candidate", rank=1).json()
    assert kept["turn_no"] == 2 and block(kept, "trail")["steps"] == ["1안을 기준으로 다듬기"]
    assert block(kept, "cards")["selected"] == "1"
    bad = press(client, cid, "keep_candidate", rank=9)
    assert bad.status_code == 200
    bad = bad.json()
    assert block(bad, "text")["tone"] == "error" and "다시 찾아 주세요" in block(bad, "text")["text"]
    assert block(bad, "trail")["steps"] == ["1안을 기준으로 다듬기"]        # 안 된 버튼은 흔적에 남기지 않는다
    assert block(bad, "cards")["items"]                                      # 보던 후보는 그대로 다시

    log = client.get(f"{URL}/conversations/{cid}")
    assert log.status_code == 200, log.text
    log = log.json()
    assert [t["event"] for t in log["turns"]] == ["message", "propose", "keep", "error"]   # 없는 후보 번호도 남는다
    assert log["turns"][0]["payload"]["text"] == "A 1일 오프" and log["conversation"]["in_memory"]
    assert [c["conversation_id"] for c in client.get(f"{URL}/conversations").json()["conversations"]] == [cid]

    api.state["user"] = _user(account="acc_other")
    stolen = press(client, cid, "keep_candidate", rank=1)
    assert stolen.status_code == 403 and code(stolen) == "forbidden"     # 남의 대화의 버튼은 거절
    fresh = say(client, "A 1일 오프", cid=cid)                           # 말은 이 사람의 새 대화로(남의 대화는 그대로)
    assert fresh.status_code == 200 and fresh.json()["conversation_id"] != cid
    assert client.get(f"{URL}/conversations/{cid}").status_code == 403

    api.state["user"] = _user()
    assert client.post(f"{URL}/conversations/{cid}/close").status_code == 204
    # 닫으면 메모리에서도 내린다 — 기록의 close 로 다시 서지 않는다
    assert cid not in [c.id for c in api.agent.store.active()]
    again = press(client, cid, "keep_candidate", rank=1)
    assert again.status_code == 410 and code(again) == "closed"
    renewed = say(client, "A 1일 오프", cid=cid).json()  # 말은 서버가 새 대화로 이어 받는다
    assert renewed["conversation_id"] != cid and renewed["turn_no"] == 1
    assert renewed["blocks"][0] == {"type": "notice", "tone": "info", "title": None, "text": RENEWED}


def test_same_conversation_cannot_run_twice_at_once(api):
    cid = say(api.client, "A 1일 오프").json()["conversation_id"]
    conv = api.agent.store.active()[0]
    assert conv.id == cid
    conv.lock.acquire()
    try:
        busy = press(api.client, cid, "keep_candidate", rank=1)
        assert busy.status_code == 409 and code(busy) == "busy"
        assert say(api.client, "x", cid=cid).status_code == 409          # 처리 중은 끝난 대화가 아니다
    finally:
        conv.lock.release()


def test_unknown_conversation_refuses_buttons_and_renews_text(api):
    gone = press(api.client, "nope", "keep_candidate", rank=1)
    assert gone.status_code == 404 and code(gone) == "not_found"
    renewed = say(api.client, "A 1일 오프", cid="nope")
    assert renewed.status_code == 200 and renewed.json()["conversation_id"] != "nope"


def test_ward_less_schedule_and_lost_permission_are_refused(api, db):
    db.add(Schedule(schedule_id="s0", office_id="o1", group_id=None, year=2026, month=11, version=1,
                    status="draft", dropped=False, name="병동 없음"))
    db.flush()
    ward_less = say(api.client, "x", schedule="s0")
    assert ward_less.status_code == 403 and code(ward_less) == "no_access"
    missing = say(api.client, "x", schedule="s-none")
    assert missing.status_code == 404 and code(missing) == "no_target"
    cid = say(api.client, "A 1일 오프").json()["conversation_id"]
    api.state["user"] = _user(account="acc_nurse")     # 같은 사람이 관리 권한을 잃은 경우와 같다
    assert api.client.get(f"{URL}/conversations/{cid}").status_code == 403


def test_close_waits_for_the_running_turn(api):
    cid = say(api.client, "A 1일 오프").json()["conversation_id"]
    conv = api.agent.store.active()[0]
    conv.lock.acquire()
    try:
        assert api.client.post(f"{URL}/conversations/{cid}/close").status_code == 409
    finally:
        conv.lock.release()
    assert api.client.post(f"{URL}/conversations/{cid}/close").status_code == 204


def test_new_conversation_refused_for_no_slot_is_not_kept(api, monkeypatch):
    monkeypatch.setattr(api.agent, "slot_wait_seconds", 0.01)
    taken = [api.agent.slots.acquire(timeout=1) for _ in range(int(api.agent.slots._initial_value))]
    try:
        busy = say(api.client, "A 1일 오프")
        assert busy.status_code == 429 and code(busy) == "server_busy" and api.agent.store.active() == []
    finally:
        for ok in taken:
            if ok:
                api.agent.slots.release()


def test_buttons_from_another_version_are_refused_and_text_moves(api):
    cid = say(api.client, "A 1일 오프").json()["conversation_id"]
    # 화면이 다른 버전(s1b)으로 옮긴 뒤 앞 버전의 '적용'이 오면 거절 — 엉뚱한 근무표를 바꾸지 않게
    stale = press(api.client, cid, "apply_edit", schedule="s1b", rank=1)
    assert stale.status_code == 409 and code(stale) == "context_changed"
    moved = say(api.client, "A 1일 오프", cid=cid, schedule="s1b").json()
    assert moved["conversation_id"] == cid and moved["context"] == {"schedule_id": "s1b"} and moved["turn_no"] == 2


def test_apply_switches_version_with_the_request_line(api, monkeypatch):
    seen = {}

    def fake_apply(db_, session, cand, user, **kw):
        seen.update(kw)
        return {"new_schedule_id": "s2", "new_version": 3, "new_name": "11월 수정본", "status": "draft"}

    monkeypatch.setattr(skills_mod, "apply_to_new_version", fake_apply)
    cid = say(api.client, "A 1일 오프").json()["conversation_id"]
    body = press(api.client, cid, "apply_edit", rank=1).json()
    assert seen["request_text"] == "A 1일 오프"                         # 화면이 보내지 않아도 대화의 요청 줄
    assert body["effects"] == [{"type": "switch_version", "schedule_id": "s2", "version": 3, "status": "draft",
                                "name": "11월 수정본"}]
    assert body["context"] == {"schedule_id": "s2"}
    assert [b["type"] for b in body["blocks"]] == ["trail", "notice"]
    assert block(body, "notice")["tone"] == "success" and "11월 수정본(VER3)" in block(body, "notice")["text"]
    assert block(body, "trail")["steps"] == ["1안 적용"]


def test_a_button_checks_the_schedule_once(api, monkeypatch):
    checks = []
    monkeypatch.setattr(agent_mod, "is_stale", lambda db_, s: checks.append(s.schedule_id) or False)
    cid = say(api.client, "A 1일 오프").json()["conversation_id"]
    checks.clear()
    assert press(api.client, cid, "keep_candidate", rank=1).status_code == 200
    assert checks == ["s1"]                                  # 흔적 줄과 버튼이 같은 판정 재료를 쓴다(검토 L3)


def test_bad_inputs_are_refused_before_the_turn(api):
    cid = say(api.client, "A 1일 오프").json()["conversation_id"]
    unknown = press(api.client, cid, "drop_table")
    assert unknown.status_code == 400 and code(unknown) == "bad_action"
    llm_only = press(api.client, cid, "propose_edit")                   # 말로만 부르는 스킬은 버튼이 아니다
    assert llm_only.status_code == 400 and code(llm_only) == "bad_action"
    blank = say(api.client, "   ", cid=cid)
    assert blank.status_code == 400 and code(blank) == "empty"
    odd = api.client.post(f"{URL}/turn", json={"conversation_id": cid, "context": {"schedule_id": "s1"},
                                               "input": {"kind": "file"}})
    assert odd.status_code == 400 and code(odd) == "unsupported_input"
    assert say(api.client, "가" * 1001, cid=cid).status_code == 422
    assert press(api.client, cid, "keep_candidate", rank=1).json()["turn_no"] == 2    # 거절한 입력은 턴이 아니다


def test_availability_gives_the_first_screen(api, monkeypatch):
    body = api.client.get(f"{URL}/availability", params={"schedule_id": "s1"}).json()
    assert body["enabled"] and body["can_use"] and body["reason"] is None
    intro = body["intro"]
    assert intro["title"] == "근무표 수정 도우미" and intro["subtitle"].startswith("VER1 기준")
    assert [b["type"] for b in intro["blocks"]] == ["notice", "examples", "notice"]
    assert intro["composer"]["max_length"] == 1000
    api.state["user"] = _user(account="acc_nurse")
    body = api.client.get(f"{URL}/availability", params={"schedule_id": "s1"}).json()
    assert body["can_use"] is False and "수간호사" in body["reason"] and body["intro"] is None
    monkeypatch.setenv("SCHEDULE_EDIT_AGENT_ENABLED", "0")
    assert api.client.get(f"{URL}/availability", params={"schedule_id": "s1"}).status_code == 404
