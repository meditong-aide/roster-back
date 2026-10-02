"""근무표 병동·달 단위 직렬화 잠금 — 버전 번호 매기기와 마감·철회.

★ 버전 번호는 "그 달 최대 VER + 1" 이다. 두 요청이 동시에 최대값을 읽으면 같은 VER 가 둘
  생긴다(새 버전 저장·복사·빈 근무표·주휴 근무표·생성 요청 5곳 — 한 곳만 잠그면 나머지와
  겹친다). 그룹·달 단위 앱 잠금(`sp_getapplock`, 트랜잭션 소유)을 잡은 **뒤에** 최대값을 읽는다.
  커밋·롤백 때 저절로 풀린다. 근무표 행은 아직 없고, 원본 행을 잠그면 그 버전 편집까지 막힌다.
★ 마감·철회도 같은 이유로 병동·달 단위로 줄 세운다 — 재마감 변경 확인이 "그 달 가장 최근 스냅샷" 을
  비교 기준으로 읽는데, 같은 달 두 마감이 동시에 같은 기준을 읽으면 활성 마감본·알림이 둘 생긴다.
★ MSSQL 에서만 잡는다(테스트용 SQLite 에는 앱 잠금이 없다).
"""
from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

#: 잠금 대기 상한(ms). 버전 행 하나 만드는 짧은 트랜잭션끼리라 이 안에 풀린다.
_LOCK_TIMEOUT_MS = 10000


def _applock(db: Session, resource: str, timeout_ms: int, busy_detail: str) -> None:
    """트랜잭션 소유 배타 앱 잠금. 0·1 = 잡음 · -1 = 시간 초과 → 409 · 그 밖(-2 취소·-3 교착·
    -999 인자 오류)은 500 + 로그 — 사용자에게 "잠시 후 다시" 라고 하면 원인이 묻힌다."""
    if db.get_bind().dialect.name != "mssql":   # 테스트(SQLite) 등 — 앱 잠금이 없는 DB
        return
    rc = db.execute(
        text(
            "DECLARE @rc int; EXEC @rc = sp_getapplock @Resource = :res, @LockMode = 'Exclusive', "
            "@LockOwner = 'Transaction', @LockTimeout = :timeout; SELECT @rc"
        ),
        {"res": resource, "timeout": int(timeout_ms)},
    ).scalar()
    if rc is not None and int(rc) >= 0:
        return
    if rc is not None and int(rc) == -1:
        raise HTTPException(status_code=409, detail=busy_detail)
    logger.error("[ScheduleLock] 앱 잠금 실패 rc=%s resource=%s", rc, resource)
    raise HTTPException(status_code=500, detail="근무표 잠금을 잡지 못했습니다(잠금 오류).")


def lock_version_numbering(
    db: Session, group_id: str, year: int, month: int, timeout_ms: int = _LOCK_TIMEOUT_MS,
) -> None:
    """같은 병동·같은 달의 새 버전 번호 매기기를 이 트랜잭션 끝까지 직렬화한다.

    Args:
        timeout_ms: 잠금 대기 상한. 화면 요청은 기본(10초), 생성 작업은 길게 준다 — 같은 달 마감이
            끝날 때까지 버전 조회가 막힐 수 있는데, 작업이 409 로 끝나면 실패 알림·재시도로 번진다.
    """
    _applock(db, f"roster_version:{group_id}:{year}:{month}", timeout_ms,
             "다른 저장이 진행 중입니다. 잠시 후 다시 시도해 주세요.")


def lock_month_publish(db: Session, group_id: str, year: int, month: int) -> None:
    """같은 병동·같은 달의 마감·철회를 이 트랜잭션 끝까지 직렬화한다(`publish_roster`·`unpublish_roster`).

    ★ 비교 기준(그 달 가장 최근 스냅샷)을 읽기 **전에** 잡는다 — 잡은 뒤 읽어야 앞 마감이 커밋한
      스냅샷을 기준으로 본다.
    """
    _applock(db, f"roster_publish:{group_id}:{year}:{month}", _LOCK_TIMEOUT_MS,
             "같은 달 근무표 마감(철회)이 진행 중입니다. 잠시 후 다시 시도해 주세요.")
