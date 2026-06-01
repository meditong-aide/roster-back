# 간호사 그룹 변경 모델 (병동이동·파견·영구속성) — Phase 0 결정물

작성: 2026-06-01
관련 시나리오: 원티드 기반 월별 클러스터링 도입, 인사이동(영구), 파견(임시), 영구속성 변경

---

## 0. 한 줄 요약
`NurseAssignment`를 **모든 변경의 통합 원장(append-only event log)**으로 두고, `Nurse`는 **현재 발효값 캐시(read model)**로 유지. 별도 snapshot 테이블 없음. 발효 cron이 시점 도래 시 Nurses 동기화. 권한·엔진 read는 **시점 기반**으로 NurseAssignment를 truth로 봄.

---

## 1. 데이터 모델

### 1.1. 테이블 구조

| 테이블 | 역할 | 변경 |
|---|---|---|
| `Nurse` | 현재 발효값 캐시 (UI 명단·검색·KPI 디폴트) | 발효 cron이 동기화. UI 변경 없음. |
| `NurseAssignment` | 모든 변경의 append-only 원장. truth. | `payload JSON`, `valid_from`, `valid_to`, `kind` 추가 |
| `NurseMonthlyLimit` | 월간 한도 (그대로) | 이동 발효 시 `group_id`만 자동 update |
| `WantedRequest` | 원티드 (그대로) | 이동 시 **건드리지 않음** (옛 group_id 유지, generate가 자연 무시) |
| `RosterConfig` | 그룹 전역 설정 | 변경 없음. 새 그룹 config 그대로 적용 |
| `NurseMonthlySnapshot` | **추가하지 않음** | — |

### 1.2. NurseAssignment.kind 분류

| kind | 의미 | Nurses 동기화 | 입력 권한 |
|---|---|---|---|
| `transfer` | 영구 병동이동 | 발효 시 `group_id` 업데이트 | 본가 HN/상위 |
| `dispatch` | 시간 한정 파견 (본가 유지) | 안 함 | 본가 HN/상위 |
| `dispatch_override` | 파견 기간 한정 속성 변경 | 안 함 (Nurses 불변) | 파견지 HN (자동 강제) |
| `permanent_change` | 영구 속성 변경 | 발효 시 해당 attribute 업데이트 | 본가 HN |
| `leave` / `return` | 휴직/복직 | 발효 시 `active` 업데이트 | 본가 HN/상위 |

### 1.3. payload JSON 스키마
한 row에 변경된 속성만 포함. 변경 안 한 속성은 키 없음.
```json
{
  "group_id": "10135890c287",
  "team_id": 2,
  "weekend_off": true,
  "allowed_shifts": ["D"],
  "is_night_nurse": false
}
```
인덱스: `(nurse_id, kind, valid_from, valid_to)`. attribute별 직접 인덱스는 필요 시 GIN/조건부.

---

## 2. 정책

### 2.1. 발효 단위
**임의 일자 허용** (그룹·속성·파견 모두). generate 단위는 월이지만, 엔진이 (nurse, date)별로 NurseAssignment를 read하므로 월 중간 발효도 자연 처리.

### 2.2. 권한 모델 (시점 기반)
- 토큰의 `group_id`/`original_group_id`는 **화면 표시용 hint만**. 권한 체크 사용 안 함.
- 권한 = DB 기준 시점-effective group으로 판정:
  ```
  can_write(user, target_nurse, target_group, year_month):
      effective = NurseAssignment.lookup(target_nurse, year_month) or Nurses.group_id
      return user == target_nurse and target_group == effective
  ```
- 본인이 발효 전에도 새 그룹용 wanted 작성 가능 (NurseAssignment의 미래 예약 반영).
- 파견지 입력 시 시스템이 `kind=dispatch_override`로 자동 강제 — 영구 속성 직접 변경 차단.

### 2.3. 승인 흐름
**C: 단방향.** 운영 책임자(옛 그룹 HN 또는 상위) 단독 결정. 새 그룹은 알림만.

### 2.4. 데이터 종류별 이동 시 처리

| 데이터 | 발효 시 처리 |
|---|---|
| `wanted` | **건드리지 않음**. group_id 그대로. generate가 새 group_id로 필터하면 옛 wanted는 자연 무시. |
| `NML` | `group_id` 자동 update (값 유지) |
| `RosterConfig` | 변경 없음 (그룹 전역) |
| 과거 schedule | 옛 그룹에 그대로. nurse_id 기반 history 조회. |

### 2.5. 충돌 검출
- 같은 nurse 미래 발효 동시 2건 → 거부
- 휴직/퇴사 시간 겹침 → 경고
- 같은 attribute 같은 valid_from → 입력 시각/id 큰 쪽 승, 둘 다 history 보존

