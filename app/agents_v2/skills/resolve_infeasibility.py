"""resolve-infeasibility skill — 근무표 생성 실패의 해결 옵션 카탈로그 (read-only).

사용자 발화 예:
  - "이번 실패 어떻게 풀어?"
  - "원인은 알겠고, 어떤 옵션 있어?"
  - "7월 근무표 실패 해결 방법 알려줘"

데이터 소스:
  최근 FAILED RosterJob 의 error_message JSON (worker.py 가 저장한
  unrecoverable_payload). 그 안의 resolution_narrative.action_levers +
  treatment_recommendations.apply_hint 를 treatment_id 로 조인.

정책:
  - read-only. 적용은 별도 mutation 스킬(manage_team_min/manage_grade/...)에
    LLM 이 chain 으로 위임.
  - apply_hint 는 _internal 격리 (LLM 다음 chain 단서이지 사용자 노출용 아님).
  - 사용자 메시지는 한국어 deterministic 텍스트.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from agents_v2.skills.registry import register
from agents_v2.tools import generation_tools
from services.semantics import get_default_ontology


def _parse_payload(error_message: str | None) -> dict | None:
    """RosterJob.error_message 가 JSON 직렬화된 payload 면 dict 반환."""
    if not error_message:
        return None
    s = error_message.lstrip()
    if not s.startswith("{"):
        return None
    try:
        return json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return None


def _treatment_apply_hints(payload: dict | None) -> dict[str, dict[str, Any]]:
    """payload → {treatment_id: apply_hint dict}."""
    if not isinstance(payload, dict):
        return {}
    infeas = payload.get("infeasibility") or {}
    recs = infeas.get("treatment_recommendations") or []
    out: dict[str, dict[str, Any]] = {}
    for r in recs:
        if not isinstance(r, dict):
            continue
        tid = r.get("treatment_id")
        hint = r.get("apply_hint")
        if tid and hint is not None:
            out[str(tid)] = hint
    return out


def _trade_offs_by_treatment(narrative: dict | None) -> dict[str, str]:
    if not isinstance(narrative, dict):
        return {}
    out: dict[str, str] = {}
    for t in (narrative.get("trade_offs") or []):
        if not isinstance(t, dict):
            continue
        tid = t.get("treatment_id")
        msg = t.get("trade_off_ko")
        if tid and msg:
            out[str(tid)] = str(msg)
    return out


def _ontology_meta_for_option(
    target_family: str | None,
    treatment_id: str,
) -> dict[str, Any] | None:
    """target_family + treatment_id → ontology Constraint/Treatment 메타 부착.

    constraint 메타: group / default_severity / runtime_lever / runtime_note / value_source
    treatment 메타: runtime_lever (treatment 노드 단위 — constraint 보다 더 정확)
    """
    if not target_family and not treatment_id:
        return None
    onto = get_default_ontology()
    meta: dict[str, Any] = {}
    if target_family:
        cid = onto.resolve_alias(target_family)
        entry = onto.get_constraint(cid) if cid else None
        if entry is not None:
            meta["constraint_id"] = entry.constraint_id
            meta["group"] = entry.parent
            meta["default_severity"] = entry.default_severity
            meta["runtime_lever"] = entry.runtime_lever
            if entry.runtime_note:
                meta["runtime_note"] = entry.runtime_note
            if entry.value_source:
                meta["value_source"] = entry.value_source
    if treatment_id:
        tr = onto.get_treatment(treatment_id) if hasattr(onto, "get_treatment") else None
        if tr is not None:
            # treatment 단위 runtime_lever 가 false 이면 constraint 단보다 우선.
            if tr.runtime_lever is False:
                meta["runtime_lever"] = False
            if tr.runtime_note and "runtime_note" not in meta:
                meta["runtime_note"] = tr.runtime_note
    return meta or None


def _compose_options(
    narrative: dict | None,
    trade_offs: dict[str, str],
) -> list[dict[str, Any]]:
    if not isinstance(narrative, dict):
        return []
    out: list[dict[str, Any]] = []
    for a in (narrative.get("action_levers") or []):
        if not isinstance(a, dict):
            continue
        tid = str(a.get("treatment_id") or "")
        target_family = a.get("target_family")
        entry: dict[str, Any] = {
            "treatment_id": tid,
            "target_family": target_family,
            "config_key": a.get("config_key"),
            "direction": a.get("direction"),
            "rationale_ko": a.get("rationale_ko") or "",
            "covers_causes": a.get("covers_causes") or [],
        }
        if tid in trade_offs:
            entry["trade_off_ko"] = trade_offs[tid]
        ont_meta = _ontology_meta_for_option(target_family, tid)
        if ont_meta is not None:
            entry["constraint"] = ont_meta
            # runtime_lever=false 인 옵션은 사용자에게 자동 처리됨을 알림.
            if ont_meta.get("runtime_lever") is False:
                entry["engine_self_resolves"] = True
        out.append(entry)
    return out


def _partition_options(
    options: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """runtime_lever 기준 분리.

    actionable: 사용자가 실제로 누를 수 있는 옵션
    auto_resolved: 엔진 자동 처리되거나 운영 미사용인 옵션 (engine_self_resolves=True)
    """
    actionable: list[dict[str, Any]] = []
    auto: list[dict[str, Any]] = []
    for opt in options:
        if opt.get("engine_self_resolves") is True:
            auto.append(opt)
        else:
            actionable.append(opt)
    return actionable, auto


def _compose_message(
    year: int | None,
    month: int | None,
    actionable: list[dict[str, Any]],
    auto_resolved: list[dict[str, Any]],
    summary_ko: str | None,
) -> str:
    period = f"{year}년 {month}월 " if year and month else ""
    auto_n = len(auto_resolved)
    auto_suffix = (
        f" (그 외 {auto_n}건은 엔진이 자동으로 처리합니다.)" if auto_n else ""
    )
    if not actionable:
        if auto_n:
            return (
                f"{period}사용자가 직접 조정할 해결 옵션은 없어요. "
                f"{auto_n}건은 엔진이 자동으로 처리합니다."
            )
        return (
            f"{period}근무표 실패의 해결 옵션을 찾지 못했어요. "
            "실패 원인부터 확인이 필요해요."
        )
    head = (summary_ko or "").strip()
    head = f"{head} " if head else ""
    bullets = []
    for i, opt in enumerate(actionable[:5], start=1):
        rationale = (opt.get("rationale_ko") or "").strip()
        if rationale:
            bullets.append(f"{i}) {rationale}")
    bullet_text = " ".join(bullets) if bullets else ""
    suffix = f" 어떤 옵션으로 진행할까요?{auto_suffix}"
    if bullet_text:
        return f"{head}{period}해결 옵션 {len(actionable)}개가 있어요: {bullet_text}.{suffix}"
    return f"{head}{period}해결 옵션 {len(actionable)}개가 있어요.{suffix}"


@register("resolve-infeasibility")
def resolve_infeasibility(db: Session, params: dict) -> Any:
    """근무표 생성 실패의 해결 옵션 카탈로그 반환.

    params:
      group_id (필수, RBAC)
      office_id (선택)
      operation (선택) — 기본 'list_options'. 그 외는 clarification.
    """
    group_id = params.get("group_id")
    if not group_id:
        return {"error": "group_id required (RBAC scope)"}

    op = (params.get("operation") or "list_options").lower()
    if op != "list_options":
        return {
            "needs_clarification": True,
            "question": "지원하는 동작은 'list_options' 입니다. 다른 동작이 필요하신가요?",
            "options": ["해결 옵션 목록 보기 (list_options)"],
        }

    # year/month 가 지정되면 그 달의 가장 최근 FAILED job 우선 검색.
    # (worker.py 의 _year/_month 메타가 generation_tools._job_dict 에서
    #  year/month 필드로 노출됨.)
    year_p = params.get("year")
    month_p = params.get("month")
    office_id = params.get("office_id")

    if year_p is not None and month_p is not None:
        year_i, month_i = int(year_p), int(month_p)
        recent = generation_tools.list_recent_jobs(
            db, group_id, status_filter="FAILED", limit=20,
        )
        if office_id:
            recent = [r for r in recent if r.get("office_id") == office_id]
        job = next(
            (r for r in recent
             if r.get("year") == year_i and r.get("month") == month_i),
            None,
        )
        if not job:
            return {
                "found": False,
                "operation": "list_options",
                "year": year_i, "month": month_i,
                "message": (
                    f"{year_i}년 {month_i}월 근무표 생성 실패 기록을 찾지 못했어요. "
                    "다른 month 거나 아직 그 달 실패 시도가 없을 수 있어요."
                ),
            }
    else:
        job = generation_tools.get_latest_job(
            db, group_id, office_id=office_id,
        )
        if not job:
            return {
                "found": False,
                "operation": "list_options",
                "message": "최근 근무표 생성 기록이 없어요. 근무표를 만들어보시면 실패 원인과 해결 옵션을 보여드릴게요.",
            }
        if (job.get("status") or "").upper() != "FAILED":
            return {
                "found": False,
                "operation": "list_options",
                "status": job.get("status"),
                "message": (
                    "최근 근무표 생성은 실패하지 않았어요. 해결 옵션이 필요한 시점이 아닙니다."
                ),
            }

    payload = _parse_payload(job.get("error_message"))
    narrative = (payload or {}).get("infeasibility", {}).get("resolution_narrative") \
        if isinstance(payload, dict) else None
    summary_ko = (narrative or {}).get("summary_ko") if isinstance(narrative, dict) else None

    all_options = _compose_options(narrative, _trade_offs_by_treatment(narrative))
    actionable, auto_resolved = _partition_options(all_options)
    apply_hints = _treatment_apply_hints(payload)
    year = job.get("year")
    month = job.get("month")

    internal: dict[str, Any] = {"job_id": job.get("job_id")}
    if apply_hints:
        internal["apply_hints"] = apply_hints
    if payload is not None:
        internal["debug_payload"] = payload

    return {
        "found": True,
        "operation": "list_options",
        "year": year,
        "month": month,
        "summary_ko": summary_ko,
        # 사용자가 직접 누를 수 있는 옵션만 노출. runtime_lever=false 는 분리.
        "options": actionable,
        "auto_resolved_options": auto_resolved,
        "verified": bool((narrative or {}).get("verified")) if isinstance(narrative, dict) else False,
        "message": _compose_message(year, month, actionable, auto_resolved, summary_ko),
        "_internal": internal,
    }
