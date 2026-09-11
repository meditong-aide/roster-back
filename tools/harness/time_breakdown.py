"""생성 소요 변동이 **어느 패스에서** 나는가 — 8절 3번을 닫는 직접 경로.

9A 에서 28→66초 변동이 관측됐다. 지금까지 배제된 원인:
  · 시간 리밋 — S1 측정(현행 430s 대 데드라인 432s)으로 배제.
    ★ 단 그 측정은 **평균**이라, 66초 회차 하나가 리밋에 걸렸어도 묻힌다. 이번에 회차별로 본다.
  · lex 패스 콜드스타트 — 현행 로그에서 lex 첫 해가 0.19~0.77초라 초 단위 미만 차이다.

추측을 멈추고 **패스별로 분해**한다. 세 축을 함께 찍는다.

  [1] 패스별 소요 — lex 는 합계가 아니라 **7패스를 따로**. 합이 원인으로 나오면
      어차피 패스별로 다시 봐야 하므로 처음부터 나눠 둔다.
  [2] 종료 사유 — 증명 / 자명 / gap발화 / 시간리밋 / UNKNOWN.
      ★ 목적값이 이미 0 이라 즉시 OPTIMAL 인 패스는 **'자명'** 으로 따로 센다.
        증명으로 세면 이중분포 판정이 희석된다.
  [3] 마지막 개선 시각 — 시간리밋으로 끝난 구간에서 **처치가 정반대로 갈린다**:
        리밋 직전까지 개선 중이었다  → 시간을 더 줘야 하는 구간
        초반에 개선이 멈추고 bound 만 올랐다 → 이미 최적인데 증명을 못 한 것 → 끊어도 된다

★ `log_search_progress` 를 켜므로 절대 소요에 약간의 오버헤드가 얹힌다.
  모든 회차·구간에 동일하게 얹히므로 **변동폭 비교**는 유효하다. 절대값은 참고로만.
"""
import contextlib, io, os, re, statistics, sys, time
from collections import Counter, defaultdict

sys.path.insert(0, "app")
from sqlalchemy import text  # noqa: E402
from db.client2 import SessionLocal  # noqa: E402
from schemas.roster_schema import RosterRequest  # noqa: E402
import services.roster_create_service as RCS  # noqa: E402
import services.cp_sat.fallback_lex as FL  # noqa: E402
import importlib.util as _iu  # noqa: E402

_sp = _iu.spec_from_file_location(
    "va", os.path.join(os.path.dirname(os.path.abspath(__file__)), "verify_all.py"))
va = _iu.module_from_spec(_sp)
_sp.loader.exec_module(va)

IMPROVE = re.compile(r"^#(\d+)\s+([\d.]+)s")     # 해 개선 로그 — 마지막 것의 시각이 필요하다
_orig = FL._solve_traced
CALLS = []


def _end_reason(solver, status_text: str, obj, bound) -> str:
    if status_text == "OPTIMAL":
        # ★ 목적값이 이미 0 이면 풀 것이 없었다는 뜻 — '증명' 과 섞으면 이중분포가 희석된다.
        if obj is not None and abs(obj) < 1e-9:
            return "자명"
        # ★★ CP-SAT 은 `relative_gap_limit` 이 충족돼 끝나도 **OPTIMAL 을 반환**한다
        #   ("증명" 이 아니라 "허용 오차 안" 인데 상태값이 같다).
        #   그래서 status 만 보면 gap 발화가 전부 '증명' 으로 잡히고, 그 결과
        #   `gap발화` 항목이 거의 안 나온다(실측에서 실제로 0 건이었다).
        #   obj 와 bound 가 다르면 증명이 아니라 gap 발화다.
        if obj is not None and bound is not None and abs(obj - bound) > 1e-6:
            return "gap발화"
        return "증명"
    if status_text in ("INFEASIBLE", "MODEL_INVALID"):
        return status_text
    try:
        wall = float(solver.WallTime())
        limit = float(solver.parameters.max_time_in_seconds)
    except Exception:
        return status_text
    if limit > 0 and wall >= limit * 0.95:
        return "시간리밋"
    if status_text == "FEASIBLE":
        return "gap발화"       # 리밋 전에 FEASIBLE 로 끝났으면 gap 조건이 끊은 것
    return status_text


