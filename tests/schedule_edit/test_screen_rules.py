"""화면 위반 판정 이식 검증 — DB 없음.

기대값은 프론트 useRosterValidation.ts·shift-util.ts(f6692a42)를 손으로 따라가 계산했다.
좌표는 화면과 같은 0 기준(1일=0, 전월 꼬리=음수). 달은 2026-02(28일).
"""
from __future__ import annotations

from services.schedule_edit.screen_rules import (
    ScreenInputs,
    ScreenViolation as V,
    ShiftDef,
    _parse_manpower,
    new_screen_violations,
    screen_violations,
)

DAYS = 28
SHIFTS = [
    ShiftDef(1, "D", "D", "데이", "근무", 1),
    ShiftDef(2, "E", "E", "이브닝", "근무", 2),
    ShiftDef(3, "N", "N", "나이트", "근무", 3),
    ShiftDef(4, "M", "M", "미드", "근무", 4),
    ShiftDef(5, "O", "O", "O", "휴무", 5),
    ShiftDef(6, "주", "주", "O", "휴무", 6),
    # 하위코드: default_shift 없음 + shift_gb 데이 → 데이 묶음 대표 'D'
    ShiftDef(7, "Dㅇ", None, "데이", "근무", 7),
    # shift_gb 없음 → etc 묶음(대표 문자 없음)
    ShiftDef(8, "연", None, None, "휴무", 8),
]


def month(*head: str) -> list:
    return list(head) + ["-"] * (DAYS - len(head))


def inputs(**overrides) -> ScreenInputs:
    base = dict(year=2026, month=2, shifts=SHIFTS, manpower_monthly={})
    base.update(overrides)
    return ScreenInputs(**base)


def short(day: int, shift: str, required: int, actual: int) -> V:
    return V(type="manpower_shortage", day=day, shift=shift, required=required, actual=actual)


# ── 근무코드 해석 ─────────────────────────────────────────────────────────────

def test_sub_code_counts_toward_shift_gb_group_letter():
    inp = inputs(manpower_monthly={"D": 0}, manpower_daily={"D": [1] + [0] * 27})
    assert screen_violations({"a": month("Dㅇ")}, None, inp) == []
    # 모르는 코드는 첫 글자의 대표 근무로(레거시 폴백) — 'Dx' → D
    assert screen_violations({"a": month("Dx")}, None, inp) == []
    # '연' 은 묶음 대표도 default_shift 도 없어 어느 근무로도 안 센다
    assert screen_violations({"a": month("연")}, None, inp) == [short(0, "D", 1, 0)]


def test_numeric_id_takes_priority_over_code():
    z_shifts = SHIFTS + [
        ShiftDef(30, "Z", None, "나이트", "근무", 30),
        ShiftDef(31, "Z", None, None, "휴무", 31),
    ]
    inp = inputs(shifts=z_shifts, config={"max_conseq_work": 2})
    cells = {"a": month("D", "D", "Z", "D", "O")}
    # 코드만: 'Z' → shift_id 첫 행(30, 근무) → D D Z D 4연속, O 에서 닫힘
    assert screen_violations(cells, None, inp) == [
        V(type="max_conseq_work", nurse_id="a", max=2, actual=4, start_day=0, end_day=3)]
    # 숫자 id 31(휴무)은 칸 코드 'Z' 와 호환 → id 행 채택 → D D 에서 끊겨 위반 없음
    assert screen_violations(cells, {"a": [1, 1, 31, 1, 5] + [None] * 23}, inp) == []


def test_incompatible_numeric_id_falls_back_to_code():
    # 칸 코드 'O' 인데 id 는 D 행 → 호환 안 됨 → 코드(O, 휴무)로. id 를 썼다면 D D 2연속 위반
    inp = inputs(config={"max_conseq_work": 1})
    assert screen_violations({"a": month("D", "O")}, {"a": [1, 1] + [None] * 26}, inp) == []


# ── 최소·최대 인원 ────────────────────────────────────────────────────────────

