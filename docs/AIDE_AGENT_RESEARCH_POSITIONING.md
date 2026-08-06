# AIDE Agent — 연구 포지셔닝 & 문제정의

> 목적: 우리의 "발화 → 5요소 분해 → category/scope 매핑" 접근을 선행연구에 정직하게
> 위치시키고, 발행 가능한 기여를 정의한다. 구조: 동기 → 문제정의 → 관련연구 → 접근/기여
> → 실측 근거 → 정직한 novelty → 실험계획 → venue.
>
> 작성: 2026-07-08. 기반: E2E 프로브 + recall A/B 실측(본 리포지토리 커밋 6c3126d).
>
> **코드 대조 갱신: 2026-08-06.** 재현 불가능한 증거(§5.3 6스킬 A/B)를 철회하고, 낡은 수치를
> 갱신했다. **어떤 주장이 어느 등급인지는 §5.0 표를 먼저 볼 것.** 🔴 등급 주장은 논문·발표에
> 인용하지 말 것.

---

## 1. 동기 & 배포 맥락 (Deployment Context)

한국어로 운영되는 **간호사 근무표(nurse rostering) 시스템** 위에 자연어 에이전트를 얹는다.
수간호사(HN)가 "4월 원티드 미제출자 리스트업", "김민지 8월 중환자실2 파견", "야간 최대
7회로" 같은 자유 발화로 조회·수정·생성·설정을 수행한다. (근무표 생성 엔진 자체는 본
연구의 기여가 아니며, 초점은 **자유 발화 → 경계 있는 typed operation 매핑**이다.)

핵심 난제: **자유 형식 한국어 발화를, 경계가 있는 typed operation(스킬) 집합으로, 올바른
scope·권한·안전성과 함께 매핑**하는 것. 이는 실배포(production) 요건이다.

---

## 2. 문제정의 (Problem Statement)

에이전트에 능력(스킬)을 계속 추가하는 실배포 환경에서, **능력 선언(declaration)과 발화
분류(intent classification)의 정합**을 어떻게 유지하는가?

우리는 이를 **5요소 분해(decomposition)** 로 정형화한다: 하나의 발화를
**목표(Goal)·데이터(Data)·권한(Permission)·예외(Exception)·검증(Verification)** 로 분해하고,
각 요소를 category/scope/게이트로 매핑한다. 각 요소는 자기 pass/fail 게이트를 갖는다.

---

## 3. 관련 연구 (Related Work) — 정직한 위치

우리 접근은 네 흐름의 교차점에 선다.

### 3.1 Intent Classification + Slot Filling (Task-Oriented Dialogue)
고전 TOD의 joint IC+SF가 우리의 직접 조상이다: **category = intent**, **scope/operation = slot**.
- IC+SF Survey (COLING 2020); 계층적 multi-task 모델(Knowledge-Based Systems; Cognitive
  Computation, Springer); ILLUMINER(LLM few-shot IC+SF, 2024).
- **차이**: 고전 IC+SF는 고정 ontology에 대해 joint model을 **학습**한다. 우리는 self-describing
  tool + LLM으로 **무학습** 실현하고, 능력을 자유 교체한다.

### 3.2 Tool Retrieval / Routing for LLM Agents
2단계 라우터 = tool retrieval/scoping.
- **ToolRet** "Retrieval Models Aren't Tool-Savvy"(ACL 2025 Findings): retriever가 신규·다양한
  tool에서 약하다고 보고 → **우리 관측(신규 스킬 미검색)과 정합**.
- "99% Success Paradox"(near-perfect retrieval ≈ random when선택 실패).

### 3.3 선언형 Tool 등록 / Auto-sync — ⚠️ 우리와 가장 근접
- **ToolDescriptor**("Evolution of Tool Use in LLM Agents", 2026): tool을 descriptor로 선언
  — name/domain/**mode(read/write/commit)**/source/**risk**/**approval**/I/O schema. **우리
  SkillSpec(name/categories/mutation/hn_only/schema)과 거의 동형.**
- **ScaleMCP**(2505.06416): MCP tool 등록 변경을 retrieval/routing에 auto-sync.
- **Agent-First Tool API**(2605.10555): enterprise semantic tool interface.
- **정직한 함의**: 선언형 descriptor와 등록→라우팅 auto-sync는 **이미 존재한다.** 매니페스트
  자체는 novelty가 약하다.

