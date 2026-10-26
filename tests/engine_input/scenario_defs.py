"""골든 시나리오 S1~S9 · 변형 S1-D·S1-P·S9-F (설계서 §7.1 '시나리오 9종 + 변형').

사다리 모드(S1-L·S1-LT)·성공 모드 L3 후처리(S1-L3)·시도별 env 는 EI-1b 몫으로
`scenario_ladder.py`·`test_ladder.py` 에 따로 있다(이 12종의 테스트·골든과 겹치지 않는다).
모든 사람·병동은 합성(N01·X01·F01·HN01 · 병원 O1 · 병동 G1/G2). 대상 달은 2026-11
(일요일 시작·30일·평일 21일), S5 만 공휴일(성탄절 금요일)이 있는 2026-12 다. 오늘(고정) 2026-10-01.

각 시나리오가 지나야 할 코드 경로는 `PATH_CHECKS`(test_scenarios.py)가 실행 결과로 확인한다.
"""

from __future__ import annotations

import copy
import importlib
import sys
from datetime import date, datetime, time
from functools import partial

from .harness import Scenario
from .mssql_shim import FIXED_NOW
from .scenarios import make_head_nurse
from .ward import (
    GROUP_ID,
    OFFICE_ID,
    OTHER_GROUP_ID,
    RAW_COLUMN_DDL,
    SEED_TS,
    SUPPORT_DDL,
    Ward,
    base_ward,
    config_row,
    grade_row,
    nurse_row,
    seed_ward,
    shift_manage_row,
    shift_row,
    stamp,
)

BASE_DDL = RAW_COLUMN_DDL + SUPPORT_DDL
#: 병동이동 발효 알림(S08) 대기열 — 운영 테이블 모양(SQLite 자동 채번 형태).
OUTBOX_DDL = (
    "CREATE TABLE push_outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, source VARCHAR(50) NOT NULL,"
    " push_code VARCHAR(10) NOT NULL, push_sub_code VARCHAR(10) NOT NULL,"
    " office_code VARCHAR(50) NOT NULL, sender_emp_seq_no VARCHAR(50) NOT NULL,"
    " sender_member_id VARCHAR(100) NOT NULL, recipients TEXT NOT NULL,"
    " recipient_count INTEGER NOT NULL, message VARCHAR(1000) NOT NULL,"
    " org_message VARCHAR(2000) NOT NULL, link_url VARCHAR(500) NOT NULL,"
    " link_code VARCHAR(100) NOT NULL, guard VARCHAR(200), status VARCHAR(20) NOT NULL,"
    " attempts INTEGER NOT NULL, next_attempt_at DATETIME NOT NULL, claimed_at DATETIME,"
    " master_idx BIGINT, device_count INTEGER, last_error VARCHAR(1000),"
    " created_at DATETIME NOT NULL, sent_at DATETIME)",
)
_OUTBOX_MODULE = "services.push_outbox_service"


def _d(month: int, day: int, year: int = 2026) -> date:
    """날짜 줄임."""
    return date(year, month, day)


def _request(year: int, month: int, **over):
    """RosterRequest 공장(설정 1번 지정 + 요청 칸 덮어쓰기)."""
    def make():
        from schemas.roster_schema import RosterRequest

        return RosterRequest(year=year, month=month, config_id=1, **over)
    return make


def _scenario(name: str, ward: Ward, description: str, *, ddl=BASE_DDL, request=None,
              patches=(), stop_at: str = "L2") -> Scenario:
    """병동 재료 → Scenario."""
    return Scenario(
        name=name, seed=partial(seed_ward, ward),
        make_request=request or _request(ward.year, ward.month),
        make_user=make_head_nurse, ddl=tuple(ddl), patches=tuple(patches),
        stop_at=stop_at, description=description)


def _rows(model_name: str, rows: list[dict]):
    """추가 행 시드 함수(모델 이름 + 행 목록 — 실행마다 복사해 넣는다)."""
    def add(db):
        import db.models as models

        model = getattr(models, model_name)
        for row in rows:
            db.add(model(**copy.deepcopy(row)))
    return add


