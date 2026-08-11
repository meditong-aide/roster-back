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
from sqlalchemy import DATE, DECIMAL, TEXT, Time, text
from datetime import datetime


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
    # 팀별 일일 최소 시프트 커버리지. 예: {"D":1,"E":1,"N":0,"M":0}
    min_shift = Column(JSON, nullable=True)
    # 팀 내 인계 제한 정책. 예:
    # {"restrictions": [{"grades":[6,7,8], "block_same_shift":true, "block_adjacent":true}]}
    # 미래 확장: {"from":[..], "to":[..], "bidirectional":bool} 규칙도 같은 배열에 추가 가능.
    handoff_policy = Column(JSON, nullable=True)
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
    # 허용 근무형 리스트(JSON). 물리 컬럼명은 is_night_nurse 유지(공유 DB·타 브랜치 호환) —
    # 코드/Python 속성만 allowed_shifts 로 수렴. 물리 rename 은 전 브랜치 머지 후 별도 마이그레이션.
    allowed_shifts = Column(JSON, name="is_night_nurse", key="allowed_shifts", nullable=True, default=list)
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
    # is_weekend_off: ORM 매핑 제거(2026-07-22). SSOT = nurse_weekendoff_period.
    #   물리 컬럼은 당분간 DROP 하지 않되(요청), ORM 이 SELECT/read 하지 않도록 언매핑.
    #   읽기는 services.nurse_period_resolver.is_weekend_off_asof / weekend_off_ids_asof 사용.
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


class NurseTeamPeriod(Base):
    """team 시점 구간 (병동귀속). effective-dated [valid_from, valid_to).

    nurses.team_id 는 '현재값' 캐시이고, 월별/기간별 team 의 진실은 이 테이블이다.
    - 변경 = close-before-open(옛 구간 valid_to 닫고 새 구간 open), 삭제 금지(완전 타임라인).
    - 겹침 금지, gap(미지정) 허용. valid_to=null 은 열린(계속) 구간.
    - 폴백은 ward-aware: 구간 없으면 nurses.group_id==group 일 때만 nurses.team_id, 아니면 None.
    참조: docs/TEMPORAL_NURSE_MODEL_DESIGN.md §2.3·§4.6 (v3).
    """

    __tablename__ = "nurse_team_period"

    id = Column(INTEGER, primary_key=True, autoincrement=True)
    nurse_id = Column(VARCHAR(50), ForeignKey("nurses.nurse_id"), nullable=False)
    group_id = Column(VARCHAR(50), ForeignKey("groups.group_id"), nullable=False)
    valid_from = Column(DATE, nullable=False)
    valid_to = Column(DATE, nullable=True)   # null = 열린(계속) 구간
    # team_id 는 (group_id, team_id)->teams 이지만 DB레벨 복합 FK 는 마이그레이션에서 결정.
    team_id = Column(INTEGER, nullable=True)
    source = Column(VARCHAR(20), nullable=False, default="edited")  # inherited|edited|redistribute
    note = Column(TEXT, nullable=True)

    created_at = Column(DATETIME, default=func.now())
    updated_at = Column(DATETIME, default=func.now(), onupdate=func.now())

    nurse = relationship("Nurse", foreign_keys=[nurse_id])
    group = relationship("Group", foreign_keys=[group_id])

    __table_args__ = (
        Index("ix_ntp_nurse", "nurse_id", "valid_from"),
        Index("ix_ntp_group", "group_id", "valid_from"),
        # 한 간호사는 한 병동에서 같은 시작일로 두 구간을 가질 수 없다(close-before-open
        #   타임라인 불변식). set_team_period 의 upsert 키와 동일 → 과거 더블인서트로 생긴
        #   완전중복 행(완전 동일 행 2개)을 DB 레벨에서 영구 차단.
        UniqueConstraint(
            "nurse_id",
            "group_id",
            "valid_from",
            name="uq_ntp_nurse_group_from",
        ),
    )


class NurseMonthlyLimit(Base):
    """월별/그룹별 간호사 개인 근무 개수 제한.

    - nurses 는 정적 기본 프로필을 유지하고,
    - 월별 변동 제한은 이 테이블에서 관리한다.
    """

    __tablename__ = "nurse_monthly_limits"

    id = Column(INTEGER, primary_key=True, autoincrement=True)
    nurse_id = Column(VARCHAR(50), ForeignKey("nurses.nurse_id"), nullable=False)
    group_id = Column(VARCHAR(50), ForeignKey("groups.group_id"), nullable=False)
    year = Column(SMALLINT, nullable=False)
    month = Column(TINYINT, nullable=False)

    d_min = Column(INTEGER, nullable=True)
    d_max = Column(INTEGER, nullable=True)
    d_exact = Column(INTEGER, nullable=True)

    e_min = Column(INTEGER, nullable=True)
    e_max = Column(INTEGER, nullable=True)
    e_exact = Column(INTEGER, nullable=True)

    n_min = Column(INTEGER, nullable=True)
    n_max = Column(INTEGER, nullable=True)
    n_exact = Column(INTEGER, nullable=True)

    o_min = Column(INTEGER, nullable=True)
    o_max = Column(INTEGER, nullable=True)
    o_exact = Column(INTEGER, nullable=True)

    created_at = Column(DATETIME, default=func.now())
    updated_at = Column(DATETIME, default=func.now(), onupdate=func.now())

    nurse = relationship("Nurse", foreign_keys=[nurse_id])
    group = relationship("Group", foreign_keys=[group_id])

    __table_args__ = (
        UniqueConstraint(
            "nurse_id", "group_id", "year", "month", name="ux_nurse_monthly_limits_scope"
        ),
    )


