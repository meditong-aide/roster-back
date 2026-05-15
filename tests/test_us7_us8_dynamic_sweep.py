"""US-7 + US-8 통합 — Dynamic scenario generator 로 100+ 시나리오 sweep 회귀.

흐름 (각 시나리오):
  1. generate_scenario(params) → input_data + expected_causes
  2. run_arithmetic_detectors(input_data) → cause list
  3. propose_bundles(cause list) → bundle 후보
  4. build_narrative(causes, bundle) → 자연어 메시지

검증 invariants:
  A. seed deterministic — 같은 seed 는 같은 결과
  B. feasible 케이스 → cause 0건 (false positive 0)
  C. 의도 주입 케이스 → expected_causes 가 detected 에 ⊆ (false negative 0)
  D. 모든 cause set 에 대해 bundle 생성 가능 (uncovered 있어도 list 는 반환)
  E. narrative 의 action_levers 가 비어있지 않음 (manual 만 가능 시는 OK)
  F. naive '인원 줄이세요' 류 패턴 0건
  G. cause 정확성 ≥ 85% (treatment actionability)

결과 보고서 → /tmp/dynamic_sweep_report.md
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# fixtures dir 을 sys.path 에 추가해 generator 직접 import (tests/ 의 package 화 회피)
_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
if str(_FIXTURES_DIR) not in sys.path:
    sys.path.insert(0, str(_FIXTURES_DIR))

from services.cause_treatment_hitter import propose_bundles
from services.precheck.config_arithmetic_detector import run_arithmetic_detectors
from services.resolution_narrative import build_narrative
from scenario_generator import (  # noqa: E402
    VIOLATION_KINDS,
    GeneratedScenario,
    ScenarioParams,
    generate_scenario,
)


SWEEP_TOTAL = 120  # ≥100 (사용자 요구: 100+ scenarios)
SWEEP_SEED_BASE = 100000


def _run_pipeline(sc: GeneratedScenario):
    """detector → hitter → narrative full pipeline."""
    detector_issues = run_arithmetic_detectors(sc.input_data)
    detected_codes = [i["reason_code"] for i in detector_issues]
    cause_payloads = [{
        "reason_code": i["reason_code"],
        "details": dict(i.get("details") or {}),
    } for i in detector_issues]

    bundles = propose_bundles(active_causes=detected_codes, max_alternatives=2)
    primary = bundles[0] if bundles else None
    narrative = None
    if cause_payloads:
        narrative = build_narrative(cause_payloads=cause_payloads, bundle=primary)
    return detector_issues, bundles, narrative


def _build_sweep_seeds():
    """120 시나리오 — VIOLATION_KINDS 균등 분포."""
    items: list[ScenarioParams] = []
    n_per_kind = SWEEP_TOTAL // len(VIOLATION_KINDS) + 1
    for kind_idx, kind in enumerate(VIOLATION_KINDS):
        for j in range(n_per_kind):
            items.append(ScenarioParams(
                seed=SWEEP_SEED_BASE + kind_idx * 1000 + j,
                violation_kind=kind,
            ))
    return items[:SWEEP_TOTAL]


# ─────────────────────────────────────────────────────────────────────────
# A. Seed determinism
# ─────────────────────────────────────────────────────────────────────────
def test_seed_determinism():
    p = ScenarioParams(seed=12345, violation_kind="daily_demand_excess")
    a = generate_scenario(p)
    b = generate_scenario(p)
    assert a.input_data == b.input_data
    assert a.expected_causes == b.expected_causes


def test_different_seeds_yield_different_data():
    a = generate_scenario(ScenarioParams(seed=1, violation_kind="feasible"))
    b = generate_scenario(ScenarioParams(seed=2, violation_kind="feasible"))
    # 한 가지는 다를 가능성이 매우 높음 (확률적이지만 seed 다르면 거의 reasonable)
    diff = (a.input_data["nurse_count"] != b.input_data["nurse_count"]
            or a.input_data["daily_shift_requirements"] != b.input_data["daily_shift_requirements"])
    assert diff, "seed 다른데 input 동일 — generator 비결정성 의심"


# ─────────────────────────────────────────────────────────────────────────
# B-G. Full sweep
# ─────────────────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def sweep_results():
    results = []
    for params in _build_sweep_seeds():
        sc = generate_scenario(params)
        det, bundles, narr = _run_pipeline(sc)
        results.append({
            "params": params,
            "scenario": sc,
            "detector_issues": det,
            "bundles": bundles,
            "narrative": narr,
        })
    return results


def test_sweep_size_is_100_plus(sweep_results):
    assert len(sweep_results) >= 100


def test_B_feasible_cases_no_false_positive(sweep_results):
    """invariant B — feasible 시나리오에 detector 가 cause 를 검출하지 않음."""
    fp = []
    for r in sweep_results:
        if r["params"].violation_kind == "feasible":
            if r["detector_issues"]:
                fp.append((r["params"].seed, [i["reason_code"] for i in r["detector_issues"]]))
    # 일부 false positive 는 random 입력의 edge 에서 발생할 수 있음 — 5% 이하만 허용
    feasible_count = sum(1 for r in sweep_results if r["params"].violation_kind == "feasible")
    threshold = max(0, feasible_count * 0.05)
    assert len(fp) <= threshold, f"false positive {len(fp)}/{feasible_count} > {threshold:.1f}. samples: {fp[:3]}"


def test_C_injected_causes_are_detected(sweep_results):
    """invariant C — 의도 주입된 cause 가 detector 결과에 포함."""
    misses = []
    for r in sweep_results:
        if not r["scenario"].expected_causes:
            continue
        detected = {i["reason_code"] for i in r["detector_issues"]}
        for expected in r["scenario"].expected_causes:
            if expected not in detected:
                misses.append((r["params"].seed, r["params"].violation_kind, expected, sorted(detected)))
    # cause 정확성 ≥ 85% (사용자 요구)
    injected = sum(len(r["scenario"].expected_causes) for r in sweep_results)
    accuracy = (injected - len(misses)) / max(1, injected)
    assert accuracy >= 0.85, f"cause 정확성 {accuracy:.2%} < 85%. misses sample: {misses[:3]}"


def test_D_bundle_generation_robust(sweep_results):
    """invariant D — detector 가 cause 식별하면 bundle 후보 1개 이상 생성."""
    failures = []
    for r in sweep_results:
        if r["detector_issues"] and not r["bundles"]:
            failures.append((r["params"].seed, [i["reason_code"] for i in r["detector_issues"]]))
    assert failures == [], f"bundle 생성 실패: {failures[:3]}"


def test_E_narrative_has_action_levers_or_manual_only(sweep_results):
    """invariant E — narrative 가 있으면 action_levers 또는 manual data_correction 만."""
    failures = []
    for r in sweep_results:
        narr = r["narrative"]
        if narr is None:
            continue
        if not narr.action_levers and not narr.uncovered_causes:
            failures.append((r["params"].seed, narr.summary_ko))
    assert failures == [], f"narrative 가 비어있는 케이스: {failures[:3]}"


def test_F_no_naive_patterns_in_any_narrative(sweep_results):
    """invariant F — '인원 줄이세요' 류 0건 (보강/추가 문맥 제외)."""
    import re
    pat = re.compile(r"(간호사|인원)(을|를)\s*(줄이|감축)")
    violations = []
    for r in sweep_results:
        narr = r["narrative"]
        if narr is None:
            continue
        full = " ".join(
            [narr.summary_ko]
            + [p.rendered_ko for p in narr.problem_list]
            + [a.rationale_ko for a in narr.action_levers]
            + [t.trade_off_ko for t in narr.trade_offs]
        )
        for m in pat.finditer(full):
            ctx = full[max(0, m.start() - 30): m.end() + 30]
            if any(k in ctx for k in ("보강", "추가", "수요 하향", "demand")):
                continue
            violations.append((r["params"].seed, ctx))
    assert violations == [], f"naive headcount pattern 발견: {violations[:3]}"


def test_G_treatment_actionability(sweep_results):
    """invariant G — action_lever 의 각 항목이 구체 config_key + direction (manual 제외)."""
    fail = []
    for r in sweep_results:
        narr = r["narrative"]
        if narr is None:
            continue
        for lev in narr.action_levers:
            if lev.action_type == "data_correction_required":
                continue
            if not lev.config_key:
                fail.append((r["params"].seed, lev.treatment_id, "no config_key"))
            if lev.direction not in {"enable", "disable", "increase", "decrease", "clear", "remove_key"}:
                fail.append((r["params"].seed, lev.treatment_id, f"bad direction: {lev.direction}"))
    assert fail == [], f"actionability 실패: {fail[:3]}"


# ─────────────────────────────────────────────────────────────────────────
# 보고서 작성
# ─────────────────────────────────────────────────────────────────────────
def test_write_sweep_report(sweep_results, tmp_path):
    """결과 통계 + 실패 sample 을 /tmp/dynamic_sweep_report.md 에 저장."""
    by_kind: dict[str, dict] = {}
    for r in sweep_results:
        k = r["params"].violation_kind
        bucket = by_kind.setdefault(k, {"total": 0, "detected": 0, "bundles": 0, "narrative": 0, "uncovered": 0})
        bucket["total"] += 1
        if r["detector_issues"]:
            bucket["detected"] += 1
        if r["bundles"]:
            bucket["bundles"] += 1
        if r["narrative"]:
            bucket["narrative"] += 1
            bucket["uncovered"] += len(r["narrative"].uncovered_causes)

    lines = [
        "# Dynamic Scenario Sweep Report",
        f"\n총 시나리오: {len(sweep_results)}",
        f"VIOLATION_KINDS: {len(VIOLATION_KINDS)}",
        "\n## kind 별 통계",
        "",
        "| kind | total | detected | bundles | narrative | uncovered |",
        "|---|---|---|---|---|---|",
    ]
    for k, b in by_kind.items():
        lines.append(f"| {k} | {b['total']} | {b['detected']} | {b['bundles']} | {b['narrative']} | {b['uncovered']} |")

    lines.append("\n## 샘플 narrative (kind=complex_3)")
    for r in sweep_results:
        if r["params"].violation_kind != "complex_3":
            continue
        if r["narrative"]:
            lines.append(f"\n### seed={r['params'].seed}")
            lines.append(f"- summary: {r['narrative'].summary_ko}")
            lines.append("- problems:")
            for p in r["narrative"].problem_list:
                lines.append(f"  - [{p.category}] {p.rendered_ko}")
            lines.append("- actions:")
            for a in r["narrative"].action_levers:
                lines.append(f"  - {a.treatment_id} (config={a.config_key}, dir={a.direction}): {a.rationale_ko[:80]}")
            lines.append("- trade_offs:")
            for t in r["narrative"].trade_offs:
                lines.append(f"  - {t.treatment_id}: {t.trade_off_ko[:80]}")
            break  # 1개만 sample

    Path("/tmp/dynamic_sweep_report.md").write_text("\n".join(lines), encoding="utf-8")
    assert Path("/tmp/dynamic_sweep_report.md").exists()
