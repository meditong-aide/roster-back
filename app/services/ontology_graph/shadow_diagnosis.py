"""운영 shadow 배선 — 운영 결과에 영향 없이 graph 진단을 함께 로그.

피드백 step1: production CP-SAT 을 중단시키지 말고, graph/IR 진단을 나란히 로그로 남겨 실제
데이터에서 production↔graph 판정 일치율·certificate 정확도·UNKNOWN율·미지원 제약을 관찰한다.
충분히 검증된 뒤에만(offline 분석) INFEASIBLE short-circuit 을 켠다.

**안전**: env AIDE_SHADOW_DIAGNOSIS 로 게이팅(기본 off = 완전 no-op). 내부 전부 try/except →
절대 운영 경로에 예외 전파 안 함. 결과값에 영향 없음(로그만).
"""

from __future__ import annotations

import calendar
import json
import os
import time


def _enabled() -> bool:
    return bool(os.environ.get("AIDE_SHADOW_DIAGNOSIS"))


def run_shadow(nurses: list, config: dict, year: int, month: int, *,
               production_status: str | None = None, budget: int = 1_500_000) -> dict | None:
    """graph/IR 진단을 shadow 로 실행·로그. 운영 결과 무영향. 비활성/실패 시 None.

    production_status: 알려져 있으면(운영 CP-SAT 판정) 일치 여부도 로그.
    """
    if not _enabled():
        return None
    try:
        num_days = calendar.monthrange(int(year), int(month))[1]
    except Exception:
        return None
    rec: dict = {"year": year, "month": month, "num_days": num_days}
    try:
        from services.ontology_graph.scope_manifest import unmodeled_active
        rec["unmodeled"] = unmodeled_active(nurses, config)
    except Exception as e:
        rec["unmodeled_error"] = str(e)
    try:
        from services.ontology_graph.hybrid_solver import solve_hybrid
        t = time.time()
        gr = solve_hybrid(nurses, config, num_days, budget=budget)
        rec["graph_status"] = gr.status
        rec["graph_ms"] = round((time.time() - t) * 1000, 1)
        rec["graph_components"] = getattr(gr, "components", 0)
        rec["certificate"] = gr.certificate.kind if gr.certificate else None
        # infeasible 이면 assumption core 도(범위 축소 데이터)
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
        rec["graph_error"] = str(e)
    if production_status is not None:
        rec["production_status"] = production_status
        # graph INFEASIBLE certificate 는 sound → production 도 infeasible 이어야
        gs = rec.get("graph_status")
        if gs == "INFEASIBLE_CERTIFIED":
            rec["agree"] = (production_status in ("INFEASIBLE", "INFEASIBLE_CERTIFIED"))
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
