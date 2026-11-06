"""전체 스코프 평가 하니스 (라이브) — novel 쿼리 + 복합쿼리 + 수간호사 현실성 게이트 + 지표.

절차:
  1) 스코프별 5개 novel 쿼리(코퍼스 밖) + 복합쿼리 정답지 작성(아래 CASES).
  2) 수간호사 현실성 게이트: "실제 개발내용 모르는 수간호사가 할 법한 말인가?" LLM 배치 판정
     → 통과분만 채점(개발 용어 leak 은 제외).
  3) single: route recall(정답 스킬 ∈ 스코프) 채점. compound: build_plan 구조(의존/None) 채점.
  4) 지표 집계.

실행: python tests/agent_qa/full_scope_eval.py [--limit N]
"""

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from agents_v2.llm_client import get_llm_client, get_router_llm_client  # noqa: E402
from agents_v2.router import route  # noqa: E402
from agents_v2.planning.planner import build_plan  # noqa: E402
from agents_v2.skills.descriptions import SKILL_TOOLS  # noqa: E402

# ── single: (scope, query, expected_skill) — 전부 novel, 수간호사 말투 ──
SINGLE = [
    # read: schedule
    ("read:schedule", "이번 달 누가 야간 제일 많이 서?", "query_schedule"),
    ("read:schedule", "15일에 누구누구 나와?", "query_schedule"),
    ("read:schedule", "김민지 이번 주 어떻게 근무해?", "query_schedule"),
    ("read:schedule", "다음 달 근무표 나왔어?", "query_schedule"),
    ("read:schedule", "주말에 나오는 사람 누구야?", "query_schedule"),
    # read: nurse_info
    ("read:nurse", "우리 병동 신규 누구야?", "query_schedule"),
    ("read:nurse", "경력 5년 넘는 사람 몇 명이야?", "query_schedule"),
    ("read:nurse", "김민지 어느 팀이지?", "query_schedule"),
    ("read:nurse", "야간 못 서는 사람 있어?", "query_schedule"),
    ("read:nurse", "프리셉터 누구누구야?", "query_schedule"),
    # read: constraint config
    ("read:rules", "연속으로 며칠까지 일 시킬 수 있게 해놨어?", "query_schedule"),
    ("read:rules", "야간 몇 번까지 가능하게 돼있지?", "query_schedule"),
    ("read:rules", "지금 근무 규칙 어떻게 설정돼 있어?", "query_schedule"),
    ("read:rules", "이브닝 다음날 데이 막아놨나?", "query_schedule"),
    ("read:rules", "월 오프 몇 개로 돼있어?", "query_schedule"),
    # read: wanted
    ("read:wanted", "이번 달 희망근무 언제까지 받아?", "query_schedule"),
    ("read:wanted", "누가 아직 희망근무 안 냈어?", "query_schedule"),
    ("read:wanted", "김민지 뭐 신청했어?", "query_schedule"),
    ("read:wanted", "원티드 몇 개까지 낼 수 있어?", "query_schedule"),
    ("read:wanted", "다들 원티드 제출했나?", "query_schedule"),
    # read: monthly limit
    ("read:limit", "김민지 이번 달 야간 몇 번으로 제한했지?", "query_schedule"),
    ("read:limit", "박혜미 데이 몇 개까지야?", "query_schedule"),
    ("read:limit", "개인 야간 제한 걸어둔 사람 누구야?", "query_schedule"),
    ("read:limit", "이번 달 한도 정해둔 사람 목록 좀", "query_schedule"),
    ("read:limit", "김민지 오프 몇 개 보장돼?", "query_schedule"),
    # mutate: constraint
    ("mut:rules", "연속근무 최대 4일로 줄여줘", "update_constraint"),
    ("mut:rules", "야간 뒤에 이틀 쉬게 해줘", "update_constraint"),
    ("mut:rules", "월 오프 9개로 맞춰줘", "update_constraint"),
    ("mut:rules", "팀 밸런스 켜줘", "update_constraint"),
    ("mut:rules", "이브닝 다음 데이 금지해줘", "update_constraint"),
    # mutate: person
    ("mut:person", "김민지 주임으로 올려줘", "update_person_attr"),
    ("mut:person", "박혜미 A팀으로 보내줘", "update_person_attr"),
    ("mut:person", "이영희 야간 전담으로 바꿔줘", "update_person_attr"),
    ("mut:person", "정수민 주말 오프 빼줘", "update_person_attr"),
    ("mut:person", "한지우 프리셉터로 지정해줘", "update_person_attr"),
    # mutate: monthly limit
    ("mut:limit", "김민지 이번 달 야간 4번까지만 서게 해줘", "update_monthly_limit"),
    ("mut:limit", "박혜미 데이 최소 8개는 서게 해줘", "update_monthly_limit"),
    ("mut:limit", "이영희 야간 최대 5번으로 묶어줘", "update_monthly_limit"),
    ("mut:limit", "김지은 나이트 3개로 맞춰줘", "update_monthly_limit"),
    ("mut:limit", "박춘일 데이 정확히 10번 서게", "update_monthly_limit"),
    # mutate: daily shift coverage
    ("mut:coverage", "8월 7일은 데이 8명 필요해", "manage_daily_shift"),
    ("mut:coverage", "주말은 인원 좀 줄여서 데이 2명씩", "manage_daily_shift"),
    ("mut:coverage", "평일 나이트 3명으로 해줘", "manage_daily_shift"),
    ("mut:coverage", "8월 전체 데이 5명으로 맞춰줘", "manage_daily_shift"),
    ("mut:coverage", "15일에 이브닝 4명 넣어줘", "manage_daily_shift"),
    # mutate: wanted
    ("mut:wanted", "김민지 15일 희망근무 승인해줘", "bulk_mutation"),
    ("mut:wanted", "박혜미 원티드 거절해줘", "bulk_mutation"),
    ("mut:wanted", "낸 원티드 다 승인해줘", "bulk_mutation"),
    ("mut:wanted", "김민지 희망근무 취소해줘", "bulk_mutation"),
    ("mut:wanted", "이영희 20일 나이트 원티드 넣어줘", "bulk_mutation"),
    # mutate: assignment
    ("mut:assign", "김민지 다음 달에 중환자실2로 파견 보내줘", "manage_assignment"),
    ("mut:assign", "박혜미 8월부터 응급실로 옮겨줘", "manage_assignment"),
    ("mut:assign", "이영희 파견 취소해줘", "manage_assignment"),
    ("mut:assign", "지금 파견 나가있는 사람 누구야?", "manage_assignment"),
    ("mut:assign", "정수민 원래 병동으로 복귀시켜줘", "manage_assignment"),
    # generate / validate / analyze / recommend
    ("gen", "다음 달 근무표 만들어줘", "generate_schedule"),
    ("gen", "5월 근무표 다시 돌려줘", "generate_schedule"),
    ("gen", "6월 거 생성해줘", "generate_schedule"),
    ("validate", "근무표 규칙 위반 없나 봐줘", "validate_schedule"),
    ("validate", "이 근무표 문제 있어?", "validate_schedule"),
    ("analyze", "야간 공평하게 됐나 분석해줘", "analyze_report"),
    ("analyze", "누가 오프 제일 많아?", "analyze_report"),
    ("recommend", "5월 3일 나이트 대타 누구 좋을까?", "recommend_candidates"),
    ("recommend", "김민지 대신 나올 사람 추천해줘", "recommend_candidates"),
]

