"""EI-14b2a3 — 일자별 수요(by_day): 그래프 엔진은 뷰가 준 사설 키(`_ir_by_day`)만 일자별로 읽는다.

D-1 demand 헬퍼 / D-2 IR·signature / D-3 frontier DP / D-4 explain 산술 / D-5 joint coverage / D-6 사설 키 없으면 불변 /
D-7 뷰 정규화(test_roster_ir_overlays.py).
"""
from __future__ import annotations

import calendar
import copy

from services.ontology_graph.demand import IR_BY_DAY_KEY, demand_by_day, demand_on_day, flat_demand, has_by_day
from services.ontology_graph.frontier_dp import diagnose_frontier
from services.ontology_graph.hybrid_solver import solve_hybrid
from services.ontology_graph.joint_coverage import detect_joint_night_infeasible
from services.ontology_graph.lagrangian import explain_infeasibility, explain_infeasibility_from_config
from services.ontology_graph.roster_ir import CoverageRule, model_signature, parse_to_ir

YEAR, MONTH, ND = 2026, 11, 30


def _weekend_days():
    return [d for d in range(ND) if calendar.weekday(YEAR, MONTH, d + 1) >= 5]


def _by_day(flat, weekend):
    rows = [dict(flat) for _ in range(ND)]
    for d in _weekend_days():
        rows[d] = dict(weekend)
    return rows


def _nurses(n):
    return [{"nurse_id": f"n{i}", "name": f"n{i}"} for i in range(n)]


def _cfg(flat=None, **over):
    base = {"daily_shift_requirements": dict(flat or {"D": 0, "E": 0, "N": 1}), "max_consecutive_work": 5}
    base.update(over)
    return base


# ── D-1 ────────────────────────────────────────────────────────────────


def test_d1_demand_helpers():
    cfg = _cfg()
    assert flat_demand(cfg) == {"D": 0, "E": 0, "N": 1}
    assert not has_by_day(cfg)
    assert demand_by_day(cfg, 3) == [{"D": 0, "E": 0, "N": 1}] * 3
    cfg[IR_BY_DAY_KEY] = [{"D": 1, "E": 2, "N": 3}, "bad", {"N": "2"}]
    assert has_by_day(cfg)
    assert demand_on_day(cfg, 0) == {"D": 1, "E": 2, "N": 3}
    assert demand_on_day(cfg, 1) == flat_demand(cfg)          # dict 아닌 원소는 평탄
    assert demand_on_day(cfg, 2) == {"D": 0, "E": 0, "N": 2}
    assert demand_on_day(cfg, 9) == flat_demand(cfg)          # 짧으면 평탄
    assert len(demand_by_day(cfg, 5)) == 5
    assert flat_demand({"nig_req": 2}) == {"D": 0, "E": 0, "N": 2}
    cfg[IR_BY_DAY_KEY] = []
    assert not has_by_day(cfg)


# ── D-2 ────────────────────────────────────────────────────────────────


def test_d2_ir_and_signature_follow_by_day():
    nurses = _nurses(3)
    plain = _cfg()
    withd = _cfg(**{IR_BY_DAY_KEY: _by_day({"D": 0, "E": 0, "N": 1}, {"D": 0, "E": 0, "N": 2})})
    cov0 = next(r for r in parse_to_ir(nurses, plain, ND).rules if isinstance(r, CoverageRule))
    cov1 = next(r for r in parse_to_ir(nurses, withd, ND).rules if isinstance(r, CoverageRule))
    assert cov0.by_day is None and cov1.by_day is not None
    assert len(cov1.by_day) == ND and cov1.reqN == 1          # 평탄 값은 그대로
    assert model_signature(nurses, plain, ND) != model_signature(nurses, withd, ND)


def test_d2b_signature_unchanged_without_private_key():
    nurses = _nurses(3)
    raw = _cfg(daily_shift_requirements_by_day=_by_day({"D": 0, "E": 0, "N": 1}, {"D": 0, "E": 0, "N": 2}))
    assert model_signature(nurses, raw, ND) == model_signature(nurses, _cfg(), ND)


# ── D-3 ────────────────────────────────────────────────────────────────


