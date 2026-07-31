# 근무표 Infeasibility 진단·해결 개발 로그 & 이론

작성일: 2026-07-30 · 브랜치: dev · 대상 인스턴스: 부산(그룹 `101358d8b48e`) 2026-08

이 문서는 (1) 지금까지 개발 내용, (2) 라이브 테스트 기록, (3) 그 과정에서 나온 궁금증과
답, (4) **max-flow와 P≠NP** 이론 배경을 한곳에 정리한다.

---

## 1. 배경

근무표를 생성하면 특정 설정 조합에서 **infeasible**(해 없음)이 나고 HTTP 500이 뜬다.
문제는 두 가지였다.

1. **원인을 안 알려줌** — "왜 안 되는지"가 사용자에게 불투명. 뻔한 모순인데도 무거운 계산
   뒤에야 알려주거나, 엉뚱한 규칙을 수백 번 흔들어 봄.
2. **해결책이 없음** — 원인을 알아도 관리자가 뭘 바꿔야 할지 카드로 안 떨어짐.

목표: **원인을 정확히 짚고, 원클릭 해결 카드를 주고, 그 진단 과정을 지식그래프로 남긴다.**

---

## 2. 개발 내용

### 2.1 원인 진단 → 행동 카드

- `explain_infeasibility_from_config`([lagrangian.py](../app/services/ontology_graph/lagrangian.py)) —
  실제 nurses+config를 보고 "왜 해가 없나"를 분류한다.
  - `personal_infeasible` : 개인 산술 즉시모순 (예: 야간 요구 13회 > 월 상한 7회). **증명 가능**.
  - `personal_overconstraint` : 개인 축 병목 (주말휴무 등). λ(라그랑주 승수) 추정.
  - `coverage_shortage` / `policy_overconstraint` / `coupled_sequence` / `unknown`.
- `cause_to_resolution_options`([mcs_trace.py](../app/services/ontology_graph/mcs_trace.py)) —
  분류·타깃을 모달 카드(`resolution_options`)로 번역. "이 방법으로 다시 생성" 카드가 뜬다.

### 2.2 다인 통합 카드 (`_sole_option`)

문제 간호사가 여럿일 때(김수선 13>7, 김도영 10>7) 카드가 **한 명만** 뜨던 버그.
원인: per-nurse MCS가 cause 블록보다 **나중에** 개별 카드를 prepend해 리스트 맨 앞을 차지.

수정: 개인 즉시모순은 전원을 **한 카드**(`cause:fix_all_personal`)로 통합하고, 응답 raise
직전에 그 카드 하나로 **최종 override**([roster_create_service.py](../app/services/roster_create_service.py) `_sole_option`).
→ 카드 하나에 문제 간호사 전원 행이 들어감.

### 2.3 `n_exact` → `n_max` 전환

카드가 야간 한도를 낮출 때 방식을 **정확히 N** → **최대 N까지**로 변경.

| 방식 | 뜻 | 성격 |
|---|---|---|
| `n_exact=7` (전) | 정확히 7개 | 경직 — 솔버가 반드시 7개 |
| `n_max=7` (후) | 최대 7개까지 (0~7) | 유연 — 솔버가 필요시 더 적게 |

주의: 지금 충돌은 **하한(`n_min`=13)**이라, `n_max=7`만 넣으면 `13 > 7` 그대로 모순.
그래서 적용 경로에서 **`n_min`이 새 상한보다 크면 함께 해제**하도록 확장
([roster_create_service.py](../app/services/roster_create_service.py) 4458~).

### 2.4 probe 스킵 / pre-solve gate — 시도했다가 되돌림

- **probe(377-combo 전수탐색)**: infeasible 시 규칙을 이것저것 완화하며 재solve를 수백 번
  돌리는 탐색. 원인이 개인 축(주말휴무)이면 전역 규칙만 흔드는 probe는 **전부 실패 = 헛수고**.
- 한때 `personal_overconstraint`를 스킵 리스트에 넣고, 증명된 산술모순은 **솔버 이전**에
  막는 pre-solve gate를 넣었으나 — **되돌렸다**(§3의 데이터 오염 사고 때문에 트리거가 실제
  버그가 아니라 내 실수였음이 드러남). 현재 코드엔 없음.
- 남은 통찰은 §5(P≠NP)에 정리: **증명 가능한 것만 pre-solve로 막아야 안전**하다.

### 2.5 Neo4j 지식그래프 적재 (신규)

