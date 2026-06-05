# Agent Session Handoff — 2026-05-17

> 다음 세션이 5분 안에 흐름 파악할 수 있게 정리. 본 세션 (ralph + ontology overhaul + dev 머지) 의 agent 관련 사용자 발언·결정·deferred 항목.

---

## 1. 에이전트 현황 한눈에

### 코드 베이스 — 두 세대 공존

```
app/agents/        ← 구버전 (legacy)
                    main_graph + 3 analyzer (LangGraph) — 새 작업 안 함

app/agents_v2/     ← 현재 (~6,300 줄, DeepAgent / Claude Code 스타일)
```

### `app/agents_v2/` 구조

```
agent_v3.py         464 줄   Main orchestrator (intent → planning → execute)
conversation.py      94 줄   Turn 관리
llm_client.py       455 줄   OpenAI / Anthropic 추상화
middleware.py               FastAPI HTTP middleware
variable_memory.py          대화 메모리

skills/             9 skill — query_schedule, bulk_mutation, update_constraint,
                    update_person_attr, generate_schedule, validate_schedule,
                    recommend_candidates, repair_schedule, analyze_report

tools/              8 도메인 raw DB 접근 (schedule, shift, nurse, wanted,
                    constraint, analysis, generation, base)

grounding/          자연어 → DB 해석 (이름 → nurse_id 등)
schemas/            SessionContext
harness/            LLM prompt builder
```

### 아키텍처 흐름 (5 stage)

```
USER (한국어)
  ↓
[1] Intent Analysis    LLM 이 전체 문장의 의도 파악
  ↓
[2] Planning           어느 skill 호출할지 (descriptions.py 가 LLM 에 노출)
  ↓
[3] Skill Execution    선택된 skill 호출, grounding 자체 수행
  ↓
[4] Verification       결과 검증
  ↓
[5] Answer Generation  자연어 응답
```

---

## 2. 사용자 결정 사항 (정책)

### ① 에이전트 우선순위 — **기능 > UI**

> "에이전트는 현재 UI보다는 기능에 포커싱 해야해."

새 작업은 UI/chat dashboard 보다 **agent 의 skill 능력 강화 / 정확도 / DB 정합성** 에 우선 투자.

### ② DB 직접 조회 패턴 — **현재 유지**

> "아니야 현재 유지하면 돼."

- `tools/*` 가 SQLAlchemy 로 DB 직접 조회 (router/service 우회) 패턴 유지
- service layer 통일은 안 함 (이전 토론에서 옵션 B 권장했으나 사용자 reject)
- 단점 (RBAC 우회, schema drift) 는 알고 있지만 우선순위 낮음

### ③ 쿼리 카테고리화 — **현재 LLM function calling 으로 충분**

> 별도 intent classifier 없이 9 skill description (descriptions.py 759 줄) 가 LLM 에 노출 → LLM 이 적절한 (skill, params) 선택.

추가 telemetry / dashboard 는 **미진행**.

---

## 3. 알려진 문제 — Deferred (사용자가 "지금 말고")

### 🐛 D-1. `tools/wanted_tools.py:get_submission_status` group_id 필터 누락

```python
# 현재 (line 168-173):
submitted_ids = set(
    r[0]
    for r in db.query(WantedRequest.nurse_id)
    .filter(
        WantedRequest.month == month_str,
        WantedRequest.is_submitted == True,
        # ⚠ group_id 조인 누락 — 회사 전체 fetch
    )
    .all()
)
```

**영향**:
- 회사 전체 nurse 의 모든 월 submission row fetch (병원 규모 비례 폭증)
- Python set intersection (O(N×M)) — 메모리·CPU 낭비
- RBAC 약점 (다른 그룹 nurse_id 가 내부 set 에 들어옴)

**수정안** (한 쿼리 left-join 으로):
```python
def get_submission_status(db, group_id, year, month):
    month_str = _wanted_request_month_str(year, month)
    rows = (
        db.query(
            Nurse.nurse_id, Nurse.name,
            WantedRequest.is_submitted,
        )
        .outerjoin(
            WantedRequest,
            (WantedRequest.nurse_id == Nurse.nurse_id)
            & (WantedRequest.month == month_str)
        )
        .filter(Nurse.group_id == group_id, Nurse.active == 1)
        .all()
    )
    submitted, not_submitted = [], []
    for nid, name, is_sub in rows:
        bucket = submitted if is_sub else not_submitted
        bucket.append({"nurse_id": nid, "name": name})
    return {
        "total": len(rows),
        "submitted_count": len(submitted),
        "not_submitted_count": len(not_submitted),
        "submitted": submitted,
        "not_submitted": not_submitted,
    }
```

→ 한 쿼리, DB-level filter, group 격리, 메모리·CPU 효율.

### 🔍 D-2. 다른 tool/skill 의 group_id 필터 누락 audit — 미진행

