"""긴급대체 연쇄 수정안 — 결원 한 칸을 메우는 1인 스왑 / 다인 연쇄 제안.

문제
    확정된 근무표에서 간호사 한 명이 특정 일자의 근무를 못 하게 됐을 때,
    솔버가 하드로 거는 규칙을 전부 지키면서 그 칸을 메울 방법을 찾는다.

접근 — y축(결원일 1열)만 연다
    다른 날짜를 고정하면 문제가 "이 한 칸을 누가 채울 수 있나"로 좁혀지고,
    후보끼리 상호작용이 없어 CP-SAT 없이 후보별 독립 검사로 풀린다.
    CP-SAT 경로는 문제 크기와 무관한 고정비(수 초)가 있어 화면 응답에 못 쓴다.

3단 구조
    1) 스윕   빈 칸을 메울 후보를 하드 규칙으로 추린다 → 1인 스왑
    2) 연쇄   후보를 데려오면 그 자리가 비므로 1)을 재귀 적용한다 → n인 연쇄
    3) 재검증 만들어진 제안을 결과 상태로 통째로 다시 검사한다
              (1)은 이동 하나씩 보는 증분 검사라 이동 간 간섭을 놓친다)

하드/소프트 기준
    사람이 법규·내규로 나누지 않는다. **솔버(fallback_lex)가 `m.Add` 로 강제하면 하드,
    슬랙으로 최소화하면 소프트**다. 근무표를 만든 주체가 솔버이므로 기준이 다르면
    "생성은 되는데 대체는 안 되는" 모순이 생긴다.

    상세 근거는 docs/EMERGENCY_REPLACEMENT_YAXIS_CHAIN_RESEARCH_2026-07-29.md 참조.

읽기 전용 — 제안만 만들고 DB 를 쓰지 않는다. 적용은 프론트가 기존 `POST /save` 로 한다.
"""

from __future__ import annotations

import calendar
import copy
import json
import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from agents_v2.tools import constraint_tools, schedule_tools, shift_tools
from db.models import (
    BannedWantedEntry, FixedWantedEntry, Nurse, NurseAllowedShiftPeriod, ScheduleEntry,
)
from db.roster_config import NurseRosterConfig
from schemas.replacement_schema import ChainMove, ChainProposal
from services.cp_sat.allowed_shift_types import normalize_allowed_shift_codes
from services.mutual_exclusion_period import resolve_mutual_exclusion_days_for_month
from services.nurse_monthly_limit_service import fetch_effective_monthly_limits_by_nurse
from services.nurse_period_resolver import (
    fetch_periods, resolve_asof, weekend_off_ids_asof,
)
from services.wanted_service import _compute_weekly_off_days

logger = logging.getLogger(__name__)

# 탐색 파라미터
#: 차선안에서만 열 수 있는 규칙. **1N 금지 하나뿐이다.**
#:
#: 필요인원(커버리지)·등급 최소인원·팀 최소인원도 열지 않는다. 솔버가
#: `soften_daily_coverage=False` 로 하드 고정하며 "무너지면 환자 안전 영향 큼"
#: 이라고 못박은 항목이다(`cp_sat/fallback_lex.py:535`).
#:
#: 연속근무 상한 · ND/ED/NE 전환금지 · 나이트 연속 상한 · 3N2O/2N2O 회복은
#: 하드락 정책(CLAUDE.md) 대상이라 어떤 경우에도 열지 않는다. 인원·등급·팀 같은
#: 구조적 제약도 마찬가지다.
#:
#: 실측 근거 — 1N 만 열어도 커버가 83.6%→97.8% 로 올라간다. 나머지를 전부 열어도
#: 98.7% 라 0.9%p 차이뿐이다. 그 폭을 얻자고 안전 규칙을 열 이유가 없다.
RELAXABLE_RULES = ("not_one_night",)
#: 차선안이 감수해도 되는 신규 하드 위반 개수 상한. 넘으면 제안하지 않는다.
MAX_FALLBACK_VIOLATIONS = 4

MAX_CHAIN_DEPTH = 3      # 연쇄에 동원할 최대 인원
RAW_PROPOSAL_LIMIT = 60  # 순위 매기기 전 원안 수집 상한
# 2단 자유 구간 — 결원일로부터 **앞으로만** 며칠까지 여는가.
#
#   과거는 절대 열지 않는다. 긴급대체는 당일·직전에 요청되는데 지나간 날의 근무는
#   이미 수행됐다. 그걸 바꾸는 제안은 실행 불가능하다.
#   (±3일로 과거까지 열면 73.4% 가 풀리지만 그 수치는 무효다)
#
#   전수 실측(1단 미해결 244건, 결원일 이후만):
#     +1일 41.0% | +2일 54.9% | +3일 60.7% | +5일 60.7%
#   +3 이상은 더 안 늘어난다.
LNS_FORWARD_DAYS = 3

# 한 칸을 비우려고 바꿀 수 있는 최대 셀 수(결원 셀 포함).
#
#   상한이 없으면 18칸짜리 안이 나온다 — 한 칸 비우자고 다른 근무자 열일곱 칸을
#   흔드는 건 현장에서 받아들여지지 않는다.
#
#   ★ 상한을 하드 제약으로 걸어도 "더 적게 바꾸는 차선해"는 나오지 않는다.
#     커버리지·시퀀스가 빡빡해 변경량은 문제 구조가 정하는 값이지 선택이 아니다.
#     (사후에 버린 수치와 제약으로 건 수치가 완전히 동일)
#
#   전수 실측(1단 미해결 244건, 결원일~+3일):
#     ≤4칸 16.4% | ≤6칸 39.8% | ≤8칸 51.2% | ≤10칸 58.6% | 무제한 60.7%
#   중앙값은 어느 상한에서도 5칸 — 큰 변경은 소수의 꼬리다.
LNS_EXPANDED_CELLS = 12
LNS_MAX_CHANGED_CELLS = 6
LNS_LOCAL_TIME_LIMIT = 5 # 전용 CP-SAT 모델 solve 상한(초). 실측 74ms 라 넉넉하다

# 소프트 점수 가중치 — 하드로 거른 뒤 남은 안들의 우선순위만 정한다
W_CHAIN_STEP = 2.0       # 연쇄가 길수록 불리
W_LOSE_OFF = 3.0         # 휴무자를 부르면 휴무가 사라진다
W_SHIFT_CHANGE = 1.0     # 근무 중인 사람의 근무형 변경
W_ADD_NIGHT = 1.5        # 나이트 추가 부담
W_SOFT_VIOLATION = 4.0   # 솔버가 소프트로 두는 규칙의 신규 위반

# DB 컬럼명 → NurseRosterConfig 속성명 (이름이 다른 것만)
_CONFIG_ALIAS = {
    "max_conseq_work": "max_consecutive_work_days",
    "max_nig_per_month": "max_night_shifts_per_month",
    "two_offs_per_week": "enforce_two_offs_per_week",
}
_CONFIG_BOOL_KEYS = (
    "not_one_night", "nod_noe", "banned_day_after_eve",
    "two_offs_after_two_nig", "two_offs_after_three_nig",
    "sequential_offs", "max_conseq_off",
    # 지금은 DB 에 컬럼이 없어 dict 에 키 자체가 없지만, 컬럼이 생기면 값이 None 이 되어
    # `.get(key, True)` 가 None 을 돌려주고 제약이 조용히 꺼진다. 미리 채워 둔다.
    "weekend_off_only_enable", "enforce_4o_hard",
)

_RE_PAREN = re.compile(r"\([^)]*\)")
_RE_NUM = re.compile(r"\d+")

# (nurse_id, date, from_shift, to_shift)
Move = Tuple[str, date, str, str]
# {nurse_id: [(date, shift_id), ...]} — 날짜 오름차순
Sequences = Dict[str, List[Tuple[date, str]]]

#: (소프트 점수, 이동 목록, 소프트 경고, 신규 하드 위반)
Scored = Tuple[float, List[Move], List[str], List[str]]


@dataclass
class ChainContext:
    """요청당 1회 로드하는 판정 재료. 후보 검사는 전부 이 위에서 메모리로 돈다."""

    sequences: Sequences = field(default_factory=dict)
    config: Dict[str, Any] = field(default_factory=dict)
    categories: Dict[str, Set[str]] = field(default_factory=dict)
    main_of: Dict[str, str] = field(default_factory=dict)
    off_code: str = "O"
    name_of: Dict[str, str] = field(default_factory=dict)
    grade_of: Dict[str, Optional[int]] = field(default_factory=dict)
    allowed_of: Dict[str, Set[str]] = field(default_factory=dict)
    fixed_of: Dict[str, Optional[str]] = field(default_factory=dict)
    grade_min: Dict[str, Any] = field(default_factory=dict)
    coverage_req: Dict[str, int] = field(default_factory=dict)
    mutex: Dict[str, Any] = field(default_factory=dict)
    monthly_limits: Dict[str, Any] = field(default_factory=dict)
    weekly_off: Dict[str, Set[int]] = field(default_factory=dict)
    banned: Dict[str, Set[str]] = field(default_factory=dict)
    fixed_cells: Set[Tuple[str, date]] = field(default_factory=set)
    weekend_off: Set[str] = field(default_factory=set)
    weekend_days: Set[int] = field(default_factory=set)
    blocked_days: Dict[str, Set[int]] = field(default_factory=dict)
    team_of: Dict[str, Optional[str]] = field(default_factory=dict)
    team_min: Dict[str, Dict[str, int]] = field(default_factory=dict)
    team_min_soft: bool = False


# ── 설정 ────────────────────────────────────────────────────────────────────


def _effective_config(raw: Optional[dict]) -> Dict[str, Any]:
    """DB 값이 비면 솔버 dataclass 디폴트로 채운다.

    `get_roster_config` 는 DB row 그대로라 미설정 항목이 None 인데, 솔버는
    `NurseRosterConfig` 디폴트로 동작한다. 안 맞추면 대체 판정이 솔버와 달라진다.
    """
    defaults = NurseRosterConfig()
    out = dict(raw or {})
    for db_key, attr in _CONFIG_ALIAS.items():
        if out.get(db_key) is None:
            out[db_key] = getattr(defaults, attr, None)
    for key in _CONFIG_BOOL_KEYS:
        if out.get(key) is None:
            out[key] = getattr(defaults, key, None)
    if out.get("three_seq_nig") is None:
        # three_seq_nig 는 bool 토글, dataclass 는 max_consecutive_nights 수치
        out["three_seq_nig"] = getattr(defaults, "max_consecutive_nights", 3) >= 3
    return out


