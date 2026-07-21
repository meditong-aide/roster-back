# fallback_lex MUS 래핑 전수 감사 (2026-07-21)

**목적**: 프로덕션 생성은 `SKIP_PRIMARY=1`로 primary 스킵→fallback_lex만 돈다. layer-④ MUS 진단도 fallback 레지스트리(`_assume_registry_fb`)에서 core를 뽑는다. 따라서 **완화가능 정책 하드 제약이 fallback에서 래핑돼 있어야** 원인(core)에 이름이 뜬다. `_optimize_fallback_lex_hard_first` 전수 감사.

## 래핑 완료 (core에 뜸)

add_hard_fb(18): AllowedShiftMaskBan D/E/M/N · ConsecutiveNightCap(+Edge) · FixedCell(+Ban) · InitialForbidden · MaxConsecutiveWorkWindow · MaxNight · OffCap · OffWindowRequirement · SpecialShiftBanW · TransitionBan N2D/E2D/N2E · CarryoverRecovery3N2OFFGuard.
create_literal: CarryoverRecovery 2N/3N(+Boundary/Partial/Tail) · **Recovery2N2OFF/3N2OFF**(커밋 e7215ce) · **NotOneNight · WeekendOffOnly**(커밋 518a275).

## 미래핑 갭 (감사 발견) — 우선순위순

| # | 제약 | line | 완화노브 | 상태 |
|---|---|---|---|---|
| 1 | 주말휴무 강제OFF/평일OFF금지 | 1013·1025 | weekend_off_only_enable | ✅ **닫음(518a275)** |
| 2 | not_one_night 1N금지 | 1643 | not_one_night | ✅ **닫음(518a275)** |
| 3 | 회복 2N/3N within-month | 2228·2126 | two_offs_after_two/three_nig | ✅ **닫음(e7215ce)** |
| 4 | 월한도/grade/team (위임모듈) | 2713·2694·2675 | n_max·allow_soft_fallback | ⬜ 후속(파일밖, registry threading) |
| 5 | 프리셉티 follow/sync | 1224·1231·1244 | preceptee_follow | ⬜ 후속 |
| 6 | 4연속OFF 금지(4O) | 919·925·968 | enforce_4o_hard(기본OFF) | ⬜ 후속(저가치) |
| 7 | ban_night_before_fixed_off | 1666 | ban_night_before_fixed_off | ⬜ 후속(니치, primary도 미래핑) |
| 8 | 회복 fixed-wanted 서브분기 | 2255 | (회복 일부) | ⬜ 후속 |
| — | M 전이규칙 | 1605 | (노브 없음) | ❌ 제외(완화 불가→core 떠도 제안 불가) |

## 정상(갭 아님)
- **coverage-min = soft**(fallback의 존재이유). coverage-max hard cap(1344/1379/1387 등)은 `_relax_coverage` 자체 완화경로 보유(별도).
- pattern_nod/noe/eod · off_quota short/excess · isolated-off · min-off lower = 전부 soft(safety/slack).

## 남은 작업 (우선순위)
1. **#4 위임모듈 registry threading** (HIGH): `add_monthly_limit_constraints`/grade/team 함수에 `_assume_registry_fb` 전달 — 파일밖 작업.
2. #5 프리셉티, #7 ban_night, #8 회복-fw, #6 4O — 파일내, primary 패턴 복사로 처리 가능.

## 참고: 진단 자체 성능
fallback MUS는 reify(하드마다 리터럴, 수천 개)로 무겁고 진단 TL=30초(MUS_DIAG_TIME)라, 큰 병동서 UNKNOWN(증명 미완) 가능. 래핑은 "회복/1N/주말이 병목이면 이름이 뜨게" 하는 필요조건이고, 진단이 시간 내 UNSAT 증명해야 실제로 뜬다(별도 성능 갭).
