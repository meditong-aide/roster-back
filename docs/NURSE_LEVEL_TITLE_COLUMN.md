# 직책(`level_`) 데이터 흐름 — `nurses.level_` 컬럼 SSOT

## 요약(결론)

수간호사 **근무자관리 사이드프로필**의 직책(`level_`) 입력/조회는 **이미 `nurses.level_`
컬럼을 단일 소스로 사용**한다. groupware(member 테이블)에서 당겨오지 않는다.

- **저장**: 프론트가 사이드프로필에서 `level_`를 보내면 → 컬럼에 그대로 저장된다.
- **조회**: 컬럼값을 그대로 반환한다.
- 따라서 "member에서 안 가져오고 컬럼만 쓰기"는 **백엔드 추가 수정 없이 이미 가능**하다.

member(groupware) `OfficialTitleName`은 **nurse 레코드가 없는 관리자 계정**의 인사 기본정보
표시에만 fallback으로 쓰인다(실제 간호사는 해당 없음).

---

## 컬럼 / 스키마

| 항목 | 위치 | 타입/비고 |
|---|---|---|
| DB 컬럼 | `nurses.level_` (`app/db/models.py:80`) | `VARCHAR(20)` — 직책 문자열 |
| 표시 토글 | `nurses.show_level` (`models.py:551`) | 근무표에 직책 표시 여부 |
| GET 응답 필드 | `NurseProfile.level_` (`app/schemas/roster_schema.py:476`) | `Optional[str]` |
| POST 요청 필드 | `NurseProfileUpdate.level_` (`roster_schema.py:656`) | `Optional[str]` |

> 주의: `level_`(직책, 문자열)와 `grade`(등급/직급, 정수, 근무표 계산용)는 **별개**다.
> `grade`는 `_PERIOD_OWNED_FIELDS`(시점 SSOT)라 컬럼 직접쓰기가 금지되지만,
> `level_`는 시점 대상이 아니라 컬럼에 바로 저장된다.

---

## GET — 사이드프로필 조회

```
GET /nurses/{nurse_id}?group_id=...
```

- 핸들러: `get_nurse_by_id` (`app/routers/nurses.py:1307`, `response_model=NurseProfile`)
- 서비스: `get_nurses_in_group_service` (`app/services/nurse_service.py:296`)
- 반환: `"level_": nurse.level_` (`nurse_service.py:420`, 관리자 경로 `:620`)
- **groupware 오버레이 없음** — 컬럼값을 그대로 응답.

## POST/PATCH — 사이드프로필 저장

```
PATCH /nurses/{nurse_id}
Body(NurseProfileUpdate): { "level_": "책임간호사", ... }
```

- 핸들러: `update_nurse_profile` (`nurses.py:1360`)
- 서비스: `update_nurse_profile_service` (`nurse_service.py:1771`)
  - `fields = update_data.dict(exclude_unset=True)` → 보낸 필드만 반영
  - 적용: `_apply_source_nurse_update` (`nurse_service.py:2008`)
    - `key in _PERIOD_OWNED_FIELDS`(= `grade`/`allowed_shifts`/`fixed_shift`/`is_weekend_off`)면 skip
    - `level_`은 해당 없음 → `setattr(nurse, "level_", value)`로 **컬럼 저장**

즉 프론트가 `level_`를 payload에 담아 PATCH 하면 그대로 저장된다. 별도 저장 API 불필요.

### bulk 저장도 동일

```
POST /nurses/bulk-update
Body: List[NurseProfile]  # 각 항목에 level_ 포함 가능
```

- 핸들러: `bulk_update_nurses` (`nurses.py:442`)
- 서비스: `bulk_update_nurses_service` (`nurse_service.py:828`)
  - `update_data = profile.dict(exclude_unset=True)` (`:919`) — 보낸 필드만
  - source 모드 적용 루프(`:984~988`):
    ```python
    for key, value in update_data.items():
        if key in ("allowed_shifts", "grade", "fixed_shift", "is_weekend_off"):
            continue  # period 일원화 — 제외
        if hasattr(db_nurse, key):
            setattr(db_nurse, key, value)   # level_ 는 제외 아님 → 컬럼 저장
    ```

**bulk · non-bulk 모두 동일한 제외 집합**(period-owned 4개)을 쓰고 `level_`은 거기 없으므로,
두 경로 다 `level_`를 컬럼에 저장한다.

> 예외(inbound/target): 파견·병동이동으로 들어온 간호사를 **target 뷰**에서 수정하면
> `_apply_target_update`(`nurse_service.py:946`, `nurse_assignment.target_*`)로 가며 `level_`
> 오버레이가 없다. 즉 **source(본인 그룹) 간호사**는 bulk·non-bulk 모두 컬럼 저장되고,
> inbound 간호사의 직책은 이 경로로는 저장되지 않는다(직책=home 속성이라 대개 의도된 동작).

---

## groupware(member) 접점 — fallback 전용

| 위치 | 용도 | 실간호사 영향 |
|---|---|---|
| `nurses.py:885` (`GET /nurses/personnel-basic-info`) | **nurse 레코드 없는 관리자** 계정의 인사 기본정보 표시 시 `OfficialTitleName` 사용 | 없음 — 실간호사는 같은 엔드포인트에서 `nurse.level_`(`:949`) 사용 |
| `member/edit.py:60` | groupware MEMBER의 `OfficialTitleName` **직접 수정**(별도 인사 화면) | 사이드프로필과 무관 |
| `nurse_sync_service.py:249` | 미등록 멤버 **신규 생성 시** `level_=title_level` (ETC 비간호직군만 값, 일반 간호사는 `None`) | 생성 1회. 기존 간호사 값 덮어쓰지 않음(기존 계정 skip) |

핵심: 위 어느 경로도 **HN 사이드프로필의 실간호사 `level_`를 groupware로 덮지 않는다.**

---

## 왜 "member에서 가져오는 것처럼" 보였나

`nurse_sync_service`가 일반 간호사를 만들 때 `level_ = None`으로 넣기 때문에 컬럼이 **비어 있는**
경우가 많다. 컬럼이 비면 화면상 직책이 공란이라, groupware 값에서 오는 것처럼 오인될 수 있다.
HN이 사이드프로필에서 직책을 한 번 입력해 저장하면 이후로는 컬럼값이 표시된다.

---

## 프론트 액션 아이템

백엔드는 준비돼 있으므로 프론트에서:

1. 사이드프로필 직책 입력칸을 **편집 가능** 필드로 두고, 저장 시
   `PATCH /nurses/{nurse_id}` body에 `level_`를 포함해 보낸다.
   (기존에 `member/edit`(groupware)로 보내던 경우 → 이 경로로 전환)
2. 표시값은 `GET /nurses/{nurse_id}`의 `NurseProfile.level_`(컬럼값)를 그대로 쓴다.
   groupware `OfficialTitleName` fallback을 쓰지 않는다.

---

## (옵션) groupware 접점 완전 제거를 원할 경우

현재 실간호사 흐름엔 필요 없지만, 정책상 groupware 접점을 더 줄이려면:

- `GET /nurses/personnel-basic-info`의 **관리자(no-nurse-record) 분기**에서
  `OfficialTitleName` 대신 공란/별도 처리 (단, 이 계정은 nurse 레코드가 없어 컬럼 소스가 없음).
- 필요 시 별도 요청.

## 검증 기준

- `PATCH /nurses/{id}`에 `{"level_":"X"}` → 재조회 `GET /nurses/{id}`에서 `level_=="X"`.
- 근무표/명단 표시(`show_level=true`)에서 컬럼값 노출.