def test_d3_frontier_dp_sees_weekend_peak():
    nurses = _nurses(2)
    by_day = _by_day({"D": 0, "E": 0, "N": 1}, {"D": 0, "E": 0, "N": 3})
    plain = _cfg()
    assert diagnose_frontier(nurses, plain, ND).status != "INFEASIBLE_CERTIFIED"
    withd = _cfg(**{IR_BY_DAY_KEY: by_day})
    res = diagnose_frontier(nurses, withd, ND)
    assert res.status == "INFEASIBLE_CERTIFIED"
    assert res.collapse_day in _weekend_days()
    assert res.certificate.witness["req"]["N"] == 3


def test_d3b_hybrid_sees_weekend_peak():
    nurses = _nurses(2)
    withd = _cfg(**{IR_BY_DAY_KEY: _by_day({"D": 0, "E": 0, "N": 1}, {"D": 0, "E": 0, "N": 3})})
    assert solve_hybrid(copy.deepcopy(nurses), copy.deepcopy(_cfg()), ND).status != "INFEASIBLE_CERTIFIED"
    assert solve_hybrid(copy.deepcopy(nurses), copy.deepcopy(withd), ND).status == "INFEASIBLE_CERTIFIED"


# ── D-4 ────────────────────────────────────────────────────────────────


def test_d4_explain_arithmetic_uses_peak_and_total():
    flat = {"D": 1, "E": 1, "N": 1}
    rows = _by_day(flat, {"D": 1, "E": 1, "N": 4})            # 주말 하루 6명
    # 평탄(3) 기준이면 4명으로 충분하지만 일자별 최대(6)면 인원 부족
    assert explain_infeasibility(4, ND, flat).classification != "coverage_shortage"
    assert explain_infeasibility(4, ND, flat, req_by_day=rows).classification == "coverage_shortage"
    # 총합 기준 off 예산: 8명 × 30 = 240 셀, 수요 총합 = 3×(30-8)+6×8 = 114 → off 예산 126 vs 하한 8×8=64 → 과제약 아님
    big = explain_infeasibility(8, ND, flat, off_floor=8, req_by_day=rows)
    assert big.arithmetic["coverage_demand"] == 3 * (ND - len(_weekend_days())) + 6 * len(_weekend_days())


def test_d4b_from_config_passes_by_day_only_with_private_key():
    nurses = _nurses(4)
    rows = _by_day({"D": 1, "E": 1, "N": 1}, {"D": 1, "E": 1, "N": 4})
    base = _cfg({"D": 1, "E": 1, "N": 1})
    raw = copy.deepcopy(base)
    raw["daily_shift_requirements_by_day"] = rows            # raw 키는 계속 무시
    withd = copy.deepcopy(base)
    withd[IR_BY_DAY_KEY] = rows
    a = explain_infeasibility_from_config(nurses, base, ND, year=YEAR, month=MONTH)
    b = explain_infeasibility_from_config(nurses, raw, ND, year=YEAR, month=MONTH)
    c = explain_infeasibility_from_config(nurses, withd, ND, year=YEAR, month=MONTH)
    assert (a.classification, a.arithmetic) == (b.classification, b.arithmetic)
    assert c.classification == "coverage_shortage" and c.arithmetic != a.arithmetic


# ── D-5 ────────────────────────────────────────────────────────────────


def test_d5_joint_night_coverage_uses_per_day_demand():
    nurses = _nurses(3)
    plain = _cfg({"D": 0, "E": 0, "N": 1}, two_offs_after_three_nig=True)
    withd = _cfg({"D": 0, "E": 0, "N": 1}, two_offs_after_three_nig=True,
                 **{IR_BY_DAY_KEY: _by_day({"D": 0, "E": 0, "N": 1}, {"D": 0, "E": 0, "N": 4})})
    assert detect_joint_night_infeasible(nurses, plain, ND) is None
    got = detect_joint_night_infeasible(nurses, withd, ND)
    assert got is not None and got["demand_n"] == 4


# ── D-6 사설 키가 없으면 raw by_day 가 있어도 결과 동일 ────────────────────


