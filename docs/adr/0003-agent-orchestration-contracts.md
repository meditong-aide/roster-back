# 0003 — 에이전트 오케스트레이션: Plan Routine · Skill Contract · Error Taxonomy · Configurable Autonomy

- Status: Proposed
- Date: 2026-07-07
- Related: [CLAUDE.md](../../CLAUDE.md) (top-down agentic flow), [agent_v3.py](../../app/agents_v2/agent_v3.py), [router.py](../../app/agents_v2/router.py), [middleware.py](../../app/agents_v2/middleware.py)

> **⚠️ 명명 주의**: 활성 오케스트레이션 에이전트 = `agents_v2` 디렉터리의
> **`agent_v3.SchedulingAgent`**. 디렉터리명(v2)과 클래스 파일명(v3)이 불일치한다.
> 마운트 경로: [main.py](../../app/main.py) → `agent_chat_router` →
> [chat_router.py](../../app/agents_v2/chat_router.py) `from agents_v2.agent_v3 import SchedulingAgent`.
> 별개로 존재하는 `app/agents/`(LangGraph `main_graph`)는 채팅 에이전트가 아니라
> `graph_service`가 쓰는 근무표 생성 그래프 — 본 ADR 범위 밖.

## 1. Context

에이전트 개발을 **5요소 분해**(목표·데이터·권한·예외·검증)로 정형화한다. 각 요소는
"이 스킬/에이전트가 성립하려면 무엇이 참이어야 하는가"의 다른 축이며, **각 요소가 자기
pass/fail 게이트를 갖는다.**

| 요소 | 게이트 | 현 상태 |
|---|---|---|
| 목표(Goal) | plan이 실행 가능한가 | ◐ 라우터 견고 / plan 암묵 |
| 데이터(Data) | grounding 완료됐나 | ● grounding 견고 (Tier-2 memory 휴면) |
| 권한(Permission) | 게이트 통과? | ● [middleware.py `_check_permission`](../../app/agents_v2/middleware.py) |
| 예외(Exception) | 오류가 알려진 타입인가 | ○ case 더미, taxonomy 없음 |
| 검증(Verification) | postcondition 만족? | ○ mutation→validate 1경로만 |

세 빈칸(**plan·예외·검증이 아직 아티팩트가 아니라 코드 분기**)을 **선언적 계약**으로
끌어올리는 것이 본 ADR의 목표다.

## 1.5 Prior Art & Reconciliation (2026-07-07 추가)

본 ADR 초안 작성 후 원본 설계 [.aide/AGENT_DESIGN_V3.md](../../.aide/AGENT_DESIGN_V3.md)를
재확인한 결과, 아래 사실이 드러나 초안을 조정한다:

- **Plan(Routine)은 이미 구현됨** — 코드가 아니라 **프롬프트 레벨**로. Routine A~G가
  [DOMAIN_KNOWLEDGE.md](../../.aide/DOMAIN_KNOWLEDGE.md) §5에 정의돼 있고
  [prompt_builder.py `_build_routine_definitions`](../../app/agents_v2/harness/prompt_builder.py)가
  system prompt에 주입한다. 원본 설계는 *"Routine은 코드가 아니라 프롬프트 내 지시문"*
  (AGENT_DESIGN_V3 §4.3)을 **의도적으로** 선택했다.
- **원본은 verification=LLM observe, error=LLM observe로 구조를 의도적으로 뺐다**
  (AGENT_DESIGN_V3 §9.1, §3.3). taxonomy/검증 게이트 부재는 누락이 아니라 결정이었다.

→ **따라서 §3.A(코드 Routine 실행기)는 폐기**한다(재발명 + 철학 역행). Plan 강화가
필요하면 DOMAIN_KNOWLEDGE §5에 Routine을 추가/정교화한다(원본 철학 유지).

