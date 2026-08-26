"""근무코드 메타 추론 PoC — 근무표 격자만 보고 default_shift(D/E/N/O/M)를 맞힐 수 있나.

배경: 신규 병원은 근무코드의 시간·타입 정보 없이 근무표만 보내는 경우가 많다. 사람이
격자를 보고 "HD 는 Day 계열" 이라고 추론해 왔는데, 그걸 자동화할 수 있는지 실측한다.

★ 이름으로 맞히지 않는다. 'D'/'데이' 같은 이름 매칭은 신규 병원에서 안 통하는 게 문제의
  전제이므로, **격자 분포만** 쓴다(코드 문자열은 식별자로만 사용).

★ 정확도보다 **abstain(모르면 비움)** 이 중요하다. 틀리면 조용히 틀린다 — 근무표는
  만들어지고 커버리지만 어긋난다. 그래서 확신 없는 건 안 찍는 게 맞다.

실행: PYTHONPATH=app python tests/agent_qa/shift_infer_poc.py [--wards N]
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "app"))

from db.client2 import SessionLocal          # noqa: E402
from db.models import Schedule, ScheduleEntry, Shift  # noqa: E402


# ── 격자 특징 추출 ────────────────────────────────────────


def build_grid(db, schedule_id):
    """schedule → {nurse_id: {day: code}}."""
    rows = (
        db.query(ScheduleEntry.nurse_id, ScheduleEntry.work_date, ScheduleEntry.shift_id)
        .filter(ScheduleEntry.schedule_id == schedule_id)
        .all()
    )
    grid = defaultdict(dict)
    for nid, wd, sid in rows:
        if wd is None or not sid:
            continue
        grid[str(nid)][wd.day] = str(sid)
    return grid


def code_features(grid):
    """코드별 분포 특징. 이름은 안 본다."""
    total_cells = 0
    freq = Counter()
    next_of = defaultdict(Counter)   # c → 다음날 코드 분포
    runs = defaultdict(list)         # c → 연속 등장 길이들
    per_day = defaultdict(Counter)   # c → {day: 인원수}

    for _nid, days in grid.items():
        if not days:
            continue
        dmax = max(days)
        seq = [days.get(d) for d in range(1, dmax + 1)]
        cur, run = None, 0
        for i, c in enumerate(seq):
            if c:
                total_cells += 1
                freq[c] += 1
                per_day[c][i + 1] += 1
                nxt = seq[i + 1] if i + 1 < len(seq) else None
                if nxt:
                    next_of[c][nxt] += 1
            if c == cur:
                run += 1
            else:
                if cur:
                    runs[cur].append(run)
                cur, run = c, 1
        if cur:
            runs[cur].append(run)

    feats = {}
    n_nurses = max(len(grid), 1)
    for c, f in freq.items():
        nx = next_of[c]
        nx_tot = sum(nx.values()) or 1
        rl = runs[c] or [1]
        feats[c] = {
            "freq": f,
            "share": f / max(total_cells, 1),
            "avg_headcount": f / max(len(per_day[c]), 1),
            "headcount_ratio": (f / max(len(per_day[c]), 1)) / n_nurses,
            "self_next": nx[c] / nx_tot,              # 자기 자신이 다음날 올 확률
            "next_dist": {k: v / nx_tot for k, v in nx.items()},
            "next_raw": dict(nx),   # 원시 횟수 — 비율만 보면 소규모 병동에서 요동친다
            "avg_run": sum(rl) / len(rl),
            "max_run": max(rl),
        }
    return feats


# ── 추론 ─────────────────────────────────────────────────

ABSTAIN = None


def infer(feats):
    """격자 특징 → {code: (추론값 or None, 확신도, 근거)}.

    ★ v2 — 핵심 신호는 '다음날 무엇이 오는가'가 아니라 **전이 비대칭**이다.
      v1 은 "N 다음날 OFF" 를 봤는데, N 은 연속으로 붙어서 다음날이 또 N 이라 안 잡혔다
      (실측: N→N 56% / N→O 41%). 대신 격자에는 훨씬 강한 지문이 찍혀 있다 —
      **이브닝 다음날 데이 금지(banned_day_after_eve)** 가 하드 제약이라 E→D 가 0% 다.
      D→E 는 33% 인데. 이 비대칭이 근무 순서를 복원해 준다: D → E → N → O.

    판별:
      1) OFF — 일평균 등장 인원 비율 최대(휴무는 매일 많이 나온다).
      2) 근무 코드들 사이의 **방향성 그래프**를 만든다. a→b 가 b→a 보다 압도적이면
         a 가 앞선 근무다. 위상 순서 = 출근 시각 순서(D→E→N).
      3) 순서가 3개면 D/E/N, 2개면 앞뒤만(D/N 로 단정하지 않고 보류), 애매하면 보류.
      4) 휴가·단발 코드는 제외(자기연속 0 + 평균런 1.0 + 저빈도).
    """
    if not feats:
        return {}
    out = {}

    # 1) OFF — ★ 앵커다. 여기서 틀리면 뒤가 통째로 뒤집힌다.
    #   실측(25병동): OFF 를 argmax 로만 고르면 근무 코드가 뽑히는 병동이 있고
    #   (D 가 일평균 29% 로 최대), 그 경우 근무 순서까지 역전돼 오답이 연쇄했다.
    #   그래서 **2위와의 마진**을 요구하고, 마진이 없으면 앵커를 포기한다
    #   (= 그 병동 전체를 보류. 틀린 앵커로 세 코드를 더 틀리는 것보다 낫다).
    cand = sorted(feats, key=lambda c: (feats[c]["headcount_ratio"], feats[c]["freq"]),
                  reverse=True)
    off = None
    if cand:
        top = cand[0]
        r1 = feats[top]["headcount_ratio"]
        r2 = feats[cand[1]]["headcount_ratio"] if len(cand) > 1 else 0.0
        margin = r1 - r2
        if r1 >= 0.20 and margin >= 0.05:
            off = top
            out[off] = ("O", round(min(0.5 + margin * 2, 0.99), 2),
                        f"일평균 등장 비율 {r1:.0%}, 2위({r2:.0%})와 {margin:.0%}p 차이")
        else:
            # 앵커 불가 → 전부 보류하고 끝낸다.
            return {c: (ABSTAIN, 0.0,
                        f"휴무 코드를 특정 못 함(1위 {r1:.0%} vs 2위 {r2:.0%}) — 기준점 없음")
                    for c in feats}

    # 휴가/단발 코드 제외 — 연속되지 않고 드물게 하루씩 등장한다.
    def is_sporadic(f):
        return f["avg_run"] <= 1.05 and f["share"] < 0.05

    work = [c for c, f in feats.items()
            if c != off and not is_sporadic(f) and f["share"] >= 0.03]
    for c, f in feats.items():
        if c != off and c not in work:
            out[c] = (ABSTAIN, 0.0, "연속되지 않는 저빈도 코드(휴가·단발 성격)")

    # 2) 전이 비대칭 → 방향성
    def p(a, b):
        return feats[a]["next_dist"].get(b, 0.0)

    def raw(a, b):
        return feats[a]["next_raw"].get(b, 0)

    # ★ 원시 관측 수 게이트 — 비율만 보면 소규모 병동에서 요동친다.
    #   실측: 간호사 5명 병동에서 E 를 N 으로 오판(유일한 오답)했다. 155셀에서
    #   특정 쌍의 전이가 한두 번이면 비율은 의미가 없다.
    MIN_OBS = 8

    order_score = {c: 0.0 for c in work}   # 클수록 앞(이른 시각)
    for a in work:
        for b in work:
            if a >= b:
                continue
            n_obs = raw(a, b) + raw(b, a)
            if n_obs < MIN_OBS:
                continue                      # 근거 부족 — 이 쌍은 판단 보류
            fwd, bwd = p(a, b), p(b, a)
            if max(fwd, bwd) < 0.05:
                continue                      # 둘 다 거의 없음 — 정보 없음
            if fwd > bwd * 3:                 # a→b 만 허용 = a 가 앞
                order_score[a] += 1; order_score[b] -= 1
            elif bwd > fwd * 3:
                order_score[b] += 1; order_score[a] -= 1

    ranked = sorted(work, key=lambda c: -order_score[c])

    # 3) ★ 점수가 **실제로 갈린 자리만** 라벨을 붙인다.
    #    v2 는 상위 3개를 무조건 D/E/N 에 배정해 오답 9.8% 가 났다. 오답 전부가
    #    '점수 +0'(= 순서 근거 없음)이었다 — 동점인데 억지로 세운 것이다.
    #    이 문제는 정확도보다 **조용히 틀리지 않는 것**이 중요하므로, 동점이거나
    #    점수가 0 이면 커버리지를 포기하고 보류한다.
    labels = ["D", "E", "N"]
    for i, c in enumerate(ranked):
        if i >= len(labels):
            out[c] = (ABSTAIN, 0.0,
                      f"주요 3교대 밖(점수 {order_score[c]:+.0f}) — 고정·특수 근무 가능")
            continue
        sc_c = order_score[c]
        # 앞/뒤 이웃과 점수가 같으면 순서가 안 갈린 것 → 보류.
        prev_tie = i > 0 and order_score[ranked[i - 1]] == sc_c
        next_tie = i + 1 < len(ranked) and order_score[ranked[i + 1]] == sc_c
        if sc_c == 0 or prev_tie or next_tie:
            why = "동점이라 순서 미확정" if (prev_tie or next_tie) else "전이 비대칭 없음"
            out[c] = (ABSTAIN, 0.0, f"{why}(점수 {sc_c:+.0f}) — 시간 정보 필요")
            continue
        out[c] = (labels[i], 0.85,
                  f"근무 순서 {i+1}위(전이 비대칭 점수 {sc_c:+.0f}, 이웃과 분리됨)")

    for c in feats:
        out.setdefault(c, (ABSTAIN, 0.0, "판별 근거 없음"))
    return out


# ── 평가 ─────────────────────────────────────────────────


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wards", type=int, default=12)
    ap.add_argument("--min-truth", type=int, default=3, help="정답 보유 코드 최소 개수")
    args = ap.parse_args()

    db = SessionLocal()
    scheds = (
        db.query(Schedule)
        .filter(Schedule.dropped == False)  # noqa: E712
        .order_by(Schedule.year.desc(), Schedule.month.desc())
        .limit(400)
        .all()
    )

    seen_groups, picked = set(), []
    for sc in scheds:
        if sc.group_id in seen_groups:
            continue
        truth = {
            s.shift_id: (s.default_shift or "").strip().upper()
            for s in db.query(Shift).filter(Shift.group_id == sc.group_id).all()
            if (s.default_shift or "").strip()
        }
        if len(truth) < args.min_truth:
            continue
        seen_groups.add(sc.group_id)
        picked.append((sc, truth))
        if len(picked) >= args.wards:
            break

    print(f"평가 대상: {len(picked)} 병동(각 최신 근무표 1개)\n")
    tot = correct = wrong = abst = 0
    wrong_cases, per_label = [], defaultdict(lambda: [0, 0])

    for sc, truth in picked:
        grid = build_grid(db, sc.schedule_id)
        if len(grid) < 5:
            continue
        pred = infer(code_features(grid))
        n_ok = n_ng = n_ab = 0
        for code, gt in truth.items():
            if code not in pred:
                continue          # 그 달 표에 안 쓰인 코드 — 추론 대상 아님
            tot += 1
            p, conf, why = pred[code]
            if p is None:
                abst += 1; n_ab += 1
            elif p == gt:
                correct += 1; n_ok += 1; per_label[gt][0] += 1
            else:
                wrong += 1; n_ng += 1; per_label[gt][1] += 1
                wrong_cases.append((sc.group_id, code, gt, p, conf, why))
        print(f"  {sc.group_id} {sc.year}-{sc.month:02d} "
              f"간호사 {len(grid):2}명 | 정답보유 {len(truth):3} | "
              f"맞음 {n_ok:2} 틀림 {n_ng:2} 보류 {n_ab:2}")

    print(f"\n{'='*62}\n판정 대상 {tot}건")
    if tot:
        dec = correct + wrong
        print(f"  맞음   {correct:4} ({correct/tot:.1%})")
        print(f"  틀림   {wrong:4} ({wrong/tot:.1%})   ← 조용히 틀리는 것")
        print(f"  보류   {abst:4} ({abst/tot:.1%})   ← 사람에게 넘김")
        if dec:
            print(f"  판정한 것 중 정확도: {correct/dec:.1%}  (n={dec})")
    print("\n라벨별 (맞음/틀림):")
    for lab, (ok, ng) in sorted(per_label.items()):
        print(f"  {lab:3} {ok:4} / {ng:4}")
    if wrong_cases:
        print("\n오답 샘플(최대 10):")
        for g, c, gt, p, conf, why in wrong_cases[:10]:
            print(f"  {g} {c:12} 정답={gt} 추론={p} conf={conf} — {why}")
    db.close()


if __name__ == "__main__":
    main()
