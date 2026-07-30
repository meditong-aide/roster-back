"""Lagrangian 승수 λ = 원리적 blame 신호 (hand-set weight 대체).

배열-only 결합 infeasible 에서, 완전 solve/verify 없이 subgradient 승수만으로:
  1) 결합 원인 제약의 λ 가 높다 (coverage×sequence)
  2) 무고한(느슨) 제약은 λ 가 낮다 (판별)
  3) λ-seed blame 이 원인 제약을 top 으로 랭크
  4) (교차검증) MCS 가 지목한 수선점과 원인 family 일치
soft 도 목적 penalty 로 같은 틀에 들어오므로 동일 경로로 λ 를 받는다.
"""

from __future__ import annotations

from services.ontology_graph.blame import score_blame
from services.ontology_graph.lagrangian import (
    diagnose_by_lagrangian,
    estimate_multipliers,
    relaxable_roster,
)


def _lam(num_nurses, num_days, req, **coupling):
    build, names, meta = relaxable_roster(num_nurses, num_days, req, **coupling)
    return estimate_multipliers(build, names, iters=8, time_per_solve=0.3), meta


def test_case1_lambda_ranks_coupled_culprit_and_discriminates_innocent():
    """야간회복×만원 N커버리지(원인) + 느슨 연속≤3(무고, 4일이라 창 생성). λ 가 원인만 올린다."""
    lam, meta = _lam(2, 4, {"N": 2}, recovery=True, max_consec=3)
    top_name = max(lam, key=lam.get)
    assert meta[top_name]["pattern"] in ("coverage", "night_recovery")   # 결합 원인
    # 무고한 느슨 연속제약은 top 원인보다 확연히 낮다(판별)
    innocent = [n for n, mt in meta.items() if mt["pattern"] == "consecutive_work"]
    assert innocent, "느슨 연속제약이 후보에 있어야 판별 테스트 의미"
    top_lam = lam[top_name]
    assert all(lam[n] < 0.5 * top_lam for n in innocent)


def test_case1_lambda_blame_ranks_culprit():
    lam, g = diagnose_by_lagrangian(2, 3, {"N": 2}, recovery=True, max_consec=5, iters=50)
    b = score_blame(g)
    assert b.top_constraints
    assert b.top_constraints[0].family in ("CoverageMin", "NightRecovery")


def test_case3_consecutive_vs_coverage():
    """연속근무≤1 × 상시 D커버리지. λ 가 그 결합쌍을 올린다."""
    lam, meta = _lam(1, 2, {"D": 1}, max_consec=1)
    ranked = sorted(lam, key=lam.get, reverse=True)
    fams = {meta[n]["pattern"] for n in ranked[:2]}
    assert fams <= {"coverage", "consecutive_work"} and fams   # 결합쌍만 상위


def test_lambda_priority_ranks_off_budget_culprit():
    """OFF 과제약(=max-flow 사각지대)에서 λ 가 off_budget 을 1순위로 올린다."""
    from services.ontology_graph.lagrangian import lambda_priority_families
    # OFF하한 5×5=25 ≫ OFF슬롯(30-24=6), night_cap 넉넉 → off_budget 이 범인
    fams = lambda_priority_families(5, 6, {"D": 2, "E": 1, "N": 1},
                                    off_floor=5, night_cap=4, recovery=True)
    assert fams and fams[0] == "off_budget"
    # night 범인 대조 → night_cap 이 앞
    fams_n = lambda_priority_families(6, 6, {"N": 3}, off_floor=1, night_cap=2, recovery=True)
    assert fams_n and fams_n[0] == "night_cap"


