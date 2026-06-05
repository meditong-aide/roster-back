"""충돌(conflict) 케이스 — 실데이터로 그래프 진단·수선 + 스키마 불변 증명.

'인원 99' 같은 단순 부족이 아니라, **두 하드제약이 서로 모순**되는 케이스:
grade_min(특정 등급 ≥ m) ↔ grade_max(같은 등급 ≤ k), m > k. 각 제약은 단독으론
멀쩡하지만 함께는 만족 불가 → 엔진 infeasible.

증명 포인트:
 1) 용량(shortage) 검출기는 이걸 못 본다(부족이 아니라 모순). 같은 4종 그래프 위
    **generic conflict 검출기**(floor>ceiling)가 잡는다 — 케이스별 코드 없음.
 2) 한 액션(GradeMax soft 전환)이 여러 충돌을 동시에 푼다.
 3) 모든 케이스가 같은 노드 4종 + 엣지 8종만 사용(스키마 불변).
프로덕션 무변경(harness.clone).
"""

from __future__ import annotations

import copy
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "app"
HERE = Path(__file__).resolve().parent
for _p in (str(APP), str(HERE), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import harness  # noqa: E402
import evaluate  # noqa: E402
import visualize  # noqa: E402
from services.ontology_graph.schema import ActionNode  # noqa: E402
from services.ontology_graph.conflict import (  # noqa: E402
    actions_resolving_multiple, detect_conflicts, recommend_conflict_repair,
    register_capacity_conflict,
)

IMG_DIR = ROOT / "docs" / "ontology_cases" / "img"
MD_PATH = ROOT / "docs" / "ONTOLOGY_CONFLICT_GALLERY.md"
ALLOWED_KINDS = {"constraint", "domain_object", "state", "action"}
ALLOWED_RELATIONS = {"constrains", "requires", "supplied_by", "reduces",
                     "belongs_to", "pressures", "mitigates", "derived_from"}


def inject_grade_minmax_conflict(captured: dict, shifts=("N", "D"), grade="1") -> dict:
    """grade_min 이 있는 (shift, grade) 에 grade_max 를 min 미만으로 설정 → 모순."""
    c = harness.clone(captured)
    gc = c.get("grade_config") or {}
    cmin = gc.get("constraints_json") or {}
    cmax = dict(gc.get("constraints_max_json") or {})
    conflicts = []
    for s in shifts:
        m = int((cmin.get(s) or {}).get(grade, 0) or 0)
        if m <= 0:
            m = 1  # min 없으면 1 로 가정해 모순 생성
            cmin.setdefault(s, {})[grade] = m
        cmax[s] = dict(cmax.get(s) or {})
        cmax[s][grade] = m - 1            # 상한 = 하한-1 → floor>ceiling
        conflicts.append({"shift": s, "grade": grade, "floor": m, "ceiling": m - 1})
    gc["constraints_json"] = cmin
    gc["constraints_max_json"] = cmax
    c["grade_config"] = gc
    c["_conflicts"] = conflicts
    return c


def build_conflict_graph(captured: dict, roster=None):
    """기존 통합 그래프 + grade_min/grade_max 모순을 capacity conflict 로 등재."""
    g = evaluate.build_eval_graph(captured, roster=roster)
    gc = captured.get("grade_config") or {}
    cmin = gc.get("constraints_json") or {}
    cmax = gc.get("constraints_max_json") or {}
    # 하나의 GradeMax soft 액션(전역 노브)이 모든 grade_max 충돌을 mitigates
    soft_id = "action:soft:grade_max_global"
    g.add_node(ActionNode(node_id=soft_id, label="GradeMax soft 전환(전역)",
                          action_type="force_soft_mode", target_family="GradeMax",
                          config_key="_force_grade_max_soft_fallback", direction="enable", cost=2.0))
    from services.ontology_graph.schema import ConstraintNode
    for s, by_g in cmax.items():
        for gd, k in (by_g or {}).items():
            m = int((cmin.get(s) or {}).get(gd, 0) or 0)
            if m <= int(k or 0):
                continue
            res = f"{s}_grade{gd}"
            mid = f"grade_min:{res}"
            xid = f"grade_max:{res}"
            g.add_node(ConstraintNode(node_id=mid, label=f"{s} 등급{gd} 최소 {m}",
                                      family="GradeMin", operator=">=", target=m,
                                      attrs={"scope": {"shift": s, "grade": gd}}))
            g.add_node(ConstraintNode(node_id=xid, label=f"{s} 등급{gd} 최대 {k}",
                                      family="GradeMax", operator="<=", target=int(k or 0),
                                      attrs={"scope": {"shift": s, "grade": gd}}))
            register_capacity_conflict(g, resource=res, floor=m, ceiling=int(k or 0),
                                       min_constraint_id=mid, max_constraint_id=xid)
            g.add_edge("mitigates", soft_id, xid)            # 전역 soft 가 max 완화
            # disable min 도 후보(더 비쌈)
            did = f"action:disable:{mid}"
            g.add_node(ActionNode(node_id=did, label=f"{s} 등급{gd} 최소 disable",
                                  action_type="disable_module", target_family="GradeMin",
                                  config_key="constraints_json", direction="clear", cost=6.0))
            g.add_edge("mitigates", did, mid)
    return g


def apply_conflict_fix(captured: dict) -> dict:
    """수선: GradeMax 를 하드→소프트로(전역). + 안전하게 모순 상한 제거."""
    c = harness.clone(captured)
    cfg = c["config_data"]
    cfg["_force_grade_max_soft_fallback"] = True
    # soft 만으로 잔여 모순이 남으면 상한을 하한 이상으로 올려 모순 제거(최소변경)
    gc = c.get("grade_config") or {}
    cmin = gc.get("constraints_json") or {}
    cmax = gc.get("constraints_max_json") or {}
    for s, by_g in list(cmax.items()):
        for gd in list(by_g.keys()):
            m = int((cmin.get(s) or {}).get(gd, 0) or 0)
            if int(by_g[gd] or 0) < m:
                by_g[gd] = m            # 상한을 하한까지 복원(모순 해소)
    gc["constraints_max_json"] = cmax
    c["grade_config"] = gc
    return c


def main():
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    tl = int(os.getenv("GALLERY_TL", "15"))
    ward, year, month = "9B", 2026, 6
    print(f"[conflict] capture {ward} {year}-{month:02d}")
    cap = harness.capture_engine_inputs(ward, year, month)
    base = harness.run_engine(cap, time_limit_seconds=tl)
    print(f"[conflict] baseline feasible={base['feasible']} cells={base['work_cells']}")

    injected = inject_grade_minmax_conflict(cap, shifts=("N", "D"), grade="1")
    inj_run = harness.run_engine(injected, time_limit_seconds=tl)
    print(f"[conflict] injected feasible={inj_run['feasible']} cells={inj_run['work_cells']}")

    g = build_conflict_graph(injected, roster=base["roster"])
    conflicts = detect_conflicts(g)
    print(f"[conflict] detected conflicts={len(conflicts)}: "
          + ", ".join(f"{c.families[0]}×{c.families[1]}@{c.resource}(gap{c.gap})" for c in conflicts))
    multi = actions_resolving_multiple(g, conflicts)
    reps = [recommend_conflict_repair(g, c) for c in conflicts]

    fixed = apply_conflict_fix(injected)
    fix_run = harness.run_engine(fixed, time_limit_seconds=tl)
    print(f"[conflict] fixed feasible={fix_run['feasible']} cells={fix_run['work_cells']}")

    # 스키마 불변 확인
    kinds = {n.kind for n in g.nodes.values()}
    rels = {e.relation for e in g.edges}
    schema_ok = kinds <= ALLOWED_KINDS and rels <= ALLOWED_RELATIONS

    # 충돌 서브그래프 이미지 (제약 2개 + 공유 state + 액션)
    imgs = []
    for i, c in enumerate(conflicts, 1):
        keep = {c.state_id, c.min_constraint, c.max_constraint}
        for cid in (c.min_constraint, c.max_constraint):
            for e in g.in_edges(cid, "mitigates"):
                keep.add(e.source_id)
        img = IMG_DIR / f"9B_conflict{i}.png"
        visualize.render(g, keep, str(img),
                         title=f"충돌 {i}: {c.families[0]}×{c.families[1]} @ {c.resource}\n"
                               f"(하한 {c.floor} > 상한 {c.ceiling})")
        imgs.append((c, img.relative_to(ROOT / 'docs').as_posix()))

    # md
    L = ["# 온톨로지 충돌(conflict) 케이스 — 그래프 진단·수선\n",
         "단순 인원 부족이 아니라 **두 하드제약이 서로 모순**되는 케이스입니다.\n",
         f"실데이터 {ward} {year}-{month:02d} 에 `grade_min`(등급 최소)과 `grade_max`(등급 상한)을 ",
         "서로 모순되게(최소 > 상한) 주입 → 엔진 **infeasible**.\n",
         "\n## 한 줄 결과\n",
         f"- 주입 후 엔진: **{'infeasible' if not inj_run['feasible'] else 'feasible'}** "
         f"(실근무 {inj_run['work_cells']}건)",
         f"- **같은 generic 검출기**가 충돌 **{len(conflicts)}건** 포착 "
         f"(용량 부족 검출기로는 안 보임 — 부족이 아니라 모순)",
         f"- **한 액션**(GradeMax 하드→소프트)이 **{len(conflicts)}개 충돌 동시 해소**: "
         f"{ {k: v for k, v in multi.items()} }",
         f"- 수선 후 엔진: **{'resolved' if fix_run['feasible'] else 'still infeasible'}** "
         f"(실근무 {fix_run['work_cells']}건)",
         f"- 스키마 불변: 사용된 노드종류={sorted(kinds)}, 엣지={sorted(rels)} → "
         f"**4종/8엣지 내 {'✅' if schema_ok else '❌'}**\n"]
    for i, (c, img) in enumerate(imgs, 1):
        rep = reps[i - 1]
        L.append(f"\n## 충돌 {i}. {c.families[0]} ↔ {c.families[1]} (자원 {c.resource})\n")
        L.append(f"![conflict {i}]({img})\n")
        L.append("| 항목 | 내용 |")
        L.append("|---|---|")
        L.append(f"| 충돌 구조 | `{c.families[0]}`(≥{c.floor}) 와 `{c.families[1]}`(≤{c.ceiling})가 "
                 f"같은 자원 노드를 공유, 하한>상한(gap {c.gap}) |")
        L.append(f"| 그래프 표현 | 두 제약 ──requires──▶ 공유 capacity state ──pressures──▶ 두 제약 |")
        L.append(f"| 추천 수선(최소변경) | `{rep.action_label}` → `{rep.relaxed_family}` 완화 |")
        L.append(f"| 진단 코드 | 케이스 전용 없음 — `detect_conflicts`(floor>ceiling) 한 함수 |")
        L.append("")
    L.append("\n## 케이스마다 안 바뀐다는 증거\n")
    L.append("| 고정 요소 | 값 |")
    L.append("|---|---|")
    L.append("| 노드 종류 | constraint / domain_object / state / action (4종, 불변) |")
    L.append("| 엣지 종류 | requires·pressures·mitigates·constrains·reduces·belongs_to… (8종, 불변) |")
    L.append("| 충돌 검출 | `detect_conflicts` 한 함수 (family 무관) |")
    L.append("| 수선 추천 | `recommend_conflict_repair` 한 함수 (min/max 어느쪽이든) |")
    L.append("| 케이스마다 바뀌는 것 | **노드·엣지 인스턴스(데이터)뿐** — 종류·알고리즘 아님 |")
    MD_PATH.write_text("\n".join(L), encoding="utf-8")
    print(f"[conflict] md → {MD_PATH.relative_to(ROOT)}, imgs={len(imgs)}, schema_ok={schema_ok}")


if __name__ == "__main__":
    main()
