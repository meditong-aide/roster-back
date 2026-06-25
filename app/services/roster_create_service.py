"""
근무표 생성 관련 서비스 로직 모듈
- DB 쿼리, 데이터 가공, 엔진 호출 등 라우터에서 분리
- 모든 함수는 한글 docstring, 한글 print/logging, PEP8 스타일 적용
"""
from __future__ import annotations

import logging
import re
import time
from copy import deepcopy
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
    Team,
    Wanted,
    WantedRequest,
)
from schemas.roster_schema import RosterRequest
from routers.utils import get_days_in_month, Timer
from datetime import date, datetime, timedelta
import json
import uuid
from sqlalchemy import func, or_
from collections import defaultdict
import calendar
from sqlalchemy import text
from db.client2 import get_db
from services.cp_sat.off_policy import resolve_effective_off_days
from services.group_access import caller_is_head_nurse, resolve_effective_group
from services.cp_sat.off_swap import postprocess_off_swap
from services.assignment_service import get_active_assignments_for_month, flush_pending_transfers
from services.day_windows import build_blocked_days
from services.nurse_monthly_limit_service import fetch_effective_monthly_limits_by_nurse
from services.cp_sat.mid_feasibility import validate_mid_hard_feasibility as _validate_mid_hard_feasibility_impl
from services.semantics import attach_reason_code_ontology

logger = logging.getLogger(__name__)
# from db.client2 import _get_mssql_session


# CP-SAT 기반 엔진들 import
try:
    from services.random_sampling import generate_roster
    from services.cp_sat_basic import generate_roster_cp_sat
    CPSAT_AVAILABLE = True
except ImportError as e:
    print(f"CP-SAT 엔진 import 실패: {e}")
    CPSAT_AVAILABLE = False

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
    """그룹 내 간호사 목록, 선호도, 특별 고정 요청을 수집한다. (FixedWantedEntry 존재 시 자동 사용).

    nurse 풀: source(Nurse.group_id == caller)만 반환.
    inbound nurse 합치기와 target_* overlay 는 generate_roster_service 의 line 3482~3508 전용
    분기에서 처리한다 (여기서 미리 합치면 _inbound_nurse_ids 차집합이 0이 되어 overlay 분기가
    발동하지 않는 버그가 발생).
    """
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
    # source nurse_ids + inbound nurse_ids (caller 관할 fixed_wanted 조회용).
    # nurses_in_group 에는 source 만 둠 — inbound nurse 객체 추가는 generate_roster_service 의
    # line 3482-3508 단일 분기에서 처리 (target_* overlay 동반).
    # 여기서는 fixed_wanted_entries 누락 방지 위해 nurse_ids 만 inbound 까지 확장.
    from db.models import NurseAssignment as _NA
    from services.nurse_service import _INBOUND_REASONS
    _source_ids = [n.nurse_id for n in nurses_in_group]
    _inbound_ids = [
        r[0] for r in db.query(_NA.nurse_id).filter(
            _NA.target_group_id == current_user.group_id,
            _NA.status == "active",
            _NA.reason.in_(_INBOUND_REASONS),
        ).all()
        if r[0] not in _source_ids
    ]
    nurse_ids = _source_ids + _inbound_ids
    if _inbound_ids:
        print(f"[RosterCreate] fixed_wanted 조회 대상 inbound nurse 추가: {_inbound_ids}")
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
                pair_data = [{"id": p.target_id, "weight": p.score if p.score is not None else 0} for p in pair_rows]

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
        pair_data = [{"id": p.target_id, "weight": p.score if p.score is not None else 0} for p in pair_rows]

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
    """요청의 config_id 우선, 없으면 그룹 최신 config을 가져온다.

    cross-group 가드: req.config_id 가 현재 그룹 소유가 아니면(타 그룹 config)
    무시하고 현재 그룹 최신 config 으로 폴백한다. 같은 office 내 다른 병동의
    config_id 가 그룹 전환 후 잔존 상태로 전달돼 schedule 에 오염 스탬핑되던
    버그(타 그룹 config_id 무검증 사용) 방어.
    """
    latest_config = None
    if req.config_id:
        latest_config = (
            db.query(RosterConfig).filter(RosterConfig.config_id == req.config_id).first()
        )
        if latest_config is not None and str(latest_config.group_id) != str(
            current_user.group_id
        ):
            logger.warning(
                "[_fetch_latest_config] cross-group config_id 무시: config_id=%s "
                "(config_group=%s) != current_group=%s → 그룹 최신 config 로 폴백",
                req.config_id,
                latest_config.group_id,
                current_user.group_id,
            )
            latest_config = None
    if latest_config is None:
        latest_config = (
            db.query(RosterConfig)
            .filter(RosterConfig.group_id == current_user.group_id)
            .order_by(RosterConfig.created_at.desc())
            .first()
        )
    return latest_config


def _ensure_grade1_default(constraints: dict | None) -> dict:
    """[GRADE_DEFAULT_111] 정책(2026-06): 모든 그룹은 grade 등록 여부와 무관하게
    grade-1 이 D/E/N 각 1명 이상 배치되도록 '기본 floor'를 보장한다.

    - 등록값이 더 크면(예: grade-1 D:2) 그대로 유지(floor 만 보장, override 아님).
    - grade 인원이 부족하거나 미등록이면 _add_minimum_constraints 의 누적 cascade 가
      자동으로 하위 등급/soft 로 흘려보내므로 infeasible 위험 없음.
    원복: 이 함수 호출부(_fetch_grade_config_dict)에서 래핑만 제거.
    """
    out = dict(constraints or {})
    for sc in ("D", "E", "N"):
        tier = dict(out.get(sc) or {})
        try:
            cur = int(tier.get("1", 0) or 0)
        except (TypeError, ValueError):
            cur = 0
        if cur < 1:
            tier["1"] = 1
        out[sc] = tier
    return out


def _fetch_grade_config_dict(db: Session, office_id: str, group_id: str) -> dict:
    """그룹의 Grade 설정을 엔진 전달용 dict로 구성한다.

    Notes:
        - DB에 설정이 없으면 기본값을 반환한다(grade-1 D/E/N 각 1 floor).
        - Grade 제약은 `cp_sat_basic`에서 grade_strategy="GRADE/COMBINED"일 때 적용된다.
    """
    def _default() -> dict:
        return {
            "use_dynamic_scaling": True,
            "allow_soft_fallback": True,
            "constraints_json": _ensure_grade1_default({}),
            "constraints_max_json": {},
        }

    def _safe_json_obj(raw, field_name: str) -> dict:
        if raw is None:
            return {}
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            txt = raw.strip()
            if not txt:
                return {}
            try:
                parsed = json.loads(txt)
            except (json.JSONDecodeError, TypeError) as e:
                sample = txt[:120]
                print(
                    "[GradeConfig][WARN] "
                    f"field={field_name} office_id={office_id} group_id={group_id} "
                    f"len={len(txt)} sample={sample!r} parse_error={e}"
                )
                return {}
            return parsed if isinstance(parsed, dict) else {}
        return {}

    # JSON 컬럼에 손상 데이터가 있을 수 있으므로 ORM(JSON 디코딩) 대신 raw text로 안전 조회한다.
    try:
        row = db.execute(
            text(
                """
                SELECT TOP 1
                    use_dynamic_scaling,
                    allow_soft_fallback,
                    CAST(constraints_json AS NVARCHAR(MAX)) AS constraints_json_text,
                    CAST(constraints_max_json AS NVARCHAR(MAX)) AS constraints_max_json_text
                FROM roster_grade_config
                WHERE office_id = :office_id AND group_id = :group_id
                ORDER BY config_id DESC
                """
            ),
            {"office_id": office_id, "group_id": group_id},
        ).fetchone()
    except Exception as e:
        print(f"[GradeConfig][WARN] raw 조회 실패. 기본값 사용: {e}")
        return _default()

    if not row:
        return _default()

    return {
        "use_dynamic_scaling": bool(getattr(row, "use_dynamic_scaling", True)),
        "allow_soft_fallback": bool(getattr(row, "allow_soft_fallback", False)),
        # [GRADE_DEFAULT_111] 등록 config 에도 grade-1 D/E/N 각 1 floor 를 보장.
        "constraints_json": _ensure_grade1_default(
            _safe_json_obj(getattr(row, "constraints_json_text", None), "constraints_json")
        ),
        "constraints_max_json": _safe_json_obj(getattr(row, "constraints_max_json_text", None), "constraints_max_json"),
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
    # [ALWAYS_COMBINED] 정책(2026-06): 전략은 항상 COMBINED(team+grade 동시).
    #   프론트/DB의 grade_strategy 컬럼이 BASE 여도 백엔드가 COMBINED 로 해석하여
    #   roster_grade_config(grade min/max)를 항상 로드·적용한다.
    #   - grade_config 에 제약이 없으면 grade 항은 자동 no-op(부작용 없음).
    #   - team 항은 team_min/team 데이터 있을 때만 활성(없으면 no-op).
    #   원복: 이 블록만 제거하면 아래 레거시(컬럼 우선 + 구버전 폴백) 로직으로 복귀.
    _gc_always = _fetch_grade_config_dict(db, office_id, group_id)
    return "COMBINED", _gc_always

    # 1) DB 컬럼 우선
    s = _fetch_grade_strategy_from_roster_config(db, roster_config_id)
    if s in ("BASE", "TEAM", "GRADE", "COMBINED"):
        if s in ("GRADE", "COMBINED"):
            gc = _fetch_grade_config_dict(db, office_id, group_id)
            return s, gc
        return s, None

    # 2) 구버전 폴백(요청 바디 말고 config_dict 기반)
    if bool(config_dict.get("team_balance_enable", False)):
        return "TEAM", None

    gc = _fetch_grade_config_dict(db, office_id, group_id)
    if bool((gc or {}).get("constraints_json") or {}) or bool((gc or {}).get("constraints_max_json") or {}):
        return "GRADE", gc
    return "BASE", None


def _has_any_grade_constraints(grade_config: dict | None) -> bool:
    gc = grade_config or {}
    return bool(
        (gc.get("constraints_json") or gc.get("constraints") or {})
        or (gc.get("constraints_max_json") or gc.get("constraints_max") or {})
    )


def _select_effective_grade_strategy(
    req_strategy: str,
    resolved_strategy: str,
    grade_config: dict | None,
) -> str:
    req = str(req_strategy or "").upper()
    resolved = str(resolved_strategy or "BASE").upper()

    if req == "COMBINED" and _has_any_grade_constraints(grade_config):
        return "COMBINED"
    if req == "GRADE" and _has_any_grade_constraints(grade_config):
        return "GRADE"
    if req == "TEAM":
        return "TEAM"
    if req == "BASE":
        return "BASE"
    return resolved

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
        # 중복행이 남아있어도 결정적이도록 id 까지 정렬: 같은 슬롯이면 최대 id(최근 저장) 행이
        # 마지막에 와서 manpower 를 덮어쓴다(코드 합집합은 _build_code_to_main_map 가 전 행 병합).
        .order_by(ShiftManage.shift_slot.asc(), ShiftManage.id.asc())
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

    def _row_to_day_counts_max(row: DailyShift) -> dict[str, int]:
        # max_enabled=False 면 *_count_max 값 무관 0 반환 (상한 미사용 모드).
        # daily_shift_service 의 자동 reset 로직이 누락된 비정합 row 도 안전하게 0 처리.
        if not bool(getattr(row, 'max_enabled', False)):
            counts = {'D': 0, 'E': 0, 'N': 0}
            if use_mid:
                counts['M'] = 0
            return counts
        counts = {
            'D': int(getattr(row, 'd_count_max', 0) or 0),
            'E': int(getattr(row, 'e_count_max', 0) or 0),
            'N': int(getattr(row, 'n_count_max', 0) or 0),
        }
        if use_mid:
            counts['M'] = int(getattr(row, 'm_count_max', 0) or 0)
        return counts

    # day→counts 맵 구성 후 리스트로 변환(0-index)
    by_day: dict[int, dict[str, int]] = {}
    by_day_max: dict[int, dict[str, int]] = {}
    for row in rows:
        day_no = int(getattr(row, 'day', 0) or 0)
        by_day[day_no] = _row_to_day_counts(row)
        by_day_max[day_no] = _row_to_day_counts_max(row)
    daily_shift_requirements_by_day = [
        by_day.get(d, dict(daily_shift_requirements))
        for d in range(1, days_in_month + 1)
    ]
    # max 기본값: 0 = 상한 없음
    _empty_max = {k: 0 for k in daily_shift_requirements}
    daily_shift_requirements_max_by_day = [
        by_day_max.get(d, dict(_empty_max))
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
    return shift_manage_data, daily_shift_requirements, daily_shift_requirements_by_day, daily_shift_requirements_max_by_day

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


def _clip_active_range_for_leaves(
    active_range_map: dict,
    leave_assignments: list,
    month_start: date,
    days_in_month: int,
) -> list[str]:
    """휴직/퇴사 active assignment 의 start_date 를 active_range 에 반영.

    nurse_assignment.reason in ("휴직", "퇴사") 인 행을 받아 해당 nurse 의
    active_range 를 [s_idx, leave_start_idx - 1] 까지로 클리핑한다.
    leave_start_date 가 월 시작일 이전이면 그 달 전체 비활성(None) 처리.

    ※ `_active_range_in_month` 는 nurses.resignation_date 만 본다.
    휴직 시작일은 nurse_assignment.start_date 에 저장되므로 이 helper 로 별도 클리핑.
    expected_end_date 이후 복귀 시점은 status=completed 처리(자동 flush)로 자연 정리됨.
    """
    if not leave_assignments:
        return []
    month_end = month_start + timedelta(days=days_in_month - 1)
    affected: list[str] = []
    for la in leave_assignments:
        nid = str(getattr(la, "nurse_id", "") or "")
        if not nid:
            continue
        existing = active_range_map.get(nid)
        if existing is None:
            continue
        start = getattr(la, "start_date", None)
        if start is None:
            continue
        if start <= month_start:
            active_range_map[nid] = None
            affected.append(f"{nid}({la.reason} 전월시작)")
            continue
        if start > month_end:
            continue
        leave_start_idx = (start - month_start).days - 1
        s_idx, e_idx = existing
        if leave_start_idx < s_idx:
            active_range_map[nid] = None
            affected.append(f"{nid}({la.reason} 시작일 {start} → 전체 비활성)")
        else:
            active_range_map[nid] = (s_idx, min(e_idx, leave_start_idx))
            affected.append(f"{nid}({la.reason} {start} 전까지)")
    return affected


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
        is_weekend_off = bool(getattr(nurse, "is_weekend_off", False))
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
                is_sunday_weekly_off = weekday == 6 and weekly_off_enabled and not is_weekend_off
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
    inbound_nurses: list | None = None,
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

    # 전입자(비-flush inbound) 주휴 보충: nurses 행이 source 그룹이라 위 group_id 필터
    # 쿼리에서 빠진다. 엔진 객체엔 assignment target_weekly_off_*(enabled/weekday)가 이미
    # overlay 돼 있고 is_weekend_off 는 본인 nurses 행 값이 그대로 있으므로, 그 객체를 rows
    # 에 추가해 동일 루프로 처리한다(flush된 병동이동은 이미 target 행이라 rows 에 포함→제외).
    if inbound_nurses:
        _wo_existing_ids = {str(getattr(_r, "nurse_id", "")) for _r in rows}
        rows = list(rows) + [
            _n for _n in inbound_nurses
            if str(getattr(_n, "nurse_id", "")) not in _wo_existing_ids
        ]

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


def _exclude_alloff_nurses(
    nurses: list,
    fixed_cells: list[dict],
    off_exc: set,
    config: dict,
    num_days: int,
) -> tuple[list, list[dict], set, dict[str, list[str]]]:
    """월 전체 OFF 고정 간호사를 엔진에서 제외하고 nurse_index를 리매핑한다."""
    off_cnt: dict[int, int] = {}
    for c in fixed_cells:
        if str(c.get("shift") or "").upper() in ("O", "OFF", "주"):
            ni = c.get("nurse_index")
            if ni is not None:
                off_cnt[ni] = off_cnt.get(ni, 0) + 1
    alloff = {ni for ni, cnt in off_cnt.items() if cnt >= num_days}
    if not alloff:
        return nurses, fixed_cells, off_exc, {}
    names = [
        f"{getattr(nurses[i], 'name', '?')}({getattr(nurses[i], 'nurse_id', '?')})"
        for i in sorted(alloff) if i < len(nurses)
    ]
    print(f"[RosterCreate] 일괄 OFF 간호사 엔진 자동 제외: {names}")
    roster: dict[str, list[str]] = {
        str(nurses[i].nurse_id): ["O"] * num_days
        for i in sorted(alloff) if i < len(nurses)
    }
    remap: dict[int, int] = {}
    new_i = 0
    for old in range(len(nurses)):
        if old not in alloff:
            remap[old] = new_i
            new_i += 1
    nurses = [n for i, n in enumerate(nurses) if i not in alloff]
    fixed_cells = [
        {**c, "nurse_index": remap[c["nurse_index"]]}
        for c in fixed_cells if c.get("nurse_index") in remap
    ]
    off_exc = {(remap[n], d) for n, d in off_exc if n in remap}
    config["off_exception_cells"] = sorted(off_exc) if off_exc else []
    pte = config.get("preceptee_fixed_wanted_map")
    if pte:
        config["preceptee_fixed_wanted_map"] = {
            (remap[ni], di): v for (ni, di), v in pte.items() if ni in remap
        }
    return nurses, fixed_cells, off_exc, roster


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
                "fixed_source": "special_fixed",
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
    """이전 달의 최종 schedule_id를 조회한다.

    우선순위:
    1) status='issued' 스케줄이 있으면 → 마감본 참조
    2) 없으면(전부 draft) → 최신 version 참조

    dropped=True (취소/대체) 인 schedule 은 어느 분기든 제외.
    """
    py, pm = _get_prev_year_month(year, month)
    # 1) status='issued' 스케줄 조회 (dropped 제외)
    issued = (
        db.query(Schedule)
        .filter(
            Schedule.group_id == group_id,
            Schedule.year == py,
            Schedule.month == pm,
            Schedule.status == "issued",
            Schedule.dropped == False,  # noqa: E712
        )
        .order_by(Schedule.version.desc())
        .first()
    )
    if issued:
        return issued.schedule_id
    # 2) issued 없으면 최신 version (dropped 제외)
    latest = (
        db.query(Schedule)
        .filter(
            Schedule.group_id == group_id,
            Schedule.year == py,
            Schedule.month == pm,
            Schedule.dropped == False,  # noqa: E712
        )
        .order_by(Schedule.version.desc())
        .first()
    )
    return latest.schedule_id if latest else None


def _query_schedule_id_for_month(db: Session, group_id: str, year: int, month: int) -> str | None:
    """특정 그룹의 해당 월 최종 schedule_id를 조회한다.

    우선순위: status='issued' > 최신 version(draft)
    dropped=True (취소/대체) 인 schedule 은 어느 분기든 제외.
    """
    issued = (
        db.query(Schedule)
        .filter(
            Schedule.group_id == group_id,
            Schedule.year == year,
            Schedule.month == month,
            Schedule.status == "issued",
            Schedule.dropped == False,  # noqa: E712
        )
        .order_by(Schedule.version.desc())
        .first()
    )
    if issued:
        return issued.schedule_id
    latest = (
        db.query(Schedule)
        .filter(
            Schedule.group_id == group_id,
            Schedule.year == year,
            Schedule.month == month,
            Schedule.dropped == False,  # noqa: E712
        )
        .order_by(Schedule.version.desc())
        .first()
    )
    return latest.schedule_id if latest else None


def _query_schedule_ref_for_month(db: Session, group_id: str, year: int, month: int) -> tuple[str | None, str]:
    """특정 그룹의 해당 월 참조 schedule과 선택 기준을 함께 반환한다.

    Returns:
        (schedule_id | None, basis)
        basis in {'issued', 'latest', 'blank'}
    """
    issued = (
        db.query(Schedule)
        .filter(
            Schedule.group_id == group_id,
            Schedule.year == year,
            Schedule.month == month,
            Schedule.status == "issued",
            Schedule.dropped == False,
        )
        .order_by(Schedule.version.desc())
        .first()
    )
    if issued:
        return issued.schedule_id, "issued"
    latest = (
        db.query(Schedule)
        .filter(
            Schedule.group_id == group_id,
            Schedule.year == year,
            Schedule.month == month,
            Schedule.dropped == False,
        )
        .order_by(Schedule.version.desc())
        .first()
    )
    if latest:
        return latest.schedule_id, "latest"
    return None, "blank"


def _get_boundary_tail(
    db: Session, schedule_id: str, nurse_id: str,
    boundary_day: int, lookback: int, code2main: dict,
) -> list[str]:
    """경계일(0-indexed) 이전 lookback일의 shift 시퀀스를 반환한다."""
    if not schedule_id or boundary_day <= 0:
        return []
    entries = (
        db.query(ScheduleEntry)
        .filter(
            ScheduleEntry.schedule_id == schedule_id,
            ScheduleEntry.nurse_id == nurse_id,
        )
        .all()
    )
    daymap: dict[int, str] = {}
    for e in entries:
        d = int(e.work_date.day) - 1  # 0-indexed
        daymap[d] = _normalize_to_main(e.shift_id, code2main)
    start = max(0, boundary_day - lookback)
    return [daymap.get(d, '-') for d in range(start, boundary_day)]


def _get_last_days_map(db: Session, schedule_id: str, days: int, code2main: dict, active_nurse_ids: list[str] | None = None) -> dict:
    """해당 schedule_id의 마지막 N일 근무코드를 메인코드로 정규화하여 반환한다.
    반환: { nurse_id: ['E','N','O','D','N','O'] } (최대 길이 days, 과거→현재 순)
    """
    if not schedule_id:
        print(f"[CrossMonth] 이전 달 스케줄 ID 없음, 빈 맵 반환")
        return {}
    # 해당 스케줄의 모든 엔트리 로딩
    q = db.query(ScheduleEntry).filter(ScheduleEntry.schedule_id == schedule_id)
    if active_nurse_ids is not None:
        q = q.filter(ScheduleEntry.nurse_id.in_(active_nurse_ids))
        print(f"[CrossMonth] 비활성 간호사 이전달 데이터 제외: active {len(active_nurse_ids)}명 기준 필터")
    entries = q.all()
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
            'consecutive_off_tail': 0,
            'last_day_shift': None,
            'offs_after_tail_nights': 0,
        }
    last = seq[-1]
    # 연속 근무 꼬리 (D/E/N/M = 메인코드, WK = shift_gb·type 미설정 근무 폴백)
    _WORK_CODES = ('D', 'E', 'N', 'M', 'WK')
    cons_work = 0
    for c in reversed(seq):
        if c in _WORK_CODES:
            cons_work += 1
        elif c == 'O':
            break
        else:
            # '-' 등 미인식 코드는 연속 끊김 처리
            break
    # 꼬리 연속 OFF 카운트 (4O 월경계 제약용)
    cons_off_tail = 0
    for c in reversed(seq):
        if c == 'O':
            cons_off_tail += 1
        else:
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
        'consecutive_off_tail': cons_off_tail,
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
        return {'forced_off': {}, 'forbidden': {}, 'day0_n_fixed_nurse_ids': [], 'prev_month_n_offs_after': {}}

    # 코드 정규화 맵 구성
    code2main = {}
    for r in (shift_manage_data or []):
        main = r.get('main_code')
        for c in (r.get('codes') or []):
            code2main[str(c).upper()] = main
    code2main['O'] = 'O'; code2main['OFF'] = 'O'; code2main['주'] = 'O'

    # Shift 테이블의 shift_id → 메인코드 매핑 보강
    # ShiftManage.codes에 없는 세부 근무코드(NT, A, VY, OO 등)를 정규화하기 위함
    # 판정 우선순위: ① shift_gb(데이→D,이브닝→E,나이트→N,미드→M) ② default_shift ③ type
    _SHIFT_GB_TO_MAIN = {"데이": "D", "이브닝": "E", "나이트": "N", "미드": "M"}
    try:
        all_shifts = (
            db.query(Shift)
            .filter(
                Shift.group_id == current_user.group_id,
                Shift.office_id == current_user.office_id,
            )
            .all()
        )
        _sup_cnt = 0
        for s in all_shifts:
            sid = str(s.shift_id).strip().upper()
            if not sid or sid in code2main:
                continue
            # ① shift_gb 기반 판정 (가장 명확)
            sgb = str(getattr(s, "shift_gb", "") or "").strip()
            if sgb in _SHIFT_GB_TO_MAIN:
                code2main[sid] = _SHIFT_GB_TO_MAIN[sgb]
                _sup_cnt += 1
                continue
            # ② default_shift 기반 판정
            ds = str(getattr(s, "default_shift", "") or "").strip().upper()
            if ds in ("OFF", "주"):
                ds = "O"
            if ds in ("D", "E", "N", "M", "O"):
                code2main[sid] = ds
                _sup_cnt += 1
                continue
            # ③ type 기반 판정 (최후 폴백)
            shift_type = str(getattr(s, "type", "") or "").strip()
            if shift_type == "근무":
                # shift_gb·default_shift 모두 미설정이지만 근무 shift → 'WK'(근무 마커)
                # 'W'는 엔진 내 주휴(Weekly Off) 타입으로 사용 중이므로 충돌 회피
                # _calc_tail_metrics에서 연속근무 카운트가 끊기지 않도록 함
                code2main[sid] = "WK"
                _sup_cnt += 1
                print(f"[CrossMonth][WARN] shift_id={sid}: shift_gb/default_shift 미설정, type=근무 → 'WK' 폴백 (데이터 확인 권장)")
            elif shift_type in ("휴가", "공가"):
                code2main[sid] = "O"
                _sup_cnt += 1
        if _sup_cnt > 0:
            _extra = {k: v for k, v in code2main.items() if k not in ('D', 'E', 'N', 'M', 'O', 'OFF', '주')}
            print(f"[CrossMonth] Shift 테이블에서 {_sup_cnt}건 추가 코드 정규화 보강: {_extra}")
    except Exception as e:
        print(f"[CrossMonth][WARN] Shift 테이블 보강 실패(무시): {e}")

    # 이전 달 최신 스케줄 조회 → 마지막 N일 시퀀스
    try:
        prev_sid = _query_prev_month_schedule_id(db, current_user.group_id, req.year, req.month)
        prev_year, prev_month = _get_prev_year_month(req.year, req.month)
        print(f"[CrossMonth] 이전 달 조회: {prev_year}년 {prev_month}월, schedule_id={prev_sid}, lookback={lookback}일")
    except Exception as e:
        print("[ERR] _query_prev_month_schedule_id:", e)
        raise
    try:
        last_map = _get_last_days_map(db, prev_sid, lookback, code2main, nurse_ids) if prev_sid else {}
        print(f"[CrossMonth] 이전 달 꼬리 패턴 조회 완료: {len(last_map)}명")
    except Exception as e:
        print("[ERR] _get_last_days_map:", e)
        raise

    # ── 인바운드 간호사 tail 보충: target 전월 schedule에 없으면 source group에서 조회 ──
    _inbound_src_map = config_dict.get("_inbound_source_map")
    if _inbound_src_map:
        _missing = [nid for nid in _inbound_src_map if nid not in last_map]
        if _missing:
            _src_groups: dict[str, list[str]] = {}
            for nid in _missing:
                _src_groups.setdefault(_inbound_src_map[nid], []).append(nid)
            for src_gid, nids in _src_groups.items():
                _src_sid = _query_prev_month_schedule_id(db, src_gid, req.year, req.month)
                if _src_sid:
                    _src_map = _get_last_days_map(db, _src_sid, lookback, code2main, nids)
                    for nid, seq in _src_map.items():
                        last_map[nid] = seq
                    print(
                        f"[CrossMonth][Inbound] source({src_gid}) 전월 tail 보충: "
                        f"{len(_src_map)}명/{len(nids)}명"
                    )
                else:
                    print(f"[CrossMonth][Inbound] source({src_gid}) 전월 schedule 없음, tail 생략")

    prev_month_last_main: dict[str, str | None] = {}
    prev_month_last_is_off: dict[str, bool] = {}
    prev_month_n_tail: dict[str, int] = {}
    prev_month_n_offs_after: dict[str, int] = {}  # N tail 뒤 이미 소비된 OFF 수
    prev_month_off_tail: dict[str, int] = {}
    off_window_constraints: dict[str, list[list[int]]] = {}
    for nurse_id, seq in last_map.items():
        last_code = seq[-1] if seq else None
        prev_month_last_main[nurse_id] = last_code
        prev_month_last_is_off[nurse_id] = bool(last_code == "O")
        # 꼬리 연속 OFF (4O 월경계 제약용)
        metrics = _calc_tail_metrics(seq)
        cons_off = metrics.get('consecutive_off_tail', 0)
        if cons_off > 0:
            prev_month_off_tail[nurse_id] = cons_off
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
    ban_e_to_d = bool(config_dict.get('ban_e_to_d', True))
    ban_n_to_d = bool(config_dict.get('ban_n_to_d', True))
    ban_n_to_e = bool(config_dict.get('ban_n_to_e', True))
    ban_d_to_n = bool(config_dict.get('ban_d_to_n', True))
    if not banned_E_to_D:
        ban_e_to_d = False
        ban_n_to_d = False
        ban_n_to_e = False
        ban_d_to_n = False
    L = int(config_dict.get('max_consecutive_nights') or (3 if bool(config_dict.get('three_seq_nig', False)) else 2))

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
        prev_month_n_tail[nurse_id] = int(cons_n or 0)
        if cons_n > 0:
            prev_month_n_offs_after[nurse_id] = int(offs_after or 0)
        
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
        # (b-0) 1N 금지: 꼬리 N이 1개(1N tail)면 day0 N 으로 이어 2N 을 만들어 1N 금지 충족 +
        #   day0 N 커버리지를 확보한다. day0 휴식이 '하드'(forced_off day0 직접 / 연속근무 상한으로
        #   day0 OFF 필수 = off_window [0,0])일 때만 day0 N 을 막고, soft 한 off_window(윈도우 내
        #   OFF≥1)가 day0 를 덮으면 window_end>=1 일 때 [1,end] 로 시프트(휴식은 day1~ 에서 확보)해
        #   day0 N 을 허용한다. (1N 금지=하드락이 월초 휴식 soft 윈도우보다 우선)
        if not_one_night and cons_n == 1:
            has_day0_forced = 0 in forced_off.get(nurse_id, [])
            day0_off_window_hard = False
            for _w in list(off_window_constraints.get(nurse_id, []) or []):
                try:
                    _l, _r = int(_w[0]), int(_w[1])
                except Exception:
                    continue
                if _l <= 0 <= _r:
                    if _r >= 1:
                        # off_window 가 day0 를 덮지만 day1~ 로 OFF≥1 충족 가능 → day0 만 비켜 시프트
                        off_window_constraints[nurse_id].remove(_w)
                        off_window_constraints[nurse_id].append([1, _r])
                    else:
                        # [0,0] = day0 OFF 필수(연속근무 상한 도달) → day0 N 불가
                        day0_off_window_hard = True
            tail_str = ' '.join(tail) if tail else '(없음)'
            if has_day0_forced or day0_off_window_hard:
                print(f"[CrossMonth] 간호사 {nurse_id}: 1N tail + day0 휴식 필수(forced_off/연속근무 상한) → day0 N 고정 스킵, tail={tail_str}")
            elif nurse_id in day0_weekly_off_nurse_ids:
                forbidden[nurse_id][0].extend(['D', 'E', 'N'])
                print(f"[CrossMonth] 간호사 {nurse_id}: 1N tail + day0 주휴 → day0 O 유지(forbidden D/E/N), tail={tail_str}")
            else:
                # day0 N 허용(전월 1N + day0 = 2N). L>=3(3N 허용 설정=three_seq_nig)이면 day1 도 N 허용 →
                #   day0+day1=3N 까지 구조적으로 가능(N 공급↑·미달일 커버리지 도움; 3N 되면 솔버의 3N2O 가
                #   day2,3 OFF 처리). L==2(3N 불가)면 현재처럼 day0만 N(2N) + 2N2O 면 day1,2 OFF 강제.
                if L >= 3:
                    _n1_msg = "day0~day1 N 허용(최대 3N, three_seq_nig)"
                else:
                    two_after_two_effective = two_after_two
                    if two_after_two_effective:
                        forced_off[nurse_id].extend([1, 2])
                    _n1_msg = "day0 N 허용(2N)" + (", day1,2 OFF(2N2O)" if two_after_two_effective else "")
                print(f"[CrossMonth] 간호사 {nurse_id}: 1N tail → {_n1_msg}(off_window day0→[1,end] 시프트), tail={tail_str}")

        # (b) N2/3 → 2OFF
        req_offs = 0
        two_after_two_effective = two_after_two
        two_after_three_effective = two_after_three
        # N 상한(L) 미도달 시: 솔버 prefix cap이 경계를 처리하므로 forced OFF 불필요
        # 예) three_seq_nig=True(L=3), cons_n=2 → day0에 N 1회 추가 허용
        if L and cons_n < L:
            two_after_two_effective = False
            two_after_three_effective = False
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
        if last_shift == 'E' and ban_e_to_d:
            forbidden[nurse_id][0].append('D')
        if last_shift == 'N' and ban_n_to_d:
            forbidden[nurse_id][0].append('D')
        if last_shift == 'N' and ban_n_to_e:
            forbidden[nurse_id][0].append('E')

        # (d) 연속 N 상한
        if L and cons_n >= L and offs_after == 0:
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
        'prev_month_n_tail': prev_month_n_tail,
        'prev_month_n_offs_after': prev_month_n_offs_after,
        'prev_month_off_tail': prev_month_off_tail,
        'off_window_constraints': off_window_constraints,
        'day0_n_fixed_nurse_ids': day0_n_fixed_nurse_ids,
    }


