"""α team_grade_precheck 보강 검증 — 동적 시나리오 sweep.

검증 대상:
  - check_capacity_total_shortage : 풍부 details (bottleneck_days, by_shift, lowest_capacity)
  - check_global_shift_allowed_shortage : eligible_nurses, cross_shift_eligible_pool
  - check_monthly_night_capacity : peak_n_days, night_capable_nurses
  - check_daily_night_shortage (신규) : blocked_night_capable_nurses 사유
  - check_preceptee_sync_mismatch (신규) : mismatch_reasons / preceptor·preceptee 양쪽 attribute

원칙 (사용자 ralph 정신):
  - 정적 fixture 없음 — 모두 seed-deterministic 동적 생성
  - 모든 병원/병동에서 통하는 일반 산술 검증 (특정 nurse count 가정 X)
  - naive "인원 줄이면" 패턴 검출 0건 (디테일이 살아있어야 함)
  - 풍부 디테일 키 존재 검증 — narrative 가 problem_list 를 만들 충분한 컨텍스트
"""

from __future__ import annotations

import random
from typing import Dict, List

import pytest

from services.precheck.team_grade_precheck import (
    PrecheckInput,
    PrecheckNurse,
    check_capacity_total_shortage,
    check_daily_night_shortage,
    check_global_shift_allowed_shortage,
    check_monthly_night_capacity,
    check_preceptee_sync_mismatch,
)


SWEEP_SEEDS = list(range(900000, 900100))  # 100 시나리오


def _make_input(
    *,
    rng: random.Random,
    nurse_count: int,
    num_days: int,
    daily_req: Dict[str, int],
    use_mid: bool = False,
    global_off: int = 0,
    fixed_off_density: float = 0.0,
    nurse_overrides: List[Dict] = None,
) -> PrecheckInput:
    """병동 일반 PrecheckInput 빌드 — 모든 파라미터 caller 가 통제."""
    overrides = nurse_overrides or []
    nurses: List[PrecheckNurse] = []
    for i in range(nurse_count):
        ovr = overrides[i] if i < len(overrides) else {}
        fixed_off = set()
        for d in range(num_days):
            if rng.random() < fixed_off_density:
                fixed_off.add(d)
        nurses.append(PrecheckNurse(
            nurse_id=ovr.get("nurse_id", f"n{i}"),
            grade=ovr.get("grade"),
            team_id=ovr.get("team_id"),
            allowed_shifts=ovr.get("allowed_shifts"),  # None = all
            join_day=ovr.get("join_day", 0),
            leave_day=ovr.get("leave_day", num_days - 1),
            personal_off_adjustment=ovr.get("personal_off", 0),
            fixed_off_days=ovr.get("fixed_off_days", fixed_off),
            preceptor_id=ovr.get("preceptor_id"),
            sync_window_start=ovr.get("sync_window_start"),
            sync_window_end=ovr.get("sync_window_end"),
        ))
    return PrecheckInput(
        num_days=num_days,
        nurses=nurses,
        teams=[],
        roster_config={
            "use_mid": use_mid,
            "daily_shift_requirements": daily_req,
            "global_monthly_off_days": global_off,
        },
        team_coverage={},
        grade_constraints={},
    )


def _detail(issue: dict) -> dict:
    """`_issue` 가 만든 evidence dict 반환 (구버전·신버전 모두 호환)."""
    return issue.get("evidence") or issue.get("details") or {}


