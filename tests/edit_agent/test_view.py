"""근무표 수정 결과 → 화면 블록(`view.py`) — 화면은 이 블록을 그리기만 하므로 문구·버튼·미리보기가 여기서 정해진다."""

from __future__ import annotations

from schedule_edit_agent import skills as skills_mod
from schedule_edit_agent.agent import Reply
from schedule_edit_agent.view import CHOICE_NOTE, LEVER_NOTE, blocks_for, step_label
from services.schedule_edit.precheck import Precheck
from tests.edit_agent.test_agent import A_OFF, N_LESS_2, _agent, _count_ward, call, make_ctx, press, say


def kinds(blocks):
    return [b["type"] for b in blocks]


def test_candidates_become_cards_with_buttons_and_table_preview(world):
    agent, _ = _agent(call("propose_edit", changes=[A_OFF]))
    reply = say(agent, world.conv, "A 1일 오프")
    blocks = blocks_for(reply, world.conv.state)
    assert kinds(blocks) == ["text", "cards"] and blocks[0]["text"] == reply.answer
    cards = blocks[1]
    candidates = reply.preview["candidates"]
    assert [c["id"] for c in cards["items"]] == [str(c["rank"]) for c in candidates]
    top, first = cards["items"][0], candidates[0]
    assert top["title"].startswith(f"1안 · {first['changed_count']}칸 · ")
    assert top["summary"] == " · ".join(p["name"] for p in first["affected"])
    assert [chip["label"] for chip in top["chips"]] == [p["name"] for p in first["affected"]]
    assert top["details"]["label"] == f"바뀌는 칸 {len(first['changes'])}개"
    assert len(top["details"]["items"]) == len(first["changes"])
    assert [a["label"] for a in top["actions"]] == ["이 안 적용", "이 안에서 다듬기", "이 사람들 말고"]
    assert top["actions"][0]["style"] == "primary"
    assert top["overlay"] == {"kind": "schedule_cells",
                              "cells": [{"nurse_id": x["nurse_id"], "day": x["day"], "before": x["before"],
                                         "after": x["after"]} for x in first["changes"]]}
    again = blocks_for(reply, world.conv.state)[1]
    assert cards["key"] and again["key"] == cards["key"]                 # 같은 결과면 같은 key — 화면이 고른 카드를 둔다
    two = {**reply.preview, "candidates": [first, {**first, "rank": 2}]}
    assert blocks_for(Reply(answer="x", preview=two), world.conv.state)[1]["selected"] == "1"
    focused = blocks_for(Reply(answer="x", preview=two, focus=2), world.conv.state)[1]
    assert focused["selected"] == "2"                                     # 다듬기로 고른 안을 펼쳐 둔다


def test_text_only_turn_shows_the_current_result_again(world):
    agent, _ = _agent(call("propose_edit", changes=[A_OFF]), call("reply_only", kind="status"))
    proposed = say(agent, world.conv, "A 1일 오프")
    status = say(agent, world.conv, "바꿔 줬어?")
    assert status.preview is None
    blocks = blocks_for(status, world.conv.state)
    assert kinds(blocks) == ["text", "cards"] and blocks[0]["text"] == status.answer
    assert blocks[1]["key"] == blocks_for(proposed, world.conv.state)[1]["key"]


def test_failed_button_is_an_error_line_over_the_same_result(world):
    agent, _ = _agent(call("propose_edit", changes=[A_OFF]))
    say(agent, world.conv, "A 1일 오프")
    failed = press(agent, world.conv, "keep_candidate", {"rank": 9})
    blocks = blocks_for(failed, world.conv.state)
    assert blocks[0]["tone"] == "error" and kinds(blocks) == ["text", "cards"]


