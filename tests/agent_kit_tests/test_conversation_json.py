"""대화 메타 JSON 왕복(S10a) — 저장하는 칸·안 하는 칸, history 8개 자르기."""

from __future__ import annotations

import json
import threading

from agent_kit.conversation import Conversation, Limits


def _conv(**kw) -> Conversation:
    return Conversation(id="p-1", agent="schedule_edit", owner="u1", context={"schedule_id": "s1"},
                        created_at=100.5, touched_at=200.25, limits=Limits(), **kw)


def test_meta_roundtrip_skips_runtime_fields():
    conv = _conv(turn_no=3, dropped=2, request="김민지 N 빼줘", steps=["하나", "둘"], carry={"edit_from": (2026, 11, 4)})
    conv.remember("안녕", "답")
    conv.last_blocks = [{"type": "x"}]
    conv.state, conv.restored = object(), {"a": 1}
    data = json.loads(json.dumps(conv.to_json({"stamp": "s"}), ensure_ascii=False))
    assert data["state"] == {"stamp": "s"}
    for gone in ("lock", "last_blocks", "restored"):
        assert gone not in data
    back = Conversation.from_json(data, Limits())
    assert (back.id, back.agent, back.owner, back.context) == ("p-1", "schedule_edit", "u1", {"schedule_id": "s1"})
    assert (back.turn_no, back.dropped, back.request, back.steps) == (3, 2, "김민지 N 빼줘", ["하나", "둘"])
    assert (back.created_at, back.touched_at) == (100.5, 200.25)
    assert back.history == conv.history
    assert back.carry == {"edit_from": [2026, 11, 4]}      # 튜플은 JSON 에서 목록이 된다
    assert back.state is None and back.restored is None and back.last_blocks == []
    assert not back.lock.locked() and isinstance(back.lock, type(threading.Lock()))


def test_history_is_trimmed_to_keep_and_dropped_grows():
    conv = _conv()
    conv.history = [{"user": f"u{i}", "answer": f"a{i}", "shown": {"kind": "text", "text": f"u{i}"}} for i in range(11)]
    data = conv.to_json()
    assert [h["user"] for h in data["history"]] == [f"u{i}" for i in range(3, 11)]
    assert len(data["history"]) == 8 and data["dropped"] == 3
    assert len(conv.history) == 11 and conv.dropped == 0      # 원본은 건드리지 않는다
    assert Conversation.from_json(data, Limits()).dropped == 3


def test_state_defaults_to_none():
    assert _conv().to_json()["state"] is None
