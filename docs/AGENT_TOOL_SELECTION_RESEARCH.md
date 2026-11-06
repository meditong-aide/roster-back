# LLM Tool-Selection / Tool-Calling 최적화 연구 아카이브

> **갱신 2026-08-06**: 본문의 "tool 22개"와 토큰 수치는 **2026-07-10 시점 스냅샷**이다. 현재는 **29개**.
> 문헌 정리(§3)는 유효하나 §2의 우리 수치는 그 시점 기준으로 읽을 것. 권고 §4-1의 실행 결과도 §4에 반영.
>
> 목적: 22개 tool을 가진 우리 에이전트의 tool 정의 토큰 비용(이중 정의)과 2-stage 라우터 스코핑의 한계를
> 학술·산업 문헌에 비추어 정리한 annotated bibliography. 모든 출처는 arXiv/ACL/공식 블로그 원문을 fetch로
> 직접 확인함(2026-07-10 검증). 검증 실패한 ID는 제외했다.

---

## 1. 문헌이 수렴하는 지점 (요약)

문헌은 세 가지 사실에 강하게 수렴한다. **첫째, tool 수가 늘어나면 tool-selection 정확도가 급격히 떨어진다.**
BFCL·edge 연구·RAG-MCP stress test 모두 수십 개를 넘어 수백 개로 가면 정확도가 크게 무너지고(문헌에 따라
7~85% 하락, 극단적으로는 43%→2%), 원인은 (1) 긴 컨텍스트로 인한 주의 분산과 (2) 의미가 겹치는 tool 설명 간
disambiguation 실패로 지목된다. **둘째, 해법의 주류는 "모든 tool을 프롬프트에 올리지 말고, 쿼리별로 관련 subset만
동적으로 retrieve"하는 것**이다 — 즉 tool을 knowledge base에 색인해 RAG로 뽑는 방식(ToolLLM 신경망 retriever,
Gorilla, Toolshed, Graph RAG-Tool Fusion, RAG-MCP, ScaleMCP). 이 계열은 프롬프트 토큰 50%+ 절감과 정확도
2~3배 향상을 공통적으로 보고한다. **셋째, retrieval과 별개로 tool 정의 자체를 압축/표준화하거나(EASYTOOL),
정의를 아예 지연 로딩·코드로 대체(Anthropic Tool Search / Code Execution with MCP)하면** 같은 문제를 정의-측에서
공략할 수 있다. 다만 ToolRet 벤치마크는 "일반 IR 모델은 tool retrieval에 약하다"는 경고를 남겨, 단순 임베딩
retriever를 그대로 붙이면 오히려 실패율이 올라갈 수 있음을 보여준다(tool 전용 학습/가중이 필요).

---

## 2. 우리 상황 매핑 (crisp statement)

- **tool 개수**: 22개 (query-schedule, bulk-mutation, generate-schedule, ... navigate 계열 포함).
  → **2026-08-06 현재 29개** (publish_schedule·manage_mutual_exclusion·log_feedback·lookup_guide·
  manage_daily_shift·manage_leave_targets·manage_banned_wanted 추가). 아래 토큰 수치는 22개 기준이라
  **현재는 과소추정**이다.
- **이중 정의 문제**: 각 tool이 요청마다 **두 번** 실린다 —
  (1) `tools` 파라미터의 JSON schema ≈ **21k 토큰**,
  (2) 보조 markdown description ≈ **15k 토큰**. 합 ≈ **36k 토큰/요청**이 tool 정의만으로 소비된다.
- **현재 완화책**: 2-stage 라우터(LLM 분류 → tool 스코핑)로 쿼리별 tool 섹션을 축소
  (tool 섹션 ~75% 절감 실측, 22개 시점). 그러나 tool 수가 늘면 스코핑 자체의 한계가 온다는 것을 우리도 인지.
  - ⚠️ **"recall 100%" 를 파이프라인 성능으로 읽지 말 것.** recall 자체는 100%가 맞지만
    (`param_eval` N=24: 73.9% → 100%), **선택(selection)은 91.7%, 전체 PASS 는 89.7%**
    (65.5 → 89.7)다. 즉 "후보에 넣었다"와 "옳게 골랐다"는 다르다. 또 별도 전코퍼스 스윕에서
    미스 6건이 남았다(개인 월한도가 mutate/settings_people 로 분류돼 settings_rules 전용
    배선을 놓친 카테고리 미스매치 — 다중 배선으로 완화). 스코핑은 **완결이 아니라 어휘
    커버리지에 비례해 새는 구조**다.