# ── 시나리오 패치(실행 동안만) ──────────────────────────────────────────


def skip_primary_off(mp) -> None:
    """S1-P — primary 경로를 강제한다(엔진은 호출 시점에 env 를 읽는다, cp_sat_basic.py:1520)."""
    mp.setenv("SKIP_PRIMARY", "0")


def enable_push_outbox(mp) -> None:
    """S6 — 병동이동 발효 알림 대기열을 운영처럼 쓰게 한다.

    가짜 `db.client2` 에 `EUN_DB_NAME` 이 없어 `push_outbox_service` import 가 실패한다. 그대로
    두면 `_enqueue_assignment_push` 가 ImportError 를 삼키고 ERROR 로그만 남겨, 운영의 대기열
    INSERT(같은 커밋)가 빠진다. 실행 동안만 이름을 주고 모듈을 올린 뒤, 끝나면 import 흔적
    (sys.modules·부모 패키지 속성)까지 되돌린다. 대기열 시각(`_now`)은 고정 시각이다.
    """
    mp.setattr(sys.modules["db.client2"], "EUN_DB_NAME", "eun_roster_harness", raising=False)
    module = sys.modules.get(_OUTBOX_MODULE)
    if module is None:
        module = importlib.import_module(_OUTBOX_MODULE)
        del sys.modules[_OUTBOX_MODULE]
        mp.setitem(sys.modules, _OUTBOX_MODULE, module)
        parent = sys.modules["services"]
        delattr(parent, "push_outbox_service")
        mp.setattr(parent, "push_outbox_service", module, raising=False)
    mp.setattr(module, "_now", lambda: FIXED_NOW)


# ── S1 기본 · S1-D · S1-P ───────────────────────────────────────────────


def _s1_wanted(db) -> None:
    """제출된 원티드(WantedRequest 경로 — 확정 원티드 없음): N01 D·O, N02 N, N03→N04 짝."""
    from db.models import NursePairRequest, NurseShiftRequest, WantedRequest

    for nid, req_text in (("N01", "3일 데이 희망"), ("N02", "나이트 선호"), ("N03", "짝 희망")):
        db.add(WantedRequest(nurse_id=nid, request_id=1, month="2026-11", group_id=GROUP_ID,
                             request=req_text, is_submitted=1, created_at=SEED_TS,
                             submitted_at=SEED_TS))
    for nid, day, code, score in (("N01", 3, "D", 8), ("N01", 10, "O", 9), ("N02", 5, "N", 6)):
        db.add(NurseShiftRequest(nurse_id=nid, request_id=1, detailed_request_id=day,
                                 shift_date=_d(11, day), group_id=GROUP_ID, shift=code, score=score))
    db.add(NursePairRequest(nurse_id="N03", request_id=1, month="2026-11", detailed_request_id=1,
                            target_id="N04", group_id=GROUP_ID, score=5))


def s1_ward() -> Ward:
    """S1 — RN 10명(등급 1×4·2×6) · 요구 D2/E2/N2 · raw n2n=4·공휴일OFF=0 · 제출 원티드."""
    ward = base_ward(10)
    for row in ward.nurses[4:]:
        row["grade"] = 2
    ward.raw = {"n2n_min_gap": 4, "fixed_holiday_off_yn": 0}
    ward.extras.append(_s1_wanted)
    return ward


def _s1d_seed_daily_shift(db) -> None:
    """9월에 화면(GET /daily-shift)을 열어 **그때 템플릿(N1)** 으로 11월 행이 시딩됐고, 그 뒤 N2 로 바뀐 이력.

    시딩은 운영 함수(`daily_shift_service.get_or_init_month` :131-175)를 그대로 부른다.
    """
    from db.models import ShiftManage
    from services.daily_shift_service import get_or_init_month

    night = db.query(ShiftManage).filter(ShiftManage.group_id == GROUP_ID,
                                         ShiftManage.main_code == "N").one()
    night.manpower = 1
    db.flush()
    get_or_init_month(db, OFFICE_ID, GROUP_ID, 2026, 11)
    night.manpower = 2
    night.updated_at = SEED_TS  # 쌍의 차이를 시딩 행으로만 좁힌다(onupdate 시각 되돌림)


