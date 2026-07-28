"""HN 복합쿼리 50개 벤치마크 — agent.run() 전 파이프라인(라우팅→계획→dry-run→미리보기/답변)을
실 eun_roster_dev 병동 기준으로 태운다.

구성: 기존 20개(회귀) + 신규 30개(신기능: triage/log_feedback, publish, 상호배제,
원티드 일자한도, 일자별 최대인원 등)를 섞은 벤치마크.

안전: mutation 은 **미리보기(awaiting_approval)까지만**. 승인·커밋 안 함(실 병동 무변경).
publish/generate 도 커밋 전이라 실제 발행/enqueue 안 됨. read 는 그대로 실행.

채점: (1) 크래시 없이 terminal 도달 + (2) 계획된 스킬 집합(신기능 커버리지 검증).

실행: AIDE_DAG_PLANNING=1 SCRATCH=<dir> python tests/agent_qa/hn_compound_bench50.py
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")
# Langfuse 소음/지연 차단(로컬 docker 미기동 가능성) — 벤치마크는 트레이스 불필요.
# pop 이 아니라 ""(빈 문자열)로 덮어써야 app 내부 재-load_dotenv(override=False)가 되살리지 못함.
for _k in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST"):
    os.environ[_k] = ""

from agents_v2.agent_v3 import SchedulingAgent  # noqa: E402
from agents_v2.llm_client import get_llm_client, get_router_llm_client  # noqa: E402
from agents_v2.schemas.session_context import SessionContext  # noqa: E402
from db.client2 import SessionLocal  # noqa: E402

OFFICE, GROUP, YEAR, MONTH = "101358", "10135834e48b", 2026, 8
N = {"a": "이시은", "b": "손담비", "c": "정수영", "d": "안소혜", "e": "김예원", "f": "김은지C"}

# (category, query) — category 는 리포트 그룹핑용
QUERIES = [
    # ── 기존 20개 (회귀) ──
    ("legacy", "8월 데이 5명 이브닝 4명 나이트 3명으로 맞추고, 주말은 데이 3명으로 줄이고, 그걸로 근무표 만들어서 규칙 위반 없나 보고 문제 있으면 고쳐줘"),
    ("legacy", "이번 달 원티드 마감 3일 미뤄주고, 아직 안 낸 사람 누군지 확인해서, 제출된 건 다 승인하고, 그걸로 근무표 돌려서 검증까지 해줘"),
    ("legacy", "연속근무 4일로 줄이고, 나이트 뒤 이틀 쉬게 하고, 월 오프 9개로 맞추고, 그 규칙으로 다음 달 근무표 만들어서 공평한지 분석해줘"),
    ("legacy", "8월 3일 나이트 한 명 빵꾸났어. 대타 추천받아서 그 사람 넣고, 그날 근무표 다시 검증하고, 문제 있으면 고치고, 최종 공평한지 봐줘"),
    ("legacy", f"{N['a']} 야간전담으로 바꾸고, {N['b']} A팀으로 옮기고, 그 둘 반영해서 근무표 새로 만들고, 규칙 위반 확인하고, 나이트 공평한지 분석해줘"),
    ("legacy", "8월 7일 데이 8명으로 늘리고, 그날 나이트도 4명으로 하고, 15일 이브닝 5명으로 잡고, 그걸로 근무표 돌려서 문제없나 검증해줘"),
    ("legacy", f"{N['b']} 이번 달 나이트 5번으로 제한하고, {N['a']} 데이 최소 9개 보장하고, {N['c']} 오프 10개 맞추고, 그 조건으로 근무표 생성해서 검증해줘"),
    ("legacy", "다음 달 근무표 만들고, 규칙 위반 확인하고, 문제 있으면 고치고, 나이트 공평한지 분석하고, 오프 몰림 없나도 봐줘"),
    ("legacy", "이번 달 희망근무 아직 안 낸 사람 확인해서, 마감 이틀 연장하고, 낸 사람들 원티드 다 승인하고, 근무표 돌리고, 위반 없나 검증해줘"),
    ("legacy", f"{N['a']} 다음 달 중환자실1로 파견 보내고, 그 자리 빈 날 대타 추천받아서 채우고, 근무표 다시 만들어서 검증하고 분석해줘"),
    ("legacy", "연속근무 5일 허용하고, 주말 근무 공평하게 켜고, 8월 전체 나이트 3명으로 맞추고, 근무표 생성해서 규칙 검증하고 문제 있으면 수정해줘"),
    ("legacy", f"{N['b']} 수간호사로 승급하고, {N['d']} 프리셉터로 지정하고, {N['e']} 야간전담으로 바꾸고, 그걸로 근무표 만들어서 검증하고 나이트 공평한지 봐줘"),
    ("legacy", "이번 달 근무표 검증해서 위반 있으면 고치고, 나이트 공평한지 분석하고, 편중된 사람 있으면 그 사람 다음 달 나이트 제한 걸어줘"),
    ("legacy", "8월 주말 데이 2명으로 줄이고, 평일은 5명으로 하고, 20일만 특별히 7명으로 잡고, 근무표 돌려서 위반 검증하고 분석해줘"),
    ("legacy", f"{N['a']} 원티드 승인하고, {N['b']} 원티드 반려하고, 나머지 제출된 거 다 승인하고, 근무표 생성해서 검증해줘"),
    ("legacy", "다음 달 근무표 만들고, 검증해서 위반 나오면 고치고, 다시 검증하고, 그다음 공평한지 분석해줘"),
    ("legacy", f"연속근무 4일로 줄이고, {N['a']} 야간전담 풀고, 이번 달 나이트 인원 2명으로 맞추고, 그걸로 근무표 다시 돌려서 검증하고 분석해줘"),
    ("legacy", "이번 달 미제출자 확인하고, 그 사람들 마감 연장 안내하고, 마감 후 낸 거 다 승인하고, 근무표 만들고, 나이트 공평한지 분석해줘"),
    ("legacy", f"5월 근무표 만들고, 규칙 검증하고, 문제 구간 고치고, {N['a']} 야간 너무 많으면 다음 달 제한 걸고, 최종 분석해줘"),
    ("legacy", f"{N['b']} 중환자실1로 파견 보내고, 그 빈자리 대타 추천받아서 넣고, 근무표 다시 만들고, 위반 검증하고, 문제 있으면 고쳐줘"),

    # ── 신규: triage / log_feedback (처리불가·불만·건의) ──
    ("triage", "관리자 권한이 자꾸 풀려서 로그인하면 아무것도 못 해요. 그리고 8월 나이트 편중된 사람 있는지도 분석해줘"),
    ("triage", "듀티표에서 간호사별 오프 개수를 한눈에 보는 화면 있으면 좋겠어요"),
    ("triage", "원티드를 관리자가 수정해도 실제 원티드에 반영이 안 돼요. 이번 달 미제출자도 알려주세요"),
    ("triage", "시스템이 너무 느리고 저장 눌러도 반응이 없어요. 그래도 8월 근무표 검증은 되는지 봐주세요"),
    ("triage", "근무 편차가 심하다는 불만이 많아. 8월 공평한지 분석해서 편중 심한 사람 다음 달 나이트 제한 걸고, 우리가 못 고치는 부분은 접수해줘"),

    # ── 신규: publish (확정/발행) ──
    ("publish", "8월 근무표 검증해서 문제 없으면 확정해줘"),
    ("publish", "다음 달 근무표 만들고, 위반 없나 확인하고, 괜찮으면 발행해줘"),
    ("publish", "이번 달 원티드 다 승인하고, 근무표 생성하고, 위반 고치고, 확정까지 해줘"),

    # ── 신규: 상호배제 ──
    ("mutex", f"{N['a']}이랑 {N['b']} 같이 근무 안 서게 묶어줘. 그리고 그걸로 근무표 다시 돌려서 검증해줘"),
    ("mutex", f"{N['c']}이랑 {N['d']} 상호배제 걸고, 8월 나이트 공평한지 분석해줘"),
    ("mutex", f"{N['a']}이랑 {N['c']} 상호배제 걸고, {N['b']} 야간전담으로 바꾸고, 근무표 생성해서 검증해줘"),

    # ── 신규: 원티드 일자별 한도 ──
    ("wanted_daily", "8월 15일 원티드 휴무는 3명까지만 받게 하고, 미제출자 확인해줘"),
    ("wanted_daily", "이번 달 원티드 하루 한도 5명으로 잡고, 마감 이틀 연장하고, 제출된 거 승인해줘"),
    ("wanted_daily", "8월 원티드 하루 3명 한도 걸고, 15일만 5명으로 풀어주고, 미제출자 확인해줘"),

    # ── 신규: 일자별 최대 인원 ──
    ("daily_max", "8월 데이 최소 4명 최대 6명으로 잡고, 나이트는 최대 3명으로 제한하고, 근무표 돌려서 검증해줘"),
    ("daily_max", "주말 데이 최대 4명으로 묶고, 평일 이브닝 최대 5명으로 하고, 그걸로 생성해서 분석해줘"),
    ("daily_max", "8월 20일 나이트 5명으로 늘리고, 21일 데이 7명으로 잡고, 생성해서 위반 확인하고 고쳐줘"),

    # ── 신규: 혼합(회귀형·새 이름/조합) ──
    ("mixed", f"{N['a']} 경력 5년으로 고치고, {N['b']} A팀으로 옮기고, {N['e']} 야간전담 풀고, 근무표 다시 만들어 검증해줘"),
    ("mixed", "이번 달 원티드 다 승인하고, 근무표 생성하고, 위반 고치고, 나이트 공평한지 분석해줘"),
    ("mixed", "연속근무 3일로 줄이고, 월 오프 10개로 맞추고, 나이트 뒤 이틀 휴식 걸고, 생성해서 검증하고 분석해줘"),
    ("mixed", f"{N['c']} 수간호사 승급하고, {N['f']} 프리셉터 지정하고, 근무표 만들어 검증해줘"),
    ("mixed", f"{N['a']} 8월 31일자로 퇴사 처리하고, 그 자리 대타 추천받아서 채우고, 근무표 다시 돌려줘"),
    ("mixed", f"{N['b']} 중환자실1로 영구 이동시키고, 근무표 다시 만들어서 검증하고 분석해줘"),
    ("mixed", "이번 달 근무표 엑셀로 내려받고, 나이트 공평한지 분석하고, 편중되면 그 사람 다음 달 제한 걸어줘"),
    ("mixed", "8월 근무표 어디서 보는지 알려주고, 미제출자 명단도 보여줘"),
    ("mixed", "원티드 마감 즉시 마감하고, 제출된 거 다 승인하고, 근무표 생성해서 확정해줘"),
    ("mixed", f"{N['a']} 나이트 6개로 제한하고, {N['c']} 데이 최소 8개 보장하고, {N['d']} 오프 9개 맞추고, 생성해서 분석해줘"),
    ("mixed", "다음 달 근무표 만들고 실패하면 왜 안 되는지 원인 알려주고 어떻게 풀지 옵션 줘"),
    ("mixed", "8월 근무표 검증하고, 위반 있으면 고치고, 다시 검증하고, 괜찮으면 확정하고, 확정된 거 엑셀로 줘"),
    ("mixed", f"{N['a']}이랑 {N['b']} 같이 근무 안 서게 묶고, 8월 3일 나이트 대타 추천받아서 채우고, 근무표 다시 돌려서 검증해줘"),
]


def _ctx():
    return SessionContext(office_id=OFFICE, group_id=GROUP, year=YEAR, month=MONTH,
                          nurse_id="EVAL_HN", nurse_name="평가자", user_role="HN")


def _planned_skills(trace):
    """이 턴에서 계획/실행된 스킬 이름 집합(순서 유지)."""
    out = []
    for s in (trace or []):
        if s.name == "planning" and s.data and s.data.get("skill"):
            sk = s.data["skill"]
            if sk not in out:
                out.append(sk)
        # DAG plan stage: tasks 는 (id, skill, kind, deps) 튜플 또는 dict.
        if s.name == "plan" and s.data:
            for t in (s.data.get("tasks") or []):
                if isinstance(t, dict):
                    sk = t.get("skill")
                elif isinstance(t, (list, tuple)) and len(t) >= 2:
                    sk = t[1]
                else:
                    sk = None
                if sk and sk not in out:
                    out.append(sk)
    return out


def main():
    if not os.getenv("OPENAI_API_KEY"):
        print("OPENAI_API_KEY 없음"); return
    agent = SchedulingAgent(get_llm_client("openai"), enable_user_memory=False,
                            router_llm=get_router_llm_client("openai"))
    agent._dag_planning = True

    rows = []
    for i, (cat, q) in enumerate(QUERIES, 1):
        db = SessionLocal()
        rec = {"n": i, "cat": cat, "q": q}
        try:
            res = agent.run(db, q, _ctx())
            stages = [s.name for s in (res.trace or [])]
            plan_stage = next((s for s in (res.trace or []) if s.name == "plan"), None)
            skills = _planned_skills(res.trace)
            ans = (res.answer or "").strip()
            err = isinstance(res.data, dict) and res.data.get("error")
            terminal = res.awaiting_approval or bool(ans) or res.needs_clarification
            rec.update(via="plan" if plan_stage else ("react" if "routing" in stages else "?"),
                       skills=skills, awaiting=res.awaiting_approval,
                       clarify=res.needs_clarification, ans=ans[:70],
                       err=bool(err), ok=bool(terminal and not err))
        except Exception as e:  # noqa: BLE001
            rec.update(via="CRASH", skills=[], awaiting=False, clarify=False, ans="",
                       err=True, ok=False, crash=str(e)[:140])
        finally:
            try: db.rollback()
            except Exception: pass
            db.close()
        rows.append(rec)
        m = "OK " if rec["ok"] else "FAIL"
        print(f"[{m}] #{i:2d} {rec['cat']:12s} via={rec.get('via'):5s} "
              f"skills={','.join(rec.get('skills') or []) or '—'} "
              f"{'CRASH:'+rec.get('crash','') if rec.get('via')=='CRASH' else ''}")

    _report(rows)


def _report(rows):
    out = Path(os.getenv("SCRATCH", "/tmp")) / "hn_compound_bench50_result.md"
    npass = sum(1 for r in rows if r["ok"])
    # 신기능 스킬 커버리지
    feat_skills = ["log_feedback", "publish_schedule", "manage_mutual_exclusion",
                   "manage_wanted_limits", "manage_daily_shift", "recommend_candidates",
                   "manage_assignment", "resolve_infeasibility", "update_person_attr"]
    coverage = {s: sum(1 for r in rows if s in (r.get("skills") or [])) for s in feat_skills}

    L = ["# HN 복합쿼리 50 벤치마크 (agent.run, eun_roster_dev)\n",
         f"> 실 병동({GROUP}) {YEAR}/{MONTH}. mutation=미리보기까지(무커밋). 기존 20 + 신규 30.\n",
         "## 요약\n", "| 지표 | 값 |", "|---|---|",
         f"| E2E terminal 성공 | {npass}/{len(rows)} ({npass/len(rows)*100:.0f}%) |",
         f"| DAG plan 경유 | {sum(1 for r in rows if r.get('via')=='plan')}/{len(rows)} |",
         f"| ReAct fallback | {sum(1 for r in rows if r.get('via')=='react')}/{len(rows)} |",
         f"| CRASH | {sum(1 for r in rows if r.get('via')=='CRASH')}/{len(rows)} |\n",
         "## 카테고리별 성공\n", "| 카테고리 | 성공 |", "|---|---|"]
    cats = {}
    for r in rows:
        cats.setdefault(r["cat"], [0, 0])
        cats[r["cat"]][1] += 1
        if r["ok"]:
            cats[r["cat"]][0] += 1
    for c, (p, t) in cats.items():
        L.append(f"| {c} | {p}/{t} |")
    L += ["\n## 신기능 스킬 커버리지 (계획된 턴 수)\n", "| 스킬 | 등장 |", "|---|---|"]
    for s, c in coverage.items():
        L.append(f"| {s} | {c} |")
    L += ["\n## 턴별\n",
          "| # | cat | 쿼리 | 경로 | 계획 스킬 | 대기 | 답변/에러 | 판정 |",
          "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        note = r.get("crash") or ("ERR:" + r["ans"][:34] if r.get("err") else r.get("ans", "")[:34])
        L.append(f"| {r['n']} | {r['cat']} | {r['q'][:34]}… | {r.get('via')} | "
                 f"{','.join(r.get('skills') or []) or '—'} | "
                 f"{'○' if r.get('awaiting') else ('?' if r.get('clarify') else '—')} | "
                 f"{note} | {'✅' if r['ok'] else '❌'} |")
    out.write_text("\n".join(L), encoding="utf-8")
    print("\n" + "=" * 70)
    print(f"E2E terminal {npass}/{len(rows)} | 채점표: {out}")
    print("신기능 커버리지:", {s: c for s, c in coverage.items() if c})


if __name__ == "__main__":
    main()