- **문헌 관점에서 본 우리 문제의 정체**:
  (a) 우리는 이미 "retrieval-before-LLM" 계열의 초기 형태(라우터 스코핑)를 하고 있으나,
  (b) tool 정의가 **이중**이라 절감 여지가 문헌 평균보다 크고,
  (c) 22개는 아직 급격한 degradation 구간(수백 개) 전이지만, navigate sub-action 확장으로 유효 분기 수가 늘고 있어
  "설명이 겹치는 tool 간 혼동"(BFCL·edge 연구가 지목한 원인)이 실질 위험이다.

---

## 3. 테마별 Annotated Bibliography

각 항목: **Title** (venue year, arXiv:ID) — 핵심 발견 — 기법 — → 우리 적용.

### 테마 A. 많은/수백 개 tool 상황의 Tool Retrieval / Selection

**Retrieval Models Aren't Tool-Savvy: Benchmarking Tool Retrieval for Large Language Models** (ACL 2025 Findings, arXiv:2503.01763)
Zhengliang Shi, Yuhan Wang, Lingyong Yan, Pengjie Ren, Shuaiqiang Wang, Dawei Yin, Zhaochun Ren.
- 이 논문이 도입한 벤치마크 이름이 **ToolRet**이다(과제에서 말한 "ToolRet"의 실체 = 이 논문. ID 확정 2503.01763).
- 규모: **7.6k retrieval 태스크 + 43k tool 코퍼스**, 여러 기존 데이터셋 통합.
- 발견 ①: 일반 IR 벤치마크에서 강한 retriever도 **tool retrieval에선 성능이 낮다**. ② 낮은 retrieval 품질이
  tool-use LLM의 최종 task pass rate를 직접 끌어내린다. ③ 저자들이 200k+ 학습 인스턴스를 공개해 tool 전용
  fine-tune 시 크게 개선됨을 보임.
- 기법: heterogeneous tool retrieval 벤치마크 + tool 전용 IR 학습 데이터.
- → 우리 적용: 라우터를 순수 임베딩 유사도로 확장할 때 **범용 임베딩만 믿으면 안 된다**는 경고. 우리 tool 설명/한국어
  질의로 소규모 tool-retrieval eval을 만들어 라우터 recall을 정량 측정하는 근거로 사용.

**ToolLLM: Facilitating Large Language Models to Master 16000+ Real-world APIs** (ICLR 2024, arXiv:2307.16789)
Yujia Qin 외 18인 (OpenBMB/Tsinghua).
- 16,464개 실제 RESTful API(49 카테고리)로 ToolBench 구축.
- 발견 ①: 수많은 API를 매 요청 프롬프트에 넣을 수 없으므로 **신경망 API retriever**로 지시별 API를 추천 —
  수동 선택 제거. ② DFSDT(depth-first search decision tree)로 다중 reasoning trace를 탐색해 복잡한 다단계 호출 성공률↑.
  ③ ToolLLaMA가 미학습 API에도 일반화.
- 기법: 대규모 API 코퍼스 + neural retriever + DFSDT 탐색.
- → 우리 적용: "retrieve-then-call"의 정석 레퍼런스. 우리 라우터의 2-stage(분류→스코핑)를 retriever 관점으로
  재해석하고, 실패 시 대안 tool로 탐색하는 fallback(DFSDT 축약판) 아이디어 제공.

**Gorilla: Large Language Model Connected with Massive APIs** (arXiv:2305.15334, 2023)
Shishir G. Patil, Tianjun Zhang, Xin Wang, Joseph E. Gonzalez (UC Berkeley).
- 발견 ①: retriever를 결합하면 **test-time에 문서(API)가 바뀌어도 적응**하고 hallucinated API 호출이 감소.
  ② API 호출 작성에서 GPT-4를 능가(fine-tune + retrieval). ③ APIBench(HuggingFace/TorchHub/TensorHub) 공개.
