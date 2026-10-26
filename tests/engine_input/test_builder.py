"""EI-9b-1 단계 A 단위 테스트 — `build_precheck_stage` 의 지연 getter 4종(설계서 EI9_SPEC.md §2.4-1).

호출처가 아직 0 이라(9b-2 가 생기기 전) 골든 시나리오 하네스를 쓰지 않는다 — `shim_db`
(test_mssql_shim.py 재사용) 위에서 직접 행을 심어 운영 함수(`_fetch_grade_config_dict`·
`weekend_off_ids_asof`·`_load_shift_lookup`·팀 질의)와 교차 확인하고, `QueryLog`(capture.py
재사용)로 조회 수·순서를 직접 센다. app 모듈은 가드 규약대로 함수 **안에서** 지연 import 한다
(수집 시점 import 는 패키지 가드 밖 — conftest.py 머릿말 참조).
"""
from __future__ import annotations

import ast
from datetime import date
from pathlib import Path

import pytest

from .capture import QueryLog
from .test_mssql_shim import shim_db  # noqa: F401 — 픽스처 재사용
from .ward import GROUP_ID, OFFICE_ID, nurse_row, shift_row

APP_ROOT = Path(__file__).resolve().parents[2] / "app"
YEAR, MONTH = 2026, 11


def _builder():
    """`services.engine_input.builder` 모듈(지연 import — 가드 규약)."""
    from services.engine_input import builder

    return builder


def _rcs():
    """`services.roster_create_service` 모듈(지연 import — 가드 규약)."""
    from services import roster_create_service

    return roster_create_service


def _make_prelude(nurse_ids: tuple[str, ...] = ("N01", "N02")):
    """최소 `PreludeState` — ctx + 엔진 대상 간호사(dict, DB 에 넣지 않는다)."""
    from services.engine_input.model import Ctx, PreludeState

    ctx = Ctx(office_id=OFFICE_ID, group_id=GROUP_ID, year=YEAR, month=MONTH)
    nurses = [nurse_row(nid, i) for i, nid in enumerate(nurse_ids, start=1)]
    return PreludeState(ctx=ctx, nurses_for_engine=nurses)


def _attach_query_log(engine) -> QueryLog:
    """켜진(armed) `QueryLog` 를 엔진에 붙인다(테스트 끝에 `detach()` 필요)."""
    log = QueryLog()
    log.attach(engine)
    log.armed = True
    return log


def _relevant_tables(log: QueryLog) -> list[str]:
    """이 테스트가 관심 있는 4개 표만 등장 순서대로 남긴다(그 밖 하네스 조회는 걸러낸다)."""
    wanted = {"roster_grade_config", "nurse_weekendoff_period", "teams", "shifts"}
    tables: list[str] = []
    for stmt in log.statements:
        _verb, _, table_part = stmt.partition(":")
        for table in table_part.split("+"):
            if table in wanted:
                tables.append(table)
    return tables


# ── 생성 자체는 조회 0 ────────────────────────────────────────────────────


def test_build_precheck_stage_itself_queries_nothing(shim_db, engine):  # noqa: F811 — 재사용
    """`build_precheck_stage()` 호출 그 자체는 thunk 만 만든다 — DB 조회 0."""
    builder = _builder()
    log = _attach_query_log(engine)
    try:
        stage = builder.build_precheck_stage(shim_db, _make_prelude())
    finally:
        log.detach()
    assert log.statements == []
    assert (stage.grade.loaded, stage.weekend_ids.loaded,
            stage.team_rows.loaded, stage.shift_lookup.loaded) == (False, False, False, False)


# ── 값이 운영 함수와 같다 ─────────────────────────────────────────────────


