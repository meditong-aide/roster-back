# 인천의료원 41병동-RN 근무표 세팅 적용안 (Phase 1 설정 + Phase 2 데이터)

작성 2026-07-27 · 대상 `office_id=102243` / `group_id=1022438ea001` / `config_id=589`(version 0 "기본 설정")
운영 DB `eun_roster` 실측 기반. **본 문서는 적용 지시서이며, 실제 쓰기(API 호출·화면 입력)는 사용자가 수행한다.**

---

## 0. 적용 전 스냅샷 (2026-07-27 실측, MSSQL read-only)

| 항목 | 실측값 |
|---|---|
| 활성 간호사 | 25명 (grade1=8 · grade2=11 · **grade3=6**) |
| 팀 | 4팀(A/B/C/D) · `teams.min_shift` 전부 NULL |
| 팀 배정 | `nurse_team_period` **23명 등록**(1:6 / 2:5 / 3:6 / 4:6, 전원 `valid_from=2026-07-01`, `valid_to=NULL`) · **미배정 2명**(291306 종미영, 291382 윤지선) |
| `shift_manage` | **0행** |
| `daily_shift` | **0행** |
| `nurse_monthly_limits` | **0행** |
| `nurse_preceptee_period` | **0행** |
| `fixed_wanted_entries` | 0행 |
| `schedules` | 0행 (생성 이력 없음) |
| 허용근무형(`nurses.is_night_nurse` = ORM `allowed_shifts`) | 최수진A(291362) / 최수진B(301324) = `["D","E"]`, 나머지 23명 = `[]`(제한없음) |
| 근무코드 12종 | D · E · N · M · O · 주 · 보건(공가) · 감(휴가) · 노조(공가) · **수면(휴가)** · 휴(휴가) · 산전(휴가) |

### roster_config 589 현행값 (Phase 1 페이로드의 베이스)

```
day_req=NULL  eve_req=NULL  nig_req=NULL
min_exp_per_shift=3   req_exp_nurses=1     two_offs_per_week=False
max_nig_per_month=15  three_seq_nig=True   two_offs_after_three_nig=False
two_offs_after_two_nig=False               banned_day_after_eve=True
max_conseq_work=6     off_days=11.0        max_conseq_off=NULL
shift_priority=0.8    sequential_offs=True nod_noe=False
not_one_night=False   use_mid=False        preceptee_on=False
preceptee_shift_count=True                 weekly_off_group=0
fixed_wanted_use_yn=False  show_level=True show_preceptor=True
off_first=False       off_swap_enabled=False
```

---

## 1. ★ 실행 순서 (역순 금지)

**Phase 2-A(필요인원) → Phase 2-B/C → Phase 1(설정 저장)** 순서로 진행한다.

