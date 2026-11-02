"""근무표 확인 화면(/roster_create)의 위반 판정을 파이썬으로 그대로 옮긴 것.

화면 판정 정본 = 프론트 useRosterValidation.ts (커밋 f6692a42 기준)
  · validateManpower(94-257)     → `_manpower_violations`
  · validateConfigRules(269-879) → `_config_violations`
근무코드 해석 정본 = utils/shift-util.ts · services/shift/shiftCatalog.ts
입력 정본 = pages/roster-create/Roster_create.tsx · hook/useRosterCreateDerivedNurses.ts ·
  hooks/useRosterConfig.ts · service/getDailyShift.ts · hooks/roster-craete/useRosterPrevTail.ts

화면 버릇(고치지 않고 그대로 옮긴다 — 판정이 화면과 1:1 이어야 후보 검사가 의미 있다):
- three_seq_nig 는 끌 수 없다: 거짓이면 N 3연속, 참이면 N 4연속을 잡는다. 간호사당 첫 1건만 낸다
  (전월 꼬리 안에서만 끝난 구간은 1건으로 치지 않는다). `all_runs=True` 면 1건 제한만 풀어
  조건을 만족하는 모든 시작일 창(겹칠 수 있음)을 낸다.
- banned_day_after_eve 하나가 ED·ND·NE 세 패턴을 모두 켠다.
- two_offs_after_three_nig 가 켜지면 two_offs_after_two_nig 는 검사하지 않는다. N 런 안의 시작일마다
  따로 검사하고(NN N 이 두 번 걸릴 수 있다), OFF 둘째 날이 (전월 꼬리+이번 달) 끝을 넘으면 건너뛴다
  (말일 근처 생략). 퇴사일 이후 칸은 OFF 로 봐준다.
- max_conseq_work 는 월 경계와 무관하게 모든 구간을 낸다(전월 꼬리 안에서 끝난 구간도).
  설정값이 비면(null) 0 과 비교한다(JS 비교 규칙).
- max_nig_per_month 는 이번 달 칸만 세고, 설정값이 0·비면 검사하지 않는다.
- 1N 금지(not_one_night)·연속 OFF 등 나머지 규칙은 화면에 없다.
- 최소인원의 M 은 병동 최신 설정 use_mid 가 False 일 때만 뺀다(설정 규칙과 출처가 다르다).
  최대인원 초과는 화면 상수 ENABLE_MAX_MANPOWER_VIOLATION=false 라 기본 꺼짐.
- 일일 인력을 못 구했거나 유효한 칸이 하나도 없으면 화면은 아무 위반도 내지 않는다.
- 근무표에 연결 설정이 없으면 설정 규칙 검사를 통째로 건너뛴다.

좌표: day·start_day·end_day·next_day·off_* 는 화면과 같은 0 기준이다(1일=0, 전월 꼬리=음수).
"""
from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date, datetime
from types import SimpleNamespace
from typing import Any, Optional

from sqlalchemy.orm import Session

Cells = dict[str, list[Optional[str]]]
CellIds = dict[str, list[Optional[int]]]

_MAIN_LETTERS = ("D", "E", "N", "O", "M")
_SUB_CODES = ("주",)
_GB_LETTER = {"데이": "D", "이브닝": "E", "나이트": "N"}
_NIGHT = ("N", "나이트")
_DAY = ("D", "데이")
_EVE = ("E", "이브닝")
_BANNED = (("ED", _EVE, _DAY), ("ND", _NIGHT, _DAY), ("NE", _NIGHT, _EVE))
_RULE_KEYS = (
    "three_seq_nig", "banned_day_after_eve", "max_conseq_work",
    "two_offs_after_three_nig", "two_offs_after_two_nig", "max_nig_per_month", "use_mid",
)
_TAIL_DAYS = 6


@dataclass(frozen=True)
class ShiftDef:
    """근무코드 한 행(GET /shifts 항목 중 판정에 쓰는 칸)."""

    id: Optional[int]
    shift_id: str
    default_shift: Optional[str] = None
    shift_gb: Optional[str] = None
    type: Optional[str] = None
    sequence: Optional[int] = None


@dataclass
class ScreenInputs:
    """화면 검증 입력 묶음.

    config: 그 근무표가 쓴 설정(None 이면 설정 규칙 검사 생략). config['use_mid'] 는 참고용이고,
      최소인원 M 판정은 화면처럼 병동 최신 설정의 `use_mid` 칸을 쓴다.
    manpower_monthly/daily: 화면 parseManpowerFromApi 모양({'D': 3, 'D_max': 0}, {'D': [..]}).
      monthly 가 None 이면 화면처럼 아무 위반도 내지 않는다.
    prev_tail: 간호사별 전월 꼬리 칸, `tail_days`(오름차순) 와 같은 순서로 맞춘 목록.
    resigned_days·null_cells: 간호사별 1 기준 일 집합. hidden_nurses: 화면 표에 없는 간호사.
    """

    year: int
    month: int
    shifts: list[ShiftDef]
    config: Optional[dict[str, Any]] = None
    use_mid: Optional[bool] = None
    manpower_monthly: Optional[dict[str, Any]] = None
    manpower_daily: Optional[dict[str, list]] = None
    use_max_manpower: bool = False
    enable_max_manpower_violation: bool = False
    prev_tail: dict[str, list[Optional[str]]] = field(default_factory=dict)
    tail_days: list[int] = field(default_factory=list)
    resigned_days: dict[str, set[int]] = field(default_factory=dict)
    null_cells: dict[str, set[int]] = field(default_factory=dict)
    hidden_nurses: set[str] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ScreenViolation:
    """화면 DetailedViolation 과 같은 칸(좌표는 0 기준)."""

    type: str
    nurse_id: Optional[str] = None
    day: Optional[int] = None
    start_day: Optional[int] = None
    end_day: Optional[int] = None
    next_day: Optional[int] = None
    off_start_day: Optional[int] = None
    off_end_day: Optional[int] = None
    pattern: Optional[str] = None
    shift: Optional[str] = None
    required: Optional[float] = None
    required_max: Optional[float] = None
    actual: Optional[int] = None
    max: Optional[float] = None

    @property
    def key(self) -> tuple:
        """규칙 종류와 위치로 만든 열쇠 — 수치(actual 등)는 넣지 않는다."""
        return (self.type, self.nurse_id, self.day, self.start_day,
                self.end_day, self.next_day, self.pattern, self.shift)


