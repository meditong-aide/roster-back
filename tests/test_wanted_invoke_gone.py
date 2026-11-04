"""닫힌 옛 API POST /wanted/invoke — 로그인한 호출은 410, 로그인 안 한 호출은 그대로 401(11-04).

닫은 이유·관찰 계획은 `app/routers/wanted.py` 의 `invoke_graph` 설명.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers.auth import get_current_user_from_cookie
from routers.wanted import router as wanted_router
from schemas.auth_schema import User as UserSchema


def _client(user):
    app = FastAPI()
    app.include_router(wanted_router)
    app.dependency_overrides[get_current_user_from_cookie] = lambda: user
    return TestClient(app)


def _user():
    return UserSchema(
        nurse_id="N001", account_id="acc_N001", office_id="OFF001", group_id="GRP001", name="김민지",
        mb_part="", office_name="", mb_part_name="", gw_useYN="Y", qpis_useYN="N", official_title_name=None,
    )


def test_wanted_invoke_is_gone_for_logged_in_caller():
    res = _client(_user()).post("/wanted/invoke", json={"request": "15일 쉬고 싶어", "year": 2026, "month": 11})
    assert res.status_code == 410
    assert res.json()["detail"]["code"] == "WANTED_INVOKE_GONE"


def test_wanted_invoke_still_needs_login():
    res = _client(None).post("/wanted/invoke", json={})
    assert res.status_code == 401
