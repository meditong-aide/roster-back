"""근무표 하나를 고칠 때 필요한 판정 재료를 한 번에 모은다(요청당 1회).

출처는 전부 생성 엔진과 같은 로더다. 엔진 입력 조립은 `roster_create_service` 안에 인라인으로
흩어져 있어(외부 재사용 함수 없음) 여기서 필요한 것만 같은 함수로 다시 부른다.

★ 설정은 그룹 최신이 아니라 **그 근무표가 쓴 config**(`schedule.config_id`)다([RVERIFY-08]).
  config_id 가 없으면(엑셀 임포트본) 그룹 최신으로 떨어지고 notes 에 남긴다.
★ 엔진처럼 '그 달 값'(period as-of)으로 본다. 간호사 ORM 객체는 건드리지 않는다 — 엔진의
  `__dict__` 덮어쓰기는 같은 세션에서 나중에 저장할 때 값이 섞일 수 있다.
★ 판정은 '지금 근무표보다 나빠지지 않게'라, 여기 값이 엔진과 조금 달라도 기준선이 같이
  움직여 상쇄된다. 다만 상한·하한 값 자체는 결과 카드 문구에 나가므로 엔진 출처를 따른다.
"""

from __future__ import annotations

import calendar
import json
import logging
from collections import defaultdict
import dataclasses
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Set, Tuple

from sqlalchemy.orm import Session

from db.models import (
    FixedWantedEntry, Nurse, NurseAllowedShiftPeriod, NurseGradePeriod, NurseShiftRequest, Schedule,
    ScheduleEntry, Shift, Team, WantedRequest,
)
from services.cp_sat.allowed_shift_types import is_n_only_profile, normalize_allowed_shift_codes
from services.cp_sat.off_policy import resolve_effective_off_days
from services.cp_sat.shift_normalizer import build_shift_normalizer

logger = logging.getLogger(__name__)


#: 근무로 세는 대표코드. W 는 대표코드 없는 근무(수간호사 `DE` 등) — 고정칸으로만 존재한다.
WORK_MAINS = ("D", "E", "N", "M", "W")
#: 모델이 고를 수 있는 대표코드(M 은 use_mid 일 때만)
EDIT_MAINS = ("D", "E", "N", "M", "O")
VACATION_TYPES = {"휴가", "공가"}
#: 확정 원티드 칸의 고정 이유 — 수간호사가 그 칸을 직접 짚은 요청에서만 풀린다(`with_released`)
FIXED_WANTED = "확정 원티드"
#: 전월 꼬리 길이 — 화면(prev-tail 6일)·엔진(lookback 6) 과 같다. N 간격이 더 길면 늘린다.
TAIL_DAYS = 6

Cell = Tuple[str, int]  # (nurse_id, 0-based day)


@dataclass
class EditContext:
    """판정 재료. 이후 후보 계산은 DB 없이 이 위에서만 돈다."""

    schedule_id: str
    group_id: str
    office_id: str
    year: int
    month: int
    days: int
    config_id: Optional[int] = None
    nurse_ids: List[str] = field(default_factory=list)
    names: Dict[str, str] = field(default_factory=dict)
    #: 칸 원문 shift_id·shift 숫자 id·대표코드 (없는 칸은 None)
    raw: Dict[str, List[Optional[str]]] = field(default_factory=dict)
    raw_ids: Dict[str, List[Optional[int]]] = field(default_factory=dict)
    main: Dict[str, List[Optional[str]]] = field(default_factory=dict)
    #: 휴가·공가 칸 — O 로 세지만 '순수 OFF' 가 아니다(OFF 수에서 뺀다)
    vacation: Set[Cell] = field(default_factory=set)
    #: 이 병동의 휴가·공가 근무 코드 → 이름(연차·병가 등) — 칸에 넣을 수 있는 휴가(`requests.LeaveCell`)
    leave_codes: Dict[str, str] = field(default_factory=dict)
    #: 바꿀 수 없는 칸과 그 이유
    frozen: Dict[Cell, str] = field(default_factory=dict)
    tail: Dict[str, List[str]] = field(default_factory=dict)
    config: Dict[str, Any] = field(default_factory=dict)
    use_mid: bool = False
    rep_code: Dict[str, str] = field(default_factory=dict)
    forbidden: Dict[Cell, Set[str]] = field(default_factory=dict)
    weekend_off: Set[str] = field(default_factory=set)
    weekend_days: Set[int] = field(default_factory=set)
    cov_min: List[Dict[str, int]] = field(default_factory=list)
    cov_max: List[Dict[str, int]] = field(default_factory=list)
    cov_exclude: Set[Cell] = field(default_factory=set)
    grade_of: Dict[str, Optional[int]] = field(default_factory=dict)
    grade_min: Dict[str, Any] = field(default_factory=dict)
    team_of: Dict[str, Optional[int]] = field(default_factory=dict)
    team_min: Dict[int, Dict[str, int]] = field(default_factory=dict)
    monthly_limits: Dict[str, Dict[str, Optional[int]]] = field(default_factory=dict)
    single_n_ok: Set[str] = field(default_factory=set)
    n_only: Set[str] = field(default_factory=set)
    single_shift: Dict[str, str] = field(default_factory=dict)
    mutex: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    ban_n_before: Set[Cell] = field(default_factory=set)
    n2n_gap: int = 0
    same_shift_k: int = 0
    isolated_work_hard: bool = False
    off_bounds: Dict[str, Tuple[int, int]] = field(default_factory=dict)
    #: 간호사 원티드(희망) — 칸 → 바란 대표코드들. 확정 원티드로 이미 묶인 칸은 뺀다.
    wishes: Dict[Cell, Set[str]] = field(default_factory=dict)
    #: 원티드 중 도우미가 넣지 않는 것 — (간호사, 날, "code"|"locked", 원하는 코드). code = 근무·OFF 가 아닌 코드(휴가 등),
    #: locked = 바꿀 수 없는 칸(파견·프리셉티 등). 답이 요청 범위(날·사람) 안만 센다
    wish_skipped: List[Tuple[str, int, str, str]] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    #: 사용자가 정한 고칠 수 있는 첫날(0부터) — 그 앞은 모두 그대로 둔다. 없으면 오늘까지만 닫는다(`requests.first_open_day`)
    edit_from: Optional[int] = None
    #: 확정 원티드인데 근무표 칸이 그 원티드와 다른 칸(그룹웨어 가져오기 등) — 원티드 반영 요청이 원티드대로만 바꿀 수 있다
    #: (`wish_targets` · `with_wish_cells`). ctx.wishes 에도 든다
    wish_fixed: Set[Cell] = field(default_factory=set)
    #: 바뀐 칸을 저장할 코드 — 나이트에 주로 묶음 코드를 쓰던 사람의 N 은 그 코드로(예: 윤지원 N → NK · `_note_group_codes`)
    keep_code: Dict[str, Dict[str, str]] = field(default_factory=dict)

    def is_free(self, nurse_id: str, day: int) -> bool:
        return (nurse_id, day) not in self.frozen

    def day_date(self, day: int) -> date:
        return date(self.year, self.month, day + 1)