class NurseNightCycle(Base):
    """수면OFF 판정을 위한 N 연번 앵커 — **월별 스냅샷**.

    ★ effective-dated period 가 **아니다.** valid_from~valid_to · close-before-open ·
      as-of 같은 개념이 없다. 위 `NurseMonthlyLimit` 과 같은 per-nurse × 월 스코프이며
      근무표 확정 시 그 달 1행을 upsert 한다.

    ★ 왜 저장이 필요한가 — `schedule_entries.shift_id` 에는 'N' 만 저장되고
      N1~N15 연번은 없다. 근무표만으로는 "15 에 도달했는가"를 알 수 없다
      (실측: 연번 없이 판정하면 중환자실 20명 전원이 후보로 나온다).

    ★ `night_count` / `sleep_off_given` 같은 집계 컬럼은 두지 않는다 —
      schedule_entries 에서 언제든 계산되므로 중복 저장할 이유가 없다.

    설계: docs/leave_auto_assignment_design.md §5.2
    """

    __tablename__ = "nurse_night_cycle"

    id = Column(INTEGER, primary_key=True, autoincrement=True)
    nurse_id = Column(VARCHAR(50), nullable=False)
    group_id = Column(VARCHAR(50), nullable=False)
    year = Column(SMALLINT, nullable=False)
    month = Column(TINYINT, nullable=False)
    # 그 달 마지막 N 의 연번 = 다음 달 시작점.
    seq_at_end = Column(INTEGER, nullable=True)
    # 이월된 미부여 수면OFF 수.
    #   ★ seq_at_end 만으로는 이월을 표현할 수 없다 (중환자실 유희주·이재영 실증):
    #     8/30 N15 → 8/31 N1 시작 → seq_at_end=1 이지만 pending_sleep=1
    pending_sleep = Column(INTEGER, nullable=True)
    # 그 달 수면OFF 부여 횟수 (보통 0 또는 1).
    sleep_off_count = Column(INTEGER, nullable=True)
    # 그 달 말 기준 **누적 회차** = 전월 sleep_off_seq + sleep_off_count.
    #   ★ 집계 컬럼 금지 원칙의 예외다. night_count 는 그 달만 세면 나오지만
    #     누적 회차는 전 기간을 훑어야 하므로 seq_at_end 와 같은 층위의 상태값이다.
    #   ★ 근무환경 지표로 쓴다 — 실측(2026-07~08): 수면 1회 수령자의 2개월 N 평균
    #     10.6회 vs 미수령자 5.8회. 수령 빈도가 곧 나이트 부담이다.
    sleep_off_seq = Column(INTEGER, nullable=True)
    created_at = Column(DATETIME, default=func.now())
    updated_at = Column(DATETIME, default=func.now(), onupdate=func.now())

    # ★ FK 는 걸지 않는다(DDL 최소화 방침). 정합은 앱단에서 유지.
    __table_args__ = (
        UniqueConstraint(
            "nurse_id", "group_id", "year", "month", name="ux_nurse_night_cycle_scope"
        ),
    )


class EffectiveDatedPeriodMixin:
    """시점 속성(effective-dated) 공통 컬럼.

    규칙: `[valid_from, valid_to)` 반열림 · 겹침 금지 · gap(미지정) 허용 ·
    변경=close-before-open(옛 구간 valid_to 닫고 새 구간 open, 삭제 금지).
    진실=이 테이블, `nurses` 캐시 컬럼=단방향 투영(앱 직접쓰기 금지).
    설계: docs/NURSE_ATTRIBUTE_PERIOD_DESIGN.md.
    """

    id = Column(INTEGER, primary_key=True, autoincrement=True)
    nurse_id = Column(VARCHAR(50), ForeignKey("nurses.nurse_id"), nullable=False)
    valid_from = Column(DATE, nullable=False)
    valid_to = Column(DATE, nullable=True)            # null = 열린(계속) 구간
    source = Column(VARCHAR(20), nullable=False, default="edited")  # inherited|edited|redistribute
    note = Column(TEXT, nullable=True)
    created_at = Column(DATETIME, default=func.now())
    updated_at = Column(DATETIME, default=func.now(), onupdate=func.now())


class NurseGradePeriod(Base, EffectiveDatedPeriodMixin):
    """grade 시점 구간 (병동귀속). 해석 스케일=roster_grade_config."""

    __tablename__ = "nurse_grade_period"
    group_id = Column(VARCHAR(50), ForeignKey("groups.group_id"), nullable=False)
    grade = Column(INTEGER, nullable=True)
    __table_args__ = (
        Index("ix_ngp_nurse", "nurse_id", "valid_from"),
        Index("ix_ngp_group", "group_id", "valid_from"),
    )