def s1d_ward() -> Ward:
    """S1-D — S1 + 시딩된 daily_shift 30행(N1). S1 이 '시딩 없음' 짝이다(MS-9)."""
    ward = s1_ward()
    ward.extras.append(_s1d_seed_daily_shift)
    return ward


# ── S2 하드락 키 NULL · S3 모든 컬럼 NULL ───────────────────────────────

#: S2 — 하드락 8종 중 NULL 이 들어갈 수 있는 키(not_one_night·use_mid 는 NOT NULL 컬럼, models.py:855-856).
#: max_conseq_work 는 뺐다 — NULL 이면 L2(create_config_from_db :606)가 TypeError 로 멈춰 나머지 키의
#: 실효값을 못 남긴다. 그 TypeError 경로는 S3 가 L0·L0'·L1 까지 남긴다.
S2_NULL_KEYS = ("max_nig_per_month", "three_seq_nig", "two_offs_after_three_nig",
                "two_offs_after_two_nig", "banned_day_after_eve")
#: S3 — roster_config 의 nullable 컬럼 전부(NOT NULL 컬럼은 모델 기본값).
S3_NULL_KEYS = (
    "day_req", "eve_req", "nig_req", "min_exp_per_shift", "req_exp_nurses", "two_offs_per_week",
    "max_nig_per_month", "three_seq_nig", "two_offs_after_three_nig", "two_offs_after_two_nig",
    "banned_day_after_eve", "max_conseq_work", "off_days", "max_conseq_off", "shift_priority",
    "sequential_offs", "nod_noe", "weekly_off_group", "ban_night_before_fixed_wanted_off",
    "health_leave_enabled", "health_leave_weekend", "sleep_off_enabled", "sleep_off_cycle",
    "version", "config_name", "config_memo", "created_at", "updated_at", "last_generate_status",
)


def s2_ward() -> Ward:
    """S2 — S1 병동 + 하드락 키 NULL 행."""
    ward = s1_ward()
    ward.config.update({key: None for key in S2_NULL_KEYS})
    return ward


def s3_ward() -> Ward:
    """S3 — S1 병동 + nullable 컬럼 전부 NULL(raw 두 컬럼도 NULL)."""
    ward = s1_ward()
    ward.config.update({key: None for key in S3_NULL_KEYS})
    ward.raw = {"n2n_min_gap": None, "fixed_holiday_off_yn": None}
    return ward


# ── S4 MID · 한글 shift_gb · 등급 상한 · 팀 · 인계 ─────────────────────


def _s4_shifts() -> list[dict]:
    """D/E/N/OFF + MID(D2 → M) + 한글 shift_gb 하위코드(D1 데이 · E1 이브닝)."""
    return [
        shift_row("D", 1, "데이", "D", "근무", "D", 1, time(7, 0), time(15, 0)),
        shift_row("E", 2, "이브닝", "E", "근무", "E", 2, time(15, 0), time(23, 0)),
        shift_row("N", 3, "나이트", "N", "근무", "N", 3, time(23, 0), time(7, 0)),
        shift_row("OFF", 4, "오프", None, "오프", "O", 4, show=False),
        shift_row("D2", 5, "미드", "M", "근무", "M", 5, time(11, 0), time(19, 0)),
        shift_row("D1", 6, "데이1", "데이", "근무", None, 6, time(8, 0), time(16, 0)),
        shift_row("E1", 7, "이브1", "이브닝", "근무", "E", 7, time(14, 0), time(22, 0)),
    ]


