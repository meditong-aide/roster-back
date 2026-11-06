# PDF/Excel 문서를 RAG로 수집(ingestion)하기 — 2024~2026 SoTA 검증 리서치

> 대상: 간호사 스케줄링 온보딩 에이전트의 도움말/가이드 지식베이스
> 현행: 손으로 쓴 markdown 청크 위 소규모 BM25 텍스트 리트리버
> 조사일: 2026-07-13 · 모든 arXiv ID / repo / 공식 문서 fetch 검증 완료

---

## 1. 요약 (핵심 합의)

2024~2026 현장 합의는 명확하다: **"raw PDF를 그대로 던져 넣으면 알아서 찾아준다"는 성립하지 않는다.** `PyPDF`/`PyMuPDF`/`pdfplumber`로 뽑은 평문 텍스트는 표·다단·읽기순서가 깨져 "데이터 레이어에서부터 망가진" 파이프라인이 되고, 아무리 좋은 LLM을 얹어도 정확한 검색이 안 된다는 것이 반복적으로 지적된다. 따라서 주류는 **레이아웃 인식 파서로 PDF를 구조화된 markdown/JSON(표 보존)으로 전처리한 뒤 청킹·임베딩**하는 파이프라인이다(Docling, Marker, LlamaParse 등). 다만 2024년 하반기부터 **"파싱/OCR을 아예 건너뛰고 페이지 이미지를 VLM으로 직접 임베딩·검색"하는 no-parse(visual RAG) 흐름**이 급부상했다(ColPali, DSE, ColQwen2, M3DocRAG). 이 계열은 ViDoRe 벤치마크에서 텍스트-파싱 파이프라인을 능가하지만 **GPU 인프라와 멀티벡터 인덱스 비용**을 요구한다. 표/엑셀은 또 다른 축이다 — 계산·집계·정확 조회가 필요한 정형 데이터에 의미(semantic) RAG를 쓰는 것은 종종 **오답**이며, Text-to-SQL / 구조적 조회(TableRAG, SpreadsheetLLM)가 정석이다. **결론: 소규모·한국어·GPU 없음 환경에서는 (a) "raw 그대로"가 아니라 (b) "레이아웃 파서 → markdown 전처리"가 맞고, no-parse VLM은 지금 도입 대상이 아니라 로드맵 후보다.**

---

## 2. 우리 상황

- **언어**: 한국어 가이드 문서 (한글 음절 블록 → CJK 전용 OCR 필요, 라틴 기반 OCR은 띄어쓰기/구조 오인)
- **입력 형식**: PDF(설명 산문 + 표) + Excel(정형 표/설정값)
- **규모**: 소규모 가이드 코퍼스 (수십~수백 페이지 수준, 대규모 아님)
- **현행 스택**: 손으로 쓴 markdown 청크 위 BM25 (lexical) 리트리버 — 임베딩/벡터 인덱스 없음
- **인프라 제약**: **GPU 없음** → 로컬 VLM(ColPali/ColQwen2/DSE) 자체 호스팅 비현실적, CPU 파서 또는 관리형 API가 현실 후보
- **품질 원칙**(CLAUDE.md): LLM-first, top-down, hardcoding/regex 금지 — ingestion도 "규칙 파싱"이 아니라 "구조 파서 + LLM 청킹"이 결이 맞음

---

## 3. 4개 영역별 검증된 도구/논문

### 영역 1. 레이아웃 인식 PDF 파서 (PDF → 구조화 markdown/JSON, 표 보존)

