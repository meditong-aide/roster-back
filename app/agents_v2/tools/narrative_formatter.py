"""narrative_formatter — build_unrecoverable_payload 결과를 agent-friendly dict 로 평탄화."""

from __future__ import annotations

from typing import Any


def format_infeasibility(payload: dict | None) -> dict | None:
    """payload (build_unrecoverable_payload 결과) → LLM 이 읽기 쉬운 narrative dict.

    None / 빈 dict 입력은 None 반환. resolution_narrative 가 없으면 raw payload
    의 핵심 필드만 추려서 반환.
    """
    if not payload or not isinstance(payload, dict):
        return None

    infeas = payload.get("infeasibility") or {}
    narrative = payload.get("resolution_narrative") or {}
    hard_case = payload.get("hard_case") or {}
    apply_hint = payload.get("apply_hint") or {}

    problems = [
        p.get("rendered_ko")
        for p in (narrative.get("problem_list") or [])
        if isinstance(p, dict) and p.get("rendered_ko")
    ]
    actions = [
        a.get("rationale_ko")
        for a in (narrative.get("action_levers") or [])
        if isinstance(a, dict) and a.get("rationale_ko")
    ]
    trade_offs = [
        t.get("trade_off_ko")
        for t in (narrative.get("trade_offs") or [])
        if isinstance(t, dict) and t.get("trade_off_ko")
    ]

    return {
        "infeasible": True,
        "severity": infeas.get("severity"),
        "summary_ko": narrative.get("summary_ko") or infeas.get("summary_message_ko") or "",
        "problems": problems,
        "actions": actions,
        "trade_offs": trade_offs,
        "hard_case": _coerce_hard_case(hard_case),
        "apply_hint": apply_hint or None,
    }


def _coerce_hard_case(hc: Any) -> dict | None:
    """hard_case 객체에서 LLM 이 알아야 할 정보만 추출."""
    if not isinstance(hc, dict):
        return None
    is_hard = hc.get("is_hard_case") or hc.get("hard_case")
    if not is_hard:
        return None
    return {
        "is_hard_case": True,
        "reason_ko": hc.get("reason_ko") or hc.get("reason") or "",
    }
