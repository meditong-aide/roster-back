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
    body = put_resp.json()
    items = body["items"]
    assert len(items) == 1
    assert items[0]["d_min"] == 3 and items[0]["d_max"] == 3 and items[0]["d_exact"] == 3
    assert items[0]["n_min"] == 2 and items[0]["n_max"] == 2 and items[0]["n_exact"] == 2
    assert body["meta"]["target_nurse_count"] == 1
    assert body["meta"]["active_nurse_count"] == 6

    get_resp = api.get(
        "/nurses/monthly-limits",
        params={"group_id": "GRP001", "nurse_id": "N001"},
    )
    assert get_resp.status_code == 200, get_resp.text
    got_body = get_resp.json()
    got = got_body["items"]
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
    assert resp.status_code == 500, resp.text
    detail = resp.json().get("detail", {})
    infeas = detail.get("infeasibility") if isinstance(detail, dict) else {}
    codes = {i.get("reason_code") for i in (infeas or {}).get("preflight_issues", [])}
    assert "MONTHLY_LIMIT_GROUP_EXACT_SUM_EXCEEDS" in codes


def test_monthly_limits_warn_when_override_ratio_exceeds_30_percent(api):
    payload = {
        "year": 2026,
        "month": 5,
        "limits": [
            {
                "nurse_id": "N001",
                "group_id": "GRP001",
                "year": 2026,
                "month": 5,
                "o_exact": 8,
            },
            {
                "nurse_id": "N002",
                "group_id": "GRP001",
                "year": 2026,
                "month": 5,
                "o_exact": 8,
            },
        ],
    }
    resp = api.put("/nurses/monthly-limits", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["meta"]["target_nurse_count"] == 2
    assert body["meta"]["active_nurse_count"] == 6
    warnings = body.get("warnings") or []
    assert any(w.get("code") == "OVERRIDE_RATIO_EXCEEDED" for w in warnings)


def test_monthly_limits_delete_row_when_all_bounds_are_null(api):
    create_payload = {
        "year": 2026,
        "month": 5,
        "limits": [
            {
                "nurse_id": "N001",
                "group_id": "GRP001",
                "year": 2026,
                "month": 5,
                "o_exact": 8,
            }
        ],
    }
    create_resp = api.put("/nurses/monthly-limits", json=create_payload)
    assert create_resp.status_code == 200, create_resp.text
    assert len(create_resp.json()["items"]) == 1

    clear_payload = {
        "year": 2026,
        "month": 5,
        "limits": [
            {
                "nurse_id": "N001",
                "group_id": "GRP001",
                "year": 2026,
                "month": 5,
                "d_min": None,
                "d_max": None,
                "d_exact": None,
                "e_min": None,
                "e_max": None,
                "e_exact": None,
                "n_min": None,
                "n_max": None,
                "n_exact": None,
                "o_min": None,
                "o_max": None,
                "o_exact": None,
            }
        ],
    }
    clear_resp = api.put("/nurses/monthly-limits", json=clear_payload)
    assert clear_resp.status_code == 200, clear_resp.text
    assert clear_resp.json()["items"] == []


def test_monthly_limits_get_forbidden_for_other_group(api):
    resp = api.get(
        "/nurses/monthly-limits",
        params={"group_id": "GRP999", "nurse_id": "N001"},
    )
    assert resp.status_code == 403, resp.text


def test_monthly_limits_reject_duplicate_scope_in_single_request(api):
    payload = {
        "year": 2026,
        "month": 5,
        "limits": [
            {
                "nurse_id": "N001",
                "group_id": "GRP001",
                "year": 2026,
                "month": 5,
                "d_exact": 10,
            },
            {
                "nurse_id": "N001",
                "group_id": "GRP001",
                "year": 2026,
                "month": 5,
                "e_exact": 10,
            },
        ],
    }
    resp = api.put("/nurses/monthly-limits", json=payload)
    assert resp.status_code == 400, resp.text
    assert "중복" in resp.json().get("detail", "")