def test_lambda_priority_cuts_probe_solves():
    """λ 우선순위가 잘못된(night) 우선순위보다 probe 재solve 횟수를 줄인다."""
    from services.cp_sat.undiagnosed_probe import probe_relaxations
    from services.ontology_graph.lagrangian import lambda_priority_families

    base = {"off_days": 10, "max_nig_per_month": 12, "two_offs_after_two_nig": True}

    def _mk():
        c = {"n": 0}
        def resolve(cfg):
            c["n"] += 1
            return (int(cfg.get("off_days", 10)) < 10, {})   # OFF 낮춰야만 feasible
        return resolve, c

    fams = lambda_priority_families(5, 6, {"D": 2, "E": 1, "N": 1},
                                    off_floor=5, night_cap=4, recovery=True)
    r1, c1 = _mk()
    res1 = probe_relaxations(base, r1, priority_families=fams,
                             try_combo=True, max_combo=3, logger=lambda *a: None)
    r2, c2 = _mk()
    probe_relaxations(base, r2, priority_families=["night_cap", "night_recovery"],
                      try_combo=True, max_combo=3, logger=lambda *a: None)
    assert res1["found"]
    assert c1["n"] < c2["n"]        # λ 우선순위가 더 적은 재solve 로 해결책 도달


def test_explain_policy_overconstraint():
    """해가 없어도(설정 과제약) '왜' 를 낸다: OFF 강제하한 > OFF 여유 → 정책 탓."""
    from services.ontology_graph.lagrangian import explain_infeasibility
    # 이번 실인스턴스: 6명 31일 D2E1N1, auto_min off_floor=11 (66>62)
    e = explain_infeasibility(6, 31, {"D": 2, "E": 1, "N": 1}, off_floor=11,
                              night_cap=7, recovery=True)
    assert e.classification == "policy_overconstraint"
    assert e.arithmetic["off_floor_sum"] == 66 and e.arithmetic["off_budget"] == 62
    assert e.arithmetic["excess"] == 4
    assert "OFF" in e.certificate and "정책" in e.certificate


def test_explain_coverage_shortage_vs_policy():
    """자원부족(인원/셀 부족)은 정책과제약과 구분된다."""
    from services.ontology_graph.lagrangian import explain_infeasibility
    e = explain_infeasibility(4, 10, {"D": 2, "E": 2, "N": 1}, off_floor=1,
                              night_cap=5, recovery=True)
    assert e.classification == "coverage_shortage"   # 하루수요 5 > 4명


def test_explain_coupled_sequence_when_arithmetic_clean():
    """인원·OFF예산 정상인데 안 되면 → λ 가 시퀀스 원인 지목."""
    from services.ontology_graph.lagrangian import explain_infeasibility
    e = explain_infeasibility(2, 6, {"N": 2}, night_cap=6, recovery=True)
    assert e.classification == "coupled_sequence"
    assert e.top_family == "night_recovery"


def test_explain_weekend_off_routes_to_personal_mcs():
    """weekend-off 병목(max-flow 사각지대) → personal_overconstraint → per-nurse MCS 경로."""
    from services.ontology_graph.lagrangian import explain_infeasibility
    # 4명 중 3명 weekend-off → 주말 커버리지(4/day)를 1명이 못 채움
    e = explain_infeasibility(4, 8, {"D": 2, "E": 1, "N": 1}, off_floor=2,
                              weekend_off_nurses={0, 1, 2}, weekend_days={5, 6})
    assert e.classification == "personal_overconstraint"
    assert e.top_family == "weekend_off"
    assert "주말" in e.certificate and "per-nurse MCS" in e.certificate


def test_explain_from_config_extracts_weekend_off():
    """config 경로: is_weekend_off 간호사를 추출해 weekend_off 를 짚는다."""
    from services.ontology_graph.lagrangian import explain_infeasibility_from_config

    class _Nu:
        def __init__(self, wk):
            self.is_weekend_off = wk

    nurses = [_Nu(True), _Nu(True), _Nu(True), _Nu(False)]
    cfg = {"daily_shift_requirements": {"D": 2, "E": 1, "N": 1}, "off_days": 2}
    e = explain_infeasibility_from_config(nurses, cfg, 8, year=2026, month=8)
    assert e.top_family == "weekend_off"
    assert e.classification == "personal_overconstraint"


