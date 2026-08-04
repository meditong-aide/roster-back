"""Shadow 로그 offline 분석 — production↔graph 를 input_hash 로 join, 3층 정확성 리포트.

피드백 point3·6: "일치율"만 보면 안 된다. 판정 정확성(**false certificate**=production FEASIBLE
인데 graph INFEASIBLE=치명), UNKNOWN 사유별 분포(재귀 hybrid 는 UNKNOWN_WIDTH 일 때만),
certificate 종류, short-circuit 자격 건수를 함께 본다. 기간이 아니라 **사례 수·분포**로 판단.

사용:
  AIDE_SHADOW_DIAGNOSIS=1 AIDE_SHADOW_LOG=/path/shadow.jsonl <운영 실행>
  python tools/infeasible_cases/shadow_analysis.py /path/shadow.jsonl
"""

from __future__ import annotations

import json
import sys
from collections import Counter


def parse_log(lines) -> list[dict]:
    out = []
    for ln in lines:
        i = ln.find("[Shadow] ")
        if i < 0:
            continue
        try:
            out.append(json.loads(ln[i + len("[Shadow] "):]))
        except Exception:
            pass
    return out


def join_by_input(records: list[dict]) -> dict:
    """input_hash → {"graph": rec|None, "production": rec|None}."""
    j: dict = {}
    for r in records:
        h = r.get("input_hash")
        if not h:
            continue
        slot = j.setdefault(h, {"graph": None, "production": None})
        if r.get("kind") == "production":
            slot["production"] = r
        elif "graph_status" in r:
            slot["graph"] = r
    return j


def analyze(records: list[dict]) -> dict:
    joined = join_by_input(records)
    from services.ontology_graph.short_circuit import ALLOWED_SHORT_CIRCUIT_CERTS
    a = {
        "cases": len(joined), "paired": 0,
        "graph_status": Counter(), "certificate": Counter(), "unknown_reason": Counter(),
        "false_certificate": 0, "false_certificate_hashes": [],
        "agree_infeasible": 0, "prod_infeasible": 0, "graph_infeasible": 0,
        "short_circuit_eligible": 0,
        "prod_status": Counter(),
    }
    for h, slot in joined.items():
        g, p = slot["graph"], slot["production"]
        if g:
            gs = g.get("graph_status", "?")
            a["graph_status"][gs] += 1
            if gs.startswith("UNKNOWN"):
                a["unknown_reason"][gs] += 1
            if g.get("certificate"):
                a["certificate"][g["certificate"]] += 1
            if gs == "INFEASIBLE_CERTIFIED":
                a["graph_infeasible"] += 1
                if g.get("certificate") in ALLOWED_SHORT_CIRCUIT_CERTS and not g.get("unmodeled"):
                    a["short_circuit_eligible"] += 1
        if p:
            a["prod_status"][p.get("production_status", "?")] += 1
            if p.get("production_status") == "INFEASIBLE":
                a["prod_infeasible"] += 1
        if g and p:
            a["paired"] += 1
            gs, ps = g.get("graph_status"), p.get("production_status")
            if gs == "INFEASIBLE_CERTIFIED":
                if ps == "INFEASIBLE":
                    a["agree_infeasible"] += 1
                elif ps == "FEASIBLE":                    # 치명: false certificate
                    a["false_certificate"] += 1
                    a["false_certificate_hashes"].append(h)
    return a


def report(a: dict) -> None:
    print(f"shadow 사례 {a['cases']} (graph+production 페어 {a['paired']})\n")
    print("판정 정확성:")
    print(f"  ★ false certificate(prod FEASIBLE인데 graph INFEASIBLE): {a['false_certificate']}  ← 반드시 0")
    if a["false_certificate_hashes"]:
        print(f"     반례 input_hash: {a['false_certificate_hashes'][:5]}")
    print(f"  graph INFEASIBLE ∧ prod INFEASIBLE 일치: {a['agree_infeasible']}/{a['graph_infeasible']}")
    print(f"\ngraph status 분포: {dict(a['graph_status'])}")
    print(f"UNKNOWN 사유(이분법 금지): {dict(a['unknown_reason'])}  ← 재귀 hybrid 는 UNKNOWN_WIDTH 일 때만")
    print(f"production status 분포: {dict(a['prod_status'])}")
    print(f"certificate 종류: {dict(a['certificate'])}")
    print(f"\nshort-circuit 자격(허용 cert·in-scope·INFEASIBLE): {a['short_circuit_eligible']}건")
    print(f"  → false certificate 0 이고 사례 수 충분할 때만 canary 활성 고려")


def main(path):
    with open(path) as f:
        recs = parse_log(f)
    report(analyze(recs))


if __name__ == "__main__":
    import os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "app"))
    if len(sys.argv) < 2:
        print("usage: shadow_analysis.py <shadow.jsonl>")
    else:
        main(sys.argv[1])