### 3.4 Nurse Rostering + NL/LLM (도메인)
- Static→Dynamic scheduler via NL(arXiv 2405.06697, NSP 대상); Schedule Explainer(AAMAS 2020);
  Nurse Preferences into AI Scheduling(**JMIR 2025, 저널**); Nurse Rostering 방법론 서베이.

### 3.5 확장 가능한 관심사 축 / AOP·NFR for Agents — ⚠️ "5요소 확장 프레임워크"와 근접
"5요소를 pluggable 축 프레임워크로"라는 아이디어의 **핵심은 이미 emerging**이다.
- **"From Goals to Aspects, Revisited: An NFR Pattern Language for Agentic AI"**(arXiv 2603.00472):
  Goal→Aspect(비기능 요구=권한/감사/비용/보안)를 pluggable 패턴으로. **우리 "확장 축"과 거의 동형.**
- **"Formal Policy Enforcement for Agentic Systems"**(arXiv 2602.16708): 에이전트 정책을
  **AOP(join point/pointcut/advice)**로 정형화 — "축을 실행 지점에 끼운다".
- **"Skill-Mediated LLM Agents 참조 아키텍처"**(arXiv 2606.20631): Policy/Config를 cross-cutting substrate로.
- **Guardrails AI / NeMo Guardrails**: composable validator(검증 축의 pluggable 구현).
- **Policy-as-Code(Cedar)**(arXiv 2606.26649): 권한 축을 pluggable 정책으로.
- 의도 분해의 **functional + non-functional decomposition**(intent-driven networking, arXiv 2208.01218).
- **정직한 함의**: pluggable 축/aspect 프레임워크는 **novelty로 주장 불가**(선행 존재). 단, 위 연구들은
  **tool이 이미 검색된 뒤 gating**을 다룬다 — **tool을 애초에 찾느냐(라우팅/vocabulary)**는 안 다룸.
  우리 §4.4는 그 **상류(functional) 층**이라 차별된다.

### 3.6 Planning vs Reactive 실행 (DAG 플래너 + ReAct fallback) — ⚠️ 정립된 조합
우리 실행 계층은 **의존 복합 → DAG 플래너, 단순/독립 → ReAct fallback**을 입력별로 라우팅한다
(`app/agents_v2/planning/`, opt-in `AIDE_DAG_PLANNING`). 자료조사 결과 이는 **새 발명이 아니라
두 개의 named 패턴을 합친 것**이며, 단일 표준 명칭만 없다.
- **LLMCompiler**("An LLM Compiler for Parallel Function Calling", arXiv 2312.04511, ICML 2024):
  Planner→Task-Fetching→Executor. 플래너가 **task DAG를 스트리밍**하고 의존 해소된 task를 병렬
  디스패치(변수참조 `${1}`로 앞 출력 소비). ReAct 대비 **지연 3.7x↓·비용 6.7x↓·정확도 ~9%↑**
  보고 — "의존/병렬 분해 가능하면 planning이 ReAct보다 낫다"의 1차 근거. 우리 DAG 층과 동형.
- **Plan-and-Execute / ReWOO**(LangChain, langchain.com/blog/planning-agents): 플래너(다단계 계획)와
  실행자(상위 LLM 재호출 없이 스텝 실행) 분리 → **더 빠름·저렴·고품질**. 우리 planner/executor 분리와 동일.
- **RP-ReAct**(EmergentMind): Reasoner-Planner를 ReAct **위에** decouple — "플래너 위, ReAct 아래"가
  published 아키텍처로 존재.
- **Dynamic/Conditional (gated) Planning**(EmergentMind, "ReAct&Plan"): **계획이 가치있을 때만 계획
  노력을 할당하는 게이팅** — 우리 "의존성 있을 때만 플래너 발동"의 추상형. "plan globally, act
  locally… not an either/or"(byaiteam 2025-12)가 모토.
