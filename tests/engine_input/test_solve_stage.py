"""EI-9b-2 단계 B 단위 테스트 — `build_solve_stage`·`build_engine_input`(설계서 EI9_SPEC.md §2.4-2).

호출처가 아직 0 이라(프로덕션 소비 없음) 골든 시나리오 하네스를 쓰지 않는다 — `ward.py` 의
`base_ward()`/`seed_ward()` 로 `shim_db`(test_mssql_shim.py 재사용) 위에 합성 병동을 심고,
운영 함수(`build_allowed_shift_type_constraints`·`build_cross_month_constraints`·
`_build_banned_wanted_constraints`·`build_team_min_by_team`·9-0 차감 함수·post 헬퍼 다수)와
부품별로 교차 확인한다. app 모듈은 가드 규약대로 함수 **안에서** 지연 import 한다(수집 시점
import 는 패키지 가드 밖 — conftest.py 머릿말 참조).

부품별 단위 — 섹션 순서: 어댑터·safe_call → demand(pre/post) → 단계 B 재조회 3종 → team →
post 6종 → solver_params·env_effective → build_solve_stage → build_engine_input(합본·무raise).
"""
from __future__ import annotations

import calendar
from datetime import date
from typing import Any

import pytest
from sqlalchemy import text

from .capture import QueryLog
from .test_mssql_shim import shim_db  # noqa: F401 — 픽스처 재사용
from .ward import (
    CONFIG_ID,
    GROUP_ID,
    OFFICE_ID,
    RAW_COLUMN_DDL,
    base_ward,
    nurse_row,
    seed_ward,
)

YEAR, MONTH = 2026, 11
DAYS = calendar.monthrange(YEAR, MONTH)[1]


def _builder():
    """`services.engine_input.builder` 모듈(지연 import — 가드 규약)."""
    from services.engine_input import builder

    return builder


def _rcs():
    """`services.roster_create_service` 모듈(지연 import — 가드 규약)."""
    from services import roster_create_service

    return roster_create_service


def _make_ctx(**over):
    from services.engine_input.model import Ctx

    return Ctx(office_id=OFFICE_ID, group_id=GROUP_ID, year=YEAR, month=MONTH,
              config_id=CONFIG_ID, **over)


def _with_daily_requirements(config: dict) -> dict:
    """EI-13d — 엔진은 `daily_shift_requirements` 만 읽는다(구 필드 day_req/eve_req/nig_req 폴백 삭제).
    생성 서비스가 늘 채우는 칸을 이 합성 config 에도 같은 값으로 채운다(day_req 등 구 칸은 그대로 둔다)."""
    return {**config, "daily_shift_requirements": {
        "D": config["day_req"], "E": config["eve_req"], "N": config["nig_req"]}}


def _make_prelude(ward, *, fixed_nurses=(), combined_fixed_cells=(), fixed_special_requests=(),
                  preferences=None, nurses_for_engine=None, special_shift_requests=()):
    """`ward` 시드 재료로 `PreludeState` 를 짓는다(차감 **전** config_dict 사본)."""
    from services.engine_input.model import PreludeState

    return PreludeState(
        ctx=_make_ctx(),
        config_dict=_with_daily_requirements(ward.config),
        nurses_for_engine=list(nurses_for_engine if nurses_for_engine is not None else ward.nurses),
        fixed_nurses=list(fixed_nurses),
        fixed_special_requests=list(fixed_special_requests),
        combined_fixed_cells=list(combined_fixed_cells),
        shift_manage_data=list(ward.shift_manage),
        preferences=preferences if preferences is not None else [],
        special_shift_requests=list(special_shift_requests),
    )


def _seed(db, ward) -> None:
    """raw 컬럼 DDL(ward.raw 가 UPDATE 할 자리)을 먼저 연 뒤 ward 를 심는다."""
    for ddl in RAW_COLUMN_DDL:
        db.execute(text(ddl))
    seed_ward(ward, db)


# ══════════════════════════════════════════════════════════════════════════
# 어댑터 · _safe_call
# ══════════════════════════════════════════════════════════════════════════


def test_as_nurse_views_supports_getattr_and_direct_attr():
    builder = _builder()
    views = builder._as_nurse_views([{"nurse_id": "N01", "sequence": 1}])
    assert views[0].nurse_id == "N01"              # 직접 접근(_build_engine_nurse_index_map 식)
    assert getattr(views[0], "allowed_shifts", None) is None  # 없는 키는 getattr 기본값


def test_safe_call_catches_and_records_error():
    builder = _builder()
    errors: dict[str, str] = {}

    def boom():
        raise RuntimeError("boom")

    assert builder._safe_call(errors, "k", boom) is None
    assert "RuntimeError: boom" in errors["k"]


def test_safe_call_passes_through_success():
    builder = _builder()
    errors: dict[str, str] = {}
    assert builder._safe_call(errors, "k", lambda a, b: a + b, 1, 2) == 3
    assert errors == {}


# ══════════════════════════════════════════════════════════════════════════
# demand — pre 스냅샷 · post(9-0 재사용)
# ══════════════════════════════════════════════════════════════════════════


def test_demand_snapshot_reads_literal_config_values():
    builder = _builder()
    config = {"day_req": 2, "eve_req": 2, "nig_req": 2,
             "daily_shift_requirements_by_day": [{"D": 2}], "daily_shift_requirements_max_by_day": None}
    snap = builder._demand_snapshot(config)
    assert snap.base == {"D": 2, "E": 2, "N": 2}
    assert snap.by_day == [{"D": 2}]
    assert snap.max_by_day is None


def test_demand_base_falls_back_to_day_eve_nig_req_when_no_dict():
    builder = _builder()
    assert builder._demand_base({"day_req": 3, "eve_req": 1, "nig_req": 0}) == {"D": 3, "E": 1, "N": 0}


def test_demand_snapshot_returns_isolated_copy():
    """BR-6 — `by_day`/`max_by_day` 는 `config` 의 리스트와 다른 객체다(리뷰 지적 M-B).

    production 모드에서 `config` 가 `first_attempt_config`(EI-13a 에서는 운영이 쓸 그
    config_dict 자체)일 때, 이전엔 바깥 리스트만 `list()` 로 복사해 안의 일자별 `dict` 는
    `config` 와 같은 객체였다 — `DemandSnapshot` 을 받은 쪽이 값을 고치면 운영 config 까지
    같이 바뀌었다.
    """
    builder = _builder()
    config = {"daily_shift_requirements_by_day": [{"D": 2}], "daily_shift_requirements_max_by_day": [{"D": 3}]}
    snap = builder._demand_snapshot(config)
    snap.by_day[0]["D"] = 99
    snap.max_by_day[0]["D"] = 99
    assert config["daily_shift_requirements_by_day"][0]["D"] == 2
    assert config["daily_shift_requirements_max_by_day"][0]["D"] == 3


def test_compute_demand_post_equals_pre_when_no_fixed_nurses(shim_db):  # noqa: F811
    """고정근무자가 없으면 9-0 함수를 부르지 않고 `pre` 그대로(§3.8 조건)."""
    builder = _builder()
    ward = base_ward(3)
    _seed(shim_db, ward)
    prelude = _make_prelude(ward)
    pre = builder._demand_snapshot(prelude.config_dict)
    post, summary, error = builder._compute_demand_post(shim_db, prelude, pre)
    assert post is pre
    assert summary == {"applied": False, "reason": "no_fixed_nurses", "cut_min": 0, "cut_max": 0, "raised": 0}
    assert error is None


def test_compute_demand_post_delegates_to_9_0_functions_when_fixed_nurse_present(shim_db):  # noqa: F811, E501
    """이름 정정(F, 리뷰 지적 LOW) — `_run_fixed_cut` 이 9-0 함수를 같은 입력으로 또 불러 만든
    기대값과 비교하는 부분(`post.by_day == expected.by_day` 등)은 동어반복이다(배선 확인).
    `summary["applied"] is True`·`cut_min > 0` 은 리터럴이라 독립적이다."""
    from db.models import Nurse

    builder = _builder()
    ward = base_ward(3)
    fixed = nurse_row("F01", 99, fixed_shift="D")
    ward.nurses = [*ward.nurses, fixed]
    _seed(shim_db, ward)
    prelude = _make_prelude(ward, fixed_nurses=[fixed])
    pre = builder._demand_snapshot(prelude.config_dict)
    post, summary, error = builder._compute_demand_post(shim_db, prelude, pre)
    assert error is None
    assert summary["applied"] is True and summary["cut_min"] > 0

    fx_nurse = shim_db.query(Nurse).filter(Nurse.nurse_id == "F01").one()
    rcs = _rcs()
    expected_inputs = rcs.read_fixed_cut_inputs(
        shim_db, [fx_nurse], office_id=OFFICE_ID, group_id=GROUP_ID, year=YEAR, month=MONTH,
        weekly_off_active=False,
    )
    expected = rcs.compute_fixed_cut_demand(expected_inputs, [], prelude.config_dict, year=YEAR, month=MONTH)
    assert post.by_day == expected.by_day
    assert post.max_by_day == expected.max_by_day
    assert summary["cut_min"] == expected.cut_min


