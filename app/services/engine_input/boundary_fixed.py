"""경계 하드락 칸 계산 — 출발 경계(EI-11 11w-2 · G3 · D34)와 확정 칸 조정(11w-3 · G2 · D32). DB·생성 서비스(rcs)를 쓰지 않는 순수 함수만 둔다.

'출발 경계'는 간호사가 이 병동 마지막 날(s-1) 다음 날(s)부터 다른 병동에서 일하는 자리다 — 파견·병동이동 출발, 파견 온
간호사의 원 병동 복귀. 상대 병동의 같은 달 근무표가 이미 있으면(마감본 우선, 없으면 작성 중 — D34) 그 첫 칸들을 이어질
근무로 보고 이 병동 마지막 날들에 전이 금지칸(#2)·연속근무 OFF 창(#1)·N 이어 붙이기 금지(#3)를 건다.

근무는 메인코드로 받는다 — 근무 D/E/N/M/W/WK · OFF 'O' · 모름 '-'(상대 근무표에 칸 없음 — 연속 끊김 규약, G7·D35 와 같다)
· '#'(파견이 이 달 안에 끝나 이 병동으로 돌아온 날 — 엔진이 고르는 칸이라 아무것도 보장되지 않는다).
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field

WORK = frozenset({"D", "E", "N", "M", "W", "WK"})
UNKNOWN = "-"
HERE = "#"


@dataclass(frozen=True)
class BoundaryRules:
    """경계 칸 계산이 쓰는 하드락 설정 — K 연속근무 상한(#1) · L 나이트 연속 상한(#3) · 회복(#4·#5) · 전이 금지(#2)."""

    K: int
    L: int
    two2: bool
    two3: bool
    ban_nd: bool
    ban_ed: bool
    ban_ne: bool

    @property
    def T(self) -> int | None:
        """회복이 걸리는 N 블록 최소 길이 — 2N 뒤 2OFF 가 켜져 있으면 2, 3N 뒤 2OFF 만이면 3, 둘 다 없으면 None."""
        if self.two2:
            return 2
        if self.two3:
            return 3
        return None


def _int_or_none(value) -> int | None:
    try:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def rules_from_config(cfg: Mapping) -> BoundaryRules:
    """월중 경계 빌더(`build_mid_month_boundary_constraints`)와 같은 해석 — K = `max_conseq_work` → 5 · L = 정본 `three_seq_nig` 만(D13 ①) · 전이 금지는 `banned_day_after_eve` 가 켜졌을 때만."""
    k = _int_or_none(cfg.get("max_conseq_work"))
    ban = bool(cfg.get("banned_day_after_eve"))
    return BoundaryRules(
        K=5 if k is None else k, L=3 if bool(cfg.get("three_seq_nig", False)) else 2,
        two2=bool(cfg.get("two_offs_after_two_nig")), two3=bool(cfg.get("two_offs_after_three_nig")),
        ban_nd=ban and bool(cfg.get("ban_n_to_d", True)), ban_ed=ban and bool(cfg.get("ban_e_to_d", True)),
        ban_ne=ban and bool(cfg.get("ban_n_to_e", True)))


@dataclass
class BoundaryCells:
    """경계 칸 — 강제 OFF 날 · {날: 금지 코드} · OFF 창 [a, b](창 안 OFF ≥ 1 — 엔진은 창 안 차단일을 뺀다)."""

    forced: set[int] = field(default_factory=set)
    forbidden: dict[int, set[str]] = field(default_factory=dict)
    windows: list[list[int]] = field(default_factory=list)

    def force_off(self, day: int) -> None:
        self.forced.add(day)

    def forbid(self, day: int, code: str) -> None:
        self.forbidden.setdefault(day, set()).add(code)

    def window(self, a: int, b: int) -> None:
        if a <= b and [a, b] not in self.windows:
            self.windows.append([a, b])


def _leading_work(head: Iterable[str]) -> int:
    """앞에서부터 이어진 근무 칸 수(O·'-'·'#' 에서 끊김)."""
    n = 0
    for code in head:
        if code not in WORK:
            break
        n += 1
    return n


def departure_cells(head: Sequence[str], s: int, rules: BoundaryRules) -> BoundaryCells:
    """출발 경계 칸 — head = 상대 병동 칸(출발일 s 부터). 이 병동 마지막 날 s-1 이하에 건다.

    - #2 전이: 상대 첫 칸 D → s-1 에 N(ban_nd)·E(ban_ed) 금지 · 첫 칸 E → s-1 에 N(ban_ne) 금지.
    - #3·합친 블록 회복: 상대 첫 칸 N → s-1 에 N 금지(상대 N 블록 앞에 이 병동 N 을 이어 붙이지 않는다 — 보수적).
    - #1 연속근무: 상대 앞쪽 연속 근무 h ≥ 1 이면 OFF 창 [max(0, s-(K-h+1)), s-1] (K-h+1 ≤ 0 이면 [s-1, s-1]).
    - 회복(#4·#5)은 걸지 않는다 — 출발 뒤 날은 이 병동에서 차단일이라 엔진의 회복 식이 'OFF 아님'으로 읽어(fallback_lex `X()` →
      `_false_var`) 회복이 필요한 N 블록이 출발 직전에 끝나지 못한다. 칸으로도 걸면 정상인 짧은 블록까지 막는다(설계 실측)."""
    cells = BoundaryCells()
    last = s - 1
    if last < 0 or not head:
        return cells
    first = head[0]
    if first == "D":
        if rules.ban_nd:
            cells.forbid(last, "N")
        if rules.ban_ed:
            cells.forbid(last, "E")
    if first == "E" and rules.ban_ne:
        cells.forbid(last, "N")
    if first == "N":
        cells.forbid(last, "N")
    h = _leading_work(head)
    if h and rules.K:
        span = rules.K - h + 1
        cells.window(max(0, s - span) if span >= 1 else last, last)
    return cells


def departure_windows(away: Sequence[str], s: int, away_end: int, days: int, K: int,
                      base: Iterable[Sequence[int]] = ()) -> list[list[int]]:
    """짧은 파견을 사이에 둔 연속근무(#1) 창까지 더한 최소 OFF 창들 — away = 상대 병동 칸 [s, away_end].

    출발 앞 x ∈ [s-K, s-1] 마다 K+1 창 [x, x+K] 의 상대 병동 부분([s, min(x+K, away_end)])이 전부 근무면, 그 창 안 이 병동 날
    (출발 앞 · 돌아온 뒤)에 OFF 가 하나는 있어야 한다. 이 병동 날 집합을 후보로 두고 `base`(departure_cells 의 창)와 함께, 다른
    후보를 진부분집합으로 품는 것을 빼고(작은 쪽이 막으면 큰 쪽도 막힌다) [최소, 최대] 창으로 낸다 — 창 안 상대 병동 날은
    엔진이 차단일로 뺀다.
    - 창이 다음 달로 넘어가는 x 는 보지 않는다 — 다음 달 칸은 아직 없다('-' = 연속 끊김 규약).
    - 창이 전월로 넘어가는 x(s < K)는 이번 달 몫만 이 병동 날로 본다(이 빌더는 전월 칸을 안 읽는다 — 막는 쪽).
    - `away` 가 [s, away_end] 보다 짧으면 모자란 날은 모름('-')이다."""
    cand = [frozenset(range(a, b + 1)) for a, b in base]
    for x in (range(s - K, s) if K else ()):
        y = x + K
        if y > days - 1:
            break
        span = min(y, away_end) - s + 1
        outside = [away[i] if i < len(away) else UNKNOWN for i in range(span)]
        if not outside or any(code not in WORK for code in outside):
            continue
        mine = frozenset(day for day in range(max(0, x), y + 1) if day < s or day > away_end)
        if mine:
            cand.append(mine)
    keep = {m for m in cand if not any(o < m for o in cand)}
    return [[min(m), max(m)] for m in sorted(keep, key=lambda z: (min(z), max(z)))]


def departure_boundary(away: Sequence[str], s: int, away_end: int, returns_here: bool, days: int,
                       rules: BoundaryRules) -> BoundaryCells:
    """출발 경계 하나의 칸 — `departure_cells`(이 달 안에 돌아오면 돌아온 날을 '#' 로 이어 붙인 head) + `departure_windows`."""
    head = list(away) + ([HERE, HERE] if returns_here else [])
    cells = departure_cells(head, s, rules)
    cells.windows = departure_windows(away, s, away_end, days, rules.K, cells.windows)
    return cells


# ───────────────────────── 11w-3 · G2 · D32 — 확정 칸 조정 ─────────────────────────
# 칸 셋: 바깥 칸(전월·상대 병동 — 이 생성이 못 바꿈) · 확정 칸(이 달 고정 칸 — 확정 원티드·특수 근무, O 포함) · 엔진 칸(나머지).
# 확정 칸 자체는 하드락 면제(D32). 엔진이 고르는 칸은 바깥 꼬리 + 확정 칸을 이어 본 상태로 의무를 다시 건다 —
# 못 받은 회복 OFF 는 다음 가능한 날로 옮긴다(강제 OFF). 더하기만 한다(원래 경계 칸은 그대로).


def night_extension_limit(cons_n: int, night_cap: int, two_after_two: bool, two_after_three: bool) -> int:
    """월중 경계 N 꼬리(cons_n 개 · 뒤 OFF 0)에 이 병동이 경계일부터 이어 붙여도 되는 N 의 최대 개수(EI-11 11q G1).

    엔진은 경계 앞 날(상대 병동 근무)을 몰라 이어 붙인 N 블록을 이 병동 몫 j 개로만 센다 — 월경계의 `prev_month_n_tail`
    같은 통로가 월중 경계에는 없다. 그래서 j 개를 이어 붙여도 되는지를 여기서 판정한다.
      ① cons_n + j ≤ night_cap(L) — #3 나이트 연속 상한
      ② 합친 블록이 회복 규칙에 걸리면(cons_n + j ≥ T) 엔진이 이 병동 몫만으로도 그 회복을 건다(j ≥ T) — #4·#5
         (T = 2N→2OFF 가 켜져 있으면 2, 3N→2OFF 만이면 3)
    1..j 가 모두 되는 가장 큰 j 를 돌려준다(OFF 창 [경계일, 경계일+j] 는 '앞에서부터 j 개까지'만 나타낸다). 0 이면 경계일 N 을
    막는다. 전이 금지(N→D·N→E)가 꺼진 병동에서는 OFF 창이 D·E 로 끊는 것까지 막아 보수적이다(하드락 완화 아님).
    """
    threshold = 2 if two_after_two else (3 if two_after_three else None)
    limit = 0
    for j in range(1, night_cap + 1):
        total = cons_n + j
        if total > night_cap or (threshold is not None and total >= threshold and j < threshold):
            break
        limit = j
    return limit


def fixed_n_block_can_extend(start: int, end: int, days: int, work_days: Collection[int], limit: int) -> bool:
    """이 달 확정 N 블록 [start, end] 가 다음 날(엔진 칸)에 N 으로 이어 갈 수 있는가(12z) — 다음 날이 월 안이고 블록이 연속 나이트
    상한(limit) 아래이며, 다음 날부터 세 칸(N 으로 이으면 회복이 닿는 칸까지)에 확정 근무 칸이 없을 때. 이어 갈 수 있으면 회복 OFF 를
    미리 박지 않는다(NNN OO 를 자르지 않는다 — 회복 의무는 블록이 실제로 끝나는 길이에 맞게 엔진 #4·#5 가 건다). 그 칸들에 확정
    근무가 끼면 엔진 회복이 확정 칸과 부딪히니 N 이어 붙이기는 막고 OFF 를 옮기는 예전 방식 그대로다. 끝이 N 인 전월 꼬리와 이어진
    블록은 월경계 조정(`reconcile_forward`)이 꼬리를 합쳐 상한 칸·강제 OFF 를 건다. `work_days` 는 이 간호사의 확정 근무(비-O) 날."""
    return end - start + 1 < limit and end + 1 < days and not any(d in work_days for d in (end + 1, end + 2, end + 3))


def trail_state(seq: Sequence[str]) -> tuple[int, int, int, str | None]:
    """(끝 연속근무 w · 끝 OFF 앞 N 연속 c · 끝 OFF 수 o · 마지막 칸)."""
    w = 0
    for code in reversed(seq):
        if code not in WORK:
            break
        w += 1
    i = len(seq) - 1
    o = 0
    while i >= 0 and seq[i] == "O":
        o += 1
        i -= 1
    c = 0
    while i >= 0 and seq[i] == "N":
        c += 1
        i -= 1
    return w, c, o, (seq[-1] if seq else None)


def owed_offs(seq: Sequence[str], threshold: int | None, outside_len: int = 0) -> int:
    """닫힌(뒤에 칸이 있는) 길이 ≥ threshold 인 모든 N 블록 가운데 아직 못 받은 회복 OFF 수의 최댓값(0~2). 뒤따르는 확정 N 이
    앞 블록의 빚을 지우지 못하게 모든 블록을 본다. 블록 뒤에 모름('-')이 있거나 열린 블록(끝이 N)이면 그 블록은 0.
    outside_len = seq 앞쪽 바깥 꼬리 칸 수 — 블록 바로 뒤 두 칸에 바깥의 비-O 가 이미 있으면 바깥이 먼저 어긴 것이니 그 블록은 0."""
    if threshold is None:
        return 0
    best = 0
    i, n = 0, len(seq)
    while i < n:
        if seq[i] != "N":
            i += 1
            continue
        j = i
        while j < n and seq[j] == "N":
            j += 1
        after = list(seq[j:])
        window_out = [k for k in range(j, min(j + 2, n)) if k < outside_len and seq[k] != "O"]
        if j - i >= threshold and after and UNKNOWN not in after and not window_out:
            best = max(best, max(0, 2 - after.count("O")))
        i = j
    return best


def reconcile_forward(tail: Sequence[str], b: int, fixed: Mapping[int, str], days: int, active: Iterable[int],
                      rules: BoundaryRules) -> BoundaryCells | None:
    """앞 경계(월경계 b=0 · 합류 · 복귀)의 확정 칸 조정 — 영향 구간 [b, b+max(K, L+2)] 안에 비-O 확정 칸이 있고 꼬리에 의무
    (연속근무 > 0 또는 못 받은 회복 OFF)가 있을 때만(아니면 None — 원래 경계 칸 그대로).

    b 부터 활동일 동안: 확정 칸은 그 코드를 이어 붙이고 건너뛴다(면제) · 엔진 칸은 이어진 상태(꼬리 + 앞 칸)로 판정한다 —
    열린 N 블록이면 N 상한(#3)·회복 이어 붙이기 금지 · 마지막 칸이 N·E 면 전이 금지(#2) · 회복 OFF 를 못 받았거나 연속근무가
    K 에 닿았으면 그 날 강제 OFF 하고 다음 날로(옮긴 OFF) · 아니면 선택점(N 이어 붙이기 한도·연속근무 OFF 창)을 걸고 끝낸다.
    활동일이 끝나거나(출발·차단) 월말이면 멈춘다 — 그 뒤 의무는 이 병동이 못 건다."""
    reach = min(days - 1, b + max(rules.K, rules.L + 2))
    if not any(fixed.get(d, "O") != "O" for d in range(b, reach + 1)):
        return None
    if trail_state(tail)[0] == 0 and owed_offs(tail, rules.T, len(tail)) == 0:
        return None
    act = set(active)
    seq = list(tail)
    cells = BoundaryCells()
    d = b
    while d < days and d in act:
        if d in fixed:
            seq.append(fixed[d])
            d += 1
            continue
        w, c, o, last = trail_state(seq)
        must_off = owed_offs(seq, rules.T, len(tail)) > 0 or bool(rules.K and w >= rules.K)
        limit = None
        if c > 0 and o == 0 and (len(seq) - c) >= len(tail):
            # 이 달 확정 칸만으로 된 열린 N 블록 — 엔진이 이 달 안의 블록을 스스로 세므로 상한·이어 붙이기는 걸지 않는다
            # (바깥 꼬리용 정적 상한을 걸면 1N 금지에서 해가 있는 달이 해 없음이 된다). 못 받은 회복 OFF 만 옮긴다.
            # 블록이 아직 상한(L) 아래면 엔진 칸이 N 으로 이어 갈 수 있다(NNN OO) — 회복은 블록이 끝나는 길이에 맞게 엔진(#4·#5)이
            # 건다. 이어 붙일 수 없는 블록(c ≥ L)만 N 을 막고 회복 OFF 를 강제한다(12z · Codex 5차 P2).
            if rules.T is not None and c >= rules.T and not fixed_n_block_can_extend(
                    d - c, d - 1, days, {x for x, v in fixed.items() if v != "O"}, rules.L):
                cells.forbid(d, "N")
                must_off = True
            elif rules.T is not None and c >= rules.T and d + 1 in act:
                cells.force_off(d + 1)   # 이어 가도(NNN OO) 멈춰도(NN OO) 둘째 회복일은 OFF
        elif c > 0 and o == 0:
            limit = 0 if c >= rules.L else night_extension_limit(c, rules.L, rules.two2, rules.two3)
            if limit == 0:
                cells.forbid(d, "N")
                if rules.T is not None and c >= rules.T:
                    must_off = True
        if last == "N":
            if rules.ban_nd:
                cells.forbid(d, "D")
            if rules.ban_ne:
                cells.forbid(d, "E")
        if last == "E" and rules.ban_ed:
            cells.forbid(d, "D")
        if must_off:
            cells.force_off(d)
            seq.append("O")
            d += 1
            continue
        if limit:
            cells.window(d, min(d + limit, days - 1))
        if rules.K and w > 0:
            cells.window(d, min(d + rules.K - w, days - 1))
        break
    return cells


def reconcile_backward(head: Sequence[str], s: int, fixed: Mapping[int, str], active: Iterable[int],
                       rules: BoundaryRules) -> BoundaryCells | None:
    """출발 경계 s 바로 앞에 이어진 확정 칸들을 상대 병동 칸(head) 앞에 붙이고 출발일을 그 첫 확정 칸으로 옮겨 출발 칸을 다시
    계산한다(확정 칸이 없으면 None). 예: 확정 N 이 마지막 날이고 상대 첫 칸이 N 이면 그 앞날 N 금지."""
    act = set(active)
    t = s - 1
    pre: list[str] = []
    while t >= 0 and t in fixed and t in act:
        pre.insert(0, fixed[t])
        t -= 1
    if not pre:
        return None
    return departure_cells(pre + list(head), t + 1, rules)
