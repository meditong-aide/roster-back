"""
근무표 생성 관련 서비스 로직 모듈
- DB 쿼리, 데이터 가공, 엔진 호출 등 라우터에서 분리
- 모든 함수는 한글 docstring, 한글 print/logging, PEP8 스타일 적용
"""
from __future__ import annotations

import logging
from sqlalchemy.orm import Session
from fastapi import HTTPException
from db.models import (
    DailyShift,
    FixedWantedEntry,
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
from sqlalchemy import func, or_
from collections import defaultdict
import calendar
from sqlalchemy import text
from db.client2 import get_db

logger = logging.getLogger(__name__)
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


def _load_special_shift_map(db: Session, group_id: str, office_id: str) -> dict[str, dict]:
    """특별 근무(휴가/공가/근무) shift_id 메타데이터를 조회해 매핑으로 반환합니다.

    필터:
        - default_shift ∉ {D,E,N,O,주}
        - show_in_preference = 1
        - shift_gb IS NULL
        - type ∈ {'근무','휴가','공가'}
    """
    
    rows = (
        db.query(Shift)
        .filter(
            Shift.group_id == group_id,
            Shift.office_id == office_id,
            Shift.show_in_preference == 1,
            Shift.type.in_(["근무", "휴가", "공가"]),
            or_(
                Shift.default_shift.is_(None),
                Shift.default_shift.notin_(["D", "E", "N", "O", "주"]),
            ),
        )
        .all()
    )
    return {
        str(r.shift_id).upper(): {
            "shift_id": r.shift_id,
            "type": r.type,
            "default_shift": r.default_shift,
        }
        for r in rows
    }

def _collect_nurses_and_preferences(db: Session, req, current_user):
    """그룹 내 간호사 목록, 선호도, 특별 고정 요청을 수집한다. (FixedWantedEntry 존재 시 자동 사용)"""
    # 1️⃣ 그룹 내 간호사 목록
    print('collect_nurses_and_preferences 진입')
    nurses_in_group = (
        db.query(Nurse)
        .filter(Nurse.group_id == current_user.group_id)
        .order_by(Nurse.experience.desc(), Nurse.nurse_id.asc())
        .all()
    )
    inactive_nurses = [
        f"{getattr(n, 'name', '?')}({getattr(n, 'nurse_id', '?')})"
        for n in nurses_in_group
        if getattr(n, "active", 1) == 0
    ]
    print(
        "[RosterCreate] 그룹 간호사 로드: total="
        f"{len(nurses_in_group)}, inactive={len(inactive_nurses)} → {inactive_nurses}"
    )
    nurse_ids = [n.nurse_id for n in nurses_in_group]
    month_str = f"{req.year}-{req.month:02d}"
    preferences = []
    special_shift_map = _load_special_shift_map(db, current_user.group_id, current_user.office_id)
    special_fixed_requests: list[dict] = []

    # FixedWantedEntry 존재 여부 확인 (단일 테이블 구조)
    # → 해당 년/월에 확정 원티드가 존재하면 자동으로 사용 (프론트에서 플래그 전달 불필요)
    has_fixed_wanted = db.query(FixedWantedEntry).filter(
        FixedWantedEntry.group_id == current_user.group_id,
        FixedWantedEntry.year == req.year,
        FixedWantedEntry.month == req.month,
    ).first() is not None

    if has_fixed_wanted:
        print(f"[RosterCreate] FixedWantedEntry 사용 (단일 테이블)")
        # FixedWantedEntry 기반으로 선호도 수집
        _fw_deno_count = 0      # DENO 선호도 반영 건수
        _fw_special_count = 0   # 특수코드 하드 고정 건수
        _fw_other_count = 0     # 미분류(DENO/특수 어디에도 해당 안 됨) 건수
        for nurse_id in nurse_ids:
            # FixedWantedEntry에서 해당 간호사의 항목 조회 (is_applied=True만)
            fixed_entries = db.query(FixedWantedEntry).filter(
                FixedWantedEntry.group_id == current_user.group_id,
                FixedWantedEntry.year == req.year,
                FixedWantedEntry.month == req.month,
                FixedWantedEntry.nurse_id == nurse_id,
                FixedWantedEntry.is_applied == True,
            ).all()

            if not fixed_entries:
                continue  # 해당 간호사의 확정 원티드가 없으면 건너뜀

            shift_data = {"D": {}, "E": {}, "N": {}, "O": {}}
            first_entry = fixed_entries[0] if fixed_entries else None
            for fe in fixed_entries:
                shift_code_raw = str(fe.shift_id or "").strip()
                shift_code = shift_code_raw.upper()
                day_str = str(fe.shift_date.day)

                if shift_code in shift_data:
                    # 확정 원티드는 최고 우선순위 (score=10)
                    shift_data[shift_code][day_str] = 10
                    _fw_deno_count += 1
                    continue

                # 특별 근무(휴가/공가 등) 처리
                if (
                    shift_code in special_shift_map
                    and fe.shift_date.year == req.year
                    and fe.shift_date.month == req.month
                ):
                    try:
                        day_int = int(day_str)
                    except Exception:
                        continue
                    special_fixed_requests.append(
                        {
                            "nurse_id": nurse_id,
                            "day": day_int,
                            "shift_id": shift_code_raw,
                            "shift_type": special_shift_map[shift_code]["type"],
                        }
                    )
                    _fw_special_count += 1
                    continue

                # DENO도 아니고 special_shift_map에도 없는 코드 (D2, E2 등)
                _fw_other_count += 1

            # pair 데이터는 기존 WantedRequest에서 가져옴 (FixedWantedEntry에는 pair 없음)
            pair_data = []
            latest_wr = db.query(WantedRequest).filter(
                WantedRequest.nurse_id == nurse_id,
                WantedRequest.month == month_str,
            ).order_by(WantedRequest.request_id.desc()).first()

            if latest_wr:
                pair_rows = db.query(NursePairRequest).filter(
                    NursePairRequest.nurse_id == nurse_id,
                    NursePairRequest.request_id == latest_wr.request_id,
                ).all()
                pair_data = [{"id": p.target_id, "weight": p.score} for p in pair_rows]

            data_json = {
                "request": "확정 원티드 적용",
                "shift": {k: v for k, v in shift_data.items() if v},
                "preference": pair_data,
            }

            preferences.append({
                "nurse_id": nurse_id,
                "year": req.year,
                "month": req.month,
                "is_submitted": True,
                "created_at": first_entry.created_at if first_entry else None,
                "submitted_at": first_entry.updated_at if first_entry else None,
                "data": data_json,
            })

        print(
            f"[RosterCreate] FixedWantedEntry 수집 완료: "
            f"DENO 선호도={_fw_deno_count}건, "
            f"특수코드 하드고정={_fw_special_count}건, "
            f"미분류(D2/E2 등)={_fw_other_count}건, "
            f"preferences={len(preferences)}건"
        )
        return nurses_in_group, preferences, special_fixed_requests, special_shift_map

    # FixedWantedEntry가 없으면 기존 WantedRequest 기반으로 수집
    print("[RosterCreate] FixedWantedEntry 없음 → WantedRequest 기반 수집")
    # 2️⃣ 각 간호사별 submitted → draft 순으로 선호도 가져오기
    _wr_deno_count = 0
    _wr_special_count = 0
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
            shift_code_raw = str(s.shift or "").strip()
            shift_code = shift_code_raw.upper()
            day_str = str(int(str(s.shift_date).split("-")[-1]))
            if shift_code in shift_data:
                shift_data[shift_code][day_str] = int(s.score) if s.score is not None else 0
                _wr_deno_count += 1
                continue
            if (
                shift_code in special_shift_map
                and getattr(s, "shift_date", None)
                and s.shift_date.year == req.year
                and s.shift_date.month == req.month
            ):
                try:
                    day_int = int(day_str)
                except Exception:
                    continue
                special_fixed_requests.append(
                    {
                        "nurse_id": nurse_id,
                        "day": day_int,
                        "shift_id": shift_code_raw,
                        "shift_type": special_shift_map[shift_code]["type"],
                    }
                )
                _wr_special_count += 1
        # print(f'\n\n\n\n\nspecial_fixed_requests {special_fixed_requests}\n\n\n\n\n')
        # print(6)
        # 4️⃣ pair 데이터 수집
        pair_rows = (
            db.query(NursePairRequest)
            .filter(
                NursePairRequest.nurse_id == nurse_id,
                NursePairRequest.request_id == target_wr.request_id,
                NursePairRequest.month == month_str,
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
    print(
        f"[RosterCreate] WantedRequest 수집 완료: "
        f"DENO 선호도={_wr_deno_count}건, "
        f"특수코드 하드고정={_wr_special_count}건, "
        f"preferences={len(preferences)}건"
    )
    return nurses_in_group, preferences, special_fixed_requests, special_shift_map


# def _collect_nurses_and_preferences(db: Session, req: RosterRequest, current_user):
#     """그룹 내 간호사 목록과 선호도(제출본 우선)를 수집한다."""
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


def _load_shift_lookup(db: Session, office_id: str, group_id: str) -> dict[str, Shift]:
    """해당 office/group의 shifts를 로드해 shift_id→Shift 매핑을 반환합니다."""
    shifts = (
        db.query(Shift)
        .filter(Shift.office_id == office_id, Shift.group_id == group_id)
        .all()
    )
    lookup = {str(s.shift_id).upper(): s for s in shifts}
    print(f"[FixedShift] shift_lookup 로드: {len(lookup)}건")
    return lookup


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

    def _normalize_main_code(code: str | None) -> str:
        """ShiftManage의 main_code를 엔진 기준 메인코드(D/E/N/O)로 정규화한다.

        Notes:
            - 요구치 키가 'D','E','N'으로 들어오지 않으면 커버리지 제약이 약해져 OFF로 쏠릴 수 있다.
            - 예: " d " → "D"
        """
        if code is None:
            return ""
        c = str(code).strip().upper()
        if c in {"OFF", "O", "주"}:
            return "O"
        return c

    use_mid = bool(getattr(latest_config, "use_mid", False))
    base_keys = ["D", "E", "N"] + (["M"] if use_mid else [])
    daily_shift_requirements: dict[str, int] = {k: 0 for k in base_keys}
    for sm in shift_manages:
        main = _normalize_main_code(getattr(sm, "main_code", None))
        if main in daily_shift_requirements:
            daily_shift_requirements[main] = int(getattr(sm, "manpower", 0) or 0)
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
        rows = []

    def _row_to_day_counts(row: DailyShift) -> dict[str, int]:
        counts = {
            'D': int(getattr(row, 'd_count', 0) or 0),
            'E': int(getattr(row, 'e_count', 0) or 0),
            'N': int(getattr(row, 'n_count', 0) or 0),
        }
        if use_mid:
            counts['M'] = int(getattr(row, 'm_count', 0) or 0)
        return counts

    # day→counts 맵 구성 후 리스트로 변환(0-index)
    by_day: dict[int, dict[str, int]] = {}
    for row in rows:
        by_day[int(getattr(row, 'day', 0) or 0)] = _row_to_day_counts(row)
    daily_shift_requirements_by_day = [
        by_day.get(d, dict(daily_shift_requirements))
        for d in range(1, days_in_month + 1)
    ]

    # 안전장치: 요구치가 전부 0이면 엔진은 OFF로 쏠릴 확률이 높다.
    if sum(daily_shift_requirements.values()) <= 0 and all(
        sum(day_req.values()) <= 0 for day_req in daily_shift_requirements_by_day
    ):
        raise ValueError(
            "일별/기본 근무 요구치(D/E/N)가 모두 0입니다. "
            "ShiftManage.main_code 또는 DailyShift 설정을 확인해주세요."
        )
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


def _weekday_to_korean(weekday: int) -> str:
    """요일 번호(0=월..6=일)를 한글 요일명으로 변환한다."""
    weekday_names = ["월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일"]
    return weekday_names[weekday % 7]


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


def _split_fixed_nurses(nurses_in_group: list[Nurse]) -> tuple[list[Nurse], list[Nurse]]:
    """고정 근무 지정 여부로 간호사 목록을 분리합니다.

    Returns:
        (fixed_nurses, engine_nurses)
    """
    fixed: list[Nurse] = []
    engine: list[Nurse] = []
    for nurse in nurses_in_group:
        if getattr(nurse, "fixed_shift", None):
            fixed.append(nurse)
        else:
            engine.append(nurse)
    try:
        fixed_info = [
            f"{getattr(n, 'name', '?')}({getattr(n, 'nurse_id', '?')}): fixed_shift={getattr(n, 'fixed_shift', None)}"
            for n in fixed
        ]
        print(f"[FixedShift] 고정 근무 간호사 {len(fixed)}명 분리됨 → {fixed_info}")
    except Exception as e:
        print(f"[FixedShift] 분리 로깅 실패: {e}")
    return fixed, engine


# def _debug_log(stage: str, info: dict | None = None) -> None:
#     """디버깅용 단계별 로그를 출력합니다.

#     Args:
#         stage: 현재 단계 라벨
#         info: 추가 정보(dict) (예: {"count": 3})
#     """
#     try:
#         print(f"[DebugStage] {stage} | info={info}")
#     except Exception:
#         pass


def _build_fixed_shift_roster(
    fixed_nurses: list[Nurse],
    year: int,
    month: int,
    weekday_off_code: str = "O",
    sunday_code: str = "주",
    shift_lookup: dict[str, Shift] | None = None,
    weekly_off_active: bool = True,
) -> dict[str, list[str]]:
    """고정 근무 간호사의 월간 스케줄을 생성합니다.

    - 평일(월~금): fixed_shift 코드 배정
    - 토요일: weekday_off_code
    - 일요일: weekly_off_active이고 간호사 weekly_off_enabled가 1일 때만 sunday_code
    """
    days_in_month = calendar.monthrange(year, month)[1]
    month_start = date(year, month, 1)
    result: dict[str, list[str]] = {}
    for nurse in fixed_nurses:
        shifts = ["-" for _ in range(days_in_month)]
        active_range = _active_range_in_month(nurse, month_start, days_in_month)
        if active_range is None:
            result[str(nurse.nurse_id)] = shifts
            continue
        start_idx, end_idx = active_range
        fixed_code = str(getattr(nurse, "fixed_shift", "") or "").upper()
        weekly_off_enabled = weekly_off_active and bool(
            getattr(nurse, "weekly_off_enabled", False)
        )
        if shift_lookup is not None:
            if fixed_code not in shift_lookup:
                raise HTTPException(
                    status_code=400,
                    detail=f"fixed_shift가 shifts에 없습니다: nurse={getattr(nurse, 'name', '?')}({getattr(nurse, 'nurse_id', '?')}), code={fixed_code}",
                )
        try:
            print(
                f"[FixedShift] 적용: {getattr(nurse, 'name', '?')}({getattr(nurse, 'nurse_id', '?')}) "
                f"코드={fixed_code}, 범위={start_idx + 1}~{end_idx + 1}일"
            )
        except Exception as e:
            print(f"[FixedShift] 로그 실패: {e}")
        for day_idx in range(start_idx, end_idx + 1):
            weekday = (month_start + timedelta(days=day_idx)).weekday()
            if weekday >= 5:
                is_sunday_weekly_off = weekday == 6 and weekly_off_enabled
                shifts[day_idx] = sunday_code if is_sunday_weekly_off else weekday_off_code
            else:
                shifts[day_idx] = fixed_code
        result[str(nurse.nurse_id)] = shifts
    return result


def _overlay_fixed_roster_with_special_requests(
    fixed_roster: dict[str, list[str]],
    requests: list[dict],
    year: int,
    month: int,
) -> dict[str, list[str]]:
    """고정 근무자 스케줄에 휴가·교육 등 특수 요청을 덮어씌웁니다.

    Args:
        fixed_roster: 고정 근무자 기본 스케줄(nurse_id → 일자별 코드 리스트)
        requests: 고정 근무자가 제출한 특수 요청 목록
        year: 대상 연도
        month: 대상 월

    Returns:
        특수 요청이 반영된 고정 근무자 스케줄
    """
    if not fixed_roster or not requests:
        return fixed_roster
    days_in_month = calendar.monthrange(year, month)[1]
    for req in requests:
        try:
            nurse_id = str(req.get("nurse_id"))
            day = int(req.get("day", 0))
            shift_id = str(req.get("shift_id") or "").strip()
            shift_type = str(req.get("shift_type") or "").strip()
        except Exception:
            continue
        if day <= 0 or day > days_in_month:
            continue
        if nurse_id not in fixed_roster:
            continue
        day_idx = day - 1
        row = fixed_roster.get(nurse_id) or []
        if day_idx >= len(row):
            continue
        # 휴가/공가는 표시용으로 shift_id를 우선 사용하고, 없으면 OFF 처리
        if shift_type in {"휴가", "공가"}:
            code = shift_id or "O"
        else:
            code = shift_id or row[day_idx]
        row[day_idx] = code
        fixed_roster[nurse_id] = row
    return fixed_roster


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
        - 엔진/저장/응답에서는 휴무 코드를 항상 'O'로 통일한다(주휴도 'O').
        - 주휴 요일이 주말(토/일)이 아니면 즉시 예외를 발생시킨다.
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
        print("[WeeklyOff] 주휴 설정 비활성화(activate=0) → 주휴 고정 셀 미적용")
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
    has_weekend_off_col = _column_exists(db, "nurses", "is_weekend_off")
    try:
        if has_enabled_col:
            if has_weekend_off_col:
                rows = db.execute(
                    text(
                        "SELECT nurse_id, name, weekly_off_enabled, weekly_off_weekday, is_weekend_off "
                        "FROM nurses WHERE group_id = :group_id AND active = 1"
                    ),
                    {"group_id": group_id},
                ).fetchall()
            else:
                rows = db.execute(
                    text(
                        "SELECT nurse_id, name, weekly_off_enabled, weekly_off_weekday "
                        "FROM nurses WHERE group_id = :group_id AND active = 1"
                    ),
                    {"group_id": group_id},
                ).fetchall()
        else:
            if has_weekend_off_col:
                rows = db.execute(
                    text(
                        "SELECT nurse_id, name, weekly_off_weekday, is_weekend_off "
                        "FROM nurses WHERE group_id = :group_id AND active = 1"
                    ),
                    {"group_id": group_id},
                ).fetchall()
            else:
                rows = db.execute(
                    text(
                        "SELECT nurse_id, name, weekly_off_weekday "
                        "FROM nurses WHERE group_id = :group_id AND active = 1"
                    ),
                    {"group_id": group_id},
                ).fetchall()
    except Exception as e:
        warnings.append({"type": "nurses_query_failed", "detail": str(e)})
        return nurse_to_days, warnings

    for r in rows:
        nurse_id = str(r.nurse_id)
        name = str(r.name)
        enabled = True
        if has_enabled_col:
            enabled = bool(getattr(r, "weekly_off_enabled", 0))
        # 주말 고정 휴무 대상일 때만 "주휴 요일은 주말"을 강제한다.
        # - 컬럼이 없으면(레거시 DB) False로 간주하여 기존 동작(에러 없음)을 유지한다.
        is_weekend_off = bool(getattr(r, "is_weekend_off", 0)) if has_weekend_off_col else False

        base_weekday = getattr(r, "weekly_off_weekday", None)
        # ── 주말 휴무 대상 처리(백그라운드 강제 정책) ──
        # 요구사항:
        # - is_weekend_off=True인 인력은 "설정된 주휴 요일"을 무시하고,
        #   대상 월의 "일요일(6)"을 주휴로 강제한다.
        # - 이 경우 주휴 계산(변동 주기) 및 주말 검증 로직을 수행하지 않는다.
        #
        # 적용 조건(누락 방지):
        # - weekly_off_enabled 컬럼이 있으면 enabled=True일 때만 적용
        # - weekly_off_enabled 컬럼이 없으면(레거시) weekly_off_weekday가 설정되어 있는 경우에만 적용
        if is_weekend_off:
            if has_enabled_col:
                if not enabled:
                    continue
            else:
                # 레거시: weekly_off_weekday 자체가 없으면 주휴 자체를 쓰지 않는 것으로 간주
                if base_weekday is None:
                    continue
            print(
                f"[WeeklyOff] 주말 휴무 대상 강제: {name}({nurse_id}) "
                f"weekly_off_weekday={base_weekday} → 일요일(6)"
            )
            nurse_to_days[nurse_id] = set(_weekday_dates_in_month(year, month, 6))
            continue

        if not enabled or base_weekday is None:
            if enabled and base_weekday is None:
                print(f"[WeeklyOff] 간호사 {name}({nurse_id}): 주휴 활성화되었으나 weekly_off_weekday 없음 (스킵)")
            continue
        # 주말 휴무 대상(is_weekend_off=True)인 간호사도 주휴를 계산하되, 주휴가 주말인 경우에만 추가
        # (주말 휴무 제약으로 주말에 자동으로 휴무를 받지만, '주' 표시를 위해 주휴 정보는 유지)
        base_weekday = int(base_weekday)

        # 계산
        if not use_variable_cycle:
            # 변동 주기 OFF → 기준 요일을 그대로 사용
            month_weekday = base_weekday % 7
            if month_weekday not in (5, 6):
                print(
                    f"[WeeklyOff] ⚠️ 평일 주휴: {name}({nurse_id}) "
                    f"weekday={_weekday_to_korean(month_weekday)} "
                    f"is_weekend_off={int(is_weekend_off)}"
                )
            if is_weekend_off and month_weekday not in (5, 6):
                raise ValueError(
                    f"주휴 요일은 주말(토/일)만 허용됩니다. "
                    f"간호사={name}, 당월 주휴는 {_weekday_to_korean(month_weekday)}입니다."
                )
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
            if int(month_weekday) not in (5, 6):
                print(
                    f"[WeeklyOff] ⚠️ 평일 주휴: {name}({nurse_id}) "
                    f"weekday={_weekday_to_korean(int(month_weekday))} "
                    f"is_weekend_off={int(is_weekend_off)}"
                )
            if is_weekend_off and int(month_weekday) not in (5, 6):
                raise ValueError(
                    f"주휴 요일은 주말(토/일)만 허용됩니다. "
                    f"간호사={name}, 당월 주휴는 {_weekday_to_korean(int(month_weekday))}입니다."
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
                if int(w) not in (5, 6):
                    print(
                        f"[WeeklyOff] ⚠️ 평일 주휴: {name}({nurse_id}) "
                        f"weekday={_weekday_to_korean(int(w))} "
                        f"is_weekend_off={int(is_weekend_off)}"
                    )
                if is_weekend_off and int(w) not in (5, 6):
                    raise ValueError(
                        f"주휴 요일은 주말(토/일)만 허용됩니다. "
                        f"간호사={name}, 당월 주휴는 {_weekday_to_korean(int(w))}입니다."
                    )
                if cur.weekday() == w:
                    day_set.add(d - 1)
            nurse_to_days[nurse_id] = day_set
            continue

        # 설정이 불완전하면 기준 요일 유지
        month_weekday = base_weekday % 7
        if is_weekend_off and month_weekday not in (5, 6):
            raise ValueError(
                f"주휴 요일은 주말(토/일)만 허용됩니다. "
                f"간호사={name}, 당월 주휴는 {_weekday_to_korean(month_weekday)}입니다."
            )
        nurse_to_days[nurse_id] = set(_weekday_dates_in_month(year, month, month_weekday))

    weekend_off_only = {
        str(r.nurse_id)
        for r in rows
        if has_weekend_off_col and bool(getattr(r, "is_weekend_off", 0))
    }
    if weekend_off_only:
        print(f"[WeeklyOff] 주말 휴무 대상 간호사 수={len(weekend_off_only)}")
    return nurse_to_days, warnings


def _compute_lookahead_weekly_off_cells(
    db: Session,
    office_id: str,
    group_id: str,
    year: int,
    month: int,
    nurses_for_engine: list,
    D_phys: int,
    K_lookahead: int,
) -> set[tuple[int, int]]:
    """룩어헤드 구간(다음 달 1~K일)에 해당하는 주휴 고정 OFF 셀을 계산한다.

    weekly_off_by_idx는 당월 D_phys에서만 의미 있는 day index이므로,
    룩어헤드 구간의 주휴는 다음 달 실제 날짜(요일/주차 규칙)로 재계산한 별도 집합만 사용한다.

    Returns:
        (n_idx, d) 집합. d는 0-based 확장 일자(D_phys ~ D_phys+K_lookahead-1).
    """
    if K_lookahead <= 0:
        return set()
    next_year = year if month < 12 else year + 1
    next_month = month + 1 if month < 12 else 1
    nurse_to_days, _ = _compute_weekly_off_day_indices_for_month(
        db=db,
        office_id=office_id,
        group_id=group_id,
        year=next_year,
        month=next_month,
    )
    nurse_id_to_idx = {str(n.nurse_id): i for i, n in enumerate(nurses_for_engine)}
    cells: set[tuple[int, int]] = set()
    for nurse_id, day_set in nurse_to_days.items():
        n_idx = nurse_id_to_idx.get(str(nurse_id))
        if n_idx is None:
            continue
        for t in range(K_lookahead):
            if t in day_set:
                d = D_phys + t
                cells.add((n_idx, d))
    if cells:
        logger.info(
            "[Lookahead] 룩어헤드 주휴 셀: 다음 달 %s-%s 1~%s일 기준 %s건 (nurse×day)",
            next_year,
            next_month,
            K_lookahead,
            len(cells),
        )
    return cells


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


def _build_special_fixed_cells(
    requests: list[dict],
    nurse_idx_map: dict[str, int],
    special_shift_map: dict[str, dict],
    active_range_map: dict[str, tuple[int, int]] | None,
    days_in_month: int,
) -> tuple[list[dict], bool]:
    """특별 근무 요청을 엔진 고정 셀 리스트로 변환한다.

    반환:
        (fixed_cells, has_working): 고정 셀 목록과 근무형(연속근무 포함) 존재 여부
    """
    fixed_cells: list[dict] = []
    has_working = False
    for req in requests or []:
        nurse_id = str(req.get("nurse_id"))
        day = int(req.get("day", 0))
        shift_id = str(req.get("shift_id") or "").strip()
        shift_type = str(req.get("shift_type") or "").strip()
        if not nurse_id or not shift_id or day <= 0 or day > days_in_month:
            continue
        if special_shift_map and shift_id.upper() not in special_shift_map:
            continue
        n_idx = nurse_idx_map.get(nurse_id)
        if n_idx is None:
            continue
        if active_range_map:
            rng = active_range_map.get(nurse_id)
            if rng:
                start_idx, end_idx = rng
                day_idx = day - 1
                if day_idx < start_idx or day_idx > end_idx:
                    continue
        if shift_type == "근무":
            has_working = True
        fixed_cells.append(
            {
                "nurse_index": n_idx,
                "day_index": day - 1,
                "shift": shift_id,
                "shift_type": shift_type,
            }
        )
    # for f in fixed_cells:
    #     if f['nurse_index'] == 21:
    #         print('fixed_cells!!!!!', f)
    return fixed_cells, has_working


def _summarize_off_fixed_cells(
    weekly_off_fixed_cells: list[dict] | None,
    special_fixed_cells: list[dict] | None,
    nurses_in_group: list[Nurse],
) -> dict[str, dict[str, list[int]]]:
    """주휴/특수요청 기반의 고정 OFF 셀을 간호사별로 요약한다.

    Args:
        weekly_off_fixed_cells: 주휴로 생성된 고정 OFF 셀 목록
        special_fixed_cells: 특수 요청 기반 고정 셀 목록
        nurses_in_group: 간호사 객체 목록(이름 매핑용)

    Returns:
        간호사 ID → {"name": str, "weekly_off_days": [int], "special_off_days": [int]}
    """
    nurse_idx_to_id: dict[int, str] = {}
    nurse_id_to_name: dict[str, str] = {}
    for n in nurses_in_group:
        try:
            nurse_idx_to_id[int(n.id)] = str(n.nurse_id)
            nurse_id_to_name[str(n.nurse_id)] = str(n.name)
        except Exception:
            continue

    result: dict[str, dict[str, list[int]]] = {}

    for c in weekly_off_fixed_cells or []:
        n_idx = c.get("nurse_index")
        d_idx = c.get("day_index")
        if n_idx is None or d_idx is None:
            continue
        nurse_id = nurse_idx_to_id.get(int(n_idx))
        if not nurse_id:
            continue
        entry = result.setdefault(
            nurse_id,
            {"name": nurse_id_to_name.get(nurse_id, nurse_id), "weekly_off_days": [], "special_off_days": []},
        )
        entry["weekly_off_days"].append(int(d_idx))

    for c in special_fixed_cells or []:
        n_idx = c.get("nurse_index")
        d_idx = c.get("day_index")
        if n_idx is None or d_idx is None:
            continue
        shift_code = str(c.get("shift") or "").strip().upper()
        shift_type = str(c.get("shift_type") or "").strip()
        if shift_code not in {"O", "OFF", "주"} and shift_type not in {"휴가", "공가"}:
            continue
        nurse_id = nurse_idx_to_id.get(int(n_idx))
        if not nurse_id:
            continue
        entry = result.setdefault(
            nurse_id,
            {"name": nurse_id_to_name.get(nurse_id, nurse_id), "weekly_off_days": [], "special_off_days": []},
        )
        entry["special_off_days"].append(int(d_idx))

    for v in result.values():
        v["weekly_off_days"] = sorted(set(v["weekly_off_days"]))
        v["special_off_days"] = sorted(set(v["special_off_days"]))
    return result


def _inject_special_work_code(config_dict: dict, enabled: bool) -> None:
    """근무형 특별 근무가 있을 때 W(0 요구) 코드를 설정에 주입한다."""
    if not enabled:
        return
    ds_req = config_dict.get("daily_shift_requirements") or {}
    if not isinstance(ds_req, dict):
        ds_req = {}
    if "W" not in ds_req:
        ds_req = {**ds_req, "W": 0}
    config_dict["daily_shift_requirements"] = ds_req

    ds_by_day = config_dict.get("daily_shift_requirements_by_day")
    if isinstance(ds_by_day, list):
        normalized = []
        for day_map in ds_by_day:
            if not isinstance(day_map, dict):
                normalized.append({**ds_req})
                continue
            if "W" not in day_map:
                normalized.append({**day_map, "W": 0})
            else:
                normalized.append(day_map)
        config_dict["daily_shift_requirements_by_day"] = normalized

    spw = config_dict.get("shift_preference_weights") or {}
    if "W" not in spw:
        base = spw.get("D", 5.0)
        spw = {**spw, "W": base}
    config_dict["shift_preference_weights"] = spw


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
    # 1) IssuedRoster 우선 (is_active=True인 것만)
    issued = (
        db.query(IssuedRoster)
        .join(Schedule, IssuedRoster.schedule_id == Schedule.schedule_id)
        .filter(
            Schedule.group_id == group_id,
            Schedule.year == py,
            Schedule.month == pm,
            IssuedRoster.is_active == True
        )
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
        print(f"[CrossMonth] 이전 달 스케줄 ID 없음, 빈 맵 반환")
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
    print(f"[CrossMonth] 이전 달 마지막 {days}일 조회: day {start}~{max_day} (총 {len(tail_days)}일)")
    for nurse_id, daymap in by_nurse.items():
        seq = []
        for d in tail_days:
            seq.append(daymap.get(d, '-'))
        result[nurse_id] = seq
        # 간호사별 꼬리 시퀀스 출력 (간략하게 상위 5명만)
        if len([k for k in result.keys() if k < nurse_id]) < 5:
            seq_str = ' '.join(seq)
            print(f"  간호사 {nurse_id}: {seq_str}")
    # 간호사 0이 될 가능성이 있는 간호사(첫 번째 간호사)의 꼬리 패턴도 출력
    if result:
        first_nurse_id = min(result.keys(), key=lambda x: (x,))
        if first_nurse_id not in [k for k in result.keys() if k < first_nurse_id][:5]:
            seq_str = ' '.join(result[first_nurse_id])
            print(f"  간호사 {first_nurse_id} (첫 번째, nurse_index=0 가능): {seq_str}")
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
        return {'forced_off': {}, 'forbidden': {}, 'day0_n_fixed_nurse_ids': []}

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
        prev_year, prev_month = _get_prev_year_month(req.year, req.month)
        print(f"[CrossMonth] 이전 달 조회: {prev_year}년 {prev_month}월, schedule_id={prev_sid}, lookback={lookback}일")
    except Exception as e:
        print("[ERR] _query_prev_month_schedule_id:", e)
        raise
    try:
        last_map = _get_last_days_map(db, prev_sid, lookback, code2main) if prev_sid else {}
        print(f"[CrossMonth] 이전 달 꼬리 패턴 조회 완료: {len(last_map)}명")
    except Exception as e:
        print("[ERR] _get_last_days_map:", e)
        raise
    prev_month_last_main: dict[str, str | None] = {}
    prev_month_last_is_off: dict[str, bool] = {}
    off_window_constraints: dict[str, list[list[int]]] = {}
    for nurse_id, seq in last_map.items():
        last_code = seq[-1] if seq else None
        prev_month_last_main[nurse_id] = last_code
        prev_month_last_is_off[nurse_id] = bool(last_code == "O")
    forced_off = defaultdict(list)
    forbidden = defaultdict(lambda: defaultdict(list))
    day0_n_fixed_nurse_ids: list[str] = []
    # 1N 시 day0 주휴 여부: 주휴면 forbidden만, 아니면 day0=N 고정 + 2N2O 시 forced_off [1,2]
    weekly_off_map = config_dict.get("weekly_off_map") or {}
    day0_weekly_off_nurse_ids = {str(nid) for nid, days in weekly_off_map.items() if 0 in (days if isinstance(days, (list, set)) else [])}

    # 설정값 활용 (max_conseq_work 기본값은 엔진과 동일하게 5로 폴백)
    def _safe_int(val, default=None):
        try:
            if val is None or (isinstance(val, str) and not val.strip()):
                return default
            return int(val)
        except Exception:
            return default

    K = _safe_int(config_dict.get('max_conseq_work'), None)
    if K is None:
        K = _safe_int(config_dict.get('max_consecutive_work_days'), None)
    if K is None:
        K = 5
    two_after_three = bool(config_dict.get('two_offs_after_three_nig'))
    two_after_two = bool(config_dict.get('two_offs_after_two_nig'))
    not_one_night = bool(config_dict.get("not_one_night", False))
    banned_E_to_D = bool(config_dict.get('banned_day_after_eve'))
    L = int(config_dict.get('max_consecutive_nights') or 0)

    # 간호사별 is_weekend_off 정보 조회 (주말 휴무 대상자 필터링용)
    weekend_off_nurse_ids: set[str] = set()
    try:
        from db.models import Nurse as NurseModel
        from sqlalchemy import text
        weekend_off_rows = db.execute(
            text("SELECT nurse_id FROM nurses WHERE group_id = :group_id AND active = 1 AND is_weekend_off = 1"),
            {"group_id": current_user.group_id}
        ).fetchall()
        weekend_off_nurse_ids = {str(row.nurse_id) for row in weekend_off_rows}
        if weekend_off_nurse_ids:
            print(f"[CrossMonth] 주말 휴무 대상 간호사: {sorted(weekend_off_nurse_ids)}")
    except Exception as e:
        print(f"[CrossMonth] 주말 휴무 대상 간호사 조회 실패 (무시): {e}")
    
    # 대상 월의 주말 day_idx 계산 (필터링용)
    from datetime import date, timedelta
    target_month = date(req.year, req.month, 1)
    days_in_month = calendar.monthrange(req.year, req.month)[1]
    weekend_day_indices = {d for d in range(days_in_month) if (target_month + timedelta(days=d)).weekday() >= 5}
    print(f"[CrossMonth] 대상 월({req.year}년 {req.month}월) 주말 day_idx: {sorted(weekend_day_indices)}")

    def _extract_day0_requirements() -> dict[str, int]:
        """1일차(D/E/N) 요구치를 정규화한다."""
        def _norm_map(raw: dict | None) -> dict[str, int]:
            out = {"D": 0, "E": 0, "N": 0}
            if not isinstance(raw, dict):
                return out
            for k in ("D", "E", "N"):
                try:
                    out[k] = int(raw.get(k, 0) or 0)
                except Exception:
                    out[k] = 0
            return out

        ds_by_day = config_dict.get("daily_shift_requirements_by_day")
        if isinstance(ds_by_day, list) and len(ds_by_day) > 0 and isinstance(ds_by_day[0], dict):
            return _norm_map(ds_by_day[0])
        return _norm_map(config_dict.get("daily_shift_requirements"))

    day0_req_map = _extract_day0_requirements()
    day0_need_total = max(0, sum(day0_req_map.values()))
    ratio = float(config_dict.get("max_forced_off_day0_ratio", 0.15) or 0.15)
    ratio = max(0.0, min(1.0, ratio))
    min_cap = int(config_dict.get("max_forced_off_day0_min", 1) or 1)
    base_cap = int(day0_need_total * ratio) if day0_need_total > 0 else int(len(nurse_ids) * ratio)
    day0_cap = max(min_cap, base_cap)
    day0_cap = min(day0_cap, len(nurse_ids))

    # 대상 월 일수 계산 (월초 구간 제약 범위 산정용)
    days_in_month = calendar.monthrange(req.year, req.month)[1]

    for nurse_id in nurse_ids:
        tail = last_map.get(nurse_id, [])
        metrics = _calc_tail_metrics(tail)
        cons_work = metrics['consecutive_work_tail']
        cons_n = metrics['consecutive_night_tail']
        last_shift = metrics['last_day_shift']
        offs_after = metrics['offs_after_tail_nights']
        
        # 디버깅: 이전 달 꼬리 패턴과 계산된 메트릭 출력 (강제 OFF가 생성되는 경우만)
        forced_off_before = len(forced_off.get(nurse_id, []))

        # (a) 연속 근무 K
        if K and cons_work == K:
            forced_off[nurse_id].append(0)
            tail_str = ' '.join(tail) if tail else '(없음)'
            print(f"[CrossMonth] 간호사 {nurse_id}: 연속근무={cons_work} (꼬리: {tail_str}) → day0(1일) OFF 강제")

        # (a-1) 꼬리 연속근무 보정: 월초 0..(K-w) 구간에 OFF ≥ 1
        # - w(=cons_work)가 0보다 크면 첫 (K-w+1)일 안에 최소 1일 OFF를 요구하여 총 연속근무가 K를 넘지 않도록 한다.
        if K > 0 and cons_work > 0:
            window_end = max(0, K - cons_work)
            window_end = min(window_end, days_in_month - 1)
            if window_end >= 0:
                off_window_constraints.setdefault(nurse_id, []).append([0, window_end])
                tail_str = ' '.join(tail) if tail else '(없음)'
                print(
                    f"[CrossMonth] 간호사 {nurse_id}: 꼬리 연속근무 w={cons_work}, "
                    f"K={K} → 월초 0..{window_end}(1~{window_end+1}일) 구간 OFF≥1 제약 추가 "
                    f"(꼬리: {tail_str})"
                )
        # (b-0) 1N 금지: 꼬리 N이 1개라면 day0 N 고정 또는 forbidden
        # day0이 주휴면 forbidden만(주휴 우선). 아니면 day0=N 고정 + 2N2O 시 day1,2 OFF 강제.
        if not_one_night and cons_n == 1:
            if 0 in forced_off.get(nurse_id, []):
                forced_off[nurse_id] = [d for d in forced_off.get(nurse_id, []) if d != 0]
            tail_str = ' '.join(tail) if tail else '(없음)'
            if nurse_id in day0_weekly_off_nurse_ids:
                forbidden[nurse_id][0].extend(['D', 'E', 'O'])
                print(f"[CrossMonth] 간호사 {nurse_id}: 1N tail + day0 주휴 → day0 O 유지(forbidden D/E/O), tail={tail_str}")
            else:
                day0_n_fixed_nurse_ids.append(nurse_id)
                two_after_two_effective = two_after_two or not_one_night
                if two_after_two_effective:
                    forced_off[nurse_id].extend([1, 2])
                print(f"[CrossMonth] 간호사 {nurse_id}: 1N tail → day0 N 고정" + (", day1,2 OFF(2N2O)" if two_after_two_effective else "") + f", tail={tail_str}")

        # (b) N2/3 → 2OFF
        req_offs = 0
        two_after_two_effective = two_after_two or not_one_night
        two_after_three_effective = two_after_three
        if two_after_three_effective and cons_n >= 3:
            req_offs = 2
        elif two_after_two_effective and cons_n >= 2:
            req_offs = 2
        rem = max(0, req_offs - offs_after)
        for d in range(min(2, rem)):
            forced_off[nurse_id].append(d)
        if rem > 0:
            tail_str = ' '.join(tail) if tail else '(없음)'
            print(f"[CrossMonth] 간호사 {nurse_id}: N tail={cons_n}, offs_after={offs_after}, req_offs={req_offs}, rem={rem} (꼬리: {tail_str}) → day0..{rem-1}(1일..{rem}일) OFF 강제")
        
        # 강제 OFF가 추가된 경우 상세 정보 출력
        forced_off_after = len(forced_off.get(nurse_id, []))
        if forced_off_after > forced_off_before:
            final_days = sorted(set(forced_off.get(nurse_id, [])))
            print(f"[CrossMonth] 간호사 {nurse_id} 최종 강제 OFF day_idx: {final_days}")
            
            # 주말 휴무 대상 간호사의 경우 평일 OFF 필터링
            if nurse_id in weekend_off_nurse_ids:
                filtered_days = [d for d in final_days if d in weekend_day_indices]
                removed_days = [d for d in final_days if d not in weekend_day_indices]
                if removed_days:
                    print(f"[CrossMonth] ⚠️ 간호사 {nurse_id} (주말 휴무 대상): 평일 OFF 제거 day_idx={removed_days}, 주말만 유지 day_idx={filtered_days}")
                    forced_off[nurse_id] = filtered_days

        # (c) E→D, N→D 금지
        if last_shift == 'E' and banned_E_to_D:
            forbidden[nurse_id][0].append('D')
        if last_shift == 'N':
            forbidden[nurse_id][0].append('D')

        # (d) 연속 N 상한
        if L and cons_n == L:
            forbidden[nurse_id][0].append('N')

    # day0 강제 OFF 과밀 완화(cap 이동)는 기본 비활성화
    enable_day0_cap = bool(config_dict.get("cross_month_day0_cap_enable", False))
    if enable_day0_cap:
        day0_all = [nid for nid, days in forced_off.items() if 0 in days]
        day0_locked = [nid for nid in day0_all if nid in weekend_off_nurse_ids]  # 주말휴무 전담은 이동하지 않음
        movable = [nid for nid in day0_all if nid not in weekend_off_nurse_ids]
        allow_movable = max(0, day0_cap - len(day0_locked))
        if len(movable) > allow_movable:
            overflow = movable[allow_movable:]
            for nid in overflow:
                days = forced_off.get(nid, [])
                days = [d for d in days if d != 0]
                placed = False
                for target in (1, 2):
                    if target not in days:
                        days.append(target)
                        placed = True
                        break
                forced_off[nid] = sorted(set(days)) if placed else sorted(set(days + [0]))
            print(
                f"[CrossMonth] day0 강제 OFF cap 적용: cap={day0_cap}, "
                f"locked={len(day0_locked)}, moved={len(overflow)}, total_before={len(day0_all)}"
            )

    # 중복 제거/정렬
    forced_off = {k: sorted(set(v)) for k, v in forced_off.items()}
    forbidden = {k: {d: sorted(set(ss)) for d, ss in v.items()} for k, v in forbidden.items()}
    off_cnt = sum(len(v) for v in forced_off.values())
    forb_cnt = sum(len(ss) for v in forbidden.values() for ss in v.values())
    print(f"강제 OFF {off_cnt}건, 금지 셀 {forb_cnt}건 적용, 1N day0 N 고정 {len(day0_n_fixed_nurse_ids)}명")
    return {
        'forced_off': forced_off,
        'forbidden': forbidden,
        'prev_month_last_main': prev_month_last_main,
        'prev_month_last_is_off': prev_month_last_is_off,
        'off_window_constraints': off_window_constraints,
        'day0_n_fixed_nurse_ids': day0_n_fixed_nurse_ids,
    }


def _build_code_to_main_map(shift_manage_data: list[dict] | None) -> dict[str, str]:
    """ShiftManage의 codes → main_code 정규화 맵을 만든다.

    Returns:
        code2main: 예) {"D": "D", "D1": "D", "E": "E", "N": "N", "O": "O"}
    """
    code2main: dict[str, str] = {}
    for row in (shift_manage_data or []):
        main = str(row.get("main_code") or "").strip().upper()
        if not main:
            continue
        for code in (row.get("codes") or []):
            c = str(code).strip().upper()
            if c:
                code2main[c] = main
    # 휴무는 항상 O로 통일
    code2main["OFF"] = "O"
    code2main["주"] = "O"
    code2main["O"] = "O"
    return code2main


def _normalize_allowed_shift_types(raw_value: object) -> set[str]:
    """간호사 row의 '허용 근무유형(D/E/N) 리스트'를 정규화한다.

    정책:
        - [] 또는 None: 제한 없음(= D/E/N 모두 가능)
        - ["N"], ["D","E"], ["D","N"] 등: 해당 코드만 가능
        - 기존 레거시 int/boolean 기반 night 전담 값은 **무시**한다.

    Args:
        raw_value: DB에 저장된 값(JSON). 기대 형태는 List[str] 또는 None.

    Returns:
        허용 코드 집합. 빈 집합이면 "제한 없음"을 의미한다.

    Raises:
        ValueError: 리스트에 D/E/N 이외의 코드가 섞여있을 때
    """
    if raw_value is None:
        return set()
    # 레거시 타입은 무시(요구사항: 기존 is_night_nurse 의미는 무시)
    if isinstance(raw_value, (int, float, bool)):
        return set()
    if not isinstance(raw_value, list):
        return set()

    allowed: set[str] = set()
    invalid: set[str] = set()
    for item in raw_value:
        code = str(item).strip().upper()
        if not code:
            continue
        if code in {"D", "E", "N"}:
            allowed.add(code)
        else:
            invalid.add(code)
    if invalid:
        raise ValueError(f"허용 근무유형 값이 올바르지 않습니다: {sorted(invalid)} (허용: D/E/N)")
    return allowed


def _normalize_shift_to_main(shift_code: object, code2main: dict[str, str]) -> str:
    """고정 셀의 shift 코드를 엔진 기준 메인코드(D/E/N/O)로 정규화한다."""
    code = str(shift_code or "").strip().upper()
    if code in {"OFF", "주"}:
        return "O"
    if code in {"D", "E", "N", "O"}:
        return code
    return str(code2main.get(code, code)).strip().upper()


def build_allowed_shift_type_constraints(
    nurses_in_group: list,
    year: int,
    month: int,
    shift_manage_data: list[dict] | None,
    fixed_cells: list[dict] | None,
) -> dict:
    """간호사별 허용 근무유형(D/E/N) 하드 제약을 forbidden 형태로 생성한다.

    목표:
        - 간호사 row에 저장된 허용 목록에 따라, 허용되지 않은 D/E/N 배정을 전일(day_idx 전체)에 대해 금지한다.
        - OFF(O)는 항상 가능하도록 금지 대상에서 제외한다.
        - fixed_cells(고정셀)가 허용 목록과 충돌하면 해당 날짜만 예외로 두고 진행한다.

    Returns:
        {"forced_off": {}, "forbidden": {nurse_id: {day_idx: ["D","E"]}}}
    """
    days_in_month = int(get_days_in_month(year, month))
    if days_in_month <= 0:
        return {"forced_off": {}, "forbidden": {}}

    code2main = _build_code_to_main_map(shift_manage_data)
    nurse_id_to_allowed: dict[str, set[str]] = {}

    for n in nurses_in_group:
        nurse_id = str(getattr(n, "nurse_id", "") or "")
        if not nurse_id:
            continue
        allowed = _normalize_allowed_shift_types(getattr(n, "is_night_nurse", None))
        nurse_id_to_allowed[nurse_id] = allowed

    # ── 고정셀 충돌은 예외 처리(고정 우선) ──
    override_days_by_nurse: dict[str, set[int]] = {}
    if fixed_cells:
        nurse_idx_map = _build_engine_nurse_index_map(nurses_in_group)
        idx_to_nurse_id = {idx: nid for nid, idx in nurse_idx_map.items()}
        for c in fixed_cells:
            try:
                n_idx = int(c.get("nurse_index"))
                day_idx = int(c.get("day_index"))
            except Exception:
                continue
            fixed_main = _normalize_shift_to_main(c.get("shift"), code2main)
            if fixed_main not in {"D", "E", "N"}:
                continue  # O는 항상 가능
            nurse_id = idx_to_nurse_id.get(n_idx)
            if not nurse_id:
                continue
            allowed = nurse_id_to_allowed.get(nurse_id, set())
            if not allowed:
                continue  # 제한 없음
            if fixed_main not in allowed:
                override_days_by_nurse.setdefault(nurse_id, set()).add(day_idx)
                allowed_sorted = sorted(allowed)
                print(
                    "[AllowedShiftTypes] 고정 근무형 우선 적용: "
                    f"nurse_id={nurse_id}, day={day_idx + 1}, fixed={fixed_main}, "
                    f"allowed={allowed_sorted}"
                )
        if override_days_by_nurse:
            for nurse_id, days in override_days_by_nurse.items():
                day_list = [d + 1 for d in sorted(days)]
                print(
                    "[AllowedShiftTypes] 고정 근무형 예외 일자: "
                    f"nurse_id={nurse_id}, days={day_list}"
                )

    forbidden: dict[str, dict[int, list[str]]] = {}
    all_codes = {"D", "E", "N"}
    for nurse_id, allowed in nurse_id_to_allowed.items():
        if not allowed:
            continue  # 제한 없음
        disallowed = sorted(all_codes - set(allowed))
        if not disallowed:
            continue
        day_map: dict[int, list[str]] = {}
        override_days = override_days_by_nurse.get(nurse_id, set())
        for d in range(days_in_month):
            if d in override_days:
                continue
            day_map[d] = disallowed
        forbidden[nurse_id] = day_map

    forb_cnt = sum(len(v) * len(next(iter(v.values()), [])) for v in forbidden.values())
    if forbidden:
        print(f"[AllowedShiftTypes] 금지 셀(월 전체) 적용: nurses={len(forbidden)}, approx_cnt={forb_cnt}")
    return {"forced_off": {}, "forbidden": forbidden}


def _merge_initial_constraints(base: dict | None, extra: dict | None) -> dict:
    """initial_constraints 딕셔너리를 안전하게 병합한다(금지/강제OFF는 합집합)."""
    base = base if isinstance(base, dict) else {}
    extra = extra if isinstance(extra, dict) else {}

    merged_forced_off: dict[str, list[int]] = {}
    for src in (base.get("forced_off") or {}, extra.get("forced_off") or {}):
        for nurse_id, day_list in (src or {}).items():
            merged_forced_off.setdefault(str(nurse_id), set()).update({int(d) for d in (day_list or [])})
    merged_forced_off_out = {k: sorted(v) for k, v in merged_forced_off.items()}

    merged_forbidden: dict[str, dict[int, set[str]]] = {}
    for src in (base.get("forbidden") or {}, extra.get("forbidden") or {}):
        for nurse_id, day_map in (src or {}).items():
            nid = str(nurse_id)
            merged_forbidden.setdefault(nid, {})
            for day_idx, codes in (day_map or {}).items():
                try:
                    d = int(day_idx)
                except Exception:
                    continue
                merged_forbidden[nid].setdefault(d, set()).update({str(c).strip().upper() for c in (codes or [])})

    merged_forbidden_out: dict[str, dict[int, list[str]]] = {}
    for nurse_id, day_map in merged_forbidden.items():
        merged_forbidden_out[nurse_id] = {d: sorted(codes) for d, codes in day_map.items()}

    return {"forced_off": merged_forced_off_out, "forbidden": merged_forbidden_out}

def _run_cp_sat_basic(db: Session, current_user, nurses_in_group, preferences, latest_config, req, shift_manage_data, fixed_cells=None, time_limit_seconds=60, config_override: dict | None = None):
    """cp_sat_basic 엔진 호출을 표준화한다."""
    cp_sat_result = None
    try:
        nurses_dict = [n.__dict__ for n in nurses_in_group]
        # is_weekend_off는 ORM 컬럼 유무와 무관하게, DB에 컬럼이 있으면 직접 조회해서 엔진 입력에 주입한다.
        # - 이유: ORM 모델/스키마가 아직 확장되지 않은 환경에서도 fallback/하드 제약이 동작해야 한다.
        try:
            if _column_exists(db, "nurses", "is_weekend_off"):
                rows = db.execute(
                    text(
                        "SELECT nurse_id, is_weekend_off "
                        "FROM nurses WHERE group_id = :group_id AND active = 1"
                    ),
                    {"group_id": current_user.group_id},
                ).fetchall()
                id_to_weekend_off = {str(r.nurse_id): bool(getattr(r, "is_weekend_off", 0)) for r in rows}
                print('id_to_weekend_off!!!!!', id_to_weekend_off)
                for nd in nurses_dict:
                    nid = str(nd.get("nurse_id") or nd.get("db_id") or "")
                    if not nid:
                        continue
                    nd["is_weekend_off"] = bool(id_to_weekend_off.get(nid, False))
        except Exception as e:
            print(f"[WeeklyOff] is_weekend_off 주입 실패(무시): {e}")
        # prefs_dict = [p.__dict__ for p in preferences]
        prefs_dict = preferences

        # 호출자가 구성한 config_dict(게이지 반영 등)이 있으면 이를 사용
        config_dict = (config_override.copy() if config_override is not None else (latest_config.__dict__.copy() if latest_config else {}))
        # ShiftManage 요구인원은 호출부에서 주입한다

        try:
            shift_lookup = _load_shift_lookup(db, current_user.office_id, current_user.group_id)
            shift_defs = []
            for shift in shift_lookup.values():
                shift_defs.append(
                    {
                        "shift_id": getattr(shift, "shift_id", None),
                        "default_shift": getattr(shift, "default_shift", None) or getattr(shift, "shift_id", None),
                        "shift_gb": getattr(shift, "shift_gb", None),
                        "type": getattr(shift, "type", None),
                        "show_in_preference": getattr(shift, "show_in_preference", None),
                    }
                )
            # print(f"[ShiftMapping] shift_defs: {shift_defs}")
            if shift_defs:
                config_dict["shift_definitions"] = shift_defs
        except Exception as e:
            print(f"[ShiftMapping] shift_definitions 구성 실패(무시): {e}")

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

    # ── 1) 허용 근무유형(D/E/N) 하드 제약 생성(월 전체) + 고정셀 충돌 검증(옵션 A) ──
    allowed_constraints = build_allowed_shift_type_constraints(
        nurses_in_group=nurses_in_group,
        year=req.year,
        month=req.month,
        shift_manage_data=shift_manage_data,
        fixed_cells=config_dict.get("fixed_cells"),
    )

    # ── 2) cross-month 경계 제약 생성 ──
    cross_month_constraints: dict = {"forced_off": {}, "forbidden": {}}
    try:
        cross_month_constraints = build_cross_month_constraints(
            db, req, current_user, shift_manage_data, config_dict, [n.nurse_id for n in nurses_in_group]
        )
    except Exception as e:
        print(f"이전 월 경계 제약 생성 실패: {e}")
    # 1N day0 N 고정 셀 병합 (고정 근무 최우선이므로 기존 fixed_cells에 추가)
    day0_n_fixed_nurse_ids = cross_month_constraints.get("day0_n_fixed_nurse_ids") or []
    if day0_n_fixed_nurse_ids:
        fixed_list = config_dict.get("fixed_cells") or []
        nurse_idx_map = _build_engine_nurse_index_map(nurses_in_group)
        for nurse_id in day0_n_fixed_nurse_ids:
            n_idx = nurse_idx_map.get(str(nurse_id))
            if n_idx is not None:
                fixed_list.append({"nurse_index": n_idx, "day_index": 0, "shift": "N"})
        config_dict["fixed_cells"] = fixed_list
    prev_month_last_is_off = cross_month_constraints.get("prev_month_last_is_off") or {}
    if prev_month_last_is_off:
        config_dict["prev_month_last_is_off"] = prev_month_last_is_off
    off_window_constraints = cross_month_constraints.get("off_window_constraints") or {}
    if off_window_constraints:
        config_dict["off_window_constraints"] = off_window_constraints
    print('prev_month_last_is_off', prev_month_last_is_off)
    # ── 3) 병합 후 주입(금지/강제OFF 합집합) ──
    config_dict["initial_constraints"] = _merge_initial_constraints(
        base=cross_month_constraints,
        extra=allowed_constraints,
    )
    try:
        print("cp_sat_basic 엔진 호출 준비 완료")
        # ── 실행 초기에 정책 파라미터가 어떻게 적용됐는지 반드시 로그로 남긴다(추후 유지보수용) ──
        try:
            mode = str(config_dict.get("distribution_mode", "hybrid"))
            og = config_dict.get("oversupply_balance_gauge")
            mg = config_dict.get("monthly_preference_gauge")
            ow = config_dict.get("oversupply_equalize_weight")
            oe = config_dict.get("oversupply_equalize_enable")
            mw = config_dict.get("monthly_preference_weight")
            mp_cnt = len((config_dict.get("monthly_shift_preferences") or {}) if isinstance(config_dict.get("monthly_shift_preferences"), dict) else {})
            max_extra = config_dict.get("max_extra_off_days")
            off_pen = config_dict.get("extra_off_penalty_weight")
            soft_k = config_dict.get("soft_max_consecutive_work_days")
            soft_w = config_dict.get("soft_consecutive_work_penalty_weight")
            print(
                "[ShiftDistributionPolicy] "
                f"mode={mode}, oversupply_gauge={og}, monthly_pref_gauge={mg}, "
                f"oversupply_equalize=({oe},{ow}), monthly_pref_weight={mw}, "
                f"monthly_pref_cnt={mp_cnt}, "
                f"max_extra_off_days={max_extra}, extra_off_penalty_weight={off_pen}, "
                f"soft_cwork=(k={soft_k},w={soft_w})"
            )
        except Exception as _log_exc:
            print(f"[ShiftDistributionPolicy] 로그 출력 실패: {_log_exc}")
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
        # 요청 바디에서 GRADE일 때는 DB에서 grade_config를 조회해 엔진에 전달
        engine_grade_config = grade_config
        if str(getattr(req, "grade_strategy", "") or "").upper() == "GRADE":
            engine_grade_config = _fetch_grade_config_dict(
                db, current_user.office_id, current_user.group_id
            )
        cp_sat_result = generate_roster_cp_sat(
            nurses_dict,
            prefs_dict,
            config_dict,
            req.year,
            req.month,
            shift_manage_data,
            time_limit_seconds=time_limit_seconds,
            grade_strategy=req.grade_strategy,
            grade_config=engine_grade_config,
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
    shift_ids, default_map = _load_shift_mappings(db, schedule)
    for nurse_id, shifts in generated.items():
        for day_index, shift_id in enumerate(shifts):
            if shift_id != '-':
                work_date = date(req.year, req.month, day_index + 1)
                main_code = str(shift_id).strip().upper()
                if main_code == "OFF":
                    main_code = "O"
                mapped_shift = default_map.get(main_code, shift_id)
                norm_shift = _normalize_shift_id_for_save(str(mapped_shift), shift_ids)
                entry = ScheduleEntry(
                    entry_id=str(uuid.uuid4().hex)[:16],
                    schedule_id=schedule.schedule_id,
                    nurse_id=nurse_id,
                    work_date=work_date,
                    shift_id=norm_shift,
                )
                db.add(entry)
        # try:
        #     print(f"[PersistRoster] nurse={nurse_id}, saved_shifts={shifts}")
        # except Exception:
        #     pass
    db.commit()


def _count_work_assignments(generated: dict[str, list[str]] | None) -> tuple[int, int]:
    """
    생성된 근무표에서 총 셀 수와 실근무 배정 수를 계산한다.

    인자:
        generated: 엔진이 반환한 간호사별 근무 리스트 딕셔너리.
    반환:
        tuple: (전체 셀 수, 실근무 배정 수).
    예외:
        없음.
    예시:
        간호사 25명, 30일, 실근무 0건 → (750, 0).
    """
    if not isinstance(generated, dict):
        return 0, 0
    off_codes = {"-", "O", "OFF", "주"}
    total_cells = 0
    work_cells = 0
    for shifts in generated.values():
        for raw_shift in shifts or []:
            total_cells += 1
            code = str(raw_shift).strip().upper() if raw_shift is not None else "-"
            if code in off_codes:
                continue
            work_cells += 1
    return total_cells, work_cells


def _validate_generated_roster(
    generated: dict[str, list[str]] | None, roster_system
) -> str | None:
    """
    엔진 결과가 고객에게 보여줄 수 없는 수준인지 최소 검증한다.

    인자:
        generated: 엔진이 반환한 근무표(dict).
        roster_system: 위반 정보를 포함한 RosterSystem 객체 또는 None.
    반환:
        str | None: 문제가 있으면 에러 메시지, 없으면 None.
    예외:
        없음.
    예시:
        총 750칸 중 실근무 0칸이거나 위반 1500건(750×2) 이상 → 메시지 반환.
    """
    total_cells, work_cells = _count_work_assignments(generated)
    num_days = getattr(roster_system, "num_days", 0) or 0
    work_ratio = (work_cells / total_cells) if total_cells else 0.0

    if total_cells > 0 and work_cells == 0:
        return "근무 배정이 한 건도 없어 스케줄을 저장하지 않습니다."

    # 근무 배정률이 10% 미만이면 비정상으로 간주
    if total_cells > 0 and work_ratio < 0.1:
        return "근무 배정률이 10% 미만이어서 스케줄을 저장하지 않습니다."

    # 일 단위 커버리지가 전부 0인 날이 있는지 확인 (필수 인원 대비 실배정 0)
    try:
        cfg = getattr(roster_system, "config", None)
        num_days = getattr(roster_system, "num_days", 0) or 0
        shift_types = list(getattr(cfg, "shift_types", []) or [])
        off_alias = {"-", "O", "OFF", "주"}

        def _required_by_day(day_idx: int) -> dict[str, int]:
            if isinstance(getattr(cfg, "daily_shift_requirements_by_day", None), list):
                by_day = cfg.daily_shift_requirements_by_day
                if day_idx < len(by_day) and isinstance(by_day[day_idx], dict):
                    return {str(k).upper(): int(v or 0) for k, v in by_day[day_idx].items()}
            if isinstance(getattr(cfg, "daily_shift_requirements", None), dict):
                return {str(k).upper(): int(v or 0) for k, v in cfg.daily_shift_requirements.items()}
            return {}

        if cfg and shift_types and generated:
            for d in range(num_days):
                req_raw = _required_by_day(d)
                req = {
                    k: v
                    for k, v in req_raw.items()
                    if k in shift_types and k not in {"O"}
                }
                total_req = sum(req.values())
                if total_req <= 0:
                    continue

                actual = {k: 0 for k in req}
                for shifts in generated.values():
                    if not isinstance(shifts, list) or d >= len(shifts):
                        continue
                    code = str(shifts[d]).strip().upper() if shifts[d] is not None else "-"
                    if code == "주":
                        code = "O"
                    if code in actual:
                        actual[code] += 1

                total_actual = sum(actual.values())
                if total_actual == 0:
                    req_msg = ", ".join(f"{k}={v}" for k, v in req.items())
                    return (
                        f"{d + 1}일에 필수 근무 인원이 모두 미배정되었습니다. "
                        f"(요구 인원: {req_msg})"
                    )
    except Exception:
        # 검증 로직이 실패해도 저장을 막지 않고, 기존 최소 검증 결과만 사용
        pass

    return None


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

    try:
        debug_counts = {
            nid: {d: code for d, code in day_map.items()} for nid, day_map in entries_by_nurse.items()
        }
        # print(f"[RosterResponse] schedule_id={schedule.schedule_id}, entries_by_nurse={debug_counts}")
    except Exception:
        pass

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


def _normalize_shift_id_for_save(raw_shift: str, valid_shift_ids: set[str]) -> str:
    """Shift ID를 저장 시 안전하게 정규화합니다.

    - raw_shift 그대로가 유효하면 그대로 사용
    - 대문자 변환본이 유효하면 대문자로 사용
    - 대소문자 무시 매칭이 되면 DB에 존재하는 원본 케이스를 보존
    - 둘 다 없으면 원본을 반환(최후 fallback)
    """
    if raw_shift in valid_shift_ids:
        return raw_shift
    upper = raw_shift.upper()
    if upper in valid_shift_ids:
        return upper
    for sid in valid_shift_ids:
        if sid.upper() == upper:
            return sid
    return raw_shift


def _load_shift_mappings(db: Session, schedule) -> tuple[set[str], dict[str, str]]:
    """office/group의 Shift 정보를 불러와 저장용 매핑을 생성한다.

    Args:
        db: DB 세션
        schedule: 현재 스케줄 객체(office_id, group_id 참조)

    Returns:
        (shift_ids, default_map):
            - shift_ids: 유효한 shift_id 집합
            - default_map: 메인코드(D/E/N/O/주) → shift_id 매핑
    """
    shifts_db = (
        db.query(Shift)
        .filter(Shift.group_id == schedule.group_id, Shift.office_id == schedule.office_id)
        .all()
    )
    shift_ids = {s.shift_id for s in shifts_db}
    default_map = _build_default_shift_mapping(shifts_db)
    return shift_ids, default_map


def _build_default_shift_mapping(shifts: list[Shift]) -> dict[str, str]:
    """Shift.default_shift를 메인코드(D/E/N/O/주)로 삼아 실제 shift_id 매핑을 구성한다.

    Args:
        shifts: 해당 그룹/오피스의 Shift 레코드 목록

    Returns:
        메인코드(D/E/N/O/주) → shift_id 매핑
    """
    mapping: dict[str, str] = {}
    for s in shifts:
        default_code = str(getattr(s, "default_shift", "") or "").strip().upper()
        if default_code == "OFF":
            default_code = "O"
        if default_code not in {"D", "E", "N", "O", "주"}:
            continue
        if default_code not in mapping:
            mapping[default_code] = str(s.shift_id)
    return mapping


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

def _apply_distribution_policy_from_req(config_dict: dict, req) -> None:
    """req(임시 UI 대체)로 전달된 분배 정책 파라미터를 config_dict에 반영한다.

    목표:
        - oversupply(여유 인원)가 D로만 쏠리지 않도록 "일별 균등 분배" 목적함수를 활성화한다.
        - Wanted(날짜 지정형 선호)는 기존 경로(강한 선호)로 반영하고,
          월단위 선호(개인 입력)는 잔여 자유도에서 약하게 유도한다.

    Args:
        config_dict: 엔진에 넘길 설정 dict(최종적으로 cp_sat_basic에 전달됨)
        req: `RosterRequest` 또는 hold_generate 요청 모델
    """
    # ── 모드 정규화 ──
    mode = str(getattr(req, "distribution_mode", None) or config_dict.get("distribution_mode") or "hybrid").lower()
    if mode == "auto":
        mode = "hybrid"
    if mode not in {"hybrid", "balanced", "preference", "off"}:
        mode = "hybrid"
    config_dict["distribution_mode"] = mode

    # ── 게이지(0~10) ──
    og = getattr(req, "oversupply_balance_gauge", None)
    mg = getattr(req, "monthly_preference_gauge", None)
    og = max(0, min(10, int(og if og is not None else 6)))
    mg = max(0, min(10, int(mg if mg is not None else 3)))
    config_dict["oversupply_balance_gauge"] = og
    config_dict["monthly_preference_gauge"] = mg

    # ── 게이지 → weight 매핑 ──
    # 기존 파라미터(oversupply_equalize_weight)를 그대로 사용하되, 게이지를 통해 일관되게 제어한다.
    def _g2w(g: int, cap: int, power: float = 1.7) -> int:
        g_norm = g / 10.0
        return int(round(cap * (g_norm ** power)))

    oversupply_w = _g2w(og, cap=220)
    monthly_w = _g2w(mg, cap=140)

    # ── 모드별 on/off ──
    if mode == "off":
        config_dict["oversupply_equalize_enable"] = False
        config_dict["oversupply_equalize_weight"] = 0
        config_dict["monthly_preference_weight"] = 0
    elif mode == "balanced":
        config_dict["oversupply_equalize_enable"] = og > 0
        config_dict["oversupply_equalize_weight"] = oversupply_w
        config_dict["monthly_preference_weight"] = 0
    elif mode == "preference":
        # 선호 우선: 균등은 최소 가드레일만 남긴다(일별 D 쏠림 방지)
        config_dict["oversupply_equalize_enable"] = og > 0
        config_dict["oversupply_equalize_weight"] = min(oversupply_w, 60)
        config_dict["monthly_preference_weight"] = monthly_w
    else:
        # hybrid
        config_dict["oversupply_equalize_enable"] = og > 0
        config_dict["oversupply_equalize_weight"] = oversupply_w
        config_dict["monthly_preference_weight"] = monthly_w

    # ── 월단위 선호 payload(개인 입력) ──
    msp = getattr(req, "monthly_shift_preferences", None)
    if isinstance(msp, dict):
        config_dict["monthly_shift_preferences"] = msp
    else:
        config_dict.setdefault("monthly_shift_preferences", {})

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
    (
        nurses_in_group,
        preferences,
        special_shift_requests,
        special_shift_map,
    ) = _collect_nurses_and_preferences(db, req, current_user)
    active_nurses_in_group = [
        n for n in nurses_in_group if getattr(n, "active", 1) != 0
    ]
    if len(active_nurses_in_group) != len(nurses_in_group):
        excluded_names = [
            f"{getattr(n, 'name', '?')}({getattr(n, 'nurse_id', '?')})"
            for n in nurses_in_group
            if getattr(n, "active", 1) == 0
        ]
        print(f"[RosterCreate] 비활성 간호사 엔진 제외: {excluded_names}")
    nurses_in_group = active_nurses_in_group
    # _debug_log(
    #     "collect_done",
    #     {
    #         "nurses_total": len(nurses_in_group),
    #         "preferences": len(preferences),
    #     },
    # )
    fixed_nurses, engine_nurses = _split_fixed_nurses(nurses_in_group)
    # _debug_log(
    #     "nurse_split",
    #     {
    #         "total": len(nurses_in_group),
    #         "fixed": len(fixed_nurses),
    #         "engine": len(engine_nurses),
    #     },
    # )
    print('fixed_nurses', [n.__dict__ for n in fixed_nurses])
    # 고정 근무는 주말 휴무 대상만 허용
    invalid = [n for n in fixed_nurses if not bool(getattr(n, "is_weekend_off", False))]
    # print('invalid', [n.__dict__ for n in invalid])
    if invalid:
        raise HTTPException(status_code=400, detail="주말 휴무 true만 고정 shift설정이 가능하다")
    try:
        weekend_off_names = [
            f"{getattr(n, 'name', '?')}({getattr(n, 'nurse_id', '?')})"
            for n in nurses_in_group
            if bool(getattr(n, "is_weekend_off", False))
        ]
        print(f"[WeekendOff] 대상 간호사({len(weekend_off_names)}명): {weekend_off_names}")
    except Exception as e:
        print(f"[WeekendOff] 대상 간호사 로깅 실패: {e}")
    month_start = date(req.year, req.month, 1)
    days_in_month = calendar.monthrange(req.year, req.month)[1]
    active_range_candidates = {
        str(n.nurse_id): _active_range_in_month(n, month_start, days_in_month)
        for n in engine_nurses
    }
    excluded_engine_nurses = [
        n for n in engine_nurses if active_range_candidates.get(str(n.nurse_id)) is None
    ]
    if excluded_engine_nurses:
        excluded_names = [
            f"{getattr(n, 'name', '?')}({getattr(n, 'nurse_id', '?')})"
            for n in excluded_engine_nurses
        ]
        print(f"[JoinDate] 대상 월 기준 활동 불가 간호사 제외: {excluded_names}")
    engine_nurses = [
        n for n in engine_nurses if active_range_candidates.get(str(n.nurse_id)) is not None
    ]
    engine_nurse_ids = {str(n.nurse_id) for n in engine_nurses}
    preferences = [p for p in preferences if str(p.get("nurse_id")) in engine_nurse_ids]
    nurses_for_engine = engine_nurses
    latest_config = _fetch_latest_config(db, req, current_user)
    shift_manage_data, daily_shift_requirements, daily_shift_requirements_by_day = _build_shift_manage_and_requirements(
        db, current_user, latest_config, req
    )
    # daily_shift_requirements를 config에 주입해서 엔진 호출
    config_dict = latest_config.__dict__ if latest_config else {}
    config_dict['daily_shift_requirements'] = daily_shift_requirements
    # 일자별 요구치 우선 적용
    config_dict['daily_shift_requirements_by_day'] = daily_shift_requirements_by_day
    # 요청에서 not_one_night가 들어오면 우선 적용 (없으면 DB 설정 유지)
    if getattr(req, "not_one_night", None) is not None:
        config_dict["not_one_night"] = bool(req.not_one_night)
    if bool(config_dict.get("two_offs_after_two_nig")) or bool(config_dict.get("two_offs_after_three_nig")):
        print(
            "[OffReason] N연속 후 2OFF 하드 활성화: "
            f"2N→2O={bool(config_dict.get('two_offs_after_two_nig'))}, "
            f"3N→2O={bool(config_dict.get('two_offs_after_three_nig'))}"
        )
    fixed_nurse_ids = {str(n.nurse_id) for n in fixed_nurses}
    engine_special_shift_requests = [
        r
        for r in special_shift_requests
        if str(r.get("nurse_id")) in engine_nurse_ids
    ]
    fixed_special_shift_requests = [
        r for r in special_shift_requests if str(r.get("nurse_id")) in fixed_nurse_ids
    ]
    # 특별 근무(근무형) shift 코드 주입
    special_fixed_cells, has_special_working = _build_special_fixed_cells(
        requests=engine_special_shift_requests,
        nurse_idx_map=_build_engine_nurse_index_map(engine_nurses),
        special_shift_map=special_shift_map,
        active_range_map=active_range_candidates,
        days_in_month=days_in_month,
    )
    _inject_special_work_code(config_dict, has_special_working)
    # OFF 상한/패널티 기본값 보정 (None 방지)
    if config_dict.get("max_extra_off_days") is None:
        config_dict["max_extra_off_days"] = 1
    if config_dict.get("extra_off_penalty_weight") is None:
        config_dict["extra_off_penalty_weight"] = 80
    # ── 프리셉터 게이지(0~10) → 파라미터 매핑 ──
    
    _apply_preceptor_gauge(config_dict, config_dict['preceptor_gauge'])
    _apply_team_balance_gauge(config_dict, config_dict.get('team_balance_gauge'))
    _apply_distribution_policy_from_req(config_dict, req)
    # 경계 제약 기능 기본값
    config_dict.setdefault("cross_month_hard_rules_enable", True)
    config_dict.setdefault("cross_month_lookback_days", 6)
    config_dict.setdefault("allow_override_by_law", False)
    config_dict.setdefault("lookahead_days", 0)
    print("cp_sat_basic 엔진으로 근무표 생성 시작")
    # _debug_log(
    #     "config_ready",
    #     {
    #         "weekly_off_group": config_dict.get("weekly_off_group"),
    #         "weekend_off_only_enable": config_dict.get("weekend_off_only_enable"),
    #         "max_extra_off_days": config_dict.get("max_extra_off_days"),
    #         "extra_off_penalty_weight": config_dict.get("extra_off_penalty_weight"),
    #     },
    # )

    # ── 주휴 고정 셀(최우선) 계산 후 엔진에 OFF(O)로 주입 ──
    weekly_off_warnings: list[dict] = []
    weekly_off_conflicts: list[dict] = []
    weekly_off_map: dict[str, set[int]] = {}
    weekly_off_fixed_cells: list[dict] = []
    active_range_map = {
        str(n.nurse_id): _active_range_in_month(n, month_start, days_in_month)
        for n in nurses_for_engine
    }

    # print('active_range_map', active_range_map)
    weekly_off_map, weekly_off_warnings = _compute_weekly_off_day_indices_for_month(
        db=db,
        office_id=current_user.office_id,
        group_id=current_user.group_id,
        year=req.year,
        month=req.month,
    )
    # weekly_off_settings의 activate 값을 config_dict에 추가
    try:
        if _table_exists(db, "weekly_off_settings"):
            setting = db.execute(
                text(
                    "SELECT TOP 1 activate "
                    "FROM weekly_off_settings "
                    "WHERE office_id = :office_id AND group_id = :group_id"
                ),
                {"office_id": current_user.office_id, "group_id": current_user.group_id},
            ).fetchone()
            if setting:
                config_dict["weekly_off_settings_activate"] = bool(setting.activate)
            else:
                config_dict["weekly_off_settings_activate"] = False
        else:
            config_dict["weekly_off_settings_activate"] = False
    except Exception:
        config_dict["weekly_off_settings_activate"] = False
    weekly_off_map = {k: v for k, v in weekly_off_map.items() if k in engine_nurse_ids}

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

    # ── 프리셉티 주휴 처리 (preceptee_on=True 일 때) ──
    # 프리셉티는 프리셉터를 100% 팔로우하므로 별도 주휴 고정 셀이 불필요.
    # 고정 셀이 있으면 result_mapping에서 fixed_lookup이 우선하여 "주" 코드가 그대로 노출됨.
    # → 프리셉티를 weekly_off_map에서 제거하여 고정 셀 미생성 + 팔로우 동기화로 OFF 처리.
    if config_dict.get('preceptee_on', False):
        for nurse in nurses_for_engine:
            nid = str(nurse.nurse_id)
            pid = getattr(nurse, 'preceptor_id', None)
            if not pid:
                continue
            if nid in weekly_off_map:
                print(f"[WeeklyOff] 프리셉티 {nurse.name}({nid}) → 주휴 고정 셀 제거 (프리셉터 팔로우로 대체)")
                weekly_off_map.pop(nid, None)

    if weekly_off_map:
        nurse_idx_map = _build_engine_nurse_index_map(nurses_for_engine)
        print(f"[WeeklyOff] 주휴 고정 셀 생성: {len(weekly_off_map)}명")
        for nurse_id, day_set in weekly_off_map.items():
            n_idx = nurse_idx_map.get(nurse_id)
            if n_idx is None:
                print(f"[WeeklyOff] ⚠️ 간호사 {nurse_id}: nurse_index 매핑 실패 (주휴 무시)")
                continue
            nurse_name = next((n.name for n in nurses_in_group if str(n.nurse_id) == str(nurse_id)), nurse_id)
            day_list = sorted(day_set)
            print(f"[WeeklyOff] 간호사 {nurse_name}({nurse_id}, index={n_idx}): 주휴 day_idx={day_list} (실제 날짜: {[d+1 for d in day_list]})")
            for d in day_list:
                weekly_off_fixed_cells.append({"nurse_index": n_idx, "day_index": d, "shift": "O"})
    config_dict["weekly_off_map"] = {k: sorted(list(v)) for k, v in weekly_off_map.items()}

    # 룩어헤드: 다음 달 1~K일 주휴 고정 OFF 셀(당월 weekly_off_by_idx와 별도로 재계산)
    K_lookahead = int(config_dict.get("lookahead_days") or 0)
    if K_lookahead > 0:
        logger.info(
            "[Lookahead] K=%s 적용 (lookahead_days), 다음 달 1~%s일 구간 사용",
            K_lookahead,
            K_lookahead,
        )
        try:
            config_dict["lookahead_weekly_off_cells"] = _compute_lookahead_weekly_off_cells(
                db=db,
                office_id=current_user.office_id,
                group_id=current_user.group_id,
                year=req.year,
                month=req.month,
                nurses_for_engine=nurses_for_engine,
                D_phys=days_in_month,
                K_lookahead=K_lookahead,
            )
        except Exception as e:
            logger.warning(
                "[Lookahead] 룩어헤드 주휴 셀 계산 실패(비활성화): %s",
                e,
                exc_info=True,
            )
            config_dict["lookahead_weekly_off_cells"] = set()
    else:
        logger.info("[Lookahead] lookahead_days=0 → 룩어헤드 비활성화")
        config_dict["lookahead_weekly_off_cells"] = set()

    # _debug_log(
    #     "weekly_off_built",
    #     {
    #         "weekly_off_enabled": bool(config_dict.get("weekly_off_group", False)),
    #         "map_count": len(weekly_off_map),
    #         "fixed_cells": len(weekly_off_fixed_cells),
    #         "warnings": len(weekly_off_warnings),
    #     },
    # )

    generated: dict[str, list[str]] = {}
    satisfaction_data = {}
    roster_system = None

    combined_fixed_cells = []
    if weekly_off_fixed_cells:
        combined_fixed_cells.extend(weekly_off_fixed_cells)
    if special_fixed_cells:
        combined_fixed_cells.extend(special_fixed_cells)

    # ── fixed_wanted_use_yn 설정에 따른 확정 원티드 하드 고정 처리 ──
    _fw_use_yn = bool(getattr(latest_config, 'fixed_wanted_use_yn', False))
    _fw_source = "FixedWantedEntry" if db.query(FixedWantedEntry).filter(
        FixedWantedEntry.group_id == current_user.group_id,
        FixedWantedEntry.year == req.year,
        FixedWantedEntry.month == req.month,
    ).first() is not None else "WantedRequest"
    print(f"[RosterCreate] fixed_wanted_use_yn={_fw_use_yn}, 데이터 출처={_fw_source}")
    if _fw_use_yn:
        all_fixed_entries = db.query(FixedWantedEntry).filter(
            FixedWantedEntry.group_id == current_user.group_id,
            FixedWantedEntry.year == req.year,
            FixedWantedEntry.month == req.month,
            FixedWantedEntry.is_applied == True,
        ).all()
        fw_nurse_idx_map = _build_engine_nurse_index_map(nurses_for_engine)
        fw_fixed_cells = []
        _fw_skip_special = 0
        _fw_skip_nurse = 0
        _fw_skip_range = 0
        _fw_code_counts: dict[str, int] = {}
        for fe in all_fixed_entries:
            shift_code_raw = str(fe.shift_id or "").strip()
            shift_code = shift_code_raw.upper()
            # special_shift_map 에 있는 코드는 이미 special_fixed_cells 에서 처리됨
            if shift_code in special_shift_map:
                _fw_skip_special += 1
                continue
            nurse_id = str(fe.nurse_id)
            n_idx = fw_nurse_idx_map.get(nurse_id)
            if n_idx is None:
                _fw_skip_nurse += 1
                continue
            day_idx = fe.shift_date.day - 1
            if day_idx < 0 or day_idx >= days_in_month:
                continue
            if active_range_candidates:
                rng = active_range_candidates.get(nurse_id)
                if rng:
                    start_idx, end_idx = rng
                    if day_idx < start_idx or day_idx > end_idx:
                        _fw_skip_range += 1
                        continue
            fw_fixed_cells.append({
                "nurse_index": n_idx,
                "day_index": day_idx,
                "shift": shift_code_raw,
                "shift_type": "근무",
            })
            _fw_code_counts[shift_code] = _fw_code_counts.get(shift_code, 0) + 1
        if fw_fixed_cells:
            combined_fixed_cells.extend(fw_fixed_cells)
        print(
            f"[RosterCreate] fixed_wanted_use_yn=True 결과: "
            f"총 조회={len(all_fixed_entries)}건, "
            f"하드 고정={len(fw_fixed_cells)}건 {dict(_fw_code_counts)}, "
            f"스킵(특수코드 중복={_fw_skip_special}, 엔진 미포함 간호사={_fw_skip_nurse}, 활동범위 밖={_fw_skip_range})"
        )
    else:
        print(
            f"[RosterCreate] fixed_wanted_use_yn=False: "
            f"특수코드만 하드 고정={len(special_fixed_cells) if special_fixed_cells else 0}건 (출처: {_fw_source}), "
            f"DENO/기타는 선호도로 반영"
        )

    off_fixed_summary = _summarize_off_fixed_cells(
        weekly_off_fixed_cells=weekly_off_fixed_cells,
        special_fixed_cells=special_fixed_cells,
        nurses_in_group=nurses_in_group,
    )
    if off_fixed_summary:
        print("[OffReason] 고정 OFF 요약(주휴/특수요청)")
        for nurse_id, info in off_fixed_summary.items():
            weekly_days = [d + 1 for d in info.get("weekly_off_days", [])]
            special_days = [d + 1 for d in info.get("special_off_days", [])]
            total = len(weekly_days) + len(special_days)
            print(
                f"[OffReason] {info.get('name', nurse_id)}({nurse_id}) "
                f"weekly_off={weekly_days}, special_off={special_days}, total_fixed_off={total}"
            )
    off_exception_cells = set()
    for c in combined_fixed_cells:
        try:
            n_idx = c.get("nurse_index")
            d_idx = c.get("day_index")
            shift_code = str(c.get("shift") or "").upper()
            shift_type = str(c.get("shift_type") or "").strip()
        except Exception:
            continue
        if n_idx is None or d_idx is None:
            continue
        if shift_code in {"O", "OFF", "주"} or shift_type in {"휴가", "공가"}:
            off_exception_cells.add((n_idx, d_idx))
    if off_exception_cells:
        config_dict["off_exception_cells"] = sorted(list(off_exception_cells))

    # # ── 디버그: 특정 간호사/일자 기준 OFF 현황 확인 ──
    # try:
    #     watch_nurse_ids = {"442918"}  # 박지은
    #     watch_day_indices = {16, 17}  # 17일(0-based 16), 18일(0-based 17)
    #     nurse_idx_map = _build_engine_nurse_index_map(nurses_for_engine)
    #     idx_to_id = {v: k for k, v in nurse_idx_map.items()}
    #     off_days_by_nurse: dict[str, list[int]] = {}
    #     for n_idx, d_idx in off_exception_cells:
    #         nurse_id = idx_to_id.get(n_idx)
    #         if not nurse_id:
    #             continue
    #         off_days_by_nurse.setdefault(nurse_id, []).append(d_idx)
    #     for nurse_id in watch_nurse_ids:
    #         weekly_days = sorted(list(weekly_off_map.get(nurse_id, set())))
    #         exception_days = sorted(off_days_by_nurse.get(nurse_id, []))
    #         _debug_log(
    #             "watch_off_days",
    #             {
    #                 "nurse_id": nurse_id,
    #                 "weekly_off_days": [d + 1 for d in weekly_days],
    #                 "off_exception_days": [d + 1 for d in exception_days],
    #             },
    #         )

    #     fixed_day_counts: dict[int, int] = {}
    #     exception_day_counts: dict[int, int] = {}
    #     for c in combined_fixed_cells:
    #         try:
    #             d_idx = int(c.get("day_index"))
    #         except Exception:
    #             continue
    #         fixed_day_counts[d_idx] = fixed_day_counts.get(d_idx, 0) + 1
    #     for _, d_idx in off_exception_cells:
    #         exception_day_counts[d_idx] = exception_day_counts.get(d_idx, 0) + 1
    #     _debug_log(
    #         "watch_day_counts",
    #         {
    #             "days": {
    #                 str(d + 1): {
    #                     "fixed_cells": fixed_day_counts.get(d, 0),
    #                     "off_exception_cells": exception_day_counts.get(d, 0),
    #                 }
    #                 for d in sorted(watch_day_indices)
    #             }
    #         },
    #     )
    # except Exception:
    #     pass

    if nurses_for_engine:
        # _debug_log(
        #     "cp_sat_start",
        #     {
        #         "weekly_off_enabled": bool(config_dict.get("weekly_off_group", False)),
        #         "weekly_off_cells": len(weekly_off_fixed_cells),
        #         "special_fixed_cells": len(special_fixed_cells),
        #         "combined_fixed_cells": len(combined_fixed_cells),
        #         "engine_nurses": len(nurses_for_engine),
        #     },
        # )
        generated, satisfaction_data, roster_system = _run_cp_sat_basic(
            db,
            current_user,
            nurses_for_engine,
            preferences,
            latest_config,
            req,
            shift_manage_data,
            fixed_cells=combined_fixed_cells if combined_fixed_cells else None,
            time_limit_seconds=60,
            config_override=config_dict,
        )
        # _debug_log(
        #     "cp_sat_end",
        #     {
        #         "generated_keys": len(generated) if isinstance(generated, dict) else None,
        #         "roster_system_ready": roster_system is not None,
        #     },
        # )

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

    # 고정 근무 간호사 스케줄 병합 (평일 fixed_shift, 주말 OFF/주)
    fixed_roster = _build_fixed_shift_roster(
        fixed_nurses,
        req.year,
        req.month,
        weekday_off_code="O",
        sunday_code="주",
        shift_lookup=_load_shift_lookup(db, current_user.office_id, current_user.group_id),
        weekly_off_active=bool(config_dict.get("weekly_off_settings_activate", False)),
    )
    fixed_roster = _overlay_fixed_roster_with_special_requests(
        fixed_roster=fixed_roster,
        requests=fixed_special_shift_requests,
        year=req.year,
        month=req.month,
    )
    if isinstance(generated, dict):
        generated.update(fixed_roster)
    else:
        generated = fixed_roster

    validation_error = _validate_generated_roster(generated, roster_system)
    if validation_error:
        db.delete(schedule)
        db.commit()
        raise Exception(validation_error)

    _persist_entries(db, schedule, generated, req)
    roster_data = _build_roster_response(db, schedule, req, nurses_in_group)
    roster_data["weekly_off_conflicts"] = weekly_off_conflicts
    roster_data["weekly_off_warnings"] = weekly_off_warnings
    return roster_data


# 


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
