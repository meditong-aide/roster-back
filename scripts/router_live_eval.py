"""Router 라이브 품질 평가 — 실제 LLM 분류를 :8001 로 측정.

각 라벨 질의를 /agent/test/chat 으로 보내고 pipeline_stages 에서
routing stage(categories, tool_names, fallback_used) 와 planning stage(실제 호출 skill)를
캡처해 라우팅 recall(필요 tool ∈ scoped set)을 계산한다.

실행:  uv run python scripts/router_live_eval.py
"""

from __future__ import annotations

import json
import urllib.request

BASE = "http://localhost:8001"

# (질의, 필요 tool) — gold 카테고리가 아니라 '실제 처리에 필요한 tool' 기준.
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


def _post(path: str, body: dict) -> dict:
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode())


def main() -> None:
    seed = _post("/agent/test/setup-db", {})  # seed (idempotent)
    print(f"setup-db: {seed}")
    # warmup — 콜드스타트 지연이 첫 측정 질의를 오염시키지 않게.
    try:
        _post("/agent/test/chat",
              {"message": "안녕", "llm_provider": "openai", "use_test_db": True})
    except Exception as e:  # noqa: BLE001
        print(f"warmup skipped: {e}")

    print(f"{'query':<32}{'fb':>3} {'cats':<26}{'need':>20} {'in?':>4} {'planning':<20}")
    print("-" * 110)
    hits = 0
    fallback_n = 0
    scoped_counts = []
    for q, need in QUERIES:
        try:
            d = _post("/agent/test/chat",
                      {"message": q, "llm_provider": "openai", "use_test_db": True})
        except Exception as e:  # noqa: BLE001
            print(f"{q:<32} ERROR {e}")
            continue
        stages = d.get("pipeline_stages", [])
        routing = next((s for s in stages if s["name"] == "routing"), None)
        plans = [s["data"].get("skill") for s in stages if s["name"] == "planning"]
        if routing is None:
            print(f"{q:<32} (no routing stage — error: {str(d.get('error'))[:40]})")
            continue
        rd = routing["data"]
        cats = rd.get("categories", [])
        fb = rd.get("fallback_used", False)
        scoped = rd.get("tool_names", [])
        scoped_counts.append(len(scoped))
        in_scope = need in scoped
        hits += int(in_scope)
        fallback_n += int(fb)
        print(f"{q:<32}{('Y' if fb else '-'):>3} {str(cats):<26}{need:>20} "
              f"{('OK' if in_scope else 'MISS'):>4} {str(plans):<20}")

    n = len(QUERIES)
    print("-" * 110)
    print(f"recall(need ∈ scoped): {hits}/{n} = {100.0*hits/n:.1f}%")
    print(f"fallback rate: {fallback_n}/{n}")
    if scoped_counts:
        print(f"avg scoped tool count: {sum(scoped_counts)/len(scoped_counts):.1f} / 14")


if __name__ == "__main__":
    main()
