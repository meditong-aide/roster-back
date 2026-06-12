"""MUS(solver conflict core) → 통합 4-노드 그래프 bridge.

연구 §4: solver-level conflict(=CP-SAT MUS) 를 domain-level cause 로 변환.
extract_conflict_cores() 가 내놓는 conflict core(서로 동시에 불가능한 하드제약 집합)를
기존 4종 노드/8엣지로 표현한다 — 새 스키마 없음:

  member constraints ──requires──▶ state:mus(충돌 집합) ──pressures──▶ 각 member
  action ──mitigates──▶ member                      (Reiter: 하나만 풀면 해소)

이로써 시퀀스/타이밍 충돌(전이금지·회복·1N 등)이 — 용량 충돌과 동일하게 —
같은 그래프 위에서 진단되고 recommend_conflict_repair/recommend_actions 로 수선된다.
"""

from __future__ import annotations

from typing import Any

from services.ontology_graph.builder import ACTION_INVASIVENESS
from services.ontology_graph.schema import ActionNode, ConstraintNode, OntologyGraph, StateNode

# member type/pattern → ontology constraint family
_TYPE_TO_FAMILY = {
    "TransitionBanNode": "BoundaryTransitionBan",
    "CarryoverTransitionNode": "CarryoverBoundary",
    "NightRecoveryNode": "NightRecovery",
    "NotOneNightNode": "NotOneNight",
    "ConsecutiveWorkNode": "ConsecutiveWorkLimit",
    "GradeMaxNode": "GradeMax",
    "ConstraintNode": "CoverageMin",
}
_PATTERN_TO_FAMILY = {
    "transition_ban": "BoundaryTransitionBan",
    "carryover_boundary": "CarryoverBoundary",
    "night_recovery": "NightRecovery",
    "not_one_night": "NotOneNight",
    "consecutive_work": "ConsecutiveWorkLimit",
}


def _family_of(member: dict, core_pattern: str) -> str:
    t = str(member.get("type") or "")
    if t in _TYPE_TO_FAMILY:
        return _TYPE_TO_FAMILY[t]
    pat = core_pattern.replace("cpsat_mus:", "")
    return _PATTERN_TO_FAMILY.get(pat, pat or "Unknown")


def cores_to_conflict_graph(
    graph: OntologyGraph,
    conflict_cores: list[dict[str, Any]],
    *,
    ontology=None,
) -> OntologyGraph:
    """conflict cores 를 그래프 충돌 구조로 등재(스키마 불변)."""
    from services.semantics.ontology import ConstraintOntology
    ont = ontology or ConstraintOntology()

    for core in conflict_cores or []:
        core_id = str(core.get("core_id") or core.get("pattern") or "core")
        pattern = str(core.get("pattern") or "")
        members = core.get("members") or []
        if not members:
            continue
        state_id = f"state:mus:{core_id}"
        if state_id not in graph.nodes:
            graph.add_node(StateNode(
                node_id=state_id, label=f"동시불가 집합({len(members)})",
                state_type="skill_supply", severity="hard",
                attrs={"mus": True, "pattern": pattern, "scope": core.get("scope")},
                evidence={"mus_size": len(members), "shortage": 1,
                          "affected": core.get("affected_count")}))

        for mm in members:
            family = _family_of(mm, pattern)
            cid = f"constraint:{mm.get('node_id') or mm.get('label')}"
            if cid not in graph.nodes:
                graph.add_node(ConstraintNode(
                    node_id=cid, label=str(mm.get("label") or family), family=family,
                    severity="hard", operator="forbid", target=None,
                    attrs={"from_mus": True, "scope": {"core": core_id}}))
            graph.add_edge("requires", cid, state_id)
            graph.add_edge("pressures", state_id, cid)

            # 완화 액션: 해당 family 의 ontology treatment, 없으면 generic disable
            attached = False
            for tid, t in ont.treatments.items():
                if t.target_family != family:
                    continue
                at = t.action_type if t.action_type in ACTION_INVASIVENESS else "disable_module"
                aid = f"action:{tid}"
                if aid not in graph.nodes:
                    graph.add_node(ActionNode(
                        node_id=aid, label=t.label, action_type=at, target_family=family,
                        config_key=t.config_key, direction=t.direction,
                        cost=ACTION_INVASIVENESS[at], reversible=(at != "data_correction_required"),
                        auto_applicable=(at in {"force_soft_mode", "set_threshold"})))
                graph.add_edge("mitigates", aid, cid)
                attached = True
            if not attached:
                aid = f"action:relax:{cid}"
                if aid not in graph.nodes:
                    graph.add_node(ActionNode(
                        node_id=aid, label=f"{family} 일시 완화", action_type="disable_module",
                        target_family=family, config_key=None, direction="disable",
                        cost=ACTION_INVASIVENESS["disable_module"]))
                graph.add_edge("mitigates", aid, cid)
    return graph


def mcs_to_graph(graph: OntologyGraph, mcs_result, *, ontology=None) -> OntologyGraph:
    """MCS(검증된 최소 수선집합) → 그래프. 완화할 제약 = 곧 실행할 액션.

    MUS bridge 와 달리 '집합'이 아니라 '풀면 feasible 한 최소 제약'만 노드화하므로
    그래프가 간결하고, verified state 가 '재실행 feasible 확인'을 담는다.
    """
    from services.semantics.ontology import ConstraintOntology
    ont = ontology or ConstraintOntology()
    # 검증 state: "이 집합을 풀면 feasible"
    vstate = "state:mcs:verified"
    if mcs_result.relaxed and vstate not in graph.nodes:
        graph.add_node(StateNode(
            node_id=vstate, label=f"최소수선({len(mcs_result.relaxed)}) → feasible",
            state_type="skill_supply",
            severity="hard" if mcs_result.verified_feasible else "none",
            attrs={"mcs": True, "verified": mcs_result.verified_feasible},
            evidence={"relaxed_count": len(mcs_result.relaxed), "shortage": 1,
                      "verified_feasible": mcs_result.verified_feasible}))
    for meta in mcs_result.relaxed_meta:
        family = _family_of(meta, str(meta.get("pattern") or ""))
        cid = f"constraint:{meta.get('node_id') or meta.get('name')}"
        if cid not in graph.nodes:
            graph.add_node(ConstraintNode(
                node_id=cid, label=str(meta.get("label") or family), family=family,
                severity="hard", operator="forbid", target=None,
                attrs={"from_mcs": True}))
        graph.add_edge("pressures", vstate, cid)
        aid = f"action:mcs_relax:{cid}"
        if aid not in graph.nodes:
            graph.add_node(ActionNode(
                node_id=aid, label=f"{family} 완화(MCS 검증)", action_type="disable_module",
                target_family=family, config_key=None, direction="disable",
                cost=ACTION_INVASIVENESS["disable_module"], auto_applicable=False))
        graph.add_edge("mitigates", aid, cid)
    return graph
