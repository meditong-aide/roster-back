"""EI-13b 시험 — 생성 실패 뒤 근무표 행 삭제 commit 이 실패(rollback)해도 해결책 탐색이 롤백 없는 런과 같은 입력으로 돈다.

예전에는 rollback 이 ORM 을 만료시켜 간호사 `__dict__` 오버레이(등급·주말휴무·월 한도·팀)와 ShiftManage `__dict__` 가 사라졌고,
`_overlay_lost` 가 탐색을 건너뛰었다(해결 카드 없음). 지금은 실패 블록 머리에서 commit 전에 입력을 평범한 사본으로 묶는다.

강제 rollback = `Session.commit` 대역 — 삭제 대기 중인 `Schedule` 이 있는 commit(실패 블록의 행 삭제 commit 하나뿐)만 예외를 낸다.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from .backend_check import assert_sqlite_backends
from .golden import build_record
from .harness import run_with_output
from .ladder import drain_live_graph_dirs
from .scenario_ladder import LADDER_SCENARIOS

RCS_PATH = Path(__file__).resolve().parents[2] / "app" / "services" / "roster_create_service.py"
NAMES = ("S1-L", "S1-LT")
SEARCH_SITES = ("undiag_probe", "weekend_multi", "weekend_single", "night_limit_single")
FORCED = {"count": 0}


def _force_rollback(mp: pytest.MonkeyPatch) -> None:
    """실패 블록의 행 삭제 commit 만 실패시킨다(그 외 commit 은 원본)."""
    from sqlalchemy.orm import Session

    original = Session.commit
    FORCED["count"] = 0

    def commit(self):
        if any(type(obj).__name__ == "Schedule" for obj in self.deleted):
            FORCED["count"] += 1
            raise RuntimeError("forced commit failure (EI-13b)")
        return original(self)

    mp.setattr(Session, "commit", commit)


def _run(engine, name: str, *, rollback: bool):
    scenario = LADDER_SCENARIOS[name]
    if rollback:
        scenario = dataclasses.replace(scenario, patches=scenario.patches + (_force_rollback,))
    FORCED["count"] = 0
    run = run_with_output(engine, scenario)
    forced = FORCED["count"]
    return build_record(scenario, run), run.stdout, forced


@pytest.fixture(scope="module")
def runs(engine, engine_input_tables):
    assert_sqlite_backends(engine)
    out = {}
    for name in NAMES:
        out[(name, False)] = _run(engine, name, rollback=False)
        out[(name, True)] = _run(engine, name, rollback=True)
    drain_live_graph_dirs()
    return out


def _attempts(record: dict) -> list[dict]:
    return record["layers"]["attempts"]


def _sites(record: dict) -> list[str]:
    return [a["site"] for a in _attempts(record)]


def _options(record: dict) -> list:
    inf = ((record.get("http_detail") or {}).get("infeasibility") or {})
    return [(o.get("title_ko"), o.get("verified")) for o in (inf.get("resolution_options") or [])]


@pytest.mark.parametrize("name", NAMES)
def test_forced_rollback_actually_happened(name, runs):
    _rec, stdout, forced = runs[(name, True)]
    assert forced == 1, "실패 블록의 행 삭제 commit 이 정확히 한 번 강제 실패해야 한다"
    assert runs[(name, False)][2] == 0
    assert "[UndiagProbe] 간호사 오버레이 소실" not in stdout


@pytest.mark.parametrize("name", NAMES)
def test_rollback_keeps_probe_site_distribution(name, runs):
    """탐색 자리 분포(자리별 시도 수)가 롤백 없는 런과 같고, 탐색이 실제로 돌았다."""
    base, rolled = runs[(name, False)][0], runs[(name, True)][0]
    assert _sites(rolled) == _sites(base)
    for site in SEARCH_SITES:
        assert _sites(base).count(site) >= 1, (name, site)


@pytest.mark.parametrize("name", NAMES)
def test_rollback_keeps_probe_attempt_inputs(name, runs):
    """탐색 시도들의 L1 config 변화(delta)가 롤백 없는 런과 같다."""
    base, rolled = runs[(name, False)][0], runs[(name, True)][0]
    for site in SEARCH_SITES:
        base_deltas = [a["delta"] for a in _attempts(base) if a["site"] == site]
        rolled_deltas = [a["delta"] for a in _attempts(rolled) if a["site"] == site]
        assert rolled_deltas == base_deltas, (name, site)


@pytest.mark.parametrize("name", NAMES)
def test_rollback_keeps_resolution_cards(name, runs):
    """해결 카드가 롤백 없는 런과 같다(전엔 롤백이면 카드가 없었다)."""
    base, rolled = runs[(name, False)][0], runs[(name, True)][0]
    assert _options(rolled) == _options(base)
    assert _options(base), "비교가 헛돌지 않으려면 롤백 없는 런에 카드가 있어야 한다"


def test_s1lt_search_sites_keep_treatment_free_config(runs):
    """S1-LT — 세 탐색 자리 config 가 롤백 없는 런과 같다(treatment·차감·누적이 빠진 `latest_config` 값 — EI-16 결함 보존)."""
    base, rolled = runs[("S1-LT", False)][0], runs[("S1-LT", True)][0]
    for site in ("weekend_multi", "weekend_single", "night_limit_single"):
        assert [a["delta"] for a in _attempts(rolled) if a["site"] == site] == \
               [a["delta"] for a in _attempts(base) if a["site"] == site], site


def test_overlay_lost_branch_is_gone():
    """정적 — 생성 서비스에 `_overlay_lost` 낱말이 없다."""
    assert "_overlay_lost" not in RCS_PATH.read_text(encoding="utf-8")
