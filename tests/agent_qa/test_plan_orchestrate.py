"""오케스트레이션 e2e — 실제 planner + mock skills + 실제 joiner (라이브)."""
import os
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[2] / ".env")
import pytest
from agents_v2.planning.orchestrate import try_plan_run, join_answer
from agents_v2.skills.descriptions import SKILL_TOOLS
from agents_v2.llm_client import get_llm_client

pytestmark = pytest.mark.skipif(not os.getenv("OPENAI_API_KEY"), reason="라이브")


def _mock_skills(db, skill, args, ctx):
    # recommend → 후보, mutation → preview
    if skill == "recommend_candidates":
        return {"candidates": [{"name": "이수정", "score": 0.9}, {"name": "박지은"}]}
    if skill in ("bulk_mutation", "manage_assignment"):
        return {"preview": True, "summary": {"nurse": args.get("nurse"), "action": "assign"}}
    return {"ok": True}


def test_dependent_compound_full_pipeline():
    llm = get_llm_client("openai")
    run = try_plan_run(None, "5월 3일 나이트 대체자 찾아서 그 사람으로 배정해줘",
                       None, llm, SKILL_TOOLS, _mock_skills)
    assert run is not None, "plan 안 생김"
    assert len(run.plan.tasks) >= 2 and run.failed is False
    # mutate 는 dry-run → 승인 대상, 그리고 $t1 참조가 실제 후보로 치환됐는지
    assert run.needs_approval is True
    mutate_args = run.exec.previews[0]["args"]
    # $t1 참조가 후보로 치환됐는지 — 플래너가 고르는 키명(nurse/nurse_name)은 런마다 다를 수 있어 값으로 검증.
    assert "이수정" in mutate_args.values(), f"참조 치환 실패: {mutate_args}"
    # Joiner: 결과 자연어 합성
    ans = join_answer(llm, "5월 3일 나이트 대체자 찾아서 배정해줘", run)
    assert ans and ("이수정" in ans or "배정" in ans)
    print("\nPLAN:", [(t.id, t.skill, t.kind) for t in run.plan.tasks])
    print("ANSWER:", ans[:120])


def test_simple_returns_none():
    llm = get_llm_client("openai")
    assert try_plan_run(None, "5월 근무표 보여줘", None, llm, SKILL_TOOLS, _mock_skills) is None
