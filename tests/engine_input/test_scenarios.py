"""골든 시나리오 테스트(설계서 §7.1 · EI-1 3부) — S1~S9 · S1-D · S1-P · S9-F.

시나리오마다 프로덕션 경로(`generate_roster_service`)를 **두 번** 돌린다.
  - 골든 비교: 첫 실행 기록 = `golden/<시나리오>.json`. `UPDATE_GOLDEN=1` 이면 대신 다시 쓴다.
  - 결정론: 두 실행 기록이 같다(층·쿼리 순서열·digest·차단 payload·삼킨 실패 줄).
  - 안전: solve 0 · 별도 세션 0 · 외부 연결 시도 0 · raw 실패 로그 0.
  - 경로: 설계서가 말한 코드 경로를 실제로 지났는지(`PATH_CHECKS`).
  - 흔적 0: 끝나면 스키마·행·import 상태가 그대로다.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass

import pytest
from sqlalchemy import inspect, text

from .backend_check import assert_sqlite_backends
from .canonical import layers_digest
from .golden import (
    build_record,
    golden_path,
    mismatch_report,
    read_golden,
    update_requested,
    write_golden,
)
from .harness import run_with_output
from .scenario_defs import S2_NULL_KEYS, SCENARIOS

#: 설계서 §7.1 'raw 실제 경유 보증' — 데이터를 넣은 시나리오에서 나오면 실패다.
RAW_FAILURE_LOGS = (
    "[GradeConfig][WARN] raw 조회 실패",
    "[N2N-MinGap] 설정 조회 실패",
    "[Holiday] 설정 조회 실패",
)
_OUTBOX_MODULE = "services.push_outbox_service"


@dataclass
class ScenarioRuns:
    """한 시나리오의 두 실행 — 골든 기록 두 벌 + 첫 실행 출력 + 그동안 막힌 외부 접촉."""

    records: list[dict]
    stdout: str
    blocked: list[str]


def _schema(engine) -> dict:
    """하네스 엔진의 표별 컬럼 이름."""
    insp = inspect(engine)
    return {t: tuple(c["name"] for c in insp.get_columns(t)) for t in sorted(insp.get_table_names())}


def _run_twice(engine, scenario, guard_log) -> ScenarioRuns:
    """시나리오를 두 번 돌려 기록을 모은다."""
    before = len(guard_log)
    runs = [run_with_output(engine, scenario) for _ in range(2)]
    return ScenarioRuns(records=[build_record(scenario, run) for run in runs],
                        stdout=runs[0].stdout, blocked=list(guard_log[before:]))


@pytest.fixture(scope="module")
def scenario_runs(engine, engine_input_tables, guard_log):
    """전 시나리오를 두 번씩 돌린다. 실행 전후 스키마·import 상태도 남긴다.

    첫 줄 backend 확인은 함수 범위 autouse 확인보다 앞서 프로덕션 실행을 막는 자리다.
    """
    assert_sqlite_backends(engine)
    state_before = (_schema(engine), _OUTBOX_MODULE in sys.modules)
    runs = {name: _run_twice(engine, scenario, guard_log) for name, scenario in SCENARIOS.items()}
    state_after = (_schema(engine), _OUTBOX_MODULE in sys.modules)
    return {"runs": runs, "before": state_before, "after": state_after}


# ── 골든 · 결정론 · 안전 ────────────────────────────────────────────────


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_golden(name, scenario_runs):
    """첫 실행 기록 = 골든(UPDATE_GOLDEN=1 이면 다시 쓴다). 어긋나면 층·키 경로를 보인다."""
    record = scenario_runs["runs"][name].records[0]
    if update_requested():
        write_golden(name, record)
        return
    golden = read_golden(name)
    assert golden is not None, f"골든 없음: {golden_path(name)} — UPDATE_GOLDEN=1 로 만든다"
    assert golden["digest"] == layers_digest(golden["layers"]), "골든 파일 digest 가 층과 맞지 않는다"
    report = mismatch_report(name, golden, record)
    assert report is None, report


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_two_runs_identical(name, scenario_runs):
    """같은 프로세스에서 두 번 돌린 기록이 같다(층·쿼리 순서열·digest·payload)."""
    first, second = scenario_runs["runs"][name].records
    report = mismatch_report(f"{name}(1회차)", first, second)
    assert report is None, report


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_safety(name, scenario_runs):
    """solve 0 · 별도 세션 0 · L1 밖 L2 호출 0 · 외부 연결 시도 0 · raw 실패 로그 0."""
    runs = scenario_runs["runs"][name]
    record = runs.records[0]
    assert record["safety"] == {"solve_calls": 0, "separate_sessions": 0, "l2_calls_outside_l1": 0}
    assert runs.blocked == [], runs.blocked
    hits = [msg for msg in RAW_FAILURE_LOGS if msg in runs.stdout]
    assert hits == [], hits
    assert record["outcome"] in {"golden_stop", "http_exception", "exception"}, record["error"]


def test_no_trace(scenario_runs, engine):
    """흔적 0 — 스키마 그대로(시나리오 DDL 롤백) · 시드 행 없음 · 알림 모듈 import 흔적 없음."""
    assert scenario_runs["after"] == scenario_runs["before"]
    assert not hasattr(sys.modules["db.client2"], "EUN_DB_NAME")
    with engine.connect() as conn:
        for sql in ("SELECT COUNT(*) FROM nurses WHERE group_id IN ('G1', 'G2')",
                    "SELECT COUNT(*) FROM schedules WHERE group_id IN ('G1', 'G2')",
                    "SELECT COUNT(*) FROM roster_config WHERE group_id = 'G1'"):
            assert conn.execute(text(sql)).scalar() == 0, sql


# ── 경로 확인 ───────────────────────────────────────────────────────────


def _l0(rec) -> dict:
    return rec["layers"]["L0"][0]["config_dict"]


def _l0p(rec) -> dict:
    return rec["layers"]["L0p"][0]


def _l1(rec) -> dict:
    return rec["layers"]["L1"][0]


def _cfg(rec) -> dict:
    return _l1(rec)["config_data"]


def _eff(rec) -> dict:
    return rec["layers"]["L2"]["settings_eff"]


def _nurse(rec, nid: str) -> dict | None:
    return next((n for n in _l1(rec)["nurses_data"] if n["nurse_id"] == nid), None)


def _first_attempt(rec, _stdout: str = "") -> bool:
    return rec["outcome"] == "golden_stop" and rec["path"] == "first_attempt"


def _night_block(rec, forced: int, slots: int) -> bool:
    text_ = json.dumps(rec["http_detail"], ensure_ascii=False)
    return (rec["path"] == "night_block" and "MONTHLY_NIGHT_FORCED_OVERSUPPLY" in text_
            and f'"forced_n_sum": {forced}' in text_ and f'"n_slots": {slots}' in text_)


def _by_day(cfg: dict, code: str) -> list[int]:
    return [day.get(code) for day in cfg["daily_shift_requirements_by_day"]]


def _fixed(rec, source: str) -> list[tuple]:
    return [(c["nurse_index"], c["day_index"], c["shift"])
            for c in _cfg(rec).get("fixed_cells") or [] if c.get("fixed_source") == source]


PATH_CHECKS = {
    "S1": [
        ("첫 시도까지", _first_attempt),
        ("raw n2n 4 → L1·L2", lambda r, o: _cfg(r)["n2n_min_gap"] == 4 and _eff(r)["n2n_min_gap"] == 4),
        # [EI-2a] data["request"](:380)는 죽은 전달이라 지웠다 — 더는 prefs_data 에 실리지
        #   않는다. "같은 경로를 지났다"는 ①print 로그(브랜치 유일 출력) ②그 경로가 실제로
        #   만든 shift 정규화 결과(D/3·O/10, S1 원본 데이터와 일치)로 바꿔 확인한다.
        ("WantedRequest 경로(:291)", lambda r, o: "WantedRequest 기반 수집" in o
         and _l1(r)["prefs_data"][0]["data"]["shift"] == {"D": {"3": 8}, "O": {"10": 9}}),
        ("daily_shift 행 없음 → 슬롯 기본값", lambda r, o: set(_by_day(_cfg(r), "N")) == {2}),
    ],
    "S1-D": [
        ("첫 시도까지", _first_attempt),
        ("시딩 행(N1)이 기본값(N2)을 이김", lambda r, o: set(_by_day(_cfg(r), "N")) == {1}
         and _cfg(r)["daily_shift_requirements"]["N"] == 2),
    ],
    "S1-P": [
        ("primary 진입(SKIP_PRIMARY=0)", lambda r, o: "SKIP_PRIMARY=0 — primary(가중합) 경로로 진입" in o),
        ("빌드 요약 기록 · solve 전 멈춤", lambda r, o: r["layers"]["P"]["constraints"] > 0
         and r["outcome"] == "golden_stop"),
    ],
    "S2": [
        ("첫 시도까지", _first_attempt),
        ("NULL 이 L1 까지", lambda r, o: all(_cfg(r)[k] is None for k in S2_NULL_KEYS)),
        ("L2 NULL 해석", lambda r, o: (_eff(r)["max_night_shifts_per_month"], _eff(r)["max_consecutive_nights"],
                                      _eff(r)["ban_n_to_d"], _eff(r)["ban_e_to_d"]) == (15, 2, False, False)),
    ],
    "S3": [
        ("L1 까지 간 뒤 L2 TypeError(:584)", lambda r, o: r["outcome"] == "exception"
         and r["error"].startswith("TypeError") and r["layers"]["L2"] == {} and len(r["layers"]["L1"]) == 1),
        ("미분류 실패 기록 경로(:5183)", lambda r, o: "insert:roster_config_attempt" in r["queries"]["tables"]),
    ],
    "S4": [
        ("첫 시도까지", _first_attempt),
        ("use_mid · M 요구", lambda r, o: _cfg(r)["daily_shift_requirements"].get("M") == 1 and _eff(r)["use_mid"]),
        ("M=D2 · 한글 gb 하위코드", lambda r, o: _l0p(r)["code2main"]["D2"] == "M"
         and _l0p(r)["id_to_main"]["D1"] == "W"),
        ("팀 NULL/{} 기본 · {0} 제외", lambda r, o: set(_cfg(r)["team_min_by_team"]) == {"1", "2"}),
        ("인계 정책", lambda r, o: "1" in _cfg(r).get("team_handoff_policy_by_team", {})),
        ("등급 상한·allow0", lambda r, o: "3" in _l1(r)["grade_config"]["constraints_max_json"]
         and _l1(r)["grade_config"]["allow_soft_fallback"] is False),
        ("MID 허용 금지셀", lambda r, o: {"N11", "N12"} <= set(_cfg(r)["initial_constraints"]["forbidden"])),
    ],
    "S5": [
        ("첫 시도까지", _first_attempt),
        ("공휴일 OFF raw → 25일 차감 없음", lambda r, o: "[Holiday] 공휴일 OFF 적용 — days=[25]" in o
         and _by_day(_l0p(r)["config"], "D")[24] == 2 and _by_day(_l0p(r)["config"], "D")[23] == 1),
        ("고정근무자는 엔진 밖", lambda r, o: _nurse(r, "F01") is None),
        ("주말휴무 기간표", lambda r, o: _nurse(r, "N01")["is_weekend_off"] is True),
        ("주휴 activate · 변동주기", lambda r, o: _cfg(r)["weekly_off_settings_activate"] is True
         and set(_cfg(r)["weekly_off_map"]) == {"N01", "N02", "N03", "N04"}),
    ],
    "S6": [
        ("첫 시도까지", _first_attempt),
        ("파견 in(target 프로필)", lambda r, o: _nurse(r, "X01")["is_inbound"] is True
         and _nurse(r, "X01")["allowed_shifts"] == ["D", "E"]),
        ("차단일 4명", lambda r, o: set(_cfg(r)["blocked_by_nurse_id"]) == {"N06", "N07", "N08", "X01"}),
        ("커버리지 제외 · 상대 병동 OFF", lambda r, o: set(_cfg(r)["coverage_exclude_nurse_days"]) == {"N07", "N08"}
         and _cfg(r)["other_group_offs"] == {"N07": 1}),
        ("월중 경계(:5793)", lambda r, o: _l0(r)["initial_constraints"]["forced_off"]["X01"] == [19]),
        ("병동이동 발효 쓰기 + 알림 대기열", lambda r, o: {"update:nurse_assignment", "update:nurses",
                                                 "insert:push_outbox"} <= set(r["queries"]["tables"])),
    ],
    "S7": [
        ("첫 시도까지", _first_attempt),
        ("issued 마감본 선택", lambda r, o: "schedule_id=g1oct0000001" in o),
        ("꼬리 NN·3N·1N", lambda r, o: {k: _cfg(r)["prev_month_n_tail"][k] for k in ("N01", "N02", "N03")}
         == {"N01": 2, "N02": 3, "N03": 1}),
        ("3N 뒤 OFF 1칸 남음 → day0 강제 OFF", lambda r, o: _cfg(r)["initial_constraints"]["forced_off"]["N02"] == [0]),
        ("1N 꼬리 분기", lambda r, o: "N03: 1N tail" in o),
        ("raw n2n 5 → L2", lambda r, o: _eff(r)["n2n_min_gap"] == 5),
    ],
    "S8": [
        ("첫 시도까지", _first_attempt),
        ("카드 delta 가 raw n2n 을 덮음", lambda r, o: _cfg(r)["n2n_min_gap"] == 0 and _cfg(r)["same_shift_hard_k"] == 0),
        ("처방 → 등급 상한 soft", lambda r, o: _l1(r)["grade_config"]["allow_soft_fallback"] is True),
        ("req 1N 금지", lambda r, o: _cfg(r)["not_one_night"] is True),
        ("분배 게이지", lambda r, o: (_cfg(r)["distribution_mode"], _cfg(r)["oversupply_balance_gauge"]) == ("balanced", 8)),
        ("확정 원티드 T 만 하드 고정", lambda r, o: sorted(_fixed(r, "fixed_wanted")) == [(0, 2, "D"), (0, 9, "O"), (1, 4, "N")]),
        ("특수코드 연차", lambda r, o: _fixed(r, "special_fixed") == [(3, 11, "연")]),
        ("금지 원티드 T 만", lambda r, o: set(_cfg(r)["initial_constraints"]["forbidden"]) == {"N05", "N06"}),
    ],
    "S9": [
        ("나이트 차단 36 > 30", lambda r, o: _night_block(r, 36, 30)),
        ("프리셉티 권위 모드 — 캐시만 있는 N10 제외", lambda r, o: set(_l0(r)["preceptee_period_by_nurse_id"]) == {"N09"}
         and _l0(r)["preceptee_period_authoritative"] is True),
        ("상호배제", lambda r, o: set(_l0(r)["mutual_exclusion_by_nurse_id"]) == {"N03", "N04"}),
        ("보건휴가 대상 선정", lambda r, o: "[HealthLeave] group=G1 2026-11 대상 7명(예외설정 1" in o),
        ("차단 기록·초안 삭제", lambda r, o: {"insert:roster_config_attempt", "delete:schedules"}
         <= set(r["queries"]["tables"])),
    ],
    "S9-F": [
        ("나이트 차단 16 > 9(차감 뒤)", lambda r, o: _night_block(r, 16, 9)),
        # (EI-10g2) 고정근무 차감이 precheck 앞으로 옮겨져 L0 도 차감 뒤 수요다 — precheck 는 9 < 16 이어도 막지 않고(수요만 비교)
        #   나이트 탐지기가 막는다. outcome·path 는 그대로(옛 문구 '차감 전 칸 30 ≥ 16' 은 이동 뒤 거짓).
        ("차감 뒤(precheck) 칸 9 < 16 — precheck 통과·나이트 탐지기 차단", lambda r, o: sum(_by_day(_l0(r), "N")) == 9),
        ("차감 뒤(탐지기) 칸 9", lambda r, o: sum(_by_day(_l0p(r)["config"], "N")) == 9),
    ],
}


def test_path_checks_cover_all_scenarios():
    """경로 확인 표가 시나리오 전부를 덮는다."""
    assert set(PATH_CHECKS) == set(SCENARIOS)


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_scenario_paths(name, scenario_runs):
    """설계서가 말한 코드 경로를 실제로 지났다."""
    runs = scenario_runs["runs"][name]
    failed = [label for label, check in PATH_CHECKS[name] if not check(runs.records[0], runs.stdout)]
    assert failed == [], f"{name} 이 지나지 못한 경로: {failed}"


# ── 시나리오 쌍 ─────────────────────────────────────────────────────────


def _layer_diff_paths(a: dict, b: dict) -> list[str]:
    """두 기록의 층 차이 경로(값 제외)."""
    from .golden import diff_paths

    return [line.split(":", 1)[0] for line in diff_paths(a["layers"], b["layers"])]


def test_s1d_pair_differs_only_in_daily_requirements(scenario_runs):
    """MS-9 쌍 — 시딩 유무는 일자별 요구(by_day)만 바꾸고 쿼리 수·순서는 같다."""
    s1 = scenario_runs["runs"]["S1"].records[0]
    s1d = scenario_runs["runs"]["S1-D"].records[0]
    paths = _layer_diff_paths(s1, s1d)
    assert paths, "시딩 쌍의 차이가 0 이다"
    assert all("daily_shift_requirements_by_day" in p for p in paths), paths[:10]
    assert s1["queries"] == s1d["queries"]


def test_s1p_shares_s1_layers(scenario_runs):
    """S1-P 는 S1 과 같은 입력이다 — L0~L2·쿼리 순서가 같고 빌드 요약(P)만 더 있다."""
    s1 = scenario_runs["runs"]["S1"].records[0]
    s1p = scenario_runs["runs"]["S1-P"].records[0]
    assert {k: v for k, v in s1p["layers"].items() if k != "P"} == s1["layers"]
    assert s1p["queries"] == s1["queries"]
