"""Unified graph builder — 하드+소프트+상태+객체를 하나의 4-노드 그래프로 합류.

기존 2레이어(constraint_impact 런타임 그래프 / semantics 온톨로지)를 대체하는
단일 그래프를 구성한다. 입력은 ward-agnostic 한 경량 구조(UnifiedGraphInput)로
받아, US-001 supply_demand(state) + US-002 soft 카탈로그(soft constraint) +
온톨로지 treatments(action)를 한 그래프에 wiring 한다.

엣지 의미(스키마 거버넌스 적용):
  constraint --constrains--> domain_object   (제약이 객체에 적용)
  constraint --requires----> state            (제약이 수요 상태를 만든다)
  state(shortage) --pressures--> constraint   (부족이 제약 위반 압력)
  action --mitigates--> constraint            (완화가 충돌 해소)
  nurse --belongs_to--> team / grade          (소속)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from services.ontology_graph.schema import (
    ActionNode,
    ConstraintNode,
    DomainObjectNode,
    OntologyGraph,
)
from services.ontology_graph.supply_demand import (
    MonthlySupplyDemandResult,
    NurseSupply,
    SupplyDemandResult,
    compute_supply_demand,
    to_monthly_state_nodes,
    to_state_nodes,
)

# action_type 별 침습도(=최소변경 비용 기저). set_threshold 가 가장 미세,
# disable_module 이 가장 과격. E3(최소변경) 랭킹의 핵심 기저값.
ACTION_INVASIVENESS: dict[str, float] = {
    "set_threshold": 1.0,
    "force_soft_mode": 2.0,
    "narrow_scope": 2.5,
    "data_correction_required": 3.0,
    "disable_module": 4.0,
}


@dataclass
class UnifiedGraphInput:
    """ward-agnostic 빌더 입력. 실제 ward 데이터는 US-005 하네스가 이 형태로 채운다."""
    nurses: list[dict[str, Any]]                 # {nurse_id, grade, team_id}
    num_days: int
    work_shifts: list[str]
    requirements_by_day: dict[int, dict[str, int]]
    team_min: dict[str, dict[str, int]] = field(default_factory=dict)       # team -> {shift: min}
    grade_min: dict[str, dict[str, int]] = field(default_factory=dict)      # shift -> {grade: min}
    grade_max: dict[str, dict[str, int]] = field(default_factory=dict)      # shift -> {grade: max}
    # 간호사별 day→eligible work shift 집합 (supply_demand 계산용). 없으면 grade/team 무관 전원 가용 가정.
    eligible_by_nurse_day: dict[str, dict[int, set[str]]] = field(default_factory=dict)
    leaves: list[dict[str, Any]] = field(default_factory=list)              # {nurse_id, day} 연차
    wanted_offs: list[dict[str, Any]] = field(default_factory=list)         # {nurse_id, day} 희망OFF


def _nurse_supplies(inp: UnifiedGraphInput) -> list[NurseSupply]:
    out: list[NurseSupply] = []
    work = set(inp.work_shifts)
    for n in inp.nurses:
        nid = str(n["nurse_id"])
        elig = inp.eligible_by_nurse_day.get(nid)
        if elig is None:
            # eligibility 미제공 → 모든 활성일에 모든 work shift 가능 가정
            elig = {d: set(work) for d in range(inp.num_days)}
        out.append(NurseSupply(nurse_id=nid, grade=n.get("grade"), team_id=n.get("team_id"),
                               eligible_by_day=elig))
    return out


def build_unified_graph(
    inp: UnifiedGraphInput,
    *,
    ontology=None,
    supply_demand: SupplyDemandResult | None = None,
    monthly_supply_demand: MonthlySupplyDemandResult | None = None,
) -> OntologyGraph:
    """UnifiedGraphInput → 4-노드 통합 그래프."""
    from services.semantics.ontology import ConstraintOntology
    ont = ontology or ConstraintOntology()
    g = OntologyGraph()

    # ── 1. supply/demand state (US-001) ─────────────────────────────────────
    if supply_demand is None:
        supply_demand = compute_supply_demand(
            _nurse_supplies(inp), inp.requirements_by_day, work_shifts=inp.work_shifts
        )
    state_by_cell: dict[tuple[int, str], str] = {}
    for sn in to_state_nodes(supply_demand):
        g.add_node(sn)
        state_by_cell[(sn.attrs["day_index"], sn.attrs["shift_code"])] = sn.node_id

    # ── 2. domain objects (nurse/day/shift/team/grade/leave/wanted_off) ──────
    teams: set[str] = set()
    grades: set[str] = set()
    for n in inp.nurses:
        nid = str(n["nurse_id"])
        g.add_node(DomainObjectNode(node_id=f"nurse:{nid}", label=f"간호사 {nid}", object_type="nurse"))
        if n.get("team_id") is not None:
            tid = str(n["team_id"]); teams.add(tid)
        if n.get("grade") is not None:
            gid = str(n["grade"]); grades.add(gid)
    for tid in sorted(teams):
        g.add_node(DomainObjectNode(node_id=f"team:{tid}", label=f"팀 {tid}", object_type="team"))
    for gid in sorted(grades):
        g.add_node(DomainObjectNode(node_id=f"grade:{gid}", label=f"등급 {gid}", object_type="grade"))
    for s in inp.work_shifts:
        g.add_node(DomainObjectNode(node_id=f"shift:{s}", label=f"시프트 {s}", object_type="shift"))
    # day 를 1급 도메인 객체 노드로
    for d in range(inp.num_days):
        g.add_node(DomainObjectNode(node_id=f"day:{d}", label=f"{d + 1}일", object_type="day",
                                    attrs={"day_index": d}))
    # belongs_to: nurse → team / grade
    for n in inp.nurses:
        nid = str(n["nurse_id"])
        if n.get("team_id") is not None:
            g.add_edge("belongs_to", f"nurse:{nid}", f"team:{n['team_id']}")
        if n.get("grade") is not None:
            g.add_edge("belongs_to", f"nurse:{nid}", f"grade:{n['grade']}")
    # leave / wanted_off → reduces → supply_demand state
    for lv in inp.leaves:
        oid = f"leave:{lv['nurse_id']}:{lv['day']}"
        g.add_node(DomainObjectNode(node_id=oid, label="연차", object_type="leave",
                                    attrs={"nurse_id": str(lv["nurse_id"]), "day": int(lv["day"])}))
        for s in inp.work_shifts:
            cell = state_by_cell.get((int(lv["day"]), s))
            if cell:
                g.add_edge("reduces", oid, cell)
    for wo in inp.wanted_offs:
        oid = f"wanted_off:{wo['nurse_id']}:{wo['day']}"
        g.add_node(DomainObjectNode(node_id=oid, label="희망OFF", object_type="wanted_off",
                                    attrs={"nurse_id": str(wo["nurse_id"]), "day": int(wo["day"])}))
        for s in inp.work_shifts:
            cell = state_by_cell.get((int(wo["day"]), s))
            if cell:
                g.add_edge("reduces", oid, cell)

    # ── 3. hard constraint nodes + requires/pressures/constrains ─────────────
    def _family_meta(family: str) -> tuple[int, str]:
        ent = ont.constraints.get(family)
        prio = int(ent.relaxation_priority) if (ent and ent.relaxation_priority) else 5
        sev = (ent.default_severity if ent else None) or "hard"
        return prio, sev

    constraints_by_family: dict[str, list[str]] = {}

    def _add_constraint(node_id, family, label, scope, operator, target, severity="hard"):
        prio, _ = _family_meta(family)
        g.add_node(ConstraintNode(node_id=node_id, label=label, family=family, severity=severity,
                                  operator=operator, target=target,
                                  attrs={"relaxation_priority": prio, "scope": scope}))
        constraints_by_family.setdefault(family, []).append(node_id)
        # constrains: 제약 → day 도메인 객체 (scope 에 day 가 있으면)
        d = scope.get("day")
        if d is not None and f"day:{d}" in g.nodes:
            g.add_edge("constrains", node_id, f"day:{d}")
        return node_id

    for day, req in inp.requirements_by_day.items():
        for s, need in (req or {}).items():
            if s not in inp.work_shifts or int(need or 0) <= 0:
                continue
            cid = _add_constraint(f"cov_min:{day}:{s}", "CoverageMin",
                                  f"day {day+1} {s} 최소 {need}", {"day": day, "shift": s}, ">=", int(need))
            g.add_edge("constrains", cid, f"shift:{s}")
            cell = state_by_cell.get((day, s))
            if cell:
                g.add_edge("requires", cid, cell)

    # team_min / grade_min / grade_max — per (cell) where applicable
    for tid, mins in (inp.team_min or {}).items():
        for s, m in (mins or {}).items():
            if s not in inp.work_shifts or int(m or 0) <= 0:
                continue
            for day in inp.requirements_by_day:
                cid = _add_constraint(f"team_min:{tid}:{day}:{s}", "TeamMin",
                                      f"팀{tid} day{day+1} {s} 최소 {m}", {"team": tid, "day": day, "shift": s}, ">=", int(m))
                if f"team:{tid}" in g.nodes:
                    g.add_edge("constrains", cid, f"team:{tid}")
    for s, by_g in (inp.grade_max or {}).items():
        for gd, mx in (by_g or {}).items():
            if s not in inp.work_shifts:
                continue
            for day in inp.requirements_by_day:
                cid = _add_constraint(f"grade_max:{gd}:{day}:{s}", "GradeMax",
                                      f"등급{gd} day{day+1} {s} 최대 {mx}", {"grade": gd, "day": day, "shift": s}, "<=", int(mx))
                if f"grade:{gd}" in g.nodes:
                    g.add_edge("constrains", cid, f"grade:{gd}")
    for s, by_g in (inp.grade_min or {}).items():
        for gd, mn in (by_g or {}).items():
            if s not in inp.work_shifts or int(mn or 0) <= 0:
                continue
            for day in inp.requirements_by_day:
                cid = _add_constraint(f"grade_min:{gd}:{day}:{s}", "GradeMin",
                                      f"등급{gd} day{day+1} {s} 최소 {mn}", {"grade": gd, "day": day, "shift": s}, ">=", int(mn))
                if f"grade:{gd}" in g.nodes:
                    g.add_edge("constrains", cid, f"grade:{gd}")

    # pressures: shortage state → 같은 (day,shift) 의 모든 하드 제약
    for c in supply_demand.bottleneck_cells:
        cell = state_by_cell.get((c.day_index, c.shift_code))
        if not cell:
            continue
        for fam_nodes in constraints_by_family.values():
            for cid in fam_nodes:
                sc = g.nodes[cid].attrs.get("scope", {})
                if sc.get("day") == c.day_index and sc.get("shift") == c.shift_code:
                    g.add_edge("pressures", cell, cid)

    # 월별 shortage state (per-day flow 가 못 본 월 총량·야간cap) → 해당 shift 의 모든
    # CoverageMin 에 pressures + requires(=수요정의자→primary). recommender 가 월별 원인도
    # 일수요 감축으로 랭킹한다. shortage 는 per-day 등가로 이미 환산돼 있다.
    if monthly_supply_demand is not None:
        for msn in to_monthly_state_nodes(monthly_supply_demand, inp.num_days):
            g.add_node(msn)
            s = msn.attrs["shift_code"]
            for day, req in inp.requirements_by_day.items():
                if s in (req or {}) and int(req.get(s, 0) or 0) > 0:
                    cid = f"cov_min:{day}:{s}"
                    if cid in g.nodes:
                        g.add_edge("pressures", msn.node_id, cid)
                        g.add_edge("requires", cid, msn.node_id)
                    # 월별 capacity 가 실제 병목(shortage>0)일 때만, 그 shift 의 일별
                    # 부족 state 를 '월별 근본'에서 파생으로 잇는다(derived_from). blame 이
                    # 일별 증상 → 월별 root 로 흘러 근본원인이 상위로 뜬다(다중홉의 척추).
                    daily_cell = state_by_cell.get((day, s))
                    if daily_cell and int(msn.evidence.get("shortage", 0) or 0) > 0:
                        g.add_edge("derived_from", daily_cell, msn.node_id)

    # ── 4. soft constraint nodes (US-002) ───────────────────────────────────
    for sid, sc in ont.soft_constraints.items():
        g.add_node(ConstraintNode(
            node_id=f"soft:{sid}", label=sc.label, family=sid, severity="soft",
            operator="minimize", target=None,
            attrs={"lex_stage": sc.lex_stage, "weight_constant": sc.weight_constant,
                   "config_key": sc.config_key},
        ))

    # ── 5. action nodes (ontology treatments) + mitigates ───────────────────
    present_families = set(constraints_by_family.keys())
    for tid, t in ont.treatments.items():
        if t.target_family not in present_families:
            continue
        action_type = t.action_type if t.action_type in ACTION_INVASIVENESS else "set_threshold"
        prio, _ = _family_meta(t.target_family)
        g.add_node(ActionNode(
            node_id=f"action:{tid}", label=t.label, action_type=action_type,
            target_family=t.target_family, config_key=t.config_key, direction=t.direction,
            cost=ACTION_INVASIVENESS[action_type] + prio,
            reversible=(action_type != "data_correction_required"),
            auto_applicable=(action_type in {"force_soft_mode", "set_threshold"}),
            attrs={"relaxation_priority": prio},
        ))
        # mitigates: action → 해당 family 의 모든 제약 노드
        for cid in constraints_by_family.get(t.target_family, []):
            g.add_edge("mitigates", f"action:{tid}", cid)

    return g
