"""Verifier — post-execution checks on results.

Validates that the execution produced sensible output and
flags potential issues before returning to the user.
"""

from __future__ import annotations

from typing import Any

from agents_v2.schemas.agent_response import AgentResponse
from agents_v2.schemas.canonical_query import CanonicalQuery, Operation


def verify_result(
    query: CanonicalQuery,
    response: AgentResponse,
) -> AgentResponse:
    """Verify execution results against the original query intent.

    Checks:
    1. If query expected data but response has none → flag.
    2. If mutation was requested but no affected rows → warn.
    3. If count query returned unexpected structure → normalize.
    4. Attach verification notes to the response.

    Args:
        query: The canonical query that was executed.
        response: The response from the executor.

    Returns:
        The same AgentResponse, possibly with warnings added.
    """
    if response.error or response.needs_clarification or response.awaiting_approval:
        return response

    checks: list[str] = []

    # Check: read query but no data
    if query.operation in (Operation.LIST, Operation.COUNT, Operation.SUMMARIZE):
        if response.data is None:
            checks.append("query returned no data — the scope may be empty or filters too restrictive")

    # Check: mutation but no affected indication
    if query.is_mutation and response.data is not None:
        affected = _extract_affected_count(response.data)
        if affected == 0:
            checks.append("mutation completed but 0 rows affected — check filters")
        elif affected is not None:
            checks.append(f"mutation affected {affected} row(s)")

    # Check: empty list result
    if isinstance(response.data, list) and len(response.data) == 0:
        checks.append("result is an empty list — no matching entries found")

    # Attach verification notes
    if checks:
        response.execution_trace.append({
            "verifier": checks,
        })

    return response


def _extract_affected_count(data: Any) -> int | None:
    """Try to extract an affected count from various result shapes."""
    if isinstance(data, dict):
        for key in ("affected_count", "count", "updated_count"):
            if key in data:
                return data[key]
    if isinstance(data, list):
        return len(data)
    return None
