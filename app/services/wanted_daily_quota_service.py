"""원티드 일자별 신청 한도(병동 전체 + 팀별) — 셈·제출 검사·작성 화면 조회·팀 한도 설정.

- 세는 대상 = 휴가·휴무 계열 '희망' 원티드. 화면(`Roster_wanted.tsx` targetShiftIds)과 같은 기준:
  shifts.type 이 OFF·휴가·휴무·연차 이거나 코드 'O'. 근무 희망(D·E·N)·기피·공가는 세지 않는다.
  간호사 1명 × 날짜 1칸이 1건이다. 응답에 `counted_shift_ids` 로 실어 화면이 따로 셈하지 않게 한다.
- 자리를 차지하는 것 = 이 병동 **활성** 간호사별 그 달 **최신 제출본**. 임시저장은 자리를 잡지 않는다
  (제출 선착순). 철회하면 자리가 돌아간다(그 달 제출본을 모두 내린다 — `retract_submission_service`).
- 팀 = 근무자관리와 같은 규칙(`team_period.quota_team_map` — 1일 기준, 월중 합류자는 그 달 첫 팀,
  비활성 팀은 기타). 팀 미배정(기타)은 팀 한도 대상이 아니다.
- **적용 기준은 날짜마다 하나다(11-04 결정 '팀 우선, 없으면 병동')**: 내 팀 한도(날짜 지정 > 팀 기본값)가
  있으면 팀 기준, 없으면 병동 일자 한도(`wanted_config`), 둘 다 없으면 제한 없음. 팀 한도를 정한 팀원에게는
  그날 병동 일자 한도가 걸리지 않는다(설정 화면 경고 `ward_limit_may_overflow`). 병동 기준의 셈은 병동 전원이다.
- 0 = 그날 그 기준으로 신청 불가(마감), 행 없음 = 제한 없음(다음 순위).
- 한도를 낮춰도 이미 제출된 신청은 그대로 두고 새 제출만 막는다.
- 수간호사·관리자가 관리보드나 AIDE 에서 직접 넣는 원티드는 이 검사를 타지 않는다(한도 예외 — 10-27 결정 ④).
  ★ AIDE 의 **간호사 본인** 원티드 추가·변경·삭제·철회는 예외가 아니다 — 화면 제출과 같은 잠금·마감·한도를
  거친다(`agents_v2/tools/wanted_tools.py` `self_rules`, 11-04 검토).
- 검사하는 제출 경로 = `/preferences/submit` 의 두 갈래(`save_wanted_entries_service` · 구형 data 경로) ·
  AIDE 간호사 본인 쓰기.
"""
import calendar
import logging
import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime

from db.models import (
    Nurse,
    NurseShiftRequest,
    Shift,
    Team,
    Wanted,
    WantedConfig,
    WantedRequest,
    WantedTeamLimit,
)
from services.preferences_service import PreferenceQuotaError, WantedBusyError
from services.team_period import quota_team_map, sort_team_ids_by_name
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

QUOTA_SHIFT_TYPES = ("OFF", "휴가", "휴무", "연차")
#: 응답에 싣는 적용 규칙 이름 — 날짜마다 내 팀 한도가 있으면 팀, 없으면 병동(11-04 결정).
QUOTA_RULE = "team_first"

#: 원티드 행 잠금 대기 상한(ms). 제출 트랜잭션은 짧아 이 안에 풀린다 — 넘으면 409 `WANTED_BUSY`(잠시 후 다시 시도).
_LOCK_TIMEOUT_MS = 10000
#: MSSQL 잠금 대기 시간 초과 오류 번호.
_MSSQL_LOCK_TIMEOUT = 1222

_TEAM_LIMIT_READY = False
#: 표가 없다고 확인한 뒤 다시 확인하지 않을 때까지(time.monotonic 기준).
_TEAM_LIMIT_MISSING_UNTIL = 0.0
_MISSING_RECHECK_SEC = 60


class TeamLimitUnavailableError(Exception):
    """wanted_team_limit 표가 없어 팀 한도를 저장할 수 없다(DDL 미적용 · 503)."""


