"""EI-10g(갈래 D) — 경로별 쿼리 수 기대표(p2/D/SPEC.md §3.2). 10g3 에서 단계 B 빌더가 precheck 앞으로 와 쿼리가 바뀌는 자리를 정확히 고정한다.

표 = (이동 전 쿼리 수, 이동 후 쿼리 수, 이동 후 더해진 쿼리 종류 Counter). 이동 후 수가 이동 전과 같으면 순서만 바뀐 경로다.
숫자는 합성 골든 기준이다 — 전월 근무표·파견·고정근무자가 있는 실제 병동은 더 늘 수 있다(구현 STATUS).
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter

import pytest

from .expected_diff import EI13C_DONE, EI13C_STAGES
from .golden import GOLDEN_DIR
from .golden_gate import GOLDEN, GOLDEN_PRESOLVE, require_golden

#: 차단 경로에서 단계 B 빌더가 더한 조회(합성 골든). precheck 차단 경로는 근무코드 getter 를 아직 안 읽어 `shifts` 가 하나 더 든다.
_ADDED_NIGHT = Counter({"select:schedules": 2, "select:shifts": 1, "select:nurse_weekendoff_period": 1,
                        "select:nurse_allowed_shift_period": 1, "select:banned_wanted_entries": 1})
_ADDED_PRECHECK = Counter({"select:schedules": 2, "select:shifts": 2, "select:nurse_weekendoff_period": 1,
                           "select:nurse_allowed_shift_period": 1, "select:banned_wanted_entries": 1})

#: 시나리오 → (이동 전 수, 이동 후 수, 더해진 종류). 차단 3경로만 수가 는다.
#: ★통합 2 — 이동 전은 X(12u-2)·A2(13c2·13c3) 뒤 골든이다(int1 골든이 아니다). EI5 는 presolve 동결본이라 int1 값 그대로다.
EXPECTED_QUERY_PATHS: dict[str, tuple[int, int, Counter]] = {
    "EI5-precheck-block": (68, 75, _ADDED_PRECHECK),
    "S9": (75, 81, _ADDED_NIGHT),
    "S9-F": (79, 85, _ADDED_NIGHT),
}
#: 비차단 — 수는 그대로(순서만 바뀜).
UNCHANGED_COUNT = {"S1": 75, "S1-D": 75, "S1-P": 75, "S2": 75, "S8": 75, "S3": 86, "S4": 71, "S5": 76, "S6": 115, "S7": 69,
                   "S1-L": 82, "S1-LT": 85, "S1-LB": 82, "S1-L3": 140}
#: 이동 전(int1) 차단 3경로의 쿼리 종류별 수 — 이동 후 `Counter` 에서 이것을 빼면 `_ADDED_*` 가 정확히 남아야 한다.
_OLD_COUNTERS: dict[str, Counter] = {
    "EI5-precheck-block": Counter({
        "select:nurses": 13, "select:groups": 3, "select:wanted": 1, "select:roster_config": 5, "select:schedules": 4,
        "insert:schedules": 1, "select:nurse_assignment": 4, "select:shifts": 2, "select:fixed_wanted_entries": 3,
        "select:wanted_requests": 3, "select:nurse_grade_period": 1, "select:nurse_weekendoff_period": 2,
        "select:nurse_allowed_shift_period": 2, "select:nurse_team_period": 3, "select:nurse_monthly_limits": 1,
        "select:shift_manage": 1, "select:daily_shift": 1, "select:information_schema.columns": 2,
        "select:nurse_preceptee_period": 2, "select:nurse_mutual_exclusion_period": 1, "select:information_schema.tables": 2,
        "select:weekly_off_settings": 2, "select:roster_grade_config": 3, "select:teams": 1, "select:banned_wanted_entries": 1,
        "update:roster_config": 1, "insert:roster_config_attempt": 1, "delete:roster_config_attempt": 1, "delete:schedules": 1}),
    "S9": Counter({
        "select:nurses": 11, "select:groups": 3, "select:wanted": 1, "select:roster_config": 5, "select:schedules": 4,
        "insert:schedules": 1, "select:nurse_assignment": 4, "select:shifts": 4, "select:fixed_wanted_entries": 3,
        "select:wanted_requests": 11, "select:nurse_grade_period": 1, "select:nurse_weekendoff_period": 2,
        "select:nurse_allowed_shift_period": 2, "select:nurse_team_period": 1, "select:nurse_leave_period": 1,
        "select:nurse_monthly_limits": 1, "select:shift_manage": 1, "select:daily_shift": 1,
        "select:information_schema.columns": 2, "select:nurse_preceptee_period": 2, "select:nurse_mutual_exclusion_period": 1,
        "select:information_schema.tables": 2, "select:weekly_off_settings": 2, "select:roster_grade_config": 3,
        "select:teams": 1, "select:banned_wanted_entries": 1, "update:roster_config": 1, "insert:roster_config_attempt": 1,
        "delete:roster_config_attempt": 1, "delete:schedules": 1}),
    "S9-F": Counter({
        "select:nurses": 11, "select:groups": 3, "select:wanted": 1, "select:roster_config": 6, "select:schedules": 4,
        "insert:schedules": 1, "select:nurse_assignment": 4, "select:shifts": 4, "select:fixed_wanted_entries": 4,
        "select:wanted_requests": 12, "select:nurse_grade_period": 1, "select:nurse_weekendoff_period": 2,
        "select:nurse_allowed_shift_period": 2, "select:nurse_team_period": 1, "select:nurse_leave_period": 1,
        "select:nurse_monthly_limits": 1, "select:shift_manage": 1, "select:daily_shift": 1,
        "select:information_schema.columns": 3, "select:nurse_preceptee_period": 2, "select:nurse_mutual_exclusion_period": 1,
        "select:information_schema.tables": 2, "select:weekly_off_settings": 2, "select:roster_grade_config": 3,
        "select:teams": 1, "select:banned_wanted_entries": 1, "update:roster_config": 1, "insert:roster_config_attempt": 1,
        "delete:roster_config_attempt": 1, "delete:schedules": 1}),
}
#: 비차단 경로의 이동 전 쿼리 종류 다중집합 해시(정렬한 테이블 목록의 sha256 앞 16자) — 이동 후에도 같아야 한다(순서만 바뀜).
_OLD_MULTISET_HASH = {
    "S1": "5b6fbda9c4de78d8", "S1-D": "5b6fbda9c4de78d8", "S1-P": "5b6fbda9c4de78d8", "S2": "5b6fbda9c4de78d8",
    "S3": "64e3c8b93a91ca81", "S4": "d335061fae2d9e36", "S5": "20f592a1ea6bf2bb", "S6": "349ef9c087f01f22",
    "S7": "8bda55a1d2e19729", "S8": "f1e35232bd35a57d", "S1-L": "b2f08bd67363236b", "S1-LB": "b2f08bd67363236b",
    "S1-LT": "1726cc08ba6933c1", "S1-L3": "6ed8f7446f08b18e",
}


def multiset_hash(tables: list[str]) -> str:
    return hashlib.sha256(json.dumps(sorted(tables), ensure_ascii=False).encode()).hexdigest()[:16]


def query_multiset_delta(before_tables: list[str], after_tables: list[str]) -> Counter:
    """이동 후에 더 생긴 쿼리(종류별 수) — 줄어든 것은 음수로 남지 않고 버린다(`Counter` 뺄셈)."""
    return Counter(after_tables) - Counter(before_tables)


def test_query_multiset_delta_counts_only_added():
    assert query_multiset_delta(["a", "b"], ["b", "a"]) == Counter()
    assert query_multiset_delta(["a", "b"], ["b", "a", "c", "c"]) == Counter({"c": 2})
    assert query_multiset_delta(["a", "b", "b"], ["a", "b"]) == Counter()


def _read_main_record(name: str) -> dict:
    """주 골든(`golden/`) 한 시나리오 기록."""
    require_golden(GOLDEN)
    return json.loads((GOLDEN_DIR / f"{name}.json").read_text(encoding="utf-8"))


def _read_presolve_record(name: str) -> dict:
    """차단 전용 골든(`golden_presolve/`) 한 시나리오 기록."""
    require_golden(GOLDEN_PRESOLVE)
    return json.loads((GOLDEN_PRESOLVE.path / f"{name}.json").read_text(encoding="utf-8"))


def _golden_query_record(name: str) -> dict:
    return _read_presolve_record(name) if name == "EI5-precheck-block" else _read_main_record(name)


@pytest.mark.parametrize("name", sorted(EXPECTED_QUERY_PATHS))
def test_blocked_paths_query_count_equals_the_expected_table(name):
    """precheck 차단 +7 · 나이트 차단 +6 — 골든 쿼리 수가 정확히 표의 이동 후 수다(주 골든·presolve 골든)."""
    before, after, _added = EXPECTED_QUERY_PATHS[name]
    assert _golden_query_record(name)["queries"]["count"] == after
    assert after - before == (7 if name == "EI5-precheck-block" else 6)
    if name != "EI5-precheck-block":   # S9·S9-F 는 presolve 골든에도 있다 — 동결본은 EI-13c 감소를 얹지 않아 그만큼 많다(통합 2)
        _ei13c = sum(EI13C_STAGES[d].query_delta.get(f"presolve/{name}", 0) for d in EI13C_DONE)
        assert _read_presolve_record(name)["queries"]["count"] == after + _ei13c


@pytest.mark.parametrize("name", sorted(EXPECTED_QUERY_PATHS))
def test_blocked_paths_added_queries_are_exactly_the_builder_kinds_and_all_before_l0(name):
    """차단 경로의 쿼리 종류별 수 = 이동 전 + 표의 더해진 종류(그 밖은 하나도 안 늘고 안 준다) — 더해진 쿼리는 전부 L0 표지 앞(precheck 전)이다."""
    _before, _after, added = EXPECTED_QUERY_PATHS[name]
    record = _golden_query_record(name)
    tables = record["queries"]["tables"]
    now = Counter(tables)
    assert query_multiset_delta(list(_OLD_COUNTERS[name].elements()), tables) == added
    assert not (_OLD_COUNTERS[name] - now), "이동 전에 있던 쿼리가 줄었다"
    before_l0 = Counter(tables[:record["queries"]["marks"]["L0"]])
    assert not (added - before_l0), "더해진 빌더 조회는 precheck(L0 표지) 앞에 있어야 한다"


@pytest.mark.parametrize(("name", "count"), sorted(UNCHANGED_COUNT.items()))
def test_unblocked_paths_keep_their_query_multiset(name, count):
    """비차단 14경로 — 수와 종류별 다중집합이 이동 전과 같다(순서만 바뀜 · 시도마다 재조회 없음은 `test_ladder` 가 본다)."""
    record = _golden_query_record(name)
    assert record["queries"]["count"] == count
    assert multiset_hash(record["queries"]["tables"]) == _OLD_MULTISET_HASH[name]


def test_expected_table_covers_every_golden_scenario():
    """표가 합성 골든 16개를 빠짐없이 덮는다(시나리오가 늘면 표도 늘려야 한다)."""
    require_golden(GOLDEN)
    names = {p.stem for p in GOLDEN_DIR.glob("*.json")}
    assert names == set(UNCHANGED_COUNT) | {"S9", "S9-F"}
