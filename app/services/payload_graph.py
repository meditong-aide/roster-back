"""U-5 — payload.infeasibility.graph 빌더.

사용자 ralph 요구 (2026-05-17):
  "그래프 명확하게 만들고"

설계:
  nodes: 5 종 kind — cause | symptom | treatment | bundle | evidence | hard_case_meta
  edges: 4 종 kind — causal (cause→symptom) | treatment (cause→treatment|bundle)
                    | evidence (treatment/bundle→evidence)
                    | aggregation (cause→hard_case_meta)

invariant (테스트로 enforced):
  - 모든 edge.src / edge.dst 는 nodes 의 id 셋에 존재 (dangling edge 0건).
  - cause / treatment 노드의 category 는 ontology 카탈로그 라벨링.
  - bundle 노드는 그 안의 atomic treatments 노드와 'member' 엣지로 연결 (참조 필요시).

frontend / UI / debug 인쇄용 자족(self-contained) 그래프 — 별도 ontology lookup
없이도 노드 라벨/카테고리/티어 모두 인라인 포함.
"""

from __future__ import annotations

from typing import Any, Optional

from services.semantics.ontology import ConstraintOntology, get_default_ontology


def _cause_node(
    cause_dict: dict[str, Any],
    onto: ConstraintOntology,
) -> tuple[dict[str, Any], str]:
    """cause dict → (node, canonical_node_id)."""
    raw = cause_dict.get("node_id") or cause_dict.get("reason_code") or "cause:unknown"
    resolved = onto.resolve_cause_alias(str(raw)) or str(raw)
    cause = onto.get_cause(resolved) if resolved else None
    label = (cause.label if cause else cause_dict.get("human_message_ko") or resolved)
    category = (cause.category if cause else "uncategorized")
    tier = (cause.tier if cause else None)
    causal_layer = (cause.causal_layer if cause else None)
    is_hard = (cause.is_hard if cause else True)
    return (
        {
            "id": resolved,
            "kind": "cause",
            "label": label,
            "category": category,
            "tier": tier,
            "causal_layer": causal_layer,
            "is_hard": is_hard,
            "reason_code": cause_dict.get("reason_code"),
        },
        resolved,
    )


def _symptom_node(symptom_dict: dict[str, Any]) -> tuple[dict[str, Any], str]:
    raw = symptom_dict.get("node_id") or symptom_dict.get("reason_code") or "symptom:unknown"
    sid = f"symptom:{str(raw).lower()}" if not str(raw).startswith("symptom:") else str(raw)
    return (
        {
            "id": sid,
            "kind": "symptom",
            "label": symptom_dict.get("human_message_ko") or str(raw),
            "category": "observed",
            "reason_code": symptom_dict.get("reason_code"),
        },
        sid,
    )


def _treatment_node(t_dict: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": t_dict.get("treatment_id") or "treatment:unknown",
        "kind": "treatment",
        "label": t_dict.get("treatment_id") or "treatment:unknown",
        "category": (t_dict.get("target_family") or "").lower() or "treatment",
        "action_type": t_dict.get("action_type"),
        "config_key": t_dict.get("config_key"),
        "direction": t_dict.get("direction"),
        "cost": t_dict.get("cost"),
        "rationale_ko": t_dict.get("rationale_ko"),
        "trade_off_ko": t_dict.get("trade_off_ko"),
    }


def _bundle_node(bundle_dict: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": bundle_dict.get("bundle_id") or "bundle:unknown",
        "kind": "bundle",
        "label": bundle_dict.get("bundle_id") or "bundle:unknown",
        "category": "bundle",
        "total_cost": bundle_dict.get("total_cost"),
        "overhead": bundle_dict.get("overhead"),
        "covered_count": len(bundle_dict.get("covered_causes") or []),
        "uncovered_count": len(bundle_dict.get("uncovered_causes") or []),
    }


