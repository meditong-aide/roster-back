# AIDE Agent v3 — Harness & Middleware Design

> Agent loop과 Skill 사이를 연결하는 인프라 레이어 설계.
> v2 harness에서 재사용 가능한 것은 유지, 나머지는 신규.

---

## 1. 전체 구조

```
HTTP Request (POST /agent/test/chat)
    │
    ▼
┌─ test_chat_router.py ─────────────────────────┐
│  1. Session Context 구성                       │
│  2. Conversation State 복원 (이전 messages)     │
│  3. Agent 호출                                 │
└───────────┬────────────────────────────────────┘
            │
            ▼
┌─ SchedulingAgent ──────────────────────────────┐
│                                                │
│  ┌─ [A] Prompt Builder (harness) ─────────┐   │
│  │  System prompt 조립:                    │   │
│  │  ① Role & Context                      │   │
│  │  ② Domain Knowledge                    │   │
│  │  ③ ★ Routine Definitions (패턴 7개)    │   │
│  │  ④ Tool Descriptions (9 skills)        │   │
│  │  ⑤ Few-shot Examples                   │   │
│  │  ⑥ Response Rules                      │   │
│  └────────────────────────────────────────┘   │
│                                                │
│  ┌─ [B] Tool-Calling Loop ────────────────┐   │
│  │  while turn < MAX_TURNS:               │   │
│  │    response = llm.chat(messages, tools) │   │
│  │    if tool_call:                        │   │
│  │      result = middleware.execute(...)   │──│──→ [C] Middleware Pipeline
│  │      messages.append(result)            │   │
│  │    else:                                │   │
│  │      return answer                      │   │
│  └────────────────────────────────────────┘   │
│                                                │
└───────────┬────────────────────────────────────┘
            │
            ▼
┌─ AgentResult ──────────────────────────────────┐
│  answer / clarification / approval_request     │
│  + trace (for debug UI)                        │
│  + updated messages (for conversation state)   │
└────────────────────────────────────────────────┘
```

### [C] Middleware Pipeline (Skill 실행 전후)

```
LLM tool_call(skill_name, args)
    │
    ▼
┌─ ① Permission Check ──────────────────┐
│  user_role != HN → mutation 차단       │
│  자기 데이터만 수정 가능 검증           │
└───────────┬────────────────────────────┘
            │
            ▼
┌─ ② Context Injection ─────────────────┐
│  args에 group_id, year, month 주입     │
│  "나/내" → session의 nurse_id 매핑     │
└───────────┬────────────────────────────┘
            │
            ▼
┌─ ③ Internal Grounding ────────────────┐
│  nurse_name → nurse_id                 │
│  shift_name → shift_code               │
│  date/date_range → YYYY-MM-DD          │
│  (clarification 발생 시 바로 반환)      │
└───────────┬────────────────────────────┘
            │
            ▼
┌─ ④ Skill Execution ───────────────────┐
│  run_skill(db, skill_name, params)     │
└───────────┬────────────────────────────┘
            │
            ▼
┌─ ⑤ Trace Capture ─────────────────────┐
│  stage name, status, data, duration    │
│  → debug UI에서 표시                    │
└────────────────────────────────────────┘
```

---

## 2. 각 컴포넌트 상세 설계

### [A] Prompt Builder

**파일**: `agents_v2/harness/prompt_builder.py`

