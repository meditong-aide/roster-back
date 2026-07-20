"""온톨로지 정합성 audit harness.

사용자 요구 (2026-05-17): "새로운 하드제약이 있거나 제약사항이 수정될때 이걸
반영하도록 하네스를 철저히 구축해줘."

새 constraint_family / cause / treatment / config_key 가 추가될 때, 다음 9개
정합성 invariant 가 자동으로 강제됨. 누락 시 audit 가 명확한 위치 + 이유로 fail.

INVARIANTS (audit_all 의 9 check):
  I1. 모든 OntologyCause 가 problem_template_ko 보유 (사용자 노출 메시지 필수).
  I2. 모든 OntologyCause 가 ≥1 treatment 매핑 (applies_to_causes 또는 manual).
  I3. 모든 OntologyTreatment 의 applies_to_causes 가 실제 cause_id 와 일치 (dangling X).
  I4. 모든 OntologyTreatment 가 rationale_ko + trade_off_ko 보유.
  I5. 모든 treatment.config_key (null 제외) 가 CONFIG_KEY_LABELS_KO 등재.
  I6. 모든 treatment.direction 이 DIRECTION_LABELS_KO 등재.
  I7. 모든 constraint_family (ontology.yaml constraints 섹션) 가
      cause_inferer._MEMBER_PREFIX_TO_CAUSE 또는 _PATTERN_TO_CAUSE 에 등재
      (CP-SAT MUS → cause 매핑 누락 없음).
  I8. matrix_50_cases.CASES_50 의 모든 cause_spec factory 가 실제 cause_id 발급.
  I9. 모든 cause.category 가 알려진 11+ 도메인 셋 안에 (오타 방지).

audit_all() → list[AuditFinding]. 비어있으면 통과, 항목 있으면 보강 필요.

테스트 (test_ontology_consistency.py) 는 audit_all() 결과 비어있는지 검증 —
새 제약 추가 시 누락하면 즉시 test red 로 fail.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from services.semantics.ontology import (
    CONFIG_KEY_LABELS_KO,
    DIRECTION_LABELS_KO,
    ConstraintOntology,
    get_default_ontology,
)


KNOWN_CAUSE_CATEGORIES = frozenset({
    # core 5 (hard_case C-WIDE 기준)
    "capacity", "eligibility", "fixed", "team", "grade",
    # 구조적 도메인
    "carryover", "recovery", "transition", "preceptee", "consecutive",
    # 설정/메타
    "config", "dispatch", "undiagnosed", "observed", "evidence", "bundle",
    "treatment", "meta", "uncategorized",
})


@dataclass(slots=True)
class AuditFinding:
    invariant: str           # I1~I9
    severity: str            # critical / major / minor
    target: str              # cause_id / treatment_id / family / config_key
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "invariant": self.invariant,
            "severity": self.severity,
            "target": self.target,
            "detail": self.detail,
        }


# ── I1 ─────────────────────────────────────────────────────────────────────
def check_I1_cause_has_problem_template(onto: ConstraintOntology) -> list[AuditFinding]:
    out: list[AuditFinding] = []
    for cid, cause in onto.causes.items():
        if not cause.problem_template_ko or not cause.problem_template_ko.strip():
            out.append(AuditFinding(
                invariant="I1", severity="critical", target=cid,
                detail="cause 가 problem_template_ko 없음 — 사용자 노출 메시지 필수.",
            ))
    return out


# ── I2 ─────────────────────────────────────────────────────────────────────
def check_I2_cause_has_treatment(onto: ConstraintOntology) -> list[AuditFinding]:
    out: list[AuditFinding] = []
    for cid in onto.causes:
        treatments = onto.treatments_for_cause(cid)
        if not treatments:
            out.append(AuditFinding(
                invariant="I2", severity="critical", target=cid,
                detail=(
                    "cause 에 매핑된 treatment 0건 — hitter 가 추천 불가, "
                    "최소 manual treatment 1개라도 applies_to_causes 에 등재 필요."
                ),
            ))
    return out


# ── I3 ─────────────────────────────────────────────────────────────────────
def check_I3_treatment_applies_to_valid_causes(onto: ConstraintOntology) -> list[AuditFinding]:
    out: list[AuditFinding] = []
    known = set(onto.causes)
    for tid, t in onto.treatments.items():
        for ref in t.applies_to_causes:
            if ref not in known:
                out.append(AuditFinding(
                    invariant="I3", severity="critical", target=tid,
                    detail=f"applies_to_causes 의 '{ref}' 가 cause 카탈로그 누락 — dangling reference.",
                ))
    return out


# ── I4 ─────────────────────────────────────────────────────────────────────
def check_I4_treatment_has_text(onto: ConstraintOntology) -> list[AuditFinding]:
    out: list[AuditFinding] = []
    for tid, t in onto.treatments.items():
        if not t.rationale_ko or not t.rationale_ko.strip():
            out.append(AuditFinding(
                invariant="I4", severity="major", target=tid,
                detail="rationale_ko 비어있음 — '해결책' column 노출 불가.",
            ))
        if not t.trade_off_ko or not t.trade_off_ko.strip():
            out.append(AuditFinding(
                invariant="I4", severity="major", target=tid,
                detail="trade_off_ko 비어있음 — '부작용 주의' column 노출 불가.",
            ))
    return out


# ── I5 ─────────────────────────────────────────────────────────────────────
def check_I5_config_key_has_friendly_label(onto: ConstraintOntology) -> list[AuditFinding]:
    out: list[AuditFinding] = []
    for tid, t in onto.treatments.items():
        if t.config_key and t.config_key not in CONFIG_KEY_LABELS_KO:
            out.append(AuditFinding(
                invariant="I5", severity="minor", target=tid,
                detail=(
                    f"config_key='{t.config_key}' 친화 라벨 누락 → "
                    f"dashboard 에 raw key 그대로 노출. "
                    f"services/semantics/ontology.py 의 CONFIG_KEY_LABELS_KO 에 등재 필요."
                ),
            ))
    return out


# ── I6 ─────────────────────────────────────────────────────────────────────
def check_I6_direction_has_friendly_label(onto: ConstraintOntology) -> list[AuditFinding]:
    out: list[AuditFinding] = []
    for tid, t in onto.treatments.items():
        if t.direction and t.direction not in DIRECTION_LABELS_KO:
            out.append(AuditFinding(
                invariant="I6", severity="minor", target=tid,
                detail=(
                    f"direction='{t.direction}' 친화 라벨 누락. "
                    f"services/semantics/ontology.py 의 DIRECTION_LABELS_KO 에 등재 필요."
                ),
            ))
    return out


# I7: constraint_family → 솔버 MUS prefix/pattern 매핑 카탈로그
# family 이름과 MUS member 이름이 1:1 이 아닐 수 있어 (예: NightRecovery family 의
# 솔버 변수는 Recovery2N2OFF / Recovery3N2OFF) 명시 mapping 유지.
# 새 family 추가 시 이 dict 도 함께 채우도록 audit 가 fail.
_FAMILY_TO_MUS_TOKENS: dict[str, list[str]] = {
    "CoverageMin":             ["CoverageMin", "coverage_min"],
    "TeamMin":                 ["TeamMin", "team_min"],
    "GradeMin":                ["GradeMin", "grade_min"],
    "GradeMax":                ["GradeMax", "grade_max"],
    "ConsecutiveWorkLimit":    ["MaxConsecutiveWork", "max_consecutive_work"],
    "ConsecutiveNightLimit":   ["ConsecutiveNightCap", "consecutive_night_cap"],
    "NightRecovery":           ["Recovery2N2OFF", "Recovery3N2OFF", "recovery_2n2off", "recovery_3n2off"],
    "MonthlyNightCap":         ["MaxNight", "max_night"],
    "BanNightBeforeFixedOff":  ["ban_night_before_fixed_off"],
    "WeekendOffOnly":          ["weekend_off_only"],
    "OffCap":                  ["OffCap", "off_cap"],
    "OffWindow":               ["OffWindowRequirement", "off_window_requirement"],
    "BoundaryTransitionBan":   ["TransitionBanN2D", "TransitionBanE2D", "TransitionBanN2E", "transition_ban"],
    "AllowedShiftMask":        ["AllowedShiftMaskBan", "SpecialShiftBanW", "allowed_shift_mask"],
    "NotOneNight":             ["NotOneNight", "not_one_night"],
    "PrecepteeSync":           [],  # 산술 detector (team_grade_precheck) 만, MUS 변수 없음
    "AssignmentWindow":        [],  # 데이터 영역 (active days) — MUS 미발생
    "CarryoverBoundary":       ["CarryoverRecovery", "carryover_boundary"],
    "FixedWanted":             ["FixedCell", "FixedCellBan", "InitialForbidden", "initial_forbidden"],
    "ConfigIntegrity":         [],  # precheck arithmetic only — MUS 미발생
}


def check_I7_constraint_family_has_cause_mapping(onto: ConstraintOntology) -> list[AuditFinding]:
    """ontology.yaml 의 constraint_families 가 cause_inferer 의 prefix/pattern 매핑에 등재.

    각 family 의 솔버-내부 MUS token (_FAMILY_TO_MUS_TOKENS) 가 cause_inferer 의
    _MEMBER_PREFIX_TO_CAUSE 또는 _PATTERN_TO_CAUSE 에 등재됐는지 확인.
    """
    out: list[AuditFinding] = []
    try:
        from services.precheck.cause_inferer import (
            _MEMBER_PREFIX_TO_CAUSE,
            _PATTERN_TO_CAUSE,
        )
    except Exception as exc:
        return [AuditFinding(
            invariant="I7", severity="critical", target="cause_inferer",
            detail=f"cause_inferer 모듈 로드 실패: {exc}",
        )]
    prefixes = set(_MEMBER_PREFIX_TO_CAUSE.keys())
    patterns = set(_PATTERN_TO_CAUSE.keys())
    for fid in onto.constraints:
        if fid not in _FAMILY_TO_MUS_TOKENS:
            out.append(AuditFinding(
                invariant="I7", severity="major", target=fid,
                detail=(
                    f"constraint_family '{fid}' 가 ontology_audit._FAMILY_TO_MUS_TOKENS "
                    f"에 등재 안 됨 — 새 family 추가 시 솔버 내부 MUS 변수명 매핑도 "
                    f"함께 등재 필요 (또는 MUS 비발생 family 면 빈 list)."
                ),
            ))
            continue
        tokens = _FAMILY_TO_MUS_TOKENS[fid]
        if not tokens:
            continue  # MUS 미발생 family (산술 detector / 데이터 영역) — skip
        if not any(t in prefixes or t in patterns for t in tokens):
            out.append(AuditFinding(
                invariant="I7", severity="major", target=fid,
                detail=(
                    f"family '{fid}' 의 MUS token({tokens}) 중 어느 것도 "
                    f"cause_inferer 에 등재 안 됨 → 솔버가 MUS 잡아도 cause_id 추론 실패. "
                    f"app/services/precheck/cause_inferer.py 에 prefix/pattern 추가 필요."
                ),
            ))
    return out


# ── I8 ─────────────────────────────────────────────────────────────────────
def check_I8_matrix_factories_produce_valid_causes(onto: ConstraintOntology) -> list[AuditFinding]:
    """matrix_50_cases.CASES_50 의 cause spec factory 결과가 실제 cause_id."""
    out: list[AuditFinding] = []
    try:
        import sys as _sys
        from pathlib import Path as _P
        _root = _P(__file__).resolve().parents[3] / "tools" / "harness"
        if str(_root) not in _sys.path:
            _sys.path.insert(0, str(_root))
        import matrix_50_cases as m50  # type: ignore
    except Exception as exc:
        return [AuditFinding(
            invariant="I8", severity="major", target="matrix_50_cases",
            detail=f"matrix_50_cases 로드 실패: {exc}",
        )]
    known = set(onto.causes)
    aliases = set(onto._cause_alias_to_id.keys())
    for case in m50.CASES_50:
        for factory in case["causes"]:
            spec = factory()
            cid = spec.get("node_id")
            rcode = spec.get("reason_code")
            if cid not in known:
                out.append(AuditFinding(
                    invariant="I8", severity="critical", target=case["id"],
                    detail=(
                        f"factory 가 미정의 cause_id '{cid}' 발급. "
                        f"ontology.yaml causes 섹션에 등재되거나 factory 수정 필요."
                    ),
                ))
            if rcode and rcode.upper() not in aliases:
                out.append(AuditFinding(
                    invariant="I8", severity="minor", target=case["id"],
                    detail=(
                        f"factory 가 미정의 alias '{rcode}' 발급. "
                        f"cause '{cid}' 의 aliases 에 추가하거나 factory 수정 필요."
                    ),
                ))
    return out


# ── I9 ─────────────────────────────────────────────────────────────────────
def check_I9_cause_category_is_known(onto: ConstraintOntology) -> list[AuditFinding]:
    out: list[AuditFinding] = []
    for cid, c in onto.causes.items():
        cat = (c.category or "").lower().strip()
        if cat not in KNOWN_CAUSE_CATEGORIES:
            out.append(AuditFinding(
                invariant="I9", severity="major", target=cid,
                detail=(
                    f"category='{c.category}' 미등록 — payload_graph 색상/dashboard 필터 누락 가능. "
                    f"오타이거나 신규 도메인이면 KNOWN_CAUSE_CATEGORIES 에 추가."
                ),
            ))
    return out


# ── audit_all ─────────────────────────────────────────────────────────────
def audit_all(ontology: ConstraintOntology | None = None) -> list[AuditFinding]:
    onto = ontology or get_default_ontology()
    findings: list[AuditFinding] = []
    for check in (
        check_I1_cause_has_problem_template,
        check_I2_cause_has_treatment,
        check_I3_treatment_applies_to_valid_causes,
        check_I4_treatment_has_text,
        check_I5_config_key_has_friendly_label,
        check_I6_direction_has_friendly_label,
        check_I7_constraint_family_has_cause_mapping,
        check_I8_matrix_factories_produce_valid_causes,
        check_I9_cause_category_is_known,
    ):
        try:
            findings.extend(check(onto))
        except Exception as exc:
            findings.append(AuditFinding(
                invariant=check.__name__, severity="critical", target="audit_runner",
                detail=f"check 실행 실패: {type(exc).__name__}: {exc}",
            ))
    return findings


def audit_summary(findings: Iterable[AuditFinding]) -> dict[str, Any]:
    fs = list(findings)
    by_inv: dict[str, list[AuditFinding]] = {}
    by_sev: dict[str, int] = {}
    for f in fs:
        by_inv.setdefault(f.invariant, []).append(f)
        by_sev[f.severity] = by_sev.get(f.severity, 0) + 1
    return {
        "total": len(fs),
        "by_invariant": {k: len(v) for k, v in sorted(by_inv.items())},
        "by_severity": dict(sorted(by_sev.items())),
        "findings": [f.to_dict() for f in fs],
        "pass": len(fs) == 0,
    }