- **Docling** (IBM, arXiv:2408.09869 *Docling Technical Report*; 툴킷 arXiv:2501.17887; repo `github.com/docling-project/docling`) — MIT 오픈소스. **DocLayNet**(레이아웃) + **TableFormer**(표 구조) 두 SoTA 모델로 PDF/DOCX/PPTX/XLSX/HTML을 읽기순서·제목·표까지 살려 Markdown/JSON/HTML로 변환. 표준 하드웨어(CPU)에서 동작. 중립 벤치마크(procycons 2025)에서 **표 셀 정확도 97.9%, 핵심 텍스트 100%**로 최상위, 선형 속도(1p 6.3s ~ 50p 65s). — **성숙도: 프로덕션 준비 완료(OSS, CPU 가능).** → **우리 적용: 1순위 후보.** GPU 없이 한글 PDF를 표 보존 markdown으로 변환해 기존 BM25/청크에 그대로 물릴 수 있음.
- **Marker** (VikParuchuri, repo `github.com/datalab-to/marker`) — 오픈소스. 자체 **surya** OCR(레이아웃/OCR) 기반, 오픈소스 SoTA급 표/수식 처리. 배치 시 H100에서 25 pages/s로 매우 빠르나 **GPU에서 진가**. — **성숙도: 프로덕션(단, GPU 지향).** → **우리 적용: GPU 없으면 Docling 대비 이점 작음.** 나중에 대량/이미지 스캔 PDF가 늘면 재검토.
- **LlamaParse** (LlamaIndex, `llamaindex.ai/llamaparse`) — 관리형 SaaS API(~$0.003/page, 문서당 ~6초). 단순 표·레이아웃은 좋지만 **복잡 표에서 열 오배치**, 원문에 없는 내용 추가 경향(procycons). — **성숙도: 프로덕션(상용 API).** → **우리 적용: 셀프호스팅 회피하고 싶을 때 편리하나, 한국어 복잡 표 정확도는 별도 검증 필요.**
- **Unstructured.io** (`unstructured.io`) — OSS+SaaS. OCR은 강하나 복잡 표 정확도 편차(단순 100% / 복잡 75%), 속도 느림(1p 51s). 최근 품질 하락 지적도 있음. — **성숙도: 프로덕션이나 표 일관성 주의.** → **우리 적용: 우선순위 낮음.**
- **PyMuPDF4LLM** (`pymupdf.readthedocs.io/en/latest/pymupdf4llm`) — 경량 로컬 라이브러리, PDF를 Markdown/JSON/TXT로 빠르게 추출. **클린 디지털 PDF엔 좋지만 복잡 레이아웃/표 정확도는 전용 파서에 못 미침**(AGPL 라이선스 유의). — **성숙도: 프로덕션(경량).** → **우리 적용: 표가 없는 순수 산문 가이드의 빠른 기준선(baseline)으로만.**
- **(중립 벤치마크) OmniDocBench** (CVPR 2025, repo `github.com/opendatalab/OmniDocBench`) — 1,651 PDF 페이지, 10개 문서 유형·5개 언어(CJK 포함) 커버하는 문서 파싱 평가 벤치마크. 2025-01에 Docling 등 추가. — **성숙도: 연구 벤치마크(중립).** → **우리 적용: 파서 선택 시 벤더 자체 벤치(ParseBench=LlamaIndex, SCORE-Bench=Unstructured) 대신 이 중립 벤치의 한국어/표 항목을 참고.**

> 표 요약: **복잡 표·다단·제목 계층을 잘 살리는 순서 ≈ Docling ≈ Marker(GPU) > LlamaParse(단순) > Unstructured(편차) > PyMuPDF4LLM(경량).** 독립 중립 벤치는 아직 부족하고 벤더 벤치가 많다는 점 유의.

### 영역 2. 비주얼/스크린샷 문서 RAG (텍스트 파싱 생략, 페이지 이미지 임베딩)

- **ColPali** (arXiv:2407.01449, ICLR 2025 · `huggingface.co/vidore`) — PaliGemma-3B VLM으로 **페이지 이미지를 ColBERT식 멀티벡터(late-interaction)로 직접 인덱싱**. OCR/파싱 불필요. **ViDoRe에서 모든 텍스트 검색 시스템을 큰 폭으로 능가**하며 end-to-end 학습 가능, 지연도 양호. — **성숙도: 연구→실전화 진행(가중치 공개, 그러나 GPU·멀티벡터 인덱스 필요).** → **우리 적용: 지금은 부적합(GPU 없음).** 스캔/이미지형 한글 가이드가 많아지면 로드맵 1순위.
- **ColQwen2** (`huggingface.co/vidore/colqwen2-v0.1`) — ColPali 아키텍처 + **Qwen2-VL-2B 백본**. 동일 데이터로 ColPali-v1.1 대비 **+5.1 nDCG@5**, ViDoRe 리더보드 최상위. — **성숙도: 연구/OSS 가중치.** → **우리 적용: ColPali 채택 시의 실질 기본 모델.** 현시점 GPU 제약으로 보류.
- **DSE (Document Screenshot Embedding)** (arXiv:2406.11251, EMNLP 2024) — 문서 스크린샷을 **단일 벡터**로 임베딩(파싱·추출 전처리 전무). Phi-3-vision 4B 기반. Wiki-SS에서 **BM25 대비 top-1 +17점**, 슬라이드(혼합 모달) 검색에서 **OCR 텍스트 검색 대비 nDCG@10 +15점 이상**. — **성숙도: 연구.** → **우리 적용: "파싱하면 정보 손실 난다"의 핵심 근거.** 단일벡터라 ColPali보다 인덱스는 가벼우나 여전히 VLM 인코딩에 GPU 유리. 근거 자료로 인용, 도입은 보류.
- **M3DocRAG** (arXiv:2411.04952, UNC·Bloomberg, 주저자 Jaemin Cho) — **ColPali로 페이지 이미지 검색 + Qwen2-VL로 답변**하는 멀티페이지·멀티문서 RAG. 모든 문서를 픽셀로 표현해 OCR이 놓치는 도표까지 활용. MP-DocVQA SoTA, 오픈도메인 벤치 M3DocVQA 공개. — **성숙도: 연구.** → **우리 적용: no-parse RAG의 "완성형" 참조 아키텍처.** 소규모+GPU 없음엔 과함.
- **ViDoRe Benchmark** (v1: ColPali 논문 내 / v2: arXiv:2505.17166 *Raising the Bar for Visual Retrieval*) — 페이지 단위 비주얼 문서 검색 벤치, **다국어**·다도메인. v1은 top 모델이 nDCG@5 90%+로 포화되어 v2에서 장문·교차문서·블라인드 쿼리로 난도 상향. — **성숙도: 연구 벤치마크.** → **우리 적용: 비주얼 RAG 도입 시 한국어 포함 다국어 성능 판단 기준.** v2가 다국어 데이터셋 포함이라 한국어 참고 가치.

