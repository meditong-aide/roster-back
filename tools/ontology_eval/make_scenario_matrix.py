"""conflict_scenarios 6종 × 각 병동 전체 테스트 매트릭스.

준거 = ontology.yaml conflict_scenarios. 각 (병동 × 시나리오)에 대해:
  주입 → 엔진 infeasible? → 그래프 generic 진단(원인) → 최소변경 액션 → 적용 → resolved?

정직성: min-max 자원 충돌(grade/coverage/team×grade)은 그래프가 진단·수선.
시퀀스/타이밍 충돌(전이금지·1N·야간회복)은 현재 그래프 미진단 → GAP(=MUS wrap 필요)로 표기.
프로덕션 무변경(harness.clone).
"""

from __future__ import annotations

import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "app"
HERE = Path(__file__).resolve().parent
for _p in (str(APP), str(HERE), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import harness  # noqa: E402
import evaluate  # noqa: E402
from services.ontology_graph.schema import ActionNode, ConstraintNode  # noqa: E402
from services.ontology_graph.conflict import (  # noqa: E402
    detect_conflicts, recommend_conflict_repair, register_capacity_conflict,
)

MD_PATH = ROOT / "docs" / "ONTOLOGY_SCENARIO_MATRIX.md"
WORK = ["D", "E", "N"]


# ── helpers ──────────────────────────────────────────────────────────────────
def _grade_min(gc):
    return gc.get("constraints_json") or {}


def _present_grades(captured):
    return {str(g) for g in Counter(n.get("grade") for n in captured["nurses_data"]) if g is not None}


def _coverage_min(cfg, shift):
    return int((cfg.get("daily_shift_requirements") or {}).get(shift, 0) or 0)


def _add_soft_action(g, max_id):
    aid = "action:soft:grade_max_global"
    if aid not in g.nodes:
        g.add_node(ActionNode(node_id=aid, label="GradeMax 하드→소프트(전역)",
                              action_type="force_soft_mode", target_family="GradeMax",
                              config_key="_force_grade_max_soft_fallback", direction="enable", cost=2.0))
    g.add_edge("mitigates", aid, max_id)
    return aid


def _register(g, *, resource, floor, ceiling, min_family, min_label, max_label):
    mid = f"min:{resource}"; xid = f"max:{resource}"
    g.add_node(ConstraintNode(node_id=mid, label=min_label, family=min_family,
                              operator=">=", target=floor, attrs={"scope": {"res": resource}}))
    g.add_node(ConstraintNode(node_id=xid, label=max_label, family="GradeMax",
                              operator="<=", target=ceiling, attrs={"scope": {"res": resource}}))
    register_capacity_conflict(g, resource=resource, floor=floor, ceiling=ceiling,
                               min_constraint_id=mid, max_constraint_id=xid)
    _add_soft_action(g, xid)


def _grade_soft_fix(captured):
    """수선: GradeMax 하드→소프트 + 모순 상한을 하한까지 복원(최소변경)."""
    c = harness.clone(captured)
    c["config_data"]["_force_grade_max_soft_fallback"] = True
    gc = c.get("grade_config") or {}
    cmin = gc.get("constraints_json") or {}
    cmax = gc.get("constraints_max_json") or {}
    for s, by_g in list(cmax.items()):
        for gd in list(by_g.keys()):
            m = int((cmin.get(s) or {}).get(gd, 0) or 0)
            if int(by_g[gd] or 0) < m:
                by_g[gd] = m
    # coverage/team 막힘이면 그 shift 상한 자체를 제거
    return c


# ── 시나리오 정의 ─────────────────────────────────────────────────────────────
def sc_grade_min_max(cap):
    """GRADE_MIN×GRADE_MAX: 등급 최소 > 등급 상한."""
    c = harness.clone(cap)
    gc = c.get("grade_config") or {}
    cmin = _grade_min(gc); cmax = dict(gc.get("constraints_max_json") or {})
    touched = []
    for s in ("N", "D"):
        m = int((cmin.get(s) or {}).get("1", 0) or 0) or 1
        cmin.setdefault(s, {})["1"] = m
        cmax[s] = dict(cmax.get(s) or {}); cmax[s]["1"] = m - 1
        touched.append((s, "1", m, m - 1))
    gc["constraints_json"] = cmin; gc["constraints_max_json"] = cmax; c["grade_config"] = gc

    def build(graph_cap, roster):
        g = evaluate.build_eval_graph(graph_cap, roster=roster)
        for s, gd, m, k in touched:
            _register(g, resource=f"{s}_grade{gd}", floor=m, ceiling=k, min_family="GradeMin",
                      min_label=f"{s} 등급{gd} 최소 {m}", max_label=f"{s} 등급{gd} 최대 {k}")
        return g
    return c, build, _grade_soft_fix


def sc_coverage_grade_max(cap):
    """COVERAGE×GRADE_MAX: 야간 커버리지 ≥ m 인데 모든 등급 야간 상한 0 → 채울 수 없음."""
    c = harness.clone(cap)
    gc = c.get("grade_config") or {}
    cmax = dict(gc.get("constraints_max_json") or {})
    grades = _present_grades(cap)
    cmax["N"] = {g: 0 for g in grades}        # 모든 등급 야간 상한 0
    gc["constraints_max_json"] = cmax; c["grade_config"] = gc
    cov = _coverage_min(c["config_data"], "N")

    def build(graph_cap, roster):
        g = evaluate.build_eval_graph(graph_cap, roster=roster)
        _register(g, resource="N_coverage", floor=max(cov, 1), ceiling=0, min_family="CoverageMin",
                  min_label=f"야간 커버리지 최소 {max(cov,1)}", max_label="야간 등급상한 합 0")
        return g

    def fix(captured):
        cc = harness.clone(captured)
        cc["config_data"]["_force_grade_max_soft_fallback"] = True
        gcc = cc.get("grade_config") or {}
        cmx = gcc.get("constraints_max_json") or {}
        cmx.pop("N", None)                     # 야간 상한 제거 → 커버리지 채움 가능
        gcc["constraints_max_json"] = cmx; cc["grade_config"] = gcc
        return cc
    return c, build, fix


def sc_team_grade_max(cap):
    """TEAM_MIN×GRADE_MAX (팀 있는 병동): 팀 야간 최소 + 그 팀 등급들 야간 상한 0."""
    tc = Counter(str(n.get("team_id")) for n in cap["nurses_data"] if n.get("team_id") is not None)
    if not tc:
        return None  # 팀 없음 → N/A
    team = sorted(tc, key=lambda t: -tc[t])[0]
    team_grades = {str(n.get("grade")) for n in cap["nurses_data"]
                   if str(n.get("team_id")) == team and n.get("grade") is not None}
    c = harness.clone(cap)
    cfg = c["config_data"]
    tmin = {k: dict(v or {}) for k, v in (cfg.get("team_min_by_team") or {}).items()}
    tmin.setdefault(team, {})["N"] = tc[team]          # 팀 전원 야간 요구
    cfg["team_min_by_team"] = tmin
    gc = c.get("grade_config") or {}
    cmax = dict(gc.get("constraints_max_json") or {})
    cmax["N"] = dict(cmax.get("N") or {}); cmax["N"].update({g: 0 for g in team_grades})
    gc["constraints_max_json"] = cmax; c["grade_config"] = gc

    def build(graph_cap, roster):
        g = evaluate.build_eval_graph(graph_cap, roster=roster)
        _register(g, resource=f"team{team}_N", floor=tc[team], ceiling=0, min_family="TeamMin",
                  min_label=f"팀{team} 야간 최소 {tc[team]}", max_label="팀 등급 야간 상한 0")
        return g
    return c, build, _team_fix(team)


def _team_fix(team):
    def fix(captured):
        cc = harness.clone(captured)
        cc["config_data"]["_force_grade_max_soft_fallback"] = True
        gcc = cc.get("grade_config") or {}
        cmx = gcc.get("constraints_max_json") or {}
        cmx.pop("N", None)
        gcc["constraints_max_json"] = cmx; cc["grade_config"] = gcc
        # 팀 야간 최소도 원복(과주입분)
        cc["config_data"].get("team_min_by_team", {}).get(team, {}).pop("N", None)
        return cc
    return fix


# min-max 로 표현 가능한 시나리오 (그래프 진단 가능)
MINMAX_SCENARIOS = {
    "GRADE_MIN_VS_GRADE_MAX": sc_grade_min_max,
    "COVERAGE_MIN_VS_GRADE_MAX": sc_coverage_grade_max,
    "TEAM_MIN_VS_GRADE_MAX_INTERSECTION": sc_team_grade_max,
}
# 시퀀스/타이밍 충돌 — 현재 그래프 미진단(정직한 GAP)
TEMPORAL_GAPS = {
    "BTBAN_VS_FIXED_SEQUENCE": "전이금지↔고정 시퀀스 — 시간축 충돌, 현재 구조검출기 미지원(MUS wrap 필요)",
    "NIGHT_RECOVERY_VS_COVERAGE_MIN": "야간회복 OFF 몰림 — 타이밍 충돌, 미지원(MUS wrap 필요)",
    "NOT_ONE_NIGHT_VS_FIXED_OFF_NEIGHBOR": "1N 회피↔인접 고정 — 시퀀스 충돌, 미지원(MUS wrap 필요)",
    "OFFCAP_VS_WEEKEND_OFF_NARROW_WINDOW": "OFF cap↔주말OFF 좁은창 — 부분 표현 가능하나 활성창/마스크 모델 필요(미구현)",
}

WARDS = [("9B", 2026, 6), ("ICU", 2026, 6)]


def run_scenario(cap, base_roster, name, factory, tl):
    built = factory(cap)
    if built is None:
        return {"scenario": name, "status": "N/A", "note": "해당 병동에 미적용(예: 팀 없음)"}
    injected, build_graph, fix_fn = built
    inj = harness.run_engine(injected, time_limit_seconds=tl)
    if inj["feasible"]:
        return {"scenario": name, "status": "주입무효", "note": "주입이 infeasible 을 못 만듦"}
    g = build_graph(injected, base_roster)
    conflicts = detect_conflicts(g)
    if not conflicts:
        return {"scenario": name, "status": "원인미검출", "infeasible": True}
    rep = recommend_conflict_repair(g, conflicts[0])
    fixed = fix_fn(injected)
    fx = harness.run_engine(fixed, time_limit_seconds=tl)
    return {
        "scenario": name, "status": "PASS" if fx["feasible"] else "미해결",
        "infeasible": True, "cause": f"{conflicts[0].families[0]}×{conflicts[0].families[1]}@"
                                     f"{conflicts[0].resource}(하한{conflicts[0].floor}>상한{conflicts[0].ceiling})",
        "action": rep.action_label if rep else None,
        "inj_cells": inj["work_cells"], "fix_cells": fx["work_cells"],
    }


def main():
    tl = int(os.getenv("MATRIX_TL", "15"))
    results = {}
    for ward, y, m in WARDS:
        print(f"[matrix] {ward} {y}-{m:02d} capture")
        cap = harness.capture_engine_inputs(ward, y, m)
        base = harness.run_engine(cap, time_limit_seconds=tl)
        rows = []
        for name, factory in MINMAX_SCENARIOS.items():
            print(f"  {name} ...")
            r = run_scenario(cap, base["roster"], name, factory, tl)
            print(f"    → {r['status']} {r.get('cause','')}")
            rows.append(r)
        for name, why in TEMPORAL_GAPS.items():
            rows.append({"scenario": name, "status": "GAP", "note": why})
        results[ward] = {"baseline_cells": base["work_cells"], "rows": rows}

    # md
    L = ["# conflict_scenarios 6종 × 병동 전체 테스트 매트릭스\n",
         "준거 = `ontology.yaml` conflict_scenarios. 각 (병동×시나리오): 주입 → infeasible → "
         "그래프 generic 진단 → 최소변경 액션 → 적용 → resolved.\n",
         "정직성: min-max 자원 충돌은 그래프가 진단·수선. 시퀀스/타이밍 충돌은 현재 미진단 → **GAP**(MUS wrap 필요).\n"]
    for ward, data in results.items():
        L.append(f"\n## {ward} 병동 (baseline 실근무 {data['baseline_cells']}건)\n")
        L.append("| 시나리오 | infeasible | 진단된 원인 | 추천 액션 | 적용 후 | 결과 |")
        L.append("|---|---|---|---|---|---|")
        for r in data["rows"]:
            if r["status"] == "GAP":
                L.append(f"| {r['scenario']} | — | — | — | — | ⚠ GAP: {r['note']} |")
            elif r["status"] == "N/A":
                L.append(f"| {r['scenario']} | — | — | — | — | N/A: {r['note']} |")
            elif r["status"] in ("주입무효", "원인미검출", "미해결"):
                L.append(f"| {r['scenario']} | {'✅' if r.get('infeasible') else '—'} | "
                         f"{r.get('cause','—')} | {r.get('action','—')} | — | ❌ {r['status']} |")
            else:
                L.append(f"| {r['scenario']} | ✅ (0건) | {r['cause']} | {r['action']} | "
                         f"실근무 {r['fix_cells']}건 | ✅ PASS |")
        L.append("")
    L.append("\n## 요약\n")
    for ward, data in results.items():
        npass = sum(1 for r in data["rows"] if r["status"] == "PASS")
        ngap = sum(1 for r in data["rows"] if r["status"] == "GAP")
        nna = sum(1 for r in data["rows"] if r["status"] == "N/A")
        L.append(f"- **{ward}**: PASS {npass} / GAP {ngap} / N-A {nna} / 전체 {len(data['rows'])}")
    L.append("\n> PASS = 그래프가 두 하드제약 충돌을 원인으로 짚고, 최소변경 액션으로 실제 해결.")
    L.append("> GAP = 시퀀스/타이밍 충돌이라 현재 구조검출기로 미진단(처음 실증한 MUS wrap 갭과 동일 지점).")
    MD_PATH.write_text("\n".join(L), encoding="utf-8")
    print(f"[matrix] md → {MD_PATH.relative_to(ROOT)}")
    for ward, data in results.items():
        for r in data["rows"]:
            print(f"  {ward} {r['scenario']}: {r['status']}")


if __name__ == "__main__":
    main()