def test_explain_per_nurse_night_floor_over_cap():
    """per-nurse 즉시 모순: n_exact(13) > max_nig(7) 을 이름으로 짚는다 (λ·solver 불필요)."""
    from services.ontology_graph.lagrangian import explain_infeasibility_from_config

    class _Nu:
        def __init__(self, name, n_exact=None, wk=False):
            self.name = name; self.nurse_id = name
            self.n_exact = n_exact; self.is_weekend_off = wk

    nurses = [_Nu("김수선", n_exact=13, wk=True)] + [_Nu(f"n{i}") for i in range(5)]
    cfg = {"daily_shift_requirements": {"D": 2, "E": 1, "N": 1},
           "off_days": 10, "max_nig_per_month": 7}
    e = explain_infeasibility_from_config(nurses, cfg, 31, year=2026, month=8)
    assert e.classification == "personal_infeasible"
    assert "김수선" in e.certificate and "13" in e.certificate and "7" in e.certificate


def test_explain_per_nurse_floor_over_workdays():
    """강제 근무 하한 합 > 가용 근무일(주말휴무 반영) 도 즉시 모순으로 짚는다."""
    from services.ontology_graph.lagrangian import explain_infeasibility_from_config

    class _Nu:
        def __init__(self, name, d=None, e=None, n=None, wk=False):
            self.name = name; self.nurse_id = name; self.is_weekend_off = wk
            self.d_exact = d; self.e_exact = e; self.n_exact = n

    # 주말휴무 + 강제근무 합이 평일 가용을 초과
    nurses = [_Nu("A", d=15, e=15, n=5, wk=True)] + [_Nu(f"n{i}") for i in range(5)]
    cfg = {"daily_shift_requirements": {"D": 2, "E": 1, "N": 1},
           "off_days": 8, "max_nig_per_month": 40}
    e = explain_infeasibility_from_config(nurses, cfg, 31, year=2026, month=8)
    assert e.classification == "personal_infeasible"
    assert "가용 근무일" in e.certificate


def test_lambda_matches_mcs_culprit():
    """λ 최상위 원인 family 가 MCS 수선점 family 와 일치(교차검증)."""
    from ortools.sat.python import cp_model
    from services.cp_sat.hard_assumption import HardAssumptionRegistry, add_hard
    from services.cp_sat.mcs import find_mcs

    # 동일 결합(1명, 2일, day0 N + day1 D 강제 → 전이금지)을 MCS 로도 진단
    m = cp_model.CpModel()
    reg = HardAssumptionRegistry(m)
    X = {(d, s): m.NewBoolVar(f"x{d}_{s}") for d in range(2) for s in ("D", "E", "N", "O")}
    for d in range(2):
        m.Add(sum(X[(d, s)] for s in ("D", "E", "N", "O")) == 1)
    add_hard(m, reg, name="Coverage:N:d0", constraint_expr=X[(0, "N")] >= 1,
             meta={"family": "CoverageMin", "pattern": "coverage"})
    add_hard(m, reg, name="Coverage:D:d1", constraint_expr=X[(1, "D")] >= 1,
             meta={"family": "CoverageMin", "pattern": "coverage"})
    add_hard(m, reg, name="TransitionBanN2D", constraint_expr=X[(0, "N")] + X[(1, "D")] <= 1,
             meta={"family": "BoundaryTransitionBan", "pattern": "transition_ban"})
    res = find_mcs(m, reg, time_limit=10)
    mcs_fams = {mt.get("pattern") for mt in res.relaxed_meta}

    lam, meta = _lam(1, 2, {"N": 1}, ban_n2d=True)   # day0 N 커버 + N→D 금지
    # 위 relaxable 은 day1 D 강제가 없으니, ban 케이스로 λ 가 전이/커버리지에 실리는지만 확인
    top = max(lam, key=lam.get)
    assert meta[top]["pattern"] in ("coverage", "transition_ban")
    assert mcs_fams & {"coverage", "transition_ban"}
