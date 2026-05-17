"""Cause/Symptom/Probe/Undiagnosed 분류기.

설계 원칙 (사용자 핵심 요구):
  EvidenceNode 와 SymptomNode 는 CauseNode 와 분리되어야 한다.
  payload 에서 cause-bucket 과 symptom-bucket 이 절대 교차 element 를 갖지 않는다.

분류 카테고리:
  - cause          : 식별된 원인 (예: CAPACITY_TOTAL_SHORTAGE)
  - partial_cause  : NO_ASSIGNMENT 의 4 축 분해 (capacity/eligibility/fixed/carryover).
                     cause-bucket 으로 분류 — '어느 축의 cause 인지' 라는 약한 cause 정보.
  - symptom        : 관찰된 실패 신호 (NO_ASSIGNMENT, DAY_ZERO_COVERAGE 등)
  - probe          : 솔버 보조 진단 결과 (GRADE_HARD_PROBE 등). cause hint 지만 정식 cause 는 아님.
  - undiagnosed    : sentinel. cause bucket 에 들어가되 confidence=low 라벨링.

US-6 에서 ontology.yaml v4 의 `causes` / `symptoms` 섹션이 도입되면
본 모듈의 frozenset 들은 ontology loader 의 카테고리 attribute 로 대체된다.
그때까지는 본 모듈이 단일 진실로 동작한다.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Tuple


SYMPTOM_CODES = frozenset({
    "NO_ASSIGNMENT",
    "NO_ASSIGNMENT_CAPACITY",
    "NO_ASSIGNMENT_ELIGIBILITY",
    "NO_ASSIGNMENT_FIXED",
    "NO_ASSIGNMENT_CARRYOVER",
    "DAY_ZERO_COVERAGE",  # legacy — Phase 1 에서 코드 throw 제거됨. 안전망.
    "NURSE_BLOCKED_DAYS",
})

PROBE_CODES = frozenset({
    "GRADE_HARD_PROBE",
    "MAX_CAP_SHORTAGE",
})

UNDIAGNOSED_SENTINEL = "UNDIAGNOSED"

# US-10: NO_ASSIGNMENT_* 4축 라벨 cause-bucket 진입 차단.
# 미배정은 "결과(symptom)" 이며 cause 가 아니다. 구체 cause_id 는 detector
# (team_grade_precheck) + MUS inferer (cause_inferer) 가 발급한다.
PARTIAL_CAUSE_CODES: frozenset = frozenset()


Category = Literal["cause", "partial_cause", "symptom", "probe", "undiagnosed"]


def classify(reason_code: str | None) -> Category:
    code = (reason_code or "").upper().strip()
    if not code:
        return "cause"
    if code in SYMPTOM_CODES:
        return "symptom"
    if code in PROBE_CODES:
        return "probe"
    if code == UNDIAGNOSED_SENTINEL:
        return "undiagnosed"
    if code in PARTIAL_CAUSE_CODES:
        return "partial_cause"
    return "cause"


def split_violations(
    violated_constraints: List[Dict[str, Any]] | None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], bool]:
    """violated_constraints 를 cause / symptom bucket 으로 분리.

    Returns:
        (causes, observed_symptoms, undiagnosed_present)

    cause-bucket: cause + partial_cause + undiagnosed.
    symptom-bucket: symptom + probe.
    교집합 element 0건 (테스트로 enforced).
    undiagnosed_present: UNDIAGNOSED sentinel 이 cause-bucket 안에 1+ 있는지.
    """
    causes: List[Dict[str, Any]] = []
    observed_symptoms: List[Dict[str, Any]] = []
    undiagnosed_present = False

    for v in violated_constraints or []:
        if not isinstance(v, dict):
            continue
        code = (v.get("reason_code") or "").upper().strip()
        cat = classify(code)
        item = dict(v)
        if cat == "undiagnosed":
            undiagnosed_present = True
            item.setdefault("confidence", "low")
            causes.append(item)
        elif cat in ("cause", "partial_cause"):
            causes.append(item)
        else:
            observed_symptoms.append(item)

    return causes, observed_symptoms, undiagnosed_present
