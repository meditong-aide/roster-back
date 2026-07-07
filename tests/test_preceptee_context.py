"""build_preceptee_context (엔진 중앙 빌더) 단위 테스트 — 순수함수, DB 무관."""
from types import SimpleNamespace

from services.cp_sat.preceptee_context import build_preceptee_context


def _nurses(*ids):
    return [SimpleNamespace(db_id=i, nurse_id=i) for i in ids]


def test_new_shape_resolves_who_and_days():
    nurses = _nurses("P", "MENTOR", "X")
    cmap = {"P": {"preceptor_id": "MENTOR", "days": {0, 1, 2}}}
    ctx = build_preceptee_context(nurses, cmap, num_days=31)
    assert ctx == {0: (1, frozenset({0, 1, 2}))}  # P=idx0 follows MENTOR=idx1


def test_empty_days_excluded():
    nurses = _nurses("P", "MENTOR")
    ctx = build_preceptee_context(nurses, {"P": {"preceptor_id": "MENTOR", "days": set()}}, 31)
    assert ctx == {}  # 종료/미겹침 → 제외


def test_absent_nurse_not_preceptee():
    nurses = _nurses("P", "MENTOR")
    ctx = build_preceptee_context(nurses, {}, 31)
    assert ctx == {}  # map 에 없으면 프리셉티 아님(default=follow 없음)


def test_full_month_kept():
    nurses = _nurses("P", "MENTOR")
    full = set(range(31))
    ctx = build_preceptee_context(nurses, {"P": {"preceptor_id": "MENTOR", "days": full}}, 31)
    assert ctx[0][1] == frozenset(full)  # 전체월도 명시 유지(삭제·default 위임 없음)


def test_preceptor_not_in_roster_is_none():
    nurses = _nurses("P")  # MENTOR 명단 밖
    ctx = build_preceptee_context(nurses, {"P": {"preceptor_id": "MENTOR", "days": {0}}}, 31)
    assert ctx == {0: (None, frozenset({0}))}  # WHO 해석 실패 → None(호출부 폴백)


def test_legacy_set_shape_backward_compat():
    nurses = _nurses("P", "MENTOR")
    ctx = build_preceptee_context(nurses, {"P": {1, 2}}, 31)  # 구 형태: set 직접
    assert ctx == {0: (None, frozenset({1, 2}))}
