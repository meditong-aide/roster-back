from __future__ import annotations

from typing import List, Dict

from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from db.models import ShiftManage


# shift_manage 기본 슬롯(단일 소스). slot 1=D, 2=E, 3=N, 5=M (4=OFF 는 응답 합성으로만 노출).
# 이 값이 shifts 라우터의 lazy 생성과 daily_shift 초기화 fallback 양쪽의 유일 기준이다.
DEFAULT_SHIFT_MANAGE_SLOTS: List[Dict] = [
    {"shift_slot": 1, "main_code": "D", "codes": [], "manpower": 3},
    {"shift_slot": 2, "main_code": "E", "codes": [], "manpower": 3},
    {"shift_slot": 3, "main_code": "N", "codes": [], "manpower": 2},
    {"shift_slot": 5, "main_code": "M", "codes": [], "manpower": 0},
]


def ensure_default_shift_manage(
    db: Session, office_id: str, group_id: str, nurse_class: str
) -> bool:
    """(office, group, nurse_class) 에 슬롯이 하나도 없으면 기본 슬롯을 seeding 한다.

    - 이미 하나라도 있으면 no-op(부분 등록 상태는 건드리지 않음 → 호출측이 판단).
    - 동시 첫 로드 race 는 UNIQUE 충돌(IntegrityError) → rollback 후 수렴(재조회는 호출측 책임).
    - 반환: 실제로 seeding 을 시도했으면 True, 기존 행이 있어 건너뛰면 False.
    """
    existing = (
        db.query(ShiftManage)
        .filter(
            ShiftManage.office_id == office_id,
            ShiftManage.group_id == group_id,
            ShiftManage.nurse_class == nurse_class,
        )
        .first()
    )
    if existing is not None:
        return False

    for slot in DEFAULT_SHIFT_MANAGE_SLOTS:
        db.add(
            ShiftManage(
                office_id=office_id,
                group_id=group_id,
                nurse_class=nurse_class,
                shift_slot=slot["shift_slot"],
                main_code=slot["main_code"],
                codes=slot["codes"],
                manpower=slot["manpower"],
            )
        )
    try:
        db.commit()
    except IntegrityError:
        # 다른 요청이 먼저 동일 슬롯 생성(UNIQUE 충돌) → 롤백 후 재조회로 수렴.
        db.rollback()
    return True