- **산업 가이드 정합**: Anthropic "Building Effective Agents"(2024-12)의 **Workflow(predefined
  path, 예측가능)↔Agent(런타임 경로결정)** 구분 + **routing 패턴**("입력 분류 후 특화 후속으로
  dispatch"); OpenAI 실전가이드의 **code(결정론)↔LLM 오케스트레이션** 선택; LangChain의
  router-vs-supervisor — 넷 다 "구조화 경로 vs 반응 경로를 입력별로 고르기"를 권장.
- **정직한 함의**: 패턴 자체는 novelty 아님. 방어 가능한 것은 **분기 기준**(발화 길이/복잡도가
  아니라 **task 간 의존 구조**로 planner↔ReAct 라우팅)이며, 이는 정식 벤치마크된 named
  contribution이라기보단 emerging best-practice → 포지셔닝 여지. **문헌이 지적하는 유일한
  리스크는 두 경로를 두는 것 자체가 아니라, cross-cutting 로직(승인/clarify/검증)을 경로별로
  복제하면 생기는 drift** — 해법은 "경로 통합"이 아니라 **공유 게이트 1개를 양쪽이 호출**
  (docs/AGENT_SHARED_GATE_REFACTOR.md, §4.5).

---

## 4. 접근 & 기여 (Contributions)

정직한 novelty 판정(§6)을 전제로, 방어 가능한 기여는 **개별 부품이 아니라 결합 + 실측**이다.

### 4.1 5요소 분해를 설계 방법론으로
Goal·Data·Permission·Exception·Verification. IC+SF(intent+slot)를 **일반화**하되,
**예외처리·검증을 1급 분해축**으로 승격 — descriptor의 mode/risk보다 넓고, 안전한 실배포에 필수.
각 요소가 pass/fail 게이트라 태스크별 임계값 조정이 설계의 본체가 된다.

### 4.2 2단계 라우팅 (IC+SF의 무학습 계층화)
Level-1 라우터 category(coarse, **10개** — navigation/read/mutate/generate/validate_repair/
analyze/recommend/settings_rules/settings_people/help) → Level-2 스킬 내부 scope(fine).
self-describing tool로 학습 없이 계층 IC+SF를 실현. tool 섹션 −75%(**tool 22개 시점 측정** —
현재 29개라 재측정 필요), 라우팅 recall 은 §5.

### 4.3 단일소스 스킬 매니페스트
`@skill(...)` 하나로 스키마·category·권한·검증을 선언 → 흩어진 배선을 파생. (ToolDescriptor와
동형이나, 아래 4.4의 발견이 차별점.)

### 4.4 ★ 핵심 실증 기여 — Vocabulary Propagation
**선언형 tool 등록은 category→tool 배선을 자동화하지만, 라우터의 "발화→category 분류 어휘"는
자동으로 따라가지 않는다.** 그 결과 신규 tool이 **조용히 미검색**(silent non-retrieval)된다 —
retrieval이 "실패"가 아니라 애초에 후보에 없다. 우리는 라이브 시스템에서 이를 관측·정량화하고,
매니페스트에 `trigger_hint`를 추가해 분류 어휘까지 co-derive함으로써 수정했다.

> ScaleMCP는 auto-sync를 "주장"하나, 우리는 **실배포에서 이 정합이 깨지는 구체적 실패모드와
> 측정된 수정**을 제시한다. ToolRet의 "retriever가 신규 tool에 약하다"를 **분류 단계에서**
> 재확인하고 저비용 수정을 보인 것이 차별점.

### 4.5 실행 계층 — 의존성-트리거 planner↔ReAct 라우팅 + 공유 게이트
§3.6 참조. 개별 패턴(LLMCompiler/Plan-and-Execute/ReAct)이 아니라 **결합 규칙**이 기여점:
(a) 분기 기준이 발화 표면이 아니라 **task 의존 구조**(독립·단일→ReAct 병렬배칭, 의존복합→DAG),
(b) 모든 실패/파싱불가/저신뢰는 ReAct로 **안전 강등**(dead-end 없음). 문헌의 drift 경고에 대한
우리 대응은 **cross-cutting 게이트(승인/clarify/L2검증) 단일화**(docs/AGENT_SHARED_GATE_REFACTOR.md).
이 통합은 novelty가 아니라 **정합성 부채 상환**으로 위치한다.

> **갱신 (2026-08-06)**: 작성 당시 근거였던 "두 경로가 게이트를 각자 구현해 비대칭(staleness는
> DAG만, auto-validation은 ReAct만)" 은 **더 이상 사실이 아니다.** staleness 가드는 ReAct 승인에도
> 대칭 적용됐고(`agent_v3.py` 의 DAG·ReAct 양쪽 승인 경로), `_auto_validate` 도 두 경로가 같은
> 함수를 호출한다. 즉 이 절이 지적한 drift 는 대부분 해소됐다 — 잔여분은
> `docs/AGENT_SHARED_GATE_REFACTOR.md` 기준.

---

## 5. 실측 근거 (Empirical Evidence)

### 5.0 증거 등급 — 무엇이 재현 가능한가 (2026-08-06 코드 대조)

