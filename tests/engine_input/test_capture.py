"""캡처 장치 테스트(설계서 §7.1 · EI-1 2부).

단위 — canonical·digest 제외 · 쿼리 정규화 · 시각 고정 · 경로 분류.
최소 시나리오(min-basic)를 프로덕션 경로로 두 번 돌려 확인한다.
  - 2회 digest 동일(층 전체·쿼리 순서열도 동일)
  - GoldenStop 으로 L2 뒤 멈춤 · CpSolver.solve 호출 0 · 별도 세션 0
  - raw 실패 로그 0 + raw 값이 실제로 L1 까지 온다(n2n_min_gap·등급 설정)
  - 가드가 켜진 상태에서 외부 연결 시도 0(가드 기록으로 확인 + 양성 대조)
  - 흔적 0: 실행 뒤 스키마·행이 그대로다
"""

from __future__ import annotations

import contextlib
import io
import logging
import socket
import sys
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import inspect, text

from .backend_check import assert_sqlite_backends
from .canonical import CanonicalError, canonical, canonical_json, digest_view, layers_digest
from .capture import DETECTOR_CONFIG_KEYS, QueryLog, normalize_statement
from .clock import CLOCK_POINTS, FIXED_TODAY, install_fixed_clock
from .harness import classify_path, run_production
from .mssql_shim import FIXED_NOW
from .scenarios import MINIMAL, N2N_MIN_GAP, NURSE_IDS

#: 설계서 §7.1 'raw 실제 경유 보증' — 데이터를 넣은 시나리오에서 나오면 실패다.
RAW_FAILURE_LOGS = (
    "[GradeConfig][WARN] raw 조회 실패",
    "[N2N-MinGap] 설정 조회 실패",
    "[Holiday] 설정 조회 실패",
)


# ── canonical ───────────────────────────────────────────────────────────


@dataclass
class _Rec:
    a: int
    tags: set


def test_canonical_rules():
    """키 정렬·set 정렬·ISO·Decimal·_sa_ 제거·정수/튜플 키·dataclass."""
    src = {
        "b": {3, 1, 2}, "a": (1, 2), "_sa_instance_state": object(),
        "d": date(2026, 11, 1), "t": datetime(2026, 11, 1, 9, 30), "x": Decimal("1.50"),
        "by_int": {2: "y", 1: "x"}, "by_tuple": {(0, 3): "N"}, "rec": _Rec(1, {"b", "a"}),
    }
    got = canonical(src)
    assert "_sa_instance_state" not in got
    assert got["b"] == [1, 2, 3] and got["a"] == [1, 2]
    assert got["d"] == "2026-11-01" and got["t"] == "2026-11-01T09:30:00" and got["x"] == "1.50"
    assert got["by_int"] == {"1": "x", "2": "y"} and got["by_tuple"] == {"[0,3]": "N"}
    assert got["rec"] == {"__type__": "_Rec", "a": 1, "tags": ["a", "b"]}
    assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'


def test_canonical_rejects_ambiguous_and_unknown():
    """정수 1 과 문자열 '1' 키가 겹치거나, 모르는 타입이면 크게 실패한다."""
    with pytest.raises(CanonicalError, match="같은 문자열"):
        canonical({1: "a", "1": "b"})
    with pytest.raises(CanonicalError, match="바꿀 수 없는 타입"):
        canonical({"f": object()})


def test_canonical_numpy_scalar():
    """numpy 스칼라는 파이썬 값으로."""
    import numpy as np

    assert canonical({"n": np.int64(3), "f": np.float64(0.5)}) == {"f": 0.5, "n": 3}


def _layers(config_extra: dict) -> dict:
    """digest 확인용 최소 층."""
    config = {"max_conseq_work": 5, **config_extra}
    return {"L0": [{"config_dict": dict(config)}], "L0p": [], "L1": [{"config_data": dict(config)}],
            "L2": {}}


def test_digest_excludes_settings_meta_only():
    """메타 칸(§2.4)을 바꿔도 digest 가 같고, 넣는 칸을 바꾸면 달라진다. 원본 층은 그대로다."""
    base = _layers({"last_generate_status": None, "config_name": "가"})
    meta_changed = _layers({"last_generate_status": "success", "config_name": "나",
                            "created_at": "2026-01-01", "updated_at": "x", "version": 9,
                            "config_memo": "m"})
    value_changed = _layers({"last_generate_status": None, "config_name": "가"})
    value_changed["L1"][0]["config_data"]["max_conseq_work"] = 6
    assert layers_digest(base) == layers_digest(meta_changed)
    assert layers_digest(base) != layers_digest(value_changed)
    assert "config_name" in base["L0"][0]["config_dict"]
    assert "config_name" not in digest_view(base)["L0"][0]["config_dict"]


