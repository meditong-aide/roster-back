"""Blame propagation scorer — "어느 노드가 가장 큰 문제인가" 결정론적 랭킹.

shortage(state.evidence['shortage']) 를 발원점으로, 온톨로지 엣지를 '증상→원인'
방향으로 재해석한 blame-DAG 위에서 **선형 누적**한다. 학습·라벨 없이 노드 자기
정보 + 다중홉 이웃 정보로 책임질량(blame)을 매긴다. GNN 의 이웃 aggregation 을
결정론 전파로 구현한 것.

- 정확성: blame-DAG 를 역위상(Kahn) 1-pass 로 누적(closed-form). 사이클 감지 시
  K-bounded 반복 전파(감쇠 alpha<1)로 fallback.
- 설명가능성: 선형이라 각 노드 blame 을 **seed shortage 별 기여로 정확히 분해**
  (contrib). "이 노드 blame 의 60% 는 월N부족, 25% 는 …" 이 근거와 함께 나온다.
- detector 무관: seed 가 evidence['shortage'] 뿐이라 max-flow/structural/MUS/conflict
  어느 detector 가 만든 state 든 하나의 스코어러로 통합 랭크.

한계: 선형 = 가산·독립 가정. coupled(초가산) 충돌 — 각각은 만족 가능한데 함께는
불가 — 은 이 모델이 책임을 나눠줄 뿐 '상호작용이 범인'을 표현 못 한다. 그 부분은
min-cut/MUS/hitting-set 가 상위에서 담당하고, blame 은 그 probe 를 '유도'한다.

weight/alpha 는 손으로 박은 prior(순서만 정당, 크기는 추측)다. 재solve 정답 대비
회귀 튜닝으로 magnitude 를 보정하는 걸 전제로 상수로 노출한다.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

from services.ontology_graph.schema import OntologyGraph

# ── prior (회귀 튜닝 대상) ──────────────────────────────────────────────────
W_HARD = 10.0   # hard shortage seed 가중 (soft/none = 1.0)
ALPHA = 0.6     # 홉당 감쇠. 근접 원인 우대 + 깊은 체인 폭주 방지.
# relation 별 blame 전달 가중. 순서(ordinal)만 prior 로 고정, 크기는 튜닝.
W_REL = {
    "pressures": 1.0,       # state → constraint          부족이 규칙 압박
    "requires_inv": 1.5,    # state → constraint (역)      수요정의자 = 1차 원인
    "reduces_inv": 1.5,     # state → object (역)          공급 깎는 주체 = 근본원인
    "derived_from": 0.8,    # state → state(base)          증상이 상류 근본 지목(다중홉)
    "constrains": 0.5,      # constraint → object          걸린 팀/등급/day 연루
    "belongs_to_inv": 0.5,  # nurse → team/grade           개인 blame 그룹 롤업
}
_MAX_ITER = 16  # 사이클 fallback 반복 상한


@dataclass
class NodeScore:
    node_id: str
    kind: str
    label: str
    blame: float
    object_type: str | None = None    # domain_object 일 때
    family: str | None = None         # constraint 일 때
    # 이 노드 blame 의 seed 별 분해 상위: (seed_state_id, seed_label, amount, pct)
    reasons: list[tuple[str, str, float, float]] = field(default_factory=list)


@dataclass
class BlameResult:
    ranked: list[NodeScore]                       # 전체, blame desc
    scores: dict[str, float]                      # node_id → blame
    contrib: dict[str, dict[str, float]]          # node_id → {seed_id: amount}
    converged: bool                               # False = 사이클 fallback 사용
    top_constraints: list[NodeScore] = field(default_factory=list)
    top_objects: list[NodeScore] = field(default_factory=list)   # 누구(nurse/leave/wanted_off)
    top_groups: list[NodeScore] = field(default_factory=list)    # 팀/등급

    def top(self, k: int = 1) -> list[NodeScore]:
        return self.ranked[:k]


def _blame_edges(graph: OntologyGraph, rollup_groups: bool) -> list[tuple[str, str, float]]:
    """온톨로지 엣지 → blame 엣지 (symptom, cause, w). symptom→cause 방향."""
    out: list[tuple[str, str, float]] = []
    for e in graph.edges:
        r = e.relation
        if r == "pressures":
            out.append((e.source_id, e.target_id, W_REL["pressures"]))
        elif r == "requires":                 # constraint→state 를 역으로: state→constraint
            out.append((e.target_id, e.source_id, W_REL["requires_inv"]))
        elif r == "reduces":                  # object→state 를 역으로: state→object
            out.append((e.target_id, e.source_id, W_REL["reduces_inv"]))
        elif r == "derived_from":             # state→state(base). derived 가 base 를 지목.
            out.append((e.source_id, e.target_id, W_REL["derived_from"]))
        elif r == "constrains":               # constraint→object
            out.append((e.source_id, e.target_id, W_REL["constrains"]))
        elif r == "belongs_to" and rollup_groups:  # nurse→team/grade
            out.append((e.source_id, e.target_id, W_REL["belongs_to_inv"]))
    return out


def _seed_vectors(graph: OntologyGraph) -> dict[str, dict[str, float]]:
    """state.evidence['shortage'] 를 발원점으로 seed 분해 벡터 초기화."""
    vec: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for st in graph.nodes_of_kind("state"):
        s = float(st.evidence.get("shortage", 0) or 0)
        if s > 0:
            w = W_HARD if st.severity == "hard" else 1.0
            vec[st.node_id][st.node_id] = s * w
    return vec


def _accumulate_dag(nodes: list[str], edges: list[tuple[str, str, float]],
                    seed: dict[str, dict[str, float]], alpha: float
                    ) -> tuple[dict[str, dict[str, float]], bool]:
    """Kahn 역위상 1-pass. symptom 확정 후 cause 로 blame 벡터 전파. (vec, converged)."""
    causes_of: dict[str, list[tuple[str, float]]] = defaultdict(list)
    indeg: dict[str, int] = {n: 0 for n in nodes}
    for sym, cause, w in edges:
        causes_of[sym].append((cause, w))
        indeg[cause] += 1

    vec: dict[str, dict[str, float]] = {n: dict(seed.get(n, {})) for n in nodes}
    q = deque(n for n in nodes if indeg[n] == 0)
    processed = 0
    while q:
        sym = q.popleft()
        processed += 1
        src = vec[sym]
        for cause, w in causes_of.get(sym, []):
            if src:
                dst = vec[cause]
                factor = alpha * w
                for seed_id, amt in src.items():
                    dst[seed_id] = dst.get(seed_id, 0.0) + factor * amt
            indeg[cause] -= 1
            if indeg[cause] == 0:
                q.append(cause)
    return vec, processed == len(nodes)


def _accumulate_iter(nodes: list[str], edges: list[tuple[str, str, float]],
                     seed: dict[str, dict[str, float]], alpha: float
                     ) -> dict[str, dict[str, float]]:
    """사이클 fallback: 감쇠 반복 relaxation. alpha<1 로 수렴."""
    incoming: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for sym, cause, w in edges:
        incoming[cause].append((sym, w))
    vec = {n: dict(seed.get(n, {})) for n in nodes}
    for _ in range(_MAX_ITER):
        nxt = {n: dict(seed.get(n, {})) for n in nodes}
        for n in nodes:
            for sym, w in incoming.get(n, []):
                factor = alpha * w
                for seed_id, amt in vec[sym].items():
                    nxt[n][seed_id] = nxt[n].get(seed_id, 0.0) + factor * amt
        vec = nxt
    return vec


def score_blame(graph: OntologyGraph, *, alpha: float = ALPHA,
                rollup_groups: bool = True, top_k: int = 10) -> BlameResult:
    """그래프의 각 노드에 blame(책임질량) 점수를 매겨 랭크. 순수·결정론."""
    nodes = list(graph.nodes.keys())
    edges = _blame_edges(graph, rollup_groups)
    seed = _seed_vectors(graph)

    vec, converged = _accumulate_dag(nodes, edges, seed, alpha)
    if not converged:
        # 사이클(주로 derived_from 배선 실수) — 근사 fallback + 경고
        print("[blame] 사이클 감지 → 반복 전파 fallback (derived_from 무순환 배선 점검)")
        vec = _accumulate_iter(nodes, edges, seed, alpha)

    scores = {n: sum(vec[n].values()) for n in nodes}
    contrib = {n: dict(vec[n]) for n in nodes if vec[n]}

    def _mk(nid: str) -> NodeScore:
        node = graph.nodes[nid]
        total = scores[nid] or 1.0
        reasons = sorted(vec[nid].items(), key=lambda kv: kv[1], reverse=True)[:3]
        rz = [(sid, graph.nodes[sid].label if sid in graph.nodes else sid,
               round(amt, 3), round(100.0 * amt / total, 1)) for sid, amt in reasons]
        return NodeScore(
            node_id=nid, kind=node.kind, label=node.label, blame=round(scores[nid], 4),
            object_type=getattr(node, "object_type", None),
            family=getattr(node, "family", None), reasons=rz)

    ranked = [_mk(n) for n in sorted(nodes, key=lambda n: scores[n], reverse=True)
              if scores[n] > 0]

    top_constraints = [s for s in ranked if s.kind == "constraint"][:top_k]
    top_objects = [s for s in ranked if s.kind == "domain_object"
                   and s.object_type in ("nurse", "leave", "wanted_off")][:top_k]
    top_groups = [s for s in ranked if s.kind == "domain_object"
                  and s.object_type in ("team", "grade")][:top_k]

    return BlameResult(
        ranked=ranked, scores=scores, contrib=contrib, converged=converged,
        top_constraints=top_constraints, top_objects=top_objects, top_groups=top_groups)
