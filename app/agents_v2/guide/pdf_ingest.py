"""PDF 매뉴얼 → 기능-단위 청크. 멀티페이지 기능 매뉴얼(스크린샷 중심) 대응.

핵심: 페이지 단위로 자르면 한 기능이 조각나므로, 제목 접두어("병동 재분배 기능 - 단일1/단일2")
로 **연속 페이지를 하나의 기능 청크로 병합**해 절차 순서를 보존한다. 텍스트가 얇으면
(스크린샷에 정보가 있으면) VLM 페이지 설명으로 보강(enrich_with_vlm, 선택).
"""

from __future__ import annotations

import re

# 제목의 서브라벨 구분자: "기능명 - 단일 병동1"
_SUBLABEL_SEP = re.compile(r"\s+-\s+")


def _feature_of(title: str) -> str:
    """제목에서 기능명(접두어) 추출. '병동 재 분배 기능 - 단일 병동1' → '병동 재 분배 기능'."""
    return _SUBLABEL_SEP.split(title.strip(), 1)[0].strip()


def _slug(s: str) -> str:
    return re.sub(r"[^0-9a-z가-힣]+", "_", s.lower()).strip("_")[:40]


def extract_pages(pdf_path: str) -> list[dict]:
    """페이지별 {page, title, feature, steps}. 텍스트 추출(pdfplumber)."""
    import pdfplumber

    out: list[dict] = []
    with pdfplumber.open(pdf_path) as doc:
        for i, p in enumerate(doc.pages):
            txt = (p.extract_text() or "").strip()
            if not txt:
                continue
            lines = [ln.strip() for ln in txt.splitlines() if ln.strip()]
            title = lines[0]
            # 본문: 제목 제외 + 홑숫자(스크린샷 콜아웃 번호) 제거
            steps = [ln for ln in lines[1:] if not ln.isdigit()]
            out.append({
                "page": i + 1, "title": title,
                "feature": _feature_of(title), "steps": steps,
                "n_images": len(p.images),
            })
    return out


def group_pages(pages: list[dict], min_steps: int = 2) -> list[dict]:
    """페이지 리스트 → 기능-단위 청크(연속 동일 feature 병합). help corpus 포맷.

    반환: [{id, title, keywords, body, pages, n_images}]. 얇은 페이지(min_steps 미만) 제외.
    """
    groups: list[dict] = []
    for pg in pages:
        if groups and groups[-1]["feature"] == pg["feature"]:
            g = groups[-1]
            g["pages"].append(pg["page"])
            g["steps"].extend(pg["steps"])
            g["subtitles"].append(pg["title"])
            g["n_images"] += pg["n_images"]
        else:
            groups.append({
                "feature": pg["feature"], "pages": [pg["page"]],
                "steps": list(pg["steps"]), "subtitles": [pg["title"]],
                "n_images": pg["n_images"],
            })

    chunks: list[dict] = []
    for g in groups:
        if len(g["steps"]) < min_steps:  # 표지/얇은 페이지 스킵
            continue
        pr = g["pages"]
        chunks.append({
            "id": "pdf_" + _slug(g["feature"]),
            "title": f"{g['feature']} (p{pr[0]}-{pr[-1]})" if len(pr) > 1 else g["feature"],
            # 서브제목까지 키워드로 넣어 검색 강화
            "keywords": g["feature"] + " " + " ".join(g["subtitles"]),
            "body": "\n".join(g["steps"]),
            "pages": pr,
            "n_images": g["n_images"],
        })
    return chunks


def ingest_pdf(pdf_path: str, min_steps: int = 2) -> list[dict]:
    """PDF → 기능-단위 청크. 표지/얇은 페이지(min_steps 미만) 제외."""
    return group_pages(extract_pages(pdf_path), min_steps=min_steps)
