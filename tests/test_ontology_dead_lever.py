"""EI-14d — 생산자 0 인 죽은 레버 `isolated_work_penalty_weight` 의 처방(가중치 조정 액션)을 두지 않는다."""
from __future__ import annotations

from pathlib import Path

import yaml
from services.engine_contract.settings import CODE_CONSTS
from services.semantics.ontology import ConstraintOntology
from services.semantics.ontology_audit import check_I5_config_key_has_friendly_label

from tests.test_graph_fully_connected import _full_graph  # noqa: F401 — 그래프 구성 재사용

KEY = "isolated_work_penalty_weight"
YAML = Path(__file__).resolve().parents[1] / "app" / "services" / "semantics" / "ontology.yaml"


def _config_keys(node):
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "config_key":
                yield v
            yield from _config_keys(v)
    elif isinstance(node, list):
        for v in node:
            yield from _config_keys(v)


def test_yaml_has_no_config_key_for_the_dead_lever():
    data = yaml.safe_load(YAML.read_text(encoding="utf-8"))
    assert KEY not in set(_config_keys(data))
    assert data["soft_constraints"]["isolated_work"]["config_key"] is None


def test_no_isolated_work_action_node_in_graph():
    g = _full_graph()
    assert "action:soft:isolated_work" not in g.nodes
    # 같은 부류의 살아 있는 레버는 그대로 처방이 있다
    assert "action:soft:night_deviation" in g.nodes


def test_engine_reads_and_registry_stay():
    spec = next(c for c in CODE_CONSTS if c.key == KEY)
    assert len(spec.sites) == 2
    assert "처방 삭제됨" in spec.note


def test_audit_i5_passes():
    assert check_I5_config_key_has_friendly_label(ConstraintOntology()) == []

