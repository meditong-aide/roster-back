"""EI-10c — precheck DP 두 개가 엔진(fallback_lex) 규칙 의미와 같다(독립 brute-force 대조).

DP 가 엔진보다 엄하면 '증명된 불가능'이 아니라 오차단이다. 하드락 값이 precheck 에 실리면(10c) 이
차이가 드러나므로 같은 단계에서 의미를 엔진에 맞춘다. 오라클은 DP 코드를 쓰지 않고 엔진 제약을
그대로 옮긴 독립 검사다.

엔진 규칙(오라클):
  - 전이 금지(N→D·E→D·N→E)는 **두 칸이 모두 고정**이면 면제(fallback_lex.py:2000·2026·2052).
  - 연속근무 K: K+1 일 창에 OFF 가 하나는 있어야 한다. 연속 N 상한 L: L+1 일 창이 전부 N 일 수 없다.
  - 회복(2N→2OFF·3N→2OFF): 길이 rec 이상 N 블록이 **끝날 때**(끝 e ≤ n-3) e+1·e+2 가 OFF
    (fallback_lex.py:2798-2829·2915-2931). L=3 이면 `NNN OO` 가 가능하다.
  - 1N 금지: N 은 이웃 N 이 하나는 있어야 한다. 면제 — 월 N 상한 1 간호사, 1일 고정 N,
    이웃이 없는 하루짜리 구간(fallback_lex.py:2129·2136·2153).
"""

from __future__ import annotations

from itertools import product

import pytest
from services.precheck.per_nurse_sequence_feasibility import nurse_sequence_infeasible
from services.precheck.team_grade_precheck import max_nights_under_recovery

_FIXED = (
    {}, {0: "N", 1: "D"}, {1: "N", 2: "N", 3: "N"}, {0: "N"}, {2: "N", 3: "N", 4: "D"}, {0: "N", 2: "O"},
    {1: "E", 2: "D"},
)
_BANS_ON = (True, True, True)
_BANS_OFF = (False, False, False)


def _transition_ok(seq, fixed, bans) -> bool:
    ban_nd, ban_ed, ban_ne = bans
    for i in range(1, len(seq)):
        if i in fixed and i - 1 in fixed:
            continue  # 엔진: 두 칸 다 고정이면 전이 금지 면제
        prev, cur = seq[i - 1], seq[i]
        if cur == "D" and ((prev == "N" and ban_nd) or (prev == "E" and ban_ed)):
            return False
        if cur == "E" and prev == "N" and ban_ne:
            return False
    return True


def _windows_ok(seq, max_work, max_night_run) -> bool:
    n = len(seq)
    if max_work is not None and any(all(seq[d] != "O" for d in range(s, s + max_work + 1)) for s in range(n - max_work)):
        return False
    return not (max_night_run is not None
                and any(all(seq[d] == "N" for d in range(s, s + max_night_run + 1)) for s in range(n - max_night_run)))


def _recovery_ok(seq, rec) -> bool:
    n = len(seq)
    for end in range(n):
        if seq[end] != "N" or (end + 1 < n and seq[end + 1] == "N"):
            continue  # 블록의 끝이 아니다
        length, start = 0, end
        while start >= 0 and seq[start] == "N":
            length, start = length + 1, start - 1
        if length >= rec and end <= n - 3 and (seq[end + 1] != "O" or seq[end + 2] != "O"):
            return False
    return True


def _single_night_ok(seq, fixed, single_ok) -> bool:
    n = len(seq)
    if single_ok or n == 1:
        return True
    for i, code in enumerate(seq):
        if code != "N" or (i == 0 and fixed.get(0) == "N"):
            continue
        if not ((i > 0 and seq[i - 1] == "N") or (i < n - 1 and seq[i + 1] == "N")):
            return False
    return True


def _engine_valid(seq, fixed, rules) -> bool:
    """엔진 제약을 그대로 옮긴 독립 검사 — 이 배열이 모든 규칙을 지키는가."""
    if any(seq[d] != code for d, code in fixed.items()):
        return False
    if not _transition_ok(seq, fixed, rules["bans"]) or not _windows_ok(seq, rules["K"], rules["L"]):
        return False
    if rules["rec"] and not _recovery_ok(seq, rules["rec"]):
        return False
    if rules["not_one"] and not _single_night_ok(seq, fixed, rules["single_ok"]):
        return False
    return seq.count("N") <= rules["n_cap"]


def _dp_infeasible(n, fixed, rules) -> bool:
    return nurse_sequence_infeasible(
        num_days=n, allowed=set(), fixed=fixed, max_consecutive_work=rules["K"],
        max_consecutive_nights=rules["L"], ban_n_to_d=rules["bans"][0], ban_e_to_d=rules["bans"][1],
        ban_n_to_e=rules["bans"][2], two_offs_after_two_nig=(rules["rec"] == 2),
        two_offs_after_three_nig=(rules["rec"] == 3), ban_single_night=rules["not_one"],
        n_min=0, n_max=rules["n_cap"], single_n_allowed=rules["single_ok"],
        day0_single_n_ok=fixed.get(0) == "N")


def _oracle_infeasible(n, fixed, rules) -> bool:
    return not any(_engine_valid(seq, fixed, rules) for seq in product("DENO", repeat=n))