@pytest.mark.parametrize("activate, enabled", [(True, 1), (False, 1), (True, 0)])
def test_run_fixed_cut_wires_weekly_off_active_from_config_dict(shim_db, monkeypatch, activate, enabled):  # noqa: F811, E501
    """리뷰 지적 LOW — `weekly_off_active` 배선(변이 D2, '늘 False 로 넘김'이 149 passed 로
    살아남던 공백)을 직접 본다. `_cut_fixed_codes`(fixed_cut.py)는 '주'/'O' 둘 다 D/E/N 요구
    키가 아니라 일요일 차이가 `by_day` 최종값에는 안 보인다(실측 확인) — 그래서 끝단 수치가
    아니라 **`read_fixed_cut_inputs` 가 받는 인자 자체**를 스파이로 본다.
    """
    builder = _builder()
    ward = base_ward(1)
    fixed = nurse_row("F01", 99, fixed_shift="D", weekly_off_enabled=enabled)
    ward.nurses = [*ward.nurses, fixed]
    _seed(shim_db, ward)
    prelude = _make_prelude(ward, fixed_nurses=[fixed])
    prelude.config_dict["weekly_off_settings_activate"] = activate
    pre = builder._demand_snapshot(prelude.config_dict)

    seen: dict[str, Any] = {}
    original = builder.read_fixed_cut_inputs

    def _spy(db, fixed_nurses, **kwargs):
        seen["weekly_off_active"] = kwargs.get("weekly_off_active")
        return original(db, fixed_nurses, **kwargs)

    monkeypatch.setattr(builder, "read_fixed_cut_inputs", _spy)
    builder._compute_demand_post(shim_db, prelude, pre)
    assert seen["weekly_off_active"] is activate


def test_compute_demand_post_catches_exception_and_keeps_pre(shim_db, monkeypatch):  # noqa: F811
    """읽기·계산이 예외를 내면 `pre` 그대로 + 메시지(§3.8 '예외면 pre 그대로')."""
    builder = _builder()
    ward = base_ward(1)
    fixed = nurse_row("F01", 99, fixed_shift="D")
    ward.nurses = [*ward.nurses, fixed]
    _seed(shim_db, ward)
    prelude = _make_prelude(ward, fixed_nurses=[fixed])
    pre = builder._demand_snapshot(prelude.config_dict)

    def _boom(*a, **k):
        raise RuntimeError("fx-boom")

    monkeypatch.setattr(builder, "read_fixed_cut_inputs", _boom)
    post, summary, error = builder._compute_demand_post(shim_db, prelude, pre)
    assert post is pre
    assert summary["reason"] == "error" and summary["applied"] is False
    assert "fx-boom" in error


# ══════════════════════════════════════════════════════════════════════════
# 단계 B 재조회 3종(§1.3 ④⑤⑥)
# ══════════════════════════════════════════════════════════════════════════


def test_query_allowed_constraints_forwards_same_args_as_production(shim_db):  # noqa: F811
    """배선 확인(동어반복 아님 주의, F) — `_query_allowed_constraints` 는 `build_allowed_
    shift_type_constraints` 를 호출만 중계하는 얇은 래퍼라, 같은 인자로 직접 부른 결과와
    같은 건 당연하다(독립 재검증이 아니다). 이 테스트는 '래퍼가 어떤 인자를 넘기는지'
    (`year`·`month`·`shift_manage_data`·`fixed_cells`·`use_mid`)가 운영 자리와 같은지를 본다
    — 독립 재검증은 9c `_compare_cells_and_carryover`(골든 DB 로 L1 과 대조)의 몫이다.
    """
    builder = _builder()
    ward = base_ward(2)
    ward.nurses[0]["allowed_shifts"] = ["D"]
    _seed(shim_db, ward)
    prelude = _make_prelude(ward)
    nurses = builder._as_nurse_views(prelude.nurses_for_engine)
    got = builder._query_allowed_constraints(shim_db, prelude, prelude.config_dict, nurses)

    rcs = _rcs()
    expected = rcs.build_allowed_shift_type_constraints(
        nurses_in_group=nurses, year=YEAR, month=MONTH, shift_manage_data=prelude.shift_manage_data,
        fixed_cells=None, use_mid=False, db=shim_db,
    )
    assert got == expected
    assert "N01" in got["forbidden"]  # 허용 D 만인데 다른 코드는 전부 금지


def test_query_cross_month_constraints_runs_with_ctx_as_req_and_current_user(shim_db):  # noqa: F811
    """`req`·`current_user` 자리에 `Ctx` 를 그대로 넣어도 함수가 쓰는 4칸은 충분하다(§1 코드 지도)."""
    builder = _builder()
    ward = base_ward(2)
    _seed(shim_db, ward)
    prelude = _make_prelude(ward)
    nurse_ids = [n["nurse_id"] for n in prelude.nurses_for_engine]
    got = builder._query_cross_month_constraints(shim_db, prelude, prelude.config_dict, nurse_ids)
    assert set(got) >= {"forced_off", "forbidden"}


def test_query_banned_constraints_reflects_banned_wanted_entry(shim_db):  # noqa: F811
    """이름 정정(F) — 예전 이름(`…_matches_production`)은 운영 함수를 또 불러 비교한다는
    인상을 줬지만, 실제로는 금지 원티드 1건을 심고 결과 값을 **직접** 단언할 뿐이다(비교
    없음). 래퍼가 깨지면(`shift_date`·`is_applied` 필터를 빠뜨리면) 이 값 자체가 달라진다."""
    from db.models import BannedWantedEntry

    builder = _builder()
    ward = base_ward(2)
    _seed(shim_db, ward)
    shim_db.add(BannedWantedEntry(group_id=GROUP_ID, nurse_id="N01", year=YEAR, month=MONTH,
                                  shift_date=date(YEAR, MONTH, 5), banned_shift_ids=["N"],
                                  is_applied=True, source="hn"))
    shim_db.flush()
    prelude = _make_prelude(ward)
    nurses = builder._as_nurse_views(prelude.nurses_for_engine)
    got = builder._query_banned_constraints(shim_db, prelude, nurses)
    assert got["forbidden"]["N01"][4] == ["N"]


def test_build_stage_b_queries_one_failure_does_not_block_the_others(shim_db, monkeypatch):  # noqa: F811
    """④ 가 실패해도 ⑤⑥ 은 돌아온다(기록 목적 — 부품별 try)."""
    builder = _builder()
    ward = base_ward(2)
    _seed(shim_db, ward)
    prelude = _make_prelude(ward)

    def _boom(*a, **k):
        raise RuntimeError("allowed-boom")

    monkeypatch.setattr(builder, "_query_allowed_constraints", _boom)
    allowed, cross_month, banned, errors = builder._build_stage_b_queries(
        shim_db, prelude, prelude.config_dict)
    assert allowed == {} and "allowed-boom" in errors["stage_b.allowed"]
    assert cross_month and banned is not None  # 나머지는 정상 반환(빈 dict 포함 허용)


def test_stage_b_hard_cell_failure_marks_engine_input_unusable(shim_db, monkeypatch):  # noqa: F811
    """EI-13a 계약(EI-11 11q 리뷰 LOW) — 월경계 꼬리(⑤) 조회가 실패하면 빌더는 빈 칸으로 이어 가되(기록 목적 · raise 0)
    `errors` 에 남기고, `engine_blocking_errors` 가 그 키를 돌려준다 — 그 합본을 엔진 입력으로 쓰면 운영 G5(하드락 없이 생성)와
    같은 꼴이라 쓰는 쪽이 막아야 한다. 정상이면 빈 목록이다."""
    builder = _builder()
    ward = base_ward(2)
    _seed(shim_db, ward)
    prelude = _make_prelude(ward)
    stage_a = builder.build_precheck_stage(shim_db, prelude)
    assert builder.engine_blocking_errors(builder.build_solve_stage(shim_db, prelude, stage_a).errors) == []

    def _boom(*a, **k):
        raise RuntimeError("cross-month-boom")

    monkeypatch.setattr(builder, "_query_cross_month_constraints", _boom)
    stage = builder.build_solve_stage(shim_db, prelude, stage_a)
    assert "cross-month-boom" in stage.errors["stage_b.cross_month"]
    assert builder.engine_blocking_errors(stage.errors) == ["stage_b.cross_month"]
    assert builder.engine_blocking_errors({"post.oncall": "x", "raw.n2n_min_gap": "y"}) == []


# ══════════════════════════════════════════════════════════════════════════
# cells.initial_constraints · carryover(리뷰 지적 — 단계 B 가 짓고 버리던 결과를 싣는다)
# ══════════════════════════════════════════════════════════════════════════


#: 손으로 적은 전월 꼬리 키 목록(리뷰 지적 HIGH — `builder._CARRYOVER_KEYS` 를 다시 가져오면
#: 피검 코드가 키를 빠뜨려도 기대값이 같이 빠져 자기 참조가 된다). rcs 가 조건부로 `config_dict`
#: 에 심는 여섯 키(⑤ `build_cross_month_constraints` 의 조건부 대입, :3610-:3625) 그대로다.
_EXPECTED_CARRYOVER_KEYS = ("prev_month_last_main", "prev_month_n_tail", "prev_month_n_offs_after",
                           "prev_month_last_n_k", "prev_month_off_tail")


