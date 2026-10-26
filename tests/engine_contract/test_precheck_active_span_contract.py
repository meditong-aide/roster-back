"""EI-10f 활동 구간 계약 — precheck 의 근무 가능 구간은 엔진과 같다(결정 D8 · 설계서 §2.6).

엔진은 같은 규칙을 네 곳에서 쓴다. 마지막 근무일은 `resignation_date − 1`(퇴사일 그날부터 소속이 아니다)이고, 그 달과
겹치지 않으면(마지막 근무일이 월초 전 · 입사일이 월말 뒤) 근무가 없다.
  (a) `cp_sat_basic.py:2585-2611` — primary 모델의 `join[]`/`leave[]`        (이 파일에 그대로 옮겨 적은 오라클)
  (b) `fallback_lex.py:716-734`  — 폴백 모델의 `join[]`/`leave[]`            (옮겨 적은 오라클)
  (c) `roster_create_service._active_range_in_month` — 엔진 명단 제외 · 주휴 맵 거르기에 쓰는 생산자 쪽 실제 함수
  (d) `cp_sat.feasibility_alerts._active_range`      — precheck 가 그대로 불러 쓰는 실제 함수
계약: 날짜 후보 10개(없음 · 아주 먼 과거 · 월초 전후 · 월중 · 월말 전후 · 먼 미래)의 입사일 × 퇴사일 모든 조합에서 precheck 의
`(join_day, leave_day)` 가 네 구현이 말하는 '근무하는 날의 집합'과 **같다**. 근무일이 없으면 정확히 `(1, 0)`(엔진 표기,
`cp_sat_basic.py:2597`)이다. 엔진이 규칙을 바꿔 네 구현이 서로 갈리면 이 테스트가 먼저 빨개진다.
"""

from __future__ import annotations

import calendar
import itertools
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from services.cp_sat.feasibility_alerts import _active_range
from services.precheck.runtime_bridge import build_precheck_input
from services.roster_create_service import _active_range_in_month

#: 30일 · 31일 · 평년 2월(28일) · 윤년 2월(29일) — 월말·연도 경계가 다른 달.
MONTHS = [(2026, 11), (2026, 12), (2027, 2), (2028, 2)]
EMPTY = (1, 0)


