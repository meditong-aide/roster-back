"""EF-F2 (D49) — §3.8 표 밖 같은 패턴 3곳: 키가 없을 때 '꺼짐' 대신 레지스트리(엔진) 기본.

(옛 값 → 새 값)
- constraint_impact/control.py NightRecovery 모드 추정: 두 키 모두 없음 inactive → enforced(three_nig 기본 켬).
- constraint_impact/snapshot_builders.py 프리셉티 fact follow_enabled: 속성 없음 False → True.
- ontology_graph/scope_manifest.py: preceptee_follows_preceptor 키 없음 + count_preceptee_in_staffing=False 일 때 미지원 표시 안 됨 → 표시.
키가 있으면 그 값 그대로(명시적 꺼짐은 꺼짐).
"""

from __future__ import annotations

from types import SimpleNamespace

from services.constraint_impact.control import _module_mode_estimate
from services.constraint_impact.snapshot_builders import _collect_preceptee_facts
from services.ontology_graph.scope_manifest import unmodeled_active


def _snap(cfg):
    return SimpleNamespace(config_payload=cfg)


def test_night_recovery_mode_missing_keys_follow_engine_default():
    assert _module_mode_estimate("NightRecovery", _snap({})) == "enforced"          # 옛 inactive
    assert _module_mode_estimate("NightRecovery", _snap({"two_offs_after_two_nig": False})) == "enforced"
    assert _module_mode_estimate("NightRecovery", _snap({"two_offs_after_three_nig": False})) == "inactive"
    assert _module_mode_estimate(
        "NightRecovery", _snap({"two_offs_after_two_nig": False, "two_offs_after_three_nig": False})
    ) == "inactive"
    assert _module_mode_estimate("NightRecovery", _snap({"two_offs_after_two_nig": True, "two_offs_after_three_nig": False})) == "enforced"


def _rs(config):
    nurses = [SimpleNamespace(db_id=1), SimpleNamespace(db_id=2)]
    return SimpleNamespace(
        config=config, nurses=nurses,
        preceptee_follow_days={0: {1, 2}}, preceptee_preceptor_idx={0: 1},
    )


def test_preceptee_fact_follow_enabled_missing_attr_is_engine_default():
    facts = _collect_preceptee_facts(_rs(SimpleNamespace()), {}, set())
    assert facts[0].follow_enabled is True                                          # 옛 False
    facts = _collect_preceptee_facts(_rs(SimpleNamespace(preceptee_follows_preceptor=False)), {}, set())
    assert facts[0].follow_enabled is False                                         # 명시적 꺼짐 유지


def test_scope_manifest_preceptee_missing_key_is_engine_default():
    assert "config.count_preceptee_in_staffing=False" in unmodeled_active([], {"count_preceptee_in_staffing": False})  # 옛 표시 없음
    assert "config.count_preceptee_in_staffing=False" not in unmodeled_active(
        [], {"preceptee_follows_preceptor": False, "count_preceptee_in_staffing": False}
    )
    assert "config.count_preceptee_in_staffing=False" not in unmodeled_active([], {})


def test_registry_default_called_with_preceptee_follows_preceptor_key(monkeypatch):
    """같은 기본값(True)의 다른 키(count_preceptee_in_staffing)로 바뀌면 잡히도록, 키별로 값을 다르게 돌려준다."""
    from services.constraint_impact import snapshot_builders as sb
    from services.ontology_graph import scope_manifest as sm

    fake = lambda k: k != "preceptee_follows_preceptor"  # noqa: E731  (preceptee_follows_preceptor 만 False)
    monkeypatch.setattr(sb, "registry_default", fake)
    monkeypatch.setattr(sm, "registry_default", fake)
    assert sb._collect_preceptee_facts(_rs(SimpleNamespace()), {}, set())[0].follow_enabled is False
    assert "config.count_preceptee_in_staffing=False" not in sm.unmodeled_active([], {"count_preceptee_in_staffing": False})


def test_base_constraint_modes_missing_recovery_attrs_follow_engine_default():
    from services.constraint_impact.snapshot_builders import _base_constraint_modes

    rs = SimpleNamespace(config=SimpleNamespace(), grade_strategy="COMBINED")
    keys = {f.key for f in _base_constraint_modes(rs, SimpleNamespace())}
    assert "two_offs_after_three_nig" in keys and "two_offs_after_two_nig" not in keys
    rs = SimpleNamespace(config=SimpleNamespace(two_offs_after_three_nig=False, two_offs_after_two_nig=True), grade_strategy="COMBINED")
    keys = {f.key for f in _base_constraint_modes(rs, SimpleNamespace())}
    assert "two_offs_after_two_nig" in keys and "two_offs_after_three_nig" not in keys