논문화 전에 **재현 가능한 것과 아닌 것을 먼저 가른다.** 아래 등급을 넘어서는 주장은 본
문서 어디에도 쓰지 않는다.

| 주장 | 등급 | 근거 / 사유 |
|---|---|---|
| 파견 쿼리 recall 7% → 87% (§5.2) | 🟢 **기록됨·재실행 가능** | 수치가 커밋 `6c3126d` 메시지에 남아 있고, 처치 토글 `INJECT_MANIFEST_HINTS` 가 코드에 있어 재실행 가능. 단 **tool 22개 시점** |
| silent non-retrieval 실패모드 존재 (§4.4) | 🟡 **기록됨·프로브 미보존** | 발견 경위는 `6c3126d` 커밋 메시지에 서술. 다만 **18쿼리 프로브 세트 자체는 레포에 없음** → 그대로는 재현 불가, 위 토글로 재구성해야 함 |
| 신규 스킬 **전반**의 일반 실패모드 (RQ1) | 🔴 **미검증** | 근거였던 §5.3 표가 재현 불가 → 철회 |
| tool 섹션 −75% 절감 | 🟡 **낡음** | tool 22개 시점. 현재 29개 → 재측정 필요 |
| 라우터 recall "100%" | 🟡 **맞지만 오해 소지** | recall 자체는 100%(param_eval N=24, 73.9→100%). 그러나 **선택(selection)까지 포함한 전체 PASS 는 89.7%**(65.5→89.7, selection 91.7%). "100%"를 파이프라인 성능으로 읽으면 과장. 별도 전코퍼스 스윕에서도 미스 6건(개인 월한도 카테고리 미스매치) |
| category 개수 9개 | 🟡 **낡음** | 현재 **10개**(`help` 추가) |
| 두 실행 경로의 게이트 비대칭 (§4.5) | 🔴 **해소됨** | staleness·auto_validate 모두 공유로 전환 |

**교훈(문서 운영 규칙)**: 수치를 적을 때는 **① 측정 시점의 tool/스킬 수, ② 재현 명령 또는
커밋 해시**를 함께 남긴다. 둘 중 하나라도 없으면 실측 칸이 아니라 계획(§7) 칸에 적는다.

### 5.1 E2E 프로브 (실 LLM, 18 쿼리 × 2런)
14/18 정상 분해(read/navigate/invoke/generate/validate/analyze/recommend/settings + 부분이름
grounding + clarification + 권한차단). **재현되는 실패**: 신규 스킬 manage_assignment(파견)가
두 런 모두 미검색 — "파견"이 read/mutate로 분류되어 tool이 스코프에서 빠짐.

### 5.2 Recall A/B (trigger_hint 전/후, N=3, gpt-5.4-nano)
| 대상 | baseline | +trigger_hint |
|---|---|---|
| 🎯 파견 쿼리 recall | **1/15 (7%)** | **13/15 (87%)** |
| 대조군(회귀확인) | 21/21 (100%) | 20/21 (95%) |
| 전체 | 22/36 (61%) | 33/36 (92%) |

핵심 그래프: **7% → 87%**. 잔여 실패는 "파견 취소"(취소=mutation 어휘가 강함) → mutate
category 다중배선으로 추가 완화. 이는 **§4.4 명제의 정량 입증**이다.

### 5.3 일반성 (RQ1) — ⛔ 미실행 (증거 없음)

> **철회 기록 (2026-08-06).** 이 자리에는 6개 신규 스킬(휴직·프리셉터·재분배·리포트export·
> 근무교환·감사이력)의 baseline 58% → +trigger_hint 100% (부트스트랩 95% CI) 표가 실려
> 있었고, 스크립트 `scratchpad/vocab_generality.py` 를 근거로 "6개로 입증(RQ1 충족)"이라고
> 적었다. **코드베이스 대조 결과 그 6개 스킬은 하나도 존재하지 않고, 그 스크립트도 레포에
> 없다.** 따라서 이 수치는 제3자가 재현할 수 없고, 시뮬레이션이었는지 유실된 실행이었는지도
> 문서에 남아 있지 않다. 재현 불가능한 수치를 실측으로 두는 것은 나머지 실측(§5.2)의
> 신뢰까지 떨어뜨리므로 **표 전체를 삭제하고 RQ1 충족 주장을 철회한다.**

**현재 RQ1 상태**: **미검증.** §5.2 는 스킬 **1개**(manage_assignment/파견)에서의 실패·수정만
보인다. "silent non-retrieval 이 신규 스킬 전반의 일반 실패모드인가"는 **아직 근거가 없다.**

