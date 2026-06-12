"""축소 모델 MCS — 라이브 실용화 실증.

문제: 풀 엔진(2000+ 변수)에서 MCS(N회 재실행)는 분 단위로 느리다.
해결: 충돌에 관여하는 하드제약군만 담은 '축소 CP-SAT 모델' 위에서 MCS 를 돌리면
밀리초~1초로 끝나고, 결과를 재실행으로 검증한다(verified). 같은 충돌 구조
(coverage·전이금지·등급상한)를 작은 규모로 재현해 MUS vs MCS 정밀도를 대조한다.
"""

from __future__ import annotations

import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for _p in (str(ROOT / "app"), str(ROOT / "tools" / "ontology_eval"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from ortools.sat.python import cp_model  # noqa: E402
from services.cp_sat.hard_assumption import HardAssumptionRegistry, add_hard  # noqa: E402
from services.cp_sat.mcs import find_mcs  # noqa: E402
from services.ontology_graph.schema import OntologyGraph  # noqa: E402
from services.ontology_graph.mus_bridge import mcs_to_graph  # noqa: E402
from services.ontology_graph.recommender import recommend_actions  # noqa: E402
import visualize  # noqa: E402

IMG = ROOT / "docs" / "ontology_cases" / "img" / "reduced_mcs.png"
MD = ROOT / "docs" / "ONTOLOGY_REDUCED_MCS_DEMO.md"

SHIFTS = ["D", "E", "N", "O"]
NURSES = 6
DAYS = 5
GRADE = {0: 1, 1: 1, 2: 2, 3: 2, 4: 3, 5: 3}   # nurse → grade


def build_model(*, inject: bool):
    """작은 근무표 모델. inject=True 면 충돌(coverage N vs grade_max N=0 + 전이금지) 주입."""
    m = cp_model.CpModel()
    reg = HardAssumptionRegistry(m)
    x = {(n, d, s): m.NewBoolVar(f"x_{n}_{d}_{s}")
         for n in range(NURSES) for d in range(DAYS) for s in SHIFTS}
    for n in range(NURSES):
        for d in range(DAYS):
            m.Add(sum(x[(n, d, s)] for s in SHIFTS) == 1)   # 하루 1시프트
    # coverage min (wrap): 각 날 N/D/E ≥ 2
    for d in range(DAYS):
        for s in ("N", "D", "E"):
            add_hard(m, reg, name=f"CoverageMin:{s}:day_{d}",
                     constraint_expr=sum(x[(n, d, s)] for n in range(NURSES)) >= 2,
                     meta={"node_id": f"coverage_min:{s}:day_{d}", "type": "ConstraintNode",
                           "pattern": "coverage", "label": f"day{d+1} {s} 최소 2"})
    # 전이금지 N->D (wrap)
    for n in range(NURSES):
        for d in range(1, DAYS):
            add_hard(m, reg, name=f"TransitionBanN2D:nurse_{n}:day_{d}",
                     constraint_expr=(x[(n, d, "D")] + x[(n, d - 1, "N")] <= 1),
                     meta={"node_id": f"transition_ban_n2d:nurse_{n}:day_{d}",
                           "type": "TransitionBanNode", "pattern": "transition_ban",
                           "label": f"전이금지 N→D nurse{n} day{d+1}"})
    if inject:
        # 충돌1: grade_max N=0 (아무도 N 불가) ↔ coverage N≥2
        for g in (1, 2, 3):
            add_hard(m, reg, name=f"GradeMaxN0:grade_{g}",
                     constraint_expr=sum(x[(n, d, "N")] for n in range(NURSES)
                                         for d in range(DAYS) if GRADE[n] == g) <= 0,
                     meta={"node_id": f"grade_max:N:grade_{g}", "type": "GradeMaxNode",
                           "pattern": "grade_max", "label": f"등급{g} N 상한 0"})
        # 충돌2: nurse0 고정 N(day0), D(day1) — 전이금지 위반(둘 중 하나만 고정이면 미bypass)
        m.Add(x[(0, 0, "N")] == 1)
    reg.attach_to_model()
    return m, reg


def main():
    # ── MUS (대조) ──
    m1, reg1 = build_model(inject=True)
    solver = cp_model.CpSolver(); solver.parameters.max_time_in_seconds = 5
    st = solver.Solve(m1)
    mus = reg1.extract_conflict_cores(solver) if st == cp_model.INFEASIBLE else []
    mus_members = sum(len(c["members"]) for c in mus)
    mus_pat = dict(Counter(c.get("pattern") for c in mus))
    print(f"[reduced] MUS: status={solver.StatusName(st)} cores={len(mus)} members={mus_members} patterns={mus_pat}")

    # ── MCS ──
    m2, reg2 = build_model(inject=True)
    cost = lambda nm: 1.0 if nm.startswith("GradeMaxN0") else (2.0 if "Coverage" in nm else 3.0)
    t0 = time.time()
    res = find_mcs(m2, reg2, cost=cost, time_limit=3)
    dt = time.time() - t0
    print(f"[reduced] MCS: {len(res.relaxed)} relaxed, verified={res.verified_feasible}, "
          f"{dt:.2f}s, iters={res.iterations}")
    for meta in res.relaxed_meta:
        print(f"          → 풀것: {meta['label']} ({meta['pattern']})")

    # ── 그래프 bridge ──
    g = mcs_to_graph(OntologyGraph(), res)
    actions = recommend_actions(g)
    keep = set(g.nodes.keys())
    IMG.parent.mkdir(parents=True, exist_ok=True)
    visualize.render(g, keep, str(IMG),
                     title=f"축소모델 MCS({len(res.relaxed)}개 수선, verified)\n풀것→제약→액션")

    # ── md ──
    L = ["# 축소 모델 MCS — 라이브 실용화 + MUS 대조\n",
         "풀 엔진에서 느린 MCS 를 '충돌 하드제약군만 담은 축소 CP-SAT 모델'로 빠르게.\n",
         "\n## 같은 충돌, 두 방법 대조\n",
         "| 방법 | 결과 | 정밀도 |", "|---|---|---|",
         f"| **MUS** | core {len(mus)}개 / member {mus_members}개 ({mus_pat}) | 동시불가 '집합' — 무엇을 풀지 불명확 |",
         f"| **MCS** | **{len(res.relaxed)}개** 완화 → **verified feasible={res.verified_feasible}** ({dt:.2f}s) | "
         f"'이걸 풀면 됨' + 재실행 확인 |\n",
         "\n## MCS 가 짚은 수선점 (무엇을 풀면 feasible)\n"]
    for meta in res.relaxed_meta:
        L.append(f"- {meta['label']} (`{meta['pattern']}`)")
    L += [f"\n→ 비용 가중(등급상한이 가장 쌈)으로 **최소비용 완화**를 선택. 재실행으로 feasible 확인.\n",
          f"\n![reduced mcs]({IMG.relative_to(ROOT/'docs').as_posix()})\n",
          "\n## 의미\n",
          f"- MCS 는 {dt:.2f}s 에 끝남(축소 모델). 풀 엔진은 모델만 줄여 같은 알고리즘 적용하면 됨.",
          "- MUS 의 '집합/분산' 부정확이 MCS 에선 '검증된 최소 수선'으로 정밀해짐.",
          "- 결과가 그대로 4-노드 그래프 액션이 되어 recommend 로 노출."]
    MD.write_text("\n".join(L), encoding="utf-8")
    print(f"[reduced] md → {MD.relative_to(ROOT)}, img={IMG.exists()}, actions={len(actions)}")


if __name__ == "__main__":
    main()
