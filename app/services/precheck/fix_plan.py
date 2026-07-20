"""Actionable infeasibility fix-plan builder.

NO_ASSIGNMENT처럼 conflict_cores가 비는 케이스에서도
운영자가 바로 수정할 수 있는 우선순위 액션을 생성한다.

v2: ontology 4-tier (T0/T1/T2/T3) + axis_registry 기반 axis-decomposed actions.
  - T0 (절대/물리 hard): data_correction_required=True, 입력 정비 가이드
  - T1 (안전 hard): protected_axes 로 격리, actions 에 절대 포함 X
  - T2 (운영 hard): axis_actions (최대 5개), relaxation_priority 오름차순
  - T3 (품질 soft): quality_hints (현재 v1 family 미정의)
기존 actions[] 는 backward-compat 유지.
"""

from __future__ import annotations

from typing import Any, Dict, List

from services.semantics.axis_registry import (
    AxisDefinition,
    AxisRegistry,
    get_default_axis_registry,
)


_MAX_AXIS_ACTIONS = 5
_MAX_AXIS_TARGETS = 5


# Stage taxonomy (S4): solver/validator의 어느 단계에서 막혔는지를
# 기존 evidence(reasons, patterns, structural_diagnosis, applied_relaxations)로부터
# heuristic 으로 추론한다. 솔버 본체 침습 없음.
_STAGE_LABELS_KO = {
    "S0_precheck": "S0 사전 점검 (설정/입력 정합성)",
    "S1_night_skeleton": "S1 Night 스켈레톤 배치",
    "S2_recovery_offwindow": "S2 회복 OFF / 월경계",
    "S3_day_eve_coverage": "S3 Day/Eve 커버리지",
    "S4_lex_fallback": "S4 lex fallback 보정",
    "unknown": "단계 미확정",
}


def _infer_failure_stage(
    *,
    reasons: list[str],
    patterns: set[str],
    applied_relaxations: list[str] | None = None,
    failed_cell_shifts: set[str] | None = None,
) -> str:
    """heuristic — 단일 stage 신호일 때만 결정. 다중 신호면 'unknown' 반환.

    Filtering safety: 다중 stage 가 동시에 나타나면 stage scope 로 axis 를
    잘못 잘라낼 위험이 크므로 보수적으로 unknown 처리.
    """
    codes = {r.upper() for r in reasons}
    pats = {p.lower() for p in patterns}
    relax = list(applied_relaxations or [])

    candidates: list[str] = []

    if codes & {
        "MID_REQUIRED_MISSING",
        "MID_DISABLED_BUT_USED",
        "ALLOWED_SHIFTS_ISOLATES_NURSE",
        "FIXED_ASSIGN_EXCEEDS_NEED",
        "FIXED_ASSIGN_VIOLATES_ALLOWED",
    }:
        candidates.append("S0_precheck")

    if any("lex" in str(x).lower() for x in relax) or any("fallback" in p for p in pats):
        candidates.append("S4_lex_fallback")

    if any(c.startswith("PREV_") for c in codes) or any(
        ("carryover" in p) or ("boundary" in p) or ("recovery" in p)
        for p in pats
    ):
        candidates.append("S2_recovery_offwindow")

    shifts = {s.upper() for s in (failed_cell_shifts or set()) if s}
    if codes & {"MONTHLY_NIGHT_CAPACITY_SHORTAGE", "N_CAPACITY_SHORTAGE"} or shifts == {"N"}:
        candidates.append("S1_night_skeleton")

    if (
        codes & {
            "GLOBAL_DAY_CAPACITY_SHORTAGE",
            "TEAM_MIN_EXCEEDS_GLOBAL_NEED",
            "GRADE_MIN_SUM_EXCEEDS_NEED",
        }
        or (shifts & {"D", "E", "M"})
    ):
        candidates.append("S3_day_eve_coverage")

    if len(candidates) == 1:
        return candidates[0]
    return "unknown"