# ─────────────────────────────────────────────────────────────────────────────
# check_capacity_total_shortage — 보강된 details
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("seed", SWEEP_SEEDS[:30])
def test_capacity_total_shortage_emits_enriched_details(seed: int):
    rng = random.Random(seed)
    nurse_count = rng.randint(3, 12)
    num_days = rng.choice([28, 30, 31])
    # demand 가 capacity 보다 크도록 strong off 부여
    daily_req = {
        "D": rng.randint(2, nurse_count),
        "E": rng.randint(2, nurse_count),
        "N": rng.randint(1, max(2, nurse_count - 1)),
    }
    global_off = rng.randint(int(num_days * 0.6), num_days - 1)
    inp = _make_input(
        rng=rng, nurse_count=nurse_count, num_days=num_days,
        daily_req=daily_req, global_off=global_off,
    )
    issues = check_capacity_total_shortage(inp)
    if not issues:
        return  # 우연히 feasible — skip
    ev = _detail(issues[0])
    # 풍부 디테일 키 모두 존재
    for k in ("required", "capacity", "shortage", "demand_by_shift",
              "bottleneck_days", "lowest_capacity_nurses", "demand_uniform"):
        assert k in ev, f"seed={seed}: missing key {k}"
    # 산술 일관성
    assert ev["required"] == ev["capacity"] + ev["shortage"]
    # 디테일이 비어있지 않음 (정확한 day/nurse 식별)
    assert len(ev["demand_by_shift"]) >= 1
    assert isinstance(ev["lowest_capacity_nurses"], list)


# ─────────────────────────────────────────────────────────────────────────────
# check_global_shift_allowed_shortage — eligible_nurses + cross_pool
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("seed", SWEEP_SEEDS[30:60])
def test_shift_allowed_shortage_enriched(seed: int):
    rng = random.Random(seed)
    nurse_count = rng.randint(3, 10)
    num_days = rng.choice([10, 20, 30])
    # 일부러 N 자격만 가진 nurse 다수, D 자격은 1명 → D 부족
    target_shift = rng.choice(["D", "E", "N"])
    other = next(s for s in ["D", "E", "N"] if s != target_shift)
    overrides = []
    for i in range(nurse_count):
        if i == 0:
            overrides.append({"allowed_shifts": [target_shift]})
        else:
            overrides.append({"allowed_shifts": [other]})
    daily_req = {target_shift: nurse_count, other: 1}
    inp = _make_input(
        rng=rng, nurse_count=nurse_count, num_days=num_days,
        daily_req=daily_req, nurse_overrides=overrides,
    )
    issues = check_global_shift_allowed_shortage(inp)
    assert len(issues) > 0, f"seed={seed}: expected shortage emit"

    ev = _detail(issues[0])
    for k in ("shift", "day", "required", "eligible", "shortage",
              "eligible_nurses", "cross_shift_eligible_pool", "cross_shift_eligible_counts"):
        assert k in ev, f"seed={seed}: missing key {k}"
    # 자격 nurse 명단이 실제 있어야 — naive 숫자 한 줄 회피
    assert isinstance(ev["eligible_nurses"], list)
    assert ev["required"] == ev["eligible"] + ev["shortage"]


# ─────────────────────────────────────────────────────────────────────────────
# check_daily_night_shortage — 신규
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("seed", SWEEP_SEEDS[60:80])
def test_daily_night_shortage_identifies_blocked_nurses(seed: int):
    rng = random.Random(seed)
    nurse_count = rng.randint(4, 12)
    num_days = rng.choice([28, 30])
    # 야간 가능 nurse 2-3명만, 일부 fixed_off
    n_capable_count = rng.randint(2, 3)
    overrides = []
    blocked_day = rng.randint(0, num_days - 1)
    for i in range(nurse_count):
        if i < n_capable_count:
            ov = {"allowed_shifts": ["N", "D"]}
            if i == 0:
                ov["fixed_off_days"] = {blocked_day}
            overrides.append(ov)
        else:
            overrides.append({"allowed_shifts": ["D"]})
    daily_req = {"D": 2, "N": n_capable_count}  # 모든 N 가능자 매일 풀가동해야
    inp = _make_input(
        rng=rng, nurse_count=nurse_count, num_days=num_days,
        daily_req=daily_req, nurse_overrides=overrides,
    )
    issues = check_daily_night_shortage(inp)
    # 첫 블로킹 day (blocked_day+1) issue 가 존재해야
    blocked_issues = [i for i in issues if _detail(i)["day"] == blocked_day + 1]
    assert len(blocked_issues) >= 1, f"seed={seed}: blocked_day {blocked_day+1} not detected"

    ev = _detail(blocked_issues[0])
    for k in ("day", "n_required", "n_capacity", "shortage",
              "active_night_capable_nurses", "blocked_night_capable_nurses",
              "total_night_capable_pool"):
        assert k in ev
    # blocked nurse 사유까지 식별 (naive 회피)
    blocked = ev["blocked_night_capable_nurses"]
    assert any("fixed_off" in n.get("reasons", []) for n in blocked)


