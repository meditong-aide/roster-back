# banned_wanted (금지 원티드) 기획서 — 재사용 우선판

`fixed_wanted`(확정 원티드)의 **배반(dual)**. 셀 단위로 "이 근무는 배정 불가"를 지정하며, 셀당 **복수 근무를 배열로** 금지한다. 근무표 생성 시 하드 제약으로 반영.

## 설계 원칙: 기존 것 최대 재사용

이 기능은 사실상 **새 알고리즘이 없다.** 기존 3개 자산을 그대로 탄다.

| 기존 자산 | 재사용 방식 |
|---|---|
| 솔버 `initial_forbidden {(n,d):[codes]} → X==0` ([cp_sat_basic.py:3032](../app/services/cp_sat_basic.py#L3032)) | banned = 이 맵의 producer 하나 추가. **신규 CP-SAT primitive 0** |
| 조정판 엔드포인트/서비스 (`/wanted/adjustment` save·get·reset, [wanted.py:711](../app/routers/wanted.py#L711)) | 신규 엔드포인트 만들지 않고 **기존 흐름에 banned를 얹음**. auth·group 해석·관할검증·shift매핑 헬퍼 재사용 |
| forbidden 프리컨버전 merge 지점 ([roster_create_service.py:2621](../app/services/roster_create_service.py#L2621)) | banned를 기존 병합 파이프의 또 다른 입력으로 |

**새로 만드는 것은 단 하나: 얇은 저장 테이블** + 그 위의 검증 규칙 1묶음. 왜 fixed 테이블을 재사용하지 않는지는 §1.

---

## 1. 저장: 왜 별도 테이블인가 (재사용의 예외)

`fixed_wanted_entries`에 `entry_kind='banned'` 컬럼만 붙이는 게 최대 재사용이지만 **채택 안 함.** 이유:

이 테이블을 읽는 지점이 8~10곳이고, 각각 `entry_kind='fixed'` 필터를 빠짐없이 넣어야 정상 동작한다. 특히 위험한 곳:
- **컨버터 [roster_create_service.py:5100](../app/services/roster_create_service.py#L5100)** — 필터 누락 시 금지코드를 **fixed 핀으로 읽어 그 근무를 강제 배정**(정반대 재앙).
- [replacement_recommend_service.py:191](../app/services/replacement_recommend_service.py#L191) — fixed 셀 추천 제외 로직에 banned가 섞이면 오배제.
- [wanted_tools.py:122](../app/agents_v2/tools/wanted_tools.py#L122) 에이전트 원티드 조회 — banned가 fixed로 노출.
- [wanted_service.py:2213](../app/services/wanted_service.py#L2213) 조정판 조회.

→ 회귀 표면이 넓고 그중 하나는 동작 역전. **저장만 격리**하는 편이 안전하고 총비용도 낮다.

### 신규 테이블 `banned_wanted_entries` (셀=1행, 코드 배열)

| 컬럼 | 타입 | 비고 |
|---|---|---|
| id | INTEGER PK | |
| group_id | VARCHAR(50) FK | |
| year / month | SMALLINT / TINYINT | |
| nurse_id | VARCHAR(50) FK | |
| shift_date | DATE | `(group,nurse,shift_date)` 유니크 |
| banned_shift_ids | NVARCHAR(50) | JSON 배열 `["D","E"]`, 1~2개 |
| banned_shifts_table_ids | NVARCHAR(50) | shifts.id 배열(코드 remap 안전) |
| is_applied | BOOLEAN default True | |
| reason | TEXT null | |
| created_by / created_at / updated_at | | |

인덱스는 fixed와 동일 패턴. **별도 사용 플래그 없음** — fixed 는 이중 데이터소스(FixedWantedEntry vs 레거시 WantedRequest)를 가르느라 `fixed_wanted_use_yn` 가 필요하지만, banned 는 단일 소스라 게이트가 무의미하다. 저장된 `is_applied=True` 금지가 있으면 항상 적용, 없으면 무효과. 개별 on/off 는 entry 의 `is_applied` 로 제어.

> 셀=1행+배열 컬럼: max2 검증·충돌검사·컨버터 타깃(`[codes]`)이 모두 셀 단위라 fixed(코드=1행)보다 이 형태가 맞다.

---

## 2. 프론트 ↔ 백엔드 계약 — 기존 조정판 흐름에 얹기

**신규 엔드포인트를 만들지 않는다.** 조정판은 이미 fixed를 저장/조회/리셋하는 화면이고, banned는 같은 그리드에서 편집되므로 같은 엔드포인트에 실어보낸다.

### 2.1 조회 — `GET /wanted/adjustment/{year}/{month}` (기존, 응답만 확장)
`AdjustmentNurse`([roster_schema.py:951](../app/schemas/roster_schema.py#L951))에 필드 1개.
```jsonc
{
  "nurses": [{
    "nurse_id": "abc", "name": "김민지",
    "entries": [ /* 기존 fixed */ ],
    "banned_entries": [                                   // ★ 신규
      { "id": 12, "shift_date": "2026-08-14", "banned_shift_ids": ["D","E"], "is_applied": true }
    ],
    "monthly_summary": {"D":5,"E":3,"N":2},
    "blocked_days": [], "assignments": []
  }],
  "has_fixed_wanted": true,
  "has_banned_wanted": true                               // ★ 신규
}
```

### 2.2 저장 — `POST /wanted/adjustment` (기존, 바디에 banned 추가)
`FixedWantedCreate`([roster_schema.py:871](../app/schemas/roster_schema.py#L871))에 `banned_entries` 배열 추가. 한 번의 저장으로 fixed+banned 동시 처리 → 프론트 저장 버튼 1개 유지, 트랜잭션 1개.
```jsonc
{
  "year": 2026, "month": 8,
  "entries": [ /* 기존 fixed 항목들 */ ],
  "banned_entries": [                                     // ★ 신규
    { "nurse_id": "abc", "shift_date": "2026-08-14", "banned_shift_ids": ["D","E"] },
    { "nurse_id": "def", "shift_date": "2026-08-03", "banned_shift_ids": ["D"], "reason": "야간 후 D 금지" }
  ]
}
```
프론트 전송 규약:
- `banned_shift_ids`: 근무코드 문자열 배열(`"D"|"E"|"N"`). id 아님. 백엔드가 shifts_table_id 매핑.
- 셀당 **1~2개**. 3번째 chip은 프론트에서 비활성(백엔드 422 이중 방어).
- `O` 금지 불가(OFF 강제는 fixed_wanted O). 중복 불가.
- **스냅샷 시맨틱**: `banned_entries`에 없는 셀은 그 월 금지에서 삭제. 그리드 전체 금지 상태를 그대로 전송.
- ⚠️ **하위호환(P4)**: `banned_entries`는 **Optional, `null`="banned 손대지 않음"**(갱신 안 된 프론트 보호), `[]`="전체 해제". banned 삭제 스코프는 fixed의 `request_nurse_ids`와 **독립적으로** `banned_entries`의 nurse 집합에서 계산할 것. 안 그러면 fixed만 담긴 저장이 banned를 오삭제/오잔존시킨다.

응답: 기존 `FixedWantedListResponse`에 `banned_entries`, `warnings` 추가.
```jsonc
{
  "group_id": "...", "year": 2026, "month": 8,
  "entries": [ /* fixed */ ],
  "banned_entries": [ { "id":12, "nurse_id":"abc", "shift_date":"2026-08-14",
                        "banned_shift_ids":["D","E"], "is_applied":true } ],
  "total_count": 5,
  "warnings": [
    { "nurse_id":"abc", "shift_date":"2026-08-14",
      "dropped_shift_ids":["D","E"], "reason":"dropped_on_fixed_cell" },  // fixed 셀이라 banned 저장 안 함(정합성)
    { "nurse_id":"def", "shift_date":"2026-08-20",
      "reason":"coverage_risk", "detail":"N 교대 need=3, 금지 적용 후 공급=2" }  // 사전 용량 경고(P2)
  ]
}
```

### 2.3 리셋 — `POST /wanted/adjustment/{year}/{month}/reset` (기존, banned도 삭제)
기존 리셋이 그 월 fixed를 지운다 → banned도 함께 삭제하도록 확장. 신규 엔드포인트 없음.

### 2.4 반려(토글) — `PATCH /wanted/adjustment/banned/entry/{entry_id}/toggle` (구현됨)
셀 단위 `is_applied` 뒤집기 = 반려. fixed 토글(`PATCH /adjustment/entry/{id}/toggle`)과
**경로를 분리**(`/banned/`)했다 — banned는 별도 테이블이라 entry_id 가 겹칠 수 있어서다.
권한·응답(`ToggleEntryResponse`)은 fixed 토글과 동일. `caller_group_id` 불일치 시 403.

동작: 반려(`is_applied=False`)하면 생성 컨버터(§4)가 그 금지를 반영하지 않고
(`BannedWantedEntry.is_applied == True` 만 조회), 다시 토글하면 되살아난다. 기록은
삭제 없이 유지 — fixed 와 동일한 반려 개념.
서비스 `toggle_banned_wanted_entry_service`(fixed 토글 미러).

### 2.5 생성용 조회 (내부)
별도 엔드포인트 대신 **컨버터가 DB 직접 조회**(fixed 컨버터도 [:5100](../app/services/roster_create_service.py#L5100)에서 직접 읽음). 신규 GET 불필요.

---

## 3. 백엔드 검증 (저장 서비스 [wanted_service.py:2617](../app/services/wanted_service.py#L2617) 내부에 banned 브랜치)

`save_fixed_wanted_service`를 **재사용**하고 banned 처리 분기를 추가. auth·group·관할검증·shift매핑은 이미 그 함수 안에 있음.
1. 개수 `1 ≤ len ≤ 2` 위반 → 422 `max_2_bans`.
2. 코드 ∈ {D,E,N}, `O` 포함 → 422 `off_ban_not_allowed`.
3. 배열 내 중복 → 422 `duplicate_shift`.
4. 관할 밖 셀 → 기존 `_validate_cross_save_entries` **재사용**.
5. **★ 허용집합 합집합 가드 (P3, 필수)**: max-2 만으로는 근무 여지가 보장되지 않는다. 그 간호사의 **유효 허용근무**(`NurseAllowedShiftPeriod` as-of, 없으면 캐시 `allowed_shifts`)에서 이미 금지된 코드 ∪ banned 가 **{D,E,N} 전체를 덮으면** 강제 OFF/INFEASIBLE.
   → 합집합이 근무 0개면 **422 `no_workable_shift_left`**(또는 정책상 허용 시 강한 warning). 예: 허용=[N] + banned=[N] → D/E/N 전멸.
6. **fixed 충돌 → 저장하지 않음(정합성, 백엔드 권위)**: 그 날 fixed가 있으면 근무가 1개로 확정되므로 banned는 **존재 이유가 없다**(모순이든 군더더기든 결과는 fixed 그대로). 프론트 비활성은 UX 편의일 뿐 — stale/버그/직접호출로 fixed 셀 banned가 들어오면 **죽은 행**이 되므로 **백엔드가 저장 자체를 drop**한다. is_applied=True fixed 존재 셀의 banned는 persist하지 않고 `warnings`에 `reason:"dropped_on_fixed_cell"` 통지만(하드 reject 아님, 나머지 정상 저장). 저장 순서상 fixed가 먼저 커밋되어 최신 fixed 상태로 판정됨.
7. **사전 용량 경고 (P2, 권장)**: banned는 fixed보다 커버리지를 깰 확률이 훨씬 높은데 현재 유일한 용량 점검([fallback_lex.py:645-714](../app/services/cp_sat/fallback_lex.py#L645-L714))은 **`print`만 하는 진단**이라 저장 전 통지가 없다. → 저장 시 같은 cap 로직(일·교대별 `need` vs banned 적용 후 공급)을 경량 재현해 `need>공급`이면 `warnings`에 `reason:"coverage_risk"`로 비차단 통지. 없으면 생성이 조용히 INFEASIBLE로 떨어진다.

저장: 그 (group,year,month) banned 스냅샷 replace (§2.2 P4 스코프 주의).

---

## 4. 솔버 컨버터 (기존 forbidden 파이프에 producer 추가)

⚠️ **경로 정정(P1)**: banned는 fixed_cells 경로가 **아니다**. [:5100](../app/services/roster_create_service.py#L5100)은 `combined_fixed_cells`(= fixed 핀 → `roster_system.fixed_cells`) 전용이며, forbidden은 **별개 경로**로 `config_dict["initial_constraints"]`에 들어간다. banned를 :5100에 넣으면 **금지코드가 fixed 핀으로 처리돼 그 근무를 강제 배정**하는 정반대 오작동이 난다.

올바른 배선:
- 저장된 banned 행(`is_applied=True`) 조회 — 별도 사용 플래그 없이 항상. 없으면 빈 맵(무효과).
- `banned_shifts_table_ids`로 코드 복원 → nurse_id, `shift_date.day-1`→day_idx (fixed 컨버터의 활동범위/일자 경계 검증 [:5144-5153](../app/services/roster_create_service.py#L5144) 미러).
- `{"forbidden": {nurse_id: {day_idx: [codes]}}}` 형태로 만들어 **[roster_create_service.py:2935](../app/services/roster_create_service.py#L2935) `config_dict["initial_constraints"] = _merge_initial_constraints(...)` 에 base/extra로 합류**. `_merge_initial_constraints`([:2610](../app/services/roster_create_service.py#L2610))가 nurse_id→day_idx→codes 합집합으로 병합.
- 이후 기존 파이프가 `initial_forbidden → m.Add(X==0)`([cp_sat_basic.py:3059](../app/services/cp_sat_basic.py#L3059), [fallback_lex.py:1149](../app/services/cp_sat/fallback_lex.py#L1149)) 자동 enforce. **추가 솔버 코드 0.**
- fixed 충돌 셀은 두 솔버가 user-fixed 셀에서 forbidden **통째 skip** → fixed 우선 자동 성립(모순 INFEASIBLE 없음, §3.6).
- (선택) 진단용 `BannedWantedNode`를 `ForbiddenCellNode`([cp_sat_basic.py:3069](../app/services/cp_sat_basic.py#L3069)) 병렬 태깅.

---

## 5. 순비용 요약 (신규 vs 재사용)

| 영역 | 신규? | 규모 |
|---|---|---|
| 솔버 하드제약 | 재사용 (initial_forbidden) | 0 |
| 엔드포인트 (save/get/reset) | **재사용** (기존 조정판에 얹음) | 0~소 |
| 검증 헬퍼 (관할/shift매핑/auth/group) | 재사용 (save 서비스 내부) | 0 |
| forbidden 병합 파이프 | 재사용 | 0 |
| **`banned_wanted_entries` 테이블 + 플래그** | 신규 | 소 |
| 스키마 필드 (`banned_entries` Optional, `warnings`) | 신규 | 소 |
| §3.1-3.4 기본 검증 (max2/근무만/중복/관할) | 신규(관할은 재사용) | 소 |
| **§3.5 허용집합 합집합 가드 (P3, 필수)** | 신규 | 소~중 |
| §3.7 사전 용량 경고 (P2, 권장) | 신규(cap 로직 재현) | 중 |
| §4 컨버터 브랜치 → `initial_constraints` 병합 (P1) | 신규 | 소 |
| 반려(토글) `/banned/entry/{id}/toggle` (구현됨) | 신규 | 소 |

**순증분 ≈ 얇은 테이블 1개 + save에 검증 브랜치(P3 필수·P2 권장) + converter 브랜치.** 새 엔드포인트·새 솔버 로직·새 검증 파이프 전부 회피. P2를 뺄 경우 규모는 더 줄지만 banned발 INFEASIBLE이 불투명해진다.

---

## 7. 검토 결과 (2026-07-24 코드 재확인)

**안전 확인**: fixed+banned 동일셀 모순 INFEASIBLE 없음(두 솔버 fixed셀 forbidden skip) · forbidden 병합구조 일치 · 커버리지 precheck가 banned 반영 · save 스냅샷 삭제 동작 확인.

**반영된 수정**: P1(컨버터 경로 정정 →§4) · P3(허용집합 합집합 가드 →§3.5) · P2(사전 용량 경고 →§3.7) · P4(None-guard/독립 삭제스코프 →§2.2) · P6(충돌경고 무해화 →§3.6). 이 중 **P1·P3는 반드시**, P2·P4는 강력 권장.

---

## 6. 프론트 체크리스트
- [ ] 조정판 셀에 D/E/N 금지 chip 토글. 2개 선택 시 3번째 비활성.
- [ ] `GET /wanted/adjustment` 응답 `banned_entries` 렌더(fixed와 시각 구분).
- [ ] 저장 시 fixed `entries` + 금지 `banned_entries`를 **한 바디**로 스냅샷 전송.
- [ ] 응답 `warnings` 처리: fixed에 가려 무시된 금지 셀 배지.
- [ ] `banned_shift_ids`는 코드 문자열 배열, O 미포함.