def _axis_in_stage_scope(axis_id: str, stage: str) -> bool:
    """stage 가 axis 와 의미상 정합인지. unknown 이면 모두 허용."""
    if stage in ("unknown", "S4_lex_fallback"):
        return True
    if stage == "S0_precheck":
        # 입력 정비 단계 — 일부 capacity 권고 외 모두 표시 (T0가 별도로 메시지를 가짐)
        return True
    if stage == "S1_night_skeleton":
        return axis_id in {
            "night_capacity",
            "monthly_n_cap",
            "team_min",  # team min for N
            "grade_min",  # grade min for N
            "allowed_shift_mask",
            "role_isolation",
        }
    if stage == "S2_recovery_offwindow":
        return axis_id in {
            "carryover_transition",
            "carryover_recovery",
            "carryover_consecutive_work",
            "off_cap",
            "consecutive_work_limit",
        }
    if stage == "S3_day_eve_coverage":
        return axis_id in {
            "day_capacity",
            "evening_capacity",
            "mid_capacity",
            "team_min",
            "grade_min",
            "grade_max",
            "fixed_excess",
            "fixed_violates_allowed",
            "fixed_breaks_team_min",
        }
    return True


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return default


def _collect_patterns(conflict_cores: List[Dict[str, Any]]) -> set[str]:
    pats: set[str] = set()
    for c in conflict_cores or []:
        if isinstance(c, dict):
            p = str(c.get("pattern") or "").lower()
            if p:
                pats.add(p)
    return pats


def _pool_shortage_axis_hint(pool_id: str) -> str | None:
    """Pool id prefix → axis_id 추정.

    team_pool:<team>:<shift>     → team_min
    grade_pool:<grade>:<shift>   → grade_min
    common_pool:<shift>          → {shift} capacity (night/day/evening_capacity)
    """
    pid = (pool_id or "").lower()
    if pid.startswith("team_pool"):
        return "team_min"
    if pid.startswith("grade_pool"):
        return "grade_min"
    if pid.startswith("common_pool"):
        if pid.endswith(":n"):
            return "night_capacity"
        if pid.endswith(":d"):
            return "day_capacity"
        if pid.endswith(":e"):
            return "evening_capacity"
        if pid.endswith(":m"):
            return "mid_capacity"
    return None


def _axis_target_from_failed_cell(cell: Dict[str, Any]) -> Dict[str, Any] | None:
    day = cell.get("day")
    shift = str(cell.get("shift") or "")
    if day is None or not shift:
        return None
    return {
        "target_type": "daily_requirement",
        "day": int(day),
        "shift": shift,
        "shortage": _safe_int(cell.get("shortage"), 0),
        "suggested_delta": -1,
    }


