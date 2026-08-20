# 에이전트/온톨로지/로깅 write-surface 하네스 구조

> **목적**: roster 백엔드에서 "에이전트·온톨로지·로깅이 DB를 건드리는 모든 접촉면"과, 그 값이
> 근무표(솔버)에 어떤 영향을 주는지, 그리고 어떤 가드가 그것을 막는지를 한 장에 정리한다.
> 다음 작업을 진행할 환경 에이전트가 **어떤 테이블·필드를 어떤 관점으로 검토해야 하는지**의 기준 문서.
>
> 근거는 전부 코드 확인 기반(2026-07-21 시점). 심볼명으로 재확인 권장(라인은 밀릴 수 있음).

---

## 0. 판정 축 — "라이브 솔버가 읽는가"

모든 위험 판정의 기준은 **라이브 솔버 경로**다.

```
roster_config (DB row)
   └─ config_dict = latest_config.__dict__          # DB 컬럼만
        └─ CPSATBasicEngine.create_config_from_db()  # 명시 kwarg → 폴백 .get(default)
             └─ NurseRosterConfig (dataclass)         # 솔버가 실제로 읽는 것
                  └─ generate_roster()
                       ├─ [SKIP] primary: _optimize_with_enhanced_constraints  ← 기본 스킵!
                       └─ [LIVE] fallback: _optimize_fallback_lex_hard_first
                                  = fallback_lex.py + fallback_objectives.py + objective_terms.py
```

- **`SKIP_PRIMARY` 환경변수 기본 `"1"`** → primary 풀모델은 **스킵**. 실질 솔버 = **fallback_lex(렉시)**.
- 따라서 **primary 전용으로만 소비되는 config는 死(INERT)** — 값을 바꿔도 근무표 불변.
- `create_config_from_db`가 dataclass에 담지 않는 DB 컬럼은 솔버가 못 본다.

### 위험 유형(taxonomy)
| 유형 | 정의 | 조치 |
|---|---|---|
| **INERT** | 저장/주입되나 라이브 솔버가 안 읽음 → 값 무의미 | 컬럼 제거(순수 정리) |
| **CONSTANT-LIVE** | 솔버가 읽지만(live) 운영 전량 상수(아무도 안 바꿈) | 에이전트 화이트리스트 제외(값 고정) + (선택)컬럼 DROP |
| **INFEASIBLE-TRIGGER** | 유효해 보이는 값이 생성 시 infeasible 유발 | 저장 시점 게이트로 차단 |
| **QUALITY** | 수정 시 근무표 품질/공정성 실제 영향 | 정당 config면 허용, 무결성만 가드 |
| **HARD-LOCK** | 8대 안전 하드제약 토글 | ★HN 정당 설정(끄는 병동 실재) → 잠그지 말 것. 에이전트 무결성만 |
| **BENIGN** | 라벨/이름/메타/읽기전용 | 무위험 |

---

## 1. 쓰기 위험은 전량 `app/agents_v2/`

**온톨로지·precheck·로깅은 전부 READ-ONLY → 쓰기 위험 0.**
- `routers/ontology.py` = GET 17개만, `from db.models` import 0, POST/PUT/DELETE 0. 진단 대시보드.
- `services/ontology_pool.py` = DB 접근 0(전달 데이터로 계산).
- `services/precheck/*` = 생성 전 검증(payload 기반). 쓰기 0. **오히려 게이트 제공자**.
- `call_action_catalog.py`(106 EP) = `fields`(비민감 화이트리스트 값 로깅) / `masked`(민감필드 존재여부만 `***`). PII 마스킹 설계됨. 신규 EP가 PII를 `fields`에 넣지만 않으면 안전.

→ 검토 대상은 **agents_v2 도구층뿐**.

## 2. 에이전트 write-surface 맵 (검토 스코프)

