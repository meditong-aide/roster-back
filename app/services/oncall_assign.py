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
    protected: Optional[dict[str, set[int]]] = None,
    carried_helped: Optional[dict[str, set[str]]] = None,
) -> OncallResult:
    """한 달치 콜을 배정한다.

    인자
    - roster: `{nurse_id: {day: 근무코드}}` — 고정근무 전개 결과
    - members_asof: `(monday: date) -> list[OncallMember]` — 그 주 시점 전체 팀 멤버.
      팀 미배정자는 애초에 빼서 넘긴다(콜 제외 대상).
    - anchor / team_order: 로테이션 기준. 예) `date(2026,7,27)`, `[2,3,4,1]`
    - unavailable: `{nurse_id: {day,...}}` 개인이 등록한 콜 불가일
    - max_per_month: `{nurse_id: n}` 개인별 월 콜 상한(상시 부분 면제용)
    - protected: `{nurse_id: {day,...}}` 확정 원티드로 굳힌 날. **대체로는 못 쓴다**
      (담당주는 그대로 선다 — 주말 `O` → `오프콜` 은 정상 로테이션이다)

    반환
    - `OncallResult` — 갈아끼울 셀과 채우지 못한 자리
    """
    unavailable = unavailable or {}
    max_per_month = max_per_month or {}
    #: ★ 확정 원티드로 굳힌 날은 **남의 주간에 끌려올 때만** 막는다.
    #:   실측 2026-09: 하재욱(t1)이 team4 주간 9/12~13 대체로 투입되면서 본인이
    #:   확정한 `O` 가 `오프콜` 로 덮였다. 담당주라면 정상이지만 대체는 아니다.
    protected = protected or {}
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

    # ── 결원 예정 일수 사전 스캔 ──
    # ★ 자기 담당주에서 나중에 빠질 사람을 **앞선 주간의 대체로 먼저 쓴다.**
    #   실측 8월: 성해인이 8/28~30(휴PM·OFF)에 빠질 예정이라 8/21~23 team1 주간을
    #   먼저 도왔고, 결과적으로 총량이 담당주 일수와 같아졌다(9일). 미리 갚는 구조다.
    own_days = _scan_own_days(
        year=year, month=month, days_in_month=days_in_month,
        members_asof=_members, anchor=anchor, team_order=team_order,
        can_take=can_take, protected=protected,
    )
    debt = _scan_future_gaps(
        year=year, month=month, days_in_month=days_in_month, roster=roster,
        members_asof=_members, anchor=anchor, team_order=team_order,
        code_map=code_map, unavailable=unavailable, max_per_month=max_per_month,
    )

    #: 품앗이 장부 `{도운 사람: {대신 서준 결원자, ...}}`.
    #: 실측 2026-08 에 성해인이 8/21~23 민희원 자리를 받고, 민희원이 8/28~30 성해인
    #: 자리를 갚았다. 다른 어떤 기준(담당주 인접·간격·누적)으로도 민희원이 뽑히지
    #: 않는다 — 갚는다는 사실 자체가 선택의 이유다.
    #: ★ **월 경계를 넘긴다.** 달 안에서 못 갚은 빚이 남기 때문이다 — 실측 2026-08
    #:   윤보라가 김영민·한승윤에게 8/10~13 을 대신 서게 하고 그 달에 갚지 못했다.
    #:   `carried_helped` 로 직전 달 장부를 받아 9월 첫 배정부터 반영한다.
    helped: dict[str, set[str]] = {k: set(v) for k, v in (carried_helped or {}).items()}

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

        # ── ① 담당 팀 배정 + 결원 수집 ──
        week_days = [(monday + timedelta(days=o)).day for o in range(7)
                     if (monday + timedelta(days=o)).month == month
                     and (monday + timedelta(days=o)).year == year]

        # ★ 담당주가 **2일 이상 끊기면 그 뒤를 통째로 넘긴다.**
        #   콜은 한 사람이 이어서 서는 것이 원칙이라, 중간에 이틀 넘게 비면 그 주는
        #   대체자에게 맡기고 담당자는 복귀하지 않는다.
        #   실측과 맞춘 예외 두 가지 —
        #     **1일 금지는 끊김이 아니다.** 그날 하루만 콜을 못 서는 사정이지 담당주를
        #       놓은 것이 아니다: 고수연 8/17~20 콜 → 8/21 노조교육(그날만 불가) →
        #       8/22~23 복귀.
        #     **확정 원티드 면제도 끊김이 아니다.** "그날 안 선다" 는 의사표시일 뿐이다:
        #       윤보라 8/10~13 원티드 → 8/14~16 복귀.
        handover: dict[str, set[int]] = {}
        for m in on_duty:
            blocked = [d for d in week_days if can_take(m.nurse_id, d) is None]
            for run in _group_consecutive(sorted(blocked)):
                if len(run) < 2:
                    continue
                tail = week_days[week_days.index(run[-1]) + 1:]
                if tail:
                    handover[m.nurse_id] = set(tail)
                break

        vacant: dict[int, list[OncallMember]] = {}
        for offset in range(7):
            cur = monday + timedelta(days=offset)
            if cur.month != month or cur.year != year:
                continue
            day = cur.day
            for m in on_duty:
                code = can_take(m.nurse_id, day)
                if code is not None and day in handover.get(m.nurse_id, ()):
                    code = None            # 2일 이상 끊긴 뒤 — 그 주는 넘긴다
                # ★ 담당주 중간에 끊겨도 **다시 설 수 있으면 복귀시킨다.** 실측 2026-08
                #   고수연이 8/17~20 콜 → 8/21 노조교육 → 8/22~23 복귀로 담당주를 쪼갰다.
                #   끊긴 뒤를 통째로 넘긴 민희원·성해인은 주 끝에 걸린 경우이거나
                #   품앗이(helped)로 설명되는 자리다.
                #   ★ 특히 오프콜은 실제 근무가 없는 대기라(휴일 콜) 며칠 끊겼다가
                #     돌아와도 부담이 다르다 — 복귀를 막지 않는다.
                # ★ 확정 원티드로 굳힌 근무는 **담당주라도** 콜로 덮지 않는다.
                #   2026-08 윤보라가 담당주 8/10~13 을 D1 으로만 서고 콜은 임종엽·
                #   채다솔·윤상준이 나눠 받았다 — 근무를 못 서서 빠진 게 아니라
                #   콜만 면제된 자리다. 원티드를 존중하면 그 형태가 그대로 나온다.
                if code is not None and day in protected.get(m.nurse_id, ()):
                    code = None
                if code is None:
                    vacant.setdefault(day, []).append(m)
                    continue
                result.cells.append(OncallCell(
                    nurse_id=m.nurse_id, day=day,
                    before=roster[m.nurse_id][day], after=code, team_id=team_id,
                ))
                used[m.nurse_id] = used.get(m.nurse_id, 0) + 1
                assigned_days.setdefault(day, set()).add(m.nurse_id)

        # ── ② 결원을 인접 블록으로 묶어 한 사람에게 통째로 맡긴다 ──
        # ★ 날짜별로 빈자리를 메우면 하루짜리 콜이 튀어나온다. 실측(2026-08)은
        #   블록 단위였다 — 8/21 고수연(t1-4) 하루 결원 + 8/22~23 민희원(t1-2) 이틀
        #   결원을 **성해인 한 명이 8/21~23 사흘 연속**으로 받았다. 서열도 안 맞는다.
        #   즉 병원은 "자리별 대체" 가 아니라 "블록 단위 투입" 으로 처리한다.
        # ★ 결원을 **역할(rank)별로** 나눠 채운다. 날짜로만 묶으면 서로 다른 역할의
        #   결원이 한 덩어리가 되어 조각이 뒤섞인다 — 실측 2026-09: rank1(상한 초과)
        #   9/10~13 과 rank3(제약·휴가) 9/10~11 이 [10,11,12,13] 한 블록으로 합쳐지면서
        #   고수진이 9/10 하루만 받는 고립 대체가 나왔다.
        #   rank 는 순번이 아니라 역할(마취/소독/순환/전담)이라 자리마다 따로 메운다.
        for rank in sorted({m.rank for ms in vacant.values() for m in ms}):
            rank_vacant = {d: [m for m in ms if m.rank == rank]
                           for d, ms in vacant.items()}
            rank_vacant = {d: ms for d, ms in rank_vacant.items() if ms}
            for block in _group_consecutive(sorted(rank_vacant)):
                _fill_block(
                    block=block, vacant=rank_vacant, others=others, team_id=team_id,
                    roster=roster, can_take=can_take, used=used,
                    assigned_days=assigned_days, helpers=helpers_this_week,
                    result=result, unavailable=unavailable, helped=helped,
                    max_per_month=max_per_month, code_map=code_map,
                    protected=protected, debt=debt, own_days=own_days,
                )

        monday += timedelta(days=7)

    return result


