"""nurse_preceptee_period SSOT 리졸버/write-through 회귀 테스트.

핵심: 기간 종료 다음달은 as-of 조회에서 자연 제외(고주성 8월 follow 누수 버그의 구조적 방지).
"""
from datetime import date

import pytest

from db.models import NursePrecepteePeriod, Nurse
from services.preceptee_period import (
    end_date_to_valid_to,
    backfill_from_assignments,
    open_preceptee_period,
    close_preceptee_period,
    close_all_for_preceptor,
    resolve_preceptor_asof,
    resolve_preceptees_asof,
    resolve_preceptee_days_for_month,
)


def _open(db, nid, pid, vf, exp_end=None, today=None, nurse=None):
    return open_preceptee_period(
        db, nurse_id=nid, preceptor_id=pid, office_id="OFF001",
        valid_from=vf, valid_to=end_date_to_valid_to(exp_end),
        nurse=nurse, today=today,
    )


def test_month_boundary_ended_preceptee_excluded_next_month(db):
    """프리셉티 기간 6/1~7/14 → 7월엔 follow, 8월엔 키 자체가 없어야(=독립)."""
    _open(db, "P01", "MENTOR01", date(2026, 6, 1), exp_end=date(2026, 7, 14))
    db.flush()

    july = resolve_preceptee_days_for_month(db, ["P01"], 2026, 7)
    assert "P01" in july
    assert july["P01"]["preceptor_id"] == "MENTOR01"
    assert max(july["P01"]["days"]) == 13  # 7/14 = 0-based idx 13 (포함)

    august = resolve_preceptee_days_for_month(db, ["P01"], 2026, 8)
    assert "P01" not in august  # ← 버그였던 지점: 종료 다음달은 미포함


def test_open_ended_preceptee_follows_every_month(db):
    """무기한(valid_to=None) 프리셉티는 어느 달이든 전체월 follow."""
    _open(db, "P02", "MENTOR01", date(2026, 6, 12), exp_end=None)
    db.flush()
    aug = resolve_preceptee_days_for_month(db, ["P02"], 2026, 8)
    assert set(aug["P02"]["days"]) == set(range(31))  # 8월 31일 전체


def test_coexistence_ended_and_open(db):
    """종료된 프리셉티 + 같은 멘토의 무기한 프리셉티 공존 → 종료자만 제외(고주성/김태혁 시나리오)."""
    _open(db, "ENDED", "MENTOR01", date(2026, 6, 1), exp_end=date(2026, 7, 14))
    _open(db, "OPEN", "MENTOR01", date(2026, 6, 12), exp_end=None)
    db.flush()
    aug = resolve_preceptee_days_for_month(db, ["ENDED", "OPEN"], 2026, 8)
    assert "ENDED" not in aug
    assert "OPEN" in aug and set(aug["OPEN"]["days"]) == set(range(31))


def test_resolve_preceptor_asof_boundary(db):
    _open(db, "P03", "MENTOR01", date(2026, 6, 1), exp_end=date(2026, 7, 14))
    db.flush()
    assert resolve_preceptor_asof(db, ["P03"], date(2026, 7, 14))["P03"] == "MENTOR01"
    assert resolve_preceptor_asof(db, ["P03"], date(2026, 7, 15))["P03"] is None  # valid_to 배타
    assert resolve_preceptor_asof(db, ["P03"], date(2026, 5, 31))["P03"] is None


def test_reverse_lookup(db):
    _open(db, "A", "MENTOR01", date(2026, 6, 1), exp_end=None)
    _open(db, "B", "MENTOR01", date(2026, 6, 1), exp_end=None)
    db.flush()
    res = resolve_preceptees_asof(db, ["MENTOR01"], date(2026, 6, 15))
    assert set(res["MENTOR01"]) == {"A", "B"}


def test_one_to_one_open_closes_previous(db):
    """같은 간호사에 새 구간 open 시 기존 open 구간이 닫힌다(1:1)."""
    _open(db, "P04", "MENTOR01", date(2026, 6, 1), exp_end=None)
    db.flush()
    _open(db, "P04", "MENTOR02", date(2026, 8, 1), exp_end=None)
    db.flush()
    rows = db.query(NursePrecepteePeriod).filter(
        NursePrecepteePeriod.nurse_id == "P04"
    ).order_by(NursePrecepteePeriod.valid_from).all()
    assert len(rows) == 2
    assert rows[0].valid_to == date(2026, 8, 1)  # 첫 구간이 닫힘
    assert rows[1].valid_to is None
    # as-of: 7월=MENTOR01, 8월=MENTOR02
    assert resolve_preceptor_asof(db, ["P04"], date(2026, 7, 1))["P04"] == "MENTOR01"
    assert resolve_preceptor_asof(db, ["P04"], date(2026, 8, 2))["P04"] == "MENTOR02"


def test_open_projects_cache_when_active_today(db, seed_data):
    """open 시 오늘이 구간 내면 nurses.preceptor_id 투영."""
    n006 = db.query(Nurse).filter(Nurse.nurse_id == "N006").first()
    n006.preceptor_id = None
    _open(db, "N006", "N002", date(2026, 6, 1), exp_end=None,
          today=date(2026, 6, 30), nurse=n006)
    assert n006.preceptor_id == "N002"