@dataclass
class TeamLimits:
    """그 달 팀 한도. defaults = {team_id: 한도}, by_date = {(날짜, team_id): 한도}."""

    defaults: dict = field(default_factory=dict)
    by_date: dict = field(default_factory=dict)

    def any(self) -> bool:
        return bool(self.defaults or self.by_date)

    def limit(self, team_id: int | None, day: date) -> int | None:
        """날짜 지정 > 팀 기본값 > 제한 없음(None)."""
        if team_id is None:
            return None
        if (day, team_id) in self.by_date:
            return self.by_date[(day, team_id)]
        return self.defaults.get(team_id)

    def limited_days(self, team_id: int | None, year: int, month: int) -> set:
        """그 팀에 한도가 걸린 날짜들."""
        if team_id is None:
            return set()
        if team_id in self.defaults:
            return set(_month_days(year, month))
        return {d for d, t in self.by_date if t == team_id}


def _month_days(year: int, month: int) -> list:
    return [date(year, month, d) for d in range(1, calendar.monthrange(year, month)[1] + 1)]


def _month_range(year: int, month: int) -> tuple:
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def _as_date(value) -> date:
    """드라이버에 따라 DATE 가 date·datetime·문자열로 온다 — date 로 맞춘다."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def team_limit_storage_ready(db: Session) -> bool:
    """wanted_team_limit 표가 있는지.

    DDL 미적용 환경에서 제출·조회까지 깨지지 않게 미리 확인한다(실패한 문장은 MSSQL 트랜잭션을
    오염시키므로 사후 try/except 로는 안 된다). True 는 계속 캐시, False 는 60초만 캐시한다 —
    표가 없는 동안 조회마다 카탈로그를 묻지 않으면서도, DDL 적용 뒤 서버 재기동 없이 1분 안에 반영되도록.
    """
    global _TEAM_LIMIT_READY, _TEAM_LIMIT_MISSING_UNTIL
    if _TEAM_LIMIT_READY:
        return True
    if time.monotonic() < _TEAM_LIMIT_MISSING_UNTIL:
        return False
    try:
        _TEAM_LIMIT_READY = sa_inspect(db.get_bind()).has_table("wanted_team_limit")
    except SQLAlchemyError as e:
        print(f"[wanted_quota] 팀 한도 저장소 확인 실패(미사용 처리): {e}")
        return False
    if not _TEAM_LIMIT_READY:
        _TEAM_LIMIT_MISSING_UNTIL = time.monotonic() + _MISSING_RECHECK_SEC
        print("[wanted_quota] wanted_team_limit 표 없음 — 팀 한도 건너뜀(병동 전체 한도만)")
    return _TEAM_LIMIT_READY


def quota_shift_ids(db: Session, group_id: str) -> set:
    """한도에서 세는 근무코드(휴가·휴무 계열)."""
    rows = db.query(Shift.shift_id, Shift.type).filter(Shift.group_id == group_id).all()
    return {sid for sid, stype in rows if (stype or "").strip() in QUOTA_SHIFT_TYPES or sid == "O"}


def ward_limits(db: Session, group_id: str, year: int, month: int) -> dict:
    """병동 전체 일자별 한도 {날짜: 한도} — `shift_type` 이 NULL 인 행만 쓴다.

    NULL = 그날 병동 전체 한도(PC 설정 화면이 늘 NULL 로 저장 · 예전 판정 `validate_wanted_limits` 와 같다).
    shift_type 이 있는 행은 쓰지 않는다 — 고유 인덱스가 (병동, 날짜, shift_type)이라 같은 날 NULL 행과 함께 있을 수 있고,
    섞어 세면 화면에 보이는 한도보다 낮은 값으로 막힌다(11-04 점검). 같은 날 NULL 행이 둘이면(인덱스 없는 DB) 작은 값.
    """
    start, end = _month_range(year, month)
    rows = db.query(WantedConfig.target_date, WantedConfig.max_requests).filter(
        WantedConfig.group_id == group_id,
        WantedConfig.target_date >= start,
        WantedConfig.target_date <= end,
        WantedConfig.shift_type.is_(None),
        WantedConfig.max_requests.isnot(None),
    ).all()
    out: dict = {}
    for target, limit in rows:
        if limit is None or int(limit) < 0:
            continue
        day = _as_date(target)
        out[day] = min(int(limit), out.get(day, int(limit)))
    return out


def load_team_limits(db: Session, group_id: str, year: int, month: int) -> TeamLimits:
    """그 달 팀 한도. 저장소가 없으면 빈 값(팀 한도 없음)."""
    limits = TeamLimits()
    if not team_limit_storage_ready(db):
        return limits
    rows = db.query(
        WantedTeamLimit.team_id, WantedTeamLimit.target_date, WantedTeamLimit.max_requests,
    ).filter(
        WantedTeamLimit.group_id == group_id,
        WantedTeamLimit.year == year,
        WantedTeamLimit.month == month,
    ).all()
    for team_id, target, limit in rows:
        if target is None:
            limits.defaults[int(team_id)] = int(limit)
        else:
            limits.by_date[(_as_date(target), int(team_id))] = int(limit)
    return limits


def _latest_submitted_ids(db: Session, group_id: str, month_str: str) -> dict:
    """{nurse_id: 최신 제출본 request_id} — 이 병동 **활성** 소속·이 병동에 낸 제출본만.

    ★ 활성만 센다 — 팀 판정(`quota_team_map`)과 같은 사람들이다. 비활성(퇴사 등) 간호사의 제출본이
      자리를 잡으면 화면의 팀 인원과 셈이 어긋난다(11-04 검토).
    """
    member_ids = [str(n) for (n,) in db.query(Nurse.nurse_id).filter(
        Nurse.group_id == group_id, Nurse.active == 1,
    ).all()]
    if not member_ids:
        return {}
    latest: dict = {}
    for nurse_id, request_id in db.query(WantedRequest.nurse_id, WantedRequest.request_id).filter(
        WantedRequest.group_id == group_id,
        WantedRequest.nurse_id.in_(member_ids),
        WantedRequest.month == month_str,
        WantedRequest.is_submitted == True,  # noqa: E712
    ).order_by(WantedRequest.nurse_id, WantedRequest.submitted_at.desc()).all():
        latest.setdefault(str(nurse_id), request_id)
    return latest


def submitted_quota_dates(
    db: Session, group_id: str, year: int, month: int, quota_ids: set,
    *, exclude_nurse_id: str | None = None,
) -> dict:
    """간호사별 그 달 최신 제출본의 휴가·휴무 계열 날짜 {nurse_id: {날짜}}."""
    latest = _latest_submitted_ids(db, group_id, f"{year}-{month:02d}")
    if exclude_nurse_id is not None:
        latest.pop(str(exclude_nurse_id), None)
    if not latest or not quota_ids:
        return {}
    start, end = _month_range(year, month)
    out: dict = {}
    for nurse_id, request_id, shift_date in db.query(
        NurseShiftRequest.nurse_id, NurseShiftRequest.request_id, NurseShiftRequest.shift_date,
    ).filter(
        NurseShiftRequest.nurse_id.in_(list(latest)),
        NurseShiftRequest.request_id.in_(set(latest.values())),
        NurseShiftRequest.shift_date >= start,
        NurseShiftRequest.shift_date <= end,
        NurseShiftRequest.shift.in_(quota_ids),
    ).all():
        if latest.get(str(nurse_id)) == request_id:
            out.setdefault(str(nurse_id), set()).add(_as_date(shift_date))
    return out


def _own_quota_dates(
    db: Session, nurse_id: str, request_id: int, year: int, month: int, quota_ids: set,
) -> set:
    """제출하려는 요청(request_id)의 휴가·휴무 계열 날짜."""
    if not quota_ids:
        return set()
    start, end = _month_range(year, month)
    rows = db.query(NurseShiftRequest.shift_date).filter(
        NurseShiftRequest.nurse_id == nurse_id,
        NurseShiftRequest.request_id == request_id,
        NurseShiftRequest.shift_date >= start,
        NurseShiftRequest.shift_date <= end,
        NurseShiftRequest.shift.in_(quota_ids),
    ).all()
    return {_as_date(d) for (d,) in rows}


def _count_by_date(dates_by_nurse: dict, team_map: dict | None = None, team_id=None) -> Counter:
    """날짜별 신청 수. team_id 를 주면 그 팀 사람만 센다."""
    counts: Counter = Counter()
    for nurse_id, days in dates_by_nurse.items():
        if team_id is not None and (team_map or {}).get(nurse_id) != team_id:
            continue
        counts.update(days)
    return counts


def _team_name(db: Session, group_id: str, team_id: int | None) -> str | None:
    if team_id is None:
        return None
    row = db.query(Team.team_name).filter(Team.group_id == group_id, Team.team_id == team_id).first()
    return row[0] if row else None


def lock_wanted_month(db: Session, group_id: str, year: int, month: int, *, source: str = "") -> None:
    """병동·월 원티드 행을 잠근다(커밋까지). 같은 병동·월 제출이 줄을 서서 뒤 사람이 앞 사람 제출을 센다.

    ★ 제출 경로는 **자기 신청 칸을 쓰기 전에** 이걸 먼저 부른다. 칸을 쓴 뒤 잠그면 두 제출이
      서로의 미커밋 칸(행 잠금)과 원티드 행 잠금을 엇갈려 기다려 교착이 난다.
    ★ `with_for_update()` 는 이 환경(mssql+pymssql·SQLAlchemy 2.0)에서 SQL 에 아무것도 붙이지
      않는다(실측) — 테이블 힌트로 건다(`retract_submission_service` 와 같은 방식).
    ★ 대기 상한 `_LOCK_TIMEOUT_MS` — roster-back 에는 쿼리 타임아웃이 없어, 잠금을 쥔 트랜잭션이 멈추면
      제출이 끝없이 기다린다. 넘으면 WantedBusyError(409 `WANTED_BUSY` '잠시 후 다시').
    ★ source 는 대기 초과 로그에만 붙는다 — AIDE 쓰기('AIDE')는 409 가 아니라 도구 문구로 끝나서 화면 경합과 가른다.
    """
    label = f"wanted {group_id} {year}-{int(month):02d}" + (f" {source}" if source else "")
    with _lock_wait_limit(db, _LOCK_TIMEOUT_MS, label=label):
        db.query(Wanted).with_hint(Wanted, "WITH (UPDLOCK, ROWLOCK)", "mssql").filter(
            Wanted.group_id == group_id, Wanted.year == year, Wanted.month == month,
        ).first()


def _is_lock_timeout(exc: OperationalError) -> bool:
    args = getattr(exc.orig, "args", ()) or ()
    return (bool(args) and args[0] == _MSSQL_LOCK_TIMEOUT) or "Lock request time out" in str(exc)


#: 잠금 대기 초과 안내 — 임시저장·제출·철회가 같이 쓴다(그래서 '다시 제출'이 아니라 '다시 시도').
_BUSY_MESSAGE = "같은 병동의 다른 원티드를 처리하는 중이에요. 잠시 후 다시 시도해 주세요."


@contextmanager
def _lock_wait_limit(db: Session, timeout_ms: int, busy_message: str = _BUSY_MESSAGE, *, label: str = ""):
    """이 블록의 잠금 대기를 timeout_ms 로 묶는다(MSSQL 만 — 테스트용 SQLite 는 그대로 통과).

    ★ `SET LOCK_TIMEOUT` 은 **연결(세션) 설정**이다. 되돌리지 않으면 연결 풀로 돌아간 뒤 다른 요청의
      쿼리까지 상한이 걸린다 — 블록이 끝나면 오류가 나도 무제한(-1)으로 되돌린다.
    ★ 시간 초과(1222)는 문장만 실패하고 트랜잭션은 살아 있다(XACT_ABORT 꺼짐) — 되돌리는 문장을 그대로 보낸다.
    ★ 시간 초과는 경고 로그를 남긴다(label = 무엇을 기다렸나) — 배포 뒤 공용 DB 경합 횟수를 센다(11-04 점검).
      문구는 중립이다 — AIDE 칸 수정·원티드 쓰기는 409 가 아니라 도구 문구로 끝난다(label 로 가른다).
    """
    if db.get_bind().dialect.name != "mssql":
        yield
        return
    db.execute(text(f"SET LOCK_TIMEOUT {int(timeout_ms)}"))
    try:
        yield
    except OperationalError as exc:
        if _is_lock_timeout(exc):
            logger.warning("[lock_wait] 잠금 대기 %sms 초과: %s", timeout_ms, label or "-")
            raise WantedBusyError(busy_message) from exc
        raise
    finally:
        try:
            db.execute(text("SET LOCK_TIMEOUT -1"))
        except SQLAlchemyError:
            logger.warning("[wanted_quota] LOCK_TIMEOUT 되돌리기 실패", exc_info=True)


def basis_for(day: date, teams: TeamLimits, ward: dict, team_id: int | None) -> tuple | None:
    """그날 이 팀(None = 기타)에 적용되는 기준 `(basis, 한도)` — 팀 우선, 없으면 병동, 둘 다 없으면 None.

    팀 한도는 날짜 지정 > 팀 기본값(`TeamLimits.limit`). 기타는 팀 한도를 건너뛴다.
    """
    team_limit = teams.limit(team_id, day)
    if team_limit is not None:
        return "team", team_limit
    if day in ward:
        return "ward", ward[day]
    return None


def _exceeded_dates(mine: set, others: dict, ward: dict, teams: TeamLimits,
                    team_map: dict, my_team: int | None) -> list:
    """내 신청 날짜 중 넘치는 것 — 그날 적용 기준에서 다른 사람 제출 수 + 나 1명 > 한도."""
    ward_others = _count_by_date(others)
    team_others = _count_by_date(others, team_map, my_team) if my_team is not None else Counter()
    over = []
    for day in sorted(mine):
        rule = basis_for(day, teams, ward, my_team)
        if rule is None:
            continue
        basis, limit = rule
        n_others = (team_others if basis == "team" else ward_others)[day]
        if n_others + 1 > limit:
            item = {"date": day.isoformat(), "basis": basis, "limit": limit, "others": n_others}
            if basis == "team":
                item["team_id"] = my_team
            over.append(item)
    return over


def _days_text(items: list) -> str:
    days = sorted({int(i["date"][-2:]) for i in items})
    return "·".join(f"{d}일" for d in days)


def _quota_message(over: list, team_name: str | None) -> str:
    """'14일·21일은 A팀 휴가·휴무 신청이 마감됐어요' 형식(화면 기획 5-4)."""
    parts = []
    team_items = [i for i in over if i["basis"] == "team"]
    ward_items = [i for i in over if i["basis"] == "ward"]
    if team_items:
        parts.append(f"{_days_text(team_items)}은 {team_name or '우리 팀'} 휴가·휴무 신청이 마감됐어요")
    if ward_items:
        parts.append(f"{_days_text(ward_items)}은 병동 전체 휴가·휴무 신청이 마감됐어요")
    return "제출하지 못했어요. " + " / ".join(parts) + ". 날짜를 바꾼 뒤 다시 제출해 주세요."


def assert_daily_quota(
    db: Session, *, nurse_id: str, group_id: str, year: int, month: int, request_id: int,
) -> None:
    """제출 직전 검사. 넘치면 PreferenceQuotaError(409 · detail.dates 에 날짜·적용 기준·한도·다른 사람 수).

    호출 전제: `lock_wanted_month` 로 병동·월을 잠갔고, 제출할 칸이 flush 돼 있다.
    """
    quota_ids = quota_shift_ids(db, group_id)
    mine = _own_quota_dates(db, nurse_id, request_id, year, month, quota_ids)
    if not mine:
        return
    ward = ward_limits(db, group_id, year, month)
    teams = load_team_limits(db, group_id, year, month)
    team_map = quota_team_map(db, group_id, year, month) if teams.any() else {}
    my_team = team_map.get(str(nurse_id))
    if not any(basis_for(d, teams, ward, my_team) for d in mine):
        return
    others = submitted_quota_dates(db, group_id, year, month, quota_ids, exclude_nurse_id=str(nurse_id))
    over = _exceeded_dates(mine, others, ward, teams, team_map, my_team)
    if not over:
        return
    team_name = _team_name(db, group_id, my_team)
    for item in over:
        if item["basis"] == "team":
            item["team_name"] = team_name
    print(f"[wanted_quota] 제출 거절: nurse={nurse_id}, {year}-{month:02d}, {over}")
    raise PreferenceQuotaError(_quota_message(over, team_name), detail={"dates": over})


def _quota_cell(basis: str, limit: int, taken: int, own: bool) -> dict:
    """날짜 칸 하나 — 화면은 이 값을 그리기만 한다(PC·모바일 같은 숫자).

    taken = 그 기준 묶음에서 제출본이 잡은 자리(나 포함) · remaining = 남은 자리 · mine = 내 제출본이 그날
    자리를 잡았는지 · available = 지금 이 날짜를 넣어 제출하면 통과하는지(제출 검사 '다른 사람 + 1 ≤ 한도'와 같은 식).
    """
    return {
        "basis": basis,
        "limit": limit,
        "taken": taken,
        "remaining": max(limit - taken, 0),
        "mine": own,
        "available": taken - (1 if own else 0) < limit,
    }


def daily_quota_view(db: Session, *, nurse_id: str, group_id: str, year: int, month: int) -> dict:
    """원티드 작성 화면용 — 날짜마다 **나에게 적용되는** 기준(team|ward)·한도·자리 수(11-04 결정).

    ★ 적용 기준의 숫자만 싣는다 — 다른 팀 수, 팀 기준인 날의 병동 수는 싣지 않는다(11-04 '본인에게 적용되는
      정보만'). 제한이 없는 날은 dates 에 없다. team = 내 팀(그 달 팀 한도가 있을 때만 계산 — 없으면 null).
    """
    ward = ward_limits(db, group_id, year, month)
    teams = load_team_limits(db, group_id, year, month)
    quota_ids = quota_shift_ids(db, group_id)
    team_map = quota_team_map(db, group_id, year, month) if teams.any() else {}
    my_team = team_map.get(str(nurse_id))
    view = {
        "rule": QUOTA_RULE,
        "counted_shift_ids": sorted(quota_ids),
        "team": ({"team_id": my_team, "team_name": _team_name(db, group_id, my_team)}
                 if my_team is not None else None),
        "dates": {},
    }
    days = set(ward) | teams.limited_days(my_team, year, month)
    if not days:
        return view
    subs = submitted_quota_dates(db, group_id, year, month, quota_ids)
    ward_taken = _count_by_date(subs)
    team_taken = _count_by_date(subs, team_map, my_team) if my_team is not None else Counter()
    mine = subs.get(str(nurse_id), set())
    for day in sorted(days):
        rule = basis_for(day, teams, ward, my_team)
        if rule is None:
            continue
        basis, limit = rule
        taken = (team_taken if basis == "team" else ward_taken)[day]
        view["dates"][day.isoformat()] = _quota_cell(basis, limit, taken, day in mine)
    return view


# ── 팀 한도 설정(수간호사·관리자) ─────────────────────────────────────────


def _active_teams(db: Session, group_id: str) -> dict:
    """{team_id: team_name} — 활성 팀만."""
    return {
        int(tid): name
        for tid, name in db.query(Team.team_id, Team.team_name).filter(
            Team.group_id == group_id, Team.active == 1,
        ).all()
    }


def _submitted_counts(subs: dict, team_map: dict) -> dict:
    """{날짜: {total, by_team: {team_id: n}}} — 신청이 있는 날만."""
    out: dict = {}
    for nurse_id, days in subs.items():
        team_id = team_map.get(nurse_id)
        for day in days:
            cell = out.setdefault(day.isoformat(), {"total": 0, "by_team": {}})
            cell["total"] += 1
            if team_id is not None:
                key = str(team_id)
                cell["by_team"][key] = cell["by_team"].get(key, 0) + 1
    return dict(sorted(out.items()))


def _members(db: Session, team_map: dict) -> list:
    """모달 근무자 목록 — 셈 대상과 같은 사람·같은 팀(`quota_team_map`). 근무자관리 순서(sequence, 이름)."""
    if not team_map:
        return []
    rows = db.query(Nurse.nurse_id, Nurse.name, Nurse.sequence).filter(
        Nurse.nurse_id.in_(list(team_map)),
    ).all()
    rows.sort(key=lambda r: (r.sequence if r.sequence is not None else 1 << 30, r.name or ""))
    return [{"nurse_id": str(r.nurse_id), "name": r.name, "team_id": team_map.get(str(r.nurse_id))}
            for r in rows]


def _limit_days(limits: TeamLimits, ward: dict, year: int, month: int) -> list:
    """한도가 걸린 날짜 — 병동 일자 한도 · 팀 날짜 지정 · (팀 기본값이 있으면) 한 달 전체."""
    days = set(ward) | {d for d, _tid in limits.by_date}
    if limits.defaults:
        days |= set(_month_days(year, month))
    return sorted(days)


def _effective_cell(day: date, team_id, limits: TeamLimits, ward: dict, group_taken: dict,
                    ward_taken: Counter) -> dict:
    """그날 한 묶음(팀 · None=기타)의 적용 결과. taken = 적용 기준 묶음의 제출 수(병동 기준이면 병동 전원),
    기준이 없으면 그 묶음 자신의 제출 수."""
    rule = basis_for(day, limits, ward, team_id)
    if rule is None:
        return {"basis": None, "limit": None, "taken": group_taken[team_id][day]}
    basis, limit = rule
    taken = group_taken[team_id][day] if basis == "team" else ward_taken[day]
    return {"basis": basis, "limit": limit, "taken": taken}


def _effective(limits: TeamLimits, ward: dict, teams: dict, team_map: dict, subs: dict,
               year: int, month: int) -> dict:
    """날짜 × (팀 + 기타) 적용 결과 — 모달 달력·관리보드가 분기 계산 없이 그리게(크기 = 날짜 × (팀 수 + 1))."""
    ward_taken = _count_by_date(subs)
    group_taken = {tid: _count_by_date(subs, team_map, tid) for tid in teams}
    group_taken[None] = _count_by_date({n: d for n, d in subs.items() if team_map.get(n) is None})
    order = sort_team_ids_by_name(teams)
    return {
        day.isoformat(): {
            "ward": {"limit": ward[day], "taken": ward_taken[day]} if day in ward else None,
            "teams": [{"team_id": tid, **_effective_cell(day, tid, limits, ward, group_taken, ward_taken)}
                      for tid in order],
            "unassigned": _effective_cell(day, None, limits, ward, group_taken, ward_taken),
        }
        for day in _limit_days(limits, ward, year, month)
    }


def get_team_limits(db: Session, group_id: str, year: int, month: int) -> dict:
    """설정 창·관리보드용 — 팀 목록·기본값·날짜 예외·근무자 목록·병동 일자 한도·날짜×(팀+기타) 적용 결과·
    날짜×팀 제출 수·경고. 팀과 셈 대상은 제출 검사와 같다(`quota_team_map`)."""
    _assert_year_month(year, month)
    teams = _active_teams(db, group_id)
    limits = load_team_limits(db, group_id, year, month)
    team_map = quota_team_map(db, group_id, year, month)
    quota_ids = quota_shift_ids(db, group_id)
    subs = submitted_quota_dates(db, group_id, year, month, quota_ids)
    ward = ward_limits(db, group_id, year, month)
    members = Counter(t for t in team_map.values() if t in teams)
    view = {
        "year": year,
        "month": month,
        "group_id": group_id,
        "storage_ready": team_limit_storage_ready(db),
        "rule": QUOTA_RULE,
        "counted_shift_ids": sorted(quota_ids),
        "teams": [
            {"team_id": tid, "team_name": teams[tid], "member_count": members.get(tid, 0),
             "default_limit": limits.defaults.get(tid)}
            for tid in sort_team_ids_by_name(teams)
        ],
        "unassigned_count": sum(1 for t in team_map.values() if t not in teams),
        "members": _members(db, team_map),
        "ward_limits": {d.isoformat(): v for d, v in sorted(ward.items())},
        # ★ 활성 팀 행만 — 비활성 팀 행을 실으면 화면이 받은 그대로 되보낸 저장(통째 교체)이 400 이 된다(11-04 리뷰).
        #   판정에서도 비활성 팀원은 기타라 그 행은 쓰이지 않는다.
        "dates": [
            {"target_date": d.isoformat(), "team_id": tid, "max_requests": v}
            for (d, tid), v in sorted(limits.by_date.items()) if tid in teams
        ],
        "submitted_counts": _submitted_counts(subs, team_map),
        "effective": _effective(limits, ward, teams, team_map, subs, year, month),
    }
    view["warnings"] = _limit_warnings(view, limits, ward, teams, team_map)
    return view


def _assert_year_month(year: int, month: int) -> None:
    if not (1 <= int(month) <= 12) or not (2000 <= int(year) <= 2100):
        raise ValueError(f"잘못된 연월입니다: {year}-{month}")


def _check_team(team_id: int, teams: dict) -> None:
    if int(team_id) not in teams:
        raise ValueError(f"이 병동의 활성 팀이 아닙니다: team_id={team_id}")


def _validated_rows(defaults: list, dates: list, teams: dict, year: int, month: int) -> list:
    """저장할 (team_id, 날짜|None, 한도) 목록. 팀·날짜·중복을 검사한다(위반 시 ValueError)."""
    start, end = _month_range(year, month)
    seen, rows = set(), []
    for item in defaults:
        _check_team(item["team_id"], teams)
        rows.append((int(item["team_id"]), None, int(item["max_requests"])))
    for item in dates:
        _check_team(item["team_id"], teams)
        day = _as_date(item["target_date"])
        if not (start <= day <= end):
            raise ValueError(f"{year}년 {month}월에 속하지 않는 날짜입니다: {day.isoformat()}")
        rows.append((int(item["team_id"]), day, int(item["max_requests"])))
    for team_id, day, limit in rows:
        if (team_id, day) in seen:
            raise ValueError(f"같은 팀·날짜가 중복되었습니다: team_id={team_id}, {day or '기본값'}")
        if limit < 0:
            raise ValueError("한도는 0 이상이어야 합니다.")
        seen.add((team_id, day))
    return rows


def _over_limit_warnings(view: dict, limits: TeamLimits) -> list:
    """이미 제출된 신청이 팀 한도를 넘는 날짜·팀 — 기존 신청은 유지되고 새 신청만 막힌다."""
    names = {t["team_id"]: t["team_name"] for t in view["teams"]}
    warnings = []
    for day_iso, cell in view["submitted_counts"].items():
        day = date.fromisoformat(day_iso)
        for key, submitted in cell["by_team"].items():
            limit = limits.limit(int(key), day)
            if limit is not None and submitted > limit:
                warnings.append({"kind": "over_limit", "date": day_iso, "team_id": int(key),
                                 "team_name": names.get(int(key)), "limit": limit, "submitted": submitted})
    return warnings


def _ward_overflow_warnings(limits: TeamLimits, ward: dict, teams: dict, team_map: dict) -> list:
    """그날 병동 전체 신청이 병동 일자 한도를 넘을 수 있는 날 — 팀 한도를 정한 팀원에게는 병동 한도가 걸리지 않는다
    (팀 우선 · 11-04 결정 D1 의 대가).

    ★ 최대 인원 = min(병동 한도, 병동 기준 인원) + Σ min(팀 한도, 팀 인원). 병동 기준 인원(기타 + 그날 팀 한도가
      없는 팀원)이 병동 자리를 먼저 채우면 팀 기준 인원이 그 위에 더해진다 — 팀 한도 합만 보면 이 경우를 놓친다(11-04 리뷰).
    """
    sizes = Counter(t if t in teams else None for t in team_map.values())
    warnings = []
    for day in sorted(ward):
        limited = {tid: limits.limit(tid, day) for tid in sort_team_ids_by_name(teams)}
        limited = {tid: v for tid, v in limited.items() if v is not None}
        if not limited:
            continue
        ward_basis = sum(n for tid, n in sizes.items() if tid not in limited)
        max_total = min(ward[day], ward_basis) + sum(min(v, sizes[tid]) for tid, v in limited.items())
        if max_total > ward[day]:
            warnings.append({"kind": "ward_limit_may_overflow", "date": day.isoformat(),
                             "ward_limit": ward[day], "max_total": max_total, "team_ids": list(limited)})
    return warnings


def _limit_warnings(view: dict, limits: TeamLimits, ward: dict, teams: dict, team_map: dict) -> list:
    """설정 경고(저장은 막지 않는다) — `over_limit` · `ward_limit_may_overflow`. 조회·저장 응답에 같이 싣는다."""
    return _over_limit_warnings(view, limits) + _ward_overflow_warnings(limits, ward, teams, team_map)


def replace_team_limits(
    db: Session, group_id: str, year: int, month: int, *, defaults: list, dates: list,
) -> dict:
    """그 달 팀 한도를 통째로 교체한다(빈 목록 = 팀 한도 끔). 저장 뒤 설정 조회 + 초과 경고."""
    _assert_year_month(year, month)
    if not team_limit_storage_ready(db):
        raise TeamLimitUnavailableError("팀 한도 저장소(wanted_team_limit)가 아직 준비되지 않았습니다.")
    rows = _validated_rows(defaults, dates, _active_teams(db, group_id), year, month)
    # 같은 병동·달 저장끼리 줄 세운다 — 둘이 동시에 '지우고 넣기'를 하면 두 저장의 행이 섞인다.
    # (제출과는 따로다 — 제출은 원티드 행을 잠근다. 시간 초과 409 · 잠금 오류 500, MSSQL 만.)
    from services.schedule_versioning import _applock

    _applock(db, f"wanted_team_limit:{group_id}:{year}:{month}", _LOCK_TIMEOUT_MS,
             "다른 팀 한도 저장이 진행 중입니다. 잠시 후 다시 시도해 주세요.")
    db.query(WantedTeamLimit).filter(
        WantedTeamLimit.group_id == group_id,
        WantedTeamLimit.year == year,
        WantedTeamLimit.month == month,
    ).delete(synchronize_session=False)
    db.add_all([
        WantedTeamLimit(group_id=group_id, year=year, month=month, team_id=team_id,
                        target_date=day, max_requests=limit)
        for team_id, day, limit in rows
    ])
    db.commit()
    return get_team_limits(db, group_id, year, month)
