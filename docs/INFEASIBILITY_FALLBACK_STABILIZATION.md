# Infeasibility 안정화 — Fallback 하드제약 케이스 카탈로그 + Catch→선택지→적용 설계

작성: 2026-07-20. 대상: fallback lex 솔버(`cp_sat/fallback_lex.py`) + precheck(`services/precheck/`).
목적: INFEASIBLE을 **사전/즉시 캐치 → 원인 + 해결선택지 제시 → 유저 선택 → 파라미터 자동 수정 → 재생성**.

---

## 0. 결론 먼저 — 뼈대는 이미 있다. 우리가 할 건 "갭 메우기"

catch→선택지→적용 루프는 **end-to-end로 구현돼 있음**:

| 단계 | 구현체 | 산출/계약 |
|---|---|---|
| 사전 캐치 | `precheck/*` (18+ 체크) → `run_runtime_precheck` (roster_create_service.py:5444) | blocking 시 HTTP 500 `infeasibility.*` (solver 스킵) |
| 즉시 캐치 | fallback INFEASIBLE → `build_unrecoverable_payload` (payload.py:436) | HTTP 500 `infeasibility.*` |
| 원인 규명 | `structural_diagnosis` + `undiagnosed_probe`(relax-and-retry) + (dormant) MUS/max-flow | `causes[]`, `structural_diagnosis`, `fix_plan.axis_actions[]` |
| **해결선택지** | `resolution_options[]` (probe=verified / treatment=bundle / combo) | `{option_id, kind, verified, changes[], trade_off_ko, apply:{col:val}}` |
| **적용→재생성** | `POST /roster_create/apply-resolution` | `apply` 델타를 config에 반영 후 `generate_roster_service` 재호출 |

→ **선택지 하나 추가 = `undiagnosed_probe.RELAX_CATALOG`에 항목 1개 추가**하면 verified 옵션으로 자동 노출됨.

---

## 0-b. ★정정 (2026-07-20) — 온톨로지 treatment가 이미 "갭"을 커버함

**이 문서 초안의 §3 P0 갭(weekend_off/ban_n_to_d/team_min "옵션 없음")은 틀렸다.** 초안은
probe 카탈로그(`undiagnosed_probe.py`)만 보고 **온톨로지 treatment 층(`semantics/ontology.yaml`)을 누락**했다.

`ontology.yaml`은 진짜 클래스/속성/관계 온톨로지이고, 아래 완화 treatment가 **이미 config_key까지 정의**돼 있다
(`treatment_applicator.py`가 런타임 적용 → `patched_config`):

| treatment | config_key | 대상 |
|---|---|---|
| `disable:weekend_off_only` | `weekend_off_only_enable` | 주말휴무 전용 |
| `disable:transition_ban` | `ban_n_to_d` | N→D 전이 |
| `soft:team_min` / `disable:team_min` | `team_min_soft_fallback` / `team_min_by_team` | 팀 최소 |
| `soft:grade_min/max`, `disable:night_recovery`, `threshold:consecutive_work/monthly_night_cap`, `disable:ban_night_before_fixed_off` | (각 config_key) | 등급/회복/연속/야간 |

이 treatment들은 `resolution_options`(kind=treatment_bundle, **verified:false**)로 이미 나가고,
`apply-resolution`의 **treatment_ids 경로**로 적용된다 → 이 경로는 **컬럼 검증을 안 거친다**(config_override 사용).

**정정된 체인 (code→cause→family→treatment→config→apply, 전부 배선):**
```
reason_code →(aliases) cause →(family) 클래스 →(applies_to_causes) treatment(config_key)
  → treatment_applicator.patched_config → 솔버 → resolution_options 카드
```

### 그래서 진짜 남은 작업 (재재조정)

| 항목 | 상태 | 성격 |
|---|---|---|
| weekend_off/ban_n_to_d/team_min "옵션 없음" | ❌ 초안 오류 — treatment 있음(기능함) | — |
| **verified 업그레이드** | treatment(verified:false)만 있음 → **probe(verified:true) 추가**로 "재solve로 검증됨" 승격 | 품질(갭 아님) |
| **apply-resolution 컬럼 블로커** | verified **probe**(컬럼-델타)만 영향. 비-컬럼 키(weekend_off_only_enable 등)는 400. treatment는 우회 | 선결 |
| **4O getattr 정합** | dataclass 기본 False인데 getattr fallback True | 정합 |