# ── 근무코드 해석 (shift-util.ts) ─────────────────────────────────────────────

@dataclass(frozen=True)
class _LookupMaps:
    by_numeric_id: dict[int, ShiftDef]
    by_shift_id: dict[str, ShiftDef]
    by_default: dict[str, list[ShiftDef]]


def _build_lookup_maps(shifts: list[ShiftDef]) -> _LookupMaps:
    """buildShiftLookupMaps — 숫자 id 는 뒤 행이 이기고 shift_id 는 첫 행이 이긴다."""
    by_numeric_id: dict[int, ShiftDef] = {}
    by_shift_id: dict[str, ShiftDef] = {}
    by_default: dict[str, list[ShiftDef]] = {}
    for s in shifts:
        if s.id is not None:
            by_numeric_id[int(s.id)] = s
        by_shift_id.setdefault(s.shift_id, s)
        if s.default_shift is not None:
            by_default.setdefault(s.default_shift, []).append(s)
    return _LookupMaps(by_numeric_id, by_shift_id, by_default)


def _seq(s: ShiftDef) -> int:
    return s.sequence if s.sequence is not None else 0


def _first_by_sequence(cands: Optional[list[ShiftDef]]) -> Optional[ShiftDef]:
    """같은 열쇠 후보 중 sequence 가 앞선 첫 행(JS 안정 정렬 [0] 과 같다)."""
    return min(cands, key=_seq) if cands else None


def _resolve_code(code: Optional[str], maps: _LookupMaps) -> Optional[ShiftDef]:
    """resolveShiftFromScheduleCodeFromMaps — shift_id → default_shift → 첫 글자 순."""
    trimmed = (code or "").strip()
    if not trimmed or trimmed == "-":
        return None
    hit = maps.by_shift_id.get(trimmed) or _first_by_sequence(maps.by_default.get(trimmed))
    if hit is not None:
        return hit
    head = trimmed[0].upper()
    return _first_by_sequence(maps.by_default.get(head)) if head in _MAIN_LETTERS else None


def _compatible(trimmed: str, by_code: Optional[ShiftDef], by_id: ShiftDef) -> bool:
    """isScheduleCodeCompatibleWithResolvedShift — 숫자 id 행이 칸 코드와 어긋나지 않는지."""
    if trimmed == by_id.shift_id:
        return True
    if by_id.default_shift is not None and trimmed == by_id.default_shift:
        return True
    if by_code is None or by_code.shift_id == by_id.shift_id:
        return True
    return (by_code.default_shift is not None and by_id.default_shift is not None
            and by_code.default_shift == by_id.default_shift)


def _resolve_cell(
    code: Optional[str], numeric_id: Optional[int], maps: _LookupMaps
) -> Optional[ShiftDef]:
    """resolveShiftFromScheduleCellFromMaps — 칸의 숫자 id 우선, 코드와 호환될 때만."""
    trimmed = (code or "").strip()
    by_code = _resolve_code(code, maps)
    if not trimmed or trimmed == "-" or numeric_id is None:
        return by_code
    by_id = maps.by_numeric_id.get(int(numeric_id))
    if by_id is None:
        return by_code
    return by_id if _compatible(trimmed, by_code, by_id) else by_code


def _gb_group_key(s: ShiftDef, o_gb: Optional[str]) -> str:
    """buildShiftIdToGroupMainLetterMap 의 shift_gb 묶음 열쇠(주휴·무코드 행은 O 묶음으로)."""
    if s.default_shift == "주" and o_gb:
        return o_gb
    if s.default_shift:
        return s.shift_gb or "etc"
    if s.shift_gb and o_gb and s.shift_gb == o_gb:
        return o_gb
    return s.shift_gb or "etc"


def _group_main_letters(shifts: list[ShiftDef], include_mid: bool) -> dict[str, str]:
    """shift_id → 같은 shift_gb 묶음의 대표 문자. shift_id 열쇠라 뒤 묶음이 이긴다."""
    letters = _MAIN_LETTERS if include_mid else _MAIN_LETTERS[:4]
    o_gb = next((s.shift_gb for s in shifts if s.default_shift == "O"), None)
    groups: dict[str, list[ShiftDef]] = {}
    for s in shifts:
        groups.setdefault(_gb_group_key(s, o_gb), []).append(s)
    out: dict[str, str] = {}
    for members in groups.values():
        letter = next((m.default_shift for m in members if m.default_shift in letters), None)
        if letter:
            out.update({m.shift_id: letter for m in members})
    return out


def _manpower_bucket(
    res: Optional[ShiftDef], letters: dict[str, str], keys: set[str]
) -> Optional[str]:
    """getManpowerBucketForResolvedShift — 묶음 대표 문자, 없으면 default_shift."""
    if res is None:
        return None
    from_group = letters.get(res.shift_id)
    if from_group and from_group in keys:
        return from_group
    return res.default_shift if res.default_shift and res.default_shift in keys else None


