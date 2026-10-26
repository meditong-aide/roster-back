"""합성 병동 빌더 — 골든 시나리오(S1~S9·변형)가 공유하는 시드 재료(설계서 §7.1 '시나리오').

규칙
  - 실명·PII 없음. 사람은 N01·X01·HN01 같은 합성 식별자, 이름 = 식별자.
  - 모든 시각 칸은 명시 값(SEED_TS) — 실행마다 digest 가 같아야 한다.
  - 행은 ORM 모델 그대로 넣는다(앱이 읽는 모양과 같게). 모델 밖 raw 컬럼은 `Ward.raw` 로
    UPDATE 한다(시나리오 DDL 이 컬럼을 만든다).
  - 시나리오는 `base_ward()` 를 고쳐 쓴다. 병동 하나 = 그룹 G1(대상) · 필요하면 G2(상대 병동).
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, time

from sqlalchemy import text

OFFICE_ID = "O1"
GROUP_ID = "G1"
OTHER_GROUP_ID = "G2"
HEAD_NURSE_ID = "HN01"
CONFIG_ID = 1
TARGET_YEAR, TARGET_MONTH = 2026, 11

#: 시드 행의 시각 칸(명시).
SEED_TS = datetime(2026, 9, 1, 8, 0, 0)

#: raw 컬럼(모델 밖) — `_holiday_off_enabled`·`_n2n_min_gap_setting` 이 INFORMATION_SCHEMA 확인 뒤 읽는다.
RAW_COLUMN_DDL = (
    "ALTER TABLE roster_config ADD COLUMN n2n_min_gap INTEGER",
    "ALTER TABLE roster_config ADD COLUMN fixed_holiday_off_yn INTEGER",
)
#: 운영에 있는 기록 테이블(차단·미분류 실패 경로가 raw SQL 로 쓴다). 모델 PK 가 BIGINT 라
#: SQLite create_all 로는 rowid 자동 채번이 안 돼 SQLite 형태로 직접 만든다(바깥 트랜잭션과 함께 롤백).
SUPPORT_DDL = (
    "CREATE TABLE roster_config_attempt (attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " source_config_id INTEGER, office_id VARCHAR(50), group_id VARCHAR(50) NOT NULL,"
    " job_id VARCHAR(100), year INTEGER, month INTEGER, status VARCHAR(20) NOT NULL,"
    " intervened BOOLEAN, snapshot TEXT, created_at DATETIME)",
)

#: (shift_id, id, name, shift_gb, type, default_shift, sequence, start, end, show_in_preference)
BASE_SHIFTS = (
    ("D", 1, "데이", "D", "근무", "D", 1, time(7, 0), time(15, 0), True),
    ("E", 2, "이브닝", "E", "근무", "E", 2, time(15, 0), time(23, 0), True),
    ("N", 3, "나이트", "N", "근무", "N", 3, time(23, 0), time(7, 0), True),
    ("OFF", 4, "오프", None, "오프", "O", 4, None, None, False),
)
#: (slot, main_code, codes, manpower)
BASE_SHIFT_MANAGE = ((1, "D", ["D"], 2), (2, "E", ["E"], 2), (3, "N", ["N"], 2))


def stamp() -> dict:
    """시드 행 공통 시각 칸."""
    return {"created_at": SEED_TS, "updated_at": SEED_TS}


# ── 행 재료 ─────────────────────────────────────────────────────────────


def nurse_row(nid: str, seq: int, **over) -> dict:
    """RN 한 명의 Nurse 칸(기본값 + 덮어쓰기). 경력은 순번이 앞설수록 많다."""
    row = {
        "nurse_id": nid, "account_id": f"acc_{nid}", "group_id": GROUP_ID, "office_id": OFFICE_ID,
        "name": nid, "role": "RN", "is_head_nurse": False, "experience": 20 - seq, "grade": 1,
        "sequence": seq, "active": 1, "allowed_shifts": [], "work_shifts": ["D", "E", "N"],
        "joining_date": datetime(2024, 3, 1), "weekly_off_enabled": 0,
    }
    row.update(over)
    return row


def nurse_rows(count: int, *, start: int = 1, prefix: str = "N") -> list[dict]:
    """N01… 순번대로 count 명."""
    return [nurse_row(f"{prefix}{i:02d}", i) for i in range(start, start + count)]


def shift_row(sid: str, pk: int, name: str, gb, kind: str, default, seq: int, start=None, end=None,
              show: bool = True, **over) -> dict:
    """근무코드 한 행."""
    row = {
        "shift_id": sid, "id": pk, "office_id": OFFICE_ID, "group_id": GROUP_ID, "name": name,
        "color": "#000000", "shift_gb": gb, "type": kind, "default_shift": default, "sequence": seq,
        "start_time": start, "end_time": end, "allday": 0, "auto_schedule": 1, "is_weekly_off": 0,
        "show_in_preference": show,
    }
    row.update(over)
    return row


def shift_manage_row(slot: int, main: str, codes: list[str], manpower: int) -> dict:
    """요구 인원 슬롯 한 행(RN)."""
    return {"office_id": OFFICE_ID, "group_id": GROUP_ID, "nurse_class": "RN", "shift_slot": slot,
            "main_code": main, "codes": list(codes), "manpower": manpower, **stamp()}


def config_row(**over) -> dict:
    """roster_config 한 행 — 운영 행처럼 칸을 채운다. None 을 넘기면 그 칸은 NULL."""
    row = {
        "config_id": CONFIG_ID, "office_id": OFFICE_ID, "group_id": GROUP_ID, "day_req": 2,
        "eve_req": 2, "nig_req": 2, "senior_min_years": 3, "senior_nurses_per_shift": 1,
        "prefer_two_offs_per_7days": True, "max_nig_per_month": 15, "allow_three_consecutive_nights": True,
        "two_offs_after_three_nig": True, "two_offs_after_two_nig": False,
        "ban_shift_transitions": True, "max_conseq_work": 5, "off_days": 8, "soft_max_conseq_off": 3,
        "shift_priority": 0.7, "avoid_isolated_off_and_work": True, "avoid_nod_noe_eod": True, "weekly_off_group": False,
        "ban_single_night": False, "use_mid": False, "preceptee_follows_preceptor": False,
        "apply_confirmed_wanted": False, "staffing_first": False, "version": 1, "config_name": "합성 설정",
        **stamp(),
    }
    row.update(over)
    return row


def grade_row(constraints: dict, maximums: dict | None = None, **over) -> dict:
    """roster_grade_config 한 행."""
    row = {"config_id": 1, "office_id": OFFICE_ID, "group_id": GROUP_ID,
           "constraints_json": constraints, "constraints_max_json": maximums or {},
           "use_dynamic_scaling": 1, "allow_soft_fallback": 0, **stamp()}
    row.update(over)
    return row


# ── 병동 ────────────────────────────────────────────────────────────────


@dataclass
class Ward:
    """시나리오 하나의 시드 재료. `seed(db)` 가 순서대로 넣는다."""

    nurses: list[dict]
    shifts: list[dict]
    shift_manage: list[dict]
    config: dict
    raw: dict
    grade: dict | None
    year: int = TARGET_YEAR
    month: int = TARGET_MONTH
    other_groups: list[str] = field(default_factory=list)
    extras: list[Callable] = field(default_factory=list)


def base_ward(nurse_count: int = 10) -> Ward:
    """기본 병동 G1 — RN nurse_count 명 · D/E/N/OFF · 요구 D2/E2/N2 · raw n2n=0·공휴일OFF=0."""
    return Ward(
        nurses=nurse_rows(nurse_count),
        shifts=[shift_row(*s) for s in BASE_SHIFTS],
        shift_manage=[shift_manage_row(*m) for m in BASE_SHIFT_MANAGE],
        config=config_row(),
        raw={"n2n_min_gap": 0, "fixed_holiday_off_yn": 0},
        grade=grade_row({"1": {"D": 1, "E": 1, "N": 1}}),
    )


def _add_all(db, model, rows: list[dict]) -> None:
    """행 목록을 모델 인스턴스로 넣는다(재료는 복사 — 실행마다 같은 재료에서 시작)."""
    for row in rows:
        db.add(model(**copy.deepcopy(row)))


def _seed_org(db, ward: Ward) -> None:
    """병원·병동(G1 + 상대 병동)·수간호사."""
    from db.models import Group, Nurse, Office

    db.add(Office(office_id=OFFICE_ID, office_name="합성병원"))
    for gid in [GROUP_ID, *ward.other_groups]:
        db.add(Group(group_id=gid, office_id=OFFICE_ID, group_name=f"합성병동{gid}", hn_id=[]))
    db.add(Nurse(**nurse_row(HEAD_NURSE_ID, 0, role="HN", is_head_nurse=True, experience=25,
                             grade=3, work_shifts=["D"], joining_date=datetime(2010, 3, 1)),
                 **stamp()))


def _seed_config(db, ward: Ward) -> None:
    """설정 행 + 등급 설정 + raw 컬럼 값."""
    from db.models import RosterConfig, RosterGradeConfig

    db.add(RosterConfig(**copy.deepcopy(ward.config)))
    if ward.grade is not None:
        db.add(RosterGradeConfig(**copy.deepcopy(ward.grade)))
    db.flush()
    if ward.raw:
        sets = ", ".join(f"{col} = :{col}" for col in ward.raw)
        db.execute(text(f"UPDATE roster_config SET {sets} WHERE config_id = :cid"),
                   {**ward.raw, "cid": ward.config["config_id"]})


def _seed_wanted(db, ward: Ward) -> None:
    """대상 달 원티드 열림."""
    from db.models import Wanted

    db.add(Wanted(group_id=GROUP_ID, year=ward.year, month=ward.month, status="requested",
                  exp_date=datetime(2026, 10, 20, 23, 59), created_at=SEED_TS))


def seed_ward(ward: Ward, db) -> None:
    """병동 시드 — 조직 → 간호사 → 근무코드·요구 → 설정 → 원티드 → 시나리오 추가분."""
    from db.models import Nurse, Shift, ShiftManage

    _seed_org(db, ward)
    _add_all(db, Nurse, [{**row, **stamp()} for row in ward.nurses])
    _add_all(db, Shift, ward.shifts)
    _add_all(db, ShiftManage, ward.shift_manage)
    _seed_config(db, ward)
    _seed_wanted(db, ward)
    db.flush()
    for extra in ward.extras:
        extra(db)
        db.flush()
