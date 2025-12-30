"""
근무표 생성 관련 서비스 로직 모듈
- DB 쿼리, 데이터 가공, 엔진 호출 등 라우터에서 분리
- 모든 함수는 한글 docstring, 한글 print/logging, PEP8 스타일 적용
"""
from __future__ import annotations

from sqlalchemy.orm import Session
from db.models import (
    DailyShift,
    Group,
    IssuedRoster,
    Nurse,
    NursePairRequest,
    NurseShiftRequest,
    RosterConfig,
    RosterGradeConfig,
    Schedule,
    ScheduleEntry,
    Shift,
    ShiftManage,
    ShiftPreference,
    Wanted,
    WantedRequest,
)
from schemas.roster_schema import RosterRequest
from routers.utils import get_days_in_month, Timer
from datetime import date, datetime, timedelta
import uuid
from sqlalchemy import func
from collections import defaultdict
import calendar
from sqlalchemy import text
from db.client2 import get_db
# from db.client2 import _get_mssql_session


# CP-SAT 기반 엔진들 import
try:
    from services.random_sampling import generate_roster
    from services.cp_sat_basic import generate_roster_cp_sat
    from services.cp_sat_main_v3 import generate_roster_cp_sat_main_v3
    from services.cp_sat_main_v2 import generate_roster_cp_sat_main_v2
    from services.cp_sat_adaptive import generate_roster_cp_sat_adaptive
    CPSAT_AVAILABLE = True
    CPSAT_MAIN_V3_AVAILABLE = True
    CPSAT_MAIN_V2_AVAILABLE = True
    CPSAT_ADAPTIVE_AVAILABLE = True
except ImportError as e:
    print(f"CP-SAT 엔진 import 실패: {e}")
    CPSAT_AVAILABLE = False
    CPSAT_MAIN_V3_AVAILABLE = False
    CPSAT_MAIN_V2_AVAILABLE = False
    CPSAT_ADAPTIVE_AVAILABLE = False

# ───────────────────────────── 공통 헬퍼 ─────────────────────────────

def _collect_nurses_and_preferences(db: Session, req, current_user):
    """그룹 내 간호사 목록과 선호도(제출본 우선)를 수집한다. (WantedRequest 기반)"""
    # 1️⃣ 그룹 내 간호사 목록
    nurses_in_group = (
        db.query(Nurse)
        .filter(Nurse.group_id == current_user.group_id)
        .order_by(Nurse.experience.desc(), Nurse.nurse_id.asc())
        .all()
    )

    nurse_ids = [n.nurse_id for n in nurses_in_group]
    month_str = f"{req.year}-{req.month:02d}"
    preferences = []

    # 2️⃣ 각 간호사별 submitted → draft 순으로 선호도 가져오기
    for nurse_id in nurse_ids:
        submitted_wr = (
            db.query(WantedRequest)
            .filter(
                WantedRequest.nurse_id == nurse_id,
                WantedRequest.month == month_str,
                WantedRequest.is_submitted == True
            )
            .order_by(WantedRequest.submitted_at.desc())
            .first()
        )

        target_wr = submitted_wr or (
            db.query(WantedRequest)
            .filter(
                WantedRequest.nurse_id == nurse_id,
                WantedRequest.month == month_str,
            )
            .order_by(WantedRequest.created_at.asc())
            .first()
        )
        if not target_wr:
            continue  # 기록이 없는 간호사는 건너뜀

        # 3️⃣ shift 데이터 수집
        shift_rows = (
            db.query(NurseShiftRequest)
            .filter(
                NurseShiftRequest.nurse_id == nurse_id,
                NurseShiftRequest.request_id == target_wr.request_id,
                # cast(NurseShiftRequest.shift_date, String).like(f"{month_str}-%"),
            )
            .all()
        )
        shift_data = {"D": {}, "E": {}, "N": {}, "O": {}}
        for s in shift_rows:
            shift_type = s.shift.upper()
            day = str(int(str(s.shift_date).split("-")[-1]))
            if shift_type in shift_data:
                shift_data[shift_type][day] = int(s.score) if s.score is not None else 0
        # 4️⃣ pair 데이터 수집
        pair_rows = (
            db.query(NursePairRequest)
            .filter(
                NursePairRequest.nurse_id == nurse_id,
                NursePairRequest.request_id == target_wr.request_id,
            )
            .all()
        )
        pair_data = [{"id": p.target_id, "weight": p.score} for p in pair_rows]

        # 5️⃣ data JSON 구성
        data_json = {
            "request": target_wr.request,
            "shift": {k: v for k, v in shift_data.items() if v},
            "preference": pair_data,
        }
        # 6️⃣ 기존 ShiftPreference 포맷으로 append
        preferences.append({
            "nurse_id": nurse_id,
            "year": req.year,
            "month": req.month,
            "is_submitted": bool(target_wr.is_submitted),
            "created_at": target_wr.created_at,
            "submitted_at": target_wr.submitted_at,
            "data": data_json,
        })
    # 7️⃣ 기존 함수와 동일하게 반환
    print("preferences", nurses_in_group, preferences)
    return nurses_in_group, preferences


# def _collect_nurses_and_preferences(db: Session, req: RosterRequest, current_user):
#     """그룹 내 간호사 목록과 선호도(제출본 우선)를 수집한다."""
#     nurses_in_group = (
#         db.query(Nurse)
#         .filter(Nurse.group_id == current_user.group_id)
#         .order_by(Nurse.experience.desc(), Nurse.nurse_id.asc())
#         .all()
#     )
#     nurse_ids = [n.nurse_id for n in nurses_in_group]

#     preferences = []
#     for nurse_id in nurse_ids:
#         submitted_pref = (
#             db.query(ShiftPreference)
#             .filter(
#                 ShiftPreference.nurse_id == nurse_id,
#                 ShiftPreference.year == req.year,
#                 ShiftPreference.month == req.month,
#                 ShiftPreference.is_submitted == True,
#             )
#             .order_by(ShiftPreference.submitted_at.desc())
#             .first()
#         )
#         if submitted_pref:
#             preferences.append(submitted_pref)
#         else:
#             draft_pref = (
#                 db.query(ShiftPreference)
#                 .filter(
#                     ShiftPreference.nurse_id == nurse_id,
#                     ShiftPreference.year == req.year,
#                     ShiftPreference.month == req.month,
#                     ShiftPreference.is_submitted == False,
#                 )
#                 .order_by(ShiftPreference.created_at.desc())
#                 .first()
#             )
#             if draft_pref:
#                 preferences.append(draft_pref)
#     return nurses_in_group, preferences


