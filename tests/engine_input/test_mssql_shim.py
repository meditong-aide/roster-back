"""MSSQL 방언 shim 단위 테스트(설계서 §7.1 'shim 자체 단위 테스트').

세 층으로 본다.
  A. 재작성 — 목록 구문은 바뀌고, 그 밖은 한 글자도 안 바뀐다(ORM 이 내는 SQL 포함).
  B. 사용자 함수 — GETDATE 고정 시각 · DATEADD 의 MSSQL 의미.
  C. 엔진에 건 shim — 격리 SQLite 엔진에서 실행·INFORMATION_SCHEMA 새로 고침·해제.
  D. app 의 실제 raw SQL — 하네스 엔진에서 app 함수를 직접 불러 shim 경유로 통과하는지.
     (app 모듈은 테스트 안에서 import 한다 — 수집 시점 import 는 차단 가드 밖이다.)
"""

from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from .mssql_shim import (
    FIXED_NOW,
    _mask,
    dateadd,
    format_ts,
    install_mssql_shim,
    rewrite_mssql,
)

_NOW_TEXT = format_ts(FIXED_NOW)


# ── A. 재작성 ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("src, expected", [
    ("SELECT TOP 1 fixed_holiday_off_yn FROM roster_config WHERE group_id = :g ORDER BY config_id DESC",
     "SELECT fixed_holiday_off_yn FROM roster_config WHERE group_id = :g ORDER BY config_id DESC LIMIT 1"),
    ("SELECT TOP 1 activate FROM weekly_off_settings WHERE office_id = :o",
     "SELECT activate FROM weekly_off_settings WHERE office_id = :o LIMIT 1"),
    ("select distinct top 3 a from t order by a", "select distinct a from t order by a LIMIT 3"),
    ("SELECT TOP (5) a FROM t", "SELECT a FROM t LIMIT 5"),
    ("SELECT TOP 1\n    a,\n    b\nFROM t\nORDER BY a DESC\n    ",
     "SELECT a,\n    b\nFROM t\nORDER BY a DESC LIMIT 1\n    "),
    ("SELECT TOP 1 a FROM t;", "SELECT a FROM t LIMIT 1;"),
])
def test_top_becomes_limit_at_scope_end(src, expected):
    """TOP n 은 그 SELECT 범위 끝의 LIMIT n 이 된다(뒤 공백·';' 앞)."""
    assert rewrite_mssql(src) == expected


def test_top_in_subqueries_stays_inside_parentheses():
    """하위 질의 TOP 은 그 괄호 안 끝에 붙는다(DELETE NOT IN · UPDATE 스칼라 하위 질의)."""
    delete_sql = ("DELETE FROM a WHERE id NOT IN ("
                  "SELECT TOP 20 id FROM a WHERE g = :g ORDER BY id DESC) "
                  "AND id NOT IN (SELECT MAX(id) FROM a GROUP BY y, m)")
    assert rewrite_mssql(delete_sql) == (
        "DELETE FROM a WHERE id NOT IN ("
        "SELECT id FROM a WHERE g = :g ORDER BY id DESC LIMIT 20) "
        "AND id NOT IN (SELECT MAX(id) FROM a GROUP BY y, m)")
    update_sql = ("UPDATE c SET v = (  SELECT TOP 1 v FROM c WHERE g = :g AND id < :n "
                  "  ORDER BY id DESC) WHERE id = :n AND v IS NULL")
    assert rewrite_mssql(update_sql) == (
        "UPDATE c SET v = (  SELECT v FROM c WHERE g = :g AND id < :n "
        "  ORDER BY id DESC LIMIT 1) WHERE id = :n AND v IS NULL")


def test_nested_tops_both_rewritten():
    """바깥·안쪽 TOP 이 각자 자기 범위에서 바뀐다."""
    src = "SELECT TOP 2 a FROM t WHERE a IN (SELECT TOP 5 b FROM u ORDER BY b) ORDER BY a"
    assert rewrite_mssql(src) == (
        "SELECT a FROM t WHERE a IN (SELECT b FROM u ORDER BY b LIMIT 5) ORDER BY a LIMIT 2")