```python
"""System prompt 조립 — domain knowledge + tools + examples."""

from pathlib import Path

_AIDE_DIR = Path(__file__).resolve().parent.parent.parent.parent / ".aide"


def build_system_prompt(ctx: SessionContext) -> str:
    """조립 순서: Role → Domain Knowledge → Tool Descriptions → Examples → Rules"""
    
    parts = [
        _build_role_section(ctx),
        _load_domain_knowledge(ctx),
        _build_routine_definitions(),    # ★ NEW: Routine 패턴 주입
        _build_tool_descriptions_section(),
        _build_few_shot_section(),
        _build_rules_section(ctx),
    ]
    return "\n\n---\n\n".join(p for p in parts if p)


def _build_role_section(ctx: SessionContext) -> str:
    return f"""당신은 병원 간호사 근무 스케줄링 AI 어시스턴트입니다.

현재 컨텍스트:
- 병원: {ctx.office_id}
- 병동: {ctx.group_id} ({ctx.group_name or ''})
- 기간: {ctx.year}년 {ctx.month}월
- 오늘 날짜: {ctx.today}
- 현재 사용자: {ctx.nurse_name or '알 수 없음'} ({ctx.user_role})
- 사용자 nurse_id: {ctx.nurse_id or '없음'}"""


def _load_domain_knowledge(ctx: SessionContext) -> str:
    """DOMAIN_KNOWLEDGE.md 로딩 + 컨텍스트 치환."""
    path = _AIDE_DIR / "DOMAIN_KNOWLEDGE.md"
    if not path.exists():
        return ""
    content = path.read_text(encoding="utf-8")
    # 동적 placeholder 치환
    content = content.replace("{year}", str(ctx.year))
    content = content.replace("{month}", str(ctx.month))
    content = content.replace("{group_id}", ctx.group_id or "?")
    return content


def _build_routine_definitions() -> str:
    """Routine 패턴을 자연어로 system prompt에 주입.
    
    Routine paper (Zeng et al., 2025): 구조화된 step sequence를 
    프롬프트에 포함하면 multi-step 정확도 41.1% → 96.3%.
    DOMAIN_KNOWLEDGE.md의 Section 5에 정의된 패턴을 로딩.
    """
    path = _AIDE_DIR / "DOMAIN_KNOWLEDGE.md"
    if not path.exists():
        return ""
    content = path.read_text(encoding="utf-8")
    # Section 5 (Routines) 추출
    marker = "## 5. 복합 쿼리 처리 패턴 (Routines)"
    idx = content.find(marker)
    if idx == -1:
        return ""
    routine_section = content[idx:]
    return f"""## 알려진 처리 패턴 (Routines)

아래 패턴에 해당하는 요청은 정해진 단계를 따르세요.
패턴에 해당하지 않는 새로운 유형의 요청은 자유롭게 도구를 선택하여 처리하세요.

{routine_section}"""


def _build_tool_descriptions_section() -> str:
    """Tool descriptions를 사람이 읽을 수 있는 텍스트로 변환.
    
    Note: 실제 JSON schema는 LLM API의 tools 파라미터로 별도 전달.
    여기서는 system prompt에 보조 설명만 포함.
    """
    from agents_v2.skills.descriptions import SKILL_TOOLS
    
    lines = ["## 사용 가능한 도구\n"]
    for tool in SKILL_TOOLS:
        lines.append(f"### {tool['name']}")
        lines.append(tool["description"])
        lines.append("")
    return "\n".join(lines)


def _build_few_shot_section() -> str:
    """한국어 입력 → tool call 매핑 예시."""
    return """## 처리 예시

사용자: "이번 달 원티드 신청 내역 보여줘"
→ query_schedule(scope="wanted_submissions")

사용자: "원티드 미제출자 알려줘"
→ query_schedule(scope="wanted_submissions", operation="count")

사용자: "김민지 4/5 D를 E로 바꿔줘"
→ 먼저 query_schedule로 현재 시프트 확인 → 일치하면 bulk_mutation(preview_only=true) → 사용자 확인 → 실행

사용자: "야간 근무자 누구야?"
→ 먼저 clarification: "해당 기간에 야간을 근무한 사람인가요, 야간 전담 간호사인가요?"
→ 응답에 따라 query_schedule(scope="schedule", shift_name="나이트") 또는 query_schedule(scope="nurse_info") + is_night_nurse 필터"""


def _build_rules_section(ctx: SessionContext) -> str:
    role_rules = ""
    if ctx.user_role != "HN":
        role_rules = """
- ⛔ 다른 간호사의 데이터를 수정할 수 없습니다. 수정 요청 시 거부하세요.
- 자기 데이터 수정만 가능합니다."""
    
    return f"""## 응답 규칙

1. 사용자 요청을 전체적으로 분석한 후, 적절한 도구를 선택하세요.
2. 이름, 시프트명, 날짜는 사용자가 말한 그대로 파라미터에 전달하세요.
3. "나", "내", "제" = 현재 사용자 ({ctx.nurse_name}, nurse_id={ctx.nurse_id}).
4. 데이터 수정은 반드시 preview_only=true → 사용자 확인 → 실행.
5. 근무표 조회 시 마감 근무표(IssuedRoster) 우선, 없으면 최신 버전.
6. 모호한 요청은 clarification 먼저 (Domain Knowledge의 clarification 트리거 참조).
7. 답변은 한국어, 간결하게. 표가 적절하면 마크다운 표 사용.
8. 조회 결과가 없으면 왜 없는지 설명하세요.{role_rules}"""
```