def _fetch_latest_config(db: Session, req: RosterRequest, current_user):
    """요청의 config_id 우선, 없으면 그룹 최신 config을 가져온다."""
    if req.config_id:
        latest_config = (
            db.query(RosterConfig).filter(RosterConfig.config_id == req.config_id).first()
        )
    else:
        latest_config = (
            db.query(RosterConfig)
            .filter(RosterConfig.group_id == current_user.group_id)
            .order_by(RosterConfig.created_at.desc())
            .first()
        )
    return latest_config


def _fetch_grade_config_dict(db: Session, office_id: str, group_id: str) -> dict:
    """그룹의 Grade 설정을 엔진 전달용 dict로 구성한다.

    Notes:
        - DB에 설정이 없으면 기본값을 반환한다.
        - Grade 제약은 `cp_sat_basic`에서 grade_strategy="GRADE"일 때만 적용된다.
    """
    config = (
        db.query(RosterGradeConfig)
        .filter(RosterGradeConfig.office_id == office_id, RosterGradeConfig.group_id == group_id)
        .first()
    )
    if not config:
        return {
            "null_grade_policy": "LOWEST",
            "use_dynamic_scaling": True,
            "constraints_json": {},
        }
    return {
        "null_grade_policy": config.null_grade_policy or "LOWEST",
        "use_dynamic_scaling": bool(config.use_dynamic_scaling),
        "constraints_json": config.constraints_json or {},
    }


def _fetch_grade_strategy_from_roster_config(db: Session, config_id: int | None) -> str | None:
    """roster_config 테이블에서 grade_strategy 값을 조회한다(있으면).

    Notes:
        - DB에 컬럼이 없을 수 있으므로 INFORMATION_SCHEMA로 확인 후 조회한다.
        - 값이 없거나 비어있으면 None을 반환한다.
    """
    if not config_id:
        return None
    if not _column_exists(db, "roster_config", "grade_strategy"):
        return None
    try:
        row = db.execute(
            text("SELECT grade_strategy FROM roster_config WHERE config_id = :cid"),
            {"cid": int(config_id)},
        ).fetchone()
    except Exception:
        return None
    if not row:
        return None
    val = getattr(row, "grade_strategy", None)
    if val is None:
        try:
            val = row[0]
        except Exception:
            val = None
    if not val:
        return None
    return str(val).upper()


def _resolve_grade_strategy(
    db: Session,
    config_dict: dict,
    office_id: str,
    group_id: str,
    roster_config_id: int | None,
) -> tuple[str, dict | None]:
    """TEAM/GRADE/BASE 전략을 단순하게 결정하고, 필요한 경우 grade_config를 함께 반환한다.

    우선순위:
        1) roster_config.grade_strategy 컬럼이 있으면 그 값을 최우선 사용한다.
        2) 없으면(구버전 호환):
            - team_balance_enable == 1 → TEAM
            - roster_grade_config.constraints_json이 비어있지 않음 → GRADE
            - 그 외 → BASE

    Returns:
        (grade_strategy, grade_config_or_none)
    """
    # 1) DB 컬럼 우선
    s = _fetch_grade_strategy_from_roster_config(db, roster_config_id)
    if s in ("BASE", "TEAM", "GRADE"):
        if s == "GRADE":
            gc = _fetch_grade_config_dict(db, office_id, group_id)
            return s, gc
        return s, None

    # 2) 구버전 폴백(요청 바디 말고 config_dict 기반)
    if bool(config_dict.get("team_balance_enable", False)):
        return "TEAM", None

    gc = _fetch_grade_config_dict(db, office_id, group_id)
    if bool((gc or {}).get("constraints_json") or {}):
        return "GRADE", gc
    return "BASE", None

def _build_shift_manage_and_requirements(db: Session, current_user, latest_config, req):
    """ShiftManage에서 인원·코드 정보를 읽어 engine용 데이터와 요구인원을 구성한다."""
    shift_manages = (
        db.query(ShiftManage)
        .filter(
            ShiftManage.office_id == current_user.office_id,
            ShiftManage.group_id == current_user.group_id,
            ShiftManage.nurse_class == 'RN',
            # ShiftManage.config_version == latest_config.config_version,
        )
        .order_by(ShiftManage.shift_slot.asc())
        .all()
    )
    shift_manage_data = [s.__dict__ for s in shift_manages]
    daily_shift_requirements = {}
    for sm in shift_manages:
        # if sm.codes:
        #     for code in sm.codes:
        # daily_shift_requirements[sm.main_code.strip()] = sm.manpower
        daily_shift_requirements[sm.main_code] = sm.manpower
    # ── DailyShift 일자별 요구치 조회 및 정규화 ──
    days_in_month = get_days_in_month(req.year, req.month)
    try:
        rows = (
            db.query(DailyShift)
            .filter(
                DailyShift.office_id == current_user.office_id,
                DailyShift.group_id == current_user.group_id,
                DailyShift.year == req.year,
                DailyShift.month == req.month,
            )
            .order_by(DailyShift.day.asc())
            .all()
        )
    except Exception as e:
        print(f"error: {e}")
    # day→counts 맵 구성 후 리스트로 변환(0-index)
    by_day = {r.day: {'D': int(r.d_count or 0), 'E': int(r.e_count or 0), 'N': int(r.n_count or 0)} for r in rows}
    daily_shift_requirements_by_day = [by_day.get(d, {'D': daily_shift_requirements.get('D', 0), 'E': daily_shift_requirements.get('E', 0), 'N': daily_shift_requirements.get('N', 0)}) for d in range(1, days_in_month + 1)]
    return shift_manage_data, daily_shift_requirements, daily_shift_requirements_by_day

def _normalize_to_main(code: str, code2main: dict) -> str:
    """세부 근무코드를 메인코드로 정규화한다."""
    if not code:
        return '-'
    c = str(code).upper()
    # 주휴('주')는 엔진/검증/제약에서 휴무(O)로 동일 취급한다.
    if c in ('O', 'OFF', '주'):
        return 'O'
    return code2main.get(c, c)

def _get_prev_year_month(year: int, month: int) -> tuple[int, int]:
    """이전 달의 (year, month)를 반환한다."""
    if month > 1:
        return year, month - 1
    return year - 1, 12


def _calc_months_diff(base_year: int, base_month: int, target_year: int, target_month: int) -> int:
    """기준 연월에서 대상 연월까지의 월 차이를 반환한다.

    Args:
        base_year: 기준 연도
        base_month: 기준 월(1~12)
        target_year: 대상 연도
        target_month: 대상 월(1~12)

    Returns:
        월 차이(음수 가능)
    """
    return (target_year - base_year) * 12 + (target_month - base_month)