def _load_config(db: Session, group_id: str) -> Dict[str, Any]:
    """그룹 **최신** config — 생성기(`_fetch_latest_config`)와 같은 출처.

    `schedule.config_id` 가 아니다. 생성기는 요청 config_id 가 없으면 그룹 최신
    config 로 근무표를 만들므로, 대체 판정도 같은 것을 봐야 "생성은 되는데 대체는
    안 되는" 모순이 안 생긴다.

    실제로 중환자실1은 `schedule.config_id=1618` 이 `max_conseq_work=1`(연속근무 1일)
    인데 최신 1631 은 5 다. 1618 로 판정하면 원본 근무표가 대량 위반 상태가 되어
    연속근무 제약이 사실상 무력화된다.
    """
    return _effective_config(constraint_tools.get_roster_config(db, group_id))


# ── 규칙 검사 ───────────────────────────────────────────────────────────────


def _runs(seq: Sequence[Tuple[date, str]], id_set: Set[str]) -> List[Tuple[int, int]]:
    """id_set 에 속한 코드의 날짜-인접 연속 구간 → [(시작 인덱스, 길이)]."""
    out: List[Tuple[int, int]] = []
    i = 0
    while i < len(seq):
        if seq[i][1] not in id_set:
            i += 1
            continue
        j = i
        while (j + 1 < len(seq) and seq[j + 1][1] in id_set
               and (seq[j + 1][0] - seq[j][0]).days == 1):
            j += 1
        out.append((i, j - i + 1))
        i = j + 1
    return out


def night_recovery_violations(
    seq: Sequence[Tuple[date, str]], categories: dict, config: dict,
) -> List[str]:
    """3N 직후 OFF 2회 / 2N 직후 OFF 2회 — 솔버 하드(fallback_lex 2021·2158)."""
    two_n = bool(config.get("two_offs_after_two_nig"))
    three_n = bool(config.get("two_offs_after_three_nig"))
    if not (two_n or three_n):
        return []
    out: List[str] = []
    for start, length in _runs(seq, categories["night"]):
        if length not in (2, 3) or (length == 2 and not two_n) or (length == 3 and not three_n):
            continue
        end = start + length - 1
        for offset in (1, 2):
            idx = end + offset
            if idx >= len(seq) or (seq[idx][0] - seq[idx - 1][0]).days != 1:
                break
            if seq[idx][1] in categories["work"]:
                out.append(f"{length}N 직후 OFF {offset}일째 아님")
                break
    return out


def four_off_violations(seq: Sequence[Tuple[date, str]], ctx: ChainContext) -> List[str]:
    """순수 O 4연속 금지 — 솔버 하드(fallback_lex 917 `is_pure_o x4 <= 3`).

    `enforce_4o_hard` 는 DB 에 값이 없으면 솔버 디폴트 True 라 대개 활성이다.
    휴가/공가는 '순수 O' 가 아니므로 OFF 코드만 센다.
    """
    if not bool(ctx.config.get("enforce_4o_hard", True)):
        return []
    return [f"순수 OFF {length}연속(4연속 금지)"
            for _start, length in _runs(seq, {ctx.off_code}) if length >= 4]


def grade_shortage(
    assigned: Sequence[str], grade_of: Dict[str, Optional[int]], per_grade: Optional[dict],
) -> List[str]:
    """등급 최소인원 — 누적(grade<=g) 의미. 인원 0 등급은 상위 등급으로 cascade."""
    if not per_grade:
        return []
    population = Counter(g for g in grade_of.values() if g is not None)
    if not population:
        return []
    effective = _cascade_grade_targets(per_grade, population)
    grades = [grade_of.get(n) for n in assigned]
    out = []
    for grade, target in sorted(effective.items()):
        cumulative = sum(1 for g in grades if g is not None and g <= grade)
        if cumulative < target:
            out.append(f"등급 최소인원 미달(grade<={grade} {cumulative}/{target})")
    return out


def _cascade_grade_targets(per_grade: dict, population: Counter) -> Dict[int, int]:
    """인원이 0인 등급의 요구를 인원이 있는 상위 등급으로 올린다."""
    existing = sorted(population)

    def cascade(grade: int) -> int:
        if population.get(grade, 0) > 0:
            return grade
        higher = [g for g in existing if g > grade]
        return higher[0] if higher else grade

    out: Dict[int, int] = {}
    for raw_grade, raw_count in per_grade.items():
        try:
            grade, count = int(raw_grade), int(raw_count)
        except (TypeError, ValueError):
            continue
        if count <= 0:
            continue
        target = cascade(grade)
        out[target] = max(out.get(target, 0), count)
    return out


def team_min_shortage(assignment: Dict[str, List[str]], ctx: ChainContext) -> List[str]:
    """팀 최소인원 — (일, 근무)마다 **서로 다른 팀**을 min(요구인원, 팀수)개 커버해야 한다.

    솔버 규칙 그대로다(`team_constraints.py:120~155`).
      present_t = 1  ⟺  팀 t 가 그 근무에 min_t 명 이상 배치
      covered = Σ present_t  >=  target = min(need, 팀수)
    "3자리에 3팀 못 넣음"이 위반이고 "2자리에 2팀"은 정상이다.
    `team_min_soft_fallback` 이 켜져 있으면 솔버가 슬랙으로 다루므로 검사하지 않는다.
    """
    if not ctx.team_min or ctx.team_min_soft:
        return []
    out = []
    for main, need in ctx.coverage_req.items():
        present, eligible = 0, 0
        for team_id, per_code in ctx.team_min.items():
            min_t = int(per_code.get(main, 0) or 0)
            if min_t <= 0:
                continue
            members = [n for n, t in ctx.team_of.items() if t == team_id]
            if len(members) < min_t:
                continue          # 그 팀은 애초에 못 채운다 — 대상에서 제외(솔버와 동일)
            eligible += 1
            if sum(1 for n in assignment[main] if ctx.team_of.get(n) == team_id) >= min_t:
                present += 1
        target = min(int(need or 0), eligible)
        if target > 0 and present < target:
            out.append(f"{main} 팀 커버 부족({present}/{target}팀)")
    return out


def _kind(message: str) -> str:
    """위반 메시지 → 규칙 종류 키. 일수·날짜를 지운다."""
    stripped = _RE_PAREN.sub("", message).split("—")[0]
    return _RE_NUM.sub("#", stripped).strip()


def delta_violations(before: Sequence[str], after: Sequence[str]) -> List[str]:
    """규칙 종류별 '건수 증가분'만 새 위반으로 본다.

    메시지에 일수가 박혀 있어 문자열 차집합을 쓰면 '순수 OFF 5연속 → 4연속' 처럼
    오히려 완화된 경우까지 새 위반으로 잘못 잡는다. 그래서 세는 건 정규화한
    종류로 하되, **돌려주는 건 원문**이다 — 정규화 문자열을 그대로 내보내면
    화면에 `E 인원 #/#` 처럼 숫자가 지워진 채 나가 판단할 수가 없다.
    """
    before_count = Counter(_kind(m) for m in before)
    seen: Counter = Counter()
    out = []
    for message in after:
        kind = _kind(message)
        seen[kind] += 1
        if seen[kind] > before_count.get(kind, 0):
            out.append(message)   # 정규화한 키가 아니라 **원문**을 돌려준다
    return out


# ── 상태 조작 ───────────────────────────────────────────────────────────────


def _day_assignment(seqs: Sequences, ctx: ChainContext, day: date) -> Dict[str, List[str]]:
    """그 날짜의 대표 근무형 → 배정 간호사 목록."""
    out: Dict[str, List[str]] = defaultdict(list)
    for nurse_id, seq in seqs.items():
        for entry_date, shift in seq:
            if entry_date == day:
                out[ctx.main_of.get(shift, shift)].append(nurse_id)
                break
    return out


def _shift_at(seqs: Sequences, nurse_id: str, day: date) -> Optional[str]:
    for entry_date, shift in seqs.get(nurse_id, ()):
        if entry_date == day:
            return shift
    return None


def _move(seqs: Sequences, nurse_id: str, day: date, shift: str) -> Sequences:
    """한 칸만 바꾼 새 상태(나머지는 원본 리스트를 공유하는 얕은 복사)."""
    out = dict(seqs)
    out[nurse_id] = [(d, shift if d == day else s) for d, s in seqs[nurse_id]]
    return out


def hard_violations(
    nurse_id: str,
    day: date,
    shift: str,
    seqs: Sequences,
    ctx: ChainContext,
) -> List[str]:
    """그 간호사를 (day, shift) 에 둔 상태에서의 하드 규칙 위반."""
    main = ctx.main_of.get(shift, shift)
    out: List[str] = []
    allowed = ctx.allowed_of.get(nurse_id) or set()
    if allowed and main not in allowed:
        out.append(f"허용 근무형 아님({sorted(allowed)})")
    fixed = ctx.fixed_of.get(nurse_id)
    if fixed and fixed != shift:
        out.append(f"고정근무자({fixed})")
    if day.day in ctx.weekly_off.get(nurse_id, set()):
        out.append("주휴일")
    out += _weekend_off_violations(nurse_id, day, shift, ctx)
    if main in ctx.banned.get(nurse_id, set()):
        out.append("기피근무")
    if day.day in ctx.blocked_days.get(nurse_id, set()) and shift in ctx.categories["work"]:
        # 파견·병동이동·휴직 아웃바운드 기간 + 프리셉티 follow 구간.
        # 솔버는 iter_nurse_days 에서 이 날짜를 아예 빼므로 배정 자체가 없다.
        out.append("관할 밖(파견·이동·휴직·프리셉티)")
    out += _monthly_night_limit_violations(nurse_id, main, seqs, ctx)
    seq = seqs[nurse_id]
    out += schedule_tools._streak_warnings(seq, ctx.categories, ctx.config)
    out += schedule_tools._adjacency_warnings(seq, ctx.categories, ctx.config)
    out += schedule_tools._monthly_night_warning(seq, ctx.categories, ctx.config)
    out += night_recovery_violations(seq, ctx.categories, ctx.config)
    out += four_off_violations(seq, ctx)
    return out


def _weekend_off_violations(
    nurse_id: str, day: date, shift: str, ctx: ChainContext,
) -> List[str]:
    """주말휴무 대상자 제약 — 솔버가 `fallback_lex.py:1011/1023` 에서 양방향 하드로 건다.

        주말(토/일)  m.Add(X(n, d, off) == 1)   → OFF 강제 = 근무 배정 불가
        평일(월~금)  m.Add(X(n, d, off) == 0)   → OFF 금지 = 반드시 근무

    `weekend_off_only_enable` 은 DB 에 값이 없으면 솔버 디폴트 True 다.
    SSOT 는 `nurse_weekendoff_period` 이며 `nurses.is_weekend_off` 캐시가 아니다.
    """
    if nurse_id not in ctx.weekend_off:
        return []
    is_weekend = day.day in ctx.weekend_days
    if is_weekend and shift in ctx.categories["work"]:
        return ["주말휴무 대상자의 주말 근무"]
    if not is_weekend and shift not in ctx.categories["work"]:
        return ["주말휴무 대상자의 평일 휴무"]
    return []


