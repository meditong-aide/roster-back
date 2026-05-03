# AIDE Agent Development Guide

## Architecture Direction

AIDE 스케줄링 에이전트는 **top-down agentic flow**를 따른다.
참고 아키텍처: **LangChain DeepAgent**, **Claude Code의 agentic flow**.

### Core Principle: LLM-Driven, Not Rule-Driven

- LLM이 사용자 입력의 **전체 의도(full intent)**를 분석한다
- LLM이 어떤 스킬을 어떤 순서로 호출할지 **planning**한다
- 각 스킬은 **자기 설명(self-describing)**을 갖고, LLM이 이를 참고해 선택한다
- 스킬 내부에서 필요한 **grounding(DB 조회, 이름 해석 등)**을 자체 처리한다

### Anti-Patterns (하지 말 것)

| Anti-Pattern | 왜 안 되는가 |
|---|---|
| regex/hardcoded string matching으로 intent 파싱 | 한국어 표현 다양성에 대응 불가. "리스트업해봐", "보여줘", "알려줘" 전부 다른 패턴 필요 |
| bottom-up fragment parsing → grounder → canonicalizer | 파편화된 조각을 합치는 과정에서 문맥 손실. "원티드"가 lexical로 분류되면 grounder에서 UNRESOLVED |
| `_WORKFLOW_PATTERNS`, `_STATUS_PATTERNS` 같은 lookup table | 새 표현 추가할 때마다 코드 수정 필요. agentic하지 않음 |
| planner에서 UNRESOLVED fragment 있으면 무조건 block | canonicalizer가 이미 scope/operation 해석했는데 planner가 이를 무시 |

### Target Architecture: Top-Down Agentic Flow

```
User Input (자연어)
    │
    ▼
[1. Intent Analysis] ─── LLM이 전체 문장의 의도를 파악
    │                     "4월 원티드 미제출자 리스트업해봐"
    │                     → intent: 원티드 미제출 간호사 목록 조회
    │
    ▼
[2. Planning with Skill Awareness] ─── LLM이 사용 가능한 스킬 목록을 보고 실행 계획 수립
    │   스킬 descriptions가 프롬프트에 포함됨
    │   → plan: query-schedule(scope=wanted, filter=미제출)
    │
    ▼
[3. Skill Execution] ─── 선택된 스킬이 실행됨
    │   스킬 내부에서 필요한 grounding 자체 수행
    │   (nurse name → nurse_id, "이번 달" → 2026-04, etc.)
    │
    ▼
[4. Result Verification] ─── 실행 결과 검증
    │
    ▼
[5. Answer Generation] ─── 결과를 자연어로 변환
```

### Skill Design Principles

각 스킬은 다음을 갖춰야 한다:

1. **Self-Description**: LLM이 읽을 수 있는 자연어 설명
   - 이 스킬이 **무엇을 하는지**
   - **어떤 상황에서** 사용하는지
   - 필요한 **파라미터**와 각각의 의미
   - **예시 입력/출력**

2. **Internal Grounding**: 스킬 내부에서 DB 조회를 통해 이름→ID, 표현→코드 매핑
   - "김민지" → nurse_id 조회는 스킬이 직접 수행
   - "나이트" → shift_code 매핑은 스킬이 직접 수행
   - LLM에게는 사용자가 말한 자연어 그대로 전달

3. **Composability**: 복잡한 요청은 여러 스킬의 조합으로 처리
   - "김민지 원티드 취소하고 이영희로 대체해줘"
   - → plan: [bulk-mutation(cancel), recommend-candidates, bulk-mutation(assign)]

### Reference Architectures

#### LangChain DeepAgent Pattern
- Agent가 tool descriptions를 보고 어떤 tool을 사용할지 결정
- Tool은 자기 설명과 input schema를 갖는다
- Agent loop: Think → Act → Observe → Think → ...
- 복잡한 작업은 여러 tool 호출의 chain으로 처리

#### Claude Code Agentic Flow
- 사용자 의도를 전체적으로 파악한 후 실행 계획 수립
- Tool 호출 결과를 관찰하고 다음 행동 결정
- 필요시 사용자에게 clarification 요청
- 실행 과정 전체가 투명하게 추적 가능

### Current Skills (9개)

| Skill | Description |
|---|---|
| `query-schedule` | 읽기 전용 데이터 조회 (근무표, 원티드, 간호사 정보 등) |
| `bulk-mutation` | 배치 수정 (원티드 일괄 승인/거부, 근무 일괄 변경) |
| `update-constraint` | 스케줄링 제약조건 수정 (근무 규칙, 시프트 설정) |
| `update-person-attr` | 간호사 개인 속성 수정 (팀, 직급, 경력 등) |
| `generate-schedule` | 근무표 자동 생성 (비동기, SQS 경유) |
| `validate-schedule` | 근무표 제약조건 위반 검증 |
| `recommend-candidates` | 대체/교체 간호사 추천 |
| `repair-schedule` | 기존 근무표 수정/재조정 |
| `analyze-report` | 공정성 분석, 분포 리포트, 비교 |

### Development Rules

1. **LLM-first**: 모든 자연어 해석은 LLM이 담당. regex, pattern dict, lookup table 금지
2. **Skill self-description**: 새 스킬 추가 시 반드시 LLM이 읽을 수 있는 description 포함
3. **No fragment-level grounding**: 사용자 입력을 조각내서 각각 ground하지 않음. 전체 의도 파악 후 스킬에 위임
4. **Thin memory**: 병원/병동별 데이터는 절대 프롬프트에 넣지 않음. 런타임 DB 조회
5. **Transparent pipeline**: 모든 단계(intent analysis, planning, execution, verification)가 추적 가능해야 함

## Tech Stack

- **Backend**: Python, FastAPI, SQLAlchemy
- **DB**: MSSQL (production), SQLite (testing)
- **LLM**: OpenAI gpt-4.1-mini (primary), Anthropic claude-haiku-4-5 (fallback)
- **Testing**: pytest, deterministic LLM doubles
- **Package**: pip

## Commands

```bash
# Test
cd roster-back && python -m pytest tests/ -v

# Lint
ruff check app/

# Run server
cd roster-back && uvicorn app.main:app --reload --port 8000

# Test chat UI
# http://localhost:8000/agent/test
```
