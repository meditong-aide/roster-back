"""Live ICU 2026-07 config mutation tier+axis test battery.

Saves baseline config, then for each scenario:
  1) mutate config (tightening one or more knobs)
  2) save mutated config
  3) generate roster
  4) capture fix_plan tier+axis output
  5) restore baseline
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
GROUP_ID = "10135857f9f9"  # ICU
YEAR = 2026
MONTH = 7
OUT = "tools/harness/reports/tier_axis_icu_2026_07_mutate.json"


def _session(token: str) -> requests.Session:
    s = requests.Session()
    raw = token[len("Bearer "):] if token.startswith("Bearer ") else token
    s.cookies.set("access_token", raw)
    return s


def _get_config(s: requests.Session) -> Dict[str, Any]:
    r = s.get(BASE + "/roster/config/version/v1", params={"year": YEAR, "month": MONTH}, timeout=30)
    r.raise_for_status()
    return r.json()


def _save_config(s: requests.Session, cfg: Dict[str, Any]) -> int:
    # POST /roster/config/save expects RosterConfigCreate body
    payload = dict(cfg)
    # strip read-only / metadata
    for k in ("config_id", "created_at", "office_id", "group_id"):
        payload.pop(k, None)
    payload["year"] = YEAR
    payload["month"] = MONTH
    r = s.post(BASE + "/roster/config/save", json=payload, timeout=60)
    return r.status_code


def _generate(s: requests.Session) -> Dict[str, Any]:
    body = {"year": YEAR, "month": MONTH, "group_id": GROUP_ID, "strategy": "COMBINED"}
    try:
        r = s.post(BASE + "/roster_create/generate", json=body, timeout=300)
    except Exception as e:
        return {"status": -1, "error": str(e)}
    try:
        payload = r.json()
    except Exception:
        payload = {"raw": r.text[:500]}
    return {"status": r.status_code, "payload": payload}


def _extract_fix_plan(payload: Any) -> Dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    if payload.get("schedule_id"):
        return None
    candidates: List[Dict[str, Any]] = []
    if isinstance(payload.get("detail"), dict):
        candidates.append(payload["detail"])
    candidates.append(payload)
    for c in candidates:
        infe = c.get("infeasibility") if isinstance(c, dict) else None
        if isinstance(infe, dict) and isinstance(infe.get("fix_plan"), dict):
            return infe["fix_plan"]
        if isinstance(c.get("fix_plan"), dict):
            return c["fix_plan"]
    return None


def _summarize(fp: Dict[str, Any] | None) -> Dict[str, Any]:
    if not fp:
        return {"present": False}
    return {
        "present": True,
        "reason_source": fp.get("reason_source"),
        "no_assignment_breakdown": fp.get("no_assignment_breakdown") or [],
        "tier_summary": fp.get("tier_summary") or {},
        "data_correction_required": fp.get("data_correction_required"),
        "data_correction_families": fp.get("data_correction_families") or [],
        "protected_axes": [
            {"axis_id": x.get("axis_id"), "family": x.get("family"), "label_ko": x.get("label_ko")}
            for x in (fp.get("protected_axes") or [])
        ],
        "axis_actions": [
            {
                "priority": x.get("priority"),
                "axis_id": x.get("axis_id"),
                "family": x.get("family"),
                "tier": x.get("tier"),
                "lock_type": x.get("lock_type"),
                "relaxation_priority": x.get("relaxation_priority"),
                "human_message_ko": x.get("human_message_ko"),
                "n_targets": len(x.get("targets") or []),
            }
            for x in (fp.get("axis_actions") or [])
        ],
        "axis_actions_truncated": fp.get("axis_actions_truncated"),
        "legacy_actions": [x.get("action_id") for x in (fp.get("actions") or [])],
    }


def run_scenario(s, baseline: Dict[str, Any], name: str, mutate) -> Dict[str, Any]:
    mutated = copy.deepcopy(baseline)
    diff = mutate(mutated)
    save_code = _save_config(s, mutated)
    if save_code != 200:
        return {"case": name, "save_status": save_code, "diff": diff, "skipped": True}
    time.sleep(0.5)
    gen = _generate(s)
    fp = _extract_fix_plan(gen.get("payload"))
    return {
        "case": name,
        "diff": diff,
        "save_status": save_code,
        "generate_status": gen.get("status"),
        "fix_plan_summary": _summarize(fp),
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--token", required=True)
    args = p.parse_args()
    s = _session(args.token)

    if s.get(BASE + "/auth/me", timeout=30).status_code != 200:
        print("auth failed")
        return 1

    baseline = _get_config(s)
    original = copy.deepcopy(baseline)
    results: List[Dict[str, Any]] = []

    try:
        # ----------- baseline -----------
        gen = _generate(s)
        results.append({
            "case": "baseline",
            "diff": {},
            "generate_status": gen.get("status"),
            "fix_plan_summary": _summarize(_extract_fix_plan(gen.get("payload"))),
        })

        # ----------- A: tight monthly N cap -----------
        def a(cfg):
            old = cfg.get("max_nig_per_month")
            cfg["max_nig_per_month"] = 1
            return {"max_nig_per_month": {"old": old, "new": 1}}
        results.append(run_scenario(s, baseline, "A_max_nig_per_month_1", a))

        # ----------- B: off_days excess (T1 OffCap protected) -----------
        def b(cfg):
            old = cfg.get("off_days")
            cfg["off_days"] = 25.0
            return {"off_days": {"old": old, "new": 25.0}}
        results.append(run_scenario(s, baseline, "B_off_days_25", b))

        # ----------- C: req_exp_nurses raise (Grade min stress) -----------
        def c(cfg):
            old = cfg.get("req_exp_nurses")
            cfg["req_exp_nurses"] = 10
            return {"req_exp_nurses": {"old": old, "new": 10}}
        results.append(run_scenario(s, baseline, "C_req_exp_nurses_10", c))

        # ----------- D: min_exp_per_shift raise -----------
        def d(cfg):
            old = cfg.get("min_exp_per_shift")
            cfg["min_exp_per_shift"] = 10
            return {"min_exp_per_shift": {"old": old, "new": 10}}
        results.append(run_scenario(s, baseline, "D_min_exp_per_shift_10", d))

        # ----------- E: use_mid=true (likely triggers ConfigIntegrity T0 if MID not configured) -----------
        def e(cfg):
            old = cfg.get("use_mid")
            cfg["use_mid"] = True
            return {"use_mid": {"old": old, "new": True}}
        results.append(run_scenario(s, baseline, "E_use_mid_true", e))

        # ----------- F: combo monthly_n + req_exp (multi-axis stress) -----------
        def f(cfg):
            diff = {}
            old1 = cfg.get("max_nig_per_month")
            cfg["max_nig_per_month"] = 2
            diff["max_nig_per_month"] = {"old": old1, "new": 2}
            old2 = cfg.get("req_exp_nurses")
            cfg["req_exp_nurses"] = 8
            diff["req_exp_nurses"] = {"old": old2, "new": 8}
            return diff
        results.append(run_scenario(s, baseline, "F_combo_nig_grade", f))

        # ----------- G: severe: monthly_n=1 + off_days=25 + req_exp=10 -----------
        def g(cfg):
            diff = {}
            cfg["max_nig_per_month"] = 1
            diff["max_nig_per_month"] = 1
            cfg["off_days"] = 22.0
            diff["off_days"] = 22.0
            cfg["req_exp_nurses"] = 10
            diff["req_exp_nurses"] = 10
            return diff
        results.append(run_scenario(s, baseline, "G_severe_triple", g))

    finally:
        # restore baseline
        _save_config(s, original)

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"group_id": GROUP_ID, "year": YEAR, "month": MONTH, "results": results}, f, ensure_ascii=False, indent=2)

    print()
    print(f"Saved: {OUT}")
    print()
    print(f"{'case':30s} {'status':>6s}  {'tier':>14s}  axes")
    for r in results:
        st = r.get("generate_status")
        fp = r.get("fix_plan_summary") or {}
        tier = fp.get("tier_summary") or {}
        tier_str = f"T0={tier.get('T0',0)} T1={tier.get('T1',0)} T2={tier.get('T2',0)}"
        axes = [a.get("axis_id") for a in (fp.get("axis_actions") or [])]
        print(f"  {r['case']:30s} {st!s:>6s}  {tier_str:>14s}  {axes}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