@pytest.mark.parametrize("src, expected", [
    ("CAST(constraints_json AS NVARCHAR(MAX)) AS j", "CAST(constraints_json AS TEXT) AS j"),
    ("cast(x as nvarchar ( max ))", "cast(x AS TEXT)"),
    ("SELECT COUNT(*) FROM eun_roster.dbo.nurses n", "SELECT COUNT(*) FROM nurses n"),
    ("SELECT * FROM [eun_roster].dbo.push_outbox", "SELECT * FROM push_outbox"),
    ("SELECT * FROM [eun_roster].[dbo].nurses", "SELECT * FROM nurses"),
    ("created_at >= DATEADD(month, -24, GETDATE())", "created_at >= DATEADD('month', -24, GETDATE())"),
    ("DATEADD( mi , 5, x)", "DATEADD( 'mi' , 5, x)"),
    ("UPDATE teams SET updated_at=CURRENT_TIMESTAMP WHERE note = 'CURRENT_TIMESTAMP'",
     "UPDATE teams SET updated_at=GETDATE() WHERE note = 'CURRENT_TIMESTAMP'"),
    ("INSERT INTO t (a, created_at) VALUES (?, current_timestamp)",
     "INSERT INTO t (a, created_at) VALUES (?, GETDATE())"),
])
def test_listed_constructs_rewritten(src, expected):
    """NVARCHAR(MAX)·3-part 접두·DATEADD 단위·CURRENT_TIMESTAMP 는 정해진 모양으로만 바뀐다."""
    assert rewrite_mssql(src) == expected


@pytest.mark.parametrize("src", [
    "SELECT nurses.nurse_id, nurses.name FROM nurses WHERE nurses.group_id = ?",
    "SELECT laptop, stop_flag, top_n, desktop FROM t",
    "SELECT 'SELECT TOP 1 x FROM eun.dbo.t' AS s, \"TOP 2\" FROM t",
    "SELECT x FROM t -- SELECT TOP 1 y\n",
    "SELECT x /* TOP 1 CAST(a AS NVARCHAR(MAX)) */ FROM t",
    "SELECT TOP 10 PERCENT x FROM t",
    "SELECT TOP 5 WITH TIES x FROM t ORDER BY y",
    "SELECT TOP 1 a FROM t UNION SELECT b FROM u",
    "SELECT x FROM dbo.nurses",
    "UPDATE TOP (1) t SET a = 1",
    "SELECT CAST(x AS VARCHAR(MAX)) FROM t",
    "SELECT DATEADD('month', 1, x), DATEADD(:unit, 1, x) FROM t",
    "SELECT TOP :n a FROM t",
    "PRAGMA main.table_info(\"nurses\")",
])
def test_unlisted_sql_untouched(src):
    """목록 밖(리터럴·주석·미지원 형태 포함)은 그대로 — 같은 객체를 돌려준다."""
    assert rewrite_mssql(src) is src


def test_orm_compiled_sql_untouched():
    """하네스 메타데이터의 모든 테이블 SELECT/INSERT/UPDATE/DELETE 를 sqlite 로 컴파일해도 그대로다.

    예외는 목록 항목 CURRENT_TIMESTAMP(onupdate=func.now())뿐 — 그 자리만 GETDATE() 로 바뀐다.
    """
    import db.models as models
    from sqlalchemy import delete, insert, select, update
    from sqlalchemy.dialects import sqlite

    dialect = sqlite.dialect()
    tables = list(models.Nurse.__table__.metadata.sorted_tables)
    assert len(tables) > 30
    for table in tables:
        col = next(iter(table.columns))
        for stmt in (select(table).where(col.isnot(None)).limit(1), insert(table),
                     update(table).where(col == 1).values({col.name: 1}), delete(table).where(col == 1)):
            compiled = str(stmt.compile(dialect=dialect))
            expected = compiled.replace("CURRENT_TIMESTAMP", "GETDATE()")
            assert rewrite_mssql(compiled) == expected, (table.name, compiled)


def test_mask_keeps_length_and_hides_literals():
    """가림 사본은 길이가 같고 리터럴·주석 안의 글자를 감춘다."""
    src = "SELECT 'it''s TOP 1' AS a -- TOP 2\nFROM t /* TOP 3 */"
    masked = _mask(src)
    assert len(masked) == len(src)
    assert "TOP" not in masked
    assert masked.startswith("SELECT ") and "FROM t" in masked


