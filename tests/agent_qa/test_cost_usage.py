"""cost.compute_cost + usage.record_llm_usage/usage_summary 검증.

테이블 부재 graceful skip(세션 무오염)은 격리 인메모리 세션으로, 기록/집계는 테이블을
직접 만든 격리 세션으로 검증(공유 conftest 엔진 비의존).
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import agents_v2.usage as usage_mod
from agents_v2.cost import compute_cost, price_for
from db.models import AgentLlmUsage


def _session(with_table: bool):
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    if with_table:
        AgentLlmUsage.__table__.create(bind=eng)
    return sessionmaker(bind=eng)()


@pytest.fixture(autouse=True)
def _reset_usage_cache():
    usage_mod._usage_table_present = None
    yield
    usage_mod._usage_table_present = None


# ── cost ──

def test_compute_cost_math():
    # gpt-4.1-mini = (0.40, 1.60)/1M → 1M+1M tok = 0.40 + 1.60 = 2.00
    assert compute_cost("gpt-4.1-mini", 1_000_000, 1_000_000) == pytest.approx(2.00)


def test_price_for_prefix_match_dated_suffix():
    assert price_for("gpt-5.4-mini-2026-03-17") == (0.75, 4.50)
    assert price_for("gpt-5.4-nano") == (0.20, 1.25)


def test_compute_cost_unknown_model_zero():
    assert compute_cost("totally-unknown-model", 1000, 1000) == 0.0
    assert compute_cost(None, 1000, 1000) == 0.0


# ── record: graceful skip ──

def test_record_missing_table_does_not_poison():
    s = _session(with_table=False)
    try:
        assert s.execute(text("SELECT 1")).scalar() == 1
        usage_mod.record_llm_usage(
            s, conversation_id="c1", group_id="GRP", user_id="n1",
            model="gpt-5.4-nano", purpose="turn", input_tokens=100, output_tokens=10,
        )
        assert s.execute(text("SELECT 1")).scalar() == 1  # 세션 미오염
        assert usage_mod._usage_table_present is False
    finally:
        s.close()


def test_record_skips_without_group_or_tokens():
    s = _session(with_table=True)
    try:
        usage_mod.record_llm_usage(
            s, conversation_id="c1", group_id=None, user_id="n1",
            model="gpt-5.4-nano", purpose="turn", input_tokens=100, output_tokens=10,
        )  # group_id 없음 → skip
        usage_mod.record_llm_usage(
            s, conversation_id="c1", group_id="GRP", user_id="n1",
            model="gpt-5.4-nano", purpose="turn", input_tokens=0, output_tokens=0,
        )  # 0-토큰 → skip
        assert s.query(AgentLlmUsage).count() == 0
    finally:
        s.close()


# ── record + summary ──

def test_record_and_summary_by_group_and_nurse():
    s = _session(with_table=True)
    try:
        # GRP-A: nurse n1 (turn) + nurse n2 (router); GRP-B: n3
        usage_mod.record_llm_usage(
            s, conversation_id="c1", group_id="GRP-A", user_id="n1",
            model="gpt-4.1-mini", purpose="turn",
            input_tokens=1_000_000, output_tokens=0,  # = $0.40
        )
        usage_mod.record_llm_usage(
            s, conversation_id="c1", group_id="GRP-A", user_id="n2",
            model="gpt-5.4-nano", purpose="router",
            input_tokens=1_000_000, output_tokens=0,  # = $0.20
        )
        usage_mod.record_llm_usage(
            s, conversation_id="c2", group_id="GRP-B", user_id="n3",
            model="gpt-4.1-mini", purpose="turn",
            input_tokens=1_000_000, output_tokens=0,  # = $0.40
        )
        assert s.query(AgentLlmUsage).count() == 3

        by_group = usage_mod.usage_summary(s, by="group")
        gmap = {r["key"]: r for r in by_group}
        assert gmap["GRP-A"]["cost_usd"] == pytest.approx(0.60)  # 0.40+0.20
        assert gmap["GRP-B"]["cost_usd"] == pytest.approx(0.40)
        # 비용 내림차순 정렬
        assert by_group[0]["key"] == "GRP-A"

        by_nurse = usage_mod.usage_summary(s, by="nurse", group_id="GRP-A")
        nmap = {r["key"]: r for r in by_nurse}
        assert set(nmap) == {"n1", "n2"}
        assert nmap["n1"]["cost_usd"] == pytest.approx(0.40)
    finally:
        s.close()


def test_usage_summary_invalid_by_raises():
    s = _session(with_table=True)
    try:
        with pytest.raises(ValueError):
            usage_mod.usage_summary(s, by="bogus")
    finally:
        s.close()
