"""배열-only(max-flow blind) 결합 infeasible 케이스 + 우리 파이프라인의 원인지목·완화.

precheck/max-flow(산술·용량)는 통과하는데 **커버리지가 시퀀스 제약을 강제**해 솔버만
INFEASIBLE 이 되는 케이스들. 각 케이스에서:
  1) 용량 clean 검증 (일별 인원 ≥ 수요, 월 야간 capacity 충분) — precheck/max-flow blind 대리
  2) 솔버 INFEASIBLE (배열 결합이 원인)
  3) find_mcs → '무엇을 풀면 feasible' 최소완화 + 재실행 verified (원인=해결책)
  4) mcs_to_graph → score_blame → 그 원인 제약이 문제 랭킹 top (설명)

핀(fixed_cell)은 전이금지가 bypass 되므로(cp_sat_basic:3654) 쓰지 않고, 커버리지 하한이
시퀀스를 강제하도록 구성한다.
"""

from __future__ import annotations

from ortools.sat.python import cp_model

from services.cp_sat.hard_assumption import HardAssumptionRegistry, add_hard
from services.cp_sat.mcs import find_mcs
from services.ontology_graph.blame import score_blame
from services.ontology_graph.mus_bridge import mcs_to_graph
from services.ontology_graph.schema import OntologyGraph

WORK = ("D", "E", "N")
SHIFTS = ("D", "E", "N", "O")


def _mini_roster(num_nurses, num_days, req_by_shift, *,
                 recovery=False, ban_n2d=False, max_consec=None):
    """add_hard 로 감싼 미니 로스터 CP-SAT 모델. 커버리지(하한)+시퀀스 결합."""
    m = cp_model.CpModel()
    reg = HardAssumptionRegistry(m)
    X = {(n, d, s): m.NewBoolVar(f"x_{n}_{d}_{s}")
         for n in range(num_nurses) for d in range(num_days) for s in SHIFTS}
    # 도메인: 하루 1시프트 (구조 제약, 완화 대상 아님)
    for n in range(num_nurses):
        for d in range(num_days):
            m.Add(sum(X[(n, d, s)] for s in SHIFTS) == 1)
    # 커버리지 하한 (열 제약) — add_hard
    for d in range(num_days):
        for s in WORK:
            need = int(req_by_shift.get(s, 0))
            if need <= 0:
                continue
            add_hard(m, reg, name=f"Coverage:{s}:day{d}",
                     constraint_expr=sum(X[(n, d, s)] for n in range(num_nurses)) >= need,
                     meta={"node_id": f"cov:{s}:{d}", "type": "ConstraintNode",
                           "family": "CoverageMin", "pattern": "coverage",
                           "label": f"day{d+1} {s} 최소 {need}"})
    # 야간회복: N(d) → O(d+1) (행 제약) — add_hard
    if recovery:
        for n in range(num_nurses):
            for d in range(num_days - 1):
                add_hard(m, reg, name=f"NightRecovery:n{n}:day{d}",
                         constraint_expr=X[(n, d, "N")] <= X[(n, d + 1, "O")],
                         meta={"node_id": f"nrec:{n}:{d}", "type": "NightRecoveryNode",
                               "pattern": "night_recovery", "label": f"n{n} N→OFF({d+1})"})
    # 전이금지 N→D
    if ban_n2d:
        for n in range(num_nurses):
            for d in range(num_days - 1):
                add_hard(m, reg, name=f"TransitionBanN2D:n{n}:day{d}",
                         constraint_expr=X[(n, d, "N")] + X[(n, d + 1, "D")] <= 1,
                         meta={"node_id": f"tb:{n}:{d}", "type": "TransitionBanNode",
                               "pattern": "transition_ban", "label": f"n{n} N→D금지({d+1})"})
    # 최대 연속근무 (K+1 창에 OFF 1회)
    if max_consec is not None:
        for n in range(num_nurses):
            for d in range(num_days - max_consec):
                add_hard(m, reg, name=f"MaxConsec:n{n}:day{d}",
                         constraint_expr=sum(X[(n, d + k, "O")]
                                             for k in range(max_consec + 1)) >= 1,
                         meta={"node_id": f"mcw:{n}:{d}", "type": "ConsecutiveWorkNode",
                               "pattern": "consecutive_work", "label": f"n{n} 연속≤{max_consec}"})
    return m, reg, X


