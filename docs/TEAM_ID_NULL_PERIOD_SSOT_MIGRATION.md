# team 이행: `nurses.team_id` → `nurse_team_period` 단일 SSOT (캐시 NULL)

> 팀 소속을 **오롯이 `nurse_team_period`(시점 구간)** 로만 관리한다. `nurses.team_id`(캐시)는
> 일괄 NULL 처리하고 추후 컬럼 DROP. 모든 리더가 period 를 보도록 정렬하는 이행 기록.
> 선행: [TEMPORAL_NURSE_MODEL_DESIGN.md](TEMPORAL_NURSE_MODEL_DESIGN.md) v3 · [PHASE1_TEAM_PERIOD_IMPL.md](PHASE1_TEAM_PERIOD_IMPL.md).
> 최초 작성 2026-06-12.

## 0. 배경 / 목표

`nurse_team_period` 가 이미 팀의 진실(SSOT)이고 `nurses.team_id` 는 "현재값 캐시"였다
(TEMPORAL §1-5). 그러나 일부 리더가 여전히 캐시(`nurses.team_id`)를 직접 읽어, 캐시와
period 가 갈리면 화면·통계가 옛 팀을 보였다.

**사용자 결정**: 캐시를 더는 신뢰하지 않는다. `nurses.team_id` 전체 **NULL**, 앞으로 팀은
**오직 period(월별 '팀 설정')** 로만 지정. 추후 컬럼 DROP.
- backfill **안 함**(현재 캐시값을 period 로 옮기지 않음). **클린슬레이트** — NULL 직후 전원
  미배정으로 보이고, **그룹마다 '팀 설정' 1회**(open period 생성)로 재설정 → 이후 자동 carry-forward.
- 전제 워크플로: **그 달 팀 period 가 있어야 솔버 팀 제약(team_min·team_balance·grade_handoff)이
  작동**. open period 는 다음 달로 자동 상속(`resolve_team_for_roster` 가 `valid_from ≤ 월초 &&
  (valid_to IS NULL OR > 월초)`, 최신 `valid_from` 우선)이라 변경 시에만 재설정.

## 1. 시점 해석 규칙 (carry-forward)

`[valid_from, valid_to)` 반쪽열림. `valid_to=NULL` = 열린 구간 → **이후 모든 달 커버**.
새 구간은 더 늦은 `valid_from` 으로 만들 때만 그 시점부터 override(close-before-open).
→ 다음 달 설정 없으면 직전 open period 가 그대로 보인다(화면·생성 동일).

## 2. 코드 변경 (backend 6파일)

| 파일 | 변경 | 분류 |
|---|---|---|
| `services/team_period.py` | **+`resolve_teams_for_month(db, group_id, on_date)`** — 그룹 전체 활성 간호사의 시점 팀을 **period 우선 + ward-aware 캐시 폴백**으로 배치 해석(2쿼리). NULL 이행 후 폴백이 비어 자동 period-only. | 헬퍼 신규 |
| `services/team_service.py` | `list_teams_with_members(+year, month)` → 멤버를 위 헬퍼로 집계(`Nurse.team_id == t.team_id` 직접조회 폐기). `apply_team_ops` 저장 응답도 그 달(year/month) 기준 반환. | C |
| `routers/teams.py` | `GET /teams` 에 `year`/`month` 쿼리 추가(없으면 오늘 시점). | C |
| `services/team_classify_service.py` | ① `team_ids` 를 **`teams` 테이블(정의된 팀)** 에서(캐시 distinct 폐기 — NULL 후 "팀 없음" 방지). ② `current_team`·apply skip 비교를 period(`resolve_teams_for_month`)로 → **멱등**(캐시는 period 기록 후 갱신 안 돼 재실행 시 skip 실패하던 문제 해소). | C |
| `services/ward_redistribute_service.py` | `_same_team(n.team_id, …)` → `_same_team(resolve_team(db, nid, cur_g, effective), …)` (이미배정 skip 판정을 period 로). | C |
| `services/assignment_service.py` | flush 의 team 캐시 갱신(`nurse.team_id = target_team_id`)은 **유지**(레거시 미러, 아래 §4). 주석만 보강. | E |