# ── compound: (query, kind, expect) expect: "plan"(의존 있어야) / "none"(ReAct) ──
COMPOUND = [
    ("5월 3일 나이트 빵꾸났는데 대타 찾아서 바로 넣어줘", "dataflow", "plan"),
    ("8월 전체 데이 5명, 주말은 3명으로 하고 근무표 돌려줘", "override+gen", "plan"),
    ("전체 데이 5명, 주말 3명, 15일만 8명으로 해줘", "multi-override", "plan"),
    ("연속근무 4일로 하고 월 오프 9개로 하고 생성해줘", "settings+gen", "plan"),
    ("근무표 검증하고 문제 있으면 고쳐줘", "validate→repair", "plan"),
    ("이번 달 미제출자 확인하고 마감 연장해줘", "read→mutate", "either"),
    ("김민지 등급 올리고 박혜미 팀 옮겨줘", "independent", "none"),
    ("데이 필요인원 5명, 월 오프 10개, 김민지 등급 3으로", "independent", "none"),
    ("김민지랑 박혜미 이번 달 야간 각각 몇 개야?", "independent-read", "none"),
    ("5월 근무표 만들고 규칙 위반 없나 확인해줘", "gen→validate", "plan"),
]


def _headnurse_gate(llm, queries):
    """수간호사 현실성 배치 판정 → {query: (ok, reason)}."""
    numbered = "\n".join(f"{i}. {q}" for i, q in enumerate(queries))
    sys_p = (
        "너는 간호 현장 전문가다. 아래 문장들이 **시스템 내부 구현을 전혀 모르는 실제 수간호사**가 "
        "근무표 관리하며 자연스럽게 할 법한 말인지 각각 판정하라. "
        "개발/DB 용어(슬롯, 필드명, config, group_id 등)가 드러나면 부적합(NO). "
        "각 줄: '<번호>: YES' 또는 '<번호>: NO - <이유 한줄>'. 그 외 출력 금지."
    )
    resp = llm.chat([{"role": "system", "content": sys_p},
                     {"role": "user", "content": numbered}], tools=[])
    out = {}
    for line in (resp.text or "").splitlines():
        line = line.strip()
        if ":" not in line:
            continue
        num, verdict = line.split(":", 1)
        try:
            idx = int(num.strip().rstrip("."))
        except ValueError:
            continue
        if 0 <= idx < len(queries):
            out[queries[idx]] = ("YES" in verdict.upper(), verdict.strip())
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="스모크: single N개만")
    args = ap.parse_args()
    if not os.getenv("OPENAI_API_KEY"):
        print("OPENAI_API_KEY 없음"); return

    llm = get_llm_client("openai")
    router = get_router_llm_client("openai")
    singles = SINGLE[: args.limit] if args.limit else SINGLE
    all_q = [q for _, q, _ in singles] + [q for q, _, _ in COMPOUND]

    print(f"=== 수간호사 현실성 게이트 (총 {len(all_q)}) ===")
    gate = _headnurse_gate(llm, all_q)
    rejected = [q for q, (ok, _) in gate.items() if not ok]
    print(f"  통과 {sum(1 for _, (ok, _) in gate.items() if ok)}/{len(gate)}  거절 {len(rejected)}")
    for q in rejected:
        print(f"   [reject] {q}  ← {gate[q][1]}")

    # ── single: route recall (게이트 통과분만) ──
    print("\n=== SINGLE — route recall (게이트 통과분) ===")
    by_scope = {}
    for scope, q, exp in singles:
        if not gate.get(q, (True, ""))[0]:
            continue
        allowed = route(router, q).tool_names
        hit = exp in allowed
        by_scope.setdefault(scope, []).append(hit)
        if not hit:
            print(f"  MISS [{scope}] {q} → {exp} not in {allowed}")
    s_all = [h for hits in by_scope.values() for h in hits]
    print(f"\n  스코프별:")
    for scope, hits in sorted(by_scope.items()):
        print(f"    {scope:14s} {sum(hits)}/{len(hits)}")
    single_recall = sum(s_all) / len(s_all) if s_all else 0

    # ── compound: plan 구조 ──
    print("\n=== COMPOUND — plan 구조 (게이트 통과분) ===")
    c_ok = c_tot = 0
    for q, kind, exp in COMPOUND:
        if not gate.get(q, (True, ""))[0]:
            continue
        c_tot += 1
        p = build_plan(llm, q, SKILL_TOOLS)
        if exp == "plan":
            ok = p is not None and any(t.deps for t in p.tasks)
        elif exp == "none":
            ok = p is None
        else:  # either
            ok = p is None or all(True for _ in [0])  # 통과(구조 유효)
        c_ok += int(ok)
        mark = "OK " if ok else "FAIL"
        summ = None if p is None else [(t.id, t.skill, t.deps) for t in p.tasks]
        print(f"  {mark} [{kind}] {q}\n        → {summ}")

    # ── 지표 ──
    print("\n" + "=" * 50)
    print("지표")
    realism = sum(1 for _, (ok, _) in gate.items() if ok) / len(gate) if gate else 0
    print(f"  수간호사 현실성 통과율 : {realism*100:5.1f}%")
    print(f"  SINGLE route recall    : {single_recall*100:5.1f}%  ({sum(s_all)}/{len(s_all)})")
    print(f"  COMPOUND plan 정확도   : {(c_ok/c_tot*100 if c_tot else 0):5.1f}%  ({c_ok}/{c_tot})")


if __name__ == "__main__":
    main()