def _capacity_clean(num_nurses, num_days, req_by_shift):
    """precheck/max-flow 대리: 일별 인원≥총수요 + 월 야간 capacity 충분(=산술 clean)."""
    per_day_need = sum(int(req_by_shift.get(s, 0)) for s in WORK)
    assert num_nurses >= per_day_need, "일별 용량이 부족(=산술이 잡음, 배열-only 아님)"
    monthly_n = int(req_by_shift.get("N", 0)) * num_days
    assert num_nurses * num_days >= monthly_n, "월 야간 capacity 부족(산술이 잡음)"


def _is_infeasible(num_nurses, num_days, req, **kw):
    m, reg, _ = _mini_roster(num_nurses, num_days, req, **kw)
    reg.attach_to_model()
    solver = cp_model.CpSolver()
    return solver.StatusName(solver.Solve(m)) == "INFEASIBLE"


def _diagnose_and_relax(num_nurses, num_days, req, **kw):
    """우리 파이프라인: MCS(원인=최소완화, verified) → blame(문제 랭킹)."""
    m, reg, _ = _mini_roster(num_nurses, num_days, req, **kw)
    res = find_mcs(m, reg, time_limit=10)
    g = mcs_to_graph(OntologyGraph(), res)
    blame = score_blame(g)
    return res, blame


# ── Case 1: 야간회복 × 만원 야간커버리지 ──────────────────────────────────────
def test_case1_night_recovery_vs_full_coverage():
    """N=2 매일 + 2명 → 둘 다 매일 N 강제, 회복(N→OFF)이 다음날 커버 붕괴. 배열-only."""
    req = {"N": 2}
    _capacity_clean(2, 3, req)                 # 일별 2명≥2, 월 6≤6 : 산술 clean
    assert _is_infeasible(2, 3, req, recovery=True)
    res, blame = _diagnose_and_relax(2, 3, req, recovery=True)
    assert res.verified_feasible               # 완화하면 feasible (원인=해결책)
    assert res.relaxed                         # 무엇을 풀지 지목
    fams = {meta.get("pattern") for meta in res.relaxed_meta}
    assert fams & {"night_recovery", "coverage"}   # 회복 또는 커버리지가 수선점
    assert blame.top_constraints               # blame 이 문제 제약 랭킹


# ── Case 2: 전이금지 N→D × 커버리지 강제 ─────────────────────────────────────
def test_case2_transition_ban_forced_by_coverage():
    """1명, 2일. day0 N=1 + day1 D=1 강제 → 그 1명이 N→D → 전이금지. 배열-only."""
    m, reg, X = _mini_roster(1, 2, {}, ban_n2d=True)
    # 커버리지로 N(day0), D(day1) 강제
    add_hard(m, reg, name="Coverage:N:day0",
             constraint_expr=X[(0, 0, "N")] >= 1,
             meta={"family": "CoverageMin", "pattern": "coverage", "label": "day1 N"})
    add_hard(m, reg, name="Coverage:D:day1",
             constraint_expr=X[(0, 1, "D")] >= 1,
             meta={"family": "CoverageMin", "pattern": "coverage", "label": "day2 D"})
    reg.attach_to_model()
    solver = cp_model.CpSolver()
    assert solver.StatusName(solver.Solve(m)) == "INFEASIBLE"

    # 진단·완화 (fresh 모델)
    m2, reg2, X2 = _mini_roster(1, 2, {}, ban_n2d=True)
    add_hard(m2, reg2, name="Coverage:N:day0", constraint_expr=X2[(0, 0, "N")] >= 1,
             meta={"family": "CoverageMin", "pattern": "coverage", "label": "day1 N"})
    add_hard(m2, reg2, name="Coverage:D:day1", constraint_expr=X2[(0, 1, "D")] >= 1,
             meta={"family": "CoverageMin", "pattern": "coverage", "label": "day2 D"})
    res = find_mcs(m2, reg2, time_limit=10)
    assert res.verified_feasible
    blame = score_blame(mcs_to_graph(OntologyGraph(), res))
    assert blame.top_constraints


# ── Case 3: 최대 연속근무 1 × 상시 커버리지 ──────────────────────────────────
def test_case3_max_consecutive_vs_coverage():
    """1명, 2일, 매일 D=1 필요 + 연속근무≤1 → 이틀 연속근무 불가. 배열-only."""
    req = {"D": 1}
    _capacity_clean(1, 2, req)                 # 1명≥1, 월 D 무관 : 산술 clean
    assert _is_infeasible(1, 2, req, max_consec=1)
    res, blame = _diagnose_and_relax(1, 2, req, max_consec=1)
    assert res.verified_feasible
    assert res.relaxed
    assert blame.top_constraints