→ **§3.B/§3.C의 정당한 정체는 "무에서 구조 추가"가 아니라 "이미 코드에 새어든 드리프트
통합"**이다. 현재 [agent_v3.py](../../app/agents_v2/agent_v3.py)는 이미 순수 LLM-observe에서
벗어나 apply_hint(:451)·dup 감지(:349)·preview(:476)·proactive 검증(:801)이 **제각각
`if` 블록으로** 박혀 있다. §3.C는 이 4개를 하나의 taxonomy+dispatch로 모으는 **동작 동치
리팩터**이며 이것이 유일한 실 구현 작업이다.

## 2. Decisions

1. ~~**Plan = 하이브리드**: 알려진 복합 패턴만 `Routine`(고정 step DAG)으로 구조화~~
   **[폐기 — §1.5 참조]** Routine은 이미 프롬프트 레벨로 구현됨. 코드 실행기 신설 안 함.
2. **계약 단위 = 스킬 단위 + 오류 union**: 성공조건 1개(op 무관 공통), 가능한 오류타입은
   그 스킬이 낼 수 있는 것 전부를 나열. op 단위는 성공조건이 갈리는 극소수 스킬만 예외 분리.
   (op 단위 전면 적용 시 `bulk_mutation` 하나가 scope×action 8계약 → 전체 40~60계약 폭증.)
3. **Error Taxonomy 1급 승격 = `clarification`, `permission_denied`** 우선. 구조는 확장
   가능하게 두되(INFEASIBLE/EMPTY_RESULT/GROUNDING_FAILED/DOWNSTREAM/VERIFICATION_FAILED),
   나머지는 당분간 `GENERIC`으로 수용.
4. **에스컬레이션 = 타입별 정책 매트릭스 × 자율성 모드**(Claude Code permission mode 방식).
   매트릭스는 하나, 열(mode)이 런타임에 바뀐다. 기본 `manual`(현행 동작 보존).

## 3. Design

### 3.A Plan — Routine (신규 `app/agents_v2/routines.py`) — ⛔ 폐기 (§1.5)

> **이 절은 폐기됐다.** Routine은 이미 [DOMAIN_KNOWLEDGE.md §5](../../.aide/DOMAIN_KNOWLEDGE.md) +
> [prompt_builder](../../app/agents_v2/harness/prompt_builder.py)로 프롬프트 레벨 구현됨.
> 아래 코드 스케치는 재발명이므로 구현하지 않는다. 이력 보존용으로만 남긴다.

```python
@dataclass
class RoutineStep:
    skill: str
    args: dict                 # $vm.{key} / $step{n}.{field} 참조 허용
    when: str | None = None    # 선택: 이전 결과 조건 (LLM 아님, 단순 표현식)

@dataclass
class Routine:
    name: str
    description: str           # LLM matcher가 읽는 자연어 (top-down, regex 금지)
    steps: list[RoutineStep]

ROUTINES: list[Routine] = [
    Routine(
        name="wanted_nonsubmitter_purge",
        description="원티드 미제출자를 조회한 뒤 일괄 처리하는 복합 흐름",
        steps=[
            RoutineStep("query_schedule", {"scope": "wanted_submissions", "operation": "count"}),
            RoutineStep("bulk_mutation", {"scope": "wanted_adjustment", "nurse_ids": "$step0.nonsubmitters"}),
        ],
    ),
]
```

- **Matcher**: [router.py](../../app/agents_v2/router.py)의 LLM 분류 패턴 재사용 —
  발화 → routine name(또는 없음). 키워드/regex 금지(CLAUDE.md).
- **통합 지점**: [agent_v3.py `_run_impl`](../../app/agents_v2/agent_v3.py) 라우터
  직후. routine 매치 시 `execute_routine()`(구조화 실행), 아니면 기존 `for turn in
  range(MAX_TURNS)` 루프. **ReAct 경로는 무변경** → 회귀 리스크 격리.
- **Executor**: step 순회하며 `execute_skill` 호출, 결과를 VariableMemory에 저장
  (`$stepN` 참조 해석). step 실패 시 §3.C 정책 매트릭스로 위임.
- Routine 실행도 기존 `Stage` trace에 `plan` 단계로 기록 → 투명성 유지.

### 3.B Skill Contract + Verification Gate (신규 `app/agents_v2/contracts.py`)

