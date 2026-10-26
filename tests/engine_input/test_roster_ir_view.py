"""EI-14b1a — `views.to_roster_ir`: precheck 시점 그래프 소비자(presolve·섀도·원인 브리지)의 입력 뷰.

14b1a 는 **불변**이다 — 겹침 0, 뷰는 `pin` 의 객체를 복사 없이 돌려주고 조회도 0 이다(BR-7 재발 방지를 구조로).
"""
from __future__ import annotations

import ast
import calendar
import copy
from pathlib import Path

import pytest

from .capture import QueryLog
from .test_mssql_shim import shim_db  # noqa: F401 — 픽스처 재사용
from .test_views import APP_ROOT, MONTH, YEAR, _make_precheck_stage, _views
from .ward import nurse_row

RCS = APP_ROOT / "services" / "roster_create_service.py"


def _pin(config=None):
    views = _views()
    stage = _make_precheck_stage()
    cfg = {"n2n_min_gap": 4, "off_days": 8} if config is None else config
    return stage, cfg, views.to_precheck_input(stage, cfg, log=lambda _m: None)


@pytest.fixture
def no_overlays(monkeypatch):
    """14b1a 의 '불변' 단언은 겹침 0 에서 성립한다 — 겹침(14b2a)은 test_roster_ir_overlays.py 가 본다."""
    monkeypatch.setattr(_views(), "_OVERLAYS", ())


def test_identity_with_pin(no_overlays):
    views = _views()
    stage, cfg, pin = _pin()
    ir = views.to_roster_ir(stage, cfg, pin=pin)
    assert ir.nurses is pin.nurses_dict
    assert ir.config is pin.config_dict
    assert ir.applied == ()
    assert (ir.year, ir.month) == (YEAR, MONTH)
    assert ir.num_days == calendar.monthrange(YEAR, MONTH)[1]


def test_without_pin_equals_to_precheck_input(no_overlays):
    views = _views()
    stage, cfg, pin = _pin()
    ir = views.to_roster_ir(stage, cfg, log=lambda _m: None)
    assert ir.nurses == pin.nurses_dict
    assert ir.config == pin.config_dict


def test_zero_queries_and_getters_untouched(shim_db, engine):  # noqa: F811
    views = _views()
    stage, cfg, pin = _pin()
    before = (stage.grade.loaded, stage.weekend_ids.loaded, stage.team_rows.loaded, stage.shift_lookup.loaded)
    log = QueryLog()
    log.attach(engine)
    log.armed = True
    try:
        views.to_roster_ir(stage, cfg, pin=pin)
    finally:
        log.detach()
    assert log.statements == []
    assert before == (stage.grade.loaded, stage.weekend_ids.loaded, stage.team_rows.loaded, stage.shift_lookup.loaded)


def test_default_overlays_never_mutate_or_raise_on_odd_config():
    """겹침이 켜져 있어도 `pin.config_dict` 는 제자리 수정되지 않고, 이상한 모양에도 안 터진다."""
    views = _views()
    _stage, _cfg, pin = _pin()
    odd = type(pin)(nurses_dict=pin.nurses_dict, config_dict={"x": object(), "shift_priority": None}, grade_config={},
                    fixed_cells=[], year=YEAR, month=MONTH)
    snap = dict(odd.config_dict)
    ir = views.to_roster_ir(None, {}, pin=odd, log=lambda _m: None)
    assert odd.config_dict == snap
    assert ir.nurses is odd.nurses_dict


def test_does_not_mutate_pin_config():
    views = _views()
    stage, cfg, pin = _pin()
    snap = copy.deepcopy(pin.config_dict)
    views.to_roster_ir(stage, cfg, pin=pin)
    assert pin.config_dict == snap


def test_requires_stage_without_pin_only():
    views = _views()
    with pytest.raises(RuntimeError):
        views.to_roster_ir(None, {})
    _stage, cfg, pin = _pin()
    ir = views.to_roster_ir(None, cfg, pin=pin)  # pin 이 있으면 stage_a 없이도 터지지 않는다
    assert ir.nurses is pin.nurses_dict


def test_to_roster_ir_never_raises_with_odd_config(no_overlays):
    views = _views()
    _stage, _cfg, pin = _pin()
    odd = type(pin)(nurses_dict=pin.nurses_dict, config_dict={"x": object()}, grade_config={}, fixed_cells=[],
                    year=YEAR, month=MONTH)
    ir = views.to_roster_ir(None, {}, pin=odd)
    assert ir.config is odd.config_dict


def _attr_chain(node):
    return ast.unparse(node)


def _calls(tree, name):
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)
            and (getattr(n.func, "id", None) == name or getattr(n.func, "attr", None) == name)]


def test_rcs_passes_ir_to_the_three_consumers():
    tree = ast.parse(RCS.read_text(encoding="utf-8"))
    for fn in ("presolve_shortage_diagnosis", "run_shadow", "_build_precheck_cause_bridge_finding", "_ihf", "_msig"):
        calls = [c for c in _calls(tree, fn) if c.args]
        assert calls, fn
        for c in calls:
            assert _attr_chain(c.args[0]) == "_ir.nurses", (fn, _attr_chain(c.args[0]))
            assert _attr_chain(c.args[1]) == "_ir.config", (fn, _attr_chain(c.args[1]))


def test_ir_is_built_after_precheck_runs():
    """뷰 문제가 precheck 를 못 끄도록 — `_ir = to_roster_ir(` 줄이 `run_runtime_precheck(` 줄보다 뒤다."""
    lines = RCS.read_text(encoding="utf-8").splitlines()
    ir_line = next(i for i, ln in enumerate(lines) if "_ir = to_roster_ir(" in ln)
    pre_line = next(i for i, ln in enumerate(lines) if "precheck_result = run_runtime_precheck(" in ln)
    assert pre_line < ir_line < pre_line + 6


def test_views_has_no_top_level_rcs_import_and_exports():
    views = _views()
    assert {"RosterIRInput", "to_roster_ir"} <= set(views.__all__)
    tree = ast.parse(Path(views.__file__).read_text(encoding="utf-8"))
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module != "services.roster_create_service"


def test_ir_nurse_rows_untouched():
    views = _views()
    stage, cfg, pin = _pin()
    rows = [nurse_row("N01", 1)]
    pin.nurses_dict.extend(rows)
    ir = views.to_roster_ir(stage, cfg, pin=pin)
    assert ir.nurses[-1] is rows[0]
