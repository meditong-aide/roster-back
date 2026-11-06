# 도움말 문서 코퍼스 (help RAG)

이 폴더의 `.pdf`가 서버 기동 시 자동 인제스트되어 lookup_guide 검색 대상이 됩니다.

## 로드 우선순위 (retriever._load_pdf_chunks)
1. `<pdf명>.chunks.json` 이 있으면 → **그 전처리 캐시를 그대로 로드** (재파싱 안 함)
2. 없으면 → 텍스트 인제스트(pdfplumber, `pdf_ingest.ingest_pdf`) 폴백

## 전처리(업로드 시 1회, 오프라인)
- **텍스트 PDF**: `pdf_ingest.ingest_pdf` — 제목 접두어로 멀티페이지를 기능 단위로 병합.
- **스크린샷 중심 PDF**: `pdf_ingest_vlm.ingest_pdf_vlm` — 페이지 이미지를 VLM(gpt-4o)이 읽어
  '어느 버튼을 어디서'까지 텍스트로 정리 + 원본 텍스트 하이브리드. 무거우니 1회만 돌려
  결과를 `<pdf명>.chunks.json` 으로 저장 → 이후 기동은 캐시 재사용.

예) 오프라인 전처리:
    python -c "from agents_v2.guide.pdf_ingest_vlm import ingest_pdf_vlm; import json; \
      json.dump(ingest_pdf_vlm('docs/manual.pdf'), open('docs/manual.chunks.json','w'), ensure_ascii=False)"

## git
`*.pdf`, `*.chunks.json`, `*.xlsx` 는 gitignore(콘텐츠=배포/로컬 제공, 메커니즘만 커밋).
