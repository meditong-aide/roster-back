"""EI-10g4 — 단계 B 제약(`initial_constraints`: forced_off·forbidden)이 개인 DP 에 들어가는 길 — 싣기(views) · 읽기(bridge) · DP."""

import copy
from types import SimpleNamespace

from services.engine_input.views import _attach_stage_b
from services.precheck.per_nurse_sequence_feasibility import nurse_sequence_infeasible
from services.precheck.runtime_bridge import _stage_b_constraints_for, build_precheck_input


def _ei(initial_constraints, cells=(), errors=None):
    return SimpleNamespace(load_report=SimpleNamespace(errors=errors or {}),
                           cells={"initial_constraints": initial_constraints, "combined_fixed_cells": list(cells)})


def _src():
    return {"nurses": [SimpleNamespace(nurse_id="N01", sequence=0, experience=0)]}


def _attach(ei, base=None):
    config = dict(base or {})
    _attach_stage_b(config, ei, _src(), lambda _m: None)
    return config


def test_attach_puts_the_merged_constraints_in_engine_key_shape_without_touching_the_source():
    ic = {"forced_off": {"N01": [2, 5]}, "forbidden": {"N01": {3: ["N"], 7: ["D", "E"]}}}
    config = _attach(_ei(ic))
    assert config["initial_constraints"] == {"forced_off": {"N01": [2, 5]}, "forbidden": {"N01": {3: ["N"], 7: ["D", "E"]}}}
    assert config["initial_constraints"] is not ic     # 사본


def test_attach_drops_days_that_have_an_engine_form_fixed_cell_even_if_precheck_cannot_model_the_code():
    """엔진 형식 고정 칸(어떤 코드든)이 있는 날은 금지·강제 OFF 를 뺀다 — 고정이 이긴다(엔진은 그 날을 무시한다)."""
    ic = {"forced_off": {"N01": [2, 5]}, "forbidden": {"N01": {3: ["N"], 7: ["D"]}}}
    cells = [{"nurse_index": 0, "day_index": 2, "shift": "W"}, {"nurse_index": 0, "day_index": 3, "shift": "D"}]
    config = _attach(_ei(ic, cells))
    assert config["initial_constraints"] == {"forced_off": {"N01": [5]}, "forbidden": {"N01": {7: ["D"]}}}


def test_attach_leaves_the_config_alone_when_empty_or_when_a_stage_b_part_failed_or_without_ei():
    base = {"x": 1}
    assert _attach(_ei({}), base) == base                                  # 비어 있으면 키를 안 만든다
    assert _attach(_ei({"forced_off": {"N01": [2]}}, [{"nurse_index": 0, "day_index": 2, "shift": "D"}]), base) == base
    assert _attach(_ei({"forced_off": {"N01": [2]}}, errors={"stage_b.banned": "boom"}), base) == base   # 하드 칸 부품 실패
    assert _attach(None, base) == base
    keep = {"x": 1, "initial_constraints": {}}
    assert _attach(_ei({}), keep) == keep                                  # 빈 값으로 이미 있던 키도 건드리지 않는다(값이 든 키는 12y 시험)


def test_attach_clears_existing_mid_month_constraints_when_fixed_days_remove_every_merged_one():
    """12y(Codex 4차 P2) — config 에 월중 경계 몫이 이미 있고(rcs 가 precheck 앞에서 심는다 — 병합본도 같은 config 로 지어 그것을 담는다)
    고정 칸 걸름이 병합본을 전부 지우면 빈 결과로 바꾼다. 걸러지기 전 값이 남으면 엔진이 무시하는 고정 칸 날의 강제 OFF·금지가 precheck 에 산다.
    원 config 의 값과 병합본은 그대로다(엔진 입력 불변 — precheck 사본만 바뀐다)."""
    mid = {"forced_off": {"N01": [2]}, "forbidden": {"N01": {2: ["N"]}}}
    base = {"x": 1, "initial_constraints": mid}
    ei = _ei(copy.deepcopy(mid), [{"nurse_index": 0, "day_index": 2, "shift": "W"}])
    config = _attach(ei, base)
    assert config["initial_constraints"] == {"forced_off": {}, "forbidden": {}}
    assert base["initial_constraints"] is mid and mid == {"forced_off": {"N01": [2]}, "forbidden": {"N01": {2: ["N"]}}}
    assert ei.cells["initial_constraints"] == mid
    # 병합본이 처음부터 비어도 같다 — 옛 단언('이미 있던 키도 건드리지 않는다')을 이것으로 바꿨다
    assert _attach(_ei({}), base)["initial_constraints"] == {"forced_off": {}, "forbidden": {}}


