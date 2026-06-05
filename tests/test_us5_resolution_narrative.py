"""US-5 검증 — Resolution narrative builder.

핵심 invariants (사용자 요구):
  1. narrative.problem_list 에 cause 별 한 줄, numeric evidence 채움.
  2. narrative.action_levers 의 각 항목이 구체 config_key + direction (manual 제외).
  3. trade_offs 가 비어있지 않음.
  4. naive '인원 줄이세요' 류 패턴 0건 (자동 검출).
  5. evidence.verified=true 이면 summary 에 검증 완료 표시.
  6. 동적: 어떤 cause set 조합에서도 invariant 유지.
"""

from __future__ import annotations

import re
import random
import pytest

from services.cause_treatment_hitter import propose_bundles
from services.resolution_narrative import (
    build_narrative,
    narrative_to_dict,
)
from services.semantics.ontology import get_default_ontology


def _causes_payload(*reason_codes_with_details):
    """헬퍼 — [{"reason_code": ..., "details": {...}}, ...] 빌드."""
    out = []
    for entry in reason_codes_with_details:
        if isinstance(entry, str):
            out.append({"reason_code": entry, "details": {}})
        else:
            code, det = entry
            out.append({"reason_code": code, "details": det})
    return out


# ─────────────────────────────────────────────────────────────────────────
# 기본 동작
# ─────────────────────────────────────────────────────────────────────────
def test_problem_list_one_item_per_cause_with_rendered_message():
    cps = _causes_payload(
        ("CAPACITY_TOTAL_SHORTAGE", {"required": 450, "capacity": 220, "shortage": 230}),
        ("GRADE_MAX_SUM_BELOW_NEED", {"day": 12, "shift": "N", "cap": 2, "required": 3}),
    )
    msg = build_narrative(cause_payloads=cps)
    assert len(msg.problem_list) == 2
    # rendered_ko 에 evidence 값이 들어감 (template substitution)
    p0 = msg.problem_list[0]
    assert "450" in p0.rendered_ko
    assert "220" in p0.rendered_ko
    # category 정확
    assert p0.category == "capacity"


def test_action_levers_present_when_bundle_provided():
    cps = _causes_payload("CAPACITY_TOTAL_SHORTAGE")
    bundles = propose_bundles(active_causes=["CAPACITY_TOTAL_SHORTAGE"])
    msg = build_narrative(cause_payloads=cps, bundle=bundles[0])
    assert len(msg.action_levers) >= 1
    for lever in msg.action_levers:
        # data_correction_required 가 아닌 treatment 는 반드시 config_key
        if lever.action_type != "data_correction_required":
            assert lever.config_key, f"{lever.treatment_id} action_type={lever.action_type} 인데 config_key 없음"
        assert lever.direction in {"enable", "disable", "increase", "decrease", "clear", "remove_key", "manual"}


def test_trade_offs_non_empty_when_bundle_provided():
    cps = _causes_payload("CAPACITY_TOTAL_SHORTAGE")
    bundles = propose_bundles(active_causes=["CAPACITY_TOTAL_SHORTAGE"])
    msg = build_narrative(cause_payloads=cps, bundle=bundles[0])
    assert msg.trade_offs
    for t in msg.trade_offs:
        assert t.trade_off_ko.strip()


def test_summary_indicates_verified_when_evidence_verified():
    cps = _causes_payload("CAPACITY_TOTAL_SHORTAGE")
    msg = build_narrative(cause_payloads=cps, evidence={"verified": True})
    assert "검증" in msg.summary_ko or "verified" in msg.summary_ko.lower()


def test_summary_indicates_unverified_default():
    msg = build_narrative(cause_payloads=_causes_payload("CAPACITY_TOTAL_SHORTAGE"))
    assert "검증" not in msg.summary_ko