# ── B. 사용자 함수 ──────────────────────────────────────────────────────


@pytest.mark.parametrize("unit, n, ts, expected", [
    ("month", -24, _NOW_TEXT, "2024-10-01 09:00:00.000000"),
    ("m", 1, "2026-01-31 00:00:00", "2026-02-28 00:00:00.000000"),
    ("yyyy", 1, "2024-02-29", "2025-02-28 00:00:00.000000"),
    ("quarter", 1, "2026-11-30 12:00:00", "2027-02-28 12:00:00.000000"),
    ("day", -1, "2026-03-01 00:00:00.000000", "2026-02-28 00:00:00.000000"),
    ("week", 2, date(2026, 10, 1), "2026-10-15 00:00:00.000000"),
    ("hh", 25, datetime(2026, 10, 1, 9), "2026-10-02 10:00:00.000000"),
    ("mi", -30, "2026-10-01T09:00:00", "2026-10-01 08:30:00.000000"),
])
def test_dateadd_mssql_semantics(unit, n, ts, expected):
    """DATEADD 는 MSSQL 의미(월·연·분기는 말일 보정)를 따른다."""
    assert dateadd(unit, n, ts) == expected


def test_dateadd_null_and_unknown_unit():
    """NULL 이 끼면 NULL, 모르는 단위는 ValueError(조용히 넘기지 않는다)."""
    assert dateadd("day", None, _NOW_TEXT) is None
    assert dateadd("day", 1, None) is None
    with pytest.raises(ValueError):
        dateadd(9, 1, _NOW_TEXT)


# ── C. 격리 엔진에 건 shim ──────────────────────────────────────────────


def _new_lite_engine():
    """격리 SQLite 엔진(StaticPool) — 하네스 엔진과 상태를 나누지 않는다."""
    return create_engine("sqlite://", poolclass=StaticPool,
                         connect_args={"check_same_thread": False})


@pytest.fixture
def lite():
    """roster_config 와 month 컬럼이 있는 표를 둔 격리 엔진 + shim."""
    eng = _new_lite_engine()
    with eng.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE roster_config (config_id INTEGER PRIMARY KEY, "
                             "group_id VARCHAR(50) NOT NULL, created_at DATETIME)")
        conn.exec_driver_sql("CREATE TABLE attempt (id INTEGER PRIMARY KEY, month INTEGER)")
        conn.exec_driver_sql("INSERT INTO roster_config VALUES (1, 'G1', '2026-01-01'), "
                             "(2, 'G1', '2026-02-01'), (3, 'G2', '2026-03-01')")
        conn.exec_driver_sql("INSERT INTO attempt VALUES (1, 5)")
    shim = install_mssql_shim(eng)
    yield eng
    shim.uninstall()
    eng.dispose()


def _scalar(eng, sql: str, params: dict | None = None):
    """한 줄 질의의 첫 값."""
    with eng.connect() as conn:
        return conn.execute(text(sql), params or {}).scalar()


def test_engine_runs_top_cast_prefix(lite):
    """TOP·NVARCHAR(MAX)·3-part 접두가 SQLite 에서 실행된다."""
    top = "SELECT TOP 1 config_id FROM roster_config WHERE group_id = :g ORDER BY config_id DESC"
    assert _scalar(lite, top, {"g": "G1"}) == 2
    assert _scalar(lite, "SELECT CAST(config_id AS NVARCHAR(MAX)) FROM roster_config "
                         "WHERE config_id = 3") == "3"
    assert _scalar(lite, "SELECT COUNT(*) FROM eun_roster.dbo.roster_config") == 3


def test_engine_getdate_dateadd_with_month_column(lite):
    """GETDATE 는 고정 시각, DATEADD 단위는 month 컬럼이 있어도 단위로 읽힌다."""
    assert _scalar(lite, "SELECT GETDATE()") == _NOW_TEXT
    got = _scalar(lite, "SELECT DATEADD(month, -24, GETDATE()) FROM attempt")
    assert got == "2024-10-01 09:00:00.000000"