def test_daily_min_and_max_manpower():
    daily = {"D": [2, 1] + [0] * 26, "D_max": [0, 1] + [0] * 26}
    monthly = {"D": 1, "D_max": 0}
    cells = {"a": month("D", "D"), "b": month("-", "D")}
    excess = V(type="manpower_excess", day=1, shift="D", required_max=1, actual=2)
    on = inputs(manpower_monthly=monthly, manpower_daily=daily,
                use_max_manpower=True, enable_max_manpower_violation=True)
    assert screen_violations(cells, None, on) == [short(0, "D", 2, 1), excess]
    # 화면 상수 ENABLE_MAX_MANPOWER_VIOLATION=false 가 기본 → 초과는 안 뜬다
    screen = inputs(manpower_monthly=monthly, manpower_daily=daily, use_max_manpower=True)
    assert screen_violations(cells, None, screen) == [short(0, "D", 2, 1)]
    # 병동 max_enabled 가 꺼져 있으면 초과 없음
    off = inputs(manpower_monthly=monthly, manpower_daily=daily,
                 use_max_manpower=False, enable_max_manpower_violation=True)
    assert screen_violations(cells, None, off) == [short(0, "D", 2, 1)]


def test_monthly_value_used_when_daily_missing():
    inp = inputs(manpower_monthly={"E": 1})
    cells = {"a": ["E"] * 5 + ["O"] + ["E"] * 22}
    assert screen_violations(cells, None, inp) == [short(5, "E", 1, 0)]


def test_mid_validated_unless_use_mid_false():
    base = dict(manpower_monthly={"D": 0, "M": 0}, manpower_daily={"M": [1] + [0] * 27})
    cells = {"a": month("D")}
    assert screen_violations(cells, None, inputs(use_mid=False, **base)) == []
    assert screen_violations(cells, None, inputs(use_mid=True, **base)) == [short(0, "M", 1, 0)]
    # use_mid 를 모르면(None) 화면처럼 M 도 검사한다
    assert screen_violations(cells, None, inputs(use_mid=None, **base)) == [short(0, "M", 1, 0)]
    assert screen_violations({"a": month("M")}, None, inputs(use_mid=True, **base)) == []


# ── 연속 근무 ─────────────────────────────────────────────────────────────────

def test_max_conseq_work_with_prev_tail_and_month_end():
    inp = inputs(config={"max_conseq_work": 3}, tail_days=[28, 29, 30, 31],
                 prev_tail={"a": [None, None, "D", "D"], "c": ["D", "D", "D", "D"]})
    cells = {"a": month("D", "D", "O"), "b": ["-"] * 24 + ["D"] * 4, "c": month("O")}
    assert screen_violations(cells, None, inp) == [
        V(type="max_conseq_work", nurse_id="a", max=3, actual=4, start_day=-2, end_day=1),
        V(type="max_conseq_work", nurse_id="b", max=3, actual=4, start_day=24, end_day=27),
        # 전월 꼬리 안에서 끝난 구간도 화면은 낸다(월 경계 검사 없음)
        V(type="max_conseq_work", nurse_id="c", max=3, actual=4, start_day=-4, end_day=-1),
    ]


def test_max_conseq_work_null_compares_as_zero():
    inp = inputs(config={"max_conseq_work": None})
    assert screen_violations({"a": month("D", "O")}, None, inp) == [
        V(type="max_conseq_work", nurse_id="a", max=None, actual=1, start_day=0, end_day=0)]


# ── N 연속 ────────────────────────────────────────────────────────────────────

def test_three_seq_nig_first_only_and_all_runs():
    cells = {"a": month("N", "N", "N", "O", "O", "N", "N", "N", "O")}
    inp = inputs(config={"three_seq_nig": False})
    first = V(type="three_seq_nig", nurse_id="a", start_day=0, end_day=2)
    assert screen_violations(cells, None, inp) == [first]
    assert screen_violations(cells, None, inp, all_runs=True) == [
        first, V(type="three_seq_nig", nurse_id="a", start_day=5, end_day=7)]


def test_three_seq_nig_true_catches_four_and_windows_overlap():
    cells = {"b": month("N", "N", "N", "N", "O")}
    assert screen_violations(cells, None, inputs(config={"three_seq_nig": True})) == [
        V(type="three_seq_nig", nurse_id="b", start_day=0, end_day=3)]
    assert screen_violations(cells, None, inputs(config={"three_seq_nig": False}), all_runs=True) == [
        V(type="three_seq_nig", nurse_id="b", start_day=0, end_day=2),
        V(type="three_seq_nig", nurse_id="b", start_day=1, end_day=3),
    ]