def test_close_projects_cache_none(db, seed_data):
    n006 = db.query(Nurse).filter(Nurse.nurse_id == "N006").first()
    _open(db, "N006", "N002", date(2026, 6, 1), exp_end=None,
          today=date(2026, 6, 30), nurse=n006)
    db.flush()
    close_preceptee_period(db, nurse_id="N006", close_date=date(2026, 6, 30),
                           end_reason="cancelled", nurse=n006, today=date(2026, 6, 30))
    assert n006.preceptor_id is None
    rows = db.query(NursePrecepteePeriod).filter(NursePrecepteePeriod.nurse_id == "N006").all()
    assert rows[0].valid_to == date(2026, 6, 30) and rows[0].end_reason == "cancelled"


def test_backfill_from_assignments(db, seed_data):
    """기존 active 프리셉티 assignment + 캐시 → period 백필. 종료월 경계도 보존."""
    from datetime import date as _d
    from db.models import NurseAssignment
    # N006(한서연) preceptor=N002 — seed 에 캐시 있음. active 프리셉티 assignment 추가.
    db.add(NurseAssignment(
        nurse_id="N006", source_group_id="GRP001", office_id="OFF001",
        start_date=_d(2026, 6, 1), expected_end_date=_d(2026, 7, 14),
        reason="프리셉티", status="active",
    ))
    db.flush()
    res = backfill_from_assignments(db)
    db.flush()
    assert res["inserted"] == 1
    rows = db.query(NursePrecepteePeriod).filter(NursePrecepteePeriod.nurse_id == "N006").all()
    assert len(rows) == 1
    assert rows[0].preceptor_id == "N002"
    assert rows[0].valid_to == _d(2026, 7, 15)  # inclusive 7/14 → exclusive 7/15
    # 경계: 7월 follow, 8월 제외
    assert "N006" in resolve_preceptee_days_for_month(db, ["N006"], 2026, 7)
    assert "N006" not in resolve_preceptee_days_for_month(db, ["N006"], 2026, 8)
    # 멱등: 재실행 시 skip
    res2 = backfill_from_assignments(db)
    assert res2["inserted"] == 0 and res2["skipped"] == 1


def test_backfill_imports():
    """services 심볼 import 가능(엔진/마이그 양쪽에서 쓰임)."""
    from services.preceptee_period import backfill_from_assignments as _bf  # noqa: F401


def test_open_backdated_no_negative_interval(db):
    """기존 open(8/1 시작)보다 이른 6/1로 새 구간 open → 음수구간 안 생김(zero-width close)."""
    _open(db, "P10", "M1", date(2026, 8, 1), exp_end=None)
    db.flush()
    _open(db, "P10", "M2", date(2026, 6, 1), exp_end=None)
    db.flush()
    for r in db.query(NursePrecepteePeriod).filter(NursePrecepteePeriod.nurse_id == "P10").all():
        assert r.valid_to is None or r.valid_to >= r.valid_from  # 음수구간 없음


def test_close_before_valid_from_no_negative_interval(db):
    _open(db, "P11", "M1", date(2026, 6, 1), exp_end=None)
    db.flush()
    close_preceptee_period(db, nurse_id="P11", close_date=date(2026, 5, 1),
                           end_reason="cancelled")
    r = db.query(NursePrecepteePeriod).filter(NursePrecepteePeriod.nurse_id == "P11").first()
    assert r.valid_to == r.valid_from  # max(close, valid_from) → zero-width, 음수 아님


def test_db_filtered_unique_rejects_second_open(db):
    """동일 간호사 두번째 open 구간 직접 INSERT → filtered-unique 거부."""
    import sqlalchemy.exc
    db.add(NursePrecepteePeriod(nurse_id="P12", preceptor_id="M1", office_id="OFF001",
                                valid_from=date(2026, 6, 1), valid_to=None, source="t"))
    db.flush()
    db.add(NursePrecepteePeriod(nurse_id="P12", preceptor_id="M2", office_id="OFF001",
                                valid_from=date(2026, 7, 1), valid_to=None, source="t"))
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        db.flush()
    db.rollback()


def test_backfill_skips_duplicate_active_assignment(db, seed_data):
    """한 간호사에 active 프리셉티 assignment 2개(더티) → open 1개만, unique 위반 없음."""
    from db.models import NurseAssignment
    for _ in range(2):
        db.add(NurseAssignment(
            nurse_id="N006", source_group_id="GRP001", office_id="OFF001",
            start_date=date(2026, 6, 1), expected_end_date=None,
            reason="프리셉티", status="active",
        ))
    db.flush()
    res = backfill_from_assignments(db)
    db.flush()  # unique 위반 시 여기서 터짐 — 안 터져야 통과
    assert res["inserted"] == 1 and res["skipped"] == 1
    opens = db.query(NursePrecepteePeriod).filter(
        NursePrecepteePeriod.nurse_id == "N006",
        NursePrecepteePeriod.valid_to.is_(None)).count()
    assert opens == 1


def test_close_all_for_preceptor(db):
    _open(db, "X", "MENTOR09", date(2026, 6, 1), exp_end=None)
    _open(db, "Y", "MENTOR09", date(2026, 6, 1), exp_end=None)
    db.flush()
    closed = close_all_for_preceptor(db, preceptor_id="MENTOR09",
                                     close_date=date(2026, 7, 1), today=date(2026, 7, 1))
    assert len(closed) == 2
    aug = resolve_preceptee_days_for_month(db, ["X", "Y"], 2026, 8)
    assert aug == {}  # 둘 다 종료