### [B] Session Context

**파일**: `agents_v2/schemas/session_context.py`

```python
"""Session context — HTTP request에서 agent까지 전달되는 상태."""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date


@dataclass
class SessionContext:
    # 필수 — 로그인 시 선택
    office_id: str
    group_id: str
    year: int
    month: int
    
    # 사용자 정보
    nurse_id: str | None = None
    nurse_name: str | None = None
    user_role: str = "nurse"  # "HN" | "nurse"
    group_name: str | None = None
    
    # 자동 계산
    today: str = field(default_factory=lambda: date.today().isoformat())
    
    # 대화 상태 (multi-turn)
    conversation_id: str | None = None
    messages: list[dict] = field(default_factory=list)
    
    # 승인 대기 (preview → confirm 플로우)
    pending_approval: dict | None = None
    
    # Variable Memory (Routine step 간 파라미터 전달)
    # Routine paper: step 1 결과 → step 2 입력으로 자동 연결
    variable_memory: dict = field(default_factory=dict)
```

### [C] Middleware Pipeline

**파일**: `agents_v2/middleware.py`

```python
"""Middleware pipeline — skill 실행 전후 처리."""

from __future__ import annotations
import time
import json
from typing import Any

from sqlalchemy.orm import Session

from agents_v2.schemas.session_context import SessionContext
from agents_v2.grounding.internal import (
    resolve_nurse, resolve_shift, resolve_date, resolve_date_range,
)
from agents_v2.skills.registry import run_skill


@dataclass
class SkillResult:
    """Skill 실행 결과 + 메타데이터."""
    data: Any
    duration_ms: float
    skill_name: str
    grounded_params: dict  # grounding 후 실제 사용된 params
    blocked: bool = False
    block_reason: str | None = None


def execute_skill(
    db: Session,
    skill_name: str,
    args: dict,
    ctx: SessionContext,
    *,
    trace: list | None = None,
) -> SkillResult:
    """Middleware pipeline을 거쳐 skill 실행."""
    
    t0 = time.time()
    
    # ── ① Permission Check ──
    block = _check_permission(skill_name, args, ctx)
    if block:
        return SkillResult(
            data={"error": block, "permission_denied": True},
            duration_ms=0, skill_name=skill_name,
            grounded_params=args, blocked=True, block_reason=block,
        )
    
    # ── ② Context Injection ──
    args = _inject_context(args, ctx)
    
    # ── ③ Internal Grounding ──
    clarification = _ground_params(db, ctx.group_id, args)
    if clarification:
        dt = (time.time() - t0) * 1000
        return SkillResult(
            data=clarification,
            duration_ms=dt, skill_name=skill_name,
            grounded_params=args,
        )
    
    # ── ④ Skill Execution ──
    try:
        result = run_skill(db, skill_name, args)
    except Exception as e:
        result = {"error": f"Skill execution error: {str(e)}"}
    
    dt = (time.time() - t0) * 1000
    
    return SkillResult(
        data=result,
        duration_ms=dt,
        skill_name=skill_name,
        grounded_params=args,
    )


# ── Permission Check ──

_MUTATION_SKILLS = {"bulk_mutation", "bulk-mutation", "update_constraint", 
                    "update-constraint", "update_person_attr", "update-person-attr",
                    "generate_schedule", "generate-schedule"}

def _check_permission(skill_name: str, args: dict, ctx: SessionContext) -> str | None:
    """권한 검사. 위반 시 에러 메시지 반환."""
    normalized = skill_name.replace("-", "_")
    
    # 일반 간호사의 타인 데이터 수정 차단
    if normalized in _MUTATION_SKILLS and ctx.user_role != "HN":
        nurse_name = args.get("nurse_name", "")
        if nurse_name and nurse_name != ctx.nurse_name and nurse_name not in ("나", "내", "제"):
            return f"다른 간호사({nurse_name})의 데이터를 수정할 권한이 없습니다."
    
    return None


# ── Context Injection ──

def _inject_context(args: dict, ctx: SessionContext) -> dict:
    """session context 값을 skill params에 주입."""
    args = {**args}  # shallow copy
    
    # 필수 context
    args.setdefault("group_id", ctx.group_id)
    args.setdefault("year", ctx.year)
    args.setdefault("month", ctx.month)
    
    # "나/내/제" → 현재 사용자
    if args.get("nurse_name") in ("나", "내", "제", "본인"):
        args["nurse_name"] = ctx.nurse_name
        args["nurse_ids"] = [ctx.nurse_id] if ctx.nurse_id else []
    
    # month override (LLM이 다른 월을 지정한 경우)
    if "target_month" in args:
        args["month"] = args.pop("target_month")
    
    return args


# ── Internal Grounding ──

def _ground_params(db, group_id: str, params: dict) -> dict | None:
    """공통 grounding. clarification dict 반환 시 skill 실행 중단."""
    
    # nurse_name → nurse_id
    if params.get("nurse_name") and not params.get("nurse_ids"):
        r = resolve_nurse(db, group_id, params["nurse_name"])
        if r.needs_clarification:
            return r.to_clarification_dict()
        if r.error:
            return {"error": r.error}
        if r.resolved:
            params["nurse_ids"] = [r.value]
    
    # shift_name → shift_codes
    if params.get("shift_name") and not params.get("shift_codes"):
        r = resolve_shift(db, group_id, params["shift_name"])
        if r.resolved:
            params["shift_codes"] = [r.value]
        # shift 못 찾아도 에러는 아님 — LLM이 다시 판단
    
    # new_shift_name → new_shift_code (mutation용)
    if params.get("new_shift_name"):
        r = resolve_shift(db, group_id, params["new_shift_name"])
        if r.resolved:
            params["new_shift_code"] = r.value
    
    # date → YYYY-MM-DD
    if params.get("date") and not _is_iso_date(params["date"]):
        resolved = resolve_date(
            params["date"], params.get("year", 2026), params.get("month", 1)
        )
        if resolved:
            params["date"] = resolved
    
    # date_range → start/end
    if params.get("date_range"):
        start, end = resolve_date_range(
            params["date_range"], params.get("year", 2026), params.get("month", 1)
        )
        if start and end:
            params["date_range_start"] = start
            params["date_range_end"] = end
    
    return None  # grounding 성공


def _is_iso_date(s: str) -> bool:
    import re
    return bool(re.match(r"\d{4}-\d{2}-\d{2}", s))
```

