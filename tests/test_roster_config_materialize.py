"""P3/P4 — 생성 materialize + 라이브 동기화(apply) + save 순수화.

검증:
  - payload != baseline → '새로운 설정n' 신규 row(version 증가)
  - payload == baseline → 재사용(새 row 없음)
  - 자동 이름 '새로운 설정1' → '새로운 설정2'
  - apply: weekly_off_group → WeeklyOffSetting.activate / Nurse.weekly_off_enabled 동기화
  - save 는 순수 — WeeklyOffSetting 을 건드리지 않음
"""
from types import SimpleNamespace

import pytest

from db.models import RosterConfig, WeeklyOffSetting, Nurse
from schemas.roster_schema import RosterConfigCreate
from services.roster_service import (
    save_roster_config_service,
    materialize_generation_config,
)


def _make_config(**ov) -> RosterConfigCreate:
    base = dict(
        min_exp_per_shift=1, req_exp_nurses=1, two_offs_per_week=True,
        max_nig_per_month=8, three_seq_nig=True, two_offs_after_three_nig=True,
        two_offs_after_two_nig=False, banned_day_after_eve=True, max_conseq_work=5,
        off_days=9, shift_priority=0.8, weekend_shift_ratio=1.0, patient_amount=0,
        even_nights=True, sequential_offs=True, nod_noe=False, preceptor_gauge=5,
    )
    base.update(ov)
    return RosterConfigCreate(**base)


@pytest.fixture
def adm_user():
    return SimpleNamespace(
        nurse_id="N001", group_id="GRP001", office_id="OFF001", is_master_admin=True
    )


def _count_presets(db):
    return (
        db.query(RosterConfig)
        .filter(
            RosterConfig.office_id == "OFF001",
            RosterConfig.group_id == "GRP001",
            RosterConfig.version.isnot(None),
        )
        .count()
    )


def test_materialize_creates_auto_named_preset(db, seed_data, adm_user):
    assert _count_presets(db) == 0
    resolved = materialize_generation_config(
        db, _make_config(off_days=10), adm_user, override_group_id="GRP001"
    )
    assert resolved.version == 0
    assert resolved.config_name == "새로운 설정1"
    assert _count_presets(db) == 1

    # 두 번째 (다른 값) → 새로운 설정2, version 1
    resolved2 = materialize_generation_config(
        db, _make_config(off_days=11), adm_user, override_group_id="GRP001"
    )
    assert resolved2.config_name == "새로운 설정2"
    assert resolved2.version == 1
    assert _count_presets(db) == 2


def test_materialize_reuses_unchanged_baseline(db, seed_data, adm_user):
    saved = save_roster_config_service(
        _make_config(config_name="기준", off_days=9),
        adm_user, db, override_group_id="GRP001",
    )
    cid = saved["config_id"]
    before = _count_presets(db)

    # baseline 과 동일한 값 + config_id 지정 → 재사용(새 row 없음)
    resolved = materialize_generation_config(
        db, _make_config(config_id=cid, config_name="기준", off_days=9),
        adm_user, override_group_id="GRP001",
    )
    assert resolved.config_id == cid
    assert _count_presets(db) == before  # 증가 없음


def test_materialize_changed_baseline_creates_new(db, seed_data, adm_user):
    saved = save_roster_config_service(
        _make_config(config_name="기준", off_days=9),
        adm_user, db, override_group_id="GRP001",
    )
    cid = saved["config_id"]

    # off_days 변경 → baseline 과 다름 → 신규 row
    resolved = materialize_generation_config(
        db, _make_config(config_id=cid, off_days=12),
        adm_user, override_group_id="GRP001",
    )
    assert resolved.config_id != cid
    assert resolved.config_name == "새로운 설정1"
    assert resolved.off_days == 12


def test_apply_syncs_weekly_off_live_tables(db, seed_data, adm_user):
    # WeeklyOffSetting 시드 (비활성)
    wos = WeeklyOffSetting(office_id="OFF001", group_id="GRP001", activate=0)
    db.add(wos)
    db.flush()

    materialize_generation_config(
        db, _make_config(weekly_off_group=True), adm_user, override_group_id="GRP001"
    )

    refreshed = (
        db.query(WeeklyOffSetting)
        .filter(WeeklyOffSetting.office_id == "OFF001",
                WeeklyOffSetting.group_id == "GRP001")
        .first()
    )
    assert refreshed.activate == 1
    nurses = db.query(Nurse).filter(Nurse.group_id == "GRP001").all()
    assert all(int(n.weekly_off_enabled or 0) == 1 for n in nurses)


def test_save_is_pure_no_weekly_off_sync(db, seed_data, adm_user):
    wos = WeeklyOffSetting(office_id="OFF001", group_id="GRP001", activate=0)
    db.add(wos)
    db.flush()

    # 저장(북마크)만 — 라이브 동기화 안 함
    save_roster_config_service(
        _make_config(config_name="순수저장", weekly_off_group=True),
        adm_user, db, override_group_id="GRP001",
    )
    refreshed = (
        db.query(WeeklyOffSetting)
        .filter(WeeklyOffSetting.office_id == "OFF001",
                WeeklyOffSetting.group_id == "GRP001")
        .first()
    )
    assert refreshed.activate == 0  # 저장은 건드리지 않음
