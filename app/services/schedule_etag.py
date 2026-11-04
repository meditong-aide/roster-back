"""근무표 내용 지문(etag) — 자동 저장(디바운싱)이 늦게 도착한 옛 요청으로 최신 근무표를 덮지 않게 한다.

- 지문 = 칸(간호사·날짜·근무코드·shifts.id)과 메모. 조회(`GET /roster/schedule/{id}`)와 저장(`POST /roster/save`)
  응답에 싣고, 저장 요청이 `base_etag` 로 되돌려 보낸다. 저장 때 지금 지문과 다르면 409 `ROSTER_STALE`.
- 칸 행만으로 만든다 — `schedules.updated_at` 이나 버전 번호는 AIDE 칸 수정(`schedule_tools`)이 건드리지 않아
  그 수정을 놓친다. DDL 이 필요 없다(11-04 결정 D8).
"""
from __future__ import annotations

import hashlib
from collections.abc import Iterable
from datetime import date, datetime
from typing import Any


def _day(value: Any) -> str:
    """`work_date` 는 모델상 DATETIME 이라 datetime·date·문자열로 온다 — 날짜 문자열로 맞춘다."""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]


def content_etag(rows: Iterable[tuple], memo: str | None) -> str:
    """근무표 내용 지문 — `rows` 는 (간호사, 날짜, 근무코드, shifts.id) 칸 행들. 순서와 무관하다.

    ★ 칸 표 `schedule_entries` 에는 유니크 키가 없어 같은 칸이 두 줄일 수 있다 — 접지 않고 그대로 넣는다.
    ★ 메모 None 과 '' 는 같게 본다(화면은 빈 메모를 '' 로 보낸다).
    """
    items = sorted(
        (str(nurse_id or ""), _day(work_date), str(shift_id or ""), "" if int_id is None else str(int(int_id)))
        for nurse_id, work_date, shift_id, int_id in rows
    )
    digest = hashlib.sha256()
    for item in items:
        digest.update("\x1f".join(item).encode("utf-8"))
        digest.update(b"\x1e")
    digest.update(b"memo\x1f")
    digest.update((memo or "").encode("utf-8"))
    return digest.hexdigest()[:32]
