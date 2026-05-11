from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from db.client2 import get_db
from db.models import NurseMonthlyLimit
from routers.auth import get_current_user_from_cookie
from routers.teams import router as teams_router
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
    app.include_router(teams_router)

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


def test_team_min_shift_reject_when_monthly_off_exact_blocks_capacity(api, db):
    # Team 1 members in seed_data: N001, N002, N005
    for nid in ["N001", "N002", "N005"]:
        db.add(
            NurseMonthlyLimit(
                nurse_id=nid,
                group_id="GRP001",
                year=2026,
                month=4,
                o_exact=30,
            )
        )
    db.commit()

    payload = {
        "teams": [
            {
                "team_id": 1,
                "team_name": "A팀",
                "min_shift": {"D": 1},
            }
        ],
        "delete_team_ids": [],
    }
    resp = api.put("/teams", json=payload)
    assert resp.status_code == 400, resp.text
    detail = resp.json().get("detail", "")
    assert "TEAM_MONTHLY_CAPACITY_LT_MIN_TOTAL" in detail


def test_team_min_shift_saveable_when_monthly_off_exact_allows_capacity(api, db):
    # Team 1 members: 3명, each off_exact=20 in 30-day month -> max work 10 each, total 30
    # day_min_sum=1 => monthly required 30, should pass at boundary
    for nid in ["N001", "N002", "N005"]:
        db.add(
            NurseMonthlyLimit(
                nurse_id=nid,
                group_id="GRP001",
                year=2026,
                month=4,
                o_exact=20,
            )
        )
    db.commit()

    payload = {
        "teams": [
            {
                "team_id": 1,
                "team_name": "A팀",
                "min_shift": {"D": 1},
            }
        ],
        "delete_team_ids": [],
    }
    resp = api.put("/teams", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    team1 = next((t for t in body if t.get("team_id") == 1), None)
    assert team1 is not None
    assert team1.get("min_shift", {}).get("D") == 1