# ── 쿼리 정규화 ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("sql, expected", [
    ('SELECT "groups".group_id AS g FROM "groups" WHERE "groups".office_id = ?', "select:groups"),
    ("SELECT nurses.nurse_id FROM nurses JOIN teams ON nurses.team_id = teams.team_id",
     "select:nurses+teams"),
    ("INSERT INTO schedules (schedule_id) VALUES (?)", "insert:schedules"),
    ("UPDATE roster_config SET n2n_min_gap = (SELECT n2n_min_gap FROM roster_config LIMIT 1)",
     "update:roster_config"),
    ("DELETE FROM schedule_entries WHERE schedule_id = :s", "delete:schedule_entries"),
    ("SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = :t",
     "select:information_schema.columns"),
    ("SELECT x FROM eun_roster.dbo.[shifts] WHERE note = 'FROM nurses'", "select:shifts"),
    ("SELECT count(*) FROM (SELECT a FROM wanted) AS anon_1", "select:wanted"),
    ("SELECT 1", "select:-"),
])
def test_normalize_statement(sql, expected):
    """문장 → '동사:테이블' (리터럴 안 글자·접두·따옴표 무시)."""
    assert normalize_statement(sql) == expected


def test_query_log_counts_only_armed_and_splits_harness():
    """꺼져 있으면 안 세고, SAVEPOINT·PRAGMA 는 하네스 몫으로 따로 센다."""
    log = QueryLog()
    log._after(None, None, "SELECT a FROM t", None, None, False)
    log.armed = True
    log._after(None, None, "SAVEPOINT sa_savepoint_1", None, None, False)
    log._after(None, None, "SELECT a FROM t", None, None, False)
    log.mark("L0")
    log.mark("L0")
    assert log.summary("x") == {"path": "x", "count": 1, "tables": ["select:t"],
                                "marks": {"L0": 1}, "harness_statements": 1}


# ── 시각 고정 · 경로 분류 ───────────────────────────────────────────────


def test_fixed_clock_points_and_restore():
    """고정 지점이 고정 시각을 내고, 타입 판정·생성자는 진짜 date/datetime 이며, 끝나면 원상 복구된다."""
    import services.assignment_service as assignment
    import services.mutual_exclusion_period as mutex
    import services.nurse_period_resolver as resolver
    import services.preceptee_period as preceptee
    import services.roster_create_service as rcs

    with pytest.MonkeyPatch.context() as mp:
        assert install_fixed_clock(mp) == CLOCK_POINTS
        assert assignment.date.today() == FIXED_TODAY and type(assignment.date(2026, 1, 1)) is date
        assert isinstance(date(2020, 1, 1), assignment.date)
        assert rcs.datetime.now() == FIXED_NOW and type(rcs.datetime.now()) is datetime
        assert isinstance(datetime(2020, 1, 1), rcs.datetime)
        assert preceptee._today() == mutex._today() == resolver._today() == FIXED_TODAY
    assert assignment.date is date and rcs.datetime is datetime
    assert resolver._today.__qualname__ == "_today"


def _swallow_exception(fn) -> None:
    """app 의 `except Exception: pass` 흉내 — BaseException 하위는 통과한다."""
    with contextlib.suppress(Exception):
        fn()


def test_safety_guards_raise_base_exceptions(engine):
    """캡처를 건 동안 CpSolver.solve·SessionLocal() 은 세어지고, `except Exception` 을 뚫고 나온다."""
    from ortools.sat.python import cp_model

    from .capture import Capture, GoldenStop, SeparateSessionBlocked, SolveBlocked

    model = cp_model.CpModel()
    model.NewBoolVar("x")
    capture = Capture()
    with pytest.MonkeyPatch.context() as mp:
        capture.install(mp, engine)
        try:
            with pytest.raises(SolveBlocked):
                _swallow_exception(lambda: cp_model.CpSolver().Solve(model))
            with pytest.raises(SeparateSessionBlocked):
                _swallow_exception(lambda: sys.modules["db.client2"].SessionLocal())
        finally:
            capture.queries.detach()
    assert (capture.solve_calls, capture.separate_sessions) == (1, 1)
    assert not issubclass(GoldenStop, Exception) and not issubclass(SolveBlocked, Exception)


