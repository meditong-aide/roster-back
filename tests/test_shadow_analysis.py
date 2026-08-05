"""Shadow 로그 offline 분석 — (request_id, attempt_id) join + mismatch 사유 분류."""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "infeasible_cases"))

from shadow_analysis import analyze, join_by_run, parse_log  # noqa: E402


def _line(rec):
    return "[Shadow] " + json.dumps(rec)


def _graph(rid, status, cert=None, unmodeled=None, ih="h", sig="sig"):
    return {"request_id": rid, "attempt_id": "primary_hard", "graph_model_stage": "primary_hard",
            "graph_status": status, "certificate": cert, "unmodeled": unmodeled or [],
            "input_hash": ih, "model_signature": sig}


def _prod(rid, primary, ih="h", source="raw", sig="sig", validator=None):
    return {"kind": "production", "request_id": rid, "attempt_id": "primary_hard",
            "production_model_stage": "fallback_lex_stage1",
            "primary_hard_status": primary, "input_hash": ih, "status_source": source,
            "model_signature": sig, "production_validator_pass": validator}


def test_join_by_run_not_input_hash():
    """같은 입력(같은 input_hash) 다른 실행이 request_id 로 분리돼야."""
    lines = [_line(_graph("r1", "INFEASIBLE_CERTIFIED", "recovery_off_starvation", ih="same")),
             _line(_prod("r1", "INFEASIBLE", ih="same")),
             _line(_graph("r2", "FEASIBLE_WITNESS", ih="same")),
             _line(_prod("r2", "FEASIBLE", ih="same"))]
    j = join_by_run(parse_log(lines))
    assert ("r1", "primary_hard", 1) in j and ("r2", "primary_hard", 1) in j
    assert j[("r1", "primary_hard", 1)]["graph"]["graph_status"] == "INFEASIBLE_CERTIFIED"


def test_real_false_certificate_requires_validator_pass():
    """동일 model_sig·입력·raw + **validator PASS** 여야 진짜 GRAPH_FALSE_CERTIFICATE."""
    recs = parse_log([_line(_graph("r1", "INFEASIBLE_CERTIFIED", "recovery_off_starvation")),
                      _line(_prod("r1", "FEASIBLE", validator=True))])
    a = analyze(recs)
    assert a["graph_false_certificate"] == 1
    assert a["mismatch_reason"]["GRAPH_FALSE_CERTIFICATE"] == 1


def test_unvalidated_mismatch_blocks_canary():
    """validator 미실행(None) FEASIBLE 불일치 = 확정 안 하되 **canary 차단**(무시 금지)."""
    recs = parse_log([_line(_graph("r1", "INFEASIBLE_CERTIFIED", "recovery_off_starvation")),
                      _line(_prod("r1", "FEASIBLE", validator=None))])
    a = analyze(recs)
    assert a["graph_false_certificate"] == 0
    assert a["mismatch_reason"]["GRAPH_FALSE_CERTIFICATE_UNVALIDATED"] == 1
    assert a["unresolved_feasible_mismatches"] == 1     # canary 차단 근거


def test_model_signature_mismatch():
    """model_signature 다르면 같은 모델 비교 아님 → false-cert 아님."""
    recs = parse_log([_line(_graph("r1", "INFEASIBLE_CERTIFIED", "recovery_off_starvation", sig="A")),
                      _line(_prod("r1", "FEASIBLE", sig="B", validator=True))])
    a = analyze(recs)
    assert a["graph_false_certificate"] == 0
    assert a["mismatch_reason"]["MODEL_SIGNATURE_MISMATCH"] == 1


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


def test_eligibility_split_structural_vs_validated():
    """구조 후보 vs 검증된 후보(canary 표본) 분리. raw+동일모델+prod INFEASIBLE 이어야 validated."""
    recs = parse_log([
        # r1: graph INFEASIBLE(허용cert·in-scope) + prod INFEASIBLE raw → structural+validated
        _line(_graph("r1", "INFEASIBLE_CERTIFIED", "recovery_off_starvation")),
        _line(_prod("r1", "INFEASIBLE", source="raw")),
        # r2: graph INFEASIBLE 구조후보이나 prod 없음 → structural만
        _line(_graph("r2", "INFEASIBLE_CERTIFIED", "recovery_off_starvation")),
        # r3: graph INFEASIBLE + prod INFEASIBLE 이나 status_source=empty(추론) → validated 아님
        _line(_graph("r3", "INFEASIBLE_CERTIFIED", "recovery_off_starvation")),
        _line(_prod("r3", "INFEASIBLE", source="empty")),
    ])
    a = analyze(recs)
    assert a["structurally_eligible"] == 3
    assert a["validated_infeasible_pairs"] == 1        # r1 만(둘 다 INFEASIBLE·raw)
    assert a["comparable_raw_pairs"] == 1             # r1 만(raw)
    assert a["paired"] == 2                            # r1, r3
    assert a["unresolved_feasible_mismatches"] == 0    # FEASIBLE 불일치 없음


def test_same_schedule_different_runs_separated():
    """같은 schedule_id 라도 다른 generation_run_id 면 분리(피드백 fix1)."""
    from shadow_analysis import join_by_run
    recs = ([
        {"request_id": "run-A", "attempt_id": "primary_hard", "graph_model_stage": "primary_hard",
         "graph_status": "INFEASIBLE_CERTIFIED", "certificate": "recovery_off_starvation",
         "unmodeled": [], "input_hash": "same", "schedule_id": "sched-1"},
        {"kind": "production", "request_id": "run-A", "attempt_id": "primary_hard",
         "primary_hard_status": "INFEASIBLE", "input_hash": "same", "status_source": "raw",
         "schedule_id": "sched-1"},
        {"request_id": "run-B", "attempt_id": "primary_hard", "graph_model_stage": "primary_hard",
         "graph_status": "FEASIBLE_WITNESS", "certificate": None, "unmodeled": [],
         "input_hash": "same", "schedule_id": "sched-1"},
    ])
    j = join_by_run(recs)
    # 같은 schedule-1 이지만 run-A / run-B 로 분리
    assert ("run-A", "primary_hard", 1) in j and ("run-B", "primary_hard", 1) in j
    assert j[("run-A", "primary_hard", 1)]["graph"]["graph_status"] == "INFEASIBLE_CERTIFIED"
    assert j[("run-B", "primary_hard", 1)]["graph"]["graph_status"] == "FEASIBLE_WITNESS"


def test_graph_and_production_pair_by_shared_seq():
    """동일 run 내에서 graph·production 이 같은 attempt_seq 로 정확히 pair(fix C)."""
    g = {"request_id": "A", "attempt_id": "primary_hard", "attempt_seq": 0,
         "graph_model_stage": "primary_hard", "graph_status": "INFEASIBLE_CERTIFIED",
         "certificate": "recovery_off_starvation", "unmodeled": [], "input_hash": "h",
         "model_signature": "sig"}
    p = {"kind": "production", "request_id": "A", "attempt_id": "primary_hard", "attempt_seq": 0,
         "primary_hard_status": "INFEASIBLE", "input_hash": "h", "status_source": "raw",
         "model_signature": "sig"}
    j = join_by_run([g, p])
    slot = j[("A", "primary_hard", 0)]
    assert slot["graph"] is not None and slot["production"] is not None   # 정확히 pair
    a = analyze([g, p])
    assert a["comparable_raw_pairs"] == 1 and a["validated_infeasible_pairs"] == 1