def _monthly_night_limit_violations(
    nurse_id: str, main: str, seqs: Sequences, ctx: ChainContext,
) -> List[str]:
    """간호사별 월 나이트 개인 한도(n_max / n_exact)."""
    if main != "N":
        return []
    limit = (ctx.monthly_limits.get(nurse_id) or {})
    cap = limit.get("n_max") or limit.get("n_exact")
    if cap is None:
        return []
    count = sum(1 for _d, s in seqs[nurse_id] if ctx.main_of.get(s, s) == "N")
    return [f"월 나이트 개인한도 초과({count}/{cap})"] if count > int(cap) else []


def soft_warnings(moves: Sequence[Move], seqs: Sequences, ctx: ChainContext) -> List[str]:
    """솔버가 소프트로 두는 규칙 — 배제하지 않고 경고로 노출한다.

    상호배제는 fallback_lex:3322 에서 "동결 부등식이라 infeasible 불가(soft)" 다.
    """
    day = moves[0][1]
    assignment = _day_assignment(seqs, ctx, day)
    out = []
    for nurse_id, _day, _from, to_shift in moves:
        main = ctx.main_of.get(to_shift, to_shift)
        rule = ctx.mutex.get(nurse_id) or {}
        partner, days = rule.get("partner_id"), (rule.get("days") or set())
        if partner and (day.day - 1) in days and partner in assignment[main]:
            out.append(f"{_name(ctx, nurse_id)}: 상호배제 동석({_name(ctx, partner)})")
    return out


def absence_side_effects(
    ctx: ChainContext, state: Sequences, target_nurse_id: str, day: date, shift: str,
) -> List[str]:
    """결원 처리 **자체**가 결원자에게 만드는 위반.

    예) N N N 가운데를 비우면 앞뒤가 각각 단독 나이트가 되고, 저연차가 빠지면
    그 근무의 등급 최소인원이 미달된다.
    추천이 만든 게 아니라 "그날 못 나옴"이라고 한 순간 생기는 것이라 피할 수 없다.
    배제 사유로 쓰지 않고 경고로만 알린다 — 사용자가 알고 판단해야 한다.
    """
    before = hard_violations(target_nurse_id, day, shift, ctx.sequences, ctx)
    after = hard_violations(target_nurse_id, day, ctx.off_code, state, ctx)
    out = [f"{_name(ctx, target_nurse_id)}: 결원 처리로 {v}"
           for v in delta_violations(before, after)]
    return out + [f"결원 처리로 {v}" for v in delta_violations(
        _structural_issues(ctx.sequences, ctx, day),
        _structural_issues(state, ctx, day))]


def _name(ctx: ChainContext, nurse_id: str) -> str:
    return ctx.name_of.get(nurse_id, nurse_id)


# ── 탐색 ────────────────────────────────────────────────────────────────────


def _sweep(
    day: date, shift: str, seqs: Sequences, ctx: ChainContext, exclude: Set[str],
) -> List[Tuple[str, str, bool]]:
    """(day, shift) 를 메울 수 있는 후보 → [(nurse_id, 현재 근무, 빈자리 생김 여부)].

    차선안을 찾을 때는 `_relaxed_ctx` 사본을 넘긴다. 검사를 건너뛰는 게 아니라
    **1N 금지만 끈 기준으로 똑같이 검사**한다 — 그래야 ND·회복·연속근무처럼
    열면 안 되는 규칙이 함께 새지 않는다.
    """
    main = ctx.main_of.get(shift, shift)
    assignment = _day_assignment(seqs, ctx, day)
    out = []
    for nurse_id in seqs:
        if nurse_id in exclude:
            continue
        if (nurse_id, day) in ctx.fixed_cells:
            # 확정 원티드 셀은 건드리지 않는다. **절대 배제**여야 한다 —
            # hard_violations 에 넣으면 before/after 양쪽에 잡혀 델타가 0 이 되고
            # 그대로 통과한다(실측: 중환자실1 19건·42병동 84건이 새고 있었다).
            continue
        current = _shift_at(seqs, nurse_id, day)
        if current is None or ctx.main_of.get(current, current) == main:
            continue  # 배정 없음 / 이미 그 근무 — 옮겨도 결원이 안 메워진다
        moved = _move(seqs, nurse_id, day, shift)
        before = hard_violations(nurse_id, day, current, seqs, ctx)
        if delta_violations(before, hard_violations(nurse_id, day, shift, moved, ctx)):
            continue  # 이 기준에 없던 하드 위반이 새로 생긴다
        out.append((nurse_id, current, _leaves_gap(nurse_id, current, day, assignment, ctx)))
    return out


def _leaves_gap(
    nurse_id: str, current: str, day: date, assignment: Dict[str, List[str]], ctx: ChainContext,
) -> bool:
    """이 후보를 데려가면 원래 근무의 인원이 요구치 아래로 떨어지는가."""
    if current not in ctx.categories["work"]:
        return False
    main = ctx.main_of.get(current, current)
    required = ctx.coverage_req.get(main, 0)
    return bool(required and len(assignment[main]) - 1 < required)


def _find_chains(
    day: date,
    shift: str,
    seqs: Sequences,
    ctx: ChainContext,
    depth: int,
    used: Set[str],
    path: List[Move],
    out: List[List[Move]],
) -> None:
    """빈 (day, shift) 를 메우는 연쇄를 깊이 제한으로 탐색한다."""
    if len(out) >= RAW_PROPOSAL_LIMIT or depth > MAX_CHAIN_DEPTH:
        return
    candidates = _sweep(day, shift, seqs, ctx, used)

    # 연쇄가 안 필요한 후보부터 전부 담는다. 재귀를 섞으면 깊은 연쇄가 수집 한도를
    # 먼저 채워, 더 좋은(=짧은) 안이 밀려난다.
    for nurse_id, current, leaves_gap in candidates:
        if leaves_gap:
            continue
        out.append(path + [(nurse_id, day, current, shift)])
        if len(out) >= RAW_PROPOSAL_LIMIT:
            return

    for nurse_id, current, leaves_gap in candidates:
        if not leaves_gap or len(out) >= RAW_PROPOSAL_LIMIT:
            continue
        _find_chains(day, current, _move(seqs, nurse_id, day, shift), ctx,
                     depth + 1, used | {nurse_id},
                     path + [(nurse_id, day, current, shift)], out)


# ── 재검증 ──────────────────────────────────────────────────────────────────


def _structural_issues(seqs: Sequences, ctx: ChainContext, day: date) -> List[str]:
    """인원 · 등급 · 팀 — **차선안에서도 절대 못 여는** 것.

    이게 깨지면 그 날 근무가 안 돌아간다. 대체를 하는 이유 자체가 사라지므로
    사용자 판단에 맡길 성질이 아니다.
    """
    assignment = _day_assignment(seqs, ctx, day)
    out = []
    for main, required in ctx.coverage_req.items():
        if required and len(assignment[main]) < required:
            out.append(f"{main} 인원 {len(assignment[main])}/{required}")
    for main in ("D", "E", "N"):
        for issue in grade_shortage(assignment[main], ctx.grade_of, (ctx.grade_min or {}).get(main)):
            out.append(f"{main} {issue}")
    return out + team_min_shortage(assignment, ctx)


def _personal_issues(
    seqs: Sequences, ctx: ChainContext, day: date, who: Set[str],
) -> List[str]:
    """관련자 개인 근무규칙 — 차선안에서 **사용자가 보고 감수할 수 있는** 것."""
    out = []
    for nurse_id in who:
        current = _shift_at(seqs, nurse_id, day) or ctx.off_code
        out += [f"{_name(ctx, nurse_id)}: {v}"
                for v in hard_violations(nurse_id, day, current, seqs, ctx)]
    return out


def _state_issues(
    seqs: Sequences, ctx: ChainContext, day: date, who: Set[str],
) -> List[str]:
    """그 날짜 기준 하드 위반 전체 — 구조 + 개인."""
    return _structural_issues(seqs, ctx, day) + _personal_issues(seqs, ctx, day, who)


def _apply(seqs: Sequences, moves: Sequence[Move]) -> Sequences:
    out = seqs
    for nurse_id, day, _from, to_shift in moves:
        out = _move(out, nurse_id, day, to_shift)
    return out


def _verify(
    moves: Sequence[Move], base: Sequences, ctx: ChainContext, origin: Sequences,
) -> Tuple[List[str], List[str]]:
    """제안을 결과 상태로 다시 검사해 **(구조적 위반, 개인규칙 위반)** 을 준다.

    판정은 '원본 대비 악화 금지'다. 원본 근무표가 이미 깨뜨리고 있는 항목까지
    제안 탓으로 돌리면 그 날짜의 모든 안이 자동 탈락한다. 새로 생긴 위반만 센다.

    구조/개인을 나누는 이유는 차선안 때문이다. 구조적 위반은 어떤 경우에도
    제안하지 않지만, 개인규칙 위반은 표시한 뒤 사용자가 고르게 한다.
    """
    day = moves[0][1]
    who = {m[0] for m in moves}
    after = _apply(base, moves)
    return (
        sorted(delta_violations(_structural_issues(origin, ctx, day),
                                _structural_issues(after, ctx, day))),
        _personal_delta(origin, after, ctx, day, who),
    )


def _personal_delta(
    before: Sequences, after: Sequences, ctx: ChainContext, day: date, who: Set[str],
) -> List[str]:
    """관련자별로 델타를 내고 이름은 **나중에** 붙인다.

    이름을 먼저 붙여 통째로 `delta_violations` 에 넣으면 그 안의 정규화(숫자→#)가
    이름까지 지운다. 사번이 이름 자리에 들어간 간호사에서 `#: 단일 나이트` 로
    표시되던 원인이다.
    """
    out = []
    for nurse_id in sorted(who):
        b = hard_violations(nurse_id, day,
                            _shift_at(before, nurse_id, day) or ctx.off_code, before, ctx)
        a = hard_violations(nurse_id, day,
                            _shift_at(after, nurse_id, day) or ctx.off_code, after, ctx)
        out += [f"{_name(ctx, nurse_id)}: {v}" for v in delta_violations(b, a)]
    return sorted(out)


