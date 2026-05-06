from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from db.client2 import get_db
from routers.auth import get_current_user_from_cookie
from routers.nurses import router as nurses_router
from schemas.auth_schema import User as UserSchema


def _make_user(*, group_id: str, office_id: str, is_head_nurse: bool = True) -> UserSchema:
    return UserSchema(
        nurse_id="N001",
        account_id="acc_N001",
        office_id=office_id,
        group_id=group_id,
        is_head_nurse=is_head_nurse,
        is_master_admin=False,
        name="김민지",
        EmpSeqNo="",
        EmpAuthGbn="",
        mb_part="",
        office_name="테스트병원",
        mb_part_name="",
        gw_useYN="Y",
        qpis_useYN="Y",
        official_title_name=None,
        is_nurse_registered=True,
        hn_auth="HN" if is_head_nurse else None,
        original_group_id=group_id,
    )


@pytest.fixture
def api(db, seed_data):
    app = FastAPI()
    app.include_router(nurses_router)

    user = _make_user(group_id=seed_data["group_id"], office_id=seed_data["office_id"])

    def _override_db():
        yield db

    def _override_user():
        return user

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user_from_cookie] = _override_user

    client = TestClient(app)
    yield client
    app.dependency_overrides.clear()


def test_monthly_limits_upsert_and_list_round_trip(api):
    payload = {
        "year": 2026,
        "month": 5,
        "limits": [
            {
                "nurse_id": "N001",
                "group_id": "GRP001",
                "year": 2026,
                "month": 5,
                "d_exact": 3,
                "n_exact": 2,
                "o_min": 8,
                "o_max": 12,
            }
        ],
    }
    put_resp = api.put("/nurses/monthly-limits", json=payload)
    assert put_resp.status_code == 200, put_resp.text
    items = put_resp.json()["items"]
    assert len(items) == 1
    assert items[0]["d_min"] == 3 and items[0]["d_max"] == 3 and items[0]["d_exact"] == 3
    assert items[0]["n_min"] == 2 and items[0]["n_max"] == 2 and items[0]["n_exact"] == 2

    get_resp = api.get("/nurses/monthly-limits", params={"year": 2026, "month": 5})
    assert get_resp.status_code == 200, get_resp.text
    got = get_resp.json()["items"]
    assert len(got) == 1
    assert got[0]["nurse_id"] == "N001"


def test_monthly_limits_reject_exact_sum_over_capacity(api):
    # seed_data fixture gives a single team member N001 in this test setup,
    # so exact counts far above month capacity must fail.
    payload = {
        "year": 2026,
        "month": 5,
        "limits": [
            {
                "nurse_id": "N001",
                "group_id": "GRP001",
                "year": 2026,
                "month": 5,
                "d_exact": 20,
                "e_exact": 20,
            }
        ],
    }
    resp = api.put("/nurses/monthly-limits", json=payload)
    assert resp.status_code == 400, resp.text
    assert "exact 합" in resp.json().get("detail", "")
