# AIDE Agent v3 — Top-Down Agentic Architecture Design

> **설계 근거**: 2024-2025 agent 연구 및 프로덕션 사례 기반.
> Routine(구조화된 프로세스) + Tool-Calling Loop의 hybrid 구조.
> LangGraph 없이 plain Python으로 구현.

---

## 1. 현재 문제 (Why Redesign)

현재 파이프라인:
```
user input → concept_typer(LLM) → grounder(DB) → canonicalizer(LLM) → planner(dict) → executor → verifier
```

**실패 케이스**: `"4월 원티드 제출 안한사람 리스트업해봐"`
- "원티드" → lexical로 분류 → shift DB에서 UNRESOLVED
- "리스트업해봐" → workflow로 분류 → `_WORKFLOW_PATTERNS`에 없어서 UNRESOLVED
- planner → UNRESOLVED 존재 → BLOCKED (canonicalizer가 scope=wanted로 해석했는데도)

**근본 원인**: 문장을 조각내서 각각 ground → 전체 문맥 손실.

---

## 2. 설계 결정의 근거 (Research-Backed Decisions)

### 2.1 LLM Function Calling으로 Skill 선택 — 왜 맞는가

| 근거 | 출처 |
|---|---|
| GPT-4o/Claude 3.5가 단일 tool 선택 85-92% 정확도 | Berkeley Function-Calling Leaderboard (BFCL, 2024) |
| tool description 품질이 선택 정확도의 최대 결정 요인 | ToolLLM (Qin et al., 2024), Gorilla (Patil et al., 2024) |
| 9개 이하 tool은 semantic routing/classifier 없이 충분 | Anthropic "Tool Use Best Practices" — 15-20개 넘을 때만 분류 필요 |
| function calling이 text-to-SQL보다 안전하고 감사 가능 | DIN-SQL, MAC-SQL (2024) 비교 연구 |

**한국어 보완 (5-15% 정확도 하락 대응)**:
- tool description을 한국어로 작성
- 한국어 입력 → tool call 매핑 few-shot 예시 포함 (10-20% 정확도 향상, ToolBench/API-Bank)
- Negative examples로 오선택 방지 ("이 skill은 조회에는 사용하지 마세요")

### 2.2 LangGraph 없이 구현 — 왜 가능한가

| 근거 | 출처 |
|---|---|
| "대부분의 에이전트에 프레임워크 불필요" | Anthropic "Building Effective Agents" (2024.12) |
| 프로덕션 에이전트는 plain tool-calling loop 사용 | Stripe, Linear, Notion 사례 |
| OpenAI Agents SDK도 경량 패턴 | OpenAI Agents SDK (2025.03) |
| 에이전트 루프 자체는 ~50줄 코드 | 업계 공통 인식 |

**LangGraph의 핵심 기능과 우리의 대안**:

| LangGraph 기능 | 우리에게 필요? | 대안 |
|---|---|---|
| State management | 부분적 (session context) | `dict` — FastAPI request scope |
| Conditional routing | 불필요 | LLM이 tool_call로 직접 라우팅 |
| Checkpointing | 불필요 | request/response 패턴, 장기 실행 없음 |
| Human-in-the-loop | 필요 | clarification을 AgentResponse로 반환, 다음 request에서 이어감 |
| Streaming | 향후 | SSE로 직접 구현 가능 |

### 2.3 에이전트 루프 패턴 선택

| 패턴 | 특징 | 우리 케이스 적합성 |
|---|---|---|
| **ReAct** | Think-Act-Observe 인터리브 | 단순 작업 OK, 5+ step에서 에러 전파 |
| **Plan-and-Execute** | 계획 먼저 → 실행 분리 | 복잡 작업에 강하지만 over-engineering |
| **Tool-Calling Loop** | LLM → tool call → result → LLM → ... | **가장 적합** — 대부분 1-2 call로 끝남 |
| **LATS (Tree Search)** | MCTS + LLM | 토큰 비용 과다, 우리 규모에 불필요 |

**선택: Tool-Calling Loop** (ReAct의 경량 버전)
- 우리 도메인의 95% 요청은 1-2 tool call이면 충분
- 복합 요청 ("취소하고 대체해줘")도 2-3 turn이면 해결
- MAX_TURNS=6으로 무한 루프 방지

### 2.4 Routine 패턴 — 왜 추가하는가

| 근거 | 출처 |
|---|---|
| Plain tool-calling loop의 multi-step 정확도: GPT-4o 41.1% | Routine (Zeng et al., 2025, arXiv:2507.14447) |
| Routine 적용 후 GPT-4o: 96.3% (+55.2%p), Qwen3-14B: 83.3% (+50.7%p) | 동일 |
| 엔터프라이즈 도메인 프로세스 지식 부족이 핵심 실패 원인 | 동일 |
| 명시적 단계 구조 + Variable Memory로 파라미터 전달 → 실행 안정성 대폭 향상 | 동일 |

**핵심 인사이트**: Tool description만으로는 LLM이 "어떤 순서로 어떤 tool을 호출해야 하는지"를 안정적으로 판단하지 못한다. 특히 3+ step의 복합 쿼리에서 실패율이 급증한다.

**Routine이란**: 자주 반복되는 복합 작업을 **미리 정의된 step sequence**로 구조화한 것.
- 각 step에 사용할 tool, 필요한 입력, 출력 변수명을 명시
- Variable Memory로 step 간 파라미터 자동 전달 (step 1 결과 → step 2 입력)
- 분기(branching) 로직 포함 가능

