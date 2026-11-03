"""공통 에이전트 틀 — 근무표와 상관없는 작은 에이전트로 경로·한 턴 순서·대화 보관만 본다(DB·LLM 없음).

새 에이전트가 붙어도 화면 계약(블록·버튼·효과·오류 code)과 서버가 맡은 판단(끝난 대화 이어 받기·다른 대상의
버튼 거절·흔적)이 그대로인지 확인한다.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from agent_kit import base as kit_base
from agent_kit import blocks as B
from agent_kit.base import Agent, TurnResult
from agent_kit.conversation import ConversationError, ConversationStore, Limits
from agent_kit.router import router as agents_router
from agent_kit.runtime import RENEWED, TurnError
from db.client2 import get_db
from routers.auth import require_current_user

URL = "/api/agents/echo"


class Echo(Agent):
    """말은 그대로 돌려주고, 버튼 press 는 흔적 한 줄, move 는 대상을 d2 로 옮기며 효과를 낸다."""

    id = "echo"
    title = "에코"
    limits = Limits(max_turns=3)
    moved_message = "다른 문서로 옮겨 이 버튼은 쓸 수 없습니다."

    def __init__(self):
        self.on = True
        self.closed = []
        super().__init__()

    def enabled(self):
        return self.on

    def authorize(self, db, user, context):
        doc = str(context.get("doc") or "")
        if doc == "secret":
            raise HTTPException(status_code=403, detail="이 문서는 볼 수 없습니다.")
        if not doc:
            raise HTTPException(status_code=404, detail="문서를 찾을 수 없습니다.")
        return {"doc": doc}

    def intro(self, db, user, context):
        return {"title": self.title, "subtitle": context["doc"], "blocks": [B.examples("예시", ["안녕"])]}

    def check_input(self, inp):
        if inp.kind == "action" and inp.action not in ("press", "move"):
            raise TurnError("bad_action", "알 수 없는 동작입니다.", 400)

    def handle(self, call):
        inp = call.input
        if inp.kind == "text":
            # '?' 로 시작하면 묻기(흔적 그대로) · '+' 면 고쳐 말하기(단계로) · 그 밖은 새 요청
            trail = "keep" if inp.text.startswith("?") else "step" if inp.text.startswith("+") else "new"
            return TurnResult(blocks=[B.text(f"{call.conv.context['doc']}: {inp.text}")], answer=inp.text,
                              trail=trail)
        if inp.action == "move":
            return TurnResult(blocks=[B.notice("옮겼습니다", tone="success")], answer="옮김", step="d2 로 옮기기",
                              context={"doc": "d2"}, effects=[{"type": "switch_doc", "doc": "d2"}])
        return TurnResult(blocks=[B.text("눌렀습니다")], answer="눌림", said="[버튼] 누르기", step="누르기")

    def restore(self, db, conversation_id):
        raise ConversationError("not_found", "대화를 찾을 수 없습니다.", 404)

    def transcript(self, db, user, conversation_id):
        return {"turns": []}

    def on_close(self, db, user, conv):
        self.closed.append(conv.id)


class _User:
    def __init__(self, account):
        self.account_id = account


@pytest.fixture
def echo(monkeypatch):
    agent = Echo()
    monkeypatch.setitem(kit_base.AGENTS, agent.id, agent)
    app = FastAPI()
    app.include_router(agents_router)
    state = {"user": _User("u1")}
    app.dependency_overrides[get_db] = lambda: None
    app.dependency_overrides[require_current_user] = lambda: state["user"]
    client = TestClient(app)
    client.agent, client.state = agent, state
    return client


def say(client, said, *, cid=None, doc="d1"):
    return client.post(f"{URL}/turn", json={"conversation_id": cid, "context": {"doc": doc},
                                             "input": {"kind": "text", "text": said}})


def press(client, cid, action="press", *, doc="d1", **params):
    return client.post(f"{URL}/turn", json={"conversation_id": cid, "context": {"doc": doc},
                                             "input": {"kind": "action", "action": action, "params": params}})


def types(body):
    return [b["type"] for b in body["blocks"]]


def code(resp):
    return resp.json()["detail"]["code"]


def test_text_turn_puts_the_trail_on_top(echo):
    body = say(echo, "안녕").json()
    assert body["agent"] == "echo" and body["turn_no"] == 1 and body["context"] == {"doc": "d1"}
    assert body["blocks"] == [{"type": "trail", "request": "안녕", "steps": []},
                              {"type": "text", "text": "d1: 안녕", "tone": "normal"}]
    assert body["status"]["turns_left"] == 2 and body["effects"] == []


def test_buttons_add_steps_and_a_new_request_resets_them(echo):
    cid = say(echo, "안녕").json()["conversation_id"]
    pressed = press(echo, cid).json()
    assert pressed["blocks"][0] == {"type": "trail", "request": "안녕", "steps": ["누르기"]}
    conv = echo.agent.store.active()[0]
    assert conv.history[-1] == {"user": "[버튼] 누르기", "answer": "눌림"}     # 버튼은 에이전트가 정한 말로
    again = say(echo, "다시", cid=cid).json()
    assert again["blocks"][0] == {"type": "trail", "request": "다시", "steps": []}


def test_the_agent_decides_how_text_changes_the_trail(echo):
    cid = say(echo, "안녕").json()["conversation_id"]
    asked = say(echo, "?지금 뭐 하고 있어", cid=cid).json()
    assert asked["blocks"][0] == {"type": "trail", "request": "안녕", "steps": []}
    revised = say(echo, "+조금 더", cid=cid).json()
    assert revised["blocks"][0] == {"type": "trail", "request": "안녕", "steps": ["+조금 더"]}


def test_target_moves_with_effect_and_old_buttons_are_refused(echo):
    cid = say(echo, "안녕").json()["conversation_id"]
    moved = press(echo, cid, "move").json()
    assert moved["context"] == {"doc": "d2"} and moved["effects"] == [{"type": "switch_doc", "doc": "d2"}]
    stale = press(echo, cid, doc="d1")                     # 화면이 아직 d1 이면 — 앞 대상의 버튼
    assert stale.status_code == 409 and code(stale) == "context_changed"
    assert stale.json()["detail"]["message"] == Echo.moved_message
    back = say(echo, "다시 d1", cid=cid, doc="d1").json()   # 말은 화면의 대상으로 옮겨 이어 간다
    assert back["context"] == {"doc": "d1"} and back["conversation_id"] == cid and back["turn_no"] == 3


def test_ended_conversation_renews_for_text_and_refuses_buttons(echo):
    cid = say(echo, "하나").json()["conversation_id"]
    press(echo, cid)
    press(echo, cid)                                       # 한도 3턴을 다 썼다
    refused = press(echo, cid)
    assert refused.status_code == 410 and code(refused) == "too_long"
    renewed = say(echo, "넷", cid=cid).json()
    assert renewed["conversation_id"] != cid and renewed["turn_no"] == 1
    assert types(renewed) == ["notice", "trail", "text"] and renewed["blocks"][0]["text"] == RENEWED

    gone = press(echo, "nope")
    assert gone.status_code == 404 and code(gone) == "not_found"
    assert say(echo, "x", cid="nope").json()["blocks"][0]["text"] == RENEWED


def test_refused_input_does_not_count_as_a_turn(echo):
    cid = say(echo, "안녕").json()["conversation_id"]
    assert code(press(echo, cid, "explode")) == "bad_action"
    assert code(say(echo, "  ", cid=cid)) == "empty"
    assert press(echo, cid).json()["turn_no"] == 2


def test_access_errors_have_codes_and_availability_answers_with_values(echo):
    secret = say(echo, "x", doc="secret")
    assert secret.status_code == 403 and secret.json()["detail"] == {"code": "no_access",
                                                                     "message": "이 문서는 볼 수 없습니다."}
    assert code(say(echo, "x", doc="")) == "no_target"
    assert echo.get(f"{URL}/availability", params={"doc": "secret"}).json() == \
        {"enabled": True, "can_use": False, "reason": "이 문서는 볼 수 없습니다.", "intro": None}
    ok = echo.get(f"{URL}/availability", params={"doc": "d1"}).json()
    assert ok["can_use"] and ok["intro"]["subtitle"] == "d1"
    echo.agent.on = False
    assert echo.get(f"{URL}/availability", params={"doc": "d1"}).status_code == 404
    assert say(echo, "x").status_code == 404


def test_someone_elses_conversation_renews_for_text_only(echo):
    # 같은 브라우저에서 다른 계정으로 다시 로그인하면 앞 사람의 대화 id 가 남아 있다
    cid = say(echo, "안녕").json()["conversation_id"]
    echo.state["user"] = _User("u2")
    refused = press(echo, cid)
    assert refused.status_code == 403 and code(refused) == "forbidden"
    mine = say(echo, "새로", cid=cid).json()
    assert mine["conversation_id"] != cid and mine["blocks"][0]["text"] == RENEWED
    owner = next(c for c in echo.agent.store.active() if c.id == cid)
    assert owner.owner == "u1" and owner.turn_no == 1                       # 앞 사람의 대화는 그대로


def test_close_calls_the_agent_and_forgets_the_conversation(echo):
    cid = say(echo, "안녕").json()["conversation_id"]
    echo.state["user"] = _User("u2")
    assert echo.post(f"{URL}/conversations/{cid}/close").status_code == 403     # 남의 대화
    echo.state["user"] = _User("u1")
    assert echo.post(f"{URL}/conversations/{cid}/close").status_code == 204
    assert echo.agent.closed == [cid] and echo.agent.store.active() == []


# ── 대화 보관 ───────────────────────────────────────────────────────────────


def _store(**limits):
    def restore(db, cid):
        raise ConversationError("not_found", "없음", 404)
    return ConversationStore("t", Limits(**limits), restore)


def test_store_limits_owner_age_turns_and_idle():
    limits = Limits(max_turns=5, max_age_seconds=3600, idle_seconds=60, max_per_user=3)
    store = ConversationStore("t", limits, restore=None)
    conv = store.start("u1", {"doc": "d1"})
    with pytest.raises(ConversationError) as err:
        store.checkout(None, conv.id, "u1")
    assert err.value.code == "busy"                                      # 꺼낸 채(처리 중)면 409
    store.checkin(conv)
    with pytest.raises(ConversationError) as err:
        store.checkout(None, conv.id, "someone_else")
    assert err.value.code == "forbidden" and not conv.lock.locked()
    conv.turn_no = limits.max_turns
    with pytest.raises(ConversationError) as err:
        store.checkout(None, conv.id, "u1")
    assert err.value.code == "too_long" and not conv.lock.locked()     # 거절하면 잠금도 푼다
    assert store.checkout(None, conv.id, "u1", check_limits=False) is conv   # 닫기는 한도와 상관없이
    store.checkin(conv)
    conv.turn_no, conv.created_at = 0, conv.created_at - limits.max_age_seconds - 1
    with pytest.raises(ConversationError) as err:
        store.checkout(None, conv.id, "u1")
    assert err.value.code == "expired" and store.active("u1") == []

    idle = store.start("u1", {"doc": "d1"})
    store.checkin(idle)
    idle.touched_at -= limits.idle_seconds + 1
    assert store.active("u1") == []                                     # 쉬면 메모리에서 내림(기록에서 다시 선다)

    many = [store.start("u1", {"doc": "d1"}) for _ in range(limits.max_per_user + 2)]
    assert len(store.active("u1")) == limits.max_per_user + 2           # 처리 중(잠김)인 대화는 내리지 않는다
    for c in many:
        store.checkin(c)
    store.start("u1", {"doc": "d1"})
    assert len(store.active("u1")) == limits.max_per_user               # 쉬는 것부터 내린다


def test_conversation_keeps_recent_history_and_drops_state_on_move():
    store = _store(history_keep=2)
    conv = store.start("u1", {"doc": "d1"})
    for i in range(3):
        conv.remember(f"말 {i}", f"답 {i}")
    assert [h["user"] for h in conv.history] == ["말 1", "말 2"]
    conv.state, conv.restored = object(), {"x": 1}
    conv.move_to({"doc": "d2"})
    assert conv.context == {"doc": "d2"} and conv.state is None and conv.restored is None
    with pytest.raises(ConversationError) as err:
        store.checkout(None, "nope", "u1")
    assert err.value.code == "not_found"
