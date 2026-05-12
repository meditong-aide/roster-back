from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import sys
from dataclasses import dataclass, field
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
    timing_ms: int = 0
    # Granular solver outcome (Group H의 cpsat/fallback 구분용).
    outcome: str | None = None
    used_fallback: bool = False
    # Constraint-level causal info from constraint_impact API.
    violated_constraints: List[Dict[str, Any]] = field(default_factory=list)
    risky_constraints: List[Dict[str, Any]] = field(default_factory=list)
    preflight_alerts: List[Dict[str, Any]] = field(default_factory=list)
    conflict_probe: Dict[str, Any] | None = None


def _derive_solver_outcome(used_fallback: bool, outcome: str | None) -> str:
    """Map (used_fallback, outcome) → granular solver status label.

    버킷:
      - primary             : CP-SAT primary 성공
      - primary-unsat       : CP-SAT primary 성공 but 의미상 unsat (드묾)
      - fallback-optimal    : CP-SAT infeasible → fallback이 해를 찾음
      - fallback-infeasible : CP-SAT infeasible → fallback도 실패
    """
    o = (outcome or "").lower()
    if used_fallback:
        return "fallback-optimal" if o == "sat" else "fallback-infeasible"
    return "primary" if o == "sat" else "primary-unsat"


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


def _coverage_over_cells(roster: Dict[str, Any], day_need: Dict[str, List[int]], shift_main_map: Dict[str, str]) -> List[Dict[str, Any]]:
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
            extra = assigned[d][sh] - need
            if extra > 0:
                out.append({"day": d + 1, "shift": sh, "need": need, "assigned": assigned[d][sh], "over": extra})
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


def _weekly_off_missing_count(weekly_off_payload: Dict[str, Any]) -> int:
    items = (weekly_off_payload or {}).get("items") or []
    missing = 0
    for it in items:
        if bool(it.get("weekly_off_enabled")) and it.get("preview_weekday") in (None, "", "null"):
            missing += 1
    return missing


def _weekly_off_missing_nurse_ids(weekly_off_payload: Dict[str, Any]) -> List[str]:
    items = (weekly_off_payload or {}).get("items") or []
    out: List[str] = []
    for it in items:
        if bool(it.get("weekly_off_enabled")) and it.get("preview_weekday") in (None, "", "null"):
            nid = str(it.get("nurse_id") or "")
            if nid:
                out.append(nid)
    return out


def _wanted_apply_ratio_from_fixed(
    roster: Dict[str, Any],
    fixed_payload: Dict[str, Any],
    shift_main_map: Dict[str, str],
) -> float:
    entries = (fixed_payload or {}).get("entries") or []
    if not entries:
        return 1.0
    by_nurse: Dict[str, List[str]] = {}
    for n in roster.get("nurses") or []:
        nid = str(n.get("id") or "")
        if nid:
            by_nurse[nid] = [_to_main(x, shift_main_map) for x in (n.get("schedule") or [])]
    total = 0
    matched = 0
    for e in entries:
        nid = str(e.get("nurse_id") or "")
        day = int(e.get("day") or 0)
        if not nid or day <= 0 or nid not in by_nurse:
            continue
        sched = by_nurse[nid]
        if day - 1 >= len(sched):
            continue
        want = _to_main(str(e.get("shift_id") or ""), shift_main_map)
        total += 1
        if sched[day - 1] == want:
            matched += 1
    if total == 0:
        return 1.0
    return matched / float(total)


def _wanted_submission_ratio(submissions_payload: Dict[str, Any], eval_ids: set[str]) -> float:
    submitted = set(str(x) for x in ((submissions_payload or {}).get("submitted_nurses") or []))
    if not eval_ids:
        return 1.0
    return len(submitted & eval_ids) / float(len(eval_ids))


def _contains_offswap_signal(obj: Any) -> bool:
    if obj is None:
        return False
    if isinstance(obj, dict):
        for k, v in obj.items():
            if "off" in str(k).lower() and "swap" in str(k).lower():
                return True
            if _contains_offswap_signal(v):
                return True
        return False
    if isinstance(obj, list):
        return any(_contains_offswap_signal(x) for x in obj)
    s = str(obj).lower()
    return ("off" in s and "swap" in s)


def _preceptee_pair_shift_concentration_ratio(
    roster: Dict[str, Any],
    nurses_payload: List[Dict[str, Any]],
    shift_main_map: Dict[str, str],
) -> float:
    by_nid = {str(n.get("nurse_id") or ""): n for n in nurses_payload}
    sched_by_nid: Dict[str, List[str]] = {}
    for n in roster.get("nurses") or []:
        nid = str(n.get("id") or "")
        if nid:
            sched_by_nid[nid] = [_to_main(x, shift_main_map) for x in (n.get("schedule") or [])]

    shift_hits = {"D": 0, "E": 0, "N": 0, "M": 0}
    total_hits = 0
    for nid, n in by_nid.items():
        pid = n.get("preceptor_id")
        if pid in (None, "", "null"):
            continue
        pid = str(pid)
        s1 = sched_by_nid.get(nid)
        s2 = sched_by_nid.get(pid)
        if not s1 or not s2:
            continue
        days = min(len(s1), len(s2))
        for d in range(days):
            a, b = s1[d], s2[d]
            if a == b and a in shift_hits:
                shift_hits[a] += 1
                total_hits += 1

    if total_hits == 0:
        return 0.0
    return max(shift_hits.values()) / float(total_hits)


def _fairness_metrics(
    roster: Dict[str, Any],
    shift_main_map: Dict[str, str],
    eval_ids: set[str],
) -> Dict[str, float]:
    den_counts: List[int] = []
    total_work_counts: List[int] = []
    night_counts: List[int] = []
    for n in roster.get("nurses") or []:
        nid = str(n.get("id") or "")
        if nid and eval_ids and nid not in eval_ids:
            continue
        main = [_to_main(x, shift_main_map) for x in (n.get("schedule") or [])]
        d = sum(1 for x in main if x == "D")
        e = sum(1 for x in main if x == "E")
        nn = sum(1 for x in main if x == "N")
        den = d + e + nn
        total_work = sum(1 for x in main if x in ("D", "E", "N", "M"))
        den_counts.append(den)
        total_work_counts.append(total_work)
        night_counts.append(nn)

    if not den_counts:
        return {
            "fairness.den_spread_ratio": 0.0,
            "fairness.total_work_spread": 0.0,
            "fairness.n_shift_skew_ratio": 0.0,
        }

    den_max = max(den_counts)
    den_min = min(den_counts)
    total_max = max(total_work_counts)
    total_min = min(total_work_counts)
    n_total = sum(night_counts)
    n_max = max(night_counts) if night_counts else 0
    return {
        "fairness.den_spread_ratio": (float(den_max - den_min) / float(den_max)) if den_max > 0 else 0.0,
        "fairness.total_work_spread": float(total_max - total_min),
        "fairness.n_shift_skew_ratio": (float(n_max) / float(n_total)) if n_total > 0 else 0.0,
    }