# ─────────────────────────────────────────────────────────────────────────
# Naive pattern 검출
# ─────────────────────────────────────────────────────────────────────────
def test_no_naive_headcount_in_built_narrative():
    cps = _causes_payload(
        ("CAPACITY_TOTAL_SHORTAGE", {"required": 450, "capacity": 220, "shortage": 230}),
        "GRADE_MAX_SUM_BELOW_NEED",
        "TEAM_MIN_EXCEEDS_GLOBAL_NEED",
    )
    bundles = propose_bundles(active_causes=[c["reason_code"] for c in cps])
    msg = build_narrative(cause_payloads=cps, bundle=bundles[0])

    full_text = " ".join(
        [msg.summary_ko]
        + [p.rendered_ko for p in msg.problem_list]
        + [a.rationale_ko for a in msg.action_levers]
        + [t.trade_off_ko for t in msg.trade_offs]
    )
    # naive 검출 패턴 (보강/추가 문맥 제외)
    bad = re.findall(r"(간호사|인원)(을|를)\s*(줄이|감축)", full_text)
    if bad:
        # 보강/추가 문맥인지 확인 (validator 가 이미 거름)
        # 어쨌든 narrative builder 의 validator 가 안 걸렀다면 fail
        for m in re.finditer(r"(간호사|인원)(을|를)\s*(줄이|감축)", full_text):
            ctx = full_text[max(0, m.start() - 30): m.end() + 30]
            if not any(k in ctx for k in ("보강", "추가", "수요 하향")):
                pytest.fail(f"naive headcount pattern leaked: {ctx}")


def test_build_narrative_raises_on_injected_naive_text():
    """직접 evidence 에 '간호사를 줄이세요' 같은 문장 넣으면 ValueError raise.
    (problem_template_ko 가 사용자 입력 evidence 를 평탄하게 fmt 하므로 방어 필요.)"""
    cps = [{
        "reason_code": "CAPACITY_TOTAL_SHORTAGE",
        # 임의 사용자 데이터에 naive 문구 포함 — narrative builder 가 검출해야
        "details": {"required": "간호사를 줄이세요", "capacity": 0, "shortage": 0,
                    "nurse_count": 0, "days": 0, "off_days": 0,
                    "off_first": False, "source": "test"},
    }]
    with pytest.raises(ValueError, match="naive"):
        build_narrative(cause_payloads=cps)


# ─────────────────────────────────────────────────────────────────────────
# 동적 sweep — 임의 cause 조합에서 invariants
# ─────────────────────────────────────────────────────────────────────────
def test_dynamic_sweep_50_random_cause_sets_invariants_hold():
    onto = get_default_ontology()
    all_causes = [cid for cid in onto.causes.keys() if cid != "cause:undiagnosed"]
    rng = random.Random(31337)

    violations: list[tuple[int, str]] = []
    for seed in range(50):
        local_rng = random.Random(rng.random())
        n = local_rng.randint(1, min(5, len(all_causes)))
        cset = local_rng.sample(all_causes, n)
        bundles = propose_bundles(active_causes=cset, max_alternatives=1)
        if not bundles:
            continue
        cps = [{"reason_code": c, "details": {}} for c in cset]
        try:
            msg = build_narrative(cause_payloads=cps, bundle=bundles[0])
        except ValueError as e:
            # naive pattern 검출 — 안 일어나야 (catalogue 에 naive 없으므로)
            violations.append((seed, f"naive pattern: {e}"))
            continue
        # invariant: problem_list 가 cause set 와 동일 size
        if len(msg.problem_list) != len(cset):
            violations.append((seed, f"problem_list size mismatch: {len(msg.problem_list)} vs {len(cset)}"))
        # invariant: bundle treatments 가 있으면 action_levers 채워짐
        if bundles[0].treatments and not msg.action_levers:
            violations.append((seed, f"bundle has treatments but no action_levers"))

    assert violations == [], f"dynamic sweep {len(violations)}/50 invariant failures: {violations[:3]}"


# ─────────────────────────────────────────────────────────────────────────
# narrative_to_dict 직렬화
# ─────────────────────────────────────────────────────────────────────────
def test_narrative_to_dict_has_all_sections():
    cps = _causes_payload("CAPACITY_TOTAL_SHORTAGE")
    bundles = propose_bundles(active_causes=["CAPACITY_TOTAL_SHORTAGE"])
    msg = build_narrative(cause_payloads=cps, bundle=bundles[0])
    d = narrative_to_dict(msg)
    assert "summary_ko" in d
    assert "problem_list" in d
    assert "action_levers" in d
    assert "trade_offs" in d
    assert "uncovered_causes" in d
    assert "verified" in d
    # JSON-serializable
    import json
    json.dumps(d)
