# M Shift (미드) 적용 문서 - Backend/Front 공통

## 문서 목적
- `M(미드)` 근무 추가 작업의 배경, 구현 범위, API/화면 반영 포인트를 한 번에 공유한다.
- 백엔드/프론트가 같은 용어와 데이터 계약으로 작업할 수 있도록 기준을 제공한다.

---

## 1) 왜 수정이 필요했는가
- 기존 엔진/데이터 구조가 사실상 `D/E/N/O` 중심이라, `09:00-18:00`의 중간 근무(`M`) 운영 요구를 반영하기 어려웠다.
- 그룹별로 M 사용 여부를 다르게 운영해야 해서, 단순 코드 추가가 아니라 **토글 가능한 구조**가 필요했다.
- 월경계/전이 제약에서 M을 명시하지 않으면 생성 결과가 비일관해질 수 있어, 알고리즘 입력/제약까지 함께 확장해야 했다.

---

## 2) 핵심 설계 요약
- 신규 시프트 정의
  - `shift_id=M`, `default_shift=M`, `name=미드`, `09:00-18:00`, `color=#E6A817`, `sequence=5`
- ShiftManage
  - `slot=5`, `main_code=M`, 기본 `manpower=0`
- 그룹 토글
  - `use_mid=true`: M 수요/배치 활성
  - `use_mid=false`: slot 5 row는 유지하되 `manpower=0` 강제
- 전이 규칙
  - `prev in {D, O}`일 때만 `M` 허용
  - `M -> next` 추가 제한 없음

---

## 3) Backend 변경 포인트

### 3.1 모델/스키마
- `RosterConfig.use_mid` 추가
- `DailyShift.m_count` 추가
- DailyShift 요청/응답 스키마에 `mid`/`M`/`M_count` 필드 확장

### 3.1.1 DB 변경 사항 (필수 기록)
- 스키마
  - `dbo.roster_config.use_mid` (BOOLEAN/BIT, 기본 `false/0`)
  - `dbo.daily_shift.m_count` (SMALLINT, 기본 `0`)
- 데이터 기본값 정리
  - 기존 `roster_config` 행: `use_mid=false`로 정합화
  - 기존 `daily_shift` 행: `m_count=0`으로 정합화
- 운영 적용 메모
  - Alembic 미사용 환경은 SQL 적용 내역을 릴리즈 노트에 반드시 남긴다.
  - 컬럼 추가 후 API/엔진 반영 순서가 어긋나지 않도록 배포 순서를 관리한다.

#### 적용 SQL (예시)
```sql
ALTER TABLE dbo.roster_config
ADD use_mid BIT NOT NULL CONSTRAINT DF_roster_config_use_mid DEFAULT 0;

ALTER TABLE dbo.daily_shift
ADD m_count SMALLINT NOT NULL CONSTRAINT DF_daily_shift_m_count DEFAULT 0;

UPDATE dbo.roster_config
SET use_mid = 0
WHERE use_mid IS NULL;

UPDATE dbo.daily_shift
SET m_count = 0
WHERE m_count IS NULL;
```

#### 검증 SQL (예시)
```sql
SELECT COLUMN_NAME
FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'roster_config' AND COLUMN_NAME = 'use_mid';

SELECT COLUMN_NAME
FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'daily_shift' AND COLUMN_NAME = 'm_count';

SELECT COUNT(*) AS null_use_mid
FROM dbo.roster_config
WHERE use_mid IS NULL;

SELECT COUNT(*) AS null_m_count
FROM dbo.daily_shift
WHERE m_count IS NULL;
```

### 3.2 라우터/서비스
- ShiftManage 기본 슬롯 생성에 `slot 5 (M)` 포함
- daily-shift 월간/일간 업데이트에 `m_count` 반영
- config 저장 시 `use_mid=false`면 아래 정리 동작 수행
  - slot5 manpower=0
  - nurse work-type 목록(`is_night_nurse`)에서 `M` 제거
  - grade constraints JSON에서 `M` 키 제거

### 3.3 엔진(CP-SAT)
- 일자별 요구치(`daily_shift_requirements`)에 M 포함 가능하도록 확장
- M 전이 제약 추가
  - `x[n,d,M] <= x[n,d-1,D] + x[n,d-1,O]`
- fallback 경로에도 M 관련 처리 일관 적용

---

## 4) Front 반영 가이드

### 4.1 설정 화면
- 그룹 설정에 `use_mid` 토글 추가
- 토글 OFF일 때도 M 슬롯은 숨기지 말고 비활성/0명 상태로 표현 권장

### 4.2 일자별 필요 인원 화면
- 기존 `D/E/N` 입력 옆에 `M` 입력 추가
- 월간 일괄 입력(`monthly`)과 일별 배열 입력(`daily`) 모두 `M` 지원

### 4.3 시프트 관리 화면
- `slot 5 / M` 표시
- OFF 계열과 다르게 근무 시프트로 취급

### 4.4 생성 결과 표시
- 배정표/요약 통계에 M 컬럼 추가
- use_mid=false인 그룹은 M 값이 0으로 유지되는지 표시

---

## 5) API 계약(요약)
- `POST /roster/config/save`
  - `use_mid: boolean` 지원
- `PUT /daily-shift/monthly`
  - `mid: int` 지원
- `PUT /daily-shift/daily`
  - `M: int[]` 지원
- `GET /daily-shift`
  - `month_summary.M_count`, `date.M_count` 포함

---

## 6) 운영/검증 체크리스트
- `use_mid=false`
  - slot5 manpower=0
  - M 배정 미발생
- `use_mid=true`
  - m_count>0인 날짜에 M 배정 가능
- 전이 검증
  - `D->M`, `O->M` 허용
  - `E->M`, `N->M` 차단

---

## 7) 비고
- DB 마이그레이션 체계(Alembic) 미사용 환경에서는 컬럼 추가 SQL을 별도 적용해야 한다.
- 엔진/폴백/후처리 경로가 모두 존재하므로, 단일 모듈만 수정하면 불일치가 발생할 수 있다.
