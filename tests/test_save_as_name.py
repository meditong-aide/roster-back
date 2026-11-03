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


def _chain(first: str, times: int):
    """수정본을 다시 고치기를 되풀이 — 매번 앞에서 만든 수정본이 원본, 같은 달 이름이 쌓인다."""
    taken, name, out = [first], first, []
    for v in range(2, times + 2):
        name = _save_as_name(None, _src(name, v), taken)
        taken.append(name)
        out.append(name)
    return out


def test_numbers_never_go_back_even_when_long_names_are_clipped():
    # 꼬리 길이에 따라 잘리는 자리가 달라 정확히 같은 이름이 아니어도 같은 계열로 센다(Codex 11-03 재현: 3번째에 번호가 1로)
    for first in ("11월 근무표 VER1", "가" * 48, "가" * 50):
        names = _chain(first, 5)
        numbers = [1 if n.endswith(" 수정본") else int(n.rsplit(" ", 1)[1]) for n in names]
        assert numbers == [1, 2, 3, 4, 5], names
        assert all(len(n) <= _SCHEDULE_NAME_MAX for n in names) and len(set(names)) == len(names)


def test_old_stacked_tails_are_cleaned_up():
    taken = ["11월 근무표 VER1", "11월 근무표 VER1 수정본", "11월 근무표 VER1 수정본 수정본"]
    assert _save_as_name(None, _src("11월 근무표 VER1 수정본 수정본", 3), taken) == "11월 근무표 VER1 수정본 2"
    # 다른 원본 계열의 번호는 세지 않는다
    assert _save_as_name(None, _src("11월 근무표 VER2", 2), taken + ["11월 근무표 VER1 수정본 4"]) \
        == "11월 근무표 VER2 수정본"
