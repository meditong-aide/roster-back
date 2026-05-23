"""Pool-capacity ontology builder.

근무표 infeasibility 의 root cause 가 "어느 풀(팀×시프트, Grade×시프트, 공통풀)의
용량 부족인가" 인 경우가 많다. 이 모듈은 :mod:`team_grade_precheck` 의 `PrecheckInput`
구조에서 다음을 산출한다.

    - **TeamPoolNode**(team_id, shift) — 팀별 시프트 가능 인원 수와 월 capacity
    - **GradePoolNode**(grade, shift) — Grade 별 시프트 가능 인원 수
    - **CommonPoolNode**(shift) — 무소속(공통풀) 시프트 가능 인원
    - **PoolShortageNode** — capacity < daily_demand 또는 monthly_capacity < monthly_demand
    - edges: `REDUCES_POOL(nurse, pool)` — N전담 / OFF / fixed 등으로 풀을 깎는 간호사

Dashboard 는 이 그래프로 "이 N 전담 배정이 어느 풀을 깎고 있는지" 한 화면에 표시.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from services.precheck.team_grade_precheck import PrecheckInput, PrecheckNurse
from services.precheck.runtime_bridge import build_precheck_input


# ---------------------------------------------------------------------------
# Helpers (private)
# ---------------------------------------------------------------------------

def _shifts(inp: PrecheckInput) -> List[str]:
    use_mid = bool((inp.roster_config or {}).get("use_mid", False))
    return ["D", "E", "N", "M"] if use_mid else ["D", "E", "N"]


def _allowed_set(nurse: PrecheckNurse, S: List[str]) -> Set[str]:
    raw = nurse.allowed_shifts
    if raw is None or len(raw) == 0:
        return set(S)
    return {str(x) for x in raw if x in S}


def _is_active_day(nurse: PrecheckNurse, d: int) -> bool:
    if not (nurse.join_day <= d <= nurse.leave_day):
        return False
    if d in nurse.fixed_off_days:
        return False
    return True


def _required_off_days(nurse: PrecheckNurse, cfg: Dict[str, Any]) -> int:
    return (
        int(cfg.get("global_monthly_off_days", 0) or 0)
        + int(cfg.get("standard_personal_off_days", 0) or 0)
        + int(nurse.personal_off_adjustment or 0)
    )


def _working_capacity(nurse: PrecheckNurse, cfg: Dict[str, Any]) -> int:
    """이 간호사가 한 달간 work shift 에 박을 수 있는 일수 상한."""
    span = max(0, nurse.leave_day - nurse.join_day + 1)
    return max(0, span - _required_off_days(nurse, cfg))


def _daily_need(cfg: Dict[str, Any], shift: str, day: int) -> int:
    by_day = cfg.get("daily_shift_requirements_by_day")
    if isinstance(by_day, list) and 0 <= day < len(by_day) and by_day[day]:
        v = by_day[day].get(shift, 0)
    else:
        v = (cfg.get("daily_shift_requirements") or {}).get(shift, 0)
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class PoolStats:
    """1개 pool 의 capacity / demand 스냅샷."""

    pool_id: str  # 예: ``team_pool:team_2:D``
    pool_type: str  # ``TeamPoolNode`` / ``GradePoolNode`` / ``CommonPoolNode``
    scope_label: str  # 예: ``Team 2``, ``Grade 1``, ``공통풀``
    shift: str
    member_nurse_ids: List[str] = field(default_factory=list)
    allowed_nurse_ids: List[str] = field(default_factory=list)
    monthly_capacity: int = 0  # 풀 멤버들의 working_capacity 합
    monthly_demand: int = 0  # 일별 need 합산
    daily_need_max: int = 0  # 가장 빡센 day 의 need
    team_min: Optional[int] = None  # 팀 min 정책 (해당 shift)
    grade_min: Optional[int] = None  # Grade min 정책

    @property
    def allowed_count(self) -> int:
        return len(self.allowed_nurse_ids)

    def to_node_attrs(self) -> Dict[str, Any]:
        return {
            "pool_id": self.pool_id,
            "scope_label": self.scope_label,
            "shift": self.shift,
            "member_count": len(self.member_nurse_ids),
            "allowed_count": self.allowed_count,
            "allowed_nurse_ids": list(self.allowed_nurse_ids),
            "monthly_capacity": self.monthly_capacity,
            "monthly_demand": self.monthly_demand,
            "daily_need_max": self.daily_need_max,
            "team_min": self.team_min,
            "grade_min": self.grade_min,
        }


@dataclass
class PoolShortage:
    """capacity < demand 인 경우 emit 되는 root-cause 후보."""

    pool: PoolStats
    kind: str  # ``count_below_policy`` | ``monthly_capacity_below_demand``
    required: int
    available: int
    human_message_ko: str

    def to_core(self) -> Dict[str, Any]:
        """ConflictCore-shape dict — 기존 conflict_cores 파이프라인에 합류."""
        return {
            "core_id": f"pool_shortage:{self.pool.pool_id}",
            "scope": "pool",
            "pattern": f"pool_shortage:{self.kind}",
            "causal_layer": "policy",
            "affected_count": self.pool.allowed_count,
            "affected_scope_keys": [self.pool.pool_id],
            "members": [
                {
                    "node_id": self.pool.pool_id,
                    "type": self.pool.pool_type,
                    "label": f"{self.pool.scope_label} · {self.pool.shift} 풀",
                    "value": self.available,
                    "human_message_ko": (
                        f"{self.pool.scope_label} 의 {self.pool.shift} 풀 "
                        f"가용 {self.available} (필요 {self.required})"
                    ),
                },
            ],
            "derivation": [
                {"step": 1, "from": f"{self.pool.scope_label} 멤버 시프트 마스크",
                 "infer": f"{self.pool.shift} 가능자 = {self.available} 명"},
                {"step": 2, "from": "정책 (team_min / grade_min / coverage_min)",
                 "infer": f"필요 = {self.required}"},
                {"step": 99, "conclusion":
                 f"{self.pool.scope_label} {self.pool.shift} 풀: {self.available} < {self.required}"},
            ],
            "conclusion": (
                f"{self.pool.scope_label} 의 {self.pool.shift} 풀 가용 {self.available} "
                f"< 필요 {self.required}"
            ),
            "resolution_hints": [
                {
                    "action": "expand_pool_eligibility",
                    "human_message_ko": (
                        f"{self.pool.scope_label} 의 {self.pool.shift} 가능 간호사를 늘리거나 "
                        f"이 시프트 의 정책 (min={self.required}) 을 낮추세요."
                    ),
                },
            ],
            "human_message_ko": self.human_message_ko,
            "source": "pool_graph",
        }


@dataclass
class PoolSnapshot:
    """pool 그래프 1세트 — runner / dashboard 로 직렬화 가능한 형태."""

    pools: List[PoolStats] = field(default_factory=list)
    shortages: List[PoolShortage] = field(default_factory=list)
    nurse_pool_edges: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pools": [
                {
                    "pool_id": p.pool_id,
                    "pool_type": p.pool_type,
                    "attrs": p.to_node_attrs(),
                }
                for p in self.pools
            ],
            "shortages": [s.to_core() for s in self.shortages],
            "nurse_pool_edges": list(self.nurse_pool_edges),
        }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_pool_snapshot(inp: PrecheckInput) -> PoolSnapshot:
    """``PrecheckInput`` 로부터 pool 그래프 한 세트를 산출한다."""

    S = _shifts(inp)
    cfg = inp.roster_config or {}
    nurses = list(inp.nurses or [])

    # 정책 dict 정리
    team_min_by_team: Dict[Any, Dict[str, int]] = {}
    for tid, by_shift in (inp.team_coverage or {}).items():
        if not isinstance(by_shift, dict):
            continue
        clean: Dict[str, int] = {}
        for k, v in by_shift.items():
            try:
                iv = int(v or 0)
            except (TypeError, ValueError):
                continue
            if iv > 0:
                clean[str(k).upper()] = iv
        if clean:
            team_min_by_team[str(tid)] = clean

    grade_min_raw = ((inp.grade_constraints or {}).get("minimum_by_shift") or {})
    grade_min_by_grade: Dict[int, Dict[str, int]] = {}
    for shift, by_grade in grade_min_raw.items():
        if not isinstance(by_grade, dict):
            continue
        for g_raw, v_raw in by_grade.items():
            try:
                g = int(g_raw)
                v = int(v_raw or 0)
            except (TypeError, ValueError):
                continue
            if v > 0:
                grade_min_by_grade.setdefault(g, {})[str(shift).upper()] = v

    # 팀 ID 집합 (정책에 없어도 멤버가 있으면 풀 생성)
    team_ids: Set[Any] = set(team_min_by_team.keys())
    for n in nurses:
        if n.team_id not in (None, "", 0):
            team_ids.add(str(n.team_id))
    grade_ids: Set[int] = set(grade_min_by_grade.keys())
    for n in nurses:
        if n.grade is not None:
            try:
                grade_ids.add(int(n.grade))
            except (TypeError, ValueError):
                continue

    # 멤버십 (팀 / Grade / 공통풀)
    by_team: Dict[Any, List[PrecheckNurse]] = {}
    by_grade: Dict[int, List[PrecheckNurse]] = {}
    common_members: List[PrecheckNurse] = []
    for n in nurses:
        if n.team_id in (None, "", 0):
            common_members.append(n)
        else:
            by_team.setdefault(str(n.team_id), []).append(n)
        if n.grade is not None:
            try:
                by_grade.setdefault(int(n.grade), []).append(n)
            except (TypeError, ValueError):
                pass

    pools: List[PoolStats] = []
    shortages: List[PoolShortage] = []
    edges: List[Dict[str, Any]] = []

    # 팀 풀
    for tid in sorted(team_ids, key=str):
        members = by_team.get(str(tid), [])
        for s in S:
            allowed = [m for m in members if s in _allowed_set(m, S)]
            allowed_ids = [str(m.nurse_id) for m in allowed]
            monthly_cap = sum(_working_capacity(m, cfg) for m in allowed)
            daily = [_daily_need(cfg, s, d) for d in range(inp.num_days)]
            need_max = max(daily) if daily else 0
            need_total = sum(daily)
            tmin_val = (team_min_by_team.get(str(tid)) or {}).get(s)
            pool = PoolStats(
                pool_id=f"team_pool:team_{tid}:{s}",
                pool_type="TeamPoolNode",
                scope_label=f"Team {tid}",
                shift=s,
                member_nurse_ids=[str(m.nurse_id) for m in members],
                allowed_nurse_ids=allowed_ids,
                monthly_capacity=monthly_cap,
                monthly_demand=tmin_val * inp.num_days if tmin_val else 0,
                daily_need_max=need_max,
                team_min=tmin_val,
            )
            pools.append(pool)

            # team_min 정책이 있고, 가용 인원 자체가 team_min 미만이면 즉시 shortage
            if tmin_val and pool.allowed_count < tmin_val:
                shortages.append(PoolShortage(
                    pool=pool,
                    kind="count_below_policy",
                    required=tmin_val,
                    available=pool.allowed_count,
                    human_message_ko=(
                        f"Team {tid} 의 {s} 풀 가용 {pool.allowed_count} 명 "
                        f"< team_min={tmin_val} — 정원 미달."
                    ),
                ))
            # 월간 capacity 부족 (allowed nurse 들의 work days 합 < team_min × num_days)
            if tmin_val and pool.monthly_capacity < pool.monthly_demand:
                shortages.append(PoolShortage(
                    pool=pool,
                    kind="monthly_capacity_below_demand",
                    required=pool.monthly_demand,
                    available=pool.monthly_capacity,
                    human_message_ko=(
                        f"Team {tid} 의 {s} 월 가능 시프트 {pool.monthly_capacity} "
                        f"< 월 요구 {pool.monthly_demand}."
                    ),
                ))
            # 시프트 가능 여부 edge — 멤버 중 미허용자는 풀을 깎는다
            for m in members:
                if s in _allowed_set(m, S):
                    edges.append({
                        "rel": "MEMBER_OF_POOL",
                        "src": f"nurse:{m.nurse_id}",
                        "dst": pool.pool_id,
                    })
                else:
                    edges.append({
                        "rel": "REDUCES_POOL",
                        "src": f"nurse:{m.nurse_id}",
                        "dst": pool.pool_id,
                        "reason": "shift_not_allowed",
                    })

    # Grade 풀
    for g in sorted(grade_ids):
        members = by_grade.get(g, [])
        for s in S:
            allowed = [m for m in members if s in _allowed_set(m, S)]
            allowed_ids = [str(m.nurse_id) for m in allowed]
            monthly_cap = sum(_working_capacity(m, cfg) for m in allowed)
            daily = [_daily_need(cfg, s, d) for d in range(inp.num_days)]
            need_max = max(daily) if daily else 0
            gmin = (grade_min_by_grade.get(g) or {}).get(s)
            pool = PoolStats(
                pool_id=f"grade_pool:grade_{g}:{s}",
                pool_type="GradePoolNode",
                scope_label=f"Grade {g}",
                shift=s,
                member_nurse_ids=[str(m.nurse_id) for m in members],
                allowed_nurse_ids=allowed_ids,
                monthly_capacity=monthly_cap,
                monthly_demand=gmin * inp.num_days if gmin else 0,
                daily_need_max=need_max,
                grade_min=gmin,
            )
            pools.append(pool)

            if gmin and pool.allowed_count < gmin:
                shortages.append(PoolShortage(
                    pool=pool,
                    kind="count_below_policy",
                    required=gmin,
                    available=pool.allowed_count,
                    human_message_ko=(
                        f"Grade {g} 의 {s} 풀 가용 {pool.allowed_count} 명 "
                        f"< grade_min={gmin} — 정원 미달."
                    ),
                ))
            if gmin and pool.monthly_capacity < pool.monthly_demand:
                shortages.append(PoolShortage(
                    pool=pool,
                    kind="monthly_capacity_below_demand",
                    required=pool.monthly_demand,
                    available=pool.monthly_capacity,
                    human_message_ko=(
                        f"Grade {g} 의 {s} 월 가능 시프트 {pool.monthly_capacity} "
                        f"< 월 요구 {pool.monthly_demand}."
                    ),
                ))
            for m in members:
                if s in _allowed_set(m, S):
                    edges.append({
                        "rel": "MEMBER_OF_POOL",
                        "src": f"nurse:{m.nurse_id}",
                        "dst": pool.pool_id,
                    })
                else:
                    edges.append({
                        "rel": "REDUCES_POOL",
                        "src": f"nurse:{m.nurse_id}",
                        "dst": pool.pool_id,
                        "reason": "shift_not_allowed",
                    })

    # 공통풀 (team_id 없는 간호사들)
    if common_members:
        for s in S:
            allowed = [m for m in common_members if s in _allowed_set(m, S)]
            allowed_ids = [str(m.nurse_id) for m in allowed]
            monthly_cap = sum(_working_capacity(m, cfg) for m in allowed)
            pool = PoolStats(
                pool_id=f"common_pool:{s}",
                pool_type="CommonPoolNode",
                scope_label="공통풀",
                shift=s,
                member_nurse_ids=[str(m.nurse_id) for m in common_members],
                allowed_nurse_ids=allowed_ids,
                monthly_capacity=monthly_cap,
                monthly_demand=0,
                daily_need_max=0,
            )
            pools.append(pool)
            for m in common_members:
                if s in _allowed_set(m, S):
                    edges.append({
                        "rel": "MEMBER_OF_POOL",
                        "src": f"nurse:{m.nurse_id}",
                        "dst": pool.pool_id,
                    })
                else:
                    edges.append({
                        "rel": "REDUCES_POOL",
                        "src": f"nurse:{m.nurse_id}",
                        "dst": pool.pool_id,
                        "reason": "shift_not_allowed",
                    })

    return PoolSnapshot(pools=pools, shortages=shortages, nurse_pool_edges=edges)


def build_pool_snapshot_from_runtime(
    *,
    nurses_dict: List[dict],
    config_dict: dict,
    grade_config: Optional[dict],
    fixed_cells: Optional[Any],
    year: int,
    month: int,
) -> PoolSnapshot:
    """런타임 dict 들에서 :class:`PoolSnapshot` 을 만들어준다.

    :func:`build_precheck_input` 와 동일한 입력 schema 를 받는다 — precheck 와
    pool 분석을 같은 데이터로 일관되게 돌리기 위함.
    """
    try:
        inp = build_precheck_input(
            nurses_dict=nurses_dict,
            config_dict=config_dict,
            grade_config=grade_config,
            fixed_cells=fixed_cells,
            year=year,
            month=month,
        )
    except Exception as exc:
        print(f"[PoolGraph] build_precheck_input 실패: {exc}")
        return PoolSnapshot()
    try:
        return build_pool_snapshot(inp)
    except Exception as exc:
        print(f"[PoolGraph] build_pool_snapshot 실패: {exc}")
        return PoolSnapshot()