class NurseAllowedShiftPeriod(Base, EffectiveDatedPeriodMixin):
    """근무형(shift-form) 시점 구간 (간호사귀속).

    한 row 가 (allowed_shifts, fixed_shift) 쌍을 함께 담는다 — 둘은 결합 속성:
    고정 간호사의 allowed 는 사실상 {fixed_code} 이고 같이 변하므로 한 satellite 로 통합.
    - allowed_shifts: 허용 근무형 집합. ["D"]/["D","E"]/["N"]/[](제한없음).
    - fixed_shift: 값이 있으면 그 코드로 '고정'(솔버 우회·평일=코드/주말=OFF). NULL=일반 스케줄.
    한쪽만 바꿔도 다른쪽은 직전 구간값 carry-forward (upsert_period carry_attrs).
    """

    __tablename__ = "nurse_allowed_shift_period"
    # default=list: fixed_shift 만 설정해 새 구간 생성 시(carry 대상 allowed 부재) []로 채움
    # ([] = 제한없음. 고정 간호사는 솔버 우회라 안전한 기본값).
    allowed_shifts = Column(JSON, nullable=False, default=list)
    fixed_shift = Column(VARCHAR(20), nullable=True)
    __table_args__ = (Index("ix_nasp_nurse", "nurse_id", "valid_from"),)


class NurseWeekendOffPeriod(Base, EffectiveDatedPeriodMixin):
    """주말휴무 시점 구간 (간호사귀속)."""

    __tablename__ = "nurse_weekendoff_period"
    weekend_off = Column(TINYINT, nullable=True)
    __table_args__ = (Index("ix_nwop_nurse", "nurse_id", "valid_from"),)


class NurseLeavePeriod(Base, EffectiveDatedPeriodMixin):
    """휴가 자동부여 대상 시점 구간 (간호사귀속). 보건휴가 · 수면OFF.

    ★ 3-state 다 — `NULL`=자동판정 · `0`=제외 · `1`=강제포함. **NULL 이 유효값**이므로
      NOT NULL/DEFAULT 0 을 걸면 안 된다(전원 제외가 된다).
    ★ **행이 없으면 전부 자동판정 = 도입 전 동작 그대로**라 백필하지 않는다. 예외만 넣는다.
    ★ `pregnant` 는 정책이 아니라 사실이다. 보건휴가 자동판정의 입력값
      (엑셀 실측: 산전 17건 중 보건 수령 0건 / 산전 없음 521건 중 413건 79%).

    ★★ `nurse_allowed_shift_period` 에 컬럼을 더하지 않고 분리한 이유 —
      그 테이블은 "설 수 있는 근무형"만 담고 두 컬럼이 **같이 변해서** 한 행에 있다.
      휴가 대상 여부는 인사이동과 무관하게 바뀌므로 결합 근거가 없고, 합치면
      `upsert_period(carry_attrs=...)` 호출부 10곳이 전부 승계 목록을 갱신해야 한다
      (누락 시 근무형만 바꿔도 휴가설정이 조용히 NULL 로 덮인다 — 이 테이블에서
       실제로 났던 사고: 응급실-RN 송혜영·윤나리 fixed_shift 소실).
    """

    __tablename__ = "nurse_leave_period"
    # DB 는 BIT · 매핑은 BOOLEAN (이 파일의 기존 관행, Shift.health_leave_target 과 동일).
    # nullable=True 가 핵심 — 3-state 의 NULL 을 살린다.
    health_leave_eligible = Column(BOOLEAN, nullable=True)
    sleep_off_eligible = Column(BOOLEAN, nullable=True)
    pregnant = Column(BOOLEAN, nullable=True)
    __table_args__ = (Index("ix_nlp_nurse", "nurse_id", "valid_from"),)


class NursePrecepteePeriod(Base, EffectiveDatedPeriodMixin):
    """프리셉터↔프리셉티 관계 시점 구간 (SSOT).

    한 row = (프리셉티 nurse_id) 가 [valid_from, valid_to) 동안 preceptor_id 를 따른다.
    - WHO = preceptor_id, WHEN = [valid_from, valid_to). valid_to=계획종료일(무기한이면 NULL).
    - 진실 = 이 테이블. nurses.preceptor_id = as-of 오늘 단방향 투영(앱 직접쓰기 금지).
    - 1:1: 한 간호사는 동시점 최대 1개 open 구간 (filtered unique).
    - end_reason: 종료 사유(expired|cancelled|released|preceptor_transfer) — 알림/감사용.
    설계: docs/NURSE_PRECEPTEE_PERIOD_DESIGN.md.
    """

    __tablename__ = "nurse_preceptee_period"
    preceptor_id = Column(VARCHAR(50), ForeignKey("nurses.nurse_id"), nullable=False)
    office_id = Column(VARCHAR(50), ForeignKey("offices.office_id"), nullable=False)
    end_reason = Column(VARCHAR(30), nullable=True)
    source_assignment_id = Column(INTEGER, nullable=True)  # write-through 추적(감사)
    __table_args__ = (
        Index("ix_npp_nurse", "nurse_id", "valid_from"),
        Index("ix_npp_preceptor", "preceptor_id", "valid_to"),
        # 1:1: 동시점 open(=valid_to NULL) 구간은 간호사당 1개. 양 dialect partial unique.
        Index(
            "uq_npp_open_per_nurse", "nurse_id", unique=True,
            mssql_where=text("valid_to IS NULL"),
            sqlite_where=text("valid_to IS NULL"),
        ),
    )