def _calc_weekly_off_weekday_by_month(
    base_weekday: int,
    shift_variation: int,
    base_year: int,
    base_month: int,
    target_year: int,
    target_month: int,
) -> int:
    """월 단위 weekday rolling(shift rotation)으로 타깃 월의 주휴 요일을 계산한다.

    수식:
        weekday = (base_weekday + months_diff * shift_variation) % 7
        (예: base=수(2), shift_variation=-1, months_diff=2 → 월(0))

    Args:
        base_weekday: 기준 월의 요일(0=월..6=일)
        shift_variation: 누적 이동 값(예: -1)
        base_year: 기준 연도
        base_month: 기준 월
        target_year: 대상 연도
        target_month: 대상 월

    Returns:
        대상 월에 적용될 주휴 요일(0~6)
    """
    months_diff = _calc_months_diff(base_year, base_month, target_year, target_month)
    return (base_weekday + months_diff * shift_variation) % 7


def _calc_weekly_off_weekday_by_week(
    base_weekday: int,
    shift_variation: int,
    cycle_start_date: date,
    target_date: date,
    cycle_interval_weeks: int,
) -> int:
    """주 단위 weekday rolling(shift rotation)으로 타깃 날짜가 속한 주의 주휴 요일을 계산한다.

    - ISO week 기준이 아니라 **cycle_start_date 기준 N*7일 경과**로 계산한다.
    - target_date < cycle_start_date 인 경우 days_diff/steps가 음수가 될 수 있으나,
      modulo 7에 의해 요일은 수학적으로 정상 순환한다.

    Args:
        base_weekday: 기준 요일(0=월..6=일)
        shift_variation: 누적 이동 값
        cycle_start_date: 주기 기준 시작일
        target_date: 대상 날짜
        cycle_interval_weeks: 주기 간격(주). 1 이상만 유효.

    Returns:
        대상 주에 적용될 주휴 요일(0~6)
    """
    interval = max(1, int(cycle_interval_weeks or 1))
    days_diff = (target_date - cycle_start_date).days
    weeks_diff = days_diff // 7
    steps = weeks_diff // interval
    return (base_weekday + steps * shift_variation) % 7


def _table_exists(db: Session, table_name: str) -> bool:
    """MSSQL 기준으로 테이블 존재 여부를 확인한다."""
    try:
        row = db.execute(
            text(
                "SELECT 1 AS ok FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = :t"
            ),
            {"t": table_name},
        ).fetchone()
        return bool(row)
    except Exception:
        return False


def _column_exists(db: Session, table_name: str, column_name: str) -> bool:
    """MSSQL 기준으로 컬럼 존재 여부를 확인한다."""
    try:
        row = db.execute(
            text(
                "SELECT 1 AS ok FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_NAME = :t AND COLUMN_NAME = :c"
            ),
            {"t": table_name, "c": column_name},
        ).fetchone()
        return bool(row)
    except Exception:
        return False


def _weekday_dates_in_month(year: int, month: int, weekday: int) -> list[int]:
    """해당 월에서 특정 요일(0=월..6=일)에 해당하는 day_idx(0-based) 목록을 반환한다."""
    days = calendar.monthrange(year, month)[1]
    result: list[int] = []
    for d in range(1, days + 1):
        if date(year, month, d).weekday() == weekday:
            result.append(d - 1)
    return result


def _active_range_in_month(nurse: Nurse, month_start: date, days_in_month: int) -> tuple[int, int] | None:
    """한 달 기준 간호사의 근무 가능 day_idx 구간을 반환합니다."""
    month_end = month_start + timedelta(days=days_in_month - 1)
    resign = getattr(nurse, "resignation_date", None)
    join = getattr(nurse, "joining_date", None)
    join_date = join.date() if join else None
    resign_date = resign.date() if resign else None
    # ❌ 월과 겹치지 않으면 바로 제외
    if resign_date and resign_date < month_start:
        return None
    if join_date and join_date > month_end:
        return None
    # ✅ 실제 근무 가능 날짜 범위
    start_date = max(join_date, month_start) if join_date else month_start
    end_date = min(resign_date, month_end) if resign_date else month_end

    if start_date > end_date:
        return None

    start_idx = (start_date - month_start).days
    end_idx = (end_date - month_start).days

    return start_idx, end_idx