**실행 가능한 대체 증거(권장)**: 가상 스킬을 새로 만들 필요 없이, **이후 실제로 추가된 스킬**로
같은 A/B 를 돌리면 된다 — `manage_daily_shift`, `publish_schedule`, `manage_mutual_exclusion`,
`log_feedback`, `lookup_guide`, `manage_leave_targets`, `manage_banned_wanted` 는 모두 `trigger_hint`
를 가진 실제 스킬이라 토글(`INJECT_MANIFEST_HINTS`) 한 번으로 baseline/처치 대조가 가능하다.
이쪽이 "논문용 더미 스킬"보다 강한 증거다(실배포 어휘 · 재현 가능). 설계는 §7 Study-1 참조.

---

## 6. 정직한 Novelty 판정 (Threats to Novelty)
- 🔴 선언형 descriptor(매니페스트) — ToolDescriptor에 선행.
- 🔴 등록→라우팅 auto-sync — ScaleMCP에 선행.
- 🔴 **pluggable 축/aspect 프레임워크(5요소 확장)** — AOP·NFR-for-agents(2603.00472 등)에 선행.
- 🟡 계층 IC+SF — 고전.
- 🟢 **우리 것 (방어 가능)**:
  (a) ★ **측정된 vocabulary-propagation 실패/수정**(silent non-retrieval, 7→87%) — 선행 AOP/NFR·
      가드레일이 다루지 않는 **상류 functional(라우팅) 층**. 핵심 기여.
      ⚠️ **현재 근거는 스킬 1개(파견)의 사례연구다.** "일반 실패모드"로 쓰려면 Study-1(§7)
      선행 필요 — 그 전까지는 *existence proof* 이상으로 주장하지 않는다.
  (b) **기능 축(goal/data/routing)과 비기능 축(permission/exception/verification)을 하나의
      런타임-구동 선언(매니페스트)에 통합** + 그 결합을 실측 — AOP/NFR은 core-logic과 aspect를
      분리하나, 우리는 통합 선언이 라우팅 vocabulary까지 co-derive.
  (c) 한국어 + 실배포 케이스, 저비용 한국어 rostering 쿼리 벤치.

→ 포지션: **프레임워크 자체가 아니라 "기능·비기능 축 통합 선언 + 측정된 functional-layer 결합
실패/수정"의 결합.** pluggable 축은 엔지니어링 산물로 두고 novelty로 내세우지 않는다.

---

## 7. 실험 설계 (Experimental Protocol)

**연구 질문**
- **RQ1 (일반성)**: silent non-retrieval은 단일 스킬 우연인가, **신규 스킬 전반의 일반 실패모드**인가?
  trigger_hint 의 recall 회복은 스킬 간 일반화되는가?
- **RQ2 (baseline 비교)**: 어휘 전파 수준별 recall — ① descriptor-only(category 배선만, 어휘 없음)
  vs ② +trigger_hint(어휘 co-derive) vs ③ 임베딩 retrieval(ToolRet식).
- **RQ3 (재현성)**: 공개 가능한 한국어 rostering 쿼리 벤치(intent/scope 라벨)로 재현.

**공통 설정**: 라우터 = gpt-5.4-nano. metric = category recall(expected_cat ∈ route(q).categories,
tool-level의 상류 격리) + 부트스트랩 95% CI, query당 N회 반복. 토글 `INJECT_MANIFEST_HINTS`.

**Study-1 (RQ1) — ⛔ 미실행.** (과거 이 자리에 "본 리포지토리에서 실행", 스크립트
`scratchpad/vocab_generality.py` 라고 적혀 있었으나 **스크립트도 대상 스킬 6개도 레포에 없다.**
§5.3 철회 기록 참조.)

재설계: 가상 스킬을 만들지 말고 **실제로 추가된 스킬**로 돌린다 — `manage_daily_shift`,
`publish_schedule`, `manage_mutual_exclusion`, `log_feedback`, `lookup_guide`,
`manage_leave_targets`, `manage_banned_wanted`(7개, 각기 다른 새 어휘). 처치 토글은
`agents_v2/router.py:INJECT_MANIFEST_HINTS`(False=baseline, True=+trigger_hint), 측정은
`tests/agent_qa/live_router_eval.py` / `full_scope_eval.py` 재사용. 결과를 §5.3 에 기입할 때
**측정 시점 tool 수와 커밋 해시를 함께** 남길 것(§5.0 규칙).