> 트렌드 요약: **파싱→markdown 대신 "페이지를 그림으로 보고 검색"하는 no-parse VLM이 텍스트 파이프라인을 성능상 넘어서는 중.** 다만 대가는 GPU·멀티벡터 저장·인덱스 비용. "소규모 + GPU 없음"에는 아직 오버스펙.

### 영역 3. 표/엑셀을 위한 RAG (언제 semantic RAG가 틀리는가)

- **핵심 원칙**: 정확한 값 조회·필터·집계·계산이 필요한 **정형 표에 의미검색(semantic RAG)을 쓰면 틀린다.** 이때는 표를 (a) 구조 보존 후 **Text-to-SQL/구조적 조회**하거나 (b) 스키마+셀 단위 검색으로 다뤄야 함. 산문(prose) 가이드는 semantic RAG, 표/설정값은 구조적 조회 — **분기**가 정답.
- **TableRAG** (arXiv:2410.04739, Google, NeurIPS 2024) — 표 전체를 프롬프트에 넣는 대신 **스키마 검색 + 셀 검색**으로 관련 열/셀만 추출(백만 토큰 표 대응). 프롬프트 길이·정보손실 대폭 감소, 대규모 표 이해 SoTA. — **성숙도: 연구(코드 공개).** → **우리 적용: Excel 설정표가 커질 때의 조회 패턴 청사진.** 지금 규모(작은 설정표)엔 "표→행별 자연어 문장화" 정도면 충분.
- **SpreadsheetLLM** (arXiv:2407.09025, Microsoft) — 스프레드시트를 LLM에 넣기 위한 인코딩 프레임워크 **SheetCompressor**(structural-anchor 압축 + inverse-index 변환 + data-format 집계). 표 탐지 GPT-4 파인튜닝 **F1 ~76%**. 2D 그리드·병합셀·서식 문제를 정면으로 다룸. — **성숙도: 연구.** → **우리 적용: Excel을 "그대로 평문 덤프"하면 안 되는 이유의 근거.** 우리는 SheetCompressor까지 필요 없고, `openpyxl`로 시트→구조 파악 후 셀 주소·헤더를 살린 markdown 표/문장으로 변환하는 원칙만 차용.
- **Table Transformer (TATR) + PubTables-1M** (arXiv:2110.00061, CVPR 2022, repo `github.com/microsoft/table-transformer`) — DETR 기반 **표 탐지 + 표 구조 인식** 모델, 947k 주석 표 데이터셋·GriTS 평가지표. Docling의 TableFormer 등 표 파서 계보의 뿌리. — **성숙도: 프로덕션/OSS(성숙).** → **우리 적용: 직접 쓰기보다 Docling 내부 표 인식의 근간으로 이해.** PDF 속 표는 이 계열 파서가 처리, Excel은 애초에 셀 구조가 있으니 파서 불필요.

