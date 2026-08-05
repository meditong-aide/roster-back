# Infeasibility 진단 — 포지셔닝 (solver 대체가 아니라 공통 IR + 가속기)

## 질문

그래프 추론 엔진(frontier DP / conditioning / certificate)을 **솔버(CP-SAT)에 안 붙이고**
따로 두는 게 이점이 있나? 같은 제약을 두 번 구현하면 "근무표 솔버를 하나 더 만든 것 아닌가",
"결국 불완전한 두 번째 솔버 아닌가"로 공격받지 않나? MUS를 붙이는 것 외에 더 나은 점은?

## 결론

**솔버와 완전히 분리하는 것 자체를 기여로 삼으면 약하다.** 오히려 중복 구현 비판을 부른다.
강한 구조는 **하나의 명세(Canonical Constraint IR)에서 두 backend를 컴파일**하고, 그래프 계층을
**설명·검증·가속을 겸하는 proof-producing domain propagator**로 두는 것이다.

```
        Canonical Constraint IR (규칙을 한 번만 정의)
                 │
     ┌───────────┴───────────┐
     │                       │
  CP-SAT Compiler       Graph Compiler
  생성·최적화·최종판정    factor graph / automaton / frontier message / typed certificate
```

예: `NightRecoveryRule(trigger_run=2, required_off=2, scope="per_nurse", hardness="hard")`
→ 여기서 CP-SAT 제약 · factor node · automaton transition · certificate metadata · repair
operator 를 **자동 생성**. "일을 두 번 했다"가 아니라 "같은 명세를 두 목적의 추론 엔진으로
컴파일했다".

## MUS/CP-SAT core 대비 그래프 계층의 실제 이점

CP-SAT 의 assumption core / MUS 산출물은 "제약 A·B·C 를 함께 켜면 infeasible"이다(최소 보장도
별도 최소화 필요). 그래프 계층이 이보다 의미 있으려면 **결과가 달라야** 한다:

| # | 이점 | MUS 와의 차이 |
|---|---|---|
| 1 | **도메인 의미 있는 정량 certificate** | core 는 literal/reified 수준. 그래프는 처음부터 야간회복·13~15일 커버리지·Senior 최소인원 같은 업무 객체로 연산 → capacity/demand/deficit/boundary state/제거된 transition 등 **솔버 core 에 없는 중간 추론값** 보존 |
| 2 | **해결 이전(부분설정)에도 사용** | MUS 는 전체 infeasible 판정 후 작동. 그래프는 "이 조건을 추가하면 13~15일 야간 부족" 같은 **interactive feasibility guard** |
| 3 | **독립 모델 검증기** | 같은 모델에서 나온 solver core 는 그 모델의 인코딩 버그를 못 잡음(회복 제약 누락 시 solver=feasible). 다른 추론방식(상태DAG/frontier)이 같은 원문 규칙을 검사 → 인코딩 오류 탐지 |
| 4 | **행동 가능한 검증된 repair** | 충돌 제약집합이 아니라 "A의 14일 D 고정 해제 / B의 13일 N 금지 해제"를 내고 재검증 |
| 5 | **솔버 가속** (가장 중요한 방어논리) | certificate 가 설명에만 쓰이면 "두 번 푼다" 공격 성립. 대신 다음 실행에 사용: impossible component→solver 생략, separator→branching hint, infeasibility proof→nogood/valid cut, feasible boundary message→domain 축소, repair 후보→제한 재탐색. **Logic-Based Benders** 처럼 subproblem proof 로 master 에 cut 추가 = 중복 계산기가 아니라 solver 의 inference engine |
| 6 | **솔버 교체 내성** | certificate 계약(group·capacity·demand·deficit·antecedents·witness·boundary relation)이 독립적이면 밑의 exact solver(CP-SAT/MIP/SAT/PB)를 바꿔도 설명·repair 유지 |

## 권장 파이프라인

```
1. IR 에서 CP-SAT·그래프 모델 동시 생성
2. 빠른 graph prover 실행 → certificate 즉시 발견 시 CP-SAT 생략/부분검증
3. graph FEASIBLE/UNKNOWN → CP-SAT 정상 실행
4. CP-SAT INFEASIBLE → assumption core 획득
5. core 로 그래프 범위 축소 → core 주변 component 만 frontier/conditioning
6. graph certificate → CP-SAT 로 soundness·repair 최종 검증
7. certificate → cut/nogood/domain reduction → 다음 solve/repair 재사용
```

즉 "솔버에 안 붙인다"가 아니라 **"솔버 내부 구현에는 종속되지 않지만 solver pipeline 에는 강하게
결합한다."**

## 세 구조 비교

| 구조 | 장점 | 약점 |
|---|---|---|
| CP-SAT MUS/core 만 | 구현비용 낮음, exact solver 재사용 | 제약집합 중심, 정량 병목·repair 약함 |
| 완전 독립 graph solver | 독립성·연구성 | 중복 구현·유지비·"두 번째 솔버" 비판 |
| **공통 IR + graph certificate + CP-SAT fallback** | 설명·독립검증·가속·repair | IR/compiler 설계 필요 |

→ **세 번째가 가장 방어력이 높다.**

## 연구 기여 표현

- 약함: "We propose a solver-independent alternative to CP-SAT for infeasibility diagnosis."
  ("왜 솔버를 다시 만들었나?"를 부름.)
- 강함: **"A solver-agnostic, proof-producing domain inference layer compiled from the same
  roster constraint specification as the optimization model, generating quantitative
  certificates and boundary messages that support presolve pruning, semantic explanation,
  and solver-verified repairs."**

목표는 "solver 대체"가 아니라 **"CP-SAT 이 해결을 맡고, 그래프 계층은 구조적 추론·certificate·
proof composition·repair·검색 가속을 맡는다."**

## 방어를 위해 필요한 실험

- **계산**: CP-SAT only / +MUS / graph only / graph precheck+CP-SAT / certificate-guided
  repair+CP-SAT 를 wall time · branch·conflict 수 · solver 호출수 · solve 생략 비율 · repair
  재탐색 시간 · UNKNOWN 비율로 비교.
- **설명**: core 제약 수 vs certificate 의 간호사·날짜 수 · 정량 deficit 제공률 · 유효 repair
  생성률 · 수간호사 이해도.
- **유지·정확성**: IR 지원 hard constraint 비율 · CP-SAT↔graph 교차검증 · 인코딩 오류 탐지
  사례 · 미지원 제약 활성 시 UNKNOWN 보장(← scope_manifest.py 로 구현됨).

## 현재 코드와의 연결

- 이미 구현: typed certificate 계약, frontier message(boundary relation), factor 완전성 audit,
  scope_manifest(미지원→UNKNOWN), 독립 oracle 교차검증, factor-level rejection trace.
- 남음(이 포지셔닝을 실현하려면): **Canonical IR + 두 compiler**, certificate→cut/nogood
  주입(LBBD 결합), CP-SAT 를 최종 oracle·repair 검증기로 배선, portability 위한 2번째 backend
  실험, 재귀 hybrid(wide component).
