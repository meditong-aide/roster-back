"""소프트 제약 penalty 계산 + 그래프 연결.

소프트 제약은 그동안 objective weight 로만 존재했고, 통합 그래프에 노드 stub
으로만 등재돼 엣지가 없었다(고립). 이 모듈이 그 갭을 메운다:

  1. 생성된 근무표에서 각 소프트 family 의 '위반량(penalty)'을 결정론적으로 계산
     → 하드의 supply_demand 와 동형(state 노드로 체크 가능).
  2. 그래프에 연결:
       penalty(state) --pressures--> soft_constraint        (위반이 제약을 압박)
       action          --mitigates--> soft_constraint        (weight 조정으로 완화)
       soft_constraint --constrains-> shift                  (적용 대상)
     → 소프트도 하드와 같은 방식으로 체크·통제 가능해진다.
"""

from __future__ import annotations

from typing import Any

from services.ontology_graph.schema import ActionNode, OntologyGraph, StateNode
from services.ontology_graph.builder import ACTION_INVASIVENESS

WORK = ("D", "E", "N", "M")


def _norm(code: object) -> str:
    c = str(code).strip().upper()
    if c in {"-", "OFF", "주", "", "NONE"}:
        return "O"
    return c


def compute_soft_penalties(
    roster: dict[str, list[Any]],
    *,
    work_shifts: tuple[str, ...] | list[str] = WORK,
) -> dict[str, int]:
    """근무표 → 소프트 family 별 위반량(클수록 나쁨). 결정론적.

    night_deviation : 간호사 간 N 횟수 편차(max-min)
    isolated_work   : O W O (양쪽 OFF 사이 단일 근무) 개수
    isolated_off    : W O W (양쪽 근무 사이 단일 OFF) 개수
    nod_noe         : N O D / N O E (야간 후 짧은 회복) 개수
    """
    work = set(work_shifts)
    seqs = {nid: [_norm(c) for c in shifts] for nid, shifts in roster.items()
            if isinstance(shifts, list)}

    # night_deviation
    n_counts = [sum(1 for c in s if c == "N") for s in seqs.values()]
    night_dev = (max(n_counts) - min(n_counts)) if n_counts else 0

    iso_work = iso_off = nod_noe = 0
    for s in seqs.values():
        for i in range(1, len(s) - 1):
            prev, cur, nxt = s[i - 1], s[i], s[i + 1]
            is_work = cur in work
            if is_work and prev == "O" and nxt == "O":
                iso_work += 1
            if cur == "O" and prev in work and nxt in work:
                iso_off += 1
        for i in range(len(s) - 2):
            if s[i] == "N" and s[i + 1] == "O" and s[i + 2] in {"D", "E"}:
                nod_noe += 1

    return {
        "night_deviation": int(night_dev),
        "isolated_work": int(iso_work),
        "isolated_off": int(iso_off),
        "nod_noe": int(nod_noe),
    }


# 소프트 family → constrains 대상 shift (없으면 전체 work shift)
_SOFT_SHIFT = {
    "night_deviation": ["N"],
    "nod_noe": ["N"],
}


def augment_soft_constraints(
    graph: OntologyGraph,
    penalties: dict[str, int],
    *,
    work_shifts: tuple[str, ...] | list[str] = WORK,
    ontology=None,
) -> OntologyGraph:
    """그래프의 soft 제약 노드에 penalty state + action + 엣지를 연결한다."""
    from services.semantics.ontology import ConstraintOntology
    ont = ontology or ConstraintOntology()

    soft_nodes = {n.node_id: n for n in graph.nodes_of_kind("constraint") if n.severity == "soft"}

    for sid, sc in ont.soft_constraints.items():
        cid = f"soft:{sid}"
        if cid not in soft_nodes:
            continue
        amount = int(penalties.get(sid, 0) or 0)

        # penalty state → pressures → soft constraint
        sstate = f"state:soft_penalty:{sid}"
        if sstate not in graph.nodes:
            graph.add_node(StateNode(
                node_id=sstate, label=f"{sc.label} 위반량", state_type="soft_penalty",
                severity="soft" if amount > 0 else "none",
                attrs={"soft_id": sid, "lex_stage": sc.lex_stage},
                evidence={"penalty": amount, "weight_constant": sc.weight_constant}))
            graph.add_edge("pressures", sstate, cid)

        # action(weight 조정) → mitigates → soft constraint
        if sc.config_key:
            aid = f"action:soft:{sid}"
            if aid not in graph.nodes:
                graph.add_node(ActionNode(
                    node_id=aid, label=f"{sc.label} 가중치 조정", action_type="set_threshold",
                    target_family=sid, config_key=sc.config_key, direction="decrease",
                    cost=ACTION_INVASIVENESS["set_threshold"], reversible=True, auto_applicable=True,
                    attrs={"soft": True, "lex_stage": sc.lex_stage}))
                graph.add_edge("mitigates", aid, cid)

        # soft constraint → constrains → shift (적용 대상)
        targets = _SOFT_SHIFT.get(sid, list(work_shifts))
        for s in targets:
            snode = f"shift:{s}"
            if snode in graph.nodes:
                graph.add_edge("constrains", cid, snode)

    return graph