def _s4_teams(db) -> None:
    """팀 1(min NULL·인계 제한) · 팀 2(min {}) · 팀 3(min 전부 0) + 팀 1 은 기간표, 2·3 은 캐시."""
    from db.models import NurseTeamPeriod, Team

    handoff = {"restrictions": [{"grades": [3], "block_same_shift": True, "block_adjacent": False}]}
    for tid, min_shift, policy in ((1, None, handoff), (2, {}, None),
                                   (3, {"D": 0, "E": 0, "N": 0}, None)):
        db.add(Team(office_id=OFFICE_ID, group_id=GROUP_ID, team_id=tid, team_name=f"팀{tid}",
                    active=1, min_shift=min_shift, handoff_policy=policy, **stamp()))
    for nid in ("N01", "N02", "N03", "N04"):
        db.add(NurseTeamPeriod(nurse_id=nid, group_id=GROUP_ID, valid_from=_d(1, 1),
                               team_id=1, source="edited", **stamp()))


def _s4_nurses() -> list[dict]:
    """RN 12명 — 팀 캐시(2·3) · 등급 1~3 · MID 전담 1명 · D+MID 1명."""
    rows = [nurse_row(f"N{i:02d}", i, grade=1 + (i - 1) % 3) for i in range(1, 13)]
    for row in rows[4:8]:
        row["team_id"] = 2
    for row in rows[8:10]:
        row["team_id"] = 3
    rows[10]["allowed_shifts"] = ["M"]
    rows[11]["allowed_shifts"] = ["D", "M"]
    return rows


def s4_ward() -> Ward:
    """S4 — use_mid + M=D2 + 한글 shift_gb 하위코드 + 등급 allow0·상한 + 팀 NULL/{}/{0} + 인계."""
    ward = base_ward(12)
    ward.nurses = _s4_nurses()
    ward.shifts = _s4_shifts()
    ward.shift_manage = [shift_manage_row(1, "D", ["D", "D1"], 2),
                         shift_manage_row(2, "E", ["E", "E1"], 2),
                         shift_manage_row(3, "N", ["N"], 2), shift_manage_row(5, "M", ["D2"], 1)]
    ward.config = config_row(use_mid=True)
    ward.grade = grade_row({"1": {"D": 1, "E": 1, "N": 1}, "2": {"D": 0, "M": 0}},
                           {"3": {"D": 1, "E": 1, "N": 1}}, allow_soft_fallback=0)
    ward.extras.append(_s4_teams)
    return ward


# ── S5 고정근무 · 공휴일 OFF · 주말휴무 · 주휴 ─────────────────────────


def _s5_periods(db) -> None:
    """주말휴무 기간표(F01·N01·N02) + 주휴 설정(활성 · 월 변동주기 · 기준 2026-10 · 변동 −1)."""
    from db.models import NurseWeekendOffPeriod, WeeklyOffSetting

    for nid in ("F01", "N01", "N02"):
        db.add(NurseWeekendOffPeriod(nurse_id=nid, valid_from=_d(1, 1), weekend_off=1,
                                     source="edited", **stamp()))
    db.add(WeeklyOffSetting(office_id=OFFICE_ID, group_id=GROUP_ID, activate=1,
                            use_variable_cycle=1, cycle_type="month", base_year=2026,
                            base_month=10, cycle_interval=1, shift_variation=-1, **stamp()))


def _s5_nurses() -> list[dict]:
    """RN 10명 + 고정 D 근무자 F01 — 주휴 대상(요일 int) 4명."""
    rows = [nurse_row(f"N{i:02d}", i) for i in range(1, 11)]
    for row, weekday in zip(rows[:4], (6, 6, 5, 2), strict=True):
        row.update(weekly_off_enabled=1, weekly_off_weekday=weekday)
    fixed = nurse_row("F01", 11, fixed_shift="D", weekly_off_enabled=1, weekly_off_weekday=6)
    return [*rows, fixed]


def s5_ward() -> Ward:
    """S5 — 2026-12(성탄절 금) · 고정근무자 F01(D) · raw 공휴일OFF=1 · 주말휴무 · 주휴 변동주기."""
    ward = base_ward(10)
    ward.year, ward.month = 2026, 12
    ward.nurses = _s5_nurses()
    ward.raw = {"n2n_min_gap": 0, "fixed_holiday_off_yn": 1}
    ward.extras.append(_s5_periods)
    return ward


