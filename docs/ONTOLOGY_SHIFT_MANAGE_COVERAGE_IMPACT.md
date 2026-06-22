# Ontology Impact — shift_manage / shifts 정합화가 온톨로지에 주는 영향

**작성일:** 2026-06-18
**상태:** 분석 완료 (코드 수정은 별건으로 완료·미커밋, 본 문서는 온톨로지 관점 분석)
**범위:** `shift_manage`(슬롯별 manpower=커버리지 수요·codes) / `shifts`(근무코드) 정합화 작업이 본 레포의 **두 온톨로지 시스템**과 어떻게 교차하는가.

관련 작업(코드 수정): ShiftManage PK=id + UNIQUE(office,group,nurse_class,slot), GET 자동생성 race-guard+클래스 화이트리스트, remove cascade(orphan code 정리), 로더 결정화(최대 id manpower·codes 합집합), DB팀 dedup 스크립트(`scripts/dedup_shift_manage.py`)+UNIQUE DDL(`scripts/shift_manage_unique.sql`).

---

## 0. 본 레포의 "온톨로지"는 둘이다

| 온톨로지 | 위치 | 역할 | 본 작업과의 관계 |
|---|---|---|---|
| **제약-임팩트 / MCS 온톨로지** | `app/services/constraint_impact/`(라이브) · `app/services/ontology_graph/`(US-001 통합, 마이그레이션 중·미배선) · `app/services/semantics/ontology.yaml` | 솔버 제약을 노드/엣지로 모델링 → 충돌(MCS/MUS) 진단·완화 추천 | **직접 교차** — manpower=커버리지 수요가 1급 입력 |
| **에이전트 그라운딩 온톨로지** | `.aide/DOMAIN_KNOWLEDGE.md` · `app/agents_v2/skills/descriptions.py` | LLM이 자연어→도메인 개념을 그라운딩 | **갭 노출** |

---

## 1. 결정적 연결 — manpower 는 온톨로지의 1급 시민 `required`

통합 스키마(`docs/UNIFIED_ONTOLOGY_GRAPH_SCHEMA.md:54-57`)의 대표 인과 경로:

```
state:supply_demand(day, N){required=3, available=2, shortage=1}
   --pressures--> constraint:coverage_min(day, N)
   --mitigates--  action:set_threshold(daily_N[day] -1) | data_correction_required
```

여기서 **`required`(필요 인원) = shift_manage.manpower → daily_shift_requirements** 다. 라이브(constraint_impact) 빌더가 동일 source를 읽는다:

- `app/services/constraint_impact/graph_builder.py:27-37` — `req = snapshot.config_payload.get("daily_shift_requirements_by_day")` → `coverage:min:{day}:{code}` 제약 노드(`family="coverage"`) 생성. `:50` 에서 `coverage:max` 도.
- 그 값의 출처 = `app/services/roster_create_service.py:4579` `config_dict['daily_shift_requirements_by_day'] = daily_shift_requirements_by_day` (= `_build_shift_manage_and_requirements`, roster_create_service.py:654 산출물 — **이번에 결정화한 로더**).
- 완화 제어도 동일 source: `constraint_impact/control.py:129,386` 가 `daily_shift_requirements_by_day` 를 읽어 `_apply_coverage_min`(control.py:411) 수행.

통합 레이어(`ontology_graph/`)도 동형이다: `supply_demand.py:124` `compute_supply_demand(requirements_by_day)` → `required`(:185) → `shortage = max(0, required - filled)`(:189) → `state:supply_demand` 노드(:223-232) → `builder.py:75` `build_unified_graph`.

> ✅ **결론: 본 수정은 온톨로지 코드를 건드리지 않고도 온톨로지의 ground truth(`coverage:min` 의 need, `supply_demand.required`)를 자동 교정한다.** 단일 upstream(daily_shift_requirements)을 공유하기 때문.

---

## 2. 중복 버그의 "온톨로지적 의미" — 오염이 진단까지 전파됐다

중복행(149행/19슬롯 manpower 충돌, 예 D=3 vs 12)은 솔버뿐 아니라 **온톨로지 진단 전체**를 오염시키고 있었다:

1. **`required` 비결정 오염** → `shortage = required − filled`(supply_demand.py:189) 왜곡 → `pressures` 엣지·병목셀(bottleneck_cells, builder.py:203) 오판 → **MCS/충돌 진단이 틀린 제약을 지목** → 잘못된 완화 추천(`set_threshold` 로 엉뚱한 날 수요 조정).
2. **codes 손실(흩어진 dedup)** → code2main 정규화 실패 → `constraint_impact/atoms.py:92-94` `counts_to_coverage`(D/E/N/M 만 커버리지 집계) 오판 → 커버리지 atom 이 실제 근무를 잘못 셈.

즉 **온톨로지 정확도가 shift_manage 데이터 위생에 직접 의존**하고 있었고, 본 수정이 그 토대를 바로잡는다.

---

