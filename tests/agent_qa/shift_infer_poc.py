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
    after_run = defaultdict(Counter)  # c → **연속이 끝난 직후** 코드 분포(회복 오프 지문)
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
                    if c:
                        after_run[cur][c] += 1
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
            "after_run": {k: v / max(sum(after_run[c].values()), 1)
                          for k, v in after_run[c].items()},
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

    # 2) 전이 비대칭 → **방향 그래프**
    #   ★ v6 — v5 는 +1/-1 을 **합산**했는데, 그게 신호를 스스로 지웠다.
    #     E 는 (D→E)에서 -1, (E→N)에서 +1 을 받아 합이 0 → "근거 없음"으로 보류됐다.
    #     실제로는 D 뒤·N 앞이라는 순서가 완벽히 정해지는데도. 합산은 위치 정보를 버린다.
    #     그래서 점수 대신 **간선을 세우고 위상 정렬**한다.
    def p(a, b):
        return feats[a]["next_dist"].get(b, 0.0)

    def raw(a, b):
        return feats[a]["next_raw"].get(b, 0)

    # 원시 관측 수 게이트 — 소규모 병동에서 비율이 요동친다.
    MIN_OBS = 8

    edges = set()          # (a, b) = a 가 b 보다 앞선 근무
    for a in work:
        for b in work:
            if a >= b:
                continue
            if raw(a, b) + raw(b, a) < MIN_OBS:
                continue
            fwd, bwd = p(a, b), p(b, a)
            if max(fwd, bwd) < 0.05:
                continue
            if fwd > bwd * 3:
                edges.add((a, b))
            elif bwd > fwd * 3:
                edges.add((b, a))

    # 위상 정렬 — 매 단계에서 **후보가 정확히 1개**일 때만 진행한다.
    #   2개 이상이면 그 자리 순서가 안 갈린 것이므로 거기서 멈춘다(억지로 세우지 않음).
    remaining = set(work)
    chain = []
    while remaining:
        heads = [c for c in remaining
                 if not any((o, c) in edges for o in remaining if o != c)]
        if len(heads) != 1:
            break                      # 동시 후보 다수 = 순서 미확정 → 중단
        h = heads[0]
        chain.append(h)
        remaining.discard(h)

    # 3) ★ v7 — 라벨을 사슬 **앞**이 아니라 **나이트 기준**으로 붙인다.
    #   v6 는 사슬 1·2·3위를 무조건 D·E·N 으로 찍었는데, 사슬 맨 앞에 3교대가 아닌
    #   코드(미드·고정근무)가 끼면 **한 칸씩 밀려** 통째로 틀렸다(실측 오답 2건이 그것).
    #   나이트는 독립적인 지문이 있다 — **연속이 끝나면 반드시 휴무**(회복 오프).
    #   그걸로 뒤에서 앵커를 잡고 거꾸로 D·E 를 센다.
    def recovery_off(c):
        return feats[c]["after_run"].get(off, 0.0) if off else 0.0

    night_idx = None
    for i, c in enumerate(chain):
        # 나이트 후보: 연속이 끝나면 휴무로 가는 비율이 압도적 + 실제로 연속으로 선다
        if recovery_off(c) >= 0.70 and feats[c]["avg_run"] >= 1.5:
            night_idx = i          # 사슬 뒤쪽일수록 나이트에 가까우므로 마지막 후보 채택
    linked_any = {c: any((c, o) in edges or (o, c) in edges
                         for o in chain if o != c) for c in chain}

    if night_idx is None:
        for c in chain:
            out[c] = (ABSTAIN, 0.0, "나이트 기준점을 못 찾음(회복 오프 지문 없음)")
    else:
        # night_idx 를 N 으로 두고 앞으로 E, D 를 센다.
        label_at = {night_idx: "N"}
        if night_idx - 1 >= 0:
            label_at[night_idx - 1] = "E"
        if night_idx - 2 >= 0:
            label_at[night_idx - 2] = "D"
        for i, c in enumerate(chain):
            lab = label_at.get(i)
            if lab is None:
                out[c] = (ABSTAIN, 0.0, "3교대 사슬 밖 — 고정·특수 근무 가능")
            elif not linked_any[c]:
                out[c] = (ABSTAIN, 0.0, "다른 근무와의 선후 근거 없음")
            else:
                out[c] = (lab, 0.85,
                          f"나이트({chain[night_idx]}) 기준 {night_idx - i}칸 앞"
                          if lab != "N" else
                          f"연속 종료 후 휴무 {recovery_off(c):.0%} → 나이트")
    for c in remaining:
        out[c] = (ABSTAIN, 0.0, "순서 후보가 여럿이라 미확정 — 시간 정보 필요")

    for c in feats:
        out.setdefault(c, (ABSTAIN, 0.0, "판별 근거 없음"))
    return out


# ── 평가 ─────────────────────────────────────────────────