@pytest.mark.parametrize("outcome, marks, expected", [
    ("golden_stop", {"L0": 1, "L0p": 2, "L1": 3}, "first_attempt"),
    ("http_exception", {"L0": 1, "L0p": 2}, "night_block"),
    ("http_exception", {"L0": 1}, "precheck_block"),
    ("exception", {"L0": 1}, "error"),
    ("http_exception", {"L0": 1, "L0p": 2, "L1": 3}, "ladder_unrecoverable"),
    ("returned", {"L0": 1, "L0p": 2, "L1": 3}, "post_complete"),
])
def test_classify_path(outcome, marks, expected):
    """결과 종류 + 지점 표지 → 경로."""
    assert classify_path(outcome, marks) == expected


# ── 최소 시나리오 — 프로덕션 경로 2회 ───────────────────────────────────


@dataclass
class _Runs:
    """두 번 실행한 결과와 그동안의 출력·경고·가드 기록."""

    results: list
    stdouts: list
    warnings: list
    blocked: list
    schema_before: dict
    schema_after: dict


def _schema(engine) -> dict:
    """하네스 엔진의 표별 컬럼 이름."""
    insp = inspect(engine)
    return {t: tuple(c["name"] for c in insp.get_columns(t)) for t in sorted(insp.get_table_names())}


class _WarningCollector(logging.Handler):
    """WARNING 이상 로그를 모은다."""

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.records: list[str] = []

    def emit(self, record):
        self.records.append(f"{record.name}: {record.getMessage()}")


@pytest.fixture(scope="module")
def minimal_runs(engine, engine_input_tables, guard_log) -> _Runs:
    """최소 시나리오를 두 번 돌린다(출력·경고·가드 기록 포함).

    첫 줄 backend 확인(msdb_manager·SessionLocal bind 포함)이 프로덕션 실행 직전 자리다.
    """
    assert_sqlite_backends(engine)
    before_blocked, schema_before = len(guard_log), _schema(engine)
    collector = _WarningCollector()
    logging.getLogger().addHandler(collector)
    results, stdouts = [], []
    try:
        for _ in range(2):
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                results.append(run_production(engine, MINIMAL))
            stdouts.append(buffer.getvalue())
    finally:
        logging.getLogger().removeHandler(collector)
    return _Runs(results, stdouts, collector.records, list(guard_log[before_blocked:]),
                 schema_before, _schema(engine))


def test_minimal_stops_after_l2_without_solve(minimal_runs):
    """두 번 다 첫 시도 경로에서 L2 뒤 GoldenStop — solve 0 · 별도 세션 0 · 층마다 1회."""
    for result in minimal_runs.results:
        assert (result.outcome, result.path) == ("golden_stop", "first_attempt"), result.error
        assert result.error is None
        assert result.solve_calls == 0 and result.separate_sessions == 0
        assert [len(result.layers[k]) for k in ("L0", "L0p", "L1")] == [1, 1, 1]
        assert set(result.layers["L2"]) == {"settings_eff", "nurses"}


def test_minimal_digest_is_deterministic(minimal_runs):
    """2회 digest 동일 — 층 전체와 쿼리 수·순서열까지 같다."""
    first, second = minimal_runs.results
    assert first.digest == second.digest
    assert first.layers == second.layers
    assert first.queries == second.queries


def test_minimal_no_raw_failure_logs(minimal_runs):
    """raw 실패 로그 0(설계서 §7.1) — 두 번의 출력 모두."""
    for stdout in minimal_runs.stdouts:
        hits = [msg for msg in RAW_FAILURE_LOGS if msg in stdout]
        assert hits == [], hits


def _swallowed_failure_lines(stdout: str) -> list[str]:
    """app 이 예외를 삼키고 남기는 실패 줄('실패' · 'error:')."""
    return [line for line in stdout.splitlines() if "실패" in line or line.startswith("error:")]


def test_minimal_no_swallowed_failures(minimal_runs):
    """삼킨 실패 0 — 실패 줄이 있으면 캡처 값이 조용히 기본값으로 떨어진 것이다. WARNING 로그도 0."""
    for stdout in minimal_runs.stdouts:
        lines = _swallowed_failure_lines(stdout)
        assert lines == [], lines
    assert minimal_runs.warnings == [], minimal_runs.warnings