# ── S6 파견 in/out · 병동이동 · 휴직 ───────────────────────────────────


#: nurse_assignment.kind — 앱의 사유→kind 표(assignment_service.REASON_TO_KIND)와 같은 값.
_KIND = {"파견": "dispatch", "병동이동": "transfer", "휴직": "leave"}


def _assignment(nid: str, src: str, dst, start: date, end, reason: str, **over) -> dict:
    """nurse_assignment 한 행."""
    row = {"nurse_id": nid, "source_group_id": src, "target_group_id": dst,
           "office_id": OFFICE_ID, "start_date": start, "expected_end_date": end,
           "reason": reason, "kind": _KIND[reason], "status": "active", **stamp()}
    row.update(over)
    return row


def _s6_assignments() -> list[dict]:
    """파견 out(N07) · 파견 in(X01) · 병동이동 out 월중(N08) · 병동이동 발효 대상(X02) · 휴직(N06)."""
    return [
        _assignment("N07", GROUP_ID, OTHER_GROUP_ID, _d(11, 10), _d(11, 14), "파견",
                    target_grade=1, target_shift_types=["D", "E"]),
        _assignment("X01", OTHER_GROUP_ID, GROUP_ID, _d(11, 20), _d(11, 25), "파견",
                    target_grade=2, target_shift_types=["D", "E"], target_weekly_off_enabled=0),
        _assignment("N08", GROUP_ID, OTHER_GROUP_ID, _d(11, 16), None, "병동이동"),
        _assignment("X02", OTHER_GROUP_ID, GROUP_ID, _d(9, 15), None, "병동이동",
                    target_weekly_off_enabled=0),
        _assignment("N06", GROUP_ID, None, _d(11, 21), None, "휴직"),
    ]


def _s6_other_schedule(db) -> None:
    """상대 병동 G2 의 11월 근무표 — G2 로 파견 간 N07(OFF 1칸) · G1 로 파견 올 X01(직전 18·19일 N)."""
    from db.models import Schedule, ScheduleEntry

    db.add(Schedule(schedule_id="g2nov0000001", office_id=OFFICE_ID, group_id=OTHER_GROUP_ID,
                    year=2026, month=11, version=1, config_id=None, created_by="acc_HN01",
                    status="draft", dropped=False, name="G2 11월", **stamp()))
    cells = [("N07", d, "O" if d == 12 else "D") for d in range(10, 15)]
    cells += [("X01", d, "N" if d >= 18 else "D") for d in range(14, 20)]
    for i, (nid, day, code) in enumerate(cells):
        db.add(ScheduleEntry(entry_id=f"g2e{i:05d}", schedule_id="g2nov0000001", nurse_id=nid,
                             work_date=datetime(2026, 11, day), shift_id=code))


def s6_ward() -> Ward:
    """S6 — G1 RN 10명 + G2 간호사 X01·X02 · 배정 5건 · G2 11월 근무표."""
    ward = base_ward(10)
    ward.other_groups = [OTHER_GROUP_ID]
    others = [nurse_row(nid, 20 + i, group_id=OTHER_GROUP_ID) for i, nid in enumerate(("X01", "X02"))]
    ward.extras += [_rows("Nurse", [{**r, **stamp()} for r in others]),
                    _rows("NurseAssignment", _s6_assignments()), _s6_other_schedule]
    return ward


# ── S7 전월 마감 꼬리 · n2n_min_gap ────────────────────────────────────

#: 전월(10월) 마지막 6일(26~31일) — NN 꼬리 · 3N 뒤 OFF 1칸 · 1N 꼬리 · 나머지 일반.
S7_TAILS = {
    "N01": "DEOONN", "N02": "EONNNO", "N03": "DDEOON", "N04": "OODDEE", "N05": "NNOODD",
    "N06": "EEOODD", "N07": "DDOOEE", "N08": "ONNOOD", "N09": "DEEOOO", "N10": "OOEEDD",
}


