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
MAX_CHAIN_DEPTH = 3      # 연쇄에 동원할 최대 인원
RAW_PROPOSAL_LIMIT = 60  # 순위 매기기 전 원안 수집 상한
# 2단 자유 구간 반경(일). 전수 실측(1단 미해결 N 결원 165건) 기준:
#   K=1  56.4% ·  56ms  |  K=2  91.5% ·  96ms  |  K=3  98.2% · 153ms
# 셋 다 변경 셀 규모가 비슷해 K=3 이 명백히 낫다.
LNS_FREE_RADIUS = 3
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
    오히려 완화된 경우까지 새 위반으로 잘못 잡는다.
    """
    before_count = Counter(_kind(m) for m in before)
    after_count = Counter(_kind(m) for m in after)
    return [k for k, n in after_count.items() if n > before_count.get(k, 0)]


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
    if (nurse_id, day) in ctx.fixed_cells:
        out.append("확정 원티드 셀")
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


def _name(ctx: ChainContext, nurse_id: str) -> str:
    return ctx.name_of.get(nurse_id, nurse_id)


# ── 탐색 ────────────────────────────────────────────────────────────────────


def _sweep(
    day: date, shift: str, seqs: Sequences, ctx: ChainContext, exclude: Set[str],
) -> List[Tuple[str, str, bool]]:
    """(day, shift) 를 메울 수 있는 후보 → [(nurse_id, 현재 근무, 빈자리 생김 여부)]."""
    main = ctx.main_of.get(shift, shift)
    assignment = _day_assignment(seqs, ctx, day)
    out = []
    for nurse_id in seqs:
        if nurse_id in exclude:
            continue
        current = _shift_at(seqs, nurse_id, day)
        if current is None or ctx.main_of.get(current, current) == main:
            continue  # 배정 없음 / 이미 그 근무 — 옮겨도 결원이 안 메워진다
        moved = _move(seqs, nurse_id, day, shift)
        before = hard_violations(nurse_id, day, current, seqs, ctx)
        if delta_violations(before, hard_violations(nurse_id, day, shift, moved, ctx)):
            continue  # 원본에 없던 하드 위반이 새로 생긴다
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


def _state_issues(
    seqs: Sequences, ctx: ChainContext, day: date, who: Set[str],
) -> List[str]:
    """그 날짜 기준 하드 위반 목록 — 커버리지 · 등급 · 관련자 개인규칙."""
    assignment = _day_assignment(seqs, ctx, day)
    out = []
    for main, required in ctx.coverage_req.items():
        if required and len(assignment[main]) < required:
            out.append(f"{main} 인원 {len(assignment[main])}/{required}")
    for main in ("D", "E", "N"):
        for issue in grade_shortage(assignment[main], ctx.grade_of, (ctx.grade_min or {}).get(main)):
            out.append(f"{main} {issue}")
    out += team_min_shortage(assignment, ctx)
    for nurse_id in who:
        current = _shift_at(seqs, nurse_id, day) or ctx.off_code
        out += [f"{_name(ctx, nurse_id)}: {v}"
                for v in hard_violations(nurse_id, day, current, seqs, ctx)]
    return out


def _apply(seqs: Sequences, moves: Sequence[Move]) -> Sequences:
    out = seqs
    for nurse_id, day, _from, to_shift in moves:
        out = _move(out, nurse_id, day, to_shift)
    return out


def _verify(
    moves: Sequence[Move], base: Sequences, ctx: ChainContext, origin: Sequences,
) -> List[str]:
    """제안을 결과 상태로 다시 검사한다. 판정은 '원본 대비 악화 금지'다.

    원본 근무표가 이미 깨뜨리고 있는 항목까지 제안 탓으로 돌리면 그 날짜의 모든
    안이 자동 탈락한다. 새로 생긴 위반만 센다.
    """
    day = moves[0][1]
    who = {m[0] for m in moves}
    return sorted(delta_violations(
        _state_issues(origin, ctx, day, who),
        _state_issues(_apply(base, moves), ctx, day, who),
    ))


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
    free_radius: int = LNS_FREE_RADIUS,
) -> Optional[ChainProposal]:
    """2단 — 1단(당일 1열)으로 못 푸는 결원을 인접 일자까지 열어 푼다.

    나이트 결원은 당일 1열만 봐서는 원리적으로 안 풀린다(1N 금지·연속 N 상한·회복이
    전부 하드라 최소 2일 블록이 필요하다). 결원일 ±`free_radius` 일을 자유변수로 연다.

    경로는 둘이다.
      ① **전용 CP-SAT 모델**(기본) — 자유 구간만 변수로 세운다. 실측 74ms.
      ② 생성 파이프라인 dry-run(폴백) — ①이 해를 못 내면. 실측 7.8s.

    ①은 제약을 이 모듈에서 다시 세우므로, 결과를 반드시 기존 체커로 재검증한다
    (`_local_result_ok`). 체커가 물면 ②로 넘어간다.

    **1단이 빈손일 때만 부른다.**  저장은 하지 않는다.
    """
    free_dates = _free_dates(day, free_radius, ctx)
    local = _repair_via_local_cpsat(ctx, target_nurse_id, day, shift, free_dates)
    if local is not None:
        return local
    logger.info("[LNS] 전용 모델 실패 — 생성 파이프라인 dry-run 으로 폴백")
    return _repair_via_generation(
        db, ctx, schedule, current_user, target_nurse_id, day, shift, free_radius)


def _free_dates(day: date, radius: int, ctx: ChainContext) -> List[date]:
    """자유 구간 일자 목록 — 그 달 범위를 벗어나지 않게 자른다."""
    last = calendar.monthrange(day.year, day.month)[1]
    out = []
    for offset in range(-radius, radius + 1):
        d = day + timedelta(days=offset)
        if 1 <= d.day <= last and d.month == day.month:
            out.append(d)
    return out


def _repair_via_generation(
    db: Session,
    ctx: ChainContext,
    schedule,
    current_user,
    target_nurse_id: str,
    day: date,
    shift: str,
    free_radius: int,
) -> Optional[ChainProposal]:
    """폴백 — 생성 파이프라인을 dry-run 으로 돌려 결과를 받는다(7.8s)."""
    from services.roster_create_service import generate_roster_service  # 지연 import(무거움)
    from schemas.roster_schema import RosterRequest

    baseline = _baseline_cells(ctx)
    if not baseline:
        return None
    free_days = _free_day_indices(day, free_radius, ctx)
    cells = _lns_fixed_cells(baseline, free_days)
    if cells is None:
        return None
    # 결원 당사자는 그 날 OFF 로 못박는다. 자유변수로 두면 솔버가 원래대로 배정해
    # "결원이 없는 근무표"를 그대로 돌려준다(대체가 아니다).
    cells.append({"nurse_id": target_nurse_id, "day_index": day.day - 1,
                  "shift": ctx.off_code})

    try:
        result = generate_roster_service(
            RosterRequest(year=int(schedule.year), month=int(schedule.month),
                          group_id=str(schedule.group_id)),
            current_user, db, dry_run=True, lns_fixed_cells=cells,
        )
    except Exception:  # noqa: BLE001 — 생성 실패는 '해 없음'으로 흡수한다
        db.rollback()
        logger.warning("[LNS] dry-run 생성 실패 — 제안 없음", exc_info=True)
        return None

    generated = (result or {}).get("generated") or {}
    return _lns_result_to_proposal(
        ctx, generated, baseline, target_nurse_id, day, shift, free_days,
    )


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


def _repair_via_local_cpsat(
    ctx: ChainContext,
    target_nurse_id: str,
    day: date,
    shift: str,
    free_dates: List[date],
) -> Optional[ChainProposal]:
    """전용 모델로 푼다. 해가 없거나 체커가 물면 None(→ 폴백)."""
    try:
        from ortools.sat.python import cp_model  # 지연 import — 1단 경로는 안 쓴다
    except ImportError:
        return None
    lm = _build_local_model(cp_model, ctx, target_nurse_id, day, free_dates)
    if lm is None:
        return None
    solution = _solve_local(cp_model, lm)
    if solution is None:
        return None
    state = _apply_local_solution(ctx, solution)
    if not _local_result_ok(ctx, state, solution, target_nurse_id, day, shift):
        return None
    return _local_solution_to_proposal(ctx, solution, target_nurse_id, day, shift)


def _build_local_model(cp_model, ctx, target_nurse_id, day, free_dates) -> Optional[_LocalModel]:
    """변수·제약을 세운다. 결원 칸이 자유 셀이 아니면(휴가 등) 포기한다."""
    codes = sorted({ctx.main_of.get(s, s) for s in ctx.categories["work"]} | {ctx.off_code})
    free_cells = _local_free_cells(ctx, free_dates, codes)
    free_set = set(free_cells)
    if (target_nurse_id, day) not in free_set:
        return None
    model = cp_model.CpModel()
    x = {(n, d, c): model.NewBoolVar(f"x_{n}_{d}_{c}")
         for (n, d) in free_cells for c in codes}
    lm = _LocalModel(ctx=ctx, model=model, codes=codes, free_dates=free_dates,
                     free_set=free_set, x=x, span=_local_span(free_dates))
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
    ctx: ChainContext, free_dates: List[date], codes: List[str],
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
    """원본에서 그 날이 want 집합인가 (0/1)."""
    current = _shift_at(lm.ctx.sequences, nurse_id, d)
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
    if (nurse_id, d) in ctx.fixed_cells:
        current = _shift_at(ctx.sequences, nurse_id, d)
        if current:
            m.Add(x[nurse_id, d, ctx.main_of.get(current, current)] == 1)
    if d.day in ctx.blocked_days.get(nurse_id, set()):
        m.Add(x[nurse_id, d, ctx.off_code] == 1)   # 관할 밖 — 근무 배정 불가


def _add_coverage_rules(lm: _LocalModel) -> None:
    """커버리지·등급 최소인원 — 원본 수준 아래로만 안 떨어지면 된다(악화 금지)."""
    ctx, m = lm.ctx, lm.model
    for d in lm.free_dates:
        assigned = _day_assignment(ctx.sequences, ctx, d)
        for main, need in ctx.coverage_req.items():
            if not need or main not in lm.codes:
                continue
            m.Add(_headcount(lm, d, main) >= min(need, len(assigned[main])))
        for main in ("D", "E", "N"):
            _add_grade_rules(lm, d, main, assigned[main])
        _add_team_min_rules(lm, d, assigned)


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
    """목적 — 원본에서 바뀌는 셀 수 최소화(MPP)."""
    terms = []
    for nurse_id, d in free_cells:
        current = _shift_at(lm.ctx.sequences, nurse_id, d)
        terms.append(1 - lm.x[nurse_id, d, lm.ctx.main_of.get(current, current)])
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


def _apply_local_solution(ctx: ChainContext, solution: Dict[Tuple[str, date], str]) -> Sequences:
    out = {n: list(seq) for n, seq in ctx.sequences.items()}
    for (nurse_id, d), code in solution.items():
        out[nurse_id] = [(d2, code if d2 == d else s) for d2, s in out[nurse_id]]
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
    touched = {n for (n, d), c in solution.items()
               if _shift_at(ctx.sequences, n, d) != c}
    for nurse_id in touched:
        for d in sorted({d for (n, d) in solution if n == nurse_id}):
            before = hard_violations(
                nurse_id, d, _shift_at(ctx.sequences, nurse_id, d) or ctx.off_code,
                ctx.sequences, ctx)
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
        if before == code or (nurse_id == target_nurse_id and d == day):
            continue
        moves.append(ChainMove(nurse_id=nurse_id, name=_name(ctx, nurse_id), date=d,
                               from_shift=before or ctx.off_code, to_shift=code))
    if len(moves) < 2:
        return None
    participants = len({m.nurse_id for m in moves if not m.is_absence})
    return ChainProposal(
        rank=1, kind="LNS", participant_count=participants,
        changed_cell_count=len(moves),
        score=round(W_CHAIN_STEP * participants + len(moves), 2),
        moves=moves,
        soft_warnings=["여러 날짜가 함께 조정됩니다. 적용 전 변경 셀을 확인하세요."],
    )


def _baseline_cells(ctx: ChainContext) -> Dict[Tuple[str, int], str]:
    """{(nurse_id, day_index): shift_id} — 고정할 원본 근무표."""
    return {
        (nurse_id, entry_date.day - 1): shift
        for nurse_id, seq in ctx.sequences.items()
        for entry_date, shift in seq
    }


def _free_day_indices(day: date, radius: int, ctx: ChainContext) -> Set[int]:
    last = calendar.monthrange(day.year, day.month)[1]
    center = day.day - 1
    return {d for d in range(center - radius, center + radius + 1) if 0 <= d < last}


def _lns_fixed_cells(
    baseline: Dict[Tuple[str, int], str], free_days: Set[int],
) -> Optional[List[dict]]:
    """자유 구간을 뺀 나머지를 고정셀로 만든다.

    **nurse_id 기반으로 넘긴다.** 엔진 목록 순서는 nurse_id 정렬이 아니므로
    여기서 인덱스를 만들면 엉뚱한 간호사에게 고정돼 INFEASIBLE 이 된다.
    인덱스 변환은 엔진 목록이 확정된 생성 파이프라인 안에서 한다.
    """
    out = [
        {"nurse_id": nurse_id, "day_index": day_index, "shift": shift}
        for (nurse_id, day_index), shift in baseline.items()
        if day_index not in free_days
    ]
    return out or None


def _lns_result_to_proposal(
    ctx: ChainContext,
    generated: Dict[str, List[str]],
    baseline: Dict[Tuple[str, int], str],
    target_nurse_id: str,
    day: date,
    shift: str,
    free_days: Set[int],
) -> Optional[ChainProposal]:
    """생성 결과를 원본과 대조해 바뀐 셀만 `moves` 로 뽑는다.

    **자유 구간 밖의 변경은 버린다.** 고정셀로 넣었는데도 값이 달라진 칸은
    후처리(`postprocess_off_swap` 등)가 건드린 것이라 이번 결원과 무관하다.
    """
    day_index = day.day - 1
    filled = _cell_of(generated, target_nurse_id, day_index)
    if not filled or filled == shift:
        return None  # 결원자가 그대로면 메워진 게 아니다

    moves: List[ChainMove] = [ChainMove(
        nurse_id=target_nurse_id, name=_name(ctx, target_nurse_id), date=day,
        from_shift=shift, to_shift=ctx.off_code, is_absence=True,
    )]
    for (nurse_id, idx), before in sorted(baseline.items()):
        after = _cell_of(generated, nurse_id, idx)
        if after is None or after == before:
            continue
        if idx not in free_days:
            continue  # 고정 구간인데 바뀐 것 = 후처리 산물. 제안에 넣지 않는다
        if nurse_id == target_nurse_id and idx == day_index:
            continue  # 결원 처리로 이미 넣었다
        moves.append(ChainMove(
            nurse_id=nurse_id, name=_name(ctx, nurse_id),
            date=date(day.year, day.month, idx + 1),
            from_shift=before, to_shift=after,
        ))
    if len(moves) < 2:
        return None

    participants = len({m.nurse_id for m in moves if not m.is_absence})
    return ChainProposal(
        rank=1, kind="LNS", participant_count=participants,
        changed_cell_count=len(moves),
        score=round(W_CHAIN_STEP * participants + len(moves), 2),
        moves=moves,
        soft_warnings=["여러 날짜가 함께 조정됩니다. 적용 전 변경 셀을 확인하세요."],
    )


def _cell_of(generated: Dict[str, List[str]], nurse_id: str, day_index: int) -> Optional[str]:
    row = generated.get(nurse_id)
    if not row or day_index < 0 or day_index >= len(row):
        return None
    return str(row[day_index] or "").strip() or None


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
        _state_issues(ctx.sequences, ctx, day, {candidate_id}),
        _state_issues(moved, ctx, day, {candidate_id}),
    )
    return personal + [s for s in structural if s not in personal]


def recommend_chain_proposals(
    ctx: ChainContext,
    target_nurse_id: str,
    day: date,
    shift: str,
    limit: int = 10,
) -> List[ChainProposal]:
    """결원 (target_nurse_id, day, shift) 에 대한 수정안 목록.

    1인 스왑과 다인 연쇄를 같은 목록에 담아 (인원수, 점수) 오름차순으로 준다.
    MPP 목적상 변경 셀이 적은 안이 먼저다.
    """
    if target_nurse_id not in ctx.sequences:
        return []
    if _shift_at(ctx.sequences, target_nurse_id, day) is None:
        return []
    base = _move(ctx.sequences, target_nurse_id, day, ctx.off_code)

    raw: List[List[Move]] = []
    _find_chains(day, shift, base, ctx, 1, {target_nurse_id}, [], raw)

    scored = _score_and_filter(raw, base, ctx)
    return [_to_proposal(ctx, moves, warnings, score, rank, target_nurse_id, day, shift)
            for rank, (score, moves, warnings) in enumerate(_dedupe(scored)[:limit], start=1)]


def _score_and_filter(
    raw: List[List[Move]], base: Sequences, ctx: ChainContext,
) -> List[Tuple[float, List[Move], List[str]]]:
    """하드 위반 안을 버리고 소프트 점수를 매긴다."""
    out = []
    for moves in raw:
        if _verify(moves, base, ctx, ctx.sequences):
            continue
        warnings = _new_soft_warnings(moves, base, ctx)
        out.append((_score(moves, ctx, warnings), moves, warnings))
    out.sort(key=lambda item: (len(item[1]), item[0]))
    return out


def _new_soft_warnings(
    moves: Sequence[Move], base: Sequences, ctx: ChainContext,
) -> List[str]:
    """원본에 없던 소프트 위반만."""
    before = soft_warnings(moves, ctx.sequences, ctx)
    after = soft_warnings(moves, _apply(base, moves), ctx)
    return sorted(set(after) - set(before))


def _dedupe(
    scored: List[Tuple[float, List[Move], List[str]]],
) -> List[Tuple[float, List[Move], List[str]]]:
    """같은 (간호사, 최종 근무) 조합은 한 번만 남긴다."""
    seen: Set[tuple] = set()
    out = []
    for score, moves, warnings in scored:
        key = tuple(sorted((m[0], m[3]) for m in moves))
        if key in seen:
            continue
        seen.add(key)
        out.append((score, moves, warnings))
    return out


def _to_proposal(
    ctx: ChainContext,
    moves: List[Move],
    warnings: List[str],
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
    return ChainProposal(
        rank=rank,
        kind="SINGLE_SWAP" if len(moves) == 1 else "CHAIN",
        participant_count=len(moves),
        changed_cell_count=len(items),
        score=round(score, 2),
        moves=items,
        soft_warnings=warnings,
    )