def build_payload_graph(
    *,
    causes: list[dict[str, Any]] | None,
    symptoms: list[dict[str, Any]] | None = None,
    treatment_bundles: list[dict[str, Any]] | None = None,
    evidence: dict[str, Any] | None = None,
    hard_case: dict[str, Any] | None = None,
    ontology: Optional[ConstraintOntology] = None,
) -> dict[str, Any]:
    """payload.infeasibility.graph dict 생성.

    Returns:
        {
          "nodes": [{id, kind, label, category, ...}],
          "edges": [{src, dst, kind, weight?}],
          "stats": {nodes: N, edges: M, dangling_edges: 0}
        }
    """
    onto = ontology or get_default_ontology()

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    node_ids: set[str] = set()

    def _add_node(node: dict[str, Any]) -> None:
        nid = node.get("id")
        if not nid or nid in node_ids:
            return
        node_ids.add(nid)
        nodes.append(node)

    # 1) cause nodes + cause_id 매핑 (alias resolution)
    cause_id_map: dict[str, str] = {}  # raw reason_code → canonical cause_id
    for c in causes or []:
        node, cid = _cause_node(c, onto)
        _add_node(node)
        raw_code = c.get("reason_code") or c.get("node_id")
        if raw_code:
            cause_id_map[str(raw_code)] = cid
        cause_id_map[cid] = cid

    # 2) symptom nodes
    symptom_ids: list[str] = []
    for s in symptoms or []:
        node, sid = _symptom_node(s)
        _add_node(node)
        symptom_ids.append(sid)

    # 3) causal edges: 각 cause → 모든 symptom (CAUSAL pattern)
    #    cause_id_map values 는 alias+canonical 양쪽 키를 갖고 있으므로 dedup 후 발행
    unique_cause_ids = sorted(set(cause_id_map.values()))
    if causes and symptom_ids:
        for cid in unique_cause_ids:
            for sid in symptom_ids:
                edges.append({"src": cid, "dst": sid, "kind": "causal"})

    # 4) treatment / bundle nodes + treatment hyperedges
    for bundle in (treatment_bundles or []):
        _add_node(_bundle_node(bundle))
        b_id = bundle.get("bundle_id")
        for t in (bundle.get("treatments") or []):
            t_node = _treatment_node(t)
            _add_node(t_node)
            t_id = t.get("treatment_id")
            # bundle membership edge (bundle → treatment)
            if b_id and t_id:
                edges.append({"src": b_id, "dst": t_id, "kind": "member"})
            # treatment cover edges (cause → treatment) — only for covered causes
            for covered in (t.get("covers") or []):
                resolved = cause_id_map.get(str(covered)) or onto.resolve_cause_alias(str(covered)) or str(covered)
                if resolved in node_ids and t_id:
                    edges.append({"src": resolved, "dst": t_id, "kind": "treatment"})
        # also link bundle → covered causes (aggregate edge)
        for covered in (bundle.get("covered_causes") or []):
            resolved = cause_id_map.get(str(covered)) or onto.resolve_cause_alias(str(covered)) or str(covered)
            if resolved in node_ids and b_id:
                edges.append({"src": resolved, "dst": b_id, "kind": "treatment"})

    # 5) evidence node + evidence edges
    if evidence and isinstance(evidence, dict):
        ev_id = "evidence:current_run"
        ev_node = {
            "id": ev_id,
            "kind": "evidence",
            "label": evidence.get("proof_type") or "evidence",
            "category": "evidence",
            "status": evidence.get("status"),
            "verified": evidence.get("verified"),
            "witness_schedule_id": evidence.get("witness_schedule_id"),
        }
        _add_node(ev_node)
        # link each bundle → evidence
        for bundle in (treatment_bundles or []):
            b_id = bundle.get("bundle_id")
            if b_id and b_id in node_ids:
                edges.append({"src": b_id, "dst": ev_id, "kind": "evidence"})

    # 6) hard_case meta node + aggregation edges
    if hard_case and hard_case.get("is_hard"):
        hc_id = "hard_case:current"
        _add_node({
            "id": hc_id,
            "kind": "hard_case_meta",
            "label": "어려운 케이스 — 복합 원인",
            "category": "meta",
            "criteria_matched": list(hard_case.get("criteria_matched") or []),
            "cause_count": hard_case.get("cause_count"),
            "category_count": hard_case.get("category_count"),
        })
        for cid in unique_cause_ids:
            edges.append({"src": cid, "dst": hc_id, "kind": "aggregation"})

    # 7) sanity: drop dangling edges (defensive — should never happen)
    valid_edges: list[dict[str, Any]] = []
    dangling = 0
    for e in edges:
        if e.get("src") in node_ids and e.get("dst") in node_ids:
            valid_edges.append(e)
        else:
            dangling += 1

    return {
        "nodes": nodes,
        "edges": valid_edges,
        "stats": {
            "node_count": len(nodes),
            "edge_count": len(valid_edges),
            "dangling_edges": dangling,
            "cause_count": sum(1 for n in nodes if n.get("kind") == "cause"),
            "symptom_count": sum(1 for n in nodes if n.get("kind") == "symptom"),
            "treatment_count": sum(1 for n in nodes if n.get("kind") == "treatment"),
            "bundle_count": sum(1 for n in nodes if n.get("kind") == "bundle"),
        },
    }
