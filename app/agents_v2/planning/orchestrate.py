"""DAG 계획 오케스트레이션 glue — Planner → Executor → Joiner.

LLMCompiler 전체 파이프라인의 진입점. 의존 복합이면 plan 실행 결과(+승인대상 previews)를
반환, 아니면 None(호출부가 기존 ReAct 로 처리). agent_v3 통합은 opt-in 플래그 뒤.

설계: docs/AGENT_DAG_PLANNING_DESIGN.md.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Callable

from agents_v2.planning.executor import PlanExecResult, execute_plan
from agents_v2.planning.plan import Plan
from agents_v2.planning.planner import build_plan

logger = logging.getLogger(__name__)


def preview_fingerprint(previews: list[dict]) -> str:
    """미리보기 요약의 지문 — 승인 시점 대비 커밋 시점 상태 변화(staleness) 감지용."""
    import hashlib

    key = json.dumps(
        [[p.get("task"), (p.get("data") or {}).get("summary")] for p in (previews or [])],
        ensure_ascii=False, sort_keys=True, default=str,
    )
    return hashlib.md5(key.encode()).hexdigest()


@dataclass
class PlanRun:
    plan: Plan
    exec: PlanExecResult

    @property
    def needs_approval(self) -> bool:
        return bool(self.exec.previews) and self.exec.failed is None

    @property
    def failed(self) -> bool:
        return self.exec.failed is not None


def _replan_feedback(run: PlanRun) -> str:
    """실패 관찰 → planner 피드백 문자열. 어느 task 가 왜 실패했는지 + 시도한 구조."""
    f = run.exec.failed or {}
    data = f.get("data") or {}
    reason = data.get("error") or data.get("question") or data.get("block_reason") or "알 수 없는 오류"
    tried = [{"id": t.id, "skill": t.skill, "kind": t.kind, "deps": t.deps, "args": t.args}
             for t in run.plan.tasks]
    return (f"- 실패 task: {f.get('task')} (skill={f.get('skill')})\n"
            f"- 실패 사유: {reason}\n"
            f"- 시도한 계획: {json.dumps(tried, ensure_ascii=False, default=str)[:1500]}")


# ctx 가 자동 주입하는 파라미터 — 되물을 필요 없음(미들웨어 _inject_context 와 일치).
_CTX_INJECTED = frozenset({"group_id", "office_id", "year", "month", "acting_user_id"})


def _enum_nurses(db: Any, ctx: Any) -> list[str]:
    """병동 소속 간호사 이름 목록(선택 옵션용). 실패/대량이면 빈 리스트."""
    try:
        from db.models import Nurse
        rows = (db.query(Nurse.name)
                .filter(Nurse.group_id == getattr(ctx, "group_id", None))
                .limit(80).all())
        names = [r[0] for r in rows if r[0]]
        return names if len(names) <= 60 else []  # 너무 많으면 선택 UI 부적합 → 자유입력
    except Exception:  # noqa: BLE001
        return []


# param 이름 → DB 열거 함수. enum 없는 파라미터를 선택형으로 만드는 지렛대.
_DB_ENUMERATORS: dict[str, Callable[[Any, Any], list[str]]] = {
    "nurse_name": _enum_nurses,
    "nurse_ids": _enum_nurses,
}


def _param_options(param: str, prop: dict, db: Any, ctx: Any) -> list[str]:
    """선택 옵션 도출 — 스키마 enum 우선, 없으면 DB 열거, 그것도 없으면 빈 리스트."""
    if prop.get("enum"):
        return [str(e) for e in prop["enum"]]
    fn = _DB_ENUMERATORS.get(param)
    if fn is not None and db is not None and ctx is not None:
        return fn(db, ctx)
    return []


def _q_type(param: str, options: list[str]) -> str:
    """질문 위젯 타입 추론: 옵션 있으면 select / 날짜 / 숫자 / 자유입력."""
    if options:
        return "select"
    if "date" in param:
        return "date"
    if param in ("year", "month") or "count" in param or param.endswith(("_min", "_max", "_exact")):
        return "number"
    return "input"


def build_clarify_form(clarifications: list[dict], *, plan: Plan | None = None,
                       ctx: Any = None, skill_tools: list[dict] | None = None,
                       db: Any = None) -> dict:
    """수집된 되물음들 → 구조화 clarify_form(프론트 렌더용 named UI-action).

    **선택형 자동 보강**: 되물음 태스크의 스킬 스키마에서 **누락된 required 파라미터**를 도출하고,
    그 파라미터의 enum(스키마) 또는 DB 열거(간호사명 등)로 **선택 옵션**을 채운다. 스키마상
    누락 required 가 없으면(의미 모호 등) 스킬이 준 free-text question 으로 자유입력 fallback.
    per-skill 코드 수정 없이 스키마+DB 로 선택지를 만든다. 설계 docs/AGENT_CLARIFY_FORM_FRONTEND_TODO.md.
    """
    tools = {t.get("name"): t for t in (skill_tools or [])}
    tasks = {t.id: t for t in (plan.tasks if plan else [])}
    questions: list[dict] = []
    for c in clarifications:
        skill = c.get("skill")
        tool = tools.get(skill)
        task = tasks.get(c.get("task"))
        added = False
        if tool is not None and task is not None:
            params = tool.get("parameters") or {}
            required = params.get("required") or []
            props = params.get("properties") or {}
            for p in required:
                if p in (task.args or {}) or p in _CTX_INJECTED:
                    continue  # 이미 있거나 ctx 가 주입 → 안 물음
                prop = props.get(p, {})
                opts = _param_options(p, prop, db, ctx)
                questions.append({
                    "task": c.get("task"), "skill": skill, "param": p,
                    "question": (prop.get("description") or f"{p} 값이 필요합니다.").split("\n")[0][:120],
                    "type": _q_type(p, opts), "options": opts,
                })
                added = True
        if not added:  # 스키마상 누락 required 없음 → 스킬 free-text 로 자유입력
            opts = c.get("options") or []
            questions.append({
                "task": c.get("task"), "skill": skill, "param": None,
                "question": c.get("question") or "추가 정보가 필요합니다.",
                "type": "select" if opts else "input", "options": opts,
            })
    return {"type": "clarify_form", "questions": questions}


def apply_clarify_answers(plan_dict: dict, answers: list[dict]) -> dict:
    """clarify_form 답변(list of {task, param, value})을 plan 의 해당 task args 에 병합.

    param 이 None(자유입력 fallback)인 답변은 특정 태스크에 못 매핑하므로 무시(다음 라운드
    되물음으로 처리). plan_dict 를 in-place 갱신하고 반환.
    """
    tasks = {t.get("id"): t for t in (plan_dict.get("tasks") or [])}
    for a in (answers or []):
        tid, p, v = a.get("task"), a.get("param"), a.get("value")
        if p and tid in tasks and v is not None:
            tasks[tid].setdefault("args", {})[p] = v
    return plan_dict


def try_plan_run(
    db: Any, user_message: str, ctx: Any,
    planner_llm: Any, skill_tools: list[dict],
    execute_fn: Callable[[Any, str, dict, Any], Any],
    *,
    session_factory: Callable[[], Any] | None = None,
    max_replans: int = 1,
    collect_clarifications: bool = False,
) -> PlanRun | None:
    """의존 복합이면 plan 생성·실행(mutate=dry-run). 아니면 None(ReAct fallback).

    session_factory: 주면 레벨 내 read 를 세션 격리 병렬 실행(mutate 는 순차).
    max_replans: dry-run 실행이 실패하면 실패 관찰을 planner 에 피드백해 **교정 plan** 을
        최대 이 횟수만큼 재시도(LLMCompiler replan). 여전히 실패하면 그 실패 run 반환(→ ReAct).
        preview 단계라 mutate 는 preview_only=True → 재시도해도 실제 반영 없음(안전).
    """
    plan = build_plan(planner_llm, user_message, skill_tools)
    if plan is None:
        return None
    logger.info("[plan] %d tasks: %s", len(plan.tasks),
                [(t.id, t.skill, t.kind, t.deps) for t in plan.tasks])
    run = PlanRun(plan=plan, exec=execute_plan(
        db, plan, ctx, execute_fn, session_factory=session_factory,
        collect_clarifications=collect_clarifications))

    attempts = 0
    while run.failed and attempts < max_replans:
        attempts += 1
        feedback = _replan_feedback(run)
        logger.info("[replan] 시도 %d/%d — 실패 task=%s",
                    attempts, max_replans, (run.exec.failed or {}).get("task"))
        new_plan = build_plan(planner_llm, user_message, skill_tools, feedback=feedback)
        if new_plan is None:
            break  # 교정 불가(planner 가 포기) → 기존 실패 run 유지 → ReAct
        logger.info("[replan] 교정 plan %d tasks: %s", len(new_plan.tasks),
                    [(t.id, t.skill, t.kind, t.deps) for t in new_plan.tasks])
        run = PlanRun(plan=new_plan, exec=execute_plan(
            db, new_plan, ctx, execute_fn, session_factory=session_factory,
            collect_clarifications=collect_clarifications))
    return run


def join_answer(llm: Any, user_message: str, run: PlanRun, correction: str | None = None) -> str:
    """전 task 출력으로 최종 답변 합성 (LLMCompiler [4] Joiner). 데이터에만 근거.

    correction: L2 검증이 직전 답변의 불일치를 지적하면, 그 사유를 넣어 데이터에 더 엄격히 재생성.
    """
    payload = {tid: out for tid, out in run.exec.outputs.items()}
    blob = json.dumps(payload, ensure_ascii=False, default=str)[:4000]
    sys = ("너는 계획 실행 결과를 사용자에게 자연어로 답하는 역할이다. "
           "아래 task 출력 데이터에만 근거해 간결·정확히 답하라. 데이터에 없는 것을 지어내지 마라.")
    if correction:
        sys += (f"\n[검증] 직전 답변이 데이터와 어긋났다: {correction}. "
                "데이터에 없는 수치·이름·상태·완료여부를 절대 말하지 마라.")
    user = f"[사용자 요청]\n{user_message}\n\n[task 실행 결과]\n{blob}"
    try:
        resp = llm.chat([{"role": "system", "content": sys},
                         {"role": "user", "content": user}], tools=[])
        return (getattr(resp, "text", None) or "").strip()
    except Exception as e:  # noqa: BLE001
        logger.warning("[plan] join 실패: %s", e)
        return ""