**Study-2 (RQ2)**: 동일 스킬셋에 ③ 임베딩 retrieval(스킬 description 임베딩 + top-k) baseline
추가 → 3자 비교. "분류 어휘 co-derive"가 임베딩 검색 대비 언제 유리한지.

**후속**: 공개 벤치(RQ3), 5요소 게이트 ablation(안전/정확도 영향), latency/token 비용 곡선.

## 8. 후보 Venue
- **EMNLP/NAACL Industry track** (실배포 + NLP), 또는 **healthcare informatics 저널(JMIR)** —
  이미 nurse-scheduling+AI 게재 이력. 도메인 fit.
- 방법 각도 강화 시: tool-learning/agent 워크숍(ACL/NeurIPS).

## 9. 위협 요인 (Threats to Validity)
- 라우팅 비결정성(LLM) — N↑, CI 필요.
- 단일 도메인/언어 — 일반화 주장 제한.
- baseline recall이 fallback(전체 tool)로 부풀 수 있음 — 미검색은 non-fallback 케이스로 한정 측정.
- 매니페스트/auto-sync는 선행 존재 — 기여를 "실측 실패모드 + 방법론 결합"으로 명확히 한정.
- **일반성 미검증(N=1)** — 실패모드 근거가 스킬 1개(파견)뿐. Study-1 전에는 일반화 주장 금지(§5.0).
- **수치 부패(measurement decay)** — tool/스킬이 계속 늘어 절감률·recall이 측정 시점에 묶인다.
  본 문서의 수치는 tool 22개 시점 기준이며 현재 29개. 인용 전 §5.0 등급 확인 필수.

---

## 부록 A. 관련 연구 링크
- IC+SF Survey (COLING 2020): https://aclanthology.org/2020.coling-main.42/
- 계층 IC+SF (KBS): https://www.sciencedirect.com/science/article/abs/pii/S0950705119303211
- ILLUMINER: https://arxiv.org/pdf/2403.17536
- ToolRet (ACL 2025): https://aclanthology.org/2025.findings-acl.1258/
- ScaleMCP: https://arxiv.org/pdf/2505.06416
- Evolution of Tool Use / ToolDescriptor: https://arxiv.org/html/2603.22862v2
- Agent-First Tool API: https://arxiv.org/pdf/2605.10555
- Static→Dynamic Scheduler via NL: https://arxiv.org/html/2405.06697v1
- Schedule Explainer (AAMAS 2020): https://www.ifaamas.org/Proceedings/aamas2020/pdfs/p2101.pdf
- Nurse Preferences into AI Scheduling (JMIR 2025): https://formative.jmir.org/2025/1/e67747
- From Goals to Aspects — NFR Pattern Language for Agentic AI: https://arxiv.org/pdf/2603.00472
- Formal Policy Enforcement for Agentic Systems (AOP): https://arxiv.org/html/2602.16708
- Harnessing Agent Skills — Skill-Mediated Reference Architecture: https://arxiv.org/html/2606.20631v1
- Autoformalization of Agent Instructions into Policy-as-Code (Cedar): https://arxiv.org/pdf/2606.26649
- SLA Management in Intent-Driven Systems (functional/non-functional decomposition): https://arxiv.org/pdf/2208.01218
- LLMCompiler — An LLM Compiler for Parallel Function Calling (ICML 2024): https://arxiv.org/abs/2312.04511 · https://proceedings.mlr.press/v235/kim24y.html · code https://github.com/SqueezeAILab/LLMCompiler
- LangChain — Plan-and-Execute Agents (Plan-Execute/ReWOO/LLMCompiler): https://www.langchain.com/blog/planning-agents
- Anthropic — Building Effective Agents (workflow vs agent, routing): https://www.anthropic.com/engineering/building-effective-agents
- OpenAI — A Practical Guide to Building AI Agents (code vs LLM orchestration): https://openai.com/business/guides-and-resources/a-practical-guide-to-building-ai-agents/
- LangChain — Router multi-agent pattern: https://docs.langchain.com/oss/python/langchain/multi-agent/router
- EmergentMind — RP-ReAct (decoupled planner+ReAct): https://www.emergentmind.com/topics/rp-react-reasoner-planner-react
- EmergentMind — ReAct&Plan / Conditional(gated) Planning: https://www.emergentmind.com/topics/react-plan-strategy
- byaiteam — ReAct vs Plan-and-Execute ("plan globally, act locally"): https://byaiteam.com/blog/2025/12/09/ai-agent-planning-react-vs-plan-and-execute-for-reliability/
