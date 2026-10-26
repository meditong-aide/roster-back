"""EI-15 15a — 대체근무 추천의 하드락 키 기본값 통일(D24) expected-diff 시험.

바뀐 것: 설정(config)이 비었을 때(config 없음 · 속성 없음 · 0/None)의 하드락 키 기본값.
  연속근무 6→5 · 월 나이트 8→15 · banned_day_after_eve False→True · two_offs_after_three_nig False→True.
안 바뀐 것: 근무표가 쓴 config 행에 값이 있으면 그 값을 그대로 읽는다([RVERIFY-08]).
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest
from services import replacement_recommend_service as rrs
from services.engine_contract.defaults import registry_default

_HARD_KEYS = ("max_conseq_work", "max_nig_per_month", "banned_day_after_eve",
              "two_offs_after_two_nig", "two_offs_after_three_nig")


def test_registry_default_matches_engine_defaults():
    assert registry_default("max_conseq_work") == 5
    assert registry_default("max_nig_per_month") == 15
    assert registry_default("two_offs_after_three_nig") is True
    assert registry_default("banned_day_after_eve") is True
    assert registry_default("two_offs_after_two_nig") is False
    # 별칭으로 물어도 정본과 같다
    assert registry_default("max_consecutive_work_days") == 5


@pytest.mark.parametrize("cfg", [None, SimpleNamespace(), SimpleNamespace(max_conseq_work=0, max_nig_per_month=None)])
def test_empty_config_uses_engine_defaults(cfg):
    # (옛 값 → 새 값) 연속 6→5 · 월N 8→15
    assert rrs._hl_int(cfg, "max_conseq_work") == 5
    assert rrs._hl_int(cfg, "max_nig_per_month") == 15


def test_empty_config_bool_defaults_flip_on():
    # 옛 기본 False → 새 True (config 없음 · 속성 없음)
    for cfg in (None, SimpleNamespace()):
        assert rrs._hl_bool(cfg, "banned_day_after_eve") is True
        assert rrs._hl_bool(cfg, "two_offs_after_three_nig") is True
        assert rrs._hl_bool(cfg, "two_offs_after_two_nig") is False


def test_config_row_values_win():
    cfg = SimpleNamespace(max_conseq_work=4, max_nig_per_month=9, banned_day_after_eve=False,
                          two_offs_after_three_nig=None, two_offs_after_two_nig=True)
    assert rrs._hl_int(cfg, "max_conseq_work") == 4
    assert rrs._hl_int(cfg, "max_nig_per_month") == 9
    assert rrs._hl_bool(cfg, "banned_day_after_eve") is False
    assert rrs._hl_bool(cfg, "two_offs_after_three_nig") is False  # NULL 은 엔진처럼 꺼짐
    assert rrs._hl_bool(cfg, "two_offs_after_two_nig") is True


def _risk_tags(config, n_work_before: int):
    sched = ["D"] * (n_work_before + 1)
    _score, _risk, tags = rrs._rule_safety_score(
        nurse=SimpleNamespace(), schedule=sched, day_idx=n_work_before, target_shift_code="D",
        shift_meta={}, config=config,
    )
    return tags


def test_sixth_consecutive_day_is_now_risky_without_config():
    # 앞 5일 근무 + 오늘 = 연속 6일. 옛 기본(6)은 통과, 새 기본(5)은 위반 위험을 단다.
    assert "consecutive_work_risk" in _risk_tags(None, 5)
    assert "consecutive_work_risk" not in _risk_tags(None, 4)
    # 설정 행이 6 이면 그 값(근무표가 쓴 config)을 따른다
    assert "consecutive_work_risk" not in _risk_tags(SimpleNamespace(max_conseq_work=6), 5)


def test_no_hard_lock_literal_defaults_left_in_service():
    """하드락 키를 getattr(config, 키, 리터럴)로 읽는 자리가 서비스에 없다(기본값 3벌 재발 방지)."""
    src = Path(rrs.__file__).read_text(encoding="utf-8")
    bad = []
    for node in ast.walk(ast.parse(src)):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr"
                and len(node.args) == 3 and isinstance(node.args[1], ast.Constant)
                and node.args[1].value in _HARD_KEYS and isinstance(node.args[2], ast.Constant)
                and node.func.id == "getattr" and getattr(node.args[0], "id", "") == "config"):
            # _hl_* 헬퍼 안의 getattr 는 변수 기본값이라 Constant 가 아니다
            bad.append((node.args[1].value, node.lineno))
    assert bad == []