def _score(moves: Sequence[Move], ctx: ChainContext, warnings: Sequence[str]) -> float:
    """소프트 점수 — 낮을수록 우선. 하드는 이미 걸렀고 순위만 정한다."""
    total = W_CHAIN_STEP * (len(moves) - 1)
    for _nurse_id, _day, from_shift, to_shift in moves:
        total += W_SHIFT_CHANGE if from_shift in ctx.categories["work"] else W_LOSE_OFF
        if ctx.main_of.get(to_shift, to_shift) == "N":
            total += W_ADD_NIGHT
    return total + W_SOFT_VIOLATION * len(warnings)


# ── 컨텍스트 로드 ───────────────────────────────────────────────────────────


def build_chain_context(db: Session, schedule) -> ChainContext:
    """요청당 1회. 이후 후보 검사는 DB 접근 없이 메모리에서 돈다."""
    group_id = str(schedule.group_id)
    year, month = int(schedule.year), int(schedule.month)
    ctx = ChainContext()
    ctx.sequences = _load_sequences(db, schedule.schedule_id)
    nurse_ids = list(ctx.sequences)
    ctx.config = _load_config(db, group_id)
    _fill_shift_maps(ctx, shift_tools.read_shift_definitions(db, group_id))
    nurses = _fill_nurse_maps(ctx, db, nurse_ids, year, month)
    ctx.grade_min = _load_grade_min(db, group_id)
    ctx.coverage_req = {
        "D": int(ctx.config.get("day_req") or 0),
        "E": int(ctx.config.get("eve_req") or 0),
        "N": int(ctx.config.get("nig_req") or 0),
    }
    ctx.mutex = resolve_mutual_exclusion_days_for_month(db, nurse_ids, year, month) or {}
    ctx.monthly_limits = fetch_effective_monthly_limits_by_nurse(
        db, year, month, nurse_ids, group_id) or {}
    ctx.weekly_off = {
        n: (_compute_weekly_off_days(db, n, group_id, year, month) or set()) for n in nurse_ids
    }
    ctx.banned = _load_banned(db, group_id, year, month)
    ctx.fixed_cells = _load_fixed_cells(db, group_id, year, month)
    _fill_weekend_off(ctx, db, nurse_ids, year, month)
    _fill_blocked_days(ctx, db, group_id, year, month)
    _fill_team_rules(ctx, db, group_id, nurses)
    return ctx




def _fill_blocked_days(ctx: ChainContext, db: Session, group_id: str,
                       year: int, month: int) -> None:
    """파견·병동이동·휴직 아웃바운드 + 프리셉티 follow 구간 — 그 날은 배정 불가.

    기존 추천 경로가 쓰는 함수를 그대로 재사용한다(같은 판정이어야 한다).
    """
    try:
        from services.replacement_recommend_service import _build_assignment_blocked_dates
    except ImportError:
        return
    last_day = calendar.monthrange(year, month)[1]
    try:
        raw = _build_assignment_blocked_dates(db, group_id, year, month, last_day) or {}
    except SQLAlchemyError:
        db.rollback()
        logger.warning("[chain] blocked dates 조회 실패 — 미반영", exc_info=True)
        return
    ctx.blocked_days = {str(k): {_day_num(v) for v in (vs or set())}
                        for k, vs in raw.items()}


def _day_num(value) -> int:
    """blocked 집합 원소가 date 인지 일(day) 정수인지 통일한다."""
    return value.day if hasattr(value, "day") else int(value)


def _fill_team_rules(ctx: ChainContext, db: Session, group_id: str,
                     nurses: List[Nurse]) -> None:
    """팀 배정과 팀별 최소인원(`teams.min_shift`)."""
    from db.models import Team
    ctx.team_of = {str(n.nurse_id): (str(n.team_id) if getattr(n, "team_id", None) else None)
                   for n in nurses}
    if not any(ctx.team_of.values()):
        return
    ctx.team_min_soft = bool(ctx.config.get("team_min_soft_fallback", False))
    for team in db.query(Team).filter(Team.group_id == group_id).all():
        raw = getattr(team, "min_shift", None)
        if not raw:
            continue
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (ValueError, TypeError):
                continue
        cleaned = {str(k).strip().upper(): int(v)
                   for k, v in (raw or {}).items() if str(v).strip().isdigit()}
        if cleaned:
            ctx.team_min[str(team.team_id)] = cleaned


def _fill_weekend_off(ctx: ChainContext, db: Session, nurse_ids: List[str],
                      year: int, month: int) -> None:
    """주말휴무 대상자와 그 달의 주말 일자. 설정이 꺼져 있으면 비운다."""
    if not bool(ctx.config.get("weekend_off_only_enable", True)):
        return
    ctx.weekend_off = weekend_off_ids_asof(db, nurse_ids, year, month) or set()
    if not ctx.weekend_off:
        return
    last_day = calendar.monthrange(year, month)[1]
    ctx.weekend_days = {d for d in range(1, last_day + 1)
                        if calendar.weekday(year, month, d) >= 5}


def _load_sequences(db: Session, schedule_id: str) -> Sequences:
    seqs: Dict[str, List[Tuple[date, str]]] = defaultdict(list)
    for row in db.query(ScheduleEntry).filter(ScheduleEntry.schedule_id == schedule_id).all():
        work_date = getattr(row, "work_date", None)
        if work_date is None:
            continue
        day = work_date.date() if hasattr(work_date, "date") else work_date
        seqs[str(row.nurse_id)].append((day, str(row.shift_id or "")))
    for nurse_id in seqs:
        seqs[nurse_id].sort(key=lambda item: item[0])
    return dict(seqs)


def _fill_shift_maps(ctx: ChainContext, shifts: List[dict]) -> None:
    ctx.categories = schedule_tools._shift_category_map(shifts)
    ctx.main_of = {s["shift_id"]: (s.get("default_shift") or s["shift_id"]) for s in shifts}
    ctx.off_code = next((s["shift_id"] for s in shifts if not s.get("is_working")), "O")


def _fill_nurse_maps(
    ctx: ChainContext, db: Session, nurse_ids: List[str], year: int, month: int,
) -> List[Nurse]:
    nurses = db.query(Nurse).filter(Nurse.nurse_id.in_(nurse_ids)).all() if nurse_ids else []
    _overlay_capability_asof(db, nurses, year, month)
    use_mid = bool(ctx.config.get("use_mid"))
    ctx.name_of = {str(n.nurse_id): n.name for n in nurses}
    ctx.grade_of = {str(n.nurse_id): n.grade for n in nurses}
    ctx.allowed_of = {
        str(n.nurse_id): normalize_allowed_shift_codes(
            getattr(n, "allowed_shifts", None), use_mid=use_mid)
        for n in nurses
    }
    ctx.fixed_of = {str(n.nurse_id): (n.fixed_shift or None) for n in nurses}
    return nurses


def _overlay_capability_asof(db: Session, nurses: List[Nurse], year: int, month: int) -> None:
    """allowed_shifts / fixed_shift 를 대상 월 as-of period 값으로 덮는다.

    `nurses` 컬럼은 오늘 값 캐시일 뿐이고 SSOT 는 `nurse_allowed_shift_period` 다.
    캐시를 그대로 쓰면 그 달에 허용근무가 달랐던 간호사를 잘못 판정한다
    (실측: 캐시 `[]` vs as-of `['D','E','N']` 인 간호사가 있었다).

    생성기 `_overlay_home_profile_asof` / 기존 추천 `_overlay_candidate_capability_asof`
    와 같은 방식·같은 기준일(월말)을 쓴다. 구간이 없으면 캐시를 유지한다(무회귀).
    읽기 전용 흐름이라 세션 객체 in-place 주입이 안전하다.
    """
    if not nurses:
        return
    as_of = date(year, month, calendar.monthrange(year, month)[1])
    rows_by_nurse = fetch_periods(
        db, NurseAllowedShiftPeriod, [str(n.nurse_id) for n in nurses],
        as_of, as_of + timedelta(days=1),
    )
    sentinel = object()
    for nurse in nurses:
        rows = rows_by_nurse.get(str(nurse.nurse_id))
        if not rows:
            continue
        for attr in ("allowed_shifts", "fixed_shift"):
            value = resolve_asof(rows, as_of, attr, sentinel)
            if value is not sentinel:
                nurse.__dict__[attr] = value


def _load_grade_min(db: Session, group_id: str) -> Dict[str, Any]:
    config = constraint_tools.get_grade_config(db, group_id) or {}
    raw = config.get("constraints") or config.get("constraints_json") or {}
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return {}
    return raw or {}


def _load_banned(db: Session, group_id: str, year: int, month: int) -> Dict[str, Set[str]]:
    """기피 근무(banned_wanted). 테이블이 아직 없는 환경에서는 빈 값으로 넘어간다."""
    out: Dict[str, Set[str]] = defaultdict(set)
    try:
        rows = (
            db.query(BannedWantedEntry)
            .filter(
                BannedWantedEntry.group_id == group_id,
                BannedWantedEntry.year == year,
                BannedWantedEntry.month == month,
                # MSSQL 은 `IS 1` 을 거부한다(.is_(True) 가 그렇게 나간다). == True 유지.
                BannedWantedEntry.is_applied == True,  # noqa: E712
            )
            .all()
        )
    except SQLAlchemyError:
        # banned_wanted_entries 는 아직 prod DDL 미적용이라 테이블이 없을 수 있다.
        # 실패한 statement 가 트랜잭션을 오염시키므로 반드시 rollback 후 빈 값으로 진행.
        logger.warning("[chain] banned_wanted_entries 조회 실패 — 기피근무 미반영", exc_info=True)
        db.rollback()
        return out
    for row in rows:
        codes = row.banned_shift_ids
        if isinstance(codes, str):
            try:
                codes = json.loads(codes)
            except (ValueError, TypeError):
                codes = []
        out[str(row.nurse_id)] |= {str(c).upper() for c in (codes or [])}
    return out


def _load_fixed_cells(db: Session, group_id: str, year: int, month: int) -> Set[Tuple[str, date]]:
    rows = (
        db.query(FixedWantedEntry)
        .filter(
            FixedWantedEntry.group_id == group_id,
            FixedWantedEntry.year == year,
            FixedWantedEntry.month == month,
            # MSSQL 은 `IS 1` 을 거부한다(.is_(True) 가 그렇게 나간다). == True 유지.
            FixedWantedEntry.is_applied == True,  # noqa: E712
        )
        .all()
    )
    return {(str(r.nurse_id), r.shift_date) for r in rows}


# ── 공개 진입점 ─────────────────────────────────────────────────────────────


