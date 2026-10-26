"""EI-13a 리뷰 MEDIUM 2: 삼켜진 읽기 실패(금지 원티드·등급)는 AttemptSource 를 막아 시도마다 재조회로 돌아간다."""
from __future__ import annotations

from datetime import date

from .test_mssql_shim import shim_db  # noqa: F401
from .test_solve_stage import _builder, _make_prelude, _rcs, _seed
from .ward import GROUP_ID, base_ward

YEAR, MONTH = 2026, 11


def _build(shim_db, monkeypatch, fail_entity=None, fail_execute=False):  # noqa: F811
    from db.models import BannedWantedEntry

    builder = _builder()
    ward = base_ward(4)
    _seed(shim_db, ward)
    shim_db.add(BannedWantedEntry(group_id=GROUP_ID, nurse_id="N01", year=YEAR, month=MONTH,
                                  shift_date=date(YEAR, MONTH, 5), banned_shift_ids=["N"],
                                  is_applied=True, source="hn"))
    shim_db.flush()
    prelude = _make_prelude(ward)
    stage_a = builder.build_precheck_stage(shim_db, prelude)
    real_query, real_execute = shim_db.query, shim_db.execute
    if fail_entity == "banned":
        def flaky_query(*entities, **kw):
            if entities and entities[0] is BannedWantedEntry:
                raise RuntimeError("transient db error")
            return real_query(*entities, **kw)
        monkeypatch.setattr(shim_db, "query", flaky_query)
    if fail_execute:
        def flaky_execute(stmt, *a, **kw):
            if "roster_grade_config" in str(stmt):
                raise RuntimeError("transient db error")
            return real_execute(stmt, *a, **kw)
        monkeypatch.setattr(shim_db, "execute", flaky_execute)
    return builder.build_engine_input_at_first_attempt(
        shim_db, stage_a, preferences=[], first_attempt_config=dict(prelude.config_dict)), prelude


def test_swallowed_banned_failure_blocks_attempt_source(shim_db, monkeypatch):  # noqa: F811
    (ei, src), prelude = _build(shim_db, monkeypatch, fail_entity="banned")
    assert "stage_b.banned" in ei.load_report.errors
    assert src is None            # 시도마다 재조회 경로 — 일시 오류가 사다리 전 시도에 남지 않는다


def test_swallowed_grade_failure_blocks_attempt_source(shim_db, monkeypatch):  # noqa: F811
    (ei, src), _ = _build(shim_db, monkeypatch, fail_execute=True)
    assert "grade" in ei.load_report.errors
    assert src is None


def test_healthy_reads_still_build_attempt_source(shim_db, monkeypatch):  # noqa: F811
    (ei, src), prelude = _build(shim_db, monkeypatch)
    assert src is not None
    assert src.banned["forbidden"]["N01"][4] == ["N"]


def test_helpers_report_swallowed_only_when_asked(shim_db, monkeypatch):  # noqa: F811
    from db.models import BannedWantedEntry

    rcs = _rcs()
    builder = _builder()
    ward = base_ward(4)
    _seed(shim_db, ward)
    prelude = _make_prelude(ward)
    monkeypatch.setattr(shim_db, "query", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    views = builder._as_nurse_views(prelude.nurses_for_engine)
    assert rcs._build_banned_wanted_constraints(shim_db, prelude.ctx, prelude.ctx, views) == {"forced_off": {}, "forbidden": {}}
    sink: list = []
    rcs._build_banned_wanted_constraints(shim_db, prelude.ctx, prelude.ctx, views, sink)
    assert len(sink) == 1
    assert BannedWantedEntry is not None