- 기법: retriever-aware training(학습 때부터 retrieved doc을 함께 조건화).
- → 우리 적용: tool 정의를 프롬프트 하드코딩 대신 **DB/문서 소스에서 런타임 주입**(CLAUDE.md의 thin-memory 원칙과
  정합)하는 설계를 지지. 정의가 바뀌어도 재학습 불필요.

**Toolshed: Scale Tool-Equipped Agents with Advanced RAG-Tool Fusion and Tool Knowledge Bases** (arXiv:2410.14594, 2024)
Elias Lumer, Vamse Kumar Subbiah, James A. Burke, Pradeep Honaganahalli Basavaraju, Austin Huber.
- 발견 ①: tool 문서를 벡터 KB에 색인하고 pre/intra/post-retrieval 3단계 RAG를 앙상블. ② fine-tune 없이
  ToolE(single/multi), Seal-Tools에서 Recall@5 **+46% / +56% / +47%** 절대 향상. ③ tool 개수·selection threshold를
  조절해 정확도-비용 trade-off 관리.
- 기법: Advanced RAG-Tool Fusion(문서 enrich → query planning/transform → rerank/self-reflection).
- → 우리 적용: 우리 markdown description(15k)을 그대로 프롬프트에 싣지 말고 **KB에 넣고 rerank로 top-k만 주입**하는
  중간 경로. 라우터를 "분류기"에서 "retrieval+rerank"로 승격하는 청사진.

**Graph RAG-Tool Fusion** (arXiv:2502.07223, 2025)
Elias Lumer, Pradeep Honaganahalli Basavaraju, Myles Mason, James A. Burke, Vamse Kumar Subbiah.
- 발견 ①: 순수 벡터 RAG는 tool 간 **의존성**(A tool이 B tool의 출력 파라미터를 필요로 함)을 못 잡는다.
  ② tool을 노드, 의존성을 엣지로 두고 벡터검색+그래프 순회 결합. ③ 신규 벤치 ToolLinkOS(573 tool, 평균 6.3 의존성)와
  ToolSandbox에서 naive RAG 대비 mAP@10 **+71.7% / +22.1%**.
- 기법: dependency graph traversal + vector retrieval.
- → 우리 적용: 우리 복합쿼리 플래닝(예: cancel→recommend→assign 체인)은 사실상 tool 의존성 그래프다. 향후 라우터가
  단일 tool이 아니라 **의존 tool 세트**를 함께 끌어오게 하는 근거.

**ScaleMCP: Dynamic and Auto-Synchronizing Model Context Protocol Tools for LLM Agents** (arXiv:2505.06416, 2025)
Elias Lumer, Anmol Gulati, Vamse Kumar Subbiah, Pradeep Honaganahalli Basavaraju, James A. Burke.
- (과제의 예상 ID 2505.06416 **정확**했음.) 발견 ①: MCP 서버를 SSOT로 두고 CRUD로 tool 저장소를 자동 동기화 —
  수동 갱신에서 오는 중복/불일치 제거. ② 에이전트가 필요 tool을 스스로 메모리에 적재하는 retriever. ③ 임베딩 전략
  **TDWA(Tool Document Weighted Average)**로 tool name·synthetic question 등 핵심 요소를 가중.
- 기법: MCP-native tool retriever + auto-sync 파이프라인 + TDWA 임베딩.
- → 우리 적용: tool 정의의 SSOT를 하나로(우리는 이미 `@skill` 단일 선언→5곳 자동파생 로드맵이 있음 = MEMORY의
  Skill Manifest). 이중 정의(JSON schema + markdown)를 **한 소스에서 파생**하도록 강제하는 방향과 정확히 일치.

