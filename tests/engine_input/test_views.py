"""EI-10a·10b 단위 테스트 — `services.engine_input.views`(`EI10AB_SPEC.md` §4.3).

10a 몫은 `stage_a_at_precheck`·`presolve_view`, 10b 몫은 `to_precheck_input`(U1~U8 — 리뷰
반영으로 10a 뒤에 이 파일에 합류했다). 골든 하네스를 쓰지 않고 `shim_db`(test_mssql_shim.py
재사용)·직접 지은 `PrecheckStage`로 운영 함수와 교차 확인한다 — `test_builder.py`와 같은 성격이다.
"""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from .capture import QueryLog
from .test_mssql_shim import shim_db  # noqa: F401 — 픽스처 재사용
from .ward import GROUP_ID, OFFICE_ID, nurse_row

APP_ROOT = Path(__file__).resolve().parents[2] / "app"
YEAR, MONTH = 2026, 11


def _views():
    from services.engine_input import views

    return views


def _builder():
    from services.engine_input import builder

    return builder


def _model():
    from services.engine_input import model

    return model


def _make_ctx(**over):
    model = _model()
    base = dict(office_id=OFFICE_ID, group_id=GROUP_ID, year=YEAR, month=MONTH, days=30)
    base.update(over)
    return model.Ctx(**base)


def _shift_ns(**over) -> SimpleNamespace:
    """`shift_lookup` 값 — `_shift_defs_from_lookup` 이 `getattr` 로 읽으므로 속성 접근형."""
    base = dict(shift_id="D", default_shift="D", shift_gb="D", type="근무", show_in_preference=True)
    base.update(over)
    return SimpleNamespace(**base)


def _make_stage(*, live=None, nurses_for_engine=None, shift_manage_data=None,
               combined_fixed_cells=None, shift_lookup=None, ctx=None):
    """`presolve_view` 단위 테스트용 — DB 없이 `Lazy` 를 직접 감싼 `PrecheckStage`."""
    builder = _builder()
    return builder.PrecheckStage(
        grade=builder.Lazy(lambda: {}),
        weekend_ids=builder.Lazy(lambda: set()),
        team_rows=builder.Lazy(lambda: []),
        shift_lookup=builder.Lazy(lambda: shift_lookup if shift_lookup is not None else {}),
        ctx=ctx or _make_ctx(),
        nurses_for_engine=nurses_for_engine or [],
        combined_fixed_cells=combined_fixed_cells or [],
        shift_manage_data=shift_manage_data or [],
        live=live,
    )


# ── import 모양(builder·fixed_cut 와 같은 가드) ──────────────────────────


def test_views_module_has_no_top_level_rcs_import():
    """`builder.py`·`fixed_cut.py` 와 같은 가드 — `roster_create_service` 최상위 import 금지."""
    import services.engine_input.views as views_module

    tree = ast.parse(Path(views_module.__file__).read_text(encoding="utf-8"))
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module != "services.roster_create_service", ast.dump(node)
        elif isinstance(node, ast.Import):
            assert all("roster_create_service" not in alias.name for alias in node.names)


def test_rcs_no_longer_references_build_presolve_view():
    """10a 전환 뒤 — rcs 소스에 옛 이름(`build_presolve_view`·`_shift_defs_for_normalizer`)이
    남아 있으면 안 된다(죽은 import·주석 미정리를 잡는다)."""
    rcs_path = APP_ROOT / "services" / "roster_create_service.py"
    source = rcs_path.read_text(encoding="utf-8")
    assert "build_presolve_view" not in source
    assert "_shift_defs_for_normalizer" not in source


def test_presolve_block_no_longer_defines_build_presolve_view():
    """`presolve_block.py` 에서 옛 함수 2종이 삭제됐다(§3.5 step 6) — `assemble_presolve_view`
    로 완전히 대체됐는지 확인."""
    import services.precheck.presolve_block as presolve_block

    assert not hasattr(presolve_block, "build_presolve_view")
    assert not hasattr(presolve_block, "_shift_defs_for_normalizer")
    assert hasattr(presolve_block, "assemble_presolve_view")
    assert hasattr(presolve_block, "_shift_defs_from_lookup")


