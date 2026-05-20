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
import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from agents_v2.harness.prompt_builder import build_system_prompt
from agents_v2.llm_client import LLMClient, LLMResponse
from agents_v2.middleware import SkillResult, execute_skill
from agents_v2.schemas.session_context import SessionContext
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
    trace: list[Stage] = field(default_factory=list)
    messages: list[dict] = field(default_factory=list)
    variable_memory: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d: dict[str, Any] = {"answer": self.answer}
        if self.needs_clarification:
            d["needs_clarification"] = True
            d["clarification_question"] = self.question
            d["clarification_options"] = self.options
        if self.awaiting_approval:
            d["awaiting_approval"] = True
            d["preview"] = self.preview
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
    ):
        """SchedulingAgent.

        Args:
            llm_client: 메인 turn LLM (도구 호출 + 응답 생성).
            memory_extractor: US-A4 Tier-2 fact extractor. 없으면 llm_client 재사용.
            enable_user_memory: False 면 inject/consolidate 모두 스킵 (테스트/긴급용).
        """
        self.llm = llm_client
        self.enable_user_memory = enable_user_memory
        if memory_extractor is None and enable_user_memory:
            memory_extractor = MemoryExtractor(llm_client)
        self.memory_extractor = memory_extractor

    def run(
        self,
        db: Session,
        user_message: str,
        ctx: SessionContext,
    ) -> AgentResult:
        """Turn entry — inject_user_memory_context → _run_impl → consolidate_after_turn."""
        # US-A4 Tier-2 memory inject + run + consolidate 흐름은 _run_impl 이 핸들링.
        # consolidate 는 응답 반환 직전에 호출 (silent on failure).
        current_user_facts: list[dict] = (
            self._load_user_facts(db, ctx) if self.enable_user_memory else []
        )

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

        return result

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

        tools = SKILL_TOOLS
        trace: list[Stage] = []
        vm = VariableMemory()
        _prev_calls: set[str] = set()  # dedup signatures
        _failed_shift_terms: list[str] = []  # Track failed shift names for auto-learn

        # Restore VM from previous turns
        if ctx.variable_memory:
            vm._store.update(ctx.variable_memory)

        for turn in range(self.MAX_TURNS):
            # ── LLM call ──
            # Truncate inject messages for token budget — 영구 messages 변수는 전체 보존.
            # MSSQL/Redis save_messages 에는 전체 messages 가 저장되어 감사 trail 손실 없음.
            inject_messages = _truncate_for_llm(messages, max_chars=_MAX_INJECT_CHARS)
            t0 = time.time()
            response = self.llm.chat(inject_messages, tools=tools)
            llm_ms = (time.time() - t0) * 1000

            # ── Text response → final answer ──
            if response.is_text:
                trace.append(
                    Stage("answer", "ok", {"text": response.text}, llm_ms)
                )
                return AgentResult(
                    answer=response.text or "",
                    trace=trace,
                    messages=messages,
                    variable_memory=vm.to_dict(),
                )

            # ── Tool call(s) → middleware pipeline ──
            if response.is_tool_call:
                # Append the single assistant message (may contain N tool_calls)
                messages.append(response.as_assistant_message())

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

                    # ── Auto-learn abbreviation tracking ──
                    _track_shift_learning(
                        skill_args, result, _failed_shift_terms,
                    )

                    # ── apply_hint 흐름 — generate_schedule INFEASIBLE 재시도 ──
                    apply_hint_q = _extract_apply_hint_question(skill_name, result.data)
                    if apply_hint_q is not None:
                        hint_data = result.data["infeasibility"]["apply_hint"]
                        pending = {
                            "type": "apply_hint",
                            "apply_hint": hint_data,
                            "original_args": skill_args,
                            "question": apply_hint_q,
                        }
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.call_id,
                            "content": _wrap_untrusted_tool_output(skill_name, result.data),
                        })
                        return AgentResult(
                            awaiting_approval=True,
                            preview=pending,
                            answer=apply_hint_q,
                            trace=trace,
                            messages=messages,
                            variable_memory=vm.to_dict(),
                        )

                    # ── Approval flow (preview) — must exit for user confirmation ──
                    if _is_preview_result(result.data):
                        preview_with_context = {
                            **result.data,
                            "skill_name": skill_name,
                            "args": skill_args,
                        }
                        # Still need to append result for message consistency
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.call_id,
                            "content": _wrap_untrusted_tool_output(skill_name, result.data),
                        })
                        preview_answer = self._generate_preview_answer(messages, trace)
                        return AgentResult(
                            awaiting_approval=True,
                            preview=preview_with_context,
                            answer=preview_answer,
                            trace=trace,
                            messages=messages,
                            variable_memory=vm.to_dict(),
                        )

                    # ── Append tool result for LLM to observe ──
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.call_id,
                            "content": _wrap_untrusted_tool_output(skill_name, result.data),
                        }
                    )

        return AgentResult(
            answer="처리 단계가 너무 많습니다. 좀 더 구체적으로 말씀해 주세요.",
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


def _extract_apply_hint_question(skill_name: str, data: Any) -> str | None:
    """generate_schedule 결과에 user_actionable apply_hint가 있으면 재시도 질문 반환.

    None 반환 시 apply_hint 흐름 미진입.
    """
    if skill_name not in ("generate_schedule", "generate-schedule"):
        return None
    if not isinstance(data, dict):
        return None

    infeasibility = data.get("infeasibility")
    if not isinstance(infeasibility, dict):
        return None

    apply_hint = infeasibility.get("apply_hint")
    if not isinstance(apply_hint, dict):
        return None

    # user_consent_required=True 인 경우만 사용자에게 질의
    if not apply_hint.get("user_consent_required", False):
        return None

    human_msg = apply_hint.get("human_message_ko") or ""
    if human_msg:
        return human_msg
    return "제약 조건을 조정하고 근무표 생성을 재시도하시겠습니까?"


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


def _is_error(data: Any) -> bool:
    return isinstance(data, dict) and "error" in data


def _needs_clarification(data: Any) -> bool:
    return isinstance(data, dict) and data.get("needs_clarification") is True


def _is_preview_result(data: Any) -> bool:
    return isinstance(data, dict) and (
        data.get("preview_only") is True or data.get("preview") is True
    )


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
#   - 최신 메시지 (마지막) 항상 유지 — LLM 호출이 의미를 가지려면 최소 latest user 보존
#   - 중간 history 는 budget 안에서 최신순으로 포함

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


def _truncate_for_llm(
    messages: list[dict], *, max_chars: int = _MAX_INJECT_CHARS
) -> list[dict]:
    """LLM injection 용 messages truncation — system + latest 필수 + 중간 history budget.

    원본 messages list 는 변경하지 않음. 영구 저장 흐름과 분리.
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

    # 최신 메시지는 반드시 보존 (LLM 호출의 baseline)
    latest = rest[-1]
    older = rest[:-1]
    must_chars = sum(_message_size(m) for m in head) + _message_size(latest)

    if must_chars >= max_chars or not older:
        # 예산 초과해도 필수만 반환 / older 없으면 즉시 반환
        return head + [latest]

    budget = max_chars - must_chars
    selected_reversed: list[dict] = []
    used = 0
    for m in reversed(older):
        size = _message_size(m)
        if used + size > budget:
            break
        selected_reversed.append(m)
        used += size

    return head + list(reversed(selected_reversed)) + [latest]
