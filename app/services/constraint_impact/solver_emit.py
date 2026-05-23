"""Phase B: solver-attach-point emit recorder.

cp_sat_basic.py 의 m.Add(...) 호출 옆에서 부수효과로 어떤 hard constraint 가
실제로 추가되었는지 기록한다. 솔버 동작은 변경하지 않으며, 기록은 사후에
roster_system 으로 노출되어 graph drift 검증에 쓰인다.

이 레이어가 충분히 안정화되면 Phase A (rule_compiler 결과를 솔버가 직접
소비) 로 feature 단위로 이행한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class EmittedConstraint:
    family: str
    scope: dict[str, Any]
    target: Any
    mode: str = "enforced"
    related_atom_keys: list[tuple[int, int]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "scope": dict(self.scope),
            "target": self.target,
            "mode": self.mode,
            "related_atom_keys": [list(t) for t in self.related_atom_keys],
            "metadata": dict(self.metadata),
        }


class ConstraintEmitRecorder:
    __slots__ = ("_records",)

    def __init__(self) -> None:
        self._records: list[EmittedConstraint] = []

    def emit(
        self,
        *,
        family: str,
        scope: dict[str, Any],
        target: Any,
        mode: str = "enforced",
        related_atom_keys: list[tuple[int, int]] | None = None,
        metadata: dict[str, Any] | None = None,
        **extra_metadata: Any,
    ) -> None:
        merged: dict[str, Any] = {}
        if metadata:
            merged.update(metadata)
        if extra_metadata:
            merged.update(extra_metadata)
        self._records.append(
            EmittedConstraint(
                family=family,
                scope=dict(scope),
                target=target,
                mode=mode,
                related_atom_keys=list(related_atom_keys or []),
                metadata=merged,
            )
        )

    def records(self) -> list[EmittedConstraint]:
        return list(self._records)

    def by_family(self, family: str) -> list[EmittedConstraint]:
        return [r for r in self._records if r.family == family]

    def count(self) -> int:
        return len(self._records)


def get_or_attach_recorder(rs: Any) -> ConstraintEmitRecorder:
    """Return the recorder attached to `rs`, attaching a fresh one if absent.

    rs is the RosterSystem (cp_sat_basic). Attach via setattr so the same
    instance is reused throughout one solve attempt.
    """
    rec = getattr(rs, "_constraint_impact_solver_emit_recorder", None)
    if isinstance(rec, ConstraintEmitRecorder):
        return rec
    rec = ConstraintEmitRecorder()
    try:
        setattr(rs, "_constraint_impact_solver_emit_recorder", rec)
    except Exception:
        # rs is some object that doesn't allow setattr; return the
        # fresh recorder anyway so the caller can still record locally.
        pass
    return rec


__all__ = [
    "ConstraintEmitRecorder",
    "EmittedConstraint",
    "get_or_attach_recorder",
]