# ── stage_a_at_precheck — 조회 0 · 실물 참조 · ORM/_sa_ 제거 ──────────────


def test_stage_a_at_precheck_queries_nothing(shim_db, engine):  # noqa: F811 — 재사용
    """조립 자체는 DB 를 읽지 않는다(`vars()`·얕은 사본·`deepcopy` 뿐 — §EI10AB_SPEC KD-2)."""
    views = _views()
    nurses = [SimpleNamespace(**nurse_row("N01", 1))]
    log = QueryLog()
    log.attach(engine)
    log.armed = True
    try:
        stage = views.stage_a_at_precheck(
            shim_db, office_id=OFFICE_ID, group_id=GROUP_ID, year=YEAR, month=MONTH,
            config_id=1, job_id="job1", config_dict={"n2n_min_gap": 4},
            nurses_for_engine=nurses, combined_fixed_cells=[], shift_manage_data=[],
        )
    finally:
        log.detach()
    assert log.statements == []
    assert (stage.grade.loaded, stage.weekend_ids.loaded,
            stage.team_rows.loaded, stage.shift_lookup.loaded) == (False, False, False, False)


def test_stage_a_at_precheck_live_is_the_same_objects(shim_db):  # noqa: F811 — 재사용
    """`live` 는 호출부가 넘긴 그 리스트 객체 그대로다(§0-1 — 복사하면 `view.nurses is
    nurses_for_engine` 을 못 지킨다)."""
    views = _views()
    nurses = [SimpleNamespace(**nurse_row("N01", 1))]
    fixed_cells = [{"nurse_index": 0, "day_index": 1, "shift": "D"}]
    shift_manage = [{"main_code": "D", "codes": ["D"], "_sa_instance_state": object()}]
    stage = views.stage_a_at_precheck(
        shim_db, office_id=OFFICE_ID, group_id=GROUP_ID, year=YEAR, month=MONTH,
        config_id=None, job_id=None, config_dict={}, nurses_for_engine=nurses,
        combined_fixed_cells=fixed_cells, shift_manage_data=shift_manage,
    )
    assert stage.live is not None
    assert stage.live.nurses_for_engine is nurses
    assert stage.live.combined_fixed_cells is fixed_cells
    assert stage.live.shift_manage_data is shift_manage


def test_stage_a_at_precheck_prelude_copies_strip_sa_and_orm(shim_db):  # noqa: F811 — 재사용
    """프렐류드 사본(`stage.nurses_for_engine`·`shift_manage_data`)은 `_sa_*` 키와 ORM 값이
    빠진 평범한 dict 다(model.py 'ORM 금지') — `live` 쪽(실물)과는 다른 칸이다."""
    views = _views()
    orm_like = SimpleNamespace(_sa_instance_state="fake-state")
    nurse = SimpleNamespace(nurse_id="N01", grade=1, _sa_instance_state="fake-state",
                            team=orm_like)
    shift_manage_row = {"main_code": "D", "codes": ["D"], "_sa_instance_state": "fake-state"}
    stage = views.stage_a_at_precheck(
        shim_db, office_id=OFFICE_ID, group_id=GROUP_ID, year=YEAR, month=MONTH,
        config_id=None, job_id=None, config_dict={"_sa_instance_state": "x", "n2n_min_gap": 4},
        nurses_for_engine=[nurse], combined_fixed_cells=[], shift_manage_data=[shift_manage_row],
    )
    assert stage.nurses_for_engine == [{"nurse_id": "N01", "grade": 1}]
    assert stage.shift_manage_data == [{"main_code": "D", "codes": ["D"]}]
    assert stage.config_dict == {"n2n_min_gap": 4}


def test_stage_a_at_precheck_ctx_has_days_filled(shim_db):  # noqa: F811 — 재사용
    """`stage.ctx.days` 는 `calendar.monthrange` 로 보강된다(2026-11 → 30)."""
    views = _views()
    stage = views.stage_a_at_precheck(
        shim_db, office_id=OFFICE_ID, group_id=GROUP_ID, year=2026, month=11,
        config_id=None, job_id=None, config_dict={}, nurses_for_engine=[],
        combined_fixed_cells=[], shift_manage_data=[],
    )
    assert stage.ctx.days == 30
    assert (stage.ctx.office_id, stage.ctx.group_id) == (OFFICE_ID, GROUP_ID)


