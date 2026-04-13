"""Main agent orchestrator — the grounding-first pipeline.

Pipeline: classify → ground → canonicalize → plan → execute → verify → respond
"""

from __future__ import annotations

from typing import Any, Callable

from sqlalchemy.orm import Session

from agents_v2.schemas.agent_response import AgentResponse
from agents_v2.schemas.grounded_concept import ConceptType, GroundedConcept
from agents_v2.core.concept_typer import classify_concepts, TypedFragment
from agents_v2.grounding.dispatcher import dispatch_grounding
from agents_v2.core.canonicalizer import build_canonical_query
from agents_v2.core.planner import build_execution_plan
from agents_v2.core.executor import execute_plan
from agents_v2.core.verifier import verify_result
from agents_v2.skills import run_skill
from agents_v2.harness.agents_loader import load_agents_prompt
from agents_v2.harness.topic_selector import load_selected_topics
from agents_v2.harness.skill_matcher import get_skill_descriptions


LLMCall = Callable[[str, str], str]
"""(system_prompt, user_prompt) → response string"""


class SchedulingAgent:
    """Grounding-first scheduling agent.

    Usage:
        agent = SchedulingAgent(llm_call=my_llm_func)
        response = agent.run(db, message, session_context)
    """

    def __init__(self, llm_call: LLMCall):
        self.llm_call = llm_call

    def run(
        self,
        db: Session,
        user_message: str,
        session_context: dict,
    ) -> AgentResponse:
        """Execute the full grounding-first pipeline.

        Args:
            db: Database session.
            user_message: Raw user input.
            session_context: Dict with group_id, year, month, user_role, office_id.

        Returns:
            AgentResponse with answer, data, or clarification request.
        """
        group_id = session_context.get("group_id", "")

        try:
            # Stage 1: Concept typing (LLM classifies fragments)
            fragments = classify_concepts(
                user_message, self.llm_call, session_context=session_context,
            )

            if not fragments:
                return AgentResponse(
                    answer="요청을 이해하지 못했습니다. 좀 더 구체적으로 말씀해 주세요.",
                    needs_clarification=True,
                )

            # Stage 2: Grounding (each fragment resolved via DB tools)
            grounded: list[GroundedConcept] = []
            for frag in fragments:
                gc = dispatch_grounding(
                    db, group_id, frag.concept_type, frag.text,
                    **_grounding_kwargs(frag),
                )
                grounded.append(gc)

            # Check for clarification needs
            clarifications = [g for g in grounded if g.requires_clarification]
            if clarifications:
                questions = [g.clarification_question for g in clarifications if g.clarification_question]
                return AgentResponse(
                    needs_clarification=True,
                    clarification_question="\n".join(questions),
                    grounding_trace=[g.to_dict() for g in grounded],
                )

            # Stage 3: Canonicalize (assemble grounded concepts into query)
            canonical = build_canonical_query(
                grounded, session_context, self.llm_call,
                user_message=user_message,
            )

            # Stage 4: Plan (map query to execution steps)
            plan = build_execution_plan(canonical)

            if plan.is_blocked:
                return AgentResponse(
                    needs_clarification=True,
                    clarification_question=plan.pending_clarification,
                    grounding_trace=[g.to_dict() for g in grounded],
                )

            # Stage 5: Execute (run skills)
            response = execute_plan(db, plan, run_skill, llm_call=self.llm_call)

            # Stage 6: Verify
            response = verify_result(canonical, response)

            # Stage 7: Generate natural language answer
            if not response.answer and response.data is not None:
                response.answer = self._generate_answer(
                    user_message, canonical, response, session_context,
                )

            # Attach grounding trace
            response.grounding_trace = [g.to_dict() for g in grounded]

            return response

        except Exception as exc:
            return AgentResponse(error=f"Agent error: {str(exc)}")

    def _generate_answer(
        self,
        user_message: str,
        canonical,
        response: AgentResponse,
        session_context: dict,
    ) -> str:
        """Use LLM to generate a natural language answer from structured data."""
        import json

        # Build system prompt with AGENTS.md
        system = load_agents_prompt(
            year=session_context.get("year"),
            month=session_context.get("month"),
            group_id=session_context.get("group_id"),
            user_role=session_context.get("user_role"),
        )

        # Truncate data for LLM context
        data_str = json.dumps(response.data, ensure_ascii=False, default=str)
        if len(data_str) > 4000:
            data_str = data_str[:4000] + "... (truncated)"

        user_prompt = (
            f"사용자 질문: \"{user_message}\"\n\n"
            f"쿼리: scope={canonical.scope}, operation={canonical.operation}\n\n"
            f"실행 결과 데이터:\n{data_str}\n\n"
            f"위 데이터를 바탕으로 사용자에게 자연스럽고 간결한 한국어 답변을 작성하세요. "
            f"표 형태가 적절하면 마크다운 표를 사용하세요."
        )

        try:
            return self.llm_call(system, user_prompt)
        except Exception:
            return ""

    def build_system_prompt(self, session_context: dict, fragments: list[TypedFragment] | None = None) -> str:
        """Build the full system prompt with AGENTS.md + relevant topics.

        Useful for external callers who need the assembled prompt.
        """
        base = load_agents_prompt(
            year=session_context.get("year"),
            month=session_context.get("month"),
            group_id=session_context.get("group_id"),
            user_role=session_context.get("user_role"),
        )

        # Load relevant topics
        concept_types = [f.concept_type for f in fragments] if fragments else None
        topics = load_selected_topics(concept_types=concept_types)
        if topics:
            topic_text = "\n\n".join(f"## {name}\n{content}" for name, content in topics.items())
            base += f"\n\n# Reference Topics\n{topic_text}"

        # Add skill descriptions for planner context
        skills = get_skill_descriptions()
        if skills:
            skill_text = "\n".join(f"- **{s['name']}**: {s['description']}" for s in skills)
            base += f"\n\n# Available Skills\n{skill_text}"

        return base


def _grounding_kwargs(frag: TypedFragment) -> dict:
    """Extract extra kwargs for the grounder based on fragment metadata."""
    kwargs = {}
    if frag.concept_type == ConceptType.STATUS:
        kwargs["scope_hint"] = frag.metadata.get("scope_hint")
    elif frag.concept_type == ConceptType.ENTITY:
        kwargs["entity_type_hint"] = frag.metadata.get("entity_type_hint")
    return kwargs
