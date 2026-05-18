"""Live API graph_export emitter.

UNRECOVERABLE (HTTP 500) 응답을 낼 때 ontology 대시보드(`/ontology/*`)가
스캔하는 `tools/harness/reports/run-*/graph_export.json` 형식으로 구조
스냅샷을 떨군다. harness rule 평가를 다시 돌리지 않기 위해 rule_results /
violations 는 비워두고 conflict_cores + pool_snapshot + violated_constraints
만으로 nodes/edges 를 구성한다.

실패 내성: 어떤 예외도 API 응답에 영향을 주지 않는다.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_REPORTS_DIR = (
    Path(__file__).resolve().parents[2] / "tools" / "harness" / "reports"
)


def _ctype_from_id(nid: str) -> str:
    s = (nid or "").lower()
    if s.startswith(("coverage:min", "coverage_min")):
        return "CoverageMinNode"
    if s.startswith(("coverage:max", "coverage_max")):
        return "CoverageMaxNode"
    if s.startswith(("team_min", "team:min")):
        return "TeamMinNode"
    if s.startswith(("team_max", "team:max")):
        return "TeamMaxNode"
    if s.startswith(("grade_min", "grade:min")):
        return "GradeMinNode"
    if s.startswith(("grade_max", "grade:max")):
        return "GradeMaxNode"
    if s.startswith(("monthly_off", "monthly:off")):
        return "MonthlyOffNode"
    if s.startswith(("weekly_off", "weekly:off")):
        return "WeeklyOffNode"
    if s.startswith(("off_window", "offwindow")):
        return "OffWindowNode"
    if s.startswith(("preceptee", "precept")):
        return "PrecepteeNode"
    if s.startswith("nurse:"):
        return "NurseNode"
    if s.startswith("fixed:"):
        return "FixedConstraintNode"
    if s.startswith("wanted:"):
        return "WantedNode"
    return "ConstraintNode"


def dump_live_graph_export(
    *,
    group_id: str,
    year: int,
    month: int,
    conflict_cores: list[dict[str, Any]] | None = None,
    pool_snapshot: dict[str, Any] | None = None,
    violated_constraints: list[dict[str, Any]] | None = None,
    applied_relaxations: list[str] | None = None,
    last_error_reason: str | None = None,
    reports_dir: Path | None = None,
) -> Path | None:
    """Write a structural-only graph_export for the live UNRECOVERABLE path.

    Returns the run directory path on success, None on failure.
    """
    try:
        target_root = reports_dir or _REPORTS_DIR
        now = datetime.now(timezone.utc)
        ts = now.strftime("%Y%m%d-%H%M%S")
        out_base = target_root / f"run-live-{year}{month:02d}-{ts}"
        out_base.mkdir(parents=True, exist_ok=True)

        run_id = f"{year}-{month:02d}-{group_id}-live-{ts}"
        run_node_id = f"run:{run_id}"
        attempt_label = "live#0"

        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, Any]] = []
        node_seen: set[str] = set()
        edge_seen: set[str] = set()
        constraint_blocks: list[dict[str, Any]] = []

        def add_node(nid: str, ntype: str, attrs: dict[str, Any]) -> None:
            if not nid or nid in node_seen:
                return
            node_seen.add(nid)
            nodes.append({"id": nid, "type": ntype, "attrs": attrs})

        def add_edge(etype: str, src: str, dst: str) -> None:
            if not (etype and src and dst):
                return
            k = f"{etype}|{src}|{dst}"
            if k in edge_seen:
                return
            edge_seen.add(k)
            edges.append({"type": etype, "from": src, "to": dst})

        add_node(
            run_node_id,
            "RunNode",
            {
                "run_id": run_id,
                "group_id": group_id,
                "year": year,
                "month": month,
                "strategy": "live-api",
                "solver_status": "UNRECOVERABLE",
                "applied_relaxations": list(applied_relaxations or []),
                "last_error_reason": last_error_reason,
            },
        )

        # Pool 그래프 먼저 — conflict member 가 thin attrs로 선점하지 못하게.
        pool_snap = pool_snapshot or {}
        for pool in (pool_snap.get("pools") or []):
            p_id = str(pool.get("pool_id") or "")
            p_type = str(pool.get("pool_type") or "PoolNode")
            if not p_id:
                continue
            attrs = dict(pool.get("attrs") or {})
            attrs["first_seen_attempt"] = attempt_label
            add_node(p_id, p_type, attrs)
        for edge in (pool_snap.get("nurse_pool_edges") or [])[:1000]:
            rel = str(edge.get("rel") or "")
            src = str(edge.get("src") or "")
            dst = str(edge.get("dst") or "")
            add_edge(rel, src, dst)

        for core in list(conflict_cores or [])[:50]:
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
                    "affected_scope_keys": core.get("affected_scope_keys") or [],
                    "per_nurse_cores": core.get("per_nurse_cores") or [],
                    "per_member_cores": core.get("per_member_cores") or [],
                    "conclusion": core.get("conclusion"),
                    "human_message_ko": core.get("human_message_ko"),
                    "derivation": core.get("derivation") or [],
                    "resolution_hints": core.get("resolution_hints") or [],
                    "source": core.get("source"),
                    "solver_phase": core.get("solver_phase"),
                    "causal_layer": core.get("causal_layer"),
                    "per_layer_counts": core.get("per_layer_counts") or {},
                    "first_seen_attempt": attempt_label,
                },
            )
            add_edge("BLOCKED_RUN", core_id, run_node_id)
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

        for vc in list(violated_constraints or [])[:50]:
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
            constraint_blocks.append(
                {
                    "node_id": cid,
                    "ctype": ctype,
                    "kind": "infeasibility",
                    "reason_code": vc.get("reason_code"),
                    "attempt": attempt_label,
                }
            )

        generated_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        graph_export = {
            "run": {
                "run_id": run_id,
                "group_id": group_id,
                "year": year,
                "month": month,
                "strategy": "live-api",
                "input_hash": "",
                "solver_status": "UNRECOVERABLE",
                "generated_at": generated_at,
            },
            "rules": [],
            "violations": [],
            "constraint_blocks": constraint_blocks,
            "tradeoff_signals": [],
            "nodes": nodes,
            "edges": edges,
            "mapping_summary": {
                "rules_total": 0,
                "mapped_rules_count": 0,
                "fail_rules_total": 0,
                "fail_rules_with_constraint_nodes": 0,
                "fail_rules_without_constraint_nodes": [],
                "unmapped_rules": [],
            },
            "consistency": {
                "fail_rule_count": 0,
                "graph_violation_count": 0,
                "missing_in_graph": [],
                "extra_in_graph": [],
            },
        }

        summary = {
            "generated_at": generated_at,
            "status": "FAIL",
            "blocking_fail_count": 0,
            "blocking_skipped_count": 0,
            "warning_fail_count": 0,
            "rules_total": 0,
            "strict_mode": False,
            "rules": [],
            "source": "live-api",
        }

        (out_base / "graph_export.json").write_text(
            json.dumps(graph_export, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (out_base / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(
            f"[LiveGraphExport] dumped → {out_base.name} "
            f"(nodes={len(nodes)}, edges={len(edges)}, "
            f"cores={len(list(conflict_cores or []))}, "
            f"pools={len((pool_snap.get('pools') or []))})"
        )
        return out_base
    except Exception as exc:
        print(f"[LiveGraphExport] dump 실패(무시): {exc}")
        return None