def test_weekend_ids_getter_uses_live_nurses_at_get_time(shim_db):  # noqa: F811 — 재사용
    """`live` 가 있으면 주말휴무 getter 가 **그 실물**(`live.nurses_for_engine`)에서
    `nurse_id` 를 `getattr` 로 읽는다 — 사본(prelude dict)이 아니라(§2.3)."""
    captured: dict = {}
    live_nurse = SimpleNamespace(nurse_id="LIVE01")
    prelude_nurse = {"nurse_id": "COPY01"}

    def fake_weekend_off_ids_asof(db, nurse_ids, year, month):
        captured["nurse_ids"] = list(nurse_ids)
        return set()

    import services.nurse_period_resolver as period_resolver
    orig = period_resolver.weekend_off_ids_asof
    period_resolver.weekend_off_ids_asof = fake_weekend_off_ids_asof
    try:
        builder = _builder()
        prelude = _model().PreludeState(ctx=_make_ctx(), nurses_for_engine=[prelude_nurse])
        live = builder.PrecheckLive(
            nurses_for_engine=[live_nurse], combined_fixed_cells=[], shift_manage_data=[])
        stage = builder.build_precheck_stage(shim_db, prelude, live=live)
        stage.weekend_ids.get()
    finally:
        period_resolver.weekend_off_ids_asof = orig
    assert captured["nurse_ids"] == ["LIVE01"], "live 가 있으면 live 실물에서 id 를 읽어야 한다"


def test_weekend_ids_getter_uses_prelude_copy_when_no_live(shim_db):  # noqa: F811 — 재사용
    """`live` 가 없으면(오프라인) 지금처럼 `prelude.nurses_for_engine` 사본에서 읽는다."""
    import services.nurse_period_resolver as period_resolver

    captured: dict = {}

    def fake_weekend_off_ids_asof(db, nurse_ids, year, month):
        captured["nurse_ids"] = list(nurse_ids)
        return set()

    orig = period_resolver.weekend_off_ids_asof
    period_resolver.weekend_off_ids_asof = fake_weekend_off_ids_asof
    try:
        builder = _builder()
        prelude = _model().PreludeState(
            ctx=_make_ctx(), nurses_for_engine=[{"nurse_id": "COPY01"}])
        stage = builder.build_precheck_stage(shim_db, prelude)
        stage.weekend_ids.get()
    finally:
        period_resolver.weekend_off_ids_asof = orig
    assert captured["nurse_ids"] == ["COPY01"]


# ── presolve_view — 단계 A 없으면 RuntimeError ────────────────────────────


def test_presolve_view_requires_stage_a():
    views = _views()
    with pytest.raises(RuntimeError):
        views.presolve_view(None, {})


# ── presolve_view — 운영(live): 실물 참조·객체 정체성 ─────────────────────


def test_presolve_view_live_uses_real_nurses_and_config_identity():
    """`live` 가 있으면 `view.nurses is live.nurses_for_engine` · `view.config_dict is
    config_dict`(§2.1 — 재조립 없이 그대로 전달)."""
    views = _views()
    nurses = [SimpleNamespace(**nurse_row("N01", 1)), SimpleNamespace(**nurse_row("N02", 2))]
    shift_manage = [{"main_code": "D", "codes": ["D"]}, {"main_code": "N", "codes": ["N"]}]
    fixed_cells = [{"nurse_index": 0, "day_index": 1, "shift": "D"}]
    live = _builder().PrecheckLive(
        nurses_for_engine=nurses, combined_fixed_cells=fixed_cells, shift_manage_data=shift_manage)
    stage = _make_stage(live=live, shift_lookup={"D": _shift_ns(shift_id="D")})
    config_dict = {"daily_shift_requirements": {"D": 1, "E": 1, "N": 1}}

    view = views.presolve_view(stage, config_dict)

    assert view.nurses is nurses
    assert view.config_dict is config_dict
    assert view.num_days == stage.ctx.days
    assert (view.year, view.month) == (YEAR, MONTH)
    assert view.code2main["D"] == "D"
    assert view.id_to_main.get("D") == "D"


