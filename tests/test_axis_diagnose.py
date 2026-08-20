"""다축 surplus 진단 — N-시퀀스 너머 D/E/N 커버리지(max-flow) 통합, argmin-surplus 지목."""

from __future__ import annotations

from services.ontology_graph.axis_diagnose import multi_axis_diagnose, render_axis
from services.ontology_graph.certificate import INFEASIBLE

_RULES = {"two_offs_after_three_nig": True, "not_one_night": True}


def _cfg(dsr, forbidden=None):
    c = dict(_RULES, daily_shift_requirements=dsr, off_days=1)
    c["initial_constraints"] = {"forbidden": forbidden or {}, "forced_off": {}}
    return c


def _pool(k, allowed=None):
    return [{"nurse_id": f"n{i}", "name": f"N{i}", "grade": 1, "team_id": "A",
             **({"allowed_shifts": allowed} if allowed else {})} for i in range(k)]


def test_coverage_axis_certified_beyond_n_sequence():
    """수요 > 인원(D2E2N1=5 > 4) → N축 시퀀스는 OK지만 커버리지 축이 잡는다."""
    nurses = _pool(4)
    diag = multi_axis_diagnose(nurses, _cfg({"D": 2, "E": 2, "N": 1}), 31, 2026, 8)
    assert diag.status == INFEASIBLE
    assert diag.primary is not None and diag.primary.group_id.startswith("shift:")
    assert diag.primary.deficit > 0


def test_argmin_surplus_picks_most_severe_group():
    """여러 축 부족 시 deficit 최대(가장 빡센) 그룹을 primary 로."""
    nurses = _pool(4)
    diag = multi_axis_diagnose(nurses, _cfg({"D": 2, "E": 2, "N": 2}), 31, 2026, 8)
    assert diag.status == INFEASIBLE
    assert all(diag.primary.deficit >= c.deficit for c in diag.certificates)


def test_sequence_axis_still_certified():
    """N전담 banned 4연속 → 커버리지 아닌 시퀀스 축이 primary."""
    nurses = _pool(5, allowed=None)
    nurses[0] = {"nurse_id": "x", "name": "엑스", "grade": 1, "team_id": "A",
                 "allowed_shifts": ["N"]}
    diag = multi_axis_diagnose(
        nurses, _cfg({"D": 1, "E": 1, "N": 1},
                     forbidden={"x": {10: ["O"], 11: ["O"], 12: ["O"], 13: ["O"]}}),
        31, 2026, 8)
    assert diag.status == INFEASIBLE
    assert any(c.kind == "sequence_path_empty" for c in diag.certificates)


def test_render_names_the_tightest_group():
    diag = multi_axis_diagnose(_pool(4), _cfg({"D": 2, "E": 2, "N": 1}), 31, 2026, 8)
    txt = render_axis(diag)
    assert "빡센 그룹" in txt and "부족" in txt
