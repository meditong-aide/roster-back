"""Live API smoke for tier+axis fix_plan.

Hits /roster_create/generate for both ICU (2026-07) and 9B-known-fail
(2026-07) and reports the new tier_summary / axis_actions / protected_axes
fields. No config mutation — relies on natural baseline outcomes.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List

import requests

BASE = "http://127.0.0.1:8000"
OUT = "tools/harness/reports/tier_axis_live_smoke.json"

CASES = [
    {"name": "icu_baseline_2026_07", "group_id": "10135857f9f9", "year": 2026, "month": 7},
    {"name": "icu_baseline_2026_06", "group_id": "10135857f9f9", "year": 2026, "month": 6},
    {"name": "9b_2026_07", "group_id": "10135890c287", "year": 2026, "month": 7},
    {"name": "9b_2026_06", "group_id": "10135890c287", "year": 2026, "month": 6},
    {"name": "icu_2026_08", "group_id": "10135857f9f9", "year": 2026, "month": 8},
    {"name": "icu_2026_09", "group_id": "10135857f9f9", "year": 2026, "month": 9},
]


def _session(token: str) -> requests.Session:
    s = requests.Session()
    raw = token[len("Bearer "):] if token.startswith("Bearer ") else token
    s.cookies.set("access_token", raw)
    return s


def _extract_fix_plan(payload: Any) -> Dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    # success path has no fix_plan
    if payload.get("schedule_id"):
        return None
    # HTTPException structured payload may be at top-level or under 'detail'
    candidates: List[Dict[str, Any]] = []
    if isinstance(payload.get("detail"), dict):
        candidates.append(payload["detail"])
    candidates.append(payload)
    for c in candidates:
        infe = c.get("infeasibility") if isinstance(c, dict) else None
        if isinstance(infe, dict):
            fp = infe.get("fix_plan")
            if isinstance(fp, dict):
                return fp
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
            {"axis_id": x.get("axis_id"), "family": x.get("family"), "tier": x.get("tier"), "label_ko": x.get("label_ko")}
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
                "label_ko": x.get("label_ko"),
                "human_message_ko": x.get("human_message_ko"),
                "n_targets": len(x.get("targets") or []),
                "targets_preview": (x.get("targets") or [])[:2],
            }
            for x in (fp.get("axis_actions") or [])
        ],
        "axis_actions_truncated": fp.get("axis_actions_truncated"),
        "legacy_actions": [x.get("action_id") for x in (fp.get("actions") or [])],
    }


def run_case(s: requests.Session, case: Dict[str, Any]) -> Dict[str, Any]:
    body = {"year": case["year"], "month": case["month"], "group_id": case["group_id"], "strategy": "COMBINED"}
    try:
        r = s.post(BASE + "/roster_create/generate", json=body, timeout=300)
    except Exception as e:
        return {**case, "error": str(e)}
    try:
        payload = r.json()
    except Exception:
        payload = {"raw": r.text[:500]}
    fp = _extract_fix_plan(payload)
    return {
        **case,
        "status": r.status_code,
        "schedule_id": payload.get("schedule_id") if isinstance(payload, dict) else None,
        "fix_plan_summary": _summarize(fp),
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--token", required=True)
    args = p.parse_args()
    s = _session(args.token)
    auth = s.get(BASE + "/auth/me", timeout=30)
    if auth.status_code != 200:
        print(f"auth failed: {auth.status_code}")
        return 1

    results = []
    for case in CASES:
        print(f"... {case['name']}")
        results.append(run_case(s, case))

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"cases": results}, f, ensure_ascii=False, indent=2)
    print()
    print(f"Saved: {OUT}")
    print()
    for r in results:
        st = r.get("status", "ERR")
        fp = r.get("fix_plan_summary") or {}
        tier = fp.get("tier_summary") or {}
        axes = [a.get("axis_id") for a in (fp.get("axis_actions") or [])]
        prot = [p.get("axis_id") for p in (fp.get("protected_axes") or [])]
        print(f"  {r['name']:24s} status={st} fp={fp.get('present')} tier={tier} axes={axes} protected={prot}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