def test_presolve_view_does_not_mutate_config_dict():
    views = _views()
    live = _builder().PrecheckLive(nurses_for_engine=[], combined_fixed_cells=[], shift_manage_data=[])
    stage = _make_stage(live=live, shift_lookup={})
    config_dict = {"n2n_min_gap": 4}
    before = dict(config_dict)
    views.presolve_view(stage, config_dict)
    assert config_dict == before


# ── presolve_view — 오프라인(live 없음): 사본을 `_as_nurse_views` 로 감싼다 ──


def test_presolve_view_offline_wraps_copy_as_namespace():
    """`live` 가 없으면 `stage.nurses_for_engine`(dict 사본)을 속성 접근형으로 감싼다 —
    `_build_engine_nurse_index_map` 이 `n.nurse_id` 속성 접근을 요구해서다."""
    views = _views()
    stage = _make_stage(
        nurses_for_engine=[nurse_row("N01", 1)],
        combined_fixed_cells=[{"nurse_index": 0, "day_index": 1, "shift": "D"}],
        shift_lookup={"D": _shift_ns(shift_id="D")},
    )
    view = views.presolve_view(stage, {})
    assert view.nurses is not stage.nurses_for_engine
    assert [n.nurse_id for n in view.nurses] == ["N01"]
    assert view.fixed_cells == [{"nurse_id": "N01", "day_index": 1, "shift": "D"}]


# ── presolve_view — 근무코드 조회 순서(code2main 먼저) ────────────────────


def test_presolve_view_code2main_failure_skips_shift_lookup_query():
    """code2main 이 먼저 실패하면 `shift_lookup.get()`(근무코드 조회)이 안 불린다(C5·쿼리 수 보존)."""
    import services.roster_create_service as rcs

    views = _views()
    live = _builder().PrecheckLive(nurses_for_engine=[], combined_fixed_cells=[], shift_manage_data=[])
    calls: list[str] = []

    def failing_loader():
        calls.append("shift_lookup")
        return {}

    stage = _builder().PrecheckStage(
        grade=_builder().Lazy(lambda: {}), weekend_ids=_builder().Lazy(lambda: set()),
        team_rows=_builder().Lazy(lambda: []), shift_lookup=_builder().Lazy(failing_loader),
        ctx=_make_ctx(), live=live,
    )
    orig = rcs._build_code_to_main_map
    rcs._build_code_to_main_map = lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        with pytest.raises(RuntimeError, match="boom"):
            views.presolve_view(stage, {})
    finally:
        rcs._build_code_to_main_map = orig
    assert calls == [], "code2main 실패 뒤에는 shift_lookup 조회가 나가면 안 된다"
    assert stage.shift_lookup.loaded is False


# ── to_precheck_input — EI-10b ───────────────────────────────────────────


def _make_precheck_stage(*, grade=None, weekend_ids=None, team_rows=None,
                        nurses_for_engine=None, combined_fixed_cells=None, live=None, ctx=None):
    """`to_precheck_input` 단위 테스트용 — grade·weekend_ids·team_rows 를 개별 제어한다."""
    builder = _builder()

    def _lazy(factory, default):
        if factory is None:
            return builder.Lazy(lambda: default)
        return builder.Lazy(factory)

    return builder.PrecheckStage(
        grade=_lazy(grade, {}), weekend_ids=_lazy(weekend_ids, set()),
        team_rows=_lazy(team_rows, []), shift_lookup=builder.Lazy(lambda: {}),
        ctx=ctx or _make_ctx(), nurses_for_engine=nurses_for_engine or [],
        combined_fixed_cells=combined_fixed_cells or [], live=live,
    )


def test_to_precheck_input_requires_stage_a():
    views = _views()
    with pytest.raises(RuntimeError):
        views.to_precheck_input(None, {})


