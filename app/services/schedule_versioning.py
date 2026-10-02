"""근무표 버전 번호 매기기 직렬화 — 같은 병동·같은 달에 새 버전을 만드는 모든 경로가 쓴다.

★ 버전 번호는 "그 달 최대 VER + 1" 이다. 두 요청이 동시에 최대값을 읽으면 같은 VER 가 둘
  생긴다(새 버전 저장·복사·빈 근무표·주휴 근무표·생성 요청 5곳 — 한 곳만 잠그면 나머지와
  겹친다). 그룹·달 단위 앱 잠금(`sp_getapplock`, 트랜잭션 소유)을 잡은 **뒤에** 최대값을 읽는다.
  커밋·롤백 때 저절로 풀린다. 근무표 행은 아직 없고, 원본 행을 잠그면 그 버전 편집까지 막힌다.
"""
from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

#: 잠금 대기 상한(ms). 버전 행 하나 만드는 짧은 트랜잭션끼리라 이 안에 풀린다.
_LOCK_TIMEOUT_MS = 10000


def lock_version_numbering(
    db: Session, group_id: str, year: int, month: int, timeout_ms: int = _LOCK_TIMEOUT_MS,
) -> None:
    """같은 병동·같은 달의 새 버전 번호 매기기를 이 트랜잭션 끝까지 직렬화한다.

    반환값 0·1 = 잡음 · -1 = 시간 초과(다른 저장이 진행 중 → 409) · 그 밖(-2 취소·-3 교착·
    -999 인자 오류)은 서버 오류로 남긴다 — 사용자에게 "잠시 후 다시" 라고 하면 원인이 묻힌다.

    Args:
        timeout_ms: 잠금 대기 상한. 화면 요청은 기본(10초), 생성 작업은 길게 준다 — 같은 달 마감이
            끝날 때까지 버전 조회가 막힐 수 있는데, 작업이 409 로 끝나면 실패 알림·재시도로 번진다.
    """
    rc = db.execute(
        text(
            "DECLARE @rc int; EXEC @rc = sp_getapplock @Resource = :res, @LockMode = 'Exclusive', "
            "@LockOwner = 'Transaction', @LockTimeout = :timeout; SELECT @rc"
        ),
        {"res": f"roster_version:{group_id}:{year}:{month}", "timeout": int(timeout_ms)},
    ).scalar()
    if rc is not None and int(rc) >= 0:
        return
    if rc is not None and int(rc) == -1:
        raise HTTPException(status_code=409, detail="다른 저장이 진행 중입니다. 잠시 후 다시 시도해 주세요.")
    logger.error("[ScheduleVersioning] 버전 잠금 실패 rc=%s group=%s %s-%s", rc, group_id, year, month)
    raise HTTPException(status_code=500, detail="버전 번호를 정하지 못했습니다(잠금 오류).")