def _compute_weekly_off_day_indices_for_month(
    db: Session,
    office_id: str,
    group_id: str,
    year: int,
    month: int,
) -> tuple[dict[str, set[int]], list[dict]]:
    """주휴 설정을 기반으로 대상 월의 주휴 날짜를 계산한다.

    Returns:
        (nurse_id_to_day_indices, warnings)

    주의:
        - weekly_off_settings/nurses 컬럼이 실제 DB에 없으면 빈 결과를 반환한다(기능 비활성).
        - 엔진에는 OFF('O')로 고정 셀을 넣고, 저장 시 해당 날짜만 '주'로 마킹하는 방식이 안전하다.
    """
    warnings: list[dict] = []
    nurse_to_days: dict[str, set[int]] = {}

    if not _table_exists(db, "weekly_off_settings"):
        return nurse_to_days, warnings

    # nurses 테이블에 주휴 컬럼이 없으면 기능을 스킵(런타임 안전)
    if not _column_exists(db, "nurses", "weekly_off_weekday"):
        return nurse_to_days, warnings

    # 설정 조회
    try:
        setting = db.execute(
            text(
                "SELECT TOP 1 "
                "activate, use_variable_cycle, cycle_type, base_year, base_month, "
                "cycle_start_date, cycle_interval, shift_variation "
                "FROM weekly_off_settings "
                "WHERE office_id = :office_id AND group_id = :group_id"
            ),
            {"office_id": office_id, "group_id": group_id},
        ).fetchone()
    except Exception as e:
        warnings.append({"type": "settings_query_failed", "detail": str(e)})
        return nurse_to_days, warnings

    if not setting:
        return nurse_to_days, warnings

    activate = bool(setting.activate)
    if not activate:
        return nurse_to_days, warnings

    use_variable_cycle = bool(getattr(setting, "use_variable_cycle", False))
    cycle_type = (getattr(setting, "cycle_type", "month") or "month").lower()
    base_year = int(getattr(setting, "base_year", 0) or 0)
    base_month = int(getattr(setting, "base_month", 0) or 0)
    cycle_start_date = getattr(setting, "cycle_start_date", None)
    cycle_interval = int(getattr(setting, "cycle_interval", 1) or 1)
    shift_variation = int(getattr(setting, "shift_variation", -1) or -1)

    # base_year/base_month가 비어있으면 안전하게 '대상 월'로 잡는다(누적 이동 0)
    if base_year < 1 or base_month < 1:
        base_year, base_month = year, month

    # 간호사 주휴 요일 조회
    # weekly_off_enabled 컬럼은 선택(없어도 weekday null 여부로 판단)
    has_enabled_col = _column_exists(db, "nurses", "weekly_off_enabled")
    try:
        if has_enabled_col:
            rows = db.execute(
                text(
                    "SELECT nurse_id, weekly_off_enabled, weekly_off_weekday "
                    "FROM nurses WHERE group_id = :group_id AND active = 1"
                ),
                {"group_id": group_id},
            ).fetchall()
        else:
            rows = db.execute(
                text(
                    "SELECT nurse_id, weekly_off_weekday "
                    "FROM nurses WHERE group_id = :group_id AND active = 1"
                ),
                {"group_id": group_id},
            ).fetchall()
    except Exception as e:
        warnings.append({"type": "nurses_query_failed", "detail": str(e)})
        return nurse_to_days, warnings

    for r in rows:
        nurse_id = str(r.nurse_id)
        enabled = True
        if has_enabled_col:
            enabled = bool(getattr(r, "weekly_off_enabled", 0))

        base_weekday = getattr(r, "weekly_off_weekday", None)
        if not enabled or base_weekday is None:
            continue
        base_weekday = int(base_weekday)

        # 계산
        if not use_variable_cycle:
            # 변동 주기 OFF → 기준 요일을 그대로 사용
            month_weekday = base_weekday % 7
            day_indices = set(_weekday_dates_in_month(year, month, month_weekday))
            nurse_to_days[nurse_id] = day_indices
            continue

        if cycle_type == "month":
            month_weekday = _calc_weekly_off_weekday_by_month(
                base_weekday=base_weekday,
                shift_variation=shift_variation,
                base_year=base_year,
                base_month=base_month,
                target_year=year,
                target_month=month,
            )
            nurse_to_days[nurse_id] = set(_weekday_dates_in_month(year, month, month_weekday))
            continue

        if cycle_type == "week" and cycle_start_date:
            # 주 단위는 월 전체가 아니라 "주차별"로 요일이 달라질 수 있으므로, 주차 단위로 주휴를 찍는다.
            # - 각 주(월~일)를 대표하는 기준일을 월요일로 보고, 그 주의 주휴 요일을 계산한다.
            days = calendar.monthrange(year, month)[1]
            day_set: set[int] = set()
            for d in range(1, days + 1):
                cur = date(year, month, d)
                # 주 시작일(월요일)
                week_start = cur - timedelta(days=cur.weekday())
                w = _calc_weekly_off_weekday_by_week(
                    base_weekday=base_weekday,
                    shift_variation=shift_variation,
                    cycle_start_date=cycle_start_date,
                    target_date=week_start,
                    cycle_interval_weeks=cycle_interval,
                )
                if cur.weekday() == w:
                    day_set.add(d - 1)
            nurse_to_days[nurse_id] = day_set
            continue

        # 설정이 불완전하면 기준 요일 유지
        month_weekday = base_weekday % 7
        nurse_to_days[nurse_id] = set(_weekday_dates_in_month(year, month, month_weekday))

    return nurse_to_days, warnings


def _build_engine_nurse_index_map(nurses_in_group: list[Nurse]) -> dict[str, int]:
    """cp_sat_basic.create_nurses_from_db의 정렬 규칙을 그대로 재현하여 nurse_id→engine_index 맵을 만든다."""
    rows = []
    for n in nurses_in_group:
        rows.append(
            {
                "nurse_id": n.nurse_id,
                "sequence": getattr(n, "sequence", 0) or 0,
                "experience": getattr(n, "experience", 0) or 0,
            }
        )
    sorted_rows = sorted(
        rows,
        key=lambda r: (
            r.get("sequence", 0),
            -int(r.get("experience", 0) or 0),
            str(r.get("nurse_id")),
        ),
    )
    return {r["nurse_id"]: i for i, r in enumerate(sorted_rows)}


def _merge_fixed_cells_with_weekly_off(
    fixed_cells: list[dict] | None,
    weekly_off_cells: list[dict],
) -> tuple[list[dict], list[dict]]:
    """고정 셀 리스트를 병합하되, 주휴 고정 셀을 항상 우선 적용한다.

    Returns:
        (merged_fixed_cells, conflicts)
    """
    fixed_cells = list(fixed_cells or [])
    merged: dict[tuple[int, int], dict] = {}
    conflicts: list[dict] = []

    def _is_off(code: str | None) -> bool:
        c = (code or "").upper()
        return c in ("O", "OFF", "주")

    # 1) 기존 고정 셀을 먼저 적재
    for c in fixed_cells:
        key = (c.get("nurse_index"), c.get("day_index"))
        if key[0] is None or key[1] is None:
            continue
        merged[key] = c

    # 2) 주휴 고정 셀로 덮어쓰기(최우선)
    for wc in weekly_off_cells:
        key = (wc.get("nurse_index"), wc.get("day_index"))
        if key[0] is None or key[1] is None:
            continue
        prev = merged.get(key)
        if prev and not _is_off(prev.get("shift")):
            conflicts.append(
                {
                    "nurse_index": key[0],
                    "day_index": key[1],
                    "overridden_shift": prev.get("shift"),
                }
            )
        merged[key] = wc

    return list(merged.values()), conflicts

def _query_prev_month_schedule_id(db: Session, group_id: str, year: int, month: int) -> str | None:
    """이전 달의 최종(issued 우선) schedule_id를 조회한다."""
    py, pm = _get_prev_year_month(year, month)
    # 1) IssuedRoster 우선
    issued = (
        db.query(IssuedRoster)
        .join(Schedule, IssuedRoster.schedule_id == Schedule.schedule_id)
        .filter(Schedule.group_id == group_id, Schedule.year == py, Schedule.month == pm)
        .order_by(IssuedRoster.issued_at.desc())
        .first()
    )
    if issued:
        return issued.schedule_id
    # 2) 없으면 해당 월의 최신 Schedule
    latest = (
        db.query(Schedule)
        .filter(Schedule.group_id == group_id, Schedule.year == py, Schedule.month == pm)
        .order_by(Schedule.version.desc())
        .first()
    )
    return latest.schedule_id if latest else None

def _get_last_days_map(db: Session, schedule_id: str, days: int, code2main: dict) -> dict:
    """해당 schedule_id의 마지막 N일 근무코드를 메인코드로 정규화하여 반환한다.
    반환: { nurse_id: ['E','N','O','D','N','O'] } (최대 길이 days, 과거→현재 순)
    """
    if not schedule_id:
        return {}
    # 해당 스케줄의 모든 엔트리 로딩
    entries = db.query(ScheduleEntry).filter(ScheduleEntry.schedule_id == schedule_id).all()
    by_nurse: dict[str, dict[int, str]] = defaultdict(dict)
    max_day = 0
    for e in entries:
        d = int(e.work_date.day)
        max_day = max(max_day, d)
        by_nurse[e.nurse_id][d] = _normalize_to_main(e.shift_id, code2main)
    # 꼬리 days일만 취득
    result = {}
    start = max(1, max_day - days + 1)
    tail_days = list(range(start, max_day + 1))
    for nurse_id, daymap in by_nurse.items():
        seq = []
        for d in tail_days:
            seq.append(daymap.get(d, '-'))
        result[nurse_id] = seq
    return result

