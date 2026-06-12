"""Unified ontology graph — 4 first-class node kinds (constraint / domain_object / state / action).

Replaces the split between ``constraint_impact/graph_nodes.py`` (runtime typed graph)
and ``semantics/ontology.yaml`` (diagnostic ontology). See
``docs/UNIFIED_ONTOLOGY_GRAPH_SCHEMA.md`` for the schema contract.
"""

from services.ontology_graph.schema import (
    ActionNode,
    ConstraintNode,
    DomainObjectNode,
    Edge,
    GraphValidationError,
    Node,
    OntologyGraph,
    StateNode,
)

__all__ = [
    "ActionNode",
    "ConstraintNode",
    "DomainObjectNode",
    "Edge",
    "GraphValidationError",
    "Node",
    "OntologyGraph",
    "StateNode",
]
