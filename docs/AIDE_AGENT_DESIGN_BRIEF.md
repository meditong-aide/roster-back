# AIDE 스케줄링 에이전트 — 기획서 (Design Brief)

> 목적: 지금까지 "만들면서 결정한" 것들을 한 문서로 정리한다. 계기(왜 만들게 되었나) →
> 구조/아키텍처 → 흐름 → **각 결정의 근거**. 마지막에 아직 불분명한 미결 항목을 모은다.
>
> 위상: `.aide/AGENT_DESIGN_V3.md`(원본 v3 설계)와 `docs/adr/0003`(계약화)을 **통합·확장**한
> 상위 기획서. 코드가 SSOT이고 이 문서는 의도와 근거를 담는다.
> 최종 갱신: 2026-07-08.

---

## 0. 한 줄 정의

간호사가 **자연어로 근무표를 조회·수정·생성**하면, 에이전트가 전체 의도를 파악해 스킬을
계획·실행하고, 모든 단계가 추적 가능한 **top-down agentic 파이프라인**.

---

## 1. 계기 — 왜 만들게 되었나 (Why)

### 1.1 기존 방식의 실패
초기 파이프라인은 문장을 조각내 각 조각을 DB에 매핑하는 **bottom-up** 구조였다:

```
user input → concept_typer → grounder → canonicalizer → planner → executor → verifier
```

실패 사례: **"4월 원티드 미제출자 리스트업해봐"**
- "원티드" → lexical 분류 → shift DB에서 UNRESOLVED
- "리스트업해봐" → 패턴사전에 없음 → UNRESOLVED
- planner → UNRESOLVED 존재 → **BLOCKED** (canonicalizer가 scope=wanted로 이미 해석했는데도)

### 1.2 근본 원인
- **문맥 손실**: 문장을 조각내 각각 ground → 합치는 과정에서 전체 의도가 사라진다.
- **한국어 표현 다양성에 취약**: "리스트업/보여줘/알려줘/뽑아줘"마다 패턴을 추가해야 함.
  regex·패턴사전·lookup table은 표현이 늘 때마다 코드 수정을 강요 → agentic하지 않음.

### 1.3 재설계 목표
1. LLM이 **전체 의도**를 한 번에 파악 (조각내지 않음)
2. LLM이 스킬 설명을 읽고 **무엇을 어떤 순서로** 호출할지 스스로 계획
3. 이름→ID 같은 grounding은 **스킬 내부**에서 자체 처리
4. 병원/병동별 데이터는 프롬프트에 넣지 않고 **런타임 DB 조회** (thin memory)
5. 전 과정이 **투명하게 추적** 가능

참고 아키텍처: **LangChain DeepAgent**, **Claude Code의 agentic flow**.

---

## 2. 설계 철학 (Principles)

### 2.1 LLM-first, not rule-driven
모든 자연어 해석은 LLM이 담당한다. regex/pattern dict/lookup table 금지.

**Anti-patterns (금지)**
| 안티패턴 | 이유 |
|---|---|
| regex/hardcoded string matching으로 intent 파싱 | 한국어 표현 다양성 대응 불가 |
| bottom-up fragment parsing → grounder → canonicalizer | 문맥 손실 |
| `_WORKFLOW_PATTERNS` 류 lookup table | 표현 추가마다 코드 수정 |
| planner에서 UNRESOLVED fragment 있으면 무조건 block | 이미 해석된 의도를 무시 |

### 2.2 5요소 분해 방법론 (2026-07 정립)
에이전트/스킬을 만들 때마다 반복하는 설계를 5개 축으로 정형화한다.
**각 요소가 자기 pass/fail 게이트를 갖는다.**

| 요소 | 게이트 질문 | 현재 구현 |
|---|---|---|
| **목표(Goal)** | plan이 실행 가능한가 | 라우터 + 하이브리드 루프 (plan은 프롬프트 레벨 routine) |
| **데이터(Data)** | grounding 완료됐나 | 미들웨어 grounding + 스킬 내부 해석 |
| **권한(Permission)** | 이 호출자가 해도 되나 | 미들웨어 role 게이트 + 매니페스트 파생 |
| **예외(Exception)** | 오류가 알려진 타입인가 | outcome taxonomy (errors.py) |
| **검증(Verification)** | 결과가 postcondition 만족하나 | ⚠️ **가장 약함** — mutation→validate 1경로 + LLM observe |

> "업무 특성에 맞는 실행 기준"이란 이 5개 게이트의 임계값을 태스크별로 조정하는 것.
> 예: generate_schedule은 검증 게이트가 무겁고, query_schedule은 권한이 가볍다.

### 2.3 처리 스코프 · 카테고리 체계 (두 층위)