def test_engine_current_timestamp_is_fixed(lite):
    """func.now() 의 SQLite 컴파일(CURRENT_TIMESTAMP)도 고정 시각 — 모델 기본값 INSERT 포함."""
    from sqlalchemy import func, select

    assert _scalar(lite, "SELECT CURRENT_TIMESTAMP") == _NOW_TEXT
    with lite.connect() as conn:
        assert conn.execute(select(func.now())).scalar() == FIXED_NOW
        conn.exec_driver_sql("INSERT INTO roster_config VALUES (9, 'G9', CURRENT_TIMESTAMP)")
        stored = conn.exec_driver_sql("SELECT created_at FROM roster_config WHERE config_id = 9").scalar()
        conn.rollback()
    assert stored == _NOW_TEXT


def test_custom_fixed_now():
    """install 때 넘긴 시각을 GETDATE 가 돌려준다."""
    eng = _new_lite_engine()
    shim = install_mssql_shim(eng, fixed_now=datetime(2026, 9, 15, 7, 30))
    try:
        assert _scalar(eng, "SELECT GETDATE()") == "2026-09-15 07:30:00.000000"
    finally:
        shim.uninstall()
        eng.dispose()


def test_information_schema_reflects_tables_and_columns(lite):
    """INFORMATION_SCHEMA 가 main 의 표·컬럼을 비추고 이름 비교는 대소문자를 가리지 않는다."""
    tables = "SELECT 1 AS ok FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = :t"
    columns = ("SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS "
               "WHERE TABLE_NAME = :t AND COLUMN_NAME = :c")
    assert _scalar(lite, tables, {"t": "roster_config"}) == 1
    assert _scalar(lite, tables, {"t": "ROSTER_CONFIG"}) == 1
    assert _scalar(lite, tables, {"t": "no_such_table"}) is None
    assert _scalar(lite, columns, {"t": "roster_config", "c": "group_id"}) == 1
    assert _scalar(lite, columns, {"t": "roster_config", "c": "n2n_min_gap"}) == 0


def test_information_schema_follows_alter_and_new_table(lite):
    """ALTER TABLE ADD COLUMN·새 표가 따로 새로 고치지 않아도 바로 보인다."""
    columns = ("SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS "
               "WHERE TABLE_NAME = 'roster_config' AND COLUMN_NAME = 'n2n_min_gap'")
    assert _scalar(lite, columns) == 0
    with lite.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE roster_config ADD COLUMN n2n_min_gap INTEGER")
        conn.exec_driver_sql("CREATE TABLE weekly_off_settings (id INTEGER PRIMARY KEY)")
    assert _scalar(lite, columns) == 1
    assert _scalar(lite, "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES "
                         "WHERE TABLE_NAME = 'weekly_off_settings'") == 1


def test_information_schema_follows_rollback(lite):
    """rollback 된 ALTER 는 사라지고, 확정된 ALTER 는 새로 고침이 rollback 돼도 남는다."""
    columns = ("SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS "
               "WHERE TABLE_NAME = 'roster_config' AND COLUMN_NAME = :c")
    with lite.connect() as conn:
        conn.exec_driver_sql("DELETE FROM attempt WHERE 1 = 0")   # 트랜잭션 시작(DDL 도 안에 든다)
        conn.exec_driver_sql("ALTER TABLE roster_config ADD COLUMN tmp_col INTEGER")
        assert conn.execute(text(columns), {"c": "tmp_col"}).scalar() == 1
        conn.rollback()
    assert _scalar(lite, columns, {"c": "tmp_col"}) == 0
    with lite.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE roster_config ADD COLUMN kept_col INTEGER")
    with lite.connect() as conn:
        conn.exec_driver_sql("DELETE FROM attempt WHERE 1 = 0")
        assert conn.execute(text(columns), {"c": "kept_col"}).scalar() == 1
        conn.rollback()
    assert _scalar(lite, columns, {"c": "kept_col"}) == 1


def test_uninstall_restores_plain_sqlite():
    """해제하면 재작성·함수·INFORMATION_SCHEMA 가 모두 걷힌다."""
    eng = _new_lite_engine()
    shim = install_mssql_shim(eng)
    assert _scalar(eng, "SELECT TOP 1 1") == 1
    shim.uninstall()
    try:
        for sql in ("SELECT TOP 1 1", "SELECT GETDATE()",
                    "SELECT 1 FROM INFORMATION_SCHEMA.TABLES"):
            with pytest.raises(OperationalError):
                _scalar(eng, sql)
        with eng.connect() as conn:
            names = [row[1] for row in conn.exec_driver_sql("PRAGMA database_list")]
        assert "INFORMATION_SCHEMA" not in [n.upper() for n in names]
    finally:
        eng.dispose()