→ 실제 작업 = **① probe 추가(verified 승격) + ② apply-resolution이 비-컬럼 키를 config_override로 라우팅 + ③ 4O 정합.**
(플러밍 확인: `weekend_off_only_enable`·`team_min_soft_fallback`·`ban_n_to_d`는 이미 cp_sat_basic:632/635/662에서 config_data로 읽힘 → probe verify·config_override 적용 둘 다 즉시 작동.)

---

## 1. Fallback 하드제약 전수 — INFEASIBLE 유발 가능한 것만 (22개)

`m.Add(...)`로 실현가능영역을 좁혀 **해가 없어질 수 있는** 제약만. (`obj.append` soft 항 제외.)
각 케이스: **완화 파라미터 1개**를 명시.

| # | 하드제약 | 위치(fallback_lex.py 기준) | 완화 파라미터 | 기본 활성? |
|---|---|---|---|---|
| 1 | Fixed cell 충돌 | 804/826/851 | 충돌 fixed 셀 제거 | 데이터 |
| 4 | Preceptee follow(=프리셉터 패턴 강제) | 1224/1231/1244 | `preceptee_on=False` | 기본 off |
| 5·6 | **4O 금지**(당월+월경계) | 919/968 | `enforce_4o_hard=False` | 실효 off* |
| 7 | **주말휴무 전용**(주말 강제 OFF + 평일 OFF 금지) | 1013/1025 | `weekend_off_only_enable=False` / nurse `is_weekend_off=False` | **기본 True** |
| 8 | Off-window(전월 carryover 회복창) | 1707/1728 | 전월 tail/fixed 조정 | 데이터 |
| 9·10·11 | **2N→2OFF / 3N→2OFF 회복**(+월경계 carryover) | 2228/2126/2006~ | `two_offs_after_two_nig=False` / `two_offs_after_three_nig=False` | 플래그 의존 |
| 12 | **N→D 금지** | 1530 | `ban_n_to_d=False` | **기본 True** |
| 13 | **E→D 금지** | 1556 | `ban_e_to_d=False`(=`banned_day_after_eve`) | **기본 True** |
| 14 | **N→E 금지** | 1582 | `ban_n_to_e=False` | **기본 True** |
| 16 | **1N 금지**(고립 단독 N) | 1643 | `not_one_night=False` / nurse `n_max=1` | **DB 대부분 True** |
| 17 | 휴가/공가 직전 N 금지 | 1666 | `ban_night_before_fixed_off=False` | 기본 off |
| 19 | **최대 연속근무** | 1747 | `max_consecutive_work_days ↑` | 항상 |
| 20·21 | **최대 연속야간**(+월경계) | 1812/1785 | `max_consecutive_nights ↑` / `three_seq_nig=True` | 항상 |
| 22 | **월 N 상한** | 1842 | `max_night_shifts_per_month ↑` | 항상 |
| 23 | Allowed-shift 마스크 / N전담 | 1875~1944 | nurse `allowed_shifts` 변경 | 데이터 |
| 24 | **최소 OFF**(short>1일) | 2489/2499 | `off_first=True` / `off_days ↓` | 항상(1일 slack) |
| 25 | **OFF cap**(over>1일) | 2586/2590 | `off_cap_relax_extra ↑` / `off_first=True` / `fallback_off_cap_bounded_slack_max ↑` | 항상(1일 slack) |
| 30·31·32 | **최대 coverage / M coverage / zero-demand** | 1379/1335/1321 | `daily_shift_requirements_max_by_day ↓` (완화 플래그 없음 — `_relax_coverage` 하드코딩 False, 540) | **항상 HARD** |
| 33 | off_first cap(assigned≤need) | 1387 | `off_first=False` | off_first 의존 |
| 34 | Stage-link(Stage3가 Stage2 최적 고정) | 2699/2701 | 구조적 — Stage2 해로 커밋 | 항상 |
| 35 | **team_min coverage** | team_constraints.py:154 | `team_min_soft_fallback=True` | **기본 False=HARD** |
| 37 | **grade 일일 min** | grade_constraints.py:575 | `grade_config.allow_soft_fallback=True` | grade 의존 |
| 39 | **per-nurse 월 D/E/N/O 한도** | monthly_limit_constraints.py:152 | nurse `n_exact/n_max/d_*/e_*/o_*` 완화 | 한도 설정 시 |

