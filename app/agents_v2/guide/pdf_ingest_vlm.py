"""PDF(스크린샷 중심) → VLM 페이지 설명으로 보강한 기능-단위 청크.

캡쳐/스크린샷 정보 처리의 정석: 업로드 시점 1회, 페이지 이미지를 VLM 이 읽어 '어느 버튼을
어떤 순서로' grounded 텍스트로 정리 → pdfplumber 원본 텍스트와 하이브리드로 합쳐 청크화.
질의 때는 VLM/이미지 재호출 없이 그 텍스트만 검색(전처리 결과 재사용).

의존성: pypdfium2(렌더, poppler 불필요) + OpenAI 비전 모델. 무거우니 인제스트 전용(오프라인).
"""

from __future__ import annotations

import base64
import io

from agents_v2.guide.pdf_ingest import extract_pages, group_pages

_VLM_PROMPT = (
    "이 근무 스케줄링 앱 매뉴얼 페이지 스크린샷을 보고, 어느 화면에서 무슨 버튼을 어떤 순서로 "
    "누르는지 절차를 한국어로 간결히 정리해. 번호 콜아웃(①②③)과 화면 UI(버튼 이름·위치)를 "
    "대응시켜라. 화면에 실제로 보이지 않는 것은 지어내지 마라. '- ' 목록으로."
)


def _render_b64(page, scale: float = 2.0) -> str:
    img = page.render(scale=scale).to_pil()
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def describe_page(b64: str, client, model: str = "gpt-4o") -> str:
    r = client.chat.completions.create(
        model=model, max_tokens=500,
        messages=[{"role": "user", "content": [
            {"type": "text", "text": _VLM_PROMPT},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}},
        ]}],
    )
    return (r.choices[0].message.content or "").strip()


def ingest_pdf_vlm(pdf_path: str, client=None, model: str = "gpt-4o",
                   scale: float = 2.0, min_steps: int = 1) -> list[dict]:
    """PDF → VLM 보강 기능 청크. pdfplumber(제목/텍스트) + VLM(화면 설명) 하이브리드.

    client 미지정 시 OpenAI 기본. 페이지 수만큼 VLM 호출(느림·비용) → 인제스트 1회용.
    """
    import pypdfium2 as pdfium

    if client is None:
        import os
        from openai import OpenAI
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    by_page = {p["page"]: p for p in extract_pages(pdf_path)}
    doc = pdfium.PdfDocument(pdf_path)

    enriched: list[dict] = []
    for i in range(len(doc)):
        pageno = i + 1
        base = by_page.get(pageno) or {
            "page": pageno, "title": f"페이지 {pageno}",
            "feature": f"페이지 {pageno}", "steps": [], "n_images": 0,
        }
        vlm = describe_page(_render_b64(doc[i], scale), client, model)
        base = dict(base)
        # 하이브리드: 원본 텍스트 라벨 + VLM 화면 설명
        base["steps"] = list(base.get("steps", [])) + ["[화면 설명(VLM)]", vlm]
        enriched.append(base)

    return group_pages(enriched, min_steps=min_steps)