```python
@dataclass
class SkillContract:
    success: Callable[[Any], bool]        # postcondition (op 무관 공통)
    errors: list[ErrorType]               # 이 스킬이 낼 수 있는 오류 union
    verify: str | None = None             # 후속 검증 스킬 (예: "validate_schedule")

CONTRACTS: dict[str, SkillContract] = {
    "query_schedule": SkillContract(
        success=lambda d: isinstance(d, (list, dict)) and "error" not in d,
        errors=[ErrorType.EMPTY_RESULT, ErrorType.GROUNDING_FAILED],
    ),
    "bulk_mutation": SkillContract(
        success=lambda d: isinstance(d, dict) and d.get("updated_count", -1) >= 0,
        errors=[ErrorType.PERMISSION_DENIED, ErrorType.EMPTY_RESULT,
                ErrorType.GROUNDING_FAILED],  # op별 union
        verify="validate_schedule",           # 변경 후 자동 검증
    ),
    # ... 스킬당 1계약, 총 ~15개
}
```

- **Verification Gate**: [agent_v3.py](../../app/agents_v2/agent_v3.py)에서
  `result = execute_skill(...)` 직후 `outcome = check_contract(skill, result.data)`.
  - success 실패 → `ErrorType.VERIFICATION_FAILED`로 taxonomy 진입.
  - `verify` 선언 시 후속 스킬 자동 호출. **현재 [`_execute_approval`의
    mutation→validate 하드코딩](../../app/agents_v2/agent_v3.py)을 계약 선언으로 일반화.**
- op 단위 예외: 극소수 스킬만 `success`를 `{op: predicate}` dict로 override.

### 3.C Error Taxonomy + Configurable Autonomy (신규 `app/agents_v2/errors.py`)

```python
class ErrorType(str, Enum):
    CLARIFICATION      = "clarification"       # 1급 (승격)
    PERMISSION_DENIED  = "permission_denied"   # 1급 (승격)
    INFEASIBLE         = "infeasible"          # apply_hint 경로 (기존)
    EMPTY_RESULT       = "empty_result"        # 확장 예정 → 당분간 GENERIC 취급 가능
    GROUNDING_FAILED   = "grounding_failed"
    DOWNSTREAM_ERROR   = "downstream_error"
    VERIFICATION_FAILED = "verification_failed"
    GENERIC            = "generic"

def classify_error(data) -> ErrorType | None:
    """산발적 _is_error/_needs_clarification/_is_preview_result를 한 곳으로 통합."""
```

**정책 매트릭스** (`Action = RETRY | ESCALATE | ABORT | PREVIEW | PROCEED`):

```python
POLICY: dict[str, dict[ErrorType, Action]] = {
    "manual": {   # 기본 = 현행 동작 보존
        ErrorType.CLARIFICATION:     Action.ESCALATE,
        ErrorType.PERMISSION_DENIED: Action.ABORT,
        ErrorType.GROUNDING_FAILED:  Action.ESCALATE,
        ErrorType.INFEASIBLE:        Action.ESCALATE,   # apply_hint 질의
        ErrorType.DOWNSTREAM_ERROR:  Action.ESCALATE,
    },
    "auto": {     # 복구가능 실패 자동 재시도 후 에스컬레이트
        ErrorType.GROUNDING_FAILED:  Action.RETRY,
        ErrorType.CLARIFICATION:     Action.ESCALATE,
        ErrorType.PERMISSION_DENIED: Action.ABORT,
        # ... 미정의 타입은 manual로 fallback
    },
    "auto_accept": { ... },  # mutation preview 생략 (HN 한정) — 위험, 후속 단계
}
```

- **`SessionContext.autonomy_mode`** 신규 필드, 기본 `"manual"`.
  [session_context.py](../../app/agents_v2/schemas/session_context.py)에 추가.
- **통합 지점**: 루프 본문의 산발 분기
  ([agent_v3.py 예: preview/apply_hint/error 처리](../../app/agents_v2/agent_v3.py))를
  `action = POLICY[ctx.autonomy_mode].get(err_type, MANUAL_DEFAULT)` 단일 dispatch로 대체.