이유: `POST /roster/config/save` 는 요청 본문의 `day_req/eve_req/nig_req` 를 **무시하고 `shift_manage` 에서 재계산해 덮어쓴다**
([roster_service.py:298-326](../app/services/roster_service.py#L298-L326)).
`shift_manage` 가 0행인 상태에서 설정을 먼저 저장하면 `day_req=eve_req=nig_req=3` 이 박히고, 이후 필요인원을 5/4/2 로 넣어도 config 값은 **stale 3/3/3 으로 남는다**(다음 설정 저장 때까지).

- 이 세 값은 솔버의 **폴백 요구치**이자([cp_sat_basic.py:529-535](../app/services/cp_sat_basic.py#L529-L535)) 대체추천의 직접 입력값이다([replacement_recommend_service.py:847-849](../app/services/replacement_recommend_service.py#L847-L849)).
- 순서를 지키면 `shift_manage` 평일값이 그대로 config 에 굳는다.

---

## 2. Phase 2-A — 필요인원 (요구 #6, 최우선 블로커)

### 왜 최우선인가
`shift_manage` · `daily_shift` 가 모두 비어 요구치 합이 0이면 근무표 생성이 **ValueError 로 중단**된다
([roster_create_service.py:637-644](../app/services/roster_create_service.py#L637-L644)).

> 보조 사실: `GET /daily-shift` 를 한 번 호출하면 `shift_manage` 가 없을 때 기본 슬롯(**D3 / E3 / N2 / M0**)이 자동 시딩되고
> ([shift_manage_defaults.py:13-18](../app/services/shift_manage_defaults.py#L13-L18)) 해당 월 `daily_shift` 31행이 생성된다
> ([daily_shift_service.py:145-163](../app/services/daily_shift_service.py#L145-L163)).
> 즉 "화면 진입"만으로도 생성 자체는 뚫리지만, 값이 요구(D5/E4)와 다르므로 **아래 입력은 여전히 필요**하다.

### ★ 2026-08 함정 — 1일이 토요일
`PUT /daily-shift` 를 일자별 배열 모드로 저장하면서 `apply_globally=true` 를 주면 **`d_list[0]`(=8/1 토요일) 값이 `shift_manage` 템플릿에 동기화**된다
([daily_shift_service.py:511-521](../app/services/daily_shift_service.py#L511-L521)).
2026-08-01은 토요일이므로 주말값이 템플릿(=config `day_req` 원천)에 박힌다. 따라서 **2단계로 나눠 저장**한다.

- 2026-08: 31일 · 1일=토 · 주말 day = **1, 2, 8, 9, 15, 16, 22, 23, 29, 30** (10일)
- 2026-09: 30일 · 1일=화 · 주말 day = 5, 6, 12, 13, 19, 20, 26, 27 (8일)

### 2-A-1. 평일값을 월 전체 + 템플릿에 적용

`PUT /daily-shift` (쿠키 `access_token` 인증)

```json
{
  "office_id": "102243",
  "group_id": "1022438ea001",
  "year": 2026,
  "month": 8,
  "apply_summary_to_days": true,
  "apply_globally": true,
  "month_summary": {
    "D_count": 5,
    "E_count": 4,
    "N_count": 4,
    "M_count": 0,
    "D_count_max": 0, "E_count_max": 0, "N_count_max": 0, "M_count_max": 0,
    "max_enabled": false
  }
}
```

- `N_count=4` 는 **실측 확정값**. 2026-07·08 실제 근무표에서 평일·주말 모두 N=4 가 최빈이다
  (분석: [INCHEON41_ACTUAL_ROSTER_ANALYSIS_2026-07-27.md](INCHEON41_ACTUAL_ROSTER_ANALYSIS_2026-07-27.md) §2①).
  초판의 잠정값 2 는 시스템 기본값이었고 실제와 다르다.
- `D_count=5` 는 DE(파트장·선임 근무형)를 D 커버리지로 셀 때의 값이다. DE 를 별도 코드로 두고
  세지 않으면 평일 순수 D 는 4 다 — DE 처리 방침이 정해질 때까지는 5 로 두는 편이 안전하다.
- `M_count=0` 고정: `use_mid=False` 이므로 MID 미사용
- 이 호출이 `shift_manage` RN 슬롯(1=D, 2=E, 3=N, 5=M)을 5/4/2/0 으로 동기화한다([daily_shift_service.py:582-584](../app/services/daily_shift_service.py#L582-L584))

### 2-A-2. 주말 10일만 덮어쓰기

`PUT /daily-shift` — `apply_globally` **반드시 false**(템플릿 오염 방지)

```json
{
  "office_id": "102243",
  "group_id": "1022438ea001",
  "year": 2026,
  "month": 8,
  "apply_globally": false,
  "max_enabled": false,
  "date": {
    "D_count": [4,4,5,5,5,5,5,4,4,5,5,5,5,5,4,4,5,5,5,5,5,4,4,5,5,5,5,5,4,4,5],
    "E_count": [4,4,4,4,4,4,4,4,4,4,4,4,4,4,4,4,4,4,4,4,4,4,4,4,4,4,4,4,4,4,4],
    "N_count": [4,4,4,4,4,4,4,4,4,4,4,4,4,4,4,4,4,4,4,4,4,4,4,4,4,4,4,4,4,4,4],
    "M_count": [0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0]
  }
}
```

- 배열 길이 = 31(8월 일수)이어야 한다. index 0 = 8/1
- 위 값은 실측 기반이다: 주말 D=4 / E=4 / N=4, 평일 D=5(DE 포함) / E=4 / N=4.
  **E 와 N 은 평일·주말이 같으므로 실제로 달라지는 것은 D 뿐**이다
- E/N 이 전 일자 동일하므로, 2-A-1 에서 D 를 4 로 넣고 이 단계에서 평일 21일만 5 로 올리는
  방식도 가능하다. 단 그 경우 `shift_manage` 템플릿에는 4 가 박힌다(§1 참조)

### 화면 입력표 (API 대신 화면으로 할 경우)

| 입력 항목 | 값 | 비고 |
|---|---|---|
| 대상 연월 | 2026-08 | 8월은 이미 확정본 존재 → 실제 생성 대상은 9월 이후일 수 있음 |
| Day(D) 인원 | 평일 5 / 주말 4 | DE 포함 기준 |
| Evening(E) 인원 | 4 | 평일·주말 동일 |
| Night(N) 인원 | **4** | 평일·주말 동일 (실측 확정) |
| MID(M) 인원 | 0 | `use_mid=False` |
| 최대 인원 사용 | 끔 | `max_enabled=false` → `*_max` 전부 0 reset |
| 월 전체 일괄적용 | 켬 | 평일값을 먼저 전체 적용 |
| 주말 개별 수정 | 1·2·8·9·15·16·22·23·29·30일 | 일괄적용 **후** 수정 |

---

## 3. Phase 2-B — N전담 (요구 #5: 나이트 전담 월 15일)

두 단계 모두 필요하다. **① 허용근무형 → ② 월 한도** 순서.

### 3-1. 허용근무형을 N 전용으로 (발효일 명시 경로 권장)

`POST /nurse-period/change` — 대상자 1명당 1회

```json
{
  "attribute": "allowed_shifts",
  "nurse_id": "<N전담 대상자 nurse_id>",
  "valid_from": "2026-08-01",
  "value": ["N"],
  "group_id": "1022438ea001",
  "note": "N전담 지정"
}
```

- `nurse_allowed_shift_period` 에 close-before-open 으로 구간을 열고 `nurses` 캐시에 투영한다([nurse_period.py:186-192](../app/routers/nurse_period.py#L186-L192))
- 저장 시 그 달 월한도·고정근무와의 모순을 hard gate 로 검사한다(모순이면 422, [nurse_period.py:174-184](../app/routers/nurse_period.py#L174-L184))
- 화면 경로(근무자관리 사이드 프로필)는 `PATCH /nurses/{nurse_id}` 의 `allowed_shifts` 필드로도 동일 효과를 낸다. 다만 **발효일을 명시 제어**하려면 위 API 가 정확하다.

### 3-2. 월 나이트 15회 고정

`PUT /nurses/monthly-limits`

```json
{
  "year": 2026,
  "month": 8,
  "limits": [
    {
      "nurse_id": "<N전담 대상자 nurse_id>",
      "group_id": "1022438ea001",
      "year": 2026,
      "month": 8,
      "n_exact": 15
    }
  ]
}
```

- `n_exact` 와 `n_max` **동시 입력 금지**(400) — [nurse_monthly_limit_service.py:333-337](../app/services/nurse_monthly_limit_service.py#L333-L337)
- 검증 통과 확인: allowed=`{"N"}` 이면 `n_exact(15) > max_nig_per_month(15)` 일 때만 차단된다. 현재 `max_nig_per_month=15` 이므로 **15는 경계값으로 통과**([monthly_limit_validator.py:394-397](../app/services/precheck/monthly_limit_validator.py#L394-L397))
- ⚠️ Phase 1 에서 `max_nig_per_month` 를 15 미만으로 낮추면 이 저장이 즉시 422 가 된다. **15 유지**
- `POST /nurses/monthly-limits/night-bulk` 는 "야간 가능 근무자 **전체**"에 일괄 적용이므로 N전담 지정 용도로는 부적합. 개별 `PUT` 을 쓴다.

### 대상자 미확정
현재 N전담 지정자는 0명이다. 대상자 명단 확정 필요 — §7 미결 ②
(참고: 최수진A·최수진B 는 현재 `["D","E"]` = 나이트 제외자이므로 N전담 후보에서 제외)

---

## 4. Phase 2-C — 프리셉티 페어 (요구 #11)

`nurse_preceptee_period` 가 SSOT 이고 `assignment` 경유는 폐지됐다([assignment_service.py:1706-1708](../app/services/assignment_service.py#L1706-L1708)).

### 프리셉티 본인 기준 등록 (권장 — 1:1)

`PATCH /nurses/{프리셉티_nurse_id}`

```json
{
  "preceptee_period": {
    "operation": "create",
    "preceptor_id": "<프리셉터 nurse_id>",
    "start_date": "2026-08-01",
    "expected_end_date": "2026-10-31"
  }
}
```

- `start_date` · `expected_end_date` **둘 다 필수**. 무기한 등록은 폐지됐다([nurse_service.py:2413-2416](../app/services/nurse_service.py#L2413-L2416))
- 이미 다른 프리셉터와 관계가 있으면 422 → 먼저 `operation: "cancel"` 후 재등록
- 프리셉터 입장에서 여러 명을 한 번에 등록하려면 `preceptor_periods: [{operation, target_nurse_id, start_date, expected_end_date}]` 사용

### 페어 후보
grade3 6명(이가영A 417829 · 양예진 449801 · 한예원 449802 · 최지은 463939 · 조하늘 417832 · 강지민 449806, 경력 1~3년)이 프리셉티 후보로 보이나 **병원 확정 필요** — §7 미결 ③
프리셉티 등록 없이 `preceptee_on=True` 만 켜면 아무 동작도 하지 않는다(페어 0건).

---

## 5. Phase 1 — 설정 저장 (§1 순서에 따라 **마지막**)

`POST /roster/config/save?group_id=1022438ea001`
(HN 은 본인 그룹 자동, ADM 은 `group_id` 쿼리 필수 — [roster.py:156-201](../app/routers/roster.py#L156-L201))

**부분 수정 불가**: `RosterConfigCreate` 의 대부분 필드가 required 이므로 **현행값 전량 + 변경분**을 함께 보내야 한다([roster_schema.py:231-284](../app/schemas/roster_schema.py#L231-L284)).

```json
{
  "config_id": 589,
  "config_name": "기본 설정",

  "min_exp_per_shift": 3,
  "req_exp_nurses": 1,
  "two_offs_per_week": false,
  "max_nig_per_month": 15,
  "three_seq_nig": true,

  "not_one_night": true,
  "two_offs_after_two_nig": true,
  "two_offs_after_three_nig": true,
  "max_conseq_work": 5,
  "preceptee_on": true,

  "banned_day_after_eve": true,
  "off_days": 11,
  "max_conseq_off": 3,
  "shift_priority": 0.8,
  "sequential_offs": true,
  "nod_noe": false,
  "use_mid": false,
  "preceptee_shift_count": true,
  "weekly_off_group": false,
  "fixed_wanted_use_yn": false,
  "show_level": true,
  "show_preceptor": true,
  "off_first": false,
  "off_swap_enabled": false,

  "day_req": 0,
  "eve_req": 0,
  "nig_req": 0
}
```

### 변경 항목 (5개)

| 요구 | 필드 | 현행 → 변경 | 효과 |
|---|---|---|---|
| #1 | `not_one_night` | False → **true** | 1N 단독 배정 금지(하드) |
| #2 | `two_offs_after_two_nig` | False → **true** | 2연속 N 후 OFF 2회(하드) |
| #2 | `two_offs_after_three_nig` | False → **true** | 3연속 N 후 OFF 2회(하드) |
| #8 | `max_conseq_work` | 6 → **5** | 연속근무 5일 상한(하드) · CLAUDE.md 하드락 #1 과도 동시 정합 |
| #11 | `preceptee_on` | False → **true** | 프리셉티 팔로우(§4 페어 등록이 선행돼야 실효) |

### 유지 항목 (요구 이미 충족)

`banned_day_after_eve=true`(#9 E→D 금지) · `off_days=11`(#14a 주휴) · `three_seq_nig=true`(#1 N 3연속 허용) · `max_nig_per_month=15`(#5 상한)

### ⚠️ 저장 시 함께 바뀌는 값 (부작용 3건)

1. **`max_conseq_off`: NULL → 3** — 스키마 기본값이 3이라 저장하면 실값이 들어간다. 앱은 NULL 을 3 으로 해석하므로 **동작은 동일**하지만 DB 값은 바뀐다([roster.py:132-135](../app/routers/roster.py#L132-L135)).
2. **`day_req`/`eve_req`/`nig_req`: NULL → shift_manage 값** — 요청값과 무관하게 서버가 덮어쓴다. §1 순서를 지키면 5/4/2 가 들어간다.
3. **`use_mid` 그룹 전파** — 저장 경로는 `sync_use_mid_live=True` 라 `use_mid` 값이 그룹의 전체 config 로 전파되고 라이브 테이블에 즉시 동기화된다([roster_service.py:361-371](../app/services/roster_service.py#L361-L371)). `false` 유지이므로 현행과 동일.

### 하드락 정책 정합
변경 5건은 모두 CLAUDE.md 제약 하드락(#1 연속근무 5일 · #3 N 3연속 · #4 3N 후 OFF2 · #5 2N 후 OFF2 · #7 1N 금지) 방향과 일치한다. 소프트/objective 전환은 없다.

---

## 6. 적용 후 검증 SELECT (read-only)

```sql
-- ① 설정 5줄 반영 확인 (기대: True/True/True/5/True + day_req 5, eve_req 4)
SELECT config_id, version, not_one_night, two_offs_after_two_nig, two_offs_after_three_nig,
       max_conseq_work, preceptee_on, max_conseq_off, day_req, eve_req, nig_req,
       max_nig_per_month, banned_day_after_eve, off_days, use_mid
FROM eun_roster.dbo.roster_config WHERE group_id = '1022438ea001';

-- ② 필요인원: shift_manage 템플릿(평일값이어야 함)
SELECT shift_slot, main_code, manpower FROM eun_roster.dbo.shift_manage
WHERE group_id = '1022438ea001' AND nurse_class = 'RN' ORDER BY shift_slot;

-- ③ 필요인원: 일자별(주말 10일이 평일과 다른지)
SELECT day, d_count, e_count, n_count, m_count, max_enabled
FROM eun_roster.dbo.daily_shift
WHERE group_id = '1022438ea001' AND year = 2026 AND month = 8 ORDER BY day;

-- ④ N전담: 허용근무형 + 월 한도
SELECT n.nurse_id, n.name, n.is_night_nurse AS allowed_shifts_cache
FROM eun_roster.dbo.nurses n WHERE n.group_id = '1022438ea001' AND n.active = 1
  AND n.is_night_nurse <> '[]';
SELECT nurse_id, year, month, n_exact, n_max, n_min FROM eun_roster.dbo.nurse_monthly_limits
WHERE group_id = '1022438ea001' AND year = 2026 AND month = 8;

-- ⑤ 프리셉티 페어
SELECT p.nurse_id, n1.name AS preceptee, p.preceptor_id, n2.name AS preceptor,
       p.valid_from, p.valid_to
FROM eun_roster.dbo.nurse_preceptee_period p
JOIN eun_roster.dbo.nurses n1 ON n1.nurse_id = p.nurse_id
LEFT JOIN eun_roster.dbo.nurses n2 ON n2.nurse_id = p.preceptor_id
WHERE n1.group_id = '1022438ea001';
```

### 최종 검증은 실제 생성으로
값이 저장돼도 **생성에서 무시되는 설정이 실재**한다(예: `teams.min_shift` 는 생성 시 하드코딩 `{D:1,E:1,N:0}` 으로 대체 — [roster_create_service.py:2801-2827](../app/services/roster_create_service.py#L2801-L2827)).
따라서 precheck → 실제 생성 → 위반 카운터까지 확인해야 적용 완료로 본다.

---

## 7. 미결값 — 실데이터 분석으로 대부분 해소 (2026-07-27 갱신)

실제 운영 근무표(2026-07·08) 분석으로 ①④⑤ 는 해소, ②③ 은 조건이 구체화됐다.
근거: [INCHEON41_ACTUAL_ROSTER_ANALYSIS_2026-07-27.md](INCHEON41_ACTUAL_ROSTER_ANALYSIS_2026-07-27.md)

| # | 항목 | 상태 |
|---|---|---|
| ① | 주말 D·E 인원, N 필요인원 | ✅ **해소** — 평일 D5(DE포함)/E4/N4, 주말 D4/E4/N4 (7·8월 교차검증) |
| ② | N전담 대상자 명단 | ⚠️ **조건 변경** — 대상자는 있으나 **매월 로테이션**(7월 최혜윤·장선희 → 8월 이주영·임지은). 월별로 누구인지 확정 필요하고, `allowed_shifts` period 로 지정하면 매월 2회 갱신해야 함 |
| ③ | 프리셉티-프리셉터 페어 + 기간 | ⚠️ **절반 해소** — 프리셉티 = 2026년 입사 4명(양예진·한예원·최지은·강지민, 전원 grade3). 프리셉터는 "윤지선(선임)이 신규 배치 시 동반"이라 **1:1 페어 매핑과 기간**만 확인 필요 |
| ④ | 대상 연월 | ✅ **해소** — 7·8월 확정본 존재 → 생성 대상은 **2026-09 이후**. (8월 재현 테스트는 검증용으로 유효) |
| ⑤ | 팀 미배정 2명(종미영·윤지선) | ✅ **정상** — 둘 다 DE 상시 근무자(파트장·선임). 조치 불필요 |

### 신규 미결 (분석에서 새로 드러난 것)

| 항목 | 내용 |
|---|---|
| DE 근무코드 | 실재하는 단일 근무형인데 그룹 `shifts` 12종에 **미등록**. 등록 여부와 D 커버리지 포함 여부 결정 필요 |
| 요구 #14 해석 | "각조에서 1,2번째 간호사가 1명은 포함" 이 조별인지 전체인지. 실측상 hard 부적합(N 8일 위반) → **soft 권고** |
| N전담 월 지정 방식 | 월 로테이션을 시스템에서 어떻게 표현할지 (월별 수동 vs 신규 스코프) |

---

## 8. 메모리 대비 정정 (2026-07-27 실측)

이전 조사 메모(`project_roster_incheon41_14constraints_setting_scope_20260727`) 대비 아래 3건이 다르다.

1. **등급**: "grade1=8 · grade2=11 · NULL 0" → 실측은 **grade1=8 · grade2=11 · grade3=6**(총 25). grade3 6명이 누락돼 있었다.
2. **팀 배정**: "25명 중 10명만(A3·B3·C4), D팀 0명" → 실측은 **23명 등록, 4팀 전부 채워짐**(6/5/6/6), 미배정 2명. 세팅이 진행된 것으로 보인다.
3. **허용근무형 컬럼**: `nurses` 테이블에 `allowed_shifts` 물리 컬럼은 없다. ORM 이 `is_night_nurse` 컬럼을 `allowed_shifts` 키로 매핑한다([models.py:85-86](../app/db/models.py#L85-L86)). 직접 SELECT 시 컬럼명 주의.

`shift_manage`·`daily_shift`·`nurse_monthly_limits`·`nurse_preceptee_period` 0행과 config 589 값들은 메모리와 일치한다.
