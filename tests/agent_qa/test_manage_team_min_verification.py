"""manage_team_min 검증 — 수간호사 자연어 쿼리로 팀 최소 인원이 실제로 바뀌는지.

근무표 생성은 하지 않는다. 자연어 → preview → '응' 확인 → 적용,
그 뒤 list_teams_with_members 로 DB 반영을 확인.
"""

from __future__ import annotations

from agents_v2.skills.registry import _ensure_loaded
from services.team_service import list_teams_with_members

from tests.agent_qa.harness import AgentTestSession, ScriptedClient, ScriptedResponse

_ensure_loaded()


def _tc(args: dict) -> ScriptedResponse:
    return ScriptedResponse(tool_calls=[{"name": "manage_team_min", "args": args}])


_PREVIEW_TEXT = ScriptedResponse(text="진행하시겠습니까? (응 / 취소)")


def _preview_seq(*arg_dicts: dict) -> list[ScriptedResponse]:
    seq: list[ScriptedResponse] = []
    for args in arg_dicts:
        seq.append(_tc(args))
        seq.append(_PREVIEW_TEXT)
    return seq


def _confirm_apply(sess: AgentTestSession, query: str) -> None:
    r1 = sess.send(query)
    assert r1.awaiting_approval, f"미리보기 단계가 떠야 함: {query!r}"
    sess.send("응")
    sess.ctx.pending_approval = None  # harness 는 승인 후 자동 비우지 않음


def _team_min(db, team_name: str):
    for t in list_teams_with_members(db, "OFF001", "GRP001"):
        if t["team_name"] == team_name:
            return t["min_shift"] or {}
    return {}


def test_hn_set_team_min_reflected(db, seed_data):
    cli = ScriptedClient(_preview_seq(
        {"operation": "set_min", "team_name": "A팀", "shift_name": "데이",
         "min_count": 2, "preview_only": True},
        {"operation": "set_min", "team_name": "A팀", "shift_name": "나이트",
         "min_count": 1, "preview_only": True},
    ))
    sess = AgentTestSession(db, client=cli, user_role="HN")

    _confirm_apply(sess, "A팀은 데이에 최소 2명은 있어야 해")
    assert int(_team_min(db, "A팀")["D"]) == 2

    _confirm_apply(sess, "A팀 나이트도 최소 1명으로")
    ms = _team_min(db, "A팀")
    assert int(ms["D"]) == 2 and int(ms["N"]) == 1


def test_hn_clear_team_min_reflected(db, seed_data):
    cli = ScriptedClient(_preview_seq(
        {"operation": "set_min", "team_name": "B팀", "shift_name": "이브닝",
         "min_count": 1, "preview_only": True},
        {"operation": "clear_min", "team_name": "B팀", "shift_name": "이브닝",
         "preview_only": True},
    ))
    sess = AgentTestSession(db, client=cli, user_role="HN")

    _confirm_apply(sess, "B팀 이브닝 최소 1명")
    assert int(_team_min(db, "B팀")["E"]) == 1

    _confirm_apply(sess, "B팀 이브닝 최소 인원 제한 없애줘")
    assert "E" not in _team_min(db, "B팀")


def test_hn_read_team_min(db, seed_data):
    cli = ScriptedClient([_tc({"operation": "read"})])
    sess = AgentTestSession(db, client=cli, user_role="HN")
    r = sess.send("팀별 최소 인원 어떻게 돼있어?")
    # read 는 preview 가 아니라 즉시 답변
    assert not r.awaiting_approval
    # 실행 단계에서 manage_team_min 이 호출됨
    sess.assert_tool_called("manage_team_min", {"operation": "read"})
