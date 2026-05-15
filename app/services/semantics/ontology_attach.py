from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

from services.semantics.ontology import ConstraintOntology, get_default_ontology


REASON_CODE_RE = re.compile(r"\[reason_code=([A-Z0-9_]+)\]")


def extract_reason_code(message: str | None) -> str | None:
    if not message:
        return None
    m = REASON_CODE_RE.search(str(message))
    return m.group(1) if m else None


def _ontology_payload(*, constraint_id: str | None, mode: str, ontology: ConstraintOntology) -> dict[str, Any] | None:
    if not constraint_id:
        return None
    entry = ontology.get_constraint(constraint_id)
    if entry is None:
        return None
    mode_entry = ontology.get_mode(mode)
    return {
        "constraint_id": entry.constraint_id,
        "group": entry.parent,
        "scope": list(entry.scope),
        "mode": mode,
        "severity": (mode_entry.severity if mode_entry else entry.default_severity),
    }


def attach_constraint_ontology(fact: dict[str, Any], ontology: ConstraintOntology | None = None) -> dict[str, Any]:
    onto = ontology or get_default_ontology()
    out = deepcopy(fact)
    raw_family = out.get("constraint_id") or out.get("family") or out.get("reason_code")
    resolved = onto.resolve_alias(raw_family)
    mode = str(out.get("mode") or out.get("effective_mode") or "precheck_blocked")
    payload = _ontology_payload(constraint_id=resolved, mode=mode, ontology=onto)
    if payload is not None:
        out["ontology"] = payload
    return out


REASON_CODE_TO_CONSTRAINT = {
    "GLOBAL_DAY_CAPACITY_SHORTAGE": "CoverageMin",
    "GLOBAL_SHIFT_ALLOWED_SHORTAGE": "CoverageMin",
    "CAPACITY_TOTAL_SHORTAGE": "CoverageMin",
    "NO_ASSIGNMENT": "CoverageMin",
    "TEAM_MIN_EXCEEDS_GLOBAL_NEED": "TeamMin",
    "TEAM_SIZE_INSUFFICIENT": "TeamMin",
    "TEAM_ACTIVE_MEMBERS_INSUFFICIENT": "TeamMin",
    "TEAM_SHIFT_ALLOWED_SHORTAGE": "TeamMin",
    "FIXED_ASSIGN_BREAKS_TEAM_MIN": "TeamMin",
    "GRADE_MIN_SUM_EXCEEDS_NEED": "GradeMin",
    "GRADE_MIN_AVAILABLE_SHORTAGE": "GradeMin",
    "GRADE_MAX_SUM_BELOW_NEED": "GradeMax",
    "GRADE_ANTIPAIR_FORCES_SHORTAGE": "GradeMax",
    "TEAM_GRADE_INTERSECT_SHORTAGE": "TeamGradeHandoff",
    "MID_DISABLED_BUT_USED": "ConfigIntegrity",
    "MID_REQUIRED_MISSING": "ConfigIntegrity",
    "ALLOWED_SHIFTS_ISOLATES_NURSE": "ConfigIntegrity",
    "FIXED_ASSIGN_EXCEEDS_NEED": "ConfigIntegrity",
    "FIXED_ASSIGN_VIOLATES_ALLOWED": "ConfigIntegrity",
    "FIXED_OFF_EXCEEDS_SPAN": "OffCap",
}


def attach_reason_code_ontology(
    *,
    reason_code: str | None = None,
    message: str | None = None,
    evidence: dict[str, Any] | None = None,
    severity: str = "hard",
    ontology: ConstraintOntology | None = None,
) -> dict[str, Any]:
    onto = ontology or get_default_ontology()
    code = reason_code or extract_reason_code(message)
    payload = {
        "reason_code": code,
        "severity": severity,
        "evidence": deepcopy(evidence or {}),
    }
    if message is not None:
        payload["message"] = message
    cid = REASON_CODE_TO_CONSTRAINT.get(str(code or "")) or onto.resolve_alias(code)
    op = _ontology_payload(constraint_id=cid, mode="precheck_blocked", ontology=onto)
    if op is not None:
        payload["ontology"] = op
    return payload
