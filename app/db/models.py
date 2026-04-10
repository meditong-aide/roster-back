from sqlalchemy import (
    Column,
    VARCHAR,
    NVARCHAR,
    SMALLINT,
    BIGINT,
    BOOLEAN,
    DATETIME,
    func,
    ForeignKey,
    JSON,
    CHAR,
    INTEGER,
    FLOAT,
    Index,
    ForeignKeyConstraint,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import TINYINT
from sqlalchemy.orm import relationship, deferred
from db.client2 import Base
from sqlalchemy import DATE, DECIMAL, TEXT, Time


class Group(Base):
    __tablename__ = "groups"
    group_id = Column(VARCHAR(50), primary_key=True)
    office_id = Column(VARCHAR(50), ForeignKey("offices.office_id"))
    group_name = Column(VARCHAR(50), nullable=False)
    hn_id = Column(JSON, nullable=True, default=list)  # 그룹 관리자 nurse_id 리스트
    # office = relationship("Office", back_populates="groups")


class Office(Base):
    __tablename__ = "offices"
    office_id = Column(VARCHAR(50), primary_key=True)
    office_name = Column(VARCHAR(100), nullable=False)
    # address = Column(VARCHAR(255))
    # contact_number = Column(VARCHAR(30))
    # groups = relationship("Group", back_populates="office")


class Team(Base):
    __tablename__ = "teams"
    office_id = Column(VARCHAR(50), ForeignKey("offices.office_id"), primary_key=True)
    group_id = Column(VARCHAR(50), ForeignKey("groups.group_id"), primary_key=True)
    team_id = Column(INTEGER, primary_key=True)  # 그룹 내 로컬 식별자
    team_name = Column(VARCHAR(100), nullable=False)
    active = Column(TINYINT, nullable=False, default=1)
    created_at = Column(DATETIME, default=func.now())
    updated_at = Column(DATETIME, default=func.now(), onupdate=func.now())

    office = relationship("Office")
    group = relationship("Group")
    __table_args__ = (
        Index("ux_teams_group_name", "group_id", "team_name", unique=True),
        UniqueConstraint("group_id", "team_id", name="ux_teams_group_teamid"),
    )


class Nurse(Base):
    __tablename__ = "nurses"
    # office_id = Column(VARCHAR(50), ForeignKey('offices.office_id'), nullable=True)
    nurse_id = Column(VARCHAR(50), primary_key=True)
    group_id = Column(VARCHAR(50), ForeignKey("groups.group_id"))
    # 관리자(ADM) 계정처럼 group_id가 없을 수 있으므로 office_id를 실컬럼으로 보유
    office_id = Column(VARCHAR(50), nullable=True)
    account_id = Column(VARCHAR(50), unique=True, nullable=False)
    emp_num = Column(VARCHAR(50), nullable=True)
    name = Column(VARCHAR(50), nullable=False)
    experience = Column(SMALLINT)
    role = Column(VARCHAR(20))
    level_ = Column(VARCHAR(20))
    is_head_nurse = Column(BOOLEAN, default=False)
    # 마스터 관리자 구분 코드(ADM/HDN/...) - 실제 컬럼명 EmpAuthGbn 매핑
    emp_auth_gbn = Column(VARCHAR(3), name="EmpAuthGbn", nullable=True)
    is_night_nurse = Column(JSON, nullable=True, default=list)
    personal_off_adjustment = Column(TINYINT, default=0)
    preceptor_id = Column(VARCHAR(50), ForeignKey("nurses.nurse_id"))
    joining_date = Column(DATETIME, nullable=True)
    created_at = Column(DATETIME, default=func.now())
    updated_at = Column(DATETIME, default=func.now(), onupdate=func.now())
    resignation_date = Column(DATETIME, nullable=True)
    # 근무 종료 사유: 휴직 / 퇴사 / 파견 / 부서이동 또는 기타 자유 입력 텍스트
    resignation_reason = Column(NVARCHAR(100), nullable=True)
    # 기타 사유 자유 입력 텍스트
    resignation_reason_memo = Column(NVARCHAR(200), nullable=True)
    # 화면 표시 및 알고리즘 입력 순서 제어용
    sequence = Column(INTEGER, nullable=False, default=0)
    active = Column(INTEGER, default=1)
    team_id = Column(INTEGER, nullable=True)
    # 주휴 관련 추가 컬럼
    weekly_off_enabled = Column(TINYINT, default=0)  # 주휴 대상 여부
    weekly_off_weekday = Column(
        TINYINT, nullable=True
    )  # 기준 월에서의 주휴 요일 (0:월~6:일)
    # 고정 근무 코드(해당 병동의 shifts.shift_id)
    fixed_shift = Column(VARCHAR(20), nullable=True)
    nurse_memo = Column(TEXT, nullable=True)
    grade = Column(INTEGER, nullable=True)
    # 사이드 프로필 관련 추가 컬럼
    birth_date = Column(VARCHAR(10), nullable=True)
    phone_number = Column(VARCHAR(20), nullable=True)
    email = Column(NVARCHAR(100), nullable=True)
    gender = Column(NVARCHAR(3), nullable=True)
    profile_image_key = Column(VARCHAR(1000), nullable=True)
    profile_image_updated_at = Column(DATETIME, nullable=True)
    is_weekend_off = Column(BOOLEAN, default=False)
    # 추가
    work_shifts = Column(JSON, nullable=True, default=list, server_default="[]")
    # 원티드 설정 (간호사별 개별 설정)
    enable_nurse_pair_preference = Column(
        BOOLEAN, nullable=True, default=True
    )  # 시크릿 기능 활성화
    enable_aide = Column(BOOLEAN, nullable=True, default=True)  # AIDE 기능 활성화
    wanted_max_requests = Column(
        INTEGER, nullable=True
    )  # 원티드 요청 개수 제한 (휴무/휴가)
    # 그룹 관리자(HN) 권한 구분 — 'HN' 또는 null
    hn_auth = Column(VARCHAR(3), nullable=True)

    group = relationship("Group")
    __table_args__ = (
        ForeignKeyConstraint(
            ["group_id", "team_id"],
            ["teams.group_id", "teams.team_id"],
            name="fk_nurses_team_group",
            ondelete="SET NULL",
            onupdate="CASCADE",
        ),
    )
    # teams와의 조인 키를 명시 (group_id, team_id)
    team = relationship(
        "Team",
        primaryjoin="and_(Nurse.group_id==Team.group_id, Nurse.team_id==Team.team_id)",
        overlaps="group",
    )

    # office_id는 컬럼으로 관리


class NurseAssignment(Base):
    """간호사 배정/상태 변경 이력 (파견/휴직/퇴사/프리셉티/병동이동)"""
    __tablename__ = "nurse_assignment"
    id = Column(INTEGER, primary_key=True, autoincrement=True)
    nurse_id = Column(VARCHAR(50), ForeignKey("nurses.nurse_id"), nullable=False)
    source_group_id = Column(VARCHAR(50), ForeignKey("groups.group_id"), nullable=False)
    target_group_id = Column(VARCHAR(50), ForeignKey("groups.group_id"), nullable=True)
    office_id = Column(VARCHAR(50), ForeignKey("offices.office_id"), nullable=False)
    start_date = Column(DATE, nullable=False)
    expected_end_date = Column(DATE, nullable=True)
    end_date = Column(DATE, nullable=True)
    reason = Column(NVARCHAR(200), nullable=False)
    status = Column(VARCHAR(10), nullable=False, default="active")
    created_at = Column(DATETIME, default=func.now())
    updated_at = Column(DATETIME, default=func.now(), onupdate=func.now())

    nurse = relationship("Nurse", foreign_keys=[nurse_id])
    source_group = relationship("Group", foreign_keys=[source_group_id])
    target_group = relationship("Group", foreign_keys=[target_group_id])


class ShiftTransferLog(Base):
    """마감 시 파견/병동이동 shift 전달 이력"""
    __tablename__ = "shift_transfer_logs"
    id = Column(INTEGER, primary_key=True, autoincrement=True)
    schedule_id = Column(CHAR(12), nullable=False)
    target_schedule_id = Column(CHAR(12), nullable=True)
    assignment_id = Column(INTEGER, nullable=False)
    nurse_id = Column(VARCHAR(50), nullable=False)
    source_group_id = Column(VARCHAR(50), nullable=False)
    target_group_id = Column(VARCHAR(50), nullable=False)
    transfer_start = Column(DATE, nullable=False)
    transfer_end = Column(DATE, nullable=False)
    entry_count = Column(INTEGER, nullable=False, default=0)
    year = Column(SMALLINT, nullable=False)
    month = Column(TINYINT, nullable=False)
    transferred_at = Column(DATETIME, nullable=False, default=func.now())


class Schedule(Base):
    __tablename__ = "schedules"
    schedule_id = Column(CHAR(12), primary_key=True)
    office_id = Column(VARCHAR(50), ForeignKey("offices.office_id"))
    group_id = Column(VARCHAR(50), ForeignKey("groups.group_id"))
    year = Column(SMALLINT, nullable=False)
    month = Column(TINYINT, nullable=False)
    version = Column(BIGINT, nullable=False)
    config_id = Column(INTEGER, ForeignKey("roster_config.config_id"))
    created_by = Column(VARCHAR(50), ForeignKey("nurses.account_id"))
    created_at = Column(DATETIME, default=func.now())
    updated_at = Column(DATETIME, default=func.now(), onupdate=func.now())
    status = Column(VARCHAR(10))  # e.g., 'requested', 'issued'
    dropped = Column(BOOLEAN, nullable=False, default=False)
    name = Column(VARCHAR(50))
    # violations = Column(JSON, nullable=True) # 임시로 주석 처리 - DB 스키마 업데이트 후 활성화 예정
    memo = Column(TEXT, nullable=True)

    roster_config = relationship("RosterConfig")


class ScheduleEntry(Base):
    __tablename__ = "schedule_entries"
    entry_id = Column(VARCHAR(16), primary_key=True)
    schedule_id = Column(CHAR(12), ForeignKey("schedules.schedule_id"))
    nurse_id = Column(VARCHAR(50), ForeignKey("nurses.nurse_id"))
    work_date = Column(DATETIME, nullable=False)
    shift_id = Column(VARCHAR(10), ForeignKey("shifts.shift_id"))  # D, E, N, O, etc.
    id = Column(INTEGER, nullable=True)  # shifts.id (stable key)


class Shift(Base):
    __tablename__ = "shifts"
    shift_id = Column(VARCHAR(10), primary_key=True)
    office_id = Column(VARCHAR(50), ForeignKey("offices.office_id"))
    group_id = Column(VARCHAR(50), ForeignKey("groups.group_id"))
    name = Column(VARCHAR(20), nullable=False)
    color = Column(VARCHAR(10), nullable=False)
    shift_gb = Column(VARCHAR(5), nullable=True)
    # start_time = Column(VARCHAR(5), nullable=True)  # HH:MM format
    start_time = Column(Time, nullable=True)  # TIME 타입
    # end_time = Column(VARCHAR(5), nullable=True)    # HH:MM format
    end_time = Column(Time, nullable=True)  # TIME 타입
    type = Column(VARCHAR(10), nullable=False, default="근무")  # 'work' or 'off'
    allday = Column(INTEGER, nullable=False, default=0)
    auto_schedule = Column(INTEGER, nullable=False, default=1)
    # time_type = Column(VARCHAR(10), nullable=False, default='range')  # 'range', 'allday', 'hours'
    duration = Column(INTEGER, nullable=True)  # for time_type='hours'
    sequence = Column(INTEGER, nullable=False, default=0)  # 순서 관리용
    default_shift = Column(VARCHAR(10), nullable=True)  # 기본 근무코드
    is_weekly_off = Column(TINYINT, nullable=False, default=0)  # 주휴 여부
    id = Column(INTEGER, primary_key=True, nullable=False, autoincrement=True)
    # DB에서 BIT 타입이라 BOOLEAN으로 매핑
    show_in_preference = Column(
        BOOLEAN,
        nullable=False,
        server_default="0",  # MSSQL에서 0 (False)
        default=False,
    )

    office = relationship("Office")
    group = relationship("Group")


# ───────────────────────────── Job Status ─────────────────────────────


class RosterJob(Base):
    __tablename__ = "roster_jobs"

    job_id = Column(VARCHAR(100), primary_key=True)
    office_id = Column(VARCHAR(50), nullable=True)
    group_id = Column(VARCHAR(50), nullable=True)
    nurse_id = Column(VARCHAR(50), nullable=True)
    status = Column(VARCHAR(20), nullable=False)  # QUEUED | RUNNING | SUCCESS | FAILED
    progress = Column(SMALLINT, nullable=True)
    result_roster_id = Column(VARCHAR(100), nullable=True)
    error_message = Column(TEXT, nullable=True)
    created_at = Column(DATETIME, default=func.now())
    updated_at = Column(DATETIME, default=func.now(), onupdate=func.now())

    __table_args__ = (Index("idx_roster_jobs_group_created", "group_id", "created_at"),)


class ShiftManage(Base):
    __tablename__ = "shift_manage"
    # ── 복합 PRIMARY KEY ──────────────────────────────
    office_id = Column(VARCHAR(50), ForeignKey("offices.office_id"), primary_key=True)
    group_id = Column(VARCHAR(50), ForeignKey("groups.group_id"), primary_key=True)
    nurse_class = Column(VARCHAR(10), nullable=False)  # 'RN', 'AN', '보조'
    shift_slot = Column(
        INTEGER, nullable=False, primary_key=True
    )  # 슬롯 번호 (1, 2, 3...)
    main_code = Column(VARCHAR(10), nullable=True)  # 메인 근무코드 (하나만)
    codes = Column(JSON, nullable=True)  # 근무코드 리스트 ['D', 'E', 'N']
    # config_version = Column(VARCHAR(20), primary_key=True)
    manpower = Column(INTEGER, nullable=False, default=0)  # 인력 수

    office = relationship("Office")
    group = relationship("Group")


class ShiftPreference(Base):
    __tablename__ = "shift_preferences"
    nurse_id = Column(VARCHAR(50), ForeignKey("nurses.nurse_id"), primary_key=True)
    year = Column(SMALLINT, primary_key=True)
    month = Column(TINYINT, primary_key=True)
    created_at = Column(DATETIME, primary_key=True)
    data = Column(JSON, nullable=False)
    is_submitted = Column(BOOLEAN, nullable=False, default=False)
    submitted_at = Column(DATETIME, nullable=True)

    # # 복합 인덱스 추가 (성능 향상)
    # __table_args__ = (
    #     Index('idx_nurse_year_month_created', 'nurse_id', 'year', 'month', 'created_at'),
    # )


class RosterConfig(Base):
    __tablename__ = "roster_config"
    config_id = Column(INTEGER, primary_key=True, autoincrement=True)
    config_version = Column(VARCHAR(20))
    office_id = Column(VARCHAR(50), ForeignKey("offices.office_id"))
    group_id = Column(VARCHAR(50), ForeignKey("groups.group_id"))
    day_req = Column(INTEGER)
    eve_req = Column(INTEGER)
    nig_req = Column(INTEGER)
    min_exp_per_shift = Column(INTEGER)
    req_exp_nurses = Column(INTEGER)
    two_offs_per_week = Column(BOOLEAN)
    max_nig_per_month = Column(INTEGER)
    three_seq_nig = Column(BOOLEAN)
    two_offs_after_three_nig = Column(BOOLEAN)
    two_offs_after_two_nig = Column(BOOLEAN)
    banned_day_after_eve = Column(BOOLEAN)
    max_conseq_work = Column(INTEGER)
    off_days = Column(INTEGER)
    shift_priority = Column(FLOAT)
    weekend_shift_ratio = Column(FLOAT)
    patient_amount = Column(INTEGER)
    sequential_offs = Column(BOOLEAN)
    even_nights = Column(BOOLEAN)
    nod_noe = Column(BOOLEAN)
    not_one_night = Column(BOOLEAN, nullable=False, default=False)
    use_mid = Column(BOOLEAN, nullable=False, default=False)
    created_at = Column(DATETIME, default=func.now())
    preceptor_gauge = Column(INTEGER, nullable=False, default=5)
    preceptee_on = Column(BOOLEAN, nullable=False, default=False)
    preceptee_shift_count = Column(BOOLEAN, nullable=False, default=True)
    weekly_off_group = Column(BOOLEAN)
    team_balance_enable = Column(BOOLEAN, nullable=False, default=False)
    team_balance_gauge = Column(INTEGER, nullable=False, default=0)
    team_balance_mode = Column(VARCHAR(20), nullable=False, default="balanced")
    off_placement_mode = Column(INTEGER, nullable=False, default=0)
    fixed_wanted_use_yn = Column(BOOLEAN, nullable=False, default=False)
    show_level = Column(BOOLEAN, nullable=False, default=True)
    show_preceptor = Column(BOOLEAN, nullable=False, default=True)
    office = relationship("Office")
    group = relationship("Group")


class RosterGradeConfig(Base):
    __tablename__ = "roster_grade_config"

    config_id = Column(INTEGER, primary_key=True, autoincrement=True)
    office_id = Column(VARCHAR(50), nullable=False)
    group_id = Column(VARCHAR(50), nullable=False)
    null_grade_policy = Column(VARCHAR(20), nullable=False, default="LOWEST")
    constraints_json = Column(JSON, nullable=True)
    use_dynamic_scaling = Column(TINYINT, nullable=False, default=1)
    created_at = Column(DATETIME, default=func.now())
    updated_at = Column(DATETIME, default=func.now(), onupdate=func.now())
    updated_by = Column(VARCHAR(50), nullable=True)

    __table_args__ = (
        UniqueConstraint("office_id", "group_id", name="ux_grade_config_office_group"),
    )


class Wanted(Base):
    __tablename__ = "wanted"
    group_id = Column(VARCHAR(50), ForeignKey("groups.group_id"), primary_key=True)
    year = Column(SMALLINT, primary_key=True)
    month = Column(TINYINT, primary_key=True)
    exp_date = Column(DATETIME, nullable=True)  # 마감일
    status = Column(VARCHAR(10), default="requested")  # requested, closed
    created_at = Column(DATETIME, default=func.now())

    group = relationship("Group")


class WantedConfig(Base):
    """일자별 원티드 제한 설정 (DAILY_LIMIT 전용)

    - GLOBAL, NURSE_LIMIT 설정은 nurses 테이블로 이동됨
    - 이 테이블은 특정 일자의 특정 근무타입에 대한 제한만 관리
    """

    __tablename__ = "wanted_config"

    config_id = Column(INTEGER, primary_key=True, autoincrement=True)
    group_id = Column(VARCHAR(50), ForeignKey("groups.group_id"), nullable=False)
    year = Column(SMALLINT, nullable=True)
    month = Column(TINYINT, nullable=True)
    max_requests = Column(INTEGER, nullable=True)  # 해당 일자의 최대 요청 개수
    target_date = Column(DATE, nullable=True)  # 특정 일자
    shift_type = Column(CHAR(1), nullable=True)  # 근무 타입 (휴무/휴가)

    created_at = Column(DATETIME, default=func.now())
    updated_at = Column(DATETIME, default=func.now(), onupdate=func.now())

    group = relationship("Group")

    __table_args__ = (
        Index(
            "idx_wanted_config_daily",
            "group_id",
            "target_date",
            "shift_type",
            unique=True,
        ),
    )


class IssuedRoster(Base):
    __tablename__ = "issued_roster"
    # 기존: seq_no 단일 PK → 변경: (office_id, group_id, version) 복합 PK
    seq_no = Column(INTEGER, nullable=False)  # PK 아님, 순번 용도로만 사용
    office_id = Column(
        VARCHAR(50), ForeignKey("offices.office_id"), primary_key=True, nullable=False
    )
    group_id = Column(
        VARCHAR(50), ForeignKey("groups.group_id"), primary_key=True, nullable=False
    )
    nurse_id = Column(
        VARCHAR(50), ForeignKey("nurses.nurse_id"), nullable=False
    )  # 발행한 사람
    issued_at = Column(DATETIME, default=func.now())
    version = Column(BIGINT, primary_key=True, nullable=False)
    v_name = Column(VARCHAR(100), nullable=True)  # 버전 명
    issue_cmmt = Column(VARCHAR(500), nullable=True)  # 발행 코멘트
    schedule_id = Column(CHAR(12), ForeignKey("schedules.schedule_id"), nullable=False)
    is_active = Column(BOOLEAN, nullable=False, default=True)  # 발행 취소 시 False

    office = relationship("Office")
    group = relationship("Group")
    nurse = relationship("Nurse")
    schedule = relationship("Schedule")


class IssuedRosterSnapshot(Base):
    __tablename__ = "issued_roster_snapshot"

    snapshot_id = Column(INTEGER, primary_key=True, autoincrement=True)
    office_id = Column(VARCHAR(50), ForeignKey("offices.office_id"), nullable=False)
    group_id = Column(VARCHAR(50), ForeignKey("groups.group_id"), nullable=False)
    year = Column(SMALLINT, nullable=True)
    month = Column(TINYINT, nullable=True)
    schedule_id = Column(CHAR(12), ForeignKey("schedules.schedule_id"), nullable=False)
    version = Column(BIGINT, nullable=False)
    created_at = Column(DATETIME, default=func.now())
    is_active_issued = Column(BOOLEAN, nullable=False, default=True)

    meta_json = Column(JSON, nullable=True)
    config_json = Column(JSON, nullable=True)
    nurses_json = Column(JSON, nullable=True)
    shifts_json = Column(JSON, nullable=True)
    shift_manage_json = Column(JSON, nullable=True)
    roster_json = Column(JSON, nullable=True)
    violations_json = Column(JSON, nullable=True)

    office = relationship("Office")
    group = relationship("Group")
    schedule = relationship("Schedule")


class RosterAnalytics(Base):
    __tablename__ = "roster_analytics"
    analytics_id = Column(INTEGER, primary_key=True, autoincrement=True)
    schedule_id = Column(CHAR(12), ForeignKey("schedules.schedule_id"), nullable=False)
    nurse_id = Column(VARCHAR(50), ForeignKey("nurses.nurse_id"), nullable=False)
    year = Column(SMALLINT, nullable=False)
    month = Column(TINYINT, nullable=False)

    # 개인별 만족도 지표
    off_satisfaction = Column(FLOAT, nullable=False, default=0.0)
    shift_satisfaction = Column(FLOAT, nullable=False, default=0.0)
    pair_satisfaction = Column(FLOAT, nullable=False, default=0.0)
    overall_satisfaction = Column(FLOAT, nullable=False, default=0.0)

    # 요청 통계
    total_requests = Column(INTEGER, nullable=False, default=0)
    satisfied_requests = Column(INTEGER, nullable=False, default=0)
    off_requests = Column(INTEGER, nullable=False, default=0)
    satisfied_off_requests = Column(INTEGER, nullable=False, default=0)
    shift_requests = Column(INTEGER, nullable=False, default=0)
    satisfied_shift_requests = Column(INTEGER, nullable=False, default=0)
    pair_requests = Column(INTEGER, nullable=False, default=0)
    satisfied_pair_requests = Column(INTEGER, nullable=False, default=0)

    # 생성 시간
    created_at = Column(DATETIME, default=func.now())

    # 관계 설정
    schedule = relationship("Schedule")
    nurse = relationship("Nurse")


class RosterRequestDetails(Base):
    __tablename__ = "roster_request_details"
    detail_id = Column(INTEGER, primary_key=True, autoincrement=True)
    analytics_id = Column(
        INTEGER, ForeignKey("roster_analytics.analytics_id"), nullable=False
    )
    nurse_id = Column(VARCHAR(50), ForeignKey("nurses.nurse_id"), nullable=False)
    day = Column(INTEGER, nullable=False)
    request_type = Column(VARCHAR(20), nullable=False)  # 'off', 'shift', 'pair'
    shift_type = Column(VARCHAR(10), nullable=True)  # 'D', 'E', 'N' (shift 요청의 경우)
    pair_type = Column(
        VARCHAR(20), nullable=True
    )  # 'work_together', 'work_apart' (pair 요청의 경우)
    nurse_2_id = Column(
        VARCHAR(50), nullable=True
    )  # pair 요청의 경우 (외래키 제약조건 제거)
    satisfied = Column(BOOLEAN, nullable=False, default=False)
    preference_score = Column(FLOAT, nullable=False, default=0.0)

    # 생성 시간
    created_at = Column(DATETIME, default=func.now())

    # 관계 설정
    analytics = relationship("RosterAnalytics")
    nurse = relationship("Nurse", foreign_keys=[nurse_id])
    # nurse_2 관계는 필요시에만 사용하도록 주석 처리
    # nurse_2 = relationship("Nurse", foreign_keys=[nurse_2_id])


class WantedRequest(Base):
    __tablename__ = "wanted_requests"
    nurse_id = Column(VARCHAR(50), primary_key=True)
    request_id = Column(INTEGER, primary_key=True)
    month = Column(CHAR(7), primary_key=True)  # 'YYYY-MM'
    request = Column(TEXT, nullable=True)
    is_submitted = Column(TINYINT(1), nullable=False, default=0)
    created_at = Column(DATETIME, nullable=False, default=func.now())
    submitted_at = Column(DATETIME, nullable=True)


class NurseShiftRequest(Base):
    __tablename__ = "nurse_shift_requests"
    nurse_id = Column(VARCHAR(50), primary_key=True)
    request_id = Column(INTEGER, primary_key=True)
    detailed_request_id = Column(INTEGER, primary_key=True)
    shift_date = Column(DATE, primary_key=True)
    shift = Column(CHAR(1), nullable=False)  # 'D','E','N','O'
    shifts_table_id = Column(INTEGER, nullable=True)  # shifts.id (stable key)
    score = Column(DECIMAL(3, 1), nullable=False)
    partial_request = Column(TEXT, nullable=True)
    # 사유작성
    comment = Column(TEXT, nullable=True)


class NursePairRequest(Base):
    __tablename__ = "nurse_pair_requests"
    nurse_id = Column(VARCHAR(50), primary_key=True)
    request_id = Column(INTEGER, primary_key=True)
    month = Column(CHAR(7), primary_key=True)  # 'YYYY-MM'
    detailed_request_id = Column(INTEGER, primary_key=True)
    target_id = Column(VARCHAR(50), primary_key=True)
    score = Column(DECIMAL(3, 1), nullable=False)
    partial_request = Column(TEXT, nullable=True)


class DailyShift(Base):
    __tablename__ = "daily_shift"

    office_id = Column(VARCHAR(50), ForeignKey("offices.office_id"), primary_key=True)
    group_id = Column(VARCHAR(50), ForeignKey("groups.group_id"), primary_key=True)
    year = Column(SMALLINT, primary_key=True)
    month = Column(TINYINT, primary_key=True)
    day = Column(TINYINT, primary_key=True)
    d_count = Column(SMALLINT, nullable=False, default=0)
    e_count = Column(SMALLINT, nullable=False, default=0)
    n_count = Column(SMALLINT, nullable=False, default=0)
    m_count = Column(SMALLINT, nullable=False, default=0)
    d_count_max = Column(SMALLINT, nullable=False, default=0)
    e_count_max = Column(SMALLINT, nullable=False, default=0)
    n_count_max = Column(SMALLINT, nullable=False, default=0)
    m_count_max = Column(SMALLINT, nullable=False, default=0)
    created_at = Column(DATETIME, default=func.now())
    updated_at = Column(DATETIME, default=func.now(), onupdate=func.now())

    office = relationship("Office")
    group = relationship("Group")


class DeletedNurseHistory(Base):
    """간호사 삭제 이력 테이블 – 삭제된 간호사의 기초 정보와 삭제 수행자를 기록"""

    __tablename__ = "deleted_nurse_history"

    id = Column(INTEGER, primary_key=True, autoincrement=True)

    # 삭제된 간호사 기초 정보
    target_nurse_id = Column(VARCHAR(50), nullable=False)
    office_id = Column(VARCHAR(50), nullable=True)
    group_id = Column(VARCHAR(50), nullable=True)
    emp_num = Column(VARCHAR(50), nullable=True)
    account_id = Column(VARCHAR(50), nullable=False)
    name = Column(VARCHAR(50), nullable=False)
    role = Column(VARCHAR(20), nullable=True)
    experience = Column(SMALLINT, nullable=True)
    is_head_nurse = Column(BOOLEAN, nullable=True)
    joining_date = Column(DATETIME, nullable=True)
    birth_date = Column(VARCHAR(10), nullable=True)
    phone_number = Column(VARCHAR(20), nullable=True)
    gender = Column(VARCHAR(3), nullable=True)

    # 삭제 수행자 정보
    deleted_by_nurse_id = Column(VARCHAR(50), nullable=False)
    deleted_by_account_id = Column(VARCHAR(50), nullable=False)
    deleted_by_name = Column(VARCHAR(50), nullable=True)
    deleted_by_role = Column(VARCHAR(3), nullable=True)  # ADM / HDN

    # 삭제 시각
    deleted_at = Column(DATETIME, nullable=False, default=func.now())


class WeeklyOffSetting(Base):
    __tablename__ = "weekly_off_settings"

    id = Column(INTEGER, primary_key=True, autoincrement=True)
    office_id = Column(VARCHAR(50), ForeignKey("offices.office_id"), nullable=False)
    group_id = Column(VARCHAR(50), ForeignKey("groups.group_id"), nullable=False)

    activate = Column(TINYINT, nullable=False, default=0)
    use_variable_cycle = Column(TINYINT, nullable=False, default=0)
    cycle_type = Column(
        VARCHAR(10), nullable=True, default="month"
    )  # 'month' or 'week'

    # 기준 시점 (설정 변경 시점의 연/월)
    base_year = Column(INTEGER, nullable=True)
    base_month = Column(INTEGER, nullable=True)

    # 주 단위 주기 기준일
    cycle_start_date = Column(DATE, nullable=True)

    cycle_interval = Column(INTEGER, nullable=True, default=1)
    shift_variation = Column(SMALLINT, nullable=False, default=-1)

    created_at = Column(DATETIME, default=func.now())
    updated_at = Column(DATETIME, default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint(
            "office_id", "group_id", name="ux_weekly_off_settings_office_group"
        ),
    )

    office = relationship("Office")
    group = relationship("Group")


# Fixed Wanted (확정 원티드)
class FixedWantedEntry(Base):
    """확정 원티드 테이블 - 수간호사가 조정/확정한 간호사별, 날짜별 근무 희망 (단일 테이블 구조)"""

    __tablename__ = "fixed_wanted_entries"

    id = Column(INTEGER, primary_key=True, autoincrement=True)
    group_id = Column(VARCHAR(50), ForeignKey("groups.group_id"), nullable=False)
    year = Column(SMALLINT, nullable=False)
    month = Column(TINYINT, nullable=False)
    nurse_id = Column(VARCHAR(50), ForeignKey("nurses.nurse_id"), nullable=False)
    shift_date = Column(DATE, nullable=False)
    shift_id = Column(NVARCHAR(10), nullable=False)  # 근무코드 (D, E, N, O 등)
    shifts_table_id = Column(INTEGER, nullable=True)  # shifts.id (stable key)
    is_applied = Column(BOOLEAN, default=True)  # 적용/미적용 여부
    source_type = Column(
        VARCHAR(20), nullable=False
    )  # 'original' | 'added' | 'modified'
    original_shift_id = Column(
        NVARCHAR(10), nullable=True
    )  # 원본 근무코드 (수정된 경우)
    reason = Column(TEXT, nullable=True)  # 사유 (원본에서 복사 또는 신규 입력)
    head_nurse_memo = Column(
        TEXT, nullable=True
    )  # 수간호사 메모 (반려 사유, 조정 코멘트 등)
    created_by = Column(
        VARCHAR(50), ForeignKey("nurses.nurse_id"), nullable=True
    )  # 생성자
    created_at = Column(DATETIME, default=func.now())
    updated_at = Column(DATETIME, default=func.now(), onupdate=func.now())

    group = relationship("Group")
    nurse = relationship("Nurse", foreign_keys=[nurse_id])
    creator = relationship("Nurse", foreign_keys=[created_by])

    __table_args__ = (
        Index("idx_fixed_entry_group_ym", "group_id", "year", "month"),
        Index(
            "idx_fixed_entry_nurse_date",
            "group_id",
            "year",
            "month",
            "nurse_id",
            "shift_date",
        ),
    )


class Notice(Base):
    __tablename__ = 'notices'
    id = Column(INTEGER, primary_key=True, autoincrement=True)
    title = Column(VARCHAR(200), nullable=False)
    content = Column(TEXT, nullable=False)
    author_id = Column(VARCHAR(50), nullable=False)    # 작성자 account_id
    author_name = Column(VARCHAR(100), nullable=False)
    is_pinned = Column(BOOLEAN, default=False)
    created_at = Column(DATETIME, default=func.now())
    updated_at = Column(DATETIME, default=func.now(), onupdate=func.now())


class ShareLink(Base):
    __tablename__ = "share_links"

    token = Column(VARCHAR(64), primary_key=True)
    schedule_id = Column(CHAR(12), ForeignKey("schedules.schedule_id"), nullable=False)
    office_id = Column(VARCHAR(50), ForeignKey("offices.office_id"), nullable=False)
    group_id = Column(VARCHAR(50), ForeignKey("groups.group_id"), nullable=False)
    image_url = Column(VARCHAR(1000), nullable=False)
    title = Column(NVARCHAR(200), nullable=True)
    description = Column(NVARCHAR(1000), nullable=True)
    created_by_nurse_id = Column(
        VARCHAR(50), ForeignKey("nurses.nurse_id"), nullable=True
    )
    expires_at = Column(DATETIME, nullable=False)
    revoked_at = Column(DATETIME, nullable=True)
    created_at = Column(DATETIME, default=func.now())
    updated_at = Column(DATETIME, default=func.now(), onupdate=func.now())

    schedule = relationship("Schedule")
    office = relationship("Office")
    group = relationship("Group")
    creator = relationship("Nurse", foreign_keys=[created_by_nurse_id])


class Message(Base):
    __tablename__ = "messages"

    id = Column(INTEGER, primary_key=True, autoincrement=True)
    office_id = Column(VARCHAR(50), ForeignKey("offices.office_id"), nullable=False)
    sender_nurse_id = Column(VARCHAR(50), ForeignKey("nurses.nurse_id"), nullable=False)
    receiver_nurse_id = Column(VARCHAR(50), ForeignKey("nurses.nurse_id"), nullable=False)
    message = Column(NVARCHAR(2000), nullable=True)
    message_img = Column(VARCHAR(50), nullable=True)
    is_read = Column(BOOLEAN, default=False, nullable=False)
    created_at = Column(DATETIME, default=func.now(), nullable=False)
    read_at = Column(DATETIME, nullable=True)

    sender = relationship("Nurse", foreign_keys=[sender_nurse_id])
    receiver = relationship("Nurse", foreign_keys=[receiver_nurse_id])
