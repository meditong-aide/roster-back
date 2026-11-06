# 에이전트 DAG 계획 설계 (LLMCompiler식) — Proposed

- Status: **Proposed (미구현)** · Date: 2026-07-08
- 근거: LLMCompiler (ICML 2024, arXiv:2312.04511), Atomix (트랜잭션 tool), HITL 3-tier
- 관련: [ADR 0003 §2](adr/0003-agent-orchestration-contracts.md) (plan 아티팩트 갭), 복합쿼리
  테스트(project 메모리 `compound_query_planning`), Step 2 consolidated 승인 게이트

> 이 문서는 **설계 박제용**이다. 구현은 opt-in 플래그 뒤로 별도 진행.

---

## 1. 동기 (Why)

복합쿼리 E2E 테스트(실 LLM)에서 확인:
- 현재 루프는 **독립 병렬**은 잘 배칭(몬스터 10요청 → 1턴 9~10 calls).
- 그러나 **의존 복합**("대체자 찾아서 그 사람 배정")은 멀티턴 ReAct로 느리게 처리하고,
  **plan이 암묵적**(관찰 의존, 아티팩트 없음) — ADR 0003 §2가 지적한 갭.

DAG 계획은 (a) 의존성을 **명시적으로** 처리하고, (b) **plan을 아티팩트화**(재계획·투명성)하며,
(c) 의존 복합에서 LLM 왕복을 줄여 오히려 **빠르다**.

---

## 2. 스키마 (plan을 아티팩트로)

```python
@dataclass
class PlanTask:
    id: str                  # "t1"
    skill: str
    args: dict               # "$t1.candidates[0].name" 처럼 이전 출력 참조 허용
    deps: list[str]          # ["t1"]  — topological 의존
    kind: str                # "read" | "mutate"  — 부수효과 tier

@dataclass
class Plan:
    tasks: list[PlanTask]    # DAG (acyclic)
```

## 3. 4-컴포넌트 (LLMCompiler 구조)

```
[1] Planner   — LLM structured-output 1회 → 전체 DAG 생성 (스킬 설명 참고)
[2] Fetcher   — deps 충족된 task 를 ready set 으로. $tN.field 를 이전 출력으로 치환
[3] Executor  — ready set 병렬 실행. read=즉시 / mutate=dry-run(preview_only=True)
[4] Joiner    — 전 task 출력으로 최종 답변 합성. 실패 task 있으면 replan(선택)
```

- Planner는 structured output(StructuredOutput tool 강제)로 Plan JSON 을 낸다 → 신뢰도 확보.
- Fetcher는 Kahn 위상정렬: 의존 없는 task 부터, 완료되면 후속 unlock.
- Executor 동시성 상한(현행 병렬과 동일 정책).

## 4. Worked Example — "5/3 나이트 대체자 찾아서 배정해줘" (의존)

```
Plan:
  t1: recommend_candidates(date=2026-05-03, shift=나이트)             deps=[]    kind=read
  t2: bulk_mutation(action=assign, nurse=$t1.candidates[0].name,      deps=[t1]  kind=mutate
                    date=2026-05-03, shift=나이트, preview_only=True)

실행:
  t1 즉시 실행 → candidates 획득
  t2: $t1.candidates[0] 치환 → dry-run preview 생성
  → mutate task 존재 → consolidated 승인 게이트(Step 2) → 사용자 "응"
  → t2 를 preview_only=False 로 위상순서 commit
```

## 5. 우리 조각과의 통합 (대부분 이미 있음)

| DAG 요소 | 재사용할 우리 기존 |
|---|---|
| kind=read/mutate 분류 | outcome taxonomy(errors.py) + manifest `mutation` 플래그 |
| mutate = dry-run | `preview_only=True` |
| mutation → 승인 | **Step 2 consolidated 게이트**(`_execute_approval_batch`) 그대로 물림 |
| $tN 출력 치환 | VariableMemory `$vm.{key}` 확장 |
| plan 아티팩트 | **신규** — trace 에 Plan 기록 → ADR 0003 §2 갭 충족 |

→ **핵심**: 부수효과 분류·dry-run·consolidated 승인이 이미 갖춰져, DAG는 그 위에 **계획층만**
얹으면 된다.

## 6. 언제 쓰나 (하이브리드 — 4번째 모드)

현행 3-mode(ReAct / Routine / client-action)에 **DAG를 추가**:
- 단순·독립 병렬 → **기존 ReAct 루프** (planner 오버헤드 없음)
- 알려진 복합 → **Routine**(프롬프트 레벨)
- **의존 복합(신규)** → **DAG planner**

감지: 값싼 분류("의존 신호 있는 2+ intent") 또는 planner가 단순 쿼리엔 1-task DAG 반환.
잘못 감지해도 ReAct fallback 으로 무해.

## 7. Tradeoff (정직)

> ⚠️ 흔한 오해: "병렬 안정성 ↔ 속도/비용". **아니다.** 병렬 불안정(체인 깨짐)은 Bug B(03f5316)
> 로 이미 해결됐고, write 경쟁은 dry-run+consolidated commit 으로 직렬화돼 있다.

| 축 | ReAct(현행) | DAG planner |
|---|---|---|
| 단순/독립 쿼리 지연 | 빠름 | **느림**(불필요한 plan 1회) |
| 의존 복합 지연 | 느림(N 순차 턴) | **빠름**(1 plan + 병렬) |
| 의존성 처리 | 암묵(observe 의존) | **명시(DAG)** |
| plan 아티팩트 | 없음 | **있음(재계획·투명)** |
| 제어흐름 복잡도 | 단순 | **복잡** |
| planner 신뢰도 리스크 | 없음 | **있음(bad DAG 가능)** |
| write 안정성 | consolidated 게이트 | 동일(재사용) |

**진짜 tradeoff**: *의존성 명시 + plan 아티팩트(획득) ↔ 제어흐름 복잡도 + planner 신뢰도
리스크(비용)*. 속도/비용은 **쿼리 복잡도 의존**(단순=손해, 복합=이득)이라 단순 "vs"가 아니다.
→ 결론: **opt-in 게이팅**(단순은 안 건드리고 의존 복합만 DAG)이 정답.

## 8. Rollout (회귀 리스크 최소)

1. 플래그 뒤 opt-in. 기존 ReAct 루프 **무변경**.
2. 감지된 의존 복합에만 DAG 적용. 나머지는 현행.
3. Planner structured-output 실패/타임아웃 → ReAct fallback.
4. Plan 을 trace 에 기록(투명성) + 회귀테스트(단일/독립/의존 각 케이스).

## 9. Non-goals
- 단순·독립 쿼리를 DAG로 강제(오버헤드만).
- 현행 병렬 배칭 대체(그건 잘 작동, Bug B로 안정화됨).
- 완전 자동 replan 루프(실패 시 fallback 우선, replan 은 후속).