def _fairness_total_work_extremes(
    roster: Dict[str, Any],
    shift_main_map: Dict[str, str],
    eval_ids: set[str],
) -> Dict[str, Any]:
    rows: List[Tuple[str, int]] = []
    for n in roster.get("nurses") or []:
        nid = str(n.get("id") or "")
        if not nid or (eval_ids and nid not in eval_ids):
            continue
        main = [_to_main(x, shift_main_map) for x in (n.get("schedule") or [])]
        total_work = sum(1 for x in main if x in ("D", "E", "N", "M"))
        rows.append((nid, total_work))
    if not rows:
        return {"min_nurse_id": None, "max_nurse_id": None, "min_total_work": 0, "max_total_work": 0}
    rows.sort(key=lambda x: x[1])
    min_nid, min_w = rows[0]
    max_nid, max_w = rows[-1]
    return {
        "min_nurse_id": min_nid,
        "max_nurse_id": max_nid,
        "min_total_work": int(min_w),
        "max_total_work": int(max_w),
    }


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


def _parse_date_ymd(v: Any) -> tuple[int, int, int] | None:
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    s = s.replace("/", "-")
    if "T" in s:
        s = s.split("T", 1)[0]
    parts = s.split("-")
    if len(parts) < 3:
        return None
    try:
        y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
        return y, m, d
    except Exception:
        return None


def _monthly_joiner_ids(nurses_payload: List[Dict[str, Any]], year: int, month: int) -> set[str]:
    out: set[str] = set()
    for n in nurses_payload:
        nid = str(n.get("nurse_id") or "")
        if not nid:
            continue
        dtv = n.get("joining_date")
        p = _parse_date_ymd(dtv)
        if not p:
            continue
        y, m, _ = p
        if y == year and m == month:
            out.add(nid)
    return out


def _preceptee_ids(nurses_payload: List[Dict[str, Any]]) -> set[str]:
    out: set[str] = set()
    for n in nurses_payload:
        nid = str(n.get("nurse_id") or "")
        pid = n.get("preceptor_id")
        if nid and pid not in (None, "", "null"):
            out.add(nid)
    return out


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


def _owner_family_nodes(owner_families: List[str], ctx: Dict[str, Any], run_id: str) -> List[Dict[str, Any]]:
    nodes: List[Dict[str, Any]] = []
    seen: set[str] = set()

    def add(node_id: str, node_type: str, attrs: Dict[str, Any]) -> None:
        if node_id in seen:
            return
        seen.add(node_id)
        nodes.append({"id": node_id, "type": node_type, "attrs": attrs})

    day = ctx.get("day")
    shift = ctx.get("shift")
    team_id = str(ctx.get("team_id") or "*")
    grade = str(ctx.get("grade") or "*")
    nurse_id = str(ctx.get("nurse_id") or "*")
    fixed_entries_count = int(ctx.get("fixed_entries_count") or 0)
    wanted_submission_ratio = float(ctx.get("wanted_submission_ratio") or 0.0)
    wanted_apply_ratio = float(ctx.get("wanted_apply_ratio") or 0.0)
    coverage_mode = str(ctx.get("coverage_mode") or "min")
    metric_name = str(ctx.get("metric_name") or "")

    for fam in owner_families:
        if fam == "coverage" and day and shift:
            if coverage_mode == "max":
                add(f"coverage:max:{day}:{shift}", "CoverageMaxNode", {"day": day, "shift": shift})
            else:
                add(f"coverage:min:{day}:{shift}", "CoverageMinNode", {"day": day, "shift": shift})
        elif fam == "team_min" and day and shift:
            add(
                f"team_min:{team_id}:{day}:{shift}",
                "TeamMinNode",
                {"team_id": team_id, "day": day, "shift": shift},
            )
        elif fam == "grade_min" and day and shift:
            add(
                f"grade_min:{grade}:{day}:{shift}",
                "GradeMinNode",
                {"grade": grade, "day": day, "shift": shift},
            )
        elif fam == "grade_max" and day and shift:
            add(
                f"grade_max:{grade}:{day}:{shift}",
                "GradeMaxNode",
                {"grade": grade, "day": day, "shift": shift},
            )
        elif fam == "off_window":
            add(
                f"off_window:{nurse_id}:{day or '*'}:{shift or '*'}",
                "OffWindowNode",
                {"nurse_id": nurse_id, "day": day, "shift": shift},
            )
        elif fam in ("transition_ban", "carryover_prev_month"):
            add(
                f"carryover:transition:{nurse_id}:{day or 1}",
                "CarryoverTransitionNode",
                {"nurse_id": nurse_id, "day": day},
            )
        elif fam == "preceptee_sync":
            add(
                f"preceptee_sync:{ctx.get('pair_id') or '*'}:{day or '*'}:{shift or '*'}",
                "PrecepteeSyncNode",
                {"pair_id": ctx.get("pair_id") or "*", "day": day, "shift": shift},
            )
        elif fam == "solver":
            add(f"solver:run:{run_id}", "ConstraintNode", {"kind": "solver"})
        elif fam == "wanted":
            add(
                f"wanted:submission:{run_id}",
                "WantedSubmissionNode",
                {"run_id": run_id, "ratio": wanted_submission_ratio},
            )
            add(
                f"wanted:apply:{run_id}",
                "WantedApplyNode",
                {
                    "run_id": run_id,
                    "ratio": wanted_apply_ratio,
                    "fixed_entries_count": fixed_entries_count,
                },
            )
        elif fam == "monthly_off":
            add(
                f"monthly_off:{nurse_id}:{run_id}",
                "MonthlyOffNode",
                {"nurse_id": nurse_id, "run_id": run_id},
            )
        elif fam == "weekly_off":
            add(
                f"weekly_off:{nurse_id}:{run_id}",
                "WeeklyOffNode",
                {"nurse_id": nurse_id, "run_id": run_id},
            )
        elif fam == "fairness":
            add(
                f"fairness:{metric_name or 'metric'}:{run_id}",
                "FairnessNode",
                {"run_id": run_id, "metric": metric_name},
            )
        elif fam == "data_quality":
            add(
                f"data_quality:role:{nurse_id or '*'}:{run_id}",
                "DataQualityNode",
                {"run_id": run_id, "nurse_id": nurse_id or "*", "field": "role"},
            )

    return nodes


