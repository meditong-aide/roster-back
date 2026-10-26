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


def test_fixed_n_then_fixed_d_is_exempt_like_engine():
    # 고정 N(5일)·고정 D(6일) — 두 칸 다 고정이면 엔진은 N→D 금지를 걸지 않는다(EI-10c)
    res = run_precheck(_inp({5: "N", 6: "D"}, {"ban_n_to_d": True}))
    assert CODE not in _codes(res)


def test_forced_night_then_fixed_day_blocks():
    # 1N 금지 + 3일 고정 O·4일 고정 N → 5일 N 강제(자유칸) → 6일 고정 D 와 N→D 금지 충돌 → 개인 단독 불가능
    res = run_precheck(_inp({3: "O", 4: "N", 6: "D"}, {"ban_n_to_d": True, "ban_single_night": True}))
    assert CODE in _codes(res)
    assert has_blocking_issues(res)  # blocking 확정


def test_conflict_gone_when_ban_off():
    res = run_precheck(_inp({3: "O", 4: "N", 6: "D"}, {"ban_n_to_d": False, "ban_single_night": True}))
    assert CODE not in _codes(res)


def test_no_fixed_no_issue():
    # 고정셀 없으면 all-OFF 완성 가능 → 개인축 이슈 없음
    res = run_precheck(_inp({}))
    assert CODE not in _codes(res)


def test_fixed_e_then_fixed_d_is_exempt_like_engine():
    # 고정 E→고정 D — 엔진은 두 칸 다 고정이면 E→D 금지(ban_shift_transitions 기본 True)를 면제한다(EI-10c)
    res = run_precheck(_inp({2: "E", 3: "D"}))
    assert CODE not in _codes(res)
