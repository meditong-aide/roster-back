"""DAG clarify_form → 재개 라운드트립 (agent.run 진입점 + harness clarify 전파).

멀티턴 갭 보강:
  #1 _resume_clarify (agent_v3.py:465 진입점)가 agent.run 레벨에서 무테스트였음.
  #2 harness.send() 가 clarify_answers 를 전파 못 해 clarify 멀티턴을 구동조차 못 했음.

흐름: [턴1] 복합 plan 의 한 task 가 정보 부족 → clarify_form 방출 + ctx.pending_clarify 저장.
      [턴2] 프론트가 답을 채워 보냄(clarify_answers) → _resume_clarify → args 병합 후 재실행.

턴1의 planner LLM 왕복은 double 로 재현하기 번거로우므로, test_dag_atomic 이 pending_approval
을 pre-seed 하듯 여기서는 pending_clarify 를 pre-seed 해 **재개 경로**(미검증이던 부분)에
집중한다. 스킬/DB 는 실 seed_data 로 실제 실행된다.
"""
from __future__ import annotations

from agents_v2.llm_client import LLMResponse
from agents_v2.skills.registry import SKILL_REGISTRY
from tests.agent_qa.harness import AgentTestSession


class _TextLLM:
    """항상 텍스트로 답하는 stub — join_answer/judge 가 안전하게 통과(계획은 pre-seed)."""

    def chat(self, messages, tools, *, tool_choice="auto"):  # noqa: ANN001
        return LLMResponse(type="text", text="확인했습니다.")


def _seed_pending_clarify(sess: AgentTestSession) -> None:
    # 턴1 결과 시뮬레이션: 주말 데이 인원 task 가 counts 없이 왔다 → clarify 로 STOP,
    # pending_clarify 에 plan+원발화 저장(=_finalize_plan 이 실제로 하는 일).
    sess.ctx.pending_clarify = {
        "plan": {"tasks": [
            {"id": "t1", "skill": "manage_daily_shift", "kind": "mutate",
             "args": {"scope": "weekend"}},  # d_count 없음 → 되물음이었음
        ]},
        "user_message": "8월 주말 데이 몇 명으로 할까",
    }


def test_dag_clarify_resume_produces_preview(db, seed_data):
    # skill 미등록 환경이면 skip (레지스트리 의존)
    assert "manage_daily_shift" in SKILL_REGISTRY
    sess = AgentTestSession(db, month=8, dag=True, client=_TextLLM())
    _seed_pending_clarify(sess)

    # 턴2: 프론트가 "주말 데이 3명" 을 채워 보냄 → harness 가 clarify_answers 전파 → 재개
    res = sess.send("3명", clarify_answers=[{"task": "t1", "param": "d_count", "value": 3}])

    # 재개 경로 진입 확인: pending_clarify/answers 소진됨
    assert sess.ctx.pending_clarify is None, "pending_clarify 미소진 — 재개 진입 실패"
    assert sess.ctx.clarify_answers is None, "clarify_answers 미소진"
    # 답이 채워졌으니 이제 preview(승인대기)로 진행
    assert res.awaiting_approval is True, f"승인대기 아님: needs_clar={res.needs_clarification}"


def test_dag_clarify_reclarify_when_still_missing(db, seed_data):
    # 답을 안 주고(빈 answers) 다른 발화를 보내면 → line465 조건(answers 필요) 미충족.
    # 여기서는 '여전히 부족'을 검증: 매핑 안 되는 답(param=None)만 주면 재차 clarify.
    assert "manage_daily_shift" in SKILL_REGISTRY
    sess = AgentTestSession(db, month=8, dag=True, client=_TextLLM())
    _seed_pending_clarify(sess)

    # param=None(자유입력 fallback) → apply_clarify_answers 가 무시 → 여전히 counts 없음 → 재-clarify
    res = sess.send("음 글쎄", clarify_answers=[{"task": "t1", "param": None, "value": "글쎄"}])

    assert res.needs_clarification is True, "여전히 부족한데 재-clarify 안 함"
    # 재개 후에도 다음 라운드를 위해 pending_clarify 는 다시 세팅됨(계속 물어볼 수 있어야)
    assert sess.ctx.pending_clarify is not None, "재-clarify 라운드용 pending_clarify 미유지"
