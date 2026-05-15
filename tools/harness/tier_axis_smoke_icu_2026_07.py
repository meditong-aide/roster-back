"""Live API smoke test for tier+axis fix_plan against ICU group 2026-07.

Runs a battery of generation requests with config modifications that push the
problem into different infeasibility regimes, then verifies the new
`tier_summary` / `protected_axes` / `axis_actions` fields are emitted.

Output: tools/harness/reports/tier_axis_icu_2026_07.json
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import time
from typing import Any, Dict, List, Tuple

import requests


BASE = "http://127.0.0.1:8000"
GROUP_ID = "10135857f9f9"  # ICU
YEAR = 2026
MONTH = 7
OUT = "tools/harness/reports/tier_axis_icu_2026_07.json"


def _session(token: str) -> requests.Session:
    s = requests.Session()
    raw = token[len("Bearer "):] if token.startswith("Bearer ") else token
    s.cookies.set("access_token", raw)
    return s


def _get(s: requests.Session, path: str, params: Dict[str, Any] | None = None) -> Tuple[int, Any]:
    r = s.get(BASE + path, params=params, timeout=60)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"raw": r.text[:300]}


def _post(s: requests.Session, path: str, body: Dict[str, Any]) -> Tuple[int, Any]:
    r = s.post(BASE + path, json=body, timeout=240)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"raw": r.text[:300]}


def _put(s: requests.Session, path: str, body: Dict[str, Any]) -> Tuple[int, Any]:
    r = s.put(BASE + path, json=body, timeout=120)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"raw": r.text[:300]}


def _extract_fix_plan(payload: Any) -> Dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    infe = payload.get("infeasibility")
    if isinstance(infe, dict):
        fp = infe.get("fix_plan")
        if isinstance(fp, dict):
            return fp
    # fallback for blocking/unrecoverable variants
    return payload.get("fix_plan") if isinstance(payload.get("fix_plan"), dict) else None


def _summarize_fix_plan(fp: Dict[str, Any] | None) -> Dict[str, Any]:
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
            {"axis_id": x.get("axis_id"), "family": x.get("family"), "tier": x.get("tier"), "label_ko": x.get("label_ko")}
            for x in (fp.get("protected_axes") or [])
        ],
        "axis_actions": [
            {
                "axis_id": x.get("axis_id"),
                "family": x.get("family"),
                "tier": x.get("tier"),
                "lock_type": x.get("lock_type"),
                "relaxation_priority": x.get("relaxation_priority"),
                "label_ko": x.get("label_ko"),
                "human_message_ko": x.get("human_message_ko"),
                "n_targets": len(x.get("targets") or []),
                "targets_preview": (x.get("targets") or [])[:2],
            }
            for x in (fp.get("axis_actions") or [])
        ],
        "axis_actions_cap": fp.get("axis_actions_cap"),
        "axis_actions_truncated": fp.get("axis_actions_truncated"),
        "legacy_actions": [x.get("action_id") for x in (fp.get("actions") or [])],
    }


def _generate(s: requests.Session) -> Dict[str, Any]:
    code, body = _post(
        s,
        "/roster_create/generate",
        {"group_id": GROUP_ID, "year": YEAR, "month": MONTH, "strategy": "COMBINED"},
    )
    return {
        "status": code,
        "schedule_id": body.get("schedule_id") if isinstance(body, dict) else None,
        "fix_plan_summary": _summarize_fix_plan(_extract_fix_plan(body)),
    }


def _get_config(s: requests.Session) -> Dict[str, Any]:
    code, body = _get(s, "/roster_create/config", {"group_id": GROUP_ID, "year": YEAR, "month": MONTH})
    return body if isinstance(body, dict) else {}


def _save_config(s: requests.Session, cfg: Dict[str, Any]) -> int:
    code, _ = _put(s, "/roster_create/config", {
        "group_id": GROUP_ID, "year": YEAR, "month": MONTH, "config": cfg,
    })
    return code


def run_case(s: requests.Session, baseline_cfg: Dict[str, Any], name: str, mutate) -> Dict[str, Any]:
    """Apply mutate(cfg) -> mutated cfg, save, generate, restore."""
    mutated = copy.deepcopy(baseline_cfg)
    detail = mutate(mutated)
    save_code = _save_config(s, mutated)
    if save_code != 200:
        return {"case": name, "save_status": save_code, "mutation": detail, "skipped": True}
    time.sleep(0.5)
    gen = _generate(s)
    return {
        "case": name,
        "mutation": detail,
        "save_status": save_code,
        "generate_status": gen["status"],
        "fix_plan_summary": gen["fix_plan_summary"],
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--token", required=True)
    args = p.parse_args()
    s = _session(args.token)

    auth_code, auth_body = _get(s, "/auth/me")
    if auth_code != 200:
        print(f"auth failed: {auth_code}")
        return 1

    baseline_cfg = _get_config(s)
    if not baseline_cfg:
        print("baseline config fetch failed")
        return 1
    # save once to ensure we have a clean restore path
    original = copy.deepcopy(baseline_cfg)

    results: List[Dict[str, Any]] = []

    try:
        # Case 0: baseline
        gen = _generate(s)
        results.append({"case": "baseline", "generate_status": gen["status"], "fix_plan_summary": gen["fix_plan_summary"]})

        # Case 1: raise team_min beyond available
        def raise_team_min(cfg):
            tm = cfg.setdefault("team_min_by_team", {})
            mutated = {}
            for k in list(tm.keys()):
                old = tm[k]
                if isinstance(old, dict):
                    new = {sh: max(int(old.get(sh, 0)) + 3, int(old.get(sh, 0))) for sh in ("D", "E", "N")}
                    tm[k] = new
                    mutated[k] = {"old": old, "new": new}
            return {"team_min_raise": mutated}
        results.append(run_case(s, baseline_cfg, "team_min_raise_high", raise_team_min))

        # Case 2: raise daily N requirement
        def raise_daily_n(cfg):
            dsr = cfg.setdefault("daily_shift_requirements", {})
            old_n = dsr.get("N")
            if isinstance(old_n, list):
                new_n = [int(v) + 3 for v in old_n]
                dsr["N"] = new_n
                return {"daily_N": {"old_len": len(old_n), "delta": +3}}
            return {"daily_N": "missing"}
        results.append(run_case(s, baseline_cfg, "daily_N_raise_high", raise_daily_n))

        # Case 3: raise daily D + N together (multi-axis force)
        def raise_daily_dn(cfg):
            dsr = cfg.setdefault("daily_shift_requirements", {})
            applied = {}
            for sh in ("D", "N"):
                old = dsr.get(sh)
                if isinstance(old, list):
                    dsr[sh] = [int(v) + 2 for v in old]
                    applied[sh] = "+2"
            return {"daily": applied}
        results.append(run_case(s, baseline_cfg, "daily_DN_raise", raise_daily_dn))

        # Case 4: drop grade_max to 0 (impossible) → tier T2 grade_max axis
        def crush_grade_max(cfg):
            gm = cfg.setdefault("grade_max", {})
            old = dict(gm)
            for g in list(gm.keys()):
                if isinstance(gm[g], dict):
                    gm[g] = {sh: 0 for sh in gm[g].keys()}
            return {"grade_max_zero": old}
        results.append(run_case(s, baseline_cfg, "grade_max_zero", crush_grade_max))

        # Case 5: lower monthly N cap to 0 (force infeasible coverage)
        def crush_monthly_n_cap(cfg):
            rc = cfg.setdefault("roster_config", {})
            old = rc.get("max_nig_per_month")
            rc["max_nig_per_month"] = 1
            return {"max_nig_per_month": {"old": old, "new": 1}}
        results.append(run_case(s, baseline_cfg, "monthly_n_cap_1", crush_monthly_n_cap))

        # Case 6: combined multi-axis stress (team + grade + daily)
        def combo(cfg):
            applied = {}
            tm = cfg.setdefault("team_min_by_team", {})
            for k in list(tm.keys()):
                if isinstance(tm[k], dict):
                    tm[k] = {sh: int(tm[k].get(sh, 0)) + 1 for sh in tm[k]}
            applied["team_min"] = "+1 each"
            gm = cfg.setdefault("grade_min", {})
            for g in list(gm.keys()):
                if isinstance(gm[g], dict):
                    gm[g] = {sh: int(gm[g].get(sh, 0)) + 1 for sh in gm[g]}
            applied["grade_min"] = "+1 each"
            dsr = cfg.setdefault("daily_shift_requirements", {})
            for sh in ("D", "E", "N"):
                old = dsr.get(sh)
                if isinstance(old, list):
                    dsr[sh] = [int(v) + 1 for v in old]
            applied["daily"] = "+1 each D/E/N"
            return applied
        results.append(run_case(s, baseline_cfg, "combo_team_grade_daily", combo))

    finally:
        _save_config(s, original)

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({
            "group_id": GROUP_ID, "year": YEAR, "month": MONTH,
            "results": results,
        }, f, ensure_ascii=False, indent=2)

    print(f"Saved: {OUT}")
    for r in results:
        fp = r.get("fix_plan_summary") or {}
        status = r.get("generate_status")
        print(f"  case={r['case']:32s} status={status} tier_summary={fp.get('tier_summary')} axes={[a.get('axis_id') for a in (fp.get('axis_actions') or [])]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