def _s7_entries() -> dict[str, list[tuple[str, int, str]]]:
    """근무표별 칸 — v1 issued 는 꼬리 6일, v2 draft 는 31일 D 만(고르면 꼬리가 달라진다)."""
    issued = [(nid, 26 + i, code) for nid, tail in S7_TAILS.items() for i, code in enumerate(tail)]
    return {"g1oct0000001": issued, "g1oct0000002": [(nid, 31, "D") for nid in S7_TAILS]}


def _s7_prev_schedules(db) -> None:
    """10월 근무표 2벌 — v1 issued(마감본, 꼬리 담김) · v2 draft(최신 버전이지만 issued 가 우선)."""
    from db.models import Schedule, ScheduleEntry

    for sid, version, status in (("g1oct0000001", 1, "issued"), ("g1oct0000002", 2, "draft")):
        db.add(Schedule(schedule_id=sid, office_id=OFFICE_ID, group_id=GROUP_ID, year=2026,
                        month=10, version=version, config_id=1, created_by="acc_HN01",
                        status=status, dropped=False, name=f"10월 VER{version}", **stamp()))
    for sid, cells in _s7_entries().items():
        for i, (nid, day, code) in enumerate(cells):
            db.add(ScheduleEntry(entry_id=f"{sid[-1]}e{i:05d}", schedule_id=sid, nurse_id=nid,
                                 work_date=datetime(2026, 10, day),
                                 shift_id="OFF" if code == "O" else code))


def s7_ward() -> Ward:
    """S7 — 2N·3N 뒤 2OFF · 1N 금지 켬 · raw n2n=5 · 10월 마감본 꼬리."""
    ward = base_ward(10)
    ward.config.update(two_offs_after_two_nig=True, two_offs_after_three_nig=True,
                       not_one_night=True)
    ward.raw = {"n2n_min_gap": 5, "fixed_holiday_off_yn": 0}
    ward.extras.append(_s7_prev_schedules)
    return ward


# ── S8 확정 원티드 · 금지 원티드 · 카드 delta · 처방 · 1N · 분배 게이지 ──


def _s8_fixed_wanted() -> list[dict]:
    """확정 원티드 — 적용(T) 4건(D·OFF·N(shifts.id)·연차 특수코드) + 미적용(F) 1건."""
    base = {"group_id": GROUP_ID, "year": 2026, "month": 11, **stamp()}
    rows = [("N01", 3, "D", None, True), ("N01", 10, "OFF", None, True),
            ("N02", 5, "N", 3, True), ("N04", 12, "연", 8, True), ("N03", 7, "OFF", None, False)]
    return [{**base, "nurse_id": nid, "shift_date": _d(11, day), "shift_id": code,
             "shifts_table_id": table_id, "is_applied": applied, "source_type": "original",
             "original_shift_id": code, "created_by": "HN01"}
            for nid, day, code, table_id, applied in rows]


def _s8_banned_wanted() -> list[dict]:
    """금지 원티드 — 적용 2건(N05 N · N06 D·E) + 미적용 1건(N07 N)."""
    base = {"group_id": GROUP_ID, "year": 2026, "month": 11, "source": "hn", **stamp()}
    rows = [("N05", 8, ["N"], True), ("N06", 9, ["D", "E"], True), ("N07", 11, ["N"], False)]
    return [{**base, "nurse_id": nid, "shift_date": _d(11, day), "banned_shift_ids": codes,
             "is_applied": applied} for nid, day, codes, applied in rows]


def _s8_pair(db) -> None:
    """확정 원티드 경로도 짝 선호는 WantedRequest 최신 건에서 읽는다(:253-263)."""
    from db.models import NursePairRequest, WantedRequest

    db.add(WantedRequest(nurse_id="N01", request_id=1, month="2026-11", group_id=GROUP_ID,
                         request="짝 희망", is_submitted=1, created_at=SEED_TS,
                         submitted_at=SEED_TS))
    db.add(NursePairRequest(nurse_id="N01", request_id=1, month="2026-11", detailed_request_id=1,
                            target_id="N02", group_id=GROUP_ID, score=4))