### [D] Conversation State Manager

**파일**: `agents_v2/conversation.py`

```python
"""Conversation state — multi-turn 대화 상태 관리.

HTTP request 간 messages 히스토리를 유지.
FastAPI 서버 메모리에 저장 (프로덕션에서는 Redis 등으로 교체 가능).
"""

from __future__ import annotations
import uuid
import time
from dataclasses import dataclass, field
from threading import Lock


@dataclass
class Conversation:
    id: str
    messages: list[dict] = field(default_factory=list)
    pending_approval: dict | None = None
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)


class ConversationStore:
    """In-memory conversation store. Thread-safe."""
    
    TTL = 3600  # 1시간 후 만료
    
    def __init__(self):
        self._store: dict[str, Conversation] = {}
        self._lock = Lock()
    
    def create(self) -> Conversation:
        conv = Conversation(id=str(uuid.uuid4()))
        with self._lock:
            self._store[conv.id] = conv
        return conv
    
    def get(self, conv_id: str) -> Conversation | None:
        with self._lock:
            conv = self._store.get(conv_id)
            if conv and (time.time() - conv.last_active) > self.TTL:
                del self._store[conv_id]
                return None
            if conv:
                conv.last_active = time.time()
            return conv
    
    def get_or_create(self, conv_id: str | None) -> Conversation:
        if conv_id:
            conv = self.get(conv_id)
            if conv:
                return conv
        return self.create()
    
    def save_messages(self, conv_id: str, messages: list[dict]):
        conv = self.get(conv_id)
        if conv:
            conv.messages = messages
    
    def set_pending_approval(self, conv_id: str, preview: dict | None):
        conv = self.get(conv_id)
        if conv:
            conv.pending_approval = preview
    
    def cleanup_expired(self):
        """만료된 대화 정리 (주기적 호출)."""
        now = time.time()
        with self._lock:
            expired = [k for k, v in self._store.items() 
                       if now - v.last_active > self.TTL]
            for k in expired:
                del self._store[k]


# 싱글톤
conversation_store = ConversationStore()
```

