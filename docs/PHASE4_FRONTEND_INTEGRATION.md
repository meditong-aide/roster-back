# Phase 4 — 근무자관리 월 셀렉터: 프론트 연결 API & 프론트 수정 내역

> 이 세션(2026-06)에서 추가/수정한 **프론트 ↔ 백엔드 연결 계약**과 **프론트 변경 파일** 정리.
> 설계 근거: `TEMPORAL_NURSE_MODEL_DESIGN.md` §3(정책 매트릭스)·§5(프론트 표시).
> 백엔드 구현 로그: `PHASE1_TEAM_PERIOD_IMPL.md`.
>
> - 백엔드 레포 `roster-back` 브랜치: `feat/even-de-on-dev` (최신 `2865c09`)
> - 프론트 레포 `roster_front` 브랜치: `feature/wanted-team-classify` (최신 `73a91d5a`)

---

## 1. 프론트에 연결된 API

### 1.1 `GET /nurses/members` — 근무자관리 월 셀렉터 (신규)

선택 **월의 '소속' 명단 + 상태 플래그 + 헤드카운트**. 가시성은 *근무일 수가 아니라 소속* 기준.

**Request (query)**
| param | type | 설명 |
|---|---|---|
| `group_id` | string | 대상 병동 (HN multi-group: 홈/original/관리그룹 허용. 권한 없으면 403) |
| `year` | int | 예: 2026 |
| `month` | int | 1~12 |

**Response**
```jsonc
{
  "members": [
    {
      "nurse_id": "208001",
      "name": "이유림",
      "membership_status": "active",   // active|inbound|outbound|dispatch_out|leave|resigned
      "marker": null,                   // "←"(전출) | "→"(전입) | null
      "badge": null,                    // "파견 중" | "휴직" | "퇴사" | "파견" | null
      "as_of_team": 2,                  // 그 달 팀(team_id, int). resolve_team 결과
      "as_of_grade": 1                  // 그 달 등급(int, 캐시/override)
    }
  ],
  "headcount": { "regular": 9, "moving": 6, "leave": 0 }
}
```

**상태/마커 정책** (백엔드 `group_members_in_month`가 결정 — 프론트는 표시만):
| 상황 | status | marker | badge | 노출 |
|---|---|---|---|---|
| 정상 소속 | `active` | — | — | ✅ |
| 병동이동 전입(이동月) | `inbound` | `→` | — | ✅ (그 달만) |
| 병동이동 전출(이동月·월중) | `outbound` | `←` | — | ✅ (그 달만) |
| 병동이동 정착(이동 다음 달~) | `active` | — | — | ✅ (마커 없음) |
| 병동이동 후 옛 병동 | — | — | — | **미표시**(그 달 선택해 봄) |
| 파견 나감(home) | `dispatch_out` | — | `파견 중` | ✅ (기간 내내) |
| 파견 인바운드(target) | `inbound` | — | `파견` | ✅ (기간 내내) |
| 휴직 / 퇴사 | `leave`/`resigned` | — | `휴직`/`퇴사` | ✅ |

> **핵심**: 마커(←/→)=**일회성 전이(병동이동)는 이동月에만**. 배지(파견/휴직)=**지속 상태는 기간 내내**.
> 헤드카운트 = 이동/휴직을 본 카운트와 분리(`정규·이동·휴직`).

### 1.2 `GET /PUT /nurses/monthly-limits` — 사이드 프로필 고급설정 (권한·경고 수정)

기존 엔드포인트인데 이 세션에서 2건 수정:
- **권한(403) 수정**: 홈 그룹만 허용하던 체크를 `assert_caller_can_access_group`(홈+original+관리그룹)로 교체. HN이 관리 병동 간호사의 월간 한도를 **조회·수정** 가능. (이전엔 403이라 프론트에 한 줄도 안 보였음)
- **N전담 soft 경고**: 저장(PUT) 응답 `warnings[]` 에 "N전담인데 N 한도 낮음" 경고를 실어 내림(저장은 허용). 하드 차단 status는 500→422.
  - **프론트 처리는 이미 존재**: `useNurseMonthlyLimits.ts` 의 저장 onSuccess 가 `response.warnings?.forEach(w => toast.info(w.message))` → **프론트 코드 변경 없음**.