#: 담당주와 **떨어진** 대체에서 한 사람이 연속으로 설 수 있는 최대 일수.
#: 실측: 한승윤 8/11~13(3일) · 민희원 8/28~30(3일) — 담당주와 붙지 않은 대체는 3일까지.
MAX_SUB_RUN = 3

#: 담당주에 **이어 붙일 때** 허용되는 연속 콜 총 길이.
#: 실측 2026-08 — 김영민이 담당주 8/03~09(7일)에 8/10 하루를 꼬리로 붙여 **8일 연속**을
#: 채웠고, 거기서 끊겨 8/11~13 은 한승윤에게 넘어갔다. 성해인도 8/21~27 로 7일 연속이다.
#: 즉 자기 주에 붙는 연장은 3일 제한이 아니라 이 총량이 상한이다.
MAX_CALL_RUN = 8


def _run_length_with(
    assigned_days: dict[int, set[str]], nid: str, days: list[int],
    future_own: set[int] | None = None,
) -> int:
    """`days` 를 추가로 맡았을 때 그 사람의 **연속 콜 최대 길이**.

    ★ `future_own` = 앞으로 설 자기 담당주 날짜. 이걸 넣어야 "담당주 직전 블록을
      받아 담당주와 이어지는" 경우를 미리 잡는다(실측 2026-09 고수연 11일 연속).
    """
    mine = ({d for d, who in assigned_days.items() if nid in who}
            | set(days) | set(future_own or ()))
    if not mine:
        return 0
    best = run = 1
    prev = None
    for d in sorted(mine):
        run = run + 1 if prev is not None and d == prev + 1 else 1
        best = max(best, run)
        prev = d
    return best


