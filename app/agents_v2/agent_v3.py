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

logger = logging.getLogger(__name__)


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

    def __init__(self, llm_client: LLMClient):
        self.llm = llm_client

    def run(
        self,
        db: Session,
        user_message: str,
        ctx: SessionContext,
    ) -> AgentResult:
        # ── Build system prompt (with domain knowledge + routines) ──
        system_prompt = build_system_prompt(ctx)

        # ── Restore or initialize conversation ──
        if ctx.messages:
            messages = ctx.messages + [
                {"role": "user", "content": user_message}
            ]
        else:
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ]

        # ── Handle pending approval ──
        if ctx.pending_approval and _is_confirmation(user_message):
            return self._execute_approval(db, ctx, messages)

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
            t0 = time.time()
            response = self.llm.chat(messages, tools=tools)
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
                            "content": json.dumps(result.data, ensure_ascii=False, default=str),
                        })
                        return AgentResult(
                            awaiting_approval=True,
                            preview=preview_with_context,
                            answer="변경 미리보기를 확인해 주세요.",
                            trace=trace,
                            messages=messages,
                            variable_memory=vm.to_dict(),
                        )

                    # ── Append tool result for LLM to observe ──
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.call_id,
                            "content": json.dumps(
                                result.data, ensure_ascii=False, default=str
                            ),
                        }
                    )

        return AgentResult(
            answer="처리 단계가 너무 많습니다. 좀 더 구체적으로 말씀해 주세요.",
            trace=trace,
            messages=messages,
            variable_memory=vm.to_dict(),
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


def _call_signature(skill_name: str, args: dict) -> str:
    """Deterministic signature for duplicate call detection.

    Uses full args (sorted, stringified) so different nurse_name/date
    with same scope won't collide, while truly identical calls are caught.
    """
    normalized = json.dumps(args, sort_keys=True, default=str)
    return f"{skill_name}:{normalized}"


def _is_confirmation(msg: str) -> bool:
    """Check if user message is confirming a pending approval.

    Uses substring matching — "응 변경해줘" matches because "응" is in it.
    """
    cleaned = msg.strip().lower()
    # Exact match first
    if cleaned in _CONFIRM_WORDS:
        return True
    # Substring: any confirm word appears in the message
    return any(w in cleaned for w in _CONFIRM_WORDS)


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