def test_build_cells_and_carryover_merges_allowed_cross_month_banned_like_rcs(shim_db):  # noqa: F811
    """`cells.initial_constraints` == rcs `_merge_initial_constraints(merge(cross_month, allowed), banned)`."""
    from db.models import BannedWantedEntry

    rcs = _rcs()
    builder = _builder()
    ward = base_ward(2)
    _seed(shim_db, ward)
    shim_db.add(BannedWantedEntry(group_id=GROUP_ID, nurse_id="N01", year=YEAR, month=MONTH,
                                  shift_date=date(YEAR, MONTH, 5), banned_shift_ids=["N"],
                                  is_applied=True, source="hn"))
    shim_db.flush()
    prelude = _make_prelude(ward, combined_fixed_cells=[{"nurse_id": "N01"}])
    allowed, cross_month, banned, _errors = builder._build_stage_b_queries(
        shim_db, prelude, prelude.config_dict)
    cells, carryover = builder._build_cells_and_carryover(
        prelude, prelude.config_dict, allowed, cross_month, banned)
    expected = rcs._merge_initial_constraints(
        base=rcs._merge_initial_constraints(base=cross_month, extra=allowed), extra=banned)
    assert cells["initial_constraints"] == expected
    assert cells["initial_constraints"]["forbidden"]["N01"][4] == ["N"]  # banned 가 실제로 반영됨
    assert carryover == {k: v for k, v in cross_month.items()
                         if k in _EXPECTED_CARRYOVER_KEYS and v}


def test_build_cells_and_carryover_combined_fixed_cells_is_isolated_copy(shim_db):  # noqa: F811
    """`cells.combined_fixed_cells` 는 `prelude` 의 리스트와 **다른 객체**다(BR-6, 리뷰 지적)."""
    builder = _builder()
    ward = base_ward(1)
    _seed(shim_db, ward)
    prelude = _make_prelude(ward, combined_fixed_cells=[{"nurse_id": "N01", "day": 1}])
    cells, _carryover = builder._build_cells_and_carryover(prelude, {}, {}, {}, {})
    assert cells["combined_fixed_cells"] == prelude.combined_fixed_cells
    assert cells["combined_fixed_cells"] is not prelude.combined_fixed_cells
    cells["combined_fixed_cells"][0]["day"] = 99
    assert prelude.combined_fixed_cells[0]["day"] == 1  # 원본은 그대로


def test_effective_off_window_constraints_unions_cross_month_and_config(shim_db):  # noqa: F811
    """O-18 · EI-11 11r — ⑤(cross_month) 창과 `config` 창(월중 경계 몫)을 간호사별로 합친다(⑤ 가 있다고 `config` 몫을
    버리지 않는다 — 예전 rcs 의 통째 교체는 월중 경계 하드락 창을 엔진에서 지웠다). 순서는 ⑤ → `config`, 같은 창은 한 번."""
    builder = _builder()
    ward = base_ward(1)
    _seed(shim_db, ward)
    prelude = _make_prelude(ward)
    config = {**prelude.config_dict, "off_window_constraints": {"X01": [[19, 22]], "N01": [[5, 7], [14, 17]]}}
    cross_month = {"off_window_constraints": {"N01": [[0, 3], [5, 7]]}}
    got = builder._effective_off_window_constraints(config, cross_month)
    assert got == {"N01": [[0, 3], [5, 7], [14, 17]], "X01": [[19, 22]]}
    assert list(got) == ["N01", "X01"]


def test_build_cells_and_carryover_merges_config_mid_month_cells(shim_db):  # noqa: F811
    """EI-11 11r — `config` 의 금지칸·강제 OFF(월중 경계, precheck 앞에서 심긴 값)도 ④⑤⑥ 병합 뒤에 합친다(rcs 와 같다)."""
    rcs = _rcs()
    builder = _builder()
    ward = base_ward(2)
    _seed(shim_db, ward)
    prelude = _make_prelude(ward)
    mid = {"forced_off": {"N02": [19]}, "forbidden": {"N02": {19: ["D", "E"]}, "N01": {3: ["N"]}}}
    config = {**prelude.config_dict, "initial_constraints": mid}
    allowed, cross_month, banned, _errors = builder._build_stage_b_queries(shim_db, prelude, config)
    cells, _carryover = builder._build_cells_and_carryover(prelude, config, allowed, cross_month, banned)
    expected = rcs._merge_initial_constraints(
        base=rcs._merge_initial_constraints(
            base=rcs._merge_initial_constraints(base=cross_month, extra=allowed), extra=banned),
        extra=mid)
    assert cells["initial_constraints"] == expected
    assert cells["initial_constraints"]["forced_off"]["N02"] == [19]
    assert cells["initial_constraints"]["forbidden"]["N02"][19] == ["D", "E"]


def test_effective_off_window_constraints_falls_back_to_config(shim_db):  # noqa: F811
    """O-18 — ⑤ 가 비면(S6 처럼) `config`(mid-month 가 precheck 앞에서 심어 둔 값, 리뷰 지적
    L16 — `prelude.config_dict` 고정이 아니라 단계 B 의 다른 계산과 같은 `config` 를 쓴다)로
    떨어진다. 이 폴백이 `_build_cells_and_carryover` 의 `carryover` 에도 실린다."""
    builder = _builder()
    ward = base_ward(1)
    _seed(shim_db, ward)
    prelude = _make_prelude(ward)
    config = {**prelude.config_dict, "off_window_constraints": {"X01": [[19, 19]], "N07": [[14, 17]]}}
    assert builder._effective_off_window_constraints(config, {}) == config["off_window_constraints"]
    _cells, carryover = builder._build_cells_and_carryover(prelude, config, {}, {}, {})
    assert carryover["off_window_constraints"] == {"X01": [[19, 19]], "N07": [[14, 17]]}


def test_effective_off_window_constraints_returns_isolated_copy(shim_db):  # noqa: F811
    """BR-6 — 반환은 `config`/`cross_month` 의 리스트와 다른 객체다(리뷰 지적 M-B)."""
    builder = _builder()
    ward = base_ward(1)
    _seed(shim_db, ward)
    prelude = _make_prelude(ward)
    config = {**prelude.config_dict, "off_window_constraints": {"N01": [[1, 1]]}}
    got = builder._effective_off_window_constraints(config, {})
    got["N01"].append([9, 9])
    assert config["off_window_constraints"]["N01"] == [[1, 1]]  # 원본은 그대로


def test_effective_off_window_constraints_empty_both_sides_is_absent_from_carryover(shim_db):  # noqa: F811, E501
    """O-18 — 양쪽 다 비어 있으면 `carryover` 에 `off_window_constraints` 키 자체가 없다
    (기존 `_CARRYOVER_KEYS` 다섯 키와 같은 '진위 판정' 규약)."""
    builder = _builder()
    ward = base_ward(1)
    _seed(shim_db, ward)
    prelude = _make_prelude(ward)
    assert builder._effective_off_window_constraints(prelude.config_dict, {}) == {}
    _cells, carryover = builder._build_cells_and_carryover(prelude, prelude.config_dict, {}, {}, {})
    assert "off_window_constraints" not in carryover


# ══════════════════════════════════════════════════════════════════════════
# team_min_by_team(§2.4-1 '단계 B 의 몫')
# ══════════════════════════════════════════════════════════════════════════


def test_build_team_dict_computes_from_team_rows_when_absent(shim_db):  # noqa: F811
    """멤버가 배정된 팀만 담는다 — 멤버 0 인 활성 팀(team_id=2)은 `min_shift` 가 있어도 빠진다
    (리뷰 지적 — 멤버십 필터를 무시해도 통과하던 테스트 공백을 메운다, MR6).
    """
    from db.models import Team

    builder = _builder()
    ward = base_ward(2)
    ward.nurses[0]["team_id"] = 1
    _seed(shim_db, ward)
    shim_db.add(Team(office_id=OFFICE_ID, group_id=GROUP_ID, team_id=1, team_name="T1",
                     active=1, min_shift={"D": 2, "E": 1}))
    shim_db.add(Team(office_id=OFFICE_ID, group_id=GROUP_ID, team_id=2, team_name="T2",
                     active=1, min_shift={"D": 3}))
    shim_db.flush()
    prelude = _make_prelude(ward)
    stage_a = builder.build_precheck_stage(shim_db, prelude)
    got = builder._build_team_dict(prelude, stage_a, prelude.config_dict)
    assert got["team_min_by_team"] == {"1": {"D": 2, "E": 1}}
    assert "2" not in got["team_min_by_team"]


def test_build_team_dict_respects_existing_override():
    """이미 `team_min_by_team` 이 있으면(런타임 override) 덮지 않는다."""
    builder = _builder()

    class _FakeLazy:
        def get(self):
            return []

    stage_a = type("S", (), {"team_rows": _FakeLazy()})()
    sentinel = {"9": {"D": 9}}
    out = builder._build_team_dict(
        type("P", (), {"nurses_for_engine": []})(), stage_a, {"team_min_by_team": sentinel})
    assert out["team_min_by_team"] is sentinel


# ══════════════════════════════════════════════════════════════════════════
# codebook · roster · settings_eff(O-15 — 이전엔 빈 채로 두었던 세 부품)
# ══════════════════════════════════════════════════════════════════════════


