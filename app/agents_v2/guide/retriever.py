"""도움말 검색기 — 순수 파이썬 BM25 + 별칭 정규화 (의존성 0).

소규모 코퍼스(수십 청크)라 벡터DB/임베딩 불필요. 한국어 사내 용어는 정확 매칭이 중요해
BM25(어휘) + alias 정규화 + 한글 bigram 으로 처리. 패러프레이즈가 문제되면 이후 임베딩 하이브리드.
"""

from __future__ import annotations

import math
import re
from collections import Counter

from agents_v2.guide.help_corpus import HELP_CHUNKS

# 별칭 → 정규 토큰 (ABBREVIATION_DICT 축약 + 도움말 도메인어). 검색 정규화용.
_ALIASES = {
    "나이트": "야간", "밤번": "야간", "n": "야간", "나탄": "야간",
    "데이": "주간", "낮번": "주간", "디": "주간",
    "이브닝": "저녁", "초번": "저녁",
    "오프": "휴무", "비번": "휴무", "쉬는날": "휴무",
    "듀티": "근무표", "스케줄": "근무표",
    "쌤": "간호사", "선생님": "간호사",
    "퇴사자": "퇴사", "퇴직": "퇴사", "그만": "퇴사",
    "명단": "근무자", "직원": "근무자", "인력": "근무자",
    "빼기": "삭제", "빼줘": "삭제", "제외": "삭제", "없애": "삭제", "지워": "삭제",
    "만들": "생성", "짜": "생성",
}

_TOKEN_RE = re.compile(r"[가-힣]+|[a-zA-Z0-9]+")


def _normalize(text: str) -> str:
    t = (text or "").lower()
    for a, b in _ALIASES.items():
        if a in t:
            t = t + " " + b  # 원형 유지 + 정규형 추가(둘 다 매칭)
    return t


def _tokenize(text: str) -> list[str]:
    toks = _TOKEN_RE.findall(_normalize(text))
    # 한글 토큰은 bigram 도 추가(부분 일치 강화: '퇴사처리'→'퇴사','처리')
    out: list[str] = []
    for w in toks:
        out.append(w)
        if len(w) >= 3 and re.match(r"[가-힣]+", w):
            out.extend(w[i:i + 2] for i in range(len(w) - 1))
    return out


class _BM25:
    def __init__(self, docs: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.docs, self.k1, self.b = docs, k1, b
        self.N = len(docs)
        self.avgdl = sum(len(d) for d in docs) / max(1, self.N)
        df: Counter = Counter()
        for d in docs:
            df.update(set(d))
        self.idf = {t: math.log(1 + (self.N - n + 0.5) / (n + 0.5)) for t, n in df.items()}

    def score(self, q: list[str], i: int) -> float:
        d = self.docs[i]
        freq = Counter(d)
        dl = len(d)
        s = 0.0
        for t in q:
            if t not in freq:
                continue
            f = freq[t]
            s += self.idf.get(t, 0.0) * f * (self.k1 + 1) / (
                f + self.k1 * (1 - self.b + self.b * dl / self.avgdl)
            )
        return s


class Retriever:
    """임의 청크(help 하드코딩 / PDF 인제스트 등) 위에 BM25 검색. 재사용 가능."""

    def __init__(self, chunks: list[dict]):
        self.chunks = chunks
        self._bm = _BM25([
            _tokenize(f"{c['title']} {c.get('keywords', '')} {c['body']}") for c in chunks
        ])

    def retrieve(self, query: str, k: int = 2, min_score: float = 1.0) -> list[dict]:
        q = _tokenize(query)
        scored = sorted(
            ((self._bm.score(q, i), i) for i in range(len(self.chunks))),
            key=lambda x: x[0], reverse=True,
        )
        if not scored or scored[0][0] < min_score:
            return []
        out = []
        for sc, i in scored[:k]:
            if sc <= 0:
                break
            c = self.chunks[i]
            out.append({"id": c.get("id"), "title": c["title"], "body": c["body"], "score": round(sc, 3)})
        return out


def _load_pdf_chunks() -> list[dict]:
    """guide/docs/*.pdf 를 기능-단위 청크로 자동 인제스트. 없거나 실패해도 무해([])."""
    import logging
    import os

    docs_dir = os.path.join(os.path.dirname(__file__), "docs")
    if not os.path.isdir(docs_dir):
        return []
    out: list[dict] = []
    try:
        from agents_v2.guide.pdf_ingest import ingest_pdf
    except Exception:
        return []
    for fn in sorted(os.listdir(docs_dir)):
        if not fn.lower().endswith(".pdf"):
            continue
        try:
            out.extend(ingest_pdf(os.path.join(docs_dir, fn)))
        except Exception as e:  # noqa: BLE001
            logging.getLogger(__name__).warning("[guide] PDF 인제스트 실패 %s: %s", fn, e)
    return out


# 기본 인스턴스 = 하드코딩 help corpus + guide/docs 의 PDF 자동 인제스트.
_DEFAULT = Retriever(HELP_CHUNKS + _load_pdf_chunks())


def retrieve(query: str, k: int = 2, min_score: float = 1.0) -> list[dict]:
    """질의 → 관련 help 청크 top-k. 최고점 < min_score 면 빈 리스트(=정보 없음)."""
    return _DEFAULT.retrieve(query, k=k, min_score=min_score)
