"""바꾸기 전 확인·적용 전 확인 흐름(11-03 · 사용자 '그럼에도 바꿀 것이냐고 납득시키는 과정') — 가짜 LLM 으로만."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from schedule_edit_agent import skills as skills_mod
from schedule_edit_agent import store as store_mod
from schedule_edit_agent.skills import CONFIRM_ASK
from schedule_edit_agent.store import restore
from schedule_edit_agent.view import CONFIRM_NOTE, blocks_for, step_label
from services.schedule_edit.precheck import Precheck
from services.schedule_edit.requests import SetCell
from tests.edit_agent.test_agent import A_OFF, _agent, call, make_ctx, press, say

OFF_KEY = ("OFF수", "A", None, None, "상한")
BEND = "A OFF 수 상한보다 1개 많게"


def _tight():
    """A 는 3·4일이 바꿀 수 없는 OFF 라 1일을 OFF 로 하면 OFF 수(2개)를 반드시 넘는다. B 는 여유."""
    ctx = make_ctx({"A": "DDOO", "B": "DDDD"}, off_days=2, config={"not_one_night": False})
    ctx.frozen[("A", 2)] = ctx.frozen[("A", 3)] = "주휴·특수 휴무"
    return ctx


def test_unavoidable_bend_is_asked_before_solving_then_granted(world):
    world.ctxs["s1"] = _tight
    agent, _ = _agent(call("propose_edit", changes=[A_OFF]))
    asked = say(agent, world.conv, "A 1일 오프")
    assert asked.preview["status"] == "confirm" and world.conv.state.last is None      # 아직 풀지 않았다
    assert f"· {BEND}" in asked.answer and asked.answer.endswith(CONFIRM_ASK)
    blocks = blocks_for(asked, world.conv.state)
    assert blocks[0]["text"] == "A 1일 OFF — 바꾸기 전에 셈해 보니(전월 근무 포함) 어떤 방법으로 바꿔도 아래가 생깁니다."
    card = blocks[-1]["items"][0]
    assert blocks[-1]["footnote"] == CONFIRM_NOTE and card["lines"][0]["text"] == BEND
    assert card["title"] == "그대로 바꾸면 생기는 것 1가지"
    assert [a["input"]["action"] for a in card["actions"]] == ["confirm_request", "cancel_request"]

    done = press(agent, world.conv, "confirm_request", {})
    assert done.preview["status"] == "ok", done.answer
    assert [e.event for e in world.events][-2:] == ["confirm", "propose"]
    assert world.conv.state.allow == {OFF_KEY: 1}                  # 동의한 만큼 이번만 넘겼다
    assert step_label("confirm_request", {}, world.conv.state) == "그래도 진행"


def test_typed_yes_goes_ahead_and_the_state_tells_the_llm(world):
    world.ctxs["s1"] = _tight
    agent, llm = _agent(call("propose_edit", changes=[A_OFF]), call("answer_confirm", go=True))
    say(agent, world.conv, "A 1일 오프")
    reply = say(agent, world.conv, "응 그래도 해줘")
    assert reply.preview["status"] == "ok"
    assert "확인 기다림" in llm.seen[1][-2]["content"]
    assert reply.trail == "step"


def test_cancel_goes_back_to_the_candidates_before_the_question(world):
    world.ctxs["s1"] = _tight
    agent, _ = _agent(call("propose_edit", changes=[{"kind": "set", "nurse": "B", "day": 1, "shift": "OFF"}]),
                      call("revise_edit", add=[A_OFF]))
    first = say(agent, world.conv, "B 1일 오프")
    assert first.preview["status"] == "ok"
    assert say(agent, world.conv, "A 1일도 오프").preview["status"] == "confirm"
    back = press(agent, world.conv, "cancel_request", {})
    session = world.conv.state
    assert back.answer == "바꾸지 않았습니다. 근무표는 그대로입니다. 앞서 찾은 후보는 아래에 그대로 있습니다."
    assert session.requests == [SetCell("B", 0, "O")] and session.last.candidates and session.allow == {}
    blocks = blocks_for(back, session)
    assert blocks[-1]["type"] == "cards" and blocks[-1]["selectable"]          # 앞 후보 카드가 그대로
    assert world.events[-1].event == "cancel"


def test_apply_lists_what_gets_worse_before_saving(world, monkeypatch):
    # 레버로 넘긴 설정은 풀기 전 확인을 거치지 않았다 — 적용 전에 구체적인 위치로 다시 묻는다
    monkeypatch.setattr(skills_mod, "precheck", lambda *a, **k: Precheck())
    world.ctxs["s1"] = _tight
    agent, _ = _agent(call("propose_edit", changes=[A_OFF]))
    first = say(agent, world.conv, "A 1일 오프")
    press(agent, world.conv, "choose_lever", {"lever_id": first.preview["levers"][0]["lever_id"]})
    captured = {}
    saved = {"new_schedule_id": "s2", "new_version": 7, "status": "draft", "new_name": "11월 수정본"}
    monkeypatch.setattr(skills_mod, "apply_to_new_version", lambda *a, **kw: captured.update(kw) or saved)

    ask = press(agent, world.conv, "apply_edit", {"rank": 1})
    assert ask.preview["status"] == "confirm_apply" and not ask.effects and not captured
    assert ask.preview["notes"] == []                      # 참고 문구는 후보 때 이미 — 확인 질문에 다시 붙이지 않는다
    assert ask.answer.startswith("1안을 적용하면 지금보다 나빠지는 것이 있습니다. 그래도 적용할까요?")
    assert f"· {BEND}" in ask.answer
    card = blocks_for(ask, world.conv.state)[-1]["items"][0]
    assert card["overlay"]["kind"] == "schedule_cells" and card["title"].endswith("적용 전 확인")
    seen = [f["id"] for f in ask.preview["findings"]]
    assert [a["input"]["params"] for a in card["actions"]] == [{"rank": 1, "confirmed": True, "seen": seen},
                                                                {"rank": 1}]
    assert world.events[-1].event == "check"

    back = press(agent, world.conv, "back_to_candidates", {"rank": 1})
    assert back.focus == 1 and back.answer.startswith("적용하지 않았습니다")

    # 카드에 보이지 않은 것이 있으면 [이대로 적용]이어도 다시 묻는다(낡은 카드 버튼 · 검토 L1)
    stale = press(agent, world.conv, "apply_edit", {"rank": 1, "confirmed": True, "seen": []})
    assert stale.preview["status"] == "confirm_apply" and not captured
    done = press(agent, world.conv, "apply_edit", {"rank": 1, "confirmed": True, "seen": seen})
    assert done.effects and BEND in [a["text"] for a in captured["accepted"]]   # 받아들인 것은 적용 기록에 남는다
    assert step_label("apply_edit", {"rank": 1, "confirmed": True}, None) == "1안 확인하고 적용"


def test_already_confirmed_bend_is_not_asked_again_at_apply(world, monkeypatch):
    world.ctxs["s1"] = _tight
    agent, _ = _agent(call("propose_edit", changes=[A_OFF]))
    say(agent, world.conv, "A 1일 오프")
    press(agent, world.conv, "confirm_request", {})
    saved = {"new_schedule_id": "s2", "new_version": 7, "status": "draft", "new_name": "11월 수정본"}
    captured = {}
    monkeypatch.setattr(skills_mod, "apply_to_new_version", lambda *a, **kw: captured.update(kw) or saved)
    reply = press(agent, world.conv, "apply_edit", {"rank": 1})
    # 동의한 위치·양은 다시 묻지 않고, 새로 나빠지는 품질만 묻는다 — 분배 지표는 '분배'로(사용자 결정 11-05)
    assert [f["text"] for f in reply.preview["findings"]] == ["사람마다 OFF 수 차이 2→3"]
    card = blocks_for(reply, world.conv.state)[-1]["items"][0]
    assert [line["text"] for line in card["lines"]] == ["분배가 나빠짐: 사람마다 OFF 수 차이 2→3"]
    done = press(agent, world.conv, "apply_edit", card["actions"][0]["input"]["params"])
    # 받아들인 것 전부를 종류와 함께 기록 — 변경 이력이 '넘긴 설정'과 '근무 모양'을 나눠 보인다
    assert done.effects and [(a["kind"], a["text"]) for a in captured["accepted"]] == [
        ("setting", BEND), ("quality", "사람마다 OFF 수 차이 2→3")]


def test_restore_brings_back_the_pending_question_and_what_was_confirmed(monkeypatch):
    finding = {"id": "OFF수/A/None/None/상한|1", "text": BEND, "kind": "setting",
               "allow": [[list(OFF_KEY), 1]]}
    req = [{"kind": "set", "nurse_id": "A", "day": 0, "code": "O"}]

    def row(event, turn, payload):
        return SimpleNamespace(event=event, turn_no=turn, payload=payload, schedule_id="s1", new_schedule_id=None,
                               created_by="acc_hn", created_at=datetime.now())
    rows = [row("message", 1, {"text": "A 1일 오프", "tool": "propose_edit"}),
            row("propose", 1, {"requests": req, "allow": [], "avoid": [], "answer": "묻는 중",
                               "result": {"status": "confirm", "findings": [finding]}})]
    monkeypatch.setattr(store_mod, "read_conversation", lambda db, cid: rows)
    monkeypatch.setattr(store_mod, "read_conversation_head", lambda db, cid: rows[0])
    assert restore(None, "c1").restored["asking"] == [finding]
    assert restore(None, "c1").restored["held"] == {"requests": [], "allow": [], "avoid": []}   # 묻기 전 = 빈 요청
    allow_total = [[list(OFF_KEY), 1]]
    rows.append(row("confirm", 2, {"ids": [finding["id"]], "allow_total": allow_total}))
    state = restore(None, "c1").restored
    assert state["asking"] == [] and state["confirmed"] == [finding["id"]]
    assert state["allow"] == allow_total          # 다음 제안 기록 전에 끊겨도 동의한 양이 남는다(검토 M1)


def test_cancel_on_a_later_question_goes_back_to_the_last_solved_request(world):
    # 동의하고 풀린 뒤의 새 질문에서 [바꾸지 않기] → 그 앞에 풀린 요청으로(두 번째 질문은 다른 칸의 원티드).
    # 풀리기 전 연달아 묻는 경우의 되돌릴 지점은 첫 질문 앞 그대로다(`_answer` 가 동의 때 held 를 비우지 않는다 · 검토 M3)
    def ward():
        ctx = _tight()
        ctx.wishes[("B", 1)] = {"D"}
        return ctx
    world.ctxs["s1"] = ward
    agent, _ = _agent(call("propose_edit", changes=[{"kind": "set", "nurse": "B", "day": 3, "shift": "OFF"}]),
                      call("revise_edit", add=[A_OFF]),
                      call("revise_edit", add=[{"kind": "set", "nurse": "B", "day": 2, "shift": "OFF"}]))
    assert say(agent, world.conv, "B 3일 오프").preview["status"] == "ok"
    assert say(agent, world.conv, "A 1일도 오프").preview["status"] == "confirm"
    press(agent, world.conv, "confirm_request", {})
    second = say(agent, world.conv, "B 2일도 오프")
    assert second.preview["status"] == "confirm" and "원티드" in second.answer
    press(agent, world.conv, "cancel_request", {})
    assert world.conv.state.requests == [SetCell("B", 2, "O"), SetCell("A", 0, "O")]


def test_questions_in_a_row_before_solving_cancel_to_the_state_before_the_first(world, monkeypatch):
    # 동의 직후 다시 묻는 경우(점검이 새 것을 짚음) — [바꾸지 않기]는 첫 질문 앞의 요청·후보로(검토 M3)
    from services.schedule_edit.precheck import Finding
    asks = iter([[], [Finding("a|1", "첫째", "setting")], [Finding("b|1", "둘째", "setting")]])
    monkeypatch.setattr(skills_mod, "precheck", lambda *a, **k: Precheck(findings=next(asks)))
    world.ctxs["s1"] = _tight
    agent, _ = _agent(call("propose_edit", changes=[{"kind": "set", "nurse": "B", "day": 1, "shift": "OFF"}]),
                      call("revise_edit", add=[A_OFF]))
    assert say(agent, world.conv, "B 1일 오프").preview["status"] == "ok"
    before = world.conv.state.last
    assert say(agent, world.conv, "A 1일도 오프").preview["status"] == "confirm"
    assert press(agent, world.conv, "confirm_request", {}).preview["status"] == "confirm"     # 또 묻는다
    back = press(agent, world.conv, "cancel_request", {})
    assert world.conv.state.requests == [SetCell("B", 0, "O")] and world.conv.state.last is before
    assert back.answer.endswith("앞서 찾은 후보는 아래에 그대로 있습니다.")


def test_change_history_says_which_setting_the_edit_passed(monkeypatch):
    # 변경 이력 — 수정 도우미가 만든 버전이면 요청과 이번만 넘긴 설정(사용자 지시 11-03 '어떤 부분을 변경해서 발생한 수정인지')
    from services.schedule_edit import journal
    row = SimpleNamespace(created_at=datetime(2026, 11, 3, 14, 20), payload={
        "request_text": "김태희 17일 OFF",
        "accepted": [{"kind": "setting", "text": "김태희 개인 OFF 수 상한을 1개 넘김"},
                     {"kind": "wanted", "text": "장원영 9일 원티드 칸 E→O"},
                     {"kind": "quality", "text": "사람마다 OFF 수 차이 2→3"}]})
    monkeypatch.setattr(journal, "read_apply_for", lambda db, sid: row if sid == "s2" else None)
    assert journal.edit_origin(None, "s2") == {
        "request": "김태희 17일 OFF", "bends": ["김태희 개인 OFF 수 상한을 1개 넘김", "장원영 9일 원티드 칸 E→O"],
        "shape": ["사람마다 OFF 수 차이 2→3"], "at": "2026-11-03T14:20:00"}
    assert journal.edit_origin(None, "s1") is None          # 사람이 직접 고친 버전은 없음
    # 이 기능 전 기록(받아들인 목록 없음) — 고른 레버의 허용 위치로(오전 실화면의 14일 D 미달 버전)
    old_row = SimpleNamespace(created_at=None, payload={"request_text": "장원영 14일 오프로", "allow": [
        [["인원최소", None, 13, "D"], 1]]})
    monkeypatch.setattr(journal, "read_apply_for", lambda db, sid: old_row)
    assert journal.edit_origin(None, "s3")["bends"] == ["14일 D 최소 근무 인원보다 1명 적게"]
