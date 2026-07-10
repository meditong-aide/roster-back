"""파라미터 근거 평가 하니스 (라이브) — 계획대로 5 outcome 채점.

축: router recall / selection / slot fill / behavior match. 카테고리·전체 집계 + 실패상세.
    python -m tests.agent_qa.param_eval [--limit N]
"""
import sys, os, json, argparse
from pathlib import Path
from collections import defaultdict

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "app")); sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "agent_qa" / "corpus"))
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from agents_v2.harness.prompt_builder import build_system_prompt
from agents_v2.schemas.session_context import SessionContext
from agents_v2.skills.descriptions import SKILL_TOOLS
from agents_v2.llm_client import get_llm_client, get_router_llm_client
from agents_v2.router import route
from agents_v2.agent_v3 import _wrap_untrusted_tool_output
from queries_params import PARAM_QUERIES

_ABSTAIN_KW = ["직접 처리", "할 수 없", "없습니다", "제공하지", "지원하지", "불가능", "기능이 없"]
_CLARIFY_KW = ["?", "인가요", "알려주", "어느", "무엇", "몇 ", "누구", "선택", "무엇을", "어떤"]


def classify_text(txt: str) -> str:
    t = txt or ""
    if any(k in t for k in _ABSTAIN_KW):
        return "abstain"
    if any(k in t for k in _CLARIFY_KW):
        return "clarify"
    return "text_other"


def slot_ok(expected: dict, args: dict) -> bool:
    if not expected:
        return True
    args = args or {}
    muts = args.get("mutations") or []
    for k, v in expected.items():
        if str(args.get(k)) == str(v):
            continue
        if any(str(m.get(k) or m.get("target_field")) == str(v) for m in muts):
            continue
        return False
    return True


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--limit", type=int, default=len(PARAM_QUERIES))
    args = ap.parse_args()
    sample = PARAM_QUERIES[:args.limit]
    ctx = SessionContext(office_id="O1", office_name="서울아산병원", group_id="G1", group_name="중환자실1",
                         year=2026, month=7, today="2026-07-09", nurse_id="N001", nurse_name="관리자", user_role="HN")
    router = get_router_llm_client("openai"); llm = get_llm_client("openai")

    rows = []
    for e in sample:
        q, exp_skill, exp_slots, exp_beh = e["query"], e["expected_skill"], e["expected_slots"], e["expected_behavior"]
        allowed = route(router, q).tool_names
        recall = (exp_skill in allowed) if exp_skill else None
        tools = [t for t in SKILL_TOOLS if t["name"] in set(allowed)]
        sysp = build_system_prompt(ctx, allowed)
        msgs = [{"role": "system", "content": sysp}, {"role": "user", "content": q}]
        r = llm.chat(msgs, tools=tools)

        if r.type == "tool_call":
            tool, aargs = r.tool_name, (r.tool_args or {})
            actual = "read_first" if (tool == "query_schedule" and exp_skill not in (None, "query_schedule")) else "tool"
            # 2-턴 인식: mutation 인데 read-first 면 조회결과 주입 후 step2 로 실제 행동 채점.
            if actual == "read_first" and exp_beh in ("tool", "reject"):
                msgs.append(r.as_assistant_message())
                for tc in r.tool_calls:  # 병렬 tool_call 전부 응답(안 그러면 400)
                    msgs.append({"role": "tool", "tool_call_id": tc.call_id,
                                 "content": _wrap_untrusted_tool_output(tc.name, {"조회완료": True, "note": "대상 확인됨"})})
                r2 = llm.chat(msgs, tools=tools)
                if r2.type == "tool_call":
                    tool, aargs = r2.tool_name, (r2.tool_args or {})
                    actual = "read_first" if tool == "query_schedule" else "tool2"
                else:
                    actual = classify_text(r2.text)
        else:
            tool, aargs, actual = None, {}, classify_text(r.text)

        # 채점
        if exp_beh in ("tool", "reject"):
            sel = (tool == exp_skill)
            slots = slot_ok(exp_slots, aargs) if sel else False
            # reject 는 라우팅까지만(거부는 실행단) → 정답스킬 도달=PASS
            pas = sel and (slots if exp_beh == "tool" else True)
        elif exp_beh == "clarify":
            sel, slots = None, None
            pas = (actual == "clarify")
        elif exp_beh == "abstain":
            sel, slots = None, None
            pas = (actual == "abstain")
        else:
            sel, slots, pas = None, None, False

        rows.append({**e, "recall": recall, "actual": actual, "tool": tool,
                     "args": aargs, "sel": sel, "slots": slots, "pass": pas})

    # ── 집계 ──
    def rate(items):
        items = [x for x in items if x is not None]
        return (sum(1 for x in items if x) / len(items) * 100, len(items)) if items else (0, 0)

    print(f"\n{'='*78}\n파라미터 근거 평가 (N={len(rows)})\n{'='*78}")
    rr, rn = rate([x["recall"] for x in rows])
    sr, sn = rate([x["sel"] for x in rows])
    slr, sln = rate([x["slots"] for x in rows if x["sel"]])
    pr, pn = rate([x["pass"] for x in rows])
    print(f"Router recall (expected∈scope) : {rr:5.1f}% ({rn})")
    print(f"Selection (tool==expected)     : {sr:5.1f}% ({sn})")
    print(f"Slot filling (slots⊆args)       : {slr:5.1f}% ({sln})")
    print(f"전체 PASS                        : {pr:5.1f}% ({pn})")

    print("\n── behavior 타입별 ──")
    by_beh = defaultdict(list)
    for x in rows: by_beh[x["expected_behavior"]].append(x["pass"])
    for beh, ps in sorted(by_beh.items()):
        print(f"  {beh:9s}: {sum(ps)}/{len(ps)}")

    print("\n── 카테고리별 ──")
    by_cat = defaultdict(list)
    for x in rows: by_cat[x["category"]].append(x["pass"])
    for c, ps in sorted(by_cat.items()):
        print(f"  {c:12s}: {sum(ps)}/{len(ps)}")

    print("\n── 실패 상세 ──")
    fails = [x for x in rows if not x["pass"]]
    if not fails:
        print("  (없음)")
    for x in fails:
        detail = f"actual={x['actual']}"
        if x["tool"]: detail += f" tool={x['tool']}"
        if x["recall"] is False: detail += " [라우터 스코프 누락]"
        print(f"  ✗ [{x['expected_behavior']}→{x['category']}] {x['query'][:32]!r}\n      기대={x['expected_skill']} | {detail}")


if __name__ == "__main__":
    main()