def _merge_by_id(shifts: list[ShiftDef]) -> list[ShiftDef]:
    """mergeShiftsForCellLookup — 화면은 같은 /shifts 목록을 두 번 넘기므로 한 목록 병합과 같다."""
    by_id: dict[int, ShiftDef] = {}
    no_id: list[ShiftDef] = []
    for s in shifts:
        if s.id is None:
            no_id.append(s)
        else:
            by_id[int(s.id)] = s
    return [*by_id.values(), *no_id]


def _main_template_code(s: ShiftDef) -> Optional[str]:
    """getShiftMainTemplateCode — 서브코드(주)가 아닌 default_shift, 없으면 대표 shift_id."""
    if s.default_shift and s.default_shift not in _SUB_CODES:
        return s.default_shift
    return s.shift_id if s.shift_id in _MAIN_LETTERS else None


def _prefer(current: ShiftDef, cand: ShiftDef) -> bool:
    """shouldPreferShiftDefinition — default_shift 있는 행, 그다음 큰 id."""
    if not current.default_shift and cand.default_shift:
        return True
    if current.default_shift and not cand.default_shift:
        return False
    if current.id is None:
        return cand.id is not None
    return cand.id is not None and cand.id > current.id


def _dedupe_catalog(shifts: list[ShiftDef]) -> list[ShiftDef]:
    """dedupeMergedShiftRows — '대표코드:shift_id' 가 같으면 한 행만(자리는 첫 행)."""
    by_key: dict[str, ShiftDef] = {}
    for s in shifts:
        key = f"{_main_template_code(s) or ''}:{s.shift_id}"
        if key not in by_key or _prefer(by_key[key], s):
            by_key[key] = s
    return list(by_key.values())


# ── 최소·최대 인원 (validateManpower) ─────────────────────────────────────────

def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _manpower_keys(monthly: dict[str, Any], use_mid: Optional[bool]) -> list[str]:
    """검증 대상 근무 = monthly 의 숫자 키(*_max 제외, use_mid 가 False 면 M 제외)."""
    return [k for k, v in monthly.items()
            if _is_number(v) and not k.endswith("_max") and not (use_mid is False and k == "M")]


def _id_at(ids: Optional[list[Optional[int]]], idx: int) -> Optional[int]:
    return ids[idx] if ids and 0 <= idx < len(ids) else None


def _daily_counts(rows: Cells, cell_ids: CellIds, keys: list[str],
                  shifts: list[ShiftDef], days: int) -> list[dict[str, int]]:
    """일별 근무별 배정 인원 — createShiftCatalog(중복 정리·M 포함 대표 문자) 기준."""
    catalog = _dedupe_catalog(_merge_by_id(shifts))
    maps, letters, key_set = _build_lookup_maps(catalog), _group_main_letters(catalog, True), set(keys)
    counts = [dict.fromkeys(keys, 0) for _ in range(days)]
    for nid, row in rows.items():
        for idx, cell in enumerate(row[:days]):
            trimmed = (cell or "").strip()
            if not trimmed or trimmed == "-":
                continue
            res = _resolve_cell(trimmed, _id_at(cell_ids.get(nid), idx), maps)
            bucket = _manpower_bucket(res, letters, key_set)
            if bucket:
                counts[idx][bucket] += 1
    return counts


def _daily_value(daily: Optional[dict[str, list]], key: str, day: int) -> Any:
    values = (daily or {}).get(key)
    return values[day] if isinstance(values, list) and 0 <= day < len(values) else None


def _manpower_day(day: int, code: str, actual: int, inputs: ScreenInputs) -> list[ScreenViolation]:
    """하루·근무 하나의 미달(일별 값 없으면 월 값)·초과(상한 켜졌고 0 초과일 때만)."""
    out: list[ScreenViolation] = []
    required = _daily_value(inputs.manpower_daily, code, day)
    if required is None:
        required = inputs.manpower_monthly[code]
    if actual < required:
        out.append(ScreenViolation(type="manpower_shortage", day=day, shift=code,
                                   required=required, actual=actual))
    if inputs.use_max_manpower and inputs.enable_max_manpower_violation:
        req_max = _daily_value(inputs.manpower_daily, f"{code}_max", day)
        if _is_number(req_max) and 0 < req_max < actual:
            out.append(ScreenViolation(type="manpower_excess", day=day, shift=code,
                                       required_max=req_max, actual=actual))
    return out


def _manpower_violations(rows: Cells, cell_ids: CellIds, inputs: ScreenInputs) -> list[ScreenViolation]:
    """validateManpower — 일별 최소(·최대) 인원."""
    keys = _manpower_keys(inputs.manpower_monthly or {}, inputs.use_mid)
    if not keys:
        return []
    days = monthrange(inputs.year, inputs.month)[1]
    counts = _daily_counts(rows, cell_ids, keys, inputs.shifts, days)
    out: list[ScreenViolation] = []
    for day in range(days):
        for code in keys:
            out.extend(_manpower_day(day, code, counts[day][code], inputs))
    return out


# ── 설정 규칙 (validateConfigRules) ───────────────────────────────────────────

