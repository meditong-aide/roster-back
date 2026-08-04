"""Shadow 로그 offline 분석 — input_hash join + false-certificate 탐지 + UNKNOWN 사유별."""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "infeasible_cases"))

from shadow_analysis import analyze, join_by_input, parse_log  # noqa: E402


def _line(rec):
    return "[Shadow] " + json.dumps(rec)


def _graph(h, status, cert=None, unmodeled=None):
    return {"input_hash": h, "graph_status": status, "certificate": cert,
            "unmodeled": unmodeled or []}


def _prod(h, status):
    return {"kind": "production", "input_hash": h, "production_status": status}


def test_parse_and_join():
    lines = [_line(_graph("h1", "INFEASIBLE_CERTIFIED", "recovery_off_starvation")),
             _line(_prod("h1", "INFEASIBLE")),
             "noise line",
             _line(_graph("h2", "FEASIBLE_WITNESS"))]
    recs = parse_log(lines)
    assert len(recs) == 3
    j = join_by_input(recs)
    assert j["h1"]["graph"] and j["h1"]["production"]
    assert j["h2"]["production"] is None


def test_false_certificate_detected():
    """prod FEASIBLE 인데 graph INFEASIBLE = 치명 false certificate."""
    recs = parse_log([
        _line(_graph("bad", "INFEASIBLE_CERTIFIED", "recovery_off_starvation")),
        _line(_prod("bad", "FEASIBLE")),
        _line(_graph("ok", "INFEASIBLE_CERTIFIED", "sequence_path_empty")),
        _line(_prod("ok", "INFEASIBLE")),
    ])
    a = analyze(recs)
    assert a["false_certificate"] == 1
    assert "bad" in a["false_certificate_hashes"]
    assert a["agree_infeasible"] == 1


def test_unknown_reasons_separated():
    recs = parse_log([
        _line(_graph("a", "UNKNOWN_WIDTH")),
        _line(_graph("b", "UNKNOWN_SCOPE", unmodeled=["nurse.n_exact"])),
        _line(_graph("c", "UNKNOWN_WIDTH")),
    ])
    a = analyze(recs)
    assert a["unknown_reason"]["UNKNOWN_WIDTH"] == 2      # 재귀 hybrid 대상
    assert a["unknown_reason"]["UNKNOWN_SCOPE"] == 1      # 재귀 hybrid 무관


def test_short_circuit_eligibility():
    recs = parse_log([
        _line(_graph("e1", "INFEASIBLE_CERTIFIED", "recovery_off_starvation")),       # 자격
        _line(_graph("e2", "INFEASIBLE_CERTIFIED", "joint_sequencing_collapse")),     # cert 제외
        _line(_graph("e3", "INFEASIBLE_CERTIFIED", "coverage_deficit",
                     unmodeled=["config.off_days"])),                                  # 범위 밖
    ])
    a = analyze(recs)
    assert a["short_circuit_eligible"] == 1              # e1 만
