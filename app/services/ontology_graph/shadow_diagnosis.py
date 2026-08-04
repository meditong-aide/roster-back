"""운영 shadow 배선 — 운영 결과에 영향 없이 graph 진단을 함께 로그.

피드백 step1: production CP-SAT 을 중단시키지 말고, graph/IR 진단을 나란히 로그로 남겨 실제
데이터에서 production↔graph 판정 일치율·certificate 정확도·UNKNOWN 사유·미지원 제약을 관찰.

**정직한 상태(피드백 point1)**: 이 함수는 **graph 쪽만** 실행·로그한다. production↔graph 비교는
호출자가 `production_status`(FEASIBLE/INFEASIBLE/TIMEOUT/ERROR)를 넘길 때만 성립한다. 운영 CP-SAT
결과를 표준화해 넘기는 **production adapter 주입은 아직 미완료** → 현재 presolve-time 훅은
graph-only 로그다(비교 아님).

**안전**: env AIDE_SHADOW_DIAGNOSIS 게이팅(기본 off = 완전 no-op). 내부 전부 try/except →
절대 운영 경로에 예외 전파 안 함. 결과값 무영향(로그만).

상태를 이분법으로 저장하지 않는다(피드백 point6): graph 는 INFEASIBLE_CERTIFIED /
FEASIBLE_WITNESS / UNKNOWN_SCOPE / UNKNOWN_WIDTH / UNKNOWN_TIMEOUT / ERROR 로 구분.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import os
import time

from services.ontology_graph.short_circuit import (
    GRAPH_ENGINE_VERSION,
    IR_SCHEMA_VERSION,
    classify_graph_unknown,
    repair_verify_flag,
    shadow_enabled,
    short_circuit_flag,
)


def _input_hash(nurses, config, num_days) -> str:
    try:
        blob = json.dumps({"n": nurses, "c": config, "d": num_days},
                          sort_keys=True, default=str, ensure_ascii=False)
        return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]
    except Exception:
        return "?"


def _sample_gate(request_id: str | None) -> bool:
    """AIDE_SHADOW_SAMPLE_PCT(0~100) — request_id 해시 기반 결정적 샘플링(운영 지연 방지)."""
    try:
        pct = int(os.environ.get("AIDE_SHADOW_SAMPLE_PCT", "100"))
    except ValueError:
        pct = 100
    if pct >= 100:
        return True
    if pct <= 0 or not request_id:
        return pct > 0 and not request_id  # request_id 없으면 pct>0 일 때만(보수적)
    h = 0
    for ch in str(request_id):
        h = (h * 131 + ord(ch)) & 0xFFFFFFFF
    return (h % 100) < pct


def run_shadow(nurses: list, config: dict, year: int, month: int, *,
               request_id: str | None = None, attempt_id: str = "primary_hard",
               graph_model_stage: str = "primary_hard",
               production_model_version: str | None = None,
               budget: int = 1_200_000) -> dict | None:
    """graph 진단을 shadow 로 실행·로그(graph-only; production 비교는 log_production_status 와
    request_id+attempt_id 로 join). 운영 결과 무영향. 비활성/샘플제외/실패 시 None.

    graph 는 **primary_hard 모델**(config 그대로, fallback 전)을 분석 → attempt_id/stage 로 표기.
    """
    if not shadow_enabled() or not _sample_gate(request_id):
        return None
    try:
        num_days = calendar.monthrange(int(year), int(month))[1]
    except Exception:
        return None
    rec: dict = {
        "request_id": request_id, "attempt_id": attempt_id,
        "graph_model_stage": graph_model_stage,
        "year": year, "month": month, "num_days": num_days,
        "input_hash": _input_hash(nurses, config, num_days),
        "graph_version": GRAPH_ENGINE_VERSION, "ir_version": IR_SCHEMA_VERSION,
        "production_model_version": production_model_version,
        "flags": {"short_circuit": short_circuit_flag(), "repair_verify": repair_verify_flag()},
    }
    try:
        from services.ontology_graph.scope_manifest import unmodeled_active
        rec["unmodeled"] = unmodeled_active(nurses, config)
    except Exception as e:
        rec["unmodeled_error"] = str(e)
    try:
        from services.ontology_graph.hybrid_solver import solve_hybrid
        t = time.time()
        gr = solve_hybrid(nurses, config, num_days, budget=budget)
        rec["graph_ms"] = round((time.time() - t) * 1000, 1)
        # 이분법 금지: UNKNOWN 을 사유별로
        if gr.status == "UNKNOWN":
            rec["graph_status"] = classify_graph_unknown(nurses, config)
        else:
            rec["graph_status"] = gr.status
        rec["graph_components"] = getattr(gr, "components", 0)
        rec["certificate"] = gr.certificate.kind if gr.certificate else None
        if gr.status == "INFEASIBLE_CERTIFIED":
            try:
                from services.ontology_graph.core_guided import compile_cpsat_assumptions, core_days
                from services.ontology_graph.roster_ir import parse_to_ir
                cr = compile_cpsat_assumptions(parse_to_ir(nurses, config, num_days))
                rec["ir_cpsat"] = cr.feasible
                rec["core_days"] = core_days(cr.core) if cr.feasible is False else None
                rec["core_cells"] = len(cr.core.get("cells", [])) if cr.feasible is False else 0
            except Exception as e:
                rec["core_error"] = str(e)
    except Exception as e:
        rec["graph_status"] = "ERROR"
        rec["graph_error"] = str(e)
    _emit(rec)
    return rec


def log_production_status(nurses: list, config: dict, year: int, month: int,
                          primary_hard_status: str, *,
                          request_id: str | None = None, attempt_id: str = "primary_hard",
                          status_source: str = "raw", raw_solver_status: str | None = None,
                          final_service_status: str | None = None,
                          fallback_applied: str | None = None,
                          production_model_version: str | None = None) -> dict | None:
    """운영 solve 상태를 로그 → graph 로그와 **request_id+attempt_id** 로 join(피드백 fix1).

    primary_hard 와 final(=fallback 후)을 분리(fix2): graph 는 primary_hard 를 분석하므로 비교는
    primary_hard_status 로. final_service_status/fallback_applied 는 맥락. status_source 로 신뢰도.
    """
    if not shadow_enabled():
        return None
    try:
        num_days = calendar.monthrange(int(year), int(month))[1]
    except Exception:
        return None
    rec = {"kind": "production", "request_id": request_id, "attempt_id": attempt_id,
           "year": year, "month": month, "num_days": num_days,
           "input_hash": _input_hash(nurses, config, num_days),
           "primary_hard_status": primary_hard_status, "status_source": status_source,
           "raw_solver_status": raw_solver_status,
           "final_service_status": final_service_status, "fallback_applied": fallback_applied,
           "production_model_version": production_model_version,
           "graph_version": GRAPH_ENGINE_VERSION, "ir_version": IR_SCHEMA_VERSION}
    _emit(rec)
    return rec


def _emit(rec: dict) -> None:
    line = "[Shadow] " + json.dumps(rec, ensure_ascii=False, default=str)
    print(line)
    path = os.environ.get("AIDE_SHADOW_LOG")
    if path:
        try:
            with open(path, "a") as f:
                f.write(line + "\n")
        except Exception:
            pass
