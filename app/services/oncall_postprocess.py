"""근무표 생성 후처리 — 콜 당번 배정.

`generated`(= `{nurse_id: [일자별 코드]}`)의 코드를 콜 코드로 갈아끼운다.
근무를 빼는 게 아니라 **표기만 바꾼다** — `D1` → `D1콜`, `O` → `오프콜`.

## 켜고 끄는 스위치가 따로 없다

`shifts.call_base_id` 등록 여부가 곧 사용 여부다. 등록이 없으면
`load_call_code_map()` 이 빈 dict 를 돌려주고 여기서 **아무것도 하지 않고 반환**한다.
기본값을 두면 모든 병동에 있는 `O` 가 걸려 미사용 병동까지 콜이 붙는다
(2026-08-18 실측: office 102243 의 15개 그룹 중 `오프콜` 코드 보유는 수술실뿐).

## 앵커를 저장하지 않는다

로테이션은 팀 번호 오름차순 순환이고, 시작점은 **직전 달 근무표에서 역산**한다.
그게 "꼬리물기" 그 자체다 — 8월 마지막 주가 team3 이면 9월 첫 주도 team3 으로 이어진다.
직전 달에 콜이 없으면(최초 도입) 배정하지 않고 넘어간다. 첫 달은 수동으로 넣어야 한다.
"""
from __future__ import annotations

import calendar
from datetime import date, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from services.oncall_assign import (
    OncallMember,
    assign_oncall,
    load_call_code_map,
    week_start,
)


def _team_map_asof(db: Session, group_id: str, asof: date) -> dict[str, int]:
    """그 시점 `{nurse_id: team_id}`. 팀 미배정자는 담지 않는다."""
    from db.models import Nurse, NurseTeamPeriod
    from services.nurse_period_resolver import fetch_periods, resolve_asof

    nurses = db.query(Nurse).filter(
        Nurse.group_id == group_id, Nurse.active == 1).all()
    ids = [str(n.nurse_id) for n in nurses]
    if not ids:
        return {}
    # ★ fetch_periods 는 [start, end) 반열림 — start==end 면 0건이 돌아온다.
    periods = fetch_periods(db, NurseTeamPeriod, ids, asof, asof + timedelta(days=1))
    sentinel = object()
    out: dict[str, int] = {}
    for n in nurses:
        nid = str(n.nurse_id)
        v = resolve_asof(periods.get(nid), asof, "team_id", default=sentinel)
        eff = n.team_id if v is sentinel else v
        if eff is not None:
            out[nid] = int(eff)
    return out


def _members_asof(db: Session, group_id: str, monday: date) -> list[OncallMember]:
    """그 주 시점 팀 멤버. 서열(rank)은 팀 안에서 `sequence` 오름차순."""
    from db.models import Nurse

    team_of = _team_map_asof(db, group_id, monday)
    if not team_of:
        return []
    nurses = {str(n.nurse_id): n for n in db.query(Nurse).filter(
        Nurse.group_id == group_id, Nurse.active == 1).all()}
    out: list[OncallMember] = []
    for tid in sorted(set(team_of.values())):
        mem = sorted(
            [nid for nid, t in team_of.items() if t == tid],
            key=lambda nid: int(getattr(nurses.get(nid), "sequence", 0) or 0),
        )
        for i, nid in enumerate(mem, start=1):
            n = nurses.get(nid)
            out.append(OncallMember(
                nurse_id=nid, name=str(getattr(n, "name", "")), team_id=tid, rank=i))
    return out