**Hybrid 전략 (Routine + Plain Loop)**:
```
사용자 입력
    │
    ▼
[Routine Matcher] ── 입력이 알려진 패턴과 매치?
    │                       │
    ├─ YES ──→ [Routine Executor]  (구조화된 step-by-step, 96.3% 정확도)
    │           각 step의 tool이 미리 지정됨
    │           Variable Memory로 step 간 파라미터 전달
    │
    └─ NO ───→ [Plain Tool-Calling Loop]  (유연한 LLM 판단, novel 쿼리 대응)
                LLM이 자유롭게 tool 선택
```

- **Known complex patterns** (원티드 제출 현황 조회, 근무표 시프트 변경 등): Routine으로 처리 → 96.3% 정확도
- **Novel/simple queries** (단순 조회, 새로운 유형): Plain loop → 유연성 확보
- Routine은 LLM의 planning 부담을 제거하되, loop의 유연성을 포기하지 않는다

---

## 3. Architecture

### 3.1 전체 흐름 (Hybrid: Routine + Tool-Calling Loop)

```
User Input (한국어 자연어)
    │
    ▼
┌──────────────────────────────────────────┐
│  System Prompt                           │
│  ─ 역할, 컨텍스트 (병동, 기간, 역할)      │
│  ─ Domain Knowledge (데이터 모델, 규칙)   │
│  ─ ★ Routine Definitions (구조화된 패턴)  │
│  ─ Tool Descriptions (9 skills)          │
│  ─ Few-shot examples                     │
│  ─ Rules (preview 필수, 한국어 답변 등)    │
└──────────────┬───────────────────────────┘
               │
               ▼
┌──────────────────────────────────────────┐
│  [Routine Matcher] (LLM 판단)            │
│  "이 요청은 알려진 Routine에 해당하는가?"  │
│    │                                     │
│    ├─ YES → Routine Executor             │
│    │   step 1 → tool_call → result       │
│    │   Variable Memory에 저장             │
│    │   step 2 → tool_call(VM 참조) → ... │
│    │   finish → answer                   │
│    │                                     │
│    └─ NO → Plain Tool-Calling Loop       │
│        while turn < MAX_TURNS:           │
│          response = llm(messages, tools)  │
│          tool_call → execute → continue  │
│          text → answer → return          │
└──────────────────────────────────────────┘
```

> **Note**: Routine matching은 별도 분류기가 아닌, system prompt에 포함된 Routine 정의를 LLM이 읽고 판단하는 방식. 추가 API 호출 없음.

### 3.2 핵심 구현: Agent Loop (Routine-aware)

```python
class SchedulingAgent:
    """Hybrid agent: Routine executor + plain tool-calling loop."""
    
    MAX_TURNS = 6
    
    def __init__(self, llm_client: LLMClient):
        self.llm = llm_client
    
    def run(self, db: Session, user_message: str, ctx: SessionContext) -> AgentResult:
        messages = [
            {"role": "system", "content": self._system_prompt(ctx)},
            {"role": "user", "content": user_message},
        ]
        trace = []
        vm = VariableMemory()  # step 간 파라미터 전달
        
        for turn in range(self.MAX_TURNS):
            response = self.llm.chat(messages, tools=SKILL_TOOLS)
            
            if response.is_text:
                trace.append(Stage("answer", "ok", {"text": response.text}))
                return AgentResult(answer=response.text, trace=trace,
                                   messages=messages)
            
            if response.is_tool_call:
                skill_name = response.tool_name
                skill_args = response.tool_args
                
                # ── Variable Memory injection ──
                # Routine step이 VM 참조 시 자동 주입
                skill_args = vm.inject(skill_args)
                
                trace.append(Stage("planning", "ok", {
                    "skill": skill_name,
                    "args": skill_args,
                    "reasoning": response.thinking,
                }))
                
                # ── Middleware Pipeline으로 skill 실행 ──
                result = execute_skill(db, skill_name, skill_args, ctx)
                
                trace.append(Stage("execution",
                    "error" if _is_error(result.data) else "ok",
                    {"skill": skill_name, "result": _truncate(result.data),
                     "grounded_params": result.grounded_params},
                    result.duration_ms,
                ))
                
                # ── Variable Memory 저장 ──
                # 현재 step 결과를 VM에 저장 → 다음 step에서 참조 가능
                vm.store(skill_name, result.data)
                
                if _needs_clarification(result.data):
                    return AgentResult(
                        needs_clarification=True,
                        question=result.data["question"],
                        options=result.data.get("options", []),
                        trace=trace, messages=messages,
                    )
                
                if result.blocked:
                    return AgentResult(
                        answer=result.block_reason,
                        trace=trace, messages=messages,
                    )
                
                messages.append(response.as_assistant_message())
                messages.append({
                    "role": "tool",
                    "tool_call_id": response.tool_call_id,
                    "content": json.dumps(result.data, 
                                          ensure_ascii=False, default=str),
                })
        
        return AgentResult(
            answer="처리 단계가 너무 많습니다. 좀 더 구체적으로 말씀해 주세요.",
            trace=trace, messages=messages,
        )


@dataclass
class VariableMemory:
    """Routine step 간 파라미터 전달을 위한 경량 key-value 저장소.
    
    Routine paper의 핵심 개념: step 1의 출력을 step 2의 입력으로 자동 연결.
    예: query_schedule → nurse_ids → bulk_mutation에서 참조
    """
    _store: dict = field(default_factory=dict)
    
    def store(self, skill_name: str, result: Any):
        """skill 실행 결과에서 재사용 가능한 값을 추출하여 저장."""
        if isinstance(result, dict):
            # schedule_id, nurse_ids 등 핵심 ID는 자동 저장
            for key in ("schedule_id", "nurse_ids", "shift_codes", 
                        "wanted_request_ids", "entries"):
                if key in result:
                    self._store[key] = result[key]
            # skill별 결과를 통째로도 저장 (last_result.{skill_name})
            self._store[f"last_{skill_name}"] = result
    
    def inject(self, args: dict) -> dict:
        """args에서 $vm.{key} 참조를 실제 값으로 치환."""
        injected = {}
        for k, v in args.items():
            if isinstance(v, str) and v.startswith("$vm."):
                vm_key = v[4:]  # "$vm.nurse_ids" → "nurse_ids"
                injected[k] = self._store.get(vm_key, v)
            else:
                injected[k] = v
        return injected
    
    def get(self, key: str, default=None):
        return self._store.get(key, default)
```

