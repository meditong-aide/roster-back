"""사용량 대시보드 엔드포인트 — 집계/권한/에이전트별 분리(purpose) 검증.

핸들러를 직접 호출(FastAPI DI 우회)해 usage_summary 집계 + HN/ADM 게이트를 확인.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from agents_v2.usage_router import get_usage
from db.models import AgentLlmUsage


def _seed(db):
    ts = datetime(2026, 7, 9, 0, 0, 0)
    db.add_all([
        AgentLlmUsage(group_id="GRP001", user_id="N001", model="gpt-5.5",
                      purpose="turn", input_tokens=1000, output_tokens=200, cost_usd=0.01, timestamp=ts),
        AgentLlmUsage(group_id="GRP001", user_id="N001", model="gpt-5.4-nano",
                      purpose="router", input_tokens=100, output_tokens=10, cost_usd=0.0001, timestamp=ts),
        AgentLlmUsage(group_id="GRP001", user_id="N002", model="gpt-5.5",
                      purpose="turn", input_tokens=500, output_tokens=100, cost_usd=0.005, timestamp=ts),
        # 원티드 agent 사용량(다른 그룹, purpose='wanted')
        AgentLlmUsage(group_id="GRP002", user_id="N003", model="gpt-5.5",
                      purpose="wanted", input_tokens=2000, output_tokens=300, cost_usd=0.02, timestamp=ts),
    ])
    db.commit()


def _hn(group="GRP001"):
    return SimpleNamespace(is_master_admin=False, is_head_nurse=True, hn_auth="HN", group_id=group)


def _adm():
    return SimpleNamespace(is_master_admin=True, is_head_nurse=False, hn_auth=None, group_id=None)


def _nurse():
    return SimpleNamespace(is_master_admin=False, is_head_nurse=False, hn_auth=None, group_id="GRP001")


def test_hn_sees_only_own_group(db):
    _seed(db)
    res = get_usage(by="group", days=None, current_user=_hn(), db=db)
    assert {r["key"] for r in res["rows"]} == {"GRP001"}  # GRP002 제외
    assert res["total"]["calls"] == 3


def test_adm_sees_all_groups(db):
    _seed(db)
    res = get_usage(by="group", days=None, current_user=_adm(), db=db)
    assert {r["key"] for r in res["rows"]} == {"GRP001", "GRP002"}
    assert res["total"]["calls"] == 4


def test_by_purpose_splits_agents(db):
    _seed(db)
    res = get_usage(by="purpose", days=None, current_user=_adm(), db=db)
    purposes = {r["key"] for r in res["rows"]}
    # 스케줄링(turn/router)과 원티드(wanted)가 purpose 로 분리됨
    assert {"turn", "router", "wanted"} <= purposes


def test_total_cost_aggregation(db):
    _seed(db)
    res = get_usage(by="model", days=None, current_user=_adm(), db=db)
    assert res["total"]["cost_usd"] == round(0.01 + 0.0001 + 0.005 + 0.02, 6)
    assert res["total"]["input_tokens"] == 1000 + 100 + 500 + 2000


def test_nurse_forbidden(db):
    with pytest.raises(HTTPException) as e:
        get_usage(by="group", days=None, current_user=_nurse(), db=db)
    assert e.value.status_code == 403


def test_invalid_by_400(db):
    with pytest.raises(HTTPException) as e:
        get_usage(by="bogus", days=None, current_user=_adm(), db=db)
    assert e.value.status_code == 400


def test_empty_usage_ok(db):
    # 데이터 없어도 200 형태(빈 rows + 0 total)
    res = get_usage(by="group", days=None, current_user=_adm(), db=db)
    assert res["rows"] == []
    assert res["total"]["cost_usd"] == 0
