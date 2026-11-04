"""처음 화면 예시 — 이 근무표의 실제 이름·날짜로, 누르면 그대로 보내도 되는 문장(ui-craft 검토 11-03)."""

from __future__ import annotations

from datetime import date

from schedule_edit_agent.examples import TEMPLATE, examples_from, live_examples
from tests.schedule_edit.test_solver import make_ctx


def _ward():
    ctx = make_ctx({"A": "DDEENNOO", "B": "EEDDOONN", "C": "OONNDDEE"})
    ctx.names.update({"A": "김가람", "B": "이나래", "C": "박다솜"})
    return ctx


def test_examples_point_at_cells_that_can_change():
    ctx = _ward()
    out = examples_from(ctx, date(2026, 11, 2))          # 2일이 오늘 → 3일(내일)은 건너뛰고 4일부터 고른다
    assert out[0] == "김가람 4일 오프로"                     # A 4일 E — 근무 칸
    assert out[1] == "4일 김가람, 이나래 근무 바꿔줘"          # 4일 A=E · B=D — 근무가 달라 맞바꾸면 바뀐다
    assert out[2] == "김가람 N 1개만 줄여줘"                   # 남은 날 N 이 가장 많은 사람(셋 다 2개 — 같으면 표 순서)
    # 휴가는 남은 날의 뒤쪽 절반(6~8일)에서 — 앞 예시와 날이 겹치지 않게
    assert out[3:] == ["4일 이브닝 한 명 더", "이나래 7일 연차 넣어줘", "4일 나이트 누구야", "N 근무 수 고르게 맞춰줘",
                       "반영 안 된 원티드 넣어줘", "하루짜리 OFF 없애줘", "남는 OFF 연차로 전환해줘"]
    assert not any("○" in e for e in out)


def test_frozen_cells_and_past_months_fall_back():
    ctx = _ward()
    ctx.frozen[("A", 3)] = "관할 밖(파견·이동·휴직·프리셉티)"
    assert examples_from(ctx, date(2026, 11, 2))[0] == "이나래 4일 오프로"    # 묶인 칸은 고르지 않는다
    assert examples_from(ctx, date(2026, 12, 1)) == []                       # 지난 달 — 바꿀 날이 없다
    assert live_examples(None, None, date(2026, 11, 2)) == TEMPLATE