def _group_consecutive(days: list[int]) -> list[list[int]]:
    """연속된 날짜를 하나의 블록으로 묶는다. `[21,22,23,28]` → `[[21,22,23],[28]]`"""
    blocks: list[list[int]] = []
    for d in days:
        if blocks and d == blocks[-1][-1] + 1:
            blocks[-1].append(d)
        else:
            blocks.append([d])
    return blocks


def _scan_own_days(
    *, year: int, month: int, days_in_month: int, members_asof,
    anchor: date, team_order: list[int], can_take, protected: dict[str, set[int]],
) -> dict[str, set[int]]:
    """각자 자기 팀 담당일 중 **실제로 설 수 있는 날**. 연속 길이 상한 계산용.

    ★ 담당주라도 근무가 콜 불가이거나 확정 원티드로 굳힌 날은 서지 않는다. 이걸
      빼지 않으면 연속 길이를 과대평가해 대체 투입이 잘린다 — 실측 2026-08 성해인은
      담당주 8/24~30 중 28~30 이 원티드로 빠지는데, 그걸 세는 바람에 8/21~23 이
      10일 연속으로 계산돼 8/23 하루만 받았다(실제로는 8/21~27 = 7일).
    """
    out: dict[str, set[int]] = {}
    monday = week_start(date(year, month, 1))
    last = date(year, month, days_in_month)
    while monday <= last:
        team_id = team_of_week(monday, anchor, team_order)
        for m in members_asof(monday):
            if m.team_id != team_id:
                continue
            for off in range(7):
                cur = monday + timedelta(days=off)
                if cur.month != month or cur.year != year:
                    continue
                if can_take(m.nurse_id, cur.day) is None:
                    continue
                if cur.day in protected.get(m.nurse_id, ()):
                    continue
                out.setdefault(m.nurse_id, set()).add(cur.day)
        monday += timedelta(days=7)
    return out


