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


# Node-type visibility tier for UI rendering.
#   - HIGH: 사용자가 즉시 봐야 하는 신호 (default visible)
#   - MED: 기본 숨김, 드릴다운 시 노출
#   - LOW: 그래프 전용 (agent/ontology layer 가 소비, UI 기본 숨김)
# 원본 nodes[] 는 그대로 유지하고, 각 노드에 `ui_visible: bool` 만 추가한다.
_NODE_TYPE_UI_TIER: dict[str, str] = {
    # 신호 강도 HIGH — 운영자 화면 1차 노출
    "ConflictCoreNode": "high",
    "ViolationNode": "high",
    "DataQualityNode": "high",
    "ConstraintNode": "high",
    # 신호 강도 MED — 드릴다운/필터 ON 시 노출
    "RuleNode": "med",
    "RunNode": "med",
    # 신호 강도 LOW — 그래프 백본/내부 풀 모델 (UI 기본 숨김)
    "MetricNode": "low",
    "TeamPoolNode": "low",
    "GradePoolNode": "low",
    "CommonPoolNode": "low",
    "MonthNode": "low",
    "GroupNode": "low",
    "NurseNode": "low",
    "TeamNode": "low",
    "DayNode": "low",
    "ShiftNode": "low",
}


def _ui_visible_for_type(node_type: str) -> bool:
    return _NODE_TYPE_UI_TIER.get(str(node_type or ""), "med") == "high"


def _ui_tier_for_type(node_type: str) -> str:
    return _NODE_TYPE_UI_TIER.get(str(node_type or ""), "med")


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


