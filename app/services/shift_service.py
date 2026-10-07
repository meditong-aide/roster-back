"""
시프트(근무코드) 관리 관련 서비스 로직 모듈
- DB 쿼리, 데이터 가공 등 라우터에서 분리
- 엑셀 일괄 업로드(템플릿/검증/확정) 포함
- 모든 함수는 한글 docstring, 한글 print/logging, PEP8 스타일 적용
"""
import math
import re
import tempfile
from datetime import time as dt_time
from typing import Any, Dict, List, Optional, Set

import pandas as pd
from fastapi import HTTPException
from openpyxl.styles import Font, PatternFill
from sqlalchemy.orm import Session
from sqlalchemy import func

from db.models import Shift, Nurse, Group, ShiftManage, ScheduleEntry
from schemas.auth_schema import User as UserSchema
from services.group_access import (
    caller_is_head_nurse,
    resolve_effective_group,
    resolve_home_group_id,
)


def _assert_single_target_flag(
    db: Session,
    *,
    group_id: str,
    column,
    label: str,
    target_value: bool,
    shift_type: Optional[str],
    self_id: Optional[int] = None,
) -> None:
    """'그룹당 1건' 타깃 플래그의 공통 저장 정책 검증.

    off_swap_target / health_leave_target / sleep_off_target 이 같은 규칙을 쓴다.

    1) 대상 shift 의 type 은 '근무' 가 아니어야 한다.
       (근무 코드를 타깃으로 두면 OFF→근무 치환·휴가 주입 시 일별 coverage 가 어긋난다)
    2) 동일 group_id 내 True 인 row 는 단 1건만 허용.
       (다수면 resolver 가 sequence ASC 첫 번째만 채택해 의도와 다른 결과)

    target_value=False 면 검증 skip. self_id 는 update 시 본인 row 를 비교 대상에서 제외.
    """
    if not target_value:
        return
    if str(shift_type or "").strip() == "근무":
        raise HTTPException(
            status_code=400,
            detail=f"{label}=True 는 type='근무' 인 근무코드에는 설정할 수 없습니다.",
        )
    q = db.query(Shift).filter(Shift.group_id == group_id, column == True)  # noqa: E712
    if self_id is not None:
        q = q.filter(Shift.id != self_id)
    other = q.first()
    if other is not None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{label}=True 는 그룹당 1건만 설정 가능합니다. "
                f"기존 설정: shift_id={other.shift_id}, name={other.name}"
            ),
        )


def _assert_off_swap_target_valid(
    db: Session,
    *,
    group_id: str,
    target_value: bool,
    shift_type: Optional[str],
    self_id: Optional[int] = None,
) -> None:
    """off_swap_target=True 저장 전 정책 검증 (초과 OFF → 연차 변환 타깃)."""
    _assert_single_target_flag(
        db,
        group_id=group_id,
        column=Shift.off_swap_target,
        label="off_swap_target",
        target_value=target_value,
        shift_type=shift_type,
        self_id=self_id,
    )


def _assert_health_leave_target_valid(
    db: Session,
    *,
    group_id: str,
    target_value: bool,
    shift_type: Optional[str],
    self_id: Optional[int] = None,
) -> None:
    """health_leave_target=True 저장 전 정책 검증 (보건휴가 부여 대상 코드).

    ★ off_swap 과 규칙만 같고 동작은 무관하다 — 보건휴가는 생성 전 사전 주입이며
      OFF 를 변환하지 않는다. 상세: docs/leave_auto_assignment_design.md §4.1
    """
    _assert_single_target_flag(
        db,
        group_id=group_id,
        column=Shift.health_leave_target,
        label="health_leave_target",
        target_value=target_value,
        shift_type=shift_type,
        self_id=self_id,
    )


def _assert_sleep_off_target_valid(
    db: Session,
    *,
    group_id: str,
    target_value: bool,
    shift_type: Optional[str],
    self_id: Optional[int] = None,
) -> None:
    """sleep_off_target=True 저장 전 정책 검증 (수면OFF 부여 대상 코드)."""
    _assert_single_target_flag(
        db,
        group_id=group_id,
        column=Shift.sleep_off_target,
        label="sleep_off_target",
        target_value=target_value,
        shift_type=shift_type,
        self_id=self_id,
    )


# ──────────────────────────────────────────────────────────────
# 색상 풀 (brightness ≤ 186 사전 필터링, 흰색 텍스트 가독성 보장)
# ──────────────────────────────────────────────────────────────
COLOR_POOLS: Dict[str, List[str]] = {
    "데이": ["#42AF3A", "#4AA36E", "#0A9999"],
    "이브닝": ["#5777F3", "#7589B8", "#6698CB", "#1C80F1"],
    "나이트": ["#A184E5", "#7C38C0", "#C082DD", "#976BA7", "#C69EC4"],
    "미드": ["#E6A817", "#D4942A", "#C98B2A", "#B8860B", "#DAA520"],
    "holiday": [
        "#EC71A2", "#F45BA8", "#F5A4F0", "#F29B87",
        "#FFA24A", "#FF7D3B", "#F54851",
    ],
    "etc": ["#CF847A", "#B5704F", "#A67B5B", "#8B6E5A", "#9C6644", "#7F5539"],
}

# shift_gb 한글 → ShiftManage slot_map 영문 코드 변환
SHIFT_GB_TO_SLOT_CODE: Dict[str, str] = {
    "데이": "D",
    "이브닝": "E",
    "나이트": "N",
    "미드": "M",
}

# shift_gb 허용 값
VALID_SHIFT_GB = {"데이", "이브닝", "나이트", "미드", "고정"}



def _pick_pool_key(shift_gb: Optional[str], shift_type: str) -> str:
    """shift_gb / type 으로 색상 풀 키 결정."""
    if shift_gb in ("데이", "이브닝", "나이트", "미드"):
        return shift_gb
    if shift_type == "휴무":
        return "holiday"
    return "etc"


def assign_color(
    shift_gb: Optional[str],
    shift_type: str,
    existing_colors: Set[str],
    counter: Dict[str, int],
) -> str:
    """적절한 풀에서 색상을 순차 배정. 미사용 색상 우선, 소진 시 순환."""
    pool_key = _pick_pool_key(shift_gb, shift_type)
    pool = COLOR_POOLS[pool_key]
    start = counter.get(pool_key, 0)

    for i in range(len(pool)):
        candidate = pool[(start + i) % len(pool)]
        if candidate not in existing_colors:
            counter[pool_key] = (start + i + 1) % len(pool)
            return candidate

    color = pool[start % len(pool)]
    counter[pool_key] = (start + 1) % len(pool)
    return color