def recommend_lns_repair(
    db: Session,
    ctx: ChainContext,
    schedule,
    current_user,
    target_nurse_id: str,
    day: date,
    shift: str,
    forward_days: int = LNS_FORWARD_DAYS,
    allow_fallback: bool = True,
) -> Optional[ChainProposal]:
    """2단 — 1단(당일 1열)으로 못 푸는 결원을 **이후** 날짜까지 열어 푼다.

    나이트 결원은 당일 1열만 봐서는 원리적으로 안 풀린다(1N 금지·연속 N 상한·회복이
    전부 하드라 최소 2일 블록이 필요하다). 결원일부터 `forward_days` 일 뒤까지 연다.

    전용 CP-SAT 모델로 푼다(실측 72ms). 제약을 이 모듈에서 다시 세우므로 결과를
    반드시 기존 체커로 재검증한다(`_local_result_ok`) — 물리면 제안하지 않는다.

    생성 파이프라인 dry-run 폴백은 **쓰지 않는다.** 그 경로는 과거 날짜를 열고
    변경량 상한도 못 걸어, 실행 불가능하거나 과도한 제안을 만든다.
    (`generate_roster_service(dry_run=...)` seam 자체는 남아 있다)

    **1단이 빈손일 때만 부른다.**  저장은 하지 않는다.
    """
    free_dates = _free_dates(day, forward_days, ctx)
    return _repair_via_local_cpsat(ctx, target_nurse_id, day, shift, free_dates,
                                   allow_fallback)


def _free_dates(day: date, forward_days: int, ctx: ChainContext) -> List[date]:
    """자유 구간 — **결원일부터 앞으로만**. 과거는 이미 수행된 근무라 건드리지 않는다."""
    last = calendar.monthrange(day.year, day.month)[1]
    out = []
    for offset in range(0, forward_days + 1):
        d = day + timedelta(days=offset)
        if d.day <= last and d.month == day.month:
            out.append(d)
    return out


# ── 2단 ① 전용 CP-SAT 모델 ────────────────────────────────────────────────
#
#   생성 파이프라인(fallback_lex)을 쓰지 않는다. 그쪽은 근무표 "생성"용이라
#   전처리 3s + 모델빌드 2.8s(BoolVar 12,522 x 3단계)가 붙는데, 실제 최적화는
#   0.5s 뿐이다. 여기서는 자유 구간만 변수로 세워 74ms 에 끝낸다.
#
#   판정 재료는 ChainContext 에 이미 다 있으므로 추가 DB 접근이 없다.


@dataclass
class _LocalModel:
    """전용 모델 한 벌. 자유 셀만 변수이고 나머지 날짜는 상수로 들어간다."""

    ctx: ChainContext
    model: Any
    codes: List[str]
    free_dates: List[date]
    free_set: Set[Tuple[str, date]]
    x: Dict[Tuple[str, date, str], Any]
    span: List[date]
    target_nurse_id: str
    absence_day: date
    max_cells: int
    #: 제약 상대화의 기준 — **결원을 이미 반영한** 근무표.
    #: 원본을 기준으로 삼으면 "N N N 가운데를 비워서 생긴 1N" 을 추천 탓으로 몰아
    #: 해를 통째로 버린다. 결원자는 그날 못 나오는 게 전제다.
    baseline: Sequences


def _relaxed_ctx(ctx: ChainContext) -> ChainContext:
    """차선안 탐색용 사본 — 개인 근무규칙만 끈다.

    얕은 복사라 근무표·명단은 원본과 공유한다(읽기만 한다). `config` 만 갈아끼워
    `_add_sequence_rules` 가 1N·회복·전환·연속 제약을 걸지 않게 만든다.
    인원·등급·팀은 건드리지 않으므로 그대로 강제된다.
    """
    out = copy.copy(ctx)
    out.config = {**ctx.config, **{k: False for k in RELAXABLE_RULES}}
    return out


def _repair_via_local_cpsat(
    ctx: ChainContext,
    target_nurse_id: str,
    day: date,
    shift: str,
    free_dates: List[date],
    allow_fallback: bool = True,
    max_cells: int = LNS_MAX_CHANGED_CELLS,
) -> Optional[ChainProposal]:
    """전용 모델로 푼다. 조건을 다 지키는 해가 없으면 차선안까지 시도한다."""
    try:
        from ortools.sat.python import cp_model  # 지연 import — 1단 경로는 안 쓴다
    except ImportError:
        return None
    proposal = _solve_local_once(cp_model, ctx, target_nurse_id, day, shift,
                                 free_dates, max_cells)
    if proposal is not None or not allow_fallback:
        return proposal
    proposal = _solve_local_once(cp_model, _relaxed_ctx(ctx), target_nurse_id,
                                 day, shift, free_dates, max_cells)
    if proposal is None:
        return None
    hard = _proposal_hard_warnings(ctx, proposal, target_nurse_id, day)
    if len(hard) > MAX_FALLBACK_VIOLATIONS:
        return None
    proposal.hard_warnings = hard
    return proposal


def _solve_local_once(
    cp_model, ctx: ChainContext, target_nurse_id: str, day: date,
    shift: str, free_dates: List[date], max_cells: int = LNS_MAX_CHANGED_CELLS,
) -> Optional[ChainProposal]:
    """주어진 ctx 기준으로 한 번 푼다. 해가 없거나 체커가 물면 None."""
    lm = _build_local_model(cp_model, ctx, target_nurse_id, day, free_dates, max_cells)
    if lm is None:
        return None
    solution = _solve_local(cp_model, lm)
    if solution is None:
        return None
    state = _apply_local_solution(ctx, solution)
    if not _local_result_ok(ctx, state, solution, target_nurse_id, day, shift):
        return None
    return _local_solution_to_proposal(ctx, solution, target_nurse_id, day, shift)


def _proposal_hard_warnings(
    ctx: ChainContext, proposal: ChainProposal, target_nurse_id: str, day: date,
) -> List[str]:
    """차선안이 **원래 기준으로** 새로 깨뜨리는 개인 근무규칙.

    결원 당사자 몫은 빼고 센다 — 그건 추천이 만든 게 아니라 결원 자체의 결과이며
    `absence_side_effects` 가 따로 알린다.
    """
    origin = _move(ctx.sequences, target_nurse_id, day, ctx.off_code)
    state = {n: list(seq) for n, seq in origin.items()}
    for mv in proposal.moves:
        state[mv.nurse_id] = [(d, mv.to_shift if d == mv.date else code)
                              for d, code in state[mv.nurse_id]]
    out: Set[str] = set()
    for mv in proposal.moves:
        if mv.is_absence:
            continue
        before = hard_violations(mv.nurse_id, mv.date,
                                 _shift_at(origin, mv.nurse_id, mv.date) or ctx.off_code,
                                 origin, ctx)
        after = hard_violations(mv.nurse_id, mv.date,
                                _shift_at(state, mv.nurse_id, mv.date) or ctx.off_code,
                                state, ctx)
        out.update(f"{mv.name}: {v}" for v in delta_violations(before, after))
    return sorted(out)


def _build_local_model(cp_model, ctx, target_nurse_id, day, free_dates,
                       max_cells: int = LNS_MAX_CHANGED_CELLS) -> Optional[_LocalModel]:
    """변수·제약을 세운다. 결원 칸이 자유 셀이 아니면(휴가 등) 포기한다."""
    codes = sorted({ctx.main_of.get(s, s) for s in ctx.categories["work"]} | {ctx.off_code})
    free_cells = _local_free_cells(ctx, free_dates, codes, target_nurse_id)
    free_set = set(free_cells)
    if (target_nurse_id, day) not in free_set:
        return None
    model = cp_model.CpModel()
    x = {(n, d, c): model.NewBoolVar(f"x_{n}_{d}_{c}")
         for (n, d) in free_cells for c in codes}
    lm = _LocalModel(ctx=ctx, model=model, codes=codes, free_dates=free_dates,
                     free_set=free_set, x=x, span=_local_span(free_dates),
                     target_nurse_id=target_nurse_id, absence_day=day, max_cells=max_cells,
                     baseline=_move(ctx.sequences, target_nurse_id, day, ctx.off_code))
    for cell in free_cells:
        model.AddExactlyOne(x[cell[0], cell[1], c] for c in codes)
        _add_cell_rules(lm, *cell)
    model.Add(x[target_nurse_id, day, ctx.off_code] == 1)   # 결원자는 그 날 OFF
    _add_coverage_rules(lm)
    _add_night_quota_rules(lm)
    for nurse_id in {n for n, _ in free_cells}:
        _add_sequence_rules(lm, nurse_id)
    _add_local_objective(lm, free_cells)
    return lm


def _local_free_cells(
    ctx: ChainContext, free_dates: List[date], codes: List[str], target_nurse_id: str,
) -> List[Tuple[str, date]]:
    """건드려도 되는 칸 — 원본 코드가 D/E/N/O 계열인 것만.

    휴가·공가·교육처럼 모델이 표현하지 못하는 코드는 상수로 남긴다.
    안 그러면 휴가를 근무로 바꿔버린다.
    """
    out = []
    for nurse_id in sorted(ctx.sequences):
        for d in free_dates:
            current = _shift_at(ctx.sequences, nurse_id, d)
            if current is not None and ctx.main_of.get(current, current) in codes:
                out.append((nurse_id, d))
    return out


def _local_span(free_dates: List[date]) -> List[date]:
    """시퀀스 제약을 볼 범위 — 자유 구간 ±6일(연속근무 5·회복 2 를 덮는다)."""
    out, cur = [], free_dates[0] - timedelta(days=6)
    end = free_dates[-1] + timedelta(days=6)
    while cur <= end:
        out.append(cur)
        cur += timedelta(days=1)
    return out


def _orig(lm: _LocalModel, nurse_id: str, d: date, want: Set[str]) -> int:
    """**결원 반영 기준선**에서 그 날이 want 집합인가 (0/1).

    제약 상한을 이 값으로 상대화하므로, 결원 처리로 생긴 위반은 자동 면제된다.
    """
    current = _shift_at(lm.baseline, nurse_id, d)
    return 1 if current and lm.ctx.main_of.get(current, current) in want else 0


def _term(lm: _LocalModel, nurse_id: str, d: date, want: Set[str]):
    """그 날이 want 집합인가 — 자유 셀이면 변수 합, 아니면 0/1 상수."""
    if (nurse_id, d) in lm.free_set:
        return sum(lm.x[nurse_id, d, c] for c in lm.codes if c in want)
    return _orig(lm, nurse_id, d, want)