def _traced(solver, model, prefix, phase):
    lines = []
    solver.parameters.log_search_progress = True
    solver.parameters.log_to_stdout = False
    try:
        solver.log_callback = lines.append
    except Exception:
        pass
    t0 = time.perf_counter()
    st = _orig(solver, model, prefix, phase)
    sec = time.perf_counter() - t0
    txt = FL._cp_sat_status_to_text(st)
    obj = None
    try:
        obj = float(solver.ObjectiveValue())
    except Exception:
        pass
    last_improve = None
    for ln in lines:
        m = IMPROVE.match(ln.strip())
        if m:
            last_improve = float(m.group(2))
    wall = None
    try:
        wall = float(solver.WallTime())
    except Exception:
        pass
    bound = None
    try:
        bound = float(solver.BestObjectiveBound())
    except Exception:
        pass
    CALLS.append({"phase": phase, "sec": sec, "status": txt,
                  "reason": _end_reason(solver, txt, obj, bound),
                  "last_improve": last_improve, "wall": wall,
                  "obj": obj, "bound": bound})
    return st


FL._solve_traced = _traced

ONLY = [x.strip() for x in (os.getenv("G", "") or "").split(",") if x.strip()]
GROUPS = [(n, g) for n, g in va.ALL if not ONLY or g in ONLY or n in ONLY] or [("시화9A", "101358f6de7b")]
REPS = int(os.getenv("R", "6"))

# ── 4팔 교차 실행 ── 블록 비교 금지. 같은 회차 안에서 네 조건을 연달아 돌린다.
#   처치 게이트가 서로 독립이라 기여가 갈린다:
#     AIDE_LEX_STALL  정체 종료 + stage3 이월
#     AIDE_LEX_GAP    lex 의 relative 0 + absolute 0.99
#     AIDE_S3_SAFETY  (가) stage3 목적에 safety 추가
ARMS = [
    ("현행", {}),
    ("정체종료", {"AIDE_LEX_STALL": "1"}),
    ("gap0", {"AIDE_LEX_GAP": "1"}),
    ("가-safety", {"AIDE_S3_SAFETY": "1"}),
]
_ARM_KEYS = ("AIDE_LEX_STALL", "AIDE_LEX_GAP", "AIDE_S3_SAFETY")
if os.getenv("ARMS"):      # 쉼표로 팔 이름을 골라 부분 실행
    _want = {x.strip() for x in os.getenv("ARMS").split(",") if x.strip()}
    ARMS = [a for a in ARMS if a[0] in _want] or ARMS

print(f"  소요 분해 — {len(GROUPS)}곳 × {REPS}회 · 패스별 · 종료사유 · 마지막개선\n", flush=True)

per_pass = defaultdict(lambda: defaultdict(list))     # [병동][패스] -> [소요...]
reasons = defaultdict(lambda: defaultdict(Counter))   # [병동][패스] -> Counter(사유)
improves = defaultdict(lambda: defaultdict(list))     # [병동][패스] -> [(마지막개선, wall)...]
totals = defaultdict(list)
nonsolve = defaultdict(list)
seen_order: list = []
quality = defaultdict(list)
frozen = defaultdict(lambda: defaultdict(list))   # [병동][lex패스] -> [동결 목적값...]
QCRIT: dict = {}          # gid -> load_criteria 결과(하드제약 판정 기준). 병동당 1회만 읽는다