- **`neo4j_sink.py`**([링크](../app/services/ontology_graph/neo4j_sink.py)) —
  `graph_export`(nodes/edges/run)를 Neo4j에 MERGE.
  - `build_cypher_payload` : 순수함수(DB 불필요) → 단위테스트 가능.
  - 노드 uid = **`run_id|node_id`** 네임스페이싱 → 생성 시도마다 **독립 스냅샷(버전)**.
  - 라벨/관계타입은 Cypher 파라미터 불가라 폐쇄어휘 화이트리스트 정규화 후 보간.
- **배선**: `dump_live_graph_export`([live_graph_export.py](../app/services/live_graph_export.py)) 끝에서 push.
- **게이팅**: `NEO4J_URI` 없으면 **no-op**. 어떤 예외도 API로 미전파.
- **테스트**: `tests/test_neo4j_sink.py` 5 passed.
- **미완**: 실제 Neo4j 인스턴스 E2E는 미검증(개발 머신에 Neo4j 없음). `.env`에
  `NEO4J_URI`/`USER`/`PASSWORD` 설정 후 서버 재시작하면 UNRECOVERABLE 생성 시 자동 적재.

### 현재 코드 상태 요약

| 항목 | 상태 |
|---|---|
| 다인 통합 카드(`_sole_option`) | **유지** |
| `n_exact`→`n_max` + `n_min` 해제 | **유지** |
| Neo4j sink + 배선 | **유지(신규)** |
| probe 스킵에 `personal_overconstraint` | 되돌림 |
| pre-solve gate / `arithmetic_only` | 되돌림 |

---

## 3. 라이브 테스트 기록

토큰으로 `/roster_create/generate`를 직접 호출해 검증.

1. **8월 plain 생성** → HTTP 500, 15초.
   `personal_infeasible` : "김수선 야간 13회 필요, 상한 7회; 김도영 야간 10회 필요, 상한 7회".
   카드 1개에 두 명 다 → 다인 통합 카드 정상 확인.

2. **데이터 오염 사고 ⚠** — 카드 적용을 검증한답시고 `monthly_limit_release`(n_exact=7)를
   body에 실어 curl. 그런데 이 파라미터는 **읽기 전용이 아니라** 생성 진입부에서
   `NurseMonthlyLimit`에 **즉시 커밋**한다. 결과: 김수선·김도영의 8월 야간 한도가
   실제로 13/10 → 7로 **바뀌어 버림**. 그래서 원인이 갑자기 주말휴무로 보였고, 그걸 고친다고
   probe 스킵·pre-solve gate까지 손대면서 상황이 꼬였다.

3. **복구** — `NurseMonthlyLimit`을 읽어 확인(n_min=13/10은 그대로, 내가 n_exact=7만 덮음)
   → n_exact를 None으로 되돌림 → plain 생성이 다시 "13>7 / 10>7" 2인 카드를 냄. **원상 복구**.

교훈(메모리 기록): **generate 검증은 plain body로만**. release 파라미터는 실제 적용 경로일
때만. 라이브(특히 eun) 데이터는 쓰기 전 값을 먼저 읽고, 오염 시 원복.

---

## 4. 궁금증 & 답

- **"카드에 왜 한 명만?"** → per-nurse MCS가 통합 카드보다 늦게 prepend돼 맨 앞을 차지한 것.
  `_sole_option` 최종 override로 전원 통합(§2.2).
- **"왜 또 전체 검사를 다시?"** → 15~22초는 probe(370회) + 솔버 폴백 재시도. 원인 진단이
  **솔브가 끝난 뒤** 사후부검으로 돌기 때문(§2.4, §5).
- **"exact 말고 max로도 가능?"** → 가능. 오히려 솔버 자유도 상 유리. 단 `n_min` 하한도 함께
  해제해야 함(§2.3).
- **"지식그래프 다는 건?"** → in-memory 그래프를 Neo4j에 적재(§2.5).

---

## 5. 이론: Max-flow와 P≠NP

가장 자주 나온 질문 — **"왜 max-flow로는 infeasible을 전부 못 잡나?"**

### 5.1 P와 NP (짧게)

- **P** : 입력 크기 n의 **다항시간에 푸는** 문제. 예: max-flow(Edmonds-Karp `O(VE²)`).
- **NP** : 해를 주면 다항시간에 **맞는지 검증**되는 문제.
- **NP-complete/NP-hard** : NP에서 가장 어려운 부류. 다항 알고리즘이 **알려져 있지 않다**.
- **P≠NP** (미해결 추측) : "검증은 쉬운데 푸는 건 어려운 문제가 실제로 존재한다." 대다수
  학자가 참이라 믿는다. 참이면 NP-complete 문제엔 **다항 알고리즘이 영원히 없다**.

### 5.2 간호사 근무표는 NP-hard

