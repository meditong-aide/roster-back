# 룩어헤드(Lookahead) 기획·아키텍처 문서

## 문서 목적

- **룩어헤드**: 당월을 풀 때 "다음 달 1~K일"을 가상 일자로 포함해, **월말 꼬리**가 다음 달 초 O/N 몰림·infeasible을 줄이기 위한 제약 반영.
- 이 문서는 **구현 명세**이자 **설계 의사결정 기록**으로, 피드백 반영 내용을 모두 포함한다.

### 관련 설계 문서

- 제약 의미 계층 + 하이퍼그래프 확장 계획은 `ONTOLOGY_GROUNDED_CONSTRAINT_IMPACT_GRAPH_PLAN.md` 참조
- precheck/진단 구조는 `INFEASIBLE_DIAGNOSTICS_FRONT_BACK_ARCHITECTURE.md` 참조
- 팀/등급 deterministic precheck 카탈로그는 `TEAM_GRADE_INFEASIBILITY_PRECHECK.md` 참조

---

## 1. 목표·역할 정의

### 1.1 룩어헤드의 목적 (한 가지로 고정)

- **룩어헤드는 "당월 꼬리 설계 개선용 제약"으로만 사용한다.**
- 즉, **이번 달 꼬리를 조정**해서 다음 달 초에 강제되는 O/N 패턴을 줄이는 것이 목적이다.
- **룩어헤드 구간의 해(해답)는 DB에 저장하지 않으며**, 다음 달을 풀 때 "룩어헤드 해"를 고정·재사용하지 않는다.
- 다음 달 생성 시에는 **저장된 당월 스케줄의 꼬리(마지막 N일 shift history)** 만 `build_cross_month_constraints` 등으로 초기 조건으로 넘긴다.

**정리**: "다음 달 1~K일을 사실상 선계획으로 고정해 쓴다"는 설계는 하지 않는다. 룩어헤드는 **꼬리 안정화 전용**이다.

---

## 2. 용어·기호 정의 (충돌 방지)

| 기호 / 용어 | 의미 | 비고 |
|-------------|------|------|
| **D_phys** | 당월 물리 일수 | `rs.num_days` (28~31) |
| **K_lookahead** | 룩어헤드 일수 | 0 / 2 / 3 / **7**(권장) |
| **D_ext** | 확장 일수 | `D_phys + K_lookahead` (비활성 시 `D_ext = D_phys`) |
| **물리 구간** | `d ∈ [0, D_phys)` | 출력·DB 반영 구간 (0-based) |
| **룩어헤드 구간** | `d ∈ [D_phys, D_ext)` | 가상 "다음 달 1일, 2일, …" |
| **L_work_max** | 연속 근무 상한(일) | `max_consecutive_work_days` (예: 5) |
| **L_night_max** | 연속 야간 상한(일) | `max_consecutive_nights` (예: 2 또는 3) |

- **K**는 룩어헤드 길이 전용으로만 쓰고, 연속 근무/야간 상한은 **L_work_max**, **L_night_max**로 구분해 기호 충돌을 막는다.

---

## 3. 인덱스 규약 (join / leave)

- **규약**: **inclusive**
  - `join[n]`: n번 간호사 **첫 근무일(0-based)**
  - `leave[n]`: n번 간호사 **마지막 근무일(0-based)** (당월 물리만)
  - 유효: `join[n] ≤ d ≤ leave[n]` (물리), 변수/패턴은 `leave_ext[n]`까지
  - 루프: `range(join[n], leave_ext[n] + 1)` (변수·제약), 월총량은 `month_total_day_range(T0, T1, D_phys)`
- **확장**: 룩어헤드 참여 n에 대해 `leave_ext[n]` = 확장 구간 **마지막 일자(inclusive)**. 당월 말까지 근무 가능하면 `leave_ext[n] = D_ext - 1`, 당월 중 퇴사면 `leave_ext[n] = leave[n]`.

---

## 4. 모듈 구성 (저결합·고응집)

| 모듈 | 역할 |
|------|------|
| `cp_sat/lookahead_helpers.py` | D_ext, leave_ext, physical_range, month_total_day_range (순수 함수) |
| `cp_sat/lookahead_constraints.py` | 룩어헤드 일별 OFF 상한(고정 vs 선택 분리), 분산 패널티 항 |
| `roster_create_service` | lookahead_weekly_off_cells 계산·config 전달, lookahead_days 기본값 |
| `cp_sat_basic._build_full_model` | D_ext/leave_ext 적용, 월총량 D_phys 한정, 룩어헤드 제약/패널티 호출 |

- 기존 로직은 **조건부(lookahead_days > 0)** 로만 확장되며, `lookahead_days == 0` 이면 기존과 동일하게 동작한다.

---

## 5. 월총량 — 당월 D_phys만 적용

- **원칙**: min/max OFF, max N 등 **월총량**은 **당월 D_phys만** 합산. 룩어헤드 일자는 **포함하지 않음**.
- 구현: `month_total_day_range(T0, T1, D_phys)` 로 합산 구간을 제한.

---

## 6. 룩어헤드 구간 제약 (총량 제외)

- **Cross-boundary 패턴**: 연속 근무(L_work_max), 연속 야간(L_night_max), 2N→2O/3N→2O — `T1 = leave_ext[n]` 로 확장.
- **고정 OFF**: 주말 휴무(요일 기준), **룩어헤드 주휴 = lookahead_weekly_off_cells**(당월 `weekly_off_by_idx`와 별도 계산).
- **일별 OFF 상한**: `add_lookahead_off_cap_constraints` — fixed_off_cnt vs selectable_off_cnt 분리.
- **분산 패널티**: `add_lookahead_distribution_penalty_terms` — 작은 가중치로 목적함수에 추가.

---

## 7. 설정·데이터 흐름

- **config_dict / config_data**: `lookahead_days`(기본 0), `lookahead_weekly_off_cells`, `next_month_head_requirements`(선택)
- **RosterSystem**: `num_days`(= D_phys) 유지. `lookahead_weekly_off_cells` 는 config 적용 시 `roster_system` 에 세팅.
- **해 반영**: `d < D_phys` 인 경우만 `rs.roster` 에 기록. (`_build_full_model` 은 `join`, `leave`(물리)를 반환하므로 기존 해 기록 루프 변경 없음.)

---

## 8. 구현·검증 체크리스트

- [x] join/leave inclusive, leave_ext 사용처 일관
- [x] lookahead_days=0 시 D=D_phys, leave_ext=leave 로 기존과 동일
- [x] weekly_off: 당월=weekly_off_by_idx, 룩어헤드=lookahead_weekly_off_cells
- [x] 룩어헤드 OFF cap: fixed vs selectable 분리
- [x] 기호: K_lookahead vs L_work_max, L_night_max 구분

---

*문서 끝*
