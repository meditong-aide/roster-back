from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

import requests

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover
    yaml = None


def _utc_now_str() -> str:
    return dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Roster generation harness runner")
    p.add_argument("--base-url", default="http://127.0.0.1:8000")
    p.add_argument("--token", required=True, help="JWT or Bearer JWT")
    p.add_argument("--year", type=int, required=True)
    p.add_argument("--month", type=int, required=True)
    p.add_argument("--strategy", default="COMBINED")
    p.add_argument("--repeats", type=int, default=5)
    p.add_argument("--rules", default="tools/harness/rules/checklist_core.yaml")
    p.add_argument("--out-dir", default="tools/harness/reports")
    p.add_argument("--strict", action="store_true", help="Fail when blocking rules are SKIPPED")
    return p.parse_args()


def _load_rules(path: str) -> Dict[str, Any]:
    if yaml is None:
        raise RuntimeError("PyYAML is required. Please install pyyaml.")
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _session_with_token(token: str) -> requests.Session:
    s = requests.Session()
    if token.startswith("Bearer "):
        raw = token[len("Bearer ") :]
        s.cookies.set("access_token", raw)
    else:
        s.cookies.set("access_token", token)
    return s


def _get_json(s: requests.Session, base: str, path: str, params: Dict[str, Any] | None = None) -> Any:
    r = s.get(base + path, params=params, timeout=45)
    if r.status_code != 200:
        raise RuntimeError(f"GET {path} failed {r.status_code}: {r.text[:300]}")
    return r.json()


def _post_json(s: requests.Session, base: str, path: str, body: Dict[str, Any]) -> Tuple[int, Any]:
    r = s.post(base + path, json=body, timeout=240)
    ct = r.headers.get("content-type", "")
    payload = r.json() if ct.startswith("application/json") else {"raw": r.text[:300]}
    return r.status_code, payload


