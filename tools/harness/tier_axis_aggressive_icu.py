"""More aggressive ICU mutations to surface diverse tier+axis fix_plan emissions.

ICU has large slack so prior mild mutations passed. Push harder.
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
OUT = "tools/harness/reports/tier_axis_icu_aggressive.json"


def _session(token: str) -> requests.Session:
    s = requests.Session()
    raw = token[len("Bearer "):] if token.startswith("Bearer ") else token
    s.cookies.set("access_token", raw)
    return s


def _get_config(s):
    r = s.get(BASE + "/roster/config/version/v1", params={"year": YEAR, "month": MONTH}, timeout=30)
    r.raise_for_status()
    return r.json()


def _save_config(s, cfg):
    payload = dict(cfg)
    for k in ("config_id", "created_at", "office_id", "group_id"):
        payload.pop(k, None)
    payload["year"] = YEAR
    payload["month"] = MONTH
    r = s.post(BASE + "/roster/config/save", json=payload, timeout=60)
    return r.status_code


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


def _summary(fp):
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
                "relaxation_priority": x.get("relaxation_priority"),
                "human_message_ko": x.get("human_message_ko"),
                "n_targets": len(x.get("targets") or []),
            }
            for x in (fp.get("axis_actions") or [])
        ],
        "legacy_actions": [x.get("action_id") for x in (fp.get("actions") or [])],
    }


def run(s, baseline, name, mut):
    cfg = copy.deepcopy(baseline)
    diff = mut(cfg)
    save = _save_config(s, cfg)
    if save != 200:
        return {"case": name, "save_status": save, "diff": diff, "skipped": True}
    time.sleep(0.4)
    g = _generate(s)
    return {
        "case": name,
        "diff": diff,
        "save_status": save,
        "generate_status": g.get("status"),
        "fix_plan_summary": _summary(_extract_fp(g.get("payload"))),
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--token", required=True)
    args = p.parse_args()
    s = _session(args.token)
    if s.get(BASE + "/auth/me", timeout=30).status_code != 200:
        print("auth failed")
        return 1

    baseline = _get_config(s)
    original = copy.deepcopy(baseline)
    results = []

    try:
        # H: req_exp_nurses=99 (definitely impossible)
        def h(c):
            c["req_exp_nurses"] = 99
            return {"req_exp_nurses": 99}
        results.append(run(s, baseline, "H_req_exp_99", h))

        # I: min_exp_per_shift=99
        def i(c):
            c["min_exp_per_shift"] = 99
            return {"min_exp_per_shift": 99}
        results.append(run(s, baseline, "I_min_exp_99", i))

        # J: monthly_n=1 + off_days=20 + req_exp=15 (triple stress)
        def j(c):
            c["max_nig_per_month"] = 1
            c["off_days"] = 20.0
            c["req_exp_nurses"] = 15
            return {"max_nig_per_month": 1, "off_days": 20, "req_exp_nurses": 15}
        results.append(run(s, baseline, "J_triple_aggressive", j))

        # K: max_nig=0 disable + use_mid (extra stress)
        def k(c):
            c["max_nig_per_month"] = 1
            c["use_mid"] = True
            return {"max_nig_per_month": 1, "use_mid": True}
        results.append(run(s, baseline, "K_use_mid_plus_n_cap", k))

        # L: use_mid + req_exp=99 (T0 + multi T2)
        def l(c):
            c["use_mid"] = True
            c["req_exp_nurses"] = 99
            c["min_exp_per_shift"] = 50
            return {"use_mid": True, "req_exp_nurses": 99, "min_exp_per_shift": 50}
        results.append(run(s, baseline, "L_mid_plus_extreme_exp", l))

        # M: off_days=29 + req_exp_nurses=12 + use_mid=True (max stress, all axes)
        def m(c):
            c["off_days"] = 29.0
            c["req_exp_nurses"] = 12
            c["use_mid"] = True
            c["max_nig_per_month"] = 1
            return {"off_days": 29, "req_exp_nurses": 12, "use_mid": True, "max_nig_per_month": 1}
        results.append(run(s, baseline, "M_all_axes_stress", m))

    finally:
        _save_config(s, original)

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"results": results}, f, ensure_ascii=False, indent=2)

    print(f"\nSaved: {OUT}\n")
    print(f"{'case':30s} {'status':>6s}  {'tier':<22s}  {'data_corr':<10s}  axes")
    for r in results:
        st = r.get("generate_status")
        fp = r.get("fix_plan_summary") or {}
        tier = fp.get("tier_summary") or {}
        tier_str = f"T0={tier.get('T0',0)} T1={tier.get('T1',0)} T2={tier.get('T2',0)}"
        dc = fp.get("data_correction_required")
        axes = [a.get("axis_id") for a in (fp.get("axis_actions") or [])]
        prot = [p.get("axis_id") for p in (fp.get("protected_axes") or [])]
        print(f"  {r['case']:30s} {st!s:>6s}  {tier_str:<22s}  {str(dc):<10s}  axes={axes} prot={prot}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
