"""EI-2 "커밋별로 사라지는 키" 단언 장치(설계서 §5.3 EI-2·§9).

EI-2(죽은 코드 삭제)는 "L0~L3 동일"이 아니라 "아래 키 소멸만 다름"을 단언한다(§5.3 EI-2
머리말). 이 모듈은 그 "아래 키"를 단계(2a·2b·2c·2d)별로 미리 적어 두고, 실제로 그 단계를
구현한 뒤 `EI2_DONE` 에 그 단계 이름을 추가하면 골든 대조가 그 범위만 허용한다.

단계 하나가 실제로 코드를 지우기 **전까지는 이 모듈은 아무것도 지우지 않는다** — `EI2_DONE = ()`
이고, 이 상태에서 `test_expected_diff.py` 는 diff 가 0 이어야 한다(장치 자체의 자기 검증, 작업
지시문 3번 "EI2_DONE=() 상태에서 이 테스트는 '차이 0'이어야 한다").

규칙(작업 지시문 3번 (a)~(d))
  (a) 골든 대 지금 기록의 diff 줄 전부가 "사라짐"이고, 활성 단계(EI2_DONE) 중 하나의
      `vanish_patterns` 정규식에 맞아야 한다. 아니면(값이 바뀜·새로 생김·허용 밖 경로가
      사라짐) 위반이다.
  (b) 각 단계의 `must_vanish[시나리오]` 에 적은 경로가 실제로 그 시나리오의 사라짐 집합에
      있어야 한다(지우기로 한 키가 조용히 안 지워지는 회귀를 잡는다).
  (c) `queries` — 수가 `query_delta[시나리오]` 만큼 정확히 줄고, 남은 순서열이 이전
      순서열의 **부분열**이어야 한다(§7.1 BR-7 "재조회 제거 커밋" 규칙). 쿼리를 더한 단계는 더한 쿼리를
      `query_added` 에 적고 `query_delta` 를 그만큼 음수로 적는다 — 부분열은 그 쿼리를 빼고 본다(EI-12 12u2).
  (d) `digest`·`queries` 는 (a)의 "사라짐만" 규칙 밖이다 — `digest` 는 층이 바뀌면 당연히
      달라지므로 보지 않고, `queries` 는 (c)로만 본다.

EI-10c~f(precheck 결함 4종 — **판정이 바뀌는 단계**)는 '사라짐만'으로 설명되지 않는다. 그래서 단계
정의(`StageDiff`)에 두 칸을 더했다 — `change_patterns`(값이 바뀌거나 새로 생기거나 사라져도 되는
경로) · `must_change`(시나리오마다 반드시 바뀌어야 하는 경로). 이 경로 밖의 변화는 EI-2 와 같은
규칙으로 위반이다. 예측은 단계마다 `ei10/STATUS_10cf.md` 에 다시 쓰기 **전에** 적는다.

경로 표기는 `golden.diff_paths` 가 쓰는 `$.layers.L1[0].config_data.grade_strategy` 식
JSONPath 다. 캡처 층의 실제 인자 이름(`nurses_dict`·`config_dict`(L0) · `nurses_data`·
`prefs_data`·`config_data`(L1) · `nurses`(L2.nurses) 등)은 `capture.py`·`golden/*.json` 으로
확인했다(HEAD `49f14bd`).

★★ 2026-10-13(dev `55dacd4` 재수신) — `test_expected_diff.py` 의 대조 기준을 `golden_base/ei1b`
  (아래 ★2a~★2d 가 실측에 쓴 49f14bd 시절 동결본)에서 `golden_base/dev55dacd4`(pure_dev 55dacd4 +
  최신 하네스, EI 적용 전 동결본)로 바꿨다. 이유: dev 가 49f14bd 이후 `roster_create_service.py`에
  사다리 새 단계(`ban_night_minimize`)·`gen_deadline` 등을 더해 `L2.settings_eff.ban_night_
  minimize` 같은 **새 키**가 생겼는데, 이 키는 EI-2 가 만든 것도 지운 것도 아니라 `vanish_
  patterns`(사라짐만 허용) 밖이다 — ei1b 를 기준으로 두면 이 새 키·관련 쿼리 수 차이가 "허용
  밖 변화"로 걸린다(재현: S1·S1-D·S1-P·S2·S4·S5·S6·S7·S8·S1-L3 10개 시나리오가 ei1b 대조에서
  위반). `dev55dacd4` 기준으로는 EI-2 가 지운 것만 드러나 아래 2a~2d 가 이미 적어 둔
  `vanish_patterns`·`must_vanish`·`query_delta` **값을 하나도 안 바꿔도** 15개 전부 통과한다
  (실측 확인 — dev 가 더한 것들은 `pure_dev`(=dev55dacd4 기준본)에도 똑같이 있어서 "사라짐"
  집합에 안 걸리기 때문이다). `golden_base/ei1b` 는 2026-10-20(D29) 저장소 밖
  `.omc/aide_docs/v2/engine_input/golden_store/retired_from_repo_20261020/` 로 옮겼다(역사 기록 — `golden_base/
  README.md`).

★ 2a 는 구현 뒤 `UPDATE_GOLDEN=1` 로 실측했다(15개 시나리오 전부 against `golden_base/ei1b`,
  2026-10-13 기준은 `dev55dacd4` 로 바뀌었지만 아래 수치 자체는 재확인 결과 그대로다 — 위 ★★).
  결과 — "조회 9회"(`grade_config_context`) 가정은 두 가지가 틀렸다:
  (1) S1~S9-F(GoldenStop, solve 전 멈춤)는 그 코드(검증·재시도 루프)에 전혀 안 닿아 `query_delta`
      0 이 맞다(실측 — 9개 시나리오 전부 쿼리 변화 0).
  (2) 사다리·성공 모드(S1-L·S1-LT·S1-L3)는 매 시도(attempt)가 **성공까지 간다** — "S1-L3 말고는
      안 닿는다"던 추정이 틀렸다. 매 시도가 `_validate_generated_roster` 최초 호출(:6959, 9곳 중
      실제로 늘 걸리는 자리)을 거쳐 쿼리 1개씩 줄고, 시도가 쌓이는 만큼 **누적**된다 — S1-L 25회
      시도 → 25, S1-LT 25회 → 25, S1-L3 1회(성공 1회뿐) → 1.
  부수 발견 — 사다리 기록의 `attempts[*].queries_before`(시도별 "그때까지 쿼리 수")는 시도마다
  1씩 벌어지는 변화를 보이는데, 이건 `golden.py`·`ladder.py` 가 이미 "엔진 입력이 아니라 쿼리
  위치"라 digest 제외 칸으로 적어 둔 바로 그 자리다 — (d) 규칙에 `_ATTEMPT_QUERIES_BEFORE` 로
  추가해 전체 집계(`$.queries`, `query_delta`)로만 보게 했다.
  또 하나 — S6 은 L1(`nurses_data`) 말고 `L0p[0].nurses[*].weekly_off_type` 에서도 사라진다
  (파견 inbound 간호사의 ORM `__dict__` 스냅샷. `nurse_effective` 매핑과는 무관 — §본문 주의).

★ 2b 도 구현 뒤 `UPDATE_GOLDEN=1` 로 실측했다(15개 시나리오 전부 against `golden_base/ei1b`).
  `allow_override_by_law`·`lookahead_days`·`lookahead_weekly_off_cells`(설계서가 안 적었던 키 —
  `lookahead_days` 소비 블록을 지우면 이 키도 같이 사라진다) 3종이 L0(`config_dict`, 15개 전부)·
  L1(`config_data`, L1 에 닿는 12개 — `S3`·`S9`·`S9-F` 는 L0 에서 멈춰 L1 자체가 없다)에서
  사라진다. `prev_month_last_is_off` 는 S7 에서만(전월 마감이 있어야 값이 생긴다).
  `lookahead_days` 를 읽던 `_effective_config_snapshot`(roster_system, L2 캡처의 `settings_eff`)
  에서도 사라지는데, 이 자리는 L1 에 닿는 12개 중 **ladder·post 모드 3개(S1-L·S1-LT·S1-L3)는
  안 닿는다** — 그 모드는 원본 엔진을 안 불러 L2 자체가 비어 있다(§7.1). 그래서 9개(S1·S1-D·
  S1-P·S2·S4·S5·S6·S7·S8)에서만 사라진다.
  ★★ S1-L3(post 모드)에서는 후처리 입력 `L3.calls[*].args.latest_config` 에서도 같은 세 키가
  사라진다. 원인은 `roster_create_service.py:5630` `config_dict = latest_config.__dict__`(**같은
  객체, 복사가 아니다** — verify) 다. `config_dict.setdefault(...)` 로 넣던 세 키가 사라지면 DB
  에서 막 읽은 `latest_config` ORM 인스턴스의 `__dict__` 도 그만큼 준다 — L0/L1 에서 단언한 세
  키 소멸과 **같은 사건**이고 매핑된 DB 컬럼이 아니라 커밋되지도 않는다(verify: `app/db/models.py`
  의 `RosterConfig` 선언에 이 세 이름이 없다).
  L3 는 큰 인자를 내용 주소(`L3.objects.<sha>`)로 한 번만 저장하고 호출 기록에는 참조
  (`{"__object__": sha}`)만 둔다(ladder.py `_L3Store`). 그대로 비교하면 키 하나만 빠져도 해시
  전체가 바뀌어 "사라짐 + 새로 생김"이 된다. 그래서 `check_record` 는 비교 전에 참조를 내용으로
  풀어 넣고(`_inline_l3_objects`) `L3.objects` 를 뺀다 — 이러면 변화가 `...latest_config.<키>:
  사라짐` 으로 나와 2b 의 키 단위 규칙을 그대로 받는다. 해시 변화를 통째로 빼는 예외는 두지 않는다
  (10-09 리뷰 지적: 통째 예외는 이후 단계에서 후처리 입력이 바뀌는 회귀를 영구히 가린다).

★ 2c 도 구현 뒤 `UPDATE_GOLDEN=1` 로 실측했다(15개 시나리오 전부 against `golden_base/ei1b`).
  `add_even_night_minmax_distribution_terms`·`add_even_mid_distribution_terms`(`even_mids` 토글을
  읽던 바로 그 함수 — 재확인 결과 **호출 0** 이라 필드뿐 아니라 함수 전체가 죽어 있었다. 둘 다
  CP-SAT 목적함수 항 생성 함수로, 솔버 모델을 실제로 빌드할 때만(이 골든 하네스가 멈추는 L0~L2
  캡처 지점 "뒤") 호출되는 자리라 삭제가 어느 층에도 안 보인다)·`off_placement_mode` 주석 잔재
  (`fallback_lex.py`·`postprocess_off.py`·`cp_sat_basic.py` — 단, `cp_sat_basic.py` 는
  `CPSATBasicEngine` 메서드 쪽 3곳만이다. 같은 이름의 주석 블록이 `_build_full_model`
  (primary 경로, D14=② 로 보존 대상) 안에도 있는데 그건 100% 주석이라도 건드리지 않았다 —
  "지우지도 고치지도 않는다"는 절대 규칙이 그 함수 전체에 적용되기 때문이다)·
  `repairs/grade_repair.py`(호출 0, 유일한 참조가 주석) 는 실측대로 L0~L3 어느 키에도 나타나지
  않았다 — 15개 시나리오 전부
  `golden_base/ei1b` 와 2a+2b 가 허용한 키 소멸 밖의 차이가 0(`StageDiff()` 그대로 유지, 쿼리
  수 변화도 전부 0). `min_ratio_floor` 는 별도 vanish 대상이 아니다 — `repairs/grade_repair.py`
  삭제에 묻혀 사라지는 지역 변수일 뿐, `constraints/grade_constraints.py` 쪽 동명의 살아있는
  사용은 건드리지 않았다(별개 모듈·공급 0 이라 항상 no-op 이지만 삭제 대상이 아니다).

★ 2d 도 구현 뒤 `UPDATE_GOLDEN=1` 로 실측했다(15개 시나리오 전부 against `golden_base/ei1b`).
  `roster_system.py` 의 `optimize_roster_with_cp_sat_v2`·`optimize_with_lns`→
  `_optimize_neighborhood`(호출 0, git grep 재확인) · `precheck/per_nurse_sequence_feasibility.
  check_per_nurse_sequence`(호출 0 — `team_grade_precheck.py:1176` 의 동명 함수는 별개, 그건
  `nurse_sequence_infeasible` 을 쓰는 살아있는 precheck 라 건드리지 않았다) · `daily_shift_
  service_mssql.py`(zero import, 파일 전체 삭제)는 골든 어디에도 안 나타났다(실측: 2a+2b+2c
  밖의 차이 0). `remaining_off_days` 만 `L2.nurses[*]` 에서 사라지는데, 실제로 닿는 범위는
  `_L2_SETTINGS_EFF_REACHING`(L1 에 닿으면서 L2 도 채우는 9개 — S1·S1-D·S1-P·S2·S4·S5·S6·
  S7·S8)뿐이다. 나머지 6개는 애초에 `L2.nurses` 자체가 없다(S3·S9·S9-F 는 precheck 에서
  멈춰 L1 조차 없고, S1-L·S1-LT·S1-L3 는 ladder·post 모드라 원본 엔진을 안 불러 L2 가
  비어 있다, §7.1 EngineRecorder) — `must_vanish` 를 `_all()`(15개 전부)로 넓게 걸면 이
  6개에서 "기대한 소멸이 없다"는 거짓 위반이 난다. 쓰기만 하던 필드를 지운 순수 in-memory
  변경이라 쿼리 수 변화는 15개 전부 0.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from services.engine_contract.names import EXTERNAL_TO_INTERNAL

from .golden import diff_paths

#: 16개 골든 전부(test_scenarios 의 12 + test_ladder 의 4) — must_vanish 를 "전 시나리오"로
#: 적을 때 쓴다. ★ 2026-10-13 `S1-LB` 추가(사다리 4차 "ban_night_minimize_retry" 실제 발화 —
#: scenario_ladder.py 참고). 아래 2a~2d 단락의 "15개"는 이 시나리오가 생기기 전 실측 기록이라
#: 그대로 둔다 — `S1-LB` 의 2a 실측치는 query_delta 주석에 따로 적었다.
ALL_SCENARIOS: tuple[str, ...] = (
    "S1", "S1-D", "S1-P", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S9", "S9-F",
    "S1-L", "S1-LT", "S1-LB", "S1-L3",
)

_DIGEST_PATH = "$.digest"
_QUERIES_PREFIX = "$.queries"
#: 사다리 시도별 "그때까지 쿼리 수"(ladder.py:291) — `golden.py`·`ladder.py` 가 이미 digest
#: 제외 칸으로 문서화한 자리다("엔진 입력이 아니라 쿼리 위치다"). `$.queries`(전체 집계)와
#: 같은 이유로 (a) 규칙 밖에 둔다 — 전체 집계는 `query_delta`(아래 (c))로만 본다.
_ATTEMPT_QUERIES_BEFORE = re.compile(r"^\$\.layers\.attempts\[\d+\]\.queries_before$")
_VANISH_MARK = "사라짐"
_OBJECT_REF = "__object__"


def _resolve_refs(value, objects: Mapping[str, object]):
    """`{"__object__": sha}` 참조를 `objects[sha]` 내용으로 바꾼 사본(모르는 sha 는 그대로 둔다)."""
    if isinstance(value, dict):
        if set(value) == {_OBJECT_REF} and value[_OBJECT_REF] in objects:
            return _resolve_refs(objects[value[_OBJECT_REF]], objects)
        return {k: _resolve_refs(v, objects) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_refs(v, objects) for v in value]
    return value


def _inline_l3_objects(record: dict) -> dict:
    """L3 의 내용 주소 참조를 풀어 넣고 `L3.objects` 를 뺀 기록 사본(모듈 docstring ★★).

    L3 가 없는 기록은 그대로 돌려준다. 원본은 바꾸지 않는다.
    """
    l3 = (record.get("layers") or {}).get("L3")
    if not isinstance(l3, dict) or "objects" not in l3:
        return record
    objects = l3["objects"]
    inlined = {k: _resolve_refs(v, objects) for k, v in l3.items() if k != "objects"}
    return {**record, "layers": {**record["layers"], "L3": inlined}}


def _all(path: str) -> dict[str, tuple[str, ...]]:
    """`must_vanish` 축약 — 15개 시나리오 전부에 같은 경로 하나를 건다."""
    return {name: (path,) for name in ALL_SCENARIOS}


#: L1 에 닿는 시나리오 — `S3`·`S9`·`S9-F` 는 precheck(L0) 단계에서 멈춰 L1 자체가 없다(실측).
_L1_REACHING: tuple[str, ...] = tuple(s for s in ALL_SCENARIOS if s not in ("S3", "S9", "S9-F"))
#: 그중에서도 L2(`settings_eff`) 까지 닿는 시나리오 — ladder·post 모드(S1-L·S1-LT·S1-LB·S1-L3) 는
#: 원본 엔진을 안 불러 L2 가 비어 있다(§7.1 EngineRecorder, 실측).
_L2_SETTINGS_EFF_REACHING: tuple[str, ...] = tuple(
    s for s in _L1_REACHING if s not in ("S1-L", "S1-LT", "S1-LB", "S1-L3")
)


def _must_vanish_2b() -> dict[str, tuple[str, ...]]:
    """2b 의 `must_vanish` — 실측(UPDATE_GOLDEN=1 재생성 후 golden_base/ei1b 대조)대로 층별로
    닿는 시나리오만 쌓는다(넓게 전 시나리오에 걸지 않는다)."""
    out: dict[str, list[str]] = {
        name: [
            "$.layers.L0[0].config_dict.allow_override_by_law",
            "$.layers.L0[0].config_dict.lookahead_days",
            "$.layers.L0[0].config_dict.lookahead_weekly_off_cells",
        ]
        for name in ALL_SCENARIOS
    }
    for name in _L1_REACHING:
        out[name].extend([
            "$.layers.L1[0].config_data.allow_override_by_law",
            "$.layers.L1[0].config_data.lookahead_days",
            "$.layers.L1[0].config_data.lookahead_weekly_off_cells",
        ])
    out["S7"].append("$.layers.L1[0].config_data.prev_month_last_is_off")
    for name in _L2_SETTINGS_EFF_REACHING:
        out[name].append("$.layers.L2.settings_eff.lookahead_days")
    # 후처리 입력 — latest_config 를 받는 L3 호출 3개(health_leave·sleep_off·off_swap) 전부.
    for i in (1, 2, 3):
        out["S1-L3"].extend(f"$.layers.L3.calls[{i}].args.latest_config.{key}"
                            for key in _CONFIG_DICT_KEYS_2B)
    return {name: tuple(paths) for name, paths in out.items()}


#: 2b 에서 `config_dict`(= `latest_config.__dict__`) 에 setdefault 로 넣던 세 키.
_CONFIG_DICT_KEYS_2B: tuple[str, ...] = ("allow_override_by_law", "lookahead_days", "lookahead_weekly_off_cells")


@dataclass(frozen=True)
class StageDiff:
    """EI-2 하위 단계 하나가 허용하는 골든 변화.

    vanish_patterns: 사라짐 줄의 경로가 매칭해야 하는 정규식들(하나라도 맞으면 허용).
    must_vanish: 시나리오 이름 → 그 시나리오에서 반드시 사라져야 하는 경로 목록.
                 (키 소멸 전제가 애초에 없는 시나리오까지 억지로 끼워 넣지 않는다 —
                 예: `weekly_off_type` 파견 inbound 주입은 S6 에만 있다.)
    query_delta: 시나리오 이름 → 기대 쿼리 수 감소(없으면 0). 음수(쿼리 증가)는 EI-2 의
                 "불변(값)·키 소멸만" 전제와 맞지 않아 EI-2 에서는 쓰지 않는다 — 쿼리를 더하는 단계(EI-12 12u2)는
                 `query_added` 에 더한 쿼리를 적고 그 수만큼 음수로 적는다.
    change_patterns: (EI-10) 값이 바뀌거나 새로 생기거나 사라져도 되는 경로 정규식들.
    must_change: (EI-10) 시나리오 이름 → 반드시 바뀌어야 하는 경로(접두 일치) — 바꾸기로 한 칸이
                 조용히 그대로인 회귀를 잡는다.
    query_added: (EI-12 12u2) 시나리오 이름 → 그 단계가 더한 쿼리(순서열 표기 — 예 'select:shift_manage'). (c) 의 부분열
                 검사는 지금 순서열에서 이 쿼리들을 한 번씩(어느 자리든) 뺀 나머지로 본다 — 적지 않은 쿼리가 끼거나 순서가
                 바뀌면 그대로 위반이다.
    query_reorder: (EI-10g) 시나리오 이름 목록 — 그 시나리오는 쿼리 **수는 같고 순서열만** 바뀌어도 된다(앞당겨 읽기).
                 (c) 의 부분열 검사를 정렬한 순서열로 본다(= `query_added` 를 뺀 쿼리 다중집합이 이전과 같다). 새 순서열 자체는 재생성
                 골든의 정확 비교가 고정한다.
    key_renames: (NM) 옛 키 → 새 키. 비교 전에 골든 기록의 dict 키만 이 표로 바꾼다 — 값·쿼리는 그대로 정확 비교라
                 '이름만 바뀌고 값은 같다'를 단언한다.
    key_renames_skip: (NM) `key_renames` 를 걸지 않을 dict 경로 정규식(그 dict 아래 전부) — 바깥 이름이 맞는 자리
                 (요청 모델 원문 `L3.calls[].args.req` · 저장 JSON `snapshot.config`).
    """

    vanish_patterns: tuple[str, ...] = ()
    must_vanish: dict[str, tuple[str, ...]] = field(default_factory=dict)
    query_delta: dict[str, int] = field(default_factory=dict)
    change_patterns: tuple[str, ...] = ()
    must_change: dict[str, tuple[str, ...]] = field(default_factory=dict)
    query_added: dict[str, tuple[str, ...]] = field(default_factory=dict)
    query_reorder: tuple[str, ...] = ()
    key_renames: dict[str, str] = field(default_factory=dict)
    key_renames_skip: tuple[str, ...] = ()


EI2_STAGES: dict[str, StageDiff] = {
    # 2a — 생성 경로 헛조회·죽은 전달(설계서 §5.3 EI-2 "2a"). 아래는 구현 후 실측값(위 docstring).
    #   `grade_config_context`(:4344) 인자·호출부 9회는 L0~L2 어느 층에도 안 보인다(§5.3
    #   "캡처층에 안 보인다") — solve 뒤 재시도/검증 루프에 있어 GoldenStop 시나리오는 안 닿고,
    #   사다리·성공 모드는 시도마다 1개씩(9곳 중 매번 걸리는 최초 호출 1곳) 줄어 누적된다.
    #   `use_fixed_wanted` 는 재확인 결과 엔진 경로로 전달되는 자리가 이미 없다(스키마·라벨
    #   뿐) — 골든에 지울 자리가 없다(작업 지시문 2 의 "설계서와 다름" 참고).
    "2a": StageDiff(
        vanish_patterns=(
            r"^\$\.layers\.L0\[\d+\]\.nurses_dict\[\d+\]\.weekly_off_type$",
            r"^\$\.layers\.L0p\[\d+\]\.nurses\[\d+\]\.weekly_off_type$",
            r"^\$\.layers\.L1\[\d+\]\.nurses_data\[\d+\]\.weekly_off_type$",
            r"^\$\.layers\.L1\[\d+\]\.prefs_data\[\d+\]\.data\.request$",
            r"^\$\.layers\.L1\[\d+\]\.grade_strategy$",
            r"^\$\.layers\.L1\[\d+\]\.config_data\.grade_strategy$",
        ),
        must_vanish={
            **_all("$.layers.L0[0].nurses_dict[0].weekly_off_type"),
            # 파견 inbound 전용(:5434) — S6 만 간호사가 실제로 있다. L1(엔진에 넘어가는
            # nurses_data)·L0p(ORM __dict__ 스냅샷) 둘 다에서 사라진다(실측).
            "S6": ("$.layers.L1[0].nurses_data", "$.layers.L0p[0].nurses"),
            # WantedRequest 경로(:380) — 실측 결과 FixedWantedEntry 경로(:266)도 같은
            # 죽은 "request" 키를 썼다(S8 이 그 경로로 "확정 원티드 적용" 문구를 실었다).
            # 그래서 S1(WantedRequest)·S8(FixedWantedEntry) 둘 다에서 사라진다.
            "S1": ("$.layers.L1[0].prefs_data",),
            "S8": ("$.layers.L1[0].prefs_data",),
        },
        # 실측(UPDATE_GOLDEN=1 재생성 후 golden_base/ei1b 대조) — GoldenStop 9개 시나리오
        # (S1~S9-F)는 검증·재시도 루프 자체에 안 닿아 0. 사다리·성공 모드는 매 시도가
        # `_validate_generated_roster` 최초 호출(:6959)을 거쳐 1개씩 줄고 시도 수만큼 누적된다
        # — S1-L 25회 시도·S1-LT 25회 시도·S1-L3 1회(성공 1회뿐).
        # ★ 2026-10-13 추가 실측(golden_base/dev55dacd4 대조, UPDATE_GOLDEN=1) — S1-LB 는 사다리
        #   2번째 지점(ban_night_minimize_retry)에 두 번(하드 복구·누적 해제) 더 들어가 시도 수가
        #   S1-L 보다 늘고, 그만큼 `_validate_generated_roster` 호출도 늘어 27회 감소다.
        # ★ EI-14b2a4 — S1-L 계열 재설계(기준본도 순수 dev 에서 새로 떴다)로 검증 호출 수가 25→23 · 25→23 · 27→25 가 됐다(실측).
        query_delta={"S1-L": 23, "S1-LT": 23, "S1-LB": 25, "S1-L3": 1},
    ),
    # 2b — 월경계 죽은 키(설계서 §5.3 EI-2 "2b"). `day0_n_fixed_nurse_ids`(늘 빈 리스트) ·
    #   월경계 전용 `ban_d_to_n`(지역변수, 반환 dict 에 안 실림) · `day0_cap`(게이트
    #   `cross_month_day0_cap_enable` 가 상시 False)은 재확인 결과 **config_dict/골든에
    #   나타나는 자리가 원래 없다** — 지워도 골든에 사라짐 줄 자체가 생기지 않는다(설계서
    #   §5.3 EI-2 L0~L2 표에도 이 셋은 없다 — 암묵적으로 일치. 실측으로도 재확인: 이 셋은
    #   아래 vanish_patterns/must_vanish 어디에도 없다 — 그래도 diff 는 0 이다).
    #   남는 셋(`allow_override_by_law`·`lookahead_days`·`lookahead_weekly_off_cells` — 마지막은
    #   설계서에 없던 키다. `lookahead_days` 소비 블록을 지우면 이 키도 같은 자리에서 같이
    #   사라진다는 걸 실측으로 알았다)은 L0(`config_dict`, **15개 전부**)·L1(`config_data`,
    #   L1 에 닿는 12개 — `S3`·`S9`·`S9-F` 는 precheck 단계에서 멈춰 L1 이 아예 없다)에서
    #   사라진다. `prev_month_last_is_off` 는 S7(전월 마감 꼬리가 있는 시나리오)에서만 L1 에서
    #   사라진다(다른 시나리오는 애초에 빈 dict라 config_dict 에 키가 없다). `lookahead_days`
    #   를 읽어 두던 `_effective_config_snapshot`(L2 캡처의 `settings_eff`)은 L1 에 닿는 12개
    #   중 **ladder·post 모드(S1-L·S1-LT·S1-L3) 는 빼고** 9개에서만 사라진다 — 그 세 시나리오는
    #   원본 엔진을 안 불러 L2 자체가 비어 있다(§7.1 EngineRecorder). S1-L3 는 후처리 입력
    #   `latest_config` 에서도 같은 세 키가 사라진다(모듈 docstring ★★ · `_inline_l3_objects`).
    "2b": StageDiff(
        vanish_patterns=(
            r"^\$\.layers\.L0\[\d+\]\.config_dict\.allow_override_by_law$",
            r"^\$\.layers\.L0\[\d+\]\.config_dict\.lookahead_days$",
            r"^\$\.layers\.L0\[\d+\]\.config_dict\.lookahead_weekly_off_cells$",
            r"^\$\.layers\.L1\[\d+\]\.config_data\.allow_override_by_law$",
            r"^\$\.layers\.L1\[\d+\]\.config_data\.lookahead_days$",
            r"^\$\.layers\.L1\[\d+\]\.config_data\.lookahead_weekly_off_cells$",
            r"^\$\.layers\.L1\[\d+\]\.config_data\.prev_month_last_is_off$",
            r"^\$\.layers\.L2\.settings_eff\.lookahead_days$",
            r"^\$\.layers\.L3\.calls\[\d+\]\.args\.latest_config\."
            r"(allow_override_by_law|lookahead_days|lookahead_weekly_off_cells)$",
        ),
        must_vanish=_must_vanish_2b(),
        # 실측(UPDATE_GOLDEN=1 재생성 후 golden_base/ei1b 대조) — 세 키 모두 config_dict 안
        # 값(키 존재 여부)만 바꾸는 순수 in-memory 삭제다. 제거한 `_compute_lookahead_weekly_
        # off_cells` 호출은 K_lookahead>0 분기 안에 있었는데 그 분기는 애초에 늘 False 라
        # 실행된 적이 없다(생산자 `lookahead_days` 가 상시 0) — 그래서 15개 전부 쿼리 변화 0.
        query_delta={},
    ),
    # 2c — 엔진 죽은 함수·필드(설계서 §5.3 EI-2 "2c"). `add_even_night_minmax_distribution_
    #   terms`(호출 0) · `add_even_mid_distribution_terms`(`even_mids` 필드를 읽던 함수 자체가
    #   호출 0 — import 만 있고 어디서도 부르지 않았다. 설계서는 "늘 True 라 no-op" 로 적었지만
    #   재확인하니 토글이 아니라 함수 전체가 죽어 있었다) · `off_placement_mode` 주석 잔재
    #   (fallback_lex·postprocess_off·cp_sat_basic 3곳, 전부 이미 주석이라 실행된 적이 없다) ·
    #   `repairs/grade_repair.py`(호출 0, 유일한 참조가 주석) · `min_ratio_floor`(grade_repair.py
    #   삭제에 묻혀 사라지는 지역변수 — `constraints/grade_constraints.py` 의 동명 사용은 별개
    #   모듈이라 건드리지 않았다)는 전부 **골든 L0~L3 어느 키에도 나타나지 않는다** — 솔버 모델
    #   빌드 내부(S1-P 의 "P" 요약 전·CP-SAT 변수/제약 단계)에서만 영향권이고, 재확인 결과 이
    #   다섯 다 primary 경로(`build_main_objective_terms`)에서도 호출되지 않아 S1-P 의 "P" 빌드
    #   요약(변수·제약 수)도 그대로다. 그래서 이 단계는 "허용 소멸 0" — 커밋 전후 골든이 완전히
    #   같아야 한다(빈 StageDiff 그 자체가 단언이다. 실측: UPDATE_GOLDEN=1 재생성 후에도
    #   golden_base/ei1b 대비 2a+2b 밖의 차이 0, 15개 시나리오 전부).
    "2c": StageDiff(),
    # 2d — 호출 0 파일·함수(설계서 §5.3 EI-2 "2d"). `roster_system.py` v2(구 :679)·lns(구 :1108)→
    #   `_optimize_neighborhood`(구 :1710) · `precheck/per_nurse_sequence_feasibility.
    #   check_per_nurse_sequence`(zero 호출 — 주의: `team_grade_precheck.py:1176` 에 **같은
    #   이름**의 다른(살아 있는) 함수가 있다. 그건 지우지 않는다) · `daily_shift_service_
    #   mssql.py`(zero import) 는 전부 골든에 안 보인다. 같은 커밋의 `remaining_off_days`
    #   (필드·`initialize_off_days`/`can_take_off`/`update_off_days` 메서드·cp_sat_basic.py
    #   의 초기값·호출)만 L2.nurses 에서 사라진다.
    #   ★ 실측(UPDATE_GOLDEN=1 재생성 후 golden_base/ei1b 대조) — `remaining_off_days` 는
    #   `_L2_SETTINGS_EFF_REACHING`(L1 에 닿으면서 L2 도 채우는 9개 — S1·S1-D·S1-P·S2·S4·
    #   S5·S6·S7·S8) 에서만 사라진다. 나머지 6개(S3·S9·S9-F 는 precheck 에서 멈춰 L2 자체가
    #   없음 · S1-L·S1-LT·S1-L3 는 ladder·post 모드라 원본 엔진을 안 불러 L2 가 비어 있음,
    #   §7.1 EngineRecorder)는 애초에 `L2.nurses` 자체가 없어 must_vanish 대상이 아니다 —
    #   `_all()`(15개 전부)을 쓰면 이 6개에서 "기대한 소멸이 없다" 거짓 위반이 난다. 쿼리
    #   변화는 15개 전부 0(순수 in-memory 필드 제거 — DB 조회 경로와 무관).
    "2d": StageDiff(
        vanish_patterns=(
            r"^\$\.layers\.L2\.nurses\[\d+\]\.remaining_off_days$",
        ),
        must_vanish={name: ("$.layers.L2.nurses[0].remaining_off_days",)
                     for name in _L2_SETTINGS_EFF_REACHING},
        query_delta={},
    ),
}

#: 활성 단계 — 비어 있음 = 아직 아무 EI-2 하위 단계도 구현 안 함. 각 단계 구현자가 자기
#: 단계 이름을 여기 추가한다(예: 2a 를 끝내면 `EI2_DONE = ("2a",)`).
EI2_DONE: tuple[str, ...] = ("2a", "2b", "2c", "2d")

#: EI-10 — precheck 결함 4종(판정이 바뀌는 단계). 단계마다 구현 뒤 이름을 `EI10_DONE` 에 더한다.
#: 10c 는 엔진 하드락 실효값이 precheck 에 실린다. 골든 L0~L2 는 그대로이고(값은 `run_runtime_precheck`
#: 본문 안에서 L0 캡처 뒤에 싣는다) EI5-precheck-block 의 MONTHLY_NIGHT 증거에만 회복 축
#: (30일 · 3N→2OFF · L=3 = 21)이 새로 붙는다(4곳 × 간호사 2명) — golden_presolve 는 다시 쓰지 않고 이
#: 허용 차이로 대조한다.
_PRESOLVE_RECOVERY_AXIS = (
    r"^\$\.http_detail\.infeasibility\.(causes\[\d+\]\.(details|evidence)|preflight_issues\[\d+\]\.evidence|"
    r"resolution_narrative\.problem_list\[\d+\]\.raw_evidence)\.night_capable_nurses\[\d+\]\.cap_by_axis\.recovery$"
)
#: 10d 는 precheck 에 넘기는 고정 칸이 엔진 형식(`nurse_index`·`fixed_source`·`shift_type`·원코드)에서 번역본
#: `{nurse_id, day_index, shift(메인코드)}` 로 바뀐다 — L0 `fixed_cells` 만이다. 고정 칸이 있는 골든은 S5(주휴 O 17칸 ×
#: {nurse_index·fixed_source·shift_type 사라짐, nurse_id 새로 생김} = 68곳 + digest = 69곳)와 S8(4칸 × 4 = 16곳 + 코드
#: `연`→`O`·`OFF`→`O` 2곳 + digest = 19곳) 둘뿐이다. `day_index`·칸 개수는 그대로여야 한다(번역이 칸을 잃으면 정규식 밖의
#: `fixed_cells[N]` 사라짐이라 위반). L0′(탐지기 인자)·L1·L2 는 그대로다 — 번역은 `PrecheckCall.fixed_cells` 만 바꾼다.
_L0_FIXED_CELL_TRANSLATION = (
    r"^\$\.layers\.L0\[\d+\]\.fixed_cells\[\d+\]\.(nurse_index|fixed_source|shift_type|nurse_id|shift)$"
)
EI10_STAGES: dict[str, StageDiff] = {
    "10c": StageDiff(
        change_patterns=(_PRESOLVE_RECOVERY_AXIS,),
        must_change={"presolve/EI5-precheck-block": (
            "$.http_detail.infeasibility.causes[2].evidence.night_capable_nurses[0].cap_by_axis.recovery",)},
    ),
    "10d": StageDiff(
        change_patterns=(_L0_FIXED_CELL_TRANSLATION,),
        must_change={
            "S5": ("$.layers.L0[0].fixed_cells[0].nurse_id", "$.layers.L0[0].fixed_cells[16].nurse_id"),
            "S8": ("$.layers.L0[0].fixed_cells[0].shift", "$.layers.L0[0].fixed_cells[2].shift"),
        },
    ),
    # 10e 는 주휴를 엔진이 만든 맵(`config_dict["weekly_off_map"]`)으로 세어 `PrecheckNurse.personal_off_adjustment`
    #   (precheck 내부 값)에 더한다. 골든 어느 층에도 그 값이 안 나타난다 — L0 는 `run_runtime_precheck` 의 입력이고
    #   (S5 의 N03 0→5 · N04 0→4 는 `build_precheck_input` 안에서만 바뀐다) 판정·이슈·쿼리·출력은 골든 19개 모두 그대로다.
    #   허용 0(빈 `StageDiff`) 그 자체가 '10e 는 어느 골든도 바꾸지 않는다'는 단언이다.
    "10e": StageDiff(),
    # 10f 는 precheck 의 근무 가능 구간(`PrecheckNurse.join_day`·`leave_day`)을 엔진과 같은 날 계산(퇴사일 −1 · 그 달과 안 겹치는
    #   입퇴사는 근무 없음)으로 읽는다. 골든 16개의 간호사는 전부 입사일이 대상 월 한참 전이고 퇴사일이 없어 구간이 월 전체 그대로라
    #   어느 골든 층에도 값이 안 나타난다(`test_precheck_resignation_characterization` 가 그 근거를 고정한다).
    #   허용 0(빈 `StageDiff`) 그 자체가 '10f 는 어느 골든도 바꾸지 않는다'는 단언이다.
    "10f": StageDiff(),
}
EI10_DONE: tuple[str, ...] = ("10c", "10d", "10e", "10f")
#: EI-11 — 카드·처방 하드락 필터(ei11/EI11_SPEC_v2.md). 단계 이름은 그 단계 프로덕션 커밋에서 EI11_DONE 에 더한다.
#: 11a0 은 판정 부품(`services.policy.card_filter`·`hard_lock.release_violations`)만 더하고 배선이 0 이라 어느 골든도
#: 바꾸지 않는다 — 허용 0(빈 `StageDiff`) 그 자체가 단언이다.
#: 11a1 은 싱크 6자리 배선과 탐색 카탈로그 하드락 7항목 삭제(#1 은 상한 클램프)다. 사다리 3개(S1-L·S1-LT·S1-LB)는 하드락
#:   탐색 시도 6개가 사라져 attempts 26→20·26→20·28→22, 쿼리 각 −60 이다(시도 하나에 재조회 10회). 카드 칸에는 새 칸 3개
#:   (`blocked_by_hard_lock` 0 · `policy_forbidden` [] · `policy_filter_error` 0)와 `cause:off_budget` 카드(`resolution_options[1]`)의
#:   `hard_lock_relax_marks` 가 붙는다. S9·S9-F 는 새 칸 3개뿐이다. presolve EI5 는 night_recovery 처방이 든 번들 카드
#:   (`resolution_options[3]`)가 빠지고 `policy_forbidden` 1건(card)이 생긴다. attempts 경로 허용은 넓다 — '하드락 탐색 시도만
#:   사라졌다'는 `test_ladder.test_ei11_only_hard_lock_probe_attempts_removed` 가 판정 함수로 단언한다(목록이 재번호라 경로로는
#:   못 가린다).
#: 11a2 는 처방 set cover 에서 금지 처방(하드락 완화 3종)을 후보에서 빼고 생성 진입 관문(400)을 둔다. 주 골든은 0 이다 — 관문은
#:   S8·S1-LT 의 처방(`treatment:soft:grade_max`)·override(인접 키)를 통과시키고 판정 기록은 DEBUG 뿐이다. presolve EI5 는 3번째
#:   번들이 coverage_min 단독이 되어(`treatment_recommendations[2]`·`resolution_options[3]`) graph 노드 11→10·간선 24→21 이고
#:   `policy_forbidden` [] · `blocked_by_hard_lock` 0 이다(싱크가 거를 카드가 생산되지 않는다). 서술·hard_case 는 그대로다.
#: 11b 는 요청 가드가 하드락 BLOCK 을 모두 400 으로 막고(D13 ③ `max_consecutive_nights` 는 값과 무관하게 400) 경계 L 이 정본
#:   `allow_three_consecutive_nights` 만 읽는다(D13 ①). 골든은 0 이다 — 하니스는 서비스를 직접 부르고 별칭을 싣지 않으며(S8 override 는 인접 키),
#:   경계 L 값은 같다. 허용 0(빈 `StageDiff`) 그 자체가 단언이다.
#: 11c 는 카드가 아닌 출구 칸에 '정책상 금지' 표지를 단다(H1 — §2.5). presolve EI5 는 problem_list 의 night_recovery 처방
#:   (`problem_list[2].mapped_treatments[0]`)에 `policy_forbidden`·`rule_nos` 가 붙고, 월 나이트 상한 처방 rationale 에 '(최대 15 —
#:   하드락 #6)' 이 들어가며(처방 목록 `[0|1]`·서술 action_levers·problem_list·graph 처방 노드 — EI5 카드는 그 처방이 수동이라
#:   그대로), 그 원인의 `cause_solution_ko` 가 다시 지어지고, `policy_forbidden` 에 처방 항목 1건(덮는 원인 monthly_night_shortage)·
#:   `blocked_by_hard_lock` 1, fix_plan 월간 N 한도 축(`axis_actions[2]`) 문구에 상한이 붙는다. 주 골든은 0 이다 — coupled_sequence 문구를 담은 골든이 없고 S1-LT 의 처방
#:   (`treatment:undiagnosed`)은 금지도 상한형도 아니며 표지 판정 기록은 DEBUG 뿐이다.
#: 11d 는 constraint_impact '적용 불가' 표지(모듈 목록·조정 기록·conflict_probe)다. 골든은 0 이다 — 조정 기록 표지는 기본값이면
#:   칸이 없고(S8·S1-LT 의 `_applied_constraint_adjustments` 는 GradeMax force_soft — 하드락 무관), conflict_probe 는 주 골든에
#:   없다(기본 lex 경로 0). 허용 0(빈 `StageDiff`) 그 자체가 단언이다.
#: 11e 는 탐색 결과 세 칸(`probe_found`·`probe_baseline_feasible`·`probe_timed_out` — `undiagnosed_probe.probe_status_fields`)을
#:   payload 에 싣는다. 탐색을 돌린 사후 경로는 사다리 3개뿐이라 그 셋에 `probe_baseline_feasible` false · `probe_timed_out` false 가
#:   새로 생기고 `probe_found` true 는 그대로다(하니스가 탐색 시간 상한을 끄고 기준 재계산은 실패라 둘 다 거짓 — scenario_ladder.py).
#:   S9·S9-F·presolve 3개는 precheck 차단이라 탐색 단계가 없어 칸이 없다. 다른 주 골든은 http_detail 이 없다.
#: 11f 는 GradeMin force_soft 죽은 쓰기(control 매트릭스·목록 추정·핸들러)·온톨로지 처방 `treatment:soft:grade_min`·레지스트리
#:   DEAD 행을 지우고, 원인 `cause:grade:min_sum_over_need` 를 수동 처방 `treatment:data:fix_config` 에 잇는다(Q11). 골든은 0 이다 —
#:   어느 골든에도 등급 최소 원인·처방이 없다. 허용 0(빈 `StageDiff`) 그 자체가 단언이다.
#: 11z 는 precheck 문구 표(`precheck.messaging._MESSAGES`)에 `PER_NURSE_SEQUENCE_INFEASIBLE` 문구를 등록한다(EI-10d 후속 U7).
#:   골든은 0 이다 — 어느 골든에도 이 코드가 없다. 허용 0(빈 `StageDiff`) 그 자체가 단언이다.
#: 11r 은 리뷰 반영이다. ① 월중 경계(파견·복귀) 금지칸·강제 OFF·OFF 창이 엔진 입력에 닿는다(`_run_cp_sat_basic` 이 호출자 config 의
#:   몫을 합친다 — 예전엔 덮어써 엔진에 안 닿았다). 그래서 S6 의 L1 `initial_constraints` 에 X01 강제 OFF(19)·19일 D/E 금지와
#:   N07 빈 강제 OFF 가 생긴다(주 골든 S6 재생성 · 모델 지문 S6 재생성 — S6 의 OFF 창은 월경계 창이 없어 그대로다). ② 처방 동의
#:   문구(`apply_hint.human_message_ko`)에 상한 문구 · 상한 문구 단위('15회') · fix_plan 조사('한도를') · 관문 처방 이름(카드 제목
#:   출처)이 presolve EI5 의 이미 허용된 칸 안에서 바뀐다. 이번에 EI-11 허용을 실제 차이로 좁혔다(리뷰 — presolve 대조가 카드 목록을
#:   지키지 못했다): 카드는 사다리의 `resolution_options[n].hard_lock_relax_marks` 와 EI5 의 `[3]`(night_recovery 번들 → coverage_min
#:   단독) 다섯 칸만, fix_plan 은 EI5 `axis_actions[2].human_message_ko` 만, 쓰이지 않던 `hard_case`·`protected_axes`·
#:   `axis_actions_truncated` 갈래는 뺐다. EI5 카드 id 목록은 presolve 특성화가 따로 단언한다.
_EI11_LADDERS = ("S1-L", "S1-LT", "S1-LB")
_EI11_INF = r"^\$\.http_detail\.infeasibility\."
_EI11_POLICY_FIELDS = _EI11_INF + r"(blocked_by_hard_lock|policy_forbidden|policy_filter_error)(\[\d+\]|\.|$)"
_EI11_CARDS = (_EI11_INF + r"resolution_options\[(\d+\]\.hard_lock_relax_marks"
               r"|3\]\.(changes|option_id|title_ko|trade_off_ko|treatment_ids))(\[\d+\]|\.|$)")
_EI11_ATTEMPTS = r"^\$\.layers\.attempts(\[\d+\]|\.|$)"
_EI11_TREATMENT_VIEWS = _EI11_INF + r"(treatment_recommendations|graph)(\[\d+\]|\.|$)"
_EI11_MARK_VIEWS = (_EI11_INF + r"resolution_narrative\.(problem_list\[\d+\]\.(mapped_treatments(\[\d+\]|\.|$)|cause_solution_ko$)"
                    r"|action_levers\[\d+\]\.rationale_ko$)")
_EI11_FIX_PLAN = _EI11_INF + r"fix_plan\.axis_actions\[2\]\.human_message_ko$"
_EI11_PROBE_STATUS = _EI11_INF + r"probe_(baseline_feasible|timed_out)$"
_EI11_NEW_FIELDS = ("$.http_detail.infeasibility.blocked_by_hard_lock",)
#: 11r ① — S6 월중 경계 간호사(X01 파견 in · N07 파견 out 복귀)의 엔진 입력 금지칸·강제 OFF 만.
_EI11R_MID_MONTH = r"^\$\.layers\.L1\[\d+\]\.config_data\.initial_constraints\.(forced_off|forbidden)\.(X01|N07)(\.|\[|$)"
#: 11r ② — 상한형 처방의 동의 문구(처방 목록 안 `apply_hint`).
_EI11R_APPLY_HINT = _EI11_INF + r"treatment_recommendations\[\d+\]\.treatments\[\d+\]\.apply_hint\.human_message_ko$"
#: 11q 는 품질 강화다(점검 `ei11/q11/AUDIT.md`). ① 월중 경계 N 꼬리 이어짐(G1) — 상대 병동 근무가 N 으로 끝나면 이 병동이 경계일부터
#:   이어 붙일 N 을 연속 상한(#3)·회복(#4·#5)을 엔진이 볼 수 있는 만큼으로 줄인다(OFF 창 또는 경계일 N 금지 —
#:   `roster_create_service._night_extension_limit`). S6 의 X01(상대 꼬리 DDDDNN · 기본 설정 3N 허용·3N 뒤 2OFF)은 경계일(19) N 금지가
#:   되어 precheck 입력(L0)의 19일 금지칸이 [D,E] → [D,E,N] 이 된다. 엔진 입력(L1)은 X01 의 허용 근무(D·E) 금지칸이 이미 N 을 막고
#:   있어 그대로다(모델 지문 S6 도 그대로). ② 경계 빌더 실패를 삼키지 않는다(G5 — 생성 중단). 어느 골든도 빌더가 실패하지 않아 0 이다.
#:   ③ 리뷰 반영 — 월초 근처 월중 경계가 상대 병동 전월 꼬리를 잇고(G7) · 하드락 #6 이 같은 달 상대 병동 나이트를 뺀 몫으로 개인 월 N
#:   상한을 건다. 골든은 0 이다 — S6 의 경계일은 6일 뒤(19·14)라 전월을 안 보고, X01 은 상대 나이트 2 · 이 병동 6일이라 상한이 안 걸린다.
_EI11Q_MID_MONTH_N =r"^\$\.layers\.L0\[\d+\]\.config_dict\.initial_constraints\.forbidden\.X01\.19(\[\d+\])?$"
EI11_STAGES: dict[str, StageDiff] = {
    "11a0": StageDiff(),
    "11a1": StageDiff(
        change_patterns=(_EI11_ATTEMPTS, _EI11_POLICY_FIELDS, _EI11_CARDS),
        must_change={
            **{n: ("$.layers.attempts", *_EI11_NEW_FIELDS,
                   "$.http_detail.infeasibility.resolution_options[1].hard_lock_relax_marks") for n in _EI11_LADDERS},
            "S9": _EI11_NEW_FIELDS, "S9-F": _EI11_NEW_FIELDS,
            "presolve/EI5-precheck-block": ("$.http_detail.infeasibility.resolution_options",
                                            "$.http_detail.infeasibility.policy_forbidden"),
            "presolve/S9": _EI11_NEW_FIELDS, "presolve/S9-F": _EI11_NEW_FIELDS,
        },
        query_delta={n: 60 for n in _EI11_LADDERS},
    ),
    "11a2": StageDiff(
        change_patterns=(_EI11_TREATMENT_VIEWS,),
        must_change={"presolve/EI5-precheck-block": ("$.http_detail.infeasibility.treatment_recommendations[2]",
                                                     "$.http_detail.infeasibility.graph.nodes")},
    ),
    "11b": StageDiff(),
    "11c": StageDiff(
        change_patterns=(_EI11_MARK_VIEWS, _EI11_FIX_PLAN),
        must_change={"presolve/EI5-precheck-block": (
            "$.http_detail.infeasibility.resolution_narrative.problem_list[2].cause_solution_ko",
            "$.http_detail.infeasibility.resolution_narrative.problem_list[2].mapped_treatments[0].policy_forbidden",
            "$.http_detail.infeasibility.fix_plan.axis_actions[2].human_message_ko")},
    ),
    "11d": StageDiff(),
    "11e": StageDiff(
        change_patterns=(_EI11_PROBE_STATUS,),
        must_change={n: ("$.http_detail.infeasibility.probe_baseline_feasible",
                         "$.http_detail.infeasibility.probe_timed_out") for n in _EI11_LADDERS},
    ),
    "11f": StageDiff(),
    "11z": StageDiff(),
    "11r": StageDiff(
        change_patterns=(_EI11R_MID_MONTH, _EI11R_APPLY_HINT),
        must_change={
            "S6": ("$.layers.L1[0].config_data.initial_constraints.forced_off.X01",
                   "$.layers.L1[0].config_data.initial_constraints.forbidden.X01.19"),
            "presolve/EI5-precheck-block": (
                "$.http_detail.infeasibility.treatment_recommendations[0].treatments[1].apply_hint.human_message_ko",
                "$.http_detail.infeasibility.treatment_recommendations[1].treatments[1].apply_hint.human_message_ko"),
        },
    ),
    "11q": StageDiff(
        change_patterns=(_EI11Q_MID_MONTH_N,),
        must_change={"S6": ("$.layers.L0[0].config_dict.initial_constraints.forbidden.X01.19",)},
    ),
    #: 11w-1(G4 · D35) — 전월 말일에 끝난 파견에서 1일 복귀하는 간호사의 월경계 꼬리를 상대 병동 전월 근무표로 보충한다. 골든은 0 이다 —
    #:   골든 시나리오에 1일 복귀가 없어 `_return_source_map` 이 안 생기고, 배정 조회는 종료일 조건만 하루 넓힌 같은 테이블 1회라
    #:   쿼리 순서열이 같다(`roster_create_service._assignments_with_day1_returns`).
    "11w1": StageDiff(),
    #: 11w-2(G3 · D34) — 출발 경계: 상대 병동 같은 달 근무표(마감본 우선, 없으면 작성 중)의 첫 칸들에 맞춰 이 병동 마지막 날들에
    #:   전이 금지칸·연속근무 OFF 창·N 이어 붙이기 금지를 건다. S6 의 N07 은 11/10 G2 파견 출발이고 G2 첫 칸이 D,D 라 9일(색인 8)
    #:   E·N 금지 · 6~9일(색인 5~8) OFF ≥1 이 precheck 입력(L0)과 엔진 입력(L1)에 생긴다(모델 지문 S6 도 바뀐다 — D37).
    #:   X01(26일 G2 복귀)·N08(16일 G2 병동이동)도 출발 경계지만 S6 의 G2 근무표에 그 뒤 칸이 없어 칸 0 이다. 월중 경계 빌더가 상대
    #:   병동마다 근무표·근무코드·칸을 한 번씩만 읽어(`_load_other_ward_cells`) S6 쿼리가 4건 준다(144 → 140 · 순서열은 부분열).
    "11w2": StageDiff(
        change_patterns=(r"^\$\.layers\.L0\[\d+\]\.config_dict\.initial_constraints\.forbidden\.N07(\.|\[|$)",
                         r"^\$\.layers\.L0\[\d+\]\.config_dict\.off_window_constraints\.N07(\.|\[|$)",
                         r"^\$\.layers\.L1\[\d+\]\.config_data\.off_window_constraints\.N07(\.|\[|$)"),
        must_change={"S6": ("$.layers.L0[0].config_dict.initial_constraints.forbidden.N07",
                            "$.layers.L0[0].config_dict.off_window_constraints.N07",
                            "$.layers.L1[0].config_data.initial_constraints.forbidden.N07",
                            "$.layers.L1[0].config_data.off_window_constraints.N07")},
        query_delta={"S6": 4},
    ),
    #: 11w-3(G2 · D32) — 확정 원티드·특수 근무 칸만 하드락 면제, 엔진 칸은 지킨다: 경계 영향 구간에 비-O 확정 칸이 있고 바깥 꼬리에
    #:   의무가 있는 간호사만 조정 칸이 더해진다. 골든 시나리오에는 경계 영향 구간의 비-O 확정 칸이 없어(S5·S8 의 고정 칸은 경계
    #:   꼬리가 없고 S7 은 확정 원티드가 없다) 골든 변화 0 이다. 쿼리도 늘지 않는다(조회 없이 맵만 짓는다).
    "11w3": StageDiff(),
    #: 11w-r(리뷰 반영) — 이 달 확정 칸만의 N 블록에 바깥 꼬리용 상한을 걸지 않는다 · 닫힌 모든 N 블록의 회복 빚(바깥이 먼저 어긴
    #:   블록은 0) · 월중 경계 근무코드 정의를 그룹 shifts 전체로(모르는 코드가 있을 때만 읽음). 골든 시나리오에 경계 영향 구간의
    #:   비-O 확정 칸이 없어 골든 변화 0 · 쿼리 변화 0.
    "11wr": StageDiff(),
    #: 11w-n(D39) — 작성 중 상대 근무표 안내를 성공 응답 `weekly_off_warnings` 에서 새 칸 `boundary_notices`(있을 때만)로 옮긴다.
    #:   골든은 서비스 반환값을 적지 않고(`harness._call` 이 버린다 · 기록 칸은 층·쿼리·로그·삼킨 실패 줄) 조회·로그도 그대로라 변화 0.
    "11wn": StageDiff(),
}
EI11_DONE: tuple[str, ...] = ("11a0", "11a1", "11a2", "11b", "11c", "11d", "11e", "11f", "11z", "11r", "11q",
                              "11w1", "11w2", "11w3", "11wr", "11wn")   # 단계 커밋마다 하나씩 더한다
#: EI-13 — 갈래 A(ei13_15/A/SPEC.md). 13a-2 의 쿼리 감소(재조회 제거)만 등록한다 — 13a-1·13b 는 골든 변화 0.
#: 13a-2 는 첫 시도 직전에 빌더를 한 번 부르고(`build_engine_input_at_first_attempt`) 시도마다 ①~⑦ 재조회를 없앤다. 첫 시도는 precheck·
#:   나이트 블록이 이미 읽은 단계 A getter(등급·주말휴무·팀·근무코드) 4개가 줄고, 사다리는 시도마다 ①~⑦ 전부(약 10)가 더 준다.
#:   `L0`~`L2`·`digest`·`attempts[*].delta/result` 는 바뀌지 않는다(`$.queries.*`·`attempts[*].queries_before` 만 바뀐다).
#:   값은 `골든(20261022_11w-3) count − 새 count`(실측). 차단 경로(S9·S9-F·presolve)는 0 이라 적지 않는다.
EI13_STAGES: dict[str, StageDiff] = {
    "13a": StageDiff(query_delta={
        **{n: 4 for n in ("S1", "S1-D", "S1-P", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S1-L3")},
        # ★ 통합(int1) — 14b2a4 의 S1-L 계열 재설계로 시도 수가 줄어 새 골든(20261023_14b2a4) 대비 감소가
        #   174·174·194 다(실측 · 옛 시나리오에서는 194·194·214).
        "S1-L": 174, "S1-LT": 174, "S1-LB": 194,
    }),
}
EI13_DONE: tuple[str, ...] = ("13a",)
#: EI-14 갈래 C1 — precheck 시점 소비자(범위 선언·IR 뷰·온톨로지 표기·개인 DP·OFF 바닥)는 골든 소비자 출력 층이 없어 골든 변화 0 이다.
#:   하위 단계마다 `StageDiff()`(변화 0)를 등록해 '0 이다'를 시험이 고정한다(통합 때 A·B·C2 와 이 dict 만 합친다).
EI14C1_STAGES: dict[str, StageDiff] = {
    #: 14a — 범위 선언의 미지원 마커 이름을 실제 키(레지스트리)로. 소비자 출력이 골든에 없다.
    "14a": StageDiff(),
    #: 14b1a — precheck 시점 소비자 3곳을 to_roster_ir 입력으로(불변·복사 0·조회 0). 골든 값·쿼리 변화 0.
    "14b1a": StageDiff(),
    #: 14d — isolated_work_penalty_weight 죽은 레버 처방 삭제(yaml config_key null). 골든에 해당 키·action:soft 0건.
    "14d": StageDiff(),
    #: 14r1 — 경고 2종의 온톨로지 심각도 표기(precheck_warning/soft). EXPECTED_ISSUES 16개 전부 이슈 없음이라 골든 0.
    "14r1": StageDiff(),
    #: 14b2a1 — 정본 키 번역(max_conseq_work·allow_three_consecutive_nights·ban_n_to_d → 그래프 이름) 겹침. 소비자 출력이 골든에 없어 0.
    "14b2a1": StageDiff(),
    #: 14b2a2 — 고정 셀 → initial_constraints 겹침(면제 인접 간호사 제외). 소비자 출력이 골든에 없어 0.
    "14b2a2": StageDiff(),
    #: 14b2a3 — by_day 수요(그래프 엔진은 뷰가 준 사설 키 _ir_by_day 만 읽는다). 소비자 출력이 골든에 없고 사후 소비자는 사설 키가 없어 0.
    "14b2a3": StageDiff(),
    #: 14r2 — 개인 DP 에 MID 규칙(mid_transition·mid_to_day_ban). 골든 병동 MID 고정 쌍 없음(EXPECTED_ISSUES 16개 전부 빈 집합)이라 0.
    "14r2": StageDiff(),
    #: 14r3 — OFF 하드 하한(min_off)을 용량 바닥에 반영. EXPECTED_ISSUES 16개 전부 빈 집합 유지라 골든 0.
    #:   ★ 골든 병동은 용량이 넉넉해 `golden/` 16개는 0 이지만, golden_presolve 의 `EI5-precheck-block`(RN 2명 · off_days 8 · staffing_first
    #:   기본 False)은 근무 용량 증거가 바뀐다: 간호사 한 명의 근무가능일 25(연속근무 닫힌형) → 22(= 30 − min_off 8) —
    #:   용량 합 50 → 44 · 부족 130 → 136 · 용량 숫자가 든 문구(`rendered_ko`). 차단 여부·이슈 코드·카드 id 는 그대로다.
    #:   동결본(FROZEN_SHA256)은 다시 쓰지 않고 허용 차이 패턴으로 받는다.
    "14r3": StageDiff(
        change_patterns=(
            r"^\$\.http_detail\.infeasibility\.(?:causes\[\d+\]\.(?:details|evidence)|preflight_issues\[\d+\]\.evidence"
            r"|resolution_narrative\.problem_list\[\d+\]\.raw_evidence)\.(?:capacity|capacity_total|shortage"
            r"|lowest_capacity_nurses\[\d+\]\.capacity_days|night_capable_nurses\[\d+\]\.(?:cap_by_axis\.working|capacity_days))$",
            r"^\$\.http_detail\.infeasibility\.resolution_narrative\.problem_list\[\d+\]\.rendered_ko$",
        ),
        must_change={"EI5-precheck-block": ("$.http_detail.infeasibility.causes[1].details.capacity",)},
    ),
    #: 14b2a4 — presolve 가 일자별 수요(_ir_by_day)를 봄 + S1-L·S1-LT·S1-LB 시나리오 재설계(Hall 결합 부족). 골든 S1-L 계열 3개만 재생성(399·419·406 경로), 나머지 13 변화 0. 기준본(golden_base/dev4ec454e/S1-L*)도 순수 dev 에서 새 시나리오로 다시 떠서 허용 차이 패턴이 따로 필요 없다.
    "14b2a4": StageDiff(),
    #: C1r — CAPACITY_TOTAL_SHORTAGE 증거에 `off_floor_binding`(OFF 하한이 원인일 때 off_days 레버 · 아니면 null) 키가 생긴다.
    #:   골든 병동은 용량이 넉넉해 `golden/` 16개는 0, golden_presolve 의 EI5-precheck-block 증거에만 null 로 새로 생긴다
    #:   (`conseq_cap_binding` 과 같은 자리). 동결본(FROZEN_SHA256)은 다시 쓰지 않는다.
    "C1r": StageDiff(
        change_patterns=(
            r"^\$\.http_detail\.infeasibility\.(?:causes\[\d+\]\.(?:details|evidence)|preflight_issues\[\d+\]\.evidence"
            r"|resolution_narrative\.problem_list\[\d+\]\.raw_evidence)\.off_floor_binding$",
        ),
    ),
}
EI14C1_DONE: tuple[str, ...] = ("14a", "14b1a", "14d", "14r1", "14b2a1", "14b2a2", "14b2a3", "14r2", "14r3", "14b2a4", "C1r")   # 단계마다 하나씩 더한다
#: EI-12(Codex 1차 — 갈래 X) — 12u 는 골든 변화 0 이라 등록하지 않았다. 12u2(상대 병동 근무코드를 그 병동 정의·근무 슬롯으로 읽는다)는
#:   월중 경계 빌더가 상대 병동마다 근무 슬롯(ShiftManage)을 한 번 더 읽어 S6 쿼리가 1건 는다(13a 뒤 숫자 136 → 137 · G2 근무코드 조회
#:   바로 뒤 `select:shift_manage`). 값은 그대로다 — S6 의 G2 는 근무코드·슬롯 행이 없고 칸이 D/E/N/OFF 뿐이다.
EI12_STAGES: dict[str, StageDiff] = {
    "12u2": StageDiff(query_delta={"S6": -1}, query_added={"S6": ("select:shift_manage",)}),
}
EI12_DONE: tuple[str, ...] = ("12u2",)
#: 12x(Codex 1차 — 갈래 X) — ALL_STAGES 에 넣지 않는다(골든 `golden/` 16개는 변화 0 · golden_presolve 의 EI5-precheck-block 만 닿는다).
#:   golden_presolve 시험이 직접 합친다.
EI12X_STAGES: dict[str, StageDiff] = {
    #: 12x — 일자별 인원 부족 차단 문구(D28-1~3) + '일별 시프트 요구 인원 하향' 카드를 버튼 없는 직접 수정 안내로(누르면 바뀌는 게 0).
    #:   golden_presolve 의 EI5-precheck-block(30일 전부 부족)만 닿는다: 원인·이슈 문장에 필요/가능 숫자(꼬리)·첫 이슈에 막힌 날 전부,
    #:   원인 details·problem_list.raw_evidence 에 날짜별 숫자(day_evidence), 해당 카드는 treatment_ids 비움(manual_required 로)·제목 '(직접 수정)'·
    #:   안내문. 차단 여부·이슈 코드·원자료 day(0 기준)는 그대로다. 동결본(FROZEN_SHA256)은 다시 쓰지 않는다.
    "12x": StageDiff(
        change_patterns=(
            r"^\$\.http_detail\.(?:infeasibility\.summary_message_ko|infeasibility\.(?:causes|preflight_issues)\[\d+\]\.human_message_ko"
            r"|infeasibility\.causes\[\d+\]\.(?:details|evidence)\.day_evidence"
            r"|infeasibility\.resolution_narrative\.problem_list\[\d+\]\.(?:rendered_ko|raw_evidence\.(?:day_evidence|human_message_ko))"
            r"|infeasibility\.resolution_options\[\d+\]\.(?:fix\.how_ko|manual_required|title_ko|treatment_ids))$",
        ),
        must_change={"EI5-precheck-block": ("$.http_detail.infeasibility.causes[0].human_message_ko",)},
    ),
}
EI12X_DONE: tuple[str, ...] = ("12x",)
#: EI-13c(갈래 A2) — 성능·구조 단계. 골든은 다시 쓰지 않는다 — 값은 **직전 단계 대비 쿼리 수 감소**(실측)이고 단계마다 합산된다.
#:   13c2(팀 N+1 일괄): 감소 = 2×(조회 대상 간호사 수) − 2(구간 행이 없는 하네스 병동은 폴백 1문장이 든다). 변화 경로는
#:   `$.queries.*`·`attempts[*].queries_before` 뿐이다(team_id 값은 그대로).
_EI13C2_DELTA = {
    **{n: 20 for n in ("S1", "S1-D", "S1-P", "S2", "S3", "S4", "S5", "S7", "S8", "S9", "S9-F", "S1-L3")},
    "S6": 22,
    "S1-L": 24, "S1-LT": 24, "S1-LB": 24,
    # presolve 동결본(`presolve/<이름>`) — 같은 감소를 쿼리 수 대조에서 받는다.
    "presolve/S9": 20, "presolve/S9-F": 20, "presolve/EI5-precheck-block": 4,
}
#:   13c3(공휴일 OFF·근무코드 getter 1회 + 고정근무 병합 재사용): 고정근무자 없는 경로는 병합 읽기 3개가 사라지고(S1-L·LB·L3),
#:   고정근무자 있는 경로는 병합 3 + 차감의 근무코드 1(S1-LT 4) 또는 병합 근무코드/공휴일 일부(S5·S9-F 1). 직전 단계(13c2) 대비 실측.
_EI13C3_DELTA = {"S5": 1, "S9-F": 1, "S1-L": 3, "S1-LB": 3, "S1-L3": 3, "S1-LT": 4, "presolve/S9-F": 1}
EI13C_STAGES: dict[str, StageDiff] = {
    "13c2": StageDiff(query_delta=_EI13C2_DELTA),
    "13c3": StageDiff(query_delta=_EI13C3_DELTA),
}
EI13C_DONE: tuple[str, ...] = ("13c2", "13c3")   # 단계마다 하나씩 더한다
#: EI-10g(갈래 D · 2차) — precheck 가 엔진 몫을 본다(p2/D/SPEC.md). 단계 이름은 그 단계 프로덕션 커밋에서 EID_DONE 에 더한다.
#:   쿼리를 더하는 단계는 `query_added` + 음수 `query_delta`, 순서만 바뀌는 단계는 `query_reorder`(다중집합 동일).
_EID_10G3_ADDED = ("select:schedules", "select:schedules", "select:shifts", "select:nurse_weekendoff_period",
                   "select:nurse_allowed_shift_period", "select:banned_wanted_entries")
EID_STAGES: dict[str, StageDiff] = {
    #: 10g2 — 고정근무 차감이 precheck 앞으로(단계 A 직후). precheck 입력(L0)의 일자별 수요가 차감 뒤 값이 된다: 고정근무자가 있는 S5(D 2→1)·
    #:   S1-LT(D 4→3)·S9-F(N 1→0)만 L0 수요가 바뀌고 나머지 13개는 변화 0. 쿼리 수는 전 시나리오 같고 차감 읽기(`schedules` 아닌
    #:   `information_schema.columns`·`roster_config`·`shifts`·`fixed_wanted_entries`)가 precheck 입력 조회(등급·주말휴무·팀) 앞으로 와
    #:   순서만 바뀐다(`query_reorder`).
    "10g2": StageDiff(
        change_patterns=(r"^\$\.layers\.L0\[0\]\.config_dict\.daily_shift_requirements_by_day\[\d+\]\.[DEMN]$",),
        must_change={name: ("$.layers.L0[0].config_dict.daily_shift_requirements_by_day",) for name in ("S5", "S1-LT", "S9-F")},
        query_reorder=("S5", "S1-LT", "S9-F"),
    ),
    #: 10g3 — 단계 B(첫 시도 직전 빌더)가 precheck 앞으로. 골든 값 변화는 0 이고 쿼리만 바뀐다: 비차단 14개는 수 같음·순서만(허용근무·전월
    #:   `schedules` 가 precheck 입력 조회 앞으로), 나이트 차단 S9·S9-F 는 빌더 조회가 더해진다(+6 — 차단 전에 이제 빌더가 돈다). 합성 골든은 파견·
    #:   전월 근무표가 거의 없어 이 숫자는 합성 기준이다(`test_ei10g_query_paths.py` 의 경로별 기대표).
    "10g3": StageDiff(
        query_reorder=tuple(f"{p}{s}" for p in ("S",) for s in ("1", "1-D", "1-P", "2", "3", "4", "5", "6", "7", "8", "9", "9-F",
                                                                "1-L", "1-LT", "1-LB", "1-L3")),
        query_delta={"S9": -6, "S9-F": -6},
        query_added={"S9": _EID_10G3_ADDED, "S9-F": _EID_10G3_ADDED},
    ),
    #: 10g4 — 단계 B 제약(허용근무 기간표·전월 경계·금지 원티드·월중 경계 병합본, 고정 칸 있는 날 제외)을 precheck 입력에 엔진 키 이름 그대로 싣는다.
    #:   비어 있으면 키를 안 만든다 — 합성 골든에서는 금지/강제가 있는 S4·S6·S7·S8·S1-L·S1-LT·S1-LB 의 L0 `config_dict.initial_constraints` 만 바뀌고
    #:   (S6 은 월중 경계 병합 몫까지) 쿼리·나머지 칸 변화는 0 이다.
    "10g4": StageDiff(
        change_patterns=(r"^\$\.layers\.L0\[0\]\.config_dict\.initial_constraints(?:\..*)?$",),
        must_change={name: ("$.layers.L0[0].config_dict.initial_constraints",)
                     for name in ("S4", "S6", "S7", "S8", "S1-L", "S1-LT", "S1-LB")},
    ),
}
EID_DONE: tuple[str, ...] = ("10g2", "10g3", "10g4")   # 단계 커밋마다 하나씩 더한다
#: NM — roster_config 14칸의 파이썬 이름 정리(`services.engine_contract.names`). 엔진 입력 dict 키만 새 이름이 되고 값·쿼리는 같다.
#:   골든에서 옛 이름 키가 있는 dict 는 7종이다(실측) — L0 config_dict · L0p config(ORM __dict__) · L1 config_data · L2 settings_eff ·
#:   L3 objects(후처리 latest_config) · attempts[].delta.config_data.changed 는 파이썬 이름이 되고, L3 calls[].args.req(RosterRequest
#:   요청 모델 원문)와 golden_presolve snapshot.config(시도 스냅숏 = 저장 JSON)는 바깥 이름 그대로다.
NM_STAGES: dict[str, StageDiff] = {
    "nm": StageDiff(
        key_renames=dict(EXTERNAL_TO_INTERNAL),
        key_renames_skip=(r"^\$\.layers\.L3\.calls\[\d+\]\.args\.req$", r"^\$\.snapshot\.config$"),
    ),
}
NM_DONE: tuple[str, ...] = ("nm",)
ALL_STAGES: dict[str, StageDiff] = {**EI2_STAGES, **EI10_STAGES, **EI11_STAGES, **EI13_STAGES, **EI14C1_STAGES, **EI12_STAGES, **EI13C_STAGES,
                                     **EID_STAGES, **NM_STAGES}
ALL_DONE: tuple[str, ...] = (EI2_DONE + EI10_DONE + EI11_DONE + EI13_DONE + EI14C1_DONE + EI12_DONE + EI13C_DONE + EID_DONE
                             + NM_DONE)


def _active_stages(done: tuple[str, ...], stages: Mapping[str, StageDiff]) -> tuple[StageDiff, ...]:
    return tuple(stages[name] for name in done)


def _is_subsequence(sub: list, full: list) -> bool:
    """sub 가 full 의 부분열(순서 유지, 중간 건너뛰기 허용)인가."""
    it = iter(full)
    return all(item in it for item in sub)


def _is_subsequence_without(sub: list, full: list, added: list) -> bool:
    """`added` 의 각 쿼리를 sub 에서 한 번씩(어느 자리든) 뺀 나머지가 full 의 부분열이 되는 경우가 있는가(EI-12 12u2).
    sub 에 없는 쿼리를 적었으면 거짓이다."""
    if not added:
        return _is_subsequence(sub, full)
    head, rest = added[0], added[1:]
    return any(_is_subsequence_without(sub[:i] + sub[i + 1:], full, rest) for i, item in enumerate(sub) if item == head)


def _check_queries(name: str, golden_q: dict, actual_q: dict, expected_delta: int,
                   added: tuple[str, ...] = ()) -> list[str]:
    """(c) 쿼리 수 감소 = 기대치, 남은 순서열(단계가 더한 쿼리 `added` 를 뺀 것) = 이전 순서열의 부분열."""
    out: list[str] = []
    got_delta = golden_q["count"] - actual_q["count"]
    if got_delta != expected_delta:
        out.append(f"{name}: 쿼리 수 변화 {got_delta}건(기대 {expected_delta}건) — "
                    f"골든 {golden_q['count']} → 지금 {actual_q['count']}")
    if not _is_subsequence_without(list(actual_q["tables"]), list(golden_q["tables"]), list(added)):
        out.append(f"{name}: 남은 쿼리 순서열이 이전 순서열의 부분열이 아니다"
                   + (f"(단계가 더한 쿼리 {list(added)} 를 빼고 봐도)" if added else ""))
    return out


def _sorted_queries(queries: dict) -> dict:
    """(EI-10g `query_reorder`) 쿼리 순서열을 정렬한 사본 — 부분열 검사가 '다중집합 포함'이 된다(수는 따로 본다)."""
    return {**queries, "tables": sorted(queries["tables"])}


def _classify_line(name: str, line: str, vanish_pats: list, change_pats: list) -> tuple[str, str]:
    """diff 줄 하나 → (종류, 경로 또는 위반 문구). 종류는 skip·changed·vanished·violation."""
    path, _, rest = line.partition(":")
    if path == _DIGEST_PATH or path.startswith(_QUERIES_PREFIX) or _ATTEMPT_QUERIES_BEFORE.match(path):
        return "skip", path  # (d) — digest·queries(전체 집계·시도별 위치)는 따로 다룬다.
    if any(pattern.match(path) for pattern in change_pats):
        return "changed", path  # EI-10 판정 변경 단계가 허용한 경로(값 변경·새로 생김·사라짐)
    if _VANISH_MARK not in rest:
        return "violation", f"{name}: 허용 밖 변화(사라짐이 아니다) — {line}"
    if not any(pattern.match(path) for pattern in vanish_pats):
        return "violation", f"{name}: 허용 밖 소멸 — {line}"
    return "vanished", path


def _rename_dict_keys(obj, renames: Mapping[str, str], skip: tuple = (), path: str = "$"):
    """(NM `key_renames`) dict 키만 바꾼 깊은 사본 — 값(문자열 포함)은 그대로 둔다. `skip` 정규식에 맞는 경로 아래는 그대로."""
    if any(pattern.match(path) for pattern in skip):
        return obj
    if isinstance(obj, dict):
        return {renames.get(k, k): _rename_dict_keys(v, renames, skip, f"{path}.{k}") for k, v in obj.items()}
    if isinstance(obj, list):
        return [_rename_dict_keys(v, renames, skip, f"{path}[{i}]") for i, v in enumerate(obj)]
    return obj


def _missing_musts(name: str, musts: list[str], seen: set[str], label: str) -> list[str]:
    """`musts` 경로(접두 일치) 중 실제로 `seen` 에 없는 것의 위반 문구."""
    return [f"{name}: {label} — {must}" for must in musts
            if not any(path == must or path.startswith(must) for path in seen)]


def check_record(name: str, golden: dict, actual: dict, *, done: tuple[str, ...] = ALL_DONE,
                  stages: Mapping[str, StageDiff] = ALL_STAGES) -> list[str]:
    """시나리오 하나 — 골든 대 지금 기록의 위반 목록(없으면 빈 리스트 = 통과).

    `done`·`stages` 는 단위 테스트가 가짜 단계 정의로 메커니즘만 검증할 때 바꿔 끼운다.
    """
    active = _active_stages(done, stages)
    vanish_pats = [re.compile(p) for stage in active for p in stage.vanish_patterns]
    change_pats = [re.compile(p) for stage in active for p in stage.change_patterns]
    musts = [path for stage in active for path in stage.must_vanish.get(name, ())]
    must_changes = [path for stage in active for path in stage.must_change.get(name, ())]
    query_delta = sum(stage.query_delta.get(name, 0) for stage in active)
    query_added = tuple(query for stage in active for query in stage.query_added.get(name, ()))
    query_reorder = any(name in stage.query_reorder for stage in active)
    key_renames = {old: new for stage in active for old, new in stage.key_renames.items()}
    key_renames_skip = tuple(re.compile(p) for stage in active for p in stage.key_renames_skip)

    violations: list[str] = []
    seen: dict[str, set[str]] = {"vanished": set(), "changed": set()}
    golden_cmp = _inline_l3_objects(golden)
    if key_renames:
        golden_cmp = _rename_dict_keys(golden_cmp, key_renames, key_renames_skip)
    for line in diff_paths(golden_cmp, _inline_l3_objects(actual)):
        kind, value = _classify_line(name, line, vanish_pats, change_pats)
        if kind == "violation":
            violations.append(value)
        elif kind != "skip":
            seen[kind].add(value)

    violations.extend(_missing_musts(name, musts, seen["vanished"], "기대한 소멸이 없다(must-vanish)"))
    violations.extend(_missing_musts(name, must_changes, seen["changed"], "기대한 변경이 없다(must-change)"))
    golden_q, actual_q = ((_sorted_queries(golden["queries"]), _sorted_queries(actual["queries"])) if query_reorder
                          else (golden["queries"], actual["queries"]))
    violations.extend(_check_queries(name, golden_q, actual_q, query_delta, query_added))
    return violations
