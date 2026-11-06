"""엔진 커버리지 소스 불변식 — day_req 는 무시, DailyShift(일자별)가 권위.

필요인원 변경 스킬이 무엇을 건드려야 하는지의 근거. day_req 를 바꿔도 엔진
요구치는 불변, DailyShift 를 바꾸면 그날 요구치가 바뀐다(base 는 없는 날 fallback).
"""
from types import SimpleNamespace
from db.models import DailyShift, RosterConfig, ShiftManage
from services.roster_create_service import _build_shift_manage_and_requirements


def _seed_manpower(db):
    # seed 의 main_code='D_GRP001' 는 엔진기준 'D' 로 정규화 안 되므로 D/E/N 로 세팅.
    for sm in db.query(ShiftManage).filter(ShiftManage.group_id == "GRP001").all():
        mc = (sm.main_code or "").split("_")[0].upper()
        if mc in ("D", "E", "N"):
            sm.main_code = mc
            sm.manpower = {"D": 3, "E": 2, "N": 1}[mc]
    db.flush()


def _build(db):
    cu = SimpleNamespace(office_id="OFF001", group_id="GRP001")
    cfg = SimpleNamespace(use_mid=False)
    req = SimpleNamespace(year=2026, month=4)
    _, base, byday, _ = _build_shift_manage_and_requirements(db, cu, cfg, req)
    return base, byday


def test_day_req_does_not_affect_engine(db, seed_data):
    _seed_manpower(db)
    base0, _ = _build(db)
    rc = db.query(RosterConfig).filter(RosterConfig.group_id == "GRP001").first()
    rc.day_req = 99; db.flush()
    base1, _ = _build(db)
    assert base1 == base0 == {"D": 3, "E": 2, "N": 1}  # day_req 무영향


def test_dailyshift_is_perday_authority(db, seed_data):
    _seed_manpower(db)
    base0, _ = _build(db)
    db.add(DailyShift(office_id="OFF001", group_id="GRP001", year=2026, month=4,
                      day=7, d_count=8, e_count=7, n_count=3)); db.flush()
    _, byday = _build(db)
    assert byday[6] == {"D": 8, "E": 7, "N": 3}   # 4/7 = DailyShift 반영
    assert byday[5] == base0                       # 4/6 = base fallback
