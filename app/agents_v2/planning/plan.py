"""Plan 아티팩트 + 결정적 오케스트레이션 코어 (Fetcher).

LLMCompiler 의 [2] Fetcher = 위상정렬 + `$tN.field` 참조 치환. LLM 무관·결정적이라
단위 테스트로 고정한다. Planner(LLM)·Executor 는 별도 모듈.

설계: docs/AGENT_DAG_PLANNING_DESIGN.md §2·§3.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PlanTask:
    id: str
    skill: str
    args: dict = field(default_factory=dict)
    deps: list[str] = field(default_factory=list)
    kind: str = "read"  # "read" | "mutate"


@dataclass
class Plan:
    tasks: list[PlanTask] = field(default_factory=list)

    def by_id(self) -> dict[str, PlanTask]:
        return {t.id: t for t in self.tasks}

    def to_dict(self) -> dict:
        return {"tasks": [{"id": t.id, "skill": t.skill, "args": t.args,
                           "deps": t.deps, "kind": t.kind} for t in self.tasks]}

    @staticmethod
    def from_dict(d: dict) -> "Plan":
        return Plan(tasks=[PlanTask(
            id=t["id"], skill=t["skill"], args=t.get("args") or {},
            deps=t.get("deps") or [], kind=t.get("kind", "read"),
        ) for t in (d or {}).get("tasks", [])])

    def validate(self) -> None:
        """중복 id·미존재 dep·순환을 검출(있으면 ValueError)."""
        ids = [t.id for t in self.tasks]
        if len(ids) != len(set(ids)):
            raise ValueError("plan: 중복 task id")
        idset = set(ids)
        for t in self.tasks:
            for d in t.deps:
                if d not in idset:
                    raise ValueError(f"plan: task '{t.id}' 의 dep '{d}' 없음")
            if t.kind not in ("read", "mutate"):
                raise ValueError(f"plan: task '{t.id}' kind 부정({t.kind})")
        self.topo_levels()  # 순환이면 여기서 ValueError

    def topo_levels(self) -> list[list[PlanTask]]:
        """Kahn 위상정렬 — 의존 없는 것부터 '레벨' 단위(각 레벨은 병렬 가능)로 반환.

        순환이면 ValueError.
        """
        by = self.by_id()
        indeg = {t.id: len(set(t.deps)) for t in self.tasks}
        remaining = set(indeg)
        levels: list[list[PlanTask]] = []
        while remaining:
            ready = sorted(tid for tid in remaining if indeg[tid] == 0)
            if not ready:
                raise ValueError("plan: 순환 의존(cycle)")
            levels.append([by[tid] for tid in ready])
            for tid in ready:
                remaining.discard(tid)
                for t in self.tasks:  # tid 를 dep 으로 갖는 task 의 indegree 감소
                    if tid in t.deps and t.id in remaining:
                        indeg[t.id] -= 1
        return levels


# ── $tN.field 참조 치환 ──────────────────────────────────────
_REF_RE = re.compile(r"^\$([a-zA-Z_][\w]*)((?:\.[\w]+|\[\d+\])*)$")
_SEG_RE = re.compile(r"\.([\w]+)|\[(\d+)\]")


def _dig(obj: Any, path: str) -> Any:
    """'.candidates[0].name' 같은 경로로 obj 를 파고든다. 실패 시 None."""
    cur = obj
    for m in _SEG_RE.finditer(path):
        key, idx = m.group(1), m.group(2)
        try:
            if key is not None:
                cur = cur[key] if isinstance(cur, dict) else getattr(cur, key)
            else:
                cur = cur[int(idx)]
        except (KeyError, IndexError, TypeError, AttributeError):
            return None
    return cur


def resolve_refs(value: Any, outputs: dict[str, Any]) -> Any:
    """value 안의 '$tN.path' 참조를 outputs[tN] 로 치환(중첩 dict/list 재귀).

    참조가 아니면 원값 그대로. 참조 대상 task 출력이 없으면 None.
    """
    if isinstance(value, str):
        m = _REF_RE.match(value.strip())
        if not m:
            return value
        tid, path = m.group(1), m.group(2)
        if tid not in outputs:
            return None
        return _dig(outputs[tid], path) if path else outputs[tid]
    if isinstance(value, dict):
        return {k: resolve_refs(v, outputs) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_refs(v, outputs) for v in value]
    return value


def resolve_args(args: dict, outputs: dict[str, Any]) -> dict:
    """task.args 전체의 참조를 치환."""
    return {k: resolve_refs(v, outputs) for k, v in (args or {}).items()}
