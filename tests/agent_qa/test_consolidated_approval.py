"""Consolidated 승인 게이트 — 한 턴에 여러 mutation preview → 1회 승인 → 전부 실행.

복합 쿼리에서 병렬 tool_call 로 mutation 이 여러 개 preview 될 때, 첫 것만 처리하고
나머지를 드롭하던 문제(Bug [4])를 batch 승인으로 해결했는지 검증.
"""

from __future__ import annotations

from tests.agent_qa.harness import AgentTestSession, ScriptedClient, ScriptedResponse


def _two_mutation_client() -> ScriptedClient:
    # 한 assistant 메시지에 mutation 2개(병렬) — 둘 다 preview_only=true 로 preview 반환
    return ScriptedClient([
        ScriptedResponse(tool_calls=[
            {"name": "update_constraint", "args": {"field": "max_conseq_work", "value": 6, "preview_only": True}},
            {"name": "update_constraint", "args": {"field": "two_offs_after_two_nig", "value": 1, "preview_only": True}},
        ]),
    ])


def test_multi_mutation_yields_batch_preview(db, seed_data):
    sess = AgentTestSession(db, client=_two_mutation_client())
    res = sess.send("야간 최대 8회로 바꾸고 연속근무도 6일로 바꿔줘")

    assert res.awaiting_approval is True
    assert res.preview.get("type") == "batch", f"batch 아님: {res.preview}"
    assert res.preview.get("count") == 2
    assert len(res.preview.get("items", [])) == 2
    # 두 mutation 모두 담겼나
    fields = {it["args"].get("field") for it in res.preview["items"]}
    assert fields == {"max_conseq_work", "two_offs_after_two_nig"}


def test_batch_confirm_executes_all(db, seed_data):
    sess = AgentTestSession(db, client=_two_mutation_client())
    sess.send("야간 최대 8회로 바꾸고 연속근무도 6일로 바꿔줘")  # → batch preview
    res = sess.send("응")  # consolidated 승인

    assert res.awaiting_approval is False
    # batch 실행: 2건의 execution stage
    exec_stages = [s for s in res.trace if s.name == "execution"]
    assert len(exec_stages) == 2, f"2건 실행 아님: {[s.data.get('skill') for s in exec_stages]}"
    # 답변에 완료 건수 반영 (전부 성공 시 '2건', 부분 실패해도 실행은 2회 시도됨)
    assert ("2건" in res.answer) or ("완료" in res.answer) or ("실패" in res.answer)


def test_batch_denial_cancels(db, seed_data):
    sess = AgentTestSession(db, client=_two_mutation_client())
    sess.send("야간 최대 8회로 바꾸고 연속근무도 6일로 바꿔줘")
    res = sess.send("취소")

    assert res.awaiting_approval is False
    assert "취소" in res.answer
    # 실제 mutation 실행 안 됨 (execution stage 없음)
    assert not [s for s in res.trace if s.name == "execution"]


def test_single_mutation_keeps_legacy_shape(db, seed_data):
    # 단일 mutation 은 batch 가 아니라 기존 단일 preview shape 유지(하위호환)
    sess = AgentTestSession(db, client=ScriptedClient([
        ScriptedResponse(tool_calls=[
            {"name": "update_constraint", "args": {"field": "max_conseq_work", "value": 6, "preview_only": True}},
        ]),
    ]))
    res = sess.send("연속근무 6일로 바꿔줘")
    assert res.awaiting_approval is True
    assert res.preview.get("type") != "batch"
    assert res.preview.get("skill_name") == "update_constraint"