def test_grade_getter_forwards_same_args_as_production(shim_db):  # noqa: F811 — 재사용
    """배선 확인(동어반복 아님 주의) — `_load_grade` 클로저가 `_fetch_grade_config_dict` 를
    **같은 인자**(db·office_id·group_id)로 부르는지. `stage.grade.get()` 자체가 그 함수를
    그대로 호출하므로 이 테스트는 '독립 재검증'이 아니라 '배선이 다른 인자로 새지 않았는가'
    를 본다(F — 이름이 '…matches_production' 이라고 운영과 다른 경로에서 같은 값이 나온다는
    뜻은 아니다. 독립 재검증은 9c `_compare_grade`(새 세션에서 다시 부른 값과 L0 대조)의 몫).
    """
    from db.models import RosterGradeConfig

    rcs = _rcs()
    builder = _builder()
    shim_db.add(RosterGradeConfig(config_id=1, office_id=OFFICE_ID, group_id=GROUP_ID,
                                  constraints_json={"1": {"D": 2}}))
    shim_db.flush()
    stage = builder.build_precheck_stage(shim_db, _make_prelude())
    assert stage.grade.get() == rcs._fetch_grade_config_dict(shim_db, OFFICE_ID, GROUP_ID)


def test_weekend_ids_getter_forwards_same_args_as_production(shim_db):  # noqa: F811 — 재사용
    """배선 확인(동어반복 아님 주의) — `_load_weekend_ids` 가 `weekend_off_ids_asof` 를 같은
    `nurse_ids`(prelude 전원)·연·월로 부르는지(F, 위 grade 테스트와 같은 성격)."""
    from db.models import NurseWeekendOffPeriod
    from services.nurse_period_resolver import weekend_off_ids_asof

    builder = _builder()
    shim_db.add(NurseWeekendOffPeriod(nurse_id="N01", valid_from=date(2024, 1, 1),
                                      valid_to=None, weekend_off=1))
    shim_db.flush()
    prelude = _make_prelude(("N01", "N02"))
    stage = builder.build_precheck_stage(shim_db, prelude)
    expected = weekend_off_ids_asof(shim_db, ["N01", "N02"], YEAR, MONTH)
    assert stage.weekend_ids.get() == expected == {"N01"}


def test_team_rows_getter_returns_active_rows_in_office_and_group(shim_db):  # noqa: F811 — 재사용
    """이름 정정(F, 리뷰 지적 LOW) — 예전 이름(`…_matches_production`)은 운영 함수를 또 불러
    비교한다는 인상을 줬지만, 실제로는 운영 함수가 없는 리터럴 필터(office_id·group_id·active)
    단언이다(비교 없음)."""
    from db.models import Team

    builder = _builder()
    shim_db.add_all([
        Team(office_id=OFFICE_ID, group_id=GROUP_ID, team_id=1, team_name="활성", active=1),
        Team(office_id=OFFICE_ID, group_id=GROUP_ID, team_id=2, team_name="비활성", active=0),
        Team(office_id=OFFICE_ID, group_id="G9", team_id=3, team_name="다른그룹", active=1),
    ])
    shim_db.flush()
    stage = builder.build_precheck_stage(shim_db, _make_prelude())
    rows = stage.team_rows.get()
    assert [r.team_id for r in rows] == [1]


def test_shift_lookup_getter_forwards_same_args_as_production(shim_db):  # noqa: F811 — 재사용
    """배선 확인(동어반복 아님 주의) — `_load_shift_lookup` 클로저가 운영과 같은
    (db, office_id, group_id) 로 `_load_shift_lookup`(rcs) 을 부르는지(F)."""
    from db.models import Shift

    rcs = _rcs()
    builder = _builder()
    shim_db.add(Shift(**shift_row("D", 1, "데이", "D", "근무", "D", 1)))
    shim_db.flush()
    stage = builder.build_precheck_stage(shim_db, _make_prelude())
    got = stage.shift_lookup.get()
    expected = rcs._load_shift_lookup(shim_db, OFFICE_ID, GROUP_ID)
    assert set(got) == set(expected) == {"D"}


# ── 1회 조회·중복 조회 0 ──────────────────────────────────────────────────