`*` 4O: dataclass 기본 False라 실효 off. 단 `getattr(...,True)` fallback이 있어 config에 없으면 True로 오작동 여지 → **정합 위험(§4-C)**.

**이름만 hard, 실제 SOFT(절대 infeasible 안 됨)**: 주2OFF(`enforce_two_offs_per_week`), NOD/NOE/EOD(`nod_noe`), `grade_monthly_hard`, off-quota, isolated-off, **최소 coverage 부족**(short slack), 경력자 요구, 그리고 우리가 방금 넣은 `max_conseq_off`. → 이것들은 catch 대상 아님.

---

## 2. 커버리지 매트릭스 — precheck(사전) × probe(즉시) × soft-slack

| 하드제약 | precheck 사전 캐치 | probe(RELAX_CATALOG) | 비고 |
|---|---|---|---|
| 월 N 상한(#22) | ✅ `MONTHLY_NIGHT_CAPACITY_SHORTAGE` | ✅ `raise_max_night_cap` | 완비 |
| 2N/3N 회복(#9·10) | △ (night capacity에 간접 반영) | ✅ `disable_2n2off`/`disable_3n2off` | 완비 |
| 최대 연속근무(#19) | ❌ | ✅ `raise_max_consec_work` | probe만 |
| 연속야간(#20·21) | ❌ | ✅ `relax_consecutive_nights` | probe만 |
| 1N 금지(#16) | ❌ | ✅ `disable_not_one_night` | probe만 |
| 휴가전 N 금지(#17) | ❌ | ✅ `disable_ban_n_before_fixed_off` | probe만 |
| E→D 금지(#13) | ❌ | ✅ `disable_banned_day_after_eve` | probe만 |
| 최소 OFF/off예산(#24) | ✅ `OFF_BUDGET_EXCEEDS_NUM_DAYS` | ✅ `lower_off_days` | 완비 |
| Preceptee(#4) | ✅ `PRECEPTEE_SYNC_MISMATCH` | ✅ `disable_preceptee_sync` | 완비 |
| grade 일일 min(#37) | ✅ 다수(`GRADE_*`) | ❌ | **precheck만 — 즉시 갭** |
| team_min(#35) | ⚠️ warning만(비차단) | ❌ | **갭(중요)** |
| 월 한도(#39) | ✅ save-time(`MONTHLY_LIMIT_*`) | ❌ | precheck만 |
| 일 capacity/eligible(#30·32) | ✅ `GLOBAL_*`/`*_SHORTAGE` | ❌(불가) | 데이터 수정 필요 |
| **주말휴무 전용(#7)** | ❌ | ❌ | **갭(기본 True)** |
| **N→D 금지(#12)** | ❌ | ❌ | **갭(기본 True)** |
| **N→E 금지(#14)** | ❌ | ❌ | **갭(기본 True)** |
| **4O(#5·6)** | ❌ | ❌ | **갭(+정합 위험)** |
| **OFF cap(#25)** | ❌ | ❌ | **갭** |
| **off_first cap(#33)** | ❌ | ❌ | **갭** |
| Fixed/allowed/off-window(#1·8·23) | 일부(`FIXED_*`/`ALLOWED_*`) | ❌(데이터) | 데이터 수정 라우팅 |
| Stage-link(#34) | ❌ | ❌(구조적) | 아래 §4-D |

---

## 3. 갭 정리 — 우선순위 (안정화 실제 작업 리스트)

### P0 — 기본 활성인데 probe도 precheck도 없음 (가장 자주 조용히 실패)
1. **주말휴무 전용(#7)** — `weekend_off_only_enable` 기본 True. 주말휴무자가 주말 커버리지에 필요하거나 평일 OFF가 회복으로 강제되면 INFEASIBLE. → **probe 추가**: `disable_weekend_off_only`.
2. **N→D / N→E 금지(#12·14)** — 기본 True인데 E→D만 probe 존재. → **probe 추가**: `disable_ban_n_to_d`, `disable_ban_n_to_e`.
3. **team_min(#35)** — 기본 HARD(`team_min_soft_fallback=False`)인데 precheck는 **warning(비차단)**뿐 → solve-time에 터짐. → **probe 추가**: `soften_team_min`(=`team_min_soft_fallback=True`). (precheck team_min을 blocking으로 올릴지는 정책 결정 필요 — team_min은 원래 "soft 의도"라 probe가 맞음.)

### P1 — OFF/cap 계열 (1일 slack 넘으면 터짐)
4. **OFF cap(#25)** — `off_cap_relax_extra ↑` 또는 `off_first=True` probe. → **probe 추가**: `raise_off_cap_relax`, `enable_off_first`.
5. **off_first cap(#33)** — off_first=True일 때 fixed_wanted 초과 → `off_first=False` probe(방향 반대라 별도).

### P2 — grade / 4O / 정합
6. **grade 일일 min(#37)** — precheck는 잡지만 즉시 probe 없음. → **probe 추가**: `soften_grade`(=`allow_soft_fallback=True`). (grade는 병원 핵심 제약이라 trade-off 문구 강하게.)
7. **4O 정합(#5·6)** — dataclass 기본 False인데 `getattr(cfg,"enforce_4o_hard",True)` fallback이 True. config에 값이 안 실리는 경로가 있으면 **의도치 않게 4O가 켜져 INFEASIBLE**. → getattr 기본을 **False로 통일** + (켤 수 있으니) probe `disable_4o` 추가.

### P3 — 구조적 (파라미터 완화로 못 푸는 것)
8. **최대/M coverage·zero-demand(#30·31·32)** — `_relax_coverage` 하드코딩 False. 파라미터로 못 풀고 **데이터(요구인원/고정셀) 수정**만 가능. → precheck에서 이미 잡히면 `fix_plan.data_correction_required=True`로 라우팅. probe 대상 아님. **명시적으로 "완화 불가, 데이터 수정 요망" 메시지** 보장.
9. **Stage-link INFEASIBLE(#34)** — Stage3가 Stage2 최적을 고정한 탓에 드물게 발생. 완화 파라미터 없음 → **Stage2 해로 graceful fallback**(이미 일부 구현) + 로깅. 유저 선택지 아님.

---

## 4. 설계 — 확장은 "기존 스파인에 얹기"

### A. probe 추가 (P0~P2의 핵심 — 코드 최소)
`undiagnosed_probe.py`의 3개 맵에 항목만 추가하면 verified 옵션으로 자동 노출:
```python
# RELAX_CATALOG 에 추가 (예)
{"id": "disable_weekend_off_only", "family": "weekend_off",
 "label_ko": "주말휴무 전용 해제", "apply": lambda c: {"weekend_off_only_enable": False}},
{"id": "disable_ban_n_to_d", "family": "transition",
 "label_ko": "야간→주간 금지 해제", "apply": lambda c: {"ban_n_to_d": False}},
{"id": "disable_ban_n_to_e", "family": "transition",
 "label_ko": "야간→저녁 금지 해제", "apply": lambda c: {"ban_n_to_e": False}},
{"id": "soften_team_min", "family": "team",
 "label_ko": "팀 최소인원 soft 완화", "apply": lambda c: {"team_min_soft_fallback": True}},
{"id": "raise_off_cap_relax", "family": "off_cap",
 "label_ko": "OFF 상한 여유 +2", "apply": lambda c: {"off_cap_relax_extra": (c.get("off_cap_relax_extra",0) or 0) + 2}},
{"id": "soften_grade", "family": "grade",
 "label_ko": "등급 일일최소 soft 완화", "apply": lambda c: {"grade_config": {**(c.get("grade_config") or {}), "allow_soft_fallback": True}}},
```
+ `TRADEOFF_KO`, `COL_LABEL_KO`에 대응 문구.
🚨 **확정 블로커** (가정 아님): `apply-resolution`의 컬럼-delta 경로는 **ORM 컬럼만 허용**한다 —
`roster_create.py:360-363` `allowed = {c.name for c in RosterConfig.__table__.columns}; bad=... → HTTP 400`.
그런데 위 완화 키들(`ban_n_to_d/e`, `weekend_off_only_enable`, `team_min_soft_fallback`,
`off_cap_relax_extra`, `max_consecutive_nights`, `grade_config`...)은 **솔버 dataclass 파라미터라 DB 컬럼이 아님 → 400 거부**.
- 증거: **기존 `relax_consecutive_nights` probe도 이미 깨져 있음** — `max_consecutive_nights`는 컬럼이 아니라
  verify(config_override)는 통과하지만 유저가 옵션을 누르면 apply-resolution이 400. (probe verify는
  `config_override`로 아무 키나 되지만, 유저 적용은 컬럼 검증을 거치는 **비대칭**.)
- 해결: apply-resolution의 **transient(persist=False) 경로를 config_override 기반**으로 바꿔 임의 override 키 허용
  (허용 키 화이트리스트 = 컬럼 ∪ 솔버 완화 파라미터 집합). `persist=True`만 컬럼 write 요구.
- **이게 P0의 #1 선결과제** — 안 고치면 신규/기존 probe 옵션 클릭 시 죄다 400.

### B. probe 순서/조합 (이미 됨)
`probe_relaxations`가 단일 완화 우선 → 실패 시 combo(2~3개) 자동 탐색(=black-box MCS). 새 probe는 자동으로 이 로직에 포함됨. verify는 `FB_VERIFY_SKIP_STAGE3=1`로 빠르게.

### C. 4O getattr 정합 (버그성)
`fallback_lex.py:886`, `cp_sat_basic.py:2849·2856`, `cp_sat_basic.py:616`의 `getattr(cfg,"enforce_4o_hard",True)` → **기본 인자 False로 통일**. dataclass가 False이므로 무해하지만, config-dict 경로 누락 시 True로 새는 것을 원천 차단.

### D. Stage-link 방어
Stage3 INFEASIBLE 시 Stage2 마지막 feasible 해를 채택(부분 구현). 이건 유저 선택지 아님 — 로깅 + 품질저하 경고만.

### E. precheck ↔ probe 역할 경계 (설계 원칙)
- **precheck(사전)** = *집계 산술*로 잡히는 구조적 불능(capacity/demand/grade/team 합/월 N cap) → blocking 500 + treatment 옵션. 데이터로만 풀리는 건 `data_correction_required`.
- **probe(즉시)** = *시간축 상호작용*(회복+cap+전이+fixed 얽힘)으로 solve-time에만 드러나는 불능 → relax-and-retry로 **검증된** 파라미터 옵션.
- 둘 다 **동일한 `resolution_options[]` 카드 계약**으로 프론트에 나감(통일 유지).

### F. 응답 계약 (그대로 사용)
```
HTTP 500 → detail.infeasibility.resolution_options[] = [
  { option_id, kind:"relax_constraint|combo|treatment_bundle", verified,
    title_ko, changes:[{config_key,label_ko,from,to}], trade_off_ko,
    apply:{col:val} | treatment_ids:[...] }
]
프론트가 고른 카드의 apply(또는 treatment_ids)를
POST /roster_create/apply-resolution { year,month, apply|treatment_ids, persist } 로 에코 → 재생성.
```

---

## 5. 다음 액션 (제안 순서)
1. **선결**: apply-resolution 키 검증 확장(컬럼 ∪ 화이트리스트) — 없으면 P0 probe가 거부됨(§4-A ⚠️).
2. **P0 probe 3~4개** 추가(weekend_off_only, ban_n_to_d/e, team_min) + trade-off 문구.
3. **4O getattr 기본값 False 통일**(§4-C).
4. **P1·P2 probe**(off_cap, off_first, grade).
5. 구조적(#30·32·34)은 **데이터 수정/graceful** 라우팅 확인만.
6. `INFEASIBILITY_FRONTEND_GUIDE.md`에 `resolution_options`/`apply-resolution` 계약 추가(현재 미기재).

각 probe 추가마다 회귀: "완화 전 INFEASIBLE → 완화 후 feasible" 재현 케이스 1개씩(=verify 하니스).
