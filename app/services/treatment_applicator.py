"""Treatment apply orchestrator — Phase 4 (Verified Resolution).

설계 원칙 (사용자 핵심 요구):
  - treatment_id list 받음 → ontology lookup → ConstraintAdjustment 로 변환
  - control.apply_adjustments_to_config 가 deepcopy + 패치 (멱등)
  - dry_run: config 패치 후 산술 detector 재실행 (solver 호출 X)
  - actual: caller (router 또는 service) 가 solver 재호출, 결과로 evidence 갱신
  - data_correction_required treatment 는 auto 적용 안 됨 (manual only)

에이전트 친화 (추후 통합 대비):
  - 멱등성: 같은 (treatment_ids, config) → 같은 patched_config
  - 부분 적용: list 1개부터 N개까지
  - 컴팩트 결과: ApplyResult dataclass (full schedule 안 토함)
  - rollback: original_config_snapshot 보존

새 시스템 필드:
  - applied_treatment_ids (실제 적용된 것만, manual 제외)
  - manual_required (data_correction_required treatment_id 들)
  - unresolved_treatment_ids (ontology 에 없는 id)
  - new_causes (적용 후 새로 식별된 cause — 부작용 감지)
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Optional

from services.constraint_impact.control import (
    ConstraintAdjustment,
    apply_adjustments_to_config,
)
from services.precheck.evidence_builder import build_evidence_node
from services.semantics.ontology import ConstraintOntology, get_default_ontology


# action_type (yaml) → control.SUPPORTED_ACTIONS 매핑
_ACTION_MAP = {
    "force_soft_mode": "force_soft_mode",
    "disable_module": "disable_module",
    "set_threshold": "set_threshold",
    "narrow_scope": "narrow_scope",
    "data_correction_required": None,  # auto 적용 안 됨 (manual)
}


@dataclass(slots=True)
class ApplyPlan:
    """treatment_ids → auto/manual/unresolved 분류."""
    treatment_ids: list[str]
    auto_adjustments: list[ConstraintAdjustment] = field(default_factory=list)
    manual_required: list[str] = field(default_factory=list)
    unresolved_treatment_ids: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ApplyResult:
    """패치된 config + plan + rollback snapshot."""
    patched_config: dict[str, Any]
    original_config_snapshot: dict[str, Any]
    applied_treatment_ids: list[str]
    manual_required: list[str]
    unresolved_treatment_ids: list[str]
    plan: ApplyPlan
    dry_run: bool


def build_apply_plan(
    treatment_ids: list[str],
    ontology: Optional[ConstraintOntology] = None,
) -> ApplyPlan:
    """treatment_id list → ApplyPlan (auto / manual / unresolved 분리).

    각 treatment 의 action_type 이 data_correction_required 면 manual_required 로 분리.
    ontology 에 없는 treatment_id 는 unresolved_treatment_ids 로.
    """
    onto = ontology or get_default_ontology()
    auto: list[ConstraintAdjustment] = []
    manual: list[str] = []
    unresolved: list[str] = []
    for tid in treatment_ids or []:
        t = onto.get_treatment(tid)
        if t is None:
            unresolved.append(tid)
            continue
        ctrl_action = _ACTION_MAP.get(t.action_type)
        if ctrl_action is None:
            manual.append(tid)
            continue
        auto.append(ConstraintAdjustment(
            family=t.target_family,
            action=ctrl_action,
            reason=f"treatment={tid}",
        ))
    return ApplyPlan(
        treatment_ids=list(treatment_ids or []),
        auto_adjustments=auto,
        manual_required=manual,
        unresolved_treatment_ids=unresolved,
    )


def apply_treatments(
    treatment_ids: list[str],
    config_dict: dict[str, Any],
    ontology: Optional[ConstraintOntology] = None,
    *,
    dry_run: bool = False,
) -> ApplyResult:
    """treatment_id list → patched config + rollback snapshot.

    멱등: 같은 입력 → 같은 patched_config.
    manual 또는 unresolved 는 적용 안 함 (분리 표시).
    config_dict 는 immutable — deepcopy 후 작업.
    """
    onto = ontology or get_default_ontology()
    plan = build_apply_plan(treatment_ids, onto)

    snapshot = copy.deepcopy(config_dict or {})

    # auto adjustments 만 적용. manual/unresolved 는 noop.
    patched = apply_adjustments_to_config(
        config_dict or {},
        plan.auto_adjustments,
        ontology=onto,
    )

    # 실제 적용된 treatment_id (manual/unresolved 제외)
    applied_ids: list[str] = []
    for tid in plan.treatment_ids:
        t = onto.get_treatment(tid)
        if t is None:
            continue
        if t.action_type == "data_correction_required":
            continue
        applied_ids.append(tid)

    return ApplyResult(
        patched_config=patched,
        original_config_snapshot=snapshot,
        applied_treatment_ids=applied_ids,
        manual_required=list(plan.manual_required),
        unresolved_treatment_ids=list(plan.unresolved_treatment_ids),
        plan=plan,
        dry_run=dry_run,
    )


def compute_violation_delta(
    before_causes: list[dict],
    after_causes: list[dict],
) -> dict[str, dict[str, int]]:
    """before/after cause list → cause_id 별 {before, after} count delta."""
    def _count(causes):
        out: dict[str, int] = {}
        for c in causes or []:
            code = (c.get("reason_code") or "").upper().strip()
            if code:
                out[code] = out.get(code, 0) + 1
        return out

    b = _count(before_causes)
    a = _count(after_causes)
    return {
        code: {"before": b.get(code, 0), "after": a.get(code, 0)}
        for code in sorted(set(b) | set(a))
    }


def detect_new_causes(
    before_causes: list[dict],
    after_causes: list[dict],
) -> list[dict]:
    """after 에만 등장하는 cause = treatment 적용으로 생긴 부작용."""
    before_codes = {(c.get("reason_code") or "").upper().strip() for c in before_causes or []}
    return [
        c for c in (after_causes or [])
        if (c.get("reason_code") or "").upper().strip() not in before_codes
    ]


def build_evidence_from_apply(
    *,
    apply_result: ApplyResult,
    before_causes: list[dict],
    after_causes: list[dict],
    solver_status: str = "INFEASIBLE",
    witness_schedule_id: Optional[str] = None,
) -> dict[str, Any]:
    """ApplyResult + before/after causes → EvidenceNode (US-A 확장).

    Status 결정:
      FEASIBLE  — solver FEASIBLE + witness + after_causes 비어있음 (모든 cause 해소)
      PARTIAL   — solver FEASIBLE 이지만 일부 cause 잔존 또는 new_causes 발생
      INFEASIBLE — solver 실패 또는 witness 없음
    """
    violation_delta = compute_violation_delta(before_causes, after_causes)
    new_causes = detect_new_causes(before_causes, after_causes)

    if solver_status == "FEASIBLE" and witness_schedule_id and not after_causes:
        status = "FEASIBLE"
    elif solver_status == "FEASIBLE" and witness_schedule_id and (after_causes or new_causes):
        status = "PARTIAL"
    else:
        status = "INFEASIBLE"

    proof_type = (
        "re_solve_witness" if witness_schedule_id else "cp_sat_unsat_core_heuristic"
    )

    ev = build_evidence_node(
        applied_relaxations=apply_result.applied_treatment_ids,
        conflict_cores=[],
        status=status,
        proof_type=proof_type,
        witness_schedule_id=witness_schedule_id,
        violation_delta=violation_delta,
    )
    # US-A 확장 필드
    ev["new_causes"] = new_causes
    ev["manual_required_treatment_ids"] = apply_result.manual_required
    ev["unresolved_treatment_ids"] = apply_result.unresolved_treatment_ids
    return ev


def rollback_config(apply_result: ApplyResult) -> dict[str, Any]:
    """ApplyResult 의 snapshot 으로 config 복원 — caller 가 적용 실패 시 호출.

    Idempotent — 여러 번 호출해도 같은 snapshot 반환.
    """
    return copy.deepcopy(apply_result.original_config_snapshot)