def _scan_future_gaps(
    *, year: int, month: int, days_in_month: int, roster, members_asof,
    anchor: date, team_order: list[int], code_map: dict[str, str],
    unavailable: dict, max_per_month: dict,
) -> dict[str, int]:
    """각자 **자기 담당주에서 못 설 날이 며칠인지** 미리 센다.

    배정 전에 계산하므로 `used`(누적) 는 못 보고, 근무 코드·불가일만 본다.
    상한(`max_per_month`)으로 빠질 몫도 더한다 — 담당주 일수가 상한을 넘으면
    그 차이만큼은 확정적으로 빈다.
    """
    gaps: dict[str, int] = {}
    own_days: dict[str, int] = {}
    monday = week_start(date(year, month, 1))
    last = date(year, month, days_in_month)
    while monday <= last:
        team_id = team_of_week(monday, anchor, team_order)
        for m in members_asof(monday):
            if m.team_id != team_id:
                continue
            for off in range(7):
                cur = monday + timedelta(days=off)
                if cur.month != month or cur.year != year:
                    continue
                own_days[m.nurse_id] = own_days.get(m.nurse_id, 0) + 1
                blocked = (cur.day in unavailable.get(m.nurse_id, ())
                           or code_map.get((roster.get(m.nurse_id) or {}).get(cur.day, "")) is None)
                if blocked:
                    gaps[m.nurse_id] = gaps.get(m.nurse_id, 0) + 1
        monday += timedelta(days=7)
    for nid, cap in max_per_month.items():
        spare = own_days.get(nid, 0) - gaps.get(nid, 0) - cap
        if spare > 0:
            gaps[nid] = gaps.get(nid, 0) + spare
    return gaps


