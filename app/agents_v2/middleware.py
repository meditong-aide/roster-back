"""Middleware pipeline — skill execution with permission, context, and grounding.

Pipeline stages:
  ① Permission Check — user role → mutation access control
  ② Context Injection — session context → skill params (group_id, year, month, "나"→nurse_id)
  ③ Internal Grounding — nurse_name→ID, shift_name→code, date→ISO
  ④ Skill Execution — run_skill(db, name, params)
  ⑤ Trace Capture — stage metadata for debug UI
"""

from __future__ import annotations

import json
import logging
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

logger = logging.getLogger(__name__)


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


_AUDIT_ARGS_MAX_LEN = 2000


def _safe_args_json(args: dict, max_len: int = _AUDIT_ARGS_MAX_LEN) -> str | None:
    """args dict → JSON 문자열. 직렬화 실패하거나 너무 길면 truncate/None."""
    try:
        s = json.dumps(args, ensure_ascii=False, default=str)
    except Exception:
        return None
    if len(s) > max_len:
        return s[: max_len - 1] + "…"
    return s


def _write_skill_audit(
    db: Session,
    ctx: SessionContext,
    skill_name: str,
    args: dict,
    status: str,
    error_message: str | None,
    latency_ms: float,
) -> None:
    """Skill 호출 audit row insert. 실패 시 silent — skill 실행 자체에 영향 X.

    의료 도메인 audit 요건 + RBAC 추적 + 디버깅용.
    """
    try:
        from db.models import AgentSkillInvocation

        row = AgentSkillInvocation(
            agent_run_id=ctx.conversation_id,
            session_id=ctx.conversation_id,
            user_id=ctx.nurse_id,
            group_id=ctx.group_id,
            skill_name=skill_name,
            args_json=_safe_args_json(args),
            status=status,
            error_message=error_message,
            latency_ms=latency_ms,
        )
        db.add(row)
        db.flush()
    except Exception as e:
        logger.warning("[middleware] skill audit insert failed: %s", e)


def execute_skill(
    db: Session,
    skill_name: str,
    args: dict,
    ctx: SessionContext,
) -> SkillResult:
    """Run the full middleware pipeline and execute the skill."""
    t0 = time.time()
    steps: list[MiddlewareStep] = []
    args_original = {**args}

    # ── ① Permission Check ──
    block = _check_permission(skill_name, args, ctx)
    if block:
        steps.append(MiddlewareStep("permission", "block", detail=block))
        dt = (time.time() - t0) * 1000
        _write_skill_audit(db, ctx, skill_name, args_original, "DENIED", block, dt)
        return SkillResult(
            data={"error": block, "permission_denied": True},
            duration_ms=dt,
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
        _write_skill_audit(
            db, ctx, skill_name, args,
            "CLARIFICATION_NEEDED",
            clarification.get("question", "ambiguous input"),
            dt,
        )
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

    # ── ③-1 본인 대상 재확인(간호사 쓰기 · 그라운딩 뒤) ──
    block = _check_self_target(skill_name, args, ctx)
    if block:
        steps.append(MiddlewareStep("permission", "block", detail=block))
        dt = (time.time() - t0) * 1000
        _write_skill_audit(db, ctx, skill_name, args_original, "DENIED", block, dt)
        return SkillResult(
            data={"error": block, "permission_denied": True},
            duration_ms=dt,
            skill_name=skill_name,
            grounded_params=args,
            blocked=True,
            block_reason=block,
            middleware_steps=steps,
        )

    # ── ④ Skill Execution ──
    status = "SUCCESS"
    err_msg: str | None = None
    try:
        result = run_skill(db, skill_name, args)
        steps.append(MiddlewareStep("execution", "ok", detail=skill_name))
    except KeyError as e:
        result = {"error": f"Unknown skill: {e}"}
        steps.append(MiddlewareStep("execution", "error", detail=str(e)))
        status = "ERROR"
        err_msg = f"Unknown skill: {e}"
    except Exception as e:
        result = {"error": f"Skill execution error: {str(e)}"}
        steps.append(MiddlewareStep("execution", "error", detail=str(e)))
        status = "ERROR"
        err_msg = str(e)

    dt = (time.time() - t0) * 1000
    _write_skill_audit(db, ctx, skill_name, args, status, err_msg, dt)

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
    "update_constraint",
    "update_person_attr",
    "generate_schedule",
    "update_monthly_limit",
})

_SELF_REFERENCE_ALIASES = ("나", "내", "제", "본인")


def _is_head_or_admin(ctx: SessionContext) -> bool:
    return ctx.user_role in ("HN", "ADM")


