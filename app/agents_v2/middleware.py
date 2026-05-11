"""Middleware pipeline — skill execution with permission, context, and grounding.

Pipeline stages:
  ① Permission Check — user role → mutation access control
  ② Context Injection — session context → skill params (group_id, year, month, "나"→nurse_id)
  ③ Internal Grounding — nurse_name→ID, shift_name→code, date→ISO
  ④ Skill Execution — run_skill(db, name, params)
  ⑤ Trace Capture — stage metadata for debug UI
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any



from sqlalchemy.orm import Session

from agents_v2.grounding.internal import (
    resolve_date,
    resolve_date_range,
    resolve_nurse,
    resolve_shift,
)
from agents_v2.schemas.session_context import SessionContext
from agents_v2.skills.registry import run_skill


@dataclass
class MiddlewareStep:
    """Single middleware substep for debug trace."""

    name: str  # "permission", "context_injection", "grounding", "execution"
    status: str  # "pass", "block", "grounded", "skip"
    detail: str = ""
    before: dict | None = None
    after: dict | None = None

    def to_dict(self) -> dict:
        d: dict[str, Any] = {"name": self.name, "status": self.status}
        if self.detail:
            d["detail"] = self.detail
        if self.before is not None:
            d["before"] = self.before
        if self.after is not None:
            d["after"] = self.after
        return d


@dataclass
class SkillResult:
    """Skill execution result with metadata."""

    data: Any
    duration_ms: float
    skill_name: str
    grounded_params: dict
    blocked: bool = False
    block_reason: str | None = None
    middleware_steps: list[MiddlewareStep] = field(default_factory=list)


def execute_skill(
    db: Session,
    skill_name: str,
    args: dict,
    ctx: SessionContext,
) -> SkillResult:
    """Run the full middleware pipeline and execute the skill."""
    t0 = time.time()
    steps: list[MiddlewareStep] = []

    # ── ① Permission Check ──
    block = _check_permission(skill_name, args, ctx)
    if block:
        steps.append(MiddlewareStep("permission", "block", detail=block))
        return SkillResult(
            data={"error": block, "permission_denied": True},
            duration_ms=0,
            skill_name=skill_name,
            grounded_params=args,
            blocked=True,
            block_reason=block,
            middleware_steps=steps,
        )
    steps.append(MiddlewareStep(
        "permission", "pass",
        detail=f"role={ctx.user_role}, skill={skill_name}",
    ))

    # ── ② Context Injection ──
    args_before = {**args}
    args = _inject_context(args, ctx)
    injected = {k: v for k, v in args.items() if args_before.get(k) != v}
    steps.append(MiddlewareStep(
        "context_injection",
        "injected" if injected else "skip",
        detail=", ".join(f"{k}={v}" for k, v in injected.items()) if injected else "no injection needed",
        before=args_before,
        after={**args},
    ))

    # ── ③ Internal Grounding ──
    args_before_ground = {**args}
    clarification = _ground_params(db, ctx.group_id, args)
    if clarification:
        steps.append(MiddlewareStep(
            "grounding", "clarification_needed",
            detail=clarification.get("question", "ambiguous input"),
        ))
        dt = (time.time() - t0) * 1000
        return SkillResult(
            data=clarification,
            duration_ms=dt,
            skill_name=skill_name,
            grounded_params=args,
            middleware_steps=steps,
        )
    grounded = {k: v for k, v in args.items() if args_before_ground.get(k) != v}
    steps.append(MiddlewareStep(
        "grounding",
        "grounded" if grounded else "skip",
        detail=", ".join(f"{k}: {args_before_ground.get(k)}→{v}" for k, v in grounded.items()) if grounded else "no grounding needed",
    ))

    # ── ④ Skill Execution ──
    try:
        result = run_skill(db, skill_name, args)
        steps.append(MiddlewareStep("execution", "ok", detail=skill_name))
    except KeyError as e:
        result = {"error": f"Unknown skill: {e}"}
        steps.append(MiddlewareStep("execution", "error", detail=str(e)))
    except Exception as e:
        result = {"error": f"Skill execution error: {str(e)}"}
        steps.append(MiddlewareStep("execution", "error", detail=str(e)))

    dt = (time.time() - t0) * 1000

    return SkillResult(
        data=result,
        duration_ms=dt,
        skill_name=skill_name,
        grounded_params=args,
        middleware_steps=steps,
    )


# ── Permission Check ────────────────────────────────────────

_MUTATION_SKILLS = frozenset({
    "bulk_mutation",
    "bulk-mutation",
    "update_constraint",
    "update-constraint",
    "update_person_attr",
    "update-person-attr",
    "generate_schedule",
    "generate-schedule",
})


def _check_permission(
    skill_name: str, args: dict, ctx: SessionContext
) -> str | None:
    """Check permissions. Returns error message if blocked."""
    normalized = skill_name.replace("-", "_")

    # Non-admin users cannot modify other nurses' data
    if normalized in _MUTATION_SKILLS and ctx.user_role not in ("HN", "ADM"):
        nurse_name = args.get("nurse_name", "")
        if nurse_name and nurse_name != ctx.nurse_name and nurse_name not in (
            "나",
            "내",
            "제",
            "본인",
        ):
            return f"다른 간호사({nurse_name})의 데이터를 수정할 권한이 없습니다."

    return None


# ── Context Injection ───────────────────────────────────────


def _inject_context(args: dict, ctx: SessionContext) -> dict:
    """Inject session context values into skill params."""
    args = {**args}  # shallow copy

    # Required context defaults
    args.setdefault("group_id", ctx.group_id)
    args.setdefault("office_id", ctx.office_id)
    args.setdefault("year", ctx.year)
    args.setdefault("month", ctx.month)

    # "나/내/제/본인" → current user
    if args.get("nurse_name") in ("나", "내", "제", "본인"):
        args["nurse_name"] = ctx.nurse_name
        if ctx.nurse_id:
            args["nurse_ids"] = [ctx.nurse_id]

    # Target month override
    if "target_month" in args:
        args["month"] = args.pop("target_month")

    return args


# ── Internal Grounding ──────────────────────────────────────


def _ground_params(db: Session, group_id: str, params: dict) -> dict | None:
    """Ground natural language params to DB IDs. Returns clarification dict if needed."""
    # nurse_name → nurse_id
    if params.get("nurse_name") and not params.get("nurse_ids"):
        r = resolve_nurse(db, group_id, params["nurse_name"])
        if r.needs_clarification:
            return r.to_clarification_dict()
        if r.error:
            return {"error": r.error}
        if r.resolved:
            params["nurse_ids"] = [r.value]

    # shift_name → shift_codes (may be single or list for category matches)
    if params.get("shift_name") and not params.get("shift_codes"):
        r = resolve_shift(db, group_id, params["shift_name"])
        if r.resolved:
            if isinstance(r.value, list):
                params["shift_codes"] = r.value
            else:
                params["shift_codes"] = [r.value]
        # Shift not found is not blocking — LLM may retry

    # new_shift_name → new_shift_code (for mutations — must be single shift)
    if params.get("new_shift_name"):
        r = resolve_shift(db, group_id, params["new_shift_name"])
        if r.resolved:
            if isinstance(r.value, list):
                # Category match for mutation — need clarification
                return {
                    "needs_clarification": True,
                    "question": f"'{params['new_shift_name']}' 카테고리에 여러 시프트가 있습니다. 어떤 시프트로 변경할까요?",
                    "options": r.value,
                }
            params["new_shift_code"] = r.value

    # date → YYYY-MM-DD
    if params.get("date") and not _is_iso_date(params["date"]):
        resolved = resolve_date(
            params["date"], params.get("year", 2026), params.get("month", 1)
        )
        if resolved:
            params["date"] = resolved

    # new_deadline → YYYY-MM-DD
    if params.get("new_deadline") and not _is_iso_date(params["new_deadline"]):
        resolved = resolve_date(
            params["new_deadline"], params.get("year", 2026), params.get("month", 1)
        )
        if resolved:
            params["new_deadline"] = resolved

    # date_range → start/end
    if params.get("date_range"):
        start, end = resolve_date_range(
            params["date_range"],
            params.get("year", 2026),
            params.get("month", 1),
        )
        if start and end:
            params["date_range_start"] = start
            params["date_range_end"] = end

    # submitted_date_range → resolve natural language ("지난주" etc.)
    if params.get("submitted_date_range") and "~" not in str(params["submitted_date_range"]):
        start, end = resolve_date_range(
            params["submitted_date_range"],
            params.get("year", 2026),
            params.get("month", 1),
        )
        if start and end:
            params["submitted_date_range"] = f"{start}~{end}"

    return None  # grounding succeeded


def _is_iso_date(s: str) -> bool:
    return bool(re.match(r"\d{4}-\d{2}-\d{2}", s))
