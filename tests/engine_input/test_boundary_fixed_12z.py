"""Codex 5차 [P2](12z) — 확정 N 블록은 연속 나이트 상한(L)까지 이어 갈 수 있다(NNN OO 허용).

전월이 D 로 끝나고 1~2일이 확정 N 이며 allow_three_consecutive_nights·two_offs_after_two_nig 가 켜진 병동: 3일(엔진 칸)에 N 을 막고 OFF 를
강제하면 엔진이 허용하는 NNN OO 를 잘라 해를 줄이고, 그날 그 간호사가 필요한 precheck 에 거짓 부족을 만든다.
회복 의무는 블록이 실제로 끝나는 길이에 맞게(NN → #5 · NNN → #4) 엔진이 건다. 블록이 이미 L 이면 다음 날 OFF 강제 그대로.
"""

from __future__ import annotations

import pytest
from services.engine_input.boundary_fixed import BoundaryRules, fixed_n_block_can_extend, reconcile_forward

from .boundary_cases import CFG
from .test_boundary_fixed_d32 import _check_d32, _month, _run

ALL = list(range(30))
R3 = BoundaryRules(K=5, L=3, two2=True, two3=True, ban_nd=True, ban_ed=True, ban_ne=True)
R3_X = BoundaryRules(K=5, L=3, two2=False, two3=True, ban_nd=True, ban_ed=True, ban_ne=True)
R2 = BoundaryRules(K=5, L=2, two2=True, two3=True, ban_nd=True, ban_ed=True, ban_ne=True)
TAIL = list("OOOOOD")


def _rf(fixed, rules):
    rc = reconcile_forward(TAIL, 0, fixed, 30, ALL, rules)
    assert rc is not None
    return rc.forced, rc.forbidden


@pytest.mark.parametrize("fixed,rules,forced,forbidden", [
    # Codex 사례 — L=3 · 2N→2OFF: 확정 NN 뒤 3일(색인 2)은 N 도 OFF 도 열어 둔다(전이 금지 D·E 만) · 4일(색인 3)은
    # NN OO 든 NNN OO 든 OFF 라 강제 OFF
    ({0: "N", 1: "N"}, R3, {3}, {2: {"D", "E"}}),
    # L=3 · 3N→2OFF 만: NN 은 회복 의무가 없고 3일 N 이 열려 있다(원래도 열림 — 그대로)
    ({0: "N", 1: "N"}, R3_X, set(), {2: {"D", "E"}}),
    # 블록이 이미 L(3) — 4일(색인 3) N 금지 · 3·4일 아니라 4·5일(색인 3·4) 강제 OFF 그대로
    ({0: "N", 1: "N", 2: "N"}, R3, {3, 4}, {3: {"N", "D", "E"}}),
    # L=2(allow_three_consecutive_nights 꺼짐): 블록이 이미 L — 3일 N 금지 · 3·4일 강제 OFF 그대로
    ({0: "N", 1: "N"}, R2, {2, 3}, {2: {"N", "D", "E"}}),
    # 회복이 닿는 칸(3·4·5일)에 확정 근무가 끼면 엔진 회복이 확정 칸과 부딪힌다 — 예전 방식(N 금지 · OFF 옮김) 그대로
    ({0: "N", 1: "N", 3: "D"}, R3, {2, 4}, {2: {"N", "D", "E"}}),
    ({0: "N", 1: "N", 4: "D"}, R3, {2, 3}, {2: {"N", "D", "E"}}),
    # 확정 OFF 나 더 먼 확정 근무(6일)는 상관없다 — 열어 둔다
    ({0: "N", 1: "N", 3: "O"}, R3, {3}, {2: {"D", "E"}}),
    ({0: "N", 1: "N", 5: "D"}, R3, {3}, {2: {"D", "E"}}),
])
def test_fixed_n_block_extends_up_to_limit(fixed, rules, forced, forbidden):
    assert _rf(fixed, rules) == (forced, forbidden)


