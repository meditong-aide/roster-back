# nurse_preceptee_period 설계 (프리셉터↔프리셉티 시점화)

> 목표: 프리셉터↔프리셉티 관계를 **기간(period) SSOT**로 일원화해, "기간 종료 후에도 엔진이 계속 follow하는" 버그(예: 고주성 8월 31/31 follow)를 **구조적으로 불가능**하게 만든다.
> 참조: [[NURSE_ATTRIBUTE_PERIOD_DESIGN]], 인벤토리(이 문서 §10).
> 상태: 설계 확정 대기 (§2 결정 항목 컨펌 후 구현).

---

## 0. 버그 요약 (왜 이 작업인가)

- `flush_expired_preceptees`는 벽시계(`today`) 기준이라, 생성 시점이 종료일(7/14) 이전이면 `nurses.preceptor_id`가 안 풀림.
- 그 달 종료는 `preceptee_period_by_nurse_id`의 "빈 set" 신호로만 강제되는데, 그 신호 생성이 (a) `_assignments` 월-제외 + (b) step2 fallback 가드 스킵으로 이중 누락.
- 솔버 membership이 시점-무시 캐시 `preceptor_id`에서 재유도되고, period entry 없으면 default가 "전체월 follow".
- **근본 결함: 관계의 "언제(WHEN)"는 assignment에, "누구(WHO)"는 캐시에 흩어져 있고, 엔진은 캐시로 membership을 판단한다.**

---

## 1. 핵심 원칙

1. **`nurse_preceptee_period` = SSOT.** WHO(preceptor_id) + WHEN([valid_from, valid_to))를 한 row에 담는다.
2. **`nurses.preceptor_id` = "as-of 오늘"의 단방향 투영(one-way cache).** period→cache upsert만, 역방향 금지. ([[attribute-period-one-way-cache]] 패턴)
3. **읽기는 의미에 따라 둘로 분리:**
   - "현재 상태" 의미(팀 분류, 재분배, 에이전트 표시 등) → **캐시 그대로 읽어도 됨**(캐시가 곧 as-of-now 투영).
   - "특정 대상월" 의미(근무표 생성, roster 출력, constraint-impact 스냅샷, repair) → **`resolve_preceptor_asof(target_month)`** 사용.
4. **`valid_to = 계획 종료일(expected_end_date)`로 모델링.** "flush 전까진 active"가 아니라 "계획상 7/14까지". → 8월 as-of 조회 시 자연히 제외되어 버그가 **구조적으로 불가능**. 무기한 = `valid_to = NULL`.
5. **엔진 follow 로직 중앙화.** 4곳 중복(§10-B)을 단일 헬퍼로 합쳐 membership+WHO+day-gate를 한 곳에서 결정.

---

## 2. 핵심 설계 결정 (컨펌 필요)

| # | 결정 | 권장안 | 대안 | 영향 |
|---|---|---|---|---|
| D1 | **assignment의 운명** | **write-through 유지**: `NurseAssignment(reason=프리셉티)`는 CRUD·알림·overlap검증 surface로 남기고 **period에 write-through**. period=읽기 SSOT, 캐시=now투영 | 완전 절연(team 전례): 라이프사이클 전부 period로 이관 | 권장안=재작성 최소(네 우려 해소). 대안=장기 정합 최상이나 대공사 |
| D2 | **status vs valid_to** | `valid_to`가 as-of 진실 + `end_reason` enum(expired/cancelled/released/preceptor_transfer) 보존(알림·감사용) | status 컬럼 유지 | 미미 |
| D3 | **1:1 강제** | active(=valid_to NULL or >today) 기준 nurse당 1행. MSSQL **filtered unique index** + 앱 가드 | 앱 레이어만 | 무결성 |
| D4 | **캐시 컬럼 운명** | 전환기엔 단방향 투영 유지 → 안정화 후 DROP 판단 | 즉시 DROP | DROP 시 36개 read-cache 전부 resolver화 필요 → 권장은 유지 |

> **권장 조합: D1=write-through, D2=valid_to+end_reason, D3=filtered unique, D4=캐시 유지.**
> 이 조합이 "버그 구조적 제거"는 달성하면서 백엔드/엔진 재작성을 최소화한다.

---

## 3. 데이터 모델