def test_each_getter_queries_exactly_once_even_called_twice(shim_db, engine):  # noqa: F811
    from db.models import RosterGradeConfig, Shift, Team

    shim_db.add(RosterGradeConfig(config_id=1, office_id=OFFICE_ID, group_id=GROUP_ID))
    shim_db.add(Team(office_id=OFFICE_ID, group_id=GROUP_ID, team_id=1, team_name="T", active=1))
    shim_db.add(Shift(**shift_row("D", 1, "데이", "D", "근무", "D", 1)))
    shim_db.flush()
    builder = _builder()
    stage = builder.build_precheck_stage(shim_db, _make_prelude(("N01",)))
    log = _attach_query_log(engine)
    try:
        for getter, table in ((stage.grade, "roster_grade_config"),
                              (stage.weekend_ids, "nurse_weekendoff_period"),
                              (stage.team_rows, "teams"), (stage.shift_lookup, "shifts")):
            getter.get()
            getter.get()
            getter.get()
            count = sum(1 for s in log.statements if f":{table}" in s or f"+{table}" in s)
            assert count == 1, (table, log.statements)
    finally:
        log.detach()


def test_unread_getter_has_zero_queries(shim_db, engine):  # noqa: F811 — 재사용
    from db.models import Team

    shim_db.add(Team(office_id=OFFICE_ID, group_id=GROUP_ID, team_id=1, team_name="T", active=1))
    shim_db.flush()
    builder = _builder()
    stage = builder.build_precheck_stage(shim_db, _make_prelude(()))
    log = _attach_query_log(engine)
    try:
        stage.team_rows.get()
    finally:
        log.detach()
    assert _relevant_tables(log) == ["teams"]
    assert (stage.grade.loaded, stage.weekend_ids.loaded, stage.shift_lookup.loaded) == (
        False, False, False)


# ── 순서는 부르는 쪽이 정한다 ─────────────────────────────────────────────


def test_caller_order_is_preserved_whatever_it_is(shim_db, engine):  # noqa: F811 — 재사용
    """순서를 뒤집어 불러도(근무코드 → 팀 → 주말휴무 → 등급) 쿼리 순서가 그대로 따라간다."""
    from db.models import RosterGradeConfig, Shift, Team

    shim_db.add(RosterGradeConfig(config_id=1, office_id=OFFICE_ID, group_id=GROUP_ID))
    shim_db.add(Team(office_id=OFFICE_ID, group_id=GROUP_ID, team_id=1, team_name="T", active=1))
    shim_db.add(Shift(**shift_row("D", 1, "데이", "D", "근무", "D", 1)))
    shim_db.flush()
    builder = _builder()
    stage = builder.build_precheck_stage(shim_db, _make_prelude(("N01",)))
    log = _attach_query_log(engine)
    try:
        stage.shift_lookup.get()
        stage.team_rows.get()
        stage.weekend_ids.get()
        stage.grade.get()
    finally:
        log.detach()
    assert _relevant_tables(log) == [
        "shifts", "teams", "nurse_weekendoff_period", "roster_grade_config"]


def test_production_order_matches_golden_subsequence(shim_db, engine):  # noqa: F811 — 재사용
    """프로덕션이 단계 A getter 를 여는 순서는 **골든 `S1.json` 의 실제 쿼리열**(`read_golden`, 손으로 적은 목록이 아니다 — 리뷰 지적 LOW)
    안에 연속 부분열로 나온다.

    EI-10g3 부터 단계 A getter 를 처음 여는 것은 `to_precheck_input` 이 아니라 precheck 앞에서 도는 첫 시도 직전 빌더다(캐시 — 이후
    precheck 입력은 캐시를 쓴다). 그래서 이 시험은 **빌더**로 getter 를 열어 본 순서(주말휴무 → 팀 → 근무코드 → 근무코드 → 주말휴무 → 등급)를 골든과 맞춘다.
    옛 순서(등급 → 주말휴무 → 팀 → 근무코드)로 부르는 `to_precheck_input` 자체의 순서는 `test_precheck_view_characterization` 이 본다."""
    from db.models import RosterGradeConfig, Shift, Team

    from .golden import read_golden

    shim_db.add(RosterGradeConfig(config_id=1, office_id=OFFICE_ID, group_id=GROUP_ID))
    shim_db.add(Team(office_id=OFFICE_ID, group_id=GROUP_ID, team_id=1, team_name="T", active=1))
    shim_db.add(Shift(**shift_row("D", 1, "데이", "D", "근무", "D", 1)))
    shim_db.flush()
    builder = _builder()
    prelude = _make_prelude(("N01",))
    stage = builder.build_precheck_stage(shim_db, prelude)
    log = _attach_query_log(engine)
    try:
        builder.build_engine_input_at_first_attempt(
            shim_db, stage, preferences=[], first_attempt_config=dict(prelude.config_dict))
    finally:
        log.detach()
    produced = _relevant_tables(log)
    assert produced == ["nurse_weekendoff_period", "teams", "shifts", "shifts", "nurse_weekendoff_period", "roster_grade_config"], produced

    golden = read_golden("S1")
    assert golden is not None
    golden_sequence: list[str] = []
    for stmt in golden["queries"]["tables"]:
        _verb, _, part = stmt.partition(":")
        for table in part.split("+"):
            if table in {"roster_grade_config", "nurse_weekendoff_period", "teams", "shifts"}:
                golden_sequence.append(table)
    width = len(produced)
    windows = range(len(golden_sequence) - width + 1)
    assert any(golden_sequence[i:i + width] == produced for i in windows), (produced, golden_sequence)


