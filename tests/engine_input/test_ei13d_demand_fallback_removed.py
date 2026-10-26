"""EI-13d 시험 — 엔진 day_req/eve_req/nig_req 수요 폴백 삭제(설계서 §5.3 EI-13d · A2 SPEC).

  1. 계약 좁힘: plain dict 입력은 `daily_shift_requirements` 가 있어야 한다(없거나 비면 ValueError — 합 0 검사와 같은 메시지).
  2. 살아 있는 API(D14=②): `probe_feasibility`·`generate_roster_cp_sat` 도 풀이 전에 같은 ValueError 로 멈춘다.
  3. 생성 경로 불변: 프로덕션이 엔진에 넘기는 config 는 모든 시도에서 비지 않은 `daily_shift_requirements` 를 든다
     → 삭제한 분기는 생성 경로에서 도달 불가(골든 L1·사다리 attempts 로 확인).
"""
from __future__ import annotations

import ast
import contextlib
import io
from pathlib import Path

import pytest

from .test_dual_build import ALL_DUAL_BUILD_SCENARIOS, PRECHECK_BLOCK_SCENARIO, dual_build_results  # noqa: F401

OLD_FIELDS = {"day_req": 2, "eve_req": 2, "nig_req": 2}
#: 엔진 입구에 닿기 전에 막히는 차단 경로 — L1 이 없다.
BLOCKED = ("S9", "S9-F", PRECHECK_BLOCK_SCENARIO.name)
REACHED = [n for n in ALL_DUAL_BUILD_SCENARIOS if n not in BLOCKED]


def _engine():
    from services.cp_sat_basic import CPSATBasicEngine

    return CPSATBasicEngine()


def _create(config: dict):
    with contextlib.redirect_stdout(io.StringIO()):
        return _engine().create_config_from_db(dict(config))


@pytest.mark.parametrize("config", [
    dict(OLD_FIELDS),                                                         # dsr 부재
    {**OLD_FIELDS, "daily_shift_requirements": None},
    {**OLD_FIELDS, "daily_shift_requirements": {}},
    {**OLD_FIELDS, "daily_shift_requirements": {"D": 0, "E": 0, "N": 0}},     # 비지 않은 0 맵 — 삭제 전에도 ValueError
])
def test_old_fields_alone_no_longer_supply_demand(config):
    with pytest.raises(ValueError, match="daily_shift_requirements"):
        _create(config)


def test_daily_shift_requirements_wins_and_old_fields_are_ignored():
    cfg = _create({"daily_shift_requirements": {"D": 4, "E": 3, "N": 2}, "day_req": 9, "eve_req": 9, "nig_req": 9})
    assert cfg.daily_shift_requirements == {"D": 4, "E": 3, "N": 2}


def test_create_config_from_db_source_no_longer_reads_old_demand_fields():
    """`create_config_from_db` 안에서 day_req/eve_req/nig_req 를 읽는 호출이 0 이다(엔진의 다른 자리 — `[WInjectFix]` — 는 이 시험 밖)."""
    import services.cp_sat_basic as module

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "create_config_from_db")
    keys = {c.args[0].value for c in ast.walk(fn)
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) and c.func.attr == "get"
            and c.args and isinstance(c.args[0], ast.Constant) and isinstance(c.args[0].value, str)}
    assert keys.isdisjoint(OLD_FIELDS), sorted(keys & set(OLD_FIELDS))
    assert "daily_shift_requirements" in keys


def test_probe_feasibility_and_generate_entry_raise_when_demand_missing():
    """D14=② — `probe_feasibility`(plain dict 입력 API)는 살아 있다. 구 필드만 든 입력은 설정 생성 단계(풀이 전)에서 ValueError 로 멈춘다."""
    import services.cp_sat_basic as module

    for call in (module.probe_feasibility, module.generate_roster_cp_sat):
        with contextlib.redirect_stdout(io.StringIO()), pytest.raises(ValueError, match="daily_shift_requirements"):
            call([], [], dict(OLD_FIELDS), 2026, 11, [])


def _demand_of(config_data: dict):
    return config_data.get("daily_shift_requirements")


@pytest.mark.parametrize("name", REACHED)
def test_generation_path_always_hands_the_engine_a_nonempty_demand_dict(name, dual_build_results):  # noqa: F811
    """삭제한 폴백 분기(`not raw_daily_req`)는 생성 경로에서 도달 불가 — 모든 L1 호출 + 사다리·후처리 시도 delta 로 본다."""
    layers = dual_build_results[name].captured_run.result.layers
    l1 = layers.get("L1", [])
    assert l1, f"{name}: 엔진 입구(L1)에 닿지 않았다 — 비교가 헛돈다"
    for call in l1:
        dsr = _demand_of(call["config_data"])
        assert isinstance(dsr, dict) and dsr, (name, dsr)
    for attempt in layers.get("attempts", []):
        cfg_delta = (attempt.get("delta") or {}).get("config_data") or {}
        assert "daily_shift_requirements" not in (cfg_delta.get("removed") or []), (name, attempt["seq"])
        changed = (cfg_delta.get("changed") or {})
        if "daily_shift_requirements" in changed:
            dsr = changed["daily_shift_requirements"]
            assert isinstance(dsr, dict) and dsr, (name, attempt["seq"], dsr)
