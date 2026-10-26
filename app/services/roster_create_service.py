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
from types import SimpleNamespace
from sqlalchemy.orm import Session
from fastapi import HTTPException
from db.models import (
    DailyShift,
    FixedWantedEntry,
    BannedWantedEntry,
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
from typing import NamedTuple
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
from services.in_chunks import in_chunks  # EI-13c IN 목록 300개 단위
from services.nurse_monthly_limit_service import fetch_effective_monthly_limits_by_nurse
from services.cp_sat.mid_feasibility import validate_mid_hard_feasibility as _validate_mid_hard_feasibility_impl
from services.semantics import attach_reason_code_ontology
from services.engine_contract.aliases import normalize_aliases
from services.precheck.relaxation_notice import build_relaxation_notice  # EI-14c 자동 완화 안내(순수 함수)
# EI-5 배관 헬퍼(설계서 §5.3) — precheck 차단·나이트 과공급 차단 두 블록이 공유한다.
#   이 모듈은 모듈 최상단에서 roster_create_service 를 끌어오지 않는다(함수 안
#   지연 import — presolve_block.py 모듈 docstring '순환 import 주의' 참조)라
#   여기서 top-level import 해도 순환이 생기지 않는다.
from services.precheck.presolve_block import (
    PresolveBlockContext,
    PresolveFinding,
    raise_presolve_block,
    run_presolve_detectors,
)
# EI-9-0 — 고정근무 차감 블록(읽기·계산·대입). 이 모듈은 rcs 를 최상단에서 끌어오지 않는다
#   (함수 안 지연 import — 위 presolve_block.py 와 같은 규약, D23).
from services.engine_input.fixed_cut import (
    apply_fixed_cut_demand,
    compute_fixed_cut_demand,
    read_fixed_cut_inputs,
)
# EI-10a·b — 탐지기·precheck 입력을 단계 A(PrecheckStage) view 로. 이 모듈도 rcs 를
#   최상단에서 끌어오지 않는다(같은 순환 가드, EI10AB_SPEC.md §2.1).
from services.engine_input.views import (
    _plain_row,
    _plain_vars,
    presolve_view,
    stage_a_at_precheck,
    to_precheck_input,
    to_roster_ir,
    to_roster_ir_post,
)
from services.engine_input.builder import AttemptSource, build_engine_input_at_first_attempt
# EI-11 11w-2·11w-3 — 출발 경계 칸 · 확정 칸 조정(순수 함수 · DB·rcs import 없음 — 같은 순환 가드).
from services.engine_input.boundary_fixed import (
    HERE,
    departure_boundary,
    fixed_n_block_can_extend,
    reconcile_backward,
    reconcile_forward,
    rules_from_config,
)
from services.engine_input.boundary_fixed import night_extension_limit as _night_extension_limit

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
    # ★ 그 달과 겹치는 **끝난 파견**으로 온 사람도 — 엔진 명단(아래 `get_active_assignments_for_month`:
    #   active·completed)엔 들어가는데 원티드 수집에서만 빠져, 받는 병동 생성에 그 사람 원티드가
    #   없었다(2026-10-06). 같은 조회로 맞춘다.
    for _a in get_active_assignments_for_month(db, current_user.group_id, req.year, req.month):
        _aid = str(_a.nurse_id)
        if (_a.reason in _INBOUND_REASONS and _a.target_group_id == current_user.group_id
                and _a.source_group_id != current_user.group_id
                and _aid not in _source_ids and _aid not in _inbound_ids):
            _inbound_ids.append(_aid)
    nurse_ids = _source_ids + _inbound_ids
    if _inbound_ids:
        print(f"[RosterCreate] fixed_wanted 조회 대상 inbound nurse 추가: {_inbound_ids}")
    month_str = f"{req.year}-{req.month:02d}"
    preferences = []
    special_shift_map = _load_special_shift_map(db, current_user.group_id, current_user.office_id)
    special_fixed_requests: list[dict] = []
    # ★★ 원본 `shift_id` 를 대표코드(D/E/N/O)로 옮기는 맵.
    #   아래 DENO 분류가 `shift_id` 를 리터럴 {"D","E","N","O"} 와 **직접 비교**하고 있었다.
    #   병동이 OFF 를 `OF`·`오프` 로 쓰면 `"OF" in {...}` 가 False 라 **원티드 OFF 가 통째로
    #   미분류로 버려진다**(실측 2026-09-17 성남 중환자실-RN: 확정 원티드 168건 중 `OF` 91건이
    #   미분류. 그 병동은 `fixed_wanted_use_yn=True` 라 다른 경로로 살아나 드러나지 않았다).
    #   지금 26/27 병동이 OFF 코드를 `O` 로 써서 **우연히** 동작할 뿐이다.
    #   ★ `fixed_wanted_use_yn=True` 경로는 원본 코드를 그대로 넘겨 솔버가 정규화한다 —
    #     같은 데이터를 두 경로가 다르게 분류하던 비대칭을 여기서 맞춘다.
    #   ★★ `shifts` 는 PK·UNIQUE 가 없어 같은 `(group_id, shift_id)` 가 **여러 행**일 수 있다.
    #     정렬 없이 `=` 로 대입하면 **DB 반환 순서에 따라 대표 `default_shift` 가 달라져**
    #     같은 원티드가 실행마다 다른 D/E/N/O 버킷으로 분류될 수 있다.
    #     코드베이스의 다른 자리와 같은 규약으로 `(sequence, id)` 오름차순 + 첫 행 채택.
    _deno_main_map: dict[str, str] = {}
    try:
        _shift_rows = (
            db.query(Shift)
            .filter(Shift.group_id == current_user.group_id)
            .order_by(Shift.sequence.asc(), Shift.id.asc())
            .all()
        )
        for _s in _shift_rows:
            _ds = str(getattr(_s, "default_shift", "") or "").strip().upper()
            if _ds in ("D", "E", "N", "O"):
                _deno_main_map.setdefault(str(_s.shift_id).strip().upper(), _ds)
    except Exception as _dm_exc:
        print(f"[RosterCreate][WARN] DENO 대표코드 맵 구성 실패 — 원본 코드로 진행: {_dm_exc}")

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

                # ★ 원본 코드가 대표코드와 우연히 같은 경우(`D`·`O`)뿐 아니라
                #   `OF`·`오프` 처럼 다른 경우도 `default_shift` 로 옮겨 잡는다.
                _deno = _deno_main_map.get(shift_code, shift_code)
                if _deno in shift_data:
                    # 확정 원티드는 최고 우선순위 (score=10)
                    shift_data[_deno][day_str] = 10
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

            # [EI-2a] "request" 키는 죽은 전달이었다 — parse_preferences_from_db(cp_sat_basic.py)가
            #   data["shift"]·data["preference"] 만 읽고 "request" 는 읽지 않는다(verify).
            data_json = {
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
    # ★ request_id 는 (간호사, 월) 스코프로 1부터 다시 채번된다(`wanted_service._next_request_id`).
    #   그래서 같은 (nurse_id, request_id) 가 여러 달에 존재하고, 아래 셀 조회를 월로 묶지
    #   않으면 다른 달 셀이 딸려 와 **일(day)만 떼어 쓰는 DENO 경로에서 같은 날짜로 잘못
    #   반영된다.** 특수코드 분기는 이미 year/month 를 확인하는데 DENO 만 빠져 있었다.
    _wr_start = date(req.year, req.month, 1)
    _wr_end = _wr_start + timedelta(days=calendar.monthrange(req.year, req.month)[1])
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

        # 제출된 원티드만 반영 — 미제출 draft 폴백 제거.
        # (미제출=미반영 정합. fixed_wanted_entries 의 노출여부/제출상태와 일치시켜,
        #  화면상 "미제출"인 원티드가 생성에 하드고정으로 새어들어가는 불일치를 차단.)
        target_wr = submitted_wr
        if not target_wr:
            continue  # 제출된 원티드가 없는 간호사는 건너뜀(미제출 draft 미반영)

        # 3️⃣ shift 데이터 수집
        shift_rows = (
            db.query(NurseShiftRequest)
            .filter(
                NurseShiftRequest.nurse_id == nurse_id,
                NurseShiftRequest.request_id == target_wr.request_id,
                # 반열린 구간 [1일, 다음달 1일) — `wanted_service.py:1086-1088` 과 같은 형태다.
                # 문자열 LIKE 는 SARGable 이 아니라 쓰지 않는다.
                NurseShiftRequest.shift_date >= _wr_start,
                NurseShiftRequest.shift_date < _wr_end,
            )
            .all()
        )
        shift_data = {"D": {}, "E": {}, "N": {}, "O": {}}
        for s in shift_rows:
            shift_code_raw = str(s.shift or "").strip()
            shift_code = shift_code_raw.upper()
            day_str = str(int(str(s.shift_date).split("-")[-1]))
            # ★ FixedWantedEntry 경로와 **같은 기준**으로 대표코드를 맞춘다.
            #   한쪽만 고치면 `FixedWantedEntry` 가 없는 달에서 도로 `OF` 가 버려진다.
            _deno_wr = _deno_main_map.get(shift_code, shift_code)
            if _deno_wr in shift_data:
                shift_data[_deno_wr][day_str] = int(s.score) if s.score is not None else 0
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
        # [EI-2a] "request" 키는 죽은 전달이었다 — parse_preferences_from_db(cp_sat_basic.py)가
        #   data["shift"]·data["preference"] 만 읽고 "request" 는 읽지 않는다(verify).
        data_json = {
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


def _build_generate_snapshot(db: Session, config, status: str, year: int, month: int,
                             nurses_dict=None, config_dict=None,
                             job_id: str | None = None,
                             persistent_fix: dict | None = None) -> str | None:
    """생성 시도 시점의 **입력 전체**를 JSON 으로 만든다.

    왜 본문인가: input_hash(shadow_diagnosis._input_hash)는 같은 입력을 묶는 지문일 뿐
    되돌릴 수 없어서 "무엇이 문제였나" 를 못 본다. 모순 분석이 목적이면 본문이 필요하다.
    또 그 해시에는 **원티드가 빠져 있어** 원티드만 바뀐 경우를 구분하지 못한다.

    담는 것 — 생성 성패를 가르는 입력을 한자리에:
      nurses  간호사별 프로파일(등급·팀·허용시프트·고정근무·개인 상하한·주휴 등)
      config  roster_config + daily_shift 요구 + team_min + 선호 가중치
      wanted  원티드 신청(해시가 놓치던 축)
      grade   등급 요구(roster_grade_config)
    """
    try:
        gid = str(getattr(config, "group_id", "") or "")
        if not gid:
            return None
        if nurses_dict is None:
            # 폴백 — 엔진이 만든 상세 프로파일이 없을 때만(precheck 전에 실패한 경우).
            #   ★ nurses.is_weekend_off 는 담지 않는다. 2026-07-22 언매핑된 캐시 컬럼이고
            #     SSOT 는 nurse_weekendoff_period 라, 여기서 읽으면 **솔버가 본 값과 다른**
            #     낡은 값이 기록으로 남는다(빠진 것보다 나쁘다 — 분석을 오도한다).
            #     그래서 이 경로의 스냅샷은 얇다는 사실 자체를 _partial 로 표시한다.
            nurses_dict = [
                dict(r._mapping) for r in db.execute(text(
                    "SELECT nurse_id, name, grade, team_id, active, sequence, "
                    "joining_date, resignation_date "
                    "FROM nurses WHERE group_id = :g"), {"g": gid})
            ]
        # 원티드는 세 테이블로 나뉜다 — solver 입력이 되는 두 개를 담는다.
        #   fixed_wanted_entries  희망 근무(고정 배정)
        #   banned_wanted_entries 기피 근무(금지)
        #   ※ `wanted` 는 그룹 단위 신청 **기간** 설정이라 개별 신청이 아니다.
        _p = {"g": gid, "y": int(year), "m": int(month)}
        wanted_fixed = [
            dict(r._mapping) for r in db.execute(text(
                "SELECT nurse_id, shift_date, shift_id, is_applied "
                "FROM fixed_wanted_entries "
                "WHERE group_id = :g AND year = :y AND month = :m"), _p)
        ]
        wanted_banned = [
            dict(r._mapping) for r in db.execute(text(
                "SELECT nurse_id, shift_date, banned_shift_ids, is_applied "
                "FROM banned_wanted_entries "
                "WHERE group_id = :g AND year = :y AND month = :m"), _p)
        ]
        # ★ 엔진이 실제로 본 값을 담아야 한다. constraints_json 은 JSON 문자열로
        #   저장돼 있어 그대로 실으면 payload 안에서 **이중 인코딩**된 문자열이 된다.
        #   또 엔진은 _ensure_grade1_default 로 grade-1 floor 를 얹은 뒤 쓰므로,
        #   가공 전 원본만 남기면 "왜 grade1 이 저렇게 잡혔지" 를 못 푼다. 둘 다 담는다.
        grade_raw = db.execute(text(
            "SELECT TOP 1 constraints_json FROM roster_grade_config "
            "WHERE group_id = :g ORDER BY config_id DESC"), {"g": gid}).scalar()
        grade_cfg = grade_raw
        if isinstance(grade_raw, str):
            try:
                grade_cfg = json.loads(grade_raw)
            except (ValueError, TypeError):
                grade_cfg = grade_raw
        grade_effective = None
        try:
            grade_effective = _fetch_grade_config_dict(
                db, str(getattr(config, "office_id", "") or ""), gid)
        except Exception:
            pass
        payload = {
            "at": datetime.now().isoformat(timespec="seconds"),
            "status": status,
            # ★ 실패는 schedules 에 행이 남지 않는다(roster_jobs.result_roster_id=None).
            #   추적 고리는 roster_jobs.job_id 뿐이라 스냅샷에 실어 둔다.
            #   roster_jobs 에 config_id 컬럼이 없어 반대 방향(job→config)은 못 잇는다.
            "job_id": job_id,
            # 어느 프리셋에서 파생됐는지 — 실패 기록이 별도 행으로 갈 때 원본을 가리킨다.
            "source_config_id": getattr(config, "config_id", None),
            "year": int(year), "month": int(month),
            "nurses": nurses_dict,
            # ★ `_` 접두 키를 통째로 버리면 **실제 솔버 입력도 같이 버린다**
            #   (_inbound_source_map · _2n2off_pre_injected 등). 진단용 임시 키만
            #   골라 빼는 대신, 담고 나서 크기 문제가 생기면 그때 좁힌다 —
            #   분석 기록에서 입력 누락은 되돌릴 수 없지만 용량은 나중에 줄일 수 있다.
            "config": dict(config_dict or {}),
            "wanted_fixed": wanted_fixed,
            "wanted_banned": wanted_banned,
            "grade_config": grade_cfg,
            "grade_config_effective": grade_effective,
            # 이 스냅샷이 얇은지(엔진 상세 프로파일 없이 DB 폴백으로 채웠는지).
            #   precheck 전에 실패하면 nurses 가 8필드짜리라 등급·허용시프트가 없다.
            "_partial": bool(config_dict is None),
            # 이번 런에서 **데이터를 실제로 고친** 옵션들(주말휴무 해제 · 월 한도 하향 ·
            #   금지근무 해제 · 근무유형 추가). nurses/wanted 는 이미 고쳐진 뒤의 값이라
            #   이 키가 없으면 "원래 그랬던 것" 과 구분되지 않는다.
            "persistent_fix": persistent_fix or None,
        }
        # ★ config_dict 에는 튜플을 키로 쓰는 dict 가 섞인다(fixed_cells 의 (nurse, day) 등).
        #   json 은 str/int/float/bool/None 키만 받으므로 default=str 로는 못 막고
        #   통째로 실패한다(실측: 나사렛 7B 두 건이 스냅샷 0B 로 유실).
        #   키를 문자열로 눌러 담는다 — 분석용 기록이므로 원형 복원보다 보존이 우선이다.
        #   ★ set 도 처리한다. default=str 로 넘어가면 "{'a', 'b'}" 같은 repr 문자열이
        #     박제돼 파싱이 안 된다(lookahead_weekly_off_cells 등이 set 이다).
        #     정렬해 리스트로 — 같은 입력이 같은 JSON 이 되어 스냅샷 간 대조가 된다.
        def _key_safe(o):
            if isinstance(o, dict):
                return {(k if isinstance(k, (str, int, float, bool)) or k is None
                         else str(k)): _key_safe(v) for k, v in o.items()}
            if isinstance(o, (set, frozenset)):
                return sorted((_key_safe(v) for v in o), key=str)
            if isinstance(o, (list, tuple)):
                return [_key_safe(v) for v in o]
            return o

        return json.dumps(_key_safe(payload), ensure_ascii=False, default=str)
    except Exception as _sn_exc:
        # ★ rollback 을 반드시 한다. 위 SELECT 중 하나가 DB 오류로 죽으면 세션이
        #   비활성 상태로 남아, 이어지는 status UPDATE 가 PendingRollbackError 로
        #   같이 죽는다(스냅샷만 없는 게 아니라 상태 기록까지 유실된다).
        try:
            db.rollback()
        except Exception:
            pass
        print(f"[ConfigGenerateStatus] 스냅샷 생성 실패(무시): {_sn_exc}")
        return None


def _mark_config_generate_status(db: Session, config, status: str,
                                 snapshot: str | None = None,
                                 year: int | None = None, month: int | None = None,
                                 job_id: str | None = None,
                                 intervened: bool = False) -> None:
    """생성 시도 결과를 두 곳에 남긴다 — 역할이 다르다.

    roster_config.last_generate_status   "목록에 보일까"
      실패하면 숨긴다 — 월 구분도, 되살림도 없다. 그 설정을 편집해 저장해도
      다시 노출하지 않는다(실패한 설정까지 보이면 프리셋의 의미가 퇴색된다).
      다시 쓰려면 새로 저장하면 신규 행이라 NULL 에서 시작한다.

    roster_config_attempt   "그때 입력이 무엇이었나" (분석 전용)

    status  success | blocked(precheck 차단) | infeasible(solver 가 못 품)
    성공·실패 모두 남긴다 — 실패만 모으면 비교 대상이 없어
      "직전엔 됐는데 왜 안 되지" 를 못 푼다.

    intervened  이 런에 개입이 있었나.
      일시적 — config_override(해결책 카드) · treatment_ids(온톨로지 처방) ·
        applied_relaxations(team_min hard→soft 자동 완화) ·
        column_delta_mode(apply-resolution 이 컬럼을 임시 commit 했다 되돌리는 것).
        저장된 프리셋 값의 성패가 아니므로 True.
      영구 — weekend_off_release · monthly_limit_release · banned_wanted_release ·
        allowed_shift_add 는 간호사·원티드 데이터를 실제로 고치고 commit 한다.
        조건 자체가 바뀐 뒤의 유효한 결과이므로 False(스냅샷의 persistent_fix 로 추적).

    ★ 월별로 판정하지 않는다. 실패한 설정은 다음 달에도 숨긴다 — 다시 쓰려면
      새로 저장하면 되므로 굳이 노출할 이유가 없다. 월별로 가르려던 설계는
      컬럼이 마지막 시도만 담아 오판이 났고(실측 2건), 판정 기준을 단순화하면서
      그 문제 자체가 사라졌다.

    ★ 기록 실패가 생성 자체를 막으면 안 된다 — 예외는 삼키고 로그만 남긴다.
      두 기록을 각각 try 로 감싸는 이유 — 한쪽이 실패해도 다른 쪽은 남겨야 한다.
    ★★ 마이그레이션은 코드보다 먼저 들어가야 한다. 이 try/except 로는 못 막는다 —
      매핑 컬럼이라 db.query(RosterConfig) 하는 곳들이 먼저 죽는다.
    """
    # ★ getattr 을 try 안에 둔다. SessionLocal 은 expire_on_commit 기본값(True)이라
    #   앞선 commit 이 인스턴스를 만료시켜 이 접근이 refresh SELECT 를 유발한다.
    #   그 SELECT 가 실패하면 예외가 이 함수 밖으로 나가고, 성공 지점에서는
    #   _ctx["recorded"] 가 아직 False 라 래퍼가 error 를 찍고 재던진다 —
    #   **이미 커밋된 정상 근무표가 HTTP 500 으로 나간다.**
    try:
        cid = getattr(config, "config_id", None)
    except Exception as _cid_exc:
        print(f"[ConfigGenerateStatus] config_id 조회 실패(기록 생략): {_cid_exc}")
        return
    if cid is None:
        return
    # ── ① 목록 필터용 상태 (roster_config) ──
    #   개입이 낀 런은 저장된 값 그대로의 성패가 아니므로 기록하지 않는다.
    #   한 번 실패로 기록되면 편집 저장으로도 되살리지 않는다 — 방침이다.
    if intervened:
        print(f"[ConfigGenerateStatus] config_id={cid} 는 개입이 있던 런 — "
              f"{status} 를 프리셋에 기록하지 않는다. 분석 기록은 attempt 로.")
    else:
        try:
            # ★ 'error' 는 **이전 판정을 지우면 안 된다.** 목록 필터가 error 를
            #   통과시키므로(설정 탓이 증명 안 된 실패라서), 조건 없이 덮으면
            #   앞서 blocked/infeasible 로 숨겨 둔 프리셋이 되살아난다.
            #   (config X 가 infeasible → 다음 달 재시도 중 MID 검증 예외 →
            #    error 가 infeasible 을 지움 → 목록에 복귀. 방침 위반)
            #   그래서 error 는 **아직 판정이 없을 때만** 쓴다.
            if status == "error":
                _sql = ("UPDATE roster_config SET last_generate_status = :s "
                        " WHERE config_id = :c AND last_generate_status IS NULL")
            else:
                _sql = ("UPDATE roster_config SET last_generate_status = :s "
                        " WHERE config_id = :c")
            _r = db.execute(text(_sql), {"s": status, "c": int(cid)})
            db.commit()
            if status == "error" and getattr(_r, "rowcount", 1) == 0:
                print(f"[ConfigGenerateStatus] config_id={cid} 는 이미 판정이 있어 "
                      f"error 로 덮지 않는다. 기록은 attempt 로.")
            else:
                print(f"[ConfigGenerateStatus] config_id={cid} → {status}")
        except Exception as _mk_exc:
            try:
                db.rollback()
            except Exception:
                pass
            print(f"[ConfigGenerateStatus] 상태 기록 실패(무시): "
                  f"config_id={cid} {_mk_exc}")

    # ── ② 분석용 스냅샷 (roster_config_attempt) ──
    try:
        db.execute(
            text("INSERT INTO roster_config_attempt "
                 "(source_config_id, office_id, group_id, job_id, "
                 " year, month, status, intervened, snapshot, created_at) "
                 "VALUES (:c, :o, :g, :j, :y, :m, :s, :iv, :snap, GETDATE())"),
            {"c": int(cid),
             "o": str(getattr(config, "office_id", "") or "") or None,
             "g": str(getattr(config, "group_id", "") or ""),
             "j": job_id, "y": year, "m": month,
             "s": status, "iv": bool(intervened), "snap": snapshot},
        )
        # ★ 프리셋당 보존량을 묶는다. 스냅샷이 건당 30KB 대라 상한이 없으면
        #   단조 증가한다(71병동 × 월 5회 = 연 130MB, 공용 온프렘 서버라 무시 못 함).
        #   스냅샷에 간호사 실명이 들어가므로 보존량 자체를 제한하는 편이 낫다.
        #
        #   ★★ 단 **월별 최신 순수 기록은 지우지 않는다.** 목록 판정에 쓰는 게 아니라
        #     (그건 roster_config.last_generate_status 가 한다) **분석 때문**이다 —
        #     "9월엔 됐는데 10월엔 왜 안 되지" 는 달별 마지막 결과를 대조해야 답이
        #     나온다. 최근 20건만 남기면 나중 달을 몇 번 돌린 순간 예전 달 기록이
        #     통째로 사라져 그 대조가 불가능해진다.
        #   실제 보존량은 20건 + (24개월 내 서로 다른 year·month 수) 다.
        #     intervened 건은 판정에 안 쓰이므로 이 보호 대상이 아니다.
        #     24개월을 넘긴 것도 제외한다 — 무한 누적을 막는다.
        db.execute(
            text("DELETE FROM roster_config_attempt "
                 "WHERE source_config_id = :c AND group_id = :g "
                 "  AND attempt_id NOT IN ("
                 "      SELECT TOP 20 attempt_id FROM roster_config_attempt "
                 "      WHERE source_config_id = :c AND group_id = :g "
                 "      ORDER BY attempt_id DESC) "
                 "  AND attempt_id NOT IN ("
                 "      SELECT MAX(attempt_id) FROM roster_config_attempt "
                 "      WHERE source_config_id = :c AND group_id = :g "
                 "        AND (intervened IS NULL OR intervened = 0) "
                 "        AND created_at >= DATEADD(month, -24, GETDATE()) "
                 "      GROUP BY year, month)"),
            {"c": int(cid), "g": str(getattr(config, "group_id", "") or "")},
        )
        db.commit()
        _sz = f" snapshot={len(snapshot)}B" if snapshot else " snapshot=없음"
        _iv = " intervened" if intervened else ""
        print(f"[ConfigGenerateAttempt] config_id={cid} {year}-{month} "
              f"{status}{_iv}{_sz} job_id={job_id}")
    except Exception as _at_exc:
        try:
            db.rollback()
        except Exception:
            pass
        print(f"[ConfigGenerateAttempt] 기록 실패(무시): config_id={cid} {_at_exc}")


def _build_precheck_cause_bridge_finding(
    nurses_dict: list[dict], config_dict: dict, year: int, month: int,
) -> PresolveFinding | None:
    """precheck 차단 시 개인/주말 원인을 진단해 카드로 합류시킨다 — post-solve cause
    경로는 솔버 호출이 생략되는 precheck 차단에서는 안 도므로 여기서 직접 진단한다
    (EI-5 이전부터의 인라인 로직, 오버레이 finding 으로만 모양을 바꿨다). 실패는
    무시하고 None 으로 돌아간다 — 탐지기 실패는 로그만 남기고 진행.
    """
    try:
        import calendar as _cal_cb

        from services.ontology_graph.lagrangian import explain_infeasibility_from_config as _explain_cb
        from services.ontology_graph.mcs_trace import cause_to_resolution_options as _cards_cb

        _nd_cb = _cal_cb.monthrange(year, month)[1]
        _cause_cb = _explain_cb(nurses_dict, config_dict, _nd_cb, year=year, month=month)
        _cards = _cards_cb(_cause_cb.classification, _cause_cb.top_family, _cause_cb.targets)
        if not _cards:
            return None
        _msg_cb = (
            _cause_cb.certificate
            if _cause_cb.classification != "unknown" and _cause_cb.certificate
            else None
        )
        print(f"[Precheck][CauseBridge] {_cause_cb.classification}/"
              f"{_cause_cb.top_family} 카드 {len(_cards)}건 합류")
        return PresolveFinding(reason_code=None, cards=_cards, message_ko=_msg_cb)
    except Exception as _cb_exc:
        print(f"[Precheck][CauseBridge] 실패(무시): {_cb_exc}")
        return None


def _presolve_block_ctx(
    *, db: Session, config, year: int, month: int, job_id: str | None,
    persistent_fix: dict | None, intervened: bool,
    nurses_dict: list[dict] | None, config_dict: dict | None, schedule,
    ctx: dict | None, log_tag: str = "[PresolveBlock]",
) -> PresolveBlockContext:
    """두 차단 블록(precheck·나이트)이 반복해 짓던 `PresolveBlockContext` 생성을
    한 곳으로(EI-5). `record_status`·`build_snapshot` 은 이 함수 **호출 시점**에
    모듈 전역(`_mark_config_generate_status`·`_build_generate_snapshot`)을
    참조한다 — 특성화 테스트의 몽키패치가 그대로 반영된다(presolve_block.py
    모듈 docstring '순환 import 주의' 참조, 같은 이유로 여기서도 지연 평가해야
    한다).
    """
    return PresolveBlockContext(
        db=db, config=config, year=year, month=month, job_id=job_id,
        persistent_fix=persistent_fix, intervened=intervened,
        nurses_dict=nurses_dict, config_dict=config_dict, schedule=schedule, ctx=ctx,
        record_status=_mark_config_generate_status, build_snapshot=_build_generate_snapshot,
        log_tag=log_tag,
    )


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


def _fetch_grade_config_dict(db: Session, office_id: str, group_id: str, swallowed: list | None = None) -> dict:
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
            # 병원이 직접 넣은 등급 최소(기본 floor 적용 전). 엔진은 쓰지 않고, 생성 결과의
            #   등급 최소 미달 보고(constraint_impact `_evaluate_grade`)가 이것만 센다.
            "configured_constraints_json": {},
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
        if swallowed is not None:
            swallowed.append(e)  # 빌더 경로(EI-13a): 삼킨 읽기 실패를 알려 AttemptSource 를 만들지 않게 한다
        return _default()

    if not row:
        return _default()

    configured_min = _safe_json_obj(getattr(row, "constraints_json_text", None), "constraints_json")
    return {
        "use_dynamic_scaling": bool(getattr(row, "use_dynamic_scaling", True)),
        "allow_soft_fallback": bool(getattr(row, "allow_soft_fallback", False)),
        # [GRADE_DEFAULT_111] 등록 config 에도 grade-1 D/E/N 각 1 floor 를 보장.
        "constraints_json": _ensure_grade1_default(configured_min),
        "constraints_max_json": _safe_json_obj(getattr(row, "constraints_max_json_text", None), "constraints_max_json"),
        # floor 적용 전 원본 — 미달 보고용(_default 주석 참고). 병원이 1등급을 0으로 둔
        #   병동에서 floor 미달까지 '하드 위반' 으로 알리던 것을 막는다.
        "configured_constraints_json": configured_min,
    }


def _build_shift_manage_and_requirements(
    db: Session, current_user, latest_config, req,
    *, require_nonzero_requirements: bool = True,
):
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
    # ★ 단, 엔진 대상이 0명이면(전원 fixed_shift) 요구치 0 은 **정상**이다.
    #   그런 병동은 솔버를 우회해 평일=fixed_shift 로 채워지므로 요구치를 쓰지 않는다.
    #   (2026-08-14 인천의료원 수술실: 19명 전원 고정근무인데 이 가드에 막혀 생성 불가)
    if require_nonzero_requirements and sum(daily_shift_requirements.values()) <= 0 and all(
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
    # ★ resignation_date 는 **퇴사일 그 자체**이고 그날부터는 소속이 아니다.
    #   따라서 마지막 근무일은 resign_date - 1 일이다.
    #   (기존에는 min(resign_date, month_end) 라 퇴사일 당일까지 배정됐다.
    #    실측: 이애진 resignation_date=2026-07-20 인데 20일에 N 배정 →
    #    18·19·20 3연속 N. 이윤지는 퇴사일을 빼면 1N 단독만 남아
    #    하드락 7번(1N 금지) 위반이 된다.)
    #   ※ wanted_service._compute_leave_blocked_days_in_month 는 이미
    #     `cur = max(_resign_d, m_start)` 로 퇴사일 당일부터 막고 있었다 —
    #     두 경로의 해석이 어긋나 있었고 이쪽을 그에 맞춘다.
    last_work_date = resign_date - timedelta(days=1) if resign_date else None
    # ❌ 월과 겹치지 않으면 바로 제외
    if last_work_date and last_work_date < month_start:
        return None
    if join_date and join_date > month_end:
        return None
    # ✅ 실제 근무 가능 날짜 범위
    start_date = max(join_date, month_start) if join_date else month_start
    end_date = min(last_work_date, month_end) if last_work_date else month_end

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


def _holiday_off_enabled(db: Session, group_id: str) -> bool:
    """`roster_config.fixed_holiday_off_yn` — 고정근무자를 공휴일에 쉬게 할지.

    ★ ORM 모델에 올리지 않고 raw SQL 로 읽는다
      `roster_config` 는 dev·prod 스키마가 이미 갈려 있다(2026-08-18 실측: 모델에만
      있는 컬럼 10개 · dev 에만 있는 컬럼 11개). 모델에 얹으면 컬럼이 없는 환경에서
      **설정 조회가 통째로 깨진다.** 존재를 먼저 확인하고 없으면 False 로 떨어뜨리면
      코드를 먼저 배포하고 DDL 을 나중에 넣어도 안전하다(`shifts.call_base_id` 와 동일).

    ★ 켜지 않은 그룹은 종전대로 평일에 `fixed_shift` 가 채워진다.
    """
    from sqlalchemy import text

    try:
        has_col = db.execute(text(
            "SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_NAME = 'roster_config' AND COLUMN_NAME = 'fixed_holiday_off_yn'"
        )).first()
        if not has_col:
            return False
        row = db.execute(text(
            "SELECT TOP 1 fixed_holiday_off_yn FROM roster_config "
            "WHERE group_id = :g ORDER BY config_id DESC"
        ), {"g": group_id}).first()
        return bool(row and row[0])
    except Exception as exc:  # noqa: BLE001
        print(f"[Holiday] 설정 조회 실패 — 미적용으로 진행: {exc}")
        return False


def _n2n_min_gap_setting(db: Session, group_id: str) -> int:
    """`roster_config.n2n_min_gap` — N 블록 간 최소 간격 하드(0=해제, 실효 4부터).

    ★ `_holiday_off_enabled` 와 **같은 규약**으로 raw SQL 로 읽는다.
      `roster_config` 는 dev·prod 스키마가 갈려 있어(모델에만 있는 컬럼 10개 ·
      dev 에만 있는 컬럼 11개) 모델에 얹으면 컬럼 없는 환경에서 설정 조회가 통째로
      깨진다. 존재를 먼저 확인하고 없으면 0(해제)으로 떨어뜨리므로 **코드를 먼저
      배포하고 DDL 을 나중에 넣어도 안전하다.**

    ★★ 이 함수가 없으면 DDL 을 넣어도 **엔진이 못 읽는다** — ORM 모델에 없는 컬럼이라
      `latest_config.__dict__` 에 안 담기고, `config_data.get("n2n_min_gap", 0)` 이
      항상 0 이 되어 하드가 영원히 꺼져 있다.

    ★★ **`n2n_min_gap IS NOT NULL` 조건이 핵심이다.** 생성은 시작할 때 config 를 포크하는데
      (`materialize_generation_config` · `roster_create_service:5127`), 그 INSERT 는 모델 밖
      컬럼을 담지 못해 새 config 의 `n2n_min_gap` 이 **NULL** 이 된다. 그런데 이 조회는
      포크 **뒤**(:5532)에 돌기 때문에, 조건 없이 `ORDER BY config_id DESC` 로 읽으면
      **방금 만들어진 NULL 행**을 집어 항상 0 이 된다 — 켜 둔 설정이 첫 생성부터 무시된다.
      NULL 행을 건너뛰면 운영자가 실제로 값을 넣은 config 를 찾으므로
      `_PRESERVE_RAW_COLUMNS` 승계 여부와 **무관하게** 동작한다(그 승계가 없는 main 에서도).
    ★ 끄려면 NULL 이 아니라 **0 을 명시**해야 한다. NULL 은 "설정한 적 없음" 이다.
    """
    from sqlalchemy import text

    try:
        has_col = db.execute(text(
            "SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_NAME = 'roster_config' AND COLUMN_NAME = 'n2n_min_gap'"
        )).first()
        if not has_col:
            return 0
        row = db.execute(text(
            "SELECT TOP 1 n2n_min_gap FROM roster_config "
            "WHERE group_id = :g AND n2n_min_gap IS NOT NULL "
            "ORDER BY config_id DESC"
        ), {"g": group_id}).first()
        return int(row[0]) if (row and row[0] is not None) else 0
    except Exception as exc:  # noqa: BLE001
        print(f"[N2N-MinGap] 설정 조회 실패 — 해제로 진행: {exc}")
        return 0


def inherit_n2n_min_gap(db: Session, group_id: str, new_config_id) -> None:
    """포크된 새 config 에 `n2n_min_gap` 을 승계한다(값이 비어 있을 때만).

    ★ 왜 필요한가 — 생성이 config 를 포크할 때(`materialize_generation_config`) 그 INSERT 는
      **모델 밖 컬럼을 담지 못해** 새 config 의 `n2n_min_gap` 이 NULL 이 된다.
      조회(`_n2n_min_gap_setting`)는 `IS NOT NULL` 로 값 있는 행을 찾으므로 **설정은 유지되지만**,
      config 가 쌓일수록 **끄기가 어려워진다** — 최신 행을 NULL 로 되돌려도 옛 행이 읽힌다.
      승계해 두면 최신 config 에 항상 값이 있어 **그 행만 0 으로 바꾸면 꺼진다.**
    ★ `roster_service._PRESERVE_RAW_COLUMNS` 와 같은 목적인데, 그쪽은 `roster_service.py` 가
      main↔dev 로 크게 갈려 있어(2026-09-18 실측 1474줄) 여기서 처리한다.
    ★ 컬럼이 없는 환경에서도 안전하도록 존재를 먼저 확인한다.
    ★ **이미 값이 있으면 건드리지 않는다**(`AND n2n_min_gap IS NULL`) — 운영자가 새 config 에
      직접 넣은 값을 옛 값으로 덮으면 안 된다.
    ★★ 출처는 **baseline(포크 직전 config)** 이다 — "값이 있는 아무 config" 가 아니다.
      운영자가 고른 baseline 을 그대로 따라야 설정 이력이 갈리지 않는다. baseline 이
      비어 있으면 새 config 도 비는 게 맞다(그 config 에는 원래 설정이 없었다는 뜻).
    """
    if not new_config_id:
        return
    from sqlalchemy import text

    try:
        has_col = db.execute(text(
            "SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_NAME = 'roster_config' AND COLUMN_NAME = 'n2n_min_gap'"
        )).first()
        if not has_col:
            return
        db.execute(text(
            "UPDATE roster_config SET n2n_min_gap = ("
            "  SELECT TOP 1 n2n_min_gap FROM roster_config "
            "  WHERE group_id = :g AND config_id < :n "
            "  ORDER BY config_id DESC"
            ") WHERE config_id = :n AND n2n_min_gap IS NULL"
        ), {"g": group_id, "n": new_config_id})
        db.commit()
    except Exception as exc:  # noqa: BLE001
        # 승계 실패가 생성을 막지 않게 한다 — 조회의 `IS NOT NULL` 이 백업으로 동작한다.
        db.rollback()
        print(f"[N2N-MinGap] config 승계 실패(무시): {exc}")


def _kr_holidays_in_month(year: int, month: int) -> set[int]:
    """그 달의 한국 공휴일(대체공휴일 포함) 일자. 조회 실패 시 빈 집합.

    `holiday_pack` 이 쓰는 `python-holidays` 와 같은 소스다. 패키지가 없거나
    조회가 실패해도 생성이 멈추면 안 되므로 빈 집합으로 떨어뜨린다 —
    그 경우 종전처럼 평일로 채워진다(퇴행이지 파손은 아니다).
    """
    try:
        import holidays as _h

        return {d.day for d in _h.KR(years=[year]) if d.year == year and d.month == month}
    except Exception as exc:  # noqa: BLE001
        print(f"[Holiday] 공휴일 조회 실패 — 평일로 진행: {exc}")
        return set()


def _build_fixed_shift_roster(
    fixed_nurses: list[Nurse],
    year: int,
    month: int,
    weekday_off_code: str = "O",
    sunday_code: str = "주",
    shift_lookup: dict[str, Shift] | None = None,
    weekly_off_active: bool = True,
    holiday_off: bool = False,
) -> dict[str, list[str]]:
    """고정 근무 간호사의 월간 스케줄을 생성합니다.

    - 평일(월~금): fixed_shift 코드 배정
    - 토요일: weekday_off_code
    - 일요일: weekly_off_active이고 간호사 weekly_off_enabled가 1일 때만 sunday_code
    - **공휴일(대체공휴일 포함): weekday_off_code** — 요일과 무관하게 쉰다.

    ★ 공휴일 규칙의 근거
      2026-08-17(광복절 대체휴일·월요일) 실측 — office 102243 의 **13개 병동
      고정근무자 28명 전원이 `O`** 였다(수술실 20명 포함 48명 전수). 즉 특정 병동
      관행이 아니라 고정근무자 공통이다. 이 처리가 없으면 그날이 평일로 잡혀
      `fixed_shift` 가 채워지고, 근무표 전체가 하루치 어긋난다(2026-08 실측:
      전체 셀 불일치 51건 중 20건이 8/17 하루에서 나왔다).

    ★ 솔버를 타는 간호사는 여기 오지 않는다 — `_split_fixed_nurses` 로 갈린
      고정근무자 전용이다. 3교대 병동은 공휴일에도 24시간 돌아가므로 그쪽에
      같은 규칙을 밀어 넣으면 안 된다.
    """
    days_in_month = calendar.monthrange(year, month)[1]
    month_start = date(year, month, 1)
    holiday_days = _kr_holidays_in_month(year, month) if holiday_off else set()
    if holiday_days:
        print(f"[Holiday] 공휴일 OFF 적용 — days={sorted(holiday_days)}")
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
            cur = month_start + timedelta(days=day_idx)
            weekday = cur.weekday()
            if weekday >= 5:
                is_sunday_weekly_off = weekday == 6 and weekly_off_enabled and not is_weekend_off
                shifts[day_idx] = sunday_code if is_sunday_weekly_off else weekday_off_code
            elif cur.day in holiday_days:
                shifts[day_idx] = weekday_off_code      # 공휴일 — 평일이어도 쉰다
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


def _read_fixed_wanted_rows(
    db: Session,
    nurse_ids: list[str],
    group_id: str,
    year: int,
    month: int,
) -> list[FixedWantedEntry]:
    """고정 근무자 몫의 확정 원티드(is_applied=True) 행을 읽는다(EI-9-0, 원문 그대로)."""
    rows: list[FixedWantedEntry] = []
    # (EI-13c) IN 목록 300개 단위 — 빈 목록은 지금처럼 한 문장(빈 IN)을 그대로 던진다.
    for chunk in (list(in_chunks(nurse_ids)) or [[]]):
        rows.extend(
            db.query(FixedWantedEntry)
            .filter(
                FixedWantedEntry.group_id == group_id,
                FixedWantedEntry.year == year,
                FixedWantedEntry.month == month,
                # MSSQL BIT: == True(=1) 사용. .is_(True) 는 'IS 1' 로 렌더돼 MSSQL 오류.
                FixedWantedEntry.is_applied == True,  # noqa: E712
                FixedWantedEntry.nurse_id.in_(chunk),
            )
            .all()
        )
    return rows


def _apply_fixed_wanted_rows(
    fixed_roster: dict[str, list[str]],
    entries: list[FixedWantedEntry],
    days_in_month: int,
) -> dict[str, list[str]]:
    """확정 원티드 행을 고정 근무자 스케줄에 덮어씌운다(DB 0, EI-9-0, 원문 그대로)."""
    for e in entries:
        nurse_id = str(e.nurse_id)
        row = fixed_roster.get(nurse_id)
        if not row or e.shift_date is None:
            continue
        day = e.shift_date.day
        if day <= 0 or day > days_in_month:
            continue
        day_idx = day - 1
        if day_idx >= len(row):
            continue
        code = str(e.shift_id or "").strip()
        if not code:
            continue
        row[day_idx] = code
        fixed_roster[nurse_id] = row
    return fixed_roster


def _overlay_fixed_roster_with_fixed_wanted(
    db: Session,
    fixed_roster: dict[str, list[str]],
    group_id: str,
    year: int,
    month: int,
) -> dict[str, list[str]]:
    """고정 근무자 스케줄에 확정 원티드(fixed_wanted_entries)를 덮어씌운다.

    고정 근무자는 솔버를 우회해 평일=fixed_shift 로 채워지므로, 수간호사가 확정한
    간호사별·날짜별 근무(예: 특정 평일 OFF)가 반영되지 않던 누락을 보정한다.
    is_applied=True 항목만, shift_date 의 일(day)을 인덱스로 shift_id 를 적용한다.

    ★ EI-9-0 — 행 읽기(`_read_fixed_wanted_rows`)·덮기(`_apply_fixed_wanted_rows`)로 쪼갰다.
      이 함수는 그 둘을 부르는 얇은 래퍼다(이름·서명·동작 불변) — 호출 자리(고정근무 병합
      자리, "확정 원티드(fixed_wanted_entries, is_applied=True) 오버레이" 주석 참고)는
      그대로 둔다. 고정근무 차감 블록은 이 래퍼 대신 두 조각을 따로 부른다
      (app/services/engine_input/fixed_cut.py).
    """
    if not fixed_roster:
        return fixed_roster
    nurse_ids = list(fixed_roster.keys())
    if not nurse_ids:
        return fixed_roster
    days_in_month = calendar.monthrange(year, month)[1]
    entries = _read_fixed_wanted_rows(db, nurse_ids, group_id, year, month)
    return _apply_fixed_wanted_rows(fixed_roster, entries, days_in_month)


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
    # 주말휴무(is_weekend_off)는 더 이상 컬럼에서 안 읽는다 — SSOT=nurse_weekendoff_period.
    try:
        if has_enabled_col:
            rows = db.execute(
                text(
                    "SELECT nurse_id, name, weekly_off_enabled, weekly_off_weekday "
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

    # 주말휴무 대상 = nurse_weekendoff_period(as-of month)에서 해석. 컬럼 미의존.
    from services.nurse_period_resolver import weekend_off_ids_asof
    _wids = weekend_off_ids_asof(
        db, [str(getattr(_r, "nurse_id", "")) for _r in rows], year, month
    )

    for r in rows:
        nurse_id = str(r.nurse_id)
        name = str(r.name)
        enabled = True
        if has_enabled_col:
            enabled = bool(getattr(r, "weekly_off_enabled", 0))
        # 주말 고정 휴무 대상일 때만 "주휴 요일은 주말"을 강제한다.
        is_weekend_off = nurse_id in _wids

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

    weekend_off_only = {str(r.nurse_id) for r in rows if str(r.nurse_id) in _wids}
    if weekend_off_only:
        print(f"[WeeklyOff] 주말 휴무 대상 간호사 수={len(weekend_off_only)}")
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


def _boundary_tail_from_cells(
    db: Session, group_id: str, cells: dict, nurse_id: str,
    boundary_day: int, lookback: int, code2main: dict, year: int, month: int,
) -> tuple[list[str], tuple[str, str] | None]:
    """월중 경계일(0부터) 앞 lookback 일의 상대 병동 근무와, 전월 근무표를 읽었으면 그 (schedule_id, 선택 기준).

    같은 달 칸은 상대 병동 근무표에서 이미 읽은 `cells`({날(0부터): 메인코드} — `_load_other_ward_cells`)를 쓴다. 경계일이 lookback
    보다 이르면 모자란 앞부분을 그 병동 전월 근무표에서 잇는다(EI-11 11q 리뷰 G7) — 같은 달 근무표만 보면 월초 1~5일 경계의 꼬리가
    짧아져, 전월부터 이어진 연속근무·N 블록(#1·#3·#4·#5)을 경계 칸이 못 막았다. 전월 근무표는 월경계와 같은 규칙(마감본 우선 ·
    없으면 최신 · 취소본 제외)으로 고른다. 전월 근무표가 없거나 그날 칸이 없으면 꼬리에 넣지 않는다('-' = 연속 끊김 — 월경계의
    '전월 근무표 없음'과 같은 규약)."""
    if boundary_day <= 0:
        return [], None
    tail = [cells.get(d, '-') for d in range(max(0, boundary_day - lookback), boundary_day)]
    missing = lookback - boundary_day
    if missing <= 0:
        return tail, None
    prev_year, prev_month = _get_prev_year_month(year, month)
    prev_sid, prev_basis = _query_schedule_ref_for_month(db, group_id, prev_year, prev_month)
    if not prev_sid:
        return tail, None
    prev_days = calendar.monthrange(prev_year, prev_month)[1]
    entries = (
        db.query(ScheduleEntry)
        .filter(ScheduleEntry.schedule_id == prev_sid, ScheduleEntry.nurse_id == nurse_id)
        .all()
    )
    daymap = {int(e.work_date.day): _normalize_to_main(e.shift_id, code2main) for e in entries}
    return [daymap.get(d, '-') for d in range(prev_days - missing + 1, prev_days + 1)] + tail, (prev_sid, prev_basis)


def _load_other_ward_cells(db: Session, year: int, month: int, code2main: dict, pairs: list[tuple[str, str]]) -> dict:
    """월중 경계의 상대 병동마다 **한 번** — 같은 달 근무표 고르기(마감본 > 최신(작성 중 포함) · 취소본 제외 — D34) · 그 병동
    근무코드 보강 · 경계 간호사 전원의 칸(한 번의 조회)을 읽는다(EI-11 11w-2). 경계마다 따로 읽던 N+1 을 없애, 합류·복귀·출발이
    같은 상대 병동이면 같은 읽기를 쓴다(출발 경계가 새 조회를 만들지 않는다).

    반환 {병동: {'sid', 'basis'('issued'|'latest'|'blank'), 'code2main', 'cells': {간호사: {날(0부터): 메인코드}}}} — 병동은 처음
    나온 순서. 근무코드 보강 실패는 삼키지 않는다(세부 코드가 '미인식 = 연속 끊김'으로 읽혀 경계 하드락이 빠진다 — 11q G5)."""
    wards: dict[str, dict] = {}
    for gid, nid in pairs:
        ward = wards.setdefault(str(gid), {"nids": [], "sid": None, "basis": "blank", "code2main": dict(code2main),
                                           "cells": {}})
        if str(nid) not in ward["nids"]:
            ward["nids"].append(str(nid))
    for gid, ward in wards.items():
        ward["sid"], ward["basis"] = _query_schedule_ref_for_month(db, gid, year, month)
        if not ward["sid"]:
            continue
        ward["code2main"] = _group_shift_code2main(db, gid, code2main)
        entries = (
            db.query(ScheduleEntry)
            .filter(ScheduleEntry.schedule_id == ward["sid"], ScheduleEntry.nurse_id.in_(ward["nids"]))
            .all()
        )
        for e in entries:
            ward["cells"].setdefault(str(e.nurse_id), {})[int(e.work_date.day) - 1] = _normalize_to_main(
                e.shift_id, ward["code2main"])
    return wards


class _StartBoundary(NamedTuple):
    """앞 경계 — 상대 병동 근무 뒤 이 병동이 이어 받는 첫날(`kind` 'join' 파견·병동이동 합류 · 'return' 파견 마치고 복귀)."""

    nurse_id: str
    group_id: str
    day: int
    kind: str


class _Departure(NamedTuple):
    """출발 경계 — 이 병동 마지막 날 다음 날 `day` 부터 상대 병동 칸 [day, away_end]. `returns_here` = 이 달 안에 돌아온다."""

    nurse_id: str
    group_id: str
    day: int
    away_end: int
    returns_here: bool


def _mid_month_starts(inbound: list, outbound: list, month_start: date, days_in_month: int) -> list[_StartBoundary]:
    """월중 앞 경계 — 인바운드 시작(월초 시작은 월경계 몫) · 아웃바운드 복귀(종료일이 이번 달 · 월 안 복귀만). 조회하지 않는다."""
    out = [_StartBoundary(str(a.nurse_id), str(a.source_group_id), (a.start_date - month_start).days, "join")
           for a in inbound if (a.start_date - month_start).days > 0]
    for a in outbound:
        end = a.end_date or a.expected_end_date
        if not end or end.year != month_start.year or end.month != month_start.month:
            continue  # 종료일 미정·이번 달 아님 → 이번 달 복귀 없음
        day = (end - month_start).days + 1  # 종료일 다음날부터 복귀
        if 0 < day < days_in_month:
            out.append(_StartBoundary(str(a.nurse_id), str(a.target_group_id), day, "return"))
    return out


def _departure_boundaries(inbound: list, outbound: list, month_start: date, days_in_month: int) -> list[_Departure]:
    """월중 출발 경계(EI-11 11w-2 · G3) — 조회하지 않는다.
      - 아웃바운드(이 병동 = source · 상대 있음): 출발일 = 시작일. 1일 출발(앞 경계가 전월 쪽)·다음 달 출발은 범위 밖이다.
        파견이고 월말 전에 끝나면 상대 칸은 종료일까지(그 뒤 이 병동 복귀), 병동이동·월말을 넘는 파견은 월말까지.
      - 인바운드 파견(이 병동 = target)이 이번 달 · 월말 전에 끝나면 다음 날 원 병동으로 돌아간다(상대 = 원 병동 · 월말까지).
        병동이동 인바운드는 돌아가지 않는다."""
    month_end = month_start + timedelta(days=days_in_month - 1)
    out = []
    for a in outbound:
        day = (a.start_date - month_start).days
        if day <= 0 or day >= days_in_month or not a.target_group_id:
            continue
        end = (a.end_date or a.expected_end_date) if a.reason == "파견" else None
        back = end is not None and end < month_end
        out.append(_Departure(str(a.nurse_id), str(a.target_group_id), day,
                              (end - month_start).days if back else days_in_month - 1, back))
    for a in inbound:
        end = a.end_date or a.expected_end_date
        if a.reason != "파견" or not end or end < month_start or end >= month_end:
            continue
        out.append(_Departure(str(a.nurse_id), str(a.source_group_id), (end - month_start).days + 1,
                              days_in_month - 1, False))
    return out


def _other_ward_ref(kind: str, nurse_id: str, group_id: str, year: int, month: int, schedule_id: str,
                    basis: str) -> dict:
    """경계·상한을 걸려고 읽은 상대 병동 근무표 한 벌(생성 결과 안내 — D38). basis 'latest' = 마감 전(작성 중) 근무표."""
    return {"kind": kind, "nurse_id": str(nurse_id), "group_id": str(group_id), "year": int(year), "month": int(month),
            "schedule_id": schedule_id, "basis": basis}


def _month_start_refs(refs: list[dict], blocked_by_nurse: dict, horizon: int) -> list[dict]:
    """월경계 꼬리를 상대 병동 전월 근무표에서 읽은 기록 가운데, 그 간호사가 월초 `horizon` 일 안에 이 병동에서 일하는 날이 있는
    것만 — 월중 합류처럼 월초가 모두 차단일이면 그 꼬리는 생성에 쓰이지 않는다(안내하지 않는다 — D38). 안내용이라 모양이
    어긋난 차단일 값은 차단일이 아닌 것으로 본다(안내를 줄이지 않는 쪽 · 생성을 멈추지 않는다)."""
    blocked_by_nurse = blocked_by_nurse if isinstance(blocked_by_nurse, dict) else {}
    out = []
    for ref in refs:
        days = blocked_by_nurse.get(ref["nurse_id"])
        blocked = {d for d in days if isinstance(d, int)} if isinstance(days, (list, set, tuple, frozenset)) else set()
        if any(d not in blocked for d in range(horizon)):
            out.append(ref)
    return out


#: 안내 문구의 '무엇을 맞췄나' — 두 병동 나이트 합산(#6)만이면 상한, 경계(합류·복귀·출발·1일 합류·1일 복귀)만이면 경계.
_DRAFT_NOTICE_WHAT = {(True, False): "경계를", (False, True): "월 나이트 상한을", (True, True): "경계와 월 나이트 상한을"}


def _other_ward_draft_notices(db: Session, refs: list[dict]) -> list[dict]:
    """경계·상한을 걸려고 읽은 상대 병동 근무표 가운데 마감 전(작성 중) 근무표를 병동·달마다 한 번 안내한다(EI-11 11w-2 · D34·D38).

    마감본이면 안내하지 않는다(D38). 작성 중 근무표는 나중에 바뀔 수 있어 이 근무표도 다시 생성해야 맞기 때문이다. 성공 응답의
    `boundary_notices` 칸에 안내가 있을 때만 싣는다(D39 — `weekly_off_warnings` 에 두면 워커 Slack 품질 요약의 주휴 경고 수에
    섞인다 · 화면 표시는 프론트 몫 · D37). 병동 이름은 안내할 것이 있을 때만 한 번 읽는다."""
    drafts: dict[tuple[str, int, int], dict] = {}
    for ref in refs:
        if ref.get("basis") != "latest" or not ref.get("schedule_id"):
            continue
        item = drafts.setdefault((ref["group_id"], ref["year"], ref["month"]),
                                 {"schedule_id": ref["schedule_id"], "uses": []})
        use = {"kind": ref["kind"], "nurse_id": ref["nurse_id"]}
        if use not in item["uses"]:
            item["uses"].append(use)
    if not drafts:
        return []
    names = _group_names(db, {gid for gid, _y, _m in drafts})
    out = []
    for (gid, year, month), item in drafts.items():
        kinds = {u["kind"] for u in item["uses"]}
        what = _DRAFT_NOTICE_WHAT[(bool(kinds - {"night_sum"}), "night_sum" in kinds)]
        name = names.get(gid) or gid
        out.append({"type": "boundary_other_ward_reference",
                    "detail": f"{name} {month}월 근무표(작성 중 · 마감 전)를 기준으로 {what} 맞췄습니다 — "
                              "그 근무표가 바뀌면 이 근무표도 다시 생성해야 맞습니다.",
                    "group_id": gid, "group_name": name, "year": year, "month": month,
                    "schedule_id": item["schedule_id"], "schedule_status": "draft", "uses": item["uses"]})
    return out


def _group_names(db: Session, group_ids: set[str]) -> dict[str, str]:
    """병동 id → 이름(안내 문구용 · 한 번의 조회). 이름이 없는 병동은 안내가 병동 id 로 나간다."""
    rows = db.query(Group).filter(Group.group_id.in_(sorted(group_ids))).all()
    return {str(g.group_id): g.group_name for g in rows}


def _assignments_with_day1_returns(db: Session, group_id: str, year: int, month: int) -> tuple[list, list]:
    """(이번 달 배정, 1일 복귀 배정)을 **한 번의 조회**로 나눠 돌려준다(EI-11 11w-1 · D35).

    조건은 `assignment_service.get_active_assignments_for_month` 와 같고(status active·completed · 시작일 ≤ 월말 · 이 병동이
    source 또는 target) 실효 종료일(`end_date` 없으면 `expected_end_date`)만 '≥ 월초 − 1일'로 넓힌다 — 1일 복귀를 따로 조회하면
    조회가 하나 늘어 골든 쿼리 순서가 깨진다(설계 실측). assignment_service 는 쓰기 금지라 조건을 여기 복제한다(같은 행·같은
    순서는 시험이 본다).
      - 이번 달 배정: 실효 종료일이 없거나 월초 이후 — 원 함수와 같은 행·같은 순서.
      - 1일 복귀: 실효 종료일이 전월 말일인 이 병동 → 다른 병동 파견(그 간호사는 이달 1일에 이 병동으로 돌아온다).
    """
    from db.models import NurseAssignment as _NA
    from sqlalchemy import case

    month_start = date(year, month, 1)
    month_end = date(year, month, calendar.monthrange(year, month)[1])
    prev_end = month_start - timedelta(days=1)
    eff_end = case((_NA.end_date.isnot(None), _NA.end_date), else_=_NA.expected_end_date)
    rows = (
        db.query(_NA)
        .filter(
            _NA.status.in_(["active", "completed"]),
            _NA.start_date <= month_end,
            or_(eff_end.is_(None), eff_end >= prev_end),
            or_(_NA.source_group_id == group_id, _NA.target_group_id == group_id),
        )
        .all()
    )
    this_month, returns = [], []
    for a in rows:
        end = a.end_date if a.end_date is not None else a.expected_end_date
        if end is None or end >= month_start:
            this_month.append(a)
        elif _is_day1_return(a, end, prev_end, group_id):
            returns.append(a)
    return this_month, returns


def _is_day1_return(a, end: date, prev_end: date, group_id: str) -> bool:
    """전월 말일에 끝난 이 병동 → 다른 병동 파견인가(그 간호사는 이달 1일에 이 병동으로 돌아온다)."""
    return (end == prev_end and a.reason == "파견" and str(a.source_group_id) == str(group_id)
            and bool(a.target_group_id) and str(a.target_group_id) != str(group_id) and a.start_date <= prev_end)


def _supplement_return_tails(
    db: Session, year: int, month: int, last_map: dict, ret_src: dict, ext: int, code2main: dict,
    own_days: list[int] | None, nurse_ids: list[str], reads: dict | None = None, maps: dict | None = None,
) -> list[dict]:
    """1일 복귀 간호사(`_return_source_map`)의 전월 꼬리에서 파견 기간 날을 상대 병동 전월 근무표 칸으로 채운다(EI-11 11w-1 · D35).
    읽은 상대 병동 전월 근무표를 간호사마다 돌려준다(생성 결과 안내 — EI-11 11w-2 · D38).

    이 병동 전월 근무표에는 파견 기간 칸이 없어(차단일 — '-' 는 저장하지 않는다) 꼬리가 끊겼고, 상대 병동에서 이어진 연속근무·
    N 블록·전이(#1~#5)가 월경계에 안 걸렸다(11q 점검 G4). 꼬리는 전월 말일로 끝나는 `ext` 일이다 — 파견 시작일 앞은 이 병동 칸
    (`own_days` = `_get_last_days_map` 이 고른 꼬리 날짜), 파견 시작일부터는 상대 병동 칸을 쓴다. 상대 병동 전월 근무표는 월경계와
    같은 규칙(`_query_prev_month_schedule_id` — 마감본 우선 · 없으면 최신 · 취소본 제외)으로 고른다. 그 근무표가 없거나 그날 칸이
    없으면 이 병동 칸을, 그것도 없으면 '-'(꼬리 없음 — 이 병동 전월 근무표가 없을 때와 같은 규약 · G7 과 같다). 이 병동에 파견
    기간 칸이 있는 옛 근무표(원 병동이 파견 기간을 만들어 넘기던 때)에서 11q 까지 걸리던 칸을 지우지 않기 위해서다(완화 0).
    상대 병동마다 한 번씩만 읽는다(근무코드 맵은 `maps` 캐시 — 1일 합류 보충과 같이 쓴다 · EI-13 12u-2). 조회 실패는 삼키지
    않는다(G5)."""
    py, pm = _get_prev_year_month(year, month)
    prev_days = calendar.monthrange(py, pm)[1]
    window = range(max(1, prev_days - ext + 1), prev_days + 1)
    wanted = {str(n) for n in nurse_ids}
    by_target: dict[str, list[tuple[str, date]]] = defaultdict(list)
    for nid, (target, start_iso) in ret_src.items():
        if str(nid) in wanted:
            by_target[str(target)].append((str(nid), date.fromisoformat(str(start_iso)[:10])))
    refs: list[dict] = []
    for target, rows in by_target.items():
        sid, basis, cells = _prev_month_cells_of(db, target, year, month, [nid for nid, _s in rows], code2main, reads=reads,
                                                     maps=maps)
        for nid, start in rows:
            own = dict(zip(own_days or (), last_map.get(nid) or (), strict=False))
            away = cells.get(nid, {})
            last_map[nid] = [(away.get(day) if date(py, pm, day) >= start else None) or own.get(day, '-') for day in window]
            print(f"[CrossMonth][Return] nurse={nid}: 1일 복귀 — 상대({target}) 전월 근무표 {sid or '없음'} · "
                  f"파견 시작 {start.isoformat()} → 꼬리 {' '.join(last_map[nid])}")
            if sid:
                refs.append(_other_ward_ref("return_day1", nid, target, py, pm, sid, basis))
    return refs


def _memo_read(reads: dict | None, key: tuple, loader, plain=None):
    """읽기 메모(EI-13a). `reads` 가 None 이면 `loader()` 를 그대로 부른다(지금과 같다 — 조회 그대로).
    있으면 `key` 가 있을 때 조회 없이 깊은 사본을 돌려주고, 없으면 `loader()` → `plain(값)`(ORM 행을 평범한 값으로) 으로 담은 뒤
    깊은 사본을 돌려준다. 담긴 값은 시도마다 다시 계산하는 쪽이 읽기만 한다 — 돌려받은 값을 고쳐도 메모에 안 닿는다.
    `loader` 예외는 담지 않고 그대로 올린다."""
    if reads is None:
        return loader()
    if key not in reads:
        value = loader()
        reads[key] = plain(value) if plain is not None else value
    return deepcopy(reads[key])


def _prev_month_cells_of(db: Session, group_id: str, year: int, month: int, nurse_ids: list[str],
                         code2main: dict, reads: dict | None = None, maps: dict | None = None) -> tuple[str | None, str, dict]:
    """그 병동 전월 근무표(월경계와 같은 선택 — 마감본 우선 · 없으면 최신 · 취소본 제외)에서 간호사들의 칸 —
    (schedule_id, 선택 기준, {간호사: {일(1부터): 메인코드}}). 근무표가 없으면 (None, 'blank', {}). 근무코드는 그 병동 정의·슬롯으로
    읽는다(`_group_shift_code2main` — `maps` 캐시 · 실패는 삼키지 않는다).
    `reads` 를 주면 반환 전체를 메모한다(EI-13a — 입력 `code2main` 은 시도마다 같다)."""
    def _load():
        sid, basis = _query_schedule_ref_for_month(db, group_id, *_get_prev_year_month(year, month))
        if not sid:
            return None, basis, {}
        group_map = _group_shift_code2main(db, group_id, code2main, maps, reads)
        entries = (
            db.query(ScheduleEntry)
            .filter(ScheduleEntry.schedule_id == sid, ScheduleEntry.nurse_id.in_(nurse_ids))
            .all()
        )
        cells: dict[str, dict[int, str]] = defaultdict(dict)
        for e in entries:
            cells[str(e.nurse_id)][int(e.work_date.day)] = _normalize_to_main(e.shift_id, group_map)
        return sid, basis, cells

    return _memo_read(reads, ("prev_cells", str(group_id), year, month, tuple(nurse_ids)), _load,
                      lambda v: (v[0], v[1], {k: dict(c) for k, c in v[2].items()}))


def _read_schedule_entries(db: Session, schedule_id: str, active_nurse_ids: list[str] | None,
                           reads: dict | None = None) -> list[tuple]:
    """근무표 칸을 (nurse_id, 일, shift_id) 로 읽는다 — 꼬리 계산의 읽기 절반(EI-13a). `reads` 를 주면 메모한다."""
    def _load():
        # (EI-13c) IN 목록 300개 단위 — None(전원)은 필터 없는 한 문장, 빈 목록은 지금처럼 빈 IN 한 문장.
        if active_nurse_ids is None:
            chunks: list[list[str] | None] = [None]
        else:
            chunks = list(in_chunks(active_nurse_ids)) or [[]]
        out: list[tuple] = []
        for chunk in chunks:
            q = db.query(ScheduleEntry).filter(ScheduleEntry.schedule_id == schedule_id)
            if chunk is not None:
                q = q.filter(ScheduleEntry.nurse_id.in_(chunk))
            out.extend((e.nurse_id, int(e.work_date.day), e.shift_id) for e in q.all())
        return out

    return _memo_read(reads, ("entries", schedule_id, None if active_nurse_ids is None else tuple(active_nurse_ids)), _load)


def _get_last_days_map(db: Session, schedule_id: str, days: int, code2main: dict, active_nurse_ids: list[str] | None = None,
                       meta: dict | None = None, reads: dict | None = None) -> dict:
    """해당 schedule_id의 마지막 N일 근무코드를 메인코드로 정규화하여 반환한다.
    반환: { nurse_id: ['E','N','O','D','N','O'] } (최대 길이 days, 과거→현재 순)
    `meta` 를 주면 꼬리 칸의 날짜(1부터)를 `meta['tail_days']` 에 담는다(1일 복귀 꼬리 보충 — EI-11 11w-1).
    """
    if not schedule_id:
        print(f"[CrossMonth] 이전 달 스케줄 ID 없음, 빈 맵 반환")
        return {}
    # 해당 스케줄의 모든 엔트리 로딩
    if active_nurse_ids is not None:
        print(f"[CrossMonth] 비활성 간호사 이전달 데이터 제외: active {len(active_nurse_ids)}명 기준 필터")
    entries = _read_schedule_entries(db, schedule_id, active_nurse_ids, reads)
    by_nurse: dict[str, dict[int, str]] = defaultdict(dict)
    max_day = 0
    for _nid, d, _sid in entries:
        max_day = max(max_day, d)
        by_nurse[_nid][d] = _normalize_to_main(_sid, code2main)
    # 꼬리 days일만 취득
    result = {}
    start = max(1, max_day - days + 1)
    tail_days = list(range(start, max_day + 1))
    if meta is not None:
        meta["tail_days"] = tail_days
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
            'night_last_k': None,
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
    # 꼬리에서 마지막 N 이후 지난 날수(마지막 날이 N 이면 0) — N 블록 간격 하한의 월경계용.
    #   `offs_after_tail_nights` 는 'N 뒤가 전부 OFF' 일 때만 세고 2 로 잘려서
    #   `N N O E` 같은 꼬리의 블록 끝 위치를 복원할 수 없다.
    night_last_k = None
    for _k, c in enumerate(reversed(seq)):
        if c == 'N':
            night_last_k = _k
            break
    return {
        'consecutive_work_tail': cons_work,
        'consecutive_night_tail': cons_n,
        'consecutive_off_tail': cons_off_tail,
        'last_day_shift': last,
        'offs_after_tail_nights': min(2, offs_after_n),
        'night_last_k': night_last_k,
    }

def build_cross_month_constraints(db: Session, req: RosterRequest, current_user, shift_manage_data, config_dict: dict, nurse_ids: list[str], fixed_by_nurse: dict | None = None,
                                 reads: dict | None = None) -> dict:
    """이전 달 꼬리 패턴을 기반으로 강제 OFF/금지 셀을 생성한다.

    fixed_by_nurse = {nurse_id: {날: 메인코드}} (확정 원티드·특수 근무 — `_fixed_main_by_nurse`). 확정 칸이 월경계 영향 구간에
    있으면 그 칸만 하드락 면제하고 엔진 칸의 의무를 다시 건다(EI-11 11w-3 · D32)."""
    print("이전 월 경계 제약 생성 시작…")
    enable = bool(config_dict.get('cross_month_hard_rules_enable', True))
    lookback = int(config_dict.get('cross_month_lookback_days', 6))
    if not enable or lookback <= 0:
        print("이전 월 경계 제약 비활성화 또는 조회일수 0")
        return {'forced_off': {}, 'forbidden': {}, 'prev_month_n_offs_after': {}}

    # 코드 정규화 맵 구성
    code2main = {}
    for r in (shift_manage_data or []):
        main = r.get('main_code')
        for c in (r.get('codes') or []):
            code2main[str(c).upper()] = main
    code2main['O'] = 'O'; code2main['OFF'] = 'O'; code2main['주'] = 'O'

    # Shift 테이블의 shift_id → 메인코드 매핑 보강
    # ShiftManage.codes에 없는 세부 근무코드(NT, A, VY, OO 등)를 정규화하기 위함
    # 판정(① shift_gb ② default_shift ③ type)은 상대 병동 코드 읽기와 같은 한 곳 — `_shift_def_main`(EI-13 12u)
    try:
        all_shifts = _memo_read(
            reads, ("shift_rows",),
            lambda: db.query(Shift).filter(
                Shift.group_id == current_user.group_id,
                Shift.office_id == current_user.office_id,
            ).all(),
            lambda rows: [SimpleNamespace(shift_id=s.shift_id, shift_gb=getattr(s, "shift_gb", None),
                                          default_shift=getattr(s, "default_shift", None), type=getattr(s, "type", None))
                          for s in rows],
        )
        _sup_cnt = 0
        for s in all_shifts:
            sid = str(s.shift_id).strip().upper()
            if not sid or sid in code2main:
                continue
            main, _by_type = _shift_def_main(s)
            if main is None:
                continue
            code2main[sid] = main
            _sup_cnt += 1
            if main == "WK":
                print(f"[CrossMonth][WARN] shift_id={sid}: shift_gb/default_shift 미설정, type=근무 → 'WK' 폴백 (데이터 확인 권장)")
        if _sup_cnt > 0:
            _extra = {k: v for k, v in code2main.items() if k not in ('D', 'E', 'N', 'M', 'O', 'OFF', '주')}
            print(f"[CrossMonth] Shift 테이블에서 {_sup_cnt}건 추가 코드 정규화 보강: {_extra}")
    except Exception as e:
        # 보강 없이 가면 세부 근무코드(NT 등)가 '미인식 = 연속 끊김'으로 읽혀 꼬리 하드락이 빠진다 — 막는 쪽(EI-11 11q G5).
        print(f"[CrossMonth] Shift 테이블 보강 실패 — 생성 중단: {type(e).__name__}: {e}")
        raise

    # 이전 달 최신 스케줄 조회 → 마지막 N일 시퀀스
    try:
        prev_sid = _memo_read(reads, ("prev_sid",),
                              lambda: _query_prev_month_schedule_id(db, current_user.group_id, req.year, req.month))
        prev_year, prev_month = _get_prev_year_month(req.year, req.month)
        print(f"[CrossMonth] 이전 달 조회: {prev_year}년 {prev_month}월, schedule_id={prev_sid}, lookback={lookback}일")
    except Exception as e:
        print("[ERR] _query_prev_month_schedule_id:", e)
        raise
    try:
        # ★ N 간격 하한(월경계)은 전월 마지막 N 까지의 거리가 필요한데, 간격이 조회일수보다
        #   길면 그 N 이 꼬리 밖에 있어 월경계 제약이 조용히 빠진다(Codex 지적 · gap 8+).
        #   조회는 둘 중 긴 쪽으로 한 번 하고, 기존 지표에는 아래에서 **마지막 lookback 일만**
        #   잘라 넘긴다 — 연속근무·OFF 꼬리 등 기존 판정은 그대로다.
        #   간격은 모델과 같은 유효값(env 주입 포함)으로 잰다(Codex 지적).
        from services.cp_sat.fallback_lex import effective_n2n_min_gap
        _ext_lookback = max(lookback, effective_n2n_min_gap(config_dict.get("n2n_min_gap", 0)))
        _tail_meta: dict = {}   # 꼬리 칸 날짜 — 1일 복귀 보충이 이 병동 칸을 날짜로 맞춘다(EI-11 11w-1)
        last_map = _get_last_days_map(db, prev_sid, _ext_lookback, code2main, nurse_ids, meta=_tail_meta, reads=reads) if prev_sid else {}
        print(f"[CrossMonth] 이전 달 꼬리 패턴 조회 완료: {len(last_map)}명")
    except Exception as e:
        print("[ERR] _get_last_days_map:", e)
        raise

    # 꼬리를 상대 병동 전월 근무표에서 읽은 기록(생성 결과 안내 — EI-11 11w-2 · D38)
    _ow_refs: list[dict] = []
    # 상대 병동 근무코드 맵(병동 → 맵) — 1일 합류·1일 복귀 보충이 같은 병동을 다시 읽지 않는다(EI-13 12u-2)
    _ow_maps: dict[str, dict] = {}
    # ── 인바운드 간호사 tail 보충: target 전월 schedule에 없으면 source group에서 조회 ──
    _inbound_src_map = config_dict.get("_inbound_source_map")
    if _inbound_src_map:
        _missing = [nid for nid in _inbound_src_map if nid not in last_map]
        if _missing:
            _src_groups: dict[str, list[str]] = {}
            for nid in _missing:
                _src_groups.setdefault(_inbound_src_map[nid], []).append(nid)
            for src_gid, nids in _src_groups.items():
                _src_sid, _src_basis = _memo_read(
                    reads, ("sched_ref", str(src_gid), req.year, req.month),
                    lambda g=src_gid: _query_schedule_ref_for_month(db, g, *_get_prev_year_month(req.year, req.month)))
                if _src_sid:
                    # 그 병동 근무표 칸은 그 병동 근무코드 정의·슬롯으로 읽는다(EI-13 12u·12u-2 — 이 병동 맵만 쓰면 같은 코드의 다른
                    #   뜻·그 병동에만 있는 특수 근무·슬롯에만 정의된 코드를 놓친다)
                    _src_map = _get_last_days_map(
                        db, _src_sid, _ext_lookback, _group_shift_code2main(db, src_gid, code2main, _ow_maps, reads), nids,
                        reads=reads)
                    for nid, seq in _src_map.items():
                        last_map[nid] = seq
                    _ow_refs += [_other_ward_ref("join_day1", nid, src_gid, *_get_prev_year_month(req.year, req.month),
                                                 _src_sid, _src_basis) for nid in nids]
                    print(
                        f"[CrossMonth][Inbound] source({src_gid}) 전월 tail 보충: "
                        f"{len(_src_map)}명/{len(nids)}명"
                    )
                else:
                    print(f"[CrossMonth][Inbound] source({src_gid}) 전월 schedule 없음, tail 생략")

    # ── 1일 복귀(전월 말일에 끝난 파견) 꼬리 보충: 파견 기간 날을 상대 병동 전월 근무표로(EI-11 11w-1 · D35) ──
    #   이후 지표·칸 계산은 그대로 — 전월 N 꼬리·연속근무·전이·`prev_month_*` 가 다른 간호사와 같은 규칙으로 걸린다.
    _return_src_map = config_dict.get("_return_source_map")
    if _return_src_map:
        _ow_refs += _supplement_return_tails(db, req.year, req.month, last_map, _return_src_map, _ext_lookback, code2main,
                                             _tail_meta.get("tail_days"), nurse_ids, reads=reads, maps=_ow_maps)

    # 기존 지표는 마지막 lookback 일만 본다(위 확장 조회 주석). 긴 꼬리는 N 간격 거리에만 쓴다.
    last_map_ext = last_map
    last_map = {k: list(v)[-lookback:] for k, v in last_map_ext.items()}

    prev_month_last_main: dict[str, str | None] = {}
    prev_month_n_tail: dict[str, int] = {}
    prev_month_n_offs_after: dict[str, int] = {}  # N tail 뒤 이미 소비된 OFF 수
    prev_month_last_n_k: dict[str, int] = {}  # 꼬리에서 마지막 N 이후 지난 날수(N 간격 하한 월경계용)
    prev_month_off_tail: dict[str, int] = {}
    off_window_constraints: dict[str, list[list[int]]] = {}
    for nurse_id, seq in last_map.items():
        last_code = seq[-1] if seq else None
        prev_month_last_main[nurse_id] = last_code
        # 꼬리 연속 OFF (4O 월경계 제약용)
        metrics = _calc_tail_metrics(seq)
        cons_off = metrics.get('consecutive_off_tail', 0)
        if cons_off > 0:
            prev_month_off_tail[nurse_id] = cons_off
    forced_off = defaultdict(list)
    forbidden = defaultdict(lambda: defaultdict(list))
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
        K = 5
    two_after_three = bool(config_dict.get('two_offs_after_three_nig'))
    two_after_two = bool(config_dict.get('two_offs_after_two_nig'))
    not_one_night = bool(config_dict.get("not_one_night", False))
    banned_E_to_D = bool(config_dict.get('banned_day_after_eve'))
    ban_e_to_d = bool(config_dict.get('ban_e_to_d', True))
    ban_n_to_d = bool(config_dict.get('ban_n_to_d', True))
    ban_n_to_e = bool(config_dict.get('ban_n_to_e', True))
    if not banned_E_to_D:
        ban_e_to_d = False
        ban_n_to_d = False
        ban_n_to_e = False
    L = 3 if bool(config_dict.get('three_seq_nig', False)) else 2  # D13 ①(EI-11 11b) — 정본만 읽는다(요청 별칭은 가드가 400)

    # 간호사별 is_weekend_off 정보 조회 (주말 휴무 대상자 필터링용)
    weekend_off_nurse_ids: set[str] = set()
    try:
        # 주말휴무 SSOT = nurse_weekendoff_period (as-of month). nurses.is_weekend_off 컬럼 미의존.
        from services.nurse_period_resolver import weekend_off_ids_asof
        weekend_off_nurse_ids = _memo_read(reads, ("weekend_ids", tuple(nurse_ids)),
                                           lambda: weekend_off_ids_asof(db, nurse_ids, req.year, req.month), set)
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
        _nk = _calc_tail_metrics(last_map_ext.get(nurse_id, [])).get('night_last_k')
        if _nk is not None:
            prev_month_last_n_k[nurse_id] = int(_nk)
        
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

    # ── 확정 칸 조정(EI-11 11w-3 · G2 · D32) — 확정 칸 자체는 면제, 엔진 칸은 꼬리 + 확정 칸을 이어 본 상태로 의무를 다시 건다 ──
    if fixed_by_nurse:
        _reconcile_cross_month_with_fixed(config_dict, last_map, fixed_by_nurse, nurse_ids, days_in_month, forced_off,
                                          forbidden, off_window_constraints, prev_month_n_tail, prev_month_n_offs_after)

    # 중복 제거/정렬
    forced_off = {k: sorted(set(v)) for k, v in forced_off.items()}
    forbidden = {k: {d: sorted(set(ss)) for d, ss in v.items()} for k, v in forbidden.items()}
    off_cnt = sum(len(v) for v in forced_off.values())
    forb_cnt = sum(len(ss) for v in forbidden.values() for ss in v.values())
    print(f"강제 OFF {off_cnt}건, 금지 셀 {forb_cnt}건 적용")
    return {
        'forced_off': forced_off,
        'forbidden': forbidden,
        'prev_month_last_main': prev_month_last_main,
        'prev_month_n_tail': prev_month_n_tail,
        'prev_month_n_offs_after': prev_month_n_offs_after,
        'prev_month_last_n_k': prev_month_last_n_k,
        'prev_month_off_tail': prev_month_off_tail,
        'off_window_constraints': off_window_constraints,
        'other_ward_refs': _month_start_refs(_ow_refs, config_dict.get("blocked_by_nurse_id") or {},
                                             min(lookback, days_in_month)),
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
    - 출발(EI-11 11w-2 · G3 · D34): 이 병동 마지막 날 → 상대 병동 첫날(파견·병동이동 출발 · 파견 온 간호사의 원 병동 복귀).
      상대 병동 같은 달 근무표가 있으면(마감본 우선, 없으면 작성 중) 그 첫 칸들에 맞춰 이 병동 마지막 날들에 전이 금지칸·
      연속근무 OFF 창·N 이어 붙이기 금지를 건다(`engine_input.boundary_fixed.departure_boundary`). 없으면 걸지 않는다.
    상대 병동마다 근무표 고르기·근무코드 보강·경계 간호사 칸을 한 번씩만 읽는다(`_load_other_ward_cells` — 경계마다 따로 읽던
    N+1 을 없애 출발 경계가 새 조회를 만들지 않는다). 경계·상한을 걸려고 읽은 상대 근무표는 `other_ward_refs` 로 돌려준다
    (생성 결과 안내 — D38). 읽은 상대 병동 근무코드 맵은 `code_maps`({병동: 맵})로 돌려준다 — #6 상대 병동 나이트 세기가 같은
    병동을 다시 읽지 않는다(EI-13 12u).
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
        K = 5
    L = 3 if bool(config_dict.get('three_seq_nig', False)) else 2  # D13 ①(EI-11 11b) — 정본만 읽는다(요청 별칭은 가드가 400)
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
    refs: list[dict] = []
    boundaries: list[dict] = []  # 확정 칸 조정용(EI-11 11w-3) — kind: forward(합류·복귀 꼬리) · departure(상대 병동 첫 칸)

    starts = _mid_month_starts(inbound_assignments, outbound_assignments or [], month_start, days_in_month)
    departures = _departure_boundaries(inbound_assignments, outbound_assignments or [], month_start, days_in_month)
    wards = _load_other_ward_cells(db, year, month, code2main, [(b.group_id, b.nurse_id) for b in (*starts, *departures)])

    # ── 합류(인바운드 시작)·복귀(아웃바운드 종료 다음 날): 상대 병동 근무 꼬리 → 경계일 제약 ──
    for b in starts:
        ward = wards[b.group_id]
        label = "[MidMonth]" if b.kind == "join" else "[MidMonth][Return]"
        if not ward["sid"]:
            print(f"{label} nurse={b.nurse_id}: 상대({b.group_id}) 근무표 없음(basis={ward['basis']}), window 생략")
            continue
        refs.append(_other_ward_ref(b.kind, b.nurse_id, b.group_id, year, month, ward["sid"], ward["basis"]))
        tail, prev = _boundary_tail_from_cells(db, b.group_id, ward["cells"].get(b.nurse_id, {}), b.nurse_id, b.day,
                                               lookback, ward["code2main"], year, month)
        if prev:
            refs.append(_other_ward_ref(b.kind, b.nurse_id, b.group_id, *_get_prev_year_month(year, month), *prev))
        if not tail:
            print(f"{label} nurse={b.nurse_id}: 상대 근무표에 데이터 없음, window 생략")
            continue
        boundaries.append({"kind": "forward", "nurse_id": b.nurse_id, "day": b.day, "seq": list(tail)})
        metrics = _calc_tail_metrics(tail)
        print(f"{label} nurse={b.nurse_id}: boundary=day{b.day}, tail=[{' '.join(tail)}], "
              f"cons_work={metrics['consecutive_work_tail']}, cons_n={metrics['consecutive_night_tail']}, "
              f"last={metrics['last_day_shift']}")
        compiled = _compile_boundary_overlap_constraints(
            nurse_id=b.nurse_id,
            boundary_day=b.day,
            days_in_month=days_in_month,
            cons_work=metrics['consecutive_work_tail'],
            cons_n=metrics['consecutive_night_tail'],
            last_shift=metrics['last_day_shift'],
            offs_after=metrics['offs_after_tail_nights'],
            K=K,
            L=L,
            two_after_two=two_after_two,
            two_after_three=two_after_three,
            ban_e_to_d=ban_e_to_d,
            ban_n_to_d=ban_n_to_d,
            ban_n_to_e=ban_n_to_e,
        )
        forced_off[b.nurse_id].extend(compiled['forced_off'])
        for d, codes in compiled['forbidden'].items():
            forbidden[b.nurse_id][d].extend(codes)
        off_window_constraints[b.nurse_id].extend(compiled['off_window_constraints'])

    # ── 출발: 상대 병동 첫 칸들 → 이 병동 마지막 날들 제약(EI-11 11w-2 · D34 — 상대 근무표가 있을 때만) ──
    rules = rules_from_config(config_dict)
    for b in departures:
        ward = wards[b.group_id]
        if not ward["sid"]:
            print(f"[MidMonth][Depart] nurse={b.nurse_id}: 상대({b.group_id}) 근무표 없음 → 출발 경계 생략")
            continue
        refs.append(_other_ward_ref("departure", b.nurse_id, b.group_id, year, month, ward["sid"], ward["basis"]))
        mine = ward["cells"].get(b.nurse_id, {})
        away = [mine.get(d, '-') for d in range(b.day, b.away_end + 1)]
        cells = departure_boundary(away, b.day, b.away_end, b.returns_here, days_in_month, rules)
        boundaries.append({"kind": "departure", "nurse_id": b.nurse_id, "day": b.day,
                           "seq": list(away) + ([HERE, HERE] if b.returns_here else [])})
        for d, codes in cells.forbidden.items():
            forbidden[b.nurse_id][d].extend(sorted(codes))
        if cells.windows:
            off_window_constraints[b.nurse_id].extend(cells.windows)
        print(f"[MidMonth][Depart] nurse={b.nurse_id}: depart=day{b.day}, 상대({b.group_id}·{ward['basis']}) 첫 칸 "
              f"[{' '.join(away[:6])}] → forbidden {dict(sorted((d, sorted(c)) for d, c in cells.forbidden.items()))} "
              f"windows {cells.windows}")

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
        'other_ward_refs': refs,
        'boundaries': boundaries,
        'code_maps': {gid: ward["code2main"] for gid, ward in wards.items() if ward["sid"]},
    }


def _build_code_to_main_map(shift_manage_data: list[dict] | None) -> dict[str, str]:
    """ShiftManage의 codes → main_code 정규화 맵을 만든다.
    상대 병동 슬롯도 이 함수로 읽는다(`_group_slot_main` — 두 병동이 같은 코드를 같은 뜻으로 · EI-13 12v · D52).

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


def _overlay_home_profile_asof(db, nurses, group_id, month_start) -> None:
    """home 간호사 grade/weekend_off/fixed_shift 를 대상월 period as-of 로 __dict__ 오버레이.

    생성기가 캐시(오늘값)가 아니라 '그 달 값'으로 돌도록 — allowed 가 per-day period 인 것과
    동일 원칙. 미래월을 미리 생성해도 미래발효 변경이 반영된다.
    - gap(그 달 구간 없음) → 캐시 유지(비회귀). 구간이 값 None 이면 그 None 을 적용(명시적 해제).
    - fixed_shift 는 호출부에서 _split_fixed_nurses 이전에 적용해야 분기 정합(여기선 값만 주입).
    - inbound 는 별도 경로(target_*/period)로 주입되므로 여기 대상 아님(home 만 전달할 것).
    """
    from datetime import timedelta as _td
    from services.nurse_period_resolver import fetch_periods, resolve_asof
    from db.models import NurseGradePeriod, NurseWeekendOffPeriod, NurseAllowedShiftPeriod
    _SENT = object()
    ids = [str(n.nurse_id) for n in nurses]
    if not ids:
        return
    nx = month_start + _td(days=1)
    gp = fetch_periods(db, NurseGradePeriod, ids, month_start, nx, group_id=group_id)
    wp = fetch_periods(db, NurseWeekendOffPeriod, ids, month_start, nx)
    ap = fetch_periods(db, NurseAllowedShiftPeriod, ids, month_start, nx)
    for n in nurses:
        nid = str(n.nurse_id)
        d = n.__dict__
        g = resolve_asof(gp.get(nid), month_start, "grade", default=_SENT)
        if g is not _SENT:
            d["grade"] = g
        w = resolve_asof(wp.get(nid), month_start, "weekend_off", default=None)
        if w is not None:
            d["is_weekend_off"] = bool(w)
        fx = resolve_asof(ap.get(nid), month_start, "fixed_shift", default=_SENT)
        if fx is not _SENT:
            d["fixed_shift"] = fx


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
    reads: dict | None = None,
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
            periods_by_nurse = _memo_read(
                reads, ("allowed_periods", tuple(nurse_id_to_allowed.keys()), year, month),
                lambda: fetch_periods(
                    db, NurseAllowedShiftPeriod,
                    list(nurse_id_to_allowed.keys()), month_start, month_end,
                ),
                lambda by: {nid: [SimpleNamespace(valid_from=r.valid_from, valid_to=r.valid_to,
                                                  allowed_shifts=r.allowed_shifts) for r in rows]
                            for nid, rows in by.items()},
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


def _fixed_main_by_nurse(fixed_cells, nurses, shift_manage_data, shift_defs) -> dict:
    """고정 칸(확정 원티드·특수 근무) → {nurse_id: {날: 메인코드 D/E/N/M/W/O}} (EI-11 11w-3 · D32).

    간호사 색인은 엔진과 같은 규칙(`_build_engine_nurse_index_map`) · 코드: O·OFF·주 또는 휴가·공가·주휴 type → O ·
    근무코드 맵 → 근무코드 정의(shift_gb → default_shift → type 근무 'W' · 휴가·공가·휴무 'O') → 모르는 코드는 근무 'W'
    (더하기만이라 보수적 — 근무로 보면 이어진 연속근무·회복을 더 지킨다)."""
    if not fixed_cells:
        return {}
    idx2nid = {i: nid for nid, i in _build_engine_nurse_index_map(nurses).items()}
    code2main = _build_code_to_main_map(shift_manage_data)
    defs: dict[str, str] = {}
    for row in shift_defs or []:
        sid = str(row.get("shift_id") or "").strip().upper()
        gb = str(row.get("shift_gb") or "").strip()
        ds = str(row.get("default_shift") or "").strip().upper()
        tp = str(row.get("type") or "").strip()
        if not sid:
            continue
        if gb in _SHIFT_GB_MAIN:
            defs[sid] = _SHIFT_GB_MAIN[gb]
        elif ds in ("D", "E", "N", "M", "O"):
            defs[sid] = ds
        elif ds in ("OFF", "주"):
            defs[sid] = "O"
        elif tp == "근무":
            defs[sid] = "W"
        elif tp in ("휴가", "공가", "휴무", "오프"):
            defs[sid] = "O"
    out: dict = {}
    for c in fixed_cells:
        nid, d = idx2nid.get(c.get("nurse_index")), c.get("day_index")
        if nid is None or d is None:
            continue
        raw = str(c.get("shift") or "").strip().upper()
        st = str(c.get("shift_type") or "").strip()
        if raw in ("O", "OFF", "주") or st in ("휴가", "공가", "주휴"):
            main = "O"
        else:
            main = code2main.get(raw) or defs.get(raw) or (raw if raw in ("D", "E", "N", "M") else "W")
        out.setdefault(str(nid), {})[int(d)] = main
    return out


def _reconcile_cross_month_with_fixed(config_dict, last_map, fixed_by_nurse, nurse_ids, days_in_month, forced_off,
                                      forbidden, off_window_constraints, prev_month_n_tail, prev_month_n_offs_after) -> None:
    """월경계 × 확정 칸 조정(EI-11 11w-3 · D32) — 결과를 forced_off·forbidden·off_window 에 더한다(더하기만).

    조정이 걸린 간호사는 엔진의 월경계 N 이월(`prev_month_n_offs_after` — 상한 가장자리·이월 회복·1일 1N 건너뜀)을 끄고
    (오프 2 로 간주) 명시 칸이 대신한다 — 엔진 이월은 확정 칸 날도 회복 칸으로 세어 해가 없어지거나 면제 칸을 죽인다."""
    rules = rules_from_config(config_dict)
    blocked_all = config_dict.get("blocked_by_nurse_id") or {}
    for nurse_id in nurse_ids:
        fixed = fixed_by_nurse.get(str(nurse_id))
        if not fixed:
            continue
        blocked = {int(x) for x in (blocked_all.get(str(nurse_id)) or ())}
        rc = reconcile_forward(list(last_map.get(nurse_id, [])), 0, fixed, days_in_month,
                               set(range(days_in_month)) - blocked, rules)
        if rc is None:
            continue
        forced_off[nurse_id].extend(sorted(rc.forced))
        for d, codes in rc.forbidden.items():
            forbidden[nurse_id][d].extend(sorted(codes))
        if rc.windows:
            off_window_constraints.setdefault(nurse_id, []).extend(rc.windows)
        carry_off = prev_month_n_tail.get(nurse_id, 0) >= 1 and prev_month_n_offs_after.get(nurse_id, 0) < 2
        if carry_off:
            prev_month_n_offs_after[nurse_id] = 2
        print(f"[CrossMonth][Fixed] 간호사 {nurse_id}: 확정 칸 {fixed} → 강제 OFF {sorted(rc.forced)} "
              f"forbidden {dict(sorted((d, sorted(c)) for d, c in rc.forbidden.items()))} windows {rc.windows} "
              f"(엔진 월경계 N 이월 끔={carry_off})")


def _reconcile_mid_month_with_fixed(config_dict, boundaries, fixed_cells, nurses, shift_manage_data, special_shift_map,
                                    active_ranges, days_in_month, db=None, office_id=None, group_id=None) -> None:
    """월중 경계(합류·복귀 = 앞 경계 · 출발 = 뒤 경계) × 확정 칸 조정(EI-11 11w-3 · D32) — 표준 키에 더한다(더하기만).

    근무코드 정의는 월경계 경로와 같은 원천(그룹 shifts 전체)이어야 한다 — 특수 근무 맵은 default_shift 가 O 인 휴가류를 빼서
    확정 원티드 휴가를 근무(W)로 잘못 센다(11w-r). 맵·근무코드 맵으로 못 푸는 코드가 있을 때만 한 번 읽는다(없으면 조회 0)."""
    defs = [{"shift_id": k, "default_shift": v.get("default_shift"), "type": v.get("type")}
            for k, v in (special_shift_map or {}).items()]
    if db is not None and fixed_cells and boundaries:
        known = set(special_shift_map or {}) | set(_build_code_to_main_map(shift_manage_data)) | {"O", "OFF", "주", "D", "E", "N", "M"}
        if any(str(c.get("shift") or "").strip().upper() not in known
               and str(c.get("shift_type") or "").strip() not in ("휴가", "공가", "주휴") for c in fixed_cells):
            try:
                defs = [{"shift_id": getattr(s, "shift_id", None), "default_shift": getattr(s, "default_shift", None),
                         "shift_gb": getattr(s, "shift_gb", None), "type": getattr(s, "type", None)}
                        for s in _load_shift_lookup(db, office_id, group_id).values()]
            except Exception as e:
                print(f"[MidMonth][Fixed] 근무코드 정의 로드 실패(특수 근무 맵으로 계속 — 모르는 코드는 근무로 본다): {e}")
    fixed_by = _fixed_main_by_nurse(fixed_cells, nurses, shift_manage_data, defs)
    if not fixed_by:
        return
    rules = rules_from_config(config_dict)
    blocked_all = config_dict.get("blocked_by_nurse_id") or {}
    ic = config_dict.get("initial_constraints") if isinstance(config_dict.get("initial_constraints"), dict) else {}
    ow = config_dict.get("off_window_constraints") if isinstance(config_dict.get("off_window_constraints"), dict) else {}
    for b in boundaries:
        nid = str(b["nurse_id"])
        fixed = fixed_by.get(nid)
        rng = active_ranges.get(nid)
        if not fixed or not rng:
            continue
        active = set(range(rng[0], rng[1] + 1)) - {int(x) for x in (blocked_all.get(nid) or ())}
        if b["kind"] == "forward":
            rc = reconcile_forward(list(b["seq"]), int(b["day"]), fixed, days_in_month, active, rules)
        else:
            rc = reconcile_backward(list(b["seq"]), int(b["day"]), fixed, active, rules)
        if rc is None:
            continue
        if rc.forced:
            ic.setdefault("forced_off", {})[nid] = sorted(set(ic.get("forced_off", {}).get(nid, [])) | set(rc.forced))
        for d, codes in rc.forbidden.items():
            cur = ic.setdefault("forbidden", {}).setdefault(nid, {}).get(d, [])
            ic["forbidden"][nid][d] = sorted(set(cur) | set(codes))
        for w in rc.windows:
            if list(w) not in ow.setdefault(nid, []):
                ow[nid].append(list(w))
        print(f"[MidMonth][Fixed] nurse={nid} {b['kind']}@{b['day']}: 확정 칸 {fixed} → 강제 OFF {sorted(rc.forced)} "
              f"forbidden {dict(sorted((d, sorted(c)) for d, c in rc.forbidden.items()))} windows {rc.windows}")
    config_dict["initial_constraints"] = ic
    if ow:
        config_dict["off_window_constraints"] = ow


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


def _merge_off_window_constraints(base: dict | None, extra: dict | None) -> dict:
    """off_window_constraints(간호사 → OFF 창 목록)를 간호사별로 합친다 — 같은 창은 한 번, 순서는 base → extra.

    월경계(`build_cross_month_constraints`)와 월중 경계(`build_mid_month_boundary_constraints` — 파견·복귀)가 둘 다
    하드락 OFF 창을 낸다. 한쪽으로 다른 쪽을 덮으면 그 경계의 하드락(#1·#2·#4·#5)이 엔진에 닿지 않는다(EI-11 11r).
    dict 가 아닌 쪽과 list/tuple 이 아닌 창 목록·창은 건너뛴다(엔진의 창 정규화처럼 너그럽게 — 예외로 통째 잃지 않게).
    """
    merged: dict = {}
    for src in (base, extra):
        if not isinstance(src, dict):
            continue
        for nurse_id, windows in src.items():
            if not isinstance(windows, (list, tuple)):
                continue
            out = merged.setdefault(nurse_id, [])
            for window in windows:
                if isinstance(window, (list, tuple)) and list(window) not in out:
                    out.append(list(window))
    return merged


#: 엔진 스냅숏(`roster_system._effective_config_snapshot`)에 따로 싣는 월중 경계 몫(EI-11 11r). 탐색(UNDIAG·A1·MUS)은 그 스냅숏을
#: 기준으로 `_run_cp_sat_basic` 을 다시 부른다 — 스냅숏의 `initial_constraints`·`off_window_constraints` 는 월경계 몫까지 합친
#: 값이라 그대로 합치면 그 시도의 config(예: 연속근무 상한을 올린 탐색)로 다시 지어야 할 옛 월경계 칸이 되살아난다. 그래서
#: 합칠 것은 이 키의 월중 경계 몫뿐이다. 엔진 입력에는 실리지 않고(`_pop_mid_month_parts` 가 꺼낸다), 레지스트리
#: `RUNTIME_DATA_NOT_SETTINGS` 에 올라 있어 요청 `config_override` 로 보내면 가드가 400 으로 막는다.
_MID_MONTH_PARTS_KEY = "_mid_month_boundary_parts"


def _pop_mid_month_parts(config_dict: dict) -> tuple:
    """호출자 config 의 월중 경계 몫 `(initial_constraints, off_window_constraints)` 를 꺼낸다(EI-11 11r).

    엔진 스냅숏 기준 호출이면 따로 실은 몫(`_MID_MONTH_PARTS_KEY` — 키는 지운다), 아니면(생성 서비스 config — 첫 시도·사다리)
    표준 키 값이다. 서비스는 precheck 앞에서 표준 키에 월중 경계 몫만 심는다."""
    parts = config_dict.pop(_MID_MONTH_PARTS_KEY, None)
    if isinstance(parts, dict):
        return parts.get("initial_constraints"), parts.get("off_window_constraints")
    return config_dict.get("initial_constraints"), config_dict.get("off_window_constraints")


def _boundary_cells_for_engine(cross_month: dict, allowed: dict, banned: dict, mid_ic, mid_ow) -> tuple[dict, dict]:
    """엔진에 넣을 `(initial_constraints, off_window_constraints)` — 월경계(이 시도의 config 로 지은 것)·허용 시프트·금지 원티드
    (이 순서로 합집합)에 월중 경계 몫(파견·복귀 — 호출자)을 마지막에 합친다. OFF 창은 월경계 → 월중 경계 순으로 간호사별로
    합친다(EI-11 11r — 예전엔 월중 경계 금지칸·강제 OFF 를 버리고, 월경계 창이 있으면 월중 경계 창을 통째로 덮었다)."""
    merged = _merge_initial_constraints(base=_merge_initial_constraints(base=cross_month, extra=allowed), extra=banned)
    cross_ow = cross_month.get("off_window_constraints") if isinstance(cross_month, dict) else None
    return _merge_initial_constraints(base=merged, extra=mid_ic), _merge_off_window_constraints(cross_ow, mid_ow)


def _build_banned_wanted_constraints(db, current_user, req, nurses_in_group, swallowed: list | None = None) -> dict:
    """금지 원티드(banned_wanted) → initial_constraints forbidden 맵으로 변환.

    fixed_cells 경로가 아니다. {"forbidden": {nurse_id:{day_idx:[codes]}}} 를 반환하여
    _merge_initial_constraints 로 기존 forbidden 과 합집합된 뒤, 하위 파이프가
    initial_forbidden → m.Add(X==0) 로 하드 enforce 한다. fixed 셀과 겹치면 솔버가
    해당 셀 forbidden 을 skip 하므로 fixed 가 우선(모순 INFEASIBLE 없음).

    별도 사용 플래그 없이 항상 적용한다 — 저장된 is_applied 금지가 있으면 반영,
    없으면 빈 맵(무효과). 개별 on/off 는 entry 의 is_applied 로 제어.

    ★★ **source='hn' 만 읽는다 — 이게 확정 게이트다.**
      간호사가 원티드에서 낸 기피는 source='nurse' 로 들어와 조정판에 보이지만,
      수간호사가 조정판을 저장해 **hn 으로 승격**하기 전까지는 여기서 안 읽힌다.
      기피의 확정 선택자는 무조건 수간호사이기 때문이다
      (승격 지점: `wanted_service.save_banned_wanted_service`).
      미제출 draft 기피가 새던 문제도 이걸로 함께 막힌다 — 승격 자체가
      WantedRequest.is_submitted 를 보고 이뤄진다.
    """
    # 지연 import — wanted_service 는 이 모듈을 되짚어 참조한다(순환 방지).
    from services.wanted_service import BANNED_SOURCE_HN, banned_source_filter

    empty = {"forced_off": {}, "forbidden": {}}
    try:
        rows = db.query(BannedWantedEntry).filter(
            BannedWantedEntry.group_id == current_user.group_id,
            BannedWantedEntry.year == req.year,
            BannedWantedEntry.month == req.month,
            BannedWantedEntry.is_applied == True,  # noqa: E712
            banned_source_filter(BANNED_SOURCE_HN),
        ).all()
    except Exception as e:
        print(f"[BannedWanted] 조회 실패(무시): {e}")
        if swallowed is not None:
            swallowed.append(e)  # 빌더 경로(EI-13a): 삼킨 읽기 실패를 알려 AttemptSource 를 만들지 않게 한다
        return empty
    if not rows:
        return empty

    days_in_month = calendar.monthrange(req.year, req.month)[1]
    engine_nurse_ids = {str(getattr(n, "nurse_id", "")) for n in nurses_in_group}
    forbidden: dict[str, dict[int, list[str]]] = {}
    total = 0
    for r in rows:
        nid = str(r.nurse_id)
        if engine_nurse_ids and nid not in engine_nurse_ids:
            continue
        codes = r.banned_shift_ids
        if isinstance(codes, str):
            try:
                codes = json.loads(codes)
            except (ValueError, TypeError):
                codes = []
        codes = [str(c).strip().upper() for c in (codes or []) if str(c).strip()]
        if not codes:
            continue
        day_idx = r.shift_date.day - 1
        if day_idx < 0 or day_idx >= days_in_month:
            continue
        forbidden.setdefault(nid, {}).setdefault(day_idx, [])
        for c in codes:
            if c not in forbidden[nid][day_idx]:
                forbidden[nid][day_idx].append(c)
                total += 1

    if forbidden:
        print(f"[BannedWanted] 금지 셀 적용: nurses={len(forbidden)}, cnt={total}")
    return {"forced_off": {}, "forbidden": forbidden}


def _validate_mid_hard_feasibility(nurses_in_group, config_dict: dict, year: int, month: int) -> str | None:
    return _validate_mid_hard_feasibility_impl(
        nurses_in_group=nurses_in_group,
        config_dict=config_dict,
        year=year,
        month=month,
    )


# ── 해결책 탐색(probe) 진행 표시 ─────────────────────────────────────────────
#   인피저블 해결책 탐색은 재solve(=풀 생성)를 여러 번 돌린다. 각 재solve 가 fallback_lex
#   stage 로그를 다 찍어 로그가 폭주하므로, 재solve 내부 로그는 삼키고(_quiet_solve) 진행만
#   한 줄에 제자리(\r) 갱신한다(_probe_step). AIDE_PROBE_QUIET=0 이면 전체 로그 그대로(디버그).
def _probe_quiet_enabled() -> bool:
    import os as _o
    return _o.getenv("AIDE_PROBE_QUIET", "1") != "0"


def _probe_step(label: str, i: int, n=None) -> None:
    """진행을 한 줄에 제자리 갱신(\\r). 숫자만 업데이트되어 로그가 쌓이지 않는다."""
    if not _probe_quiet_enabled():
        return
    import sys as _s
    try:
        _out = getattr(_s, "__stdout__", None) or _s.stdout
        _tail = f"{i}/{n}" if n else f"{i}회"
        _out.write(f"\r[해결책 탐색] {label} {_tail} 확인 중…                    ")
        _out.flush()
    except Exception:
        pass


def _probe_done(msg: str = "") -> None:
    """진행 줄 마무리(개행) — 이후 결과 로그가 진행 줄을 덮지 않게 한다."""
    if not _probe_quiet_enabled():
        return
    import sys as _s
    try:
        _out = getattr(_s, "__stdout__", None) or _s.stdout
        _out.write("\r" + (msg or "") + " " * 20 + "\n")
        _out.flush()
    except Exception:
        pass


def _quiet_solve(fn):
    """probe 재solve 내부 stage 로그를 삼킨다(진행은 _probe_step 로 표시).

    AIDE_PROBE_QUIET=0 이면 로그 그대로 통과(디버그).
    """
    if not _probe_quiet_enabled():
        return fn()
    import io as _io2, contextlib as _cx2
    with _cx2.redirect_stdout(_io2.StringIO()):
        return fn()


def _quiet_verify_solve(fn):
    """per-nurse 재solve용: 내부 로그 억제 + verify-mode(FB_VERIFY_SKIP_STAGE3)로 feasibility 만
    빠르게 확인. per-nurse 블록은 probe_relaxations 밖이라 stage3(최적화)를 그대로 돌아 feasible
    케이스가 time_limit 까지 소비했다 → 병목 지목엔 feasible 여부만 필요하므로 stage3 를 스킵한다.
    """
    import os as _o
    _prev = _o.environ.get("FB_VERIFY_SKIP_STAGE3")
    _o.environ["FB_VERIFY_SKIP_STAGE3"] = "1"
    try:
        return _quiet_solve(fn)
    finally:
        if _prev is None:
            _o.environ.pop("FB_VERIFY_SKIP_STAGE3", None)
        else:
            _o.environ["FB_VERIFY_SKIP_STAGE3"] = _prev


def _probe_time_limit() -> int:
    """probe 재solve 1회의 시간 상한(초). 기본 20 — feasible 은 몇 초에 끝나고 infeasible 벽만
    짧아진다(생성 본solve 60s 와 분리). AIDE_PROBE_TIME_LIMIT 로 조절.
    """
    import os as _o
    try:
        return max(5, int(_o.getenv("AIDE_PROBE_TIME_LIMIT", "20")))
    except Exception:
        return 20


_CHANGE_LABEL_KO = {
    "weekend_off": "주말 휴무",
    "n_exact": "월 야간 고정(N)",
    "n_max": "월 야간 상한(N)",
}


def assert_night_caps_within_hard_lock(config_delta: dict | None, monthly_limit_release: list | None) -> None:
    """요청(해결 카드 재생성·직접 호출)의 `config_override`·`monthly_limit_release` 가 하드락을 완화하면 400.

    순서 고정(EI-11 11b) — 앞에서 걸리면 그 문구로 끝난다.
      ① 숫자 검사·#6(1인 월 15회) — 월 나이트 키 2개와 `monthly_limit_release`(`hard_lock.release_violations`).
      ② 월경계 하드락 스위치·엔진 내부 키(`boundary_switch_off` — '요청으로 끄기 금지', DECISIONS D17 추가 결정).
      ③ 요청으로 보낼 수 없는 런타임 키(`runtime_key_forbidden` — D13 ③ `max_consecutive_nights` · 서버가 계산해 엔진에
         넣는 내부 값 `RUNTIME_DATA_NOT_SETTINGS` — EI-11 11r).
      ④ 그 밖의 하드락 8종 완화(BLOCK) 전부. 인접·영향(relax_mark)은 통과한다.
    판정은 기준값 없는 절대값이다(Q1) — bool 하드락 키(#2·#4·#5·#7·#8)는 지금 값과 같은 False 여도 400 이다.
    정수 상한 키의 문자열·실수 값은 판정이 `int()` 로 읽어 통과시키고, 생성 서비스가 관문 뒤에서 그 정수로 바꿔 엔진에
    넘긴다(`_normalize_int_cap_override` — EI-11 11r).

    ★ 카드는 서버가 만들지만 적용 요청은 클라이언트가 그 값을 **그대로 echo** 한다 — 배포 전에 만든
      카드·조작된 요청이 하드락을 완화하는 값을 넣을 수 있다(OMC 리뷰 2026-10-02). 큐에 넣기 전(라우터)에
      막는다 — 1548b08 이후 입력 오류는 재시도 없이 FAILED 다 — 라우터에서 막는 이유는 큐 적재·프리셋
      포크 전에 끊기 위해서다. 생성 서비스 진입 관문(EI-11 H3)도 같은 함수를 부른다. 설정 화면 저장값은
      대상이 아니다.
    """
    from services.cp_sat.allowed_shift_types import HARD_MAX_NIGHTS_PER_MONTH as _hard
    from services.policy.hard_lock import release_violations

    def _num(v) -> int:
        try:
            return int(v)
        # 숫자가 아니면 500 이 아니라 400 — OverflowError 도 포함한다(FastAPI 가 JSON
        # Infinity 토큰을 그대로 받아들여 `int(float('inf'))` 가 터질 수 있다, Codex 리뷰 MEDIUM).
        except (TypeError, ValueError, OverflowError) as exc:
            raise HTTPException(status_code=400, detail=f"월 나이트 값이 숫자가 아닙니다: {v!r}") from exc

    over: list[str] = []
    for key in ("max_nig_per_month", "max_night_shifts_per_month"):
        val = (config_delta or {}).get(key)
        if val is not None and _num(val) > _hard:
            over.append(f"{key}={val}")
    for v in release_violations(monthly_limit_release):  # 카드 필터와 같은 #6 판정(입력 순서 그대로)
        if v.reason_code == "non_numeric":
            raise HTTPException(status_code=400, detail=f"월 나이트 값이 숫자가 아닙니다: {v.value!r}")
        over.append(f"{v.key}={v.value}")
    if over:
        raise HTTPException(status_code=400, detail=f"월 나이트는 1인 최대 {_hard}회(하드락)입니다: {', '.join(over)}")
    detail = _override_block_detail(config_delta)
    if detail is not None:
        raise HTTPException(status_code=400, detail=detail)


def _normalize_int_cap_override(config_override: dict | None) -> dict | None:
    """관문을 지난 `config_override` 의 정수 상한 하드락 키(레지스트리 `int_cap` 정본·별칭 — #1·#6) 값 가운데 문자열·실수를
    판정이 쓴 정수(`int()`)로 바꾼 사본을 돌려준다. 바꿀 것이 없으면 받은 dict 그대로다.

    판정(요청 가드 ①·④ · 카드 필터)은 `int()` 로 읽어 상한 안이면 통과시키는데 원값은 그대로 엔진까지 가 `'5'`·`5.0`·`5.9`
    에서 TypeError 가 났다(500 — 워커는 코드 결함으로 보고 재시도·DLQ, EI-11 11r 리뷰 LOW). 판정은 그대로 두고(P-1 — 가드
    400 ⇔ 카드 BLOCK) 엔진이 받는 값만 판정이 본 값과 같게 맞춘다. None(D3 — NULL 하드락 키의 엔진 의미 미결)과
    bool(int 하위형)은 건드리지 않는다. `int()` 가 실패하는 값은 관문이 이미 400 으로 막았다."""
    from services.engine_contract.settings import SETTINGS, HardLock

    if not isinstance(config_override, dict):
        return config_override
    keys = [key for spec in SETTINGS if isinstance(spec.hard_lock, HardLock) and spec.hard_lock.kind == "int_cap"
            for key in (spec.key, *spec.aliases)]
    fixed: dict = {}
    for key in keys:
        value = config_override.get(key)
        if isinstance(value, (str, float)):
            try:
                fixed[key] = int(value)
            except (TypeError, ValueError, OverflowError):
                continue  # 관문이 이미 400 으로 막는 값 — 여기서는 건드리지 않는다
    return {**config_override, **fixed} if fixed else config_override


def _override_block_detail(config_delta: dict | None) -> str | None:
    """요청 가드 ②~④ — `override_violations` 의 BLOCK 을 순서대로 본 400 문구(없으면 None · 인접·영향 표지는 무시).

    ② 월경계 스위치·엔진 내부 키 → ③ 런타임 금지 별칭(D13 ③) → ④ 그 밖의 하드락 8종 BLOCK. 앞에서 걸리면 그 문구다."""
    from services.policy.hard_lock import BLOCK, override_violations, rule_label

    violations = override_violations(config_delta or {})
    boundary = [v for v in violations if v.reason_code == "boundary_switch_off"]
    if boundary:
        return "월경계 하드락은 요청으로 끌 수 없습니다(하드락): " + ", ".join(f"{v.key}={v.value!r}" for v in boundary)
    runtime = [v for v in violations if v.reason_code == "runtime_key_forbidden"]
    if runtime:
        return " ".join(v.message_ko for v in runtime)
    rest = [v for v in violations if v.kind == BLOCK]
    if rest:
        return "하드락 규칙은 요청으로 완화할 수 없습니다: " + ", ".join(
            f"{rule_label(v.rule_no)} — {v.key}={v.value!r}" for v in rest)
    return None


def n_only_release_targets(db, group_id: str, year: int, month: int, monthly_limit_release: list | None) -> set[str]:
    """해결 카드의 월 야간 한도 변경(`monthly_limit_release`) 대상 중 **그 달 N전담**인 간호사.

    ★ 카드 생성은 N전담을 뺀다(96e4f86) — 하지만 적용 요청은 클라이언트가 카드 값을 echo 하므로,
      일반 근무자일 때 받은 카드를 그 사람이 N전담이 된 뒤 다시 보내면 N전담 고정 횟수(n_exact)가
      지워지고 즉시 커밋됐다(Codex 커밋 큐 3회차 HIGH). 판정은 카드 생성과 같은 `is_n_only_profile` 이되
      **그 달 기준** 근무형태(`group_members_in_month` 의 as_of — 파견 온 사람은 파견지 근무형태)로 본다.
    """
    from services.assignment_service import group_members_in_month
    from services.cp_sat.allowed_shift_types import is_n_only_profile

    want = {str(r.get("nurse_id")) for r in (monthly_limit_release or [])
            if isinstance(r, dict) and r.get("nurse_id") and r.get("field") in ("n_exact", "n_max", "n_min")}
    if not want or not group_id:
        return set()
    # ★ use_mid 는 **설정과 무관하게 False 로** 본다 — MID 를 빼고 N 만 남으면 N전담. 생성이 쓰는 설정
    #   (요청 config_id·모달 config·config_override)과 최신 설정이 달라 [N,M] 같은 프로필을 서로 다르게
    #   판정하면 보호를 비켜 갔다(Codex 커밋 큐 4회차 HIGH). use_mid=True 에서 N전담이면 False 에서도
    #   N전담이라 이쪽이 항상 넓다. 대가: MID 쓰는 병동의 N+M 근무자 카드도 막힌다(드물고 하드락 쪽이 안전).
    use_mid = False
    profile = {str(m["nurse_id"]): m.get("as_of_allowed_shifts")
               for m in group_members_in_month(db, group_id, int(year), int(month))["members"]
               if str(m["nurse_id"]) in want}
    for nid, raw in db.query(Nurse.nurse_id, Nurse.allowed_shifts).filter(
            Nurse.nurse_id.in_(list(want - set(profile)))).all():
        profile[str(nid)] = raw   # 그 달 명단에 없으면 지금 프로필로
    return {nid for nid, raw in profile.items() if is_n_only_profile(raw, use_mid=use_mid)}


def assert_release_not_n_only(db, group_id: str, year: int, month: int, monthly_limit_release: list | None) -> None:
    """N전담의 월 야간 한도를 바꾸려는 해결 카드 적용은 큐에 넣기 전에 400(N전담 횟수는 하드락·계약 값)."""
    bad = n_only_release_targets(db, group_id, year, month, monthly_limit_release)
    if bad:
        raise HTTPException(status_code=400, detail=f"N전담 간호사의 월 나이트 횟수는 해결 카드로 바꿀 수 없습니다: {sorted(bad)}")


def _mk_change(nurse_id, attr, frm, to):
    """resolution_options.changes[*] 항목. 프론트가 label_ko/config_key 로 라벨을 렌더하므로
    (attr 만 주면 'undefined 표시') 둘을 함께 채운다.

    boolean 은 true/false 대신 '적용/해제'로 표기하고, 생성을 막던 현재값(from)은 생략
    → 프론트가 목표만 렌더('주말 휴무 해제'). 숫자(야간 13→7)는 from/to 둘 다 유지.
    (changes 는 표시 전용 — 실제 적용은 weekend_off_release/monthly_limit_release 로.)"""
    from_val = None if isinstance(frm, bool) else frm
    to_val = ("적용" if to else "해제") if isinstance(to, bool) else to
    return {"nurse_id": nurse_id, "attr": attr, "config_key": attr,
            "label_ko": _CHANGE_LABEL_KO.get(attr, attr), "from": from_val, "to": to_val}


def _priority_families_from_presolve(presolve_diag) -> list:
    """presolve(max-flow) 부족 진단 → probe 우선 완화군(온톨로지 소프트정렬).

    N 부족이면 야간 규칙군을, D/E 부족이면 OFF/연속/팀군을 먼저 검증하도록 순서를 준다.
    probe 가 우선군에서 못 풀면 나머지도 폴백 검증하므로(soft), 지목이 틀려도 안전.
    presolve 없음/부족 없음이면 []=기존 동작(전체 순차 검증).
    """
    try:
        _pd = presolve_diag or {}
        shortages = _pd.get("shortages") or []
        pressure = _pd.get("pressure_families") or []
    except Exception:
        return []
    fams: list = []
    # 개인 제약 압박(제약모순형)을 최우선 — 커버리지 shortages 사각지대를 여기서 메운다.
    fams.extend(pressure)
    if not shortages and not pressure:
        return []
    shifts = {str(s.get("shift")) for s in shortages}
    reasons = {str(s.get("reason")) for s in shortages}
    if "N" in shifts:
        fams += ["night_cap", "night_recovery", "night_consecutive", "night_pattern", "transition"]
    if shifts & {"D", "E"}:
        fams += ["off_budget", "consecutive", "weekend_off", "team", "transition"]
    if "capacity_shortage" in reasons:
        fams += ["off_budget", "consecutive"]
    _seen: set = set()
    out: list = []
    for f in fams:
        if f not in _seen:
            _seen.add(f)
            out.append(f)
    return out


def build_team_min_by_team(
    team_rows,
    member_team_ids: set[str],
    use_mid: bool,
) -> dict[str, dict[str, int]]:
    """팀별 최소 인원(`team_min_by_team`)을 구성한다.

    ★★ **생성과 precheck 가 같은 함수를 써야 한다.** 예전엔 두 곳이 따로 만들었고, 생성만
      `teams.min_shift` 를 읽고 precheck 는 디폴트(D:1, E:1)만 넣었다. 그래서 저장값이
      더 엄격하면(예: N:1) precheck 를 통과하고 솔버에서 실패했고, 반대로 D 를 0 으로 둔
      팀은 **없는 제약으로 precheck 가 요청을 막았다.** 진단 차이가 아니라 정상 생성이
      차단되거나 성공을 잘못 예고하는 문제다.

    규칙:
      - `teams.min_shift` 가 저장돼 있으면 **그 값이 우선**이다.
      - 미설정 팀만 디폴트(D:1, E:1, N:0[, use_mid 면 M:0])로 채운다.
      - 0 은 무제약이라 제외한다 — 디폴트의 실효 제약은 팀별 D≥1·E≥1 이다.
      - **멤버가 배정된 팀에만** 적용한다. 인원 0 팀에 넣으면 precheck 가
        TEAM_SIZE_INSUFFICIENT 로 오블로킹한다.

    Args:
        team_rows: 활성 Team ORM 행.
        member_team_ids: 실제 간호사가 배정된 team_id 문자열 집합.
        use_mid: MID 근무코드 사용 여부.
    """
    default_min: dict[str, int] = {"D": 1, "E": 1, "N": 0}
    if use_mid:
        default_min["M"] = 0

    result: dict[str, dict[str, int]] = {}
    for t in team_rows:
        tid = str(t.team_id)
        if tid not in member_team_ids:
            continue
        raw = t.min_shift if isinstance(t.min_shift, dict) else None
        src = raw if raw else default_min
        cleaned: dict[str, int] = {}
        for k, v in src.items():
            try:
                iv = int(v or 0)
            except (TypeError, ValueError):
                continue
            if iv > 0:
                cleaned[k] = iv
        if cleaned:
            result[tid] = cleaned
    return result


def _call_time_config(latest_config) -> dict:
    """`config_override` 를 안 넘기던 탐색 호출(주말휴무·원인 분리 등)이 쓰던 값 — 호출 **시점**의 `latest_config.__dict__` 에서
    `_sa_*` 만 뺀 사본(EI-13a-2 ⓐ). 이전엔 `_run_cp_sat_basic` 안에서 `latest_config.__dict__.copy()` 로 같은 값을 읽었다
    (L1 canonical 이 `_sa_*` 를 빼므로 엔진에 닿는 값은 같다). 호출 시점에 부르는 것이 요점이다."""
    return {k: v for k, v in latest_config.__dict__.items() if not str(k).startswith("_sa_")} if latest_config else {}


def _run_cp_sat_basic(db: Session, current_user, nurses_in_group, preferences, latest_config, req, shift_manage_data, fixed_cells=None, time_limit_seconds=60, config_override: dict | None = None, _assignments=None, _inbound_assignments=None, _outbound_assignments=None, weekend_off_override: dict | None = None, attempt_src: AttemptSource | None = None):
    """cp_sat_basic 엔진 호출을 표준화한다."""
    cp_sat_result = None
    # 첫 시도 직전 빌더가 읽은 DB 부품(EI-13a-2) — 같은 명단용일 때만 재조회 대신 쓴다. 없으면 지금처럼 매번 읽는다.
    _src = attempt_src if (attempt_src is not None and attempt_src.matches(nurses_in_group)) else None
    try:
        nurses_dict = [n.__dict__ for n in nurses_in_group]
        # is_weekend_off는 ORM 컬럼 유무와 무관하게, DB에 컬럼이 있으면 직접 조회해서 엔진 입력에 주입한다.
        # - 이유: ORM 모델/스키마가 아직 확장되지 않은 환경에서도 fallback/하드 제약이 동작해야 한다.
        try:
            # 주말휴무 SSOT = nurse_weekendoff_period (as-of month). 컬럼 미의존, 엔진 입력에 주입.
            # weekend_off_override(per-nurse MCS 검증용): 지정 간호사만 값 덮어씀(1회성, DB 미변경).
            from services.nurse_period_resolver import weekend_off_ids_asof
            _wids = set(_src.weekend_ids) if _src is not None else weekend_off_ids_asof(
                db,
                [nd.get("nurse_id") or nd.get("db_id") for nd in nurses_dict],
                req.year, req.month,
            )
            for nd in nurses_dict:
                nid = str(nd.get("nurse_id") or nd.get("db_id") or "")
                if nid:
                    _v = nid in _wids
                    if weekend_off_override and nid in weekend_off_override:
                        _v = bool(weekend_off_override[nid])
                    nd["is_weekend_off"] = _v
        except Exception as e:
            print(f"[WeeklyOff] is_weekend_off 주입 실패(무시): {e}")
        # prefs_dict = [p.__dict__ for p in preferences]
        prefs_dict = preferences

        # 호출자가 구성한 config_dict(게이지 반영 등)이 있으면 이를 사용
        config_dict = (config_override.copy() if config_override is not None else (latest_config.__dict__.copy() if latest_config else {}))
        # 월중 경계(파견·복귀) 몫 — 아래 3) 에서 이 시도의 월경계·허용 시프트·금지 원티드와 합친다(EI-11 11r).
        _mid_ic, _mid_ow = _pop_mid_month_parts(config_dict)
        # 근무 우선(off_first=True)은 월 OFF 수를 하드로 보지 않는 모드라 개인 OFF 한도(o_*)도
        #   읽지 않는다(2026-10-12 사용자 결정). 사본으로 바꿔 간호사 객체(재시도가 공유)는 그대로 둔다.
        if bool(config_dict.get("off_first", False)):
            nurses_dict = [{**nd, "o_min": None, "o_max": None, "o_exact": None} for nd in nurses_dict]
        # ShiftManage 요구인원은 호출부에서 주입한다

        # teams.min_shift → config_dict["team_min_by_team"] 로 주입
        # 키는 team_id(str). 팀별 dict 가 비어있거나 NULL 이면 제외.
        try:
            team_rows = deepcopy(_src.team_rows) if _src is not None else (
                db.query(Team)
                .filter(
                    Team.office_id == current_user.office_id,
                    Team.group_id == current_user.group_id,
                    Team.active == 1,
                )
                .all()
            )
            team_handoff_policy_by_team: dict[str, dict] = {}
            # 멤버가 배정된 팀에만 team_min 적용 — 솔버(team_members)·precheck(members_by_team)와
            # 동일 기준. 구성 규칙은 build_team_min_by_team 에 한 곳으로 모여 있다(precheck 와 공유).
            _member_team_ids = {
                str(getattr(n, "team_id", None))
                for n in nurses_in_group
                if getattr(n, "team_id", None) not in (None, "", 0)
            }
            # ★★ 런타임에서 이미 들어온 값(치료·config_override)이 있으면 **그것이 이긴다.**
            #   예전엔 조건 없이 `teams.min_shift` 로 다시 만들어 덮어썼다. precheck 는
            #   반대로 값이 있으면 그대로 썼기 때문에, 팀 최소인원을 완화한 override 가
            #   **precheck 는 통과하고 솔버 직전에 버려져** 사전검증과 실제 생성이 또 갈렸다.
            #   (override 를 넣어도 안 먹는다는 뜻이기도 하다.)
            #   두 경로 모두 "없을 때만 저장값으로 구성" 으로 맞춘다.
            if "team_min_by_team" not in config_dict:
                team_min_by_team = build_team_min_by_team(
                    team_rows,
                    _member_team_ids,
                    bool(config_dict.get("use_mid", False)),
                )
                if team_min_by_team:
                    config_dict["team_min_by_team"] = team_min_by_team
            for t in team_rows:
                _tid = str(t.team_id)
                hp = t.handoff_policy if isinstance(t.handoff_policy, dict) else None
                if hp and isinstance(hp.get("restrictions"), list) and hp["restrictions"]:
                    team_handoff_policy_by_team[_tid] = hp
            if team_handoff_policy_by_team:
                config_dict["team_handoff_policy_by_team"] = team_handoff_policy_by_team
        except Exception as e:
            print(f"[TeamMin] teams.min_shift/handoff_policy 로딩 실패(무시): {e}")

        try:
            shift_defs = []
            if _src is not None:
                shift_defs = deepcopy(_src.shift_defs)
            else:
                shift_lookup = _load_shift_lookup(db, current_user.office_id, current_user.group_id)
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
        reads=dict(_src.allowed_reads) if _src is not None else None,
    )

    # ── 2) cross-month 경계 제약 생성 ──
    #   실패를 삼키면 월경계 하드락(#1·#2·#3·#4·#5·#7) 없이 근무표가 나간다(fail-open) — 생성을 멈춘다(EI-11 11q G5).
    try:
        cross_month_constraints = build_cross_month_constraints(
            db, req, current_user, shift_manage_data, config_dict, [n.nurse_id for n in nurses_in_group],
            fixed_by_nurse=_fixed_main_by_nurse(config_dict.get("fixed_cells"), nurses_in_group, shift_manage_data,
                                                config_dict.get("shift_definitions")),
            reads=dict(_src.cross_month_reads) if _src is not None else None,
        )
    except Exception as e:
        print(f"이전 월 경계 제약 생성 실패 — 생성 중단(월경계 하드락 없이 만들지 않는다): {type(e).__name__}: {e}")
        raise
    # 전월 마지막 근무코드: 월 경계 N-O-D / N-O-E / E-O-D 판정에 쓰인다.
    #   `last_is_off` 는 OFF 여부만 알려줘 "전월 마지막이 E 였다"를 복원할 수 없다.
    prev_month_last_main = cross_month_constraints.get("prev_month_last_main") or {}
    if prev_month_last_main:
        config_dict["prev_month_last_main"] = prev_month_last_main
    prev_month_n_tail = cross_month_constraints.get("prev_month_n_tail") or {}
    if prev_month_n_tail:
        config_dict["prev_month_n_tail"] = prev_month_n_tail
    prev_month_n_offs_after = cross_month_constraints.get("prev_month_n_offs_after") or {}
    if prev_month_n_offs_after:
        config_dict["prev_month_n_offs_after"] = prev_month_n_offs_after
    prev_month_last_n_k = cross_month_constraints.get("prev_month_last_n_k") or {}
    if prev_month_last_n_k:
        config_dict["prev_month_last_n_k"] = prev_month_last_n_k
    prev_month_off_tail = cross_month_constraints.get("prev_month_off_tail") or {}
    if prev_month_off_tail:
        config_dict["prev_month_off_tail"] = prev_month_off_tail

    # ── 3) 병합 후 주입(금지/강제OFF 합집합 · OFF 창은 간호사별 합집합) ──
    # 금지 원티드(banned_wanted) 를 forbidden producer 로 합류 (fixed_cells 경로 아님).
    banned_constraints = deepcopy(_src.banned) if _src is not None else _build_banned_wanted_constraints(
        db, current_user, req, nurses_in_group,
    )
    # 월중 경계(파견·복귀) 몫 `_mid_ic`·`_mid_ow` 는 함수 첫머리에서 호출자 config 에서 꺼냈다(`_pop_mid_month_parts`).
    #   예전엔 금지칸·강제 OFF 를 버리고 월경계 창이 있으면 월중 경계 창을 덮어, precheck 는 보는 월중 경계 하드락이
    #   엔진에 안 닿았다(EI-11 11r · 운영 main 에도 있던 결함). 키가 있던 config(스냅숏 기준 탐색)는 빈 창으로도 덮는다.
    config_dict["initial_constraints"], off_window_constraints = _boundary_cells_for_engine(
        cross_month_constraints, allowed_constraints, banned_constraints, _mid_ic, _mid_ow)
    if off_window_constraints or "off_window_constraints" in config_dict:
        config_dict["off_window_constraints"] = off_window_constraints
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
            soft_off_k = config_dict.get("max_conseq_off")
            soft_off_w = config_dict.get("max_conseq_off_penalty_weight")
            print(
                "[ShiftDistributionPolicy] "
                f"mode={mode}, oversupply_gauge={og}, monthly_pref_gauge={mg}, "
                f"oversupply_equalize=({oe},{ow}), monthly_pref_weight={mw}, "
                f"monthly_pref_cnt={mp_cnt}, "
                f"max_extra_off_days={max_extra}, extra_off_penalty_weight={off_pen}, "
                f"soft_cwork=(k={soft_k},w={soft_w}), "
                f"soft_coff=(k={soft_off_k},w={soft_off_w})"
            )
        except Exception as _log_exc:
            print(f"[ShiftDistributionPolicy] 로그 출력 실패: {_log_exc}")
        # [ALWAYS_COMBINED] 전략은 항상 COMBINED(team+grade 동시). 요청 바디 grade_strategy 는 무시.
        #   grade_config 는 항상 로드 — grade 제약 없으면 grade 항 자동 no-op, team 항은 team 데이터 있을 때만 활성.
        grade_config = deepcopy(_src.grade) if _src is not None else _fetch_grade_config_dict(
            db, current_user.office_id, current_user.group_id
        )
        engine_grade_config = grade_config
        if bool(config_dict.get("_force_grade_max_soft_fallback")) and isinstance(engine_grade_config, dict):
            engine_grade_config = dict(engine_grade_config)
            engine_grade_config["allow_soft_fallback"] = True
            print("[GradeFallback] force allow_soft_fallback=True (grade_max soft)")
        # [EI-2a] 엔진이 grade_strategy 를 "COMBINED" 로 하드고정한다(cp_sat_basic.py 의
        #   `setattr(roster_system, "grade_strategy", "COMBINED")` — [ALWAYS_COMBINED]).
        #   여기서 전달하던 config_dict["grade_strategy"] 기록·엔진 인자 grade_strategy= 는
        #   둘 다 그 하드고정 값과 같아 동작에 영향이 없는 죽은 전달이라 지웠다(verify).
        cp_sat_result = generate_roster_cp_sat(
            nurses_dict,
            prefs_dict,
            config_dict,
            req.year,
            req.month,
            shift_manage_data,
            time_limit_seconds=time_limit_seconds,
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
                    # 탐색이 이 스냅숏으로 다시 부를 때 합칠 월중 경계 몫만 따로 싣는다(EI-11 11r — `_MID_MONTH_PARTS_KEY`).
                    _eff_snap[_MID_MONTH_PARTS_KEY] = {"initial_constraints": deepcopy(_mid_ic),
                                                       "off_window_constraints": deepcopy(_mid_ow)}
                    setattr(_rs, "_effective_config_snapshot", _eff_snap)
                except Exception as _eff_exc:
                    print(f"[EffectiveConfigSnapshot] 실패(무시): {_eff_exc}")
                setattr(_rs, "_constraint_impact_merged_initial_constraints", deepcopy(config_dict.get("initial_constraints") or {}))
                # 월경계 꼬리를 읽은 상대 병동 전월 근무표 — 생성 결과 안내(EI-11 11w-2 · D38)가 응답 조립에서 읽는다.
                _rs._boundary_other_ward_refs = deepcopy(cross_month_constraints.get("other_ward_refs") or [])
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
                            "preceptee_on": bool(config_dict.get("preceptee_on", True)),
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


def _enqueue_assignment_created_push(db: Session, req, current_user, assignments, nurses_in_group) -> None:
    """파견·병동이동 대상자에게 '근무표 생성' 알림(S09)을 발송 대기열에 넣는다. **커밋은 호출부**.

    알림 준비가 실패해도 근무표 저장은 막지 않는다.
    ★ 준비용 조회(병동 이름·간호사 이름)는 **별도 읽기 세션**에서 한다. 업무 세션에서 조회하다
      DB 오류(시간 초과·교착)가 나면 세션이 실패 상태가 되어, 예외를 삼켜도 뒤이은 근무표 저장
      commit 이 함께 실패한다(Codex 2026-10-02). 업무 세션에는 대기열 행 추가(`db.add`)만 한다.
    ★ 저장 **전**에 부르므로 `nurses_in_group` 에 파견 들어온 간호사가 아직 없다(저장 뒤에 붙인다).
      거기서 못 찾은 이름은 간호사 테이블에서 직접 읽는다 — 안 그러면 알림에 사번이 찍힌다.
    """
    try:
        from db.client2 import SessionLocal
        from services.assignment_service import _get_group_name
        from utils.utils import send_assignment_roster_created_push
        nurse_ids = {
            str(a.nurse_id) for a in assignments
            if a.reason in ("파견", "병동이동") and a.status != "cancelled"
        }
        if not nurse_ids:
            return
        names_by_id = {str(n.nurse_id): n.name for n in nurses_in_group}
        ordered = [str(n.nurse_id) for n in nurses_in_group if str(n.nurse_id) in nurse_ids]
        rest = sorted(nurse_ids - set(ordered))
        with SessionLocal() as read_db:
            group_name = _get_group_name(read_db, current_user.group_id) or str(current_user.group_id)
            if rest:
                names_by_id.update({
                    str(nid): name
                    for nid, name in read_db.query(Nurse.nurse_id, Nurse.name).filter(Nurse.nurse_id.in_(rest))
                })
        names = [names_by_id.get(nid) or nid for nid in ordered + rest]
        send_assignment_roster_created_push(
            nurse_name=", ".join(names), group_name=group_name, year=req.year, month=req.month,
            recipients=list(nurse_ids), office_code=current_user.office_id,
            sender_emp_seq_no=current_user.nurse_id, sender_member_id=current_user.account_id, db=db,
        )
    except Exception as e:
        print(f"[RosterCreate] assignment 생성 알림 준비 실패: {e}")


def _persist_entries(db: Session, schedule, generated, req):
    """생성된 근무표를 ScheduleEntry로 저장한다."""
    db.query(ScheduleEntry).filter(ScheduleEntry.schedule_id == schedule.schedule_id).delete()
    shift_ids, default_map = _load_shift_mappings(db, schedule)
    shifts_db = (
        db.query(Shift)
        .filter(Shift.group_id == schedule.group_id, Shift.office_id == schedule.office_id)
        # ★ 같은 병동에 같은 `shift_id` 가 여러 행일 수 있다(실측: 운영 9개 병동 75조합).
        #   `shifts` 에 UNIQUE 제약이 아직 없어 개명·일괄업로드로 중복이 만들어졌다.
        #   정렬 없이 dict 로 접으면 **DB 가 돌려주는 마지막 행**이 이겨서, 근무코드 화면이
        #   보여 주는 행(sequence 순 첫 행)과 근무표 셀이 가리키는 행이 갈렸다
        #   — 사용자에겐 "코드를 고쳤는데 근무표에 반영이 안 된다" 로 보인다.
        #   화면과 같은 순서로 정렬하고 아래에서 **첫 행이 이기게** 한다.
        .order_by(Shift.sequence.asc(), Shift.id.asc())
        .all()
    )
    shift_id_to_int_id: dict[str, int] = {}
    for s in shifts_db:
        shift_id_to_int_id.setdefault(s.shift_id, s.id)
    weekend_off_nurse_ids: set[str] = set()
    try:
        # 주말휴무 SSOT = nurse_weekendoff_period (as-of month). 컬럼 미의존.
        from services.nurse_period_resolver import weekend_off_ids_asof
        weekend_off_nurse_ids = weekend_off_ids_asof(
            db, list(generated.keys()), schedule.year, schedule.month
        )
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
        # ★ 중복 shift_id 대비 — _persist_entries 와 **같은 순서**로 정렬해 같은 행을 고른다.
        #   여기만 다른 행을 고르면 파견/병동이동 전달분만 다른 코드 행을 가리킨다.
        tgt_shifts = (
            db.query(Shift)
            .filter(Shift.group_id == group_id)
            .order_by(Shift.sequence.asc(), Shift.id.asc())
            .all()
        )
        default_to_tgt = {}
        for s in tgt_shifts:
            if s.default_shift and s.default_shift not in default_to_tgt:
                default_to_tgt[s.default_shift] = s.shift_id
        tgt_shift_id_to_int: dict[str, int] = {}
        for s in tgt_shifts:
            tgt_shift_id_to_int.setdefault(s.shift_id, s.id)

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
                    continue
                # ★ 휴무 계열은 default_shift 가 비어 있어도 OFF 로 센다.
                #   콜 코드(`오프콜` = 휴일 콜대기)가 여기 해당한다 — 근무로 세면
                #   연속근무일수가 부풀어 위반 검출이 통째로 어긋난다.
                #   ★ `default_shift` 에 'O' 를 넣어 해결하면 안 된다. 프론트가 그 값을
                #     **D/E/N/O 대표 코드 선택**에 쓰기 때문에(nurse-util.ts `getDefaultShifts`
                #     의 `shifts.find(default_shift === "O")`) 값이 중복되면 대표가
                #     뒤바뀌어 OFF 행이 `오프콜` 로 표시된다(2026-08-18 실측).
                if sid and str((row or {}).get("type") or "").strip() == "휴무":
                    shift_main_map[sid] = "O"
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


def _ban_plan_key(rs, released: int = 0) -> tuple[int, int, int, int]:
    """전날 N 금지 최소화 결과의 비교 키 — (휴가·공가 전날 N, N 간격 위반, 하드락 해제 여부,
    신청 휴무 전날 N). 작을수록 낫다. `released` 는 앞 단계에서 풀린 같은 근무 연속·고립 근무를
    그대로 쓴 시도면 1 — 신청 휴무 전날 N 몇 칸을 위해 하드락 성격의 규칙을 풀지 않게 한다."""
    _t, _w = getattr(rs, "_ban_night_violations", None) or (0, 0)
    _n2n = None
    try:
        from services.cp_sat.fallback_lex import count_n2n_gap_violations
        _n2n = count_n2n_gap_violations(rs)
    except Exception as _exc:
        print(f"[BanNightFallback] N 간격 위반 계산 실패(고정값 사용): {_exc}")
    if _n2n is None:
        _n2n = int(getattr(rs, "_n2n_gap_violations", 0) or 0)
    return int(_t), int(_n2n), int(released), int(_w)


def _carried_hardlock_relaxations(final_cfg, ss_orig_on: bool, iw_orig_on: bool, *,
                                  roster_system=None, nurses=None, year=None, month=None,
                                  ss_orig_k=None) -> list[tuple[str, dict]]:
    """완화 사다리가 실패하며 누적한 하드락 해제 중 최종 채택 설정에 남은 것 — (완화 키, 경고).

    same_shift·isolated_work 재시도가 실패하면 그 해제가 config_dict 에 남아 뒤 단계(전날 N
    최소화·N 간격 해제)가 물고 간다. 뒤 단계는 자기 완화만 알리므로 앞 해제가 경고 없이 나갔다
    (Codex 지적 · 실측 성남 중환자실-RN 2026-11 VER31: 같은 근무 4연속 17곳).
    """
    out: list[tuple[str, dict]] = []
    if final_cfg is None:
        return out
    if ss_orig_on and int(getattr(final_cfg, "same_shift_hard_k", 0) or 0) == 0:
        out.append(("same_shift_hard_to_soft", build_relaxation_notice("same_shift", {
            "type": "same_shift_hard_to_soft_applied",
            "detail": ("같은 시프트 연속 하드 제한이 infeasible 로 자동 soft 전환되어 재생성됐습니다. "
                       "일부 간호사에게 같은 시프트가 4회 이상 연속될 수 있습니다."),
        }, roster_system=roster_system, nurses=nurses, year=year, month=month, param=ss_orig_k)))
    if iw_orig_on and not bool(getattr(final_cfg, "isolated_work_hard", True)):
        out.append(("isolated_work_hard_to_soft", build_relaxation_notice("isolated_work", {
            "type": "isolated_work_hard_to_soft_applied",
            "detail": ("고립근무 금지가 infeasible 로 자동 soft 전환되어 재생성됐습니다. "
                       "일부 간호사에게 휴무 사이 단일 근무가 생길 수 있습니다."),
        }, roster_system=roster_system, nurses=nurses, year=year, month=month, param=None)))
    return out


# 엔진 1단계 시간 초과(해 없음 미증명) — 첫 생성이면 일시 실패(TimeoutError)로 올린다.
_STAGE1_UNKNOWN_CODE = "STAGE1_UNKNOWN"


def _validate_generated_roster(
    generated: dict[str, list[str]] | None,
    roster_system,
    nurses_context: list | None = None,
    config_context: dict | None = None,
) -> str | None:
    """엔진 결과 검증 — 기존 검증(`_validate_generated_roster_core`)을 **먼저** 돌리고, 그게 놓친
    '엔진 1단계 해 없음'만 더 잡는다(순수 추가 · 기존 검증이 잡은 경우의 문구·원인 탐색은 그대로).

    ★ 놓치는 경우: 확정 원티드 등 고정 칸이 남아 '실근무 0건'·'하루 0명' 증상에 안 걸린다
      (성남 61-AN 2026-11 지정 1N + 1N 금지 → 빈 근무 116칸이 '성공' 저장 · 운영 2026-07~10
      61-RN 25건). 엔진 표시(`fallback_lex` `_fallback_stage1_no_solution`)를 직접 본다.
    """
    msg = _validate_generated_roster_core(
        generated, roster_system, nurses_context=nurses_context,
        config_context=config_context,
    )
    if msg:
        return msg

    def _with_ontology(m: str) -> str:
        if roster_system is not None:
            try:
                setattr(roster_system, "_ontology_last_reason",
                        attach_reason_code_ontology(message=m, severity="hard"))
            except Exception:
                pass
        return m

    _s1_none = getattr(roster_system, "_fallback_stage1_no_solution", None)
    if _s1_none == "unknown":
        # 엔진 1단계가 시간 안에 해를 못 찾았다(해 없음은 증명 안 됨). 빈 배정은 실패로 돌리되
        # 완화 사다리 신호(`_infeasible_empty`)는 세우지 않는다 — 다시 돌리면 풀릴 수 있다.
        # 첫 생성이면 호출부가 TimeoutError 로 올려 워커가 재시도하고(`_STAGE1_UNKNOWN_CODE`),
        # 완화 재시도·진단 중이면 '그 시도 실패' 로만 쓰인다.
        return _with_ontology(
            f"[reason_code={_STAGE1_UNKNOWN_CODE}] 엔진 1단계가 제한 시간 안에 해를 찾지 못했습니다"
            "(해 없음 미증명)."
        )
    if _s1_none == "infeasible":
        # 엔진 1단계가 해 없음을 증명했다(하드 제약끼리 충돌). 확정 원티드 등 고정 칸이 남아
        # 기존 검증의 '실근무 0건'·'하루 0명' 증상에 안 걸리는 경우라 엔진 표시를 직접 본다.
        # 신호·문구는 실근무 0건과 같게 둔다 — 완화 사다리·구조진단·해결 카드가 그대로 이어받는다.
        try:
            setattr(roster_system, "_infeasible_empty", True)
        except Exception:
            pass
        ev = getattr(roster_system, "_validator_evidence", None) or {}
        ev_brief = {
            "total_failed_cells": ev.get("total_failed_cells"),
            "required_minus_assigned_total": ev.get("required_minus_assigned_total"),
            "fixed_forbidden_count": ev.get("fixed_forbidden_count"),
        }
        return _with_ontology(
            "[reason_code=NO_ASSIGNMENT] 엔진 배정 0건(1단계 해 없음 — 고정 칸만 남음 · 증상). "
            f"원인은 구조진단에서 결정. evidence={ev_brief}"
        )
    return None


def _validate_generated_roster_core(
    generated: dict[str, list[str]] | None,
    roster_system,
    nurses_context: list | None = None,
    config_context: dict | None = None,
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


def _preceptee_coverage_skipper(roster_system, cfg):
    """커버리지 회계에서 제외할 프리셉티 판별기를 만든다.

    preceptee_shift_count=False 면 프리셉티는 커버리지상 '없는 인력'이다. 솔버의
    need/supply 와 같은 기준으로 세지 않으면 부족 리포트가 솔버와 어긋나 phantom
    부족·과소보고의 씨앗이 된다.
    ★ 이 설정은 roster_config 상 preceptee_on=True 일 때만 유효하므로 여기서
      preceptee_on 을 다시 보지 않는다.
    ★ 판별 규칙은 cp_sat_basic 의 _is_preceptee_at 과 동일하다
      (SSOT = nurse_preceptee_period, 설계: docs/NURSE_PRECEPTEE_PERIOD_DESIGN.md §6).
        맵 있음(권위)  → 그 사람의 follow day 집합에 d 가 있을 때만 프리셉티
        맵 없음(폴백)  → preceptor_id 보유자를 전체월 프리셉티로(기존 동작 보존)
    """
    if bool(getattr(cfg, "preceptee_shift_count", True)):
        return lambda n, d: False
    if bool(getattr(roster_system, "preceptee_period_authoritative", False)):
        follow_days = getattr(roster_system, "preceptee_follow_days", {}) or {}
        return lambda n, d: d in (follow_days.get(n) or set())
    fallback = {n for n, nu in enumerate(roster_system.nurses)
                if getattr(nu, "preceptor_id", None)}
    return lambda n, d: n in fallback


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

        # 시프트별 '정책상 가능 인원'(allowed_shifts 기준) 캐시 — 부족 원인 분류에 사용.
        # allowed_shifts 가 빈/None 이면 전 시프트 가용. 특정 코드로 제한되면 그 코드만.
        def _eligible_for(s_code: str) -> int:
            cnt = 0
            for nu in roster_system.nurses:
                raw = getattr(nu, "allowed_shifts", None)
                if isinstance(raw, list) and raw:
                    allowed = {str(x).strip().upper() for x in raw if str(x).strip()}
                    allowed = {a for a in allowed if a in shift_types}
                    if allowed and s_code not in allowed:
                        continue
                cnt += 1
            return cnt
        _elig_cache: dict[str, int] = {}
        # 프리셉티 제외 판별(솔버와 같은 기준). eligible 은 day 무관 집계라 그대로 둔다 —
        # 권위 모드의 프리셉티는 day 별이라 전월 단위 정원에 반영할 수 없다.
        _skip_pte = _preceptee_coverage_skipper(roster_system, cfg)

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
                assigned = int(sum(int(roster_system.roster[n, d, s_idx])
                                   for n in range(N) if not _skip_pte(n, d)))
                if assigned < req:
                    if s_code not in _elig_cache:
                        _elig_cache[s_code] = _eligible_for(s_code)
                    eligible = _elig_cache[s_code]
                    # 원인 분류: 그 시프트 정책상 가능 인원이 요구보다 적으면 eligibility 부족
                    # (예: 야간 불가 인원 과다 → N 가능 인원 < 요구). 아니면 총 capacity 부족.
                    reason = "eligibility_shortage" if eligible < req else "capacity_shortage"
                    gaps.append({
                        "day": d + 1,
                        "shift": s_code,
                        "need": req,
                        "assigned": assigned,
                        "short": req - assigned,
                        "eligible": eligible,
                        "reason": reason,
                    })
        return gaps
    except Exception as exc:
        print(f"[CoverageGaps] 계산 실패: {exc}")
        return []


def _violation_counts_by_family(violations) -> dict[str, int]:
    """위반 목록 → {family prefix: 건수}. family 구분은 품질 알림과 같은 함수로 한다."""
    from utils.roster_quality import _family_label

    counts: dict[str, int] = {}
    for v in violations or []:
        fam, _ = _family_label(getattr(v, "node_id", None))
        counts[fam] = counts.get(fam, 0) + 1
    return counts


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
            # violated_constraints 는 50건에서 자른다 → 종류별 건수는 자르기 전 전체로 따로 싣는다
            #   (품질 알림 `roster_quality._ontology_violations` 가 이걸로 센다).
            "violated_counts_by_family": _violation_counts_by_family(analysis.violated_constraints),
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
    elif L and cons_n > 0 and offs_after == 0:
        # 상대 병동 N 꼬리에 이 병동 N 을 이어 붙일 수 있는 만큼만 연다(#3 상한 · #4·#5 회복 — EI-11 11q G1).
        limit = _night_extension_limit(cons_n, L, two_after_two, two_after_three)
        if limit <= 0:
            forbidden[boundary_day].append('N')
        else:
            off_window_constraints.append([boundary_day, min(boundary_day + limit, days_in_month - 1)])

    return {
        'forced_off': sorted(set(forced_off)),
        'forbidden': {d: sorted(set(v)) for d, v in forbidden.items()},
        'off_window_constraints': off_window_constraints,
    }


_SHIFT_GB_MAIN = {"데이": "D", "이브닝": "E", "나이트": "N", "미드": "M"}


def _shift_def_main(s) -> tuple[str | None, bool]:
    """근무코드 정의(Shift 행) 하나 → (메인코드, type 으로 정했나). 월경계(`build_cross_month_constraints`)와 상대 병동 코드 읽기
    (`_group_shift_code2main`)가 같이 쓰는 한 규칙이다(EI-13 12u — 두 벌이면 한쪽만 고쳐져 갈라진다).
      ① shift_gb(데이·이브닝·나이트·미드) ② default_shift(D·E·N·M·O — OFF·주는 O) ③ type(최후 폴백) — '근무'는 'WK'(근무 마커 ·
      'W' 는 엔진 주휴 타입이라 피한다 · `_calc_tail_metrics` 가 연속근무로 센다), '휴가'·'공가'는 'O'. 못 정하면 (None, False)."""
    sgb = str(getattr(s, "shift_gb", "") or "").strip()
    if sgb in _SHIFT_GB_MAIN:
        return _SHIFT_GB_MAIN[sgb], False
    ds = str(getattr(s, "default_shift", "") or "").strip().upper()
    ds = "O" if ds in ("OFF", "주") else ds
    if ds in ("D", "E", "N", "M", "O"):
        return ds, False
    kind = str(getattr(s, "type", "") or "").strip()
    if kind == "근무":
        return "WK", True
    if kind in ("휴가", "공가"):
        return "O", True
    return None, False


def _group_slot_main(db: Session, group_id: str) -> dict[str, str]:
    """그 병동 근무 슬롯(ShiftManage — 생성이 읽는 'RN' 행 · AN 병동도 'RN' 관례)을 그 병동이 자기 근무표를 만들 때와 같은 함수로
    읽는다(`_build_code_to_main_map` — 슬롯 main_code · 생성 조회처럼 슬롯·id 순 마지막 행 · EI-13 12v · D52). 근무(D·E·N·M)로 정한
    코드만 돌려준다 — 휴무 슬롯(main_code O)과 OFF·주·O 는 빼서 상대 병동 근무를 휴무로 바꾸지 않는다(보수 예외)."""
    rows = db.query(ShiftManage).filter(ShiftManage.group_id == group_id, ShiftManage.nurse_class == "RN").all()
    own = _build_code_to_main_map([{"main_code": r.main_code, "codes": r.codes}
                                   for r in sorted(rows, key=lambda r: (int(r.shift_slot or 0), int(r.id or 0)))])
    return {code: main for code, main in own.items() if main in ("D", "E", "N", "M")}


def _group_shift_code2main(db: Session, group_id: str, base: dict, maps: dict | None = None,
                           reads: dict | None = None) -> dict:
    """그 병동 근무표 칸을 읽는 근무코드 → 메인코드 맵 — 그 병동이 자기 근무표를 만들 때와 같은 순서로 읽는다(EI-13 12v · D52 — 두
    병동이 같은 코드를 같은 뜻으로). 그 병동 뜻이 이 병동 맵(`base`)보다 먼저다(12u · Codex P1). 우선순위:
      ① 그 병동 근무 슬롯(`_group_slot_main` — 그 병동 자신의 `_build_code_to_main_map` · 근무코드 행이 없는 슬롯 코드도 · 12u-2) →
      ② 슬롯에 없는 코드는 그 병동 구분·기본코드(`_shift_def_main` — 월경계 보강과 같은 판정) → ③ `base` → ④ type 폴백(Codex P2 —
      근무 WK · 휴가·공가 O · `base` 가 못 읽는 코드에만. 단 그 병동이 '근무'라 하면 `base` 의 OFF 는 덮는다).
    구분·기본코드와 슬롯이 다르면(설정 오류) 그 병동 자신처럼 슬롯을 따르고 알린다(WARN).
    보수 예외 — 그 병동 자신과 달리 읽어 하드락이 풀리는 쪽을 막는다: type 만으로 정한 뜻은 `base` 를 덮지 않는다(덮으면 `base` 근무
    대 type 휴가 → 휴무 · `base` N 대 type 근무 → WK 로 #6·N 규칙·전이 금지가 빠진다) · 휴무 슬롯은 근무를 지우지 않는다(①에서 뺀다).
    그 병동이 정하지 못한 코드와 그 병동에 없는 코드는 `base` 그대로다.
    `maps`(병동 → 맵)를 주면 병동마다 한 번만 읽는다(같은 `base` 를 쓰는 호출끼리 — 근무코드·슬롯 조회 각 1회). `reads` 를 주면
    시도 사이에도 한 번만 읽는다(EI-13a 읽기 메모 — 입력 `base` 는 시도마다 같다).
    조회 실패는 삼키지 않는다 — 세부 코드를 못 읽으면 하드락 판정을 할 수 없다(막는 쪽)."""
    if maps is not None and group_id in maps:
        return maps[group_id]

    def _load():
        rows = db.query(Shift).filter(Shift.group_id == group_id).all()
        slots = _group_slot_main(db, group_id)
        out = {**base, **slots}
        for s in rows:
            sid = str(s.shift_id).strip().upper()
            main, by_type = _shift_def_main(s)
            if not sid or main is None:
                continue
            if sid in slots:
                if not by_type and main != slots[sid]:
                    print(f"[WARN] 상대 병동 근무코드 정의≠슬롯: group={group_id} code={sid} 정의={main} 슬롯={slots[sid]} "
                          f"→ 슬롯을 따름(그 병동 자신의 판정)")
                continue
            if not by_type or sid not in base or (main == "WK" and base[sid] == "O"):
                out[sid] = main
        return out

    out = _memo_read(reads, ("group_code2main", str(group_id)), _load)
    if maps is not None:
        maps[group_id] = out
    return out


def _count_other_ward_nights(db: Session, entries: list, days: set, code2main: dict, group_id: str,
                             group_maps: dict) -> int:
    """같은 달 상대 병동 근무표에서 이 간호사가 선 나이트 수 — 그 병동에 있는 날(`days` — `_other_ward_days`: 그 병동 배정 날짜 ∩
    차단일)만 센다(EI-11 11q 리뷰 #6 · EI-13 12w).

    차단일만 세는 이유: 상대 근무표가 파견 전에 지어져 이 병동이 배정할 날까지 칸이 있으면 같은 날을 두 번 센다.
    근무코드는 그 병동 정의·슬롯으로 읽는다(`_group_shift_code2main` — 이 병동 맵이 알아보는 코드도 그 병동 뜻이 먼저 · EI-13 12u ·
    12u-2 · Codex P1). 병동마다 한 번 읽는다(`group_maps` 캐시 — 월중 경계 빌더가 이미 읽은 병동 맵을 받는다). 고를 칸이 없으면 읽지
    않는다."""
    picked = [e for e in entries if int(e.work_date.day) - 1 in days]
    if not picked:
        return 0
    group_map = _group_shift_code2main(db, group_id, code2main, group_maps)
    return sum(1 for e in picked if _normalize_to_main(e.shift_id, group_map) == 'N')


def _other_ward_days(assignments: list, nurse_id: str, other_gid: str, group_id: str, month_start: date,
                     days_in_month: int, blocked_days: set) -> set[int]:
    """이 간호사가 같은 달 상대 병동 `other_gid` 에 있는 날(0부터) — 그 병동과의 배정 날짜만이다(같은 병동 배정이 여럿이면 합집합 ·
    EI-13 12w · Codex 3차 P2). 차단일 전체로 세면 두 병동으로 따로 간 간호사의 한 병동 근무표에 남은 기간 밖 칸(배정이 바뀌기 전에
    지어진 칸)까지 세어 두 병동 나이트 합(#6)이 부풀고, 이 병동 몫이 0 이 되어 거짓 해 없음이 난다.
      - 이 병동 → 그 병동(파견·병동이동 출발): 배정 기간(차단일과 같은 규칙 — `day_windows.build_blocked_days`).
      - 그 병동 → 이 병동(파견·병동이동 합류): 이 병동에 없는 날 전부(차단일 — 원 병동에 있는 날).
    차단일 밖(이 병동이 배정하는 날)은 늘 뺀다 — 같은 날을 두 번 세지 않는다. 배정 행은 이미 읽은 것이다(조회 0)."""
    month_end = month_start + timedelta(days=days_in_month - 1)
    days: set[int] = set()
    for a in assignments:
        if str(a.nurse_id) != nurse_id or a.reason not in ("파견", "병동이동"):
            continue
        if a.source_group_id == group_id and a.target_group_id == other_gid:
            lo = max(a.start_date, month_start)
            hi = min(a.end_date or a.expected_end_date or month_end, month_end)
            days.update(range((lo - month_start).days, (hi - month_start).days + 1))
        elif a.target_group_id == group_id and a.source_group_id == other_gid:
            days |= blocked_days
    return days & blocked_days


def _active_days_here(active_range: tuple[int, int] | None, blocked_days: set) -> int:
    """이 병동이 그 간호사에게 배정하는 날 수(활동 구간 − 차단일)."""
    if not active_range:
        return 0
    return sum(1 for d in range(active_range[0], active_range[1] + 1) if d not in blocked_days)


def _apply_cross_ward_night_caps(nurses: list, other_nights: dict[str, int], active_here: dict[str, int],
                                 config_dict: dict) -> dict[str, int]:
    """하드락 #6(간호사 1인당 월 나이트 상한)이 같은 달 상대 병동 나이트까지 세도록 파견·병동이동 간호사의 이 병동 몫을
    줄인다(EI-11 11q 리뷰 #6 — CLAUDE.md '간호사 1인당 월 나이트 최대 15회'). 바꾼 간호사 → 새 `n_max` 를 돌려준다.

    엔진의 월 나이트 상한(`max_night_shifts_per_month`)은 이 병동 배정만 센다. 그래서 개인 월 N 상한(`n_max` — precheck 와 엔진
    `add_monthly_limit_constraints` 가 하드로 읽는다)을 `상한 − 상대 병동 나이트` 이하로 낮춘다.
      - 이 병동에서 서는 날 수가 남은 몫 이하면 넘을 수 없으니 건드리지 않는다.
      - 정확 횟수(`n_exact`)가 남은 몫 이하면 그대로 둔다. 넘으면 [정확 횟수, 남은 몫] 범위로 바꿔 해가 없게 한다(완화하지 않고
        infeasible 로 보고 — CLAUDE.md).
      - 1N 금지(#7)가 켜져 있으면 새로 생기는 상한 1 을 0 으로 둔다 — 엔진은 `n_max == 1` 간호사를 1N 금지에서 빼므로(운영자
        명시 의도) 이 계산이 그 면제를 새로 만들면 #7 이 풀린다. 이미 `n_max == 1` 로 저장된 간호사는 그대로다."""
    from services.cp_sat.allowed_shift_types import HARD_MAX_NIGHTS_PER_MONTH as _hard

    try:
        cap = int(config_dict.get("max_nig_per_month") or _hard)
    except (TypeError, ValueError):
        cap = _hard
    cap = _hard if cap <= 0 else min(cap, _hard)
    not_one_night = bool(config_dict.get("not_one_night", False))
    changed: dict[str, int] = {}
    for nurse in nurses:
        nid = str(getattr(nurse, "nurse_id", "") or "")
        room = max(0, cap - int(other_nights.get(nid) or 0))
        if nid not in other_nights or active_here.get(nid, 0) <= room:
            continue
        attrs = nurse.__dict__
        exact = attrs.get("n_exact")
        if exact is not None and int(exact) <= room:
            continue
        if exact is not None:
            attrs["n_min"], attrs["n_exact"] = int(exact), None
        current = attrs.get("n_max")
        new = room if current is None else min(int(current), room)
        if new == 1 and not_one_night and current != 1:
            new = 0
        attrs["n_max"] = new
        changed[nid] = new
    return changed


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

def _record_unclassified_failure(db: Session, req, job_id, ctx: dict) -> None:
    """낙인 지점을 안 거치고 빠져나간 실패를 'error' 로 남긴다.

    latest_config 를 얻기 전에 난 것(권한 오류 등)은 기록하지 않는다 —
    그건 "이 설정으로 생성이 안 된다" 가 아니라 요청 자체의 문제다.
    """
    # ★ 실패로 끝난 생성의 draft 근무표를 지운다 — blocked(:db.delete)·
    #   infeasible 과 대칭. 이게 없으면 예외마다 병동 근무표 목록에 draft 가 쌓인다.
    _sch = ctx.get("schedule")
    if _sch is not None:
        try:
            _sid = getattr(_sch, "schedule_id", None)
            if _sid is not None:
                db.execute(text("DELETE FROM schedule_entries WHERE schedule_id = :s"),
                           {"s": _sid})
                db.execute(text("DELETE FROM schedules WHERE schedule_id = :s"),
                           {"s": _sid})
                db.commit()
                print(f"[RosterGenerate] 미분류 실패 — draft 근무표 정리: {_sid}")
        except Exception as _del_exc:
            try:
                db.rollback()
            except Exception:
                pass
            print(f"[RosterGenerate] draft 정리 실패(무시): {_del_exc}")

    cfg = ctx.get("config")
    if cfg is None:
        return
    try:
        _mark_config_generate_status(
            db, cfg, "error",
            _build_generate_snapshot(
                db, cfg, "error", req.year, req.month,
                ctx.get("nurses"), ctx.get("config_dict"), job_id,
                ctx.get("persistent_fix")),
            req.year, req.month, job_id,
            bool(ctx.get("intervened")) or bool(ctx.get("relaxed")),
        )
    except Exception as _exc:
        print(f"[ConfigGenerateStatus] 예외 경로 기록 실패(무시): {_exc}")


def generate_roster_service(req: RosterRequest, current_user, db: Session, treatment_ids=None, config_override: dict | None = None, weekend_off_release=None, monthly_limit_release=None, banned_wanted_release=None, allowed_shift_add=None, job_id: str | None = None, column_delta_mode: str | None = None):
    """근무표 생성 — 실패 기록을 빠짐없이 남기기 위한 얇은 래퍼.

    본체(_generate_roster_service_impl)는 precheck 차단(blocked)과 solver 실패
    (infeasible) 두 지점에서만 기록한다. 그런데 그 둘을 안 거치고 빠져나가는
    실패가 있다 — `_validate_mid_hard_feasibility` 의 `raise Exception(...)`
    (MID 하드 검증), 솔버 예외 등. 그대로 두면 실패한 설정이 프리셋 목록에
    계속 남아 이 기능이 그 입력에서만 무력해진다.

    HTTPException 도 그냥 통과시키면 안 된다 — blocked/infeasible 은 낙인 후 던지지만,
    MID 하드 검증 실패처럼 **기록 없이 나가는 HTTPException 도 있다**(실측:
    use_mid=False 인데 fixed_shift='MID' 인 간호사가 있으면 그 경로로 빠진다).
    그래서 "이미 기록했는가"(_ctx["recorded"])로 판단한다.
    권한 오류처럼 latest_config 를 얻기 전에 나는 것은 _ctx["config"] 가 없어
    자연히 걸러진다 — 그건 "이 설정으로 생성이 안 된다" 가 아니기 때문이다.
    """
    _ctx: dict = {}
    # 요청 전체 실행 마감(사다리 재시도·엔진 추가 풀이가 공유) — `services.cp_sat.gen_deadline`.
    from services.cp_sat import gen_deadline
    _dl_token = gen_deadline.start()
    try:
        return _generate_roster_service_impl(
            req, current_user, db, treatment_ids, config_override,
            weekend_off_release, monthly_limit_release, banned_wanted_release,
            allowed_shift_add, job_id, column_delta_mode, _ctx,
        )
    except HTTPException:
        # ★ HTTPException 이라고 다 기록된 건 아니다. blocked/infeasible 은 낙인 후
        #   던지지만, MID 하드 검증 실패 같은 다른 HTTPException 은 기록 없이 나간다
        #   (실측: use_mid=False 인데 fixed_shift='MID' 인 간호사가 있으면 여기로 빠진다).
        #   기록 여부를 _ctx 로 확인해, 안 남았으면 여기서 남긴다.
        #   권한 오류처럼 latest_config 를 얻기 전에 나는 것은 _ctx["config"] 가 없어
        #   자연히 걸러진다 — 그건 "이 설정으로 생성이 안 된다" 가 아니다.
        if not _ctx.get("recorded"):
            _record_unclassified_failure(db, req, job_id, _ctx)
        raise
    except Exception as _gen_exc:
        print(f"[RosterGenerate] 미분류 실패 — {type(_gen_exc).__name__}: {_gen_exc}")
        if not _ctx.get("recorded"):
            _record_unclassified_failure(db, req, job_id, _ctx)
        raise
    finally:
        gen_deadline.reset(_dl_token)



def _generate_roster_service_impl(req: RosterRequest, current_user, db: Session, treatment_ids=None, config_override: dict | None = None, weekend_off_release=None, monthly_limit_release=None, banned_wanted_release=None, allowed_shift_add=None, job_id: str | None = None, column_delta_mode: str | None = None, _ctx: dict | None = None):
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
    # ★ 생성 진입 관문(EI-11 H3) — 개인 속성 쓰기·materialize·`_ctx["config"]` 보다 앞이다(부분 반영·멀쩡한 프리셋의
    #   실패 낙인을 남기지 않게). 라우터 가드를 안 지나는 통로(큐 잔류·DLQ redrive·JOB_JSON·직접 호출)도 같은 판정으로 400.
    #   처방(`treatment_ids`)은 라우터가 아예 보지 않는다 — 화면 `/async` 를 포함해 모든 경로에서 이 관문이 처방의 유일한
    #   판정이라, 화면에서 고른 금지 처방은 큐에 실린 뒤 여기서 걸려 워커 FAILED 토스트가 된다(11r 리뷰 — 주석 정정).
    assert_night_caps_within_hard_lock(
        config_override, monthly_limit_release or getattr(req, "monthly_limit_release", None))
    from services.policy.card_filter import assert_treatments_allowed
    assert_treatments_allowed(treatment_ids)
    # 판정이 `int()` 로 읽어 통과시킨 정수 상한 키의 문자열·실수를 그 정수로 바꿔 엔진에 넘긴다(EI-11 11r — 예전엔 엔진 TypeError).
    config_override = _normalize_int_cap_override(config_override)
    # 별칭(dataclass 이름) → 정본 키(DB 컬럼명) — 판정이 원래 이름을 본 뒤에 바꾼다(EI-12 · D9).
    _override_given = bool(config_override)  # 값 없는 별칭만 보냈어도 '개입'이다 — 정규화 결과가 비어도 낙인 판정은 그대로(EI-12 12r)
    config_override = normalize_aliases(config_override)
    # per-nurse 주말휴무 해제(검증된 resolution 원클릭). 해당 월 발효로 period 에 해제(0) write
    #   = 개인 속성 변경(영구). nurse_weekendoff_period SSOT. 컬럼 미조회.
    _wor = weekend_off_release or getattr(req, "weekend_off_release", None)
    if _wor:
        from db.models import NurseWeekendOffPeriod as _NWOP
        from services.nurse_period_resolver import upsert_period as _up
        from datetime import date as _d
        _vf = _d(int(req.year), int(req.month), 1)
        for _nid in _wor:
            _up(db, _NWOP, str(_nid), _vf, "weekend_off", 0, source="resolution")
        db.commit()
        print(f"[RosterGenerate] 주말휴무 해제 적용(period, 발효 {_vf}): {list(_wor)}")
    # per-nurse 월 야간 한도 하향(검증된 resolution 원클릭). 해당 월 NurseMonthlyLimit 에 field 하향
    #   write = 개인 속성 변경. fetch 와 동일 스코프(current_user.group_id)에 써 target-first 로 픽업.
    _mlr = monthly_limit_release or getattr(req, "monthly_limit_release", None)
    if _mlr:
        from db.models import NurseMonthlyLimit as _NML
        _gid = str(current_user.group_id)
        _applied = []
        # ★ 쓰기 직전에 다시 본다 — 큐 대기 중 N전담으로 바뀐 사람의 고정 횟수를 지우지 않게. 거부는 라우터
        #   (`assert_release_not_n_only`)가 하고 여기선 건너뛴다. 1548b08 이후 입력 오류는 재시도 없이 FAILED 다 —
        #   라우터에서 막는 이유는 큐 적재·프리셋 포크 전에 끊기 위해서다.
        _n_only = n_only_release_targets(db, _gid, int(req.year), int(req.month), _mlr)
        for _rel in _mlr:
            _rnid = str(_rel.get("nurse_id") or "")
            _rfld = _rel.get("field")
            _rval = _rel.get("value")
            if not _rnid or _rfld not in ("n_exact", "n_max") or _rval is None:
                continue
            if _rnid in _n_only:
                print(f"[RosterGenerate] N전담 월 야간 한도는 해결 카드로 바꾸지 않음 — 건너뜀: {_rnid}:{_rfld}={_rval}")
                continue
            _row = (
                db.query(_NML)
                .filter(_NML.nurse_id == _rnid, _NML.group_id == _gid,
                        _NML.year == int(req.year), _NML.month == int(req.month))
                .first()
            )
            if _row is None:
                _row = _NML(nurse_id=_rnid, group_id=_gid,
                            year=int(req.year), month=int(req.month))
                db.add(_row)
            setattr(_row, _rfld, int(_rval))
            # 야간 고정/최대 상호배타: 하나를 세팅하면 반대 필드 제거(DB 정합).
            if _rfld == "n_exact":
                _row.n_max = None
            else:  # n_max
                _row.n_exact = None
                # 하한(n_min)이 새 상한보다 크면 모순(min>max) → 하한 해제해 '최대 N'으로 묶는다.
                if _row.n_min is not None and int(_row.n_min) > int(_rval):
                    _row.n_min = None
            _applied.append(f"{_rnid}:{_rfld}={_rval}")
        db.commit()
        print(f"[RosterGenerate] 월 야간 한도 하향 적용(NurseMonthlyLimit {req.year}-{req.month}): {_applied}")
    # per-nurse 금지근무(banned_wanted) 해제(검증된 resolution 원클릭). 겹치는 날의 금지 셀을
    #   is_applied=False 로(가역) → 필수 휴무가 가능해짐. day_idx→shift_date 변환.
    _bwr = banned_wanted_release or getattr(req, "banned_wanted_release", None)
    if _bwr:
        from datetime import date as _dbw
        _gid_bw = str(current_user.group_id)
        _rel_cnt = 0
        for _rel in _bwr:
            _rnid = str(_rel.get("nurse_id") or "")
            for _dy in (_rel.get("days") or []):
                try:
                    _sd = _dbw(int(req.year), int(req.month), int(_dy) + 1)
                except (TypeError, ValueError):
                    continue
                for _r in db.query(BannedWantedEntry).filter(
                        BannedWantedEntry.group_id == _gid_bw,
                        BannedWantedEntry.nurse_id == _rnid,
                        BannedWantedEntry.year == int(req.year),
                        BannedWantedEntry.month == int(req.month),
                        BannedWantedEntry.shift_date == _sd).all():
                    _r.is_applied = False
                    _rel_cnt += 1
        db.commit()
        print(f"[RosterGenerate] 금지근무 해제 적용(banned_wanted, {req.year}-{req.month}): {_rel_cnt}건")
    # per-nurse 근무유형(allowed_shifts)에 D/E 추가(검증된 resolution 원클릭). 현재 허용 ∪ add 를
    #   NurseAllowedShiftPeriod 에 해당 월 발효로 write(period SSOT). N전담 병목 해소용.
    _asa = allowed_shift_add or getattr(req, "allowed_shift_add", None)
    if _asa:
        from db.models import NurseAllowedShiftPeriod as _NASP, Nurse as _NurseM
        from services.nurse_period_resolver import upsert_period as _up_as
        from datetime import date as _das
        _vf_as = _das(int(req.year), int(req.month), 1)
        _applied_as = []
        for _rel in _asa:
            _rnid = str(_rel.get("nurse_id") or "")
            _add = [str(x).strip().upper() for x in (_rel.get("add") or []) if str(x).strip()]
            if not _rnid or not _add:
                continue
            _nrow = db.query(_NurseM).filter(_NurseM.nurse_id == _rnid).first()
            _cur = [str(x).strip().upper() for x in (getattr(_nrow, "allowed_shifts", None) or [])]
            _new = sorted(set(_cur or ["N"]) | set(_add))
            _up_as(db, _NASP, _rnid, _vf_as, "allowed_shifts", _new, source="resolution")
            _applied_as.append(f"{_rnid}:{_new}")
        db.commit()
        print(f"[RosterGenerate] 근무유형 추가 적용(NurseAllowedShiftPeriod, 발효 {_vf_as}): {_applied_as}")
    # 모달 payload(req.config) 제공 시 생성 직전 config row 로 materialize(굳히기) → req.config_id 세팅.
    #   /async 라우터는 이미 materialize 후 req.config=None 으로 넘겨 여기선 no-op(이중 생성 방지).
    #   /roster_create/generate(로컬 sync) 등 config 를 실어 직접 호출하는 경로는 여기서 materialize 되어
    #   ① config row('새로운 설정n' 또는 baseline 재사용)가 생성/확정되고 ② 편집값으로 생성된다(기존엔 baseline 무시 버그).
    if getattr(req, "config", None):
        from services.roster_service import materialize_generation_config
        from schemas.roster_schema import RosterConfigCreate
        _mat = materialize_generation_config(
            db, RosterConfigCreate(**req.config), current_user,
            override_group_id=current_user.group_id,
        )
        req.config_id = _mat.config_id
        # 포크된 새 config 에 모델 밖 컬럼을 baseline 에서 승계한다. 안 하면 값이 NULL 로
        #   들어가 config 가 쌓일수록 **끄기가 어려워진다**(옛 행이 계속 읽힌다).
        inherit_n2n_min_gap(db, current_user.group_id, _mat.config_id)
        req.config = None
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
    if _ctx is not None:
        # 예외 경로에서 draft 를 정리하기 위해 넘긴다(blocked/infeasible 과 대칭).
        _ctx["schedule"] = schedule
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
    # [속성 SSOT] home 간호사 grade/weekend/fixed 를 대상월 period as-of 로 오버레이(캐시 대신 그 달 값).
    #   fixed_shift 가 아래 _split_fixed_nurses 분기를 좌우하므로 split 이전에 적용한다.
    #   inbound 는 하단에서 별도 as-of 주입 → 여기는 home(nurses_in_group) 만 대상.
    try:
        _overlay_home_profile_asof(
            db, nurses_in_group, current_user.group_id, date(req.year, req.month, 1)
        )
    except Exception as _e_home_asof:
        print(f"[PeriodAsOf][home] grade/weekend/fixed 오버레이 실패(캐시 유지로 진행): {_e_home_asof}")
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
    # ── 병동이동 레이지 체크 + assignment 기반 blocked_by_nurse 구성 ──
    #   [도려내기] 프리셉티 만료 flush 제거 — period as-of resolver(아래 preceptee_period 빌드)가 자동 처리.
    flush_pending_transfers(db, current_user.group_id)
    # 이번 달 배정 + 1일 복귀(전월 말일에 끝난 파견)를 한 번에 — 1일 복귀는 월경계 꼬리 보충에 쓴다(EI-11 11w-1 · D35).
    _assignments, _day1_returns = _assignments_with_day1_returns(db, current_user.group_id, req.year, req.month)
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
                # [EI-2a] weekly_off_type 주입은 죽은 전달이었다 — 읽는 곳이 없다(verify. 주의:
                #   nurse_effective.py 의 같은 이름 매핑은 NurseAssignment 를 직접 재조회하는
                #   별도 경로라 이 삭제와 무관하다).
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
    # ── 허용 근무형(allowed_shifts) 대상월 as-of 오버레이 (regular nurse) ──
    #   nurses.allowed_shifts 컬럼은 as-of-TODAY 단방향 캐시라 미래월 생성 시 stale 하다.
    #   예: 6월 N전담(['N'])→8월 해제([])를 미래발효로 저장하면, upsert_period 가 valid_from>today
    #   인 캐시투영을 건너뛰어 컬럼은 현재값(['N']) 그대로. 8월 생성 시 day-grain 하드제약은 period
    #   (=[], 제한없음)로 정확하지만, is_n_only_profile·objective·off-cap 은 컬럼(['N'])을 읽어
    #   8월인데 야간전담으로 오판 → 팀제외+야간편향 → D/E 실종. 대상월 as-of period 값으로 컬럼을
    #   맞춘다(SSOT 정합). period 미이행(row 없음)이면 컬럼 유지=무회귀. 인바운드는 위에서 처리됨.
    try:
        from services.nurse_period_resolver import fetch_periods as _fp_as, resolve_asof as _ra_as
        from db.models import NurseAllowedShiftPeriod as _AP_as
        _reg_ids = [str(n.nurse_id) for n in engine_nurses
                    if not bool(getattr(n, "is_inbound", False))]
        if _reg_ids:
            _as_rows = _fp_as(db, _AP_as, _reg_ids, month_start, month_start + timedelta(days=1))
            _overlaid = []
            for _en in engine_nurses:
                if bool(getattr(_en, "is_inbound", False)):
                    continue
                _rows = _as_rows.get(str(_en.nurse_id))
                if not _rows:
                    continue  # period 미이행 → 컬럼 유지(무회귀)
                _oldv = getattr(_en, "allowed_shifts", None)
                _newv = _ra_as(_rows, month_start, "allowed_shifts",
                               default=(_oldv or []))
                _en.__dict__['allowed_shifts'] = _newv
                if list(_newv or []) != list(_oldv or []):
                    _overlaid.append(f"{_en.nurse_id}:{_oldv}→{_newv}")
            if _overlaid:
                print(f"[AllowedShiftAsof] 대상월 as-of 오버레이({req.year}-{req.month}): {_overlaid}")
    except Exception as _as_exc:
        print(f"[AllowedShiftAsof] 오버레이 실패(무시): {_as_exc}")

    from services.team_period import resolve_teams_for_roster
    from services.cp_sat.allowed_shift_types import is_n_only_profile
    # (EI-13c) 간호사마다 부르던 구간·폴백 조회(N+1)를 일괄 1벌로 — N전담은 지금처럼 조회 대상이 아니다.
    _team_by_nurse = resolve_teams_for_roster(
        db, [str(_en.nurse_id) for _en in engine_nurses
             if not is_n_only_profile(getattr(_en, 'allowed_shifts', None))],
        current_user.group_id, req.year, req.month,
    )
    for _en in engine_nurses:
        # N전담(허용 shift=N뿐)은 팀 D/E 커버리지 로테이션에 참여 불가 → 미지정(team None).
        #   team_id=None 이면 team_constraints 가 자동 스킵 → 유령 멤버로 팀 인원/커버리지를
        #   부풀리지 않는다. 야간 수급은 글로벌(nig_req)이라 영향 없음.
        if is_n_only_profile(getattr(_en, 'allowed_shifts', None)):
            _en.__dict__['team_id'] = None
            continue
        _rt = _team_by_nurse.get(str(_en.nurse_id))
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

    # ── 보건휴가 대상자 선정 (health_leave_enabled=True 인 그룹만) ──
    #   ★ 날짜는 정하지 않는다. 대상자에게 OFF 하한만 1 올려두고(아래 플래그),
    #     생성이 끝나면 postprocess_health_leave 가 OFF 하나를 휴가코드로 바꾼다.
    #     실무는 보건 셀의 86.6% 가 OFF 에 인접해 연휴를 만든다(엑셀 238셀 실측).
    #   ★ 고정근무자도 후보에 넣는다 — 자동판정은 꺼져 있지만(planner 의 auto)
    #     health_leave_eligible=1 로 개인별로 켤 수 있어야 한다.
    health_leave_targets: set[str] = set()
    try:
        from services.leave.health_leave_planner import select_health_leave_targets
        health_leave_targets = select_health_leave_targets(
            db,
            group_id=str(current_user.group_id),
            year=req.year,
            month=req.month,
            engine_nurses=list(engine_nurses) + list(fixed_nurses),
            # active_range_candidates 는 engine_nurses 만 담으므로 고정근무자 몫을 더한다.
            active_range_map={
                **active_range_candidates,
                **{str(_fn.nurse_id): _active_range_in_month(_fn, month_start, days_in_month)
                   for _fn in fixed_nurses},
            },
            existing_requests=special_shift_requests,
        )
        # 솔버가 읽는 통로 — fallback_lex 가 nu.health_leave_extra_off 를 보고
        # compute_off_bounds(extra_min_off=1) + 상한 clamp 를 적용한다.
        for _n in engine_nurses:
            if str(getattr(_n, "nurse_id", "")) in health_leave_targets:
                _n.health_leave_extra_off = True
    except Exception as _hl_exc:
        health_leave_targets = set()
        print(f"[HealthLeave] 대상자 선정 실패(무시): {_hl_exc}")

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
    # ★ 래퍼가 미분류 실패를 기록할 수 있게 컨텍스트를 넘긴다(로컬 변수는 밖에서 못 본다).
    #   schedule 도 담는다 — blocked/infeasible 은 각각 db.delete(schedule) 로 draft 를
    #   지우는데 예외 경로에는 그게 없어, 실패할 때마다 병동 근무표 목록에 draft 가
    #   쌓인다(실측: dev 에 고아 54건 · schedule_entries 6,750행).
    #   ★★ 개입 판정을 **여기서 함께** 세운다. 아래 정식 계산부(_persistent_fix /
    #     _base_intervened)는 700줄 넘게 뒤라, 그 사이에 예외가 나면 래퍼가
    #     intervened=False 로 읽어 **개입 런을 프리셋에 낙인한다**(방침 4 위반).
    #     apply-resolution 의 임시 컬럼 델타가 그 구간에서 터지면, 원복될 값이
    #     "프리셋의 설정" 으로 스냅샷에 박제되기까지 한다.
    #     여기 쓰는 값은 전부 함수 인자·req 라 이 시점에 이미 확정돼 있다.
    if _ctx is not None:
        _ctx["config"] = latest_config
        _ctx["intervened"] = bool(
            _override_given or treatment_ids or column_delta_mode)
        _ctx["persistent_fix"] = {
            k: v for k, v in (
                ("weekend_off_release",
                 weekend_off_release or getattr(req, "weekend_off_release", None)),
                ("monthly_limit_release",
                 monthly_limit_release or getattr(req, "monthly_limit_release", None)),
                ("banned_wanted_release",
                 banned_wanted_release or getattr(req, "banned_wanted_release", None)),
                ("allowed_shift_add",
                 allowed_shift_add or getattr(req, "allowed_shift_add", None)),
            ) if v
        }
    shift_manage_data, daily_shift_requirements, daily_shift_requirements_by_day, daily_shift_requirements_max_by_day = _build_shift_manage_and_requirements(
        db, current_user, latest_config, req,
        # 엔진 대상이 없으면(전원 고정근무) 요구치 0 이 정상이므로 가드를 걸지 않는다.
        require_nonzero_requirements=bool(nurses_for_engine),
    )
    # daily_shift_requirements를 config에 주입해서 엔진 호출
    config_dict = latest_config.__dict__ if latest_config else {}
    config_dict['daily_shift_requirements'] = daily_shift_requirements
    # 일자별 요구치 우선 적용
    config_dict['daily_shift_requirements_by_day'] = daily_shift_requirements_by_day
    config_dict['daily_shift_requirements_max_by_day'] = daily_shift_requirements_max_by_day
    # N 블록 간 최소 간격 하드 — 모델 밖 컬럼이라 raw SQL 로 읽어 주입한다.
    #   컬럼이 없는 환경에서는 0(해제)이라 동작 불변. `config_override` 가 아래에서 덮으므로
    #   A/B·완화 재시도가 이 값을 이긴다(4차 완화가 0 으로 내려 재생성하는 경로).
    config_dict["n2n_min_gap"] = _n2n_min_gap_setting(db, current_user.group_id)
    # 요청에서 not_one_night가 들어오면 우선 적용 (없으면 DB 설정 유지)
    if getattr(req, "not_one_night", None) is not None:
        config_dict["not_one_night"] = bool(req.not_one_night)
    # 런타임 config override(비-DB-컬럼 solver 파라미터 포함) — apply-resolution의 probe/비컬럼 옵션이
    # DB 커밋 없이 이번 생성에만 완화값을 주입할 때 사용. config_dict 최종 병합(가장 우선).
    if config_override:
        config_dict.update(config_override)
    # 1일 복귀 간호사(전월 말일에 끝난 파견) → 상대 병동·파견 시작일. 월경계 꼬리를 상대 병동 전월 근무표로 보충한다
    #   (EI-11 11w-1 · D35). 서버 내부 값이라 요청으로 오면 가드가 400 으로 막고, 비면 키를 두지 않는다(남은 값도 지운다).
    _return_src = {str(a.nurse_id): [str(a.target_group_id), a.start_date.isoformat()]
                   for a in sorted(_day1_returns, key=lambda a: a.start_date) if str(a.nurse_id) in engine_nurse_ids}
    if _return_src:
        config_dict["_return_source_map"] = _return_src
        print(f"[Assignment] 1일 복귀(전월 말일 파견 종료) 꼬리 보충 대상: {_return_src}")
    else:
        config_dict.pop("_return_source_map", None)
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
    _apply_distribution_policy_from_req(config_dict, req)
    # 경계 제약 기능 기본값
    config_dict.setdefault("cross_month_hard_rules_enable", True)
    config_dict.setdefault("cross_month_lookback_days", 6)
    # 경계·상한을 걸려고 읽은 상대 병동 근무표(월중 경계 · 두 병동 나이트 합산) — 성공 응답의 안내(EI-11 11w-2 · D38)
    _other_ward_refs: list[dict] = []
    # 월중 경계(합류·복귀·출발) 기록 — 확정 칸이 정해진 뒤 조정한다(EI-11 11w-3 · D32)
    _mid_boundaries: list[dict] = []
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
        # 상대 병동 근무코드 맵(병동 → 맵) — 월중 경계 빌더가 읽은 병동은 아래 #6 나이트 세기가 다시 읽지 않는다(EI-13 12u)
        _night_code_maps: dict[str, dict] = {}
        if _inbound_assignments or _outbound_assignments:
            try:
                _code2main = _build_code_to_main_map(shift_manage_data)
                mid_constraints = build_mid_month_boundary_constraints(
                    db, _inbound_assignments, current_user.group_id,
                    req.year, req.month, config_dict, _code2main,
                    outbound_assignments=_outbound_assignments,
                )
                _night_code_maps.update(mid_constraints.get('code_maps') or {})
                _other_ward_refs += mid_constraints.get('other_ward_refs') or []
                _mid_boundaries = list(mid_constraints.get('boundaries') or [])
                # dict 가 아니면 새로 시작한다 — 예외로 월중 경계 몫을 통째로 건너뛰지 않게(EI-11 11r · 요청 내부 키는 관문이 막는다).
                _ic = config_dict.get("initial_constraints")
                _ic = _ic if isinstance(_ic, dict) else {}
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
                    ow = config_dict.get("off_window_constraints")
                    ow = ow if isinstance(ow, dict) else {}
                    for nid, windows in mid_ow.items():
                        ow.setdefault(nid, []).extend(windows)
                    config_dict["off_window_constraints"] = ow
            except Exception as e:
                # 삼키면 월중 경계 하드락 없이 근무표가 나간다(fail-open · 배정 하나의 오류가 전체 몫을 지운다) — 생성을 멈춘다(EI-11 11q G5).
                print(f"[MidMonth] mid-month 경계 제약 생성 실패 — 생성 중단(월중 경계 하드락 없이 만들지 않는다): {type(e).__name__}: {e}")
                raise
        # ── 상대 그룹 OFF 카운트 → off cap 조정용 · 같은 달 상대 병동 나이트 → 하드락 #6(EI-11 11q 리뷰) ──
        _other_group_offs: dict[str, int] = {}
        _other_group_nights: dict[str, int] = {}
        _nights_counted: set[tuple[str, str]] = set()
        _off_codes = {'O', 'OFF', '주'}
        for a in (_inbound_assignments + _outbound_assignments):
            nid = str(a.nurse_id)
            other_gid = a.source_group_id if a.target_group_id == current_user.group_id else a.target_group_id
            if not other_gid:
                continue
            other_sid, other_basis = _query_schedule_ref_for_month(db, other_gid, req.year, req.month)
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
                if (nid, str(other_sid)) not in _nights_counted:
                    _nights_counted.add((nid, str(other_sid)))
                    _other_ward_refs.append(_other_ward_ref("night_sum", nid, other_gid, req.year, req.month,
                                                            other_sid, other_basis))
                    # 그 병동 배정 날짜만 센다 — 다른 병동 배정 날의 남은 칸까지 세면 두 병동 합이 부푼다(EI-13 12w · Codex 3차 P2)
                    _ward_days = _other_ward_days(_inbound_assignments + _outbound_assignments, nid, other_gid,
                                                  current_user.group_id, month_start, days_in_month,
                                                  _blocked_by_id.get(nid, set()))
                    _n_cnt = _count_other_ward_nights(db, other_entries, _ward_days,
                                                      _build_code_to_main_map(shift_manage_data), other_gid,
                                                      _night_code_maps)
                    if _n_cnt:
                        _other_group_nights[nid] = _other_group_nights.get(nid, 0) + _n_cnt
            except Exception as e:
                # 같은 조회로 하드락 #6(상대 병동 나이트)을 세므로 삼키지 않는다 — 못 세면 막는 쪽(EI-11 11q 리뷰).
                print(f"[OffCap][CrossGroup] nurse={nid} 상대 병동 근무 조회 실패 — 생성 중단: {type(e).__name__}: {e}")
                raise
        if _other_group_offs:
            config_dict["other_group_offs"] = _other_group_offs
        if _other_group_nights:
            _active_here = {
                str(n.nurse_id): _active_days_here(active_range_candidates.get(str(n.nurse_id)),
                                                   _blocked_by_id.get(str(n.nurse_id), set()))
                for n in nurses_for_engine
            }
            _night_caps = _apply_cross_ward_night_caps(nurses_for_engine, _other_group_nights, _active_here, config_dict)
            print(f"[NightCap][CrossGroup] 상대 병동 나이트 {_other_group_nights} → 이 병동 월 N 상한(n_max) {_night_caps}")
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
        # 프리셉티 기간은 아래 nurse_preceptee_period(SSOT) as-of 빌드로 일원화한다
        # (이전의 _assignments/preceptor_id 캐시 직독 블록 제거 — follow 누수 원인이었음).
    # ── 프리셉티 기간: nurse_preceptee_period(SSOT) as-of 대상월 (캐시·assignment 직독 안 함) ──
    # 형태: {nurse_id: {"preceptor_id": pid, "days": set(0-based)}}. 그 달 미겹침=키 부재(=독립).
    # 종료월 다음달은 resolver 가 구조적으로 제외 → "기간 종료 후 follow" 버그 원천 차단.
    from services.preceptee_period import resolve_preceptee_days_for_month as _resolve_pte_days
    from db.models import NursePrecepteePeriod as _NPP
    _pte_nids = [str(n.nurse_id) for n in nurses_for_engine]
    _preceptee_period = _resolve_pte_days(db, _pte_nids, req.year, req.month)
    # 권위 판정: 이 그룹 간호사에 period row 가 하나라도 있으면(=백필됨) period 가 진실.
    # → 빈 맵이어도 '권위 모드'로 보내 종료자(캐시 잔존)를 확실히 제외. period 전무면(pre-backfill)
    #   엔진이 캐시 폴백(무회귀). bool(map) 만으로는 '데이터 없음'과 '활성자 없음'을 구분 못함(버그 원인).
    _pte_authoritative = bool(
        _pte_nids and db.query(_NPP.id).filter(_NPP.nurse_id.in_(_pte_nids)).first() is not None
    )
    config_dict["preceptee_period_authoritative"] = _pte_authoritative
    if _pte_authoritative or _preceptee_period:
        config_dict["preceptee_period_by_nurse_id"] = _preceptee_period  # 빈 dict 도 설정(권위 모드)
    for _nid, _info in _preceptee_period.items():
        print(f"[Assignment][Preceptee] nurse_id={_nid}, preceptor={_info['preceptor_id']}, "
              f"follow_days={sorted(_info['days'])}")
    print(f"[Assignment][Preceptee] authoritative={_pte_authoritative}, active={len(_preceptee_period)}")
    # ── 상호 근무 배제: nurse_mutual_exclusion_period(SSOT) as-of 대상월 ──
    # 형태: {nurse_id: {"partner_id": pid, "days": set(0-based)}}. 프리셉티 follow 의 배반(같은날 같은근무조 회피·소프트).
    try:
        from services.mutual_exclusion_period import (
            resolve_mutual_exclusion_days_for_month as _resolve_mutex_days,
        )
        _mutex_period = _resolve_mutex_days(db, _pte_nids, req.year, req.month)
        if _mutex_period:
            config_dict["mutual_exclusion_by_nurse_id"] = _mutex_period
            for _nid, _info in _mutex_period.items():
                print(f"[Assignment][MutualExcl] nurse_id={_nid}, partner={_info['partner_id']}, "
                      f"days={sorted(_info['days'])}")
        print(f"[Assignment][MutualExcl] active={len(_mutex_period)}")
    except Exception as _e_mx:
        print(f"[Assignment][MutualExcl] 로딩 실패(무시): {_e_mx}")
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
    if config_dict.get('preceptee_on', True):
        # 프리셉티 판정 = nurse_preceptee_period(SSOT) 그 달 as-of 맵(캐시 preceptor_id 안 봄).
        _pte_month_ids = set((config_dict.get("preceptee_period_by_nurse_id") or {}).keys())
        for nurse in nurses_for_engine:
            nid = str(nurse.nurse_id)
            if nid not in _pte_month_ids:
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
        # 정책(사용자 지시): preceptee 팔로우 기본 활성. 값 없으면 True (우발적 False 방지).
        _preceptee_on_val = config_dict.get("preceptee_on", True)
        print(
            f"[RosterCreate] 프리셉티 fixed_wanted 맵 구성 조건: preceptee_on={_preceptee_on_val}, all_fixed_entries={len(all_fixed_entries)}건"
        )
        if _preceptee_on_val:
            _pte_fw_map: dict[tuple[int, int], str] = {}
            _pte_ids = set()
            _pte_names: dict[str, str] = {}
            # 프리셉티 판정 = nurse_preceptee_period(SSOT) 그 달 as-of 맵(캐시 preceptor_id 안 봄).
            _pte_period_map = config_dict.get("preceptee_period_by_nurse_id") or {}
            for nurse in nurses_for_engine:
                _nid = str(nurse.nurse_id)
                if _nid in _pte_period_map:
                    _pte_ids.add(_nid)
                    _pte_names[_nid] = getattr(nurse, "name", "?")
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
        _rules_L = rules_from_config(config_dict).L
        for _ni, _n_days in _n_cells_by_nurse.items():
            _sorted = sorted(_n_days)
            _work_days = {
                _di for _cc in combined_fixed_cells if _cc.get("nurse_index") == _ni and (_di := _cc.get("day_index")) is not None
                and str(_cc.get("shift") or "").upper() not in ("O", "OFF", "주")
                and str(_cc.get("shift_type") or "").strip() not in ("휴가", "공가", "주휴")
            }
            # N블록 종료 감지: 다음 날이 N이 아닌 연속 N의 마지막
            for i, d in enumerate(_sorted):
                if d + 1 not in _n_days and i > 0 and _sorted[i - 1] == d - 1:
                    # 연속 나이트 상한(L) 아래이고 다음 날이 엔진 칸이면 엔진이 N 으로 이어 갈 수 있다(NNN OO) — 회복은 블록이
                    # 끝나는 길이에 맞게 엔진(#4·#5)이 건다. 그래도 둘째 회복일(d+2)은 NN OO 든 NNN OO 든 OFF 다 — 그 날만 박는다
                    # (precheck 가 그 날을 쉬는 날로 세는 것은 맞다). 이어 갈 수 없는 블록은 d+1·d+2 모두(12z · Codex 5차 P2).
                    _start = d
                    while _start - 1 in _n_days:
                        _start -= 1
                    _extendable = fixed_n_block_can_extend(_start, d, days_in_month, _work_days, _rules_L)
                    # d가 2+ 연속 N의 마지막 → recovery d+1, d+2
                    for r in ((d + 2,) if _extendable else (d + 1, d + 2)):
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

    # ── 월중 경계 × 확정 칸 조정(EI-11 11w-3 · D32) — precheck 앞(L0·L1 같은 값 · 사다리·스냅숏 탐색이 그대로 받는다) ──
    if _mid_boundaries and combined_fixed_cells:
        _reconcile_mid_month_with_fixed(config_dict, _mid_boundaries, combined_fixed_cells, nurses_for_engine,
                                        shift_manage_data, special_shift_map, active_range_candidates, days_in_month,
                                        db=db, office_id=current_user.office_id, group_id=current_user.group_id)

    # ── ontology treatment 적용: 선택된 treatment_ids 를 config 에 패치(재생성용) ──
    # apply_treatments 가 force_soft_mode/disable_module/set_threshold 를 patched_config 로
    # 반영(대부분 런타임 플래그). data_correction_required(manual)는 적용 안 됨.
    # ★ precheck 앞에서 적용해야 한다: team_min_soft_fallback 같은 완화가 precheck 의
    #   team-min 하드 블록을 실제로 풀 수 있어야 하기 때문(적용이 precheck 뒤면 무효).
    if treatment_ids:
        try:
            from services.policy.card_filter import assert_treatment_patch_allowed
            from services.treatment_applicator import apply_treatments
            _clean_cfg = {k: v for k, v in config_dict.items() if not str(k).startswith("_sa_")}
            _tres = apply_treatments(list(treatment_ids), _clean_cfg)
            assert_treatment_patch_allowed(_tres.patched_config)  # 적용 직후 재검(EI-12 · BR-3) — 관문을 지난 뒤의 마지막 방어선
            config_dict = _tres.patched_config
            print(f"[TreatmentApply] applied={_tres.applied_treatment_ids} "
                  f"manual={_tres.manual_required} unresolved={_tres.unresolved_treatment_ids}")
        except HTTPException:
            raise
        except Exception as _tapp_exc:
            print(f"[TreatmentApply] 실패(무시): {_tapp_exc}")

    # ── Precheck: 솔버 호출 전 산술적 infeasibility 검사 ──
    _fixed_cut_done = False   # 고정근무 차감을 이미 했는가(EI-10g2 — precheck 앞에서 한다)
    _fx_inputs = None  # (EI-13c) 고정근무 병합이 공휴일 OFF·근무코드를 다시 읽지 않고 쓴다
    _first_attempt_cfg: dict | None = None
    _attempt_src: AttemptSource | None = None   # 첫 시도 직전 빌더가 읽은 DB 부품 — 시도마다 재조회 대신(EI-13a-2)
    _ei_first = None                             # 첫 시도 직전 엔진 입력 한 벌(14b-1b 사후 소비자가 쓴다)
    precheck_result: dict | None = None
    presolve_diag: dict | None = None   # 솔버 전 부족 조기진단(advisory) — 응답에 부착
    # ★ 실패 스냅샷용 — 아래 precheck 블록에서 채워진다. 여기서 미리 잡아두는 이유는
    #   solver 실패(infeasible) 지점이 그 블록 밖이라 스코프를 못 타기 때문이다.
    #   초기화가 없으면 infeasible 스냅샷이 DB 기본 조회로 떨어져 얇아진다
    #   (실측: blocked 32KB vs infeasible 15KB — 정작 분석이 더 필요한 쪽이 부실했다).
    _nurses_dict_for_precheck: list | None = None
    precheck_config: dict | None = None
    # (EI-10a·D27) 단계 A(PrecheckStage) — precheck try 첫 문장에서 짓는다. 엔진 대상이
    #   0명이면(바로 아래 분기) 짓지 않는다 — 두 소비자(precheck·나이트)가 모두 건너뛴다.
    stage_a = None  # PrecheckStage | None
    # ★ 개입에는 두 종류가 있고 **낙인 규칙이 반대**다.
    #
    #   일시적(transient) — 이번 런에만 적용되고 DB 는 그대로다.
    #     config_override(해결책 카드의 config 델타) · treatment_ids(온톨로지 처방) ·
    #     applied_relaxations(team_min hard→soft 자동 완화, solver 단계에서 정해져
    #     각 낙인 지점에서 더한다).
    #     → 다음 생성 때는 원래 설정으로 돌아가므로 이 성패는 프리셋 값의 성패가
    #       아니다. **낙인하지 않는다.**
    #
    #   영구(persistent) — 간호사·원티드 데이터를 실제로 고치고 commit 한다.
    #     weekend_off_release(NurseWeekendOffPeriod) · monthly_limit_release
    #     (NurseMonthlyLimit) · banned_wanted_release(BannedWantedEntry.is_applied) ·
    #     allowed_shift_add(NurseAllowedShiftPeriod).
    #     → 조건 자체가 바뀌었고 그 상태가 유지된다. 그 뒤의 성패는 "고쳐진 조건에서
    #       이 프리셋이 되는가" 라는 유효한 답이므로 **낙인한다.**
    #       특히 성공했는데 낙인을 생략하면, 사용자가 화면에서 문제를 고쳐 생성에
    #       성공했는데도 이전 실패 기록이 남아 프리셋이 계속 숨겨진다.
    #   ★ 판정은 **적용부와 같은 소스**를 봐야 한다. 네 옵션 모두 위에서
    #     `인자 or getattr(req, ..., None)` 으로 읽어 적용하므로, 여기서 명시 인자만
    #     보면 호출자가 req 에만 실어 보낸 경우를 놓친다 — 데이터를 실제로 고쳤는데
    #     스냅샷에는 persistent_fix=null 로 남아 "아무것도 안 고쳤다" 로 읽힌다
    #     (worker 가 banned_wanted_release·allowed_shift_add 를 명시 전달하지 않아
    #      비동기 경로에서 실제로 발생하던 문제).
    _persistent_fix = {
        k: v for k, v in (
            ("weekend_off_release",
             weekend_off_release or getattr(req, "weekend_off_release", None)),
            ("monthly_limit_release",
             monthly_limit_release or getattr(req, "monthly_limit_release", None)),
            ("banned_wanted_release",
             banned_wanted_release or getattr(req, "banned_wanted_release", None)),
            ("allowed_shift_add",
             allowed_shift_add or getattr(req, "allowed_shift_add", None)),
        ) if v
    }
    #   ★ apply-resolution 의 컬럼 델타(column_delta_mode)는 **DB 컬럼을 임시로
    #     commit 했다가 finally 에서 되돌린다**. config_override 로는 안 넘어오므로
    #     따로 받아야 한다. 안 그러면 원복될 값의 성패가 프리셋에 찍혀,
    #     · 임시 값으로 성공 → 검증 안 된 원본이 success 로 보호되고
    #     · 임시 값으로 실패 → 멀쩡한 원본이 목록에서 사라진다.
    #     'persist' 는 **성공 시에만** 값이 유지되므로(실패는 원복) 성공만 낙인한다.
    _base_intervened = bool(_override_given or treatment_ids or column_delta_mode)
    # 성공 지점용 — persist 요청은 성공하면 값이 유지되니 그 성공은 프리셋의 진짜 결과다.
    _success_intervened_base = bool(
        _override_given or treatment_ids or column_delta_mode == "temporary"
    )
    if _base_intervened:
        print(f"[ConfigGenerateStatus] 일시적 개입 — override={bool(config_override)} "
              f"treatment={bool(treatment_ids)} column_delta={column_delta_mode!r} "
              f"→ 실패는 낙인 생략"
              + ("" if column_delta_mode == "persist"
                 else " · 성공도 낙인 생략"))
    if _persistent_fix:
        print(f"[ConfigGenerateStatus] 영구 수정 — {list(_persistent_fix)} "
              f"→ 조건이 바뀌었으므로 기록은 유지(성공하면 프리셋 복귀)")
    if _ctx is not None:
        # 위(latest_config 자리)에서 선반영한 값을 정식 계산 결과로 확정한다.
        _ctx["intervened"] = _base_intervened
        _ctx["persistent_fix"] = _persistent_fix
    # ★ 엔진 대상이 0명이면 검사 자체가 성립하지 않는다.
    #   전원이 fixed_shift 인 병동(수술실 등)은 솔버를 우회해 평일=코드로 채워지므로
    #   `if nurses_for_engine:`(아래 솔버 호출부)이 이미 엔진을 건너뛴다. 그런데 precheck 는
    #   그 가드가 없어 **간호사 0명·요구 0** 으로 돌았고, `_ensure_grade1_default` 의
    #   grade-1 floor(D/E/N 각 1)가 요구 0 을 넘어서 GRADE_MIN_SUM_EXCEEDS_NEED 로 항상 막혔다.
    #   (2026-08-14 인천의료원 수술실 실측: 19명 전원 고정근무인데 생성이 불가)
    if not nurses_for_engine:
        print("[Precheck] 엔진 대상 0명(전원 고정근무) — 검사 건너뜀")
        precheck_result = None
        # ★ 이 경로에서도 스냅샷 config 는 채운다. 안 채우면 `config` 가 통째로 {} 인
        #   기록이 남아 "그때 설정이 뭐였나" 를 못 본다 — 정작 이런 특수 병동
        #   (전원 고정근무)이 분석이 더 필요한 쪽이다. team_min 보강은 precheck
        #   블록에서만 하므로 여기 값은 그만큼 얇지만, 비는 것보다 낫다.
        precheck_config = dict(config_dict)
        if _ctx is not None:
            # ★ 이 분기에서도 채운다. 안 채우면 미분류 실패 시 스냅샷이 8필드짜리
            #   DB 폴백으로 떨어지는데, 정작 이런 특수 병동이 분석이 더 필요하다.
            _ctx["nurses"] = _nurses_dict_for_precheck
            _ctx["config_dict"] = precheck_config
    else:
        try:
            # (EI-10a·D27) 단계 A — 이 자리 지역 변수로 PreludeState 를 짓고 지연 getter 4종을
            #   연다(조회 0). try 첫 문장인 이유: 조립이 실패해도 아래 '[Precheck] 실행 실패'
            #   길로 가고, 이 뒤에서 예외가 나도(기록 실패 등) 나이트 블록이 단계 A 를 그대로 쓴다.
            stage_a = stage_a_at_precheck(
                db, office_id=current_user.office_id, group_id=current_user.group_id,
                year=req.year, month=req.month, config_id=getattr(req, "config_id", None),
                job_id=job_id, config_dict=config_dict, nurses_for_engine=nurses_for_engine,
                combined_fixed_cells=combined_fixed_cells, shift_manage_data=shift_manage_data,
            )
            # (EI-10g2) 고정근무 차감 — precheck 앞으로. precheck·그래프 소비자·실패 스냅숏이 엔진이 푸는 수요(차감 뒤)를 본다.
            #   `stage_a.config_dict` 는 위에서 깊은 사본으로 지어져 **차감 전** 값 그대로다(`demand.pre`). 조건·쿼리 순서·예외 처리는 옛
            #   블록과 같다(차감 실패는 생성을 막지 않는다).
            if fixed_nurses:
                try:
                    _fx_inputs = read_fixed_cut_inputs(
                        db, fixed_nurses, office_id=current_user.office_id,
                        group_id=current_user.group_id, year=req.year, month=req.month,
                        weekly_off_active=bool(config_dict.get("weekly_off_settings_activate", False)),
                        shift_lookup_getter=(stage_a.shift_lookup.get if stage_a is not None else None),
                    )
                    _fx_demand = compute_fixed_cut_demand(
                        _fx_inputs, fixed_special_shift_requests, config_dict,
                        year=req.year, month=req.month,
                    )
                    apply_fixed_cut_demand(config_dict, _fx_demand, n_fixed=len(fixed_nurses))
                except Exception as _fx_exc:
                    # 차감 실패는 생성을 막지 않는다(기존 동작으로 진행). 다만 초과가 남을 수 있다.
                    print(f"[FixedShiftCoverage] 차감 실패(무시): {type(_fx_exc).__name__}: {_fx_exc}")

            _fixed_cut_done = True
            # (EI-10g3) 단계 B(허용근무·전월 꼬리·금지 원티드 …) — 첫 시도 직전에 짓던 빌더를 precheck 앞으로. 이 자리의 config_dict 는
            #   차감 뒤이고 이후 첫 시도까지 precheck·나이트 블록이 바꾸지 않아(단계 A 는 불변) 옛 자리 값과 같다. 이 단계에서는 precheck 가
            #   결과를 쓰지 않는다(10g4~). 차단 경로도 이제 빌더 조회를 한다(S9·S9-F +6 · precheck 차단 +7).
            _first_attempt_cfg = {k: v for k, v in config_dict.items() if not str(k).startswith("_sa_")}
            _ei_first, _attempt_src = build_engine_input_at_first_attempt(
                db, stage_a, preferences=preferences, first_attempt_config=_first_attempt_cfg,
                advanced_inference=bool(getattr(req, "advanced_inference", False)))
            from services.precheck import (
                run_runtime_precheck,
                has_blocking_issues,
            )
            # (EI-10b) 등급 → 주말휴무 → 투영 → 팀(team_min_by_team 이 없을 때만). 조회
            #   자리·순서·예외 처리는 지금과 같다(views.to_precheck_input docstring 참조 —
            #   [온톨로지 데이터 단일 소스] 정찰/온톨로지에 넘길 nurse 속성 목록은
            #   `views.ONTOLOGY_NURSE_FIELDS` 로 옮겼다).
            _pin = to_precheck_input(stage_a, config_dict, ei=_ei_first)
            _engine_grade_config = _pin.grade_config
            _nurses_dict_for_precheck = _pin.nurses_dict
            precheck_config = _pin.config_dict
            if _ctx is not None:
                _ctx["nurses"] = _nurses_dict_for_precheck
                _ctx["config_dict"] = precheck_config

            precheck_result = run_runtime_precheck(**_pin.kwargs())
            # (EI-14b) 그래프 소비자 3곳(presolve·섀도·원인 브리지)이 받는 입력 — 조회 0, precheck 가 끝난 뒤에 짓는다.
            _ir = to_roster_ir(stage_a, config_dict, pin=_pin)
            # 솔버 전 부족 조기진단(advisory, non-blocking) — max-flow(per-day+월별) 기반.
            # 개인 속성(allowed_shifts/max_nig/weekend_off)은 불가침 → 복구 선택지는 관리자 노브만.
            # 실측 부족 수치는 솔버 후 coverage_gaps 가 담당(여기 값은 증명된 하한).
            try:
                from services.ontology_graph.presolve_diagnosis import presolve_shortage_diagnosis
                presolve_diag = presolve_shortage_diagnosis(
                    _ir.nurses, _ir.config, _ir.year, _ir.month)
                if presolve_diag.get("shortages"):
                    print(
                        f"[Presolve] 부족 예상 {len(presolve_diag['shortages'])}건 "
                        f"({presolve_diag['elapsed_ms']}ms): "
                        + "; ".join(
                            f"{s['shift']} 월부족≥{s['monthly_shortage_lower_bound']}({s['reason']})"
                            for s in presolve_diag['shortages']))
            except Exception as _psd_exc:
                print(f"[Presolve] 진단 실패(무시): {_psd_exc}")
                presolve_diag = None
            # 운영 shadow 진단(env AIDE_SHADOW_DIAGNOSIS 게이팅, 기본 off=no-op). 결과 무영향, 로그만.
            # request_id=새 run_id(fix1). input_hash·model_signature 는 **여기서 한 번 확정**해 graph·
            # production 로그에 동일 전달(fix2·3 — 재hash·모델차이로 인한 오분류 방지).
            # solve_context: run_id·attempt_seq·input_hash·model_signature 를 **여기서 한 번 확정**해
            # graph·production 양쪽이 동일 값을 쓴다(fix3·C — 양쪽 drift 방지, 확실히 pair).
            _gen_run_id = _gen_ih = _gen_sig = _gen_nd = None
            _gen_seq = 0
            try:
                import calendar as _cal
                import uuid as _uuid
                from services.ontology_graph.roster_ir import model_signature as _msig
                from services.ontology_graph.shadow_diagnosis import _input_hash as _ihf
                from services.ontology_graph.shadow_diagnosis import run_shadow
                _gen_run_id = _uuid.uuid4().hex
                _gen_nd = _cal.monthrange(req.year, req.month)[1]
                _gen_ih = _ihf(_ir.nurses, _ir.config, _gen_nd)
                _gen_sig = _msig(_ir.nurses, _ir.config, _gen_nd)
                run_shadow(_ir.nurses, _ir.config, _ir.year, _ir.month,
                           request_id=_gen_run_id, schedule_id=getattr(schedule, "schedule_id", None),
                           attempt_id="primary_hard", attempt_seq=_gen_seq,
                           input_hash=_gen_ih, model_signature=_gen_sig)
            except Exception:
                pass
            if has_blocking_issues(precheck_result):
                # 이 설정은 solver 에 넣기도 전에 막혔다 — 프리셋 목록에서 빼기 위해 기록.
                #   스냅샷도 남긴다: 어떤 인원·요구·원티드에서 막혔는지 사후 분석용.
                #   (EI-5) 기록·payload·카드 배관은 raise_presolve_block 공통 헬퍼로.
                #   원인 브리지(post-solve cause 가 못 도는 자리를 메우는 카드)는
                #   **기록이 끝난 뒤**(전환 전과 같은 순서) 지연 평가한다
                #   (`extra_findings`) — 기록이 실패해 솔버로 진행하는 경로에서는
                #   돌면 안 된다. BLOCKING 로그(건수·message·fix·"솔버 호출
                #   생략")도 카드 합류가 끝난 뒤 한 번에 찍는다(`log_after_merge`)
                #   — 전환 전과 같은 순서로, 기록이 실패하면 이 로그도 안 찍힌다.
                issue_codes = sorted({
                    str(i.get("reason_code", "?"))
                    for i in (precheck_result.get("issues") or [])
                })

                def _cb_extra_findings() -> list[PresolveFinding]:
                    _cb_finding = _build_precheck_cause_bridge_finding(
                        _ir.nurses, _ir.config, _ir.year, _ir.month)
                    return [_cb_finding] if _cb_finding is not None else []

                def _log_precheck_blocking(_payload: dict) -> None:
                    _inf = _payload.get("infeasibility", {})
                    print(
                        f"[Precheck][BLOCKING] {len(precheck_result.get('issues', []))}건 — "
                        f"codes={issue_codes}"
                    )
                    print(f"[Precheck][BLOCKING][message] {_inf.get('summary_message_ko')}")
                    for _s in (_inf.get("fix_suggestions_ko") or [])[:5]:
                        print(f"[Precheck][BLOCKING][fix] {_s}")
                    print("[Precheck][BLOCKING] 솔버 호출 생략. HTTP 500 응답.")

                # ★ 여기 config_dict 는 반드시 precheck_config(team_min_by_team 주입본)여야
                #   한다 — `config_dict`(주입 전)로 바꿔도 지금 특성화 시나리오(base_ward(2),
                #   팀 없음)에서는 둘 다 같은 값이라 안 잡힌다(EI-5 상속 결함, EI-5b 확인).
                #   팀이 있는 병동에서만 두 값이 갈린다.
                bctx = _presolve_block_ctx(
                    db=db, config=latest_config, year=req.year, month=req.month,
                    job_id=job_id, persistent_fix=_persistent_fix, intervened=_base_intervened,
                    nurses_dict=_nurses_dict_for_precheck, config_dict=precheck_config,
                    schedule=schedule, ctx=_ctx,
                )
                raise_presolve_block(
                    bctx, [],
                    base_issues=precheck_result.get("issues") or [],
                    record_first=True, on_record_fail="proceed", card_merge="prepend",
                    extra_findings=_cb_extra_findings, log_after_merge=_log_precheck_blocking,
                )
        except HTTPException:
            raise
        except Exception as _pre_exc:
            # precheck 자체가 실패하면 무시하고 솔버 진행
            print(f"[Precheck] 실행 실패(무시하고 솔버 진행): {_pre_exc}")
            precheck_result = None

    # ── 고정근무자 몫을 커버리지 요구에서 뺀다 ────────────────────────────────
    # ★★ 고정근무자(`fixed_shift`)는 `_split_fixed_nurses` 로 갈려 **솔버를 타지 않고**
    #   나중에 `fixed_roster` 로 병합된다. 그래서 솔버의 커버리지 식은 이들을 못 본다.
    #   요구 D=4 인 날 솔버가 4명을 채우고 거기에 고정근무자 1명이 얹혀 **5명**이 됐다
    #   (성남ICU-RN 2026-10 실측: 평일 11일 전부 정확히 +1 · E/N 은 0건 —
    #    고정 코드가 D 뿐이라 D 에만 생겼다).
    #   ★ `off_first` 와 무관한 결함이다. `off_first=True` 의 `assigned <= need` 하드는
    #     정상 작동하고 있었고, **그 상한이 보는 세계 밖에서** 1명이 더해진 것이다.
    #     `off_first=False` 일 때도 같은 +1 이 얹히지만 그쪽은 초과가 soft 라 덜 보였다.
    # 일자별로 빼야 한다 — 고정근무자는 평일만 근무하고 주말·공휴일은 OFF 다.
    # ★ EI-9-0 — 읽기(`read_fixed_cut_inputs`)·계산(`compute_fixed_cut_demand`)·대입
    #   (`apply_fixed_cut_demand`)세 함수로 나눴다(app/services/engine_input/fixed_cut.py).
    #   조건·쿼리 순서·대입 값·예외 처리(차감 실패는 생성을 막지 않는다)는 그대로다.
    #   ★단, O-3(EI9_SPEC.md §3.11) — by_day 값이 int() 불가 문자열 + 그 칸 max 활성이면
    #   대입 결과가 한 가지만 다르다(옛 코드는 by_day 만 대입 후 예외, 지금은 아무것도
    #   대입하지 않음). config_override 로만 도달 가능 — DB 값으로는 이 경로에 안 온다.
    # ★ (EI-10g2) 차감 본문은 precheck 블록의 단계 A 조립 직후로 옮겼다(precheck·그래프 소비자·실패 스냅숏이 차감 뒤 수요를 본다).
    #   여기는 단계 A 조립이 실패해 거기서 못 돈 경우의 안전망이다(옛 동작: precheck 가 실패해도 차감은 한다).
    if fixed_nurses and nurses_for_engine and not _fixed_cut_done:
        try:
            _fx_inputs = read_fixed_cut_inputs(
                db, fixed_nurses, office_id=current_user.office_id,
                group_id=current_user.group_id, year=req.year, month=req.month,
                weekly_off_active=bool(config_dict.get("weekly_off_settings_activate", False)),
                shift_lookup_getter=(stage_a.shift_lookup.get if stage_a is not None else None),
            )
            _fx_demand = compute_fixed_cut_demand(
                _fx_inputs, fixed_special_shift_requests, config_dict,
                year=req.year, month=req.month,
            )
            apply_fixed_cut_demand(config_dict, _fx_demand, n_fixed=len(fixed_nurses))
        except Exception as _fx_exc:
            # 차감 실패는 생성을 막지 않는다(기존 동작으로 진행). 다만 초과가 남을 수 있다.
            print(f"[FixedShiftCoverage] 차감 실패(무시): {type(_fx_exc).__name__}: {_fx_exc}")

    # ── 솔버 전 산술 차단: 간호사별 나이트 하한 합 > 그 달 나이트 칸 ─────────────────
    # ★ 엔진과 같은 입력(고정근무자 차감 뒤 config_dict · 엔진 간호사 · 고정 칸)으로 판정한다.
    #   개인 n_exact/n_min 합이 날짜별 N 상한의 합을 넘으면 다른 규칙과 무관하게 배정이 없다.
    #   솔버를 돌리지 않고 사전검사처럼 바로 막고, 근무자 관리 그 달로 보내는 카드 하나만 낸다.
    #   실사례(2026-10-08 운영 102598edcd0e 11월): 11월 한도 행이 없어 10월 값(합 31)이 이월,
    #   30일 × 하루 N 1명 = 30칸 → 솔버 INFEASIBLE 뒤 진단이 원인을 못 찾아 카드 0장이었다.
    # ★ 판정 실패는 생성을 막지 않는다(기존 흐름대로 솔버 진행). 판정 규칙은 오판이 없도록
    #   모르는 것을 크게 잡는다(`lagrangian.detect_night_forced_oversupply`).
    # (EI-5b, 2026-10-14) 탐지 자체(`detect_night_forced_oversupply` 호출 + Finding 조립)는
    #   `presolve_block.PRESOLVE_DETECTORS` 등록표로 옮겼다(설계서 R8). 여기 남은 일은
    #   view 조립(EI-10a `views.presolve_view` — 단계 A + 이 자리 config_dict, 쿼리 자리·
    #   순서 그대로)과 `run_presolve_detectors` 호출뿐이다. 등록표가 Finding 을 돌려주면
    #   (지금은 탐지기 1개라 0개 또는 1개)
    #   바로 아래 `raise_presolve_block` 으로 — 여러 탐지기가 각자 Finding 을 내도
    #   첫 번째에서 멈추지 않고 전부 모아 한 번에 넘긴다(정책은
    #   `run_presolve_detectors` docstring).
    if nurses_for_engine:
        _nfo_findings: list[PresolveFinding] = []
        _view_nfo = None
        try:
            # (EI-10a) 입력 = 단계 A(간호사·고정 칸·근무코드) + 이 자리 config_dict(고정근무
            #   차감 뒤). 이 try 는 view 조립(근무코드 조회 포함) 실패만 감싼다 — 지금과 같다.
            _view_nfo = presolve_view(stage_a, config_dict)
        except Exception as _nfo_exc:
            print(f"[NightOversupply] 판정 실패(무시하고 진행): {type(_nfo_exc).__name__}: {_nfo_exc}")
            _view_nfo = None
        # ★(EI-5b, 리뷰 반영) run_presolve_detectors 호출을 위 try **밖**으로 뺐다.
        #   산술 탐지 실패는 run_presolve_detectors 가 자체적으로 로그만 남기고
        #   다음 탐지기로 넘어가므로(`log_tag="[NightOversupply]"` 로 전환 전
        #   접두어 보존) try 밖에서 불러도 '탐지 실패 → 로그 후 진행'은 그대로다.
        #   반대로 **Finding 조립(카드·메시지 생성) 실패는 이 try 안에 있으면 안
        #   된다** — 전환 전에는 카드·메시지 생성이 `if _nfo:` 블록으로 이 try
        #   밖에 있어 실패하면 예외가 그대로 올라갔는데, EI-5 전환 때 탐지와
        #   조립이 한 호출(`run_presolve_detectors`)로 합쳐지며 이 try 안에
        #   같이 들어가 조립 실패까지 삼켜졌었다(역방향 감사로 재현해 되돌림).
        if _view_nfo is not None:
            _nfo_findings = run_presolve_detectors(_view_nfo, log_tag="[NightOversupply]")
        if _nfo_findings:
            # (EI-5) 이 원인은 산술로 확정이라 카드 하나만 낸다(card_merge="exclusive" —
            #   다른 일반 카드와 섞이면 안내가 갈린다). 기록은 payload 뒤(record_first=False)
            #   · 기록이 실패해도 차단은 그대로 한다(on_record_fail="block"). 기록 실패
            #   로그는 `[NightOversupply]` 접두어를 그대로 쓴다(log_tag).
            # BLOCKING 로그는 raise_presolve_block 안에서 카드 합류가 끝난 뒤
            #   찍힌다(log_after_merge) — 전환 전과 같은 순서.
            # ★ card_merge="exclusive"·on_record_fail="block"·log_tag 는 등록표가 낸
            #   findings 전부에 일괄 적용된다 — 탐지기가 하나뿐인 지금은 영향이
            #   없지만, R8 로 두 번째 탐지기가 들어오면 탐지기별 정책으로 나눠야
            #   한다(이 호출부는 지금 나이트 전용이다).
            def _log_night_blocking(_payload: dict) -> None:
                _ev = _nfo_findings[0].evidence or {}
                print(f"[NightOversupply][BLOCKING] 나이트 하한 합 {_ev.get('forced_n_sum')} > "
                      f"칸 {_ev.get('n_slots')} — 솔버 호출 생략. HTTP 500 응답.")

            bctx = _presolve_block_ctx(
                db=db, config=latest_config, year=req.year, month=req.month,
                job_id=job_id, persistent_fix=_persistent_fix, intervened=_base_intervened,
                nurses_dict=_nurses_dict_for_precheck, config_dict=precheck_config,
                schedule=schedule, ctx=_ctx, log_tag="[NightOversupply]",
            )
            raise_presolve_block(
                bctx, _nfo_findings,
                record_first=False, on_record_fail="block", card_merge="exclusive",
                log_after_merge=_log_night_blocking,
            )

    # UNDIAGNOSED 탐색 기준의 대체값(첫 시도 스냅샷이 없을 때). 아래 완화 사다리가 config_dict 의
    # 최상위 키를 누적 갱신(same_shift·isolated_work·n2n_min_gap)하므로 첫 시도 직전 값을 떠 둔다.
    if nurses_for_engine:
        if _first_attempt_cfg is None:   # precheck 앞(EI-10g3)에서 빌더가 못 돈 경우(단계 A 조립 실패)에만 여기서 만든다
            _first_attempt_cfg = {k: v for k, v in config_dict.items() if not str(k).startswith("_sa_")}
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
            time_limit_seconds=180 if bool(getattr(req, "advanced_inference", False)) else 60,
            config_override=config_dict,
            _assignments=_assignments,
            _inbound_assignments=_inbound_assignments,
            _outbound_assignments=_outbound_assignments,
            attempt_src=_attempt_src,
        )
        # 운영 primary_hard solve 상태를 shadow 로 상관로깅(env 게이팅, 무영향). graph 로그와
        # **같은 run_id·input_hash·model_signature** 로 join(fix1·2·3). raw solver status 우선.
        try:
            from services.ontology_graph.production_adapter import standardize_status
            from services.ontology_graph.shadow_diagnosis import log_production_status
            _raw = getattr(roster_system, "_primary_solver_status", None)
            _prod_status, _src = standardize_status(roster_system, generated, raw_status=_raw)
            log_production_status(_nurses_dict_for_precheck, precheck_config, req.year, req.month,
                                  _prod_status, request_id=_gen_run_id,
                                  schedule_id=getattr(schedule, "schedule_id", None),
                                  attempt_id="primary_hard", attempt_seq=_gen_seq,
                                  status_source=_src, raw_solver_status=_raw,
                                  input_hash=_gen_ih, model_signature=_gen_sig,
                                  production_model_stage="fallback_lex_stage1",
                                  production_validator_pass=None)  # validator 연결=운영 shift코드 확정 후
        except Exception:
            pass
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
    # (EI-13c) 고정근무자가 없으면 읽을 것이 없다(빈 명단 → 빈 로스터). 있으면 차감 블록이 이미 읽은 공휴일 OFF·근무코드를 쓰고,
    #   차감 블록이 안 돈 경우(전원 고정근무 등)만 여기서 읽는다.
    if not fixed_nurses:
        fixed_roster = {}
    else:
        if _fx_inputs is not None and _fx_inputs.shift_lookup is not None:
            _merge_holiday_off, _merge_shift_lookup = _fx_inputs.holiday_off, _fx_inputs.shift_lookup
        else:
            _merge_holiday_off = _holiday_off_enabled(db, current_user.group_id)
            _merge_shift_lookup = _load_shift_lookup(db, current_user.office_id, current_user.group_id)
        fixed_roster = _build_fixed_shift_roster(
            fixed_nurses,
            req.year,
            req.month,
            weekday_off_code="O",
            sunday_code="주",
            holiday_off=_merge_holiday_off,
            shift_lookup=_merge_shift_lookup,
            weekly_off_active=bool(config_dict.get("weekly_off_settings_activate", False)),
        )
    # 확정 원티드(fixed_wanted_entries, is_applied=True) 오버레이 — 고정근무자 평일 확정근무(예: OFF) 반영.
    # 솔버 우회로 평일=fixed_shift 만 채워지던 누락을 보정. 특수요청(휴가/교육)은 아래에서 최종 우선.
    fixed_roster = _overlay_fixed_roster_with_fixed_wanted(
        db, fixed_roster, current_user.group_id, req.year, req.month,
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
    )
    if validation_error and f"reason_code={_STAGE1_UNKNOWN_CODE}" in validation_error:
        # 1단계가 시간 안에 해를 못 찾았는데(해 없음 미증명) 기존 검증은 아무것도 못 잡았다 —
        # 예전엔 빈 배정이 '성공' 저장되던 경우다. 저장하지도, 확정 실패(완화 사다리·해결 카드·
        # 워커 ack)로 보내지도 않는다. TimeoutError 는 워커가 일시 장애로 보고 재시도하고
        # (`worker._is_transient`), draft 는 바깥 래퍼가 지운다(`_record_unclassified_failure`).
        # ★ 기존 검증이 '실근무 0건'·'하루 0명' 을 잡은 경우는 여기 안 온다 — 예전처럼 완화
        #   사다리로 간다(그 경로로 성공하던 작업을 막지 않는다 · Codex 8회차).
        raise TimeoutError(f"{validation_error} 일시 실패로 다시 시도합니다.")
    applied_relaxations: list[str] = []
    # 사다리가 config_dict 에 누적하는 하드락 해제를 최종 결과 기준으로 알리려고 원래 값을 잡아 둔다
    #   (`_carried_hardlock_relaxations`).
    _ss_orig_k = int(config_dict.get("same_shift_hard_k", 3) or 0)
    _ss_orig_on = _ss_orig_k > 0
    _iw_orig_on = bool(config_dict.get("isolated_work_hard", True))

    def _ladder_time_ok(_what: str) -> bool:
        """재시도 1회분 시간이 남았는가 — 없으면 건너뛰어 Lambda 제한 안에서 결과·실패를 낸다."""
        import os as _os_dl
        from services.cp_sat import gen_deadline
        _need = float(_os_dl.environ.get("ROSTER_LADDER_MIN_LEFT_SEC", "150") or 150)
        _left = gen_deadline.left()
        if _left >= _need:
            return True
        print(f"[RosterGenerate][Deadline] {_what} 재시도 생략 — 남은 {_left:.0f}초 < {_need:.0f}초")
        return False

    def _ladder_retry(_cfg: dict, _what: str):
        """완화 사다리 재시도 1회 — (생성 결과, roster_system, 검증 오류). 다른 단계와 같은 후처리."""
        if not _ladder_time_ok(_what):
            return None, None, f"{_what} 재시도 생략(생성 시간 예산 부족)"
        try:
            _g, _, _rs = _run_cp_sat_basic(
                db, current_user, nurses_for_engine, preferences, latest_config, req,
                shift_manage_data,
                fixed_cells=combined_fixed_cells if combined_fixed_cells else None,
                time_limit_seconds=180 if bool(getattr(req, "advanced_inference", False)) else 60,
                config_override=_cfg,
                _assignments=_assignments,
                _inbound_assignments=_inbound_assignments,
                _outbound_assignments=_outbound_assignments,
                attempt_src=_attempt_src,
            )
            _g = dict(_g) if isinstance(_g, dict) else {}
            _g.update(fixed_roster or {})
            _g.update(_alloff_roster or {})
            _e = _validate_generated_roster(
                _g, _rs,
                nurses_context=list(nurses_for_engine or []),
                config_context=_cfg,
            )
            return _g, _rs, _e
        except Exception as _exc:
            return None, None, f"{_what} 재시도 예외: {type(_exc).__name__}: {_exc}"

    if validation_error:
        # 자연 soft 트리거: 엔진이 실근무를 못 배정한 infeasible-empty 신호(실제 플래그)
        # 를 본다. 과거처럼 휴리스틱이 만든 NO_ASSIGNMENT/grade_max 문자열을 매칭하지 않음.
        _trigger_soft = bool(getattr(roster_system, "_infeasible_empty", False))

        # ── 1차 완화: team_min hard → soft (기존) ────────────────────────────
        if (validation_error and _trigger_soft and not bool(config_dict.get("_team_min_soft_retry_attempted"))
                and _ladder_time_ok("team_min soft")):
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
                attempt_src=_attempt_src,
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
            )
            if not retry_validation_error:
                generated = retry_generated
                roster_system = retry_rs
                # 정책: AUTO-SOFT는 team_min만 풀고 grade는 끝까지 hard 유지 (2026-05-23).
                # agent-qa-harness의 grade flip 패턴은 의도적으로 폐기됨.
                applied_relaxations.append("team_min_hard_to_soft")
                if _ctx is not None:
                    # ★ 완화도 일시적 개입이다(방침 4). 성공 낙인 전에 예외가 나면
                    #   래퍼가 이 사실을 몰라 개입 아닌 것으로 기록해 버린다.
                    _ctx["relaxed"] = True
                # Ontology treatment 어휘로도 노출 (agent-qa-harness의 dispatch 카탈로그 호환).
                applied_relaxations.append("treatment:soft:team_min")
                if _ctx is not None:
                    # ★ 완화도 일시적 개입이다(방침 4). 성공 낙인 전에 예외가 나면
                    #   래퍼가 이 사실을 몰라 개입 아닌 것으로 기록해 버린다.
                    _ctx["relaxed"] = True
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

        # ── 2차 완화: same_shift hard → soft ──────────────────────────────────
        # ★★ 순서 주의 — **team_min 을 먼저 푼다.** 처음엔 "나중에 얹은 제약부터" 라는
        #   생각으로 same_shift 를 앞에 뒀는데 실측에서 뒤집혔다: 이 병동의 INFEASIBLE
        #   원인은 team_min(1/1/1) 이라 same_shift 만 풀면 **1차가 실패**하고, 결국
        #   team_min 까지 풀리면서 **지킬 수 있었던 same_shift 하드까지 잃었다**
        #   (같은시프트 4연속 0~1건 → 7건). 원인 아닌 제약을 먼저 풀면 손해만 본다.
        # 전환 방식: `same_shift_hard_k` 를 0 으로 내린다 → 하드가 사라지고
        #   기존 `max_same_shift` soft 벌점(D/E/N 4연속 창)이 그대로 이어받는다.
        #   별도 slack 을 만들지 않는 이유는 그 soft 인프라가 이미 있기 때문이다.
        # ★ 기본값(3)을 함께 본다 — `same_shift_hard_k` 는 **DB 컬럼이 아니라서**
        #   `config_dict`(= latest_config.__dict__) 에 키 자체가 없다. 기본값을 빼고
        #   `.get(...) or 0` 로만 보면 **항상 False** 가 되어 이 완화가 죽는다(실측).
        _ss_hard_on = int(config_dict.get("same_shift_hard_k", 3) or 0) > 0
        # ★ `validation_error` 필수 — 1차(team_min)로 이미 살아났으면 여기 들어오면 안 된다.
        #   빠뜨리면 성공한 해를 버리고 same_shift 까지 푼 해로 덮어쓴다.
        if (validation_error and _trigger_soft and _ss_hard_on
                and not bool(config_dict.get("_same_shift_soft_retry_attempted"))
                and _ladder_time_ok("same_shift soft")):
            print("[SameShiftFallback] infeasible 감지 → same_shift hard→soft 자동 전환으로 1회 재시도 "
                  "(team_min·grade hard 유지)")
            _ss_cfg = dict(config_dict)
            _ss_cfg["same_shift_hard_k"] = 0
            _ss_cfg["_same_shift_soft_retry_attempted"] = True
            try:
                _ss_generated, _, _ss_rs = _run_cp_sat_basic(
                    db, current_user, nurses_for_engine, preferences, latest_config, req,
                    shift_manage_data,
                    fixed_cells=combined_fixed_cells if combined_fixed_cells else None,
                    time_limit_seconds=180 if bool(getattr(req, "advanced_inference", False)) else 60,
                    config_override=_ss_cfg,
                    _assignments=_assignments,
                    _inbound_assignments=_inbound_assignments,
                    _outbound_assignments=_outbound_assignments,
                    attempt_src=_attempt_src,
                )
                if isinstance(_ss_generated, dict):
                    _ss_generated.update(fixed_roster)
                else:
                    _ss_generated = fixed_roster
                if _alloff_roster:
                    if isinstance(_ss_generated, dict):
                        _ss_generated.update(_alloff_roster)
                    else:
                        _ss_generated = dict(_alloff_roster)
                _ss_err = _validate_generated_roster(
                    _ss_generated, _ss_rs,
                    nurses_context=list(nurses_for_engine or []),
                    config_context=_ss_cfg,
                )
            except Exception as _ss_e:
                _ss_err = f"same_shift soft 재시도 예외: {type(_ss_e).__name__}: {_ss_e}"
                _ss_generated = _ss_rs = None
            if not _ss_err:
                generated = _ss_generated
                roster_system = _ss_rs
                applied_relaxations.append("same_shift_hard_to_soft")
                if _ctx is not None:
                    _ctx["relaxed"] = True
                weekly_off_warnings.append(build_relaxation_notice("same_shift", {
                    "type": "same_shift_hard_to_soft_applied",
                    "detail": ("같은 시프트 연속 하드 제한이 infeasible 로 자동 soft 전환되어 재생성됐습니다. "
                               "일부 간호사에게 같은 시프트가 4회 이상 연속될 수 있습니다."),
                }, roster_system=roster_system, nurses=nurses_for_engine, year=req.year, month=req.month,
                    param=_ss_orig_k))
                print("[SameShiftFallback][AUTO-SOFT][success] same_shift hard→soft 자동 전환으로 근무표 생성. "
                      "applied_relaxations=['same_shift_hard_to_soft']")
                validation_error = None
            else:
                # 실패 → 이후 완화 단계가 **same_shift soft 를 물고** 가도록 남긴다.
                print(f"[SameShiftFallback][AUTO-SOFT][fail] 재시도 실패: {_ss_err}")
                config_dict["same_shift_hard_k"] = 0
                config_dict["_same_shift_soft_retry_attempted"] = True

        # ── 3차 완화: isolated_work hard → soft ───────────────────────────────
        # 전환 방식: `isolated_work_hard` 를 False 로 내린다 → 하드가 사라지고 기존
        #   `ISOLATED_WORK_PENALTY`(1500) soft 벌점이 그대로 이어받는다.
        # ★ 앞 단계(team_min·same_shift)가 풀린 상태를 **물고** 간다 — config_dict 가
        #   누적 갱신되므로 여기까지 왔다는 건 그것들로도 안 풀렸다는 뜻이다.
        _iw_hard_on = bool(config_dict.get("isolated_work_hard", True))
        if (validation_error and _trigger_soft and _iw_hard_on
                and not bool(config_dict.get("_isolated_work_soft_retry_attempted"))
                and _ladder_time_ok("isolated_work soft")):
            print("[IsolatedWorkFallback] infeasible 감지 → isolated_work hard→soft 자동 전환으로 "
                  "1회 재시도")
            _iw_cfg = dict(config_dict)
            _iw_cfg["isolated_work_hard"] = False
            _iw_cfg["_isolated_work_soft_retry_attempted"] = True
            try:
                _iw_generated, _, _iw_rs = _run_cp_sat_basic(
                    db, current_user, nurses_for_engine, preferences, latest_config, req,
                    shift_manage_data,
                    fixed_cells=combined_fixed_cells if combined_fixed_cells else None,
                    time_limit_seconds=180 if bool(getattr(req, "advanced_inference", False)) else 60,
                    config_override=_iw_cfg,
                    _assignments=_assignments,
                    _inbound_assignments=_inbound_assignments,
                    _outbound_assignments=_outbound_assignments,
                    attempt_src=_attempt_src,
                )
                if isinstance(_iw_generated, dict):
                    _iw_generated.update(fixed_roster)
                else:
                    _iw_generated = fixed_roster
                if _alloff_roster:
                    if isinstance(_iw_generated, dict):
                        _iw_generated.update(_alloff_roster)
                    else:
                        _iw_generated = dict(_alloff_roster)
                _iw_err = _validate_generated_roster(
                    _iw_generated, _iw_rs,
                    nurses_context=list(nurses_for_engine or []),
                    config_context=_iw_cfg,
                )
            except Exception as _iw_exc:
                _iw_err = f"isolated_work soft 재시도 예외: {type(_iw_exc).__name__}: {_iw_exc}"
                _iw_generated = _iw_rs = None
            if not _iw_err:
                generated = _iw_generated
                roster_system = _iw_rs
                applied_relaxations.append("isolated_work_hard_to_soft")
                if _ctx is not None:
                    _ctx["relaxed"] = True
                weekly_off_warnings.append(build_relaxation_notice("isolated_work", {
                    "type": "isolated_work_hard_to_soft_applied",
                    "detail": ("고립근무 금지가 infeasible 로 자동 soft 전환되어 재생성됐습니다. "
                               "일부 간호사에게 휴무 사이 단일 근무가 생길 수 있습니다."),
                }, roster_system=roster_system, nurses=nurses_for_engine, year=req.year, month=req.month,
                    param=None))
                print("[IsolatedWorkFallback][AUTO-SOFT][success] isolated_work hard→soft 자동 전환으로 "
                      "근무표 생성. applied_relaxations=['isolated_work_hard_to_soft']")
                validation_error = None
            else:
                print(f"[IsolatedWorkFallback][AUTO-SOFT][fail] 재시도 실패: {_iw_err}")
                config_dict["isolated_work_hard"] = False
                config_dict["_isolated_work_soft_retry_attempted"] = True

        # ── 4차 완화: 신청 휴무·휴가 전날 N 금지 → 위반 칸 최소화 후 고정 ───────────
        # 순서는 하드락 완화들 **뒤**다(2026-10-12 사용자 결정: 팀 최소를 먼저 양보하고, 원티드·휴가 앞
        #   N 금지는 끝까지 지킨다). 단순 벌점이 아니라 커버리지를 고정한 채 위반 칸 수를 최소화해
        #   묶으므로(fallback_lex '[BanNBeforeFixedOff] 최소화'), 지킬 수 있으면 위반 0 = 하드와 같다.
        # ★ 앞 단계가 실패하며 남긴 같은 근무 연속·고립 근무 해제는 **먼저 되돌려** 시도하고,
        #   안 될 때만 누적 해제 상태로 다시 시도한다(2026-10-12 사용자 결정). 누적 해제로만 돌면
        #   전날 N 은 줄지만 그 대가가 하드락 해제로 나간다 — 실측 성남 중환자실-RN 11월 VER31:
        #   신청 휴무 전날 N 3칸인 대신 같은 근무 4연속 17곳.
        _ban_on = (bool(config_dict.get("ban_night_before_fixed_wanted_off"))
                   or bool(config_dict.get("ban_night_before_fixed_off")))
        if (validation_error and _trigger_soft and _ban_on
                and not bool(config_dict.get("_ban_night_minimize_retry_attempted"))):
            _bn_base = dict(config_dict)
            _bn_base["ban_night_minimize"] = True
            _bn_base["_ban_night_minimize_retry_attempted"] = True
            _bn_restore = {}
            if _ss_orig_k > 0 and int(config_dict.get("same_shift_hard_k", 3) or 0) == 0:
                _bn_restore["same_shift_hard_k"] = _ss_orig_k
            if _iw_orig_on and not bool(config_dict.get("isolated_work_hard", True)):
                _bn_restore["isolated_work_hard"] = True
            _bn_plans =([("하드 복구", {**_bn_base, **_bn_restore})] if _bn_restore else []) + [
                ("누적 해제", _bn_base)]
            _bn_err = "시도 없음"
            _bn_generated = _bn_rs = None
            # ★ 하드 복구 결과에 **휴가 전날 N** 이 남을 때만 누적 해제도 돌려 더 나은 쪽을 쓴다
            #   (2026-10-12 사용자 결정 · 비교 키 `_ban_plan_key` = 휴가 전날 N → N 간격 위반 →
            #   하드락 해제 여부 → 신청 휴무 전날 N). 처음엔 N 간격 위반만 남아도 돌렸는데, dev Lambda
            #   (CPU 2.3개)에서 한 번에 300초씩 두 번 돌아 518초가 걸렸고, 누적 해제가 신청 휴무 전날 N
            #   1칸을 줄이는 대가로 같은 근무 4연속 0→26 을 낸 쪽이 이겼다. 비기면 하드 복구를 쓴다.
            _bn_done = []
            for _bn_i, (_bn_label, _bn_cfg) in enumerate(_bn_plans):
                print(f"[BanNightFallback] infeasible 감지 → 신청 휴무·휴가 전날 N 금지를 "
                      f"위반 칸 최소화 모드로 재시도({_bn_label} · 되돌림 {sorted(_bn_restore)})")
                _g, _rs, _e = _ladder_retry(_bn_cfg, "전날 N 금지 최소화")
                if _e:
                    _bn_err = _e
                    print(f"[BanNightFallback][AUTO-MIN][fail] {_bn_label} 실패: {_e}")
                    continue
                # 누적 해제 시도는 앞 단계에서 풀린 하드락(같은 근무 연속·고립 근무)을 그대로 쓴다.
                _released = 1 if (_bn_label == "누적 해제" and _bn_restore) else 0
                _k = _ban_plan_key(_rs, _released)
                print(f"[BanNightFallback] {_bn_label} 결과 "
                      f"(휴가 전날 N, N 간격 위반, 하드락 해제, 신청 휴무 전날 N) = {_k}")
                _bn_done.append((_k, _bn_i, _bn_label, _g, _rs))
                if _k[0] == 0:
                    break
            if _bn_done:
                _bn_err = None
                _, _, _bn_label, _bn_generated, _bn_rs = min(_bn_done, key=lambda x: (x[0], x[1]))
                generated = _bn_generated
                roster_system = _bn_rs
                # 안내·완화 이력은 아래 **공통 종료 경로**에서 최종 채택된 결과 기준으로 만든다
                #   (이 단계가 실패해도 설정이 누적돼 뒤 재시도의 결과에 최소화가 들어갈 수 있다).
                _bn_t, _bn_w = getattr(_bn_rs, "_ban_night_violations", None) or (0, 0)
                _bn_viol = int(_bn_t) + int(_bn_w)
                print(f"[BanNightFallback][AUTO-MIN][success] 위반 {_bn_viol}칸으로 근무표 생성({_bn_label}). "
                      "applied_relaxations=['ban_night_before_off_minimized']")
                validation_error = None
            else:
                print(f"[BanNightFallback][AUTO-MIN][fail] 재시도 실패: {_bn_err}")
                config_dict["ban_night_minimize"] = True
                config_dict["_ban_night_minimize_retry_attempted"] = True

    # ── 4차 완화: n2n 최소 간격 하드 해제 ─────────────────────────────────
    # ★★ 이 블록은 아래 `if validation_error:` 사다리(team_min → same_shift →
    #   isolated_work) **밖**에 있고, 그보다 **먼저** 돈다. 둘 다 이유가 있다.
    #
    #   (1) 밖에 있는 이유 — n2n 하드가 과하면 stage2 가 UNKNOWN 으로 끝나는데, 그러면
    #     엔진이 보존해 둔 stage1 해로 복원해 **근무표는 정상 산출된다**. 즉
    #     `validation_error` 가 None 이라 그 사다리에는 **영원히 도달하지 못한다.**
    #     실측(2026-09-18 · `n2n_min_gap=15`): 폴백 로그 0줄 · 하드는 조용히 무시
    #     (gap<15 위반 30건) · 게다가 lex 최적화가 통째로 무효가 되어 간격중앙이
    #     10~11 → **7** 로 **오히려 나빠졌다**. 제약을 걸고도 품질만 잃는 최악의 조합이다.
    #     그래서 트리거를 `_infeasible_empty` 가 아니라 `fallback_lex` 가 세우는
    #     `roster_system._n2n_min_gap_stage2_failed` 로 잡는다.
    #
    #   (2) 1~3차 **뒤**에 있어야 하는 이유 — 실측(2026-09-18)에서 성남 중환자실-RN 은
    #     **첫 생성의 stage1 이 원래 INFEASIBLE** 이라(hard·broad_soft 둘 다) 1차 완화
    #     team_min 이 재생성을 돌린다. 하드가 실제로 stage2 에 걸리는 것은 **그 재생성**
    #     이고, 플래그도 그때 세워진다. 1~3차 앞에 두면 아직 None 인 플래그를 읽어
    #     영원히 발화하지 않는다(실측: DBG 421행 vs 하드 등록 676행).
    _mg_on = int(config_dict.get("n2n_min_gap", 0) or 0)
    _mg_failed = bool(getattr(roster_system, "_n2n_min_gap_stage2_failed", False))
    if (_mg_failed and _mg_on >= 4
            and not bool(config_dict.get("_n2n_min_gap_retry_attempted"))
            and _ladder_time_ok("n2n_min_gap 해제")):
        print(f"[N2NMinGapFallback] stage2 실패 감지 → n2n_min_gap {_mg_on}→0 해제로 1회 재시도")
        _mg_cfg = dict(config_dict)
        _mg_cfg["n2n_min_gap"] = 0
        _mg_cfg["_n2n_min_gap_retry_attempted"] = True
        try:
            _mg_generated, _, _mg_rs = _run_cp_sat_basic(
                db, current_user, nurses_for_engine, preferences, latest_config, req,
                shift_manage_data,
                fixed_cells=combined_fixed_cells if combined_fixed_cells else None,
                time_limit_seconds=180 if bool(getattr(req, "advanced_inference", False)) else 60,
                config_override=_mg_cfg,
                _assignments=_assignments,
                _inbound_assignments=_inbound_assignments,
                _outbound_assignments=_outbound_assignments,
                attempt_src=_attempt_src,
            )
            if isinstance(_mg_generated, dict):
                _mg_generated.update(fixed_roster)
            else:
                _mg_generated = fixed_roster
            if _alloff_roster:
                if isinstance(_mg_generated, dict):
                    _mg_generated.update(_alloff_roster)
                else:
                    _mg_generated = dict(_alloff_roster)
            _mg_err = _validate_generated_roster(
                _mg_generated, _mg_rs,
                nurses_context=list(nurses_for_engine or []),
                config_context=_mg_cfg,
            )
        except Exception as _mg_exc:
            _mg_err = f"n2n_min_gap 해제 재시도 예외: {type(_mg_exc).__name__}: {_mg_exc}"
            _mg_generated = _mg_rs = None
        if not _mg_err:
            generated = _mg_generated
            roster_system = _mg_rs
            applied_relaxations.append("n2n_min_gap_released")
            if _ctx is not None:
                _ctx["relaxed"] = True
            weekly_off_warnings.append(build_relaxation_notice("n2n_released", {
                "type": "n2n_min_gap_released_applied",
                "detail": ("나이트 블록 간 최소 간격 제한이 infeasible 로 자동 해제되어 "
                           "재생성됐습니다. 회복 휴무 직후 다시 나이트가 배정될 수 있습니다."),
            }, roster_system=roster_system, nurses=nurses_for_engine, year=req.year, month=req.month,
                param=_mg_on))
            print("[N2NMinGapFallback][AUTO-RELEASE][success] n2n_min_gap 해제로 근무표 생성. "
                  "applied_relaxations=['n2n_min_gap_released']")
            validation_error = None
        else:
            print(f"[N2NMinGapFallback][AUTO-RELEASE][fail] 재시도 실패: {_mg_err}")
            config_dict["n2n_min_gap"] = 0
            config_dict["_n2n_min_gap_retry_attempted"] = True



    # ── 앞 단계 실패로 누적된 하드락 해제 — 최종 채택 설정 기준으로 빠진 경고를 채운다 ──
    if not validation_error:
        for _rk, _rw in _carried_hardlock_relaxations(
                getattr(roster_system, "config", None), _ss_orig_on, _iw_orig_on,
                roster_system=roster_system, nurses=nurses_for_engine, year=req.year, month=req.month,
                ss_orig_k=_ss_orig_k):
            if _rk in applied_relaxations:
                continue
            applied_relaxations.append(_rk)
            weekly_off_warnings.append(_rw)
            if _ctx is not None:
                _ctx["relaxed"] = True
            print(f"[RelaxCarry] 앞 단계에서 넘어온 해제 보고: {_rk}")

    # ── 전날 N 금지 최소화 적용(fallback_lex `_ban_night_violations`) ──
    #   어느 경로로 성공했든 **최종 채택된 결과**에 최소화가 들어갔으면 알린다. 최소화 단계가
    #   실패해도 설정이 누적돼 뒤의 N 간격 해제 재시도에 실려 갈 수 있기 때문이다(Codex 지적).
    _bn_final = getattr(roster_system, "_ban_night_violations", None)
    if not validation_error and _bn_final is not None:
        _bn_t, _bn_w = _bn_final
        _bn_viol = int(_bn_t) + int(_bn_w)
        applied_relaxations.append("ban_night_before_off_minimized")
        if _ctx is not None:
            _ctx["relaxed"] = True
        _bn_proven = bool(getattr(roster_system, "_ban_night_proven", False))
        weekly_off_warnings.append({
            "type": "ban_night_before_off_minimized_applied",
            "detail": ((f"신청 휴무·휴가 전날 나이트 금지를 모두 지킬 수 없어 {_bn_viol}칸"
                        f"(휴가·공가 전날 {int(_bn_t)} · 신청 휴무 전날 {int(_bn_w)})은 전날에 "
                        "나이트가 배정됐습니다(인원을 맞춘 상태에서 가능한 최소)."
                        if _bn_proven else
                        f"생성 시간 안에 신청 휴무·휴가 전날 나이트 금지를 다 맞추지 못해 {_bn_viol}칸"
                        f"(휴가·공가 전날 {int(_bn_t)} · 신청 휴무 전날 {int(_bn_w)})은 전날에 "
                        "나이트가 배정됐습니다(더 줄일 여지가 있을 수 있습니다).")
                       if _bn_viol else
                       "신청 휴무·휴가 전날 나이트 금지는 모두 지켰습니다(다른 제약 조정으로 생성)."),
        })
        print(f"[BanNightFallback][FINAL] 위반 {_bn_viol}칸(휴가 {_bn_t}·신청휴무 {_bn_w}, "
              f"증명={_bn_proven}) — applied_relaxations=['ban_night_before_off_minimized']")

    # ── N 간격 하한 일부 미충족(위반칸 방식 · fallback_lex `_n2n_gap_violations`) ──
    #   통째 해제(위 4차)까지 가지 않고 위반 칸 수를 최소화해 묶은 경우다. 하드를 다 지키지
    #   못한 것이므로 다른 완화와 같은 경로로 알린다.
    #   ★ 개수는 **나간 배정에서 센다.** stage2 가 실패해 stage1 해로 돌아가고 위 해제 재시도도
    #     실패하면, stage2 에서 고정한 값(대개 0)은 나간 근무표와 무관해 위반이 경고 없이
    #     나간다(Codex 지적). 셀 수 없으면(금지쌍 미등록) 고정값을 쓴다.
    _n2n_viol = None
    try:
        from services.cp_sat.fallback_lex import count_n2n_gap_violations
        _n2n_viol = count_n2n_gap_violations(roster_system)
    except Exception as _n2n_cnt_exc:
        print(f"[N2NMinGap] 최종 배정 위반 계산 실패(고정값 사용): {_n2n_cnt_exc}")
    if _n2n_viol is None:
        _n2n_viol = int(getattr(roster_system, "_n2n_gap_violations", 0) or 0)
    if not validation_error and _n2n_viol > 0 and "n2n_min_gap_released" not in applied_relaxations:
        applied_relaxations.append("n2n_min_gap_partial")
        if _ctx is not None:
            _ctx["relaxed"] = True
        # stage1 해로 돌아간 결과는 최소화를 거치지 않았으므로 '가능한 최소' 로 안내하지 않는다.
        _n2n_proven = (bool(getattr(roster_system, "_n2n_gap_proven", False))
                       and not bool(getattr(roster_system, "_n2n_min_gap_stage2_failed", False)))
        weekly_off_warnings.append(build_relaxation_notice("n2n_partial", {
            "type": "n2n_min_gap_partial_applied",
            "detail": (f"나이트 블록 간 최소 간격을 모두 지킬 수 없어 {_n2n_viol}곳은 간격이 짧게 "
                       "배정됐습니다(가능한 최소로 줄인 값)." if _n2n_proven else
                       f"생성 시간 안에 나이트 블록 간 최소 간격을 다 맞추지 못해 {_n2n_viol}곳은 간격이 "
                       "짧게 배정됐습니다(더 줄일 여지가 있을 수 있습니다)."),
        }, roster_system=roster_system, nurses=nurses_for_engine, year=req.year, month=req.month,
            param=int(config_dict.get("n2n_min_gap") or 0)))
        print(f"[N2NMinGap][PARTIAL] 위반칸 {_n2n_viol} — applied_relaxations=['n2n_min_gap_partial']")

    if validation_error:
        print(f"[RosterGenerate][UNRECOVERABLE] {validation_error}")
        print(f"[RosterGenerate][UNRECOVERABLE] applied_relaxations={applied_relaxations}")
        # ★★ 이 커밋이 간호사 ORM 을 **만료시키면 안 된다.** 등급·주말휴무·고정근무(월 as-of)와
        #   팀은 `__dict__` 오버레이로만 들어가 있어(DB 컬럼은 캐시·NULL), 만료되면 아래 해결책
        #   탐색·MUS·개별 지목 재계산이 **DB 원래 값으로** 돈다. 실측(2026-09-28 여수 6병동 dev):
        #   첫 시도 `by_grade {1:[0..5],…}` → 탐색 `by_grade {1:[],2:[],3:[]}` 로 등급이 통째로
        #   사라져 등급 1/1/1 하드가 무의미해지고, 무엇을 바꿔도 '풀림' → "+1 이면 된다" 헛답.
        # (EI-13b) 탐색이 읽는 메모리 입력(간호사·ShiftManage·config 기준값)은 commit **전에** 평범한 사본으로 다시 묶는다 —
        #   이 블록은 늘 raise 로 끝나 재바인딩이 성공 경로로 새지 않는다. rollback 이 ORM 을 만료시켜도 탐색은 롤백 없는 런과
        #   같은 입력으로 돈다(예전엔 오버레이 소실을 감지해 탐색을 건너뛰었다). `preferences`·`combined_fixed_cells` 는 처음부터
        #   평범한 dict 리스트라 묶지 않는다.
        nurses_for_engine = [SimpleNamespace(**_plain_vars(_n)) for _n in (nurses_for_engine or [])]
        shift_manage_data = [_plain_row(_r) for _r in shift_manage_data]
        _cfg_base_snap = _call_time_config(latest_config)
        _eoc = db.expire_on_commit
        db.expire_on_commit = False
        try:
            db.delete(schedule)
            db.commit()
        except Exception:
            db.rollback()
        finally:
            db.expire_on_commit = _eoc
        try:
            from services.precheck import build_unrecoverable_payload
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
            # ── 실패-시-한번 MUS 진단 재solve (기본 OFF, opt-in) ──
            # 실측(2026-07-21): reified 재solve 는 core 추출 지점(cp_sat_basic:2285=첫 hard
            # solve)이 실제 infeasibility 표면화 단계(broad_soft)를 안 덮어 **0 core** 를 낸다.
            # 그동안 30~60초를 낭비할 뿐, 뒤의 undiag probe(재solve 탐색)가 원인+검증해결을
            # 이미 제공한다("완화하면 풀린다"⟺"이게 원인"). 따라서 기본 스킵.
            # 추출 지점을 broad_soft 까지 확장(별도 후속)한 뒤 MUS_DIAG_ENABLE=1 로 재활성.
            if not _cpsat_cores:
                try:
                    import os as _os_mus
                    if _os_mus.getenv("MUS_DIAG_ENABLE") == "1":
                        _prev_mus = _os_mus.environ.get("AIDE_ENABLE_MUS_REGISTRY")
                        _os_mus.environ["AIDE_ENABLE_MUS_REGISTRY"] = "1"
                        _mus_base = getattr(roster_system, "_effective_config_snapshot", None)
                        try:
                            _mg, _, _mrs = _run_cp_sat_basic(
                                db, current_user, nurses_for_engine, preferences, latest_config, req,
                                shift_manage_data,
                                fixed_cells=combined_fixed_cells if combined_fixed_cells else None,
                                time_limit_seconds=int(_os_mus.getenv("MUS_DIAG_TIME", "30") or 30),
                                config_override=_mus_base,
                                attempt_src=_attempt_src,
                            )
                            _mus_cores = list(getattr(_mrs, "_cpsat_conflict_cores", []) or [])
                            if _mus_cores:
                                print(f"[MUS-Diag] 진단 재solve → conflict cores {len(_mus_cores)}건")
                                _cpsat_cores = _mus_cores
                                _conflict_cores = _conflict_cores + _mus_cores
                        finally:
                            if _prev_mus is None:
                                _os_mus.environ.pop("AIDE_ENABLE_MUS_REGISTRY", None)
                            else:
                                _os_mus.environ["AIDE_ENABLE_MUS_REGISTRY"] = _prev_mus
                except Exception as _mus_exc:
                    print(f"[MUS-Diag] 진단 재solve 실패(무시): {_mus_exc}")
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

                # EI-11 11e — 탐색 결과 세 칸(probe_found·probe_baseline_feasible·probe_timed_out). 이 경로엔 탐색 단계가
                #   있다: 돌리지 않으면(게이트 닫힘·UNDIAG_PROBE_DISABLE·오버레이 소실·원인 진단 스킵) 세 칸이 null 로 남는다.
                from services.cp_sat.undiagnosed_probe import probe_status_fields
                unrecoverable.setdefault("infeasibility", {}).update(probe_status_fields(None))
                _inf_pre = unrecoverable.get("infeasibility", {}) or {}
                _sd_pre = _inf_pre.get("structural_diagnosis", {}) or {}
                _codes_pre = (_sd_pre.get("signals") or {}).get("reason_codes") or []
                # probe 게이트: '사용자에게 줄 actionable 진단(해결옵션 / 수정가능 원인)이 없으면'
                # 무조건 probe 를 돌린다. 뭉뚱그린 구조 라벨(primary_causes)이 있다는 이유로
                # probe 를 막지 않는다(그게 capacity_structural 이 진짜 진단을 죽이던 원인).
                _ui_options_pre = _inf_pre.get("resolution_options") or []
                _ui_causes_fix = [
                    _c for _c in (_inf_pre.get("causes") or [])
                    if isinstance(_c, dict) and _c.get("fix")
                ]
                _has_actionable = bool(_ui_options_pre) or bool(_ui_causes_fix)
                _is_undiag = ("UNDIAGNOSED" in _codes_pre) or not _has_actionable
                if _is_undiag and _os_undiag.getenv("UNDIAG_PROBE_DISABLE") != "1":
                    from services.cp_sat.undiagnosed_probe import probe_relaxations, to_resolution_options

                    _probe_cnt = [0]

                    def _undiag_resolve(_relaxed_cfg):
                        _probe_cnt[0] += 1
                        _probe_step("규칙 완화 탐색", _probe_cnt[0])

                        def _do():
                            _g, _, _rs = _run_cp_sat_basic(
                                db, current_user, nurses_for_engine, preferences, latest_config, req,
                                shift_manage_data,
                                fixed_cells=combined_fixed_cells if combined_fixed_cells else None,
                                time_limit_seconds=_probe_time_limit(),
                                config_override=_relaxed_cfg,
                                _assignments=_assignments,
                                _inbound_assignments=_inbound_assignments,
                                _outbound_assignments=_outbound_assignments,
                                attempt_src=_attempt_src,
                            )
                            _err = _validate_generated_roster(
                                _g, _rs,
                                nurses_context=list(nurses_for_engine or []),
                                config_context=_relaxed_cfg,
                            )
                            return (_err is None), {"validation_error": (str(_err)[:80] if _err else None)}

                        return _quiet_solve(_do)

                    # probe base: solve 시점에 박아둔 유효 config 스냅샷(하드규칙+조립분 포함, 충실).
                    # 실패 시점 메모리(config_dict)는 ORM 만료·stale 라 부정확하므로 스냅샷 우선.
                    #   ★ 스냅샷이 없을 때 지금의 config_dict 를 쓰면 안 된다 — 완화 사다리가 누적
                    #     갱신한 값이라, 그 위에서 찾은 '해결책' 은 실제 재생성의 첫 시도와 조건이 다르다.
                    _probe_base = getattr(roster_system, "_effective_config_snapshot", None)
                    print(f"[UndiagProbe] 기준={'첫 시도 스냅샷' if _probe_base else '첫 시도 직전 설정 사본'}")
                    if not _probe_base:
                        _probe_base = _first_attempt_cfg or {
                            k: v for k, v in dict(config_dict).items() if not str(k).startswith("_sa_")}
                    _probe_base = dict(_probe_base)
                    # 금지근무(banned)×강제OFF 개인모순 진단이 initial_constraints 를 봐야 하므로
                    # 스냅샷에 없으면 merged config 에서 승계(금지·강제OFF 합집합 맵).
                    if "initial_constraints" not in _probe_base:
                        _probe_base["initial_constraints"] = config_dict.get("initial_constraints")
                    # (EI-14b-1b) 사후 소비자 5곳이 받는 입력 한 벌 — 조회 0. 지금은 받은 객체 그대로(겹침 0 · 14b-2b 에서 채운다).
                    _ir_post = to_roster_ir_post(
                        _ei_first, nurses_for_engine, _probe_base, year=req.year, month=req.month,
                        shift_manage_data=shift_manage_data)
                    # 온톨로지 소프트정렬: presolve(max-flow) 병목 → 우선 완화군을 먼저 검증.
                    try:
                        _prio_fams = _priority_families_from_presolve(presolve_diag)
                    except NameError:
                        _prio_fams = []
                    # λ(Lagrangian) 우선순위(opt-in): max-flow 가 놓치는 결합(OFF예산·야간cap 등)
                    # 을 재solve subgradient 로 실측해 완화 레버를 앞에 배치 → 전수폴백 방지.
                    # AIDE_PROBE_LAMBDA=1 로 활성. 실패/미설정 시 기존 presolve 순서 그대로.
                    if _os_undiag.getenv("AIDE_PROBE_LAMBDA") == "1":
                        try:
                            import calendar as _cal
                            from services.ontology_graph.lagrangian import lambda_priority_from_config
                            _nd = _cal.monthrange(req.year, req.month)[1]
                            _lam_fams = lambda_priority_from_config(_ir_post.nurses, _ir_post.config, _nd)
                            if _lam_fams:
                                _prio_fams = _lam_fams + [f for f in _prio_fams if f not in _lam_fams]
                                print(f"[UndiagProbe] λ 우선 완화군 {_lam_fams} 선반영")
                        except Exception as _le:
                            print(f"[UndiagProbe] λ 우선순위 실패(무시): {_le}")
                    # ── 원인(cause) → 탐색(repair) 게이팅 ────────────────────────────
                    #   explain_infeasibility 로 '왜 해가 없나'를 진단 → (a) 항상 payload 에
                    #   cause_explanation 첨부(미션: 설명), (b) AIDE_CAUSE_GATE=1 이면 탐색 제어:
                    #     · arithmetic-증명 원인(인원/셀 부족·정책 과제약)=카탈로그 레버 없음 → probe 스킵
                    #     · coupled_sequence → 그 family 를 최우선(prune) → 전수(377회) 방지
                    #   이게 '원인 vs 해결책 분리'를 실제로 통합(원인이 repair 탐색을 게이팅)한다.
                    _skip_probe = False
                    try:
                        import calendar as _cal2
                        from services.ontology_graph.lagrangian import (
                            explain_infeasibility_from_config,
                        )
                        _nd2 = _cal2.monthrange(req.year, req.month)[1]
                        # [SSOT] 진단도 솔버(2897)와 동일하게 주말휴무를 period 로 재해석해 주입한다.
                        #   진단(lagrangian)은 is_weekend_off 속성만 신뢰 → 속성 표류 시 원인이
                        #   'unknown' 으로 뭉개져 "주말휴무자 과다"가 실종되던 갭을 막는다.
                        try:
                            from services.nurse_period_resolver import (
                                weekend_off_ids_asof as _wo_ids_dx,
                            )
                            _wo_set_dx = _wo_ids_dx(
                                db,
                                [getattr(_n, "nurse_id", None) for _n in nurses_for_engine],
                                req.year, req.month)
                            for _n in nurses_for_engine:
                                _wnid = str(getattr(_n, "nurse_id", "") or "")
                                if _wnid:
                                    try:
                                        _n.__dict__["is_weekend_off"] = _wnid in _wo_set_dx
                                    except Exception:
                                        setattr(_n, "is_weekend_off", _wnid in _wo_set_dx)
                        except Exception as _wo_dx_exc:
                            print(f"[Cause] 주말휴무 SSOT 주입 실패(무시): {_wo_dx_exc}")
                        _cause = explain_infeasibility_from_config(
                            _ir_post.nurses, _ir_post.config, _nd2,
                            year=req.year, month=req.month)
                        unrecoverable["infeasibility"]["cause_explanation"] = {
                            "classification": _cause.classification,
                            "top_family": _cause.top_family,
                            "certificate": _cause.certificate,
                            "arithmetic": _cause.arithmetic,
                            "lambda_by_family": _cause.lambda_by_family,
                        }
                        print(f"[Cause] {_cause.classification}: {_cause.certificate}")
                        # N축 branch-and-infer: 야간축 원인이면 proof-tree 설명 + 검증된 복구를
                        # payload 에 **추가**(기존 분류·카드 불변, 실패내성). "각자OK 같이X" 를
                        # "어느 배정이든 실패" 서사로 보여주고, 복구 후보를 N축 재판정으로 선별.
                        if _cause.top_family in ("banned_wanted", "night_coverage"):
                            try:
                                from services.ontology_graph.branch_infer import (
                                    diagnose_night_axis, verified_repairs)
                                from services.ontology_graph.certificate import (
                                    INFEASIBLE as _INF, render_explanation)
                                _nd_dicts = [{"nurse_id": str(getattr(_n, "nurse_id", "") or ""),
                                              "name": getattr(_n, "name", None),
                                              "allowed_shifts": list(getattr(_n, "allowed_shifts", None) or [])}
                                             for _n in nurses_for_engine]
                                _pn = diagnose_night_axis(_nd_dicts, _ir_post.config, _nd2)
                                if _pn.status == _INF:
                                    _ce = unrecoverable["infeasibility"]["cause_explanation"]
                                    _ce["proof_explanation"] = render_explanation(_pn)
                                    _ce["verified_repairs"] = [
                                        r for r in verified_repairs(_pn, _nd_dicts, _ir_post.config, _nd2)
                                        if r.get("verified")]
                                    print(f"[Cause][BranchInfer] {_ce['proof_explanation'][:80]}")
                            except Exception as _bi_exc:
                                print(f"[Cause][BranchInfer] 실패(무시): {_bi_exc}")
                        # 연구용 케이스 캡처(게이팅): AIDE_DUMP_CASE=<dir> 설정 시 진단 입력을
                        # 그대로 JSON 스냅샷(오프라인 재현/실험). 미설정=no-op.
                        if _os_undiag.getenv("AIDE_DUMP_CASE"):
                            try:
                                from services.ontology_graph.case_export import dump_case
                                dump_case(_os_undiag.getenv("AIDE_DUMP_CASE"),
                                          year=req.year, month=req.month,
                                          group_id=str(getattr(current_user, "group_id", "") or ""),
                                          num_days=_nd2, nurses=_ir_post.nurses,
                                          config=_ir_post.config, cause=_cause)
                            except Exception as _dc_exc:
                                print(f"[CaseDump] hook 실패(무시): {_dc_exc}")
                        # (a) 구체 원인이 있으면 generic 3문구(payload.py 하드코딩)를 그것으로
                        #     대체. unknown 일 때만 generic fallback 유지.
                        if _cause.classification != "unknown":
                            unrecoverable["infeasibility"]["fix_suggestions_ko"] = [_cause.certificate]
                        # (b) cause → 모달 행동카드(resolution_options) 로 번역해 앞에 prepend.
                        #     "조건변경필요" 모달의 [이 방법으로 다시 생성] 카드로 뜬다.
                        try:
                            from services.ontology_graph.mcs_trace import cause_to_resolution_options
                            _cards = cause_to_resolution_options(
                                _cause.classification, _cause.top_family, _cause.targets)
                            if _cards:
                                _ex = unrecoverable["infeasibility"].get("resolution_options") or []
                                unrecoverable["infeasibility"]["resolution_options"] = _cards + _ex
                                # cause 액션카드는 raise 직전 유일(집합) 옵션이어야 한다. 이후 per-nurse
                                # MCS 가 개별카드를 덧붙여도 이 카드(들)로 최종 override.
                                #   · fix_all_personal(월한도/주말) = 단일 통합 카드
                                #   · banned/allowed(금지근무 모순) = 대안 2카드(HN 선택)
                                _oid0 = _cards[0].get("option_id") or ""
                                if _oid0 in ("cause:fix_all_personal", "cause:night_only_over_hard_cap"):
                                    # night_only_over_hard_cap(야간전담 >15 정정 안내)도 단독 — 안 그러면
                                    # 그 위에 '상한을 올리라' 는 일반 설명카드가 함께 떠 서로 모순된다.
                                    unrecoverable["infeasibility"]["_sole_option"] = _cards[0]
                                elif _oid0 in ("cause:banned_release", "cause:allowed_add"):
                                    unrecoverable["infeasibility"]["_sole_option"] = list(_cards)
                        except Exception as _cc_exc:
                            print(f"[Cause] 행동카드 생성 실패(무시): {_cc_exc}")
                        # arithmetic-증명 원인(개인 즉시모순·인원/셀 부족)은 확실 → config
                        # 카탈로그 전수탐색이 무의미. 게이트 무관 **기본 스킵**(377회 회피).
                        # 그 외(정책과제약·coupled)는 추정이라 AIDE_CAUSE_GATE=1 일 때만.
                        if _cause.classification in ("personal_infeasible", "coverage_shortage"):
                            _skip_probe = True
                            # cause 액션카드(_sole_option)가 있으면 그게 최종 → generic 설명카드 안 붙임.
                            if not unrecoverable["infeasibility"].get("_sole_option"):
                                unrecoverable["infeasibility"]["resolution_options"] = [{
                                    "option_id": f"cause:{_cause.classification}",
                                    "kind": "cause_explanation", "source": "cause", "verified": True,
                                    "title_ko": _cause.certificate,
                                    "fix": {"mode": "manual", "where": "nurse.monthly_limit",
                                            "where_label_ko": "간호사 관리 > 해당 간호사 > 월 근무 한도"},
                                }] + (unrecoverable["infeasibility"].get("resolution_options") or [])
                            print("[Cause] arithmetic-증명 원인 → probe 스킵(전수탐색 회피, 기본)")
                        elif _os_undiag.getenv("AIDE_CAUSE_GATE") == "1":
                            if _cause.classification == "policy_overconstraint":
                                _skip_probe = True
                                print("[Cause] 정책 과제약 → probe 스킵")
                            elif _cause.top_family:
                                _prio_fams = [_cause.top_family] + [
                                    f for f in _prio_fams if f != _cause.top_family]
                    except Exception as _ce:
                        print(f"[Cause] 진단 실패(무시): {_ce}")
                    # hard-filter: 온톨로지가 압박군을 지목했으면(=_prio_fams 존재) 그 단일+콤보만
                    # 먼저 확인하고 안 되면 전수 폴백 → 흔한 케이스 재solve 대폭↓. AIDE_PROBE_HARD_FILTER=0 로 해제.
                    _hard_filter = bool(_prio_fams) and _os_undiag.getenv("AIDE_PROBE_HARD_FILTER", "1") != "0"
                    if _skip_probe:
                        _probe_res = {"found": False, "resolutions": [], "all_probed": [],
                                      "probed_count": 0, "skipped_by_cause": True}
                    else:
                        _probe_res = probe_relaxations(
                            _probe_base, _undiag_resolve,
                            priority_families=(_prio_fams or None),
                            # 필요한 만큼만 probe: 검증된 해결책 2건이면 종료(우선군 먼저라 최우선 완화 포함).
                            stop_after=int(_os_undiag.getenv("AIDE_PROBE_STOP_AFTER", "2") or 2),
                            hard_filter=_hard_filter,
                        )
                    _probe_done(f"[해결책 탐색] 규칙 완화 탐색 {_probe_cnt[0]}회 완료")
                    unrecoverable["infeasibility"]["probe_resolutions"] = _probe_res.get("resolutions", [])
                    unrecoverable["infeasibility"]["probe_combo"] = _probe_res.get("combo")
                    unrecoverable["infeasibility"].update(probe_status_fields(_probe_res))  # EI-11 11e — 세 칸(원인 스킵이면 null)
                    # 프론트용 통합 옵션 카드: probe(검증됨)를 앞에, 기존 ontology 옵션
                    # (build_unrecoverable_payload 가 treatment 로 채운 것) 뒤에 prepend.
                    _probe_opts = to_resolution_options(_probe_res, _probe_base)
                    _exist_opts = unrecoverable["infeasibility"].get("resolution_options") or []
                    unrecoverable["infeasibility"]["resolution_options"] = _probe_opts + _exist_opts

                    # ── per-nurse MCS: 주말휴무가 병목이면 어느 간호사가 원인인지 1명씩 해제(1회성
                    #    override)해 재solve 로 지목. 게이트는 presolve 가 데이터로 잡은 weekend_off_load
                    #    (주말휴무자 존재)로 판단 — config 플래그(weekend_off_only_enable)에 의존하지 않는다
                    #    (그 플래그는 catalog 에서 제외됨). 주말휴무자만+top-K+조기종료 로 비용 억제.
                    try:
                        _wk_flags = (presolve_diag or {}).get("constraint_flags") or []
                    except NameError:
                        _wk_flags = []
                    _wk_feasible = any(f.get("type") == "weekend_off_load" for f in _wk_flags)
                    # 기준 설정이 탐색 재계산에서 풀리면(실패 재현 불가) 1명씩 해제해 봐도 전부 '풀림'
                    # 이라 지목이 무의미하다. 탐색이 시간 상한에 걸렸으면 여기서 재계산을 더 돌리면
                    # 상한을 둔 의미가 없다(묶음 1회 + 최대 6명 × 20초). 둘 다 건너뛴다.
                    _pn_allowed = not (_probe_res.get("baseline_feasible") or _probe_res.get("timed_out"))
                    if _wk_feasible and _pn_allowed:
                        try:
                            from services.nurse_period_resolver import weekend_off_ids_asof as _wo_ids
                            _wk_cands = sorted(_wo_ids(
                                db, [getattr(n, "nurse_id", None) for n in (nurses_for_engine or [])],
                                req.year, req.month))
                            _PN_K = 6
                            _culprit = None
                            _multi = None
                            # [관계 → 인원] presolve 가 max-flow 로 '주말 몇 명 풀어야 하는지'를 산출.
                            #   ≥2 면 조합탐색 대신 상위 N명을 '한 번에' 해제·검증(다인 번들).
                            try:
                                _wk_need = int((presolve_diag or {}).get("weekend_release_needed") or 0)
                            except NameError:
                                _wk_need = 0
                            if _wk_need >= 2 and len(_wk_cands) >= _wk_need:
                                _grp = [str(c) for c in _wk_cands[:_wk_need]]
                                _probe_step("주말휴무 다인 해제", _wk_need, _wk_need)

                                def _do_multi(_grp=_grp):
                                    _g2, _, _rs2 = _run_cp_sat_basic(
                                        db, current_user, nurses_for_engine, preferences, latest_config, req,
                                        shift_manage_data,
                                        fixed_cells=combined_fixed_cells if combined_fixed_cells else None,
                                        time_limit_seconds=_probe_time_limit(),
                                        weekend_off_override={c: False for c in _grp},
                                        config_override=dict(_cfg_base_snap),
                                        _assignments=_assignments,
                                        _inbound_assignments=_inbound_assignments,
                                        _outbound_assignments=_outbound_assignments,
                                        attempt_src=_attempt_src,
                                    )
                                    return _validate_generated_roster(
                                        _g2, _rs2, nurses_context=list(nurses_for_engine or []),
                                        config_context=_probe_base,
                                    )

                                if _quiet_verify_solve(_do_multi) is None:
                                    _multi = _grp
                                _probe_done()
                            # 단일 지목(다인이 아니거나 실패 시): 1명씩 해제해 첫 범인.
                            if not _multi:
                                _wk_n = min(len(_wk_cands), _PN_K)
                                for _wk_i, _cnid in enumerate(_wk_cands[:_PN_K], start=1):
                                    _probe_step("주말휴무 조정 후보", _wk_i, _wk_n)

                                    def _do_wk(_cnid=_cnid):
                                        _g2, _, _rs2 = _run_cp_sat_basic(
                                            db, current_user, nurses_for_engine, preferences, latest_config, req,
                                            shift_manage_data,
                                            fixed_cells=combined_fixed_cells if combined_fixed_cells else None,
                                            time_limit_seconds=_probe_time_limit(),
                                            weekend_off_override={str(_cnid): False},
                                            config_override=dict(_cfg_base_snap),
                                            _assignments=_assignments,
                                            _inbound_assignments=_inbound_assignments,
                                            _outbound_assignments=_outbound_assignments,
                                            attempt_src=_attempt_src,
                                        )
                                        return _validate_generated_roster(
                                            _g2, _rs2, nurses_context=list(nurses_for_engine or []),
                                            config_context=_probe_base,
                                        )

                                    _err2 = _quiet_verify_solve(_do_wk)
                                    if _err2 is None:
                                        _culprit = str(_cnid)
                                        break
                                _probe_done()
                            # 다인 번들 옵션(관계분석 지목): N명 주말 해제를 한 옵션·한 클릭으로.
                            if _multi:
                                _mnames = [
                                    next((getattr(n, "name", "") for n in (nurses_for_engine or [])
                                          if str(getattr(n, "nurse_id", "")) == c), c) or c
                                    for c in _multi]
                                _wk_opt = {
                                    "option_id": "release_weekend_off_multi:" + "+".join(_multi),
                                    "kind": "release_weekend_off_multi", "source": "probe", "verified": True,
                                    "title_ko": f"주말 휴무 {len(_multi)}명 해제 ({', '.join(_mnames)})",
                                    "trade_off_ko": "그 간호사들이 주말에도 근무할 수 있게 됩니다.",
                                    "changes": [_mk_change(c, "weekend_off", True, False) for c in _multi],
                                    "weekend_off_release": _multi,
                                    "fix": {
                                        "mode": "auto_apply",
                                        "where": "nurse.weekend_off",
                                        "where_label_ko": "간호사 관리 > 주말 휴무",
                                        "how_ko": f"이 방법을 고르면 {len(_multi)}명({', '.join(_mnames)})의 "
                                                  f"주말 휴무를 해제하고 다시 만듭니다(이 달).",
                                        "config_key": None, "target": {"nurse_ids": _multi},
                                    },
                                }
                                _opts_now = unrecoverable["infeasibility"].get("resolution_options") or []
                                unrecoverable["infeasibility"]["resolution_options"] = [_wk_opt] + _opts_now
                                print(f"[UndiagProbe][per-nurse] 주말휴무 다인 해제 지목({_wk_need}명): {_multi}")
                            if _culprit:
                                _nm = next((getattr(n, "name", "") for n in (nurses_for_engine or [])
                                            if str(getattr(n, "nurse_id", "")) == _culprit), "") or _culprit
                                # [데이터 기반 번들] per-nurse 주말 probe 의 feasible 은 검증이 월한도
                                # (n_exact)를 안 봐서 거짓양성일 수 있다. presolve 가 데이터(산술)로 이미
                                # 잡은 이 간호사의 다른 하드 모순(night_floor_over_cap: n_exact>max_nig)이
                                # 있으면, 주말만 풀어선 그 모순이 남아 실패한다 → 야간 하향도 같은 옵션에
                                # 묶어 "주말 해제 + 야간 낮추기" 한 클릭으로 만든다.
                                try:
                                    _cflags = (presolve_diag or {}).get("constraint_flags") or []
                                except NameError:
                                    _cflags = []
                                # ★ N전담이면 야간 쪽은 **아무것도 바꾸지 않는다** — N전담 15회 고정은 하드락이다
                                #   (2026-10-02 사용자 지적). 개인 고정을 낮추지도, 병동 상한을 올리지도 않는다. 주말
                                #   해제만 내되, 이 probe 검증은 월 한도를 안 봐서 야간 충돌이 남을 수 있으므로
                                #   verified=False 로 낸다(아래 else 분기).
                                from services.cp_sat.allowed_shift_types import is_n_only_profile as _is_n_only
                                _culprit_nu = next((n for n in (nurses_for_engine or [])
                                                    if str(getattr(n, "nurse_id", "")) == _culprit), None)
                                # 가드(`n_only_release_targets`)와 같은 판정 — MID 병동 [N,M] 간호사 카드가
                                #   400 막다른 길이 되지 않게(EI-11 Q16)
                                _culprit_n_only = _is_n_only(getattr(_culprit_nu, "allowed_shifts", None),
                                                             use_mid=False)
                                _nflag = next(
                                    (f for f in _cflags
                                     if str(f.get("nurse_id")) == _culprit
                                     and f.get("type") == "night_floor_over_cap"), None)
                                if _nflag and not _culprit_n_only:
                                    from services.cp_sat.allowed_shift_types import HARD_MAX_NIGHTS_PER_MONTH as _HARD_N
                                    _mn = min(int(_nflag.get("max_nig") or 0), _HARD_N)   # 하드락 #6 안으로
                                    _nf = int(_nflag.get("n_floor") or 0)
                                    _wk_opt = {
                                        "option_id": f"release_weekend_off+lower_night:{_culprit}",
                                        "kind": "release_weekend_off+lower_night",
                                        "source": "probe", "verified": True,
                                        "title_ko": f"{_nm} 간호사 주말 휴무 해제 + 야간 고정 {_nf}→{_mn}회로 낮추기",
                                        "trade_off_ko": "주말에도 근무 가능해지고, 월 야간이 그 값 이하로 제한됩니다.",
                                        "changes": [
                                            _mk_change(_culprit, "weekend_off", True, False),
                                            _mk_change(_culprit, "n_exact", _nf, _mn),
                                        ],
                                        # 한 클릭에 둘 다 적용(주말 해제 + 야간 고정 하향).
                                        "weekend_off_release": [_culprit],
                                        "monthly_limit_release": [
                                            {"nurse_id": _culprit, "field": "n_exact", "value": _mn}],
                                        "fix": {
                                            "mode": "auto_apply",
                                            "where": "nurse.weekend_off+monthly_limit",
                                            "where_label_ko": "간호사 관리 > 해당 간호사 > 주말 휴무 · 월 근무 한도(야간)",
                                            "how_ko": f"이 방법을 고르면 {_nm} 간호사의 주말 휴무를 해제하고 "
                                                      f"야간 고정을 {_mn}회로 낮춘 뒤 다시 만듭니다(이 달).",
                                            "config_key": None, "target": {"nurse_id": _culprit},
                                        },
                                    }
                                else:
                                    _wk_opt = {
                                        "option_id": f"release_weekend_off:{_culprit}",
                                        "kind": "release_weekend_off", "source": "probe", "verified": True,
                                        "title_ko": f"{_nm} 간호사 주말 휴무 해제",
                                        "trade_off_ko": "그 간호사가 주말에도 근무할 수 있게 됩니다.",
                                        "changes": [_mk_change(_culprit, "weekend_off", True, False)],
                                        # 원클릭 재생성용 — 이 달 발효로 nurse_weekendoff_period 해제 후 생성.
                                        "weekend_off_release": [_culprit],
                                        "fix": {
                                            "mode": "auto_apply",
                                            "where": "nurse.weekend_off",
                                            "where_label_ko": "간호사 관리 > 해당 간호사 > 주말 휴무",
                                            "how_ko": f"이 방법을 고르면 {_nm} 간호사의 주말 휴무를 해제하고 다시 만듭니다(이 달부터).",
                                            "config_key": None, "target": {"nurse_id": _culprit},
                                        },
                                    }
                                    if _nflag:   # N전담 + 야간 고정이 병동 상한을 넘는 충돌 — 야간은 하드락이라 그대로
                                        # ★ presolve 의 n_floor > max_nig 는 **산술로 확정된 모순**이라 주말만 풀면
                                        #   반드시 다시 실패한다. 자동 적용하면 주말휴무만 영구로 사라지므로 수동
                                        #   안내로 내리고 자동 적용 필드를 뺀다(OMC 리뷰 2026-10-02).
                                        _wk_opt["verified"] = False
                                        _wk_opt.pop("weekend_off_release", None)
                                        _wk_opt["changes"] = []          # 자동으로 바꾸는 게 없다
                                        _wk_opt["fix"]["mode"] = "manual"
                                        _wk_opt["fix"]["how_ko"] = (
                                            f"근무 설정의 '한 달 밤 근무 최대 횟수'와 {_nm} 간호사의 나이트 개수를 "
                                            f"맞춘 뒤 다시 만드세요.")
                                        _wk_opt["title_ko"] = f"{_nm} 간호사(야간전담) 나이트 설정 확인 필요"
                                        _wk_opt["trade_off_ko"] = (
                                            f"{_nm} 간호사(야간전담)의 나이트 개수가 병동 월 야간 상한보다 커서 그대로는 "
                                            f"다시 만들어도 실패합니다. 근무 설정의 '한 달 밤 근무 최대 횟수'와 근무자의 "
                                            f"나이트 개수(야간전담 15회 고정 — 하드락)가 맞는지 먼저 확인하세요. 주말 휴무 "
                                            f"해제는 그다음에 필요하면 하세요.")
                                # 그룹 전체 끄는 coarse '단독' 옵션(apply 가 weekend_off_only_enable 하나뿐)만
                                # 개인 지목으로 대체. 콤보(예: weekend_off_only_enable + max_nig 상향)는 유효한
                                # 대안이므로 남긴다 — 그래야 "개인 낮추기 / config 상한 올리기" 두 갈래가 모두 노출.
                                _opts_now = unrecoverable["infeasibility"].get("resolution_options") or []
                                _opts_now = [
                                    o for o in _opts_now
                                    if not ((o.get("apply") or {}).get("weekend_off_only_enable") is False
                                            and len(o.get("apply") or {}) <= 1)
                                ]
                                unrecoverable["infeasibility"]["resolution_options"] = [_wk_opt] + _opts_now
                                print(f"[UndiagProbe][per-nurse] 주말휴무 병목 지목: {_culprit}({_nm})")
                            else:
                                print(f"[UndiagProbe][per-nurse] 단일 해제로 미해결 "
                                      f"(후보 {len(_wk_cands)}, 검사 {min(len(_wk_cands), _PN_K)})")
                        except Exception as _pn_exc:
                            print(f"[UndiagProbe][per-nurse] failed (ignore): {_pn_exc}")

                    # ── per-nurse MCS(월 야간 한도): config 야간 상한 완화가 병목(raise_max_night_cap
                    #    검증)이면, 어느 간호사의 n_exact/n_max 가 상한과 충돌하는지 1명씩 상한값으로
                    #    낮춰(1회성 override) 재solve 로 지목. config 상한 완화(전체)와 개인 하향(핀포인트)
                    #    두 옵션을 모두 제시한다(개인 지목을 앞에).
                    _nc_feasible = any(
                        o.get("verified") and (o.get("apply") or {}).get("max_nig_per_month") is not None
                        for o in _probe_opts
                    )
                    if _nc_feasible and _pn_allowed:
                        try:
                            _cfg_mn = int(_probe_base.get("max_nig_per_month") or 0)
                            # 후보: n_exact 또는 n_max 가 config 야간 상한보다 큰 간호사(그 값을 상한
                            # 이하로 낮추면 충돌 해소). 야간이 상한을 초과할 수 없으니 이들이 병목 후보.
                            # ★ N전담은 후보에서 뺀다 — N전담 15회 고정은 하드락이라 해결카드가 바꾸는 대상이
                            #   아니다(2026-10-02 사용자 지적). 낮추면 나머지가 강제 OFF 가 되어 야간 공급도 준다.
                            from services.cp_sat.allowed_shift_types import is_n_only_profile as _is_n_only
                            _nc_cands = []
                            for _n in (nurses_for_engine or []):
                                # 가드(`n_only_release_targets`)와 같은 판정 — MID 병동 [N,M] 간호사 카드가
                                #   400 막다른 길이 되지 않게(EI-11 Q16)
                                if _is_n_only(getattr(_n, "allowed_shifts", None),
                                              use_mid=False):
                                    continue
                                for _fld in ("n_exact", "n_max"):
                                    _val = getattr(_n, _fld, None)
                                    if _val is not None and _cfg_mn and int(_val) > _cfg_mn:
                                        _nc_cands.append(
                                            (str(getattr(_n, "nurse_id", "")), _fld, int(_val), _n))
                                        break
                            _PN_K = 6
                            _nc_culprit = None
                            _nc_n = min(len(_nc_cands), _PN_K)
                            for _nc_i, (_cnid, _cfld, _ccur, _cnu) in enumerate(_nc_cands[:_PN_K], start=1):
                                _probe_step("야간한도 조정 후보", _nc_i, _nc_n)
                                # nurses_dict = [n.__dict__] 라 객체 dict 를 직접 낮췄다가 finally 로 복원
                                # (다음 후보 재solve 오염 방지). 엔진은 nd 에서 n_exact/n_max 를 읽는다.
                                _orig = _cnu.__dict__.get(_cfld)
                                _cnu.__dict__[_cfld] = _cfg_mn
                                try:
                                    def _do_nc():
                                        _g3, _, _rs3 = _run_cp_sat_basic(
                                            db, current_user, nurses_for_engine, preferences,
                                            latest_config, req, shift_manage_data,
                                            fixed_cells=combined_fixed_cells if combined_fixed_cells else None,
                                            time_limit_seconds=_probe_time_limit(),
                                            config_override=dict(_cfg_base_snap),
                                            _assignments=_assignments,
                                            _inbound_assignments=_inbound_assignments,
                                            _outbound_assignments=_outbound_assignments,
                                            attempt_src=_attempt_src,
                                        )
                                        return _validate_generated_roster(
                                            _g3, _rs3, nurses_context=list(nurses_for_engine or []),
                                            config_context=_probe_base,
                                        )

                                    _err3 = _quiet_verify_solve(_do_nc)
                                finally:
                                    _cnu.__dict__[_cfld] = _orig
                                if _err3 is None:
                                    _nc_culprit = (_cnid, _cfld, _ccur)
                                    break
                            _probe_done()
                            if _nc_culprit:
                                _nid, _fld, _cur = _nc_culprit
                                _nm = next((getattr(n, "name", "") for n in (nurses_for_engine or [])
                                            if str(getattr(n, "nurse_id", "")) == _nid), "") or _nid
                                _fld_ko = "야간 고정 횟수" if _fld == "n_exact" else "월 야간 최대"
                                _nc_opt = {
                                    "option_id": f"lower_night_limit:{_nid}",
                                    "kind": "lower_night_limit", "source": "probe", "verified": True,
                                    "title_ko": f"{_nm} 간호사 {_fld_ko} {_cur}→{_cfg_mn}회로 낮추기",
                                    "trade_off_ko": f"그 간호사의 월 야간이 {_cfg_mn}회 이하로 제한됩니다.",
                                    "changes": [_mk_change(_nid, _fld, _cur, _cfg_mn)],
                                    # 원클릭 재생성용 — 이 달 NurseMonthlyLimit 에 하향 write 후 생성.
                                    "monthly_limit_release": [
                                        {"nurse_id": _nid, "field": _fld, "value": _cfg_mn}],
                                    "fix": {
                                        "mode": "auto_apply",
                                        "where": "nurse.monthly_limit.night",
                                        "where_label_ko": "간호사 관리 > 해당 간호사 > 월 근무 한도(야간)",
                                        "how_ko": f"이 방법을 고르면 {_nm} 간호사의 {_fld_ko}를 "
                                                  f"{_cfg_mn}회로 낮추고 다시 만듭니다(이 달).",
                                        "config_key": None,
                                        "target": {"nurse_id": _nid, "field": _fld},
                                    },
                                }
                                # config 야간 상한 완화(전체)는 그대로 두고, 개인 지목을 앞에 추가(둘 다 노출).
                                _opts_now = unrecoverable["infeasibility"].get("resolution_options") or []
                                unrecoverable["infeasibility"]["resolution_options"] = [_nc_opt] + _opts_now
                                print(f"[UndiagProbe][per-nurse] 야간한도 병목 지목: "
                                      f"{_nid}({_nm}) {_fld}={_cur}→{_cfg_mn}")
                            else:
                                print(f"[UndiagProbe][per-nurse] 야간한도 단일 하향 미해결 "
                                      f"(후보 {len(_nc_cands)}, 검사 {min(len(_nc_cands), _PN_K)})")
                        except Exception as _nc_exc:
                            print(f"[UndiagProbe][per-nurse] night-limit failed (ignore): {_nc_exc}")
                    # probe 의 검증된 완화 = 원인 그 자체다: "이걸 완화하면 풀린다" ⟺ "이게 병목".
                    # MUS core 가 비어도 probe 가 원인(=완화 대상 정책)을 지목한다.
                    # 단일 완화들 = 각각이 단독 충분한 병목(대안), combo = 함께여야 풀리는 결합 병목.
                    # 원인·해결 요약: config 레버뿐 아니라 per-nurse(주말/야간/번들/다인) 옵션까지
                    # 전부 읽어 만든다. 검증된 해결책이 하나라도 있으면 NO_ASSIGNMENT 증상 문구를
                    # 절대 사용자에게 노출하지 않는다(그건 내부 트리거일 뿐).
                    import re as _re_cause
                    def _cause_label(_t):
                        # title 의 액션어(완화/해제/…)를 떼 읽기 쉽게. per-nurse 는 원문 유지.
                        return _re_cause.sub(r"\s*(완화|해제|비활성화|감소|상향)(\(.*?\))?$", "", _t or "").strip() or _t
                    # EI-11 싱크 d — 요약 문구가 하드락 카드를 거른 목록만 읽게 한다(예외를 내지 않는다).
                    from services.policy.card_filter import finalize_infeasibility as _finalize_cards
                    _finalize_cards(unrecoverable["infeasibility"])
                    _all_opts = unrecoverable["infeasibility"].get("resolution_options") or []
                    _v_opts = [o for o in _all_opts if o.get("verified") and o.get("title_ko")]
                    _v_combo = next((o for o in _v_opts if o.get("kind") == "combo"), None)
                    _v_single = [o for o in _v_opts if o.get("kind") != "combo"]
                    if _v_single:
                        _cause = " / ".join(_cause_label(o["title_ko"]) for o in _v_single[:4])
                        unrecoverable["infeasibility"]["summary_message_ko"] = (
                            f"지금 인원·설정으로는 근무표를 만들 수 없어요 — {_cause} 중 하나로 "
                            f"조정하면 만들 수 있습니다(다시 계산해 확인했습니다). 아래에서 골라 주세요."
                        )
                    elif _v_combo:
                        unrecoverable["infeasibility"]["summary_message_ko"] = (
                            f"여러 조건이 얽혀 있어 하나만 바꿔서는 풀리지 않습니다. "
                            f"'{_v_combo.get('title_ko')}'를 함께 적용하면 근무표를 만들 수 있어요(확인 완료)."
                        )
                    else:
                        # 검증된 해결책이 하나도 없을 때: 증상('실근무 0건') 대신 행동 안내로 대체.
                        _cur_msg = unrecoverable["infeasibility"].get("summary_message_ko") or ""
                        if ("배정 0건" in _cur_msg) or ("NO_ASSIGNMENT" in _cur_msg):
                            unrecoverable["infeasibility"]["summary_message_ko"] = (
                                "지금 인원·설정으로는 근무표를 만들 수 없어요. 간호사 인원을 보강하거나 "
                                "설정 > 날짜별 필요 인원을 줄여 다시 시도해 주세요."
                            )
                    _combo = _probe_res.get("combo")
                    print(f"[UndiagProbe] found={_probe_res.get('found')} "
                          f"resolutions={[r['id'] for r in _probe_res.get('resolutions', [])]} "
                          f"combo={_combo.get('id') if _combo else None} "
                          f"baseline_feasible={_probe_res.get('baseline_feasible')} "
                          f"timed_out={_probe_res.get('timed_out')} solves={_probe_cnt[0]}")
            except Exception as _undiag_exc:
                print(f"[UndiagProbe] failed (ignore): {_undiag_exc}")
            # (ontology treatment → resolution_options 는 build_unrecoverable_payload 에서 처리됨)
            # 최종 안전망: 어떤 경로(probe 스킵/실패 포함)로 왔든 NO_ASSIGNMENT 증상 문구가
            # 사용자에게 새지 않게 대체한다. 이 코드는 트리거(reason_code)와 무관, 문구만 정리.
            # EI-11 싱크 e — 탐색이 건너뛰었거나 실패해도 아래 문구·기록·_sole_option·raise 가 거른 카드만 본다.
            from services.policy.card_filter import finalize_infeasibility as _finalize_cards
            _finalize_cards(unrecoverable.get("infeasibility"))
            _fin = unrecoverable.get("infeasibility", {})
            _fmsg = _fin.get("summary_message_ko") or ""
            if ("배정 0건" in _fmsg) or ("NO_ASSIGNMENT" in _fmsg):
                _fv = [o for o in (_fin.get("resolution_options") or [])
                       if o.get("verified") and o.get("title_ko")]
                _fin["summary_message_ko"] = (
                    "지금 인원·설정으로는 근무표를 만들 수 없어요. "
                    "아래에서 방법을 골라 조정하면 만들 수 있습니다(다시 계산해 확인했습니다)."
                    if _fv else
                    "지금 인원·설정으로는 근무표를 만들 수 없어요. 간호사 인원을 보강하거나 "
                    "설정 > 날짜별 필요 인원을 줄여 다시 시도해 주세요."
                )
            inf = unrecoverable.get("infeasibility", {})
            # solver 까지 갔으나 못 풀었다 — blocked 와 구분해 기록한다.
            #   이쪽은 인원·요구가 늘거나 줄면 결과가 달라지므로 스냅샷이 특히 중요하다.
            #   precheck 블록에서 만든 상세 입력을 그대로 넘긴다(없으면 헬퍼가 DB 로 폴백).
            _mark_config_generate_status(
                db, latest_config, "infeasible",
                _build_generate_snapshot(db, latest_config, "infeasible",
                                         req.year, req.month,
                                         _nurses_dict_for_precheck, precheck_config,
                                         job_id, _persistent_fix),
                req.year, req.month, job_id,
                _base_intervened or bool(applied_relaxations))
            if _ctx is not None:
                _ctx["recorded"] = True
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
            # 개인 즉시모순: cause 액션카드만 노출(per-nurse MCS 가 덧붙인 개별카드 제거).
            #   단일(dict)=월한도/주말 통합, 리스트=금지근무 대안 2카드.
            _so = unrecoverable.get("infeasibility", {}).pop("_sole_option", None)
            if _so:
                unrecoverable["infeasibility"]["resolution_options"] = _so if isinstance(_so, list) else [_so]
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
    # ── 수면OFF 부여 후처리 (sleep_off_enabled=True 일 때만) ──
    #   N 연번이 cycle(15)에 도달한 사람에게 그 N 블록 종료 후 근무일 하나를 수면OFF 로 치환.
    #   ★ OFF 가 아니라 근무일을 치환한다 — 실측(2026-08)에서 수면 수령자의 OFF 는
    #     비수령자와 같고(11.00 vs 11.24) 쉼만 늘었다(12.00 vs 11.24).
    #     OFF 를 치환하면 countable_off 가 줄어 off_days 미달이 된다.
    #   ★ 근무를 빼면 그날 인원이 1 줄므로 daily_shift 필요 인원을 넘길 때만 뺀다.
    #   off_swap 보다 먼저 — 생성 결과에 더 가까운 상태에서 판정한다.
    # 보건휴가: 대상자의 OFF(고정근무자는 근무일) 하나를 휴가코드로 치환.
    #   커버리지 무관 — 근무를 빼는 게 아니라 OFF 자리의 코드만 바꾼다.
    # 생성 결과 요약 — 응답에 실어 화면이 "몇 명에게 줬고 몇 건이 이월됐는지" 를
    # 보여줄 수 있게 한다. 로그로만 남기면 운영자가 확인할 방법이 없다.
    _leave_summary: dict = {}
    try:
        from services.leave.health_leave_postprocess import postprocess_health_leave
        _hl_stats: dict = {}
        generated = postprocess_health_leave(
            db, schedule, generated, health_leave_targets, latest_config,
            fixed_ids={str(getattr(_fn, "nurse_id", "")) for _fn in fixed_nurses},
            stats=_hl_stats,
        )
        if _hl_stats:
            _leave_summary["health_leave"] = _hl_stats
    except Exception as _hl_post_exc:
        print(f"[HealthLeave] 후처리 실패 — 미부여 진행: {_hl_post_exc}")
    try:
        from services.leave.sleep_off_postprocess import postprocess_sleep_off
        _so_stats: dict = {}
        generated = postprocess_sleep_off(db, schedule, generated, latest_config,
                                          stats=_so_stats)
        if _so_stats:
            _leave_summary["sleep_off"] = _so_stats
    except Exception as _sleep_exc:
        print(f"[SleepOff] 후처리 실패 — 미부여 진행: {_sleep_exc}")
    try:
        generated = postprocess_off_swap(db, schedule, generated, latest_config, req)
    except Exception as _off_swap_exc:
        print(f"[OffSwap] 후처리 실패 — 변환 미적용 진행: {_off_swap_exc}")
    # 휴직 가림막 — `blocked_by_nurse_id`(:4990)는 **솔버 전용**이라 `fixed_shift` 를 가진
    # 사람에게는 닿지 않는다. `_split_fixed_nurses` 로 갈라져 솔버를 안 타고 고정근무
    # 전개(평일=fixed_shift·주말=OFF)로 채워지기 때문. 그 구멍만 여기서 막는다.
    # ★ 콜보다 먼저 — 휴직자가 남아 있으면 콜 후보로 잡힐 수 있다.
    try:
        from services.leave_mask_postprocess import postprocess_leave_mask
        generated = postprocess_leave_mask(db, schedule, generated, current_user, req)
    except Exception as _leave_mask_exc:
        print(f"[LeaveMask] 후처리 실패 — 가림 미적용 진행: {_leave_mask_exc}")
    # 콜 당번 — 근무를 빼지 않고 코드만 갈아끼운다(D1→D1콜 · O→오프콜).
    # ★ 검증(_validate_generated_roster)·재시도가 모두 끝난 뒤에 얹는다. 앞에 두면
    #   검증이 모르는 콜 코드를 위반으로 잡을 수 있다.
    # ★ 사용 여부 스위치는 따로 없다 — `shifts.call_base_id` 미등록 그룹은 안에서
    #   즉시 원본을 돌려준다.
    try:
        from services.oncall_postprocess import postprocess_oncall
        # ★ 솔버가 쓴 그 프리셋을 넘긴다. 안 넘기면 후처리가 **최신** config 를 읽어
        #   옛 프리셋으로 생성했을 때 솔버와 다른 판정을 한다(콜 금지일이 어긋난다).
        generated = postprocess_oncall(
            db, schedule, generated, current_user, req,
            config_id=getattr(latest_config, "config_id", None),
        )
    except Exception as _oncall_exc:
        print(f"[Oncall] 후처리 실패 — 콜 미부여 진행: {_oncall_exc}")
    # ★ S09 알림은 근무표 저장과 **같은 커밋**에 남긴다(`_persist_entries` 끝의 commit).
    #   저장 뒤 따로 넣으면 그 사이 죽었을 때 근무표는 있는데 알림 근거가 없다(Codex 2026-10-02).
    _enqueue_assignment_created_push(db, req, current_user, _assignments, nurses_in_group)
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
    # 작성 중인 상대 병동 근무표를 기준으로 경계·상한을 맞춘 경우의 안내(EI-11 11w-2 · D34·D38) — 월중 경계·나이트 합산은 이 함수가,
    #   월경계 꼬리(1일 합류·1일 복귀)는 엔진 시도(`_run_cp_sat_basic`)가 모았다. 주휴 경고와 섞이지 않게 따로 싣는다(D39 —
    #   `utils/roster_quality.summarize` 가 `weekly_off_warnings` 개수를 Slack '주간 OFF 경고'로 센다). 있을 때만 싣는다.
    _boundary_notices = _other_ward_draft_notices(
        db, [*_other_ward_refs, *(getattr(roster_system, "_boundary_other_ward_refs", None) or [])])
    if _boundary_notices:
        roster_data["boundary_notices"] = _boundary_notices
    roster_data["constraint_impact"] = _build_constraint_impact_payload(roster_system, req)
    # 솔버 전 부족 조기진단(있으면) 부착 — 실측 부족은 coverage_gaps(constraint_impact 내부),
    # 여기는 '왜/얼마나(하한) + 관리자 복구 선택지' advisory.
    if presolve_diag:
        roster_data["presolve_diagnosis"] = presolve_diag
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
                and isinstance(_inf_a1, dict)):
            from services.cp_sat.undiagnosed_probe import probe_relaxations, probe_status_fields, to_resolution_options
            from services.policy.card_filter import finalize_infeasibility as _finalize_cards
            # EI-11 11e — 이 경로엔 탐색 단계가 있다: 끔(UNDIAG_PROBE_DISABLE)·기준 스냅샷 없음이면 세 칸이 null 로 남는다.
            _inf_a1.update(probe_status_fields(None))
            _a1_base = getattr(roster_system, "_effective_config_snapshot", None)
            if _os_a1.getenv("UNDIAG_PROBE_DISABLE") != "1" and _a1_base:
                def _a1_resolve(_relaxed_cfg):
                    _g2, _, _rs2 = _run_cp_sat_basic(
                        db, current_user, nurses_for_engine, preferences, latest_config, req,
                        shift_manage_data,
                        fixed_cells=combined_fixed_cells if combined_fixed_cells else None,
                        time_limit_seconds=_probe_time_limit(), config_override=_relaxed_cfg,
                        _assignments=_assignments, _inbound_assignments=_inbound_assignments,
                        _outbound_assignments=_outbound_assignments,
                        attempt_src=_attempt_src,
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
                _finalize_cards(_inf_a1)  # EI-11 싱크 f — 목록을 제자리에서 거르므로 아래 _a1_opts 도 거른 값이다
                _inf_a1.update(probe_status_fields(_a1_res))  # EI-11 11e — 탐색 결과 세 칸
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

    # 휴가 자동 부여 요약 — 화면이 생성 결과를 검토할 수 있게 싣는다.
    #   ★ 기능이 꺼져 있거나 대상 코드가 없으면 후처리가 stats 를 안 채우므로 키 자체가
    #     없다. 프론트는 키 유무로 "이번 생성에 적용됐는가" 를 판단하면 된다.
    #   ★ sleep_off.carried_out 은 실패가 아니라 **다음 달 이월**이다.
    if _leave_summary and isinstance(roster_data, dict):
        roster_data["leave_summary"] = _leave_summary

    # 여기까지 왔으면 이 설정으로 근무표가 만들어졌다 — 프리셋 목록에 남겨도 된다.
    #   ★ 성공에도 스냅샷을 남긴다. 실패 스냅샷만 있으면 **비교 대상이 없어** 분석이 안 된다.
    #     "1월엔 됐는데 2월엔 왜 안 되지" 는 직전 성공 조건과 대조해야 답이 나온다
    #     (명단이 줄었는지 · daily_shift 요구가 늘었는지 · 원티드가 몰렸는지).
    #     attempt 에 한 행씩 누적되므로 성공/실패를 시간순으로 대조할 수 있다.
    _mark_config_generate_status(
        db, latest_config, "success",
        _build_generate_snapshot(db, latest_config, "success", req.year, req.month,
                                 _nurses_dict_for_precheck, precheck_config, job_id,
                                 _persistent_fix),
        req.year, req.month, job_id,
        _success_intervened_base or bool(applied_relaxations))
    if _ctx is not None:
        _ctx["recorded"] = True
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
    # ★ 번호를 읽기 전에 그룹·달 잠금 — 새 버전 저장·복사와 동시에 같은 VER 를 만들지 않게.
    #   생성 작업은 30초까지 기다린다(409 면 워커가 실패 알림·재시도로 번진다).
    from services.schedule_versioning import lock_version_numbering
    lock_version_numbering(db, current_user.group_id, req.year, req.month, timeout_ms=30000)
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
