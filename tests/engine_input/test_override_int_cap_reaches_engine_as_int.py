"""요청 `config_override` 의 정수 상한 하드락 키(#1·#6) 문자열·실수 값이 엔진에는 정수로 닿는다 — EI-11 11r(리뷰 LOW).

판정(요청 가드·생성 진입 관문·카드 필터)은 `int()` 로 읽어 상한 안이면 통과시킨다. 예전엔 원값('5'·5.0·14.5)이 그대로
엔진까지 가 TypeError(500 — 워커는 코드 결함으로 보고 재시도·DLQ)였다. 지금은 생성 서비스가 관문 뒤에서 판정이 본 정수로
바꿔 넘긴다(`roster_create_service._normalize_int_cap_override`). 판정 자체는 그대로다(C5 ⑦ — 가드 400 ⇔ 카드 BLOCK).
"""

from __future__ import annotations

import dataclasses

from .harness import run_with_output
from .scenario_defs import SCENARIOS, _request


def _engine_config(engine, override: dict) -> dict:
    scenario = dataclasses.replace(SCENARIOS["S1"], make_request=_request(2026, 11, config_override=override))
    rec = run_with_output(engine, scenario).result
    assert rec.layers.get("L1"), (rec.outcome, rec.error)
    return rec.layers["L1"][0]["config_data"]


def test_string_and_float_caps_reach_the_engine_as_ints(engine, engine_input_tables):
    """S1 + `{'max_conseq_work': '5', 'max_nig_per_month': 14.5}` → 엔진 입력(L1)은 5 · 14(정수)."""
    config = _engine_config(engine, {"max_conseq_work": "5", "max_nig_per_month": 14.5})
    assert config["max_conseq_work"] == 5 and type(config["max_conseq_work"]) is int
    assert config["max_nig_per_month"] == 14 and type(config["max_nig_per_month"]) is int