| 테이블 | 도구 (`agents_v2/tools/`) | 위험 | 우선 | 상태 |
|---|---|---|---|---|
| **RosterConfig** | `constraint_tools.update_roster_config` | 자유 setattr → 스코프손상·상수값·하드락 | P0 | ✅ **가드 완료**(화이트리스트+범위+상수-live 제외) |
| **FixedWantedEntry** | `wanted_tools.bulk_update_wanted_adjustments` | 자유 setattr(LLM `field`) → shift_id/is_applied 임의변경 | P0 | ✅ **가드 완료**(is_applied/reason/memo만 허용) |
| **ShiftManage** | `constraint_tools.update_shift_manage_manpower` | manpower 값범위 무검증 → 과다=infeasible | P1 | ⬜ 미착수 |
| **Nurse (+ period 위성)** | `nurse_tools.update_nurse_attributes_batch` | 화이트리스트·검증 있음. 단 allowed_shifts에서 N제거 시 기존 n_exact와 역방향 정합 없음 | P1 | 🟡 부분 |
| **ScheduleEntry** | `schedule_tools.update_schedule_entry` | 셀 직접 덮어쓰기 → 하드제약 사후검증 없음(ND/NE/6연속 우회) | P1 | ⬜ 미착수 |
| **WantedRequest/NurseShiftRequest/NursePairRequest** | `wanted_tools` (CRUD) | 선호 왜곡(품질). 즉시 infeasible 아님 | P2 | ⬜ |
| **NurseMonthlyLimit** | `nurse_monthly_limit_tools.upsert_monthly_limit` | non-N에 N한도 = infeasible | ✅ | ✅ **게이트 완료**(모범) |
| RosterGradeConfig / Wanted / RosterJob / Shift | 읽기전용 / 메타 | benign | P3 | — |

전체 writer 인벤토리(grep 확정): `constraint_tools`(update_roster_config, update_shift_manage_manpower) · `generation_tools`(create_generation_job) · `nurse_monthly_limit_tools`(upsert) · `nurse_tools`(batch) · `schedule_tools`(update_entry) · `wanted_tools`(add/modify/delete/cancel/deadline). 스킬층은 직접 커밋 없음(도구 위임).

---

## 3. 가드 패턴 (복제할 모범)

### 3-A. constraint_tools 무결성 가드 (RosterConfig 쓰기)
`_validate_config_field(field, value)` — 3겹:
1. **화이트리스트** `_AGENT_SETTABLE_FIELDS` — 정책/요구/하드락 토글만 허용. 식별·스코프·타임스탬프 컬럼(`config_id/office_id/group_id/version/created_at/updated_at`)과 상수-live 값은 제외. **신규 컬럼은 명시 추가 전까지 차단(fail-safe)**.
2. **필드 존재** `hasattr` — 오타 반려.
3. **값 범위** `_NUMERIC_BOUNDS` — 숫자 sane 범위(음수·폭주 차단). 정책 아님, 무결성.
> ★하드락 토글(`banned_day_after_eve` 등)은 **허용**한다 — prod에서 병동별로 끄는 사례 실재(정당 config). 잠그면 기능 제거.

### 3-B. monthly_limit work_shifts 게이트 (NurseMonthlyLimit 쓰기)
`precheck/monthly_limit_validator._check_work_shifts` — 근무유형(allowed_shifts)에 없는 시프트에 `{p}_min/max/exact` 양수 → 차단. PUT 경로 + **에이전트 도구(`upsert_monthly_limit`) 양쪽**에 적용(에이전트 우회 방지). non-N 간호사에 N한도 저장 = infeasible 원천 차단.
> 이 "쓰기 경로마다 동일 게이트"가 다른 자유-setattr 도구(FixedWantedEntry 등)에 복제할 패턴.

---

## 4. 완료된 정리 (2026-07-20~21)

