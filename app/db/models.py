from sqlalchemy import Column, VARCHAR, SMALLINT, BOOLEAN, DATETIME, func, ForeignKey, JSON, CHAR, INTEGER, FLOAT, Index, ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.dialects.mysql import TINYINT 
from sqlalchemy.orm import relationship
from db.client2 import Base
from sqlalchemy import DATE, DECIMAL, TEXT, Time

class Group(Base):
    __tablename__ = 'groups'
    group_id = Column(VARCHAR(50), primary_key=True)
    office_id = Column(VARCHAR(50), ForeignKey('offices.office_id'))
    group_name = Column(VARCHAR(50), nullable=False)
    # office = relationship("Office", back_populates="groups") 
class Office(Base):
    __tablename__ = 'offices'
    office_id = Column(VARCHAR(50), primary_key=True)
    office_name = Column(VARCHAR(100), nullable=False)
    # address = Column(VARCHAR(255))
    # contact_number = Column(VARCHAR(30))
    # groups = relationship("Group", back_populates="office") 

class Team(Base):
    __tablename__ = 'teams'
    office_id = Column(VARCHAR(50), ForeignKey('offices.office_id'), primary_key=True)
    group_id = Column(VARCHAR(50), ForeignKey('groups.group_id'), primary_key=True)
    team_id = Column(INTEGER, primary_key=True)  # 그룹 내 로컬 식별자
    team_name = Column(VARCHAR(100), nullable=False)
    active = Column(TINYINT, nullable=False, default=1)
    created_at = Column(DATETIME, default=func.now())
    updated_at = Column(DATETIME, default=func.now(), onupdate=func.now())

    office = relationship("Office")
    group = relationship("Group")
    __table_args__ = (
        Index('ux_teams_group_name', 'group_id', 'team_name', unique=True),
        UniqueConstraint('group_id', 'team_id', name='ux_teams_group_teamid'),
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
    emp_auth_gbn = Column(VARCHAR(3), name='EmpAuthGbn', nullable=True)
    is_night_nurse = Column(JSON, nullable=True, default=list)
    personal_off_adjustment = Column(TINYINT, default=0)
    preceptor_id = Column(VARCHAR(50), ForeignKey("nurses.nurse_id"))
    joining_date = Column(DATETIME, nullable=True)
    created_at = Column(DATETIME, default=func.now())
    updated_at = Column(DATETIME, default=func.now(), onupdate=func.now())
    resignation_date = Column(DATETIME, nullable=True)
    # 화면 표시 및 알고리즘 입력 순서 제어용
    sequence = Column(INTEGER, nullable=False, default=0)
    active = Column(INTEGER, default=1)
    team_id = Column(INTEGER, nullable=True)
    # 주휴 관련 추가 컬럼
    weekly_off_enabled = Column(TINYINT, default=0)  # 주휴 대상 여부
    weekly_off_weekday = Column(TINYINT, nullable=True)  # 기준 월에서의 주휴 요일 (0:월~6:일)
    # 고정 근무 코드(해당 병동의 shifts.shift_id)
    fixed_shift = Column(VARCHAR(20), nullable=True)
    nurse_memo = Column(TEXT, nullable=True)
    grade = Column(INTEGER, nullable=True)
    # 사이드 프로필 관련 추가 컬럼
    birth_date = Column(VARCHAR(10), nullable=True)
    phone_number = Column(VARCHAR(20), nullable=True)
    gender = Column(VARCHAR(3), nullable=True)
    is_weekend_off = Column(BOOLEAN, default=False)
    # 추가
    work_shifts = Column(JSON, nullable=True, default=list, server_default='[]')
    
    group = relationship("Group")
    __table_args__ = (
        ForeignKeyConstraint(['group_id', 'team_id'], ['teams.group_id', 'teams.team_id'], name='fk_nurses_team_group', ondelete='SET NULL', onupdate='CASCADE'),
    )
    # teams와의 조인 키를 명시 (group_id, team_id)
    team = relationship("Team", primaryjoin="and_(Nurse.group_id==Team.group_id, Nurse.team_id==Team.team_id)", overlaps="group")

    # office_id는 컬럼으로 관리
class Schedule(Base):
    __tablename__ = "schedules"
    schedule_id = Column(CHAR(12), primary_key=True)
    office_id = Column(VARCHAR(50), ForeignKey("offices.office_id"))
    group_id = Column(VARCHAR(50), ForeignKey("groups.group_id"))
    year = Column(SMALLINT, nullable=False)
    month = Column(TINYINT, nullable=False)
    version = Column(TINYINT, nullable=False)
    config_id = Column(INTEGER, ForeignKey("roster_config.config_id"))
    created_by = Column(VARCHAR(50), ForeignKey("nurses.account_id"))
    created_at = Column(DATETIME, default=func.now())
    updated_at = Column(DATETIME, default=func.now(), onupdate=func.now())
    status = Column(VARCHAR(10)) # e.g., 'requested', 'issued'
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
    shift_id = Column(VARCHAR(10), ForeignKey("shifts.shift_id")) # D, E, N, O, etc.

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
    end_time = Column(Time, nullable=True)    # TIME 타입
    type = Column(VARCHAR(10), nullable=False, default='근무')  # 'work' or 'off'
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
        server_default="0" , # MSSQL에서 0 (False)
        default=False
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

    __table_args__ = (
        Index("idx_roster_jobs_group_created", "group_id", "created_at"),
    )

class ShiftManage(Base):
    __tablename__ = "shift_manage"
    # ── 복합 PRIMARY KEY ──────────────────────────────
    office_id  = Column(VARCHAR(50), ForeignKey("offices.office_id"), primary_key=True)
    group_id   = Column(VARCHAR(50), ForeignKey("groups.group_id"), primary_key=True)
    nurse_class = Column(VARCHAR(10), nullable=False)  # 'RN', 'AN', '보조'
    shift_slot = Column(INTEGER, nullable=False, primary_key=True)  # 슬롯 번호 (1, 2, 3...)
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
    __tablename__ = 'roster_config'
    config_id = Column(INTEGER, primary_key=True, autoincrement=True)
    config_version = Column(VARCHAR(20))
    office_id = Column(VARCHAR(50), ForeignKey('offices.office_id'))
    group_id = Column(VARCHAR(50), ForeignKey('groups.group_id'))
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
    created_at = Column(DATETIME, default=func.now())
    preceptor_gauge = Column(INTEGER, nullable=False, default=5)
    weekly_off_group = Column(BOOLEAN)
    team_balance_enable = Column(BOOLEAN, nullable=False, default=False)
    team_balance_gauge = Column(INTEGER, nullable=False, default=0)
    team_balance_mode = Column(VARCHAR(20), nullable=False, default="balanced")
    off_placement_mode = Column(INTEGER, nullable=False, default=0)

    office = relationship("Office")
    group = relationship("Group")


class RosterGradeConfig(Base):
    __tablename__ = 'roster_grade_config'

    config_id = Column(INTEGER, primary_key=True, autoincrement=True)
    office_id = Column(VARCHAR(50), nullable=False)
    group_id = Column(VARCHAR(50), nullable=False)
    null_grade_policy = Column(VARCHAR(20), nullable=False, default='LOWEST')
    constraints_json = Column(JSON, nullable=True)
    use_dynamic_scaling = Column(TINYINT, nullable=False, default=1)
    created_at = Column(DATETIME, default=func.now())
    updated_at = Column(DATETIME, default=func.now(), onupdate=func.now())
    updated_by = Column(VARCHAR(50), nullable=True)

    __table_args__ = (
        UniqueConstraint('office_id', 'group_id', name='ux_grade_config_office_group'),
    )

class Wanted(Base):
    __tablename__ = 'wanted'
    group_id = Column(VARCHAR(50), ForeignKey('groups.group_id'), primary_key=True)
    year = Column(SMALLINT, primary_key=True)
    month = Column(TINYINT, primary_key=True)
    exp_date = Column(DATETIME, nullable=True)  # 마감일
    status = Column(VARCHAR(10), default='requested')  # requested, closed
    created_at = Column(DATETIME, default=func.now())

    group = relationship("Group")

class WantedConfig(Base):
    __tablename__ = 'wanted_config'

    config_id = Column(INTEGER, primary_key=True, autoincrement=True)
    group_id = Column(VARCHAR(50), ForeignKey('groups.group_id'), nullable=False)
    config_type = Column(VARCHAR(20), nullable=False)  # 'GLOBAL', 'NURSE_LIMIT', 'DAILY_LIMIT'

    # 간호사별 제한 (NURSE_LIMIT)
    nurse_id = Column(VARCHAR(50), ForeignKey('nurses.nurse_id'), nullable=True)
    year = Column(SMALLINT, nullable=True)
    month = Column(TINYINT, nullable=True)
    max_requests = Column(INTEGER, nullable=True)

    # 일자별 제한 (DAILY_LIMIT)
    target_date = Column(DATE, nullable=True)
    shift_type = Column(CHAR(1), nullable=True)

    # 전역 설정 (GLOBAL)
    # 사용하지 않지만 유지하는 컬럼 (향후 확장용)
    default_deadline_days = Column(INTEGER, nullable=True)
    allow_edit_after_submit = Column(BOOLEAN, nullable=True, default=True)
    auto_close_on_deadline = Column(BOOLEAN, nullable=True, default=True)
    send_reminder = Column(BOOLEAN, nullable=True, default=True)
    reminder_days_before = Column(INTEGER, nullable=True, default=3)
    # 실제 사용하는 컬럼
    enable_nurse_pair_preference = Column(BOOLEAN, nullable=True, default=True)
    enable_aide = Column(BOOLEAN, nullable=True, default=True)

    created_at = Column(DATETIME, default=func.now())
    updated_at = Column(DATETIME, default=func.now(), onupdate=func.now())

    group = relationship("Group")
    nurse = relationship("Nurse")

    __table_args__ = (
        Index('idx_wanted_config_global', 'group_id', 'config_type', unique=True),
        Index('idx_wanted_config_nurse', 'group_id', 'nurse_id', 'year', 'month', unique=True),
        Index('idx_wanted_config_daily', 'group_id', 'target_date', 'shift_type', unique=True),
    )

class IssuedRoster(Base):
    __tablename__ = 'issued_roster'
    # 기존: seq_no 단일 PK → 변경: (office_id, group_id, version) 복합 PK
    seq_no = Column(INTEGER, nullable=False)  # PK 아님, 순번 용도로만 사용
    office_id = Column(VARCHAR(50), ForeignKey('offices.office_id'), primary_key=True, nullable=False)
    group_id = Column(VARCHAR(50), ForeignKey('groups.group_id'), primary_key=True, nullable=False)
    nurse_id = Column(VARCHAR(50), ForeignKey('nurses.nurse_id'), nullable=False)  # 발행한 사람
    issued_at = Column(DATETIME, default=func.now())
    version = Column(TINYINT, primary_key=True, nullable=False)
    v_name = Column(VARCHAR(100), nullable=True)  # 버전 명
    issue_cmmt = Column(VARCHAR(500), nullable=True)  # 발행 코멘트
    schedule_id = Column(CHAR(12), ForeignKey('schedules.schedule_id'), nullable=False)
    
    office = relationship("Office")
    group = relationship("Group")
    nurse = relationship("Nurse")
    schedule = relationship("Schedule") 


class IssuedRosterSnapshot(Base):
    __tablename__ = 'issued_roster_snapshot'

    snapshot_id = Column(INTEGER, primary_key=True, autoincrement=True)
    office_id = Column(VARCHAR(50), ForeignKey('offices.office_id'), nullable=False)
    group_id = Column(VARCHAR(50), ForeignKey('groups.group_id'), nullable=False)
    year = Column(SMALLINT, nullable=True)
    month = Column(TINYINT, nullable=True)
    schedule_id = Column(CHAR(12), ForeignKey('schedules.schedule_id'), nullable=False)
    version = Column(TINYINT, nullable=False)
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
    __tablename__ = 'roster_analytics'
    analytics_id = Column(INTEGER, primary_key=True, autoincrement=True)
    schedule_id = Column(CHAR(12), ForeignKey('schedules.schedule_id'), nullable=False)
    nurse_id = Column(VARCHAR(50), ForeignKey('nurses.nurse_id'), nullable=False)
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
    __tablename__ = 'roster_request_details'
    detail_id = Column(INTEGER, primary_key=True, autoincrement=True)
    analytics_id = Column(INTEGER, ForeignKey('roster_analytics.analytics_id'), nullable=False)
    nurse_id = Column(VARCHAR(50), ForeignKey('nurses.nurse_id'), nullable=False)
    day = Column(INTEGER, nullable=False)
    request_type = Column(VARCHAR(20), nullable=False)  # 'off', 'shift', 'pair'
    shift_type = Column(VARCHAR(10), nullable=True)  # 'D', 'E', 'N' (shift 요청의 경우)
    pair_type = Column(VARCHAR(20), nullable=True)  # 'work_together', 'work_apart' (pair 요청의 경우)
    nurse_2_id = Column(VARCHAR(50), nullable=True)  # pair 요청의 경우 (외래키 제약조건 제거)
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
    __tablename__ = 'wanted_requests'
    nurse_id = Column(VARCHAR(50), primary_key=True)
    request_id = Column(INTEGER, primary_key=True)
    month = Column(CHAR(7), primary_key=True)  # 'YYYY-MM'
    request = Column(TEXT, nullable=True)
    is_submitted = Column(TINYINT(1), nullable=False, default=0)
    created_at = Column(DATETIME, nullable=False, default=func.now())
    submitted_at = Column(DATETIME, nullable=True)


class NurseShiftRequest(Base):
    __tablename__ = 'nurse_shift_requests'
    nurse_id = Column(VARCHAR(50), primary_key=True)
    request_id = Column(INTEGER, primary_key=True)
    detailed_request_id = Column(INTEGER, primary_key=True)
    shift_date = Column(DATE, primary_key=True)
    shift = Column(CHAR(1), nullable=False)  # 'D','E','N','O'
    score = Column(DECIMAL(3, 1), nullable=False)
    partial_request = Column(TEXT, nullable=True)


class NursePairRequest(Base):
    __tablename__ = 'nurse_pair_requests'
    nurse_id = Column(VARCHAR(50), primary_key=True)
    request_id = Column(INTEGER, primary_key=True)
    detailed_request_id = Column(INTEGER, primary_key=True)
    target_id = Column(VARCHAR(50), primary_key=True)
    score = Column(DECIMAL(3, 1), nullable=False)
    partial_request = Column(TEXT, nullable=True)
class DailyShift(Base):
    __tablename__ = 'daily_shift'

    office_id = Column(VARCHAR(50), ForeignKey('offices.office_id'), primary_key=True)
    group_id = Column(VARCHAR(50), ForeignKey('groups.group_id'), primary_key=True)
    year = Column(SMALLINT, primary_key=True)
    month = Column(TINYINT, primary_key=True)
    day = Column(TINYINT, primary_key=True)
    d_count = Column(SMALLINT, nullable=False, default=0)
    e_count = Column(SMALLINT, nullable=False, default=0)
    n_count = Column(SMALLINT, nullable=False, default=0)
    created_at = Column(DATETIME, default=func.now())
    updated_at = Column(DATETIME, default=func.now(), onupdate=func.now())

    office = relationship("Office")
    group = relationship("Group")


class WeeklyOffSetting(Base):
    __tablename__ = 'weekly_off_settings'
    
    id = Column(INTEGER, primary_key=True, autoincrement=True)
    office_id = Column(VARCHAR(50), ForeignKey('offices.office_id'), nullable=False)
    group_id = Column(VARCHAR(50), ForeignKey('groups.group_id'), nullable=False)
    
    activate = Column(TINYINT, nullable=False, default=0)
    use_variable_cycle = Column(TINYINT, nullable=False, default=0)
    cycle_type = Column(VARCHAR(10), nullable=True, default='month')  # 'month' or 'week'
    
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
        UniqueConstraint('office_id', 'group_id', name='ux_weekly_off_settings_office_group'),
    )

    office = relationship("Office")
    group = relationship("Group")