발화를 처리하려면 **"무엇을 하는 요청인가"를 두 단계로 분류**한다. 둘 다 LLM 의미판단이며
키워드/regex가 아니다.

**Level-1 — 라우터 카테고리 (coarse, 9개)**: "어떤 종류의 작업인가"로 tool subset을 좁힌다.

| 카테고리 | 의미 | 대표 tool |
|---|---|---|
| `navigation` | 화면 이동·폼·비파괴 명령 | navigate, prefill, switch_ward, invoke |
| `read` | 조회·집계 | query_schedule, query_generation_job … |
| `mutate` | 근무/원티드 변경 | bulk_mutation … |
| `generate` | 근무표 자동 생성 | generate_schedule, resolve_infeasibility … |
| `validate_repair` | 위반 검증·교정 | validate_schedule, repair_schedule … |
| `analyze` | 분포·공정성 분석 | analyze_report |
| `recommend` | 대체/교체 추천 | recommend_candidates |
| `settings_rules` | 병동 전체 정책 | update_constraint, update_monthly_limit … |
| `settings_people` | 등급·팀·개인속성·파견 | manage_grade, manage_team_min, manage_assignment … |

> 왜 coarse: 15+ tool에서 바로 고르면 정확도↓. 카테고리로 좁힘(tool 섹션 −75%, recall 57→100%).
> gray-zone(조회 vs 화면이동)은 카테고리에 navigate를 번들 → 미세결정은 메인 프롬프트가.

**Level-2 — 스킬 내부 스코프 (fine)**: 스킬이 top-down으로 "어느 데이터 도메인·어떤 형태"인지 판단.

예) `query_schedule` = **9 scope × 3 operation**
- scope: `wanted_campaign` · `wanted_submissions` · `wanted_adjustment` · `schedule` · `nurse_info`
  · `shift_definitions` · `constraint_config` · `generation_job` · `monthly_limit`
- operation: `list`(목록) · `count`(현황/집계) · `summarize`(요약)

> 왜 스킬이 fine 판단을 갖나: scope는 "키워드 매칭"이 아니라 "묻는 정보가 어느 도메인에 속하나"의
> 의미 판단. 그래서 스킬 description에 scope별 의미·경계를 자연어로 적고 LLM이 고르게 한다.
> (예: "마감일"은 `wanted_campaign`, "미제출자"는 `wanted_submissions` — 축이 다름.)

### 2.4 쿼리 분석 → 요소 decomposition (핵심 방법)

**하나의 발화를 5요소로 분해하고, 각 요소를 category/scope/gate로 매핑**하는 것이 실행 설계의
본체다. 아래는 worked examples — 발화가 어떻게 분해되어 스킬 호출로 귀결되는가.

**예1. "4월 원티드 미제출자 리스트업해봐"** → `query_schedule(scope=wanted_submissions, operation=count)`
| 요소 | 분해 |
|---|---|
| 목표 | 미제출자 목록/현황 조회 → category=`read` |
| 데이터 | 원티드 제출 메타 · 4월 → scope=`wanted_submissions`, operation=`count` |
| 권한 | 조회 · HN 관리병동 → pass |
| 예외 | 캠페인 없음 → `empty_result` / 병동 모호 → `clarification` |
| 검증 | 결과가 목록/집계 형태(postcondition) |

**예2. "김민지 8월 중환자실2로 파견해줘"** → `manage_assignment(create, preview)`
| 요소 | 분해 |
|---|---|
| 목표 | 파견 등록 → category=`settings_people` |
| 데이터 | 김민지→nurse_id · 중환자실2→group_id · 8월 기간 (grounding) |
| 권한 | 병동 영향 mutation → HN/ADM 전용(`hn_only`) |
| 예외 | 퇴사자·기간겹침·office경계(서비스 검증) / 병동 모호→`clarification` |
| 검증 | preview→승인 게이트 → 등록 성공(postcondition) |

**예3. "야간 최대 7회로 바꿔줘"** → `update_constraint(preview)`
| 요소 | 분해 |
|---|---|
| 목표 | 제약 변경 → category=`settings_rules` |
| 데이터 | constraint config · `max_consecutive_nights` |
| 권한 | 병동 전체 설정 → HN/ADM 전용 |
| 예외 | 다음 생성에 영향(경고) |
| 검증 | preview→승인 |

> 이 decomposition이 **스킬 설계의 입력**이다: 새 업무가 들어오면 먼저 5요소로 분해해
> category(어디), scope(무엇), 권한/예외/검증 게이트의 임계값을 정하고 → 그에 맞는 스킬을 짠다.
> manage_assignment도 이 분해에서 hn_only·preview·resolve_group 필요가 도출됐다.

