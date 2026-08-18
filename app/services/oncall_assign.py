"""수술실 온콜(콜 당번) 배정.

주(월~일) 단위로 한 팀이 통째로 콜을 서고, 팀은 4주 주기로 돈다.
실측 근거(2026-07/08 인천의료원 수술실 근무일정표):

  7/27~8/02 team2 · 8/03~09 team3 · 8/10~16 team4 · 8/17~23 team1 · 8/24~30 team2

콜은 **근무를 대체하지 않고 코드를 갈아끼운다** — `schedule_entries` 가 (간호사, 날짜)당
코드 하나만 담기 때문이다. 그날 근무가 있으면 `D1` → `D1콜`, 쉬는 날이면 `O` → `오프콜`.

## ★ 팀은 그릇, 사람은 내용물

로테이션은 **팀 번호**로 돌고 멤버는 **그 주 월요일 시점 as-of** 로 조회한다.
2026-08 에 팀이 전면 재편된 전례가 있어(7월 조와 team2 만 동일) 멤버를 고정으로
박아두면 재편 시 통째로 틀어진다. as-of 로 읽으면 팀원이 바뀌어도 새 멤버가
자동으로 대상이 되고, 이동 중인 사람이 누락되지 않는다. 서열도 같은 이유로
그 시점 멤버에서 매번 계산한다.

## 결원

담당자가 그날 콜을 설 수 없으면(휴가·교육 등으로 근무 자체가 없거나, 콜 불가일로
등록했거나) 대체자를 찾는다. 실측 7건 중 6건이 **같은 서열의 타 팀** 이었다.

  8/10 윤보라 t4-1 → 김영민 t3-1 · 8/11~13 → 한승윤 t2-1
  8/22~23 민희원 t1-2 → 성해인 t2-2 · 8/28~30 성해인 t2-2 → 민희원 t1-2
  7/22 김영민 t3-1 → 하재욱 t1-1 · 7/27 한승윤 t2-1 → 김영민 t3-1

어긋난 1건(8/21 고수연 t1-4 → 성해인 t2-2)은 성해인이 8/21~23 을 **연속으로** 선
사례라, 이미 그 주에 대체 중인 사람을 먼저 쓰는 규칙(꼬리물기)이 서열보다 앞선다.

아무도 못 찾으면 **비워두고 결원으로 보고**한다. 자동으로 아무나 채우지 않는다 —
윤보라가 8/10~13 에 D1 근무를 하면서 콜만 빠진 것처럼 데이터에 사유가 없는 경우가
있고, 그때 임의로 채우면 병원 의도와 어긋난다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Iterable, Optional

#: ★★ 기본값(폴백)을 두지 않는다 — **콜 코드 맵이 비어 있으면 그 그룹은 콜 미사용**이다.
#:
#: `shifts.call_base_id` 등록 여부가 곧 "콜 부여 사용 여부" 이고, 별도 on/off 플래그가
#: 필요 없다. 여기에 `{"D1":"D1콜","O":"오프콜"}` 같은 기본값을 두면 **등록하지 않은
#: 병동까지 콜이 붙는다** — 모든 병동에 `O` 코드가 있기 때문이다.
#: 2026-08-18 실측(office 102243): 15개 그룹 중 `오프콜` 코드를 가진 곳은 수술실뿐이고
#: 나머지 **14개 그룹은 `O` 만 있어**, 폴백이 걸리면 존재하지 않는 코드로 OFF 가
#: 덮인다. 무결성이 조용히 깨지는 경로라 폴백 자체를 두지 않는다.


def load_call_code_map(db, group_id: str) -> dict[str, str]:
    """`shifts.call_base_id` 로 이어진 관계를 `{기반 근무코드: 콜 코드}` 로 푼다.

    등록 예(수술실) — `D1콜.call_base_id = D1.id` · `오프콜.call_base_id = O.id`
    → `{"D1": "D1콜", "O": "오프콜"}`

    ★ 왜 `shift_id`(문자열)가 아니라 `id`(안정 키)로 잇는가
      `shifts.shift_id` 는 표기라서 바뀐다(레거시 ASP 코드 → 표준코드 교체 전례).
      그래서 이 코드베이스는 값을 참조할 때 안정 키를 병행하는 관습이 이미 있다 —
      `schedule_entries.id` · `nurse_shift_requests.shifts_table_id` ·
      `fixed_wanted_entries.shifts_table_id` 가 모두 `shifts.id (stable key)` 다.
      콜 관계도 같은 방식으로 잇는다.

    ★ `default_shift` / `shift_gb` 를 재사용하지 않는 이유
      둘 다 엔진 핵심 로직이 이미 쓰고 있다. `default_shift` 는
      `roster_create_service.py:87` 이 `∉{D,E,N,O,주}` 를 **이상 코드로 탐지**하고,
      전월 꼬리 판정(:1851)·병동이동 코드 매핑(:3308)도 이 값을 읽는다.
      `shift_gb` 는 `WORK_SHIFT_GB_TO_MAIN`(replacement_recommend_service.py:291)
      매핑에 걸려 모르는 값이면 폴백으로 떨어진다. 콜 관계를 얹으면 조용히 오작동한다.

    ★ ORM 모델(`Shift`)에 컬럼을 올리지 않고 raw SQL 로 읽는다
      DDL 이 환경마다 시차를 두고 들어간다(2026-08-18 실측: dev 에만 있고 prod 엔 없었다).
      모델에 올리면 SQLAlchemy 가 모든 `shifts` SELECT 에 그 컬럼을 끼워 넣어,
      컬럼이 아직 없는 환경에서는 **근무표 조회가 통째로 깨진다**.
      존재를 먼저 확인하고 없으면 빈 dict 로 폴백하게 두면 코드를 먼저 배포하고
      DDL 을 나중에 넣어도 안전하다.
    """
    from sqlalchemy import text  # 지역 import — 이 모듈은 순수 계산으로도 쓰인다

    has_col = db.execute(text(
        "SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_NAME = 'shifts' AND COLUMN_NAME = 'call_base_id'"
    )).first()
    if not has_col:
        return {}

    rows = db.execute(text(
        "SELECT c.shift_id AS call_code, b.shift_id AS base_code "
        "  FROM shifts c JOIN shifts b "
        "    ON b.id = c.call_base_id AND b.group_id = c.group_id "  # 같은 그룹 안에서만
        " WHERE c.group_id = :g AND c.call_base_id IS NOT NULL"
    ), {"g": group_id}).fetchall()
    return {str(base).strip(): str(call).strip() for call, base in rows}


@dataclass(frozen=True)
class OncallMember:
    """어느 주 시점의 팀 소속 1인."""

    nurse_id: str
    name: str
    team_id: int
    #: 그 시점 팀 안에서의 서열(1=최선임). `sequence` 오름차순.
    rank: int


@dataclass
class OncallCell:
    """콜로 갈아끼울 셀 한 칸."""

    nurse_id: str
    day: int
    before: str
    after: str
    team_id: int
    #: 담당 팀원이면 False, 타 팀에서 메운 자리면 True
    substitute: bool = False


@dataclass
class OncallVacancy:
    """채우지 못한 자리."""

    day: int
    team_id: int
    rank: int
    nurse_id: str
    name: str
    reason: str


@dataclass
class OncallResult:
    cells: list[OncallCell] = field(default_factory=list)
    vacancies: list[OncallVacancy] = field(default_factory=list)
    #: 주 시작일(월) → 담당 팀
    weeks: dict[date, int] = field(default_factory=dict)


def week_start(d: date) -> date:
    """그 날짜가 속한 주의 월요일."""
    return d - timedelta(days=d.weekday())


def team_of_week(monday: date, anchor: date, team_order: list[int]) -> int:
    """주 시작일로 담당 팀을 구한다.

    앵커 주를 `team_order[0]` 으로 두고 이후 주마다 한 칸씩 민다.
    앵커보다 과거여도 파이썬의 `%` 가 음수에서도 양수를 돌려주므로 그대로 성립한다.
    """
    weeks = (monday - week_start(anchor)).days // 7
    return team_order[weeks % len(team_order)]


def _rank_members(members: Iterable[OncallMember]) -> list[OncallMember]:
    return sorted(members, key=lambda m: m.rank)


def assign_oncall(
    *,
    year: int,
    month: int,
    roster: dict[str, dict[int, str]],
    members_asof: "callable",
    anchor: date,
    team_order: list[int],
    unavailable: Optional[dict[str, set[int]]] = None,
    max_per_month: Optional[dict[str, int]] = None,
    call_code_map: Optional[dict[str, str]] = None,
) -> OncallResult:
    """한 달치 콜을 배정한다.

    인자
    - roster: `{nurse_id: {day: 근무코드}}` — 고정근무 전개 결과
    - members_asof: `(monday: date) -> list[OncallMember]` — 그 주 시점 전체 팀 멤버.
      팀 미배정자는 애초에 빼서 넘긴다(콜 제외 대상).
    - anchor / team_order: 로테이션 기준. 예) `date(2026,7,27)`, `[2,3,4,1]`
    - unavailable: `{nurse_id: {day,...}}` 개인이 등록한 콜 불가일
    - max_per_month: `{nurse_id: n}` 개인별 월 콜 상한(상시 부분 면제용)

    반환
    - `OncallResult` — 갈아끼울 셀과 채우지 못한 자리
    """
    unavailable = unavailable or {}
    max_per_month = max_per_month or {}
    #: 이 표에 없는 코드(휴가·교육 등)는 콜을 설 수 없다 = 결원 판정 근거.
    code_map = call_code_map or {}
    result = OncallResult()
    #: ★ 콜 코드가 등록돼 있지 않으면 그 그룹은 콜을 쓰지 않는다 — 즉시 빈 결과.
    #:   폴백으로 아무 코드나 끼워 넣으면 미사용 병동의 OFF 가 덮인다.
    if not code_map:
        return result
    valid_teams = set(team_order)

    def _members(monday: date) -> list[OncallMember]:
        """★ 팀 미배정자는 **절대** 온콜 대상이 아니다.

        호출자가 걸러 넘기는 것에만 의존하지 않는다 — 대체자 탐색의 마지막 폴백이
        `others` 전체를 훑기 때문에, 한 명이라도 새어 들어오면 미배정자가 남의 팀
        결원을 메우게 된다. 로테이션에 속하지 않는 팀 번호는 여기서 잘라낸다.
        (실측: 수술실 강민정·이아현·한주영·이선미 4명은 team_period 가 없고
         7·8월 콜 명단에도 한 번도 등장하지 않는다.)
        """
        return [m for m in members_asof(monday)
                if m.team_id is not None and m.team_id in valid_teams]

    import calendar

    days_in_month = calendar.monthrange(year, month)[1]
    used: dict[str, int] = {}          # 누적 콜 횟수 — 쏠림 방지·형평 정렬용
    assigned_days: dict[int, set[str]] = {}   # day → 이미 콜인 사람(중복 배정 차단)

    def can_take(nid: str, day: int) -> Optional[str]:
        """그날 콜을 설 수 있으면 갈아끼울 코드를, 못 서면 None."""
        if day in unavailable.get(nid, ()):
            return None
        if nid in assigned_days.get(day, ()):
            return None
        cap = max_per_month.get(nid)
        if cap is not None and used.get(nid, 0) >= cap:
            return None
        return code_map.get((roster.get(nid) or {}).get(day, ""))

    # 주 단위로 순회 — 그 달에 걸치는 모든 주(월~일)를 본다.
    d = date(year, month, 1)
    last = date(year, month, days_in_month)
    monday = week_start(d)
    #: 그 주에 이미 대체로 들어온 사람 — 꼬리물기에서 최우선으로 재사용한다.
    while monday <= last:
        team_id = team_of_week(monday, anchor, team_order)
        result.weeks[monday] = team_id
        roster_members = _rank_members(_members(monday))
        on_duty = [m for m in roster_members if m.team_id == team_id]
        others = [m for m in roster_members if m.team_id != team_id]
        helpers_this_week: list[str] = []

        for offset in range(7):
            cur = monday + timedelta(days=offset)
            if cur.month != month or cur.year != year:
                continue
            day = cur.day
            for m in on_duty:
                code = can_take(m.nurse_id, day)
                if code is not None:
                    result.cells.append(OncallCell(
                        nurse_id=m.nurse_id, day=day,
                        before=roster[m.nurse_id][day], after=code, team_id=team_id,
                    ))
                    used[m.nurse_id] = used.get(m.nurse_id, 0) + 1
                    assigned_days.setdefault(day, set()).add(m.nurse_id)
                    continue

                # ── 결원 → 대체자 탐색 ──
                sub = _find_substitute(
                    rank=m.rank, day=day, others=others,
                    helpers_this_week=helpers_this_week,
                    can_take=can_take, used=used,
                )
                if sub is None:
                    result.vacancies.append(OncallVacancy(
                        day=day, team_id=team_id, rank=m.rank,
                        nurse_id=m.nurse_id, name=m.name,
                        reason=_vacancy_reason(m.nurse_id, day, roster, unavailable,
                                               max_per_month, used, code_map),
                    ))
                    continue
                code = can_take(sub.nurse_id, day)
                result.cells.append(OncallCell(
                    nurse_id=sub.nurse_id, day=day,
                    before=roster[sub.nurse_id][day], after=code,
                    team_id=team_id, substitute=True,
                ))
                used[sub.nurse_id] = used.get(sub.nurse_id, 0) + 1
                assigned_days.setdefault(day, set()).add(sub.nurse_id)
                if sub.nurse_id not in helpers_this_week:
                    helpers_this_week.append(sub.nurse_id)

        monday += timedelta(days=7)

    return result


def _find_substitute(
    *, rank: int, day: int, others: list[OncallMember],
    helpers_this_week: list[str], can_take, used: dict[str, int],
) -> Optional[OncallMember]:
    """결원 자리를 메울 사람. 꼬리물기 → 같은 서열 → 형평 순.

    ★ 꼬리물기가 서열보다 먼저다 — 실측 8/21 에서 성해인(t2-2)이 고수연(t1-4) 자리를
      메운 건 서열이 맞아서가 아니라 8/21~23 을 연속으로 섰기 때문이다.
    """
    by_id = {m.nurse_id: m for m in others}

    # ① 이 주에 이미 대체 중인 사람이 계속 가능하면 그대로 이어간다
    for nid in helpers_this_week:
        m = by_id.get(nid)
        if m is not None and can_take(nid, day) is not None:
            return m

    # ② 같은 서열의 타 팀 — 동률이면 누적 콜이 적은 사람
    same_rank = [m for m in others if m.rank == rank and can_take(m.nurse_id, day) is not None]
    if same_rank:
        return min(same_rank, key=lambda m: (used.get(m.nurse_id, 0), m.nurse_id))

    # ③ 서열이 맞는 사람이 없으면 서열 차가 작은 순 → 누적 콜 적은 순
    pool = [m for m in others if can_take(m.nurse_id, day) is not None]
    if not pool:
        return None
    return min(pool, key=lambda m: (abs(m.rank - rank), used.get(m.nurse_id, 0), m.nurse_id))


def _vacancy_reason(
    nid: str, day: int, roster: dict[str, dict[int, str]],
    unavailable: dict[str, set[int]], max_per_month: dict[str, int], used: dict[str, int],
    code_map: dict[str, str],
) -> str:
    if day in unavailable.get(nid, ()):
        return "콜 불가일 등록"
    cap = max_per_month.get(nid)
    if cap is not None and used.get(nid, 0) >= cap:
        return f"월 상한 {cap}회 도달"
    code = (roster.get(nid) or {}).get(day)
    if code is None:
        return "근무표에 해당 일자 없음"
    if code not in code_map:
        return f"콜 불가 근무({code})"
    return "대체자 없음"
