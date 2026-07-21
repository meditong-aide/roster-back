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
| 4a | 월한도(monthly_limit) | monthly_limit_constraints.py:93 | n_max/n_min | ✅ **이미 래핑**(m._cpsat_assumption_registry self-read, 감사 오판 정정) |
| 4b | grade min | grade_constraints.py:598 | allow_soft_fallback | ✅ **이미 래핑**(self-read) |
| 4c | team_min 커버 | team_constraints.py:154 | team_min_soft_fallback | ✅ **닫음(a1d46d1)** (self-read 추가) |
| 5 | 프리셉티 follow/sync + 고정핀 | 1246·1253·1266 | preceptee_follow | ✅ **닫음(6b98451)** |
| 6 | 회복 fixed-wanted 서브분기 | 2295 | (회복 일부) | ✅ **닫음(6b98451)** |
| 7 | ban_night_before_fixed_off | 1706 | ban_night_before_fixed_off | ✅ **닫음(6b98451)** |
| — | 4연속OFF(4O) → **max_conseq_off** | 919·968 | max_conseq_off | ❌ **제외**(이제 soft=infeasible 유발 안 함→core 대상 아님. 레거시 enforce_4o_hard 코드만 잔존) |
| — | M 전이규칙 | 1605 | (노브 없음) | ❌ 제외(완화 불가→core 떠도 제안 불가) |

## 정상(갭 아님)
- **coverage-min = soft**(fallback의 존재이유). coverage-max hard cap(1344/1379/1387 등)은 `_relax_coverage` 자체 완화경로 보유(별도).
- pattern_nod/noe/eod · off_quota short/excess · isolated-off · min-off lower = 전부 soft(safety/slack).

## 결론 (2026-07-21)
**fallback 완화가능 정책 하드 제약 전부 MUS 래핑 완료.** 위임모듈은 registry 를 파라미터로 넘길 필요 없이 각 모듈이 `m._cpsat_assumption_registry`(fallback_lex 가 stash)를 self-read — monthly_limit/grade 는 원래 그랬고, team 만 이번에 추가(a1d46d1).

- 파일내: 회복·주말·1N·프리셉티·ban_night·회복-fw (커밋 e7215ce/518a275/6b98451)
- 위임모듈: monthly_limit·grade(기존)·team(a1d46d1)
- 제외: 4O(→max_conseq_off soft) · M전이(노브없음) · coverage-min(soft)

**남은 건 진단 성능뿐**: reify 무거움 + 30초 TL → 큰 병동서 UNKNOWN 가능. 래핑은 필요조건이고, 진단이 시간 내 UNSAT 증명해야 실제로 core 에 뜬다.

## 참고: 진단 자체 성능
fallback MUS는 reify(하드마다 리터럴, 수천 개)로 무겁고 진단 TL=30초(MUS_DIAG_TIME)라, 큰 병동서 UNKNOWN(증명 미완) 가능. 래핑은 "회복/1N/주말이 병목이면 이름이 뜨게" 하는 필요조건이고, 진단이 시간 내 UNSAT 증명해야 실제로 뜬다(별도 성능 갭).