### [E] 통합: Agent에서 Middleware 호출

**파일**: `agents_v2/agent_v3.py` (수정)

```python
class SchedulingAgent:
    MAX_TURNS = 6
    
    def __init__(self, llm_client: LLMClient):
        self.llm = llm_client
    
    def run(self, db: Session, user_message: str, ctx: SessionContext) -> AgentResult:
        # ── Prompt 조립 (harness) ──
        system_prompt = build_system_prompt(ctx)
        
        # ── 대화 복원 (이전 messages가 있으면 이어감) ──
        if ctx.messages:
            messages = ctx.messages + [{"role": "user", "content": user_message}]
        else:
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ]
        
        # ── 승인 대기 처리 ──
        if ctx.pending_approval and _is_confirmation(user_message):
            return self._execute_approval(db, ctx)
        
        tools = SKILL_TOOLS
        trace = []
        
        for turn in range(self.MAX_TURNS):
            # LLM 호출
            t0 = time.time()
            response = self.llm.chat(messages, tools=tools)
            llm_ms = (time.time() - t0) * 1000
            
            if response.is_text:
                trace.append(Stage("answer", "ok", 
                    {"text": response.text}, llm_ms))
                return AgentResult(
                    answer=response.text,
                    trace=trace,
                    messages=messages,  # 대화 상태 저장용
                )
            
            if response.is_tool_call:
                trace.append(Stage("planning", "ok", {
                    "skill": response.tool_name,
                    "args": response.tool_args,
                }, llm_ms))
                
                # ── Middleware Pipeline으로 skill 실행 ──
                result = execute_skill(
                    db, response.tool_name, response.tool_args, ctx,
                )
                
                trace.append(Stage("execution",
                    "error" if _is_error(result.data) else "ok",
                    {"skill": response.tool_name, 
                     "result": _truncate(result.data),
                     "grounded_params": result.grounded_params},
                    result.duration_ms,
                ))
                
                # Grounding clarification?
                if _needs_clarification(result.data):
                    return AgentResult(
                        needs_clarification=True,
                        question=result.data["question"],
                        options=result.data.get("options", []),
                        trace=trace,
                        messages=messages,
                    )
                
                # Permission blocked?
                if result.blocked:
                    return AgentResult(
                        answer=result.block_reason,
                        trace=trace,
                        messages=messages,
                    )
                
                # messages에 추가 → 다음 turn
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
```

### [F] Router에서의 통합

**파일**: `agents_v2/test_chat_router.py` (수정)

```python
@router.post("/chat")
async def chat(request: ChatRequest, db: Session = Depends(get_db)):
    # 대화 상태 복원
    conv = conversation_store.get_or_create(request.conversation_id)
    
    # Session Context 구성
    ctx = SessionContext(
        office_id=request.office_id,
        group_id=request.group_id,
        year=request.year,
        month=request.month,
        nurse_id=request.nurse_id,
        nurse_name=request.nurse_name,
        user_role=request.user_role,
        conversation_id=conv.id,
        messages=conv.messages,
        pending_approval=conv.pending_approval,
    )
    
    # Agent 실행
    agent = SchedulingAgent(llm_client)
    result = agent.run(db, request.message, ctx)
    
    # 대화 상태 저장
    conversation_store.save_messages(conv.id, result.messages)
    if result.awaiting_approval:
        conversation_store.set_pending_approval(conv.id, result.preview)
    else:
        conversation_store.set_pending_approval(conv.id, None)
    
    return {
        "conversation_id": conv.id,
        "answer": result.answer,
        "needs_clarification": result.needs_clarification,
        "clarification_question": result.question,
        "clarification_options": result.options,
        "awaiting_approval": result.awaiting_approval,
        "preview": result.preview,
        "pipeline_stages": [s.to_dict() for s in result.trace],
        "total_time_ms": sum(s.duration_ms for s in result.trace),
    }
```

