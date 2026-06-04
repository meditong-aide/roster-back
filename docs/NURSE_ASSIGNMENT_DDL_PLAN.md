# NurseAssignment DDL 계획서 (Phase 1.4)

작성: 2026-06-01
관련: [docs/NURSE_GROUP_CHANGE_MODEL.md](./NURSE_GROUP_CHANGE_MODEL.md) §1
환경: 프로덕션 MSSQL

---

## 0. 목적
간호사 그룹 변경 통합 모델(Phase 0 결정물)의 신규 케이스 — `permanent_change`/`dispatch_override` 등 — 를 지원하기 위해 `nurse_assignment` 테이블에 최소 컬럼·인덱스를 추가한다.

**적용 시점**: Phase 1.1~1.3 헬퍼·회귀 검증 완료 후, Phase 2 (cron) 진입 전.

---

## 1. 변경 사항

### 1.1. ALTER (3건)
```sql
-- ① kind 컬럼: 현재 reason 텍스트 기반 → 명시적 enum-like
ALTER TABLE nurse_assignment
  ADD kind VARCHAR(30) NOT NULL CONSTRAINT DF_na_kind DEFAULT 'transfer';

-- ② payload JSON (영구속성 변경 등 신규 케이스용)
ALTER TABLE nurse_assignment
  ADD payload NVARCHAR(MAX) NULL;
-- (선택) ADD CONSTRAINT CK_na_payload_json
--   CHECK (payload IS NULL OR ISJSON(payload) = 1);

-- ③ 시점-effective lookup 인덱스 (헬퍼가 매 generate마다 hit)
CREATE INDEX idx_na_nurse_kind_date
  ON nurse_assignment (nurse_id, kind, start_date)
  INCLUDE (end_date, status);

-- ④ 그룹별 활성 조회 (기존 inbound + 신규 outbound)
CREATE INDEX idx_na_target_active
  ON nurse_assignment (target_group_id, status, start_date, end_date);
```

### 1.2. kind 값 정의 (코드 enum과 동기화 — `assignment_service.REASON_TO_KIND`)
| 값 | 한글 reason | 의미 | Nurses 동기화 |
|---|---|---|---|
| `transfer` | 병동이동 | 영구 병동이동 | 발효 시 `group_id` 업데이트 |
| `dispatch` | 파견 | 시간 한정 파견 (본가 유지) | 안 함 |
| `preceptee` | 프리셉티 | 신규 교육 배정 (그룹·속성 변경 없음) | 안 함 |
| `leave` | 휴직 | 휴직 | 발효 시 `active=0` |
| `return` | 복직 | 복직 | 발효 시 `active=1` |
| `resign` | 퇴사 | 퇴사 (별도 `flush_resigned_nurses` 처리) | 발효 시 삭제/비활성 |
| `dispatch_override` | (Phase 2) | 파견 기간 한정 속성 변경 | 안 함 (assignment row만) |
| `permanent_change` | (Phase 2) | 영구 속성 변경 | 발효 시 attribute 업데이트 |

> **`transfer`는 DB DEFAULT이자 미분류 fallback이다.** 신규 reason 추가 시 `REASON_TO_KIND`와 본 표를 함께 갱신해야 default 오염을 막는다.

---

## 2. 백필 (기존 row의 kind 채움)

### 2.1. 사전 분포 확인 (필수, 실행 전)
```sql
SELECT TOP 50 reason, COUNT(*) cnt
FROM nurse_assignment
GROUP BY reason
ORDER BY cnt DESC;
```
→ reason 텍스트 종류를 운영자가 검토. 매핑 규칙 정밀화.

### 2.2. 매핑 UPDATE (배치, 1000건/회)
실측 분포(2026-06-04): 프리셉티 11 / 파견 10 / 병동이동 3 (총 24행).
프리셉티가 최대 그룹이라 백필 누락 시 `transfer`로 오염됨 → `preceptee` 분류 필수.
```sql
UPDATE nurse_assignment SET kind='preceptee' WHERE reason LIKE N'%프리셉%' AND kind='transfer';
UPDATE nurse_assignment SET kind='dispatch'  WHERE reason LIKE N'%파견%'   AND kind='transfer';
UPDATE nurse_assignment SET kind='transfer'  WHERE reason LIKE N'%이동%'   AND kind='transfer';
UPDATE nurse_assignment SET kind='leave'     WHERE reason LIKE N'%휴직%'   AND kind='transfer';
UPDATE nurse_assignment SET kind='return'    WHERE reason LIKE N'%복직%'   AND kind='transfer';
UPDATE nurse_assignment SET kind='resign'    WHERE reason LIKE N'%퇴사%'   AND kind='transfer';
-- 적용 후 기대 분포: preceptee 11 / dispatch 10 / transfer 3, 잔여 default 0
```