def s8_ward() -> Ward:
    """S8 — 확정 원티드 사용(T) · 특수코드 '연'(휴가) · 금지 원티드 · raw n2n=4(카드가 0 으로 덮음)."""
    ward = base_ward(10)
    ward.shifts.append(shift_row("연", 8, "연차", None, "휴가", None, 8))
    ward.config.update(fixed_wanted_use_yn=True)
    ward.raw = {"n2n_min_gap": 4, "fixed_holiday_off_yn": 0}
    ward.extras += [_rows("FixedWantedEntry", _s8_fixed_wanted()),
                    _rows("BannedWantedEntry", _s8_banned_wanted()), _s8_pair]
    return ward


S8_REQUEST = {
    "config_override": {"n2n_min_gap": 0, "same_shift_hard_k": 0},
    "treatment_ids": ["treatment:soft:grade_max"],
    "not_one_night": True,
    "distribution_mode": "balanced",
    "oversupply_balance_gauge": 8,
    "monthly_preference_gauge": 5,
    "monthly_shift_preferences": {"N03": {"shift": "D", "strength": 7}},
}


# ── S9 프리셉티 · 상호배제 · 월한도 이월 · 보건휴가 / S9-F ──────────────


def _s9_people(db) -> None:
    """프리셉티 기간(N09←N01, 백필됨) · 캐시만 있는 N10(←N02) · 상호배제 N03↔N04 · 보건 예외(N02).

    팀 1(N01~N05·N09)·팀 2(N06~N08·N10) — 팀이 없으면 precheck 가 프리셉티 쌍을
    PRECEPTEE_SYNC_MISMATCH(team_mismatch, hard)로 막는다(team_grade_precheck.py:1127-1143:
    양쪽 팀이 None 이어도 '불일치'). 그러면 나이트 탐지기(:6797)에 닿지 못한다.
    """
    from db.models import (
        NurseLeavePeriod,
        NurseMutualExclusionPeriod,
        NursePrecepteePeriod,
        Team,
    )

    for tid in (1, 2):
        db.add(Team(office_id=OFFICE_ID, group_id=GROUP_ID, team_id=tid, team_name=f"팀{tid}",
                    active=1, min_shift=None, **stamp()))

    db.add(NursePrecepteePeriod(nurse_id="N09", preceptor_id="N01", office_id=OFFICE_ID,
                                valid_from=_d(11, 1), valid_to=_d(11, 16), source="edited",
                                **stamp()))
    for nid, partner in (("N03", "N04"), ("N04", "N03")):
        db.add(NurseMutualExclusionPeriod(nurse_id=nid, partner_id=partner, office_id=OFFICE_ID,
                                          valid_from=_d(10, 1), source="edited", **stamp()))
    db.add(NurseLeavePeriod(nurse_id="N02", valid_from=_d(1, 1), health_leave_eligible=False,
                            source="edited", **stamp()))


def _monthly_limits(floors: dict[str, int], month: int = 10) -> list[dict]:
    """월 한도 행(n_exact) — 대상 달(11월) 행이 없어 10월 값이 이월된다(:691)."""
    return [{"nurse_id": nid, "group_id": GROUP_ID, "year": 2026, "month": month,
             "n_exact": floor, **stamp()} for nid, floor in floors.items()]


#: S9 — n_exact 합 36 > N 칸 30(하루 N 1명 × 30일, off_first · N 상한 없음) → 나이트 차단.
S9_FLOORS = {f"N0{i}": 6 for i in range(1, 7)}
#: S9-F — 합 16: 차감 전 칸 30 이하 · 차감 뒤 칸 9(평일 21일은 고정 N 이 채움) 초과.
S9F_FLOORS = {f"N0{i}": 4 for i in range(1, 5)}