# ── 진입점 ──────────────────────────────────────────────────────────────────


#: 판정 기준 사본이 없을 때 — 무엇이 '지금 값'인지 카드에 알린다
BASIS_MISSING_NOTE = ("이 근무표는 판정 기준 사본(생성 당시 값)이 없어 일별 인원·등급·팀 최소·간호사 속성·"
                      "원티드·전월 근무를 현재 값으로 판정했습니다.")


def load_edit_context(db: Session, schedule_id: str, *, use_basis: bool = True) -> EditContext:
    """근무표 하나의 판정 재료를 모은다. 없는 근무표면 LookupError.

    use_basis: 생성 당시 사본(`schedule_edit_basis`)이 있으면 그걸로 판정한다(사용자 지시 11-02 —
    지금 값이 아니라 그 근무표의 스냅샷 기준). 사본을 **만들 때**는 False 로 지금 값을 읽는다.
    """
    schedule = db.query(Schedule).filter(Schedule.schedule_id == schedule_id).first()
    if schedule is None:
        raise LookupError(f"근무표 없음: {schedule_id}")
    year, month = int(schedule.year), int(schedule.month)
    ctx = EditContext(
        schedule_id=str(schedule_id), group_id=str(schedule.group_id),
        office_id=str(getattr(schedule, "office_id", "") or ""), year=year, month=month,
        days=calendar.monthrange(year, month)[1],
        config_id=getattr(schedule, "config_id", None),
    )
    if use_basis:
        from services.schedule_edit.basis import apply_basis, read_basis
        payload = read_basis(db, ctx.schedule_id)
        if payload is not None:
            apply_basis(db, ctx, payload)
            return ctx
        ctx.notes.append(BASIS_MISSING_NOTE)
    _load_current(db, ctx)
    return ctx


def load_cells(db: Session, ctx: EditContext) -> List[dict]:
    """근무 정의·칸·이름만 — 사본으로 판정할 때도 칸은 지금 근무표에서 읽는다."""
    shifts = _load_shift_defs(db, ctx)
    _load_cells(db, ctx, shifts)
    if ctx.nurse_ids:
        ctx.names = {str(nid): name for nid, name in
                     db.query(Nurse.nurse_id, Nurse.name).filter(Nurse.nurse_id.in_(ctx.nurse_ids)).all()}
    return shifts


def refresh_derived(ctx: EditContext) -> None:
    """칸·설정에서 다시 계산하는 것(휴가 전날 N 금지)."""
    _load_ban_n_before(ctx)


def refresh_facts(db: Session, ctx: EditContext) -> List[FixedWantedEntry]:
    """지금 사실로 막을 칸 — 파견·이동·휴직·프리셉티 기간과 확정 원티드(사본 값에 더한다). 확정 원티드 행을 돌려준다."""
    rows = _applied_fixed_wanted(db, ctx)
    _freeze_fixed_wanted(ctx, rows)
    _load_blocked(db, ctx)
    return rows


def _load_current(db: Session, ctx: EditContext) -> None:
    """지금 테이블 값으로 모은다 — 생성 직후(사본 만들 때) 또는 사본 없는 옛 근무표."""
    _load_config(db, ctx)
    shifts = _load_shift_defs(db, ctx)
    _load_cells(db, ctx, shifts)
    nurses = _load_nurses(db, ctx)
    _load_coverage(db, ctx)
    _load_people_rules(db, ctx, nurses)
    _load_cell_rules(db, ctx, nurses, shifts)
    _load_sequence_settings(db, ctx)   # 꼬리 길이가 N 간격에 달려 있어 꼬리보다 먼저
    _load_tail(db, ctx, shifts)
    _load_off_bounds(ctx)


# ── 설정 ────────────────────────────────────────────────────────────────────


def _load_config(db: Session, ctx: EditContext) -> None:
    from agents_v2.tools.constraint_tools import get_roster_config, get_roster_config_by_id
    raw = get_roster_config_by_id(db, int(ctx.config_id)) if ctx.config_id else None
    if raw is None:
        raw = get_roster_config(db, ctx.group_id)
        ctx.notes.append("근무표에 연결된 설정이 없어 병동 최신 설정으로 판정했습니다.")
    raw = dict(raw or {})
    raw.update(_extra_config(db, raw.get("config_id")))
    ctx.config = _effective_config(raw)
    ctx.use_mid = bool(ctx.config.get("use_mid"))


#: 수정 엔진이 읽는데 `constraint_tools._config_dict`(agents_v2 공용 · 조회 결과로 LLM 에도 나간다)에 없는 설정 칸 —
#: 그 사전은 넓히지 않고 같은 설정 행에서 여기서만 더한다(31장 결함 2 · 없으면 기본값 꺼짐·3·꺼짐으로 판정했다 ·
#: off_first 는 사용자 결정 11-05 — 켜진 병동은 남는 인원을 OFF 로·인원 상한 = 필요 인원 · 생성 엔진과 같다)
EXTRA_CONFIG = ("ban_night_before_fixed_wanted_off", "max_conseq_off", "off_first")