def _calc_tail_metrics(seq: list[str]) -> dict:
    """꼬리 시퀀스(길이<=6)로부터 연속성 메트릭을 계산한다."""
    if not seq:
        return {
            'consecutive_work_tail': 0,
            'consecutive_night_tail': 0,
            'last_day_shift': None,
            'offs_after_tail_nights': 0,
        }
    last = seq[-1]
    # 연속 근무 꼬리(D/E/N)
    cons_work = 0
    for c in reversed(seq):
        if c in ('D', 'E', 'N'):
            cons_work += 1
        elif c == 'O':
            break
    # tail 끝의 OFF 카운트
    offs_after_n = 0
    i = len(seq) - 1
    while i >= 0 and seq[i] == 'O':
        offs_after_n += 1
        i -= 1
    # 그 직전의 연속 N 카운트
    cons_n = 0
    while i >= 0 and seq[i] == 'N':
        cons_n += 1
        i -= 1
    return {
        'consecutive_work_tail': cons_work,
        'consecutive_night_tail': cons_n,
        'last_day_shift': last,
        'offs_after_tail_nights': min(2, offs_after_n),
    }

def build_cross_month_constraints(db: Session, req: RosterRequest, current_user, shift_manage_data, config_dict: dict, nurse_ids: list[str]) -> dict:
    """이전 달 꼬리 패턴을 기반으로 강제 OFF/금지 셀을 생성한다."""
    print("이전 월 경계 제약 생성 시작…")
    enable = bool(config_dict.get('cross_month_hard_rules_enable', True))
    lookback = int(config_dict.get('cross_month_lookback_days', 6))
    if not enable or lookback <= 0:
        print("이전 월 경계 제약 비활성화 또는 조회일수 0")
        return {'forced_off': {}, 'forbidden': {}}

    # 코드 정규화 맵 구성
    code2main = {}
    for r in (shift_manage_data or []):
        main = r.get('main_code')
        for c in (r.get('codes') or []):
            code2main[str(c).upper()] = main
    code2main['O'] = 'O'; code2main['O'] = 'O'

    # 이전 달 최신 스케줄 조회 → 마지막 N일 시퀀스
    try:
        prev_sid = _query_prev_month_schedule_id(db, current_user.group_id, req.year, req.month)
    except Exception as e:
        print("[ERR] _query_prev_month_schedule_id:", e)
        raise
    try:
        last_map = _get_last_days_map(db, prev_sid, lookback, code2main) if prev_sid else {}
    except Exception as e:
        print("[ERR] _get_last_days_map:", e)
        raise    
    forced_off = defaultdict(list)
    forbidden = defaultdict(lambda: defaultdict(list))

    # 설정값 활용
    K = int(config_dict.get('max_conseq_work') or 0)
    two_after_three = bool(config_dict.get('two_offs_after_three_nig'))
    two_after_two = bool(config_dict.get('two_offs_after_two_nig'))
    banned_E_to_D = bool(config_dict.get('banned_day_after_eve'))
    L = int(config_dict.get('max_consecutive_nights') or 0)

    for nurse_id in nurse_ids:
        tail = last_map.get(nurse_id, [])
        metrics = _calc_tail_metrics(tail)
        cons_work = metrics['consecutive_work_tail']
        cons_n = metrics['consecutive_night_tail']
        last_shift = metrics['last_day_shift']
        offs_after = metrics['offs_after_tail_nights']

        # (a) 연속 근무 K
        if K and cons_work == K:
            forced_off[nurse_id].append(0)
            print(f"간호사 {nurse_id}: 연속근무={cons_work} → day1 OFF")

        # (b) N2/3 → 2OFF
        req_offs = 0
        if two_after_three and cons_n >= 3:
            req_offs = 2
        elif two_after_two and cons_n >= 2:
            req_offs = 2
        rem = max(0, req_offs - offs_after)
        for d in range(min(2, rem)):
            forced_off[nurse_id].append(d)
        if rem > 0:
            print(f"간호사 {nurse_id}: N tail={cons_n}, offs_after={offs_after} → day1..{rem} OFF")

        # (c) E→D, N→D 금지
        if last_shift == 'E' and banned_E_to_D:
            forbidden[nurse_id][0].append('D')
        if last_shift == 'N':
            forbidden[nurse_id][0].append('D')

        # (d) 연속 N 상한
        if L and cons_n == L:
            forbidden[nurse_id][0].append('N')

    # 중복 제거/정렬
    forced_off = {k: sorted(set(v)) for k, v in forced_off.items()}
    forbidden = {k: {d: sorted(set(ss)) for d, ss in v.items()} for k, v in forbidden.items()}
    off_cnt = sum(len(v) for v in forced_off.values())
    forb_cnt = sum(len(ss) for v in forbidden.values() for ss in v.values())
    print(f"강제 OFF {off_cnt}건, 금지 셀 {forb_cnt}건 적용")
    return {'forced_off': forced_off, 'forbidden': forbidden}

