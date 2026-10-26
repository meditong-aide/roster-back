"""EI-10g1 — 배정 차단일(blocked_by_nurse_id)이 precheck 용량·개인 DP 에 들어간다. DB 인자가 없는 순수 함수만 부른다(쿼리 0)."""

from services.precheck.per_nurse_sequence_feasibility import nurse_sequence_infeasible
from services.precheck.runtime_bridge import build_precheck_input
from services.precheck.team_grade_precheck import PrecheckNurse, _active, _span, _working_capacity


def _nd(nid="N01", **over):
    row = {"nurse_id": nid, "name": nid, "allowed_shifts": ["D"], "is_weekend_off": False, "joining_date": "2024-03-01T00:00:00"}
    row.update(over)
    return row


def _build(blocked, nurses=None, **cfg):
    config = {"off_first": True, "daily_shift_requirements": {"D": 1, "E": 0, "N": 0}, "blocked_by_nurse_id": blocked, **cfg}
    return build_precheck_input(nurses_dict=nurses or [_nd()], config_dict=config, grade_config=None, fixed_cells=None,
                                year=2026, month=11)


def test_blocked_days_are_only_the_days_inside_the_active_span():
    """퇴사로 구간이 0~18 인 간호사 — 구간 밖 차단일(25일)은 용량에서 빼지 않는다."""
    inp = _build({"N01": {3, 4, 25}}, [_nd(resignation_date="2026-11-20T00:00:00")])
    nurse = inp.nurses[0]
    assert nurse.blocked_days == frozenset({3, 4})
    assert (nurse.join_day, nurse.leave_day) == (0, 18)
    assert not _active(nurse, 3) and _active(nurse, 5) and _span(nurse) == 17


def test_without_blocked_map_nothing_changes():
    for blocked in (None, {}, {"OTHER": {1}}):
        nurse = _build(blocked).nurses[0]
        assert nurse.blocked_days == frozenset() and _span(nurse) == 30


def test_capacity_drops_by_blocked_days_but_the_closed_form_uses_the_calendar_span():
    """차단일 12일 · 연속근무 상한 6 — 용량 = min(30−12, 30−30//7) = 18. 상한 닫힌형은 달력 구간으로 센다(차단일이 휴식을 대신하므로)."""
    inp = _build({"N01": set(range(5, 17))}, max_conseq_work=6)
    assert _working_capacity(inp.nurses[0], inp.roster_config) == 18
    inp = _build({}, max_conseq_work=6)
    assert _working_capacity(inp.nurses[0], inp.roster_config) == 26


def test_min_off_floor_uses_the_engine_avail_days_including_out_of_span_blocked_days():
    """엔진은 구간 밖 차단일도 `len()` 으로 센다(OFF 하한 avail_days) — precheck 하한이 그보다 커지면 오차단이다."""
    base = _build({}, off_first=False, off_days=8).nurses[0].min_off_required
    blocked = _build({"N01": set(range(5, 17))}, off_first=False, off_days=8).nurses[0].min_off_required
    assert blocked < base


def test_blocked_day_overlapping_a_weekend_is_not_counted_as_a_hard_off_twice():
    """주말휴무자의 주말이 차단일과 겹치면 하드 O 날에서 뺀다(차단일은 용량에서 이미 빠진다)."""
    both = _build({"N01": {6, 7}}, [_nd(is_weekend_off=True)]).nurses[0]
    plain = _build({}, [_nd(is_weekend_off=True)]).nurses[0]
    assert plain.personal_off_adjustment - both.personal_off_adjustment == 2


def test_dp_blocked_day_rests_and_releases_the_single_night_requirement():
    """개인 DP — 차단일은 쉬는 날이다. 1N 금지에서 N(3일) 바로 뒤 차단일(4일)이어도 다음 날 N 요구가 풀려 불가능이 아니다."""
    kwargs = {"num_days": 8, "allowed": {"D", "E", "N"}, "fixed": {3: "N"}, "not_one_night": True}
    assert nurse_sequence_infeasible(**kwargs) is False  # 4·5일 N 으로 이어 갈 수 있다
    assert nurse_sequence_infeasible(**kwargs, blocked={4, 2}) is False
    # 차단일은 연속근무를 끊는다 — 연속근무 상한 2 에서 고정 D 3칸(0·1·2)은 불가능, 2일이 차단일이면 그 칸이 쉬는 날이라 가능
    fixed = {0: "D", 1: "D", 2: "D"}
    assert nurse_sequence_infeasible(num_days=5, allowed={"D"}, fixed=fixed, max_consecutive_work=2) is True
    assert nurse_sequence_infeasible(num_days=5, allowed={"D"}, fixed=fixed, max_consecutive_work=2, blocked={2}) is False


def test_precheck_nurse_default_has_no_blocked_days():
    assert PrecheckNurse(nurse_id="X").blocked_days == frozenset()