def _evidence_from_rule(
    rule: Dict[str, Any],
    drilldown: Dict[str, Any],
    runs: List[RunStats],
) -> Dict[str, Any]:
    rid = str(rule.get("rule_id") or "")
    metric = str(rule.get("metric") or "")
    value = rule.get("value")
    ci_node_ids = list(drilldown.get("ci_violated_node_ids") or [])
    ci_reason_codes = list(drilldown.get("ci_preflight_reason_codes") or [])

    if rid in ("D_D_MIN", "D_E_MIN", "D_N_MIN", "D_M_MIN"):
        shift = rid.split("_")[1]
        cells = [c for c in (drilldown.get("coverage_under_cells") or []) if str(c.get("shift")) == shift]
        if cells:
            top = cells[0]
            return {
                "metric": metric,
                "value": value,
                "condition": rule.get("pass_condition"),
                "day": top.get("day"),
                "shift": top.get("shift"),
                "need": top.get("need"),
                "assigned": top.get("assigned"),
                "short": top.get("short"),
                "slack": -int(top.get("short") or 0),
            }

    if rid == "F_PREV_TRANSITION":
        rows = drilldown.get("prev_transition_violations") or []
        if rows:
            top = rows[0]
            return {
                "metric": metric,
                "value": value,
                "condition": rule.get("pass_condition"),
                "nurse_id": top.get("nurse_id"),
                "prev": top.get("prev"),
                "curr": top.get("curr"),
                "slack": -1,
            }

    if rid == "F_PREV_CONSEQ_WORK":
        rows = drilldown.get("prev_conseq_work_violations") or []
        if rows:
            top = rows[0]
            overflow = max(0, int(top.get("total") or 0) - 6)
            return {
                "metric": metric,
                "value": value,
                "condition": rule.get("pass_condition"),
                "nurse_id": top.get("nurse_id"),
                "prev_suffix_work": top.get("prev_suffix_work"),
                "curr_prefix_work": top.get("curr_prefix_work"),
                "total": top.get("total"),
                "slack": -int(overflow),
            }

    if rid == "H_NO_INFEASIBLE":
        details = [r.infeasible_detail for r in runs if r.status_code != 200 and r.infeasible_detail is not None]
        return {
            "metric": metric,
            "value": value,
            "condition": rule.get("pass_condition"),
            "infeasible_details": details[:5],
            "slack": -int(value or 0),
        }

    if rid == "E_WANTED_APPLY":
        return {
            "metric": metric,
            "value": value,
            "condition": rule.get("pass_condition"),
            "fixed_entries_count": int(rule.get("fixed_entries_count") or 0),
            "wanted_submission_ratio": float(rule.get("wanted_submission_ratio") or 0.0),
            "wanted_apply_ratio": float(rule.get("wanted_apply_ratio") or float(value or 0.0)),
            "slack": float(value or 0.0) - float(str(rule.get("pass_condition", ">= 0")).replace(">=", "").strip() or 0.0),
        }

    if rid == "D_MAX_OVER":
        rows = drilldown.get("coverage_over_cells") or []
        if rows:
            top = rows[0]
            return {
                "metric": metric,
                "value": value,
                "condition": rule.get("pass_condition"),
                "day": top.get("day"),
                "shift": top.get("shift"),
                "need": top.get("need"),
                "assigned": top.get("assigned"),
                "over": top.get("over"),
                "coverage_mode": "max",
                "ci_node_ids": ci_node_ids[:20],
                "ci_reason_codes": ci_reason_codes[:20],
                "slack": -int(top.get("over") or 0),
            }

    if rid == "B_OFF_NEAR_CONFIG":
        rows = drilldown.get("off_band_nurses") or []
        if rows:
            top = rows[0]
            return {
                "metric": metric,
                "value": value,
                "condition": rule.get("pass_condition"),
                "nurse_id": top.get("nurse_id"),
                "team_id": top.get("team_id"),
                "off_days": top.get("off_days"),
                "off_cfg": top.get("off_cfg"),
                "slack": -1,
            }

    if rid == "B_WEEKLY_OFF":
        rows = drilldown.get("weekly_off_missing_nurses") or []
        if rows:
            top = rows[0]
            return {
                "metric": metric,
                "value": value,
                "condition": rule.get("pass_condition"),
                "nurse_id": top.get("nurse_id"),
                "team_id": top.get("team_id"),
                "slack": -1,
            }

    if rid == "C_TOTAL_BALANCE":
        ex = drilldown.get("fairness_total_work_extremes") or {}
        if ex:
            return {
                "metric": metric,
                "value": value,
                "condition": rule.get("pass_condition"),
                "nurse_id": ex.get("max_nurse_id"),
                "team_id": ex.get("max_team_id"),
                "min_nurse_id": ex.get("min_nurse_id"),
                "min_team_id": ex.get("min_team_id"),
                "min_total_work": ex.get("min_total_work"),
                "max_total_work": ex.get("max_total_work"),
                "slack": None,
            }

    if rid == "G_ROLE_NULL":
        rows = drilldown.get("role_null_nurses") or []
        if rows:
            top = rows[0]
            return {
                "metric": metric,
                "value": value,
                "condition": rule.get("pass_condition"),
                "nurse_id": top.get("nurse_id"),
                "team_id": top.get("team_id"),
                "slack": -1,
            }

    slack = None
    try:
        if str(rule.get("pass_condition", "")).strip() == "== 0" and value is not None:
            slack = -float(value)
    except Exception:
        slack = None
    return {
        "metric": metric,
        "value": value,
        "condition": rule.get("pass_condition"),
        "ci_node_ids": ci_node_ids[:20],
        "ci_reason_codes": ci_reason_codes[:20],
        "slack": slack,
    }


