"""EI-14r1 — 경고로 내려간 precheck 이슈 2종의 온톨로지 심각도 표기.

`FIXED_ASSIGN_EXCEEDS_NEED`·`FIXED_OFF_EXCEEDS_SPAN` 는 EI-10d 부터 이슈 `severity="warning"` 인데, 이슈 안 `ontology` 블록은
늘 `precheck_blocked`/`hard` 로 나와 어긋났다. 이제 두 칸이 `precheck_warning`/`soft` 다. 차단 여부(`has_blocking_issues`)는 불변.
"""
from __future__ import annotations

from services.precheck import run_runtime_precheck
from services.precheck.payload import has_blocking_issues
from services.semantics.ontology import get_default_ontology

from tests.test_precheck_fixed_cell_results import OFF_SPAN, OVER_NEED, _cell, _cfg, _nurses

WARNING_CODES = (OVER_NEED, OFF_SPAN)


def _run(nurses, cfg, cells):
    return run_runtime_precheck(
        nurses_dict=nurses, config_dict=cfg, grade_config=None, fixed_cells=cells,
        year=2026, month=11, stop_on_config_error=False)


def _issues(result, code):
    return [i for i in result["issues"] if i["reason_code"] == code]


def test_yaml_mode_exists_and_is_soft():
    mode = get_default_ontology().get_mode("precheck_warning")
    assert mode is not None and mode.severity == "soft" and mode.is_enforced is False
    blocked = get_default_ontology().get_mode("precheck_blocked")
    assert blocked is not None and blocked.severity == "hard"


def test_over_need_warning_is_marked_soft():
    cells = [_cell(f"n{i}", 4, "D") for i in range(3)]
    result = _run(_nurses(), _cfg(), cells)
    issues = _issues(result, OVER_NEED)
    assert issues
    for issue in issues:
        assert issue["severity"] == "warning"
        assert issue["ontology"]["mode"] == "precheck_warning"
        assert issue["ontology"]["severity"] == "soft"
        assert issue["ontology"]["constraint_id"] == "ConfigIntegrity"
        assert set(issue["ontology"]) == {"constraint_id", "group", "scope", "mode", "severity"}
    assert not has_blocking_issues(result)


def test_off_span_warning_is_marked_soft():
    nurses = _nurses() + [{"nurse_id": "p0", "grade": 1, "allowed_shifts": [], "joining_date": "2026-11-21"}]
    cells = [_cell("p0", day, "O") for day in range(20, 30)]
    result = _run(nurses, _cfg(), cells)
    issues = _issues(result, OFF_SPAN)
    assert issues
    for issue in issues:
        assert issue["severity"] == "warning"
        assert issue["ontology"]["mode"] == "precheck_warning"
        assert issue["ontology"]["severity"] == "soft"
    assert not has_blocking_issues(result)


def test_blocking_code_keeps_precheck_blocked_hard():
    """차단 코드(GLOBAL_DAY_CAPACITY_SHORTAGE)는 표기가 그대로다."""
    nurses = _nurses(2)
    result = _run(nurses, _cfg(daily_shift_requirements={"D": 3, "E": 3, "N": 3}), [])
    issues = _issues(result, "GLOBAL_DAY_CAPACITY_SHORTAGE")
    assert issues and has_blocking_issues(result)
    for issue in issues:
        assert issue["severity"] == "hard"
        assert issue["ontology"]["mode"] == "precheck_blocked"
        assert issue["ontology"]["severity"] == "hard"
