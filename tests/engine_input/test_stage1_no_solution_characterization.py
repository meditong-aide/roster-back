"""dev `1bedd4d` '엔진 1단계 해 없음' 분기 특성화 — 설계서 §7.4 · 재수신 흡수 `4ec454e` 리뷰 지적.

왜 필요한가
  골든(`golden/`)은 이 분기를 못 본다. 하네스 `HarnessRosterSystem` 에 표시
  (`roster_system._fallback_stage1_no_solution`)가 없어 `getattr(…, None)` 이 늘 None 이고,
  표시를 되돌리는 자리(`cp_sat_basic.py` 의 `SKIP_PRIMARY` 둘째 읽기)는 풀이가 있어야 닿는다.
  그래서 재수신 흡수가 `_validate_generated_roster` 래퍼를 잘못 이어 붙여도(2026-10-20
  `grade_config_context` → `NameError`) 골든·untag·A1 은 초록이었고 dev 테스트 4개만 우연히 잡았다.
  EI-11·EI-13a 가 `roster_create_service.py` 를 크게 고치기 전에 이 분기를 영구히 지켜 본다.

기대값의 출처 — 이 브랜치 출력으로 덮어쓰지 않는다
  아래 기대값은 전부 **순수 dev `4ec454e`**(`git archive`)에서 같은 코드로 실측한 값이다.
  이 파일 자체가 `redev_4ec454e/pure_harness`(순수 dev + 하네스)에서도 그대로 통과한다.
  이 브랜치에서 다르게 나오면 EI 가 dev 동작을 바꾼 것이니 고치기 전에 원인을 본다.
  새 dev 커밋이 문구·분기를 바꾼 재수신이면, 순수 dev 에서 다시 실측해 기대값을 갱신한다.

다루는 것
  A. `_validate_generated_roster` 래퍼 — 표시 3종(None·'unknown'·'infeasible') × 생성물 3종(정상·전원 OFF·None):
     문구 · `_infeasible_empty` · `_ontology_last_reason` · '코어 먼저' 우선순위 · 코어와 같은 매개변수 ·
     받은 인자를 코어에 그대로 넘김.
  B. 생성 서비스 전체 흐름 — 엔진 대역이 표시를 세운 결과를 돌려줄 때 본선(TimeoutError)·완화 사다리가
     어디로 가는지. 시나리오 S1-L3 에 결과 표시만 얹는다(기록기 `ladder.py` 는 건드리지 않는다 — 골든 무변경).

못 다루는 것
  - `SKIP_PRIMARY=0` 일 때 표시를 None 으로 되돌리는 자리 — 실제 풀이가 필요하다(solve 0 원칙).
  - dev `4ec454e` 의 설정 조회 라우터(NO_LINKED_CONFIG·LINKED_CONFIG_MISSING) — 라우터는 이 브랜치가 고치지 않는다.
"""

from __future__ import annotations

import dataclasses
import inspect
from types import SimpleNamespace

import pytest

from . import ladder
from .backend_check import assert_sqlite_backends
from .harness import run_with_output
from .ladder import drain_live_graph_dirs
from .scenario_ladder import LADDER_SCENARIOS

# ── 순수 dev 4ec454e 실측 문구 ─────────────────────────────────────────────
MSG_UNKNOWN = "[reason_code=STAGE1_UNKNOWN] 엔진 1단계가 제한 시간 안에 해를 찾지 못했습니다(해 없음 미증명)."
MSG_INFEASIBLE = (
    "[reason_code=NO_ASSIGNMENT] 엔진 배정 0건(1단계 해 없음 — 고정 칸만 남음 · 증상). 원인은 구조진단에서 결정. "
    "evidence={'total_failed_cells': None, 'required_minus_assigned_total': None, 'fixed_forbidden_count': None}"
)
MSG_EMPTY = (
    "[reason_code=NO_ASSIGNMENT] 실근무 배정 0건(증상). 원인은 구조진단에서 결정. "
    "evidence={'total_failed_cells': None, 'required_minus_assigned_total': None, 'eligible_zero_cells': None, "
    "'fixed_forbidden_count': None, 'carryover_artifact_count': None}"
)
GENERATED = {
    "ok": {"N1": ["D", "D", "O"], "N2": ["E", "O", "D"]},
    "empty": {"N1": ["O", "O", "O"], "N2": ["O", "O", "O"]},
    "none": None,
}
#: (생성물, 표시) → (검증 문구, `_infeasible_empty`, `_ontology_last_reason` 의 reason_code).
#: 전원 OFF 는 코어가 먼저 잡아 표시를 보지 않는다(코어 문구 · 표시는 그대로 남는다).
WRAPPER_MATRIX = {
    ("ok", None): (None, None, None),
    ("ok", "unknown"): (MSG_UNKNOWN, None, "STAGE1_UNKNOWN"),
    ("ok", "infeasible"): (MSG_INFEASIBLE, True, "NO_ASSIGNMENT"),
    ("empty", None): (MSG_EMPTY, True, "NO_ASSIGNMENT"),
    ("empty", "unknown"): (MSG_EMPTY, True, "NO_ASSIGNMENT"),
    ("empty", "infeasible"): (MSG_EMPTY, True, "NO_ASSIGNMENT"),
    ("none", None): (None, None, None),
    ("none", "unknown"): (MSG_UNKNOWN, None, "STAGE1_UNKNOWN"),
    ("none", "infeasible"): (MSG_INFEASIBLE, True, "NO_ASSIGNMENT"),
}


def _rcs():
    """생성 서비스 모듈(수집 시점 import 는 외부 접촉 가드 밖이라 테스트 안에서 부른다)."""
    import services.roster_create_service as rcs

    return rcs


# ── A. 검증 래퍼 ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("gen_kind", "flag"), list(WRAPPER_MATRIX))
def test_wrapper_matrix(gen_kind, flag):
    """표시 × 생성물 9조합의 문구 · 완화 사다리 신호 · 온톨로지 사유가 순수 dev 와 같다."""
    message, infeasible_empty, reason_code = WRAPPER_MATRIX[(gen_kind, flag)]
    roster_system = SimpleNamespace(config=None, num_days=0, _fallback_stage1_no_solution=flag)
    got = _rcs()._validate_generated_roster(GENERATED[gen_kind], roster_system, nurses_context=[], config_context={})
    assert got == message
    assert getattr(roster_system, "_infeasible_empty", None) == infeasible_empty
    assert roster_system._fallback_stage1_no_solution == flag, "래퍼는 엔진 표시를 지우지 않는다"
    reason = getattr(roster_system, "_ontology_last_reason", None)
    if reason_code is None:
        assert reason is None
    else:
        assert (reason["reason_code"], reason["severity"], reason["message"]) == (reason_code, "hard", message)


def test_wrapper_takes_the_same_parameters_as_core():
    """래퍼는 코어의 대체품이다 — 매개변수 이름·종류·기본값이 같다(한쪽만 바뀌면 호출부 10곳이 깨진다)."""
    rcs = _rcs()

    def shape(func):
        return [(p.name, p.kind, p.default) for p in inspect.signature(func).parameters.values()]

    assert shape(rcs._validate_generated_roster) == shape(rcs._validate_generated_roster_core)


def test_wrapper_forwards_every_argument_to_core(monkeypatch):
    """래퍼는 받은 인자를 이름 그대로 코어에 넘긴다(코어 결과가 있으면 그 결과를 그대로 돌려준다)."""
    rcs = _rcs()
    seen: dict = {}

    def core_spy(generated, roster_system, **kwargs):
        seen.update(generated=generated, roster_system=roster_system, **kwargs)
        return "코어 결과"

    monkeypatch.setattr(rcs, "_validate_generated_roster_core", core_spy)
    generated, system, nurses, config = {"N1": ["D"]}, object(), [object()], {"k": 1}
    got = rcs._validate_generated_roster(generated, system, nurses_context=nurses, config_context=config)
    assert got == "코어 결과"
    assert seen["generated"] is generated
    assert seen["roster_system"] is system
    assert seen["nurses_context"] is nurses
    assert seen["config_context"] is config


# ── B. 생성 서비스 전체 흐름 ─────────────────────────────────────────────────


def _marker_patch(first_kind: str | None, rest_kind: str | None):
    """엔진 대역의 첫 결과에는 first_kind, 그 뒤 결과에는 rest_kind 표시를 세운다(None 이면 세우지 않는다).

    `ladder.py` 의 결과 대역을 감쌀 뿐이다 — 기록기·골든은 그대로다. 시나리오 실행 동안만 걸린다.
    """

    def patch(mp: pytest.MonkeyPatch) -> None:
        calls = {"n": 0}

        def wrap(original):
            def result(bound: dict) -> dict:
                res = original(bound)
                kind = first_kind if calls["n"] == 0 else rest_kind
                calls["n"] += 1
                if kind is not None:
                    res["roster_system"]._fallback_stage1_no_solution = kind
                return res

            return result

        mp.setattr(ladder, "success_result", wrap(ladder.success_result))
        mp.setattr(ladder, "failure_result", wrap(ladder.failure_result))

    return patch


#: 변형 이름 → (기반 시나리오, 첫 엔진 결과 표시, 그 뒤 엔진 결과 표시). L3 계열만 둔다 —
#: S1-L 계열 전 시도 변형은 탐색이 460여 회라 느리다.
VARIANTS = {
    "infeasible-first": ("S1-L3", "infeasible", None),
    "unknown-first": ("S1-L3", "unknown", None),
    "infeasible-then-unknown": ("S1-L3", "infeasible", "unknown"),
}
#: 변형 → (종착 outcome, path, error, 엔진을 부른 지점 순서). 순수 dev 4ec454e 실측.
EXPECTED_FLOW = {
    # 첫 결과 '해 없음 증명' → 완화 사다리 1단계(team_min_soft)가 표시 없는 결과로 성공 → 후처리까지 정상 반환.
    "infeasible-first": ("returned", "post_complete", None, ("first_attempt", "team_min_soft")),
    # 첫 결과 '시간 초과' → 본선이 일시 실패(TimeoutError)로 올린다(완화 사다리·확정 실패로 가지 않는다).
    "unknown-first": ("exception", "error", f"TimeoutError: {MSG_UNKNOWN} 일시 실패로 다시 시도합니다.",
                      ("first_attempt",)),
    # 완화 재시도 결과가 '시간 초과'면 그 시도만 실패 — 사다리가 이어지다 구조진단의 풀이(하네스가 막음)에서 멈춘다.
    "infeasible-then-unknown": ("solve_blocked", "error", "하네스는 CpSolver.solve 를 돌리지 않는다",
                                ("first_attempt", "team_min_soft", "same_shift_soft", "isolated_work_soft")),
}


@pytest.fixture(scope="module")
def marker_runs(engine, engine_input_tables, guard_log):
    """변형 3개를 한 번씩 돌린다(첫 줄이 프로덕션 실행 앞 backend 확인)."""
    assert_sqlite_backends(engine)
    before = len(guard_log)
    runs = {}
    for name, (base_name, first_kind, rest_kind) in VARIANTS.items():
        base = LADDER_SCENARIOS[base_name]
        scenario = dataclasses.replace(base, name=name, patches=(*base.patches, _marker_patch(first_kind, rest_kind)))
        runs[name] = run_with_output(engine, scenario)
    drain_live_graph_dirs()
    return {"runs": runs, "blocked": list(guard_log[before:])}


@pytest.mark.parametrize("name", list(EXPECTED_FLOW))
def test_flow_endpoint_and_attempt_order(name, marker_runs):
    """표시를 세운 엔진 결과가 본선·사다리에서 가는 곳(종착 · 오류 · 엔진 호출 지점 순서)이 순수 dev 와 같다."""
    result = marker_runs["runs"][name].result
    sites = tuple(attempt["site"] for attempt in result.layers["attempts"])
    assert (result.outcome, result.path, result.error, sites) == EXPECTED_FLOW[name]


def test_flow_safety(marker_runs):
    """풀이 0(하네스가 막은 1회 제외) · 별도 세션 0 · L1 밖 L2 0 · 외부 접촉 0."""
    assert marker_runs["blocked"] == []
    for name, run in marker_runs["runs"].items():
        result = run.result
        assert result.separate_sessions == 0, name
        assert result.l2_calls_outside_l1 == 0, name
        assert result.solve_calls == (1 if result.outcome == "solve_blocked" else 0), name


def test_unknown_first_is_reported_as_a_transient_failure(marker_runs):
    """시간 초과 표시는 '미분류 실패'로 기록되고(워커가 일시 장애로 재시도) 확정 실패 신호는 남지 않는다."""
    stdout = marker_runs["runs"]["unknown-first"].stdout
    assert "[RosterGenerate] 미분류 실패 — TimeoutError: [reason_code=STAGE1_UNKNOWN]" in stdout
    assert "[TeamMinFallback]" not in stdout


def test_unknown_during_relaxation_only_fails_that_attempt(marker_runs):
    """완화 재시도 중의 시간 초과 표시는 그 시도의 실패일 뿐 TimeoutError 로 올라가지 않는다."""
    stdout = marker_runs["runs"]["infeasible-then-unknown"].stdout
    for tag in ("TeamMinFallback", "SameShiftFallback", "IsolatedWorkFallback"):
        assert f"[{tag}][AUTO-SOFT][fail] 재시도 실패: {MSG_UNKNOWN}" in stdout
    assert "TimeoutError" not in stdout
