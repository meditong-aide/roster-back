"""client_action_targets.json snapshot 이 현행 NAVIGATE_TARGETS 와 일치하는지 검증.

dev 가 NAVIGATE_TARGETS 를 수정하고 dump 스크립트 재실행을 잊으면 이 테스트가 실패.
실패 시 안내: `uv run python scripts/dump_client_action_targets.py`
"""
from __future__ import annotations

import json
from pathlib import Path

from agents_v2.skills.client_actions import NAVIGATE_TARGETS

_BACKEND_JSON = (
    Path(__file__).resolve().parents[2]
    / "app" / "agents_v2" / "contract" / "client_action_targets.json"
)


def _expected_targets() -> dict:
    return {
        name: {"subs": sorted(meta["subs"]), "hn_only": bool(meta["hn_only"])}
        for name, meta in NAVIGATE_TARGETS.items()
    }


def test_backend_snapshot_matches_navigate_targets():
    assert _BACKEND_JSON.exists(), (
        f"snapshot not found: {_BACKEND_JSON}. "
        "Run: uv run python scripts/dump_client_action_targets.py"
    )
    snap = json.loads(_BACKEND_JSON.read_text(encoding="utf-8"))
    snap_targets = snap.get("targets", {})
    expected = _expected_targets()
    assert snap_targets == expected, (
        "client_action_targets.json snapshot drift detected. "
        "Run: uv run python scripts/dump_client_action_targets.py"
    )


def test_all_targets_have_canonical_metadata():
    """NAVIGATE_TARGETS entries 모두 subs/hn_only 키 보유 — 구조 회귀 차단."""
    for name, meta in NAVIGATE_TARGETS.items():
        assert "subs" in meta, f"{name}: 'subs' 키 누락"
        assert "hn_only" in meta, f"{name}: 'hn_only' 키 누락"
        assert isinstance(meta["subs"], (set, frozenset)), f"{name}: subs 가 set 이 아님"
