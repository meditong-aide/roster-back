"""EF-F1 (D48) — validate_roster: 설정의 max_conseq_work 가 NULL 이면 엔진 기본(5)으로 검증한다.

옛 동작: None 이 엔진으로 넘어가 TypeError → except 가 삼켜 '위반사항 계산 중 오류' 응답.
새 동작: 5 로 검증 → 6연속 근무가 '최대 연속 근무일 초과' 로 잡힌다. 값이 있는 설정은 그 값 그대로.
"""

from __future__ import annotations

from types import SimpleNamespace

from db.models import Nurse, RosterConfig, ShiftManage
from routers.roster import validate_roster


def _adm():
    return SimpleNamespace(nurse_id="N001", group_id="GRP001", office_id="OFF001", is_master_admin=True)


def _call(db, work_days):
    nurse = db.query(Nurse).filter(Nurse.group_id == "GRP001").order_by(Nurse.nurse_id).first()
    sched = ["D"] * work_days + ["O"] * (30 - work_days)
    data = {
        "year": 2026, "month": 4, "schedule_id": "SCH202604V1",
        "roster": [{"nurse_id": nurse.nurse_id, "schedule": sched}],
    }
    return validate_roster(roster_data=data, group_id="GRP001", current_user=_adm(), db=db)


def _set(db, value):
    for sm in db.query(ShiftManage).filter_by(group_id="GRP001", nurse_class="RN").all():
        sm.main_code = sm.main_code.split("_")[0]  # 'D_GRP001' → 'D' (엔진 근무코드)
    db.query(RosterConfig).filter_by(config_id=1).update({"max_conseq_work": value})
    db.flush()
    db.expire_all()


def test_null_max_conseq_work_uses_engine_default(db, seed_data):
    _set(db, None)
    res = _call(db, 6)
    assert not any("오류가 발생" in m for m in res["violations"]), res["violations"]
    assert any(v["type"] == "consecutive_work" for v in res["detailed_violations"])  # 5 기준: 6연속 위반


def test_null_max_conseq_work_five_days_ok(db, seed_data):
    _set(db, None)
    res = _call(db, 5)
    assert not any("오류가 발생" in m for m in res["violations"]), res["violations"]
    assert not any(v["type"] == "consecutive_work" for v in res["detailed_violations"])


def test_explicit_value_kept(db, seed_data):
    # 검증기는 DB 값을 신뢰한다(하드락 판정은 생성 쪽). 명시 4 → 5연속이 위반, 명시 값이 기본 5 로 덮이지 않음.
    _set(db, 4)
    res = _call(db, 5)
    assert any(v["type"] == "consecutive_work" for v in res["detailed_violations"])


def test_explicit_zero_is_not_replaced_by_default(db, seed_data):
    # 0 은 NULL 이 아니다 -> 기본 5 로 치환하지 않는다(레지스트리 null_behavior '0->0 그대로').
    # 5 기준이면 위반이 아닐 3연속 근무도 0 기준이라 위반으로 잡힌다.
    _set(db, 0)
    res = _call(db, 3)
    assert any(v["type"] == "consecutive_work" for v in res["detailed_violations"])
