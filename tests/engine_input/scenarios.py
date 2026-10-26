"""골든 하네스 합성 시나리오 — 실명·PII 없음(N01 등). 설계서 §7.1 '시나리오'.

여기는 캡처 장치 확인용 **최소 시나리오**(test_capture)와 공용 호출자(`make_head_nurse`)만 둔다.
골든 시나리오 S1~S9·변형은 `scenario_defs.py`(재료는 `ward.py`)에 있다.

최소 시나리오(min-basic)
  - 병원 O1 · 병동 G1 · 수간호사 HN01(role HN — 엔진 대상 밖) · RN 8명 N01~N08(grade 1, 팀 없음)
  - 근무코드 D/E/N/OFF · 요구 인원 D2/E2/N1(shift_manage RN) · 일자별 요구(daily_shift) 없음
  - 설정 1행(config_id 1, 운영 행처럼 칸을 채움) + raw 컬럼 두 개(`n2n_min_gap`=3 · `fixed_holiday_off_yn`=0)
    ★ `shift_priority` 를 비우면 엔진이 `max(0.05, None)`(cp_sat_basic.py:584)에서 TypeError 로
      멈춘다 — NULL 칸 동작은 골든 S2·S3 가 따로 본다
    → `_n2n_min_gap_setting`(:1252)이 raw 를 실제로 읽어 L1 config 에 3 이 실리는지로 경유를 확인한다
  - 등급 설정 1행(raw `CAST … NVARCHAR(MAX)` 경로 :819) · 2026-11 원티드 열림 · 원티드 신청 없음
  - 대상 2026-11(30일) · 오늘(고정) 2026-10-01
  - 모든 시각 칸은 명시 값이다(모델 기본값 func.now() 는 하네스에서도 고정되지만, 시드는 명시).
"""

from __future__ import annotations

from datetime import date, datetime, time

from sqlalchemy import text

from .harness import Scenario

OFFICE_ID = "O1"
GROUP_ID = "G1"
HEAD_NURSE_ID = "HN01"
NURSE_IDS = tuple(f"N{i:02d}" for i in range(1, 9))
TARGET_YEAR, TARGET_MONTH = 2026, 11
CONFIG_ID = 1
N2N_MIN_GAP = 3

#: 시드 행의 시각 칸(명시) — 실행마다 같아야 digest 가 같다.
SEED_TS = datetime(2026, 9, 1, 8, 0, 0)

RAW_COLUMN_DDL = (
    "ALTER TABLE roster_config ADD COLUMN n2n_min_gap INTEGER",
    "ALTER TABLE roster_config ADD COLUMN fixed_holiday_off_yn INTEGER",
)

#: (shift_id, id, name, shift_gb, type, default_shift, sequence, start, end, show_in_preference)
_SHIFTS = (
    ("D", 1, "데이", "D", "근무", "D", 1, time(7, 0), time(15, 0), True),
    ("E", 2, "이브닝", "E", "근무", "E", 2, time(15, 0), time(23, 0), True),
    ("N", 3, "나이트", "N", "근무", "N", 3, time(23, 0), time(7, 0), True),
    ("OFF", 4, "오프", None, "오프", "O", 4, None, None, False),
)
#: (slot, main_code, codes, manpower)
_SHIFT_MANAGE = ((1, "D", ["D"], 2), (2, "E", ["E"], 2), (3, "N", ["N"], 1))


def _stamp() -> dict:
    """시드 행 공통 시각 칸."""
    return {"created_at": SEED_TS, "updated_at": SEED_TS}


def _seed_org(db) -> None:
    """병원·병동·수간호사."""
    from db.models import Group, Nurse, Office

    db.add(Office(office_id=OFFICE_ID, office_name="합성병원"))
    db.add(Group(group_id=GROUP_ID, office_id=OFFICE_ID, group_name="합성병동", hn_id=[]))
    db.add(Nurse(nurse_id=HEAD_NURSE_ID, account_id=f"acc_{HEAD_NURSE_ID}", group_id=GROUP_ID,
                 office_id=OFFICE_ID, name=HEAD_NURSE_ID, role="HN", is_head_nurse=True,
                 experience=20, grade=3, sequence=0, active=1, allowed_shifts=[],
                 work_shifts=["D"], joining_date=datetime(2010, 3, 1), **_stamp()))


def _seed_nurses(db) -> None:
    """RN 8명 — grade 1, 경력 내림차순, 팀 없음."""
    from db.models import Nurse

    for i, nid in enumerate(NURSE_IDS, start=1):
        db.add(Nurse(nurse_id=nid, account_id=f"acc_{nid}", group_id=GROUP_ID, office_id=OFFICE_ID,
                     name=nid, role="RN", is_head_nurse=False, experience=12 - i, grade=1,
                     sequence=i, active=1, allowed_shifts=[], work_shifts=["D", "E", "N"],
                     joining_date=datetime(2024, 3, 1), **_stamp()))