for name, gid in GROUPS:
    db = SessionLocal()
    try:
        user = va.make_user(db, gid)
        tm = db.execute(text("SELECT TOP 1 year, month FROM schedules WHERE group_id=:g "
                             "AND dropped=0 ORDER BY created_at DESC"), {"g": gid}).fetchone()
    finally:
        db.close()
    if user is None or tm is None:
        continue
    Y, M = int(tm[0]), int(tm[1])
    if gid not in QCRIT:
        db3 = SessionLocal()
        try:
            QCRIT[gid] = va.load_criteria(db3, gid, Y, M, user)
        except Exception:
            QCRIT[gid] = None
        finally:
            db3.close()
    for r in range(1, REPS + 1):
        CALLS.clear()
        db2 = SessionLocal()
        t0 = time.perf_counter()
        buf = io.StringIO()
        out = None
        try:
            with contextlib.redirect_stdout(buf):
                out = RCS.generate_roster_service(
                    RosterRequest(year=Y, month=M, group_id=gid), user, db2)
        except Exception as e:
            print(f"  {name} #{r} 생성실패: {type(e).__name__}: {str(e)[:50]}", flush=True)
            continue
        finally:
            try:
                db2.rollback()
            except Exception:
                pass
            db2.close()
        total = time.perf_counter() - t0
        # ── 품질 축 — 처치가 소요만 바꾸고 품질을 깎으면 이득이 아니다 ──
        log = buf.getvalue()
        _pm = re.search(r"\[PrefRate\] rate=([\d.]+) want=(\d+)/(\d+)", log)
        _fin = re.search(r"폴백 완료: 커버리지부족=(-?\d+), 안전위반합=(-?\d+)", log)
        rate = float(_pm.group(1)) if _pm else None
        want = (int(_pm.group(2)), int(_pm.group(3))) if _pm else None
        safe = int(_fin.group(2)) if _fin else -1
        hard = 0
        if isinstance(out, dict) and out.get("nurses"):
            try:
                crit = QCRIT[gid]
                for nu in out["nurses"]:
                    seq = [va.to_main(x, crit["id_to_main"], Counter()) or "-"
                           for x in (nu.get("schedule") or [])]
                    v, _ = va.check_nurse(seq, 0, len(seq) - 1, crit, 0, 0)
                    hard += sum(v.values())
            except Exception:
                hard = -1
        quality[name].append({"rate": rate, "want": want, "safe": safe, "hard": hard})
        acc = defaultdict(float)
        for c in CALLS:
            ph = c["phase"]
            if ph not in seen_order:
                seen_order.append(ph)
            acc[ph] += c["sec"]
            reasons[name][ph][c["reason"]] += 1
            # ★ lex 동결값 — 이 패스가 확정한 목적값이 다음 패스에 **고정**된다.
            #   `relative_gap_limit=0.15` 로 끊기면 최적보다 최대 15% 나쁜 값이 고정되므로,
            #   조건 간 이 값을 패스별로 대조해야 "gap 이 실제로 품질을 깎았는지" 가 갈린다.
            if ph.startswith("lex") and c["obj"] is not None:
                frozen[name][ph].append(c["obj"])
            if c["reason"] in ("시간리밋", "UNKNOWN"):
                improves[name][ph].append((c["last_improve"], c["wall"]))
        ns = max(0.0, total - sum(acc.values()))
        totals[name].append(total)
        nonsolve[name].append(ns)
        for ph, v in acc.items():
            per_pass[name][ph].append(v)
        top = sorted(acc.items(), key=lambda kv: -kv[1])[:4]
        brief = " ".join(f"{k}={v:.1f}" for k, v in top)
        print(f"  {name} #{r}  총 {total:>5.1f}s  비-solve {ns:>5.1f}s   상위: {brief}", flush=True)
        # ★ 회차별 종료 사유 — 집계만으로는 확장 여부까지만 판단되고, **임계값을 정할 때는
        #   그 병동 그 회차의 '마지막 개선 시각' 이 필요하다**(stage2 는 개선이 덩어리로 와서
        #   lex 와 같은 임계를 쓸 수 없다). stage2·stage3 는 값까지 함께 남긴다.
        _big = []
        for c in CALLS:
            if c["phase"] in ("stage2", "stage3"):
                _li = f"{c['last_improve']:.1f}" if c["last_improve"] is not None else "—"
                _w = f"{c['wall']:.1f}" if c["wall"] is not None else "—"
                _big.append(f"{c['phase']}={c['reason']}(개선{_li}/wall{_w})")
        _lexr = "/".join(f"{c['phase'].split(':')[0]}:{c['reason']}"
                         for c in CALLS if c["phase"].startswith("lex"))
        print(f"  {'':<12}└ {' · '.join(_big)}   lex[{_lexr}]", flush=True)

print(f"\n  ── 패스별 변동폭 ── 변동이 큰 패스가 원인이다\n", flush=True)
print(f"  {'병동':<8}{'패스':<20}{'최소':>7}{'최대':>7}{'폭':>7}{'중앙':>7}{'총대비':>8}", flush=True)
print("  " + "-" * 64, flush=True)
for name, d in per_pass.items():
    tot_span = max(totals[name]) - min(totals[name]) if totals[name] else 0
    rows = []
    for ph, v in d.items():
        if len(v) < 2:
            v = v * 2
        rows.append((max(v) - min(v), ph, min(v), max(v), statistics.median(v)))
    ns = nonsolve[name]
    rows.append((max(ns) - min(ns), "비-solve", min(ns), max(ns), statistics.median(ns)))
    rows.sort(reverse=True)
    for span, ph, lo, hi, med in rows:
        share = (span / tot_span * 100) if tot_span > 0 else 0
        star = " ★" if share >= 40 else ""
        print(f"  {name:<8}{ph:<20}{lo:>7.1f}{hi:>7.1f}{span:>7.1f}{med:>7.1f}{share:>7.0f}%{star}",
              flush=True)
    t = totals[name]
    print(f"  {'':<8}{'총':<20}{min(t):>7.1f}{max(t):>7.1f}{max(t) - min(t):>7.1f}"
          f"{statistics.median(t):>7.1f}", flush=True)

print(f"\n  ── 종료 사유 ── '증명/자명' 과 '시간리밋' 이 섞이면 이중분포다\n", flush=True)
for name, per in reasons.items():
    for ph in seen_order:
        c = per.get(ph)
        if not c:
            continue
        mixed = any(k in c for k in ("증명", "자명", "gap발화")) and \
            any(k in c for k in ("시간리밋", "UNKNOWN"))
        print(f"  {name:<8}{ph:<20}{dict(c)}{'  ★ 이중분포' if mixed else ''}", flush=True)