> **인덱스(§1.1 ③④)는 현재 24행 규모에선 생략.** 옵티마이저가 스캔을 택하므로 이득 없음.
> `nurse_assignment`가 수백 행 이상으로 커지면 그때 `idx_na_nurse_kind_date`만 추가.

### 2.3. 검증
```sql
SELECT kind, COUNT(*) FROM nurse_assignment GROUP BY kind;
```
→ 분포가 합리적인지 확인. 'transfer'가 비정상적으로 많으면 백필 누락 의심.

---

## 3. 적용 순서 (프로덕션 안전 룰)

| 단계 | 작업 | 비고 |
|---|---|---|
| 0 | **DB 백업** | memory directive 준수 |
| 1 | 사전 분포 쿼리 실행 | reason 종류 파악, 매핑 규칙 확정 |
| 2 | ALTER ADD COLUMN ① ② | MSSQL 2012+에서 metadata-only, 즉시 적용 |
| 3 | CREATE INDEX ③ ④ | Enterprise edition: `WITH (ONLINE=ON)`. Standard: 새벽 무중단 시간 |
| 4 | 백필 UPDATE (배치) | 1000건/회, 락 풀어줌 |
| 5 | 검증 쿼리 (분포) | kind별 분포 합리적인지 |
| 6 | 헬퍼 코드 갱신 | `nurse_effective.py`에서 kind 활용 (Phase 2와 함께) |

---

## 4. 롤백 절차

### 4.1. 코드만 롤백
- 헬퍼/cron 코드를 이전 버전으로 되돌림.
- 컬럼은 그대로 두되 안 씀 → 데이터 손실 없음.

### 4.2. 스키마까지 롤백 (비추 — 백업 복원 우선)
```sql
DROP INDEX idx_na_target_active ON nurse_assignment;
DROP INDEX idx_na_nurse_kind_date ON nurse_assignment;
ALTER TABLE nurse_assignment DROP CONSTRAINT DF_na_kind;
ALTER TABLE nurse_assignment DROP COLUMN payload;
ALTER TABLE nurse_assignment DROP COLUMN kind;
```
**경고**: `DROP COLUMN`은 데이터 손실. 가능한 한 **백업 복원**을 우선.

---

## 5. 의도된 인덱스 사용 패턴

| 쿼리 | 인덱스 |
|---|---|
| `get_active_assignment(nurse_id, as_of)` | `idx_na_nurse_kind_date` |
| 기존 inbound (`target_group_id=current AND status='active'`) | `idx_na_target_active` |
| Phase 2 cron: 오늘 발효 처리 (`start_date <= today AND status='active'`) | `idx_na_nurse_kind_date` |
| 그룹별 outbound (`source_group_id=current AND target_group_id≠current`) | (기존 FK 인덱스 또는 신규 필요 시 추가) |

---

## 6. 미결정/주의

- **payload JSON 사용 시작 시점**: Phase 2에서 `permanent_change`/`dispatch_override` 도입과 함께. 그 전엔 NULL만.
- **ISJSON 체크 제약**: 강하게 강제하려면 추가, 유연성 원하면 생략. 권장은 추가(잘못된 JSON 입력 차단).
- **kind 디폴트 'transfer'**: 백필 누락 시에도 안전한 선택이지만, 운영 운영 후 reason 분포 보고 정밀화 필요.
- **외부 시스템 영향**: 본 DDL은 우리 측 테이블만. HR/qpis 연동은 별도 hook (Phase 4).

---

## 7. 다음 단계 (Phase 2 진입 조건)
1. ✅ 본 DDL 계획 검토 완료
2. ⏳ 운영자 백필 매핑 규칙 확정 (분포 쿼리 결과 보고)
3. ⏳ 프로덕션 DB 백업
4. ⏳ DDL 실행
5. → Phase 2 cron 구현 착수