> 표/엑셀 요약: **Excel = 이미 구조가 있으므로 파서(OCR) 불필요 → 시트별로 헤더/셀을 살려 markdown 또는 (질의가 계산형이면) SQL/DataFrame 조회.** PDF 속 표 = 레이아웃 파서(Docling)로 표 구조 복원. **정형 값 질의는 semantic RAG 금지, 구조적 조회로.**

### 영역 4. 핵심 합의 + 한국어

- **전처리 합의** (firecrawl/fastio/procycons 2025~2026 벤치·가이드 종합) — "대부분의 RAG는 **LlamaParse 또는 Docling으로 시작**하라. 셀프호스팅이면 **Docling·Marker가 최선의 오픈소스**." "PyPDF/PyMuPDF/pdfplumber에 의존하면 파이프라인은 **데이터 레이어에서 이미 고장**." 실전에서 성패를 가르는 두 축은 **OCR 지원**과 **표 처리**. — **성숙도: 업계 합의(다수 벤치/실무 글).** → **우리 적용: "raw 그대로"는 탈락, "레이아웃 파서→markdown 전처리"가 정석임을 확정.**
- **Upstage Document Parse** (`upstage.ai/products/document-parse`) — **한국 기업**의 상용 문서 파싱 API. 복잡 문서를 HTML/Markdown으로 변환, **한국어·복잡 레이아웃 OCR 95%+**, 표 구조 인식, RAG-ready 출력. 저해상도 한글에도 강함. — **성숙도: 프로덕션(상용 API).** → **우리 적용: 한국어 표·스캔 PDF에서 Docling이 부족하면 가장 현실적인 한국어 특화 대안.** GPU 불필요(API).

---

## 4. 권고 파이프라인 (우리 케이스 최소 구성)

전제: 한국어 · 소규모 · 기존 BM25 · **GPU 없음.** no-parse VLM은 **지금 도입하지 않음**(GPU 부재 + 코퍼스 소규모라 ROI 낮음). "레이아웃 파서 → markdown 전처리" 노선을 최소로 구성.

### 공통 인입 흐름
```
가이드 파일 도착
   │
   ├─ 확장자/유형 분기 ──────────────┐
   │                                 │
  PDF                              Excel(.xlsx)
   │                                 │
[Docling 변환]                   [openpyxl 로드]
 PDF→구조 markdown                시트별 구조 파악
 (표=markdown table,               │
  제목=heading 계층)          질의 성격 분기:
   │                          ├ 산문/설명형 → 시트를 헤더 살린
[LLM 청킹]                    │   markdown 표/문장으로 → 텍스트 청크
 heading 경계로 시맨틱 분할    └ 계산/필터/집계형 → 원본 표 유지,
 (표는 통째로 한 청크)             DataFrame/SQL 조회 경로로 분리
   │                                 │
   └────────── 공통 인덱스 ──────────┘
        기존 BM25 (+ 선택: 소형 임베딩 하이브리드)
```

### 단계별
1. **분기**: 확장자로 PDF vs Excel 라우팅. (LLM-first 원칙상 규칙 파싱이 아니라 "구조 파서 + LLM 청킹"으로 구성)
2. **PDF → markdown**: **Docling**(CPU, MIT)으로 표 보존 markdown/JSON 변환. 한글 표/스캔 정확도가 부족하면 그 문서만 **Upstage Document Parse** API로 폴백.
3. **Excel 처리**: `openpyxl`로 시트 로드. **평문 덤프 금지**(SpreadsheetLLM 교훈) — 헤더·셀 주소를 살려 (a) 설명형이면 markdown 표/행별 문장으로 청크, (b) 계산형 질의 대상이면 원표를 그대로 두고 DataFrame/Text-to-SQL 조회 경로로 분리(TableRAG 원칙).
4. **청킹**: markdown의 heading 계층 경계로 시맨틱 분할, **표는 쪼개지 말고 통째 한 청크**로. 기존 손작성 markdown 청크와 동일 스키마로 합류.
5. **인덱싱/검색**: 현행 **BM25 유지가 소규모엔 합리적.** 한글 동의어/표현 다양성 리콜이 부족하면 소형 다국어 임베딩(예: 한국어 지원 문장 임베딩)으로 **BM25+임베딩 하이브리드**만 추가(여전히 CPU로 가능, 로컬 VLM 불필요).
6. **(로드맵, 지금 아님)**: 스캔/이미지형 한글 가이드가 급증하거나 표 검색 품질이 파서로 한계에 부딪히면 → **ColQwen2/ColPali 기반 비주얼 RAG(M3DocRAG 구조)** 를 GPU 확보 후 도입 검토. ViDoRe v2 다국어 항목으로 한국어 성능 선검증.