print(f"\n  ── 시간리밋 구간의 마지막 개선 시각 ── 처치가 정반대로 갈린다\n", flush=True)
any_imp = False
for name, per in improves.items():
    for ph, vals in per.items():
        if not vals:
            continue
        any_imp = True
        for last, wall in vals:
            if last is None or wall is None:
                print(f"  {name:<8}{ph:<20}개선로그 없음(해를 못 찾음) → 끊어도 무해", flush=True)
                continue
            ratio = last / wall if wall > 0 else 0
            if ratio >= 0.7:
                verdict = "★ 리밋 직전까지 개선 중 → 시간을 더 줘야 한다"
            elif ratio <= 0.3:
                verdict = "초반에 개선 멈춤(bound 만 상승) → 이미 최적, 끊어도 된다"
            else:
                verdict = "중간"
            print(f"  {name:<8}{ph:<20}마지막개선 {last:>6.1f}s / wall {wall:>6.1f}s "
                  f"({ratio * 100:>3.0f}%)  {verdict}", flush=True)
if not any_imp:
    print("  (시간리밋으로 끝난 구간 없음)", flush=True)

print("\n  ── 품질 축 ── 소요만 줄고 품질이 깎이면 이득이 아니다\n", flush=True)
for name, qs in quality.items():
    rates = [q["rate"] for q in qs if q["rate"] is not None]
    wh = sum(q["want"][0] for q in qs if q["want"])
    wt = sum(q["want"][1] for q in qs if q["want"])
    safes = [q["safe"] for q in qs if q["safe"] >= 0]
    hards = [q["hard"] for q in qs if q["hard"] >= 0]
    print(f"  {name}  PrefRate 중앙 {statistics.median(rates) if rates else '—'} · "
          f"want {wh}/{wt} ({wh / max(1, wt) * 100:.1f}%) · "
          f"안전위반 중앙 {statistics.median(safes) if safes else '—'} · "
          f"하드위반 합 {sum(hards) if hards else '—'}", flush=True)

print("\n  ── lex 동결값 ── 이 값이 다음 패스에 고정된다. 낮을수록 좋다(전부 minimize)\n", flush=True)
print(f"  {'병동':<9}{'패스':<18}{'중앙':>10}{'최소':>10}{'최대':>10}", flush=True)
print("  " + "-" * 58, flush=True)
for name, per in frozen.items():
    for ph in seen_order:
        v = per.get(ph)
        if not v:
            continue
        print(f"  {name:<9}{ph:<18}{statistics.median(v):>10.0f}{min(v):>10.0f}{max(v):>10.0f}",
              flush=True)

print("\n  ── 최대 소요 ── Lambda Timeout 600s 대비 여유 확인용\n", flush=True)
for name, t in totals.items():
    print(f"  {name:<9}최대 {max(t):>6.1f}s · 중앙 {statistics.median(t):>6.1f}s · "
          f"최소 {min(t):>6.1f}s", flush=True)

_stall = os.getenv("AIDE_LEX_STALL") == "1"
print(f"\n  ▶ 조건: {'정체종료+이월(AIDE_LEX_STALL=1)' if _stall else '현행'}", flush=True)
if _stall:
    # ★ 판정 전제(2026-09-10) — 이 조건의 처치는 **lex 패스에만** 걸린다
    #   (`_solve_with_stall_stop` 의 `phase.startswith("lex")` 게이트).
    #   stage2 본 solve 는 손대지 않았으므로, **stage2 의 종료 사유·마지막 개선 시각은
    #   현행과 동일한 분포로 읽어도 된다.** stage2 정체 종료 확장 여부를 이 블록
    #   데이터로 판단하는 근거가 이것이다 — "현행 데이터로 확인한 게 아니다" 라는
    #   반박에 대한 답을 여기 남긴다.
    print("  ▶ 전제: 처치는 lex 패스에만 적용된다(stage2 무변경) → stage2 사유 분포는\n"
          "         현행과 동일하게 읽을 수 있다. stage2 확장 판단의 근거로 사용 가능.", flush=True)
print("\n  ※ '총대비' 가 40% 이상인 패스가 변동의 주원인이다.\n"
      "    log_search_progress 오버헤드가 모든 회차에 동일하게 얹히므로 변동폭 비교는 유효하다.\n"
      "  ※ 하드위반은 전월 꼬리를 0 으로 넘겨 재므로 '1일 단독 N' 이 과대 계상된다.\n"
      "    조건 간 **차이**만 보고, 절대값은 verify_all.py 로 따로 확인한다.", flush=True)