### 3.3 LangGraph 구조를 코드로 가져오기

LangGraph의 유용한 패턴을 프레임워크 없이 구현:

**1) State = SessionContext (단순 dict)**
```python
@dataclass
class SessionContext:
    group_id: str
    office_id: str
    year: int
    month: int
    user_role: str
    nurse_id: str | None = None
    
    # 대화 히스토리 (multi-turn clarification용)
    conversation_id: str | None = None
    pending_approval: dict | None = None  # preview 결과 저장
```

**2) Conditional Routing = LLM의 tool_call 자체**
- LangGraph: node → conditional edge → next node
- 우리: LLM이 tool_call 반환 = 어떤 skill로 갈지 결정 완료
- routing logic이 LLM 내부에 있으므로 별도 graph 불필요

**3) Human-in-the-loop = Request/Response 경계**
```python
# Turn 1: 사용자 "김민지 4/5 D→E 변경해줘"
# Agent: bulk_mutation(preview_only=true) → preview 결과 반환
# → AgentResult(awaiting_approval=True, preview={...})

# Turn 2: 사용자 "응 진행해"
# Agent: messages에 이전 context + 승인 → bulk_mutation(preview_only=false)
```

**4) Error Recovery = LLM의 자연스러운 observe**
```python
# skill이 에러 반환 → messages에 에러 추가 → LLM이 다음 turn에서 판단
# LLM: "해당 간호사를 찾을 수 없습니다. 이름을 다시 확인해 주세요."
```

---

## 4. Routine Definitions (구조화된 프로세스)

> Routine paper (Zeng et al., 2025): 엔터프라이즈 에이전트의 핵심 문제는 **도메인 프로세스 지식 부족**.
> 자주 반복되는 복합 작업을 step sequence로 미리 정의하면 multi-step 정확도가 41.1% → 96.3%로 향상.

### 4.1 Routine 구조

각 Routine은 다음 필드를 갖는다:

```python
@dataclass
class RoutineStep:
    step: int                    # step 번호
    name: str                    # step 이름 (사람이 읽을 수 있는)
    tool: str                    # 호출할 skill name
    args_template: dict          # 파라미터 템플릿 ($vm.{key}로 이전 step 참조)
    type: str = "node"           # "node" (중간) | "finish" (마지막) | "branch" (분기)
    branch_condition: str = ""   # branch일 때: 분기 조건 설명 (LLM이 판단)
    branch_true: int = 0         # 조건 True일 때 다음 step
    branch_false: int = 0        # 조건 False일 때 다음 step
    output_keys: list[str] = field(default_factory=list)  # VM에 저장할 결과 키

@dataclass 
class Routine:
    id: str                      # "wanted_submission_query" 등
    description: str             # LLM이 매칭에 사용하는 설명
    trigger_examples: list[str]  # 이 Routine을 트리거하는 예시 입력
    steps: list[RoutineStep]
```

### 4.2 Routine Definitions (7개)

#### R1: wanted_submission_query — 원티드 제출 현황 조회
```
Trigger: "원티드 미제출자", "원티드 현황", "원티드 신청 내역"
Steps:
  1. [node]   query_schedule(scope="wanted_submissions", operation="count")
              → VM: {submitted_count, not_submitted_count, not_submitted_names}
  2. [finish] answer: 미제출자 명단 + 현황 요약
```

#### R2: wanted_status_check — 특정 간호사 원티드 상태 확인
```
Trigger: "김민지 원티드 했어?", "내 원티드 제출됐나?"
Steps:
  1. [node]   query_schedule(scope="wanted_submissions", nurse_name={name})
              → VM: {is_submitted, submission_details}
  2. [branch] is_submitted == True?
              → True: step 3 (제출 내역 표시)
              → False: step 4 (미제출 안내)
  3. [finish] answer: "{name}님은 제출 완료. 신청 시프트: ..."
  4. [finish] answer: "{name}님은 아직 미제출입니다."
```

#### R3: schedule_shift_change — 근무표 시프트 변경 (Human-in-the-loop)
```
Trigger: "김민지 4/5 D→E 변경", "{이름} {날짜} {시프트}를 {시프트}로"
Steps:
  1. [node]   query_schedule(scope="schedule", nurse_name={name}, date={date})
              → VM: {current_shift, schedule_id, entry_id}
  2. [branch] current_shift == expected_shift?
              → True: step 3 (preview)
              → False: step 5 (mismatch clarification)
  3. [node]   bulk_mutation(scope="schedule", action="change_shift", 
                           nurse_name={name}, date={date}, 
                           new_shift_name={new_shift}, preview_only=true)
              → VM: {preview_result}
  4. [finish] approval_request: preview 결과 표시 → 사용자 확인 대기
  5. [finish] clarification: "현재 {current_shift}인데 {new_shift}로 변경할까요?"
```

