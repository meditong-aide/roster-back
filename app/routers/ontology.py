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


def _latest_infeasible_detail(target: dict[str, Any]) -> dict[str, Any] | None:
    """가장 최근 시도의 infeasible_detail 전체 (validator_evidence_summary,
    structural_diagnosis, pool_snapshot 등 진단 패널에 필요한 부수 데이터 포함)."""
    rr = target.get("run_result") or {}
    runs = list(rr.get("runs") or [])
    for attempt in reversed(runs):
        inf = attempt.get("infeasible_detail") or {}
        if isinstance(inf, dict) and inf:
            return inf
    return None


def _latest_run_attempt_meta(target: dict[str, Any]) -> dict[str, Any]:
    """가장 최근 시도의 status_code/solver_status/used_fallback 등 메타."""
    rr = target.get("run_result") or {}
    runs = list(rr.get("runs") or [])
    if not runs:
        return {}
    a = runs[-1] or {}
    return {
        "status_code": a.get("status_code"),
        "solver_status": a.get("solver_status"),
        "outcome": a.get("outcome"),
        "used_fallback": a.get("used_fallback"),
        "applied_relaxations": a.get("applied_relaxations") or [],
        "summary_message_ko": a.get("summary_message_ko"),
        "schedule_id": a.get("schedule_id"),
    }


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
        # 진단-우선 UI 용 부수 데이터
        "diagnostic": {
            "run_meta": {
                "run_id": meta.get("run_id"),
                "group_id": meta.get("group_id"),
                "year": meta.get("year"),
                "month": meta.get("month"),
                "strategy": meta.get("strategy"),
                "generated_at": generated_at,
            },
            "attempt": _latest_run_attempt_meta(target),
            "validator_evidence_summary": (
                (_latest_infeasible_detail(target) or {}).get("validator_evidence_summary")
                or (_latest_infeasible_detail(target) or {}).get("validator_evidence")
                or {}
            ),
            "structural_diagnosis": (_latest_infeasible_detail(target) or {}).get("structural_diagnosis") or {},
            "pool_shortages": list(((_latest_infeasible_detail(target) or {}).get("pool_snapshot") or {}).get("shortages") or []),
            "violated_constraints": list((_latest_infeasible_detail(target) or {}).get("violated_constraints") or []),
            "conflict_cores": list((_latest_infeasible_detail(target) or {}).get("conflict_cores") or []),
            "preflight_issues": list((_latest_infeasible_detail(target) or {}).get("preflight_issues") or []),
        },
    })


# ── DiagnosisReport + Patterns ─────────────────────────────


def _list_runs_with_meta() -> list[dict[str, Any]]:
    """List recent runs (sorted by mtime asc) with minimal fields for the
    diagnosis tab's run dropdown."""
    rows: list[dict[str, Any]] = []
    for r in _scan_runs():
        meta = r["data"].get("run") or {}
        attempt = _latest_run_attempt_meta(r)
        rows.append(
            {
                "run_id": meta.get("run_id"),
                "group_id": meta.get("group_id"),
                "year": meta.get("year"),
                "month": meta.get("month"),
                "strategy": meta.get("strategy"),
                "status_code": attempt.get("status_code"),
                "solver_status": attempt.get("solver_status"),
                "schedule_id": attempt.get("schedule_id"),
                "fail": int(attempt.get("status_code") or 0) >= 400,
            }
        )
    return rows


def _why_analysis(*, ves: dict, fp: dict, structural: dict, attempt: dict,
                  violated: list) -> dict[str, Any]:
    """Derive a causal 'why infeasible' explanation, not just symptom numbers.

    Returns dict with:
      - headline_ko: 1~2 sentence root-cause statement (the answer to '왜?')
      - mechanism_ko: 작동 메커니즘 1~2 sentences
      - evidence_bullets: [{label, value}] tying claim to concrete signals
      - confidence: 'high' | 'med' | 'low'
    """
    total = int(ves.get("total_failed_cells") or 0)
    elig_zero = int(ves.get("eligible_zero_cells") or 0)
    fixed_cnt = int(ves.get("fixed_forbidden_count") or 0)
    carry = int(ves.get("carryover_artifact_count") or 0)
    short_total = int(ves.get("required_minus_assigned_total") or 0)
    cells = list(ves.get("top_failed_cells") or [])
    codes = {str(v.get("reason_code") or "").upper() for v in (violated or [])}
    used_fallback = bool(attempt.get("used_fallback"))
    applied = list(attempt.get("applied_relaxations") or [])

    early_days = [c for c in cells if int(c.get("day") or 0) <= 7]
    by_shift: dict[str, int] = defaultdict(int)
    elig_vs_assigned_zero = 0
    for c in cells:
        sh = str(c.get("shift") or "").upper()
        by_shift[sh] += 1
        if int(c.get("eligible") or 0) > 0 and int(c.get("assigned") or 0) == 0:
            elig_vs_assigned_zero += 1
    dominant_shift = max(by_shift.items(), key=lambda kv: kv[1])[0] if by_shift else ""
    sample_day_shift = [
        f"{c.get('day')}일 {str(c.get('shift') or '').upper()}"
        for c in cells[:5]
        if c.get("day") is not None
    ]

    # Pattern 1: ConfigIntegrity (T0)
    if fp.get("data_correction_required"):
        return {
            "headline_ko": "입력 설정 자체가 수학적으로 풀리지 않습니다.",
            "mechanism_ko": (
                "룰 완화로 해소되지 않는 영역입니다. "
                f"감지된 데이터 무결성 family: {', '.join(fp.get('data_correction_families') or ['ConfigIntegrity'])}. "
                "인력풀·요구 인원·고정 배정 같은 입력 데이터를 직접 정비해야 합니다."
            ),
            "evidence_bullets": [
                {"label": "데이터 무결성 코드", "value": ", ".join(sorted(codes & {"MID_REQUIRED_MISSING","MID_DISABLED_BUT_USED","ALLOWED_SHIFTS_ISOLATES_NURSE","FIXED_ASSIGN_EXCEEDS_NEED","FIXED_ASSIGN_VIOLATES_ALLOWED"}) or "ConfigIntegrity")},
            ],
            "confidence": "high",
        }

    # Pattern 2: eligible_zero — 진짜 인력 부족 / 마스크 고립
    if elig_zero > 0:
        return {
            "headline_ko": f"{elig_zero}개 셀에 배정 가능한 간호사가 0명입니다.",
            "mechanism_ko": (
                "이 셀들에는 시프트 마스크·휴가/공가·전월 회복 OFF 같은 hard 제약으로 "
                "그날 그 시프트를 할 수 있는 인력이 단 한 명도 없는 상태입니다. "
                "룰 완화보다는 인력 충원 또는 허용 시프트 마스크 확장이 우선입니다."
            ),
            "evidence_bullets": [
                {"label": "후보 0 셀 수", "value": str(elig_zero)},
                {"label": "실패 셀 예시", "value": " · ".join(sample_day_shift)} if sample_day_shift else {"label": "스코프", "value": "n/a"},
            ],
            "confidence": "high",
        }

    # Pattern 3: 후보는 있는데 배정 0 — 고정 잠금 우세
    sample_ratio = elig_vs_assigned_zero / max(len(cells), 1)
    if total > 10 and sample_ratio >= 0.5 and fixed_cnt >= 30:
        scope_txt = ""
        if early_days:
            mn = min(int(c.get("day") or 0) for c in early_days)
            mx = max(int(c.get("day") or 0) for c in early_days)
            scope_txt = f"{mn}~{mx}일 "
        sh_txt = f"{dominant_shift} 시프트 중심으로 " if dominant_shift else ""
        return {
            "headline_ko": (
                f"{scope_txt}{sh_txt}고정/금지 셀 {fixed_cnt}건이 후보 인력을 선점해 "
                f"솔버가 어떤 인원도 배정할 수 없었습니다."
            ),
            "mechanism_ko": (
                f"후보 자체는 충분합니다(top {len(cells)} 셀 중 후보 0인 셀 없음). "
                f"하지만 가용 인력이 다른 일자/시프트의 고정 배정(휴가·공가·원티드·N전담 등)에 묶여 있어, "
                f"{dominant_shift or 'D/E'} 시프트 최소 인원을 채울 자유 인력이 0이 되는 조합입니다. "
                f"fallback 솔버도 fixed 영역은 hard 가드라 풀지 못합니다."
            ),
            "evidence_bullets": [
                {"label": "후보 있으나 배정 0인 셀", "value": f"{elig_vs_assigned_zero}건"},
                {"label": "실패 셀 예시", "value": " · ".join(sample_day_shift) if sample_day_shift else "n/a"},
                {"label": "고정/금지 셀 총량", "value": f"{fixed_cnt}건"},
                {"label": "fallback 결과", "value": "시도됐으나 효과 0 (fixed 미우회)"} if used_fallback and not applied else {"label": "누적 미배정", "value": f"{short_total}인-셀"},
            ],
            "confidence": "high",
        }

    # Pattern 4: carryover dominant
    if carry > 0 and (early_days or "PREV_MONTH_TRANSITION" in codes):
        return {
            "headline_ko": f"전월에서 넘어온 회복/연속근무 꼬리({carry}건)가 월초 셀을 차단합니다.",
            "mechanism_ko": (
                "전월 마지막 N 블록의 회복 OFF, 전월 연속근무 잔여, 또는 전월→당월 전이 금지가 "
                "월초 1~5일 셀 후보를 사전 제거하는 상태입니다."
            ),
            "evidence_bullets": [
                {"label": "carryover artifact", "value": f"{carry}건"},
                {"label": "월초 실패 셀", "value": f"1~7일 {len(early_days)}개"} if early_days else {"label": "직접 reason", "value": "PREV_MONTH_TRANSITION"},
            ],
            "confidence": "high",
        }

    # Pattern 5: 산술 불가능 / pool 부족
    structural_codes = codes & {"CAPACITY_TOTAL_SHORTAGE","GLOBAL_DAY_CAPACITY_SHORTAGE","TEAM_MIN_EXCEEDS_GLOBAL_NEED","GRADE_MIN_SUM_EXCEEDS_NEED"}
    if structural_codes:
        return {
            "headline_ko": "요구 인원이 가용 풀을 수학적으로 초과합니다.",
            "mechanism_ko": (
                f"감지된 구조 코드: {', '.join(sorted(structural_codes))}. "
                "이는 인원 충원 또는 요구치 하향 없이는 풀 수 없는 산술 불가능 케이스입니다."
            ),
            "evidence_bullets": [
                {"label": "구조 reason codes", "value": ", ".join(sorted(structural_codes))},
                {"label": "누적 부족", "value": f"{short_total}인-셀"} if short_total else {"label": "스코프", "value": "전체"},
            ],
            "confidence": "high",
        }

    # Pattern 6: axis hints only
    axes = fp.get("axis_actions") or []
    if axes:
        names = ", ".join(a.get("axis_id") for a in axes[:3] if a.get("axis_id"))
        return {
            "headline_ko": f"{len(axes)}개 축의 조정이 권장됩니다 (top: {names}).",
            "mechanism_ko": "구체 인과 evidence는 약하지만 (conflict cores 미생성), 축 단위 완화로 재시도해 볼 수 있습니다.",
            "evidence_bullets": [{"label": f"axis {a.get('axis_id')}", "value": str(a.get("human_message_ko") or "")[:60]} for a in axes[:3]],
            "confidence": "low",
        }

    # Default — 신호 부족
    return {
        "headline_ko": "원인 신호가 부족합니다.",
        "mechanism_ko": "conflict cores·pool snapshot·validator evidence 모두 비어 있어 구체 인과를 추정할 수 없습니다. precheck 단계를 통과시킨 뒤 solver-stage infeasible 을 유도해 보세요.",
        "evidence_bullets": [],
        "confidence": "low",
    }


