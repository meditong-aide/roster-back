# off_days(월 휴무) 소프트 제약 ↔ capacity precheck 회귀 분석·수정

작성: 2026-07-22 · 대상: 남촌의료재단 시화병원 중환자실1 (group `10135857f9f9`) 8월 사례

## 1. 증상

연속근무 상한(`max_conseq_work`)을 1→5까지 올려도, `off_days=10`이면 근무표 생성이
갑자기 **infeasible**(CAPACITY_TOTAL_SHORTAGE)로 막혔다. 직전 커밋 `bac2c11`에서는 같은
설정이 정상 생성됐다. "off=9면 통과, off=10이면 차단"이라는 이상한 경계선이 관측됨.

## 2. 근본 원인 — 두 개의 잘못이 겹쳐 있었음

### (원래부터 잘못) `_working_capacity`가 소프트 off를 하드 감산
`team_grade_precheck._working_capacity`는 1인당 근무가능일 상한을
`span − _required_off_days`로 계산하며 `off_days`를 뺐다. 그러나 **off_days는 엔진에서
소프트 제약**이다:

- `fallback_lex.py:2385` — 개인 OFF 목표는 `off_quota_short` **슬랙**으로 강제(목표 미달 시
  벌점만, 하드 아님). capacity가 빡빡하면 엔진은 OFF를 목표 밑으로 깎아 coverage를 채운다.
- 대조적으로 **연속근무 상한은 하드**: `fallback_lex.py:1730` — `K+1` 창마다 `≥1 OFF`를
  `m.Add`로 강제(우회 불가).

→ 하드 capacity 상한을 계산할 때 소프트 off를 빼면 **false CAPACITY_TOTAL_SHORTAGE**가 난다.

### (가림막) 키 이름 불일치로 off가 0으로 새고 있었음
`bac2c11` 시점 `runtime_bridge`는 off를 `standard_personal_off_days` 키로 읽었는데
(`runtime_bridge.py:329`), 실제 config dict은 DB 컬럼명 `off_days`만 담는다
(`cp_sat_basic.py:568` = `config_data.get('off_days')`). → `.get("standard_personal_off_days")`
= 없음 → **기본값 0**. off가 0으로 새서 위 잘못이 **발현되지 않고** 있었다.

### 회귀 트리거 — 커밋 `8d2c2a9`
"precheck config dict 키를 DB canonical로 통일"이 `_required_off_days`를 `off_days`로 읽게
고쳤다(가림막 제거) → 가려져 있던 하드 감산이 **활성화** → 회귀 발생.

```diff
- + int(cfg.get("standard_personal_off_days", 0) or 0)   # DB dict엔 이 키 없음 → 0
+ + int(cfg.get("off_days", cfg.get("standard_personal_off_days", 0)) or 0)  # 이제 10
```

## 3. `off_first`와의 연관성 (이 병동은 `off_first=True`)

`off_first`는 capacity 여유분을 근무/OFF 중 어디로 흘릴지 결정한다.

- **`off_first=False`(기본)**: Work oversupply / OFF tight. OFF를 `min_off_required`로 tight
  clamp하고 잔여 셀을 근무로. (`cp_sat_basic.py:4295`)
- **`off_first=True`**: OFF oversupply. **off_days를 아예 무시**하고 OFF는 잔여 셀로 자연 결정,
  `min_off` 하드도 해제. (`cp_sat_basic.py:4190`, `:4282`)

이 병동 config(1616/1625 등)는 **`off_first=True`** → 엔진이 off_days를 **완전 무시**한다.
그런데 버그 precheck는 off_first를 보지 않고 off_days를 무조건 감산 → **엔진엔 존재하지도 않는
유령 임계값**(off=9 통과 / off=10 차단)을 만들어낸 것.

## 4. 수정

소프트 off는 하드 capacity에서 빼지 않는다. 하드하게 근무가능일을 줄이는 것만 감산:
전사 고정 휴무(`global_monthly_off_days`) + 개인 하드 조정(`personal_off_adjustment`).

- `team_grade_precheck._hard_off_floor()` 신설 → `_working_capacity`·`_capacity_with_mcw`가 사용.
- `ontology_pool._required_off_days` 동일 트윈 수정(pool shortage에도 같은 false positive).

연속근무(하드)만 capacity 천장으로 남으므로:

| 설정 (35명, 8월 수요 744) | 버그(off 감산) | 수정 후(off 제외) |
|---|---|---|
| conseq=5, off=10 | `min(31−10,26)=21` → 735 → **infeasible** | `min(31,26)=26` → 910 → **OK** |
| conseq=5, off=9 | `min(31−9,26)=22` → 770 → OK | 910 → OK (off 무관) |
| conseq=2, off=10 | 735 → infeasible | `min(31,21)=21` → 735 → infeasible (연속근무 하드 한계, 진짜) |

off_days는 이제 feasibility 판정에 관여하지 않는다(엔진 실제 동작과 일치). off를 못 채우면
**하드 차단이 아니라 소프트 벌점**(실제 OFF ≈ 목표보다 약간 미달).

## 5. 해결 옵션(자동 완화 버튼)

capacity 부족이 **연속근무 상한(하드)** 때문이고 그것만 올리면 풀릴 때, "연속근무 N으로 올리기"
auto_apply 옵션을 만든다(`conseq_cap_binding` evidence → `config_lever_options_from_issues`).

- 예: conseq=2 → `{max_conseq_work: 3}` (capacity 840 ≥ 744, 재계산으로 실검증).
- ~~off_days combo 레버~~는 제거됨: off는 소프트라 낮춰도 capacity가 안 바뀜 → **허상**이었다
  (이 버그가 만든 착시). 연속근무 단일 레버만 유지.

## 6. 프론트 payload 명확화 (프론트 레포에서 별도 커밋)

'이 방법으로 다시 생성' 버튼이 초기 `config`(전체 객체, 원본값)를 그대로 재전송하고 조정값은
별도 `config_override` 델타로만 붙여서, payload의 `config`가 원본값을 보여주는 혼동이 있었다.
→ `onSelectResolution`에서 `opt.apply` 델타를 **config 객체에 병합**해 payload가 조정값을
명시적으로 담게 수정(config_override도 병행 — config_id 경로·비-컬럼 solver 키 커버).

## 7. 검증

- 유닛: `tests/test_precheck_capacity_refinements.py` — off 소프트 회귀 가드 + 연속근무 단일 레버.
  광범위 스위트 396 passed, 회귀 0.
- DB E2E(8월, config 1625): 기본/conseq3/conseq5 전부 SUCCESS. worker 체인(config_id +
  config_override) SUCCESS. 프론트 병합 config(max_conseq 2→3) materialize → SUCCESS.

## 8. 교훈

- precheck의 하드 capacity 상한에는 **하드 제약만** 반영한다. 소프트 목표(off_days)는 절대 빼지
  않는다. "capacity를 조이는 방향이니 안전"은 소프트 제약엔 성립하지 않는다(false positive).
- 키 이름 불일치로 값이 0으로 새는 버그를 "고칠" 때는, 그 0이 **다른 잘못을 가리고 있지 않은지**
  확인한다. 여기선 키 정합화가 잠복 버그를 깨웠다.
- 엔진 정책 노브(`off_first`)에 따라 제약의 하드/소프트 성격이 달라진다. precheck는 엔진의 실제
  강제 방식과 일치해야 한다.