### PDF vs Excel 분기 요약
| | PDF (산문+표) | Excel (.xlsx) |
|---|---|---|
| OCR/파서 | **필요** (Docling; 한글 약하면 Upstage) | **불필요** (셀 구조 이미 존재, openpyxl) |
| 산출물 | 표 보존 markdown → 텍스트 청크 | 설명형=markdown 문장 청크 / 계산형=원표 유지 |
| 검색 | BM25(+임베딩 하이브리드) | 값 조회는 **semantic RAG 금지 → 구조적 조회/SQL** |
| 함정 | 평문 추출(PyPDF) 시 표·순서 붕괴 | 평문 덤프 시 2D 구조·병합셀·서식 손실 |

---

## 5. 미해결 / 한국어 관련 주의

- **한국어 OCR 정확도**: 라틴 기반 OCR은 한글 음절 블록의 띄어쓰기·구조를 오인. **CJK 전용 OCR**이 필수. Docling도 스캔 한글에서 편차가 있을 수 있어, 스캔/저품질 한글 PDF는 **Upstage(한국어 95%+)** 폴백을 실측으로 판단해야 함(현시점 우리 코퍼스로 A/B 필요).
- **독립 중립 벤치 부재**: 인기 파서 벤치가 벤더 자체(ParseBench=LlamaIndex, SCORE-Bench=Unstructured)라 편향 가능. 한국어 표 항목은 **OmniDocBench(CVPR 2025, 다국어/CJK)** 로 교차확인하되, 최종은 **우리 실제 가이드 샘플로 직접 측정**할 것.
- **표 정확도 갭**: 파서 전반이 텍스트 정확도는 높아도(≈74%) **표 구조 보존은 낮음(≈35%, 벤치별)** — "표가 핵심 정보"인 스케줄 가이드에서 가장 큰 리스크. 표는 청킹 시 분할 금지 + 파서 표 출력의 육안 검수 루틴 권장.
- **정형 vs 비정형 오라우팅**: 계산·정확조회형 질의(예: "X등급 야간 상한 몇?")를 semantic RAG로 보내면 근접 청크로 **그럴듯한 오답** 위험. 질의 성격 분기(구조 조회 vs 의미 검색)를 에이전트 플래닝 단계에서 판단하도록 스킬 설계 필요(현 top-down agentic flow와 정합).
- **no-parse VLM의 인프라 벽**: ColPali/ColQwen2/DSE/M3DocRAG는 성능은 우위지만 **GPU + 멀티벡터 인덱스(저장/지연 비용)** 전제. 소규모 코퍼스에선 비용 대비 이득이 작아 **현재는 근거 자료로만 보유, 도입 보류.**
- **라이선스**: PyMuPDF(4LLM)=AGPL, Docling=MIT, Marker/Upstage 상용 조건 확인 필요 — 제품 배포 형태에 따라 검토.

---

### 검증된 출처 (17)
1. Docling Technical Report — arXiv:2408.09869 · repo `docling-project/docling`
2. Docling: Efficient Open-Source Toolkit — arXiv:2501.17887
3. LlamaParse — `llamaindex.ai/llamaparse`
4. Unstructured.io — `unstructured.io`
5. PyMuPDF4LLM — `pymupdf.readthedocs.io/en/latest/pymupdf4llm`
6. Marker — repo `datalab-to/marker` (VikParuchuri)
7. OmniDocBench (CVPR 2025) — repo `opendatalab/OmniDocBench`
8. ColPali (ICLR 2025) — arXiv:2407.01449
9. ColQwen2 — `huggingface.co/vidore/colqwen2-v0.1`
10. DSE / Document Screenshot Embedding (EMNLP 2024) — arXiv:2406.11251
11. M3DocRAG — arXiv:2411.04952
12. ViDoRe Benchmark V2 — arXiv:2505.17166
13. TableRAG (NeurIPS 2024) — arXiv:2410.04739
14. SpreadsheetLLM (Microsoft) — arXiv:2407.09025
15. Table Transformer / PubTables-1M (CVPR 2022) — arXiv:2110.00061 · repo `microsoft/table-transformer`
16. Upstage Document Parse (한국어) — `upstage.ai/products/document-parse`
17. PDF 전처리 합의: procycons PDF Extraction Benchmark 2025 / firecrawl / fastio (2025~2026)