def test_build_codebook_forwards_shift_lookup_to_production_maps(shim_db):  # noqa: F811
    """배선 확인(동어반복 아님 주의, F) — `code2main`·`id_to_main` 은 운영 함수(`_build_code_
    to_main_map`·`_build_shift_normalizer`)를 그대로 불러 비교하므로 독립 재검증이지만,
    `shift_definitions`(`expected_defs`)는 `_shift_definition_row` 와 **같은 필드 매핑을
    테스트가 손으로 다시 적은 것**이라 그 매핑 자체의 버그는 못 잡는다(그 부분만 동어반복 —
    대신 `rcs._load_shift_lookup` 을 새로 불러 같은 조회 결과를 돌려주는지는 검증한다).
    9c `_compare_codebook` 이 골든 DB 로 독립 재검증한다.
    """
    rcs = _rcs()
    builder = _builder()
    ward = base_ward(2)
    _seed(shim_db, ward)
    prelude = _make_prelude(ward)
    stage_a = builder.build_precheck_stage(shim_db, prelude)

    codebook = builder._build_codebook(prelude, stage_a)

    expected_defs = []
    for shift in rcs._load_shift_lookup(shim_db, OFFICE_ID, GROUP_ID).values():
        expected_defs.append({
            "shift_id": shift.shift_id, "default_shift": shift.default_shift or shift.shift_id,
            "shift_gb": shift.shift_gb, "type": shift.type,
            "show_in_preference": shift.show_in_preference,
        })
    assert codebook["shift_definitions"] == expected_defs
    assert codebook["code2main"] == rcs._build_code_to_main_map(ward.shift_manage)
    from services.cp_sat_basic import _build_shift_normalizer

    expected_id_to_main, _ = _build_shift_normalizer(expected_defs)
    assert codebook["id_to_main"] == expected_id_to_main
    # `build_shift_normalizer` 는 "OFF" 를 "O" 로 접어 키를 "O" 로 둔다(cp_sat/shift_normalizer.py).
    assert codebook["id_to_main"]["D"] == "D" and codebook["id_to_main"]["O"] == "O"


def test_build_codebook_keeps_falsy_show_in_preference(shim_db):  # noqa: F811
    """`show_in_preference=False` 가 `or` 폴백에 먹혀 `None` 이 되지 않는다(변이 가드)."""
    from db.models import Shift

    builder = _builder()
    ward = base_ward(1)
    ward.shifts = [s for s in ward.shifts if s["shift_id"] != "OFF"]
    _seed(shim_db, ward)
    shim_db.add(Shift(**{**ward.shifts[0], "shift_id": "D2", "id": 99, "show_in_preference": False}))
    shim_db.flush()
    prelude = _make_prelude(ward)
    stage_a = builder.build_precheck_stage(shim_db, prelude)
    codebook = builder._build_codebook(prelude, stage_a)
    row = next(r for r in codebook["shift_definitions"] if r["shift_id"] == "D2")
    assert row["show_in_preference"] is False


def test_build_codebook_reuses_cached_shift_lookup_no_extra_query(shim_db, engine):  # noqa: F811
    """`stage_a.shift_lookup` 이 이미 캐시돼 있으면 `_build_codebook` 는 새 조회를 내지 않는다."""
    builder = _builder()
    ward = base_ward(1)
    _seed(shim_db, ward)
    prelude = _make_prelude(ward)
    stage_a = builder.build_precheck_stage(shim_db, prelude)
    stage_a.shift_lookup.get()  # 미리 읽어 캐시해 둔다
    log = QueryLog()
    log.attach(engine)
    log.armed = True
    try:
        builder._build_codebook(prelude, stage_a)
        builder._build_codebook(prelude, stage_a)  # 두 번 불러도 추가 조회 없음
    finally:
        log.detach()
    assert log.statements == []


def test_build_roster_weekend_getter_failure_is_swallowed_list_stays_intact(shim_db, monkeypatch):  # noqa: F811, E501
    """리뷰 지적 LOW — 주말휴무 getter 가 실패해도 rcs :3490-3491 처럼 **삼키고** 명단·index
    맵은 그대로 돌아온다(이전엔 바깥 `_safe_call` 이 함수 전체를 감싸 명단까지 함께 비었다)."""
    builder = _builder()
    ward = base_ward(2)
    _seed(shim_db, ward)
    prelude = _make_prelude(ward)
    stage_a = builder.build_precheck_stage(shim_db, prelude)

    def _boom():
        raise RuntimeError("weekend-boom")

    monkeypatch.setattr(stage_a.weekend_ids, "get", _boom)
    roster = builder._build_roster(prelude, stage_a, prelude.config_dict)
    assert [n["nurse_id"] for n in roster["nurses"]] == ["N01", "N02"]
    assert "is_weekend_off" not in roster["nurses"][0]


def test_build_roster_nid_falls_back_to_db_id_when_nurse_id_missing(shim_db, monkeypatch):  # noqa: F811, E501
    """리뷰 지적 LOW — `nid` 선택이 rcs :3481·:3484 와 같이 `nurse_id or db_id` 다."""
    builder = _builder()
    ward = base_ward(1)
    _seed(shim_db, ward)
    nurses_for_engine = [{**ward.nurses[0], "nurse_id": None, "db_id": "N01"}]
    prelude = _make_prelude(ward, nurses_for_engine=nurses_for_engine)
    stage_a = builder.build_precheck_stage(shim_db, prelude)
    monkeypatch.setattr(stage_a.weekend_ids, "get", lambda: {"N01"})
    roster = builder._build_roster(prelude, stage_a, prelude.config_dict)
    assert roster["nurses"][0]["is_weekend_off"] is True


def test_build_roster_injects_is_weekend_off_via_cached_getter(shim_db):  # noqa: F811
    """`is_weekend_off` 는 `stage_a.weekend_ids`(캐시된 Lazy)로 주입한다 — 재조회하지 않는다."""
    from db.models import NurseWeekendOffPeriod

    builder = _builder()
    ward = base_ward(2)
    _seed(shim_db, ward)
    shim_db.add(NurseWeekendOffPeriod(nurse_id="N01", valid_from=date(2024, 1, 1),
                                     valid_to=None, weekend_off=1))
    shim_db.flush()
    prelude = _make_prelude(ward)
    stage_a = builder.build_precheck_stage(shim_db, prelude)
    roster = builder._build_roster(prelude, stage_a, prelude.config_dict)
    by_id = {n["nurse_id"]: n for n in roster["nurses"]}
    assert by_id["N01"]["is_weekend_off"] is True
    assert by_id["N02"]["is_weekend_off"] is False
    assert stage_a.weekend_ids.loaded is True


def test_build_roster_preserves_engine_order_and_delegates_index_map(shim_db):  # noqa: F811, E501
    """이름 정정(F, 리뷰 지적 LOW) — 명단 순서 단언은 독립적이지만, index 맵 단언은 같은
    `_build_engine_nurse_index_map` 을 또 불러 만든 기대값과 비교하는 배선 확인이다(동어반복
    — 데이터도 sequence=experience 역순=nurse_id 순 퇴화라 정렬 키 자체의 결함은 못 잡는다,
    비퇴화 검증은 아래 `..._on_non_degenerate_order` 가 한다)."""
    rcs = _rcs()
    builder = _builder()
    ward = base_ward(3)
    _seed(shim_db, ward)
    prelude = _make_prelude(ward)
    stage_a = builder.build_precheck_stage(shim_db, prelude)
    roster = builder._build_roster(prelude, stage_a, prelude.config_dict)
    assert [n["nurse_id"] for n in roster["nurses"]] == ["N01", "N02", "N03"]
    expected_index = rcs._build_engine_nurse_index_map(builder._as_nurse_views(prelude.nurses_for_engine))
    assert roster["nurse_id_to_engine_index"] == {str(k): v for k, v in expected_index.items()}


def test_build_roster_index_map_matches_production_sort_key_on_non_degenerate_order(shim_db):  # noqa: F811, E501
    """정렬 키(`sequence`→`-experience`→`nurse_id`)가 실제로 갈리는 입력에서도 손으로 적은
    기대값과 같다(리뷰 지적 HIGH — `ward.nurse_row` 의 기본값은 `sequence` 오름차순 =
    `experience` 내림차순 = `nurse_id` 오름차순이 전부 같은 방향으로 퇴화돼 있어, 정렬 키가
    틀려도(예: `nurse_id` 단독 정렬) 통과하는 공백이 있었다). 세 축이 서로 다른 방향을 가리키게
    구성한다 — N01(seq1,exp5) · N02(seq1,exp9) · N03(seq0,exp1).
    """
    builder = _builder()
    ward = base_ward(0)
    ward.nurses = [
        nurse_row("N01", 1, experience=5), nurse_row("N02", 1, experience=9),
        nurse_row("N03", 0, experience=1),
    ]
    _seed(shim_db, ward)
    prelude = _make_prelude(ward)
    stage_a = builder.build_precheck_stage(shim_db, prelude)
    roster = builder._build_roster(prelude, stage_a, prelude.config_dict)
    # (sequence, -experience, nurse_id) 오름차순 → N03(0,-1) → N02(1,-9) → N01(1,-5).
    assert roster["nurse_id_to_engine_index"] == {"N03": 0, "N02": 1, "N01": 2}