#### R4: night_worker_query — 야간 근무자 조회
```
Trigger: "야간 근무자 누구", "나이트 누가 들어가?"
Steps:
  1. [branch] LLM 판단: "야간 전담" vs "해당 기간 야간 배정"?
              → 모호: step 2 (clarification)
              → "배정": step 3
              → "전담": step 4
  2. [finish] clarification: "해당 기간에 야간을 근무한 사람인가요, 야간 전담 간호사인가요?"
  3. [node]   query_schedule(scope="schedule", shift_name="나이트", date_range={range})
              → VM: {night_workers}
              → [finish] answer: 야간 근무자 목록
  4. [node]   query_schedule(scope="nurse_info") + is_night_nurse 필터
              → VM: {night_dedicated_nurses}
              → [finish] answer: 야간 전담 간호사 목록
```

#### R5: grade_coverage_check — 경력 커버리지 확인
```
Trigger: "Grade 1이 빠지는 날", "고참이 없는 근무 있어?"
Steps:
  1. [node]   query_schedule(scope="schedule", date_range={range})
              → VM: {schedule_entries}
  2. [node]   query_schedule(scope="nurse_info")
              → VM: {nurse_grades}
  3. [finish] LLM이 schedule_entries × nurse_grades 교차 분석 → 커버리지 빈 날짜 답변
```

#### R6: schedule_fairness — 공정성 분석
```
Trigger: "야간 균형 분석", "간호사별 근무 수 비교", "불균형 분석"
Steps:
  1. [node]   analyze_report(scope="schedule", analysis_type="fairness")
              → VM: {fairness_report}
  2. [branch] 불균형 발견?
              → True: step 3 (조정 제안)
              → False: step 4 (균형 확인)
  3. [node]   repair_schedule() → VM: {repair_suggestions}
              → [finish] answer: 불균형 상세 + 조정 제안
  4. [finish] answer: "현재 근무표는 균형 상태입니다."
```

#### R7: multi_step_mutation — 복합 수정 (취소 + 대체 배정)
```
Trigger: "김민지 원티드 취소하고 이영희로 대체", "{A} 취소하고 {B}에 넣어줘"
Steps:
  1. [node]   bulk_mutation(scope="wanted_submissions", action="cancel",
                           nurse_name={A}, preview_only=true)
              → VM: {cancel_preview}
  2. [finish] approval_request: "1) {A} 원티드 취소, 2) {B} 배정 진행할까요?"
  — (사용자 승인 후 새 Routine 또는 plain loop로 실행 재개) —
  3. [node]   bulk_mutation(preview_only=false) → VM: {cancel_result}
  4. [node]   recommend_candidates(date={date}, shift_name={shift})
              → VM: {candidates}  (또는 직접 {B} 배정)
  5. [node]   bulk_mutation(scope="schedule", action="change_shift",
                           nurse_name={B}, preview_only=true)
              → VM: {assign_preview}
  6. [finish] approval_request: 배정 preview → 사용자 확인
```

### 4.3 System Prompt에 Routine 주입 방식

Routine 정의는 system prompt에 **자연어 형태**로 포함된다. LLM이 이를 읽고 매칭 여부를 판단.

```
## 알려진 처리 패턴 (Routines)

아래 패턴에 해당하는 요청은 정해진 단계를 따르세요.

### 원티드 제출 현황 조회
- 트리거: "원티드 미제출자", "원티드 현황"
- Step 1: query_schedule(scope="wanted_submissions", operation="count")
- Step 2: 결과를 바탕으로 미제출자 명단 답변

### 근무표 시프트 변경
- 트리거: "{이름} {날짜} {시프트}→{시프트}"
- Step 1: query_schedule로 현재 시프트 확인
- Step 2: 현재 시프트가 예상과 다르면 clarification
- Step 3: bulk_mutation(preview_only=true)로 미리보기
- Step 4: 사용자 확인 후 실행
...
```

> **핵심**: Routine은 코드가 아니라 **프롬프트 내 지시문**이다. LLM이 이를 참고하여 step-by-step으로 실행하되, 예외 상황에서는 자유롭게 벗어날 수 있다. 이것이 hard-coded workflow와의 차이.

### 4.4 Variable Memory의 역할

```
Step 1: query_schedule(scope="schedule", nurse_name="김민지", date="4/5")
        → result: {current_shift: "D", schedule_id: 42, entry_id: 1234}
        → VM에 자동 저장: schedule_id=42, current_shift="D"

Step 2: LLM이 current_shift == "D" 확인 (예상과 일치)

Step 3: bulk_mutation(schedule_id=$vm.schedule_id, entry_id=$vm.entry_id, ...)
        → VM에서 schedule_id, entry_id를 자동 주입
```

- Variable Memory는 **LLM의 context window에 의존하지 않고** 구조적으로 파라미터를 전달
- LLM의 "이전 tool 결과에서 값을 추출하여 다음 tool에 넘기는" 부담을 제거
- 특히 3+ step에서 중간 결과를 잃어버리는 문제를 방지

---

## 5. System Prompt 설계

### 5.1 System Prompt 구조 (순서)

```
[1] Role & Context — 역할, 병동, 기간, 사용자 정보
[2] Domain Knowledge — 데이터 모델, 접근 경로, 비즈니스 규칙 (DOMAIN_KNOWLEDGE.md)
[3] ★ Routine Definitions — 구조화된 복합 패턴 7개 (Section 4 참조)
[4] Tool Descriptions — 9개 skill JSON schema
[5] Few-shot Examples — 한국어 입력 → tool call 매핑
[6] Rules — preview 필수, 한국어 답변, clarification 규칙
```

### 5.2 Few-Shot Examples (정확도 10-20% 향상)

