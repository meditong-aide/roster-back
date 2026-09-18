"""렉시코그래피(사전식) 근무표 최적화 엔진 — **이것이 프로덕션 정식 경로다.**

이름이 `fallback` 이라 임시 우회로 보이지만 실제로 도는 것은 이 모듈이다.
`cp_sat_basic.py:1498` 의 `SKIP_PRIMARY` 기본값이 `"1"` 이라 primary(가중합 단일
Maximize) 를 건너뛰고 곧장 여기로 온다. 2026-05-28(3a0fc53)에 10런 검증에서
시간 -54~61% · coverage 0/10 · 품질 동급↑ 로 우세해 기본을 스킵으로 승격한 결과다.

★ 성능·파라미터를 논할 때 `cp_sat_basic.py` 의 값(workers=4/10, `random_seed`,
  `solution_pool_size`)을 근거로 삼으면 틀린다. 그 경로는 기본값에서 실행되지 않는다.
  이 파일의 솔버 5개는 전부 `num_search_workers=8` 이다.

구조 (순차 풀이 · CP-SAT 에 네이티브 렉시코가 없어 목적 교체 + 비회귀 제약으로 구현):
    stage1  커버리지 부족·과잉·OFF 총량        Minimize
    stage2  safety 합 + team 슬랙 + grade 미달   Minimize
      └ lex 6패스(`LEX_PASS_ORDER_DEFAULT`)를 같은 모델에 Minimize 교체로 순차 적용
    stage3  선호·공정성                        Maximize  ← grade 동결 탓에 자주 INFEASIBLE

계측(S0): `AIDE_LEX_TRACE=1` 로 단계·패스별 wall/status/objective/bound,
          `AIDE_LEX_LOG=1` 로 CP-SAT 탐색 로그. 둘 다 기본 off 라 동작은 불변이다.
"""

from __future__ import annotations

import os as _os_lex

import calendar
from datetime import timedelta
from typing import Dict, Optional

import numpy as np
from ortools.sat.python import cp_model

from services.cp_sat.hardcoded_weights import (
    FALLBACK_COVERAGE_SHORT_WEIGHT,
    FALLBACK_EXPERIENCE_SHORT_PENALTY,
    N_ONLY_NIGHT_BONUS,
    PREFERENCE_SCORE_SCALE,
)
from services.cp_sat.allowed_shift_types import (
    effective_night_cap,
    is_n_only_profile,
    normalize_allowed_shift_codes,
)
from services.cp_sat.fallback_objectives import build_fallback_stage3_objective_terms
from services.cp_sat.m_coverage import compute_main_bucket_indices
from services.cp_sat.night_distribution_log import log_n_even_distribution
from services.constraints.team_constraints import add_team_min_constraints
from services.day_windows import iter_nurse_days, build_active_days

# lex Stage 2 에서 team_min 커버 부족(slack) 1건당 패널티. safety(off-quota 10~30만) 위에
# 두어 팀 커버를 상위 co-priority 로 만든다(단, 커버리지/안전은 이미 상위 stage 에서 고정).
TEAM_COVER_LEX_WEIGHT = 300_000
# stage3 에 grade 미달 상한을 물려 lex 6-pass 재배치 결과를 지킬지 여부.
#   끄면 stage3 가 배치를 새로 계산해 재배치 결과가 흩어진다. 상세는 '폴백3 grade 동결' 주석.
#   문제가 생기면 이 값만 False 로 되돌리면 기존 동작으로 복귀한다.
GRADE_FREEZE_STAGE3 = True
# lex 패스 기본 순서. 앞일수록 우선(사전식) — 뒤 패스는 앞 결과를 동결한 채 움직인다.
#   env LEX_PASS_ORDER 로 덮어써 순열을 실측할 수 있다.
# 1순위 off_range · 2순위 grade · 3순위 team.
#
# 먼저 확정된 것 — grade·team 을 뒤에서 앞으로 옮긴 효과(초기 실측):
#   · grade 를 앞에 두면 7병동 중 6곳 개선·1곳 동일·악화 0 (9병동 13.7→5.7, 2병동 36.3→25.3)
#   · team 은 마지막(45.0) → 3번째(22.8) 로 절반. 위치에 강하게 반응한다.
#
# ★ 1번 자리는 그 뒤 다시 쟀다(7병동·5병원 × 2회, 3조건).
#   이전 비교는 측정 도구가 오염된 상태였다 — default_shift 가 빈 근무 코드
#   (MID·D2·Day·DE)를 OFF 로 세고 있었고, n2n 지표 자체가 없었다. 둘 다 고친 뒤 재측정.
#
#   합계로는 지표 10개 중 8개가 off_range 1번 쪽이 낫다
#     (n2n 380.5→341.0 · 고립OFF 28→24 · N폭 73→68.5 · N편차 18.0→17.1,
#      OFF폭 21.5 · 하드 0 · 커버부족 4.5 는 동률, grade 미달만 14.5→16.5 로 진다).
#
#   ★★ 그런데 **합계는 근거가 못 된다.** 병동별 부호검정으로 다시 보면
#     p<0.10 인 지표가 **하나도 없다**(36개 비교 전부).
#       n2n   -39.5 인데 3승 3패 1무 (p=1.000) — 4병동 한 곳의 -40 이 만든 값
#       고립OFF -4.0  인데 2승 2패 3무 (p=1.000)
#       가장 일관된 DE차이 1승 5패도 p=0.219 — 7병동으로는 여기까지다
#     즉 **품질 지표는 방향이 off_range 쪽으로 기울 뿐 증명되지 않았다.**
#     solver 가 비결정적이라 같은 조건 재실행에서도 크게 흔들린다
#     (51병동 grade 5.5~8.0 · 42병동 n2n 1.0 ↔ 55.5).
#
#   ★ 확실한 것은 **안전 지표가 순서와 무관하다**는 점이다 —
#     하드 위반 0 · 커버부족 4.5 가 세 조건 모두 7/7 동률.
#     그래서 어느 순서를 골라도 위험이 없고, 기운 쪽을 택했다.
#
#   ★ grade 는 병동 성격에 따라 갈린다:
#     · 공급 부족(9병동-9A: grade≤1 요구 90칸 대 가용 60칸) 6.0→9.0 악화
#     · 여유(남촌 중환자실1 37명·grade1 5명) 세 조건 모두 4.0 으로 **완전 동일**
#     못 채울 grade 를 붙들지 않고 다른 품질로 전환하는 셈이다.
#
#   ※ 표본 한계: 7병동 × 2회. 부호검정에서 p<0.05 를 보려면 동률을 빼고도
#     10곳 이상이 남아야 한다(지금은 동률이 많아 실효 3~4).
#   설정이 없는 병동에서는 각 prep 이 None 을 돌려 자동으로 건너뛴다.
#   ★ iso_off · off_quota 는 _PASS_PREP 에 구현돼 있으나 **의도적으로 뺐다**
#     (2026-09-04 실측 후 기각). 되살리려는 사람이 같은 길을 다시 걷지 않도록 남긴다.
#
#     동기는 타당했다 — stage2 는 safety 를 `sum(flat_safety) <= best_sum2` 로
#     **합계만** 동결한다. 30만·10만 가중치가 붙은 항목(isolated_off_slack ·
#     off_quota_excess)이 총합의 99.99% 를 차지해, 한 자릿수 항목
#     (week_off_missing · off_quota_short · pattern_eod)은 사실상 무제한으로 교환된다.
#     `_prep_iso_off` docstring 도 같은 진단을 적어 두었다("grade 를 6번째→1번째로
#     옮겨도 고립OFF 는 27 근처에서 꿈쩍하지 않았다 — 순서 문제가 아니라 대상이
#     아니었던 것").
#
#     그런데 **세 자리를 다 재보니 순이득이 없었다.**
#       맨앞(iso_off,off_quota,…)  safety 는 크게 좋아지는데 그 개선분이
#         grade·team 에서 옮겨온 것이었다 — 7B grade 36→40 · team 32→36 ·
#         off_range 5→10, 9병동 grade 7→12(+71%) · team 19→23.
#         lex 는 사전식이라 1·2번을 새 패스가 차지하면 기존 3~8번이 한 칸씩 밀리고
#         앞 패스가 동결한 상한 안에서만 최적화된다. 지표를 옮긴 것이지 좋아진 게 아니다.
#       맨뒤(…,iso_off,off_quota)  고립OFF -1건. 앞 6패스가 이미 동결해 여지가 없다.
#       team뒤(…,team,iso_off,off_quota,…)  고립OFF +1건 · pattern_eod 2→8 로 악화.
#
#     ★★ 그리고 그 차이들은 **재현되지 않는다.** 같은 조건을 반복하면
#       고립OFF 가 10↔11 건으로 흔들리고 두 조건의 값이 서로 뒤바뀐다
#       (현행 11건/10건 · 맨뒤 10건/11건). 시간도 같은 조건에서 47s/97s/139s 로
#       2~3배 흔들린다. 세 조건을 같은 병동(7B) 하나로 나란히 보면
#       iso 300만/330만/300만 · grade 37/35/36 · team 27/28/29 로 전부 편차 안이다.
#       즉 1회 측정으로는 어떤 자리도 판정할 수 없다. safety 합계 총량은 대체로
#       보존되고 **어디에 배분하느냐만 바뀐다** 는 것이 지금까지의 결론이다.
#
#     ★ 측정 함정 하나 더 — 조건마다 stage2 **도달 병동 수가 다를 수 있다.**
#       9병동은 맨뒤 조건에서 폴백1 이 INFEASIBLE 로 끝나 lex 에 진입조차 못 했다
#       (LEX_PASS_ORDER 는 폴백2 에서만 쓰이므로 순서와 무관한 비결정성이다).
#       그 상태로 조건별 **합계**를 비교하면 그 조건만 병동 1곳 값이 되어
#       "압도적으로 좋아 보인다". 반드시 모든 조건에서 도달한 병동만으로 비교할 것.
#
#     되살리려면 (가) 반복 측정으로 편차를 걷어내고 (나) safety 뿐 아니라
#     grade·team·off_range 를 **함께** 재야 한다. safety 만 보면 개선으로 착각한다
#     (실제로 그렇게 오판했다가 grade 를 함께 재고 뒤집었다).
#
#   ★ off_quota 를 **단독으로** 켜는 것은 어느 경우에도 안 된다 — 7B 실측에서
#     off_quota_excess 12→1 로 누른 대가로 pattern_eod 가 5→211 로 폭증했고
#     합계도 300만→340만으로 나빠졌다. 한 항목만 누르면 압력이 다른 데로 간다.
# ★ `pref` 를 맨 뒤에 둔다(2026-09-08 추가).
#   선호는 지금까지 stage3 목적에만 있었는데 그 stage3 가 실측 11곳 중 6곳에서
#   INFEASIBLE 이라, 그때 커밋되는 stage2 해에는 선호가 반영되지 않았다.
#   같은 병동 5회에서 반영이 1/1·0/1·1/1·0/1·0/1 로 흔들린 것이 그 증거다.
#   패스로 넣으면 stage3 성패와 무관하게 stage2 최종 해가 이 패스를 거친다(5/5 → 1/1).
#   ★ 맨 뒤인 것이 중요하다 — 앞의 safety·grade·team·n_range·n2n·de 가 모두 동결된
#     뒤에 돌므로 선호가 그것들을 이길 수 없다. 정책상 그게 맞다.
LEX_PASS_ORDER_DEFAULT = "off_range,grade,team,n_range,n2n,de,pref"
# Stage 2 에서 "요구 등급 미달"(cascade off=0) 을 최소화할 때의 가중치.
#   team(30만) 아래, safety off-quota 하한(10만) 위에 둔다. grade 는 team 과 같은
#   성격(고정된 커버리지 안에서의 재배치)이라 같은 층이되 team 을 앞세운다.
#   ★ cascade 의 대체 단계(off≥1) 는 2M/6M 이라 여기에 넣지 않는다 — 넣으면 team 과
#     safety 를 압도해 lex 우선순위가 뒤집힌다. 그 단계는 Stage 3 목적함수가 맡는다.
# ★★ 2026-09-11 · 15만 → 16만. **정수배를 깨는 것이 목적이다.**
#   종전 15만은 고립OFF(30만)와 정확히 2:1 이라
#     `고립OFF 1건 + 미달 3건` = 30만 + 45만 = **75만**
#     `고립OFF 2건 + 미달 1건` = 60만 + 15만 = **75만**   ← 완전 동률
#   16만이면 78만 대 76만이 되어 **뒤쪽(미달 1건)이 이긴다.**
#   그래서 어느 구성이 나올지를 한 자릿수 항목(off_quota 등) **1단위가 결정**했고,
#   달마다 작은 항목 지형이 바뀌면 반대 구성이 나왔다 —
#   사용자에겐 "같은 규칙인데 결과 성격이 다르다" 로 보인다. 이건 데이터가 아니라 설계 문제였다.
#   실측(9A · 2026-09-11): stage2 최적해가 750,013 대 750,014 로 **1 차이**였고
#   큰 항목 합은 양쪽 75만으로 같았다.
#   ★ 사용자 결정(2026-09-11): **등급 순수미달이 고립OFF 보다 나쁘다.**
#     (미달 = 그날 그 근무에 요구 등급 간호사가 목표 수에 못 미친 인원.
#      대체 등급으로 메운 건은 제외한 순수 미달이라 환자 안전 쪽 항목이다.
#      2026-05-13 주석이 team/grade 를 커버리지 다음의 안전 항목으로 두는 것과 같은 층.)
#   ★ 16만이면 `미달 2건(32만) > 고립OFF 1건(30만)` 이라 동률이 깨지고,
#     솔버가 **고립OFF 를 받더라도 미달을 줄이는** 해를 일관되게 고른다.
#   ★ 정수배(30만/15만·30만/10만)로 되돌리지 말 것 — 동률이 되살아난다.
#
#   ★★ **롤백 조건 (사용자 지시 · 2026-09-11)**
#     이 변경은 "미달을 줄이는 대신 고립OFF 를 **받는**" 방향이다. 그 대가가 예상보다
#     크면 되돌린다 — 구체적으로 **실측에서 고립OFF 건수가 유의하게 늘면 동급으로 되돌린다.**
#       · 판정: 같은 병동 현행(15만) 5회의 고립OFF 건수 **노이즈 폭**을 기준선으로 잡고,
#         16만 팔의 중앙값이 그 폭을 넘어 증가하면 회귀로 본다.
#       · 되돌릴 때는 15만이 아니라 **동률이 안 되는 값**을 쓴다(정수배 금지 원칙은 유효).
#         "동급으로" 는 우선순위 판단을 되돌린다는 뜻이지 15만 복귀가 아니다 —
#         15만으로 가면 어느 구성이 나올지를 다시 한 자릿수 항목이 정하게 된다.
#       · 되돌림도 `AIDE_GRADE_OFF0_W` 로 즉시 가능하다(코드 수정 불요).
GRADE_OFF0_LEX_WEIGHT = int(
    _os_lex.environ.get("AIDE_GRADE_OFF0_W", "160000") or 160_000)


# ── 계측(S0) ────────────────────────────────────────────────────────────────
# 전환 설계 S0 단계. **기본 off 라 프로덕션 동작은 불변**이다.
#   AIDE_LEX_TRACE=1  단계·패스별 wall time / status / objective / bound 를 [LexTrace] 로 출력
#   AIDE_LEX_LOG=1    CP-SAT log_search_progress 활성(그 자체로 오버헤드가 있어 별도 플래그)
# 왜 필요한가: 같은 조건 재실행에서 소요가 2.4배(9A 28~66초) 흔들리는데, 어느 단계가
#   시간을 쓰는지 몰라 원인을 추측만 하고 있다. 가설은 "어떤 패스가 어느 실행에선
#   최적을 증명해 즉시 끊기고 다른 실행에선 리밋까지 도는 이중분포" 인데, 패스별
#   wall/status/bound 없이는 확인할 수 없다.
def _trace_on() -> bool:
    import os as _os_t
    return _os_t.environ.get("AIDE_LEX_TRACE") == "1"


def _trace(logger_prefix: str, phase: str, **kv) -> None:
    """계측 한 줄. 기본 off."""
    if not _trace_on():
        return
    body = " ".join(f"{k}={v}" for k, v in kv.items() if v is not None)
    print(f"{logger_prefix} [LexTrace] {phase} {body}")


def _apply_trace_params(solver) -> None:
    """log_search_progress 는 오버헤드가 있어 AIDE_LEX_LOG=1 일 때만 켠다."""
    import os as _os_t
    if _os_t.environ.get("AIDE_LEX_LOG") == "1":
        solver.parameters.log_search_progress = True


# ── [S1 재정의] 정체 기반 조기 종료 + stage3 이월 · 2026-09-10 ─────────────────
#   S1 의 **전역 데드라인** 형태는 기각됐다(시간 이득 0). 그러나 그 측정이 실제로
#   말한 것은 "리밋이 사실상 안 걸린다" 가 아니라 **"패스마다 걸리는 회차와 안 걸리는
#   회차가 섞여 있다"** 였다. 9A 6회 실측(2026-09-10):
#
#     총 소요 36.4~58.5s(변동 22.0s). 40% 이상 단일 구간 **없음** —
#     lex2 26% · lex5 26% · lex6 24% · lex7 17% · 비-solve 15% 로 고르게 분산.
#     거의 모든 lex 패스가 **이중분포**다(증명 성공→조기 종료 ↔ 증명 실패→리밋 소진).
#     리밋까지 돈 27건의 '마지막 개선 시각 / wall' 분포:
#       >=70% 11건(리밋 직전까지 개선 중 — 시간이 더 필요)
#       <=30%  7건(초반에 멈추고 bound 만 상승 — 이미 최적인데 증명을 못 함)
#
#   ★ 그래서 예산을 일률로 늘리거나 줄이면 **양쪽 다 손해**다. 기준을 시간이 아니라
#     **개선 정체**로 바꾼다 — 최소 시간은 보장하고, 그 뒤 일정 시간 새 해가 없으면 끊는다.
#     리밋 직전까지 개선 중이던 회차는 안 건드리고, 초반에 멈춘 회차에서만 시간이 회수된다.
#   ★ 회수분은 **stage3 로 이월**한다. stage3 는 6/6 리밋 소진에 개선 시각이
#     58·82·44·32·74·99% 라 **시간을 더 주면 품질이 오르는 유일한 구간**이다.
#     전역 컷 형태에선 stage3 가 먼저 손해를 봤지만, 이월 형태에선 수혜자가 된다.
#   ★ lex 패스 목적은 전부 **정수 카운트**라 `absolute_gap_limit` 을 1 미만으로 두면
#     `best - bound < 1` = 사실상 최적에서 끊긴다. bound 가 오르는 패스에서 증명 완료를
#     기다릴 필요가 없어진다. LP 완화가 0 인 패스(고립OFF 등)엔 효과가 없지만 비용도 0 이다.
#
#   기본 off. `AIDE_LEX_STALL=1` 로 켠다. 회수 시간은 `_STALL_SAVED` 에 누적된다.
_STALL_SAVED: list = []          # [초...] — 생성 1회분. stage3 직전에 합산해 tl3 에 얹는다
# ★ stage2 의 **원래** 예산(tl2). 상한을 배수로 올려도 이월은 이 값을 넘지 않는다 —
#   안 그러면 늘려 준 시간이 회수분으로 둔갑해 stage3 로 흘러 총 소요가 폭증한다(:368).
_S2_BASE_TL: list = [0.0]
# ★ 정체 회수분의 **목적지**를 고른다(기본 off = 종전대로 stage3 로만).
#   `AIDE_LEX_CARRY=1` 이면 **뒤따르는 lex 패스**에 먼저 주고, 남으면 stage3 로 간다.
#   왜 분리했나: `AIDE_LEX_STALL` 은 "끊는" 처치이고 이건 "어디에 쓰는가" 라, 묶으면
#   A/B 에서 두 기여가 안 갈린다(6b 의 게이트 분리 원칙과 같다).
#   ★★ **모듈 상수로 잡으면 안 된다** — import 시점에 고정돼 A/B 에서 런마다 환경변수를
#     바꿔도 반영되지 않는다(측정이 조용히 한 조건만 반복한다). 다른 게이트가 전부
#     `_stall_config()` 처럼 함수 안에서 읽는 이유가 이것이다.
def _lex_carry_on() -> bool:
    return _os_lex.environ.get("AIDE_LEX_CARRY") == "1"


def _stall_config():
    """(정체종료 활성, 최소보장초, 정체판정초, gap처치 활성, absolute gap). 둘 다 기본 off.

    ★ 게이트를 **둘로 나눈다.** 처음엔 한 플래그에 묶여 있었는데, 그러면 A/B 에서
      "정체 종료" 와 "relative 0 + absolute gap" 의 기여가 안 갈린다.
        AIDE_LEX_STALL=1  정체 기반 조기 종료 + stage3 이월
        AIDE_LEX_GAP=1    lex 패스의 relative_gap_limit 0 + absolute_gap_limit 0.99
    """
    import os as _os_s
    _stall = _os_s.environ.get("AIDE_LEX_STALL") == "1"
    _gap = _os_s.environ.get("AIDE_LEX_GAP") == "1"
    return (_stall,
            float(_os_s.environ.get("AIDE_LEX_STALL_MIN", "2") or 2),
            float(_os_s.environ.get("AIDE_LEX_STALL_IDLE", "3") or 3),
            _gap,
            float(_os_s.environ.get("AIDE_LEX_ABS_GAP", "0.99") or 0.99))


def _s2_stall_config():
    """stage2 본 solve 전용 (활성, 최소보장초, 정체판정초). 기본 off.

    ★★ 왜 lex 값을 그대로 안 쓰는가 — **stage2 는 시간을 더 줘야 하는 구간**이라
      판정이 반대 방향이다. lex 는 "정체하면 빨리 끊고 넘긴다"(IDLE 3초)지만,
      stage2 는 "개선이 이어지는 한 상한까지 간다" 가 목적이라 임계가 보수적이어야 한다.
      실측(2026-09-11 · 시화중환2)에서 stage2 는 리밋 직전까지(wall 대비 90~100%)
      개선 중이었다 — 3초 임계면 그 개선을 중간에 끊는다.

    ★ 이 처치가 겨냥하는 것: 종료 사유가 똑같이 "시간리밋" 이어도 속은 정반대다.
        시화중환2  마지막개선 90·97%  → 개선 중인데 잘렸다  → 상한까지 줘야 한다
        세브7      마지막개선 47·15%  → 3.2초에 멈추고 21초까지 돌았다 → 끊어야 한다
      정체 워치독은 **그 둘을 자동으로 가른다.** 임계를 사전에 정할 필요가 없다.
    """
    import os as _os_s2
    return (_os_s2.environ.get("AIDE_S2_STALL") == "1",
            float(_os_s2.environ.get("AIDE_S2_STALL_MIN", "3") or 3),
            float(_os_s2.environ.get("AIDE_S2_STALL_IDLE", "7") or 7))


class _LastImprove(cp_model.CpSolverSolutionCallback):
    """새 해가 나올 때마다 시각을 갱신한다 — 정체 판정의 기준."""

    def __init__(self):
        super().__init__()
        import time as _t
        self._t = _t
        self.last = _t.perf_counter()
        self.count = 0

    def on_solution_callback(self):
        self.last = self._t.perf_counter()
        self.count += 1


def _solve_with_stall_stop(solver, model, phase: str):
    """정체하면 끊는다. `stop_search()` 는 다른 스레드에서 부르도록 만들어져 있다.

    걸리는 곳: lex 패스 전부 + **stage2 본 solve**(`phase == "stage2"` · 2026-09-11 확장).
    ★ stage3 에는 걸지 않는다 — 거기는 시간을 **더** 줘야 하는 구간이다.
    ★ stage2 를 넣는 것은 그 원칙과 모순이 아니다: 상한을 3배로 올려 주면서(:3723)
      정체했을 때만 끊으므로 **주는 쪽**이다. 개선이 이어지는 병동은 63초까지 가고,
      3.2초에 멈추는 병동은 임계에서 끊겨 현행(21초)보다 오히려 빨라진다.
    """
    import threading
    import time as _t

    on, min_sec, idle_sec, gap_on, abs_gap = _stall_config()
    # ── stage2 본 solve 로 확장 (2026-09-11) ──────────────────────────────────
    #   ★ 별도 게이트를 만들지 않는다. "마지막개선/wall ≥ 임계면 연장" 과
    #     "N초 정체면 종료" 는 **같은 정보를 쓰는 같은 규칙**이라, 이미 있는 워치독을
    #     stage2 로 확장하고 상한만 올리면 된다(:3680 에서 tl2 × 3).
    #   ★★ `phase == "stage2"` 로 **정확히** 좁힌다 — `startswith` 를 쓰면
    #     best-of-N 의 서브 solve(`stage2#1`·`stage2#2`…)와 `stage2:best` 에 각각
    #     상한 3배가 걸려 총 예산이 **N×3 배**가 된다. best-of-N 은 기본 off(:3679)라
    #     지금은 안 닿지만, 켜는 순간 조용히 터진다.
    _s2_phase = (phase == "stage2")
    if _s2_phase:
        on, min_sec, idle_sec = _s2_stall_config()
    elif not phase.startswith("lex"):
        return solver.Solve(model)
    if not (on or gap_on):
        return solver.Solve(model)
    if _s2_phase:
        gap_on = False          # stage2 의 gap 은 :3690 에서 따로 건다(AIDE_S2_GAP)

    # ★★ lex 패스는 `s2` 를 **재사용**한다(:3570 에서 한 번 만들고 max_time 만 바꾼다).
    #   거기 `relative_gap_limit = 0.15`(:3573)가 걸려 있는데, lex 목적은 정수 카운트라
    #   목적값 20 이면 **gap 3 에서 끊긴다** — `absolute_gap_limit=0.99` 보다 훨씬 먼저다.
    #   그러면 absolute 는 영영 발화하지 못하고, 더 나쁜 것은 **동결값이 최적보다 최대 15%
    #   나쁜 채로 다음 패스에 고정**된다는 것이다(품질 누수).
    #   정체 종료가 시간 낭비를 막아 주므로 lex 패스에서는 relative 를 0 으로 두고
    #   absolute 만 남긴다. 큰 목적값을 다루는 stage2 본 solve 는 0.15 를 그대로 쓴다.
    _prev_rel = float(getattr(solver.parameters, "relative_gap_limit", 0.0) or 0.0)
    if gap_on:                                   # ★ gap 처치는 독립 게이트다
        solver.parameters.relative_gap_limit = 0.0
        if abs_gap > 0:
            solver.parameters.absolute_gap_limit = abs_gap

    if not on:
        # gap 처치만 켠 팔 — 워치독 없이 그대로 푼다.
        try:
            return solver.Solve(model)
        finally:
            solver.parameters.relative_gap_limit = _prev_rel

    cb = _LastImprove()
    budget = float(getattr(solver.parameters, "max_time_in_seconds", 0) or 0)
    started = _t.perf_counter()
    done = threading.Event()
    stopped = threading.Event()      # 워치독이 실제로 끊었는가

    def _watch():
        while not done.wait(0.2):
            now = _t.perf_counter()
            if now - started < min_sec:
                continue
            if now - cb.last >= idle_sec:
                stopped.set()
                solver.stop_search()
                return

    th = threading.Thread(target=_watch, daemon=True)
    th.start()
    try:
        st = solver.SolveWithSolutionCallback(model, cb)
    finally:
        done.set()
        # ★★ 반드시 합류시킨다. `s2` 가 **공유 객체**라, 살아남은 워치독이 다음 패스의
        #   `stop_search()` 를 부르면 그 패스가 이유 없이 일찍 끝난다 —
        #   로그에도 안 보이고 "왜 갑자기 품질이 나쁘지" 로만 나타난다.
        th.join(timeout=1.0)
        solver.parameters.relative_gap_limit = _prev_rel     # 다음 패스에 새지 않게 복원

    spent = _t.perf_counter() - started
    # ★ 이월하는 것은 **정체로 끊어 회수한 시간뿐**이다.
    #   증명으로 일찍 끝난 여유까지 넘기면 그건 현행에서도 안 쓰던 시간이라
    #   총 소요가 늘어난다(1회 샘플에서 48.1s → 53.3s 로 는 것이 이 때문이다).
    # ★★ stage2 는 **상한을 3배로 올려 놓았기 때문에** `budget - spent` 로 재면 안 된다.
    #   세브7 을 예로 들면 상한 63초에서 10초에 정체로 끊기는데, 그대로 재면 53초가
    #   "회수" 로 잡힌다. 그중 42초는 현행(tl2=21초)에 **애초에 없던 시간**이라
    #   이월하면 총 소요가 폭증한다 — 바로 위 주석이 경고하는 함정과 같은 것이고,
    #   상한을 올리는 순간 그 전제("워치독이 남긴 시간은 어차피 안 쓰던 시간")가 깨진다.
    #   그래서 **원래 예산(tl2)을 상한으로 잘라서** 잰다.
    _cap = budget
    if _s2_phase and _S2_BASE_TL[0] > 0:
        _cap = min(budget, _S2_BASE_TL[0])
    if stopped.is_set() and _cap > 0 and spent < _cap:
        _STALL_SAVED.append(_cap - spent)
    return st


def _solve_traced(solver, model, logger_prefix: str, phase: str):
    """Solve 를 감싸 wall time·status·objective·bound 를 남긴다.

    ★ BestObjectiveBound 는 지금 이 파일 어디서도 안 쓴다. 최적 증명 실패 시
      하한 정보가 버려지고 있어, 갭이 큰 패스를 식별하려면 먼저 기록해야 한다.
    """
    from time import perf_counter as _pc
    _apply_trace_params(solver)
    _t0 = _pc()
    # 정체 조기 종료(기본 off · AIDE_LEX_STALL=1). lex 패스에만 걸린다 — 위 주석 참조.
    st = _solve_with_stall_stop(solver, model, phase)
    _dt = _pc() - _t0
    if _trace_on():
        _obj = _bnd = None
        try:
            _obj = solver.ObjectiveValue()
            _bnd = solver.BestObjectiveBound()
        except Exception:
            pass
        _trace(logger_prefix, phase, sec=f"{_dt:.2f}",
               status=_cp_sat_status_to_text(st), obj=_obj, bound=_bnd)
    return st


def _cp_sat_status_to_text(status: int) -> str:
    """CP-SAT 상태 코드를 사람이 읽을 수 있는 문자열로 변환한다."""
    mapping = {
        cp_model.OPTIMAL: "OPTIMAL",
        cp_model.FEASIBLE: "FEASIBLE",
        cp_model.INFEASIBLE: "INFEASIBLE",
        cp_model.MODEL_INVALID: "MODEL_INVALID",
        cp_model.UNKNOWN: "UNKNOWN",
    }
    return mapping.get(status, f"UNKNOWN({status})")


def _load_off_policy_helpers():
    module = __import__("services.cp_sat.off_policy", fromlist=["*"])
    return (
        getattr(module, "build_off_partitions"),
        getattr(module, "compute_off_bounds"),
        getattr(module, "off_cap_semantics_label"),
        getattr(module, "resolve_effective_off_days"),
        getattr(module, "resolve_max_extra_off_days"),
    )


def _log_weekend_off_enforcement(
    roster_system,
    join: list[int],
    leave: list[int],
    weekend_days: set[int],
    fixed: dict[tuple[int, int], int],
    off_idx: int | None,
    logger_prefix: str,
) -> None:
    """주말 OFF 강제 제약 적용 내역을 간호사별로 출력한다."""
    if not getattr(roster_system.config, "weekend_off_only_enable", True):
        return
    if off_idx is None:
        return
    for n, nu in enumerate(roster_system.nurses):
        if not bool(getattr(nu, "is_weekend_off", False)):
            continue
        t0, t1 = join[n], leave[n]
        weekend_in_range = [d for d in sorted(weekend_days) if t0 <= d <= t1]
        weekend_days_1based = [d + 1 for d in weekend_in_range]
        forced_days = []
        skipped_fixed_days = []
        for d in weekend_in_range:
            if (n, d) in fixed and fixed[(n, d)] != off_idx:
                skipped_fixed_days.append(d + 1)
            else:
                forced_days.append(d + 1)
        nurse_id = getattr(nu, "nurse_id", "?")
        nurse_name = getattr(nu, "name", "?")
        print(
            f"{logger_prefix} [WeekendOff][Enforce] nurse_idx={n}, "
            f"nurse_id={nurse_id}, name={nurse_name}, "
            f"weekend_days={weekend_days_1based}, "
            f"forced_off_days={forced_days}, "
            f"skipped_fixed_days={skipped_fixed_days}"
        )


def _log_weekend_work_assignments(
    roster_system,
    weekend_days: set[int],
    off_idx: int | None,
    logger_prefix: str,
) -> None:
    """주말 근무 배정 여부를 간호사별로 출력한다."""
    shift_types = roster_system.config.shift_types
    off_code = shift_types[off_idx] if off_idx is not None else None
    for n, nu in enumerate(roster_system.nurses):
        weekend_work = []
        for d in sorted(weekend_days):
            if d < 0 or d >= roster_system.num_days:
                continue
            assigned_code = None
            for s_idx, code in enumerate(shift_types):
                if int(roster_system.roster[n, d, s_idx]) == 1:
                    assigned_code = code
                    break
            if assigned_code is None:
                continue
            if off_code is not None and assigned_code == off_code:
                continue
            weekend_work.append(f"{d + 1}:{assigned_code}")
        nurse_id = getattr(nu, "nurse_id", "?")
        nurse_name = getattr(nu, "name", "?")
        is_weekend_off = bool(getattr(nu, "is_weekend_off", False))
        # if weekend_work or is_weekend_off:
        #     print(
        #         f"{logger_prefix} [WeekendOff][Work] nurse_idx={n}, "
        #         f"nurse_id={nurse_id}, name={nurse_name}, "
        #         f"is_weekend_off={int(is_weekend_off)}, weekend_work={weekend_work}"
        #     )


def optimize_fallback_lex_hard_first(
    *,
    roster_system,
    time_limit_seconds: int,
    grouped: list[dict] | None,
    shift_type_map: dict[str, str] | None,
    logger_prefix: str,
    timer_cls,
    add_preceptor_terms_fn,
    add_grade_constraints_fn,
    postprocess_rebalance_off_fn,
    blocked_by_nurse: dict[int, set[int]] | None = None,
) -> bool:
    """하드 제약을 최우선으로 하는 서열(lexicographic) 폴백 최적화 수행.

    단계 개요:
    1단계(커버리지 우선): 일/교대 커버리지 부족(short) 최소화. 식: assigned + short - over == need.
    2단계(안전/법규): 1단계 최솟값(short 합)과 over 상한을 고정, 전이/연속/월간/주2OFF/회복/NOD/NOE/야간전담 위반을 정량 슬랙으로 최소화.
    3단계(품질/선호): 1,2단계 결과를 고정(특히 2단계에서 0이었던 위반 위치는 0으로 잠금)한 채 선호/공정성 최대화. 새 위반 생성 금지.

    Args:
        roster_system: 근무표 시스템 객체
        time_limit_seconds: 총 시간 제한(초)
        grouped: 교대 코드 매핑 정보(고정셀 main_code 정규화에 사용)
        shift_type_map: 근무 코드별 유형 매핑(예: 휴가/공가/교육 등)
        logger_prefix: 로그 접두사
        timer_cls: with 구문에 사용할 Timer 클래스
        add_preceptor_terms_fn: 프리셉터 목적함수 항 생성 함수
        add_grade_constraints_fn: Grade 제약 추가 함수
        postprocess_rebalance_off_fn: 후처리(OFF 재배치) 함수

    Returns:
        bool: 최종적으로 하드 위반 합이 0인 해를 달성했는지 여부
    """
    print(f"{logger_prefix} 폴백(서열) 최적화 시작…")

    # 동적 시간 배분(대략): 45% / 35% / 20%
    #
    # ★★ `time_limit_seconds` 는 **총 상한이 아니라 단계별 배분의 기준**이다.
    #   각 단계가 자기 몫을 따로 받고, 그 위에 후속 패스가 더 얹는다:
    #     · lex 7패스 = tl2 × (비율 합 **2.0**)
    #       `LEX_PASS_ORDER_DEFAULT`(:142) 기준 off_range .2 + grade .3 + team .3
    #       + n_range .2 + n2n .5 + de .3 + pref .2 — 7개 전부 기본 활성이다
    #       (`de_balance_enable` 은 roster_config.py 기본 True, `pref` 는 항이 있으면 항상).
    #     · mutex-lex = `max(8, int(tl3))` 를 한 번 더(:4252)
    #   그래서 실제 소요가 이 값을 크게 넘는다 —
    #   실측(중환자실1 · 2026-09-09): 설정 60초, 실제 160~178초.
    #     tl1 27 + tl2 21 + lex 42 + mutex-lex 12 + 서비스 계층 ≈ 29 로 계산이 닫힌다.
    #   ★ 이걸 '초과' 로 읽고 조이면 안 된다. stage3 가 시간에 쫓겨 UNKNOWN 으로
    #     죽으면 stage2 해가 커밋되고 **선호·공정성이 통째로 버려진다** —
    #     grade 중복 수정으로 방금 되살린 바로 그 문제로 되돌아간다.
    #   ★★ 다만 상한이 **없지는 않다.** SQS 소비자는 ECS 가 아니라 **Lambda** 다
    #     (`app/lambda_handler.py` 가 진입점 · `Dockerfile.lambda` · deploy-lambda.yml
    #      이 `roster-solver-{prod,dev}` 를 갱신. worker.py 의 `main()` 은 ECS 진입점이나
    #      SQS 가 아니라 `JOB_JSON` 을 읽는다 — SQS 를 소비하는 ECS 경로는 없다).
    #     실측(2026-09-10 · AWS 조회): Lambda Timeout **600초** · MemorySize 4096,
    #     큐 `roster-job-queue` VisibilityTimeout 720초 · maxReceiveCount 3 → DLQ.
    #     즉 600초에서 잘리고, 잘리면 720초 뒤 **같은 근무표를 3번 다시 생성한 뒤** DLQ 로 간다.
    #     현재 160~178초는 그 30% 수준이라 여유가 있지만, 예산을 더 올릴 때는 이 벽을 봐야 한다.
    #     (전역 데드라인 시도는 2026-09-08 기각 — 아래 주석 참조)
    tl1 = max(5, int(time_limit_seconds * 0.45))
    tl2 = max(5, int(time_limit_seconds * 0.35))
    tl3 = max(3, time_limit_seconds - tl1 - tl2)
    # ★ 생성 1회분 누적이다. 리셋하지 않으면 같은 프로세스의 두 번째 생성부터
    #   이월이 계속 부풀어 stage3 예산이 무한정 늘어난다(in-process 하네스에서 즉시 드러난다).
    _STALL_SAVED.clear()
    import os as _os_tl3
    _tl3_override = int(_os_tl3.environ.get("AIDE_FB_TL3", 0) or 0)
    if _tl3_override > 0:
        tl3 = _tl3_override

    # ── [기각] 전역 데드라인 · 2026-09-08 제거 (AIDE_LEX_S1 / _CARRY / _SKIP0) ──
    # 착상: 단계마다 예산을 새로 주므로 lex 6패스의 tl2 비율 합 1.8 까지 얹혀
    #   60초 요청이 실제 121초까지 간다. 시작 시각 기준으로 각 solve 를 조이려 했다.
    #
    # ★ 기각 근거 ― 시간 이득이 0 이었다. 11곳 × 5회에서 현행 430s 대 S1 432s.
    #   앞서 보이던 15% 단축은 함께 켜져 있던 '이미 0 인 패스는 solve 를 건너뛴다'
    #   (SKIP0)가 패스를 **죽여서** 생긴 착시였다.
    # ★ SKIP0 의 오류: **아직 풀지 않은 패스의 목적값을 직전 해에서 읽으려 한 것**이다.
    #   각 패스의 변수는 `_prep()` 안에서 그때 생성된다(아래 `_prep_*` 참조).
    #   그래서 `s2.Value(_obj)` 는 그 변수가 없던 시점의 해를 읽어 0 을 돌려주고,
    #   0 이 아닌 패스를 0 으로 오판해 동결한다. `off_range` 라면 `max-min <= 0`,
    #   곧 "전원의 OFF 수가 완전히 같아야 한다" 가 되어 뒤 패스가 전부 무너진다.
    #   실측: 스킵 16회에 패스 실패 12회가 딸렸고 pass_team 은 33→0.
    #   ★ `_freeze` 자체는 문제가 아니다 — 그걸 빼면 뒤 패스가 앞 패스 결과를
    #     희생시켜 lex 가 성립하지 않는다. 문제는 '풀지 않고 값을 안다고 가정한 것'.
    #
    # ★ 시간을 줄일 자리는 여기가 아니다. 실측(중환자실2) solver 59.1s / 전체 96.8s 로,
    #   차 ~29초는 서비스 계층(간호사 수집·선호 파싱·constraint_impact·presolve 진단)이다.
    #   solve 를 조여도 그쪽은 줄지 않는다.
    # ★ 대신 재고 있는 것은 S4(safety 항목별 동결)다. 예산을 깎는 대신 탐색 공간을
    #   좁혀, 같은 품질을 더 빨리 낸다(별관1 38s→10s · 회차 편차도 소멸).

    # ── [S4] safety 동결 — 성격이 다른 둘이라 플래그를 나눈다 ────────────────
    #   ① stage2: 총합 동결 → **항목별** 동결. 설계 변경이다.
    #      총합만 묶으면 30만·15만 가중 항목이 합계의 99.99% 라, 한 자릿수 항목
    #      (week_off_missing·off_quota_short·pattern_eod)이 사실상 무제약이 된다.
    #   ② stage3: `sum(safety3[k]) == sum(safety2[k])` 를 **값 기준**으로 고친다.
    #      양변이 변수식이라 계수가 상쇄돼 `0 == 0` 이던 **빈 제약**이었다.
    #      즉 stage3 내내 safety 방어가 없었다 — 이건 개선이 아니라 **버그 수정**이다.
    #
    #   ★ 나눈 이유: 둘은 근거의 성격이 다르다. 묶어 두면 ② 를 올리려고 ① 까지 끌고 간다.
    #
    #   ── 판정 (2026-09-08) ────────────────────────────────────────────────
    #   ① **보류.** 11곳 × 3회 + 2곳 × 8회(98회)로도 개선이 입증되지 않았다.
    #      변화가 전부 노이즈 폭 안이었고 부호검정 p=1.000.
    #      한때 시간이 10% 줄어 보였으나 표본을 바꾸면 방향이 뒤집혔다(-10.2% ↔ +13%).
    #   ② **채택(기본 on).** 11곳 × 5회(110회) 결과가 품질 중립이다 —
    #      `_s3_ok` 5=5(stage3 성공률 유지) · `_pass_fail` 0 · 소요 462→470s.
    #      safety 합계가 2,440만→2,790만이지만 노이즈 폭이 360만이고 그 증가분이
    #      **중환자실2 한 곳**(iso 폭 870만~1230만)에 몰려 있어 판정 근거가 못 된다.
    #      개선도 악화도 입증되지 않았다는 것이 정확한 요약이다.
    #      ★ 그런데도 켜는 이유: 이건 A/B 우위를 다투는 개선이 아니라 **버그 수정**이다.
    #        고친 제약은 원래 `0 == 0` 이라 stage3 내내 safety 방어가 없었다.
    #        방치하면 stage3 가 safety 를 망칠 여지가 열려 있고, 망가져도 드러나지 않는다.
    #        측정이 요구한 것은 '고쳐도 안 죽는다' 하나였고 그것이 확인됐다.
    #   ★ 되돌리려면 `AIDE_LEX_S4_STAGE3=0`. `AIDE_LEX_S4=1` 은 둘 다 켠다(측정 재현용).
    _s4_all = _os_tl3.environ.get("AIDE_LEX_S4") == "1"
    _s4_stage2 = _s4_all or _os_tl3.environ.get("AIDE_LEX_S4_STAGE2") == "1"
    _s4_stage3 = _s4_all or _os_tl3.environ.get("AIDE_LEX_S4_STAGE3", "1") != "0"

    # [S0 계측] 구간 타이머. solve+build 를 다 합쳐도 총 소요의 절반뿐이라
    #   (실측 39.6s / 70.2s) 나머지가 어디에 쓰이는지 본다.
    from time import perf_counter as _pc_seg0
    _seg_t = [_pc_seg0()]

    def _mark(name: str) -> None:
        from time import perf_counter as _pc_m
        _now = _pc_m()
        if _seg_t[0] is not None and _trace_on():
            _trace(logger_prefix, "seg:" + name, sec=f"{_now - _seg_t[0]:.2f}")
        _seg_t[0] = _now

    N, D, S = len(roster_system.nurses), roster_system.num_days, roster_system.config.num_shifts
    cfg = roster_system.config
    prev_off_tail = getattr(roster_system, "prev_month_off_tail_by_idx", {}) or {}
    prev_month_n_tail_by_idx = getattr(roster_system, "prev_month_n_tail_by_idx", {}) or {}
    (
        build_off_partitions,
        compute_off_bounds,
        off_cap_semantics_label,
        resolve_effective_off_days,
        resolve_max_extra_off_days,
    ) = _load_off_policy_helpers()
    effective_off_days, effective_off_source = resolve_effective_off_days(cfg)
    effective_max_extra = resolve_max_extra_off_days(cfg, 0)
    off_cap_semantics = off_cap_semantics_label()
    print(
        f"{logger_prefix} [OffPolicy][Fallback] effective_off_days={effective_off_days}, "
        f"source={effective_off_source}, max_extra_off_days={effective_max_extra}, "
        f"raw_off_days={getattr(cfg, 'off_days', None)}, cap_semantics={off_cap_semantics}"
    )

    # 공통 인덱스/구간
    idx = {c: roster_system.config.shift_types.index(c) for c in ("D", "E", "N", "O")}
    day_idx, eve_idx, night_idx, off_idx = idx["D"], idx["E"], idx["N"], idx["O"]
    mid_idx = roster_system.config.shift_types.index("M") if "M" in roster_system.config.shift_types else None
    has_w = "W" in roster_system.config.shift_types
    w_idx = roster_system.config.shift_types.index("W") if has_w else None                      # O 인덱스 e.g. 2
    off_exception_cells = set(getattr(roster_system.config, "off_exception_cells", []) or [])  # (n, d) 튜플 집합 e.g. {(0, 1), (1, 2)}
    off_exception_vacation_cells = set(
        getattr(roster_system.config, "off_exception_vacation_cells", []) or []              # (n, d) 튜플 집합 e.g. {(0, 1), (1, 2)}
    )
    vac_cells = set(off_exception_vacation_cells)
    # print('이미 있음 W', w_idx)
    # print('이미 있음 off_exception_cells', off_exception_cells)
    # print('이미 있음 off_exception_vacation_cells', off_exception_vacation_cells)
    first_day = roster_system.target_month
    D_phys = calendar.monthrange(first_day.year, first_day.month)[1]
    last_day = first_day + timedelta(days=D - 1)
    weekend_days = {d for d in range(D) if (first_day + timedelta(days=d)).weekday() >= 5}
    join, leave = [], []
    for nu in roster_system.nurses:
        j = (nu.joining_date - first_day).days if nu.joining_date else 0
        if nu.resignation_date:
            # ★ resignation_date 는 퇴사일 그 자체 — 그날부터 소속이 아니므로
            #   마지막 근무일은 resignation_date - 1 일이다(cp_sat_basic 과 동일 규약).
            _last_work = nu.resignation_date - timedelta(days=1)
            if _last_work < first_day:
                # 이번 달에 근무하지 않는 인원은 범위 밖으로 설정하여 변수 생성을 건너뛴다.
                join.append(1)
                leave.append(0)
                continue
            l = (_last_work - first_day).days
            if _last_work > last_day:
                l = D - 1
        else:
            l = D - 1
        j = max(j, 0)
        l = min(l, D - 1)
        join.append(j)
        leave.append(l)

    # 고정셀(메인코드 정규화)
    code2main = {
        str(c).strip().upper(): str(r["main_code"]).strip().upper()
        for r in (grouped or [])
        for c in r["codes"]
    }
    code2type = {}
    if shift_type_map:
        code2type.update(shift_type_map)
    code2type.update(
        {
            str(c).strip().upper(): r.get("type")
            for r in (grouped or [])
            for c in r["codes"]
        }
    )
    shift_id_to_main_map = {
        str(k).strip().upper(): str(v).strip().upper()
        for k, v in (getattr(roster_system, "shift_id_to_main", {}) or {}).items()
        if str(k or "").strip() and str(v or "").strip()
    }

    def _normalize_fixed_to_main(raw_code: object) -> str:
        code = str(raw_code or "").strip().upper()
        if not code:
            return ""
        mapped = code2main.get(code) or shift_id_to_main_map.get(code) or code
        if mapped in {"OFF", "주"}:
            return "O"
        return mapped

    fixed, fixed_cnt = {}, [[0] * S for _ in range(D)]
    fixed_type_by_cell: dict[tuple[int, int], Optional[str]] = {}
    fixed_source_by_cell: dict[tuple[int, int], str] = {}
    fixed_wanted_cells: set[tuple[int, int]] = set()

    def _normalize_fixed_source(
        raw_source: object,
        shift_type: Optional[str],
        shift_main: str,
    ) -> str:
        src = str(raw_source or "").strip().lower()
        if src:
            if src == "fixed_wanted":
                return "fixed_wanted"
            if src == "2n2off_recovery":
                return "recovery_2n2off"
            if src == "3n2off_recovery":
                return "recovery_3n2off"
            if src == "recovery_off":
                return "recovery_off"
            if src in {"weekly_off", "weekoff", "weekly_off_fixed"}:
                return "weekly_off"
            if src in {"special_fixed", "special", "vacation", "leave"}:
                return "special"
            return src
        st = str(shift_type or "").strip()
        if st == "주휴":
            return "weekly_off"
        if st in {"휴가", "공가", "휴무"}:
            return "special"
        if shift_main == "O":
            return "off_fixed"
        return "manual"

    def _fixed_pattern_from_source(source: str) -> str:
        return f"fixed_assignment:{source or 'manual'}"

    # print('이미 있음 fixed_type_by_cell', fixed_type_by_cell)
    for c in getattr(roster_system, "fixed_cells", []) or []:
        
        n, d = c["nurse_index"], c["day_index"]
        s_main = _normalize_fixed_to_main(c.get("shift"))
        if s_main not in roster_system.config.shift_types:
            print(
                f"{logger_prefix} fixed 셀 코드 스킵(미지원 메인코드): "
                f"n={n}, d={d + 1}, raw={c.get('shift')}, main={s_main}"
            )
            continue
        s_idx = roster_system.config.shift_types.index(s_main)
        # print('이미 있음 c', c, s_idx)
        fixed[(n, d)] = s_idx
        fixed_cnt[d][s_idx] += 1
        # print('fallback fixed', fixed)
        # 코드에 타입 매핑이 없으면 메인 코드 기준으로 재시도
        raw_code = str(c.get("shift") or "").strip().upper()
        # 빌더가 명시한 shift_type 을 우선 (예: weekly_off="주휴", special="휴가/휴무/공가").
        fixed_type_by_cell[(n, d)] = (
            (c.get("shift_type") or "").strip()
            or code2type.get(raw_code)
            or code2type.get(s_main)
        )
        fixed_source_by_cell[(n, d)] = _normalize_fixed_source(
            c.get("fixed_source"),
            fixed_type_by_cell[(n, d)],
            s_main,
        )
        if str(c.get("fixed_source") or "").strip().lower() == "fixed_wanted":
            fixed_wanted_cells.add((n, d))

        # print('이미 있음 fixed_type_by_cell', fixed_type_by_cell)
        # print('이미 있음 fixed_type_by_cell', fixed_type_by_cell)

    # 초기 금지(경계) 맵
    initial_forbidden = (
        getattr(roster_system, "initial_forbidden", {})
        if isinstance(getattr(roster_system, "initial_forbidden", {}), dict)
        else {}
    )

    # ── 프리셉티 인덱스/기간 (cp_sat_basic 와 동일 정책 — nurse_preceptee_period SSOT) ──
    # 맵 있음=권위 모드(맵만 신뢰, default=follow 없음), 맵 없음=전환 폴백(캐시 기반 무회귀).
    # 설계: docs/NURSE_PRECEPTEE_PERIOD_DESIGN.md §6.
    preceptee_follow = bool(getattr(cfg, 'preceptee_on', False))
    _fb_id_to_idx = {nu.db_id: n for n, nu in enumerate(roster_system.nurses)}
    preceptee_follow_days: dict[int, set[int]] = getattr(roster_system, "preceptee_follow_days", {}) or {}
    _has_preceptee_period = bool(getattr(roster_system, "preceptee_period_authoritative", False))
    if _has_preceptee_period:
        preceptee_indices: set[int] = {n for n, days in preceptee_follow_days.items() if days}
    else:
        preceptee_indices = {n for n, nu in enumerate(roster_system.nurses) if getattr(nu, 'preceptor_id', None)}
    if preceptee_indices:
        print(f"{logger_prefix} [Fallback] 프리셉티 인덱스: {len(preceptee_indices)}명 "
              f"(follow={preceptee_follow}, period_map={_has_preceptee_period})")

    def _is_preceptee_at(n: int, d: int = -1) -> bool:
        """(n, d)가 프리셉티 follow 대상인지. 맵 없으면 전체월 follow(폴백), 맵 있으면 day별."""
        if not preceptee_follow or n not in preceptee_indices:
            return False
        if not _has_preceptee_period:
            return True  # 폴백(맵 없음): 전체월 follow
        days = preceptee_follow_days.get(n)
        if not days:
            return False  # 그 달 프리셉티 아님(종료/미겹침)
        if d < 0:
            return False  # nurse-level: day별 판별 필요
        return d in days

    exclude_preceptee_from_den = (not getattr(cfg, 'preceptee_shift_count', True)) and bool(preceptee_indices)
    coverage_exclude_cells: set[tuple[int, int]] = getattr(roster_system, "coverage_exclude_cells", set()) or set()
    # 외부 스코프에서 로깅용으로 사용 (build_model 내부에서도 별도 정의)
    weekly_off_by_idx = (
        getattr(roster_system, "weekly_off_by_idx", {})
        if isinstance(getattr(roster_system, "weekly_off_by_idx", {}), dict)
        else {}
    )
    # print('이미 있음 initial_forbidden', initial_forbidden)
    # ── 폴백 사전 진단 로그(불가능 원인 빠른 파악용) ──
    try:
        # 월간 N 총 요구(고정셀로 이미 채워진 N은 제외)
        total_need_n = 0
        for d in range(D):
            if (
                hasattr(cfg, "daily_shift_requirements_by_day")
                and isinstance(cfg.daily_shift_requirements_by_day, list)
                and d < len(cfg.daily_shift_requirements_by_day)
            ):
                need_map = cfg.daily_shift_requirements_by_day[d]
                # print('1', need_map)
            else:
                need_map = cfg.daily_shift_requirements
                # print('2', need_map)
            need_n = int((need_map or {}).get("N", 0) or 0)
            # print('3', need_n)
            need_n = max(0, need_n - int(fixed_cnt[d][night_idx] or 0))
            # print('4', need_n)
            total_need_n += need_n
            # print('5', total_need_n)
        # 간호사별 N 가능 여부(허용 근무유형 기반: []=제한없음, ['N']=N전담)
        n_allowed_indices: list[int] = []
        n_only_cnt = 0
        for i, nu in enumerate(roster_system.nurses):
            raw = getattr(nu, "allowed_shifts", None)
            allowed = normalize_allowed_shift_codes(raw, use_mid=bool(getattr(cfg, "use_mid", False)))
            if not allowed:
                n_allowed_indices.append(i)
                continue
            if "N" in allowed:
                n_allowed_indices.append(i)
                if allowed == {"N"}:
                    n_only_cnt += 1

        # N 용량 상한(1) 단순: 개인 월 상한 + 재직일수(입/퇴사) 클램프
        cap_basic = 0
        cap_recovery = 0
        for n in n_allowed_indices:
            T0, T1 = join[n], leave[n]
            avail_days = max(0, int(T1 - T0 + 1))
            cap_basic += min(int(cfg.max_night_shifts_per_month), avail_days)
            # 2N→2OFF hard가 켜지면, 한 사람의 N은 대략 2일 중 1일 수준(최대 0.5 비율)로 제한되는 경향이 있다.
            # 예) avail_days=30 이면 (30+1)//2 = 15 가 상한 근사치.
            cap_recovery += min(
                int(cfg.max_night_shifts_per_month), int((avail_days + 1) // 2)
            )

        # 일별 N 요구 최대값(피크 일자 확인용)
        max_daily_need_n = 0
        for d in range(D):
            if (
                hasattr(cfg, "daily_shift_requirements_by_day")
                and isinstance(cfg.daily_shift_requirements_by_day, list)
                and d < len(cfg.daily_shift_requirements_by_day)
            ):
                need_map = cfg.daily_shift_requirements_by_day[d]
            else:
                need_map = cfg.daily_shift_requirements
            need_n = int((need_map or {}).get("N", 0) or 0)
            need_n = max(0, need_n - int(fixed_cnt[d][night_idx] or 0))
            max_daily_need_n = max(max_daily_need_n, need_n)

        print(
            f"{logger_prefix} [FallbackFeasibility] "
            f"need_N(total)={total_need_n}, need_N(daily_max)={max_daily_need_n}, "
            f"N_allowed_nurses={len(n_allowed_indices)}/{N}, N_only={n_only_cnt}, "
            f"cap_N_basic≈{cap_basic}, "
            f"cap_N_2N2OFF≈{cap_recovery if cfg.two_offs_after_two_nig else 'n/a'}, "
            f"maxN={cfg.max_night_shifts_per_month}, two_offs_after_two_nig={bool(cfg.two_offs_after_two_nig)}"
        )
        if total_need_n > cap_basic:
            print(
                f"{logger_prefix} [FallbackFeasibility][WARN] "
                f"월간 N 요구({total_need_n})가 단순 상한(cap≈{cap_basic})을 초과합니다. "
                f"→ 하드 상한을 강제하면 infeasible 가능성이 큽니다."
            )
        if bool(cfg.two_offs_after_two_nig) and total_need_n > cap_recovery:
            print(
                f"{logger_prefix} [FallbackFeasibility][WARN] "
                f"2N→2OFF 기준 상한(cap≈{cap_recovery})도 초과합니다. "
                f"→ 2N→2OFF를 hard로 두면 폴백1부터 infeasible 가능성이 큽니다."
            )
        # 핵심: 일별 피크 요구 vs N 가능 인원 비교(2N→2OFF 하드가 있으면 특정 날짜에서 N 배정 가능 인원이 급감할 수 있음)
        if bool(cfg.two_offs_after_two_nig) and max_daily_need_n > len(n_allowed_indices) * 0.5:
            print(
                f"{logger_prefix} [FallbackFeasibility][WARN] "
                f"일별 N 피크 요구({max_daily_need_n})가 N 가능 인원({len(n_allowed_indices)})의 절반 이상입니다. "
                f"→ 2N→2OFF 하드 + 다른 제약(주2OFF/연속근무K 등)과 겹치면 특정 날짜에서 N 배정 불가능할 수 있습니다."
            )
        # 월 최대 OFF 상한 vs 2N→2OFF 강제 OFF 충돌 확인
        try:
            base_min_off = effective_off_days
            extra_allowed = effective_max_extra
            # 2N2O/3N2O 활성 시 자동 확장분 반영
            _noff_extra = 0
            if bool(cfg.two_offs_after_two_nig) or bool(cfg.two_offs_after_three_nig):
                _noff_extra = 2
            effective_extra = extra_allowed + _noff_extra
            max_off_allowed_per_person = base_min_off + effective_extra
            if bool(cfg.two_offs_after_two_nig) and max_off_allowed_per_person < base_min_off + 5:
                est_extra_off_from_2n2o = (
                    int(total_need_n / len(n_allowed_indices) * 0.5)
                    if n_allowed_indices
                    else 0
                )
                if est_extra_off_from_2n2o > effective_extra:
                    print(
                        f"{logger_prefix} [FallbackFeasibility][WARN] "
                        f"2N→2OFF 하드가 예상 강제 OFF({est_extra_off_from_2n2o})가 월 최대 OFF 여유({effective_extra}, "
                        f"config={extra_allowed}+자동확장={_noff_extra})를 초과할 수 있습니다. "
                        f"(min_off={base_min_off}, max_allowed={max_off_allowed_per_person}) "
                        f"→ 2N→2OFF 하드 + 월 최대 OFF 상한 하드가 충돌하여 infeasible 가능성이 큽니다."
                    )
        except Exception:
            print(f"{logger_prefix} [FallbackFeasibility] 진단 로그 실패 후 pass: {e}")
            pass
    except Exception as e:
        print(f"{logger_prefix} [FallbackFeasibility] 진단 로그 실패: {e}")
    ############################################################## build model 시작 ##############################################################
    # 모델 빌더: stage에 따라 목적 및 고정 제약 선택, 안전 위반 변수 구조도 반환
    def build_model(
        stage: int,
        coverage_eq: Optional[int] = None,
        over_le: Optional[int] = None,
        stage2_zero_locks: Optional[Dict[str, list]] = None,
        broad_soft: bool = False,
    ):
        # [S0 계측] 재빌드 비용. 단계마다 CpModel 을 새로 만들고 전 제약을 다시 쌓으므로
        #   (stage1 은 2회 시도라 한 생성에서 최대 4벌) 그 파이썬 시간이 얼마인지를
        #   S6(단일 모델 체인)의 기대 이득 판단에 쓴다. ★ 프리솔브는 매 solve 다시 도므로
        #   단일 모델화로 줄일 수 있는 것은 이 빌드 시간뿐이다.
        from time import perf_counter as _pc_bm
        _bm_t0 = _pc_bm()
        m = cp_model.CpModel()
        # per-nurse OFF cap slack 추적: post-solve 시 어느 nurse 가 슬랙을 실제로
        # 사용했는지 로그하기 위함.
        m._off_slack_lower_by_n = {}  # type: ignore[attr-defined]
        m._off_slack_upper_by_n = {}  # type: ignore[attr-defined]
        # HardAssumptionRegistry — MUS (UNSAT core) 추출 인프라.
        # 모든 hard 식을 BoolVar + OnlyEnforceIf(lit) 로 reify 하므로 모델 사이즈와
        # search branching 이 늘어나 wall-time 비용이 상당하다. INFEASIBLE 케이스가
        # 드문 운영 환경에서는 비용 대비 효용이 낮아 **기본 OFF**.
        # MUS 추출이 필요하면 `AIDE_ENABLE_MUS_REGISTRY=1` 로 명시 활성화.
        _add_hard_fb = None
        _assume_registry_fb = None
        try:
            import os as _os_fb
            if _os_fb.environ.get("AIDE_ENABLE_MUS_REGISTRY") == "1":
                from services.cp_sat.hard_assumption import HardAssumptionRegistry, add_hard as _add_hard_fb
                _assume_registry_fb = HardAssumptionRegistry(m)
                m._cpsat_assumption_registry = _assume_registry_fb  # type: ignore[attr-defined]
                print(f"[FallbackLex] AIDE_ENABLE_MUS_REGISTRY=1 — registry wrapping ON (stage={stage})")
        except Exception as _ar_fb_exc:
            print(f"[FallbackLex] HardAssumptionRegistry init failed (ignore): {_ar_fb_exc}")
            _assume_registry_fb = None
        soft_coverage = bool(getattr(cfg, "soften_daily_coverage", False))
        coverage_soft_slack = int(getattr(cfg, "coverage_soft_slack", 0) or 0)
        # 정책 (2026-05-13, HanJongjun): max coverage / M min 은 어떤 단계든 HARD 유지.
        # 무너지면 환자 안전 영향 큼 → relax 시에는 다른 hard(team/grade) 를 먼저 풀어야 함.
        # ralph 의 broad_soft 트리거는 폐기 (사용자 정책 결정 2026-05-17).
        _relax_coverage = False
        coverage_soft_weight = int(
            getattr(cfg, "coverage_soft_penalty_weight", 120000) or 120000
        )
        per_nurse_off_cap_override: dict[int, int] = {}
        # 강제 OFF 집합을 미리 구성 (프리체크에서 사용)
        # forced_off_cells: set[tuple[int, int]] = set()
        # forced_off_for_cap: set[tuple[int, int]] = set()
        # if off_idx is not None:
        #     forced_off_cells.update(
        #         (n_idx, d_idx)
        #         for (n_idx, d_idx), s_idx in fixed.items()
        #         if s_idx == off_idx
        #     )
        #     # cap 계산에서 휴가/공가는 제외
        #     vacation_types = {"휴가", "공가"}
        #     for (n_idx, d_idx), s_idx in fixed.items():
        #         if s_idx != off_idx:
        #             continue
        #         cell_type = fixed_type_by_cell.get((n_idx, d_idx))
        #         # print('이런 경우, cell_type', cell_type, s_idx)
        #         if cell_type in vacation_types:
        #             # print('이런 경우, vacation_types', cell_type)
        #             continue
        #         # off_exception_vacation_cells도 체크
        #         if (n_idx, d_idx) in off_exception_vacation_cells:
        #             continue
        #         forced_off_for_cap.add((n_idx, d_idx))
        # forced_off_cells.update(off_exception_cells)
        # # 휴가/공가는 상한 계산에서 제외
        # forced_off_for_cap.update(
        #     {
        #         (n_idx, d_idx)
        #         for (n_idx, d_idx) in off_exception_cells
        #         if (n_idx, d_idx) not in off_exception_vacation_cells
        #     }
        # )

        fixed_off_cells = {(n, d) for (n, d), s_idx in fixed.items() if s_idx == off_idx}
        fixed_vacation_off_cells = {
            (n, d)
            for (n, d), s_idx in fixed.items()
            if s_idx == off_idx and fixed_type_by_cell.get((n, d)) in {"휴가", "공가"}
        }
        fixed_non_off_cells = {(n, d) for (n, d), s_idx in fixed.items() if s_idx != off_idx}
        partition = build_off_partitions(
            nurses=roster_system.nurses,
            num_days=D,
            first_day=first_day,
            fixed_off_cells=fixed_off_cells,
            fixed_vacation_off_cells=fixed_vacation_off_cells,
            off_exception_cells=off_exception_cells,
            off_exception_vacation_cells=off_exception_vacation_cells,
            weekly_off_by_idx=None,
            weekend_off_only_enable=bool(cfg.weekend_off_only_enable),
            include_off_exception_cells=False,
            include_weekly_off_cells=False,
            include_weekend_off_cells=True,
            weekend_within_active_range=True,
            join=join,
            leave=leave,
            fixed_non_off_cells=fixed_non_off_cells,
        )
        structural_off_cells = set(partition["structural_off_cells"])
        vacation_off_cells = set(partition["vacation_off_cells"])
        weekend_days = set(partition["weekend_days"])

        # weekly_off_by_idx, cross-month 등
        # 👉 여기서만 structural_off_cells에 추가
        if stage == 1 and not broad_soft:
            try:
                mapping_logs = []
                for idx, nu in enumerate(roster_system.nurses):
                    
                    mapping_logs.append(
                        f"{idx}:{getattr(nu, 'nurse_id', '?')}/"    # nurse_id가 없음
                        f"{getattr(nu, 'name', '?')}/"
                        f"{getattr(nu, 'account_id', '?')}"
                    )
                print("[NurseIndexMap] " + ", ".join(mapping_logs))
                if off_exception_cells:
                    exc_map = {}
                    for n_idx, d_idx in off_exception_cells:
                        exc_map.setdefault(n_idx, []).append(d_idx + 1)
                    exc_logs = []
                    for n_idx, days in sorted(exc_map.items()):
                        nu = roster_system.nurses[n_idx]
                        exc_logs.append(
                            f"{n_idx}:{getattr(nu, 'nurse_id', '?')}/"
                            f"{getattr(nu, 'name', '?')}/"
                            f"{getattr(nu, 'account_id', '?')} -> {sorted(days)}"
                        )
                    print("[OffExceptionCells] " + "; ".join(exc_logs))
            except Exception:
                pass
        Xv = {}

        _false_var = m.NewConstant(0)
        def X(n, d, s):
            return Xv.get((n, d, s), _false_var)

        def is_pure_o(n: int, d: int):
            """휴가/공가(예외 휴무) 좌표는 제외한 순수 O만 반환합니다."""
            if (n, d) in vac_cells:
                return 0
            return X(n, d, off_idx)

        # ── 하드 모순 사전 점검: 커버리지 cap / 강제 OFF 상한 ──
        try:
            # (1) 교대별 최대 가능 인원(cap) 대비 need 초과 여부
            if hasattr(cfg, "daily_shift_requirements") and cfg.daily_shift_requirements:
                forbidden = initial_forbidden if initial_forbidden else {}
                shift_allow_map = getattr(roster_system, "shift_codes_by_nurse", None)
                # print('hahaha, shift_allow_map', shift_allow_map)       # None
                for d in range(D):
                    if (
                        hasattr(cfg, "daily_shift_requirements_by_day")
                        and isinstance(cfg.daily_shift_requirements_by_day, list)
                        and d < len(cfg.daily_shift_requirements_by_day)
                    ):
                        need_map = cfg.daily_shift_requirements_by_day[d]
                    else:
                        need_map = cfg.daily_shift_requirements
                    for code, req in (need_map or {}).items():
                        if code not in roster_system.config.shift_types:
                            continue
                        s_idx = roster_system.config.shift_types.index(code)
                        need = int(req) - fixed_cnt[d][s_idx]
                        if need <= 0:
                            continue
                        cap = 0
                        blocked = {
                            "forced_off": 0,
                            "weekend_off_only": 0,
                            "forbidden": 0,
                            "not_allowed": 0,
                        }
                        for n in range(N):
                            if not (join[n] <= d <= leave[n]):
                                continue
                            if (n, d) in fixed:
                                # print('고정:', fixed[(n, d)])
                                continue  # 다른 교대로 이미 고정
                            if (n, d) in structural_off_cells:
                                blocked["forced_off"] += 1
                                continue
                            if (
                                d in weekend_days
                                and getattr(cfg, "weekend_off_only_enable", True)
                                and bool(getattr(roster_system.nurses[n], "is_weekend_off", False))
                                and s_idx != off_idx
                            ):
                                blocked["weekend_off_only"] += 1
                                # print('weekend_off_only', roster_system.nurses[n] )
                                continue
                            if (n, d) in forbidden:
                                forbid_codes = [
                                    c
                                    for c in forbidden[(n, d)]
                                    if c in roster_system.config.shift_types
                                ]
                                forbid_idx = {
                                    roster_system.config.shift_types.index(c) for c in forbid_codes
                                }
                                if s_idx in forbid_idx:
                                    blocked["forbidden"] += 1
                                    continue
                            if shift_allow_map and isinstance(shift_allow_map, dict):
                                allowed_codes = shift_allow_map.get(n, None)
                                if allowed_codes and roster_system.config.shift_types[s_idx] not in allowed_codes:
                                    blocked["not_allowed"] += 1
                                    continue
                            cap += 1
                        if need > cap:
                            print(
                                f"[HardCheck] day={d+1}, shift={code}, need={need}, cap={cap}, blocked={blocked}"
                            )

            # (2) 강제/고정 OFF로 개인 OFF 상한 초과 여부
            try:
                for n in range(N):
                    T0, T1 = join[n], leave[n]
                    _blocked_set = blocked_by_nurse.get(n, set()) if blocked_by_nurse else set()
                    _n_blocked = len(_blocked_set)
                    avail_days = T1 - T0 + 1 - _n_blocked
                    forced_off_cnt = sum(
                        1
                        for d in range(T0, T1 + 1)
                        if (n, d) in structural_off_cells and d not in _blocked_set
                    )
                    nu = roster_system.nurses[n]
                    raw = getattr(nu, "allowed_shifts", None)
                    is_n_only = is_n_only_profile(raw, use_mid=bool(getattr(cfg, "use_mid", False)))
                    # 디버그: 강제 OFF 개수 로그
                    print(
                        f"{logger_prefix} [HardCheck][ForcedOffCnt] "
                        f"nurse_idx={n}, id={getattr(nu, 'nurse_id', '?')}, "
                        f"name={getattr(nu, 'name', '?')}, forced_off_cnt={forced_off_cnt}, "
                        f"avail_days={avail_days}"
                    )
                    if is_n_only:
                        # off-cap = avail - 실효 N상한(min(글로벌 max_night, n_max/n_exact)).
                        # 활성 cap(아래 total_cap_effective)과 동일 공식 유지.
                        _global_mn = int(getattr(cfg, "max_night_shifts_per_month", 15) or 15)
                        max_off_allowed = max(0, avail_days - effective_night_cap(nu, _global_mn))
                        # print(f'is_n_only, 간호사 n: {n}, max_off_allowed: {max_off_allowed}')
                    else:
                        vacation_cnt = sum(
                            1 for d in range(T0, T1 + 1) if (n, d) in vacation_off_cells
                        )
                        weekend_slots_nonvac = sum(
                            1
                            for d in weekend_days
                            if T0 <= d <= T1 and (n, d) not in vacation_off_cells
                        )
                        off_bounds = compute_off_bounds(
                            source=cfg,
                            avail_days=avail_days,
                            vacation_cnt=vacation_cnt,
                            reference_days=D_phys,
                            weekend_only=bool(getattr(nu, "is_weekend_off", False)),
                            weekend_slots_nonvac=weekend_slots_nonvac,
                        )
                        max_off_allowed = int(off_bounds["max_off_allowed"])
                        # print(f'not is_n_only, 간호사 n: {n}, max_off_allowed: {max_off_allowed}')
                    if forced_off_cnt > max_off_allowed:
                        # print(f'forced_off_cnt > max_off_allowed, 간호사 n: {n}, forced_off_cnt: {forced_off_cnt}, max_off_allowed: {max_off_allowed}')
                        per_nurse_off_cap_override[n] = forced_off_cnt
                        forced_days = [
                            d_idx + 1
                            for d_idx in range(T0, T1 + 1)
                            if (n, d_idx) in structural_off_cells
                        ]
                        nurse_id = getattr(nu, "nurse_id", "?")
                        nurse_name = getattr(nu, "name", "?")
                        account_id = getattr(nu, "account_id", "?")
                        print(
                            "[HardCheck] "
                            f"nurse_idx={n}, nurse_id={nurse_id}, name={nurse_name}, "
                            f"account_id={account_id}, forced_off={forced_off_cnt}, "
                            f"max_off_allowed={max_off_allowed}, forced_off_days={forced_days} "
                            "→ OFF 상한 초과(모순 가능)"
                        )
            except Exception as e:
                print(f"{logger_prefix} [HardCheck] 강제 OFF 상한 초과 여부 실패: {e}")
                pass
        except Exception as exc:
            print(f"{logger_prefix} [HardCheck] precheck 실패: {exc}")

        for n in range(N):
            for d in iter_nurse_days(n, join, leave, blocked_by_nurse):
                for s in range(S):
                    Xv[n, d, s] = m.NewBoolVar(f"x_{n}_{d}_{s}")
        active_days = build_active_days(N, join, leave, blocked_by_nurse)
        # 고정 셀
        for (n, d), s_idx in fixed.items():
            _fixed_source = fixed_source_by_cell.get((n, d), "manual")
            _fixed_pattern = _fixed_pattern_from_source(_fixed_source)
            if (n, d) not in active_days:
                continue
            # day-aware: 팔로우 day의 프리셉티 고정셀만 스킵(팔로우가 지배). primary(cp_sat_basic:3089)와 동일.
            # day-less 는 authoritative 에서 항상 False→고정셀이 hard로 적용돼 팔로우와 모순(INFEASIBLE) 유발.
            if _is_preceptee_at(n, d):
                continue
            _fixed_expr = (X(n, d, s_idx) == 1)
            if _assume_registry_fb is not None and _add_hard_fb is not None:
                _add_hard_fb(
                    m,
                    _assume_registry_fb,
                    name=f"FixedCell:nurse_{n}:day_{d}",
                    constraint_expr=_fixed_expr,
                    meta={
                        "node_id": f"fixed_cell:nurse_{n}:day_{d}",
                        "type": "FixedWantedNode",
                        "label": "fixed_assignment",
                        "value": {"day": d + 1, "shift_idx": int(s_idx)},
                        "fixed_source": _fixed_source,
                        "scope": "nurse",
                        "scope_key": f"nurse_{n}",
                        "pattern": _fixed_pattern,
                        "nurse_id": str(getattr(roster_system.nurses[n], "nurse_id", n)),
                        "human_message_ko": f"{d + 1}일 고정 근무를 유지해야 합니다.",
                        "resolution_hint": "해당 날짜 고정 근무를 해제하거나 충돌 정책을 완화하세요.",
                    },
                )
            else:
                m.Add(_fixed_expr)
            for s in range(S):
                if s != s_idx:
                    _ban_expr = (X(n, d, s) == 0)
                    if _assume_registry_fb is not None and _add_hard_fb is not None:
                        _add_hard_fb(
                            m,
                            _assume_registry_fb,
                            name=f"FixedCellBan:nurse_{n}:day_{d}",
                            constraint_expr=_ban_expr,
                            meta={
                                "node_id": f"fixed_cell_ban:nurse_{n}:day_{d}",
                                "type": "FixedWantedNode",
                                "label": "fixed_assignment_exclusive",
                                "value": {"day": d + 1, "fixed_shift_idx": int(s_idx)},
                                "fixed_source": _fixed_source,
                                "scope": "nurse",
                                "scope_key": f"nurse_{n}",
                                "pattern": _fixed_pattern,
                                "nurse_id": str(getattr(roster_system.nurses[n], "nurse_id", n)),
                                "human_message_ko": f"{d + 1}일 고정 근무와 다른 시프트는 금지됩니다.",
                                "resolution_hint": "고정 근무 또는 다른 하드 제약 중 하나를 조정하세요.",
                            },
                        )
                    else:
                        m.Add(_ban_expr)
        # W(특별 근무)는 고정 셀 외에는 전부 금지
        if has_w and w_idx is not None:
            for n in range(N):
                if _is_preceptee_at(n):
                    continue
                for d in iter_nurse_days(n, join, leave, blocked_by_nurse):
                    if (n, d) in fixed and fixed[(n, d)] == w_idx:
                        continue
                    _w_ban_expr = (X(n, d, w_idx) == 0)
                    if _assume_registry_fb is not None and _add_hard_fb is not None:
                        _add_hard_fb(
                            m,
                            _assume_registry_fb,
                            name=f"SpecialShiftBanW:nurse_{n}:day_{d}",
                            constraint_expr=_w_ban_expr,
                            meta={
                                "node_id": f"special_shift_ban_w:nurse_{n}:day_{d}",
                                "type": "ForbiddenCellNode",
                                "label": "ban_unfixed_w_shift",
                                "value": {"day": d + 1, "shift": "W"},
                                "scope": "nurse",
                                "scope_key": f"nurse_{n}",
                                "pattern": "forbidden_shift",
                                "nurse_id": str(getattr(roster_system.nurses[n], "nurse_id", n)),
                                "human_message_ko": "고정되지 않은 W(특별 근무)는 배정할 수 없습니다.",
                                "resolution_hint": "W 배정이 필요하면 해당 셀을 고정 근무로 지정하세요.",
                            },
                        )
                    else:
                        m.Add(_w_ban_expr)
        # 순수 O 4연속 금지 (fixed로 이미 4O면 경고만 남기고 스킵)
        # cfg.skip_4o_hard_first_days: 월초 N일 구간에서는 4O Hard 미적용 (기본 3)
        # cfg.enforce_4o_hard=False 또는 env ROSTER_DISABLE_4O_HARD=1 이면 4O hard 전체 비활성(테스트/완화).
        import os as _os_4o_env_fb
        _enforce_4o_hard_eff_fb = bool(getattr(cfg, "enforce_4o_hard", True))
        if _os_4o_env_fb.environ.get("ROSTER_DISABLE_4O_HARD"):
            _enforce_4o_hard_eff_fb = False
        if not _enforce_4o_hard_eff_fb:
            print(f"{logger_prefix} [4O-hard] DISABLED (cfg.enforce_4o_hard={getattr(cfg, 'enforce_4o_hard', True)}, env={_os_4o_env_fb.environ.get('ROSTER_DISABLE_4O_HARD')})")
        if off_idx is not None and _enforce_4o_hard_eff_fb:
            vac_cells = set(off_exception_vacation_cells)
            skip_4o_hard_first_days = int(getattr(cfg, "skip_4o_hard_first_days", 3) or 0)
            for n in range(N):
                if _is_preceptee_at(n):
                    continue
                for d in range(join[n], leave[n] - 2):
                    if d + 3 > leave[n]:
                        continue
                    if any((n, d+k) not in active_days for k in range(4)):
                        continue
                    # if skip_4o_hard_first_days > 0 and d < skip_4o_hard_first_days:
                    #     continue
                    fixed_o_cnt = sum(
                        1
                        for (fn, fd), fs_idx in fixed.items()
                        if fn == n
                        and fd in {d, d + 1, d + 2, d + 3}
                        and fs_idx == off_idx
                        and (fn, fd) not in vac_cells
                    )
                    # print('fixed.items()', fixed.items())
                    # print('이미 있음 fixed_o_cnt', fixed_o_cnt)
                    if fixed_o_cnt >= 4:
                        print(
                            f"{logger_prefix} [4O-skip-fixed] nurse_idx={n}, days={d+1},{d+2},{d+3},{d+4} (fixed O x{fixed_o_cnt})"
                        )
                        continue
                    m.Add(
                        is_pure_o(n, d)
                        + is_pure_o(n, d + 1)
                        + is_pure_o(n, d + 2)
                        + is_pure_o(n, d + 3)
                        <= 3
                    )
        # ── 4O 월경계 제약: 전월 꼬리 연속 OFF + 현월 초 연속 OFF 합산 4 이상 금지 (하드) ──
        _4o_cross_affected_fb: set[int] = set()
        prev_off_tail = getattr(roster_system, "prev_month_off_tail_by_idx", {}) or {}
        print(f"{logger_prefix} [4O-cross-month-debug] prev_off_tail_by_idx keys={list(prev_off_tail.keys())}, "
              f"values={dict(prev_off_tail)}, N={N}")
        for n in range(N):
            if not _enforce_4o_hard_eff_fb:
                break
            if _is_preceptee_at(n):
                continue
            t = prev_off_tail.get(n, 0)
            if t <= 0 or t >= 4:
                continue
            if join[n] > 0:
                continue
            need = 4 - t
            window_days = list(range(0, min(need, leave[n] + 1)))
            if len(window_days) < need:
                continue
            free_vars = []
            effective_t = t
            _detail_per_day = []
            for wd in window_days:
                in_structural = (n, wd) in structural_off_cells
                in_fixed_off = (n, wd) in fixed and fixed[(n, wd)] == off_idx
                is_fixed_off = in_structural or in_fixed_off
                if is_fixed_off:
                    effective_t += 1
                    _detail_per_day.append(f"day{wd}=고정OFF(struct={in_structural},fixed={in_fixed_off})")
                else:
                    free_vars.append(is_pure_o(n, wd))
                    _detail_per_day.append(f"day{wd}=free")
            if effective_t >= 4:
                nu = roster_system.nurses[n] if n < len(roster_system.nurses) else None
                print(f"{logger_prefix} [4O-cross-month-SKIP] nurse_idx={n}, "
                      f"name={getattr(nu, 'name', '?')}, prev_tail={t}, "
                      f"effective_t={effective_t}>=4 → 제약 스킵 (이미 4O 불가피), "
                      f"detail={_detail_per_day}")
                continue
            if not free_vars:
                continue
            remaining = 3 - effective_t
            m.Add(sum(free_vars) <= remaining)
            _4o_cross_affected_fb.add(n)
            nu = roster_system.nurses[n] if n < len(roster_system.nurses) else None
            print(
                f"{logger_prefix} [4O-cross-month] nurse_idx={n}, "
                f"name={getattr(nu, 'name', '?')}, prev_tail={t}, "
                f"고정OFF={effective_t - t}, free={len(free_vars)}, OFF<={remaining}, "
                f"detail={_detail_per_day}"
            )
        # 주말 휴무 제약: is_weekend_off=True인 간호사는 주말(토/일)은 기본적으로 OFF를 강제하고,
        # 평일(월~금)에는 OFF를 금지한다.
        #
        # 예외:
        # - 특정 날짜가 '고정 셀(fixed_cells)'로 이미 근무(D/E/N/W 등)로 지정된 경우,
        #   기존 고정이 우선이며 주말 OFF 강제를 덮어쓰지 않는다.
        if getattr(cfg, "weekend_off_only_enable", True):
            if stage == 1:
                _log_weekend_off_enforcement(
                    roster_system=roster_system,
                    join=join,
                    leave=leave,
                    weekend_days=weekend_days,
                    fixed=fixed,
                    off_idx=off_idx,
                    logger_prefix=logger_prefix,
                )
            for n, nu in enumerate(roster_system.nurses):
                if _is_preceptee_at(n):
                    continue
                if not bool(getattr(nu, "is_weekend_off", False)):
                    continue
                for d in iter_nurse_days(n, join, leave, blocked_by_nurse):
                    if d in weekend_days:
                        # 주말(토/일): 기본 OFF 강제
                        # 단, 고정 셀이 근무로 지정되어 있으면(예: 특수 근무/교육 등) 고정이 우선이다.
                        if (n, d) in fixed and fixed[(n, d)] != off_idx:
                            try:
                                fixed_code = cfg.shift_types[fixed[(n, d)]]
                            except Exception:
                                fixed_code = str(fixed.get((n, d)))
                            print(
                                f"{logger_prefix} [WeekendOff] 주말 OFF 강제 스킵(고정 우선): "
                                f"nurse_index={n}, day={d+1}, fixed_shift={fixed_code}"
                            )
                            continue
                        m.Add(X(n, d, off_idx) == 1)
                    else:
                        # 평일(월~금): OFF 금지(D/E/N만 가능)
                        # 단, 사용자 고정 OFF는 예외로 허용하고 별도 제약을 걸지 않는다.
                        if (n, d) in fixed and fixed[(n, d)] == off_idx:
                            continue
                        if d <= 1 and getattr(roster_system, "prev_month_n_tail_by_idx", {}).get(n, 0) >= 2:
                            continue
                        # off_window 범위 내 평일: 전월 꼬리 연속근무 보정을 위해 OFF 허용 필요
                        _ow_ranges_fb = (getattr(roster_system, "off_window_constraints", {}) or {}).get(n, []) or []
                        if any(ws <= d <= we for (ws, we) in _ow_ranges_fb):
                            continue
                        m.Add(X(n, d, off_idx) == 0)

        # raw_off_placement_mode = int(getattr(cfg, "off_placement_mode", 0) or 0)
        # if raw_off_placement_mode != 0:
        #     print(f"{logger_prefix} [OffPlacementMode] deprecated: forcing off_placement_mode=0")
        # off_placement_mode = 0
        weekly_off_by_idx = (
            getattr(roster_system, "weekly_off_by_idx", {})
            if isinstance(getattr(roster_system, "weekly_off_by_idx", {}), dict)
            else {}
        )
        prev_month_last_is_off = (
            getattr(roster_system, "prev_month_last_is_off", {})
            if isinstance(getattr(roster_system, "prev_month_last_is_off", {}), dict)
            else {}
        )
        prev_month_n_tail_by_idx = (
            getattr(roster_system, "prev_month_n_tail_by_idx", {})
            if isinstance(getattr(roster_system, "prev_month_n_tail_by_idx", {}), dict)
            else {}
        )
        # print('hahaha, weekly_off_by_idx', weekly_off_by_idx)
        # print('hahaha, prev_month_last_is_off', prev_month_last_is_off)
        # forced_off_cells: set[tuple[int, int]] = set(
        #     (n_idx, d_idx) for (n_idx, d_idx), s_idx in fixed.items() if s_idx == off_idx
        # )
        # forced_off_cells.update(off_exception_cells)
        # 예상 커버리지 부족일 계산(단순 근사): 필요한 총 인원 > (활성 인원 - 고정 OFF)
        shortage_days: set[int] = set()
        try:
            for d in range(D):
                if (
                    hasattr(cfg, "daily_shift_requirements_by_day")
                    and isinstance(cfg.daily_shift_requirements_by_day, list)
                    and d < len(cfg.daily_shift_requirements_by_day)
                ):
                    need_map = cfg.daily_shift_requirements_by_day[d]
                else:
                    need_map = cfg.daily_shift_requirements
                total_need = sum(int(v) for v in (need_map or {}).values())
                active_cnt = sum(1 for n in range(N) if join[n] <= d <= leave[n])
                fixed_off_cnt = fixed_cnt[d][off_idx] if off_idx is not None else 0
                avail_eff = max(0, active_cnt - fixed_off_cnt)
                if avail_eff < total_need:
                    shortage_days.add(d)
        except Exception:
            shortage_days = set()
        # if off_placement_mode > 0 and weekly_off_by_idx:
        #     for n, day_list in weekly_off_by_idx.items():
        #         if n >= len(join):
        #             continue
        #         if _is_preceptee_at(n):
        #             continue
        #         T0, T1 = join[n], leave[n]
        #         for d_raw in day_list or []:
        #             try:
        #                 d = int(d_raw)
        #             except Exception:
        #                 continue
        #             if d < T0 or d > T1:
        #                 continue
        #             if d == D - 1:
        #                 continue
        #             if d == 0:
        #                 if bool(prev_month_last_is_off.get(n, False)):
        #                     continue
        #                 if d + 1 <= T1:
        #                     m.Add(X(n, d + 1, off_idx) == 1)
        #                     structural_off_cells.add((n, d + 1))
        #                 continue
        #             if off_placement_mode == 1:
        #                 neighbours = []
        #                 left_pos = d - 1
        #                 right_pos = d + 1
        #                 # if left_pos >= T0 and left_pos not in shortage_days:
                #                 allow_shortage_off = broad_soft

        #                 if left_pos >= T0 and (allow_shortage_off or left_pos not in shortage_days):
        #                     neighbours.append(("left", X(n, left_pos, off_idx)))
        #                 # if right_pos <= T1 and right_pos not in shortage_days:
        #                 if right_pos <= T1 and (allow_shortage_off or right_pos not in shortage_days):
        #                     neighbours.append(("right", X(n, right_pos, off_idx)))
        #                 # 둘 다 부족일이면 스킵
        #                 if not neighbours:
        #                     continue
        #                 vars_only = [v for _, v in neighbours]
        #                 if len(vars_only) == 1:
        #                     m.Add(vars_only[0] == 1)
        #                 else:
        #                     m.Add(sum(vars_only) >= 1)
        #                 for direction, _var in neighbours:
        #                     if direction == "left":
        #                         structural_off_cells.add((n, left_pos))
        #                     else:
        #                         structural_off_cells.add((n, right_pos))
        #             else:
        #                 left_pos = d - 1
        #                 right_pos = d + 1
        #                 placed = False
        #                 if left_pos >= T0 and left_pos not in shortage_days:
        #                     m.Add(X(n, left_pos, off_idx) == 1)
        #                     structural_off_cells.add((n, left_pos))
        #                     placed = True
        #                 elif right_pos <= T1 and right_pos not in shortage_days:
        #                     m.Add(X(n, right_pos, off_idx) == 1)
        #                     structural_off_cells.add((n, right_pos))
        #                     placed = True
        #                 # 둘 다 부족일이면 스킵 (커버리지 우선)
        #                 if not placed:
        #                     continue

        # 초기 금지: 고정과 충돌하면 금지 무시(로그만)
        try:
            if initial_forbidden:
                for (n, d), code_list in initial_forbidden.items():
                    if _is_preceptee_at(n):
                        continue
                    for code in (code_list or []):
                        if code not in roster_system.config.shift_types:
                            continue
                        s_idx = roster_system.config.shift_types.index(code)
                        if (n, d) not in active_days:
                            continue
                        if (n, d) in fixed:
                            # 유저 고정 셀 우선: 해당 날 전체 금지 무시
                            continue
                        _if_expr = (X(n, d, s_idx) == 0)
                        if _assume_registry_fb is not None and _add_hard_fb is not None:
                            _add_hard_fb(
                                m,
                                _assume_registry_fb,
                                name=f"InitialForbidden:nurse_{n}:day_{d}",
                                constraint_expr=_if_expr,
                                meta={
                                    "node_id": f"initial_forbidden:nurse_{n}:day_{d}",
                                    "type": "ForbiddenCellNode",
                                    "label": "initial_forbidden_shift",
                                    "value": {"day": d + 1, "shift": code},
                                    "scope": "nurse",
                                    "scope_key": f"nurse_{n}",
                                    "pattern": "initial_forbidden",
                                    "nurse_id": str(getattr(roster_system.nurses[n], "nurse_id", n)),
                                    "human_message_ko": f"초기 금지 규칙에 의해 {d + 1}일 {code} 배정이 금지됩니다.",
                                    "resolution_hint": "초기 금지 규칙을 해제하거나 다른 하드 제약을 조정하세요.",
                                },
                            )
                        else:
                            m.Add(_if_expr)
        except Exception as e:
            print(f"{logger_prefix} 초기 금지 셀 적용 중 오류: {e}")

        # exactly-one
        for n in range(N):
            for d in iter_nurse_days(n, join, leave, blocked_by_nurse):
                if (n, d) in fixed and not _is_preceptee_at(n, d):
                    continue
                m.AddExactlyOne(X(n, d, s) for s in range(S))

        # 프리셉티 팔로우 제약 (fallback) — assignment 기간 내에만 적용
        if preceptee_follow and preceptee_indices:
            _fb_id_map = {nu.db_id: n for n, nu in enumerate(roster_system.nurses)}
            _fb_pre_ptr_idx = getattr(roster_system, 'preceptee_preceptor_idx', {}) or {}  # period SSOT
            # Option C: 프리셉티 1급 시민화 — 등가는 (a)프리셉티 fixed일 제외 (b)프리셉터 특수코드일엔 OFF
            _pte_std = {'D', 'E', 'N', 'O'} | ({'M'} if mid_idx is not None else set())
            _pte_orig_map = getattr(roster_system, '_fixed_original_shift_map', {}) or {}
            _pte_work_sub = {str(x).upper() for x in (getattr(roster_system, '_work_sub_ids', set()) or set())}
            _pte_fw_map = getattr(roster_system, '_preceptee_fixed_wanted_map', {}) or {}
            for n in sorted(preceptee_indices):
                nu = roster_system.nurses[n]
                # 권위 모드: period SSOT 로 프리셉터 결정(캐시 미사용 — NULL 캐시 프리셉티도 solve 반영).
                if _has_preceptee_period:
                    p = _fb_pre_ptr_idx.get(n)
                    if p is None:
                        continue
                else:
                    pid = getattr(nu, 'preceptor_id', None)
                    if not pid or pid not in _fb_id_map:
                        continue
                    p = _fb_id_map[pid]
                d_start = max(join[n], join[p])
                d_end = min(leave[n], leave[p])
                for d in range(d_start, d_end + 1):
                    if not _is_preceptee_at(n, d):
                        continue
                    # (a) 프리셉티 fixed일: 등가 제외(본인 고정값은 아래 하드고정 블록이 처리)
                    if (n, d) in _pte_fw_map:
                        continue
                    # (b) 프리셉터가 비표준 fixed코드(휴가/공가/W 등)면 등가 대신 프리셉티 OFF
                    _p_special = False
                    if (p, d) in fixed:
                        _p_orig = _pte_orig_map.get((p, d))
                        if _p_orig:
                            _pou = str(_p_orig).upper()
                            _p_special = (_pou not in _pte_std and _pou not in _pte_work_sub)
                        else:
                            _p_special = fixed[(p, d)] not in (day_idx, eve_idx, night_idx, off_idx, mid_idx)
                    if _p_special:
                        _xo = X(n, d, off_idx)
                        if not isinstance(_xo, int):
                            m.Add(_xo == 1)
                        continue
                    for s in range(S):
                        xn = X(n, d, s)
                        xp = X(p, d, s)
                        if isinstance(xn, int) or isinstance(xp, int):
                            continue
                        m.Add(xn == xp)
            # Option C: 프리셉티 fixed_wanted 프리솔브 하드고정 → fixed일 인접을 솔버가 조율/불가보고
            for (_pte_n, _pte_d), _pte_code in _pte_fw_map.items():
                if _pte_n not in preceptee_indices:
                    continue
                if not (join[_pte_n] <= _pte_d <= leave[_pte_n]):
                    continue
                _cu = str(_pte_code).strip().upper()
                if _cu not in roster_system.config.shift_types:
                    continue
                _ci = roster_system.config.shift_types.index(_cu)
                _xv = X(_pte_n, _pte_d, _ci)
                if not isinstance(_xv, int):
                    m.Add(_xv == 1)

        # DEN 커버리지에서 프리셉티 제외 시 fixed_cnt 보정
        if exclude_preceptee_from_den:
            _fb_fixed_cnt_adj = [[0] * S for _ in range(D)]
            for (n2, d2), s_idx in fixed.items():
                if n2 not in preceptee_indices:
                    _fb_fixed_cnt_adj[d2][s_idx] += 1
        else:
            _fb_fixed_cnt_adj = fixed_cnt

        m_bucket_indices = compute_main_bucket_indices(
            roster_system.config.shift_types,
            target_main="M",
            code2main=code2main,
            shift_id_to_main_map=shift_id_to_main_map,
        )

        # 1) 커버리지 등식: assigned + short - over == need (날짜별 요구치 적용)
        _fb_max_by_day = getattr(cfg, "daily_shift_requirements_max_by_day", None)
        _fb_has_any_max = isinstance(_fb_max_by_day, list) and any(
            any(int(v or 0) > 0 for v in dm.values())
            for dm in _fb_max_by_day if isinstance(dm, dict)
        )
        # off_first=True: max coverage 미설정 코드/일에 대해 min을 max로 강제(=잔여 셀 OFF 회수)
        _fb_off_first_cfg = bool(getattr(cfg, "off_first", False))
        print(f"{logger_prefix} [OffFirstCoverage] off_first={_fb_off_first_cfg}, _fb_has_any_max={_fb_has_any_max} → force_min_as_max={_fb_off_first_cfg and not _fb_has_any_max}")
        short_terms, over_terms = [], []
        over_vars_by_day = {}
        short_vars_by_day_code: Dict[tuple[int, str], cp_model.IntVar] = {}
        over_vars_by_day_code: Dict[tuple[int, str], cp_model.IntVar] = {}
        zero_demand_block_codes = {"D", "E", "N", "M"}
        _fb_daily_assigned_by_code: dict[str, list] = {}  # 일자별 커버리지 균등화용
        for d in range(D):
            if (
                hasattr(cfg, "daily_shift_requirements_by_day")
                and isinstance(cfg.daily_shift_requirements_by_day, list)
                and d < len(cfg.daily_shift_requirements_by_day)
            ):
                need_map = cfg.daily_shift_requirements_by_day[d]
            else:
                need_map = cfg.daily_shift_requirements
            need_max_map = _fb_max_by_day[d] if isinstance(_fb_max_by_day, list) and d < len(_fb_max_by_day) else None
            for code, req in need_map.items():
                if code not in roster_system.config.shift_types:
                    continue
                s = roster_system.config.shift_types.index(code)
                req_raw = max(0, int(req or 0))
                need = req_raw - _fb_fixed_cnt_adj[d][s]
                req_max_raw = int((need_max_map or {}).get(code, 0) or 0)
                need_max = max(0, req_max_raw - _fb_fixed_cnt_adj[d][s]) if req_max_raw > 0 else 0
                assigned = sum(
                    X(n, d, s)
                    for n in range(N)
                    if join[n] <= d <= leave[n] and (n, d) not in fixed
                    and (not exclude_preceptee_from_den or not _is_preceptee_at(n, d))
                    and (n, d) not in coverage_exclude_cells
                )
                if code == "M":
                    if m_bucket_indices:
                        assigned_m_bucket = sum(
                            X(n, d, s2)
                            for n in range(N)
                            if join[n] <= d <= leave[n]
                            and (n, d) not in fixed
                            and (not exclude_preceptee_from_den or not _is_preceptee_at(n, d))
                            and (n, d) not in coverage_exclude_cells
                            for s2 in m_bucket_indices
                        )
                    else:
                        assigned_m_bucket = assigned
                    fixed_m_bucket = (
                        sum(int(_fb_fixed_cnt_adj[d][s2] or 0) for s2 in m_bucket_indices)
                        if m_bucket_indices
                        else int(_fb_fixed_cnt_adj[d][s] or 0)
                    )
                    if req_raw == 0:
                        m.Add(assigned_m_bucket == 0)
                        sh = m.NewIntVar(0, 0, f"short_{d}_{code}")
                        ov = m.NewIntVar(0, 0, f"over_{d}_{code}")
                        short_terms.append(sh)
                        over_terms.append(ov)
                        over_vars_by_day.setdefault(d, {})[code] = ov
                        short_vars_by_day_code[(d, code)] = sh
                        over_vars_by_day_code[(d, code)] = ov
                        continue
                    m_need = max(0, int(req_raw - fixed_m_bucket))
                    m_cap_max = max(0, int(req_max_raw - fixed_m_bucket)) if req_max_raw > 0 else 0
                    # M min coverage: max coverage 있으면 hard, 없으면 soft
                    # _relax_coverage 활성 시: 항상 soft (인원 부족 대응)
                    if _fb_has_any_max and not _relax_coverage and m_need > 0:
                        m.Add(assigned_m_bucket >= m_need)
                        sh = m.NewIntVar(0, 0, f"short_{d}_{code}")
                    else:
                        sh = m.NewIntVar(0, m_need if m_need > 0 else 0, f"short_{d}_{code}")
                        if m_need > 0:
                            m.Add(assigned_m_bucket + sh >= m_need)
                    # M 상한: max coverage 있으면 hard, 없으면 min으로 hard cap
                    # _relax_coverage 활성 시: max도 soft
                    if m_cap_max > 0 and not _relax_coverage:
                        m.Add(assigned_m_bucket <= m_cap_max)
                        ov = m.NewIntVar(0, 0, f"over_{d}_{code}")
                    elif m_cap_max > 0 and _relax_coverage:
                        ov = m.NewIntVar(0, N, f"over_{d}_{code}")
                        m.Add(ov >= assigned_m_bucket - m_cap_max)
                    else:
                        m_cap_non_fixed = max(0, int(req_raw - fixed_m_bucket))
                        m.Add(assigned_m_bucket <= m_cap_non_fixed)
                        ov = m.NewIntVar(0, 0, f"over_{d}_{code}")
                    short_terms.append(sh)
                    over_terms.append(ov)
                    over_vars_by_day.setdefault(d, {})[code] = ov
                    short_vars_by_day_code[(d, code)] = sh
                    over_vars_by_day_code[(d, code)] = ov
                    continue
                if code in zero_demand_block_codes and req_raw == 0:
                    m.Add(assigned == 0)
                    sh = m.NewIntVar(0, 0, f"short_{d}_{code}")
                    ov = m.NewIntVar(0, 0, f"over_{d}_{code}")
                    short_terms.append(sh)
                    over_terms.append(ov)
                    over_vars_by_day.setdefault(d, {})[code] = ov
                    short_vars_by_day_code[(d, code)] = sh
                    over_vars_by_day_code[(d, code)] = ov
                    continue
                # min 제약: assigned + shortage >= need
                if need <= 0:
                    sh = m.NewIntVar(0, 0, f"short_{d}_{code}")
                else:
                    sh = m.NewIntVar(0, N, f"short_{d}_{code}")
                    m.Add(assigned + sh >= need)
                # max 제약: hard (상한 초과 불가), _relax_coverage 시 soft
                if need_max > 0 and d < D_phys:
                    _fb_daily_assigned_by_code.setdefault(code, []).append((d, assigned, need))
                if need_max > 0 and not _relax_coverage:
                    m.Add(assigned <= need_max)
                    ov = m.NewIntVar(0, 0, f"over_{d}_{code}")
                elif need_max > 0 and _relax_coverage:
                    ov = m.NewIntVar(0, N, f"over_{d}_{code}")
                    m.Add(ov >= assigned - need_max)
                elif _fb_off_first_cfg:
                    # off_first=True 우선: max 미설정 시 assigned <= need 하드 (잔여 셀 OFF로 회수).
                    # relax_coverage / need=0 무관 강제 — fixed_wanted 가 min 다 채워도 추가 근무 차단.
                    m.Add(assigned <= max(0, need))
                    ov = m.NewIntVar(0, 0, f"over_{d}_{code}")
                elif need > 0:
                    ov = m.NewIntVar(0, N, f"over_{d}_{code}")
                    m.Add(assigned - ov <= need)
                else:
                    ov = m.NewIntVar(0, 0, f"over_{d}_{code}")
                short_terms.append(sh)
                over_terms.append(ov)
                over_vars_by_day.setdefault(d, {})[code] = ov
                short_vars_by_day_code[(d, code)] = sh
                over_vars_by_day_code[(d, code)] = ov

        # 1-B) Max coverage / off_first OFF 균등 분배
        # off_first=True: max coverage 미설정이라도 OFF가 잔여 셀로 회수되므로
        # 일반 간호사 사이에 균등 분배 유도 (전담/주말휴무/preceptee 제외)
        _fb_max_cov_off_equalize_terms = []
        if _fb_has_any_max or _fb_off_first_cfg:
            _fb_nurse_off_vars = []
            for n in range(N):
                if n in preceptee_indices:
                    continue
                if _fb_off_first_cfg:
                    # 주말휴무자·N 전담은 cap 관리 대상 아님 → 풀에서 제외
                    _nu = roster_system.nurses[n] if n < len(roster_system.nurses) else None
                    if _nu is not None and bool(getattr(_nu, "is_weekend_off", False)):
                        continue
                    _raw_nn = getattr(_nu, "allowed_shifts", None) if _nu is not None else None
                    if isinstance(_raw_nn, (set, list, tuple)) and set(_raw_nn) == {"N"}:
                        continue
                # off_first=False 경로의 nonvac_offs 식과 동일한 도메인:
                #   range(T0, T1+1) 중 vacation_off_cells 제외, 고정 OFF는 X(n,d,off)=1 자동
                T0, T1 = join[n], leave[n]
                # off_first=True HARD 풀 가드: 풀먼스 active window 아닌 간호사는 제외
                #   - 중도 가입자(T0>0) / 중도 퇴사자(T1<D_phys-1) → 최대 OFF 용량 상이
                #   - blocked_by_nurse 보유자 → 출장/연수 등으로 OFF 가용량 비대칭
                _fb_blk_set_n = blocked_by_nurse.get(n, set()) if blocked_by_nurse else set()
                if _fb_off_first_cfg:
                    if T0 > 0 or T1 < D_phys - 1 or _fb_blk_set_n:
                        continue
                _phys_days_n = [d for d in range(T0, T1 + 1) if (n, d) not in vacation_off_cells]
                if not _phys_days_n:
                    continue
                _total_off_n = m.NewIntVar(0, len(_phys_days_n), f"fb_mc_off_{n}")
                m.Add(_total_off_n == sum(X(n, d, off_idx) for d in _phys_days_n))
                _fb_nurse_off_vars.append(_total_off_n)
            if len(_fb_nurse_off_vars) >= 2:
                _fb_off_max = m.NewIntVar(0, D_phys, "fb_mc_off_max")
                _fb_off_min = m.NewIntVar(0, D_phys, "fb_mc_off_min")
                m.AddMaxEquality(_fb_off_max, _fb_nurse_off_vars)
                m.AddMinEquality(_fb_off_min, _fb_nurse_off_vars)
                _fb_off_range = m.NewIntVar(0, D_phys, "fb_mc_off_range")
                m.Add(_fb_off_range == _fb_off_max - _fb_off_min)
                # off_first=True: OFF range는 SOFT objective(가중치)로만 유도, HARD 제거.
                # (사용자 명세: off_days 무시 + daily 커버리지 우선 → OFF 균등은 차순위)
                if _fb_off_first_cfg:
                    print(f"{logger_prefix} [MaxCoverage/OffFirst] OFF range는 SOFT (off_first=True)")
                _fb_off_eq_w = -100000 if _fb_off_first_cfg else -200
                _fb_max_cov_off_equalize_terms.append(_fb_off_eq_w * _fb_off_range)
                if _fb_off_first_cfg and len(_fb_nurse_off_vars) >= 3:
                    _fbN = len(_fb_nurse_off_vars)
                    _fb_off_sum = m.NewIntVar(0, D_phys * _fbN, "fb_mc_off_sum")
                    m.Add(_fb_off_sum == sum(_fb_nurse_off_vars))
                    for _i, _ov in enumerate(_fb_nurse_off_vars):
                        _dev = m.NewIntVar(0, D_phys * _fbN, f"fb_mc_off_dev_{_i}")
                        m.Add(_dev * _fbN >= _ov * _fbN - _fb_off_sum)
                        m.Add(_dev * _fbN >= _fb_off_sum - _ov * _fbN)
                        _fb_max_cov_off_equalize_terms.append(-2000 * _dev)
                print(f"{logger_prefix} [MaxCoverage/OffFirst] OFF 균등 분배 제약 추가: 간호사 {len(_fb_nurse_off_vars)}명, range_weight={_fb_off_eq_w}")

        # 2) 안전/법규 위반(정량 슬랙) 구성
        safety = {
            "trans_nd": [],  # N→D 위반 (Bool)
            "trans_ed": [],  # E→D 위반 (Bool)
            "trans_ne": [],  # N→E 위반 (Bool)
            "cwork_missing": [],  # 연속근무 창에서 필요한 OFF 부족량(Int)
            "cnight_excess": [],  # 연속 N 초과(Int)
            "mnight_excess": [],  # 월간 N 초과(Int)
            "night_only_de": [],  # 야간전담의 D/E 배정 위반(Bool/Int)
            "week_off_missing": [],  # 주별 2OFF 부족(Int)
            "rec_3n2o": [],  # N3→2O 회복 부족(Int)
            "rec_2n2o": [],  # N2→2O 회복 부족(Int)
            "pattern_nod": [],  # N-O-D 패턴(Int)
            "pattern_noe": [],  # N-O-E 패턴(Int)
            "pattern_eod": [],  # E-O-D 패턴(Int)
            "min_off_missing": [],  # 월 최소 OFF 부족(Int)
            "off_quota_short": [],  # 개인별 O 할당(주휴 제외) 부족 슬랙(Int)
            "off_quota_excess": [],  # 개인별 O 초과 슬랙(Int)
            "off_cap_bounded_slack": [],
            "isolated_off_slack": [],  # 고립 OFF 허용 슬랙(가중치 포함)
        }
        off_quota_short_by_n: dict[int, cp_model.IntVar] = {}
        off_quota_excess_by_n: dict[int, cp_model.IntVar] = {}
        min_off_miss_by_n: dict[int, cp_model.IntVar] = {}
        target_o_by_n: dict[int, int] = {}
        off_cap_bounded_slack_enable = bool(
            getattr(cfg, "fallback_off_cap_bounded_slack_enable", False)
        )
        off_cap_bounded_slack_max = max(
            0,
            int(getattr(cfg, "fallback_off_cap_bounded_slack_max", 1) or 0),
        )
        off_cap_bounded_slack_weight = max(
            1,
            int(getattr(cfg, "fallback_off_cap_bounded_slack_weight", 10) or 1),
        )

        # 고립 OFF 금지(슬랙 허용): sequential_offs 활성 + 옵션 켜졌을 때만 적용
        if (
            bool(getattr(cfg, "sequential_offs", True))
        ):
            slack_penalty = int(getattr(cfg, "isolated_off_slack_penalty", 300000) or 0)
            # ★★ 휴가·공가가 낀 자리는 **고립 OFF 판정에서 뺀다.**
            #   솔버의 shift_types 는 D/E/N/O 넷뿐이라 연차·반차·공가가 전부 `O` 로 접힌다
            #   (`_build_special_fixed_cells` → special_off_days). 그래서 이 식이
            #   `off_idx` 하나만 보면 **본인이 신청한 휴가를 "근무 사이에 낀 고립 OFF"** 로
            #   읽고 30만 가중치로 회피하려 든다. 반대로 옆에 연차가 붙은 **진짜 고립 OFF** 는
            #   양옆이 off 로 보여 판정을 빠져나간다. 두 방향 모두 틀린다.
            #   실측(성남ICU-RN 2026-10): 최신 판의 실제 고립OFF 0 건인데 엔진은 3 건으로
            #   셌고 전부 연차·반차·공가였다.
            #   ★ 셋 중 하나라도 휴가·공가면 스킵한다 — 가운데가 휴가면 고립이 아니고,
            #     양옆이 휴가면 "근무 사이" 자체가 성립하지 않는다.
            _iso_leave_types = {"휴가", "공가"}

            def _iso_has_leave(_n: int, _d: int) -> bool:
                return any(
                    fixed_type_by_cell.get((_n, _dd)) in _iso_leave_types
                    for _dd in (_d - 1, _d, _d + 1)
                )

            _iso_skip_cnt = 0
            for n in range(N):
                if _is_preceptee_at(n):
                    continue
                t0, t1 = join[n], leave[n]
                for d in range(t0, t1 + 1):
                    if _iso_has_leave(n, d):
                        _iso_skip_cnt += 1
                        continue
                    neighbours = []
                    if d - 1 >= t0:
                        neighbours.append(X(n, d - 1, off_idx))
                    if d + 1 <= t1:
                        neighbours.append(X(n, d + 1, off_idx))
                    slack = m.NewBoolVar(f"iso_off_slack_{n}_{d}")
                    if neighbours:
                        m.Add(X(n, d, off_idx) <= sum(neighbours) + slack)
                    else:
                        m.Add(X(n, d, off_idx) <= slack)
                    if slack_penalty > 0:
                        scaled = m.NewIntVar(0, slack_penalty, f"iso_off_cost_{n}_{d}")
                        m.Add(scaled == slack * slack_penalty)
                        safety["isolated_off_slack"].append(scaled)
                    else:
                        safety["isolated_off_slack"].append(slack)
            print(f"{logger_prefix} [IsoOffLeaveSkip] 휴가·공가로 판정 제외 {_iso_skip_cnt}셀 "
                  f"(stage={stage})")

        # 전이 위반: 정확한 reification (iff) — Option C: 프리셉티도 적용(등가로 프리셉터에 전파)
        for n in range(N):
            T0, T1 = join[n], leave[n]
            for d in range(T0 + 1, T1 + 1):
                xn = X(n, d - 1, night_idx)
                xd = X(n, d, day_idx)
                if getattr(cfg, "ban_n_to_d", True):
                    # fixed_cells로 N→D가 명시적으로 고정된 경우 제약 면제
                    if not (fixed.get((n, d-1)) == night_idx and fixed.get((n, d)) == day_idx):
                        _n2d_expr = (xn + xd <= 1)
                        if _assume_registry_fb is not None and _add_hard_fb is not None:
                            _add_hard_fb(
                                m,
                                _assume_registry_fb,
                                name=f"TransitionBanN2D:nurse_{n}:day_{d}",
                                constraint_expr=_n2d_expr,
                                meta={
                                    "node_id": f"transition_ban_n2d:nurse_{n}:day_{d}",
                                    "type": "TransitionBanNode",
                                    "label": "ban_n_to_d",
                                    "value": {"day": d + 1, "transition": "N->D"},
                                    "scope": "nurse",
                                    "scope_key": f"nurse_{n}",
                                    "pattern": "transition_ban",
                                    "nurse_id": str(getattr(roster_system.nurses[n], "nurse_id", n)),
                                    "human_message_ko": "N 다음날 D 전이는 금지됩니다.",
                                    "resolution_hint": "전이 금지 설정을 완화하거나 해당 날짜 고정을 조정하세요.",
                                },
                            )
                        else:
                            m.Add(_n2d_expr)
                if getattr(cfg, "ban_e_to_d", True):
                    xe = X(n, d - 1, eve_idx)
                    # fixed_cells로 E→D가 명시적으로 고정된 경우 제약 면제
                    if not (fixed.get((n, d-1)) == eve_idx and fixed.get((n, d)) == day_idx):
                        _e2d_expr = (xe + xd <= 1)
                        if _assume_registry_fb is not None and _add_hard_fb is not None:
                            _add_hard_fb(
                                m,
                                _assume_registry_fb,
                                name=f"TransitionBanE2D:nurse_{n}:day_{d}",
                                constraint_expr=_e2d_expr,
                                meta={
                                    "node_id": f"transition_ban_e2d:nurse_{n}:day_{d}",
                                    "type": "TransitionBanNode",
                                    "label": "ban_e_to_d",
                                    "value": {"day": d + 1, "transition": "E->D"},
                                    "scope": "nurse",
                                    "scope_key": f"nurse_{n}",
                                    "pattern": "transition_ban",
                                    "nurse_id": str(getattr(roster_system.nurses[n], "nurse_id", n)),
                                    "human_message_ko": "E 다음날 D 전이는 금지됩니다.",
                                    "resolution_hint": "전이 금지 설정을 완화하거나 해당 날짜 고정을 조정하세요.",
                                },
                            )
                        else:
                            m.Add(_e2d_expr)
                if getattr(cfg, "ban_n_to_e", True):
                    xe2 = X(n, d, eve_idx)
                    # fixed_cells로 N→E가 명시적으로 고정된 경우 제약 면제
                    if not (fixed.get((n, d-1)) == night_idx and fixed.get((n, d)) == eve_idx):
                        _n2e_expr = (xn + xe2 <= 1)
                        if _assume_registry_fb is not None and _add_hard_fb is not None:
                            _add_hard_fb(
                                m,
                                _assume_registry_fb,
                                name=f"TransitionBanN2E:nurse_{n}:day_{d}",
                                constraint_expr=_n2e_expr,
                                meta={
                                    "node_id": f"transition_ban_n2e:nurse_{n}:day_{d}",
                                    "type": "TransitionBanNode",
                                    "label": "ban_n_to_e",
                                    "value": {"day": d + 1, "transition": "N->E"},
                                    "scope": "nurse",
                                    "scope_key": f"nurse_{n}",
                                    "pattern": "transition_ban",
                                    "nurse_id": str(getattr(roster_system.nurses[n], "nurse_id", n)),
                                    "human_message_ko": "N 다음날 E 전이는 금지됩니다.",
                                    "resolution_hint": "전이 금지 설정을 완화하거나 해당 날짜 고정을 조정하세요.",
                                },
                            )
                        else:
                            m.Add(_n2e_expr)
                if mid_idx is not None:
                    # M 은 전날이 D 또는 O 일 때만 — 즉 `D→M` · `O→M` 만 허용한다.
                    #   (전날이 M 이면 둘 다 0 이라 **연속 M 도 여기서 이미 막힌다**)
                    m.Add(X(n, d, mid_idx) <= X(n, d - 1, day_idx) + X(n, d - 1, off_idx))
                    # ★★ 역방향 `M→D` 금지 (2026-09-16). 위 식은 **M 의 앞만** 보고 뒤는 안 봐서
                    #   `M→D` 가 그대로 나왔다(실측 성남 61병동-AN 2026-10 v6 에서 **12건** —
                    #   이 병동의 M 코드는 `D2` 다. 같은 판 `D→M` 14건은 정상이라 그대로 둔다).
                    #   MID 는 늦게 끝나는데 다음날 D 는 이르게 시작해 `E→D` 와 같은 문제다.
                    #   ★ `mid_idx is not None` 이 곧 게이트라 M 코드가 없는 병동은 안 탄다 —
                    #     운영 대상은 **4곳**(220병동 · 220병동-AN · 41병동-AN · 61병동-AN).
                    #   ★ 고정 셀로 `M→D` 가 명시된 자리는 면제한다(ND/ED/NE 와 같은 규약).
                    if not (fixed.get((n, d - 1)) == mid_idx and fixed.get((n, d)) == day_idx):
                        m.Add(X(n, d - 1, mid_idx) + X(n, d, day_idx) <= 1)
                if w_idx is not None:
                    # ── 메인코드 없는 근무(W)에도 `E→` · `N→` 금지를 건다 ──
                    #   `shift_normalizer` 는 shift_gb·default_shift 가 없는 type='근무' 를
                    #   전부 가상코드 **W** 로 접는다(`DD` Day 08:30~17:30 등). 그런데 위
                    #   전이 금지는 `day_idx` **한 인덱스만** 봐서 W 로 접힌 주간 근무가
                    #   그대로 통과했다 — 실측 성남 61병동-RN 2026-10: 김진아 26일 E → 27일 DD.
                    #   ★ `N→W` 가 더 나쁘다. N 은 07:00 에 끝나는데 DD 는 08:30 에 시작해
                    #     휴식이 **1.5시간**이다(`E→D` 의 7.5시간보다 짧다).
                    #   ★ W 셀은 **고정셀로만 존재**한다(솔버는 W 를 배정하지 않는다). 그래서
                    #     이 식은 선행 자유셀 한 칸만 묶고, 수용량 손실이 사실상 없다 —
                    #     실측(61병동-RN 2026-10): 흩어진 DD 허용 8건으로 **수정 전과 동일**.
                    #     같은 목적을 `DD→M` 매핑으로 우회하면 MID 선행 규칙에 걸려 3건으로 준다.
                    #   ★ 양쪽이 모두 고정이면 면제한다 — ND/ED/NE 와 같은 규약.
                    if getattr(cfg, "ban_e_to_d", True):
                        if not (fixed.get((n, d - 1)) == eve_idx and fixed.get((n, d)) == w_idx):
                            m.Add(X(n, d - 1, eve_idx) + X(n, d, w_idx) <= 1)
                    if getattr(cfg, "ban_n_to_d", True):
                        if not (fixed.get((n, d - 1)) == night_idx and fixed.get((n, d)) == w_idx):
                            m.Add(X(n, d - 1, night_idx) + X(n, d, w_idx) <= 1)
                # if getattr(cfg, "ban_d_to_n", True):
                #     xd_prev = X(n, d - 1, day_idx)
                #     m.Add(xd_prev + xn <= 1)

        # 1N 금지 (day0 N 고정인 경우 해당일만 스킵)
        # nurse_monthly_limit.n_max==1 nurse 는 사용자 명시 의도 우선으로 면제.
        not_one_night_val = getattr(cfg, "not_one_night", False)
        print(f"{logger_prefix} [1N금지] not_one_night={not_one_night_val!r} (type={type(not_one_night_val).__name__})")
        try:
            from services.constraints.monthly_limit_constraints import (
                collect_single_n_allowed_nurse_indices,
            )
            _single_n_allowed_lex = collect_single_n_allowed_nurse_indices(roster_system)
        except Exception as _e_sn:
            print(f"{logger_prefix} [1N금지] single_n allowed set 계산 실패(무시): {_e_sn}")
            _single_n_allowed_lex = set()
        if bool(not_one_night_val):
            _pte_fw_1n = getattr(roster_system, '_preceptee_fixed_wanted_map', {}) or {}
            for n in range(N):
                if n in _single_n_allowed_lex:
                    continue
                T0, T1 = join[n], leave[n]
                for d in range(T0, T1 + 1):
                    # 프리셉티 fixed셀은 1N 강제 제외(사용자: 고정 단독N 존중, 복사된 단독N만 방지)
                    if (n, d) in _pte_fw_1n:
                        continue
                    if d == 0 and (n, 0) in fixed and fixed[(n, 0)] == night_idx:
                        continue
                    if d == 0 and prev_month_n_tail_by_idx.get(n, 0) > 0:
                        continue
                    neighbors = []
                    if d - 1 >= T0:
                        neighbors.append(X(n, d - 1, night_idx))
                    if d + 1 <= T1:
                        neighbors.append(X(n, d + 1, night_idx))
                    if not neighbors:
                        continue
                    m.Add(X(n, d, night_idx) <= sum(neighbors))

        # 고정 셀의 직전일 N 금지 (하드). 대상을 고르는 축이 **둘**이다.
        #   ① type  : 휴가·공가        → ban_night_before_fixed_off      (기본 True)
        #   ② 출처  : 확정 원티드 O    → ban_night_before_fixed_wanted_off (기본 False)
        # ②의 근거 — 신청해서 받은 휴일이 직전 N 의 **회복 OFF 시작점**으로 소비되면
        #   실질적으로 쉰 것이 아니다. 휴가·공가와 같은 취급을 받아야 한다.
        #   ★ 대상은 `fixed_wanted_cells`(출처가 fixed_wanted)로 한정한다 —
        #     솔버·주휴 규칙이 **자동으로** 넣은 OFF 는 걸리지 않는다.
        #   ★ 판정은 대표코드 O 로 한다. `주`(주휴) 코드로 신청한 셀도 여기 든다 —
        #     `_load_special_shift_map` 이 `type ∈ {근무,휴가,공가}` 로 거르는데
        #     `O`·`주` 는 실측상 둘 다 `type='휴무'` 라 special 로 안 빠지고
        #     `fixed_wanted_cells` 에 남는다. 반면 휴가·공가 계열 특수 코드는
        #     `special_fixed_cells` 로 먼저 잡혀 ②가 아니라 ① 축이 담당한다.
        # 월 경계: **당월 1일(d == T0)이 고정 OFF 면 대상 밖**이다(루프가 T0+1 부터 돈다).
        #   전월이 N 으로 끝나 그 회복 OFF 가 넘어오는 건 막을 수 없고 막아서도 안 된다.
        #   반면 2일(d == T0+1)이 고정 OFF 면 prev_d == T0(1일) 이므로 **금지가 걸린다**.
        # ★ SKIP_PRIMARY 기본값이 "1" 이라 **이 경로가 실제로 도는 곳**이다.
        #   cp_sat_basic 에만 넣으면 걸리지 않는다.
        _BAN_N_TYPES = {"휴가", "공가"}
        _ban_by_type = bool(getattr(cfg, "ban_night_before_fixed_off", False))
        _ban_by_wanted = bool(getattr(cfg, "ban_night_before_fixed_wanted_off", False))
        if _ban_by_type or _ban_by_wanted:
            # ★ 금지한 (nurse, day) 를 남긴다. 프리셉티 미러링이 프리셉터 셀을 그대로
            #   덮어써 여기서 막은 N 을 되살리기 때문이다(모델 제약은 미러 이전 값에만 걸린다).
            #   미러 쪽에서 조건을 다시 판정하면 두 곳이 갈리므로 **실제로 건 셀**을 넘긴다.
            _ban_n_prev_cells: set[tuple[int, int]] = set()
            for n in range(N):
                T0, T1 = join[n], leave[n]
                _ban_n_cnt = 0
                _ban_n_wanted = 0
                for d in range(T0 + 1, T1 + 1):
                    if (n, d) not in fixed:
                        continue
                    _fw_type = fixed_type_by_cell.get((n, d))
                    _hit_type = _ban_by_type and _fw_type in _BAN_N_TYPES
                    _hit_wanted = (
                        _ban_by_wanted
                        and (n, d) in fixed_wanted_cells
                        and off_idx is not None
                        and fixed.get((n, d)) == off_idx
                    )
                    if not (_hit_type or _hit_wanted):
                        continue  # 어느 축에도 안 걸리는 셀
                    prev_d = d - 1
                    if prev_d < T0:
                        continue
                    if blocked_by_nurse and prev_d in blocked_by_nurse.get(n, set()):
                        continue
                    if (n, prev_d) in fixed:
                        continue  # 이미 고정된 셀은 변경 불가
                    m.Add(X(n, prev_d, night_idx) == 0)
                    _ban_n_prev_cells.add((n, prev_d))
                    _ban_n_cnt += 1
                    if _hit_wanted and not _hit_type:
                        _ban_n_wanted += 1
                if _ban_n_cnt > 0:
                    print(f"{logger_prefix} [BanNBeforeFixedOff] nurse_idx={n}: "
                          f"{_ban_n_cnt}건 N 금지 (그중 확정원티드 O {_ban_n_wanted}건)")
            roster_system._ban_n_prev_cells = _ban_n_prev_cells
        else:
            # ★ 두 축이 모두 꺼졌으면 **비워 둔다**. 같은 roster_system 으로 다시 build 할 때
            #   지난 회차의 목록이 남아 있으면, 제약이 없는데도 미러가 그 칸을 OFF 로 눕힌다.
            roster_system._ban_n_prev_cells = set()

        # # 주말 휴무자 N 요일 제한: 2N 2O 켜진 경우 목금만 N 허용 (2O가 주말에 자연 달성)
        # if bool(getattr(cfg, "two_offs_after_two_nig", False)):
        #     allowed_wd = {3, 4}  # 목금 (weekday: Mon=0 .. Fri=4)
        #     for n in range(N):
        #         nu = roster_system.nurses[n]
        #         if not bool(getattr(nu, "is_weekend_off", False)):
        #             continue
        #         T0, T1 = join[n], leave[n]
        #         for d in range(T0, T1 + 1):
        #             if (n, d) in fixed and fixed[(n, d)] == night_idx:
        #                 continue
        #             wd = (first_day + timedelta(days=d)).weekday()
        #             if wd not in allowed_wd:
        #                 m.Add(X(n, d, night_idx) == 0)

        # 월초 OFF 윈도우 (전월 꼬리 연속근무 보정): 지정 구간에 OFF ≥ 1
        try:
            off_windows = getattr(roster_system, "off_window_constraints", {}) or {}
            if off_idx is not None:
                for n in range(N):
                    if _is_preceptee_at(n):
                        continue
                    # 주말 휴무자도 월경계 연속근무 초과 가능 → 동일 적용
                    T0, T1 = join[n], leave[n]
                    _blocked_ow = blocked_by_nurse.get(n, set()) if blocked_by_nurse else set()
                    for (w_start, w_end) in off_windows.get(n, []) or []:
                        left = max(T0, w_start)
                        right = min(T1, w_end)
                        if left > right:
                            continue
                        # 유저 고정 우선: 윈도우 내 고정 비-OFF 셀은 제외하고 적용
                        # blocked day도 제외 (X 변수 없음 → sum=0 → INFEASIBLE 방지)
                        free_days_w = [d for d in range(left, right + 1) if d not in _blocked_ow and not ((n, d) in fixed and fixed[(n, d)] != off_idx)]
                        if not free_days_w:
                            print(f"{logger_prefix} off_window 무시 (유저 고정 우선, fallback): n={n}, window=[{left+1},{right+1}] 전체 고정")
                            continue
                        _ow_expr_fb = (sum(X(n, d, off_idx) for d in free_days_w) >= 1)
                        if _assume_registry_fb is not None and _add_hard_fb is not None:
                            _add_hard_fb(
                                m,
                                _assume_registry_fb,
                                name=f"OffWindowRequirement:nurse_{n}:left_{left}:right_{right}",
                                constraint_expr=_ow_expr_fb,
                                meta={
                                    "node_id": f"off_window_requirement:nurse_{n}:left_{left}:right_{right}",
                                    "type": "OffWindowNode",
                                    "label": "off_window_min_off",
                                    "value": {"left_day": left + 1, "right_day": right + 1},
                                    "scope": "nurse",
                                    "scope_key": f"nurse_{n}",
                                    "pattern": "off_window_requirement",
                                    "nurse_id": str(getattr(roster_system.nurses[n], "nurse_id", n)),
                                    "human_message_ko": "월초 보정 구간 내 최소 1회 OFF가 필요합니다.",
                                    "resolution_hint": "해당 구간의 고정 근무를 일부 해제하거나 OFF 배정 여유를 확보하세요.",
                                },
                            )
                        else:
                            m.Add(_ow_expr_fb)
        except Exception as e:
            print(f"{logger_prefix} 월초 OFF 윈도우 적용 실패(fallback): err={e}")

        # 연속 근무 K+1 창에서 최소 1 OFF 필요 → HARD 제약 (fixed_wanted 포함, 우회 불가)
        # 정책:
        #   - blocked day 포함 윈도우: X 변수 부재 → 자동 중단 → 스킵
        #   - fixed OFF 포함 윈도우: 자동 만족 → 스킵
        #   - 그 외: 전체 윈도우에 대해 enforce. 유저가 K+1 연속 근무를 fixed_wanted로 지정했다면 INFEASIBLE로 보고.
        K = cfg.max_consecutive_work_days
        for n in range(N):
            T0, T1 = join[n], leave[n]
            _blocked = blocked_by_nurse.get(n, set()) if blocked_by_nurse else set()
            for d0 in range(T0, T1 - K + 1):
                window = [d0 + t for t in range(K + 1)]
                if any(d in _blocked for d in window):
                    continue
                if any((n, d) in fixed and fixed[(n, d)] == off_idx for d in window):
                    continue
                _mcw_expr_fb = (sum(X(n, d, off_idx) for d in window) >= 1)
                if _assume_registry_fb is not None and _add_hard_fb is not None:
                    _add_hard_fb(
                        m,
                        _assume_registry_fb,
                        name=f"MaxConsecutiveWorkWindow:nurse_{n}:start_{d0}:k_{K}",
                        constraint_expr=_mcw_expr_fb,
                        meta={
                            "node_id": f"max_consecutive_work:nurse_{n}:start_{d0}:k_{K}",
                            "type": "ConsecutiveWorkNode",
                            "label": "max_consecutive_work_min_off",
                            "value": {"start_day": d0 + 1, "window_size": K + 1},
                            "scope": "nurse",
                            "scope_key": f"nurse_{n}",
                            "pattern": "max_consecutive_work",
                            "nurse_id": str(getattr(roster_system.nurses[n], "nurse_id", n)),
                            "human_message_ko": "연속 근무 제한 구간(K+1)에는 최소 1회 OFF가 필요합니다.",
                            "resolution_hint": "연속 근무 구간의 고정 배정을 완화하거나 OFF를 추가하세요.",
                        },
                    )
                else:
                    m.Add(_mcw_expr_fb)

        # 동일 시프트 연속 상한 — `AIDE_SAME_SHIFT_HARD_K=3` 이면 D/E/N 각각
        #   **4연속 이상 금지**를 HARD 로 건다(= (K+1) 창의 합 <= K). 기본 OFF(0).
        # 정책:
        #   - ★ 단일 시프트 전담(allowed == {code})은 **제외**한다. 그 코드 연속이 강제되므로
        #     넣으면 근무 4연속만으로 확정 INFEASIBLE 이다(성남ICU 김은경 = D 전담).
        #   - blocked day 포함 창은 스킵 — 연속근무 하드(위)와 같은 규약.
        #   - fixed_wanted 로 같은 코드가 K+1 연속 박혀 있으면 INFEASIBLE 로 보고된다.
        #   - N 은 이미 `max_consecutive_nights` 하드가 있어 중복이지만 그대로 건다(무해).
        #   build_model 은 stage 1/2/3 마다 호출되므로 이 제약도 전 스테이지에 걸린다.
        import os as _os_ssh
        # 설정(`roster_config.same_shift_hard_k`)이 정본. 환경변수는 A/B 용 오버라이드.
        # ★ cfg 가 **0 이면 자동 완화로 내려간 상태**이므로 환경변수로 되살리지 않는다.
        #   (안 그러면 A/B 중 INFEASIBLE 이 나도 재시도에서 하드가 그대로 살아 완화가 죽는다.)
        # ★★ 환경변수는 **빈 문자열과 "0" 을 구분**한다. `int(env or 0) or cfg` 로 쓰면
        #   env="0" 이 falsy 라 cfg(기본 3)로 되돌아가 **하드를 끌 수가 없다** — A/B 의
        #   "현행(하드 off)" 팔 자체를 만들지 못한다.
        _ssh_cfg_k = int(getattr(cfg, "same_shift_hard_k", 0) or 0)
        _ssh_env = _os_ssh.environ.get("AIDE_SAME_SHIFT_HARD_K", "")
        if _ssh_cfg_k == 0:
            _ssh_k = 0          # cfg 0 = 자동 완화로 내려간 상태 → 환경변수로 되살리지 않는다
        elif _ssh_env != "":
            _ssh_k = int(_ssh_env)   # 명시값 존중(0 이면 끈다)
        else:
            _ssh_k = _ssh_cfg_k
        if _ssh_k > 0:
            _use_mid_ssh = bool(getattr(cfg, "use_mid", False))
            _ssh_cnt = 0
            for _code_ssh in ("D", "E", "N"):
                if _code_ssh not in cfg.shift_types:
                    continue
                _s_idx_ssh = cfg.shift_types.index(_code_ssh)
                for n in range(N):
                    _al_ssh = normalize_allowed_shift_codes(
                        getattr(roster_system.nurses[n], "allowed_shifts", None),
                        use_mid=_use_mid_ssh)
                    if _al_ssh and _al_ssh == {_code_ssh}:
                        continue  # 단일 시프트 전담 — 연속 강제라 제외
                    T0, T1 = join[n], leave[n]
                    _blk_ssh = blocked_by_nurse.get(n, set()) if blocked_by_nurse else set()
                    # ★ `+1` 필수 — 없으면 **월말을 끝으로 하는 창이 통째로 빠진다**
                    #   (위 연속근무 하드의 `range(T0, T1 - K + 1)` 와 같은 규약).
                    #   실측: 빠뜨렸을 때 D4/E4 가 회차당 0~1건씩 새어나왔다.
                    for d0 in range(T0, T1 - _ssh_k + 1):
                        _win_ssh = [d0 + t for t in range(_ssh_k + 1)]
                        if any(d in _blk_ssh for d in _win_ssh):
                            continue
                        m.Add(sum(X(n, d, _s_idx_ssh) for d in _win_ssh) <= _ssh_k)
                        _ssh_cnt += 1
            print(f"{logger_prefix} [SameShiftHard] k={_ssh_k} 제약 {_ssh_cnt}건 (stage={stage})")

        # 고립근무(O-W-O) HARD 금지 — `isolated_work_hard`(기본 True).
        #   "양옆이 OFF 면 가운데는 근무 불가" 를 직접 건다.
        #   soft 항(`ISOLATED_WORK_PENALTY` 1500)과 **같은 식**을 부등식으로 바꾼 것이다:
        #     soft: iw >= off[d-1] + off[d+1] + mid_work - 2   (위반량을 벌점으로)
        #     hard:       off[d-1] + off[d+1] + mid_work <= 2   (위반 자체를 금지)
        # ★ soft 만으로는 못 줄인다 — 벌점 스윕(1500/4000/10000/30000)이 9·9·7·9 로
        #   **방향성이 없었다**. 가중치가 약한 게 아니라 다른 제약이 그 자리를 강제한다.
        # ★ N 단독(O-N-O)은 제외한다 — 하드락 7(1N 금지)이 별도로 관리하고,
        #   `n_max==1` 면제와 충돌한다(soft 항의 `mid_work` 정의와 동일하게 맞춘다).
        # ★ 경계일(d=T0/T1)은 양옆 확인이 불가해 자연히 빠진다(range(T0+1, T1)).
        # ★ 고정 셀은 건드릴 수 없으므로 셋 중 하나라도 fixed 면 스킵한다 —
        #   확정 원티드로 O-W-O 가 이미 박혀 있으면 INFEASIBLE 로 보고되는 게 아니라
        #   그 자리는 제약 대상이 아니어야 한다(사용자가 지정한 것이다).
        # build_model 은 stage 1/2/3 마다 호출되므로 이 제약도 전 스테이지에 걸린다.
        if bool(getattr(cfg, "isolated_work_hard", False)) and "O" in cfg.shift_types:
            _ih_off = cfg.shift_types.index("O")
            _ih_has_n = "N" in cfg.shift_types
            _ih_night = cfg.shift_types.index("N") if _ih_has_n else None
            # 커버리지 0 근무(교육류·전담)가 접히는 가상 코드. 없는 병동도 있다.
            _ih_w = cfg.shift_types.index("W") if "W" in cfg.shift_types else None
            _ih_cnt = 0
            _ih_fx = 0
            for n in range(N):
                T0, T1 = join[n], leave[n]
                _ih_blk = blocked_by_nurse.get(n, set()) if blocked_by_nurse else set()
                for d in range(T0 + 1, T1):
                    if any(dd in _ih_blk for dd in (d - 1, d, d + 1)):
                        continue
                    # ★ **가운데(d)가 고정일 때만** 스킵한다. 양옆이 고정 OFF 여도
                    #   가운데가 자유로우면 제약을 걸어야 거기에 근무가 안 들어간다.
                    #   처음엔 셋 중 하나라도 고정이면 통째로 건너뛰었는데, 확정 원티드로
                    #   **양옆 OFF 만** 박힌 자리가 그대로 뚫려 고립근무가 남았다
                    #   (실측: 하드 447건을 걸고도 3~6건 잔존 · 전부 순수 O-근무-O).
                    if (n, d) in fixed:
                        # ★★ 가운데가 **고정 근무**면 위 식은 못 쓴다(가운데를 못 바꾼다).
                        #   대신 **양옆이 둘 다 OFF 가 되는 것**을 막아야 한다 — 이걸 빼면
                        #   확정 원티드 D 한 칸 주변을 엔진이 O 로 채워 O-D-O 가 완성된다
                        #   (실측 성남ICU-RN 2026-10: 잔존 4건이 전부 이 경로 ·
                        #    임옥희 13/27 은 뒤가 고정 O · 오정 7 은 양옆 둘 다 자유였다).
                        _ih_s = fixed[(n, d)]
                        if _ih_s == _ih_off or (_ih_has_n and _ih_s == _ih_night):
                            continue        # 고정 OFF·N 은 고립근무가 아니다
                        if _ih_w is not None and _ih_s == _ih_w:
                            # ★★ W = **커버리지 0 근무**(교육·노조교육·보수교육·직무교육·DA·DD).
                            #   `shifts.type` 은 '근무' 지만 `daily_shift_requirements` 에 요구가
                            #   없어 병동 인원으로 안 잡힌다(`:1763-1777`). 그래서 옆에 근무를
                            #   붙여도 **병동이 얻는 게 없다** — 교육일 커버리지는 0 그대로다.
                            #   ★ 교육 날짜는 병원이 정해 옮길 수도 없다. 옆에 근무를 붙여도
                            #     그 날 병동 인원은 그대로라 **아무것도 개선되지 않는다**.
                            #   ★ 실측 2026-09-15 성남ICU-RN: 이 제외로 고정근무 주변 제약이
                            #     20건 → 4건이 되고, 커버리지 근무 고립은 5회 전부 0건을 유지했다.
                            continue
                        # 양옆이 **둘 다** 고정이면 손댈 칸이 없다 → 걸면 INFEASIBLE 만 난다
                        if (n, d - 1) in fixed and (n, d + 1) in fixed:
                            continue
                        m.Add(X(n, d - 1, _ih_off) + X(n, d + 1, _ih_off) <= 1)
                        _ih_fx += 1
                        continue
                    # W 는 커버리지 0 근무라 고립근무로 세지 않는다(위 고정 분기와 같은 기준).
                    _ih_mid = (1 - X(n, d, _ih_off)
                               - (X(n, d, _ih_night) if _ih_has_n else 0)
                               - (X(n, d, _ih_w) if _ih_w is not None else 0))
                    m.Add(X(n, d - 1, _ih_off) + X(n, d + 1, _ih_off) + _ih_mid <= 2)
                    _ih_cnt += 1
            print(
                f"{logger_prefix} [IsolatedWorkHard] 제약 {_ih_cnt}건 "
                f"(+고정근무 주변 {_ih_fx}건) (stage={stage})"
            )

        # 연속 Night 상한 L → 초과량 정량화
        L = cfg.max_consecutive_nights
        for n in range(N):
            T0, T1 = join[n], leave[n]
            n_tail = prev_month_n_tail_by_idx.get(n, 0)
            _n_offs_after_cnight = (getattr(roster_system, "prev_month_n_offs_after_by_idx", {}) or {}).get(n, 0)
            # offs_after >= 1 이면 야간 연속이 이미 끊긴 상태 → 월경계 연속N 제약 스킵
            if n_tail > 0 and _n_offs_after_cnight == 0:
                for w in range(1, n_tail + 1):
                    april_window_end = L - w
                    cap = L - w
                    if april_window_end < 0 or cap < 0:
                        continue
                    days_in_window = list(range(T0, min(T0 + april_window_end + 1, T1 + 1)))
                    if days_in_window:
                        _cn_edge_expr_fb = (sum(X(n, d, night_idx) for d in days_in_window) <= cap)
                        if _assume_registry_fb is not None and _add_hard_fb is not None:
                            _add_hard_fb(
                                m,
                                _assume_registry_fb,
                                name=f"ConsecutiveNightCapEdge:nurse_{n}:w_{w}",
                                constraint_expr=_cn_edge_expr_fb,
                                meta={
                                    "node_id": f"consecutive_night_cap_edge:nurse_{n}:w_{w}",
                                    "type": "ConsecutiveNightCapNode",
                                    "label": "consecutive_night_cap_edge",
                                    "value": {"window_len": len(days_in_window), "cap": int(cap)},
                                    "scope": "nurse",
                                    "scope_key": f"nurse_{n}",
                                    "pattern": "consecutive_night_cap",
                                    "nurse_id": str(getattr(roster_system.nurses[n], "nurse_id", n)),
                                    "human_message_ko": "월경계 연속 야간 상한을 초과할 수 없습니다.",
                                    "resolution_hint": "해당 구간의 N 고정을 완화하거나 야간 배정을 분산하세요.",
                                },
                            )
                        else:
                            m.Add(_cn_edge_expr_fb)
            for d0 in range(T0, T1 - L + 1):
                sum_n = sum(X(n, d0 + t, night_idx) for t in range(L + 1))
                exc = m.NewIntVar(0, L + 1, f"cnight_exc_{n}_{d0}")
                m.Add(exc >= sum_n - L)
                # 연속 N 상한 L 하드: three_seq_nig False면 L=2(3N 금지), True면 L=3(3N 허용)
                _cn_expr_fb = (sum_n <= L)
                if _assume_registry_fb is not None and _add_hard_fb is not None:
                    _add_hard_fb(
                        m,
                        _assume_registry_fb,
                        name=f"ConsecutiveNightCap:nurse_{n}:start_{d0}:L_{L}",
                        constraint_expr=_cn_expr_fb,
                        meta={
                            "node_id": f"consecutive_night_cap:nurse_{n}:start_{d0}:L_{L}",
                            "type": "ConsecutiveNightCapNode",
                            "label": "consecutive_night_cap",
                            "value": {"start_day": d0 + 1, "window_size": L + 1, "cap": int(L)},
                            "scope": "nurse",
                            "scope_key": f"nurse_{n}",
                            "pattern": "consecutive_night_cap",
                            "nurse_id": str(getattr(roster_system.nurses[n], "nurse_id", n)),
                            "human_message_ko": "연속 야간 상한을 초과할 수 없습니다.",
                            "resolution_hint": "연속 야간 구간의 고정을 완화하거나 다른 간호사로 분산하세요.",
                        },
                    )
                else:
                    m.Add(_cn_expr_fb)
                safety["cnight_excess"].append(exc)

        # 월 Night 상한 초과량 — MUS 추출용 assumption literal로 wrap
        for n in range(N):
            if _is_preceptee_at(n):
                continue
            T0, T1 = join[n], leave[n]
            sum_m = sum(X(n, d, night_idx) for d in range(T0, T1 + 1))
            _fb_mn_expr = (sum_m <= cfg.max_night_shifts_per_month)
            if _assume_registry_fb is not None and _add_hard_fb is not None:
                _fb_nu = roster_system.nurses[n] if n < len(roster_system.nurses) else None
                _add_hard_fb(
                    m, _assume_registry_fb,
                    name=f"MaxNight:nurse_{n}",
                    constraint_expr=_fb_mn_expr,
                    meta={
                        "node_id": f"max_night:nurse_{n}",
                        "type": "NightCapNode",
                        "label": "max_night_shifts_per_month",
                        "value": int(cfg.max_night_shifts_per_month or 0),
                        "scope": "nurse", "scope_key": f"nurse_{n}",
                        "pattern": "max_night",
                        "nurse_id": str(getattr(_fb_nu, "nurse_id", n)) if _fb_nu else str(n),
                        "human_message_ko": f"월간 N 상한 {int(cfg.max_night_shifts_per_month or 0)}일",
                        "resolution_hint": "월간 N 상한을 늘리거나 이 간호사의 N 부담을 다른 인력에 분산하세요.",
                    },
                )
            else:
                m.Add(_fb_mn_expr)

        # N 전담: D/E 하드 금지 (메인 모델과 동일)
        for n, nu in enumerate(roster_system.nurses):
            if _is_preceptee_at(n):
                continue
            raw = getattr(nu, "allowed_shifts", None)
            allowed = normalize_allowed_shift_codes(raw, use_mid=bool(getattr(cfg, "use_mid", False)))
            if not allowed:
                continue
            T0, T1 = join[n], leave[n]
            for d in range(T0, T1 + 1):
                if "D" not in allowed:
                    _allow_d_expr = (X(n, d, day_idx) == 0)
                    if _assume_registry_fb is not None and _add_hard_fb is not None:
                        _add_hard_fb(
                            m,
                            _assume_registry_fb,
                            name=f"AllowedShiftMaskBanD:nurse_{n}:day_{d}",
                            constraint_expr=_allow_d_expr,
                            meta={
                                "node_id": f"allowed_shift_mask:nurse_{n}:day_{d}:shift_D",
                                "type": "AllowedShiftMaskNode",
                                "label": "allowed_shift_mask_ban",
                                "value": {"day": d + 1, "shift": "D", "allowed": sorted(allowed)},
                                "scope": "nurse",
                                "scope_key": f"nurse_{n}",
                                "pattern": "allowed_shift_mask",
                                "nurse_id": str(getattr(roster_system.nurses[n], "nurse_id", n)),
                                "human_message_ko": "해당 간호사는 D 근무 허용 대상이 아닙니다.",
                                "resolution_hint": "간호사 허용 시프트 설정을 변경하거나 해당 배정을 제거하세요.",
                            },
                        )
                    else:
                        m.Add(_allow_d_expr)
                if "E" not in allowed:
                    _allow_e_expr = (X(n, d, eve_idx) == 0)
                    if _assume_registry_fb is not None and _add_hard_fb is not None:
                        _add_hard_fb(
                            m,
                            _assume_registry_fb,
                            name=f"AllowedShiftMaskBanE:nurse_{n}:day_{d}",
                            constraint_expr=_allow_e_expr,
                            meta={
                                "node_id": f"allowed_shift_mask:nurse_{n}:day_{d}:shift_E",
                                "type": "AllowedShiftMaskNode",
                                "label": "allowed_shift_mask_ban",
                                "value": {"day": d + 1, "shift": "E", "allowed": sorted(allowed)},
                                "scope": "nurse",
                                "scope_key": f"nurse_{n}",
                                "pattern": "allowed_shift_mask",
                                "nurse_id": str(getattr(roster_system.nurses[n], "nurse_id", n)),
                                "human_message_ko": "해당 간호사는 E 근무 허용 대상이 아닙니다.",
                                "resolution_hint": "간호사 허용 시프트 설정을 변경하거나 해당 배정을 제거하세요.",
                            },
                        )
                    else:
                        m.Add(_allow_e_expr)
                if "N" not in allowed:
                    _allow_n_expr = (X(n, d, night_idx) == 0)
                    if _assume_registry_fb is not None and _add_hard_fb is not None:
                        _add_hard_fb(
                            m,
                            _assume_registry_fb,
                            name=f"AllowedShiftMaskBanN:nurse_{n}:day_{d}",
                            constraint_expr=_allow_n_expr,
                            meta={
                                "node_id": f"allowed_shift_mask:nurse_{n}:day_{d}:shift_N",
                                "type": "AllowedShiftMaskNode",
                                "label": "allowed_shift_mask_ban",
                                "value": {"day": d + 1, "shift": "N", "allowed": sorted(allowed)},
                                "scope": "nurse",
                                "scope_key": f"nurse_{n}",
                                "pattern": "allowed_shift_mask",
                                "nurse_id": str(getattr(roster_system.nurses[n], "nurse_id", n)),
                                "human_message_ko": "해당 간호사는 N 근무 허용 대상이 아닙니다.",
                                "resolution_hint": "간호사 허용 시프트 설정을 변경하거나 해당 배정을 제거하세요.",
                            },
                        )
                    else:
                        m.Add(_allow_n_expr)
                if mid_idx is not None and "M" not in allowed:
                    _allow_m_expr = (X(n, d, mid_idx) == 0)
                    if _assume_registry_fb is not None and _add_hard_fb is not None:
                        _add_hard_fb(
                            m,
                            _assume_registry_fb,
                            name=f"AllowedShiftMaskBanM:nurse_{n}:day_{d}",
                            constraint_expr=_allow_m_expr,
                            meta={
                                "node_id": f"allowed_shift_mask:nurse_{n}:day_{d}:shift_M",
                                "type": "AllowedShiftMaskNode",
                                "label": "allowed_shift_mask_ban",
                                "value": {"day": d + 1, "shift": "M", "allowed": sorted(allowed)},
                                "scope": "nurse",
                                "scope_key": f"nurse_{n}",
                                "pattern": "allowed_shift_mask",
                                "nurse_id": str(getattr(roster_system.nurses[n], "nurse_id", n)),
                                "human_message_ko": "해당 간호사는 M 근무 허용 대상이 아닙니다.",
                                "resolution_hint": "간호사 허용 시프트 설정을 변경하거나 해당 배정을 제거하세요.",
                            },
                        )
                    else:
                        m.Add(_allow_m_expr)

        # 야간전담의 D/E 금지 위반(OR: D or E) — N전담은 하드로 처리하므로 소프트 미사용
        # for n, nu in enumerate(roster_system.nurses):
        #     if nu.allowed_shifts != 0:
        #         continue
        #     T0, T1 = join[n], leave[n]
        #     for d in range(T0, T1 + 1):
        #         v = m.NewIntVar(0, 1, f"nonly_de_{n}_{d}")
        #         m.Add(v >= X(n, d, day_idx))
        #         m.Add(v >= X(n, d, eve_idx))
        #         m.Add(v <= X(n, d, day_idx) + X(n, d, eve_idx))
        #         safety["night_only_de"].append(v)

        # 주별 2OFF 부족량
        if cfg.enforce_two_offs_per_week:
            weeks = D // 7
            for n in range(N):
                if _is_preceptee_at(n):
                    continue
                for w in range(weeks):
                    d0, d1 = w * 7, min(w * 7 + 7, D)
                    offs = sum(X(n, d, off_idx) for d in range(d0, d1) if join[n] <= d <= leave[n])
                    miss = m.NewIntVar(0, 2, f"week_miss_{n}_{w}")
                    m.Add(miss >= 2 - offs)
                    safety["week_off_missing"].append(miss)

        # 회복 규칙: N3→2O, N2→2O 부족량
        if cfg.two_offs_after_three_nig:
            _n_offs_after_map_3n = getattr(roster_system, "prev_month_n_offs_after_by_idx", {}) or {}
            for n in range(N):
                T0, T1 = join[n], leave[n]
                _blocked_3n = blocked_by_nurse.get(n, set()) if blocked_by_nurse else set()
                n_tail = prev_month_n_tail_by_idx.get(n, 0)
                n_offs_after_3n = _n_offs_after_map_3n.get(n, 0)
                _3n_rem = max(0, 2 - n_offs_after_3n) if n_tail >= 3 else 2
                if n_tail >= 3 and _3n_rem > 0 and (T0 + 1) <= T1 and T0 not in _blocked_3n and (T0 + 1) not in _blocked_3n:
                    end_prev_block = m.NewBoolVar(f"end_3n_prev_soft_{n}")
                    m.Add(end_prev_block == X(n, T0, night_idx).Not())
                    if not any((n, d2) in fixed_wanted_cells and fixed.get((n, d2)) not in (off_idx, night_idx, None) for d2 in (T0, T0 + 1)):
                        if _3n_rem >= 2:
                            _co_3n_expr_fb = (X(n, T0, off_idx) + X(n, T0 + 1, off_idx) == 2)
                            if _assume_registry_fb is not None:
                                _co_lit_fb = _assume_registry_fb.create_literal(
                                    f"CarryoverRecovery3N2OFF:nurse_{n}:day_{T0}",
                                    meta={
                                        "node_id": f"carryover_recovery_3n2off:nurse_{n}:day_{T0}",
                                        "type": "CarryoverTransitionNode",
                                        "label": "prev_month 3N2OFF boundary",
                                        "value": {"day": T0 + 1, "remaining_off_needed": 2},
                                        "scope": "nurse",
                                        "scope_key": f"nurse_{n}",
                                        "pattern": "carryover_boundary",
                                        "nurse_id": str(getattr(roster_system.nurses[n], "nurse_id", n)),
                                        "human_message_ko": "전월 3N 꼬리로 월초 2OFF 회복이 필요합니다.",
                                        "resolution_hint": "전월 경계 carryover 또는 월초 고정 배정을 조정하세요.",
                                    },
                                )
                                m.Add(_co_3n_expr_fb).OnlyEnforceIf([end_prev_block, _co_lit_fb])
                            else:
                                m.Add(_co_3n_expr_fb).OnlyEnforceIf([end_prev_block])
                        else:
                            # _3n_rem == 1: 전월 OFF가 T0 직전에 인접 → 남은 OFF는 T0에 강제(연속 2OFF 보장)
                            _co_3n_expr_fb = (X(n, T0, off_idx) >= 1)
                            if _assume_registry_fb is not None:
                                _co_lit_fb = _assume_registry_fb.create_literal(
                                    f"CarryoverRecovery3N2OFFPartial:nurse_{n}:day_{T0}",
                                    meta={
                                        "node_id": f"carryover_recovery_3n2off_partial:nurse_{n}:day_{T0}",
                                        "type": "CarryoverTransitionNode",
                                        "label": "prev_month 3N2OFF boundary partial",
                                        "value": {"day": T0 + 1, "remaining_off_needed": 1},
                                        "scope": "nurse",
                                        "scope_key": f"nurse_{n}",
                                        "pattern": "carryover_boundary",
                                        "nurse_id": str(getattr(roster_system.nurses[n], "nurse_id", n)),
                                        "human_message_ko": "전월 3N 꼬리 회복 OFF가 월초에 추가로 필요합니다.",
                                        "resolution_hint": "월초 OFF 슬롯 또는 전월 carryover 입력을 조정하세요.",
                                    },
                                )
                                m.Add(_co_3n_expr_fb).OnlyEnforceIf([_co_lit_fb])
                            else:
                                m.Add(_co_3n_expr_fb)
                    print(f"{logger_prefix} [3N2OFF-cross] nurse_idx={n}, n_tail={n_tail}, "
                          f"offs_after={n_offs_after_3n}, rem={_3n_rem}")
                elif n_tail >= 3 and _3n_rem == 0:
                    print(f"{logger_prefix} [3N2OFF-cross] nurse_idx={n}, n_tail={n_tail}, "
                          f"offs_after={n_offs_after_3n} → 전월 내 2OFF 충족, 현월 강제 OFF 스킵")
                if n_tail >= 2 and n_offs_after_3n < 2 and (T0 + 2) <= T1:
                    if not any((n, d2) in fixed_wanted_cells and fixed.get((n, d2)) not in (off_idx, night_idx, None) for d2 in (T0 + 1, T0 + 2)):
                        _expr_3n_tail2_fb = (X(n, T0 + 1, off_idx) + X(n, T0 + 2, off_idx) == 2)
                        if _assume_registry_fb is not None and _add_hard_fb is not None:
                            _co_lit_fb = _assume_registry_fb.create_literal(
                                f"CarryoverRecovery3N2OFFTail2:nurse_{n}:day_{T0}",
                                meta={
                                    "node_id": f"carryover_recovery_3n2off_tail2:nurse_{n}:day_{T0}",
                                    "type": "CarryoverTransitionNode",
                                    "label": "prev_month 3N2OFF boundary tail2",
                                    "value": {"day": T0 + 2, "remaining_off_needed": 2},
                                    "scope": "nurse",
                                    "scope_key": f"nurse_{n}",
                                    "pattern": "carryover_boundary",
                                    "nurse_id": str(getattr(roster_system.nurses[n], "nurse_id", n)),
                                    "human_message_ko": "전월 N 꼬리로 월초 회복 OFF 2일이 필요합니다.",
                                    "resolution_hint": "월초 OFF 슬롯 또는 전월 carryover 입력을 조정하세요.",
                                },
                            )
                            m.Add(_expr_3n_tail2_fb).OnlyEnforceIf([X(n, T0, night_idx), _co_lit_fb])
                        else:
                            m.Add(_expr_3n_tail2_fb).OnlyEnforceIf([X(n, T0, night_idx)])
                if n_tail == 1 and n_offs_after_3n < 2 and (T0 + 3) <= T1:
                    if not any((n, d2) in fixed_wanted_cells and fixed.get((n, d2)) not in (off_idx, night_idx, None) for d2 in (T0 + 2, T0 + 3)):
                        _expr_3n_tail1_fb = (X(n, T0 + 2, off_idx) + X(n, T0 + 3, off_idx) == 2)
                        if _assume_registry_fb is not None and _add_hard_fb is not None:
                            _co_lit_fb = _assume_registry_fb.create_literal(
                                f"CarryoverRecovery3N2OFFTail1:nurse_{n}:day_{T0}",
                                meta={
                                    "node_id": f"carryover_recovery_3n2off_tail1:nurse_{n}:day_{T0}",
                                    "type": "CarryoverTransitionNode",
                                    "label": "prev_month 3N2OFF boundary tail1",
                                    "value": {"day": T0 + 3, "remaining_off_needed": 2},
                                    "scope": "nurse",
                                    "scope_key": f"nurse_{n}",
                                    "pattern": "carryover_boundary",
                                    "nurse_id": str(getattr(roster_system.nurses[n], "nurse_id", n)),
                                    "human_message_ko": "전월 N 꼬리 회복을 위해 월초 OFF 2일이 필요합니다.",
                                    "resolution_hint": "월초 OFF 슬롯 또는 전월 carryover 입력을 조정하세요.",
                                },
                            )
                            m.Add(_expr_3n_tail1_fb).OnlyEnforceIf([X(n, T0, night_idx), X(n, T0 + 1, night_idx), _co_lit_fb])
                        else:
                            m.Add(_expr_3n_tail1_fb).OnlyEnforceIf([X(n, T0, night_idx), X(n, T0 + 1, night_idx)])
                for d in range(T0 + 2, T1 - 1):
                    if any((n, d2) in fixed_wanted_cells and fixed.get((n, d2)) not in (off_idx, night_idx, None) for d2 in (d + 1, d + 2)):
                        # 회복 OFF 슬롯에 non-OFF fixed_wanted → 3N 블록 자체를 금지
                        _guard_3n_expr_fb = (X(n, d, night_idx) + X(n, d - 1, night_idx) + X(n, d - 2, night_idx) <= 2)
                        if _assume_registry_fb is not None and _add_hard_fb is not None:
                            _add_hard_fb(
                                m,
                                _assume_registry_fb,
                                name=f"CarryoverRecovery3N2OFFGuard:nurse_{n}:day_{d}",
                                constraint_expr=_guard_3n_expr_fb,
                                meta={
                                    "node_id": f"carryover_recovery_3n2off_guard:nurse_{n}:day_{d}",
                                    "type": "CarryoverTransitionNode",
                                    "label": "carryover 3N block guard",
                                    "value": {"day": d + 1, "blocked_by_fixed": True},
                                    "scope": "nurse",
                                    "scope_key": f"nurse_{n}",
                                    "pattern": "carryover_boundary",
                                    "nurse_id": str(getattr(roster_system.nurses[n], "nurse_id", n)),
                                    "human_message_ko": "회복 OFF 슬롯 고정과 3N 블록이 충돌합니다.",
                                    "resolution_hint": "해당 고정 배정 또는 회복 규칙을 조정하세요.",
                                },
                            )
                        else:
                            m.Add(_guard_3n_expr_fb)
                        continue
                    xn0 = X(n, d, night_idx)
                    xn1 = X(n, d - 1, night_idx)
                    xn2 = X(n, d - 2, night_idx)
                    m.Add(
                        X(n, d + 1, off_idx) + X(n, d + 2, off_idx) == 2
                    ).OnlyEnforceIf([xn0, xn1, xn2])
        if cfg.two_offs_after_two_nig:
            _n_offs_after_map = getattr(roster_system, "prev_month_n_offs_after_by_idx", {}) or {}
            for n in range(N):
                T0, T1 = join[n], leave[n]
                _blocked_2n = blocked_by_nurse.get(n, set()) if blocked_by_nurse else set()
                n_tail = prev_month_n_tail_by_idx.get(n, 0)
                n_offs_after = _n_offs_after_map.get(n, 0)
                # 전월 N tail 뒤 이미 소비된 OFF 수를 반영
                _2n_rem = max(0, 2 - n_offs_after) if n_tail >= 2 else 2
                if n_tail >= 2 and _2n_rem > 0 and (T0 + 1) <= T1 and T0 not in _blocked_2n and (T0 + 1) not in _blocked_2n:
                    end_prev_block = m.NewBoolVar(f"end_2n_prev_soft_{n}")
                    m.Add(end_prev_block == X(n, T0, night_idx).Not())
                    if not any((n, d2) in fixed_wanted_cells and fixed.get((n, d2)) not in (off_idx, night_idx, None) for d2 in (T0, T0 + 1)):
                        if _2n_rem >= 2:
                            _co_2n_expr_fb = (X(n, T0, off_idx) + X(n, T0 + 1, off_idx) == 2)
                            if _assume_registry_fb is not None:
                                _co_lit_fb = _assume_registry_fb.create_literal(
                                    f"CarryoverRecovery2N2OFF:nurse_{n}:day_{T0}",
                                    meta={
                                        "node_id": f"carryover_recovery_2n2off:nurse_{n}:day_{T0}",
                                        "type": "CarryoverTransitionNode",
                                        "label": "prev_month 2N2OFF boundary",
                                        "value": {"day": T0 + 1, "remaining_off_needed": 2},
                                        "scope": "nurse",
                                        "scope_key": f"nurse_{n}",
                                        "pattern": "carryover_boundary",
                                        "nurse_id": str(getattr(roster_system.nurses[n], "nurse_id", n)),
                                        "human_message_ko": "전월 2N 꼬리로 월초 2OFF 회복이 필요합니다.",
                                        "resolution_hint": "전월 경계 carryover 또는 월초 고정 배정을 조정하세요.",
                                    },
                                )
                                m.Add(_co_2n_expr_fb).OnlyEnforceIf([end_prev_block, _co_lit_fb])
                            else:
                                m.Add(_co_2n_expr_fb).OnlyEnforceIf([end_prev_block])
                        else:
                            # _2n_rem == 1: 전월 OFF가 T0 직전에 인접 → 남은 OFF는 T0에 강제(연속 2OFF 보장)
                            _co_2n_expr_fb = (X(n, T0, off_idx) >= 1)
                            if _assume_registry_fb is not None:
                                _co_lit_fb = _assume_registry_fb.create_literal(
                                    f"CarryoverRecovery2N2OFFPartial:nurse_{n}:day_{T0}",
                                    meta={
                                        "node_id": f"carryover_recovery_2n2off_partial:nurse_{n}:day_{T0}",
                                        "type": "CarryoverTransitionNode",
                                        "label": "prev_month 2N2OFF boundary partial",
                                        "value": {"day": T0 + 1, "remaining_off_needed": 1},
                                        "scope": "nurse",
                                        "scope_key": f"nurse_{n}",
                                        "pattern": "carryover_boundary",
                                        "nurse_id": str(getattr(roster_system.nurses[n], "nurse_id", n)),
                                        "human_message_ko": "전월 2N 꼬리 회복 OFF가 월초에 추가로 필요합니다.",
                                        "resolution_hint": "월초 OFF 슬롯 또는 전월 carryover 입력을 조정하세요.",
                                    },
                                )
                                m.Add(_co_2n_expr_fb).OnlyEnforceIf([_co_lit_fb])
                            else:
                                m.Add(_co_2n_expr_fb)
                    print(f"{logger_prefix} [2N2OFF-cross] nurse_idx={n}, n_tail={n_tail}, "
                          f"offs_after={n_offs_after}, rem={_2n_rem}")
                elif n_tail >= 2 and _2n_rem == 0:
                    print(f"{logger_prefix} [2N2OFF-cross] nurse_idx={n}, n_tail={n_tail}, "
                          f"offs_after={n_offs_after} → 전월 내 2OFF 충족, 현월 강제 OFF 스킵")
                if n_tail >= 1 and n_offs_after < 2 and (T0 + 2) <= T1:
                    end_block_b0 = m.NewBoolVar(f"end_2n_soft_b0_{n}")
                    m.Add(end_block_b0 == X(n, T0 + 1, night_idx).Not())
                    if not any((n, d2) in fixed_wanted_cells and fixed.get((n, d2)) not in (off_idx, night_idx, None) for d2 in (T0 + 1, T0 + 2)):
                        _expr_2n_b0_fb = (X(n, T0 + 1, off_idx) + X(n, T0 + 2, off_idx) == 2)
                        if _assume_registry_fb is not None and _add_hard_fb is not None:
                            _co_lit_fb = _assume_registry_fb.create_literal(
                                f"CarryoverRecovery2N2OFFBoundary:nurse_{n}:day_{T0}",
                                meta={
                                    "node_id": f"carryover_recovery_2n2off_boundary:nurse_{n}:day_{T0}",
                                    "type": "CarryoverTransitionNode",
                                    "label": "prev_month 2N2OFF boundary enforce",
                                    "value": {"day": T0 + 2, "remaining_off_needed": 2},
                                    "scope": "nurse",
                                    "scope_key": f"nurse_{n}",
                                    "pattern": "carryover_boundary",
                                    "nurse_id": str(getattr(roster_system.nurses[n], "nurse_id", n)),
                                    "human_message_ko": "전월 2N 꼬리 회복 OFF가 월초에 강제됩니다.",
                                    "resolution_hint": "월초 OFF 슬롯 또는 전월 carryover 입력을 조정하세요.",
                                },
                            )
                            m.Add(_expr_2n_b0_fb).OnlyEnforceIf([X(n, T0, night_idx), end_block_b0, _co_lit_fb])
                        else:
                            m.Add(_expr_2n_b0_fb).OnlyEnforceIf([X(n, T0, night_idx), end_block_b0])
                for d in range(T0 + 1, T1 - 1):
                    if any((n, d2) in fixed_wanted_cells and fixed.get((n, d2)) not in (off_idx, night_idx, None) for d2 in (d + 1, d + 2)):
                        # 회복 OFF 슬롯에 non-OFF fixed_wanted → 이 위치에서 2N 블록 종료 금지
                        xn_prev_fw = X(n, d - 1, night_idx)
                        xn_curr_fw = X(n, d, night_idx)
                        end_block_fw = m.NewBoolVar(f"end_2n_fw_{n}_{d}")
                        m.Add(end_block_fw == X(n, d + 1, night_idx).Not())
                        m.Add(xn_prev_fw + xn_curr_fw + end_block_fw <= 2)
                        continue
                    xn_prev = X(n, d - 1, night_idx)
                    xn_curr = X(n, d, night_idx)
                    xn_next = X(n, d + 1, night_idx)
                    end_block = m.NewBoolVar(f"end_2n_hard_{n}_{d}")
                    m.Add(end_block == xn_next.Not())
                    m.Add(
                        X(n, d + 1, off_idx) + X(n, d + 2, off_idx) == 2
                    ).OnlyEnforceIf([xn_prev, xn_curr, end_block])

        # ── N 블록 간 최소 간격 하드 — "회복 OFF 직후 재-N" 차단 ──
        # ★★ 이 바닥은 목적식으로 안 올라간다. 실측(성남 중환자실-RN 2026-10 · 동일조건 10판)에서
        #   최소 간격이 **10판 전부 정확히 3** 이었다 — 실제 패턴은 `N N O O N N` 이다.
        #   2N/3N 후 2OFF 하드가 `N O O N`(gap 3)을 바닥으로 만들고, `n2n` 은 lex 5번째 패스라
        #   앞 패스(off_range·grade·team·n_range)가 굳혀 놓은 뒤에는 그 바닥을 못 민다.
        #   목적식 축으로는 여섯 번 시도해 여섯 번 다 기각됐다(`n_to_n_interval_target` 을 낮추는
        #   것은 오히려 **역효과** — 벌점이 `target−gap` 한쪽이라 target 이 곧 압력의 도달 거리다).
        # ★ 가벼운 제약이다. 같은 10판에서 3블록 간격 중앙이 8 이고 gap3 은 전체 간격의 **10%**,
        #   판당 평균 2.3명뿐이라 하한을 걸어도 그 10% 만 밀어내면 된다.
        # ★ 양끝이 **둘 다** `fixed` 로 못 박힌 경우만 건너뛴다(MID 제약과 같은 선례).
        #   한쪽만 고정이면 반대쪽을 밀어내면 되므로 제약을 유지한다.
        # ★ 게이트 `AIDE_N2N_MIN_GAP` — 미설정(0)이면 **동작 불변**. 3 이하는 2OFF 하드가
        #   이미 보장하므로 무의미해서 4 부터만 건다.
        # ★★ **stage1 에는 걸지 않는다.** stage1 의 커버리지 부족은 하드가 아니라 **최소화
        #   대상**이고(`m.Minimize(FALLBACK_COVERAGE_SHORT_WEIGHT * sum(short_terms) + …)`),
        #   stage2 가 그 값을 `m.Add(sum(short_terms) == coverage_eq)` 로 **등식으로** 물려받는다.
        #   그래서 stage1 에 이 하드를 걸면 커버리지와 충돌할 때 INFEASIBLE 이 아니라
        #   **`best_short` 가 올라간 채 정상 종료**한다 — 운영자는 "간격은 벌어졌는데 사람이
        #   모자란 근무표" 를 받는다. 완화 사다리(`roster_create_service` 의 `_trigger_soft`)는
        #   `work_cells == 0` 에서만 발화하므로 이 부분 부족을 **전혀 못 잡는다.**
        #   stage2 부터 걸면 커버리지가 이미 등식으로 고정된 뒤라, 충돌은 **명시적 INFEASIBLE**
        #   로 드러난다. 조용한 열화보다 낫다.
        # 설정(`roster_config.n2n_min_gap`) 이 정본이고, env 는 A/B 주입구다(env 가 이긴다).
        _n2n_env = _os_lex.environ.get("AIDE_N2N_MIN_GAP")
        _n2n_min_gap = (int(_n2n_env) if _n2n_env not in (None, "")
                        else int(getattr(cfg, "n2n_min_gap", 0) or 0))
        # 모델에 실어 보낸다 — solve 쪽은 이 함수의 지역변수를 못 본다(`_grade_cell_spec` 규약).
        m._n2n_min_gap = _n2n_min_gap        # type: ignore[attr-defined]
        if _n2n_min_gap >= 4 and stage >= 2:
            _mg_cnt = 0
            _mg_use_mid = bool(getattr(cfg, "use_mid", False))
            for n in range(N):
                # ★ N전담(N-only)은 야간이 강제라 간격 하한을 걸면 즉시 INFEASIBLE 이다.
                #   `_prep_n2n` 이 같은 이유로 제외하는 것과 같은 규약.
                if is_n_only_profile(
                    getattr(roster_system.nurses[n], "allowed_shifts", None),
                    use_mid=_mg_use_mid,
                ):
                    continue
                T0, T1 = join[n], leave[n]
                for d in range(T0, T1):
                    # d 가 N 블록의 마지막 날 = `X(d)=1 AND X(d+1)=0`.
                    #   그때 d+2 … d+(gap-1) 에 N 을 금지하면 다음 블록은 d+gap 이후로 밀린다.
                    #   (d+1 은 블록 끝 정의상 이미 0 이라 뺀다)
                    _mg_end = [X(n, d, night_idx), X(n, d + 1, night_idx).Not()]
                    for _k in range(2, _n2n_min_gap):
                        _dk = d + _k
                        if _dk > T1:
                            break
                        if fixed.get((n, d)) == night_idx and fixed.get((n, _dk)) == night_idx:
                            continue
                        m.Add(X(n, _dk, night_idx) == 0).OnlyEnforceIf(_mg_end)
                        _mg_cnt += 1
            if stage == 2:
                print(f"{logger_prefix} [N2N-MinGap] gap>={_n2n_min_gap} 하드 {_mg_cnt}건 "
                      f"(stage1 제외 — 커버리지 우선)")

        # 금지 패턴 N-O-D/E
        if getattr(cfg, "nod_noe", True):
            for n in range(N):
                T0, T1 = join[n], leave[n]
                for d in range(T0, T1 - 2):
                    v1 = m.NewIntVar(0, 1, f"nod_{n}_{d}")
                    m.Add(
                        v1
                        >= X(n, d, night_idx)
                        + X(n, d + 1, off_idx)
                        + X(n, d + 2, day_idx)
                        - 2
                    )
                    safety["pattern_nod"].append(v1)
                    v2 = m.NewIntVar(0, 1, f"noe_{n}_{d}")
                    m.Add(
                        v2
                        >= X(n, d, night_idx)
                        + X(n, d + 1, off_idx)
                        + X(n, d + 2, eve_idx)
                        - 2
                    )
                    safety["pattern_noe"].append(v2)
                    v3 = m.NewIntVar(0, 1, f"eod_{n}_{d}")
                    m.Add(
                        v3
                        >= X(n, d, eve_idx)
                        + X(n, d + 1, off_idx)
                        + X(n, d + 2, day_idx)
                        - 2
                    )
                    safety["pattern_eod"].append(v3)
            # 월 경계(전월 마지막 → day0/day1). 위 루프는 세 칸이 다 당월에 있어야 돌아
            # d=0 이전을 못 본다 — 같은 safety 계층에 넣어 강도를 맞춘다.
            from services.cp_sat.objective_terms import build_cross_month_pattern_vars
            _xm_vars = build_cross_month_pattern_vars(
                m, roster_system, X, join, leave,
                off_idx=off_idx, day_idx=day_idx, eve_idx=eve_idx, prefix="fb_",
            )
            for _kind, _var in _xm_vars:
                safety[f"pattern_{_kind}"].append(_var)
            if stage == 1:
                _xm_cnt: dict[str, int] = {}
                for _kind, _ in _xm_vars:
                    _xm_cnt[_kind] = _xm_cnt.get(_kind, 0) + 1
                print(f"{logger_prefix} [CrossMonth-Pattern] 경계 패턴 지표 {len(_xm_vars)}건 {_xm_cnt}")

        # 월 최소 OFF 부족량(가능일수 클램프)
        # max coverage 설정 시: min/max coverage 기반 OFF cap 자동 조정
        # off_first=True: max coverage 미설정이라도 동일한 자동 조정 수행 (단일 cap 소스)
        _fb_auto_min_off = None
        _fb_auto_max_off = None
        if _fb_has_any_max or _fb_off_first_cfg:
            import math as _math
            _fb_blocked_set = set(blocked_by_nurse.keys()) if blocked_by_nurse else set()
            _fb_total_capacity = 0
            _fb_total_required = 0
            for _dd in range(D_phys):
                _day_min_sum = 0
                _day_max_sum = 0
                if hasattr(cfg, "daily_shift_requirements_by_day") and isinstance(cfg.daily_shift_requirements_by_day, list) and _dd < len(cfg.daily_shift_requirements_by_day):
                    _day_min_sum = sum(int(v or 0) for v in cfg.daily_shift_requirements_by_day[_dd].values())
                elif hasattr(cfg, "daily_shift_requirements") and isinstance(cfg.daily_shift_requirements, dict):
                    _day_min_sum = sum(int(v or 0) for v in cfg.daily_shift_requirements.values())
                if isinstance(_fb_max_by_day, list) and _dd < len(_fb_max_by_day) and isinstance(_fb_max_by_day[_dd], dict):
                    _day_min_map = {}
                    if hasattr(cfg, "daily_shift_requirements_by_day") and isinstance(cfg.daily_shift_requirements_by_day, list) and _dd < len(cfg.daily_shift_requirements_by_day):
                        _day_min_map = cfg.daily_shift_requirements_by_day[_dd]
                    elif hasattr(cfg, "daily_shift_requirements") and isinstance(cfg.daily_shift_requirements, dict):
                        _day_min_map = cfg.daily_shift_requirements
                    _all_codes = set(list(_fb_max_by_day[_dd].keys()) + (list(_day_min_map.keys()) if isinstance(_day_min_map, dict) else []))
                    for _code in _all_codes:
                        if _code == 'O':
                            continue
                        _mv = int((_fb_max_by_day[_dd].get(_code) or 0))
                        _minv = int((_day_min_map.get(_code) or 0) if isinstance(_day_min_map, dict) else 0)
                        if _mv > 0 and _mv >= _minv:
                            _day_max_sum += _mv
                        else:
                            _day_max_sum += _minv
                _day_active = sum(
                    1 for nn in range(N)
                    if join[nn] <= _dd <= leave[nn]
                    and _dd not in (blocked_by_nurse.get(nn, set()) if blocked_by_nurse else set())
                )
                _fb_total_capacity += max(0, _day_active - _day_min_sum)
                if _day_max_sum > 0:
                    _fb_total_required += max(0, _day_active - _day_max_sum)
            _fb_n_full = max(1, sum(1 for nn in range(N) if nn not in _fb_blocked_set))
            _fb_auto_min_off = max(1, int(_math.ceil(_fb_total_required / _fb_n_full)))
            _fb_auto_max_off = max(_fb_auto_min_off, int(_fb_total_capacity / _fb_n_full))
            # 2N2O/3N2O 하드 제약 활성 시 회복 OFF 여유 확보
            if getattr(cfg, "two_offs_after_two_nig", False) or getattr(cfg, "two_offs_after_three_nig", False):
                _fb_auto_max_off += 2
            if _fb_total_required > _fb_total_capacity:
                print(
                    f"{logger_prefix} [OffCap][MaxCov] required({_fb_total_required}) > capacity({_fb_total_capacity})"
                    f" → auto 조정 비활성화"
                )
                _fb_auto_min_off = None
                _fb_auto_max_off = None
            print(
                f"{logger_prefix} [OffCap][MaxCov] 자동 조정: capacity={_fb_total_capacity}, "
                f"required={_fb_total_required}, N={_fb_n_full}, "
                f"auto_min={_fb_auto_min_off}, auto_max={_fb_auto_max_off}"
            )
        # N 전일 금지 간호사 집합 (2N2O/3N2O OFF 확장용)
        from services.cp_sat.objective_terms import _n_forbid_n_set
        _fb_n_forbid = _n_forbid_n_set(roster_system, join, leave)
        try:
            # 개인별 O 정량 할당(나이트 전담 제외, 주휴 제외한 순수 O 목표)
            if off_idx is not None and effective_off_days > 0:
                for n in range(N):
                    if _is_preceptee_at(n):
                        continue
                    nu = roster_system.nurses[n]
                    raw = getattr(nu, "allowed_shifts", None)
                    is_n_only = is_n_only_profile(raw, use_mid=bool(getattr(cfg, "use_mid", False)))
                    if is_n_only:
                        continue
                    weekly_target = (
                        len(weekly_off_by_idx.get(n, []))
                        if isinstance(weekly_off_by_idx, dict)
                        else 0
                    )
                    vacation_cnt = sum(
                        1 for d in iter_nurse_days(n, join, leave, blocked_by_nurse) if (n, d) in vacation_off_cells
                    )
                    weekend_forced = 0
                    if bool(getattr(nu, "is_weekend_off", False)):
                        try:
                            weekend_forced = sum(
                                1
                                for d in weekend_days
                                if join[n] <= d <= leave[n]
                                and not (
                                    (n, d) in fixed
                                    and fixed.get((n, d)) is not None
                                    and fixed.get((n, d)) != off_idx
                                )
                            )
                        except Exception:
                            weekend_forced = 0
                    _n_blocked_t = len(blocked_by_nurse.get(n, set())) if blocked_by_nurse else 0
                    off_bounds_for_target = compute_off_bounds(
                        source=cfg,
                        avail_days=(leave[n] - join[n] + 1 - _n_blocked_t),
                        vacation_cnt=vacation_cnt,
                        reference_days=D_phys,
                        weekend_only=bool(getattr(nu, "is_weekend_off", False)),
                        weekend_slots_nonvac=weekend_forced,
                    )
                    min_target = int(off_bounds_for_target["min_off_required"])
                    max_target = int(off_bounds_for_target["max_off_allowed"])
                    # blocked days 비례로 effective_off_days 조정
                    _eff_off = effective_off_days
                    if _n_blocked_t > 0:
                        _ratio = max(0, leave[n] - join[n] + 1 - _n_blocked_t) / max(1, D_phys)
                        _eff_off = max(0, round(effective_off_days * _ratio))
                    raw_target_o = max(0, _eff_off - (weekly_target + weekend_forced))
                    target_o = min(max(raw_target_o, min_target), max_target)
                    if target_o <= 0:
                        continue
                    # 휴가/공가는 개인 O 목표 충족에서 제외
                    assigned_o = sum(
                        X(n, d, off_idx)
                        for d in iter_nurse_days(n, join, leave, blocked_by_nurse)
                        if (n, d) not in vacation_off_cells
                    )
                    slack_short = m.NewIntVar(0, D, f"off_quota_short_{n}")
                    slack_excess = m.NewIntVar(0, D, f"off_quota_excess_{n}")
                    m.Add(target_o - assigned_o <= slack_short)
                    m.Add(assigned_o - target_o <= slack_excess)
                    safety["off_quota_short"].append(slack_short)
                    # [OffLevelLock] off_first=False(=근무 oversupply 의도) 실효화:
                    # OFF 상한(target 초과=잉여)을 floor와 대칭으로 가중한다. 기존엔 excess만
                    # raw(weight 1)라, stage2 safety의 pre-scaled 항(isolated_off 300K,
                    # min_off_lower 100K 등)에 묻혀 OFF가 cap까지 드리프트했다. scaled 슬랙을
                    # safety에 넣어 stage2(OPTIMAL, KLD/grade 이전)에서 OFF 수준을 target에
                    # 고정 → stage3가 zero-lock으로 보존. "수준"만 잠그고 배치/예외는 soft라
                    # 유지(하드 고정 아님). 원복: off_quota_excess_weight=0.
                    import os as _os
                    _oqx_w = int(
                        _os.environ.get(
                            "OFF_QUOTA_EXCESS_WEIGHT",
                            getattr(cfg, "off_quota_excess_weight", 100000),
                        )
                        or 0
                    )
                    if _oqx_w > 1 and not bool(getattr(cfg, "off_first", False)):
                        _ex_scaled = m.NewIntVar(0, D * _oqx_w, f"off_quota_excess_cost_{n}")
                        m.Add(_ex_scaled == slack_excess * _oqx_w)
                        safety["off_quota_excess"].append(_ex_scaled)
                    else:
                        safety["off_quota_excess"].append(slack_excess)
                    off_quota_short_by_n[n] = slack_short
                    off_quota_excess_by_n[n] = slack_excess
                    target_o_by_n[n] = target_o
                    print(
                        f"{logger_prefix} [OffCap][force] n={n}, "
                        f"id={getattr(nu, 'nurse_id', '?')}, name={getattr(nu, 'name', '?')}, "
                        f"cap_semantics={off_cap_semantics}, target_O={target_o}, weekly_off_target={weekly_target}"
                    )
            for n in range(N):
                if _is_preceptee_at(n):
                    continue
                T0, T1 = join[n], leave[n]
                nu = roster_system.nurses[n]
                raw = getattr(nu, "allowed_shifts", None)
                is_n_only = is_n_only_profile(raw, use_mid=bool(getattr(cfg, "use_mid", False)))
                nurse_name = getattr(nu, "name", "?")
                nurse_id = getattr(nu, "nurse_id", "?")
                is_weekend_off = bool(getattr(nu, "is_weekend_off", False))

                _n_blocked = len(blocked_by_nurse.get(n, set())) if blocked_by_nurse else 0
                avail_days = T1 - T0 + 1 - _n_blocked
                vacation_cnt = sum(
                    1 for d in range(T0, T1 + 1) if (n, d) in vacation_off_cells and (n, d) in active_days
                )
                structural_cnt = sum(
                    1
                    for d in range(T0, T1 + 1)
                    if (n, d) in structural_off_cells and (n, d) not in vacation_off_cells and (n, d) in active_days
                )
                nonvac_active_days = max(0, avail_days - vacation_cnt)
                # ★ 보건휴가 대상자는 OFF 하한을 1 올린다(후처리가 그중 하나를 휴가코드로 치환).
                _hl_extra = 1 if getattr(nu, "health_leave_extra_off", False) else 0
                off_bounds = compute_off_bounds(
                    source=cfg,
                    avail_days=avail_days,
                    vacation_cnt=vacation_cnt,
                    reference_days=D_phys,
                    weekend_only=is_weekend_off,
                    weekend_slots_nonvac=sum(
                        1
                        for d in weekend_days
                        if T0 <= d <= T1 and (n, d) not in vacation_off_cells
                    ),
                    extra_min_off=_hl_extra,
                )
                min_off_required = int(off_bounds["min_off_required"])
                # max coverage 자동 조정 적용
                # max coverage 기반 자동 조정: max_off만 제한, min_off는 기존 유지
                max_off_allowed_from_policy = int(off_bounds["max_off_allowed"])
                extra_allowed = int(off_bounds["max_extra_off_days"])
                # print(
                #     f"{logger_prefix} [OffCap][vac] n={n}, id={nurse_id}, name={nurse_name}, "
                #     f"base_min_off={base_min_off}, avail_days={avail_days}, "
                #     f"vacation={vacation_cnt}, min_off_required={min_off_required}"
                # )
                # N전담 예외: offcap 고정값 적용 제외 (max는 avail_days-15 공식)
                if is_n_only:
                    min_off_required = 0
                # off_first=True: 사용자 명세상 월 OFF 수(off_days) 무시 → min_off HARD 해제.
                if bool(getattr(cfg, "off_first", False)):
                    min_off_required = 0
                if min_off_required > 0 and not is_weekend_off:
                    # 휴가/공가는 최소 OFF 충족에서 제외
                    offs = sum(
                        X(n, d, off_idx)
                        for d in range(T0, T1 + 1)
                        if (n, d) not in vacation_off_cells
                    )
                    hard_lower = int(min_off_required)
                    # ★★ OFF 하한은 HARD 다 — per-nurse 1일 slack 을 쓰지 않는다.
                    #   이 블록은 off_first=False 일 때만 실행된다(True 면 위에서
                    #   min_off_required=0 이 되어 건너뛴다). off_first=False 는
                    #   "근무 oversupply 허용 · OFF tight" 모드이므로 OFF 를 정확히
                    #   지키는 것이 설계 의도다.
                    #
                    #   실측(2026-08-04) — slack=1 이던 시절:
                    #     중환자실  쉼여력 328 · 필요 260(여유 68)인데 6명이 OFF 9
                    #     42-AN     여유 50 인데 7명이 OFF 9
                    #   여력이 남는데도 solver 가 페널티(100000)를 물고 slack 을 사서
                    #   다른 제약을 맞추는 쪽을 택했다. 실무 근무표는 한 시트 안에서
                    #   22~29/26~29명이 같은 OFF 수를 갖는다 — 균일이 정상이다.
                    #
                    #   ★ 구 주석의 회복 대상(9B 2026-07 min_off=11)은 현재 off_first=1
                    #     이라 이 블록에 들어오지 않는다. 되살릴 때는 그 그룹의
                    #     off_first 를 먼저 확인할 것.
                    _lower_slack_max = 0
                    _lower_slack_weight = 100000
                    if _lower_slack_max > 0:
                        min_off_lower_slack = m.NewIntVar(
                            0, _lower_slack_max, f"min_off_lower_slack_{n}"
                        )
                        m.Add(offs + min_off_lower_slack >= hard_lower)
                        _lower_weighted = m.NewIntVar(
                            0,
                            _lower_slack_max * _lower_slack_weight,
                            f"min_off_lower_slack_weighted_{n}",
                        )
                        m.Add(_lower_weighted == min_off_lower_slack * _lower_slack_weight)
                        safety["off_cap_bounded_slack"].append(_lower_weighted)
                        m._off_slack_lower_by_n[n] = min_off_lower_slack  # type: ignore[attr-defined]
                    else:
                        m.Add(offs >= hard_lower)
                    miss = m.NewIntVar(0, D, f"min_off_miss_{n}")
                    m.Add(miss >= min_off_required - offs)
                    min_off_miss_by_n[n] = miss
                    safety["min_off_missing"].append(miss)
                if extra_allowed >= 0:
                    nonvac_offs = sum(
                        X(n, d, off_idx)
                        for d in range(T0, T1 + 1)
                        if (n, d) not in vacation_off_cells
                    )
                    if is_n_only:
                        # OFF=avail-N 항등식 → off-cap = avail - 실효 N상한.
                        # 실효 N상한 = min(글로벌 max_night, per-nurse n_max/n_exact).
                        # n_exact 등으로 N이 낮게 고정되면 forced OFF(=avail-N)가 커지므로
                        # cap을 그만큼 확장(미반영 시 INFEASIBLE). n 한도 없으면 기존 동작 동일.
                        _global_mn = int(getattr(cfg, "max_night_shifts_per_month", 15) or 15)
                        _eff_ncap = effective_night_cap(nu, _global_mn)
                        total_cap_effective = max(0, avail_days - _eff_ncap)
                    else:
                        # 2N2O/3N2O 하드 제약으로 인한 추가 OFF를 OffCap에 반영 (미반영 시 INFEASIBLE)
                        _extra_off_fb = 0
                        if n not in _fb_n_forbid and (
                            getattr(cfg, "two_offs_after_two_nig", False)
                            or getattr(cfg, "two_offs_after_three_nig", False)
                        ):
                            _extra_off_fb += 2
                        if is_weekend_off:
                            _ow_data_fb = (getattr(roster_system, "off_window_constraints", {}) or {}).get(n, []) or []
                            for (_ws, _we) in _ow_data_fb:
                                _wl = max(T0, _ws)
                                _wr = min(T1, _we)
                                if _wl <= _wr and not any(_d in weekend_days for _d in range(_wl, _wr + 1)):
                                    _extra_off_fb += 1
                        # 4O 월경계 제약으로 월초 OFF 배치 제한된 간호사는 max_off +1 보정
                        if n in _4o_cross_affected_fb:
                            _extra_off_fb += 1
                        # off_first 분기: False=근무 oversupply(OFF tight) / True=OFF oversupply(dev HEAD)
                        _off_first_fb = bool(getattr(cfg, "off_first", False))
                        if _off_first_fb:
                            base_cap = max_off_allowed_from_policy
                            base_cap += _extra_off_fb
                            if n in per_nurse_off_cap_override:
                                base_cap = max(base_cap, per_nurse_off_cap_override[n])
                            # 글로벌 relax 증가 없음 — per-nurse cap override 만 반영
                            total_cap_effective = min(base_cap, avail_days)
                            # max coverage 자동 조정: max_off cap
                            if _fb_auto_max_off is not None:
                                import math as _math
                                _ratio_fb = nonvac_active_days / max(1, D_phys)
                                _scaled_max_fb = max(min_off_required, int(_fb_auto_max_off * _ratio_fb))
                                total_cap_effective = min(total_cap_effective, _scaled_max_fb)
                        else:
                            # off_first=False: 근무 oversupply 를 허용하는 모드다(위 분기 주석).
                            # ★★ 그러므로 OFF 는 **하한으로 고정**한다 — _extra_off_fb 를 더하지 않는다.
                            #   더하면 상한이 하한+2 로 열려 사람마다 OFF 가 흩어진다.
                            #   실측(2026-08-04):
                            #     실무 엑셀   한 시트 안 22~29/26~29명이 동일값 (사실상 전원 균일)
                            #     우리 issued 992 인원·월 중 하한정확 54.3% · 초과 26.5% · 미달 19.2%
                            #   실무 기준으로 균일이 정상이고, 남는 인원은 근무로 흘려보내면 된다
                            #   (커버리지 미달은 HARD 로 막히지만 초과는 무방하다).
                            base_cap = min_off_required
                            if n in per_nurse_off_cap_override:
                                base_cap = max(base_cap, per_nurse_off_cap_override[n])
                            # 글로벌 relax 증가 없음 — per-nurse cap override 만 반영
                            total_cap_effective = min(base_cap, avail_days)
                            total_cap_effective = max(total_cap_effective, min_off_required)
                    # OFF cap slack 결정 정책:
                    # 1) cfg gate (off_cap_bounded_slack_enable=True) → 기존 cfg max/weight 사용
                    # 2) gate False → **per-nurse 1일 slack 기본 활성** (페널티 무거움)
                    #    → 솔버가 cap 부족한 nurse 만 자동으로 1일 풀어줌. 나머지는 0.
                    #    이전 relax_level=1 의 글로벌 retry 단계를 항상-ON 형태로 대체
                    #    (9B 2026-07 등 정적 cap 진단은 통과하지만 combinatorial 로 막히는
                    #    케이스 회복).
                    if off_cap_bounded_slack_enable and off_cap_bounded_slack_max > 0:
                        _slack_max = int(off_cap_bounded_slack_max)
                        _slack_weight = int(off_cap_bounded_slack_weight)
                    else:
                        _slack_max = 1
                        _slack_weight = 100000
                    if _slack_max > 0:
                        cap_slack = m.NewIntVar(0, _slack_max, f"off_cap_slack_{n}")
                        weighted = m.NewIntVar(
                            0,
                            _slack_max * _slack_weight,
                            f"off_cap_slack_weighted_{n}",
                        )
                        m.Add(weighted == cap_slack * _slack_weight)
                        safety["off_cap_bounded_slack"].append(weighted)
                        m.Add(nonvac_offs <= total_cap_effective + cap_slack)
                        m._off_slack_upper_by_n[n] = cap_slack  # type: ignore[attr-defined]
                    else:
                        # OFF cap hard branch — MUS 추출용 assumption literal로 wrap
                        _fb_oc_expr = (nonvac_offs <= total_cap_effective)
                        if _assume_registry_fb is not None and _add_hard_fb is not None:
                            _add_hard_fb(
                                m, _assume_registry_fb,
                                name=f"OffCap:nurse_{n}",
                                constraint_expr=_fb_oc_expr,
                                meta={
                                    "node_id": f"off_cap:nurse_{n}",
                                    "type": "OffCapNode",
                                    "label": "max_off (effective)",
                                    "value": int(total_cap_effective),
                                    "scope": "nurse", "scope_key": f"nurse_{n}",
                                    "pattern": "off_cap",
                                    "nurse_id": str(nurse_id) if nurse_id is not None else str(n),
                                    "human_message_ko": f"이 간호사 OFF 상한 {int(total_cap_effective)}일",
                                    "resolution_hint": "이 간호사 OFF 상한을 늘리세요.",
                                },
                            )
                        else:
                            m.Add(_fb_oc_expr)
                    if stage == 3:
                        print(
                            f"{logger_prefix} [OffCap][total] n={n}, id={nurse_id}, name={nurse_name}, "
                            f"cap_semantics={off_cap_semantics}, nonvac_cap={total_cap_effective}, "
                            f"min_off={min_off_required}, vacation={vacation_cnt}, structural_nonvac={structural_cnt}, "
                            f"nonvac_active_days={nonvac_active_days}, "
                            f"is_n_only={int(is_n_only)}, weekend_off={int(is_weekend_off)}"
                        )
                    # print(
                    #     f"{logger_prefix} [OffCap][pure] n={n}, id={nurse_id}, name={nurse_name}, "
                    #     f"pure_off_cap={MAX_PURE_OFF}, effective_cap={pure_cap_effective}, "
                    #     f"extra_allowed={extra_allowed}, vacation={vacation_cnt}, "
                    #     f"is_n_only={int(is_n_only)}, weekend_off={int(is_weekend_off)}"
                    # )
        except Exception as e:
            print('예외데쇼!!', e)
            pass

        # team_min hard 제약 — Stage 1, 2 에도 등록한다.
        # Stage 3 는 build_fallback_stage3_objective_terms 가 자체 호출하므로 중복 회피.
        # 누락 시 Stage 3 INFEASIBLE → Stage 2 해 commit 경로에서 team_min 위반이 새어나감.
        # team_min soft slack 을 stage 목적함수에 주입하기 위해 캡처.
        # soft 모드에선 add_team_min_constraints 가 slack 제약만 걸고 반환 패널티는
        # Maximize 용(-w*slack)이라 Minimize stage 에선 버려졌다 → 슬랙을 직접 재가중.
        _tm_cover_slacks: list = []
        if stage in (1, 2):
            try:
                _gs_tm = "COMBINED"
                add_team_min_constraints(
                    m, roster_system, X, join, leave,
                    grade_strategy=_gs_tm,
                    blocked_by_nurse=blocked_by_nurse,
                )
                _tm_cover_slacks = list(getattr(roster_system, "_team_min_cover_slacks", []) or [])
            except Exception as e:
                print(f"{logger_prefix} [Stage{stage}] team_min hard 등록 실패: {e}")

        # Grade hard 제약은 fallback 모든 stage에서 동일하게 유지되어야 한다.
        # (기존에는 stage3 objective 경로에서만 add_grade_constraints가 호출되어,
        #  stage3 infeasible 시 stage2/1 해로 내려가며 grade hard가 빠질 수 있었다.)
        try:
            _gs_fb = "COMBINED"
            _gc_fb = getattr(roster_system, "grade_config", None)
            _allow_soft_fb = True
            if isinstance(_gc_fb, dict):
                _allow_soft_fb = bool(_gc_fb.get("allow_soft_fallback", False))
            if isinstance(_gc_fb, dict) and not _allow_soft_fb:
                # ★★ 반환값(목적항)을 **버리지 않고 모델에 보관**한다.
                #   stage3 objective 경로가 목적항이 필요해 같은 함수를 다시 부르는데,
                #   그러면 제약과 side-channel(`_grade_cell_spec`)이 **두 벌** 쌓인다.
                #   그 spec 은 호출마다 append 만 하고 초기화하지 않기 때문이다.
                #   실측: stage2 cells=93 · stage3 cells=186(정확히 2배)이고,
                #   동결식 `sum(stage3 shorts) <= lex_grade_short` 의 우변은 93개 기준이라
                #   같은 셀을 두 번 세면서 실질 상한이 절반이 된다 → stage3 INFEASIBLE.
                #   `short <= 0` 인 병동만 멀쩡했다(0 은 두 벌로 세도 0).
                #   보관해 두면 objective 가 재호출 없이 이 항을 그대로 쓴다.
                _gt_fb = add_grade_constraints_fn(
                    m=m,
                    rs=roster_system,
                    X=X,
                    join=join,
                    leave=leave,
                    grade_strategy=_gs_fb,
                    grade_config=_gc_fb,
                )
                try:
                    m._grade_obj_terms = list(_gt_fb or [])  # type: ignore[attr-defined]
                    # ★ 목적항이 **비어 있어도** 제약은 이미 걸렸다. 최대 제약만 있는
                    #   설정이면 반환값이 빈 리스트라, 목적항 유무로 판단하면
                    #   소비처가 "안 걸렸다" 로 읽고 다시 불러 중복이 되살아난다.
                    #   그래서 '걸었다' 를 **별도 마커**로 남긴다.
                    m._grade_constraints_added = True  # type: ignore[attr-defined]
                except Exception:
                    pass
        except Exception as _grade_hard_exc:
            print(f"{logger_prefix} [GradeHard] fallback stage 공통 제약 추가 실패: {_grade_hard_exc}")

        # grade cascade 는 `short >= target - 누적` 형태의 **soft** 다. 페널티가 목적함수에
        #   없으면 solver 가 short 를 최대로 써도 손해가 없어 제약이 사실상 사라진다.
        #   기존에는 Stage 3 objective 에서만 반영되어, Stage 3 가 시간 내(기본 tl3=12초)
        #   못 끝내거나 실패하면 grade 가 통째로 빠진 Stage 2 해가 commit 됐다.
        #   (실측: 운영 8월 3건 모두 월 총합은 초과 달성인데 일별 20~25% 가 0명,
        #    재생성마다 19/22/23 으로 흔들림)
        #   ★ off=0(요구 등급 미달)만 가져온다. 대체 단계는 위 상수 주석 참조.
        _grade_off0 = list(getattr(m, "_grade_off0_shorts", []) or [])

        # nurse-level 월간 D/E/N/O 한도 hard (모든 stage 공통).
        # primary cp_sat_basic이 INFEASIBLE 되어 fallback 진입 시 사용자 입력 한도가
        # 무시되던 회귀 fix. 같은 모듈을 primary와 공유하여 동작 일치성 확보.
        try:
            from services.constraints.monthly_limit_constraints import (
                add_monthly_limit_constraints,
            )
            _ml_added = add_monthly_limit_constraints(m, roster_system, X, join, leave)
            if _ml_added:
                print(f"{logger_prefix} [MonthlyLimit][stage{stage}] {_ml_added}건 hard 제약 추가")
        except Exception as _ml_exc:
            print(f"{logger_prefix} [MonthlyLimit][stage{stage}] 제약 추가 실패(무시): {_ml_exc}")

        # stage별 목적/고정
        if stage == 1:
            # m.Minimize(FALLBACK_COVERAGE_SHORT_WEIGHT * sum(short_terms) + sum(over_terms))
            OFF_PENALTY=30
            # ※ stage1 에 'OFF 개인별 baseline 편차' 항을 얹어 본 적이 있다(가중치 300).
            #   OFF 총량만 최소화하면 잔여 OFF 가 임의로 쏠린다는 가설이었으나,
            #   실측(7병동 × 5회)에서 목적 지표가 나아지지 않았다:
            #     OFF폭 20.40 → 20.00 · OFF편차 5.97 → 5.82 (둘 다 **끄는 쪽**이 좋음,
            #     OFF폭 은 켜서 이긴 병동이 0곳)
            #   반면 생성 시간은 303.8s → 268.6s 로 **11.6% 느려졌다**(7병동 중 6곳).
            #   목적을 달성하지 못하면서 모델만 키우므로 제거했다. 다시 시도한다면
            #   stage1(커버리지 확정) 이 아니라 lex off_range 패스 쪽을 손대는 게 맞다.
            m.Minimize(
            FALLBACK_COVERAGE_SHORT_WEIGHT * sum(short_terms)
            + sum(over_terms)
            + OFF_PENALTY * sum(
                X(n, d, off_idx)
                for n in range(N)
                for d in iter_nurse_days(n, join, leave, blocked_by_nurse)
                if (n, d) not in structural_off_cells
                and (n, d) not in vacation_off_cells
                )
            )
        elif stage == 2:
            if coverage_eq is not None:
                m.Add(sum(short_terms) == coverage_eq)
            if over_le is not None:
                m.Add(sum(over_terms) <= over_le)
            safety_sum = []
            for k, arr in safety.items():
                safety_sum.extend(arr)
            # team_min soft: Stage 3 가 infeasible 이면 이 Stage 2 해가 commit 되므로,
            # 여기서 팀 커버 부족(slack)을 목적함수에 넣어야 실제로 최소화된다(안 그러면 무력).
            # 커버리지(coverage_eq)는 이미 고정 → 팀 스프레드는 동일 커버 내 재배치로 개선.
            # 가중치 TEAM_COVER_LEX_WEIGHT(30만)는 safety(off-quota 10~30만) 상위 co-priority.
            m.Minimize(
                sum(safety_sum)
                + TEAM_COVER_LEX_WEIGHT * sum(_tm_cover_slacks)
                + GRADE_OFF0_LEX_WEIGHT * sum(_grade_off0)
            )
            # ── [S6] stage3 목적을 **같은 모델(m2)** 에 지어 lex 패스 8 로 쓴다 ──
            #   (2026-09-11 · `AIDE_S6_PASS8=1` · 기본 off)
            #   ★★ 왜 이 방향인가 — 대안은 lex 목적 6개(off_range·team·n_range·n2n·de·pref)를
            #     **m3 에 X3 로 다시 짓는 것**인데, 그건 "같은 목적을 두 모델에 두 번 짓기" 라
            #     이 세션에서 결함 3건을 낸 바로 그 구조다(S4-② 빈 제약 · grade 중복 등록 ·
            #     de 가 산출물에 안 박힘). 그걸 6배로 늘리는 셈이고 n2n 쌍 변수는 사본이
            #     미묘하게 어긋나기 쉽다.
            #   ★ 반대로 stage3 목적은 **이미 주입 구조**다 — `build_fallback_stage3_objective_terms`
            #     가 `m`·`X` 를 인자로 받고(fallback_objectives.py:26) 나머지 인자도 전부
            #     stage 분기 **이전**에 정의된다(over_vars_by_day:1748 · structural_off_cells:1079
            #     · off_exception_cells:674 · weekly_off_by_idx:849). m3 전용 의존이 없다.
            #   → m3 를 없애면 **발산 클래스가 통째로 사라지고**(m2≠m3 가 성립 불가)
            #     stage3 예산 tl3(12초)이 자연히 체인 예산으로 들어온다.
            #   ★ 여기서는 **목적항만 만들어 보관**한다. lex 패스가 `m2.Minimize` 를 교체하며
            #     도는 구조라, 지금 Minimize 를 걸면 위의 stage2 본 목적을 덮어쓴다.
            if _os_lex.environ.get("AIDE_S6_PASS8") == "1":
                try:
                    m._s6_stage3_terms = build_fallback_stage3_objective_terms(
                        m=m,
                        roster_system=roster_system,
                        X=X,
                        join=join,
                        leave=leave,
                        fixed_cnt=fixed_cnt,
                        over_vars_by_day=over_vars_by_day,
                        forced_off_cells=(structural_off_cells | vacation_off_cells),
                        off_exception_cells=off_exception_cells,
                        weekly_off_by_idx=weekly_off_by_idx,
                        logger_prefix=logger_prefix,
                        add_preceptor_terms_fn=add_preceptor_terms_fn,
                        add_grade_constraints_fn=add_grade_constraints_fn,
                        blocked_by_nurse=blocked_by_nurse,
                    )
                    print(f"{logger_prefix} [S6] stage3 목적을 m2 에 빌드: "
                          f"{len(m._s6_stage3_terms)}개 항")
                except Exception as _s6_exc:
                    m._s6_stage3_terms = None
                    print(f"{logger_prefix} [S6] m2 빌드 실패(무시·기존 경로 유지): "
                          f"{type(_s6_exc).__name__}: {_s6_exc}")
        else:
            if coverage_eq is not None:
                m.Add(sum(short_terms) == coverage_eq)
            if over_le is not None:
                m.Add(sum(over_terms) <= over_le)
            # ── stage3 INFEASIBLE 원인 추적 (2026-09-08) — 게이트는 걷어냈고 결론만 남긴다
            #   증상: stage3 가 11곳 중 6곳에서 INFEASIBLE. 그때 stage2 해가 그대로
            #     커밋되므로 **선호·공정성이 통째로 버려지는데 오류로는 안 보인다.**
            #     DiagS3 가 6곳 전부에서 "m3 가 stage2 해를 거부한다(m3≠m2 확정)".
            #   ★ 원인은 **grade 제약의 중복 등록**이었다. `add_grade_constraints` 가
            #     `_grade_cell_spec` 에 초기화 없이 append 하는데, stage3 만 그 함수를
            #     두 번 부른다(여기 build_model + stage3 objective 경로).
            #     그래서 spec 이 stage2 93개 → stage3 186개(정확히 2배)가 되고,
            #     동결식 `sum(stage3 shorts) <= lex_grade_short` 의 우변은 93개 기준이라
            #     같은 셀을 두 번 세면서 실질 상한이 절반이 된다.
            #     `short <= 0` 인 병동만 멀쩡했다(0 은 두 벌로 세도 0). 인과가 닫힌다.
            #   기각된 가설(다시 의심하지 말 것):
            #     · `stage2_zero_locks` — 빼도 4곳 전부 INFEASIBLE 그대로였다.
            #     · 목적항의 `NewIntVar` 상한 — 창 길이와 임계값이 맞아 최대가 1이다.
            #     · build_model 의 grade 조건부 호출 비대칭 — 대상 전 병동이
            #       `allow_soft_fallback=0` 이라 모든 stage 에 걸린다.
            #   ★ MUS 로는 진단할 수 없다 — 켜면 solve 가 4~6배 느려져 INFEASIBLE 이
            #     UNKNOWN 으로 바뀐다. 관측이 대상을 바꾼다.
            if stage2_zero_locks:
                for k, arr in stage2_zero_locks.items():
                    for v in arr:
                        m.Add(v == 0)
            obj = build_fallback_stage3_objective_terms(
                m=m,
                roster_system=roster_system,
                X=X,
                join=join,
                leave=leave,
                fixed_cnt=fixed_cnt,
                over_vars_by_day=over_vars_by_day,
                forced_off_cells=(structural_off_cells | vacation_off_cells),
                off_exception_cells=off_exception_cells,
                weekly_off_by_idx=weekly_off_by_idx,
                logger_prefix=logger_prefix,
                add_preceptor_terms_fn=add_preceptor_terms_fn,
                add_grade_constraints_fn=add_grade_constraints_fn,
                blocked_by_nurse=blocked_by_nurse,
            )
            # 일자별 커버리지 균등화 (min~max 범위 내 고른 배정)
            if _fb_has_any_max and _fb_daily_assigned_by_code:
                for _eq_code, _eq_entries in _fb_daily_assigned_by_code.items():
                    if len(_eq_entries) < 2:
                        continue
                    _eq_vars = []
                    for _eq_d, _eq_assigned, _eq_need in _eq_entries:
                        _eq_v = m.NewIntVar(0, N, f"fb_dcov_{_eq_code}_{_eq_d}")
                        m.Add(_eq_v == _eq_assigned)
                        _eq_vars.append(_eq_v)
                        # min 초과분 패널티: min에 가깝게 유도
                        if _eq_need > 0:
                            _eq_excess = m.NewIntVar(0, N, f"fb_dcov_excess_{_eq_code}_{_eq_d}")
                            m.Add(_eq_excess >= _eq_v - _eq_need)
                            obj.append(-80 * _eq_excess)
                    # 글로벌 range: 블록 쏠림 방지
                    _eq_max = m.NewIntVar(0, N, f"fb_dcov_max_{_eq_code}")
                    _eq_min = m.NewIntVar(0, N, f"fb_dcov_min_{_eq_code}")
                    m.AddMaxEquality(_eq_max, _eq_vars)
                    m.AddMinEquality(_eq_min, _eq_vars)
                    _eq_range = m.NewIntVar(0, N, f"fb_dcov_range_{_eq_code}")
                    m.Add(_eq_range == _eq_max - _eq_min)
                    obj.append(-150 * _eq_range)
                    # 인접일 평활화: 급변 방지
                    for _i in range(1, len(_eq_vars)):
                        _adj = m.NewIntVar(0, N, f"fb_dcov_adj_{_eq_code}_{_i}")
                        m.Add(_adj >= _eq_vars[_i] - _eq_vars[_i - 1])
                        m.Add(_adj >= _eq_vars[_i - 1] - _eq_vars[_i])
                        obj.append(-60 * _adj)
            if _fb_max_cov_off_equalize_terms:
                obj.extend(_fb_max_cov_off_equalize_terms)
            # ── [안건 (가)] stage3 목적에 safety 를 넣는다 · 기본 off ─────────────
            #   ★ 이것은 **결함 수리가 아니라 상한 아래에서 공짜로 얻는 개선**이다.
            #     S4-② 동결(`sum(safety3) <= stage2 값`)이 이미 상위 목적을 지켜 주므로
            #     (가)가 safety 를 나쁘게 만들 수는 없고, 좋게 만들 수만 있다.
            #   ★ 왜 개선 여지가 있나 — stage2 본 solve 는 tl2 안에서 찾은 값에서 멈춘다.
            #     고립OFF 제약의 LP 완화가 항등적으로 0 이라 bound 가 안 올라와 **최적을
            #     증명하지 못하고**, 그 값이 최적이라는 보장이 없다. 그런데 지금 stage3 는
            #     목적에 safety 가 없어 **더 낮출 수 있어도 낮출 동기가 없다.**
            #     목적에 넣으면 stage3 가 받는 12~20초를 safety 감소에도 쓴다.
            #   ★★ 위험은 **스케일 지배**다. safety 슬랙은 이미 30만·10만 가중이라
            #     pref(수천 단위)를 눌러 버릴 수 있다. 그러면 선호가 뒷전이 되어 실패다.
            #     판정 축에 want·PrefRate 비회귀를 반드시 넣는다(S2 (a) 에서 겪은 문제).
            #   ★ `relative_gap_limit=0.05` 도 함께 봐야 한다 — 목적 스케일이 커지면
            #     5% 가 15,000 이라 safety 한 항목을 통째로 포기해도 gap 이 발화한다.
            if stage == 3 and _os_lex.environ.get("AIDE_S3_SAFETY") == "1":
                _s3w = float(_os_lex.environ.get("AIDE_S3_SAFETY_W", "1") or 1)
                _flat = [v for _k, _arr in (safety or {}).items() for v in (_arr or [])]
                if _flat:
                    obj.append(-_s3w * sum(_flat))     # maximize 라 음수로 넣는다
                    print(f"{logger_prefix} [(가)] stage3 목적에 safety {len(_flat)}항 "
                          f"추가 (w={_s3w})")
            # 최종 lex 패스(옵션)용: stage3 목적식 항을 side-channel 에 보존.
            try:
                m._stage3_obj_terms = list(obj)  # type: ignore[attr-defined]
            except Exception:
                pass
            m.Maximize(sum(obj))

        _trace(logger_prefix, "build_model", stage=stage,
               sec=f"{_pc_bm() - _bm_t0:.3f}", broad_soft=broad_soft)
        return (
            m,
            X,
            short_terms,
            over_terms,
            safety,
            short_vars_by_day_code,
            over_vars_by_day_code,
            target_o_by_n,
            off_quota_short_by_n,
            off_quota_excess_by_n,
            min_off_miss_by_n,
        )
    ############################################################## build model 끝 ##############################################################

    def _log_off_slack_used(stage_label, solver, model):
        """post-solve: 어느 nurse 가 OFF cap slack 을 실제 사용했는지 stdout 로그."""
        try:
            lo = getattr(model, "_off_slack_lower_by_n", {}) or {}
            hi = getattr(model, "_off_slack_upper_by_n", {}) or {}
            rows = []
            for n in sorted(set(list(lo.keys()) + list(hi.keys()))):
                lv = solver.Value(lo[n]) if n in lo else 0
                uv = solver.Value(hi[n]) if n in hi else 0
                if lv > 0 or uv > 0:
                    nu = roster_system.nurses[n] if 0 <= n < len(roster_system.nurses) else None
                    nid = getattr(nu, "nurse_id", "?") if nu else "?"
                    name = getattr(nu, "name", "?") if nu else "?"
                    rows.append((n, nid, name, int(lv), int(uv)))
            if rows:
                print(
                    f"{logger_prefix} [OffCapSlack][used][{stage_label}] "
                    f"count={len(rows)} (페널티 100K 강제 trigger — combinatorial 회피)"
                )
                for n, nid, name, lv, uv in rows:
                    parts = []
                    if lv > 0:
                        parts.append(f"min_off -{lv}")
                    if uv > 0:
                        parts.append(f"max_off +{uv}")
                    print(
                        f"{logger_prefix} [OffCapSlack][used][{stage_label}]   "
                        f"nurse_idx={n}, id={nid}, name={name}, {', '.join(parts)}"
                    )
        except Exception as _slack_exc:
            print(f"{logger_prefix} [OffCapSlack][log] 실패(무시): {_slack_exc}")

    # ───── 1단계: 커버리지 (hard 1회 → broad soft 1회) ─────
    m1, X1, short1, over1, safety1 = None, None, None, None, None
    _mark("init")
    short_map1 = {}
    over_map1 = {}
    s1 = None
    best_short, best_over = None, None
    used_broad_soft = False
    attempt_specs = [(False, "hard"), (True, "broad_soft")]
    time_per_attempt = max(5, tl1 // len(attempt_specs))

    def _sync_preceptee_rosters():
        """프리셉티 roster 를 프리셉터와 동기화한다.

        ★ 함수로 뽑은 이유: stage2/stage3 실패 경로가 각자 `return` 으로 끝나는데,
          그 경로들도 이 동기화를 반드시 거쳐야 한다. 예전에는 건너뛰어
          preceptee_follow 병동에서 미러링이 빠진 근무표가 그대로 나갔다.
          중첩 함수라 바깥 지역 변수(cfg·roster_system·preceptee_* 등)를 그대로 쓴다.
        """
        if preceptee_follow and preceptee_indices:
            _fb_id_to_idx = {nu.db_id: n for n, nu in enumerate(roster_system.nurses)}
            _fb_shift_types = cfg.shift_types
            _fb_off_idx = _fb_shift_types.index('O') if 'O' in _fb_shift_types else None
            _fb_standard = {'D', 'E', 'N', 'O'}
            if bool(getattr(cfg, 'use_mid', False)):
                _fb_standard.add('M')
            _fb_pte_fw = getattr(roster_system, '_preceptee_fixed_wanted_map', {})
            # 고정 OFF 직전 N 금지가 실제로 건 셀. 미러가 프리셉터 N 을 덮어쓰지 못하게 한다.
            _fb_ban_prev = getattr(roster_system, '_ban_n_prev_cells', set()) or set()
            _fb_n_idx = _fb_shift_types.index('N') if 'N' in _fb_shift_types else None
            synced = 0
            special_converted = 0
            _fb_fw_restored = 0
            _fb_ban_kept = 0
            _fb_pre_ptr_idx2 = getattr(roster_system, 'preceptee_preceptor_idx', {}) or {}  # period SSOT
            for pte_idx in preceptee_indices:
                # 권위 모드: period SSOT 로 프리셉터 결정(캐시 미사용 — NULL 캐시 프리셉티 누락 방지).
                if _has_preceptee_period:
                    ptr_idx = _fb_pre_ptr_idx2.get(pte_idx)
                    if ptr_idx is None:
                        continue
                else:
                    pid = getattr(roster_system.nurses[pte_idx], 'preceptor_id', None)
                    if not pid or pid not in _fb_id_to_idx:
                        continue
                    ptr_idx = _fb_id_to_idx[pid]
                # 권위 모드면 nurse_preceptee_period 기간 내 day만, 폴백이면 전체월 복사.
                _fb_follow = preceptee_follow_days.get(pte_idx)
                _fb_days_iter = (sorted(_fb_follow) if (_has_preceptee_period and _fb_follow)
                                 else list(range(roster_system.num_days)))
                for _cd in _fb_days_iter:
                    roster_system.roster[pte_idx, _cd, :] = roster_system.roster[ptr_idx, _cd, :]
                # 특수코드 일자는 프리셉티를 OFF로 전환 (복사한 day 한정)
                # 단, type=근무 + shift_gb=D/E/N 계열 하위코드는 근무이므로 그대로 유지
                _fb_work_sub = getattr(roster_system, '_work_sub_ids', set())
                _fb_orig_map = getattr(roster_system, '_fixed_original_shift_map', {})
                if _fb_off_idx is not None:
                    for d in _fb_days_iter:
                        # 프리셉티 fixed_wanted 일자는 프리셉터 복사 대신 본인 값 적용
                        if (pte_idx, d) in _fb_pte_fw:
                            _fw_code = _fb_pte_fw[(pte_idx, d)].strip().upper()
                            if _fw_code in _fb_shift_types:
                                roster_system.roster[pte_idx, d, :] = 0
                                roster_system.roster[pte_idx, d, _fb_shift_types.index(_fw_code)] = 1
                                _fb_fw_restored += 1
                            continue
                        # ★ 고정 OFF 직전 N 금지가 건 셀이면, 프리셉터를 따라 N 이 복사되는 것을
                        #   막는다. 모델 제약은 미러 **이전** 값에만 걸리므로 여기서 다시 막지
                        #   않으면 신청해 받은 휴일이 도로 회복 OFF 자리로 돌아간다.
                        #   교육 연속성보다 휴일 보호를 우선한다(2026-08-31 결정).
                        if (pte_idx, d) in _fb_ban_prev and _fb_n_idx is not None:
                            if roster_system.roster[pte_idx, d, _fb_n_idx] == 1:
                                roster_system.roster[pte_idx, d, :] = 0
                                if _fb_off_idx is not None:
                                    roster_system.roster[pte_idx, d, _fb_off_idx] = 1
                                _fb_ban_kept += 1
                                continue
                        _fb_need = False
                        _fb_orig = _fb_orig_map.get((ptr_idx, d))
                        if _fb_orig:
                            _fb_ou = _fb_orig.upper()
                            if _fb_ou not in _fb_standard and _fb_ou not in _fb_work_sub:
                                _fb_need = True
                        else:
                            _idx_arr = np.where(roster_system.roster[ptr_idx, d] == 1)[0]
                            if len(_idx_arr) > 0:
                                _fb_sc = _fb_shift_types[int(_idx_arr[0])]
                                if _fb_sc not in _fb_standard and _fb_sc.upper() not in _fb_work_sub:
                                    _fb_need = True
                        if _fb_need:
                            roster_system.roster[pte_idx, d, :] = 0
                            roster_system.roster[pte_idx, d, _fb_off_idx] = 1
                            special_converted += 1
                synced += 1
            if synced:
                msg = f"{logger_prefix} [PrecepteeSync] 후처리 후 프리셉티 roster 동기화: {synced}명"
                if special_converted:
                    msg += f" (특수코드→OFF 전환: {special_converted}건)"
                if _fb_fw_restored:
                    msg += f", fixed_wanted 재적용: {_fb_fw_restored}건"
                if _fb_ban_kept:
                    msg += f", 고정OFF직전N 보호: {_fb_ban_kept}건"
                print(msg)
            # if bool(getattr(cfg, "ban_e_to_d", True)) and _fb_off_idx is not None:
            #     _fb_eve_idx = _fb_shift_types.index('E') if 'E' in _fb_shift_types else None
            #     _fb_day_idx = _fb_shift_types.index('D') if 'D' in _fb_shift_types else None
            #     _fixed_blocked = 0
            #     _repaired = 0
            #     if _fb_eve_idx is not None and _fb_day_idx is not None:
            #         for pte_idx in preceptee_indices:
            #             for d in range(1, roster_system.num_days):
            #                 if int(roster_system.roster[pte_idx, d - 1, _fb_eve_idx]) != 1:
            #                     continue
            #                 if int(roster_system.roster[pte_idx, d, _fb_day_idx]) != 1:
            #                     continue
            #                 cur_fixed = (pte_idx, d) in _fb_pte_fw
            #                 prev_fixed = (pte_idx, d - 1) in _fb_pte_fw
            #                 if not cur_fixed:
            #                     roster_system.roster[pte_idx, d, :] = 0
            #                     roster_system.roster[pte_idx, d, _fb_off_idx] = 1
            #                     _repaired += 1
            #                 elif not prev_fixed:
            #                     roster_system.roster[pte_idx, d - 1, :] = 0
            #                     roster_system.roster[pte_idx, d - 1, _fb_off_idx] = 1
            #                     _repaired += 1
            #                 else:
            #                     _fixed_blocked += 1
            #     if _repaired or _fixed_blocked:
            #         print(
            #             f"{logger_prefix} [PrecepteeSync][Repair-E->D] repaired={_repaired}, blocked_fixed={_fixed_blocked}"
            #         )

        _log_weekend_work_assignments(
            roster_system=roster_system,
            weekend_days=weekend_days,
            off_idx=off_idx,
            logger_prefix=logger_prefix,
        )
        # ── 후처리 완료 후 최종 커버리지 상태 로깅 ──
        try:
            final_viols = roster_system._find_violations()
            final_cov_viols = [v for v in final_viols if v.get('type') == 'shift_requirement']
            if final_cov_viols:
                print(f"{logger_prefix} [최종 커버리지 부족] 후처리 후 {len(final_cov_viols)}건 부족:")
                for v in sorted(final_cov_viols, key=lambda x: (x['day'], x['shift'])):
                    print(
                        f"  day={v['day']+1}, shift={v['shift']}, "
                        f"required={v['required']}, actual={v['actual']}, "
                        f"gap={v['required'] - v['actual']}"
                    )
            else:
                print(f"{logger_prefix} [최종 커버리지] 후처리 후 커버리지 부족 없음 ✓")
        except Exception as exc:
            print(f"{logger_prefix} [최종 커버리지 로깅 실패]: {exc}")

    for broad_soft, attempt_label in attempt_specs:
        with timer_cls(f"폴백 1단계: 커버리지 부족 최소화 ({attempt_label})"):
            (
                m1,
                X1,
                short1,
                over1,
                safety1,
                short_map1,
                over_map1,
                _,
                _,
                _,
                _,
            ) = build_model(
                stage=1, broad_soft=broad_soft
            )
            s1 = cp_model.CpSolver()
            s1.parameters.max_time_in_seconds = time_per_attempt
            s1.parameters.num_search_workers = 8
            # ── [6c-3] stage1 gap 조이기 · 기본 현행(0.15) ──────────────────────
            #   ★★ stage2 는 stage1 결과를 **동결로 물려받는다**(`coverage == best_short`,
            #     `over <= best_over`). stage1 이 gap 0.15 로 끝나면 그 동결값이 회차마다
            #     달라지고, 그러면 **stage2 가 매번 다른 문제를 푼다.**
            #     그때는 stage2 를 아무리 조여도(gapABS) 회차 간 값이 안 모인다 —
            #     최적값 자체가 회차마다 다르기 때문이다.
            #   ★ 실측 정황(시화중환2 · 2026-09-11): gapABS 인데도 6,300,065 / 5,700,273 /
            #     9,300,074 로 흩어졌다. 셋이 각각 최적으로 증명됐다면 같은 문제일 수 없다.
            #     작은 병동은 stage1 이 늘 같은 값에 도달해 이 층이 안 보였다.
            #   ★ abs 임계 주의: stage1 목적에는 **OFF 총량 × 30** 항이 있어 `0.99` 로 두면
            #     "OFF 한 개 단위까지 증명" 을 요구하게 된다. `29`(OFF 한 단위 미만)가 현실적이고,
            #     부족(10만)·과잉 항은 그 임계로도 충분히 잡힌다.
            _s1_relgap = float(_os_tl3.environ.get("AIDE_S1_GAP", "0.15") or 0.15)
            s1.parameters.relative_gap_limit = _s1_relgap
            if _s1_relgap <= 0:
                s1.parameters.absolute_gap_limit = float(
                    _os_tl3.environ.get("AIDE_S1_ABS_GAP", "29") or 29)
            # MUS 추출용 assumption registry attach
            _reg_s1 = getattr(m1, "_cpsat_assumption_registry", None)
            if _reg_s1 is not None:
                _reg_s1.attach_to_model()
            st = _solve_traced(s1, m1, logger_prefix, f"stage1[{attempt_label}]")
            # shadow ground truth(피드백 fix3): 첫 hard 솔브(=effective primary hard) raw status 저장.
            try:
                _st_txt = _cp_sat_status_to_text(st)
                if not getattr(roster_system, "_primary_solver_status", None):
                    roster_system._primary_solver_status = _st_txt
                _sas = getattr(roster_system, "_solver_attempt_statuses", None) or {}
                _sas.setdefault(str(attempt_label or "primary_hard"), _st_txt)
                roster_system._solver_attempt_statuses = _sas
            except Exception:
                pass
            if st == cp_model.INFEASIBLE and _reg_s1 is not None:
                try:
                    _fb_cores = _reg_s1.extract_conflict_cores(s1, solver_phase="fallback")
                    if _fb_cores:
                        roster_system._cpsat_conflict_cores = (
                            list(getattr(roster_system, "_cpsat_conflict_cores", []) or []) + _fb_cores
                        )
                        print(f"[FallbackLex][stage1] MUS cores: {len(_fb_cores)}")
                except Exception as _mus_exc:
                    print(f"[FallbackLex][stage1] MUS 추출 실패(무시): {_mus_exc}")
            print(
                f"{logger_prefix} 폴백1 결과: attempt={attempt_label}, "
                f"status={_cp_sat_status_to_text(st)}"
            )
            if st in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                _log_off_slack_used(f"stage1:{attempt_label}", s1, m1)
                best_short = int(s1.Value(sum(short1)))
                best_over = int(s1.Value(sum(over1)))
                used_broad_soft = broad_soft
                if broad_soft:
                    print(f"{logger_prefix} 폴백1 성공: broad soft 적용 (max coverage soft, M min soft)")
                print(f"{logger_prefix} 최소 커버리지 부족: {best_short}, 과잉: {best_over}")
                try:
                    short_items = []
                    for (d, code), var in short_map1.items():
                        val = s1.Value(var)
                        if val > 0:
                            short_items.append((d, code, val))
                    if short_items:
                        short_items.sort()
                        print(
                            f"{logger_prefix} [Stage1 부족 상세] day,shift,shortage =",
                            short_items,
                        )
                    over_items = []
                    for (d, code), var in over_map1.items():
                        val = s1.Value(var)
                        if val > 0:
                            over_items.append((d, code, val))
                    if over_items:
                        over_items.sort()
                        print(
                            f"{logger_prefix} [Stage1 과잉 상세] day,shift,over =",
                            over_items,
                        )
                except Exception as exc:
                    print(f"{logger_prefix} [Stage1 상세로그 실패]: {exc}")
                break
            if not broad_soft:
                print(f"{logger_prefix} 폴백1 실패 ({attempt_label}): broad soft 1회 재시도...")
            else:
                print(f"{logger_prefix} 폴백1 최종 실패: hard/broad soft 모두 실패")

    _mark("stage1_all")
    if best_short is None or best_over is None:
        print(f"{logger_prefix} 폴백 중단: 1단계 해를 찾지 못함")
        return False

    # ───── 2단계: 안전/법규 ─────
    with timer_cls("폴백 2단계: 안전/법규 위반 최소화"):
        (
            m2,
            X2,
            short2,
            over2,
            safety2,
            short_map2,
            over_map2,
            _,
            _,
            _,
            _,
        ) = build_model(
            stage=2,
            coverage_eq=best_short,
            over_le=best_over,
            broad_soft=used_broad_soft,
        )
        s2 = cp_model.CpSolver()
        # ── [6c 실험] stage2 편차가 **시간 부족**인가 **국소해**인가 · 2026-09-10 ──────
        #   같은 병동 같은 입력이 회차마다 고립OFF 1~2개를 오간다. 그 편차의 정체를
        #   같은 비용의 두 처치로 가른다:
        #     AIDE_S2_TL_MULT=3   tl2 를 3배 — 시간이 모자란 것이면 분포가 좁아진다
        #     AIDE_S2_BEST_N=3    독립 3회 풀어 safety 합 최선 선택 — 국소해면 이쪽이 듣는다
        #   ★ 둘 다 못 좁히면 남는 카드는 레벨 정책(고립OFF 를 team 위로)뿐이다.
        #     그건 "grade·team 을 내주고 고립OFF 를 없앤다" 는 제품 판단이라 별건이다.
        from time import perf_counter as _pc_one
        # ★ 정체 종료가 켜지면 상한 기본을 3배로 올린다 — 워치독이 "개선이 멈춘 병동" 을
        #   알아서 끊으므로, 상한은 **개선이 이어지는 병동이 갈 수 있는 곳**만 정하면 된다.
        #   실측(2026-09-11): 전역 3배는 기각됐지만(11곳 중 개선 1·악화 1·동일 9,
        #   소요 +31%) 그건 **끊는 장치 없이** 전부에게 3배를 준 경우다. 워치독이 붙으면
        #   증명 8곳은 애초에 상한이 바인딩이 아니고(0.7~1.0초 OPTIMAL · :3744 주석),
        #   개선이 멈춘 병동은 임계에서 끊긴다.
        _s2_stall_on = _s2_stall_config()[0]
        _s2_mult = float(_os_tl3.environ.get(
            "AIDE_S2_TL_MULT", "3" if _s2_stall_on else "1") or 1)
        _s2_bestn = int(_os_tl3.environ.get("AIDE_S2_BEST_N", "1") or 1)
        _S2_BASE_TL[0] = float(tl2)      # 이월 상한(:368) — 늘린 몫은 회수분이 아니다
        s2.parameters.max_time_in_seconds = tl2 * _s2_mult
        s2.parameters.num_search_workers = 8
        # ── [6c-2] stage2 gap 조이기 · 기본 현행(0.15) ──────────────────────────
        #   ★★ 작은 safety 항목(off_quota_short · week_off_missing · pattern_eod)을
        #     최소화하는 자리가 **파이프라인 전체에서 stage2 본 solve 뿐**이다.
        #     lex 패스 목적은 off_range·grade·team·n_range·n2n·de·pref 이고,
        #     작은 항목은 총합 동결 아래에서 **교환만 될 뿐 아무 패스도 안 줄인다.**
        #     stage3 도 (가) 없이는 안 건드린다. 그런데 stage2 가 gap 0.15 로 **1초 만에**
        #     끝나므로(실측 9A: 0.5~1.2초 · tl2 21초 중 20초 미사용)
        #     그 항목들은 **최적화되는 순간이 아예 없다.**
        #   ★ 이건 우선순위를 바꾸는 게 아니라 **같은 우선순위 안에서 더 잘 푸는 것**이라
        #     제품 판단이 필요 없다(가중치·순서 불변).
        #   ★ 0.01 은 75만 목적에서 7,500 단위라 grade_off0 10만 이상치는 잡지만
        #     한 자릿수 항목(16~24)은 여전히 못 잡는다. 그건 `absolute_gap_limit=0.99`
        #     (정수 목적이라 "bound 와 1 미만 차이")가 필요하고, 증명 부담이 커 tl2 까지 돌 수 있다.
        #   ★★ **기본 승격(2026-09-11)** — `0.15` → `0`(+ absolute 0.99).
        #     실측: 활성 7곳 × 3회 · 회차 교차 · stage3 최종값 폭 규칙 판정 →
        #       **개선 5 · 동일 2 · 악화 0**
        #       시화9A 폭 300,009→1 · 시화9B 폭 300,001→0 · 시화중환1 중앙 43→5 ·
        #       시화중환2 중앙 −150만 · 인천41RN 중앙 −10만+폭 제거 ·
        #       인천별관1/나사렛7B 동일. 선호 비회귀(메디통10 은 오히려 26→27).
        #       소요 중앙 42s→44s(노이즈) · stage3 커밋 33/33.
        #     ★ 초기 11곳 판정에서 **유일한 악화가 세브2** 였는데 그 병동이 방치 데이터로
        #       확인돼 모수에서 빠졌다 — 원래 악화가 아니었던 것이 드러난 셈이다.
        #   ★★ **Lambda 게이트(6d) 없이 승격하는 이유**: 이 처치는 **임계값이 없다.**
        #     stage2 가 시간 안에 증명되면 최적을 찾고, 못 하면 리밋에서 끝난다 —
        #     즉 CPU 가 느려서 생기는 최악이 **현행과 동일**이다. 게이트가 필요한 것은
        #     정체 임계(`AIDE_S2_STALL_IDLE`)처럼 **임계가 CPU 속도에 직접 걸린** 처치다.
        #     Lambda 검증은 "채택 조건" 이 아니라 "확인" 으로 뒤에 붙인다.
        _s2_relgap = float(_os_tl3.environ.get("AIDE_S2_GAP", "0") or 0)
        s2.parameters.relative_gap_limit = _s2_relgap
        if _s2_relgap <= 0:
            s2.parameters.absolute_gap_limit = float(
                _os_tl3.environ.get("AIDE_S2_ABS_GAP", "0.99") or 0.99)
        _reg_s2 = getattr(m2, "_cpsat_assumption_registry", None)
        if _reg_s2 is not None:
            _reg_s2.attach_to_model()
        if _s2_bestn > 1:
            # ★★ best-of-N — **더 좋은 해를 찾는 게 아니라, 동률 최적해 중에서 고르는 것**이다.
            #   실측(2026-09-11 · 9A): stage2 본 solve 가 **0.7~1.0초에 OPTIMAL** 로 끝나고
            #   bound 750,002 대 obj 750,014~750,021 로 gap 이 0.003% 다. 즉 tl2(21초)는
            #   전혀 바인딩이 아니고(=`AIDE_S2_TL_MULT` 는 구조적 no-op), gap 종료도 아니다.
            #   그런데 **seed 마다 목적값이 다르다** — 같은 모델을 최적으로 푸는데 750,014/016/020/021.
            #   목적 안에서 safety 와 team·grade_off0 이 **서로 상쇄되어 동등한 해가 여럿**이라
            #   솔버가 그중 아무거나 고르기 때문이다. 이것이 회차 편차의 정체다.
            #
            #   ★★ 선택 기준은 **stage2 본 목적값 하나뿐**이다. 동률이면 먼저 나온 해를 쓴다.
            #     처음엔 2차 기준으로 safety 벡터(고립OFF → off_quota)를 넣었다가 **뺐다** —
            #     고립OFF(30만)와 팀 커버(30만)가 목적에서 **동급**이라, "동률이면 고립OFF 가
            #     낮은 쪽" 은 곧 **팀 커버를 내주고 고립OFF 를 사는 선택**이다. 그건 성능이 아니라
            #     "환자 안전(팀 최소커버) 대 간호사 근무 품질(고립 OFF)" 의 제품 판단이고,
            #     2026-05-13 정책 주석("팀 커버 무너지면 환자 안전 영향 큼")과 반대 방향이다.
            #     ★ 사용자 판단(2026-09-11): **현행 순서 유지** — 가중치도 우선순위도 안 바꾼다.
            #       따라서 이 선택은 목적값만 보고, 동률 해 사이의 구성 선택에는 개입하지 않는다.
            #   ★★ seed 를 반복마다 바꾸는 이유는 **다양성 확보**다. 메모리 규칙의
            #     "seed 고정 ≠ 결정론" 은 골든 시드·재현성 얘기이고 여기는 정반대 용도다.
            #     이걸 안 하면 vCPU 가 적은 환경(Lambda)에서 N 회가 같은 해로 수렴해 무효가 된다.
            #   ★ N 은 고정이 아니라 **예산 안에서 최대 N회**다. 남은 예산이 한 번치 아래면 중단.
            _flat2 = [v for _a in safety2.values() for v in (_a or [])]
            from time import perf_counter as _pc_s2   # ★ 313행의 `_pc` 는 다른 함수 스코프다
            _budget2 = float(tl2 * _s2_mult)
            _t_s2 = _pc_s2()
            _best_key, _best_hint, st2 = None, None, cp_model.UNKNOWN
            _tries = 0
            for _i in range(_s2_bestn):
                _left = _budget2 - (_pc_s2() - _t_s2)
                if _i > 0 and _left < max(2.0, _budget2 / (_s2_bestn * 2)):
                    print(f"{logger_prefix} [6c] 예산 소진 — {_i}회에서 중단(남은 {_left:.1f}s)")
                    break
                s2.parameters.random_seed = _i
                s2.parameters.max_time_in_seconds = max(2.0, _left)
                _sti = _solve_traced(s2, m2, logger_prefix, f"stage2#{_i + 1}")
                _tries += 1
                if _sti not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                    continue
                try:
                    _obj2 = float(s2.ObjectiveValue())
                except Exception:
                    _obj2 = float("inf")
                # ★ 목적값 **하나만** 본다(위 주석 참조). `<` 이라 동률이면 먼저 나온 해가 남는다.
                _key = (_obj2,)
                if _best_key is None or _key < _best_key:
                    _best_key, st2 = _key, _sti
                    _best_hint = {(n, d, s): int(s2.Value(X2(n, d, s)))
                                  for n in range(N)
                                  for d in iter_nurse_days(n, join, leave, blocked_by_nurse)
                                  for s in range(S)}
            print(f"{logger_prefix} [6c] stage2 best-of-{_tries}/{_s2_bestn}: "
                  f"obj={_best_key[0] if _best_key else None} (목적값 단일 기준)")
            if _best_hint:
                m2.ClearHints()
                for (n, d, s), v in _best_hint.items():
                    try:
                        m2.AddHint(X2(n, d, s), v)
                    except Exception:
                        pass
                # 최선 해를 실제 s2 상태로 되돌린다(이후 코드가 s2.Value 를 읽는다).
                _sti = _solve_traced(s2, m2, logger_prefix, "stage2:best")
                if _sti in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                    st2 = _sti
        else:
            _t_s2_one = _pc_one()
            st2 = _solve_traced(s2, m2, logger_prefix, "stage2")
            # ★ stage2 본 solve 는 실측 **0.5~1.0초**에 끝나는데 tl2 는 21초다(9A · 2026-09-11).
            #   매 회차 20초가 그냥 버려진다. S1 재정의의 이월 통로가 이미 있으니 그리로 보낸다 —
            #   stage3 는 리밋 직전까지 개선 중인 유일한 구간이라 그 시간이 실제로 쓰인다.
            #   ★ 정책과 무관하고 위험이 없다: 안 쓰던 시간을 옮길 뿐 제약을 건드리지 않는다.
            #   ★★ 게이트를 **정체 종료와 분리한다**(`AIDE_S2_CARRY`). 정체 종료는 품질 판단이
            #     붙은 처치라 기본 off 인데, 미사용분 이월은 위험 0 이라 거기 묶일 이유가 없다.
            #     이런 묶임이 또 생기는 걸 막는 것이 6b(플래그 인벤토리)의 취지이기도 하다.
            _spent_s2 = _pc_one() - _t_s2_one
            # ★★ 정체 종료가 켜져 있으면 **여기서 이월하지 않는다** — 워치독이 이미
            #   같은 시간을 넘겼다(:375). 둘 다 켜면 같은 회수분이 두 번 얹혀
            #   세브7 처럼 일찍 끊기는 병동에서 tl3 가 배로 부풀고, 그 증상은
            #   "왜 총 소요가 늘지" 로만 나타나 추적이 어렵다.
            if (_os_tl3.environ.get("AIDE_S2_CARRY") == "1"
                    and not _s2_stall_on and _spent_s2 < tl2):
                _STALL_SAVED.append(tl2 - _spent_s2)
                print(f"{logger_prefix} [S2이월] stage2 미사용 {tl2 - _spent_s2:.1f}s → stage3")
        if st2 == cp_model.INFEASIBLE and _reg_s2 is not None:
            try:
                _fb_cores = _reg_s2.extract_conflict_cores(s2, solver_phase="fallback")
                if _fb_cores:
                    roster_system._cpsat_conflict_cores = (
                        list(getattr(roster_system, "_cpsat_conflict_cores", []) or []) + _fb_cores
                    )
                    print(f"[FallbackLex][stage2] MUS cores: {len(_fb_cores)}")
            except Exception as _mus_exc:
                print(f"[FallbackLex][stage2] MUS 추출 실패(무시): {_mus_exc}")
        print(f"{logger_prefix} 폴백2 결과: status={_cp_sat_status_to_text(st2)}")
        # ★★ stage2 가 못 풀렸는데 n2n 최소간격 하드가 켜져 있으면 **호출부에 알린다.**
        #   실측(2026-09-18 · `n2n_min_gap=15`): stage2 가 UNKNOWN 으로 끝나면 엔진은
        #   보존해 둔 stage1 해로 복원하고 근무표는 **정상 산출된다**. 그래서
        #   `validation_error` 가 안 뜨고, 완화 사다리는 `work_cells == 0` 에서만 발화하므로
        #   **아무도 이 상태를 못 잡는다.** 결과는 최악의 조합이다 —
        #     · 간격 하한은 안 지켜지고(그 판 실측: 최소 3 · gap<15 위반 30건)
        #     · lex 최적화가 **통째로 무효**가 되어 품질이 오히려 나빠진다
        #       (간격중앙 7 · 정상 판은 10~11).
        #   정상 범위(4~5)에서는 80런 동안 한 번도 안 났지만, 설정값을 잘못 넣으면
        #   경고 한 줄 없이 이 상태가 된다. 호출부가 하드를 내리고 재시도하게 한다.
        _mg_on2 = int(getattr(m2, "_n2n_min_gap", 0) or 0)
        if (st2 not in (cp_model.OPTIMAL, cp_model.FEASIBLE)) and _mg_on2 >= 4:
            try:
                roster_system._n2n_min_gap_stage2_failed = True    # type: ignore[attr-defined]
            except Exception:
                pass
            print(f"{logger_prefix} [N2N-MinGap][WARN] stage2 {_cp_sat_status_to_text(st2)} — "
                  f"간격 하한(gap>={_mg_on2})이 반영되지 않았고 lex 최적화도 무효다. "
                  f"호출부에 해제 재시도를 요청한다.")
        if st2 in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            _log_off_slack_used("stage2", s2, m2)
            # H1: Stage2 lex 3-pass — safety_sum → OFF range → N range 순차 minimize.
            # 각 단계는 이전 cost 동결로 다른 항 영향 0 보장.
            # lex 재solve 는 cold-start 라 큰 인스턴스에서 시간 내 incumbent 를 못 찾고
            # UNKNOWN(빈 해)으로 끝날 수 있다. 그러면 downstream(zero-lock/hint/stage3 실패
            # fallback)이 망가진 s2 를 읽어 빈 roster 를 만든다. → 성공한 마지막 해를
            # 스냅샷에 보존하고(downstream 은 이 스냅샷을 읽음), 재solve 전 warm-start hint 로
            # 직전 feasible 해를 주입해 빈 해 반환 자체를 막는다.
            lex_x2_val: dict = {}
            lex_safety_val: dict = {}
            # 6-pass(grade 재배치)가 달성한 미달 칸 수. stage3 가 이 값을 상한으로
            #   물려받아 재배치 결과를 지킨다(아래 '폴백3 grade 동결' 참조).
            lex_grade_short: Optional[int] = None
            # ※ n2n 을 grade 처럼 stage3 에 동결하는 것은 **기각**했다(2026-09-18).
            #   구현해 24런 교차한 결과 기여가 0 이었다 — 기준선을 보정하면
            #   A gap<5 3.0 → 동결+하드 2.0 (개선 1.0) vs A 2.0 → 하드단독 1.0 (개선 1.0) 로
            #   개선폭이 같고, 오히려 하드 단독이 2블록 17.0 대 13.5 로 더 좋았다.
            #   간격은 목적식/동결이 아니라 **하한 하드**(`AIDE_N2N_MIN_GAP`)로만 움직인다.

            def _capture_lex_solution() -> None:
                for _n in range(N):
                    for _d in iter_nurse_days(_n, join, leave, blocked_by_nurse):
                        for _s in range(S):
                            lex_x2_val[(_n, _d, _s)] = int(s2.Value(X2(_n, _d, _s)))
                for _k, _arr in safety2.items():
                    lex_safety_val[_k] = [int(s2.Value(_v)) for _v in _arr]

            def _hint_lex_solution() -> None:
                try:
                    m2.ClearHints()
                    for (_n, _d, _s), _val in lex_x2_val.items():
                        m2.AddHint(X2(_n, _d, _s), _val)
                except Exception:
                    pass

            _capture_lex_solution()  # stage2 해 보존: lex 전부 실패해도 이 해로 복원
            try:
                flat_safety = []
                for arr in safety2.values():
                    flat_safety.extend(arr)
                if flat_safety:
                    best_sum2 = sum(int(s2.Value(v)) for v in flat_safety)
                    # ── [계측] lex 진입 시점 값 · 2026-09-10 ────────────────────────
                    #   ★ 두 결함을 가르는 기준선이다:
                    #     진입값 = lex후 = stage3  → 전부 **stage2 본 solve 편차**.
                    #                               항목별 동결로는 못 막는다(진입 전에 이미 나쁨).
                    #     진입값 < lex후            → **lex 안에서 교환**. S4-①이 직접 막는 몫.
                    #   ★ team 슬랙·grade_off0 은 **동결 범위 밖**이다(아래 _prep_team 주석 참조).
                    #     stage2 본 목적에는 30만/15만 가중으로 들어가는데 lex 진입 시 안 묶여,
                    #     lex1·lex2 가 도는 동안 자유롭게 나빠진다. 함께 찍어 비중을 본다.
                    try:
                        _entry = {k: sum(int(s2.Value(v)) for v in (a or []))
                                  for k, a in safety2.items() if a}
                        _tm_e = sum(int(s2.Value(v)) for v in
                                    (getattr(roster_system, "_team_min_cover_slacks", []) or []))
                        _g0_e = sum(int(s2.Value(v)) for v in
                                    (getattr(m2, "_grade_off0_shorts", []) or []))
                        print(f"{logger_prefix} [lex진입] safety합={best_sum2} "
                              f"team슬랙={_tm_e} grade_off0={_g0_e} — "
                              + ", ".join(f"{k}={v}" for k, v in sorted(_entry.items()) if v))
                    except Exception as _e_entry:
                        print(f"{logger_prefix} [lex진입] 계측 실패(무시): {_e_entry}")
                    # [S4-①] 항목별 동결 (AIDE_LEX_S4_STAGE2 · 기본 off · 파일 상단 참조)
                    if _s4_stage2:
                        # ★★ 하이브리드(2026-09-11) — **큰 항목만** 항목별로 묶고,
                        #   한 자릿수 항목은 총합 동결에 맡겨 lex 패스의 운신 폭으로 남긴다.
                        #
                        #   왜: S4-① 은 **렉시코 정의보다 엄격하다.** stage2 목적이 "safety 합"
                        #   이면 합이 같은 교환(고립OFF 30만 1건 ↔ 10만 항목 3건)은 정의상
                        #   동급이고, 그 아래서 lex 패스 값이 좋아지면 렉시코적으로는 **더 좋은
                        #   해**다. 항목별 동결은 그 교환을 금지해 해 공간을 자른다.
                        #   실측(11곳 × 3회 · 2026-09-11)에서 잘린 쪽에 상위 패스 개선이 있었다:
                        #     시화9A   lex5:n2n  20[16~20] → 47[47~57]  (현행폭 4 · 2배 이상 악화)
                        #     시화중환1 lex5:n2n   3[1~5]  → 10[8~141] (현행폭 4 · 최대 141)
                        #   ★ 이 악화는 **stage3 최종값에 안 잡힌다**(시화9A 는 600,014 로 동일).
                        #     n2n 은 stage3 목적 축이 아니기 때문이다 — 축 셋을 같이 봐야 보인다.
                        #   반면 세브2 가 보여준 **해로운** 교환은 큰 항목끼리의 맞바꿈이었다
                        #   (진입 고립OFF 0 → lex후 2건 · 등급미달은 29 로 동일).
                        #   → 큰 항목만 막으면 둘 다 얻는다.
                        #
                        #   ★★ 가를 때 **현재 값이 아니라 가중치**를 본다. 30만 항목이 이번
                        #     회차에 0 이면 값은 0 이지만 여전히 큰 항목이고, 항목별 동결의
                        #     핵심이 정확히 **"0 인 것을 0 으로 묶는 것"** 이다(세브2 가 그 케이스).
                        #     `s2.Value` 로 가르면 값이 0 인 큰 항목이 작은 항목으로 분류돼
                        #     막아야 할 것을 정확히 놓친다.
                        #     가중치는 변수에 이미 곱해져 있고(`scaled == slack * penalty` · :1983)
                        #     **도메인 상한이 곧 가중치**라 거기서 읽는다. 항목 이름을 하드코딩
                        #     하지 않는 이유도 이것 — `off_cap_bounded_slack` 은 가중치가 cfg 에서
                        #     와서(:1956) 병동마다 다르다.
                        _w_min = int(_os_tl3.environ.get("AIDE_S4_ITEM_W_MIN", "10000") or 10000)
                        _s4_n = _s4_skip = 0
                        for _k4, _arr4 in safety2.items():
                            if not _arr4:
                                continue
                            try:
                                _ub4 = max(int(_v4.Proto().domain[-1]) for _v4 in _arr4)
                            except Exception:
                                _ub4 = _w_min          # 못 읽으면 큰 항목으로 보수적 처리
                            if _ub4 < _w_min:
                                _s4_skip += 1
                                continue
                            m2.Add(sum(_arr4) <= sum(int(s2.Value(_v4)) for _v4 in _arr4))
                            _s4_n += 1
                        # ★ 작은 항목은 여전히 총합으로 묶어야 한다 — 안 그러면 무제한이 된다.
                        #   (현행은 항목별/총합이 배타였는데, 하이브리드에서는 **둘 다** 건다.)
                        m2.Add(sum(flat_safety) <= best_sum2)
                        print(f"{logger_prefix} [S4-1] stage2 safety 항목별 동결 {_s4_n}개 "
                              f"(가중치<{_w_min} {_s4_skip}개는 총합 동결에 위임)")
                    else:
                        m2.Add(sum(flat_safety) <= best_sum2)
                    use_mid_h1 = bool(getattr(cfg, "use_mid", False))
                    # ── 공통 준비: 순서와 무관하게 한 번만 계산한다 ──
                    #   원래는 OFF range 패스 루프 안에서 채워졌고 N range·D/E 가 그걸
                    #   물려 썼다. 순서를 바꾸려면 이 계산이 패스와 분리돼 있어야 한다.
                    #   ★ 제외 집합은 **용도가 둘**이다. 하나로 쓰면 안 된다.
                    #     · nightonly_idx      = N전담. D/E 가 없으니 어떤 패스에서도 대상 아님.
                    #     · range_excluded_idx = N전담 + 월 전체를 일하지 않는 사람.
                    #       range(max-min) 패스에서만 뺀다 — 특이값 하나에 무력화되므로.
                    #     D/E 균등은 개인별 |D-E| 초과분의 **합**이라 특이값에 무너지지 않는다.
                    #     여기에 부분근무자를 빼면 그들의 D/E 가 방치된다(실제로 그랬다).
                    nightonly_idx: set[int] = set()
                    range_excluded_idx: set[int] = set()
                    for n_lex in range(N):
                        if leave[n_lex] < join[n_lex]:
                            continue
                        nu = roster_system.nurses[n_lex]
                        try:
                            is_n_only_lex = is_n_only_profile(
                                getattr(nu, "allowed_shifts", None), use_mid=use_mid_h1
                            )
                        except Exception:
                            is_n_only_lex = False
                        if is_n_only_lex:
                            nightonly_idx.add(n_lex)
                            range_excluded_idx.add(n_lex)
                            continue
                        # ★ 주말휴무자(평일만 근무)도 range 에서 뺀다.
                        #   위 [WeekendOff] 블록이 주말=OFF 강제·평일=OFF 금지를 **하드로**
                        #   걸므로 그들의 월 OFF 수는 주말 일수로 완전히 결정된다. solver 가
                        #   못 바꾸는 값이 max-min 안에 있으면 목적식이 그 바닥에 눌려,
                        #   조절 가능한 사람들을 고르게 만들어도 range 가 줄지 않는다.
                        #   (실측: 주말휴무자 있는 20개 병동 중 8곳에서 경계를 점유.
                        #    51병동-RN 은 주휴무 OFF 8 이 min 을 잡아 일반 10~11 인데 range 3,
                        #    빼면 1. 중환자실은 21 → 4.)
                        #   ★ 빼도 통제를 잃지 않는다 — 이미 하드로 고정돼 있다.
                        if bool(getattr(nu, "is_weekend_off", False)):
                            range_excluded_idx.add(n_lex)
                            continue
                        # ★ 활동 기간이 그 달 전체가 아닌 사람(중도 입퇴사·전입)도 뺀다.
                        #   range 는 max-min 이라 **특이값 하나에 무력화**된다. 9일만 활동해
                        #   OFF 가 3 인 사람이 섞이면 min 이 3 에 고정되고, 그러면 전체 활동자가
                        #   7 이든 10 이든 range 가 같아져 그들 사이를 고르게 만들 유인이 사라진다.
                        #   (실측: OFF range OPTIMAL=7 인데 31일 활동자끼리 7~10 으로 벌어짐)
                        #   ★ join/leave 는 전체 범위이고 실제 비활동은 blocked_by_nurse 로
                        #     표현된다. leave-join+1 로 재면 전원 31 이 나와 걸러지지 않는다.
                        _act_days = sum(
                            1 for _ in iter_nurse_days(n_lex, join, leave, blocked_by_nurse)
                        )
                        if _act_days < D:
                            range_excluded_idx.add(n_lex)   # range 패스에서만 제외
                    night_idx_h1 = (
                        cfg.shift_types.index("N") if "N" in cfg.shift_types else None
                    )
                    # ★ D 균등 패스(`d_range`)용. N 과 같은 방식으로 잡는다.
                    day_idx_h1 = (
                        cfg.shift_types.index("D") if "D" in cfg.shift_types else None
                    )
                    # ── 시프트 개수 상·하한 ── (2026-09-11)
                    #   ★★ range 패스(max-min)는 **목적식에서 상수인 사람 하나에 무력화**된다.
                    #     N 을 구조적으로 못 하는 사람이 섞이면 min 이 0 에 못박혀, 조절 가능한
                    #     사람을 아무리 고르게 만들어도 range 가 안 줄어든다.
                    #     (실측 2026-09-11 · 성남시의료원 중환자실-RN 2026-10:
                    #      N=0 이 4명 — 송순진·남경준은 확정원티드로 한 달이 고정, 김은경은
                    #      allowed_shifts=["D"], **임옥희만 설정이 전무한데 0**. 9월엔 N 6회 한
                    #      정상 3교대자다. 그들 때문에 min=0 이 상수라 `lex n_range OPTIMAL=7`
                    #      이 나온다 — 솔버는 맞고 목적함수가 무력화된 것이다.)
                    #   ★ **원인이 아니라 결과로 판정한다.** allowed_shifts 든 확정원티드 고정이든
                    #     구조적 창 부족이든, 결과가 같은 상수면 똑같이 뺀다. 설정 기반 가드만
                    #     두면 확정원티드로 고정된 사람을 못 잡는다(`_prep_de` 가 그 상태다 —
                    #     :4188 은 `allowed_shifts` 만 보는데 빈 값은 "제한 없음" 으로 읽힌다).
                    #   ★ 제외는 **range 계산에서만**이고 배정 자체는 건드리지 않는다.
                    _fx_lex: dict[tuple[int, int], int] = {}
                    for _c in getattr(roster_system, "fixed_cells", []) or []:
                        try:
                            _sm = _normalize_fixed_to_main(_c.get("shift"))
                            if _sm in cfg.shift_types:
                                _fx_lex[(_c["nurse_index"], _c["day_index"])] = \
                                    cfg.shift_types.index(_sm)
                        except Exception:
                            continue
                    # 1N 금지가 켜지면 N 은 최소 2일 연속이라 자유 셀도 그만큼 이어져야 한다.
                    _n_block_min = 2 if bool(getattr(cfg, "not_one_night", False)) else 1

                    def _count_bounds(_n: int, _sidx: int) -> tuple[int, int]:
                        """(lo, hi) — 그 사람이 그 시프트를 받을 수 있는 최소·최대 개수.

                        `lo == hi` 면 그 패스의 목적식에서 **상수**라 range 대상이 아니다.
                        ★ 오진 비용이 비대칭이라 **확실할 때만** 좁힌다 — 상수가 아닌데
                          상수로 보면 조절 가능한 사람을 빼서 목적식을 약화시키고(새 결함),
                          상수인데 아니라고 보면 현행과 같다(기존 결함 유지).
                        """
                        _days = list(iter_nurse_days(_n, join, leave, blocked_by_nurse))
                        _lo = sum(1 for _d in _days if _fx_lex.get((_n, _d)) == _sidx)
                        _free = [_d for _d in _days if (_n, _d) not in _fx_lex]
                        if not _free:
                            return (_lo, _lo)          # 한 달이 통째로 고정됨
                        try:
                            # ★ `is_code_blocked_by_profile` 은 이 모듈에 import 되어 있지 않다
                            #   (:39-43). 이미 들어온 `normalize_allowed_shift_codes` 를 쓴다.
                            #   ★ **빈 집합은 "제한 없음"** 이다(:4254 와 같은 해석) — 빈 값을
                            #     "아무것도 못 함" 으로 읽으면 전원이 제외돼 대상이 사라진다.
                            _al_b = normalize_allowed_shift_codes(
                                getattr(roster_system.nurses[_n], "allowed_shifts", None),
                                use_mid=use_mid_h1)
                            if _al_b and cfg.shift_types[_sidx] not in _al_b:
                                return (_lo, _lo)      # 프로필이 그 코드를 막음
                        except Exception:
                            pass
                        if _sidx == night_idx_h1 and _n_block_min > 1:
                            # 자유 셀의 연속 구간이 N 블록을 한 번도 못 담으면 더 못 받는다.
                            _run = _best = 0
                            _prev = None
                            for _d in _free:
                                _run = _run + 1 if _prev is not None and _d == _prev + 1 else 1
                                _best = max(_best, _run)
                                _prev = _d
                            if _best < _n_block_min:
                                return (_lo, _lo)
                        return (_lo, _lo + len(_free))

                    def _is_range_const(_n: int, _sidx: int) -> bool:
                        _lo, _hi = _count_bounds(_n, _sidx)
                        return _lo == _hi

                    # ── 패스 정의 ──
                    #   각 prep 은 (목적식, 동결콜백, 최소초, 시간비율) 또는 None(대상 없음).
                    #   동결콜백은 그 패스가 달성한 값을 상한으로 걸어 뒤 패스가 깨지 못하게 한다.
                    #   ★ 변수 생성이 prep 안에 있는 것이 중요하다 — 그 패스 차례가 와야
                    #     모델에 변수가 붙으므로, 쓰이지 않는 순서에서는 모델이 커지지 않는다.
                    def _prep_off():
                        _cnts = []
                        for _n in range(N):
                            if leave[_n] < join[_n] or _n in range_excluded_idx:
                                continue
                            _cnts.append(sum(
                                X2(_n, _d, off_idx)
                                for _d in iter_nurse_days(_n, join, leave, blocked_by_nurse)
                            ))
                        if not _cnts:
                            return None
                        _mx = m2.NewIntVar(0, D, "lex_max_off")
                        _mn = m2.NewIntVar(0, D, "lex_min_off")
                        for _c in _cnts:
                            m2.Add(_c <= _mx)
                            m2.Add(_c >= _mn)
                        return (_mx - _mn, lambda v: m2.Add(_mx - _mn <= v), 2.0, 0.2)

                    def _prep_grade():
                        # 총량은 그대로 두고 날짜만 옮겨 미달 칸을 줄인다. 목적함수에 항을
                        # 더하는 방식(초과 페널티·가중치 증가)은 둘 다 실측에서 실패했다.
                        _spec = list(getattr(m2, "_grade_cell_spec", []) or [])
                        if not _spec:
                            return None
                        _shorts = []
                        for _gd, _gs, _gt, _gmem in _spec:
                            _cum = sum(
                                X2(_n, _gd, _gs) for _n in _gmem
                                if join[_n] <= _gd <= leave[_n]
                            )
                            _sh = m2.NewIntVar(0, int(_gt), f"lex_g_short_d{_gd}_s{_gs}")
                            m2.Add(_sh >= int(_gt) - _cum)
                            _shorts.append(_sh)

                        def _freeze_grade(v):
                            # stage3 가 이 값을 상한으로 물려받아 재배치 결과를 지킨다.
                            nonlocal lex_grade_short
                            lex_grade_short = int(v)
                            m2.Add(sum(_shorts) <= int(v))

                        # ★ 설정된 등급이 전부 반영됐는지는 이 개수로만 사후 확인된다.
                        #   (일수 x 값>0 인 (등급,시프트) 쌍 수) 와 일치해야 한다.
                        print(f"{logger_prefix} [GradeSpec] cells={len(_shorts)}")
                        return (sum(_shorts), _freeze_grade, 3.0, 0.3)

                    def _prep_n_range():
                        if night_idx_h1 is None:
                            return None
                        _cnts, _skip = [], []
                        for _n in range(N):
                            if leave[_n] < join[_n] or _n in range_excluded_idx:
                                continue
                            # ★ N 이 구조적으로 상수인 사람을 뺀다 — 안 빼면 min 이 0 에
                            #   못박혀 목적식이 무력화된다(위 `_count_bounds` 주석).
                            if _is_range_const(_n, night_idx_h1):
                                _skip.append(getattr(roster_system.nurses[_n], "name", _n))
                                continue
                            _cnts.append(sum(
                                X2(_n, _d, night_idx_h1)
                                for _d in iter_nurse_days(_n, join, leave, blocked_by_nurse)
                            ))
                        # ★★ 제외 인원을 반드시 찍는다. 이 집합이 조용히 커지면 대상이 사라져
                        #   range 가 0 이 되고 **항상 OPTIMAL** 이 나온다 — "OPTIMAL=7 인데
                        #   목적함수가 무력화" 의 거울상이고, 좋아 보이는 숫자라 더 위험하다.
                        if _skip:
                            print(f"{logger_prefix} [n_range] N 상수라 제외 {len(_skip)}명: "
                                  f"{', '.join(str(x) for x in _skip)} / 대상 {len(_cnts)}명")
                        if len(_cnts) < 2:
                            return None            # 대상이 1명 이하면 range 는 의미가 없다
                        _mx = m2.NewIntVar(0, D, "lex_max_n")
                        _mn = m2.NewIntVar(0, D, "lex_min_n")
                        for _c in _cnts:
                            m2.Add(_c <= _mx)
                            m2.Add(_c >= _mn)
                        return (_mx - _mn, lambda v: m2.Add(_mx - _mn <= v), 2.0, 0.2)

                    def _prep_d_range():
                        """D 개수 균등(max-min) — `n_range` 의 D 판(2026-09-14).

                        ★ 왜 필요한가 — **D 균등을 보는 패스가 하나도 없었다.**
                          `off_range` 는 OFF, `n_range` 는 N, `de` 는 개인 내 |D-E| 를 보는데
                          **D 자체의 사람 간 균등은 아무도 안 본다.**
                          실측(성남ICU 2026-10 · schedule 856e14283992):
                            김은경 D18(전담·정상) · **임옥희 D11** · 그 외 21명 D 4~8(대부분 6~7)
                          임옥희 혼자 3~4개 더 받았고, 그 11개 중 **10개가 D5 두 구간**
                          (10/09~13 · 10/23~27)이었다. "D 가 많다" 와 "D 가 뭉쳤다" 가 같은 현상이다.
                        ★ 수요 구조: 전담 3명 중 김은경만 D 코드로 18일을 채우고
                          송순진(DA)·남경준(DD)은 **D 커버리지에 안 잡힌다.** 남은 D 를
                          24명이 나누는데 배분을 보는 축이 없으니 한 명에게 몰린다.
                        ★ D5 를 직접 겨냥한 세 경로가 모두 실패한 뒤 나온 처방이다 —
                          소프트 가중치(고립근무에 밀림) · `d5-lex`(20회 성공 0) ·
                          D5 체인 패스(창이 이미 확정돼 운신 폭 없음). D 를 흩으면
                          5일 창을 D 로 채울 이유 자체가 줄어든다.
                        ★ 가드는 `n_range` 와 동일 — `_count_bounds` 가 D 상수인 사람을 뺀다
                          (전담 3명이 자동 제외된다).
                        """
                        if day_idx_h1 is None:
                            return None
                        _cnts, _skip = [], []
                        for _n in range(N):
                            if leave[_n] < join[_n] or _n in range_excluded_idx:
                                continue
                            if _is_range_const(_n, day_idx_h1):
                                _skip.append(getattr(roster_system.nurses[_n], "name", _n))
                                continue
                            _cnts.append(sum(
                                X2(_n, _d, day_idx_h1)
                                for _d in iter_nurse_days(_n, join, leave, blocked_by_nurse)
                            ))
                        if _skip:
                            print(f"{logger_prefix} [d_range] D 상수라 제외 {len(_skip)}명: "
                                  f"{', '.join(str(x) for x in _skip)} / 대상 {len(_cnts)}명")
                        if len(_cnts) < 2:
                            return None
                        _mx = m2.NewIntVar(0, D, "lex_max_d")
                        _mn = m2.NewIntVar(0, D, "lex_min_d")
                        for _c in _cnts:
                            m2.Add(_c <= _mx)
                            m2.Add(_c >= _mn)
                        return (_mx - _mn, lambda v: m2.Add(_mx - _mn <= v), 2.0, 0.2)

                    def _prep_n2n():
                        # 야간 블록 간 간격(target 미만)을 벌린다. soft 항은 KLD 에 눌려
                        # 무시되므로 lex 우선순위로 끌어올린다.
                        if night_idx_h1 is None:
                            return None
                        _tgt = int(getattr(cfg, "n_to_n_interval_target", 0) or 0)
                        if _tgt < 2:
                            return None
                        _terms = []
                        # 아래 월경계 기존 경로가 루프 밖에서 이 둘을 참조한다(결함 보존 분기).
                        # N==0 이거나 전원 continue 될 때 NameError 가 나지 않도록 초기화한다.
                        _n = -1
                        _aset: set = set()
                        for _n in range(N):
                            if leave[_n] < join[_n]:
                                continue
                            # N전담(N-only)은 야간 강제 → n2n 간격 벌점 제외(유령 페널티 방지).
                            if is_n_only_profile(
                                getattr(roster_system.nurses[_n], "allowed_shifts", None),
                                use_mid=use_mid_h1,
                            ):
                                continue
                            _aset = set(iter_nurse_days(_n, join, leave, blocked_by_nurse))
                            for _d1 in sorted(_aset):
                                for _gap in range(2, _tgt):
                                    _d2 = _d1 + _gap
                                    if _d2 not in _aset:
                                        continue
                                    _btw = [_d1 + _k for _k in range(1, _gap)]
                                    if any(_b not in _aset for _b in _btw):
                                        continue
                                    _pair = m2.NewBoolVar(f"lex_n2n_{_n}_{_d1}_{_d2}")
                                    m2.Add(_pair <= X2(_n, _d1, night_idx_h1))
                                    m2.Add(_pair <= X2(_n, _d2, night_idx_h1))
                                    for _b in _btw:
                                        m2.Add(_pair <= 1 - X2(_n, _b, night_idx_h1))
                                    _btw_sum = sum(X2(_n, _b, night_idx_h1) for _b in _btw)
                                    m2.Add(
                                        _pair >= X2(_n, _d1, night_idx_h1)
                                        + X2(_n, _d2, night_idx_h1) - 1 - _btw_sum
                                    )
                                    _terms.append((_tgt - _gap) * _pair)
                        # ── 월 경계: 전월 말 N 블록 → 현월 첫 N 블록 간격 ──
                        #   ★ 위 루프는 현월 활동일(_aset) 안에서만 쌍을 만든다. 그래서 전월
                        #     말일에 N 을 끝낸 사람이 현월 1일부터 다시 N 을 받아도 벌점이 0 이었다.
                        #     회복 간격을 벌리라는 제약이 가장 위험한 구간에서 놀고 있었다.
                        #   전월 꼬리는 확정 상수라 "현월 d 일이 첫 N 이고 그 앞은 전부 N 아님"만
                        #     변수로 세우면 된다. gap = d - (전월 마지막 N 날짜) 인데, 전월
                        #     마지막 날을 0 으로 두면 현월 d(1-based)의 gap 은 그대로 d 다.
                        # ★★ 결함(2026-09-17 발견): 이 블록이 위 `for _n in range(N)` 루프 **밖**에
                        #   있었다 — 들여쓰기가 24칸으로 루프와 같은 레벨이다. 그래서 전월 꼬리
                        #   벌점이 **마지막 간호사 한 명에게만**, 그것도 그 사람의 `_aset` 으로
                        #   생성됐다. 배선은 있는데 실질적으로 안 걸려 있었던 셈이다.
                        #   ★ 게이트 `AIDE_N2N_CROSS_FIX=1` 로 전원 적용을 켠다. 기본 off 는
                        #     **기존 동작 그대로** — 실측으로 확인하기 전에는 동작을 바꾸지 않는다.
                        #   ※ 과거 기각된 "월경계 gap 보정"(개선 11 vs 악화 10)은 `_gap_x` 계산에
                        #     `offs_after` 를 반영하는 **다른 축**이었다. 이건 스코프 결함이다.
                        def _add_cross(_cn, _caset):
                            _n_tail = int((getattr(roster_system, "prev_month_n_tail_by_idx", {})
                                           or {}).get(_cn, 0) or 0)
                            if _n_tail < 1:
                                return
                            _days_sorted = sorted(_caset)
                            for _d in _days_sorted:
                                _gap_x = _d + 1        # 전월 마지막 N=0일, 현월 인덱스 d → gap
                                if _gap_x >= _tgt:
                                    break              # 이후는 전부 무벌점
                                _before = [_b for _b in _days_sorted if _b < _d]
                                _pair_x = m2.NewBoolVar(f"lex_n2n_cross_{_cn}_{_d}")
                                m2.Add(_pair_x <= X2(_cn, _d, night_idx_h1))
                                for _b in _before:
                                    m2.Add(_pair_x <= 1 - X2(_cn, _b, night_idx_h1))
                                _bsum = sum(X2(_cn, _b, night_idx_h1) for _b in _before)
                                m2.Add(_pair_x >= X2(_cn, _d, night_idx_h1) - _bsum)
                                _terms.append((_tgt - _gap_x) * _pair_x)

                        if _os_lex.environ.get("AIDE_N2N_CROSS_FIX") == "1":
                            for _cn in range(N):
                                if leave[_cn] < join[_cn]:
                                    continue
                                if is_n_only_profile(
                                    getattr(roster_system.nurses[_cn], "allowed_shifts", None),
                                    use_mid=use_mid_h1,
                                ):
                                    continue
                                _add_cross(
                                    _cn,
                                    set(iter_nurse_days(_cn, join, leave, blocked_by_nurse)),
                                )
                        else:
                            _add_cross(_n, _aset)   # 기존 동작(결함) 보존 — 루프 마지막 값
                        if not _terms:
                            return None
                        _frac = float(_os_lex.getenv("N2N_LEX_TIME_FRAC", "0.5"))

                        return (sum(_terms), lambda v: m2.Add(sum(_terms) <= v), 8.0, _frac)

                    def _prep_de():
                        # D/E per-nurse 균등(X축). tol 초과분만 벌해 "완전동일" 아님(밴드).
                        _env = _os_lex.getenv("DE_LEX_ENABLE")
                        _on = (
                            (_env == "1") if _env is not None
                            else bool(getattr(cfg, "de_balance_enable", True))
                        )
                        if not _on or "E" not in cfg.shift_types:
                            return None
                        _tol = int(_os_lex.getenv(
                            "DE_BALANCE_TOL", str(getattr(cfg, "de_balance_tolerance", 2))
                        ) or 0)
                        _di = cfg.shift_types.index("D")
                        _ei = cfg.shift_types.index("E")
                        _excs, _de_skip = [], []
                        for _n in range(N):
                            # ★ 부분근무자는 제외하지 않는다 — |D-E| 는 개인 내 차이의
                            #   **합산**이라 특이값에 무너지지 않고, 빼면 그들의 D/E 가
                            #   방치된다(실제로 그랬다).
                            #   ★ 다만 D/E 중 한쪽만 가능한 사람은 빼야 한다. D전담은
                            #     |D-E| = D 라 줄일 방법이 없는데 목적식에는 잡혀,
                            #     solver 가 못 없애는 페널티가 상수로 깔린다.
                            #     (실측: 김유정 allowed=["D"] → D22/E0 로 유령 페널티 20.
                            #      N전담만 걸러서 D전담·E전담이 그대로 샜다.)
                            if leave[_n] < join[_n] or _n in nightonly_idx:
                                continue
                            #   실측(9병동-9A × 5회): 제외하면 목적식 대상자들의 D/E 균등이
                            #   4.00 → 3.35, 커버부족도 4.00 → 3.60. 하드 위반 0 불변.
                            #   ※ 표본은 **병동 1곳 · 실효 대상 1명**(김유정)이라 이 수치로
                            #     크기를 단정할 수 없다. 다만 목적식에서 상수 페널티를 빼는
                            #     것이라 방향은 논리적으로 분명하다.
                            # ★★ 판정을 `allowed_shifts` 에서 `_count_bounds` 로 바꿨다(2026-09-11).
                            #   구 가드 `if _al and not {"D","E"} <= _al` 는 **빈 배열을 "제한 없음"
                            #   으로 읽어** 확정원티드로 한쪽만 하는 사람을 못 걸렀다.
                            #   실측(성남시의료원 중환자실-RN 2026-10): N=0 인 4명이 **전원
                            #   `allowed_shifts=[]`** 였다 — 설정 기반 가드로는 한 명도 못 뺀다.
                            #   그래서 **원인(설정·원티드·창 부족)이 아니라 결과(상수인가)로**
                            #   판정한다. D 든 E 든 한쪽이 상수면 |D-E| 를 줄일 수 없다.
                            if (_is_range_const(_n, _di) or _is_range_const(_n, _ei)):
                                _de_skip.append(
                                    getattr(roster_system.nurses[_n], "name", _n))
                                continue
                            _ad = list(iter_nurse_days(_n, join, leave, blocked_by_nurse))
                            _td = sum(X2(_n, _d, _di) for _d in _ad)
                            _te = sum(X2(_n, _d, _ei) for _d in _ad)
                            _df = m2.NewIntVar(0, D, f"lex_de_diff_{_n}")
                            m2.Add(_df >= _td - _te)
                            m2.Add(_df >= _te - _td)
                            _ex = m2.NewIntVar(0, D, f"lex_de_exc_{_n}")
                            m2.Add(_ex >= _df - _tol)
                            _excs.append(_ex)
                        # ★★ 제외 인원을 찍는다 — 이 집합이 조용히 커지면 대상이 사라져
                        #   목적값이 0 이 되고 "완벽" 해 보인다(n_range 와 같은 함정의 거울상).
                        if _de_skip:
                            print(f"{logger_prefix} [de] D/E 상수라 제외 {len(_de_skip)}명: "
                                  f"{', '.join(str(x) for x in _de_skip)} / 대상 {len(_excs)}명")
                        if not _excs:
                            return None
                        return (sum(_excs), lambda v: m2.Add(sum(_excs) <= v), 5.0, 0.3)

                    def _prep_iso_off():
                        """고립 OFF(앞뒤가 근무인 하루짜리 OFF) 최소화.

                        이 슬랙은 safety 묶음의 한 항목이라 lex 진입 전에 '전체 합'으로만
                        동결된다. 합 안에서 다른 safety 항목과 교환되므로 따로 최소화하지
                        않으면 밀린다 — 실측에서 grade 를 6번째→1번째로 옮겨도 고립OFF 는
                        27 근처에서 꿈쩍하지 않았다(순서 문제가 아니라 대상이 아니었던 것).
                        ★ 값은 slack * isolated_off_slack_penalty(기본 30만) 라 크다.
                          건수로 읽으려면 그 값으로 나눈다.
                        """
                        _sl = list(safety2.get("isolated_off_slack") or [])
                        if not _sl:
                            return None
                        return (sum(_sl), lambda v: m2.Add(sum(_sl) <= v), 3.0, 0.3)

                    def _prep_team():
                        """팀 최소 커버 부족(slack) 최소화.

                        ★ 팀 슬랙은 stage2 '본 목적함수'에만 TEAM_COVER_LEX_WEIGHT(30만)로
                          들어가고, lex 진입 시 동결되지 않는다 — 동결되는 건 safety 뿐이다.
                          그래서 이후 패스들이 재배치하는 동안 팀 커버가 자유롭게 나빠진다.
                        ★ 슬랙 자체가 없으면(team_min 이 hard 로 풀렸거나 설정이 없으면)
                          None 을 돌려 건너뛴다.
                        """
                        _tm = list(getattr(roster_system, "_team_min_cover_slacks", []) or [])
                        if not _tm:
                            return None
                        print(f"{logger_prefix} [TeamSpec] slacks={len(_tm)}")
                        return (sum(_tm), lambda v: m2.Add(sum(_tm) <= v), 3.0, 0.3)

                    def _prep_off_quota():
                        """개인별 OFF 가 설정값(target)을 넘은 초과분 최소화.

                        ★ off_days 는 하한만 지켜지고 상한은 흐른다 — 실측: off_days=8 인
                          병동에서 OFF 가 8~19 로 드리프트했다. off_quota_excess 슬랙이
                          safety 묶음 안에 있어 다른 항목(고립OFF 30만·최소OFF 10만)과
                          교환되기 때문이다. 코드 주석도 '하드 고정 아님' 이라 자인한다.
                        ★ off_range 와 역할이 다르다 — off_range 는 사람 간 격차라 전원이
                          균등하게 초과해도 만족하지만, 이 패스는 설정값으로 끌어내린다.
                        ★ off_first=True 는 근무 상한이 하드(assigned <= need)라 남는 셀이
                          OFF 로 갈 수밖에 없다 — 초과가 구조적이라 줄일 여지가 없다.
                          다만 그 판단은 실측으로 확인한다(지금은 대상에 넣어 둔다).
                        """
                        _ex = list(safety2.get("off_quota_excess") or [])
                        if not _ex:
                            return None
                        print(f"{logger_prefix} [OffQuotaSpec] slacks={len(_ex)}")
                        return (sum(_ex), lambda v: m2.Add(sum(_ex) <= v), 3.0, 0.3)

                    def _prep_pref():
                        """소프트 선호 반영 — 사용자가 낸 요청·기피를 lex 패스로 다룬다.

                        왜 패스인가:
                          선호는 지금 **stage3 목적에만** 있는데(fallback_objectives.py:68-78)
                          그 stage3 가 실측 11곳 중 6곳에서 INFEASIBLE 이다. 그때 stage2 해가
                          커밋되므로 선호가 목적에서 빠진 채 우연에 맡겨진다
                          (같은 병동 재실행에서 PrefRate 0.0000 ↔ 1.0000).
                          lex 패스로 넣으면 stage3 성패와 무관하게 stage2 최종 해가
                          이 패스를 거친다.

                        왜 가중치(목적항 co-priority)가 아닌가:
                          stage2 본 solve 는 relative_gap_limit=0.15 이고 목적값이 6~9M 이라
                          솔버가 bound 대비 90만~140만 남기고 멈춘다. safety 최하위보다 작은
                          가중치는 그 gap 안에 묻혀 **최적화될 기회 자체가 없다**.
                          패스로 넣으면 동결 체계 안에서 확실히 돌고, safety·grade·team 을
                          절대 못 이긴다(그 위 패스들이 이미 동결돼 있으므로).

                        ★ 대상은 `_pref_cells`(사용자 선호가 실제로 덮어쓴 칸)뿐이다.
                          `preference_matrix` 전체를 쓰면 기본값 1 이 전 칸에 깔려 있어
                          (nurse_config.py:98 `np.ones(...)`) 목적이 사실상 '총 근무 최대화'
                          로 왜곡된다.
                        ★ Minimize 체계라 부호를 뒤집는다. 요청(delta>0)을 받으면 목적이
                          내려가고, 기피(delta<0)를 받으면 올라간다.
                        """
                        _cells = getattr(roster_system, "_pref_cells", None)
                        if not _cells:
                            return None
                        _P = getattr(roster_system, "preference_matrix", None)
                        if _P is None:
                            return None
                        _terms = []
                        for (_pn, _pd, _ps), _delta in _cells.items():
                            if not (0 <= _pn < N and 0 <= _pd < D and 0 <= _ps < S):
                                continue
                            if _pd < join[_pn] or _pd > leave[_pn]:
                                continue
                            _sc = int(round(float(_delta) * PREFERENCE_SCORE_SCALE))
                            if _sc:
                                _terms.append(-_sc * X2(_pn, _pd, _ps))
                        if not _terms:
                            return None
                        print(f"{logger_prefix} [PrefSpec] cells={len(_terms)}")
                        return (sum(_terms), lambda v: m2.Add(sum(_terms) <= v), 3.0, 0.2)

                    # ── 순서는 데이터다 ──
                    #   lex 는 사전식이라 앞 순위가 절대 우선이다. 즉 이 순서가 곧
                    #   "무엇을 더 중요하게 볼 것인가" 라는 정책이다. env 로 주입해
                    #   순열을 실측할 수 있게 둔다.
                    def _prep_s6():
                        """[S6] stage3 목적을 lex 패스 8 로 — m3 없이 같은 모델에서 푼다.

                        ★★ **부호**: stage3 는 `m.Maximize(sum(obj))`(:3432) 인데 lex 체인은
                          Minimize 라, 여기서는 `-sum(terms)` 를 최소화한다. 목적값을 비교할
                          때 **부호를 뒤집어야** stage3 값과 같은 축이 된다.
                        ★ 예산은 `tl3` 와 같게 준다 — "같은 목적 · 같은 시간 · 다른 모델" 이라야
                          패스 8 과 stage3 의 비교가 공정하다. 시간 재배분은 m3 를 걷어낸 뒤 일.
                        ★ 동결은 다른 패스와 같은 규약(`<= v`)이다. 부호를 뒤집었으므로
                          "이 값 이하" 가 곧 "선호·공정성이 이만큼 이상" 을 뜻한다.
                        """
                        _terms = getattr(m2, "_s6_stage3_terms", None)
                        if not _terms:
                            return None
                        _neg = -sum(_terms)
                        # tl2 비율로 환산해 tl3 와 같은 시간을 준다(_budget = tl2 * _frac).
                        _frac8 = float(tl3) / float(tl2) if tl2 else 0.5
                        return (_neg, lambda v: m2.Add(_neg <= v), float(tl3), _frac8)

                    def _prep_d5():
                        """D 5연속(D5) 최소화 — 체인 패스(2026-09-14).

                        ★ 실무 요청: "5연D 는 힘드니 D2/E2 로 꺾어달라 · D3 까지는 허용".
                          실측(성남ICU 2026-10 · schedule 856e14283992): 임옥희가
                          10/09~13 · 10/23~27 **D5 두 구간**. 하드락 1번(연속근무 5일)은
                          지키므로 위반이 아니고, 같은 시프트 연속을 막는 건
                          **소프트 벌점(D5 가중 1200)** 뿐인데 고립근무(1500) 아래로
                          **의도적으로 낮게** 설정돼 있다(fallback_objectives.py 주석).
                        ★★ 왜 패스인가 — 세 갈래를 실측으로 좁혔다:
                          · 소프트 가중치 상향 → 올리면 고립근무가 밀린다(g0 와 같은 자리)
                          · 하드 제약 → 27명·N 요구 큼·D 전담 존재로 INFEASIBLE 위험
                          · `d5-lex`(stage3 뒤 freeze re-solve) → **실측 기각**
                            (인원게이트 열어도 개선 0·악화 2·소요 +11~21s · 20회 성공 0)
                          남는 건 체인 패스뿐이고, 레벨이라 가중치 경쟁을 안 한다.
                        ★ 전제: `AIDE_S6_PASS8=1`. D5 핸들(`_ms_d5_lex_vars`)은
                          stage3 목적 빌더 안에서만 만들어지므로(fallback_objectives.py:396),
                          S6 으로 그 빌더를 m2 에 호출해야 이 패스가 성립한다.
                        ★ 자리: `de` 뒤 · `pref` 앞 — 근무품질 축이라 선호보다 상위.
                        """
                        _d5v = getattr(m2, "_ms_d5_lex_vars", None) or []
                        if not _d5v:
                            return None
                        return (sum(_d5v), lambda v: m2.Add(sum(_d5v) <= v), 3.0, 0.15)

                    _PASS_PREP = {
                        "s6": _prep_s6,
                        "d5": _prep_d5,
                        "d_range": _prep_d_range,
                        "off_range": _prep_off,
                        "grade": _prep_grade,
                        "n_range": _prep_n_range,
                        "n2n": _prep_n2n,
                        "de": _prep_de,
                        "iso_off": _prep_iso_off,
                        "team": _prep_team,
                        "off_quota": _prep_off_quota,
                        "pref": _prep_pref,
                    }
                    _lex_order = [
                        _t.strip()
                        for _t in _os_lex.getenv("LEX_PASS_ORDER", LEX_PASS_ORDER_DEFAULT).split(",")
                        if _t.strip()
                    ]
                    _mark("pre_lex")
                    for _i, _pname in enumerate(_lex_order, start=1):
                        _prep = _PASS_PREP.get(_pname)
                        if _prep is None:
                            print(
                                f"{logger_prefix} 폴백2 lex {_i}-pass ({_pname}): "
                                f"알 수 없는 패스 — 건너뜀"
                            )
                            continue
                        try:
                            _spec = _prep()
                        except Exception as _prep_e:
                            print(
                                f"{logger_prefix} 폴백2 lex {_i}-pass ({_pname}) 준비 예외: {_prep_e}"
                            )
                            continue
                        if _spec is None:
                            print(
                                f"{logger_prefix} 폴백2 lex {_i}-pass ({_pname}): 대상 없음 — 건너뜀"
                            )
                            continue
                        _obj, _freeze, _min_t, _frac = _spec
                        try:
                            m2.Minimize(_obj)
                            # ★★ 정체로 회수한 시간을 **뒤따르는 lex 패스**에 먼저 쓴다
                            #   (2026-09-11). 기존엔 회수분이 전부 stage3 로만 갔는데
                            #   (`_STALL_SAVED` → tl3 · :4644), 그러면 앞 패스에서 끊어 번 시간이
                            #   **같은 lex 체인 안에서 시간이 모자란 패스에 닿지 못한다.**
                            #   실측(시화9B · 3회): `lex4:n_range` 는 0.3s 에 개선이 멈추고
                            #   4.3s 까지 도는데(8%), `lex6:de` 는 6.0/6.3s(96%)·5.2/6.3s(83%)
                            #   로 **리밋 직전까지 개선 중**이다. n_range 에서 끊은 4초가
                            #   de 로 가야 27 이 더 내려간다 — stage3 로 보내면 de 는 그대로다.
                            #   ★ 남은 회수분은 그대로 stage3 로 간다(:4644). 순서만 바뀐다.
                            _budget = max(_min_t, float(tl2) * _frac)
                            if _lex_carry_on() and _STALL_SAVED:
                                _add = sum(_STALL_SAVED)
                                _STALL_SAVED.clear()      # 소비 — stage3 중복 가산 방지
                                _budget += _add
                                print(f"{logger_prefix} [lex이월] 회수 {_add:.1f}s → "
                                      f"lex{_i}:{_pname} 예산 {_budget:.1f}s")
                            s2.parameters.max_time_in_seconds = _budget
                            _hint_lex_solution()
                            _st_p = _solve_traced(s2, m2, logger_prefix, f"lex{_i}:{_pname}")
                            if _st_p in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                                _capture_lex_solution()
                                _val = int(s2.ObjectiveValue())
                                _freeze(_val)
                                print(
                                    f"{logger_prefix} 폴백2 lex {_i}-pass ({_pname}): "
                                    f"status={_cp_sat_status_to_text(_st_p)} value={_val}"
                                )
                            else:
                                print(
                                    f"{logger_prefix} 폴백2 lex {_i}-pass ({_pname}): "
                                    f"status={_cp_sat_status_to_text(_st_p)} — 직전 결과 유지"
                                )
                        except Exception as _pass_e:
                            print(
                                f"{logger_prefix} 폴백2 lex {_i}-pass ({_pname}) 예외: {_pass_e}"
                            )
            except Exception as _h1_e:
                print(f"{logger_prefix} 폴백2 lex H1 예외: {_h1_e}")
        if st2 not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            # ★ 알려진 한계: 이 경로도 아래 `return` 으로 끝나 함수 끝의 프리셉티 동기화를
            #   건너뛴다(stage3 실패 경로에서 고친 것과 같은 문제). 여기서 같은 방식으로
            #   고치지 못하는 이유는, 이어지는 코드가 stage2 산출물(safety2·lex_x2_log·
            #   stage2_zero_locks·m2/X2)을 전제로 하기 때문이다. stage2 가 해를 못 냈으면
            #   그것들이 비어 있어 흘려보내면 오히려 깨진다.
            #   ★ 다만 stage2 실패는 stage3 실패(실측 55%)와 달리 드물다 —
            #     stage1 이 풀렸는데 stage2 가 UNKNOWN 인 경우이고, 표본 11곳 5회에서 0건이었다.
            #   구조적 해결은 S6(단일 모델 체인)에서 단계 경계를 없애는 것이다.
            print(f"{logger_prefix} 폴백2 실패: 단계 불가능 → 1단계 해 사용")
            roster_system.roster.fill(0)
            for n in range(N):
                for d in iter_nurse_days(n, join, leave, blocked_by_nurse):
                    for s in range(S):
                        if s1.Value(X1(n, d, s)):
                            roster_system.roster[n, d, s] = 1
            _log_weekend_work_assignments(
                roster_system=roster_system,
                weekend_days=weekend_days,
                off_idx=off_idx,
                logger_prefix=logger_prefix,
            )
            # ★ 이 경로도 프리셉티 동기화를 거쳐야 한다. 예전에는 그냥 return 해서
            #   preceptee_follow 병동이 미러링 없이 나갔다.
            _sync_preceptee_rosters()
            return best_short == 0
        stage2_zero_locks = {}
        best_safe_sum = 0
        for k, arr in safety2.items():
            vals = lex_safety_val.get(k) or [int(s2.Value(v)) for v in arr]
            zeros = [v for v, val in zip(arr, vals) if val == 0]
            best_safe_sum += sum(int(val) for val in vals)
            stage2_zero_locks[k] = zeros
        print(f"{logger_prefix} 최소 안전 위반 합: {best_safe_sum}")
        try:
            for k, arr in safety2.items():
                total_k = sum(lex_safety_val.get(k) or [int(s2.Value(v)) for v in arr])
                if total_k > 0:
                    print(f"{logger_prefix} [Stage2 위반] {k} = {total_k}")
            short_items = [
                (d, code, int(s2.Value(var)))
                for (d, code), var in short_map2.items()
                if int(s2.Value(var)) > 0
            ]
            over_items = [
                (d, code, int(s2.Value(var)))
                for (d, code), var in over_map2.items()
                if int(s2.Value(var)) > 0
            ]
            if short_items:
                print(f"{logger_prefix} [Stage2 부족 참고] day,shift,shortage =", sorted(short_items))
            if over_items:
                print(f"{logger_prefix} [Stage2 과잉 참고] day,shift,over =", sorted(over_items))
        except Exception as exc:
            print(f"{logger_prefix} [Stage2 상세로그 실패]: {exc}")

    # ── verify-mode: stage3(선호/공정성) 건너뛰고 stage2 해로 즉시 반환 ──
    # hard_viol(커버리지+안전)은 stage2 에서 확정되고, stage3 는 이를 동결한 채
    # (아래 m3.Add(sum(safety3)==sum(safety2))) 선호만 최적화하므로 "되나/안되나"
    # 검증에는 stage2 해로 충분하다. 가장 무거운 stage3 를 건너뛰어 지연을 제거한다.
    # 기본 OFF(env gate). 일반 생성 경로는 영향 없음.
    import os as _os_vfb
    # ★ 앞으로 stage3 를 건너뛰는 장치를 더할 때 이 블록에 태우지 말 것 — 여기는
    #   **검증 전용 early return** 이라 이후 프리셉티/프리셉터 동기화를 건너뛴다.
    #   검증 경로에서는 그래도 되지만, 정상 생성에서 발동하면 preceptee_follow 를 켠
    #   병동에서 미러링이 빠진 근무표가 그대로 나간다. stage3 를 '풀지 않기'만 하고,
    #   기존 INFEASIBLE 경로가 stage2 해를 커밋한 뒤 공용 마무리까지 잇게 해야 한다.
    if _os_vfb.getenv("FB_VERIFY_SKIP_STAGE3") == "1":
        roster_system.roster.fill(0)
        for n in range(N):
            for d in iter_nurse_days(n, join, leave, blocked_by_nurse):
                for s in range(S):
                    if lex_x2_val.get((n, d, s), 0):
                        roster_system.roster[n, d, s] = 1
        _log_weekend_work_assignments(
            roster_system=roster_system,
            weekend_days=weekend_days,
            off_idx=off_idx,
            logger_prefix=logger_prefix,
        )
        print(
            f"{logger_prefix} [verify-mode] stage3 skip → stage2 해 반환 "
            f"(best_short={best_short}, best_safe_sum={best_safe_sum})"
        )
        return best_short == 0 and best_safe_sum == 0

    # ───── 3단계: 선호/공정성 ─────
    # 아래 with 블록 안에서 대입되지만, 함수 끝(4154)에서도 읽으므로 미리 잡아 둔다.
    _stage3_failed = False
    _s6_chain_commit = False
    with timer_cls("폴백 3단계: 선호/공정성 최대화"):
        (
            m3,
            X3,
            short3,
            over3,
            safety3,
            short_map3,
            over_map3,
            target_o_by_n,
            off_quota_short_by_n,
            off_quota_excess_by_n,
            min_off_miss_by_n,
        ) = build_model(
            stage=3,
            coverage_eq=best_short,
            over_le=best_over,
            stage2_zero_locks=stage2_zero_locks,
            broad_soft=used_broad_soft,
        )
        # ★ 아래 else 의 `sum(safety3[k]) == sum(safety2[k])` 는 오래 **빈 제약**이었다
        #   (2026-09-07 proto 로 확정). 양변이 모두 변수식이고 m2·m3 가 같은 순서로
        #   빌드돼 인덱스가 같으므로 계수가 상쇄돼 `0 == 0` 이 된다. 즉 stage3 가 도는
        #   동안 safety 항목별 방어가 없었다(실제 방어는 `stage2_zero_locks` 뿐).
        #   ★ 그래서 `_s4_stage3` 를 기본 on 으로 두고 **값 기준**으로 건다(파일 상단 판정).
        #     else 가지는 되돌림용으로만 남는다 — 그쪽을 타면 방어가 다시 사라진다.
        if _s4_stage3:
            # ★ `s2.Value()` 를 여기서 직접 읽으면 안 된다 — s2 의 마지막 solve 는
            #   lex 패스이고, 그 패스가 실패했으면 실패한 solve 의 값을 읽는다.
            #   `lex_safety_val` 은 성공한 패스에서만 갱신되므로(3943) 그쪽이 정확하다.
            _s4n3 = 0
            _s4_frozen, _s4_skipped = [], []
            _s4_caps: dict = {}      # 계측용 — 항목별 동결 상한. stage3 최종값과 대조한다
            for k in safety3.keys():
                _lhs3 = safety3.get(k) or []
                _vals2 = lex_safety_val.get(k)
                if not _lhs3 or _vals2 is None:
                    # ★ 어느 항목이 방어에서 빠지는지 남긴다. 개수만으로는
                    #   '항목이 없어서 빠진 것'과 '값을 못 구해 포기한 것'이 구분되지 않는다.
                    _s4_skipped.append(
                        f"{k}({'항목없음' if not _lhs3 else '값없음'})")
                    continue
                m3.Add(sum(_lhs3) <= sum(int(_v) for _v in _vals2))
                _s4n3 += 1
                _s4_frozen.append(k)
                _s4_caps[k] = sum(int(_v) for _v in _vals2)    # 계측용 상한 보관
            print(f"{logger_prefix} [S4-2] stage3 safety 동결 {_s4n3}개 (값 기준)")
            print(f"{logger_prefix} [S4-2] 동결됨: {', '.join(_s4_frozen) or '없음'}")
            print(f"{logger_prefix} [S4-2] 제외됨: {', '.join(_s4_skipped) or '없음'}")
        else:
            for k in safety3.keys():
                m3.Add(sum(safety3[k]) == sum(safety2[k]))
        # grade 도 동결한다. lex 6-pass 가 재배치로 미달을 낮춰 놔도 stage3 는 배치를
        #   새로 계산하므로, 안 걸면 흩어진다(실측: lex short=4 → 최종 14~20).
        #   safety 와 달리 등호가 아니라 상한이다 — 더 좋게 만드는 것은 막지 않는다.
        #
        # ★★ 이 제약이 걸리면 stage3 는 **거의 매번 INFEASIBLE** 이 된다. 버그가 아니다.
        #    grade 목표와 stage3 의 KLD·선호를 동시에 만족시키는 해가 없기 때문이고,
        #    그때 아래 실패 경로가 stage2 해(=lex 2~6-pass 로 이미 최적화된 해)를 쓴다.
        #    실측(시화 6병동 2026-08, 각 5회):
        #      동결 O : grade 미달 0.0 · 21초 · bidir 위반 0 · 커버초과 72
        #      동결 X : grade 미달 13.8 · 44초 · stage3 가 OPTIMAL 이 아닌 FEASIBLE 로 열화
        #      6-pass 자체를 뺌 : 미달 15.62 · 33초 · stage3 OPTIMAL
        #    즉 동결이 시간·grade·다른 품질 지표 모두에서 우세해 채택했다.
        #    ※ grade_config 가 없는 그룹은 _grade_cell_spec 이 비어 이 블록을 건너뛰므로
        #      기존 동작(stage3 정상 수행)이 그대로 유지된다.
        _g3_spec = list(getattr(m3, "_grade_cell_spec", []) or [])
        if GRADE_FREEZE_STAGE3 and lex_grade_short is not None and _g3_spec:
            _g3_shorts = []
            for _gd, _gs, _gt, _gmem in _g3_spec:
                _cum3 = sum(
                    X3(_n, _gd, _gs) for _n in _gmem
                    if join[_n] <= _gd <= leave[_n]
                )
                _sh3 = m3.NewIntVar(0, int(_gt), f"s3_g_short_d{_gd}_s{_gs}")
                m3.Add(_sh3 >= int(_gt) - _cum3)
                _g3_shorts.append(_sh3)
            m3.Add(sum(_g3_shorts) <= int(lex_grade_short))
            print(f"{logger_prefix} 폴백3 grade 동결: short <= {lex_grade_short} "
                  f"(cells={len(_g3_shorts)})")
        # ※ n2n 동결은 여기 있었는데 **기각**했다(2026-09-18 · 24런 교차). grade 와 같은
        #   규약으로 구현해 봤으나 기여가 0 이었다 — 기준선을 보정하면 개선폭이
        #   하드 단독과 같고(각 1.0), 2블록은 하드 단독이 오히려 나았다(17.0 대 13.5).
        #   `n2n` soft 값 자체가 회차마다 1~96 으로 흔들려 "달성치를 지킨다" 가
        #   나쁜 회차의 나쁜 값을 지키는 것으로도 작동한다. 간격은 **하한 하드**로만 움직인다.
        for n in range(N):
            for d in iter_nurse_days(n, join, leave, blocked_by_nurse):
                for s in range(S):
                    try:
                        m3.AddHint(X3(n, d, s), lex_x2_val.get((n, d, s), 0))
                    except Exception:
                        pass
        s3 = cp_model.CpSolver()
        # ★ lex 패스에서 정체로 조기 종료해 회수한 시간을 **여기로 이월**한다.
        #   stage3 는 6/6 리밋 소진에 마지막 개선 시각이 58·82·44·32·74·99% 라
        #   시간을 더 주면 품질이 오르는 유일한 구간이다(9A 6회 실측 · 2026-09-10).
        #   기본 off — `AIDE_LEX_STALL=1` 일 때만 `_STALL_SAVED` 에 값이 쌓인다.
        _tl3_eff = tl3
        if _STALL_SAVED:
            _carry = sum(_STALL_SAVED)
            _tl3_eff = tl3 + _carry
            print(f"{logger_prefix} [S1-재정의] lex 정체 회수 {_carry:.1f}s → "
                  f"stage3 예산 {tl3}s → {_tl3_eff:.1f}s")
        s3.parameters.max_time_in_seconds = _tl3_eff
        s3.parameters.num_search_workers = 8
        s3.parameters.relative_gap_limit = 0.05
        _mark("pre_stage3_solve")
        _reg_s3 = getattr(m3, "_cpsat_assumption_registry", None)
        if _reg_s3 is not None:
            _reg_s3.attach_to_model()
        # ★ stage3 진입 자체를 조건부로 막고 싶어도 **여기서는 이미 늦다** —
        #   `build_model(stage=3)` 이 실측 1~4초를 쓰고 위에서 끝나 있다. 빌드를
        #   건너뛰려면 m3·X3·s3 를 미정의로 두게 되는데 같은 `with` 블록 안에서
        #   그것들을 참조하는 코드가 여럿이라 NameError 가 된다. 구조적으로 풀려면
        #   S6(단일 모델 체인)에서 stage3 빌드 자체를 없애야 한다.
        st3 = _solve_traced(s3, m3, logger_prefix, "stage3")
        if st3 == cp_model.INFEASIBLE and _reg_s3 is not None:
            try:
                _fb_cores = _reg_s3.extract_conflict_cores(s3, solver_phase="fallback")
                if _fb_cores:
                    roster_system._cpsat_conflict_cores = (
                        list(getattr(roster_system, "_cpsat_conflict_cores", []) or []) + _fb_cores
                    )
                    print(f"[FallbackLex][stage3] MUS cores: {len(_fb_cores)}")
            except Exception as _mus_exc:
                print(f"[FallbackLex][stage3] MUS 추출 실패(무시): {_mus_exc}")
        print(f"{logger_prefix} 폴백3 결과: status={_cp_sat_status_to_text(st3)}")
        # [S0 계측] stage3 상태를 남긴다 — 선호 반영률을 INFEASIBLE/FEASIBLE 로 갈라 보려면
        #   필요하다. 선호는 stage3 목적에만 있어 INFEASIBLE 이면 그 회차 선호가 소실된다.
        try:
            setattr(roster_system, "_lex_stage3_status", _cp_sat_status_to_text(st3))
        except Exception:
            pass
        if st3 in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            _log_off_slack_used("stage3", s3, m3)
        # [S0-7 진단] stage3 가 왜 INFEASIBLE 인가. **기본 off**(AIDE_LEX_DIAG_S3=1).
        #   논리: stage2 최종 해는 grade 동결·zero_locks·커버리지 동결을 전부 만족한다.
        #   m3 가 m2 와 같은 제약 집합이면 그 해가 m3 에서도 feasible 이어야 하므로
        #   INFEASIBLE(=증명됨)이 나올 수 없다. 그런데 실측 11곳 중 6곳이 INFEASIBLE 이다.
        #   → m3 의 제약이 m2 와 다르거나, 동결값들이 서로 다른 시점의 해에서 왔다는 뜻이다.
        #   여기서 stage2 해를 m3 에 그대로 박아 그것을 확정하고, MUS 로 어느 제약이
        #   그 해를 거부하는지 이름을 뽑는다.
        if (st3 == cp_model.INFEASIBLE
                and _os_lex.environ.get("AIDE_LEX_DIAG_S3") == "1"):
            try:
                # ★ 진단도 데드라인 안에서 돈다. 여기서 무조건 15초를 더 쓰면
                #   S1 이 보장한다는 시간 상한이 진단 켤 때만 깨진다(stage3 가 예산을
                #   다 쓴 경우가 특히 그렇다).
                _sd = cp_model.CpSolver()
                _sd.parameters.max_time_in_seconds = 15.0
                _sd.parameters.num_search_workers = 8
                # m3 에는 이미 stage2 해가 힌트로 들어가 있다(위 AddHint 루프).
                # 그 힌트를 값으로 고정하면 "stage2 해가 m3 에서 성립하는가" 를 직접 묻는다.
                _sd.parameters.fix_variables_to_their_hinted_value = True
                _std = _sd.Solve(m3)
                print(f"{logger_prefix} [DiagS3] stage2해 고정 재solve: "
                      f"{_cp_sat_status_to_text(_std)} "
                      f"→ {'m3 가 stage2 해를 거부한다(m3≠m2 확정)' if _std == cp_model.INFEASIBLE else 'stage2 해는 m3 에서 성립 — 원인은 목적/시간 쪽'}")
                # 어느 제약이 거부하는지까지 보려면 registry 가 있어야 한다.
                #   `AIDE_ENABLE_MUS_REGISTRY=1` 을 함께 켜면 위 3746 의 기존 core 추출이
                #   돈다(그쪽이 정본이므로 여기서 MUS 를 중복 추출하지 않는다).
                if _std == cp_model.INFEASIBLE and _reg_s3 is None:
                    print(f"{logger_prefix} [DiagS3] 거부 제약 이름을 보려면 "
                          "AIDE_ENABLE_MUS_REGISTRY=1 을 함께 켤 것")
            except Exception as _dg_e:
                print(f"{logger_prefix} [DiagS3] 진단 실패(무시): {_dg_e}")
        # ★ 여기서 `return` 하면 안 된다 — 함수 끝의 **프리셉티 동기화**를
        #   건너뛴다. `preceptee_follow` 병동에서 프리셉터의 DEN/O 미러링이 빠진
        #   근무표가 그대로 나간다. stage3 INFEASIBLE 은 예외가 아니라 **설계상 흔한
        #   경로**여서(실측 11곳 중 6곳) 이 누락이 상시 발생하고 있었다.
        #   반환값도 최종 `return` 과 같으므로, stage3 전용 후속 블록만 건너뛰고
        #   공용 마무리로 흘려보낸다.
        _stage3_failed = st3 not in (cp_model.OPTIMAL, cp_model.FEASIBLE)
        # ── [S6] 커밋 출처 ── `AIDE_S6_COMMIT=chain` 이면 **체인(m2) 해를 커밋**하고
        #   stage3 는 돌리되 결과를 쓰지 않는다(기록만).
        #   ★★ 왜 필요한가: 처치 팔에서 stage3 를 그대로 커밋하면 **stage3 가 여전히
        #     lex 값을 되돌려서** 패스 8 의 효과가 산출물에 안 나타난다. 두 경로를 모두
        #     둬야 "패스 8 커밋" 과 "stage3 커밋" 을 같은 배치에서 비교할 수 있다.
        #   ★ stage3 는 패스 8 해를 힌트로 받아 돌므로 (c) 축(stage3 Δ = 패스 8 에서
        #     stage3 가 더 바꾼 양)이 그대로 측정된다. Δ ≈ 0 이면 m3 제거 근거다.
        #   ★ `_stage3_failed` 를 건드리면 **stage3 계측 로그까지 사라져**(`[S4-2계측]`)
        #     판정축 (a) 목적값 비교와 (c) stage3 Δ 를 못 잰다. 커밋 자리만 막는다(:5082).
        _s6_chain_commit = (_os_lex.environ.get("AIDE_S6_COMMIT") == "chain"
                            and not _stage3_failed)
        if _s6_chain_commit:
            print(f"{logger_prefix} [S6] 커밋 출처=chain — stage3 는 정상 수행·계측하되 "
                  f"결과는 쓰지 않고 체인(m2) 해를 커밋한다")
        # ★ [S6] chain 커밋도 **이 경로를 탄다** — `lex_x2_val`(체인 해)로 roster 를 채우는
        #   코드가 여기뿐이라, 커밋 자리만 막으면 roster 가 비어 INFEASIBLE 로 끝난다
        #   (스모크에서 실제로 그랬다 · 2026-09-11).
        if _stage3_failed or _s6_chain_commit:
            print(f"{logger_prefix} "
                  + ("[S6] 체인(m2) 해를 커밋한다 — stage3 결과는 기록만"
                     if _s6_chain_commit else "폴백3 실패: 선호 단계 불가능 → 2단계 해 사용"))
            roster_system.roster.fill(0)
            for n in range(N):
                for d in iter_nurse_days(n, join, leave, blocked_by_nurse):
                    for s in range(S):
                        if lex_x2_val.get((n, d, s), 0):
                            roster_system.roster[n, d, s] = 1
            _log_weekend_work_assignments(
                roster_system=roster_system,
                weekend_days=weekend_days,
                off_idx=off_idx,
                logger_prefix=logger_prefix,
            )
        # ★ 아래는 stage3 가 성공했을 때만 의미가 있는 후속 처리다(D5 lex · mutex lex ·
        #   stage3 상세로그). 실패했으면 건너뛴다 — 예전에는 여기서 `return` 했는데,
        #   그러면 함수 끝의 프리셉티 동기화까지 건너뛰어 미러링이 빠진 근무표가 나갔다.
        if not _stage3_failed:
            # ── 최종 lex 패스: DDDDD 보장 강화 (기본 OFF · AIDE_D5_LEX=1 로 켬) ──
            # stage3 목적값을 동결(무회귀)한 뒤 D5 viol 합만 최소화하는 별도 solve.
            # 자기-게이트: 잔여 D5=0 이면 스킵(무비용) → DDDDD 남은 병동에서만 재-solve.
            # payload/사용자 입력 불필요. (주의) postprocess/preceptee 경로 재유입은 별도.
            #
            # 인원수 게이트: 대형 병동은 freeze 재-solve 가 시간 내 못 풀고(UNKNOWN) 헛돎 →
            # 소인원 병동(기본 N<=15)에서만 실행. AIDE_D5_LEX_MAXN 으로 임계 조절.
            #
            # ── [기각] 기본 ON → OFF · 2026-09-09 (근거 정정 2026-09-10) ──────
            #   ★★ 기각의 **결정적 근거는 통계가 아니라 인과 통로가 없다는 것**이다.
            #     실패(UNKNOWN)한 패스는 `s3` 를 재대입하지 않고(:4313-4321 의 and 조건),
            #     `m3` 는 :4325 이후 참조가 0건이다 — 이 패스가 m3 에 얹은 동결 부등식·
            #     Minimize·AddHint 는 solve 반환 직후 전부 죽은 코드가 된다.
            #     즉 **실패 회차는 최종 근무표에 도달하는 경로가 하나도 없다.** 벽시계만 쓴다.
            #   ★ 발동 대비 성공 ― 확정 A/B 에서 **20회 중 성공 0회**(전부 UNKNOWN).
            #     ※ 40런 중 절반은 `AIDE_D5_LEX=0` 팔이라 게이트가 False → 발동 자체를 안 한다.
            #     ※ 사전 관측 26회(성공 1 · 별관1)는 **분모로 쓰지 않는다** — grade 중복 수정
            #       (`c90626f` · 2026-09-09 09:41) 전후가 섞여 있어 stage3 가 죽던 코드
            #       상태의 관측이 포함된다. 지형이 달라진 뒤의 수치와 합산할 수 없다.
            #     ※ "N회 발동" 을 런 수로 세지 말 것 — `_run_cp_sat_basic` 호출부가 8곳이라
            #       한 생성 요청이 이 게이트를 여러 번 지날 수 있다. 정확한 발동 수는 미계수다.
            #   ★ 판정축은 패스의 성공/실패가 아니라 **최종 근무표의 D5 건수**로 잡았다
            #     (실패 회차엔 `D5 before→after` 가 안 찍혀 로그로는 못 잰다).
            #     쌍별 차이(현행-끔) 합 +11 / 20쌍 · 회당 4.95 대 4.40 · 부호검정 p=0.238.
            #     ★ 이 차이를 "끈 쪽이 더 좋다" 로 읽으면 안 된다. 성공 0회면 두 팔은
            #       **알고리즘적으로 동일**하므로 이 ±는 전부 솔버 비결정성이다.
            #       역으로 그게 증거다 — 켜 둬도 출력이 안 바뀐다는 뜻이니까.
            #       (같은 이유로 검정력 논의는 성립하지 않는다. 열린 통로가 없다.)
            #     소요는 회당 52s → 44s (-8s) 로 패스가 쓰던 시간만큼 줄었다.
            #   ★ 측정 기준선 주의 ― 이 A/B 는 워킹트리에 `teams.min_shift` 반영
            #     (roster_create_service.py:3255~ · 당시 미커밋)이 함께 있는 상태에서 돌았다.
            #     솔버 입력이 바뀌는 변경이므로, 절대 수치를 뒤 세션과 비교할 땐 이걸 감안한다.
            #     (같은 트리 안 두 팔 비교이므로 이번 판정 자체는 영향받지 않는다.)
            #   ★★ 껐다고 D5 가 방치되는 게 아니다 ― `fallback_objectives.py` 의
            #     `obj.append(-_d5_w * v5)` 로 **stage3 목적에 이미 페널티가 들어가 있다**
            #     (기본 가중치 1200). 이 패스는 그 위에 "한 번 더" 얹던 시도일 뿐이다.
            #   ★ 예산만 줄이는 안(②)도 함께 기각했다 ― 유일한 성공 회차가 10.49초라
            #     예산을 깎으면 그 1건마저 놓쳐 끄는 것과 실질이 같다.
            #   ★ 동결을 푸는 안(③)은 손대지 않는다 ― 같은 세션에서 S4-② 빈 제약과
            #     grade 중복 계수로 **동결이 어긋나면 품질이 무너지는 것**을 실측했다.
            #   ★ 되살리려면 고칠 것은 **모델**이다 — 이 패스는 애초에 실행가능해를
            #     하나도 못 찾는다(UNKNOWN). 정지 조건을 손대는 시도는 아래에서 기각됐다.
            #   ★★ 되살리기가 `AIDE_D5_LEX=1` 한 줄로 끝나지 않는다.
            #     운영 소비자는 Lambda(`roster-solver-{prod,dev}`)인데
            #     `.github/workflows/deploy-lambda.yml` 은 `update-function-code`(이미지 URI)만
            #     호출하고 `update-function-configuration`(환경변수)은 **부르지 않는다.**
            #     즉 이 플래그를 넣을 자리가 레포에 없다 — 콘솔/CLI 로 함수 설정을 직접
            #     건드려야 하고, 그건 다음 배포 때 코드와 어긋날 수 있다.
            #   ★ 끈 뒤에는 로그에 흔적이 없다 — `if _lex_on:` 이 False 면 성공·실패 한 줄도
            #     안 남아 "꺼짐" 과 "코드에서 제거됨" 이 로그상 구별되지 않는다.
            #     그래서 아래에 `AIDE_LEX_TRACE=1` 일 때만 스킵 사실을 한 줄 남긴다.
            _lex_maxn = int(_os_tl3.environ.get("AIDE_D5_LEX_MAXN", 15) or 15)
            # ── mutex lex 패스(D5-lex 앞=상위 우선): "grade/team 바로 밑" ──
            # grade/team 소프트 품질을 동결(무회귀)한 뒤 상호배제 위반합만 최소화 →
            # mutex 가 선호/야간분포보다 위, grade/team·하드보다 아래. 동결 부등식이라 infeasible 불가(soft).
            # 자기게이트(mutex=0 skip)는 D5-lex 와 같은 방식이나, 인원게이트는 **별도 임계**다
            # (`_mx_lex_maxn` 기본 40 · D5-lex 는 15). 같다고 읽으면 발동 범위를 오판한다.
            # mutex-lex 는 grade/team 만 동결(총품질 아님)해 D5 보다 가벼움 + 자기게이트(위반 0 skip)
            # + 타임리밋(초과 시 원해 유지)이라, D5(15)보다 높은 40 을 기본으로 실병동(19·35명 등) 커버.
            _mx_lex_maxn = int(_os_tl3.environ.get("AIDE_MUTEX_LEX_MAXN", 40) or 40)
            _mx_lex_on = (_os_tl3.environ.get("AIDE_MUTEX_LEX", "1") != "0") and (N <= _mx_lex_maxn)
            if _mx_lex_on:
                _mx_vars = getattr(m3, "_mutex_lex_vars", None) or []
                _gt_terms = getattr(m3, "_grade_team_lex_terms", None)
                if _mx_vars and _gt_terms:
                    class _MxSkipLex(Exception):
                        pass
                    try:
                        _mx_before = sum(int(s3.Value(v)) for v in _mx_vars)
                        print(f"{logger_prefix} 폴백3 mutex-lex 진입: stage3 위반 {_mx_before}건 "
                              f"(페어변수 {len(_mx_vars)}개)")
                        if _mx_before == 0:
                            raise _MxSkipLex  # 위반 0 → 재-solve 불필요(무비용)
                        _gt_val = int(round(s3.Value(sum(_gt_terms))))
                        m3.Add(sum(_gt_terms) >= _gt_val)   # grade/team 무회귀(penalty=음수 → >= 로 최소보장)
                        m3.Minimize(sum(_mx_vars))
                        m3.ClearHints()
                        for _hn in range(N):
                            for _hd in iter_nurse_days(_hn, join, leave, blocked_by_nurse):
                                for _hs in range(S):
                                    try:
                                        m3.AddHint(X3(_hn, _hd, _hs), int(s3.Value(X3(_hn, _hd, _hs))))
                                    except Exception:
                                        pass
                        _s3m = cp_model.CpSolver()
                        _s3m.parameters.max_time_in_seconds = max(8, int(tl3))
                        _s3m.parameters.num_search_workers = 8
                        # ── [기각] 후속 패스 gap_limit 완화 · 2026-09-09 ──────────────
                        #   착상: 후속 패스(mutex-lex · D5-lex)만 `relative_gap_limit` 기본 0.0
                        #     이라 완전 최적 증명까지 돈다. 앞 단계는 0.15(stage1·2)/0.05(stage3)
                        #     로 느슨히 끊는데 뒤로 갈수록 조여지는 역전으로 보였다.
                        #   ★ 기각: 0.05 를 걸어도 **아무것도 안 바뀐다**(9B 실측).
                        #     d5-lex 12.08s→12.19s · 둘 다 UNKNOWN.
                        #   ★★ 이유는 gap 계산이 아니라 **비교 대상이 없다는 것**이다.
                        #     OR-Tools 정의: `abs(O - B) / max(1, abs(O))`, O = best **feasible**
                        #     objective. 최적화 모델의 UNKNOWN 은 실행가능해를 하나도 못 찾았다는
                        #     뜻이라(찾았으면 FEASIBLE) **O 자체가 없어** 이 파라미터는 bound 가
                        #     어떻든 구조적으로 발화하지 못한다. 분모도 obj 가 아니라 max(1,|O|) 다.
                        #     즉 병목은 정지 조건이 아니라 **해를 아예 못 찾는 것**이다.
                        #   ★ 손대려면 gap 이 아니라 모델 쪽이다 — 이 패스는 `Minimize(sum(_mx_vars))`
                        #     처럼 목적이 0/1 변수 합인데 하한 근거가 약해 LP 완화가 0 을 준다.
                        _st3m = _solve_traced(_s3m, m3, logger_prefix, "stage3:mutex-lex")
                        if _st3m in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                            _mx_after = sum(int(_s3m.Value(v)) for v in _mx_vars)
                            if _mx_after <= _mx_before:
                                m3.Add(sum(_mx_vars) <= _mx_after)  # 이후 D5 패스가 mutex 되돌리지 못하게 락
                                s3 = _s3m
                                print(f"{logger_prefix} 폴백3 mutex-lex 패스: "
                                      f"status={_cp_sat_status_to_text(_st3m)} mutex {_mx_before}→{_mx_after}")
                            else:
                                print(f"{logger_prefix} 폴백3 mutex-lex 패스 미개선 → 원 해 유지")
                        else:
                            print(f"{logger_prefix} 폴백3 mutex-lex 패스 실패("
                                  f"{_cp_sat_status_to_text(_st3m)}) → 원 해 유지")
                    except _MxSkipLex:
                        pass  # 위반 0 → 스킵
                    except Exception as _mx_lex_e:
                        print(f"{logger_prefix} 폴백3 mutex-lex 패스 예외: {_mx_lex_e}")
            # 기본 "0" ― 위 기각 주석 참조. 되살리려면 AIDE_D5_LEX=1.
            _lex_on = (_os_tl3.environ.get("AIDE_D5_LEX", "0") != "0") and (N <= _lex_maxn)
            if not _lex_on:
                # ★ 끈 상태를 로그로 구별할 수 있게 남긴다 — 없으면 "꺼짐" 과
                #   "코드에서 제거됨" 이 로그상 같아 보인다(위 기각 주석 참조).
                #   `_trace` 가 자체적으로 AIDE_LEX_TRACE 를 보므로 평소엔 조용하다.
                _trace(logger_prefix, "stage3:d5-lex", skip="off",
                       AIDE_D5_LEX=_os_tl3.environ.get("AIDE_D5_LEX", "(unset)"),
                       maxn=_lex_maxn, N=N)
            if _lex_on:
                _d5_vars = getattr(m3, "_ms_d5_lex_vars", None) or []
                _obj_terms = getattr(m3, "_stage3_obj_terms", None)
                if _d5_vars and _obj_terms is not None:
                    class _SkipLex(Exception):
                        pass
                    try:
                        # mutex-lex 패스가 앞서 m3 목적을 바꿨을 수 있으므로 ObjectiveValue 대신 항 합으로 총품질 계산
                        _obj_val = int(round(s3.Value(sum(_obj_terms))))
                        _d5_before = sum(int(s3.Value(v)) for v in _d5_vars)
                        if _d5_before == 0:
                            raise _SkipLex  # 잔여 DDDDD 없음 → 재-solve 불필요(대형병동 비용 0)
                        m3.Add(sum(_obj_terms) >= _obj_val)  # maximize 라 품질 무회귀
                        m3.Minimize(sum(_d5_vars))
                        # 직전 stage3 해를 hint 로 seed → 재solve 가 feasible incumbent 에서 출발
                        # (없으면 tight freeze 로 짧은 시간에 UNKNOWN → 개선 실패).
                        m3.ClearHints()
                        for _hn in range(N):
                            for _hd in iter_nurse_days(_hn, join, leave, blocked_by_nurse):
                                for _hs in range(S):
                                    try:
                                        m3.AddHint(X3(_hn, _hd, _hs), int(s3.Value(X3(_hn, _hd, _hs))))
                                    except Exception:
                                        pass
                        _s3b = cp_model.CpSolver()
                        _s3b.parameters.max_time_in_seconds = max(8, int(tl3))
                        _s3b.parameters.num_search_workers = 8
                        # (위 mutex-lex 의 gap_limit 기각 주석과 같은 사안)
                        _st3b = _solve_traced(_s3b, m3, logger_prefix, "stage3:d5-lex")
                        if _st3b in (cp_model.OPTIMAL, cp_model.FEASIBLE) and \
                                sum(int(_s3b.Value(v)) for v in _d5_vars) <= _d5_before:
                            s3 = _s3b  # 개선(또는 동률)일 때만 채택
                            print(f"{logger_prefix} 폴백3 D5-lex 패스: "
                                  f"status={_cp_sat_status_to_text(_st3b)} "
                                  f"D5 {_d5_before}→{int(round(_s3b.ObjectiveValue()))}")
                        else:
                            print(f"{logger_prefix} 폴백3 D5-lex 패스 실패("
                                  f"{_cp_sat_status_to_text(_st3b)}) → 원 해 유지")
                    except _SkipLex:
                        pass  # 잔여 D5=0 → 스킵(무비용)
                    except Exception as _lex_e:
                        print(f"{logger_prefix} 폴백3 D5-lex 패스 예외: {_lex_e}")
            try:
                short_items = [
                    (d, code, int(s3.Value(var)))
                    for (d, code), var in short_map3.items()
                    if int(s3.Value(var)) > 0
                ]
                over_items = [
                    (d, code, int(s3.Value(var)))
                    for (d, code), var in over_map3.items()
                    if int(s3.Value(var)) > 0
                ]
                if short_items:
                    print(f"{logger_prefix} [Stage3 부족 참고] day,shift,shortage =", sorted(short_items))
                if over_items:
                    print(f"{logger_prefix} [Stage3 과잉 참고] day,shift,over =", sorted(over_items))
                for k, arr in safety3.items():
                    total_k = sum(int(s3.Value(v)) for v in arr)
                    if total_k > 0:
                        print(f"{logger_prefix} [Stage3 위반] {k} = {total_k}")
                # ── [계측] stage3 최종 safety vs S4-2 동결 상한 · 2026-09-10 ──────────
                #   ★ 이 대조가 필요한 이유: `폴백 완료: 안전위반합=` 은 **stage2 확정값**이라
                #     (`best_safe_sum` 이 stage2 블록에서 계산되고 재대입이 없다)
                #     stage3 가 상한까지 safety 를 더 써도 그 숫자에는 안 나타난다.
                #   ★ stage3 목적에는 safety 가 없고 동결은 `<= 상한` 이다. 그래서 stage3 는
                #     선호를 더 얻는 방향으로 **상한까지 safety 를 소비할 수 있다.**
                #     최종 ≈ 상한이면 그 교환이 실제로 일어난 것이고, 최종 < 상한이면
                #     시간이 모자라 다 못 쓴 것이다(= 지금까지의 낮은 값은 잔여물일 뿐).
                try:
                    _cap_rows, _s3_tot, _cap_tot = [], 0, 0
                    for k in sorted(safety3.keys()):
                        _cur = sum(int(s3.Value(v)) for v in (safety3.get(k) or []))
                        _cap = _s4_caps.get(k)
                        _s3_tot += _cur
                        if _cap is not None:
                            _cap_tot += _cap
                            if _cur or _cap:
                                _cap_rows.append(f"{k}={_cur}/{_cap}")
                    # ★★ 판정 축은 **절대값이 아니라 Δ(= stage2 확정값 − stage3 최종값)** 다.
                    #   절대값은 stage2 노이즈가 지배해서 stage3 처치의 효과가 안 보인다
                    #   (실측: (가) 회차가 절대값은 낮았지만 그건 stage2 가 낮았던 것이고,
                    #    stage3 안에서 낮춘 양은 오히려 현행이 컸다).
                    #   Δ > 0 이면 stage3 가 safety 를 더 낮췄다는 뜻이다.
                    _delta = _cap_tot - _s3_tot
                    _use = (_s3_tot / _cap_tot * 100) if _cap_tot else 0.0
                    print(f"{logger_prefix} [S4-2계측] stage3 safety {_s3_tot} / 상한 {_cap_tot} "
                          f"({_use:.2f}%) **Δ={_delta}** — {', '.join(_cap_rows) or '항목없음'}")
                    # ★★ 완료 로그의 `안전위반합` 을 **커밋된 해 기준**으로 바꾼다.
                    #   지금까지는 stage2 확정값만 찍혔다. stage3 는 동결 상한(=stage2 값)을
                    #   넘을 수 없으므로 두 값이 같아 티가 안 났지만, stage3 목적에 safety 를
                    #   넣으면(안건 '(가)') stage3 가 상한 **아래로** 내려갈 수 있어 갈라진다.
                    #   그때 stage2 값을 계속 찍으면 개선이 통째로 안 보인다.
                    if _cap_tot:
                        best_safe_sum = _s3_tot
                except Exception as _cap_exc:
                    print(f"{logger_prefix} [S4-2계측] 실패(무시): {_cap_exc}")
                # 실제 배정된 휴무 카운트(O/주휴/휴가) 요약
                if off_idx is not None:
                    for n, nu in enumerate(roster_system.nurses):
                        assigned_off = sum(
                            int(s3.Value(X3(n, d, off_idx))) for d in iter_nurse_days(n, join, leave, blocked_by_nurse)
                        )
                        vac_cnt = sum(
                            1
                            for d in iter_nurse_days(n, join, leave, blocked_by_nurse)
                            if (n, d) in off_exception_vacation_cells
                        )
                        weekly_target = len(weekly_off_by_idx.get(n, []) if isinstance(weekly_off_by_idx, dict) else [])
                        target_o = target_o_by_n.get(n)
                        slack_short_val = (
                            s3.Value(off_quota_short_by_n[n]) if n in off_quota_short_by_n else None
                        )
                        slack_excess_val = (
                            s3.Value(off_quota_excess_by_n[n]) if n in off_quota_excess_by_n else None
                        )
                        min_off_miss_val = (
                            s3.Value(min_off_miss_by_n[n]) if n in min_off_miss_by_n else None
                        )
                        print(
                            f"{logger_prefix} [OffCount][final] n={n}, "
                            f"id={getattr(nu, 'nurse_id', '?')}, name={getattr(nu, 'name', '?')}, "
                            f"cap_semantics={off_cap_semantics}, "
                            f"assigned_O={assigned_off}, vacation={vac_cnt}, weekly_off_target={weekly_target}, "
                            f"target_O={target_o}, slack_short={slack_short_val}, slack_excess={slack_excess_val}, "
                            f"min_off_miss={min_off_miss_val}"
                        )
            except Exception as exc:
                print(f"{logger_prefix} [Stage3 상세로그 실패]: {exc}")

    # ★ stage3 가 실패했으면 s3 에 해가 없다. 여기서 무조건 추출하면 위에서 채워 둔
    #   stage2 해를 지우고 빈 값(또는 예외)으로 덮는다. 실패 시에는 이미 채워진
    #   roster 를 그대로 두고, 아래 공용 마무리(프리셉티 동기화 등)만 이어간다.
    if not _stage3_failed and not _s6_chain_commit:      # [S6] chain 커밋이면 stage3 해를 안 쓴다
        roster_system.roster.fill(0)
        for n in range(N):
            for d in iter_nurse_days(n, join, leave, blocked_by_nurse):
                for s in range(S):
                    if s3.Value(X3(n, d, s)):
                        roster_system.roster[n, d, s] = 1
    log_n_even_distribution(roster_system, logger_prefix, join=join, leave=leave)
    # NOTE: rebalance_off 후처리 비활성화(호출 무시)
    # try:
    #     print(f"{logger_prefix} [PostOff] 시작: 최종 stage3 해 기반 후처리 시도")
    #     before_viol = len(roster_system._find_violations())
    #     postprocess_rebalance_off_fn(roster_system)
    #     after_viol = len(roster_system._find_violations())
    #     print(
    #         f"{logger_prefix} [PostOff] 종료: viol {before_viol}->{after_viol} "
    #         f"(감소={before_viol - after_viol})"
    #     )
    # except Exception as exc:
    #     print(f"{logger_prefix} [PostOff] 후처리 실패: {exc}")

    # ── 후처리 완료 후 프리셉티 roster를 프리셉터와 동기화 ──
    # 규칙: 프리셉터의 DEN/O → 프리셉티 동일 복사
    #       프리셉터의 특수코드(W 등) → 프리셉티는 OFF
    _sync_preceptee_rosters()
    # ★ 완료 로그는 동기화 함수 밖이어야 한다. best_safe_sum 은 stage2 이후에야
    #   대입되는데, 함수 안에 두면 stage2 실패 경로에서 부르는 순간 UnboundLocalError 가 난다.
    _mark("post_stage3")
    # ★★ `안전위반합` 은 **stage2 확정값**이다(best_safe_sum 은 stage2 블록에서만 대입된다).
    #   최종 근무표가 stage3 해인지 stage2 해인지에 따라 이 숫자의 뜻이 달라지므로
    #   어느 해가 커밋됐는지 함께 남긴다 — 없으면 표에서 어느 값이 최종인지 못 읽는다.
    print(f"{logger_prefix} 폴백 완료: 커버리지부족={best_short}, 안전위반합={best_safe_sum} "
          f"(커밋해={'stage2(선호 미반영)' if _stage3_failed else ('chain(패스8)' if _s6_chain_commit else 'stage3')} 기준)")
    return best_short == 0 and best_safe_sum == 0