def test_to_precheck_input_order_grade_then_weekend_then_team():
    """getter 읽힘 순서 = 등급 → 주말휴무 → 팀(지금 rcs 와 같은 순서)."""
    order: list[str] = []
    live = _builder().PrecheckLive(
        nurses_for_engine=[SimpleNamespace(**nurse_row("N01", 1))],
        combined_fixed_cells=[], shift_manage_data=[])
    stage = _make_precheck_stage(
        grade=lambda: (order.append("grade"), {})[1],
        weekend_ids=lambda: (order.append("weekend"), set())[1],
        team_rows=lambda: (order.append("team"), [])[1],
        live=live,
    )
    _views().to_precheck_input(stage, {})
    assert order == ["grade", "weekend", "team"]


def test_to_precheck_input_grade_failure_propagates():
    """등급 getter 예외는 그대로 전파된다(지금 rcs 의 바깥 except 와 같은 범위)."""
    stage = _make_precheck_stage(grade=lambda: (_ for _ in ()).throw(RuntimeError("grade boom")))
    with pytest.raises(RuntimeError, match="grade boom"):
        _views().to_precheck_input(stage, {})


def test_to_precheck_input_weekend_failure_returns_all_false_without_log():
    """주말휴무 getter 예외는 로그 없이 `set()` 으로 떨어져 전원 `is_weekend_off=False`."""
    logs: list[str] = []
    live = _builder().PrecheckLive(
        nurses_for_engine=[SimpleNamespace(**nurse_row("N01", 1)),
                          SimpleNamespace(**nurse_row("N02", 2))],
        combined_fixed_cells=[], shift_manage_data=[])
    stage = _make_precheck_stage(
        weekend_ids=lambda: (_ for _ in ()).throw(RuntimeError("weekend boom")), live=live)
    call = _views().to_precheck_input(stage, {}, log=logs.append)
    assert logs == []
    assert all(nd["is_weekend_off"] is False for nd in call.nurses_dict)


def test_to_precheck_input_team_min_already_present_skips_team_getter():
    """`team_min_by_team` 이 이미 config 에 있으면 팀 getter 를 **읽지 않는다**(§2.1 5단계)."""
    stage = _make_precheck_stage(
        team_rows=lambda: (_ for _ in ()).throw(RuntimeError("team getter 가 불리면 안 된다")))
    config = {"team_min_by_team": {"1": {"D": 1}}}
    call = _views().to_precheck_input(stage, config)
    assert stage.team_rows.loaded is False
    assert call.config_dict["team_min_by_team"] is config["team_min_by_team"]


def test_to_precheck_input_team_failure_logged_and_swallowed():
    """팀 조회·빌드 실패는 지금과 같은 문구로 로그만 남기고 삼킨다."""
    logs: list[str] = []
    stage = _make_precheck_stage(team_rows=lambda: (_ for _ in ()).throw(RuntimeError("team boom")))
    call = _views().to_precheck_input(stage, {}, log=logs.append)
    assert logs == ["[Precheck] team_min 로딩 실패(무시): team boom"]
    assert "team_min_by_team" not in call.config_dict


def test_to_precheck_input_config_is_shallow_copy():
    """`precheck_config` 는 `config_dict` 의 얕은 사본 — 최상위는 다른 객체, 입력은 불변."""
    stage = _make_precheck_stage()
    nested = {"nested": "value"}
    config = {"n2n_min_gap": 4, "nested_marker": nested}
    before = dict(config)
    call = _views().to_precheck_input(stage, config)
    assert call.config_dict is not config
    assert call.config_dict["nested_marker"] is nested
    assert config == before


def test_to_precheck_input_projection_key_order():
    """투영 키 순서 = `ONTOLOGY_NURSE_FIELDS` + `db_id`·`name`·`is_weekend_off`."""
    views = _views()
    live = _builder().PrecheckLive(
        nurses_for_engine=[SimpleNamespace(**nurse_row("N01", 1))],
        combined_fixed_cells=[], shift_manage_data=[])
    stage = _make_precheck_stage(live=live)
    call = views.to_precheck_input(stage, {})
    expected = [*views.ONTOLOGY_NURSE_FIELDS, "db_id", "name", "is_weekend_off"]
    assert list(call.nurses_dict[0].keys()) == expected
    assert call.nurses_dict[0]["db_id"] == "N01"
    assert call.nurses_dict[0]["name"] == "N01"