def test_three_seq_nig_ignores_run_only_in_prev_tail():
    inp = inputs(config={"three_seq_nig": False}, tail_days=[29, 30, 31],
                 prev_tail={"a": ["N", "N", "N"]})
    cells = {"a": month("O", "N", "N", "N", "O")}
    assert screen_violations(cells, None, inp) == [
        V(type="three_seq_nig", nurse_id="a", start_day=1, end_day=3)]


# ── ED/ND/NE ─────────────────────────────────────────────────────────────────

def test_banned_day_after_eve_turns_on_ed_nd_ne():
    cells = {"a": month("E", "Dㅇ", "N", "D", "N", "E", "O")}
    inp = inputs(config={"banned_day_after_eve": True, "three_seq_nig": True})
    assert screen_violations(cells, None, inp) == [
        V(type="banned_day_after_eve", nurse_id="a", day=0, next_day=1, pattern="ED"),
        V(type="banned_day_after_eve", nurse_id="a", day=2, next_day=3, pattern="ND"),
        V(type="banned_day_after_eve", nurse_id="a", day=4, next_day=5, pattern="NE"),
    ]
    assert screen_violations(cells, None, inputs(config={"banned_day_after_eve": False})) == []


def test_banned_pattern_across_month_boundary():
    inp = inputs(config={"banned_day_after_eve": True, "three_seq_nig": True},
                 tail_days=[31], prev_tail={"a": ["E"]})
    assert screen_violations({"a": month("D")}, None, inp) == [
        V(type="banned_day_after_eve", nurse_id="a", day=-1, next_day=0, pattern="ED")]


# ── N 뒤 OFF 2일 ─────────────────────────────────────────────────────────────

def test_three_nights_rule_suppresses_two_nights_rule():
    cells = {"a": month("N", "N", "N", "O", "D")}
    both = inputs(config={"two_offs_after_three_nig": True, "two_offs_after_two_nig": True,
                          "three_seq_nig": True})
    assert screen_violations(cells, None, both) == [
        V(type="two_offs_after_three_nig", nurse_id="a", start_day=0, end_day=4,
          off_start_day=3, off_end_day=4)]
    # 2N2O 만 켜면 런 안의 시작일마다 따로 본다: (0,1)→2·3일 / (1,2)→3·4일
    two_only = inputs(config={"two_offs_after_two_nig": True, "three_seq_nig": True})
    assert screen_violations(cells, None, two_only) == [
        V(type="two_offs_after_two_nig", nurse_id="a", start_day=0, end_day=3,
          off_start_day=2, off_end_day=3),
        V(type="two_offs_after_two_nig", nurse_id="a", start_day=1, end_day=4,
          off_start_day=3, off_end_day=4),
    ]


def test_off_check_skipped_near_month_end():
    inp = inputs(config={"two_offs_after_three_nig": True, "three_seq_nig": True})
    # 25~27일 N 3연속 → OFF 둘째 날(29일)이 달 밖 → 생략. 28일이 D 여도 잡지 않는다
    assert screen_violations({"a": ["-"] * 24 + ["N", "N", "N", "D"]}, None, inp) == []


def test_resigned_days_count_as_off_for_night_rule():
    inp = inputs(config={"two_offs_after_three_nig": True, "three_seq_nig": True},
                 resigned_days={"c": set(range(5, DAYS + 1))})
    # 4일 D 는 위반, 5일(퇴사)은 봐줘서 OFF 필요 범위가 4일 하루로 줄어든다
    assert screen_violations({"c": month("N", "N", "N", "D", "D")}, None, inp) == [
        V(type="two_offs_after_three_nig", nurse_id="c", start_day=0, end_day=3,
          off_start_day=3, off_end_day=3)]
    assert screen_violations({"c": month("N", "N", "N", "O", "D")}, None, inp) == []


# ── 월 N 상한 ─────────────────────────────────────────────────────────────────

