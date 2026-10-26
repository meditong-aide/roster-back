"""EI-15 15c — GET 무행 시드(`GET /config/version/latest` 가 설정이 없을 때 만드는 '기본 설정' 행) expected-diff 시험(D5).

바뀐 것(새 행만): max_conseq_work 6→5 · two_offs_after_three_nig False→True · nod_noe False→True ·
preceptee_on False→True · off_days 11→8. 값은 레지스트리(엔진) 기본이다.
안 바뀐 것: 이미 있는 행은 읽기만 한다(시드가 돌지 않는다).
"""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

from db.models import RosterConfig
from routers.roster import get_config_by_version
from schemas.roster_schema import RosterConfigCreate
from services.roster_service import save_roster_config_service


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _save(db, user, **ov):
    base = dict(
        min_exp_per_shift=1, req_exp_nurses=1, two_offs_per_week=True,
        max_nig_per_month=8, three_seq_nig=True, two_offs_after_three_nig=True,
        two_offs_after_two_nig=False, banned_day_after_eve=True, max_conseq_work=5,
        off_days=9, shift_priority=0.8, weekend_shift_ratio=1.0, patient_amount=0,
        even_nights=True, sequential_offs=True, nod_noe=False, preceptor_gauge=5,
    )
    base.update(ov)
    return save_roster_config_service(RosterConfigCreate(**base), user, db, override_group_id="GRP001")


def _adm():
    return SimpleNamespace(nurse_id="N001", group_id="GRP001", office_id="OFF001", is_master_admin=True)


def _call(db):
    res = get_config_by_version(config_version="latest", group_id="GRP001", current_user=_adm(), db=db)
    return _run(res) if inspect.isawaitable(res) else res


def test_new_seed_row_uses_registry_defaults(db, seed_data):
    db.query(RosterConfig).filter_by(group_id="GRP001").delete()  # 설정 없는 병동으로 만든다
    db.flush()
    row = _call(db)
    # (옛 값 → 새 값)
    assert row["max_conseq_work"] == 5           # 6 → 5
    assert row["two_offs_after_three_nig"] is True   # False → True
    assert row["nod_noe"] is True                # False → True
    assert row["preceptee_on"] is True           # False → True
    assert row["off_days"] == 8                  # 11 → 8
    # 그대로인 하드락 칸
    assert row["max_nig_per_month"] == 15
    assert row["banned_day_after_eve"] is True
    assert row["three_seq_nig"] is True


def test_existing_row_is_not_reseeded(db, seed_data):
    db.query(RosterConfig).filter_by(group_id="GRP001").delete()
    db.flush()
    _save(db, _adm(), config_name="기존", max_conseq_work=6, off_days=11)
    row = _call(db)
    assert row["config_name"] == "기존"
    assert row["max_conseq_work"] == 6
    assert row["off_days"] == 11