def test_install_refuses_non_sqlite():
    """sqlite 가 아닌 엔진에는 걸지 않는다(연결 없이 거절)."""
    eng = create_engine("mssql+pymssql://u:p@127.0.0.1:1/x")
    try:
        with pytest.raises(ValueError, match="sqlite"):
            install_mssql_shim(eng)
    finally:
        eng.dispose()


def test_prepare_inside_open_transaction_refused():
    """이미 쓰기 트랜잭션이 열린 연결에는 INFORMATION_SCHEMA 를 붙이지 않고 크게 실패한다."""
    eng = _new_lite_engine()
    conn = eng.connect()
    conn.exec_driver_sql("CREATE TABLE t (a INTEGER)")
    conn.exec_driver_sql("INSERT INTO t VALUES (1)")
    shim = install_mssql_shim(eng)
    try:
        with pytest.raises(RuntimeError, match="트랜잭션"):
            conn.exec_driver_sql("SELECT 1")
    finally:
        conn.rollback()
        conn.close()
        shim.uninstall()
        eng.dispose()


# ── D. app 의 실제 raw SQL ──────────────────────────────────────────────


def _schema_fingerprint(engine) -> dict[str, tuple[str, ...]]:
    """하네스 엔진의 표별 컬럼 이름(스키마 오염 확인용)."""
    insp = inspect(engine)
    return {t: tuple(c["name"] for c in insp.get_columns(t)) for t in sorted(insp.get_table_names())}


@pytest.fixture
def shim_db(engine, engine_input_tables):
    """하네스 엔진 + shim 위의 트랜잭션 세션. 끝나면 rollback 하고 스키마가 그대로인지 본다."""
    before = _schema_fingerprint(engine)
    shim = install_mssql_shim(engine)
    connection = engine.connect()
    trans = connection.begin()
    connection.exec_driver_sql("DELETE FROM roster_config WHERE 1 = 0")  # 이후 ALTER 도 트랜잭션 안
    session = Session(bind=connection)
    try:
        yield session
    finally:
        session.close()
        trans.rollback()
        connection.close()
        shim.uninstall()
    assert _schema_fingerprint(engine) == before, "하네스 엔진 스키마가 테스트 뒤에 바뀌었다"


def _rcs():
    """roster_create_service 를 가드 안에서 import 한다."""
    from services import roster_create_service

    return roster_create_service


def _add_config_rows(db, rows: list[dict]) -> None:
    """roster_config 행을 ORM 으로 넣는다(NOT NULL 칸은 모델 기본값)."""
    from db.models import RosterConfig

    for row in rows:
        db.add(RosterConfig(office_id="O1", **row))
    db.flush()


def test_table_and_column_exists(shim_db):
    """`_table_exists`·`_column_exists`(:1063·:1077)가 실제 스키마대로 답한다."""
    rcs = _rcs()
    assert rcs._table_exists(shim_db, "weekly_off_settings") is True
    assert rcs._table_exists(shim_db, "no_such_table") is False
    assert rcs._column_exists(shim_db, "nurses", "weekly_off_weekday") is True
    assert rcs._column_exists(shim_db, "roster_config", "n2n_min_gap") is False


def test_holiday_off_and_n2n_raw_columns(shim_db, capsys):
    """raw 컬럼 읽기(:1236~:1290)가 컬럼 부재·존재 양쪽에서 실패 로그 없이 돈다."""
    rcs = _rcs()
    _add_config_rows(shim_db, [{"config_id": 1, "group_id": "G1"}, {"config_id": 2, "group_id": "G1"}])
    assert rcs._holiday_off_enabled(shim_db, "G1") is False
    assert rcs._n2n_min_gap_setting(shim_db, "G1") == 0
    shim_db.execute(text("ALTER TABLE roster_config ADD COLUMN fixed_holiday_off_yn INTEGER"))
    shim_db.execute(text("ALTER TABLE roster_config ADD COLUMN n2n_min_gap INTEGER"))
    shim_db.execute(text("UPDATE roster_config SET fixed_holiday_off_yn = 1, n2n_min_gap = 5 "
                         "WHERE config_id = 1"))
    assert rcs._holiday_off_enabled(shim_db, "G1") is False      # 최신 행(2)은 NULL
    assert rcs._n2n_min_gap_setting(shim_db, "G1") == 5          # IS NOT NULL 로 1 을 찾는다
    out = capsys.readouterr().out
    assert "[Holiday] 설정 조회 실패" not in out
    assert "[N2N-MinGap] 설정 조회 실패" not in out


