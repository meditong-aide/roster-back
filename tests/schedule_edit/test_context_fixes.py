"""31장 결함 1~3 — 확정 원티드 날짜 이유 · 수정 엔진 설정 칸 · 묶음 근무코드(NK) · 반영 안 된 확정 원티드.

DB 는 쓰지 않는다 — 막힌 날짜 함수·설정 행 조회는 가짜로 바꾼다.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from services import preceptee_period, replacement_recommend_service
from services.schedule_edit.context import (
    FIXED_WANTED, GROUP_CODE_REASON, _extra_config, _load_ban_n_before, _load_blocked, _load_wishes,
    _note_group_codes, group_codes, with_wish_cells,
)
from services.schedule_edit.precheck import FIXED_WISH, apply_findings
from services.schedule_edit.solver import Candidate, cell_change
from tests.schedule_edit.test_solver import make_ctx

SHIFTS = [
    {"id": 1, "shift_id": "D", "default_shift": "D", "shift_gb": "데이", "type": "근무"},
    {"id": 2, "shift_id": "N", "default_shift": "N", "shift_gb": "나이트", "type": "근무"},
    {"id": 3, "shift_id": "NK", "default_shift": None, "shift_gb": "나이트", "type": "근무"},
    {"id": 4, "shift_id": "2N", "default_shift": None, "shift_gb": None, "type": "근무"},
    {"id": 5, "shift_id": "D2", "default_shift": None, "shift_gb": "고정", "type": "근무"},
    {"id": 6, "shift_id": "OFF", "default_shift": "O", "shift_gb": "O", "type": "휴무"},
]


def test_wanted_dates_keep_their_reason_and_dispatch_wins(monkeypatch):
    ctx = make_ctx({"A": "DDODO", "B": "DDDDD"})
    ctx.frozen[("A", 2)] = ctx.frozen[("A", 3)] = FIXED_WANTED       # 3일·4일 확정 원티드 · 4일·5일 파견
    dispatch = {"A": {date(2026, 11, 4), date(2026, 11, 5)}}
    wanted = {"A": {date(2026, 11, 3), date(2026, 11, 4)}}

    def blocked(db, group, year, month, days, include_fixed_wanted=True):
        out = {n: set(v) for n, v in dispatch.items()}
        if include_fixed_wanted:                                      # 대체 추천용은 원티드 날도 막는다
            for n, v in wanted.items():
                out.setdefault(n, set()).update(v)
        return out

    monkeypatch.setattr(replacement_recommend_service, "_build_assignment_blocked_dates", blocked)
    monkeypatch.setattr(preceptee_period, "resolve_preceptee_days_for_month", lambda *a: {})
    _load_blocked(None, ctx)
    assert ctx.frozen[("A", 2)] == FIXED_WANTED                       # '관할 밖'으로 덮지 않는다(결함 1)
    assert ctx.frozen[("A", 3)].startswith("관할 밖")                  # 파견 날이면 파견이 우선(사용자 결정 11-05)
    assert ctx.frozen[("A", 4)].startswith("관할 밖")
    ctx.config["ban_night_before_fixed_wanted_off"] = True
    _load_ban_n_before(ctx)
    assert ("A", 1) in ctx.ban_n_before                               # 원티드 OFF 전날 N 금지가 살아난다


class _FakeDB:
    def __init__(self, row):
        self.row = row

    def query(self, *_cols):
        return self

    def filter(self, *_args):
        return self

    def first(self):
        return self.row


def test_edit_engine_reads_settings_missing_from_the_shared_config_dict():
    assert _extra_config(_FakeDB((1, 4)), 77) == {"ban_night_before_fixed_wanted_off": True, "max_conseq_off": 4}
    assert _extra_config(_FakeDB((None, None)), 77) == {}             # 비면 기본값(`_effective_config`)
    assert _extra_config(_FakeDB(None), None) == {}


def test_group_codes_follow_the_screen_shift_gb_rule():
    assert group_codes(SHIFTS) == {"NK": "N"}                         # 묶음에 대표 N 이 있다 · 2N·D2 는 그대로


def _nk_ctx():
    ctx = make_ctx({"A": "NNOON", "B": "DDDOD", "C": "NOOOO"})
    ctx.raw["A"] = ["NK", "NK", "OFF", "OFF", "NK"]
    return ctx


def test_nk_is_just_n_and_only_its_saved_code_follows_the_person(caplog):
    # NK 는 계산상 그냥 N — 칸은 바꿀 수 있고, 근무 제한은 원래 간호사 설정만 따른다(우리가 D·E·M 금지를 덧붙이지 않는다).
    # 저장만 후처리: NK 는 N 의 하위코드 · 한 사람은 하나만 쓴다 — 그 사람이 NK 를 쓰면 그 사람의 N 은 NK, 다른 사람은 표준 N.
    # 섞여 있으면 데이터 이상 — 표준 N 으로 두고 로그만(사용자 결정 11-05)
    ctx = _nk_ctx()
    ctx.raw["B"], ctx.main["B"] = ["N", "N", "OFF", "OFF", "NK"], ["N", "N", "O", "O", "N"]   # B 는 N·NK 가 섞였다
    ctx.raw["C"] = ["NK", "OFF", "OFF", "OFF", "OFF"]                 # 나이트가 한 칸뿐이어도 그 코드
    with caplog.at_level("WARNING", logger="services.schedule_edit.context"):
        _note_group_codes(ctx, {"NK": "N"})
    assert ("A", 0) not in ctx.frozen and ctx.forbidden == {}
    assert ctx.keep_code == {"A": {"N": "NK"}, "C": {"N": "NK"}}
    assert cell_change(ctx, "A", 2, "N").after == "NK" and cell_change(ctx, "B", 3, "N").after == "N"
    assert [r.getMessage() for r in caplog.records if "섞여" in r.getMessage()] == [
        "[schedule_edit] 한 사람의 나이트 코드가 섞여 있음(표준 N 으로 저장): s1 B ['N', 'NK']"]


def test_other_group_codes_stay_fixed():
    ctx = make_ctx({"A": "DDOOD"})
    ctx.raw["A"] = ["DK", "D", "OFF", "OFF", "DK"]
    _note_group_codes(ctx, {"DK": "D"})
    assert ctx.frozen[("A", 0)] == GROUP_CODE_REASON and ("A", 1) not in ctx.frozen and ctx.keep_code == {}


def test_unreflected_fixed_wanted_becomes_a_wish_target_and_reflected_ones_are_not_skipped():
    ctx = make_ctx({"A": "DDDDD", "B": "DDODD"})
    ctx.frozen[("A", 2)] = FIXED_WANTED                               # 신청 OFF 인데 D(7B 허건 11일)
    ctx.frozen[("B", 2)] = FIXED_WANTED                               # 신청 OFF 그대로
    rows = [SimpleNamespace(nurse_id="A", shift_date=date(2026, 11, 3), shift_id="OFF"),
            SimpleNamespace(nurse_id="B", shift_date=date(2026, 11, 3), shift_id="OFF")]
    _load_wishes(None, ctx, SHIFTS, rows)
    assert ctx.wish_fixed == {("A", 2)} and ctx.wishes == {("A", 2): {"O"}}
    assert ctx.wish_skipped == []
    cand = Candidate(rank=1, changes=[cell_change(ctx, "A", 2, "O")], affected=["A"], optimal=True, window=(0, 4))
    assert not any(f.id.startswith(FIXED_WISH) for f in apply_findings(ctx, None, cand, []))
    released = with_wish_cells(ctx, {("A", 2)})
    assert ("A", 2) not in released.frozen                            # 검토 L3: 무엇을 했는지 분명하게
    assert released.notes[-1] == "확정 원티드와 근무표가 다른 1칸은 원티드대로 되돌릴 수 있습니다(다른 근무로는 바꾸지 않습니다)."