def _add_cell_rules(lm: _LocalModel, nurse_id: str, d: date) -> None:
    """셀 하나에 걸리는 제약 — 허용근무·고정근무·주휴·주말휴무·기피·확정원티드."""
    ctx, m, x = lm.ctx, lm.model, lm.x
    allowed = ctx.allowed_of.get(nurse_id) or set()
    fixed = ctx.fixed_of.get(nurse_id)
    banned = ctx.banned.get(nurse_id, set())
    for c in lm.codes:
        if c == ctx.off_code:
            continue
        if (allowed and c not in allowed) or (fixed and c != fixed) or c in banned:
            m.Add(x[nurse_id, d, c] == 0)
    if d.day in ctx.weekly_off.get(nurse_id, set()):
        m.Add(x[nurse_id, d, ctx.off_code] == 1)
    if nurse_id in ctx.weekend_off:
        m.Add(x[nurse_id, d, ctx.off_code] == (1 if d.day in ctx.weekend_days else 0))
    if (nurse_id, d) in ctx.fixed_cells and not (nurse_id == lm.target_nurse_id and d == lm.absence_day):
        # 결원 당사자의 결원일은 제외 — OFF 강제와 충돌해 INFEASIBLE 이 된다.
        current = _shift_at(ctx.sequences, nurse_id, d)
        if current:
            m.Add(x[nurse_id, d, ctx.main_of.get(current, current)] == 1)
    if d.day in ctx.blocked_days.get(nurse_id, set()):
        m.Add(x[nurse_id, d, ctx.off_code] == 1)   # 관할 밖 — 근무 배정 불가


def _add_coverage_rules(lm: _LocalModel) -> None:
    """커버리지·등급 최소인원 — 원본 수준 아래로만 안 떨어지면 된다(악화 금지)."""
    ctx, m = lm.ctx, lm.model
    for d in lm.free_dates:
        # 원본 기준이어야 한다. baseline(결원 반영)으로 낮추면 '아무도 안 채워도 됨'
        # 이 되어 사후 검증(원본 인원 유지)과 모순된다.
        assigned = _day_assignment(ctx.sequences, ctx, d)
        for main, need in ctx.coverage_req.items():
            if not need or main not in lm.codes:
                continue
            m.Add(_headcount(lm, d, main) >= min(need, len(assigned[main])))
        # 인원과 달리 **등급·팀은 결원 반영 후가 기준**이다. 결원자가 그 근무의
        # 유일한 저연차였다면 그 요구는 결원으로 이미 깨진 것이고, 대체자가 반드시
        # 같은 등급이어야 할 이유는 없다. 원본으로 강제하면 "다른 저연차를 데려와라"
        # 가 되는데 그쪽 근무도 1명뿐이라 빼는 순간 거기가 깨져 INFEASIBLE 이 된다.
        # (실측: 민지혜 8-09 N — 등급 제약만 빼면 즉시 OPTIMAL)
        based = _day_assignment(lm.baseline, ctx, d)
        for main in ("D", "E", "N"):
            _add_grade_rules(lm, d, main, based[main])
        _add_team_min_rules(lm, d, based)


def _headcount(lm: _LocalModel, d: date, main: str, only: Optional[Set[str]] = None):
    """그 날 main 근무 인원 — 자유 셀은 변수, 고정 셀은 상수."""
    pool = [n for n in lm.ctx.sequences if only is None or n in only]
    free_part = sum(lm.x[n, d, main] for n in pool if (n, d) in lm.free_set)
    fixed_part = sum(_orig(lm, n, d, {main}) for n in pool if (n, d) not in lm.free_set)
    return free_part + fixed_part


def _add_grade_rules(lm: _LocalModel, d: date, main: str, assigned: List[str]) -> None:
    per_grade = (lm.ctx.grade_min or {}).get(main)
    if not per_grade or main not in lm.codes:
        return
    population = Counter(g for g in lm.ctx.grade_of.values() if g is not None)
    for grade, target in _cascade_grade_targets(per_grade, population).items():
        eligible = {n for n in lm.ctx.sequences
                    if lm.ctx.grade_of.get(n) is not None and lm.ctx.grade_of[n] <= grade}
        if not eligible:
            continue
        base = sum(1 for n in assigned if n in eligible)
        lm.model.Add(_headcount(lm, d, main, eligible) >= min(target, base))


def _add_team_min_rules(lm: _LocalModel, d: date, assigned: Dict[str, List[str]]) -> None:
    """팀 최소인원 — 서로 다른 팀을 min(요구인원, 팀수)개 커버.

    `present_t` 를 BoolVar 로 두고 `Σ present_t >= target` 을 건다(솔버와 같은 구조).
    상한은 원본 대비 상대화한다 — 원본이 이미 못 채우고 있으면 그 수준까지 허용.
    """
    ctx = lm.ctx
    if not ctx.team_min or ctx.team_min_soft:
        return
    for main, need in ctx.coverage_req.items():
        if main not in lm.codes:
            continue
        present_vars, base_present, eligible = [], 0, 0
        for team_id, per_code in ctx.team_min.items():
            min_t = int(per_code.get(main, 0) or 0)
            members = [n for n, t in ctx.team_of.items() if t == team_id]
            if min_t <= 0 or len(members) < min_t:
                continue
            eligible += 1
            if sum(1 for n in assigned[main] if ctx.team_of.get(n) == team_id) >= min_t:
                base_present += 1
            present = lm.model.NewBoolVar(f"tmin_{team_id}_{d}_{main}")
            lm.model.Add(_headcount(lm, d, main, set(members)) >= min_t * present)
            present_vars.append(present)
        target = min(int(need or 0), eligible)
        if target > 0 and present_vars:
            lm.model.Add(sum(present_vars) >= min(target, base_present))


def _add_night_quota_rules(lm: _LocalModel) -> None:
    """월 나이트 개인 한도 — 고정 구간 N 수 + 자유 구간 변수."""
    ctx = lm.ctx
    if "N" not in lm.codes:
        return
    for nurse_id in ctx.sequences:
        limit = ctx.monthly_limits.get(nurse_id) or {}
        cap = limit.get("n_max") or limit.get("n_exact")
        if cap is None:
            continue
        fixed_n = sum(1 for d, s in ctx.sequences[nurse_id]
                      if (nurse_id, d) not in lm.free_set and ctx.main_of.get(s, s) == "N")
        total_now = sum(1 for _d, s in ctx.sequences[nurse_id]
                        if ctx.main_of.get(s, s) == "N")
        free_n = sum(lm.x[nurse_id, d, "N"] for d in lm.free_dates
                     if (nurse_id, d) in lm.free_set)
        lm.model.Add(fixed_n + free_n <= max(int(cap), total_now))


def _add_sequence_rules(lm: _LocalModel, nurse_id: str) -> None:
    """시퀀스 규칙 — 전환·연속근무·연속N·1N·4O·회복.

    상한은 전부 **원본 대비 상대화**한다. 원본 근무표가 이미 위반 상태인 경우가 있어
    절대 무위반으로 걸면 원본조차 재현 못 해 INFEASIBLE 이 된다.
    """
    _add_transition_rules(lm, nurse_id)
    _add_streak_rules(lm, nurse_id)
    _add_one_night_rule(lm, nurse_id)
    _add_four_off_rule(lm, nurse_id)
    _add_recovery_rules(lm, nurse_id)


def _touches_free(lm: _LocalModel, nurse_id: str, days: Sequence[date]) -> bool:
    return any((nurse_id, d) in lm.free_set for d in days)


def _add_transition_rules(lm: _LocalModel, nurse_id: str) -> None:
    """N→D · N→E · E→D 금지."""
    cfg, m = lm.ctx.config, lm.model
    pairs = []
    if bool(cfg.get("nod_noe")):
        pairs += [({"N"}, {"D"}), ({"N"}, {"E"})]
    if bool(cfg.get("banned_day_after_eve")):
        pairs.append(({"E"}, {"D"}))
    for i in range(len(lm.span) - 1):
        d0, d1 = lm.span[i], lm.span[i + 1]
        if not _touches_free(lm, nurse_id, (d0, d1)):
            continue
        for a, b in pairs:
            cap = max(1, _orig(lm, nurse_id, d0, a) + _orig(lm, nurse_id, d1, b))
            m.Add(_term(lm, nurse_id, d0, a) + _term(lm, nurse_id, d1, b) <= cap)


def _add_streak_rules(lm: _LocalModel, nurse_id: str) -> None:
    """연속 근무 상한 K · 연속 나이트 상한 L."""
    cfg, m = lm.ctx.config, lm.model
    work = {c for c in lm.codes if c != lm.ctx.off_code}
    limits = [(int(cfg.get("max_conseq_work") or 5), work),
              (3 if bool(cfg.get("three_seq_nig")) else 2, {"N"})]
    for cap_len, want in limits:
        for i in range(len(lm.span) - cap_len):
            window = lm.span[i:i + cap_len + 1]
            if not _touches_free(lm, nurse_id, window):
                continue
            cap = max(cap_len, sum(_orig(lm, nurse_id, d, want) for d in window))
            m.Add(sum(_term(lm, nurse_id, d, want) for d in window) <= cap)


def _add_one_night_rule(lm: _LocalModel, nurse_id: str) -> None:
    """1N 단독 배정 금지 — N 인 날은 앞뒤 중 하나가 N."""
    if not bool(lm.ctx.config.get("not_one_night")):
        return
    for i in range(1, len(lm.span) - 1):
        prev, cur, nxt = lm.span[i - 1], lm.span[i], lm.span[i + 1]
        if (nurse_id, cur) not in lm.free_set:
            continue
        if _orig(lm, nurse_id, cur, {"N"}) and not (
                _orig(lm, nurse_id, prev, {"N"}) or _orig(lm, nurse_id, nxt, {"N"})):
            continue  # 원본이 이미 1N — 면제
        lm.model.Add(_term(lm, nurse_id, cur, {"N"})
                     <= _term(lm, nurse_id, prev, {"N"}) + _term(lm, nurse_id, nxt, {"N"}))


def _add_four_off_rule(lm: _LocalModel, nurse_id: str) -> None:
    """순수 OFF 4연속 금지."""
    if not bool(lm.ctx.config.get("enforce_4o_hard", True)):
        return
    off = {lm.ctx.off_code}
    for i in range(len(lm.span) - 3):
        window = lm.span[i:i + 4]
        if not _touches_free(lm, nurse_id, window):
            continue
        cap = max(3, sum(_orig(lm, nurse_id, d, off) for d in window))
        lm.model.Add(sum(_term(lm, nurse_id, d, off) for d in window) <= cap)