@dataclass(frozen=True)
class _RuleKit:
    """설정 규칙용 근무코드 판정 도구(validateConfigRules 의 Set·헬퍼 묶음)."""

    maps: _LookupMaps
    letters: dict[str, str]
    id_to_default: dict[str, str]
    work_ids: frozenset[str]
    work_defaults: frozenset[str]
    gb_ids: dict[str, frozenset[str]]

    def normalize(self, shift: Optional[str], res: Optional[ShiftDef]) -> Optional[str]:
        """normalizeShift — 해석된 행(대표 문자·default·shift_gb) 우선, 없으면 코드 문자열로."""
        if res is not None:
            letter = (self.letters.get(res.shift_id) or res.default_shift
                      or _GB_LETTER.get(res.shift_gb or ""))
            if letter:
                return letter
        if not shift or shift == "-":
            return None
        if shift in self.id_to_default:
            return self.id_to_default[shift]
        return shift[0] if len(shift) > 1 and len(set(shift)) == 1 else shift

    def is_kind(self, letter: str, gb: str, shift: Optional[str], res: Optional[ShiftDef]) -> bool:
        """isNightShift·isDayShift·isEveningShift 공통."""
        if not shift:
            return False
        if self.normalize(shift, res) == letter:
            return True
        return (res is not None and res.shift_gb == gb) or shift in self.gb_ids[gb]

    def is_off(self, shift: Optional[str], res: Optional[ShiftDef]) -> bool:
        """isOffDay — 근무가 아니면 OFF. 미배정·'-' 는 OFF 가 아니다."""
        if not shift or shift == "-":
            return False
        if res is not None and res.type in ("근무", "휴무"):
            return res.type == "휴무"
        normalized = self.normalize(shift, res)
        if normalized and normalized in self.work_defaults:
            return False
        return shift not in self.work_ids

    def is_work(self, shift: Optional[str], res: Optional[ShiftDef]) -> bool:
        """연속 근무의 근무일 — 해석되면 type 만, 아니면 코드 집합으로."""
        if not shift or shift == "-":
            return False
        if res is not None:
            return res.type == "근무"
        normalized = self.normalize(shift, None)
        return shift in self.work_ids or bool(normalized and normalized in self.work_defaults)


def _rule_kit(shifts: list[ShiftDef]) -> _RuleKit:
    """설정 규칙은 중복 정리 없이 병합만 한 목록을 쓴다(최소인원과 다르다)."""
    merged = _merge_by_id(shifts)
    work = [s for s in merged if s.type == "근무"]
    return _RuleKit(
        maps=_build_lookup_maps(merged),
        letters=_group_main_letters(merged, include_mid=True),
        id_to_default={s.shift_id: s.default_shift for s in merged if s.default_shift},
        work_ids=frozenset(s.shift_id for s in work),
        work_defaults=frozenset(s.default_shift for s in work if s.default_shift),
        gb_ids={gb: frozenset(s.shift_id for s in merged if s.shift_gb == gb) for gb in _GB_LETTER},
    )


@dataclass(frozen=True)
class _Timeline:
    """간호사 한 명의 [전월 꼬리 + 이번 달] 칸(화면 getShift·getResolvedShift)."""

    shifts: list[Optional[str]]
    resolved: list[Optional[ShiftDef]]
    start: int
    days: int
    resigned: frozenset[int]

    @property
    def total(self) -> int:
        return len(self.shifts)

    def at(self, i: int) -> tuple[Optional[str], Optional[ShiftDef]]:
        if 0 <= i < len(self.shifts):
            return self.shifts[i], self.resolved[i]
        return None, None

    def cur(self, i: int) -> int:
        return i - self.start

    def intersects(self, a: int, b: int) -> bool:
        return b >= self.start and a <= self.start + self.days - 1

    def resigned_at(self, i: int) -> bool:
        day = i - self.start + 1
        return 1 <= day <= self.days and day in self.resigned


def _build_timeline(nid: str, row: list[Optional[str]], ids: Optional[list[Optional[int]]],
                    inputs: ScreenInputs, maps: _LookupMaps) -> _Timeline:
    """꼬리 칸은 숫자 id 없이, 이번 달 칸은 숫자 id 로 해석한다."""
    tail = [c or None for c in inputs.prev_tail.get(nid, [])]
    shifts = tail + [c if isinstance(c, str) and c else None for c in row]
    resolved = [
        None if not c or c == "-"
        else _resolve_cell(c, None if i < len(tail) else _id_at(ids, i - len(tail)), maps)
        for i, c in enumerate(shifts)
    ]
    days = monthrange(inputs.year, inputs.month)[1]
    return _Timeline(shifts, resolved, len(tail), days, frozenset(inputs.resigned_days.get(nid, ())))


@dataclass
class _ScanState:
    nights: int = 0
    run_len: int = 0
    run_start: int = -1
    found_seq: bool = False


def _all_nights(tl: _Timeline, kit: _RuleKit, i: int, n: int) -> bool:
    return all(kit.is_kind(*_NIGHT, *tl.at(i + k)) for k in range(n))


def _three_seq_step(nid: str, tl: _Timeline, kit: _RuleKit, cfg: dict, all_runs: bool,
                    st: _ScanState, i: int) -> list[ScreenViolation]:
    """three_seq_nig — 거짓이면 N 3연속, 참이면 N 4연속. 화면은 간호사당 첫 1건."""
    if st.found_seq and not all_runs:
        return []
    length = 4 if cfg.get("three_seq_nig") else 3
    end = i + length - 1
    if not _all_nights(tl, kit, i, length) or not tl.intersects(i, end):
        return []
    st.found_seq = True
    return [ScreenViolation(type="three_seq_nig", nurse_id=nid,
                            start_day=tl.cur(i), end_day=tl.cur(end))]


