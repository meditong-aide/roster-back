"""후보가 있어도 더 적게 바꾸는 길(11-03 · 4차 평가 'OFF 하나에 18칸' · 사용자 제안 '설정을 바꾸면 수정 안건을 더 줄일 수
있을 경우만 카드로').

  · 좁은 창에서 찾은 안보다 한 달 전체에 주 loss 가 작은 안이 있으면 그것을 낸다(`solver._wider`).
  · 그래도 바꾸는 칸이 많으면(SHORTCUT_MIN_CHANGES 이상) 설정 하나를 이번만 넘겨 칸이 줄어드는 레버만 붙인다(`_shortcuts`).
사례는 무작위 작은 근무표에서 찾은 것(개인 OFF 수가 정해진 병동 · 하루 D·E 최소 1명). 패널 표시는 tests/edit_agent/test_shortcut_panel.py.
"""

from __future__ import annotations

from services.schedule_edit import solver as S
from services.schedule_edit.requests import SetCell
from tests.schedule_edit.test_solver import BEFORE_MONTH, make_ctx

LOOSE = {"not_one_night": False, "isolated_work_hard": False, "two_offs_after_two_nig": False,
         "two_offs_after_three_nig": False, "three_seq_nig": False}


def _ward(rows):
    ctx = make_ctx(rows, cov_min={"D": 1, "E": 1}, config=LOOSE)
    for nid, row in rows.items():
        ctx.off_bounds[nid] = (row.count("O"), row.count("O"))       # 개인 OFF 수가 정해져 있다
    return ctx


def _wide_ward():
    return _ward({"A": "DEOODDDEOE", "B": "EDEODEOODD", "C": "ODDOOEDEDE"})


def _long_ward():
    return _ward({"A": "OEDODODD", "B": "DDDDDDEO", "C": "DDDDEEEO"})


def test_a_better_plan_beyond_the_narrow_window_is_not_thrown_away(monkeypatch):
    request = [SetCell("B", 0, "O")]
    wide = S.propose_edits(_wide_ward(), request, today=BEFORE_MONTH)
    assert wide.status == "ok" and min(c.loss for c in wide.candidates) == 14
    monkeypatch.setattr(S, "_wider", lambda run, found: None)              # 예전: 좁은 창에서 찾으면 멈췄다
    narrow = S.propose_edits(_wide_ward(), request, today=BEFORE_MONTH)
    assert min(c.loss for c in narrow.candidates) == 15


def test_levers_are_offered_only_when_they_cut_the_changes():
    ctx = _long_ward()
    result = S.propose_edits(ctx, [SetCell("A", 1, "O")], today=BEFORE_MONTH)
    assert result.status == "ok" and min(c.changed_count for c in result.candidates) == 5
    # 레버마다 칸이 실제로 줄어든다(같은 비용의 두 번째 레버는 풀 때마다 A·B 중 하나로 갈려 이름은 보지 않는다)
    assert result.levers and all(lv.changes[0] == 5 and lv.changes[1] < 5 for lv in result.levers)
    assert min((lv.changes[1], lv.lose[0]) for lv in result.levers) == (2, "2일 일별 최소 인원(E) 1명 넘김")
    # 고르면 그 칸 수로 다시 찾아진다
    best = min(result.levers, key=lambda lv: lv.changes[1])
    again = S.propose_edits(ctx, [SetCell("A", 1, "O")], today=BEFORE_MONTH, allow=best.allow)
    assert min(c.changed_count for c in again.candidates) == 2
    # 작은 안(SHORTCUT_MIN_CHANGES 미만)에는 찾지 않는다
    small = S.propose_edits(_long_ward(), [SetCell("A", 0, "D")], today=BEFORE_MONTH)
    assert small.status == "ok" and min(c.changed_count for c in small.candidates) < S.SHORTCUT_MIN_CHANGES
    assert small.levers == []