def _parse_time_str(val: Optional[str]) -> Optional[dt_time]:
    """'HH:MM' 문자열 → datetime.time 변환."""
    if not val:
        return None
    parts = val.strip().split(":")
    if len(parts) >= 2:
        return dt_time(int(parts[0]), int(parts[1]))
    return None


def _parse_boolean(value: Any) -> bool:
    """Y/N/YES/TRUE/1/T/참/예 → bool."""
    if value is None:
        return False
    if isinstance(value, float) and math.isnan(value):
        return False
    s = str(value).strip().upper()
    return s in ("Y", "YES", "TRUE", "1", "T", "참", "예")


def _safe_str(value: Any) -> str:
    """NaN-safe 문자열 변환."""
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    return str(value).strip()


def _is_guide_row(row: pd.Series) -> bool:
    """입력 가이드 행 판별."""
    for v in row.values:
        s = _safe_str(v)
        if "입력 가이드" in s or (s.startswith("(") and s.endswith(")")):
            return True
    return False


# shift_gb(영문 D/E/N/M 또는 한글) → ShiftManage.shift_slot
_SHIFT_GB_TO_SLOT = {"D": 1, "E": 2, "N": 3, "M": 5, "데이": 1, "이브닝": 2, "나이트": 3, "미드": 5}
# shift_gb(한글) → main_code(영문). 영문 키는 그대로 통과한다.
_SHIFT_GB_TO_MAIN_CODE = {"데이": "D", "이브닝": "E", "나이트": "N", "미드": "M"}


def _resolve_slot_main(shift_gb: str | None) -> tuple[int | None, str | None]:
    """shift_gb → (shift_slot, main_code). 매핑이 없으면 (None, None)."""
    if shift_gb not in _SHIFT_GB_TO_SLOT:
        return None, None
    return _SHIFT_GB_TO_SLOT[shift_gb], _SHIFT_GB_TO_MAIN_CODE.get(shift_gb, shift_gb)


def _remove_shift_manage_code(
    db: Session,
    office_id: str | None,
    group_id: str,
    shift_id: str | None,
    shift_gb: str | None,
) -> bool:
    """
    shift_manage.codes 에서 shift_id 를 제거합니다.

    - 근무코드 삭제/변경 시 orphan code 가 codes 에 남지 않도록 정리하는 공용 헬퍼.
    - 커밋은 호출자 책임(원자적 트랜잭션 보존).
    - ★ 슬롯을 `shift_gb` 로 좁히지 않는다. 예전엔 좁혔는데, 두 경우에 고아가 남았다.
      ① `shift_gb` 가 슬롯으로 안 풀리면(None·비표준값) 아무 것도 못 지우고 반환했다.
      ② 코드의 `shift_gb` 를 바꿔 온 이력이 있으면 **이전 슬롯**의 등록이 그대로 남았다.
      남은 고아는 그 슬롯의 대체코드로 읽혀 커버리지 계산에 조용히 섞인다.
      근무코드는 그룹 안에서 정확히 한 슬롯에만 속하므로 그룹 전체를 훑어 지우는 편이
      안전하다. `shift_gb` 는 이제 쓰지 않지만 호출부 시그니처 호환을 위해 남겨 둔다.
    """
    if not office_id or not shift_id:
        return False
    rows = (
        db.query(ShiftManage)
        .filter(
            ShiftManage.office_id == office_id,
            ShiftManage.group_id == group_id,
        )
        .all()
    )
    removed = False
    for shift_manage in rows:
        codes = shift_manage.codes or []
        if shift_id in codes:
            shift_manage.codes = [code for code in codes if code != shift_id]
            removed = True
    return removed


def _append_shift_manage_code(
    db: Session,
    office_id: str | None,
    group_id: str,
    shift_id: str,
    shift_gb: str | None,
    old_shift_id: str | None = None,
    old_shift_gb: str | None = None,
    commit: bool = True,
) -> None:
    """
    shift_manage.codes에 근무코드를 중복 없이 추가합니다.

    - shift_gb가 D/E/N/M(또는 한글)일 때만 동작하며, slot은 1/2/3/5에 매핑됩니다.
    - 기존에 코드가 있으면 추가하지 않습니다.
    - update 시 shift_id 또는 shift_gb가 바뀌면 이전 슬롯에서 제거한 뒤 새 슬롯에 추가합니다.
    - `commit=False` 면 커밋하지 않는다. 여러 코드를 한 트랜잭션으로 묶어야 하는 호출부
      (근무코드 일괄 가져오기)가 쓴다 — 코드마다 커밋하면 중간에 실패했을 때 앞쪽 코드는
      확정되고 뒤쪽은 누락된 채로 남는다.
    """
    if not office_id:
        return

    def _append(target_gb: str | None, target_id: str) -> bool:
        target_slot, main_code = _resolve_slot_main(target_gb)
        if target_slot is None:
            return False
        shift_manages = (
            db.query(ShiftManage)
            .filter(
                ShiftManage.office_id == office_id,
                ShiftManage.group_id == group_id,
                ShiftManage.shift_slot == target_slot,
                ShiftManage.main_code == main_code,
            )
            .all()
        )
        added = False
        for shift_manage in shift_manages:
            codes = shift_manage.codes or []
            if shift_id not in codes:
                shift_manage.codes = codes + [shift_id]
                added = True
        return added

    removed_any = False
    if old_shift_id and (old_shift_id != shift_id or old_shift_gb != shift_gb):
        removed_any = _remove_shift_manage_code(db, office_id, group_id, old_shift_id, old_shift_gb)

    added_any = _append(shift_gb, shift_id)

    if commit and (removed_any or added_any):
        db.commit()


def shift_code_taken(
    db: Session,
    office_id: str | None,
    group_id: str,
    shift_id: str,
    exclude_row_id: int | None = None,
) -> bool:
    """같은 병동에 이 근무코드가 이미 있는지.

    ★ 유일성 키는 **(office_id, group_id, shift_id)** 다. `group_id` 만으로 잡으면 안 된다 —
      `group_id` 가 빈 문자열인 행이 오피스마다 따로 존재해서(ADM 이 병동 지정 없이 목록을
      열면 그 오피스 몫으로 기본코드가 깔린다) `group_id` 만 보면 남의 오피스 행을 자기
      중복으로 오판한다. 조회 경로도 전부 office_id + group_id 로 필터한다.
    ★ `shifts` 에는 이 조합의 UNIQUE 제약이 아직 없다. 그래서 이 검사가 유일한 방어선이고,
      동시 요청 두 건은 여전히 함께 통과할 수 있다(제약이 생기면 IntegrityError 로 막힌다).
      그때까지의 순차 중복만이라도 여기서 끊는다.

    Args:
        exclude_row_id: 수정 중인 자기 자신(`shifts.id`)은 충돌에서 제외한다.
    """
    if not shift_id:
        return False
    q = db.query(Shift.id).filter(
        Shift.group_id == group_id,
        Shift.shift_id == shift_id,
    )
    if office_id is not None:
        q = q.filter(Shift.office_id == office_id)
    if exclude_row_id is not None:
        q = q.filter(Shift.id != exclude_row_id)
    # ★ `db.query(q.exists()).scalar()` 를 쓰면 안 된다 — SQLAlchemy 가 `SELECT EXISTS(...)`
    #   를 만드는데 MSSQL 에는 그 문법이 없어 "Incorrect syntax near the keyword 'EXISTS'"
    #   로 죽는다(실측). 한 행만 집어 유무를 본다.
    return q.first() is not None


