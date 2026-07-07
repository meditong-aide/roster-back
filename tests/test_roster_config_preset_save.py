"""P1 — roster_config 프리셋 저장(upsert) + version 할당 테스트.

검증:
  - 신규 저장(config_id 없음) → 그룹별 version 0, 1, 2 ... 증가
  - config_name/config_memo 저장
  - config_id 지정 저장 → in-place 수정(같은 config_id·version 유지, 값 갱신)
  - legacy(version NULL) row 와 공존
"""
from types import SimpleNamespace

import pytest

from db.models import RosterConfig
from schemas.roster_schema import RosterConfigCreate
from services.roster_service import save_roster_config_service, _next_config_version


def _make_config(**overrides) -> RosterConfigCreate:
    """필수 필드를 채운 RosterConfigCreate. overrides 로 일부 덮어쓰기."""
    base = dict(
        min_exp_per_shift=1,
        req_exp_nurses=1,
        two_offs_per_week=True,
        max_nig_per_month=8,
        three_seq_nig=True,
        two_offs_after_three_nig=True,
        two_offs_after_two_nig=False,
        banned_day_after_eve=True,
        max_conseq_work=5,
        off_days=9,
        shift_priority=0.8,
        weekend_shift_ratio=1.0,
        patient_amount=0,
        even_nights=True,
        sequential_offs=True,
        nod_noe=False,
        preceptor_gauge=5,
    )
    base.update(overrides)
    return RosterConfigCreate(**base)


@pytest.fixture
def adm_user():
    return SimpleNamespace(
        nurse_id="N001", group_id="GRP001", office_id="OFF001", is_master_admin=True
    )


def _versions(db, office="OFF001", group="GRP001"):
    return sorted(
        v for (v,) in db.query(RosterConfig.version)
        .filter(RosterConfig.office_id == office, RosterConfig.group_id == group)
        .all()
        if v is not None
    )


def test_new_preset_gets_version_zero_then_increments(db, seed_data, adm_user):
    # seed_data 가 만든 legacy config 는 version=NULL
    legacy = _versions(db)
    assert legacy == [], "사전 조건: 프리셋(version) 없음"

    r0 = save_roster_config_service(
        _make_config(config_name="기본 설정", config_memo="메모1"),
        adm_user, db, override_group_id="GRP001",
    )
    assert r0["version"] == 0
    assert r0["config_name"] == "기본 설정"

    r1 = save_roster_config_service(
        _make_config(config_name="주말 균형"),
        adm_user, db, override_group_id="GRP001",
    )
    assert r1["version"] == 1
    assert r1["config_id"] != r0["config_id"]

    assert _versions(db) == [0, 1]


def test_update_by_config_id_keeps_version_and_id(db, seed_data, adm_user):
    r0 = save_roster_config_service(
        _make_config(config_name="원본", off_days=9),
        adm_user, db, override_group_id="GRP001",
    )
    cid, ver = r0["config_id"], r0["version"]

    r_upd = save_roster_config_service(
        _make_config(config_id=cid, config_name="수정본", off_days=10),
        adm_user, db, override_group_id="GRP001",
    )
    # in-place: 같은 config_id·version
    assert r_upd["config_id"] == cid
    assert r_upd["version"] == ver
    assert r_upd["config_name"] == "수정본"

    row = db.query(RosterConfig).filter(RosterConfig.config_id == cid).first()
    assert row.off_days == 10
    # 새 row 가 생기지 않았는지 (version 목록 길이 1)
    assert _versions(db) == [ver]


def test_update_missing_config_id_raises(db, seed_data, adm_user):
    with pytest.raises(Exception):
        save_roster_config_service(
            _make_config(config_id=999999, config_name="없음"),
            adm_user, db, override_group_id="GRP001",
        )


def test_next_config_version_helper(db, seed_data, adm_user):
    assert _next_config_version(db, "OFF001", "GRP001") == 0
    save_roster_config_service(
        _make_config(config_name="a"), adm_user, db, override_group_id="GRP001"
    )
    assert _next_config_version(db, "OFF001", "GRP001") == 1
