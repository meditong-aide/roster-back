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
        name: {
            "subs": sorted(meta["subs"]),
            "hn_only": bool(meta["hn_only"]),
            "adm_only": bool(meta.get("adm_only", False)),
        }
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


def test_adm_only_is_narrower_than_hn_only():
    """adm_only 타깃은 hn_only 도 True 여야 한다 — 게이트가 역전되면 HN 이 통과한다."""
    for name, meta in NAVIGATE_TARGETS.items():
        if meta.get("adm_only"):
            assert meta["hn_only"], f"{name}: adm_only 인데 hn_only=False (게이트 역전)"


def test_mworks_register_blocks_head_nurse():
    """mWorks 등록 화면은 마스터 관리자 전용 — 프론트 Nav 의 is_master_admin 게이팅과 일치.

    hn_only 만으로는 HN 이 통과하므로 adm_only 가 실제로 막는지 확인한다.
    """
    from types import SimpleNamespace

    from agents_v2.skills.client_actions import target_permission_error

    hn = SimpleNamespace(user_role="HN")
    adm = SimpleNamespace(user_role="ADM")
    rn = SimpleNamespace(user_role="RN")

    assert target_permission_error("mworks_register", hn) is not None
    assert target_permission_error("mworks_register", rn) is not None
    assert target_permission_error("mworks_register", adm) is None
    # 대조군: 일반 HN 전용 화면은 HN 이 통과해야 한다(과차단 방지).
    assert target_permission_error("nurse_management", hn) is None
