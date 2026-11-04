"""문장·시나리오 평가(11-03)에서 나온 결함·문장 — 가짜 LLM 으로 그 흐름을 다시 밟는다."""

from __future__ import annotations

from schedule_edit_agent.answer import status_text
from schedule_edit_agent.grounding import count_status
from services.schedule_edit.requests import CountTarget, count_in
from tests.edit_agent.test_agent import A_OFF, _agent, call, make_ctx, say


def _period_ward():
    # A 의 20~22일 중 21일을 그대로 두라고 고쳐 말하는 흐름(B1) — 5일짜리로 줄였다: A 1~3일 OFF 요청, 2일은 그대로
    return make_ctx({"A": "DDDOO", "B": "OOODD", "C": "DDOOD"}, cov_min={"D": 1}, config={"not_one_night": False})


def test_keeping_a_requested_day_drops_that_request(world):
    world.ctxs["s1"] = _period_ward
    agent, _ = _agent(call("propose_edit", changes=[{"kind": "off_period", "nurse": "A", "day": 1, "to_day": 3}]),
                      call("revise_edit", keep=[{"day": 2}]))
    say(agent, world.conv, "A 1~3일 오프")
    reply = say(agent, world.conv, "2일은 그대로 둬")
    reqs = world.conv.state.requests
    assert ("A", 1) not in {(r.nurse_id, r.day) for r in reqs if r.code == "O"}     # A 2일 OFF 요청은 빠진다
    assert "2일 3명 그대로" in reply.answer and "A 2일 OFF" not in reply.answer


def test_status_says_already_and_not_no_way(world):
    agent, _ = _agent(call("propose_edit", changes=[{"kind": "set", "nurse": "A", "day": 2, "shift": "OFF"}]),
                      call("reply_only", kind="status"))
    first = say(agent, world.conv, "A 2일 오프")                    # A 는 DODO — 2일은 이미 OFF
    assert "이미 그렇게 되어 있습니다" in first.answer
    status = say(agent, world.conv, "반영 안 된 거지?")
    assert "이미 그렇게 되어 있어 바꿀 칸이 없습니다" in status.answer and "방법이 없었습니다" not in status.answer


def test_off_count_leaves_out_vacation_cells():
    ctx = make_ctx({"A": "OOODD", "B": "DDDOO"})
    ctx.vacation.add(("A", 0))                                        # A 1일은 연차(대표코드 O)
    req = CountTarget("A", frozenset({"O"}), 3, 0, 4)
    assert count_in(ctx, ctx.main, req) == 2                          # 순수 OFF 만
    assert count_status(ctx, "A", "O").startswith("A 11월 OFF 2회")


def test_swapping_the_same_shift_is_answered_at_once(world):
    agent, _ = _agent(call("propose_edit", changes=[{"kind": "swap", "nurse": "A", "other_nurse": "B", "day": 1}]))
    world.ctxs["s1"] = lambda: make_ctx({"A": "DODO", "B": "DOOD"}, cov_min={"D": 1})
    reply = say(agent, world.conv, "1일 A랑 B 바꿔줘")
    assert reply.answer.startswith("A·B 둘 다 1일 D라 바꿔도 같습니다")


def test_reference_note_is_shown_once_per_conversation(world):
    def noted():
        ctx = make_ctx({"A": "DODO", "B": "ODOD"}, cov_min={"D": 1}, cov_max={"D": 1}, off_days=2,
                       config={"not_one_night": False})
        ctx.notes.append("전월 근무표가 없어 월초 규칙을 이번 달 안에서만 봤습니다.")
        return ctx
    world.ctxs["s1"] = noted
    agent, _ = _agent(call("propose_edit", changes=[A_OFF]), call("revise_edit", keep=[{"day": 3}]))
    assert "참고: 전월 근무표가 없어" in say(agent, world.conv, "A 1일 오프").answer
    assert "참고:" not in say(agent, world.conv, "3일은 건드리지 마").answer


def test_unsupported_answer_names_the_kind(world):
    agent, _ = _agent(call("reply_only", kind="unsupported", topic="undo"),
                      call("reply_only", kind="unsupported"))
    assert say(agent, world.conv, "방금 버전 되돌려줘").answer.startswith("만든 버전 되돌리기")
    # (여러 사람 고르게 맞추기는 11-03 · 근무 모양은 11-04 부터 지원 — test_balance.py · test_shape.py)
    assert "(버전 되돌리기)" in say(agent, world.conv, "?").answer              # 종류를 모르면 전체 목록


def test_status_after_postprocess_is_kept_in_the_session(world):
    # (자동 연차전환 이유는 test_postprocess 에서) 지금 요청이 없을 때의 기본 문장
    assert status_text(SimpleSession()) == "지금 요청: 없음. 아직 찾은 결과가 없습니다."


class SimpleSession:
    def __init__(self):
        self.ctx = make_ctx({"A": "DODO"})
        self.requests, self.last = [], None


def test_name_only_answer_shows_the_interpreted_request_in_the_trail(world):
    # '18일 오프로' → 누구? → '박민지' — 흔적의 요청 줄이 '박민지' 가 아니라 해석한 요청(평가 11-03 U1)
    agent, _ = _agent(call("reply_only", kind="who"), call("propose_edit", changes=[A_OFF]))
    say(agent, world.conv, "1일 오프로 해줘")
    reply = say(agent, world.conv, "A")
    assert reply.trail == "new" and reply.request_line == "A 1일 OFF"


def test_restore_uses_the_described_request_for_the_trail(monkeypatch):
    from types import SimpleNamespace
    from schedule_edit_agent import store as store_mod
    from schedule_edit_agent.store import restore
    from datetime import datetime
    rows = [SimpleNamespace(event="message", turn_no=1, payload={"text": "A", "tool": "propose_edit"},
                            schedule_id="s1", new_schedule_id=None, created_by="acc_hn", created_at=datetime.now()),
            SimpleNamespace(event="propose", turn_no=1, payload={"requests": [], "described": "A 1일 OFF"},
                            schedule_id="s1", new_schedule_id=None, created_by="acc_hn", created_at=datetime.now())]
    monkeypatch.setattr(store_mod, "read_conversation", lambda db, cid: rows)
    monkeypatch.setattr(store_mod, "read_conversation_head", lambda db, cid: rows[0])
    assert restore(None, "c1").request == "A 1일 OFF"