def test_mid_month_boundary_fixed_nn_extends_too():
    """월중 경계(합류·복귀 b=19)에서도 같은 함수 — 경계일 직후 확정 NN 뒤 엔진 칸이 N 으로 이어 갈 수 있다."""
    rc = reconcile_forward(list("DDDOOD"), 19, {19: "N", 20: "N"}, 30, ALL, R3)
    assert rc is not None and rc.forced == {22} and "N" not in rc.forbidden.get(21, set())


def test_fixed_n_block_gap_in_middle_still_extends():
    """블록 중간에 엔진 칸이 낀 경우(N, 엔진, N) — 엔진 칸 앞 확정 N 은 c=1(T 미만)이라 원래도 열려 있다."""
    forced, forbidden = _rf({0: "N", 2: "N"}, R3)
    assert 1 not in forced and "N" not in forbidden.get(1, set())


# 엔진 탐침 — 전월 …D · 1~2일 확정 N. 엔진 칸 색인: 2(3일)·3·4 …
_Y = _month("g2z-month-D-NN@1-2-Y", "EOOOOD", "Y", [("N01", 1, "N"), ("N01", 2, "N")])
_X = _month("g2z-month-D-NN@1-2-X", "EOOOOD", "X", [("N01", 1, "N"), ("N01", 2, "N")])
_Z = _month("g2z-month-D-NN@1-2-Z", "EOOOOD", "Z", [("N01", 1, "N"), ("N01", 2, "N")])
_Y3 = _month("g2z-month-D-NNN@1-3-Y", "EOOOOD", "Y", [("N01", 1, "N"), ("N01", 2, "N"), ("N01", 3, "N")])
_W = _month("g2z-month-D-NN@1-2-W", "EOOOOD", "W", [("N01", 1, "N"), ("N01", 2, "N")])

FEAS, INFEAS = "FEASIBLE", "INFEASIBLE"
# 색인 0·1 = 확정 N. 탐침은 엔진 칸 색인 2~ 만 강제한다.
PROBES = {
    "Y": [({2: "N", 3: "O", 4: "O"}, FEAS),     # NNN OO — 엔진이 허용
          ({2: "N", 3: "N"}, INFEAS),             # NNNN — #3
          ({2: "N", 3: "D"}, INFEAS), ({2: "N", 3: "O", 4: "D"}, INFEAS),   # NNN 뒤 D/OD — #4
          ({2: "D"}, INFEAS), ({2: "E"}, INFEAS), ({2: "O", 3: "D"}, INFEAS),    # NN D · NN O D — #2·#5
          ({2: "O", 3: "O", 4: "D"}, FEAS)],    # NN OO D
    "Z": [({2: "N"}, INFEAS), ({2: "D"}, INFEAS), ({2: "O", 3: "D"}, INFEAS), ({2: "O", 3: "O", 4: "D"}, FEAS)],   # L=2
}


@pytest.mark.parametrize("case,extra,probes", [
    (*_Y, PROBES["Y"]),
    (*_X, [p for p in PROBES["Y"] if p[0] != {2: "O", 3: "D"}] + [({2: "O", 3: "D"}, FEAS)]),   # X: NN 뒤 회복 의무 없음
    (*_Z, PROBES["Z"]),
    (*_W, [({2: "N", 3: "O", 4: "O"}, FEAS), ({2: "N", 3: "N"}, INFEAS), ({2: "O", 3: "O", 4: "D"}, FEAS)]),
], ids=["Y", "X", "Z", "W"])
def test_engine_allows_nnn_oo_and_keeps_hard_locks(case, extra, probes, engine, engine_input_tables, monkeypatch):
    _check_d32(case, engine, monkeypatch, extra, extra_probes=probes, need_probes=False)


def test_engine_block_at_limit_keeps_forced_off(engine, engine_input_tables, monkeypatch):
    """확정 NNN(= L) — 4일 N·D·E 는 막히고 4·5일은 OFF, 6일부터 열린다(NNN 뒤 N 은 #3)."""
    case, extra = _Y3
    _check_d32(case, engine, monkeypatch, extra,
               need_probes=False, extra_probes=[({3: "N"}, INFEAS), ({3: "D"}, INFEAS), ({3: "O", 4: "D"}, INFEAS), ({3: "O", 4: "O", 5: "D"}, FEAS)])


