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

from sqlalchemy import text
from sqlalchemy.orm import Session

from services.oncall_assign import (
    OncallMember,
    assign_oncall,
    load_call_code_map,
    team_of_week,
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


def _pick_schedule(db: Session, group_id: str, year: int, month: int):
    """그 달의 **대표 근무표 1건** — 발행본 우선, 없으면 셀이 있는 최신 draft.

    ★ 같은 달에 근무표가 여러 개 쌓인다(실측 2026-08: 7건 · 2026-09: 8건).
      전부 합쳐 읽으면 재생성할 때마다 실적이 뒤섞여 역산도 학습도 망가진다.
      확정된 것이 있으면 그것이 사실이고, 없으면 가장 최근 시도를 본다.
    """
    from db.models import Schedule, ScheduleEntry

    rows = db.query(Schedule).filter(
        Schedule.group_id == group_id,
        Schedule.year == year, Schedule.month == month).all()
    if not rows:
        return None
    issued = [r for r in rows if str(r.status or "").strip() == "issued"]
    for cand in (issued or rows):
        pass
    pool = issued or rows
    pool = sorted(pool, key=lambda r: (r.created_at or date.min), reverse=True)
    for r in pool:
        if db.query(ScheduleEntry).filter(
                ScheduleEntry.schedule_id == r.schedule_id).first() is not None:
            return r.schedule_id
    return None


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
    sid = _pick_schedule(db, group_id, prev_y, prev_m)
    if sid is None:
        return None
    rows = db.query(ScheduleEntry).filter(ScheduleEntry.schedule_id == sid).all()
    calls = [e for e in rows if str(e.shift_id or "").strip() in call_codes]
    if not calls:
        return None

    def _d(v):
        return v.date() if hasattr(v, "date") else v

    last_monday = max(week_start(_d(e.work_date)) for e in calls)
    # ★ 팀 조회 시점은 **대상 월 1일 이상**으로 clamp 한다. 직전 달 마지막 콜 주의
    #   월요일이 전월로 넘어가면 nurse_team_period 구간(예: 2026-08-01 신설) 밖이라
    #   전원 팀 미배정으로 떨어져 역산이 통째로 실패한다(2026-08 생성에서 실측).
    #   그 주의 담당 팀은 "지금 팀 구성" 으로 해석하는 게 맞다 — 팀이 그때 생겼다면
    #   그 편성이 곧 그 주의 편성이다.
    team_of = _team_map_asof(db, group_id, max(last_monday, date(year, month, 1)))
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


#: 담당주 수행률이 이 값 미만이면 "상시 부분 면제" 로 보고 상한을 학습한다.
#: 2026-08 실측(수술실): 윤보라 43% 만 아래로 튀고 나머지는 71~100% 였다.
#: 그달 사정으로 한두 번 빠진 사람(성해인 75% · 민희원 71%)은 걸리지 않는 선.
_QUOTA_RATIO_THRESHOLD = 0.70
#: 학습에 쓸 직전 개월 수. 여러 달을 보면 우연한 한 달에 흔들리지 않는다.
_QUOTA_LOOKBACK_MONTHS = 3


def _learn_call_quota(
    db: Session, group_id: str, year: int, month: int, code_map: dict[str, str],
) -> dict[str, int]:
    """직전 달 실적에서 개인별 월 콜 상한을 **학습**한다.

    ★ 설정으로 박지 않는 이유
      "윤보라 월 3회" 를 파라미터로 넣으면 사람이 바뀌거나 병동이 늘 때마다 다시
      넣어야 한다. 앵커를 저장하지 않고 직전 달에서 역산하는 이 모듈의 방식과도
      어긋난다. 과거 근무표에 이미 답이 남아 있으므로 거기서 읽는다.

    ★ 무엇을 재는가 — **담당주 수행률**
      그 사람 팀의 담당 주간 중 "콜을 설 수 있었던 날"(근무가 콜 코드이거나 그 기반
      근무인 날) 대비 "실제로 선 날" 의 비율. 휴가·교육으로 못 선 날은 분모에서
      빠지므로, 그달 사정과 **상시 면제**가 구분된다.

    ★ 상한은 그 사람의 **최대 실적**으로 잡는다(보수적)
      평균으로 잡으면 우연히 적었던 달에 끌려가 실제보다 빡빡해진다.
    """
    from db.models import Schedule, ScheduleEntry

    call_codes = set(code_map.values())
    base_codes = set(code_map)
    stats: dict[str, list[tuple[int, int]]] = {}   # nurse_id → [(수행, 가능), ...]
    #: ★ 학습은 **팀 소속이 그대로일 때만** 유효하다. 수행률의 분모가 "담당주" 라
    #:   팀이 바뀌면 비교 기준 자체가 달라진다. 2026-08 에 팀이 전면 재편된 전례가
    #:   있어(7월 조와 team2 만 동일) 그때 7월 실적을 그대로 썼다면 엉뚱한 상한이
    #:   붙었을 것이다. 소속이 달라진 달의 샘플은 버린다.
    now_team = _team_map_asof(db, group_id, date(year, month, 1))

    y, m = year, month
    for _ in range(_QUOTA_LOOKBACK_MONTHS):
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
        sid = _pick_schedule(db, group_id, y, m)
        if sid is None:
            continue
        rows = db.query(ScheduleEntry).filter(ScheduleEntry.schedule_id == sid).all()
        if not any(str(e.shift_id or "").strip() in call_codes for e in rows):
            continue                       # 그달에 콜 자체가 없으면 판단 근거가 아니다
        cells: dict[str, dict[date, str]] = {}
        for e in rows:
            d = e.work_date.date() if hasattr(e.work_date, "date") else e.work_date
            cells.setdefault(str(e.nurse_id), {})[d] = str(e.shift_id or "").strip()
        _tally_month(db, group_id, y, m, cells, base_codes, call_codes, stats,
                     now_team=now_team)

    quota: dict[str, int] = {}
    for nid, samples in stats.items():
        ratios = [did / able for did, able in samples if able]
        if not ratios or max(ratios) >= _QUOTA_RATIO_THRESHOLD:
            continue                       # 한 달이라도 정상이면 면제로 보지 않는다
        quota[nid] = max(did for did, _able in samples)
    return quota


def _tally_month(
    db: Session, group_id: str, y: int, m: int, cells: dict, base_codes: set,
    call_codes: set, stats: dict, now_team: dict[str, int],
) -> None:
    """한 달치 담당주 수행/가능 일수를 `stats` 에 누적한다.

    ★ 그때 팀과 지금 팀이 다른 사람은 건너뛴다 — 담당주가 달라져 수행률을
      나란히 놓을 수 없다.
    """
    start = _resolve_start_team(db, group_id, y, m, call_codes)
    if start is None:
        return
    anchor, start_team = start
    members = _members_asof(db, group_id, max(anchor, date(y, m, 1)))
    teams = sorted({mm.team_id for mm in members})
    if not teams or start_team not in teams:
        return
    i = teams.index(start_team)
    order = teams[i:] + teams[:i]
    days_in_month = calendar.monthrange(y, m)[1]
    for mm in members:
        if now_team.get(mm.nurse_id) != mm.team_id:
            continue                       # 그 사이 팀이 바뀐 사람 — 비교 불가
        able = did = 0
        for dd in range(1, days_in_month + 1):
            cur = date(y, m, dd)
            if team_of_week(week_start(cur), anchor, order) != mm.team_id:
                continue
            code = (cells.get(mm.nurse_id) or {}).get(cur)
            if code in call_codes:
                able += 1
                did += 1
            elif code in base_codes:
                able += 1
        if able:
            stats.setdefault(mm.nurse_id, []).append((did, able))


def _protected_days(
    db: Session, group_id: str, year: int, month: int,
) -> dict[str, set[int]]:
    """확정 원티드(`is_applied=True`)로 굳힌 날 `{nurse_id: {day,...}}`.

    담당주·대체 **양쪽 모두** 이 날을 콜로 덮지 않는다. 근무를 못 서는 것이 아니라
    콜만 면제되는 자리라, 결원으로 잡혀 다른 사람이 대신 받는다(실측 2026-08
    윤보라 8/10~13 — 본인은 D1 유지, 콜은 같은 팀 3명이 나눠 받았다).
    """
    from db.models import FixedWantedEntry

    rows = db.query(FixedWantedEntry).filter(
        FixedWantedEntry.group_id == group_id,
        FixedWantedEntry.year == year, FixedWantedEntry.month == month,
        FixedWantedEntry.is_applied == True,  # noqa: E712
    ).all()
    out: dict[str, set[int]] = {}
    for r in rows:
        out.setdefault(str(r.nurse_id), set()).add(r.shift_date.day)
    return out


#: 직전일 N 을 금지하는 확정 원티드 type — CP-SAT `ban_night_before_fixed_off` 과 동일.
#: `O`/`휴무`/`주휴` 는 자발·자동 OFF 라 대상이 아니다(cp_sat_basic `_BAN_N_TYPES`).
_BAN_EVE_TYPES = ("휴가", "공가")


def _ban_night_before_off(db: Session, group_id: str) -> bool:
    """`roster_config.ban_night_before_fixed_off` — 컬럼이 없으면 solver 기본값(True)."""
    row = db.execute(text(
        "SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_NAME='roster_config' AND COLUMN_NAME='ban_night_before_fixed_off'"
    )).scalar()
    if not row:
        return True
    val = db.execute(
        text("SELECT TOP 1 ban_night_before_fixed_off FROM roster_config WHERE group_id=:g"),
        {"g": group_id},
    ).scalar()
    return True if val is None else bool(val)


def _call_banned_eves(
    db: Session, group_id: str, year: int, month: int,
) -> dict[str, set[int]]:
    """확정 원티드 **휴가/공가 직전일** `{nurse_id: {day,...}}` — 콜 금지.

    ★ 콜은 야간 대기라 N 과 같은 판정을 받는다. 쉬기로 굳힌 날 바로 앞에 N 을
      못 두는 것과 같은 이유로 콜도 둘 수 없다.
    ★ 1일의 전날은 전월이라 대상 밖(CP-SAT 도 `prev_d < T0` 를 면제한다).
    """
    from db.models import FixedWantedEntry, Shift

    rows = db.query(FixedWantedEntry, Shift.type).outerjoin(
        Shift, Shift.id == FixedWantedEntry.shifts_table_id,
    ).filter(
        FixedWantedEntry.group_id == group_id,
        FixedWantedEntry.year == year, FixedWantedEntry.month == month,
        FixedWantedEntry.is_applied == True,  # noqa: E712
    ).all()
    out: dict[str, set[int]] = {}
    for r, stype in rows:
        if str(stype or "") not in _BAN_EVE_TYPES:
            continue
        if r.shift_date.day <= 1:
            continue
        out.setdefault(str(r.nurse_id), set()).add(r.shift_date.day - 1)
    return out


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

    # ★ 여기도 대상 월 1일로 clamp — anchor 가 전월(예: 7/27)이면 팀 구간 밖이라
    #   멤버가 0명으로 나와 "시작 팀이 유효하지 않다" 로 빠진다.
    members = _members_asof(db, group_id, max(anchor, date(year, month, 1)))
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

    # ★ 개인별 콜 상한은 설정이 아니라 **직전 달 실적에서 학습**한다.
    quota = _learn_call_quota(db, group_id, year, month, code_map)
    if quota:
        from db.models import Nurse
        _nm = {str(n.nurse_id): n.name for n in
               db.query(Nurse).filter(Nurse.group_id == group_id).all()}
        print("[Oncall] 학습된 콜 상한(직전 실적 기준): "
              + ", ".join(f"{_nm.get(k, k)} {v}회" for k, v in quota.items()))

    # ★ 확정 원티드로 굳힌 날 — 대체 투입으로 덮지 않는다.
    protected = _protected_days(db, group_id, year, month)

    # ★ 콜 = 야간 대기 → 확정 원티드 휴가/공가 **직전일**은 N 과 같이 막는다.
    unavailable = (_call_banned_eves(db, group_id, year, month)
                   if _ban_night_before_off(db, group_id) else {})
    if unavailable:
        print(f"[Oncall] 원티드 휴가/공가 직전일 콜 금지 "
              f"{sum(len(v) for v in unavailable.values())}건")

    res = assign_oncall(
        year=year, month=month, roster=roster,
        members_asof=lambda mon: _members_asof(db, group_id, max(mon, date(year, month, 1))),
        anchor=anchor, team_order=team_order, call_code_map=code_map,
        max_per_month=quota, protected=protected, unavailable=unavailable,
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
