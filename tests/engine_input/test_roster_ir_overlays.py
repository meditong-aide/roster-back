"""EI-14b2a — `views.to_roster_ir` 의 겹침(정본 키 번역 · 고정 셀 · by_day) 단위 시험.

K-*: 14b2a1 정본 키 번역 / C-*: 14b2a2 고정 셀 / D-*: 14b2a3 by_day(뷰 쪽).
"""
from __future__ import annotations

import calendar
import copy
from types import SimpleNamespace

import pytest

NODES = 30  # 2026-11


def _views():
    from services.engine_input import views

    return views


def _cfg(**over):
    base = {
        "daily_shift_requirements": {"D": 1, "E": 1, "N": 1}, "off_days": 8, "max_nig_per_month": 15,
        "two_offs_after_three_nig": True, "initial_constraints": {"forbidden": {}, "forced_off": {}},
        "max_conseq_work": 2, "three_seq_nig": True, "ban_n_to_d": True, "banned_day_after_eve": True,
    }
    base.update(over)
    return base


def _nurses(n=4, **kw):
    return [dict({"nurse_id": f"n{i}", "name": f"n{i}"}, **kw) for i in range(n)]


def _pin(cfg=None, nurses=None, fixed=None, year=2026, month=11):
    return SimpleNamespace(nurses_dict=nurses if nurses is not None else _nurses(), config_dict=cfg if cfg is not None else _cfg(),
                           fixed_cells=fixed or [], year=year, month=month)


def _ir(pin):
    return _views().to_roster_ir(None, pin.config_dict, pin=pin, log=lambda _m: None)


# ── 14b2a1 정본 키 번역 ────────────────────────────────────────────────


def test_k1_db_keys_are_translated_to_graph_names():
    pin = _pin()
    ir = _ir(pin)
    assert "keys" in ir.applied
    assert ir.config["max_consecutive_work"] == 2
    assert ir.config["max_consecutive_nights"] == 3
    assert ir.config["forbid_night_to_day"] is True


def test_k2_masking_and_two_night_limit():
    ir = _ir(_pin(_cfg(banned_day_after_eve=False, three_seq_nig=False)))
    assert ir.config["forbid_night_to_day"] is False
    assert ir.config["max_consecutive_nights"] == 2


def test_k3_pin_config_is_not_mutated():
    pin = _pin()
    before = copy.deepcopy(pin.config_dict)
    ir = _ir(pin)
    assert pin.config_dict == before
    assert ir.config is not pin.config_dict
    assert "max_consecutive_work" not in pin.config_dict


def test_k4_unreadable_config_falls_back_to_pin_values():
    pin = _pin(_cfg(shift_priority=None))
    ir = _ir(pin)
    assert ir.config is pin.config_dict
    assert ir.applied == () and ir.notes


def test_k5_no_stdout(capsys):
    _ir(_pin())
    assert capsys.readouterr().out == ""


def test_k6_graph_rules_and_verdict_flip():
    from services.ontology_graph.hybrid_solver import solve_hybrid
    from services.ontology_graph.roster_ir import MaxConsecutiveWorkRule, parse_to_ir

    pin = _pin()
    raw_rules = parse_to_ir(pin.nurses_dict, pin.config_dict, NODES).rules
    assert not any(isinstance(r, MaxConsecutiveWorkRule) for r in raw_rules)
    ir = _ir(pin)
    rules = parse_to_ir(ir.nurses, ir.config, NODES).rules
    assert any(isinstance(r, MaxConsecutiveWorkRule) and r.limit == 2 for r in rules)
    before = solve_hybrid(copy.deepcopy(pin.nurses_dict), copy.deepcopy(pin.config_dict), NODES, budget=1_200_000)
    after = solve_hybrid(copy.deepcopy(ir.nurses), copy.deepcopy(ir.config), NODES, budget=1_200_000)
    assert before.status == "UNKNOWN"
    assert after.status == "INFEASIBLE_CERTIFIED"


def test_k7_env_cap_is_reflected(monkeypatch):
    monkeypatch.setenv("AIDE_MAX_CONSEC_WORK", "4")
    ir = _ir(_pin(_cfg(max_conseq_work=6)))
    assert ir.config["max_consecutive_work"] == 4
    monkeypatch.setenv("AIDE_MAX_CONSEC_WORK", "9")
    ir = _ir(_pin(_cfg(max_conseq_work=6)))
    assert ir.config["max_consecutive_work"] == 6