```python
class NursePrecepteePeriod(Base, EffectiveDatedPeriodMixin):
    __tablename__ = "nurse_preceptee_period"
    id            = Column(INTEGER, primary_key=True, autoincrement=True)
    nurse_id      = Column(VARCHAR(50), ForeignKey("nurses.nurse_id"), nullable=False)  # 프리셉티 본인
    preceptor_id  = Column(VARCHAR(50), ForeignKey("nurses.nurse_id"), nullable=False)  # WHO
    office_id     = Column(VARCHAR(50), ForeignKey("offices.office_id"), nullable=False) # 동일 office 검증용
    # valid_from / valid_to  ← EffectiveDatedPeriodMixin ([valid_from, valid_to))
    end_reason    = Column(VARCHAR(30), nullable=True)  # expired|cancelled|released|preceptor_transfer
    source_assignment_id = Column(INTEGER, nullable=True)  # write-through 추적(감사)
    created_at/updated_at
```

- **valid_from** = assignment.start_date, **valid_to** = expected_end_date(계획 종료) / 무기한이면 NULL.
- **as-of(month)** 판정: `valid_from <= month_end AND (valid_to IS NULL OR valid_to > month_start)` (그 달과 겹침). 겹치는 day-set은 [max(valid_from,월초), min(valid_to-1,월말)].
- **인덱스**: `ix_npp_nurse (nurse_id, valid_from)`, `ix_npp_preceptor (preceptor_id, valid_to)`, **filtered unique** `uq_npp_open_per_nurse (nurse_id) WHERE valid_to IS NULL` → nurse당 open period 1개.
- 쓰기 불변식(분할 시 valid_to 승계, autoflush=False면 flush 선행)은 [[period-write-invariants]] 헬퍼 재사용.

---

## 4. 쓰기 경로 (모든 write/lifecycle site 매핑)

| 현재 site | 동작 | period 대체 |
|---|---|---|
| nurse_service.py:2251-2252 (create) | assignment INSERT + `preceptor_id=pid` | + `nurse_preceptee_period` INSERT (valid_from=start, valid_to=expected_end). 캐시는 투영으로 set |
| nurse_service.py:2315-2323 (cancel) | cancel assignment + (잔여 없으면)`preceptor_id=None` | period **close**(valid_to=today, end_reason=cancelled). 캐시 투영 |
| assignment_service.py:1668-1710 `flush_expired_preceptees` | exp_end<today → completed + 캐시 NULL | period는 이미 valid_to=exp_end라 **as-of 자동 종료**. flush는 **캐시 투영 동기화 + 알림**만 담당 |
| assignment_service.py:1749-1775 orphan cleanup | 캐시=NULL인데 active assignment 정리 | period 기준 orphan 탐지로 전환(또는 write-through라 orphan 자체가 감소) |
| assignment_service.py:1527-1533 `_release_preceptor_subtree` (프리셉터 병동이동) | preceptee 일괄 `preceptor_id=None` | preceptor_id=X인 **모든 open period 일괄 close**(end_reason=preceptor_transfer) |
| assignment_service.py:1848-1969 permanent_change(release_preceptor) + flush | payload bridge로 지연 NULL | flush 시 period close(valid_to=발효일). payload는 감사용 유지 |
| team_classify_service.py:142,375-409 release | `preceptor_id=None` | period close(end_reason=released), team변경과 동일 트랜잭션 |
| excel_service.py:1550 bulk import preceptor_id=None | 일괄 캐시 NULL | period close write-through |

> 1:1/overlap/office 검증(nurse_service.py:2217-2240, 2268-2269, 2310-2311)은 **period active 행 기준**으로 재작성(§2 D3).

---

## 5. 읽기 경로

**단일 resolver 신설** (`services/preceptee_period.py`, nurse_period_resolver 패턴):
```python
def resolve_preceptor_asof(db, nurse_ids, as_of: date) -> dict[str, str|None]   # 프리셉티→프리셉터
def resolve_preceptees_asof(db, preceptor_ids, as_of: date) -> dict[str, list]  # 프리셉터→프리셉티들
def resolve_preceptee_days_for_month(db, group/nurses, year, month) -> dict[str, dict] # {nid:{preceptor_id, days:set}}
```

