"""EI-14b-1b·2b — 사후 소비자 5곳이 `to_roster_ir_post` 로 받는다(p2/D/SPEC.md §4.8~4.11).

1b: 겹침 0 · 불변(정체성) · AST 배선. 2b1: 정본 키(K-P) · 2b2: 고정 셀(C-P) · 2b3: 일자별 수요(D-P).
"""
from __future__ import annotations

import ast
import calendar
import json
from pathlib import Path
from types import SimpleNamespace

from services.engine_contract.names import to_internal
from services.engine_input import views
from services.engine_input.views import RosterIRInput, to_roster_ir_post

from .golden import GOLDEN_DIR
from .golden_gate import GOLDEN, require_golden


def _args():
    nurses = [SimpleNamespace(nurse_id="N01"), SimpleNamespace(nurse_id="N02")]
    config = {"max_conseq_work": 5, "initial_constraints": None}
    return nurses, config


def test_without_overlays_the_inputs_come_back_as_the_same_objects(monkeypatch):
    monkeypatch.setattr(views, "_OVERLAYS_POST", ())   # 14b-1b 상태 — 겹침 0
    nurses, config = _args()
    ir = to_roster_ir_post(None, nurses, config, year=2026, month=11)
    assert isinstance(ir, RosterIRInput)
    assert ir.nurses is nurses and ir.config is config
    assert (ir.year, ir.month, ir.num_days) == (2026, 11, calendar.monthrange(2026, 11)[1])
    assert ir.applied == () and ir.notes == () and ir.ei is None


def test_ei_is_carried_and_changes_nothing_and_a_missing_ei_gives_the_same_result(monkeypatch):
    monkeypatch.setattr(views, "_OVERLAYS_POST", ())
    nurses, config = _args()
    ei = SimpleNamespace(cells={})
    with_ei = to_roster_ir_post(ei, nurses, config, year=2026, month=11)
    without = to_roster_ir_post(None, nurses, config, year=2026, month=11)
    assert with_ei.ei is ei
    assert (with_ei.nurses, with_ei.config, with_ei.num_days, with_ei.applied) == (
        without.nurses, without.config, without.num_days, without.applied)


def test_it_never_raises_and_a_failing_overlay_falls_back_to_the_given_objects(monkeypatch):
    nurses, config = _args()
    logs: list[str] = []

    def boom(*_a):
        raise RuntimeError("overlay-boom")

    monkeypatch.setattr(views, "_OVERLAYS_POST", (("boom", boom),))
    ir = to_roster_ir_post(None, nurses, config, year=2026, month=11, log=logs.append)
    assert ir.nurses is nurses and ir.config is config and ir.applied == ()
    assert logs and "roster_ir_post boom" in logs[0]
    # 달 값이 깨져도 예외 없음
    assert to_roster_ir_post(None, nurses, config, year=2026, month=13).num_days == 0


def test_an_overlay_that_changes_the_config_gets_a_copy_and_the_original_stays(monkeypatch):
    nurses, config = _args()

    def add_key(cfg, _ei, _nurses, _days, _notes, _smd):
        cfg["added"] = 1
        return True

    monkeypatch.setattr(views, "_OVERLAYS_POST", (("keys", add_key),))
    ir = to_roster_ir_post(None, nurses, config, year=2026, month=11)
    assert ir.applied == ("keys",) and ir.config is not config and ir.config["added"] == 1 and "added" not in config
    assert ir.nurses is nurses


def test_overlay_registry_order_is_the_stage_order():
    """14b-2b 가 겹침을 하나씩 더한다 — 이름과 순서는 사전 `_OVERLAYS` 와 같다(정본 키 → 고정 셀 → 일자별 수요)."""
    assert tuple(name for name, _ in views._OVERLAYS_POST) in {("keys",), ("keys", "cells"), ("keys", "cells", "by_day")}


# ── 14b-2b1 정본 키 번역 ───────────────────────────────────────────────────────────────────────────────────────


def test_k_p1_keys_overlay_adds_graph_keys_to_a_copy_and_leaves_the_snapshot_alone():
    nurses = [SimpleNamespace(nurse_id="N01")]
    snapshot = {"max_conseq_work": 2, "allow_three_consecutive_nights": True, "ban_n_to_d": True, "initial_constraints": None}
    before = dict(snapshot)
    ir = to_roster_ir_post(None, nurses, snapshot, year=2026, month=11)
    assert "keys" in ir.applied and ir.config is not snapshot
    assert ir.config["max_consecutive_work"] == 2 and ir.config["forbid_night_to_day"] is True
    assert snapshot == before and ir.nurses is nurses