def add_shift_service(req, current_user, db, override_group_id: str | None = None):
    """
    시프트 등록 서비스 함수
    """
    print('----------------------------------[add_shift_service] group_id', override_group_id)
    if not current_user or not (caller_is_head_nurse(db, current_user) or getattr(current_user, 'is_master_admin', False)):
        raise Exception("Permission denied")
    # 그룹 스코프: 조회(LIST)와 동일하게 토큰 group_id 기준으로 group_access 모듈에서 해석+권한검증.
    target_group_id = resolve_effective_group(db, current_user, override_group_id or current_user.group_id)
    if override_group_id:
        g = db.query(Group).filter(Group.group_id == target_group_id).first()
        if not g:
            raise Exception("Group not found")
        office_id = g.office_id
    else:
        nurse = db.query(Nurse).filter(Nurse.nurse_id == current_user.nurse_id).first()
        if not nurse or not nurse.group:
            raise Exception("User group information not found")
        office_id = nurse.group.office_id
    if shift_code_taken(db, office_id, target_group_id, req.shift_id):
        raise Exception("이미 존재하는 근무코드입니다.")
    _assert_off_swap_target_valid(
        db,
        group_id=target_group_id,
        target_value=bool(getattr(req, "off_swap_target", False) or False),
        shift_type=getattr(req, "type", None),
        self_id=None,
    )
    _assert_health_leave_target_valid(
        db,
        group_id=target_group_id,
        target_value=bool(getattr(req, "health_leave_target", False) or False),
        shift_type=getattr(req, "type", None),
        self_id=None,
    )
    _assert_sleep_off_target_valid(
        db,
        group_id=target_group_id,
        target_value=bool(getattr(req, "sleep_off_target", False) or False),
        shift_type=getattr(req, "type", None),
        self_id=None,
    )
    max_sequence = db.query(func.max(Shift.sequence)).filter(Shift.group_id == target_group_id).scalar() or 0
    new_shift = Shift(
        shift_id=req.shift_id,
        office_id=office_id,
        group_id=target_group_id,
        name=req.name,
        color=req.color,
        start_time=req.start_time,
        end_time=req.end_time,
        type=req.type,
        duration=req.duration,
        allday=req.allday,
        auto_schedule=req.auto_schedule,
        sequence=max_sequence + 1,
        shift_gb=req.shift_gb,
        # ★ 요청의 default_shift 를 저장한다. 빠뜨리면 NULL 로 들어가 주휴 식별
        #   (SSOT 가 shifts.default_shift 다)·MID 판정 같은 하위 로직이 조용히 어긋난다.
        default_shift=getattr(req, "default_shift", None),
        # 추가
        show_in_preference=getattr(req, "show_in_preference", False), # 프론트 미 전송 시 False
        off_swap_target=bool(getattr(req, "off_swap_target", False) or False),
        health_leave_target=bool(getattr(req, "health_leave_target", False) or False),
        sleep_off_target=bool(getattr(req, "sleep_off_target", False) or False),
        description=getattr(req, "description", None),
    )
    db.add(new_shift)
    # ★ 근무코드 저장과 슬롯 등록을 한 트랜잭션으로 확정한다(업로드·가져오기 경로와 동일).
    #   먼저 커밋해 버리면 슬롯 등록에서 실패했을 때 코드만 남고, 같은 코드를 다시 넣으려
    #   해도 중복검사에 걸려 **슬롯 누락을 복구할 방법이 없다.**
    db.flush()
    db.refresh(new_shift)
    try:
        _append_shift_manage_code(
            db=db,
            office_id=office_id,
            group_id=target_group_id,
            shift_id=new_shift.shift_id,
            shift_gb=req.shift_gb,
            commit=False,
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {
        "message": "근무코드가 성공적으로 추가되었습니다.",
        "shift": {
            "shift_id": new_shift.shift_id,
            "name": new_shift.name,
            "color": new_shift.color,
            "sequence": new_shift.sequence,
            "shift_gb": new_shift.shift_gb,
            "description": new_shift.description,
        }
    }


def update_shift_service(req, current_user, db, override_group_id: str | None = None):
    """
    시프트 수정 서비스 함수
    """
    if not current_user or not (caller_is_head_nurse(db, current_user) or getattr(current_user, 'is_master_admin', False)):
        raise Exception("Permission denied")

    # 그룹 스코프: 조회(LIST)와 동일하게 토큰 group_id 기준으로 group_access 모듈에서 해석+권한검증.
    # (HN 그룹 전환 시 home 강제 필터로 근무코드 미발견 → None 접근 크래시 나던 버그 회귀 방지)
    target_group_id = resolve_effective_group(db, current_user, override_group_id or current_user.group_id)
    existing_shift = db.query(Shift).filter(Shift.id == req.id, Shift.group_id == target_group_id).first()
    if not existing_shift:
        raise Exception("해당 근무코드를 찾을 수 없습니다.")
    # off_swap_target 정책 검증 — 변경 후 (type, off_swap_target) 조합 기준.
    _new_target = (
        bool(req.off_swap_target)
        if (hasattr(req, "off_swap_target") and req.off_swap_target is not None)
        else bool(existing_shift.off_swap_target or False)
    )
    _assert_off_swap_target_valid(
        db,
        group_id=target_group_id,
        target_value=_new_target,
        shift_type=req.type,
        self_id=existing_shift.id,
    )
    # health_leave_target 정책 검증 — 동일하게 변경 후 조합 기준.
    _new_health_target = (
        bool(req.health_leave_target)
        if (hasattr(req, "health_leave_target") and req.health_leave_target is not None)
        else bool(existing_shift.health_leave_target or False)
    )
    _assert_health_leave_target_valid(
        db,
        group_id=target_group_id,
        target_value=_new_health_target,
        shift_type=req.type,
        self_id=existing_shift.id,
    )
    # sleep_off_target 정책 검증 — 동일하게 변경 후 조합 기준.
    _new_sleep_target = (
        bool(req.sleep_off_target)
        if (hasattr(req, "sleep_off_target") and req.sleep_off_target is not None)
        else bool(existing_shift.sleep_off_target or False)
    )
    _assert_sleep_off_target_valid(
        db,
        group_id=target_group_id,
        target_value=_new_sleep_target,
        shift_type=req.type,
        self_id=existing_shift.id,
    )
    old_shift_id = existing_shift.shift_id
    old_shift_gb = getattr(existing_shift, "shift_gb", None)
    # ★ 코드 개명 충돌 검사. 예전엔 이 검사가 **아예 없었다** — 이미 있는 코드로 이름을
    #   바꿔도 그대로 저장돼 같은 병동에 같은 코드가 두 행이 됐고, 그 뒤로는 화면이 집는 행과
    #   근무표 생성이 집는 행이 갈렸다(목록은 sequence 순 첫 행, 생성은 dict 마지막 행).
    #   실측된 '같은 shifts.id 에 N→N1, O→OFF, D→Dㅇ' 이력이 이 경로에서 나왔다.
    if str(req.shift_id) != str(old_shift_id) and shift_code_taken(
        db,
        getattr(existing_shift, "office_id", None),
        target_group_id,
        req.shift_id,
        exclude_row_id=existing_shift.id,
    ):
        raise Exception("이미 존재하는 근무코드입니다.")
    # shift_id · name · color · type 은 스키마상 필수라 항상 실려 온다.
    existing_shift.shift_id = req.shift_id
    existing_shift.name = req.name
    existing_shift.color = req.color
    existing_shift.type = req.type
    # ★ 아래 5개는 스키마가 **생략을 허용**한다(Optional + 기본값). 무조건 대입하면 이름만
    #   바꾸는 부분 수정 요청이 근무시간을 NULL 로, allday 를 0 으로, auto_schedule 을 1 로
    #   되돌린다 — 주휴처럼 auto_schedule=0 인 코드가 자동편성 대상으로 바뀌는 식이다.
    #   `shift_gb`·`default_shift` 와 같은 규칙(생략은 보존, 명시적 null 은 반영)으로 맞춘다.
    #   ★ 현재 웹·모바일 프론트는 둘 다 행 전체를 보내므로 이 가드로 동작이 달라지지 않는다.
    #     계약상 열려 있는 구멍만 막는 것이다.
    _sent = getattr(req, "model_fields_set", ())
    for _f in ("start_time", "end_time", "duration", "allday", "auto_schedule"):
        if _f in _sent:
            setattr(existing_shift, _f, getattr(req, _f))
    # ★★ `default_shift` 와 **똑같은 이유로** 보낸 경우에만 반영한다. 스키마 기본값이 None
    #   이라 무조건 대입하면, 이 필드를 안 싣는 화면이 이름만 바꿔 저장해도 기존 '데이' 가
    #   NULL 로 날아간다. 게다가 그러면 old_shift_gb 와 값이 달라져
    #   `_append_shift_manage_code` 가 **모든 슬롯에서 코드를 지우고 어디에도 넣지 못한다**
    #   — 근무코드가 `shift_manage` 에서 사라진 채 커밋된다(실측으로 재현함).
    #   아래 슬롯 등록에도 이 실효값을 넘겨야 저장값과 등록이 어긋나지 않는다.
    if "shift_gb" in getattr(req, "model_fields_set", ()):
        existing_shift.shift_gb = req.shift_gb
    effective_shift_gb = existing_shift.shift_gb
    # ★ 추가와 같은 이유로 수정에서도 반영한다(누락 시 화면에서 바꾼 구분이 저장되지 않는다).
    # ★★ 단 **보낸 경우에만** 반영한다. 스키마 기본값이 None 이라 무조건 대입하면, 이 필드를
    #   모르는 기존 화면이 다른 항목만 고쳐 저장할 때마다 default_shift 가 조용히 지워진다
    #   (주휴 식별·MID 판정이 그때 깨진다).
    # ★★★ 그렇다고 `is not None` 으로 거르면 **명시적 해제(null)** 를 생략과 구분 못 해,
    #   D/E/N 이던 코드를 일반 근무로 되돌릴 방법이 없어진다. `model_fields_set` 은
    #   "요청에 실제로 담겨 온 필드"만 담으므로 생략과 명시적 null 을 정확히 가른다.
    if "default_shift" in getattr(req, "model_fields_set", ()):
        existing_shift.default_shift = req.default_shift
    # 원티드 페이지 노출 여부 업데이트
    if hasattr(req, "show_in_preference") and req.show_in_preference is not None:
        existing_shift.show_in_preference = req.show_in_preference
    # 초과 OFF 변환 타깃 업데이트 (None 이면 기존 값 유지)
    if hasattr(req, "off_swap_target") and req.off_swap_target is not None:
        existing_shift.off_swap_target = bool(req.off_swap_target)
    # 보건휴가 부여 대상 코드 업데이트 (None 이면 기존 값 유지)
    if hasattr(req, "health_leave_target") and req.health_leave_target is not None:
        existing_shift.health_leave_target = bool(req.health_leave_target)
    # 수면OFF 부여 대상 코드 업데이트 (None 이면 기존 값 유지)
    if hasattr(req, "sleep_off_target") and req.sleep_off_target is not None:
        existing_shift.sleep_off_target = bool(req.sleep_off_target)
    # ★ 설명도 같은 규칙 — **생략은 보존, 명시적 null 은 삭제.**
    #   예전엔 무조건 대입해서, 필수 필드만 실은 부분 수정 요청이 설명을 지웠다. 게다가
    #   스키마 주석은 "None 이면 기존 값 유지" 라고 정반대로 적혀 있어 계약이 서로 어긋나
    #   있었다. 프론트가 설명을 지울 때는 null 을 **명시해서** 보내므로 클리어도 그대로 된다.
    if "description" in _sent:
        existing_shift.description = req.description
    # ★ 추가와 같은 이유로 한 트랜잭션이다. 개명 시에는 이전 슬롯 제거와 새 슬롯 등록이
    #   함께 일어나므로, 중간에 끊기면 코드가 **두 슬롯 어디에도 없거나 양쪽에 남는다.**
    db.flush()
    db.refresh(existing_shift)
    try:
        _append_shift_manage_code(
            db=db,
            office_id=existing_shift.office_id,
            group_id=target_group_id,
            shift_id=existing_shift.shift_id,
            shift_gb=effective_shift_gb,
            old_shift_id=old_shift_id,
            old_shift_gb=old_shift_gb,
            commit=False,
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {
        "message": "근무코드가 성공적으로 수정되었습니다.",
        "shift": {
            "shift_id": existing_shift.shift_id,
            "name": existing_shift.name,
            "color": existing_shift.color,
            "sequence": existing_shift.sequence,
            "shift_gb": existing_shift.shift_gb,
            "description": existing_shift.description,
        }
    }


def remove_shift_service(req, current_user, db, override_group_id: str | None = None):
    """
    시프트 삭제 서비스 함수
    """
    if not current_user or not (caller_is_head_nurse(db, current_user) or getattr(current_user, 'is_master_admin', False)):
        raise Exception("Permission denied")
    # 그룹 스코프: 조회(LIST)와 동일하게 토큰 group_id 기준으로 group_access 모듈에서 해석+권한검증.
    target_group_id = resolve_effective_group(db, current_user, override_group_id or current_user.group_id)
    # ★ 중복 행이 있으면 정렬 없는 `.first()` 는 **어느 행을 지울지 보장하지 않는다.**
    #   목록이 보여 주는 행(= sequence ASC, id ASC 첫 행)을 지워야 사용자가 화면에서
    #   고른 것과 실제 삭제분이 일치한다(정본 `shift_service_mssql.SHIFT_LIST_ORDER`).
    existing_shift = (
        db.query(Shift)
        .filter(Shift.shift_id == req.shift_id, Shift.group_id == target_group_id)
        .order_by(Shift.sequence.asc(), Shift.id.asc())
        .first()
    )
    if not existing_shift:
        raise Exception("해당 근무코드를 찾을 수 없습니다.")
    schedule_entries_count = db.query(ScheduleEntry).filter(
        ScheduleEntry.shift_id == req.shift_id
    ).count()
    if schedule_entries_count > 0:
        raise Exception("해당 근무코드는 현재 사용 중이므로 삭제할 수 없습니다.")
    deleted_sequence = existing_shift.sequence
    # 근무코드 삭제 시 shift_manage.codes 에 남는 orphan code 를 정리한다(같은 트랜잭션 커밋).
    _remove_shift_manage_code(
        db,
        existing_shift.office_id,
        target_group_id,
        existing_shift.shift_id,
        getattr(existing_shift, "shift_gb", None),
    )
    db.delete(existing_shift)
    db.query(Shift).filter(Shift.group_id == target_group_id, Shift.sequence > deleted_sequence).update({"sequence": Shift.sequence - 1})
    db.commit()
    return {"message": "근무코드가 성공적으로 삭제되었습니다."}


def move_shift_service(req, current_user, db, override_group_id: str | None = None):
    """
    시프트 순서 이동 서비스 함수
    """
    if not current_user or not (caller_is_head_nurse(db, current_user) or getattr(current_user, 'is_master_admin', False)):
        raise Exception("Permission denied")
    # 그룹 스코프: 조회(LIST)와 동일하게 토큰 group_id 기준으로 group_access 모듈에서 해석+권한검증.
    target_group_id = resolve_effective_group(db, current_user, override_group_id or current_user.group_id)
    # ★ 중복 행이 있으면 정렬 없는 `.first()` 가 **화면에 안 보이는 행**을 옮길 수 있다.
    #   목록과 같은 첫 행을 집는다(정본 `shift_service_mssql.SHIFT_LIST_ORDER`).
    shift_to_move = (
        db.query(Shift)
        .filter(Shift.shift_id == req.shift_id, Shift.group_id == target_group_id)
        .order_by(Shift.sequence.asc(), Shift.id.asc())
        .first()
    )
    if not shift_to_move:
        raise Exception("해당 근무코드를 찾을 수 없습니다.")
    old_sequence = shift_to_move.sequence
    new_sequence = req.new_sequence
    if old_sequence == new_sequence:
        return {"message": "변경사항이 없습니다."}
    if old_sequence < new_sequence:
        db.query(Shift).filter(Shift.group_id == target_group_id, Shift.sequence > old_sequence, Shift.sequence <= new_sequence).update({"sequence": Shift.sequence - 1})
    else:
        db.query(Shift).filter(Shift.group_id == target_group_id, Shift.sequence >= new_sequence, Shift.sequence < old_sequence).update({"sequence": Shift.sequence + 1})
    shift_to_move.sequence = new_sequence
    db.commit()
    return {"message": "근무코드 순서가 성공적으로 변경되었습니다."}


# ──────────────────────────────────────────────────────────────
# 엑셀 일괄 업로드: 템플릿 / 검증 / 확정
# ──────────────────────────────────────────────────────────────

def create_shift_template() -> str:
    """근무코드 엑셀 업로드 템플릿 생성. 임시파일 경로 반환."""
    template_data = {
        "근무코드(필수)":     ["D1", "E1", "N1", "OFF"],
        "근무코드명(필수)":   ["Day근무", "Evening근무", "Night근무", "휴무"],
        "근무구분(선택)":     ["데이", "이브닝", "나이트", ""],
        "근무유형(필수)":     ["근무", "근무", "근무", "휴무"],
        "시작시간(선택)":     ["06:00", "14:00", "22:00", ""],
        "종료시간(선택)":     ["14:00", "22:00", "06:00", ""],
        "종일여부(선택)":     ["N", "N", "N", "Y"],
        "자동스케줄(선택)":   ["Y", "Y", "Y", "Y"],
        "근무시간(선택)":     ["", "", "", ""],
        "주휴여부(선택)":     ["N", "N", "N", "N"],
        "희망근무표시(선택)": ["N", "N", "N", "N"],
    }

    df = pd.DataFrame(template_data)

    with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as tmp:
        path = tmp.name

    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="근무코드", index=False)
        ws = writer.sheets["근무코드"]
        header_font = Font(bold=True)
        header_fill = PatternFill("solid", fgColor="CCCCCC")
        for col in range(1, len(df.columns) + 1):
            cell = ws.cell(row=1, column=col)
            cell.font = header_font
            cell.fill = header_fill

    return path


def shift_upload_validate(
    file_path: str,
    user: UserSchema,
    db: Session,
    group_id: str,
) -> Dict[str, Any]:
    """
    근무코드 엑셀 업로드 검증.
    Returns: {success, errors, rows, summary}
    """
    df = pd.read_excel(file_path, sheet_name=0)
    df = df.dropna(how="all")
    if len(df) > 500:
        raise ValueError("최대 500행까지만 업로드 가능합니다.")

    # ── 컬럼 매칭 ─────────────────────────────────────────────
    def find_col(candidates: List[str]) -> str:
        for c in df.columns:
            if str(c).strip() in candidates:
                return c
        raise ValueError(f"필수 컬럼 누락: {candidates}")

    def find_col_optional(candidates: List[str]) -> Optional[str]:
        for c in df.columns:
            if str(c).strip() in candidates:
                return c
        return None

    col_shift_id = find_col(["근무코드(필수)", "근무코드", "shift_id"])
    col_name     = find_col(["근무코드명(필수)", "근무코드명", "이름", "name"])
    col_type     = find_col(["근무유형(필수)", "근무유형", "type"])
    col_shift_gb = find_col_optional(["근무구분(선택)", "근무구분", "shift_gb"])
    col_start    = find_col_optional(["시작시간(선택)", "시작시간", "start_time"])
    col_end      = find_col_optional(["종료시간(선택)", "종료시간", "end_time"])
    col_allday   = find_col_optional(["종일여부(선택)", "종일여부", "allday"])
    col_auto     = find_col_optional(["자동스케줄(선택)", "자동스케줄", "auto_schedule"])
    col_duration = find_col_optional(["근무시간(선택)", "근무시간", "duration"])
    col_weekly   = find_col_optional(["주휴여부(선택)", "주휴여부", "is_weekly_off"])
    col_pref     = find_col_optional(["희망근무표시(선택)", "희망근무표시", "show_in_preference"])

    # ── 기존 데이터 조회 ──────────────────────────────────────
    existing_shift_ids: Set[str] = set(
        row[0] for row in db.query(Shift.shift_id).filter(Shift.group_id == group_id).all()
    )
    existing_colors: Set[str] = set(
        row[0] for row in db.query(Shift.color).filter(Shift.group_id == group_id).all()
    )

    normalized: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    shift_ids_in_file: Set[str] = set()
    color_counter: Dict[str, int] = {}
    TIME_RE = re.compile(r"^\d{2}:\d{2}$")

    for ridx, row in df.iterrows():
        if _is_guide_row(row):
            continue

        row_errs: List[str] = []

        # --- shift_id ---
        shift_id = _safe_str(row.get(col_shift_id))
        if not shift_id:
            row_errs.append("근무코드 누락")
        elif len(shift_id) > 10:
            row_errs.append("근무코드는 최대 10자까지 가능합니다.")
        elif shift_id in shift_ids_in_file:
            row_errs.append("엑셀 내 중복 근무코드")
        elif shift_id in existing_shift_ids:
            row_errs.append(f"이미 존재하는 근무코드: {shift_id}")

        # --- name ---
        name = _safe_str(row.get(col_name))
        if not name:
            row_errs.append("근무코드명 누락")
        elif len(name) > 20:
            row_errs.append("근무코드명은 최대 20자까지 가능합니다.")

        # --- shift_gb (데이/이브닝/나이트/고정 또는 빈값) ---
        shift_gb = _safe_str(row.get(col_shift_gb)) if col_shift_gb else ""
        shift_gb = shift_gb if shift_gb else None
        if shift_gb and shift_gb not in VALID_SHIFT_GB:
            row_errs.append("근무구분은 데이, 이브닝, 나이트, 미드, 고정 중 하나여야 합니다.")

        # --- type ---
        shift_type = _safe_str(row.get(col_type))
        if not shift_type:
            row_errs.append("근무유형 누락")
        elif shift_type not in ("근무", "휴무"):
            row_errs.append("근무유형은 '근무' 또는 '휴무'여야 합니다.")

        # --- start_time / end_time ---
        start_time_str: Optional[str] = None
        end_time_str: Optional[str] = None
        for label, col_t, target in [("시작시간", col_start, "start"), ("종료시간", col_end, "end")]:
            if col_t is None:
                continue
            val = _safe_str(row.get(col_t))
            if not val:
                continue
            if not TIME_RE.match(val):
                row_errs.append(f"{label} 형식 오류 (HH:MM)")
            else:
                h, m = int(val[:2]), int(val[3:5])
                if h > 23 or m > 59:
                    row_errs.append(f"{label} 범위 오류")
                elif target == "start":
                    start_time_str = val
                else:
                    end_time_str = val

        # --- start_time / end_time 기본값 (미입력 시 00:00 / 23:59) ---
        if not start_time_str:
            start_time_str = "00:00"
        if not end_time_str:
            end_time_str = "23:59"

        # --- Y/N 필드 ---
        allday_val = 1 if (_parse_boolean(row.get(col_allday)) if col_allday else False) else 0

        if col_auto:
            raw_auto = _safe_str(row.get(col_auto))
            auto_val = (0 if raw_auto and not _parse_boolean(raw_auto) else 1)
        else:
            auto_val = 1

        weekly_val = 1 if (_parse_boolean(row.get(col_weekly)) if col_weekly else False) else 0
        pref_val = 1 if (_parse_boolean(row.get(col_pref)) if col_pref else False) else 0

        # --- duration ---
        duration_val: Optional[int] = None
        if col_duration:
            raw_dur = row.get(col_duration)
            if raw_dur is not None and not (isinstance(raw_dur, float) and math.isnan(raw_dur)):
                s = str(raw_dur).strip()
                if s:
                    try:
                        duration_val = int(float(s))
                    except (ValueError, TypeError):
                        row_errs.append("근무시간은 숫자여야 합니다.")

        # --- 색상 자동 배정 ---
        color = assign_color(shift_gb, shift_type or "근무", existing_colors, color_counter)
        existing_colors.add(color)

        # --- 결과 수집 ---
        if shift_id and shift_id not in shift_ids_in_file:
            shift_ids_in_file.add(shift_id)

        if not row_errs:
            normalized.append({
                "row": int(ridx) + 2,
                "shift_id": shift_id,
                "name": name,
                "shift_gb": shift_gb,
                "type": shift_type,
                "start_time": start_time_str,
                "end_time": end_time_str,
                "allday": allday_val,
                "auto_schedule": auto_val,
                "duration": duration_val,
                "is_weekly_off": weekly_val,
                "show_in_preference": pref_val,
                "color": color,
            })

        if row_errs:
            errors.append({"row": int(ridx) + 2, "reason": " | ".join(row_errs)})

    return {
        "success": 0 if errors else len(normalized),
        "errors": errors,
        "rows": normalized,
        "summary": {
            "total": len(normalized),
            "error_count": len(errors),
        },
    }


def shift_upload_confirm(
    rows: List[Dict[str, Any]],
    user: UserSchema,
    db: Session,
    target_group_id: str,
) -> Dict[str, Any]:
    """검증 통과된 근무코드를 DB 에 저장."""
    if not target_group_id:
        return {"success": 0, "errors": [{"row": 0, "reason": "group_id가 필요합니다."}]}

    # office_id 결정
    if getattr(user, "is_master_admin", False):
        g = db.query(Group).filter(Group.group_id == target_group_id).first()
        if not g:
            return {"success": 0, "errors": [{"row": 0, "reason": "그룹을 찾을 수 없습니다."}]}
        office_id = g.office_id
    else:
        nurse = db.query(Nurse).filter(Nurse.nurse_id == user.nurse_id).first()
        if nurse and nurse.group:
            office_id = nurse.group.office_id
        else:
            office_id = getattr(user, "office_id", None)

    if not office_id:
        return {"success": 0, "errors": [{"row": 0, "reason": "office_id를 결정할 수 없습니다."}]}

    max_seq = (
        db.query(func.max(Shift.sequence))
        .filter(Shift.group_id == target_group_id)
        .scalar()
        or 0
    )

    saved = 0
    errors: List[Dict[str, Any]] = []
    saved_items: List[Dict[str, Any]] = []

    # ★ 확정(confirm) 단계에는 중복 검사가 **하나도 없었다**. 검사는 별도 요청인
    #   upload-validate 에만 있어서, 검증과 확정 사이에 코드가 추가되거나 사용자가 검증을
    #   건너뛰고 확정만 호출하면 중복이 그대로 들어갔다. 파일 안 중복도 막지 못했다.
    #   여기서 (기존 DB 행) + (같은 파일 앞쪽 행) 양쪽을 본다.
    seen_in_file: Set[str] = set()

    for idx, item in enumerate(rows, 1):
        try:
            shift_id = str(item.get("shift_id", "")).strip()
            if not shift_id:
                errors.append({"row": item.get("row", 0), "reason": "shift_id 누락"})
                continue
            if shift_id in seen_in_file:
                errors.append({"row": item.get("row", 0),
                               "reason": f"파일 안에 '{shift_id}' 가 중복입니다."})
                continue
            if shift_code_taken(db, office_id, target_group_id, shift_id):
                errors.append({"row": item.get("row", 0),
                               "reason": f"이미 존재하는 근무코드입니다: {shift_id}"})
                continue
            seen_in_file.add(shift_id)
            # ★ `shift_gb` 도 한 번만 다듬어 저장·슬롯등록 양쪽에 같은 값을 쓴다.
            #   다듬지 않으면 `" 데이 "` 가 SHIFT_GB_TO_SLOT_CODE 매칭에 실패해 슬롯 등록이
            #   **조용히 건너뛰어진다**(오류도 안 난다).
            shift_gb = (str(item.get("shift_gb")).strip() or None) if item.get("shift_gb") else None

            max_seq += 1

            new_shift = Shift(
                shift_id=shift_id,
                office_id=office_id,
                group_id=target_group_id,
                name=str(item.get("name", "")).strip(),
                color=str(item.get("color", "#CF847A")).strip(),
                shift_gb=shift_gb,
                start_time=_parse_time_str(item.get("start_time")),
                end_time=_parse_time_str(item.get("end_time")),
                type=str(item.get("type", "근무")).strip(),
                allday=item.get("allday", 0),
                auto_schedule=item.get("auto_schedule", 1),
                duration=item.get("duration"),
                sequence=max_seq,
                default_shift=None,
                is_weekly_off=item.get("is_weekly_off", 0),
                show_in_preference=item.get("show_in_preference", False),
            )
            db.add(new_shift)
            saved += 1
            # ★ 원본 item 을 그대로 담으면 안 된다. `shift_id` 는 위에서 strip() 한 값으로
            #   저장하는데 item 에는 다듬기 전 값이 남아 있어서, 아래 shift_manage 등록이
            #   `"  D1  "` 같은 공백 포함 코드를 넣는다. 그러면 슬롯 코드가 실제 근무코드와
            #   달라져 커버리지 계산에서 빠지고, 나중에 그 코드를 지워도 슬롯에는 고아로 남는다.
            #   (confirm 은 rows 가 List[dict] 라 validate 를 거치지 않고도 호출된다.)
            saved_items.append({"shift_id": shift_id, "shift_gb": shift_gb})

        except Exception as e:
            errors.append({"row": item.get("row", 0), "reason": str(e)})

    # ★★ Shift 삽입과 shift_manage 등록을 **한 트랜잭션**으로 확정한다(import 경로와 동일).
    #   예전엔 Shift 를 먼저 커밋하고 그 뒤 코드마다 따로 커밋했다. 등록 도중 실패하면
    #   근무코드만 저장되고 슬롯 등록이 빠지는데, 같은 파일을 다시 올려도 위쪽 중복검사가
    #   "이미 존재하는 근무코드" 로 걸러 버려 **누락된 슬롯 등록이 영영 복구되지 않는다.**
    try:
        # ShiftManage codes 업데이트 (데이/이브닝/나이트 → D/E/N 변환)
        for item in saved_items:
            sg = item.get("shift_gb")
            slot_code = SHIFT_GB_TO_SLOT_CODE.get(sg)
            if slot_code:
                _append_shift_manage_code(
                    db=db,
                    office_id=office_id,
                    group_id=target_group_id,
                    shift_id=item["shift_id"],
                    shift_gb=slot_code,
                    commit=False,
                )
        db.commit()
    except Exception:
        db.rollback()
        raise

    return {"success": saved, "saved": saved, "errors": errors}


# ──────────────────────────────────────────────────────────────
# 타 병동 근무코드 가져오기
# ──────────────────────────────────────────────────────────────

def get_available_shifts_for_import(
    office_id: str,
    target_group_id: str,
    db: Session,
) -> List[Dict[str, Any]]:
    """동일 오피스 내 다른 그룹의 근무코드 중 현재 그룹에 없는 것만 반환."""
    other_group_ids = [
        g.group_id for g in
        db.query(Group.group_id)
        .filter(Group.office_id == office_id, Group.group_id != target_group_id)
        .all()
    ]
    if not other_group_ids:
        return []

    existing_shift_ids: Set[str] = set(
        row[0] for row in
        db.query(Shift.shift_id).filter(Shift.group_id == target_group_id).all()
    )

    candidates = (
        db.query(Shift, Group.group_name)
        .join(Group, Shift.group_id == Group.group_id)
        .filter(
            Shift.group_id.in_(other_group_ids),
            Shift.office_id == office_id,
        )
        # ★ import_shifts_to_group 의 원본 선택 정렬과 반드시 같아야 한다.
        #   화면에 보인 행과 실제로 복사되는 행이 갈리면 설정이 조용히 바뀐다.
        .order_by(Group.group_name, Shift.sequence, Group.group_id, Shift.id)
        .all()
    )

    seen: Set[str] = set()
    result: List[Dict[str, Any]] = []
    for shift, group_name in candidates:
        if shift.shift_id in seen or shift.shift_id in existing_shift_ids:
            continue
        seen.add(shift.shift_id)
        result.append({
            "shift_id": shift.shift_id,
            "name": shift.name,
            "color": shift.color,
            "shift_gb": shift.shift_gb,
            "type": shift.type,
            "start_time": shift.start_time.strftime("%H:%M") if shift.start_time else None,
            "end_time": shift.end_time.strftime("%H:%M") if shift.end_time else None,
            "allday": shift.allday,
            "auto_schedule": shift.auto_schedule,
            "duration": shift.duration,
            "is_weekly_off": shift.is_weekly_off,
            "show_in_preference": shift.show_in_preference,
            "source_group_id": shift.group_id,
            "source_group_name": group_name,
        })
    return result


def import_shifts_to_group(
    shift_ids: List[str],
    target_group_id: str,
    office_id: str,
    db: Session,
    sources: Dict[str, str] | None = None,
) -> Dict[str, Any]:
    """선택된 근무코드를 다른 그룹에서 현재 그룹으로 복사.

    `sources` 는 `{shift_id: source_group_id}` — 사용자가 후보 목록에서 실제로 본 병동이다.
    ★ 배치 하나에 **여러 병동**에서 고른 코드가 섞일 수 있어 코드별 매핑으로 받는다.
      원본을 배치 전체에 하나만 두면, 화면에서 두 병동의 코드를 함께 고른 사용자는
      나머지가 전부 "원본을 찾을 수 없습니다" 로 떨어진다.
    매핑에 없는 코드는 후보 조회와 **같은 정렬**로 같은 행을 고른다.
    ★ 예전엔 정렬 없이 `.first()` 였다. 같은 오피스의 여러 병동에 같은 `shift_id` 가 있으면
      DB 가 고르는 대로라, 화면에 보인 것과 다른 병동의 행이 복사될 수 있었다. 특히
      `default_shift` 가 NULL 인 행(=이 경로로 기본코드를 채운 병동)이 뽑히면 대표코드
      표식이 또 유실돼 같은 장애가 재발한다.
    """
    if not shift_ids:
        return {"imported": 0, "skipped": 0, "errors": []}

    # ★ 유일성 키는 (office_id, group_id, shift_id) 다 — shift_code_taken 참조.
    #   group_id 만으로 잡으면 group_id 가 빈 행에서 남의 오피스 코드를 자기 것으로 읽는다.
    existing_ids: Set[str] = set(
        row[0] for row in
        db.query(Shift.shift_id)
        .filter(Shift.group_id == target_group_id, Shift.office_id == office_id)
        .all()
    )
    max_seq = (
        db.query(func.max(Shift.sequence))
        .filter(Shift.group_id == target_group_id, Shift.office_id == office_id)
        .scalar() or 0
    )
    other_group_ids = [
        g.group_id for g in
        db.query(Group.group_id)
        .filter(Group.office_id == office_id, Group.group_id != target_group_id)
        .all()
    ]
    other_group_set = set(other_group_ids)
    sources = sources or {}

    imported = 0
    skipped = 0
    errors: List[Dict[str, Any]] = []
    saved_items: List[Dict[str, str]] = []

    for sid in shift_ids:
        if sid in existing_ids:
            skipped += 1
            continue

        # 지정된 원본은 같은 오피스의 다른 병동이어야 한다(타 오피스·자기 자신 차단).
        # 코드 하나가 막혀도 나머지는 정상 처리한다 — 배치 전체를 되돌리면 사용자가
        # 어느 항목이 문제인지 알 수 없다.
        requested_src = sources.get(sid)
        if requested_src is not None and requested_src not in other_group_set:
            errors.append({"shift_id": sid, "reason": "원본 병동에 접근할 수 없습니다."})
            continue
        allowed_group_ids = [requested_src] if requested_src else other_group_ids

        source = (
            db.query(Shift)
            .join(Group, Shift.group_id == Group.group_id)
            .filter(
                Shift.shift_id == sid,
                Shift.group_id.in_(allowed_group_ids),
                Shift.office_id == office_id,
            )
            # 후보 조회(get_available_shifts_for_import)와 같은 순서로 골라
            # 화면에 보인 행과 복사되는 행을 일치시킨다.
            # ★ `group_name` 과 `sequence` 는 둘 다 유일하지 않다(같은 그룹 안에서도
            #   sequence 가 겹치는 행이 실재한다). 마지막에 유일 키를 붙여야 두 쿼리가
            #   확실히 같은 행을 고른다 — 양쪽 정렬을 한 글자도 다르지 않게 유지할 것.
            .order_by(Group.group_name, Shift.sequence, Group.group_id, Shift.id)
            .first()
        )
        if not source:
            errors.append({"shift_id": sid, "reason": "원본 근무코드를 찾을 수 없습니다."})
            continue

        max_seq += 1

        new_shift = Shift(
            shift_id=sid,
            office_id=office_id,
            group_id=target_group_id,
            name=source.name,
            color=source.color,
            shift_gb=source.shift_gb,
            start_time=source.start_time,
            end_time=source.end_time,
            type=source.type,
            allday=source.allday,
            auto_schedule=source.auto_schedule,
            duration=source.duration,
            sequence=max_seq,
            # ★ `default_shift` 를 원본 그대로 옮긴다. 예전엔 None 으로 박았다.
            #   이 값은 두 가지를 겸한다 — 기본코드에서는 "이 코드가 대표코드다" 라는 표식
            #   (주휴 식별자이기도 하다), 파생코드에서는 **대표코드 매핑**이다
            #   (실제 데이터에 D1→D · E1→E · N1→N · MD→M · OFF→O 가 있다).
            #   지우면 전자는 주휴 판정과 대표코드 접기가 깨지고, 후자는 가져온 파생코드가
            #   슬롯 미분류로 떨어진다.
            #   ★ 신규 그룹은 `GET /shifts` 가 행 0건일 때만 기본 6종(D/E/N/M/O/주)을 깔아
            #     준다. 다른 코드를 먼저 import 하면 그 분기를 못 타서 기본코드까지 이 경로로
            #     채우게 되고, 그때 대표코드 표식이 통째로 사라졌다.
            default_shift=source.default_shift,
            is_weekly_off=source.is_weekly_off,
            show_in_preference=source.show_in_preference,
        )
        db.add(new_shift)
        existing_ids.add(sid)
        imported += 1
        saved_items.append({"shift_id": sid, "shift_gb": source.shift_gb})

    # ★★ Shift 삽입과 shift_manage 등록을 **한 트랜잭션**으로 확정한다.
    #   예전엔 Shift 를 먼저 커밋하고 그 뒤에 코드마다 따로 커밋했다. 등록 도중 실패하면
    #   Shift 행만 남고 shift_manage 등록이 빠지는데, 재시도해도 `existing_ids` 에 걸려
    #   skipped 로 넘어가 **영영 복구되지 않는다**(가져오기는 성공한 것처럼 보이지만
    #   커버리지·인력 설정에서는 그 코드가 계속 빠져 있다).
    try:
        for item in saved_items:
            slot_code = SHIFT_GB_TO_SLOT_CODE.get(item["shift_gb"] or "")
            if slot_code:
                _append_shift_manage_code(
                    db=db,
                    office_id=office_id,
                    group_id=target_group_id,
                    shift_id=item["shift_id"],
                    shift_gb=slot_code,
                    commit=False,
                )
        db.commit()
    except Exception:
        db.rollback()
        raise

    return {"imported": imported, "skipped": skipped, "errors": errors}