**36개 read-cache site 처리 방침:**
- **"현재 시점" 의미 → 캐시 유지(무변경):** team_auto_assign(전부 NurseInput 입력 추상), team_classify_service(현재 풀), ward_redistribute(`_eff_preceptor`는 as-of date 받으면 resolver로, 그 외 현재풀은 캐시), agents_v2/nurse_tools(표시), precheck 입력 struct. → 캐시가 as-of-now 투영이므로 정확.
- **"대상월" 의미 → resolver로 교체(필수):**
  - roster_create_service.py:4812-4871 `_preceptee_period` 빌드 → `resolve_preceptee_days_for_month` 호출로 교체(캐시·assignment 직독 제거).
  - roster_service.py:1509 (roster 출력 dict) → as-of(해당월).
  - constraint_impact/snapshot_builders.py:48,92-106 → as-of(스냅샷 월).
  - repairs/grade_repair.py:351 → as-of(대상월).
  - precheck/runtime_bridge.py:244-264 → payload 대신 as-of 조회.

---

## 6. 엔진 통합 (핵심 — WHO+WHEN 일원화 + 중복 제거)

**6-1. config map에 WHO 포함.** 현재 `preceptee_period_by_nurse_id = {nid: set(days)}` → **`{nid: {"preceptor_id": pid, "days": set}}`** 로 확장. WHO를 캐시가 아니라 이 map에서 받는다.

**6-2. 솔버 membership/follow 중앙화.** 중복 4곳(§10-B)을 단일 헬퍼로:
```python
def build_preceptee_context(rs, config_map) -> dict[int, tuple[int, set[int]]]:
    # {preceptee_idx: (preceptor_idx, follow_day_set)} — config_map(period 유래)만 신뢰. 캐시 미사용.
```
- cp_sat_basic.py:2680-2718, fallback_lex.py:340-386, postprocess_off.py:251-266, feasibility_alerts.py:276-338 → 모두 이 헬퍼 호출로 교체.
- **default 분기 제거:** "period entry 없음 → 전체월 follow"(cp_sat_basic.py:2713/2715, fallback_lex.py:382-383) 삭제. map에 있는 자만 preceptee. 무기한 follow는 map에 `days=전체월`로 **명시**(full-month-delete 해킹 제거, cp_sat_basic.py:2688-2690).

**6-3. ungated 경로 게이팅(현재 버그).** preceptor_id 직독 + period 미게이팅:
- pair-preference: cp_sat_basic.py:1408-1435, 1685-1702 → `build_preceptee_context`로 active 여부 + day 게이팅.
- fixed-copy: cp_sat_basic.py:1609-1645 → `_follow_set` 미적용 루프를 day 게이팅.
- fallback copy: fallback_lex.py:3350-3404 → day 게이팅(현재 전체월 복사).
- objective: fallback_objectives.py add_preceptor_terms_fn → membership 체크 후 호출.

**6-4. WHO 소스 전환.** cp_sat_basic.py:790/1516/1625, fallback_lex.py:345 등 `getattr(nu,'preceptor_id')` → context의 preceptor_idx 사용. (캐시 직독 제거)

> 결과: 고주성 8월 = period에 8월과 겹치는 행 없음 → context에 미포함 → membership/pair/fixed/objective 어디서도 안 잡힘. flush·캐시 무관.

---

## 7. 마이그레이션 / 백필 / 전환

1. **DDL**: `nurse_preceptee_period` 생성(+인덱스). `f7fded2`처럼 ORM + 별도 마이그레이션.
2. **백필**: 기존 active `NurseAssignment(reason=프리셉티)` → period row(valid_from=start, valid_to=expected_end, preceptor_id=`nurses.preceptor_id`, source_assignment_id). `nurses.preceptor_id` set인데 assignment 없는 비대칭도 흡수.
3. **전환(dual)**: 한동안 write-through(assignment+period+캐시 동시), 읽기는 §5대로 점진 전환. resolver 결과 vs 캐시 불일치 로깅으로 검증.
4. 안정화 후 D4(캐시 DROP) 판단.

---

## 8. 알림 / 스케줄러 / lazy flush

- 알림 S06(시작)/S07(취소)/S13(종료, assignment_service.py:1716-1740,539-548,871-887)은 write-through 시점에 그대로 발화. 종료 알림은 flush가 period close 동기화할 때.
- `flush_expired_preceptees`(main.py:80 cron + nurses.py:333 lazy)는 **캐시 투영 동기화 + 종료 알림** 역할로 축소(as-of 진실은 이미 period가 보유). lazy flush 성능은 `(valid_to)` 인덱스로 보전.