def _check_permission(
    skill_name: str, args: dict, ctx: SessionContext
) -> str | None:
    """Permission gate. router/auth.py 의 is_head_nurse / is_master_admin 와 동일 의미.

    규칙:
      1) 병동 전체 영향 (update_constraint / generate_schedule) — HN/ADM 만.
      2) bulk_mutation 의 마감일 변경 / 일괄 승인 등 ward-wide action — HN/ADM 만.
      3) 개인 속성 mutation (update_person_attr) — HN/ADM 또는 본인.
      4) update_monthly_limit (개인별 N 한도) — HN/ADM 만 (다른 nurse 한도 설정).
      5) 그 외 mutation 은 본인 데이터 mutation 만 허용.
    """
    normalized = skill_name.replace("-", "_")
    is_hn = _is_head_or_admin(ctx)

    # (1) 병동 전체 영향 mutation
    if normalized in {"update_constraint", "generate_schedule"} and not is_hn:
        return "병동 전체 설정 변경은 수간호사(HN) 또는 관리자(ADM) 권한이 필요합니다."

    # (4) 다른 간호사의 월 한도 설정
    if normalized == "update_monthly_limit" and not is_hn:
        return "다른 간호사의 월 한도 설정은 수간호사(HN) 또는 관리자(ADM) 권한이 필요합니다."

    # (2) bulk_mutation 의 ward-wide action
    if normalized == "bulk_mutation" and not is_hn:
        scope = args.get("scope", "")
        action = args.get("action", "")
        if scope == "wanted_submissions" and action in ("update_deadline", "clear_deadline"):
            return "원티드 마감일 변경은 수간호사(HN) 또는 관리자(ADM) 권한이 필요합니다."
        if scope == "wanted_adjustment":
            # 본인 nurse 만 명시되어 있고 self 인 경우는 허용
            nurse_ids = args.get("nurse_ids") or []
            nurse_name = args.get("nurse_name") or ""
            is_self_target = (
                (nurse_name in _SELF_REFERENCE_ALIASES)
                or (nurse_name == ctx.nurse_name)
                or (ctx.nurse_id and nurse_ids == [ctx.nurse_id])
            )
            if not is_self_target:
                return "원티드 일괄 처리는 수간호사(HN) 또는 관리자(ADM) 권한이 필요합니다."

    # (3, 5) 다른 간호사 data 수정 차단 (기존 로직 유지)
    if normalized in _MUTATION_SKILLS and not is_hn:
        nurse_name = args.get("nurse_name", "")
        if nurse_name and nurse_name != ctx.nurse_name and nurse_name not in _SELF_REFERENCE_ALIASES:
            return f"다른 간호사({nurse_name})의 데이터를 수정할 권한이 없습니다."
        # ★ 이름 없이 nurse_ids 로 다른 간호사를 가리키는 길도 막는다 — 이름만 봐서 통과했다(11-04 검토).
        raw_ids = args.get("nurse_ids") or []
        if isinstance(raw_ids, (str, int)):
            raw_ids = [raw_ids]
        if any(str(n) != str(ctx.nurse_id) for n in raw_ids):
            return "다른 간호사의 데이터를 수정할 권한이 없습니다."

    return None


def _check_self_target(skill_name: str, args: dict, ctx: SessionContext) -> str | None:
    """간호사(HN·ADM 아님) 쓰기는 그라운딩 **뒤에** 대상이 본인·본인 병동인지 다시 본다. 막을 문구 또는 None.

    ★ ① 권한 검사는 원본 인자만 본다 — 대상을 비우면 스킬이 병동 전원에 쓰고(예: 근무표 일괄 변경), 이름 해석
      (③)은 부분 일치로 남을 가리킬 수 있으며, LLM 이 준 group_id 는 `_inject_context` 가 그대로 둔다(11-04 리뷰).
    ★ 대상을 비운 간호사 쓰기는 본인으로 채운다(간호사가 고칠 수 있는 건 본인 데이터뿐이다).
    """
    if skill_name.replace("-", "_") not in _MUTATION_SKILLS or _is_head_or_admin(ctx):
        return None
    if str(args.get("group_id")) != str(ctx.group_id) or str(args.get("office_id")) != str(ctx.office_id):
        return "다른 병동의 데이터를 수정할 권한이 없습니다."
    if not ctx.nurse_id:
        return "본인 정보를 확인할 수 없어 수정할 수 없습니다."
    raw_ids = args.get("nurse_ids") or []
    if isinstance(raw_ids, (str, int)):
        raw_ids = [raw_ids]
    if not raw_ids:
        args["nurse_ids"] = [ctx.nurse_id]
        return None
    if {str(n) for n in raw_ids} != {str(ctx.nurse_id)}:
        return "다른 간호사의 데이터를 수정할 권한이 없습니다."
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
    # 호출자 역할 — 스킬이 간호사 본인 쓰기 규칙(원티드 잠금·마감·일자 한도)을 걸지 정한다.
    # ★ LLM 이 넣은 값은 무시하고 세션 역할로 **덮어쓴다**(setdefault 금지 — 역할을 사칭하지 못하게).
    args["_caller_is_manager"] = _is_head_or_admin(ctx)

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
