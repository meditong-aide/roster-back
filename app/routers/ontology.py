"""Ontology hypergraph inspector router.

Reads harness reports (`tools/harness/reports/run-*/graph_export.json`) and
exposes a graph-DB-style explorer at `/ontology`:

- GET /ontology              → interactive HTML (cytoscape + side panel)
- GET /ontology/runs         → run metadata list
- GET /ontology/graph        → merged graph across filtered runs
- GET /ontology/node/{id}    → single node detail (attrs + run incidences)
- GET /ontology/rules        → per-rule aggregate (fail rate, recent values)

Independent from the main agent / roster flow — purely for inspecting
constraint check results.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse


router = APIRouter(prefix="/ontology", tags=["ontology"])


# ── Rule catalog (human-readable explanation per rule_id) ──
#
# 출처: docs/ONTOLOGY_RULE_GRANULARITY_SPEC.md §3 + checklist_core.yaml.
# "위반 시 어떤 의미인가"를 사용자가 직관적으로 읽을 수 있도록 짧게 정리.

RULE_CATALOG: dict[str, dict[str, str]] = {
    # Group A — Hard transition/recovery
    "A_1N_SINGLE":        {"group": "A", "title": "N 단독 금지",        "what": "야간(N)이 단 하루만 끼이는 고립 패턴",            "why": "야간 1일은 회복 부담이 큼"},
    "A_2N_2OFF":          {"group": "A", "title": "2N 후 2OFF 회복",    "what": "N 2연속 뒤 OFF 2일 회복 누락",                   "why": "야간 회복 시간 보장"},
    "A_3N_2OFF":          {"group": "A", "title": "3N 후 2OFF 회복",    "what": "N 3연속 뒤 OFF 2일 회복 누락",                   "why": "야간 회복 시간 보장"},
    "A_4N_MAX":           {"group": "A", "title": "N 4연속 금지",       "what": "야간(N) 4일 연속",                               "why": "야간 누적 피로 한계"},
    "A_MAX_CONSEQ_WORK":  {"group": "A", "title": "최대 연속근무 초과", "what": "설정된 최대 연속 근무일 초과",                  "why": "근로기준 가이드"},
    "A_NOD":              {"group": "A", "title": "N→OFF→D 금지",       "what": "야간 다음날 OFF 후 데이 전환",                   "why": "역방향 회복 부족"},
    "A_NOE":              {"group": "A", "title": "N→OFF→E 금지",       "what": "야간 다음날 OFF 후 이브닝 전환",                 "why": "역방향 회복 부족"},
    "A_EOD":              {"group": "A", "title": "E→OFF→D 금지",       "what": "이브닝 다음날 OFF 후 데이 전환",                 "why": "이브닝→데이 짧은 회복"},
    "A_MONTHLY_N_CAP":    {"group": "A", "title": "월간 N 상한 초과",   "what": "월 N 시프트 상한 초과",                          "why": "야간 누적 부담"},

    # Group B — OFF / 휴무
    "B_OFF_NEAR_CONFIG":      {"group": "B", "title": "월 OFF 권장 밴드 이탈", "what": "월간 OFF 일수가 권장 범위를 벗어난 간호사 수", "why": "휴식권 보장"},
    "B_OFF_CAP_EXACT":        {"group": "B", "title": "월 OFF 정확치 불일치", "what": "OFF cap이 정확값과 다른 간호사 수",            "why": "정원·휴무 정합성"},
    "B_WEEKLY_OFF":           {"group": "B", "title": "주휴 누락",            "what": "주 1회 OFF가 보장되지 않은 셀",               "why": "주 단위 휴식권"},
    "B_OFF_SWAP_RECOVERY":    {"group": "B", "title": "회복 OFF 변환",        "what": "회복용 OFF가 다른 시프트로 변환",             "why": "회복 OFF는 swap 불가"},
    "B_OFF_SWAP_N_ONLY":      {"group": "B", "title": "N-only OFF 변환",      "what": "N 전용 OFF가 변환",                          "why": "야간 후속 OFF 보호"},
    "B_OFF_SWAP_FIXED":       {"group": "B", "title": "고정 OFF 변환",        "what": "fixed OFF가 변환됨",                         "why": "고정 OFF는 immutable"},
    "B_OFF_SWAP_JU":          {"group": "B", "title": "주휴 OFF 변환",        "what": "주휴 OFF가 변환됨",                          "why": "주휴는 보호"},
    "B_OFF_SWAP_TARGET_SINGLE":{"group":"B", "title": "OFF swap target 다중", "what": "target_shift 설정이 다중으로 잡힘",          "why": "swap 대상은 단일이어야"},

    # Group C — Fairness
    "C_DEN_BALANCE":   {"group": "C", "title": "D/E/N 분포 공정성", "what": "D/E/N 분포가 nurse 간 spread 임계 초과", "why": "특정 시프트 편중 방지"},
    "C_TOTAL_BALANCE": {"group": "C", "title": "총 근무일 공정성", "what": "총 근무일수 편차(최대-최소)가 임계 초과",  "why": "전체 부하 균형"},
    "C_N_SKEW":        {"group": "C", "title": "N 시프트 편중",    "what": "야간 시프트가 특정 nurse에 쏠림",         "why": "야간 부담 분산"},

    # Group D — Coverage
    "D_D_MIN": {"group": "D", "title": "D 최소 인원 미달", "what": "day×D에서 최소 정원 미달",          "why": "데이 인력 부족"},
    "D_E_MIN": {"group": "D", "title": "E 최소 인원 미달", "what": "day×E에서 최소 정원 미달",          "why": "이브닝 인력 부족"},
    "D_N_MIN": {"group": "D", "title": "N 최소 인원 미달", "what": "day×N에서 최소 정원 미달",          "why": "야간 인력 부족"},
    "D_M_MIN": {"group": "D", "title": "M 최소 인원 미달", "what": "day×M에서 최소 정원 미달",          "why": "미드 인력 부족"},
    "D_MAX_OVER":             {"group": "D", "title": "최대 인원 초과 셀",       "what": "day×shift에서 최대 정원 초과한 셀 수",        "why": "과배치 방지"},
    "D_MAX_ENABLED_INTEGRITY":{"group": "D", "title": "max_enabled 정합성",      "what": "max_enabled 설정이 일관되지 않음",            "why": "설정 정합성"},

    # Group E — Wanted / Fixed
    "E_WANTED_APPLY":          {"group": "E", "title": "원티드 반영률",        "what": "수간호사 승인 원티드 중 실제 반영 비율",        "why": "원티드 시스템 신뢰"},
    "E_FIXED_LOCK":            {"group": "E", "title": "고정 셀 변경",         "what": "fixed 셀이 다른 값으로 바뀜",                  "why": "고정은 immutable"},
    "E_BAN_N_BEFORE_FIXED_OFF":{"group": "E", "title": "fixed OFF 전 N 금지",  "what": "fixed OFF 직전날 N이 배치됨",                  "why": "OFF 전 야간 금지"},

    # Group F — Carryover
    "F_PREV_TRANSITION":  {"group": "F", "title": "전월 경계 전이 위반",  "what": "전월 마지막 시프트와의 전이 규칙 위반",         "why": "월 경계 회복"},
    "F_PREV_CONSEQ_WORK": {"group": "F", "title": "전월 연속근무 초과",   "what": "전월부터 이어진 연속근무가 한도 초과",          "why": "월경계 회복"},
    "F_PREV_N_RECOVERY":  {"group": "F", "title": "전월 N 회복 미달",     "what": "전월 야간 후 회복 OFF 누락",                   "why": "월 경계 회복"},
    "F_DROPPED_FILTER":   {"group": "F", "title": "carryover ref 누락",   "what": "carryover 참조가 dropping됨",                  "why": "데이터 정합성"},

    # Group G — 특수
    "G_PRECEPTEE_SYNC":         {"group": "G", "title": "프리셉티 동반 미스", "what": "프리셉티-프리셉터가 같은 시프트에 동반되지 않음", "why": "교육 동반 원칙"},
    "G_PRECEPTEE_PAIR_SPREAD":  {"group": "G", "title": "프리셉티 페어 산포 편중", "what": "프리셉티 페어가 특정 시프트에 집중",          "why": "교육 균등"},
    "G_PRECEPTEE_MAPPING":      {"group": "G", "title": "프리셉티 매핑 무효", "what": "프리셉티 매핑이 유효하지 않음",                 "why": "데이터 검증"},
    "G_ROLE_NULL":              {"group": "G", "title": "역할 미지정 활성 간호사", "what": "active 간호사 중 role이 NULL",              "why": "데이터 정합성"},
    "G_GRADE_CONSTRAINT":       {"group": "G", "title": "grade min/max 위반", "what": "셀별 grade 분포가 min/max 범위를 벗어남",        "why": "숙련도 분포"},

    # Group H — 시스템
    "H_NO_INFEASIBLE": {"group": "H", "title": "infeasible 발생", "what": "solver가 infeasible로 종료된 run",  "why": "스케줄러 안정성"},
    "H_FALLBACK_OK":   {"group": "H", "title": "fallback 에러",   "what": "fallback 경로에서 에러 발생",       "why": "안정성"},
    "H_RUNTIME":       {"group": "H", "title": "런타임 초과",     "what": "solver p95 런타임이 임계 초과",     "why": "응답성"},
    "H_OFF_SWAP_LOG":  {"group": "H", "title": "OFF swap 추적 누락","what": "OFF swap 흔적이 로그에 없음",       "why": "추적성"},
}


@router.get("/rule_catalog")
def rule_catalog() -> JSONResponse:
    return JSONResponse(RULE_CATALOG)


# ── Reports loader (cached by mtime) ───────────────────────


_REPORTS_DIR = (
    Path(__file__).resolve().parents[2] / "tools" / "harness" / "reports"
)
_cache: dict[str, Any] = {"sig": None, "runs": []}


def _scan_runs() -> list[dict[str, Any]]:
    """Load every run-*/graph_export.json. Caches by (dir mtime, file count)."""
    if not _REPORTS_DIR.exists():
        return []
    run_dirs = sorted(p for p in _REPORTS_DIR.glob("run-*") if p.is_dir())
    sig = (
        _REPORTS_DIR.stat().st_mtime_ns,
        len(run_dirs),
        tuple((p.name, p.stat().st_mtime_ns) for p in run_dirs),
    )
    if _cache["sig"] == sig:
        return _cache["runs"]

    runs: list[dict[str, Any]] = []
    for d in run_dirs:
        ge = d / "graph_export.json"
        if not ge.exists():
            continue
        try:
            data = json.loads(ge.read_text(encoding="utf-8"))
        except Exception:
            continue
        summary_path = d / "summary.json"
        summary = None
        if summary_path.exists():
            try:
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
            except Exception:
                summary = None
        rr_path = d / "run_result.json"
        run_result = None
        if rr_path.exists():
            try:
                run_result = json.loads(rr_path.read_text(encoding="utf-8"))
            except Exception:
                run_result = None
        runs.append({"dir": d.name, "data": data, "summary": summary, "run_result": run_result})

    _cache["sig"] = sig
    _cache["runs"] = runs
    return runs


def _month_key(year: Any, month: Any) -> str | None:
    try:
        return f"{int(year):04d}-{int(month):02d}"
    except Exception:
        return None


def _run_matches(
    run: dict[str, Any],
    months: set[str],
    groups: set[str],
    strategies: set[str],
    solver_statuses: set[str] | None = None,
) -> bool:
    meta = run["data"].get("run") or {}
    mk = _month_key(meta.get("year"), meta.get("month"))
    if months and mk not in months:
        return False
    if groups and str(meta.get("group_id")) not in groups:
        return False
    if strategies and str(meta.get("strategy")) not in strategies:
        return False
    if solver_statuses:
        attempts = ((run.get("run_result") or {}).get("runs")) or []
        seen = {str(a.get("solver_status")) for a in attempts}
        if not (seen & solver_statuses):
            return False
    return True


def _run_summary(run: dict[str, Any]) -> dict[str, Any]:
    """Extract per-run enrichment info from run_result.json."""
    rr = run.get("run_result") or {}
    attempts = rr.get("runs") or []
    statuses = [str(a.get("solver_status")) for a in attempts if a.get("solver_status") is not None]
    status_dist: dict[str, int] = defaultdict(int)
    for s in statuses:
        status_dist[s] += 1
    schedule_ids = [a.get("schedule_id") for a in attempts if a.get("schedule_id")]
    over_cells = (rr.get("drilldown") or {}).get("coverage_over_cells") or []
    under_cells = (rr.get("drilldown") or {}).get("coverage_under_cells") or []
    infeasible_details = [a.get("infeasible_detail") for a in attempts if a.get("infeasible_detail")]
    return {
        "schedule_ids": schedule_ids,
        "schedule_id": schedule_ids[0] if schedule_ids else None,
        "solver_status_dist": dict(status_dist),
        "attempts": len(attempts),
        "fallback_used": any(s != "primary" for s in statuses),
        "infeasible_detail_count": len(infeasible_details),
        "coverage_over_cells_count": len(over_cells),
        "coverage_under_cells_count": len(under_cells),
    }


# ── Endpoints ──────────────────────────────────────────────


@router.get("/runs")
def list_runs() -> JSONResponse:
    runs = _scan_runs()
    rows = []
    for r in runs:
        meta = r["data"].get("run") or {}
        sm = r.get("summary") or {}
        rows.append(
            {
                "dir": r["dir"],
                "run_id": meta.get("run_id"),
                "group_id": meta.get("group_id"),
                "year": meta.get("year"),
                "month": meta.get("month"),
                "month_key": _month_key(meta.get("year"), meta.get("month")),
                "strategy": meta.get("strategy"),
                "input_hash": meta.get("input_hash"),
                "status": sm.get("status"),
                "blocking_fail_count": sm.get("blocking_fail_count"),
                "warning_fail_count": sm.get("warning_fail_count"),
                "rules_total": sm.get("rules_total"),
                "violation_count": len(r["data"].get("violations") or []),
            }
        )

    months: dict[str, int] = defaultdict(int)
    groups: dict[str, int] = defaultdict(int)
    strategies: dict[str, int] = defaultdict(int)
    solver_statuses: dict[str, int] = defaultdict(int)
    for r, row in zip(runs, rows):
        if row["month_key"]:
            months[row["month_key"]] += 1
        if row["group_id"]:
            groups[str(row["group_id"])] += 1
        if row["strategy"]:
            strategies[str(row["strategy"])] += 1
        summary = _run_summary(r)
        row["schedule_id"] = summary["schedule_id"]
        row["solver_status_dist"] = summary["solver_status_dist"]
        row["fallback_used"] = summary["fallback_used"]
        for s in summary["solver_status_dist"]:
            solver_statuses[s] += summary["solver_status_dist"][s]

    return JSONResponse(
        {
            "runs": rows,
            "facets": {
                "months": sorted(months.items()),
                "groups": sorted(groups.items()),
                "strategies": sorted(strategies.items()),
                "solver_statuses": sorted(solver_statuses.items()),
            },
        }
    )


@router.get("/graph")
def merged_graph(
    months: str = Query("", description="CSV, e.g. 2026-04,2026-05"),
    groups: str = Query("", description="CSV of group_ids"),
    strategies: str = Query("", description="CSV of strategies"),
    rules: str = Query("", description="CSV of rule_ids"),
    status: str = Query("ALL", description="ALL|FAIL|PASS"),
    level: str = Query("full", description="full|rule|run|month (legacy preset)"),
    layers: str = Query("", description="CSV of layers to show: month,group,run,rule,violation,metric,cause"),
    solver_statuses: str = Query("", description="CSV of solver_status (primary, fallback, ...)"),
    severities: str = Query("", description="CSV of severities: blocking,warning"),
) -> JSONResponse:
    runs = _scan_runs()
    mset = {s for s in months.split(",") if s}
    gset = {s for s in groups.split(",") if s}
    sset = {s for s in strategies.split(",") if s}
    rset = {s for s in rules.split(",") if s}
    solset = {s for s in solver_statuses.split(",") if s}

    selected = [r for r in runs if _run_matches(r, mset, gset, sset, solset)]
    run_summary_cache = {(r["data"].get("run") or {}).get("run_id"): _run_summary(r) for r in selected}
    sev_set = {s.strip().lower() for s in (severities or "").split(",") if s.strip()}

    nodes: dict[str, dict[str, Any]] = {}
    edges: dict[tuple[str, str, str], dict[str, Any]] = {}
    violations: list[dict[str, Any]] = []
    rule_stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"pass": 0, "fail": 0, "skipped": 0, "values": [], "severity": None}
    )

    for r in selected:
        d = r["data"]
        meta = d.get("run") or {}
        run_id = meta.get("run_id")
        mk = _month_key(meta.get("year"), meta.get("month"))
        group_id = meta.get("group_id")

        # Synthesize structural backbone if missing (early runs omit nodes/edges).
        synthetic_nodes: list[dict[str, Any]] = []
        synthetic_edges: list[dict[str, str]] = []
        existing_ids = {n.get("id") for n in (d.get("nodes") or [])}
        if mk and f"month:{mk}" not in existing_ids:
            synthetic_nodes.append(
                {"id": f"month:{mk}", "type": "MonthNode",
                 "attrs": {"year": meta.get("year"), "month": meta.get("month")}}
            )
        if group_id and f"group:{group_id}" not in existing_ids:
            synthetic_nodes.append(
                {"id": f"group:{group_id}", "type": "GroupNode",
                 "attrs": {"group_id": group_id}}
            )
        if run_id and f"run:{run_id}" not in existing_ids:
            synthetic_nodes.append(
                {"id": f"run:{run_id}", "type": "RunNode",
                 "attrs": {"run_id": run_id, "strategy": meta.get("strategy"),
                           "input_hash": meta.get("input_hash")}}
            )
        if run_id and mk:
            synthetic_edges.append({"type": "RUN_ON", "from": f"run:{run_id}", "to": f"month:{mk}"})
        if run_id and group_id:
            synthetic_edges.append({"type": "RUN_ON", "from": f"run:{run_id}", "to": f"group:{group_id}"})
        if mk and group_id:
            synthetic_edges.append({"type": "IN_GROUP", "from": f"month:{mk}", "to": f"group:{group_id}"})

        all_nodes = (d.get("nodes") or []) + synthetic_nodes
        all_edges = (d.get("edges") or []) + synthetic_edges

        for rule in d.get("rules") or []:
            rid = rule.get("rule_id")
            if not rid:
                continue
            stat = (rule.get("status") or "").upper()
            if stat == "PASS":
                rule_stats[rid]["pass"] += 1
            elif stat == "FAIL":
                rule_stats[rid]["fail"] += 1
            else:
                rule_stats[rid]["skipped"] += 1
            rule_stats[rid]["severity"] = rule.get("severity")
            rule_stats[rid]["values"].append(
                {"run_id": run_id, "value": rule.get("value"), "status": stat}
            )

        for n in all_nodes:
            nid = n.get("id")
            if not nid:
                continue
            if nid not in nodes:
                merged = {
                    "id": nid,
                    "type": n.get("type"),
                    "attrs": dict(n.get("attrs") or {}),
                    "_seen_in_runs": [],
                    "_months": [],
                    "_groups": [],
                }
                nodes[nid] = merged
            entry = nodes[nid]
            # 같은 id의 attrs를 union — 새 attrs에 있는 key가 기존에 없으면 채움.
            # (cross-run 누적 시 최신 run의 풍부한 메타가 표면화되도록.)
            new_attrs = n.get("attrs") or {}
            for k, v in new_attrs.items():
                cur = entry["attrs"].get(k)
                if cur is None or cur == "" or cur == []:
                    entry["attrs"][k] = v
            if run_id and run_id not in entry["_seen_in_runs"]:
                entry["_seen_in_runs"].append(run_id)
            if mk and mk not in entry["_months"]:
                entry["_months"].append(mk)
            if group_id and group_id not in entry["_groups"]:
                entry["_groups"].append(group_id)

        for e in all_edges:
            key = (e.get("type"), e.get("from"), e.get("to"))
            if None in key:
                continue
            if key not in edges:
                edges[key] = {"type": e.get("type"), "from": e.get("from"), "to": e.get("to")}

        for v in d.get("violations") or []:
            violations.append(
                {
                    **v,
                    "run_id": run_id,
                    "month_key": mk,
                    "group_id": meta.get("group_id"),
                    "strategy": meta.get("strategy"),
                }
            )

    # Rule filter — restrict to specified rule_ids (keeps related metric/violation chain).
    if rset:
        keep_ids: set[str] = set()
        for nid, n in nodes.items():
            if n["type"] == "RuleNode" and n["attrs"].get("rule_id") in rset:
                keep_ids.add(nid)
        for v in violations:
            if v.get("rule_id") in rset:
                for nid in v.get("node_ids") or []:
                    keep_ids.add(nid)
                keep_ids.add(v.get("violation_id"))
        # plus the structural backbone (Group/Month/Run nodes) — context.
        for nid, n in nodes.items():
            if n["type"] in {"GroupNode", "MonthNode", "RunNode"}:
                keep_ids.add(nid)
        nodes = {nid: n for nid, n in nodes.items() if nid in keep_ids}
        violations = [v for v in violations if v.get("rule_id") in rset]

    # Status filter — only show rules (and their chain) of given status.
    if status.upper() == "FAIL":
        fail_rule_ids = {rid for rid, s in rule_stats.items() if s["fail"] > 0}
        keep_ids = set()
        for nid, n in nodes.items():
            if n["type"] == "RuleNode":
                if n["attrs"].get("rule_id") in fail_rule_ids:
                    keep_ids.add(nid)
            elif n["type"] in {"GroupNode", "MonthNode", "RunNode"}:
                keep_ids.add(nid)
        # add violations + their chain
        for v in violations:
            if v.get("rule_id") in fail_rule_ids:
                for nid in v.get("node_ids") or []:
                    keep_ids.add(nid)
                keep_ids.add(v.get("violation_id"))
        nodes = {nid: n for nid, n in nodes.items() if nid in keep_ids}
        violations = [v for v in violations if v.get("rule_id") in fail_rule_ids]
    elif status.upper() == "PASS":
        fail_rule_ids = {rid for rid, s in rule_stats.items() if s["fail"] > 0}
        # keep non-failing rules
        nodes = {
            nid: n
            for nid, n in nodes.items()
            if not (n["type"] == "RuleNode" and n["attrs"].get("rule_id") in fail_rule_ids)
        }

    # Layer filter — explicit list of layers wins; otherwise fall back to legacy `level`.
    LAYER_TO_TYPES = {
        "month": {"MonthNode"},
        "group": {"GroupNode"},
        "run": {"RunNode"},
        "rule": {"RuleNode"},
        "violation": {"ViolationNode"},
        "metric": {"MetricNode"},
        "cause": {
            "CoverageMinNode", "CoverageMaxNode",
            "TeamMinNode", "TeamMaxNode",
            "GradeMinNode", "GradeMaxNode",
            "MonthlyOffNode", "WeeklyOffNode", "OffWindowNode",
            "CarryoverTransitionNode", "PrecepteeSyncNode",
            "WantedSubmissionNode", "WantedApplyNode",
            "NurseNode", "ShiftNode", "DayNode", "TeamNode",
            "FairnessNode", "DataQualityNode", "ConstraintNode",
            "ConflictCoreNode", "NurseRoleNode", "OffCapNode",
            "NightCapNode", "MonthlyNExactNode",
        },
    }
    layer_set = {s.strip() for s in (layers or "").split(",") if s.strip()}
    if layer_set:
        keep_types: set[str] = set()
        for lyr in layer_set:
            keep_types |= LAYER_TO_TYPES.get(lyr, set())
        nodes = {nid: n for nid, n in nodes.items() if n["type"] in keep_types}
    else:
        # legacy preset
        level = (level or "full").lower()
        if level == "month":
            keep_types = {"MonthNode", "GroupNode", "RuleNode"}
            nodes = {nid: n for nid, n in nodes.items() if n["type"] in keep_types}
        elif level == "run":
            keep_types = {"MonthNode", "GroupNode", "RunNode", "RuleNode"}
            nodes = {nid: n for nid, n in nodes.items() if n["type"] in keep_types}
        elif level == "rule":
            keep_types = {"MonthNode", "GroupNode", "RunNode", "RuleNode", "ViolationNode"}
            nodes = {nid: n for nid, n in nodes.items() if n["type"] in keep_types}
        # else "full" → no collapse

    # Severity filter — restrict rules/violations to selected severity buckets.
    if sev_set:
        rule_severity = {rid: str(s.get("severity") or "").lower() for rid, s in rule_stats.items()}
        nodes = {
            nid: n for nid, n in nodes.items()
            if n["type"] not in {"RuleNode", "ViolationNode"}
            or str(
                n["attrs"].get("severity")
                or rule_severity.get(n["attrs"].get("rule_id"), "")
                or ""
            ).lower() in sev_set
        }
        violations = [
            v for v in violations
            if rule_severity.get(v.get("rule_id"), "") in sev_set
        ]

    # Enrich RuleNode with aggregated stats.
    for n in nodes.values():
        if n["type"] == "RuleNode":
            rid = n["attrs"].get("rule_id")
            stats = rule_stats.get(rid)
            if stats:
                n["attrs"]["_pass_count"] = stats["pass"]
                n["attrs"]["_fail_count"] = stats["fail"]
                n["attrs"]["_skipped_count"] = stats["skipped"]
                total = stats["pass"] + stats["fail"] + stats["skipped"]
                n["attrs"]["_runs_total"] = total
                n["attrs"]["_fail_ratio"] = (
                    round(stats["fail"] / total, 3) if total else None
                )
        elif n["type"] == "RunNode":
            rid = n["attrs"].get("run_id")
            rs = run_summary_cache.get(rid)
            if rs:
                n["attrs"]["_schedule_id"] = rs["schedule_id"]
                n["attrs"]["_solver_status_dist"] = rs["solver_status_dist"]
                n["attrs"]["_fallback_used"] = rs["fallback_used"]
                n["attrs"]["_attempts"] = rs["attempts"]
                n["attrs"]["_coverage_over_cells"] = rs["coverage_over_cells_count"]
                n["attrs"]["_coverage_under_cells"] = rs["coverage_under_cells_count"]

    # Edge pruning to surviving nodes.
    pruned_edges = [
        e for e in edges.values() if e["from"] in nodes and e["to"] in nodes
    ]

    return JSONResponse(
        {
            "nodes": list(nodes.values()),
            "edges": pruned_edges,
            "violations": violations,
            "stats": {
                "runs_in_view": len(selected),
                "node_count": len(nodes),
                "edge_count": len(pruned_edges),
                "violation_count": len(violations),
            },
        }
    )


@router.get("/rules")
def rules_aggregate() -> JSONResponse:
    """Per-rule aggregate stats across ALL runs (unfiltered)."""
    runs = _scan_runs()
    stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "pass": 0,
            "fail": 0,
            "skipped": 0,
            "severity": None,
            "metric": None,
            "pass_condition": None,
            "recent": [],
        }
    )
    for r in runs:
        sm = r.get("summary") or {}
        meta = r["data"].get("run") or {}
        for rule in sm.get("rules") or r["data"].get("rules") or []:
            rid = rule.get("rule_id")
            if not rid:
                continue
            stat = (rule.get("status") or "").upper()
            if stat == "PASS":
                stats[rid]["pass"] += 1
            elif stat == "FAIL":
                stats[rid]["fail"] += 1
            else:
                stats[rid]["skipped"] += 1
            stats[rid]["severity"] = rule.get("severity") or stats[rid]["severity"]
            stats[rid]["metric"] = rule.get("metric") or stats[rid]["metric"]
            stats[rid]["pass_condition"] = (
                rule.get("pass_condition") or stats[rid]["pass_condition"]
            )
            stats[rid]["recent"].append(
                {
                    "run_id": meta.get("run_id"),
                    "month_key": _month_key(meta.get("year"), meta.get("month")),
                    "status": stat,
                    "value": rule.get("value"),
                }
            )

    rows = []
    for rid, s in stats.items():
        total = s["pass"] + s["fail"] + s["skipped"]
        rows.append(
            {
                "rule_id": rid,
                "severity": s["severity"],
                "metric": s["metric"],
                "pass_condition": s["pass_condition"],
                "pass": s["pass"],
                "fail": s["fail"],
                "skipped": s["skipped"],
                "total": total,
                "fail_ratio": round(s["fail"] / total, 3) if total else None,
                "recent": s["recent"][-10:],
            }
        )
    rows.sort(key=lambda x: (-(x["fail_ratio"] or 0), x["rule_id"]))
    return JSONResponse({"rules": rows})


@router.get("/node/{node_id:path}")
def node_detail(node_id: str) -> JSONResponse:
    runs = _scan_runs()
    incidences: list[dict[str, Any]] = []
    base_node: dict[str, Any] | None = None
    related_violations: list[dict[str, Any]] = []
    metric_history: list[dict[str, Any]] = []
    inbound: dict[str, dict[str, Any]] = {}   # node_id -> {type, attrs, edge_types: set}
    outbound: dict[str, dict[str, Any]] = {}
    # For RunNode: full run_result drilldown
    run_drilldown: dict[str, Any] = {}

    for r in runs:
        d = r["data"]
        meta = d.get("run") or {}
        run_id = meta.get("run_id")

        # Build local id→node lookup for this run so we can resolve neighbor attrs.
        local_nodes = {n.get("id"): n for n in (d.get("nodes") or [])}

        for n in d.get("nodes") or []:
            if n.get("id") == node_id:
                if base_node is None:
                    base_node = {"id": n.get("id"), "type": n.get("type"), "attrs": dict(n.get("attrs") or {})}
                else:
                    # cross-run attrs union — 새 run이 더 풍부한 attrs 가지면 채움.
                    for k, v in (n.get("attrs") or {}).items():
                        cur = base_node["attrs"].get(k)
                        if cur is None or cur == "" or cur == []:
                            base_node["attrs"][k] = v
                incidences.append(
                    {
                        "run_id": run_id,
                        "month_key": _month_key(meta.get("year"), meta.get("month")),
                        "group_id": meta.get("group_id"),
                        "strategy": meta.get("strategy"),
                        "attrs": n.get("attrs") or {},
                    }
                )
                if n.get("type") == "MetricNode":
                    metric_history.append(
                        {
                            "run_id": run_id,
                            "month_key": _month_key(meta.get("year"), meta.get("month")),
                            "value": (n.get("attrs") or {}).get("value"),
                        }
                    )

        for e in d.get("edges") or []:
            if e.get("to") == node_id:
                src_id = e.get("from")
                src = local_nodes.get(src_id) or {"id": src_id}
                bucket = inbound.setdefault(src_id, {"id": src_id, "type": src.get("type"), "attrs": dict(src.get("attrs") or {}), "edge_types": set(), "seen_in_runs": []})
                bucket["edge_types"].add(e.get("type"))
                if run_id and run_id not in bucket["seen_in_runs"]:
                    bucket["seen_in_runs"].append(run_id)
            if e.get("from") == node_id:
                tgt_id = e.get("to")
                tgt = local_nodes.get(tgt_id) or {"id": tgt_id}
                bucket = outbound.setdefault(tgt_id, {"id": tgt_id, "type": tgt.get("type"), "attrs": dict(tgt.get("attrs") or {}), "edge_types": set(), "seen_in_runs": []})
                bucket["edge_types"].add(e.get("type"))
                if run_id and run_id not in bucket["seen_in_runs"]:
                    bucket["seen_in_runs"].append(run_id)

        for v in d.get("violations") or []:
            if node_id in (v.get("node_ids") or []) or v.get("violation_id") == node_id:
                related_violations.append(
                    {
                        **v,
                        "run_id": run_id,
                        "month_key": _month_key(meta.get("year"), meta.get("month")),
                        "group_id": meta.get("group_id"),
                    }
                )

        # RunNode drilldown — surface run_result.json content.
        if node_id == f"run:{run_id}":
            rr = r.get("run_result") or {}
            attempts = rr.get("runs") or []
            run_drilldown = {
                "attempts": attempts,
                "metrics": rr.get("metrics") or {},
                "drilldown": rr.get("drilldown") or {},
            }

    if base_node is None:
        raise HTTPException(status_code=404, detail=f"node not found: {node_id}")

    def _normalize(neighbors: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for v in neighbors.values():
            v["edge_types"] = sorted(v["edge_types"])
            out.append(v)
        out.sort(key=lambda x: (x.get("type") or "", x["id"]))
        return out

    return JSONResponse(
        {
            "node": base_node,
            "incidence_count": len(incidences),
            "incidences": incidences,
            "related_violations": related_violations,
            "metric_history": metric_history,
            "inbound_neighbors": _normalize(inbound),
            "outbound_neighbors": _normalize(outbound),
            "run_drilldown": run_drilldown if run_drilldown else None,
        }
    )


# ── HTML page ──────────────────────────────────────────────


_HTML = """<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <title>Ontology Inspector</title>
  <script src="https://unpkg.com/cytoscape@3.28.1/dist/cytoscape.min.js"></script>
  <style>
    :root {
      --bg:#0b1020; --bg2:#0d1428; --panel:#121a33; --panel2:#19234a;
      --border:#29304a; --hover:#27345d;
      --text:#e8ecf4; --muted:#9aa3b8; --accent:#60a5fa;
      --fail:#ef4444; --pass:#22c55e; --warn:#fbbf24;
      --cause:#fca5a5; --effect:#7dd3fc;
    }
    html,body { margin:0; height:100%; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif; background:var(--bg); color:var(--text); }
    #app { display:grid; grid-template-columns: 280px 1fr 400px; grid-template-rows: 56px 1fr; height:100vh; }
    /* ── Topbar ───────────────────────────── */
    #topbar { grid-column:1/4; padding:0 16px; border-bottom:1px solid var(--border); display:flex; gap:14px; align-items:center; background:var(--bg2); }
    #topbar h1 { font-size:15px; margin:0; font-weight:600; }
    #topbar h1 small { color:var(--muted); font-size:11px; font-weight:400; margin-left:6px; }
    .tb-group { display:flex; gap:0; align-items:center; }
    .tb-group .tb-label { font-size:10px; color:var(--muted); margin-right:6px; text-transform:uppercase; letter-spacing:.05em; }
    .tb-group .btn { border-radius:0; border-right-width:0; }
    .tb-group .btn:first-of-type { border-top-left-radius:6px; border-bottom-left-radius:6px; }
    .tb-group .btn:last-of-type { border-radius:0 6px 6px 0; border-right-width:1px; }
    #topbar .stats { color:var(--muted); font-size:11px; margin-left:auto; font-family:monospace; }
    .divider { width:1px; height:24px; background:var(--border); }
    /* ── Sidebar ──────────────────────────── */
    #sidebar { padding:0; border-right:1px solid var(--border); overflow:auto; background:var(--bg2); }
    #sidebar .help { background:var(--panel); border-bottom:1px solid var(--border); padding:10px 12px; color:var(--muted); font-size:11px; line-height:1.4; }
    #sidebar .help b { color:var(--text); }
    .sb-section { padding:10px 12px; border-bottom:1px solid var(--border); }
    .sb-section h2 { font-size:11px; text-transform:uppercase; color:var(--muted); margin:0 0 4px; letter-spacing:.05em; display:flex; align-items:center; gap:6px; }
    .sb-section h2 .hint-tip { color:#475569; font-size:10px; text-transform:none; font-weight:normal; letter-spacing:0; }
    #sidebar label { display:flex; align-items:center; gap:6px; font-size:12px; padding:3px 0; cursor:pointer; }
    #sidebar label:hover { color:var(--accent); }
    #sidebar input[type=text] { width:100%; background:var(--panel); color:var(--text); border:1px solid var(--border); border-radius:6px; padding:5px 8px; font-size:12px; box-sizing:border-box; }
    /* Node legend */
    .legend-grid { display:grid; grid-template-columns:1fr 1fr; gap:4px 8px; margin-top:4px; }
    .legend-item { display:flex; align-items:center; gap:5px; font-size:10px; color:#cbd5e1; }
    .legend-dot { width:10px; height:10px; border-radius:3px; border:1px solid #0b1020; flex-shrink:0; }
    /* ── Canvas overlay ─────────────────── */
    #canvas-wrap { position:relative; }
    #cy { width:100%; height:100%; background:var(--bg); }
    .canvas-hint { position:absolute; left:50%; top:14px; transform:translateX(-50%); background:rgba(18,26,51,.85); border:1px solid var(--border); border-radius:8px; padding:6px 14px; font-size:11px; color:var(--muted); pointer-events:none; }
    /* ── Detail panel ──────────────────── */
    #detail { padding:0; border-left:1px solid var(--border); overflow:auto; font-size:12px; background:var(--bg2); }
    .d-empty { padding:24px; color:var(--muted); font-size:13px; text-align:center; line-height:1.5; }
    .d-empty .icon { font-size:28px; margin-bottom:6px; }
    .d-section { padding:12px 14px; border-bottom:1px solid var(--border); }
    .d-section.cause { background:linear-gradient(180deg, rgba(252,165,165,.06), transparent); border-left:3px solid var(--cause); }
    .d-section.effect { background:linear-gradient(180deg, rgba(125,211,252,.06), transparent); border-left:3px solid var(--effect); }
    .d-section.viol { background:linear-gradient(180deg, rgba(239,68,68,.07), transparent); border-left:3px solid var(--fail); }
    .d-section.drilldown { background:linear-gradient(180deg, rgba(96,165,250,.06), transparent); border-left:3px solid var(--accent); }
    .d-section h3 { margin:0 0 4px; font-size:13px; display:flex; align-items:center; gap:6px; }
    .d-section .h-desc { display:block; color:var(--muted); font-size:10px; font-weight:normal; margin-top:1px; }
    #detail .badge { display:inline-block; padding:2px 7px; border-radius:999px; background:var(--panel); border:1px solid var(--border); font-size:10px; margin-right:4px; }
    #detail .kv { display:grid; grid-template-columns:auto 1fr; gap:4px 10px; margin:6px 0; }
    #detail .kv b { color:var(--muted); font-weight:500; }
    #detail pre { background:var(--panel); border:1px solid var(--border); border-radius:6px; padding:8px; max-width:100%; overflow:auto; font-size:11px; }
    .btn { background:var(--panel); color:var(--text); border:1px solid var(--border); border-radius:6px; padding:5px 9px; cursor:pointer; font-size:12px; }
    .btn:hover { background:var(--hover); }
    .btn.active { background:var(--accent); color:#0b1020; border-color:var(--accent); }
    .row { display:flex; gap:6px; flex-wrap:wrap; }
    .pill { padding:2px 8px; border-radius:999px; font-size:11px; }
    .pill.pass { background:rgba(34,197,94,.15); color:#86efac; border:1px solid rgba(34,197,94,.4); }
    .pill.fail { background:rgba(239,68,68,.15); color:#fca5a5; border:1px solid rgba(239,68,68,.4); }
    .violation-card { background:var(--panel); border:1px solid var(--border); border-left:3px solid var(--fail); border-radius:6px; padding:8px 10px; margin:8px 0; }
    .violation-card.warn { border-left-color:var(--warn); }
    .sev-dot { width:8px; height:8px; border-radius:2px; display:inline-block; flex-shrink:0; }
    .sev-dot.blocking { background:var(--fail); }
    .sev-dot.warning  { background:var(--warn); }
    .violation-card .vc-head { display:flex; gap:6px; align-items:center; flex-wrap:wrap; }
    .violation-card .vc-title { font-size:13px; font-weight:600; margin:4px 0 2px; }
    .violation-card .vc-what  { color:#cbd5e1; font-size:12px; }
    .violation-card .vc-metric { background:rgba(255,255,255,.03); border:1px solid var(--border); border-radius:4px; padding:6px 8px; margin:6px 0; font-size:11px; white-space:pre; }
    .violation-card .vc-cell  { color:#fcd34d; font-size:11px; margin-top:4px; }
    .violation-card .vc-ctx   { color:var(--muted); font-size:10px; margin-top:6px; }
    .violation-card .vc-why   { color:#94a3b8; font-size:11px; margin-top:4px; font-style:italic; }
    .neighbor-card { background:var(--panel); border:1px solid var(--border); border-left:3px solid var(--accent); border-radius:5px; padding:6px 8px; margin:5px 0; }
    .neighbor-card:hover { background:var(--hover); }
    .neighbor-card .vc-title { font-size:11px; word-break:break-all; color:#cbd5e1; margin:3px 0; }
    .cell-table { font-family:monospace; font-size:11px; border:1px solid var(--border); border-radius:5px; overflow:hidden; }
    .cell-row { display:grid; grid-template-columns:40px 50px 60px 70px 1fr; gap:6px; padding:3px 6px; border-bottom:1px solid var(--border); }
    .cell-row.cell-head { background:var(--panel); color:var(--muted); font-weight:600; }
    .pill.warn { background:rgba(251,191,36,.15); color:#fbbf24; border:1px solid rgba(251,191,36,.4); }
    .vc-btn { margin-top:6px; font-size:10px; }
  </style>
</head>
<body>
<div id="app">
  <div id="topbar">
    <h1>🔬 Ontology Inspector <small>제약 위반 인과 그래프</small></h1>
    <div class="tb-group" id="layer-toggles">
      <span class="tb-label">레이어</span>
      <button class="btn active" data-layer="month"     title="월 노드">월</button>
      <button class="btn active" data-layer="group"     title="그룹 노드">그룹</button>
      <button class="btn active" data-layer="run"       title="실행 노드 — 월별 안에서 실행 목록을 함께 보기">실행</button>
      <button class="btn active" data-layer="rule"      title="규칙 노드">규칙</button>
      <button class="btn"        data-layer="violation" title="위반 노드 — 특정 run에서 발생한 위반을 그래프상에 표시">위반</button>
      <button class="btn"        data-layer="metric"    title="측정값 노드 (느림)">측정값</button>
      <button class="btn"        data-layer="cause"     title="환경/제약 원인 노드 (CoverageMin/TeamMin/OffWindow/Nurse 등)">원인</button>
    </div>
    <div class="divider"></div>
    <div class="tb-group">
      <span class="tb-label">결과</span>
      <button class="btn" data-status="ALL">전체</button>
      <button class="btn" data-status="FAIL">실패만</button>
      <button class="btn" data-status="PASS">통과만</button>
    </div>
    <div class="divider"></div>
    <div class="tb-group" id="severity-toggles">
      <span class="tb-label">심각도</span>
      <button class="btn active" data-severity="blocking" title="hard 위반 — 반드시 막아야 함">🔴 블로킹</button>
      <button class="btn active" data-severity="warning"  title="soft 위반 — 경고">🟡 경고</button>
    </div>
    <div class="divider"></div>
    <div class="tb-group" id="outcome-toggles">
      <span class="tb-label">Solver</span>
      <button class="btn" data-outcome="success"  title="CP-SAT primary 통과만">🟢 CP-SAT</button>
      <button class="btn" data-outcome="fallback" title="CP-SAT 실패→fallback이 해를 찾음">🟡 Fallback</button>
      <button class="btn" data-outcome="failed"   title="CP-SAT/fallback 모두 실패 또는 primary-unsat">🔴 실패</button>
    </div>
    <div class="divider"></div>
    <button class="btn" id="btnFit" title="화면에 맞춤">⛶ 맞춤</button>
    <button class="btn" id="btnReload" title="데이터 다시 불러오기">↻ 새로고침</button>
    <div class="stats" id="stats">로딩 중…</div>
  </div>

  <div id="sidebar">
    <div class="help">
      <b>사용 방법</b><br>
      ① 아래 필터로 조회 범위를 좁힙니다.<br>
      ② 가운데 그래프에서 <b>노드를 클릭</b>합니다.<br>
      ③ 오른쪽 패널에 <b>원인 / 영향 / 위반</b>이 색 구역으로 나타납니다.
    </div>
    <div class="sb-section">
      <h2>📅 월 <span class="hint-tip">조회할 월 선택</span></h2>
      <div id="filter-months"></div>
    </div>
    <div class="sb-section">
      <h2>🏥 그룹 <span class="hint-tip">병동 그룹</span></h2>
      <div id="filter-groups"></div>
    </div>
    <div class="sb-section">
      <h2>⚙️ 전략 <span class="hint-tip">스케줄러 모드</span></h2>
      <div id="filter-strategies"></div>
    </div>
    <div class="sb-section">
      <h2>🧮 Solver 상태 <span class="hint-tip">CP-SAT/fallback 결과</span></h2>
      <div id="filter-solver"></div>
    </div>
    <div class="sb-section">
      <h2>📋 룰 ID <span class="hint-tip">예: D_MAX_OVER</span></h2>
      <input type="text" id="filter-rule-search" placeholder="rule_id 검색" />
      <div id="filter-rules" style="max-height:240px; overflow:auto; margin-top:4px;"></div>
    </div>
    <div class="sb-section">
      <h2>🎨 노드 색 범례</h2>
      <div class="legend-grid" id="legend"></div>
    </div>
  </div>

  <div id="canvas-wrap" style="position:relative;">
    <div id="cy"></div>
    <div class="canvas-hint">노드 클릭 → 오른쪽 패널에 원인·영향 노출</div>
  </div>

  <div id="detail">
    <div class="d-empty">
      <div class="icon">🖱️</div>
      <b>노드를 클릭하세요</b><br>
      클릭한 노드의 속성·원인 노드·영향 노드·관련 위반이<br>
      여기 색 구역으로 정리되어 표시됩니다.
    </div>
  </div>
</div>

<script>
const PALETTE = {
  MonthNode:'#93c5fd', GroupNode:'#c4b5fd', RunNode:'#7dd3fc',
  RuleNode:'#a7f3d0', MetricNode:'#f9a8d4', ViolationNode:'#fca5a5',
  CarryoverTransitionNode:'#67e8f9',
  CoverageMinNode:'#fde68a', TeamMinNode:'#fdba74', GradeMinNode:'#fcd34d',
  GradeMaxNode:'#fbbf24', OffWindowNode:'#86efac', PrecepteeSyncNode:'#d8b4fe',
  WantedSubmissionNode:'#60a5fa', WantedApplyNode:'#34d399',
  // Conflict core + member types
  ConflictCoreNode:'#e879f9',   // 자주색 — 다중 충돌 코어
  NurseRoleNode:'#fb923c',
  OffCapNode:'#fcd34d',
  NightCapNode:'#a78bfa',
  MonthlyNExactNode:'#fb7185',
  ConstraintNode:'#9ca3af', default:'#d1d5db',
};

const state = {
  months:new Set(), groups:new Set(), strategies:new Set(), rules:new Set(),
  solverStatuses:new Set(),
  layers:new Set(['month','group','run','rule']),  // default
  severities:new Set(['blocking','warning']),       // default both
  status:'ALL',
  cy:null, facets:null, allRules:[], reloadPending:false, catalog:{},
};

const GROUP_LABEL = {
  A:'Hard 전이/회복', B:'OFF/휴무', C:'공정성', D:'커버리지',
  E:'Wanted/Fixed', F:'전월 carryover', G:'특수', H:'시스템',
};

const TYPE_RANK = {
  GroupNode:0, MonthNode:1, RunNode:2, RuleNode:3,
  ConflictCoreNode:4,  // 충돌 코어는 violation 위, member 아래
  MetricNode:5, ViolationNode:5, CarryoverTransitionNode:5,
  CoverageMinNode:6, TeamMinNode:6, GradeMinNode:6, GradeMaxNode:6,
  OffWindowNode:6, PrecepteeSyncNode:6,
  NurseRoleNode:6, OffCapNode:6, NightCapNode:6, MonthlyNExactNode:6,
};

function $(sel) { return document.querySelector(sel); }
function el(tag, attrs={}, ...kids) {
  const e = document.createElement(tag);
  for (const k in attrs) {
    if (k === 'class') e.className = attrs[k];
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), attrs[k]);
    else e.setAttribute(k, attrs[k]);
  }
  for (const k of kids) e.append(k);
  return e;
}

async function fetchJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url}: ${r.status}`);
  return r.json();
}

function renderFacet(containerId, facetEntries, stateSet) {
  const c = $(containerId);
  c.innerHTML = '';
  for (const [key, count] of facetEntries) {
    const cb = el('input', { type:'checkbox' });
    cb.checked = stateSet.has(key);
    cb.addEventListener('change', () => {
      if (cb.checked) stateSet.add(key); else stateSet.delete(key);
      reloadGraph();
    });
    c.append(el('label', {}, cb, `${key} (${count})`));
  }
}

function renderRulesList(filter='') {
  const c = $('#filter-rules');
  c.innerHTML = '';
  const q = filter.trim().toUpperCase();
  for (const r of state.allRules) {
    if (q && !r.rule_id.toUpperCase().includes(q)) continue;
    const cb = el('input', { type:'checkbox' });
    cb.checked = state.rules.has(r.rule_id);
    cb.addEventListener('change', () => {
      if (cb.checked) state.rules.add(r.rule_id); else state.rules.delete(r.rule_id);
      reloadGraph();
    });
    const label = el('label', {}, cb);
    const sevDot = el('span', { class:'sev-dot ' + (r.severity || '') });
    sevDot.title = r.severity === 'blocking' ? '블로킹 (hard)' : '경고 (soft)';
    label.append(sevDot);
    label.append(r.rule_id);
    if (r.fail > 0) {
      const pillCls = r.severity === 'blocking' ? 'pill fail' : 'pill warn';
      label.append(el('span', { class:pillCls }, `${r.fail}/${r.total}`));
    } else {
      label.append(el('span', { class:'pill pass' }, 'OK'));
    }
    c.append(label);
  }
}

function buildQuery() {
  const p = new URLSearchParams();
  if (state.months.size) p.set('months', [...state.months].join(','));
  if (state.groups.size) p.set('groups', [...state.groups].join(','));
  if (state.strategies.size) p.set('strategies', [...state.strategies].join(','));
  if (state.solverStatuses.size) p.set('solver_statuses', [...state.solverStatuses].join(','));
  if (state.rules.size) p.set('rules', [...state.rules].join(','));
  p.set('status', state.status);
  if (state.layers.size) p.set('layers', [...state.layers].join(','));
  if (state.severities.size && state.severities.size < 2) p.set('severities', [...state.severities].join(','));
  return p.toString();
}

function ensureCy() {
  if (state.cy) return state.cy;
  state.cy = cytoscape({
    container: $('#cy'),
    elements: [],
    style: [
      { selector:'node', style: {
        'background-color':'data(color)',
        'label':'data(label)',
        'font-size':11, 'font-weight':500,
        'text-wrap':'wrap', 'text-max-width':160,
        'text-valign':'bottom', 'text-halign':'center', 'text-margin-y':3,
        'color':'#e8ecf4',
        'text-background-color':'#0b1020', 'text-background-opacity':0.85,
        'text-background-padding':3, 'text-background-shape':'roundrectangle',
        'text-border-color':'#29304a', 'text-border-width':1, 'text-border-opacity':0.5,
        'border-width':2, 'border-color':'#0b1020',
        'width':32, 'height':32,
        'min-zoomed-font-size': 5,
      }},
      { selector:'node[isFail = 1][severity = "blocking"]', style: {
        'border-width':4, 'border-color':'#ef4444', 'width':40, 'height':40,
        'text-border-color':'#ef4444', 'text-border-opacity':0.9,
        'shape':'octagon',
      }},
      { selector:'node[isFail = 1][severity = "warning"]', style: {
        'border-width':3, 'border-color':'#fbbf24', 'width':36, 'height':36,
        'text-border-color':'#fbbf24', 'text-border-opacity':0.8,
        'shape':'diamond',
      }},
      { selector:'node[isFail = 1][severity = ""]', style: {
        'border-width':3, 'border-color':'#ef4444', 'width':36, 'height':36,
      }},
      // ConflictCoreNode visual differentiation by solver_phase
      { selector:'node[type = "ConflictCoreNode"][solverPhase = "primary"]', style: {
        'background-color':'#e879f9',
        'shape':'round-hexagon',
        'border-width':3, 'border-color':'#a855f7',
      }},
      { selector:'node[type = "ConflictCoreNode"][solverPhase = "fallback"]', style: {
        'background-color':'#fb923c',
        'shape':'octagon',
        'border-width':4, 'border-color':'#ea580c',
        'border-style':'dashed',
      }},
      // RunNode visual differentiation by solver outcome
      { selector:'node[type = "RunNode"][runOutcome = "success"]', style: {
        'border-width':3, 'border-color':'#22c55e',
        'shape':'ellipse',
      }},
      { selector:'node[type = "RunNode"][runOutcome = "fallback"]', style: {
        'border-width':3, 'border-color':'#fbbf24',
        'shape':'round-hexagon',
      }},
      { selector:'node[type = "RunNode"][runOutcome = "failed"]', style: {
        'border-width':4, 'border-color':'#ef4444',
        'shape':'octagon', 'width':40, 'height':40,
      }},
      { selector:'edge', style: {
        'curve-style':'haystack','haystack-radius':0.3,
        'line-color':'#60709c','width':1.2, 'opacity':0.5,
      }},
      { selector:'.dim', style:{ 'opacity':0.08 }},
      { selector:'node.focus', style:{
        'opacity':1,
        'border-width':4, 'border-color':'#facc15', 'border-opacity':1,
        'text-border-color':'#facc15', 'text-border-opacity':1, 'text-border-width':2,
        'z-index':99,
      }},
      { selector:'edge.focus', style:{
        'opacity':1,
        'line-color':'#facc15', 'width':3.5,
        'curve-style':'bezier', 'target-arrow-shape':'triangle',
        'target-arrow-color':'#facc15', 'arrow-scale':1.2,
        'z-index':99,
      }},
    ],
    textureOnViewport: true,
    hideEdgesOnViewport: true,
    pixelRatio: 1,
    wheelSensitivity: 0.2,
  });
  state.cy.on('tap', 'node', (evt) => {
    const id = evt.target.data('id');
    const nb = evt.target.closedNeighborhood();
    state.cy.elements().addClass('dim').removeClass('focus');
    nb.addClass('focus').removeClass('dim');
    showNodeDetail(id);
  });
  state.cy.on('tap', (evt) => {
    if (evt.target === state.cy) state.cy.elements().removeClass('dim focus');
  });
  return state.cy;
}

// Deterministic layered layout — O(n), no force simulation.
// Layers with many nodes wrap into multiple sub-rows so labels don't overlap.
function computePresetPositions(nodes) {
  const layers = {};
  for (const n of nodes) {
    const r = TYPE_RANK[n.type] ?? 5;
    (layers[r] = layers[r] || []).push(n);
  }
  const W = Math.max(900, $('#cy').clientWidth - 40);
  const SUB_ROW_H = 75;  // 노드 라벨이 노드 아래에 박히기 때문에 행간 더 띄움
  const LAYER_GAP = 80;
  const MAX_PER_ROW = 12;
  const pos = {};
  const ranks = Object.keys(layers).map(Number).sort((a, b) => a - b);
  let yCursor = 60;
  for (const r of ranks) {
    const row = layers[r];
    row.sort((a, b) => (a.id < b.id ? -1 : 1));
    const subRows = Math.max(1, Math.ceil(row.length / MAX_PER_ROW));
    const perRow = Math.ceil(row.length / subRows);
    for (let i = 0; i < row.length; i++) {
      const sub = Math.floor(i / perRow);
      const col = i % perRow;
      const colsInSub = (sub === subRows - 1)
        ? (row.length - sub * perRow)
        : perRow;
      const gap = W / (colsInSub + 1);
      pos[row[i].id] = { x: gap * (col + 1), y: yCursor + sub * SUB_ROW_H };
    }
    yCursor += subRows * SUB_ROW_H + LAYER_GAP;
  }
  return pos;
}

async function reloadGraph() {
  if (state.reloadPending) return;
  state.reloadPending = true;
  try {
    const t0 = performance.now();
    const g = await fetchJSON('/ontology/graph?' + buildQuery());
    $('#stats').textContent =
      `runs ${g.stats.runs_in_view} · nodes ${g.stats.node_count} · edges ${g.stats.edge_count} · violations ${g.stats.violation_count}`;

    const failRuleIds = new Set();
    for (const v of g.violations || []) failRuleIds.add(v.rule_id);

    const positions = computePresetPositions(g.nodes);
    const nodeIds = new Set(g.nodes.map(n => n.id));
    const elements = [];
    for (const n of g.nodes) {
      const isFail = (n.type === 'RuleNode' && failRuleIds.has(n.attrs?.rule_id))
                  || (n.type === 'ViolationNode');
      // RunNode → 'success' (all primary) | 'fallback' (CP-SAT 실패→fallback success) | 'failed' (infeasible/unsat)
      let runOutcome = '';
      if (n.type === 'RunNode') {
        const dist = n.attrs?._solver_status_dist || {};
        if (dist['cpsat-infeasible'] || dist['fallback-infeasible'] || dist['primary-unsat'] || dist['http-error']) runOutcome = 'failed';
        else if (dist['fallback-optimal'] || dist['fallback']) runOutcome = 'fallback';
        else if (dist['primary']) runOutcome = 'success';
      }
      elements.push({
        group: 'nodes',
        data: {
          id: n.id, label: shortLabel(n), type: n.type,
          color: PALETTE[n.type] || PALETTE.default,
          isFail: isFail ? 1 : 0,
          severity: String(n.attrs?.severity || ''),
          runOutcome,
          solverPhase: String(n.attrs?.solver_phase || ''),
        },
        position: positions[n.id],
      });
    }
    for (const e of g.edges) {
      if (!nodeIds.has(e.from) || !nodeIds.has(e.to)) continue;
      elements.push({
        group: 'edges',
        data: { id: `${e.type}|${e.from}|${e.to}`, source: e.from, target: e.to, type: e.type },
      });
    }

    const cy = ensureCy();
    cy.batch(() => {
      cy.elements().remove();
      cy.add(elements);
    });
    cy.fit(cy.elements(), 40);
    $('#stats').textContent += ` · ${Math.round(performance.now() - t0)}ms`;
  } finally {
    state.reloadPending = false;
  }
}

function shortLabel(n) {
  if (n.type === 'RuleNode') {
    const rid = n.attrs?.rule_id || n.id;
    const f = n.attrs?._fail_count, t = n.attrs?._runs_total;
    if (f > 0 && t) return `${rid}\n${f}/${t} fail`;
    return rid;
  }
  if (n.type === 'MonthNode') return `${n.attrs?.year}-${String(n.attrs?.month).padStart(2,'0')}`;
  if (n.type === 'RunNode') {
    const s = n.attrs?.strategy || '';
    const sched = n.attrs?._schedule_id || (n.attrs?.run_id || '').slice(-8);
    const dist = n.attrs?._solver_status_dist || {};
    let icon = '';
    if (dist['cpsat-infeasible'] || dist['fallback-infeasible'] || dist['primary-unsat'] || dist['http-error']) icon = '🔴 ';
    else if (dist['fallback-optimal'] || dist['fallback']) icon = '🟡 ';
    else if (dist['primary']) icon = '🟢 ';
    return `${icon}${s} · ${sched}`;
  }
  if (n.type === 'GroupNode') return `grp:${(n.attrs?.group_id || '').slice(0, 12)}`;
  if (n.type === 'MetricNode') return `${n.attrs?.metric || ''}=${n.attrs?.value}`;
  if (n.type === 'ViolationNode') return n.attrs?.rule_id || 'violation';
  return n.id.length > 28 ? n.id.slice(0, 25) + '…' : n.id;
}

function renderLegend() {
  const items = [
    'MonthNode','GroupNode','RunNode','RuleNode',
    'MetricNode','ViolationNode',
    'ConflictCoreNode',
    'NurseRoleNode','OffCapNode','NightCapNode','MonthlyNExactNode',
    'CoverageMinNode','CoverageMaxNode',
    'TeamMinNode','GradeMinNode','GradeMaxNode',
    'MonthlyOffNode','WeeklyOffNode','OffWindowNode',
    'CarryoverTransitionNode','PrecepteeSyncNode',
    'WantedSubmissionNode','WantedApplyNode',
    'NurseNode','ConstraintNode',
  ];
  const box = $('#legend');
  box.innerHTML = '';
  for (const t of items) {
    const item = el('div', { class:'legend-item' });
    const dot = el('span', { class:'legend-dot' });
    dot.style.background = PALETTE[t] || PALETTE.default;
    item.append(dot);
    item.append(nodeTypeKorean(t));
    box.append(item);
  }
}

function renderNeighborList(neighbors) {
  const box = el('div');
  for (const n of neighbors) {
    const card = el('div', { class:'neighbor-card' });
    const head = el('div', { class:'vc-head' });
    const tb = el('span', { class:'badge' }, nodeTypeKorean(n.type));
    tb.style.background = (PALETTE[n.type] || PALETTE.default);
    tb.style.color = '#0b1020';
    head.append(tb);
    for (const et of (n.edge_types || [])) {
      const ep = el('span', { class:'pill fail' }, edgeTypeKorean(et));
      ep.title = et;
      head.append(ep);
    }
    card.append(head);
    card.append(el('div', { class:'vc-title' }, n.id));
    const attrs = n.attrs || {};
    const lines = [];
    for (const [k, v] of Object.entries(attrs)) {
      if (typeof v === 'object') continue;
      if (k.startsWith('_')) continue;
      lines.push(`${k}: ${v}`);
    }
    if (lines.length) card.append(el('pre', { class:'vc-metric' }, lines.join('\\n')));
    card.append(el('div', { class:'vc-ctx' }, `${(n.seen_in_runs || []).length} runs에서 등장`));
    // Click to jump
    card.addEventListener('click', () => showNodeDetail(n.id));
    card.style.cursor = 'pointer';
    box.append(card);
  }
  return box;
}

function renderCellTable(cells, kind) {
  const wrap = el('div', { class:'cell-table' });
  const head = el('div', { class:'cell-row cell-head' });
  ['day','shift','need','assigned', kind].forEach(k => head.append(el('span', {}, k)));
  wrap.append(head);
  for (const c of cells.slice(0, 50)) {
    const row = el('div', { class:'cell-row' });
    row.append(el('span', {}, String(c.day ?? '?')));
    row.append(el('span', {}, c.shift ?? '?'));
    row.append(el('span', {}, String(c.need ?? '-')));
    row.append(el('span', {}, String(c.assigned ?? '-')));
    row.append(el('span', { class: kind === 'shortage' ? 'pill fail' : 'pill warn' },
      String(c[kind] ?? c.over ?? c.shortage ?? '?')));
    wrap.append(row);
  }
  if (cells.length > 50) wrap.append(el('div', { class:'vc-ctx' }, `(+${cells.length - 50} more)`));
  return wrap;
}

function renderGroupedViolationCard(g) {
  const meta = state.catalog[g.rule_id] || {};
  const ruleStat = state.allRules.find(r => r.rule_id === g.rule_id);
  const severity = ruleStat ? ruleStat.severity : null;
  const card = el('div', { class:'violation-card' + (severity === 'warning' ? ' warn' : '') });
  const head = el('div', { class:'vc-head' });
  head.append(el('span', { class:'pill ' + (severity === 'blocking' ? 'fail' : 'warn') },
    severity === 'blocking' ? '🔴 블로킹' : (severity === 'warning' ? '🟡 경고' : '?')));
  head.append(el('span', { class:'pill fail' }, g.rule_id));
  if (meta.group) head.append(el('span', { class:'badge' }, `${meta.group} · ${GROUP_LABEL[meta.group] || ''}`));
  head.append(el('span', { class:'badge' }, `${g.month_key || '?'}`));
  head.append(el('span', { class:'badge' }, `${g.items.length} runs`));
  card.append(head);
  if (meta.title) card.append(el('div', { class:'vc-title' }, meta.title));
  if (meta.what)  card.append(el('div', { class:'vc-what' }, `→ ${meta.what}`));

  const values = g.items.map(v => (v.evidence || {}).value).filter(x => typeof x === 'number');
  const ev0 = g.items[0].evidence || {};
  const lines = [];
  if (ev0.metric) lines.push(`측정값  ${ev0.metric}`);
  if (ev0.condition) lines.push(`조건    ${ev0.condition}`);
  if (values.length) {
    const min = Math.min(...values), max = Math.max(...values);
    const avg = values.reduce((a, b) => a + b, 0) / values.length;
    lines.push(`값      ${min === max ? min : `${min} ~ ${max} (avg ${Number(avg.toFixed(2))})`}  · n=${values.length}`);
  }
  if (lines.length) card.append(el('pre', { class:'vc-metric' }, lines.join('\\n')));

  // Cell-level evidence: collect distinct (day,shift) combos if present
  const cells = new Set();
  for (const v of g.items) {
    const e = v.evidence || {};
    if (e.day !== undefined || e.shift) cells.add(`d${e.day ?? '?'}·${e.shift ?? '?'}`);
  }
  if (cells.size) {
    const arr = [...cells].slice(0, 10);
    card.append(el('div', { class:'vc-cell' }, `cells: ${arr.join(', ')}${cells.size > 10 ? ` (+${cells.size - 10})` : ''}`));
  }

  // Expand button — show individual run breakdown on demand
  const expanded = el('div', { class:'vc-expand' });
  expanded.style.display = 'none';
  for (const v of g.items.slice(0, 20)) {
    const val = (v.evidence || {}).value;
    expanded.append(el('div', { class:'vc-ctx' }, `${(v.run_id || '').slice(-12)} · ${v.strategy || ''} · value=${val}`));
  }
  const btn = el('button', { class:'btn vc-btn' }, `${g.items.length}개 run 펼치기`);
  btn.addEventListener('click', () => {
    const visible = expanded.style.display !== 'none';
    expanded.style.display = visible ? 'none' : 'block';
    btn.textContent = visible ? `${g.items.length}개 run 펼치기` : '접기';
  });
  card.append(btn);
  card.append(expanded);

  if (meta.why) card.append(el('div', { class:'vc-why' }, `※ ${meta.why}`));
  return card;
}

function renderViolationCard(v) {
  const meta = state.catalog[v.rule_id] || {};
  const card = el('div', { class:'violation-card' });
  const head = el('div', { class:'vc-head' });
  head.append(el('span', { class:'pill fail' }, v.rule_id));
  if (meta.group) head.append(el('span', { class:'badge' }, `${meta.group} · ${GROUP_LABEL[meta.group] || ''}`));
  card.append(head);
  if (meta.title) card.append(el('div', { class:'vc-title' }, meta.title));
  if (meta.what)  card.append(el('div', { class:'vc-what' }, `→ ${meta.what}`));

  const ev = v.evidence || {};
  const cond = ev.condition || '';
  const val = ev.value !== undefined ? ev.value : (ev.metric !== undefined ? ev.metric : '');
  const slack = (v.slack !== null && v.slack !== undefined) ? v.slack : ev.slack;
  const lines = [];
  if (ev.metric && val !== '') lines.push(`측정값  ${ev.metric} = ${val}`);
  if (cond) lines.push(`조건    ${cond}`);
  if (slack !== null && slack !== undefined) lines.push(`초과량  slack ${slack}`);
  if (lines.length) card.append(el('pre', { class:'vc-metric' }, lines.join('\\n')));

  // Cell-level evidence — surface day/shift/nurse if present (richer for D_*_MIN, A_*, etc.)
  const cellBits = [];
  if (ev.day !== undefined) cellBits.push(`day ${ev.day}`);
  if (ev.shift) cellBits.push(`shift ${ev.shift}`);
  if (ev.nurse_id !== undefined) cellBits.push(`nurse ${ev.nurse_id}`);
  if (ev.need !== undefined) cellBits.push(`need ${ev.need}`);
  if (ev.assigned !== undefined) cellBits.push(`assigned ${ev.assigned}`);
  if (ev.shortage !== undefined) cellBits.push(`shortage ${ev.shortage}`);
  if (cellBits.length) card.append(el('div', { class:'vc-cell' }, cellBits.join(' · ')));

  const ctx = [];
  if (v.month_key) ctx.push(v.month_key);
  if (v.strategy) ctx.push(v.strategy);
  if (v.run_id) ctx.push(v.run_id.slice(-12));
  if (ctx.length) card.append(el('div', { class:'vc-ctx' }, ctx.join(' · ')));

  if (meta.why) card.append(el('div', { class:'vc-why' }, `※ ${meta.why}`));
  return card;
}

function edgeTypeKorean(t) {
  return ({
    RUN_ON:'실행 기준', IN_GROUP:'그룹 소속',
    EVALUATED_BY:'규칙 평가', FAILED_RULE:'규칙 실패',
    OBSERVED_IN:'발견된 run', CAUSES_VIOLATION:'위반 유발',
    CONTEXT_OF:'문맥', DAY_SHIFT:'일·시프트',
    BLOCKED_RUN:'run 차단', RISKY_FOR:'위태로운 제약',
    MEMBER_OF_CONFLICT:'충돌 구성원',
  })[t] || t;
}

function nodeTypeKorean(t) {
  const M = {
    MonthNode:'월',  GroupNode:'그룹', RunNode:'실행', RuleNode:'규칙',
    MetricNode:'측정값', ViolationNode:'위반',
    CoverageMinNode:'최소 인원 제약', CoverageMaxNode:'최대 인원 제약',
    TeamMinNode:'팀 최소 인원', TeamMaxNode:'팀 최대 인원',
    GradeMinNode:'숙련도 최소', GradeMaxNode:'숙련도 최대',
    MonthlyOffNode:'월간 OFF 제약', WeeklyOffNode:'주휴 제약',
    OffWindowNode:'OFF 구간', PrecepteeSyncNode:'프리셉티 동반',
    CarryoverTransitionNode:'전월 전이', WantedSubmissionNode:'원티드 제출',
    WantedApplyNode:'원티드 반영', NurseNode:'간호사',
    ShiftNode:'시프트', DayNode:'일자', TeamNode:'팀',
    FairnessNode:'공정성', DataQualityNode:'데이터 정합성',
    // Conflict core members
    ConflictCoreNode:'⚡ 충돌 코어',
    NurseRoleNode:'간호사 시프트 가용역할',
    OffCapNode:'OFF 상한',
    NightCapNode:'월간 N 상한',
    MonthlyNExactNode:'월 N exact',
    ConstraintNode:'기타 제약',
  };
  return M[t] || t || '?';
}

async function showNodeDetail(id) {
  const d = $('#detail');
  d.innerHTML = '<div class="d-empty"><div class="icon">⏳</div>불러오는 중…</div>';
  try {
    const j = await fetchJSON('/ontology/node/' + encodeURIComponent(id));
    d.innerHTML = '';
    const n = j.node;

    // ── 1. 기본 정보 ──
    const head = el('div', { class:'d-section' });
    const titleRow = el('h3', {});
    const typeBadge = el('span', { class:'badge' }, nodeTypeKorean(n.type));
    typeBadge.style.background = (PALETTE[n.type] || PALETTE.default);
    typeBadge.style.color = '#0b1020';
    titleRow.append(typeBadge);
    titleRow.append(el('span', {}, n.type));
    head.append(titleRow);
    head.append(el('div', { class:'h-desc' }, n.id));

    const kv = el('div', { class:'kv' });
    for (const [k, v] of Object.entries(n.attrs || {})) {
      if (typeof v === 'object' || k.startsWith('_')) continue;
      kv.append(el('b', {}, k));
      kv.append(el('span', {}, String(v)));
    }
    if (n.type === 'RunNode') {
      if (n.attrs._schedule_id) { kv.append(el('b', {}, 'schedule')); kv.append(el('span', {}, n.attrs._schedule_id)); }
      if (n.attrs._solver_status_dist) { kv.append(el('b', {}, 'solver 결과')); kv.append(el('span', {}, JSON.stringify(n.attrs._solver_status_dist))); }
      if (n.attrs._fallback_used !== undefined) { kv.append(el('b', {}, 'fallback')); kv.append(el('span', {}, n.attrs._fallback_used ? '⚠ 사용됨' : '미사용')); }
      if (n.attrs._coverage_over_cells !== undefined) { kv.append(el('b', {}, 'over cells')); kv.append(el('span', {}, String(n.attrs._coverage_over_cells))); }
      if (n.attrs._coverage_under_cells !== undefined) { kv.append(el('b', {}, 'under cells')); kv.append(el('span', {}, String(n.attrs._coverage_under_cells))); }
    } else if (n.type === 'RuleNode') {
      if (n.attrs._fail_count !== undefined) {
        kv.append(el('b', {}, '실패 / 전체'));
        kv.append(el('span', {}, `${n.attrs._fail_count} / ${n.attrs._runs_total}  (${(n.attrs._fail_ratio*100).toFixed(0)}%)`));
      }
      const meta = state.catalog[n.attrs.rule_id];
      if (meta) {
        if (meta.title) { kv.append(el('b', {}, '제목')); kv.append(el('span', {}, meta.title)); }
        if (meta.what)  { kv.append(el('b', {}, '의미')); kv.append(el('span', {}, meta.what)); }
        if (meta.why)   { kv.append(el('b', {}, '이유')); kv.append(el('span', {}, meta.why)); }
      }
    }
    head.append(kv);
    head.append(el('div', { class:'h-desc' }, `📊 ${j.incidence_count}개 실행에서 등장`));
    d.append(head);

    // ── 1.5 ConflictCoreNode 전용 인과 스토리 ──
    if (n.type === 'ConflictCoreNode') {
      const sec = el('div', { class:'d-section viol' });
      const phase = n.attrs.solver_phase;
      const phaseHead = el('h3', {}, '⚡ 충돌 코어 — 인과 스토리');
      if (phase === 'primary') {
        phaseHead.append(el('span', { class:'badge', style:'background:#e879f9; color:#0b1020; margin-left:6px;' }, '🟣 Primary'));
      } else if (phase === 'fallback') {
        phaseHead.append(el('span', { class:'badge', style:'background:#fb923c; color:#0b1020; margin-left:6px;' }, '🟠 Fallback (일부 hard→soft 전환됨)'));
      } else {
        phaseHead.append(el('span', { class:'badge', style:'margin-left:6px;' }, '🔵 Detector'));
      }
      sec.append(phaseHead);
      const phaseHint = phase === 'fallback'
        ? 'fallback 단계 MUS — primary에서 풀리지 않아 일부 hard 제약이 soft 전환된 상태에서 남은 충돌.'
        : phase === 'primary'
        ? 'CP-SAT primary 솔버가 직접 식별한 충돌 (모든 hard 제약 활성 상태).'
        : '코드 기반 detector가 식별한 충돌 (모델 외 신호 포함).';
      sec.append(el('div', { class:'h-desc' }, phaseHint));
      // group-level: affected_nurse_ids (nurse-scoped) 또는 affected_scope_keys (정책-scoped) 표시
      const affected = n.attrs.affected_nurse_ids || [];
      const affectedKeys = n.attrs.affected_scope_keys || [];
      const affCount = n.attrs.affected_count;
      if (affCount && affCount > 1 && affected.length > 0) {
        // nurse-scoped: "n명에 동시 발생"
        const pill = el('div', { class:'badge', style:'background:rgba(232,121,249,.18); color:#f0abfc; border-color:#e879f9; display:block; padding:6px 10px; margin:4px 0;' },
          `👥 ${affCount}명에 동시 발생`);
        sec.append(pill);
        const nurseList = el('div', { style:'font-size:11px; color:#cbd5e1; margin:4px 0 8px;' });
        nurseList.append(el('b', {}, 'affected nurses: '));
        nurseList.append(affected.slice(0, 30).join(', '));
        if (affected.length > 30) nurseList.append(` (+${affected.length - 30}명)`);
        sec.append(nurseList);
        const perNurse = n.attrs.per_nurse_cores || [];
        if (perNurse.length) {
          const btn = el('button', { class:'btn vc-btn' }, `${perNurse.length}개 nurse별 코어 펼치기`);
          const list = el('div');
          list.style.display = 'none';
          for (const pid of perNurse.slice(0, 50)) {
            const link = el('div', { class:'badge', style:'display:block; cursor:pointer; margin:2px 0;' }, pid);
            link.addEventListener('click', () => showNodeDetail(pid));
            list.append(link);
          }
          btn.addEventListener('click', () => {
            const vis = list.style.display !== 'none';
            list.style.display = vis ? 'none' : 'block';
            btn.textContent = vis ? `${perNurse.length}개 nurse별 코어 펼치기` : '접기';
          });
          sec.append(btn);
          sec.append(list);
        }
      } else if (affCount && affCount > 1 && affectedKeys.length > 0) {
        // 정책-scoped (grade 등): "n건의 정책에 동시 발생"
        const scopeLabel = String(n.attrs.scope || 'group');
        const pill = el('div', { class:'badge', style:'background:rgba(232,121,249,.18); color:#f0abfc; border-color:#e879f9; display:block; padding:6px 10px; margin:4px 0;' },
          `📐 ${affCount}건의 ${scopeLabel} 정책에 동시 발생`);
        sec.append(pill);
        const keyList = el('div', { style:'font-size:11px; color:#cbd5e1; margin:4px 0 8px;' });
        keyList.append(el('b', {}, 'affected policies: '));
        keyList.append(affectedKeys.slice(0, 30).join(', '));
        if (affectedKeys.length > 30) keyList.append(` (+${affectedKeys.length - 30}건)`);
        sec.append(keyList);
        const perMember = n.attrs.per_member_cores || [];
        if (perMember.length) {
          const btn = el('button', { class:'btn vc-btn' }, `${perMember.length}개 정책별 코어 펼치기`);
          const list = el('div');
          list.style.display = 'none';
          for (const pid of perMember.slice(0, 50)) {
            const link = el('div', { class:'badge', style:'display:block; cursor:pointer; margin:2px 0;' }, pid);
            link.addEventListener('click', () => showNodeDetail(pid));
            list.append(link);
          }
          btn.addEventListener('click', () => {
            const vis = list.style.display !== 'none';
            list.style.display = vis ? 'none' : 'block';
            btn.textContent = vis ? `${perMember.length}개 정책별 코어 펼치기` : '접기';
          });
          sec.append(btn);
          sec.append(list);
        }
      }
      const concl = n.attrs.conclusion;
      if (concl) {
        sec.append(el('pre', { class:'vc-metric', style:'border-color:var(--fail);' }, `결론: ${concl}`));
      }
      const deriv = n.attrs.derivation || [];
      if (deriv.length) {
        const dv = el('div', { class:'kv' });
        for (const s of deriv) {
          dv.append(el('b', {}, `step ${s.step}`));
          dv.append(el('span', {}, s.infer || s.conclusion || s.from || ''));
        }
        sec.append(dv);
      }
      const hints = n.attrs.resolution_hints || [];
      if (hints.length) {
        sec.append(el('div', { class:'h-desc', style:'margin-top:8px; color:#86efac;' }, '✅ 해결 제안 (택일 또는 조합)'));
        for (const h of hints) {
          sec.append(el('div', { class:'badge', style:'display:block; margin:4px 0; padding:6px 10px; line-height:1.4;' }, `• ${h.human_message_ko || h.action}`));
        }
      }
      d.append(sec);
    }

    // ── 2. 원인 (inbound) — 빨간 stripe ──
    if (j.inbound_neighbors && j.inbound_neighbors.length) {
      const sec = el('div', { class:'d-section cause' });
      const h = el('h3', {}, '← 원인 노드');
      h.append(el('span', { class:'badge fail' }, String(j.inbound_neighbors.length)));
      sec.append(h);
      sec.append(el('div', { class:'h-desc' }, '이 노드를 만든 상위 노드들 (CAUSES_VIOLATION, BLOCKED_RUN, RISKY_FOR 등). 카드 클릭 → 그 노드로 이동.'));
      sec.append(renderNeighborList(j.inbound_neighbors));
      d.append(sec);
    }

    // ── 3. 영향 (outbound) — 파란 stripe ──
    if (j.outbound_neighbors && j.outbound_neighbors.length) {
      const sec = el('div', { class:'d-section effect' });
      const h = el('h3', {}, '→ 영향 노드');
      h.append(el('span', { class:'badge' }, String(j.outbound_neighbors.length)));
      sec.append(h);
      sec.append(el('div', { class:'h-desc' }, '이 노드가 가리키는 하위 노드들 (FAILED_RULE, OBSERVED_IN, RUN_ON 등 outbound edge).'));
      sec.append(renderNeighborList(j.outbound_neighbors));
      d.append(sec);
    }

    // ── 4. 관련 위반 — 빨간 stripe ──
    if (j.related_violations && j.related_violations.length) {
      const groups = {};
      for (const v of j.related_violations) {
        const k = `${v.rule_id}|${v.month_key || '?'}`;
        if (!groups[k]) groups[k] = { rule_id: v.rule_id, month_key: v.month_key, items: [] };
        groups[k].items.push(v);
      }
      const gList = Object.values(groups).sort((a, b) =>
        b.items.length - a.items.length || (a.month_key || '').localeCompare(b.month_key || ''));
      const sec = el('div', { class:'d-section viol' });
      const h = el('h3', {}, '⚠️ 관련 위반');
      h.append(el('span', { class:'badge fail' }, `${j.related_violations.length}건 / ${gList.length}그룹`));
      sec.append(h);
      sec.append(el('div', { class:'h-desc' }, '(rule_id, 월) 단위로 묶음. 하단 "n개 run 펼치기" 버튼으로 attempt별 보기.'));
      for (const g of gList) sec.append(renderGroupedViolationCard(g));
      d.append(sec);
    }

    // ── 5. 메트릭 추이 ──
    if (j.metric_history && j.metric_history.length) {
      const sec = el('div', { class:'d-section' });
      sec.append(el('h3', {}, '📈 메트릭 추이'));
      sec.append(el('div', { class:'h-desc' }, '같은 메트릭이 여러 run에서 보인 값.'));
      const tbl = el('div', { class:'kv' });
      for (const h of j.metric_history.slice(-12)) {
        tbl.append(el('b', {}, `${h.month_key || '?'} · ${(h.run_id || '').slice(-10)}`));
        tbl.append(el('span', {}, String(h.value)));
      }
      sec.append(tbl);
      d.append(sec);
    }

    // ── 6. Run drilldown — 파란 stripe ──
    if (j.run_drilldown) {
      const rd = j.run_drilldown;
      const sec = el('div', { class:'d-section drilldown' });
      sec.append(el('h3', {}, '🧩 Run 상세 — attempts · 셀'));
      sec.append(el('div', { class:'h-desc' }, 'solver attempts와 over/under cell 표. fallback이 일어난 attempt는 ⚠.'));
      const at = el('div', { class:'kv' });
      for (const a of (rd.attempts || [])) {
        at.append(el('b', {}, `attempt #${a.run_index || '?'}`));
        const fb = (a.solver_status || '').includes('fallback') ? ' ⚠' : '';
        at.append(el('span', {}, `${a.solver_status || '-'}${fb} · schedule=${a.schedule_id || '-'} · ${a.timing_ms || '?'}ms`));
      }
      sec.append(at);
      const dd = rd.drilldown || {};
      const over = dd.coverage_over_cells || [];
      const under = dd.coverage_under_cells || [];
      if (over.length) {
        sec.append(el('div', { class:'h-desc', style:'margin-top:8px;' }, `Over cells (정원 초과) · ${over.length}건`));
        sec.append(renderCellTable(over, 'over'));
      }
      if (under.length) {
        sec.append(el('div', { class:'h-desc', style:'margin-top:8px;' }, `Under cells (정원 미달) · ${under.length}건`));
        sec.append(renderCellTable(under, 'shortage'));
      }
      d.append(sec);
    }

    // ── 7. 등장한 run 리스트 ──
    if (j.incidences && j.incidences.length) {
      const sec = el('div', { class:'d-section' });
      sec.append(el('h3', {}, `📁 등장한 실행 (${j.incidence_count})`));
      const ul = el('div');
      for (const inc of j.incidences.slice(0, 30)) {
        ul.append(el('div', { class:'badge' }, `${inc.month_key || '?'} · ${inc.strategy || ''} · ${(inc.run_id || '').slice(-10)}`));
      }
      sec.append(ul);
      d.append(sec);
    }
  } catch (err) {
    d.innerHTML = `<div class="d-empty"><div class="icon">⚠️</div>오류: ${err.message}</div>`;
  }
}

async function bootstrap() {
  const facets = await fetchJSON('/ontology/runs');
  state.facets = facets.facets;
  renderFacet('#filter-months', facets.facets.months, state.months);
  renderFacet('#filter-groups', facets.facets.groups, state.groups);
  renderFacet('#filter-strategies', facets.facets.strategies, state.strategies);
  renderFacet('#filter-solver', facets.facets.solver_statuses || [], state.solverStatuses);
  renderLegend();

  const r = await fetchJSON('/ontology/rules');
  state.allRules = r.rules;
  renderRulesList('');

  state.catalog = await fetchJSON('/ontology/rule_catalog');

  $('#filter-rule-search').addEventListener('input', (e) => renderRulesList(e.target.value));

  document.querySelectorAll('[data-layer]').forEach(b => {
    b.addEventListener('click', () => {
      const lyr = b.dataset.layer;
      if (state.layers.has(lyr)) {
        state.layers.delete(lyr);
        b.classList.remove('active');
      } else {
        state.layers.add(lyr);
        b.classList.add('active');
      }
      reloadGraph();
    });
  });
  document.querySelectorAll('[data-severity]').forEach(b => {
    b.addEventListener('click', () => {
      const sv = b.dataset.severity;
      if (state.severities.has(sv)) {
        state.severities.delete(sv);
        b.classList.remove('active');
      } else {
        state.severities.add(sv);
        b.classList.add('active');
      }
      reloadGraph();
    });
  });

  // Solver outcome quick toggles → map to raw solver_status values
  const OUTCOME_MAP = {
    success:  ['primary'],
    fallback: ['fallback', 'fallback-optimal'],
    failed:   ['cpsat-infeasible', 'fallback-infeasible', 'primary-unsat', 'http-error'],
  };
  document.querySelectorAll('[data-outcome]').forEach(b => {
    b.addEventListener('click', () => {
      const oc = b.dataset.outcome;
      const mapped = OUTCOME_MAP[oc] || [];
      const isActive = b.classList.contains('active');
      if (isActive) {
        for (const s of mapped) state.solverStatuses.delete(s);
        b.classList.remove('active');
      } else {
        for (const s of mapped) state.solverStatuses.add(s);
        b.classList.add('active');
      }
      // Re-render sidebar checkboxes to stay in sync
      renderFacet('#filter-solver', state.facets?.solver_statuses || [], state.solverStatuses);
      reloadGraph();
    });
  });
  document.querySelectorAll('[data-status]').forEach(b => {
    b.addEventListener('click', () => {
      document.querySelectorAll('[data-status]').forEach(x => x.classList.remove('active'));
      b.classList.add('active');
      state.status = b.dataset.status;
      reloadGraph();
    });
  });
  $('#btnFit').addEventListener('click', () => state.cy && state.cy.fit(state.cy.elements(), 40));
  $('#btnReload').addEventListener('click', async () => {
    // Full soft-refresh: re-fetch facets + rules + graph so newly produced
    // harness runs (new schedule_id / solver_status / etc) appear without
    // requiring a full page reload.
    const facets = await fetchJSON('/ontology/runs');
    state.facets = facets.facets;
    renderFacet('#filter-months', facets.facets.months, state.months);
    renderFacet('#filter-groups', facets.facets.groups, state.groups);
    renderFacet('#filter-strategies', facets.facets.strategies, state.strategies);
    renderFacet('#filter-solver', facets.facets.solver_statuses || [], state.solverStatuses);
    const r = await fetchJSON('/ontology/rules');
    state.allRules = r.rules;
    renderRulesList($('#filter-rule-search').value || '');
    await reloadGraph();
  });

  await reloadGraph();
}
bootstrap();
</script>
</body>
</html>
"""


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def dashboard() -> HTMLResponse:
    return HTMLResponse(_HTML)