def test_build_roster_off_first_zeroes_personal_limits_without_mutating_prelude(shim_db):  # noqa: F811
    """`off_first=True` 면 `o_min`/`o_max`/`o_exact` 를 지운다(:3491-3492) — 원본은 그대로.

    `o_min`/`o_max` 는 월 한도 오버레이가 `__dict__` 로 직접 얹는 칸이라(:5666-5702) ORM
    컬럼이 아니다 — `nurses_for_engine` 에만 넣고(DB 시드 대상은 그대로 둔다, ORM 생성자가
    모르는 키를 거부한다).
    """
    builder = _builder()
    ward = base_ward(1)
    _seed(shim_db, ward)
    engine_nurses = [{**ward.nurses[0], "o_min": 2, "o_max": 10, "o_exact": None}]
    prelude = _make_prelude(ward, nurses_for_engine=engine_nurses)
    stage_a = builder.build_precheck_stage(shim_db, prelude)
    roster = builder._build_roster(prelude, stage_a, {**prelude.config_dict, "off_first": True})
    assert roster["nurses"][0]["o_min"] is None and roster["nurses"][0]["o_max"] is None
    assert prelude.nurses_for_engine[0]["o_min"] == 2  # 원본 prelude 는 불변


def test_build_settings_eff_input_merges_demand_post_team_and_shift_definitions():
    """차감 뒤 demand · team · codebook.shift_definitions 를 L1 `config_data` 와 같은 순서로 얹는다."""
    from services.engine_input.model import DemandPart, DemandSnapshot

    builder = _builder()
    demand = DemandPart(pre=DemandSnapshot(base={"D": 2}),
                        post=DemandSnapshot(base={"D": 2}, by_day=[{"D": 1}], max_by_day=[{"D": 3}]))
    team = {"team_min_by_team": {"1": {"D": 1}}}
    codebook = {"shift_definitions": [{"shift_id": "D"}]}
    out = builder._build_settings_eff_input({"day_req": 2, "off_first": False}, demand, team, codebook)
    assert out["daily_shift_requirements_by_day"] == [{"D": 1}]
    assert out["daily_shift_requirements_max_by_day"] == [{"D": 3}]
    assert out["team_min_by_team"] == {"1": {"D": 1}}
    assert out["shift_definitions"] == [{"shift_id": "D"}]
    assert out["day_req"] == 2  # 바탕 config 칸은 그대로 남는다


def test_build_settings_eff_input_skips_empty_shift_definitions():
    from services.engine_input.model import DemandPart, DemandSnapshot

    builder = _builder()
    demand = DemandPart(pre=DemandSnapshot(), post=None)
    out = builder._build_settings_eff_input({"day_req": 1}, demand, {}, {"shift_definitions": []})
    assert "shift_definitions" not in out
    assert "daily_shift_requirements_by_day" not in out


def test_build_settings_eff_calls_create_config_from_db_and_returns_canonical_dict():
    """`create_config_from_db` 제자리 호출 — 결과는 `canonical()` 로 dict 화(ORM/엔진 객체 금지)."""
    builder = _builder()
    config_data = {"day_req": 2, "eve_req": 2, "nig_req": 2, "max_nig_per_month": 15,
                   "daily_shift_requirements": {"D": 2, "E": 2, "N": 2}}
    settings_eff = builder._build_settings_eff(config_data)
    assert settings_eff["__type__"] == "NurseRosterConfig"
    assert settings_eff["daily_shift_requirements"] == {"D": 2, "E": 2, "N": 2}
    assert settings_eff["max_night_shifts_per_month"] == 15


def test_build_settings_eff_propagates_typeerror_for_null_shift_priority():
    """S3 류 — `shift_priority` 가 명시적 `None` 이면 `max(0.05, None)` 이 TypeError 를 낸다."""
    import pytest

    builder = _builder()
    with pytest.raises(TypeError):
        builder._build_settings_eff({"day_req": 2, "eve_req": 2, "nig_req": 2, "shift_priority": None,
                                     "daily_shift_requirements": {"D": 2, "E": 2, "N": 2}})


# ══════════════════════════════════════════════════════════════════════════
# post 6종(§1.5, D20 — 기록만)
# ══════════════════════════════════════════════════════════════════════════


def test_post_health_leave_returns_empty_set_when_disabled(shim_db):  # noqa: F811
    """`health_leave_enabled` 가 기본 미설정(falsy)이면 조회만 하고 빈 집합(§1.5 ①)."""
    builder = _builder()
    ward = base_ward(2)
    _seed(shim_db, ward)
    prelude = _make_prelude(ward)
    assert builder._post_health_leave(shim_db, prelude) == {"target_ids": []}


def test_post_health_leave_passes_full_special_shift_requests_not_fixed_only(shim_db, monkeypatch):  # noqa: F811, E501
    """O-11 — `existing_requests` 는 `prelude.special_shift_requests`(운영 전체)를 받는다.

    이전엔 `prelude.fixed_special_requests`(고정근무자만)로 근사했다 — 이미 그 달 보건휴가
    코드를 원티드로 선점한 **일반** 간호사가 `select_health_leave_targets` 의 `already` 판정
    에서 빠지는 근사 오차가 있었다. `select_health_leave_targets` 를 가로채 실제로 넘어온
    `existing_requests` 가 어느 칸인지 직접 본다.
    """
    builder = _builder()
    ward = base_ward(2)
    _seed(shim_db, ward)
    full = [{"nurse_id": "N01", "day": 3, "shift_id": "연", "shift_type": "휴가"}]
    fixed_only = [{"nurse_id": "F01", "day": 5, "shift_id": "연", "shift_type": "휴가"}]
    prelude = _make_prelude(ward, fixed_special_requests=fixed_only, special_shift_requests=full)
    seen: dict[str, Any] = {}

    def _spy(db, **kwargs):
        seen["existing_requests"] = kwargs.get("existing_requests")
        return set()

    import services.leave.health_leave_planner as hlp

    monkeypatch.setattr(hlp, "select_health_leave_targets", _spy)
    builder._post_health_leave(shim_db, prelude)
    assert seen["existing_requests"] == full
    assert seen["existing_requests"] != fixed_only


def test_post_sleep_off_shape(shim_db):  # noqa: F811
    builder = _builder()
    ward = base_ward(2)
    _seed(shim_db, ward)
    prelude = _make_prelude(ward)
    got = builder._post_sleep_off(shim_db, prelude)
    assert got == {"requirements_days": 0, "prev_month_tail": {}}  # DailyShift·전월 근무표 미시드


def test_post_off_swap_shape_without_target(shim_db):  # noqa: F811
    builder = _builder()
    ward = base_ward(2)
    _seed(shim_db, ward)
    prelude = _make_prelude(ward)
    got = builder._post_off_swap(shim_db, prelude)
    assert got["target_shift_id"] is None               # off_swap_target 없음
    assert got["n_codes"] == ["N"] and got["o_only_codes"] == ["OFF"]
    assert got["nurse_count"] == 3                       # RN 2 + HN01
    assert set(got["prev_month_tail"]) == {"N01", "N02"}
    assert all(v == ["", "", ""] for v in got["prev_month_tail"].values())  # 전월 근무표 없음


def test_post_leave_mask_reads_leave_spans(shim_db):  # noqa: F811
    from db.models import NurseAssignment

    builder = _builder()
    ward = base_ward(2)
    _seed(shim_db, ward)
    shim_db.add(NurseAssignment(nurse_id="N01", source_group_id=GROUP_ID, office_id=OFFICE_ID,
                                start_date=date(YEAR, MONTH, 3), end_date=date(YEAR, MONTH, 6),
                                reason="휴직", status="active"))
    shim_db.flush()
    prelude = _make_prelude(ward)
    got = builder._post_leave_mask(shim_db, prelude)
    assert got["spans"]["N01"] == [(f"{YEAR}-{MONTH:02d}-03", f"{YEAR}-{MONTH:02d}-06")]
    assert "N02" not in got["spans"]


def test_post_leave_mask_ignores_cancelled(shim_db):  # noqa: F811
    from db.models import NurseAssignment

    builder = _builder()
    ward = base_ward(1)
    _seed(shim_db, ward)
    shim_db.add(NurseAssignment(nurse_id="N01", source_group_id=GROUP_ID, office_id=OFFICE_ID,
                                start_date=date(YEAR, MONTH, 3), end_date=date(YEAR, MONTH, 6),
                                reason="휴직", status="cancelled"))
    shim_db.flush()
    prelude = _make_prelude(ward)
    assert builder._post_leave_mask(shim_db, prelude)["spans"] == {}


def test_post_oncall_gate_empty_when_no_call_codes(shim_db):  # noqa: F811
    builder = _builder()
    ward = base_ward(2)
    _seed(shim_db, ward)
    prelude = _make_prelude(ward)
    assert builder._post_oncall(shim_db, prelude) == {"call_codes": {}}