def _banned_at(nid: str, tl: _Timeline, kit: _RuleKit, i: int) -> list[ScreenViolation]:
    """banned_day_after_eve — ED·ND·NE 를 한 설정이 같이 켠다."""
    prev, cur = tl.at(i - 1), tl.at(i)
    if not tl.intersects(i - 1, i):
        return []
    return [ScreenViolation(type="banned_day_after_eve", nurse_id=nid, day=tl.cur(i - 1),
                            next_day=tl.cur(i), pattern=pattern)
            for pattern, first, second in _BANNED
            if kit.is_kind(*first, *prev) and kit.is_kind(*second, *cur)]


def _cfg_gt(value: int, cfg: dict, key: str) -> bool:
    """JS `value > cfg[key]` — 키가 없으면(undefined) 거짓, 값이 null 이면 0 과 비교."""
    if key not in cfg:
        return False
    limit = cfg[key]
    return value > (0 if limit is None else limit)


def _conseq_violation(nid: str, tl: _Timeline, cfg: dict, st: _ScanState,
                      end: int) -> list[ScreenViolation]:
    if not _cfg_gt(st.run_len, cfg, "max_conseq_work"):
        return []
    return [ScreenViolation(type="max_conseq_work", nurse_id=nid, max=cfg["max_conseq_work"],
                            actual=st.run_len, start_day=tl.cur(st.run_start), end_day=tl.cur(end))]


def _conseq_step(nid: str, tl: _Timeline, kit: _RuleKit, cfg: dict,
                 st: _ScanState, i: int) -> list[ScreenViolation]:
    """max_conseq_work — 근무가 끊길 때마다 구간을 닫는다(월 경계 무관)."""
    if kit.is_work(*tl.at(i)):
        if st.run_len == 0:
            st.run_start = i
        st.run_len += 1
        return []
    out = _conseq_violation(nid, tl, cfg, st, i - 1)
    st.run_len, st.run_start = 0, -1
    return out


def _needs_off(tl: _Timeline, kit: _RuleKit, i: int) -> bool:
    return not tl.resigned_at(i) and not kit.is_off(*tl.at(i))


def _off_after_nights(nid: str, tl: _Timeline, kit: _RuleKit, i: int, n: int,
                      vtype: str) -> list[ScreenViolation]:
    """N n연속(i 시작) 뒤 OFF 2일 — OFF 둘째 날이 끝을 넘으면 생략, 퇴사일 이후 칸은 봐준다."""
    first, second = i + n, i + n + 1
    if second >= tl.total or not _all_nights(tl, kit, i, n):
        return []
    if not (_needs_off(tl, kit, first) or _needs_off(tl, kit, second)):
        return []
    end = first if tl.resigned_at(second) else second
    if not tl.intersects(i, end):
        return []
    return [ScreenViolation(type=vtype, nurse_id=nid, start_day=tl.cur(i), end_day=tl.cur(end),
                            off_start_day=tl.cur(first), off_end_day=tl.cur(end))]


def _off_rules_at(nid: str, tl: _Timeline, kit: _RuleKit, cfg: dict, i: int) -> list[ScreenViolation]:
    """3N2O 가 켜지면 2N2O 는 보지 않는다(화면의 중복 방지)."""
    if cfg.get("two_offs_after_three_nig"):
        return _off_after_nights(nid, tl, kit, i, 3, "two_offs_after_three_nig")
    if cfg.get("two_offs_after_two_nig"):
        return _off_after_nights(nid, tl, kit, i, 2, "two_offs_after_two_nig")
    return []


def _after_scan(nid: str, tl: _Timeline, cfg: dict, st: _ScanState) -> list[ScreenViolation]:
    """월 N 상한(이번 달만 셈·0 이면 생략)과 끝까지 이어진 연속 근무."""
    out: list[ScreenViolation] = []
    limit = cfg.get("max_nig_per_month")
    if _is_number(limit) and limit > 0 and st.nights > limit:
        out.append(ScreenViolation(type="max_nig_per_month", nurse_id=nid, max=limit,
                                   actual=st.nights))
    out.extend(_conseq_violation(nid, tl, cfg, st, tl.total - 1))
    return out


def _scan_nurse(nid: str, tl: _Timeline, kit: _RuleKit, cfg: dict,
                all_runs: bool) -> list[ScreenViolation]:
    """한 간호사를 한 번 훑는다 — 같은 날 안의 규칙 순서도 화면 그대로."""
    out: list[ScreenViolation] = []
    st = _ScanState()
    for i in range(tl.total):
        if tl.start <= i < tl.start + tl.days and kit.is_kind(*_NIGHT, *tl.at(i)):
            st.nights += 1
        out.extend(_three_seq_step(nid, tl, kit, cfg, all_runs, st, i))
        if cfg.get("banned_day_after_eve"):
            out.extend(_banned_at(nid, tl, kit, i))
        out.extend(_conseq_step(nid, tl, kit, cfg, st, i))
        out.extend(_off_rules_at(nid, tl, kit, cfg, i))
    out.extend(_after_scan(nid, tl, cfg, st))
    return out


def _config_violations(rows: Cells, cell_ids: CellIds, inputs: ScreenInputs,
                       all_runs: bool) -> list[ScreenViolation]:
    """validateConfigRules — 그 근무표 설정의 규칙 위반."""
    kit = _rule_kit(inputs.shifts)
    out: list[ScreenViolation] = []
    for nid, row in rows.items():
        if row is None or not nid:
            continue
        tl = _build_timeline(nid, row, cell_ids.get(nid), inputs, kit.maps)
        out.extend(_scan_nurse(nid, tl, kit, inputs.config, all_runs))
    return out


# ── 공개 함수 ─────────────────────────────────────────────────────────────────