def _narrative_ko(*, ves: dict, fp: dict, structural: dict) -> str | None:
    """Back-compat one-liner for any legacy caller."""
    why = _why_analysis(ves=ves, fp=fp, structural=structural, attempt={}, violated=[])
    parts = [why.get("headline_ko") or "", why.get("mechanism_ko") or ""]
    s = " ".join(p for p in parts if p)
    return s or None


def _build_diagnosis_report(target: dict[str, Any]) -> dict[str, Any]:
    fp = _latest_fix_plan(target) or {}
    inf = _latest_infeasible_detail(target) or {}
    attempt = _latest_run_attempt_meta(target)
    meta = target["data"].get("run") or {}
    ves = inf.get("validator_evidence_summary") or inf.get("validator_evidence") or {}
    sd = inf.get("structural_diagnosis") or {}
    status = attempt.get("status_code")
    ok = bool(attempt.get("schedule_id")) or (isinstance(status, int) and status < 400)
    return {
        "run": {
            "run_id": meta.get("run_id"),
            "group_id": meta.get("group_id"),
            "year": meta.get("year"),
            "month": meta.get("month"),
            "strategy": meta.get("strategy"),
        },
        "verdict": {
            "ok": ok,
            "status_code": status,
            "solver_status": attempt.get("solver_status"),
            "outcome": attempt.get("outcome"),
            "used_fallback": attempt.get("used_fallback"),
            "summary_message_ko": attempt.get("summary_message_ko"),
            "schedule_id": attempt.get("schedule_id"),
        },
        "narrative_ko": _narrative_ko(ves=ves, fp=fp, structural=sd),
        "why": _why_analysis(
            ves=ves,
            fp=fp,
            structural=sd,
            attempt=attempt,
            violated=list(inf.get("violated_constraints") or []),
        ),
        "failure_stage": fp.get("failure_stage"),
        "failure_stage_label_ko": fp.get("failure_stage_label_ko"),
        "tier_summary": fp.get("tier_summary") or {"T0": 0, "T1": 0, "T2": 0, "T3": 0},
        "data_correction": {
            "required": bool(fp.get("data_correction_required")),
            "families": list(fp.get("data_correction_families") or []),
            "message_ko": fp.get("data_correction_message_ko"),
        },
        "protected_axes": list(fp.get("protected_axes") or []),
        "axis_actions": list(fp.get("axis_actions") or []),
        "axis_actions_cap": fp.get("axis_actions_cap"),
        "axis_actions_truncated": fp.get("axis_actions_truncated"),
        "evidence": {
            "total_failed_cells": int(ves.get("total_failed_cells") or 0),
            "required_minus_assigned_total": int(ves.get("required_minus_assigned_total") or 0),
            "eligible_zero_cells": int(ves.get("eligible_zero_cells") or 0),
            "fixed_forbidden_count": int(ves.get("fixed_forbidden_count") or 0),
            "carryover_artifact_count": int(ves.get("carryover_artifact_count") or 0),
            "pool_shortages_count": len(list((inf.get("pool_snapshot") or {}).get("shortages") or [])),
            "top_failed_cells": list(ves.get("top_failed_cells") or [])[:15],
        },
        "violated_reason_codes": [
            v.get("reason_code") for v in (inf.get("violated_constraints") or []) if v.get("reason_code")
        ],
        "structural_diagnosis": {
            "mode": sd.get("mode"),
            "primary_causes": list(sd.get("primary_causes") or []),
            "decision_trace": list(sd.get("decision_trace") or []),
        },
    }


@router.get("/diagnosis")
def diagnosis(
    run_id: str = Query("", description="omit for latest failure (else most-recent)"),
) -> JSONResponse:
    runs = _scan_runs()
    if not runs:
        raise HTTPException(status_code=404, detail="no runs available")
    target: dict[str, Any] | None = None
    if run_id:
        for r in runs:
            if (r["data"].get("run") or {}).get("run_id") == run_id:
                target = r
                break
        if target is None:
            raise HTTPException(status_code=404, detail=f"run_id {run_id} not found")
    else:
        # prefer most recent failure
        for r in reversed(runs):
            attempt = _latest_run_attempt_meta(r)
            if int(attempt.get("status_code") or 0) >= 400:
                target = r
                break
        if target is None:
            target = runs[-1]
    return JSONResponse(_build_diagnosis_report(target))


@router.get("/diagnosis/runs")
def diagnosis_runs() -> JSONResponse:
    """Lightweight list for the 진단 탭의 run dropdown."""
    rows = _list_runs_with_meta()
    rows.sort(key=lambda x: str(x.get("run_id") or ""), reverse=True)
    return JSONResponse({"runs": rows[:50]})


@router.get("/patterns")
def patterns(
    group_id: str = Query("", description="optional group filter"),
    limit_runs: int = Query(60, description="recent N runs to aggregate"),
) -> JSONResponse:
    """Cross-run frequency aggregation: axis, family, tier, stage, lock_type."""
    runs = _scan_runs()
    runs.sort(key=lambda r: str((r["data"].get("run") or {}).get("run_id") or ""), reverse=True)
    axis_freq: dict[str, int] = defaultdict(int)
    family_freq: dict[str, int] = defaultdict(int)
    lock_freq: dict[str, int] = defaultdict(int)
    tier_runs: dict[str, int] = defaultdict(int)
    stage_runs: dict[str, int] = defaultdict(int)
    fail_count = 0
    ok_count = 0
    processed = 0
    sample_runs: list[dict[str, Any]] = []
    for r in runs:
        meta = r["data"].get("run") or {}
        if group_id and meta.get("group_id") != group_id:
            continue
        if processed >= limit_runs:
            break
        attempt = _latest_run_attempt_meta(r)
        status = int(attempt.get("status_code") or 0)
        if status >= 400:
            fail_count += 1
        else:
            ok_count += 1
        fp = _latest_fix_plan(r) or {}
        for ax in fp.get("axis_actions") or []:
            aid = str(ax.get("axis_id") or "")
            if aid:
                axis_freq[aid] += 1
            fam = str(ax.get("family") or "")
            if fam:
                family_freq[fam] += 1
            lt = str(ax.get("lock_type") or "")
            if lt:
                lock_freq[lt] += 1
        for tier_id, n in (fp.get("tier_summary") or {}).items():
            if n:
                tier_runs[tier_id] += 1
        stage = fp.get("failure_stage")
        if stage and stage != "unknown":
            stage_runs[stage] += 1
        sample_runs.append(
            {
                "run_id": meta.get("run_id"),
                "year": meta.get("year"),
                "month": meta.get("month"),
                "group_id": meta.get("group_id"),
                "status": status,
                "stage": stage,
                "axes": [a.get("axis_id") for a in (fp.get("axis_actions") or [])],
            }
        )
        processed += 1

    def _sorted_dict(d: dict[str, int]) -> list[dict[str, Any]]:
        return [{"key": k, "count": v} for k, v in sorted(d.items(), key=lambda kv: -kv[1])]

    return JSONResponse(
        {
            "scope": {"group_id": group_id or None, "limit_runs": limit_runs, "runs_used": processed},
            "outcome": {"fail": fail_count, "ok": ok_count},
            "axis_freq": _sorted_dict(axis_freq),
            "family_freq": _sorted_dict(family_freq),
            "lock_type_freq": _sorted_dict(lock_freq),
            "tier_run_freq": _sorted_dict(tier_runs),
            "stage_run_freq": _sorted_dict(stage_runs),
            "sample_runs": sample_runs,
        }
    )


# ── Alpha case API (α 새 시스템 e2e 결과 노출) ─────────────────────────

_ALPHA_CASES_DIR = Path(__file__).resolve().parents[2] / "tools" / "harness" / "reports" / "alpha_cases"


def _list_alpha_cases() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not _ALPHA_CASES_DIR.exists():
        return out
    for p in sorted(_ALPHA_CASES_DIR.glob("*.json")):
        try:
            d = json.loads(p.read_text())
            inf = d.get("infeasibility", {}) or {}
            causes = inf.get("causes") or []
            out.append({
                "case_id": p.stem,
                "severity": inf.get("severity"),
                "cause_count": len(causes),
                "cause_ids": [c.get("reason_code") for c in causes],
                "treatment_count": len(inf.get("treatment_recommendations") or []),
            })
        except Exception:
            continue
    return out


@router.get("/cases")
def list_cases() -> JSONResponse:
    return JSONResponse({"items": _list_alpha_cases()})


@router.get("/case/{case_id}")
def get_case(case_id: str) -> JSONResponse:
    path = _ALPHA_CASES_DIR / f"{case_id}.json"
    if not path.exists():
        return JSONResponse({"error": f"case {case_id} not found"}, status_code=404)
    try:
        return JSONResponse(json.loads(path.read_text()))
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


# ── U-51: Matrix 50 cases endpoints (synthetic, no solver) ────────────────
import sys as _sys  # local import — module-level 의존성 추가 안 하려고

_MATRIX_PATH = Path(__file__).resolve().parents[2] / "tools" / "harness"
if str(_MATRIX_PATH) not in _sys.path:
    _sys.path.insert(0, str(_MATRIX_PATH))


def _load_matrix_module():
    try:
        import matrix_50_cases  # type: ignore
        return matrix_50_cases
    except Exception:
        return None


@router.get("/matrix/cases")
def matrix_cases_list() -> JSONResponse:
    """50 case meta list — dashboard dropdown 용."""
    m = _load_matrix_module()
    if m is None:
        return JSONResponse({"error": "matrix_50_cases module not loadable"}, status_code=500)
    items = [
        {
            "id": c["id"],
            "title": c["title"],
            "category": c["category"],
            "cause_count": len(c["causes"]),
            "expected_cats": sorted(c["expected_cats"]),
        }
        for c in m.CASES_50
    ]
    by_cat: dict[str, list] = {}
    for it in items:
        by_cat.setdefault(it["category"], []).append(it["id"])
    return JSONResponse({"items": items, "count": len(items), "by_category": by_cat})


@router.get("/audit")
def ontology_audit_endpoint() -> JSONResponse:
    """Ontology consistency audit — 9 invariant 검증.

    응답: {pass, total, by_invariant, by_severity, findings[]}
    HTTP 200 항상 (audit fail 도 OK 응답으로 — UI 가 노출).

    invariants:
      I1 cause.problem_template_ko    I2 cause ≥1 treatment
      I3 treatment.applies_to_causes  I4 treatment rationale+trade_off
      I5 config_key 친화 라벨         I6 direction 친화 라벨
      I7 family ↔ MUS token mapping   I8 matrix factory cause_id 정합
      I9 cause.category known set
    """
    from services.semantics.ontology_audit import audit_all, audit_summary
    findings = audit_all()
    return JSONResponse(audit_summary(findings))