def _extra_config(db: Session, config_id) -> Dict[str, Any]:
    """그 설정 행의 EXTRA_CONFIG 값(비어 있는 칸은 뺀다 — 기본값은 `_effective_config` 가 채운다). 행이 없으면 빈 사전."""
    from db.models import RosterConfig
    if not config_id:
        return {}
    row = db.query(*(getattr(RosterConfig, k) for k in EXTRA_CONFIG)).filter(
        RosterConfig.config_id == int(config_id)).first()
    if row is None:
        return {}
    out = {k: v for k, v in zip(EXTRA_CONFIG, row) if v is not None}
    for key in ("ban_night_before_fixed_wanted_off", "off_first"):
        if key in out:
            out[key] = bool(out[key])
    return out


def apply_extra_config(db: Session, ctx: EditContext) -> None:
    """판정 기준 사본으로 판정할 때도 — 사본은 이 칸이 빠진 사전에 기본값을 채워 남겼다(그 근무표가 쓴 설정 행 값으로 덮는다)."""
    ctx.config.update(_extra_config(db, ctx.config_id))


def _effective_config(raw: Dict[str, Any]) -> Dict[str, Any]:
    """DB 값이 비면 엔진(`create_config_from_db`) 기본값으로 채운다."""
    out = dict(raw)
    defaults = {
        "max_conseq_work": 5, "max_nig_per_month": 15, "three_seq_nig": True,
        "not_one_night": False, "banned_day_after_eve": True,
        "two_offs_after_two_nig": False, "two_offs_after_three_nig": False,
        "weekend_off_only_enable": True, "off_first": False,
        "ban_night_before_fixed_off": True, "ban_night_before_fixed_wanted_off": False,
        "preceptee_shift_count": True, "fixed_wanted_use_yn": True,
    }
    for key, value in defaults.items():
        if out.get(key) is None:
            out[key] = value
    if int(out.get("max_nig_per_month") or 0) <= 0:
        out["max_nig_per_month"] = 15
    return out


# ── 근무 정의·칸 ────────────────────────────────────────────────────────────


def _load_shift_defs(db: Session, ctx: EditContext) -> List[dict]:
    rows = db.query(Shift).filter(Shift.group_id == ctx.group_id).order_by(Shift.sequence).all()
    shifts = [{
        "id": r.id, "shift_id": r.shift_id, "default_shift": r.default_shift,
        "shift_gb": r.shift_gb, "type": r.type, "is_weekly_off": bool(r.is_weekly_off),
    } for r in rows]
    _id_to_main, main_to_id = build_shift_normalizer(shifts)
    ctx.rep_code = {m: main_to_id.get(m, m) for m in EDIT_MAINS}
    ctx.leave_codes = {}
    for r in rows:       # 같은 코드가 여럿이면 순서상 앞 행(화면 규약)
        if str(r.type or "") in VACATION_TYPES:
            ctx.leave_codes.setdefault(str(r.shift_id), str(r.name or r.shift_id))
    return shifts


def _main_of(row: Optional[dict], code: str, id_to_main: Dict[str, str]) -> Optional[str]:
    """칸 하나의 대표코드. 주·휴가·공가·휴무는 O([RVERIFY-03])."""
    if row is not None:
        if row.get("is_weekly_off") or str(row.get("default_shift") or "") == "주":
            return "O"
        mapped = id_to_main.get(str(row["shift_id"]).upper())
        if mapped:
            return "O" if mapped == "주" else mapped
        if str(row.get("type") or "") in VACATION_TYPES | {"휴무"}:
            return "O"
    upper = str(code or "").strip().upper()
    if upper in ("O", "OFF", "주"):
        return "O"
    return id_to_main.get(upper)


def group_codes(shifts: List[dict]) -> Dict[str, str]:
    """대표코드가 없는 근무 코드 중 같은 shift_gb 묶음에 대표 근무(D·E·N·M)가 있는 것 → 그 근무(예: 7B NK → N).
    화면 검증(`screen_rules._group_main_letters`)과 같은 규칙이라 하루 인원·규칙 셈이 화면과 같다(31장 결함 3). 생성 엔진의
    정규화(`build_shift_normalizer`)는 이런 코드를 '대표코드 없는 근무'(W)로 둔다 — 그쪽은 건드리지 않고 여기서만 더한다."""
    from services.schedule_edit.screen_rules import ShiftDef, _group_main_letters
    id_to_main, _ = build_shift_normalizer(shifts)
    defs = [ShiftDef(id=s.get("id"), shift_id=str(s["shift_id"]), default_shift=s.get("default_shift") or None,
                     shift_gb=s.get("shift_gb") or None, type=s.get("type")) for s in shifts]
    out = {}
    for code, letter in _group_main_letters(defs, include_mid=True).items():
        upper = str(code).upper()
        if letter in ("D", "E", "N", "M") and id_to_main.get(upper, "W") == "W":
            out[upper] = letter
    return out


#: 나이트 묶음이 아닌 묶음 근무코드(D·E·M 묶음) 칸의 고정 이유 — 그런 코드는 아직 본 병동이 없어 이전처럼 바꾸지 않는다
#: (사용자 결정 11-05 · 나이트 묶음은 `_note_group_codes`)
GROUP_CODE_REASON = "묶음 근무코드"


def _shift_lookup(shifts: List[dict]) -> Tuple[Dict[str, str], Dict[int, dict], Dict[str, dict]]:
    """(코드→대표코드, 숫자 id→행, 코드→행). 같은 코드가 여럿이면 순서상 앞 행(화면 규약)."""
    id_to_main, _ = build_shift_normalizer(shifts)
    id_to_main = {**id_to_main, **group_codes(shifts)}
    by_num = {int(s["id"]): s for s in shifts if s.get("id") is not None}
    by_code: Dict[str, dict] = {}
    for s in shifts:
        by_code.setdefault(str(s["shift_id"]).upper(), s)
    return id_to_main, by_num, by_code