def test_inherit_n2n_min_gap_update_subquery(shim_db, capsys):
    """`inherit_n2n_min_gap`(:1322 UPDATE … (SELECT TOP 1 …))이 baseline 값을 승계한다."""
    rcs = _rcs()
    _add_config_rows(shim_db, [{"config_id": 1, "group_id": "G1"}, {"config_id": 2, "group_id": "G1"}])
    shim_db.execute(text("ALTER TABLE roster_config ADD COLUMN n2n_min_gap INTEGER"))
    shim_db.execute(text("UPDATE roster_config SET n2n_min_gap = 4 WHERE config_id = 1"))
    rcs.inherit_n2n_min_gap(shim_db, "G1", 2)
    got = shim_db.execute(text("SELECT n2n_min_gap FROM roster_config WHERE config_id = 2")).scalar()
    assert got == 4
    assert "승계 실패" not in capsys.readouterr().out


def test_fetch_grade_config_dict_nvarchar_max(shim_db, capsys):
    """`_fetch_grade_config_dict`(:819 TOP 1 + CAST AS NVARCHAR(MAX))가 그 병동 행을 읽는다.

    (office_id, group_id) 는 유일 제약이라 병동당 한 행이다 — 다른 병동 행으로 WHERE 를 확인한다.
    """
    from db.models import RosterGradeConfig

    rcs = _rcs()
    for cid, gid, d_min in ((1, "G2", 9), (2, "G1", 3)):
        shim_db.add(RosterGradeConfig(config_id=cid, office_id="O1", group_id=gid,
                                      constraints_json={"1": {"D": d_min}},
                                      constraints_max_json={"1": {"N": 2}}))
    shim_db.flush()
    got = rcs._fetch_grade_config_dict(shim_db, "O1", "G1")
    assert got["constraints_json"]["1"]["D"] == 3
    assert got["constraints_max_json"] == {"1": {"N": 2}}
    assert "[GradeConfig][WARN]" not in capsys.readouterr().out


def test_weekly_off_settings_top_without_order(shim_db, capsys):
    """주휴 설정(:1552 TOP 1, ORDER BY 없음)을 읽어 비활성 분기까지 간다."""
    from db.models import WeeklyOffSetting

    rcs = _rcs()
    shim_db.add(WeeklyOffSetting(office_id="O1", group_id="G1", activate=0))
    shim_db.flush()
    days, warnings = rcs._compute_weekly_off_day_indices_for_month(shim_db, "O1", "G1", 2026, 10)
    assert (days, warnings) == ({}, [])
    assert "주휴 설정 비활성화" in capsys.readouterr().out


def test_generate_snapshot_reads_grade_raw(shim_db, capsys):
    """`_build_generate_snapshot`(:487~:513, TOP 1 constraints_json)이 스냅샷을 만든다."""
    import json

    from db.models import RosterGradeConfig

    rcs = _rcs()
    shim_db.add(RosterGradeConfig(config_id=7, office_id="O1", group_id="G1",
                                  constraints_json={"1": {"E": 2}}))
    shim_db.flush()
    config = SimpleNamespace(config_id=1, office_id="O1", group_id="G1")
    snap = rcs._build_generate_snapshot(shim_db, config, "blocked", 2026, 10)
    assert snap is not None, capsys.readouterr().out
    assert json.loads(snap)["grade_config"] == {"1": {"E": 2}}


_ATTEMPT_DDL = (
    "CREATE TABLE roster_config_attempt (attempt_id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "source_config_id INTEGER, office_id VARCHAR(50), group_id VARCHAR(50), job_id VARCHAR(64), "
    "year INTEGER, month INTEGER, status VARCHAR(20), intervened INTEGER, snapshot TEXT, "
    "created_at DATETIME)"
)