**RAG-MCP: Mitigating Prompt Bloat in LLM Tool Selection via Retrieval-Augmented Generation** (arXiv:2505.03275, 2025)
Tiantian Gan, Qiyao Sun.
- 발견 ①: 모든 MCP tool을 한 번에 노출하지 말고 쿼리 기반으로 관련 subset만 semantic retrieval. ② prompt 토큰
  **50%+ 절감**. ③ tool selection 정확도 **13.62%→43.13% (3배+)**. MCP stress test로 tool 수 증가에 따른 붕괴를 실증.
- 기법: 외부 인덱스에서 관련 MCP만 retrieve 후 LLM에 전달(retrieval-before-LLM).
- → 우리 적용: 우리 2-stage 라우터의 학술적 쌍둥이. "라우터를 유지·강화하라"는 직접 근거이자, 우리 recall/토큰 절감
  수치를 이 논문 형식(토큰↓, 정확도↑)으로 리포트하면 경력기술서·ADR 근거가 됨.

---

### 테마 B. Tool 정의의 Token / Context 축소

**EASYTOOL: Enhancing LLM-based Agents with Concise Tool Instruction** (arXiv:2401.06201, 2024)
Siyu Yuan, Kaitao Song, Jiangjie Chen, Xu Tan, Yongliang Shen, Ren Kan, Dongsheng Li, Deqing Yang (Microsoft JARVIS).
- 발견 ①: 실제 tool 문서는 **다양·중복·불완전**해서 LLM tool 사용을 방해한다. ② 이를 **통일된 간결 instruction**으로
  정제하면 token 소비가 크게 줄고 tool 활용 성능이 오른다. ③ 여러 태스크에서 일관된 개선.
- 기법: tool documentation purify + 표준 인터페이스로 재작성(문서 압축·표준화).
- → 우리 적용: 우리 문제의 정곡. **JSON schema(21k) + markdown(15k) 이중 정의는 전형적 "diverse/redundant"**.
  두 소스를 하나의 concise instruction으로 통합하면 즉각적 토큰 절감 + 혼동 감소. 가장 저위험·고효율 착수점.

**Introducing advanced tool use on the Claude Developer Platform** (Anthropic Engineering, 2025-11-24)
Bin Wu 외 (Claude Developer Platform team).
- 발견 ①: **Tool Search Tool** — tool을 `defer_loading: true`로 표시해 필요 시에만 정의를 로드. 검색 도구 자체는
  ~500 토큰. 58 tool·5 서버 기준 **upfront ~55K → ~8.7K 토큰**, 컨텍스트 ~85% 보존. MCP eval에서
  정확도 Opus 4 49%→74%, Opus 4.5 79.5%→88.1%. ② **Programmatic Tool Calling** — 여러 tool을 파이썬 코드로
  오케스트레이션, 중간결과를 컨텍스트에 안 넣음(연구 태스크 43,588→27,297 토큰, -37%). ③ **Tool Use Examples**로
  복잡 파라미터 정확도 72%→90%.
- 기법: deferred tool loading + progressive disclosure + 코드 기반 오케스트레이션.
- → 우리 적용: 우리 세션 자체가 deferred-tool 방식을 쓰는 것과 동일 철학. tool 정의를 **기본 지연 로딩**하고 라우터가
  선택한 것만 실체화하면 이중 정의 36k를 요청당 대폭 절감. (예시 삽입으로 navigate sub-action 파라미터 정확도↑도 참고.)

**Code execution with MCP: Building more efficient agents** (Anthropic Engineering, 2025-11-04)
Adam Jones, Conor Kelly.
- 발견 ①: 모든 tool 정의를 upfront 로드 + 중간결과가 모델을 반복 통과 = 토큰 폭증. ② tool을 코드 API(파일시스템)로
  제시하고 에이전트가 코드를 써서 호출하면 **필요한 정의만** 로드하고 데이터를 실행환경에서 필터. ③ 한 워크플로에서
  **150,000→2,000 토큰 (-98.7%)**.
- 기법: MCP-as-code-API + 필요 시 정의 탐색(progressive disclosure) + 실행환경 내 데이터 처리.
- → 우리 적용: 우리 tool이 늘어 정의 폭증이 심해지면, 정의를 프롬프트가 아니라 **코드/파일로 노출**하는 상위 옵션.
  중장기 아키텍처 후보(단기엔 과함).

