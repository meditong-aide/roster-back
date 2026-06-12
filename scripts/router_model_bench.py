"""라우터 모델 벤치마크 — 후보 모델별 (시간 / 분기 정확도 / 비용) 비교.

각 라벨 질의를 모델별로 N회(기본 10) classify → 측정:
  - latency(ms): classify 1회 호출 시간
  - accuracy: 분기 정확도 = 필요 tool ∈ resolve_tools(분류결과) 비율(라우터가 올바른 tool 로 좁혔는가)
  - tokens/cost: LLMResponse usage → cost.compute_cost (USD)

실행:  uv run python scripts/router_model_bench.py
"""

from __future__ import annotations

import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

# .env 로드 (OPENAI_API_KEY)
_env = Path(__file__).resolve().parent.parent / ".env"
if _env.exists():
    for line in _env.read_text().splitlines():
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k, v = s.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from agents_v2.cost import compute_cost  # noqa: E402
from agents_v2.llm_client import OpenAIClient  # noqa: E402
from agents_v2.router import (  # noqa: E402
    _CLASSIFY_SYSTEM,
    VALID_CATEGORIES,
    _parse_categories,
    resolve_tools,
)

MODELS = os.getenv(
    "BENCH_MODELS", "gpt-5.4-nano,gpt-5.4-mini,gpt-4.1-mini,gpt-5.5"
).split(",")
RUNS = int(os.getenv("BENCH_RUNS", "10"))

# (질의, 필요 tool) — 분기 정확도 = 필요 tool ∈ resolve_tools(분류 카테고리)
QUERIES: list[tuple[str, str]] = [
    ("5월 근무표 보여줘", "navigate"),
    ("김민지 5월 근무 보여줘", "query_schedule"),
    ("원티드 미제출자 누구야", "query_schedule"),
    ("팀 어디서 바꿔", "navigate"),
    ("등급 설정 화면 띄워줘", "navigate"),
    ("A팀 나이트 최소 2명으로 해줘", "manage_team_min"),
    ("나이트에 시니어 최소 2명 넣어줘", "manage_grade"),
    ("김민지 5월 10일 D를 N으로 바꿔줘", "bulk_mutation"),
    ("5월 근무표 생성해줘", "generate_schedule"),
    ("4월 위반사항 뭐야", "validate_schedule"),
    ("야간 분포 공정성 분석해줘", "analyze_report"),
    ("5월 3일 데이 빈자리 대체자 추천해줘", "recommend_candidates"),
    ("야간 최대 7회로 바꿔줘", "update_constraint"),
    ("김민지 야간전담으로 바꿔줘", "update_person_attr"),
]


def _classify_once(client: OpenAIClient, query: str):
    """classify 1회 — (categories, latency_ms, in_tok, out_tok)."""
    messages = [
        {"role": "system", "content": _CLASSIFY_SYSTEM},
        {"role": "user", "content": query},
    ]
    t0 = time.time()
    resp = client.chat(messages, tools=[])
    dt = (time.time() - t0) * 1000
    cats = [c for c in _parse_categories(resp.text or "") if c in VALID_CATEGORIES]
    return cats, dt, resp.input_tokens, resp.output_tokens


def bench_model(model: str) -> dict:
    client = OpenAIClient(model=model)
    lat, hits, total, errors = [], 0, 0, 0
    in_sum = out_sum = 0
    for query, needed in QUERIES:
        for _ in range(RUNS):
            total += 1
            try:
                cats, ms, it, ot = _classify_once(client, query)
            except Exception as e:  # noqa: BLE001
                errors += 1
                if errors <= 2:
                    print(f"  [{model}] error on '{query}': {repr(e)[:120]}")
                continue
            lat.append(ms)
            in_sum += it
            out_sum += ot
            if needed in resolve_tools(cats):
                hits += 1
    ok = total - errors
    cost = compute_cost(model, in_sum, out_sum)
    return {
        "model": model,
        "runs": total,
        "errors": errors,
        "accuracy": (hits / ok * 100) if ok else 0.0,
        "avg_ms": statistics.mean(lat) if lat else 0.0,
        "p95_ms": (sorted(lat)[int(len(lat) * 0.95)] if len(lat) > 1 else (lat[0] if lat else 0.0)),
        "in_tok": in_sum,
        "out_tok": out_sum,
        "cost_usd": cost,
        "cost_per_1k_calls": (cost / ok * 1000) if ok else 0.0,
    }


def main() -> None:
    print(f"=== ROUTER MODEL BENCH — {len(QUERIES)} queries x {RUNS} runs x {len(MODELS)} models ===")
    rows = []
    for m in MODELS:
        print(f"running {m} ...", flush=True)
        rows.append(bench_model(m))

    print("\n" + "=" * 96)
    print(f"{'model':<16}{'acc%':>7}{'avg ms':>9}{'p95 ms':>9}{'in tok':>9}{'out tok':>9}"
          f"{'cost$':>10}{'$/1k calls':>12}{'err':>5}")
    print("-" * 96)
    for r in rows:
        print(f"{r['model']:<16}{r['accuracy']:>6.1f}%{r['avg_ms']:>9.0f}{r['p95_ms']:>9.0f}"
              f"{r['in_tok']:>9}{r['out_tok']:>9}{r['cost_usd']:>10.5f}"
              f"{r['cost_per_1k_calls']:>12.4f}{r['errors']:>5}")
    print("-" * 96)

    valid = [r for r in rows if r["runs"] - r["errors"] > 0]
    if valid:
        best_acc = max(valid, key=lambda r: r["accuracy"])
        best_cost = min(valid, key=lambda r: r["cost_per_1k_calls"])
        best_fast = min(valid, key=lambda r: r["avg_ms"])
        print(f"최고 정확도: {best_acc['model']} ({best_acc['accuracy']:.1f}%)")
        print(f"최저 비용  : {best_cost['model']} (${best_cost['cost_per_1k_calls']:.4f}/1k calls)")
        print(f"최저 지연  : {best_fast['model']} ({best_fast['avg_ms']:.0f}ms)")
    print("주의: 단가는 cost.MODEL_PRICING 추정치 — openai.com/api/pricing 로 검증.")


if __name__ == "__main__":
    main()