---

## 3. v2 → v3 Harness 매핑

| v2 (현재) | v3 (신규) | 상태 |
|---|---|---|
| `harness/agents_loader.py` | `harness/prompt_builder.py` | **대체** — AGENTS.md 대신 DOMAIN_KNOWLEDGE.md + 조립 로직 |
| `harness/topic_selector.py` | (제거) | **삭제** — ConceptType 기반 선택 불필요. domain knowledge는 항상 전체 로딩 |
| `harness/skill_matcher.py` | `skills/descriptions.py` | **대체** — .aide/skills/SKILL.md frontmatter 대신 Python dict로 tool schema 정의 |
| `harness/memory_manager.py` | `conversation.py` | **발전** — 파일 기반 memory → 메모리 기반 conversation state |
| (없음) | `middleware.py` | **신규** — permission, context injection, grounding, trace |
| (없음) | `schemas/session_context.py` | **신규** — 타입 안전한 session context |
| (없음) | `grounding/internal.py` | **신규** — resolve_nurse, resolve_shift, resolve_date |

---

## 4. 데이터 흐름 요약

```
[HTTP Request]
  │ message, office_id, group_id, nurse_id, conversation_id
  │
  ▼
[Router] ─── ConversationStore.get_or_create() ──→ 이전 messages 복원
  │
  ▼
[SessionContext] 구성 (office_id, group_id, year, month, nurse_id, user_role, messages)
  │
  ▼
[PromptBuilder] ─── DOMAIN_KNOWLEDGE.md + Routines + SKILL_TOOLS + few-shot ──→ system prompt
  │
  ▼
[Agent Loop]
  │ Turn 1: LLM(system + user_message + tools) → tool_call
  │         │
  │         ▼
  │ [Middleware]
  │   ① PermissionCheck(user_role, skill, args)
  │   ② ContextInjection(group_id, year, month, "나"→nurse_id)
  │   ③ InternalGrounding(nurse_name→id, shift_name→code, date→ISO)
  │   ④ run_skill(db, skill_name, grounded_params)
  │   ⑤ TraceCapture(stage, duration)
  │         │
  │         ▼
  │ Turn 2: LLM(system + user + tool_call + tool_result) → text(answer)
  │
  ▼
[AgentResult] → answer + trace + messages
  │
  ▼
[Router] ─── ConversationStore.save_messages() ──→ 상태 저장
  │
  ▼
[HTTP Response] → JSON (answer, stages, conversation_id)
```

---

## 5. 파일 구조 (최종)

```
agents_v2/
├── agent_v3.py              # Agent loop (Routine-aware hybrid: Routine + plain loop)
├── middleware.py             # Permission, context, grounding pipeline
├── conversation.py          # Multi-turn conversation state + Variable Memory
├── variable_memory.py       # ★ Routine step 간 파라미터 전달 (VM)
├── llm_client.py            # LLM abstraction (OpenAI/Anthropic/Deterministic)
├── debug_agent_v3.py        # Debug wrapper (trace capture for UI)
│
├── harness/
│   ├── prompt_builder.py    # System prompt 조립 (domain knowledge + routines + tools + examples)
│   ├── agents_loader.py     # (유지 — backward compat, v3에서는 prompt_builder 사용)
│   └── memory_manager.py    # (유지 — 장기 memory, conversation.py와 역할 구분)
│
├── schemas/
│   ├── session_context.py   # SessionContext dataclass
│   ├── agent_response.py    # (유지) AgentResponse / AgentResult
│   └── ...
│
├── grounding/
│   ├── internal.py          # resolve_nurse, resolve_shift, resolve_date
│   └── ...                  # (v2 grounders는 Phase 4에서 제거)
│
├── skills/
│   ├── descriptions.py      # SKILL_TOOLS (9개 tool JSON schema)
│   ├── registry.py          # (유지) skill dispatcher
│   ├── query_schedule.py    # (수정) internal grounding 제거 → middleware에서 처리
│   └── ...
│
└── tools/                   # (유지) DB 접근 레이어
    ├── schedule_tools.py
    ├── wanted_tools.py
    └── ...
```
