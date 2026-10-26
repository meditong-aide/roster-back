# golden_base — EI-2 "키 소멸만 다름" 대조 기준본

> **D29(2026-10-20) — 이 폴더의 JSON 은 커밋하지 않는다(로컬 전용 · `.gitignore` 의 `tests/engine_input/golden*/**/*.json`).**
> 커밋되는 것은 이 README 하나다. 골든이 없는 체크아웃(새로 받은 저장소)에는 `dev4ec454e/` 가 폴더째 없고, 그때
> `test_expected_diff` 의 기준 대조 테스트는 실패하지 않고 D29 사유로 건너뛴다(`golden_gate.require_golden(GOLDEN_BASE)` —
> 기준 폴더 이름·개수의 정본도 `golden_gate.GOLDEN_BASE` 다). 일부만 있으면 건너뛰지 않고 실패하며, 이 브랜치 로컬 실행은
> `REQUIRE_GOLDEN=1` 을 붙여 건너뜀을 실패로 바꾼다. 아래 옛 기준 `ei1b/`·`dev3ede458/` 는 2026-10-20 저장소 밖으로 옮겼다.
> 문지기는 모두 테스트 실행 시점에 불린다(import 때 부르는 곳 0 — `test_golden_gate.py` 가 단언). 그래서 골든 수가 어긋나거나
> `REQUIRE_GOLDEN=1` 로 골든 없는 체크아웃을 돌려도 해당 테스트들만 실패하고 pytest 세션은 멈추지 않는다.

`test_expected_diff.py` 가 "지금 기록"과 대조하는 **동결된 과거 시점 골든** 모음이다. 시나리오별로
그 시점의 app 코드(EI-2 적용 전)로 한 번 돌린 기록을 그대로 굳혀 둔 것 — 원본 `tests/engine_input/
golden/`(현재 app 코드 기준)과는 다른 자리다.

## `ei1b/` — 2026-10-02(49f14bd 시절) 동결본 (더 이상 참조되지 않음) — 2026-10-20 저장소 밖으로 옮김(→ `.omc/aide_docs/v2/engine_input/golden_store/retired_from_repo_20261020/golden_base/ei1b/`)

EI-1b 단계에서 "49f14bd + 당시 하네스"로 동결했다. `expected_diff.py` 의 EI-2 2a~2d 단계를
실측(`UPDATE_GOLDEN=1`)으로 검증할 때 이 기준본을 썼다.

**2026-10-13 이후 `test_expected_diff.py` 는 이 폴더를 더는 읽지 않는다** — `dev55dacd4/` 로
바꿨다(아래). 지우지 않고 그대로 둔 이유: 이 폴더 자체가 "EI-2가 49f14bd 기준으로 무엇을
지웠는가"의 역사 기록이고, 과거 분석(§5.3 EI-2 ★2a~2d 실측 설명, `expected_diff.py` 주석)이
이 폴더를 근거로 적혀 있어 지우면 그 설명을 재현할 수 없게 된다.

## (옛) `dev55dacd4/` — 2026-10-13(origin/dev `55dacd4` 재수신) 동결본 — 2026-10-14 `dev3ede458/` 로 교체

`redev_55dacd4/pure_harness`(= `pure_dev`(dev `55dacd4`, EI 이름표·삭제 전혀 없음) + 사용자의
미커밋 M→M 패치(`user_mm_change.patch`) + 최신 하네스(`tests/engine_input`·`tests/engine_contract`))
에서 `UPDATE_GOLDEN=1` 로 15개 시나리오를 전부 돌려 만들었다. 두 번째 실행(플래그 없이)으로
결정론을 확인했다(차이 0).

`ei1b` 대신 쓴 이유: dev 가 49f14bd 이후 `roster_create_service.py`(사다리 새 단계
`ban_night_minimize`·`gen_deadline` 전체 마감 등)·`fallback_lex.py`(N 간격 슬랙·월 상한 슬랙 등)를
더해 "EI-2 가 아직 안 건드린 원형" 자체가 바뀌었다. `ei1b` 를 그대로 기준으로 쓰면 "EI-2 가 지운
것"과 "dev 가 더한 것"이 뒤섞여 `check_record` 의 "사라짐만" 규칙에 **새로 생긴 키**(dev 가
더한 것)까지 "허용 밖 변화"로 잘못 걸린다. `dev55dacd4` 는 "지금 app 의 dev 버전에서 EI-2를
적용하기 **직전**" 상태라 EI-2 가 지운 것만 정확히 드러난다.

