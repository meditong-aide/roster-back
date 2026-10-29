"""원티드 일자별 신청 한도(병동 전체 + 팀별) — 셈·제출 검사·작성 화면 조회·팀 한도 설정.

- 세는 대상 = 휴가·휴무 계열 '희망' 원티드. 화면(`Roster_wanted.tsx` targetShiftIds)과 같은 기준:
  shifts.type 이 OFF·휴가·휴무·연차 이거나 코드 'O'. 근무 희망(D·E·N)·기피·공가는 세지 않는다.
- 자리를 차지하는 것 = 간호사별 그 달 **최신 제출본**(`/preferences/all` 과 같은 범위). 임시저장은
  자리를 잡지 않는다(제출 선착순). 철회하면 자리가 돌아간다.
- 팀 = 그 달 1일 기준 팀 배정(`resolve_teams_for_month`). 팀 미배정자는 팀 한도 대상이 아니다.
- 팀 한도 우선순위 = 날짜 지정 > 팀 기본값 > 제한 없음. 병동 전체 제한(`wanted_config`)과 함께 적용.
- 한도를 낮춰도 이미 제출된 신청은 그대로 두고 새 제출만 막는다.
- 수간호사가 관리보드에서 직접 넣는 원티드는 이 검사를 타지 않는다(한도 예외). AIDE 에이전트의
  원티드 추가·삭제·변경(`agents_v2/tools/wanted_tools.py` `_submit_draft`)도 쓰기 스킬이
  수간호사·ADM 전용(`agents_v2/middleware.py` 권한 게이트)이라 같은 예외로 둔다.
- 검사하는 제출 경로 = `/preferences/submit` 의 두 갈래(`save_wanted_entries_service` · 구형 data 경로).
"""
import calendar
from collections import Counter
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
from services.preferences_service import PreferenceQuotaError
from services.team_period import resolve_teams_for_month, sort_team_ids_by_name
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

QUOTA_SHIFT_TYPES = ("OFF", "휴가", "휴무", "연차")