def _resolve_start_team(
    db: Session, group_id: str, year: int, month: int, call_codes: set[str],
) -> Optional[tuple[date, int]]:
    """직전 달 근무표에서 마지막 콜 주의 담당 팀을 역산한다.

    반환: `(그 달 첫 주 월요일, 그 주에 배정할 팀)` — 없으면 None.

    ★ 팀 최빈값으로 판정한다. 결원 대체 때문에 한 주에 타 팀 사람이 섞이는데
      (실측 8월 5건), 다수결이면 그 노이즈에 흔들리지 않는다.
    """
    from db.models import Schedule, ScheduleEntry

    prev_y, prev_m = (year - 1, 12) if month == 1 else (year, month - 1)
    sids = [s.schedule_id for s in db.query(Schedule).filter(
        Schedule.group_id == group_id,
        Schedule.year == prev_y, Schedule.month == prev_m).all()]
    if not sids:
        return None
    rows = db.query(ScheduleEntry).filter(
        ScheduleEntry.schedule_id.in_(sids)).all()
    calls = [e for e in rows if str(e.shift_id or "").strip() in call_codes]
    if not calls:
        return None

    def _d(v):
        return v.date() if hasattr(v, "date") else v

    last_monday = max(week_start(_d(e.work_date)) for e in calls)
    team_of = _team_map_asof(db, group_id, last_monday)
    tally: dict[int, int] = {}
    for e in calls:
        if week_start(_d(e.work_date)) != last_monday:
            continue
        t = team_of.get(str(e.nurse_id))
        if t is not None:
            tally[t] = tally.get(t, 0) + 1
    if not tally:
        return None
    last_team = max(tally.items(), key=lambda kv: kv[1])[0]

    teams = sorted(set(team_of.values()))
    if last_team not in teams:
        return None
    # 다음 주 = 팀 번호 오름차순으로 한 칸
    nxt = teams[(teams.index(last_team) + 1) % len(teams)]
    this_monday = week_start(date(year, month, 1))
    # 직전 달 마지막 콜 주가 이번 달 첫 주와 같으면(월 경계 주) 그 팀을 그대로 이어간다
    if last_monday == this_monday:
        return this_monday, last_team
    return this_monday, nxt


def postprocess_oncall(
    db: Session, schedule, generated: dict, current_user, req,
) -> dict:
    """생성 결과에 콜 코드를 얹는다. 실패해도 원본을 그대로 돌려준다."""
    if not isinstance(generated, dict) or not generated:
        return generated
    group_id = str(getattr(current_user, "group_id", "") or "")
    if not group_id:
        return generated

    code_map = load_call_code_map(db, group_id)
    if not code_map:
        return generated                     # 콜 미사용 그룹 — 손대지 않는다

    year, month = int(req.year), int(req.month)
    days_in_month = calendar.monthrange(year, month)[1]

    start = _resolve_start_team(db, group_id, year, month, set(code_map.values()))
    if start is None:
        print("[Oncall] 직전 달 콜 배정이 없어 시작 팀을 역산할 수 없습니다 — 배정 건너뜀")
        return generated
    anchor, start_team = start

    members = _members_asof(db, group_id, anchor)
    teams = sorted({m.team_id for m in members})
    if not teams or start_team not in teams:
        print(f"[Oncall] 팀 구성이 없거나 시작 팀({start_team})이 유효하지 않습니다 — 배정 건너뜀")
        return generated
    # 시작 팀부터 오름차순 순환하도록 순서를 회전한다
    i = teams.index(start_team)
    team_order = teams[i:] + teams[:i]

    # generated(list) → roster(dict[day])
    roster: dict[str, dict[int, str]] = {}
    for nid, days in generated.items():
        if not isinstance(days, list):
            continue
        roster[str(nid)] = {
            d: str(days[d - 1] or "").strip()
            for d in range(1, min(days_in_month, len(days)) + 1)
        }

    res = assign_oncall(
        year=year, month=month, roster=roster,
        members_asof=lambda mon: _members_asof(db, group_id, max(mon, date(year, month, 1))),
        anchor=anchor, team_order=team_order, call_code_map=code_map,
    )

    for c in res.cells:
        days = generated.get(c.nurse_id)
        if isinstance(days, list) and 0 <= c.day - 1 < len(days):
            days[c.day - 1] = c.after

    print(f"[Oncall] 시작팀=team{start_team}({anchor}) 순서={team_order} "
          f"→ {len(res.cells)}셀 배정(대체 {sum(1 for c in res.cells if c.substitute)}) "
          f"· 결원 {len(res.vacancies)}")
    for v in res.vacancies:
        print(f"[Oncall][결원] {month}/{v.day} t{v.team_id}-{v.rank} {v.name} — {v.reason}")
    return generated