def _candidates(year: int, month: int) -> list[date | None]:
    """입사일·퇴사일 후보 — 없음 + 월 경계 앞뒤로 촘촘히 + 먼 과거/미래."""
    days = calendar.monthrange(year, month)[1]
    first, last = date(year, month, 1), date(year, month, days)
    return [None, first - timedelta(days=400), first - timedelta(days=1), first, first + timedelta(days=1),
            first + timedelta(days=days // 2), last - timedelta(days=1), last, last + timedelta(days=1),
            last + timedelta(days=40)]


def _dt(value: date | None) -> datetime | None:
    """DB 칸(DATETIME)과 같은 모양 — 자정 datetime."""
    return datetime(value.year, value.month, value.day) if value else None


def _csb_span(first: date, days: int, joining: date | None, resignation: date | None) -> tuple[int, int]:
    """`cp_sat_basic.py:2585-2611` 를 그대로 옮긴 (join, leave) — 근무일이 없으면 join > leave."""
    last_phys = first + timedelta(days=days - 1)
    join = (joining - first).days if joining else 0
    if resignation:
        last_work = resignation - timedelta(days=1)
        if last_work < first:
            return 1, 0
        leave = days - 1 if last_work > last_phys else (last_work - first).days
    else:
        leave = days - 1
    return max(join, 0), min(leave, days - 1)


def _lex_span(first: date, days: int, joining: date | None, resignation: date | None) -> tuple[int, int]:
    """`fallback_lex.py:716-734` 를 그대로 옮긴 (join, leave)."""
    last_day = first + timedelta(days=days - 1)
    join = (joining - first).days if joining else 0
    if resignation:
        last_work = resignation - timedelta(days=1)
        if last_work < first:
            return 1, 0
        leave = (last_work - first).days
        if last_work > last_day:
            leave = days - 1
    else:
        leave = days - 1
    return max(join, 0), min(leave, days - 1)


def _as_days(span: tuple[int, int] | None) -> frozenset[int]:
    """구간 → 근무하는 날의 집합(빈 구간 · None 은 공집합)."""
    return frozenset() if span is None else frozenset(range(span[0], span[1] + 1))


def _engine_spans(year: int, month: int, joining: date | None, resignation: date | None) -> dict[str, frozenset[int]]:
    """엔진 네 구현이 말하는 근무하는 날의 집합."""
    days = calendar.monthrange(year, month)[1]
    first, last = date(year, month, 1), date(year, month, days)
    producer = _active_range_in_month(
        SimpleNamespace(joining_date=_dt(joining), resignation_date=_dt(resignation)), first, days)
    alerts = _active_range(SimpleNamespace(joining_date=joining, resignation_date=resignation), first, last)
    return {"cp_sat_basic": _as_days(_csb_span(first, days, joining, resignation)),
            "fallback_lex": _as_days(_lex_span(first, days, joining, resignation)),
            "rcs._active_range_in_month": _as_days(producer),
            "feasibility_alerts._active_range": _as_days(alerts)}


def _precheck_span(year: int, month: int, joining, resignation) -> tuple[int, int]:
    nurse = {"nurse_id": "n0", "grade": 1, "allowed_shifts": [], "joining_date": joining, "resignation_date": resignation}
    inp = build_precheck_input(nurses_dict=[nurse], config_dict={"daily_shift_requirements": {"D": 1}},
                               grade_config=None, fixed_cells=None, year=year, month=month)
    return inp.nurses[0].join_day, inp.nurses[0].leave_day


@pytest.mark.parametrize("year, month", MONTHS)
def test_precheck_span_equals_all_four_engine_implementations(year, month):
    """모든 입사일 × 퇴사일 조합에서 precheck 구간 = 엔진 네 구현의 근무일 집합, 빈 구간이면 정확히 `(1, 0)`."""
    mismatches = []
    for joining, resignation in itertools.product(_candidates(year, month), repeat=2):
        span = _precheck_span(year, month, _dt(joining), _dt(resignation))
        for name, expected in _engine_spans(year, month, joining, resignation).items():
            if _as_days(span) != expected:
                mismatches.append((joining, resignation, name, span, sorted(expected)[:3]))
        if not _as_days(span) and span != EMPTY:
            mismatches.append((joining, resignation, "빈 구간 표기", span, EMPTY))
    assert not mismatches, mismatches[:5]


def _iso(value: date) -> str:
    return value.isoformat()


def _iso_datetime(value: date) -> str:
    return f"{value.isoformat()}T09:30:00"


@pytest.mark.parametrize("convert", [lambda d: d, _iso, _iso_datetime], ids=["date", "iso", "iso_datetime"])
def test_every_input_form_gives_the_same_span(convert):
    """생성 경로는 DB datetime, 직접 호출·스냅샷은 date·ISO 문자열이다 — 어느 모양이든 같은 구간이다."""
    for joining, resignation in itertools.product(_candidates(2026, 11), repeat=2):
        expected = _precheck_span(2026, 11, _dt(joining), _dt(resignation))
        got = _precheck_span(2026, 11, joining and convert(joining), resignation and convert(resignation))
        assert got == expected, (joining, resignation, got, expected)


def test_known_engine_boundaries_are_pinned():
    """엔진 경계 값을 손으로 못 박는다(2026-11) — 위 전수 대조가 잘못된 오라클로 통과하는 일을 막는 눈금."""
    pinned = {
        (None, datetime(2026, 11, 20)): (0, 18), (None, datetime(2026, 11, 30)): (0, 28),
        (None, datetime(2026, 11, 2)): (0, 0), (None, datetime(2026, 11, 1)): EMPTY,
        (None, datetime(2026, 12, 1)): (0, 29), (None, datetime(2026, 10, 31)): EMPTY,
        (datetime(2026, 11, 10), None): (9, 29), (datetime(2026, 12, 1), None): EMPTY,
        (datetime(2026, 11, 5), datetime(2026, 11, 20)): (4, 18), (datetime(2026, 11, 20), datetime(2026, 11, 10)): EMPTY,
    }
    for (joining, resignation), expected in pinned.items():
        assert _precheck_span(2026, 11, joining, resignation) == expected, (joining, resignation)