_TEAM_LIMIT_READY = False


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
    오염시키므로 사후 try/except 로는 안 된다). True 는 캐시, False 는 캐시하지 않는다 —
    DDL 적용 뒤 서버 재기동 없이 바로 반영되도록.
    """
    global _TEAM_LIMIT_READY
    if _TEAM_LIMIT_READY:
        return True
    try:
        _TEAM_LIMIT_READY = sa_inspect(db.get_bind()).has_table("wanted_team_limit")
    except SQLAlchemyError as e:
        print(f"[wanted_quota] 팀 한도 저장소 확인 실패(미사용 처리): {e}")
        return False
    if not _TEAM_LIMIT_READY:
        print("[wanted_quota] wanted_team_limit 표 없음 — 팀 한도 건너뜀(병동 전체 한도만)")
    return _TEAM_LIMIT_READY


def quota_shift_ids(db: Session, group_id: str) -> set:
    """한도에서 세는 근무코드(휴가·휴무 계열)."""
    rows = db.query(Shift.shift_id, Shift.type).filter(Shift.group_id == group_id).all()
    return {sid for sid, stype in rows if (stype or "").strip() in QUOTA_SHIFT_TYPES or sid == "O"}


def ward_limits(db: Session, group_id: str, year: int, month: int) -> dict:
    """병동 전체 일자별 한도 {날짜: 한도}. 화면처럼 shift_type 은 보지 않는다(항상 NULL)."""
    start, end = _month_range(year, month)
    rows = db.query(WantedConfig.target_date, WantedConfig.max_requests).filter(
        WantedConfig.group_id == group_id,
        WantedConfig.target_date >= start,
        WantedConfig.target_date <= end,
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
    """{nurse_id: 최신 제출본 request_id} — 이 병동 소속·이 병동에 낸 제출본만(`/preferences/all` 과 같은 범위)."""
    member_ids = [str(n) for (n,) in db.query(Nurse.nurse_id).filter(Nurse.group_id == group_id).all()]
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


def lock_wanted_month(db: Session, group_id: str, year: int, month: int) -> None:
    """병동·월 원티드 행을 잠근다(커밋까지). 같은 병동·월 제출이 줄을 서서 뒤 사람이 앞 사람 제출을 센다.

    ★ 제출 경로는 **자기 신청 칸을 쓰기 전에** 이걸 먼저 부른다. 칸을 쓴 뒤 잠그면 두 제출이
      서로의 미커밋 칸(행 잠금)과 원티드 행 잠금을 엇갈려 기다려 교착이 난다.
    ★ `with_for_update()` 는 이 환경(mssql+pymssql·SQLAlchemy 2.0)에서 SQL 에 아무것도 붙이지
      않는다(실측) — 테이블 힌트로 건다(`retract_submission_service` 와 같은 방식).
    """
    db.query(Wanted).with_hint(Wanted, "WITH (UPDLOCK, ROWLOCK)", "mssql").filter(
        Wanted.group_id == group_id, Wanted.year == year, Wanted.month == month,
    ).first()


def _exceeded_dates(mine: set, others: dict, ward: dict, teams: TeamLimits,
                    team_map: dict, my_team: int | None) -> list:
    """내 신청 날짜 중 넘치는 것 — 다른 사람 제출 수 + 나 1명 > 한도."""
    ward_used = _count_by_date(others)
    team_used = _count_by_date(others, team_map, my_team) if my_team is not None else Counter()
    over = []
    for day in sorted(mine):
        limit = ward.get(day)
        if limit is not None and ward_used[day] + 1 > limit:
            over.append({"date": day.isoformat(), "scope": "ward", "limit": limit, "used": ward_used[day]})
        team_limit = teams.limit(my_team, day)
        if team_limit is not None and team_used[day] + 1 > team_limit:
            over.append({"date": day.isoformat(), "scope": "team", "team_id": my_team,
                         "limit": team_limit, "used": team_used[day]})
    return over


def _days_text(items: list) -> str:
    days = sorted({int(i["date"][-2:]) for i in items})
    return "·".join(f"{d}일" for d in days)


def _quota_message(over: list, team_name: str | None) -> str:
    """'14일·21일은 A팀 휴가·휴무 신청이 마감됐어요' 형식(화면 기획 5-4)."""
    parts = []
    team_items = [i for i in over if i["scope"] == "team"]
    ward_items = [i for i in over if i["scope"] == "ward"]
    if team_items:
        parts.append(f"{_days_text(team_items)}은 {team_name or '우리 팀'} 휴가·휴무 신청이 마감됐어요")
    if ward_items:
        parts.append(f"{_days_text(ward_items)}은 병동 전체 휴가·휴무 신청이 마감됐어요")
    return "제출하지 못했어요. " + " / ".join(parts) + ". 날짜를 바꾼 뒤 다시 제출해 주세요."


def assert_daily_quota(
    db: Session, *, nurse_id: str, group_id: str, year: int, month: int, request_id: int,
) -> None:
    """제출 직전 검사. 넘치면 PreferenceQuotaError(409 · detail.dates 에 날짜·범위).

    호출 전제: `lock_wanted_month` 로 병동·월을 잠갔고, 제출할 칸이 flush 돼 있다.
    """
    quota_ids = quota_shift_ids(db, group_id)
    mine = _own_quota_dates(db, nurse_id, request_id, year, month, quota_ids)
    if not mine:
        return
    ward = ward_limits(db, group_id, year, month)
    teams = load_team_limits(db, group_id, year, month)
    team_map = resolve_teams_for_month(db, group_id, date(year, month, 1)) if teams.any() else {}
    my_team = team_map.get(str(nurse_id))
    if not any(d in ward or teams.limit(my_team, d) is not None for d in mine):
        return
    others = submitted_quota_dates(db, group_id, year, month, quota_ids, exclude_nurse_id=str(nurse_id))
    over = _exceeded_dates(mine, others, ward, teams, team_map, my_team)
    if not over:
        return
    team_name = _team_name(db, group_id, my_team)
    for item in over:
        if item["scope"] == "team":
            item["team_name"] = team_name
    print(f"[wanted_quota] 제출 거절: nurse={nurse_id}, {year}-{month:02d}, {over}")
    raise PreferenceQuotaError(_quota_message(over, team_name), detail={"dates": over})


def _quota_cell(day: date, ward: dict, ward_used: Counter, team_limit: int | None,
                team_used: Counter | None, mine: set) -> dict:
    return {
        "ward_limit": ward.get(day),
        "ward_used": ward_used[day],
        "team_limit": team_limit,
        "team_used": team_used[day] if team_used is not None else None,
        "mine": day in mine,
    }


def daily_quota_view(db: Session, *, nurse_id: str, group_id: str, year: int, month: int) -> dict:
    """원티드 작성 화면용 — 한도가 걸린 날짜별 '병동 전체'·'우리 팀' 한도와 현재 제출 수.

    used 는 나를 포함한 제출 수다(화면 기존 dailyRequestCounts 와 같음). mine = 내 제출본이 그날
    자리를 잡고 있는지. 다른 팀 수는 싣지 않는다.
    """
    ward = ward_limits(db, group_id, year, month)
    teams = load_team_limits(db, group_id, year, month)
    team_map = resolve_teams_for_month(db, group_id, date(year, month, 1)) if teams.any() else {}
    my_team = team_map.get(str(nurse_id))
    view = {
        "team_limit_enabled": teams.any(),
        "team": ({"team_id": my_team, "team_name": _team_name(db, group_id, my_team)}
                 if my_team is not None else None),
        "dates": {},
    }
    days = set(ward) | teams.limited_days(my_team, year, month)
    if not days:
        return view
    subs = submitted_quota_dates(db, group_id, year, month, quota_shift_ids(db, group_id))
    ward_used = _count_by_date(subs)
    team_used = _count_by_date(subs, team_map, my_team) if my_team is not None else None
    mine = subs.get(str(nurse_id), set())
    for day in sorted(days):
        view["dates"][day.isoformat()] = _quota_cell(
            day, ward, ward_used, teams.limit(my_team, day), team_used, mine)
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


def get_team_limits(db: Session, group_id: str, year: int, month: int) -> dict:
    """설정 창·관리보드용 — 팀 목록(1일 기준 인원)·기본값·날짜 예외·날짜×팀 제출 수."""
    _assert_year_month(year, month)
    teams = _active_teams(db, group_id)
    limits = load_team_limits(db, group_id, year, month)
    team_map = resolve_teams_for_month(db, group_id, date(year, month, 1))
    members = Counter(t for t in team_map.values() if t in teams)
    subs = submitted_quota_dates(db, group_id, year, month, quota_shift_ids(db, group_id))
    return {
        "year": year,
        "month": month,
        "group_id": group_id,
        "storage_ready": team_limit_storage_ready(db),
        "teams": [
            {"team_id": tid, "team_name": teams[tid], "member_count": members.get(tid, 0),
             "default_limit": limits.defaults.get(tid)}
            for tid in sort_team_ids_by_name(teams)
        ],
        "unassigned_count": sum(1 for t in team_map.values() if t not in teams),
        "dates": [
            {"target_date": d.isoformat(), "team_id": tid, "max_requests": v}
            for (d, tid), v in sorted(limits.by_date.items())
        ],
        "submitted_counts": _submitted_counts(subs, team_map),
    }


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


def _over_limit_warnings(db: Session, group_id: str, year: int, month: int, view: dict) -> list:
    """이미 제출된 신청이 새 한도를 넘는 날짜·팀 — 경고만(기존 신청 유지, 새 신청만 막힘)."""
    limits = load_team_limits(db, group_id, year, month)
    names = {t["team_id"]: t["team_name"] for t in view["teams"]}
    warnings = []
    for day_iso, cell in view["submitted_counts"].items():
        day = date.fromisoformat(day_iso)
        for key, submitted in cell["by_team"].items():
            limit = limits.limit(int(key), day)
            if limit is not None and submitted > limit:
                warnings.append({"date": day_iso, "team_id": int(key), "team_name": names.get(int(key)),
                                 "limit": limit, "submitted": submitted})
    return warnings


def replace_team_limits(
    db: Session, group_id: str, year: int, month: int, *, defaults: list, dates: list,
) -> dict:
    """그 달 팀 한도를 통째로 교체한다(빈 목록 = 팀 한도 끔). 저장 뒤 설정 조회 + 초과 경고."""
    _assert_year_month(year, month)
    if not team_limit_storage_ready(db):
        raise TeamLimitUnavailableError("팀 한도 저장소(wanted_team_limit)가 아직 준비되지 않았습니다.")
    rows = _validated_rows(defaults, dates, _active_teams(db, group_id), year, month)
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
    view = get_team_limits(db, group_id, year, month)
    view["warnings"] = _over_limit_warnings(db, group_id, year, month, view)
    return view