### 2.6. dry-run 영향 분석
이동 예약 입력 직후 화면 표시:
```
영향: NML 3건 group_id update, 7월 generate 시 새 그룹 사용,
      wanted 5건은 옛 그룹에 보존(generate 무시), 본인 알림 1건
```

---

## 3. 자동 발효 cron
- 매일 자정 (또는 매시간) 실행
- `WHERE NurseAssignment.valid_from <= now() AND status != 'applied' AND kind IN ('transfer', 'permanent_change', 'leave', 'return')`
- 처리:
  - `transfer` → Nurses.group_id 업데이트 + NML group_id update
  - `permanent_change` → Nurses의 해당 attribute 업데이트 (payload 키별)
  - `dispatch`/`dispatch_override` → Nurses 불변 (이벤트만 활성 마크)
- 실패 시 catch-up: 다음 실행에서 누락 row 자동 처리. 멱등성 확보(`status=applied` 마크).
- 야간 reconcile job: Nurses와 latest NurseAssignment 비교, 불일치 시 동기화·경고.

---

## 4. 취소/롤백
- **발효 전 취소**: `status=cancelled` 마크. cron이 무시. dry-run 보였던 영향 자동 무효.
- **발효 후 되돌리기**: 반대 방향 새 이동 이벤트(B→A)로. 자동 이전된 NML group_id 등은 그대로 새 이동에 따라 또 update.

---

## 5. UI 정책

### 5.1. 근무자 추가 vs 병동이동
- **"추가" 액션**: 신규 입사/비활성/퇴사 후 복귀만. 다른 그룹 소속자 검색 시 선택 차단 + "병동이동으로 진행하세요" 딥링크.
- **"병동이동" 액션**: 대상자·발효 일자·승인자·메모 입력. dry-run 영향 미리 보기 필수.

### 5.2. 명단 뷰 — 3 탭 분리
- **현재 멤버** (디폴트): 현재 운영주가 나인 nurse. 편집권 있음.
- **들어올 예정 / 나갈 예정**: 발효 전 이동 예약. 읽기 전용. 발효일 표시.
- **이력**: 한 번이라도 우리 그룹과 관계 있던 nurse. 회색·배지. 읽기 전용.
  - 가시 범위: 무제한 (1~2년 후 노이즈 보이면 N개월 필터 추가 검토)

### 5.3. "예정 변경" 가시화
미래 발효 변경이 있는 nurse에 배지 표시. 클릭 시 발효 일자별 변경 목록 노출. 운영자가 본인 입력 결과를 즉시 확인 가능하도록.

---

## 6. 알림
- 대상: 본인 / 옛 그룹 HN / 새 그룹 HN
- 트리거: 예약 직후 + 발효 직후 + 취소 시
- 본인 알림: "B 그룹으로 이동 예정/완료. 새 그룹에서 wanted 작성 가능."

---

## 7. 외부 시스템 동기화
- HR/qpis hook: `transfer` 발효 시 외부 시스템에 동기화 요청 (배치 또는 이벤트 push).
- 토큰 갱신: 발효 후 본인이 다음 로그인 시 새 group_id 토큰. 권한 체크는 어차피 DB 기준이라 즉시 stale 권한 사고 없음.

---

## 8. 마이그레이션
- `NurseAssignment`에 현재 Nurse 값 backfill **하지 않음**. 새 변경부터 NurseAssignment에 쌓임.
- 폴백 룰: 해당 (nurse, date)에 effective NurseAssignment row 없으면 Nurses 폴백.
- 과거 이벤트 재현 정확도는 NurseAssignment 도입 이후만 보장. 그 이전 시점 상태는 git/audit log 별도.

---

## 9. 시나리오 시퀀스

### 9.1. 영구 병동이동 (인사이동·월별 클러스터링)
1. 운영자가 박혜미 7/1자 A→B 이동 입력 → NurseAssignment row 생성 (`kind=transfer`, `valid_from=2026-07-01`, payload={group_id: B})
2. dry-run 영향 표시 → 확정
3. 본인·옛 HN·새 HN 알림
4. (발효 전) 박혜미는 새 그룹용 wanted 자유 작성 가능 (권한 시점 기반)
5. 7/1 자정 cron: Nurses.group_id=B 업데이트, NML group_id update, status=applied 마크
6. 박혜미 새 그룹 운영 시작. A는 명단에서 제거, 이력 탭에 잔존.

### 9.2. 파견 (임시)
1. 운영자가 박혜미 6/1~6/20 B로 파견 입력 → `kind=dispatch`, `valid_from=2026-06-01`, `valid_to=2026-06-20`, target_group=B
2. 파견지 B HN이 그 기간 weekend_off=true로 운영하고 싶다 → `kind=dispatch_override`, `valid_from/to=동일`, payload={weekend_off: true}
3. 6월 generate(B): 6/1~6/20 박혜미는 dispatch_override 우선 → weekend_off=true 적용. 그 외엔 Nurses(A 본가) 폴백.
4. 6/21 이후: 박혜미 다시 A로. dispatch_override row는 valid_to 지나 더 이상 effective 아님. 본가 운영 자동 복귀.
5. Nurses 테이블은 처음부터 끝까지 안 건드림.