def test_k_p2_keys_overlay_failing_to_read_the_hard_lock_is_fail_open(monkeypatch):
    """`engine_hard_lock` 이 못 읽으면 번역 없이 받은 객체 그대로 — 사다리 진단을 막지 않는다."""
    import services.precheck.hard_lock_view as hard_lock_view

    monkeypatch.setattr(hard_lock_view, "engine_hard_lock", lambda _cfg: None)
    nurses, config = _args()
    ir = to_roster_ir_post(None, nurses, config, year=2026, month=11)
    assert ir.applied == () and ir.config is config and any("정본 키 번역 생략" in n for n in ir.notes)


def test_k_p3_a_request_supplied_private_by_day_key_is_dropped():
    """요청 config_override 가 준 `_ir_by_day` 는 믿지 않는다(C1r) — 겹침이 있든 없든 결과 설정에 안 남는다."""
    from services.ontology_graph.demand import IR_BY_DAY_KEY

    nurses = [SimpleNamespace(nurse_id="N01")]
    ir = to_roster_ir_post(None, nurses, {IR_BY_DAY_KEY: [{"D": 9}], "max_conseq_work": 5}, year=2026, month=11)
    assert IR_BY_DAY_KEY not in ir.config


# ── rcs 배선(AST) — 사후 소비자 5곳이 `_ir_post.*` 를 받는다 ───────────────────────────────────────────────────


def _rcs_tree():
    path = Path(views.__file__).resolve().parent.parent / "roster_create_service.py"
    return ast.parse(path.read_text(encoding="utf-8"))


def _calls(tree, name):
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call) and
            ((isinstance(n.func, ast.Name) and n.func.id == name) or (isinstance(n.func, ast.Attribute) and n.func.attr == name))]


def _is_ir_post_attr(node, attr):
    return isinstance(node, ast.Attribute) and node.attr == attr and isinstance(node.value, ast.Name) and node.value.id == "_ir_post"


def test_the_five_post_consumers_receive_ir_post_attributes():
    tree = _rcs_tree()
    (lam,) = _calls(tree, "lambda_priority_from_config")
    assert _is_ir_post_attr(lam.args[0], "nurses") and _is_ir_post_attr(lam.args[1], "config")
    (explain,) = _calls(tree, "explain_infeasibility_from_config")
    assert _is_ir_post_attr(explain.args[0], "nurses") and _is_ir_post_attr(explain.args[1], "config")
    (night,) = _calls(tree, "diagnose_night_axis")
    assert _is_ir_post_attr(night.args[1], "config")
    (repairs,) = _calls(tree, "verified_repairs")
    assert _is_ir_post_attr(repairs.args[2], "config")
    (dump,) = _calls(tree, "dump_case")
    kw = {k.arg: k.value for k in dump.keywords}
    assert _is_ir_post_attr(kw["nurses"], "nurses") and _is_ir_post_attr(kw["config"], "config")


def test_ir_post_is_built_once_from_the_probe_base_and_the_first_attempt_input():
    tree = _rcs_tree()
    (call,) = _calls(tree, "to_roster_ir_post")
    assert [a.id for a in call.args if isinstance(a, ast.Name)] == ["_ei_first", "nurses_for_engine", "_probe_base"]


def test_rcs_imports_only_the_new_name_from_views():
    tree = _rcs_tree()
    imported = {alias.name for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "services.engine_input.views"
                for alias in n.names}
    assert "to_roster_ir_post" in imported and "to_roster_ir" in imported


# ── 14b-2b2 고정 셀 겹침 ──────────────────────────────────────────────────────────────────────────────────────


def _cells_ei(*cells):
    return SimpleNamespace(load_report=SimpleNamespace(errors={}), cells={"combined_fixed_cells": list(cells)})


def _cell(index, day, shift):
    return {"nurse_index": index, "day_index": day, "shift": shift, "fixed_source": "fixed_wanted",
            "shift_type": "휴무" if shift in ("O", "OFF") else "근무"}


def _two_nurses():
    return [SimpleNamespace(nurse_id="N01", sequence=0, experience=0), SimpleNamespace(nurse_id="N02", sequence=1, experience=0)]