★ 2026-10-13 추가 — 이 동결본을 만든 `pure_harness`(= `pure_dev` + 사용자 미커밋 M→M 패치)의
`app/services/cp_sat/fallback_lex.py` 는 **origin/dev `c9517da`("Feat : MID 연속 부여 허용",
2026-10-12 20:39)와 AST 상 같은 코드**다 — c9517da 의 변경(`mid_transition` 식에
`X(n,d-1,mid_idx)` 항 추가)은 사용자 패치가 이미 적용한 식과 동일하고, 다른 점은 주석 문구뿐
(c9517da 는 "연속 M 도 여기서 이미 막힌다"는 옛 주석을 고치지 않고 남겨 바뀐 코드와 모순된다 —
사용자 패치·`merged` 쪽 주석이 맞다). 그래서 이 `dev55dacd4` 동결본은 `55dacd4` 단독이 아니라
실질적으로 `c9517da` 기준과도 같다.

## (옛) `dev3ede458/` — 2026-10-14(origin/dev `3ede458` 재수신) 동결본 — 2026-10-20 `dev4ec454e/` 로 기준 교체(폴더는 남김) — 2026-10-20 저장소 밖으로 옮김(→ `.omc/aide_docs/v2/engine_input/golden_store/retired_from_repo_20261020/golden_base/dev3ede458/`)

`dev55dacd4/` 폴더를 이 이름으로 바꾸고 내용을 순수 dev `3ede458`(git archive) + 최신 하네스에서
`UPDATE_GOLDEN=1` 로 다시 뜬 16개(15 + S1-LB)로 교체했다(`.omc/aide_docs/v2/engine_input/redev_3ede458/pure_harness`).
옛 판과 다른 것은 **S1-L3 하나**다 — dev `3f63c2c`(off_swap: 주2OFF 병동은 연차 전환 대신 O 유지)가 후처리
입력(`L3.calls[4·5].args.generated`)의 `연`↔`OFF` 5칸을 바꿨다. 나머지 15개는 바이트까지 같았다.
옛 `dev55dacd4` 판은 `.omc/aide_docs/v2/engine_input/backup_20261014b/untracked.tgz` 에 있다.

## `dev4ec454e/` — 2026-10-20(origin/dev `4ec454e` 재수신) 동결본 (현재 기준)

순수 dev `4ec454e`(git archive 780파일) + 최신 하네스 28파일 + EI 지원 모듈 5개에서 `UPDATE_GOLDEN=1` 로 뜬 16개다
(`.omc/aide_docs/v2/engine_input/redev_4ec454e/golden_pure/golden`, 하네스 변화와 dev 변화를 가른 대조 사본 `ctrl_3ede458` 포함 —
`STATUS_redev_4ec454e.md` §1·§3). 같은 트리에서 독립 2회 실행해 **바이트 동일**했고 `SHA256SUMS.txt` 와 일치한다.
옛 `dev3ede458/` 와 다른 곳은 **51곳 = 새 키 `configured_constraints_json` 35곳 + digest 16곳**뿐이다(그 밖의 값·키·길이 변화 0).
새 키는 dev `863e0f2` 가 `_fetch_grade_config_dict` 에 더한 병원 원본 등급 최소다 — L0 `grade_config` 16 · L1 `grade_config` 14
(S9·S9-F 는 L1 없음) · S1-LT 시도 델타 5곳. dev `1bedd4d`(1단계 해 없음 표시)·`4ec454e`(라우터)는 골든에 닿지 않는다
(하네스가 그 분기를 지나지 않는다 — 골든 무변화는 그 변경들의 무사 증거가 아니다).
`golden_model/`(모델 지문) 9개는 순수 3ede458 ↔ 4ec454e 가 바이트 동일해 다시 쓰지 않았다.