# ── 실패 시 삼키지 않는다 ─────────────────────────────────────────────────


def test_failing_getter_propagates_and_does_not_retry(shim_db):  # noqa: F811 — 재사용
    """팀 표를 깨뜨리면 `team_rows.get()` 이 그 예외를 그대로 올린다(삼키지 않음, §1.2).

    두 번째 `get()` 도 같은 예외를 다시 올리되 — '1회 조회' 이므로 재조회(두 번째 실패를
    만드는 추가 SQL)는 없다. `load_report` 에 담는 것은 이 getter 의 몫이 아니라 이를 부르는
    쪽(EI-9b-2 `build_engine_input`)의 몫이다 — 여기서는 전파만 확인한다.
    """
    from sqlalchemy import text

    builder = _builder()
    shim_db.execute(text("DROP TABLE teams"))
    stage = builder.build_precheck_stage(shim_db, _make_prelude(()))
    with pytest.raises(Exception) as first:  # noqa: PT011 — sqlite OperationalError, 정확한 타입은 무관
        stage.team_rows.get()
    assert stage.team_rows.loaded is True
    with pytest.raises(Exception) as second:  # noqa: PT011
        stage.team_rows.get()
    assert first.value is second.value


def test_lazy_generic_failure_is_cached_not_retried():
    """`Lazy` 자체 — 실패해도 loader 를 다시 부르지 않는다(§1.2 '처음 읽힐 때 1회')."""
    builder = _builder()
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("boom")

    lazy = builder.Lazy(boom)
    for _ in range(3):
        with pytest.raises(RuntimeError, match="boom"):
            lazy.get()
    assert len(calls) == 1
    assert lazy.loaded is True


# ── 모양 ────────────────────────────────────────────────────────────────


def test_precheck_stage_instance_shape(shim_db):  # noqa: F811 — 재사용
    builder = _builder()
    stage = builder.build_precheck_stage(shim_db, _make_prelude())
    for name in ("grade", "weekend_ids", "team_rows", "shift_lookup"):
        assert isinstance(getattr(stage, name), builder.Lazy)


# ══════════════════════════════════════════════════════════════════════════
# 메모리에 이미 있는 객체 4칸(B — EI-10a/b 의 view 가 stage_a 만으로 지을 길)
# ══════════════════════════════════════════════════════════════════════════


