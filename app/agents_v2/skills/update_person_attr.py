"""update-person-attr skill — modify nurse attributes."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from agents_v2.skills.registry import register
from agents_v2.tools import nurse_tools


@register("update-person-attr")
def update_person_attr(db: Session, params: dict) -> Any:
    """Update a nurse's attribute."""
    nurse_ids = params.get("nurse_ids", [])
    mutation = params.get("mutation", {})
    attribute = mutation.get("target_field", "")
    value = mutation.get("target_value")
    preview_only = params.get("preview_only", False)

    if not nurse_ids:
        return {"error": "nurse_id required"}
    if not attribute:
        return {"error": "target_field (attribute name) required"}

    results = []
    for nid in nurse_ids:
        if preview_only:
            current = nurse_tools.get_nurse_by_id(db, nid)
            results.append({
                "preview": True,
                "nurse_id": nid,
                "attribute": attribute,
                "current_value": current.get(attribute) if current else None,
                "new_value": value,
            })
        else:
            result = nurse_tools.update_nurse_attribute(db, nid, attribute, value)
            results.append(result)

    if len(results) == 1:
        return results[0]
    return {"affected_count": len(results), "results": results}