def _load_cells(db: Session, ctx: EditContext, shifts: List[dict]) -> None:
    id_to_main, by_num, by_code = _shift_lookup(shifts)
    entries =db.query(ScheduleEntry).filter(ScheduleEntry.schedule_id == ctx.schedule_id).all()
    for e in entries:
        nid = str(e.nurse_id)
        if nid not in ctx.raw:
            _blank_row(ctx, nid)
        day = int(e.work_date.day) - 1
        if not 0 <= day < ctx.days:
            continue
        row = _resolve_row(e, by_num, by_code)
        ctx.raw[nid][day] = e.shift_id
        ctx.raw_ids[nid][day] = e.id
        ctx.main[nid][day] = _main_of(row, e.shift_id, id_to_main)
        _classify_cell(ctx, nid, day, row)
    ctx.nurse_ids = sorted(ctx.raw)
    _freeze_missing(ctx)
    _note_group_codes(ctx, group_codes(shifts))


def _note_group_codes(ctx: EditContext, group: Dict[str, str]) -> None:
    """묶음 근무코드 칸(사용자 결정 11-05) — NK 는 N 의 하위코드 · 한 사람은 하나만 쓴다(7B 윤지원은 나이트를 늘 NK 로).
    계산은 그냥 N 이다 — 칸은 다른 N 칸처럼 바꿀 수 있고, 근무 제한은 원래 간호사 설정(허용 근무형 등)만 따른다(7B 윤지원은
    그 설정으로 D·E·M 이 막혀 있다). 저장만 후처리한다 — 그 사람이 쓰는 나이트 코드가 하위코드면 그 사람의 N 은 그 코드로
    (keep_code), 다른 사람은 표준 N 으로. 한 사람의 나이트 칸에 N 과 하위코드가 섞여 있으면 데이터 이상이라 표준 N 으로 두고
    로그만 남긴다. 나이트가 아닌 묶음 코드는 바꾸지 않는 칸으로 둔다(GROUP_CODE_REASON)."""
    if not group:
        return
    for nid in ctx.nurse_ids:
        night = {str(raw).upper(): str(raw) for d, raw in enumerate(ctx.raw[nid]) if raw and ctx.main[nid][d] == "N"}
        sub = [code for code in night if group.get(code) == "N"]
        if sub and len(night) > 1:
            logger.warning("[schedule_edit] 한 사람의 나이트 코드가 섞여 있음(표준 N 으로 저장): %s %s %s",
                           ctx.schedule_id, nid, sorted(night))
        elif sub:
            ctx.keep_code[nid] = {"N": night[sub[0]]}
        for day, raw in enumerate(ctx.raw[nid]):
            if raw and group.get(str(raw).upper()) not in (None, "N"):
                ctx.frozen.setdefault((nid, day), GROUP_CODE_REASON)


def _blank_row(ctx: EditContext, nurse_id: str) -> None:
    ctx.raw[nurse_id] = [None] * ctx.days
    ctx.raw_ids[nurse_id] = [None] * ctx.days
    ctx.main[nurse_id] = [None] * ctx.days


def _freeze_missing(ctx: EditContext) -> None:
    """칸이 없는 날(입사·퇴사 달·일부만 입력된 근무표)은 비워 둔다 — 채우면 없던 근무가 생긴다."""
    for nid in ctx.nurse_ids:
        for day, code in enumerate(ctx.raw[nid]):
            if code is None:
                ctx.frozen.setdefault((nid, day), "근무표에 칸 없음")


def _resolve_row(entry, by_num: Dict[int, dict], by_code: Dict[str, dict]) -> Optional[dict]:
    """화면과 같이 숫자 id 를 먼저 보고, 없으면 코드로 찾는다."""
    if entry.id is not None and int(entry.id) in by_num:
        row = by_num[int(entry.id)]
        if str(row["shift_id"]).upper() == str(entry.shift_id or "").upper():
            return row
    return by_code.get(str(entry.shift_id or "").upper())


def _classify_cell(ctx: EditContext, nurse_id: str, day: int, row: Optional[dict]) -> None:
    """바꿀 수 없는 칸을 표시한다 — 모델이 표현 못 하는 코드는 상수로 남긴다."""
    main = ctx.main[nurse_id][day]
    rtype = str((row or {}).get("type") or "")
    if rtype in VACATION_TYPES:
        ctx.vacation.add((nurse_id, day))
        ctx.frozen[(nurse_id, day)] = "휴가·공가"
    elif main is None:
        ctx.frozen[(nurse_id, day)] = "근무코드 해석 불가"
    elif main == "W":
        ctx.frozen[(nurse_id, day)] = "대표코드 없는 근무"
    elif main == "O" and not _is_plain_off(ctx, row, ctx.raw[nurse_id][day]):
        ctx.frozen[(nurse_id, day)] = "주휴·특수 휴무"
    elif main == "M" and not ctx.use_mid:
        ctx.frozen[(nurse_id, day)] = "MID 미사용 병동의 M"


def _is_plain_off(ctx: EditContext, row: Optional[dict], code: Optional[str]) -> bool:
    """대표 OFF 코드인가. 주휴·산모OFF 같은 특수 휴무는 일부러 넣은 값이라 건드리지 않는다."""
    if row is not None and (row.get("is_weekly_off") or row.get("default_shift") == "주"):
        return False
    upper = str(code or "").upper()
    return upper in ("O", "OFF") or upper == str(ctx.rep_code.get("O", "O")).upper()


# ── 간호사 속성 ─────────────────────────────────────────────────────────────


