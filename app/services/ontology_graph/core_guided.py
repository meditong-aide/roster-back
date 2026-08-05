"""Core-guided diagnosis 사이클 — 피드백의 핵심 다음 기능.

  Production CP-SAT 실패 → assumption core 로 범위 축소 → graph 로 구조적 원인 설명 →
  최소 변경 repair 생성 → Production CP-SAT 재검증.

여기 solver=IR CP-SAT(assumption 지원, 공통 automaton 으로 exact). 운영 배선 시 production
CP-SAT adapter 로 교체(FeasibilityVerifier + core 인터페이스). MUS 와 경쟁이 아니라 **core 를
범위 축소 도구로** 사용: core 는 어디가 의심스러운지, graph 는 그 영역이 수치적으로 왜 불가능한지.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from services.ontology_graph.roster_ir import (
    CellDomainRule,
    CoverageRule,
    RosterConstraintIR,
    compile_graph,
    parse_to_ir,
)


@dataclass
class CoreResult:
    feasible: bool | None
    core: dict = field(default_factory=dict)     # {coverage_days:[...], cells:[(nid,d),...]}


def compile_cpsat_assumptions(ir: RosterConstraintIR, time_limit: float = 5.0) -> CoreResult:
    """IR CP-SAT + assumption literals(커버리지·셀). INFEASIBLE 이면 **충분 core** 반환.

    시퀀스 automaton 은 항상 on(relax 대상 아님). 완화 가능한 커버리지·셀만 assumption 으로 걸어
    "이들 중 어떤 부분집합이 시퀀스와 함께 infeasible 인가"를 얻는다(= 범위 축소).
    """
    try:
        from ortools.sat.python import cp_model
    except Exception:
        return CoreResult(None)
    from services.ontology_graph.frontier_dp import build_shift_automaton

    nurses, config = compile_graph(ir)
    _, triples, start, finals = build_shift_automaton(config)
    L = {"D": 0, "E": 1, "N": 2, "O": 3}
    days = ir.days
    specs = {s.nurse_id: (s.allowed or frozenset({"D", "E", "N"})) for s in ir.nurses}
    cells = {(r.nurse_id, r.day): r for r in ir.rules if isinstance(r, CellDomainRule)}
    cov = next((r for r in ir.rules if isinstance(r, CoverageRule)), CoverageRule(0, 0, 0))

    m = cp_model.CpModel()
    isD: dict = {}
    isE: dict = {}
    isN: dict = {}
    lit_ref: dict = {}                                # lit.Index() -> ("cov", d) | ("cell", nid, d)
    assumptions: list = []
    for s in ir.nurses:
        i = s.nurse_id
        labels = []
        for d in range(days):
            lv = m.NewIntVar(0, 3, f"lab_{i}_{d}")
            cell = cells.get((i, d))
            banned = cell.banned if cell else frozenset()
            forced_off = bool(cell and cell.forced_off)
            if forced_off:
                allowed = {"O"}
            else:
                allowed = {c for c in ("D", "E", "N") if c in specs[i] and c not in banned}
                if "O" not in banned:
                    allowed.add("O")
            if cell:                                  # 셀 제약은 assumption 으로 gate(완화 가능)
                clit = m.NewBoolVar(f"cell_{i}_{d}")
                for c in ("D", "E", "N", "O"):
                    if c not in allowed:
                        m.Add(lv != L[c]).OnlyEnforceIf(clit)
                lit_ref[clit.Index()] = ("cell", i, d)
                assumptions.append(clit)
            else:                                     # 셀 제약 없으면 work-set 만(항상 on)
                for c in ("D", "E", "N"):
                    if c not in specs[i]:
                        m.Add(lv != L[c])
            labels.append(lv)
            bN = m.NewBoolVar(f"N_{i}_{d}")
            bD = m.NewBoolVar(f"D_{i}_{d}")
            bE = m.NewBoolVar(f"E_{i}_{d}")
            for b, val in ((bN, 2), (bD, 0), (bE, 1)):
                m.Add(lv == val).OnlyEnforceIf(b)
                m.Add(lv != val).OnlyEnforceIf(b.Not())
            isN[i, d], isD[i, d], isE[i, d] = bN, bD, bE
        m.AddAutomaton(labels, start, finals, triples)
    for d in range(days):
        covlit = m.NewBoolVar(f"cov_{d}")
        m.Add(sum(isD[s.nurse_id, d] for s in ir.nurses) >= cov.reqD).OnlyEnforceIf(covlit)
        m.Add(sum(isE[s.nurse_id, d] for s in ir.nurses) >= cov.reqE).OnlyEnforceIf(covlit)
        m.Add(sum(isN[s.nurse_id, d] for s in ir.nurses) >= cov.reqN).OnlyEnforceIf(covlit)
        lit_ref[covlit.Index()] = ("cov", d)
        assumptions.append(covlit)

    for a in assumptions:
        m.AddAssumption(a)
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit
    solver.parameters.num_search_workers = 1
    st = solver.Solve(m)
    if st in (cp_model.FEASIBLE, cp_model.OPTIMAL):
        return CoreResult(True)
    if st != cp_model.INFEASIBLE:
        return CoreResult(None)
    core = {"coverage_days": [], "cells": []}
    for idx in solver.SufficientAssumptionsForInfeasibility():
        ref = lit_ref.get(idx)
        if ref and ref[0] == "cov":
            core["coverage_days"].append(ref[1])
        elif ref and ref[0] == "cell":
            core["cells"].append((ref[1], ref[2]))
    core["coverage_days"].sort()
    return CoreResult(False, core)


def core_days(core: dict) -> list[int]:
    return sorted(set(core.get("coverage_days", [])) | {d for _, d in core.get("cells", [])})


@dataclass
class CycleResult:
    solver_feasible: bool | None
    core: dict = field(default_factory=dict)
    certificate: object = None            # graph 구조 원인
    repairs: list = field(default_factory=list)
    core_window: tuple | None = None


def core_guided_diagnosis(nurses: list, config: dict, num_days: int,
                          verifier=None) -> CycleResult:
    """전체 사이클: solver(assumption core) → graph 설명 → repair → solver 재검증."""
    from services.ontology_graph.hybrid_solver import solve_hybrid
    from services.ontology_graph.repair import verify_repairs

    ir = parse_to_ir(nurses, config, num_days)
    cr = compile_cpsat_assumptions(ir)
    if cr.feasible is not False:
        return CycleResult(cr.feasible)                # infeasible 아님 → 사이클 종료
    # core 범위 축소 → graph 로 구조적 원인 설명(core window 로 국소화)
    cd = core_days(cr.core)
    window = (min(cd), max(cd) + 1) if cd else (0, num_days)
    gr = solve_hybrid(nurses, config, num_days)         # 전 인스턴스 graph 원인(빠름·in-scope)
    cert = gr.certificate if gr.status == "INFEASIBLE_CERTIFIED" else None
    # repair 생성 + solver 재검증(verifier=운영 exact 시 solver_verified)
    reps = verify_repairs(nurses, config, num_days, verifier=verifier)
    return CycleResult(False, core=cr.core, certificate=cert, repairs=reps, core_window=window)