---

## 3. 아키텍처 (What) — 구조

### 3.1 전체 흐름
```
자연어 발화
  → [1] 2단계 라우터   : LLM이 카테고리 분류 → tool subset 스코핑
  → [2] 에이전트 루프  : Routine(구조화) 또는 ReAct(유연) 하이브리드
  → [3] 미들웨어       : 권한 → 컨텍스트 주입 → 내부 그라운딩
  → [4] 스킬 실행      : 17 self-describing 스킬 중 선택
  → [5] Outcome 분류   : ok / clarification / permission / preview / error
  → 답변 · UI-action
```
프레임워크 없이 순수 Python. 모든 단계가 `Stage` trace로 남는다.

### 3.2 컴포넌트별 역할 + 왜

| 컴포넌트 | 역할 | 왜 이렇게 |
|---|---|---|
| **2단계 라우터** (`router.py`) | 발화를 9 카테고리로 분류 → tool 스코핑 | 15+ tool에서 선택 정확도↓ → 좁혀줌. 실패 시 전체 fallback(안전). recall 57→100% 실측 |
| **하이브리드 루프** (`agent_v3.py`) | 알려진 패턴=Routine, 새 요청=ReAct | 순수 tool-loop는 multi-step에서 실패↑(연구 41%). Routine으로 구조화(96%). 단 프롬프트 레벨(코드 아님) → 유연성 유지 |
| **미들웨어** (`middleware.py`) | 권한·컨텍스트·그라운딩을 스킬 공통 배관으로 | 모든 스킬이 재사용 → DRY. 각 스텝이 trace |
| **17 스킬** (`skills/`) | 자기 설명 + 내부 그라운딩 | LLM이 설명 읽고 선택. 이름→ID는 스킬이 자체 해석 → 프롬프트에 데이터 안 넣음 |
| **client-action** (`client_actions.py`) | navigate/prefill/switch_ward/invoke | 화면 이동·폼·비파괴 명령은 서버 실행 아니라 프론트 위임. 백엔드는 논리적 의도만 emit, SSOT→프론트 브릿지 |
| **3-Layer 보안** (`security/`) | 입력 분류·출력 검사·tool결과 `<untrusted>` 래핑 | prompt-injection 방어(nurse_memo 등 사용자 필드가 명령으로 오인되는 것 차단) |
| **계층 메모리** | VariableMemory(스텝간)·대화이력·Tier-2 장기기억 | 스텝간 파라미터 전달 + 세션 넘는 사용자 사실. Tier-2는 **휴면**(테이블 미마이그레이션) |
| **관측성** (`usage.py`) | 목적별(router/turn/memory) 토큰 회계 + 라이브 평가 | 비용·품질을 스킬 단위로 추적 |
| **Outcome Taxonomy** (`errors.py`) | 결과를 단일 분류로 (ADR 0003 §3.C) | 흩어진 예외 분기 통합. 1급=clarification·permission_denied |
| **Skill Manifest** (`manifest.py`) | `@skill` 단일 선언 → 5곳 자동 파생 (§3.D) | 스킬 추가 시 5곳 수동동기 세금 제거. 선언층 단일소스 |

### 3.3 명명 주의
활성 에이전트 = `agents_v2` 디렉터리의 **`agent_v3.SchedulingAgent`** (디렉터리 v2 / 클래스 v3
불일치). 별개의 `app/agents/`(LangGraph)는 채팅 에이전트가 아니라 근무표 생성 그래프.

---

## 4. 흐름 (How) — 발화 처리 과정

예: **"김민지 8월에 중환자실2로 파견해줘"**
```
① 라우터: settings_people 카테고리 → manage_assignment 노출
② LLM: manage_assignment(operation=create, nurse_name=김민지, target_ward=중환자실2, ...)
③ 미들웨어 권한: HN/ADM인가? (매니페스트 hn_only 파생)
④ 미들웨어 grounding: 김민지→nurse_id (target_ward는 스킬 내부 resolve_group)
⑤ 스킬: preview_assignment_impact(dry-run) → 영향 미리보기
⑥ Outcome: PREVIEW로 분류 → awaiting_approval → 사용자 "응" → 실제 create
```

**예외/에스컬레이션** (outcome taxonomy 기반)
- clarification: 이름 모호 → 되묻기
- permission_denied: 권한 없음 → 차단
- preview: mutation 미리보기 → 승인 대기
- error: 그 외 실패 → LLM이 observe해 자연어 설명
- INFEASIBLE(생성): apply_hint → 사용자 동의 → 제약 조정 재시도

---

## 5. 주요 결정 기록 (Why each)

