"""구조적 부족 검출기 (team / grade / monthly-night).

supply_demand max-flow 는 글로벌 coverage 병목을 잡지만, team_min·grade_max·
월 N cap 위반은 파티션/누적 제약이라 글로벌 flow 로는 안 보인다. 이들을
결정론적 산술로 검출해 통합 그래프에 shortage state + pressures + mitigates 로
합류시킨다. (연구의 'team×grade intersection min-cut' 의 경량 결정론 버전.)
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from services.ontology_graph.schema import ActionNode, ConstraintNode, OntologyGraph, StateNode

# 침습도 기저 (builder 와 동일 기준)
from services.ontology_graph.builder import ACTION_INVASIVENESS


def _ensure_constraint(graph: OntologyGraph, node_id: str, family: str, label: str,
                       operator: str, target: Any, ont) -> str:
    if node_id in graph.nodes:
        return node_id
    ent = ont.constraints.get(family)
    prio = int(ent.relaxation_priority) if (ent and ent.relaxation_priority) else 5
    graph.add_node(ConstraintNode(node_id=node_id, label=label, family=family, severity="hard",
                                  operator=operator, target=target,
                                  attrs={"relaxation_priority": prio, "scope": {}}))
    # 이 family 를 mitigates 하는 action 들 부착
    for tid, t in ont.treatments.items():
        if t.target_family != family:
            continue
        at = t.action_type if t.action_type in ACTION_INVASIVENESS else "set_threshold"
        aid = f"action:{tid}"
        if aid not in graph.nodes:
            graph.add_node(ActionNode(
                node_id=aid, label=t.label, action_type=at, target_family=family,
                config_key=t.config_key, direction=t.direction,
                cost=ACTION_INVASIVENESS[at] + prio,
                reversible=(at != "data_correction_required"),
                auto_applicable=(at in {"force_soft_mode", "set_threshold"}),
                attrs={"relaxation_priority": prio}))
        graph.add_edge("mitigates", aid, node_id)
    return node_id


def _add_shortage(graph: OntologyGraph, state_id: str, label: str, evidence: dict,
                  constraint_id: str) -> None:
    if state_id not in graph.nodes:
        graph.add_node(StateNode(node_id=state_id, label=label, state_type="skill_supply",
                                 severity="hard", attrs={}, evidence=evidence))
    # constraint --requires--> state (수요 정의자 = primary), state --pressures--> constraint
    graph.add_edge("requires", constraint_id, state_id)
    graph.add_edge("pressures", state_id, constraint_id)


def augment_structural_shortages(
    graph: OntologyGraph,
    *,
    nurses: list[dict[str, Any]],
    work_shifts: list[str],
    requirements_by_day: dict[int, dict[str, int]],
    team_min: dict[str, dict[str, int]] | None = None,
    grade_max: dict[str, dict[str, int]] | None = None,
    night_cap: int | None = None,
    ontology=None,
) -> OntologyGraph:
    from services.semantics.ontology import ConstraintOntology
    ont = ontology or ConstraintOntology()
    work = set(work_shifts)

    # 일별 최대 요구(대표 수요)
    max_req: dict[str, int] = {}
    for req in requirements_by_day.values():
        for s, v in (req or {}).items():
            if s in work:
                max_req[s] = max(max_req.get(s, 0), int(v or 0))

    # ── team_min: 팀별 가용(팀 소속 인원) < min, 그리고 교차시프트 과구독 ──────
    team_counts = Counter(str(n.get("team_id")) for n in nurses if n.get("team_id") is not None)
    for tid, mins in (team_min or {}).items():
        avail = team_counts.get(str(tid), 0)
        # (a) 단일 시프트 min > 가용
        for s, m in (mins or {}).items():
            if s not in work:
                continue
            shortage = int(m or 0) - avail
            if shortage > 0:
                cid = _ensure_constraint(graph, f"team_min:{tid}:agg:{s}", "TeamMin",
                                         f"팀{tid} {s} 최소 {m}", ">=", int(m), ont)
                _add_shortage(graph, f"state:team_supply:{tid}:{s}",
                              f"팀{tid} {s} 가용/요구", {"required": int(m), "available": avail,
                              "shortage": shortage}, cid)
        # (b) 교차시프트 과구독: Σ_shift team_min > 팀원 수 (각 min ≤ 가용이라 enforce 되지만
        #     하루 1시프트 제약상 동시 충족 불가)
        sum_min = sum(int(m or 0) for s, m in (mins or {}).items() if s in work)
        over = sum_min - avail
        if over > 0 and avail > 0:
            cid = _ensure_constraint(graph, f"team_min:{tid}:agg:oversub", "TeamMin",
                                     f"팀{tid} 교차시프트 합 {sum_min} > 팀원 {avail}", ">=", sum_min, ont)
            _add_shortage(graph, f"state:team_oversub:{tid}",
                          f"팀{tid} 교차시프트 과구독", {"required": sum_min, "available": avail,
                          "shortage": over}, cid)

    # ── grade_max: 등급 상한 합이 수요보다 낮음 ──────────────────────────────
    grade_counts = Counter(str(n.get("grade")) for n in nurses if n.get("grade") is not None)
    for s, by_g in (grade_max or {}).items():
        if s not in work:
            continue
        need = max_req.get(s, 0)
        if need <= 0:
            continue
        constrained = {str(g): int(v or 0) for g, v in (by_g or {}).items()}
        cap = 0
        for g, cnt in grade_counts.items():
            cap += min(cnt, constrained[g]) if g in constrained else cnt
        shortage = need - cap
        if shortage > 0:
            cid = _ensure_constraint(graph, f"grade_max:agg:{s}", "GradeMax",
                                     f"{s} 등급상한 합 {cap} < 필요 {need}", "<=", cap, ont)
            _add_shortage(graph, f"state:grade_supply:{s}",
                          f"{s} 등급상한 가용/요구", {"required": need, "available": cap,
                          "shortage": shortage}, cid)

    # ── monthly night cap: nurses×cap < 월 N 수요 ────────────────────────────
    # cap<=0 은 '무제한/미설정'(엔진 관례) — 하드 cap 아님 → 검출 skip.
    if night_cap is not None and int(night_cap) > 0 and "N" in work:
        monthly_n_demand = sum(int((req or {}).get("N", 0) or 0) for req in requirements_by_day.values())
        capacity = len(nurses) * int(night_cap)
        shortage = monthly_n_demand - capacity
        if shortage > 0:
            cid = _ensure_constraint(graph, "monthly_night_cap:agg", "MonthlyNightCap",
                                     f"월 N 용량 {capacity} < 수요 {monthly_n_demand}", "<=", capacity, ont)
            _add_shortage(graph, "state:night_supply",
                          "월 N 가용/수요", {"required": monthly_n_demand, "available": capacity,
                          "shortage": shortage}, cid)
    return graph
