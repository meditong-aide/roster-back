"""사다리 모드·성공 모드 골든 테스트(설계서 §7.1 · EI-1b) — S1-L · S1-LT · S1-LB · S1-L3.

시나리오마다 프로덕션 경로(`generate_roster_service`)를 **두 번** 돌린다. 엔진 입구는 기록기가
대신한다(solve 0 — ladder.py). 기존 골든 12종(test_scenarios.py)과 골든 파일이 겹치지 않는다.
  - 골든 비교 · 2회 동일 · 안전(solve 0 · 별도 세션 0 · 외부 연결 시도 0 · raw 실패 로그 0)
  - 사다리 12곳 중 지난 곳 — :7680·:7707·:7895 세 곳은 따로 단언한다
  - 시도별 변형(§4.6 ⓐⓑⓒ)이 그 시도 인자에만 실린다 · S1-L 과 S1-LT 의 탐색 3곳 config 차이
  - 시도별 env(실행 중 토글 포함) · digest 제외 규칙 · L3 후처리 입력 6종
  - 흔적 0: DB·스키마·env·저장소 `tools/harness/reports/`·감싼 함수가 그대로다
미도달 1곳(S1-L·S1-LT 기준 — S1-LB 는 아래에서 도달을 단언한다):
  - :8204(a1_probe) — 성공 경로 + `req.suggest_fixes` + constraint_impact `hard_violation_count>0`
    이 필요하다. 기록기 roster_system 대역으로는 constraint_impact 분석이 성립하지 않아(:4751 에서
    `enabled=False`) 위반 수가 0 이다. 실제 RosterSystem 없이 그 값을 만드는 것은 대역 범위를 넘는다.

★ 2026-10-13 추가 — :7068(ban_night_minimize_retry, dev `55dacd4` 신규)은 `_ladder_retry` 헬퍼
  본문이다. S1-L·S1-LT 는 `ban_night_before_fixed_wanted_off` 가 꺼져 있어(`_ban_on` 이 보는
  두 키 중 `runtime.ban_night_before_fixed_off` 는 ORM 매핑이 없어 config_dict 에 실리는 일이
  없다 — 켤 수 있는 자리는 `ban_night_before_fixed_wanted_off` 뿐이다) 사다리 4차
  ("ban_night_minimize")가 트리거되지 않고, 그래서 `_ladder_retry` 자체가 호출되지 않는다
  (ladder.py 모듈 docstring ★ 참고) —
  `test_ban_night_minimize_retry_not_reached_by_current_scenarios`(미도달 사실 자체를 단언)로 다룬다.
  새 시나리오 **S1-LB**(`ban_night_before_fixed_wanted_off=True`)는 이 지점을 두 번(하드 복구·누적
  해제) 실제로 지난다 — `test_s1lb_*` 묶음이 그 실행 순서·config 변화를 단언한다. 이 지점은 소스상
  `first_attempt` 다음(2번째)이라 "라벨 순서 == 실행 순서"를 그대로 적용하면 틀린 가정이 된다 —
  `test_call_sites_are_twelve`(지점 수·라벨 1:1)가 라벨 쪽을, `test_s1lb_sites_in_order`가 S1-LB의
  실행 순서 쪽을 따로 다룬다.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from services.policy.hard_lock import BLOCK, override_violations
from sqlalchemy import inspect, text

from .backend_check import assert_sqlite_backends
from .canonical import layers_digest
from .engine_env import engine_env_names, env_effective, is_runtime_toggle
from .golden import (
    build_record,
    golden_path,
    mismatch_report,
    read_golden,
    update_requested,
    write_golden,
)
from .golden_gate import GOLDEN_BASE, require_golden
from .harness import run_with_output
from .ladder import (
    L3_TARGETS,
    LADDER_SITE_LABELS,
    LIVE_GRAPH_DIRS,
    EngineRecorder,
    RecorderFailure,
    call_sites,
    drain_live_graph_dirs,
    l1_delta,
    site_lines,
)
from .scenario_ladder import (
    LADDER_E_CAPABLE,
    LADDER_MAX_NIG,
    LADDER_NIGHT_CAP_NURSE,
    LADDER_SCENARIOS,
    LADDER_TREATMENT,
    LADDER_WEEKEND_OFF,
)

RAW_FAILURE_LOGS = (
    "[GradeConfig][WARN] raw 조회 실패",
    "[N2N-MinGap] 설정 조회 실패",
    "[Holiday] 설정 조회 실패",
)
LADDER_NAMES = ("S1-L", "S1-LT")
#: config_override 없이 부르는 탐색 3곳(:7680·:7707·:7895) — 설계서 §7.1 S1-L 도달 조건.
SEARCH_SITES = ("weekend_multi", "weekend_single", "night_limit_single")
#: 사다리 모드가 "실행 순서대로" 지나는 곳 — a1_probe(:8204)·ban_night_minimize_retry(:7068,
#: dev `55dacd4` 신규)만 미도달(모듈 docstring). ★ ban_night_minimize_retry 는 LADDER_SITE_LABELS
#: 안에서는 소스 위치상 2번째(first_attempt 다음)지만, 그 자리는 `_ladder_retry` 헬퍼 **정의**
#: 줄이라 실행 순서는 따로 결정된다(실제로 불렸다면 isolated_work_soft 뒤였을 것 — ladder.py
#: 모듈 docstring ★ 참고). 이번 시나리오는 그 지점에 닿지 않으므로 "라벨 소스 순서 == 실행
#: 순서" 전제가 깨지는 사례가 현재는 생기지 않는다 — 닿게 되면 이 튜플과 함께 순서 전제를
#: 다시 검토해야 한다(아래 test_ban_night_minimize_retry_not_reached_by_current_scenarios).
EXPECTED_LADDER_SITES = tuple(
    s for s in LADDER_SITE_LABELS if s not in ("a1_probe", "ban_night_minimize_retry")
)
#: ★ 2026-10-13 신규 — S1-LB 전용 실행 순서(EXPECTED_LADDER_SITES 10곳 + ban_night_minimize_retry
#: 를 isolated_work_soft 뒤·n2n_min_gap_release 앞에 끼운 11곳). S1-L·S1-LT 의 EXPECTED_LADDER_SITES
#: 는 건드리지 않는다(위치 의존 단언을 흔들지 않기 위해 별도 시나리오로 뺐다 — 모듈 docstring 참고).
EXPECTED_LADDER_SITES_LB = (
    "first_attempt", "team_min_soft", "same_shift_soft", "isolated_work_soft",
    "ban_night_minimize_retry", "n2n_min_gap_release", "mus_diag", "undiag_probe",
    "weekend_multi", "weekend_single", "night_limit_single",
)
#: 사다리 누적 갱신 키(:7134·:7196·:7271).
#: ★ 설계 §7.1 의 11곳 순회를 위한 기록기 정책이다. 운영 엔진은 1단계 INFEASIBLE 일 때 stage2 에
#:   들어가지 않아(cp_sat/fallback_lex.py:3989-3991) '실근무 0 + stage2 실패 플래그' 조합이 생기지 않는다.
#:   S1-L 탐색 3곳의 n2n_min_gap=0 은 하네스가 만든 값이다(:7226 도달도 기록기가 세운 플래그 덕이다).
LADDER_ACCUMULATED = {"same_shift_hard_k": 0, "isolated_work_hard": False, "n2n_min_gap": 0}
_REPORTS_DIR = Path(__file__).resolve().parents[2] / "tools" / "harness" / "reports"
_RUNTIME_TOGGLES = ("FB_VERIFY_SKIP_STAGE3", "AIDE_ENABLE_MUS_REGISTRY")


@dataclass
class LadderRuns:
    """한 시나리오의 두 실행 — 기록 두 벌 · 첫 실행 출력 · 막힌 외부 접촉 · live graph 쓰기 수."""

    records: list[dict]
    stdout: str
    blocked: list[str]
    live_graph: list[int]


def _schema(engine) -> dict:
    """하네스 엔진의 표별 컬럼 이름."""
    insp = inspect(engine)
    return {t: tuple(c["name"] for c in insp.get_columns(t)) for t in sorted(insp.get_table_names())}


def _reports_listing() -> tuple[str, ...]:
    """저장소 `tools/harness/reports/` 의 파일·폴더 목록(재귀)."""
    if not _REPORTS_DIR.exists():
        return ()
    return tuple(sorted(str(p.relative_to(_REPORTS_DIR)) for p in _REPORTS_DIR.rglob("*")))


def _run_twice(engine, scenario, guard_log) -> LadderRuns:
    """시나리오를 두 번 돌려 기록을 모은다(live graph 임시 폴더는 세고 지운다)."""
    before = len(guard_log)
    runs = [run_with_output(engine, scenario) for _ in range(2)]
    return LadderRuns(records=[build_record(scenario, run) for run in runs], stdout=runs[0].stdout,
                      blocked=list(guard_log[before:]), live_graph=drain_live_graph_dirs())


def _state(engine) -> tuple:
    """흔적 비교용 — 스키마 · 엔진 env · 저장소 reports 목록."""
    return (_schema(engine), env_effective(), _reports_listing())


@pytest.fixture(scope="module")
def ladder_runs(engine, engine_input_tables, guard_log):
    """새 시나리오를 두 번씩 돌린다. 실행 전후 상태도 남긴다(첫 줄이 프로덕션 실행 앞 backend 확인)."""
    assert_sqlite_backends(engine)
    before = _state(engine)
    runs = {name: _run_twice(engine, scenario, guard_log) for name, scenario in LADDER_SCENARIOS.items()}
    return {"runs": runs, "before": before, "after": _state(engine)}


def _attempts(record: dict, site: str | None = None) -> list[dict]:
    """시도 기록(지점으로 거를 수 있다)."""
    return [a for a in record["layers"]["attempts"] if site is None or a["site"] == site]


def _cfg_delta(attempt: dict) -> dict:
    """시도의 config 차이(없으면 빈 changed/removed)."""
    return attempt["delta"].get("config_data") or {"changed": {}, "removed": []}


# ── 호출 지점 · 골든 · 결정론 · 안전 ────────────────────────────────────


def test_call_sites_are_twelve():
    """`_run_cp_sat_basic(` 호출 지점이 12곳이고 라벨과 1:1 이다(지점이 늘거나 줄면 여기서 실패).

    ★ 2026-10-13(dev `55dacd4`) — 11→12. 새 지점은 `_ladder_retry` 헬퍼 본문(소스상 2번째,
    `ban_night_minimize_retry`) — ladder.py 모듈 docstring ★ 참고.
    """
    sites = call_sites()
    assert len(sites) == len(LADDER_SITE_LABELS) == 12
    assert list(sites) == sorted(sites)
    assert set(site_lines()) == set(LADDER_SITE_LABELS)


@pytest.mark.parametrize("name", list(LADDER_SCENARIOS))
def test_golden(name, ladder_runs):
    """첫 실행 기록 = 골든(UPDATE_GOLDEN=1 이면 다시 쓴다)."""
    record = ladder_runs["runs"][name].records[0]
    if update_requested():
        write_golden(name, record)
        return
    golden = read_golden(name)
    assert golden is not None, f"골든 없음: {golden_path(name)} — UPDATE_GOLDEN=1 로 만든다"
    assert golden["digest"] == layers_digest(golden["layers"]), "골든 파일 digest 가 층과 맞지 않는다"
    report = mismatch_report(name, golden, record)
    assert report is None, report


@pytest.mark.parametrize("name", list(LADDER_SCENARIOS))
def test_two_runs_identical(name, ladder_runs):
    """같은 프로세스에서 두 번 돌린 기록이 같다(시도·차이·env·L3·쿼리 순서열·payload)."""
    first, second = ladder_runs["runs"][name].records
    report = mismatch_report(f"{name}(1회차)", first, second)
    assert report is None, report


@pytest.mark.parametrize("name", list(LADDER_SCENARIOS))
def test_safety(name, ladder_runs):
    """solve 0 · 별도 세션 0 · L1 밖 L2 0 · 가드 차단 기록 0 · raw 실패 로그 0 · 예상 종착점."""
    runs = ladder_runs["runs"][name]
    record = runs.records[0]
    assert record["safety"] == {"solve_calls": 0, "separate_sessions": 0, "l2_calls_outside_l1": 0}
    assert runs.blocked == [], runs.blocked
    assert [msg for msg in RAW_FAILURE_LOGS if msg in runs.stdout] == []
    expected = (("returned", "post_complete") if name == "S1-L3"
                else ("http_exception", "ladder_unrecoverable"))
    assert (record["outcome"], record["path"]) == expected, record["error"]


# ── 사다리 12곳(미도달 2곳) ──────────────────────────────────────────────


@pytest.mark.parametrize("name", LADDER_NAMES)
def test_ladder_reaches_search_sites(name, ladder_runs):
    """:7680(주말휴무 다인) · :7707(주말휴무 단일) · :7895(야간한도 개인) 세 곳에 닿는다."""
    sites = {a["site"] for a in _attempts(ladder_runs["runs"][name].records[0])}
    missing = [s for s in SEARCH_SITES if s not in sites]
    assert missing == [], f"{name} 이 닿지 못한 탐색 지점: {missing}"


@pytest.mark.parametrize("name", LADDER_NAMES)
def test_ladder_sites_in_order(name, ladder_runs):
    """지난 곳 = a1_probe·ban_night_minimize_retry 를 뺀 10곳, 처음 지난 순서가 소스 순서와 같다
    (첫 시도 → 완화 4단 → 탐색). EXPECTED_LADDER_SITES 주석 참고."""
    seen: list[str] = []
    for attempt in _attempts(ladder_runs["runs"][name].records[0]):
        if attempt["site"] not in seen:
            seen.append(attempt["site"])
    assert seen == list(EXPECTED_LADDER_SITES)


@pytest.mark.parametrize("name", LADDER_NAMES)
def test_ban_night_minimize_retry_not_reached_by_current_scenarios(name, ladder_runs):
    """★ 2026-10-13 신규 — dev `55dacd4` 가 끼운 2번째 호출 지점(`ban_night_minimize_retry`,
    `_ladder_retry` 헬퍼 본문)이 지금 시나리오로는 닿지 않는다는 사실 자체를 단언한다(지점을 그냥
    빼기만 하면 나중에 데이터가 바뀌어 실제로 닿았을 때 조용히 넘어간다 — 단언을 약하게 하지
    않는다). 원인: 이 시나리오의 S1 설정이 `ban_night_before_fixed_wanted_off` 를 켜지 않아
    `roster_create_service.py` 의 `_ban_on` 이 False 라 사다리 4차("ban_night_minimize")가
    트리거되지 않는다(`_ban_on` 이 보는 다른 한 키 `runtime.ban_night_before_fixed_off` 는 ORM
    매핑이 없어 config_dict 에 실리는 일이 없으므로, 켤 수 있는 자리는 애초에 저 키 하나뿐이다)
    — 트리거되지 않으면 그 안의 `_ladder_retry(...)`
    호출 자체가 일어나지 않는다. 이 지점에 실제로 닿게 되면(S1 설정에 그 플래그를 켜는 등) 이
    테스트가 실패해 `EXPECTED_LADDER_SITES`·순서 전제를 다시 봐야 한다는 신호가 된다."""
    sites = {a["site"] for a in _attempts(ladder_runs["runs"][name].records[0])}
    assert "ban_night_minimize_retry" not in sites


# ── S1-LB — 사다리 4차 "전날 N 금지 최소화" 실제 발화(2026-10-13 신규) ──────────────


def test_s1lb_sites_in_order(ladder_runs):
    """S1-LB 의 실행 순서 = EXPECTED_LADDER_SITES_LB(10곳 + ban_night_minimize_retry 1곳, 11곳)."""
    seen: list[str] = []
    for attempt in _attempts(ladder_runs["runs"]["S1-LB"].records[0]):
        if attempt["site"] not in seen:
            seen.append(attempt["site"])
    assert seen == list(EXPECTED_LADDER_SITES_LB)


def test_s1lb_reaches_ban_night_minimize_retry_twice(ladder_runs):
    """S1-LB 는 `_ladder_retry` 를 두 번 부른다 — ①하드 복구(앞 단계가 푼 same_shift·isolated_work
    하드를 이 시도만 되돌림) ②누적 해제(되돌리지 않고 그대로). 둘 다 월 야간 상한을 안 올려 실패
    결과라 사다리는 다음 단계(n2n_min_gap_release)로 계속 내려간다(§본문 "4차 완화" 주석)."""
    record = ladder_runs["runs"]["S1-LB"].records[0]
    retries = _attempts(record, "ban_night_minimize_retry")
    assert [a["result"] for a in retries] == ["fail", "fail"]
    hard_recovery, cumulative_release = (_cfg_delta(a)["changed"] for a in retries)
    common = {"ban_night_minimize": True, "_ban_night_minimize_retry_attempted": True,
              "_same_shift_soft_retry_attempted": True, "_isolated_work_soft_retry_attempted": True}
    assert {k: hard_recovery[k] for k in common} == common
    assert {k: cumulative_release[k] for k in common} == common
    # ①하드 복구 — 이 시도만 same_shift_hard_k(기본 3)·isolated_work_hard 를 되돌린다.
    assert hard_recovery["same_shift_hard_k"] == 3
    assert hard_recovery["isolated_work_hard"] is True
    # ②누적 해제 — 앞 단계가 풀어 둔 값(0·False)을 그대로 쓴다.
    assert cumulative_release["same_shift_hard_k"] == 0
    assert cumulative_release["isolated_work_hard"] is False


def test_s1lb_ban_night_minimize_accumulates_into_later_search_sites(ladder_runs):
    """두 계획 다 실패하면 `config_dict` 에 `ban_night_minimize`·시도 플래그가 영구히 남는다.
    바로 다음 단계(n2n_min_gap_release)와, 그 뒤 사다리 누적을 그대로 받는 탐색 3곳
    (SEARCH_SITES — LADDER_ACCUMULATED 와 같은 누적 경로)에는 쌓인 채로 실린다.
    ★ `mus_diag`·`undiag_probe` 는 쌓이지 않는다(실측) — 그 두 지점은 사다리가 아직 실패를
    모르던 시점의 config 사본을 따로 쓴다(이 사실 자체는 이번 범위 밖이라 단언하지 않는다)."""
    record = ladder_runs["runs"]["S1-LB"].records[0]
    for site in ("n2n_min_gap_release", *SEARCH_SITES):
        for attempt in _attempts(record, site):
            delta = _cfg_delta(attempt)
            assert delta["changed"].get("ban_night_minimize") is True, (site, attempt["seq"])
            assert delta["changed"].get("_ban_night_minimize_retry_attempted") is True, (site, attempt["seq"])


@pytest.mark.parametrize("name", LADDER_NAMES)
def test_ladder_result_policy(name, ladder_runs):
    """기록기 결과 — 월 야간 상한을 올린 탐색 시도만 성공, 나머지는 실패. 탐색이 상한 완화를 찾았다."""
    attempts = _attempts(ladder_runs["runs"][name].records[0])
    for attempt in attempts:
        raised = int(_cfg_delta(attempt)["changed"].get("max_nig_per_month") or 0) > LADDER_MAX_NIG
        assert attempt["result"] == ("success" if raised else "fail"), attempt["seq"]
    assert {a["site"] for a in attempts if a["result"] == "success"} == {"undiag_probe"}
    assert "found=True resolutions=['raise_max_night_cap']" in ladder_runs["runs"][name].stdout


# ── EI-11 11a-1 — 사다리에서 하드락 탐색 시도만 사라졌다 ───────────────────────────────

#: 11a-1 이 지운 하드락 탐색 시도(기준본 seq) — 3N2OFF 해제(#4)·연속 N 7·전이 3종·연속근무 14. 2N2OFF 해제는 병동 기본값이
#: 이미 꺼져 있어(`ward.py`) 기준본에도 시도가 없다(no-op 건너뜀).
#: ★ EI-14b2a4 — S1-L 계열 시나리오를 재설계해(주말 일자별 요구 = 기본 · E·N 가능자 3명 · 월 야간 상한 12) 기준본(`golden_base`)도
#:   순수 dev(`redev_4ec454e/pure_harness`)에서 새 시나리오로 다시 떴다 — 탐색 시도 번호가 달라졌다.
EI11_REMOVED_PROBE_SEQ = {"S1-L": [10, 11, 14, 15, 16, 18], "S1-LT": [10, 11, 14, 15, 16, 18],
                          "S1-LB": [12, 13, 16, 17, 18, 20]}


def _base_attempts(name: str) -> list[dict]:
    """EI 적용 전 동결본(`golden_base/dev4ec454e`)의 사다리 시도 — EI-11 정규화의 기준."""
    require_golden(GOLDEN_BASE)
    record = json.loads((GOLDEN_BASE.path / f"{name}.json").read_text(encoding="utf-8"))
    return record["layers"]["attempts"]


def _is_hard_lock_probe_attempt(attempt: dict) -> bool:
    """탐색(undiag_probe) 시도 중 config 변경이 하드락 BLOCK 인 것 — 판정은 요청 가드와 같은 `override_violations`."""
    changed = _cfg_delta(attempt)["changed"]
    return attempt["site"] == "undiag_probe" and any(v.kind == BLOCK for v in override_violations(dict(changed)))


def _without_queries_before(attempt: dict) -> dict:
    return {k: v for k, v in attempt.items() if k != "queries_before"}


@pytest.mark.parametrize("name", ("S1-L", "S1-LT", "S1-LB"))
def test_ei11_only_hard_lock_probe_attempts_removed(name, ladder_runs):
    """기준본 시도에서 하드락 탐색 시도만 지우고 seq 를 0부터 다시 매긴 것 == 지금 시도(`queries_before` 제외).

    `expected_diff` 의 11a1 허용(`$.layers.attempts` 경로)은 목록 재번호 때문에 '그 시도만'을 못 가린다 — 여기서 판정
    함수로 가린다. 쿼리 위치(`queries_before`)는 시도가 빠진 만큼 당겨지므로 비교에서 뺀다(전체 수는 query_delta 60)."""
    base = _base_attempts(name)
    assert [a["seq"] for a in base if _is_hard_lock_probe_attempt(a)] == EI11_REMOVED_PROBE_SEQ[name]
    kept = [a for a in base if not _is_hard_lock_probe_attempt(a)]
    expected = [{**_without_queries_before(a), "seq": seq} for seq, a in enumerate(kept)]
    current = [_without_queries_before(a) for a in _attempts(ladder_runs["runs"][name].records[0])]
    assert current == expected


#: `_run_cp_sat_basic` 이 시도마다 다시 읽던 테이블(설계서 §5.3 EI-13a 재조회 ①~⑦ 의 대상) — EI-13a-2 뒤로는 첫 시도 직전 빌더가
#: 한 번만 읽고 시도는 `attempt_src` 를 쓴다.
REREAD_TABLES = {"select:nurse_weekendoff_period", "select:teams", "select:shifts",
                 "select:nurse_allowed_shift_period", "select:schedules",
                 "select:banned_wanted_entries", "select:roster_grade_config"}


@pytest.mark.parametrize("name", LADDER_NAMES)
def test_attempts_do_not_reread_db(name, ladder_runs):
    """EI-13a-2 — 사다리 2~5번째 시도 사이 쿼리 창에 재조회 7종이 하나도 없다(전엔 창마다 7종이 다 있었다)."""
    record = ladder_runs["runs"][name].records[0]
    marks = [a["queries_before"] for a in _attempts(record)]
    assert marks == sorted(marks) and marks[0] == record["queries"]["marks"]["L1"]
    tables = record["queries"]["tables"]
    for start, end in zip(marks[1:5], marks[2:6], strict=True):    # 사다리 2~5번째 시도 사이
        assert not set(tables[start:end]) & REREAD_TABLES, (start, end, tables[start:end])


# ── 시도별 변형 ⓐⓑⓒ ─────────────────────────────────────────────────────


def test_s1l_search_calls_get_accumulated_config(ladder_runs):
    """ⓐ S1-L(treatment 없음) — 탐색 3곳 config 에 사다리 누적 갱신 값이 실린다(§4.6 ⓐ).

    n2n_min_gap=0 은 기록기 정책(첫 시도 stage2 실패 플래그)이 만든 값이다 — LADDER_ACCUMULATED 주석.
    """
    record = ladder_runs["runs"]["S1-L"].records[0]
    for site in SEARCH_SITES:
        for attempt in _attempts(record, site):
            delta = _cfg_delta(attempt)
            assert {k: delta["changed"].get(k) for k in LADDER_ACCUMULATED} == LADDER_ACCUMULATED
            assert delta["removed"] == []


def test_s1lt_search_calls_get_latest_config_copy(ladder_runs):
    """ⓐ S1-LT — 탐색 3곳은 treatment 패치·차감 뒤 수요·사다리 누적이 빠진 latest_config 사본을 받는다."""
    record = ladder_runs["runs"]["S1-LT"].records[0]
    first = record["layers"]["L1"][0]["config_data"]
    assert first["_force_grade_max_soft_fallback"] is True                 # treatment 적용(:6361)
    assert first["daily_shift_requirements_by_day"][2]["D"] == 3           # 평일 D 4 − 고정근무자 1(:6729)
    for site in SEARCH_SITES:
        for attempt in _attempts(record, site):
            delta = _cfg_delta(attempt)
            assert "_force_grade_max_soft_fallback" in delta["removed"]
            assert delta["changed"]["daily_shift_requirements_by_day"][2]["D"] == 4
            assert not set(LADDER_ACCUMULATED) & set(delta["changed"])
    assert f"applied=['{LADDER_TREATMENT}']" in ladder_runs["runs"]["S1-LT"].stdout


@pytest.mark.parametrize("name", LADDER_NAMES)
def test_weekend_override_only_in_its_attempt(name, ladder_runs):
    """ⓒ weekend_off_override 가 그 시도 간호사에만 실린다(다인 = 앞 2명, 단일 = 1명씩)."""
    record = ladder_runs["runs"][name].records[0]
    (multi,) = _attempts(record, "weekend_multi")
    assert multi["delta"]["nurses_data"] == {
        nid: {"changed": {"is_weekend_off": False}, "removed": []} for nid in LADDER_WEEKEND_OFF[:2]}
    singles = _attempts(record, "weekend_single")
    assert [list(a["delta"]["nurses_data"]) for a in singles] == [[nid] for nid in LADDER_WEEKEND_OFF]


@pytest.mark.parametrize("name", LADDER_NAMES)
def test_night_patch_only_in_its_attempt(name, ladder_runs):
    """ⓑ 야간한도 개인 지목 — 그 시도에만 n_max 가 상한으로 낮춰 실리고, 다른 시도엔 없다(finally 복원)."""
    record = ladder_runs["runs"][name].records[0]
    (night,) = _attempts(record, "night_limit_single")
    assert night["delta"]["nurses_data"] == {
        LADDER_NIGHT_CAP_NURSE: {"changed": {"n_max": LADDER_MAX_NIG}, "removed": []}}
    others = [a for a in _attempts(record) if a["site"] != "night_limit_single"]
    assert all(LADDER_NIGHT_CAP_NURSE not in a["delta"].get("nurses_data", {}) for a in others)


def test_e_capable_nurse_keeps_profile(ladder_runs):
    """데이터 조건 확인 — 주말 E·N 을 설 수 있는 비주말휴무자는 N04~N06 세 명(나머지 6명은 D 전담) — Hall 결합 부족을 만드는 구조."""
    nurses = ladder_runs["runs"]["S1-L"].records[0]["layers"]["L1"][0]["nurses_data"]
    restricted = {n["nurse_id"] for n in nurses if n["allowed_shifts"] == ["D"]}
    assert not set(LADDER_E_CAPABLE) & restricted and len(restricted) == 6


# ── 시도별 env ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", LADDER_NAMES)
def test_env_effective_per_attempt(name, ladder_runs):
    """호출 시점 env — MUS 진단 시도에만 MUS 등록 토글, 탐색·개인 지목 시도에만 stage3 생략 토글."""
    for attempt in _attempts(ladder_runs["runs"][name].records[0]):
        env, site = attempt["env"], attempt["site"]
        assert env.get("MUS_DIAG_ENABLE") == "1"
        assert env.get("AIDE_PROBE_BUDGET_SEC") == "0"                 # 탐색 벽시계 상한 끔
        assert ("AIDE_ENABLE_MUS_REGISTRY" in env) == (site == "mus_diag"), attempt["seq"]
        in_search = site in ("undiag_probe", *SEARCH_SITES)
        assert (env.get("FB_VERIFY_SKIP_STAGE3") == "1") == in_search, attempt["seq"]


def test_env_names_and_classes():
    """env 이름은 엔진 경로 스캔 결과 — 토글 2종 포함 · 분류는 §3.1 표대로."""
    names = engine_env_names()
    assert set(_RUNTIME_TOGGLES) | {"AIDE_N2N_MIN_GAP", "SKIP_PRIMARY"} <= set(names)
    assert all(is_runtime_toggle(n) for n in (*_RUNTIME_TOGGLES, "AIDE_PROBE_TIME_LIMIT", "MUS_DIAG_TIME"))
    assert not any(is_runtime_toggle(n) for n in ("AIDE_N2N_MIN_GAP", "SKIP_PRIMARY", "LEX_PASS_ORDER"))


def test_digest_excludes_runtime_toggles_and_l3():
    """digest — 런타임 토글 env·L3·시도 config 메타 칸·쿼리 위치는 빼고, 실험 env 는 넣는다."""
    def layers(env: dict, l3: dict, meta: str, queries_before: int) -> dict:
        delta = {"config_data": {"changed": {"config_name": meta}, "removed": []}}
        return {"L1": [], "attempts": [{"seq": 0, "site": "first_attempt", "result": "fail",
                                        "queries_before": queries_before, "delta": delta, "env": env}],
                "L3": l3}
    base = layers({"MUS_DIAG_ENABLE": "1"}, {"calls": []}, "가", 40)
    same = layers({"FB_VERIFY_SKIP_STAGE3": "1", "AIDE_ENABLE_MUS_REGISTRY": "1",
                   "AIDE_PROBE_BUDGET_SEC": "0"}, {"calls": [1]}, "나", 33)
    other = layers({"AIDE_N2N_MIN_GAP": "5"}, {"calls": []}, "가", 40)
    assert layers_digest(base) == layers_digest(same)
    assert layers_digest(base) != layers_digest(other)
    assert base["attempts"][0]["queries_before"] == 40             # 원본 기록은 그대로 둔다


def test_l1_delta_rules():
    """시도 차이 — config 는 키 단위(바뀜·사라짐), 간호사는 id 별, 그 밖 인자는 새 값."""
    first = {"config_data": {"a": 1, "b": 2}, "nurses_data": [{"nurse_id": "N1", "x": 1}],
             "time_limit_seconds": 60}
    current = {"config_data": {"a": 1, "c": 3}, "nurses_data": [{"nurse_id": "N1", "x": 2}],
               "time_limit_seconds": 20}
    assert l1_delta(first, first) == {}
    assert l1_delta(first, current) == {
        "config_data": {"changed": {"c": 3}, "removed": ["b"]},
        "nurses_data": {"N1": {"changed": {"x": 2}, "removed": []}},
        "time_limit_seconds": 20,
    }


# ── L3 후처리 입력 ──────────────────────────────────────────────────────


def test_l3_post_inputs_recorded(ladder_runs):
    """S1-L3 — 후처리 6종 인자가 호출 순서대로 남고, 큰 객체는 내용 주소로 한 벌만 있다."""
    l3 = ladder_runs["runs"]["S1-L3"].records[0]["layers"]["L3"]
    assert [c["func"] for c in l3["calls"]] == [t[2] for t in L3_TARGETS]
    calls = {c["func"]: c["args"] for c in l3["calls"]}
    assert calls["postprocess_health_leave"]["target_ids"] == [f"N0{i}" for i in range(1, 7)]
    assert "schedule_id" not in calls["postprocess_off_swap"]["schedule"]
    refs = {calls[f]["latest_config"]["__object__"] for f in
            ("postprocess_health_leave", "postprocess_sleep_off", "postprocess_off_swap")}
    assert len(refs) == 1 and refs <= set(l3["objects"])
    assert calls["_apply_work_shift_overrides"]["nurses_data"]["__object__"] in l3["objects"]
    assert calls["postprocess_oncall"]["config_id"] == 1


def test_l3_postprocess_actually_ran(ladder_runs):
    """후처리가 기록기 근무표 위에서 실제로 돈다(보건휴가 배치 · 연차 변환)."""
    stdout = ladder_runs["runs"]["S1-L3"].stdout
    assert "[HealthLeave] G1 2026-11 배치 6건" in stdout
    assert "[OffSwap][DONE]" in stdout and "target_shift=연" in stdout


def test_l3_not_in_ladder_and_no_attempts_beyond_first_in_post(ladder_runs):
    """L3 는 성공 모드에만 있고, 성공 모드는 첫 시도 1회로 끝난다(사다리 미진입)."""
    for name in LADDER_NAMES:
        assert "L3" not in ladder_runs["runs"][name].records[0]["layers"]
    post = _attempts(ladder_runs["runs"]["S1-L3"].records[0])
    assert [(a["site"], a["result"]) for a in post] == [("first_attempt", "success")]


# ── 흔적 0 ──────────────────────────────────────────────────────────────


def test_no_trace(ladder_runs, engine):
    """흔적 0 — 스키마 · 엔진 env · 저장소 reports 가 그대로 · 시드 행 없음 · 임시 폴더 지움."""
    assert ladder_runs["after"] == ladder_runs["before"]
    assert LIVE_GRAPH_DIRS == []
    assert [ladder_runs["runs"][n].live_graph for n in LADDER_SCENARIOS] == [[1, 1], [1, 1], [1, 1], [0, 0]]
    with engine.connect() as conn:
        for sql in ("SELECT COUNT(*) FROM nurses WHERE group_id = 'G1'",
                    "SELECT COUNT(*) FROM daily_shift WHERE group_id = 'G1'",
                    "SELECT COUNT(*) FROM schedule_entries"):
            assert conn.execute(text(sql)).scalar() == 0, sql


def test_patches_restored(ladder_runs):
    """감싼 함수·토글·쓰기 경로가 원본으로 돌아왔다."""
    import services.cp_sat_basic as cp_sat_basic
    import services.live_graph_export as live_graph_export
    import services.roster_create_service as rcs

    assert rcs.generate_roster_cp_sat is cp_sat_basic.generate_roster_cp_sat
    for module_name, class_name, func_name in L3_TARGETS:
        owner = sys.modules[module_name]
        owner = getattr(owner, class_name) if class_name else owner
        assert getattr(owner, func_name).__module__.startswith("services."), func_name
    before_env = ladder_runs["before"][1]                  # 개발자 셸 값이 있으면 그 값으로 돌아와야 한다
    assert {t: os.environ.get(t) for t in _RUNTIME_TOGGLES} == {t: before_env.get(t) for t in _RUNTIME_TOGGLES}
    assert live_graph_export._REPORTS_DIR == _REPORTS_DIR


def test_recorder_failure_is_not_swallowed():
    """기록기 자체 오류는 BaseException(RecorderFailure)이라 앱의 `except Exception` 을 뚫고 나온다."""
    from types import SimpleNamespace

    from .capture import QueryLog

    capture = SimpleNamespace(layers={"L1": [], "attempts": []}, queries=QueryLog())
    spy = EngineRecorder(capture, "ladder").spy(lambda nurses_data: nurses_data)
    with pytest.raises(RecorderFailure, match="엔진 입구 기록기 오류"), contextlib.suppress(Exception):
        spy(object())                          # canonical 로 바꿀 수 없는 인자
    assert not issubclass(RecorderFailure, Exception)