(테마 B에는 RAG-MCP·Toolshed·EASYTOOL이 토큰 절감 측면에서도 교차 적용됨.)

---

### 테마 C. Tool 수 증가에 따른 정확도 저하 (scaling / degradation)

**Less is More: Optimizing Function Calling for LLM Execution on Edge Devices** (DATE 2025, arXiv:2411.15399)
Varatheepan Paramanayakam, Andreas Karatzas, Iraklis Anagnostopoulos, Dimitrios Stamoulis.
- 발견 ①: 핵심 통찰 — **가용 tool 수를 선택적으로 줄이면 function-calling 성능이 유의미하게 오른다**(fewer, more
  relevant tools = 혼동↓, 집중↑). ② fine-tune 불필요. ③ 실행시간 최대 -70%, 전력 -40%, 작은 context window 사용 가능.
- 기법: fine-tuning-free 동적 tool 선택(subset 제한).
- → 우리 적용: "라우터로 tool subset을 줄이는 것"이 정확도 자체를 올린다는 인과 근거. 우리 스코핑을 단순 비용절감이
  아니라 **품질 개선 장치**로 포지셔닝. 22개도 쿼리당 5~7개로 좁히면 이득.

**Berkeley Function Calling Leaderboard (BFCL)** (UC Berkeley, blog 2024-08 업데이트; 논문화 진행)
Fanjia Yan, Huanzhi Mao, Charlie Cheng-Jie Ji, Ion Stoica, Joseph E. Gonzalez, Tianjun Zhang, Shishir G. Patil.
- 발견 ①: 2,000개 question-function-answer로 **AST 기반 평가**(수천 개 함수로 확장 가능, 타입/파라미터 엄격 매칭).
  ② simple 호출은 오픈/프로프라이어터리 비슷하나 **multiple·parallel 호출로 갈수록 성능 저하**가 뚜렷.
  ③ 후속 버전(V3 multi-turn, V4 agentic)으로 난이도 확장. (문헌·2차 분석에서 tool 수 4→51 시 특정 태스크
  43%→2%, 49~741 tool 스트레스에서 7~85% 하락이 인용됨 — 근본 관찰의 출처.)
- 기법: 실행가능·AST 이중 평가로 대규모 함수 정확도 측정 프레임워크.
- → 우리 적용: 우리 라우터+tool 세트에 대한 **회귀 평가 방법론**의 표준. MEMORY의 live_router_eval와 결합해 tool 추가
  시마다 degradation을 모니터링하는 게이트로 활용.

(RAG-MCP의 MCP stress test, Less-is-More의 결과도 테마 C의 정량 근거로 교차 인용.)

---

### 테마 D. 아키텍처 패턴 (hierarchical / orchestration / 압축)

**An LLM Compiler for Parallel Function Calling** (ICML 2024, arXiv:2312.04511)
Sehoon Kim, Suhong Moon, Ryan Tabrizi, Nicholas Lee, Michael W. Mahoney, Kurt Keutzer, Amir Gholami (UC Berkeley).
- (과제 예상 ID 2312.04511 **정확**.) 발견 ①: sequential reasoning(ReAct)의 지연·비용·오류를 **병렬 오케스트레이션**으로
  개선. ② 3-컴포넌트: Function Calling Planner / Task Fetching Unit / Executor. ③ ReAct 대비 지연 최대 3.7x↓,
  비용 6.7x↓, 정확도 ~9%↑; OpenAI parallel FC 대비도 1.35x 지연 이득.
- 기법: DAG 계획 → 의존성 해소 → 병렬 실행(컴파일러 유추).
- → 우리 적용: 우리 복합쿼리 병렬 배칭(MEMORY: Compound Query Planning)의 학술 근거. planner가 tool 호출 DAG를
  세우면 지연·비용 동시 개선.