def test_transfer_question_becomes_a_choice(world):
    world.ctxs["s1"] = _count_ward
    agent, _ = _agent(call("propose_edit", changes=[N_LESS_2]))
    asked = say(agent, world.conv, "A N 2개만 빼줘")
    blocks = blocks_for(asked, world.conv.state)
    assert kinds(blocks) == ["text", "choice"]
    assert blocks[0]["text"].startswith(asked.preview["question"])
    choice = blocks[1]
    preview_choices = asked.preview["choices"]
    assert [o["id"] for o in choice["options"]] == [c["choice_id"] for c in preview_choices]
    recommended = next(c["choice_id"] for c in preview_choices if c["recommended"])
    assert choice["selected"] == recommended and choice["legend"] == "메우는 방식"
    assert next(o for o in choice["options"] if o["id"] == recommended)["badge"] == "추천"
    for c, o in zip(preview_choices, choice["options"]):
        assert (o["disabled_reason"] is None) == bool(c["feasible"])
        if c["feasible"]:
            assert o["notes"] == c["quality"]                             # 근무 모양은 요약과 따로 전부
            assert all(q not in o["summary"] for q in c["quality"])
    assert choice["submit"]["input"] == {"kind": "action", "action": "choose_transfer", "params": {}}
    assert choice["submit"]["param"] == "choice_id" and choice["footnote"] == CHOICE_NOTE

    step = step_label("choose_transfer", {"choice_id": "same_team"}, world.conv.state)
    assert step.startswith("메우는 방식: ") and "같은 팀" in step


def test_infeasible_shows_lever_cards_titled_by_the_setting(world, monkeypatch):
    # 같은 근무표에서 사전 점검은 먼저 묻는다(test_confirm) — 여기서는 끄고 레버 카드만 본다
    monkeypatch.setattr(skills_mod, "precheck", lambda *a, **k: Precheck())

    def tight():
        ctx = make_ctx({"A": "DDOO"}, off_days=2, config={"not_one_night": False})
        ctx.frozen[("A", 2)] = ctx.frozen[("A", 3)] = "주휴·특수 휴무"
        return ctx
    world.ctxs["s1"] = tight
    agent, _ = _agent(call("propose_edit", changes=[A_OFF]))
    reply = say(agent, world.conv, "A 1일 오프")
    blocks = blocks_for(reply, world.conv.state)
    levers = blocks[-1]
    assert levers["type"] == "cards" and levers["selectable"] is False and levers["footnote"] == LEVER_NOTE
    assert reply.preview.get("blocking")
    assert [b["type"] for b in blocks[1:]] == ["cards"]          # 내부 규칙 이름 줄('막는 설정 — OFF수')은 없다
    lever = reply.preview["levers"][0]
    card = levers["items"][0]
    assert card["id"] == lever["lever_id"]
    assert card["actions"][0]["input"] == {"kind": "action", "action": "choose_lever",
                                           "params": {"lever_id": lever["lever_id"]}}
    assert card["title"] == " · ".join(lever["lose"]) == "A 개인 OFF 수 상한을 1개 넘김"   # 카드마다 무엇을 넘기는지
    assert card["lines"][0]["text"].startswith("넘기면 ")

    step = step_label("choose_lever", {"lever_id": lever["lever_id"]}, world.conv.state)
    assert step == "이번만 넘김: " + ", ".join(lever["lose"])
    assert step_label("choose_lever", {"lever_id": "없는 레버"}, world.conv.state) == "설정 하나를 이번만 넘김"


def test_applied_is_one_success_notice(world, monkeypatch):
    agent, _ = _agent(call("propose_edit", changes=[A_OFF]))
    say(agent, world.conv, "A 1일 오프")
    monkeypatch.setattr(skills_mod, "apply_to_new_version",
                        lambda *a, **kw: {"new_schedule_id": "s2", "new_version": 7, "new_name": "11월 수정본"})
    applied = press(agent, world.conv, "apply_edit", {"rank": 1})
    blocks = blocks_for(applied, world.conv.state)
    assert kinds(blocks) == ["notice"] and blocks[0]["tone"] == "success"
    assert blocks[0]["text"].startswith("11월 수정본(VER7)으로 저장했습니다")


def test_step_labels_are_made_from_the_state_before_the_button(world):
    agent, _ = _agent(call("propose_edit", changes=[A_OFF]))
    reply = say(agent, world.conv, "A 1일 오프")
    session = world.conv.state
    names = [session.ctx.names.get(n, n) for n in sorted(session.last.candidates[0].affected)]
    assert step_label("apply_edit", {"rank": 1}, session) == "1안 적용"
    assert step_label("keep_candidate", {"rank": 2}, session) == "2안을 기준으로 다듬기"
    assert step_label("reject_candidate", {"rank": 1}, session) == " · ".join(names) + " 말고 다시 찾기"
    assert step_label("reject_candidate", {"rank": 9}, session) == "9안의 사람들 말고 다시 찾기"
    assert step_label("something_else", {}, session) is None
    assert reply.preview["status"] == "ok"
