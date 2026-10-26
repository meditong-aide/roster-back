"""요청 `config_override` 의 별칭이 판정(원래 이름) 뒤 정본 키로 바뀌어 엔진에 닿는다 — EI-12 12a(D9 · BR-3 순서).

판정(요청 가드)은 별칭·정본 둘 다 보고(상한 초과는 400), 그 뒤에 이름만 정본으로 바뀐다. 엔진 입력(L1 `config_data`)에는 별칭 키가 없다.
"""

from __future__ import annotations

import dataclasses
from functools import partial

import pytest
from services.engine_contract.settings import SETTINGS

from .harness import run_with_output
from .scenario_defs import SCENARIOS, _request, s1_ward
from .ward import seed_ward

ALIASES = frozenset(a for spec in SETTINGS for a in spec.aliases)


def _run(engine, **request_kwargs):
    scenario = dataclasses.replace(SCENARIOS["S1"], make_request=_request(2026, 11, **request_kwargs))
    return run_with_output(engine, scenario).result


def _l1(engine, **request_kwargs) -> dict:
    rec = _run(engine, **request_kwargs)
    assert rec.layers.get("L1"), (rec.outcome, rec.error)
    return rec.layers["L1"][0]["config_data"]


def test_alias_override_reaches_the_engine_as_canonical(engine, engine_input_tables):
    config = _l1(engine, config_override={"max_consecutive_work_days": 4, "max_night_shifts_per_month": 9})
    assert config["max_conseq_work"] == 4 and config["max_nig_per_month"] == 9
    assert not set(config) & ALIASES


def test_canonical_wins_over_alias_in_the_same_request(engine, engine_input_tables):
    config = _l1(engine, config_override={"max_conseq_work": 4, "max_consecutive_work_days": 5})
    assert config["max_conseq_work"] == 4 and not set(config) & ALIASES


def test_over_cap_alias_is_still_rejected_before_normalization(engine, engine_input_tables):
    rec = _run(engine, config_override={"max_consecutive_work_days": 6})
    assert rec.outcome == "http_exception" and rec.error == "HTTP 400" and "max_consecutive_work_days=6" in rec.http_detail


def test_runtime_forbidden_alias_is_still_rejected(engine, engine_input_tables):
    rec = _run(engine, config_override={"max_consecutive_nights": 3})
    assert rec.outcome == "http_exception" and "max_consecutive_nights" in rec.http_detail



def test_gate_bypass_cannot_apply_a_hard_lock_breaking_treatment_night_recovery(engine, engine_input_tables, monkeypatch):
    """12b — 진입 관문을 꺼도 적용 직후 재검이 400 으로 끊는다(2N·3N 회복 해제 = 하드락 #4·#5)."""
    from services.policy import card_filter

    monkeypatch.setattr(card_filter, "assert_treatments_allowed", lambda treatment_ids: None)
    rec = _run(engine, treatment_ids=["treatment:disable:night_recovery"],
               config_override={"two_offs_after_two_nig": True, "two_offs_after_three_nig": True})
    assert rec.outcome == "http_exception" and rec.error == "HTTP 400" and "하드락 #4" in rec.http_detail


_CONSEC = "treatment:threshold:consecutive_work"
_NIGHT = "treatment:threshold:monthly_night_cap"


def test_revived_night_cap_treatment_raises_the_engine_cap_by_one(engine, engine_input_tables):
    """정본화 전에는 처방이 별칭 키만 써 엔진 입력이 그대로였다(재생성해도 같은 결과)."""
    after = _l1(engine, treatment_ids=[_NIGHT], config_override={"max_nig_per_month": 9})  # S1 의 월 나이트 상한은 이미 15
    # 직접 호출(override + treatment_ids 동시)은 의도적으로 누적된다 — 새 카드는 크기 계산 값을 apply 에만 싣는다(`undiagnosed_probe._sized_ids`).
    assert after["max_nig_per_month"] == 10 and not set(after) & ALIASES
    assert _l1(engine, treatment_ids=[_NIGHT])["max_nig_per_month"] == 15  # 15 에서는 올리지 않는다(수동 분류)


def test_gate_bypass_cannot_raise_the_consecutive_work_cap(engine, engine_input_tables, monkeypatch):
    """12s — 진입 관문을 꺼도 연속근무 처방(+1 → 상한 5 가 6)은 적용 직후 재검이 400 '하드락 #1' 로 끊는다(처방 키가 정본이라 값을 쓴다)."""
    from services.policy import card_filter

    monkeypatch.setattr(card_filter, "assert_treatments_allowed", lambda treatment_ids: None)
    rec = _run(engine, treatment_ids=[_CONSEC], config_override={"max_conseq_work": 5})
    assert rec.outcome == "http_exception" and rec.error == "HTTP 400" and "하드락 #1" in rec.http_detail


def _run_ward(engine, ward_cap: int, **request_kwargs):
    """S1 의 병동 설정 `roster_config.max_conseq_work` 를 `ward_cap` 으로 바꿔 돌린다 — 요청 가드를 지나지 않는 병동 값이라 6 도 넣는다."""
    ward = s1_ward()
    ward.config["max_conseq_work"] = ward_cap
    scenario = dataclasses.replace(SCENARIOS["S1"], seed=partial(seed_ward, ward),
                                   make_request=_request(2026, 11, **request_kwargs))
    return run_with_output(engine, scenario).result


@pytest.mark.parametrize("ward_cap", [3, 4, 5, 6])
def test_consecutive_work_treatment_is_refused_at_the_gate(engine, engine_input_tables, ward_cap):
    """12t — 진입 관문은 엄격 표본(상한 5)으로 판정해 병동 값과 무관하게 400 이다(엔진 입력 L1 에 닿지 않는다)."""
    rec = _run_ward(engine, ward_cap, treatment_ids=[_CONSEC])
    assert rec.outcome == "http_exception" and rec.error == "HTTP 400" and "하드락 #1" in rec.http_detail
    assert not rec.layers["L1"]


@pytest.mark.parametrize(("ward_cap", "engine_cap"), [(3, 4), (4, 5), (5, None), (6, None)])
def test_gate_bypass_consecutive_work_treatment_by_ward_cap(engine, engine_input_tables, monkeypatch, ward_cap, engine_cap):
    """12t — 관문을 끄면 처방 +1 이 병동 값에 얹힌다. 상한 5 안(3→4·4→5)은 그 값 그대로 엔진 입력(L1)에 닿고, 넘으면
    (5→6·6→7) 12b 재검이 400 '하드락 #1' 로 끊는다(L1 에 닿지 않는다). `engine_cap=None` 이 재검 400 이다."""
    from services.policy import card_filter

    monkeypatch.setattr(card_filter, "assert_treatments_allowed", lambda treatment_ids: None)
    rec = _run_ward(engine, ward_cap, treatment_ids=[_CONSEC])
    if engine_cap is None:
        assert rec.outcome == "http_exception" and rec.error == "HTTP 400" and not rec.layers["L1"]
        assert rec.http_detail.startswith("처방 적용 결과가") and "하드락 #1" in rec.http_detail
        return
    assert rec.layers["L1"], (rec.outcome, rec.error, rec.http_detail)
    config = rec.layers["L1"][0]["config_data"]
    assert config["max_conseq_work"] == engine_cap and not set(config) & ALIASES