### 프론트 = 변경 0
근무자관리·팀설정·편집프로필 팀 표시가 **이미 `as_of_team ?? cache` (period-first)**.
`as_of_team` 은 백엔드 `group_members_in_month`/`_resolved_team`(period 우선+폴백)에서 옴.
→ NULL 후 캐시 폴백이 자동 null 화 → period-only. (`NurseManagementTables` synth 행도
`member.as_of_team` 기반, `NurseEditSideProfile.currentTeam`·`initTeam` 도 as_of_team 기반.)

## 3. 응답 `team_id` 필드 = NULL-ok (변경 0)

`nurse_service:349·543`, `roster_service:1388` 의 `"team_id": nurse.team_id` 는 **그대로**
(NULL 후 null 반환). 프론트 전 소비처가 as_of_team 우선이라 표시는 period 로 정확.
`roster_create:5199·5546` 은 **주입된 engine_nurses**(생성 직전 `resolve_team_for_roster`
주입, `roster_create_service.py:4467/4474`)를 읽어 이미 period 정확.

## 4. flush(발효 cron) 결정 — 캐시 미러 유지

`flush_pending_permanent_changes`(`assignment_service.py:1774`)는 발효일에
`nurse.team_id = target_team_id` 로 캐시를 갱신한다. team 은 생성시 이미 period 기록([B3])이라
이 캐시 쓰기는 **중복**. period 로 바꾸는 시도를 했으나 확립된 계약 테스트 3개
(`test_flush_*`)를 깨고 solver-인접 일배치라, **되돌려 캐시 미러로 유지**한다.
- 근거: NULL 후 **모든 리더가 period 를 보므로 이 캐시값은 무시**(무해·불가시). 사용자 목표
  (서비스 안 깨짐·혼선 없음)에 불필요.
- **Milestone 2(컬럼 DROP)** 시 이 writer + 나머지(legacy add/clear) + 모델 컬럼 + 테스트를
  **일괄** 정리한다. 그때 flush→period 로 전환 + 테스트를 `resolve_team` assert 로 갱신.

### 나머지 writer
- `team_service:238` legacy add(`update team_id`) = `PUT /teams` 가 year/month 필수라
  **도달 불가(dead)**.
- clear(`assignment:1410/1473`, `team_service:251/257`) = `team_id=None` → NULL 과 정합.
- `team_classify_service:_current_team_ids`(151) = 호출 0 **죽은 코드**(무해, DROP 때 제거).

## 5. 실행 순서 ★(어기면 dev 깨짐)

```
1. 코드 커밋·푸시 → GHA 로 dev 배포 (API EC2 + solver Lambda)     ← 사용자
2. nurses.team_id 일괄 NULL  (스크립트 /tmp/team_id_null.py DRY_RUN=False
                              또는  UPDATE nurses SET team_id=NULL WHERE team_id IS NOT NULL)
3. 그룹마다 '팀 설정' 1회 재저장(open period) → 이후 자동 carry-forward
4. 검증 (팀 화면·근무표 생성 팀 제약)
```

배포 **전** NULL 하면 현재 배포된 구코드(`list_teams_with_members` 가 캐시 직접조회)가
빈 팀을 반환한다. 배포 후엔 전 리더가 period 라 안전.

> DRY_RUN 실측(2026-06-12, dev): `nurses` 총 829 / `team_id` NOT NULL **259**(active 251)
> = NULL 대상 259. 18개 그룹.

## 6. 검증

- 6파일 구문 OK.
- team 회귀 스위트 **84 passed**(`test_team_classify`/`_api`, `test_team_period`,
  `test_ward_redistribute`/`_api`, `test_team_auto_assign_fixed`,
  `test_team_min_shift_monthly_limits_validation`, `test_permanent_change`,
  `test_outbound_transfer_visibility`).

## 7. 영향 / 비영향

- **알고리즘 영향 없음**: 솔버는 이미 생성 직전 `resolve_team_for_roster` 로 period 팀을
  engine nurses 에 주입(`4467/4474`). backfill-불요·NULL 후도 동일(period 가 진실).
  단 **그 달 period 없는 간호사는 team=None** → 팀 제약 비대상(클린슬레이트의 의도).
- **혼선 없음**: 모든 화면이 as_of_team(period-first) → NULL 후 period-only 로 자연 수렴.
- **남은 일(Milestone 2, 별도)**: `nurses.team_id` 컬럼 DROP + writer/모델/테스트 일괄 정리.
