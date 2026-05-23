"""Aggressive multi-table DB stress for ICU 2026-07 → tier/axis node behavior.

Mutates roster_config + grade_config simultaneously across scenarios, runs
harness for each, captures fix_plan + /ontology/graph node tier distribution.

Restores baseline on exit (always).
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import time
from typing import Any, Dict, List

import requests

BASE = "http://127.0.0.1:8000"
GROUP_ID = "10135857f9f9"
YEAR = 2026
MONTH = 7
OUT = "tools/harness/reports/db_stress_icu_tier_nodes.json"


def _session(token: str) -> requests.Session:
    s = requests.Session()
    raw = token[len("Bearer "):] if token.startswith("Bearer ") else token
    s.cookies.set("access_token", raw)
    return s


def _get_roster_cfg(s):
    return s.get(BASE + "/roster/config/version/v1", params={"year": YEAR, "month": MONTH}, timeout=30).json()


def _save_roster_cfg(s, cfg):
    p = dict(cfg)
    for k in ("config_id", "created_at", "office_id", "group_id"):
        p.pop(k, None)
    p["year"] = YEAR
    p["month"] = MONTH
    return s.post(BASE + "/roster/config/save", json=p, timeout=60).status_code


def _get_grade(s):
    return s.get(BASE + "/grade/config", timeout=30).json()


def _save_grade(s, body):
    return s.post(BASE + "/grade/config", json=body, timeout=60).status_code


def _grade_payload_from(g: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "null_grade_policy": g.get("null_grade_policy") or "LOWEST",
        "use_dynamic_scaling": bool(g.get("use_dynamic_scaling", True)),
        "allow_soft_fallback": bool(g.get("allow_soft_fallback", False)),
        "constraints": g.get("constraints") or {},
        "constraints_max": g.get("constraints_max") or {},
        "grade_names": g.get("grade_names"),
        "use_mid": bool(g.get("use_mid", False)),
        "default_shifts": g.get("default_shifts") or [],
    }


def _generate(s):
    body = {"year": YEAR, "month": MONTH, "group_id": GROUP_ID, "strategy": "COMBINED"}
    try:
        r = s.post(BASE + "/roster_create/generate", json=body, timeout=300)
    except Exception as e:
        return {"status": -1, "error": str(e)}
    try:
        return {"status": r.status_code, "payload": r.json()}
    except Exception:
        return {"status": r.status_code, "payload": {"raw": r.text[:500]}}


def _extract_fp(payload):
    if not isinstance(payload, dict):
        return None
    if payload.get("schedule_id"):
        return None
    for c in [payload.get("detail"), payload]:
        if isinstance(c, dict):
            infe = c.get("infeasibility")
            if isinstance(infe, dict) and isinstance(infe.get("fix_plan"), dict):
                return infe["fix_plan"]
            if isinstance(c.get("fix_plan"), dict):
                return c["fix_plan"]
    return None


def _summarize_fp(fp):
    if not fp:
        return {"present": False}
    return {
        "present": True,
        "reason_source": fp.get("reason_source"),
        "tier_summary": fp.get("tier_summary"),
        "failure_stage": fp.get("failure_stage"),
        "failure_stage_label_ko": fp.get("failure_stage_label_ko"),
        "data_correction_required": fp.get("data_correction_required"),
        "data_correction_families": fp.get("data_correction_families"),
        "protected_axes": [(p.get("axis_id"), p.get("family"), p.get("tier")) for p in (fp.get("protected_axes") or [])],
        "axis_actions": [
            {
                "priority": a.get("priority"),
                "axis_id": a.get("axis_id"),
                "family": a.get("family"),
                "tier": a.get("tier"),
                "relaxation_priority": a.get("relaxation_priority"),
                "human_message_ko": a.get("human_message_ko"),
                "targets_n": len(a.get("targets") or []),
                "targets_preview": (a.get("targets") or [])[:3],
            }
            for a in (fp.get("axis_actions") or [])
        ],
        "no_assignment_breakdown": fp.get("no_assignment_breakdown") or [],
    }


def _node_distribution(s):
    """/ontology/graph 호출해 tier 분포 + 노드타입별 카운트 리턴."""
    try:
        r = s.get(BASE + "/ontology/graph?level=full", timeout=60)
    except Exception as e:
        return {"error": str(e)}
    if r.status_code != 200:
        return {"http_status": r.status_code}
    g = r.json()
    by_tier = {"high": 0, "med": 0, "low": 0, None: 0}
    by_type: Dict[str, Dict[str, int]] = {}
    visible = 0
    for n in g.get("nodes", []):
        tier = n.get("ui_tier")
        by_tier[tier] = by_tier.get(tier, 0) + 1
        if n.get("ui_visible"):
            visible += 1
        t = str(n.get("type") or "")
        d = by_type.setdefault(t, {"total": 0, "ui_visible": 0})
        d["total"] += 1
        if n.get("ui_visible"):
            d["ui_visible"] += 1
    return {
        "stats": g.get("stats"),
        "by_tier": by_tier,
        "visible_total": visible,
        "by_type_top10": dict(sorted(by_type.items(), key=lambda kv: -kv[1]["total"])[:10]),
    }


def run_scenario(s, name, mutate_roster, mutate_grade, baseline_roster, baseline_grade):
    print(f"... running {name}")
    rcfg = copy.deepcopy(baseline_roster)
    gcfg = copy.deepcopy(baseline_grade)
    diff = {}
    if mutate_roster:
        d = mutate_roster(rcfg)
        diff.update({"roster": d})
        _save_roster_cfg(s, rcfg)
    if mutate_grade:
        d = mutate_grade(gcfg)
        diff.update({"grade": d})
        _save_grade(s, _grade_payload_from(gcfg))
    time.sleep(0.6)
    gen = _generate(s)
    fp_summary = _summarize_fp(_extract_fp(gen.get("payload")))
    node_dist = _node_distribution(s)
    return {
        "scenario": name,
        "diff": diff,
        "generate_status": gen.get("status"),
        "fix_plan_summary": fp_summary,
        "node_distribution": node_dist,
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--token", required=True)
    args = p.parse_args()
    s = _session(args.token)
    if s.get(BASE + "/auth/me", timeout=30).status_code != 200:
        print("auth failed")
        return 1

    baseline_roster = _get_roster_cfg(s)
    baseline_grade = _get_grade(s)
    results: List[Dict[str, Any]] = []

    try:
        # 0: baseline (for reference)
        gen = _generate(s)
        results.append({
            "scenario": "00_baseline",
            "diff": {},
            "generate_status": gen.get("status"),
            "fix_plan_summary": _summarize_fp(_extract_fp(gen.get("payload"))),
            "node_distribution": _node_distribution(s),
        })

        # 1: aggressive roster_config across many fields
        def mr1(c):
            d = {}
            for k, v in {
                "max_nig_per_month": 1,
                "off_days": 22.0,
                "req_exp_nurses": 99,
                "min_exp_per_shift": 99,
                "use_mid": True,
                "preceptor_gauge": 0.9,
                "patient_amount": 99.0,
                "team_balance_enable": True,
                "team_balance_gauge": 10,
                "off_swap_enabled": True,
                "off_first": True,
                "fixed_wanted_use_yn": True,
                "sequential_offs": True,
                "three_seq_nig": True,
                "two_offs_after_three_nig": True,
                "two_offs_after_two_nig": True,
                "banned_day_after_eve": True,
                "weekend_shift_ratio": 1.0,
                "even_nights": True,
                "not_one_night": True,
            }.items():
                d[k] = {"old": c.get(k), "new": v}
                c[k] = v
            return d
        results.append(run_scenario(s, "01_roster_extreme_all_fields", mr1, None, baseline_roster, baseline_grade))

        # 2: grade min impossible (huge)
        def mg2(g):
            old = copy.deepcopy(g.get("constraints") or {})
            g["constraints"] = {sh: {"1": 10, "2": 10, "3": 10, "4": 10} for sh in ("D", "E", "N")}
            return {"constraints_old": old, "constraints_new": g["constraints"]}
        results.append(run_scenario(s, "02_grade_min_huge", None, mg2, baseline_roster, baseline_grade))

        # 3: grade max zero (forbid)
        def mg3(g):
            old = copy.deepcopy(g.get("constraints_max") or {})
            g["constraints_max"] = {sh: {"1": 0, "2": 0, "3": 0, "4": 0} for sh in ("D", "E", "N")}
            return {"constraints_max_old": old, "constraints_max_new": g["constraints_max"]}
        results.append(run_scenario(s, "03_grade_max_zero", None, mg3, baseline_roster, baseline_grade))

        # 4: combined — grade huge + roster_config aggressive (multi-axis blow up)
        def mr4(c):
            for k, v in {
                "max_nig_per_month": 1,
                "off_days": 15.0,
                "use_mid": True,
            }.items():
                c[k] = v
            return {"keys": ["max_nig_per_month=1", "off_days=15", "use_mid=True"]}
        def mg4(g):
            g["constraints"] = {sh: {"1": 5, "2": 5, "3": 5, "4": 5} for sh in ("D", "E", "N")}
            g["constraints_max"] = {sh: {"1": 3, "2": 3, "3": 3, "4": 3} for sh in ("D", "E", "N")}
            return {"grade_min=5_max=3": True}
        results.append(run_scenario(s, "04_combined_roster_grade", mr4, mg4, baseline_roster, baseline_grade))

        # 5: extreme — totally absurd config to provoke ConfigIntegrity (T0)
        def mr5(c):
            c["use_mid"] = True
            c["max_nig_per_month"] = 1
            c["off_days"] = 30.0
            c["preceptor_gauge"] = 1.0
            c["req_exp_nurses"] = 100
            c["min_exp_per_shift"] = 100
            return {"absurd": True}
        def mg5(g):
            g["constraints"] = {sh: {"1": 99, "2": 99, "3": 99, "4": 99} for sh in ("D", "E", "N")}
            g["constraints_max"] = {sh: {"1": 0, "2": 0, "3": 0, "4": 0} for sh in ("D", "E", "N")}
            g["use_mid"] = True
            return {"grade_extreme": True}
        results.append(run_scenario(s, "05_absurd_kitchen_sink", mr5, mg5, baseline_roster, baseline_grade))

    finally:
        # restore
        _save_roster_cfg(s, baseline_roster)
        _save_grade(s, _grade_payload_from(baseline_grade))
        print("[restored baseline roster + grade]")

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"results": results}, f, ensure_ascii=False, indent=2)

    print(f"\nSaved: {OUT}\n")
    print(f"{'scenario':40s} {'status':>6s}  {'tier':<22s}  {'stage':<22s}  axes")
    for r in results:
        st = r.get("generate_status")
        fp = r.get("fix_plan_summary") or {}
        tier = fp.get("tier_summary") or {}
        tier_str = f"T0={tier.get('T0',0)} T1={tier.get('T1',0)} T2={tier.get('T2',0)}"
        stage = fp.get("failure_stage") or "-"
        axes = [a.get("axis_id") for a in (fp.get("axis_actions") or [])]
        print(f"  {r['scenario']:40s} {st!s:>6s}  {tier_str:<22s}  {stage:<22s}  axes={axes}")
        nd = r.get("node_distribution") or {}
        if "by_tier" in nd:
            tt = nd["by_tier"]
            print(f"    └─ graph: total={nd['stats']['node_count']} visible={nd['visible_total']} (high={tt.get('high',0)}, med={tt.get('med',0)}, low={tt.get('low',0)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
