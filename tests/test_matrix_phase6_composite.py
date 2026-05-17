"""U-6 — matrix harness Phase 6 (CX-MIX-046~050) composite case 검증.

각 case 는 service-level (HTTP/solver 우회) 로 합성된 payload 의 ontology 정합성 검증:
  - causes ≥6 동시 노출
  - hard_case=true + criteria_matched ≥1
  - manual_investigation treatment append (uncovered 영역 있을 때)
  - payload.graph dangling=0 + 5 카테고리 라벨링
  - NO_ASSIGNMENT* cause 진입 0건

목표: 5 cases 중 ≥3 PASS (acceptance: ≥3/5).
"""

from __future__ import annotations

import sys
from pathlib import Path

# tools.harness 경로 추가
_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "tools" / "harness"))
sys.path.insert(0, str(_ROOT / "app"))

import matrix_case_e2e as harness  # noqa: E402


def _run_case_no_io(fn):
    """case 함수는 _save_payload 가 disk write — 테스트에서는 monkey-patch 로 우회."""
    saved = []
    orig = harness._save_payload
    harness._save_payload = lambda *a, **kw: saved.append(a[0] if a else None)
    try:
        result = fn([])  # base list 사용 안 함 (composite is synthetic)
    finally:
        harness._save_payload = orig
    return result


def test_phase6_case_mix_046_passes() -> None:
    r = _run_case_no_io(harness.case_mix_046)
    assert r["pass"], f"CX-MIX-046 FAIL: missing={r.get('missing_causes')} graph={r.get('graph_stats')}"
    assert r["hard_case"] is True
    assert r["graph_stats"]["dangling_edges"] == 0
    assert r["no_assignment_leak"] is False


def test_phase6_case_mix_047_passes() -> None:
    r = _run_case_no_io(harness.case_mix_047)
    assert r["pass"], f"CX-MIX-047 FAIL: missing={r.get('missing_causes')}"
    assert r["hard_case"] is True


def test_phase6_case_mix_048_passes() -> None:
    r = _run_case_no_io(harness.case_mix_048)
    assert r["pass"], f"CX-MIX-048 FAIL: missing={r.get('missing_causes')}"
    assert r["hard_case"] is True


def test_phase6_case_mix_049_passes() -> None:
    r = _run_case_no_io(harness.case_mix_049)
    assert r["pass"], f"CX-MIX-049 FAIL: missing={r.get('missing_causes')}"
    assert r["hard_case"] is True


def test_phase6_case_mix_050_passes() -> None:
    r = _run_case_no_io(harness.case_mix_050)
    assert r["pass"], f"CX-MIX-050 FAIL: missing={r.get('missing_causes')}"
    assert r["hard_case"] is True


def test_phase6_aggregate_at_least_3_of_5_pass() -> None:
    """매트릭스 acceptance: ≥3/5 PASS."""
    cases = [
        harness.case_mix_046, harness.case_mix_047, harness.case_mix_048,
        harness.case_mix_049, harness.case_mix_050,
    ]
    results = [_run_case_no_io(fn) for fn in cases]
    passed = sum(1 for r in results if r.get("pass"))
    assert passed >= 3, (
        f"Phase 6 acceptance violation: {passed}/5 PASS (≥3 required). "
        f"per-case: {[(r['case_id'], r.get('pass')) for r in results]}"
    )
