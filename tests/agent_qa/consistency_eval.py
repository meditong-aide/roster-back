"""L2 answer-consistency judge 평가 하니스 (라이브) — fault-injection 으로 검증기 자체를 측정.

정합/비정합 라벨 코퍼스로 judge 의 판별력을 집계한다:
  - recall          : 주입한 결함(비정합)을 얼마나 잡나        (TP / (TP+FN))
  - false-positive  : 정상 답변을 잘못 막는 비율(오탐)          (FP / (FP+TN))
  - precision       : 잡았다고 한 것 중 진짜 결함 비율          (TP / (TP+FP))

검증기(L2)를 만들었으니 검증기 자체를 검증한다. L1(read-back)은 결정적이라
test_verify_readback 의 fault-injection(no-op write→포착) 유닛테스트가 곧 측정.

실행: python tests/agent_qa/consistency_eval.py
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from agents_v2.llm_client import get_router_llm_client  # noqa: E402
from agents_v2.verify import judge_answer_consistency  # noqa: E402

# label=True  → 정합(judge 는 CONSISTENT 여야, 오탐이면 실패)
# label=False → 비정합(주입 결함, judge 는 INCONSISTENT 로 잡아야)
CASES = [
    # ── 정합(통과해야) ──
    {"q": "김민지 야간 몇 개야?", "data": {"nurse": "김민지", "nights": 3},
     "answer": "김민지 간호사의 야간은 3개입니다.", "label": True, "note": "숫자 일치"},
    {"q": "이번 달 야간/주간 수는?", "data": {"nights": 3, "days": 10},
     "answer": "야간은 세 번, 주간은 열 번입니다.", "label": True, "note": "숫자 한글표기(paraphrase)"},
    {"q": "원티드 미제출자 있어?",
     "data": {"unsubmitted": [{"name": "김민지"}, {"name": "박지은"}]},
     "answer": "미제출자는 김민지, 박지은 두 분입니다.", "label": True, "note": "목록 요약"},
    {"q": "제출 상태 알려줘", "data": {"nurse": "박지은", "submitted": False},
     "answer": "박지은 간호사는 아직 미제출 상태입니다.", "label": True, "note": "상태 일치"},
    {"q": "간호사 몇 명이야?", "data": {"nurses": [1, 2, 3, 4, 5, 6]},
     "answer": "총 6명입니다.", "label": True, "note": "카운트 일치"},
    {"q": "김민지 등급은?", "data": {"nurse": "김민지", "grade": 2},
     "answer": "김민지 간호사의 등급은 2입니다.", "label": True, "note": "값 일치"},
    # ── 비정합(잡아야) ──
    {"q": "김민지 야간 몇 개야?", "data": {"nurse": "김민지", "nights": 3},
     "answer": "김민지 간호사의 야간은 5개입니다.", "label": False, "note": "숫자 환각"},
    {"q": "원티드 미제출자 있어?", "data": {"nurses": [{"name": "김민지"}, {"name": "박지은"}]},
     "answer": "미제출자는 김민지, 박지은, 이수정 세 분입니다.", "label": False, "note": "없는 이름 추가"},
    {"q": "제출 상태 알려줘", "data": {"nurse": "박지은", "submitted": False},
     "answer": "박지은 간호사는 제출 완료 상태입니다.", "label": False, "note": "상태 반전"},
    {"q": "변경했어?", "data": {"ok": False, "error": "간호사를 찾을 수 없습니다"},
     "answer": "변경이 완료되었습니다.", "label": False, "note": "실패인데 완료(거짓완료)"},
    {"q": "김민지 등급은?", "data": {"nurse": "김민지", "grade": 2},
     "answer": "박지은 간호사의 등급은 2입니다.", "label": False, "note": "다른 간호사 언급"},
    {"q": "간호사 몇 명이야?", "data": {"nurses": [1, 2, 3, 4, 5, 6]},
     "answer": "총 9명입니다.", "label": False, "note": "카운트 환각"},
]


def run(limit=None):
    if not os.getenv("OPENAI_API_KEY"):
        print("OPENAI_API_KEY 없음 — 라이브 평가 불가"); return None
    llm = get_router_llm_client("openai")
    cases = CASES[: limit or len(CASES)]
    tp = fp = tn = fn = 0
    rows = []
    for c in cases:
        r = judge_answer_consistency(llm, c["q"], c["data"], c["answer"])
        pred_consistent = r.consistent
        if c["label"] is False:  # 결함(비정합)
            if not pred_consistent:
                tp += 1; verdict = "TP✓ 결함 포착"
            else:
                fn += 1; verdict = "FN✗ 결함 놓침"
        else:  # 정상(정합)
            if pred_consistent:
                tn += 1; verdict = "TN✓ 정상 통과"
            else:
                fp += 1; verdict = "FP✗ 정상 오탐"
        rows.append((verdict, c["note"], r.reason))

    recall = tp / (tp + fn) if (tp + fn) else 0.0
    fp_rate = fp / (fp + tn) if (fp + tn) else 0.0
    precision = tp / (tp + fp) if (tp + fp) else 0.0

    print("\n=== L2 answer-consistency judge — fault-injection 평가 ===")
    for v, note, reason in rows:
        tail = f"  ← {reason}" if reason else ""
        print(f"  {v:14s} | {note}{tail}")
    print("\n--- 집계 ---")
    print(f"  Recall(결함 포착)     : {recall*100:5.1f}%  (TP={tp}, FN={fn})")
    print(f"  False-positive(오탐)  : {fp_rate*100:5.1f}%  (FP={fp}, TN={tn})")
    print(f"  Precision             : {precision*100:5.1f}%")
    return {"recall": recall, "fp_rate": fp_rate, "precision": precision,
            "tp": tp, "fn": fn, "fp": fp, "tn": tn}


if __name__ == "__main__":
    run()
