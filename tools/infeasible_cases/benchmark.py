"""End-to-end benchmark — graph presolve/repair 가 solver 파이프라인에 주는 실이득 측정.

피드백 5단계: "solver 생략 98%" 같은 단일 수치 대신, 비교군별 정량 지표로 방어한다.
비교군:
  A. Solver only               — 운영 solver(여기선 exact oracle 대역) 단독.
  B. Graph INFEASIBLE presolve → Solver — graph 가 INFEASIBLE 인증하면 solver 생략, 아니면 solver.
  D. Graph repair → Solver verify — infeasible 에 복구후보 생성 후 solver 로 검증.

지표: infeasible 중 graph 인증(solver 생략)률 · graph 시간 · solver 시간 · UNKNOWN 율 ·
      graph false-cert(오인증) · repair 후보수 · solver-verified repair 성공률.

주의: 여기 solver=exact backtracking oracle(운영 CP-SAT 대역, 소형 exact). graph 는 solve_hybrid.
데이터: fuzz 생성(지원범위 in-scope) — feasible/infeasible 혼합. 실병원 캡처는 별도.

**실측 정직한 결론(seed1, 400건, infeasible 172)**:
  · graph false-certificate 0(sound) · infeasible 100% 인증 · UNKNOWN 2% · repair solver-verified 59%
  · **그러나 graph presolve 는 소형 인스턴스에서 exact oracle 보다 66× 느림**(17.25s vs 0.26s).
  → **presolve 시간이득은 운영 solver 가 비쌀 때(대형 인스턴스)만 발생**한다. 소형·빠른 solver
    에선 순수 오버헤드. 따라서 graph 의 실증 가치는 **속도가 아니라 certificate + 검증 repair**
    (설명·행동가능성)다. "solver 98% 생략=빠름" 주장 금지 — 생략은 되지만 소형에선 이득 아님.
    대형 CP-SAT 벤치는 운영 배선 후 별도 측정.
"""

from __future__ import annotations

import random
import sys
import time

_HERE = __file__.rsplit("/", 1)[0]
sys.path.insert(0, _HERE + "/../../app")
sys.path.insert(0, _HERE)

import exact_oracle  # noqa: E402

exact_oracle._BUDGET = 400_000

from exact_oracle import is_feasible  # noqa: E402
from fuzz_crossval import _rand_case  # noqa: E402
from services.ontology_graph.hybrid_solver import solve_hybrid  # noqa: E402
from services.ontology_graph.repair import verify_repairs  # noqa: E402
from services.ontology_graph.verifier import ProductionCpSatVerifier  # noqa: E402

_EXACT = ProductionCpSatVerifier(lambda nu, c, d: is_feasible(nu, c, d))


def run(n=400, seed=1, graph_budget=400_000):
    rng = random.Random(seed)
    agg = {"n": 0, "inf": 0, "feas": 0,
           "graph_cert": 0, "graph_false_cert": 0, "graph_unknown": 0,
           "t_solver": 0.0, "t_graph": 0.0, "t_B": 0.0,
           "repair_cases": 0, "repair_cands": 0, "repair_verified": 0}
    for _ in range(n):
        nu, cfg, D = _rand_case(rng)
        orc = is_feasible(nu, cfg, D)
        if orc is None:
            continue
        agg["n"] += 1
        agg["inf" if orc is False else "feas"] += 1

        # A: solver only
        t = time.time(); is_feasible(nu, cfg, D); agg["t_solver"] += time.time() - t

        # B: graph presolve → solver
        t = time.time()
        gr = solve_hybrid(nu, cfg, D, budget=graph_budget)
        tg = time.time() - t
        agg["t_graph"] += tg
        tB = tg
        if gr.status == "INFEASIBLE_CERTIFIED":
            agg["graph_cert"] += 1
            if orc is not False:                     # 오인증(false certificate) — 반드시 0
                agg["graph_false_cert"] += 1
        else:
            if gr.status == "UNKNOWN":
                agg["graph_unknown"] += 1
            t = time.time(); is_feasible(nu, cfg, D); tB += time.time() - t   # solver 실행
        agg["t_B"] += tB

        # D: repair (infeasible 만)
        if orc is False:
            agg["repair_cases"] += 1
            reps = verify_repairs(nu, cfg, D, verifier=_EXACT)
            agg["repair_cands"] += len(reps)
            if any(r.solver_verified is True for r in reps):
                agg["repair_verified"] += 1
    return agg


def report(agg):
    inf = max(1, agg["inf"])
    print(f"benchmark {agg['n']}건 (infeasible {agg['inf']} / feasible {agg['feas']})\n")
    print("A. Solver only        : 총 %.2fs" % agg["t_solver"])
    print("B. Graph presolve→Slvr: 총 %.2fs  (graph %.2fs)" % (agg["t_B"], agg["t_graph"]))
    print("   시간 절감 vs A     : %+.1f%%" % (100 * (agg["t_solver"] - agg["t_B"]) / max(1e-9, agg["t_solver"])))
    print()
    print("infeasible 사례 중:")
    print("  graph INFEASIBLE 인증(solver 생략): %d/%d = %d%%" %
          (agg["graph_cert"], agg["inf"], 100 * agg["graph_cert"] // inf))
    print("  graph false-certificate(오인증)   : %d   ← 반드시 0" % agg["graph_false_cert"])
    print("  graph UNKNOWN 율(전체)            : %d/%d = %d%%" %
          (agg["graph_unknown"], agg["n"], 100 * agg["graph_unknown"] // max(1, agg["n"])))
    print()
    print("D. repair (infeasible %d건):" % agg["repair_cases"])
    print("   후보 평균 %.1f개, solver-verified 복구 확보 %d/%d = %d%%" %
          (agg["repair_cands"] / max(1, agg["repair_cases"]),
           agg["repair_verified"], agg["repair_cases"],
           100 * agg["repair_verified"] // max(1, agg["repair_cases"])))


if __name__ == "__main__":
    N = int(sys.argv[1]) if len(sys.argv) > 1 else 400
    report(run(N))