def test_c_p1_fixed_off_and_work_cells_overlay_the_initial_constraints_of_a_copy():
    nurses, snapshot = _two_nurses(), {"initial_constraints": None, "use_mid": False}
    ei = _cells_ei(_cell(0, 3, "O"), _cell(1, 5, "D"))
    ir = to_roster_ir_post(ei, nurses, snapshot, year=2026, month=11)
    assert "cells" in ir.applied
    ic = ir.config["initial_constraints"]
    assert ic["forced_off"] == {"N01": [3]}
    assert ic["forbidden"] == {"N02": {5: ["E", "N", "O"]}}
    assert snapshot["initial_constraints"] is None, "원본 스냅숏은 그대로여야 한다"


def test_c_p2_exempt_adjacent_nurse_is_left_out_like_the_pre_overlay():
    """고정 N 바로 뒤 고정 D — 엔진은 전이 금지를 면제하는데 그래프 DP 는 못 담는다 → 그 간호사 칸은 통째로 제외(오늘 판정 유지)."""
    nurses = _two_nurses()
    ei = _cells_ei(_cell(0, 4, "N"), _cell(0, 5, "D"), _cell(1, 7, "O"))
    ir = to_roster_ir_post(ei, nurses, {"initial_constraints": None}, year=2026, month=11)
    ic = ir.config["initial_constraints"]
    assert "N01" not in (ic.get("forbidden") or {}) and "N01" not in (ic.get("forced_off") or {})
    assert ic["forced_off"] == {"N02": [7]}
    assert any("면제 인접 1명 제외" in n for n in ir.notes)


def test_c_p3_without_ei_or_cells_the_overlay_is_skipped_and_notes_say_why():
    nurses, snapshot = _two_nurses(), {"initial_constraints": None}
    for ei in (None, _cells_ei()):
        ir = to_roster_ir_post(ei, nurses, snapshot, year=2026, month=11)
        assert "cells" not in ir.applied and any("고정 셀 겹침 생략" in n for n in ir.notes)


def test_c_p4_cells_on_assignment_blocked_days_are_dropped():
    nurses = _two_nurses()
    ei = _cells_ei(_cell(0, 3, "O"), _cell(0, 4, "O"))
    ir = to_roster_ir_post(ei, nurses, {"initial_constraints": None, "blocked_by_nurse_id": {"N01": {3}}}, year=2026, month=11)
    assert ir.config["initial_constraints"]["forced_off"] == {"N01": [4]}


def test_c_p5_the_overlay_failing_is_fail_open(monkeypatch):
    nurses, snapshot = _two_nurses(), {"initial_constraints": None}
    monkeypatch.setattr(views, "precheck_fixed_cells", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("cells-boom")))
    logs: list[str] = []
    ir = to_roster_ir_post(_cells_ei(_cell(0, 3, "O")), nurses, snapshot, year=2026, month=11, log=logs.append)
    assert ir.config is snapshot or "cells" not in ir.applied
    assert any("roster_ir_post cells" in m for m in logs)


def test_c_p6_snapshot_ban_or_forced_off_on_a_fixed_day_is_dropped_because_the_fixed_cell_wins():
    """엔진은 고정 칸이 있는 날의 금지·강제 OFF 를 무시한다(고정이 이긴다) — 사후도 스냅숏 쪽을 먼저 뺀다(사전 `_attach_stage_b` 와 같은 기준)."""
    nurses = _two_nurses()
    snapshot = {"initial_constraints": {"forbidden": {"N01": {3: ["D"], 9: ["E"]}}, "forced_off": {"N01": [5, 8]}}}
    ei = _cells_ei(_cell(0, 3, "D"), _cell(0, 5, "N"))
    ir = to_roster_ir_post(ei, nurses, snapshot, year=2026, month=11)
    ic = ir.config["initial_constraints"]
    assert ic["forbidden"]["N01"][3] == ["E", "N", "O"], "금지 D 가 남아 D 까지 전부 금지되면 가짜 모순이다"
    assert ic["forbidden"]["N01"][9] == ["E"] and ic["forced_off"]["N01"] == [8]
    assert ic["forbidden"]["N01"][5] == ["D", "E", "O"]
    assert snapshot["initial_constraints"]["forbidden"]["N01"][3] == ["D"], "원본 스냅숏은 그대로"