```python
FEW_SHOT_EXAMPLES = """
## 예시

사용자: "이번 달 원티드 신청 내역 보여줘"
→ query_schedule(scope="wanted_submissions", operation="list")

사용자: "원티드 미제출자 알려줘"  
→ query_schedule(scope="wanted_submissions", operation="count")

사용자: "김민지 4/5 D를 E로 바꿔줘"
→ bulk_mutation(scope="schedule", action="change_shift", nurse_name="김민지", date="4월 5일", new_shift_name="이브닝", preview_only=true)
"""
```

### 5.3 Chain-of-Thought 유도

```python
SYSTEM_RULES = """
규칙:
1. 사용자 요청을 먼저 분석하세요: 어떤 데이터가 필요한지, 어떤 작업인지 판단합니다.
2. 적절한 도구를 선택하고, 파라미터를 채웁니다.
   - 이름, 시프트명, 날짜는 사용자가 말한 그대로 전달하세요 (내부에서 자동 변환).
3. 데이터 변경 요청은 반드시 preview_only=true로 먼저 실행 → 결과 확인 → 사용자 승인 후 실행.
4. 조회 결과가 비어있으면, 왜 없는지 설명하세요.
5. 답변은 한국어로 간결하게. 표가 적절하면 마크다운 표를 사용하세요.
"""
```

---

## 6. Skill Descriptions (Tool Schema)

### 6.1 설계 원칙 (연구 기반)

| 원칙 | 근거 |
|---|---|
| 한국어로 작성 + 한국어 예시 | 비영어 입력 시 5-15% 정확도 하락 보완 (BFCL) |
| "When to use" 명시 | ToolBench/API-Bank: specificity가 정확도 결정 |
| "When NOT to use" 명시 | Negative examples로 오선택 방지 |
| 파라미터에 예시 포함 | 파라미터 filling 정확도 10-20% 향상 |
| enum으로 제한 가능한 것은 enum 사용 | structured output으로 hallucination 방지 |

### 6.2 9개 Skill Definitions

```python
SKILL_TOOLS = [
    {
        "name": "query_schedule",
        "description": (
            "병동의 근무 관련 데이터를 조회합니다.\n"
            "원티드(희망근무) 신청 내역, 원티드 조정판, 근무표, 간호사 정보, "
            "시프트 정의, 제약조건 설정 등을 조회할 때 사용합니다.\n\n"
            "⛔ 데이터 수정/삭제에는 사용하지 마세요. 읽기 전용입니다.\n\n"
            "예시:\n"
            "- '원티드 신청 내역 보여줘' → scope='wanted_submissions'\n"
            "- '원티드 미제출자 알려줘' → scope='wanted_submissions', operation='count'\n"
            "- '김민지 근무 조회' → scope='schedule', nurse_name='김민지'\n"
            "- '나이트 근무자 누구야?' → scope='schedule', shift_name='나이트'\n"
            "- '현재 설정값' → scope='constraint_config'"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": ["wanted_submissions", "wanted_adjustment", "schedule",
                             "nurse_info", "shift_definitions", "constraint_config",
                             "generation_job"],
                    "description": "조회 대상. 원티드=wanted_submissions, 근무표=schedule, 간호사=nurse_info",
                },
                "operation": {
                    "type": "string",
                    "enum": ["list", "count", "summarize"],
                    "description": "list=목록, count=현황/개수, summarize=요약. 기본값 list",
                },
                "nurse_name": {
                    "type": "string",
                    "description": "간호사 이름 (한글, 예: '김민지'). 내부에서 ID로 변환됨",
                },
                "shift_name": {
                    "type": "string",
                    "description": "시프트명 (예: '데이', '이브닝', '나이트', 'OFF'). 내부에서 코드로 변환됨",
                },
                "date": {
                    "type": "string",
                    "description": "특정 날짜 (예: '4월 5일', '2026-04-05')",
                },
                "date_range": {
                    "type": "string",
                    "description": "날짜 범위 (예: '4월 1일~10일', '이번 주')",
                },
            },
            "required": ["scope"],
        },
    },
    {
        "name": "bulk_mutation",
        "description": (
            "근무 데이터를 수정합니다. 원티드 취소, 조정판 수정, 근무표 시프트 변경 등.\n\n"
            "⚠️ 반드시 preview_only=true로 먼저 실행하고 사용자 확인 후 실제 수정하세요.\n"
            "⛔ 읽기 전용 조회에는 사용하지 마세요. query_schedule을 사용하세요.\n\n"
            "예시:\n"
            "- '김민지 원티드 취소' → scope='wanted_submissions', action='cancel'\n"
            "- '조정판 쉬는사람 해제' → scope='wanted_adjustment', action='unapply_off'\n"
            "- '김민지 4/5 D→E' → scope='schedule', action='change_shift'"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": ["wanted_submissions", "wanted_adjustment", "schedule"],
                },
                "action": {
                    "type": "string",
                    "enum": ["cancel", "approve", "reject", "unapply_off",
                             "apply", "change_shift", "add_shift", "remove_shift"],
                },
                "nurse_name": {"type": "string"},
                "shift_name": {"type": "string"},
                "new_shift_name": {"type": "string", "description": "변경할 새 시프트"},
                "date": {"type": "string"},
                "preview_only": {"type": "boolean", "default": True},
            },
            "required": ["scope", "action"],
        },
    },
    {
        "name": "validate_schedule",
        "description": (
            "근무표의 제약조건 위반을 검사합니다. 연속 야간, 최대 근무일, 경력 커버리지 등.\n\n"
            "예시: '근무표 검증해줘', '위반사항 뭐야?'"
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "recommend_candidates",
        "description": (
            "특정 날짜/시프트에 투입 가능한 대체 간호사를 추천합니다.\n"
            "해당 날짜에 비번인 간호사 중 자격 요건에 맞는 사람을 찾습니다.\n\n"
            "예시: '4월 5일 나이트 대체자 추천해줘'"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "대상 날짜"},
                "shift_name": {"type": "string", "description": "대상 시프트"},
                "exclude_nurse_name": {"type": "string", "description": "제외할 간호사"},
            },
            "required": ["date", "shift_name"],
        },
    },
    {
        "name": "repair_schedule",
        "description": (
            "근무표 불균형을 분석하고 조정 제안을 합니다. 직접 수정하지 않습니다.\n\n"
            "예시: '근무표 조정 제안해줘', '야간 균형 맞춰줘'"
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "analyze_report",
        "description": (
            "근무표/원티드 데이터를 분석하여 리포트를 생성합니다.\n"
            "공정성 분석, 시프트 분포, 간호사별 비교 등.\n\n"
            "예시: '야간 불균형 보여줘', '간호사별 근무 수 비교'"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "scope": {"type": "string", "enum": ["schedule", "wanted"]},
                "analysis_type": {"type": "string",
                    "enum": ["fairness", "distribution", "comparison", "headcount"]},
                "shift_name": {"type": "string"},
            },
        },
    },
    {
        "name": "update_constraint",
        "description": (
            "근무표 생성 제약조건을 수정합니다. 야간 최대 횟수, 연속 근무일 제한 등.\n\n"
            "⚠️ 다음 근무표 생성에 영향을 미칩니다.\n\n"
            "예시: '야간 최대 7회로 설정', '연속 근무 5일로 제한'"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "field": {"type": "string", "description": "설정 필드 (자연어 가능)"},
                "value": {"type": ["string", "number", "boolean"]},
                "preview_only": {"type": "boolean", "default": True},
            },
            "required": ["field", "value"],
        },
    },
    {
        "name": "update_person_attr",
        "description": (
            "간호사 개인 속성 수정. 직급, 팀, 경력, 야간 전담 등.\n\n"
            "예시: '김민지 직급 3으로 변경', '박지은 야간 요원 지정'"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "nurse_name": {"type": "string"},
                "field": {"type": "string"},
                "value": {"type": ["string", "number", "boolean"]},
                "preview_only": {"type": "boolean", "default": True},
            },
            "required": ["nurse_name", "field", "value"],
        },
    },
    {
        "name": "generate_schedule",
        "description": (
            "근무표 자동 생성 (비동기). 요청 후 작업 ID가 반환됩니다.\n\n"
            "⚠️ 고위험 작업. 반드시 사용자 확인 후 실행.\n\n"
            "예시: '4월 근무표 생성해줘'"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "confirm": {"type": "boolean", "default": False,
                    "description": "사용자 확인 여부. false면 확인 요청 반환"},
            },
        },
    },
]
```