def _core_details_by_id(target: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rr = target.get("run_result") or {}
    out: dict[str, dict[str, Any]] = {}
    for attempt in (rr.get("runs") or []):
        inf = attempt.get("infeasible_detail") or {}
        for core in (inf.get("conflict_cores") or []):
            cid = str(core.get("core_id") or "")
            if cid and cid not in out:
                out[cid] = core
    return out


def _latest_fix_plan(target: dict[str, Any]) -> dict[str, Any] | None:
    rr = target.get("run_result") or {}
    runs = list(rr.get("runs") or [])
    for attempt in reversed(runs):
        inf = attempt.get("infeasible_detail") or {}
        fp = inf.get("fix_plan")
        if isinstance(fp, dict) and fp:
            return fp
    return None


def _build_fix_plan_links(target: dict[str, Any], fix_plan: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(fix_plan, dict):
        return []
    nodes = list((target.get("data") or {}).get("nodes") or [])
    pool_nodes = {
        str(n.get("id") or "")
        for n in nodes
        if str(n.get("type") or "") in {"TeamPoolNode", "GradePoolNode", "CommonPoolNode"}
    }
    links: list[dict[str, Any]] = []
    for action in list(fix_plan.get("actions") or []):
        aid = str(action.get("action_id") or "")
        for t in list(action.get("targets") or []):
            pool_id = str(t.get("pool_id") or "")
            if not pool_id:
                continue
            links.append(
                {
                    "action_id": aid,
                    "pool_id": pool_id,
                    "pool_node_exists": pool_id in pool_nodes,
                    "shortage": t.get("shortage"),
                }
            )
    return links


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
            "TeamPoolNode", "GradePoolNode", "CommonPoolNode",
            "MonthlyOffNode", "WeeklyOffNode", "OffWindowNode",
            "CarryoverTransitionNode", "PrecepteeSyncNode",
            "WantedSubmissionNode", "WantedApplyNode",
            "NurseNode", "ShiftNode", "DayNode", "TeamNode",
            "FairnessNode", "DataQualityNode", "ConstraintNode",
            "ConflictCoreNode", "OffCapNode",
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

    # Attach ui_visible/ui_tier per node — agent/ontology consumers see all nodes;
    # UI clients can filter by ui_visible.
    final_nodes: list[dict[str, Any]] = []
    ui_visible_count = 0
    for n in nodes.values():
        ntype = str(n.get("type") or "")
        tier = _ui_tier_for_type(ntype)
        visible = tier == "high"
        n["ui_tier"] = tier
        n["ui_visible"] = visible
        if visible:
            ui_visible_count += 1
        final_nodes.append(n)

    return JSONResponse(
        {
            "nodes": final_nodes,
            "edges": pruned_edges,
            "violations": violations,
            "stats": {
                "runs_in_view": len(selected),
                "node_count": len(nodes),
                "edge_count": len(pruned_edges),
                "violation_count": len(violations),
                "ui_visible_node_count": ui_visible_count,
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
            # 생성 시각: graph_export.run.generated_at 또는 run_dir 이름에서 파싱
            _gen_at = (d.get("run") or {}).get("generated_at")
            if not _gen_at:
                try:
                    _parts = r["dir"].split("-")
                    if len(_parts) >= 2:
                        _dp, _tp = _parts[-2], _parts[-1]
                        if len(_dp) == 8 and len(_tp) == 6:
                            _gen_at = (
                                f"{_dp[:4]}-{_dp[4:6]}-{_dp[6:]}T"
                                f"{_tp[:2]}:{_tp[2:4]}:{_tp[4:]}"
                            )
                except Exception:
                    pass
            if base_node is not None and _gen_at:
                base_node["attrs"]["_generated_at"] = _gen_at
                base_node["attrs"]["_run_dir"] = r["dir"]
            run_drilldown = {
                "attempts": attempts,
                "metrics": rr.get("metrics") or {},
                "drilldown": rr.get("drilldown") or {},
                "nurse_index_map": d.get("nurse_index_map") or {},
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

    # nurse_index_map: 이 노드가 등장한 run들 중 가장 최신 run의 매핑 사용
    # (같은 group이면 통상 동일하지만 ConflictCoreNode가 여러 run에 걸친 경우 보강용)
    _nurse_idx_map: dict[str, dict] = {}
    for r in runs:
        rdir = r["dir"]
        rid = (r["data"].get("run") or {}).get("run_id")
        if rid and rid in [inc.get("run_id") for inc in incidences]:
            nim = r["data"].get("nurse_index_map") or {}
            if nim:
                _nurse_idx_map = nim  # latest wins

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
            "nurse_index_map": _nurse_idx_map,
        }
    )


CONFLICT_CATALOG: dict[str, dict[str, Any]] = {
    "cpsat_mus:fixed_assignment:fixed_wanted": {
        "title": "원티드 고정 셀 충돌",
        "what_ko": "승인/고정 원티드 셀이 다른 하드 제약과 동시에 만족되지 않습니다.",
        "action_ko": "해당 원티드 고정을 해제하거나, 충돌하는 하드 제약(연속근무/야간상한/전이금지)을 완화하세요.",
        "action_target": "fixed_wanted",
        "adjustable": True,
    },
    "cpsat_mus:fixed_assignment:weekly_off": {
        "title": "주휴(weekly OFF) 고정 셀 충돌",
        "what_ko": "주휴로 고정된 OFF 셀이 다른 하드 제약과 동시에 만족되지 않습니다.",
        "action_ko": "주휴 정책 또는 충돌하는 하드 제약(연속근무/야간상한/전이금지)을 조정하세요.",
        "action_target": "weekly_off",
        "adjustable": True,
    },
    "cpsat_mus:fixed_assignment:recovery_2n2off": {
        "title": "2N→2OFF 회복 고정 셀 충돌",
        "what_ko": "2N 후 2OFF 회복으로 고정된 OFF 셀이 다른 하드 제약과 충돌합니다.",
        "action_ko": "two_offs_after_two_nig 설정 또는 충돌하는 하드 제약을 완화하세요.",
        "action_target": "two_offs_after_two_nig",
        "adjustable": True,
    },
    "cpsat_mus:fixed_assignment:recovery_3n2off": {
        "title": "3N→2OFF 회복 고정 셀 충돌",
        "what_ko": "3N 후 2OFF 회복으로 고정된 OFF 셀이 다른 하드 제약과 충돌합니다.",
        "action_ko": "two_offs_after_three_nig 설정 또는 충돌하는 하드 제약을 완화하세요.",
        "action_target": "two_offs_after_three_nig",
        "adjustable": True,
    },
    "cpsat_mus:fixed_assignment:recovery_off": {
        "title": "회복 OFF 고정 셀 충돌",
        "what_ko": "회복 OFF로 분류된 고정 셀이 다른 하드 제약과 동시에 만족되지 않습니다.",
        "action_ko": "회복 OFF 관련 정책(2N/3N 회복) 또는 충돌하는 하드 제약을 완화하세요.",
        "action_target": "recovery_off",
        "adjustable": True,
    },
    "cpsat_mus:fixed_assignment:special": {
        "title": "특수/휴가 고정 셀 충돌",
        "what_ko": "휴가·공가·특수 요청으로 고정된 셀이 다른 하드 제약과 충돌합니다.",
        "action_ko": "해당 특수/휴가 고정을 조정하거나, 충돌하는 하드 제약을 완화하세요.",
        "action_target": "special_fixed",
        "adjustable": True,
    },
    "cpsat_mus:fixed_assignment:off_fixed": {
        "title": "OFF 고정 셀 충돌",
        "what_ko": "일반 OFF 고정 셀이 다른 하드 제약과 동시에 만족되지 않습니다.",
        "action_ko": "해당 OFF 고정을 조정하거나, 충돌하는 하드 제약을 완화하세요.",
        "action_target": "fixed_off",
        "adjustable": True,
    },
    "cpsat_mus:fixed_assignment:manual": {
        "title": "수동 고정 셀 충돌",
        "what_ko": "수동으로 고정된 셀이 다른 하드 제약과 동시에 만족되지 않습니다.",
        "action_ko": "해당 고정 셀을 조정하거나, 충돌하는 하드 제약을 완화하세요.",
        "action_target": "fixed_assignment",
        "adjustable": True,
    },
    "cpsat_mus:fixed_assignment": {
        "title": "고정 셀 충돌",
        "what_ko": "고정 셀이 다른 하드 제약과 동시에 만족되지 않습니다.",
        "action_ko": "해당 고정 셀 또는 충돌하는 하드 제약을 조정하세요.",
        "action_target": "fixed_assignment",
        "adjustable": True,
    },
    "cpsat_mus:not_one_night": {
        "title": "야간 배정 규칙 과부하",
        "what_ko": "야간 단독 금지·월간 N상한·OFF상한·회복 규칙이 동시에 너무 좁습니다.",
        "action_ko": "월간 야간 상한을 1~2 늘리거나, 야간 단독 금지 규칙을 완화하세요.",
        "action_target": "night_cap",
        "adjustable": True,
    },
    "cpsat_mus:monthly_limit_n": {
        "title": "월간 야간 횟수 고정값 충돌",
        "what_ko": "월간 야간 정확값(N_exact) 제약이 다른 OFF 제약과 동시에 맞지 않습니다.",
        "action_ko": "해당 간호사의 월간 야간 횟수 고정 제약을 해제하세요.",
        "action_target": "n_exact",
        "adjustable": True,
    },
    "cpsat_mus:coverage_min": {
        "title": "최소 인원 미달 충돌",
        "what_ko": "특정 시프트의 최소 인원 기준이 현재 배정 가능 인원보다 많습니다.",
        "action_ko": "해당 시프트의 최소 인원 기준을 낮추거나, 배정 가능 간호사를 추가하세요.",
        "action_target": "coverage_min",
        "adjustable": True,
    },
    "n_only_vs_caps": {
        "title": "N전담 + OFF 상한 불가능 조합",
        "what_ko": "N 전담 설정인데 OFF 여유가 없어 어떤 배정도 불가합니다.",
        "action_ko": "해당 간호사의 N전담 지정을 해제하거나, OFF 상한을 높이세요.",
        "action_target": "nurse_role",
        "adjustable": True,
    },
    "cpsat_mus:max_night": {
        "title": "월간 야간 상한 초과 충돌",
        "what_ko": "월간 야간 상한이 다른 제약(연속근무, OFF 등)과 맞지 않습니다.",
        "action_ko": "월간 야간 상한을 1~2 늘리거나, 연속 야간 제한을 완화하세요.",
        "action_target": "night_cap",
        "adjustable": True,
    },
    # ── 전월(carryover) 기반 구조적 제약 — 변경 불가 ──
    # 진단 가치는 보존하되, agent가 잘못 "전월 기록 무시"를 제안하지 않도록
    # action_target=NON_ADJUSTABLE로 라우팅 차단.
    "carryover_prev_n_tail": {
        "title": "전월 야간 후속 (구조적, 조정 불가)",
        "what_ko": "전월 마지막에 N 시프트가 있어 이번 달 초의 회복 OFF가 강제됩니다.",
        "action_ko": "전월 기록은 변경할 수 없습니다. 같은 충돌의 다른 제약(OFF 상한·이번달 야간 상한)을 조정하세요.",
        "action_target": "NON_ADJUSTABLE",
        "adjustable": False,
    },
    "carryover_prev_consec_work": {
        "title": "전월 연속근무 (구조적, 조정 불가)",
        "what_ko": "전월부터 이어진 연속근무가 이번 달 최대 연속근무 한도에 직접 영향을 미칩니다.",
        "action_ko": "전월 기록은 변경할 수 없습니다. 이번 달 최대 연속근무 한도를 늘리거나 첫 주 OFF 배치를 조정하세요.",
        "action_target": "NON_ADJUSTABLE",
        "adjustable": False,
    },
    "carryover_prev_off_tail": {
        "title": "전월 OFF 꼬리 (구조적, 조정 불가)",
        "what_ko": "전월 마지막 OFF 패턴이 이번 달 초의 배정 가능성을 제한합니다.",
        "action_ko": "전월 기록은 변경할 수 없습니다. 같은 충돌의 다른 멤버를 조정하세요.",
        "action_target": "NON_ADJUSTABLE",
        "adjustable": False,
    },
}

# nurse_role_constrained는 운영상 실제 문제가 아니므로 operator card 제외
_CONFLICT_CATALOG_SKIP_PATTERNS = {"nurse_role_constrained"}

# 조정 불가 패턴: 단독으로는 operator card 생성하지 않음.
# (같은 MUS의 다른 adjustable 멤버가 있을 때만 진단 보조로 노출)
_CONFLICT_CATALOG_NON_ADJUSTABLE_PATTERNS = {
    "carryover_prev_n_tail",
    "carryover_prev_consec_work",
    "carryover_prev_off_tail",
}


_FIXED_ASSIGNMENT_DISPLAY_MAP: dict[str, str] = {
    "fixed_wanted": "fixed_wanted",
    "weekly_off": "weekly_off",
    "recovery_2n2off": "recovery_2n2off",
    "recovery_3n2off": "recovery_3n2off",
    "recovery_off": "recovery_off",
    "special": "special_fixed",
    "off_fixed": "off_fixed",
    "manual": "fixed_cell",
}


def _to_display_pattern(pattern: str) -> str:
    p = str(pattern or "")
    if p.startswith("cpsat_mus:fixed_assignment:"):
        src = p.split(":")[-1]
        return _FIXED_ASSIGNMENT_DISPLAY_MAP.get(src, src)
    return p


_GUIDANCE_META: dict[str, dict[str, str]] = {
    "supply_policy_gap": {
        "title_ko": "수요/정책 요구 과밀",
        "kind": "root",
        "why_ko": "coverage/team/grade 최소 요구가 현재 가능한 인력/배치 조합보다 큽니다.",
        "change_ko": "최소 요구치를 현실화하거나, 해당 shift를 소화할 수 있는 인력 풀을 늘리세요.",
    },
    "eligibility_gap": {
        "title_ko": "배치 가능 인력 자격 부족",
        "kind": "root",
        "why_ko": "allowed shift, initial forbidden, role 제한 때문에 필요한 shift에 들어갈 후보 자체가 부족합니다.",
        "change_ko": "허용 시프트/역할 제약을 재검토하거나, 특정 간호사의 금지 규칙을 완화하세요.",
    },
    "fixed_assignment_pressure": {
        "title_ko": "고정 셀 압력",
        "kind": "pressure",
        "why_ko": "고정 OFF/원티드/특수 고정이 남은 배치 선택지를 줄이고 있습니다.",
        "change_ko": "고정 셀을 유지해야 하는지 먼저 확인하고, 가능하면 일부 고정을 예외 처리하세요.",
    },
    "sequence_pressure": {
        "title_ko": "시퀀스 제약 압력",
        "kind": "pressure",
        "why_ko": "연속근무·전이금지·월간 N 상한·회복 OFF 규칙이 남은 배치 경로를 더 좁히고 있습니다.",
        "change_ko": "전역 완화보다, 특정 cohort의 연속근무/전이/야간 상한을 국소적으로 조정하세요.",
    },
    "boundary_transition_pressure": {
        "title_ko": "경계/회복창 전이 압력",
        "kind": "pressure",
        "why_ko": "전월 tail·회복 OFF 창(OffWindow)과 전이금지 규칙이 함께 걸려 일반 nod/noe 완화만으로는 풀리지 않는 구간입니다.",
        "change_ko": "단순 전이금지 토글보다, 경계일 carryover·회복 OFF 창·해당 구간 고정 셀을 함께 검토하세요.",
    },
    "coupling_pressure": {
        "title_ko": "연동 제약 압력",
        "kind": "pressure",
        "why_ko": "프리셉티/페어링/인계 같은 연동 규칙이 개별 feasible 조합을 묶고 있습니다.",
        "change_ko": "동반/연동이 꼭 필요한 구간만 유지하고 나머지는 범위를 좁히세요.",
    },
    "soft_tradeoff": {
        "title_ko": "자동 완화/소프트 희생 발생",
        "kind": "tradeoff",
        "why_ko": "하드 infeasible을 피하거나 품질을 높이기 위해 일부 제약이 soft fallback 또는 완화 상태로 처리되었습니다.",
        "change_ko": "이 결과를 확정하기 전에 어떤 hard 규칙이 soft로 내려갔는지 검토하세요.",
    },
}


def _guidance_category_for_core(core: dict[str, Any]) -> str | None:
    p = str(core.get("pattern") or "")
    member_types = {str(t) for t in (core.get("member_types") or [])}
    if p.startswith("pool_shortage:"):
        return "supply_policy_gap"
    if p in {
        "n_only_vs_caps",
        "nurse_role_constrained",
    } or p.endswith("initial_forbidden"):
        return "eligibility_gap"
    if p.startswith("cpsat_mus:fixed_assignment"):
        return "fixed_assignment_pressure"
    if "transition_ban" in p and "OffWindowNode" in member_types:
        return "boundary_transition_pressure"
    if any(tok in p for tok in (
        "max_consecutive_work",
        "transition_ban",
        "monthly_limit_n",
        "not_one_night",
        "recovery_2n2off",
        "recovery_3n2off",
        "recovery_off",
        "off_window",
        "consecutive_night",
    )):
        return "sequence_pressure"
    if "preceptee" in p or "pair" in p or "handoff" in p:
        return "coupling_pressure"
    if any(tok in p for tok in (
        "coverage_min",
        "team_min",
        "grade_min",
    )):
        return "supply_policy_gap"
    return None


def _collect_tradeoff_signals(target: dict[str, Any]) -> list[dict[str, Any]]:
    graph_signals = list((target.get("data") or {}).get("tradeoff_signals") or [])
    if graph_signals:
        return graph_signals
    rr = target.get("run_result") or {}
    out: list[dict[str, Any]] = []
    for attempt in (rr.get("runs") or []):
        inf = attempt.get("infeasible_detail") or {}
        applied = list(attempt.get("applied_relaxations") or inf.get("applied_relaxations") or [])
        summary = attempt.get("summary_message_ko") or inf.get("summary_message_ko")
        if not applied and not summary:
            continue
        out.append(
            {
                "run_index": attempt.get("run_index"),
                "attempt": f"{attempt.get('solver_status') or '?'}#{attempt.get('run_index')}",
                "solver_status": attempt.get("solver_status"),
                "used_fallback": bool(attempt.get("used_fallback") or False),
                "outcome": attempt.get("outcome"),
                "applied_relaxations": applied,
                "summary_message_ko": summary,
            }
        )
    return out


def _build_operator_guidance(
    *,
    all_cores: list[dict[str, Any]],
    tradeoff_signals: list[dict[str, Any]],
) -> dict[str, Any]:
    grouped: dict[str, dict[str, Any]] = {}
    for core in all_cores:
        cat = _guidance_category_for_core(core)
        if not cat:
            continue
        meta = _GUIDANCE_META[cat]
        bucket = grouped.setdefault(
            cat,
            {
                "category_id": cat,
                "title_ko": meta["title_ko"],
                "kind": meta["kind"],
                "why_ko": meta["why_ko"],
                "change_ko": meta["change_ko"],
                "evidence_patterns": [],
                "evidence_patterns_raw": [],
                "sample_core_ids": [],
                "affected_nurse_ids": set(),
                "max_affected_count": 0,
                "evidence_core_count": 0,
                "sample_hints": [],
            },
        )
        bucket["evidence_core_count"] += 1
        patt = _to_display_pattern(core.get("pattern") or "")
        raw = core.get("pattern") or ""
        if patt not in bucket["evidence_patterns"]:
            bucket["evidence_patterns"].append(patt)
        if raw not in bucket["evidence_patterns_raw"]:
            bucket["evidence_patterns_raw"].append(raw)
        cid = core.get("node_id") or core.get("core_id") or ""
        if cid and cid not in bucket["sample_core_ids"] and len(bucket["sample_core_ids"]) < 5:
            bucket["sample_core_ids"].append(cid)
        bucket["max_affected_count"] = max(bucket["max_affected_count"], int(core.get("affected_count") or 0))
        for nid in (core.get("affected_nurse_ids") or []):
            bucket["affected_nurse_ids"].add(str(nid))
        for hint in (core.get("resolution_hints") or [])[:2]:
            msg = hint.get("human_message_ko")
            if msg and msg not in bucket["sample_hints"] and len(bucket["sample_hints"]) < 4:
                bucket["sample_hints"].append(msg)

    guidance_items: list[dict[str, Any]] = []
    for cat in ("supply_policy_gap", "eligibility_gap", "fixed_assignment_pressure", "boundary_transition_pressure", "sequence_pressure", "coupling_pressure"):
        bucket = grouped.get(cat)
        if not bucket:
            continue
        bucket["affected_nurse_ids"] = sorted(bucket["affected_nurse_ids"])
        guidance_items.append(bucket)

    soft_items: list[dict[str, Any]] = []
    if tradeoff_signals:
        seen_relax = []
        for sig in tradeoff_signals:
            for relax in (sig.get("applied_relaxations") or []):
                if relax not in seen_relax:
                    seen_relax.append(relax)
        meta = _GUIDANCE_META["soft_tradeoff"]
        soft_items.append(
            {
                "category_id": "soft_tradeoff",
                "title_ko": meta["title_ko"],
                "kind": meta["kind"],
                "why_ko": meta["why_ko"],
                "change_ko": meta["change_ko"],
                "applied_relaxations": seen_relax,
                "attempts": tradeoff_signals,
            }
        )

    summary_parts = []
    if guidance_items:
        top = guidance_items[0]
        summary_parts.append(f"우선 원인 축은 '{top['title_ko']}' 입니다")
    if soft_items:
        summary_parts.append("일부 run에서는 soft fallback/완화도 함께 고려해야 합니다")

    return {
        "root_and_pressure": guidance_items,
        "soft_tradeoffs": soft_items,
        "summary_ko": ". ".join(summary_parts) + ("." if summary_parts else ""),
    }


def _render_operator_card(core: dict[str, Any], priority: int) -> dict[str, Any]:
    """conflict core 1개를 운영자용 한국어 카드로 변환."""
    pattern = core.get("pattern") or ""
    # pattern이 "cpsat_mus:xxx" 형태일 수 있어 catalog key와 일치시킴
    catalog = CONFLICT_CATALOG.get(pattern) or {}
    affected = core.get("affected_nurse_ids") or []
    affected_count = core.get("affected_count") or 0
    scope_keys = core.get("affected_scope_keys") or []
    # stale-data fallback: 구버전 run은 cell-scope 코어를 multi_nurse로 잘못 묶고
    # affected_nurse_ids=[], per_nurse_cores=[N개] 형태로 저장했음. per_nurse_cores
    # 길이로 affected_count 보정.
    if affected_count == 0 and not affected and not scope_keys:
        per_nurse = core.get("per_nurse_cores") or []
        per_member = core.get("per_member_cores") or []
        fallback_count = len(per_nurse) + len(per_member)
        if fallback_count > 0:
            affected_count = fallback_count
            scope_keys = [
                str(c).replace("conflict:cpsat:", "")
                for c in (per_nurse + per_member)
            ]

    # scope-aware unit: cell/grade → "건", nurse → "명"
    unit = "명"
    is_cell_scope = bool(
        scope_keys
        or pattern.endswith("coverage_min")
        or pattern.endswith("coverage_max")
        or "day_" in str(core.get("node_id", ""))
    ) and not affected
    if is_cell_scope:
        unit = "건"

    if affected_count > 1 and affected:
        scope_msg = f"{affected_count}명 동시 영향"
    elif affected_count > 1 and is_cell_scope:
        scope_msg = f"{affected_count}건의 정책 동시 영향"
    elif len(affected) == 1:
        scope_msg = f"간호사 {affected[0]} 단독"
    elif affected_count == 1:
        scope_msg = f"1{unit} 단독"
    else:
        scope_msg = "전역"

    # adjustable: catalog 우선, 없으면 패턴 멤버십으로 판단, 둘 다 없으면 True
    if "adjustable" in catalog:
        adjustable = bool(catalog["adjustable"])
    else:
        adjustable = pattern not in _CONFLICT_CATALOG_NON_ADJUSTABLE_PATTERNS

    return {
        "priority": priority,
        "title": catalog.get("title") or pattern,
        "scope_msg": scope_msg,
        "affected_count": affected_count,
        "affected_nurse_ids": affected,
        "what_ko": catalog.get("what_ko") or core.get("human_message_ko") or "",
        "action_ko": catalog.get("action_ko") or "",
        "detail": core.get("conclusion") or "",
        "action_target": catalog.get("action_target") or "",
        "adjustable": adjustable,
        "pattern": _to_display_pattern(pattern),
        "pattern_raw": pattern,
        "pattern_candidates": core.get("pattern_candidates") or [],
        "node_id": core.get("node_id") or "",
        # causal_layer 는 dashboard 가 root vs cascade 분리 렌더할 때 사용
        "causal_layer": core.get("causal_layer") or "unknown",
        "per_layer_counts": core.get("per_layer_counts") or {},
    }


def _make_operator_summary_ko(
    multi_nurse: list,
    individual: list,
    global_infeas: list,
) -> str:
    parts = []
    if multi_nurse:
        top = multi_nurse[0]
        # cell-scope 코어(coverage_min/max)는 "건", nurse-scope는 "명"
        affected_ids = top.get("affected_nurse_ids") or []
        scope_keys = top.get("affected_scope_keys") or []
        per_member = top.get("per_member_cores") or []
        per_nurse = top.get("per_nurse_cores") or []
        cnt = top.get("affected_count") or 0
        if not affected_ids and (scope_keys or per_member or (per_nurse and cnt == 0)):
            # cell/policy 단위 코어 — 정상 또는 stale 둘 다 처리
            display_cnt = cnt if cnt > 0 else (len(scope_keys) or len(per_member) or len(per_nurse))
            parts.append(
                f"가장 광범위한 충돌은 '{_to_display_pattern(top['pattern'])}' "
                f"({display_cnt}건의 정책 동시 영향)"
            )
        else:
            parts.append(
                f"가장 광범위한 충돌은 '{_to_display_pattern(top['pattern'])}' "
                f"({cnt}명 동시 영향)"
            )
    if individual:
        parts.append(f"개별 충돌 {len(individual)}건 (nurse별 단독 MUS)")
    if global_infeas:
        parts.append(f"전역 infeasibility 신호 {len(global_infeas)}건")
    if not parts:
        return "이 run에서 충돌 코어가 탐지되지 않았습니다."
    return ". ".join(parts) + "."


@router.get("/conflict_summary")
def conflict_summary(
    run_id: str = Query("", description="run_id to analyze (omit for most-recent run)"),
    nurse_id: str = Query("", description="optional nurse_id for perspective card"),
) -> JSONResponse:
    """3계층 conflict 분해 + 운영자용 한국어 요약 + (선택) nurse 관점 카드.

    Layers:
        individual_nurse_cores — scope=nurse, affected_count=1
        multi_nurse_cores      — scope=multi_nurse (same pattern, N nurses)
        global_infeasibility   — non-nurse scope or data_quality / no_assignment
    """
    runs = _scan_runs()
    target: dict[str, Any] | None = None
    if run_id:
        for r in runs:
            if (r["data"].get("run") or {}).get("run_id") == run_id:
                target = r
                break
    if target is None:
        if not runs:
            raise HTTPException(status_code=404, detail="no runs found")
        target = runs[-1]  # fallback: most-recent

    d = target["data"]
    meta = d.get("run") or {}
    core_details = _core_details_by_id(target)
    # nurse idx → {nurse_id, name} 매핑 (harness가 생성 시 주입)
    nurse_index_map: dict[str, dict] = d.get("nurse_index_map") or {}

    def _nurse_label(idx_or_id: Any) -> str:
        """idx(0/1/...) 또는 nurse_id 문자열을 'idx (이름)' 형태로 변환."""
        key = str(idx_or_id)
        entry = nurse_index_map.get(key) or {}
        name = entry.get("name")
        nid = entry.get("nurse_id")
        if name:
            return f"{key} ({name})"
        # 혹시 key가 nurse_id 형태로 들어왔다면 역방향 매칭
        for _idx, _meta in nurse_index_map.items():
            if str(_meta.get("nurse_id")) == key:
                _nm = _meta.get("name")
                return f"{_idx} ({_nm})" if _nm else key
        return key

    conflict_nodes = [
        n for n in (d.get("nodes") or [])
        if n.get("type") == "ConflictCoreNode"
    ]

    individual: list[dict[str, Any]] = []
    multi_nurse_list: list[dict[str, Any]] = []
    global_infeas: list[dict[str, Any]] = []

    for n in conflict_nodes:
        a = n.get("attrs") or {}
        detailed_core = core_details.get(str(a.get("core_id") or n.get("id") or "")) or {}
        scope = str(a.get("scope") or "")
        pattern = str(a.get("pattern") or "")
        node_id = n.get("id") or ""
        affected_count = int(a.get("affected_count") or 0)

        is_global = (
            scope in ("global", "data_quality")
            or "no_assignment" in pattern
            or "infeasibility" in pattern
            or "data_quality" in node_id
        )
        is_multi = scope == "multi_nurse" or (scope == "nurse" and affected_count > 1)

        core: dict[str, Any] = {
            "node_id": node_id,
            "core_id": a.get("core_id") or node_id,
            "pattern": pattern,
            "pattern_display": _to_display_pattern(pattern),
            "member_types": [
                str((m or {}).get("type") or "")
                for m in ((detailed_core.get("members") or a.get("members") or []))
                if str((m or {}).get("type") or "")
            ],
            "scope": scope,
            "affected_count": affected_count,
            "affected_nurse_ids": a.get("affected_nurse_ids") or [],
            "conclusion": a.get("conclusion"),
            "human_message_ko": a.get("human_message_ko"),
            "resolution_hints": a.get("resolution_hints") or [],
            "solver_phase": a.get("solver_phase"),
            "per_nurse_cores": a.get("per_nurse_cores") or [],
            "source": a.get("source"),
            # causal layer (root vs cascade) — hard_assumption.derive_core_layer 가 세팅
            "causal_layer": a.get("causal_layer") or "unknown",
            "per_layer_counts": a.get("per_layer_counts") or {},
            "pattern_candidates": detailed_core.get("pattern_candidates") or a.get("pattern_candidates") or [],
        }
        if is_global:
            global_infeas.append(core)
        elif is_multi:
            multi_nurse_list.append(core)
        else:
            individual.append(core)

    multi_nurse_list.sort(key=lambda x: -(x["affected_count"] or 0))
    individual.sort(key=lambda x: -(x["affected_count"] or 0))

    # TOP-N 원인 (pattern 단위 dedupe)
    all_cores = multi_nurse_list + individual + global_infeas
    top_causes: list[dict[str, Any]] = []
    seen_patterns: set[str] = set()
    for c in all_cores:
        p = c["pattern"]
        if p in seen_patterns:
            continue
        seen_patterns.add(p)
        layer = (
            "multi_nurse" if c in multi_nurse_list
            else "individual" if c in individual
            else "global"
        )
        hints = c.get("resolution_hints") or []
        top_causes.append({
            "pattern": _to_display_pattern(p),
            "pattern_raw": p,
            "affected_count": c["affected_count"],
            "human_message_ko": c["human_message_ko"],
            "top_hint": hints[0] if hints else None,
            "layer": layer,
        })

    # Nurse 관점 카드
    nurse_perspective: dict[str, Any] | None = None
    if nurse_id:
        nid = str(nurse_id)
        cohort_cores = [
            c for c in multi_nurse_list
            if nid in [str(x) for x in (c.get("affected_nurse_ids") or [])]
        ]
        solo_cores = [
            c for c in individual
            if nid in [str(x) for x in (c.get("affected_nurse_ids") or [])]
        ]
        if cohort_cores or solo_cores:
            label_parts = []
            if cohort_cores:
                label_parts.append(f"{len(cohort_cores)}개 집계 충돌 코어의 cohort 소속")
            if solo_cores:
                label_parts.append(f"{len(solo_cores)}개 단독 충돌의 원인")
            nurse_perspective = {
                "nurse_id": nid,
                "is_in_cohort": bool(cohort_cores),
                "is_solo": bool(solo_cores),
                "cohort_cores": cohort_cores,
                "solo_cores": solo_cores,
                "summary_ko": f"간호사 {nid}는 " + " + ".join(label_parts) + ".",
            }
        else:
            nurse_perspective = {
                "nurse_id": nid,
                "is_in_cohort": False,
                "is_solo": False,
                "cohort_cores": [],
                "solo_cores": [],
                "summary_ko": (
                    f"간호사 {nid}는 이 run의 충돌 코어에 직접 등장하지 않습니다."
                ),
            }

    total_affected = len(set(
        str(x)
        for c in all_cores
        for x in (c.get("affected_nurse_ids") or [])
    ))

    tradeoff_signals = _collect_tradeoff_signals(target)
    operator_guidance = _build_operator_guidance(
        all_cores=all_cores,
        tradeoff_signals=tradeoff_signals,
    )
    fix_plan = _latest_fix_plan(target)
    fix_plan_links = _build_fix_plan_links(target, fix_plan)

    # operator_cards: 우선순위 정렬 후 skip 패턴 제외, catalog 기반 카드 생성
    cardable = [
        c for c in all_cores
        if (c.get("pattern") or "") not in _CONFLICT_CATALOG_SKIP_PATTERNS
        and (c.get("scope") != "multi_nurse" or (c.get("affected_count") or 0) > 0)
    ]
    # 정렬 순 (root 가 가장 위, cascade 는 아래):
    #   1. causal_layer rank: policy(0) → data(1) → personal(2) → structural(3) → unknown(4)
    #   2. adjustable=True 가 먼저 (운영자가 실제로 조정 가능한 것 우선)
    #   3. multi_nurse(영향 큰 순) → individual(nurse) → global
    #   4. affected_count 내림차순
    _CAUSAL_LAYER_RANK = {
        "policy": 0, "data": 1, "personal": 2, "structural": 3, "unknown": 4,
    }

    def _card_sort_key(c: dict) -> tuple:
        pattern = c.get("pattern") or ""
        catalog = CONFLICT_CATALOG.get(pattern) or {}
        if "adjustable" in catalog:
            adjustable = bool(catalog["adjustable"])
        else:
            adjustable = pattern not in _CONFLICT_CATALOG_NON_ADJUSTABLE_PATTERNS
        adj_rank = 0 if adjustable else 1
        scope = c.get("scope") or ""
        scope_rank = 0 if scope == "multi_nurse" else 1 if scope == "nurse" else 2
        layer_rank = _CAUSAL_LAYER_RANK.get(c.get("causal_layer") or "unknown", 4)
        return (layer_rank, adj_rank, scope_rank, -(c.get("affected_count") or 0))

    cardable.sort(key=_card_sort_key)
    operator_cards = [
        _render_operator_card(c, i + 1)
        for i, c in enumerate(cardable)
    ]

    # run 생성 시각: graph_export["run"]["generated_at"] 또는 run_dir 이름에서 파싱
    generated_at = meta.get("generated_at")
    if not generated_at:
        # run-YYYYMM-YYYYMMDD-HHMMSS 형태에서 마지막 토큰 추출
        try:
            parts = target["dir"].split("-")
            if len(parts) >= 2:
                date_part = parts[-2]  # YYYYMMDD
                time_part = parts[-1]  # HHMMSS
                if len(date_part) == 8 and len(time_part) == 6:
                    generated_at = (
                        f"{date_part[:4]}-{date_part[4:6]}-{date_part[6:]}T"
                        f"{time_part[:2]}:{time_part[2:4]}:{time_part[4:]}"
                    )
        except Exception:
            pass

    # operator_cards에 nurse 이름 보강
    for card in operator_cards:
        ids = card.get("affected_nurse_ids") or []
        if ids and nurse_index_map:
            card["affected_nurse_labels"] = [_nurse_label(x) for x in ids]
            # scope_msg에도 첫 번째 이름 살짝 노출 (간호사 X 단독 케이스)
            if len(ids) == 1:
                card["scope_msg"] = f"간호사 {_nurse_label(ids[0])} 단독"
            elif len(ids) > 1 and len(ids) <= 6:
                # 적은 cohort는 이름 나열
                card["scope_msg"] = (
                    f"{card.get('affected_count')}명 — "
                    + ", ".join(_nurse_label(x) for x in ids[:6])
                )

    return JSONResponse({
        "run_dir": target["dir"],
        "run_id": meta.get("run_id"),
        "generated_at": generated_at,
        "total_conflict_nodes": len(conflict_nodes),
        "layers": {
            "individual_nurse_cores": individual,
            "multi_nurse_cores": multi_nurse_list,
            "global_infeasibility": global_infeas,
        },
        "operator_summary": {
            "top_causes": top_causes[:5],
            "structural_conflict_count": len(seen_patterns),
            "total_affected_nurses": total_affected,
            "summary_ko": _make_operator_summary_ko(
                multi_nurse_list, individual, global_infeas
            ),
        },
        "operator_guidance": operator_guidance,
        "fix_plan": fix_plan,
        "fix_plan_links": fix_plan_links,
        "fix_plan_context": {
            "run_id": meta.get("run_id"),
            "generated_at": generated_at,
        },
        "operator_cards": operator_cards,
        "nurse_perspective": nurse_perspective,
        "nurse_index_map": nurse_index_map,
    })


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
    #app { display:grid; grid-template-columns: 300px 1fr 420px; grid-template-rows: 56px 1fr; height:100vh; }
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
    .preset-row { display:flex; gap:6px; flex-wrap:wrap; margin-top:6px; }
    .preset-btn { font-size:11px; padding:4px 8px; border-radius:999px; border:1px solid var(--border); background:var(--panel); color:var(--text); cursor:pointer; }
    .preset-btn:hover { background:var(--hover); }
    /* ── Canvas overlay ─────────────────── */
    #canvas-wrap { position:relative; }
    #cy { width:100%; height:100%; background:var(--bg); }
    .canvas-hint { position:absolute; left:50%; top:14px; transform:translateX(-50%); background:rgba(18,26,51,.85); border:1px solid var(--border); border-radius:8px; padding:6px 14px; font-size:11px; color:var(--muted); pointer-events:none; }
    /* ── Detail panel ──────────────────── */
    #detail { padding:0; border-left:1px solid var(--border); overflow:auto; font-size:12px; background:var(--bg2); }
    .d-empty { padding:24px; color:var(--muted); font-size:13px; text-align:center; line-height:1.5; }
    .d-empty .icon { font-size:28px; margin-bottom:6px; }
    .d-overview { padding:10px 12px; border-bottom:1px solid var(--border); background:linear-gradient(180deg, rgba(96,165,250,.08), transparent); }
    .ov-grid { display:grid; grid-template-columns:1fr 1fr; gap:8px; }
    .ov-card { background:var(--panel); border:1px solid var(--border); border-radius:8px; padding:8px; }
    .ov-card b { display:block; font-size:10px; color:var(--muted); margin-bottom:4px; }
    .ov-card span { font-size:16px; font-weight:700; }
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
    .collapsible-head { display:flex; align-items:center; justify-content:space-between; gap:8px; cursor:pointer; margin-bottom:6px; }
    .collapsible-toggle { font-size:11px; color:var(--muted); }
    .collapsed .collapsible-body { display:none; }
    .collapsible-wrap { padding:12px 14px; border-bottom:1px solid var(--border); }
    .collapsible-btn { all:unset; display:flex; align-items:center; justify-content:space-between; width:100%; cursor:pointer; }
    .collapsible-btn:focus-visible { outline:2px solid var(--accent); border-radius:6px; }
    /* ── Conflict layer analysis ─────────────────────────── */
    .layer-badge { display:inline-flex; align-items:center; gap:4px; padding:2px 9px; border-radius:999px; font-size:11px; font-weight:600; margin:2px 0 6px; }
    .layer-badge.individual { background:rgba(251,191,36,.15); color:#fbbf24; border:1px solid rgba(251,191,36,.4); }
    .layer-badge.multi_nurse { background:rgba(232,121,249,.15); color:#e879f9; border:1px solid rgba(232,121,249,.4); }
    .layer-badge.global { background:rgba(239,68,68,.15); color:#fca5a5; border:1px solid rgba(239,68,68,.4); }
    .cause-item { background:var(--panel); border:1px solid var(--border); border-left:3px solid; border-radius:6px; padding:8px 10px; margin:5px 0; cursor:pointer; }
    .cause-item:hover { background:var(--hover); }
    .cause-item.multi_nurse { border-left-color:#e879f9; }
    .cause-item.individual  { border-left-color:#fbbf24; }
    .cause-item.global      { border-left-color:#ef4444; }
    .cause-item.non-adjustable { opacity:0.85; border-left-color:#64748b !important; background:rgba(100,116,139,.08); }
    .non-adj-badge { display:inline-flex; padding:2px 8px; border-radius:999px; font-size:10px; background:rgba(100,116,139,.18); color:#cbd5e1; border:1px solid rgba(100,116,139,.4); font-weight:600; }
    .cause-rank { font-size:14px; font-weight:700; color:var(--muted); margin-right:4px; }
    .nurse-persp-card { background:var(--panel); border:1px solid var(--border); border-left:3px solid #60a5fa; border-radius:6px; padding:10px 12px; margin:6px 0; }
    .nurse-persp-card.cohort { border-left-color:#e879f9; }
    .nurse-persp-card.solo   { border-left-color:#fbbf24; }
    .nurse-persp-input { display:flex; gap:6px; margin:8px 0; }
    .nurse-persp-input input { flex:1; background:var(--panel); color:var(--text); border:1px solid var(--border); border-radius:6px; padding:5px 8px; font-size:12px; }
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
      <button class="btn"        id="btnShowAllNodes"   title="저신호 노드(메트릭/풀/맥락) 까지 모두 표시. 기본은 사용자 핵심 노드만.">🔧 전체노드</button>
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
      <div class="preset-row" id="quick-presets">
        <button class="preset-btn" data-preset="fail-focus">실패 집중</button>
        <button class="preset-btn" data-preset="core-trace">코어 추적</button>
        <button class="preset-btn" data-preset="full-reset">전체 보기</button>
      </div>
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
  TeamPoolNode:'#fb7185', GradePoolNode:'#f87171', CommonPoolNode:'#fda4af',
  GradeMaxNode:'#fbbf24', OffWindowNode:'#86efac', PrecepteeSyncNode:'#d8b4fe',
  WantedSubmissionNode:'#60a5fa', WantedApplyNode:'#34d399',
  // Conflict core + member types
  ConflictCoreNode:'#e879f9',   // 자주색 — 다중 충돌 코어
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
  showAllNodes:false,  // default: 사용자 핵심 노드(ui_visible=true)만 그래프 표시
  cy:null, facets:null, allRules:[], reloadPending:false, catalog:{}, selectedNodeId:'',
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
  TeamPoolNode:5, GradePoolNode:5, CommonPoolNode:5,
  OffWindowNode:6, PrecepteeSyncNode:6,
  OffCapNode:6, NightCapNode:6, MonthlyNExactNode:6,
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

    // UI 가시성 필터: 기본은 ui_visible=true 만, "전체노드" 토글 시 모두.
    const showAll = !!state.showAllNodes;
    const visibleNodes = g.nodes.filter(n => showAll || n.ui_visible !== false);
    const positions = computePresetPositions(visibleNodes);
    const nodeIds = new Set(visibleNodes.map(n => n.id));
    const elements = [];
    for (const n of visibleNodes) {
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
    // 선택 유지: 리로드 후에도 기존 노드를 유지하고, 없으면 첫 RunNode 자동 선택
    let targetId = state.selectedNodeId;
    if (!targetId || !nodeIds.has(targetId)) {
      const firstRun = (g.nodes || []).find(n => n.type === 'RunNode');
      targetId = firstRun ? firstRun.id : ((g.nodes || [])[0] ? g.nodes[0].id : '');
    }
    if (targetId) {
      const node = cy.getElementById(targetId);
      if (node && node.length) {
        const nb = node.closedNeighborhood();
        cy.elements().addClass('dim').removeClass('focus');
        nb.addClass('focus').removeClass('dim');
      }
      await showNodeDetail(targetId);
    } else {
      $('#detail').innerHTML = '<div class="d-empty"><div class="icon">🧭</div>표시할 노드가 없습니다.</div>';
    }
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
    'OffCapNode','NightCapNode','MonthlyNExactNode',
    'CoverageMinNode','CoverageMaxNode',
    'TeamMinNode','GradeMinNode','GradeMaxNode',
    'TeamPoolNode','GradePoolNode','CommonPoolNode',
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
    TeamPoolNode:'팀 풀(시프트 가용)', GradePoolNode:'Grade 풀(시프트 가용)', CommonPoolNode:'공통 풀(무소속)',
    GradeMinNode:'숙련도 최소', GradeMaxNode:'숙련도 최대',
    MonthlyOffNode:'월간 OFF 제약', WeeklyOffNode:'주휴 제약',
    OffWindowNode:'OFF 구간', PrecepteeSyncNode:'프리셉티 동반',
    CarryoverTransitionNode:'전월 전이', WantedSubmissionNode:'원티드 제출',
    WantedApplyNode:'원티드 반영', NurseNode:'간호사',
    ShiftNode:'시프트', DayNode:'일자', TeamNode:'팀',
    FairnessNode:'공정성', DataQualityNode:'데이터 정합성',
    // Conflict core members
    ConflictCoreNode:'⚡ 충돌 코어',
    OffCapNode:'OFF 상한',
    NightCapNode:'월간 N 상한',
    MonthlyNExactNode:'월 N exact',
    ConstraintNode:'기타 제약',
  };
  return M[t] || t || '?';
}

function createCollapsibleSection(section, title, badgeText, opened = true) {
  const headBtn = el('button', { class:'collapsible-btn', type:'button', 'aria-expanded': String(opened) });
  const head = el('div', { class:'collapsible-head' });
  const titleWrap = el('h3', {}, title);
  if (badgeText) titleWrap.append(el('span', { class:'badge' }, badgeText));
  const toggle = el('span', { class:'collapsible-toggle' }, opened ? '접기 ▲' : '펼치기 ▼');
  head.append(titleWrap, toggle);
  headBtn.append(head);
  const body = el('div', { class:'collapsible-body' });
  const wrap = el('div', { class:'collapsible-wrap' });
  wrap.append(headBtn, body);
  if (!opened) wrap.classList.add('collapsed');
  headBtn.addEventListener('click', () => {
    const collapsed = wrap.classList.toggle('collapsed');
    headBtn.setAttribute('aria-expanded', String(!collapsed));
    toggle.textContent = collapsed ? '펼치기 ▼' : '접기 ▲';
  });
  section.append(wrap);
  return body;
}

function syncToolbarState() {
  document.querySelectorAll('[data-layer]').forEach(b => b.classList.toggle('active', state.layers.has(b.dataset.layer)));
  document.querySelectorAll('[data-severity]').forEach(b => b.classList.toggle('active', state.severities.has(b.dataset.severity)));
  document.querySelectorAll('[data-status]').forEach(b => b.classList.toggle('active', state.status === b.dataset.status));
}

function renderDetailOverview(container, info) {
  const box = el('div', { class:'d-overview' });
  const grid = el('div', { class:'ov-grid' });
  const items = [
    ['원인 노드', info.inboundCount],
    ['영향 노드', info.outboundCount],
    ['관련 위반', info.violationCount],
    ['등장 실행', info.incidenceCount],
  ];
  for (const [k, v] of items) {
    const card = el('div', { class:'ov-card' });
    card.append(el('b', {}, k), el('span', {}, String(v || 0)));
    grid.append(card);
  }
  box.append(grid);
  container.append(box);
}

async function showNodeDetail(id) {
  state.selectedNodeId = id;
  const d = $('#detail');
  d.innerHTML = '<div class="d-empty"><div class="icon">⏳</div>불러오는 중…</div>';
  try {
    const j = await fetchJSON('/ontology/node/' + encodeURIComponent(id));
    d.innerHTML = '';
    const n = j.node;

    renderDetailOverview(d, {
      inboundCount: (j.inbound_neighbors || []).length,
      outboundCount: (j.outbound_neighbors || []).length,
      violationCount: (j.related_violations || []).length,
      incidenceCount: j.incidence_count || 0,
    });

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
    const _nurseMap = j.nurse_index_map || {};
    function _nurseName(idx) {
      const e = _nurseMap[String(idx)];
      return e && e.name ? e.name : null;
    }
    for (const [k, v] of Object.entries(n.attrs || {})) {
      if (typeof v === 'object' || k.startsWith('_')) continue;
      kv.append(el('b', {}, k));
      // nurse_id 필드: idx → 이름 자동 첨부
      let display = String(v);
      if (k === 'nurse_id' && _nurseMap && _nurseName(v)) {
        display = `${v} (${_nurseName(v)})`;
      }
      kv.append(el('span', {}, display));
    }
    // affected_nurse_ids: idx 배열 → 이름 첨부 한 줄
    const _aff = (n.attrs || {}).affected_nurse_ids;
    if (Array.isArray(_aff) && _aff.length && _nurseMap && Object.keys(_nurseMap).length) {
      kv.append(el('b', {}, 'affected_nurses (이름)'));
      const labeled = _aff.map(x => {
        const nm = _nurseName(x);
        return nm ? `${x} (${nm})` : String(x);
      }).join(', ');
      kv.append(el('span', {}, labeled));
    }
    if (n.type === 'RunNode') {
      if (n.attrs._generated_at) {
        kv.append(el('b', {}, '🕒 생성시각'));
        const ga = String(n.attrs._generated_at);
        const ageMs = Date.now() - Date.parse(ga + (ga.endsWith('Z') ? '' : 'Z'));
        let ageStr = '';
        if (!isNaN(ageMs) && ageMs >= 0) {
          const h = Math.floor(ageMs / 3600000);
          const m = Math.floor((ageMs % 3600000) / 60000);
          ageStr = h > 0 ? ` (${h}시간 전)` : m > 0 ? ` (${m}분 전)` : ' (방금)';
        }
        kv.append(el('span', {}, ga + ageStr));
      }
      if (n.attrs._run_dir) { kv.append(el('b', {}, 'run_dir')); kv.append(el('span', { style:'font-family:monospace; font-size:11px;' }, n.attrs._run_dir)); }
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

    // ── 1.55 RunNode → Conflict 분석 버튼 ──
    if (n.type === 'RunNode') {
      const runId = n.id.replace(/^run:/, '');
      const cfSec = el('div', { class:'d-section drilldown' });
      const cfHdr = el('h3', {}, '🔬 Conflict 분석');
      cfHdr.append(el('span', { class:'h-desc', style:'display:inline; margin-left:6px;' }, '3계층 충돌 코어 + 운영자 요약'));
      cfSec.append(cfHdr);
      const cfBody = el('div');
      const cfMeta = el('div', { class:'h-desc', style:'margin:4px 0 8px;' }, '선택한 실행의 대표 원인을 자동으로 표시합니다.');
      cfSec.append(cfMeta);
      cfSec.append(cfBody);
      await showConflictSummary(runId, cfBody);
      d.append(cfSec);
    }

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
      // ── layer badge (개별 / 집계 / 전역) ──
      {
        const scope = n.attrs.scope || '';
        const afc   = n.attrs.affected_count || 0;
        let lbl, lcls;
        if (scope === 'multi_nurse') {
          lbl = '👥 집계 코어 (multi_nurse)'; lcls = 'multi_nurse';
        } else if (scope === 'nurse' && afc <= 1) {
          lbl = '👤 개별 코어 (individual)';  lcls = 'individual';
        } else {
          lbl = '🌐 전역/데이터품질';           lcls = 'global';
        }
        sec.append(el('span', { class:`layer-badge ${lcls}` }, lbl));
      }
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
      const body = createCollapsibleSection(sec, '← 원인 노드', String(j.inbound_neighbors.length), false);
      body.append(el('div', { class:'h-desc' }, '이 노드를 만든 상위 노드들 (CAUSES_VIOLATION, BLOCKED_RUN, RISKY_FOR 등). 카드 클릭 → 그 노드로 이동.'));
      body.append(renderNeighborList(j.inbound_neighbors));
      d.append(sec);
    }

    // ── 3. 영향 (outbound) — 파란 stripe ──
    if (j.outbound_neighbors && j.outbound_neighbors.length) {
      const sec = el('div', { class:'d-section effect' });
      const body = createCollapsibleSection(sec, '→ 영향 노드', String(j.outbound_neighbors.length), false);
      body.append(el('div', { class:'h-desc' }, '이 노드가 가리키는 하위 노드들 (FAILED_RULE, OBSERVED_IN, RUN_ON 등 outbound edge).'));
      body.append(renderNeighborList(j.outbound_neighbors));
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
      const body = createCollapsibleSection(sec, '⚠️ 관련 위반', `${j.related_violations.length}건 / ${gList.length}그룹`, true);
      body.append(el('div', { class:'h-desc' }, '(rule_id, 월) 단위로 묶음. 하단 "n개 run 펼치기" 버튼으로 attempt별 보기.'));
      for (const g of gList) body.append(renderGroupedViolationCard(g));
      d.append(sec);
    }

    // ── 5. 메트릭 추이 ──
    if (j.metric_history && j.metric_history.length) {
      const sec = el('div', { class:'d-section' });
      const body = createCollapsibleSection(sec, '📈 메트릭 추이', '', false);
      body.append(el('div', { class:'h-desc' }, '같은 메트릭이 여러 run에서 보인 값.'));
      const tbl = el('div', { class:'kv' });
      for (const h of j.metric_history.slice(-12)) {
        tbl.append(el('b', {}, `${h.month_key || '?'} · ${(h.run_id || '').slice(-10)}`));
        tbl.append(el('span', {}, String(h.value)));
      }
      body.append(tbl);
      d.append(sec);
    }

    // ── 6. Run drilldown — 파란 stripe ──
    if (j.run_drilldown) {
      const rd = j.run_drilldown;
      const sec = el('div', { class:'d-section drilldown' });
      const body = createCollapsibleSection(sec, '🧩 Run 상세 — attempts · 셀', '', false);
      body.append(el('div', { class:'h-desc' }, 'solver attempts와 over/under cell 표. fallback이 일어난 attempt는 ⚠.'));
      const at = el('div', { class:'kv' });
      for (const a of (rd.attempts || [])) {
        at.append(el('b', {}, `attempt #${a.run_index || '?'}`));
        const fb = (a.solver_status || '').includes('fallback') ? ' ⚠' : '';
        at.append(el('span', {}, `${a.solver_status || '-'}${fb} · schedule=${a.schedule_id || '-'} · ${a.timing_ms || '?'}ms`));
      }
      body.append(at);
      const dd = rd.drilldown || {};
      const over = dd.coverage_over_cells || [];
      const under = dd.coverage_under_cells || [];
      if (over.length) {
        body.append(el('div', { class:'h-desc', style:'margin-top:8px;' }, `Over cells (정원 초과) · ${over.length}건`));
        body.append(renderCellTable(over, 'over'));
      }
      if (under.length) {
        body.append(el('div', { class:'h-desc', style:'margin-top:8px;' }, `Under cells (정원 미달) · ${under.length}건`));
        body.append(renderCellTable(under, 'shortage'));
      }
      d.append(sec);
    }

    // ── 7. 등장한 run 리스트 ──
    if (j.incidences && j.incidences.length) {
      const sec = el('div', { class:'d-section' });
      const body = createCollapsibleSection(sec, `📁 등장한 실행`, String(j.incidence_count), false);
      const ul = el('div');
      for (const inc of j.incidences.slice(0, 30)) {
        ul.append(el('div', { class:'badge' }, `${inc.month_key || '?'} · ${inc.strategy || ''} · ${(inc.run_id || '').slice(-10)}`));
      }
      body.append(ul);
      d.append(sec);
    }
  } catch (err) {
    d.innerHTML = `<div class="d-empty"><div class="icon">⚠️</div>오류: ${err.message}</div>`;
  }
}

// ── Conflict analysis helpers ──────────────────────────────────────────

function renderCauseItem(cause, rank) {
  const lcls = cause.layer === 'multi_nurse' ? 'multi_nurse'
    : cause.layer === 'global' ? 'global' : 'individual';
  const card = el('div', { class:`cause-item ${lcls}` });
  const head = el('div', { style:'display:flex; align-items:center; gap:8px; margin-bottom:4px;' });
  head.append(el('span', { class:'cause-rank' }, `#${rank}`));
  const lbl = lcls === 'multi_nurse' ? '👥 집계' : lcls === 'global' ? '🌐 전역' : '👤 개별';
  head.append(el('span', { class:`layer-badge ${lcls}` }, lbl));
  if ((cause.affected_count || 0) > 1) {
    head.append(el('span', { class:'badge' }, `${cause.affected_count}명 영향`));
  }
  card.append(head);
  card.append(el('div', { style:'font-size:12px; font-weight:600;' }, cause.pattern || ''));
  if (cause.pattern_candidates && cause.pattern_candidates.length) {
    card.append(el('div', { style:'font-size:10px; color:var(--muted); margin-top:2px;' },
      `candidates: ${cause.pattern_candidates.join(', ')}`));
  }
  if (cause.human_message_ko) {
    card.append(el('div', { style:'font-size:11px; color:#cbd5e1; margin-top:3px;' }, cause.human_message_ko));
  }
  if (cause.top_hint) {
    const hintMsg = cause.top_hint.human_message_ko || cause.top_hint.action || '';
    if (hintMsg) {
      card.append(el('div', { class:'badge', style:'display:block; margin-top:6px; color:#86efac; border-color:#22c55e; line-height:1.4;' },
        `✅ ${hintMsg}`));
    }
  }
  return card;
}

function renderNursePerspectiveCard(persp) {
  if (!persp) return el('div');
  const pcls = persp.is_in_cohort ? 'cohort' : persp.is_solo ? 'solo' : '';
  const card = el('div', { class:`nurse-persp-card ${pcls}` });
  card.append(el('div', { style:'font-size:13px; font-weight:700; margin-bottom:5px;' },
    `👤 Nurse ${persp.nurse_id} 관점`));
  card.append(el('div', { style:'font-size:12px; color:#cbd5e1; margin-bottom:8px; line-height:1.4;' },
    persp.summary_ko));
  if (persp.is_in_cohort && persp.cohort_cores.length) {
    card.append(el('div', { style:'font-size:11px; color:var(--muted); margin:4px 0 2px;' },
      `📌 소속 집계 코어 (${persp.cohort_cores.length}건):`));
    for (const c of persp.cohort_cores) {
      const link = el('div', { class:'badge', style:'display:block; cursor:pointer; margin:2px 0; border-color:#e879f9; color:#f0abfc;' },
        `${c.node_id} · ${c.affected_count}명`);
      link.addEventListener('click', () => showNodeDetail(c.node_id));
      card.append(link);
    }
  }
  if (persp.is_solo && persp.solo_cores.length) {
    card.append(el('div', { style:'font-size:11px; color:var(--muted); margin:6px 0 2px;' },
      `⚡ 단독 충돌 코어 (${persp.solo_cores.length}건):`));
    for (const c of persp.solo_cores) {
      const link = el('div', { class:'badge', style:'display:block; cursor:pointer; margin:2px 0; border-color:#fbbf24; color:#fde68a; line-height:1.4;' },
        `${c.node_id}\\n${c.conclusion || ''}`);
      link.addEventListener('click', () => showNodeDetail(c.node_id));
      card.append(link);
    }
  }
  return card;
}

function renderConflictSummaryPanel(data, container) {
  container.innerHTML = '';
  const panel = el('div', { class:'conflict-summary-panel' });

  // 헤더
  const hdr = el('h3', { style:'margin:0 0 6px; font-size:14px; padding:12px 14px 0;' }, '⚡ 생성 실패 원인 및 해결 방법');
  const opsm = data.operator_summary || {};
  if (opsm.total_affected_nurses != null) {
    hdr.append(el('span', { class:'badge', style:'margin-left:8px; font-size:10px;' },
      `영향 ${opsm.total_affected_nurses}명 · 충돌 ${opsm.structural_conflict_count}종`));
  }
  panel.append(hdr);

  // fix plan 요약 (NO_ASSIGNMENT 분해 + 링크 + tier/axis/stage v2)
  const fp = data.fix_plan || null;
  if (fp) {
    const fpSec = el('div', { style:'padding:8px 14px 10px;' });
    fpSec.append(el('div', { style:'font-size:11px; color:var(--muted); margin-bottom:4px;' },
      `🧭 Fix plan (${fp.reason_source || 'inferred'} · ${fp.plan_mode || '-'})`));

    // failure_stage badge (S0~S4 or unknown)
    if (fp.failure_stage && fp.failure_stage !== 'unknown') {
      const stageBadge = el('div', { style:'display:flex; align-items:center; gap:6px; margin-bottom:6px;' });
      stageBadge.append(el('span', { class:'badge', style:'background:#1e293b; color:#93c5fd; border:1px solid #334155;' },
        `🪜 ${fp.failure_stage_label_ko || fp.failure_stage}`));
      fpSec.append(stageBadge);
    }

    // tier_summary (T0/T1/T2/T3 4-mini badge)
    if (fp.tier_summary) {
      const t = fp.tier_summary;
      const tWrap = el('div', { style:'display:flex; gap:4px; margin-bottom:6px; font-size:10px;' });
      const tierColors = {
        T0: { bg:'#7f1d1d', fg:'#fecaca' }, // 절대 hard / 데이터 정비
        T1: { bg:'#713f12', fg:'#fde68a' }, // 안전 hard / 절대 풀지마
        T2: { bg:'#1e3a8a', fg:'#bfdbfe' }, // 운영 hard / 풀 수 있음
        T3: { bg:'#374151', fg:'#d1d5db' }, // 품질 soft
      };
      const tierLabels = { T0:'절대', T1:'안전', T2:'운영', T3:'품질' };
      for (const tier of ['T0','T1','T2','T3']) {
        const n = Number(t[tier] || 0);
        if (n <= 0) continue;
        const c = tierColors[tier];
        tWrap.append(el('span', { style:`background:${c.bg}; color:${c.fg}; padding:2px 7px; border-radius:10px; font-weight:600;` },
          `${tier} ${tierLabels[tier]} ${n}`));
      }
      if (tWrap.children.length > 0) fpSec.append(tWrap);
    }

    // data_correction_required (T0 banner)
    if (fp.data_correction_required) {
      const fams = (fp.data_correction_families || []).join(', ') || 'ConfigIntegrity';
      const banner = el('div', {
        style:'background:#7f1d1d; color:#fee2e2; padding:6px 9px; border-radius:6px; font-size:11px; margin-bottom:6px;',
      }, `⛔ 데이터 정비 필요 (${fams}): ${fp.data_correction_message_ko || '입력 데이터를 직접 정비하세요.'}`);
      fpSec.append(banner);
    }

    // protected_axes (T1)
    if ((fp.protected_axes || []).length) {
      fpSec.append(el('div', { style:'font-size:10px; color:#fbbf24; margin:6px 0 3px;' },
        `🔒 절대 풀지 마세요 (T1)`));
      const pWrap = el('div', { style:'display:flex; flex-direction:column; gap:3px; padding-left:8px;' });
      for (const pa of fp.protected_axes) {
        pWrap.append(el('div', { style:'font-size:11px; color:#fde68a;' },
          `• ${pa.label_ko} (${pa.family})`));
      }
      fpSec.append(pWrap);
    }

    // axis_actions (T2 권고, max 5)
    if ((fp.axis_actions || []).length) {
      fpSec.append(el('div', { style:'font-size:10px; color:#93c5fd; margin:8px 0 3px;' },
        `🛠 풀 수 있는 룰 (T2) — 우선순위 순`));
      const aWrap = el('div', { style:'display:flex; flex-direction:column; gap:6px;' });
      for (const ax of fp.axis_actions) {
        const card = el('div', {
          style:'background:#0f1a35; border:1px solid #1e3a8a; border-radius:5px; padding:6px 8px;',
        });
        const head = el('div', { style:'display:flex; align-items:center; gap:6px; margin-bottom:3px;' });
        head.append(el('span', { class:'badge', style:'background:#1e3a8a; color:#bfdbfe;' },
          `${ax.priority || '?'}`));
        head.append(el('span', { style:'font-size:11px; color:#cbd5e1;' }, ax.axis_id || ''));
        head.append(el('span', { style:'font-size:10px; color:#64748b;' },
          `${ax.family || ''} · prio=${ax.relaxation_priority ?? '-'}`));
        card.append(head);
        if (ax.human_message_ko) {
          card.append(el('div', { style:'font-size:11px; color:#e2e8f0; line-height:1.5;' }, ax.human_message_ko));
        }
        const tgts = ax.targets || [];
        if (tgts.length) {
          const tline = tgts.slice(0, 3).map(t => {
            if (t.pool_id) return `${t.pool_id}(부족${t.shortage ?? '?'})`;
            if (t.day) return `${t.day}일 ${t.shift || ''}`;
            return JSON.stringify(t);
          }).join(', ');
          card.append(el('div', { style:'font-size:10px; color:#94a3b8; margin-top:3px;' },
            `📍 ${tline}${tgts.length > 3 ? ` 외 ${tgts.length - 3}건` : ''}`));
        }
        aWrap.append(card);
      }
      fpSec.append(aWrap);
      if (Number(fp.axis_actions_truncated || 0) > 0) {
        fpSec.append(el('div', { style:'font-size:10px; color:#64748b; margin-top:4px;' },
          `(${fp.axis_actions_truncated}건 더 있음 — 상위 ${fp.axis_actions_cap || 5}개만 표시)`));
      }
    }

    // Legacy no_assignment_breakdown (요약 배지)
    if (fp.no_assignment_breakdown && fp.no_assignment_breakdown.length) {
      const row = el('div', { style:'display:flex; flex-wrap:wrap; gap:6px; margin-top:6px;' });
      for (const k of fp.no_assignment_breakdown) {
        row.append(el('span', { class:'badge' }, `NO_ASSIGNMENT/${k}`));
      }
      fpSec.append(row);
    }
    const links = data.fix_plan_links || [];
    if (links.length) {
      const lwrap = el('div', { style:'display:flex; flex-direction:column; gap:4px; margin-top:6px;' });
      for (const lk of links.slice(0, 8)) {
        const txt = `${lk.action_id} → ${lk.pool_id} (shortage=${lk.shortage ?? '?'})`;
        const tone = lk.pool_node_exists ? '#86efac' : '#fca5a5';
        lwrap.append(el('div', { style:`font-size:11px; color:${tone};` }, txt));
      }
      fpSec.append(lwrap);
    }
    panel.append(fpSec);
  }

  // 운영자 카드 (주 UI) — causal_layer 기반 root vs cascade 분리 렌더
  const cards = data.operator_cards || [];

  // 대표 원인 그룹(원인 중심 요약) — 기본 노출
  function buildCanonicalCauseGroups(items) {
    const m = new Map();
    for (const c of (items || [])) {
      const sig = [
        c.causal_layer || 'unknown',
        c.pattern_raw || c.pattern || '-',
        c.action_target || '-',
        c.scope_msg || '-',
      ].join('||');
      if (!m.has(sig)) {
        m.set(sig, {
          signature: sig,
          title: c.title || c.pattern || '원인',
          causal_layer: c.causal_layer || 'unknown',
          pattern: c.pattern || c.pattern_raw || '-',
          action_target: c.action_target || '-',
          scope_msg: c.scope_msg || '',
          count: 0,
          affected_max: 0,
          samples: [],
        });
      }
      const g = m.get(sig);
      g.count += 1;
      g.affected_max = Math.max(g.affected_max || 0, Number(c.affected_count || 0));
      if (g.samples.length < 3 && c.node_id) g.samples.push(c.node_id);
    }
    return [...m.values()].sort((a, b) => {
      if (b.count !== a.count) return b.count - a.count;
      return (b.affected_max || 0) - (a.affected_max || 0);
    });
  }

  if (cards.length) {
    const groups = buildCanonicalCauseGroups(cards);
    const gSec = el('div', { style:'padding:0 14px 8px;' });
    const gWrap = el('div');
    const gBody = createCollapsibleSection(
      gWrap,
      `🧭 대표 원인 그룹 (${groups.length}개)` ,
      '',
      true,
    );
    for (const g of groups.slice(0, 8)) {
      const item = el('div', { class:'cause-item', style:'margin-bottom:8px;' });
      const top = el('div', { style:'display:flex; align-items:center; gap:6px; flex-wrap:wrap; margin-bottom:4px;' });
      top.append(el('span', { class:'badge' }, `빈도 ${g.count}`));
      top.append(el('span', { class:'badge' }, `${g.causal_layer}`));
      if (g.scope_msg) top.append(el('span', { class:'badge' }, g.scope_msg));
      if ((g.affected_max || 0) > 1) top.append(el('span', { class:'badge' }, `최대 영향 ${g.affected_max}명`));
      item.append(top);
      item.append(el('div', { style:'font-size:12px; font-weight:700;' }, g.title));
      item.append(el('div', { style:'font-size:11px; color:var(--muted); margin-top:2px;' }, `pattern: ${g.pattern}`));
      if (g.samples.length) {
        const samples = el('div', { style:'font-size:10px; color:var(--muted); margin-top:5px;' },
          `sample nodes: ${g.samples.map(x => x.slice(-16)).join(', ')}`);
        item.append(samples);
      }
      gBody.append(item);
    }
    gSec.append(gWrap);
    panel.append(gSec);
  }
  // 카드 1장을 DOM 으로 렌더 (root/cascade 양쪽이 공유)
  function _renderCauseCard(c) {
    const isNonAdj = c.adjustable === false || c.action_target === 'NON_ADJUSTABLE';
    const lcls = c.pattern && (c.pattern.includes('multi') || (c.affected_count > 1))
      ? 'multi_nurse'
      : c.action_target === 'nurse_role' || c.action_target === 'n_exact'
      ? 'individual'
      : 'global';
    const cardCls = `cause-item ${lcls}` + (isNonAdj ? ' non-adjustable' : '');
    const card = el('div', { class:cardCls, style:'cursor:default;' });

    // causal_layer badge — 한 눈에 root/cascade 인지 보이게
    const layer = c.causal_layer || 'unknown';
    const LAYER_BADGE = {
      policy:    { icon:'💥', text:'POLICY ROOT', bg:'rgba(239,68,68,.2)', fg:'#fca5a5', bd:'#ef4444' },
      data:      { icon:'📊', text:'DATA ROOT',   bg:'rgba(245,158,11,.2)',fg:'#fcd34d', bd:'#f59e0b' },
      personal:  { icon:'👤', text:'PERSONAL',    bg:'rgba(96,165,250,.18)',fg:'#93c5fd', bd:'#3b82f6' },
      structural:{ icon:'⚙️', text:'CASCADE',     bg:'rgba(148,163,184,.18)',fg:'#cbd5e1', bd:'#64748b' },
      unknown:   { icon:'❔', text:'UNKNOWN',     bg:'rgba(148,163,184,.18)',fg:'#cbd5e1', bd:'#64748b' },
    };
    const lb = LAYER_BADGE[layer] || LAYER_BADGE.unknown;

    // 제목 행
    const titleRow = el('div', { style:'display:flex; align-items:center; gap:8px; margin-bottom:6px; flex-wrap:wrap;' });
    titleRow.append(el('span', { class:'cause-rank' }, `#${c.priority}`));
    titleRow.append(el('span', {
      style:`padding:1px 7px; font-size:10px; font-weight:700; border-radius:4px;
             background:${lb.bg}; color:${lb.fg}; border:1px solid ${lb.bd};`,
    }, `${lb.icon} ${lb.text}`));
    titleRow.append(el('span', { style:'font-size:13px; font-weight:700;' }, c.title));
    if (c.scope_msg) titleRow.append(el('span', { class:'badge' }, c.scope_msg));
    if (c.pattern_candidates && c.pattern_candidates.length) {
      titleRow.append(el('span', { class:'badge' }, `cands:${c.pattern_candidates.length}`));
    }
    if (isNonAdj) titleRow.append(el('span', { class:'non-adj-badge' }, '🔒 조정 불가'));
    card.append(titleRow);

    // per_layer_counts breakdown — cascade 케이스에서 "structural 7개 + personal 2개" 표기
    const plc = c.per_layer_counts || {};
    const keys = Object.keys(plc);
    if (keys.length > 1) {
      const order = ['policy','data','personal','structural','unknown'];
      const parts = order
        .filter(k => plc[k])
        .map(k => `${(LAYER_BADGE[k]||{}).icon||''} ${k} ×${plc[k]}`);
      card.append(el('div', { style:'font-size:10px; color:var(--muted); margin-bottom:6px;' },
        '구성: ' + parts.join(' · ')));
    }

    // 문제 설명
    if (c.what_ko) {
      card.append(el('div', { style:'font-size:12px; color:#cbd5e1; margin-bottom:4px; line-height:1.5;' },
        `🔍 ${c.what_ko}`));
    }
    if (c.pattern_candidates && c.pattern_candidates.length) {
      card.append(el('div', { style:'font-size:10px; color:var(--muted); margin-bottom:5px;' },
        `pattern candidates: ${c.pattern_candidates.join(', ')}`));
    }
    if (c.detail) {
      card.append(el('div', { style:'font-size:11px; color:var(--muted); font-family:monospace; margin-bottom:6px;' },
        c.detail));
    }
    if (c.action_ko) {
      const actionStyle = isNonAdj
        ? 'display:block; padding:7px 10px; color:#cbd5e1; border-color:#64748b; background:rgba(100,116,139,.12); font-size:12px; line-height:1.5;'
        : 'display:block; padding:7px 10px; color:#86efac; border-color:#22c55e; font-size:12px; line-height:1.5;';
      card.append(el('div', { class:'badge', style:actionStyle },
        (isNonAdj ? '🔒 안내: ' : '✅ 해결: ') + c.action_ko));
    }
    if (c.node_id) {
      const link = el('div', { style:'font-size:10px; color:var(--muted); margin-top:5px; cursor:pointer; text-decoration:underline;' },
        `→ 충돌 코어 상세 보기 (${c.node_id.slice(-30)})`);
      link.addEventListener('click', () => showNodeDetail(c.node_id));
      card.append(link);
    }
    return card;
  }

  if (cards.length) {
    const detailWrapOuter = el('div', { style:'padding:0 14px;' });
    const detailSec = el('div');
    const detailBody = createCollapsibleSection(
      detailSec,
      `🔍 상세 충돌 카드 (${cards.length}건)` ,
      '',
      false,
    );

    const rootCards    = cards.filter(c => (c.causal_layer === 'policy' || c.causal_layer === 'data'));
    const cascadeCards = cards.filter(c => !(c.causal_layer === 'policy' || c.causal_layer === 'data'));

    // 💥 Root 섹션 — 항상 펼침
    if (rootCards.length) {
      const rootHeader = el('div', {
        style:'margin:6px 14px 4px; font-size:11px; font-weight:700; color:#fca5a5; letter-spacing:.5px;',
      }, `💥 ROOT — 정책/데이터 근본 원인 (${rootCards.length}건)`);
      detailBody.append(rootHeader);
      const rootWrap = el('div');
      for (const c of rootCards) rootWrap.append(_renderCauseCard(c));
      detailBody.append(rootWrap);
    }

    // 📋 Cascade 섹션 — root 있으면 접고, 없으면 펼침 (root 없으면 cascade 가 사실상 root)
    if (cascadeCards.length) {
      const cascadeWrap = el('div', { style:'margin-top:8px;' });
      const cascadeSec = el('div');
      const cascadeBody = createCollapsibleSection(
        cascadeSec,
        rootCards.length
          ? `📋 CASCADE — 위 root 로 인해 유발된 부수 충돌 (${cascadeCards.length}건)`
          : `📋 충돌 cores (${cascadeCards.length}건)`,
        '',
        rootCards.length === 0,  // root 없으면 펼친다
      );
      for (const c of cascadeCards) cascadeBody.append(_renderCauseCard(c));
      cascadeWrap.append(cascadeSec);
      detailBody.append(cascadeWrap);
    }
    detailWrapOuter.append(detailSec);
    panel.append(detailWrapOuter);
  } else {
    panel.append(el('div', { style:'padding:14px; color:var(--muted); font-size:12px;' },
      '이 run에서 해석 가능한 충돌 원인이 없습니다.'));
  }

  // 3계층 breakdown (기술용, collapsible — 기본 접힘)
  const layers = data.layers || {};
  const layerDefs = [
    { key:'multi_nurse_cores',     label:'👥 집계(multi_nurse) 코어', cls:'multi_nurse', open:false },
    { key:'individual_nurse_cores',label:'👤 개별(individual) 코어',  cls:'individual',  open:false },
    { key:'global_infeasibility',  label:'🌐 전역 infeasibility 신호', cls:'global',      open:false },
  ];
  const hasLayers = layerDefs.some(ld => (layers[ld.key] || []).length > 0);
  if (hasLayers) {
    const techWrap = el('div', { style:'padding:0 14px;' });
    const techSec  = el('div');
    const techBody = createCollapsibleSection(techSec, '🔧 기술 상세 (3계층 분해)', '', false);
    for (const ld of layerDefs) {
      const items = layers[ld.key] || [];
      if (!items.length) continue;
      const subSec = el('div');
      const subBody = createCollapsibleSection(subSec, ld.label, String(items.length), false);
      for (const c of items) {
        const card = el('div', { class:`cause-item ${ld.cls}` });
        if ((c.affected_count || 0) > 1) card.append(el('span', { class:'badge', style:'margin-bottom:4px;' }, `${c.affected_count}명`));
        card.append(el('div', { style:'font-size:11px; font-weight:600;' }, c.pattern || c.node_id || ''));
        if (c.conclusion) card.append(el('div', { style:'font-size:10px; color:var(--muted); margin-top:2px;' }, c.conclusion));
        card.addEventListener('click', () => showNodeDetail(c.node_id));
        subBody.append(card);
      }
      techBody.append(subSec);
    }
    techWrap.append(techSec);
    panel.append(techWrap);
  }

  // Nurse 관점 입력
  const perspSec = el('div', { style:'padding:12px 14px; border-top:1px solid var(--border); margin-top:8px;' });
  perspSec.append(el('div', { style:'font-size:11px; color:var(--muted); margin-bottom:6px;' }, '👤 특정 간호사 관점 조회'));
  const perspRow = el('div', { class:'nurse-persp-input' });
  const perspInput = el('input', { type:'text', placeholder:'nurse_id (예: 13)' });
  const perspBtn = el('button', { class:'btn', style:'white-space:nowrap;' }, '관점 보기');
  const perspResult = el('div');
  perspRow.append(perspInput, perspBtn);
  perspSec.append(perspRow, perspResult);

  perspBtn.addEventListener('click', async () => {
    const nid = perspInput.value.trim();
    if (!nid) return;
    perspBtn.disabled = true;
    try {
      const url = '/ontology/conflict_summary?run_id=' + encodeURIComponent(data.run_id || '') + '&nurse_id=' + encodeURIComponent(nid);
      const res = await fetchJSON(url);
      perspResult.innerHTML = '';
      perspResult.append(renderNursePerspectiveCard(res.nurse_perspective));
    } catch(e) {
      perspResult.textContent = '오류: ' + e.message;
    } finally {
      perspBtn.disabled = false;
    }
  });
  perspInput.addEventListener('keydown', e => { if (e.key === 'Enter') perspBtn.click(); });

  // nurse_perspective가 이미 포함된 경우 바로 표시
  if (data.nurse_perspective) {
    perspInput.value = data.nurse_perspective.nurse_id || '';
    perspResult.append(renderNursePerspectiveCard(data.nurse_perspective));
  }

  panel.append(perspSec);
  container.append(panel);
}

async function showConflictSummary(runId, container) {
  container.innerHTML = '<div class="d-empty" style="padding:16px;">⏳ Conflict 분석 중…</div>';
  try {
    const data = await fetchJSON('/ontology/conflict_summary?run_id=' + encodeURIComponent(runId));
    renderConflictSummaryPanel(data, container);
  } catch (e) {
    container.innerHTML = `<div class="d-empty">⚠️ Conflict 분석 오류: ${e.message}</div>`;
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

  document.querySelectorAll('#quick-presets [data-preset]').forEach(btn => {
    btn.addEventListener('click', () => {
      const p = btn.dataset.preset;
      if (p === 'fail-focus') {
        state.status = 'FAIL';
        state.layers = new Set(['month','group','run','rule','violation']);
        state.severities = new Set(['blocking','warning']);
      } else if (p === 'core-trace') {
        state.status = 'FAIL';
        state.layers = new Set(['run','rule','cause','violation']);
        state.severities = new Set(['blocking']);
      } else {
        state.months.clear();
        state.groups.clear();
        state.strategies.clear();
        state.rules.clear();
        state.solverStatuses.clear();
        state.status = 'ALL';
        state.layers = new Set(['month','group','run','rule']);
        state.severities = new Set(['blocking','warning']);
        renderFacet('#filter-months', state.facets?.months || [], state.months);
        renderFacet('#filter-groups', state.facets?.groups || [], state.groups);
        renderFacet('#filter-strategies', state.facets?.strategies || [], state.strategies);
        renderFacet('#filter-solver', state.facets?.solver_statuses || [], state.solverStatuses);
        renderRulesList($('#filter-rule-search').value || '');
      }
      syncToolbarState();
      reloadGraph();
    });
  });

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
      state.status = b.dataset.status;
      syncToolbarState();
      reloadGraph();
    });
  });
  $('#btnFit').addEventListener('click', () => state.cy && state.cy.fit(state.cy.elements(), 40));
  const btnShowAll = $('#btnShowAllNodes');
  if (btnShowAll) {
    btnShowAll.addEventListener('click', () => {
      state.showAllNodes = !state.showAllNodes;
      if (state.showAllNodes) btnShowAll.classList.add('active');
      else btnShowAll.classList.remove('active');
      reloadGraph();
    });
  }
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
  syncToolbarState();
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
