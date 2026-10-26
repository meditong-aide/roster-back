"""EI-13a-1 읽기 메모 seam 시험(설계 ei13_15/A/SPEC.md §2).

`build_allowed_shift_type_constraints`·`build_cross_month_constraints` 의 `reads` 인자:
  - 기본값(None)이면 지금과 같고,
  - 메모를 채운 뒤 설정만 바꿔 다시 부르면 조회 0 으로 `reads=None` 새 호출과 같은 결과가 나온다.
app 모듈은 가드 규약대로 함수 안에서 지연 import 한다.
"""
from __future__ import annotations

import copy
from datetime import date, datetime

import pytest
from sqlalchemy import text

from .capture import QueryLog
from .test_mssql_shim import shim_db  # noqa: F401 — 픽스처 재사용
from .test_solve_stage import _make_ctx, _make_prelude, _rcs
from .ward import (
    GROUP_ID,
    OFFICE_ID,
    OTHER_GROUP_ID,
    RAW_COLUMN_DDL,
    base_ward,
    nurse_row,
    seed_ward,
    stamp,
)

YEAR, MONTH = 2026, 11


def _s7_tails():
    from .scenario_defs import _s7_prev_schedules

    return _s7_prev_schedules


def _g2_prev(db) -> None:
    """상대 병동 G2 의 10월 근무표 — 인바운드 X01(이 병동 전월 없음)·복귀 N07 의 꼬리 출처."""
    from db.models import Schedule, ScheduleEntry

    db.add(Schedule(schedule_id="g2oct0000001", office_id=OFFICE_ID, group_id=OTHER_GROUP_ID, year=2026,
                    month=10, version=1, config_id=None, created_by="acc_HN01", status="issued",
                    dropped=False, name="G2 10월", **stamp()))
    cells = [("X01", 29, "N"), ("X01", 30, "N"), ("X01", 31, "O"), ("N07", 29, "N"), ("N07", 30, "E"),
             ("N07", 31, "E")]
    for i, (nid, day, code) in enumerate(cells):
        db.add(ScheduleEntry(entry_id=f"g2o{i:05d}", schedule_id="g2oct0000001", nurse_id=nid,
                             work_date=datetime(2026, 10, day), shift_id="OFF" if code == "O" else code))


def _older_nights(db) -> None:
    """전월 마감본 24·25일 N — 간격 8 이면 꼬리(6일)보다 먼 N 이 지표에 잡힌다(행 공유가 필요한 이유)."""
    from db.models import ScheduleEntry

    for i, (nid, day) in enumerate((("N04", 24), ("N05", 25))):
        db.add(ScheduleEntry(entry_id=f"xe{i:05d}", schedule_id="g1oct0000001", nurse_id=nid,
                             work_date=datetime(2026, 10, day), shift_id="N"))


def _periods(db) -> None:
    """허용근무 기간표(N01 D 만 · N02 E/N 이달 중순부터) · 주말휴무 기간표(N03)."""
    from db.models import NurseAllowedShiftPeriod, NurseWeekendOffPeriod

    db.add(NurseAllowedShiftPeriod(nurse_id="N01", valid_from=date(2026, 1, 1), valid_to=None,
                                   allowed_shifts=["D"], source="edited"))
    db.add(NurseAllowedShiftPeriod(nurse_id="N02", valid_from=date(2026, 11, 15), valid_to=None,
                                   allowed_shifts=["E", "N"], source="edited"))
    db.add(NurseWeekendOffPeriod(nurse_id="N03", valid_from=date(2026, 1, 1), valid_to=None, weekend_off=1,
                                 source="edited"))


def _seeded(db):
    ward = base_ward(10)
    ward.other_groups = [OTHER_GROUP_ID]
    ward.nurses += [nurse_row("X01", 21, group_id=OTHER_GROUP_ID)]
    ward.extras += [_s7_tails(), _older_nights, _g2_prev, _periods]
    for ddl in RAW_COLUMN_DDL:
        db.execute(text(ddl))
    seed_ward(ward, db)
    return ward


def _cfg(ward, **over) -> dict:
    cfg = dict(ward.config)
    cfg["_inbound_source_map"] = {"X01": OTHER_GROUP_ID}
    cfg["_return_source_map"] = {"N07": (OTHER_GROUP_ID, "2026-11-01")}
    cfg.update(over)
    return cfg


#: 설정 변형 6종 — 사다리가 바꾸는 키(n2n_min_gap·max_conseq_work 등)와 월경계가 읽는 키를 고루.
VARIANTS = [
    {},
    {"n2n_min_gap": 8},
    {"n2n_min_gap": 0, "max_conseq_work": 4},
    {"three_seq_nig": False, "n2n_min_gap": 5},
    {"two_offs_after_two_nig": True, "ban_n_to_e": False},
    {"n2n_min_gap": 3, "banned_day_after_eve": True, "not_one_night": True},
]


def _cross(db, ward, cfg, reads):
    rcs = _rcs()
    ctx = _make_ctx()
    ids = [n["nurse_id"] for n in ward.nurses if n["group_id"] == GROUP_ID]
    return rcs.build_cross_month_constraints(db, ctx, ctx, _sm(ward), cfg, ids,
                                              reads=reads)


def _sm(ward):
    return list(_make_prelude(ward).shift_manage_data)


def _allowed(db, ward, use_mid, reads):
    rcs = _rcs()
    from services.engine_input.builder import _as_nurse_views

    prelude = _make_prelude(ward)
    nurses = _as_nurse_views(prelude.nurses_for_engine)
    return rcs.build_allowed_shift_type_constraints(
        nurses_in_group=nurses, year=YEAR, month=MONTH, shift_manage_data=prelude.shift_manage_data,
        fixed_cells=None, use_mid=use_mid, db=db, reads=reads)