---

## 7. LLM Client: Tool Calling 구현

### 7.1 추상 인터페이스

```python
@dataclass
class LLMResponse:
    """LLM 응답 — text 또는 tool_call."""
    type: Literal["text", "tool_call"]
    text: str | None = None
    tool_name: str | None = None
    tool_args: dict | None = None
    tool_call_id: str | None = None
    thinking: str | None = None  # chain-of-thought (if available)
    _raw: Any = None  # provider-specific raw response
    
    @property
    def is_text(self) -> bool:
        return self.type == "text"
    
    @property
    def is_tool_call(self) -> bool:
        return self.type == "tool_call"
    
    def as_assistant_message(self) -> dict:
        """Convert to message format for conversation history."""
        ...


class LLMClient(Protocol):
    """Provider-agnostic LLM interface with tool calling."""
    
    def chat(
        self,
        messages: list[dict],
        tools: list[dict],
        *,
        tool_choice: str = "auto",
    ) -> LLMResponse:
        ...
```

### 7.2 OpenAI 구현

```python
class OpenAIClient:
    def __init__(self, api_key: str, model: str = "gpt-4.1-mini"):
        self.client = openai.OpenAI(api_key=api_key)
        self.model = model
    
    def chat(self, messages, tools, *, tool_choice="auto") -> LLMResponse:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=[{"type": "function", "function": t} for t in tools],
            tool_choice=tool_choice,
            temperature=0.1,
            max_tokens=2048,
        )
        choice = response.choices[0]
        
        if choice.message.tool_calls:
            tc = choice.message.tool_calls[0]
            return LLMResponse(
                type="tool_call",
                tool_name=tc.function.name,
                tool_args=json.loads(tc.function.arguments),
                tool_call_id=tc.id,
                _raw=choice.message,
            )
        return LLMResponse(type="text", text=choice.message.content)
```

### 7.3 Anthropic 구현

```python
class AnthropicClient:
    def __init__(self, api_key: str, model: str = "claude-haiku-4-5-20251001"):
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model
    
    def chat(self, messages, tools, *, tool_choice="auto") -> LLMResponse:
        # Anthropic: system은 별도, messages에서 분리
        system_msg = ""
        conv_messages = []
        for m in messages:
            if m["role"] == "system":
                system_msg = m["content"]
            else:
                conv_messages.append(m)
        
        anthropic_tools = [
            {"name": t["name"], "description": t["description"],
             "input_schema": t["parameters"]}
            for t in tools
        ]
        
        response = self.client.messages.create(
            model=self.model,
            system=system_msg,
            messages=conv_messages,
            tools=anthropic_tools,
            max_tokens=2048,
            temperature=0.1,
        )
        
        for block in response.content:
            if block.type == "tool_use":
                return LLMResponse(
                    type="tool_call",
                    tool_name=block.name,
                    tool_args=block.input,
                    tool_call_id=block.id,
                    _raw=response,
                )
        
        text = next((b.text for b in response.content if b.type == "text"), "")
        return LLMResponse(type="text", text=text)
```