---

## 9. 테스트

- 픽스처 dual-write: conftest.py:188, test_team_classify.py:180-182(_set_preceptor), test_ward_redistribute.py:380, test_permanent_change.py:100,116 → period row도 INSERT(헬퍼 1곳 수정으로 전파).
- **신규 회귀(필수):** "종료된 프리셉티 + 같은 병동 무기한 프리셉티 공존 → 종료자는 미-follow/미-pair/미-fixed, 무기한자는 follow" + "as-of 월 경계(종료월/다음월)" + "프리셉터 병동이동 시 subtree 일괄 close".

---

## 10. 인벤토리 (전수, file:line)

### A. write/lifecycle/cache
- 캐시 write: nurse_service.py:2252,2323 / assignment_service.py:1528-1532,1697-1710,1962 / team_classify_service.py:142 / excel_service.py:1550
- assignment write: nurse_service.py:2251,2315 / assignment_service.py:394-550,681-823
- lifecycle: flush_expired(1668-1746)+main.py:80+nurses.py:333 / orphan(1749-1775) / permanent_release(1848-1969) / subtree(1527-1533)
- 검증: 1:1(nurse_service.py:2225-2240) / ownership(2268-2269,2310-2311) / office(2217-2221) / cleanup guard(2316-2321)
- 알림: S13(1716-1740) / S06(539-548) / S07(871-887)
- 스키마: models.py:88(preceptor_id), 295-328(NurseAssignment), 238-254(EffectiveDatedPeriodMixin)

### B. engine/solver (★중복 = 중앙화 대상)
- MEMBERSHIP ★: cp_sat_basic.py:2680-2718 / fallback_lex.py:340-386 / postprocess_off.py:251-266 / feasibility_alerts.py:276-338
- WHO(캐시직독): cp_sat_basic.py:790,1516,1625 / fallback_lex.py:345
- FOLLOW 미게이팅(버그): pair cp_sat_basic.py:1408-1435,1685-1702 / fixed-copy 1609-1645 / fallback copy fallback_lex.py:3350-3404 / objective fallback_objectives.py:40-431
- map 빌드/읽기: roster_create_service.py:4812-4871(빌드) / cp_sat_basic.py:1054-1073(읽기)
- config-param(무변경): roster_config.py:117-124 / cp_sat_basic.py:585-591 / preceptor_gauge roster_create_service.py:4206-4237,4703
- postprocess 보호: postprocess_off.py:470-475,665-666 / cp_sat_basic.py:1502-1575(PrecepteeSync)
- fixed_wanted map: roster_create_service.py:5134-5174 / cp_sat_basic.py:1166-1181,1537-1543

### C. 주변 consumer + test
- READ-cache(현재시점=유지): team_auto_assign.py:134-135,231,236,279,284,313-319,385,390,468 / team_classify_service.py:44,58,204-206 / ward_redistribute_service.py:146-156,305-309 / weekly_off_service.py:357,365 / agents_v2/nurse_tools.py:75,77,96,320-341,576
- READ(대상월=resolver): roster_service.py:1509 / constraint_impact/snapshot_builders.py:48,92-106 / simulation.py:133-145 / graph_builder.py:80 / atoms.py:74 / repairs/grade_repair.py:351 / precheck/runtime_bridge.py:244-264, team_grade_precheck.py:949-1008
- TEST 픽스처: conftest.py:188 / test_team_classify.py:180-240 / test_ward_redistribute.py:380-509 / test_permanent_change.py:100-125 / test_selection_matrix.py / test_team_auto_assign_fixed.py:97 / test_alpha_enriched_checks_dynamic.py
- DISPLAY/AGENT: nurse_tools.py:96 / prompt_builder.py:203 / skills/descriptions.py:624,656 / ontology.py:3019

---

## 11. 롤아웃 단계

1. **(stop-gap, 선택)** step2 per-nurse 보충 ~10줄로 라이브 8월 버그 즉시 완화.
2. DDL + 백필 + write-through(D1) + resolver 신설.
3. 엔진 §6 전환(map에 WHO, 중앙 헬퍼, default 제거, ungated 게이팅) + 회귀테스트.
4. 대상월 read 사이트(§5) resolver화.
5. dual 검증 안정화 → (선택) 캐시 DROP(D4).