class NurseMutualExclusionPeriod(Base, EffectiveDatedPeriodMixin):
    """상호 근무 배제 관계 시점 구간 (SSOT).

    한 row = (nurse_id) 가 [valid_from, valid_to) 동안 partner_id 와 **같은 날 같은
    근무조에 배정되지 않도록** 솔버가 소프트 회피한다(프리셉티↔프리셉터 '함께근무'의 배반적 개념).
    - WHO = partner_id, WHEN = [valid_from, valid_to). valid_to=종료(무기한이면 NULL=지속).
    - 1:1: 한 간호사는 동시점 최대 1개 open 구간(한사람당 한명씩). **양방향 저장**(A→B, B→A).
    - end_reason: 종료 사유(released|cancelled|replaced) — 감사용.
    - **하드 아님**: 솔버 stage3 objective 소프트 페널티. grade/team 하드가 lexicographic 상위라 항상 우선.
    설계: NursePrecepteePeriod(프리셉티 SSOT) 패턴 미러.
    """

    __tablename__ = "nurse_mutual_exclusion_period"
    partner_id = Column(VARCHAR(50), ForeignKey("nurses.nurse_id"), nullable=False)
    office_id = Column(VARCHAR(50), ForeignKey("offices.office_id"), nullable=False)
    end_reason = Column(VARCHAR(30), nullable=True)
    __table_args__ = (
        # 1:1: 동시점 open(=valid_to NULL) 구간은 간호사당 1개("한사람당 한명씩"). 양 dialect partial unique.
        # 일반 인덱스는 생략 — 배제쌍은 소수라 테이블이 작고, as-of 조회도 이 필터 유니크로 충분.
        Index(
            "uq_nmep_open_per_nurse", "nurse_id", unique=True,
            mssql_where=text("valid_to IS NULL"),
            sqlite_where=text("valid_to IS NULL"),
        ),
    )


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
    # kind: reason(한글) 기반 명시적 분류 (DDL Phase 1.4). DB DEFAULT 'transfer'.
    kind = Column(VARCHAR(30), nullable=False, server_default="transfer")
    # payload: 영구속성 변경 등 신규 케이스용 JSON (Phase 2에서 사용 시작, 그 전엔 NULL)
    payload = Column(JSON(none_as_null=True), nullable=True)
    status = Column(VARCHAR(10), nullable=False, default="active")
    note = Column(NVARCHAR(1000), nullable=True)
    # target 그룹 전용 설정
    target_weekly_off_type = Column(VARCHAR(20), nullable=True)
    target_weekly_off_enabled = Column(TINYINT, nullable=True)
    target_weekly_off_weekday = Column(TINYINT, nullable=True)
    target_shift_types = Column(JSON(none_as_null=True), nullable=True, default=list)
    target_team_id = Column(INTEGER, nullable=True)
    target_grade = Column(INTEGER, nullable=True)
    target_fixed_shift = Column(VARCHAR(20), nullable=True)
    target_wanted_max_requests = Column(INTEGER, nullable=True)
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
    status = Column(VARCHAR(10))  # e.g., 'draft', 'issued'
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
    off_swap_target = Column(BOOLEAN, nullable=False, default=False)
    # 보건휴가 부여 대상 코드 표식. 그룹당 1건만 True (앱단 검증).
    #   ★ off_swap_target 과 컬럼 관례만 같고 동작은 무관하다.
    #     off_swap  = 생성 후 후처리로 초과 OFF 를 연차로 변환 (OFF 총량 유지)
    #     보건휴가   = 생성 전 사전 주입으로 근무일을 대체 (OFF 쿼터 미소비)
    #   코드명이 병동마다 '보건'/'보건휴가' 로 갈려 name 매칭이 불가해 표식으로 지목한다.
    health_leave_target = Column(BOOLEAN, nullable=False, default=False)
    # 수면OFF 부여 대상 코드 표식. 그룹당 1건만 True (앱단 검증).
    #   보건휴가와 같은 관례. 코드가 없는 그룹(응급실-AN)은 켤 행이 없어 자동 미사용.
    sleep_off_target = Column(BOOLEAN, nullable=False, default=False)
    # 근무코드 설명(자유 텍스트). MSSQL NVARCHAR(MAX) = NVARCHAR(-1).
    description = Column(NVARCHAR(None), nullable=True)

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
    # 실DB 정합: 실제 PK 는 id(IDENTITY) 단독이다. 모델이 (office,group,slot) 복합 PK 로
    # 선언돼 있던 탓에 ORM identity-map 이 중복행을 같은 PK 로 병합해 비결정적으로 동작했다.
    # 유일성은 아래 UniqueConstraint 로 보장한다. (slot 1=D,2=E,3=N,4=OFF(응답 합성),5=M)
    # 실DB는 bigint IDENTITY 지만, 모델 타입은 읽기/테스트(SQLite) 호환을 위해 INTEGER 로 둔다
    # (테이블은 이미 존재 → 모델로 생성하지 않음. SQLite autoincrement 는 INTEGER PK 만 지원).
    id = Column(INTEGER, primary_key=True, autoincrement=True)
    office_id = Column(VARCHAR(50), ForeignKey("offices.office_id"), nullable=False)
    group_id = Column(VARCHAR(50), ForeignKey("groups.group_id"), nullable=False)
    nurse_class = Column(NVARCHAR(16), nullable=False)  # 'RN', 'AN', '보조'
    shift_slot = Column(INTEGER, nullable=False)  # 슬롯 번호
    main_code = Column(NVARCHAR(10), nullable=False)  # 메인 근무코드 (하나만)
    codes = Column(JSON, nullable=False)  # 근무코드(shift_id) 리스트 ['D', 'E', 'N']
    manpower = Column(INTEGER, nullable=False, default=0)  # 인력 수
    # 실DB 존재 컬럼(현재 전부 NULL=미사용, 정합용으로만 선언). per-month 전환 시 활용.
    year = Column(SMALLINT, nullable=True)
    month = Column(INTEGER, nullable=True)
    config_version = Column(VARCHAR(20), nullable=True)
    created_at = Column(DATETIME, nullable=True)
    updated_at = Column(DATETIME, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "office_id",
            "group_id",
            "nurse_class",
            "shift_slot",
            name="UQ_shift_manage_slot",
        ),
    )

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
    # 연속 OFF 최대 개수(soft 상한). NULL=앱 기본 3 적용. (k+1)연속 OFF 고weight 벌점, 불가피 시 양보.
    max_conseq_off = Column(INTEGER, nullable=True)
    shift_priority = Column(FLOAT)
    sequential_offs = Column(BOOLEAN)
    nod_noe = Column(BOOLEAN)
    not_one_night = Column(BOOLEAN, nullable=False, default=False)
    use_mid = Column(BOOLEAN, nullable=False, default=False)
    created_at = Column(DATETIME, default=func.now())
    preceptee_on = Column(BOOLEAN, nullable=False, default=False)
    preceptee_shift_count = Column(BOOLEAN, nullable=False, default=True)
    weekly_off_group = Column(BOOLEAN)
    fixed_wanted_use_yn = Column(BOOLEAN, nullable=False, default=False)
    # ban_night_before_fixed_off: ORM 매핑 제거(DDL DROP 대상). 컬럼이 아니라 solver 기본값으로 존속 —
    #   roster_config.py 의 dataclass 기본값(True) + cp_sat_basic create_config_from_db 의
    #   config_data.get('ban_night_before_fixed_off', True) 가 컬럼 부재 시 동일 동작 보장(prod 전량 True).
    #   재추가 금지: FE 미노출·probe/ontology 전용 상수-live 레버라 컬럼 저장 불필요.
    show_level = Column(BOOLEAN, nullable=False, default=True)
    show_preceptor = Column(BOOLEAN, nullable=False, default=True)
    off_first = Column(BOOLEAN, nullable=False, default=False)
    off_swap_enabled = Column(BOOLEAN, nullable=False, default=False)
    # ── 보건휴가 자동 부여 ──
    #   NULL = 미설정(= 꺼짐). 기존 row 를 건드리지 않으려 nullable 로 둔다.
    #   판정은 항상 bool(getattr(cfg, ..., False)) — None 이 False 로 떨어져야 한다.
    health_leave_enabled = Column(BOOLEAN, nullable=True, default=None)
    # 주말 배치 허용. NULL/False = 평일만.
    #   2026-08 실측 주말 비율: 41-RN 34% · 별관1 25% · 52-AN 22% / 그 외 0~5%
    health_leave_weekend = Column(BOOLEAN, nullable=True, default=None)
    # ── 수면OFF 자동 부여 ── (보건휴가와 동일한 NULL=미설정 규약)
    sleep_off_enabled = Column(BOOLEAN, nullable=True, default=None)
    # 트리거 주기(N 연번). 실측 15. NULL 이면 코드 기본값을 쓴다.
    sleep_off_cycle = Column(INTEGER, nullable=True, default=None)
    # ── 설정 프리셋 (저장한 설정 모달) ──
    # version: 그룹(office+group)별 0부터 시작하는 프리셋 버전.
    #   기능 이전(legacy) row 는 NULL → 프리셋 아님(목록 비노출). 신규 저장 및
    #   생성 materialize(="새로운 설정n") 는 항상 값을 부여한다(익명 row 없음).
    version = Column(INTEGER, nullable=True)
    config_name = Column(NVARCHAR(100), nullable=True)  # 프리셋 이름. '새로운 설정n' 자동분 포함
    config_memo = Column(NVARCHAR(500), nullable=True)  # 간단 메모
    # 마지막 저장 시각 — upsert(in-place 수정) 시 갱신. created_at 은 최초 생성 고정.
    updated_at = Column(DATETIME, default=func.now(), onupdate=func.now())
    office = relationship("Office")
    group = relationship("Group")
    # NOTE: 그룹별 version 유일성 인덱스(ux_roster_config_group_version,
    #   WHERE version IS NOT NULL 필터드 유니크)는 개발 마무리 후 추가 예정.
    #   현재는 version 할당이 MAX+1(앱) 단독 — 동시 저장 충돌은 실무상 희박해 보류.


