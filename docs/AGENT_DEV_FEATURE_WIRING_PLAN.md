# dev 신규 기능 → 에이전트 배선 설계

> 작성 2026-08-26. 대상: `origin/dev` 27커밋(merge-base `5bd13ed` 이후) 중 에이전트 노출이
> 필요한 기능. **설계 단계 — 구현 전.** 머지가 선행돼야 착수 가능.
>
> 사전 확인 완료: merge-tree 충돌 0 · 임시 워크트리 실제 병합 성공 · 병합본 에이전트
> 테스트 1190 passed. 양쪽이 건드린 파일은 `requirements.txt`(다른 줄) ·
> `wanted_service.py`(다른 위치) 둘뿐.

---

## 1. 배선 대상 판정

27커밋에서 나온 신규 엔드포인트 4개 · 신규 서비스 함수 9개를 전수 분류했다.

| dev 기능 | 성격 | 판정 |
|---|---|---|
| 기피/확정 **전체 반영·미반영** (`set_adjustment_applied_service`) | API | **배선 O** — §2.1 |
| **수술실 콜 당번** (`assign_oncall`·`load_call_code_map`·`postprocess_oncall`) | 엔진 후처리 | **배선 O(조회만)** — §2.2 |
| **고정근무자 공휴일 휴무** (`fixed_holiday_off_yn`) | config 토글 | **배선 O(조회, 쓰기는 §3 결론 대기)** — §2.3 |
| 수면OFF 주기 조회/재계산 EP | API | 조회는 **구현 완료**(`sleep_off_status`). rebuild 는 **배선 X**(수치 조작 불가 방침) |
| 야간 상한 병목 진단 (`personal_night_cap_options_from_issues`) | 해결 카드 | **배선 불필요** — `resolve_infeasibility._APPLY_KEYS` 5개가 dev 가 내는 키 5개와 정확히 일치(확인함) |
| 월경계 NOD/NOE/EOD 패턴 (`build_cross_month_pattern_vars`) | 엔진 제약 | **배선 불필요** — 별도 설정 없이 항상 적용 |
| 고정근무자 휴직 가림막 (`postprocess_leave_mask`) | 엔진 후처리 | **배선 불필요** — 사용자 조작 지점 없음 |
| 미제출자 기피 차단 · 수면OFF 1N 가드 · 전원 고정근무 생성 차단 해소 | 엔진 수정 | **배선 불필요** — API 없음 |
| `GET /jobs/status/latest` | API | **배선 불필요** — 에이전트는 `RosterJob` 을 직접 읽어 이미 커버 |

---

## 2. 개별 설계

### 2.1 기피/확정 전체 반영·미반영 → `manage_banned_wanted` 확장

`manage_banned_wanted` 에 operation 두 개를 추가한다. 새 스킬을 만들지 않는 이유는
대상(조정판의 기피/확정)이 같고, LLM 이 "금지 원티드" 라는 한 도메인으로 인지해야
어휘가 갈리지 않기 때문이다.

```
operation: list | add | remove | clear | apply_all | unapply_all   ← 2개 추가
```

- `apply_all`   → `set_adjustment_applied_service(db, group_id, year, month, applied=True)`
- `unapply_all` → 같은 함수, `applied=False`

**설계상 반드시 지켜야 할 것 세 가지** (전부 dev 서비스 주석에 근거가 있다):

1. **범위를 사용자에게 명시한다.** 이 연산은 `FixedWantedEntry`(확정) 와
   `BannedWantedEntry`(기피) **두 채널을 동시에** 뒤집는다. "기피만 끄겠지" 로 읽히면
   확정 원티드까지 꺼진다. 미리보기 문구에 두 건수를 따로 적는다.
   → `"확정 원티드 N건 · 기피 M건을 모두 미반영 처리합니다"`

