"""Resolution narrative builder — cause + bundle → 자연어 메시지 (US-5).

설계 (사용자 핵심 요구):
  메시지가 (a) problem_list (b) action_levers (c) trade_offs 3 섹션을 모두 갖는다.
  - problem_list: 어떤 cause 가 문제됐는지 — list 형태, numeric evidence 포함
  - action_levers: 어떤 설정 키를 어느 방향으로 조절하면 해결되는지 — config_key + direction 명시
  - trade_offs: 각 treatment 의 부작용 — 운영상 주의사항

금지 패턴:
  - '인원을 줄이세요' / '간호사를 줄이세요' 등 naive headcount 추천
  - config_key 없는 추상 추천
  - cause 없이 treatment 만 추천 (evidence 결여)

evidence 파라미터:
  EvidenceNode (US-1) 가 있으면 narrative 마지막에 "현재 상태" 섹션으로 추가.
  EvidenceNode 의 verified=true 이면 "이 추천이 적용 후 검증되었음" 명시.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from services.cause_treatment_hitter import Bundle
from services.semantics.ontology import (
    ConstraintOntology,
    OntologyCause,
    friendly_config_key_label,
    friendly_direction_label,
    get_default_ontology,
)


# 사용자 요구 — naive headcount reduction 류 패턴 차단 (regex 로 narrative 후처리 검증)
_NAIVE_PATTERNS = [
    re.compile(r"(간호사|인원)(을|를)?\s*\S{0,12}?\s*(줄이|감축)", re.IGNORECASE),
]


@dataclass(slots=True)
class ProblemListItem:
    cause_id: str
    label: str
    category: str
    causal_layer: str
    tier: str
    rendered_ko: str        # problem_template_ko 에 evidence 채워넣은 문장
    raw_evidence: dict[str, Any] = field(default_factory=dict)
    # U-4: 각 problem 에 해결책 (treatment) 매핑 — '원인 + 해결' 묶음 가시화
    mapped_treatments: list[dict[str, Any]] = field(default_factory=list)
    cause_solution_ko: str = ""    # "원인 + 해결책" 한 문장 결합 표시


@dataclass(slots=True)
class ActionLever:
    treatment_id: str
    target_family: str
    action_type: str
    config_key: str | None
    direction: str
    rationale_ko: str
    covers_causes: list[str]


@dataclass(slots=True)
class TradeOff:
    treatment_id: str
    trade_off_ko: str


@dataclass(slots=True)
class NarrativeMessage:
    summary_ko: str
    problem_list: list[ProblemListItem]
    action_levers: list[ActionLever]
    trade_offs: list[TradeOff]
    uncovered_causes: list[str] = field(default_factory=list)
    verified: bool = False


def _fill_template(template: str, evidence: Mapping[str, Any]) -> str:
    """problem_template_ko 의 {key} 들을 evidence 값으로 채움.

    누락된 key 는 '?' 로 대체. format() 의 KeyError 방지.
    """
    if not template:
        return ""

    class _SafeDict(dict):
        def __missing__(self, key):
            return "?"

    try:
        return template.format_map(_SafeDict(evidence or {}))
    except Exception:
        return template


def _validate_no_naive_patterns(text: str) -> None:
    """assertion — naive headcount reduction 패턴이 발견되면 ValueError raise.

    예외 컨텍스트:
      - 인력 보강/추가/배치 (반대 방향 권장)
      - demand 하향 비교 문맥
      - "단순 일시" / "재발" / "권장하지" / "지양" / "주의" 같은 anti-pattern 경고문
    """
    _SAFE_CONTEXT_KEYWORDS = (
        "보강", "추가", "배치", "demand", "수요 하향", "demand 하향",
        "단순 일시", "재발", "권장하지", "지양", "주의", "안 됨", "위험",
    )
    for pat in _NAIVE_PATTERNS:
        m = pat.search(text or "")
        if m:
            context = text[max(0, m.start() - 60): m.end() + 60]
            if any(k in context for k in _SAFE_CONTEXT_KEYWORDS):
                continue
            raise ValueError(
                f"naive headcount reduction pattern detected: '{m.group(0)}' in '{context}'"
            )


def build_narrative(
    *,
    cause_payloads: list[Mapping[str, Any]],
    bundle: Optional[Bundle] = None,
    evidence: Optional[Mapping[str, Any]] = None,
    ontology: Optional[ConstraintOntology] = None,
) -> NarrativeMessage:
    """주어진 cause + bundle 로부터 자연어 메시지 빌드.

    cause_payloads: payload.causes 의 list. 각 항목: {"reason_code"|"cause_id", "details": {...}}
    bundle: hitter.propose_bundles() 의 첫 후보 (또는 사용자 선택). None 이면 action 섹션 빈 채.
    evidence: US-1 의 EvidenceNode dict. verified 여부 표시용.
    """
    onto = ontology or get_default_ontology()

    # 1. problem_list 생성 — 각 cause 에 매칭되는 treatment 도 함께 묶어 둠
    problem_list: list[ProblemListItem] = []
    for p in cause_payloads or []:
        if not isinstance(p, Mapping):
            continue
        raw_code = p.get("cause_id") or p.get("reason_code")
        c: OntologyCause | None = onto.get_cause(str(raw_code or "")) if raw_code else None
        if c is None:
            continue
        ev = dict(p.get("details") or p.get("evidence") or {})
        # cause_id / reason_code 도 evidence 로 평탄화 (template 변수 충분히 확보)
        merged_evidence = {**ev, **{k: v for k, v in p.items()
                                    if k not in ("cause_id", "reason_code", "details", "evidence")}}
        rendered = _fill_template(c.problem_template_ko, merged_evidence)
        # U-4: 본 cause 에 대응 가능한 treatment 들을 ontology lookup
        mapped: list[dict[str, Any]] = []
        for t in onto.treatments_for_cause(c.cause_id):
            mapped.append({
                "treatment_id": t.treatment_id,
                "label": t.label,
                "action_type": t.action_type,
                "config_key": t.config_key,
                "direction": t.direction,
                "rationale_ko": t.rationale_ko,
                "trade_off_ko": t.trade_off_ko,
            })
        # cause + solution 결합 문장 — UI/agent 가 한 줄로 표시 가능
        if mapped:
            actions_ko = " 또는 ".join(t["rationale_ko"].split(".")[0] for t in mapped[:2])
            cause_solution_ko = f"{rendered} → 해결책: {actions_ko}."
        else:
            cause_solution_ko = (
                f"{rendered} → 해결책: 자동 lever 없음 — 데이터/설정 직접 점검 필요."
            )
        problem_list.append(ProblemListItem(
            cause_id=c.cause_id,
            label=c.label,
            category=c.category,
            causal_layer=c.causal_layer,
            tier=c.tier,
            rendered_ko=rendered,
            raw_evidence=merged_evidence,
            mapped_treatments=mapped,
            cause_solution_ko=cause_solution_ko,
        ))

    # 2. action_levers + trade_offs (bundle 가 있을 때만)
    action_levers: list[ActionLever] = []
    trade_offs: list[TradeOff] = []
    if bundle and bundle.treatments:
        for t in bundle.treatments:
            action_levers.append(ActionLever(
                treatment_id=t.treatment_id,
                target_family=t.target_family,
                action_type=t.action_type,
                config_key=t.config_key,
                direction=t.direction,
                rationale_ko=t.rationale_ko,
                covers_causes=list(t.covers),
            ))
            trade_offs.append(TradeOff(
                treatment_id=t.treatment_id,
                trade_off_ko=t.trade_off_ko,
            ))

    # 3. summary 생성
    n_problems = len(problem_list)
    n_actions = len(action_levers)
    summary_parts = []
    if n_problems > 0:
        summary_parts.append(f"식별된 원인 {n_problems}건")
    if n_actions > 0:
        summary_parts.append(f"조정 가능한 설정 {n_actions}개")
    if bundle and bundle.uncovered_causes:
        summary_parts.append(f"수동 검토 필요 {len(bundle.uncovered_causes)}건")
    summary_ko = "; ".join(summary_parts) if summary_parts else "원인 식별 결과 없음"

    verified = bool((evidence or {}).get("verified"))
    if verified:
        summary_ko += " — 적용 후 재실행 검증 완료"

    uncovered = list(bundle.uncovered_causes) if bundle else []

    msg = NarrativeMessage(
        summary_ko=summary_ko,
        problem_list=problem_list,
        action_levers=action_levers,
        trade_offs=trade_offs,
        uncovered_causes=uncovered,
        verified=verified,
    )

    # naive 패턴 후처리 검증
    full_text = " ".join(
        [summary_ko]
        + [p.rendered_ko for p in problem_list]
        + [a.rationale_ko for a in action_levers]
        + [t.trade_off_ko for t in trade_offs]
    )
    _validate_no_naive_patterns(full_text)

    return msg


def narrative_to_dict(msg: NarrativeMessage) -> dict[str, Any]:
    """payload 노출용 dict 변환."""
    return {
        "summary_ko": msg.summary_ko,
        "problem_list": [
            {
                "cause_id": p.cause_id,
                "label": p.label,
                "category": p.category,
                "causal_layer": p.causal_layer,
                "tier": p.tier,
                "rendered_ko": p.rendered_ko,
                "raw_evidence": p.raw_evidence,
                "mapped_treatments": list(p.mapped_treatments),
                "cause_solution_ko": p.cause_solution_ko,
            }
            for p in msg.problem_list
        ],
        "action_levers": [
            {
                "treatment_id": a.treatment_id,
                "target_family": a.target_family,
                "action_type": a.action_type,
                "config_key": a.config_key,
                "config_key_label_ko": friendly_config_key_label(a.config_key),
                "direction": a.direction,
                "direction_label_ko": friendly_direction_label(a.direction),
                "rationale_ko": a.rationale_ko,
                "covers_causes": a.covers_causes,
            }
            for a in msg.action_levers
        ],
        "trade_offs": [
            {"treatment_id": t.treatment_id, "trade_off_ko": t.trade_off_ko}
            for t in msg.trade_offs
        ],
        "uncovered_causes": msg.uncovered_causes,
        "verified": msg.verified,
    }
