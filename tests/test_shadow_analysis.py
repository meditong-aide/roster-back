"""Shadow 로그 offline 분석 — (request_id, attempt_id) join + mismatch 사유 분류."""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "infeasible_cases"))

from shadow_analysis import analyze, join_by_run, parse_log  # noqa: E402


def _line(rec):
    return "[Shadow] " + json.dumps(rec)


def _graph(rid, status, cert=None, unmodeled=None, ih="h", stage="primary_hard"):
    return {"request_id": rid, "attempt_id": "primary_hard", "graph_model_stage": stage,
            "graph_status": status, "certificate": cert, "unmodeled": unmodeled or [],
            "input_hash": ih}


def _prod(rid, primary, ih="h", source="raw"):
    return {"kind": "production", "request_id": rid, "attempt_id": "primary_hard",
            "primary_hard_status": primary, "input_hash": ih, "status_source": source}


def test_join_by_run_not_input_hash():
    """같은 입력(같은 input_hash) 다른 실행이 request_id 로 분리돼야."""
    lines = [_line(_graph("r1", "INFEASIBLE_CERTIFIED", "recovery_off_starvation", ih="same")),
             _line(_prod("r1", "INFEASIBLE", ih="same")),
             _line(_graph("r2", "FEASIBLE_WITNESS", ih="same")),
             _line(_prod("r2", "FEASIBLE", ih="same"))]
    j = join_by_run(parse_log(lines))
    assert ("r1", "primary_hard") in j and ("r2", "primary_hard") in j
    assert j[("r1", "primary_hard")]["graph"]["graph_status"] == "INFEASIBLE_CERTIFIED"


def test_real_false_certificate():
    """동일 stage·입력·in-scope·raw 인데 graph INFEASIBLE vs prod FEASIBLE = 진짜 치명."""
    recs = parse_log([_line(_graph("r1", "INFEASIBLE_CERTIFIED", "recovery_off_starvation")),
                      _line(_prod("r1", "FEASIBLE"))])
    a = analyze(recs)
    assert a["graph_false_certificate"] == 1
    assert a["mismatch_reason"]["GRAPH_FALSE_CERTIFICATE"] == 1


def test_inferred_status_not_false_certificate():
    """production status 가 raw 아니면(추론) 확정 false-cert 아님 → PRODUCTION_STATUS_INFERRED."""
    recs = parse_log([_line(_graph("r1", "INFEASIBLE_CERTIFIED", "recovery_off_starvation")),
                      _line(_prod("r1", "FEASIBLE", source="empty"))])
    a = analyze(recs)
    assert a["graph_false_certificate"] == 0
    assert a["mismatch_reason"]["PRODUCTION_STATUS_INFERRED"] == 1


def test_scope_mismatch_not_false_certificate():
    recs = parse_log([_line(_graph("r1", "INFEASIBLE_CERTIFIED", "coverage_deficit",
                                   unmodeled=["config.off_days"])),
                      _line(_prod("r1", "FEASIBLE"))])
    a = analyze(recs)
    assert a["graph_false_certificate"] == 0
    assert a["mismatch_reason"]["MODEL_SCOPE_MISMATCH"] == 1


def test_input_version_mismatch():
    recs = parse_log([_line(_graph("r1", "INFEASIBLE_CERTIFIED", "recovery_off_starvation", ih="A")),
                      _line(_prod("r1", "FEASIBLE", ih="B"))])
    a = analyze(recs)
    assert a["mismatch_reason"]["INPUT_VERSION_MISMATCH"] == 1
    assert a["graph_false_certificate"] == 0
