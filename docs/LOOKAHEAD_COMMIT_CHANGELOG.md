# Lookahead 커밋 변경 기록

## 문서 목적
- Lookahead 커밋이 왜 필요했는지, 어떤 범위를 바꿨는지 구현 관점에서 요약한다.
- 기존 아키텍처 문서(`LOOKAHEAD_ARCHITECTURE.md`)와 별개로, 변경 이력을 빠르게 확인할 수 있도록 한다.

---

## 1) 왜 수정이 필요했는가
- 월말에서 해를 끊으면 다음 달 초에 OFF/N 몰림이 생겨 품질 저하 또는 infeasible 위험이 커졌다.
- 당월만 바라보는 최적화는 월경계 패턴 비용을 충분히 반영하지 못했다.
- 따라서 “다음 달 1~K일”을 가상 구간으로 포함해 당월 꼬리를 안정화할 필요가 있었다.

---

## 2) 무엇을 수정했는가

### 2.1 룩어헤드 확장 구간 도입
- 물리 일수(`D_phys`) 외에 확장 일수(`D_ext = D_phys + K_lookahead`) 개념 적용
- 변수/제약은 확장 구간까지 생성하되, DB 반영은 물리 구간만 유지

### 2.2 모듈 분리
- helper: `cp_sat/lookahead_helpers.py`
  - `get_D_ext`, `compute_leave_ext`, 구간 유틸
- constraints: `cp_sat/lookahead_constraints.py`
  - 룩어헤드 OFF cap 제약
  - 룩어헤드 분산 패널티 항

### 2.3 주휴/고정 셀 처리 확장
- 당월 주휴와 룩어헤드 주휴 셀을 분리 계산
- 룩어헤드 구간의 고정 OFF와 선택 OFF를 분리해 cap 제약에 반영

### 2.4 월총량 규칙 유지
- min/max OFF, 월총량 관련 판단은 당월 물리 구간 기준을 유지
- 룩어헤드 구간은 꼬리 안정화 제약용으로만 활용

### 2.5 DB 변경 사항
- 본 커밋 범위에서는 **신규 DB 스키마 변경 없음**.
- `lookahead_days`, `lookahead_weekly_off_cells` 등은 런타임 설정/계산값으로 처리되며, 물리 월 구간 결과만 기존 저장 경로로 반영한다.

#### 점검 SQL (스키마 변경 없음 확인)
```sql
-- 룩어헤드 커밋은 DB 컬럼 추가가 없으므로,
-- 핵심 테이블 컬럼 수/목록이 기존과 동일한지 확인
SELECT TABLE_NAME, COUNT(*) AS column_count
FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_SCHEMA = 'dbo'
  AND TABLE_NAME IN ('roster_config', 'daily_shift', 'schedule_entries')
GROUP BY TABLE_NAME
ORDER BY TABLE_NAME;
```

---

## 3) 같이 보완된 안정화 포인트
- 호출 인자/시그니처 불일치 정리
  - `add_lookahead_off_cap_constraints(..., leave_ext=...)` 형태로 통일
- 룩어헤드 구간 인덱스 오류 방지
  - 선호도 행렬 접근 시 물리 구간 외 인덱스 보호
  - 물리 roster 반영 시 leave 범위 cap 처리

---

## 4) 기대 효과
- 월말에서 과도하게 공격적인 배정을 줄여 다음 달 초 급격한 패턴 붕괴 완화
- 월경계 관련 hard fail 가능성 감소
- 룩어헤드 ON/OFF에 따른 동작 분리가 명확해져 디버깅/튜닝 용이

---

## 5) 검증 체크리스트
- `lookahead_days=0`
  - 기존 결과와 동등 동작(회귀 없음)
- `lookahead_days>0`
  - 생성 성공률/월경계 패턴 안정성 개선 여부 확인
- 인덱스 안정성
  - 확장 구간 접근 중 out-of-bounds 없음

---

## 6) 관련 문서
- 상세 설계/의사결정: `docs/LOOKAHEAD_ARCHITECTURE.md`
