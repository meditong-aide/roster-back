"""SQLite 엔진에 거는 MSSQL 방언 shim — 골든 하네스 전용(설계서 §7.1 · EI-1).

app 의 raw SQL(`text(...)`)은 MSSQL 방언이다. 하네스는 SQLite 위에서 돌므로 그 방언만 좁게
바꾼다. **아래 목록 밖의 구문은 한 글자도 바꾸지 않는다**(test_mssql_shim 이 확인한다).

재작성 — `before_cursor_execute(retval=True)`
  - `SELECT [DISTINCT] TOP n` · `TOP (n)` → TOP 을 빼고 **그 SELECT 범위 끝**에 `LIMIT n`
    (괄호 하위 질의 안의 TOP 은 그 괄호 안 끝에 붙는다 — `NOT IN (SELECT TOP 20 …)` 등)
  - `CAST(x AS NVARCHAR(MAX))` → `CAST(x AS TEXT)`
  - 3-part 접두 `<db>.dbo.` · `[<db>].dbo.` · `[<db>].[dbo].` → 제거
  - `DATEADD(<단위>, …)` → `DATEADD('<단위>', …)` — **설계서와 다른 점(코드 사실)**.
    SQLite 는 맨 단위 이름을 컬럼 이름으로 읽는다. `roster_config_attempt` 에는 실제로
    `month` 컬럼이 있어(`roster_create_service.py:706`) 오류 없이 그 행의 month 값이 단위로
    들어간다. 그래서 알려진 단위 이름만 문자열로 감싼다.
  - `CURRENT_TIMESTAMP` → `GETDATE()` — **설계서에 없던 항목(2부 시각 고정)**. app 의 `func.now()`
    (모델 기본값·onupdate·쿼리, 82곳)는 SQLite 에서 CURRENT_TIMESTAMP(UTC 벽시계)로 컴파일된다.
    운영 MSSQL 에서도 서버 시각이므로 고정 GETDATE() 로 보낸다. 컴파일 단계(SQLiteCompiler)에서
    바꾸면 ORM 매퍼 자체 컴파일 캐시(`mapper._compiled_cache`)에 남아 하네스 밖으로 새므로,
    실행 직전 재작성으로만 한다.
사용자 함수 — 재작성 아님
  - `GETDATE()` 고정 시각(`fixed_now`) · `DATEADD(unit, n, ts)` MSSQL 의미(월·연은 말일 보정)
  - 시각 문자열 형식은 SQLAlchemy SQLite DateTime 저장 형식과 같다(`YYYY-MM-DD HH:MM:SS.ffffff`).
INFORMATION_SCHEMA
  - `ATTACH ':memory:' AS INFORMATION_SCHEMA` 에 `TABLES`·`COLUMNS` 를 두고 main 스키마
    (`create_all` 결과 + 시나리오의 `ALTER TABLE ADD COLUMN`)를 비춘다.
  - INFORMATION_SCHEMA 를 읽는 문장 직전에 main 의 `schema_version` 이 채운 때와 다르면
    다시 채운다. 기준 버전도 같은 트랜잭션에 적으므로 rollback 되면 함께 되돌아가 다음에 다시 채운다.
  - 이름 비교는 NOCASE — MSSQL 기본 콜레이션(CI)과 같게.
지원하지 않는 것 — 그대로 두어 SQLite 가 크게 실패하게 한다
  - `TOP n PERCENT` · `WITH TIES` · `TOP @var`/`TOP :p` · `UPDATE/DELETE TOP` · 4-part 이름
  - TOP 이 있는 SELECT 범위에 UNION·EXCEPT·INTERSECT 가 있는 경우
"""

from __future__ import annotations

import calendar
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import event
from sqlalchemy.engine import Engine

#: GETDATE() 기본 고정 시각. 시나리오가 다른 '오늘'을 쓰면 install 때 넘긴다.
FIXED_NOW = datetime(2026, 10, 1, 9, 0, 0)

_TS_FORMAT = "%Y-%m-%d %H:%M:%S.%f"
_IS_SCHEMA = "INFORMATION_SCHEMA"

# ── 재작성 정규식 ───────────────────────────────────────────────────────

