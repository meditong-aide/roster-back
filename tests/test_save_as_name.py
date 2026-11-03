"""새 버전 기본 이름 — 수정본을 다시 고쳐도 '수정본 수정본'이 쌓이지 않는다(사용자 지적 11-03)."""

from __future__ import annotations

from types import SimpleNamespace

from routers.roster import _SCHEDULE_NAME_MAX, _save_as_name


def _src(name, version=1, month=11):
    return SimpleNamespace(name=name, version=version, month=month)


def test_first_edit_adds_the_tail_and_later_edits_count_up():
    assert _save_as_name(None, _src(None)) == "11월 근무표 VER1 수정본"
    taken = ["11월 근무표 VER1", "11월 근무표 VER1 수정본"]
    assert _save_as_name(None, _src("11월 근무표 VER1 수정본", 2), taken) == "11월 근무표 VER1 수정본 2"
    taken.append("11월 근무표 VER1 수정본 2")
    assert _save_as_name(None, _src("11월 근무표 VER1 수정본 2", 3), taken) == "11월 근무표 VER1 수정본 3"


def test_same_source_twice_does_not_repeat_a_name():
    taken = ["11월 근무표 VER1", "11월 근무표 VER1 수정본"]
    assert _save_as_name(None, _src(None), taken) == "11월 근무표 VER1 수정본 2"


def test_typed_name_and_long_names():
    assert _save_as_name("  내가 붙인 이름 ", _src("11월 근무표 VER1 수정본")) == "내가 붙인 이름"
    long = "가" * 48
    out = _save_as_name(None, _src(long), [long[:_SCHEDULE_NAME_MAX - 4] + " 수정본"])
    assert len(out) <= _SCHEDULE_NAME_MAX and out.endswith(" 수정본 2")
    assert _save_as_name(None, _src("수정본", 4)) == "11월 근무표 VER4 수정본"    # 꼬리만 남는 이름은 VER 이름으로
