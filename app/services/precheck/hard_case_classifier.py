"""U-3 — 어려운 케이스(hard_case) 자동 분류기.

사용자 ralph 요구 (2026-05-17):
  "어려운 케이스가 있으면 그건 따로빼는데, 어려운 케이스의 기준은 너가 알아서 잡아."

기준 (4 종, OR 결합):
  C-MULTI    : cause ≥3 AND distinct category ≥2
               → 한 도메인이 아닌 여러 도메인 충돌 → 단일 lever 로 해결 어려움
  C-UNCOVER  : primary bundle 의 uncovered_causes ≥1
               → ontology 카탈로그가 cover 못 하는 cause 존재 → manual investigation 필요
  C-MANUAL   : cause 중 1+ 가 structural / meta causal_layer 이면서 해당 cause 의 모든
               applies_to treatments 가 action_type=data_correction_required (manual only)
               → 자동 lever 없음 → 인사/협의 필요
  C-WIDE     : 5 종 카테고리 (capacity / eligibility / fixed / team / grade) 중 4+ 동시 cause
               → cross-domain explosion

산출:
  HardCaseVerdict {
      is_hard, criteria_matched, hard_reason_ko, recommended_action_ko,
      cause_categories, uncovered_causes, manual_only_causes
  }

payload integration: build_unrecoverable_payload / build_blocking_payload 가
  infeasibility.hard_case 필드로 노출. is_hard=true 면 treatment_recommendations 의
  맨 뒤에 'treatment:meta:manual_investigation_required' 가 append.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from services.semantics.ontology import ConstraintOntology, get_default_ontology


_WIDE_CORE_CATEGORIES = frozenset({
    "capacity", "eligibility", "fixed", "team", "grade",
})

_MANUAL_ACTION_TYPES = frozenset({"data_correction_required"})


@dataclass(slots=True)
class HardCaseVerdict:
    is_hard: bool
    criteria_matched: list[str] = field(default_factory=list)
    hard_reason_ko: str = ""
    recommended_action_ko: str = ""
    cause_categories: list[str] = field(default_factory=list)
    uncovered_causes: list[str] = field(default_factory=list)
    manual_only_causes: list[str] = field(default_factory=list)
    cause_count: int = 0
    category_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_hard": self.is_hard,
            "criteria_matched": list(self.criteria_matched),
            "hard_reason_ko": self.hard_reason_ko,
            "recommended_action_ko": self.recommended_action_ko,
            "cause_categories": list(self.cause_categories),
            "uncovered_causes": list(self.uncovered_causes),
            "manual_only_causes": list(self.manual_only_causes),
            "cause_count": int(self.cause_count),
            "category_count": int(self.category_count),
        }


def _resolve_cause_categories(
    causes: list[dict[str, Any]],
    ontology: ConstraintOntology,
) -> tuple[list[str], list[str]]:
    """causes 에서 (sorted category set, cause_ids resolved) 반환."""
    cats: set[str] = set()
    cids: list[str] = []
    for c in causes or []:
        raw = c.get("node_id") or c.get("reason_code")
        if not raw:
            continue
        cid = ontology.resolve_cause_alias(str(raw))
        if not cid:
            # ontology 미등록 cause — uncovered 로 다뤄지지만 category 라벨링 시도
            cids.append(str(raw))
            continue
        cids.append(cid)
        cause = ontology.get_cause(cid)
        if cause and cause.category:
            cats.add(cause.category)
    return sorted(cats), cids


def _find_manual_only_causes(
    cause_ids: Iterable[str],
    ontology: ConstraintOntology,
) -> list[str]:
    """cause_id 별로 매칭되는 treatments 가 모두 manual (data_correction_required) 인지 검사."""
    out: list[str] = []
    for cid in cause_ids:
        cause = ontology.get_cause(cid)
        if cause is None:
            continue
        if cause.causal_layer not in {"structural", "meta"}:
            continue
        treatments = ontology.treatments_for_cause(cid)
        if not treatments:
            # ontology 카탈로그가 매핑 treatment 0건 → 자동 lever 없음 → manual
            out.append(cid)
            continue
        if all(t.action_type in _MANUAL_ACTION_TYPES for t in treatments):
            out.append(cid)
    return out


def classify_hard_case(
    causes: list[dict[str, Any]] | None,
    bundles: list[Any] | None = None,
    ontology: ConstraintOntology | None = None,
) -> HardCaseVerdict:
    """causes + (선택) treatment bundles → HardCaseVerdict.

    bundles 는 Bundle 객체 list (cause_treatment_hitter.Bundle).
    None 이면 uncovered 검사 skip (criteria_matched 에 C-UNCOVER 만 빠짐).
    """
    onto = ontology or get_default_ontology()
    causes = causes or []
    cats, cause_ids = _resolve_cause_categories(causes, onto)
    cause_count = len(cause_ids)
    category_count = len(cats)

    criteria_matched: list[str] = []
    reasons_ko: list[str] = []

    # C-MULTI
    if cause_count >= 3 and category_count >= 2:
        criteria_matched.append("C-MULTI")
        reasons_ko.append(
            f"원인 {cause_count}건이 {category_count}개 도메인({', '.join(cats)})에 걸쳐 발생"
        )

    # C-UNCOVER
    uncovered: list[str] = []
    if bundles:
        primary = bundles[0] if bundles else None
        if primary is not None:
            uncov = getattr(primary, "uncovered_causes", None) or []
            uncovered = list(uncov)
            if uncovered:
                criteria_matched.append("C-UNCOVER")
                reasons_ko.append(
                    f"자동 추천 bundle 이 cover 못 한 원인 {len(uncovered)}건 존재"
                )

    # C-MANUAL
    manual_only = _find_manual_only_causes(cause_ids, onto)
    if manual_only:
        criteria_matched.append("C-MANUAL")
        reasons_ko.append(
            f"manual 만 가능한 원인 {len(manual_only)}건 (인사/협의 영역)"
        )

    # C-WIDE
    wide_overlap = _WIDE_CORE_CATEGORIES & set(cats)
    if len(wide_overlap) >= 4:
        criteria_matched.append("C-WIDE")
        reasons_ko.append(
            f"5대 카테고리 중 {len(wide_overlap)}개({', '.join(sorted(wide_overlap))}) 동시 충돌"
        )

    is_hard = bool(criteria_matched)
    hard_reason_ko = (
        " · ".join(reasons_ko) if reasons_ko
        else ("단일 도메인의 명확한 원인 — 자동 추천 가능" if cause_count > 0 else "원인 미식별")
    )
    recommended_action_ko = (
        "복합 원인이므로 자동 추천된 bundle 을 1순위로 시도하되, 적용 후에도 잔존 원인이 있으면 "
        "관리자가 직접 (a) uncovered 원인의 데이터/설정 점검, (b) manual 항목의 인사/협의 진행, "
        "(c) 일부 원인의 우선 완화 후 단계적 재시도를 권장합니다."
        if is_hard
        else "추천된 단일 bundle 적용으로 해결 가능 — 별도 manual 조치 불요."
    )

    return HardCaseVerdict(
        is_hard=is_hard,
        criteria_matched=criteria_matched,
        hard_reason_ko=hard_reason_ko,
        recommended_action_ko=recommended_action_ko,
        cause_categories=cats,
        uncovered_causes=uncovered,
        manual_only_causes=manual_only,
        cause_count=cause_count,
        category_count=category_count,
    )


def manual_investigation_treatment_dict(verdict: HardCaseVerdict) -> dict[str, Any] | None:
    """hard_case 일 때 treatment_recommendations 끝에 append 할 manual lever.

    is_hard=False 면 None.
    """
    if not verdict.is_hard:
        return None
    return {
        "bundle_id": "bundle:meta:manual_investigation",
        "total_cost": 0,
        "overhead": 0,
        "covered_causes": [],
        "uncovered_causes": list(verdict.uncovered_causes or verdict.manual_only_causes),
        "treatments": [
            {
                "treatment_id": "treatment:meta:manual_investigation_required",
                "target_family": "HardCase",
                "action_type": "data_correction_required",
                "config_key": None,
                "direction": "manual",
                "rationale_ko": (
                    "어려운 케이스(복합 원인 / uncovered / manual-only) 자동 해결 불가. "
                    "관리자가 직접 데이터·설정·인사 항목을 점검하고 우선순위에 따라 단계적으로 적용 권장."
                ),
                "trade_off_ko": (
                    "수동 분석 시간이 필요. 그러나 자동 lever 만으론 잔존 원인이 남아 재시도 시 "
                    "또다시 infeasible 가능."
                ),
                "cost": 0,
                "covers": list(verdict.uncovered_causes or verdict.manual_only_causes),
            }
        ],
    }