def _load_nurses(db: Session, ctx: EditContext) -> List[Nurse]:
    from services.nurse_period_resolver import fetch_periods, resolve_asof
    nurses = db.query(Nurse).filter(Nurse.nurse_id.in_(ctx.nurse_ids)).all() if ctx.nurse_ids else []
    ctx.names = {str(n.nurse_id): n.name for n in nurses}
    start = date(ctx.year, ctx.month, 1)
    grade_p = fetch_periods(db, NurseGradePeriod, ctx.nurse_ids, start, start + timedelta(days=1),
                            group_id=ctx.group_id)
    allow_p = fetch_periods(db, NurseAllowedShiftPeriod, ctx.nurse_ids, start, start + timedelta(days=1))
    sentinel = object()
    for n in nurses:
        nid = str(n.nurse_id)
        grade = resolve_asof(grade_p.get(nid), start, "grade", default=sentinel)
        ctx.grade_of[nid] = n.grade if grade is sentinel else grade
        fixed = resolve_asof(allow_p.get(nid), start, "fixed_shift", default=sentinel)
        allowed = resolve_asof(allow_p.get(nid), start, "allowed_shifts", default=sentinel)
        _note_profile(ctx, nid, n.fixed_shift if fixed is sentinel else fixed,
                      n.allowed_shifts if allowed is sentinel else allowed)
    return nurses


def _note_profile(ctx: EditContext, nurse_id: str, fixed_shift, allowed_raw) -> None:
    """고정근무자는 엔진이 아예 빼고 따로 채우므로 한 달 전부 상수로 둔다(RCS `_split_fixed_nurses`)."""
    if fixed_shift:
        for day in range(ctx.days):
            ctx.frozen.setdefault((nurse_id, day), "고정근무자")
    allowed = normalize_allowed_shift_codes(allowed_raw, use_mid=ctx.use_mid)
    if is_n_only_profile(allowed_raw, use_mid=ctx.use_mid):
        ctx.n_only.add(nurse_id)
    if len(allowed) == 1:
        ctx.single_shift[nurse_id] = next(iter(allowed))


def _load_people_rules(db: Session, ctx: EditContext, nurses: List[Nurse]) -> None:
    _load_grade(db, ctx)
    _load_teams(db, ctx)
    _load_monthly_limits(db, ctx)
    _load_weekend_off(db, ctx)
    _load_mutex(db, ctx)


def _load_grade(db: Session, ctx: EditContext) -> None:
    from services.roster_create_service import _fetch_grade_config_dict
    config = _fetch_grade_config_dict(db, ctx.office_id, ctx.group_id)
    raw = config.get("constraints_json") or {}
    ctx.grade_min = json.loads(raw) if isinstance(raw, str) else dict(raw)


def _load_teams(db: Session, ctx: EditContext) -> None:
    """팀은 그 달 1일 기준, 팀 최소는 `teams.min_shift`(엔진 `build_team_min_by_team` 과 같은 함수)."""
    from services.roster_create_service import build_team_min_by_team
    from services.team_period import resolve_teams_for_month
    team_map = resolve_teams_for_month(db, ctx.group_id, date(ctx.year, ctx.month, 1))
    ctx.team_of = {nid: team_map.get(nid) for nid in ctx.nurse_ids}
    if ctx.config.get("team_min_soft_fallback"):
        return
    rows = db.query(Team).filter(Team.group_id == ctx.group_id, Team.active == 1).all()
    members = {str(t) for t in ctx.team_of.values() if t is not None}
    by_team = build_team_min_by_team(rows, members, ctx.use_mid) or {}
    ctx.team_min = {int(k): {str(c): int(v) for c, v in (mins or {}).items()}
                    for k, mins in by_team.items()}


def _load_monthly_limits(db: Session, ctx: EditContext) -> None:
    from services.constraints.monthly_limit_constraints import _norm_bounds
    from services.nurse_monthly_limit_service import fetch_effective_monthly_limits_by_nurse
    ctx.monthly_limits = fetch_effective_monthly_limits_by_nurse(
        db, ctx.year, ctx.month, ctx.nurse_ids, ctx.group_id) or {}
    for nid, limit in ctx.monthly_limits.items():
        _lo, hi = _norm_bounds(limit.get("n_min"), limit.get("n_max"), limit.get("n_exact"))
        if hi == 1:
            ctx.single_n_ok.add(str(nid))


def _load_weekend_off(db: Session, ctx: EditContext) -> None:
    from services.nurse_period_resolver import weekend_off_ids_asof
    if not ctx.config.get("weekend_off_only_enable", True):
        return
    ctx.weekend_off = weekend_off_ids_asof(db, ctx.nurse_ids, ctx.year, ctx.month) or set()
    ctx.weekend_days = {d for d in range(ctx.days)
                        if date(ctx.year, ctx.month, d + 1).weekday() >= 5}


def _load_mutex(db: Session, ctx: EditContext) -> None:
    from services.mutual_exclusion_period import resolve_mutual_exclusion_days_for_month
    ctx.mutex = resolve_mutual_exclusion_days_for_month(
        db, ctx.nurse_ids, ctx.year, ctx.month) or {}


# ── 인원 ────────────────────────────────────────────────────────────────────


def _load_coverage(db: Session, ctx: EditContext) -> None:
    """일별 최소·최대 — 엔진 `_build_shift_manage_and_requirements` 그대로."""
    from services.roster_create_service import _build_shift_manage_and_requirements
    user = SimpleNamespace(office_id=ctx.office_id, group_id=ctx.group_id)
    req = SimpleNamespace(year=ctx.year, month=ctx.month)
    cfg = SimpleNamespace(use_mid=ctx.use_mid)
    _sm, _base, by_day, by_day_max = _build_shift_manage_and_requirements(
        db, user, cfg, req, require_nonzero_requirements=False)
    ctx.cov_min = [dict(x) for x in by_day]
    ctx.cov_max = [dict(x) for x in by_day_max]


# ── 칸 규칙 ─────────────────────────────────────────────────────────────────


