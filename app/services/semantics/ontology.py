from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


@dataclass(slots=True)
class OntologyConstraintEntry:
    constraint_id: str
    label: str
    parent: str
    scope: list[str]
    effective_modes: list[str]
    connects: list[str]
    explanation_template: str
    produces: list[str] = field(default_factory=list)
    consumes: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    default_severity: str | None = None
    notes: str | None = None


@dataclass(slots=True)
class OntologyOverrideEntry:
    override_id: str
    label: str
    parent: str
    bypasses: list[str]
    does_not_bypass: list[str]
    conditional_effects: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class OntologyModeEntry:
    mode_id: str
    label: str
    meaning: str
    is_enforced: bool
    severity: str


class ConstraintOntology:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else Path(__file__).with_name("ontology.yaml")
        self.version = 0
        self.constraints: dict[str, OntologyConstraintEntry] = {}
        self.overrides: dict[str, OntologyOverrideEntry] = {}
        self.modes: dict[str, OntologyModeEntry] = {}
        self.relations: dict[str, dict[str, Any]] = {}
        self._alias_to_id: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        self.version = int(raw.get("version") or 0)
        for cid, body in (raw.get("constraints") or {}).items():
            entry = OntologyConstraintEntry(
                constraint_id=cid,
                label=str(body.get("label") or cid),
                parent=str(body.get("parent") or ""),
                scope=list(body.get("scope") or []),
                effective_modes=list(body.get("effective_modes") or []),
                connects=list(body.get("connects") or []),
                explanation_template=str(body.get("explanation_template") or ""),
                produces=list(body.get("produces") or []),
                consumes=list(body.get("consumes") or []),
                aliases=list(body.get("aliases") or []),
                default_severity=body.get("default_severity"),
                notes=body.get("notes"),
            )
            self.constraints[cid] = entry
            self._alias_to_id[cid.upper()] = cid
            for alias in entry.aliases:
                self._alias_to_id[str(alias).upper()] = cid
        for oid, body in (raw.get("overrides") or {}).items():
            self.overrides[oid] = OntologyOverrideEntry(
                override_id=oid,
                label=str(body.get("label") or oid),
                parent=str(body.get("parent") or ""),
                bypasses=list(body.get("bypasses") or []),
                does_not_bypass=list(body.get("does_not_bypass") or []),
                conditional_effects=dict(body.get("conditional_effects") or {}),
            )
        for mid, body in (raw.get("modes") or {}).items():
            self.modes[mid] = OntologyModeEntry(
                mode_id=mid,
                label=str(body.get("label") or mid),
                meaning=str(body.get("meaning") or ""),
                is_enforced=bool(body.get("is_enforced", False)),
                severity=str(body.get("severity") or "none"),
            )
        self.relations = dict(raw.get("relations") or {})

    def get_constraint(self, family: str) -> OntologyConstraintEntry | None:
        resolved = self.resolve_alias(family)
        return self.constraints.get(resolved) if resolved else None

    def get_mode(self, mode_id: str) -> OntologyModeEntry | None:
        return self.modes.get(str(mode_id or ""))

    def get_override(self, override_id: str) -> OntologyOverrideEntry | None:
        return self.overrides.get(str(override_id or ""))

    def get_parent(self, family: str) -> str | None:
        entry = self.get_constraint(family)
        return entry.parent if entry else None

    def get_scope(self, family: str) -> list[str]:
        entry = self.get_constraint(family)
        return list(entry.scope) if entry else []

    def get_template(self, family: str) -> str | None:
        entry = self.get_constraint(family)
        return entry.explanation_template if entry else None

    def get_default_severity(self, family: str) -> str | None:
        entry = self.get_constraint(family)
        return entry.default_severity if entry else None

    def resolve_alias(self, raw: str | None) -> str | None:
        if raw is None:
            return None
        return self._alias_to_id.get(str(raw).strip().upper())

    def can_bypass(self, override_type: str, family: str) -> bool | None:
        ov = self.get_override(override_type)
        target = self.resolve_alias(family)
        if ov is None or target is None:
            return None
        if target in ov.bypasses:
            return True
        if target in ov.does_not_bypass:
            return False
        return None


@lru_cache(maxsize=1)
def get_default_ontology() -> ConstraintOntology:
    return ConstraintOntology()
