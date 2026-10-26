"""SQL `IN (...)` 목록을 수백 개 단위로 끊는다(EI-13c).

MSSQL 은 문장당 파라미터가 약 2100개로 막혀 있고, 무거운 목록 하나가 공용 서버의 메모리 그랜트를 오래 문다(CLAUDE.md
'운영 DB 조회' 절 — roster-back 은 쿼리 타임아웃이 없다). 병동 하나는 보통 300명 아래라 **한 덩어리 = 지금과 같은 문장**이다.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator

#: 한 문장에 싣는 최대 id 수. 병동(≤300명)은 한 덩어리라 문장·파라미터가 지금과 같다.
IN_CHUNK_SIZE = 300


def in_chunks(items: Iterable, size: int = IN_CHUNK_SIZE) -> Iterator[list]:
    """`items` 를 순서를 지키며 `size` 개씩 끊는다. 빈 입력이면 아무것도 내지 않는다(쿼리 0)."""
    chunk: list = []
    for item in items:
        chunk.append(item)
        if len(chunk) >= size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk
