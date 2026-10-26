"""EI-2 "키 소멸만 다름" 단언 장치(`expected_diff.py`) 검증.

둘로 나뉜다.
  1) 실측 검증 — 15개 시나리오를 한 번씩 돌린 '지금' 기록을 동결해 둔 `golden_base/dev4ec454e/`
     와 `check_record(..., done=EI2_DONE)`로 대조한다. `EI2_DONE=()`(아직 아무 EI-2 하위
     단계도 구현 안 함)일 때는 완전히 같아야 하고(장치 자체의 자기 검증 — 작업 지시문 3번
     "EI2_DONE=() 상태에서 이 테스트는 '차이 0'이어야 한다"), 단계가 추가되면(예:
     `EI2_DONE=("2a",)`) 그 단계가 허용한 키만 사라졌는지를 같은 함수로 본다 — `check_record`
     의 기본 `done` 인자가 모듈의 `EI2_DONE` 을 그대로 쓰므로 테스트 쪽은 바뀌지 않는다.
     ★ 2026-10-13(dev `55dacd4` 재수신) — 기준을 `golden_base/ei1b`(49f14bd 시절 동결본)에서
     `golden_base/dev55dacd4`(pure_dev 55dacd4 + 최신 하네스, EI 적용 전 동결본)로 바꿨다. dev
     가 `roster_create_service.py` 에 사다리 새 단계(`ban_night_minimize`)·`gen_deadline` 등을
     더해 EI-2 가 아직 안 건드린 "원형" 자체가 달라졌기 때문이다 — ei1b 대조로는 EI-2 가 지운
     것과 dev 가 더한 것이 섞여 "허용 밖 변화"로 잘못 걸린다. `golden_base/ei1b` 는 2026-10-20(D29)
     저장소 밖 `.omc/aide_docs/v2/engine_input/golden_store/retired_from_repo_20261020/` 로 옮겼다(이전
     EI-1b 단계의 기록 — `golden_base/README.md` 참고).
     ★ 2026-10-14(dev `3ede458` 재수신) — 기준 폴더를 `golden_base/dev3ede458` 로 바꿨다. dev 의
     `off_swap.py` 변경(3f63c2c — 주2OFF 병동은 연차 전환 대신 O 유지)으로 S1-L3 후처리 입력만
     바뀌었고, 순수 dev `3ede458` 로 다시 뜬 16개 중 나머지 15개는 옛 `dev55dacd4` 와 바이트까지 같았다.
     ★ 2026-10-20(dev `4ec454e` 재수신) — 기준 폴더를 `golden_base/dev4ec454e` 로 바꿨다. dev `863e0f2` 가
     `_fetch_grade_config_dict` 에 더한 새 키 `configured_constraints_json`(L0·L1 `grade_config` 와 S1-LT 시도 델타 5곳)이
     '지금 기록'에만 있어, 옛 기준으로는 '새로 생김'이 허용 밖 변화로 걸린다. 순수 dev `4ec454e` 로 다시 뜬 16개 중
     `dev3ede458` 판과 다른 곳은 이 키 35곳과 digest 16곳뿐이다(`redev_4ec454e/STATUS_redev_4ec454e.md`).
  2) 메커니즘 단위 테스트 — 가짜 dict·가짜 StageDiff 로 `check_record` 가 네 가지 위반을
     각각 잡는지, 그리고 올바른 경우엔 통과시키는지 확인한다. 실제 골든을 다시 돌리지 않는다.
D29(2026-10-20) — `golden_base/` 의 JSON 은 커밋하지 않는 로컬 전용이다. `_read_base` 는 첫 줄에서 문지기
  (`golden_gate.require_golden(GOLDEN_BASE)`)를 거친다 — 기준 폴더가 비었으면 1) 이 D29 사유로 건너뛰고
  (`REQUIRE_GOLDEN=1` 이면 실패), 일부만 있으면 실패한다. 2) 는 골든을 읽지 않아 그대로 돈다.
"""

from __future__ import annotations

import json

import pytest

from .backend_check import assert_sqlite_backends
from .expected_diff import StageDiff, check_record
from .golden import build_record
from .golden_gate import GOLDEN_BASE, require_golden
from .harness import run_with_output
from .ladder import drain_live_graph_dirs
from .scenario_defs import SCENARIOS
from .scenario_ladder import LADDER_SCENARIOS

#: 기준 폴더의 정본은 `golden_gate.GOLDEN_BASE`(폴더 이름·개수 — 재수신 때 고치는 자리) 다. 이 이름은 별칭이다(D29).
GOLDEN_BASE_DIR = GOLDEN_BASE.path
#: 15개 골든 전부 — test_scenarios 의 12 + test_ladder 의 3(겹치는 이름 없음).
ALL_SCENARIO_OBJECTS = {**SCENARIOS, **LADDER_SCENARIOS}


def _read_base(name: str) -> dict:
    """동결해 둔 dev `4ec454e`(EI 적용 전) 시점 골든 사본(이 테스트 파일이 비교 기준으로 쓴다 —
    원본 `golden/` 은 건드리지 않는다)."""
    require_golden(GOLDEN_BASE)
    path = GOLDEN_BASE_DIR / f"{name}.json"
    assert path.exists(), f"golden_base 사본 없음: {path} — EI-2 준비 1번(동결)이 먼저다"
    return json.loads(path.read_text(encoding="utf-8"))


# ── 1) 실측 검증 — 지금 기록 대 golden_base, 허용은 EI2_DONE 활성 단계만큼 ──────────


@pytest.fixture(scope="module")
def current_records(engine, engine_input_tables, guard_log):
    """15개 시나리오를 한 번씩 돌린 '지금' 기록(골든과 같은 방식 — `build_record`).

    첫 줄 backend 확인은 `scenario_runs`(test_scenarios.py)·`ladder_runs`(test_ladder.py)와
    같은 자리다(conftest.py 의 "(2) 모듈 범위 실행 픽스처 첫 줄" 규칙).
    """
    assert_sqlite_backends(engine)
    before = len(guard_log)
    records = {name: build_record(scenario, run_with_output(engine, scenario))
               for name, scenario in ALL_SCENARIO_OBJECTS.items()}
    blocked = list(guard_log[before:])
    # 사다리 시나리오(S1-L·S1-LT·S1-L3)는 live graph 임시 폴더를 전역 목록(ladder.LIVE_GRAPH_DIRS)에
    # 남긴다. test_ladder.py 가 같은 전역 목록을 세어 '흔적 0'을 확인하므로(test_no_trace), 여기서도
    # 드레인해 비워 둔다 — 안 그러면 파일 수집 순서(test_expected_diff.py 가 test_ladder.py 보다
    # 알파벳상 앞)에 따라 그쪽 단언이 깨진다.
    drain_live_graph_dirs()
    assert blocked == [], blocked  # 외부 접촉 시도 0(설계서 §5.3 '흔적 0'과 같은 기준)
    return records


@pytest.mark.parametrize("name", list(ALL_SCENARIO_OBJECTS))
def test_ei2_done_matches_frozen_base_within_allowance(name, current_records):
    """지금 기록 대 `golden_base/dev4ec454e` — 차이는 `EI2_DONE` 활성 단계가 허용한 것만이어야 한다.

    `EI2_DONE=()` 이면 차이가 0 이어야 한다(장치 자기 검증). 단계가 추가되면(예:
    `EI2_DONE=("2a",)`) 그 단계의 `vanish_patterns`·`must_vanish`·`query_delta` 밖의
    변화가 있으면 잡는다 — 값이 바뀌거나, 허용 밖 키가 사라지거나, 지우기로 한 키가
    조용히 안 지워지거나, 쿼리 수 변화가 기대와 다르면 전부 위반이다.
    """
    violations = check_record(name, _read_base(name), current_records[name])
    assert violations == [], "\n".join(violations)


# ── 2) 메커니즘 단위 테스트(가짜 dict) ──────────────────────────────────────


def _record(config_data: dict, *, queries_count: int = 10,
            queries_tables: tuple[str, ...] = ("select:a", "select:b", "select:c", "select:d")) -> dict:
    """작은 가짜 기록 — `layers.L1[0].config_data` 안에 원하는 키만 넣는다."""
    return {
        "scenario": "FAKE", "description": "", "outcome": "golden_stop", "path": "first_attempt",
        "error": None, "http_detail": None, "digest": "fake-digest",
        "safety": {"solve_calls": 0, "separate_sessions": 0, "l2_calls_outside_l1": 0},
        "queries": {"path": "first_attempt", "count": queries_count, "tables": list(queries_tables),
                    "marks": {}, "harness_statements": 0},
        "swallowed_failures": [], "logs": [],
        "layers": {"L1": [{"config_data": dict(config_data)}]},
    }


#: 가짜 단계 — `dead_key` 가 사라지는 것만 허용하고, 쿼리는 2건 줄어드는 것을 기대한다.
FAKE_STAGE = StageDiff(
    vanish_patterns=(r"^\$\.layers\.L1\[0\]\.config_data\.dead_key$",),
    must_vanish={"FAKE": ("$.layers.L1[0].config_data.dead_key",)},
    query_delta={"FAKE": 2},
)
FAKE_STAGES = {"x": FAKE_STAGE}
FAKE_DONE = ("x",)


def test_mechanism_passes_when_only_allowed_key_vanishes():
    """허용한 그대로(죽은 키 소멸 + 쿼리 2건 감소 + 부분열 유지) — 위반 0."""
    golden = _record({"dead_key": 1, "alive_key": 1}, queries_count=10)
    actual = _record({"alive_key": 1}, queries_count=8,
                      queries_tables=("select:a", "select:c", "select:d"))
    assert check_record("FAKE", golden, actual, done=FAKE_DONE, stages=FAKE_STAGES) == []


def test_mechanism_catches_vanish_outside_allowlist():
    """허용 목록 밖의 키가 사라지면 잡는다."""
    golden = _record({"dead_key": 1, "other_dead_key": 1})
    actual = _record({})  # dead_key 는 허용, other_dead_key 는 허용 밖인데 같이 사라짐
    violations = check_record("FAKE", golden, actual, done=FAKE_DONE, stages=FAKE_STAGES)
    assert any("허용 밖 소멸" in v and "other_dead_key" in v for v in violations), violations


def test_mechanism_catches_value_change():
    """키가 사라진 게 아니라 값이 바뀌면(허용 패턴에 맞는 경로라도) 잡는다."""
    golden = _record({"dead_key": 1})
    actual = _record({"dead_key": 2})  # 소멸이 아니라 값 변경
    violations = check_record("FAKE", golden, actual, done=FAKE_DONE, stages=FAKE_STAGES)
    assert any("사라짐이 아니다" in v for v in violations), violations


def test_mechanism_catches_new_key():
    """새 키가 생기면(EI-2 는 "불변(값)·키 소멸만" 이라 새 키는 범위 밖) 잡는다."""
    golden = _record({"dead_key": 1})
    actual = _record({"dead_key": 1, "surprise_key": 1})
    violations = check_record("FAKE", golden, actual, done=FAKE_DONE, stages=FAKE_STAGES)
    assert any("사라짐이 아니다" in v and "surprise_key" in v for v in violations), violations


def test_mechanism_catches_must_vanish_not_happening():
    """지우기로 한 키가 실제로는 그대로 남아 있으면(회귀) must-vanish 가 잡는다."""
    golden = _record({"dead_key": 1})
    actual = _record({"dead_key": 1})  # 아무것도 안 지워짐
    violations = check_record("FAKE", golden, actual, done=FAKE_DONE, stages=FAKE_STAGES)
    assert any("must-vanish" in v for v in violations), violations


def test_mechanism_catches_query_count_mismatch():
    """쿼리 수 감소가 기대(2건)와 다르면 잡는다."""
    golden = _record({"dead_key": 1}, queries_count=10)
    actual = _record({}, queries_count=9)  # 1건만 줄었다(기대 2건)
    violations = check_record("FAKE", golden, actual, done=FAKE_DONE, stages=FAKE_STAGES)
    assert any("쿼리 수 변화" in v for v in violations), violations


def test_mechanism_catches_query_subsequence_violation():
    """쿼리 수는 기대대로 줄어도, 남은 순서가 이전 순서의 부분열이 아니면 잡는다."""
    golden = _record({"dead_key": 1}, queries_count=10,
                      queries_tables=("select:a", "select:b", "select:c", "select:d"))
    # 수는 2 감소(기대와 일치)하지만 c·b 순서가 뒤집혔다 — 부분열 위반.
    actual = _record({}, queries_count=8, queries_tables=("select:a", "select:c", "select:b"))
    violations = check_record("FAKE", golden, actual, done=FAKE_DONE, stages=FAKE_STAGES)
    assert any("부분열" in v for v in violations), violations


#: 가짜 단계 — 쿼리를 하나 더한다(EI-12 12u2 꼴 · `query_added` + 음수 `query_delta`).
FAKE_ADD_STAGES = {"z": StageDiff(query_delta={"FAKE": -1}, query_added={"FAKE": ("select:x",)})}


def test_mechanism_allows_declared_query_addition():
    """단계가 적은 쿼리가 끼어도 그것을 빼면 이전 순서열의 부분열이고 수가 기대대로면 위반 0."""
    golden = _record({}, queries_count=10)
    actual = _record({}, queries_count=11, queries_tables=("select:a", "select:b", "select:x", "select:c", "select:d"))
    assert check_record("FAKE", golden, actual, done=("z",), stages=FAKE_ADD_STAGES) == []


def test_mechanism_catches_query_addition_outside_declaration():
    """적지 않은 쿼리가 끼거나 · 적은 쿼리를 빼도 순서가 바뀌었거나 · 단계가 꺼져 있으면 부분열 위반이다."""
    golden = _record({}, queries_count=10)
    other = _record({}, queries_count=11, queries_tables=("select:a", "select:b", "select:y", "select:c", "select:d"))
    swapped = _record({}, queries_count=11, queries_tables=("select:a", "select:x", "select:c", "select:b", "select:d"))
    added = _record({}, queries_count=11, queries_tables=("select:a", "select:b", "select:x", "select:c", "select:d"))
    for actual, done in ((other, ("z",)), (swapped, ("z",)), (added, ())):
        violations = check_record("FAKE", golden, actual, done=done, stages=FAKE_ADD_STAGES)
        assert any("부분열" in v for v in violations), (actual["queries"]["tables"], done, violations)


#: 가짜 단계 — 쿼리 수는 같고 순서만 바뀐다(EI-10g `query_reorder`), 하나는 쿼리도 더한다.
FAKE_REORDER_STAGES = {
    "r": StageDiff(query_reorder=("FAKE",)),
    "ra": StageDiff(query_reorder=("FAKE",), query_delta={"FAKE": -1}, query_added={"FAKE": ("select:x",)}),
}
_ABCD = ("select:a", "select:b", "select:c", "select:d")


def test_mechanism_query_reorder_allows_same_multiset():
    """`query_reorder` 시나리오는 순서가 달라도 다중집합이 같으면 위반 0 이다(없으면 부분열 위반)."""
    golden = _record({}, queries_tables=_ABCD)
    shuffled = _record({}, queries_tables=("select:c", "select:a", "select:d", "select:b"))
    assert check_record("FAKE", golden, shuffled, done=("r",), stages=FAKE_REORDER_STAGES) == []
    assert any("부분열" in v for v in check_record("FAKE", golden, shuffled, done=(), stages=FAKE_REORDER_STAGES))


def test_mechanism_query_reorder_catches_missing_or_extra_query():
    """순서만 바뀌는 단계에서 쿼리 하나가 빠지거나 더해지거나 종류가 바뀌면 잡는다."""
    golden = _record({}, queries_tables=_ABCD)
    fewer = _record({}, queries_count=9, queries_tables=("select:c", "select:a", "select:b"))
    extra = _record({}, queries_count=11, queries_tables=("select:c", "select:a", "select:d", "select:b", "select:a"))
    swapped_kind = _record({}, queries_tables=("select:c", "select:a", "select:d", "select:y"))
    for actual in (fewer, extra, swapped_kind):
        assert check_record("FAKE", golden, actual, done=("r",), stages=FAKE_REORDER_STAGES), actual["queries"]["tables"]


def test_mechanism_query_reorder_with_query_added_excludes_only_added():
    """`query_added` 와 함께 쓰면 더한 쿼리만 빼고 다중집합을 본다 — 더한 쿼리가 없으면 위반."""
    golden = _record({}, queries_tables=_ABCD)
    ok = _record({}, queries_count=11, queries_tables=("select:c", "select:x", "select:a", "select:d", "select:b"))
    no_x = _record({}, queries_count=11, queries_tables=("select:c", "select:a", "select:d", "select:b", "select:a"))
    assert check_record("FAKE", golden, ok, done=("ra",), stages=FAKE_REORDER_STAGES) == []
    assert check_record("FAKE", golden, no_x, done=("ra",), stages=FAKE_REORDER_STAGES)


def test_ei12_12u2_allows_only_one_slot_query_in_s6():
    """12u2 는 S6 에 상대 병동 근무 슬롯 조회 1건만 더한다 — 값 변화·사라짐 허용 0(허용을 넓히면 골든이 조용히 바뀌어도 통과한다)."""
    from .expected_diff import EI12_STAGES

    assert set(EI12_STAGES) == {"12u2"}
    assert EI12_STAGES["12u2"] == StageDiff(query_delta={"S6": -1}, query_added={"S6": ("select:shift_manage",)})


def test_mechanism_ignores_digest_and_queries_in_vanish_only_rule():
    """digest·queries 자체의 값 변경은 (a) "사라짐만" 규칙에 안 걸린다(별도 규칙 (c)로만 본다).

    활성 단계 없이(done=()) 본다 — must-vanish·query_delta 는 (c)의 몫이라 여기서는 끈다.
    """
    golden = _record({}, queries_count=10)
    golden["digest"] = "old-digest"
    actual = _record({}, queries_count=10)
    actual["digest"] = "new-digest"  # digest 만 바뀜 — (a) 규칙에서는 무시해야 한다
    violations = check_record("FAKE", golden, actual, done=(), stages=FAKE_STAGES)
    assert violations == [], violations


def _l3_record(latest_config: dict, *, sha: str) -> dict:
    """L3 를 가진 가짜 기록 — latest_config 를 내용 주소(objects[sha])로 두고 호출은 참조만 갖는다."""
    record = _record({}, queries_count=10)
    record["layers"]["L3"] = {
        "calls": [{"func": "postprocess_sleep_off", "args": {"latest_config": {"__object__": sha}}}],
        "objects": {sha: dict(latest_config)},
    }
    return record


#: 가짜 단계 — 후처리 입력 latest_config 의 `dead_key` 소멸만 허용한다.
FAKE_L3_STAGES = {"x": StageDiff(
    vanish_patterns=(r"^\$\.layers\.L3\.calls\[\d+\]\.args\.latest_config\.dead_key$",),
    must_vanish={"FAKE": ("$.layers.L3.calls[0].args.latest_config.dead_key",)},
)}


def test_mechanism_inlines_l3_refs_so_key_vanish_is_seen_per_key():
    """L3 내용 주소가 바뀌어도(키 소멸 → 해시 변화) 참조를 풀어 키 단위 소멸로 본다 — 위반 0."""
    golden = _l3_record({"dead_key": 1, "alive_key": 1}, sha="aaaa")
    actual = _l3_record({"alive_key": 1}, sha="bbbb")
    assert check_record("FAKE", golden, actual, done=("x",), stages=FAKE_L3_STAGES) == []


def test_mechanism_catches_l3_value_change_behind_hash():
    """해시 뒤에 숨은 latest_config 값 변경을 잡는다(통째 예외를 두지 않는다 — 10-09 리뷰 지적)."""
    golden = _l3_record({"dead_key": 1, "alive_key": 1}, sha="aaaa")
    actual = _l3_record({"alive_key": 2}, sha="bbbb")  # dead_key 소멸은 허용, alive_key 값 변경은 아님
    violations = check_record("FAKE", golden, actual, done=("x",), stages=FAKE_L3_STAGES)
    assert any("사라짐이 아니다" in v and "alive_key" in v for v in violations), violations


def test_mechanism_catches_l3_change_without_active_stage():
    """활성 단계가 없으면(done=()) L3 내용 변화는 전부 위반이다 — 해시 churn 도 그냥 넘기지 않는다."""
    golden = _l3_record({"dead_key": 1}, sha="aaaa")
    actual = _l3_record({}, sha="bbbb")
    violations = check_record("FAKE", golden, actual, done=(), stages=FAKE_L3_STAGES)
    assert any("허용 밖 소멸" in v and "dead_key" in v for v in violations), violations


def test_mechanism_attempt_queries_before_exception_is_narrow():
    """(d) 의 시도별 쿼리 위치 예외는 `attempts[i].queries_before` 만 — 시도 기록의 다른 칸 변화는 잡는다."""
    golden = _record({}, queries_count=10)
    golden["layers"]["attempts"] = [{"queries_before": 5, "site": "first"}]
    moved = _record({}, queries_count=10)
    moved["layers"]["attempts"] = [{"queries_before": 4, "site": "first"}]
    assert check_record("FAKE", golden, moved, done=(), stages=FAKE_STAGES) == []
    other = _record({}, queries_count=10)
    other["layers"]["attempts"] = [{"queries_before": 5, "site": "team_min"}]
    violations = check_record("FAKE", golden, other, done=(), stages=FAKE_STAGES)
    assert any("site" in v for v in violations), violations


def test_ei2_stages_cover_all_declared_substages():
    """EI2_STAGES 키가 설계서 §5.3 EI-2 하위 단계 네 개(2a·2b·2c·2d) 그대로다."""
    from .expected_diff import EI2_STAGES

    assert set(EI2_STAGES) == {"2a", "2b", "2c", "2d"}


# ── 3) EI-10 확장(change_patterns·must_change) — 가짜 단계 ───────────────────────────


#: 가짜 EI-10 단계 — `changed_key` 는 값이 바뀌거나 새로 생겨도 되고, 반드시 바뀌어야 한다.
FAKE_CHANGE_STAGES = {"y": StageDiff(
    change_patterns=(r"^\$\.layers\.L1\[0\]\.config_data\.changed_key$",),
    must_change={"FAKE": ("$.layers.L1[0].config_data.changed_key",)},
)}


def test_mechanism_change_allowed_inside_change_patterns():
    """허용한 경로는 값이 바뀌어도, 새로 생겨도 위반이 아니다(EI-10 판정 변경 단계)."""
    golden = _record({"changed_key": 1})
    assert check_record("FAKE", golden, _record({"changed_key": 2}), done=("y",), stages=FAKE_CHANGE_STAGES) == []
    created = check_record("FAKE", _record({}), _record({"changed_key": 2}), done=("y",), stages=FAKE_CHANGE_STAGES)
    assert created == []


def test_mechanism_catches_change_outside_change_patterns():
    """허용 경로 밖의 값 변경은 그대로 위반이다 — 변경 허용이 다른 키까지 새지 않는다."""
    golden = _record({"changed_key": 1, "other_key": 1})
    actual = _record({"changed_key": 2, "other_key": 2})
    violations = check_record("FAKE", golden, actual, done=("y",), stages=FAKE_CHANGE_STAGES)
    assert any("사라짐이 아니다" in v and "other_key" in v for v in violations), violations
    assert not any("changed_key" in v for v in violations), violations


def test_mechanism_catches_must_change_not_happening():
    """바꾸기로 한 경로가 조용히 그대로면(회귀) must-change 가 잡는다."""
    golden = _record({"changed_key": 1})
    violations = check_record("FAKE", golden, _record({"changed_key": 1}), done=("y",), stages=FAKE_CHANGE_STAGES)
    assert any("must-change" in v for v in violations), violations


def test_ei10_stages_registered_and_active_by_default():
    """구현한 EI-10 단계(10c·10d·10e·10f)는 등록돼 있고 기본 활성 단계(`ALL_DONE`)에 들어 있다 — EI-2 4단계 정의는 그대로다.

    EI-10 단계는 '사라짐'이 아니라 '변경'(`change_patterns`)만 허용한다 — 사라짐·쿼리 변화 허용은 0 이다.
    """
    from .expected_diff import (
        ALL_DONE,
        ALL_STAGES,
        EI2_DONE,
        EI10_DONE,
        EI10_STAGES,
        EI11_DONE,
        EI11_STAGES,
        EI12_DONE,
        EI12_STAGES,
        EI13_DONE,
        EI13_STAGES,
        EI13C_DONE,
        EI13C_STAGES,
        EI14C1_DONE,
        EI14C1_STAGES,
        EID_DONE,
        EID_STAGES,
        NM_DONE,
        NM_STAGES,
    )

    assert set(EI10_STAGES) == set(EI10_DONE) == {"10c", "10d", "10e", "10f"}
    # EI-11 단계는 그 단계 프로덕션 커밋마다 EI11_* 에 더해진다(ei11/EI11_SPEC_v2.md §4.3) — 11a0 은 허용 0 이다.
    # EI-13 갈래 A — 13a 는 쿼리 감소(재조회 제거)만 허용한다(13a-1·13b 는 골든 변화 0 이라 등록하지 않는다).
    assert ALL_DONE == (EI2_DONE + EI10_DONE + EI11_DONE + EI13_DONE + EI14C1_DONE + EI12_DONE + EI13C_DONE + EID_DONE
                        + NM_DONE)
    assert set(ALL_STAGES) == ({"2a", "2b", "2c", "2d", "10c", "10d", "10e", "10f"} | set(EI11_STAGES) | set(EI13_STAGES)
                               | set(EI14C1_STAGES) | set(EI12_STAGES) | set(EI13C_STAGES) | set(EID_STAGES) | set(NM_STAGES))
    assert EI11_STAGES["11a0"] == StageDiff()
    for name in EI10_DONE:
        stage = EI10_STAGES[name]
        assert not stage.vanish_patterns and not stage.must_vanish and not stage.query_delta, name


def test_ei10_10c_change_pattern_is_narrow():
    """등록된 10c 허용 정규식은 회복 축 경로만 받는다 — 같은 증거의 다른 칸·엔진 입력 층은 못 받는다.

    허용을 넓히면 골든이 '조용히 바뀌어도 통과'하게 되므로 정규식 자체를 고정한다.
    """
    import re

    from .expected_diff import EI10_STAGES

    pattern = re.compile(EI10_STAGES["10c"].change_patterns[0])
    base = "$.http_detail.infeasibility"
    allowed = [
        f"{base}.causes[2].details.night_capable_nurses[0].cap_by_axis.recovery",
        f"{base}.causes[2].evidence.night_capable_nurses[1].cap_by_axis.recovery",
        f"{base}.preflight_issues[31].evidence.night_capable_nurses[0].cap_by_axis.recovery",
        f"{base}.resolution_narrative.problem_list[2].raw_evidence.night_capable_nurses[0].cap_by_axis.recovery",
    ]
    refused = [
        f"{base}.causes[2].evidence.night_capable_nurses[0].cap_by_axis.config",
        f"{base}.causes[2].evidence.night_capable_nurses[0].night_cap",
        f"{base}.causes[2].evidence.shortage",
        "$.layers.L0[0].config_dict.two_offs_after_two_nig",
        "$.layers.L1[0].config_data.max_conseq_work",
    ]
    assert all(pattern.match(path) for path in allowed)
    assert not any(pattern.match(path) for path in refused)


def test_ei10_10d_change_pattern_is_narrow():
    """등록된 10d 허용 정규식은 precheck 입력(L0) 고정 칸의 다섯 칸 이름만 받는다.

    `day_index`(번역이 일자를 바꾸면 회귀)·칸 통째 사라짐·설정·엔진 입력 층(L0′·L1)의 고정 칸은 못 받는다.
    허용을 넓히면 골든이 '조용히 바뀌어도 통과'하게 되므로 정규식 자체를 고정한다.
    """
    import re

    from .expected_diff import EI10_STAGES

    stage = EI10_STAGES["10d"]
    assert len(stage.change_patterns) == 1
    pattern = re.compile(stage.change_patterns[0])
    allowed = [f"$.layers.L0[0].fixed_cells[{i}].{key}"
               for i in (0, 16) for key in ("nurse_index", "fixed_source", "shift_type", "nurse_id", "shift")]
    refused = [
        "$.layers.L0[0].fixed_cells[0].day_index",
        "$.layers.L0[0].fixed_cells[3]",
        "$.layers.L0[0].config_dict.use_mid",
        "$.layers.L0[0].nurses_dict[0].fixed_shift",
        "$.layers.L0p[0].fixed_cells[0].shift",
        "$.layers.L1[0].config_data.fixed_cells[0].shift",
    ]
    assert all(pattern.match(path) for path in allowed)
    assert not any(pattern.match(path) for path in refused)


def test_ei10_10d_must_change_covers_both_scenarios_with_fixed_cells():
    """10d 가 반드시 바꾸는 경로는 고정 칸이 있는 골든 둘(S5·S8)뿐이다 — 다른 시나리오는 칸이 없어 바뀌지 않는다."""
    from .expected_diff import EI10_STAGES

    assert set(EI10_STAGES["10d"].must_change) == {"S5", "S8"}


def test_ei10_10e_allows_no_golden_change():
    """10e(주휴 — 엔진 맵으로 하드 용량 계산)는 어느 골든 경로도 바꾸지 않는다 — 허용이 0 이다.

    S5 의 N03 0→5 · N04 0→4 는 `build_precheck_input` 안의 값이라 골든 층에 안 나타난다. 허용을 하나라도 넣으면
    골든이 '조용히 바뀌어도 통과'하게 되므로 빈 `StageDiff` 자체를 고정하고, 10e 만 활성일 때 값 변화 · 새 키 ·
    사라짐이 모두 위반인 것도 가짜 기록으로 확인한다.
    """
    from .expected_diff import EI10_STAGES

    assert EI10_STAGES["10e"] == StageDiff()
    stages, done = EI10_STAGES, ("10e",)
    golden = _record({"weekly_off_map": 1, "other_key": 1})
    changed = check_record("S5", golden, _record({"weekly_off_map": 2, "other_key": 1}), done=done, stages=stages)
    created = check_record("S5", golden, _record({"weekly_off_map": 1, "other_key": 1, "new_key": 1}),
                           done=done, stages=stages)
    vanished = check_record("S5", golden, _record({"other_key": 1}), done=done, stages=stages)
    assert changed and created and vanished, "10e 는 어떤 변화도 허용하지 않는다"


def test_ei10_10f_allows_no_golden_change():
    """10f(입퇴사 구간 — 퇴사일 −1 · 엔진과 같은 근무 가능 구간)는 어느 골든 경로도 바꾸지 않는다 — 허용이 0 이다.

    골든 16개의 간호사는 월중 입퇴사자가 없어 구간이 월 전체 그대로다(`test_precheck_resignation_characterization`).
    허용을 하나라도 넣으면 골든이 '조용히 바뀌어도 통과'하게 되므로 빈 `StageDiff` 자체를 고정하고, 10f 만 활성일 때 값 변화 ·
    새 키 · 사라짐이 모두 위반인 것도 가짜 기록으로 확인한다.
    """
    from .expected_diff import EI10_STAGES

    assert EI10_STAGES["10f"] == StageDiff()
    stages, done = EI10_STAGES, ("10f",)
    golden = _record({"leave_day": 1, "other_key": 1})
    changed = check_record("S5", golden, _record({"leave_day": 2, "other_key": 1}), done=done, stages=stages)
    created = check_record("S5", golden, _record({"leave_day": 1, "other_key": 1, "new_key": 1}),
                           done=done, stages=stages)
    vanished = check_record("S5", golden, _record({"other_key": 1}), done=done, stages=stages)
    assert changed and created and vanished, "10f 는 어떤 변화도 허용하지 않는다"


def test_nm_stage_renames_only_config_dicts():
    """NM 단계는 이름표 그대로의 키 바꾸기만 허용하고, 바깥 이름이 맞는 자리(요청 원문·저장 JSON)는 바꾸지 않는다."""
    import re

    from services.engine_contract.names import EXTERNAL_TO_INTERNAL

    from .expected_diff import NM_DONE, NM_STAGES, StageDiff, _rename_dict_keys

    assert NM_DONE == ("nm",)
    stage = NM_STAGES["nm"]
    assert stage.key_renames == dict(EXTERNAL_TO_INTERNAL)
    assert stage == StageDiff(key_renames=stage.key_renames, key_renames_skip=stage.key_renames_skip), "이름 바꾸기 말고는 허용 0"
    skip = tuple(re.compile(p) for p in stage.key_renames_skip)
    record = {"layers": {"L0": [{"config_dict": {"off_first": True}}],
                         "L3": {"calls": [{"args": {"req": {"not_one_night": None}, "latest_config": {"nod_noe": True}}}]}},
              "snapshot": {"config": {"three_seq_nig": True}}}
    out = _rename_dict_keys(record, stage.key_renames, skip)
    assert out["layers"]["L0"][0]["config_dict"] == {"staffing_first": True}
    assert out["layers"]["L3"]["calls"][0]["args"]["req"] == {"not_one_night": None}, "요청 모델 원문은 바깥 이름"
    assert out["layers"]["L3"]["calls"][0]["args"]["latest_config"] == {"avoid_nod_noe_eod": True}
    assert out["snapshot"]["config"] == {"three_seq_nig": True}, "저장 JSON 은 바깥 이름"