def test_max_nig_per_month_counts_current_month_only():
    inp = inputs(config={"max_nig_per_month": 2, "three_seq_nig": True},
                 tail_days=[30, 31], prev_tail={"b": ["N", "N"]})
    cells = {"a": month("N", "O", "N", "O", "N"), "b": month("N", "O", "N")}
    assert screen_violations(cells, None, inp) == [
        V(type="max_nig_per_month", nurse_id="a", max=2, actual=3)]
    off = inputs(config={"max_nig_per_month": 0, "three_seq_nig": True})
    assert screen_violations(cells, None, off) == []


# ── 입력 전처리·검증 생략 ────────────────────────────────────────────────────

def test_null_cells_and_hidden_nurses_drop_cells():
    base = dict(manpower_monthly={"D": 0}, manpower_daily={"D": [1] + [0] * 27})
    cells = {"a": month("D"), "b": month("O")}
    assert screen_violations(cells, None, inputs(**base)) == []
    assert screen_violations(cells, None, inputs(null_cells={"a": {1}}, **base)) == [short(0, "D", 1, 0)]
    assert screen_violations(cells, None, inputs(hidden_nurses={"a"}, **base)) == [short(0, "D", 1, 0)]


def test_no_violation_when_screen_would_not_validate():
    rule = {"max_conseq_work": 0}
    cells = {"a": month("D", "O")}
    assert screen_violations(cells, None, inputs(config=rule)) == [
        V(type="max_conseq_work", nurse_id="a", max=0, actual=1, start_day=0, end_day=0)]
    # 일일 인력을 못 구함 · 유효 칸 없음 · 표에 남은 간호사 없음 → 화면은 아무것도 안 띄운다
    assert screen_violations(cells, None, inputs(config=rule, manpower_monthly=None)) == []
    assert screen_violations({"a": month()}, None, inputs(config=rule)) == []
    assert screen_violations(cells, None, inputs(config=rule, hidden_nurses={"a"})) == []


def test_parse_manpower_matches_screen_shape():
    resp = {
        "month_summary": {"D_count": 3, "E_count": 2, "N_count": 2, "M_count": 0,
                          "D_count_max": 4, "E_count_max": 0, "N_count_max": 0,
                          "M_count_max": 0, "max_enabled": True},
        "date": {"D_count": [3, None], "D_count_max": [4, 4]},
        "max_enabled": False,
    }
    monthly, daily, use_max = _parse_manpower(resp)
    assert monthly == {"D": 3, "E": 2, "N": 2, "M": 0, "D_max": 4, "E_max": 0, "N_max": 0, "M_max": 0}
    assert daily == {"D": [3, 0], "D_max": [4, 4]}  # Number(null) = 0
    # 바깥 max_enabled 가 false(널 아님)면 month_summary 값은 보지 않는다(?? 연산)
    assert use_max is False


# ── 새 위반 비교 ──────────────────────────────────────────────────────────────

def test_new_screen_violations_detects_moved_violation():
    inp = inputs(manpower_monthly={"D": 0}, manpower_daily={"D": [1, 1] + [0] * 26})
    before = screen_violations({"a": month("-", "D")}, None, inp)
    after = screen_violations({"a": month("D", "-")}, None, inp)
    assert before == [short(0, "D", 1, 0)]
    assert new_screen_violations(before, after) == [short(1, "D", 1, 0)]
    assert new_screen_violations(before, before) == []


def test_new_screen_violations_pattern_is_part_of_key():
    inp = inputs(config={"banned_day_after_eve": True, "three_seq_nig": True})
    before = screen_violations({"a": month("E", "D")}, None, inp)
    after = screen_violations({"a": month("N", "D")}, None, inp)
    assert new_screen_violations(before, after) == [
        V(type="banned_day_after_eve", nurse_id="a", day=0, next_day=1, pattern="ND")]


def test_new_screen_violations_key_ignores_counts():
    # 월 N 상한 위반이 3회→4회로 늘어도 같은 위반(같은 열쇠)이다
    inp = inputs(config={"max_nig_per_month": 2, "three_seq_nig": True})
    before = screen_violations({"a": month("N", "O", "N", "O", "N")}, None, inp)
    after = screen_violations({"a": month("N", "O", "N", "O", "N", "O", "N")}, None, inp)
    assert [v.actual for v in before + after] == [3, 4]
    assert new_screen_violations(before, after) == []