def test_post_oncall_non_empty_when_call_codes_registered(shim_db):  # noqa: F811
    from db.models import Shift, Team
    from sqlalchemy import text

    builder = _builder()
    ward = base_ward(1)
    ward.nurses[0]["team_id"] = 1
    _seed(shim_db, ward)
    shim_db.add(Team(office_id=OFFICE_ID, group_id=GROUP_ID, team_id=1, team_name="T1", active=1))
    shim_db.add(Shift(shift_id="D콜", id=901, office_id=OFFICE_ID, group_id=GROUP_ID,
                      name="D콜", color="#fff"))
    shim_db.flush()
    shift_d_id = shim_db.execute(text(
        "SELECT id FROM shifts WHERE shift_id = 'D' AND group_id = :g"), {"g": GROUP_ID}).scalar()
    shim_db.execute(text("ALTER TABLE shifts ADD COLUMN call_base_id INTEGER"))
    shim_db.execute(text("UPDATE shifts SET call_base_id = :b WHERE id = 901"),
                    {"b": shift_d_id})
    prelude = _make_prelude(ward)
    got = builder._post_oncall(shim_db, prelude)
    assert got["call_codes"] == {"D": "D콜"}
    assert got["team_of"] == {"N01": 1}
    assert [m["nurse_id"] for m in got["members"]] == ["N01"]
    assert got["protected_days"] == {} and got["banned_eves"] == {}


def test_post_work_shift_overrides_is_memory_only_marker():
    """`roster` 부품은 이제 있다(O-15) — marker 는 'post 시점 값은 따로 안 남긴다'는 뜻으로 남는다."""
    builder = _builder()
    assert builder._post_work_shift_overrides() == {
        "source": "roster.work_shifts",
        "note": "엔진 입력 roster.nurses[*].work_shifts 를 읽는다(post 시점 값은 따로 기록하지 않는다)",
    }


def test_record_post_has_all_six_keys_and_partial_markers(shim_db):  # noqa: F811
    builder = _builder()
    ward = base_ward(2)
    _seed(shim_db, ward)
    prelude = _make_prelude(ward)
    post, errors = builder._record_post(shim_db, prelude)
    assert errors == {}
    assert set(post) == {"health_leave", "sleep_off", "off_swap", "leave_mask", "oncall",
                         "work_shift_overrides", "_partial"}
    assert post["_partial"] == list(builder._POST_PARTIAL_ALWAYS)


def test_record_post_one_part_failing_does_not_drop_the_rest(shim_db, monkeypatch):  # noqa: F811
    builder = _builder()
    ward = base_ward(2)
    _seed(shim_db, ward)
    prelude = _make_prelude(ward)

    def _boom(*a, **k):
        raise RuntimeError("sleepoff-boom")

    monkeypatch.setattr(builder, "_post_sleep_off", _boom)
    post, errors = builder._record_post(shim_db, prelude)
    assert post["sleep_off"] == {} and "sleepoff-boom" in errors["post.sleep_off"]
    assert post["off_swap"]["nurse_count"] == 3  # 나머지 부품은 그대로 채워진다


# ══════════════════════════════════════════════════════════════════════════
# solver_params · env_effective
# ══════════════════════════════════════════════════════════════════════════


def test_solver_params_time_limit_matches_rcs_formula():
    """키 이름은 설계서 §2.4 표의 `time_limit` 을 따른다(리뷰 지적 — `time_limit_seconds` 아님).

    `distribution_mode` 는 더 이상 여기 들어가지 않는다 — `settings_raw` 에 이미 있는 값이
    `solver_params` 에도 복제되면 digest 가 같은 변화를 두 번 반영한다(리뷰 지적).
    """
    builder = _builder()
    assert builder._solver_params(advanced_inference=False) == {"time_limit": 60}
    assert builder._solver_params(advanced_inference=True) == {"time_limit": 180}


def test_env_effective_reads_os_environ_at_call_time(monkeypatch):
    """BR-4 — import 시점이 아니라 호출 시점에 `os.environ` 을 읽는다."""
    from services.engine_contract.env import ENGINE_PATH_NAMES

    builder = _builder()
    name = next(iter(ENGINE_PATH_NAMES))
    monkeypatch.delenv(name, raising=False)
    assert name not in builder._env_effective()
    monkeypatch.setenv(name, "1")
    assert builder._env_effective()[name] == "1"


# ══════════════════════════════════════════════════════════════════════════
# build_solve_stage
# ══════════════════════════════════════════════════════════════════════════


def test_build_solve_stage_assembles_all_parts_without_raising(shim_db):  # noqa: F811
    builder = _builder()
    ward = base_ward(3)
    _seed(shim_db, ward)
    prelude = _make_prelude(ward)
    stage_a = builder.build_precheck_stage(shim_db, prelude)
    stage = builder.build_solve_stage(shim_db, prelude, stage_a)
    assert stage.errors == {}
    assert stage.demand.pre.base == {"D": 2, "E": 2, "N": 2}
    assert stage.demand.post is stage.demand.pre  # 고정근무자 없음
    assert stage.solver_params["time_limit"] == 60
    assert set(stage.post) == {"health_leave", "sleep_off", "off_swap", "leave_mask", "oncall",
                               "work_shift_overrides", "_partial"}
    # O-15 — codebook·roster·settings_eff 도 채워진다.
    assert stage.codebook["code2main"]["D"] == "D"
    assert [n["nurse_id"] for n in stage.roster["nurses"]] == ["N01", "N02", "N03"]
    assert stage.settings_eff["__type__"] == "NurseRosterConfig"


def test_build_solve_stage_uses_first_attempt_config_when_given(shim_db):  # noqa: F811
    """`first_attempt_config` 를 넘기면 그 config 로 ④⑤⑥·team·solver_params 를 계산한다."""
    builder = _builder()
    ward = base_ward(2)
    _seed(shim_db, ward)
    prelude = _make_prelude(ward)
    stage_a = builder.build_precheck_stage(shim_db, prelude)
    override = {**prelude.config_dict, "use_mid": True, "distribution_mode": "max_only"}
    stage = builder.build_solve_stage(shim_db, prelude, stage_a, first_attempt_config=override,
                                      advanced_inference=True)
    assert stage.solver_params == {"time_limit": 180}


# ══════════════════════════════════════════════════════════════════════════
# D20 모드 스위치(C) — mode="production" 은 post·raw·고정근무 재조회 SQL 0
# ══════════════════════════════════════════════════════════════════════════


def test_build_solve_stage_production_mode_without_config_records_error_not_pre_cut_value(shim_db):  # noqa: F811, E501
    """리뷰 지적 HIGH — `mode="production"` 인데 `first_attempt_config` 를 안 넘기면 **차감
    전** `prelude.config_dict` 로 조용히 떨어지지 않는다(D20 위반을 숨기는 자리였다).
    `errors["demand.post"]` 에 남고 `demand.post` 는 `None`(미확정)이다."""
    builder = _builder()
    ward = base_ward(1)
    fixed = nurse_row("F01", 99, fixed_shift="D")
    ward.nurses = [*ward.nurses, fixed]
    _seed(shim_db, ward)
    prelude = _make_prelude(ward, fixed_nurses=[fixed])
    stage_a = builder.build_precheck_stage(shim_db, prelude)
    stage = builder.build_solve_stage(shim_db, prelude, stage_a, mode="production")
    assert stage.demand.post is None
    assert "first_attempt_config" in stage.errors["demand.post"]
    assert stage.fixed_cut_summary["reason"] == "production_missing_config"


def test_build_solve_stage_production_mode_reads_demand_post_from_config_without_requery(shim_db, monkeypatch):  # noqa: F811, E501
    """C, D20 — `mode="production"` 이면 고정근무 재조회(`_run_fixed_cut`)를 부르지 않고,
    `first_attempt_config`(그 자리 `config_dict`, 이미 차감이 끝난 값)을 그대로 스냅샷한다."""
    builder = _builder()
    ward = base_ward(1)
    fixed = nurse_row("F01", 99, fixed_shift="D")
    ward.nurses = [*ward.nurses, fixed]
    _seed(shim_db, ward)
    prelude = _make_prelude(ward, fixed_nurses=[fixed])
    stage_a = builder.build_precheck_stage(shim_db, prelude)
    calls: list[int] = []
    monkeypatch.setattr(builder, "_run_fixed_cut", lambda *a, **k: (calls.append(1), (None, None))[1])
    already_cut_config = {**prelude.config_dict,
                          "daily_shift_requirements_by_day": [{"D": 1}] * DAYS,
                          "daily_shift_requirements_max_by_day": [{"D": 1}] * DAYS}
    stage = builder.build_solve_stage(shim_db, prelude, stage_a,
                                      first_attempt_config=already_cut_config, mode="production")
    assert calls == []  # 재조회 0
    assert stage.demand.post.by_day == already_cut_config["daily_shift_requirements_by_day"]
    assert stage.demand.post.max_by_day == already_cut_config["daily_shift_requirements_max_by_day"]
    assert stage.fixed_cut_summary["reason"] == "production_mode_passthrough"
    # 리뷰 지적 L15 — `applied` 는 고정 `False` 가 아니라 pre 와 실제로 다른지로 판정한다.
    assert stage.fixed_cut_summary["applied"] is True


