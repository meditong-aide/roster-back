"""EI-14c-4 — OFF 4연속 금지(`enforce_4o_hard`)를 푸는 검증형 카드 `disable_enforce_4o`.

사다리에 없는 규칙이라 단독 해제가 실제로 풀 수 있다. 기본 꺼짐이면 빈 delta 라 탐색이 건너뛴다(재계산 0).
하드락 8종과 무관(레지스트리 hard_lock=None)이라 BLOCK 0 이다.
"""

from __future__ import annotations

import dataclasses

import pytest
from db.roster_config import NurseRosterConfig
from services.cp_sat.rule_card import RULE_CARD
from services.cp_sat.undiagnosed_probe import (
    COL_LABEL_KO,
    RELAX_CATALOG,
    TRADEOFF_KO,
    probe_relaxations,
    to_resolution_options,
)
from services.policy import card_filter as cf

_NULL = lambda *_a, **_k: None  # noqa: E731
_ID = "disable_enforce_4o"
_TRADEOFF = "쉬는 날이 4일 이상 이어질 수 있고, 그만큼 다른 날 근무가 몰릴 수 있어요."
_ITEM = next(it for it in RELAX_CATALOG if it["id"] == _ID)


def _stub(cfg):
    """enforce_4o_hard 가 False 이면 feasible."""
    return (not bool(cfg.get("enforce_4o_hard")), {})  # 다른 완화는 효과 없음 — 이 키만 본다


def test_catalog_last_and_unique() -> None:
    ids = [it["id"] for it in RELAX_CATALOG]
    assert ids[-1] == _ID and ids.count(_ID) == 1
    assert _ITEM["family"] == "off_streak"
    assert _ITEM["label_ko"] == "OFF 4연속 금지 해제"
    assert TRADEOFF_KO[_ID] == _TRADEOFF
    assert COL_LABEL_KO["enforce_4o_hard"] == "OFF 4연속 금지"


@pytest.mark.parametrize("base,expect", [
    ({"enforce_4o_hard": True}, {"enforce_4o_hard": False}),
    ({}, {}),
    ({"enforce_4o_hard": False}, {}),
    ({"enforce_4o_hard": None}, {}),
])
def test_apply_delta_only_when_on(base, expect) -> None:
    assert _ITEM["apply"](base) == expect


def test_no_hard_lock_violation() -> None:
    assert cf.catalog_item_violations(_ITEM, {"enforce_4o_hard": True}) == []
    assert cf.catalog_item_violations(_ITEM, {}) == []


def test_probe_when_on_gives_verified_card() -> None:
    res = probe_relaxations({"enforce_4o_hard": True}, _stub, catalog=[_ITEM], try_combo=False, verify=False,
                            logger=_NULL)
    assert res["found"] and [r["id"] for r in res["resolutions"]] == [_ID]
    opts = to_resolution_options(res, {"enforce_4o_hard": True})
    assert len(opts) == 1
    o = opts[0]
    assert o["option_id"] == "probe:disable_enforce_4o" and o["verified"] is True
    assert o["apply"] == {"enforce_4o_hard": False}
    assert o["trade_off_ko"] == _TRADEOFF
    assert o["title_ko"] == "OFF 4연속 금지 해제"
    assert o["changes"] == [{"config_key": "enforce_4o_hard", "label_ko": "OFF 4연속 금지",
                             "from": True, "to": False}]


def test_probe_when_off_skips_item_without_recompute() -> None:
    calls = []

    def rf(cfg):
        calls.append(dict(cfg))
        return (False, {})

    res = probe_relaxations({"enforce_4o_hard": False}, rf, catalog=[_ITEM], try_combo=False, verify=False,
                            baseline_check=False, logger=_NULL)
    assert _ID not in [p["id"] for p in res["all_probed"]]
    assert calls == []


# 전후 표(SPEC §6-B): 새 항목을 뺀 사본('전') vs 현재('후') × 기준 꺼짐/켜짐 × 판정 풀림/안 풀림
BEFORE = [it for it in RELAX_CATALOG if it["id"] != _ID]
TABLE = [
    # (기준 설정, resolve_fn, 전 resolutions id, 후 resolutions id, 후 옵션 id)
    ({"enforce_4o_hard": False}, lambda c: (False, {}), [], [], []),
    ({"enforce_4o_hard": True}, _stub, [], [_ID], ["probe:" + _ID]),
    ({"enforce_4o_hard": True}, lambda c: (False, {}), [], [], []),
]


@pytest.mark.parametrize("base,rf,before_ids,after_ids,after_opts", TABLE)
def test_before_after_table(base, rf, before_ids, after_ids, after_opts) -> None:
    kw = dict(try_combo=False, verify=False, baseline_check=False, logger=_NULL, search_budget=40)
    before = probe_relaxations(dict(base), rf, catalog=BEFORE, **kw)
    after = probe_relaxations(dict(base), rf, catalog=RELAX_CATALOG, **kw)
    assert [r["id"] for r in before["resolutions"]] == before_ids
    assert [r["id"] for r in after["resolutions"]] == after_ids
    assert [o["option_id"] for o in to_resolution_options(after, base)] == after_opts
    if not base.get("enforce_4o_hard"):
        assert [p["id"] for p in after["all_probed"]] == [p["id"] for p in before["all_probed"]]


def test_apply_resolution_key_is_a_config_field() -> None:
    assert "enforce_4o_hard" in {f.name for f in dataclasses.fields(NurseRosterConfig)}


def test_rule_card_row_and_token_exists() -> None:
    assert RULE_CARD["enforce_4o"] == "probe:disable_enforce_4o"
    assert _ID in [it["id"] for it in RELAX_CATALOG]