def _validation_rows(cells: Cells, inputs: ScreenInputs) -> Cells:
    """화면 validationCopyNurses — 표에 없는 간호사를 빼고 null_cells 일을 비운다."""
    rows: Cells = {}
    for nid, row in cells.items():
        if nid in inputs.hidden_nurses:
            continue
        nulls = inputs.null_cells.get(nid, set())
        rows[nid] = [None if day + 1 in nulls else cell for day, cell in enumerate(row or [])]
    return rows


def _has_valid_cell(rows: Cells) -> bool:
    return any(c not in (None, "", "-") for row in rows.values() for c in row)


def screen_violations(cells: Cells, cell_ids: Optional[CellIds], inputs: ScreenInputs,
                      *, all_runs: bool = False) -> list[ScreenViolation]:
    """화면이 띄울 위반 목록(최소인원 → 설정 규칙 순).

    cells: {nurse_id: [그날 칸 코드|None, …]} (1일부터). cell_ids: 같은 모양의 shift 숫자 id.
    all_runs: three_seq_nig 의 간호사당 1건 제한을 푼다(화면 그대로가 기본).
    """
    rows = _validation_rows(cells, inputs)
    if not rows or inputs.manpower_monthly is None or not _has_valid_cell(rows):
        return []
    ids = cell_ids or {}
    out = _manpower_violations(rows, ids, inputs)
    if inputs.config is not None:
        out.extend(_config_violations(rows, ids, inputs, all_runs))
    return out


def new_screen_violations(before: list[ScreenViolation],
                          after: list[ScreenViolation]) -> list[ScreenViolation]:
    """after 에만 있는 위반(열쇠 비교). 위반이 다른 날로 옮겨가면 새 위반이다."""
    before_keys = {v.key for v in before}
    return [v for v in after if v.key not in before_keys]


# ── DB 적재 (읽기만) ──────────────────────────────────────────────────────────
# DB 모듈은 함수 안에서 가져온다 — 위 판정 함수는 DB 드라이버 없이도 불러 써야 하기 때문.

