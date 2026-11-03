"""근무표 수정 도우미 핵심 — 가짜 LLM 으로만 돈다(서비스 키로 실제 호출하지 않는다).

판정 재료는 손으로 만든 근무표(`tests/schedule_edit/test_solver.make_ctx`), 기록은 목록에 모은다.
턴 번호·최근 대화·대상 이동은 공통 틀(runtime)이 하므로 `say`·`press` 가 같은 순서로 대신한다.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from agent_kit.conversation import ConversationError, ConversationStore
from agents_v2.llm_client import LLMResponse, ToolCall
from schedule_edit_agent import agent as agent_mod
from schedule_edit_agent import skills as skills_mod
from schedule_edit_agent import store as store_mod
from schedule_edit_agent.agent import LLM_FAILED, EditAgent
from schedule_edit_agent.prompt import HISTORY_TURNS
from schedule_edit_agent.store import AGENT_ID, LIMITS, restore
from services.schedule_edit.rules import build_rules
from services.schedule_edit.serialize import lever_id
from services.schedule_edit.session import EditSession
from tests.schedule_edit.test_solver import BEFORE_MONTH, _two_nurse_ctx, make_ctx

USER = SimpleNamespace(account_id="acc_hn", nurse_id="HN", is_master_admin=False)


def call(name, **args):
    return LLMResponse(type="tool_call", tool_calls=[ToolCall(name=name, args=args, call_id="c1")])


def text(t):
    return LLMResponse(type="text", text=t)


class FakeLLM:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.seen = []
        self.calls = []
        self.last_usage = {"model": "fake", "input": 120, "output": 30, "cached": 100}

    def chat(self, messages, tools, *, tool_choice="auto", timeout=None):
        self.seen.append(messages)
        self.calls.append({"tool_choice": tool_choice, "timeout": timeout})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def new_store():
    return ConversationStore(AGENT_ID, LIMITS, restore)


@pytest.fixture
def world(monkeypatch):
    """판정 재료·기록을 가짜로 — open_session 은 손 근무표, record_event 는 목록에 쌓는다."""
    events = []
    ctxs = {"s1": _two_nurse_ctx}

    def fake_open(db, schedule_id, **_kw):
        ctx = ctxs.get(schedule_id, _two_nurse_ctx)()
        ctx.schedule_id = schedule_id
        return EditSession(schedule_id=schedule_id, ctx=ctx, rules=build_rules(ctx), screen=None, stamp="x")

    def fake_record(db, schedule_id, event, payload, **kw):
        events.append(SimpleNamespace(schedule_id=schedule_id, event=event, payload=payload, **kw))

    monkeypatch.setattr(agent_mod, "open_session", fake_open)
    monkeypatch.setattr(agent_mod, "is_stale", lambda db, s: False)
    monkeypatch.setattr(skills_mod, "record_event", fake_record)
    monkeypatch.setattr(agent_mod, "record_event", fake_record)
    store = new_store()
    conv = store.start(USER.account_id, {"schedule_id": "s1"})
    store.checkin(conv)
    return SimpleNamespace(events=events, ctxs=ctxs, store=store, conv=conv)


def _agent(*replies):
    llm = FakeLLM(*replies)
    return EditAgent(lambda: llm), llm


def say(agent, conv, said, today=BEFORE_MONTH, **kw):
    """말 한 턴 — runtime 처럼 턴 번호를 올리고, 끝나면 최근 대화·대상 이동."""
    conv.begin_turn()
    return _after(conv, said, agent.chat(None, USER, conv, said, today, **kw))


def press(agent, conv, action, params, today=BEFORE_MONTH):
    conv.begin_turn()
    return _after(conv, "", agent.act(None, USER, conv, action, params, today))


def _after(conv, said, reply):
    conv.remember(reply.said or said, reply.answer)
    if reply.context is not None:
        conv.move_to(reply.context)
    return reply


A_OFF = {"kind": "set", "nurse": "A", "day": 1, "shift": "OFF"}


def test_chat_proposes_and_records_message_and_result(world):
    agent, _llm = _agent(call("propose_edit", changes=[A_OFF]))
    reply = say(agent, world.conv, "A 1일 오프로 해줘")
    assert reply.preview["status"] == "ok" and reply.preview["candidates"]
    assert "A 1일 OFF — 바꿀 방법" in reply.answer
    assert reply.effects == [] and not reply.failed
    assert [e.event for e in world.events] == ["message", "propose"]
    message, proposed = world.events
    assert message.payload["usage"]["cached"] == 100 and message.payload["tool"] == "propose_edit"
    assert proposed.payload["requests"] == [{"kind": "set", "nurse_id": "A", "day": 0, "code": "O"}]
    assert proposed.conversation_id == world.conv.id and proposed.turn_no == 1


def test_unknown_or_ambiguous_names_are_asked_back(world):
    world.ctxs["s1"] = lambda: _names(make_ctx({"n1": "DODO", "n2": "ODOD"}), {"n1": "김민지", "n2": "이민지"})
    agent, _ = _agent(call("propose_edit", changes=[{**A_OFF, "nurse": "민지"}]),
                      call("propose_edit", changes=[{**A_OFF, "nurse": "박서준"}]))
    first = say(agent, world.conv, "민지 1일 오프")
    assert "김민지·이민지 중 누구인가요" in first.answer
    second = say(agent, world.conv, "박서준 1일 오프")
    assert "'박서준' 간호사가 없습니다" in second.answer
    assert [e.event for e in world.events] == ["message", "clarify", "message", "clarify"]


def _names(ctx, names):
    ctx.names.update(names)
    return ctx


def test_context_is_compact_recent_turns_plus_state(world):
    # 6턴 주고받은 뒤 LLM 에 가는 것: 고정 지시 + 최근 4턴 + 지금 상태 + 이번 말 — 후보 칸 목록은 안 간다
    agent, llm = _agent(*[call("propose_edit", changes=[A_OFF]) for _ in range(7)])
    for i in range(7):
        say(agent, world.conv, f"요청 {i}")
    last = llm.seen[-1]
    assert len(last) == 1 + 2 * HISTORY_TURNS + 1 + 1
    assert last[0]["role"] == "system" and last[0]["content"] == llm.seen[0][0]["content"]   # 앞부분 고정(캐시)
    assert [m["content"] for m in last if m["role"] == "user"][:2] == ["요청 2", "요청 3"]
    assert "지금 요청: A 1일 OFF" in last[-2]["content"] and "후보" in last[-2]["content"]
    assert all('"changes"' not in m["content"] and "before_main" not in m["content"] for m in last)


def test_revise_keeps_a_day_as_is(world):
    agent, _ = _agent(call("propose_edit", changes=[A_OFF]), call("revise_edit", keep=[{"day": 3}]))
    say(agent, world.conv, "A 1일 오프")
    reply = say(agent, world.conv, "3일은 건드리지 마")
    assert "A 3일 그대로" in reply.answer and "B 3일 그대로" in reply.answer
    for cand in reply.preview["candidates"]:
        assert all(c["day"] != 3 for c in cand["changes"])


def test_buttons_keep_reject_lever_and_apply(world, monkeypatch):
    agent, _ = _agent(call("propose_edit", changes=[A_OFF]))
    say(agent, world.conv, "A 1일 오프")
    kept = press(agent, world.conv, "keep_candidate", {"rank": 1})
    assert "1번 안을 기준으로" in kept.answer and world.conv.state.hint is not None and kept.focus == 1
    rejected = press(agent, world.conv, "reject_candidate", {"rank": 1})
    assert world.conv.state.avoid_people and rejected.preview is not None

    saved = {"new_schedule_id": "s2", "new_version": 7, "new_name": "11월 수정본", "changed_cells": 4}
    agent2, _ = _agent(call("propose_edit", changes=[A_OFF]))
    conv = world.store.start(USER.account_id, {"schedule_id": "s1"})
    say(agent2, conv, "A 1일 오프")
    monkeypatch.setattr(skills_mod, "apply_to_new_version", lambda *a, **kw: saved)
    applied = press(agent2, conv, "apply_edit", {"rank": 1})
    assert applied.effects == [{"type": "switch_version", "schedule_id": "s2", "version": 7, "status": "draft",
                                "name": "11월 수정본"}]
    assert applied.context == {"schedule_id": "s2"}
    assert conv.context == {"schedule_id": "s2"} and conv.state is None       # 다음 턴은 새 버전에서
    assert conv.history[-1]["user"] == "[버튼] 1번 안 적용"


def test_lever_button_reproposes_with_allowance(world):
    def tight():
        ctx = make_ctx({"A": "DDOO"}, off_days=2, config={"not_one_night": False})
        ctx.frozen[("A", 2)] = ctx.frozen[("A", 3)] = "주휴·특수 휴무"
        return ctx
    world.ctxs["s1"] = tight
    agent, _ = _agent(call("propose_edit", changes=[A_OFF]))
    first = say(agent, world.conv, "A 1일 오프")
    assert first.preview["status"] == "infeasible" and first.preview["levers"]
    chosen = first.preview["levers"][0]["lever_id"]
    again = press(agent, world.conv, "choose_lever", {"lever_id": chosen})
    assert again.preview["status"] == "ok"
    assert [e.event for e in world.events][-2:] == ["lever", "propose"]
    assert lever_id(world.conv.state.allow) == chosen


def test_llm_failure_and_free_text_never_reach_the_user(world):
    # 도구를 강제한다 — 그래도 글이 오면 보여 주지 않고 정해진 문장(지어낸 '반영했습니다' 를 막는다 · 검토 M3)
    agent, llm = _agent(RuntimeError("키 없음"), text("네, 김민지 14일 오프로 반영했습니다!"))
    failed = say(agent, world.conv, "바꿔줘")
    assert failed.answer == LLM_FAILED and world.events[-1].event == "error"
    asked = say(agent, world.conv, "바꿔 줬지?")
    assert "반영했습니다" not in asked.answer and asked.answer.startswith("무엇을 바꿀지 다시 말씀해")
    assert world.events[-1].payload["llm_text"].startswith("네, 김민지")     # 분석용으로만 남는다
    assert world.conv.turn_no == 2
    assert all(c["tool_choice"] == "required" and 0 < c["timeout"] <= 8 for c in llm.calls)


def test_reply_only_uses_fixed_sentences_including_status(world):
    agent, _ = _agent(call("propose_edit", changes=[A_OFF]), call("reply_only", kind="status"),
                      call("reply_only", kind="settings"), call("reply_only", kind="nonsense"))
    say(agent, world.conv, "A 1일 오프")
    status = say(agent, world.conv, "바꿔 줬어?")
    assert status.answer.startswith("지금 요청: A 1일 OFF.") and "적용'을 누르기 전까지 바뀌지 않습니다" in status.answer
    assert "설정 화면에서" in say(agent, world.conv, "나이트 인원 3명으로").answer
    assert say(agent, world.conv, "?").answer.startswith("무엇을 바꿀지")
    agent2, _ = _agent(call("reply_only", kind="unsupported"))
    assert "아직 여기서 못 합니다" in say(agent2, world.conv, "15일 E 한 명 더").answer
    assert [e.event for e in world.events][-2:] == ["message", "clarify"]


def test_deadline_counts_from_route_entry(world):
    # 경로에 들어온 지 이미 23초 — 솔버 최소 3초를 못 채우니 LLM 도 솔버도 부르지 않는다(검토 HIGH-2)
    import time as _t
    agent, llm = _agent(call("propose_edit", changes=[A_OFF]))
    reply = say(agent, world.conv, "A 1일 오프", deadline=_t.monotonic() + 2.0)
    assert "아직 반영되지 않았습니다" in reply.answer and llm.calls == []
    assert world.events[-1].payload["stage"] == "budget"


def test_partial_names_need_two_letters_at_the_end():
    from schedule_edit_agent.grounding import resolve_nurse
    ctx = _names(make_ctx({"n1": "DODO", "n2": "ODOD"}), {"n1": "김민지", "n2": "박지은수"})
    assert resolve_nurse(ctx, "민지") == ("n1", None)                 # 성을 뺀 이름
    ctx3 = _names(make_ctx({"n1": "DODO", "n2": "ODOD"}), {"n1": "장원영", "n2": "이민지"})
    assert resolve_nurse(ctx3, "원영이") == ("n1", None)              # 부를 때 붙는 '이'
    assert resolve_nurse(ctx3, "철수이")[0] is None
    assert resolve_nurse(ctx, "김민지 ")[0] == "n1"
    for said in ("민", "김", "지은"):                                  # 한 글자·성만·가운데 글자는 묻는다
        nurse, ask = resolve_nurse(ctx, said)
        assert nurse is None and ask


def test_unknown_candidate_button_is_explained(world):
    agent, _ = _agent()
    reply = press(agent, world.conv, "apply_edit", {"rank": 1})
    assert "다시 찾아 주세요" in reply.answer and reply.failed


# ── 기록에서 다시 세우기 ─────────────────────────────────────────────────────


def _row(event, turn, payload, *, schedule_id="s1", new_schedule_id=None, created_by="acc_hn", at=None):
    from datetime import datetime
    return SimpleNamespace(event=event, turn_no=turn, payload=payload, schedule_id=schedule_id,
                           new_schedule_id=new_schedule_id, created_by=created_by, created_at=at or datetime.now())


def _logs(monkeypatch, rows):
    monkeypatch.setattr(store_mod, "read_conversation", lambda db, cid: rows)
    monkeypatch.setattr(store_mod, "read_conversation_head", lambda db, cid: rows[0] if rows else None)


def test_restore_rebuilds_requests_levers_rejects_and_history(monkeypatch):
    req = [{"kind": "set", "nurse_id": "A", "day": 0, "code": "O"}]
    allow = [[["OFF수", "A", None, None, "상한"], 1]]
    rows = [_row("message", 1, {"text": "A 1일 오프"}),
            _row("propose", 1, {"requests": req, "allow": [], "avoid": [], "answer": "후보 3개"}),
            _row("lever", 2, {"lever_id": "x"}),
            _row("propose", 2, {"requests": req, "allow": allow, "avoid": [["A", "B"]], "answer": "다시 찾음"}),
            _row("message", 3, {"text": "고마워", "reply": "더 바꿀 것이 있으면 말씀해 주세요."})]
    _logs(monkeypatch, rows)
    conv = restore(None, "c1")
    assert conv.turn_no == 3 and conv.context == {"schedule_id": "s1"} and conv.owner == "acc_hn"
    assert conv.agent == AGENT_ID and conv.limits == LIMITS
    assert conv.restored == {"requests": req, "allow": allow, "avoid": [["A", "B"]]}
    assert [h["user"] for h in conv.history] == ["A 1일 오프", "고마워"]
    assert conv.request == "고마워"                                     # 흔적은 마지막 말 한 줄


def test_restore_after_apply_moves_to_new_version_and_refuses_closed(monkeypatch):
    req = [{"kind": "set", "nurse_id": "A", "day": 0, "code": "O"}]
    rows = [_row("propose", 1, {"requests": req, "allow": [], "avoid": []}),
            _row("apply", 2, {}, new_schedule_id="s2")]
    _logs(monkeypatch, rows)
    conv = restore(None, "c1")
    assert conv.context == {"schedule_id": "s2"} and conv.restored["requests"] == []
    rows.append(_row("close", 3, {}))
    with pytest.raises(ConversationError) as err:
        restore(None, "c1")
    assert err.value.code == "closed"


def test_restored_state_is_put_back_into_the_session(world, monkeypatch):
    req = [{"kind": "set", "nurse_id": "A", "day": 0, "code": "O"}]
    rows = [_row("message", 1, {"text": "A 1일 오프"}),
            _row("propose", 1, {"requests": req, "allow": [], "avoid": [["A", "B"]], "answer": "후보"})]
    _logs(monkeypatch, rows)
    store = new_store()
    conv = store.checkout(None, "c-restored", "acc_hn")
    store.checkin(conv)
    agent, llm = _agent(call("revise_edit", add=[{"kind": "set", "nurse": "B", "day": 3, "shift": "OFF"}]))
    say(agent, conv, "B 3일도 오프")
    assert "지금 요청: A 1일 OFF" in llm.seen[0][-2]["content"]          # 복원된 요청이 상태 요약에
    assert llm.seen[0][1] == {"role": "user", "content": "A 1일 오프"}  # 복원된 대화가 최근 턴에
    assert conv.state.avoid_people == [] and conv.turn_no == 2         # 요청이 바뀌어 넘긴 묶음은 비움


def test_someone_elses_conversation_is_not_loaded_into_memory(monkeypatch):
    _logs(monkeypatch, [_row("message", 1, {"text": "x", "reply": "y"}, created_by="acc_other")])
    store = new_store()
    with pytest.raises(ConversationError) as err:
        store.checkout(None, "c-other", "acc_hn")
    assert err.value.code == "forbidden" and store.active() == []


def test_restore_follows_the_version_of_the_last_record(monkeypatch):
    # 화면에서 다른 버전(s9)으로 옮겨 간 뒤의 기록은 s9 — 대상은 마지막 기록, 앞 버전의 요청은 버린다(검토 M5)
    req = [{"kind": "set", "nurse_id": "A", "day": 0, "code": "O"}]
    rows = [_row("propose", 1, {"requests": req, "allow": [], "avoid": []}),
            _row("message", 2, {"text": "x", "reply": "y"}, schedule_id="s9")]
    _logs(monkeypatch, rows)
    conv = restore(None, "c1")
    assert conv.context == {"schedule_id": "s9"} and conv.restored["requests"] == []


def test_today_matters_for_past_days(world):
    # 오늘이 11-02 면 1일은 지난 날 — 요청을 받지 않는다
    agent, _ = _agent(call("propose_edit", changes=[A_OFF]))
    reply = say(agent, world.conv, "A 1일 오프", date(2026, 11, 2))
    assert reply.preview["status"] == "invalid" and "지난 날짜" in reply.answer


# ── 개수 요청 ───────────────────────────────────────────────────────────────


def _count_ward():
    from tests.schedule_edit.test_count import _ward
    return _ward()      # A N 4개 · B·C D 4개 · 날마다 N 1명 · 등급 A2 B2 C1 · 팀 A1 B2 C1


N_LESS_2 = {"kind": "count", "nurse": "A", "shift": "N", "change": -2}


def test_count_request_asks_how_to_cover_then_solves_the_choice(world):
    world.ctxs["s1"] = _count_ward
    agent, _ = _agent(call("propose_edit", changes=[N_LESS_2]))
    asked = say(agent, world.conv, "A N 2개만 빼줘")
    assert asked.preview["status"] == "choose_transfer" and asked.effects == []
    choices = {c["choice_id"]: c for c in asked.preview["choices"]}
    assert set(choices) == {"same_grade", "same_team", "fair", "reduce_coverage", "auto"}
    assert [r["name"] for r in choices["same_grade"]["partners"]] == ["B"]
    assert [r["name"] for r in choices["same_team"]["partners"]] == ["C"]
    assert choices["reduce_coverage"]["coverage_changes"] and not choices["reduce_coverage"]["recommended"]
    assert asked.preview["direction"] == "down"
    assert all("_cells" not in c for c in asked.preview["choices"])            # 내부 판정용은 내보내지 않는다
    assert "A N 4→2회 — 빠지는 N 2개를 어떻게 메울지" in asked.answer
    assert asked.preview["question"].startswith("A N 4→2회 — 빠지는 N 2개를 어떻게 메울지")
    assert world.conv.state.last is None                                       # 묻는 동안 지난 후보는 적용 못 함

    done = press(agent, world.conv, "choose_transfer", {"choice_id": "same_team"})
    assert done.preview["status"] == "ok" and "같은 팀 간호사가 메우기" in done.answer
    assert "C N 0→2" in done.answer
    assert [e.event for e in world.events][-2:] == ["choose", "propose"]


def test_saying_another_way_overrides_the_chosen_one(world):
    world.ctxs["s1"] = _count_ward
    agent, _ = _agent(call("propose_edit", changes=[{**N_LESS_2, "transfer": "same_grade"}]),
                      call("revise_edit", transfer="same_team"))
    first = say(agent, world.conv, "A N 2개 빼고 같은 등급이 메워")
    assert first.preview["status"] == "ok" and "B N 0→2" in first.answer         # 말한 방식이면 묻지 않는다
    second = say(agent, world.conv, "아니 같은 팀으로")
    assert "C N 0→2" in second.answer                                            # 정한 방식도 말로 바꾼다


def test_count_without_number_shows_status_and_asks(world):
    world.ctxs["s1"] = _count_ward
    agent, _ = _agent(call("propose_edit", changes=[{"kind": "count", "nurse": "A", "shift": "N"}]))
    reply = say(agent, world.conv, "A 나이트 너무 많아")
    assert reply.answer.startswith("A 11월 N 4회입니다(병동 평균") and reply.answer.endswith("몇 회로 바꿀까요?")


def test_asking_for_more_offers_whom_to_take_from(world):
    world.ctxs["s1"] = _count_ward
    agent, _ = _agent(call("propose_edit", changes=[{"kind": "count", "nurse": "B", "shift": "N", "change": 1}]))
    asked = say(agent, world.conv, "B N 하나 더 줘")
    assert asked.preview["status"] == "choose_transfer" and asked.preview["direction"] == "up"
    assert "늘어나는 N 1개를 누구에게서 넘겨받을지" in asked.answer
    choices = {c["choice_id"]: c for c in asked.preview["choices"]}
    assert set(choices) == {"same_grade", "same_team", "fair", "add_coverage", "auto"}
    assert choices["same_grade"]["label"] == "같은 등급 간호사의 근무를 넘겨받기"
    assert [r["name"] for r in choices["same_grade"]["partners"]] == ["A"]       # A 의 N 이 줄어든다