2. **`source='nurse'` 도 포함됨을 밝힌다.** `clear`(=reset) 는 HN 출처만 지우지만
   이 연산은 간호사 본인이 낸 기피까지 플래그를 뒤집는다. 삭제가 아니라 적용 플래그라
   비파괴적이지만, 사용자에겐 "간호사가 낸 기피도 함께 꺼집니다" 라고 말해야 한다.
   (안 밝히면 다음 조정판 저장에서 `is_applied=True` 로 되살아나 조용히 되돌려진다 —
   dev 가 이 EP 를 만든 이유가 정확히 그 구멍이다.)

3. **켜는 방향의 `warnings` 를 버리지 않는다.** 서비스가 `applied=True` 일 때
   강제휴무 위반을 `resp.warnings` 로 돌려준다. **차단이 아니라 경고**다(끄기를 막으면
   복구 수단이 사라지므로 의도적으로 비차단). 에이전트는 이걸 반드시 답변에 실어야
   한다 — 안 실으면 나중에 생성이 INFEASIBLE 로 죽은 뒤에야 원인을 되짚게 된다.

**권한**: 라우터가 `caller_is_head_nurse or is_master_admin` 을 강제. 스킬은 이미
`hn_only=True` 라 이중 게이트가 된다(문제 없음).

**승인**: `preview_only=True` 기본 유지. 미리보기에서 대상 건수(확정/기피 각각)를 세어
보여주고, 실행은 승인 후. `set_adjustment_applied_service` 는 내부에서 `db.commit()` 을
호출하므로 `atomic_commit` 블록 안에서는 flush 로 리다이렉트된다(기존 규약 그대로).

**read-back**: `apply_all` 후 두 테이블의 `is_applied` 가 의도한 값인지 되읽어 대조.
기존 `_verify_manage_banned_wanted` 에 분기 추가.

---

### 2.2 수술실 콜 당번 → `query_schedule` 신규 scope `oncall_status` (조회 전용)

**쓰기는 열지 않는다.** 콜 배정은 근무표 생성 후처리(`postprocess_oncall`)가 만들어내는
결과이고, 사용자가 조정하는 지점은 `shifts.call_base_id` 등록(근무유형 관리 화면)이다.
에이전트가 배정 결과를 직접 고치면 다음 생성에서 덮인다.

응답 설계:

```
{
  "적용": bool,                  # 코드맵이 비어 있으면 False
  "코드맵": {"D1": "D1콜", "O": "오프콜"},
  "note": "콜 부여 사용 여부는 근무유형의 call_base_id 등록으로 정해집니다."
}
```

**★ 폴백 금지가 핵심이다.** `load_call_code_map` 은 **의도적으로 기본값을 두지 않는다** —
맵이 비면 그 병동은 콜 미사용이다. 실측(2026-08-18, office 102243): 15개 그룹 중 콜 코드를
가진 곳은 수술실뿐이고 나머지 14개는 `O` 만 있어, 폴백을 두면 **존재하지 않는 코드로 OFF 가
덮인다.** 에이전트도 같은 규약을 따라 "맵 비어 있음 = 미사용" 으로만 답하고 추정하지 않는다.

---

### 2.3 고정근무자 공휴일 휴무 (`fixed_holiday_off_yn`)

- **조회**: `query_schedule scope=constraint_config` 응답에 포함(현재 노출 안 됨).
- **쓰기**: **보류**(§3 결정 B). `update_constraint` 는 ORM 화이트리스트라 raw SQL 필드를
  끼우면 검증 경계가 갈린다. 설정 변경은 프론트 설정 화면에 맡기고, 에이전트는 조회만.

의미: 켜면 고정근무자가 공휴일에 쉬고, 끄면(기본) 종전대로 평일에 `fixed_shift` 가 채워진다.
`update_constraint` 의 기존 bool 토글들과 성격이 같아 화이트리스트 추가만으로 충분하다.

---

## 3. ★ ORM 컬럼 문제 — 결정 재고 요청

`fixed_holiday_off_yn`(roster_config) 과 `call_base_id`(shifts) 는 마이그레이션
(`2026_08_19_add_oncall_and_holiday_columns.sql`)에 있으나 **`models.py` 에는 없다.**
dev 는 raw SQL + `INFORMATION_SCHEMA` 존재검사로 읽는다.