def _run_cp_sat_basic(db: Session, current_user, nurses_in_group, preferences, latest_config, req, shift_manage_data, fixed_cells=None, time_limit_seconds=60, config_override: dict | None = None):
    """cp_sat_basic 엔진 호출을 표준화한다."""
    cp_sat_result = None
    try:
        nurses_dict = [n.__dict__ for n in nurses_in_group]
        # prefs_dict = [p.__dict__ for p in preferences]
        prefs_dict = preferences

        # 호출자가 구성한 config_dict(게이지 반영 등)이 있으면 이를 사용
        config_dict = (config_override.copy() if config_override is not None else (latest_config.__dict__.copy() if latest_config else {}))
        # ShiftManage 요구인원은 호출부에서 주입한다

        # fixed_cells 는 옵션
        # - '주' 등 휴무류 코드는 엔진에서 'O'로 정규화해야 한다(shift_types=['D','E','N','O']).
        if fixed_cells:
            norm_fixed = []
            for c in fixed_cells:
                try:
                    shift_code = str(c.get('shift') or '').upper()
                except Exception:
                    shift_code = ''
                if shift_code in ('OFF', 'O', '주'):
                    shift_code = 'O'
                norm_fixed.append(
                    {
                        'nurse_index': c.get('nurse_index'),
                        'day_index': c.get('day_index'),
                        'shift': shift_code,
                    }
                )
            config_dict['fixed_cells'] = norm_fixed
    except Exception as e:
        print(f"error: {e}")
        raise
    # cross-month 경계 제약 생성 및 주입
    try:
        initial_constraints = build_cross_month_constraints(
            db, req, current_user, shift_manage_data, config_dict, [n.nurse_id for n in nurses_in_group]
        )
        config_dict['initial_constraints'] = initial_constraints
    except Exception as e:
        print(f"이전 월 경계 제약 생성 실패: {e}")
    try:
        print("cp_sat_basic 엔진 호출 준비 완료")
        # 전략은 요청 바디가 아니라 "DB(roster_config.grade_strategy) → 없으면 config_dict 기반 폴백"만 사용한다.
        grade_strategy, grade_config = _resolve_grade_strategy(
            db=db,
            config_dict=config_dict,
            office_id=current_user.office_id,
            group_id=current_user.group_id,
            roster_config_id=getattr(latest_config, "config_id", None),
        )
        # 엔진에서도 사용할 수 있게 config_dict에 기록(디버깅/로그용)
        config_dict["grade_strategy"] = grade_strategy
        cp_sat_result = generate_roster_cp_sat(
            nurses_dict,
            prefs_dict,
            config_dict,
            req.year,
            req.month,
            shift_manage_data,
            time_limit_seconds=time_limit_seconds,
            grade_strategy=req.grade_strategy,
            grade_config=grade_config,
        )
    except Exception as e:
        print(f"error: {e}")
        raise
    if isinstance(cp_sat_result, dict) and "roster" in cp_sat_result:
        return (
            cp_sat_result["roster"],
            cp_sat_result.get("satisfaction_data", {}),
            cp_sat_result.get("roster_system"),
        )
    # 구형 반환 형식 호환
    return cp_sat_result, {}, None


def _persist_entries(db: Session, schedule, generated, req):
    """생성된 근무표를 ScheduleEntry로 저장한다."""
    db.query(ScheduleEntry).filter(ScheduleEntry.schedule_id == schedule.schedule_id).delete()
    for nurse_id, shifts in generated.items():
        for day_index, shift_id in enumerate(shifts):
            if shift_id != '-':
                work_date = date(req.year, req.month, day_index + 1)
                entry = ScheduleEntry(
                    entry_id=str(uuid.uuid4().hex)[:16],
                    schedule_id=schedule.schedule_id,
                    nurse_id=nurse_id,
                    work_date=work_date,
                    shift_id=shift_id.upper(),
                )
                db.add(entry)
    db.commit()


def _build_roster_response(db: Session, schedule, req, nurses_in_group):
    """프론트에서 쓰는 roster_data 형태로 응답을 구성한다."""
    shifts_db = db.query(Shift).all()
    shift_colors = {s.shift_id: s.color for s in shifts_db}
    entries = db.query(ScheduleEntry).filter(ScheduleEntry.schedule_id == schedule.schedule_id).all()

    roster_data = {
        "year": req.year,
        "month": req.month,
        "schedule_id": schedule.schedule_id,
        "days_in_month": get_days_in_month(req.year, req.month),
        "shift_colors": shift_colors,
        "nurses": [],
        "violations": [],
    }

    entries_by_nurse = {}
    for entry in entries:
        if entry.nurse_id not in entries_by_nurse:
            entries_by_nurse[entry.nurse_id] = {}
        entries_by_nurse[entry.nurse_id][entry.work_date.day] = entry.shift_id

    for nurse in nurses_in_group:
        nurse_schedule = [
            entries_by_nurse.get(nurse.nurse_id, {}).get(d, '-')
            for d in range(1, roster_data["days_in_month"] + 1)
        ]
        counts = {shift: nurse_schedule.count(shift) for shift in shift_colors.keys()}
        roster_data["nurses"].append(
            {
                "id": nurse.nurse_id,
                "name": nurse.name,
                "experience": nurse.experience,
                "schedule": nurse_schedule,
                "counts": counts,
            }
        )
    return roster_data
def _apply_preceptor_gauge(config_dict: dict, gauge: int | None) -> None:
    """프리셉터 게이지(0~10)를 엔진 설정 파라미터로 매핑한다.

    Args:
        config_dict: 엔진에 전달할 설정 딕셔너리 (in-place 수정)
        gauge: 프론트에서 전달한 게이지 값(0~10). None이면 미적용
    """

    if gauge is None:
        return
    print(f"프리셉터 게이지: {gauge}")
    g = max(0, min(10, int(gauge)))
    # 강도: 0→0.2x, 10→2.0x
    strength = round(0.2 + 0.18 * g, 2)
    # 상위 일수 K: 0→4, 10→30
    top_k = int(4 + (30 - 4) * (g / 10.0))
    # 최소 가중치 하한: 0→10.0, 10→5.0
    min_w = round(10.0 - 0.5 * g, 2)

    config_dict['preceptor_enable'] = g > 0
    config_dict['preceptor_strength_multiplier'] = strength
    config_dict['preceptor_top_days'] = top_k
    config_dict['preceptor_min_pair_weight'] = min_w
    # # 교대 포커스: 게이지 낮음→N, 중간→E/N, 높음→D/E/N
    # if g <= 3:
    #     config_dict['preceptor_focus_shifts'] = ['N']
    # elif g <= 6:
    #     config_dict['preceptor_focus_shifts'] = ['E','N']
    # else:
    #     config_dict['preceptor_focus_shifts'] = ['D','E','N']
    # print(f"[프리셉터 게이지] g={g} → strength={strength}x, top_k={top_k}, min_w={min_w}, focus={config_dict['preceptor_focus_shifts']}")
    print(f"[프리셉터 게이지] g={g} → strength={strength}x, top_k={top_k}, min_w={min_w}")