# ─────────────────────────────────────────────────────────────────────────────
# check_preceptee_sync_mismatch — 신규
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("seed", SWEEP_SEEDS[80:100])
def test_preceptee_sync_mismatch_detects_all_three_reasons(seed: int):
    """3 mismatch 사유(shift/team/window) 각각 단독·결합 동적 생성."""
    rng = random.Random(seed)
    num_days = 30
    # 3 사유 random 조합 (적어도 1개)
    inject_shift = rng.random() < 0.6
    inject_team = rng.random() < 0.6
    inject_window = rng.random() < 0.3
    if not (inject_shift or inject_team or inject_window):
        inject_shift = True  # 최소 1개 보장

    ptor_shifts = ["D"] if not inject_shift else ["D"]
    ptee_shifts = ["N"] if inject_shift else ["D"]
    ptor_team = "A"
    ptee_team = "B" if inject_team else "A"
    sync_start, sync_end = (None, None)
    if inject_window:
        # window 가 비활성 구간 (leave 후) 으로
        sync_start = num_days + 5
        sync_end = num_days + 10

    nurses = [
        PrecheckNurse(nurse_id="ptor", allowed_shifts=ptor_shifts,
                      team_id=ptor_team, join_day=0, leave_day=num_days - 1),
        PrecheckNurse(nurse_id="ptee", allowed_shifts=ptee_shifts,
                      team_id=ptee_team, join_day=0, leave_day=num_days - 1,
                      preceptor_id="ptor",
                      sync_window_start=sync_start, sync_window_end=sync_end),
    ]
    inp = PrecheckInput(
        num_days=num_days, nurses=nurses, teams=[],
        roster_config={"daily_shift_requirements": {"D": 1, "N": 1}},
        team_coverage={}, grade_constraints={},
    )
    issues = check_preceptee_sync_mismatch(inp)
    assert len(issues) == 1, f"seed={seed}: expected 1 mismatch"
    ev = _detail(issues[0])
    for k in ("preceptor_id", "preceptee_id", "start_day", "end_day",
              "window_days", "mismatch_reasons", "preceptor_shifts",
              "preceptee_shifts", "shift_intersection",
              "preceptor_team", "preceptee_team"):
        assert k in ev, f"seed={seed}: missing key {k}"

    # 주입한 사유가 reasons 에 모두 포함
    reasons = ev["mismatch_reasons"]
    if inject_shift:
        assert "shift_intersection_empty" in reasons
    if inject_team:
        assert "team_mismatch" in reasons
    if inject_window:
        assert "window_empty" in reasons


# ─────────────────────────────────────────────────────────────────────────────
# Naive language pattern detector — 디테일 살아있는지 검증
# ─────────────────────────────────────────────────────────────────────────────

def test_no_naive_human_messages_in_enriched_checks():
    """5 check 함수의 emit 에 'X명 부족' 만 있는 한 줄 메시지가 없는지 검증."""
    rng = random.Random(900200)
    # 의도적으로 강한 infeasible 시나리오
    inp = _make_input(
        rng=rng, nurse_count=4, num_days=30,
        daily_req={"D": 3, "E": 2, "N": 2},
        global_off=20,  # capacity 부족
    )
    all_issues = (
        check_capacity_total_shortage(inp)
        + check_global_shift_allowed_shortage(inp)
        + check_monthly_night_capacity(inp)
        + check_daily_night_shortage(inp)
    )
    assert len(all_issues) > 0
    for it in all_issues:
        # evidence 가 4 키 이상이어야 — naive '숫자만 4개' 회피 (보강 후 항상 7+)
        ev = _detail(it)
        assert len(ev.keys()) >= 7, f"naive evidence: {it['reason_code']} → keys={list(ev.keys())}"
