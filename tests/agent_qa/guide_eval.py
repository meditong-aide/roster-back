"""가이드(help RAG) 검색 평가 — 같은 의도를 다양하게 물었을 때 올바른 청크를 가져오나.

검색기(BM25)는 결정론적이라 LLM 없이 대량 변형을 즉시 채점. '답을 잘 하는지'의 핵심은
'올바른 가이드를 가져오는지'(retrieval)이므로 여기서 Retrieval@1/@2 + abstain 을 계산한다.
(액션 분기 여부와 무관 — 순수하게 '묻는 것에 맞는 가이드 회수' 능력 측정.)

    python -m tests.agent_qa.guide_eval
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "app"))
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")  # --rerank(LLM) 용 API 키
except Exception:
    pass

import argparse  # noqa: E402

from agents_v2.guide.retriever import retrieve, retrieve_rerank  # noqa: E402

# (질의, 기대 청크 id | None=미지원→abstain). 의도별 다양한 표현(정중/구어/약어/간접/오타).
GUIDE_EVAL: list[tuple[str, str | None]] = [
    # ── 병동 재분배 ──
    ("병동 재분배 어떻게 해?", "pdf_병동_재_분배_기능"),
    ("두 병동 같이 재배치하려면?", "pdf_병동_재_분배_기능"),
    ("병동 옮기는 기능 어디서 써", "pdf_병동_재_분배_기능"),
    ("팀 재분배 하는 방법 알려줘", "pdf_병동_재_분배_기능"),
    ("9A 9B 합쳐서 배치하고 싶어", "pdf_병동_재_분배_기능"),
    ("재분배 미리보기 어떻게 봐", "pdf_병동_재_분배_기능"),
    # ── 근무자 추가 ──
    ("근무자 추가 어떻게?", {"pdf_근무자_추가_개선", "member_add"}),
    ("새 간호사 등록하려면?", {"pdf_근무자_추가_개선", "member_add"}),
    ("직원 추가하는 법", {"pdf_근무자_추가_개선", "member_add"}),
    ("인원 넣는거 어디서 해", {"pdf_근무자_추가_개선", "member_add"}),
    ("근무자 검색해서 추가하고싶어", {"pdf_근무자_추가_개선", "member_add"}),
    # ── 나이트 개수 설정(근무자 설정) ──
    ("나이트 개수 한번에 지정하는 법", "pdf_근무자_설정"),
    ("전체 야간 개수 설정 어떻게", "pdf_근무자_설정"),
    ("나이트 최대 개수 어디서 정해", "pdf_근무자_설정"),
    ("연필 버튼으로 나이트 설정", "pdf_근무자_설정"),
    # ── 웹 접속 ──
    ("PC에서 어떻게 접속해?", "pdf_웹_접속_경로"),
    ("웹으로 들어가는 법", "pdf_웹_접속_경로"),
    ("컴퓨터로 근무표 보려면", "pdf_웹_접속_경로"),
    # ── 모바일 접속 ──
    ("모바일 접속 방법", "pdf_모바일_접속_경로"),
    ("폰으로 어떻게 들어가", "pdf_모바일_접속_경로"),
    ("앱에서 접속하려면?", "pdf_모바일_접속_경로"),
    ("에이드 앱 접속", "pdf_모바일_접속_경로"),
    # ── 하드코딩 help(퇴사/삭제/원티드/속성) ──
    ("퇴사 처리 어떻게 해?", "resignation"),
    ("간호사 그만두면 어떻게 정리해", "resignation"),
    ("명단에서 삭제하는 법", "member_delete"),
    ("근무자 빼는거 어디서", "member_delete"),
    ("원티드 마감 연장 방법", "wanted"),
    ("등급 바꾸는 법", "person_attr"),
    # ── 미지원(abstain 기대) ──
    ("급여 명세서 뽑는 법", None),
    ("카톡으로 근무표 공유", None),
    ("환자 통계 보는 법", None),
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rerank", action="store_true", help="BM25 recall → LLM rerank")
    args = ap.parse_args()

    _do = retrieve
    if args.rerank:
        from agents_v2.llm_client import get_router_llm_client
        _llm = get_router_llm_client("openai")  # nano = 싸게
        def _do(q, k=2):  # noqa: E731
            return retrieve_rerank(q, _llm, recall_k=6, top_k=k)
    print(f"[모드: {'BM25 recall + LLM rerank' if args.rerank else 'BM25 only'}]")

    r1_hit = r2_hit = r1_tot = 0
    ab_hit = ab_tot = 0
    by_feat: dict[str, list[bool]] = defaultdict(list)
    fails: list[tuple] = []

    for q, exp in GUIDE_EVAL:
        res = _do(q, k=2)
        ids = [c["id"] for c in res]
        if exp is None:  # 미지원 → 빈 결과여야
            ab_tot += 1
            ok = (len(res) == 0)
            ab_hit += ok
            if not ok:
                fails.append(("abstain", q, exp, ids))
        else:
            exp_set = exp if isinstance(exp, set) else {exp}
            key = sorted(exp_set)[0]
            r1_tot += 1
            top1 = ids[0] if ids else None
            hit1 = (top1 in exp_set)
            hit2 = any(i in exp_set for i in ids)
            r1_hit += hit1
            r2_hit += hit2
            by_feat[key].append(hit1)
            if not hit1:
                fails.append(("retr", q, key, ids))

    print("=" * 72)
    print(f"가이드 검색 평가 (질의 {len(GUIDE_EVAL)})")
    print("=" * 72)
    print(f"Retrieval@1 (top1 정답) : {100*r1_hit/max(1,r1_tot):5.1f}%  ({r1_hit}/{r1_tot})")
    print(f"Retrieval@2 (top2 안 정답): {100*r2_hit/max(1,r1_tot):5.1f}%  ({r2_hit}/{r1_tot})")
    print(f"Abstain (미지원→빈결과)  : {100*ab_hit/max(1,ab_tot):5.1f}%  ({ab_hit}/{ab_tot})")
    overall = r1_hit + ab_hit
    print(f"전체 PASS               : {100*overall/len(GUIDE_EVAL):5.1f}%  ({overall}/{len(GUIDE_EVAL)})")

    print("\n── 기능별 Retrieval@1 ──")
    for f, hs in sorted(by_feat.items()):
        print(f"  {f:24s}: {sum(hs)}/{len(hs)}")

    print("\n── 실패 ──")
    if not fails:
        print("  (없음)")
    for kind, q, exp, ids in fails:
        print(f"  ✗ [{kind}] {q!r}  기대={exp}  받음={ids}")


if __name__ == "__main__":
    main()