def s9_ward(floors: dict[str, int] = S9_FLOORS) -> Ward:
    """S9 — off_first · N 1명/일 · 프리셉티 부분 백필 · 상호배제 · 10월 한도 이월 · 보건휴가."""
    ward = base_ward(10)
    for i, row in enumerate(ward.nurses):
        row["team_id"] = 1 if i < 5 or i == 8 else 2
        if i < 8:
            row["gender"] = "여"
    ward.nurses[9]["preceptor_id"] = "N02"
    ward.shift_manage = [shift_manage_row(1, "D", ["D"], 2), shift_manage_row(2, "E", ["E"], 2),
                         shift_manage_row(3, "N", ["N"], 1)]
    ward.shifts.append(shift_row("보", 8, "보건", None, "휴가", None, 8, health_leave_target=True))
    ward.config.update(off_first=True, preceptee_on=True, health_leave_enabled=True)
    ward.extras += [_s9_people, _rows("NurseMonthlyLimit", _monthly_limits(floors))]
    return ward


def _s9f_fixed_night(db) -> None:
    """S9-F — N 고정근무자 F01 의 주말휴무 기간표(고정근무는 주말휴무 대상만 허용, :5375)."""
    from db.models import NurseWeekendOffPeriod

    db.add(NurseWeekendOffPeriod(nurse_id="F01", valid_from=_d(1, 1), weekend_off=1,
                                 source="edited", **stamp()))


def s9f_ward() -> Ward:
    """S9-F — S9 + N 고정근무자 F01 · 하한 합 16(차감 전 30 이하 · 차감 뒤 9 초과)."""
    ward = s9_ward(S9F_FLOORS)
    ward.nurses.append(nurse_row("F01", 11, fixed_shift="N", gender="여"))
    ward.extras.append(_s9f_fixed_night)
    return ward


# ── 등록 ────────────────────────────────────────────────────────────────


def build_scenarios() -> dict[str, Scenario]:
    """시나리오 이름 → Scenario(실행 순서 = 등록 순서)."""
    items = [
        _scenario("S1", s1_ward(), "기본 — raw 컬럼 있음(n2n 4) · 제출 원티드 · daily_shift 시딩 없음"),
        _scenario("S1-D", s1d_ward(), "S1 + daily_shift 시딩 있음(시딩 때 템플릿 N1, 지금 N2) — MS-9 쌍"),
        _scenario("S1-P", s1_ward(), "S1 + SKIP_PRIMARY=0 — primary 빌드 요약 뒤 멈춤(solve 0)",
                  patches=(skip_primary_off,), stop_at="primary_build"),
        _scenario("S2", s2_ward(), "하드락 키 NULL 행(max_conseq_work 제외 — S3 참조)"),
        _scenario("S3", s3_ward(), "모든 nullable 컬럼 NULL(BR-1) — 현행 TypeError 경로"),
        _scenario("S4", s4_ward(), "use_mid + M=D2 + 한글 shift_gb + 등급 allow0·상한 + 팀 NULL/{}/{0} + 인계"),
        _scenario("S5", s5_ward(), "고정근무자 + 공휴일 OFF(2026-12) + 주말휴무 + 주휴 변동주기·요일 int"),
        _scenario("S6", s6_ward(), "파견 in/out · 병동이동(월중 out · 발효 flush) · 휴직",
                  ddl=BASE_DDL + OUTBOX_DDL, patches=(enable_push_outbox,)),
        _scenario("S7", s7_ward(), "전월 마감 꼬리 NN·3N 뒤 OFF·1N + n2n_min_gap 5"),
        _scenario("S8", s8_ward(), "확정 원티드 T/F + 금지 원티드 + 카드 delta + 처방 + 1N + 분배 게이지",
                  request=_request(2026, 11, **S8_REQUEST)),
        _scenario("S9", s9_ward(), "프리셉티 부분 백필 + 상호배제 + 월한도 이월 n_exact 합>N칸 + 보건휴가"),
        _scenario("S9-F", s9f_ward(), "S9 + N 고정근무자 — 차감 뒤 수요로만 나이트 차단"),
    ]
    return {s.name: s for s in items}


SCENARIOS = build_scenarios()