def test_attach_swallows_malformed_input():
    config = _attach(SimpleNamespace(load_report=None, cells={"initial_constraints": {"forced_off": {"N01": ["x"]}}}))
    assert "initial_constraints" not in config


def test_bridge_reads_only_active_unfixed_days_and_known_codes():
    ic = {"forced_off": {"N01": [1, 4, 40, True]}, "forbidden": {"N01": {"2": ["n", "zz"], "5": ["D"], "9": ["zz"], "x": ["N"]}}}
    forced, forbidden = _stage_b_constraints_for(ic, "N01", {}, (0, 20), skip_days={4, 5})
    assert forced == frozenset({1})
    assert forbidden == {2: frozenset({"N"})}


def test_bridge_ignores_bad_shapes_and_preceptees_are_skipped_by_the_caller():
    assert _stage_b_constraints_for("garbage", "N01", {}, (0, 20), set()) == (frozenset(), {})
    assert _stage_b_constraints_for({"forced_off": [1]}, "N01", {}, (0, 20), set()) == (frozenset(), {})


def _build(initial_constraints, **nurse):
    nd = {"nurse_id": "N01", "name": "N01", "allowed_shifts": [], "joining_date": "2024-03-01T00:00:00", **nurse}
    return build_precheck_input(
        nurses_dict=[nd], config_dict={"daily_shift_requirements": {"D": 1, "E": 1, "N": 1}, "initial_constraints": initial_constraints},
        grade_config=None, fixed_cells=[{"nurse_id": "N01", "day_index": 3, "shift": "D"}], year=2026, month=11).nurses[0]


def test_precheck_nurse_gets_constraints_except_fixed_days_and_preceptees_get_none():
    ic = {"forced_off": {"N01": [3, 5]}, "forbidden": {"N01": {3: ["N"], 6: ["E"]}}}
    nurse = _build(ic)
    assert nurse.forced_off_days == frozenset({5})
    assert nurse.forbidden_by_day == {6: frozenset({"E"})}
    skipped = _build(ic, preceptor_id="P01")
    assert skipped.forced_off_days == frozenset() and skipped.forbidden_by_day == {}


def test_dp_choices_forbidden_removes_forced_off_wins_fixed_wins():
    base = {"num_days": 4, "allowed": {"N"}, "not_one_night": True}
    # 고정 N(1일) 양옆 N 금지 → 단독 N → 불가능 / 한쪽만 금지면 가능
    assert nurse_sequence_infeasible(**base, fixed={1: "N"}, forbidden={0: {"N"}, 2: {"N"}}) is True
    assert nurse_sequence_infeasible(**base, fixed={1: "N"}, forbidden={0: {"N"}}) is False
    # 양옆 강제 OFF → 불가능 / 강제 OFF 는 금지보다 앞선다(같은 날 O 도 금지돼도 강제 OFF 가 이긴다)
    assert nurse_sequence_infeasible(**base, fixed={1: "N"}, forced_off={0, 2}) is True
    assert nurse_sequence_infeasible(**base, fixed={1: "N"}, forced_off={0}, forbidden={0: {"O"}}) is False
    # 고정 칸이 이긴다 — 같은 날 금지·강제 OFF 가 있어도 고정 값
    assert nurse_sequence_infeasible(**base, fixed={1: "N", 2: "N"}, forbidden={1: {"N"}}, forced_off={2}) is False
    # 모르는 코드는 무시(선택지가 줄지 않는다)
    assert nurse_sequence_infeasible(**base, fixed={1: "N"}, forbidden={0: {"ZZ"}, 2: {"ZZ"}}) is False
