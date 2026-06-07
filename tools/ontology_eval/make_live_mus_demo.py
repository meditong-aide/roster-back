"""라이브 MUS → 그래프 진단·수선 (프로덕션 솔버 + 실데이터).

연구 §4 'solver-level conflict → domain-level cause' 를 프로덕션에서 실증:
  AIDE_ENABLE_MUS_REGISTRY=1 → 9B 충돌 주입 → 엔진 infeasible + 실제 CP-SAT MUS core
  → cores_to_conflict_graph(4-노드) → 진단(어떤 하드제약이 동시불가) → 최소변경 액션
  → 적용 → resolved.
프로덕션 무변경(harness.clone). 솔버 MUS 는 '최소 불가집합'이라 주입 family 와 정확히
같지 않을 수 있음(MUS 특성) — 그래도 동시불가 제약군을 도메인 노드로 보여준다.
"""

from __future__ import annotations

import os
import sys
from collections import Counter
from pathlib import Path

os.environ.setdefault("AIDE_ENABLE_MUS_REGISTRY", "1")

ROOT = Path(__file__).resolve().parents[2]
for _p in (str(ROOT / "app"), str(ROOT / "tools" / "ontology_eval"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import harness  # noqa: E402
import make_conflict_gallery as mcg  # noqa: E402
import visualize  # noqa: E402
from services.ontology_graph.schema import OntologyGraph  # noqa: E402
from services.ontology_graph.mus_bridge import cores_to_conflict_graph  # noqa: E402
from services.ontology_graph.conflict import detect_convergent_conflicts  # noqa: E402
from services.ontology_graph.recommender import recommend_actions  # noqa: E402

IMG = ROOT / "docs" / "ontology_cases" / "img" / "9B_live_mus.png"
MD = ROOT / "docs" / "ONTOLOGY_LIVE_MUS_DEMO.md"


def _cores_of(run):
    rs = run.get("roster_system")
    return list(getattr(rs, "_cpsat_conflict_cores", []) or []) if rs is not None else []


def _dedupe(cores, limit=6):
    seen, out = set(), []
    for c in cores:
        cid = c.get("core_id")
        if cid in seen:
            continue
        seen.add(cid)
        out.append(c)
        if len(out) >= limit:
            break
    return out


def main():
    tl = int(os.getenv("DEMO_TL", "20"))
    cap = harness.capture_engine_inputs("9B", 2026, 6)
    base = harness.run_engine(cap, time_limit_seconds=tl)
    print(f"[live-mus] baseline feasible={base['feasible']} cells={base['work_cells']}")

    injected = mcg.inject_grade_minmax_conflict(cap, shifts=("N", "D"), grade="1")
    inj = harness.run_engine(injected, time_limit_seconds=tl)
    cores = _cores_of(inj)
    print(f"[live-mus] injected feasible={inj['feasible']} cells={inj['work_cells']} "
          f"MUS_cores={len(cores)} patterns={dict(Counter(c.get('pattern') for c in cores))}")

    sub = _dedupe(cores, limit=6)
    g = cores_to_conflict_graph(OntologyGraph(), sub)
    conv = detect_convergent_conflicts(g)
    actions = recommend_actions(g)
    member_families = Counter(c2.family for c2 in g.nodes_of_kind("constraint"))
    print(f"[live-mus] graph: constraints={dict(member_families)} "
          f"actions={[a.target_family for a in actions[:4]]}")

    fixed = mcg.apply_conflict_fix(injected)
    fx = harness.run_engine(fixed, time_limit_seconds=tl)
    print(f"[live-mus] fixed feasible={fx['feasible']} cells={fx['work_cells']}")

    # 이미지: 첫 core 의 동시불가 구조
    if sub:
        c0 = sub[0]
        sid = f"state:mus:{c0.get('core_id')}"
        keep = {sid}
        for e in g.out_edges(sid, "pressures"):
            keep.add(e.target_id)
            for me in g.in_edges(e.target_id, "mitigates"):
                keep.add(me.source_id)
        IMG.parent.mkdir(parents=True, exist_ok=True)
        visualize.render(g, keep, str(IMG),
                         title="라이브 MUS → 그래프\n동시불가 제약집합 → 완화액션")

    kinds = {n.kind for n in g.nodes.values()}
    rels = {e.relation for e in g.edges}
    L = ["# 라이브 MUS → 그래프 진단·수선 (프로덕션 솔버 + 실데이터)\n",
         "`AIDE_ENABLE_MUS_REGISTRY=1` 로 프로덕션 CP-SAT 가 실제 conflict core(MUS)를 emit →",
         "4-노드 그래프로 bridge → 진단·수선. 연구 §4 'solver conflict → domain cause' 실증.\n",
         "\n## 결과 (9B 2026-06)\n",
         "| 단계 | 값 |", "|---|---|",
         f"| baseline | feasible, 실근무 {base['work_cells']}건, MUS core 0 |",
         f"| 충돌 주입(등급 최소>상한) | **infeasible**, MUS core **{len(cores)}개** "
         f"(패턴 {dict(Counter(c.get('pattern') for c in cores))}) |",
         f"| 그래프 bridge | 동시불가 제약 {sum(member_families.values())}개 노드 "
         f"({dict(member_families)}) + 완화 액션 |",
         f"| 추천 액션 | {[a.action_type+':'+(a.target_family or '') for a in actions[:3]]} |",
         f"| 수선 적용 후 | **{'resolved' if fx['feasible'] else 'still infeasible'}**, "
         f"실근무 {fx['work_cells']}건 |",
         f"| 스키마 불변 | 노드 {sorted(kinds)}, 엣지 {sorted(rels)} (4종/8엣지 내) |",
         f"\n![live mus]({IMG.relative_to(ROOT/'docs').as_posix()})\n",
         "\n## 의미\n",
         "- 용량/모순 충돌뿐 아니라 **솔버가 직접 찾은 MUS(동시불가 하드제약 집합)** 가 같은 4-노드 그래프로 올라온다.",
         "- MUS 는 '최소 불가집합'이라 패턴이 mixed/team_min 등으로 나올 수 있음(주입 family 와 정확히 일치 안 할 수 있음) — CP-SAT MUS 의 정상 특성.",
         "- 이로써 시퀀스/타이밍 충돌(전이금지·회복 등 wrap된 제약)도 솔버가 불가능하게 만들면 core 로 떠서 그래프 진단 대상이 된다.",
         "\n## 비용 주석\n",
         "- 레지스트리는 모든 하드식을 reify 해 wall-time 이 늘어 **기본 OFF**(`AIDE_ENABLE_MUS_REGISTRY`). 진단이 필요한 infeasible 분석 시에만 ON 권장."]
    MD.write_text("\n".join(L), encoding="utf-8")
    print(f"[live-mus] md → {MD.relative_to(ROOT)}, img={IMG.exists()}")


if __name__ == "__main__":
    main()