class RosterGradeConfig(Base):
    __tablename__ = "roster_grade_config"

    config_id = Column(INTEGER, primary_key=True, autoincrement=True)
    office_id = Column(VARCHAR(50), nullable=False)
    group_id = Column(VARCHAR(50), nullable=False)
    null_grade_policy = Column(VARCHAR(20), nullable=False, default="LOWEST")
    constraints_json = Column(JSON, nullable=True)
    # anti-pair: shift별 grade 최대 인원. 예: {"N": {"1": 2}} → N 시프트에 grade 1을 최대 2명까지
    constraints_max_json = Column(JSON, nullable=True)
    grade_names_json = Column(JSON, nullable=True)
    default_shifts_json = Column(JSON, nullable=True, default=list, server_default="[]")
    use_dynamic_scaling = Column(TINYINT, nullable=False, default=1)
    allow_soft_fallback = Column(TINYINT, nullable=False, default=0)
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
    group_id = Column(VARCHAR(50), ForeignKey("groups.group_id"), nullable=False)
    request = Column(TEXT, nullable=True)
    is_submitted = Column(TINYINT(1), nullable=False, default=0)
    created_at = Column(DATETIME, nullable=False, default=func.now())
    submitted_at = Column(DATETIME, nullable=True)


class WantedMonthlyMemo(Base):
    """원티드 작성 화면의 월별 메모. 날짜·근무와 무관한 그 달 전체 메모다.

    ★ 왜 별도 테이블인가
      `wanted_requests` 는 월 헤더가 아니라 요청 단위 행이다(실측: 한 간호사·월에
      최대 198행). 거기에 컬럼을 붙이면 어느 행에 쓸지가 모호해진다.
    ★ 왜 별도 엔드포인트인가
      `POST /preferences` 는 저장 한 번에 BannedWantedEntry · NurseShiftRequest ·
      NursePairRequest 를 delete-then-insert 한다. 메모는 입력 중 디바운스로 자주
      저장되므로 그 경로를 타면 원티드가 통째로 지워질 위험이 크다.

    PK 가 (nurse_id, group_id, year, month) 라 월당 1행이고 upsert 가 단순하다.
    """

    __tablename__ = "wanted_monthly_memo"
    nurse_id = Column(VARCHAR(50), primary_key=True)
    group_id = Column(VARCHAR(50), primary_key=True)
    year = Column(SMALLINT, primary_key=True)
    month = Column(TINYINT, primary_key=True)
    # NULL = 메모 없음(삭제된 상태). 빈 문자열도 삭제로 정규화해 저장한다.
    memo = Column(TEXT, nullable=True)
    updated_at = Column(DATETIME, nullable=False, default=func.now())


