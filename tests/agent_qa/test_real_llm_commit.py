"""Real-LLM 2턴 커밋 자동화 (멀티턴 갭 #3).

기존 real-LLM 테스트/벤치는 전부 **미리보기(turn1)에서 멈추고 rollback** — 즉 "실제 LLM이
낸 미리보기를 turn2 '응'으로 승인 → DB에 올바르게 반영"되는 라운드트립이 scripted double
로만 검증됐다. 여기서 실 LLM으로 turn1(자연어→mutation+preview) → turn2(승인→commit) →
**DB 실반영**까지 자동 검증한다.

네트워크 호출이라 기본 스위트 오염 방지 위해 `RUN_REAL_LLM=1` 일 때만 실행(키만으론 안 됨).
실행: RUN_REAL_LLM=1 pytest tests/agent_qa/test_real_llm_commit.py -q -s
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

from db.models import DailyShift, RosterConfig  # noqa: E402
from tests.agent_qa.harness import AgentTestSession  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.getenv("RUN_REAL_LLM"),
    reason="실 LLM 네트워크 호출 — RUN_REAL_LLM=1 일 때만 실행",
)


def _llm():
    from agents_v2.llm_client import get_llm_client
    return get_llm_client("openai")


def _cfg(db):
    return db.query(RosterConfig).filter_by(config_id=1, group_id="GRP001").first()


def test_react_single_mutation_commit(db, seed_data):
    """turn1 실 LLM: '연속근무 4일로' → 미리보기(무커밋) / turn2 '응' → 커밋(5→4).

    (야간 최대는 프롬프트상 '시스템 고정 관리'로 LLM이 거부 — whitelist 와 불일치, 별도 이슈.
     여기선 정책 오버레이 없는 max_conseq_work 로 커밋 라운드트립만 검증.)
    """
    assert _cfg(db).max_conseq_work == 5  # 초기값
    sess = AgentTestSession(db, month=8, client=_llm())

    r1 = sess.send("이 병동 연속근무를 최대 4일로 제한해줘")
    assert r1.awaiting_approval is True, f"미리보기 안 옴: {r1.answer[:120]}"
    assert _cfg(db).max_conseq_work == 5, "turn1 에서 이미 커밋됨(무커밋 위반)"

    r2 = sess.send("응")
    assert r2.awaiting_approval is False
    assert _cfg(db).max_conseq_work == 4, f"커밋 반영 안 됨: {r2.answer[:150]}"


def test_react_compound_independent_commit(db, seed_data):
    """turn1 실 LLM 복합(독립 2개): 연속근무 4 + 이브닝후 데이금지 해제 → turn2 '응' → 둘 다 커밋.

    ('월 오프' 류는 병동 config off_days vs 개인 월한도로 해석이 갈려 부적합 — 순수 config
     불리언/정수 2개로 다중-mutation ReAct 커밋만 검증.)
    """
    c = _cfg(db)
    assert (c.max_conseq_work, bool(c.banned_day_after_eve)) == (5, True)
    sess = AgentTestSession(db, month=8, client=_llm())

    r1 = sess.send("연속근무는 최대 4일로 하고, 이브닝 다음날 데이 금지는 해제해줘")
    assert r1.awaiting_approval is True, f"미리보기 안 옴: {r1.answer[:120]}"

    sess.send("응")
    c = _cfg(db)
    assert c.max_conseq_work == 4, "연속근무 한도 커밋 안 됨"
    assert bool(c.banned_day_after_eve) is False, "이브닝후 데이금지 해제 커밋 안 됨"


def test_dag_override_commit(db, seed_data):
    """turn1 실 LLM 의존복합(override): 전체 데이6 + 주말 데이2 → turn2 '응' → 원자 커밋.

    dag=True 로 DAG 경로 유도. 경로가 ReAct 로 fallback 하더라도 최종 DB 상태로 판정(path-agnostic).
    (turn1 무커밋 체크는 생략 — DAG dry-run 이 미리보기용으로 flush 하면 같은 세션에서 보여
     commit/flush 구분이 불가. 실 DB 반영 여부는 teardown rollback 으로 무해.)
    """
    sess = AgentTestSession(db, month=8, dag=True, client=_llm())

    r1 = sess.send("8월 전체 데이 필요인원을 6명으로 하고, 주말은 데이 2명으로 줄여줘")
    assert r1.awaiting_approval is True, f"미리보기 안 옴: {r1.answer[:150]}"

    sess.send("응")
    rows = [r for r in db.query(DailyShift).filter_by(group_id="GRP001", year=2026, month=8).all()
            if int(r.day) > 0]
    dcnts = {int(r.d_count) for r in rows}
    assert 6 in dcnts, f"평일 데이6 커밋 안 됨: {sorted(dcnts)}"
    assert 2 in dcnts, f"주말 데이2 override 커밋 안 됨: {sorted(dcnts)}"