- **INERT 제거**(roster_config): `even_nights` `weekend_shift_ratio` `patient_amount` `config_version` `off_placement_mode` + **team_balance 계열 전체**(운영 100% off) + **preceptor 소프트보너스**(셋업만 제거, `_add_preceptor_objective_terms` 함수는 범용 고가중 페어 보너스로 존치). → `git 5c1c3bc`. ★DB DROP은 `scripts/drop_inert_columns.sql`(사용자 실행, dev `preceptor_gauge` NOT NULL landmine 선처리).
- **INFEASIBLE 게이트**(NurseMonthlyLimit): non-N N한도 차단. → `git ded3510`.
- **무결성 가드**(constraint_tools): 위 3-A. → 미커밋.
- **보존 원칙**: `preceptee_on` 하드팔로우 · `team_min` · `config_id` FK(역추적) · `version`(int 프리셋) · 하드락 토글 · 일반 페어링 objective.

## 5. CONSTANT-LIVE 후보 (prod 데이터 기준 · 검토 대기)

솔버가 읽지만 prod 전량 상수 → 에이전트가 건드릴 이유 없음. **화이트리스트 제외는 완료(무위험)**, 컬럼 DROP은 별도(DDL/FE/probe 얽힘).

| 컬럼 | prod | 소비 | DROP 시 얽힘 |
|---|---|---|---|
| `shift_priority` | 578/578 = 0.8 | objective_terms(커버리지부족 가중) | FE schema/echo 노출 |
| `ban_night_before_fixed_off` | 578/578 True | fallback_lex | constraint_impact **probe 레버** |
| `use_dynamic_scaling`(grade) | 20/20 True | grade_constraints | 에이전트 read-only |
| `allow_soft_fallback`(grade) | 0/20 (항상 하드) | grade_constraints | 에이전트 read-only |
| `null_grade_policy`(grade) | 전부 "LOWEST" | grade_service | 에이전트 read-only |

> **하드락 토글은 상수 아님**(varied): `banned_day_after_eve` OFF 17, `three_seq_nig` OFF 23, `not_one_night` OFF 163, `nod_noe` OFF 154 등 — 정당 config, 유지.

## 6. 다음 검토 착수 순서

1. ✅ ~~P0 — `wanted_tools.bulk_update_wanted_adjustments`~~ (완료: is_applied/reason/head_nurse_memo만 허용, shift_id/정체성 차단).
2. **P1 — `schedule_tools.update_schedule_entry`**: 8대 하드제약 사후검증 훅(셀 편집으로 ND/NE/6연속 생성 차단).
3. **P1 — Nurse allowed_shifts ↔ NurseMonthlyLimit 역방향 정합**: 근무유형 변경 시 기존 월한도 재검증(`_check_work_shifts` 역적용).
4. **P1 — ShiftManage.manpower 값범위 검증**.
5. **P2 — 원티드 CRUD 품질가드** / **P3 — 상수-live 컬럼 DROP(hygiene)** / 온톨로지·로깅 = 신규 EP 리뷰 체크리스트만.

---

## 참고 파일 (절대경로 아님, roster-back 기준)
- `app/agents_v2/tools/constraint_tools.py` — RosterConfig/ShiftManage 쓰기 + 무결성 가드
- `app/agents_v2/tools/nurse_monthly_limit_tools.py` — 월한도 + work_shifts 게이트
- `app/agents_v2/tools/wanted_tools.py` — 원티드/고정원티드 (P0 미착수)
- `app/agents_v2/tools/schedule_tools.py` · `nurse_tools.py`
- `app/services/precheck/monthly_limit_validator.py` — 게이트 로직(`_check_work_shifts`)
- `app/services/cp_sat_basic.py` — `create_config_from_db`, `SKIP_PRIMARY`, generate_roster
- `app/services/cp_sat/fallback_lex.py` · `fallback_objectives.py` · `objective_terms.py` — 라이브 솔버
- `scripts/drop_inert_columns.sql` — inert 컬럼 DROP DDL(사용자 실행)