def _count(db, engine, fn):
    log = QueryLog()
    log.attach(engine)
    log.armed = True
    try:
        out = fn()
    finally:
        log.detach()
    return out, list(log.statements)


def test_cross_month_reads_reuse_equals_fresh_call_with_zero_queries(shim_db, engine):  # noqa: F811
    ward = _seeded(shim_db)
    reads: dict = {}
    first = _cross(shim_db, ward, _cfg(ward), reads)
    assert first == _cross(shim_db, ward, _cfg(ward), None)
    assert {"shift_rows", "prev_sid"} <= {k[0] for k in reads}
    assert any(k[0] == "entries" for k in reads) and any(k[0] == "prev_cells" for k in reads)
    assert any(k[0] == "sched_ref" for k in reads) and any(k[0] == "weekend_ids" for k in reads)
    for over in VARIANTS:
        cfg = _cfg(ward, **over)
        fresh = _cross(shim_db, ward, cfg, None)
        reused, stmts = _count(shim_db, engine, lambda c=cfg: _cross(shim_db, ward, c, copy.copy(reads)))
        assert reused == fresh, over
        assert stmts == [], over


def test_cross_month_n2n_gap_changes_result_so_rows_not_results_are_shared(shim_db):  # noqa: F811
    """결과(`last_map`)가 아니라 행을 공유하는 이유 — 간격이 꼬리 자르기 길이를 바꾼다(SPEC A3)."""
    ward = _seeded(shim_db)
    reads: dict = {}
    _cross(shim_db, ward, _cfg(ward, n2n_min_gap=0), reads)
    gap0 = _cross(shim_db, ward, _cfg(ward, n2n_min_gap=0), None)
    gap8 = _cross(shim_db, ward, _cfg(ward, n2n_min_gap=8), None)
    assert gap0 != gap8
    assert _cross(shim_db, ward, _cfg(ward, n2n_min_gap=8), copy.copy(reads)) == gap8


def test_allowed_reads_reuse_equals_fresh_call_with_zero_queries(shim_db, engine):  # noqa: F811
    ward = _seeded(shim_db)
    reads: dict = {}
    first = _allowed(shim_db, ward, False, reads)
    assert first == _allowed(shim_db, ward, False, None)
    assert first["forbidden"]  # N01 D 만 · N02 중순부터 E/N 만
    for use_mid in (False, True):
        fresh = _allowed(shim_db, ward, use_mid, None)
        reused, stmts = _count(shim_db, engine, lambda m=use_mid: _allowed(shim_db, ward, m, copy.copy(reads)))
        assert reused == fresh
        assert stmts == []


def test_memo_miss_queries_and_returns_same_value(shim_db, engine):  # noqa: F811
    ward = _seeded(shim_db)
    fresh = _cross(shim_db, ward, _cfg(ward), None)
    partial: dict = {}
    _cross(shim_db, ward, _cfg(ward), partial)
    del partial[("shift_rows",)]
    got, stmts = _count(shim_db, engine, lambda: _cross(shim_db, ward, _cfg(ward), partial))
    assert got == fresh
    assert len(stmts) == 1 and "shifts" in stmts[0]
    assert ("shift_rows",) in partial


def test_memo_returns_isolated_copies(shim_db):  # noqa: F811
    rcs = _rcs()
    reads: dict = {}
    calls = []
    first = rcs._memo_read(reads, ("k",), lambda: calls.append(1) or {"a": [1, 2]})
    first["a"].append(3)
    again = rcs._memo_read(reads, ("k",), lambda: calls.append(1) or {"a": [9]})
    assert again == {"a": [1, 2]} and len(calls) == 1
    # loader 예외는 담지 않는다
    def _boom():
        raise RuntimeError("x")
    with pytest.raises(RuntimeError):
        rcs._memo_read(reads, ("bad",), _boom)
    assert ("bad",) not in reads
    # reads=None 이면 loader 그대로
    marker = object()
    assert rcs._memo_read(None, ("k",), lambda: marker) is marker


def test_memo_stores_plain_values_not_orm_rows(shim_db):  # noqa: F811
    ward = _seeded(shim_db)
    reads: dict = {}
    _cross(shim_db, ward, _cfg(ward), reads)
    _allowed(shim_db, ward, False, reads)
    for key, value in reads.items():
        blob = repr(value)
        assert "db.models" not in blob and "_sa_instance_state" not in blob, key


def test_ladder_changes_n2n_min_gap_but_not_cross_month_keys():
    """설계 전제 고정(§5.3 EI-13a) — 사다리가 바꾸는 키에 월경계 켬/조회일수는 없고, `n2n_min_gap` 은 있다."""
    import re
    from pathlib import Path

    from services import roster_create_service as rcs
    from services.cp_sat.undiagnosed_probe import RELAX_CATALOG

    src = Path(rcs.__file__).read_text(encoding="utf-8")
    start = src.index("_first_attempt_cfg")  # 사다리는 첫 시도 설정을 만든 뒤부터 설정 키를 바꾼다
    block = src[start:]
    targets = set(re.findall(r"\[\s*['\"]([A-Za-z0-9_]+)['\"]\s*\]\s*=[^=]", block))
    targets |= {str(k) for k in RELAX_CATALOG}
    assert "n2n_min_gap" in targets
    assert "cross_month_hard_rules_enable" not in targets
    assert "cross_month_lookback_days" not in targets