def _load_cell_rules(db: Session, ctx: EditContext, nurses: List[Nurse], shifts: List[dict]) -> None:
    _load_allowed(db, ctx, nurses)
    _load_banned(db, ctx, nurses)
    fixed_rows = _applied_fixed_wanted(db, ctx)
    _freeze_fixed_wanted(ctx, fixed_rows)
    _load_blocked(db, ctx)
    _load_ban_n_before(ctx)
    _load_wishes(db, ctx, shifts, fixed_rows)


def _add_forbidden(ctx: EditContext, forbidden: Dict[str, Dict[int, List[str]]]) -> None:
    for nid, by_day in (forbidden or {}).items():
        for day, codes in (by_day or {}).items():
            ctx.forbidden.setdefault((str(nid), int(day)), set()).update(
                str(c).upper() for c in (codes or []))


def _load_allowed(db: Session, ctx: EditContext, nurses: List[Nurse]) -> None:
    from services.roster_create_service import build_allowed_shift_type_constraints
    out = build_allowed_shift_type_constraints(
        nurses, ctx.year, ctx.month, None, None, ctx.use_mid, db=db)
    _add_forbidden(ctx, (out or {}).get("forbidden") or {})


def _load_banned(db: Session, ctx: EditContext, nurses: List[Nurse]) -> None:
    """기피 근무 — 수간호사가 확정한(source='hn') 것만. 엔진과 같은 함수(조회 실패는 그 안에서 빈 값)."""
    from services.roster_create_service import _build_banned_wanted_constraints
    user = SimpleNamespace(office_id=ctx.office_id, group_id=ctx.group_id)
    req = SimpleNamespace(year=ctx.year, month=ctx.month)
    out = _build_banned_wanted_constraints(db, user, req, nurses)
    _add_forbidden(ctx, (out or {}).get("forbidden") or {})


def _applied_fixed_wanted(db: Session, ctx: EditContext) -> List[FixedWantedEntry]:
    return db.query(FixedWantedEntry).filter(
        FixedWantedEntry.group_id == ctx.group_id,
        FixedWantedEntry.year == ctx.year,
        FixedWantedEntry.month == ctx.month,
        FixedWantedEntry.is_applied == True,  # noqa: E712 — MSSQL 은 IS 1 을 거부한다
    ).all()


def _freeze_fixed_wanted(ctx: EditContext, rows: List[FixedWantedEntry]) -> None:
    """확정 원티드 칸은 그대로 둔다(엔진 고정칸). 설정이 꺼져 있으면 엔진도 고정하지 않는다."""
    if not ctx.config.get("fixed_wanted_use_yn", True):
        return
    for r in rows:
        nid, day = str(r.nurse_id), int(r.shift_date.day) - 1
        if nid in ctx.raw and 0 <= day < ctx.days:
            ctx.frozen[(nid, day)] = FIXED_WANTED


def released_wanted(ctx: EditContext, cells) -> Set[Cell]:
    """그 칸들 중 확정 원티드로 묶인 칸 — 수간호사가 그 칸을 직접 짚었을 때만 푼다(사용자 지시 11-03)."""
    return {c for c in cells if ctx.frozen.get(c) == FIXED_WANTED}


def with_released(ctx: EditContext, cells) -> EditContext:
    """직접 짚은 원티드 칸을 바꿀 수 있게 한 재료(사본) — 확정 원티드는 고정을 풀고 그 칸 때문에 걸린 '전날 N 금지'도
    다시 계산한다. 지금 반영된 제출 원티드는 모델·검증이 짚은 칸에서 면제하니 안내만 붙인다.

    짚지 않은 원티드 칸은 그대로 묶여 있다(개수·인원 요청·연쇄 변경·레버로는 못 바꾼다). 바꿀 원티드 칸이 없으면 그대로.
    """
    fixed = released_wanted(ctx, cells)
    wished = {(n, d) for n, d in cells if ctx.main[n][d] in ctx.wishes.get((n, d), ())} - fixed
    if not fixed and not wished:
        return ctx
    out = dataclasses.replace(ctx, notes=list(ctx.notes))
    if fixed:
        out.frozen = {c: r for c, r in ctx.frozen.items() if c not in fixed}
        out.ban_n_before = set()
        _load_ban_n_before(out)
    out.notes += [f"{ctx.names.get(n, n)} {d + 1}일은 {'확정 원티드' if (n, d) in fixed else '원티드가 반영된'} 칸이지만 "
                  "직접 요청하셔서 바꿉니다." for n, d in sorted(fixed | wished, key=lambda c: (c[1], c[0]))]
    return out


def with_wish_cells(ctx: EditContext, cells) -> EditContext:
    """원티드 반영 요청이 짚은 '반영 안 된 확정 원티드' 칸을 바꿀 수 있게 한 재료(사본) — 원티드대로만 바꾼다(`model.add_wish`).
    전날 N 금지는 그대로 둔다(원티드 OFF 로 되돌리는 칸이다). 그런 칸이 없으면 그대로."""
    cells = {c for c in cells if c in ctx.wish_fixed and ctx.frozen.get(c) == FIXED_WANTED}
    if not cells:
        return ctx
    out = dataclasses.replace(ctx, notes=list(ctx.notes))
    out.frozen = {c: r for c, r in ctx.frozen.items() if c not in cells}
    out.notes.append(f"확정 원티드와 근무표가 다른 {len(cells)}칸은 원티드대로 되돌릴 수 있습니다(다른 근무로는 바꾸지 않습니다).")
    return out


