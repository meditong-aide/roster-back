"""Factor hypergraph + recursive conditioning — 진짜 separator 분해 (AND/OR + component).

frontier DP 는 시간축(day) separator 만 쓴다. 여기선 **factor 하이퍼그래프**를 실제로 구성하고,
조건화(conditioning)로 그래프가 **독립 component 로 분리**되면 각각을 개별 exact solve 후 **AND**
결합한다(= recursive conditioning / AND-OR search, Dechter·Darwiche). sparse·다-component 결합에서
monolithic DP 보다 이득.

**factor 완전성이 생명**이라 3층 audit 로 "모든 factor 가 들어갔는지" 검사한다:
  ① 구조: 모든 x 변수·per-nurse 시퀀스·per-day 커버리지·banned/forced 가 factor 로 존재하는가
  ② 규칙 인코딩: 활성 규칙마다 위반 배열을 만들어 해당 factor 가 **실제로 거부**하는가
  ③ 의미 재구성: factor graph solver 판정 == 독립 oracle (누락 factor 면 반드시 불일치)

변수: x[i,d] ∈ 정적 허용 shift. factor:
  · Nf(i): per-nurse 시퀀스 전체(회복=실제 OFF·max run·not_one_night·전이·max연속근무).
           frontier_dp._options/_step 를 그대로 시뮬레이션 → 검증된 엔진과 동일 semantics.
  · Cf(d): 그날 D/E/N 커버리지.
  (banned/forced 는 x 도메인 정적 제약 + Nf 시뮬레이션에 반영.)

context caching(AND/OR): component feasibility 는 경계(boundary) 배정에만 의존 → (cvars, ctx)
로 memoize. correctness 는 재구성 교차검증으로 확인(불일치 0). **한계(정직)**: Nf 를 **날짜 전체
1개 factor**로 뒀기 때문에 시간축 분할의 boundary = 전체 prefix(작은 state 로 추상화 안 됨) →
**dense 시간격자에선 캐시가 잘 안 먹혀 UNKNOWN**. 그런 dense 는 frontier DP(상태 추상화 O)가
담당. 캐시 이득은 sparse·반복 subproblem 에서. 진짜 dense 효율은 (a) 상태변수 transition factor
분해 또는 (b) hybrid(component 분리→각 component 를 frontier DP sweep) 필요 — 미구현.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from services.ontology_graph.certificate import Certificate
from services.ontology_graph.frontier_dp import _options, _prep, _req, _step
from services.ontology_graph.lagrangian import _night_rules


class _Budget(Exception):
    pass


@dataclass
class Factor:
    scope: tuple                      # 변수 튜플
    pred: Callable                    # assign(dict) -> bool (scope 전체 배정됐다고 가정)
    kind: str = ""
    meta: dict = field(default_factory=dict)
    scope_set: frozenset = field(default_factory=frozenset)

    def __post_init__(self):
        if not self.scope_set:
            self.scope_set = frozenset(self.scope)

    def fully_assigned(self, assign: dict) -> bool:
        return self.scope_set <= assign.keys()


@dataclass
class FactorGraph:
    variables: dict                   # var -> domain(tuple)
    factors: list


def build_factor_graph(nurses: list, config: dict, num_days: int) -> FactorGraph:
    prep = _prep(nurses, config)
    k = len(prep)
    max_run, rec_trig, min_run = _night_rules(config)
    track_w = config.get("max_consecutive_work") is not None
    track_prev = bool(config.get("forbid_night_to_day"))
    reqD, reqE, reqN = _req(config, "D"), _req(config, "E"), _req(config, "N")

    # 변수 도메인 (정적 per-cell 허용 shift; 시퀀스는 Nf 가 봄)
    variables: dict = {}
    for i, n in enumerate(prep):
        for d in range(num_days):
            banned = n["banned"].get(d, set())
            if d in n["foff"]:
                variables["x", i, d] = () if "O" in banned else ("O",)
                continue
            dom = [s for s in ("D", "E", "N") if s in n["work"] and s not in banned]
            if "O" not in banned:
                dom.append("O")
            variables["x", i, d] = tuple(dom)

    factors: list = []

    # per-nurse 시퀀스 factor (검증된 _options/_step 시뮬레이션)
    def _make_seq(i):
        def pred(assign):
            state = (0, 0, 0, "")
            for d in range(num_days):
                x = assign["x", i, d]
                if x not in _options(prep[i], state, d, config, max_run, min_run):
                    return False
                state = _step(state, x, config, track_w, track_prev)
            return True
        return pred

    for i in range(k):
        scope = tuple(("x", i, d) for d in range(num_days))
        factors.append(Factor(scope, _make_seq(i), kind="sequence", meta={"nurse": i}))

    # per-day 커버리지 factor — 강제OFF(도메인={O}) 간호사는 항상 0 기여 → scope 제외(분리 유도)
    def _make_cov(d, members):
        def pred(assign):
            cD = sum(assign["x", i, d] == "D" for i in members)
            cE = sum(assign["x", i, d] == "E" for i in members)
            cN = sum(assign["x", i, d] == "N" for i in members)
            return cD >= reqD and cE >= reqE and cN >= reqN
        return pred

    for d in range(num_days):
        if reqD or reqE or reqN:
            members = [i for i in range(k) if variables["x", i, d] != ("O",)]
            scope = tuple(("x", i, d) for i in members)
            factors.append(Factor(scope, _make_cov(d, members), kind="coverage",
                                  meta={"day": d, "req": (reqD, reqE, reqN), "members": members}))

    return FactorGraph(variables, factors)


# ── recursive conditioning solver (component 분해 + 조건화 + budget) ──────────────
def _components(unassigned: set, factors: list) -> list:
    """미배정 변수 그래프(같은 factor 공유=엣지)의 연결 component + 각 factor 귀속."""
    parent = {v: v for v in unassigned}

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        parent[find(a)] = find(b)

    for f in factors:
        us = [v for v in f.scope_set if v in unassigned]
        for j in range(1, len(us)):
            union(us[0], us[j])
    groups: dict = {}
    for v in unassigned:
        groups.setdefault(find(v), set()).add(v)
    comp_of = {v: find(v) for v in unassigned}
    fac_by: dict = {r: [] for r in groups}
    for f in factors:
        us = [v for v in f.scope_set if v in unassigned]
        if us:
            fac_by[comp_of[us[0]]].append(f)      # 미배정 변수는 한 component 에만
    return [(groups[r], fac_by[r]) for r in groups]


def _pick(unassigned: set, factors: list, variables: dict):
    """min-degree(작은 separator) + 도메인/시간축 타이브레이크(temporal sweep 유도로 캐시 적중↑)."""
    deg = {v: 0 for v in unassigned}
    for f in factors:
        us = [v for v in f.scope_set if v in unassigned]
        for v in us:
            deg[v] += len(us) - 1
    # 타이브레이크: 도메인 작은 것 → 이른 날(day) → 낮은 nurse. day-major 순회는 경계상태 재사용↑
    return min(unassigned, key=lambda v: (deg[v], len(variables[v]), v[2], v[1]))


def _boundary_ctx(cvars: set, rel: list, assign: dict) -> tuple:
    """component 의 **context** = rel factor scope 중 이미 배정된 경계변수 (v,val) 집합.

    component 의 feasibility 는 이 context 에만 의존 → (cvars, ctx) 로 memoize 가능(AND/OR 캐싱).
    """
    seen = {}
    for f in rel:
        for v in f.scope_set:
            if v not in cvars and v in assign:
                seen[v] = assign[v]
    return tuple(sorted(seen.items()))


def _solve(cvars: set, factors: list, assign: dict, variables: dict,
           budget: list, cache: dict) -> bool:
    budget[0] -= 1
    if budget[0] < 0:
        raise _Budget()
    # cvars 를 실제로 건드리는 factor 만(캐시 키가 cvars 로 결정론적이도록)
    rel = [f for f in factors if f.scope_set & cvars]
    if not cvars:
        return all(f.pred(assign) for f in factors if f.fully_assigned(assign))
    key = (frozenset(cvars), _boundary_ctx(cvars, rel, assign))
    cached = cache.get(key)
    if cached is not None:
        return cached
    comps = _components(cvars, rel)
    if len(comps) > 1:
        res = True
        for cv, cf in comps:                       # 독립 component → AND
            if not _solve(cv, cf, assign, variables, budget, cache):
                res = False
                break
        cache[key] = res
        return res
    v = _pick(cvars, rel, variables)
    rest = cvars - {v}
    res = False
    for val in variables[v]:
        assign[v] = val
        ok = True
        for f in rel:                              # v 로 완전배정된 factor 즉시 검사(가지치기)
            if v in f.scope_set and f.fully_assigned(assign) and not f.pred(assign):
                ok = False
                break
        if ok and _solve(rest, rel, assign, variables, budget, cache):
            res = True
        del assign[v]
        if res:
            break
    cache[key] = res
    return res


@dataclass
class ConditioningResult:
    status: str
    certificate: Certificate | None = None
    components_seen: int = 0


def diagnose_conditioning(nurses: list, config: dict, num_days: int,
                          budget: int = 4_000_000) -> ConditioningResult:
    """factor graph + recursive conditioning 판정. budget 초과=UNKNOWN."""
    fg = build_factor_graph(nurses, config, num_days)
    if any(len(dom) == 0 for dom in fg.variables.values()):
        return ConditioningResult("INFEASIBLE_CERTIFIED", certificate=Certificate(
            kind="empty_domain", group_id="cell", capacity=0, demand=1, deficit=1,
            antecedents=["강제근무(OFF금지)와 강제OFF 가 같은 칸에서 충돌"], witness={}))
    # singleton(강제) 변수 선배정 → 미배정에서 제외(component 분리 노출)
    assign = {v: dom[0] for v, dom in fg.variables.items() if len(dom) == 1}
    unassigned = {v for v, dom in fg.variables.items() if len(dom) > 1}
    for f in fg.factors:                          # 선배정만으로 이미 위반된 factor 즉시 감지
        if f.fully_assigned(assign) and not f.pred(assign):
            return ConditioningResult("INFEASIBLE_CERTIFIED", components_seen=0,
                                      certificate=Certificate(
                                          kind="conditioning_infeasible", group_id=f.kind,
                                          capacity=0, demand=1, deficit=1,
                                          antecedents=[f"강제배정만으로 {f.kind} factor 위반"],
                                          witness=dict(f.meta)))
    top_comps = len(_components(unassigned, fg.factors))
    bud = [budget]
    cache: dict = {}
    try:
        feasible = _solve(unassigned, fg.factors, assign, fg.variables, bud, cache)
    except _Budget:
        return ConditioningResult("UNKNOWN", components_seen=top_comps)
    if feasible:
        return ConditioningResult("FEASIBLE_WITNESS", components_seen=top_comps)
    return ConditioningResult("INFEASIBLE_CERTIFIED", components_seen=top_comps,
                              certificate=Certificate(
                                  kind="conditioning_infeasible", group_id="global",
                                  capacity=0, demand=1, deficit=1,
                                  antecedents=[f"factor graph 의 어떤 배정으로도 충족 불가"
                                               f"(최상위 {top_comps} component)"],
                                  witness={"components": top_comps}))
