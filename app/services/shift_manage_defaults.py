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
    except IntegrityError as exc:
        # 다른 요청이 먼저 동일 슬롯 생성(UNIQUE 충돌) → 롤백 후 재조회로 수렴.
        # ★★ **반드시 남긴다.** 종전엔 로그 한 줄 없이 삼키고 `True` 를 돌려줬다.
        #   UNIQUE 경쟁이면 정상이지만 FK·NOT NULL·스키마 불일치·CHECK 도 같은
        #   `IntegrityError` 라, 슬롯이 안 생긴 채 호출측이 "성공" 으로 읽고 넘어간다.
        #   그러면 `GET /shifts` 는 "근무코드 설정을 준비하지 못했습니다" 로 500 을 내는데
        #   **무엇이 위반이었는지 어디에도 안 남아** 원인 추적이 불가능하다
        #   (2026-09-19 동탄시티병원 8병동 실사례 — UNIQUE·FK·NOT NULL·스키마를 전부
        #    정상 확인하고도 실패 원인을 못 좁혔다).
        db.rollback()
        print(
            f"[ensure_default_shift_manage][WARN] 기본 슬롯 seeding 실패(IntegrityError) — "
            f"office={office_id} group={group_id} class={nurse_class} :: {exc}"
        )
    except Exception as exc:  # noqa: BLE001
        # ★ IntegrityError 가 아닌 실패(연결·타입·드라이버)는 종전에 **전파**돼
        #   라우터 catch-all 이 원문을 응답에 실어 보냈다. 로그로 남기고 같은 계약을
        #   유지하되(재전파), 최소한 서버 로그에서 원인을 볼 수 있게 한다.
        db.rollback()
        print(
            f"[ensure_default_shift_manage][ERROR] 기본 슬롯 seeding 예외 — "
            f"office={office_id} group={group_id} class={nurse_class} :: "
            f"{type(exc).__name__}: {exc}"
        )
        raise
    return True