사용자 발언 "비슷한 패턴 다른 tool 에도 있나?" 에 대한 audit 안 함.

권장: 19 tool 함수 전체 grep:
```bash
grep -rE 'db\.query.*WantedRequest|db\.query.*ScheduleEntry|db\.query.*Nurse' \
    app/agents_v2/tools/ app/agents_v2/skills/
```
각각 `group_id` 필터/조인 있는지 확인.

---

## 4. 본 세션 ralph 작업과 agent 의 연결고리

본 세션은 ralph (ontology 진단·치료 시스템) 중심이었지만, **agent 와 직접 연결되는 path** 가 있음:

```
agent.generate_schedule skill
  → /roster_create/generate 호출
  → CP-SAT 솔버 INFEASIBLE
  → build_unrecoverable_payload  ← 본 세션 산출물
    · causes / hard_case / treatments / narrative / graph / apply_hint
  → agent 가 narrative.problem_list / action_levers 받음
  → 사용자에게 "왜 못 만들어졌는지 + 어떻게 해결할지" 자연어 응답
```

### 새 활용 포인트 (agent 측 보강 가능)

| 영역 | 가능 작업 | 우선순위 |
|---|---|---|
| **narrative 활용** | generate_schedule 결과 narrative 를 agent 의 자연어 답변에 흡수 (현재 raw 노출) | 높음 (사용자 UX 직접 영향) |
| **apply_hint 활용** | "team_min 풀고 재시도?" 같은 사용자 동의 흐름을 agent 가 chat 으로 진행 (apply_hint 따라 재호출) | 중간 (frontend 통합 후) |
| **hard_case 인지** | hard_case=true 케이스는 agent 가 즉시 "어려운 케이스 — 운영자 검토 권장" 안내 | 중간 |
| **recommend_candidates + treatment bundle** | 두 skill 통합 — 본 ralph 의 hitter 가 recommend 의 backbone 될 수 있음 | 낮음 (별도 사전 설계 필요) |

---

## 5. 권장 다음 세션 시작 작업

### Tier 1 — 즉시 fix (≤30분)

- [ ] **D-1** `get_submission_status` group_id 필터 추가
- [ ] **D-2** 다른 tool 함수 group_id audit (script 한 번 돌리고 결과 표로)

### Tier 2 — agent 기능 보강 (≤2시간)

- [ ] generate_schedule skill → narrative 활용 보강 (response_template_ko 자동 생성)
- [ ] apply_hint 받아서 사용자에게 "재시도?" 질문 → 답 받으면 재호출

### Tier 3 — 큰 작업 (반나절+)

- [ ] DB 직접 조회 → service layer 통일 (사용자 currently reject. 미래 우선순위 변경 시)
- [ ] 쿼리 카테고리화 telemetry (사용자가 명시 안 했지만 운영 관점에서 유용)

---

## 6. 핵심 파일 reference (다음 세션이 빨리 찾기)

### Agent 코드
- `app/agents_v2/agent_v3.py` — main orchestrator (5 stage)
- `app/agents_v2/skills/descriptions.py` — 9 skill 의 LLM-facing description (사실상 query taxonomy)
- `app/agents_v2/skills/registry.py` — run_skill(name, args)
- `app/agents_v2/tools/wanted_tools.py:156` — **D-1 bug 위치** (`get_submission_status`)

### 본 세션 ralph 결과 (agent 가 흡수할 것)
- `app/services/precheck/payload.py` — build_unrecoverable_payload (causes/hard_case/graph/narrative/apply_hint)
- `app/services/resolution_narrative.py` — narrative 빌더
- `app/services/semantics/ontology.py:120` — build_apply_hint 헬퍼

### 정책 결정 commit
- `cf66820` — Merge ralph + 정책 (max coverage hard / team_min user-confirmation / grade auto-soft)

---

## 7. 키워드 요약 (다음 세션 prompt 참고용)

| 키워드 | 의미 |
|---|---|
| **DeepAgent / Claude Code style** | 본 agent 의 아키텍처 패러다임 (top-down LLM intent → skill 위임) |
| **LLM-first** | regex / pattern dict / lookup table 금지 (CLAUDE.md 정책) |
| **skill self-description** | descriptions.py 가 사실상 query taxonomy |
| **thin memory** | 병원/병동별 데이터 프롬프트 미포함, 런타임 DB 조회 |
| **DB 직접 조회** | tools/* 의 패턴 (service layer 우회) — 사용자 결정으로 유지 |
| **apply_hint** | 본 세션 신설 — user-confirmation 후 재실행 호출 정보 |
| **hard_case** | 본 세션 신설 — 어려운 케이스 자동 분류 4 기준 |
| **friendly labels** | 본 세션 신설 — raw config_key → 한국어 운영 어휘 매핑 |

---

**End of handoff.** 다음 세션은 이 파일 + `progress.txt` + `CLAUDE.md` 만 읽으면 본 세션의 상태와 정책을 모두 인지 가능.
