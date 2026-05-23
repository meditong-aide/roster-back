# Harness Branch Protection Operations Guide

> 목적: `harness-dev-gate`를 GitHub Branch Protection의 required check로 안전하게 켜고/끄고/완화하는 운영 절차를 표준화한다.

---

## 1) What this controls

- 대상 체크: **`Harness Dev Gate / harness-dev-gate`**
- 체크 소스: `.github/workflows/harness-dev-gate.yml`
- 효과: required check로 지정 시, 해당 체크가 성공해야 브랜치 머지 가능

---

## 2) Enable (강제 켜기)

### GitHub UI 경로
1. Repository → **Settings** → **Branches**
2. 보호할 브랜치(`dev`, 필요 시 `main`)의 Branch protection rule 생성/수정
3. **Require status checks to pass before merging** 활성화
4. Required checks 목록에서 `Harness Dev Gate / harness-dev-gate` 선택
5. 저장

### 권장 기본값
- `dev`: required check **ON** (강제)
- `main`: 운영팀 정책에 맞춰 단계적 적용

---

## 3) Disable (즉시 해제)

긴급 상황(외부 API 장애, 토큰 만료, 인프라 이슈)에서는 아래 중 하나로 빠르게 완화 가능:

1. Branch protection의 required check 목록에서
   `Harness Dev Gate / harness-dev-gate` 제거
2. 또는 rule 자체에서 status check requirement 일시 비활성

> 이 조치는 머지 차단만 해제하며, 워크플로 파일은 그대로 유지 가능.

---

## 4) Tune (강도 조절)

### 4.1 적용 범위 축소
- `.github/workflows/harness-dev-gate.yml`의 `on.push.paths`를 더 좁혀
  하네스/스케줄러 변경에만 실행되도록 조정

### 4.2 실행 비용 완화
- `repeats` 기본값 축소 (예: 2 유지)
- 필요 시 `workflow_dispatch` 중심 운영

### 4.3 강도 강화
- Branch protection에 required check 유지
- 팀 규칙으로 PR 머지 전 `summary.json` 첨부 의무화

---

## 5) Required Secrets

`harness-dev-gate`가 성공하려면 repository secrets가 필요:

- `HARNESS_BASE_URL`
- `HARNESS_ACCESS_TOKEN`

비어 있으면 워크플로는 의도적으로 실패한다.

---

## 6) Failure Playbook (실패 시 대응)

1. Actions artifact에서 `summary.json`, `triage.md` 확인
2. 실패 유형 분류
   - `blocking_fail_count > 0`: 실제 규칙 위반
   - `blocking_skipped_count > 0`: 미구현 metric/strict 위반
   - `graph_consistency` mismatch: export 정합성 이슈
3. 필요 시 일시 완화(Section 3) 후 원인 수정
4. 수정 후 required check 재활성

---

## 7) Team Policy Recommendation

- 기본: `dev` required check ON
- 장애 시: 임시 OFF 가능 (사유를 PR/이슈에 기록)
- 복구: 24시간 내 재활성화 권장
