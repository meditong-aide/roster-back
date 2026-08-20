"""P3 엔드포인트 배선 — /roster_create/async 가 config payload 를 materialize 하는지.

솔버 없이 검증(materialize/저장은 enqueue 전에 끝남). SQS 전송만 mock.
"""
import asyncio
from types import SimpleNamespace

import pytest
from starlette.requests import Request

import routers.roster_create as rc
from db.models import RosterConfig
from schemas.roster_schema import RosterRequest


def _cfg_payload(**ov):
    base = dict(
        min_exp_per_shift=1, req_exp_nurses=1, two_offs_per_week=True, max_nig_per_month=8,
        three_seq_nig=True, two_offs_after_three_nig=True, two_offs_after_two_nig=False,
        banned_day_after_eve=True, max_conseq_work=5, off_days=9, shift_priority=0.8,
        weekend_shift_ratio=1.0, patient_amount=0, even_nights=True, sequential_offs=True,
        nod_noe=False, preceptor_gauge=5,
    )
    base.update(ov)
    return base


@pytest.fixture
def adm_user():
    return SimpleNamespace(
        nurse_id="N001", account_id="acc_N001",
        group_id="GRP001", office_id="OFF001", is_master_admin=True,
    )


@pytest.fixture(autouse=True)
def _mock_sqs(monkeypatch):
    async def fake_sqs(body):
        return {"MessageId": "msg-test"}
    monkeypatch.setattr(rc, "_send_sqs_job", fake_sqs)


def _run(coro):
    # py3.13: get_event_loop() 격리 문제 회피 — 호출마다 새 루프.
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _call(req, adm_user, db):
    # request: FastAPI Request — 실서버는 자동 주입(로깅용 x-forwarded-for/client.host/user-agent).
    # 테스트는 최소 scope mock 으로 직접 주입.
    request = Request({"type": "http", "headers": [], "client": ("127.0.0.1", 0)})
    return _run(
        rc.roster_create_async(
            req, request, current_user=adm_user, _db=db, wait_for_result=False
        )
    )


def test_async_materializes_config_payload(db, seed_data, adm_user):
    req = RosterRequest(
        year=2026, month=7, group_id="GRP001", config=_cfg_payload(off_days=11)
    )
    out = _call(req, adm_user, db)

    mc = out["materialized_config"]
    assert mc is not None
    assert mc["config_name"] == "새로운 설정1"
    assert mc["version"] == 0
    # job 에 결정된 config_id 가 실리고, bulky config 는 슬림화됨
    assert out["job"]["params"]["config_id"] == mc["config_id"]
    assert out["job"]["params"]["config"] is None
    # 신규 프리셋 row 가 실제로 생성됨
    row = db.query(RosterConfig).filter(RosterConfig.config_id == mc["config_id"]).first()
    assert row is not None and row.version == 0 and row.off_days == 11


def test_async_without_config_payload_no_materialize(db, seed_data, adm_user):
    req = RosterRequest(year=2026, month=7, group_id="GRP001")  # config 없음
    out = _call(req, adm_user, db)
    assert out["materialized_config"] is None