def _apply_team_balance_gauge(config_dict: dict, gauge: int | None) -> None:
    """
    팀 균등/집중 보너스 게이지(0~10)를 엔진 설정 파라미터로 매핑한다.
    - enable이 False이면 weight/top_days를 0으로 초기화
    """
    enable_flag = bool(config_dict.get("team_balance_enable", False))
    g = gauge if gauge is not None else config_dict.get("team_balance_gauge", 0)
    g = max(0, min(10, int(g or 0)))
    enable = enable_flag and g > 0
    config_dict["team_balance_gauge"] = g
    config_dict["team_balance_enable"] = enable

    # 정규화된 팀 보너스 강도(soft) 매핑:
    # weight는 개인 선호도 항의 계수(P*100) 스케일을 기준으로 "대략 0~240" 범위에서 동작하도록 캡을 둔다.
    # 식: weight = round(cap * (g/10)^p)
    # 예) cap=240, p=1.7, g=5 → 약 74, g=10 → 240
    cap = int(config_dict.get("team_balance_weight_cap", 240) or 240)
    power = float(config_dict.get("team_balance_weight_power", 1.7) or 1.7)
    cap = max(0, min(500, cap))  # 안전 상한(임의 폭주 방지)
    power = max(0.5, min(3.0, power))

    if enable:
        g_norm = g / 10.0
        config_dict["team_balance_weight"] = int(round(cap * (g_norm ** power)))
        config_dict["team_balance_top_days"] = int(6 + (30 - 6) * g_norm)
    else:
        config_dict["team_balance_weight"] = 0
        config_dict["team_balance_top_days"] = 0
    if "team_balance_focus_shifts" not in config_dict:
        config_dict["team_balance_focus_shifts"] = None
    if "team_balance_mode" not in config_dict:
        config_dict["team_balance_mode"] = "balanced"

    # 모드별 교대 가중치가 비어있으면 기본값을 채운다.
    if not config_dict.get("team_balance_shift_weights"):
        mode = str(config_dict.get("team_balance_mode", "balanced") or "balanced").lower()
        if mode == "focus_d":
            config_dict["team_balance_shift_weights"] = {"D": 1.5, "E": 0.6, "N": 0.3}
        elif mode == "focus_de":
            config_dict["team_balance_shift_weights"] = {"D": 1.2, "E": 1.2, "N": 0.5}
        else:
            config_dict["team_balance_shift_weights"] = {"D": 1.0, "E": 1.0, "N": 1.0}

# ───────────────────────────── 서비스 함수 ─────────────────────────────

def generate_roster_service(req: RosterRequest, current_user, db: Session):
    """
    근무표 생성 서비스 함수 (cp_sat_basic 엔진만 사용)
    """
    if not current_user or not current_user.is_head_nurse:
        raise Exception("Permission denied")
    wanted = (
        db.query(Wanted)
        .filter(
            Wanted.group_id == current_user.group_id,
            Wanted.year == req.year,
            Wanted.month == req.month,
        )
        .first()
    )
    if not wanted:
        raise Exception("해당 월의 wanted 작성을 먼저 요청해주세요.")
    schedule = request_schedule_service(req, current_user, db)
    nurses_in_group, preferences = _collect_nurses_and_preferences(db, req, current_user)
    latest_config = _fetch_latest_config(db, req, current_user)
    shift_manage_data, daily_shift_requirements, daily_shift_requirements_by_day = _build_shift_manage_and_requirements(
        db, current_user, latest_config, req
    )
    # daily_shift_requirements를 config에 주입해서 엔진 호출
    config_dict = latest_config.__dict__ if latest_config else {}
    config_dict['daily_shift_requirements'] = daily_shift_requirements
    # 일자별 요구치 우선 적용
    config_dict['daily_shift_requirements_by_day'] = daily_shift_requirements_by_day
    # ── 프리셉터 게이지(0~10) → 파라미터 매핑 ──
    
    _apply_preceptor_gauge(config_dict, config_dict['preceptor_gauge'])
    _apply_team_balance_gauge(config_dict, config_dict.get('team_balance_gauge'))
    # 경계 제약 기능 기본값
    config_dict.setdefault('cross_month_hard_rules_enable', True)
    config_dict.setdefault('cross_month_lookback_days', 6)
    config_dict.setdefault('allow_override_by_law', False)
    print("cp_sat_basic 엔진으로 근무표 생성 시작")

    # ── 주휴 고정 셀(최우선) 계산 후 엔진에 OFF(O)로 주입 ──
    weekly_off_warnings: list[dict] = []
    weekly_off_conflicts: list[dict] = []
    weekly_off_map: dict[str, set[int]] = {}
    weekly_off_fixed_cells: list[dict] = []
    month_start = date(req.year, req.month, 1)
    days_in_month = calendar.monthrange(req.year, req.month)[1]
    active_range_map = {
        str(n.nurse_id): _active_range_in_month(n, month_start, days_in_month)
        for n in nurses_in_group
    }

    print('active_range_map', active_range_map)
    try:
        weekly_off_map, weekly_off_warnings = _compute_weekly_off_day_indices_for_month(
            db=db,
            office_id=current_user.office_id,
            group_id=current_user.group_id,
            year=req.year,
            month=req.month,
        )
        if weekly_off_map:
            filtered_map: dict[str, set[int]] = {}
            for nurse_id, day_set in weekly_off_map.items():
                rng = active_range_map.get(str(nurse_id))
                if not rng:
                    continue
                start_idx, end_idx = rng
                clipped = {d for d in day_set if start_idx <= d <= end_idx}
                if clipped:
                    filtered_map[str(nurse_id)] = clipped
            weekly_off_map = filtered_map
        if weekly_off_map:
            nurse_idx_map = _build_engine_nurse_index_map(nurses_in_group)
            for nurse_id, day_set in weekly_off_map.items():
                n_idx = nurse_idx_map.get(nurse_id)
                if n_idx is None:
                    continue
                for d in sorted(day_set):
                    weekly_off_fixed_cells.append(
                        {"nurse_index": n_idx, "day_index": d, "shift": "O"}
                    )
    except Exception as e:
        weekly_off_warnings.append({"type": "weekly_off_failed", "detail": str(e)})

    generated, satisfaction_data, roster_system = _run_cp_sat_basic(
        db,
        current_user,
        nurses_in_group,
        preferences,
        latest_config,
        req,
        shift_manage_data,
        fixed_cells=weekly_off_fixed_cells if weekly_off_fixed_cells else None,
        time_limit_seconds=60,
        config_override=config_dict,
    )

    # ── 저장 시 주휴 날짜만 '주'로 마킹(표시용) ──
    try:
        if weekly_off_map and isinstance(generated, dict):
            for nurse_id, day_set in weekly_off_map.items():
                shifts = generated.get(nurse_id)
                if not shifts:
                    continue
                for d in day_set:
                    if start_idx <= d <= end_idx and 0 <= d < len(shifts):
                        shifts[d] = "주"
    except Exception as e:
        weekly_off_warnings.append({"type": "weekly_off_mark_failed", "detail": str(e)})

    _persist_entries(db, schedule, generated, req)
    roster_data = _build_roster_response(db, schedule, req, nurses_in_group)
    roster_data["weekly_off_conflicts"] = weekly_off_conflicts
    roster_data["weekly_off_warnings"] = weekly_off_warnings
    return roster_data