| 결정 | 대안 | 선택 이유 | 근거 |
|---|---|---|---|
| Think-Act-Observe 루프 | Plan-Execute, LATS | 대부분 1–2 call로 종결, 경량 | ReAct (Yao 2022) |
| Routine=프롬프트 레벨 | 코드 실행기 | 유연성 유지, 재발명 회피 | AGENT_DESIGN_V3 §4.3 |
| 프레임워크 없이 | LangGraph | ~100줄로 충분, 과잉추상 회피 | Anthropic, Building Effective Agents |
| 계약 단위=스킬 | 오퍼레이션 단위 | op 단위는 40~60계약 폭증 | ADR 0003 §2 |
| taxonomy 1급=2개만 | 전체 승격 | 보수적 시작, 확장 가능 | ADR 0003 §3.C |
| 매니페스트 단일선언 | 5곳 수동동기 유지 | "따라붙는 세금" 구조적 제거 | ADR 0003 §3.D |
| navigate 액션 제외 | 발행/저장도 트리거 | 파괴적 액션은 사용자 클릭(안전) | 2026-07 결정 |

---

## 6. 현재 상태 & 미결 사항 (Open)

### 6.1 구현 상태
- ✅ 라우터 · 하이브리드 루프 · 미들웨어 · 17 스킬 · 3-Layer 보안 · 관측성
- ✅ Outcome taxonomy (ADR 0003 §3.C)
- ✅ Skill Manifest + 첫 시민 manage_assignment(파견)
- ✅ navigate/invoke 프론트 개편 대응
- 🟡 **Tier-2 장기 메모리**: 코드 완성, prod 테이블 미마이그레이션 → 휴면
- 🟡 **검증 게이트**: mutation→validate 1경로만, 스킬별 postcondition 미선언
- ⬜ **autonomy_mode**: 설계만(ADR 0003 §3.C), 미구현 (현재 항상 수동 승인)
- ⬜ **simulate_constraint**: 보류 (solver 의존 + 온톨로지 진행중)
- ⬜ Tier2 갭: dashboard 확장(기획 필요), ward_redistribute, daily-shift, preferences
- ⬜ Tier3: 변경 이력/undo

### 6.2 확정된 방향 (2026-07-08 결정)
| # | 항목 | 결정 | 함의 |
|---|---|---|---|
| 1 | 대상 사용자 | **HN/ADM 운영도구 중심** | hn_only 기본값이 의도적으로 옳음. nurse-facing 확대는 우선순위 아님 → 권한 설계 단순 유지 |
| 2 | 자율성 | **안전한 것만 자동(auto 모드)** | `autonomy_mode` 를 **구현**한다. 조회·비파괴=자동, 발행·삭제 등 파괴적=승인 유지 |
| 3 | 검증 | **스킬별 postcondition 도입** | 5요소의 마지막 빈칸을 채운다. 매니페스트에 `postcondition=` 을 얹어 단일소스로 |
| 4 | endgame | **병원 프로덕션 배포** | 안정성·권한·프론트 계약·마이그레이션이 우선순위. "미결"을 무기한 미루지 않고 해소 |

---

## 7. 로드맵 (프로덕션 목표 기준 재우선순위)

결정(§6.2)으로 방향이 좁혀졌다. 프로덕션 배포 + auto 모드 + postcondition = **"5요소 완성"**이
자연스러운 다음 축이다.

**P0 — 5요소 완성 (auto + 검증)**
1. `autonomy_mode` 구현 — 이미 만든 outcome taxonomy 위에 정책 매트릭스(manual/auto)를 얹는다.
   조회·비파괴 자동, 파괴적 승인. `SessionContext.autonomy_mode`, 기본 manual(현행 동치).
2. **스킬별 postcondition** — 매니페스트 `SkillSpec` 에 `postcondition` 추가 → 런타임 검증 게이트.
   실패 시 `ErrorType.VERIFICATION_FAILED`. 매니페스트가 검증까지 단일소스로 흡수.

**P1 — 프로덕션 준비**
3. **Tier-2 메모리 결단**: 마이그레이션 적용(활성화) 또는 명시적 보류 표기. (프로덕션이면 활성화 유력)
4. 권한 견고화 · 프론트 계약(SSOT 브릿지) 안정화 · 라우팅 품질 바 설정.

**P2 — 능력 확장 (서비스 갭)**
5. Tier2 스킬: dashboard(기획 선행) · ward_redistribute · daily-shift · preferences.
6. Tier3: 변경 이력/undo. simulate_constraint(온톨로지 안정화 후).

> 원칙: 각 단계는 회귀 리스크 오름차순, 독립 배포 가능, manifest/taxonomy 재사용.