@router.get("/matrix/case/{case_id}/payload")
def matrix_case_payload(case_id: str) -> JSONResponse:
    """case_id → 합성된 build_unrecoverable_payload (실시간)."""
    m = _load_matrix_module()
    if m is None:
        return JSONResponse({"error": "matrix_50_cases module not loadable"}, status_code=500)
    case = next((c for c in m.CASES_50 if c["id"] == case_id), None)
    if case is None:
        return JSONResponse({"error": f"case {case_id} not found in matrix"}, status_code=404)
    try:
        payload = m._build_case_payload(case)
        result = m.assert_case(case)
        return JSONResponse({
            "case_meta": {
                "id": case["id"],
                "title": case["title"],
                "category": case["category"],
                "expected_cats": sorted(case["expected_cats"]),
            },
            "payload": payload,
            "verdict": result,
        })
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


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
      --t0:#fca5a5; --t0bg:#7f1d1d;
      --t1:#fde68a; --t1bg:#713f12;
      --t2:#93c5fd; --t2bg:#1e3a8a;
      --t3:#d1d5db; --t3bg:#374151;
    }
    * { box-sizing: border-box; }
    html, body { margin:0; padding:0; height:100%; background:var(--bg); color:var(--text); font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif; font-size:13px; }

    /* Top nav */
    .topnav { display:flex; align-items:center; padding:0 22px; height:52px; border-bottom:1px solid var(--border); background:var(--bg2); position:sticky; top:0; z-index:50; }
    .brand { font-size:14px; font-weight:600; color:var(--text); margin-right:32px; }
    .brand small { color:var(--muted); font-weight:400; margin-left:6px; font-size:11px; }
    .main-tabs { display:flex; gap:0; }
    .mt-btn { padding:6px 18px; background:transparent; color:var(--muted); border:none; cursor:pointer; font-size:13px; border-bottom:2px solid transparent; height:52px; }
    .mt-btn:hover { color:var(--text); background:rgba(255,255,255,0.02); }
    .mt-btn.active { color:var(--text); border-bottom-color:var(--accent); }
    .topnav .meta { margin-left:auto; color:var(--muted); font-size:11px; font-family:monospace; }

    /* Content */
    main { padding:24px 32px 64px; max-width:1100px; margin:0 auto; }
    .tab-panel { display:none; }
    .tab-panel.active { display:block; }

    /* Common widgets */
    .picker { display:flex; align-items:center; gap:12px; padding:14px 16px; background:var(--panel); border:1px solid var(--border); border-radius:8px; margin-bottom:18px; }
    .picker label { color:var(--muted); font-size:12px; }
    .picker select, .picker input { background:var(--bg); color:var(--text); border:1px solid var(--border); border-radius:5px; padding:6px 10px; font-family:inherit; font-size:12px; min-width:300px; }
    .picker input[type=number] { min-width:80px; }
    .picker .btn { background:var(--panel2); color:var(--text); border:1px solid var(--border); border-radius:5px; padding:6px 12px; cursor:pointer; font-size:12px; }
    .picker .btn:hover { background:var(--hover); }

    .empty { padding:48px 0; text-align:center; color:var(--muted); font-size:13px; }
    .badge { display:inline-block; padding:2px 8px; border-radius:10px; font-size:10px; background:var(--panel2); color:var(--text); font-weight:500; }

    /* Diagnosis screen */
    .verdict { background:var(--panel); border:1px solid var(--border); border-left:4px solid var(--accent); border-radius:8px; padding:16px 20px; margin-bottom:14px; }
    .verdict.ok { border-left-color:var(--pass); }
    .verdict.fail { border-left-color:var(--fail); }
    .verdict h2 { margin:0; font-size:18px; font-weight:600; display:flex; align-items:center; gap:10px; }
    .verdict .v-sub { color:var(--muted); font-size:12px; margin-top:6px; line-height:1.6; }
    .verdict .v-meta { color:var(--muted); font-size:10px; margin-top:10px; font-family:monospace; }
    .verdict .v-status-pill { font-size:10px; padding:2px 8px; border-radius:10px; background:var(--panel2); color:var(--accent); }

    .narrative { background:#0a1525; border:1px solid #1e3a8a; border-radius:8px; padding:14px 18px; margin-bottom:14px; font-size:13px; line-height:1.7; color:#dbeafe; }

    /* Why panel — 인과 답변 */
    .why-panel { background:#1a1605; border:1px solid #713f12; border-left:4px solid #fbbf24; border-radius:8px; padding:14px 18px; margin-bottom:14px; }
    .why-headline { font-size:15px; font-weight:600; color:#fef3c7; line-height:1.55; margin-bottom:8px; }
    .why-mech { font-size:12px; color:#fde68a; line-height:1.7; margin-bottom:10px; }
    .why-bullets { background:rgba(0,0,0,0.2); border-radius:6px; padding:8px 12px; margin-top:8px; }
    .wb-row { display:flex; align-items:baseline; gap:10px; font-size:11px; padding:3px 0; }
    .wb-label { color:#a16207; min-width:140px; }
    .wb-value { color:#fef3c7; font-family:monospace; }
    .why-conf { font-size:10px; color:#a16207; margin-top:8px; text-align:right; font-style:italic; }
    .stage-bar { margin-bottom:14px; }
    .stage-pill { display:inline-block; background:#1e293b; color:#93c5fd; padding:5px 14px; border-radius:14px; font-size:12px; border:1px solid #334155; }

    .banner-t0 { background:linear-gradient(90deg,#7f1d1d,#991b1b); color:#fee2e2; padding:12px 16px; border-radius:8px; margin-bottom:14px; }
    .banner-t0 .b-title { font-weight:600; margin-bottom:4px; }
    .banner-t0 .b-msg { font-size:11px; opacity:0.92; }

    .tier-strip { display:grid; grid-template-columns:repeat(4,1fr); gap:10px; margin-bottom:14px; }
    .tier-card { background:var(--panel); border:1px solid var(--border); border-radius:6px; padding:10px 12px; text-align:center; }
    .tier-card .tc-label { font-size:9px; color:var(--muted); text-transform:uppercase; letter-spacing:.04em; }
    .tier-card .tc-num { font-size:28px; font-weight:700; margin-top:4px; }
    .tier-card .tc-sub { font-size:10px; color:var(--muted); margin-top:2px; }
    .tier-card.T0 .tc-num { color:var(--t0); }
    .tier-card.T0.has { border-color:var(--t0bg); background:#1a0a0a; }
    .tier-card.T1 .tc-num { color:var(--t1); }
    .tier-card.T1.has { border-color:var(--t1bg); background:#1a1605; }
    .tier-card.T2 .tc-num { color:var(--t2); }
    .tier-card.T2.has { border-color:var(--t2bg); background:#0a1525; }
    .tier-card.T3 .tc-num { color:var(--t3); }

    .ev-strip { display:grid; grid-template-columns:repeat(3,1fr); gap:10px; margin-bottom:14px; }
    .ev-card { background:var(--panel); border:1px solid var(--border); border-radius:6px; padding:10px 12px; }
    .ev-card .ec-label { font-size:10px; color:var(--muted); }
    .ev-card .ec-value { font-size:18px; font-weight:600; margin-top:2px; font-family:monospace; }
    .ev-card.warn { border-color:#7f1d1d; background:#1a0a0a; }
    .ev-card.warn .ec-value { color:#fca5a5; }
    .ev-card.ok-hint { border-color:#14532d; background:#06140b; }
    .ev-card.ok-hint .ec-value { color:#86efac; }

    .section-title { font-size:11px; color:var(--muted); text-transform:uppercase; letter-spacing:.06em; margin:18px 0 8px; }

    .axis-card { background:var(--panel); border:1px solid var(--t2bg); border-radius:8px; padding:12px 16px; margin-bottom:10px; }
    .axis-card .ax-top { display:flex; align-items:center; gap:10px; flex-wrap:wrap; margin-bottom:6px; }
    .axis-card .ax-prio { background:var(--t2bg); color:var(--t2); padding:2px 10px; border-radius:12px; font-size:11px; font-weight:700; min-width:24px; text-align:center; }
    .axis-card .ax-id { font-size:14px; font-weight:600; font-family:monospace; }
    .axis-card .ax-meta { font-size:10px; color:var(--muted); }
    .axis-card .ax-msg { font-size:13px; color:#e2e8f0; line-height:1.55; }
    .axis-card .ax-tgt { font-size:11px; color:#94a3b8; margin-top:8px; padding-top:8px; border-top:1px solid #1e293b; }

    .protected-card { background:#1a1605; border:1px solid var(--t1bg); border-radius:6px; padding:8px 14px; margin-bottom:6px; }
    .protected-card .pc-label { font-size:12px; color:var(--t1); font-weight:500; }
    .protected-card .pc-meta { font-size:10px; color:#a16207; margin-top:2px; }

    .accordion { background:var(--panel); border:1px solid var(--border); border-radius:6px; margin-top:18px; }
    .accordion summary { padding:10px 14px; cursor:pointer; font-size:12px; color:var(--muted); }
    .accordion summary:hover { color:var(--text); }
    .accordion[open] summary { color:var(--text); border-bottom:1px solid var(--border); }
    .accordion .acc-body { padding:14px 16px; }

    table.cells { width:100%; border-collapse:collapse; font-size:11px; }
    table.cells th { text-align:left; padding:6px 8px; color:var(--muted); font-weight:500; border-bottom:1px solid var(--border); }
    table.cells td { padding:6px 8px; border-bottom:1px solid var(--panel); }
    table.cells tr:hover { background:rgba(255,255,255,0.02); }
    .axis-pill { display:inline-block; background:var(--t2bg); color:var(--t2); padding:1px 7px; border-radius:8px; font-size:9px; margin-right:3px; font-family:monospace; }

    .rc-list { display:flex; flex-wrap:wrap; gap:6px; }

    /* Patterns screen */
    .stat-row { display:grid; grid-template-columns:repeat(2,1fr); gap:10px; margin-bottom:14px; }
    .bar-list { background:var(--panel); border:1px solid var(--border); border-radius:8px; padding:14px 16px; }
    .bar-list h3 { margin:0 0 10px; font-size:12px; color:var(--muted); text-transform:uppercase; }
    .bar-row { display:flex; align-items:center; gap:10px; margin-bottom:6px; font-size:12px; }
    .bar-row .br-key { width:160px; font-family:monospace; font-size:11px; }
    .bar-row .br-bar { flex:1; height:14px; background:#0a1525; border-radius:7px; overflow:hidden; }
    .bar-row .br-fill { height:100%; background:linear-gradient(90deg,var(--t2bg),var(--accent)); }
    .bar-row .br-count { width:36px; text-align:right; font-family:monospace; font-size:11px; color:var(--muted); }

    /* Graph screen */
    #graph-wrap { height:calc(100vh - 100px); display:flex; flex-direction:column; }
    #graph-controls { display:flex; gap:10px; align-items:center; padding:10px 14px; background:var(--panel); border:1px solid var(--border); border-radius:8px 8px 0 0; }
    #graph-controls select { background:var(--bg); color:var(--text); border:1px solid var(--border); border-radius:5px; padding:5px 8px; font-size:12px; min-width:280px; }
    #graph-controls .btn { background:var(--panel2); color:var(--text); border:1px solid var(--border); border-radius:5px; padding:5px 12px; cursor:pointer; font-size:12px; }
    #graph-controls .btn.active { background:var(--accent); border-color:var(--accent); color:#0a1525; }
    #graph-controls .gc-stat { margin-left:auto; color:var(--muted); font-size:11px; font-family:monospace; }
    #cy { flex:1; background:var(--bg); border:1px solid var(--border); border-top:none; border-radius:0 0 8px 8px; min-height:520px; }
    .cy-hint { padding:6px 14px; color:var(--muted); font-size:10px; }
  </style>
</head>
<body>
  <header class="topnav">
    <div class="brand">Ontology Inspector <small>v2 · diagnosis-first</small></div>
    <nav class="main-tabs">
      <button class="mt-btn active" data-tab="diagnosis">🩺 진단</button>
      <button class="mt-btn" data-tab="patterns">📊 패턴</button>
      <button class="mt-btn" data-tab="graph">🕸 그래프</button>
    </nav>
    <div class="meta" id="run-meta-hint"></div>
  </header>

  <main>
    <!-- 진단 탭 -->
    <section class="tab-panel active" data-tab="diagnosis">
      <div class="picker">
        <label>실행:</label>
        <select id="run-select"></select>
        <button class="btn" id="btn-refresh-runs" title="새로고침">↻</button>
        <span id="run-status-hint" style="color:var(--muted); font-size:11px; margin-left:auto;"></span>
      </div>
      <div id="diagnosis-content"><div class="empty">로딩 중…</div></div>
    </section>

    <!-- 패턴 탭 -->
    <section class="tab-panel" data-tab="patterns">
      <div class="picker">
        <label>그룹:</label>
        <select id="pat-group"></select>
        <label>최근:</label>
        <input id="pat-limit" type="number" value="60" min="1" max="500" />
        <span style="color:var(--muted); font-size:11px;">runs</span>
        <button class="btn" id="btn-refresh-patterns">↻</button>
      </div>
      <div id="patterns-content"><div class="empty">로딩 중…</div></div>
    </section>

    <!-- 그래프 탭 -->
    <section class="tab-panel" data-tab="graph">
      <div id="graph-wrap">
        <div id="graph-controls">
          <label style="color:var(--muted); font-size:11px;">Run:</label>
          <select id="graph-run-select"></select>
          <button class="btn" id="btn-show-all" title="저신호 노드도 함께 표시">🔧 전체노드</button>
          <button class="btn" id="btn-fit" title="화면 맞춤">⛶</button>
          <span class="gc-stat" id="graph-stat"></span>
        </div>
        <div id="cy"></div>
        <div class="cy-hint">기본은 핵심 노드(violation, constraint) 만 표시. “전체노드” 토글로 메트릭/풀/맥락 노드까지 노출.</div>
      </div>
    </section>
  </main>

  <script>
    // ── State ──────────────────────────────────────────────────
    const state = {
      tab: 'diagnosis',
      currentRunId: null,
      runs: [],
      cy: null,
      showAllNodes: false,
      lastGraphData: null,
    };

    // ── Helpers ────────────────────────────────────────────────
    const $ = sel => document.querySelector(sel);
    const $$ = sel => Array.from(document.querySelectorAll(sel));
    function el(tag, attrs={}, ...kids) {
      const e = document.createElement(tag);
      for (const k in attrs) {
        if (k === 'class') e.className = attrs[k];
        else if (k.startsWith('on')) e.addEventListener(k.slice(2), attrs[k]);
        else if (k === 'style' && typeof attrs[k] === 'string') e.style.cssText = attrs[k];
        else e.setAttribute(k, attrs[k]);
      }
      for (const k of kids) {
        if (k == null) continue;
        if (typeof k === 'string') e.append(document.createTextNode(k));
        else e.append(k);
      }
      return e;
    }
    async function fetchJSON(url) {
      const r = await fetch(url);
      if (!r.ok) throw new Error(`${r.status} ${url}`);
      return r.json();
    }
    function fmt(n) { return new Intl.NumberFormat().format(n || 0); }

    // ── Tab switching ──────────────────────────────────────────
    $$('.mt-btn').forEach(b => b.addEventListener('click', () => switchTab(b.dataset.tab)));

    async function switchTab(tab) {
      state.tab = tab;
      $$('.mt-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
      $$('.tab-panel').forEach(s => s.classList.toggle('active', s.dataset.tab === tab));
      if (tab === 'diagnosis') await refreshDiagnosis();
      else if (tab === 'patterns') await refreshPatterns();
      else if (tab === 'graph') await refreshGraph();
    }

    // ── Run dropdown population ────────────────────────────────
    async function loadRuns() {
      const data = await fetchJSON('/ontology/diagnosis/runs');
      state.runs = data.runs || [];
      const fmtOpt = r => `${r.fail ? '🚫' : '✅'} ${r.year}-${String(r.month).padStart(2,'0')} · ${r.group_id} · ${r.solver_status || r.status_code || ''}`;
      const sel1 = $('#run-select');
      const sel2 = $('#graph-run-select');
      sel1.innerHTML = ''; sel2.innerHTML = '';
      if (!state.runs.length) {
        sel1.append(el('option', { value:'' }, '(harness run 없음 — harness runner 1회 실행 필요)'));
        sel2.append(el('option', { value:'' }, '(harness run 없음)'));
        return;
      }
      for (const r of state.runs) {
        sel1.append(el('option', { value: r.run_id }, fmtOpt(r)));
        sel2.append(el('option', { value: r.run_id }, fmtOpt(r)));
      }
      // default: latest failure if any, else first
      const def = state.runs.find(r => r.fail) || state.runs[0];
      state.currentRunId = def.run_id;
      sel1.value = def.run_id;
      sel2.value = def.run_id;
      sel1.addEventListener('change', async () => { state.currentRunId = sel1.value; sel2.value = sel1.value; await refreshDiagnosis(); });
      sel2.addEventListener('change', async () => { state.currentRunId = sel2.value; sel1.value = sel2.value; await refreshGraph(); });
      // populate pattern group filter
      const groups = [...new Set(state.runs.map(r => r.group_id))].filter(Boolean);
      const pg = $('#pat-group');
      pg.innerHTML = '';
      pg.append(el('option', { value:'' }, '전체'));
      for (const g of groups) pg.append(el('option', { value: g }, g));
    }

    // ── Diagnosis tab ──────────────────────────────────────────
    async function refreshDiagnosis() {
      const c = $('#diagnosis-content');
      if (!state.currentRunId) {
        c.innerHTML = '';
        c.append(el('div', { class:'empty' }, '실행을 선택하세요.'));
        return;
      }
      c.innerHTML = '<div class="empty">진단 분석 중…</div>';
      try {
        const d = await fetchJSON('/ontology/diagnosis?run_id=' + encodeURIComponent(state.currentRunId));
        renderDiagnosis(d, c);
        $('#run-meta-hint').textContent = `${d.run.group_id} · ${d.run.year}-${String(d.run.month).padStart(2,'0')} · ${d.run.strategy}`;
      } catch (e) {
        c.innerHTML = `<div class="empty">진단 로드 실패: ${e.message}</div>`;
      }
    }

    function renderDiagnosis(d, root) {
      root.replaceChildren();
      const v = d.verdict || {};
      const ev = d.evidence || {};
      const ts = d.tier_summary || {};

      // Verdict
      const card = el('div', { class: 'verdict ' + (v.ok ? 'ok' : 'fail') });
      const h2 = el('h2');
      h2.append(v.ok ? '✅' : '🚫', v.ok ? '근무표 생성 성공' : '근무표 생성 실패');
      if (v.solver_status) h2.append(el('span', { class:'v-status-pill' }, v.solver_status));
      card.append(h2);
      if (v.summary_message_ko) card.append(el('div', { class:'v-sub' }, v.summary_message_ko));
      card.append(el('div', { class:'v-meta' }, `${d.run.run_id}`));
      root.append(card);

      // Why panel (the actual answer to "왜?") — replaces narrative one-liner
      const why = d.why || {};
      if (why.headline_ko) {
        root.append(el('div', { class:'section-title', style:'color:#fbbf24; font-size:12px;' }, '📍 왜 안 됐는가'));
        const wp = el('div', { class:'why-panel' });
        wp.append(el('div', { class:'why-headline' }, why.headline_ko));
        if (why.mechanism_ko) wp.append(el('div', { class:'why-mech' }, why.mechanism_ko));
        const bullets = why.evidence_bullets || [];
        if (bullets.length) {
          const ul = el('div', { class:'why-bullets' });
          for (const b of bullets) {
            if (!b || !b.value) continue;
            const row = el('div', { class:'wb-row' });
            row.append(el('span', { class:'wb-label' }, b.label || ''));
            row.append(el('span', { class:'wb-value' }, b.value));
            ul.append(row);
          }
          wp.append(ul);
        }
        if (why.confidence) {
          wp.append(el('div', { class:'why-conf' }, `근거 강도: ${why.confidence}`));
        }
        root.append(wp);
      } else if (d.narrative_ko) {
        root.append(el('div', { class:'narrative' }, d.narrative_ko));
      }

      // Stage
      if (d.failure_stage && d.failure_stage !== 'unknown') {
        const sb = el('div', { class:'stage-bar' });
        sb.append(el('span', { class:'stage-pill' }, '🪜 ' + (d.failure_stage_label_ko || d.failure_stage)));
        root.append(sb);
      }

      // Data correction banner (T0)
      if (d.data_correction && d.data_correction.required) {
        const fams = (d.data_correction.families || []).join(', ') || 'ConfigIntegrity';
        const ban = el('div', { class:'banner-t0' });
        ban.append(el('div', { class:'b-title' }, `⛔ 데이터 정비 필요 (${fams})`));
        ban.append(el('div', { class:'b-msg' }, d.data_correction.message_ko || '룰 완화가 아닌 입력 데이터를 직접 정비하세요.'));
        root.append(ban);
      }

      // Tier strip
      root.append(el('div', { class:'section-title' }, '무엇이 막혔는가 (Tier)'));
      const ts2 = el('div', { class:'tier-strip' });
      const tierMeta = {
        T0:{ label:'절대/물리', sub:'데이터 정비' },
        T1:{ label:'안전', sub:'절대 풀지 마' },
        T2:{ label:'운영', sub:'풀 수 있음' },
        T3:{ label:'품질', sub:'soft' },
      };
      for (const t of ['T0','T1','T2','T3']) {
        const n = Number(ts[t] || 0);
        const card = el('div', { class:`tier-card ${t}${n>0?' has':''}` });
        card.append(el('div', { class:'tc-label' }, `${t} · ${tierMeta[t].label}`));
        card.append(el('div', { class:'tc-num' }, String(n)));
        card.append(el('div', { class:'tc-sub' }, tierMeta[t].sub));
        ts2.append(card);
      }
      root.append(ts2);

      // Evidence (raw measurements — reference)
      root.append(el('div', { class:'section-title' }, '측정값 (참고)'));
      const es = el('div', { class:'ev-strip' });
      const evItems = [
        { label:'실패 셀', value:ev.total_failed_cells, warn:ev.total_failed_cells > 0 },
        { label:'미배정 인-셀', value:ev.required_minus_assigned_total, warn:ev.required_minus_assigned_total > 0 },
        { label:'후보 0 셀', value:ev.eligible_zero_cells, warn:ev.eligible_zero_cells > 0, okHint: ev.total_failed_cells > 0 && ev.eligible_zero_cells === 0 },
        { label:'고정/금지 셀', value:ev.fixed_forbidden_count, warn:ev.fixed_forbidden_count > 50 },
        { label:'월경계 carryover', value:ev.carryover_artifact_count, warn:ev.carryover_artifact_count > 0 },
        { label:'pool 부족', value:ev.pool_shortages_count, warn:ev.pool_shortages_count > 0 },
      ];
      for (const it of evItems) {
        const c = el('div', { class:'ev-card' + (it.warn?' warn':'') + (it.okHint?' ok-hint':'') });
        c.append(el('div', { class:'ec-label' }, it.label));
        c.append(el('div', { class:'ec-value' }, fmt(it.value)));
        es.append(c);
      }
      root.append(es);

      // Protected
      const pAxes = d.protected_axes || [];
      if (pAxes.length) {
        root.append(el('div', { class:'section-title', style:'color:var(--t1);' }, '🔒 절대 풀지 마세요 (T1)'));
        for (const p of pAxes) {
          const c = el('div', { class:'protected-card' });
          c.append(el('div', { class:'pc-label' }, '• ' + (p.label_ko || p.axis_id)));
          c.append(el('div', { class:'pc-meta' }, `${p.family || ''} — ${p.why_protected_ko || '안전·법규성 룰'}`));
          root.append(c);
        }
      }

      // Axis actions
      const acts = d.axis_actions || [];
      if (acts.length) {
        root.append(el('div', { class:'section-title', style:'color:var(--t2);' }, `🛠 풀 수 있는 룰 (T2, 우선순위 순 · 최대 ${d.axis_actions_cap || 5}건)`));
        for (const a of acts) {
          const c = el('div', { class:'axis-card' });
          const top = el('div', { class:'ax-top' });
          top.append(el('span', { class:'ax-prio' }, String(a.priority || '?')));
          top.append(el('span', { class:'ax-id' }, a.axis_id || ''));
          top.append(el('span', { class:'ax-meta' }, `${a.family || ''} · ${a.tier || ''} · prio=${a.relaxation_priority ?? '-'}`));
          c.append(top);
          if (a.human_message_ko) c.append(el('div', { class:'ax-msg' }, a.human_message_ko));
          const tgts = a.targets || [];
          if (tgts.length) {
            const lines = tgts.slice(0,6).map(t => {
              if (t.pool_id) return `${t.pool_id} (부족 ${t.shortage ?? '?'})`;
              if (t.day != null) return `${t.day}일 ${t.shift || ''}`;
              if (t.grade) return `등급 ${t.grade}`;
              return JSON.stringify(t);
            });
            const more = tgts.length > 6 ? ` 외 ${tgts.length - 6}건` : '';
            c.append(el('div', { class:'ax-tgt' }, '📍 ' + lines.join(' · ') + more));
          }
          root.append(c);
        }
        if (Number(d.axis_actions_truncated || 0) > 0) {
          root.append(el('div', { style:'color:var(--muted); font-size:11px; margin-top:6px;' },
            `(${d.axis_actions_truncated}건 더 있음 — 상위 ${d.axis_actions_cap || 5}개만 표시)`));
        }
      }

      // Accordion: details
      const det = el('details', { class:'accordion' });
      det.append(el('summary', {}, '📋 상세 보기 (Reason codes · 실패 셀 · 구조 진단)'));
      const body = el('div', { class:'acc-body' });

      // reason codes
      const codes = d.violated_reason_codes || [];
      if (codes.length) {
        body.append(el('div', { class:'section-title', style:'margin-top:0;' }, '직접 reason codes'));
        const rcl = el('div', { class:'rc-list' });
        for (const c of codes) rcl.append(el('span', { class:'badge' }, c));
        body.append(rcl);
      }

      // cells table
      const cells = ev.top_failed_cells || [];
      if (cells.length) {
        body.append(el('div', { class:'section-title' }, `실패 셀 Top ${Math.min(cells.length, 15)}`));
        const tbl = el('table', { class:'cells' });
        const thead = el('thead');
        const trh = el('tr');
        ['Day','Shift','요구','후보','배정','부족','막힌 axis'].forEach(h => trh.append(el('th', {}, h)));
        thead.append(trh); tbl.append(thead);
        const tbody = el('tbody');
        for (const c of cells.slice(0, 15)) {
          const tr = el('tr');
          tr.append(el('td', {}, String(c.day ?? '?')));
          tr.append(el('td', {}, String(c.shift ?? '')));
          tr.append(el('td', {}, String(c.required ?? '')));
          tr.append(el('td', {}, String(c.eligible ?? '')));
          tr.append(el('td', {}, String(c.assigned ?? '')));
          tr.append(el('td', { style:'color:#fca5a5; font-weight:600;' }, String(c.shortage ?? '')));
          const axCell = el('td');
          const axes = c.blocking_axes || [];
          if (axes.length) for (const ax of axes) axCell.append(el('span', { class:'axis-pill' }, ax));
          else axCell.append(el('span', { style:'color:var(--muted); font-size:10px;' }, '-'));
          tr.append(axCell);
          tbody.append(tr);
        }
        tbl.append(tbody);
        body.append(tbl);
      }

      // structural diagnosis
      const sd = d.structural_diagnosis || {};
      if (sd.mode) {
        body.append(el('div', { class:'section-title' }, 'G0 구조 진단'));
        body.append(el('div', { style:'font-size:11px; color:var(--muted); font-family:monospace;' },
          `mode=${sd.mode} · causes=[${(sd.primary_causes || []).join(',')}] · trace=${(sd.decision_trace || []).join(' / ')}`));
      }

      det.append(body);
      root.append(det);
    }

    // ── Patterns tab ───────────────────────────────────────────
    async function refreshPatterns() {
      const c = $('#patterns-content');
      c.innerHTML = '<div class="empty">패턴 집계 중…</div>';
      try {
        const group = $('#pat-group').value;
        const limit = Number($('#pat-limit').value) || 60;
        const qp = new URLSearchParams();
        if (group) qp.set('group_id', group);
        qp.set('limit_runs', String(limit));
        const d = await fetchJSON('/ontology/patterns?' + qp.toString());
        renderPatterns(d, c);
      } catch (e) {
        c.innerHTML = `<div class="empty">패턴 로드 실패: ${e.message}</div>`;
      }
    }

    function renderPatterns(d, root) {
      root.replaceChildren();
      const sc = d.scope || {};
      const oc = d.outcome || {};

      // Scope summary
      root.append(el('div', { class:'narrative' },
        `${sc.group_id || '전체 그룹'} · 최근 ${sc.runs_used}개 실행 분석 · ` +
        `성공 ${oc.ok}건 / 실패 ${oc.fail}건`));

      // 2-column bar lists
      const row = el('div', { class:'stat-row' });
      row.append(buildBarList('자주 막힌 axis', d.axis_freq, 'axis_id'));
      row.append(buildBarList('자주 막힌 family', d.family_freq, 'family'));
      root.append(row);

      const row2 = el('div', { class:'stat-row' });
      row2.append(buildBarList('단계 분포', d.stage_run_freq, 'stage'));
      row2.append(buildBarList('lock_type 분포', d.lock_type_freq, 'lock'));
      root.append(row2);

      // Tier prevalence
      root.append(buildBarList('티어 등장 빈도 (run 단위)', d.tier_run_freq, 'tier'));

      // Insight
      if (d.axis_freq && d.axis_freq.length) {
        const top = d.axis_freq[0];
        root.append(el('div', { class:'narrative', style:'margin-top:14px;' },
          `💡 이 범위에서 가장 자주 막히는 룰은 \`${top.key}\` (${top.count}회) 입니다. 이 axis 의 정책·구성을 우선 점검하세요.`));
      }
    }

    function buildBarList(title, items, _kind) {
      const wrap = el('div', { class:'bar-list' });
      wrap.append(el('h3', {}, title));
      if (!items || !items.length) {
        wrap.append(el('div', { style:'color:var(--muted); font-size:11px;' }, '데이터 없음'));
        return wrap;
      }
      const max = Math.max(...items.map(i => i.count), 1);
      for (const it of items.slice(0, 12)) {
        const r = el('div', { class:'bar-row' });
        r.append(el('div', { class:'br-key' }, it.key || '?'));
        const bar = el('div', { class:'br-bar' });
        bar.append(el('div', { class:'br-fill', style:`width:${(it.count/max*100).toFixed(1)}%` }));
        r.append(bar);
        r.append(el('div', { class:'br-count' }, String(it.count)));
        wrap.append(r);
      }
      return wrap;
    }

    // ── Graph tab ──────────────────────────────────────────────
    async function refreshGraph() {
      try {
        const g = await fetchJSON('/ontology/graph?level=full');
        state.lastGraphData = g;
        renderCytoscape(g);
      } catch (e) {
        console.error(e);
      }
    }

    function renderCytoscape(g) {
      const showAll = state.showAllNodes;
      const visibleNodes = g.nodes.filter(n => showAll || n.ui_visible !== false);
      const nodeIds = new Set(visibleNodes.map(n => n.id));
      const elements = visibleNodes.map(n => ({
        group:'nodes',
        data:{ id:n.id, label:shortLabel(n), type:n.type, color:typeColor(n.type), tier:n.ui_tier || 'med' },
      }));
      for (const e of g.edges) {
        if (!nodeIds.has(e.from) || !nodeIds.has(e.to)) continue;
        elements.push({ group:'edges', data:{ id:`${e.type}|${e.from}|${e.to}`, source:e.from, target:e.to, type:e.type } });
      }
      if (!state.cy) {
        state.cy = cytoscape({
          container: $('#cy'),
          elements: [],
          style: [
            { selector:'node', style:{
              'background-color':'data(color)', 'label':'data(label)',
              'color':'#e8ecf4', 'font-size':10,
              'text-valign':'bottom', 'text-margin-y':4,
              'width':32, 'height':32,
              'border-width':1, 'border-color':'#29304a'
            }},
            { selector:'node[tier = "high"]', style:{ 'border-color':'#60a5fa', 'border-width':2 }},
            { selector:'edge', style:{
              'line-color':'#334155', 'width':1, 'curve-style':'bezier',
              'target-arrow-shape':'triangle', 'target-arrow-color':'#334155'
            }},
            { selector:':selected', style:{ 'border-color':'#60a5fa', 'border-width':3 }},
          ],
          layout: { name:'cose', animate:false },
        });
      }
      state.cy.elements().remove();
      state.cy.add(elements);
      state.cy.layout({ name:'cose', animate:false, fit:true, padding:30 }).run();
      $('#graph-stat').textContent = `nodes ${g.nodes.length} · 표시 ${visibleNodes.length} · edges ${g.edges.length}`;
    }

    function shortLabel(n) {
      const t = n.type || '';
      const id = String(n.id || '');
      if (t === 'RuleNode') return n.attrs?.rule_id || id;
      if (t === 'ViolationNode') return 'V/' + (n.attrs?.rule_id || id.slice(-8));
      if (t === 'ConflictCoreNode') return 'core';
      if (t === 'ConstraintNode') return n.attrs?.family || 'C';
      return id.slice(-12);
    }
    function typeColor(t) {
      return ({
        ViolationNode:'#fca5a5', ConflictCoreNode:'#f59e0b',
        RuleNode:'#a7f3d0', ConstraintNode:'#c4b5fd',
        DataQualityNode:'#fde68a', MetricNode:'#7dd3fc',
        TeamPoolNode:'#fb923c', GradePoolNode:'#a3e635', CommonPoolNode:'#94a3b8',
      })[t] || '#94a3b8';
    }

    // Graph controls
    $('#btn-show-all').addEventListener('click', () => {
      state.showAllNodes = !state.showAllNodes;
      $('#btn-show-all').classList.toggle('active', state.showAllNodes);
      if (state.lastGraphData) renderCytoscape(state.lastGraphData);
    });
    $('#btn-fit').addEventListener('click', () => state.cy && state.cy.fit(state.cy.elements(), 30));

    // Run picker buttons
    $('#btn-refresh-runs').addEventListener('click', async () => { await loadRuns(); await refreshDiagnosis(); });
    $('#btn-refresh-patterns').addEventListener('click', () => refreshPatterns());
    $('#pat-group').addEventListener('change', refreshPatterns);
    $('#pat-limit').addEventListener('change', refreshPatterns);

    // ── Bootstrap ──────────────────────────────────────────────
    (async function bootstrap() {
      try {
        await loadRuns();
        await switchTab('diagnosis');
      } catch (e) {
        $('#diagnosis-content').innerHTML = `<div class="empty">초기 로딩 실패: ${e.message}</div>`;
      }
    })();
  </script>
</body>
</html>
"""


_HTML_V2 = """<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <title>Ontology — 진단 v3</title>
  <style>
    * { box-sizing: border-box; }
    :root {
      --bg: #f7f8fa; --panel: #fff; --border: #e6e8ed; --ink: #1a1d24; --muted: #5a6373;
      --cat-capacity:#EF4444; --cat-eligibility:#F59E0B; --cat-fixed:#8B5CF6;
      --cat-team:#3B82F6; --cat-grade:#10B981; --cat-carryover:#6B7280;
      --cat-recovery:#0EA5E9; --cat-transition:#EC4899; --cat-preceptee:#14B8A6;
      --cat-consecutive:#A855F7; --cat-config:#F97316; --cat-meta:#DC2626;
      --cat-evidence:#0EA5E9; --cat-bundle:#3B82F6; --cat-treatment:#10B981;
      --hard-bg:#FEE2E2; --hard-fg:#991B1B; --hard-border:#FCA5A5;
      --soft-bg:#FEF3C7; --soft-fg:#92400E;
      --ok-bg:#D1FAE5; --ok-fg:#065F46;
    }
    body {
      margin: 0; padding: 0;
      font-family: -apple-system, "Apple SD Gothic Neo", "Noto Sans KR", sans-serif;
      background: var(--bg); color: var(--ink); line-height: 1.5; font-size: 13px;
    }
    header {
      background: var(--panel); border-bottom: 1px solid var(--border);
      padding: 12px 24px; display: flex; align-items: center; gap: 16px;
      position: sticky; top: 0; z-index: 50;
    }
    header h1 { margin: 0; font-size: 16px; font-weight: 700; }
    header h1 small { color: var(--muted); font-weight: 400; margin-left: 6px; font-size: 11px; }
    header select {
      padding: 6px 10px; border: 1px solid #cfd4dc; border-radius: 6px;
      background: #fff; font-size: 13px; min-width: 360px; cursor: pointer;
    }
    header .meta {
      font-size: 11px; color: var(--muted); font-family: ui-monospace, monospace;
      margin-left: auto;
    }
    header .verdict {
      padding: 4px 10px; border-radius: 999px; font-size: 11px; font-weight: 600;
    }
    header .verdict.pass { background: var(--ok-bg); color: var(--ok-fg); }
    header .verdict.fail { background: var(--hard-bg); color: var(--hard-fg); }
    main { max-width: 1200px; margin: 0 auto; padding: 20px 24px 80px; }
    /* Tier 1 — Status Banner */
    .banner {
      border-radius: 12px; padding: 18px 22px; margin-bottom: 18px;
      display: flex; align-items: center; gap: 16px;
      border: 1px solid var(--border); background: var(--panel);
    }
    .banner.hard { background: var(--hard-bg); border-color: var(--hard-border); }
    .banner.soft { background: var(--soft-bg); border-color: #FCD34D; }
    .banner.ok   { background: var(--ok-bg);   border-color: #6EE7B7; }
    .banner .icon { font-size: 32px; line-height: 1; }
    .banner .title { font-size: 16px; font-weight: 700; margin-bottom: 4px; }
    .banner.hard .title { color: var(--hard-fg); }
    .banner.soft .title { color: var(--soft-fg); }
    .banner.ok   .title { color: var(--ok-fg); }
    .banner .desc { font-size: 13px; color: var(--muted); }
    .banner .criteria { display: flex; gap: 6px; margin-top: 6px; }
    .criteria .chip {
      background: rgba(0,0,0,0.05); color: var(--ink); padding: 2px 8px;
      border-radius: 4px; font-size: 11px; font-family: ui-monospace, monospace;
    }
    .banner.hard .criteria .chip { background: rgba(255,255,255,0.6); color: var(--hard-fg); }

    /* Tier 2 — Narrative Cards (3-column) */
    .cards-3col {
      display: grid; grid-template-columns: repeat(3, 1fr);
      gap: 14px; margin-bottom: 20px;
    }
    .col { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; }
    .col-title { font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.6px; color: var(--muted); margin-bottom: 10px; }
    .col-problem .col-title { color: #DC2626; }
    .col-solution .col-title { color: #059669; }
    .col-tradeoff .col-title { color: #B45309; }
    .item {
      padding: 10px 0; border-bottom: 1px dashed #e6e8ed;
    }
    .item:last-child { border-bottom: none; }
    .item .top { display: flex; align-items: center; gap: 6px; margin-bottom: 4px; }
    .cat-badge {
      display: inline-block; padding: 2px 7px; border-radius: 4px; font-size: 10px;
      font-weight: 600; color: white; text-transform: uppercase; letter-spacing: 0.3px;
    }
    .cat-capacity { background: var(--cat-capacity); }
    .cat-eligibility { background: var(--cat-eligibility); }
    .cat-fixed { background: var(--cat-fixed); }
    .cat-team { background: var(--cat-team); }
    .cat-grade { background: var(--cat-grade); }
    .cat-carryover { background: var(--cat-carryover); }
    .cat-recovery { background: var(--cat-recovery); }
    .cat-transition { background: var(--cat-transition); }
    .cat-preceptee { background: var(--cat-preceptee); }
    .cat-consecutive { background: var(--cat-consecutive); }
    .cat-config { background: var(--cat-config); }
    .cat-meta { background: var(--cat-meta); }
    .cat-evidence { background: var(--cat-evidence); }
    .cat-bundle { background: var(--cat-bundle); }
    .cat-treatment { background: var(--cat-treatment); }
    .item .text { font-size: 13px; color: var(--ink); }
    .item .meta { font-size: 11px; color: var(--muted); margin-top: 3px; font-family: ui-monospace, monospace; }
    .item .config-row {
      display: inline-block; background: #F3F4F6; padding: 2px 8px;
      border-radius: 4px; font-family: ui-monospace, monospace;
      font-size: 11px; margin-top: 4px;
    }
    .show-more {
      font-size: 11px; color: var(--muted); margin-top: 8px;
      text-align: center; cursor: pointer;
    }
    .show-more:hover { color: var(--ink); text-decoration: underline; }

    /* Tier 3 — Mini Graph */
    .graph-wrap {
      background: var(--panel); border: 1px solid var(--border); border-radius: 12px;
      padding: 16px 20px; margin-bottom: 18px;
    }
    .graph-header { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; flex-wrap: wrap; }
    .graph-title { font-size: 13px; font-weight: 700; color: var(--ink); margin-right: auto; }
    .filter-chips { display: flex; gap: 6px; flex-wrap: wrap; }
    .filter-chip {
      padding: 3px 9px; border-radius: 999px; font-size: 11px; cursor: pointer;
      border: 1px solid var(--border); background: var(--panel); color: var(--muted);
      user-select: none; font-family: ui-monospace, monospace;
    }
    .filter-chip.active { background: var(--ink); color: white; border-color: var(--ink); }
    .graph-svg { width: 100%; height: 480px; background: #FAFBFC; border-radius: 8px; }
    .graph-stats { font-size: 11px; color: var(--muted); margin-top: 8px; font-family: ui-monospace, monospace; }

    /* Advanced */
    details.advanced {
      background: var(--panel); border: 1px solid var(--border); border-radius: 10px;
      padding: 10px 16px; margin-top: 16px;
    }
    details.advanced > summary { cursor: pointer; font-size: 13px; font-weight: 600; color: var(--muted); list-style: none; outline: none; }
    details.advanced > summary::before { content: "▶  "; font-size: 10px; }
    details.advanced[open] > summary::before { content: "▼  "; }
    details.advanced pre {
      background: #1a1d24; color: #e6e8ed; padding: 12px;
      border-radius: 6px; font-size: 10px; overflow-x: auto;
      max-height: 400px; margin: 10px 0 0;
    }
    .adv-section h4 { font-size: 12px; margin: 4px 0 6px; color: var(--muted); }
    .empty { color: #888; padding: 40px 0; text-align: center; }
  </style>
</head>
<body>
  <header>
    <h1>온톨로지 진단 <small>v3 split-panel</small></h1>
    <select id="case-select"></select>
    <span id="verdict" class="verdict">…</span>
    <span class="meta" id="meta-info"></span>
  </header>
  <main id="root">
    <div class="empty">케이스 로드 중…</div>
  </main>

  <script>
  const sel = document.getElementById('case-select');
  const verdictEl = document.getElementById('verdict');
  const metaInfo = document.getElementById('meta-info');
  const root = document.getElementById('root');
  let activeFilters = new Set();  // empty = show all

  function el(tag, cls, html) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (html !== undefined) e.innerHTML = html;
    return e;
  }

  async function loadCases() {
    let items = [];
    // Matrix 50 cases (synthetic — primary source)
    try {
      const r = await fetch('/ontology/matrix/cases');
      const d = await r.json();
      (d.items || []).forEach(c => items.push({
        source: 'matrix', id: c.id, title: c.title,
        category: c.category, cause_count: c.cause_count,
      }));
    } catch (e) {}
    // Alpha cases (file-based). Live API UNRECOVERABLE 도 dump_live_graph_export
    // 가 alpha_cases sidecar 로 저장하므로 같은 채널에서 로드되며, 파일명
    // prefix (run-live-) 로 별도 카테고리 분류.
    try {
      const r = await fetch('/ontology/cases');
      const d = await r.json();
      (d.items || []).forEach(c => {
        const isLive = (c.case_id || '').startsWith('run-live-');
        items.push({
          source: 'alpha',
          id: c.case_id,
          title: c.case_id,
          category: isLive ? 'Live API run' : 'Alpha file',
          cause_count: c.cause_count,
        });
      });
    } catch (e) {}

    sel.innerHTML = '';
    let group = {};
    items.forEach(it => {
      group[it.category] = group[it.category] || [];
      group[it.category].push(it);
    });
    Object.keys(group).sort().forEach(cat => {
      const og = document.createElement('optgroup');
      og.label = cat;
      group[cat].forEach(it => {
        const opt = document.createElement('option');
        opt.value = `${it.source}::${it.id}`;
        opt.textContent = `${it.id}  —  ${it.title}`;
        og.appendChild(opt);
      });
      sel.appendChild(og);
    });
    if (items.length) loadCase(sel.value);
  }

  async function loadCase(sourceId) {
    const [source, id] = sourceId.split('::');
    const url = source === 'matrix'
      ? `/ontology/matrix/case/${encodeURIComponent(id)}/payload`
      : `/ontology/case/${encodeURIComponent(id)}`;
    const r = await fetch(url);
    const d = await r.json();
    activeFilters = new Set();   // reset filters per case
    render(d, source);
  }

  sel.addEventListener('change', () => loadCase(sel.value));

  // ───── Narrative helpers ─────
  function causeHeadline(c) {
    const det = c.details || c.evidence || {};
    const rc = c.reason_code || c.node_id || '?';
    const map = {
      PRECEPTEE_SYNC_MISMATCH:        `프리셉터-프리셉티 동기화 불가 (${det.preceptor_id || '?'} ↔ ${det.preceptee_id || '?'})`,
      N_CAPACITY_SHORTAGE:            `일별 야간 인력 부족 (${det.day || '?'}일, 수요 ${det.n_required ?? '?'} / 가능 ${det.n_capacity ?? '?'})`,
      MONTHLY_NIGHT_CAPACITY_SHORTAGE:`월간 야간 capacity 부족 (수요 ${det.n_required ?? '?'} / 한도 ${det.n_capacity ?? '?'})`,
      GLOBAL_SHIFT_ALLOWED_SHORTAGE:  `${det.day || '?'}일 ${det.shift || '?'} 시프트 자격자 부족 (가능 ${det.eligible ?? '?'} / 수요 ${det.required ?? '?'})`,
      CAPACITY_TOTAL_SHORTAGE:        `월 총 인력 부족 (수요 ${det.required ?? '?'} / 공급 ${det.capacity ?? '?'})`,
      TEAM_MIN_EXCEEDS_GLOBAL_NEED:   `${det.day || '?'}일 ${det.shift || '?'} 팀 최소 합 ${det.min_sum ?? '?'} > 일 수요 ${det.required ?? '?'}`,
      GRADE_MIN_SUM_EXCEEDS_NEED:     `${det.day || '?'}일 ${det.shift || '?'} 등급 최소 합 ${det.min_sum ?? '?'} > 수요 ${det.required ?? '?'}`,
      GRADE_MAX_SUM_BELOW_NEED:       `${det.day || '?'}일 ${det.shift || '?'} 등급 상한 합 ${det.cap ?? '?'} < 수요 ${det.required ?? '?'}`,
      FIXED_ASSIGN_EXCEEDS_NEED:      `${det.day || '?'}일 ${det.shift || '?'} 고정 배정 ${det.fixed_count ?? '?'}명 > 수요 ${det.required ?? '?'}`,
      ALLOWED_SHIFTS_ISOLATES_NURSE:  `간호사 ${det.nurse_id || '?'} 의 allowed_shifts 비어있음`,
      GRADE_MIN_EXCEEDS_MAX:          `grade ${det.grade || '?'} min(${det.min_val ?? '?'}) > max(${det.max_val ?? '?'}) — 산술 모순`,
      MID_DISABLED_BUT_USED:          `미드 시프트 비활성인데 team/grade 제약에 M 참조`,
      RECOVERY_2N2OFF_BLOCKS:         `2N→2OFF 회복으로 ${det.day_c}~${det.day_d}일 ${det.affected_shift} 수요 ${det.shortage}명 부족`,
      RECOVERY_3N2OFF_BLOCKS:         `3N→2OFF 회복으로 ${det.day_d}~${det.day_e}일 ${det.affected_shift} 수요 ${det.shortage}명 부족`,
      TRANSITION_BAN_NOD_CHAIN:       `간호사 ${det.nurse_id || '?'} day ${det.day || '?'} N→OFF→D 또는 N→D 전이 금지 위반`,
      PREV_MONTH_N_TAIL_BLOCKS:       `간호사 ${det.nurse_id || '?'} 전월 마지막 N → 본 월 day ${det.day || '?'} 배정 차단`,
      CARRYOVER_FIXED_N_ISOLATION:    `간호사 ${det.nurse_id || '?'} ${det.day || '?'}일 fixed N 양옆 OFF → NotOneNight 위반`,
      WEEKEND_OFF_ONLY_DRAINS_WEEKDAY:`주말 OFF 전용 ${det.weekend_off_count ?? '?'}명 → 평일 ${det.weekday_eligible ?? '?'} 가용/수요 ${det.weekday_demand ?? '?'}`,
      BAN_N_BEFORE_FIXED_OFF_ISOLATES:`${det.day || '?'}일 N 수요 ${det.n_required ?? '?'}, 인접 fixed OFF 간호사 ${det.blocked_nurses ?? '?'}명 차단 → 가용 ${det.available_n ?? '?'} 미달`,
      N_ONLY_VS_CAPS:                 `${det.role || '?'}-only ${det.role_only_count ?? '?'}명 > role 수요 ${det.role_demand ?? '?'} → 다른 shift 부족`,
      CONSECUTIVE_WORK_LIMIT_BLOCKS:  `연속근무 한도 ${det.limit ?? '?'} → ${det.day_a}~${det.day_b}일 ${det.affected_shift} 수요 ${det.shortage}명 부족`,
      INITIAL_FORBIDDEN_CONCENTRATION:`간호사 ${det.nurse_id || '?'} ${det.forbidden_shifts || '?'} 전 기간 차단 → 실질 ${det.effective_role || '?'} 역할 강제`,
      MONTHLY_LIMIT_MIN_EXCEEDS_MAX:  `간호사 ${det.nurse_id || '?'} ${det.shift || '?'} 월 min(${det.min_val ?? '?'}) > max(${det.max_val ?? '?'})`,
      MONTHLY_LIMIT_N_EXACT_UNATTAINABLE:`간호사 ${det.nurse_id || '?'} N exact(${det.n_exact ?? '?'}) 가 active ${det.active_days ?? '?'}일과 모순`,
      FIXED_OFF_EXCEEDS_SPAN:         `간호사 ${det.nurse_id || '?'} fixed OFF/예산 ${det.total ?? '?'}일 > 활성 ${det.span ?? '?'}일`,
      GLOBAL_DAY_CAPACITY_SHORTAGE:   `${det.day || '?'}일 총 근무 수요 ${det.total_demand ?? '?'}명 > 간호사 ${det.nurse_count ?? '?'}명`,
      TEAM_SIZE_INSUFFICIENT:         `팀 ${det.team_id || '?'} 크기(${det.size ?? '?'}) < 팀 최소(${det.team_min ?? '?'})`,
      FIXED_ASSIGN_VIOLATES_ALLOWED:  `간호사 ${det.nurse_id || '?'} fixed ${det.shift || '?'} → allowed_shifts 위반`,
    };
    // 우선순위: enriched human_message_ko (placeholder 없는 구체 메시지) > JS template > fallback.
    // treatment_enricher 가 cause.human_message_ko 를 member_sample 기반으로 재작성하므로,
    // 미치환 `{xx}` 가 없으면 그 메시지를 우선 사용. (JS template 의 `?` fallback 회피)
    const hmk = c.human_message_ko;
    const isEnrichedSpecific = hmk && !/\{[^}]+\}/.test(hmk);
    if (isEnrichedSpecific) return hmk;
    return map[rc] || hmk || rc;
  }

  function categoryFromCauseId(cid) {
    if (!cid) return 'meta';
    const m = cid.match(/^cause:([a-z]+):/);
    return m ? m[1] : 'meta';
  }

  function categoryOfNode(n) {
    if (n.kind === 'cause') return n.category || categoryFromCauseId(n.id);
    if (n.kind === 'hard_case_meta') return 'meta';
    if (n.kind === 'evidence') return 'evidence';
    if (n.kind === 'bundle') return 'bundle';
    if (n.kind === 'treatment') return 'treatment';
    if (n.kind === 'symptom') return 'meta';
    return 'meta';
  }

  // ───── Tier 1 — Status Banner ─────
  function renderBanner(inf, meta) {
    const hc = inf.hard_case || {};
    const severity = inf.severity || 'unknown';
    let cls = 'soft', icon = '⚠', title = '문제 발견', desc = '';
    if (severity === 'ok') {
      cls = 'ok'; icon = '✅'; title = '근무표 생성 가능'; desc = '식별된 원인 없음';
    } else if (hc.is_hard) {
      cls = 'hard'; icon = '🚨'; title = `어려운 케이스 (${(hc.criteria_matched || []).join(', ')})`;
      desc = hc.hard_reason_ko || '';
    } else if (severity === 'blocking') {
      cls = 'soft'; icon = '⚠'; title = `${(inf.causes || []).length}건의 원인 발견`;
      desc = '아래 추천대로 적용하면 자동 해소 가능합니다.';
    }
    const banner = el('div', 'banner ' + cls);
    banner.appendChild(el('div', 'icon', icon));
    const body = el('div');
    body.appendChild(el('div', 'title', title));
    body.appendChild(el('div', 'desc', desc));
    if (hc.criteria_matched && hc.criteria_matched.length) {
      const crit = el('div', 'criteria');
      hc.criteria_matched.forEach(c => crit.appendChild(el('span', 'chip', c)));
      body.appendChild(crit);
    }
    banner.appendChild(body);
    return banner;
  }

  // ───── Tier 2 — Narrative Cards (3 columns) ─────
  function renderNarrative(inf) {
    const narr = inf.resolution_narrative || {};
    const probs = narr.problem_list || [];
    const acts  = narr.action_levers || [];
    const tos   = narr.trade_offs || [];

    const wrap = el('div', 'cards-3col');

    // Problems
    const colP = el('div', 'col col-problem');
    colP.appendChild(el('div', 'col-title', `🔴  문제 (${probs.length})`));
    if (!probs.length) colP.appendChild(el('div', 'item text', '식별된 원인 없음'));
    probs.slice(0, 6).forEach(p => {
      const it = el('div', 'item');
      const top = el('div', 'top');
      top.appendChild(el('span', `cat-badge cat-${p.category || 'meta'}`, p.category || 'meta'));
      it.appendChild(top);
      it.appendChild(el('div', 'text', p.rendered_ko || p.label || p.cause_id));
      it.appendChild(el('div', 'meta', `${p.cause_id} · tier ${p.tier || '?'} · ${p.causal_layer || '?'}`));
      colP.appendChild(it);
    });
    if (probs.length > 6) colP.appendChild(el('div', 'show-more', `…더 ${probs.length - 6}건`));
    wrap.appendChild(colP);

    // Solutions
    const colS = el('div', 'col col-solution');
    colS.appendChild(el('div', 'col-title', `🟢  해결책 (${acts.length})`));
    if (!acts.length) colS.appendChild(el('div', 'item text', '추천 lever 없음'));
    acts.slice(0, 6).forEach(a => {
      const it = el('div', 'item');
      it.appendChild(el('div', 'text', a.rationale_ko || a.treatment_id));
      const keyLabel = a.config_key_label_ko || a.config_key;
      const dirLabel = a.direction_label_ko || a.direction;
      if (keyLabel && dirLabel) {
        it.appendChild(el('span', 'config-row', `${keyLabel} → ${dirLabel}`));
      } else if (a.action_type === 'data_correction_required') {
        it.appendChild(el('span', 'config-row', '수동 점검'));
      }
      colS.appendChild(it);
    });
    if (acts.length > 6) colS.appendChild(el('div', 'show-more', `…더 ${acts.length - 6}건`));
    wrap.appendChild(colS);

    // Trade-offs
    const colT = el('div', 'col col-tradeoff');
    colT.appendChild(el('div', 'col-title', `⚠  부작용 주의 (${tos.length})`));
    if (!tos.length) colT.appendChild(el('div', 'item text', '특이사항 없음'));
    tos.slice(0, 6).forEach(t => {
      const it = el('div', 'item');
      it.appendChild(el('div', 'text', t.trade_off_ko));
      colT.appendChild(it);
    });
    if (tos.length > 6) colT.appendChild(el('div', 'show-more', `…더 ${tos.length - 6}건`));
    wrap.appendChild(colT);

    return wrap;
  }

  // ───── Tier 3 — Mini Graph (3-column SVG) ─────
  function buildGraphSvg(graph) {
    const nodes = (graph && graph.nodes) || [];
    const edges = (graph && graph.edges) || [];

    // Apply category filter
    const filtered = activeFilters.size
      ? nodes.filter(n => activeFilters.has(categoryOfNode(n)) || n.kind !== 'cause')
      : nodes;
    const visibleIds = new Set(filtered.map(n => n.id));

    // Layout: 3 columns
    const colMap = {
      cause: 0, symptom: 0,
      treatment: 1, bundle: 1,
      evidence: 2, hard_case_meta: 2,
    };
    const cols = [[], [], []];
    filtered.forEach(n => {
      const c = colMap[n.kind] ?? 2;
      cols[c].push(n);
    });

    const W = 1100, H = 480;
    const colX = [120, 540, 980];
    const colW = 280;
    const lineH = 38;
    const startY = 50;

    const positions = {};
    cols.forEach((nodeList, ci) => {
      const total = nodeList.length;
      const colH = total * lineH;
      const offsetY = Math.max(startY, (H - colH) / 2);
      nodeList.forEach((n, i) => {
        positions[n.id] = { x: colX[ci], y: offsetY + i * lineH, ci };
      });
    });

    let svg = `<svg class="graph-svg" viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg">`;
    // Column headers
    svg += `<text x="${colX[0]}" y="22" text-anchor="middle" font-size="12" font-weight="700" fill="#5a6373">CAUSES</text>`;
    svg += `<text x="${colX[1]}" y="22" text-anchor="middle" font-size="12" font-weight="700" fill="#5a6373">TREATMENTS / BUNDLES</text>`;
    svg += `<text x="${colX[2]}" y="22" text-anchor="middle" font-size="12" font-weight="700" fill="#5a6373">EVIDENCE · HARD_CASE</text>`;

    // Edges
    edges.forEach(e => {
      if (!visibleIds.has(e.src) || !visibleIds.has(e.dst)) return;
      const p1 = positions[e.src], p2 = positions[e.dst];
      if (!p1 || !p2) return;
      const stroke = { causal: '#94a3b8', treatment: '#10b981',
                       evidence: '#0ea5e9', aggregation: '#dc2626',
                       member: '#cbd5e1' }[e.kind] || '#cbd5e1';
      const dash = e.kind === 'member' ? '4,4' : (e.kind === 'aggregation' ? '6,3' : 'none');
      const x1 = p1.x + 100, y1 = p1.y;
      const x2 = p2.x - 100, y2 = p2.y;
      const cx = (x1 + x2) / 2;
      svg += `<path d="M${x1},${y1} C${cx},${y1} ${cx},${y2} ${x2},${y2}" stroke="${stroke}" stroke-width="1.2" stroke-dasharray="${dash}" fill="none" opacity="0.6"/>`;
    });

    // Nodes
    filtered.forEach(n => {
      const p = positions[n.id]; if (!p) return;
      const cat = categoryOfNode(n);
      const color = getComputedStyle(document.documentElement).getPropertyValue('--cat-' + cat).trim() || '#6B7280';
      const label = (n.label || n.id || '').replace(/cause:[a-z]+:/, '').slice(0, 32);
      svg += `<rect x="${p.x - 100}" y="${p.y - 14}" width="200" height="28" rx="6" fill="${color}" opacity="0.92"/>`;
      svg += `<text x="${p.x}" y="${p.y + 4}" text-anchor="middle" font-size="11" fill="white" font-weight="600">${escapeXml(label)}</text>`;
    });
    svg += '</svg>';
    return svg;
  }

  function escapeXml(s) {
    return String(s).replace(/[<>&'"]/g, c => ({'<':'&lt;','>':'&gt;','&':'&amp;',"'":'&apos;','"':'&quot;'}[c]));
  }

  function renderGraph(inf) {
    const graph = inf.graph || { nodes: [], edges: [], stats: {} };
    const stats = graph.stats || {};
    const wrap = el('div', 'graph-wrap');
    const header = el('div', 'graph-header');
    header.appendChild(el('div', 'graph-title', '온톨로지 그래프 — cause → treatment → evidence'));

    // Category filter chips (only cause categories in this graph)
    const cats = new Set();
    (graph.nodes || []).forEach(n => { if (n.kind === 'cause') cats.add(n.category); });
    const chips = el('div', 'filter-chips');
    Array.from(cats).sort().forEach(cat => {
      const chip = el('div', 'filter-chip', cat);
      chip.style.borderLeft = `4px solid var(--cat-${cat}, #6B7280)`;
      if (activeFilters.size === 0 || activeFilters.has(cat)) chip.classList.add('active');
      chip.addEventListener('click', () => {
        if (activeFilters.has(cat)) activeFilters.delete(cat);
        else activeFilters.add(cat);
        renderAll();
      });
      chips.appendChild(chip);
    });
    if (cats.size > 1) {
      const resetChip = el('div', 'filter-chip', '⟲ all');
      resetChip.addEventListener('click', () => { activeFilters.clear(); renderAll(); });
      chips.appendChild(resetChip);
    }
    header.appendChild(chips);
    wrap.appendChild(header);

    const svgWrap = el('div');
    svgWrap.innerHTML = buildGraphSvg(graph);
    wrap.appendChild(svgWrap);

    wrap.appendChild(el('div', 'graph-stats',
      `nodes=${stats.node_count ?? 0} (cause:${stats.cause_count ?? 0} · treatment:${stats.treatment_count ?? 0} · bundle:${stats.bundle_count ?? 0}) · edges=${stats.edge_count ?? 0} · dangling=${stats.dangling_edges ?? 0}`));
    return wrap;
  }

  // ───── Render orchestrator ─────
  let _lastData = null, _lastSource = null;
  function render(d, source) {
    _lastData = d; _lastSource = source;
    renderAll();
  }
  function renderAll() {
    const d = _lastData; if (!d) return;
    root.innerHTML = '';
    const inf = (d.payload && d.payload.infeasibility) || d.infeasibility || {};
    const verdict = d.verdict;
    const meta = d.case_meta || {};

    // Header verdict pill
    if (verdict !== undefined) {
      verdictEl.className = 'verdict ' + (verdict && verdict.pass ? 'pass' : 'fail');
      verdictEl.textContent = verdict && verdict.pass ? `PASS (${verdict.actual_codes?.length || 0} causes)` : 'FAIL';
    } else {
      verdictEl.textContent = inf.severity || '—';
      verdictEl.className = 'verdict';
    }
    metaInfo.textContent = meta.category ? `${meta.category} · ${meta.id}` : '';

    root.appendChild(renderBanner(inf, meta));
    root.appendChild(renderNarrative(inf));
    root.appendChild(renderGraph(inf));

    // Advanced (raw debug)
    const adv = el('details', 'advanced');
    adv.appendChild(el('summary', null, '자세히 보기 (payload · graph · verdict raw)'));
    const causes = inf.causes || [];
    if (causes.length) {
      const s = el('div', 'adv-section');
      s.appendChild(el('h4', null, `Causes (${causes.length}) — raw`));
      s.appendChild(el('pre', null, JSON.stringify(causes, null, 2)));
      adv.appendChild(s);
    }
    if (inf.treatment_recommendations) {
      const s = el('div', 'adv-section');
      s.appendChild(el('h4', null, `Treatments (${inf.treatment_recommendations.length} bundles)`));
      s.appendChild(el('pre', null, JSON.stringify(inf.treatment_recommendations, null, 2)));
      adv.appendChild(s);
    }
    if (inf.hard_case) {
      const s = el('div', 'adv-section');
      s.appendChild(el('h4', null, 'Hard Case Verdict'));
      s.appendChild(el('pre', null, JSON.stringify(inf.hard_case, null, 2)));
      adv.appendChild(s);
    }
    if (inf.graph) {
      const s = el('div', 'adv-section');
      s.appendChild(el('h4', null, `Graph (${(inf.graph.nodes || []).length} nodes / ${(inf.graph.edges || []).length} edges)`));
      s.appendChild(el('pre', null, JSON.stringify(inf.graph, null, 2)));
      adv.appendChild(s);
    }
    if (verdict) {
      const s = el('div', 'adv-section');
      s.appendChild(el('h4', null, 'Verdict (assert_case)'));
      s.appendChild(el('pre', null, JSON.stringify(verdict, null, 2)));
      adv.appendChild(s);
    }
    root.appendChild(adv);
  }

  loadCases();
  </script>
</body>
</html>
"""


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def dashboard() -> HTMLResponse:
    return HTMLResponse(_HTML_V2)


@router.get("/legacy", response_class=HTMLResponse)
def dashboard_legacy() -> HTMLResponse:
    """기존 cytoscape + side panel UI — 참고용 보존."""
    return HTMLResponse(_HTML)