class NurseShiftRequest(Base):
    __tablename__ = "nurse_shift_requests"
    nurse_id = Column(VARCHAR(50), primary_key=True)
    request_id = Column(INTEGER, primary_key=True)
    detailed_request_id = Column(INTEGER, primary_key=True)
    shift_date = Column(DATE, primary_key=True)
    group_id = Column(VARCHAR(50), ForeignKey("groups.group_id"), nullable=False)
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
    group_id = Column(VARCHAR(50), ForeignKey("groups.group_id"), nullable=False)
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
    max_enabled = Column(BOOLEAN, nullable=False, default=False)
    created_at = Column(DATETIME, default=func.now())
    updated_at = Column(DATETIME, default=func.now(), onupdate=func.now())

    office = relationship("Office")
    group = relationship("Group")


class DailyTeamShift(Base):
    """일자별 가동 팀 + 팀별 최소 인원.

    teams.min_shift 는 월 전체 고정이라 "주중엔 4개 팀, 주말엔 4·3·2팀만" 을
    표현할 수 없다. 이 테이블이 날짜마다 도는 팀을 지정한다.

    읽는 쪽 계약(세 줄이 전부):
        1) 그 날짜에 행이 하나라도 있으면 → 그 team_id 들만 가동. 나머지 팀
           인원은 그날 강제 OFF.
        2) 행이 하나도 없으면 → 미설정으로 보고 **전 팀 가동**(현행 유지).
           ★ 이 규칙이 없으면 기존 그룹이 매일 전원 OFF 가 된다.
           ★ 그래서 "그날 아무 팀도 안 돔"은 이 구조로 표현할 수 없다(행 0개가
             미설정과 같아진다). 저장 API 가 빈 목록을 거부하는 이유다.
        3) *_count 가 0 이면 인원 미지정 → 기존 min(need, 가동팀수) 규칙에 위임.
           양수면 그 팀이 그날 그 시프트에 최소 그만큼 서야 한다.
    """

    __tablename__ = "daily_team_shift"

    office_id = Column(VARCHAR(50), primary_key=True)
    group_id = Column(VARCHAR(50), primary_key=True)
    year = Column(SMALLINT, primary_key=True)
    month = Column(TINYINT, primary_key=True)
    day = Column(TINYINT, primary_key=True)
    team_id = Column(INTEGER, primary_key=True)
    d_count = Column(SMALLINT, nullable=False, default=0)
    e_count = Column(SMALLINT, nullable=False, default=0)
    n_count = Column(SMALLINT, nullable=False, default=0)
    m_count = Column(SMALLINT, nullable=False, default=0)
    created_at = Column(DATETIME, default=func.now())
    updated_at = Column(DATETIME, default=func.now(), onupdate=func.now())


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