---

## 2. 프론트 수정 내역 (`roster_front`)

### 2.1 신규/추가
| 파일 | 내용 |
|---|---|
| `src/api/nurses.ts` | `fetchGroupMembers(groupId,year,month)` + 타입 `GroupMemberInMonth`/`GroupMembersResponse`/`MembershipStatus` 추가 |
| `src/hooks/useGroupMembers.ts` | **신규** react-query 훅. `GET /nurses/members` 호출. isAdmin 게이팅 없이 `selectedGroupId` 그대로 전달(백엔드가 권한검증). queryKey `[NURSES,"members",gid,year,month]`, `enabled` = gid·year·month 존재 시 |

### 2.2 근무자관리 페이지/테이블 수정
| 파일 | 변경 | 단계 |
|---|---|---|
| `src/pages/roster-management/Head_nurse_management.tsx` | 상단 툴바에 **월 스테퍼(‹ YYYY년 M월 ›)** + **헤드카운트("정규·이동·휴직")**. `useGroupMembers` 호출 + `memberStatusMap`(nurse_id→member) 구축해 명단에 전달 | 토대/B-1 |
| `src/pages/roster-management/components/NurseManagementTables.tsx` | `memberStatusMap` prop 받아 4개 `ManagerTable` 호출에 전달. **B-2**: 월 데이터 있으면 표시 행=그 달 소속만(완전전출 숨김/인바운드 포함), TEAM 그룹핑·미지정을 `as_of_team` 기준으로(가드: 비었으면 기존 동작) | B-1·B-2 |
| `src/pages/roster-management/components/ManagerTable.tsx` | `memberStatusMap` prop → `NurseTableRow`에 `membership={map.get(nurse_id)}` 전달 | B-1 |
| `src/pages/roster-management/components/NurseTableRow.tsx` | `membership?` 옵셔널 prop. 이름 옆 **상태 배지**(전입→/전출←/파견 중/휴직 …). status=active거나 prop 없으면 미표시 | B-1 |

> **무회귀 가드**: 모든 추가가 옵셔널 prop/가드 — `memberStatusMap`이 비어있거나(로딩·미연결) 백엔드가 status를 안 주면 **기존 `useNurses` 테이블 동작 100% 동일**.

### 2.3 커밋 매핑 (롤백 단위 — 각 단독 revert 가능)
| 커밋 | 내용 |
|---|---|
| `7824e9bc` | 월 셀렉터 + 헤드카운트 + api/훅 (토대) |
| `3fb20ae0` | **B-1** 행 소속 배지(오버레이) |
| `7e80d4a8` | **B-2** 월-정확 명단 + as_of_team 그룹핑 |
| `73a91d5a` | 원격(페이징/grade·team config hidden) 머지 — `NurseTableRow` union 해소(`addNurseModal`+`membership` 둘 다 유지) |

---

## 3. 미반영 / 주의
- **P4-3 (사이드 프로필 선택-월 as-of)** 미착수 — 사이드 프로필은 아직 선택 월 기준 team/grade 세그먼트 표시 안 함.
- **team 라벨 불일치**: `as_of_team`은 `team_id`(1/2/3) 반환. 재분배 UI는 "1/2/3팀", 팀관리 페이지는 team_name("A/B/C팀", `teams` 테이블에 team_id 1=B팀 등). 데이터는 일관(team_id)이나 **화면 라벨 혼선 가능** — 정합 필요.
- **화면 눈 검증 권장**: 백엔드 tsc/단위테스트는 통과했으나, 원격 소속컬럼+배지 동시 렌더·B-2 월별 명단 전환은 dev 서버로 시각 확인 권장.
- shiftrule(일자별) phase는 **보류**(설계 §6 참조).