**Toolformer: Language Models Can Teach Themselves to Use Tools** (NeurIPS 2023, arXiv:2302.04761)
Timo Schick, Jane Dwivedi-Yu, Roberto Dessì, Roberta Raileanu, Maria Lomeli, Luke Zettlemoyer, Nicola Cancedda, Thomas Scialom (Meta AI).
- 발견 ①: 소수 데모만으로 **언제/어떤 API를 어떤 인자로 호출할지 self-supervised로 학습**. ② 계산기·검색·QA·번역·달력
  등 통합. ③ zero-shot 성능이 훨씬 큰 모델과 경쟁.
- 기법: self-supervised API-call 삽입 학습(정의 few-shot 최소화).
- → 우리 적용: 직접 채택 대상은 아니나, "tool 호출은 방대한 정의 나열보다 **소수 예시·경량 시그널**로 유도 가능"이라는
  방향성. tool 정의를 얇게 유지하는 철학적 근거(테마 B와 연결).

(Anthropic Programmatic Tool Calling / Code Execution with MCP도 hierarchical·code-orchestration 패턴으로 테마 D에 해당.)

---

## 4. 권고 방향 (우리 시스템 적합도 순 랭킹)

1. **[최우선·저위험] 이중 정의 통합 → 단일 SSOT에서 파생 (EASYTOOL + ScaleMCP + Skill Manifest)**
   JSON schema(21k)와 markdown(15k)을 하나의 정제된 소스로 통합하고 스키마/설명을 자동 파생. 즉시 토큰 절감 +
   중복 설명으로 인한 tool 혼동 감소. 우리 로드맵의 `@skill` 단일 선언과 정확히 합치 → 가장 빠른 ROI.
   - ✅ **부분 실행됨(2026-07)**: markdown 설명이 JSON `function.description` 과 글자 그대로 이중이라
     마크다운 블록을 통째로 제거하는 경로를 넣었다 —
     `agents_v2/harness/prompt_builder.py:TOOL_DESC_MODE`(env `AIDE_TOOL_DESC_MODE=names`, **기본 full**).
     라이브 A/B 결과 **입력 토큰 −28.5%**(스코프 6개 쿼리는 −51%), 회귀 없음. 다만 기본값이 아직
     `full` 이라 **실사용 절감은 0** — 켜는 결정이 남아 있다. `@skill` 매니페스트도 신규 스킬만
     적용돼 이중 정의 통합은 미완.

2. **[우선] 라우터를 "분류"에서 "retrieval+rerank"로 승격 (RAG-MCP / Toolshed / ToolLLM)**
   tool 설명을 KB에 색인, 쿼리별 top-k만 프롬프트 주입. 우리 2-stage 라우터의 자연스러운 진화. 문헌 공통 결과(토큰
   50%+↓, 정확도 2~3배↑)를 기대. 단, **ToolRet 경고** — 범용 임베딩만 쓰지 말고 우리 tool/한국어 질의로 소규모 학습·튜닝.

3. **[우선] Deferred / progressive tool loading (Anthropic Tool Search Tool)**
   라우터가 고른 tool만 실체화하고 나머지는 지연 로딩. 우리 하니스가 이미 지원하는 패턴(deferred tools)과 동형 →
   구현 장벽 낮고 이중 정의 36k를 요청당 대폭 절감. Tool-use 예시 삽입으로 navigate sub-action 파라미터 정확도도 개선.

4. **[품질 게이트] Tool-count degradation 회귀 평가 상설화 (BFCL / Less-is-More / live_router_eval)**
   tool 추가/스코프 변경 시마다 recall·selection accuracy·토큰을 측정하는 게이트. "라우터 스코핑은 비용이 아니라 품질
   장치"임을 수치로 증명(Less-is-More 근거). 이미 있는 live_router_eval 하니스를 확장.

5. **[중장기] 의존성-aware tool 세트 retrieval & 병렬 오케스트레이션 (Graph RAG-Tool Fusion / LLMCompiler)**
   복합쿼리(cancel→recommend→assign) 체인을 tool 의존성 그래프로 모델링하고 planner가 DAG로 병렬 실행. tool 수가
   더 늘고 체인이 깊어질 때 도입. 현재 22개엔 과투자.

---

## 5. 미해결 / 추가 조사 필요