def evaluate(db, picked, *, consensus: bool):
    """한 설정으로 전 병동을 채점. (지표 dict, 흔들림 목록, 상시오답 목록) 반환.

    consensus=True — ★ Fix B: 같은 병동·같은 코드에서 **여러 표의 답이 일치할 때만**
      확정한다. 하나라도 다르면 전부 보류로 내린다. 표마다 답이 달라지는 판정은
      근거가 약한 것이고, 그런 건 사람에게 넘기는 게 맞다.
    """
    tot = correct = wrong = abst = 0
    per_label = defaultdict(lambda: [0, 0])
    unstable, always_wrong = [], []

    for gid, truth, scs in picked:
        votes = defaultdict(list)
        for sc in scs:
            grid = build_grid(db, sc.schedule_id)
            if len(grid) < 5:
                continue
            pred = infer(code_features(grid))
            for code in truth:
                if code in pred:
                    votes[code].append(pred[code][0])

        for code, vs in votes.items():
            gt = truth[code]
            decided = {v for v in vs if v is not None}
            if len(decided) > 1:
                unstable.append((gid, code, gt, sorted(decided), len(vs)))
            elif decided and decided != {gt}:
                always_wrong.append((gid, code, gt, next(iter(decided)), len(vs)))

            # Fix B: 표들의 답이 갈리면 전부 보류로 내린다.
            eff = [None] * len(vs) if (consensus and len(decided) > 1) else vs
            for v in eff:
                tot += 1
                if v is None:
                    abst += 1
                elif v == gt:
                    correct += 1; per_label[gt][0] += 1
                else:
                    wrong += 1; per_label[gt][1] += 1

    dec = correct + wrong
    return ({
        "tot": tot, "correct": correct, "wrong": wrong, "abst": abst,
        "acc": (correct / dec) if dec else 0.0, "dec": dec,
        "labels": dict(per_label),
    }, unstable, always_wrong)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wards", type=int, default=20)
    ap.add_argument("--per-ward", type=int, default=5)
    ap.add_argument("--min-truth", type=int, default=3)
    args = ap.parse_args()

    db = SessionLocal()
    gids = [g for (g,) in db.query(Schedule.group_id)
            .filter(Schedule.dropped == False)  # noqa: E712
            .distinct().all() if g]

    picked = []
    for gid in gids:
        truth = {
            s.shift_id: (s.default_shift or "").strip().upper()
            for s in db.query(Shift).filter(Shift.group_id == gid).all()
            if (s.default_shift or "").strip()
        }
        if len(truth) < args.min_truth:
            continue
        scs = (db.query(Schedule)
               .filter(Schedule.group_id == gid, Schedule.dropped == False)  # noqa: E712
               .order_by(Schedule.year.desc(), Schedule.month.desc(),
                         Schedule.version.desc())
               .limit(args.per_ward).all())
        if scs:
            picked.append((gid, truth, scs))
        if len(picked) >= args.wards:
            break

    n_sched = sum(len(x[2]) for x in picked)
    print(f"평가: {len(picked)} 병동 × 최대 {args.per_ward}표 = 근무표 {n_sched}개\n")

    # ★ ablation 기록(2026-09-09, 20병동×5표=309건):
    #     baseline            맞음 114 / 틀림  5 / 보류 190 / 판정정확도 95.8%
    #     +휴무집합 앵커       맞음 129 / 틀림 52 / 보류 128 / 판정정확도 71.3%  ← 폐기
    #     +여러표 합의         맞음 114 / 틀림  2 / 보류 193 / 판정정확도 98.3%  ← 채택
    #   휴무집합 안(양방향 인접으로 묶기)은 **근무↔휴무도 양방향 인접**이라 D·E 가
    #   휴무로 빨려 들어갔다(D 20/3 → 8/24). 관측은 맞았고 묶는 기준이 틀렸다.
    arms = [
        ("합의 없음 (baseline)", dict(consensus=False)),
        ("여러표 합의 (채택)",   dict(consensus=True)),
    ]
    print(f"{'설정':22} {'맞음':>6} {'틀림':>6} {'보류':>6} {'판정정확도':>10} {'흔들림':>6} {'상시오답':>7}")
    print("─" * 70)
    results = {}
    for name, kw in arms:
        m, unstable, aw = evaluate(db, picked, **kw)
        results[name] = (m, unstable, aw)
        print(f"{name:22} {m['correct']:5}  {m['wrong']:5}  {m['abst']:5}  "
              f"{m['acc']:9.1%}  {len(unstable):5}  {len(aw):6}")

    print(f"\n(판정 대상 {results['합의 없음 (baseline)'][0]['tot']}건 기준)")
    print("\n라벨별 (맞음/틀림) — 합의 전 → 합의 후")
    b = results["합의 없음 (baseline)"][0]["labels"]
    f = results["여러표 합의 (채택)"][0]["labels"]
    for lab in sorted(set(b) | set(f)):
        bo, bn = b.get(lab, [0, 0]); fo, fn = f.get(lab, [0, 0])
        print(f"  {lab:3} {bo:4}/{bn:<3} → {fo:4}/{fn:<3}")

    for name in ("합의 없음 (baseline)", "여러표 합의 (채택)"):
        _m, unstable, aw = results[name]
        print(f"\n[{name}] 흔들림 {len(unstable)} · 상시오답 {len(aw)}")
        for gid, c, gt, ds, n in unstable[:5]:
            print(f"   흔들림 {gid} {c:10} 정답={gt} → {ds}")
        for gid, c, gt, d, n in aw[:5]:
            print(f"   상시오답 {gid} {c:10} 정답={gt} 추론={d}")
    db.close()


if __name__ == "__main__":
    main()