### 7.4 Deterministic Client (테스트용)

```python
class DeterministicClient:
    """API 호출 없이 예측 가능한 tool calling 시뮬레이션."""
    
    def chat(self, messages, tools, **kwargs) -> LLMResponse:
        user_msg = _last_user_message(messages)
        has_tool_result = _has_tool_result(messages)
        
        # tool result가 있으면 → 답변 생성
        if has_tool_result:
            result = _last_tool_result(messages)
            return LLMResponse(type="text", text=self._format_answer(result))
        
        # 패턴 기반 tool 선택 (테스트 전용, 프로덕션에서는 실제 LLM)
        return self._route(user_msg)
    
    def _route(self, msg: str) -> LLMResponse:
        if "원티드" in msg:
            if any(w in msg for w in ["미제출", "현황", "제출"]):
                return LLMResponse(type="tool_call", tool_name="query_schedule",
                    tool_args={"scope": "wanted_submissions", "operation": "count"})
            if "취소" in msg:
                return LLMResponse(type="tool_call", tool_name="bulk_mutation",
                    tool_args={"scope": "wanted_submissions", "action": "cancel",
                               "nurse_name": _extract_name(msg)})
            return LLMResponse(type="tool_call", tool_name="query_schedule",
                tool_args={"scope": "wanted_submissions"})
        
        if any(w in msg for w in ["근무표", "근무", "스케줄"]):
            if "생성" in msg:
                return LLMResponse(type="tool_call", tool_name="generate_schedule",
                    tool_args={"confirm": False})
            if "검증" in msg or "위반" in msg:
                return LLMResponse(type="tool_call", tool_name="validate_schedule",
                    tool_args={})
            return LLMResponse(type="tool_call", tool_name="query_schedule",
                tool_args={"scope": "schedule", "nurse_name": _extract_name(msg)})
        
        return LLMResponse(type="text", text="요청을 이해하지 못했습니다.")
```

---

## 8. Internal Grounding (Skill 내부)

### 8.1 공통 Resolve 함수

```python
# agents_v2/grounding/internal.py

@dataclass
class ResolveResult:
    resolved: bool = False
    value: Any = None
    needs_clarification: bool = False
    question: str | None = None
    options: list[str] | None = None
    error: str | None = None
    
    def to_clarification_dict(self) -> dict:
        return {"needs_clarification": True,
                "question": self.question, "options": self.options or []}


def resolve_nurse(db, group_id, name: str) -> ResolveResult:
    """간호사 이름 → nurse_id. 동명이인 시 clarification."""
    exact = nurse_tools.find_nurse_by_name(db, group_id, name)
    if len(exact) == 1:
        return ResolveResult(resolved=True, value=exact[0]["nurse_id"])
    if len(exact) > 1:
        return ResolveResult(needs_clarification=True,
            question=f"'{name}'에 해당하는 간호사가 {len(exact)}명입니다:",
            options=[f"{n['name']} ({n.get('team_name', '')})" for n in exact])
    # fuzzy search
    fuzzy = nurse_tools.fuzzy_search_nurse(db, group_id, name)
    if fuzzy:
        return ResolveResult(needs_clarification=True,
            question=f"'{name}'을 찾을 수 없습니다. 혹시:",
            options=[n["name"] for n in fuzzy[:5]])
    return ResolveResult(error=f"'{name}' 간호사를 찾을 수 없습니다.")


def resolve_shift(db, group_id, name: str) -> ResolveResult:
    """시프트명 → shift_id. 별칭 매핑 포함."""
    ALIASES = {
        "데이": "D", "주간": "D", "낮": "D",
        "이브닝": "E", "초번": "E",
        "나이트": "N", "야간": "N", "밤": "N",
        "오프": "OFF", "비번": "OFF",
        "연차": "V", "휴가": "V", "주휴": "WO",
    }
    shifts = shift_tools.read_shift_definitions(db, group_id)
    # 직접 매치
    for s in shifts:
        if name.upper() == s["shift_code"] or name == s.get("shift_name"):
            return ResolveResult(resolved=True, value=s["shift_id"])
    # 별칭 매치
    code = ALIASES.get(name)
    if code:
        for s in shifts:
            if code == s["shift_code"]:
                return ResolveResult(resolved=True, value=s["shift_id"])
    return ResolveResult(error=f"'{name}' 시프트를 찾을 수 없습니다.")


def resolve_date(text: str, year: int, month: int) -> str | None:
    """자연어 날짜 → YYYY-MM-DD. 포맷 변환만 수행 (intent 파싱 아님)."""
    import re
    from datetime import datetime, timedelta
    if re.match(r"\d{4}-\d{2}-\d{2}", text):
        return text
    m = re.match(r"(\d{1,2})월\s*(\d{1,2})일", text)
    if m:
        return f"{year}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    m = re.match(r"(\d{1,2})일", text)
    if m:
        return f"{year}-{month:02d}-{int(m.group(1)):02d}"
    today = datetime.now()
    if "오늘" in text: return today.strftime("%Y-%m-%d")
    if "내일" in text: return (today + timedelta(days=1)).strftime("%Y-%m-%d")
    return None
```

### 8.2 Skill에 Grounding 통합 패턴

