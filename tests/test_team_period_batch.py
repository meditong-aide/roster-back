"""EI-13c — `resolve_teams_for_roster`(일괄) == 간호사마다 `resolve_team_for_roster`(단건) · 쿼리 수 단언."""

from __future__ import annotations

from datetime import date

import pytest
from db.models import Group, Nurse, NurseTeamPeriod, Office
from services.team_period import (
    _team_of_periods,
    resolve_team_for_roster,
    resolve_teams_for_roster,
)
from sqlalchemy import event

Y, M = 2026, 7
START = date(Y, M, 1)


def _nurse(nid, gid="A", team=None):
    return Nurse(nurse_id=nid, account_id=f"acc_{nid}", group_id=gid, office_id="o1", name=nid,
                 active=1, team_id=team, allowed_shifts=[])


def _period(nid, gid, team, frm, to=None):
    return NurseTeamPeriod(nurse_id=nid, group_id=gid, team_id=team, valid_from=frm, valid_to=to)


@pytest.fixture
def seeded(db):
    db.add(Office(office_id="o1", office_name="병원"))
    db.add(Group(group_id="A", group_name="A병동", office_id="o1"))
    db.add(Group(group_id="B", group_name="B병동", office_id="o1"))
    db.add_all([
        _nurse("p_cover", team=9),                     # 월초를 덮는 구간 1개 → 그 구간 팀(캐시 9 무시)
        _nurse("p_two_cover", team=9),                 # 월초를 덮는 구간 2개(겹침) → 가장 늦게 시작한 것
        _nurse("p_midjoin", team=9),                   # 월초는 못 덮고 그 달에 겹치는 첫 구간(중간 합류)
        _nurse("p_future", team=9),                    # 구간이 이 달 이후만 → 구간 없음 → 폴백(home A → 캐시 9)
        _nurse("f_home", team=4),                      # 구간 없음 · home == A → 캐시 4
        _nurse("f_other", gid="B", team=5),            # 구간 없음 · home != A → None
        _nurse("f_none", team=None),                   # 구간 없음 · 캐시 NULL → None
    ])
    db.flush()
    db.add_all([
        _period("p_cover", "A", 1, date(2026, 6, 1)),
        _period("p_two_cover", "A", 1, date(2026, 5, 1)),
        _period("p_two_cover", "A", 2, date(2026, 6, 20)),
        _period("p_midjoin", "A", 3, date(2026, 7, 15)),
        _period("p_midjoin", "A", 4, date(2026, 7, 25)),
        _period("p_future", "A", 7, date(2026, 8, 1)),
        _period("p_cover", "B", 8, date(2026, 6, 1)),   # 다른 병동 구간 — A 조회에 섞이면 안 된다
    ])
    db.flush()
    return db


ALL_IDS = ["p_cover", "p_two_cover", "p_midjoin", "p_future", "f_home", "f_other", "f_none", "ghost"]


def test_batch_equals_single_for_every_case(seeded):
    db = seeded
    batch = resolve_teams_for_roster(db, ALL_IDS, "A", Y, M)
    single = {nid: resolve_team_for_roster(db, nid, "A", Y, M) for nid in ALL_IDS}
    assert batch == single
    # 기대값을 손으로도 고정한다(두 쪽이 같이 틀리는 일을 막는다).
    assert single == {"p_cover": 1, "p_two_cover": 2, "p_midjoin": 3, "p_future": 9,
                      "f_home": 4, "f_other": None, "f_none": None, "ghost": None}


def test_batch_other_group_equals_single(seeded):
    db = seeded
    batch = resolve_teams_for_roster(db, ALL_IDS, "B", Y, M)
    assert batch == {nid: resolve_team_for_roster(db, nid, "B", Y, M) for nid in ALL_IDS}


@pytest.mark.parametrize("extra", [0, 10, 100])
def test_batch_queries_do_not_grow_with_nurse_count(seeded, extra):
    db = seeded
    ids = ALL_IDS + [f"ghost_{i}" for i in range(extra)]
    statements: list[str] = []

    def _count(_conn, _cur, statement, *_a):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", _count)
    try:
        resolve_teams_for_roster(db, ids, "A", Y, M)
    finally:
        event.remove(engine, "before_cursor_execute", _count)
    assert len(statements) == 2   # 구간 1 + 폴백 1(구간 없는 간호사 몫) — 인원과 무관


def test_rcs_uses_batch_resolver_only():
    """rcs 팀 루프가 단건 `resolve_team_for_roster` 로 되돌아가지 않는다(정적 단언)."""
    import ast
    from pathlib import Path

    import services.roster_create_service as module

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    names = [c.func.id if isinstance(c.func, ast.Name) else c.func.attr
             for c in ast.walk(tree) if isinstance(c, ast.Call) and isinstance(c.func, (ast.Name, ast.Attribute))]
    assert names.count("resolve_team_for_roster") == 0
    assert names.count("resolve_teams_for_roster") == 1


def test_batch_empty_input_issues_no_query(seeded):
    assert resolve_teams_for_roster(seeded, [], "A", Y, M) == {}
    assert resolve_teams_for_roster(seeded, [None, ""], "A", Y, M) == {}


def test_batch_pick_is_shared_with_single_and_keeps_padded_ids_aligned(seeded, monkeypatch):
    """MSSQL CHAR 꼬리 공백 — 행 id 가 'p_cover ' 로 돌아와도 입력 'p_cover' 와 맞는다."""
    from services import team_period

    class _Row:
        def __init__(self, nid, team, frm):
            self.nurse_id, self.team_id, self.valid_from, self.valid_to = nid, team, frm, None

    rows = [_Row("n1   ", 3, date(2026, 6, 1))]

    class _Q:
        def __init__(self, data):
            self.data = data

        def filter(self, *a, **k):
            return self

        def order_by(self, *a, **k):
            return self

        def all(self):
            return self.data

    class _DB:
        def query(self, *entities):
            return _Q(rows if entities[0] is team_period.NurseTeamPeriod else [])

    assert team_period.resolve_teams_for_roster(_DB(), ["n1"], "A", Y, M) == {"n1": 3}
    assert _team_of_periods(rows, START) == 3