def test_to_precheck_input_fixed_cells_translated_and_year_month_from_ctx():
    """`fixed_cells` 는 번역본 `[{nurse_id, day_index, shift}]`(EI-10d — 엔진 색인·원코드를 precheck 가
    읽는 모양으로) · 원본 `combined_fixed_cells` 는 건드리지 않는다 · `year`/`month` 는 `stage.ctx`.

    10b 에서는 실물을 그대로 넘겼다(`is`) — 그것이 결함 ②(bridge 가 칸을 전부 버림)의 원인이었다.
    """
    fixed_cells = [{"nurse_index": 0, "day_index": 1, "shift": "D"}]
    before = [dict(cell) for cell in fixed_cells]
    live = _builder().PrecheckLive(
        nurses_for_engine=[SimpleNamespace(**nurse_row("N01", 1))], combined_fixed_cells=fixed_cells,
        shift_manage_data=[])
    stage = _make_precheck_stage(live=live, ctx=_make_ctx(year=2027, month=3))
    call = _views().to_precheck_input(stage, {})
    assert call.fixed_cells == [{"nurse_id": "N01", "day_index": 1, "shift": "D"}]
    assert call.fixed_cells is not fixed_cells and fixed_cells == before
    assert (call.year, call.month) == (2027, 3)


def _stage_with_cells(cells, *, offline=False):
    """고정 칸 번역 테스트용 단계 A — 간호사 N01(엔진 색인 0)·N02(1). `offline=True` 면 `live` 없이 사본 경로."""
    nurses = [SimpleNamespace(**nurse_row("N01", 1)), SimpleNamespace(**nurse_row("N02", 2))]
    if offline:
        return _make_precheck_stage(
            nurses_for_engine=[dict(nurse_row("N01", 1)), dict(nurse_row("N02", 2))], combined_fixed_cells=cells)
    live = _builder().PrecheckLive(
        nurses_for_engine=nurses, combined_fixed_cells=cells, shift_manage_data=[])
    return _make_precheck_stage(live=live)


_CELLS_ENGINE = [
    {"nurse_index": 1, "day_index": 4, "shift": "연", "shift_type": "휴가", "fixed_source": "special_fixed"},
    {"nurse_index": 0, "day_index": 2, "shift": "OFF", "shift_type": "근무", "fixed_source": "fixed_wanted"},
    {"nurse_index": 0, "day_index": 7, "shift": "M", "shift_type": "근무", "fixed_source": "fixed_wanted"},
]


def test_to_precheck_input_fixed_cells_follow_use_mid_of_config():
    """고정 M 은 병동이 MID 를 쓸 때(`config_dict['use_mid']`)만 번역본에 남는다 — 엔진도 shift_types 밖 코드는 버린다."""
    off = _views().to_precheck_input(_stage_with_cells(_CELLS_ENGINE), {"use_mid": False})
    on = _views().to_precheck_input(_stage_with_cells(_CELLS_ENGINE), {"use_mid": True})
    assert off.fixed_cells == [{"nurse_id": "N02", "day_index": 4, "shift": "O"},
                              {"nurse_id": "N01", "day_index": 2, "shift": "O"}]
    assert on.fixed_cells == off.fixed_cells + [{"nurse_id": "N01", "day_index": 7, "shift": "M"}]


def test_to_precheck_input_fixed_cells_offline_stage_matches_live():
    """`live` 없는 사본 경로(오프라인 — `_as_nurse_views` 로 감싼 간호사)도 같은 번역본을 낸다."""
    live_call = _views().to_precheck_input(_stage_with_cells(_CELLS_ENGINE), {})
    offline_call = _views().to_precheck_input(_stage_with_cells(_CELLS_ENGINE, offline=True), {})
    assert offline_call.fixed_cells == live_call.fixed_cells != []


def test_to_precheck_input_without_fixed_cells_gives_empty_list():
    """칸이 없으면 번역도 빈 목록이다(S1 같은 병동 — L0 `fixed_cells == []` 가 그대로다)."""
    assert _views().to_precheck_input(_stage_with_cells([]), {}).fixed_cells == []


