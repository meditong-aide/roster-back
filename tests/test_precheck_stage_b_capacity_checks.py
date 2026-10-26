"""EI-10g5 보강(Dr) — 단계 B 금지 코드가 일자·월 용량 검사 각각에서 공급을 정확히 1 줄이는가 · OFF 하한은 합이 아니라 max."""

from services.precheck.runtime_bridge import build_precheck_input
from services.precheck.team_grade_precheck import (
    PrecheckInput,
    PrecheckNurse,
    _hard_off_floor,
    _off_floor_with_forced,
    _working_capacity,
    check_daily_night_shortage,
    check_global_shift_allowed_shortage,
    check_grade_antipair_forces_shortage,
    check_grade_max_sum_below_need,
    check_grade_min_available_shortage,
    check_monthly_night_capacity,
    check_per_nurse_sequence,
    check_team_shift_allowed_shortage,
)

ND = 30
BAN_DAY = 5


def _nurse(nid, *, ban=None, ban_days=(BAN_DAY,), **kw):
    forbidden = {d: frozenset({ban}) for d in ban_days} if ban else {}
    return PrecheckNurse(nurse_id=nid, join_day=0, leave_day=ND - 1, forbidden_by_day=forbidden, **kw)


def _inp(nurses, need, *, team_coverage=None, grade_constraints=None, **cfg):
    return PrecheckInput(
        num_days=ND, nurses=nurses, teams=[], roster_config={"daily_shift_requirements": need, **cfg},
        team_coverage=team_coverage or {}, grade_constraints=grade_constraints or {})


def _days(issues):
    return sorted({i["evidence"]["day"] for i in issues})


def test_global_shift_allowed_sees_a_banned_code_on_that_day_only():
    need = {"D": 2, "E": 0, "N": 0}
    assert check_global_shift_allowed_shortage(_inp([_nurse("A"), _nurse("B")], need)) == []
    issues = check_global_shift_allowed_shortage(_inp([_nurse("A", ban="D"), _nurse("B")], need))
    assert _days(issues) == [BAN_DAY + 1] and issues[0]["evidence"]["eligible"] == 1


def test_team_shift_allowed_sees_a_banned_code():
    need = {"D": 0, "E": 0, "N": 0}
    cov = {1: {"D": 2}}
    ok = [_nurse("A", team_id=1), _nurse("B", team_id=1)]
    assert check_team_shift_allowed_shortage(_inp(ok, need, team_coverage=cov)) == []
    bad = [_nurse("A", team_id=1, ban="D"), _nurse("B", team_id=1)]
    assert _days(check_team_shift_allowed_shortage(_inp(bad, need, team_coverage=cov))) == [BAN_DAY]


def test_grade_max_sum_below_need_counts_only_unbanned_free_nurses():
    need, gc = {"D": 2, "E": 0, "N": 0}, {"max_by_shift": {"D": {1: 0}}}
    ok = [_nurse("A", grade=2), _nurse("B", grade=2)]
    assert check_grade_max_sum_below_need(_inp(ok, need, grade_constraints=gc)) == []
    bad = [_nurse("A", grade=2, ban="D"), _nurse("B", grade=2)]
    assert _days(check_grade_max_sum_below_need(_inp(bad, need, grade_constraints=gc))) == [BAN_DAY]


def test_grade_min_available_counts_only_unbanned_members_of_the_grade():
    need, gc = {"D": 0, "E": 0, "N": 0}, {"minimum_by_shift": {"D": {1: 2}}}
    ok = [_nurse("A", grade=1), _nurse("B", grade=1)]
    assert check_grade_min_available_shortage(_inp(ok, need, grade_constraints=gc)) == []
    bad = [_nurse("A", grade=1, ban="D"), _nurse("B", grade=1)]
    assert _days(check_grade_min_available_shortage(_inp(bad, need, grade_constraints=gc))) == [BAN_DAY]


def test_grade_antipair_counts_only_unbanned_non_grade_nurses():
    need, gc = {"D": 2, "E": 0, "N": 0}, {"max_by_shift": {"D": {1: 0}}}
    ok = [_nurse("A", grade=2), _nurse("B", grade=2)]
    assert check_grade_antipair_forces_shortage(_inp(ok, need, grade_constraints=gc)) == []
    bad = [_nurse("A", grade=2, ban="D"), _nurse("B", grade=2)]
    assert _days(check_grade_antipair_forces_shortage(_inp(bad, need, grade_constraints=gc))) == [BAN_DAY]


def test_daily_night_shortage_sees_a_banned_night():
    need = {"D": 0, "E": 0, "N": 2}
    assert check_daily_night_shortage(_inp([_nurse("A"), _nurse("B")], need)) == []
    issues = check_daily_night_shortage(_inp([_nurse("A", ban="N"), _nurse("B")], need))
    assert _days(issues) == [BAN_DAY + 1]
    assert issues[0]["evidence"]["blocked_night_capable_nurses"][0]["reasons"] == ["forbidden_night"]


def test_monthly_night_capacity_uses_the_available_axis_from_banned_nights():
    need = {"D": 0, "E": 0, "N": 1}            # 월 야간 30
    ok = [_nurse("A"), _nurse("B")]
    assert check_monthly_night_capacity(_inp(ok, need)) == []
    banned = range(20)                          # 각자 야간 가능일 10 → 공급 20 < 30
    bad = [_nurse("A", ban="N", ban_days=banned), _nurse("B", ban="N", ban_days=banned)]
    assert check_monthly_night_capacity(_inp(bad, need)) != []


def test_per_nurse_dp_runs_when_only_off_is_banned_without_any_fixed_cell():
    """고정 칸이 없어도 모든 날 O 금지(강제 OFF 아님)면 연속근무 상한 때문에 혼자 완성이 불가능 — DP 를 건너뛰면 안 된다."""
    cfg = {"max_conseq_work": 5, "daily_shift_requirements": {"D": 1, "E": 0, "N": 0}}
    inp = PrecheckInput(num_days=ND, nurses=[_nurse("A", ban="O", ban_days=range(ND), allowed_shifts=["D"])], teams=[],
                        roster_config=cfg, team_coverage={}, grade_constraints={})
    assert [i["evidence"]["nurse_id"] for i in check_per_nurse_sequence(inp)] == ["A"]
    inp.nurses = [_nurse("A", allowed_shifts=["D"])]
    assert check_per_nurse_sequence(inp) == []


def test_off_floor_is_max_of_floor_and_forced_off_not_their_sum():
    """바닥(min_off)과 강제 OFF 는 겹칠 수 있다 — 합치면 엔진보다 엄해져 오차단이다(max)."""
    nd = {"nurse_id": "N01", "name": "N01", "allowed_shifts": ["D"], "is_weekend_off": False, "joining_date": "2024-03-01T00:00:00"}
    cfg = {"off_first": False, "off_days": 8, "daily_shift_requirements": {"D": 1, "E": 0, "N": 0},
           "initial_constraints": {"forced_off": {"N01": [2, 3, 4, 5, 6, 7]}, "forbidden": {}}}
    inp = build_precheck_input(nurses_dict=[nd], config_dict=cfg, grade_config=None, fixed_cells=None, year=2026, month=11)
    n, rc = inp.nurses[0], inp.roster_config
    floor, forced = _hard_off_floor(n, rc), len(n.forced_off_days)
    assert forced == 6 and floor >= 6, "겹치는 입력이어야 max 와 합이 갈린다"
    assert _off_floor_with_forced(n, rc) == max(floor, forced) != floor + forced
    assert _working_capacity(n, rc) == 30 - max(floor, forced)
