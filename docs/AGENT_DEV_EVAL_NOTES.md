# AIDE Agent — 개발 & 평가 노트

> agents_v2 온보딩/스케줄링 에이전트의 아키텍처·평가 방법론·결과를 한 곳에 정리.
> 수치는 실제 라이브 API(gpt-5.5 main / gpt-5.4-nano router / gpt-4o vision) 측정값.
> 작성 2026-07 (세션 누적). 평가 하니스는 재실행 가능 — 아래 경로 참조.

---

## 1. 응답 분기 (Response Modes)

질의 → 2단계 라우터(nano 분류 → tool 스코핑) → 메인 에이전트(gpt-5.5).

```
질의 → 라우터
  ├─ action ─┬─ read (조회)   query_schedule / analyze_report / recommend / validate
  │          └─ write (변경)  update_person_attr / bulk_mutation / update_constraint / ...
  ├─ ui ──────  화면 이동       navigate (프론트가 route/modal 해석)
  └─ guide ───  절차 안내(RAG)  lookup_guide (help 코퍼스 검색 → grounded)
       ⊗ 가로지르는 안전장치:  abstain(능력경계·도메인경계) + clarify(되묻기)
```

- **상호배타 아님**: 라우터가 복수 카테고리 선택 가능(ui+guide, read-first→write 등).
- **abstain/clarify는 별도 분기 아님** — 모든 분기 위에 얹히는 안전장치(prompt_builder 능력경계).

---

## 2. 평가 하니스 (재사용)

| 하니스 | 위치 | 측정 |
|---|---|---|
| 파라미터 근거 평가 | `tests/agent_qa/param_eval.py` (+`corpus/queries_params.py`) | 라우팅/선택/슬롯충족/abstain/reject, 2턴 read-first 인식 |
| 가이드 검색 평가 | `tests/agent_qa/guide_eval.py` | Retrieval@1/@2 + abstain (`--rerank` 지원) |
| 오프가이드 분기 평가 | (scratchpad) | 가이드 밖 질의를 action/ui/미지원/모호/도메인밖으로 적절히 분기하나(풀파이프라인) |
| 라우터 recall(단독) | `tests/agent_qa/live_router_eval.py` | 라우터 recall만(상위=param_eval) |

원칙: **selection-level(1콜)은 mutation의 read-first를 오답처럼 보이게 함** → 2턴 인식 또는 full-loop로 채점.

---

## 3. 평가 결과 (before → after)

### 3.1 라우터 recall 구멍 (param_eval, N=24)
| 지표 | baseline | 어휘 전파 후 |
|---|---|---|
| Router recall | 73.9% | **100%** |
| Selection | 69.6% | **91.7%** |
| Slot filling | 100% | 100% |
| 전체 PASS | 65.5% | **89.7%** |

원인=스킬은 다 있는데 classify 어휘 미전파(프리셉터/고정근무/메모/2오프/이브닝금지). 스킬 0개 추가, settings 어휘 + settings_people에 query_schedule 추가로 해결. (커밋 c6fdd67)

### 3.2 가이드 검색 recall + rerank (guide_eval, 변형질의 31)
| 단계 | Retrieval@1 | @2 |
|---|---|---|
| baseline (BM25) | 71.4% | 82.1% |
| + alias 확장 (recall↑) | 78.6% | 92.9% |
| + LLM rerank (nano, 정밀) | 85.7% | 92.9% |
| + 라벨 정정(중복정답) | **100%** | 100% |

- **recall_k 스윕**: recall@k는 k=2에서 이미 100%(alias 효과), 최종@1은 k=6에서 100% 도달·평탄(k=12까지 저하 없음). **sweet spot=k=6**. 코퍼스 커지면 재측정, K는 modest 캡. (커밋 0b6f6ff)
- 교훈: rerank는 recall 세트 안 후보만 승격 → 패러프레이즈 miss는 recall(alias/임베딩)부터.

### 3.3 오프가이드 분기 (풀파이프라인, 변형질의 20)
도메인 경계 규칙 추가 후 **20/20 (100%)**: action(read/write) 6/6, ui 2/2, guide 2/2, 미지원 3/3, 미보유 how-to 2/2(가이드검색→없으면 정직 abstain), 모호 1/1, **도메인밖 4/4**(잡담 거절, 규칙 전 0/4). (커밋 25dbbe9)

### 3.4 선행조건 게이팅 (axis③, 확정 근무표)
확정본 변경 요청 시 baseline 3/3 조용히 변경 → 트리거 추가 후 **3/3 "조정판 vs 직접?" 확인**. (커밋 955dc7a)

### 3.5 토큰/비용
- 툴설명 이중정의(마크다운=JSON) → `TOOL_DESC_MODE=names` 마크다운 제거: 라이브 A/B 무회귀, 입력 -28.5% (커밋 d50dfff)
- 시스템프롬프트 캐싱 재정렬(고정→가변): 유저 간 프리픽스 98% 캐시, 품질 무회귀 (커밋 8b1ce81)

---

## 4. help RAG 파이프라인

```
[업로드 1회·오프라인]
 PDF → (텍스트) pdf_ingest: 제목 접두어로 멀티페이지→기능청크
     → (스크린샷) pdf_ingest_vlm: pypdfium2 렌더 → gpt-4o가 '어느 버튼 어디서' → pdfplumber 하이브리드
     → <pdf>.chunks.json 캐시
[질의]
 lookup_guide → BM25 recall + alias → (선택) LLM rerank → grounded 답변 / found=false면 abstain
```
- 코드: `app/agents_v2/guide/{help_corpus,retriever,pdf_ingest,pdf_ingest_vlm}.py`
- 콘텐츠(PDF·chunks.json)는 gitignore(배포/로컬 제공), 메커니즘만 커밋.
- 문서 유형 판단: 텍스트레이어 O→pdfplumber / 스캔(글자)→OCR / 스크린샷(의미)→VLM / 표→Text-to-SQL(로드맵).

---

## 5. 로드맵 / 남은 것

- rerank를 lookup_guide 프로덕션에 배선(현재 함수만, help질의당 nano콜 비용 결정)
- 전처리 캐시를 JSON 파일 → DB/벡터스토어(운영 규모 시)
- Excel 인제스트(산문→마크다운 / 계산표→Text-to-SQL), 스캔 PDF OCR(Upstage 한국어)
- 표지 청크 노이즈 필터, 임베딩 하이브리드(한국어 패러프레이즈 recall↑ 필요 시)
- 라우터 recall/가이드 검색은 코퍼스·스킬 변경 시 위 하니스로 재측정(회귀 게이트)
