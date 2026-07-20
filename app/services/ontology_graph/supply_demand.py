"""Supply/Demand max-flow state computer — the deterministic grounding floor.

연구목표의 "max-flow 수요·공급 점검" 을 구현한다. MUS(solver conflict) 가
assumption-wrap 누락으로 침묵해도(test_conflict_core_real_solver.py 로 입증),
이 모듈은 solve 이전에 결정론적으로 day×shift 단위 구조적 병목을 산출한다.

핵심: 하루에 간호사 1명은 1개 시프트만 가능하므로 같은 날 여러 시프트가 한정된
가용 인력을 두고 경쟁한다. 단순 "가용>=수요" 산술로는 이 교차 경쟁을 못 본다.
그래서 하루를 bipartite max-flow 로 푼다:

    source ──(cap 1)──▶ nurse ──(cap 1, eligible)──▶ shift ──(cap required)──▶ sink

max-flow = 그날 채울 수 있는 필수 슬롯 수. shift→sink 간선의 flow = 그 시프트가
실제로 채워진 수. shortage = required - filled (교차 경쟁 반영된 진짜 부족).

의존성 없는 순수 파이썬 Edmonds-Karp (그래프가 작음: 간호사 ~30, 시프트 ~5).
ortools max_flow 로의 업그레이드는 OR_EVALUATION Tier-1 참고.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterable

from services.ontology_graph.schema import StateNode

WORK_SHIFTS_DEFAULT = ("D", "E", "N", "M")


# ── max-flow core (Edmonds-Karp) ────────────────────────────────────────────
class _MaxFlow:
    """정수 용량 방향 그래프의 max-flow. 노드는 int 인덱스."""

    def __init__(self, n: int) -> None:
        self.n = n
        self.cap: list[dict[int, int]] = [dict() for _ in range(n)]

    def add_edge(self, u: int, v: int, c: int) -> None:
        if c <= 0:
            return
        self.cap[u][v] = self.cap[u].get(v, 0) + c
        self.cap[v].setdefault(u, 0)  # residual

    def _bfs(self, s: int, t: int, parent: list[int]) -> bool:
        for i in range(self.n):
            parent[i] = -1
        parent[s] = s
        q = deque([s])
        while q:
            u = q.popleft()
            for v, c in self.cap[u].items():
                if parent[v] == -1 and c > 0:
                    parent[v] = u
                    if v == t:
                        return True
                    q.append(v)
        return False

    def max_flow(self, s: int, t: int) -> int:
        parent = [-1] * self.n
        flow = 0
        while self._bfs(s, t, parent):
            # bottleneck along augmenting path
            v = t
            bott = float("inf")
            while v != s:
                u = parent[v]
                bott = min(bott, self.cap[u][v])
                v = u
            v = t
            while v != s:
                u = parent[v]
                self.cap[u][v] -= bott
                self.cap[v][u] = self.cap[v].get(u, 0) + bott
                v = u
            flow += int(bott)
        return flow

    def residual_cap(self, u: int, v: int) -> int:
        return self.cap[u].get(v, 0)


# ── input model ─────────────────────────────────────────────────────────────
@dataclass
class NurseSupply:
    """한 간호사의 시점별 가용성 (read-only 입력)."""
    nurse_id: str
    grade: int | None = None
    team_id: str | None = None
    # day_index -> 그 날 배정 가능한 work shift code 집합
    eligible_by_day: dict[int, set[str]] = field(default_factory=dict)

    def eligible(self, day: int, shift: str) -> bool:
        return shift in self.eligible_by_day.get(day, set())


@dataclass
class SupplyDemandCell:
    day_index: int
    shift_code: str
    required: int
    available_eligible: int   # 단순 가용(교차경쟁 무시)
    filled: int               # max-flow 가 실제 채운 수(교차경쟁 반영)
    shortage: int             # required - filled

    @property
    def is_bottleneck(self) -> bool:
        return self.shortage > 0


@dataclass
class SupplyDemandResult:
    cells: list[SupplyDemandCell]
    day_shortage: dict[int, int]              # day -> 미충족 필수 슬롯 합
    monthly_shortage: dict[str, int]          # shift -> 월 합 부족(일별 shortage 합)
    bottleneck_cells: list[SupplyDemandCell] = field(default_factory=list)

    def total_shortage(self) -> int:
        return sum(c.shortage for c in self.cells)


# ── core computation ─────────────────────────────────────────────────────────
def compute_supply_demand(
    nurses: list[NurseSupply],
    requirements_by_day: dict[int, dict[str, int]],
    *,
    work_shifts: Iterable[str] = WORK_SHIFTS_DEFAULT,
) -> SupplyDemandResult:
    """day×shift 별 수요·공급·부족을 max-flow 로 산출.

    Args:
        nurses: 간호사별 day→eligible shift 집합 (fixed-off/비활성일은 eligible 에서 제외해 입력).
        requirements_by_day: day_index -> {shift_code: required_count}.
        work_shifts: O 제외 work 시프트 코드.

    Returns:
        SupplyDemandResult — 셀별 shortage + 일/월 집계 + 병목 셀.
    """
    work = tuple(s for s in work_shifts)
    cells: list[SupplyDemandCell] = []
    day_shortage: dict[int, int] = {}
    monthly_shortage: dict[str, int] = {s: 0 for s in work}

    all_days = sorted(requirements_by_day.keys())
    for day in all_days:
        req_map = {
            s: int(v or 0)
            for s, v in (requirements_by_day.get(day) or {}).items()
            if s in work and int(v or 0) > 0
        }
        if not req_map:
            day_shortage[day] = 0
            continue

        shifts = list(req_map.keys())
        # 가용 간호사 = 그날 어떤 work shift 든 1개 이상 가능한 사람
        avail_nurses = [
            nu for nu in nurses
            if any(nu.eligible(day, s) for s in shifts)
        ]

        # 노드 인덱싱: 0=source, 1..N=nurse, N+1..N+S=shift, last=sink
        N = len(avail_nurses)
        S = len(shifts)
        src = 0
        nurse_node = {i: 1 + i for i in range(N)}
        shift_node = {s: 1 + N + j for j, s in enumerate(shifts)}
        sink = 1 + N + S
        mf = _MaxFlow(sink + 1)

        for i in range(N):
            mf.add_edge(src, nurse_node[i], 1)  # 하루 1시프트
        for i, nu in enumerate(avail_nurses):
            for s in shifts:
                if nu.eligible(day, s):
                    mf.add_edge(nurse_node[i], shift_node[s], 1)
        for s in shifts:
            mf.add_edge(shift_node[s], sink, req_map[s])

        mf.max_flow(src, sink)

        d_short = 0
        for s in shifts:
            required = req_map[s]
            # filled = required - (잔여 shift→sink 용량)
            residual = mf.residual_cap(shift_node[s], sink)
            filled = required - residual
            shortage = max(0, required - filled)
            available_eligible = sum(1 for nu in avail_nurses if nu.eligible(day, s))
            cell = SupplyDemandCell(
                day_index=day,
                shift_code=s,
                required=required,
                available_eligible=available_eligible,
                filled=filled,
                shortage=shortage,
            )
            cells.append(cell)
            d_short += shortage
            monthly_shortage[s] = monthly_shortage.get(s, 0) + shortage
        day_shortage[day] = d_short

    bottleneck = [c for c in cells if c.is_bottleneck]
    return SupplyDemandResult(
        cells=cells,
        day_shortage=day_shortage,
        monthly_shortage=monthly_shortage,
        bottleneck_cells=bottleneck,
    )


def to_monthly_state_nodes(result: "MonthlySupplyDemandResult", num_days: int) -> list[StateNode]:
    """월별 shortage → StateNode. per-day flow 가 못 본 월 총량·야간cap 부족을 그래프에
    싣는다. evidence.shortage 는 per-day 등가(ceil(월부족/일수))로 환산 — recommender 의
    일수요 감축량이 합리적이게 한다.
    """
    days = max(1, int(num_days))
    nodes: list[StateNode] = []
    for s in result.shifts:
        if s.shortage <= 0:
            continue
        per_day = -(-s.shortage // days)  # ceil
        nodes.append(
            StateNode(
                node_id=f"state:monthly_supply_demand:{s.shift_code}",
                label=f"{s.shift_code} 월 공급/수요 (월부족 {s.shortage})",
                state_type="monthly_supply_demand",
                severity="hard",
                attrs={"shift_code": s.shift_code, "scope": "monthly"},
                evidence={
                    "shortage": per_day,               # per-day 등가(감축량 유도용)
                    "monthly_shortage": s.shortage,
                    "required": s.required,
                    "filled": s.filled,
                    "eligible_nurses": s.eligible_nurses,
                },
            )
        )
    return nodes


# ── monthly (aggregate) supply/demand ────────────────────────────────────────
# per-day flow 는 하루 교차경쟁만 본다. 월 총량(간호사당 근무가능일)·월 야간cap(max_nig)·
# per-shift eligibility 는 못 본다(35명이면 하루 N=14 는 채우지만 월 근무일 총량이 모자랄 수
# 있음). 이 월별 flow 가 그 층을 담당한다. 두 결과는 상보 — 함께 써야 완전하다.

@dataclass
class MonthlyShiftResult:
    shift_code: str
    required: int          # 월 총 수요 (일수요 × 일수)
    eligible_nurses: int   # 그 시프트 가능 인원
    filled: int            # max-flow 가 채운 월 슬롯 수
    shortage: int          # required - filled

    @property
    def is_bottleneck(self) -> bool:
        return self.shortage > 0


@dataclass
class MonthlySupplyDemandResult:
    shifts: list[MonthlyShiftResult]
    total_required: int
    total_filled: int

    def total_shortage(self) -> int:
        return sum(s.shortage for s in self.shifts)

    @property
    def bottleneck_shifts(self) -> list[MonthlyShiftResult]:
        return [s for s in self.shifts if s.is_bottleneck]


def compute_monthly_supply_demand(
    nurses: list[NurseSupply],
    monthly_demand: dict[str, int],
    *,
    workdays_by_nurse: dict[str, int],
    night_cap_by_nurse: dict[str, int] | None = None,
    night_shift: str = "N",
    work_shifts: Iterable[str] = WORK_SHIFTS_DEFAULT,
) -> MonthlySupplyDemandResult:
    """월 단위 bipartite max-flow 로 시프트별 월 충족/부족을 산출.

    source ─(cap=근무가능일)─▶ nurse ─(cap: N은 max_nig, 그외 근무가능일; eligible만)─▶
        shift ─(cap=월 총수요)─▶ sink

    Args:
        monthly_demand: {shift: 월 총 수요}. 보통 일수요 × 일수.
        workdays_by_nurse: {nurse_id: 월 근무가능일}(= 일수 − 필요off). 월 총 capacity.
        night_cap_by_nurse: {nurse_id: 월 야간 상한}(min(max_nig, n_max)). 없으면 근무가능일.
        night_shift: 야간 상한을 적용할 시프트 코드(기본 "N").

    Returns:
        MonthlySupplyDemandResult — 시프트별 required/filled/shortage + 총계.
    """
    work = tuple(s for s in work_shifts)
    shifts = [s for s in work if int(monthly_demand.get(s, 0) or 0) > 0]
    night_cap_by_nurse = night_cap_by_nurse or {}

    # 월별 eligibility: nurse 의 eligible_by_day 합집합(비면 전 work_shifts 가용).
    def _monthly_elig(nu: NurseSupply) -> set[str]:
        if not nu.eligible_by_day:
            return set(work)
        u: set[str] = set()
        for ds in nu.eligible_by_day.values():
            u |= set(ds)
        return u or set(work)

    N, S = len(nurses), len(shifts)
    if N == 0 or S == 0:
        req = {s: int(monthly_demand.get(s, 0) or 0) for s in shifts}
        return MonthlySupplyDemandResult(
            shifts=[MonthlyShiftResult(s, req[s], 0, 0, req[s]) for s in shifts],
            total_required=sum(req.values()), total_filled=0)

    src = 0
    nurse_node = {i: 1 + i for i in range(N)}
    shift_node = {s: 1 + N + j for j, s in enumerate(shifts)}
    sink = 1 + N + S
    mf = _MaxFlow(sink + 1)

    eligible_count = {s: 0 for s in shifts}
    for i, nu in enumerate(nurses):
        wd = int(workdays_by_nurse.get(nu.nurse_id, 0) or 0)
        if wd <= 0:
            continue
        mf.add_edge(src, nurse_node[i], wd)
        elig = _monthly_elig(nu)
        for s in shifts:
            if s not in elig:
                continue
            eligible_count[s] += 1
            if s == night_shift:
                cap = min(wd, int(night_cap_by_nurse.get(nu.nurse_id, wd)))
            else:
                cap = wd
            mf.add_edge(nurse_node[i], shift_node[s], cap)
    for s in shifts:
        mf.add_edge(shift_node[s], sink, int(monthly_demand[s]))

    mf.max_flow(src, sink)

    results: list[MonthlyShiftResult] = []
    total_filled = 0
    for s in shifts:
        required = int(monthly_demand[s])
        residual = mf.residual_cap(shift_node[s], sink)
        filled = required - residual
        total_filled += filled
        results.append(MonthlyShiftResult(
            shift_code=s, required=required, eligible_nurses=eligible_count[s],
            filled=filled, shortage=max(0, required - filled)))
    return MonthlySupplyDemandResult(
        shifts=results, total_required=sum(int(monthly_demand[s]) for s in shifts),
        total_filled=total_filled)


# ── StateNode 변환 ────────────────────────────────────────────────────────────
def to_state_nodes(result: SupplyDemandResult) -> list[StateNode]:
    """SupplyDemandResult → 통합 그래프 StateNode 리스트.

    부족(shortage>0) 셀은 severity=hard 로 표기해 pressures 엣지의 tail 이 된다.
    """
    nodes: list[StateNode] = []
    for c in result.cells:
        nodes.append(
            StateNode(
                node_id=f"state:supply_demand:d{c.day_index}:{c.shift_code}",
                label=f"day {c.day_index + 1} {c.shift_code} 수요/가용",
                state_type="supply_demand",
                severity="hard" if c.is_bottleneck else "none",
                attrs={
                    "day_index": c.day_index,
                    "shift_code": c.shift_code,
                },
                evidence={
                    "required": c.required,
                    "available_eligible": c.available_eligible,
                    "filled": c.filled,
                    "shortage": c.shortage,
                },
            )
        )
    return nodes