def test_build_solve_stage_offline_mode_still_requeries_fixed_cut(shim_db):  # noqa: F811
    """대조 — `mode="offline"`(기본)은 지금까지처럼 9-0 함수로 다시 계산한다(바이트 불변 확인)."""
    builder = _builder()
    ward = base_ward(1)
    fixed = nurse_row("F01", 99, fixed_shift="D")
    ward.nurses = [*ward.nurses, fixed]
    _seed(shim_db, ward)
    prelude = _make_prelude(ward, fixed_nurses=[fixed])
    stage_a = builder.build_precheck_stage(shim_db, prelude)
    stage = builder.build_solve_stage(shim_db, prelude, stage_a, mode="offline")
    assert stage.fixed_cut_summary["reason"] == "ok"
    assert stage.fixed_cut_summary["applied"] is True


def test_build_solve_stage_production_mode_skips_post(shim_db, monkeypatch):  # noqa: F811
    """C, D20 — `mode="production"` 이면 post 6종(`_record_post`)을 전혀 부르지 않는다."""
    builder = _builder()
    ward = base_ward(2)
    _seed(shim_db, ward)
    prelude = _make_prelude(ward)
    stage_a = builder.build_precheck_stage(shim_db, prelude)
    calls: list[int] = []
    monkeypatch.setattr(builder, "_record_post", lambda *a, **k: (calls.append(1), ({}, {}))[1])
    stage = builder.build_solve_stage(shim_db, prelude, stage_a, mode="production")
    assert calls == []
    assert stage.post == {}


def test_build_engine_input_production_mode_skips_raw_column(shim_db):  # noqa: F811
    """C, D20 — `mode="production"` 이면 `raw_reader` 를 한 번도 안 부른다.

    (리뷰 지적 — 호출 0 이 '그 SQL' 0 을 보장하는 건 raw 가 유일한 자리라서다. `_run_fixed_cut`·
    `_record_post` 처럼 **다른 경로로도** 같은 SQL 을 낼 수 있는 함수의 호출 0 과는 다르다 —
    그쪽은 `test_build_engine_input_production_mode_touches_none_of_the_post_raw_fixed_cut_
    tables`(아래, 실제 SQL 문장으로 단언)가 진짜 보장을 맡는다.)
    """
    builder = _builder()
    ward = base_ward(1)
    _seed(shim_db, ward)
    prelude = _make_prelude(ward)
    calls: list[str] = []

    def _counting_reader(db, key, *, office_id, group_id):
        calls.append(key)
        return builder.read_raw_setting(db, key, office_id=office_id, group_id=group_id)

    ei = builder.build_engine_input(shim_db, prelude, raw_reader=_counting_reader, mode="production")
    assert calls == []
    assert ei.load_report.raw_column == {}


def test_build_engine_input_offline_mode_still_reads_raw_column(shim_db):  # noqa: F811
    """대조 — `mode="offline"`(기본)은 지금까지처럼 raw 컬럼 3종을 그대로 읽는다(바이트 불변)."""
    builder = _builder()
    ward = base_ward(1)
    _seed(shim_db, ward)
    prelude = _make_prelude(ward)
    ei = builder.build_engine_input(shim_db, prelude, mode="offline")
    assert set(ei.load_report.raw_column) == set(builder.RAW_SETTING_KEYS)


def test_build_engine_input_production_mode_has_strictly_fewer_queries_than_offline(shim_db, engine):  # noqa: F811, E501
    """쿼리 카운터(C, D20) — 같은 고정근무자 `prelude` 에서 `mode="production"` 이
    `mode="offline"` 보다 SQL 문장 수가 **엄격히 적다**(post·raw·고정근무 재조회가 빠진 만큼).
    ④⑤⑥(§1.3) 재조회는 모드와 무관하게 양쪽에서 똑같이 돈다 — D20 이 가리키는 건 그게 아니다.
    """
    builder = _builder()
    ward = base_ward(1)
    fixed = nurse_row("F01", 99, fixed_shift="D")
    ward.nurses = [*ward.nurses, fixed]
    _seed(shim_db, ward)
    prelude = _make_prelude(ward, fixed_nurses=[fixed])

    def _query_count(mode: str) -> int:
        log = QueryLog()
        log.attach(engine)
        log.armed = True
        try:
            builder.build_engine_input(shim_db, prelude, mode=mode)
        finally:
            log.detach()
        return len(log.statements)

    offline_count = _query_count("offline")
    production_count = _query_count("production")
    assert production_count < offline_count, (production_count, offline_count)


#: post·raw·고정근무 재조회가 실제로 읽는 표(C, D20 — 운영 모드는 이 표들과 교집합이 0 이어야
#: 한다). `roster_config`·`information_schema`(raw 3종) · `fixed_wanted_entries`·`daily_shift`·
#: `nurses`(9-0 `read_fixed_cut_inputs` 고정근무 재조회) · `nurse_assignment`(leave_mask).
_POST_RAW_FIXED_CUT_ONLY_TABLES = (
    "roster_config", "information_schema", "fixed_wanted_entries", "daily_shift",
    "nurse_assignment",
)


def test_build_engine_input_production_mode_touches_none_of_the_post_raw_fixed_cut_tables(shim_db, engine):  # noqa: F811, E501
    """실제 SQL 문장으로 'post·raw·고정근무 재조회 SQL 0'을 단언한다(리뷰 지적 HIGH).

    기존 단언(`production_count < offline_count`, 위)은 문장 **수**만 '엄격히 적다'를 보여
    MUT-fin-B-1 류(post 가 production 모드에서도 호출되는 회귀)·N3a~c(후처리 헬퍼를 `_record_
    post`/`_run_fixed_cut` 바깥에서 직접 부름)를 못 잡는다. 이 테스트는 production 모드의
    SQL 문장 **집합**과 `_POST_RAW_FIXED_CUT_ONLY_TABLES` 의 교집합이 0 임을 직접 본다.
    ward 는 고정근무자(F01)를 둬 offline 쪽이 그 표들을 실제로 읽는지(헛돌이 아님)도 같이 확인한다.
    """
    builder = _builder()
    ward = base_ward(2)
    fixed = nurse_row("F01", 99, fixed_shift="D")
    ward.nurses = [*ward.nurses, fixed]
    _seed(shim_db, ward)
    prelude = _make_prelude(ward, fixed_nurses=[fixed])
    already_cut_config = {**prelude.config_dict,
                          "daily_shift_requirements_by_day": [{"D": 1}] * DAYS,
                          "daily_shift_requirements_max_by_day": [{"D": 1}] * DAYS}

    def _statements(mode: str, **kwargs) -> list[str]:
        log = QueryLog()
        log.attach(engine)
        log.armed = True
        try:
            builder.build_engine_input(shim_db, prelude, mode=mode, **kwargs)
        finally:
            log.detach()
        return log.statements

    offline_statements = _statements("offline")
    production_statements = _statements("production", first_attempt_config=already_cut_config)
    offline_text = " ".join(offline_statements)
    assert any(marker in offline_text for marker in _POST_RAW_FIXED_CUT_ONLY_TABLES), (
        "offline 쪽이 그 표들을 안 읽어 비교가 헛돈다", offline_statements)
    for stmt in production_statements:
        assert not any(marker in stmt for marker in _POST_RAW_FIXED_CUT_ONLY_TABLES), stmt


# ══════════════════════════════════════════════════════════════════════════
# build_engine_input — 합본(단계 A + B), 무raise
# ══════════════════════════════════════════════════════════════════════════


def test_build_engine_input_happy_path(shim_db):  # noqa: F811
    builder = _builder()
    ward = base_ward(3)
    _seed(shim_db, ward)
    prelude = _make_prelude(ward)
    ei = builder.build_engine_input(shim_db, prelude)
    assert ei.ctx.days == DAYS
    assert ei.load_report.errors == {}
    assert ei.settings_raw["day_req"] == 2
    assert ei.grade["constraints_json"]["1"] == {"D": 1, "E": 1, "N": 1}  # base_ward() grade_row
    assert ei.grade["use_dynamic_scaling"] is True
    # `cells.initial_constraints` 는 리뷰 지적 수정분 — ④⑤⑥(이 ward 는 전부 빈 결과) 병합이다.
    assert ei.cells == {"combined_fixed_cells": [],
                        "initial_constraints": {"forced_off": {}, "forbidden": {}}}
    # rcs 와 같은 진위 판정(`if prev_month_n_tail:`) — 값이 전부 0 이어도 dict 자체가 비지
    # 않았으면 담는다(간호사 3명 모두 꼬리 0, base_ward() 는 전월 근무표가 없는 병동).
    assert ei.carryover == {"prev_month_n_tail": {"N01": 0, "N02": 0, "N03": 0}}
    assert ei.load_report.raw_column == {
        "fixed_holiday_off_yn": False, "n2n_min_gap": 0, "weekly_off_settings_activate": False}
    # O-15 — codebook·roster·settings_eff.
    assert ei.codebook["code2main"] == {"D": "D", "E": "E", "N": "N", "OFF": "O", "주": "O", "O": "O"}
    assert [n["nurse_id"] for n in ei.roster["nurses"]] == ["N01", "N02", "N03"]
    assert ei.roster["nurse_id_to_engine_index"] == {"N01": 0, "N02": 1, "N03": 2}
    assert ei.settings_eff["__type__"] == "NurseRosterConfig"
    assert ei.settings_eff["daily_shift_requirements"] == {"D": 2, "E": 2, "N": 2}


