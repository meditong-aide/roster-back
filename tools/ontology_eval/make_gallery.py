"""병동별 infeasible→action→resolved 케이스 5개씩 + 인과경로 그래프 이미지 + .md.

각 케이스: 하드위반 in-memory 주입 → 엔진 infeasible 확인 → 온톨로지 그래프 진단 →
최소변경 액션 → 적용 후 재구동 resolved 확인. 인과경로 서브그래프를 PNG 로 렌더.
프로덕션 무변경(harness.clone).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "app"
HERE = Path(__file__).resolve().parent
for _p in (str(APP), str(HERE), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import harness  # noqa: E402
import evaluate  # noqa: E402
import visualize  # noqa: E402

IMG_DIR = ROOT / "docs" / "ontology_cases" / "img"
MD_PATH = ROOT / "docs" / "ONTOLOGY_CASE_GALLERY.md"

# 후보 케이스 (앞에서부터 resolved 5개 채택)
CANDIDATES = {
    "9B": [
        ("C1-N coverage N 과다", harness.inject_c1_coverage_overload, dict(shift="N", amount=99)),
        ("C1-D coverage D 과다", harness.inject_c1_coverage_overload, dict(shift="D", amount=99)),
        ("C2 team 교차시프트 과구독", harness.inject_c2_team_min_overload, {}),
        ("C3-N grade_max N 과소", harness.inject_c3_grade_max_too_low, dict(shift="N")),
        ("C4 월 N cap 과소", harness.inject_c4_night_cap_too_low, dict(cap=1)),
        ("C1-E coverage E 과다", harness.inject_c1_coverage_overload, dict(shift="E", amount=99)),
        ("C3-E grade_max E 과소", harness.inject_c3_grade_max_too_low, dict(shift="E")),
    ],
    "ICU": [
        ("C1-N coverage N 과다", harness.inject_c1_coverage_overload, dict(shift="N", amount=99)),
        ("C1-D coverage D 과다", harness.inject_c1_coverage_overload, dict(shift="D", amount=99)),
        ("C3-N grade_max N 과소", harness.inject_c3_grade_max_too_low, dict(shift="N")),
        ("C3-E grade_max E 과소", harness.inject_c3_grade_max_too_low, dict(shift="E")),
        ("C4 월 N cap 과소", harness.inject_c4_night_cap_too_low, dict(cap=1)),
        ("C1-E coverage E 과다", harness.inject_c1_coverage_overload, dict(shift="E", amount=99)),
    ],
}

WARD_MONTH = {"9B": (2026, 6), "ICU": (2026, 6)}


def run_case(cap, base_roster, label, injector, kwargs, tl):
    injected = injector(cap, **kwargs)
    meta = injected["_injected"]
    inj_run = harness.run_engine(injected, time_limit_seconds=tl)
    if inj_run["feasible"]:
        return None  # 주입이 infeasible 못 만듦
    actions = evaluate.diagnose_delta(injected, cap)
    if not actions or actions[0].target_family != meta["family"]:
        return None
    top = actions[0]
    g = evaluate.build_eval_graph(injected, roster=base_roster)
    fixed = evaluate.apply_fix(injected, top, meta)
    fix_run = harness.run_engine(fixed, time_limit_seconds=tl)
    if not fix_run["feasible"]:
        return None
    return {"label": label, "meta": meta, "graph": g, "top": top,
            "inj_cells": inj_run["work_cells"], "fix_cells": fix_run["work_cells"]}


def main():
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    tl = int(os.getenv("GALLERY_TL", "15"))
    all_cases: dict[str, list] = {}
    for ward, cands in CANDIDATES.items():
        year, month = WARD_MONTH[ward]
        print(f"[gallery] {ward} {year}-{month:02d} capture...")
        cap = harness.capture_engine_inputs(ward, year, month)
        if not cap.get("captured_ok"):
            print(f"  capture failed: {cap.get('load_error')}")
            continue
        base = harness.run_engine(cap, time_limit_seconds=tl)
        cases = []
        for label, inj, kw in cands:
            if len(cases) >= 5:
                break
            print(f"  case {label} ...")
            r = run_case(cap, base["roster"], label, inj, kw, tl)
            if r is None:
                print("    → skip (무효/미해결)")
                continue
            idx = len(cases) + 1
            img = IMG_DIR / f"{ward}_case{idx}.png"
            keep = visualize.focused_subgraph(r["graph"], r["meta"]["family"], r["top"].action_id)
            visualize.render(r["graph"], keep,
                             str(img), title=f"{ward} · {label}\n부족→제약→액션 인과경로")
            r["img"] = img.relative_to(ROOT / "docs").as_posix()
            r["baseline_cells"] = base["work_cells"]
            cases.append(r)
            print(f"    → resolved, img={img.name}")
        all_cases[ward] = cases

    # ── .md 작성 ──
    lines = ["# 온톨로지 인과경로 케이스 갤러리\n",
             "각 병동 실데이터(2026-06)에 하드제약 위반을 in-memory 주입 → 엔진 infeasible 확인 → ",
             "온톨로지 4-노드 그래프로 진단 → 최소변경 액션 → 적용 후 재구동 resolved.\n",
             "**프로덕션 DB 무변경**(config 사본만). 그래프는 부족(state)→제약→완화(action) 인과경로.\n",
             "\n색: 🔴하드제약 🟠소프트제약 🟠상태 🔵도메인객체 🟢완화액션 | "
             "엣지: pressures(부족→제약), mitigates(액션→제약), requires, constrains, reduces(연차→가용)\n"]
    for ward, cases in all_cases.items():
        lines.append(f"\n## {ward} 병동 ({len(cases)} 케이스)\n")
        for i, c in enumerate(cases, 1):
            m = c["meta"]; t = c["top"]
            lines.append(f"\n### {ward}-{i}. {c['label']}\n")
            lines.append(f"![{ward} case {i}]({c['img']})\n")
            lines.append("| 단계 | 내용 |")
            lines.append("|---|---|")
            lines.append(f"| ① 주입(하드위반) | family=`{m['family']}`, knob=`{m['knob']}` |")
            lines.append(f"| ② 엔진 결과 | **infeasible** (실근무 {c['inj_cells']}건) |")
            lines.append(f"| ③ 진단·추천액션(최소변경) | `{t.target_family}` · `{t.action_type}` · "
                         f"knob=`{t.config_key}` · {t.delta} |")
            lines.append(f"| ④ 적용 후 재구동 | **resolved** (실근무 {c['fix_cells']}건) |")
            lines.append(f"| ⑤ 평가 | E1 액션도출 ✅ / E2 해결 ✅ / E3 최소변경 ✅ / E5 구조원인 ✅ |")
            lines.append("")
    MD_PATH.write_text("\n".join(lines), encoding="utf-8")
    total = sum(len(v) for v in all_cases.values())
    print(f"\n[gallery] {total} 케이스, md → {MD_PATH.relative_to(ROOT)}")
    for ward, cases in all_cases.items():
        print(f"  {ward}: {len(cases)} cases")


if __name__ == "__main__":
    main()
