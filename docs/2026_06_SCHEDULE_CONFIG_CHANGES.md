# 2026-06 근무표 생성 설정 변경 내역

> 작성: 2026-05-23
> 대상: 9B(병동, group=10135890c287), ICU(중환자실, group=10135857f9f9)
> 목적: 6월 근무표 생성 시 발생한 INFEASIBLE / 품질 저하를 해결하기 위한 정책·파라미터 조정 1차

본 문서는 6월 근무표 생성에 한해 수동/일회성으로 적용한 변경을 기록한다. 후속 단계에서 일부는 **글로벌 디폴트로 승격**, 일부는 **UI 노출 옵션으로 외부화**할 예정이다(섹션 4 참조).

---

## 1. 코드 변경

| 파일·라인 | 항목 | 변경 전 → 후 | 적용 범위 |
|---|---|---|---|
| `app/db/roster_config.py:144` | `NurseRosterConfig.team_min_soft_fallback` (dataclass 기본값) | `False` → `True` | **전역**(모든 그룹의 team_min 디폴트가 soft로 동작) |

### 동작 영향
- `team_min_by_team`이 설정된 그룹(9B 등)에서 팀별 일일 최소(D≥1, E≥1 등)가 hard → soft 슬랙으로 전환됨.
- 슬랙 패널티: `team_min_penalty_weight = 500` (`roster_config.py:145`, 미변경)
- ICU는 `team_min_by_team`이 비어있으므로(`[TeamMin] skip: cfg.team_min_by_team is empty`) 본 변경의 직접 영향은 9B 계열 그룹에 한정.

---

## 2. DB 데이터 변경 (MSSQL prod 적용 완료)

### 2-1. Grade 최소 인원: hard 전환

| 그룹 | 테이블 | 컬럼 | 변경 |
|---|---|---|---|
| 9B (10135890c287) | `roster_grade_config` (config_id=16) | `allow_soft_fallback` | `1 → 0` |
| ICU (10135857f9f9) | `roster_grade_config` (config_id=10) | `allow_soft_fallback` | `1 → 0` |

- Hard 의도: Grade D/E/N 최소(예: `{D:{1:1,2:2}, E:{1:1,2:2}, N:{1:1,2:2}}`)를 엄격 충족.
- 단, 엔진에는 **`roster_create_service.py:5238`의 AUTO-SOFT 재시도 경로**가 살아있다. `NO_ASSIGNMENT` / `MAX_CAP_SHORTAGE` / `GRADE_MAX_SUM_BELOW_NEED` 검출 시 1회 자동 soft 재시도. ICU 6월은 이 경로로 풀렸음(grade_min 위반 50건, 달성 51.7%, HTTP 200 + `applied_relaxations=['grade_hard_to_soft']`).

### 2-2. 야간 월 상한: ICU 6·7월

| 그룹 | 테이블 | 변경 |
|---|---|---|
| ICU (10135857f9f9) | `nurse_monthly_limits` | 활성 27명(정아영 271772 제외) × 2026-06/07 → `n_max=7` 일괄 upsert (INSERT 53, UPDATE 1) |

- 운영팀 요청에 따른 야간 부담 상한.

---

## 3. 변경하지 않은 항목 (확인만 수행)

- `RosterConfig.team_min_by_team` (DB 컬럼이 아니라 `teams.min_shift` → 런타임 주입)
- `RosterGradeConfig.use_dynamic_scaling = 1` (Grade 목표 동적 상향 활성)
- OFF 균등화 가중치(`cp_sat_basic.py:2957~`의 `_off_eq_weight = -200`) — `off_first=False` 경로에서 약함. 후속 작업 대상(섹션 4-B).
- `extra_off_penalty_weight = 80`

---

## 4. 후속 작업 가이드 — 디폴트 승격 vs UI 옵션화

> 본 1차 변경은 임시조치 성격. 다음 항목을 단계적으로 정리한다.

### 4-A. 디폴트로 승격 검토

| 항목 | 현재 | 권장 방향 | 근거 |
|---|---|---|---|
| `team_min_soft_fallback` | 이번에 전역 디폴트 True | **유지(전역 True)** | 9B 표유진 케이스처럼 인력 결손 시 hard로는 풀리지 않음. soft + 패널티가 안전 |
| Grade `allow_soft_fallback` | 그룹별 DB 컬럼 (9B/ICU만 0) | **그룹별 유지**, 단 기본은 1(soft) | hard는 운영팀이 명시적으로 원할 때만. 자동 fallback 경로(`roster_create_service.py:5238`)와 정책 일관성 정리 필요 |
| `use_dynamic_scaling` | 1(활성) | 검토 후 결정 | hard 모드에서 동적 상향이 INFEASIBLE을 키우는지 확인 필요 |

### 4-B. UI 옵션화 (그룹/사용자 설정으로 외부화)

| 항목 | 현재 위치 | 외부화 방향 |
|---|---|---|
| `team_min_soft_fallback` | 코드 디폴트만 존재 (DB 컬럼 없음) | `RosterConfig`에 컬럼 추가 → UI에서 그룹별 토글 |
| `team_min_penalty_weight` (500) | 코드 상수 | 그룹별 가중치 슬라이더 |
| `extra_off_penalty_weight` (80) | `config_dict` (이미 주입 가능) | UI에서 게이지 노출 |
| OFF 균등 강도 (`_off_eq_weight`) | 코드 상수(`-200`/`-100000`) | "OFF 균등 강도" 게이지로 통합 (off_first 분기 제거) |
| `n_max` (월별 야간 상한) | `nurse_monthly_limits` 컬럼 | 이미 DB 컬럼 — **UI 일괄 설정 도구 필요** (이번에 일회성 스크립트로 처리) |
| Grade hard/soft AUTO 재시도 | `roster_create_service.py:5238` 하드코드 | 그룹별 옵션(`auto_grade_soft_retry_enabled`)으로 노출 |

### 4-C. 구조적(데이터) 후속 과제

| 그룹 | 관찰 | 후속 |
|---|---|---|
| 9B | 표유진(390658) 5/26 영구 병동이동(target=101358f6de7b) → 9B에서 사용 불가, C팀 D/E 공급 부족 | `active=0` 처리 또는 인력 재배치 |
| ICU | g2 8명 vs 일일 D2+E2+N2=6명 요구 → 30일 수요 180명-day vs 최대 공급 168명-day(off=9 기준) → 산술 부족 | g2 일일 요구 하향(2→1) 또는 g3 일부 승급 검토 |

---

## 5. 검증 결과 요약 (2026-05-23)

| 그룹 | 월 | 1차(hard) 결과 | AUTO-SOFT 후 | 응답 |
|---|---|---|---|---|
| 9B | 6월 | INFEASIBLE (표유진 결손 + team_min 영향, 본 변경 직전 상태) | — | HTTP 500 `NO_ASSIGNMENT` |
| ICU | 6월 | 폴백 0~9 전 단계 INFEASIBLE | OPTIMAL (`relax_level=0`) | HTTP 200 + `applied_relaxations=['grade_hard_to_soft']`, grade_min 위반 50건 |

본 변경 적용 이후 9B 재실행은 아직 수행하지 않음. 다음 단계에서 표유진 처리와 함께 재검증 예정.
