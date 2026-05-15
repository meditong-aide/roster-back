# Prepend 전환 설계안 (전월 특수처리 단순화 + 정책성 제약 유지)

## 1) 배경과 목표

현재 월경계 처리에서 전월 꼬리 정보를 해석해 월초 셀(1일, 2일 등)에 `forced_off`, `forbidden`을 추가로 박는 분기가 존재한다.  
이 구조는 동일 셀에 대해:

- (A) 전월 파생 분기 제약
- (B) 기존 동월 하드제약(연속근무/전이/회복)

이 중복 적용되면서 충돌 회피용 조건문이 누적되는 문제가 있다.

**전환 목표**

1. 전월 꼬리를 horizon 앞에 pin(prepend)해서 경계 제약을 동일 하드제약으로 처리
2. 전월 파생 `forced_off/forbidden` 분기 제거
3. 단, **당월 정책/인사/운영성 `forced_off/forbidden`은 유지**

---

## 2) 핵심 원칙 (중요)

### 원칙 A — `forced_off/forbidden` 프레임은 유지

`forced_off/forbidden` 자체를 없애는 것이 아니라, **출처별로 분리**한다.

### 원칙 B — 제거 대상은 “전월 파생 분기”만

제거/비활성 대상:

- 전월 마지막 패턴(E→D, N회복, 연속근무 꼬리 등)을 월초 셀에 직접 투영하는 분기
- 전월 해석 결과를 `forced_off`로 만든 뒤 다시 `fixed_cells`로 전환하는 중복 경로

유지 대상:

- 주말휴무/주휴
- 휴가/고정휴무/특별오프
- N전담/개인 속성 기반 타근무 불가
- 직급/팀/역할 기반 금지
- 유저 고정값/수동 고정

---

## 3) 제약 출처 분리 모델

`forced_off/forbidden` 생성 시 아래와 같이 provenance(출처)를 명시한다.

- `source=policy_weekly_off`
- `source=policy_leave_fixed`
- `source=policy_night_dedicated`
- `source=policy_grade_team`
- `source=user_fixed`
- `source=carryover_prev_month`  ← prepend 전환 후 제거 대상

운영 규칙:

1. `carryover_prev_month`만 flag ON 시 생성 금지
2. 나머지 source는 기존과 동일하게 유지
3. 로그/디버그/하네스에서 source별 카운트를 리포트

---

## 4) Day Scope 계약 (스코프 누락 방지)

prepend 전환의 실제 리스크는 월집계 스코프 누락이다. 아래 3스코프를 강제한다.

- `phys`: 이번 달 물리 일자만 (월 OFF/N cap, 공정성 집계)
- `full`: prepend + 본월 + lookahead (연속/전이/회복 윈도우)
- `coverage`: 본월(+정책상 lookahead) 커버리지, prepend 제외

`iter_nurse_days(..., scope=...)` 호출에서 scope 인자를 필수로 하여 의도를 강제한다.

---

## 5) 구현 설계 (파트별)

### 5.1 Solver Core (`cp_sat_basic.py`, `day_windows.py`, 관련 constraints)

1. `K_prepend`, `D_phys`, `K_lookahead`를 기준으로 정규 day range 생성
2. 월합산 루프를 `phys` 범위로 통일
3. 슬라이딩 제약(연속근무, N회복, 전이)은 `full` 범위 사용
4. `daily_shift_requirements_by_day`는 prepend 구간에 요구치 미적용

### 5.2 Cross-month 생성 경로 (`roster_create_service.py` 등)

1. `cross_month_prepend_mode` 플래그 추가
2. ON 시: prev-tail을 fixed pin으로 주입
3. ON 시: `source=carryover_prev_month`의 `forced_off/forbidden` 생성 비활성
4. OFF 시: 기존 경로 유지(롤백 가능)

### 5.3 Fallback 경로 (`cp_sat/fallback_lex.py`)

1. 월합산 raw range를 `phys` 기준으로 통일
2. primary와 동일한 day scope 계약 적용
3. primary/fallback parity 하네스에 포함

### 5.4 Harness/Diagnostics (`tools/harness`, `/ontology`, `/constraint_impact`)

1. source별 제약 카운트 수집
2. prepend ON/OFF diff에서 hard family별 결과 비교
3. infeasible 시 conflict core에 source 분포 포함

### 5.5 Agent/문서 정합 (`CLAUDE.md`, `.aide/AGENTS.md`, prompt artifacts)

1. “전월 파생 분기 제거 + 정책성 제약 유지”를 도메인 규칙으로 명시
2. AGENTS/DOMAIN_KNOWLEDGE의 월경계 설명 업데이트
3. 운영자 설명문구: “전월 데이터는 입력으로 사용, 전월 전용 강제분기는 미사용” 고정

---

## 6) 단계별 마이그레이션

### Phase 0 — 무동작 리팩토링

- scope 계약 도입 (`phys/full/coverage`)
- provenance 필드 도입
- 동작 동일성 회귀 (prepend 비활성)

### Phase 1 — prepend 경로 추가 (Dual-Stack)

- flag ON 시 prepend pin 활성
- 기존 cross-month 분기와 병행 실행하여 diff 비교

### Phase 2 — 전월 파생 source 제거

- `carryover_prev_month` 생성 비활성
- 정책성 source만 남김

### Phase 3 — 컷오버

- flag 기본값 ON
- 안정화 후 obsolete 분기 정리

---

## 7) 검증 기준 (Go/No-Go)

### 필수 통과

1. 월 OFF/N cap 집계가 `phys` 범위와 100% 일치
2. primary/fallback 월합산 결과 일치
3. hard family 위반 패턴 악화 없음
4. infeasible 비율 악화 없음(표본군 기준)
5. source 리포트에서 `carryover_prev_month`가 ON 모드에서 0

### 권장 통과

- 공정성 지표(KLD/even-night) 동등 또는 개선
- 월초 셀 충돌 로그(중복 강제/금지) 유의미 감소

---

## 8) 리스크와 대응

### 리스크 1: 월합산 누락(가장 큼)

- 증상: 이번 달 OFF/N 카운트가 1~2개 미묘하게 틀어짐
- 대응: scope 강제 + raw range 금지 lint + parity 하네스

### 리스크 2: primary/fallback 불일치

- 증상: 동일 입력에서 solver 경로별 결과 상이
- 대응: fallback도 동일 scope/provenance 계약 적용

### 리스크 3: 정책성 제약까지 실수로 제거

- 증상: 주휴/휴가/N전담 보호 약화
- 대응: source 분리 후 `carryover_prev_month`만 제거, 나머지 source snapshot 테스트

---

## 9) 최종 결론

prepend 전환의 정답은:

- **전월 데이터 사용은 유지**
- **전월 전용 강제 분기 제거**
- **정책성 `forced_off/forbidden` 유지**

즉, `forced_off/forbidden`은 폐기 대상이 아니라 **출처 정리 대상**이며,  
이번 전환의 성공 조건은 “경계 로직 단순화 + 월집계 스코프 안전성 확보”다.