## 3. 정합점 — 본 작업 = 온톨로지가 이미 정의한 `data_correction_required` action

온톨로지엔 완화 action 종류로 **`data_correction_required`(데이터 정정 필요, manual-only, 비가역 cost 3.0)** 가 이미 존재한다:

- `app/services/ontology_graph/schema.py:55` (ActionType), `builder.py:40` (INVASIVENESS 3.0), `structural.py:40`/`mus_bridge.py:95` (reversible=False)
- `app/services/treatment_applicator.py:43,177` — `data_correction_required` 는 auto 적용 안 됨(manual_required 로 분리)
- `app/routers/ontology.py:1701,1857,3137` — API/FE 노출

솔버가 런타임 완화 불가로 판단하면 온톨로지가 "데이터를 고쳐라"라고 권고하는 종류다.
→ **이번 dedup/UNIQUE 작업이 바로 그 `data_correction_required` action 의 실현체**다. 온톨로지가 가리키던 "정정 필요" 영역을 실제로 정정한 셈.

---

## 4. 온톨로지 관점 근본원인 = 개념 중복(redundant encoding)

버그의 진짜 뿌리는 **하나의 개념이 여러 표현으로 흩어져 단일 진실원천이 없던 것**이다:

| 개념(온톨로지상) | 흩어진 물질화 | 결과 |
|---|---|---|
| 근무의 메인 카테고리 | `shift_gb`(한글)·`default_shift`·`main_code`·`shift_slot` **4중 인코딩** | 동기화 누락 → orphan / drift |
| 슬롯의 코드집합(`codes`) | Shift.shift_gb 에서 **파생 가능한데 명령형으로 비정규화 보관** | remove cascade 누락 = orphan |
| 커버리지 수요(`required`) | shift_manage.manpower · DailyShift · RosterConfig(day_req/eve_req/nig_req) **3중 물질화** | last-wins drift |
| ShiftManage 정체성(identity) | 모델 복합 PK vs 실DB id, UNIQUE 부재 | 중복 자유 |

본 수정은 이 중 **identity(UNIQUE)·수요 결정성·codes 무결성(cascade/union)** 을 강제했다(개념 redundancy 자체 제거가 아니라 무결성 강제).

---

## 5. 영향 및 후속

- ✅ **온톨로지 코드 변경 불필요**: coverage 값은 upstream(daily_shift_requirements) 상속이라 자동 교정.
- ✅ **온톨로지 테스트 무회귀**: 전체 1378 pass 에 `test_mcs.py`·`test_ontology_*.py`·`test_graph_fully_connected.py` 포함. (실패 6건은 전부 team-flush 도메인, 온톨로지 무관.)
- ⚠️ **후속 권고**:
  1. **에이전트 그라운딩 갭**: `.aide/DOMAIN_KNOWLEDGE.md` 에 Shift만 있고 **shift_manage/슬롯/manpower(커버리지 수요) 개념이 없음**. 그런데 에이전트는 `app/agents_v2/tools/constraint_tools.py:109` `update_shift_manage_manpower` 로 manpower 를 수정한다(스킬 설명 `descriptions.py:549` 엔 존재). 도메인지식↔스킬 불일치 → DOMAIN_KNOWLEDGE 에 "슬롯별 커버리지 수요(shift_manage)" 개념 추가 권장.
  2. **US-001 통합 그래프 미배선**: `ontology_graph/builder.py` `build_unified_graph` 는 app 내 호출처 0(마이그레이션 중). 배선 시 `UnifiedGraphInput.requirements_by_day` 도 반드시 같은 `daily_shift_requirements_by_day`(결정화된 source)를 쓰도록 해야 함(별도 재계산 시 결정성 회귀).

---

## 부록 — 핵심 file:line

- `docs/UNIFIED_ONTOLOGY_GRAPH_SCHEMA.md` — 4종 노드(constraint/domain_object/state/action) + 엣지 스키마, coverage 인과 경로.
- `app/services/constraint_impact/graph_builder.py:27-51` — **coverage:min/max 노드 생성(라이브), daily_shift_requirements_by_day 소비**.
- `app/services/constraint_impact/control.py:129,386,411` — coverage 완화 제어.
- `app/services/constraint_impact/atoms.py:92-94` — counts_to_coverage(D/E/N/M).
- `app/services/ontology_graph/supply_demand.py:124-232` — supply_demand(required/filled/shortage) max-flow + state 노드.
- `app/services/ontology_graph/builder.py:75-233` — 통합 그래프 빌더(미배선) + data_correction_required action.
- `app/services/ontology_graph/schema.py:43,55` — supply_demand state / data_correction_required action 정의.
- `app/services/treatment_applicator.py:43,177` — data_correction_required = manual-only.
- `app/services/roster_create_service.py:654,4579` — 커버리지 수요 단일 source(결정화된 로더).
- `.aide/DOMAIN_KNOWLEDGE.md` — 에이전트 그라운딩(shift_manage 개념 부재).