def _load_wishes(db: Session, ctx: EditContext, shifts: List[dict], fixed_rows: List[FixedWantedEntry]) -> None:
    """간호사 원티드(희망) — 엔진 선호와 같은 출처.

    확정 원티드가 있으면 그것(RCS:200), 없으면 간호사별 마지막 제출 원티드(RCS:293). 확정 원티드가
    고정칸으로 묶인 병동이면 그 칸은 이미 못 바꾸므로 여기선 고정되지 않은 칸만 남는다.
    """
    if fixed_rows:
        id_to_main, _by_num, by_code = _shift_lookup(shifts)
        pairs = [(str(r.nurse_id), int(r.shift_date.day) - 1,
                  _main_of(by_code.get(str(r.shift_id or "").upper()), r.shift_id, id_to_main))
                 for r in fixed_rows]
    else:
        pairs = _submitted_wishes(db, ctx)
    for nid, day, main in pairs:
        if nid not in ctx.raw or not 0 <= day < ctx.days:
            continue
        if main not in EDIT_MAINS:
            ctx.wish_skipped.append((nid, day, "code", str(main)))
        elif _unreflected_fixed(ctx, nid, day, main):
            ctx.wishes.setdefault((nid, day), set()).add(main)
            ctx.wish_fixed.add((nid, day))
        elif ctx.frozen.get((nid, day)) == FIXED_WANTED and ctx.main[nid][day] == main:
            continue                     # 이미 반영된 확정 원티드 — 넣을 것도 못 넣는 것도 아니다
        elif not ctx.is_free(nid, day):
            ctx.wish_skipped.append((nid, day, "locked", str(main)))
        else:
            ctx.wishes.setdefault((nid, day), set()).add(main)


def _unreflected_fixed(ctx: EditContext, nid: str, day: int, main: Optional[str]) -> bool:
    """확정 원티드로만 묶였는데 칸이 그 원티드와 다르다(예: 7B 허건 11일 신청 OFF 인데 그룹웨어 판 D) — 원티드 반영 요청이
    원티드대로 되돌릴 수 있는 칸. 칸 자체가 바꿀 수 없는 코드(휴가 등)면 아니다."""
    return (ctx.frozen.get((nid, day)) == FIXED_WANTED and main in EDIT_MAINS and ctx.main[nid][day] != main
            and ctx.main[nid][day] in EDIT_MAINS and (nid, day) not in ctx.vacation)


def note_unreflected_fixed(db: Session, ctx: EditContext, shifts: List[dict], rows: List[FixedWantedEntry]) -> None:
    """판정 기준 사본으로 판정할 때 — 사본의 원티드에는 묶인 칸이 없어 반영 안 된 확정 원티드만 지금 행으로 더한다."""
    if not rows:
        return
    id_to_main, _by_num, by_code = _shift_lookup(shifts)
    for r in rows:
        nid, day = str(r.nurse_id), int(r.shift_date.day) - 1
        main = _main_of(by_code.get(str(r.shift_id or "").upper()), r.shift_id, id_to_main)
        if nid in ctx.raw and 0 <= day < ctx.days and _unreflected_fixed(ctx, nid, day, main):
            ctx.wishes.setdefault((nid, day), set()).add(main)
            ctx.wish_fixed.add((nid, day))


def _submitted_wishes(db: Session, ctx: EditContext) -> List[Tuple[str, int, str]]:
    """간호사별 마지막 제출 원티드의 희망 칸(점수 > 0). 미제출 초안은 엔진도 안 쓴다."""
    latest: Dict[str, Tuple[Any, int]] = {}
    for nid, rid, at in db.query(WantedRequest.nurse_id, WantedRequest.request_id, WantedRequest.submitted_at).filter(
        WantedRequest.nurse_id.in_(ctx.nurse_ids),
        WantedRequest.month == f"{ctx.year}-{ctx.month:02d}",
        WantedRequest.is_submitted == True,  # noqa: E712
    ).all():
        key = (at or datetime.min, int(rid))
        if str(nid) not in latest or key > latest[str(nid)]:
            latest[str(nid)] = key
    if not latest:
        return []
    first = date(ctx.year, ctx.month, 1)
    rows = db.query(NurseShiftRequest.nurse_id, NurseShiftRequest.request_id, NurseShiftRequest.shift_date,
                    NurseShiftRequest.shift, NurseShiftRequest.score).filter(
        NurseShiftRequest.nurse_id.in_(list(latest)),
        NurseShiftRequest.request_id.in_({rid for _at, rid in latest.values()}),
        NurseShiftRequest.shift_date >= first,
        NurseShiftRequest.shift_date < first + timedelta(days=ctx.days),
    ).all()
    return [(str(nid), sd.day - 1, str(code or "").strip().upper())
            for nid, rid, sd, code, score in rows
            if latest[str(nid)][1] == int(rid) and float(score or 0) > 0]


def _load_blocked(db: Session, ctx: EditContext) -> None:
    """파견·이동·휴직·프리셉티 기간 — 엔진은 그 칸 변수를 아예 안 만든다.

    프리셉티는 프리셉터를 그대로 따라가므로(엔진 `X(n)==X(p)` 하드) 그날 프리셉터 칸도 묶는다.
    ★ 막힌 날짜 함수(대체 추천용)는 확정 원티드 날짜도 돌려준다 — 빼고 받는다(31장 결함 1: '관할 밖'으로 덮어 원티드 OFF 전날
      N 금지가 빠지고 직접 짚어 풀기도 안 됐다). 파견 날과 겹치는 원티드 날은 파견이 우선이라 관할 밖이다(사용자 결정 11-05).
    """
    from services.preceptee_period import resolve_preceptee_days_for_month
    from services.replacement_recommend_service import _build_assignment_blocked_dates
    raw = _build_assignment_blocked_dates(db, ctx.group_id, ctx.year, ctx.month, ctx.days,
                                          include_fixed_wanted=False) or {}
    for nid, days in raw.items():
        for value in days or ():
            day = (value.day if hasattr(value, "day") else int(value)) - 1
            if str(nid) in ctx.raw and 0 <= day < ctx.days:
                ctx.frozen[(str(nid), day)] = "관할 밖(파견·이동·휴직·프리셉티)"
    periods = resolve_preceptee_days_for_month(db, ctx.nurse_ids, ctx.year, ctx.month) or {}
    for nid, info in periods.items():
        _freeze_preceptee(ctx, str(nid), info)