def _axis_targets_from_pool_shortages(
    axis: AxisDefinition,
    shortages: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for s in shortages or []:
        pool_id = str(s.get("pool_id") or s.get("pool") or "")
        if _pool_shortage_axis_hint(pool_id) != axis.axis_id:
            continue
        out.append({"pool_id": pool_id, "shortage": _safe_int(s.get("shortage"), 0)})
    return out[:_MAX_AXIS_TARGETS]


def _axis_targets_for_capacity_axis(
    axis: AxisDefinition,
    validator_evidence: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """night_capacity / day_capacity / evening_capacity / mid_capacity 는
    top_failed_cells 의 shift 와 매칭되는 셀들만 모은다."""
    shift_key = (axis.config_lever or {}).get("key")
    if shift_key not in ("D", "E", "N", "M"):
        return []
    cells = list((validator_evidence or {}).get("top_failed_cells") or [])
    out: List[Dict[str, Any]] = []
    for c in cells:
        if str(c.get("shift") or "").upper() != shift_key:
            continue
        t = _axis_target_from_failed_cell(c)
        if t:
            out.append(t)
    out.sort(key=lambda x: -x.get("shortage", 0))
    return out[:_MAX_AXIS_TARGETS]


def _build_axis_action(
    *,
    axis: AxisDefinition,
    registry: AxisRegistry,
    reasons: List[str],
    validator_evidence: Dict[str, Any],
    shortages: List[Dict[str, Any]],
    priority: int,
) -> Dict[str, Any]:
    tier = registry.tier_of(axis)
    relax_prio = registry.relaxation_priority_of(axis)
    targets: List[Dict[str, Any]] = []
    if axis.lock_type == "capacity_shortage":
        targets.extend(_axis_targets_for_capacity_axis(axis, validator_evidence))
        targets.extend(_axis_targets_from_pool_shortages(axis, shortages))
    # dedupe
    seen: set[str] = set()
    deduped: List[Dict[str, Any]] = []
    for t in targets:
        k = f"{t.get('pool_id') or ''}|{t.get('day')}|{t.get('shift')}"
        if k in seen:
            continue
        seen.add(k)
        deduped.append(t)
        if len(deduped) >= _MAX_AXIS_TARGETS:
            break

    direction = axis.direction_hint
    direction_phrase = {
        "decrease": "1 줄여보세요",
        "increase": "1 늘려보세요",
        "review": "검토 후 조정해보세요",
    }.get(direction, "조정해보세요")

    if deduped:
        scope_hint = ", ".join(
            f"{t.get('day') or ''}일 {t.get('shift') or ''}".strip()
            for t in deduped[:3]
            if t.get("day") or t.get("shift")
        ) or "해당 항목"
        msg = f"{axis.label_ko}을 {direction_phrase} (예: {scope_hint})"
    else:
        msg = f"{axis.label_ko}을 {direction_phrase}"

    return {
        "priority": priority,
        "axis_id": axis.axis_id,
        "family": axis.family,
        "tier": tier,
        "lock_type": axis.lock_type,
        "relaxation_priority": relax_prio,
        "label_ko": axis.label_ko,
        "human_message_ko": msg,
        "direction_hint": direction,
        "config_lever": dict(axis.config_lever or {}),
        "targets": deduped,
        "evidence": {
            "matched_reason_codes": [c for c in axis.matches_reason_codes if c in reasons],
            "matched_patterns": list(axis.matches_patterns),
        },
    }


def _reason_codes(preflight_issues: List[Dict[str, Any]], violated_constraints: List[Dict[str, Any]]) -> List[str]:
    out: List[str] = []
    for item in (preflight_issues or []):
        code = str(item.get("reason_code") or "").upper().strip()
        if code and code not in out:
            out.append(code)
    for item in (violated_constraints or []):
        code = str(item.get("reason_code") or "").upper().strip()
        if code and code not in out:
            out.append(code)
    return out


def _extract_validator_evidence_from_violations(violated_constraints: List[Dict[str, Any]]) -> Dict[str, Any]:
    for v in (violated_constraints or []):
        if not isinstance(v, dict):
            continue
        d = v.get("details") or {}
        if not isinstance(d, dict):
            continue
        ev = d.get("validator_evidence")
        if isinstance(ev, dict) and ev:
            return ev
    return {}


def _bounded_step_hint(shortages: List[Dict[str, Any]]) -> Dict[str, int]:
    """보수적 단계 조정 힌트.

    정밀 최적값 계산 대신, 운영 리스크를 줄이기 위한 소폭 조정 범위를 제시한다.
    """
    if not shortages:
        return {"min": 1, "max": 1}
    max_shortage = max(_safe_int(s.get("shortage"), 0) for s in shortages)
    if max_shortage >= 5:
        return {"min": 1, "max": 2}
    return {"min": 1, "max": 1}


def build_fix_plan(
    *,
    structural_diagnosis: Dict[str, Any] | None,
    preflight_issues: List[Dict[str, Any]] | None,
    violated_constraints: List[Dict[str, Any]] | None,
    conflict_cores: List[Dict[str, Any]] | None,
    pool_snapshot: Dict[str, Any] | None,
    applied_relaxations: List[str] | None = None,
) -> Dict[str, Any]:
    diagnosis = structural_diagnosis or {}
    reasons = _reason_codes(preflight_issues or [], violated_constraints or [])
    shortages = list((pool_snapshot or {}).get("shortages") or [])
    mode = str(diagnosis.get("mode") or "relaxation_candidate")
    has_core_evidence = bool(conflict_cores)
    validator_evidence = _extract_validator_evidence_from_violations(list(violated_constraints or []))
    no_assignment = ("NO_ASSIGNMENT" in reasons) or any(r.startswith("NO_ASSIGNMENT_") for r in reasons)
    no_assignment_breakdown: List[str] = []

    direct_map = {
        "NO_ASSIGNMENT_CAPACITY": "capacity_shortage",
        "NO_ASSIGNMENT_ELIGIBILITY": "eligibility_lock",
        "NO_ASSIGNMENT_FIXED": "fixed_lock",
        "NO_ASSIGNMENT_CARRYOVER": "carryover_lock",
    }
    direct_breakdown = [v for k, v in direct_map.items() if k in reasons]
    if no_assignment and direct_breakdown:
        no_assignment_breakdown.extend(direct_breakdown)

    actions: List[Dict[str, Any]] = []

    if shortages:
        if no_assignment:
            no_assignment_breakdown.append("capacity_shortage")
        step_hint = _bounded_step_hint(shortages)
        top = sorted(shortages, key=lambda s: -_safe_int(s.get("shortage"), 0))[:5]
        targets = []
        for s in top:
            pool_id = str(s.get("pool_id") or s.get("pool") or "")
            shortage = _safe_int(s.get("shortage"), 0)
            if pool_id:
                targets.append({"pool_id": pool_id, "shortage": shortage})
        actions.append(
            {
                "priority": 1,
                "action_id": "adjust_coverage_or_supply",
                "title_ko": "수요/공급 불일치 먼저 해소",
                "why_ko": "NO_ASSIGNMENT 또는 pool shortage가 있으면 상세 core 이전에 배정이 0으로 종료될 수 있습니다.",
                "how_ko": [
                    "부족한 team/grade/shift 수요를 소폭(한 번에 크게 말고) 단계적으로 완화하세요.",
                    "해당 shift 투입 가능 인원을 1~2명 범위의 작은 단계로 늘리고, 매 단계마다 재실행해 확인하세요.",
                ],
                "targets": targets,
                "bounded_adjustment": step_hint,
                "guardrail_ko": "대규모 구조 변경(예: 특정 역할/전담 일괄 해제) 대신 소폭 조정 후 재검증을 반복하세요.",
                "evidence": {"reason_codes": reasons, "shortage_count": len(shortages)},
            }
        )
    elif no_assignment and "NO_ASSIGNMENT_CAPACITY" in reasons:
        # pool shortage가 비어도 validator evidence에서 실패 셀 기반으로 구체 조정안 생성
        top_cells = list((validator_evidence or {}).get("top_failed_cells") or [])[:5]
        targets = []
        for c in top_cells:
            day = c.get("day")
            shift = str(c.get("shift") or "")
            shortage = _safe_int(c.get("shortage"), 0)
            if day is None or not shift:
                continue
            targets.append(
                {
                    "target_type": "daily_requirement",
                    "day": int(day),
                    "shift": shift,
                    "suggested_delta": -1,
                    "shortage": shortage,
                }
            )
        if targets:
            actions.append(
                {
                    "priority": 1,
                    "action_id": "adjust_daily_requirement_stepwise",
                    "title_ko": "문제 셀의 일일 필요 인원을 1단계 조정",
                    "why_ko": "실패가 집중된 day/shift 셀이 확인되어 해당 셀부터 소폭 조정하는 것이 가장 안전합니다.",
                    "how_ko": [
                        "아래 제시된 day/shift에서 필요 인원을 1씩만 낮춰 재실행하세요.",
                        "성공 전환되면 추가 변경 없이 종료하고, 실패면 다음 셀 1건만 추가 조정하세요.",
                    ],
                    "targets": targets,
                    "guardrail_ko": "대량 조정 금지: 한 번에 1~2 셀만 조정 후 재실행하세요.",
                    "evidence": {
                        "reason_codes": reasons,
                        "total_failed_cells": int((validator_evidence or {}).get("total_failed_cells") or 0),
                    },
                }
            )

    has_role_signal = any(
        c in reasons for c in ("ALLOWED_SHIFTS_ISOLATES_NURSE", "TEAM_SHIFT_ALLOWED_SHORTAGE")
    ) or any("allowed_shift" in str((cc or {}).get("pattern") or "") for cc in (conflict_cores or []))
    if has_role_signal:
        if no_assignment:
            no_assignment_breakdown.append("eligibility_lock")
        actions.append(
            {
                "priority": len(actions) + 1,
                "action_id": "relax_allowed_shift_or_role_lock",
                "title_ko": "허용 시프트/역할 잠금 완화",
                "why_ko": "투입 가능한 간호사가 role/allowed-shift로 고립되면 배정이 막힙니다.",
                "how_ko": [
                    "allowed shift mask를 소수 인원부터 점검해 최소 1개 이상 실배정 가능 시프트를 보장하세요.",
                    "팀/등급 분리 규칙은 전면 해제 대신 1개 제약씩 단계적으로 완화하세요.",
                ],
                "targets": [],
                "guardrail_ko": "N전담 일괄 해제 같은 급격한 변경은 피하고, 영향 범위가 작은 항목부터 조정하세요.",
                "evidence": {"reason_codes": reasons},
            }
        )

    has_fixed_signal = any(
        c in reasons for c in ("FIXED_ASSIGN_EXCEEDS_NEED", "FIXED_ASSIGN_VIOLATES_ALLOWED", "FIXED_ASSIGN_BREAKS_TEAM_MIN")
    ) or any(
        (
            "fixed" in str((cc or {}).get("pattern") or "")
            or "initial_forbidden" in str((cc or {}).get("pattern") or "")
        )
        for cc in (conflict_cores or [])
    )
    has_carryover_signal = any(rc.startswith("PREV_") for rc in reasons) or any(
        "carryover" in str((cc or {}).get("pattern") or "") for cc in (conflict_cores or [])
    )
    if has_fixed_signal:
        if no_assignment:
            no_assignment_breakdown.append("fixed_lock")
        actions.append(
            {
                "priority": len(actions) + 1,
                "action_id": "rebalance_fixed_and_carryover",
                "title_ko": "고정 배정/월경계 회복 규칙 재배치",
                "why_ko": "월초 고정 및 carryover 회복 규칙이 겹치면 feasible 영역이 급격히 줄어듭니다.",
                "how_ko": [
                    "월초 고정 셀을 일부(소수)만 분산/해제하고 결과를 확인하세요.",
                    "2N/3N 회복 규칙과 충돌하는 셀을 우선순위 순으로 1~2건씩 조정하세요.",
                ],
                "targets": [],
                "guardrail_ko": "고정/경계 규칙을 한 번에 대량 해제하지 말고, 최소 변경 후 재실행하세요.",
                "evidence": {"reason_codes": reasons},
            }
        )

    if no_assignment and has_carryover_signal:
        no_assignment_breakdown.append("carryover_lock")

    if not actions:
        actions.append(
            {
                "priority": 1,
                "action_id": "collect_more_signals",
                "title_ko": "추가 진단 신호 확보",
                "why_ko": "현재 증거가 부족해 구체 수정 대상을 좁히기 어렵습니다.",
                "how_ko": [
                    "precheck 통과 후 solver infeasible 구간이 나오도록 조건을 단계적으로 완화하세요.",
                    "재실행 후 conflict_cores 패턴을 기준으로 우선순위를 확정하세요.",
                ],
                "targets": [],
                "guardrail_ko": "한 번에 큰 폭으로 바꾸지 말고, 변경 1개당 실행 1회를 원칙으로 하세요.",
                "evidence": {"reason_codes": reasons, "mode": mode},
            }
        )

    plan_mode = "detected_causes" if has_core_evidence else "hypothesis_checks"
    summary = (
        "infeasible 원인 신호를 기반으로 수정 우선순위를 제안합니다."
        if has_core_evidence
        else "conflict core가 비어 있어 가설 기반 점검 순서로 안내합니다."
    )

    # ---- v2: tier + axis layer ----------------------------------------
    registry = get_default_axis_registry()
    ontology = registry.ontology
    patterns = _collect_patterns(list(conflict_cores or []))
    axes_matched: list[AxisDefinition] = registry.axes_for_reasons(
        reason_codes=set(reasons),
        patterns=patterns,
    )
    # pool shortage 가 있는데 reason_code 가 비어 있어도 axis 추론
    for s in shortages or []:
        hint = _pool_shortage_axis_hint(str(s.get("pool_id") or s.get("pool") or ""))
        if not hint:
            continue
        a = registry.get(hint)
        if a and a not in axes_matched:
            axes_matched.append(a)
    # validator_evidence 의 top_failed_cells 에서 axis 추론
    # 우선 blocking_axes (S3) 사용, fallback 으로 shift→axis 매핑
    for c in (validator_evidence or {}).get("top_failed_cells") or []:
        cell_axes = list(c.get("blocking_axes") or [])
        if not cell_axes:
            sh = str(c.get("shift") or "").upper()
            axis_id = {
                "N": "night_capacity",
                "D": "day_capacity",
                "E": "evening_capacity",
                "M": "mid_capacity",
            }.get(sh)
            if axis_id:
                cell_axes = [axis_id]
        for aid in cell_axes:
            a = registry.get(aid)
            if a and a not in axes_matched:
                axes_matched.append(a)

    axes_sorted = registry.sort_for_user(axes_matched)

    # S4: heuristic failure-stage 추론
    failed_cell_shifts = {
        str(c.get("shift") or "").upper()
        for c in (validator_evidence or {}).get("top_failed_cells") or []
        if c.get("shift")
    }
    failure_stage = _infer_failure_stage(
        reasons=reasons,
        patterns=patterns,
        applied_relaxations=applied_relaxations,
        failed_cell_shifts=failed_cell_shifts,
    )
    # S5: stage 가 결정적일 때 (unknown / S0 / S4 외) axis scope 좁힘
    if failure_stage in ("S1_night_skeleton", "S2_recovery_offwindow", "S3_day_eve_coverage"):
        axes_sorted = [a for a in axes_sorted if _axis_in_stage_scope(a.axis_id, failure_stage)]

    t0_axes = [a for a in axes_sorted if ontology.get_tier(a.family) == "T0"]
    t1_axes = [a for a in axes_sorted if ontology.get_tier(a.family) == "T1"]
    t2_axes = [a for a in axes_sorted if ontology.get_tier(a.family) == "T2"]
    t3_axes = [a for a in axes_sorted if ontology.get_tier(a.family) == "T3"]

    # T0 detection via raw reason_codes → family resolution (axis 없어도 감지)
    t0_families: set[str] = set()
    for rc in reasons:
        fam = ontology.resolve_alias(rc)
        if fam and ontology.get_tier(fam) == "T0":
            t0_families.add(fam)

    tier_summary = {
        "T0": len(t0_axes) + len(t0_families - {a.family for a in t0_axes}),
        "T1": len(t1_axes),
        "T2": len(t2_axes),
        "T3": len(t3_axes),
    }

    protected_axes: List[Dict[str, Any]] = [
        {
            "axis_id": a.axis_id,
            "family": a.family,
            "tier": "T1",
            "label_ko": a.label_ko,
            "why_protected_ko": "안전·법규성 룰입니다. 자동 완화 대상이 아니며 수동 검토만 가능합니다.",
        }
        for a in t1_axes
    ]

    axis_actions: List[Dict[str, Any]] = []
    for idx, a in enumerate(t2_axes[:_MAX_AXIS_ACTIONS], start=1):
        axis_actions.append(
            _build_axis_action(
                axis=a,
                registry=registry,
                reasons=reasons,
                validator_evidence=validator_evidence,
                shortages=shortages,
                priority=idx,
            )
        )

    data_correction_required = bool(t0_axes) or bool(t0_families)
    data_correction_message_ko = (
        "수학적으로 풀 수 없는 케이스입니다. 룰 완화가 아닌 입력 데이터(요구 인원·인력풀·고정 배정)를 직접 정비하세요."
        if data_correction_required
        else None
    )
    data_correction_families = sorted(
        {a.family for a in t0_axes} | t0_families
    )

    axis_actions_truncated = max(0, len(t2_axes) - _MAX_AXIS_ACTIONS)

    plan: Dict[str, Any] = {
        "mode": mode,
        "plan_mode": plan_mode,
        "reason_source": "direct" if direct_breakdown else "inferred",
        "no_assignment_breakdown": sorted(set(no_assignment_breakdown)) if no_assignment else [],
        "summary_ko": summary,
        "actions": actions,
        # v2 tier + axis fields
        "tier_summary": tier_summary,
        "protected_axes": protected_axes,
        "axis_actions": axis_actions,
        "axis_actions_cap": _MAX_AXIS_ACTIONS,
        "axis_actions_truncated": axis_actions_truncated,
        "data_correction_required": data_correction_required,
        "failure_stage": failure_stage,
        "failure_stage_label_ko": _STAGE_LABELS_KO.get(failure_stage, _STAGE_LABELS_KO["unknown"]),
    }
    if data_correction_message_ko:
        plan["data_correction_message_ko"] = data_correction_message_ko
    if data_correction_families:
        plan["data_correction_families"] = data_correction_families
    return plan