def _build_graph_export(
    *,
    run_id: str,
    group_id: str,
    year: int,
    month: int,
    strategy: str,
    input_hash: str,
    rule_results: List[Dict[str, Any]],
    rules_catalog: List[Dict[str, Any]],
    drilldown: Dict[str, Any],
    runs: List[RunStats],
    solver_status: str,
) -> Dict[str, Any]:
    catalog_by_id = {str(r.get("id") or ""): r for r in rules_catalog}
    nodes: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []
    node_seen: set[str] = set()
    edge_seen: set[str] = set()

    def add_node(node_id: str, node_type: str, attrs: Dict[str, Any]) -> None:
        if node_id in node_seen:
            return
        node_seen.add(node_id)
        nodes.append({"id": node_id, "type": node_type, "attrs": attrs})

    def add_edge(edge_type: str, from_id: str, to_id: str) -> None:
        k = f"{edge_type}|{from_id}|{to_id}"
        if k in edge_seen:
            return
        edge_seen.add(k)
        edges.append({"type": edge_type, "from": from_id, "to": to_id})

    def add_entity_context_nodes(ev: Dict[str, Any], violation_id: str) -> List[str]:
        created: List[str] = []
        day = ev.get("day")
        shift = ev.get("shift")
        nurse_id = ev.get("nurse_id")
        team_id = ev.get("team_id")

        if day is not None:
            day_node = f"day:{year}-{month:02d}:{int(day)}"
            add_node(day_node, "DayNode", {"year": year, "month": month, "day": int(day)})
            add_edge("CONTEXT_OF", day_node, violation_id)
            created.append(day_node)

        if shift:
            shift_code = str(shift).upper()
            shift_node = f"shift:{shift_code}"
            add_node(shift_node, "ShiftNode", {"code": shift_code})
            add_edge("CONTEXT_OF", shift_node, violation_id)
            created.append(shift_node)

        if nurse_id:
            n_node = f"nurse:{nurse_id}"
            add_node(n_node, "NurseNode", {"nurse_id": str(nurse_id)})
            add_edge("CONTEXT_OF", n_node, violation_id)
            created.append(n_node)

        if team_id and str(team_id) != "*":
            t_node = f"team:{team_id}"
            add_node(t_node, "TeamNode", {"team_id": str(team_id)})
            add_edge("CONTEXT_OF", t_node, violation_id)
            created.append(t_node)

        if day is not None and shift:
            day_node = f"day:{year}-{month:02d}:{int(day)}"
            shift_node = f"shift:{str(shift).upper()}"
            add_edge("DAY_SHIFT", day_node, shift_node)

        return created

    month_id = f"month:{year}-{month:02d}"
    group_node_id = f"group:{group_id}"
    run_node_id = f"run:{run_id}"
    add_node(month_id, "MonthNode", {"year": year, "month": month})
    add_node(group_node_id, "GroupNode", {"group_id": group_id})
    add_node(
        run_node_id,
        "RunNode",
        {"run_id": run_id, "strategy": strategy, "input_hash": input_hash, "solver_status": solver_status},
    )
    add_edge("RUN_ON", run_node_id, month_id)
    add_edge("RUN_ON", run_node_id, group_node_id)

    mapped_rules = 0
    fail_rules_with_constraint_nodes = 0
    fail_rules_without_constraint_nodes: List[str] = []
    violations: List[Dict[str, Any]] = []
    for idx, rr in enumerate(rule_results):
        rid = str(rr.get("rule_id") or "")
        metric_name = str(rr.get("metric") or "")
        rule_node_id = f"rule:{rid}"
        metric_node_id = f"metric:{run_id}:{metric_name}"
        add_node(rule_node_id, "RuleNode", {"rule_id": rid, "severity": rr.get("severity")})
        add_node(metric_node_id, "MetricNode", {"metric": metric_name, "value": rr.get("value"), "run_id": run_id})
        add_edge("EVALUATED_BY", metric_node_id, rule_node_id)

        spec_rule = catalog_by_id.get(rid, {})
        families = list(spec_rule.get("owner_family") or [])
        if families:
            mapped_rules += 1

        if rr.get("status") != "FAIL":
            continue

        evidence = _evidence_from_rule(rr, drilldown, runs)
        viol_node_id = f"violation:{run_id}:{rid}:{idx}"
        add_node(
            viol_node_id,
            "ViolationNode",
            {
                "rule_id": rid,
                "run_id": run_id,
                "severity": rr.get("severity"),
                "slack": evidence.get("slack"),
            },
        )
        # 방향: cause→effect 통일 (Rule이 violation을 발생시킴, Run이 violation을 만들어냄)
        add_edge("FAILED_RULE", rule_node_id, viol_node_id)
        add_edge("OBSERVED_IN", run_node_id, viol_node_id)

        context = {
            "day": evidence.get("day"),
            "shift": evidence.get("shift"),
            "nurse_id": evidence.get("nurse_id"),
            "pair_id": evidence.get("pair_id"),
            "team_id": evidence.get("team_id"),
            "fixed_entries_count": evidence.get("fixed_entries_count", 0),
            "wanted_submission_ratio": evidence.get("wanted_submission_ratio", 0.0),
            "wanted_apply_ratio": evidence.get("wanted_apply_ratio", 0.0),
            "coverage_mode": evidence.get("coverage_mode", "min"),
            "metric_name": metric_name,
        }
        c_nodes = _owner_family_nodes(families, context, run_id)
        if c_nodes:
            fail_rules_with_constraint_nodes += 1
        else:
            fail_rules_without_constraint_nodes.append(rid)
        for cn in c_nodes:
            add_node(cn["id"], cn["type"], cn["attrs"])
            add_edge("CAUSES_VIOLATION", cn["id"], viol_node_id)
        add_edge("CAUSES_VIOLATION", metric_node_id, viol_node_id)

        entity_node_ids = add_entity_context_nodes(evidence, viol_node_id)

        node_ids = [rule_node_id, metric_node_id] + [cn["id"] for cn in c_nodes] + entity_node_ids
        violations.append(
            {
                "rule_id": rid,
                "violation_id": viol_node_id,
                "node_ids": node_ids,
                "evidence": evidence,
                "slack": evidence.get("slack"),
            }
        )

    # Constraint-level causality from API constraint_impact (violated/risky).
    # Each RunStats can carry the constraint node_ids that the solver could not satisfy
    # (BLOCKED_RUN) or barely satisfied (RISKY_FOR). These are independent of harness
    # rule evaluation — they explain *why* the solver took the fallback path.
    def _ctype_from_id(nid: str) -> str:
        s = (nid or "").lower()
        if s.startswith("coverage:min") or s.startswith("coverage_min"):
            return "CoverageMinNode"
        if s.startswith("coverage:max") or s.startswith("coverage_max"):
            return "CoverageMaxNode"
        if s.startswith("team_min") or s.startswith("team:min"):
            return "TeamMinNode"
        if s.startswith("team_max") or s.startswith("team:max"):
            return "TeamMaxNode"
        if s.startswith("grade_min") or s.startswith("grade:min"):
            return "GradeMinNode"
        if s.startswith("grade_max") or s.startswith("grade:max"):
            return "GradeMaxNode"
        if s.startswith("monthly_off") or s.startswith("monthly:off"):
            return "MonthlyOffNode"
        if s.startswith("weekly_off") or s.startswith("weekly:off"):
            return "WeeklyOffNode"
        if s.startswith("off_window") or s.startswith("offwindow"):
            return "OffWindowNode"
        if s.startswith("preceptee") or s.startswith("precept"):
            return "PrecepteeNode"
        if s.startswith("nurse:"):
            return "NurseNode"
        if s.startswith("fixed:"):
            return "FixedConstraintNode"
        if s.startswith("wanted:"):
            return "WantedNode"
        return "ConstraintNode"

    constraint_blocks: List[Dict[str, Any]] = []
    # H_NO_INFEASIBLE / H_FALLBACK_OK 위반을 미리 찾아둠 — 5xx run의 preflight 이슈를
    # 직접 그 위반들의 인과로 박기 위함.
    h_infeas_viol_ids = [v["violation_id"] for v in violations if v.get("rule_id") == "H_NO_INFEASIBLE"]
    for rs in runs:
        attempt_label = f"{rs.solver_status or '?'}#{rs.run_index}"
        if rs.status_code != 200:
            # 5xx 경로: API가 채운 preflight_issues / violated_constraints를
            # ConstraintNode + BLOCKED_RUN + CAUSES_VIOLATION 으로 표면화.
            inf = rs.infeasible_detail if isinstance(rs.infeasible_detail, dict) else {}

            preflight = list(inf.get("preflight_issues") or [])
            for idx, issue in enumerate(preflight[:50]):
                rc = str(issue.get("reason_code") or issue.get("code") or f"issue{idx}")
                cid = f"precheck:{rc}:{rs.run_index}:{idx}"
                ctype = _ctype_from_id(rc) if rc else "ConstraintNode"
                add_node(
                    cid,
                    ctype,
                    {
                        "reason_code": rc,
                        "human_message_ko": issue.get("human_message_ko"),
                        "evidence": issue.get("evidence"),
                        "fix_suggestions_ko": issue.get("fix_suggestions_ko"),
                        "first_seen_attempt": attempt_label,
                    },
                )
                add_edge("BLOCKED_RUN", cid, run_node_id)
                for vid in h_infeas_viol_ids:
                    add_edge("CAUSES_VIOLATION", cid, vid)
                constraint_blocks.append(
                    {"node_id": cid, "ctype": ctype, "kind": "precheck",
                     "reason_code": rc, "attempt": attempt_label}
                )

            # ConflictCore: 다중 hard 제약이 결합한 충돌 코어를 ConflictCoreNode +
            # member ConstraintNode들 + 인과 엣지로 emit. dashboard에서 인과 스토리로
            # 펼쳐 보임.
            cores = list(inf.get("conflict_cores") or [])
            for core in cores[:50]:
                core_id = str(core.get("core_id") or "")
                if not core_id:
                    continue
                add_node(
                    core_id,
                    "ConflictCoreNode",
                    {
                        "core_id": core_id,
                        "pattern": core.get("pattern"),
                        "scope": core.get("scope"),
                        "nurse_id": core.get("nurse_id"),
                        "affected_count": core.get("affected_count"),
                        "affected_nurse_ids": core.get("affected_nurse_ids") or [],
                        "per_nurse_cores": core.get("per_nurse_cores") or [],
                        "conclusion": core.get("conclusion"),
                        "human_message_ko": core.get("human_message_ko"),
                        "derivation": core.get("derivation") or [],
                        "resolution_hints": core.get("resolution_hints") or [],
                        "source": core.get("source"),
                        "solver_phase": core.get("solver_phase"),
                        "first_seen_attempt": attempt_label,
                    },
                )
                # 코어 자체가 H_NO_INFEASIBLE 위반의 원인
                for vid in h_infeas_viol_ids:
                    add_edge("CAUSES_VIOLATION", core_id, vid)
                add_edge("BLOCKED_RUN", core_id, run_node_id)
                # 구성원 ConstraintNode들 → core (MEMBER_OF_CONFLICT)
                for member in (core.get("members") or []):
                    m_id = str(member.get("node_id") or "")
                    m_type = str(member.get("type") or "ConstraintNode")
                    if not m_id:
                        continue
                    add_node(
                        m_id,
                        m_type,
                        {
                            "node_id": m_id,
                            "label": member.get("label"),
                            "value": member.get("value"),
                            "human_message_ko": member.get("human_message_ko"),
                        },
                    )
                    add_edge("MEMBER_OF_CONFLICT", m_id, core_id)
                constraint_blocks.append(
                    {
                        "node_id": core_id,
                        "ctype": "ConflictCoreNode",
                        "kind": "conflict_core",
                        "pattern": core.get("pattern"),
                        "attempt": attempt_label,
                    }
                )

            # build_unrecoverable_payload가 채운 violated_constraints — 솔버/검증기가
            # 식별한 실제 인과 제약. node_id prefix가 typed ConstraintNode로 매핑된다.
            violated = list(inf.get("violated_constraints") or [])
            for vc in violated[:50]:
                cid = str(vc.get("node_id") or "")
                if not cid:
                    continue
                ctype = _ctype_from_id(cid)
                add_node(
                    cid,
                    ctype,
                    {
                        "node_id": cid,
                        "reason_code": vc.get("reason_code"),
                        "slack": vc.get("slack"),
                        "details": vc.get("details"),
                        "human_message_ko": vc.get("human_message_ko"),
                        "first_seen_attempt": attempt_label,
                    },
                )
                add_edge("BLOCKED_RUN", cid, run_node_id)
                for vid in h_infeas_viol_ids:
                    add_edge("CAUSES_VIOLATION", cid, vid)
                constraint_blocks.append(
                    {"node_id": cid, "ctype": ctype, "kind": "infeasibility",
                     "reason_code": vc.get("reason_code"), "attempt": attempt_label}
                )
            continue
        for vc in (rs.violated_constraints or [])[:50]:
            cid = str(vc.get("node_id") or "")
            if not cid:
                continue
            ctype = _ctype_from_id(cid)
            add_node(
                cid,
                ctype,
                {
                    "node_id": cid,
                    "slack": vc.get("slack"),
                    "pressure": vc.get("pressure"),
                    "details": vc.get("details"),
                    "first_seen_attempt": attempt_label,
                },
            )
            add_edge("BLOCKED_RUN", cid, run_node_id)
            constraint_blocks.append(
                {
                    "node_id": cid,
                    "ctype": ctype,
                    "kind": "violated",
                    "slack": vc.get("slack"),
                    "pressure": vc.get("pressure"),
                    "attempt": attempt_label,
                }
            )
        for rc in (rs.risky_constraints or [])[:50]:
            cid = str(rc.get("node_id") or "")
            if not cid:
                continue
            ctype = _ctype_from_id(cid)
            add_node(
                cid,
                ctype,
                {
                    "node_id": cid,
                    "slack": rc.get("slack"),
                    "pressure": rc.get("pressure"),
                    "details": rc.get("details"),
                    "risky": True,
                    "first_seen_attempt": attempt_label,
                },
            )
            add_edge("RISKY_FOR", cid, run_node_id)
            constraint_blocks.append(
                {
                    "node_id": cid,
                    "ctype": ctype,
                    "kind": "risky",
                    "slack": rc.get("slack"),
                    "pressure": rc.get("pressure"),
                    "attempt": attempt_label,
                }
            )

    fail_rule_ids = {str(r.get("rule_id") or "") for r in rule_results if r.get("status") == "FAIL"}
    graph_fail_rule_ids = {str(v.get("rule_id") or "") for v in violations}
    consistency = {
        "fail_rule_count": len(fail_rule_ids),
        "graph_violation_count": len(violations),
        "missing_in_graph": sorted(fail_rule_ids - graph_fail_rule_ids),
        "extra_in_graph": sorted(graph_fail_rule_ids - fail_rule_ids),
    }

    return {
        "run": {
            "run_id": run_id,
            "group_id": group_id,
            "year": year,
            "month": month,
            "strategy": strategy,
            "input_hash": input_hash,
            "solver_status": solver_status,
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
        "violations": violations,
        "constraint_blocks": constraint_blocks,
        "nodes": nodes,
        "edges": edges,
        "mapping_summary": {
            "rules_total": len(rule_results),
            "mapped_rules_count": mapped_rules,
            "fail_rules_total": len(fail_rule_ids),
            "fail_rules_with_constraint_nodes": fail_rules_with_constraint_nodes,
            "fail_rules_without_constraint_nodes": sorted(set(fail_rules_without_constraint_nodes)),
            "unmapped_rules": sorted(
                [
                    str(r.get("rule_id") or "")
                    for r in rule_results
                    if not list((catalog_by_id.get(str(r.get("rule_id") or ""), {}) or {}).get("owner_family") or [])
                ]
            ),
        },
        "consistency": consistency,
    }


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
    weekly_off = _get_json(s, args.base_url, "/weekly-off/nurses", params={"year": year, "month": month})
    wanted_status = _get_json(s, args.base_url, "/wanted/status", params={"year": year, "month": month})
    wanted_submissions = _get_json(s, args.base_url, f"/wanted/{year}/{month}/submissions")
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
        "weekly_off": weekly_off,
        "wanted_status": wanted_status,
        "wanted_submissions": wanted_submissions,
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
            # 5xx 응답의 body는 build_blocking_payload / build_unrecoverable_payload
            # 결과로 `detail.infeasibility.{preflight_issues, applied_relaxations, ...}`
            # 구조를 갖는다. 이 정보로 solver_status를 더 세밀하게 분류한다.
            body_obj = body if isinstance(body, dict) else {}
            detail = body_obj.get("detail") if isinstance(body_obj, dict) else None
            if isinstance(detail, dict):
                inf = detail.get("infeasibility") or {}
                applied_relax = list(inf.get("applied_relaxations") or [])
                preflight = list(inf.get("preflight_issues") or [])
                # `last_error_reason`는 build_unrecoverable_payload에서만 채워지므로
                # precheck blocking vs solver/fallback-infeasible을 구분하는 신뢰 신호.
                # applied_relaxations는 부가 신호.
                last_err = inf.get("last_error_reason")
                if last_err or applied_relax:
                    st.solver_status = "fallback-infeasible"
                    st.used_fallback = True
                else:
                    st.solver_status = "cpsat-infeasible"
                st.outcome = "unsat"
                st.preflight_alerts = preflight
                st.infeasible_detail = inf
            else:
                st.infeasible_detail = detail if detail is not None else body
                st.solver_status = "http-error"
            runs.append(st)
            continue

        st.schedule_id = str(body.get("schedule_id") or "")
        ci = body.get("constraint_impact") or {}
        st.coverage_gaps_count = len(ci.get("coverage_gaps") or [])
        st.violated_constraints = list(ci.get("violated_constraints") or [])
        st.risky_constraints = list(ci.get("risky_constraints") or [])
        st.preflight_alerts = list(ci.get("preflight_alerts") or [])
        st.conflict_probe = ci.get("conflict_probe")
        st.violated_constraints_count = len(st.violated_constraints)
        st.timing_ms = int(ci.get("timing_ms") or 0)
        # Derive granular solver_status from (used_fallback, outcome).
        snap_meta = ci.get("snapshot_meta") or {}
        st.used_fallback = bool(snap_meta.get("used_fallback") or False)
        st.outcome = ci.get("outcome")
        st.solver_status = _derive_solver_outcome(st.used_fallback, st.outcome)
        # Surface constraint-level info under infeasible_detail even when HTTP 200,
        # so the dashboard can answer "what blocked this run".
        if st.violated_constraints or st.risky_constraints or st.conflict_probe or st.preflight_alerts:
            st.infeasible_detail = {
                "violated_constraints": st.violated_constraints[:50],
                "risky_constraints": st.risky_constraints[:50],
                "preflight_alerts": st.preflight_alerts[:20],
                "conflict_probe": st.conflict_probe,
            }

        roster = _get_json(s, args.base_url, f"/roster/schedule/{st.schedule_id}")
        cov = _coverage_counts(roster, day_need, shift_main_map)
        for k, v in cov.items():
            setattr(st, k, int(v))
        runs.append(st)

    # aggregate metrics
    success_runs = [r for r in runs if r.status_code == 200]
    success_timing = sorted([int(r.timing_ms) for r in success_runs if int(r.timing_ms) > 0])
    if success_timing:
        idx95 = max(0, int(math.ceil(0.95 * len(success_timing))) - 1)
        solve_time_ms_p95 = int(success_timing[min(idx95, len(success_timing) - 1)])
    else:
        solve_time_ms_p95 = 0
    fallback_error_count = sum(
        1
        for r in success_runs
        if str(r.solver_status or "").strip().lower() not in ("", "primary", "ok", "success")
    )

    metrics: Dict[str, Any] = {
        "coverage.under_count_D": sum(r.under_count_D for r in runs),
        "coverage.under_count_E": sum(r.under_count_E for r in runs),
        "coverage.under_count_N": sum(r.under_count_N for r in runs),
        "coverage.under_count_M": sum(r.under_count_M for r in runs),
        "coverage.max_overflow_count": sum(
            r.over_count_D + r.over_count_E + r.over_count_N + r.over_count_M for r in runs
        ),
        "system.infeasible_run_count": sum(1 for r in runs if r.status_code != 200),
        "system.fallback_error_count": fallback_error_count,
        "system.solve_time_ms_p95": solve_time_ms_p95,
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
    nurses_list = nurses_payload if isinstance(nurses_payload, list) else []
    active_ids = _active_nurse_ids(nurses_list)
    excluded_ids = _excluded_nurse_ids_from_assignments(assignments if isinstance(assignments, dict) else {})
    monthly_joiners = _monthly_joiner_ids(nurses_list, year, month)
    preceptee_ids = _preceptee_ids(nurses_list)
    eval_ids = active_ids - excluded_ids - monthly_joiners - preceptee_ids
    limit_items = (monthly_limits or {}).get("items") if isinstance(monthly_limits, dict) else []
    o_exact_map = _build_monthly_limit_exact_map(limit_items or [])

    # evaluate off metrics on first successful run roster as representative snapshot
    off_band_cnt = 0
    off_cap_mismatch = 0
    role_null_active_count = 0
    preceptee_sync_viol = 0
    preceptee_mapping_invalid = _preceptee_mapping_invalid_count(nurses_payload if isinstance(nurses_payload, list) else [])
    grade_minmax_viol = 0
    weekly_off_missing_count = 0
    wanted_apply_ratio = 1.0
    wanted_submission_ratio = 1.0
    wanted_fixed_apply_ratio = 1.0
    fixed_entries_count = 0
    preceptee_pair_shift_concentration_ratio = 0.0
    offswap_trace_missing_count = 0
    fairness_den_spread_ratio = 0.0
    fairness_total_work_spread = 0.0
    fairness_n_shift_skew_ratio = 0.0
    prev_transition_violation_count = 0
    prev_n_recovery_violation_count = 0
    prev_conseq_work_overflow_count = 0
    fixed_changed_cell_count = 0
    fixed_n_before_off_violation_count = 0
    drilldown: Dict[str, Any] = {
        "coverage_under_cells": [],
        "coverage_over_cells": [],
        "off_band_nurses": [],
        "weekly_off_missing_nurses": [],
        "role_null_nurses": [],
        "fairness_total_work_extremes": {},
        "prev_transition_violations": [],
        "prev_conseq_work_violations": [],
        "ci_violated_node_ids": [],
        "ci_preflight_reason_codes": [],
    }

    nurse_team_map = {
        str(n.get("nurse_id") or ""): str(n.get("team_id") or "")
        for n in (nurses_payload if isinstance(nurses_payload, list) else [])
        if str(n.get("nurse_id") or "")
    }

    for n in (nurses_payload if isinstance(nurses_payload, list) else []):
        act = str(n.get("active", 1))
        role = str(n.get("role") or "").strip()
        nid = str(n.get("nurse_id") or "")
        if act in ("1", "True", "true") and role == "":
            role_null_active_count += 1
            if nid:
                drilldown["role_null_nurses"].append({"nurse_id": nid, "team_id": nurse_team_map.get(nid)})

    first_ok = next((r for r in runs if r.status_code == 200 and r.schedule_id), None)
    if first_ok is not None:
        roster = _get_json(s, args.base_url, f"/roster/schedule/{first_ok.schedule_id}")
        drilldown["coverage_under_cells"] = _coverage_under_cells(roster, day_need, shift_main_map)
        drilldown["coverage_over_cells"] = _coverage_over_cells(roster, day_need, shift_main_map)
        off_counts = _roster_off_counts(roster, shift_main_map)
        off_days_cfg = float(cfg.get("off_days") or 0)
        for nid in eval_ids:
            actual = int(off_counts.get(nid, 0))
            if not (math.floor(off_days_cfg - 1) <= actual <= math.ceil(off_days_cfg + 1)):
                off_band_cnt += 1
                drilldown["off_band_nurses"].append(
                    {
                        "nurse_id": nid,
                        "team_id": nurse_team_map.get(nid),
                        "off_days": actual,
                        "off_cfg": off_days_cfg,
                    }
                )
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
            drilldown["ci_violated_node_ids"] = [str(v.get("node_id") or "") for v in (ci.get("violated_constraints") or [])]
            drilldown["ci_preflight_reason_codes"] = [
                str(x.get("reason_code") or "") for x in (ci.get("preflight_alerts") or []) if x.get("reason_code")
            ]
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

        weekly_off_missing_count = _weekly_off_missing_count(weekly_off if isinstance(weekly_off, dict) else {})
        weekly_missing_ids = _weekly_off_missing_nurse_ids(weekly_off if isinstance(weekly_off, dict) else {})
        drilldown["weekly_off_missing_nurses"] = [
            {"nurse_id": nid, "team_id": nurse_team_map.get(nid)} for nid in weekly_missing_ids
        ]
        fixed_entries_count = len(((fixed or {}).get("entries") or [])) if isinstance(fixed, dict) else 0
        wanted_fixed_apply_ratio = _wanted_apply_ratio_from_fixed(roster, fixed if isinstance(fixed, dict) else {}, shift_main_map)
        wanted_submission_ratio = _wanted_submission_ratio(wanted_submissions if isinstance(wanted_submissions, dict) else {}, eval_ids)
        w_status = str((wanted_status or {}).get("status") or "").lower()
        if fixed_entries_count > 0:
            wanted_apply_ratio = wanted_fixed_apply_ratio
        else:
            wanted_apply_ratio = wanted_fixed_apply_ratio if w_status in ("closed", "applied", "done") else wanted_submission_ratio
        preceptee_pair_shift_concentration_ratio = _preceptee_pair_shift_concentration_ratio(
            roster,
            nurses_payload if isinstance(nurses_payload, list) else [],
            shift_main_map,
        )
        fairness = _fairness_metrics(roster, shift_main_map, eval_ids)
        fairness_den_spread_ratio = float(fairness.get("fairness.den_spread_ratio", 0.0))
        fairness_total_work_spread = float(fairness.get("fairness.total_work_spread", 0.0))
        fairness_n_shift_skew_ratio = float(fairness.get("fairness.n_shift_skew_ratio", 0.0))
        fx = _fairness_total_work_extremes(roster, shift_main_map, eval_ids)
        drilldown["fairness_total_work_extremes"] = {
            **fx,
            "min_team_id": nurse_team_map.get(str(fx.get("min_nurse_id") or "")),
            "max_team_id": nurse_team_map.get(str(fx.get("max_nurse_id") or "")),
        }
        # 로그 파이프라인 미연동인 환경에서는 offswap 자체 비활성 여부로 최소 점검
        offswap_enabled = bool(cfg.get("off_swap_enabled", False))
        has_target = any(int((row.get("off_swap_target") or 0)) == 1 for row in (shifts if isinstance(shifts, list) else []))
        ci_blob = (r0[1].get("constraint_impact") or {}) if (r0[0] == 200 and isinstance(r0[1], dict)) else {}
        offswap_signal = _contains_offswap_signal(ci_blob)
        # offswap가 켜져 있는데 target/event 신호가 전혀 없으면 trace 누락으로 본다.
        offswap_trace_missing_count = 1 if (offswap_enabled and (not has_target or not offswap_signal)) else 0

    metrics.update(
        {
            "off.off_days_out_of_band_count": off_band_cnt,
            "off.off_cap_exact_mismatch_count": off_cap_mismatch,
            "off.weekly_off_missing_count": weekly_off_missing_count,
            "fairness.den_spread_ratio": fairness_den_spread_ratio,
            "fairness.total_work_spread": fairness_total_work_spread,
            "fairness.n_shift_skew_ratio": fairness_n_shift_skew_ratio,
            "data.role_null_active_count": role_null_active_count,
            "preceptee.sync_violation_count": preceptee_sync_viol,
            "preceptee.pair_shift_concentration_ratio": preceptee_pair_shift_concentration_ratio,
            "preceptee.mapping_invalid_count": preceptee_mapping_invalid,
            "grade.minmax_violation_count": grade_minmax_viol,
            "carryover.prev_transition_violation_count": prev_transition_violation_count,
            "carryover.prev_n_recovery_violation_count": prev_n_recovery_violation_count,
            "carryover.prev_conseq_work_overflow_count": prev_conseq_work_overflow_count,
            "carryover.dropped_ref_count": 1 if str((prev_tail or {}).get("data", {}).get("schedule_status") or "").lower() == "dropped" else 0,
            "config.max_enabled_inconsistency_count": _max_enabled_inconsistency_count(daily if isinstance(daily, dict) else {}),
            "fixed.changed_cell_count": fixed_changed_cell_count,
            "fixed.n_before_fixed_off_violation_count": fixed_n_before_off_violation_count,
            "wanted.apply_ratio": wanted_apply_ratio,
            "wanted.submission_ratio": wanted_submission_ratio,
            "wanted.fixed_apply_ratio": wanted_fixed_apply_ratio,
            "wanted.fixed_entries_count": fixed_entries_count,
            "offswap.target_shift_multi_count": _offswap_target_multi_count(shifts if isinstance(shifts, list) else []),
            # TODO: exact offswap conversion metrics require conversion trace / annual-leave delta source
            "offswap.recovery_off_converted_count": 0,
            "offswap.night_only_off_converted_count": 0,
            "offswap.fixed_off_converted_count": 0,
            "offswap.ju_converted_count": 0,
            "logs.offswap_trace_missing_count": offswap_trace_missing_count,
        }
    )

    # evaluate rules
    rule_results = []
    fairness_mode = str(os.getenv("HARNESS_FAIRNESS_MODE", "warning")).strip().lower()
    for rule in rules:
        rid = rule.get("id")
        metric_name = rule.get("metric")
        cond = str(rule.get("pass_condition", "== 0"))
        severity = rule.get("severity", "warning")
        if fairness_mode == "blocking" and str(rid).startswith("C_"):
            severity = "blocking"
        # Guardrail: when fixed wanted exists, wanted apply must be exact.
        if rid == "E_WANTED_APPLY" and int(metrics.get("wanted.fixed_entries_count") or 0) > 0:
            cond = ">= 1.0"
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
                "fixed_entries_count": int(metrics.get("wanted.fixed_entries_count") or 0) if rid == "E_WANTED_APPLY" else 0,
                "wanted_submission_ratio": float(metrics.get("wanted.submission_ratio") or 0.0) if rid == "E_WANTED_APPLY" else 0.0,
                "wanted_apply_ratio": float(metrics.get("wanted.apply_ratio") or 0.0) if rid == "E_WANTED_APPLY" else 0.0,
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

    run_id = f"{year}-{month:02d}-{group_id}-{args.strategy}-{ts}"
    solver_status_agg = "unknown"
    for r in runs:
        if r.solver_status:
            solver_status_agg = str(r.solver_status)
            if solver_status_agg.lower() == "primary":
                break
    graph_export = _build_graph_export(
        run_id=run_id,
        group_id=group_id,
        year=year,
        month=month,
        strategy=args.strategy,
        input_hash=input_hash,
        rule_results=rule_results,
        rules_catalog=rules,
        drilldown=drilldown,
        runs=runs,
        solver_status=solver_status_agg,
    )
    summary["graph_consistency"] = graph_export.get("consistency", {})
    summary["mapping_summary"] = graph_export.get("mapping_summary", {})

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