def _seed_attempts(db) -> None:
    """보존 규칙을 가르는 시도 30건: 24개월 밖 5 · 24개월 안 월별 5 · 이번 달 직전 20."""
    rows = [(i, 2023, i, f"2023-{i:02d}-15 00:00:00.000000") for i in range(1, 6)]
    rows += [(i, 2025, i - 5, f"2025-{i - 5:02d}-15 00:00:00.000000") for i in range(6, 11)]
    rows += [(i, 2026, 9, f"2026-09-{i - 10:02d} 00:00:00.000000") for i in range(11, 31)]
    for attempt_id, year, month, created in rows:
        db.execute(text("INSERT INTO roster_config_attempt (attempt_id, source_config_id, group_id, "
                        "year, month, status, intervened, created_at) "
                        "VALUES (:a, 1, 'G1', :y, :m, 'success', 0, :c)"),
                   {"a": attempt_id, "y": year, "m": month, "c": created})


def test_mark_config_generate_status_attempt_retention(shim_db, capsys):
    """`_mark_config_generate_status`(:655·:673 GETDATE·:696 TOP 20+DATEADD)가 기록·보존 정리를 한다.

    기대: 새 시도(31)는 GETDATE 시각으로 들어가고, 최근 20건(12~31)과 24개월 안 월별 최신(6~10)
    만 남는다. 24개월 밖(1~5)과 이번 달 직전 묶음의 최신이 아닌 11 은 지워진다.
    """
    _add_config_rows(shim_db, [{"config_id": 1, "group_id": "G1"}])
    shim_db.execute(text(_ATTEMPT_DDL))
    _seed_attempts(shim_db)
    config = SimpleNamespace(config_id=1, office_id="O1", group_id="G1")
    _rcs()._mark_config_generate_status(shim_db, config, "success", snapshot="{}",
                                        year=2026, month=10, job_id="job-1")
    out = capsys.readouterr().out
    assert "기록 실패" not in out and "상태 기록 실패" not in out, out
    kept = {r[0] for r in shim_db.execute(text("SELECT attempt_id FROM roster_config_attempt"))}
    assert kept == set(range(6, 11)) | set(range(12, 32))
    created = shim_db.execute(text("SELECT created_at FROM roster_config_attempt "
                                   "WHERE attempt_id = 31")).scalar()
    assert created == _NOW_TEXT
    status = shim_db.execute(text("SELECT last_generate_status FROM roster_config "
                                  "WHERE config_id = 1")).scalar()
    assert status == "success"


def test_oncall_read_config_flag(shim_db):
    """콜 후처리 `_read_config_flag`(oncall_postprocess.py:316·:329)가 컬럼 유무대로 읽는다."""
    from services.oncall_postprocess import _read_config_flag

    _add_config_rows(shim_db, [
        {"config_id": 1, "group_id": "G1", "created_at": datetime(2026, 1, 1)},
        {"config_id": 2, "group_id": "G1", "created_at": datetime(2026, 2, 1)},
    ])
    flag = "ban_night_before_fixed_off"
    assert _read_config_flag(shim_db, "G1", flag, config_id=None, default=True) is True
    shim_db.execute(text(f"ALTER TABLE roster_config ADD COLUMN {flag} INTEGER"))
    shim_db.execute(text(f"UPDATE roster_config SET {flag} = CASE config_id WHEN 2 THEN 0 ELSE 1 END"))
    assert _read_config_flag(shim_db, "G1", flag, config_id=None, default=True) is False
    assert _read_config_flag(shim_db, "G1", flag, config_id=1, default=False) is True


def test_oncall_call_code_map(shim_db):
    """`load_call_code_map`(oncall_assign.py:80)이 컬럼 부재면 {}, 있으면 콜 코드를 잇는다."""
    from db.models import Shift
    from services.oncall_assign import load_call_code_map

    for sid, pk in (("D1", 101), ("D1콜", 102)):
        shim_db.add(Shift(shift_id=sid, id=pk, office_id="O1", group_id="G1", name=sid, color="#fff"))
    shim_db.flush()
    assert load_call_code_map(shim_db, "G1") == {}
    shim_db.execute(text("ALTER TABLE shifts ADD COLUMN call_base_id INTEGER"))
    shim_db.execute(text("UPDATE shifts SET call_base_id = 101 WHERE id = 102"))
    assert load_call_code_map(shim_db, "G1") == {"D1": "D1콜"}
