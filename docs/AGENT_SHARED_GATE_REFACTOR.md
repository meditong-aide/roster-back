# Agent 실행 계층 — 공유 게이트 리팩터 설계

> 목적: DAG 플래너 경로와 ReAct 경로가 **각자 구현**하고 있는 cross-cutting 로직
> (승인/커밋·staleness·자동검증·L2검증·clarify)을 **단일 공유 게이트**로 추출.
> 근거·문헌: docs/AIDE_AGENT_RESEARCH_POSITIONING.md §3.6·§4.5 (Anthropic/LLMCompiler 등,
> "두 경로 자체가 아니라 cross-cutting 로직 복제가 drift를 낳는다").

## 1. 문제 — 이미 발생한 비대칭(drift)

같은 "미리보기→승인→커밋" 개념이 경로별로 3벌 구현돼 있고, 서로 다르다.

| cross-cutting 관심사 | DAG 경로 | ReAct 경로 | 비대칭(=버그 위험) |
|---|---|---|---|
| 진입 dispatch | `_run_impl` L468 `pending_approval.type` 분기 | 동일 지점 | (공유됨 ✓) |
| 확인/거부 판정 | `_is_confirmation`/`_is_denial` | 동일 | (공유됨 ✓) |
| 커밋 실행 | `_commit_plan` (L329) | `_execute_approval` (L1135) / `_execute_approval_batch` (L1208) | **3벌** |
| **staleness 가드**(#5, 승인 후 미리보기 변화 재확인) | 있음(`preview_fingerprint`) | **없음** | ❌ ReAct 승인은 stale commit 가능 |
| **원자성**(all-or-nothing 롤백) | 있음(commit→flush 리다이렉트) | batch는 순차, 부분반영 가능 | ❌ ReAct batch 부분커밋 |
| **커밋 후 async**(generate 등 deferred) | 있음 | 없음(개념 부재) | 경로별 기능 격차 |
| **자동 사후검증**(mutation 후 validate_schedule) | **없음** | 있음(L1177) | ❌ DAG 커밋은 auto-validate 안 함 |
| **L2 답변검증**(answer↔data 정합) | `_plan_answer` (L215) | inline (L591) | **2벌**(동일 로직) |
| clarify 방출 | `_finalize_plan` clarify 브랜치(구조화 clarify_form) | tool 루프 needs_clarification 버블업(free-text) | **2 메커니즘** |
| clarify 재개 | `_resume_clarify` (L251) | (해당 없음 — 단발) | 표현 불일치 |

**결론**: 문헌이 경고한 "경로별 cross-cutting 복제 → drift"가 **이미 실현**됨. staleness·원자성·
자동검증이 경로마다 있거나 없어 사용자 체감 동작이 승인 대상에 따라 달라진다.

## 2. 목표 / 비목표

**목표**
- 위 관심사를 **경로 독립 공유 컴포넌트 1개씩**으로 추출, 두 경로가 호출.
- 비대칭 3건(staleness/원자성/자동검증) 해소 → 어느 경로든 동일 안전보장.
- **행동 보존**(단계별). 각 단계는 회귀 0으로 머지 가능해야 함.

**비목표**
- 두 실행 경로(DAG/ReAct) 자체를 하나로 합치지 않는다. §3.6대로 **분기 유지가 정답**.
- 라우팅/플래너 로직(build_plan, router)은 건드리지 않는다.

## 3. 핵심 추상 — `CommitUnit`

두 경로의 차이는 **커밋 단위**뿐이다: ReAct=단일 스킬콜(또는 batch 항목들), DAG=plan DAG.
공유 게이트는 이 단위를 몰라도 되게, 최소 인터페이스로 감싼다.

```python
# app/agents_v2/commit_gate.py (신규)
class CommitUnit(Protocol):
    def previews(self, db, ctx) -> list[dict]: ...          # dry-run 미리보기 목록
    def commit(self, db, ctx) -> CommitOutcome: ...          # 실제 반영(원자 경계 안에서 호출됨)
    def deferred(self) -> list[Deferred]: ...                # 커밋 후 실행할 async(없으면 [])
    def answer(self, llm, user_message, outcome) -> str: ... # 결과 답변 합성(경로별 구현)
    scope_hint: str | None                                   # 자동검증 트리거용(예: "schedule")

@dataclass
class CommitOutcome:
    ok: bool
    outputs: dict          # task/skill id → 결과
    failed: dict | None    # 실패 정보(rollback 사유)
    order: list[str]       # 커밋 순서(감사)
```

두 어댑터:
- `PlanCommitUnit(plan)` — 기존 `_commit_plan` 본문을 이 어댑터의 `commit()`으로 이동.
- `SkillCommitUnit(skill_name, args)` / `BatchCommitUnit(items)` — 기존 `_execute_approval(_batch)` 본문 이동.

## 4. 공유 게이트 API

```python
def commit_gate(db, ctx, unit: CommitUnit, *, approved_fp, llm,
                auto_validate: bool, messages) -> AgentResult:
    # 1) staleness: approved_fp 있으면 dry-run 재실행→지문 비교, 다르면 재확인(커밋 안 함)
    # 2) 원자 경계: db.commit→flush 리다이렉트로 unit.commit() 호출, 실패 시 전체 rollback
    # 3) 성공 시 db.commit(); 실패 시 롤백 답변
    # 4) 커밋 후 unit.deferred() 실행(트랜잭션 밖)
    # 5) auto_validate and unit.scope_hint 이면 validate_schedule 후 요약 부착
    # 6) answer = unit.answer(llm, ...) + L2(verify_answer) 정합 검증
```

- **staleness/원자성/deferred/자동검증/L2**가 여기 **한 곳**에 산다. 경로는 `unit`만 바꿔 넣는다.
- `verify_answer(llm, user_message, data, answer)` = 기존 `_plan_answer`의 judge+l2 래퍼를
  `verify.py`로 승격, `_plan_answer`와 ReAct inline(L591) **양쪽이 호출**.

## 5. 단계별 마이그레이션 (각 단계 = 독립 PR, 회귀 0)

| 단계 | 내용 | 위험 | 검증 |
|---|---|---|---|
| **P1** | `verify_answer()` 공유화 — `_plan_answer`와 ReAct L591이 같은 함수 호출(로직 이동만) | 낮음 | 기존 verify/L2 테스트 그린 |
| **P2** | `preview_fingerprint` 기반 staleness 헬퍼 추출 + **ReAct 승인에 추가**(현재 없음) | 중 | 신규: ReAct 승인 staleness 재확인 테스트 |
| **P3** | `commit_gate` 원자 경계 추출 → `_commit_plan`을 `PlanCommitUnit`으로 이관(행동보존) | 중 | test_dag_atomic 전부 그린 |
| **P4** | `_execute_approval(_batch)`를 `SkillCommitUnit`/`BatchCommitUnit`으로 이관 → **원자성 획득** | 중높 | 신규: ReAct batch 부분실패 롤백 테스트 |
| **P5** | 자동검증 정책 통일(`auto_validate` 플래그) — DAG 커밋도 옵션 적용할지 결정·배선 | 중 | 정책 테스트(양 경로 동일) |
| **P6** | clarify 표현 통일 — ReAct needs_clarification도 `clarify_form` 스키마로 방출, `_resume_clarify` 공용화 | 높음 | test_dag_clarify_resume + ReAct clarify 재개 신규 |

P1~P2가 **저위험·즉효**(L2 dedupe + ReAct staleness 갭 메움). P6은 별도 착수(표현 통일은 프론트 계약 영향).

## 6. 테스트 전략
- 각 단계는 **기존 테스트 그린 유지**가 통과 기준(행동 보존).
- 비대칭 해소는 **신규 테스트로 고정**: ReAct staleness(P2), ReAct batch 원자성(P4),
  양경로 자동검증 동일(P5).
- 공유 게이트는 `CommitUnit` 더블로 **경로 무관 단위테스트**(staleness/rollback/deferred 각각).
- real-LLM 2턴 커밋(tests/agent_qa/test_real_llm_commit.py)은 P3·P4 후에도 그린이어야(경로 무관).

## 7. 리스크 / 결정 필요
- **자동 사후검증 정책(P5)**: 지금 ReAct만 함. DAG 커밋에도 켤지 = 제품 결정(성능↔안전). 기본 정책 미정.
- **clarify 표현 통일(P6)**: ReAct free-text clarify를 구조화 form으로 바꾸면 **프론트 렌더 계약** 변경.
  별도 핸드오프 필요. 백엔드 단독으로는 P5까지.
- **StaticPool 테스트 오염**(기존 부채): P3/P4가 실 commit 경계를 건드리므로, 풀스위트 격리
  문제(shift_manage UNIQUE)를 이 참에 savepoint fixture로 손볼지 검토(별건이나 인접).

## 8. 착수 순서 권고
P1(L2 dedupe) → P2(ReAct staleness) 를 먼저. 이 둘만으로 "동일 개념 2벌 + ReAct 안전갭"의
가장 큰 부분이 정리되고, P3~P4(원자 경계 통합)로 커밋 3벌→1벌 수렴. P5·P6은 정책/프론트 결정 후.