- **한국어 tool retrieval 성능**: 모든 벤치(ToolRet/BFCL/Toolshed)는 영어 중심. 우리 한국어 질의·tool 설명에서
  임베딩 retriever recall이 얼마나 유지되는지 자체 측정 필요(ToolRet 경고가 한국어에서 더 클 수 있음).
- **"scaling law" 형태의 정량식 부재**: tool 수 N vs 정확도의 깔끔한 수식/논문을 확정 검증하지 못함. BFCL·edge·
  RAG-MCP의 산발적 수치(43%→2%, 7~85%↓)는 2차 인용이거나 stress-test라 우리 tool 밀도/유사도에 그대로 대입 불가.
  → 우리 세트로 직접 측정하는 것이 가장 신뢰도 높음.
- **JSON schema vs markdown 어느 쪽이 정확도에 더 기여하는가**: 이중 정의 중 하나를 제거·압축할 때 정확도 손실을
  측정한 논문을 못 찾음. ablation을 우리가 직접 돌려야 함.
- **검증 제외 항목**: 검색 결과에 2026년 arXiv ID로 뜬 다수 항목(예: "Scaling Enterprise Agent Routing",
  "Beyond Single-Shot: Multi-step Tool Retrieval", 각종 26xx ID)은 abstract 원문 fetch로 **개별 검증하지 않아 본
  아카이브에서 제외**했다. 필요 시 후속 조사 대상.
- **MCP 코드실행 방식의 정확도 trade-off**: Anthropic 블로그는 토큰 절감을 강조하나, 코드 오케스트레이션이 단순 태스크에서
  오히려 오류를 늘리는지에 대한 독립 벤치마크는 미확인.

---

### 부록: 검증한 출처 목록 (fetch 확인, 총 14건)

| # | 출처 | ID / URL | 검증 |
|---|---|---|---|
| 1 | Retrieval Models Aren't Tool-Savvy (ToolRet) | arXiv:2503.01763 (ACL 2025) | ✓ abstract |
| 2 | ToolLLM | arXiv:2307.16789 (ICLR 2024) | ✓ abstract |
| 3 | Gorilla | arXiv:2305.15334 | ✓ abstract |
| 4 | Toolshed | arXiv:2410.14594 | ✓ abstract |
| 5 | Graph RAG-Tool Fusion | arXiv:2502.07223 | ✓ abstract |
| 6 | ScaleMCP | arXiv:2505.06416 | ✓ abstract |
| 7 | RAG-MCP | arXiv:2505.03275 | ✓ abstract |
| 8 | EASYTOOL | arXiv:2401.06201 | ✓ abstract |
| 9 | Anthropic Advanced Tool Use (Tool Search Tool) | anthropic.com/engineering/advanced-tool-use | ✓ 원문 |
| 10 | Anthropic Code Execution with MCP | anthropic.com/engineering/code-execution-with-mcp | ✓ 원문 |
| 11 | Less is More (edge function calling) | arXiv:2411.15399 (DATE 2025) | ✓ abstract |
| 12 | Berkeley Function Calling Leaderboard | gorilla.cs.berkeley.edu/blogs/8_... | ✓ 원문 |
| 13 | LLMCompiler | arXiv:2312.04511 (ICML 2024) | ✓ abstract |
| 14 | Toolformer | arXiv:2302.04761 (NeurIPS 2023) | ✓ abstract |

**ID 정정 노트**: 과제의 "ToolRet (ACL 2025)"는 별도 논문이 아니라 **arXiv:2503.01763 "Retrieval Models Aren't
Tool-Savvy"** 가 도입한 벤치마크 이름이다(정정·확정). ScaleMCP(2505.06416)와 LLMCompiler(2312.04511)의 예상 ID는
원문 대조 결과 **정확**했다. 나머지 known starting points(Toolformer 2302.04761, Gorilla 2305.15334, ToolLLM
2307.16789, EASYTOOL 2401.06201)도 원문으로 확정.

_작성 2026-07-10. 모든 항목 arXiv/ACL/공식 블로그 원문 fetch로 검증._
