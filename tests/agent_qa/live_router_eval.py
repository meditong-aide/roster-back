"""라이브 LLM 라우팅 평가 하니스.

기존 corpus 가드(`test_*_corpus.py`)는 mock LLM 으로 wiring 만 검증.
본 스크립트는 corpus 발화를 **실제 OpenAI/Anthropic** 으로 돌려
LLM 라우팅 정확도 / 토큰 / 지연을 정량 측정한다.

pytest collect 제외 (파일명 live_*) — 명시 실행 필요:

    python -m tests.agent_qa.live_router_eval --provider openai --limit 20
    python -m tests.agent_qa.live_router_eval --provider anthropic
    python -m tests.agent_qa.live_router_eval --provider openai --corpus wave1,wave3

Metrics:
  - recall: expected_tool ∈ tool_names (전체 / by-category / by-tool)
  - 평균 latency, 누적 토큰 (in/out), 추정 cost (provider 기본 단가)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

# sys.path 보정 (script-style 실행 시 app/ + corpus import 위해)
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parent / "corpus"))

# .env 자동 로드 (OPENAI_API_KEY 등). 없으면 silent.
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

from agents_v2.llm_client import get_router_llm_client  # noqa: E402
from agents_v2.router import route  # noqa: E402

import importlib  # noqa: E402


# ── corpus 로더 ─────────────────────────────────────────────


def _load_corpus(names: list[str]) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    for name in names:
        module = importlib.import_module(f"queries_{name}")
        # 우선 ALL_*_PHRASES, 없으면 *PHRASES 접두로 끝나는 list 다 합침.
        all_attr = next((k for k in dir(module) if k.startswith("ALL_") and k.endswith("_PHRASES")), None)
        if all_attr:
            out.extend(getattr(module, all_attr))
            continue
        for k in dir(module):
            if k.endswith("_PHRASES") and isinstance(getattr(module, k), list):
                out.extend(getattr(module, k))
    return out


# 단가 (USD, 백만 토큰당 기준 1k 토큰당 가격으로 환산 — 대략치).
_COST_PER_1K = {
    "openai":    {"in": 0.000150, "out": 0.000600},   # gpt-4.1-mini 추정
    "anthropic": {"in": 0.000800, "out": 0.004000},   # haiku 추정
}


def _estimate_cost(provider: str, tokens_in: int, tokens_out: int) -> float:
    rate = _COST_PER_1K.get(provider, {"in": 0.0, "out": 0.0})
    return (tokens_in / 1000.0) * rate["in"] + (tokens_out / 1000.0) * rate["out"]


# ── 실행 ───────────────────────────────────────────────────────


def _eval_one(llm, phrase: str, expected_tool: str) -> dict:
    t0 = time.perf_counter()
    result = route(llm, phrase)
    elapsed = time.perf_counter() - t0
    return {
        "phrase": phrase,
        "expected_tool": expected_tool,
        "categories": result.categories,
        "tool_names": result.tool_names,
        "hit": expected_tool in result.tool_names,
        "fallback_used": result.fallback_used,
        "confidence": result.confidence,
        "latency_s": elapsed,
        # router 자체는 토큰 미보고. LLM client 내부 메타에 의존.
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Live LLM 라우팅 평가")
    ap.add_argument("--provider", default="openai", choices=["openai", "anthropic", "deterministic"])
    ap.add_argument("--corpus", default="wave1,wave2,wave3,read",
                    help="콤마 구분 corpus 이름 (queries_<name>.py)")
    ap.add_argument("--limit", type=int, default=0,
                    help="발화 최대 개수 (0=전체). 토큰 비용 통제용.")
    ap.add_argument("--output", default=None, help="결과 JSON 저장 경로 (선택).")
    args = ap.parse_args()

    corpus_names = [n.strip() for n in args.corpus.split(",") if n.strip()]
    phrases = _load_corpus(corpus_names)
    if args.limit > 0:
        phrases = phrases[: args.limit]

    if not phrases:
        print("corpus 가 비어있어요.", file=sys.stderr)
        return 2

    print(f"# Live router eval — provider={args.provider}, corpus={','.join(corpus_names)}, "
          f"n={len(phrases)}")
    llm = get_router_llm_client(args.provider)

    rows: list[dict] = []
    by_cat: dict[str, dict] = defaultdict(lambda: {"hit": 0, "total": 0})
    by_tool: dict[str, dict] = defaultdict(lambda: {"hit": 0, "total": 0})
    total_lat = 0.0
    hits = 0
    fallbacks = 0

    for phrase, expected_cat, expected_tool in phrases:
        try:
            r = _eval_one(llm, phrase, expected_tool)
        except Exception as e:
            print(f"  ! '{phrase[:40]}...' 실패: {e}")
            r = {"phrase": phrase, "expected_tool": expected_tool, "hit": False,
                 "error": str(e), "latency_s": 0.0, "fallback_used": True,
                 "categories": [], "tool_names": []}
        rows.append(r)
        if r["hit"]:
            hits += 1
        if r.get("fallback_used"):
            fallbacks += 1
        by_cat[expected_cat]["total"] += 1
        if r["hit"]:
            by_cat[expected_cat]["hit"] += 1
        by_tool[expected_tool]["total"] += 1
        if r["hit"]:
            by_tool[expected_tool]["hit"] += 1
        total_lat += r["latency_s"]
        mark = "✅" if r["hit"] else ("⚠️ fallback" if r.get("fallback_used") else "❌")
        cats = ",".join(r.get("categories") or [])
        print(f"  {mark} [{expected_tool:>30}] '{phrase[:60]}' → cats=[{cats}] "
              f"({r['latency_s']:.2f}s)")

    n = len(phrases)
    print("\n# Summary")
    print(f"  recall (overall):  {hits}/{n} = {hits/n:.1%}")
    print(f"  fallback rate:     {fallbacks}/{n} = {fallbacks/n:.1%}")
    print(f"  avg latency:       {total_lat/n:.2f}s")
    print("\n  by category:")
    for cat, stat in sorted(by_cat.items()):
        h, t = stat["hit"], stat["total"]
        print(f"    {cat:>20}  {h}/{t}  ({h/t:.1%})")
    print("\n  by tool:")
    for tool, stat in sorted(by_tool.items()):
        h, t = stat["hit"], stat["total"]
        print(f"    {tool:>32}  {h}/{t}  ({h/t:.1%})")

    if args.output:
        Path(args.output).write_text(json.dumps({
            "provider": args.provider,
            "corpus": corpus_names,
            "n": n,
            "hits": hits,
            "fallbacks": fallbacks,
            "avg_latency_s": total_lat / n,
            "by_category": dict(by_cat),
            "by_tool": dict(by_tool),
            "rows": rows,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n  ↳ JSON saved: {args.output}")

    return 0 if hits == n else 1


if __name__ == "__main__":
    sys.exit(main())