def test_precheck_stage_carries_prelude_memory_objects_as_copies(shim_db, engine):  # noqa: F811
    """`config_dict`·`nurses_for_engine`·`combined_fixed_cells`·`shift_manage_data` 는
    `prelude` 와 **값은 같고 객체는 다른** deepcopy 사본이다(BR-6) — 조회 없이 그대로 옮긴다."""
    from services.engine_input.model import Ctx, PreludeState

    builder = _builder()
    ctx = Ctx(office_id=OFFICE_ID, group_id=GROUP_ID, year=YEAR, month=MONTH)
    prelude = PreludeState(
        ctx=ctx, config_dict={"day_req": 2}, nurses_for_engine=[{"nurse_id": "N01"}],
        combined_fixed_cells=[{"nurse_id": "N01", "day": 1}], shift_manage_data=[{"main_code": "D"}],
    )
    log = _attach_query_log(engine)
    try:
        stage = builder.build_precheck_stage(shim_db, prelude)
    finally:
        log.detach()
    assert log.statements == []  # 메모리 사본만 뜬다 — 조회 0
    assert stage.config_dict == prelude.config_dict and stage.config_dict is not prelude.config_dict
    assert (stage.nurses_for_engine == prelude.nurses_for_engine
            and stage.nurses_for_engine is not prelude.nurses_for_engine)
    assert (stage.combined_fixed_cells == prelude.combined_fixed_cells
            and stage.combined_fixed_cells is not prelude.combined_fixed_cells)
    assert (stage.shift_manage_data == prelude.shift_manage_data
            and stage.shift_manage_data is not prelude.shift_manage_data)
    # 사본이 진짜 격리돼 있는지 — stage 쪽을 바꿔도 prelude 원본은 그대로.
    stage.config_dict["day_req"] = 99
    stage.nurses_for_engine[0]["nurse_id"] = "바뀜"
    assert prelude.config_dict["day_req"] == 2
    assert prelude.nurses_for_engine[0]["nurse_id"] == "N01"


def test_precheck_stage_ctx_fills_days_from_prelude_ctx(shim_db):  # noqa: F811
    """`stage.ctx` 는 `prelude.ctx`(`days` 없음) 와 나머지 칸이 같고 `days` 만 채워진 사본이다
    (리뷰 지적 MEDIUM — `to_precheck_input`/`presolve_view` 가 `stage_a` 만으로 지어지려면
    연·월·일수 묶음이 필요한데 `prelude.ctx` 에는 `days` 가 없었다)."""
    import calendar

    builder = _builder()
    prelude = _make_prelude()
    stage = builder.build_precheck_stage(shim_db, prelude)
    assert prelude.ctx.days is None
    assert stage.ctx.days == calendar.monthrange(YEAR, MONTH)[1]
    assert (stage.ctx.office_id, stage.ctx.group_id, stage.ctx.year, stage.ctx.month) == (
        OFFICE_ID, GROUP_ID, YEAR, MONTH)


# ── import 모양 ──────────────────────────────────────────────────────────


def test_builder_has_no_top_level_rcs_import():
    """`fixed_cut.py`·`raw.py` 와 같은 가드 — `roster_create_service` 최상위 import 금지."""
    import services.engine_input.builder as builder_module

    tree = ast.parse(Path(builder_module.__file__).read_text())
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module != "services.roster_create_service", ast.dump(node)
        elif isinstance(node, ast.Import):
            assert all("roster_create_service" not in alias.name for alias in node.names)


def _iter_app_py_files():
    for path in APP_ROOT.rglob("*.py"):
        # engine_input 패키지 자신은 제외 — 9b-2 가 나중에 여기서 부른다.
        if "engine_input" in path.relative_to(APP_ROOT).parts:
            continue
        yield path


def _imports_builder_module(tree: ast.Module) -> str | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "services.engine_input.builder":
                    return alias.name
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "services.engine_input.builder":
                return module
            if module in ("services.engine_input", "..engine_input", ".engine_input"):
                for alias in node.names:
                    if alias.name == "builder":
                        return f"{module}.builder"
    return None


#: 빌더를 부르는 프로덕션 호출처 — EI-13a-2 가 `roster_create_service` 에서 첫 시도 직전 1회 호출한다(그 밖은 아직 0).
BUILDER_CALLERS = {"services/roster_create_service.py"}


def test_no_app_code_imports_builder_yet():
    """빌더 호출처는 `BUILDER_CALLERS`(EI-13a-2 — rcs 첫 시도 직전) 뿐이다 — engine_input 패키지 밖 그 밖의 누구도 `builder` 를 안 쓴다."""
    offenders = []
    for path in _iter_app_py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        hit = _imports_builder_module(tree)
        if hit and path.relative_to(APP_ROOT).as_posix() not in BUILDER_CALLERS:
            offenders.append(f"{path.relative_to(APP_ROOT)}: {hit}")
    assert offenders == [], offenders