def _freeze_preceptee(ctx: EditContext, nurse_id: str, info: Dict[str, Any]) -> None:
    preceptor = str(info.get("preceptor_id") or "")
    count_in_cov = bool(ctx.config.get("preceptee_shift_count", True))
    for day in info.get("days") or ():
        ctx.frozen[(nurse_id, int(day))] = "프리셉티 동행"
        if not count_in_cov:
            ctx.cov_exclude.add((nurse_id, int(day)))
        if preceptor in ctx.raw:
            ctx.frozen.setdefault((preceptor, int(day)), "프리셉티 동행(프리셉터)")


def with_vacation(ctx: EditContext, cells) -> EditContext:
    """그 칸들을 휴가로 넣은 재료(사본) — 순수 OFF·고립 OFF 판정과 '휴가 전날 N 금지'(설정 켜짐)가 그 칸까지 본다.

    근무표를 열 때 한 번 계산하는 `ban_n_before` 도 같이 넓힌다(검토 M1: vacation 만 넓히면 새 휴가 칸 전날 N 을 놓친다).
    """
    cells = set(cells)
    out = dataclasses.replace(ctx, vacation=set(ctx.vacation) | cells, ban_n_before=set(ctx.ban_n_before))
    if ctx.config.get("ban_night_before_fixed_off"):
        out.ban_n_before |= {(n, d - 1) for n, d in cells if d >= 1}
    return out


def _load_ban_n_before(ctx: EditContext) -> None:
    """휴가·공가(설정 켜짐) / 확정 원티드 OFF(설정 켜짐) 전날 N 금지 — 엔진 FL:2230 축 둘."""
    by_type = bool(ctx.config.get("ban_night_before_fixed_off"))
    by_wanted = bool(ctx.config.get("ban_night_before_fixed_wanted_off"))
    for (nid, day), reason in ctx.frozen.items():
        hit_type = by_type and (nid, day) in ctx.vacation
        hit_wanted = by_wanted and reason == FIXED_WANTED and ctx.main[nid][day] == "O"
        if (hit_type or hit_wanted) and day >= 1:
            ctx.ban_n_before.add((nid, day - 1))


# ── 시퀀스 규칙 설정·전월 꼬리 ───────────────────────────────────────────────


def _load_sequence_settings(db: Session, ctx: EditContext) -> None:
    """설정 컬럼이 아닌 엔진 하드 — 기본값을 그대로 쓴다(재시도 자동 완화는 근무표에 안 남는다)."""
    from services.cp_sat.fallback_lex import effective_n2n_min_gap
    from services.roster_create_service import _n2n_min_gap_setting
    ctx.n2n_gap = effective_n2n_min_gap(_n2n_min_gap_setting(db, ctx.group_id))
    ctx.same_shift_k = int(ctx.config.get("same_shift_hard_k", 3) or 0)
    ctx.isolated_work_hard = bool(ctx.config.get("isolated_work_hard", True))


def _load_tail(db: Session, ctx: EditContext, shifts: List[dict]) -> None:
    """전월 마지막 며칠 — 엔진과 같은 근무표(마감본 우선, 없으면 최신 초안)."""
    from services.roster_create_service import _query_prev_month_schedule_id
    prev_id = _query_prev_month_schedule_id(db, ctx.group_id, ctx.year, ctx.month)
    if not prev_id:
        ctx.notes.append("전월 근무표가 없어 월초 규칙을 이번 달 안에서만 봤습니다.")
        return
    first = date(ctx.year, ctx.month, 1)
    span = max(TAIL_DAYS, ctx.n2n_gap)
    rows = db.query(ScheduleEntry.nurse_id, ScheduleEntry.work_date, ScheduleEntry.shift_id,
                    ScheduleEntry.id).filter(
        ScheduleEntry.schedule_id == prev_id,
        ScheduleEntry.nurse_id.in_(ctx.nurse_ids),
        ScheduleEntry.work_date >= first - timedelta(days=span),
        ScheduleEntry.work_date < first,
    ).all()
    id_to_main, by_num, by_code = _shift_lookup(shifts)
    seqs: Dict[str, List[str]] = defaultdict(lambda: ["-"] * span)
    for nid, work_date, code, num in rows:
        offset = (first - _as_date(work_date)).days  # 1 = 전월 말일
        if 1 <= offset <= span:
            row = _resolve_row(SimpleNamespace(id=num, shift_id=code), by_num, by_code)
            seqs[str(nid)][span - offset] = _main_of(row, code, id_to_main) or "-"
    ctx.tail = dict(seqs)


def _as_date(value) -> date:
    return value.date() if hasattr(value, "date") else value


# ── 개인 OFF 수 ─────────────────────────────────────────────────────────────


def _load_off_bounds(ctx: EditContext) -> None:
    """순수 OFF 수의 [하한, 상한] — 엔진 off_policy 기준을 근사한다.

    하한 = off_days, 상한 = off_first 면 하한+여유, 아니면 하한(+1 은 엔진이 벌점으로만 허용).
    월 일부만 근무하는 사람은 엔진이 비례로 줄이지만 여기선 줄이지 않는다 — 판정이
    min(하한, 지금 값) 이라 지금 값이 기준선이 되어 더 엄격한 쪽으로만 틀린다.
    """
    lo, _src = resolve_effective_off_days(ctx.config)
    extra = 0
    if ctx.config.get("off_first"):
        two_n = ctx.config.get("two_offs_after_two_nig") or ctx.config.get("two_offs_after_three_nig")
        extra = int(ctx.config.get("max_extra_off_days") or (6 if two_n else 1))
    for nid in ctx.nurse_ids:
        ctx.off_bounds[nid] = (lo, lo + extra)


def pure_off_count(ctx: EditContext, nurse_id: str, mains: List[Optional[str]]) -> int:
    """순수 OFF 수 — O 이면서 휴가·공가가 아닌 칸."""
    return sum(1 for d, m in enumerate(mains) if m == "O" and (nurse_id, d) not in ctx.vacation)