def test_d6_raw_by_day_is_ignored_everywhere():
    nurses = _nurses(3)
    base = _cfg({"D": 0, "E": 0, "N": 1}, two_offs_after_three_nig=True)
    raw = copy.deepcopy(base)
    raw["daily_shift_requirements_by_day"] = _by_day({"D": 0, "E": 0, "N": 1}, {"D": 0, "E": 0, "N": 3})
    for fn in (lambda c: diagnose_frontier(nurses, c, ND).status,
               lambda c: solve_hybrid(copy.deepcopy(nurses), copy.deepcopy(c), ND).status,
               lambda c: detect_joint_night_infeasible(nurses, c, ND) is None,
               lambda c: model_signature(nurses, c, ND)):
        assert fn(base) == fn(raw)


# ── D-8 presolve (14b2a4) ──────────────────────────────────────────────


def _presolve(nurses, cfg):
    import contextlib
    import io

    from services.ontology_graph.presolve_diagnosis import presolve_shortage_diagnosis

    with contextlib.redirect_stdout(io.StringIO()):
        return presolve_shortage_diagnosis(copy.deepcopy(nurses), copy.deepcopy(cfg), YEAR, MONTH)


def _shape(res):
    return (res["shortages"], res["weekend_release_needed"], res["pressure_families"], res["per_day_bottleneck_cells"])


def test_d8_presolve_ignores_raw_by_day_and_matches_flat_without_private_key():
    nurses = _nurses(2)
    base = _cfg()
    raw = copy.deepcopy(base)
    raw["daily_shift_requirements_by_day"] = _by_day({"D": 0, "E": 0, "N": 1}, {"D": 0, "E": 0, "N": 3})
    assert _shape(_presolve(nurses, base)) == _shape(_presolve(nurses, raw))


def test_d8_presolve_sees_weekend_peak_with_private_key():
    nurses = _nurses(2)
    withd = _cfg(**{IR_BY_DAY_KEY: _by_day({"D": 0, "E": 0, "N": 1}, {"D": 0, "E": 0, "N": 3})})
    plain = _presolve(nurses, _cfg())
    got = _presolve(nurses, withd)
    assert not plain["shortages"] and plain["weekend_release_needed"] == 0
    assert got["shortages"] and got["shortages"][0]["shift"] == "N"
    assert got["shortages"][0]["daily_required"] == 3          # 보고용 수요는 시프트별 최대값
    assert got["weekend_release_needed"] == 1                  # 주말 하루 부족 1 (3 − 2명)


def test_d8_presolve_lower_weekend_demand_removes_flat_shortage():
    """평탄 수요로는 부족인데 일자별 수요(주말 E 0)로는 부족이 아니다 — 평탄이 과대였다."""
    nurses = [{"nurse_id": "a", "name": "a", "allowed_shifts": ["D", "N"]}, {"nurse_id": "b", "name": "b", "allowed_shifts": ["D", "N"]}]
    flat = {"D": 1, "E": 1, "N": 1}
    base = _cfg(flat)
    withd = _cfg(flat, **{IR_BY_DAY_KEY: [{"D": 1, "E": 0, "N": 1}] * ND})
    assert _presolve(nurses, base)["shortages"]
    assert not _presolve(nurses, withd)["shortages"]


def test_c1r_signature_of_flat_ward_keeps_old_format():
    """EI-14C1r — 평탄 병동 서명은 CoverageRule.by_day(None) 필터 덕에 옛 문자열과 같다(필터 제거 변이 사살)."""
    sig = model_signature(_nurses(3), _cfg(), ND)
    assert "'by_day'" not in sig


def test_c1r_weekend_aggregate_uses_the_busiest_weekend_day():
    """EI-14C1r — 주말 날마다 수요가 다르면 '가장 바쁜 날'로 주말휴무 부족을 센다(max → min 변이 사살)."""
    nurses = _nurses(4)
    nurses[0]["is_weekend_off"] = True
    rows = [{"D": 0, "E": 0, "N": 1} for _ in range(ND)]
    for d in _weekend_days():
        rows[d] = {"D": 0, "E": 0, "N": 1 if calendar.weekday(YEAR, MONTH, d + 1) == 5 else 4}   # 토 1 · 일 4
    cfg = _cfg({"D": 0, "E": 0, "N": 1}, **{IR_BY_DAY_KEY: rows})
    got = explain_infeasibility_from_config(nurses, cfg, ND, year=YEAR, month=MONTH)
    assert got.classification == "coverage_shortage" and got.arithmetic["weekend_demand"] == 4
    assert got.arithmetic["weekend_available"] == 3 and got.arithmetic["release_needed"] == 1