def _add_recovery_rules(lm: _LocalModel, nurse_id: str) -> None:
    """2N 직후 2일은 근무 금지(회복)."""
    if not bool(lm.ctx.config.get("two_offs_after_two_nig")):
        return
    work = {c for c in lm.codes if c != lm.ctx.off_code}
    for i in range(len(lm.span) - 3):
        a, b = lm.span[i], lm.span[i + 1]
        for tail in (lm.span[i + 2], lm.span[i + 3]):
            if not _touches_free(lm, nurse_id, (a, b, tail)):
                continue
            cap = max(2, _orig(lm, nurse_id, a, {"N"}) + _orig(lm, nurse_id, b, {"N"})
                      + _orig(lm, nurse_id, tail, work))
            lm.model.Add(_term(lm, nurse_id, a, {"N"}) + _term(lm, nurse_id, b, {"N"})
                         + _term(lm, nurse_id, tail, work) <= cap)


def _add_local_objective(lm: _LocalModel, free_cells: List[Tuple[str, date]]) -> None:
    """목적 — 원본에서 바뀌는 셀 수 최소화(MPP) + 상한 하드.

    상한을 제약으로 걸면 "최적해가 상한을 넘어서 버려지는" 일이 없다.
    솔버가 상한 안에서 최선을 찾는다.
    """
    terms = []
    for nurse_id, d in free_cells:
        current = _shift_at(lm.ctx.sequences, nurse_id, d)
        terms.append(1 - lm.x[nurse_id, d, lm.ctx.main_of.get(current, current)])
    if not terms:
        return
    # 결원 셀 1칸은 상한에 이미 포함돼 있다(결원자 OFF 도 변경으로 센다)
    lm.model.Add(sum(terms) <= lm.max_cells)
    lm.model.Minimize(sum(terms))