"models.py 에 컬럼 추가" 로 결정했으나, **dev 가 ORM 을 피한 데는 실측 근거가 있어
그대로 진행하면 운영 장애 위험이 있다.** dev 주석 원문:

> `roster_config` 는 dev·prod 스키마가 이미 갈려 있다(2026-08-18 실측: 모델에만 있는
> 컬럼 10개 · dev 에만 있는 컬럼 11개). 모델에 얹으면 컬럼이 없는 환경에서 **설정 조회가
> 통째로 깨진다.**

> 모델에 올리면 SQLAlchemy 가 모든 `shifts` SELECT 에 그 컬럼을 끼워 넣어, 컬럼이 아직
> 없는 환경에서는 **근무표 조회가 통째로 깨진다.** (2026-08-18 실측: dev 에만 있고 prod 엔 없었다)

즉 위험은 "이 기능이 안 된다" 가 아니라 **근무표/설정 조회 전체가 죽는다** 이다.

### 선택지

| 안 | 내용 | 위험 | 비고 |
|---|---|---|---|
| **A** | `models.py` 에 컬럼 추가 | **높음** — DDL 미적용 환경에서 전체 조회 장애 | prod 에 DDL 적용 확인이 **선행 필수** |
| **B** | dev 방식대로 raw SQL, 단 **읽기 헬퍼를 한 곳으로** 모아 에이전트도 재사용 | 낮음 | 일관성은 떨어지나 dev 와 규약 일치. 중복 구현 방지 |
| **C** | `deferred()` 컬럼으로 매핑 | 중간 | 기본 SELECT 에서는 빠지므로 조회 장애는 피하나, **접근 시점**엔 컬럼이 없으면 실패 |

**결정: B (raw SQL) — 확정 2026-08-26.** 이유는 (1) prod DDL 적용 여부가 미확인이고, (2) A 의 실패 양상이
"기능 미동작" 이 아니라 "조회 전면 장애" 라 되돌리기 비용이 크며, (3) B 는 dev 코드를
건드리지 않아 머지 충돌을 만들지 않는다.

구현 규약(B):
- `app/agents_v2/tools/` 에 **읽기 헬퍼 하나**를 두고 에이전트는 그것만 쓴다. dev 의
  `_holiday_off_enabled` / `load_call_code_map` 을 **재구현하지 말고 그대로 호출**한다
  (규칙이 두 벌이 되면 존재검사·폴백 규약이 갈린다).
- 컬럼 부재는 오류가 아니라 **미설정**으로 떨어뜨린다(dev 와 동일: 공휴일=False, 콜맵={}).
- 쓰기(`fixed_holiday_off_yn` 설정 변경)는 이 결정으로 **보류**한다 — `update_constraint`
  는 ORM 화이트리스트 기반이라 raw SQL 필드를 끼우면 검증·경계가 갈린다. 조회만 연다.

(A 안은 폐기하지 않되 보류: `2026_08_19_add_oncall_and_holiday_columns.sql` 이 prod 포함
전 환경에 적용된 것이 확인되면 그때 재검토한다. 그 전엔 조회 전면 장애 위험이 크다.)

---

## 4. 구현 순서

1. **선행** — 미커밋 작업분 커밋 → `origin/dev` 머지 (충돌 0 확인됨)
2. §2.1 기피/확정 전체 반영 — 가장 값어치 높음(HN 조정판의 실제 운영 동작)
3. §2.2 콜 당번 조회 — 읽기 전용이라 위험 낮음
4. §2.3 공휴일 휴무 **조회**(raw SQL 헬퍼 경유)
5. 각 단계마다 회귀 + 신규 테스트

## 5. 미결

- `set_adjustment_applied_service` 가 `db.commit()` 을 직접 호출 → 서비스 커밋 관행의
  일부. 별건인 "트랜잭션 경계를 호출자로" 이행과 함께 정리 대상.
- dev 의 `/night-cycle` GET 은 미개설 월을 전월 기준으로 채우는 로직을 포함한다.
  현재 `sleep_off_status` 는 해당 월 행만 읽으므로, 머지 후 그 로직을 재사용할지 재검토.
