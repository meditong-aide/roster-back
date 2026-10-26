"""`services.engine_input.model` 단위 — EI-9a(EI9_SPEC.md §2.2). 호출처 0 상태에서 타입만 본다.

타입 생성(필드가 설계서 §2.2 표 그대로인지)·불변(frozen)·`PreludeState` 모양을 확인한다.
digest·canonical 쪽은 `test_snapshot.py` 가 본다.
"""
from __future__ import annotations

import dataclasses

import pytest
from services.engine_input.model import (
    Ctx,
    DemandPart,
    DemandSnapshot,
    EngineInput,
    LoadReport,
    PreludeState,
)


def _sample_ei() -> EngineInput:
    return EngineInput(
        ctx=Ctx(office_id="102560", group_id="G1", year=2026, month=11, days=30,
                config_id=7, job_id="job-1", runtime="local"),
        settings_raw={"max_nig_per_month": 15, "last_generate_status": "success"},
        settings_eff={"max_nig_per_month": 15},
        codebook={"shift_definitions": {"D": {"id": 1}}},
        demand=DemandPart(
            pre=DemandSnapshot(base={"D": 4, "E": 3, "N": 2}),
            post=DemandSnapshot(base={"D": 3, "E": 3, "N": 2}),
        ),
        roster={"nurse_ids": ["N1", "N2"]},
        cells={"fixed": {}},
        carryover={"n_tail": {}},
        grade={"N1": 1},
        team={"min_by_team": {}},
        preferences={"N1": {}},
        post={"health_leave": {"targets": []}},
        solver_params={"time_limit": 60},
        env_effective={"AIDE_MAX_CONSEC_WORK": "6"},
        load_report=LoadReport(errors={}, fixed_cut={"applied": False}),
    )


# ── 타입 생성 ──────────────────────────────────────────────────────────────


def test_engine_input_constructs_with_sample_values():
    ei = _sample_ei()
    assert ei.schema_version == 1
    assert ei.ctx.office_id == "102560"
    assert ei.demand.pre.base["D"] == 4
    assert ei.demand.post.base["D"] == 3
    assert ei.grade == {"N1": 1}
    assert ei.load_report.fixed_cut == {"applied": False}


def test_engine_input_defaults_are_empty_not_shared():
    """default_factory 를 안 쓰면 인스턴스끼리 같은 dict/list 를 공유한다 — 그 함정이 없는지 확인."""
    a = EngineInput()
    b = EngineInput()
    assert a.settings_raw == {} and b.settings_raw == {}
    a.settings_raw["x"] = 1  # frozen 은 속성 재할당만 막는다 — 내부 dict 변경은 허용
    assert "x" not in b.settings_raw, "두 인스턴스가 같은 dict 를 공유하면 안 된다"
    assert a.demand.pre.base == {} and a.demand is not b.demand


def test_demand_part_post_optional_means_not_cut():
    """`demand.post is None` 은 '차감 안 함'(고정근무자 없음 또는 실패) — §2.2."""
    ei = EngineInput(demand=DemandPart(pre=DemandSnapshot(base={"D": 1})))
    assert ei.demand.post is None


# ── frozen(불변) ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("instance,attr", [
    (EngineInput(), "schema_version"),
    (Ctx(office_id="o", group_id="g", year=2026, month=1), "office_id"),
    (DemandSnapshot(), "base"),
    (DemandPart(pre=DemandSnapshot()), "pre"),
    (LoadReport(), "errors"),
])
def test_parts_are_frozen(instance, attr):
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(instance, attr, getattr(instance, attr))


# ── PreludeState(§2.4-1) ──────────────────────────────────────────────────


def test_prelude_state_has_fields_from_spec():
    """EI9_SPEC.md §2.4-1 목록 그대로 — ctx 는 `days` 없이 office·group·year·month·
    config_id·job_id·runtime 만 쓴다(연·월에서 파생 가능)."""
    prelude = PreludeState(
        ctx=Ctx(office_id="102560", group_id="G1", year=2026, month=11, config_id=7,
                job_id="job-1", runtime="local"),
        config_dict={"max_nig_per_month": 15},
        nurses_for_engine=[{"id": "N1"}],
        fixed_nurses=[{"id": "F1"}],
        fixed_special_requests=[{"nurse_id": "F1", "shift_id": None}],
        combined_fixed_cells={"F1": ["D"] * 30},
        shift_manage_data={"day_req": 4},
        preferences={"N1": {}},
        assignments={"inbound": ["N3"], "outbound": []},
    )
    assert prelude.ctx.days is None, "PreludeState.ctx 는 days 를 안 쓴다(§2.4-1)"
    assert prelude.nurses_for_engine == [{"id": "N1"}]
    assert prelude.assignments == {"inbound": ["N3"], "outbound": []}
    with pytest.raises(dataclasses.FrozenInstanceError):
        prelude.config_dict = {}


def test_prelude_state_defaults_are_independent():
    a = PreludeState(ctx=Ctx(office_id="o", group_id="g", year=2026, month=1))
    b = PreludeState(ctx=Ctx(office_id="o", group_id="g", year=2026, month=1))
    a.fixed_nurses.append({"id": "F1"})
    assert b.fixed_nurses == [], "기본값 리스트를 인스턴스끼리 공유하면 안 된다"