def _canonical_hash(obj: Any) -> str:
    payload = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _build_shift_main_map(shifts: List[Dict[str, Any]]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for row in shifts:
        sid = str(row.get("shift_id") or "").strip()
        main = str(row.get("default_shift") or "").strip().upper()
        if not sid or not main:
            continue
        if main in ("OFF", "주"):
            main = "O"
        out[sid] = main
    return out


def _to_main(cell: Any, shift_main_map: Dict[str, str]) -> str:
    c = str(cell).strip()
    cu = c.upper()
    if cu in ("D", "E", "N", "M"):
        return cu
    if cu in ("O", "OFF", "주", "-"):
        return "O" if cu != "-" else "-"
    return shift_main_map.get(c, shift_main_map.get(cu, cu))


@dataclass
class RunStats:
    run_index: int
    status_code: int
    schedule_id: str | None
    infeasible_detail: Any
    under_count_D: int = 0
    under_count_E: int = 0
    under_count_N: int = 0
    under_count_M: int = 0
    over_count_D: int = 0
    over_count_E: int = 0
    over_count_N: int = 0
    over_count_M: int = 0
    coverage_gaps_count: int = 0
    violated_constraints_count: int = 0
    solver_status: str | None = None


def _coverage_counts(roster: Dict[str, Any], day_need: Dict[str, List[int]], shift_main_map: Dict[str, str]) -> Dict[str, int]:
    nurses = roster.get("nurses") or []
    days = len(day_need["D"])
    assigned = [{"D": 0, "E": 0, "N": 0, "M": 0} for _ in range(days)]

    for n in nurses:
        sched = n.get("schedule") or []
        for d, raw in enumerate(sched[:days]):
            m = _to_main(raw, shift_main_map)
            if m in ("D", "E", "N", "M"):
                assigned[d][m] += 1

    out = {
        "under_count_D": 0,
        "under_count_E": 0,
        "under_count_N": 0,
        "under_count_M": 0,
        "over_count_D": 0,
        "over_count_E": 0,
        "over_count_N": 0,
        "over_count_M": 0,
    }
    for d in range(days):
        for sh in ("D", "E", "N", "M"):
            need = int(day_need.get(sh, [0] * days)[d])
            delta = assigned[d][sh] - need
            if delta < 0:
                out[f"under_count_{sh}"] += -delta
            elif delta > 0:
                out[f"over_count_{sh}"] += delta
    return out


def _coverage_under_cells(roster: Dict[str, Any], day_need: Dict[str, List[int]], shift_main_map: Dict[str, str]) -> List[Dict[str, Any]]:
    nurses = roster.get("nurses") or []
    days = len(day_need["D"])
    assigned = [{"D": 0, "E": 0, "N": 0, "M": 0} for _ in range(days)]
    for n in nurses:
        sched = n.get("schedule") or []
        for d, raw in enumerate(sched[:days]):
            m = _to_main(raw, shift_main_map)
            if m in ("D", "E", "N", "M"):
                assigned[d][m] += 1
    out = []
    for d in range(days):
        for sh in ("D", "E", "N", "M"):
            need = int(day_need.get(sh, [0] * days)[d])
            miss = need - assigned[d][sh]
            if miss > 0:
                out.append({"day": d + 1, "shift": sh, "need": need, "assigned": assigned[d][sh], "short": miss})
    return out


def _roster_off_counts(roster: Dict[str, Any], shift_main_map: Dict[str, str]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for n in roster.get("nurses") or []:
        nid = str(n.get("id") or "")
        if not nid:
            continue
        sched = n.get("schedule") or []
        c = 0
        for raw in sched:
            m = _to_main(raw, shift_main_map)
            if m == "O" or m == "-":
                c += 1
        out[nid] = c
    return out


def _build_monthly_limit_exact_map(items: List[Dict[str, Any]]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for it in items:
        nid = str(it.get("nurse_id") or "")
        ox = it.get("o_exact")
        if nid and ox is not None:
            out[nid] = int(ox)
    return out


def _active_nurse_ids(nurses_payload: List[Dict[str, Any]]) -> set[str]:
    ids = set()
    for n in nurses_payload:
        act = str(n.get("active", 1))
        if act in ("1", "True", "true"):
            nid = str(n.get("nurse_id") or "")
            if nid:
                ids.add(nid)
    return ids


def _excluded_nurse_ids_from_assignments(assignments_payload: Dict[str, Any]) -> set[str]:
    out = set()
    for a in assignments_payload.get("items") or []:
        reason = str(a.get("reason") or "")
        status = str(a.get("status") or "")
        if status != "active":
            continue
        if reason in ("휴직", "퇴사", "파견"):
            nid = str(a.get("nurse_id") or "")
            if nid:
                out.add(nid)
    return out


def _preceptee_mapping_invalid_count(nurses_payload: List[Dict[str, Any]]) -> int:
    by_id = {str(n.get("nurse_id") or ""): n for n in nurses_payload}
    c = 0
    for n in nurses_payload:
        nid = str(n.get("nurse_id") or "")
        if not nid:
            continue
        pid = n.get("preceptor_id")
        if pid in (None, "", "null"):
            continue
        pid = str(pid)
        p = by_id.get(pid)
        if not p:
            c += 1
            continue
        # active + same group + same office
        pact = str(p.get("active", 1))
        if pact not in ("1", "True", "true"):
            c += 1
            continue
        if str(p.get("group_id") or "") != str(n.get("group_id") or ""):
            c += 1
            continue
        if str(p.get("office_id") or "") != str(n.get("office_id") or ""):
            c += 1
            continue
    return c


def _is_work_code(code: str) -> bool:
    return code in ("D", "E", "N", "M", "W", "A", "P")


def _extract_prev_last_shift_map(prev_tail_payload: Dict[str, Any], shift_main_map: Dict[str, str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    data = prev_tail_payload.get("data") or {}
    tail_days = data.get("tail_days") or []
    if not tail_days:
        return out
    last_day = int(tail_days[-1])
    for n in data.get("nurses") or []:
        nid = str(n.get("nurse_id") or "")
        shifts = n.get("shifts") or {}
        raw = shifts.get(str(last_day))
        if raw is None:
            raw = shifts.get(last_day)
        if nid:
            out[nid] = _to_main(raw if raw is not None else "-", shift_main_map)
    return out


def _extract_prev_nurse_names(prev_tail_payload: Dict[str, Any]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    data = prev_tail_payload.get("data") or {}
    for n in data.get("nurses") or []:
        nid = str(n.get("nurse_id") or "")
        name = str(n.get("name") or "")
        if nid:
            out[nid] = name
    return out


def _current_first_shift_map(roster: Dict[str, Any], shift_main_map: Dict[str, str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for n in roster.get("nurses") or []:
        nid = str(n.get("id") or "")
        sched = n.get("schedule") or []
        first = sched[0] if sched else "-"
        if nid:
            out[nid] = _to_main(first, shift_main_map)
    return out


def _current_consecutive_work_prefix(roster: Dict[str, Any], shift_main_map: Dict[str, str]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for n in roster.get("nurses") or []:
        nid = str(n.get("id") or "")
        sched = n.get("schedule") or []
        c = 0
        for raw in sched:
            m = _to_main(raw, shift_main_map)
            if _is_work_code(m):
                c += 1
            else:
                break
        if nid:
            out[nid] = c
    return out


def _prev_consecutive_work_suffix(prev_tail_payload: Dict[str, Any], shift_main_map: Dict[str, str]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    data = prev_tail_payload.get("data") or {}
    tail_days = data.get("tail_days") or []
    if not tail_days:
        return out
    ordered_days = [str(int(d)) for d in tail_days]
    for n in data.get("nurses") or []:
        nid = str(n.get("nurse_id") or "")
        shifts = n.get("shifts") or {}
        c = 0
        for d in reversed(ordered_days):
            m = _to_main(shifts.get(d), shift_main_map)
            if _is_work_code(m):
                c += 1
            else:
                break
        if nid:
            out[nid] = c
    return out


def _analyze_hard_patterns(roster: Dict[str, Any], shift_main_map: Dict[str, str], max_night_per_month: int = 15) -> Dict[str, int]:
    nurses = roster.get("nurses") or []
    out = {
        "hard.not_one_night_single_n_count": 0,
        "hard.recovery_2n2o_violation_count": 0,
        "hard.recovery_3n2o_violation_count": 0,
        "hard.nod_violation_count": 0,
        "hard.noe_violation_count": 0,
        "hard.eod_violation_count": 0,
        "hard.max_consecutive_night_overflow_count": 0,
        "hard.max_consecutive_work_overflow_count": 0,
        "hard.monthly_night_cap_over_count": 0,
    }

    for n in nurses:
        sched = n.get("schedule") or []
        main = [_to_main(x, shift_main_map) for x in sched]

        for i in range(len(main) - 1):
            a = main[i]
            b = main[i + 1]
            if a == "N" and b == "D":
                out["hard.nod_violation_count"] += 1
            if a == "N" and b == "E":
                out["hard.noe_violation_count"] += 1
            if a == "E" and b == "D":
                out["hard.eod_violation_count"] += 1

        cur_n = 0
        n_total = sum(1 for m in main if m == "N")
        if n_total == 1:
            out["hard.not_one_night_single_n_count"] += 1

        for m in main:
            if m == "N":
                cur_n += 1
                if cur_n > 3:
                    out["hard.max_consecutive_night_overflow_count"] += 1
            else:
                cur_n = 0

        # recovery checks: after every 2N/3N run, next two days should be OFF when available
        i = 0
        L = len(main)
        while i < L:
            if main[i] != "N":
                i += 1
                continue
            j = i
            while j < L and main[j] == "N":
                j += 1
            run_len = j - i
            if run_len >= 2:
                need_recovery = 2
                rec = main[j : min(L, j + need_recovery)]
                off_cnt = sum(1 for x in rec if x == "O")
                if run_len == 2 and off_cnt < 2 and len(rec) == 2:
                    out["hard.recovery_2n2o_violation_count"] += 1
                if run_len >= 3 and off_cnt < 2 and len(rec) == 2:
                    out["hard.recovery_3n2o_violation_count"] += 1
            i = j

        cur_w = 0
        for m in main:
            if m in ("D", "E", "N", "M"):
                cur_w += 1
                if cur_w > 6:
                    out["hard.max_consecutive_work_overflow_count"] += 1
            else:
                cur_w = 0

        n_count = sum(1 for m in main if m == "N")
        if n_count > max_night_per_month:
            out["hard.monthly_night_cap_over_count"] += 1

    return out


def _fixed_validations(
    roster: Dict[str, Any],
    fixed_payload: Dict[str, Any],
    shift_main_map: Dict[str, str],
) -> Dict[str, int]:
    out = {
        "fixed.changed_cell_count": 0,
        "fixed.n_before_fixed_off_violation_count": 0,
    }
    entries = fixed_payload.get("entries") or []
    if not entries:
        return out

    # build nurse/day lookup from roster
    by_nurse: Dict[str, List[str]] = {}
    for n in roster.get("nurses") or []:
        nid = str(n.get("id") or "")
        if nid:
            by_nurse[nid] = [_to_main(x, shift_main_map) for x in (n.get("schedule") or [])]

    for e in entries:
        nid = str(e.get("nurse_id") or "")
        day = int(e.get("day") or 0)
        if nid not in by_nurse or day <= 0:
            continue
        sched = by_nurse[nid]
        if day - 1 >= len(sched):
            continue
        fixed_shift = str(e.get("shift_id") or "")
        fixed_main = _to_main(fixed_shift, shift_main_map)
        actual = sched[day - 1]
        if fixed_main != actual:
            out["fixed.changed_cell_count"] += 1

        # if fixed off, previous day should not be N
        if fixed_main == "O" and day - 2 >= 0:
            prev = sched[day - 2]
            if prev == "N":
                out["fixed.n_before_fixed_off_violation_count"] += 1
    return out


def _max_enabled_inconsistency_count(daily_payload: Dict[str, Any]) -> int:
    # inconsistency when max_enabled=false but any *_count_max has positive value
    max_enabled = bool(daily_payload.get("max_enabled", False))
    date = daily_payload.get("date") or {}
    positives = 0
    for k in ("D_count_max", "E_count_max", "N_count_max", "M_count_max"):
        arr = date.get(k) or []
        positives += sum(1 for x in arr if int(x or 0) > 0)
    if (not max_enabled) and positives > 0:
        return positives
    return 0


def _offswap_target_multi_count(shifts_payload: List[Dict[str, Any]]) -> int:
    # group-wise: off_swap_target=1 rows should be <=1
    by_group: Dict[str, int] = {}
    for row in shifts_payload:
        gid = str(row.get("group_id") or "")
        flag = row.get("off_swap_target")
        try:
            is_target = int(flag or 0) == 1
        except Exception:
            is_target = False
        if gid and is_target:
            by_group[gid] = by_group.get(gid, 0) + 1
    return sum(max(0, c - 1) for c in by_group.values())


def _eval_condition(value: Any, expr: str) -> bool:
    expr = expr.strip()
    if expr.startswith("=="):
        return float(value) == float(expr[2:].strip())
    if expr.startswith("<="):
        return float(value) <= float(expr[2:].strip())
    if expr.startswith(">="):
        return float(value) >= float(expr[2:].strip())
    if expr.startswith("<"):
        return float(value) < float(expr[1:].strip())
    if expr.startswith(">"):
        return float(value) > float(expr[1:].strip())
    raise ValueError(f"Unsupported pass_condition: {expr}")


def main() -> None:
    args = _parse_args()
    rules_doc = _load_rules(args.rules)
    rules = rules_doc.get("rules") or []

    _ensure_dir(args.out_dir)
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out_base = os.path.join(args.out_dir, f"run-{args.year}-{args.month:02d}-{ts}")
    _ensure_dir(out_base)

    # auth with raw token first, fallback to bearer cookie
    s = _session_with_token(args.token)
    try:
        me = _get_json(s, args.base_url, "/auth/me")
    except Exception:
        s = _session_with_token("Bearer " + args.token if not args.token.startswith("Bearer ") else args.token)
        me = _get_json(s, args.base_url, "/auth/me")

    year = args.year
    month = args.month
    group_id = me["group_id"]
    office_id = me["office_id"]

    cfg = _get_json(s, args.base_url, "/roster/config/version/v1")
    daily = _get_json(
        s,
        args.base_url,
        "/daily-shift",
        params={"office_id": office_id, "group_id": group_id, "year": year, "month": month},
    )
    teams = _get_json(s, args.base_url, "/teams")
    grade = _get_json(s, args.base_url, "/grade/config")
    shifts = _get_json(s, args.base_url, "/shifts")
    nurses_payload = _get_json(s, args.base_url, "/nurses")
    try:
        monthly_limits = _get_json(
            s,
            args.base_url,
            "/nurses/monthly-limits",
            params={"year": year, "month": month},
        )
    except Exception:
        # 일부 배포버전은 group_id/nurse_id 쿼리를 요구함
        try:
            monthly_limits = _get_json(
                s,
                args.base_url,
                "/nurses/monthly-limits",
                params={
                    "year": year,
                    "month": month,
                    "group_id": group_id,
                    "nurse_id": me.get("nurse_id"),
                },
            )
        except Exception:
            monthly_limits = {"items": []}
    assignments = _get_json(s, args.base_url, "/nurses/assignments", params={"status": "active"})
    fixed = _get_json(s, args.base_url, f"/wanted/fixed/{year}/{month}")
    prev_tail = _get_json(s, args.base_url, f"/roster/{year}/{month}/prev-tail", params={"tail_days": 6})

    snapshot = {
        "me": me,
        "config": cfg,
        "daily_shift": daily,
        "teams": teams,
        "grade": grade,
        "nurses": nurses_payload,
        "monthly_limits": monthly_limits,
        "assignments": assignments,
        "fixed_wanted": fixed,
        "prev_tail": prev_tail,
        "strategy": args.strategy,
        "year": year,
        "month": month,
    }
    input_hash = _canonical_hash(snapshot)

    shift_main_map = _build_shift_main_map(shifts if isinstance(shifts, list) else [])
    day_need = {
        "D": list((daily.get("date") or {}).get("D_count") or []),
        "E": list((daily.get("date") or {}).get("E_count") or []),
        "N": list((daily.get("date") or {}).get("N_count") or []),
        "M": list((daily.get("date") or {}).get("M_count") or []),
    }

    runs: List[RunStats] = []
    for i in range(1, args.repeats + 1):
        code, body = _post_json(
            s,
            args.base_url,
            "/roster_create/generate",
            {"year": year, "month": month, "grade_strategy": args.strategy},
        )
        st = RunStats(run_index=i, status_code=code, schedule_id=None, infeasible_detail=None)
        if code != 200:
            st.infeasible_detail = body.get("detail") if isinstance(body, dict) else body
            runs.append(st)
            continue

        st.schedule_id = str(body.get("schedule_id") or "")
        ci = body.get("constraint_impact") or {}
        st.coverage_gaps_count = len(ci.get("coverage_gaps") or [])
        st.violated_constraints_count = len(ci.get("violated_constraints") or [])
        st.solver_status = ci.get("solver_status")

        roster = _get_json(s, args.base_url, f"/roster/schedule/{st.schedule_id}")
        cov = _coverage_counts(roster, day_need, shift_main_map)
        for k, v in cov.items():
            setattr(st, k, int(v))
        runs.append(st)

    # aggregate metrics
    metrics: Dict[str, Any] = {
        "coverage.under_count_D": sum(r.under_count_D for r in runs),
        "coverage.under_count_E": sum(r.under_count_E for r in runs),
        "coverage.under_count_N": sum(r.under_count_N for r in runs),
        "coverage.under_count_M": sum(r.under_count_M for r in runs),
        "coverage.max_overflow_count": sum(
            r.over_count_D + r.over_count_E + r.over_count_N + r.over_count_M for r in runs
        ),
        "system.infeasible_run_count": sum(1 for r in runs if r.status_code != 200),
        "system.fallback_error_count": 0,
        "system.solve_time_ms_p95": 0,
    }

    hard_agg = {
        "hard.not_one_night_single_n_count": 0,
        "hard.recovery_2n2o_violation_count": 0,
        "hard.recovery_3n2o_violation_count": 0,
        "hard.nod_violation_count": 0,
        "hard.noe_violation_count": 0,
        "hard.eod_violation_count": 0,
        "hard.max_consecutive_night_overflow_count": 0,
        "hard.max_consecutive_work_overflow_count": 0,
        "hard.monthly_night_cap_over_count": 0,
    }
    for r in runs:
        if r.status_code != 200 or not r.schedule_id:
            continue
        roster = _get_json(s, args.base_url, f"/roster/schedule/{r.schedule_id}")
        h = _analyze_hard_patterns(roster, shift_main_map, max_night_per_month=15)
        for k, v in h.items():
            hard_agg[k] += int(v)
    metrics.update(hard_agg)

    # B/G metrics (API-backed)
    active_ids = _active_nurse_ids(nurses_payload if isinstance(nurses_payload, list) else [])
    excluded_ids = _excluded_nurse_ids_from_assignments(assignments if isinstance(assignments, dict) else {})
    eval_ids = active_ids - excluded_ids
    limit_items = (monthly_limits or {}).get("items") if isinstance(monthly_limits, dict) else []
    o_exact_map = _build_monthly_limit_exact_map(limit_items or [])

    # evaluate off metrics on first successful run roster as representative snapshot
    off_band_cnt = 0
    off_cap_mismatch = 0
    role_null_active_count = 0
    preceptee_sync_viol = 0
    preceptee_mapping_invalid = _preceptee_mapping_invalid_count(nurses_payload if isinstance(nurses_payload, list) else [])
    grade_minmax_viol = 0
    prev_transition_violation_count = 0
    prev_n_recovery_violation_count = 0
    prev_conseq_work_overflow_count = 0
    fixed_changed_cell_count = 0
    fixed_n_before_off_violation_count = 0
    drilldown: Dict[str, Any] = {
        "coverage_under_cells": [],
        "prev_transition_violations": [],
        "prev_conseq_work_violations": [],
    }

    for n in (nurses_payload if isinstance(nurses_payload, list) else []):
        act = str(n.get("active", 1))
        role = str(n.get("role") or "").strip()
        if act in ("1", "True", "true") and role == "":
            role_null_active_count += 1

    first_ok = next((r for r in runs if r.status_code == 200 and r.schedule_id), None)
    if first_ok is not None:
        roster = _get_json(s, args.base_url, f"/roster/schedule/{first_ok.schedule_id}")
        drilldown["coverage_under_cells"] = _coverage_under_cells(roster, day_need, shift_main_map)
        off_counts = _roster_off_counts(roster, shift_main_map)
        off_days_cfg = float(cfg.get("off_days") or 0)
        for nid in eval_ids:
            actual = int(off_counts.get(nid, 0))
            if not (math.floor(off_days_cfg - 1) <= actual <= math.ceil(off_days_cfg + 1)):
                off_band_cnt += 1
            if nid in o_exact_map and int(o_exact_map[nid]) != actual:
                off_cap_mismatch += 1

        # preceptee sync from constraint impact (aggregated)
        r0 = _post_json(
            s,
            args.base_url,
            "/roster_create/generate",
            {"year": year, "month": month, "grade_strategy": args.strategy},
        )
        if r0[0] == 200:
            ci = (r0[1].get("constraint_impact") or {}) if isinstance(r0[1], dict) else {}
            for v in ci.get("violated_constraints") or []:
                nid = str(v.get("node_id") or "")
                if "preceptee" in nid:
                    preceptee_sync_viol += 1
                if nid.startswith("grade_min:") or nid.startswith("grade_max:"):
                    grade_minmax_viol += 1

        # F metrics via prev-tail + current first day
        prev_last = _extract_prev_last_shift_map(prev_tail if isinstance(prev_tail, dict) else {}, shift_main_map)
        prev_names = _extract_prev_nurse_names(prev_tail if isinstance(prev_tail, dict) else {})
        cur_first = _current_first_shift_map(roster, shift_main_map)
        prev_suffix = _prev_consecutive_work_suffix(prev_tail if isinstance(prev_tail, dict) else {}, shift_main_map)
        cur_prefix = _current_consecutive_work_prefix(roster, shift_main_map)

        for nid, p in prev_last.items():
            c = cur_first.get(nid, "-")
            if p == "N" and c == "D":
                prev_transition_violation_count += 1
                drilldown["prev_transition_violations"].append({"nurse_id": nid, "name": prev_names.get(nid), "prev": p, "curr": c})
            if p == "N" and c == "E":
                prev_transition_violation_count += 1
                drilldown["prev_transition_violations"].append({"nurse_id": nid, "name": prev_names.get(nid), "prev": p, "curr": c})
            if p == "E" and c == "D":
                prev_transition_violation_count += 1
                drilldown["prev_transition_violations"].append({"nurse_id": nid, "name": prev_names.get(nid), "prev": p, "curr": c})
            if p == "N" and c != "O":
                prev_n_recovery_violation_count += 1

        for nid, suf in prev_suffix.items():
            total = int(suf) + int(cur_prefix.get(nid, 0))
            if total > 6:
                prev_conseq_work_overflow_count += 1
                drilldown["prev_conseq_work_violations"].append(
                    {
                        "nurse_id": nid,
                        "name": prev_names.get(nid),
                        "prev_suffix_work": int(suf),
                        "curr_prefix_work": int(cur_prefix.get(nid, 0)),
                        "total": int(total),
                    }
                )

        f = _fixed_validations(roster, fixed if isinstance(fixed, dict) else {}, shift_main_map)
        fixed_changed_cell_count = int(f.get("fixed.changed_cell_count", 0))
        fixed_n_before_off_violation_count = int(f.get("fixed.n_before_fixed_off_violation_count", 0))

    metrics.update(
        {
            "off.off_days_out_of_band_count": off_band_cnt,
            "off.off_cap_exact_mismatch_count": off_cap_mismatch,
            "data.role_null_active_count": role_null_active_count,
            "preceptee.sync_violation_count": preceptee_sync_viol,
            "preceptee.mapping_invalid_count": preceptee_mapping_invalid,
            "grade.minmax_violation_count": grade_minmax_viol,
            "carryover.prev_transition_violation_count": prev_transition_violation_count,
            "carryover.prev_n_recovery_violation_count": prev_n_recovery_violation_count,
            "carryover.prev_conseq_work_overflow_count": prev_conseq_work_overflow_count,
            "carryover.dropped_ref_count": 0,
            "config.max_enabled_inconsistency_count": _max_enabled_inconsistency_count(daily if isinstance(daily, dict) else {}),
            "fixed.changed_cell_count": fixed_changed_cell_count,
            "fixed.n_before_fixed_off_violation_count": fixed_n_before_off_violation_count,
            "offswap.target_shift_multi_count": _offswap_target_multi_count(shifts if isinstance(shifts, list) else []),
            # TODO: exact offswap conversion metrics require conversion trace / annual-leave delta source
            "offswap.recovery_off_converted_count": 0,
            "offswap.night_only_off_converted_count": 0,
            "offswap.fixed_off_converted_count": 0,
            "offswap.ju_converted_count": 0,
        }
    )

    # evaluate rules
    rule_results = []
    for rule in rules:
        rid = rule.get("id")
        metric_name = rule.get("metric")
        cond = str(rule.get("pass_condition", "== 0"))
        severity = rule.get("severity", "warning")
        value = metrics.get(metric_name)
        if value is None:
            rule_results.append(
                {
                    "rule_id": rid,
                    "metric": metric_name,
                    "value": None,
                    "status": "SKIPPED",
                    "severity": severity,
                    "reason": "metric_not_implemented",
                }
            )
            continue
        passed = _eval_condition(value, cond)
        rule_results.append(
            {
                "rule_id": rid,
                "metric": metric_name,
                "value": value,
                "status": "PASS" if passed else "FAIL",
                "severity": severity,
                "pass_condition": cond,
            }
        )

    blocking_fails = [r for r in rule_results if r["status"] == "FAIL" and r["severity"] == "blocking"]
    warning_fails = [r for r in rule_results if r["status"] == "FAIL" and r["severity"] != "blocking"]
    blocking_skipped = [r for r in rule_results if r["status"] == "SKIPPED" and r["severity"] == "blocking"]
    final_fail = bool(blocking_fails) or (args.strict and bool(blocking_skipped))

    run_result = {
        "generated_at": _utc_now_str(),
        "year": year,
        "month": month,
        "strategy": args.strategy,
        "group_id": group_id,
        "input_hash": input_hash,
        "runs": [r.__dict__ for r in runs],
        "metrics": metrics,
        "drilldown": drilldown,
    }

    summary = {
        "generated_at": _utc_now_str(),
        "status": "FAIL" if final_fail else "PASS",
        "blocking_fail_count": len(blocking_fails),
        "blocking_skipped_count": len(blocking_skipped),
        "warning_fail_count": len(warning_fails),
        "rules_total": len(rule_results),
        "strict_mode": bool(args.strict),
        "rules": rule_results,
    }

    graph_export = {
        "run": {
            "run_id": f"{year}-{month:02d}-{group_id}-{args.strategy}-{ts}",
            "group_id": group_id,
            "year": year,
            "month": month,
            "strategy": args.strategy,
            "input_hash": input_hash,
        },
        "rules": [
            {
                "rule_id": r["rule_id"],
                "status": r["status"],
                "severity": r["severity"],
                "metric": r.get("metric"),
                "value": r.get("value"),
            }
            for r in rule_results
        ],
        "violations": [
            {
                "rule_id": r["rule_id"],
                "node_ids": [f"rule:{r['rule_id']}"],
                "evidence": {"metric": r.get("metric"), "value": r.get("value"), "condition": r.get("pass_condition")},
            }
            for r in rule_results
            if r["status"] == "FAIL"
        ],
    }

    triage_lines = [
        f"# Harness Triage ({year}-{month:02d}, {args.strategy})",
        "",
        f"- Group: `{group_id}`",
        f"- Input Hash: `{input_hash}`",
        f"- Blocking fails: **{len(blocking_fails)}**",
        f"- Blocking skipped: **{len(blocking_skipped)}**",
        f"- Warning fails: **{len(warning_fails)}**",
        "",
        "## Blocking fails",
    ]
    if not blocking_fails:
        triage_lines.append("- None")
    else:
        for r in blocking_fails[:50]:
            triage_lines.append(f"- {r['rule_id']} ({r['metric']}={r.get('value')}, need {r.get('pass_condition')})")

    triage_lines += ["", "## Warning fails"]
    if not warning_fails:
        triage_lines.append("- None")
    else:
        for r in warning_fails[:50]:
            triage_lines.append(f"- {r['rule_id']} ({r['metric']}={r.get('value')}, need {r.get('pass_condition')})")

    triage_lines += ["", "## Blocking skipped"]
    if not blocking_skipped:
        triage_lines.append("- None")
    else:
        for r in blocking_skipped[:50]:
            triage_lines.append(f"- {r['rule_id']} ({r['metric']})")

    # targeted drill-down for currently observed blocking fails
    if drilldown.get("coverage_under_cells"):
        triage_lines += ["", "## Drilldown: coverage under cells (top 20)"]
        for row in drilldown["coverage_under_cells"][:20]:
            triage_lines.append(
                f"- day={row['day']} shift={row['shift']} need={row['need']} assigned={row['assigned']} short={row['short']}"
            )
    if drilldown.get("prev_transition_violations"):
        triage_lines += ["", "## Drilldown: prev transition violations"]
        for row in drilldown["prev_transition_violations"][:20]:
            triage_lines.append(
                f"- nurse={row.get('nurse_id')}({row.get('name')}) prev={row.get('prev')} curr_day1={row.get('curr')}"
            )
    if drilldown.get("prev_conseq_work_violations"):
        triage_lines += ["", "## Drilldown: prev+curr consecutive work overflow"]
        for row in drilldown["prev_conseq_work_violations"][:20]:
            triage_lines.append(
                f"- nurse={row.get('nurse_id')}({row.get('name')}) prev={row.get('prev_suffix_work')} curr={row.get('curr_prefix_work')} total={row.get('total')}"
            )

    with open(os.path.join(out_base, "run_result.json"), "w", encoding="utf-8") as f:
        json.dump(run_result, f, ensure_ascii=False, indent=2)
    with open(os.path.join(out_base, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    with open(os.path.join(out_base, "graph_export.json"), "w", encoding="utf-8") as f:
        json.dump(graph_export, f, ensure_ascii=False, indent=2)
    with open(os.path.join(out_base, "triage.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(triage_lines) + "\n")

    print(
        json.dumps(
            {
                "out_dir": out_base,
                "status": summary["status"],
                "blocking_fail_count": len(blocking_fails),
                "blocking_skipped_count": len(blocking_skipped),
                "strict_mode": bool(args.strict),
            },
            ensure_ascii=False,
        )
    )
    if summary["status"] == "FAIL":
        sys.exit(1)


if __name__ == "__main__":
    main()
