"""P2 — 프리셋 목록(/config/versions) + 로드(/config/version/{v}) 엔드포인트.

검증:
  - 목록: version 부여된 프리셋만(legacy NULL 제외), version desc, name/summary/최근적용 포함
  - 로드: version 정수 → 해당 프리셋, 없는 version → 404, schedule_id → 그 근무표의 config
"""
import asyncio
from types import SimpleNamespace

import pytest

from db.models import RosterConfig, Schedule
from schemas.roster_schema import RosterConfigCreate
from services.roster_service import save_roster_config_service
from routers.roster import get_config_versions, get_config_by_version


def _make_config(**overrides) -> RosterConfigCreate:
    base = dict(
        min_exp_per_shift=1, req_exp_nurses=1, two_offs_per_week=True,
        max_nig_per_month=8, three_seq_nig=True, two_offs_after_three_nig=True,
        two_offs_after_two_nig=False, banned_day_after_eve=True, max_conseq_work=5,
        off_days=9, shift_priority=0.8, weekend_shift_ratio=1.0, patient_amount=0,
        even_nights=True, sequential_offs=True, nod_noe=False, preceptor_gauge=5,
    )
    base.update(overrides)
    return RosterConfigCreate(**base)


@pytest.fixture
def adm_user():
    return SimpleNamespace(
        nurse_id="N001", group_id="GRP001", office_id="OFF001", is_master_admin=True
    )


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _save(db, adm_user, **ov):
    return save_roster_config_service(
        _make_config(**ov), adm_user, db, override_group_id="GRP001"
    )


def test_versions_lists_presets_only_desc(db, seed_data, adm_user):
    # seed legacy(version NULL) 존재 → 목록에서 제외돼야 함
    r0 = _save(db, adm_user, config_name="기본 설정", config_memo="메모")
    r1 = _save(db, adm_user, config_name="주말 균형")

    out = _run(get_config_versions(group_id="GRP001", current_user=adm_user, db=db))
    assert [p["version"] for p in out] == [1, 0]  # version desc
    assert all(p["config_name"] is not None for p in out)
    top = out[0]
    assert top["config_name"] == "주말 균형"
    assert "summary" in top and "off_days" in top["summary"]
    assert top["last_applied"] is None  # 아직 적용된 근무표 없음
    # legacy NULL 은 빠졌으니 정확히 2개
    assert len(out) == 2


def test_versions_includes_last_applied(db, seed_data, adm_user):
    r0 = _save(db, adm_user, config_name="적용대상")
    # 이 config 로 만든 근무표 1건
    sched = Schedule(
        schedule_id="SCH000000001", office_id="OFF001", group_id="GRP001",
        year=2026, month=7, version=1, config_id=r0["config_id"],
        created_by="acc_N001", status="draft",
    )
    db.add(sched)
    db.flush()

    out = _run(get_config_versions(group_id="GRP001", current_user=adm_user, db=db))
    applied = next(p for p in out if p["config_id"] == r0["config_id"])["last_applied"]
    assert applied == {"year": 2026, "month": 7, "schedule_version": 1}


def test_load_by_version(db, seed_data, adm_user):
    r0 = _save(db, adm_user, config_name="버전0", off_days=9)
    r1 = _save(db, adm_user, config_name="버전1", off_days=11)

    loaded = _run(get_config_by_version(
        config_version="1", group_id="GRP001", current_user=adm_user, db=db
    ))
    assert loaded["version"] == 1
    assert loaded["config_name"] == "버전1"
    assert loaded["off_days"] == 11
    assert loaded["config_id"] == r1["config_id"]


def test_load_missing_version_404(db, seed_data, adm_user):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        _run(get_config_by_version(
            config_version="999", group_id="GRP001", current_user=adm_user, db=db
        ))
    assert ei.value.status_code == 404


def test_load_by_schedule_id(db, seed_data, adm_user):
    r0 = _save(db, adm_user, config_name="스케줄설정", off_days=10)
    sched = Schedule(
        schedule_id="SCH000000002", office_id="OFF001", group_id="GRP001",
        year=2026, month=8, version=1, config_id=r0["config_id"],
        created_by="acc_N001", status="draft",
    )
    db.add(sched)
    db.flush()

    loaded = _run(get_config_by_version(
        config_version="ignored", schedule_id="SCH000000002",
        group_id="GRP001", current_user=adm_user, db=db
    ))
    assert loaded["config_id"] == r0["config_id"]
    assert loaded["off_days"] == 10
