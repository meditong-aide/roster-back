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

from agents_v2.errors import ErrorType, classify
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


# audit 테이블 존재 여부 프로브 캐시 (process-wide).
# None=미확인, True/False=확인됨. 운영 MSSQL 에 agent_skill_invocation 이 아직
# 마이그레이션 안 됐을 때 매 스킬마다 실패 INSERT 로 세션을 오염시키는 것을 막는다.
_audit_table_present: bool | None = None


def _write_skill_audit(
    db: Session,
    ctx: SessionContext,
    skill_name: str,
    args: dict,
    status: str,
    error_message: str | None,
    latency_ms: float,
) -> None:
    """Skill 호출 audit row insert. 실패해도 skill 실행/세션에 절대 영향 X.

    의료 도메인 audit 요건 + RBAC 추적 + 디버깅용.

    견고성: 실패한 INSERT 의 flush 는 SQLAlchemy 세션을 오염(PendingRollbackError)시켜
    같은 턴의 다음 스킬 쿼리를 500 으로 만든다(운영 MSSQL 에 테이블 미마이그레이션 시 발생).
    그래서 테이블 존재를 has_table 로 1회 프로브해서 **부재면 INSERT 자체를 회피**(영구 skip)한다.
    INSERT 시도 자체가 없으니 세션이 오염될 일도 없다.
    """
    global _audit_table_present
    if _audit_table_present is False:
        return
    try:
        from sqlalchemy import inspect as _sa_inspect

        from db.models import AgentSkillInvocation

        if _audit_table_present is None:
            _audit_table_present = _sa_inspect(db.get_bind()).has_table(
                AgentSkillInvocation.__tablename__
            )
            if not _audit_table_present:
                logger.warning(
                    "[middleware] agent_skill_invocation 테이블이 없습니다 — skill audit "
                    "비활성화 (이후 skip). 마이그레이션(2026_05_19_add_agent_memory_tables) 적용 필요."
                )
                return

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
        # 의도적으로 db.rollback() 하지 않는다: 스킬은 자체 commit 을 하지 않으므로
        # 이 시점에 미커밋 mutation 이 세션에 남아 있을 수 있고, 롤백하면 그 변경이
        # 조용히 사라진다. 테이블 부재(운영의 실제 원인)는 위 has_table 프로브가 이미
        # INSERT 자체를 막으므로 여기 도달하지 않는다. 테이블이 존재하는데도 INSERT 가
        # 실패하는 경우(스키마 정상 가정상 드묾)는 audit 만 포기하고 로그로 남긴다.
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

    # ── ④b Postcondition 검증 (5요소의 '검증' 게이트) ──
    # 매니페스트 스킬이 postcondition 을 선언했고, error/preview/clarification 이 아닌
    # 완료 결과(classify OK)인데 성공조건을 통과 못 하면 VERIFICATION_FAILED 로 승격한다.
    # silent 부분실패(error 없이 반환했으나 실제로 성립 안 함)를 차단.
    if status == "SUCCESS" and classify(result) is ErrorType.OK:
        from agents_v2.skills.manifest import SKILL_SPECS, load_manifest_skills

        load_manifest_skills()
        spec = SKILL_SPECS.get(skill_name.replace("-", "_"))
        if spec is not None and spec.postcondition is not None:
            try:
                passed = bool(spec.postcondition(result))
            except Exception as e:  # noqa: BLE001
                passed = False
                logger.warning("[middleware] postcondition raised for %s: %s", skill_name, e)
            if not passed:
                result = {
                    "error": "실행 결과가 성공 조건(postcondition)을 통과하지 못했습니다.",
                    "verification_failed": True,
                }
                steps.append(
                    MiddlewareStep("verification", "block", detail="postcondition failed")
                )
                status = "VERIFICATION_FAILED"
                err_msg = "postcondition failed"

    # ── ④c Read-back 검증 (L1) — 실행 후 DB 대조로 '조용한 거짓완료' 차단 ──
    # postcondition(shape 검사)을 통과했어도, 등록된 read-back 이 실제 DB 반영을 확인한다.
    # (postcondition 이 이미 실패했으면 result 에 verification_failed 가 있어 classify≠OK → 건너뜀)
    if status == "SUCCESS" and classify(result) is ErrorType.OK:
        from agents_v2.verify import run_readback

        vr = run_readback(db, skill_name, args, result)
        if not vr.ok:
            result = {
                "error": f"실행됐다고 보고됐으나 실제 반영이 확인되지 않았습니다: {vr.reason}",
                "verification_failed": True,
            }
            steps.append(
                MiddlewareStep("verification", "block", detail=f"readback: {vr.reason}")
            )
            status = "VERIFICATION_FAILED"
            err_msg = f"readback failed: {vr.reason}"

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
    "manage_grade",
    "manage_team_min",
    "manage_wanted_deadline",
    "manage_teams",
    "manage_wanted_limits",
    "publish_schedule",
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

    # (0) client-action (navigate/prefill/switch_ward) — HN 전용 화면 게이팅.
    # target 별 hn_only 여부는 client_actions 모듈(route SSOT 매핑)이 소유.
    # switch_ward 는 target 없는 컨텍스트 전환 — 권한은 프론트 셀렉터(접근 가능 ward 목록)가 강제.
    if normalized in ("navigate", "prefill", "switch_ward"):
        from agents_v2.skills.client_actions import target_permission_error

        return target_permission_error(args.get("target"), ctx)

    # invoke: 비파괴 UI 명령 대행. command 별 hn_only 게이트(client_actions SSOT).
    if normalized == "invoke":
        from agents_v2.skills.client_actions import command_permission_error

        return command_permission_error(args.get("command"), ctx)

    # 매니페스트 스킬: 권한을 SkillSpec 에서 파생 — 하드코딩 불필요.
    # (신규 스킬은 @skill(mutation=..., hn_only=...) 선언만으로 게이트가 걸린다.)
    # load 보장: execute_skill 은 _check_permission(초반)→run_skill(후반 로드) 순서라,
    # 프로세스 첫 스킬 호출 시 매니페스트가 아직 로드 안 됐을 수 있다. idempotent 로드로
    # 권한 게이트가 스킵되는 것을 방지(방어적).
    from agents_v2.skills.manifest import SKILL_SPECS, load_manifest_skills

    load_manifest_skills()
    spec = SKILL_SPECS.get(normalized)
    if spec is not None:
        if spec.hn_only and not is_hn:
            return "이 작업은 수간호사(HN) 또는 관리자(ADM) 권한이 필요합니다."
        if spec.mutation and not is_hn:
            nurse_name = args.get("nurse_name", "")
            if (
                nurse_name
                and nurse_name != ctx.nurse_name
                and nurse_name not in _SELF_REFERENCE_ALIASES
            ):
                return f"다른 간호사({nurse_name})의 데이터를 수정할 권한이 없습니다."
        return None

    # (1) 병동 전체 영향 mutation
    if normalized in {"update_constraint", "generate_schedule"} and not is_hn:
        return "병동 전체 설정 변경은 수간호사(HN) 또는 관리자(ADM) 권한이 필요합니다."

    # (4) 다른 간호사의 월 한도 설정
    if normalized == "update_monthly_limit" and not is_hn:
        return "다른 간호사의 월 한도 설정은 수간호사(HN) 또는 관리자(ADM) 권한이 필요합니다."

    # (1b) 등급 정책 — 조회·변경 모두 HN/ADM 전용.
    # 등급(역량) 정보는 일반 간호사에게 노출되어선 안 되는 민감 정보.
    if normalized == "manage_grade" and not is_hn:
        return "등급 정책 조회·변경은 수간호사(HN) 또는 관리자(ADM) 권한이 필요합니다."

    # (1c) 팀 최소 인원 — 조회·변경 모두 HN/ADM 전용.
    if normalized == "manage_team_min" and not is_hn:
        return "팀 최소 인원 조회·변경은 수간호사(HN) 또는 관리자(ADM) 권한이 필요합니다."

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
    # 변경 주체 (audit / updated_by 용). mutation skill 이 참조.
    args.setdefault("acting_user_id", ctx.nurse_id)

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