def generate_roster_service_with_fixed_cells(req, current_user, db: Session):
    """
    고정된 셀을 반영한 근무표 생성 서비스 함수 (cp_sat_basic 엔진만 사용)
    req: ex. year=2027 month=3 fixed_cells=[{'nurse_index': 0, 'day_index': 11, 'shift': 'D'}]
    """
    if not current_user or not current_user.is_head_nurse:
        raise Exception("Permission denied")

    fixed_cells = req.fixed_cells
    print(f"고정된 셀 개수: {len(fixed_cells)}")

    wanted = (
        db.query(Wanted)
        .filter(
            Wanted.group_id == current_user.group_id,
            Wanted.year == req.year,
            Wanted.month == req.month,
        )
        .first()
    )
    if not wanted:
        raise Exception("해당 월의 wanted 작성을 먼저 요청해주세요.")

    schedule = request_schedule_service(req, current_user, db)

    nurses_in_group, preferences = _collect_nurses_and_preferences(db, req, current_user)
    latest_config = _fetch_latest_config(db, req, current_user)
    shift_manage_data, daily_shift_requirements, daily_shift_requirements_by_day = _build_shift_manage_and_requirements(
        db, current_user, latest_config, req
    )
    # fixed_cells 및 요구인원 설정 반영
    config_dict = latest_config.__dict__ if latest_config else {}
    config_dict['daily_shift_requirements'] = daily_shift_requirements
    # 일자별 요구치 우선 적용
    config_dict['daily_shift_requirements_by_day'] = daily_shift_requirements_by_day
    # ── 프리셉터 게이지(0~10) → 파라미터 매핑 (고정 생성에도 동일 적용) ──
    _apply_preceptor_gauge(config_dict, config_dict['preceptor_gauge'])
    _apply_team_balance_gauge(config_dict, config_dict.get('team_balance_gauge'))
    # 경계 제약 기능 기본값 및 충돌 정책(hold는 기본 차단)
    config_dict.setdefault('cross_month_hard_rules_enable', True)
    config_dict.setdefault('cross_month_lookback_days', 6)
    config_dict.setdefault('allow_override_by_law', False)
    print("cp_sat_basic 엔진으로 고정 셀 반영 근무표 생성 시작")

    # ── 주휴 고정 셀(최우선) 계산 + 기존 고정 셀과 병합 ──
    weekly_off_warnings: list[dict] = []
    weekly_off_conflicts: list[dict] = []
    weekly_off_map: dict[str, set[int]] = {}
    weekly_off_fixed_cells: list[dict] = []
    merged_fixed_cells = fixed_cells
    try:
        weekly_off_map, weekly_off_warnings = _compute_weekly_off_day_indices_for_month(
            db=db,
            office_id=current_user.office_id,
            group_id=current_user.group_id,
            year=req.year,
            month=req.month,
        )
        if weekly_off_map:
            nurse_idx_map = _build_engine_nurse_index_map(nurses_in_group)
            for nurse_id, day_set in weekly_off_map.items():
                n_idx = nurse_idx_map.get(nurse_id)
                if n_idx is None:
                    continue
                for d in sorted(day_set):
                    weekly_off_fixed_cells.append(
                        {"nurse_index": n_idx, "day_index": d, "shift": "O"}
                    )
            merged_fixed_cells, weekly_off_conflicts = _merge_fixed_cells_with_weekly_off(
                fixed_cells=fixed_cells,
                weekly_off_cells=weekly_off_fixed_cells,
            )
    except Exception as e:
        weekly_off_warnings.append({"type": "weekly_off_failed", "detail": str(e)})

    generated, satisfaction_data, roster_system = _run_cp_sat_basic(
        db,
        current_user,
        nurses_in_group,
        preferences,
        latest_config,
        req,
        shift_manage_data,
        fixed_cells=merged_fixed_cells,
        time_limit_seconds=300,
        config_override=config_dict,
    )

    # ── 저장 시 주휴 날짜만 '주'로 마킹(표시용) ──
    try:
        if weekly_off_map and isinstance(generated, dict):
            for nurse_id, day_set in weekly_off_map.items():
                shifts = generated.get(nurse_id)
                if not shifts:
                    continue
                for d in day_set:
                    if 0 <= d < len(shifts):
                        shifts[d] = "주"
    except Exception as e:
        weekly_off_warnings.append({"type": "weekly_off_mark_failed", "detail": str(e)})

    _persist_entries(db, schedule, generated, req)
    roster_data = _build_roster_response(db, schedule, req, nurses_in_group)
    roster_data["weekly_off_conflicts"] = weekly_off_conflicts
    roster_data["weekly_off_warnings"] = weekly_off_warnings
    # print(7)
    # # 기존 로직 유지: 대시보드 분석 데이터 저장 시도 (있으면 사용)
    # try:
    #     from services.dashboard_service import save_roster_analytics
    #     if roster_system:
    #         print("CP-SAT 엔진 결과를 사용하여 대시보드 분석 데이터 저장 중...")
    #         save_roster_analytics(schedule.schedule_id, roster_system, db)
    #         print("대시보드 분석 데이터 저장 완료")
    # except ImportError as e:
    #     print(f"대시보드 서비스를 찾을 수 없습니다: {e}")
    # except Exception as e:
    #     print(f"대시보드 분석 데이터 저장 실패: {e}")

    # print(f"고정된 셀을 반영한 근무표 생성 완료: {len(fixed_cells)}개 셀 고정")
    return roster_data


def request_schedule_service(req: RosterRequest, current_user, db: Session):
    """
    스케줄 생성 서비스 함수
    """
    if not current_user or not current_user.is_head_nurse:
        raise Exception("Permission denied")
    nurse = db.query(Nurse).filter(Nurse.nurse_id == current_user.nurse_id).first()
    if not nurse or not nurse.group:
        raise Exception("User group information not found")
    # config_id가 제공된 경우 해당 config 사용, 아니면 최신 config 사용
    if req.config_id:
        latest_config = db.query(RosterConfig).filter(
            RosterConfig.config_id == req.config_id
        ).first()
    else:
        latest_config = db.query(RosterConfig).filter(
            RosterConfig.office_id == nurse.group.office_id,
            RosterConfig.group_id == nurse.group_id
        ).order_by(RosterConfig.created_at.desc()).first()
    print('latest_config', latest_config)
    # if not latest_config :
        # raise Exception("설정값을 입력해주세요")
    if not latest_config or latest_config == None:
        return "noConfigId"
    latest_version = db.query(func.max(Schedule.version)).filter(
        Schedule.group_id == current_user.group_id,
        Schedule.year == req.year,
        Schedule.month == req.month
    ).scalar() or 0
    new_schedule = Schedule(
        schedule_id=str(uuid.uuid4().hex)[:12],
        office_id=nurse.group.office_id,
        group_id=current_user.group_id,
        year=req.year,
        month=req.month,
        version=latest_version + 1,
        config_id=latest_config.config_id,
        created_by=current_user.account_id,
        status='draft',
        dropped=False,
        name=f"{req.month}월 근무표 VER{latest_version + 1}"
    )
    db.add(new_schedule)
    db.commit()
    db.refresh(new_schedule)
    db.commit()
    return new_schedule 