def test_build_engine_input_isolates_db_parts_from_prelude_and_cache(shim_db):  # noqa: F811
    """`settings_raw`·`preferences`·`cells`·`team` 는 `prelude`/override config 와 **다른 객체**다
    (BR-6, 리뷰 지적 — 이전엔 `EngineInput` 을 받은 쪽이 값을 바꾸면 `prelude`·주입 config 까지
    같이 바뀌었다). `team_min_by_team` override 경로(같은 리스트를 참조로 들고 있던 바로 그
    경우)로 가장 좁게 재현한다.
    """
    builder = _builder()
    ward = base_ward(1)
    _seed(shim_db, ward)
    sentinel_team = {"D": 9}
    prelude = _make_prelude(ward, preferences=[{"nurse_id": "N01", "pref": "D"}],
                            combined_fixed_cells=[{"nurse_id": "N01", "day": 1}])
    override = {**prelude.config_dict, "team_min_by_team": {"1": sentinel_team}}
    ei = builder.build_engine_input(shim_db, prelude, first_attempt_config=override)

    assert ei.settings_raw is not prelude.config_dict
    assert ei.preferences == prelude.preferences and ei.preferences is not prelude.preferences
    assert (ei.cells["combined_fixed_cells"] == prelude.combined_fixed_cells
            and ei.cells["combined_fixed_cells"] is not prelude.combined_fixed_cells)
    assert ei.team["team_min_by_team"]["1"] is not sentinel_team
    # O-15 — roster.nurses 도 prelude.nurses_for_engine 과 다른 객체(deepcopy, BR-6).
    assert ei.roster["nurses"] is not prelude.nurses_for_engine
    assert ei.roster["nurses"][0] is not prelude.nurses_for_engine[0]

    # 사본이 진짜 격리돼 있는지 — EngineInput 쪽을 바꿔도 원본·override 는 그대로.
    ei.preferences[0]["pref"] = "N"
    ei.cells["combined_fixed_cells"][0]["day"] = 99
    ei.roster["nurses"][0]["name"] = "바뀜"
    ei.team["team_min_by_team"]["1"]["D"] = -1
    assert prelude.preferences[0]["pref"] == "D"
    assert prelude.combined_fixed_cells[0]["day"] == 1
    assert sentinel_team["D"] == 9
    assert prelude.nurses_for_engine[0]["name"] == "N01"  # roster 사본도 원본에 안 닿는다


def test_build_solve_stage_isolates_carryover_and_demand_post(shim_db):  # noqa: F811
    """BR-6 — `carryover`·`demand.post` 도 `prelude`/`first_attempt_config`(production 모드 —
    EI-13a 에서는 운영이 쓸 그 config_dict 자체) 와 다른 객체다(리뷰 지적 M-B — 이전엔
    `_effective_off_window_constraints` 폴백이 `config` 의 dict 를 그대로 돌려줘(off_window)·
    production 모드 `demand.post` 가 `first_attempt_config` 의 dict 와 같은 객체였다).
    """
    builder = _builder()
    ward = base_ward(1)
    _seed(shim_db, ward)
    prelude = _make_prelude(ward)
    prelude.config_dict["off_window_constraints"] = {"N01": [[1, 1]]}
    stage_a = builder.build_precheck_stage(shim_db, prelude)
    live_config = {**prelude.config_dict,
                   "daily_shift_requirements_by_day": [{"D": 1}]}
    stage = builder.build_solve_stage(shim_db, prelude, stage_a,
                                      first_attempt_config=live_config, mode="production")

    stage.carryover["off_window_constraints"]["N01"].append([9, 9])
    stage.demand.post.by_day[0]["D"] = 99
    assert prelude.config_dict["off_window_constraints"]["N01"] == [[1, 1]]
    assert live_config["daily_shift_requirements_by_day"][0]["D"] == 1


def test_build_engine_input_never_raises_when_a_part_fails(shim_db, monkeypatch):  # noqa: F811
    """stage_a 의 등급 getter 가 깨져도 `build_engine_input` 은 raise 하지 않는다(§2.4-2).

    `_fetch_grade_config_dict` 자체는 자기 예외를 삼키고 기본값을 돌려주므로(9-0 과 같은 폭),
    여기서는 그 함수를 직접 깨뜨려 '빌더 바깥 레이어가 raise 하지 않는지' 를 확인한다.
    """
    rcs = _rcs()
    builder = _builder()
    ward = base_ward(2)
    _seed(shim_db, ward)
    prelude = _make_prelude(ward)

    def _boom(*a, **k):
        raise RuntimeError("grade-boom")

    monkeypatch.setattr(rcs, "_fetch_grade_config_dict", _boom)
    ei = builder.build_engine_input(shim_db, prelude)
    assert ei.grade == {}
    assert "grade-boom" in ei.load_report.errors["grade"]
    assert ei.demand.pre.base == {"D": 2, "E": 2, "N": 2}  # 나머지 부품은 영향 없음


def test_build_engine_input_records_error_when_solve_stage_itself_fails(shim_db, monkeypatch):  # noqa: F811
    """`build_solve_stage` 가 통째로 실패해도 등급·raw_column 등 나머지는 채워진다."""
    builder = _builder()
    ward = base_ward(2)
    _seed(shim_db, ward)
    prelude = _make_prelude(ward)

    def _boom(*a, **k):
        raise RuntimeError("solve-stage-boom")

    monkeypatch.setattr(builder, "build_solve_stage", _boom)
    ei = builder.build_engine_input(shim_db, prelude)
    assert "solve-stage-boom" in ei.load_report.errors["solve_stage"]
    assert ei.grade["constraints_json"]["1"] == {"D": 1, "E": 1, "N": 1}  # 등급=stage_a 독립, 그대로
    assert ei.demand.pre.base == {}                            # stage_b 없음 → EngineInput 기본값


def test_build_engine_input_has_a_last_resort_guard_outside_safe_call(shim_db, monkeypatch):  # noqa: F811
    """`_safe_call` 로 감싸지 않은 자리(`build_precheck_stage` 호출 자체)가 터져도 raise 하지
    않는다(리뷰 지적 — 마지막 방어선). `ei.ctx.days` 는 그래도 채워진다(예외 **전에** 계산).
    """
    builder = _builder()
    ward = base_ward(1)
    _seed(shim_db, ward)
    prelude = _make_prelude(ward)

    def _boom(*a, **k):
        raise RuntimeError("precheck-stage-boom")

    monkeypatch.setattr(builder, "build_precheck_stage", _boom)
    ei = builder.build_engine_input(shim_db, prelude)
    assert "precheck-stage-boom" in ei.load_report.errors["build"]
    assert ei.ctx.days == DAYS
    assert ei.grade == {} and ei.demand.pre.base == {}


def test_build_engine_input_uses_raw_reader_for_raw_column(shim_db):  # noqa: F811
    """`raw_reader` 교체 — 9d `read_raw_setting` 대신 꽂아도 `load_report.raw_column` 에 반영된다."""
    from services.engine_input.raw import RawRead

    builder = _builder()
    ward = base_ward(1)
    _seed(shim_db, ward)
    prelude = _make_prelude(ward)

    def _fake_reader(db, key, *, office_id, group_id):
        return RawRead(key, "fake", "test")

    ei = builder.build_engine_input(shim_db, prelude, raw_reader=_fake_reader)
    assert ei.load_report.raw_column == {k: "fake" for k in builder.RAW_SETTING_KEYS}


def test_build_engine_input_unknown_mode_falls_back_to_production_not_offline(shim_db):  # noqa: F811, E501
    """리뷰 지적 LOW — 모르는 `mode` 값(오타)은 조용히 `"offline"` 으로 떨어지지 않는다(D20
    위반을 소리 없이 만드는 쪽). `errors["mode"]` 에 남기고 안전한 `"production"`(조회 0)으로
    처리한다 — raw_column 이 비고, 고정근무 재조회도 없다."""
    builder = _builder()
    ward = base_ward(1)
    fixed = nurse_row("F01", 99, fixed_shift="D")
    ward.nurses = [*ward.nurses, fixed]
    _seed(shim_db, ward)
    prelude = _make_prelude(ward, fixed_nurses=[fixed])
    ei = builder.build_engine_input(shim_db, prelude, mode="prod")  # 오타
    assert "mode" in ei.load_report.errors
    assert ei.load_report.raw_column == {}


# ══════════════════════════════════════════════════════════════════════════
# import 모양
# ══════════════════════════════════════════════════════════════════════════


def test_solve_stage_module_has_no_heavy_top_level_imports():
    """post·stage-B 헬퍼가 쓰는 서비스 모듈도 함수 안에서만 지연 import 한다(순환 방지 가드)."""
    import ast
    from pathlib import Path

    import services.engine_input.builder as builder_module

    forbidden = {
        "services.roster_create_service", "services.leave.health_leave_planner",
        "services.leave.sleep_off_postprocess", "services.cp_sat.off_swap",
        "services.leave_mask_postprocess", "services.oncall_assign", "services.oncall_postprocess",
        "services.engine_contract.env", "services.cp_sat_basic",
    }
    tree = ast.parse(Path(builder_module.__file__).read_text())
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module not in forbidden, ast.dump(node)
        elif isinstance(node, ast.Import):
            assert all(alias.name not in forbidden for alias in node.names)