- `manual` 매트릭스가 현행 분기와 1:1 동치가 되도록 매핑 → **행동 변화 0 (회귀 안전)**.

### 3.D Skill Manifest — 선언 단일화 (2026-07-07 구현)

**문제**: 스킬 하나 추가 = 최대 5곳 수동 동기(descriptions 스키마 · registry @register ·
router CATEGORY_TOOLS · middleware 권한 · grounding). 백엔드 기능이 늘 때마다 이 세금을
반복 → "에이전트가 따라붙어 고생"하는 구조적 원인. 실행층(execute_skill·grounding·
taxonomy)은 DRY인데 **선언층에 단일 소스가 없었다.**

**해법**: `app/agents_v2/skills/manifest.py` — `@skill(name, schema, categories=,
mutation=, hn_only=, grounds=)` 하나로 선언하면 소비 지점들이 **파생(derive)**:

```python
@skill("manage_assignment", SCHEMA,
       categories=["settings_people"], mutation=True, hn_only=True,
       grounds=["nurse_name", "group_name"])
def manage_assignment(db, params): ...
```

- 핸들러 → 기존 `SKILL_REGISTRY`(run_skill 무변경)
- `manifest_tools()` → descriptions.`SKILL_TOOLS` 병합
- `manifest_category_tools()` → router.`CATEGORY_TOOLS` 병합
- `manifest_mutation_skills()`/`manifest_hn_only_skills()` → middleware `_check_permission` 파생

**성질**: 기존 17개(@register)는 무변경(무위험). 신규 스킬만 @skill → **준비물 ②(권한)·
③(라우터)이 자동**. client_actions 의 dump→프론트 브릿지와 같은 '단일 소스 → 파생' 패턴을
내부에 적용. 부수 효과로 **SKILL_TOOLS↔SKILL_REGISTRY 정합 테스트**를 추가해 드리프트
(스키마만 있고 핸들러 없음 → 런타임 KeyError)를 사전 차단.

**남은 준비물 ①**: 병동/그룹 이름→group_id 그라운딩 헬퍼(`resolve_group`)는 manage_assignment
구현 시 함께. 매니페스트 `grounds=` 는 현재 문서/검증용이며, 자동 그라운딩 배선은 후속.

## 4. Rollout Order (회귀 리스크 오름차순)

1. **`errors.py` taxonomy + `classify_error`** — 기존 산발 분기를 통합하되 `manual`
   매트릭스로 동치 보존. 동작 변화 없음. 회귀테스트: 기존 `tests/agent_qa/` 전부 그린.
2. **`contracts.py` + verification gate** — mutation→validate를 계약으로 이관(동치),
   이후 read 계약 확장.
3. **`autonomy_mode`** 필드 + `auto` 매트릭스 — opt-in. 기본값이 manual이라 무주입=현행.
4. **`routines.py`** — routine 1~2개로 시작(미제출자 흐름 등), 매치 실패=기존 ReAct.

각 단계는 독립 배포 가능하며, 앞 단계 없이 뒤 단계만 켜도 무해(graceful degrade).

## 5. Non-goals

- Plan-then-execute 전면 재계획(Q1에서 하이브리드 채택 — routine 밖은 ReAct 유지).
- Reflexion식 무제한 자기교정(자율성은 `autonomy_mode`로 명시 제한).
- Tier-2 user memory 활성화(별건 — prod 테이블 마이그레이션 이슈).

## 6. 외부 연구 정합성

- Routine = Anthropic "Building Effective Agents"의 workflow(구조화) vs agent(자율) 하이브리드.
- Contract/verification gate = evaluator-optimizer 패턴의 경량 구현.
- autonomy_mode = Claude Code permission mode(ask/auto-accept) 구조 차용.
- taxonomy = 에이전트 오류 관측성/복구 연구 흐름과 정합. 선행 강점(guardrails·라우팅 recall
  정량화·목적별 토큰 회계)은 유지.
