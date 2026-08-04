"""Hybrid inference — separator conditioning(희소) + component frontier DP(조밀 시간축).

피드백 핵심: 큰 결합을 separator conditioning 으로 희소화하고, 분리된 각 component 내부의
조밀한 시간·시퀀스 결합은 frontier DP 로 압축해 exact 하게 푼다.

여기(1차): **top-level component 분해 → 각 component 를 frontier DP(temporal sweep)로 판정 →
AND**. component 는 factor 하이퍼그래프의 실제 연결관계로 나뉘고, 각 component 는 담당 날짜의
per-day 커버리지만 부과해 frontier_message 로 sweep. dense(1 component)면 frontier DP 로 귀결
(효율), sparse(다 component)면 각각 작게 푼다.

미구현(재귀): component 가 여전히 wide(frontier 폭발) 면 그 안에서 separator 변수 conditioning
→ 재분해 → 다시 frontier DP. 현재는 wide component = UNKNOWN. (BoundaryState 계약·message 는
step0 에서 준비됨 → 재귀는 그 위에 얹으면 됨.)
"""

from __future__ import annotations

from dataclasses import dataclass

from services.ontology_graph.certificate import Certificate
from services.ontology_graph.frontier_dp import (
    _collapse_cert,
    _prep,
    fresh_joint,
    frontier_message,
)
from services.ontology_graph.hypergraph_conditioning import (
    _components,
    build_factor_graph,
)
from services.ontology_graph.lagrangian import _night_rules


@dataclass
class HybridResult:
    status: str
    certificate: Certificate | None = None
    components: int = 0
    component_sizes: tuple = ()


def _solve_component(cvars: set, cfacs: list, prep: list, config: dict, num_days: int,
                     budget: list, cap: int, singletons: list) -> tuple:
    """component(cvars) → frontier DP 판정. (status, certificate).

    singletons: [(i,d,shift)] 강제 D/E/N 배정. **component 밖(i∉nc)** 강제기여만 담당 날 req 에서
    차감(안 나눔). component 안 강제 간호사는 sub frontier DP 가 직접 처리하므로 차감 금지(이중차감
    방지 — 과거 falseFEAS 버그).
    """
    nc_set = {v[1] for v in cvars}
    nc = sorted(nc_set)                             # 이 component 의 간호사(전역 idx)
    sub = [prep[i] for i in nc]
    forced_out: dict = {}                           # component 밖 강제기여만
    for i, d, sh in singletons:
        if i not in nc_set:
            fD, fE, fN = forced_out.get(d, (0, 0, 0))
            forced_out[d] = (fD + (sh == "D"), fE + (sh == "E"), fN + (sh == "N"))
    # 담당 커버리지: 이 component 의 coverage factor 가 있는 날만, 밖 강제기여분 차감 후 부과
    day_reqs = {d: (0, 0, 0) for d in range(num_days)}
    for f in cfacs:
        if f.kind == "coverage":
            d = f.meta["day"]
            rD, rE, rN = f.meta["req"]
            fD, fE, fN = forced_out.get(d, (0, 0, 0))
            day_reqs[d] = (max(0, rD - fD), max(0, rE - fE), max(0, rN - fN))
    mr = frontier_message(sub, config, 0, num_days, {fresh_joint(len(sub))},
                          symmetry=None, cap=cap, budget=budget, day_reqs=day_reqs)
    if mr.overflow:
        return "UNKNOWN", None
    if mr.collapse is not None:
        max_run, _, min_run = _night_rules(config)
        rej = mr.collapse
        # sub-index 기준 cert → 전역 간호사 id 로 antecedent 보강
        cert = _collapse_cert(rej.frontier, rej.day, config, sub,
                              (0, 0, 0) if not rej.reqs else rej.reqs, max_run, min_run)
        cert.witness["component_nurses"] = [prep[i]["nid"] for i in nc]
        cert.witness["rejection"] = {"day": rej.day, "best_cov": rej.best_cov,
                                     "binding": list(rej.binding),
                                     "dead_states": rej.dead_states}
        cert.antecedents = [f"component(간호사 {[prep[i]['nid'] for i in nc]}) " + a
                            for a in cert.antecedents]
        return "INFEASIBLE_CERTIFIED", cert
    return "FEASIBLE_WITNESS", None


def solve_hybrid(nurses: list, config: dict, num_days: int,
                 budget: int = 6_000_000, cap: int = 200_000) -> HybridResult:
    """component 분해 → 각 component frontier DP → AND."""
    prep = _prep(nurses, config)
    fg = build_factor_graph(nurses, config, num_days)
    if any(len(dom) == 0 for dom in fg.variables.values()):
        return HybridResult("INFEASIBLE_CERTIFIED", components=0, certificate=Certificate(
            kind="empty_domain", group_id="cell", capacity=0, demand=1, deficit=1,
            antecedents=["강제근무(OFF금지)와 강제OFF 가 같은 칸에서 충돌"], witness={}))
    unassigned = {v for v, dom in fg.variables.items() if len(dom) > 1}
    # 강제(singleton) D/E/N 배정 목록 [(i,d,shift)] + 일별 합(fully-forced 날 검사용)
    singletons: list = []
    forced: dict = {}
    for v, dom in fg.variables.items():
        if len(dom) == 1 and dom[0] in ("D", "E", "N"):
            _, i, d = v
            singletons.append((i, d, dom[0]))
            fD, fE, fN = forced.get(d, (0, 0, 0))
            forced[d] = (fD + (dom[0] == "D"), fE + (dom[0] == "E"), fN + (dom[0] == "N"))
    # 전원 강제(unassigned 없음)인 커버리지 날은 어떤 component 도 안 가짐 → 직접 검사
    for f in fg.factors:
        if f.kind == "coverage" and not (f.scope_set & unassigned):
            d = f.meta["day"]
            rD, rE, rN = f.meta["req"]
            fD, fE, fN = forced.get(d, (0, 0, 0))
            if fD < rD or fE < rE or fN < rN:
                return HybridResult("INFEASIBLE_CERTIFIED", components=0, certificate=Certificate(
                    kind="forced_coverage_deficit", group_id=f"day:{d + 1}",
                    capacity=fD + fE + fN, demand=rD + rE + rN, deficit=1,
                    antecedents=[f"{d + 1}일: 전원 강제배정만으로 커버리지 미달"
                                 f"(D{fD}/{rD} E{fE}/{rE} N{fN}/{rN})"], witness={"day": d}))
    comps = _components(unassigned, fg.factors)
    sizes = tuple(sorted(len({v[1] for v in cv}) for cv, _ in comps))
    bud = [budget]
    unknown = False
    for cvars, cfacs in comps:                      # 독립 component → AND
        st, cert = _solve_component(cvars, cfacs, prep, config, num_days, bud, cap, singletons)
        if st == "INFEASIBLE_CERTIFIED":
            return HybridResult(st, certificate=cert, components=len(comps),
                                component_sizes=sizes)
        if st == "UNKNOWN":
            unknown = True                          # 이 component 판정 실패(=wide)
    if unknown:
        return HybridResult("UNKNOWN", components=len(comps), component_sizes=sizes)
    return HybridResult("FEASIBLE_WITNESS", components=len(comps), component_sizes=sizes)