def _fill_block(
    *, block: list[int], vacant: dict[int, list[OncallMember]],
    others: list[OncallMember], team_id: int, roster, can_take, used,
    assigned_days, helpers: list[str], result: OncallResult,
    unavailable, max_per_month, code_map, protected: dict[str, set[int]],
    debt: dict[str, int], own_days: dict[str, set[int]],
    helped: dict[str, set[str]],
) -> None:
    """한 블록의 결원을 대체자에게 맡긴다 — **블록 전체를 덮는 사람이 우선**.

    ★ 이미 그 주에 들어온 대체자(helpers)를 먼저 본다 — 한 사람이 이어서 서는 게
      여러 명이 하루씩 끼어드는 것보다 낫다(실측 8/21~23 성해인).
    ★ 전부는 못 덮어도 되는 만큼 맡기고 남은 날은 다음 후보로 넘긴다
      (실측 8/10~13: 김영민 1일 + 한승윤 3일로 갈렸다).
    """
    # ★ 결원이 **아닌** 날까지 대체자에게 넘기는 "같은 서열 자리 교대" 는 넣지 않았다.
    #   2026-08 에 그렇게 보이는 사례가 있으나(8/21~23 성해인 IN ↔ 민희원 OUT,
    #   8/24~30 민희원 IN ↔ 성해인 OUT) 둘은 **성해인·민희원 1:1 맞교환 한 건**이고,
    #   2026-07 은 대체 자체가 0건이다. 표본 1건으로 규칙을 세우면 결원이 하루뿐인
    #   주에서도 담당팀원을 밀어내 실측 재현율이 91.1%→87.9% 로 떨어진다(실측).
    #   근거가 쌓이기 전까지는 **결원인 날만** 대체한다.
    remaining = {d: list(vacant[d]) for d in block}
    while any(remaining.values()):
        days = [d for d in block if remaining[d]]
        def _free(m, d) -> bool:
            return (can_take(m.nurse_id, d) is not None
                    and d not in protected.get(m.nurse_id, ()))

        # ★ 자기 담당주에 **이어 붙는** 사람은 MAX_CALL_RUN(8일)까지, 떨어진 대체는
        #   MAX_SUB_RUN(3일)까지. 실측 8/10~13 이 김영민 1일(8/3~9 꼬리) + 한승윤 3일로
        #   갈린 것이 이 두 상한의 결과다.
        def _take_span(m) -> list[int]:
            """그 사람이 이번에 받을 수 있는 날들.

            ★ 상한은 **그 사람의 전체 연속 콜 길이**로 잰다 — `own_days` 로 앞으로 설
              담당주까지 포함해야 한다. 이걸 빼먹으면 담당주 직전 블록을 받은 사람이
              담당주와 이어져 11일 연속이 된다(2026-09 고수연에서 실측).
            """
            # ★ 남는 조각이 1일이 되면 **고립 1일 대체**가 생긴다. 실측 2026-08 에서
            #   1일 블록은 김영민 8/10 하나뿐이고 그건 자기 담당주 8/3~9 바로 다음날이라
            #   연속의 연장이었다 — **담당주와 떨어진 1일은 0건**이다.
            #   그래서 4일을 3+1 로 자르지 않고 한 사람이 통째로 받는다. MAX_SUB_RUN 을
            #   한 칸 넘기지만, 고립 1일을 만드는 것보다 실측 형태에 가깝다
            #   (실측 2026-09 생성분에서 고수진·하재욱이 하루씩 끼어들던 자리).
            limit = MAX_SUB_RUN
            if len(days) - limit == 1:
                limit = len(days)      # 3+1 로 갈라 고립을 만들지 말고 통째로 준다
            span = days[-limit:] if len(days) > limit else list(days)
            span = [d for d in span if _free(m, d)]
            while span and (
                _run_length_with(assigned_days, m.nurse_id, span,
                                 own_days.get(m.nurse_id, set())) > MAX_CALL_RUN
                or not _sub_run_ok(m.nurse_id, span, limit)
            ):
                span = span[1:]
            return span

        def _sub_run_ok(nid: str, span: list[int], allow: int = MAX_SUB_RUN) -> bool:
            """담당주와 **떨어진** 대체 구간이 MAX_SUB_RUN 을 넘지 않는지.

            ★ 한 번에 3일씩 자르는 것만으로는 부족하다 — 같은 사람이 다음 블록에서
              또 뽑히면 이어 붙어 5일이 된다(2026-09 한승윤 9~13 실측).
              그래서 **누적 배정까지 합친 연속 구간**으로 판정한다.
            ★ `allow` 는 호출부가 정한 상한이다. 기본은 MAX_SUB_RUN 이지만, 3+1 로 갈라
              고립 1일이 생기는 블록에서는 통째로 주려고 4 가 넘어온다.
            """
            own = own_days.get(nid, set())
            mine = ({d for d, who in assigned_days.items() if nid in who}
                    | set(span) | own)
            for run in _group_consecutive(sorted(mine)):
                if not set(run) & set(span):
                    continue
                if set(run) & own:
                    continue        # 담당주에 이어붙는 연장 — MAX_CALL_RUN 이 상한
                if len(run) > allow:
                    return False
            return True

        cands = [(m, _take_span(m)) for m in others]
        cands = [(m, sp) for m, sp in cands if sp]
        if not cands:
            break
        # ★ 서열(rank)은 순번이 아니라 **역할**이다 — 엑셀 주석 "1일 콜 대기 4명
        #   (마취/소독/순환/전담간호사)". 마취가 빠지면 다른 팀 마취가 와야 한다.
        #   실측 2026-08 대체 3건이 전부 역할 일치다:
        #     윤보라 t4-1 ← 김영민 t3-1 · 한승윤 t2-1
        #     민희원 t1-2 ← 성해인 t2-2 / 성해인 t2-2 ← 민희원 t1-2
        #   같은 역할이 아무도 못 서면 다른 역할로 내려간다(4명 정원이 우선).
        want_ranks = {m.rank for d in days for m in remaining[d]}

        def _adjoins_own(nid: str, span: list[int]) -> bool:
            """그 구간이 자기 담당주에 **바로 이어 붙는가**.

            실측 8/10 은 김영민(t3-1)이 자기 담당주 8/3~9 마지막날 다음을 그대로
            이어받았다 — 남의 주에 새로 끼어드는 것보다 이쪽이 먼저다.
            """
            own = own_days.get(nid, set())
            return bool(own) and (span[0] - 1 in own or span[-1] + 1 in own)

        def _own_gap(nid: str, span: list[int]) -> int:
            """그 구간과 자기 담당주 사이의 최단 간격 — **클수록 좋다**.

            콜은 야간 대기라 대체 직후 담당주가 오면 사실상 연속 대기가 된다.
            실측 8/11~13 이 그 근거다 — 누적 콜이 0회인 하재욱(t1, 담당주까지 4일)
            대신 2회인 한승윤(t2, 9일)을 불렀다. 하재욱을 부르면 3일 쉬고 담당주
            7일이라 회복이 안 된다.
            """
            own = own_days.get(nid, set())
            if not own:
                return 99
            return min(abs(d - o) for d in (span[0], span[-1]) for o in own)

        def _repays(nid: str) -> bool:
            """이번 결원자 중 **나를 대신 서준 사람**이 있는가 — 갚을 차례다."""
            return any(nid in helped.get(v.nurse_id, ())
                       for d in days for v in remaining[d])

        # 역할 → 갚기 → 담당주 꼬리물기 → 그 주 재사용 → 담당주와 먼 순 → debt → 누적
        sub, window = min(
            cands,
            key=lambda t: (t[0].rank not in want_ranks,
                           not _adjoins_own(t[0].nurse_id, t[1]),
                           not _repays(t[0].nurse_id),
                           t[0].nurse_id not in helpers,
                           -_own_gap(t[0].nurse_id, t[1]),
                           -debt.get(t[0].nurse_id, 0),
                           used.get(t[0].nurse_id, 0), t[0].nurse_id),
        )
        took = False
        for d in window:
            code = can_take(sub.nurse_id, d)
            if code is None:
                continue
            if d in protected.get(sub.nurse_id, ()):
                continue                   # 확정 원티드 날 — 대체로 덮지 않는다
            if not remaining[d]:
                continue
            covered = remaining[d].pop(0)
            helped.setdefault(sub.nurse_id, set()).add(covered.nurse_id)
            result.cells.append(OncallCell(
                nurse_id=sub.nurse_id, day=d,
                before=roster[sub.nurse_id][d], after=code,
                team_id=team_id, substitute=True,
            ))
            used[sub.nurse_id] = used.get(sub.nurse_id, 0) + 1
            assigned_days.setdefault(d, set()).add(sub.nurse_id)
            took = True
        if sub.nurse_id not in helpers:
            helpers.append(sub.nurse_id)
        if not took:
            break

    for d, ms in remaining.items():
        for m in ms:
            result.vacancies.append(OncallVacancy(
                day=d, team_id=team_id, rank=m.rank,
                nurse_id=m.nurse_id, name=m.name,
                reason=("확정 원티드(콜 면제)" if d in protected.get(m.nurse_id, ())
                        else _vacancy_reason(m.nurse_id, d, roster, unavailable,
                                             max_per_month, used, code_map)),
            ))


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