# Banned Wanted (금지 원티드) — fixed_wanted 의 배반. 셀 단위로 배정 불가 근무(복수)를 배열로 저장.
class BannedWantedEntry(Base):
    """금지 원티드 테이블 — 셀(간호사·날짜)당 금지 근무코드 배열(D/E/N, 최대 2개).

    솔버에서는 initial_forbidden 으로 반영되어 해당 셀에 그 근무 배정이 금지된다.
    fixed_wanted 와 같은 셀에서 충돌 시 fixed 가 우선(솔버가 fixed 셀의 forbidden 을 skip).
    """

    __tablename__ = "banned_wanted_entries"

    id = Column(INTEGER, primary_key=True, autoincrement=True)
    group_id = Column(VARCHAR(50), ForeignKey("groups.group_id"), nullable=False)
    year = Column(SMALLINT, nullable=False)
    month = Column(TINYINT, nullable=False)
    nurse_id = Column(VARCHAR(50), ForeignKey("nurses.nurse_id"), nullable=False)
    shift_date = Column(DATE, nullable=False)
    # 금지 근무코드 배열(main code). 예: ["D","E"]. 1~2개.
    banned_shift_ids = Column(JSON, nullable=False, default=list)
    is_applied = Column(BOOLEAN, default=True)  # 적용/미적용 여부
    # 출처: 'hn'=수간호사 조정판 저장, 'nurse'=간호사 본인이 원티드 작성에서 넣은 기피근무.
    #   두 출처가 한 테이블에 섞이므로 스냅샷 replace 삭제 스코프를 이 값으로 가른다
    #   (안 가르면 HN 저장 1회에 간호사 기피근무가 통째로 삭제된다).
    source = Column(VARCHAR(10), nullable=False, server_default="hn", default="hn")
    reason = Column(TEXT, nullable=True)
    created_by = Column(VARCHAR(50), ForeignKey("nurses.nurse_id"), nullable=True)
    created_at = Column(DATETIME, default=func.now())
    updated_at = Column(DATETIME, default=func.now(), onupdate=func.now())

    group = relationship("Group")
    nurse = relationship("Nurse", foreign_keys=[nurse_id])
    creator = relationship("Nurse", foreign_keys=[created_by])

    __table_args__ = (
        Index("idx_banned_entry_group_ym", "group_id", "year", "month"),
        Index(
            "idx_banned_entry_nurse_date",
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


# ───────────────────────────── Agent Session Memory (US-A1) ─────────────────────────────


class AgentConversation(Base):
    """Agent 다중 대화 세션 — MSSQL SOT.

    Redis (sess:{group_id}:{session_id}:msgs / :vm) 가 hot cache,
    이 테이블이 source of truth.
    """

    __tablename__ = "agent_conversation"

    id = Column(INTEGER, primary_key=True, autoincrement=True)
    session_id = Column(VARCHAR(64), unique=True, nullable=False, index=True)
    user_id = Column(VARCHAR(64), nullable=False, index=True)
    group_id = Column(VARCHAR(64), nullable=False, index=True)
    # variable_memory JSON-직렬화 (skill 간 cross-turn state)
    vm_json = Column(TEXT, nullable=True)
    created_at = Column(DATETIME, default=func.now())
    last_active_at = Column(DATETIME, default=func.now(), onupdate=func.now())
    ttl_until = Column(DATETIME, nullable=True)


class AgentConversationMessage(Base):
    """Agent 대화 메시지 turn — MSSQL SOT.

    turn_idx 는 세션 내 0-based 순서.
    """

    __tablename__ = "agent_conversation_message"

    id = Column(INTEGER, primary_key=True, autoincrement=True)
    session_id = Column(
        VARCHAR(64),
        ForeignKey("agent_conversation.session_id"),
        nullable=False,
        index=True,
    )
    turn_idx = Column(INTEGER, nullable=False)
    role = Column(VARCHAR(32), nullable=False)  # system / user / assistant / tool
    content = Column(TEXT, nullable=True)
    tool_calls_json = Column(TEXT, nullable=True)
    created_at = Column(DATETIME, default=func.now())

    __table_args__ = (
        UniqueConstraint(
            "session_id", "turn_idx", name="ux_agent_conv_msg_session_turn"
        ),
    )


# ───────────────────────────── Agent User Memory (US-A3) ─────────────────────────────


class AgentUserMemory(Base):
    """Tier 2 user fact — cross-session에서 학습된 사용자 선호/사실 저장소.

    Zep 식 temporal validity: valid_to IS NULL = 현재 유효,
    non-NULL = 만료(이력 보존).
    group_id NOT NULL — D-1/D-2 audit 정책 (cross-group read/write 차단).
    """

    __tablename__ = "agent_user_memory"

    id = Column(INTEGER, primary_key=True, autoincrement=True)
    user_id = Column(VARCHAR(64), nullable=False, index=True)
    group_id = Column(VARCHAR(64), nullable=False, index=True)  # NOT NULL — D-1/D-2 audit 정책
    fact_type = Column(VARCHAR(64), nullable=False, index=True)  # 'nurse_pref' / 'shift_alias' / 'ward_pattern' 등
    fact_text = Column(TEXT, nullable=False)
    source = Column(VARCHAR(32), nullable=False)  # 'USER_STATED' | 'AGENT_INFERRED' | 'SYSTEM_DERIVED'
    valid_from = Column(DATETIME, default=func.now(), nullable=False)
    valid_to = Column(DATETIME, nullable=True)  # NULL = currently valid; non-NULL = expired/superseded
    confidence = Column(FLOAT, default=1.0)
    evidence_session_id = Column(VARCHAR(64), nullable=True)
    created_at = Column(DATETIME, default=func.now(), nullable=False)


# ───────────────────────── Agent Memory Audit (US-A5) ─────────────────────────────


class AgentMemoryAudit(Base):
    """의료 도메인 audit 요건 — 메모리 계층별 읽기/쓰기/변경/삭제 이력.

    action  : READ | WRITE | UPDATE | DELETE | EXPIRE
    tier    : SESSION | USER | PROCEDURAL
    row_id  : 영향 받은 row PK (해당하는 경우)
    who     : user_id 또는 agent identity
    why     : reason / context
    agent_run_id : evidence_session_id 등 추적 식별자
    timestamp    : UTC 기록 시각 (index for efficient audit queries)
    """

    __tablename__ = "agent_memory_audit"

    id = Column(INTEGER, primary_key=True, autoincrement=True)
    action = Column(VARCHAR(16), nullable=False)      # READ | WRITE | UPDATE | DELETE | EXPIRE
    tier = Column(VARCHAR(16), nullable=False)         # SESSION | USER | PROCEDURAL
    row_id = Column(INTEGER, nullable=True)            # 영향 받은 row PK (가능 시)
    who = Column(VARCHAR(64), nullable=False, index=True)  # user_id 또는 agent identity — 의료 audit 필수
    why = Column(TEXT, nullable=True)                  # reason / context
    agent_run_id = Column(VARCHAR(64), nullable=True)  # evidence_session_id 등
    timestamp = Column(
        DATETIME,
        default=datetime.utcnow,
        nullable=False,
        index=True,
    )


# ───────────────────────── Agent Skill Invocation Audit ───────────────────────


class AgentSkillInvocation(Base):
    """Agent skill 호출 audit — 의료 감사 + 디버깅 + RBAC 검증 용도.

    PRD 사용자 결정 ("tool history는 MSSQL audit으로 위임")의 구현.

    status:
      - SUCCESS              : skill 정상 실행
      - DENIED               : permission_check 차단 (RBAC)
      - CLARIFICATION_NEEDED : grounding 단계에서 clarification 요구
      - ERROR                : skill 내부 예외
      - BLOCKED              : 기타 차단 (정책 등)

    group_id NOT NULL — D-1/D-2 audit 정책 (cross-group 추적).
    args_json 은 PII 포함 가능 — 디버깅용 raw 저장.
    프로덕션 PII 보호 필요 시 column-level encryption 적용 권장.
    """

    __tablename__ = "agent_skill_invocation"

    id = Column(INTEGER, primary_key=True, autoincrement=True)
    agent_run_id = Column(VARCHAR(64), nullable=True, index=True)
    session_id = Column(VARCHAR(64), nullable=True, index=True)
    user_id = Column(VARCHAR(64), nullable=True, index=True)
    group_id = Column(VARCHAR(64), nullable=False, index=True)
    skill_name = Column(VARCHAR(64), nullable=False, index=True)
    args_json = Column(TEXT, nullable=True)
    status = Column(VARCHAR(32), nullable=False, index=True)
    error_message = Column(TEXT, nullable=True)
    latency_ms = Column(FLOAT, nullable=True)
    timestamp = Column(
        DATETIME,
        default=datetime.utcnow,
        nullable=False,
        index=True,
    )