def test_to_precheck_input_fixed_cell_translation_failure_logged_and_swallowed(monkeypatch):
    """번역이 예외를 내면 로그 한 줄을 남기고 칸 없이(빈 목록) 판정한다 — 생성을 막지 않는다(팀 getter 와 같은 폭).

    10d 이전 precheck 는 칸을 한 개도 못 봤으므로 '칸 없이 판정'은 그때와 같은 입력이다.
    """
    logs: list[str] = []

    def boom(*_args, **_kwargs):
        raise RuntimeError("translate boom")

    monkeypatch.setattr(_views(), "precheck_fixed_cells", boom)
    call = _views().to_precheck_input(_stage_with_cells(_CELLS_ENGINE), {}, log=logs.append)
    assert call.fixed_cells == []
    assert logs == ["[Precheck] 고정 칸 변환 실패(무시 — 고정 칸 없이 판정): RuntimeError: translate boom"]


def test_to_precheck_input_kwargs_matches_run_runtime_precheck_signature():
    """`PrecheckCall.kwargs()` 는 `run_runtime_precheck` 를 키워드로 그대로 bind 할 수 있다."""
    import inspect

    from services.precheck import run_runtime_precheck

    stage = _make_precheck_stage()
    call = _views().to_precheck_input(stage, {})
    bound = inspect.signature(run_runtime_precheck).bind(**call.kwargs())
    assert set(bound.arguments) == {
        "nurses_dict", "config_dict", "grade_config", "fixed_cells", "year", "month",
        "stop_on_config_error",
    }
    assert call.kwargs()["stop_on_config_error"] is False


def test_to_precheck_input_projection_reads_via_getattr_not_dict():
    """U6 — 투영은 `getattr` 로만 읽는다(`__dict__` 에 없고 property 로만 있는 값도 실린다) —
    실물 ORM 경로(만료 후 재적재되는 지연 속성 포함)를 보장한다. `vars(nurse).get(field)` 로
    바꾸면(예: 오프라인 사본 경로와 통일하려는 리팩터) 그런 속성이 조용히 `None` 이 된다
    (리뷰 지적 MEDIUM — 지금 골든에 그런 간호사가 없어 골든·특성화만으로는 못 잡는다)."""

    class LazyGradeNurse:
        """`grade` 가 `__dict__` 에 없고 property 로만 노출 — 만료 뒤 재적재되는 ORM
        속성(또는 계산 속성)을 흉내낸다."""

        def __init__(self, nurse_id: str) -> None:
            self.nurse_id = nurse_id

        @property
        def grade(self) -> int:
            return 9

    nurse = LazyGradeNurse("N01")
    assert "grade" not in vars(nurse), "전제 — grade 는 __dict__ 에 없어야 한다"
    live = _builder().PrecheckLive(
        nurses_for_engine=[nurse], combined_fixed_cells=[], shift_manage_data=[])
    stage = _make_precheck_stage(live=live)
    call = _views().to_precheck_input(stage, {})
    assert call.nurses_dict[0]["grade"] == 9, "getattr 로 읽어야 property 값이 실린다"


def test_presolve_view_shift_lookup_consumed_via_stage_cache():
    """`presolve_view` 는 `stage.shift_lookup.get` 을 그대로 넘긴다 — 같은 조회를 다시 하지
    않고 이미 캐시된 `Lazy` 를 쓴다(1회만 호출됨을 호출 카운터로 확인)."""
    calls: list[int] = []

    def loader():
        calls.append(1)
        return {"D": _shift_ns(shift_id="D")}

    views = _views()
    live = _builder().PrecheckLive(nurses_for_engine=[], combined_fixed_cells=[], shift_manage_data=[])
    stage = _builder().PrecheckStage(
        grade=_builder().Lazy(lambda: {}), weekend_ids=_builder().Lazy(lambda: set()),
        team_rows=_builder().Lazy(lambda: []), shift_lookup=_builder().Lazy(loader),
        ctx=_make_ctx(), live=live,
    )
    views.presolve_view(stage, {})
    views.presolve_view(stage, {})
    assert calls == [1], "shift_lookup 은 Lazy 캐시로 한 번만 조회돼야 한다"