근무표 배정은 제약충족(CSP) + 조합 최적화다. **feasibility 판정**("이 설정으로 표가
만들어지나?")이 일반적으로 NP-complete(그래프 채색·bin-packing류로 환원). 즉 **일반적으론
다항시간에 답할 수 없다**(P≠NP라면). 그래서 CP-SAT 같은 솔버가 필요하다.

### 5.3 Max-flow는 P라서 문제의 '일부'만 본다

하루 커버리지(간호사 → 시프트 이분매칭)는 max-flow로 다항시간에 푼다. 하지만 근무표의
**조합적 제약** — 연속근무 상한, 2N2OFF 회복, N→D 금지, 야간 간격, 월 OFF 배분, 주말휴무와
이들의 상호작용 — 은 flow로 표현되지 않는다. flow는 "각 노드 용량"만 보지 **시간축 순서와
규칙 간 상호작용**은 못 본다. 그래서 max-flow가 증명할 수 있는 건 **하루·월 용량 부족**뿐이다.

### 5.4 한 방향으로만 참 (soundness vs completeness)

- **shortage > 0 ⟹ infeasible** (건전) : max-flow조차 못 채우면 진짜 못 채운다.
  이건 **증명된 하한**이다. → 안심하고 즉시 차단 가능.
- **shortage = 0 ⇏ feasible** (불완전) : 하루 용량은 되는데 조합적으로 불가능할 수 있다.
  ← **우리가 겪은 주말휴무 케이스가 정확히 이것**. 주말 인원은 충분(shortage=0)이지만,
  주말휴무 2명 + 2N2OFF + 연속제약이 얽혀 실제론 배치 불가.

  > 비유: 각 날짜에 사람 수는 충분한데, "그 사람들이 규칙을 다 지키면서 동시에 배치되는가"는
  > 완전히 별개의 (더 어려운) 문제다.

### 5.5 그래서 2단 구조 (P-floor + NP-escalate)

- **Tier-1 (P, ms)** : max-flow로 용량 부족을 즉시 **증명**. 확실한 것만.
- **Tier-2 (NP, 초)** : 용량은 되는데 안 되면 → **솔버**가 조합적으로 판정. λ/MCS로 원인 지목.

이게 **pre-solve gate를 산술모순만 막고 주말휴무는 솔버에 맡겨야 하는 이유**다:
- 산술모순(13>7)과 max-flow 부족은 **P** → 증명 가능 → 솔버 없이 미리 막아도 **안전**.
- 주말휴무는 조합적(NP-part) → max-flow로 shortage=0이라 **증명 불가** → 솔버 없이 막으면
  **실제로는 풀리는 표를 오탐 차단**할 위험. (그래서 pre-solve gate는 산술모순 한정이어야 했고,
  성급히 확장했다가 되돌렸다.)

### 5.6 MUS vs MCS 속도 (곁가지)

- **하나의 MCS**(최소 수선집합) 찾기 = deletion으로 선형 재solve `O(N)`. **다항**.
- **최소 크기 MCS** 또는 **모든 MUS 열거** = **NP-hard**.
- 우리는 "하나의 수선집합"만 필요하므로 선형. **MUS는 안 쓴다**(reify 비용 + 결과가 비유일·임의).

---

## 6. 남은 일 / 다음 단계

1. **Neo4j 실연결 E2E** — Neo4j(Desktop/Aura Free/Community) 띄우고 `.env` 설정 → 샘플 적재 →
   Cypher 조회로 확인.
2. **적재 범위 확장(선택)** — 지금은 UNRECOVERABLE 경로만 적재. 성공/precheck-block 경로
   그래프도 남길지 결정.
3. **pre-solve gate 재도입(선택)** — 이번엔 **증명 가능한 것만**(산술모순 + max-flow shortage>0)
   한정으로, 오탐 없이. §5.5의 원칙을 코드로.
4. **커밋** — 위 변경들 미커밋 상태.

---

### 부록: 관련 파일

| 파일 | 역할 |
|---|---|
| [lagrangian.py](../app/services/ontology_graph/lagrangian.py) | 원인 분류(산술·λ) |
| [mcs_trace.py](../app/services/ontology_graph/mcs_trace.py) | 원인 → 카드 번역 |
| [supply_demand.py](../app/services/ontology_graph/supply_demand.py) | max-flow(Edmonds-Karp) |
| [presolve_diagnosis.py](../app/services/ontology_graph/presolve_diagnosis.py) | 솔브 전 부족 하한 |
| [neo4j_sink.py](../app/services/ontology_graph/neo4j_sink.py) | 지식그래프 Neo4j 적재 |
| [live_graph_export.py](../app/services/live_graph_export.py) | graph_export 덤프 + Neo4j 배선 |
| [roster_create_service.py](../app/services/roster_create_service.py) | 생성·진단·카드 오케스트레이션 |