def test_cfg_has_expected_flags():
    assert CFG["Y"]["two_offs_after_two_nig"] and CFG["Z"]["allow_three_consecutive_nights"] is False


# ── 사전 주입(2N→2OFF recovery) — 이어 갈 수 있는 확정 N 블록에는 박지 않는다 ───────────────────────────


@pytest.mark.parametrize("start,end,days,work,limit,expected", [
    (0, 1, 30, set(), 3, True),          # Codex 사례 — NN · L=3
    (0, 2, 30, set(), 3, False),         # 블록이 이미 L
    (0, 1, 30, set(), 2, False),         # L=2(allow_three_consecutive_nights 꺼짐)
    (0, 1, 30, {2}, 3, False),   # 다음 날이 확정 근무 칸
    (0, 1, 30, {3}, 3, False),   # 회복이 닿는 둘째 칸이 확정 근무 — 엔진 회복과 부딪힌다
    (0, 1, 30, {4}, 3, False),   # N 으로 이으면 회복이 닿는 칸이 확정 근무
    (0, 1, 30, {5}, 3, True),    # 그 밖은 상관없다
    (28, 29, 30, set(), 3, False),       # 월말 — 다음 날이 없다
    (5, 6, 30, set(), 3, True),          # 월중 블록
])
def test_fixed_n_block_can_extend_table(start, end, days, work, limit, expected):
    assert fixed_n_block_can_extend(start, end, days, work, limit) is expected


def _fixed_o_days(l0):
    return sorted(c["day_index"] for c in l0["fixed_cells"] if c["nurse_id"] == "N01" and str(c["shift"]).upper() == "O")


_TAILN = _month("g2z-month-N-tail-NN-Y", "EOOOON", "Y", [("N01", 1, "N"), ("N01", 2, "N")])


@pytest.mark.parametrize("case,extra,o_days", [
    (*_Y, [3]),       # NN · L=3 — precheck 가 3일(색인 2)을 쉬는 날로 보지 않는다(거짓 부족 없음) · 4일(색인 3)은 OFF
    (*_W, [3]),
    (*_Z, [2, 3]),    # L=2 — 이어 갈 수 없는 블록은 회복 OFF 를 그대로 박는다
    (*_Y3, [3, 4]),   # NNN = L
], ids=["Y", "W", "Z", "Y3"])
def test_precheck_input_does_not_rest_day_after_extendable_block(case, extra, o_days, engine, engine_input_tables):
    l0, _l1, rec = _run(engine, case, extra)
    assert l0, (rec.outcome, rec.error)
    assert _fixed_o_days(l0) == o_days


def test_engine_tail_n_plus_fixed_nn_stays_locked(engine, engine_input_tables, monkeypatch):
    """전월 끝 N + 확정 NN = 3(L) — 4번째 N 금지 · 3·4일 OFF 그대로(상한에 닿은 블록은 열지 않는다)."""
    case, extra = _TAILN
    _check_d32(case, engine, monkeypatch, extra, need_probes=False,
               extra_probes=[({2: "N"}, INFEAS), ({2: "D"}, INFEAS), ({2: "O", 3: "D"}, INFEAS), ({2: "O", 3: "O", 4: "D"}, FEAS)])



def test_tail_n_plus_fixed_nn_is_locked_by_boundary_reconcile(engine, engine_input_tables):
    """끝이 N 인 전월 꼬리와 이어진 확정 NN(=L) — 월경계 조정이 3일을 강제 OFF·N 금지로 건다(4일은 사전 주입 OFF —
    precheck 는 둘 다 쉬는 날로 읽는다)."""
    case, extra = _TAILN
    l0, l1, rec = _run(engine, case, extra)
    assert l1, (rec.outcome, rec.error)
    ic = l1["config_data"]["initial_constraints"]
    assert ic["forced_off"]["N01"] == [2], ic
    assert "N" in ic["forbidden"]["N01"]["2"], ic
    assert _fixed_o_days(l0) == [3], l0["fixed_cells"]