def _seed_shifts(db) -> None:
    """근무코드 D/E/N/OFF 와 RN 요구 인원(shift_manage)."""
    from db.models import Shift, ShiftManage

    for sid, pk, name, gb, kind, default, seq, start, end, show in _SHIFTS:
        db.add(Shift(shift_id=sid, id=pk, office_id=OFFICE_ID, group_id=GROUP_ID, name=name,
                     color="#000000", shift_gb=gb, type=kind, default_shift=default, sequence=seq,
                     start_time=start, end_time=end, allday=0, auto_schedule=1, is_weekly_off=0,
                     show_in_preference=show))
    for slot, main, codes, manpower in _SHIFT_MANAGE:
        db.add(ShiftManage(office_id=OFFICE_ID, group_id=GROUP_ID, nurse_class="RN",
                           shift_slot=slot, main_code=main, codes=codes, manpower=manpower,
                           **_stamp()))


def _seed_config(db) -> None:
    """생성 설정 1행 + raw 컬럼 값 + 등급 설정 1행."""
    from db.models import RosterConfig, RosterGradeConfig

    db.add(RosterConfig(
        config_id=CONFIG_ID, office_id=OFFICE_ID, group_id=GROUP_ID, day_req=2, eve_req=2,
        nig_req=1, senior_min_years=3, senior_nurses_per_shift=1, prefer_two_offs_per_7days=True,
        max_nig_per_month=15, allow_three_consecutive_nights=True, two_offs_after_three_nig=False,
        two_offs_after_two_nig=False, ban_shift_transitions=True, max_conseq_work=5, off_days=8,
        soft_max_conseq_off=3, shift_priority=0.7, avoid_isolated_off_and_work=True, avoid_nod_noe_eod=True,
        weekly_off_group=False, ban_single_night=False, use_mid=False, preceptee_follows_preceptor=False,
        apply_confirmed_wanted=False, staffing_first=False, version=1, config_name="합성 설정",
        **_stamp()))
    db.add(RosterGradeConfig(
        config_id=1, office_id=OFFICE_ID, group_id=GROUP_ID,
        constraints_json={"1": {"D": 1, "E": 1, "N": 1}}, constraints_max_json={},
        use_dynamic_scaling=1, allow_soft_fallback=0, **_stamp()))
    db.flush()
    db.execute(text("UPDATE roster_config SET n2n_min_gap = :gap, fixed_holiday_off_yn = 0 "
                    "WHERE config_id = :cid"), {"gap": N2N_MIN_GAP, "cid": CONFIG_ID})


def _seed_wanted(db) -> None:
    """대상 달 원티드 열림(신청 0건)."""
    from db.models import Wanted

    db.add(Wanted(group_id=GROUP_ID, year=TARGET_YEAR, month=TARGET_MONTH, status="requested",
                  exp_date=datetime(2026, 10, 20, 23, 59), created_at=SEED_TS))


def seed_minimal(db) -> None:
    """최소 시나리오 시드."""
    _seed_org(db)
    _seed_nurses(db)
    _seed_shifts(db)
    _seed_config(db)
    _seed_wanted(db)
    db.flush()


def make_request():
    """2026-11 생성 요청(config_id 지정)."""
    from schemas.roster_schema import RosterRequest

    return RosterRequest(year=TARGET_YEAR, month=TARGET_MONTH, config_id=CONFIG_ID)


def make_head_nurse():
    """호출자 — 수간호사 HN01(토큰 값은 DB 와 같게)."""
    from schemas.auth_schema import User

    return User(nurse_id=HEAD_NURSE_ID, account_id=f"acc_{HEAD_NURSE_ID}", office_id=OFFICE_ID,
                group_id=GROUP_ID, is_head_nurse=True, is_master_admin=False, name=HEAD_NURSE_ID,
                EmpSeqNo="", EmpAuthGbn="", mb_part="", office_name="합성병원", mb_part_name="",
                gw_useYN="Y", qpis_useYN="Y", official_title_name=None,
                is_nurse_registered=True, hn_auth=None, original_group_id=GROUP_ID)


MINIMAL = Scenario(
    name="min-basic",
    seed=seed_minimal,
    make_request=make_request,
    make_user=make_head_nurse,
    ddl=RAW_COLUMN_DDL,
)

#: 대상 달 첫날 — 테스트가 날짜 계산에 쓴다.
TARGET_MONTH_START = date(TARGET_YEAR, TARGET_MONTH, 1)