def test_c_p7_a_strip_alone_still_counts_as_applied_when_no_cell_translates():
    nurses = _two_nurses()
    snapshot = {"initial_constraints": {"forbidden": {"N01": {3: ["D"]}}, "forced_off": {}}}
    ei = _cells_ei(_cell(0, 3, "W"))     # precheck 가 모델링 못 하는 코드 — 번역은 비지만 엔진은 그 날 금지를 무시한다
    ir = to_roster_ir_post(ei, nurses, snapshot, year=2026, month=11)
    assert "cells" in ir.applied
    assert not (ir.config["initial_constraints"].get("forbidden") or {})


# ── 14b-2b3 일자별 수요 ──────────────────────────────────────────────────────────────────────────────────────


def _by_day_snapshot(weekend_n=3):
    nd = calendar.monthrange(2026, 11)[1]
    rows = [{"D": 1, "E": 1, "N": 1} for _ in range(nd)]
    for d in range(nd):
        if calendar.weekday(2026, 11, d + 1) >= 5:
            rows[d] = {"D": 1, "E": 1, "N": weekend_n}
    return {"daily_shift_requirements": {"D": 1, "E": 1, "N": 1}, "daily_shift_requirements_by_day": rows, "initial_constraints": None}


def test_d_p1_by_day_overlay_adds_the_private_key_to_a_copy_from_the_snapshot_itself():
    from services.ontology_graph.demand import IR_BY_DAY_KEY

    snapshot = _by_day_snapshot()
    ir = to_roster_ir_post(None, [SimpleNamespace(nurse_id="N01")], snapshot, year=2026, month=11)
    assert "by_day" in ir.applied and IR_BY_DAY_KEY in ir.config and IR_BY_DAY_KEY not in snapshot
    weekend = [d for d in range(30) if calendar.weekday(2026, 11, d + 1) >= 5]
    assert all(ir.config[IR_BY_DAY_KEY][d]["N"] == 3 for d in weekend)


def test_d_p2_flat_by_day_makes_no_key():
    from services.ontology_graph.demand import IR_BY_DAY_KEY

    flat = _by_day_snapshot(weekend_n=1)
    ir = to_roster_ir_post(None, [SimpleNamespace(nurse_id="N01")], flat, year=2026, month=11)
    assert "by_day" not in ir.applied and IR_BY_DAY_KEY not in ir.config


def test_d_p3_a_request_supplied_private_key_is_discarded_even_when_the_overlay_adds_its_own():
    from services.ontology_graph.demand import IR_BY_DAY_KEY

    snapshot = {**_by_day_snapshot(), IR_BY_DAY_KEY: [{"D": 99, "E": 99, "N": 99}] * 30}
    ir = to_roster_ir_post(None, [SimpleNamespace(nurse_id="N01")], snapshot, year=2026, month=11)
    assert ir.config[IR_BY_DAY_KEY][0] == snapshot["daily_shift_requirements_by_day"][0]   # 스냅숏 값(요청 값 99 아님)
    assert snapshot[IR_BY_DAY_KEY][0]["D"] == 99, "원본은 그대로"


def _read_s1_lt_golden() -> dict:
    """사다리 골든 S1-LT 기록."""
    require_golden(GOLDEN)
    return json.loads((GOLDEN_DIR / "S1-LT.json").read_text(encoding="utf-8"))


def test_d_p4_s1_lt_snapshot_demand_is_not_flat_so_the_overlay_would_hit_the_lambda_stage():
    """S1-LT(고정근무 차감 뒤 평일 D 3 · 주말 4)의 첫 시도 수요는 평탄이 아니다 → 겹침이 `_ir_by_day` 를 만든다. 그래서 사다리 골든은 이 시나리오에서만
    그 겹침을 끈다(`scenario_ladder.skip_post_by_day` — λ 단계 재풀이가 하네스에서 막힘, D-D14). 원천 일치: L1 수요 = precheck 입력(L0, 차감 뒤)."""
    from services.ontology_graph.demand import IR_BY_DAY_KEY

    record = _read_s1_lt_golden()
    l1_cfg = to_internal(record["layers"]["L1"][0]["config_data"])   # 골든은 옛(바깥) 이름일 수 있다(재생성 전)
    assert l1_cfg["daily_shift_requirements_by_day"] == record["layers"]["L0"][0]["config_dict"]["daily_shift_requirements_by_day"]
    ir = to_roster_ir_post(None, [SimpleNamespace(nurse_id="N01")], l1_cfg, year=2026, month=11)
    assert "by_day" in ir.applied and IR_BY_DAY_KEY in ir.config