def build_mid_month_boundary_constraints(
    db: Session,
    inbound_assignments: list,
    group_id: str,
    year: int,
    month: int,
    config_dict: dict,
    code2main: dict,
    outbound_assignments: list | None = None,
) -> dict:
    """인바운드/아웃바운드 간호사의 mid-month 경계 window 제약을 생성한다.

    - 인바운드: source group의 같은 달 근무표에서 경계일 이전 tail 추출
    - 아웃바운드 복귀: target group의 같은 달 근무표에서 복귀일 이전 tail 추출
    """
    lookback = int(config_dict.get('cross_month_lookback_days', 6))
    days_in_month = calendar.monthrange(year, month)[1]
    month_start = date(year, month, 1)

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
    L = int(config_dict.get('max_consecutive_nights') or (3 if bool(config_dict.get('three_seq_nig', False)) else 2))
    two_after_three = bool(config_dict.get('two_offs_after_three_nig'))
    two_after_two = bool(config_dict.get('two_offs_after_two_nig'))
    ban_e_to_d = bool(config_dict.get('ban_e_to_d', True))
    ban_n_to_d = bool(config_dict.get('ban_n_to_d', True))
    ban_n_to_e = bool(config_dict.get('ban_n_to_e', True))
    if not bool(config_dict.get('banned_day_after_eve')):
        ban_e_to_d = False
        ban_n_to_d = False
        ban_n_to_e = False

    forced_off: dict[str, list[int]] = defaultdict(list)
    forbidden: dict[str, dict[int, list[str]]] = defaultdict(lambda: defaultdict(list))
    off_window_constraints: dict[str, list[list[int]]] = defaultdict(list)

    for a in inbound_assignments:
        nurse_id = str(a.nurse_id)
        boundary_day = (a.start_date - month_start).days
        if boundary_day <= 0:
            continue  # 월초 시작 = cross-month constraints가 처리

        src_sid, src_basis = _query_schedule_ref_for_month(db, a.source_group_id, year, month)
        if not src_sid:
            print(f"[MidMonth] nurse={nurse_id}: source({a.source_group_id}) 근무표 없음(basis={src_basis}), window 생략")
            continue

        # source 그룹의 Shift 기반 code2main 빌드 (target과 shift 코드 체계가 다를 수 있음)
        src_code2main = dict(code2main)
        try:
            src_shifts = db.query(Shift).filter(Shift.group_id == a.source_group_id).all()
            _GB = {"데이": "D", "이브닝": "E", "나이트": "N", "미드": "M"}
            for s in src_shifts:
                sid = str(s.shift_id).strip().upper()
                if not sid or sid in src_code2main:
                    continue
                sgb = str(getattr(s, "shift_gb", "") or "").strip()
                if sgb in _GB:
                    src_code2main[sid] = _GB[sgb]
                    continue
                ds = str(getattr(s, "default_shift", "") or "").strip().upper()
                if ds in ("OFF", "주"):
                    ds = "O"
                if ds in ("D", "E", "N", "M", "O"):
                    src_code2main[sid] = ds
        except Exception as e:
            print(f"[MidMonth] source Shift 보강 실패(무시): {e}")

        tail = _get_boundary_tail(db, src_sid, nurse_id, boundary_day, lookback, src_code2main)
        if not tail:
            print(f"[MidMonth] nurse={nurse_id}: source 근무표에 데이터 없음, window 생략")
            continue

        metrics = _calc_tail_metrics(tail)
        cons_work = metrics['consecutive_work_tail']
        cons_n = metrics['consecutive_night_tail']
        last_shift = metrics['last_day_shift']
        offs_after = metrics['offs_after_tail_nights']
        bd = boundary_day
        tail_str = ' '.join(tail)
        print(f"[MidMonth] nurse={nurse_id}: boundary=day{bd}, tail=[{tail_str}], "
              f"cons_work={cons_work}, cons_n={cons_n}, last={last_shift}")

        compiled = _compile_boundary_overlap_constraints(
            nurse_id=nurse_id,
            boundary_day=bd,
            days_in_month=days_in_month,
            cons_work=cons_work,
            cons_n=cons_n,
            last_shift=last_shift,
            offs_after=offs_after,
            K=K,
            L=L,
            two_after_two=two_after_two,
            two_after_three=two_after_three,
            ban_e_to_d=ban_e_to_d,
            ban_n_to_d=ban_n_to_d,
            ban_n_to_e=ban_n_to_e,
        )
        forced_off[nurse_id].extend(compiled['forced_off'])
        for d, codes in compiled['forbidden'].items():
            forbidden[nurse_id][d].extend(codes)
        off_window_constraints[nurse_id].extend(compiled['off_window_constraints'])

    # ── 아웃바운드 복귀: target group 근무표 tail → 복귀일 제약 ──
    for a in (outbound_assignments or []):
        nurse_id = str(a.nurse_id)
        a_end = a.end_date or a.expected_end_date
        if not a_end:
            continue  # 종료일 미정 → 이번 달 복귀 없음
        if a_end.year != year or a_end.month != month:
            continue  # 종료일이 이번 달이 아님
        return_day = (a_end - month_start).days + 1  # 종료일 다음날부터 복귀
        if return_day >= days_in_month or return_day <= 0:
            continue  # 월 범위 밖

        tgt_sid, tgt_basis = _query_schedule_ref_for_month(db, a.target_group_id, year, month)
        if not tgt_sid:
            print(f"[MidMonth][Return] nurse={nurse_id}: target({a.target_group_id}) 근무표 없음(basis={tgt_basis}), window 생략")
            continue

        # target 그룹의 Shift 기반 code2main 빌드 (source와 shift 코드 체계가 다를 수 있음)
        tgt_code2main = dict(code2main)  # base copy
        try:
            tgt_shifts = db.query(Shift).filter(Shift.group_id == a.target_group_id).all()
            _GB = {"데이": "D", "이브닝": "E", "나이트": "N", "미드": "M"}
            for s in tgt_shifts:
                sid = str(s.shift_id).strip().upper()
                if not sid or sid in tgt_code2main:
                    continue
                sgb = str(getattr(s, "shift_gb", "") or "").strip()
                if sgb in _GB:
                    tgt_code2main[sid] = _GB[sgb]
                    continue
                ds = str(getattr(s, "default_shift", "") or "").strip().upper()
                if ds in ("OFF", "주"):
                    ds = "O"
                if ds in ("D", "E", "N", "M", "O"):
                    tgt_code2main[sid] = ds
        except Exception as e:
            print(f"[MidMonth][Return] target Shift 보강 실패(무시): {e}")

        tail = _get_boundary_tail(db, tgt_sid, nurse_id, return_day, lookback, tgt_code2main)
        if not tail:
            print(f"[MidMonth][Return] nurse={nurse_id}: target 근무표에 데이터 없음, window 생략")
            continue

        metrics = _calc_tail_metrics(tail)
        cons_work = metrics['consecutive_work_tail']
        cons_n = metrics['consecutive_night_tail']
        last_shift = metrics['last_day_shift']
        offs_after = metrics['offs_after_tail_nights']
        rd = return_day
        tail_str = ' '.join(tail)
        print(f"[MidMonth][Return] nurse={nurse_id}: return=day{rd}, tail=[{tail_str}], "
              f"cons_work={cons_work}, cons_n={cons_n}, last={last_shift}")

        compiled = _compile_boundary_overlap_constraints(
            nurse_id=nurse_id,
            boundary_day=rd,
            days_in_month=days_in_month,
            cons_work=cons_work,
            cons_n=cons_n,
            last_shift=last_shift,
            offs_after=offs_after,
            K=K,
            L=L,
            two_after_two=two_after_two,
            two_after_three=two_after_three,
            ban_e_to_d=ban_e_to_d,
            ban_n_to_d=ban_n_to_d,
            ban_n_to_e=ban_n_to_e,
        )
        forced_off[nurse_id].extend(compiled['forced_off'])
        for d, codes in compiled['forbidden'].items():
            forbidden[nurse_id][d].extend(codes)
        off_window_constraints[nurse_id].extend(compiled['off_window_constraints'])

    forced_off_final = {k: sorted(set(v)) for k, v in forced_off.items()}
    forbidden_final = {k: {d: sorted(set(ss)) for d, ss in v.items()} for k, v in forbidden.items()}
    off_cnt = sum(len(v) for v in forced_off_final.values())
    forb_cnt = sum(len(ss) for v in forbidden_final.values() for ss in v.values())
    ow_cnt = sum(len(v) for v in off_window_constraints.values())
    if off_cnt or forb_cnt or ow_cnt:
        print(f"[MidMonth] 경계 제약: forced_off {off_cnt}건, forbidden {forb_cnt}건, off_window {ow_cnt}건")
    return {
        'forced_off': forced_off_final,
        'forbidden': forbidden_final,
        'off_window_constraints': dict(off_window_constraints),
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


def _normalize_allowed_shift_types(raw_value: object, use_mid: bool = False) -> set[str]:
    if raw_value is None:
        return set()
    # 레거시 타입은 무시(요구사항: 기존 allowed_shifts 의미는 무시)
    if isinstance(raw_value, (int, float, bool)):
        return set()
    if not isinstance(raw_value, list):
        return set()

    valid_codes = {"D", "E", "N"}
    if bool(use_mid):
        valid_codes.add("M")

    allowed: set[str] = set()
    invalid: set[str] = set()
    for item in raw_value:
        code = str(item).strip().upper()
        if not code:
            continue
        if code in valid_codes:
            allowed.add(code)
        else:
            invalid.add(code)
    if invalid:
        print(
            f"[AllowedShiftTypes] 허용 근무유형 무시: invalid={sorted(invalid)}, "
            f"allowed={sorted(valid_codes)}"
        )
    return allowed


def _normalize_shift_to_main(shift_code: object, code2main: dict[str, str]) -> str:
    """고정 셀의 shift 코드를 엔진 기준 메인코드(D/E/N/O)로 정규화한다."""
    code = str(shift_code or "").strip().upper()
    if code in {"OFF", "주"}:
        return "O"
    mapped = str(code2main.get(code, code)).strip().upper()
    if mapped in {"D", "E", "N", "O", "M", "W"}:
        return mapped
    return code


def build_allowed_shift_type_constraints(
    nurses_in_group: list,
    year: int,
    month: int,
    shift_manage_data: list[dict] | None,
    fixed_cells: list[dict] | None,
    use_mid: bool,
    db=None,
) -> dict:
    """간호사별 허용 근무유형(D/E/N) 하드 제약을 forbidden 형태로 생성한다.

    목표:
        - 간호사 row에 저장된 허용 목록에 따라, 허용되지 않은 D/E/N(및 use_mid=True이면 M) 배정을
          전일(day_idx 전체)에 대해 금지한다.
        - OFF(O)는 항상 가능하도록 금지 대상에서 제외한다.
        - fixed_cells(고정셀)가 허용 목록과 충돌하면 해당 날짜만 예외로 두고 진행한다.

    Returns:
        {"forced_off": {}, "forbidden": {nurse_id: {day_idx: ["D","E"]}}}
    """
    days_in_month = int(get_days_in_month(year, month))
    if days_in_month <= 0:
        return {"forced_off": {}, "forbidden": {}}
    month_start = date(year, month, 1)

    code2main = _build_code_to_main_map(shift_manage_data)
    allowed_main_codes = {"D", "E", "N"}
    if bool(use_mid):
        allowed_main_codes.add("M")
    nurse_id_to_allowed: dict[str, set[str]] = {}
    nurse_id_to_active_range: dict[str, tuple[int, int] | None] = {}

    for n in nurses_in_group:
        nurse_id = str(getattr(n, "nurse_id", "") or "")
        if not nurse_id:
            continue
        allowed = _normalize_allowed_shift_types(
            getattr(n, "allowed_shifts", None),
            use_mid=bool(use_mid),
        )
        nurse_id_to_allowed[nurse_id] = allowed
        nurse_id_to_active_range[nurse_id] = _active_range_in_month(n, month_start, days_in_month)

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
            if fixed_main not in allowed_main_codes:
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

    # ── 일자별 허용 해석: NurseAllowedShiftPeriod(시점) 우선, gap이면 캐시 폴백 ──
    # (P3) db 가 있으면 period 를 bulk fetch 해 day-grain 으로 금지셀을 만든다.
    # 구간 없음(미backfill/gap)이면 nurses 캐시값으로 폴백 → 무회귀(기존 동작과 동일).
    periods_by_nurse: dict[str, list] = {}
    _resolve = None
    if db is not None:
        try:
            from services.nurse_period_resolver import fetch_periods, resolve_asof as _resolve
            from db.models import NurseAllowedShiftPeriod
            month_end = date(year, month, days_in_month) + timedelta(days=1)
            periods_by_nurse = fetch_periods(
                db, NurseAllowedShiftPeriod,
                list(nurse_id_to_allowed.keys()), month_start, month_end,
            )
        except Exception as exc:
            print(f"[AllowedShiftTypes] period fetch 실패 → 캐시 폴백: {exc}")
            periods_by_nurse, _resolve = {}, None

    forbidden: dict[str, dict[int, list[str]]] = {}
    all_codes = set(allowed_main_codes)
    used_period = False
    for nurse_id, cache_allowed in nurse_id_to_allowed.items():
        active_range = nurse_id_to_active_range.get(nurse_id)
        if not active_range:
            continue
        start_idx, end_idx = active_range
        override_days = override_days_by_nurse.get(nurse_id, set())
        rows = periods_by_nurse.get(nurse_id)
        day_map: dict[int, list[str]] = {}
        for d in range(start_idx, end_idx + 1):
            if d in override_days:
                continue
            # period 우선(없으면 캐시). val=[] 는 '제한 없음', val=None 은 gap.
            val = _resolve(rows, date(year, month, d + 1), "allowed_shifts", None) \
                if (_resolve and rows) else None
            if val is not None:
                used_period = True
            allowed_day = set(val) if val is not None else set(cache_allowed)
            if not allowed_day:
                continue  # 제한 없음
            disallowed = sorted(all_codes - allowed_day)
            if disallowed:
                day_map[d] = disallowed
        if day_map:
            forbidden[nurse_id] = day_map

    forb_cnt = sum(len(codes) for v in forbidden.values() for codes in v.values())
    if forbidden:
        src = "period+캐시" if used_period else "캐시(월전체)"
        print(f"[AllowedShiftTypes] 금지 셀 적용({src}): nurses={len(forbidden)}, cnt={forb_cnt}")
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


def _validate_mid_hard_feasibility(nurses_in_group, config_dict: dict, year: int, month: int) -> str | None:
    return _validate_mid_hard_feasibility_impl(
        nurses_in_group=nurses_in_group,
        config_dict=config_dict,
        year=year,
        month=month,
    )

def _run_cp_sat_basic(db: Session, current_user, nurses_in_group, preferences, latest_config, req, shift_manage_data, fixed_cells=None, time_limit_seconds=60, config_override: dict | None = None, _assignments=None, _inbound_assignments=None, _outbound_assignments=None):
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

        # teams.min_shift → config_dict["team_min_by_team"] 로 주입
        # 키는 team_id(str). 팀별 dict 가 비어있거나 NULL 이면 제외.
        try:
            team_rows = (
                db.query(Team)
                .filter(
                    Team.office_id == current_user.office_id,
                    Team.group_id == current_user.group_id,
                    Team.active == 1,
                )
                .all()
            )
            team_min_by_team: dict[str, dict[str, int]] = {}
            team_handoff_policy_by_team: dict[str, dict] = {}
            # teams.min_shift 는 현재 저장 수단/디폴트가 없어 보지 않는다. 존재하는 활성 팀마다
            # 디폴트 최소인원(D:1, E:1, N:0[, use_mid면 M:0])을 적용한다. 0(=무제약)은 제외하므로
            # 실효 제약은 팀별 D≥1·E≥1. (N/M 은 최소 0 = 제약 없음.)
            _use_mid = bool(config_dict.get("use_mid", False))
            _default_team_min: dict[str, int] = {"D": 1, "E": 1, "N": 0}
            if _use_mid:
                _default_team_min["M"] = 0
            # 멤버가 배정된 팀에만 team_min 적용 — 솔버(team_members)·precheck(members_by_team)와
            # 동일 기준. 인원 0 팀에 디폴트를 넣으면 precheck 가 TEAM_SIZE_INSUFFICIENT 로 오블로킹.
            _member_team_ids = {
                str(getattr(n, "team_id", None))
                for n in nurses_in_group
                if getattr(n, "team_id", None) not in (None, "", 0)
            }
            for t in team_rows:
                _tid = str(t.team_id)
                if _tid in _member_team_ids:
                    cleaned = {k: v for k, v in _default_team_min.items() if v > 0}
                    if cleaned:
                        team_min_by_team[_tid] = cleaned
                hp = t.handoff_policy if isinstance(t.handoff_policy, dict) else None
                if hp and isinstance(hp.get("restrictions"), list) and hp["restrictions"]:
                    team_handoff_policy_by_team[_tid] = hp
            if team_min_by_team:
                config_dict["team_min_by_team"] = team_min_by_team
            if team_handoff_policy_by_team:
                config_dict["team_handoff_policy_by_team"] = team_handoff_policy_by_team
        except Exception as e:
            print(f"[TeamMin] teams.min_shift/handoff_policy 로딩 실패(무시): {e}")

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
                        'shift_type': c.get('shift_type'),
                        'fixed_source': c.get('fixed_source'),
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
        use_mid=bool(config_dict.get("use_mid", False)),
        db=db,
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
                fixed_list.append(
                    {
                        "nurse_index": n_idx,
                        "day_index": 0,
                        "shift": "N",
                        "shift_type": "근무",
                        "fixed_source": "carryover_day0_n",
                    }
                )
        config_dict["fixed_cells"] = fixed_list
    prev_month_last_is_off = cross_month_constraints.get("prev_month_last_is_off") or {}
    if prev_month_last_is_off:
        config_dict["prev_month_last_is_off"] = prev_month_last_is_off
    prev_month_n_tail = cross_month_constraints.get("prev_month_n_tail") or {}
    if prev_month_n_tail:
        config_dict["prev_month_n_tail"] = prev_month_n_tail
    prev_month_n_offs_after = cross_month_constraints.get("prev_month_n_offs_after") or {}
    if prev_month_n_offs_after:
        config_dict["prev_month_n_offs_after"] = prev_month_n_offs_after
    prev_month_off_tail = cross_month_constraints.get("prev_month_off_tail") or {}
    if prev_month_off_tail:
        config_dict["prev_month_off_tail"] = prev_month_off_tail
    off_window_constraints = cross_month_constraints.get("off_window_constraints") or {}
    if off_window_constraints:
        config_dict["off_window_constraints"] = off_window_constraints
    print('prev_month_last_is_off', prev_month_last_is_off)

    # ── 3) 병합 후 주입(금지/강제OFF 합집합) ──
    config_dict["initial_constraints"] = _merge_initial_constraints(
        base=cross_month_constraints,
        extra=allowed_constraints,
    )
    preflight_alerts = []
    try:
        checker_module = __import__(
            "services.cp_sat.feasibility_alerts",
            fromlist=["run_preflight_feasibility_alerts"],
        )
        checker_fn = getattr(checker_module, "run_preflight_feasibility_alerts", None)
        if callable(checker_fn):
            preflight_alerts = checker_fn(
                nurses_in_group=nurses_in_group,
                config_dict=config_dict,
                year=req.year,
                month=req.month,
                logger_prefix="[PreflightFeasibility]",
            ) or []
    except Exception as precheck_exc:
        print(f"[PreflightFeasibility] checker failed: {precheck_exc}")
    mid_feasibility_error = _validate_mid_hard_feasibility(
        nurses_in_group=nurses_in_group,
        config_dict=config_dict,
        year=req.year,
        month=req.month,
    )
    if mid_feasibility_error:
        raise Exception(mid_feasibility_error)
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
        # 기본 전략은 DB(roster_config.grade_strategy) 기준으로 잡되,
        # 요청에서 COMBINED/GRADE를 명시하고 grade 제약이 존재하면 해당 전략을 우선 적용한다.
        grade_strategy, grade_config = _resolve_grade_strategy(
            db=db,
            config_dict=config_dict,
            office_id=current_user.office_id,
            group_id=current_user.group_id,
            roster_config_id=getattr(latest_config, "config_id", None),
        )
        # 요청 바디에서 GRADE/COMBINED일 때는 DB에서 grade_config를 조회해 엔진에 전달
        engine_grade_config = grade_config
        if str(getattr(req, "grade_strategy", "") or "").upper() in ("GRADE", "COMBINED"):
            engine_grade_config = _fetch_grade_config_dict(
                db, current_user.office_id, current_user.group_id
            )
        if bool(config_dict.get("_force_grade_max_soft_fallback")) and isinstance(engine_grade_config, dict):
            engine_grade_config = dict(engine_grade_config)
            engine_grade_config["allow_soft_fallback"] = True
            print("[GradeFallback] force allow_soft_fallback=True (grade_max soft)")
        req_strategy = str(getattr(req, "grade_strategy", "") or "").upper()
        effective_grade_strategy = _select_effective_grade_strategy(
            req_strategy=req_strategy,
            resolved_strategy=grade_strategy,
            grade_config=engine_grade_config,
        )
        # 엔진에서도 사용할 수 있게 config_dict에 기록(디버깅/로그용)
        config_dict["grade_strategy"] = effective_grade_strategy
        cp_sat_result = generate_roster_cp_sat(
            nurses_dict,
            prefs_dict,
            config_dict,
            req.year,
            req.month,
            shift_manage_data,
            time_limit_seconds=time_limit_seconds,
            grade_strategy=effective_grade_strategy,
            grade_config=engine_grade_config,
        )
    except Exception as e:
        print(f"error: {e}")
        raise
    if isinstance(cp_sat_result, dict) and "roster" in cp_sat_result:
        try:
            _rs = cp_sat_result.get("roster_system")
            if _rs is not None:
                setattr(_rs, "_constraint_impact_preflight_alerts", list(preflight_alerts or []))
                setattr(_rs, "_constraint_impact_mid_feasibility_error", mid_feasibility_error)
                # 엔진에 실제로 들어간 유효 config 스냅샷(하드규칙+조립분 전부 포함).
                # UNDIAGNOSED probe 가 충실한 base 로 재완화하는 데 쓴다(실패 시점 메모리는
                # ORM 만료·stale 라 부정확하므로 solve 시점에 박아둔다). _sa_* 는 제외.
                try:
                    _eff_snap: dict = {}
                    for _ck, _cv in config_dict.items():
                        if str(_ck).startswith("_sa_"):
                            continue
                        try:
                            _eff_snap[_ck] = deepcopy(_cv)
                        except Exception:
                            _eff_snap[_ck] = _cv
                    setattr(_rs, "_effective_config_snapshot", _eff_snap)
                except Exception as _eff_exc:
                    print(f"[EffectiveConfigSnapshot] 실패(무시): {_eff_exc}")
                setattr(_rs, "_constraint_impact_merged_initial_constraints", deepcopy(config_dict.get("initial_constraints") or {}))
                setattr(_rs, "_constraint_impact_special_fixed_requests", deepcopy(config_dict.get("special_fixed_requests") or []))
                setattr(
                    _rs,
                    "_constraint_impact_assignment_windows",
                    _build_constraint_impact_assignment_windows(
                        _assignments,
                        current_user.group_id,
                        date(req.year, req.month, 1),
                        calendar.monthrange(req.year, req.month)[1],
                    ),
                )
                setattr(
                    _rs,
                    "_constraint_impact_carryover_artifacts",
                    _build_constraint_impact_carryover_artifacts(
                        db,
                        _inbound_assignments,
                        _outbound_assignments,
                        req.year,
                        req.month,
                    ),
                )
                setattr(
                    _rs,
                    "_constraint_impact_attempt_meta",
                    {
                        "attempt_index": 1 if (bool(config_dict.get("_team_min_soft_retry_attempted")) or bool(config_dict.get("_force_grade_max_soft_fallback"))) else 0,
                        "label": (
                            "team_min_retry" if bool(config_dict.get("_team_min_soft_retry_attempted"))
                            else ("grade_max_retry" if bool(config_dict.get("_force_grade_max_soft_fallback")) else "primary")
                        ),
                        "forced_grade_soft_fallback": bool(config_dict.get("_force_grade_max_soft_fallback")),
                        "config_flags": {
                            "preceptee_on": bool(config_dict.get("preceptee_on", False)),
                            "preceptee_shift_count": bool(config_dict.get("preceptee_shift_count", True)),
                            "use_mid": bool(config_dict.get("use_mid", False)),
                            "off_first": bool(config_dict.get("off_first", False)),
                            "weekend_off_only_enable": bool(config_dict.get("weekend_off_only_enable", True)),
                            "team_min_soft_fallback": bool(config_dict.get("team_min_soft_fallback", False)),
                            "team_handoff_soft_fallback": bool(config_dict.get("team_handoff_soft_fallback", True)),
                            "grade_allow_soft_fallback": bool((engine_grade_config or {}).get("allow_soft_fallback", False)) if isinstance(engine_grade_config, dict) else False,
                            "two_offs_after_two_nig": bool(config_dict.get("two_offs_after_two_nig", False)),
                            "two_offs_after_three_nig": bool(config_dict.get("two_offs_after_three_nig", False)),
                            "not_one_night": bool(config_dict.get("not_one_night", False)),
                            "ban_n_to_d": bool(config_dict.get("ban_n_to_d", True)),
                            "ban_e_to_d": bool(config_dict.get("ban_e_to_d", True)),
                            "ban_n_to_e": bool(config_dict.get("ban_n_to_e", True)),
                        },
                    },
                )
        except Exception as _snapshot_exc:
            print(f"[ConstraintImpact] roster_system metadata attach failed: {_snapshot_exc}")
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
    shifts_db = (
        db.query(Shift)
        .filter(Shift.group_id == schedule.group_id, Shift.office_id == schedule.office_id)
        .all()
    )
    shift_id_to_int_id = {s.shift_id: s.id for s in shifts_db}
    weekend_off_nurse_ids: set[str] = set()
    try:
        rows = db.execute(
            text("SELECT nurse_id FROM nurses WHERE group_id = :group_id AND active = 1 AND is_weekend_off = 1"),
            {"group_id": schedule.group_id},
        ).fetchall()
        weekend_off_nurse_ids = {str(getattr(r, 'nurse_id', '')) for r in rows if getattr(r, 'nurse_id', None)}
    except Exception:
        weekend_off_nurse_ids = set()
    for nurse_id, shifts in generated.items():
        for day_index, shift_id in enumerate(shifts):
            if shift_id != '-':
                work_date = date(req.year, req.month, day_index + 1)
                main_code = str(shift_id).strip().upper()
                if main_code == "OFF":
                    main_code = "O"
                mapped_shift = default_map.get(main_code, shift_id)
                if (
                    str(nurse_id) in weekend_off_nurse_ids
                    and str(mapped_shift).strip() == '주'
                    and work_date.weekday() == 6
                ):
                    continue
                norm_shift = _normalize_shift_id_for_save(str(mapped_shift), shift_ids)
                entry = ScheduleEntry(
                    entry_id=str(uuid.uuid4().hex)[:16],
                    schedule_id=schedule.schedule_id,
                    nurse_id=nurse_id,
                    work_date=work_date,
                    shift_id=norm_shift,
                    id=shift_id_to_int_id.get(norm_shift),
                )
                db.add(entry)
        # try:
        #     print(f"[PersistRoster] nurse={nurse_id}, saved_shifts={shifts}")
        # except Exception:
        #     pass
    db.commit()


def _copy_transferred_entries(
    db: Session, schedule: Schedule, group_id: str, year: int, month: int
) -> int:
    """Source group의 issued schedule에서 파견/병동이동 간호사 shift를 현재 target schedule로 복사.

    source 마감 시 transfer_shifts_on_publish()로 전달했지만, target이 새 schedule을
    생성하면 entry가 유실된다. source의 issued schedule에서 직접 가져온다.
    """
    import uuid
    from services.assignment_service import get_active_assignments_for_month

    _assignments = get_active_assignments_for_month(db, group_id, year, month)
    # 파견/병동이동으로 들어오는 assignment (target == 이 그룹, start/end 월)
    _inbound = []
    print(f"[CopyTransfer] assignments={len(_assignments)}, group_id={group_id}, year={year}, month={month}")
    for a in _assignments:
        if a.reason not in ("파견", "병동이동"):
            continue
        if a.target_group_id != group_id:
            continue
        from services.day_windows import is_source_generated_month
        _a_end = a.end_date or a.expected_end_date
        _is_src = is_source_generated_month(a.start_date, _a_end, date(year, month, 1), monthrange(year, month)[1])
        print(f"[CopyTransfer] nurse={a.nurse_id}, reason={a.reason}, src={a.source_group_id}, is_src_gen={_is_src}")
        if _is_src:
            _inbound.append(a)
    if not _inbound:
        print(f"[CopyTransfer] 인바운드 없음 → skip")
        return 0
    print(f"[CopyTransfer] 인바운드 {len(_inbound)}건 처리 시작")

    from calendar import monthrange
    month_start = date(year, month, 1)
    month_end = date(year, month, monthrange(year, month)[1])
    from db.models import IssuedRoster, Shift

    count = 0
    for a in _inbound:
        src_gid = a.source_group_id
        # source group의 해당 월 issued(마감) schedule 찾기
        src_schedule = (
            db.query(Schedule)
            .filter(
                Schedule.group_id == src_gid,
                Schedule.year == year,
                Schedule.month == month,
                Schedule.status == "issued",
                Schedule.dropped == False,
            )
            .order_by(Schedule.version.desc())
            .first()
        )
        print(f"[CopyTransfer] src_gid={src_gid}, src_schedule={'found: '+src_schedule.schedule_id if src_schedule else 'NOT FOUND'}")
        if not src_schedule:
            continue

        # source shift → default_shift → target shift_id 매핑
        src_shifts = db.query(Shift).filter(Shift.group_id == src_gid).all()
        src_to_default = {s.shift_id: s.default_shift for s in src_shifts if s.default_shift}
        tgt_shifts = db.query(Shift).filter(Shift.group_id == group_id).all()
        default_to_tgt = {}
        for s in tgt_shifts:
            if s.default_shift and s.default_shift not in default_to_tgt:
                default_to_tgt[s.default_shift] = s.shift_id
        tgt_shift_id_to_int = {s.shift_id: s.id for s in tgt_shifts}

        # 전달 기간
        a_end = a.end_date or a.expected_end_date or month_end
        t_start = max(a.start_date, month_start)
        t_end = min(a_end, month_end)

        # source entry 조회
        src_entries = (
            db.query(ScheduleEntry)
            .filter(
                ScheduleEntry.schedule_id == src_schedule.schedule_id,
                ScheduleEntry.nurse_id == a.nurse_id,
                ScheduleEntry.work_date >= t_start,
                ScheduleEntry.work_date <= t_end,
            )
            .all()
        )
        if not src_entries:
            continue

        # 기존 target entry 삭제 (재복사 지원)
        db.query(ScheduleEntry).filter(
            ScheduleEntry.schedule_id == schedule.schedule_id,
            ScheduleEntry.nurse_id == a.nurse_id,
            ScheduleEntry.work_date >= t_start,
            ScheduleEntry.work_date <= t_end,
        ).delete(synchronize_session=False)

        for e in src_entries:
            src_shift = e.shift_id
            if not src_shift or src_shift == '-':
                continue
            default_code = src_to_default.get(src_shift, src_shift.upper())
            tgt_shift = default_to_tgt.get(default_code, default_code)
            new_entry = ScheduleEntry(
                entry_id=str(uuid.uuid4().hex)[:16],
                schedule_id=schedule.schedule_id,
                nurse_id=a.nurse_id,
                work_date=e.work_date,
                shift_id=tgt_shift,
                id=tgt_shift_id_to_int.get(tgt_shift),
            )
            db.add(new_entry)
            count += 1

    if count > 0:
        db.flush()
        print(f"[Assignment] 전달 entry 복사: {count}건 → schedule {schedule.schedule_id}")

    return count


def _count_work_assignments(
    generated: dict[str, list[str]] | None,
    shift_main_map: dict[str, str] | None = None,
) -> tuple[int, int]:
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
    off_codes = {"-", "O"}
    total_cells = 0
    work_cells = 0
    for shifts in generated.values():
        for raw_shift in shifts or []:
            total_cells += 1
            code = _normalize_assigned_code_for_validation(raw_shift, shift_main_map)
            if code in off_codes:
                continue
            work_cells += 1
    return total_cells, work_cells


def _build_validation_shift_main_map(roster_system) -> dict[str, str]:
    shift_main_map: dict[str, str] = {
        "OFF": "O",
        "O": "O",
        "주": "O",
    }
    try:
        ext_map = getattr(roster_system, "shift_id_to_main", None)
        if isinstance(ext_map, dict):
            for k, v in ext_map.items():
                key = str(k or "").strip().upper()
                val = str(v or "").strip().upper()
                if key and val:
                    shift_main_map[key] = val
    except Exception:
        pass
    try:
        cfg = getattr(roster_system, "config", None)
        defs = getattr(cfg, "shift_definitions", None)
        if isinstance(defs, list):
            for row in defs:
                sid = str((row or {}).get("shift_id") or "").strip().upper()
                default_shift = str((row or {}).get("default_shift") or "").strip().upper()
                if default_shift in {"OFF", "주"}:
                    default_shift = "O"
                if sid and default_shift in {"D", "E", "N", "O", "M", "W"}:
                    shift_main_map[sid] = default_shift
    except Exception:
        pass
    return shift_main_map


def _collect_validator_evidence(
    generated: dict[str, list[str]] | None,
    roster_system,
    shift_main_map: dict[str, str] | None = None,
) -> dict:
    """validator 실패 해석용 근거 스냅샷(경량).

    - day/shift 단위로 required/assigned/eligible를 수집
    - top_failed_cells(최대 50) 샘플 생성
    """
    try:
        cfg = getattr(roster_system, "config", None)
        if cfg is None or not isinstance(generated, dict):
            return {}

        nurses = list(getattr(roster_system, "nurses", []) or [])
        nurse_count = len(nurses)
        num_days = int(getattr(roster_system, "num_days", 0) or 0)
        if num_days <= 0:
            num_days = max((len(v or []) for v in generated.values()), default=0)
        if num_days <= 0:
            return {}

        join = list(getattr(roster_system, "join", []) or [])
        leave = list(getattr(roster_system, "leave", []) or [])
        blocked_by_nurse = getattr(roster_system, "blocked_by_nurse", None) or {}

        def _req_by_day(day_idx: int) -> dict[str, int]:
            by_day = getattr(cfg, "daily_shift_requirements_by_day", None)
            if isinstance(by_day, list) and day_idx < len(by_day) and isinstance(by_day[day_idx], dict):
                return {str(k).upper(): int(v or 0) for k, v in by_day[day_idx].items()}
            base = getattr(cfg, "daily_shift_requirements", None)
            if isinstance(base, dict):
                return {str(k).upper(): int(v or 0) for k, v in base.items()}
            return {}

        # assigned counter by day/shift
        assigned_map: dict[tuple[int, str], int] = defaultdict(int)
        for shifts in generated.values():
            for d, raw in enumerate(shifts or []):
                code = _normalize_assigned_code_for_validation(raw, shift_main_map)
                if code in {"-", "O"}:
                    continue
                assigned_map[(d, code)] += 1

        def _is_eligible(n_idx: int, d: int, code: str) -> bool:
            if n_idx < len(join) and n_idx < len(leave):
                if not (join[n_idx] <= d <= leave[n_idx]):
                    return False
            if d in (blocked_by_nurse.get(n_idx, set()) or set()):
                return False
            if n_idx < nurse_count:
                nu = nurses[n_idx]
                allowed = set(str(x).upper() for x in (getattr(nu, "allowed_shifts", None) or []))
                if allowed and code not in allowed:
                    return False
            return True

        cells: list[dict] = []
        for d in range(num_days):
            req = _req_by_day(d)
            for code, need in req.items():
                code_u = str(code).upper()
                if code_u == "O":
                    continue
                n_need = int(need or 0)
                if n_need <= 0:
                    continue
                assigned = int(assigned_map.get((d, code_u), 0) or 0)
                eligible = 0
                if nurse_count > 0:
                    for n_idx in range(nurse_count):
                        if _is_eligible(n_idx, d, code_u):
                            eligible += 1
                cells.append(
                    {
                        "day": d + 1,
                        "shift": code_u,
                        "required": n_need,
                        "assigned": assigned,
                        "eligible": eligible,
                        "shortage": max(0, n_need - assigned),
                        "eligible_gap": max(0, n_need - eligible),
                    }
                )

        failed = [c for c in cells if c["assigned"] < c["required"]]
        failed.sort(key=lambda x: (x["shortage"], x["eligible_gap"]), reverse=True)
        top_failed = failed[:50]

        # Attach blocking_axes per cell (S3)
        _shift_to_capacity_axis = {
            "N": "night_capacity",
            "D": "day_capacity",
            "E": "evening_capacity",
            "M": "mid_capacity",
        }
        for c in top_failed:
            sh = str(c.get("shift") or "").upper()
            primary = _shift_to_capacity_axis.get(sh)
            blocking: list[str] = []
            if primary:
                blocking.append(primary)
            eligible = int(c.get("eligible") or 0)
            required = int(c.get("required") or 0)
            assigned = int(c.get("assigned") or 0)
            # Eligibility lock: 후보 자체가 부족
            if eligible < required:
                blocking.append("allowed_shift_mask")
            # Coverage-but-eligible: 후보는 있지만 배정 안 됨 → fixed/carryover/team_min 의심
            elif eligible >= required and assigned < required:
                # team_min / fixed_lock / carryover_lock 중 후속 단계에서 분기. 일단 capacity 만 표시.
                pass
            c["blocking_axes"] = blocking
            c["blocking_detail"] = {
                "primary_axis": primary,
                "eligibility_gap": max(0, required - eligible),
                "assignment_gap": max(0, required - assigned),
                "shift": sh,
            }

        # fixed/forbidden 및 carryover 단서 집계
        merged_ic = getattr(roster_system, "_constraint_impact_merged_initial_constraints", None) or {}
        forbidden_map = (merged_ic or {}).get("forbidden") or {}
        fixed_forbidden_count = 0
        if isinstance(forbidden_map, dict):
            for day_map in forbidden_map.values():
                if not isinstance(day_map, dict):
                    continue
                for codes in day_map.values():
                    fixed_forbidden_count += len(list(codes or []))

        carryover_artifacts = getattr(roster_system, "_constraint_impact_carryover_artifacts", None) or []
        carryover_artifact_count = len(list(carryover_artifacts or []))

        return {
            "total_failed_cells": len(failed),
            "top_failed_cells": top_failed,
            "eligible_zero_cells": sum(1 for c in failed if c["eligible"] <= 0),
            "required_minus_assigned_total": sum(int(c["shortage"]) for c in failed),
            "fixed_forbidden_count": int(fixed_forbidden_count),
            "carryover_artifact_count": int(carryover_artifact_count),
        }
    except Exception:
        return {}


def _normalize_assigned_code_for_validation(
    raw_shift: object,
    shift_main_map: dict[str, str] | None = None,
) -> str:
    code = str(raw_shift).strip().upper() if raw_shift is not None else "-"
    if not code:
        return "-"
    if code in {"-", "OFF", "O", "주"}:
        return "O" if code != "-" else "-"
    if shift_main_map and code in shift_main_map:
        return str(shift_main_map[code]).strip().upper()
    if code in {"D", "E", "N", "O", "M", "W"}:
        return code
    return code


def _extract_unrecoverable_violated_constraints(
    roster_system,
    generated: dict[str, list[str]] | None,
    validation_error: str | None,
) -> list[dict]:
    """Unrecoverable infeasibility 케이스에서 인과 제약 리스트를 추출한다.

    출처:
      1. validation_error 문자열의 reason_code 패턴 (예: `[reason_code=NO_ASSIGNMENT]`)
      2. roster_system.blocked_by_nurse — 해당 nurse가 전부 차단된 케이스

    주의: 여기서 가짜 원인을 휴리스틱으로 추측하지 않는다. 실제 cause 는
    하류 build_unrecoverable_payload (pool snapshot · conflict_detector ·
    structural_diagnosis · CP-SAT MUS) 가 결정론적으로 만든다.

    반환 항목 형식:
        {"node_id", "slack", "details", "reason_code", "human_message_ko"}

    node_id prefix 컨벤션은 ontology dashboard의 _ctype_from_id가
    typed ConstraintNode로 매핑한다.
    """
    import re

    out: list[dict] = []
    seen_codes: set[str] = set()
    err = str(validation_error or "")

    REASON_TO_NODE = {
        "CAPACITY_TOTAL_SHORTAGE": ("infeasibility:capacity_total", "ConstraintNode"),
        "N_CAPACITY_SHORTAGE":     ("infeasibility:n_capacity", "ConstraintNode"),
        "MAX_CAP_SHORTAGE":        ("infeasibility:max_cap_shortage", "ConstraintNode"),
        "GRADE_MAX_SUM_BELOW_NEED":("grade_max:sum_below_need", "GradeMaxNode"),
        "GRADE_HARD_PROBE":        ("grade_max:hard_probe", "GradeMaxNode"),
    }

    def _push(reason_code: str, human_msg: str | None, slack=None, details=None):
        if not reason_code or reason_code in seen_codes:
            return
        seen_codes.add(reason_code)
        node_id, _ = REASON_TO_NODE.get(reason_code, (f"infeasibility:{reason_code.lower()}", "ConstraintNode"))
        out.append({
            "node_id": node_id,
            "reason_code": reason_code,
            "slack": slack,
            "details": details or {},
            "human_message_ko": (human_msg or "")[:400] if human_msg else None,
        })

    for m in re.finditer(r"\[reason_code=([A-Z_]+)\]", err):
        _push(m.group(1), err, details={"source": "validation_error"})

    # 미배정 cell 자체는 "결과(symptom)" 이며 cause 가 아니다.
    # 가짜 원인을 추측하던 _build_infeasible_diagnosis / _probe_first_grade_hard_blocker
    # 휴리스틱은 제거됨. 실제 cause 는 team_grade_precheck 의 산술 detector +
    # cause_inferer 의 MUS pattern 추론이 구체 cause_id 로 만든다.

    try:
        blocked_by_nurse = getattr(roster_system, "blocked_by_nurse", None) or {}
        nurses = list(getattr(roster_system, "nurses", []) or [])
        for idx, blocked_days in blocked_by_nurse.items():
            try:
                if not blocked_days:
                    continue
                if idx < len(nurses):
                    nurse_id = str(getattr(nurses[idx], "nurse_id", idx))
                else:
                    nurse_id = str(idx)
                node_id = f"nurse:{nurse_id}"
                if node_id in {x.get("node_id") for x in out}:
                    continue
                out.append({
                    "node_id": node_id,
                    "reason_code": "NURSE_BLOCKED_DAYS",
                    "slack": -len(blocked_days),
                    "details": {"blocked_day_count": len(blocked_days), "source": "blocked_by_nurse"},
                    "human_message_ko": f"간호사 {nurse_id}가 {len(blocked_days)}일 차단됨",
                })
                if len(out) >= 50:
                    return out
            except Exception:
                continue
    except Exception:
        pass

    return out[:50]


def _validate_generated_roster(
    generated: dict[str, list[str]] | None,
    roster_system,
    nurses_context: list | None = None,
    config_context: dict | None = None,
    grade_config_context: dict | None = None,
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
    def _with_ontology(msg: str | None) -> str | None:
        if msg and roster_system is not None:
            try:
                setattr(roster_system, "_ontology_last_reason", attach_reason_code_ontology(message=msg, severity="hard"))
            except Exception:
                pass
        return msg

    shift_main_map = _build_validation_shift_main_map(roster_system)
    total_cells, work_cells = _count_work_assignments(generated, shift_main_map)
    try:
        if roster_system is not None:
            setattr(
                roster_system,
                "_validator_evidence",
                _collect_validator_evidence(generated, roster_system, shift_main_map),
            )
    except Exception:
        pass

    if total_cells > 0 and work_cells == 0:
        # 엔진이 실근무를 한 건도 배정하지 못함 = infeasible 의 '증상(symptom)'.
        # 여기서 가짜 원인(grade_max cap 추측 등)을 만들어내지 않는다. 실제 cause 는
        # 하류 build_unrecoverable_payload (pool snapshot · conflict_detector ·
        # structural_diagnosis · CP-SAT MUS) 가 결정론적으로 진단한다.
        # _infeasible_empty 플래그는 soft-fallback 자동재시도의 '실제 신호'다
        # (NO_ASSIGNMENT 문자열 매칭 대신 사용).
        try:
            setattr(roster_system, "_infeasible_empty", True)
        except Exception:
            pass
        ev = getattr(roster_system, "_validator_evidence", None) or {}
        ev_brief = {
            "total_failed_cells": ev.get("total_failed_cells"),
            "required_minus_assigned_total": ev.get("required_minus_assigned_total"),
            "eligible_zero_cells": ev.get("eligible_zero_cells"),
            "fixed_forbidden_count": ev.get("fixed_forbidden_count"),
            "carryover_artifact_count": ev.get("carryover_artifact_count"),
        }
        return _with_ontology(
            f"[reason_code=NO_ASSIGNMENT] 실근무 배정 0건(증상). 원인은 구조진단에서 결정. evidence={ev_brief}"
        )

    # day-zero coverage 감지 — symptom 라벨은 throw 하지 않고 cause probe 4축을 강제 실행.
    # cause 가 식별되면 그것만 반환, 모든 probe 가 침묵하면 UNDIAGNOSED sentinel + evidence dump.
    try:
        cfg = getattr(roster_system, "config", None)
        num_days = getattr(roster_system, "num_days", 0) or 0
        shift_types = list(getattr(cfg, "shift_types", []) or [])

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
                    code = _normalize_assigned_code_for_validation(shifts[d], shift_main_map)
                    if code in actual:
                        actual[code] += 1

                total_actual = sum(actual.values())
                if total_actual == 0:
                    # 특정일 coverage 0 = 증상. 가짜 원인 추측 없이 plain symptom +
                    # evidence 만 반환하고, 실제 cause 는 하류 구조진단이 결정한다.
                    try:
                        setattr(roster_system, "_infeasible_empty", True)
                    except Exception:
                        pass
                    ev = getattr(roster_system, "_validator_evidence", None) or {}
                    req_msg = ", ".join(f"{k}={v}" for k, v in req.items())
                    ev_brief = {
                        "day": d + 1,
                        "required": req,
                        "total_failed_cells": ev.get("total_failed_cells"),
                        "eligible_zero_cells": ev.get("eligible_zero_cells"),
                        "fixed_forbidden_count": ev.get("fixed_forbidden_count"),
                        "carryover_artifact_count": ev.get("carryover_artifact_count"),
                    }
                    return _with_ontology(
                        f"[reason_code=UNDIAGNOSED] day-zero trigger fired but no root cause identified "
                        f"(day={d + 1}, req={req_msg}). evidence={ev_brief}"
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
        nurse_schedule = []
        for d in range(1, roster_data["days_in_month"] + 1):
            shift_code = entries_by_nurse.get(nurse.nurse_id, {}).get(d, '-')
            nurse_schedule.append(shift_code)
        counts = {shift: nurse_schedule.count(shift) for shift in shift_colors.keys()}
        nurse_entry = {
            "id": nurse.nurse_id,
            "name": nurse.name,
            "experience": nurse.experience,
            "schedule": nurse_schedule,
            "counts": counts,
        }
        if getattr(nurse, 'is_inbound', False):
            nurse_entry["is_inbound"] = True
            nurse_entry["source_group_id"] = getattr(nurse, 'group_id', None)
        roster_data["nurses"].append(nurse_entry)
    return roster_data


def _compute_coverage_gaps(roster_system) -> list[dict]:
    """현재 roster vs daily_shift_requirements 비교해 부족분 리스트를 반환.

    primary 솔버는 coverage hard지만, INFEASIBLE 시 fallback에서 soft로 떨어져
    shortage가 남을 수 있다. 사용자 진단용으로 일/시프트별 부족 셀을 노출한다.
    """
    try:
        cfg = roster_system.config
        shift_types = list(getattr(cfg, "shift_types", []) or [])
        if not shift_types or not hasattr(roster_system, "roster"):
            return []
        ds_by_day = getattr(cfg, "daily_shift_requirements_by_day", None)
        base_req = getattr(cfg, "daily_shift_requirements", {}) or {}
        N = len(roster_system.nurses)
        gaps: list[dict] = []
        for d in range(roster_system.num_days):
            need_map = (
                ds_by_day[d]
                if isinstance(ds_by_day, list) and d < len(ds_by_day) and isinstance(ds_by_day[d], dict)
                else base_req
            )
            for code, req_val in (need_map or {}).items():
                s_code = str(code or "").strip().upper()
                if s_code not in shift_types:
                    continue
                req = int(req_val or 0)
                if req <= 0:
                    continue
                s_idx = shift_types.index(s_code)
                assigned = int(sum(int(roster_system.roster[n, d, s_idx]) for n in range(N)))
                if assigned < req:
                    gaps.append({
                        "day": d + 1,
                        "shift": s_code,
                        "need": req,
                        "assigned": assigned,
                        "short": req - assigned,
                    })
        return gaps
    except Exception as exc:
        print(f"[CoverageGaps] 계산 실패: {exc}")
        return []


def _build_constraint_impact_payload(roster_system, req) -> dict:
    """생성 완료된 roster_system 기준 constraint-impact 요약을 생성한다."""
    started = time.perf_counter()
    try:
        from services.constraint_impact import (
            analyze_current_roster,
            build_current_atoms_from_roster_system,
            build_semantics_snapshot_from_roster_system,
        )

        snapshot = build_semantics_snapshot_from_roster_system(
            roster_system,
            year=req.year,
            month=req.month,
        )
        atoms = build_current_atoms_from_roster_system(snapshot, roster_system)
        analysis = analyze_current_roster(snapshot=snapshot, current_atoms=atoms)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        used_fallback = bool(getattr(roster_system, "_used_fallback", False))
        coverage_gaps = _compute_coverage_gaps(roster_system)

        # Phase B: surface solver-attach-point emit recordings.
        # Outcome-conditional retention:
        #   SAT  → aggregate + interesting_events (bypassed_by_fixed) only
        #   UNSAT/infeasibility → full granular records + ConflictProbeReport
        emit_rec = getattr(roster_system, "_constraint_impact_solver_emit_recorder", None)
        emit_summary: dict[str, dict[str, int]] = {}
        interesting_events: list[dict] = []
        full_records_for_unsat: list[dict] = []
        conflict_probe_payload: dict | None = None
        outcome_label = "sat" if analysis.valid_under_current_semantics else "unsat"
        if emit_rec is not None:
            try:
                records = emit_rec.records()
                for r in records:
                    fam = emit_summary.setdefault(r.family, {})
                    fam[r.mode] = fam.get(r.mode, 0) + 1
                    if r.mode != "enforced":
                        interesting_events.append(r.to_dict())
                if outcome_label == "unsat":
                    # cap per family to keep payload bounded; agents can re-fetch via dedicated endpoint
                    per_family_cap = 50
                    counter: dict[str, int] = {}
                    for r in records:
                        c = counter.get(r.family, 0)
                        if c >= per_family_cap:
                            continue
                        counter[r.family] = c + 1
                        full_records_for_unsat.append(r.to_dict())
                    from services.constraint_impact.conflict_probe import (
                        build_conflict_probe_report,
                    )
                    probe = build_conflict_probe_report(emit_records=records)
                    conflict_probe_payload = {
                        "ranked_candidates": [
                            {
                                "family": c.family,
                                "score": c.score,
                                "relaxation_priority": c.relaxation_priority,
                                "scope_explosion": c.scope_explosion,
                                "emit_count": c.emit_count,
                                "matched_scenario_ids": c.matched_scenario_ids,
                                "reasons": c.reasons,
                                "sample_records": c.sample_records,
                            }
                            for c in probe.ranked_candidates
                        ],
                        "matched_scenarios": [
                            {
                                "scenario_id": s.scenario_id,
                                "involved_families": s.involved_families,
                                "confidence": s.confidence,
                                "suggested_relaxation": s.suggested_relaxation,
                                "why_infeasible": s.why_infeasible,
                                "detection_hint": s.detection_hint,
                            }
                            for s in probe.matched_scenarios
                        ],
                        "probe_plan": [
                            {
                                "order": p.order,
                                "family": p.family,
                                "action": p.action,
                                "rationale": p.rationale,
                            }
                            for p in probe.probe_plan
                        ],
                        "notes": probe.notes,
                    }
            except Exception:
                pass
        return {
            "enabled": True,
            "timing_ms": elapsed_ms,
            "valid_under_current_semantics": analysis.valid_under_current_semantics,
            "atom_count": analysis.atom_count,
            "fixed_atom_count": analysis.fixed_atom_count,
            "preceptee_atom_count": analysis.preceptee_atom_count,
            "coverage_excluded_atom_count": analysis.coverage_excluded_atom_count,
            "hard_violation_count": analysis.hard_violation_count,
            "risky_constraint_count": analysis.risky_constraint_count,
            "constraint_mode_summary": analysis.constraint_mode_summary,
            "preflight_alerts": analysis.preflight_alerts,
            "violated_constraints": [
                {
                    "node_id": v.node_id,
                    "slack": v.slack,
                    "pressure": v.pressure,
                    "details": v.details,
                }
                for v in analysis.violated_constraints[:50]
            ],
            "risky_constraints": [
                {
                    "node_id": v.node_id,
                    "slack": v.slack,
                    "pressure": v.pressure,
                    "details": v.details,
                }
                for v in analysis.risky_constraints[:50]
            ],
            "snapshot_meta": {
                "attempt_label": snapshot.attempt.label,
                "attempt_index": snapshot.attempt.attempt_index,
                "forced_grade_soft_fallback": snapshot.attempt.forced_grade_soft_fallback,
                "used_fallback": used_fallback,
                "nurse_count": len(snapshot.nurses),
                "fixed_cell_count": len(snapshot.fixed_cells),
                "preceptee_count": len(snapshot.preceptee_facts),
            },
            "solver_status": "fallback" if used_fallback else "primary",
            "coverage_gaps": coverage_gaps,
            "outcome": outcome_label,
            "solver_emitted_summary": emit_summary,
            "interesting_emit_events": interesting_events,
            "solver_emitted_nodes": full_records_for_unsat,
            "conflict_probe": conflict_probe_payload,
        }
    except Exception as e:
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        return {
            "enabled": False,
            "timing_ms": elapsed_ms,
            "error": str(e),
        }


def _build_constraint_impact_assignment_windows(assignments, target_group_id: str, month_start: date, days_in_month: int) -> list[dict]:
    month_end = month_start + timedelta(days=days_in_month - 1)
    rows: list[dict] = []
    for a in assignments or []:
        nurse_id = str(getattr(a, "nurse_id", "") or "")
        if not nurse_id:
            continue
        reason = str(getattr(a, "reason", "") or "assignment")
        source_gid = getattr(a, "source_group_id", None)
        target_gid = getattr(a, "target_group_id", None)
        start = getattr(a, "start_date", None)
        if start is None:
            continue
        if reason == "병동이동":
            end = getattr(a, "expected_end_date", None) or month_end
        else:
            end = getattr(a, "end_date", None) or getattr(a, "expected_end_date", None) or month_end
        overlap_start = max(start, month_start)
        overlap_end = min(end, month_end)
        active_days = set()
        if overlap_start <= overlap_end:
            active_days = {int((overlap_start - month_start).days + i) for i in range((overlap_end - overlap_start).days + 1)}
        all_days = set(range(days_in_month))
        direction = "inbound" if target_gid == target_group_id and source_gid != target_group_id else "outbound"
        if reason == "병동이동":
            direction = "transfer"
        if reason in ("휴직", "퇴사"):
            direction = "leave"
        if reason == "교육":
            direction = "training"
        rows.append(
            {
                "nurse_id": nurse_id,
                "direction": direction,
                "source_group_id": source_gid,
                "target_group_id": target_gid,
                "reason": reason,
                "active_day_indices": sorted(active_days),
                "inactive_day_indices": sorted(all_days - active_days),
                "allowed_shift_codes": list(getattr(a, "target_shift_types", None) or []),
                "carries_state": direction in {"inbound", "transfer", "training"},
                "counts_to_coverage": True,
                "metadata": {
                    "start_date": str(start),
                    "end_date": str(end) if end is not None else None,
                },
            }
        )
    return rows


def _build_constraint_impact_carryover_artifacts(
    db: Session,
    inbound_assignments: list,
    outbound_assignments: list,
    year: int,
    month: int,
) -> list[dict]:
    """현재 파견/복귀 정책과 동일한 schedule 선택 우선순위로 carryover artifact를 생성한다.

    정책:
    - 참조 schedule 선택: issued > latest > blank
    - inbound: source 병동 기준
    - outbound 복귀: target 병동 기준
    - tail metrics는 기존 mid-month boundary 계산과 동일 helper 사용
    """
    month_start = date(year, month, 1)
    lookback = 6
    rows: list[dict] = []

    def _group_code2main(group_id: str | None) -> dict:
        if not group_id:
            return {}
        out: dict[str, str] = {}
        try:
            shifts = db.query(Shift).filter(Shift.group_id == group_id).all()
            _GB = {"데이": "D", "이브닝": "E", "나이트": "N", "미드": "M"}
            for s in shifts:
                sid = str(getattr(s, "shift_id", "") or "").strip().upper()
                if not sid:
                    continue
                sgb = str(getattr(s, "shift_gb", "") or "").strip()
                if sgb in _GB:
                    out[sid] = _GB[sgb]
                    continue
                ds = str(getattr(s, "default_shift", "") or "").strip().upper()
                if ds in ("OFF", "주"):
                    ds = "O"
                if ds in ("D", "E", "N", "M", "O"):
                    out[sid] = ds
        except Exception:
            return {}
        return out

    def _append_artifact(*, nurse_id: str, direction: str, boundary_day_index: int, reference_group_id: str | None, schedule_id: str | None, basis: str, tail: list[str], carries_state: bool, metadata: dict):
        rows.append(
            {
                "nurse_id": nurse_id,
                "direction": direction,
                "boundary_day_index": boundary_day_index,
                "reference_group_id": reference_group_id,
                "selected_schedule_id": schedule_id,
                "selected_schedule_basis": basis,
                "carries_state": carries_state,
                "tail_sequence": list(tail or []),
                "metrics": _calc_tail_metrics(tail) if tail else {},
                "metadata": metadata,
            }
        )

    for a in inbound_assignments or []:
        nurse_id = str(a.nurse_id)
        boundary_day = (a.start_date - month_start).days
        if boundary_day <= 0:
            continue
        schedule_id, basis = _query_schedule_ref_for_month(db, a.source_group_id, year, month)
        tail = _get_boundary_tail(db, schedule_id, nurse_id, boundary_day, lookback, _group_code2main(a.source_group_id)) if schedule_id else []
        _append_artifact(
            nurse_id=nurse_id,
            direction="inbound" if a.reason != "병동이동" else "transfer",
            boundary_day_index=boundary_day,
            reference_group_id=a.source_group_id,
            schedule_id=schedule_id,
            basis=basis,
            tail=tail,
            carries_state=True,
            metadata={
                "reason": a.reason,
                "start_date": str(a.start_date),
                "expected_end_date": str(getattr(a, "expected_end_date", None)) if getattr(a, "expected_end_date", None) else None,
            },
        )

    for a in outbound_assignments or []:
        nurse_id = str(a.nurse_id)
        a_end = a.end_date or a.expected_end_date
        if not a_end:
            continue
        if a_end.year != year or a_end.month != month:
            continue
        return_day = (a_end - month_start).days + 1
        if return_day <= 0:
            continue
        schedule_id, basis = _query_schedule_ref_for_month(db, a.target_group_id, year, month)
        tail = _get_boundary_tail(db, schedule_id, nurse_id, return_day, lookback, _group_code2main(a.target_group_id)) if schedule_id else []
        _append_artifact(
            nurse_id=nurse_id,
            direction="outbound" if a.reason != "병동이동" else "transfer",
            boundary_day_index=return_day,
            reference_group_id=a.target_group_id,
            schedule_id=schedule_id,
            basis=basis,
            tail=tail,
            carries_state=True,
            metadata={
                "reason": a.reason,
                "return_date": str(a_end + timedelta(days=1)),
            },
        )

    return rows


def _compile_boundary_overlap_constraints(
    *,
    nurse_id: str,
    boundary_day: int,
    days_in_month: int,
    cons_work: int,
    cons_n: int,
    last_shift: str | None,
    offs_after: int,
    K: int,
    L: int,
    two_after_two: bool,
    two_after_three: bool,
    ban_e_to_d: bool,
    ban_n_to_d: bool,
    ban_n_to_e: bool,
) -> dict:
    forced_off: list[int] = []
    forbidden: dict[int, list[str]] = defaultdict(list)
    off_window_constraints: list[list[int]] = []

    if K and cons_work >= K:
        forced_off.append(boundary_day)

    if K > 0 and cons_work > 0:
        window_end = boundary_day + max(0, K - cons_work)
        window_end = min(window_end, days_in_month - 1)
        if window_end >= boundary_day:
            off_window_constraints.append([boundary_day, window_end])

    req_offs = 0
    if two_after_three and L and cons_n >= 3:
        req_offs = 2
    elif two_after_two and L and cons_n >= 2:
        req_offs = 2
    rem = max(0, req_offs - offs_after)
    for i in range(min(2, rem)):
        if boundary_day + i < days_in_month:
            forced_off.append(boundary_day + i)

    if last_shift == 'E' and ban_e_to_d:
        forbidden[boundary_day].append('D')
    if last_shift == 'N' and ban_n_to_d:
        forbidden[boundary_day].append('D')
    if last_shift == 'N' and ban_n_to_e:
        forbidden[boundary_day].append('E')
    if L and cons_n >= L and offs_after == 0:
        forbidden[boundary_day].append('N')

    return {
        'forced_off': sorted(set(forced_off)),
        'forbidden': {d: sorted(set(v)) for d, v in forbidden.items()},
        'off_window_constraints': off_window_constraints,
    }


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
            - default_map: 메인코드(D/E/N/M/O/주) → shift_id 매핑
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
    """Shift.default_shift를 메인코드(D/E/N/M/O/주)로 삼아 실제 shift_id 매핑을 구성한다.

    Args:
        shifts: 해당 그룹/오피스의 Shift 레코드 목록

    Returns:
        메인코드(D/E/N/M/O/주) → shift_id 매핑
    """
    mapping: dict[str, str] = {}
    for s in shifts:
        default_code = str(getattr(s, "default_shift", "") or "").strip().upper()
        if default_code == "OFF":
            default_code = "O"
        if default_code not in {"D", "E", "N", "M", "O", "주"}:
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

def generate_roster_service(req: RosterRequest, current_user, db: Session, treatment_ids=None):
    """
    근무표 생성 서비스 함수 (cp_sat_basic 엔진만 사용)
    """
    if not current_user or not caller_is_head_nurse(db, current_user):
        raise Exception("Permission denied")
    # 대상 그룹: 토큰 group_id 대신 req.group_id(없으면 DB home)로 해석·검증.
    # 이후 모든 current_user.group_id 참조가 검증된 대상 그룹을 가리킨다(HN home 외 관리 그룹 포함).
    current_user.group_id = resolve_effective_group(
        db, current_user, getattr(req, "group_id", None)
    )
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
    # ── 병동이동 레이지 체크 + 프리셉티 만료 체크 + assignment 기반 blocked_by_nurse 구성 ──
    flush_pending_transfers(db, current_user.group_id)
    from services.assignment_service import flush_expired_preceptees
    flush_expired_preceptees(db)
    _assignments = get_active_assignments_for_month(db, current_user.group_id, req.year, req.month)
    print(f"[Assignment] group_id={current_user.group_id}, year={req.year}, month={req.month}, assignments_count={len(_assignments)}")
    for _a in _assignments:
        print(f"[Assignment] id={_a.id}, nurse_id={_a.nurse_id}, reason={_a.reason}, source={_a.source_group_id}, target={_a.target_group_id}, start={_a.start_date}, end={_a.end_date}")
    # ── 인바운드: source/target 독립 생성 — 모든 인바운드를 엔진에 추가 ──
    _inbound_assignments = []
    _outbound_assignments = []
    for a in _assignments:
        if a.reason not in ("파견", "병동이동"):
            continue
        if a.target_group_id != current_user.group_id:
            continue
        if a.source_group_id == current_user.group_id:
            continue
        _inbound_assignments.append(a)
    _existing_nurse_ids = {str(n.nurse_id) for n in engine_nurses}
    _inbound_nurse_ids = {str(a.nurse_id) for a in _inbound_assignments} - _existing_nurse_ids
    # 병동이동 flush 후: nurse.group_id가 이미 target으로 옮겨져 engine_nurses에 자연 포함된 케이스.
    # 이런 간호사도 mid-month 시작이면 active_range를 assignment 기간으로 클리핑해야
    # weekly_off가 이동 이전 일자에 박히는 누수를 막을 수 있다.
    _flushed_transfer_ids = {str(a.nurse_id) for a in _inbound_assignments} & _existing_nurse_ids
    if _inbound_nurse_ids:
        _inbound_nurses = db.query(Nurse).filter(
            Nurse.nurse_id.in_(_inbound_nurse_ids),
            Nurse.active == 1,
        ).all()
        # assignment의 target 설정으로 nurse 속성 오버라이드.
        # 정책: inbound nurse 는 nurse_assignment.target_* 만 진실 원천.
        #   target_* 가 NULL 이면 default(비활성/없음) 적용. source(nurses table) 값으로 fallback 금지.
        # 주의: SQLAlchemy ORM instrumented attr setter 는 `[n.__dict__ for n in nurses_in_group]`
        #   변환 시 dict 에 미반영될 수 있어 `n.__dict__` 직접 주입으로 강제 동기화한다.
        _assignment_by_nurse = {str(a.nurse_id): a for a in _inbound_assignments}
        for n in _inbound_nurses:
            n.__dict__['is_inbound'] = True
            _a = _assignment_by_nurse.get(str(n.nurse_id))
            if _a:
                d = n.__dict__
                # team SSOT = nurse_team_period. 아래 resolve_team_for_roster 가 period 로 채운다
                # (없으면 None=팀 미배정). target_team_id 는 더 이상 team 결정에 쓰지 않는다.
                d['team_id'] = None
                d['weekly_off_enabled'] = bool(_a.target_weekly_off_enabled or 0)
                d['weekly_off_type'] = _a.target_weekly_off_type
                d['weekly_off_weekday'] = _a.target_weekly_off_weekday
                if _a.reason == "파견":
                    # 파견 = 임시 overlay(period 미관여) → dispatch 프로필(target_*) 사용.
                    d['grade'] = _a.target_grade
                    d['allowed_shifts'] = _a.target_shift_types or []
                    d['fixed_shift'] = _a.target_fixed_shift
                else:
                    # 병동이동(영구) = period as-of(target group=현 그룹). __dict__ 직접주입(영속화 회피).
                    from services.nurse_period_resolver import fetch_periods as _fp, resolve_asof as _ra
                    from db.models import NurseGradePeriod as _GP2, NurseAllowedShiftPeriod as _AP2
                    _nid2 = str(n.nurse_id)
                    _nx2 = month_start + timedelta(days=1)
                    _g2 = _fp(db, _GP2, [_nid2], month_start, _nx2, group_id=current_user.group_id)
                    d['grade'] = _ra(_g2.get(_nid2), month_start, "grade", default=getattr(n, "grade", None))
                    _ap2 = _fp(db, _AP2, [_nid2], month_start, _nx2)
                    d['allowed_shifts'] = _ra(_ap2.get(_nid2), month_start, "allowed_shifts",
                                              default=(getattr(n, "allowed_shifts", None) or []))
                    d['fixed_shift'] = _ra(_ap2.get(_nid2), month_start, "fixed_shift",
                                           default=getattr(n, "fixed_shift", None))
        engine_nurses.extend(_inbound_nurses)
        nurses_in_group.extend(_inbound_nurses)
        print(
            f"[Assignment][Inbound] 인바운드 간호사 {len(_inbound_nurses)}명 엔진 추가: "
            f"{[f'{n.name}({n.nurse_id})' for n in _inbound_nurses]}"
        )
    if _flushed_transfer_ids:
        _flushed_names = []
        for n in engine_nurses:
            if str(n.nurse_id) in _flushed_transfer_ids:
                # 인바운드와 동일하게 마킹하여 build_blocked_days에서 인바운드 분기를 타게 한다.
                # SQLAlchemy ORM 우회 위해 __dict__ 직접 주입.
                n.__dict__['is_inbound'] = True
                _flushed_names.append(f'{getattr(n, "name", "?")}({n.nurse_id})')
        print(
            f"[Assignment][FlushedTransfer] 병동이동 flush된 간호사 {len(_flushed_transfer_ids)}명 활동범위 클리핑 대상: "
            f"{_flushed_names}"
        )
    active_range_candidates = {
        str(n.nurse_id): _active_range_in_month(n, month_start, days_in_month)
        for n in engine_nurses
    }
    # 인바운드 간호사: active_range를 assignment 기간 기준으로 오버라이드 (joining_date가 다른 그룹 기준이므로)
    # 병동이동 flush 케이스(_flushed_transfer_ids)도 동일하게 클리핑한다.
    # 단 병동이동은 영구 이동이므로 end_date(=flush 시각의 today)로 clip하지 않고 월말까지 활성으로 본다.
    _assignment_by_nurse_all = {str(a.nurse_id): a for a in _inbound_assignments}
    _override_targets = _inbound_nurse_ids | _flushed_transfer_ids
    for nid in _override_targets:
        _ia = _assignment_by_nurse_all.get(nid)
        if _ia is None:
            continue
        _a_start = _ia.start_date
        _month_end = month_start + timedelta(days=days_in_month - 1)
        if _ia.reason == "병동이동":
            # 병동이동은 영구 이동이라 end_date(today marker)는 무시하고 월말까지 활성
            _a_end = _ia.expected_end_date or _month_end
        else:
            _a_end = _ia.end_date or _ia.expected_end_date or _month_end
        _s = max(_a_start, month_start)
        _e = min(_a_end, _month_end)
        if _s <= _e:
            active_range_candidates[nid] = (((_s - month_start).days), ((_e - month_start).days))
    # 휴직/퇴사: nurse_assignment.start_date 이후 nurse 자체를 비활성화한다.
    # `_active_range_in_month` 는 nurses.resignation_date 만 보므로 별도 클리핑 필요.
    _leave_assignments_main = [
        a for a in _assignments
        if a.reason in ("휴직", "퇴사")
        and a.source_group_id == current_user.group_id
    ]
    _leave_clipped_main = _clip_active_range_for_leaves(
        active_range_candidates, _leave_assignments_main, month_start, days_in_month
    )
    if _leave_clipped_main:
        print(f"[Assignment][Leave] 휴직/퇴사 active_range 클리핑: {_leave_clipped_main}")
    # [B4] 영구 전출(병동이동) source-side: start_date 경계로 active_range 클리핑 → 엔진에서 제외.
    #   영구 이동은 source 병동에서 그 날부터 빠져야 한다. 그렇지 않으면 전출자가 "전 일자 차단"
    #   상태로 엔진에 잔류해 팀/등급 풀의 유효 인력을 갉아먹고 커버리지를 구조적으로 막는다.
    #   파견(일시)은 복귀하므로 제외하지 않고 기존 day-block(아래)을 유지한다 — 병동이동만 분기.
    _transfer_out_main = [
        a for a in _assignments
        if a.reason == "병동이동"
        and a.source_group_id == current_user.group_id
        and a.target_group_id != current_user.group_id
    ]
    _transfer_clipped_main = _clip_active_range_for_leaves(
        active_range_candidates, _transfer_out_main, month_start, days_in_month
    )
    if _transfer_clipped_main:
        print(f"[Assignment][TransferOut] 영구 전출 active_range 클리핑(엔진 제외): {_transfer_clipped_main}")
    # [Phase1-B2] team read 전환: nurse_team_period(SSOT) 가 진실.
    #   period 가 그 달을 덮으면 그 값으로 team_id 를 확정한다(재분배 B3가 기록한 미래 팀 포함).
    #   None(구간 없음)이면 기존 값 유지 — 홈은 ward-aware 폴백=캐시(현행 동일),
    #   인바운드는 4689 의 target_team_id 보존(period 없을 때 비회귀).
    from services.team_period import resolve_team_for_roster
    from services.cp_sat.allowed_shift_types import is_n_only_profile
    for _en in engine_nurses:
        # N전담(허용 shift=N뿐)은 팀 D/E 커버리지 로테이션에 참여 불가 → 미지정(team None).
        #   team_id=None 이면 team_constraints 가 자동 스킵 → 유령 멤버로 팀 인원/커버리지를
        #   부풀리지 않는다. 야간 수급은 글로벌(nig_req)이라 영향 없음.
        if is_n_only_profile(getattr(_en, 'allowed_shifts', None)):
            _en.__dict__['team_id'] = None
            continue
        _rt = resolve_team_for_roster(
            db, str(_en.nurse_id), current_user.group_id, req.year, req.month
        )
        if _rt is not None:
            # team_id 컨벤션은 문자열('2'). period(INT)→str 캐스팅으로 캐시/team_coverage 키와 일치.
            _en.__dict__['team_id'] = str(_rt)
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
    # 인바운드는 source 그룹의 role이 파트장 등 비RN이어도 target에서 임상 배정되어야 함
    engine_nurses = [
        n
        for n in engine_nurses
        if str(getattr(n, 'role', 'RN') or 'RN').upper() in ('RN', 'AN')
        or bool(getattr(n, 'is_inbound', False))
    ]
    engine_nurse_ids = {str(n.nurse_id) for n in engine_nurses}
    preferences = [p for p in preferences if str(p.get("nurse_id")) in engine_nurse_ids]
    # nurse_index 일관성: cp_sat_basic.create_nurses_from_db 및 _build_engine_nurse_index_map과
    # 동일한 키로 정렬하여 fixed_cells.nurse_index ↔ nurses_for_engine[i] 포지셔널 매칭을 보장한다.
    engine_nurses.sort(key=lambda n: (
        int(getattr(n, "sequence", 0) or 0),
        -int(getattr(n, "experience", 0) or 0),
        str(getattr(n, "nurse_id", "")),
    ))

    # 월별 개인 shift/off 제한 오버레이 적용 (group/year/month scope)
    try:
        _limit_map = fetch_effective_monthly_limits_by_nurse(
            db=db,
            year=req.year,
            month=req.month,
            nurse_ids=[str(n.nurse_id) for n in engine_nurses],
            group_id=str(current_user.group_id),
        )
        # 전입자(병동이동/파견 inbound) 월한도 보충: 한도가 source 그룹에 저장돼 있어
        # 위 target group_id 필터에서 누락된다(flush된 병동이동도 NurseMonthlyLimit.group_id
        # 미이관이라 동일). source 그룹 기준으로 추가 조회해 target 에 없던 nurse 만 채운다.
        # (전월 tail 보충과 동일한 _inbound_source 패턴.)
        _limit_src_by_nurse = {
            str(_a.nurse_id): str(_a.source_group_id)
            for _a in _inbound_assignments
            if getattr(_a, "source_group_id", None)
        }
        _limit_missing_by_src = {}
        for _nid, _src_gid in _limit_src_by_nurse.items():
            if _nid not in _limit_map:
                _limit_missing_by_src.setdefault(_src_gid, []).append(_nid)
        for _src_gid, _src_nids in _limit_missing_by_src.items():
            _src_limit_map = fetch_effective_monthly_limits_by_nurse(
                db=db, year=req.year, month=req.month,
                nurse_ids=_src_nids, group_id=_src_gid,
            )
            for _nid, _lim in _src_limit_map.items():
                _limit_map.setdefault(_nid, _lim)
        for _n in engine_nurses:
            _lim = _limit_map.get(str(_n.nurse_id))
            if not _lim:
                continue
            for _k, _v in _lim.items():
                _n.__dict__[_k] = _v
    except Exception as _e:
        print(f"[MonthlyLimits] 오버레이 실패(무시): {_e}")

    nurses_for_engine = engine_nurses
    latest_config = _fetch_latest_config(db, req, current_user)
    shift_manage_data, daily_shift_requirements, daily_shift_requirements_by_day, daily_shift_requirements_max_by_day = _build_shift_manage_and_requirements(
        db, current_user, latest_config, req
    )
    # daily_shift_requirements를 config에 주입해서 엔진 호출
    config_dict = latest_config.__dict__ if latest_config else {}
    config_dict['daily_shift_requirements'] = daily_shift_requirements
    # 일자별 요구치 우선 적용
    config_dict['daily_shift_requirements_by_day'] = daily_shift_requirements_by_day
    config_dict['daily_shift_requirements_max_by_day'] = daily_shift_requirements_max_by_day
    # 요청에서 not_one_night가 들어오면 우선 적용 (없으면 DB 설정 유지)
    if getattr(req, "not_one_night", None) is not None:
        config_dict["not_one_night"] = bool(req.not_one_night)
    # 인바운드 간호사의 source group 매핑 → cross-month tail 보충용
    if _inbound_assignments:
        config_dict["_inbound_source_map"] = {
            str(a.nurse_id): str(a.source_group_id)
            for a in _inbound_assignments
        }
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
        _has_2n2o = bool(config_dict.get("two_offs_after_two_nig")) or bool(config_dict.get("two_offs_after_three_nig"))
        config_dict["max_extra_off_days"] = 6 if _has_2n2o else 1
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
    # ── nurse_assignment 기반 blocked_by_nurse 구성 (솔버에 전달) ──
    if _assignments:
        # nurse_id(문자열) → blocked days로 구성. 솔버 내부에서 자체 인덱스로 변환함.
        _blocked_by_id: dict[str, set[int]] = {}
        for n in nurses_for_engine:
            nid = str(n.nurse_id)
            _is_inbound = bool(getattr(n, 'is_inbound', False))
            days = build_blocked_days(
                _assignments, nid, current_user.group_id, month_start, days_in_month,
                is_inbound=_is_inbound,
            )
            if days:
                _blocked_by_id[nid] = days
                _label = "[Inbound]" if _is_inbound else ""
                print(f"[Assignment]{_label} nurse_id={nid}, blocked_days={sorted(days)}")
        if _blocked_by_id:
            config_dict["blocked_by_nurse_id"] = _blocked_by_id
            print(f"[Assignment] blocked_by_nurse_id 적용: {len(_blocked_by_id)}명")
        # ── mid-month 경계 window 제약 (인바운드 + 아웃바운드 복귀) ──
        _outbound_assignments = [
            a for a in _assignments
            if a.reason in ("파견", "병동이동")
            and a.source_group_id == current_user.group_id
            and a.target_group_id is not None
        ]
        if _inbound_assignments or _outbound_assignments:
            try:
                _code2main = _build_code_to_main_map(shift_manage_data)
                mid_constraints = build_mid_month_boundary_constraints(
                    db, _inbound_assignments, current_user.group_id,
                    req.year, req.month, config_dict, _code2main,
                    outbound_assignments=_outbound_assignments,
                )
                _ic = config_dict.get("initial_constraints") or {}
                for nid, days in mid_constraints.get('forced_off', {}).items():
                    _ic.setdefault('forced_off', {}).setdefault(nid, []).extend(days)
                    _ic['forced_off'][nid] = sorted(set(_ic['forced_off'][nid]))
                for nid, day_shifts in mid_constraints.get('forbidden', {}).items():
                    for d, shifts in day_shifts.items():
                        _ic.setdefault('forbidden', {}).setdefault(nid, {}).setdefault(d, []).extend(shifts)
                        _ic['forbidden'][nid][d] = sorted(set(_ic['forbidden'][nid][d]))
                config_dict["initial_constraints"] = _ic
                mid_ow = mid_constraints.get('off_window_constraints', {})
                if mid_ow:
                    ow = config_dict.get("off_window_constraints") or {}
                    for nid, windows in mid_ow.items():
                        ow.setdefault(nid, []).extend(windows)
                    config_dict["off_window_constraints"] = ow
            except Exception as e:
                print(f"[MidMonth] mid-month 경계 제약 생성 실패(무시): {e}")
        # ── 상대 그룹 OFF 카운트 → off cap 조정용 ──
        _other_group_offs: dict[str, int] = {}
        _off_codes = {'O', 'OFF', '주'}
        for a in (_inbound_assignments + _outbound_assignments):
            nid = str(a.nurse_id)
            other_gid = a.source_group_id if a.target_group_id == current_user.group_id else a.target_group_id
            if not other_gid:
                continue
            other_sid = _query_schedule_id_for_month(db, other_gid, req.year, req.month)
            if not other_sid:
                continue
            try:
                other_entries = (
                    db.query(ScheduleEntry)
                    .filter(ScheduleEntry.schedule_id == other_sid, ScheduleEntry.nurse_id == nid)
                    .all()
                )
                off_cnt = sum(
                    1 for e in other_entries
                    if str(e.shift_id).upper() in _off_codes
                    or str(getattr(e, 'default_shift', '') or '').upper() in _off_codes
                )
                if off_cnt > 0:
                    _other_group_offs[nid] = off_cnt
                    print(f"[OffCap][CrossGroup] nurse={nid}: other_group({other_gid}) OFF={off_cnt}건")
            except Exception as e:
                print(f"[OffCap][CrossGroup] nurse={nid} OFF 카운트 실패(무시): {e}")
        if _other_group_offs:
            config_dict["other_group_offs"] = _other_group_offs
        # ── 커버리지 제외: 파견/병동이동 기간에는 source 커버리지에서 제외 ──
        _cov_exclude: dict[str, set[int]] = {}
        for a in _assignments:
            if a.reason not in ("파견", "병동이동"):
                continue
            if a.source_group_id != current_user.group_id:
                continue
            if a.target_group_id is None:
                continue
            # source/target 독립 생성: assignment 기간은 항상 커버리지 제외
            # assignment 기간과 월의 교집합 → 커버리지 제외 day indices
            _month_end = month_start + timedelta(days=days_in_month - 1)
            _a_end_actual = a.end_date or a.expected_end_date or _month_end
            _s = max(a.start_date, month_start)
            _e = min(_a_end_actual, _month_end)
            nid = str(a.nurse_id)
            for d in range((_s - month_start).days, (_e - month_start).days + 1):
                _cov_exclude.setdefault(nid, set()).add(d)
        if _cov_exclude:
            config_dict["coverage_exclude_nurse_days"] = _cov_exclude
            for nid, days in _cov_exclude.items():
                print(f"[Assignment][CovExclude] nurse_id={nid}, days={sorted(days)}")
        # ── 프리셉티 기간: assignment 기간 내에만 프리셉터 follow 적용 ──
        # 프리셉티 assignment가 존재하는 간호사는 해당 월과 겹치는 기간만 follow,
        # 겹치지 않으면 빈 set (독립 배정). assignment 미조회 월에도 적용되도록
        # DB에서 해당 간호사의 active 프리셉티 assignment를 직접 조회.
        _preceptee_period: dict[str, set[int]] = {}
        # 1) 현재 _assignments에서 프리셉티 추출
        for a in _assignments:
            if a.reason != "프리셉티" or a.status == "cancelled":
                continue
            nid = str(a.nurse_id)
            _a_start = a.start_date
            _a_end = a.end_date or a.expected_end_date or (month_start + timedelta(days=days_in_month - 1))
            _month_end = month_start + timedelta(days=days_in_month - 1)
            if _a_end < month_start or _a_start > _month_end:
                _preceptee_period.setdefault(nid, set())
                continue
            _s = max(_a_start, month_start)
            _e = min(_a_end, _month_end)
            for d in range((_s - month_start).days, (_e - month_start).days + 1):
                _preceptee_period.setdefault(nid, set()).add(d)
        # 2) preceptor_id가 있지만 _assignments에 프리셉티 레코드가 없는 간호사도 체크
        #    (assignment 기간이 다른 월이라 조회 안 된 경우)
        from db.models import NurseAssignment as _NA
        for n in nurses_for_engine:
            nid = str(n.nurse_id)
            if nid in _preceptee_period:
                continue  # 이미 처리됨
            if not getattr(n, 'preceptor_id', None):
                continue
            # DB에서 이 간호사의 active 프리셉티 assignment가 존재하는지 확인
            _has_pte = db.query(_NA.id).filter(
                _NA.nurse_id == nid,
                _NA.reason == "프리셉티",
                _NA.status == "active",
            ).first()
            if _has_pte:
                _preceptee_period[nid] = set()  # 해당 월 겹침 없음 → 빈 set (독립 배정)
        if _preceptee_period:
            config_dict["preceptee_period_by_nurse_id"] = _preceptee_period
            for nid, days in _preceptee_period.items():
                print(f"[Assignment][Preceptee] nurse_id={nid}, follow_days={sorted(days)}")
    # ── 프리셉티 기간 체크 2단계: _assignments가 비어도 DB에서 직접 확인 ──
    if "preceptee_period_by_nurse_id" not in config_dict:
        from db.models import NurseAssignment as _NA2
        _preceptee_period_2: dict[str, set[int]] = {}
        for n in nurses_for_engine:
            nid = str(n.nurse_id)
            if not getattr(n, 'preceptor_id', None):
                continue
            _has_pte = db.query(_NA2.id).filter(
                _NA2.nurse_id == nid,
                _NA2.reason == "프리셉티",
                _NA2.status == "active",
            ).first()
            if _has_pte:
                _preceptee_period_2[nid] = set()
        if _preceptee_period_2:
            config_dict["preceptee_period_by_nurse_id"] = _preceptee_period_2
            for nid, days in _preceptee_period_2.items():
                print(f"[Assignment][Preceptee] nurse_id={nid}, follow_days={sorted(days)} (DB직접조회)")
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
    # 휴직/퇴사: 위 main path 와 동일한 정책으로 active_range_map 도 클리핑.
    _leave_assignments_alt = [
        a for a in _assignments
        if a.reason in ("휴직", "퇴사")
        and a.source_group_id == current_user.group_id
    ]
    _leave_clipped_alt = _clip_active_range_for_leaves(
        active_range_map, _leave_assignments_alt, month_start, days_in_month
    )
    if _leave_clipped_alt:
        print(f"[Assignment][Leave/AltPath] 휴직/퇴사 active_range_map 클리핑: {_leave_clipped_alt}")

    # print('active_range_map', active_range_map)
    weekly_off_map, weekly_off_warnings = _compute_weekly_off_day_indices_for_month(
        db=db,
        office_id=current_user.office_id,
        group_id=current_user.group_id,
        year=req.year,
        month=req.month,
        # 전입자(inbound)는 nurses 행이 source 그룹이라 group_id 필터 쿼리에서 빠지므로,
        # target_weekly_off_* 가 overlay 된 엔진 객체를 넘겨 주휴 셀을 보충한다.
        inbound_nurses=[n for n in nurses_for_engine if getattr(n, "is_inbound", False)],
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

    # source-side outbound 기간(파견/병동이동으로 다른 그룹에 가 있는 일자)은
    # 해당 병동 실 근무가 아니므로 weekly_off 셀도 미표시한다.
    _outbound_day_idx_map: dict[str, set[int]] = {}
    _month_end_d = month_start + timedelta(days=days_in_month - 1)
    for _a in _assignments:
        if _a.reason not in ("파견", "병동이동"):
            continue
        if _a.source_group_id != current_user.group_id:
            continue
        if _a.target_group_id == current_user.group_id:
            continue
        _nid = str(_a.nurse_id)
        _a_end = _a.end_date or _a.expected_end_date or _month_end_d
        _s = max(_a.start_date, month_start)
        _e = min(_a_end, _month_end_d)
        if _s > _e:
            continue
        _s_idx = (_s - month_start).days
        _e_idx = (_e - month_start).days
        _outbound_day_idx_map.setdefault(_nid, set()).update(range(_s_idx, _e_idx + 1))

    if weekly_off_map:
        filtered_map: dict[str, set[int]] = {}
        for nurse_id, day_set in weekly_off_map.items():
            rng = active_range_map.get(str(nurse_id))
            if not rng:
                continue
            start_idx, end_idx = rng
            _ob_days = _outbound_day_idx_map.get(str(nurse_id), set())
            clipped = {
                d for d in day_set
                if start_idx <= d <= end_idx and d not in _ob_days
            }
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
                weekly_off_fixed_cells.append(
                    {
                        "nurse_index": n_idx,
                        "day_index": d,
                        "shift": "O",
                        "shift_type": "주휴",
                        "fixed_source": "weekly_off",
                    }
                )
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
        # shifts_table_id → shift_id 매핑 (정확한 코드 복원용)
        _fw_table_id_to_shift_id: dict[int, str] = {
            s.id: s.shift_id
            for s in db.query(Shift.id, Shift.shift_id).filter(
                Shift.group_id == current_user.group_id
            ).all()
        }
        fw_nurse_idx_map = _build_engine_nurse_index_map(nurses_for_engine)
        fw_fixed_cells = []
        _fw_skip_special = 0
        _fw_skip_nurse = 0
        _fw_skip_range = 0
        _fw_code_counts: dict[str, int] = {}
        for fe in all_fixed_entries:
            # shifts_table_id가 있으면 정확한 shift_id로 복원, 없으면 기존 값 사용
            if fe.shifts_table_id and fe.shifts_table_id in _fw_table_id_to_shift_id:
                shift_code_raw = _fw_table_id_to_shift_id[fe.shifts_table_id]
            else:
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
                "fixed_source": "fixed_wanted",
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
        _preceptee_on_val = config_dict.get("preceptee_on", False)
        print(
            f"[RosterCreate] 프리셉티 fixed_wanted 맵 구성 조건: preceptee_on={_preceptee_on_val}, all_fixed_entries={len(all_fixed_entries)}건"
        )
        if _preceptee_on_val:
            _pte_fw_map: dict[tuple[int, int], str] = {}
            _pte_ids = set()
            _pte_names: dict[str, str] = {}
            for nurse in nurses_for_engine:
                if getattr(nurse, "preceptor_id", None):
                    _pte_ids.add(str(nurse.nurse_id))
                    _pte_names[str(nurse.nurse_id)] = getattr(nurse, "name", "?")
            print(
                f"[RosterCreate] 프리셉티 목록: {len(_pte_ids)}명 → {[(nid, _pte_names.get(nid, '?')) for nid in sorted(_pte_ids)]}"
            )
            _idx_to_nid = {idx: nid for nid, idx in fw_nurse_idx_map.items()}
            for fe in all_fixed_entries:
                _nid = str(fe.nurse_id)
                if _nid not in _pte_ids:
                    continue
                _n_idx = fw_nurse_idx_map.get(_nid)
                if _n_idx is None:
                    continue
                _d_idx = fe.shift_date.day - 1
                if _d_idx < 0 or _d_idx >= days_in_month:
                    continue
                if active_range_candidates:
                    _rng = active_range_candidates.get(_nid)
                    if _rng:
                        _start_idx, _end_idx = _rng
                        if _d_idx < _start_idx or _d_idx > _end_idx:
                            continue
                _pte_fw_map[(_n_idx, _d_idx)] = str(fe.shift_id or "").strip()
            if _pte_fw_map:
                config_dict["preceptee_fixed_wanted_map"] = _pte_fw_map
                print(
                    f"[RosterCreate] 프리셉티 fixed_wanted 맵: {len(_pte_fw_map)}건 (후처리에서 보호)"
                )
                for (ni, di), sc in sorted(_pte_fw_map.items()):
                    _nm = _pte_names.get(_idx_to_nid.get(ni, ""), "?")
                    print(f"  → 프리셉티 {_nm}(idx={ni}) {di + 1}일차={sc}")
            else:
                print(
                    f"[RosterCreate] 프리셉티 fixed_wanted 맵: 0건 (프리셉티 FixedWantedEntry 매칭 없음)"
                )
                print("  [DEBUG] 프리셉티 원본 FixedWantedEntry가 없거나 활동범위/엔진 매핑에서 제외됨")
        else:
            print(
                f"[RosterCreate] preceptee_on=False → 프리셉티 fixed_wanted 맵 구성 스킵"
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

    # ── 2N→2OFF: N블록 종료 후 recovery OFF를 fixed_cells로 사전 주입 ──
    if bool(config_dict.get("two_offs_after_two_nig")):
        _n_cells_by_nurse: dict[int, set[int]] = {}
        _o_cells_by_nurse: dict[int, set[int]] = {}
        for c in combined_fixed_cells:
            _ni = c.get("nurse_index")
            _di = c.get("day_index")
            _sh = str(c.get("shift") or "").upper()
            if _ni is None or _di is None:
                continue
            if _sh == "N":
                _n_cells_by_nurse.setdefault(_ni, set()).add(_di)
            elif _sh in ("O", "OFF", "주"):
                _o_cells_by_nurse.setdefault(_ni, set()).add(_di)
        _existing_fixed = {(c.get("nurse_index"), c.get("day_index")) for c in combined_fixed_cells}
        _recovery_added = 0
        for _ni, _n_days in _n_cells_by_nurse.items():
            _sorted = sorted(_n_days)
            # N블록 종료 감지: 다음 날이 N이 아닌 연속 N의 마지막
            for i, d in enumerate(_sorted):
                if d + 1 not in _n_days and i > 0 and _sorted[i - 1] == d - 1:
                    # d가 2+ 연속 N의 마지막 → recovery d+1, d+2
                    for r in (d + 1, d + 2):
                        if r >= days_in_month:
                            continue
                        if (_ni, r) in _existing_fixed:
                            continue
                        combined_fixed_cells.append({
                            "nurse_index": _ni, "day_index": r,
                            "shift": "O", "fixed_source": "2n2off_recovery",
                        })
                        off_exception_cells.add((_ni, r))
                        _existing_fixed.add((_ni, r))
                        _recovery_added += 1
        if _recovery_added > 0:
            config_dict["off_exception_cells"] = sorted(off_exception_cells)
            config_dict["_2n2off_pre_injected"] = True
            print(f"[2N2OFF-PreInject] recovery OFF {_recovery_added}건 fixed_cells에 추가")

    # ── 일괄 OFF 간호사 엔진 자동 제외 ──
    _alloff_roster: dict[str, list[str]] = {}
    nurses_for_engine, combined_fixed_cells, off_exception_cells, _alloff_roster = (
        _exclude_alloff_nurses(
            nurses_for_engine, combined_fixed_cells, off_exception_cells,
            config_dict, days_in_month,
        )
    )

    # ── Precheck: 솔버 호출 전 산술적 infeasibility 검사 ──
    precheck_result: dict | None = None
    try:
        from services.precheck import (
            run_runtime_precheck,
            has_blocking_issues,
            build_blocking_payload,
        )
        _engine_grade_config = _fetch_grade_config_dict(db, current_user.office_id, current_user.group_id)
        # `n.__dict__` 은 SQLAlchemy 의 이미 로딩된 attr 만 담아서 team_id /
        # allowed_shifts 가 lazy-load 상태면 빠진다. 명시적으로 attribute 접근해
        # 풀에서 사용할 키를 모두 일관되게 채운다.
        _nurses_dict_for_precheck = [
            {
                "nurse_id": getattr(n, "nurse_id", None),
                "db_id": getattr(n, "nurse_id", None),
                "team_id": getattr(n, "team_id", None),
                "grade": getattr(n, "grade", None),
                "allowed_shifts": getattr(n, "allowed_shifts", None),
                "work_shifts": getattr(n, "work_shifts", None),
                "joining_date": getattr(n, "joining_date", None),
                "resignation_date": getattr(n, "resignation_date", None),
                "personal_off_adjustment": getattr(n, "personal_off_adjustment", 0),
                "is_weekend_off": getattr(n, "is_weekend_off", False),
                "weekly_off_weekday": getattr(n, "weekly_off_weekday", None),
            }
            for n in (nurses_for_engine or [])
        ]
        # team_min_by_team은 _run_cp_sat_basic 내부에서 주입되므로 precheck 시점엔 누락된다.
        # precheck용으로 미리 한 번 더 로드해서 config_dict에 임시 주입한다.
        precheck_config = dict(config_dict)
        if "team_min_by_team" not in precheck_config:
            try:
                _team_rows = (
                    db.query(Team)
                    .filter(
                        Team.office_id == current_user.office_id,
                        Team.group_id == current_user.group_id,
                        Team.active == 1,
                    )
                    .all()
                )
                _team_min_by_team: dict[str, dict[str, int]] = {}
                # teams.min_shift 미사용 — 활성 팀 중 '멤버가 배정된 팀'에만 디폴트 최소
                # (D:1, E:1, N:0[, use_mid면 M:0]). 인원 0 팀은 솔버가 무시하므로 team_min 에서도
                # 제외(안 그러면 TEAM_SIZE_INSUFFICIENT 로 오블로킹). 멤버십은 솔버와 동일 기준.
                _use_mid = bool(precheck_config.get("use_mid", False))
                _default_tm: dict[str, int] = {"D": 1, "E": 1, "N": 0}
                if _use_mid:
                    _default_tm["M"] = 0
                _member_team_ids = {
                    str(_n.get("team_id"))
                    for _n in _nurses_dict_for_precheck
                    if _n.get("team_id") not in (None, "", 0)
                }
                for _t in _team_rows:
                    if str(_t.team_id) not in _member_team_ids:
                        continue
                    _cleaned = {k: v for k, v in _default_tm.items() if v > 0}
                    if _cleaned:
                        _team_min_by_team[str(_t.team_id)] = _cleaned
                if _team_min_by_team:
                    precheck_config["team_min_by_team"] = _team_min_by_team
            except Exception as _team_exc:
                print(f"[Precheck] team_min 로딩 실패(무시): {_team_exc}")

        precheck_result = run_runtime_precheck(
            nurses_dict=_nurses_dict_for_precheck,
            config_dict=precheck_config,
            grade_config=_engine_grade_config,
            fixed_cells=combined_fixed_cells,
            year=req.year,
            month=req.month,
            stop_on_config_error=False,
        )
        if has_blocking_issues(precheck_result):
            payload = build_blocking_payload(precheck_result)
            inf = payload.get("infeasibility", {})
            issue_codes = sorted({
                str(i.get("reason_code", "?"))
                for i in (precheck_result.get("issues") or [])
            })
            print(
                f"[Precheck][BLOCKING] {len(precheck_result.get('issues', []))}건 — "
                f"codes={issue_codes}"
            )
            print(f"[Precheck][BLOCKING][message] {inf.get('summary_message_ko')}")
            for s in (inf.get("fix_suggestions_ko") or [])[:5]:
                print(f"[Precheck][BLOCKING][fix] {s}")
            print("[Precheck][BLOCKING] 솔버 호출 생략. HTTP 500 응답.")
            try:
                db.delete(schedule)
                db.commit()
            except Exception:
                db.rollback()
            from fastapi import HTTPException
            raise HTTPException(status_code=500, detail=payload)
    except HTTPException:
        raise
    except Exception as _pre_exc:
        # precheck 자체가 실패하면 무시하고 솔버 진행
        print(f"[Precheck] 실행 실패(무시하고 솔버 진행): {_pre_exc}")
        precheck_result = None

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
        # ── ontology treatment 적용: 선택된 treatment_ids 를 config 에 패치(재생성용) ──
        # apply_treatments 가 force_soft_mode/disable_module/set_threshold 를 patched_config 로
        # 반영(대부분 런타임 플래그). data_correction_required(manual)는 적용 안 됨.
        if treatment_ids:
            try:
                from services.treatment_applicator import apply_treatments
                _clean_cfg = {k: v for k, v in config_dict.items() if not str(k).startswith("_sa_")}
                _tres = apply_treatments(list(treatment_ids), _clean_cfg)
                config_dict = _tres.patched_config
                print(f"[TreatmentApply] applied={_tres.applied_treatment_ids} "
                      f"manual={_tres.manual_required} unresolved={_tres.unresolved_treatment_ids}")
            except Exception as _tapp_exc:
                print(f"[TreatmentApply] 실패(무시): {_tapp_exc}")
        generated, satisfaction_data, roster_system = _run_cp_sat_basic(
            db,
            current_user,
            nurses_for_engine,
            preferences,
            latest_config,
            req,
            shift_manage_data,
            fixed_cells=combined_fixed_cells if combined_fixed_cells else None,
            time_limit_seconds=180 if bool(getattr(req, "advanced_inference", False)) else 60,
            config_override=config_dict,
            _assignments=_assignments,
            _inbound_assignments=_inbound_assignments,
            _outbound_assignments=_outbound_assignments,
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
        weekend_off_display_ids = {
            str(getattr(n, "nurse_id", ""))
            for n in nurses_in_group
            if bool(getattr(n, "is_weekend_off", False)) and getattr(n, "nurse_id", None)
        }
        if weekly_off_map and isinstance(generated, dict):
            for nurse_id, day_set in weekly_off_map.items():
                if str(nurse_id) in weekend_off_display_ids:
                    continue
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
    # 일괄 OFF 간호사 스케줄 병합
    if _alloff_roster:
        if isinstance(generated, dict):
            generated.update(_alloff_roster)
        else:
            generated = dict(_alloff_roster)

    validation_error = _validate_generated_roster(
        generated,
        roster_system,
        nurses_context=list(nurses_for_engine or []),
        config_context=config_dict,
        grade_config_context=_fetch_grade_config_dict(db, current_user.office_id, current_user.group_id),
    )
    applied_relaxations: list[str] = []
    if validation_error:
        # 자연 soft 트리거: 엔진이 실근무를 못 배정한 infeasible-empty 신호(실제 플래그)
        # 를 본다. 과거처럼 휴리스틱이 만든 NO_ASSIGNMENT/grade_max 문자열을 매칭하지 않음.
        _trigger_soft = bool(getattr(roster_system, "_infeasible_empty", False))
        if _trigger_soft and not bool(config_dict.get("_team_min_soft_retry_attempted")):
            print("[TeamMinFallback] infeasible 감지 → team_min hard→soft 자동 전환으로 1회 재시도 (grade hard 유지)")
            soft_cfg = dict(config_dict)
            soft_cfg["team_min_soft_fallback"] = True
            soft_cfg["_team_min_soft_retry_attempted"] = True
            retry_generated, _, retry_rs = _run_cp_sat_basic(
                db,
                current_user,
                nurses_for_engine,
                preferences,
                latest_config,
                req,
                shift_manage_data,
                fixed_cells=combined_fixed_cells if combined_fixed_cells else None,
                time_limit_seconds=180 if bool(getattr(req, "advanced_inference", False)) else 60,
                config_override=soft_cfg,
                _assignments=_assignments,
                _inbound_assignments=_inbound_assignments,
                _outbound_assignments=_outbound_assignments,
            )
            # 동일 후처리 적용
            try:
                weekend_off_display_ids = {
                    str(getattr(n, "nurse_id", ""))
                    for n in nurses_in_group
                    if bool(getattr(n, "is_weekend_off", False)) and getattr(n, "nurse_id", None)
                }
                if weekly_off_map and isinstance(retry_generated, dict):
                    for nurse_id, day_set in weekly_off_map.items():
                        if str(nurse_id) in weekend_off_display_ids:
                            continue
                        shifts = retry_generated.get(nurse_id)
                        if not shifts:
                            continue
                        for d in day_set:
                            if 0 <= d < len(shifts):
                                shifts[d] = "주"
            except Exception as e:
                weekly_off_warnings.append({"type": "weekly_off_mark_failed_retry", "detail": str(e)})

            if isinstance(retry_generated, dict):
                retry_generated.update(fixed_roster)
            else:
                retry_generated = fixed_roster
            if _alloff_roster:
                if isinstance(retry_generated, dict):
                    retry_generated.update(_alloff_roster)
                else:
                    retry_generated = dict(_alloff_roster)

            retry_validation_error = _validate_generated_roster(
                retry_generated,
                retry_rs,
                nurses_context=list(nurses_for_engine or []),
                config_context=soft_cfg,
                grade_config_context=_fetch_grade_config_dict(db, current_user.office_id, current_user.group_id),
            )
            if not retry_validation_error:
                generated = retry_generated
                roster_system = retry_rs
                # 정책: AUTO-SOFT는 team_min만 풀고 grade는 끝까지 hard 유지 (2026-05-23).
                # agent-qa-harness의 grade flip 패턴은 의도적으로 폐기됨.
                applied_relaxations.append("team_min_hard_to_soft")
                # Ontology treatment 어휘로도 노출 (agent-qa-harness의 dispatch 카탈로그 호환).
                applied_relaxations.append("treatment:soft:team_min")
                weekly_off_warnings.append(
                    {
                        "type": "team_min_hard_to_soft_applied",
                        "detail": (
                            "Team_min hard 제약이 infeasible로 인해 자동 soft 전환되어 재생성됐습니다. "
                            "일부 팀의 일일 D/E 최소가 미충족일 수 있습니다. (grade hard는 유지)"
                        ),
                    }
                )
                print(
                    "[TeamMinFallback][AUTO-SOFT][success] team_min hard→soft 자동 전환으로 근무표 생성. "
                    "사용자 응답: HTTP 200, severity=warning, "
                    "applied_relaxations=['team_min_hard_to_soft', 'treatment:soft:team_min']"
                )
                validation_error = None
            else:
                print(f"[TeamMinFallback][AUTO-SOFT][fail] 재시도 실패: {retry_validation_error}")
                validation_error = retry_validation_error

    if validation_error:
        print(f"[RosterGenerate][UNRECOVERABLE] {validation_error}")
        print(f"[RosterGenerate][UNRECOVERABLE] applied_relaxations={applied_relaxations}")
        try:
            db.delete(schedule)
            db.commit()
        except Exception:
            db.rollback()
        try:
            from services.precheck import build_unrecoverable_payload
            from fastapi import HTTPException
            try:
                _violated = _extract_unrecoverable_violated_constraints(
                    roster_system, generated, validation_error
                )
            except Exception:
                _violated = []
            try:
                from services.precheck.conflict_detector import run_conflict_detectors
                _conflict_cores = run_conflict_detectors(roster_system)
            except Exception as _cc_exc:
                print(f"[ConflictDetector] failed (ignore): {_cc_exc}")
                _conflict_cores = []
            # CP-SAT MUS 가 추출한 conflict cores 도 합쳐서 같은 리스트로 노출.
            # fallback의 multi-stage retry로 같은 core_id가 여러 번 emit될 수 있어
            # core_id 단위 dedup (affected_count 가장 큰 entry keep).
            _cpsat_cores_raw = list(getattr(roster_system, "_cpsat_conflict_cores", []) or [])
            _cpsat_by_id: dict = {}
            for _c in _cpsat_cores_raw:
                _cid = _c.get("core_id")
                if not _cid:
                    continue
                _existing = _cpsat_by_id.get(_cid)
                if _existing is None or (_c.get("affected_count") or 0) > (_existing.get("affected_count") or 0):
                    _cpsat_by_id[_cid] = _c
            _cpsat_cores = list(_cpsat_by_id.values())
            if _cpsat_cores:
                print(f"[ConflictCore] CP-SAT MUS: {len(_cpsat_cores_raw)}건 → dedup {len(_cpsat_cores)}건, detector: {len(_conflict_cores)}건 합산")
                _conflict_cores = _conflict_cores + _cpsat_cores
            # Pool 그래프 스냅샷 — TeamPool / GradePool / CommonPool capacity vs demand
            # 분석을 통한 root cause 표면화. shortage 가 발견되면 conflict_cores 에 합류.
            _pool_snapshot_dict: dict[str, Any] = {}
            try:
                from services.ontology_pool import build_pool_snapshot_from_runtime
                _pool_snap = build_pool_snapshot_from_runtime(
                    nurses_dict=_nurses_dict_for_precheck,
                    config_dict=precheck_config,
                    grade_config=_engine_grade_config,
                    fixed_cells=combined_fixed_cells,
                    year=req.year,
                    month=req.month,
                )
                _pool_snapshot_dict = _pool_snap.to_dict()
                _shortage_cores = list(_pool_snapshot_dict.get("shortages") or [])
                if _shortage_cores:
                    print(
                        f"[PoolGraph] shortages 발견: {len(_shortage_cores)}건 — "
                        f"pools={len(_pool_snapshot_dict.get('pools') or [])}, "
                        f"edges={len(_pool_snapshot_dict.get('nurse_pool_edges') or [])}"
                    )
                    _conflict_cores = _shortage_cores + _conflict_cores
            except Exception as _pool_exc:
                print(f"[PoolGraph] build 실패(무시): {_pool_exc}")
            # nurse_index_map: enricher 가 node_id (예: off_cap:nurse_5) 의 idx 를
            # 실제 이름+사번 으로 치환할 수 있도록 engine 순서로 매핑 빌드.
            _nurse_index_map: dict[str, dict] = {}
            try:
                for _i, _n in enumerate(nurses_for_engine or []):
                    _nurse_index_map[str(_i)] = {
                        "nurse_id": str(getattr(_n, "nurse_id", "") or ""),
                        "name": getattr(_n, "name", "") or "",
                        "team_id": getattr(_n, "team_id", None),
                        "grade": getattr(_n, "grade", None),
                    }
            except Exception as _nim_exc:
                print(f"[UNRECOVERABLE] nurse_index_map build 실패(무시): {_nim_exc}")
                _nurse_index_map = {}
            unrecoverable = build_unrecoverable_payload(
                precheck_result=precheck_result,
                applied_relaxations=applied_relaxations,
                last_error_reason=str(validation_error),
                violated_constraints=_violated,
                conflict_cores=_conflict_cores,
                pool_snapshot=_pool_snapshot_dict,
                nurse_index_map=_nurse_index_map,
            )
            # ── UNDIAGNOSED 블랙박스 probe ──
            # 분석(MUS/산술/max-flow)이 원인을 못 짚은 경우, 결합제약을 하나씩 풀어
            # 엔진을 재실행해 "무엇을 풀면 feasible 해지는가"를 실측한다(verified resolution).
            # UNDIAGNOSED(원인 미식별) 상황이면 자동 실행. kill-switch(UNDIAG_PROBE_DISABLE=1)
            # 로만 끈다. 실패해도 기존 payload 그대로(graceful). 이미 실패한 케이스에만 타므로
            # 정상 생성에는 영향 없음. (※ 동기 경로는 probe 시간만큼 응답 지연 — async 권장)
            try:
                import os as _os_undiag
                _inf_pre = unrecoverable.get("infeasibility", {}) or {}
                _sd_pre = _inf_pre.get("structural_diagnosis", {}) or {}
                _codes_pre = (_sd_pre.get("signals") or {}).get("reason_codes") or []
                _is_undiag = ("UNDIAGNOSED" in _codes_pre) or not (_sd_pre.get("primary_causes") or [])
                if _is_undiag and _os_undiag.getenv("UNDIAG_PROBE_DISABLE") != "1":
                    from services.cp_sat.undiagnosed_probe import probe_relaxations, to_resolution_options

                    def _undiag_resolve(_relaxed_cfg):
                        _g, _, _rs = _run_cp_sat_basic(
                            db, current_user, nurses_for_engine, preferences, latest_config, req,
                            shift_manage_data,
                            fixed_cells=combined_fixed_cells if combined_fixed_cells else None,
                            time_limit_seconds=60,
                            config_override=_relaxed_cfg,
                            _assignments=_assignments,
                            _inbound_assignments=_inbound_assignments,
                            _outbound_assignments=_outbound_assignments,
                        )
                        _err = _validate_generated_roster(
                            _g, _rs,
                            nurses_context=list(nurses_for_engine or []),
                            config_context=_relaxed_cfg,
                            grade_config_context=_fetch_grade_config_dict(
                                db, current_user.office_id, current_user.group_id),
                        )
                        return (_err is None), {"validation_error": (str(_err)[:80] if _err else None)}

                    # probe base: solve 시점에 박아둔 유효 config 스냅샷(하드규칙+조립분 포함, 충실).
                    # 실패 시점 메모리(config_dict)는 ORM 만료·stale 라 부정확하므로 스냅샷 우선.
                    _probe_base = getattr(roster_system, "_effective_config_snapshot", None)
                    if not _probe_base:
                        _probe_base = {k: v for k, v in dict(config_dict).items()
                                       if not str(k).startswith("_sa_")}
                    _probe_base = dict(_probe_base)
                    _probe_res = probe_relaxations(_probe_base, _undiag_resolve)
                    unrecoverable["infeasibility"]["probe_resolutions"] = _probe_res.get("resolutions", [])
                    unrecoverable["infeasibility"]["probe_combo"] = _probe_res.get("combo")
                    unrecoverable["infeasibility"]["probe_found"] = _probe_res.get("found", False)
                    # 프론트용 통합 옵션 카드: probe(검증됨)를 앞에, 기존 ontology 옵션
                    # (build_unrecoverable_payload 가 treatment 로 채운 것) 뒤에 prepend.
                    _probe_opts = to_resolution_options(_probe_res, _probe_base)
                    _exist_opts = unrecoverable["infeasibility"].get("resolution_options") or []
                    unrecoverable["infeasibility"]["resolution_options"] = _probe_opts + _exist_opts
                    # 정합성: probe 가 검증된(verified) 옵션을 찾았으면 "해를 못 찾음, 점검하세요"
                    # 메시지와 모순되므로, 적용 가능한 옵션이 있음을 알리는 문구로 교정.
                    if any(o.get("verified") for o in _probe_opts):
                        unrecoverable["infeasibility"]["summary_message_ko"] = (
                            "자동 진단으로는 원인을 특정하지 못했지만, 아래 옵션 중 하나를 "
                            "적용하면 근무표를 생성할 수 있습니다. 적용할 옵션을 선택해주세요."
                        )
                    _combo = _probe_res.get("combo")
                    print(f"[UndiagProbe] found={_probe_res.get('found')} "
                          f"resolutions={[r['id'] for r in _probe_res.get('resolutions', [])]} "
                          f"combo={_combo.get('id') if _combo else None}")
            except Exception as _undiag_exc:
                print(f"[UndiagProbe] failed (ignore): {_undiag_exc}")
            # (ontology treatment → resolution_options 는 build_unrecoverable_payload 에서 처리됨)
            inf = unrecoverable.get("infeasibility", {})
            print(
                f"[RosterGenerate][UNRECOVERABLE][response] HTTP 500, severity={inf.get('severity')}, "
                f"message={inf.get('summary_message_ko')}"
            )
            try:
                from services.live_graph_export import dump_live_graph_export

                dump_live_graph_export(
                    group_id=str(getattr(current_user, "group_id", "") or ""),
                    year=int(req.year),
                    month=int(req.month),
                    conflict_cores=_conflict_cores,
                    pool_snapshot=_pool_snapshot_dict,
                    violated_constraints=_violated,
                    applied_relaxations=applied_relaxations,
                    last_error_reason=str(validation_error),
                    unrecoverable_payload=unrecoverable,
                )
            except Exception as _lg_exc:
                print(f"[LiveGraphExport] hook 실패(무시): {_lg_exc}")
            raise HTTPException(status_code=500, detail=unrecoverable)
        except HTTPException:
            raise
        except Exception:
            raise Exception(validation_error)

    # 초과 OFF → 연차 변환 후처리 (off_swap_enabled=True 일 때만 동작, 보호 4종 적용)
    print(
        f"[OffSwap][CALL] schedule_id={schedule.schedule_id} "
        f"latest_config_id={getattr(latest_config, 'config_id', None)} "
        f"off_swap_enabled={getattr(latest_config, 'off_swap_enabled', None)!r} "
        f"off_days={getattr(latest_config, 'off_days', None)!r}"
    )
    try:
        generated = postprocess_off_swap(db, schedule, generated, latest_config, req)
    except Exception as _off_swap_exc:
        print(f"[OffSwap] 후처리 실패 — 변환 미적용 진행: {_off_swap_exc}")
    _persist_entries(db, schedule, generated, req)
    # NOTE: ShiftTransferLog 기반 전달 복사는 source/target 독립 생성 전환으로 비활성화 (2026-04-13)
    # ── 전달된 인바운드 간호사를 nurses_in_group에 추가 (표시용) ──
    _group_nids = {str(n.nurse_id) for n in nurses_in_group}
    _entry_nids = {
        row.nurse_id for row in
        db.query(ScheduleEntry.nurse_id)
        .filter(ScheduleEntry.schedule_id == schedule.schedule_id)
        .distinct()
        .all()
    }
    _transferred_nids = _entry_nids - _group_nids
    if _transferred_nids:
        _transferred_nurses = db.query(Nurse).filter(Nurse.nurse_id.in_(_transferred_nids)).all()
        for n in _transferred_nurses:
            setattr(n, 'is_inbound', True)
        nurses_in_group = list(nurses_in_group) + _transferred_nurses
        print(f"[Assignment] 전달 간호사 응답 추가: {[f'{n.name}({n.nurse_id})' for n in _transferred_nurses]}")
        # 인바운드 배정표 로그 (CloudWatch 분석용)
        for _tn in _transferred_nurses:
            _t_entries = (
                db.query(ScheduleEntry)
                .filter(
                    ScheduleEntry.schedule_id == schedule.schedule_id,
                    ScheduleEntry.nurse_id == _tn.nurse_id,
                )
                .order_by(ScheduleEntry.work_date)
                .all()
            )
            _t_map = {e.work_date.day if hasattr(e.work_date, 'day') else e.work_date: e.shift_id for e in _t_entries}
            _t_schedule = [_t_map.get(d, '-') for d in range(1, days_in_month + 1)]
            print(f"[CP-SAT-Basic] 배정표(인바운드) {_tn.name}({_tn.nurse_id}): {' '.join(_t_schedule)}")
    roster_data = _build_roster_response(db, schedule, req, nurses_in_group)
    roster_data["weekly_off_conflicts"] = weekly_off_conflicts
    roster_data["weekly_off_warnings"] = weekly_off_warnings
    roster_data["constraint_impact"] = _build_constraint_impact_payload(roster_system, req)
    # ── infeasibility 페이로드 (precheck warning + applied_relaxations + violation summary) ──
    try:
        from services.precheck import build_success_payload
        _ci = roster_data.get("constraint_impact") or {}
        roster_data.update(
            build_success_payload(
                precheck_result=precheck_result,
                applied_relaxations=applied_relaxations,
                violated_constraints=_ci.get("violated_constraints") or [],
                hard_violation_count=int(_ci.get("hard_violation_count") or 0),
            )
        )
        _inf = roster_data.get("infeasibility") or {}
        _vs = _inf.get("violation_summary") or {}
        _vs_summary = {k: v.get("count") for k, v in _vs.items()}
        print(
            f"[RosterGenerate][response] HTTP 200, severity={_inf.get('severity')}, "
            f"applied_relaxations={_inf.get('applied_relaxations')}, "
            f"violations={_vs_summary}"
        )
        if _inf.get("severity") == "warning" and _inf.get("summary_message_ko"):
            print(f"[RosterGenerate][response][message] {_inf['summary_message_ko']}")
    except Exception as _exc:
        print(f"[Infeasibility] payload 빌드 실패(무시): {_exc}")

    # ── A1: 표는 나왔으나 hard 위반(hv>0) → 위반 0 으로 만드는 완화 옵션 probe (opt-in) ──
    # UNDIAGNOSED probe 와 동일 메커니즘. 성공조건만 feasible → hard_viol==0.
    # 흔한 케이스라 req.suggest_fixes=True 일 때만 실행(매 생성 지연 방지). kill-switch 동일.
    try:
        import os as _os_a1
        _ci_a1 = roster_data.get("constraint_impact") or {}
        _hv_cnt = int(_ci_a1.get("hard_violation_count") or 0)
        _inf_a1 = roster_data.get("infeasibility")
        if (bool(getattr(req, "suggest_fixes", False)) and _hv_cnt > 0
                and isinstance(_inf_a1, dict)
                and _os_a1.getenv("UNDIAG_PROBE_DISABLE") != "1"):
            from services.cp_sat.undiagnosed_probe import probe_relaxations, to_resolution_options
            _a1_base = getattr(roster_system, "_effective_config_snapshot", None)
            if _a1_base:
                def _a1_resolve(_relaxed_cfg):
                    _g2, _, _rs2 = _run_cp_sat_basic(
                        db, current_user, nurses_for_engine, preferences, latest_config, req,
                        shift_manage_data,
                        fixed_cells=combined_fixed_cells if combined_fixed_cells else None,
                        time_limit_seconds=60, config_override=_relaxed_cfg,
                        _assignments=_assignments, _inbound_assignments=_inbound_assignments,
                        _outbound_assignments=_outbound_assignments,
                    )
                    try:
                        _ci2 = _build_constraint_impact_payload(_rs2, req)
                        _hv2 = int((_ci2 or {}).get("hard_violation_count") or 0)
                    except Exception:
                        _hv2 = 1
                    return (_hv2 == 0), {"hard_viol": _hv2}

                _a1_res = probe_relaxations(dict(_a1_base), _a1_resolve)
                _a1_opts = to_resolution_options(_a1_res, _a1_base)
                _inf_a1["resolution_options"] = _a1_opts
                _inf_a1["probe_found"] = _a1_res.get("found", False)
                # 정합성: 위반표(hv>0) warning 메시지에, 위반을 0 으로 만드는 검증된
                # 옵션이 있음을 덧붙여 안내(기존 위반 요약 메시지는 유지).
                if any(o.get("verified") for o in _a1_opts):
                    _base_msg = (_inf_a1.get("summary_message_ko") or "").rstrip()
                    _add_msg = ("아래 옵션 중 하나를 적용하면 위반 없이 다시 생성할 수 있습니다.")
                    _inf_a1["summary_message_ko"] = (
                        f"{_base_msg} {_add_msg}" if _base_msg else _add_msg
                    )
                print(f"[A1Probe] hv={_hv_cnt} → found={_a1_res.get('found')} "
                      f"options={[o['option_id'] for o in (_inf_a1.get('resolution_options') or [])]}")
    except Exception as _a1_exc:
        print(f"[A1Probe] failed (ignore): {_a1_exc}")

    # ── assignment 대상자 근무표 생성 알림 (S09) ──
    try:
        from utils.utils import send_assignment_roster_created_push
        from services.assignment_service import _get_group_name
        _assign_nurse_ids = set()
        for _a in _assignments:
            if _a.reason in ("파견", "병동이동") and _a.status != "cancelled":
                _assign_nurse_ids.add(str(_a.nurse_id))
        if _assign_nurse_ids:
            _gname = _get_group_name(db, current_user.group_id) or str(current_user.group_id)
            _nurse_names = [
                n.name for n in nurses_in_group if str(n.nurse_id) in _assign_nurse_ids
            ] or list(_assign_nurse_ids)
            send_assignment_roster_created_push(
                nurse_name=", ".join(_nurse_names),
                group_name=_gname,
                year=req.year,
                month=req.month,
                recipients=list(_assign_nurse_ids),
                office_code=current_user.office_id,
                sender_emp_seq_no=current_user.nurse_id,
                sender_member_id=current_user.account_id,
            )
    except Exception as e:
        print(f"[RosterCreate] assignment 생성 알림 실패: {e}")

    return roster_data


# 


def request_schedule_service(req: RosterRequest, current_user, db: Session):
    """
    스케줄 생성 서비스 함수
    """
    if not current_user or not caller_is_head_nurse(db, current_user):
        raise Exception("Permission denied")
    # 대상 그룹: 토큰 대신 req.group_id(없으면 DB home)로 해석·검증.
    current_user.group_id = resolve_effective_group(
        db, current_user, getattr(req, "group_id", None)
    )
    nurse = db.query(Nurse).filter(Nurse.nurse_id == current_user.nurse_id).first()
    if not nurse or not nurse.group:
        raise Exception("User group information not found")
    # config_id가 제공된 경우 해당 config 사용, 아니면 최신 config 사용.
    # cross-group 가드: config 는 반드시 생성 대상 그룹(current_user.group_id) 소유여야 한다.
    # req.config_id 가 타 그룹 것이거나 폴백 시에도 current_user.group_id 기준으로만 조회해,
    # 같은 office 의 다른 병동 config 가 schedule 에 오염 스탬핑되던 버그를 차단한다.
    latest_config = None
    if req.config_id:
        latest_config = db.query(RosterConfig).filter(
            RosterConfig.config_id == req.config_id
        ).first()
        if latest_config is not None and str(latest_config.group_id) != str(
            current_user.group_id
        ):
            logger.warning(
                "[request_schedule_service] cross-group config_id 무시: config_id=%s "
                "(config_group=%s) != current_group=%s → 그룹 최신 config 로 폴백",
                req.config_id,
                latest_config.group_id,
                current_user.group_id,
            )
            latest_config = None
    if latest_config is None:
        latest_config = db.query(RosterConfig).filter(
            RosterConfig.group_id == current_user.group_id
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
