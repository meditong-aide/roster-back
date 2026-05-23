"""update-person-attr skill — modify nurse attributes (single or multi-field)."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from agents_v2.skills.registry import register
from agents_v2.tools import nurse_tools
from agents_v2.tools.nurse_tools import compute_batch_changeset


def _extract_mutations(params: dict) -> list[dict]:
    """params에서 mutations 배열을 추출. 단일 field/value도 mutations로 정규화.

    LLM/grounding 변형 모두 수용:
      - {mutations: [{field, value}, ...]}                 # 권장 (다중)
      - {field, value}                                     # 단일 (backward-compat)
      - {mutation: {target_field, target_value}}           # 구식 grounding
    """
    raw = params.get("mutations")
    if isinstance(raw, list) and raw:
        return [
            {"field": m.get("field") or m.get("target_field"),
             "value": m.get("value") if "value" in m else m.get("target_value")}
            for m in raw
        ]
    nested = params.get("mutation") or {}
    field = params.get("field") or nested.get("target_field")
    if field:
        value = params.get("value") if params.get("value") is not None else nested.get("target_value")
        return [{"field": field, "value": value}]
    return []


@register("update-person-attr")
def update_person_attr(db: Session, params: dict) -> Any:
    """Update one or more nurse attributes (transactional per nurse)."""
    nurse_ids = params.get("nurse_ids", [])
    if not nurse_ids:
        return {"error": "nurse_id required"}

    group_id = params.get("group_id")
    if not group_id:
        return {"error": "group_id required (RBAC scope)"}

    mutations = _extract_mutations(params)
    if not mutations:
        return {"error": "mutations (or field+value) required"}

    preview_only = params.get("preview_only", False)

    results = []
    for nid in nurse_ids:
        if preview_only:
            cs = compute_batch_changeset(db, nid, group_id, mutations)
            if not cs.get("ok"):
                results.append({"nurse_id": nid, **{k: v for k, v in cs.items() if k != "ok"}})
                continue
            preview = {
                "preview": True,
                "nurse_id": nid,
                "applied_mutations": [
                    {
                        "field": m["field"],
                        "current_value": cs["pre_summary"].get(m["field"]),
                        "new_value": m["value"],
                    }
                    for m in cs["normalized_mutations"]
                ],
                "changed_fields": cs["changed_fields"],
            }
            if cs["coupled_log"]:
                preview["coupled_changes"] = cs["coupled_log"]
            results.append(preview)
        else:
            result = nurse_tools.update_nurse_attributes_batch(db, nid, group_id, mutations)
            results.append(result)

    # 단일 간호사 + 단일 mutation이면 평탄화
    if len(results) == 1:
        return results[0]
    return {"affected_count": len(results), "results": results}