```python
def _ground_params(db, group_id, params: dict) -> dict | None:
    """공통 grounding. clarification dict 반환 시 skill은 바로 return."""
    if params.get("nurse_name"):
        r = resolve_nurse(db, group_id, params["nurse_name"])
        if r.needs_clarification: return r.to_clarification_dict()
        if r.error: return {"error": r.error}
        params["nurse_ids"] = [r.value]
    
    if params.get("shift_name"):
        r = resolve_shift(db, group_id, params["shift_name"])
        if r.resolved: params["shift_codes"] = [r.value]
    
    if params.get("date"):
        resolved = resolve_date(params["date"], params.get("year"), params.get("month"))
        if resolved: params["date"] = resolved
    
    return None  # grounding 성공, clarification 없음


@register("query-schedule")
def query_schedule(db: Session, params: dict) -> Any:
    group_id = params["group_id"]
    
    # grounding
    clarification = _ground_params(db, group_id, params)
    if clarification:
        return clarification
    
    # 기존 비즈니스 로직 그대로
    scope = params.get("scope", "")
    if scope == "wanted_submissions":
        return _query_wanted_submissions(db, group_id, ...)
    # ...
```

---

## 9. Test Chat UI 연동

### 9.1 Stage 표시 (v2 → v3)

| v2 Stage | v3 Stage | 설명 |
|---|---|---|
| concept_typing | (제거) | LLM이 전체 의도를 한번에 파악 |
| grounding | (skill 내부) | 별도 stage 없음 |
| canonicalize | (제거) | LLM이 scope/operation 직접 선택 |
| planning | planning (turn별) | LLM의 tool 선택 + 파라미터 |
| execution | execution (turn별) | skill 실행 결과 |
| verification | (LLM observe) | LLM이 결과 보고 판단 |
| answer_generation | answer | 최종 답변 |

### 9.2 UI에서 표시되는 trace 예시

```
[Turn 1]
├─ 🧠 Planning: query_schedule(scope="wanted_submissions", operation="count")
└─ ⚡ Execution: {submitted: 15, not_submitted: 3, names: ["홍길동", "이순신", "강감찬"]}

[Turn 2]  
└─ 💬 Answer: "4월 원티드 미제출자 3명입니다: 홍길동, 이순신, 강감찬"
```

---

## 10. 마이그레이션

### Phase 1: Foundation (새 파일 추가, 기존 코드 안 건드림)
| 파일 | 내용 |
|---|---|
| `agents_v2/llm_client.py` | LLMResponse, LLMClient protocol, OpenAI/Anthropic/Deterministic 구현 |
| `agents_v2/skills/descriptions.py` | SKILL_TOOLS (9개 tool schema) |
| `agents_v2/grounding/internal.py` | resolve_nurse, resolve_shift, resolve_date, _ground_params |
| `agents_v2/agent_v3.py` | SchedulingAgent (tool-calling loop) |
| `agents_v2/debug_agent_v3.py` | DebugSchedulingAgent (trace 포함) |

### Phase 2: Skill 수정 (internal grounding 추가)
- 각 skill에 `_ground_params()` 호출 추가
- 기존 비즈니스 로직은 변경 없음

### Phase 3: Router + UI 연결
- `test_chat_router.py` → v3 agent 사용
- `agent_test_chat.html` → turn 기반 stage 표시

### Phase 4: v2 코드 정리
- `core/concept_typer.py`, `core/canonicalizer.py`, `core/planner.py` 제거
- `grounding/dispatcher.py`, `grounding/*_grounder.py` 5개 제거

---

## 11. 실패 케이스 검증

### "4월 원티드 제출 안한사람 리스트업해봐"

```
v2 (실패): "원티드" → lexical → UNRESOLVED → BLOCKED
v3 (성공): LLM → query_schedule(scope="wanted_submissions", operation="count") → 결과 → 답변
```

### "김민지 원티드 취소하고 이영희 4/5 나이트에 넣어줘"

```
Turn 1: bulk_mutation(scope="wanted_submissions", action="cancel", nurse_name="김민지", preview_only=true)
        → {preview: true, target: "김민지 4월 원티드"}
Turn 2: LLM observes preview → "취소 진행할까요? 또한 이영희 4/5 나이트 배정도 필요합니다."
Turn 3: (사용자 확인 후) bulk_mutation(preview_only=false) + bulk_mutation(schedule, change_shift, ...)
```

### "나이트 근무자 누구야?"

```
Turn 1: query_schedule(scope="schedule", shift_name="나이트")
        → skill 내부: resolve_shift("나이트") → N → 조회
Turn 2: LLM formats answer as markdown table
```

---

## 12. References

| # | 논문/문서 | 적용 부분 |
|---|---|---|
| 1 | **Routine: A Structural Planning Framework for LLM Agent System in Enterprise** (Zeng et al., 2025, arXiv:2507.14447) | Section 2.4, 4 — Routine 패턴 도입, Variable Memory, 구조화된 step sequence. GPT-4o 41.1%→96.3% 근거 |
| 2 | **Berkeley Function-Calling Leaderboard (BFCL)** (2024) | Section 2.1 — LLM function calling 정확도 85-92% 근거, 한국어 5-15% 하락 보완 전략 |
| 3 | **ToolLLM** (Qin et al., 2024) | Section 2.1, 6.1 — Tool description 품질이 선택 정확도의 최대 결정 요인 |
| 4 | **Gorilla: Large Language Model Connected with Massive APIs** (Patil et al., 2024) | Section 2.1 — API 선택 정확도 벤치마크, description 설계 원칙 |
| 5 | **Anthropic "Building Effective Agents"** (2024.12) | Section 2.2 — "대부분의 에이전트에 프레임워크 불필요", plain loop 패턴 정당화 |
| 6 | **OpenAI Agents SDK** (2025.03) | Section 2.2 — 경량 에이전트 패턴 프로덕션 사례 |
| 7 | **ToolBench / API-Bank** | Section 5.2 — Few-shot 예시 포함 시 정확도 10-20% 향상 근거 |
| 8 | **DIN-SQL, MAC-SQL** (2024) | Section 2.1 — Function calling vs text-to-SQL 안전성/감사 가능성 비교 |
