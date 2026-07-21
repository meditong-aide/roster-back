"""Scheduling Agent v3 — Routine-aware hybrid tool-calling loop.

Architecture:
- Known complex patterns → Routine (structured step sequence, 96.3% accuracy)
- Novel/simple queries → Plain tool-calling loop (flexible LLM judgment)
- Variable Memory for inter-step parameter passing
- Middleware pipeline for permission, context injection, grounding

No framework dependency. ~100 lines of core logic.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from agents_v2.errors import (
    ErrorType,
    classify as _classify_outcome,
    extract_apply_hint_question as _extract_apply_hint_question,
    is_error as _is_error,
)
from agents_v2.harness.dev_query_log import log_dev_query
from agents_v2.harness.prompt_builder import build_system_prompt
from agents_v2.llm_client import LLMClient
from agents_v2.middleware import SkillResult, _check_permission, execute_skill
from agents_v2.router import route
from agents_v2.schemas.session_context import SessionContext
from agents_v2 import observability as obs
from agents_v2.usage import record_llm_usage
from agents_v2.skills.client_actions import build_ui_action, is_client_action
from agents_v2.skills.descriptions import SKILL_TOOLS
from agents_v2.variable_memory import VariableMemory
from services.memory.extractor import MemoryExtractor
from services.memory.user_repo import UserMemoryRepo

logger = logging.getLogger(__name__)

# US-A4: Tier-2 user memory injection 시 system prompt 에 들어가는 fact 갯수 상한.
# 토큰 절약 + LLM attention 산만 방지.
_MAX_INJECTED_FACTS = 20

# Preview confirmation: LLM 으로 자연어 요약을 생성할 때 끼우는 내부 지시.
# Layer C 의 보안 경계 안에 있으므로 untrusted_tool_output 안의 명령을 따르지 않음.
_PREVIEW_DIRECTIVE = (
    "[내부 지시] 직전 tool 결과는 mutation preview 입니다. "
    "어떤 변경이 일어날지 한국어로 한두 문장으로 자연스럽게 요약하고, "
    "마지막에 '진행하시겠습니까? (응 / 취소)' 형태로 확인을 요청하세요. "
    "추가 도구를 호출하지 말고 텍스트로만 응답하세요. "
    "JSON 또는 코드 블록은 출력하지 마세요. "
    "내부 식별자(config_id, nurse_id, entry_id, schedule_id, request_id, draft_id, group_id, office_id 등 *_id 필드와 DB row PK, UUID, job_id, 그리고 'acc_*' 같은 내부 코드)는 사용자에게 절대 노출하지 마세요. "
    "사람이 읽을 수 있는 정보(연/월/날짜, 간호사 이름, 시프트 명, 병동 명 등)만 사용해 요약하세요."
)
_PREVIEW_FALLBACK_ANSWER = "변경 미리보기를 확인해 주세요. 진행하시겠습니까? (응 / 취소)"


def _wrap_untrusted_tool_output(skill_name: str, payload: Any) -> str:
    """Tool 결과를 LLM 에 inject 할 때 <untrusted_tool_output> 으로 감싼다.

    payload 안에 prompt injection (예: nurse_memo, resignation_reason_memo 같은
    사용자 작성 필드)이 섞여 있어도 LLM 이 명령이 아닌 데이터로 다루도록 만든다.
    system prompt 의 보안 경계 안내문(SECURITY_BOUNDARY)과 짝을 이룬다.
    """
    body = json.dumps(payload, ensure_ascii=False, default=str)
    safe_skill = str(skill_name).replace("<", "&lt;").replace(">", "&gt;").replace('"', "")
    return (
        f'<untrusted_tool_output skill="{safe_skill}">\n'
        f"{body}\n"
        f"</untrusted_tool_output>"
    )


# ── Data classes ────────────────────────────────────────────


@dataclass
class Stage:
    """Single pipeline stage for debug trace."""

    name: str  # "planning", "execution", "answer", "clarification"
    status: str  # "ok", "error"
    data: dict = field(default_factory=dict)
    duration_ms: float = 0.0

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self.status,
            "data": self.data,
            "duration_ms": round(self.duration_ms, 1),
        }


@dataclass
class AgentResult:
    """What the agent returns to the caller."""

    answer: str = ""
    needs_clarification: bool = False
    question: str | None = None
    options: list[str] = field(default_factory=list)
    awaiting_approval: bool = False
    preview: dict | None = None
    ui_actions: list[dict] = field(default_factory=list)
    trace: list[Stage] = field(default_factory=list)
    messages: list[dict] = field(default_factory=list)
    variable_memory: dict = field(default_factory=dict)
    # B7: 인라인 렌더용 답형 조회결과. 마지막 성공 조회 스킬 결과를 누적해
    # ChatResponse.data 로 전달 (프론트가 카드/테이블로 시각화).
    data: dict | list | None = None

    def to_dict(self) -> dict:
        d: dict[str, Any] = {"answer": self.answer}
        if self.needs_clarification:
            d["needs_clarification"] = True
            d["clarification_question"] = self.question
            d["clarification_options"] = self.options
        if self.awaiting_approval:
            d["awaiting_approval"] = True
            d["preview"] = self.preview
        d["ui_actions"] = self.ui_actions
        d["pipeline_stages"] = [s.to_dict() for s in self.trace]
        d["total_time_ms"] = round(
            sum(s.duration_ms for s in self.trace), 1
        )
        # Debug: sanitized message chain (strip system prompt for brevity)
        d["message_chain"] = _sanitize_messages(self.messages)
        return d


# ── Agent ───────────────────────────────────────────────────


class SchedulingAgent:
    """Hybrid agent: Routine executor + plain tool-calling loop."""

    MAX_TURNS = 6

    def __init__(
        self,
        llm_client: LLMClient,
        memory_extractor: MemoryExtractor | None = None,
        enable_user_memory: bool = True,
        router_llm: LLMClient | None = None,
    ):
        """SchedulingAgent.

        Args:
            llm_client: 메인 turn LLM (도구 호출 + 응답 생성).
            memory_extractor: US-A4 Tier-2 fact extractor. 없으면 llm_client 재사용.
            enable_user_memory: False 면 inject/consolidate 모두 스킵 (테스트/긴급용).
            router_llm: 2단계 tool 스코핑용 분류 LLM. None 이면 라우팅 OFF(전체 tool,
                추가 LLM 호출 없음 — scripted 테스트 더블 시퀀스 보존). prod 에서만 주입.
        """
        self.llm = llm_client
        self.enable_user_memory = enable_user_memory
        if memory_extractor is None and enable_user_memory:
            memory_extractor = MemoryExtractor(llm_client)
        self.memory_extractor = memory_extractor
        self.router_llm = router_llm
        # DAG 계획(의존 복합) — opt-in. 기본 OFF → ReAct 무변경. env 로 켠다.
        self._dag_planning = os.getenv("AIDE_DAG_PLANNING", "").lower() in ("1", "true", "on")

    def run(
        self,
        db: Session,
        user_message: str,
        ctx: SessionContext,
    ) -> AgentResult:
        """Turn entry — inject_user_memory_context → _run_impl → consolidate_after_turn.

        턴 전체를 Langfuse trace 로 감싼다(키 있을 때만; 없으면 no-op). 내부 LLM 호출은
        generation, Stage 는 span, 결과 outcome 은 score 로 적재.
        """
        from agents_v2.errors import classify

        # US-A4 Tier-2 memory inject + run + consolidate 흐름은 _run_impl 이 핸들링.
        # consolidate 는 응답 반환 직전에 호출 (silent on failure).
        current_user_facts: list[dict] = (
            self._load_user_facts(db, ctx) if self.enable_user_memory else []
        )

        with obs.turn(conversation_id=getattr(ctx, "conversation_id", None),
                      user_id=getattr(ctx, "nurse_id", None),
                      group_id=getattr(ctx, "group_id", None),
                      user_message=user_message):
            result = self._run_impl(db, user_message, ctx, current_user_facts)

            # ── US-A4 consolidate_after_turn (turn 종료 후) ──
            if self.enable_user_memory and self.memory_extractor is not None:
                try:
                    self._consolidate_after_turn(
                        db, ctx, result.messages, current_user_facts
                    )
                except Exception as e:
                    # silent log — agent 응답에 영향 X
                    logger.warning(
                        "[agent_v3] consolidate_after_turn failed: %s", e
                    )

            try:
                obs.finish_turn(result.answer, result.trace,
                                score_name="outcome", score_value=classify(result.data).name)
            except Exception:  # noqa: BLE001
                pass
            return result

    # ── DAG 계획 (opt-in) ────────────────────────────────────
    def _plan_answer(self, user_message, run) -> str:
        """플랜 답변 합성 + L2 정합성 검증(답변↔task 출력). 어긋나면 데이터 근거로 1회 재생성."""
        from agents_v2.planning.orchestrate import join_answer
        from agents_v2.verify import judge_answer_consistency, l2_data_fits

        answer = join_answer(self.llm, user_message, run)
        data = run.exec.outputs
        if answer.strip() and data and l2_data_fits(data):
            cons = judge_answer_consistency(self.router_llm or self.llm, user_message, data, answer)
            if not cons.consistent:
                answer = join_answer(self.llm, user_message, run, correction=cons.reason) or answer
        return answer

    def _try_dag_plan(self, db, user_message, ctx, messages):
        """의존 복합이면 plan→execute. read-only 는 즉시 답변, mutate 는 승인 대기.
        plan 없음/실패면 None → 호출부가 ReAct 로 진행."""
        from agents_v2.middleware import execute_skill
        from agents_v2.planning.orchestrate import preview_fingerprint, try_plan_run
        from db.client2 import SessionLocal

        # 계획은 복합 의존 추론이라 **메인 LLM** 사용(nano 라우터는 과생성/오류).
        planner_llm = self.llm
        try:
            with obs.purpose("planner"):
                run = try_plan_run(db, user_message, ctx, planner_llm, SKILL_TOOLS, execute_skill,
                                   session_factory=SessionLocal)  # read 세션 격리 병렬
        except Exception as e:  # noqa: BLE001
            logger.warning("[agent_v3] DAG plan 실패 → ReAct: %s", e)
            return None
        if run is None or run.failed:
            return None
        trace = [Stage("plan", "ok",
                       {"tasks": [(t.id, t.skill, t.kind, t.deps) for t in run.plan.tasks]}, 0)]
        last = run.exec.outputs.get(run.plan.tasks[-1].id) if run.plan.tasks else None
        if run.needs_approval:
            ctx.pending_approval = {"type": "plan", "plan": run.plan.to_dict(),
                                    "user_message": user_message,
                                    "preview_fp": preview_fingerprint(run.exec.previews)}
            previews = run.exec.previews
            preview = ({"type": "batch", "count": len(previews), "items": previews}
                       if len(previews) > 1 else previews[0])
            answer = self._plan_answer(user_message, run)
            if run.exec.deferred:  # 승인 시 커밋 후 생성 등 async 시작 예정
                answer += " (승인하면 설정 반영 후 근무표 생성이 시작됩니다.)"
            answer += "\n\n진행할까요?"
            return AgentResult(awaiting_approval=True, preview=preview, answer=answer,
                               trace=trace, messages=messages,
                               variable_memory=ctx.variable_memory, data=last)
        # read-only(승인 불필요): 커밋할 mutation 이 없으니 deferred(async) 를 바로 실행.
        from agents_v2.planning.plan import resolve_args
        async_done: list[str] = []
        for t in run.exec.deferred:
            try:
                execute_skill(db, t.skill, resolve_args(t.args, run.exec.outputs), ctx)
                async_done.append(t.skill)
            except Exception as e:  # noqa: BLE001
                logger.warning("[agent_v3] deferred %s 실행 실패: %s", t.skill, e)
        answer = self._plan_answer(user_message, run)
        if async_done:
            answer += "\n\n(**근무표 생성을 시작**했습니다 — 완료까지 잠시 걸립니다.)"
        return AgentResult(answer=answer, trace=trace, messages=messages,
                           variable_memory=ctx.variable_memory, data=last)

    def _commit_plan(self, db, user_message, ctx, messages):
        """계획 승인 후 mutate 를 위상순서로 실제 commit(dry_run 해제) → 답변 합성."""
        from agents_v2.middleware import execute_skill
        from agents_v2.planning.executor import PlanExecResult, execute_plan
        from agents_v2.planning.orchestrate import PlanRun, preview_fingerprint
        from agents_v2.planning.plan import Plan, resolve_args

        pending = ctx.pending_approval or {}
        plan = Plan.from_dict(pending.get("plan") or {})
        orig = pending.get("user_message", user_message)
        approved_fp = pending.get("preview_fp")
        ctx.pending_approval = None

        # ── #5 staleness: 승인 시점 미리보기와 지금이 다르면 커밋 말고 재확인 ──
        # dry-run 을 flush+rollback 로 재실행(부수효과 없이) → 지문 비교.
        if approved_fp is not None:
            _rc = db.commit
            db.commit = db.flush  # type: ignore[method-assign]
            try:
                dry = execute_plan(db, plan, ctx, execute_skill, dry_run_mutations=True)
            except Exception:  # noqa: BLE001
                dry = None
            finally:
                db.commit = _rc  # type: ignore[method-assign]
                try:
                    db.rollback()  # dry-run 부수효과(get_or_init 등) 되돌림
                except Exception:  # noqa: BLE001
                    pass
            if dry is not None and not dry.failed and preview_fingerprint(dry.previews) != approved_fp:
                fresh_fp = preview_fingerprint(dry.previews)
                ctx.pending_approval = {"type": "plan", "plan": plan.to_dict(),
                                        "user_message": orig, "preview_fp": fresh_fp}
                previews = dry.previews
                preview = ({"type": "batch", "count": len(previews), "items": previews}
                           if len(previews) > 1 else (previews[0] if previews else None))
                return AgentResult(
                    awaiting_approval=True, preview=preview,
                    answer="승인 이후 상황이 바뀌어 미리보기가 달라졌습니다. 바뀐 내용으로 진행할까요?",
                    trace=[Stage("plan_staleness", "block", {}, 0)],
                    messages=messages, variable_memory=ctx.variable_memory)

        # ── 원자성: 플랜의 전 mutation 을 한 트랜잭션으로 ──
        # 스킬/서비스가 내부에서 부르는 db.commit() 을 flush() 로 리다이렉트해 트랜잭션을 유지.
        # 전부 성공 → 한 번에 commit / 하나라도 실패(read-back 위반 포함) → rollback(부분 반영 방지).
        # commit 단계는 순차·단일세션(원자성·일관성 우선; 병렬 read 는 dry-run 단계에서 이미 활용).
        _real_commit = db.commit
        db.commit = db.flush  # type: ignore[method-assign]
        try:
            exec_res = execute_plan(db, plan, ctx, execute_skill, dry_run_mutations=False)
        except Exception as e:  # noqa: BLE001
            exec_res = PlanExecResult(failed={"task": "plan", "skill": "", "data": {"error": str(e)}})
        finally:
            db.commit = _real_commit  # type: ignore[method-assign]

        if exec_res.failed:
            try:
                db.rollback()  # 전체 되돌림 — 부분 반영 없음
            except Exception:  # noqa: BLE001
                pass
            err = exec_res.failed.get("data") or {}
            reason = err.get("error") or err.get("question") or "알 수 없는 오류"
            return AgentResult(
                answer=f"실행 중 문제가 있어 **전체 취소(롤백)**했습니다 — 부분 반영 없음. ({reason})",
                trace=[Stage("plan_commit", "error",
                             {"order": exec_res.order, "atomic": "rolled_back"}, 0)],
                messages=messages, variable_memory=ctx.variable_memory)
        try:
            db.commit()  # 전 mutation 원자 커밋
        except Exception as e:  # noqa: BLE001
            db.rollback()
            return AgentResult(
                answer=f"커밋 중 오류로 **전체 취소**했습니다: {e}",
                trace=[Stage("plan_commit", "error", {"atomic": "rolled_back"}, 0)],
                messages=messages, variable_memory=ctx.variable_memory)
        # ── 커밋 성공 후: deferred(async, 예: 근무표 생성) 실행 ──
        # 커밋된 상태를 읽고 enqueue(트랜잭션 밖). 롤백됐으면 여기 안 옴 → 잘못된 설정으로 생성 안 됨.
        async_done: list[str] = []
        for t in exec_res.deferred:
            try:
                aargs = resolve_args(t.args, exec_res.outputs)
                execute_skill(db, t.skill, aargs, ctx)
                async_done.append(t.skill)
            except Exception as e:  # noqa: BLE001
                logger.warning("[agent_v3] deferred %s 실행 실패: %s", t.skill, e)

        run = PlanRun(plan=plan, exec=exec_res)
        last = exec_res.outputs.get(plan.tasks[-1].id) if plan.tasks else None
        answer = self._plan_answer(orig, run)
        if async_done:
            answer += "\n\n(설정을 반영하고 **근무표 생성을 시작**했습니다 — 완료까지 잠시 걸립니다.)"
        return AgentResult(
            answer=answer,
            trace=[Stage("plan_commit", "ok",
                         {"order": exec_res.order, "atomic": "committed", "async": async_done}, 0)],
            messages=messages, variable_memory=ctx.variable_memory, data=last)

    def _run_impl(
        self,
        db: Session,
        user_message: str,
        ctx: SessionContext,
        current_user_facts: list[dict],
    ) -> AgentResult:
        # ── Build system prompt (with domain knowledge + routines) ──
        system_prompt = build_system_prompt(ctx)

        # ── US-A4 inject_user_memory_context (turn 시작) ──
        # 현재 user_id+group_id 의 valid facts 를 system prompt 끝에 자연어로 주입.
        # 빈 list 면 주입 생략 (토큰 절약).
        if self.enable_user_memory:
            mem_block = self._format_memory_block(current_user_facts)
            if mem_block:
                system_prompt = f"{system_prompt}\n\n---\n\n{mem_block}"

        # ── Restore or initialize conversation ──
        # turn 2+ 에서도 system prompt 를 최신으로 유지한다 — 이전엔 첫 턴 system 이
        # ctx.messages[0] 으로 고정되어 user_memory / SECURITY_BOUNDARY / 날짜 갱신이
        # 반영되지 않았다. 매 턴 prepend/replace.
        fresh_system = {"role": "system", "content": system_prompt}
        if ctx.messages:
            history = list(ctx.messages)
            if history and history[0].get("role") == "system":
                history[0] = fresh_system
            else:
                history.insert(0, fresh_system)
            messages = history + [
                {"role": "user", "content": user_message}
            ]
        else:
            messages = [
                fresh_system,
                {"role": "user", "content": user_message},
            ]

        # ── Handle pending approval ──
        if ctx.pending_approval:
            ptype = ctx.pending_approval.get("type")
            if ptype == "plan":
                # DAG 계획 승인 라운드트립: 확인이면 mutate 를 위상순서로 실제 commit.
                if _is_confirmation(user_message):
                    return self._commit_plan(db, user_message, ctx, messages)
                if _is_denial(user_message):
                    ctx.pending_approval = None
                    return AgentResult(answer="계획을 취소했습니다. 다음에 무엇을 할까요?",
                                       messages=messages, variable_memory=ctx.variable_memory)
            if ptype == "apply_hint":
                # apply_hint 재시도 흐름: 확인/거부/모호 분기
                if _is_denial(user_message):
                    return AgentResult(
                        answer="취소했습니다. 다음에 어떤 행동을 원하시나요?",
                        trace=[],
                        messages=messages,
                        variable_memory=ctx.variable_memory,
                    )
                if _is_confirmation(user_message):
                    return self._execute_apply_hint(db, ctx, messages)
                # 모호한 입력 → 재질의 (pending_approval 유지)
                return AgentResult(
                    awaiting_approval=True,
                    preview=ctx.pending_approval,
                    answer=ctx.pending_approval.get("question", "재시도하시겠습니까?"),
                    trace=[],
                    messages=messages,
                    variable_memory=ctx.variable_memory,
                )
            elif _is_confirmation(user_message):
                return self._execute_approval(db, ctx, messages)
            elif _is_denial(user_message):
                return AgentResult(
                    answer="취소했습니다. 다음에 어떤 행동을 원하시나요?",
                    trace=[],
                    messages=messages,
                    variable_memory=ctx.variable_memory,
                )

        # ── DAG 계획 (opt-in, 의존 복합) — plan 없음/실패면 아래 ReAct 로 fallthrough ──
        if self._dag_planning and not ctx.pending_approval:
            planned = self._try_dag_plan(db, user_message, ctx, messages)
            if planned is not None:
                return planned

        tools = SKILL_TOOLS
        trace: list[Stage] = []
        vm = VariableMemory()
        ui_actions: list[dict] = []
        _prev_calls: set[str] = set()
        _failed_shift_terms: list[str] = []
        # B7: 마지막 성공 조회 결과 — 인라인 렌더용 ChatResponse.data 채움.
        last_query_data: dict | list | None = None
        # 이번 턴의 모든 OK 조회 결과 — L2 는 (마지막 하나가 아니라) 턴 전체 데이터로 대조해야
        # 복합쿼리("A랑 B 각각")에서 오탐이 안 난다.
        turn_query_data: list = []

        # Restore VM from previous turns
        if ctx.variable_memory:
            vm._store.update(ctx.variable_memory)

        # ── US-R2: Router (2단계 tool 스코핑) — main path only ──
        # 질의를 LLM이 카테고리로 분류 → scoped tool subset 으로 (a) system prompt 의
        # '## 사용 가능한 도구' 섹션, (b) chat(tools=) 둘 다 스코핑. fallback(분류 실패/
        # 저신뢰)이면 전체 tool 유지(턴 안 깨짐). pending_approval 턴은 tools= 를 안 쓰므로 스킵.
        if self.router_llm is not None and not ctx.pending_approval:
            t_route = time.time()
            with obs.purpose("router"):
                router_result = route(self.router_llm, user_message)
            route_ms = (time.time() - t_route) * 1000
            log_dev_query(
                router_result.categories, user_message, ctx.conversation_id
            )
            if not router_result.fallback_used:
                allowed = router_result.tool_names
                scoped_prompt = build_system_prompt(ctx, allowed_tools=allowed)
                if self.enable_user_memory:
                    mb = self._format_memory_block(current_user_facts)
                    if mb:
                        scoped_prompt = f"{scoped_prompt}\n\n---\n\n{mb}"
                if messages and messages[0].get("role") == "system":
                    messages[0] = {"role": "system", "content": scoped_prompt}
                allowed_set = set(allowed)
                tools = [t for t in SKILL_TOOLS if t["name"] in allowed_set]
            trace.append(
                Stage("routing", "ok", router_result.to_dict(), route_ms)
            )
            record_llm_usage(
                db, conversation_id=ctx.conversation_id, group_id=ctx.group_id,
                user_id=ctx.nurse_id, model=router_result.model, purpose="router",
                input_tokens=router_result.input_tokens,
                output_tokens=router_result.output_tokens,
            )

        for turn in range(self.MAX_TURNS):
            # ── LLM call ──
            # Truncate inject messages for token budget — 영구 messages 변수는 전체 보존.
            # MSSQL/Redis save_messages 에는 전체 messages 가 저장되어 감사 trail 손실 없음.
            inject_messages = _truncate_for_llm(messages, max_chars=_MAX_INJECT_CHARS)
            t0 = time.time()
            with obs.purpose("turn"):
                response = self.llm.chat(inject_messages, tools=tools)
            llm_ms = (time.time() - t0) * 1000

            record_llm_usage(
                db, conversation_id=ctx.conversation_id, group_id=ctx.group_id,
                user_id=ctx.nurse_id, model=response.model, purpose="turn",
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
            )

            # ── Text response → final answer ──
            if response.is_text:
                answer_text = response.text or ""
                # ── L2 answer-consistency: 조회 데이터가 있으면 답변↔데이터 정합성 검증 ──
                # (read 답변의 환각 방지. mutation "완료"류는 조회 데이터 없어 skip → L1 담당)
                #
                # 정합성 최우선(오탐 금지):
                #  - 턴 '전체' 조회 데이터로 대조(복합쿼리 오탐 방지). 마지막 하나만 보면
                #    "A랑 B" 답변에서 A 를 근거없다고 오탐한다.
                #  - 데이터가 크면(잘림 불가피) judge 가 '부분만 보고' 오탐하므로 skip.
                #    대용량 조회는 L2 미적용(recall↓ 감수, false-positive 0 우선).
                from agents_v2.verify import judge_answer_consistency, l2_data_fits

                _judge_data = (turn_query_data[0] if len(turn_query_data) == 1
                               else turn_query_data)
                if turn_query_data and answer_text.strip() and l2_data_fits(_judge_data):
                    judge_llm = self.router_llm or self.llm
                    cons = judge_answer_consistency(
                        judge_llm, user_message, _judge_data, answer_text
                    )
                    trace.append(Stage(
                        "answer_consistency",
                        "ok" if cons.consistent else "block",
                        {"reason": cons.reason}, 0,
                    ))
                    if not cons.consistent:
                        # LLM-Modulo 루프: 어긋나면 데이터에만 근거해 1회 재생성.
                        fix = inject_messages + [{
                            "role": "system",
                            "content": (
                                f"[검증] 직전 답변이 도구 데이터와 어긋난다: {cons.reason}. "
                                "도구가 반환한 데이터에만 근거해 다시 답하라. "
                                "데이터에 없는 수치·이름·상태를 지어내지 마라."
                            ),
                        }]
                        try:
                            regen = self.llm.chat(fix, tools=[])
                            if regen.is_text and (regen.text or "").strip():
                                answer_text = regen.text
                                trace.append(Stage(
                                    "answer_regenerated", "ok", {"text": answer_text}, 0
                                ))
                        except Exception:  # noqa: BLE001
                            pass
                trace.append(Stage("answer", "ok", {"text": answer_text}, llm_ms))
                return AgentResult(
                    answer=answer_text,
                    ui_actions=ui_actions,
                    trace=trace,
                    messages=messages,
                    variable_memory=vm.to_dict(),
                    data=last_query_data,
                )

            # ── Tool call(s) → middleware pipeline ──
            if response.is_tool_call:
                # Append the single assistant message (may contain N tool_calls)
                messages.append(response.as_assistant_message())

                # 병렬 tool_call 배치 중 preview/apply_hint 가 나와도 즉시 return 하지 않고
                # 여기 capture 해둔다. 그 자리에서 return 하면 같은 assistant 메시지의 나머지
                # tool_call 이 tool 응답 없이 남아 메시지 체인이 무효화된다(OpenAI 400).
                # 배치를 끝까지 실행해 모든 tool_call 응답을 채운 뒤(체인 완결) return 한다.
                pending_previews: list[dict] = []
                pending_apply_hint: dict | None = None

                # Execute each tool call (parallel calls executed sequentially)
                for tc in response.tool_calls:
                    skill_name = tc.name
                    skill_args = tc.args or {}

                    # Variable Memory injection ($vm.{key} → actual value)
                    skill_args = vm.inject(skill_args)

                    # ── Duplicate call detection ──
                    call_sig = _call_signature(skill_name, skill_args)
                    if call_sig in _prev_calls:
                        logger.warning("Duplicate tool call detected: %s — forcing answer generation", skill_name)
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.call_id,
                            "content": json.dumps({
                                "error": "DUPLICATE_CALL",
                                "message": "You already called this tool with identical arguments. Use the previous result to answer the user's question.",
                            }),
                        })
                        continue
                    _prev_calls.add(call_sig)

                    trace.append(
                        Stage(
                            "planning",
                            "ok",
                            {
                                "skill": skill_name,
                                "args": skill_args,
                                "reasoning": response.thinking,
                                "parallel": response.is_parallel,
                            },
                            llm_ms if tc is response.tool_calls[0] else 0,
                        )
                    )

                    # ── Client-action tool (navigate/prefill) — 프론트 위임, 서버 실행 X ──
                    if is_client_action(skill_name):
                        perm_err = _check_permission(skill_name, skill_args, ctx)
                        ui_action = None
                        ca_err = perm_err
                        if not perm_err:
                            ui_action, ca_err = build_ui_action(skill_name, skill_args)
                        if ca_err:
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc.call_id,
                                "content": json.dumps(
                                    {"error": ca_err, **({"permission_denied": True} if perm_err else {})},
                                    ensure_ascii=False,
                                ),
                            })
                            trace.append(Stage(
                                "execution", "error",
                                {"skill": skill_name, "result": {"error": ca_err}}, 0.0,
                            ))
                            continue
                        ui_actions.append(ui_action)
                        vm.store(skill_name, {"dispatched": True, "action": ui_action})
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.call_id,
                            "content": json.dumps(
                                {"dispatched": True, "action": ui_action}, ensure_ascii=False
                            ),
                        })
                        trace.append(Stage(
                            "execution", "ok",
                            {"skill": skill_name, "result": {"ui_action": ui_action}}, 0.0,
                        ))
                        continue

                    # ── Execute via middleware ──
                    result = execute_skill(db, skill_name, skill_args, ctx)

                    trace.append(
                        Stage(
                            "execution",
                            "error" if _is_error(result.data) else "ok",
                            {
                                "skill": skill_name,
                                "result": _truncate(result.data),
                                "grounded_params": result.grounded_params,
                                "middleware_steps": [
                                    s.to_dict() for s in result.middleware_steps
                                ],
                            },
                            result.duration_ms,
                        )
                    )

                    # Store result in Variable Memory
                    vm.store(skill_name, result.data)

                    # B7: 인라인 렌더용 데이터 누적 — 마지막 성공 조회 결과가 이긴다.
                    # error / needs_clarification / preview 는 데이터 의미가 없어 제외.
                    # §3.C: 3개 판별식을 outcome taxonomy 단일 분류(OK)로 통합.
                    if (
                        result.data is not None
                        and _classify_outcome(result.data) is ErrorType.OK
                    ):
                        last_query_data = result.data
                        turn_query_data.append(result.data)

                    # ── Auto-learn abbreviation tracking ──
                    _track_shift_learning(
                        skill_args, result, _failed_shift_terms,
                    )

                    # ── apply_hint 흐름 — capture, don't return (배치 완결 후 처리) ──
                    apply_hint_q = _extract_apply_hint_question(skill_name, result.data)
                    if apply_hint_q is not None:
                        if pending_apply_hint is None:
                            pending_apply_hint = {
                                "type": "apply_hint",
                                "apply_hint": result.data["infeasibility"]["apply_hint"],
                                "original_args": skill_args,
                                "question": apply_hint_q,
                            }
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.call_id,
                            "content": _wrap_untrusted_tool_output(skill_name, result.data),
                        })
                        continue

                    # ── Approval flow (preview) — capture all, don't return (배치 완결 후 처리) ──
                    # §3.C: outcome taxonomy 단일 분류로 dispatch. consolidated 승인을 위해
                    # 배치의 모든 mutation preview 를 모은다(다중 mutation 도 1회 승인으로).
                    if _classify_outcome(result.data) is ErrorType.PREVIEW:
                        pending_previews.append({
                            **result.data,
                            "skill_name": skill_name,
                            "args": skill_args,
                        })
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.call_id,
                            "content": _wrap_untrusted_tool_output(skill_name, result.data),
                        })
                        continue

                    # ── Append tool result for LLM to observe ──
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.call_id,
                            "content": _wrap_untrusted_tool_output(skill_name, result.data),
                        }
                    )

                # ── 배치 완결 후: pending 승인 처리 (메시지 체인 무결성 보존) ──
                # 모든 tool_call 이 응답을 가진 상태이므로 _generate_preview_answer 의
                # LLM 호출이 400 나지 않는다. apply_hint 우선(생성 재시도).
                if pending_apply_hint is not None:
                    return AgentResult(
                        awaiting_approval=True,
                        preview=pending_apply_hint,
                        answer=pending_apply_hint["question"],
                        ui_actions=ui_actions,
                        trace=trace,
                        messages=messages,
                        variable_memory=vm.to_dict(),
                    )
                # ── autonomy_mode "auto": auto_safe mutation 은 승인 없이 즉시 commit ──
                # manual(기본)이면 no-op. auto 여도 auto_safe=True 스킬만 자동(안전 opt-in).
                auto_committed: list[tuple[str, Any]] = []
                if pending_previews and getattr(ctx, "autonomy_mode", "manual") == "auto":
                    from agents_v2.skills.manifest import SKILL_SPECS, load_manifest_skills

                    load_manifest_skills()
                    still_pending: list[dict] = []
                    for p in pending_previews:
                        spec = SKILL_SPECS.get(str(p.get("skill_name", "")).replace("-", "_"))
                        if spec is not None and spec.auto_safe:
                            r = execute_skill(
                                db, p["skill_name"],
                                {**p.get("args", {}), "preview_only": False}, ctx,
                            )
                            trace.append(Stage(
                                "auto_execution",
                                "error" if _is_error(r.data) else "ok",
                                {"skill": p["skill_name"], "result": _truncate(r.data)},
                                r.duration_ms,
                            ))
                            auto_committed.append((p["skill_name"], r.data))
                        else:
                            still_pending.append(p)
                    pending_previews = still_pending

                if pending_previews:
                    # 단일이면 기존 shape 유지(하위호환). 다중이면 consolidated batch.
                    if len(pending_previews) == 1:
                        preview_payload = pending_previews[0]
                    else:
                        preview_payload = {
                            "type": "batch",
                            "count": len(pending_previews),
                            "items": pending_previews,
                        }
                    preview_answer = self._generate_preview_answer(messages, trace)
                    if auto_committed:
                        ok_n = sum(1 for _, d in auto_committed if not _is_error(d))
                        preview_answer = (
                            f"{ok_n}건은 자동 실행했습니다. 나머지는 확인이 필요합니다.\n\n"
                            + preview_answer
                        )
                    return AgentResult(
                        awaiting_approval=True,
                        preview=preview_payload,
                        answer=preview_answer,
                        ui_actions=ui_actions,
                        trace=trace,
                        messages=messages,
                        variable_memory=vm.to_dict(),
                    )

                if auto_committed:
                    # 전부 auto_safe → 승인 없이 실행 완료 (text 답변)
                    ok_n = sum(1 for _, d in auto_committed if not _is_error(d))
                    err_n = len(auto_committed) - ok_n
                    ans = f"{ok_n}건의 변경을 자동 실행했습니다."
                    if err_n:
                        ans += f" ({err_n}건 실패)"
                    return AgentResult(
                        answer=ans,
                        ui_actions=ui_actions,
                        trace=trace,
                        messages=messages,
                        variable_memory=vm.to_dict(),
                        data=last_query_data,
                    )

        return AgentResult(
            answer="처리 단계가 너무 많습니다. 좀 더 구체적으로 말씀해 주세요.",
            ui_actions=ui_actions,
            trace=trace,
            messages=messages,
            variable_memory=vm.to_dict(),
        )

    # ── US-A4: Tier-2 user memory helpers ─────────────────────────

    @staticmethod
    def _resolve_memory_user_id(ctx: SessionContext) -> str | None:
        """SessionContext → user_id (nurse_id 기반). 없으면 None.

        AgentUserMemory.user_id NOT NULL 이므로 None 이면 memory hooks 모두 스킵.
        """
        return getattr(ctx, "nurse_id", None)

    _user_memory_sot_disabled = False  # process-wide once-set flag

    def _load_user_facts(
        self,
        db: Session,
        ctx: SessionContext,
    ) -> list[dict]:
        """현재 user_id+group_id 의 valid facts 조회. 실패 시 빈 list.

        agent_user_memory 테이블 부재 시 한 번만 warning 로그하고 이후 silent skip.
        """
        user_id = self._resolve_memory_user_id(ctx)
        if not user_id or not ctx.group_id:
            return []
        if SchedulingAgent._user_memory_sot_disabled:
            return []
        try:
            repo = UserMemoryRepo(db)
            return repo.query_valid_facts(user_id=user_id, group_id=ctx.group_id)
        except Exception as e:
            msg = str(e).lower()
            is_missing = any(
                p in msg
                for p in ("invalid object name", "does not exist", "no such table")
            )
            if is_missing:
                try:
                    db.rollback()
                except Exception:  # noqa: BLE001
                    pass
                SchedulingAgent._user_memory_sot_disabled = True
                logger.warning(
                    "[agent_v3] agent_user_memory 테이블이 운영 DB 에 없습니다 — "
                    "migrations/2026_05_19_add_agent_memory_tables.sql 적용 필요. "
                    "장기 사용자 메모리 비활성화 (이후 silent skip)."
                )
            else:
                logger.warning("[agent_v3] _load_user_facts failed: %s", e)
            return []

    @staticmethod
    def _format_memory_block(facts: list[dict]) -> str:
        """facts → system prompt 에 붙일 <user_memory> 블록. 빈 list 면 빈 문자열.

        fact_text 는 사용자 발화에서 추출된 데이터이므로 prompt injection 매개체가
        될 수 있다. 외곽을 <user_memory> 태그로 감싸고 fact_text 안의 태그 시작
        문자(<, >)는 escape 해서 LLM 이 명령으로 해석하지 않도록 만든다.
        system prompt 의 보안 경계 안내문(SECURITY_BOUNDARY)과 짝을 이룬다.
        """
        if not facts:
            return ""
        limited = facts[-_MAX_INJECTED_FACTS:] if len(facts) > _MAX_INJECTED_FACTS else facts

        def _escape(s: Any) -> str:
            return str(s).replace("<", "&lt;").replace(">", "&gt;")

        lines = ["## 사용자에 대해 알고 있는 사실 (장기 기억)", "<user_memory>"]
        for f in limited:
            fact_type = _escape(f.get("fact_type", "?"))
            fact_text = _escape(f.get("fact_text", "?"))
            lines.append(f"- [{fact_type}] {fact_text}")
        lines.append("</user_memory>")
        return "\n".join(lines)

    def _consolidate_after_turn(
        self,
        db: Session,
        ctx: SessionContext,
        messages: list[dict],
        existing_facts: list[dict],
    ) -> None:
        """Turn 종료 후 MemoryExtractor 호출 → apply_fact 순회.

        실패 시 silent log (호출자가 try/except 로 감쌈).
        SOT 비활성 (테이블 부재) 시 skip.
        """
        user_id = self._resolve_memory_user_id(ctx)
        if not user_id or not ctx.group_id:
            return
        if self.memory_extractor is None:
            return
        if SchedulingAgent._user_memory_sot_disabled:
            return

        # 이번 turn 의 user/assistant/tool 메시지만 추출 (system 제외)
        recent = [m for m in messages if m.get("role") != "system"]
        if not recent:
            return

        decisions = self.memory_extractor.extract_facts(
            messages=recent,
            existing_facts=existing_facts,
        )

        # B6: memory consolidate LLM 호출 사용량 회계. decisions 가 비어도(=NOOP)
        # 토큰은 소비됐으므로 항상 기록.
        in_tok, out_tok, ext_model = getattr(
            self.memory_extractor, "last_usage", (0, 0, None)
        )
        if in_tok or out_tok:
            try:
                record_llm_usage(
                    db,
                    conversation_id=getattr(ctx, "conversation_id", None),
                    group_id=ctx.group_id,
                    user_id=user_id,
                    model=ext_model,
                    purpose="memory_consolidate",
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                )
            except Exception as e:
                logger.warning("[agent_v3] memory usage record failed: %s", e)

        if not decisions:
            return

        repo = UserMemoryRepo(db)
        session_id = getattr(ctx, "conversation_id", None)
        for d in decisions:
            action = d["action"]
            fact_payload = {
                "user_id": user_id,
                "group_id": ctx.group_id,
                "fact_type": d["fact_type"],
                "fact_text": d["fact_text"],
                "source": d["source"],
                "confidence": d.get("confidence", 1.0),
                "evidence_session_id": session_id,
            }
            try:
                repo.apply_fact(action, fact_payload)
            except Exception as e:
                logger.warning(
                    "[agent_v3] apply_fact failed action=%s fact_type=%s: %s",
                    action, d.get("fact_type"), e,
                )

        # commit — UserMemoryRepo 는 flush 만 호출하므로 turn 단위 commit 필요
        try:
            db.commit()
        except Exception as e:
            logger.warning("[agent_v3] consolidate commit failed: %s", e)
            db.rollback()

    def _generate_preview_answer(
        self,
        messages: list[dict],
        trace: list[Stage],
    ) -> str:
        """Preview tool 결과를 LLM 으로 한 번 더 요약해 자연어 confirmation 답변을 만든다.

        tools=[] 로 호출해 추가 tool_call 을 차단하고 텍스트만 받는다.
        실패·빈 응답 시 hardcoded fallback 으로 graceful degrade.
        """
        directive_msg = {"role": "user", "content": _PREVIEW_DIRECTIVE}
        summary_messages = list(messages) + [directive_msg]
        inject_messages = _truncate_for_llm(summary_messages, max_chars=_MAX_INJECT_CHARS)
        try:
            t0 = time.time()
            response = self.llm.chat(inject_messages, tools=[])
            llm_ms = (time.time() - t0) * 1000
            text = (response.text or "").strip() if response.is_text else ""
            if text:
                trace.append(
                    Stage("preview_summary", "ok", {"text": text[:200]}, llm_ms)
                )
                return text
            trace.append(
                Stage(
                    "preview_summary",
                    "fallback",
                    {"reason": "tool_call_or_empty"},
                    llm_ms,
                )
            )
        except Exception as exc:
            logger.warning("Preview answer generation failed: %s", exc)
            trace.append(
                Stage(
                    "preview_summary",
                    "error",
                    {"error": str(exc)[:200]},
                    0.0,
                )
            )
        return _PREVIEW_FALLBACK_ANSWER

    def _execute_apply_hint(
        self,
        db: Session,
        ctx: SessionContext,
        messages: list[dict],
    ) -> AgentResult:
        """apply_hint를 constraint_adjustments에 적용하고 generate_schedule 재호출."""
        approval = ctx.pending_approval
        if not approval:
            return AgentResult(answer="재시도할 apply_hint가 없습니다.", messages=messages)

        hint = approval.get("apply_hint") or {}
        original_args = approval.get("original_args") or {}

        # config_overrides → generate_schedule 의 constraint_adjustments 로 전달
        config_overrides = hint.get("config_overrides") or {}

        new_args = {
            **original_args,
            "preview_only": False,
            "constraint_adjustments": config_overrides,
        }

        trace: list[Stage] = []
        result = execute_skill(db, "generate_schedule", new_args, ctx)

        trace.append(
            Stage(
                "execution",
                "error" if _is_error(result.data) else "ok",
                {"skill": "generate_schedule", "result": _truncate(result.data)},
                result.duration_ms,
            )
        )

        if _is_error(result.data):
            return AgentResult(
                answer=f"재시도 중 오류가 발생했습니다: {result.data.get('error', '')}",
                trace=trace,
                messages=messages,
            )

        return AgentResult(
            answer="제약 조건을 조정하여 근무표 생성을 재시도했습니다.",
            trace=trace,
            messages=messages,
            variable_memory=ctx.variable_memory,
        )

    def _execute_approval(
        self,
        db: Session,
        ctx: SessionContext,
        messages: list[dict],
    ) -> AgentResult:
        """Execute a previously previewed mutation after user confirmation.

        Proactive behavior: auto-validates schedule after mutation to catch
        any new constraint violations introduced by the change.
        """
        approval = ctx.pending_approval
        if not approval:
            return AgentResult(answer="승인 대기 중인 작업이 없습니다.", messages=messages)

        # consolidated batch 승인 — 여러 mutation 을 한 번에 실행 (multi-mutation 복합 쿼리).
        if approval.get("type") == "batch":
            return self._execute_approval_batch(db, ctx, messages, approval.get("items", []))

        # Re-execute with preview_only=false
        args = {**approval.get("args", {}), "preview_only": False}
        skill_name = approval.get("skill_name", "bulk_mutation")

        trace: list[Stage] = []
        result = execute_skill(db, skill_name, args, ctx)

        trace.append(
            Stage(
                "execution",
                "error" if _is_error(result.data) else "ok",
                {"skill": skill_name, "result": _truncate(result.data)},
                result.duration_ms,
            )
        )

        if _is_error(result.data):
            return AgentResult(
                answer=f"실행 중 오류가 발생했습니다: {result.data.get('error', '')}",
                trace=trace,
                messages=messages,
            )

        # ── Proactive post-mutation validation ──
        answer = "변경이 완료되었습니다."
        if skill_name in ("bulk_mutation", "bulk-mutation") and args.get("scope") in (
            "schedule", "draft_schedule", "published_schedule",
        ):
            val_result = execute_skill(
                db, "validate_schedule",
                {"group_id": ctx.group_id, "year": ctx.year, "month": ctx.month},
                ctx,
            )
            trace.append(
                Stage(
                    "auto_validation",
                    "error" if _is_error(val_result.data) else "ok",
                    {"result": _truncate(val_result.data)},
                    val_result.duration_ms,
                )
            )
            if not _is_error(val_result.data):
                v_count = val_result.data.get("violation_count", 0)
                if v_count > 0:
                    answer += f"\n\n⚠️ 자동 검증 결과: {v_count}건의 제약조건 위반이 감지되었습니다. '위반사항 보여줘'로 상세 내역을 확인하세요."
                else:
                    answer += "\n\n✅ 자동 검증 완료: 제약조건 위반 없음."

        return AgentResult(
            answer=answer,
            trace=trace,
            messages=messages,
        )

    def _execute_approval_batch(
        self,
        db: Session,
        ctx: SessionContext,
        messages: list[dict],
        items: list[dict],
    ) -> AgentResult:
        """Consolidated 승인 — 여러 mutation preview 를 순차 실행(각각 preview_only=False).

        복합 쿼리에서 한 턴에 여러 변경이 preview 됐을 때, 사용자 1회 승인으로 전부 실행.
        schedule mutation 이 하나라도 있으면 끝에 proactive 검증 1회.
        """
        trace: list[Stage] = []
        done: list[str] = []
        errors: list[str] = []
        needs_validate = False

        for item in items:
            skill_name = item.get("skill_name", "bulk_mutation")
            args = {**item.get("args", {}), "preview_only": False}
            result = execute_skill(db, skill_name, args, ctx)
            trace.append(
                Stage(
                    "execution",
                    "error" if _is_error(result.data) else "ok",
                    {"skill": skill_name, "result": _truncate(result.data)},
                    result.duration_ms,
                )
            )
            if _is_error(result.data):
                errors.append(f"{skill_name}: {result.data.get('error', '')}")
            else:
                done.append(skill_name)
                if skill_name in ("bulk_mutation", "bulk-mutation") and args.get(
                    "scope"
                ) in ("schedule", "draft_schedule", "published_schedule"):
                    needs_validate = True

        parts = []
        if done:
            parts.append(f"{len(done)}건의 변경을 완료했습니다.")
        if errors:
            parts.append("일부 변경은 실패했습니다: " + "; ".join(errors))
        answer = " ".join(parts) or "실행할 변경이 없습니다."

        if needs_validate:
            val = execute_skill(
                db, "validate_schedule",
                {"group_id": ctx.group_id, "year": ctx.year, "month": ctx.month},
                ctx,
            )
            trace.append(
                Stage(
                    "auto_validation",
                    "error" if _is_error(val.data) else "ok",
                    {"result": _truncate(val.data)},
                    val.duration_ms,
                )
            )
            if not _is_error(val.data):
                v_count = val.data.get("violation_count", 0)
                answer += (
                    f"\n\n⚠️ 자동 검증: {v_count}건의 제약조건 위반이 감지되었습니다."
                    if v_count > 0
                    else "\n\n✅ 자동 검증 완료: 제약조건 위반 없음."
                )

        return AgentResult(answer=answer, trace=trace, messages=messages)


# ── Helpers ─────────────────────────────────────────────────

_CONFIRM_WORDS = frozenset({
    "응",
    "네",
    "예",
    "ㅇㅇ",
    "확인",
    "진행",
    "실행",
    "해줘",
    "좋아",
    "ok",
    "yes",
    "진행해",
    "실행해",
    "변경해",
    "확인했어",
    # 2026-06-01 추가: 일상 발화에서 자주 쓰는 컨펌. "그래"가 누락돼 LLM 추론에
    # 의존했고, 일관성 낮은 처리가 발견됨(S5.B).
    "그래",
    "그렇게",
    "그렇게 해",
    "그렇게해",
    "맞아",
    "맞아요",
    "맞습니다",
    "오케이",
    "okay",
    "응응",
    "yep",
    "y",
    # B9 (2026-06-12): apply_hint 컨텍스트에서 제안된 옵션 적용을 표하는 어휘.
    "적용",
    "적용해",
    "적용해줘",
})


_DENY_WORDS = frozenset({
    "아니오",
    "아니요",
    "아니",
    "no",
    "취소",
    "취소해",
    "취소해줘",
    "하지마",
    "그만",
    "됐어",
    "괜찮아",
    "skip",
    "건너뛰기",
    # B9 (2026-06-12): apply_hint 컨텍스트에서 옵션 거부를 표하는 어휘.
    "싫어",
    "말고",
    "원래대로",
})


_CONFIRM_MAX_LEN = 20  # confirmation 으로 보려는 메시지의 최대 길이 (짧은 ack 가정)


def _is_denial(msg: str) -> bool:
    """사용자 메시지가 거부/취소 의사인지 확인.

    Substring 매칭을 폐기 — '예전에는' 같은 정상 발화가 'yes'/'예'로 잘못 매칭되던
    문제를 막는다. exact match 또는 짧은 문장에서 단어 경계 매칭만 허용.
    """
    cleaned = msg.strip().lower()
    if not cleaned:
        return False
    if cleaned in _DENY_WORDS:
        return True
    # 짧은 메시지(≤ _CONFIRM_MAX_LEN)에서만 토큰 분할 매칭 허용
    if len(cleaned) > _CONFIRM_MAX_LEN:
        return False
    import re as _re

    tokens = [t for t in _re.split(r"[\s.,!?~…]+", cleaned) if t]
    return any(t in _DENY_WORDS for t in tokens)


def _call_signature(skill_name: str, args: dict) -> str:
    """Deterministic signature for duplicate call detection.

    Uses full args (sorted, stringified) so different nurse_name/date
    with same scope won't collide, while truly identical calls are caught.
    """
    normalized = json.dumps(args, sort_keys=True, default=str)
    return f"{skill_name}:{normalized}"


def _is_confirmation(msg: str) -> bool:
    """Check if user message is confirming a pending approval.

    Substring 매칭을 폐기 — '예전' 같은 정상 발화가 잘못 confirm 되던 문제를 막는다.
    exact match 또는 짧은 문장(≤ _CONFIRM_MAX_LEN)에서 단어 경계 매칭만 허용.
    """
    cleaned = msg.strip().lower()
    if not cleaned:
        return False
    if cleaned in _CONFIRM_WORDS:
        return True
    if len(cleaned) > _CONFIRM_MAX_LEN:
        return False
    import re as _re

    tokens = [t for t in _re.split(r"[\s.,!?~…]+", cleaned) if t]
    return any(t in _CONFIRM_WORDS for t in tokens)


def _truncate(data: Any, max_len: int = 500) -> Any:
    """Truncate data for trace display."""
    s = json.dumps(data, ensure_ascii=False, default=str)
    if len(s) > max_len:
        return s[:max_len] + "…"
    return data


def _track_shift_learning(
    args: dict,
    result: SkillResult,
    failed_terms: list[str],
) -> None:
    """Track shift grounding failures/successes for auto-learning.

    When grounding fails for a shift_name, record the failed term.
    When a subsequent call succeeds with a different shift_name,
    learn the mapping: failed_term → successful_term.
    """
    shift_name = args.get("shift_name") or args.get("new_shift_name")
    if not shift_name:
        return

    # Check if grounding had an error for this call
    for step in result.middleware_steps:
        if step.name == "grounding" and step.status in ("clarification_needed",):
            return  # clarification, not a learn case

    if _is_error(result.data) and "시프트를 찾을 수 없습니다" in str(result.data.get("error", "")):
        # Grounding failed — record the failed term
        if shift_name not in failed_terms:
            failed_terms.append(shift_name)
        return

    # Grounding succeeded — check if we can learn from a previous failure
    if not failed_terms or _is_error(result.data):
        return

    # Success! Check if shift_name differs from any failed term
    for failed_term in list(failed_terms):
        if failed_term != shift_name:
            _append_learned_abbreviation(failed_term, shift_name)
            logger.info("Auto-learned abbreviation: '%s' → '%s'", failed_term, shift_name)
    failed_terms.clear()


def _append_learned_abbreviation(abbreviation: str, canonical: str) -> None:
    """Append a newly learned abbreviation to ABBREVIATION_DICT.md."""
    from pathlib import Path

    dict_path = Path(__file__).resolve().parent.parent.parent / ".aide" / "ABBREVIATION_DICT.md"
    if not dict_path.exists():
        return

    content = dict_path.read_text(encoding="utf-8")
    marker = "<!-- AUTO_LEARNED_START -->"
    entry = f"| {abbreviation} | {canonical} | 자동 학습 |"

    # Check if already exists
    if abbreviation in content:
        return

    content = content.replace(marker, f"{marker}\n{entry}")
    dict_path.write_text(content, encoding="utf-8")


def _sanitize_messages(messages: list[dict]) -> list[dict]:
    """Strip system prompt content for UI display, keep structure."""
    sanitized = []
    for m in messages:
        msg = {**m}
        if msg.get("role") == "system":
            content = msg.get("content", "")
            msg["content"] = content[:200] + "..." if len(content) > 200 else content
        elif msg.get("role") == "tool":
            content = msg.get("content", "")
            if isinstance(content, str) and len(content) > 500:
                msg["content"] = content[:500] + "..."
        sanitized.append(msg)
    return sanitized


# ── Token budget — LLM inject 직전 messages truncation ──────────────────────
#
# 영구 저장 (MSSQL/Redis) 의 messages 는 전체 보존되어 감사 trail 손실 없음.
# 본 truncation 은 매 LLM call 직전에만 적용되어 token 비용 방어.
#
# build_system_prompt 가 한국어 도메인 지식 + skill descriptions 포함으로
# 약 31k chars (~10k tokens). 96000 chars 예산 = ~24k tokens →
# 최신 modern LLM context window (128k+) 안에서 안전.
#
# 정책:
#   - system message (index 0) 항상 head 에 유지
#   - 최신 unit (마지막) 항상 유지 — LLM 호출이 의미를 가지려면 최소 latest 보존
#   - 중간 history 는 budget 안에서 최신순으로 포함
#   - tool-call group (assistant.tool_calls + 뒤따르는 tool 메시지들) 은 분리 불가:
#     truncation 이 group 을 쪼개 tool 메시지를 부모 assistant 와 떼어놓으면
#     OpenAI 가 "messages with role 'tool' must be a response to a preceding
#     message with 'tool_calls'" 400 을 던진다. 그래서 message 단위가 아니라
#     unit(=group) 단위로 자른다.

_MAX_INJECT_CHARS = 96000  # ≈ 24k tokens


def _message_size(m: dict) -> int:
    """단일 메시지의 대략적 char size (content + tool_calls 직렬화 추정)."""
    content = m.get("content")
    n = len(content) if isinstance(content, str) else 0
    tc = m.get("tool_calls")
    if tc:
        try:
            n += len(str(tc))
        except Exception:
            pass
    return n


def _group_message_units(rest: list[dict]) -> list[list[dict]]:
    """메시지 시퀀스를 truncation 원자 단위(unit)로 묶는다.

    - assistant(tool_calls) + 뒤따르는 연속 tool 메시지 → 하나의 unit (분리 금지)
    - 그 외(user, tool_calls 없는 assistant, orphan tool) → 단독 unit
    """
    units: list[list[dict]] = []
    i, n = 0, len(rest)
    while i < n:
        m = rest[i]
        if m.get("role") == "assistant" and m.get("tool_calls"):
            j = i + 1
            while j < n and rest[j].get("role") == "tool":
                j += 1
            units.append(rest[i:j])
            i = j
        else:
            units.append([rest[i]])
            i += 1
    return units


def _truncate_for_llm(
    messages: list[dict], *, max_chars: int = _MAX_INJECT_CHARS
) -> list[dict]:
    """LLM injection 용 messages truncation — system + latest unit 필수 + budget.

    tool-call group 을 쪼개지 않도록 unit 단위로 자른다. 원본 list/ dict 는 변경 안 함.
    """
    if not messages:
        return []

    head: list[dict] = []
    if messages[0].get("role") == "system":
        head = [messages[0]]
        rest = messages[1:]
    else:
        rest = messages

    if not rest:
        return head

    budget = max_chars - sum(_message_size(m) for m in head)
    units = _group_message_units(rest)

    # 최신 unit 부터 역순으로 budget 안에서 포함 (첫 unit 은 예산 초과해도 무조건 보존).
    selected: list[list[dict]] = []
    used = 0
    for unit in reversed(units):
        size = sum(_message_size(m) for m in unit)
        if selected and used + size > budget:
            break
        selected.append(unit)
        used += size
    selected.reverse()

    flat = [m for unit in selected for m in unit]

    # 안전장치: 손상된 history(앞단 orphan tool)가 들어와도 무효 chain 을 만들지 않음.
    while flat and flat[0].get("role") == "tool":
        flat.pop(0)

    return head + flat