### 9.3. 영구속성 변경 (본가)
1. A HN이 박혜미 weekend_off를 7/1자 true로 변경 입력 → `kind=permanent_change`, `valid_from=2026-07-01`, payload={weekend_off: true}
2. (발효 전) 근무자관리 화면엔 옛 값(false). "예정 변경 1건" 배지.
3. 7/1 자정 cron: Nurses.weekend_off=true 업데이트.

### 9.4. 미래 wanted 작성 (이동 예약 중)
1. 박혜미 7/1 A→B 예약된 상태. 현재 6/15.
2. 박혜미가 7월/8월 wanted 작성 시도. 권한 검사 → 7월·8월의 effective group=B → group_id=B로 저장 허용.
3. 7/1 자동 발효. 박혜미의 7월 generate는 B의 wanted를 사용.

### 9.5. 이동 취소
- 발효 전: NurseAssignment row → status=cancelled. cron 무시. NML/wanted 변화 없음.
- 발효 후 되돌리기: 새 transfer(B→A) 이벤트 append. 7/1에 발효된 NML group_id가 다시 update될 뿐.

---

## 10. 구현 우선순위

| Phase | 내용 | 소요 |
|---|---|---|
| **0** | 본 문서 (스키마·정책·발효 룰·권한 매트릭스) | ✓ 완료 |
| **1** | NurseAssignment 페이로드/kind 확장 + 시점-aware read 헬퍼 + 엔진 입력 빌더. 회귀 0 확인. | ~1~2주 |
| **2** | 즉시/예약 변경 트랜잭션 + 발효 cron + reconcile job + 취소 로직 | ~2주 |
| **3** | UI — 추가 차단, 병동이동 액션, 명단 3탭, dry-run, 예정 변경 배지 | ~2주 |
| **4** | 권한(시점-기반)·알림·외부동기화 + 파견 시 영구속성 차단 검증 | ~1~2주 |
| **5** | 회귀 가드 + watchlist 통합 (이동 시나리오 자동 회귀) | ~1주 |

**총 ~7~8주.**

---

## 11. 회귀 가드 (watchlist 통합)
- 기존 [Schedule Quality Watchlist](../memory) 항목에 다음 시나리오 추가:
  - 발효 전 wanted 입력 → 발효 → generate → 새 그룹에서 반영되는지
  - 발효 취소 → 자동 이전된 데이터 환원되는지
  - cross-month tail (옛 그룹 전월 N 꼬리 → 새 그룹 prev tail) nurse_id 기반 조회 정상
  - 파견 종료 후 dispatch_override 자동 효력 종료 + Nurses 본가 값 복귀
  - dispatch_override 권한 위반(파견지가 permanent_change 시도) 차단

---

## 12. 미결정/후속 검토 사항
- **이력 탭 노이즈**: 1~2년 운영 후 N개월 필터 도입 검토.
- **외부 동기화 주기**: HR/qpis 즉시 push vs 배치, 운영 운영방식 확정 필요.
- **속성별 인덱스 최적화**: JSON payload의 attribute별 쿼리가 느려지면 materialized projection 추가.
- **연쇄 이동 정책**: 같은 nurse 미래 발효 2건 시 거부 vs 합치기 UX 결정.

---

## 13. 결정 이력 요약 (출처: 2026-05-30 ~ 2026-06-01 설계 대화)

| 결정 | 채택 | 기각된 대안 |
|---|---|---|
| Snapshot 테이블 | 추가 안 함 | NurseMonthlySnapshot(과도한 설계로 판단) |
| 발효 단위 | 임의 일자 | 월 1일 강제 |
| 권한 모델 | 시점 기반 (DB) | 토큰 기반 |
| 파견 시 영구속성 | 본가만 직접, 파견지는 dispatch_override | 파견지 영구속성 직접 편집 |
| 승인 흐름 | C 단방향 | A 자동 / B 양쪽 승인 |
| wanted 이동 처리 | 안 건드림(자연 무시) | 삭제·자동 이전 |
| NML 이동 처리 | group_id 자동 update | 삭제 |
| 페이로드 | JSON | row-per-attribute |
| 이력 탭 가시 범위 | 무제한 | N개월 제한 |
| original_group_id | hint로만 유지 | 완전 제거 |
| 근무자 추가 정책 | 다른 그룹 소속자 옮기기 차단 | 자유 추가 |
| 명단 분리 | 별도 탭 (현재/예정/이력) | 한 리스트 혼합 |

---
**다음 단계**: Phase 1 착수 (NurseAssignment 페이로드/kind 확장 + 시점-aware read 헬퍼). 본 문서를 기준점 삼아 진행.