_QUICK_RE = re.compile(r"TOP|NVARCHAR|DATEADD|dbo|CURRENT_TIMESTAMP", re.IGNORECASE)
_CURRENT_TS_RE = re.compile(r"\bCURRENT_TIMESTAMP\b", re.IGNORECASE)
_TOP_RE = re.compile(
    r"\bSELECT(?:\s+DISTINCT)?\s+"
    r"(?P<top>TOP\s*(?:\(\s*(?P<n1>\d+)\s*\)|(?P<n2>\d+)(?!\d)))"
    r"(?!\s*(?:PERCENT|WITH\s+TIES)\b)(?P<trail>\s*)",
    re.IGNORECASE,
)
_COMPOUND_RE = re.compile(r"\b(?:UNION|EXCEPT|INTERSECT)\b", re.IGNORECASE)
_NVARCHAR_MAX_RE = re.compile(r"\bAS\s+NVARCHAR\s*\(\s*MAX\s*\)", re.IGNORECASE)
_THREE_PART_RE = re.compile(
    r"(?<![\w\].])(?:\[[A-Za-z0-9_]+\]|[A-Za-z_][A-Za-z0-9_]*)\.(?:\[dbo\]|dbo)\.",
    re.IGNORECASE,
)
_DATEADD_UNIT_RE = re.compile(
    r"\bDATEADD\s*\(\s*(?P<unit>year|yyyy|yy|quarter|qq|q|month|mm|m|dayofyear|dy|y|"
    r"day|dd|d|week|wk|ww|weekday|dw|w|hour|hh|minute|mi|n|second|ss|s|millisecond|ms)"
    r"\s*,",
    re.IGNORECASE,
)
_INFO_SCHEMA_RE = re.compile(r"\bINFORMATION_SCHEMA\s*\.", re.IGNORECASE)


# ── 가림(위치 보존) ─────────────────────────────────────────────────────


def _literal_end(sql: str, start: int, quote: str) -> int:
    """start 의 따옴표로 시작한 리터럴이 끝나는 위치(닫는 따옴표 다음). 겹 따옴표는 이스케이프."""
    i = start + 1
    while i < len(sql):
        if sql[i] == quote:
            if i + 1 < len(sql) and sql[i + 1] == quote:
                i += 2
                continue
            return i + 1
        i += 1
    return len(sql)


def _comment_end(sql: str, start: int) -> int | None:
    """start 가 주석 시작이면 그 끝 위치, 아니면 None."""
    if sql.startswith("--", start):
        newline = sql.find("\n", start)
        return len(sql) if newline < 0 else newline
    if sql.startswith("/*", start):
        close = sql.find("*/", start + 2)
        return len(sql) if close < 0 else close + 2
    return None


def _mask(sql: str) -> str:
    """문자열·인용 식별자는 '#', 주석은 공백으로 가린 같은 길이 사본을 만든다.

    재작성 위치는 이 사본에서 찾고 원문에 적용한다 — 리터럴 안의 'TOP 1' 같은 글자를
    구문으로 오인하지 않기 위해서다.
    """
    out = list(sql)
    i = 0
    while i < len(sql):
        if sql[i] in ("'", '"'):
            end = _literal_end(sql, i, sql[i])
            out[i:end] = "#" * (end - i)
            i = end
            continue
        end = _comment_end(sql, i)
        if end is not None:
            out[i:end] = " " * (end - i)
            i = end
            continue
        i += 1
    return "".join(out)


# ── 재작성 ──────────────────────────────────────────────────────────────