@pytest.mark.parametrize("over", [{}, {"three_seq_nig": None}])
def test_k8_view_does_not_reinterpret_defaults(over):
    from services.precheck.hard_lock_view import engine_hard_lock

    cfg = _cfg(**over)
    if "three_seq_nig" in over:
        cfg.pop("three_seq_nig")
    eff = engine_hard_lock(copy.deepcopy(cfg))
    ir = _ir(_pin(cfg))
    assert ir.config["max_consecutive_nights"] == eff["three_seq_nig"]
    assert ir.config["max_consecutive_work"] == eff["max_conseq_work"]
    assert calendar.monthrange(2026, 11)[1] == ir.num_days


# ── 14b2a2 고정 셀 겹침 ────────────────────────────────────────────────


def _cell(nid, day, shift):
    return {"nurse_id": nid, "day_index": day, "shift": shift}


def test_c1_fixed_off_becomes_forced_off():
    ir = _ir(_pin(fixed=[_cell("n0", 4, "O"), _cell("n0", 2, "O")]))
    assert "cells" in ir.applied
    assert ir.config["initial_constraints"]["forced_off"]["n0"] == [2, 4]


def test_c2_fixed_work_forbids_the_others_and_off():
    ir = _ir(_pin(fixed=[_cell("n1", 3, "D")]))
    assert ir.config["initial_constraints"]["forbidden"]["n1"][3] == ["E", "N", "O"]


def test_c3_union_with_existing_initial_constraints():
    cfg = _cfg(initial_constraints={"forbidden": {"n1": {3: ["N"], "5": ["E"]}}, "forced_off": {"n0": [4]}})
    ir = _ir(_pin(cfg, fixed=[_cell("n1", 3, "E"), _cell("n0", 4, "O"), _cell("n0", 1, "O")]))
    ic = ir.config["initial_constraints"]
    assert ic["forbidden"]["n1"][3] == ["D", "N", "O"]      # {N} ∪ {D,N,O}
    assert ic["forbidden"]["n1"][5] == ["E"]                # 문자열 키는 int 로 정규화
    assert ic["forced_off"]["n0"] == [1, 4]                 # 중복 없음·정렬


def test_c4_original_initial_constraints_untouched():
    cfg = _cfg(initial_constraints={"forbidden": {"n1": {3: ["N"]}}, "forced_off": {"n0": [4]}})
    pin = _pin(cfg, fixed=[_cell("n1", 3, "E"), _cell("n0", 9, "O")])
    before = copy.deepcopy(pin.config_dict["initial_constraints"])
    ir = _ir(pin)
    assert pin.config_dict["initial_constraints"] == before
    assert ir.config["initial_constraints"] is not pin.config_dict["initial_constraints"]


@pytest.mark.parametrize("cells,extra", [
    ([_cell("n0", 4, "N"), _cell("n0", 5, "D")], {}),
    ([_cell("n0", 4, "N"), _cell("n0", 5, "E")], {}),
    ([_cell("n0", 0, "N")], {"not_one_night": True}),
])
def test_c5_exempt_adjacent_nurse_loses_all_cells_but_others_keep(cells, extra):
    cells = cells + [_cell("n0", 9, "O"), _cell("n1", 3, "D")]
    ir = _ir(_pin(_cfg(**extra), fixed=cells))
    ic = ir.config["initial_constraints"]
    assert "n0" not in ic["forbidden"] and "n0" not in ic["forced_off"]
    assert ic["forbidden"]["n1"][3] == ["E", "N", "O"]
    assert any("면제 인접 1명" in n for n in ir.notes)


def test_c5b_first_day_night_without_not_one_night_is_not_exempt():
    ir = _ir(_pin(fixed=[_cell("n0", 0, "N")]))
    assert ir.config["initial_constraints"]["forbidden"]["n0"][0] == ["D", "E", "O"]


def test_c6_other_codes_unknown_nurses_and_out_of_month_days_are_skipped():
    ir = _ir(_pin(fixed=[_cell("n0", 3, "M"), _cell("n0", 4, "W"), _cell("zz", 3, "D"), _cell("n0", 99, "O"),
                         _cell("n0", -1, "O")]))
    assert "cells" not in ir.applied
    assert ir.config["initial_constraints"] == {"forbidden": {}, "forced_off": {}}