def _solve_local(cp_model, lm: _LocalModel) -> Optional[Dict[Tuple[str, date], str]]:
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(LNS_LOCAL_TIME_LIMIT)
    solver.parameters.num_search_workers = 8
    status = solver.Solve(lm.model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None
    return {(n, d): next(c for c in lm.codes if solver.Value(lm.x[n, d, c]))
            for (n, d) in lm.free_set}


def _keep_subcode(ctx: ChainContext, current: Optional[str], code: str) -> str:
    """main 이 그대로면 **원본 코드를 지킨다.** 바뀔 때만 모델이 고른 코드를 쓴다.

    전용 모델은 main(D/E/N/O)만 다룬다. 그대로 얹으면 `Dㅇ` 같은 서브코드가 `D` 로
    덮여, 실제 근무는 하나도 안 바뀌었는데 변경 셀로 잡힌다(실측: 한 제안에서 22칸이
    `Dㅇ→D` 였고 변경 상한 6칸을 24칸으로 넘겼다). 적용하면 서브코드도 소실된다.
    """
    if current and ctx.main_of.get(current, current) == code:
        return current
    return code


def _apply_local_solution(ctx: ChainContext, solution: Dict[Tuple[str, date], str]) -> Sequences:
    out = {n: list(seq) for n, seq in ctx.sequences.items()}
    for (nurse_id, d), code in solution.items():
        out[nurse_id] = [(d2, _keep_subcode(ctx, s, code) if d2 == d else s)
                         for d2, s in out[nurse_id]]
    return out


def _local_result_ok(
    ctx: ChainContext,
    state: Sequences,
    solution: Dict[Tuple[str, date], str],
    target_nurse_id: str,
    day: date,
    shift: str,
) -> bool:
    """전용 모델 결과를 **기존 체커로** 재검증한다.

    제약을 이 모듈에서 다시 세웠으므로 모델만 믿지 않는다. 체커는 1단과 같은 코드다.
    """
    if _shift_at(state, target_nurse_id, day) != ctx.off_code:
        return False
    main = ctx.main_of.get(shift, shift)
    if len(_day_assignment(state, ctx, day)[main]) < len(
            _day_assignment(ctx.sequences, ctx, day)[main]):
        return False
    # 결원자의 기준선은 **결원을 반영한 상태**다.
    #   `_streak_warnings` 는 시퀀스 전체를 보므로, N N N 가운데를 비우면 앞뒤 1N 이
    #   어느 날짜로 검사하든 함께 딸려 나온다. 원본과 비교하면 "추천이 만든 위반"으로
    #   오인해 해를 통째로 버린다(실측: N뭉치 가운데 65건 중 49건이 이 때문에 막혔다).
    #   앞쪽 1N 은 과거라 고칠 수도 없다. 다른 간호사는 기준선 == 원본이라 영향 없다.
    origin = _move(ctx.sequences, target_nurse_id, day, ctx.off_code)
    touched = {n for (n, d), c in solution.items()
               if _shift_at(origin, n, d) != _keep_subcode(ctx, _shift_at(origin, n, d), c)}
    for nurse_id in touched:
        for d in sorted({d for (n, d) in solution if n == nurse_id}):
            before = hard_violations(
                nurse_id, d, _shift_at(origin, nurse_id, d) or ctx.off_code,
                origin, ctx)
            after = hard_violations(
                nurse_id, d, _shift_at(state, nurse_id, d) or ctx.off_code, state, ctx)
            if delta_violations(before, after):
                return False
    return True


def _local_solution_to_proposal(
    ctx: ChainContext,
    solution: Dict[Tuple[str, date], str],
    target_nurse_id: str,
    day: date,
    shift: str,
) -> Optional[ChainProposal]:
    moves = [ChainMove(
        nurse_id=target_nurse_id, name=_name(ctx, target_nurse_id), date=day,
        from_shift=shift, to_shift=ctx.off_code, is_absence=True,
    )]
    for (nurse_id, d), code in sorted(solution.items()):
        before = _shift_at(ctx.sequences, nurse_id, d)
        code = _keep_subcode(ctx, before, code)
        if before == code or (nurse_id == target_nurse_id and d == day):
            continue
        moves.append(ChainMove(nurse_id=nurse_id, name=_name(ctx, nurse_id), date=d,
                               from_shift=before or ctx.off_code, to_shift=code))
    if len(moves) < 2:
        return None
    participants = len({m.nurse_id for m in moves if not m.is_absence})
    warnings = ["여러 날짜가 함께 조정됩니다. 적용 전 변경 셀을 확인하세요."]
    warnings += absence_side_effects(
        ctx, _apply_local_solution(ctx, solution), target_nurse_id, day, shift)
    return ChainProposal(
        rank=1, kind="LNS", participant_count=participants,
        changed_cell_count=len(moves),
        score=round(W_CHAIN_STEP * participants + len(moves), 2),
        moves=moves,
        soft_warnings=warnings,
    )


def hard_violation_reasons(
    ctx: ChainContext,
    target_nurse_id: str,
    day: date,
    shift: str,
    candidate_id: str,
) -> List[str]:
    """후보를 결원 칸에 넣었을 때 **새로 생기는** 하드 위반 사유.

    빈 리스트면 솔버 기준으로 배정 가능하다는 뜻이다.
    기존 추천 경로(`replacement_recommend_service`)가 후보를 거르는 데 쓴다.

    판정은 §원본 대비 악화 금지 — 원본 근무표가 이미 깨뜨리고 있는 항목은 면책한다.
    대상/후보가 이 스케줄에 없으면 판단 근거가 없으므로 빈 리스트(통과)를 준다.
    """
    if target_nurse_id not in ctx.sequences or candidate_id not in ctx.sequences:
        return []
    if (candidate_id, day) in ctx.fixed_cells:
        return ["확정 원티드 셀"]   # 델타로는 상쇄되므로 여기서 절대 배제
    base = _move(ctx.sequences, target_nurse_id, day, ctx.off_code)
    current = _shift_at(base, candidate_id, day)
    if current is None:
        return []
    if ctx.main_of.get(current, current) == ctx.main_of.get(shift, shift):
        return ["이미 같은 근무"]  # 데려와도 결원이 안 메워진다
    moved = _move(base, candidate_id, day, shift)
    personal = delta_violations(
        hard_violations(candidate_id, day, current, base, ctx),
        hard_violations(candidate_id, day, shift, moved, ctx),
    )
    structural = delta_violations(
        _structural_issues(ctx.sequences, ctx, day),
        _structural_issues(moved, ctx, day),
    )
    return personal + [s for s in structural if s not in personal]


def recommend_chain_proposals(
    ctx: ChainContext,
    target_nurse_id: str,
    day: date,
    shift: str,
    limit: int = 10,
    allow_fallback: bool = True,
) -> List[ChainProposal]:
    """결원 (target_nurse_id, day, shift) 에 대한 수정안 목록.

    1인 스왑과 다인 연쇄를 같은 목록에 담아 (하드위반 수, 인원수, 점수) 오름차순
    으로 준다. MPP 목적상 변경 셀이 적은 안이 먼저다.

    `allow_fallback` 이면 조건을 다 지키는 안이 없을 때 개인 근무규칙 위반을
    감수하는 차선안까지 찾는다. 그 안들은 `hard_warnings` 가 비어 있지 않다.
    """
    if target_nurse_id not in ctx.sequences:
        return []
    if _shift_at(ctx.sequences, target_nurse_id, day) is None:
        return []
    base = _move(ctx.sequences, target_nurse_id, day, ctx.off_code)

    raw: List[List[Move]] = []
    _find_chains(day, shift, base, ctx, 1, {target_nurse_id}, [], raw)
    main = ctx.main_of.get(shift, shift)
    scored = _score_and_filter(raw, base, ctx, target_main=main)

    if not scored and allow_fallback:
        # 조건을 다 지키는 안이 하나도 없다. 개인 근무규칙을 여는 차선안을 찾아
        # **위반을 명시한 채로** 내놓는다. 판단은 사용자 몫이다.
        judge = _relaxed_ctx(ctx)
        raw = []
        _find_chains(day, shift, base, judge, 1, {target_nurse_id}, [], raw)
        scored = _score_and_filter(raw, base, ctx, judge, target_main=main)

    return [_to_proposal(ctx, moves, warnings, hard, score, rank,
                         target_nurse_id, day, shift)
            for rank, (score, moves, warnings, hard)
            in enumerate(_dedupe(scored)[:limit], start=1)]


def recommend_repair_plan(
    ctx: ChainContext,
    target_nurse_id: str,
    day: date,
    shift: str,
    limit: int = 10,
    use_lns: bool = False,
    forward_days: int = LNS_FORWARD_DAYS,
) -> List[ChainProposal]:
    """결원 한 칸에 대한 수정안 — **규칙을 지키는 안을 끝까지 먼저 찾는다.**

    ① 1단 무위반 → ② 2단 무위반 → ③ 넓힌 2단 무위반 + 1N 차선안

    ②를 ③보다 먼저 두는 게 핵심이다. 반대로 하면 규칙을 다 지키는 2단 해가
    있는데도 위반을 감수하는 안을 먼저 주게 된다(실측: 그 순서에서 LNS 가
    122건→10건으로 줄고 조건 준수가 622→504 로 떨어졌다).

    ③은 성격이 다른 두 안을 **함께** 담는다. 정렬이 무위반을 위로 올리므로
    "규칙 준수·변경 많음" 이 "변경 적음·1N 위반" 보다 앞선다.

    `use_lns` 가 꺼져 있으면 1단 계열만 본다. 2단은 결원당 수십 ms 가 붙는다.
    """
    solo = _absence_only_plan(ctx, target_nurse_id, day, shift)
    clean = recommend_chain_proposals(ctx, target_nurse_id, day, shift, limit,
                                      allow_fallback=False)
    if clean:
        # 대체안이 우선이다. 요구인원은 **최소 안전선**이지 운영 목표가 아니다 —
        # D 에 8명을 배치해 둔 병동에 "요구 3명은 넘으니 비워도 된다" 를 1순위로
        # 밀면 안 된다. 비우기는 맨 뒤에 선택지로만 붙인다.
        out = clean[:limit]
        if solo is not None and len(out) < limit:
            out.append(solo)
        for rank, plan in enumerate(out, start=1):
            plan.rank = rank
        return out
    if solo is not None:
        return [solo]
    free = _free_dates(day, forward_days, ctx) if use_lns else []
    if use_lns:
        lns = _repair_via_local_cpsat(ctx, target_nurse_id, day, shift, free,
                                      allow_fallback=False)
        if lns is not None:
            return [lns]

    # 여기부터는 무언가를 감수해야 한다. 성격이 다른 두 갈래를 **함께** 내놓는다.
    #   ㄱ. 규칙은 다 지키되 변경 칸이 많은 안
    #   ㄴ. 변경은 작지만 1N 을 어기는 안
    # 어느 쪽이 나은지는 상황에 달렸으므로 고르는 건 사용자 몫이다.
    # 인원·등급·팀 미달은 여기 없다 — 솔버가 하드로 걸고 "무너지면 환자 안전 영향
    # 큼"(fallback_lex:535) 이라 명시한 항목이라 감수 대상이 아니다.
    plans: List[ChainProposal] = []
    if use_lns:
        wide = _repair_via_local_cpsat(ctx, target_nurse_id, day, shift, free,
                                       allow_fallback=False,
                                       max_cells=LNS_EXPANDED_CELLS)
        if wide is not None:
            wide.soft_warnings.insert(
                0, f"변경 범위가 큽니다 — {wide.changed_cell_count}칸")
            plans.append(wide)
    # 무위반 탐색을 한 번 더 돌지만 1단은 결원당 0.04ms 라 무시할 수 있다.
    plans += recommend_chain_proposals(ctx, target_nurse_id, day, shift, limit,
                                       allow_fallback=True)
    if use_lns and not plans:
        lns = _repair_via_local_cpsat(ctx, target_nurse_id, day, shift, free,
                                      allow_fallback=True)
        if lns is not None:
            plans.append(lns)
    if not plans:
        return []
    plans.sort(key=lambda p: (len(p.hard_warnings), p.changed_cell_count, p.score))
    for rank, plan in enumerate(plans[:limit], start=1):
        plan.rank = rank
    return plans[:limit]


def explain_no_plan(
    ctx: ChainContext, target_nurse_id: str, day: date, shift: str,
) -> Tuple[str, Dict[str, int]]:
    """수정안이 하나도 없을 때 **왜인지**.

    빈 화면은 "시스템이 고장났나" 로 읽힌다. 그날 인원 현황과 후보별 탈락 사유를
    돌려줘, 무엇을 풀어야 대체가 가능해지는지 사용자가 알게 한다.
    """
    base = _move(ctx.sequences, target_nurse_id, day, ctx.off_code)
    assignment = _day_assignment(base, ctx, day)
    main = ctx.main_of.get(shift, shift)
    reasons: Counter = Counter()
    for nurse_id in ctx.sequences:
        if nurse_id == target_nurse_id:
            continue
        current = _shift_at(base, nurse_id, day)
        if current is None:
            reasons["그날 배정 없음"] += 1
            continue
        if ctx.main_of.get(current, current) == main:
            reasons["이미 같은 근무"] += 1
            continue
        if (nurse_id, day) in ctx.fixed_cells:
            reasons["확정 원티드 셀"] += 1
            continue
        moved = _move(base, nurse_id, day, shift)
        hit = delta_violations(hard_violations(nurse_id, day, current, base, ctx),
                               hard_violations(nurse_id, day, shift, moved, ctx))
        if not hit:
            reasons["데려오면 원래 근무가 미달"] += 1
            continue
        for violation in hit:
            reasons[_kind(violation)] += 1
    status = " · ".join(
        f"{m} {len(assignment[m])}/{ctx.coverage_req.get(m, 0)}" for m in ("D", "E", "N")
        if ctx.coverage_req.get(m)
    )
    return f"{day.month}월 {day.day}일 결원 반영 인원 {status} — 조건을 지키는 대체가 없습니다", dict(reasons)


def _absence_only_plan(
    ctx: ChainContext, target_nurse_id: str, day: date, shift: str,
) -> Optional[ChainProposal]:
    """대체가 **필요 없는** 경우 — 결원만 비우고 끝낸다.

    그날 그 근무 인원이 빠지고도 요구치를 만족하면 남을 건드릴 이유가 없다.
    MPP 원칙상 변경은 0칸(결원 처리 제외)이 최선이다.

    실측: 52병동은 D 요구 4명인데 6명이 배정돼 있어 한 명이 빠져도 5명인데,
    무조건 대체자를 찾다 못 찾고 빈 화면을 내보내고 있었다(20건).
    """
    base = _move(ctx.sequences, target_nurse_id, day, ctx.off_code)
    if delta_violations(_structural_issues(ctx.sequences, ctx, day),
                        _structural_issues(base, ctx, day)):
        return None
    return ChainProposal(
        rank=1, kind="ABSENCE_ONLY", participant_count=0, changed_cell_count=1,
        score=0.0,
        moves=[ChainMove(nurse_id=target_nurse_id, name=_name(ctx, target_nurse_id),
                         date=day, from_shift=shift, to_shift=ctx.off_code,
                         is_absence=True)],
        soft_warnings=["대체 없이 비워도 인원 기준을 만족합니다."]
                      + absence_side_effects(ctx, base, target_nurse_id, day, shift),
    )


def _score_and_filter(
    raw: List[List[Move]], base: Sequences, ctx: ChainContext,
    judge: Optional[ChainContext] = None,
    target_main: Optional[str] = None,
) -> List[Scored]:
    """소프트 점수를 매긴다. 반환은 (점수, 이동, 소프트경고, 하드위반).

    비교 기준선은 **결원을 반영한** `base` 다. 원본과 비교하면 "그 사람이 빠져서"
    생긴 등급 미달·인원 부족까지 제안 탓으로 돌린다(실측 16건). 결원 자체의
    여파는 `absence_side_effects` 가 따로 알린다.

    `judge` 는 통과 기준이다. 차선안 탐색이면 1N 금지만 끈 사본이 들어온다.
    **judge 기준으로는 무위반이어야 통과한다** — 열어 준 규칙 말고 다른 게 깨지면
    차선안으로도 쓰지 않는다. 사용자에게 보여줄 `hard_warnings` 는 그와 별개로
    **원래 기준**으로 다시 계산한다.
    """
    judge = judge or ctx
    relaxed = judge is not ctx
    out = []
    for moves in raw:
        if target_main and not _restores_headcount(moves, base, ctx, target_main):
            continue
        structural, residual = _verify(moves, base, judge, base)
        if structural or residual:
            continue   # 인원·등급·팀은 솔버에서 하드다. 어떤 경우에도 열지 않는다.
        personal = _verify(moves, base, ctx, base)[1] if relaxed else []
        if len(personal) > MAX_FALLBACK_VIOLATIONS:
            continue
        warnings = _new_soft_warnings(moves, base, ctx)
        out.append((_score(moves, ctx, warnings), moves, warnings, personal))
    out.sort(key=lambda item: (len(item[3]), len(item[1]), item[0]))
    return out


def _restores_headcount(
    moves: Sequence[Move], base: Sequences, ctx: ChainContext, main: str,
) -> bool:
    """결원 근무의 인원이 **원본 수준으로** 돌아오는가.

    기준선이 결원 반영 후(`base`)라 이 검사가 없으면 제자리걸음인 안이 통과한다.
    실측: "신솔희 E→N + 이나영 N→E" 는 N 을 채웠다 다시 빼서 7/8 그대로인데
    델타가 0 이라 1순위로 올라왔다.
    """
    day = moves[0][1]
    after = _apply(base, moves)
    return (len(_day_assignment(after, ctx, day)[main])
            >= len(_day_assignment(ctx.sequences, ctx, day)[main]))


def _new_soft_warnings(
    moves: Sequence[Move], base: Sequences, ctx: ChainContext,
) -> List[str]:
    """원본에 없던 소프트 위반만."""
    before = soft_warnings(moves, ctx.sequences, ctx)
    after = soft_warnings(moves, _apply(base, moves), ctx)
    return sorted(set(after) - set(before))


def _dedupe(scored: List[Scored]) -> List[Scored]:
    """같은 (간호사, 최종 근무) 조합은 한 번만 남긴다."""
    seen: Set[tuple] = set()
    out = []
    for item in scored:
        key = tuple(sorted((m[0], m[3]) for m in item[1]))
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _to_proposal(
    ctx: ChainContext,
    moves: List[Move],
    warnings: List[str],
    hard: List[str],
    score: float,
    rank: int,
    target_nurse_id: str,
    day: date,
    shift: str,
) -> ChainProposal:
    """결원자의 OFF 처리까지 포함해 프론트가 그대로 적용할 수 있는 형태로."""
    items = [ChainMove(
        nurse_id=target_nurse_id,
        name=_name(ctx, target_nurse_id),
        date=day,
        from_shift=shift,
        to_shift=ctx.off_code,
        is_absence=True,
    )]
    items += [
        ChainMove(
            nurse_id=nurse_id,
            name=_name(ctx, nurse_id),
            date=move_day,
            from_shift=from_shift,
            to_shift=to_shift,
        )
        for nurse_id, move_day, from_shift, to_shift in moves
    ]
    side = absence_side_effects(
        ctx, _apply(_move(ctx.sequences, target_nurse_id, day, ctx.off_code), moves),
        target_nurse_id, day, shift,
    )
    return ChainProposal(
        rank=rank,
        kind="SINGLE_SWAP" if len(moves) == 1 else "CHAIN",
        participant_count=len(moves),
        changed_cell_count=len(items),
        score=round(score, 2),
        moves=items,
        soft_warnings=list(warnings) + side,
        hard_warnings=list(hard),
    )