def test_minimal_raw_values_reach_engine(minimal_runs):
    """raw 컬럼 값이 실제로 엔진 인자까지 온다 — n2n_min_gap · 등급 설정(raw CAST 경로)."""
    l1 = minimal_runs.results[0].layers["L1"][0]
    assert l1["config_data"]["n2n_min_gap"] == N2N_MIN_GAP
    assert l1["grade_config"]["constraints_json"]["1"] == {"D": 1, "E": 1, "N": 1}
    assert l1["grade_config"]["allow_soft_fallback"] is False


def test_minimal_layer_contents(minimal_runs):
    """층 내용 — 엔진 정렬 순 간호사 · 탐지기 config 키 · L2 간호사 수 · 쿼리 지점 순서."""
    result = minimal_runs.results[0]
    l0p, l2 = result.layers["L0p"][0], result.layers["L2"]
    assert [n["nurse_id"] for n in result.layers["L1"][0]["nurses_data"]] == list(NURSE_IDS)
    assert [n["nurse_id"] for n in result.layers["L0"][0]["nurses_dict"]] == list(NURSE_IDS)
    assert set(l0p["config"]) <= set(DETECTOR_CONFIG_KEYS) and l0p["num_days"] == 30
    assert [n["db_id"] for n in l2["nurses"]] == list(NURSE_IDS)
    marks = result.queries["marks"]
    assert 0 < marks["L0"] <= marks["L0p"] <= marks["L1"] == result.queries["count"]


def test_minimal_no_external_access_attempt(minimal_runs, guard_log):
    """가드가 켜진 채로 외부 연결 시도 0 — 기록이 비었고, 지금도 가드가 막고 기록한다(양성 대조)."""
    assert minimal_runs.blocked == []
    before = len(guard_log)
    with pytest.raises(RuntimeError, match="engine_input 가드"):
        socket.create_connection(("203.0.113.10", 1433), timeout=0.2)
    assert len(guard_log) == before + 1


def test_minimal_restores_patches_and_cache(minimal_runs, engine):
    """실행 뒤 원상 복구 — 감싼 함수·안전장치·시각·리스너가 원본이고, 하네스 밖 INSERT 가 그대로 돈다."""
    import services.assignment_service as assignment
    import services.cp_sat_basic as cp_sat_basic
    import services.ontology_graph.lagrangian as lagrangian
    import services.precheck as precheck
    import services.precheck.runtime_bridge as runtime_bridge
    import services.roster_create_service as rcs
    from db.models import Schedule
    from ortools.sat.python import cp_model
    from sqlalchemy.orm import Session, sessionmaker

    assert rcs.generate_roster_cp_sat is cp_sat_basic.generate_roster_cp_sat
    assert precheck.run_runtime_precheck is runtime_bridge.run_runtime_precheck
    assert lagrangian.detect_night_forced_oversupply.__qualname__ == "detect_night_forced_oversupply"
    for name in ("create_config_from_db", "create_nurses_from_db"):
        assert getattr(cp_sat_basic.CPSATBasicEngine, name).__qualname__ == f"CPSATBasicEngine.{name}"
    assert cp_model.CpSolver.solve.__qualname__ == "CpSolver.solve"
    assert isinstance(sys.modules["db.client2"].SessionLocal, sessionmaker)
    assert assignment.date is date and rcs.datetime is datetime
    assert len(engine.dispatch.after_cursor_execute) == 0 and len(engine.dispatch.before_cursor_execute) == 0
    # 하네스가 실행한 것과 같은 모양의 근무표 INSERT(created_at·updated_at = func.now() 기본값)가
    # shim 없이 돈다 — 하네스 몫(GETDATE() 등)이 캐시·연결에 남았다면 여기서 실패한다.
    with engine.connect() as conn, conn.begin() as trans:
        session = Session(bind=conn)
        session.add(Schedule(schedule_id="zz0000000000", office_id="Z9", group_id="Z9", year=2026,
                             month=1, version=1, config_id=1, created_by="acc_Z9", status="draft",
                             dropped=False, name="캐시 확인"))
        session.flush()
        session.close()
        trans.rollback()


def test_minimal_leaves_no_trace(minimal_runs, engine):
    """흔적 0 — 실행 뒤 스키마가 같고(raw 컬럼 DDL 도 롤백) 시드 행이 없다."""
    assert minimal_runs.schema_after == minimal_runs.schema_before
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM nurses WHERE nurse_id = 'N01'")).scalar() == 0
        assert conn.execute(text("SELECT COUNT(*) FROM schedules WHERE group_id = 'G1'")).scalar() == 0
