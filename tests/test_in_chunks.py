"""EI-13c-1 — SQL `IN (...)` 목록 300개 단위 끊기(`services.in_chunks`) 시험.

병동(≤300명)은 한 덩어리 = 지금과 같은 한 문장이고, 그보다 큰 목록만 여러 문장으로 나뉜다.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest
from db.models import FixedWantedEntry, Group, Nurse, NurseTeamPeriod, Office, ScheduleEntry
from services.in_chunks import IN_CHUNK_SIZE, in_chunks
from services.nurse_period_resolver import fetch_periods
from services.roster_create_service import _read_fixed_wanted_rows, _read_schedule_entries
from sqlalchemy import event


@pytest.mark.parametrize(
    ("n", "sizes"),
    [(0, []), (1, [1]), (299, [299]), (300, [300]), (301, [300, 1]), (600, [300, 300]), (601, [300, 300, 1])],
)
def test_in_chunks_boundaries(n, sizes):
    items = [f"n{i}" for i in range(n)]
    chunks = list(in_chunks(items))
    assert [len(c) for c in chunks] == sizes
    assert [x for c in chunks for x in c] == items  # 순서 유지·누락 0
    assert IN_CHUNK_SIZE == 300


def test_in_chunks_accepts_iterable_and_size():
    assert list(in_chunks(iter(range(5)), 2)) == [[0, 1], [2, 3], [4]]
    assert list(in_chunks((), 2)) == []


class _Counter:
    """SELECT 문장 수와 문장당 바인드 파라미터 수를 센다."""

    def __init__(self, db):
        self.params: list[int] = []
        self._bind = db.get_bind()

    def _on(self, conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            self.params.append(len(parameters) if parameters else 0)

    def __enter__(self):
        event.listen(self._bind, "before_cursor_execute", self._on)
        return self

    def __exit__(self, *exc):
        event.remove(self._bind, "before_cursor_execute", self._on)


def _ids(n):
    return [f"n{i:04d}" for i in range(n)]


@pytest.fixture
def seeded(db):
    db.add(Office(office_id="o1", office_name="병원"))
    db.add(Group(group_id="A", group_name="A병동", office_id="o1"))
    # 덩어리 경계 양쪽(0·299 | 300 | 649)에 한 명씩
    for nid in ("n0000", "n0299", "n0300", "n0649"):
        db.add(Nurse(nurse_id=nid, account_id=f"acc_{nid}", group_id="A", office_id="o1", name=nid,
                     active=1, team_id=1, allowed_shifts=[]))
    db.flush()
    return db


def test_fetch_periods_650_nurses_three_statements_same_result(seeded):
    db = seeded
    for i, nid in enumerate(("n0000", "n0299", "n0300", "n0649")):
        db.add(NurseTeamPeriod(nurse_id=nid, group_id="A", team_id=i + 1, valid_from=date(2026, 6, 1)))
        db.add(NurseTeamPeriod(nurse_id=nid, group_id="A", team_id=i + 5, valid_from=date(2026, 7, 10)))
    db.flush()
    ids = _ids(650)
    ms, me = date(2026, 7, 1), date(2026, 8, 1)
    with _Counter(db) as c:
        got = fetch_periods(db, NurseTeamPeriod, ids, ms, me, group_id="A")
    assert len(c.params) == 3
    assert all(p <= IN_CHUNK_SIZE + 4 for p in c.params)  # IN 300 + 월·그룹 고정 파라미터
    # 기대값: 같은 입력을 덩어리로 직접 세 번 읽어 합친 것(간호사별 valid_from 오름차순)
    expect: dict[str, list] = {}
    for lo in range(0, 650, 300):
        for r in (db.query(NurseTeamPeriod).filter(NurseTeamPeriod.nurse_id.in_(ids[lo:lo + 300]))
                  .order_by(NurseTeamPeriod.valid_from.asc()).all()):
            expect.setdefault(r.nurse_id, []).append(r)
    assert {k: [r.id for r in v] for k, v in got.items()} == {k: [r.id for r in v] for k, v in expect.items()}
    assert set(got) == {"n0000", "n0299", "n0300", "n0649"}
    assert [r.team_id for r in got["n0300"]] == [3, 7]


def test_fetch_periods_300_nurses_single_statement_and_empty_zero(seeded):
    db = seeded
    ms, me = date(2026, 7, 1), date(2026, 8, 1)
    with _Counter(db) as c:
        fetch_periods(db, NurseTeamPeriod, _ids(300), ms, me)
    assert len(c.params) == 1
    with _Counter(db) as c:
        assert fetch_periods(db, NurseTeamPeriod, [], ms, me) == {}
    assert c.params == []


def test_read_fixed_wanted_rows_650_three_statements(seeded):
    db = seeded
    for nid in ("n0000", "n0300", "n0649"):
        db.add(FixedWantedEntry(group_id="A", year=2026, month=7, nurse_id=nid, shift_date=date(2026, 7, 1),
                                shift_id="D", is_applied=True, source_type="original"))
    db.add(FixedWantedEntry(group_id="A", year=2026, month=7, nurse_id="n0299", shift_date=date(2026, 7, 2),
                            shift_id="E", is_applied=False, source_type="original"))
    db.flush()
    with _Counter(db) as c:
        rows = _read_fixed_wanted_rows(db, _ids(650), "A", 2026, 7)
    assert len(c.params) == 3
    assert sorted(r.nurse_id for r in rows) == ["n0000", "n0300", "n0649"]  # 미적용 행 제외 그대로
    with _Counter(db) as c:
        assert _read_fixed_wanted_rows(db, _ids(10), "A", 2026, 7)[0].nurse_id == "n0000"
    assert len(c.params) == 1
    with _Counter(db) as c:  # 빈 목록은 지금처럼 한 문장
        assert _read_fixed_wanted_rows(db, [], "A", 2026, 7) == []
    assert len(c.params) == 1


def test_read_schedule_entries_650_three_statements_and_none_single(seeded):
    db = seeded
    for i, nid in enumerate(("n0000", "n0299", "n0300", "n0649")):
        db.add(ScheduleEntry(entry_id=f"e{i}", schedule_id="S1", nurse_id=nid,
                             work_date=datetime(2026, 6, 30), shift_id="D"))
    db.flush()
    with _Counter(db) as c:
        got = _read_schedule_entries(db, "S1", _ids(650))
    assert len(c.params) == 3
    assert sorted(got) == sorted((nid, 30, "D") for nid in ("n0000", "n0299", "n0300", "n0649"))
    with _Counter(db) as c:  # None = 전원 → 필터 없는 한 문장
        assert len(_read_schedule_entries(db, "S1", None)) == 4
    assert len(c.params) == 1
    with _Counter(db) as c:  # 빈 목록은 지금처럼 한 문장(결과 0)
        assert _read_schedule_entries(db, "S1", []) == []
    assert len(c.params) == 1