def _to_date(value: Any) -> Optional[date]:
    """date·datetime·'YYYY-MM-DD…' 문자열 → date."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _entries_by_nurse(db: Session, schedule_id: str) -> tuple[dict, dict]:
    from db.models import ScheduleEntry

    codes: dict[str, dict[int, Optional[str]]] = {}
    ids: dict[str, dict[int, Optional[int]]] = {}
    rows = (db.query(ScheduleEntry.nurse_id, ScheduleEntry.work_date,
                     ScheduleEntry.shift_id, ScheduleEntry.id)
            .filter(ScheduleEntry.schedule_id == schedule_id).all())
    for nurse_id, work_date, shift_id, shift_row_id in rows:
        codes.setdefault(nurse_id, {})[work_date.day] = shift_id
        ids.setdefault(nurse_id, {})[work_date.day] = shift_row_id
    return codes, ids


def _owner_on(day: date, assignments: list[dict], home: Optional[str]) -> Optional[str]:
    """그날 칸을 가진 병동 — 파견·병동이동 기간이면 그 대상 병동(라우터와 같은 첫 일치)."""
    for a in assignments:
        if a.get("reason") not in ("파견", "병동이동"):
            continue
        start, end = _to_date(a.get("start_date")), _to_date(a.get("end_date"))
        if start is None or day < start or (end is not None and day > end):
            continue
        return a.get("target_group_id") or home
    return home


def _mask_outside_ownership(db: Session, sched: Any, cells: Cells, cell_ids: CellIds) -> None:
    """GET /roster/schedule/{id} 처럼 이 병동 관할 밖 일자를 None 으로 지운다."""
    from db.models import Nurse
    from services.assignment_service import get_roster_assignments

    assignments = get_roster_assignments(db, group_id=sched.group_id, year=sched.year, month=sched.month)
    targets = [nid for nid in cells if assignments.get(nid)]
    if not targets:
        return
    homes = dict(db.query(Nurse.nurse_id, Nurse.group_id).filter(Nurse.nurse_id.in_(targets)).all())
    for nid in targets:
        for idx in range(len(cells[nid])):
            day = date(int(sched.year), int(sched.month), idx + 1)
            if _owner_on(day, assignments[nid], homes.get(nid) or None) != sched.group_id:
                cells[nid][idx] = None
                cell_ids[nid][idx] = None


def load_schedule_cells(db: Session, schedule_id: str) -> tuple[Cells, CellIds]:
    """근무표 칸을 GET /roster/schedule/{id} 의 schedule·schedule_ids 와 같은 값으로 읽는다.

    schedule_ids 는 schedule_entries.id(shifts.id 를 가리키는 칸, PK entry_id 아님)다.
    칸이 하나도 없는 간호사는 판정에 영향이 없어 넣지 않는다.
    """
    from db.models import Schedule

    sched = db.query(Schedule).filter(Schedule.schedule_id == schedule_id).first()
    if sched is None:
        raise ValueError(f"근무표 없음: {schedule_id}")
    days = monthrange(int(sched.year), int(sched.month))[1]
    codes, ids = _entries_by_nurse(db, schedule_id)
    cells = {nid: [by_day.get(d, "-") for d in range(1, days + 1)] for nid, by_day in codes.items()}
    cell_ids = {nid: [by_day.get(d) for d in range(1, days + 1)] for nid, by_day in ids.items()}
    _mask_outside_ownership(db, sched, cells, cell_ids)
    return cells, cell_ids


def _group_office_id(db: Session, group_id: str) -> Optional[str]:
    from db.models import Group

    return db.query(Group.office_id).filter(Group.group_id == group_id).scalar()


def _load_shifts(db: Session, office_id: Optional[str], group_id: str,
                 notes: list[str]) -> list[ShiftDef]:
    """GET /shifts 의 조회 경로(get_shifts_service 의 _list_shifts)와 같은 행·순서.

    서비스 자체는 코드가 없거나 M 이 없으면 기본 코드를 INSERT 하므로 부르지 않는다(읽기만).
    """
    from db.models import Shift
    from services.shift_service_mssql import SHIFT_LIST_ORDER

    rows = (db.query(Shift).filter(Shift.office_id == office_id, Shift.group_id == group_id)
            .order_by(*SHIFT_LIST_ORDER).all())
    if not any("M" in (str(r.shift_id or "").upper(), str(r.default_shift or "").upper()) for r in rows):
        notes.append("근무코드에 M 이 없음 — 화면 GET /shifts 는 이때 기본 코드를 만든다(여기선 만들지 않음)")
    return [ShiftDef(id=r.id, shift_id=r.shift_id, default_shift=r.default_shift,
                     shift_gb=r.shift_gb, type=r.type, sequence=r.sequence) for r in rows]


def _load_rules_config(db: Session, schedule: Any, notes: list[str]) -> Optional[dict[str, Any]]:
    """GET /roster/config/version/latest?schedule_id= 분기와 같은 출처(근무표의 config_id 행)."""
    from db.models import RosterConfig

    if schedule.config_id is None:
        notes.append("근무표에 연결된 설정 없음(NO_LINKED_CONFIG) — 화면처럼 설정 규칙 검사 생략")
        return None
    row = db.query(RosterConfig).filter(RosterConfig.config_id == schedule.config_id).first()
    if row is None:
        notes.append(f"연결 설정 행 없음(LINKED_CONFIG_MISSING config_id={schedule.config_id})"
                     " — 화면처럼 설정 규칙 검사 생략")
        return None
    config = {key: getattr(row, key, None) for key in _RULE_KEYS}
    config["use_mid"] = bool(config["use_mid"])
    return config


def _load_use_mid(db: Session, office_id: Optional[str], group_id: str, notes: list[str]) -> bool:
    """화면 serverConfig(schedule_id 없는 latest)의 use_mid — 병동 최신 설정."""
    from db.models import RosterConfig

    row = (db.query(RosterConfig.use_mid)
           .filter(RosterConfig.office_id == office_id, RosterConfig.group_id == group_id)
           .order_by(RosterConfig.created_at.desc()).first())
    if row is None:
        notes.append("병동 설정 없음 — 화면은 기본 설정(use_mid=False)을 만든다")
        return False
    return bool(row[0])


def _js_number(value: Any) -> float:
    """JS Number() — null 은 0, 숫자가 아니면 NaN."""
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


def _rename_count_key(key: str) -> Optional[str]:
    if key.endswith("_count_max"):
        return key[: -len("_count_max")] + "_max"
    if key.endswith("_count"):
        return key[: -len("_count")]
    return None


def _parse_manpower(resp: dict) -> tuple[dict[str, Any], dict[str, list], bool]:
    """parseManpowerFromApi — 'D_count'→'D', 'D_count_max'→'D_max'."""
    monthly: dict[str, Any] = {}
    for key, value in (resp.get("month_summary") or {}).items():
        name = _rename_count_key(key)
        number = _js_number(value)
        if name and number == number:  # NaN 이 아닐 때만(Number.isFinite)
            monthly[name] = number
    daily: dict[str, list] = {}
    for key, values in (resp.get("date") or {}).items():
        name = _rename_count_key(key)
        if name and isinstance(values, list):
            daily[name] = [_js_number(v) for v in values]
    use_max = resp.get("max_enabled")
    if use_max is None:
        use_max = (resp.get("month_summary") or {}).get("max_enabled")
    return monthly, daily, bool(use_max)


def _template_daily_rows(db: Session, office_id: Optional[str], schedule: Any, notes: list[str]) -> list:
    """일일 인력 행이 없을 때 서비스가 저장할 값(shift_manage 템플릿)을 메모리로만 만든다."""
    from services.daily_shift_service import _query_rn_slots, _read_template_from_shift_manage

    if len(_query_rn_slots(db, office_id, schedule.group_id)) < 3:
        notes.append("일일 인력·shift_manage 템플릿 없음 — 화면은 기본값을 만들거나 500 이라 위반을 못 띄운다")
        return []
    # 슬롯이 3개 이상이면 이 함수는 읽기만 한다(부족할 때만 시딩).
    d, e, n, m = _read_template_from_shift_manage(db, office_id, schedule.group_id)
    notes.append("일일 인력 행 없음 — 화면 GET /daily-shift 처럼 shift_manage 템플릿 값으로 채움(저장 안 함)")
    days = monthrange(int(schedule.year), int(schedule.month))[1]
    zeros = dict.fromkeys(("d_count_max", "e_count_max", "n_count_max", "m_count_max"), 0)
    return [SimpleNamespace(day=day, d_count=d, e_count=e, n_count=n, m_count=m,
                            max_enabled=False, **zeros) for day in range(1, days + 1)]


def _load_manpower(db: Session, office_id: Optional[str], schedule: Any,
                   notes: list[str]) -> tuple[Optional[dict], Optional[dict], bool]:
    """GET /daily-shift(get_or_init_month) 응답을 화면 모양으로 — 행 조회 + _to_response."""
    from db.models import DailyShift
    from services.daily_shift_service import _to_response

    rows = (db.query(DailyShift)
            .filter(DailyShift.office_id == office_id, DailyShift.group_id == schedule.group_id,
                    DailyShift.year == schedule.year, DailyShift.month == schedule.month).all())
    rows = rows or _template_daily_rows(db, office_id, schedule, notes)
    if not rows:
        return None, None, False
    resp = _to_response(office_id, schedule.group_id, int(schedule.year), int(schedule.month), rows)
    return _parse_manpower(resp)


def _load_prev_tail(db: Session, schedule: Any, notes: list[str]) -> tuple[list[int], dict]:
    """GET /roster/{y}/{m}/prev-tail?tail_days=6&schedule_id= — get_prev_month_tail_service."""
    from services.roster_service import get_prev_month_tail_service

    # current_user=None 이면 서비스가 관리자 분기로 group_id 존재만 확인한다(권한 판정은 호출자 몫).
    resp = get_prev_month_tail_service(
        year=int(schedule.year), month=int(schedule.month), schedule_id=schedule.schedule_id,
        tail_days=_TAIL_DAYS, group_id=schedule.group_id, current_user=None, db=db,
    )
    data = resp.get("data") or {}
    nurses = data.get("nurses") or []
    if not nurses:
        notes.append("전월 근무표 없음 — 전월 꼬리 없이 검사(화면도 같다)")
        return [], {}
    days = sorted(d for d in (int(x) for x in data.get("tail_days") or []) if 1 <= d <= 31)
    tail = {str(n["nurse_id"]): [(n.get("shifts") or {}).get(str(d)) for d in days] for n in nurses}
    return days, tail


def _load_resigned_days(db: Session, schedule: Any) -> dict[str, set[int]]:
    """GET /nurses/members(group_members_in_month)의 resign_date → 퇴사 처리된 일."""
    from services.assignment_service import group_members_in_month

    year, month = int(schedule.year), int(schedule.month)
    result = group_members_in_month(db, schedule.group_id, year, month)
    days = monthrange(year, month)[1]
    out: dict[str, set[int]] = {}
    for member in result.get("members", []):
        resign = _to_date(member.get("resign_date"))
        if resign is not None:
            out[str(member["nurse_id"])] = {
                d for d in range(1, days + 1) if date(year, month, d) >= resign}
    return out


def _listed_by_assignment(db: Session, group_id: str, nurse_ids: list[str]) -> set[str]:
    """GET /nurses 가 다른 병동 간호사를 넣는 두 경우(받은 파견·이동, 나간 병동이동)."""
    if not nurse_ids:
        return set()
    from sqlalchemy import and_, or_

    from db.models import NurseAssignment
    from services.group_access import _INBOUND_REASONS

    na = NurseAssignment
    inbound = and_(na.target_group_id == group_id, na.status == "active",
                   na.reason.in_(_INBOUND_REASONS))
    outbound = and_(na.source_group_id == group_id, na.target_group_id != group_id,
                    na.reason == "병동이동")
    rows = (db.query(na.nurse_id).filter(na.nurse_id.in_(nurse_ids), or_(inbound, outbound))
            .distinct().all())
    return {r[0] for r in rows}


def _load_hidden_nurses(db: Session, schedule: Any) -> set[str]:
    """화면 표에서 빠지는 간호사 — /nurses 목록에 없거나 active 가 거짓(computeCopyNurses)."""
    from db.models import Nurse, ScheduleEntry

    ids = [r[0] for r in db.query(ScheduleEntry.nurse_id)
           .filter(ScheduleEntry.schedule_id == schedule.schedule_id).distinct().all()]
    if not ids:
        return set()
    rows = db.query(Nurse.nurse_id, Nurse.active, Nurse.group_id).filter(Nurse.nurse_id.in_(ids)).all()
    listed = {r.nurse_id for r in rows if r.group_id == schedule.group_id}
    others = [r.nurse_id for r in rows if r.nurse_id not in listed]
    listed |= _listed_by_assignment(db, schedule.group_id, others)
    active = {r.nurse_id for r in rows if r.active}
    return {nid for nid in ids if nid not in listed or nid not in active}


def load_screen_inputs(db: Session, schedule: Any, with_prev_tail: bool = True) -> ScreenInputs:
    """근무표 하나의 화면 검증 입력을 화면이 부르는 API 의 서비스 경로로 모은다(읽기만).

    schedule: Schedule 행. with_prev_tail: 화면은 '전월 보기' 패널을 열었을 때만 전월 꼬리를
    넣는다 — 기본 True(전월 경계 위반까지 본다). null_cells 는 비워 둔다: 화면이 프리셉티 기간을
    검증에 포함하고(includePrecepteeInValidationCount=true) 거절 원티드 투명화(isAppliedMap)는
    원티드 모달에서만 하기 때문.
    """
    notes: list[str] = []
    office_id = _group_office_id(db, schedule.group_id)
    monthly, daily, use_max = _load_manpower(db, office_id, schedule, notes)
    inputs = ScreenInputs(
        year=int(schedule.year), month=int(schedule.month),
        shifts=_load_shifts(db, office_id, schedule.group_id, notes),
        config=_load_rules_config(db, schedule, notes),
        use_mid=_load_use_mid(db, office_id, schedule.group_id, notes),
        manpower_monthly=monthly, manpower_daily=daily, use_max_manpower=use_max,
        resigned_days=_load_resigned_days(db, schedule),
        hidden_nurses=_load_hidden_nurses(db, schedule), notes=notes,
    )
    if with_prev_tail:
        inputs.tail_days, inputs.prev_tail = _load_prev_tail(db, schedule, notes)
    else:
        notes.append("전월 꼬리 미포함(화면 '전월 보기' 패널을 닫은 상태와 같다)")
    return inputs