def _scope_end(masked: str, start: int) -> int:
    """start 부터 같은 괄호 깊이의 SELECT 범위가 끝나는 위치(닫는 괄호·';'·끝)."""
    depth = 0
    for i in range(start, len(masked)):
        ch = masked[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            if depth == 0:
                return i
            depth -= 1
        elif ch == ";" and depth == 0:
            return i
    return len(masked)


def _top_edits(masked: str) -> list[tuple[int, int, str]]:
    """`SELECT TOP n` 마다 (TOP 제거, 범위 끝 LIMIT 삽입) 편집 두 개를 만든다."""
    edits: list[tuple[int, int, str]] = []
    for m in _TOP_RE.finditer(masked):
        end = _scope_end(masked, m.end())
        if _COMPOUND_RE.search(masked, m.end(), end):
            continue
        insert_at = end
        while insert_at > m.end() and masked[insert_at - 1].isspace():
            insert_at -= 1
        n = m.group("n1") or m.group("n2")
        edits.append((m.start("top"), m.end("trail"), ""))
        edits.append((insert_at, insert_at, f" LIMIT {int(n)}"))
    return edits


def _simple_edits(masked: str) -> list[tuple[int, int, str]]:
    """NVARCHAR(MAX)·3-part 접두·DATEADD 단위·CURRENT_TIMESTAMP 편집."""
    edits = [(m.start(), m.end(), "AS TEXT") for m in _NVARCHAR_MAX_RE.finditer(masked)]
    edits += [(m.start(), m.end(), "") for m in _THREE_PART_RE.finditer(masked)]
    edits += [(m.start(), m.end(), "GETDATE()") for m in _CURRENT_TS_RE.finditer(masked)]
    edits += [
        (m.start("unit"), m.end("unit"), f"'{m.group('unit')}'")
        for m in _DATEADD_UNIT_RE.finditer(masked)
    ]
    return edits


def _apply_edits(sql: str, edits: list[tuple[int, int, str]]) -> str:
    """겹치지 않는 편집을 뒤에서부터 적용한다. 겹치면 shim 결함이라 ValueError."""
    ordered = sorted(edits, key=lambda e: (e[0], e[1]), reverse=True)
    out = sql
    prev_start = len(sql) + 1
    for start, end, text in ordered:
        if end > prev_start:
            raise ValueError(f"mssql_shim 편집이 겹친다: {start}-{end} vs {prev_start}")
        out = out[:start] + text + out[end:]
        prev_start = start
    return out


def rewrite_mssql(sql: str) -> str:
    """MSSQL 방언 SQL 을 SQLite 가 받는 모양으로 바꾼다(목록 밖 구문은 그대로)."""
    if not _QUICK_RE.search(sql):
        return sql
    masked = _mask(sql)
    edits = _top_edits(masked) + _simple_edits(masked)
    return _apply_edits(sql, edits) if edits else sql


# ── 사용자 함수 ─────────────────────────────────────────────────────────

_UNIT_ALIASES = {
    "year": "year", "yyyy": "year", "yy": "year",
    "quarter": "quarter", "qq": "quarter", "q": "quarter",
    "month": "month", "mm": "month", "m": "month",
    "dayofyear": "day", "dy": "day", "y": "day",
    "day": "day", "dd": "day", "d": "day",
    "weekday": "day", "dw": "day", "w": "day",
    "week": "week", "wk": "week", "ww": "week",
    "hour": "hour", "hh": "hour",
    "minute": "minute", "mi": "minute", "n": "minute",
    "second": "second", "ss": "second", "s": "second",
    "millisecond": "millisecond", "ms": "millisecond",
}
_FIXED_DELTAS = {
    "week": timedelta(weeks=1), "day": timedelta(days=1), "hour": timedelta(hours=1),
    "minute": timedelta(minutes=1), "second": timedelta(seconds=1),
    "millisecond": timedelta(milliseconds=1),
}


def format_ts(value: datetime) -> str:
    """시각을 SQLAlchemy SQLite DateTime 저장 형식 문자열로 바꾼다."""
    return value.strftime(_TS_FORMAT)


def _parse_ts(value) -> datetime:
    """DATEADD 입력 시각(문자열·date·datetime)을 datetime 으로 바꾼다."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    return datetime.fromisoformat(str(value).strip())


def _add_months(ts: datetime, months: int) -> datetime:
    """월 더하기 — 결과 달에 그 날이 없으면 말일로 맞춘다(MSSQL 과 같다)."""
    total = ts.year * 12 + (ts.month - 1) + months
    year, month0 = divmod(total, 12)
    day = min(ts.day, calendar.monthrange(year, month0 + 1)[1])
    return ts.replace(year=year, month=month0 + 1, day=day)


def dateadd(unit, number, value) -> str | None:
    """MSSQL `DATEADD(unit, number, value)` — NULL 이 끼면 NULL, 모르는 단위는 ValueError."""
    if number is None or value is None:
        return None
    kind = _UNIT_ALIASES.get(str(unit).strip().lower())
    if kind is None:
        raise ValueError(f"DATEADD 단위를 모른다: {unit!r}")
    n = int(number)
    ts = _parse_ts(value)
    if kind in ("year", "quarter", "month"):
        months = n * {"year": 12, "quarter": 3, "month": 1}[kind]
        return format_ts(_add_months(ts, months))
    return format_ts(ts + _FIXED_DELTAS[kind] * n)


# ── INFORMATION_SCHEMA ──────────────────────────────────────────────────

_IS_DDL = (
    f"CREATE TABLE IF NOT EXISTS {_IS_SCHEMA}.TABLES ("
    " TABLE_CATALOG TEXT, TABLE_SCHEMA TEXT, TABLE_NAME TEXT COLLATE NOCASE, TABLE_TYPE TEXT)",
    f"CREATE TABLE IF NOT EXISTS {_IS_SCHEMA}.COLUMNS ("
    " TABLE_CATALOG TEXT, TABLE_SCHEMA TEXT, TABLE_NAME TEXT COLLATE NOCASE,"
    " COLUMN_NAME TEXT COLLATE NOCASE, ORDINAL_POSITION INTEGER, COLUMN_DEFAULT TEXT,"
    " IS_NULLABLE TEXT, DATA_TYPE TEXT)",
    f"CREATE TABLE IF NOT EXISTS {_IS_SCHEMA}._shim_state (main_schema_version INTEGER)",
)
_MAIN_TABLES_SQL = (
    "SELECT name, type FROM main.sqlite_master WHERE type IN ('table', 'view') "
    "AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\' ORDER BY name"
)
_TABLE_INFO_SQL = (
    'SELECT cid, name, type, "notnull", dflt_value FROM pragma_table_info(?, \'main\') ORDER BY cid'
)


def _is_attached(dbapi_conn) -> bool:
    """INFORMATION_SCHEMA 가 이미 붙어 있으면 참."""
    rows = dbapi_conn.execute("PRAGMA database_list").fetchall()
    return any(str(row[1]).upper() == _IS_SCHEMA for row in rows)


def _schema_rows(cur) -> tuple[list[tuple], list[tuple]]:
    """main 스키마에서 TABLES·COLUMNS 행을 만든다."""
    table_rows, column_rows = [], []
    for name, kind in cur.execute(_MAIN_TABLES_SQL).fetchall():
        table_type = "VIEW" if kind == "view" else "BASE TABLE"
        table_rows.append(("main", "dbo", name, table_type))
        for cid, col, ctype, notnull, dflt in cur.execute(_TABLE_INFO_SQL, (name,)).fetchall():
            data_type = re.sub(r"\s*\(.*$", "", str(ctype or "")).lower()
            nullable = "NO" if notnull else "YES"
            column_rows.append(("main", "dbo", name, col, cid + 1, dflt, nullable, data_type))
    return table_rows, column_rows


def refresh_information_schema(dbapi_conn, *, force: bool = False) -> bool:
    """main 의 schema_version 이 채운 때와 다르면 TABLES·COLUMNS 를 다시 채운다. 채웠으면 참."""
    cur = dbapi_conn.cursor()
    try:
        version = cur.execute("PRAGMA main.schema_version").fetchone()[0]
        stored = cur.execute(f"SELECT main_schema_version FROM {_IS_SCHEMA}._shim_state").fetchone()
        if not force and stored is not None and stored[0] == version:
            return False
        table_rows, column_rows = _schema_rows(cur)
        for table in ("TABLES", "COLUMNS", "_shim_state"):
            cur.execute(f"DELETE FROM {_IS_SCHEMA}.{table}")
        cur.executemany(f"INSERT INTO {_IS_SCHEMA}.TABLES VALUES (?, ?, ?, ?)", table_rows)
        cur.executemany(f"INSERT INTO {_IS_SCHEMA}.COLUMNS VALUES (?, ?, ?, ?, ?, ?, ?, ?)", column_rows)
        cur.execute(f"INSERT INTO {_IS_SCHEMA}._shim_state VALUES (?)", (version,))
        return True
    finally:
        cur.close()


# ── 설치·해제 ───────────────────────────────────────────────────────────


@dataclass
class MssqlShim:
    """한 SQLite 엔진에 건 shim. `uninstall()` 로 이벤트·함수·ATTACH 를 걷는다."""

    engine: Engine
    fixed_now: datetime = FIXED_NOW
    _prepared: dict[int, object] = field(default_factory=dict, repr=False)

    def install(self) -> MssqlShim:
        """이벤트를 건다. 각 DBAPI 연결은 처음 쓰일 때(checkout·첫 문장) 준비한다."""
        if self.engine.dialect.name != "sqlite":
            raise ValueError(f"mssql_shim 은 sqlite 엔진에만 건다: {self.engine.dialect.name}")
        event.listen(self.engine, "checkout", self._on_checkout)
        event.listen(self.engine, "before_cursor_execute", self._before_execute, retval=True)
        return self

    def uninstall(self) -> None:
        """이벤트를 떼고, 준비한 연결에서 함수·INFORMATION_SCHEMA 를 걷는다."""
        event.remove(self.engine, "checkout", self._on_checkout)
        event.remove(self.engine, "before_cursor_execute", self._before_execute)
        for dbapi_conn in list(self._prepared.values()):
            self._unprepare(dbapi_conn)
        self._prepared.clear()

    def _on_checkout(self, dbapi_conn, _record, _proxy) -> None:
        """풀에서 꺼낼 때 준비한다."""
        self._prepare(dbapi_conn)

    def _before_execute(self, _conn, cursor, statement, parameters, _context, _many):
        """문장마다: 연결 준비 확인 → INFORMATION_SCHEMA 새로 고침 → 방언 재작성."""
        dbapi_conn = cursor.connection
        self._prepare(dbapi_conn)
        if _INFO_SCHEMA_RE.search(statement):
            refresh_information_schema(dbapi_conn)
        return rewrite_mssql(statement), parameters

    def _prepare(self, dbapi_conn) -> None:
        """함수 등록 + INFORMATION_SCHEMA ATTACH(연결마다 한 번)."""
        if id(dbapi_conn) in self._prepared:
            return
        if not _is_attached(dbapi_conn):
            if dbapi_conn.in_transaction:
                raise RuntimeError("mssql_shim: 트랜잭션 안에서는 INFORMATION_SCHEMA 를 붙일 수 없다 — "
                                   "세션을 열기 전에 설치한다")
            dbapi_conn.execute(f"ATTACH DATABASE ':memory:' AS {_IS_SCHEMA}")
            for ddl in _IS_DDL:
                dbapi_conn.execute(ddl)
        now_text = format_ts(self.fixed_now)
        dbapi_conn.create_function("GETDATE", 0, lambda: now_text)
        dbapi_conn.create_function("DATEADD", 3, dateadd)
        self._prepared[id(dbapi_conn)] = dbapi_conn

    @staticmethod
    def _unprepare(dbapi_conn) -> None:
        """함수를 지우고 INFORMATION_SCHEMA 를 뗀다(트랜잭션 안이면 RuntimeError)."""
        try:
            attached = _is_attached(dbapi_conn)
        except sqlite3.ProgrammingError as exc:  # 닫힌 연결이면 걷을 것이 없다
            if "closed" in str(exc).lower():
                return
            raise
        dbapi_conn.create_function("GETDATE", 0, None)
        dbapi_conn.create_function("DATEADD", 3, None)
        if attached:
            if dbapi_conn.in_transaction:
                raise RuntimeError("mssql_shim: 트랜잭션이 열린 채로 해제할 수 없다 — 세션을 먼저 닫는다")
            dbapi_conn.execute(f"DETACH DATABASE {_IS_SCHEMA}")


def install_mssql_shim(engine: Engine, *, fixed_now: datetime = FIXED_NOW) -> MssqlShim:
    """engine 에 shim 을 걸고 그 핸들을 돌려준다."""
    return MssqlShim(engine=engine, fixed_now=fixed_now).install()
