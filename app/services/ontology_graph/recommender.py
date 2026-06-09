"""Action 추천 + 최소변경 랭킹 (US-004).

통합 그래프에서 부족(shortage) state → pressures → constraint 경로를 따라
그 제약을 mitigates 하는 ActionNode 후보를 모으고, **최소변경 우선**으로 랭크한다.

최소변경 랭킹 기준(낮을수록 우선):
  effective_cost = action.cost - primary_bonus
    · action.cost = 침습도(set_threshold<force_soft<disable) + relaxation_priority
    · primary_bonus: 그 제약이 부족 state 의 수요를 '직접 정의'(requires 엣지)하면 가산.
      → coverage 부족이면 CoverageMin(수요 정의자) 액션이 1순위로 올라온다.

각 action 은 적용 가능한 config delta(노브·방향·양)를 동반한다. delta.amount 는
해당 부족량(shortage) — 즉 '부족한 만큼만' 바꾸는 최소 변경.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from services.ontology_graph.schema import OntologyGraph

_PRIMARY_BONUS = 10.0


@dataclass
class RankedAction:
    rank: int
    action_id: str
    target_family: str
    action_type: str
    config_key: str | None
    direction: str | None
    cost: float
    effective_cost: float
    is_primary: bool
    targets: list[str]              # mitigates 하는, 압력받는 제약 노드 id
    delta: dict[str, Any]           # 적용할 config 변경 (노브·방향·양)
    rationale: str = ""


def _build_delta(action, shortage: int) -> dict[str, Any]:
    at = action.action_type
    if at == "set_threshold":
        # direction(decrease=수요/상한 하향, increase=상한 상향) + 부족한 만큼만
        return {"kind": "set_threshold", "config_key": action.config_key,
                "direction": action.direction or "decrease", "amount": int(shortage)}
    if at == "force_soft_mode":
        return {"kind": "force_soft_mode", "config_key": action.config_key, "value": True}
    if at == "disable_module":
        return {"kind": "disable_module", "config_key": action.config_key, "op": "clear"}
    if at == "narrow_scope":
        return {"kind": "narrow_scope", "config_key": action.config_key}
    return {"kind": at, "config_key": action.config_key}


def recommend_actions(graph: OntologyGraph, *, top_k: int | None = None) -> list[RankedAction]:
    """부족 state 가 압박하는 제약을 완화할 action 들을 최소변경 우선으로 랭크."""
    # 1. 부족 state 수집
    bottleneck = [
        s for s in graph.nodes_of_kind("state")
        if int(s.evidence.get("shortage", 0) or 0) > 0
    ]
    if not bottleneck:
        return []

    pressured: dict[str, int] = {}       # constraint_id -> max shortage
    primary: set[str] = set()            # 수요를 직접 정의(requires)하는 제약
    for st in bottleneck:
        short = int(st.evidence.get("shortage", 0) or 0)
        for e in graph.out_edges(st.node_id, "pressures"):
            pressured[e.target_id] = max(pressured.get(e.target_id, 0), short)
        # constraint --requires--> state : state 의 incoming requires = 수요 정의자
        for e in graph.in_edges(st.node_id, "requires"):
            primary.add(e.source_id)

    # 2. 압력받는 제약을 mitigates 하는 action 수집
    cand: dict[str, dict[str, Any]] = {}
    for cid, short in pressured.items():
        for e in graph.in_edges(cid, "mitigates"):
            act = graph.nodes.get(e.source_id)
            if act is None or act.kind != "action":
                continue
            entry = cand.setdefault(act.node_id, {"action": act, "targets": set(), "shortage": 0})
            entry["targets"].add(cid)
            entry["shortage"] = max(entry["shortage"], short)

    # 3. RankedAction 생성 + effective_cost 계산
    ranked: list[RankedAction] = []
    for aid, info in cand.items():
        act = info["action"]
        is_primary = any(t in primary for t in info["targets"])
        eff = float(act.cost) - (_PRIMARY_BONUS if is_primary else 0.0)
        delta = _build_delta(act, info["shortage"])
        ranked.append(RankedAction(
            rank=0, action_id=aid, target_family=act.target_family or "",
            action_type=act.action_type, config_key=act.config_key, direction=act.direction,
            cost=float(act.cost), effective_cost=eff, is_primary=is_primary,
            targets=sorted(info["targets"]), delta=delta,
            rationale=f"{act.label} (부족 {info['shortage']} 해소, {'1차' if is_primary else '간접'})",
        ))

    # 4. 최소변경 우선: effective_cost asc, 동률이면 set_threshold(미세) 우선
    ranked.sort(key=lambda r: (r.effective_cost, 0 if r.action_type == "set_threshold" else 1, r.action_id))
    for i, r in enumerate(ranked):
        r.rank = i + 1
    return ranked[:top_k] if top_k else ranked
