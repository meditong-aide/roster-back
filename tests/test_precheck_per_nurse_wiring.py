"""per-nurse 시퀀스 feasibility 가 run_precheck 에 배선돼 blocking 으로 잡히는지."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from services.precheck.payload import has_blocking_issues  # noqa: E402
from services.precheck.team_grade_precheck import (  # noqa: E402
    PrecheckInput,
    PrecheckNurse,
    run_precheck,
)

CODE = "PER_NURSE_SEQUENCE_INFEASIBLE"


def _inp(fixed, cfg_extra=None):
    cfg = {"daily_shift_requirements": {"D": 1, "E": 1, "N": 1}}
    cfg.update(cfg_extra or {})
    nurse = PrecheckNurse(
        nurse_id="n1", join_day=0, leave_day=6, allowed_shifts=None,
        fixed_shift_assignments=fixed,
    )
    return PrecheckInput(
        num_days=7, nurses=[nurse], teams=[], roster_config=cfg,
        team_coverage={}, grade_constraints={},
    )


def _codes(res):
    return {i["reason_code"] for i in res["issues"]}


def test_fixed_n_then_d_conflict_blocks():
    # 고정 N(5일) 다음날 고정 D(6일) + N→D 금지(기본 True) → 개인 단독 불가능
    res = run_precheck(_inp({5: "N", 6: "D"}, {"ban_n_to_d": True}))
    assert CODE in _codes(res)
    assert has_blocking_issues(res)  # blocking 확정


def test_conflict_gone_when_ban_off():
    res = run_precheck(_inp({5: "N", 6: "D"}, {"ban_n_to_d": False}))
    assert CODE not in _codes(res)


def test_no_fixed_no_issue():
    # 고정셀 없으면 all-OFF 완성 가능 → 개인축 이슈 없음
    res = run_precheck(_inp({}))
    assert CODE not in _codes(res)


def test_e_then_d_conflict_blocks():
    # 고정 E→D + E→D 금지(banned_day_after_eve 기본 True)
    res = run_precheck(_inp({2: "E", 3: "D"}))
    assert CODE in _codes(res)
    assert has_blocking_issues(res)