def test_c7_fixed_cells_reach_the_graph_and_flag_the_ward():
    """같은 날 두 명 모두 고정 OFF 면 그날 야간 1명을 채울 수 없다 — 전(고정 셀 미반영)은 못 보고(UNKNOWN), 후는 증명한다."""
    from services.ontology_graph.hybrid_solver import solve_hybrid

    cfg = _cfg(max_conseq_work=5, daily_shift_requirements={"D": 0, "E": 0, "N": 1})
    pin = _pin(cfg, _nurses(2), [_cell("n0", 6, "O"), _cell("n1", 6, "O")])
    before = solve_hybrid(copy.deepcopy(pin.nurses_dict), copy.deepcopy(pin.config_dict), NODES, budget=1_200_000)
    ir = _ir(pin)
    after = solve_hybrid(copy.deepcopy(ir.nurses), copy.deepcopy(ir.config), NODES, budget=1_200_000)
    assert before.status != "INFEASIBLE_CERTIFIED"
    assert after.status == "INFEASIBLE_CERTIFIED"


# ── 14b2a3 by_day 겹침(뷰 쪽 · D-7) ─────────────────────────────────────


def _bd(**weekend):
    rows = [{"D": 1, "E": 1, "N": 1} for _ in range(NODES)]
    for d in (4, 5):
        rows[d] = {"D": 1, "E": 1, "N": 1, **weekend}
    return rows


def test_d7_by_day_is_normalized_into_private_key():
    from services.ontology_graph.demand import IR_BY_DAY_KEY

    rows = _bd(N=3)
    rows[7] = "bad"                                  # dict 아닌 원소는 평탄 행
    rows[8] = {"D": 2, "E": 0, "N": 1, "M": 5}       # M 키는 버린다
    pin = _pin(_cfg(daily_shift_requirements_by_day=rows))
    ir = _ir(pin)
    assert "by_day" in ir.applied
    got = ir.config[IR_BY_DAY_KEY]
    assert len(got) == NODES
    assert got[4] == {"D": 1, "E": 1, "N": 3}
    assert got[7] == {"D": 1, "E": 1, "N": 1}
    assert got[8] == {"D": 2, "E": 0, "N": 1}
    assert "daily_shift_requirements_by_day" in ir.config            # raw 키는 그대로
    assert IR_BY_DAY_KEY not in pin.config_dict                      # 원본 불변


def test_d7b_length_is_fitted_and_flat_rows_give_no_key():
    from services.ontology_graph.demand import IR_BY_DAY_KEY

    short = [{"D": 1, "E": 1, "N": 2}] * 3
    ir = _ir(_pin(_cfg(daily_shift_requirements_by_day=short)))
    assert len(ir.config[IR_BY_DAY_KEY]) == NODES
    assert ir.config[IR_BY_DAY_KEY][3] == {"D": 1, "E": 1, "N": 1}   # 모자란 날은 평탄
    flat = _ir(_pin(_cfg(daily_shift_requirements_by_day=[{"D": 1, "E": 1, "N": 1}] * NODES)))
    assert IR_BY_DAY_KEY not in flat.config
    not_list = _ir(_pin(_cfg(daily_shift_requirements_by_day={"x": 1})))
    assert IR_BY_DAY_KEY not in not_list.config


def test_d7c_view_makes_the_graph_see_weekend_peak():
    from services.ontology_graph.hybrid_solver import solve_hybrid

    cfg = _cfg(max_conseq_work=5, daily_shift_requirements={"D": 0, "E": 0, "N": 1},
               daily_shift_requirements_by_day=[{"D": 0, "E": 0, "N": 3 if d in (4, 5) else 1} for d in range(NODES)])
    pin = _pin(cfg, _nurses(2))
    before = solve_hybrid(copy.deepcopy(pin.nurses_dict), copy.deepcopy(pin.config_dict), NODES, budget=1_200_000)
    ir = _ir(pin)
    after = solve_hybrid(copy.deepcopy(ir.nurses), copy.deepcopy(ir.config), NODES, budget=1_200_000)
    assert before.status != "INFEASIBLE_CERTIFIED"
    assert after.status == "INFEASIBLE_CERTIFIED"


def test_c1r_request_injected_private_key_is_dropped():
    """EI-14C1r — 요청 config_override 로 들어온 `_ir_by_day` 는 뷰가 버린다(겹침이 만든 값만 믿는다)."""
    from services.ontology_graph.demand import IR_BY_DAY_KEY

    fake = [{"D": 9, "E": 9, "N": 9} for _ in range(NODES)]
    pin = _pin(_cfg(**{IR_BY_DAY_KEY: fake}))
    ir = _ir(pin)
    assert IR_BY_DAY_KEY not in ir.config
    assert IR_BY_DAY_KEY in pin.config_dict                          # 원본은 그대로
    made = _ir(_pin(_cfg(**{IR_BY_DAY_KEY: fake, "daily_shift_requirements_by_day": _bd(N=3)})))
    assert made.config[IR_BY_DAY_KEY][4] == {"D": 1, "E": 1, "N": 3}  # 겹침이 만든 값이 이긴다
