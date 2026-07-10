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


# ── 원티드 agent 통합: record_graph_usage (LangChain usage_metadata → agent_llm_usage) ──


def test_record_graph_usage_creates_rows_per_model(db):
    from agents_v2.usage import record_graph_usage

    usage_metadata = {
        "gpt-4o": {"input_tokens": 1500, "output_tokens": 300, "total_tokens": 1800},
        "claude-haiku-4-5": {"input_tokens": 500, "output_tokens": 100},
    }
    record_graph_usage(db, group_id="GRP001", user_id="N001",
                       usage_metadata=usage_metadata, purpose="wanted")

    rows = db.query(AgentLlmUsage).filter(AgentLlmUsage.purpose == "wanted").all()
    assert len(rows) == 2  # 모델별 1행
    assert {r.model for r in rows} == {"gpt-4o", "claude-haiku-4-5"}
    assert sum(r.input_tokens for r in rows) == 2000

    # 대시보드 by=purpose 에 원티드가 잡힘
    res = get_usage(by="purpose", days=None, current_user=_adm(), db=db)
    assert "wanted" in {r["key"] for r in res["rows"]}


def test_record_graph_usage_noop_on_empty(db):
    from agents_v2.usage import record_graph_usage

    record_graph_usage(db, group_id="GRP001", user_id="N", usage_metadata=None, purpose="wanted")
    record_graph_usage(db, group_id=None, user_id="N",
                       usage_metadata={"m": {"input_tokens": 1}}, purpose="wanted")
    assert db.query(AgentLlmUsage).count() == 0


# ── office 롤업 / 일·월 시계열 / 서버렌더 뷰 ──────────────────────────────────


def _seed_office(db):
    from db.models import Group, Office
    db.add_all([
        Office(office_id="OFF_A", office_name="A병원"),
        Office(office_id="OFF_B", office_name="B병원"),
        Group(group_id="GRP001", office_id="OFF_A", group_name="중환자실1"),
        Group(group_id="GRP002", office_id="OFF_A", group_name="중환자실2"),
        Group(group_id="GRP003", office_id="OFF_B", group_name="응급실"),
    ])
    ts1 = datetime(2026, 6, 10, 9, 0, 0)
    ts2 = datetime(2026, 7, 3, 14, 0, 0)
    db.add_all([
        AgentLlmUsage(group_id="GRP001", user_id="N1", model="gpt-5.5", purpose="turn",
                      input_tokens=1000, output_tokens=200, cost_usd=0.010, timestamp=ts1),
        AgentLlmUsage(group_id="GRP002", user_id="N2", model="gpt-5.5", purpose="turn",
                      input_tokens=500, output_tokens=100, cost_usd=0.005, timestamp=ts1),
        AgentLlmUsage(group_id="GRP003", user_id="N3", model="gpt-4o", purpose="wanted",
                      input_tokens=2000, output_tokens=300, cost_usd=0.020, timestamp=ts2),
    ])
    db.commit()


def test_usage_by_office_rollup(db):
    from agents_v2.usage import usage_by_office
    _seed_office(db)
    offs = usage_by_office(db)
    names = {o["office_name"] for o in offs}
    assert names == {"A병원", "B병원"}
    a = next(o for o in offs if o["office_name"] == "A병원")
    # A병원 총합 == 그 안 두 병동 합, 그리고 병동 2개
    assert len(a["groups"]) == 2
    assert a["cost_usd"] == round(0.010 + 0.005, 6)
    assert a["calls"] == 2
    assert {g["group_name"] for g in a["groups"]} == {"중환자실1", "중환자실2"}


def test_usage_timeseries_day_and_month(db):
    from agents_v2.usage import usage_timeseries
    _seed_office(db)
    daily = usage_timeseries(db, bucket="day")
    assert [d["bucket"] for d in daily] == ["2026-06-10", "2026-07-03"]
    # 누적 단조증가 + 마지막 누적 == 전체합
    assert daily[-1]["cum_cost_usd"] == round(0.010 + 0.005 + 0.020, 6)
    assert daily[0]["cum_cost_usd"] == round(0.015, 6)

    monthly = usage_timeseries(db, bucket="month")
    assert [m["bucket"] for m in monthly] == ["2026-06", "2026-07"]
    assert monthly[0]["cost_usd"] == round(0.015, 6)


def test_usage_timeseries_invalid_bucket(db):
    from agents_v2.usage import usage_timeseries
    with pytest.raises(ValueError):
        usage_timeseries(db, bucket="week")


def test_dashboard_view_renders_html(db):
    from agents_v2.usage_router import build_dashboard_data, render_dashboard_html
    _seed_office(db)
    data = build_dashboard_data(db)
    assert data["total"]["calls"] == 3
    assert len(data["offices"]) == 2
    assert len(data["daily"]) == 2
    html = render_dashboard_html(data)
    assert html.startswith("<!doctype html>")
    assert "LLM 사용량 대시보드" in html
    # 데이터 플레이스홀더가 실제 JSON 으로 치환됨
    assert "/*__USAGE_DATA__*/null" not in html
    assert "A병원" in html and "응급실" in html


def test_dashboard_view_empty_ok(db):
    from agents_v2.usage_router import build_dashboard_data, render_dashboard_html
    data = build_dashboard_data(db)
    assert data["offices"] == [] and data["daily"] == []
    html = render_dashboard_html(data)  # 빈 데이터도 렌더 실패 없이
    assert "<!doctype html>" in html