@pytest.mark.parametrize("rec", (0, 2, 3))
@pytest.mark.parametrize("night_run", (2, 3))
def test_sequence_dp_equals_engine_semantics(rec, night_run):
    """개인 시퀀스 DP = 엔진 의미 brute-force (n ≤ 6 · 고정 7종 · K·금지·1N·면제·N 상한 조합)."""
    combos = product(range(1, 7), _FIXED, (None, 3), (_BANS_ON, _BANS_OFF), (False, True), (False, True), (1, 99))
    for n, fixed, max_work, bans, not_one, single_ok, n_cap in combos:
        if any(day >= n for day in fixed):
            continue
        rules = {"K": max_work, "L": night_run, "bans": bans, "rec": rec, "not_one": not_one,
                 "single_ok": single_ok, "n_cap": n_cap}
        assert _dp_infeasible(n, fixed, rules) == _oracle_infeasible(n, fixed, rules), (n, fixed, rules)


@pytest.mark.parametrize("block", (2, 3))
@pytest.mark.parametrize("night_run", (None, 2, 3))
def test_recovery_cap_equals_engine_semantics(block, night_run):
    """회복 용량 DP(`max_nights_under_recovery`) = 엔진 의미 brute-force 최대 N (span ≤ 12).

    `night_run=None` 은 '연속 N 상한 = block'(10c 전 의미)이다.
    """
    rules = {"K": None, "L": block if night_run is None else night_run, "bans": _BANS_OFF, "rec": block,
             "not_one": False, "single_ok": False, "n_cap": 99}
    for span in range(13):
        best = max((seq.count("N") for seq in product("NO", repeat=span) if _engine_valid(seq, {}, rules)),
                   default=0)
        assert max_nights_under_recovery(span, block, max_run=night_run) == best, (block, night_run, span)


@pytest.mark.parametrize(("span", "block", "night_run", "expected"), (
    (30, 3, 3, 21),   # 골든 EI5 의 회복 축 값(30일 · 3N→2OFF · L=3)
    (20, 2, 2, 11),   # 11일 입사 N 전담 · 2N→2OFF · L=2   (NNOO 반복 + 앞 단독 N)
    (20, 2, 3, 13),   # 같은 구간 · L=3 이면 NNN OO 가 가능
    (20, 3, 3, 14),   # 3N→2OFF · NNO 반복
))
def test_recovery_cap_known_production_values(span, block, night_run, expected):
    """운영 크기(20·30일) 값 고정 — 엔진 규칙 그대로의 비트마스크 전수 열거(2^20·2^30, C)로 확인한 값이다."""
    assert max_nights_under_recovery(span, block, max_run=night_run) == expected


def test_recovery_cap_default_run_equals_block():
    """`max_run` 을 안 주면 연속 N 상한이 block 이다(10c 전 호출부와 같은 값)."""
    for span in range(31):
        for block in (2, 3):
            assert max_nights_under_recovery(span, block) == max_nights_under_recovery(span, block, max_run=block)


def test_nnn_then_two_offs_is_allowed_under_two_offs_after_two():
    """L=3 · 2N→2OFF — 고정 NNN 은 블록이 끝난 뒤 OO 로 풀린다(옛 DP 는 2개에서 끊어 막았다)."""
    kwargs = {"num_days": 8, "allowed": set(), "fixed": {2: "N", 3: "N", 4: "N"},
              "max_consecutive_nights": 3, "two_offs_after_two_nig": True}
    assert nurse_sequence_infeasible(**kwargs) is False
    assert nurse_sequence_infeasible(**{**kwargs, "max_consecutive_nights": 2}) is True


def test_recovery_not_required_when_block_ends_in_last_two_days():
    """블록 끝이 마지막 이틀이면 회복 OFF 를 요구하지 않는다(엔진 range(T0+1, T1-1)) — 마지막 날 고정 D 가 가능."""
    base = {"allowed": set(), "max_consecutive_nights": 2, "two_offs_after_two_nig": True, "ban_n_to_d": False}
    assert nurse_sequence_infeasible(num_days=6, fixed={3: "N", 4: "N", 5: "D"}, **base) is False
    assert nurse_sequence_infeasible(num_days=6, fixed={2: "N", 3: "N", 5: "D"}, **base) is True


def test_transition_ban_exempt_only_when_both_cells_fixed():
    """N→D 금지는 두 칸이 다 고정일 때만 면제 — 한쪽이 자유칸이면 그대로 걸린다."""
    both = {"num_days": 3, "allowed": set(), "fixed": {0: "N", 1: "D"}, "ban_n_to_d": True}
    assert nurse_sequence_infeasible(**both) is False
    forced = {"num_days": 2, "allowed": {"N", "D"}, "fixed": {1: "D"}, "ban_n_to_d": True, "n_min": 1}
    assert nurse_sequence_infeasible(**forced) is True


def test_single_night_exemptions():
    """1N 금지 면제 3종 — 월 N 상한 1 · 1일 고정 N · 하루짜리 구간. 면제가 없으면 막힌다."""
    fixed_pair = {"allowed": set(), "ban_single_night": True}
    assert nurse_sequence_infeasible(num_days=5, fixed={2: "N", 1: "O", 3: "O"}, **fixed_pair) is True
    assert nurse_sequence_infeasible(
        num_days=5, fixed={2: "N", 1: "O", 3: "O"}, single_n_allowed=True, **fixed_pair) is False
    assert nurse_sequence_infeasible(num_days=5, fixed={0: "N", 1: "O"}, **fixed_pair) is True
    assert nurse_sequence_infeasible(
        num_days=5, fixed={0: "N", 1: "O"}, day0_single_n_ok=True, **fixed_pair) is False
    assert nurse_sequence_infeasible(num_days=1, fixed={0: "N"}, **fixed_pair) is False
