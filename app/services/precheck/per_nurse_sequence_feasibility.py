"""Per-nurse 시퀀스 feasibility — 개인축 조기진단 (solve 전, blocking).

한 간호사만 놓고, 그 사람의 고정셀 + 시퀀스 규칙(전이금지/연속근무/연속야간/회복/1N)과
월 N 범위가 서로 물려 **혼자서도 완성 불가능**한지를 30일 DP 로 판정한다.
혼자 불가능하면 전체도 불가능(증명) → aggregate/max-flow 가 못 보는
'고정셀 × 시퀀스' 배치 충돌과 'n_exact vs 회복상한' 을 solve 전에 잡는다.

★ EI-10c — 규칙 의미는 **엔진(fallback_lex)과 같다**. DP 가 엔진보다 엄하면 '증명된 불가능'이
  아니라 오차단이 된다. 하드락 값이 precheck 에 실리면(`hard_lock_view`) 이 차이가 바로 드러난다.
  - 전이 금지(N→D·E→D·N→E)는 두 칸이 **모두 고정**이면 면제(fallback_lex.py:2000·2026·2052).
  - 회복(2N→2OFF·3N→2OFF)은 길이 `block` 이상 N 블록이 **끝날 때** 그 다음 이틀 OFF
    (fallback_lex.py:2915-2931·2798-2829). 블록 길이는 연속 N 상한 L 까지 — L=3·2N→2OFF 면
    `NNN OO` 가 된다. 블록 끝이 마지막 이틀이면 회복을 요구하지 않는다(`range(T0+1, T1-1)`).
  - 1N 금지는 월 N 상한 1 간호사·1일 고정 N·하루짜리 구간(이웃이 없다)에서 면제
    (fallback_lex.py:2129·2136·2153).
  - MID 병동(EI-14r2): `mid_transition` — M 은 전날이 D·O·M 일 때만(E→M·N→M 금지, M→M 허용, **고정 면제 없음**),
    `mid_to_day_ban` — M→D 금지(**양끝이 고정 M→D 일 때만 면제**) (fallback_lex.py:2072-2093). 월 첫날 직전은 O 로 본다
    (전월 꼬리를 모른다 — 느슨한 쪽).

한계(설계): 개인 단독 판정만 한다(cross-nurse 커버리지는 max-flow 층 담당).
OFF 예산 최소는 aggregate capacity 층이 담당하므로 여기선 다루지 않는다.
전월 꼬리(월경계)는 모른다 — EI-10g.
DP 는 상한만 조이는 '증명된 불가능'만 반환 → false positive 없음.

상태: (day, prev, consec_work, consec_night, recovery_off_rem, need_night_next, nights)
- prev: 직전 근무 코드(전이금지 판정용)
- recovery_off_rem: 야간 블록이 끝난 뒤 강제 OFF 잔여
- need_night_next: 1N 금지 시 방금 시작한 단독 N 이 다음날 N 을 요구하는지
- nights: 지금까지 N 수(월 상/하한 판정용)
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from functools import cache

_WORK = ("D", "E", "N")
_KNOWN_CODES = frozenset({"D", "E", "N", "M", "O"})
_State = tuple[int, str, int, int, int, bool, int]


@dataclass(frozen=True, eq=False)
class _Rules:
    """DP 가 보는 규칙 한 벌(상태 캐시 키가 아니라 클로저로만 쓴다)."""

    num_days: int
    work_allowed: frozenset[str]
    fixed: dict[int, str]
    max_work: int | None      # 연속근무 상한 K
    max_night_run: int | None  # 연속 N 상한 L
    bans: tuple[bool, bool, bool]  # (N→D, E→D, N→E)
    rec_block: int            # 회복 규칙이 걸리는 N 블록 길이(0=없음)
    not_one: bool
    day0_single_n_ok: bool
    n_min: int
    n_cap: int
    mid: bool = False         # MID 병동(use_mid) — E→M·N→M 금지(EI-14r2)
    blocked: frozenset = frozenset()  # 배정 차단일(0-based 상대 일자) — 쉬는 날(O 로만 · 1N 요구 해제)(EI-10g1)
    forced_off: frozenset = frozenset()   # 엔진이 O 로 박는 날(고정 칸이 없는 날만) — 금지보다 앞선다(EI-10g4)
    forbidden: Mapping[int, frozenset] = field(default_factory=dict)  # 날 → 못 서는 근무코드(O 포함 가능)(EI-10g4)


def _transition_banned(rules: _Rules, day: int, prev: str, code: str) -> bool:
    """`prev`→`code` 전이가 금지인가 — 두 칸이 모두 고정이면 엔진이 면제한다(MID 의 `E/N→M` 은 면제 없음).

    ※ MID 의 `M→D` 금지는 여기서 판정하지 않는다 — 한쪽만 고정이면 DP 가 자유 칸을 O 로 보낼 수 있고 양끝 고정이면 엔진이
      면제하므로 어떤 입력도 이 규칙으로는 막히지 않는다(관측 불가 — 7.2e5 무작위 대조 0건, EI-14C1r).
    """
    if rules.mid and code == "M" and prev in ("E", "N"):
        return True
    if day in rules.fixed and (day - 1) in rules.fixed:
        return False
    ban_n_to_d, ban_e_to_d, ban_n_to_e = rules.bans
    if code == "D":
        return (prev == "N" and ban_n_to_d) or (prev == "E" and ban_e_to_d)
    return code == "E" and prev == "N" and ban_n_to_e


def _recovery_next(rules: _Rules, day: int, code: str, night_run: int, off_rem: int) -> int | None:
    """다음 날 강제 OFF 잔여 — 이 선택이 회복 규칙을 어기면 None.

    `night_run` 은 직전까지의 연속 N 수다. 길이 `rec_block` 이상 N 블록이 어제 끝났고(오늘이 N 이
    아니다) 오늘이 월말 이틀 안쪽이 아니면, 오늘과 내일이 OFF 여야 한다.
    ※ 엔진의 3N→2OFF 는 NNN 이 끝나는 모든 자리에 걸리지만 연속 N 상한이 3 이하(엔진은 2/3)라
      블록 끝과 같다. 2N→2OFF 는 블록 끝에만 걸린다(lex:2798-2829).
    """
    if off_rem > 0:
        return off_rem - 1 if code == "O" else None
    block_ended = bool(rules.rec_block) and code != "N" and night_run >= rules.rec_block
    if not block_ended or day > rules.num_days - 2:
        return 0
    return 1 if code == "O" else None


def _exceeds_limits(rules: _Rules, work_run: int, night_run: int, nights: int) -> bool:
    """연속근무·연속 N·월 N 상한을 넘는가."""
    if rules.max_work is not None and work_run > rules.max_work:
        return True
    if rules.max_night_run is not None and night_run > rules.max_night_run:
        return True
    return nights > rules.n_cap


def _starts_single_night(rules: _Rules, day: int, prev: str, code: str) -> bool:
    """1N 금지 하에서 이 선택이 이웃 N 을 요구하는 단독 N 의 시작인가(1일 고정 N 은 면제)."""
    if not rules.not_one or code != "N" or prev == "N":
        return False
    return not (day == 0 and rules.day0_single_n_ok)


def _step(rules: _Rules, state: _State, code: str) -> _State | None:
    """상태에서 `code` 를 고른 다음 상태(규칙을 어기면 None)."""
    day, prev, work_run, night_run, off_rem, need_n, nights = state
    if need_n and code != "N" and day not in rules.blocked:
        return None
    next_off = _recovery_next(rules, day, code, night_run, off_rem)
    if next_off is None or _transition_banned(rules, day, prev, code):
        return None
    new_work = work_run + 1 if code != "O" else 0
    new_night = night_run + 1 if code == "N" else 0
    new_nights = nights + (1 if code == "N" else 0)
    if _exceeds_limits(rules, new_work, new_night, new_nights):
        return None
    need_next = _starts_single_night(rules, day, prev, code)
    return (day + 1, code, new_work, new_night, next_off, need_next, new_nights)


def _feasible(rules: _Rules) -> bool:
    """첫 상태에서 끝까지 갈 수 있는 완성이 하나라도 있는가(상태 캐시는 호출마다 새로 만든다)."""
    free_choices = rules.work_allowed | {"O"}

    @cache
    def go(state: _State) -> bool:
        day, *_, need_n, nights = state
        if day == rules.num_days:
            return (not need_n) and rules.n_min <= nights <= rules.n_cap
        if day in rules.blocked:
            choices = {"O"}
        elif day in rules.fixed:
            choices = {rules.fixed[day]}               # 고정 칸이 이긴다(엔진: 금지·강제 OFF 는 고정 칸 날을 무시)
        elif day in rules.forced_off:
            choices = {"O"}                            # 강제 OFF 는 금지보다 앞선다(엔진: O 고정 칸이 되어 금지 판정을 건너뜀)
        else:
            choices = free_choices - rules.forbidden.get(day, frozenset())
        return any(
            nxt is not None and go(nxt) for nxt in (_step(rules, state, code) for code in choices))

    try:
        return go((0, "O", 0, 0, 0, False, 0))
    finally:
        go.cache_clear()


def _positive_or_none(value: int | None) -> int | None:
    """1 이상인 한도만 상한으로 쓴다(None·0·음수 = 상한 없음)."""
    return int(value) if value and value >= 1 else None


def nurse_sequence_infeasible(
    *,
    num_days: int,
    allowed: set[str],            # 허용 근무(work) 집합. 비면 D/E/N 전부. O 는 항상 가능.
    fixed: dict[int, str],        # {day: shift} 고정셀 (0-based day)
    max_consecutive_work: int | None = None,
    max_consecutive_nights: int | None = None,
    ban_n_to_d: bool = False,
    ban_e_to_d: bool = False,
    ban_n_to_e: bool = False,
    two_offs_after_two_nig: bool = False,
    two_offs_after_three_nig: bool = False,
    ban_single_night: bool = False,
    n_min: int = 0,
    n_max: int | None = None,
    single_n_allowed: bool = False,
    day0_single_n_ok: bool = False,
    use_mid: bool = False,
    blocked: frozenset | set | None = None,
    forced_off: frozenset | set | None = None,
    forbidden: Mapping[int, Collection[str]] | None = None,
) -> bool:
    """혼자서 모든 규칙을 지키는 완성이 **존재하지 않으면 True**(=infeasible), 있으면 False.

    `single_n_allowed`: 월 N 상한 1 간호사 — 엔진이 1N 금지를 면제한다.
    `day0_single_n_ok`: 월 1일 고정 N — 엔진이 그날의 1N 금지를 면제한다.
    `max_consecutive_nights` 가 없으면 연속 N 상한은 회복 블록 길이(없으면 무제한)다.
    `use_mid`: MID 병동 — `allowed` 는 M 을 포함해 넘기고(없으면 D/E/N/M 전부), E→M·N→M·M→D 를 엔진처럼 막는다.
    `blocked`: 배정 차단일(0-based) — 그날은 근무 없이 쉰다(연속 근무·연속 N 이 끊기고 회복 OFF 로 센다). 1N 요구는 풀린다(느슨한 쪽).
    `forced_off`·`forbidden`(EI-10g4): 단계 B 제약 — 호출부가 **고정 칸이 있는 날을 이미 뺀** 값을 준다(고정이 이긴다). 강제 OFF 날은 O 뿐이고,
    금지 코드(D/E/N/M/O 대문자)는 그 날 선택지에서 빠진다. 알 수 없는 코드는 무시한다(선택지가 줄지 않는 쪽 = 느슨한 쪽).
    """
    if num_days <= 0:
        return False
    rec_block = 2 if two_offs_after_two_nig else (3 if two_offs_after_three_nig else 0)
    night_run = _positive_or_none(max_consecutive_nights)
    rules = _Rules(
        num_days=num_days,
        work_allowed=frozenset(allowed) if allowed else frozenset(_WORK + (("M",) if use_mid else ())),
        fixed={int(day): str(code).upper() for day, code in (fixed or {}).items()},
        max_work=_positive_or_none(max_consecutive_work),
        max_night_run=night_run if night_run is not None else (rec_block or None),
        bans=(ban_n_to_d, ban_e_to_d, ban_n_to_e),
        rec_block=rec_block,
        not_one=bool(ban_single_night) and not single_n_allowed and num_days > 1,
        day0_single_n_ok=day0_single_n_ok,
        n_min=n_min,
        n_cap=int(n_max) if n_max is not None else num_days,
        mid=bool(use_mid),
        blocked=frozenset(int(d) for d in (blocked or ())),
        forced_off=frozenset(int(d) for d in (forced_off or ())),
        forbidden={int(d): frozenset(str(c).upper() for c in codes) & _KNOWN_CODES for d, codes in (forbidden or {}).items()},
    )
    return not _feasible(rules)
