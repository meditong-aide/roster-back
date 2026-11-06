"""공유 원자 커밋 경계 — DAG 플랜과 ReAct 배치 승인이 **같은 트랜잭션 규약**을 쓰게 한다.

문제(docs/AGENT_SHARED_GATE_REFACTOR.md P3/P4): `_commit_plan`(DAG)은 전 mutation 을
한 트랜잭션으로 원자 커밋했지만, `_execute_approval_batch`(ReAct)는 항목을 **순차 커밋**해
중간 실패 시 **부분 반영**이 남았다. AIDE_DAG_PLANNING 이 기본 OFF 라 의존 복합도 ReAct 배치로
처리되므로, 반쯤 적용된 의존 변경이 실제 위험이었다.

규약: 블록 안의 스킬/서비스가 내부에서 부르는 `db.commit()` 을 `db.flush()` 로 리다이렉트해
트랜잭션을 유지 → 블록이 정상 종료하면 **한 번에 commit**, `AbortCommit`/예외면 **rollback**
(부분 반영 없음). commit 단계는 순차·단일세션(원자성·일관성 우선).
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator


class AbortCommit(Exception):
    """원자 커밋 중단(전체 롤백) 신호 — 사용부가 실패를 감지하면 raise 한다."""

    def __init__(self, info: Any = None):
        super().__init__("atomic commit aborted")
        self.info = info


@contextmanager
def atomic_commit(db: Any) -> Iterator[None]:
    """전 mutation 을 한 트랜잭션으로 묶는다.

    - 진입: `db.commit` → `db.flush` 리다이렉트(내부 커밋을 트랜잭션 유지 flush 로).
    - 정상 종료: 리다이렉트 복원 후 **단일 `db.commit()`**.
    - `AbortCommit`/임의 예외: 복원 후 **`db.rollback()`** (부분 반영 없음) 후 재전파.

    사용부는 실패 감지 시 `raise AbortCommit(info)` 로 전체 롤백을 유도한다.
    """
    real_commit = db.commit
    db.commit = db.flush  # type: ignore[method-assign]
    try:
        yield
    except BaseException:
        db.commit = real_commit  # type: ignore[method-assign]
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        raise
    # 정상 종료 → 원자 커밋
    db.commit = real_commit  # type: ignore[method-assign]
    try:
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        raise
