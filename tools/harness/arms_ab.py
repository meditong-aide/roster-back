"""4팔 교차 A/B — lex 처치 분리 + (가) 판정을 한 배치로.

팔(서로 독립 게이트):
  현행        처치 없음
  정체종료     AIDE_LEX_STALL=1   정체 기반 조기 종료 + stage3 이월
  gap0        AIDE_LEX_GAP=1     lex 의 relative_gap_limit 0 + absolute 0.99
  가-safety    AIDE_S3_SAFETY=1   stage3 목적에 safety 추가

★★ 판정 축 두 층
  (1) stage2 확정 안전위반 — lex 처치(정체종료·gap0)의 기여를 가른다.
      악화가 gap0 에서 나오면 처치는 "relative 복원" 이 아니라 **S4-①(항목별 동결) 착수**다.
      총합 동결이 항목을 못 지킨다는 진단이 실측으로 재현된 것이기 때문이다.
  (2) **Δ = stage2 확정값 − stage3 최종값** — (가)의 판정 축.
      ★ 절대값으로 보면 안 된다. stage2 노이즈가 절대값을 지배해서 (가)가 안 보인다
        (실측: (가) 회차가 절대값은 낮았지만 그건 stage2 가 낮았던 것이고,
         stage3 안에서 낮춘 양은 오히려 현행이 컸다).
  ★ want·PrefRate 비회귀 필수 — (가)의 실패 조건은 safety 항이 목적을 지배해
    선호가 뒷전으로 밀리는 것이다. 9A 는 pref 가 포화(18/18)라 안 보이므로
    메디통10 처럼 안 차는 병동이 결정적이다.

★ 회차 안에서 네 팔을 연달아 돈다(블록 비교 금지).
"""
import contextlib, io, os, re, statistics, sys, time
from collections import defaultdict

sys.path.insert(0, "app")
from sqlalchemy import text  # noqa: E402
from db.client2 import SessionLocal  # noqa: E402
from schemas.roster_schema import RosterRequest  # noqa: E402
import services.roster_create_service as RCS  # noqa: E402
import services.cp_sat.fallback_lex as FL  # noqa: E402
import importlib.util as _iu  # noqa: E402

# ★ lex 패스별 동결값 — 이 값이 다음 패스에 고정된다.
#   S4-①(항목별 동결)이 lex 패스의 운신 폭을 줄여 **상위 패스 값을 나쁘게 만드는지**가
#   그 처치의 대가인데, 그건 이 값으로만 보인다(진입/stage3 축에는 안 잡힌다).
#   iso_off 재시험에서도 "고립OFF 가 좋아진 게 grade·team 에서 옮겨온 것인지" 를 가른다.
_FROZEN: dict = {}
_orig_traced = FL._solve_traced


def _traced(solver, model, prefix, phase):
    st = _orig_traced(solver, model, prefix, phase)
    # ★★ `stage3` 도 잡는다(2026-09-11) — S6 판정축 (a)는 **패스 8 목적값 vs stage3
    #   목적값**인데, 기존엔 lex 패스만 잡아 비교 대상이 없었다. 그래서 63런을 돌리고도
    #   "대가의 크기" 를 못 쟀다.
    #   ★ 부호: stage3 는 `m.Maximize`(:3432) 라 obj 가 **클수록 좋고**, 패스 8 은
    #     `-sum(terms)` 를 Minimize 라 **작을수록 좋다**. 비교하려면 패스 8 값에 -1.
    #     즉 `-lex8:s6` 와 `stage3` 를 같은 축에서 견준다.
    if phase.startswith("lex") or phase == "stage3":
        try:
            _FROZEN[phase] = int(round(solver.ObjectiveValue()))
        except Exception:
            pass
    return st


FL._solve_traced = _traced

_sp = _iu.spec_from_file_location(
    "va", os.path.join(os.path.dirname(os.path.abspath(__file__)), "verify_all.py"))
va = _iu.module_from_spec(_sp)
_sp.loader.exec_module(va)

CAP = re.compile(r"\[S4-2계측\] stage3 safety (\d+) / 상한 (\d+) \(([\d.]+)%\) \*\*Δ=(-?\d+)\*\*"
                 r" — ([^\n]*)")
ITEM = re.compile(r"(\w+)=(\d+)/(\d+)")     # 항목별 stage3/상한
FIN = re.compile(r"폴백 완료: 커버리지부족=(-?\d+), 안전위반합=(-?\d+) \(커밋해=(\S+?) 기준\)")
PREF = re.compile(r"\[PrefRate\] rate=([\d.]+) want=(\d+)/(\d+)")
# ★ lex 진입 시점 값 — 두 결함을 가르는 기준선.
#   진입값 = lex후 = stage3 → 전부 stage2 본 solve 편차(S4-① 로는 못 막는다)
#   진입값 < lex후          → lex 안 교환(S4-① 이 직접 막는 몫)
ENTRY = re.compile(r"\[lex진입\] safety합=(\d+) team슬랙=(\d+) grade_off0=(\d+) — ([^\n]*)")
ENT_ITEM = re.compile(r"(\w+)=(\d+)")
# ★★ stage2 는 stage1 결과를 동결로 물려받는다(`coverage == best_short`).
#   이 값이 회차마다 다르면 **stage2 가 매번 다른 문제를 푼다** — 그때는 stage2 gap 을
#   아무리 조여도 값이 안 모인다(최적값 자체가 회차마다 다르므로).
S1RES = re.compile(r"최소 커버리지 부족: (-?\d+), 과잉: (-?\d+)")
ISO_UNIT = 300000        # isolated_off_slack 1건당 페널티. 값/이 값 = 건수

ARMS = [("현행", {}), ("정체종료", {"AIDE_LEX_STALL": "1"}),
        ("gap0", {"AIDE_LEX_GAP": "1"}), ("가-safety", {"AIDE_S3_SAFETY": "1"}),
        # ★ S4-① 은 **이미 구현돼 있고 기본 off** 다(fallback_lex.py:3682 `_s4_stage2`).
        #   착수할 안건이 아니라 켜서 재는 안건이다. lex 진입 시 safety 를 **항목별**로
        #   `<= s2.Value` 동결한다(현행은 총합만) — 항목 간 교환을 직접 막는 처방.
        ("S4-1", {"AIDE_LEX_S4_STAGE2": "1"}),
        # ── iso_off 재시험 ──
        #   2026-09-04 에 기각됐다(team뒤: 고립OFF +1건 · pattern_eod 2→8 악화).
        #   ★ 그 실험은 **총합 동결 상태**에서 돌았다. 파일 주석이 진단까지 적어 뒀다 —
        #     "30만·10만 가중 항목이 총합의 99.99% 라 한 자릿수 항목은 사실상 무제한 교환".
        #     즉 고립OFF 를 줄이려고 pattern_eod 를 늘리는 게 **허용됐다.**
        #     S4-①(항목별 동결)이 켜지면 그 교환이 막힌다 → 기각 사유가 사라질 수 있다.
        #   ★ team 뒤에 둔다: grade·team 이 동결된 뒤라 팀 커버보다 우선이 아니고,
        #     "렉시코 순서에서 grade·team 아래 첫 자리" 라는 의미만 갖는다.
        #     이래야 '고립OFF 를 자기 레벨로' 의 정책 논쟁(환자안전 대 근무품질)을 피한다.
        ("iso_off", {"LEX_PASS_ORDER": "off_range,grade,team,iso_off,n_range,n2n,de,pref"}),
        ("S4-1+iso", {"AIDE_LEX_S4_STAGE2": "1",
                      "LEX_PASS_ORDER": "off_range,grade,team,iso_off,n_range,n2n,de,pref"}),
        # ── 6c: stage2 편차가 시간 부족인가 국소해인가 ──
        #   같은 비용의 두 처치. 시간 3배가 분포를 좁히면 → 예산 재배분이 답.
        #   best-of-3 만 좁히면 → 국소해이고 best-of-N 이 답.
        #   둘 다 못 좁히면 → 남는 카드는 레벨 정책(제품 판단)뿐.
        ("tl2x3", {"AIDE_S2_TL_MULT": "3"}),
        ("best3", {"AIDE_S2_BEST_N": "3"}),
        ("best5", {"AIDE_S2_BEST_N": "5"}),
        # ── 6c-2: stage2 gap 조이기 ──
        #   작은 safety 항목을 최소화하는 자리가 stage2 본 solve 뿐인데
        #   gap 0.15 로 1초에 끝나 그 항목들이 최적화되는 순간이 없다.
        ("gap0.01", {"AIDE_S2_GAP": "0.01"}),
        ("gapABS", {"AIDE_S2_GAP": "0", "AIDE_S2_ABS_GAP": "0.99"}),
        ("s2carry", {"AIDE_S2_CARRY": "1"}),
        # ── g0 가중치 롤백 판정 ──
        #   사용자 결정(2026-09-11): 등급 순수미달 > 고립OFF. 15만(정수배·동률) → 16만.
        #   ★ 롤백 조건: **고립OFF 건수가 유의하게 늘면 동급으로 되돌린다.**
        #     그래서 두 팔을 같은 배치에서 재야 노이즈 폭 기준선이 선다.
        #     둘 다 gapABS 를 켠다 — gap 이 느슨하면 최적을 못 찾아 가중치 효과가 안 보인다.
        ("g0=15만", {"AIDE_GRADE_OFF0_W": "150000",
                     "AIDE_S2_GAP": "0", "AIDE_S2_ABS_GAP": "0.99"}),
        ("g0=16만", {"AIDE_GRADE_OFF0_W": "160000",
                     "AIDE_S2_GAP": "0", "AIDE_S2_ABS_GAP": "0.99"}),
        # ── stage1 층 의심 ──
        #   시화중환2 는 gapABS 인데도 값이 흩어졌다. stage2 가 회차마다 **다른 문제**를
        #   푸는 것으로 보이고, 그걸 바꿀 수 있는 건 stage1 동결값뿐이다.
        #   ★ 규모별 게이트를 만들기 전에 이 층부터 확인한다 — 규모가 아니라 층이 문제면
        #     게이트가 원인을 가린다.
        ("s1gap", {"AIDE_S1_GAP": "0", "AIDE_S1_ABS_GAP": "29"}),
        ("s1+s2gap", {"AIDE_S1_GAP": "0", "AIDE_S1_ABS_GAP": "29",
                      "AIDE_S2_GAP": "0", "AIDE_S2_ABS_GAP": "0.99"}),
        # ── 시간 예산 ── (2026-09-11 · stage1 층 기각 후)
        #   ★ 시화중환2 stage2 는 **3/3 전부 시간리밋**이고 마지막 개선이 wall 대비
        #     95%·95%·100% 였다 — 리밋 직전까지 개선 중. gap 게이트가 발화할 기회가 없다.
        #     그래서 gapABS 가 이 병동에서만 안 들었다. 처방은 gap 이 아니라 **시간**이다.
        #   ★ 두 처치를 가른다:
        #     tl2x3+gapABS — stage2 에 시간을 직접 더 준다(21s → 63s).
        #     stall+gapABS — lex1(4.2s 중 0.5s 에 개선 멈춤) · lex2(6.3s 중 0.5s) 의
        #                    낭비 9.5s 를 끊어 stage3 로 이월한다. stage3 도 시간리밋 2/3.
        #   ★ 둘 다 gapABS 를 켜 둔다 — 시간을 늘려도 gap 이 느슨하면 도로 조기 종료다.
        ("tl2x3+gapABS", {"AIDE_S2_TL_MULT": "3",
                          "AIDE_S2_GAP": "0", "AIDE_S2_ABS_GAP": "0.99"}),
        ("stall+gapABS", {"AIDE_LEX_STALL": "1",
                          "AIDE_S2_GAP": "0", "AIDE_S2_ABS_GAP": "0.99"}),
        # ── 세브2 악화 처방 ── (2026-09-11 · ① 판정에서 유일한 악화 병동)
        #   ★ 기전: gapABS 에서 **진입 고립OFF 0·0·0 → lex후 1·2**. 없던 고립OFF 를
        #     lex 패스가 만들어냈다. (2b) 도 "isolated_off_slack 450000→300000 가능" 이라
        #     여지가 있었는데 못 내렸다고 찍는다. 등급미달은 오히려 36 → 29 로 gapABS 가 낫다.
        #   ★ 즉 gapABS 자체의 결함이 아니라 **lex 안 교환**이고, 정확히 S4-①(항목별 동결)이
        #     막는 몫이다. 총합만 동결하는 현행에서는 30만 항목끼리 자유롭게 맞바꿀 수 있다.
        #   ★★ 반드시 gapABS 를 **깔고** 비교한다 — S4-① 단독 팔과 견주면 두 처치가 섞인다.
        ("S4-1+gapABS", {"AIDE_LEX_S4_STAGE2": "1",
                         "AIDE_S2_GAP": "0", "AIDE_S2_ABS_GAP": "0.99"}),
        # ── ② stage2 정체 종료 + 상한 확장 ── (2026-09-11 · dev 코드 기준)
        #   ★ 별도 게이트를 만들지 않고 **기존 워치독을 stage2 로 확장**했다.
        #     "마지막개선/wall >= 임계면 연장" 과 "N초 정체면 종료" 는 같은 정보를 쓰는
        #     같은 규칙이라 임계를 사전에 정할 필요가 없다(경계값 0.9 가 소거된 이유).
        #   ★ `AIDE_S2_STALL=1` 이면 상한 배수가 자동 3배(tl2 21s → 63s).
        #     개선이 이어지는 병동은 63s 까지 가고, 멈춘 병동은 임계(7s)에서 끊겨
        #     **현행보다 빨라진다.** 전역 tl2x3 기각 사유(+31% 소요)를 구조적으로 회피.
        #   ★★ 판정축 — 지도 실측으로 **stage3 가 아니라 소요**로 옮겼다.
        #     시화중환2 stage3 폭이 10회에서 **5,100,005**(고립OFF 17건)라 5회로는 판정 불가.
        #     소요는 폭 14s 로 좁아 시간 회수가 직접 읽힌다.
        #     품질은 **폭 0 병동**(9A·9B·중환1)에서 **비회귀**로 본다 — 거기선 1만 움직여도 신호.
        #   ★ 대조군은 "현행" 이다 — gapABS 가 이제 **기본값**이라 아무것도 안 켜면 그 상태다.
        ("s2stall", {"AIDE_S2_STALL": "1"}),
        # ── lex 패스 정체 종료 + 회수분을 다음 lex 패스로 ── (2026-09-11)
        #   ★ 실측(시화9B · time_breakdown 3회)이 가른 자리:
        #     lex4:n_range  0.3s/4.3s(8%)  → 이미 최적인데 4초를 태운다
        #     lex6:de       6.0/6.3s(96%) · 5.2/6.3s(83%) → 리밋 직전까지 개선 중
        #     즉 de 의 obj=27 은 최적이 아니라 **시간이 끊긴 값**이다.
        #   ★★ 두 게이트를 나눠 잰다 — `AIDE_LEX_STALL` 은 "끊는" 처치이고
        #     `AIDE_LEX_CARRY` 는 "회수분을 어디에 쓰나" 다. 묶으면 기여가 안 갈린다.
        #     STALL 만 켜면 회수분이 **stage3 로만** 가서 de 는 그대로 6.3초에 잘린다.
        ("lexstall", {"AIDE_LEX_STALL": "1"}),
        ("lexstall+carry", {"AIDE_LEX_STALL": "1", "AIDE_LEX_CARRY": "1"}),
        # ── [S6] stage3 목적을 lex 패스 8 로 ── (2026-09-11)
        #   ★ 배경: lex 체인 2~7 이 **산출물에 안 박힌다**(de 43→20 인데 stage3 최종 동일).
        #     stage3 가 물려받는 동결은 커버리지·safety 항목별·grade 뿐이라, 나머지 여섯은
        #     stage3 가 선호·KLD 를 위해 자유롭게 되돌린다.
        #   ★★ 처방 두 갈래 중 **싼 쪽**을 골랐다 — lex 목적 6개를 m3 에 X3 로 재구성하면
        #     "같은 목적을 두 모델에 두 번 짓기"(이 세션 결함 3건의 구조)를 6배로 늘린다.
        #     반대로 stage3 목적은 이미 `m`·`X` 주입 구조라 m2 로 그대로 옮겨진다(8,579개 항 실측).
        #   판정축 3층: (a) 패스8 목적값 vs stage3 목적값(부호 뒤집어 비교)
        #               (b) 체인 커밋 해의 lex 6값이 동결값 이하로 유지되는가
        #               (c) stage3 Δ — ≈0 이면 m3 제거 근거
        ("s6-stage3커밋", {"AIDE_S6_PASS8": "1",
                           "LEX_PASS_ORDER": "off_range,grade,team,n_range,n2n,de,pref,s6"}),
        ("s6-chain커밋", {"AIDE_S6_PASS8": "1", "AIDE_S6_COMMIT": "chain",
                          "LEX_PASS_ORDER": "off_range,grade,team,n_range,n2n,de,pref,s6"})]
_KEYS = ("AIDE_LEX_STALL", "AIDE_LEX_GAP", "AIDE_S3_SAFETY", "AIDE_LEX_S4_STAGE2",
         "LEX_PASS_ORDER", "AIDE_S2_TL_MULT", "AIDE_S2_BEST_N",
         "AIDE_S2_GAP", "AIDE_S2_ABS_GAP", "AIDE_S2_CARRY", "AIDE_GRADE_OFF0_W",
         "AIDE_S2_STALL", "AIDE_S2_STALL_IDLE", "AIDE_S1_GAP", "AIDE_S1_ABS_GAP",
         "AIDE_LEX_CARRY", "AIDE_S6_PASS8", "AIDE_S6_COMMIT")
if os.getenv("ARMS"):
    _w = {x.strip() for x in os.getenv("ARMS").split(",") if x.strip()}
    ARMS = [a for a in ARMS if a[0] in _w] or ARMS

ONLY = [x.strip() for x in (os.getenv("G", "") or "").split(",") if x.strip()]
GROUPS = [(n, g) for n, g in va.ALL if not ONLY or g in ONLY or n in ONLY]
REPS = int(os.getenv("R", "3"))

print(f"  4팔 교차 A/B — {len(GROUPS)}곳 × {REPS}회 × {len(ARMS)}팔 "
      f"= {len(GROUPS) * REPS * len(ARMS)}런\n", flush=True)
print(f"  {'병동':<9}{'회':>3}{'팔':>10}{'소요':>7}{'stage2':>10}{'stage3':>10}{'Δ':>7}"
      f"{'want':>9}{'커밋해':>9}", flush=True)
print("  " + "-" * 76, flush=True)

acc = defaultdict(lambda: defaultdict(list))     # [(병동,팔)][지표] -> [값...]
items = defaultdict(lambda: defaultdict(list))   # [(병동,팔)][safety항목] -> [(stage3, 상한)...]
lexvals = defaultdict(lambda: defaultdict(list))  # [(병동,팔)][lex패스] -> [동결 목적값...]
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
    for r in range(1, REPS + 1):
        for arm, env in ARMS:                    # ★ 회차 안에서 교차
            for k in _KEYS:
                os.environ.pop(k, None)
            os.environ.update(env)
            buf, db2 = io.StringIO(), SessionLocal()
            _FROZEN.clear()
            t0 = time.perf_counter()
            # ★★ 생성 1회가 70~80초라 그 사이 DB 연결이 끊기면 세션이 죽는다
            #   (`PendingRollbackError: Can't reconnect until invalid transaction is
            #    rolled back`). 엔진에 `pool_pre_ping=True` 가 있어도 **이미 바인딩된
            #   커넥션이 중간에 끊기는 것**은 못 막는다 — pre_ping 은 꺼낼 때만 본다.
            #   이 세션에서 3회 겪었고(시화9B·나사렛7B·시화중환1) 그때마다 표본이 깎였다.
            #   → 실패하면 **세션을 버리고 새로 만들어 1회 재시도**한다.
            _err = None
            for _try in range(2):
                try:
                    with contextlib.redirect_stdout(buf):
                        RCS.generate_roster_service(
                            RosterRequest(year=Y, month=M, group_id=gid), user, db2)
                    _err = None
                    break
                except Exception as e:
                    _err = e
                    try:
                        db2.invalidate()      # 오염된 커넥션을 풀에서 제거
                    except Exception:
                        pass
                    db2.close()
                    if _try == 0:
                        db2, buf = SessionLocal(), io.StringIO()
                        t0 = time.perf_counter()
            if _err is not None:
                print(f"  {name:<9}{r:>3}{arm:>10}  생성실패(재시도 후) "
                      f"{type(_err).__name__}: {str(_err)[:36]}", flush=True)
                continue
            try:
                db2.rollback()
            except Exception:
                pass
            db2.close()
            dt = time.perf_counter() - t0
            log = buf.getvalue()
            cap, fin, pm = CAP.search(log), FIN.search(log), PREF.search(log)
            for _ph, _v in _FROZEN.items():
                lexvals[(name, arm)][_ph].append(_v)
            # ★ search 가 아니라 findall 의 **마지막** — stage1 은 attempt 를 여러 번 돌 수 있고
            #   (broad soft 재시도 등) stage2 가 물려받는 건 마지막으로 성공한 값이다.
            _s1 = S1RES.findall(log)
            if _s1:
                acc[(name, arm)]["s1_short"].append(int(_s1[-1][0]))
                acc[(name, arm)]["s1_over"].append(int(_s1[-1][1]))
                acc[(name, arm)]["s1_n"].append(len(_s1))
            ent = ENTRY.search(log)
            if ent:
                acc[(name, arm)]["entry"].append(int(ent.group(1)))
                acc[(name, arm)]["team"].append(int(ent.group(2)))
                acc[(name, arm)]["g0"].append(int(ent.group(3)))
                # ★ 고립OFF 는 **건수**로 본다(값/30만). 그리고 **진입값 대비**로 읽어야 한다 —
                #   진입 2건 → lex후 0건이면 성공이고, 진입 0건 → 0건은 정보가 없다.
                _ei = dict(ENT_ITEM.findall(ent.group(4)))
                acc[(name, arm)]["iso_entry"].append(
                    int(_ei.get("isolated_off_slack", 0)) // ISO_UNIT)
            s2v = int(cap.group(2)) if cap else None
            s3v = int(cap.group(1)) if cap else None
            dlt = int(cap.group(4)) if cap else None
            commit = fin.group(3) if fin else "?"
            want = (int(pm.group(2)), int(pm.group(3))) if pm else None
            key = (name, arm)
            acc[key]["sec"].append(dt)
            if s2v is not None:
                acc[key]["s2"].append(s2v)
                acc[key]["s3"].append(s3v)
                acc[key]["delta"].append(dlt)
                # ★ 항목별 stage3/상한 — Δ=0 이 "내릴 게 없었다" 인지 "있었는데 못 내렸다" 인지
                #   가르려면 총합이 아니라 항목별이 필요하다.
                #   상한>stage3 인 항목이 있으면 여지가 있었는데 안 내린 것이다.
                for _k, _cur, _cap in ITEM.findall(cap.group(5)):
                    items[key][_k].append((int(_cur), int(_cap)))
            if want:
                acc[key]["want_hit"].append(want[0])
                acc[key]["want_tot"].append(want[1])
            acc[key]["commit_s3"].append(1 if commit.startswith("stage3") else 0)
            ws = f"{want[0]}/{want[1]}" if want else "—"
            print(f"  {name:<9}{r:>3}{arm:>10}{dt:>6.0f}s{s2v if s2v is not None else '—':>10}"
                  f"{s3v if s3v is not None else '—':>10}{dlt if dlt is not None else '—':>7}"
                  f"{ws:>9}{commit:>9}", flush=True)

med = lambda xs: statistics.median(xs) if xs else None      # noqa: E731
# ★★ 세 열을 **나란히** 둔다. Δ 만 보면 안 된다 — Δ 는 stage2 가 나쁠수록 커지는 지표라
#   "stage3 가 많이 회수했다" 와 "stage2 가 나빠서 회수할 게 많았다" 가 안 갈린다.
#   **순이득은 stage3 최종값**이고, Δ 는 그 이득이 어느 단계에서 나왔는지 설명하는 열이다.
print(f"\n{'=' * 96}\n  (0) 두 결함의 비중 — 진입 → lex후 → stage3\n", flush=True)
print("  ★ 진입=lex후=stage3 이면 전부 **stage2 본 solve 편차**(S4-①로 못 막는다)\n"
      "  ★ 진입 < lex후 이면 **lex 안 교환**(S4-①이 직접 막는 몫)\n", flush=True)
print(f"  {'병동':<9}{'팔':<10}{'진입':>12}{'lex후':>12}{'stage3':>12}{'진입→lex':>10}"
      f"{'team':>8}{'g0':>8}", flush=True)
print("  " + "-" * 74, flush=True)
for name, _g in GROUPS:
    for a, _ in ARMS:
        d = acc[(name, a)]
        if not d["entry"] or not d["s2"]:
            continue
        e, s2m, s3m = med(d["entry"]), med(d["s2"]), med(d["s3"])
        mark = "  ★교환" if s2m > e else ("  개선" if s2m < e else "")
        print(f"  {name:<9}{a:<10}{e:>12,.0f}{s2m:>12,.0f}{s3m:>12,.0f}{s2m - e:>10,.0f}"
              f"{med(d['team']) or 0:>8,.0f}{med(d['g0']) or 0:>8,.0f}{mark}", flush=True)

print(f"\n{'=' * 96}\n  (1)(2) stage2 확정 · Δ · stage3 최종 — 순이득은 **stage3 최종**\n", flush=True)
print(f"  {'병동':<9}" + "".join(f"{a + ' (s2/Δ/s3)':>26}" for a, _ in ARMS), flush=True)
print("  " + "-" * (9 + 26 * len(ARMS)), flush=True)
for name, _g in GROUPS:
    if not any(acc[(name, a)]["s2"] for a, _ in ARMS):
        continue
    cells = []
    for a, _ in ARMS:
        d = acc[(name, a)]
        if not d["s2"]:
            cells.append("—")
            continue
        cells.append(f"{med(d['s2']):,.0f}/{med(d['delta']):,.0f}/{med(d['s3']):,.0f}")
    print(f"  {name:<9}" + "".join(f"{c:>26}" for c in cells), flush=True)

print("\n  (2b) Δ=0 의 해석 — 상한에 여지가 있었는데 못 내린 항목이 있나\n", flush=True)
print(f"  {'병동':<9}{'팔':<10}여지있던 항목(상한>stage3 가 가능했던 자리)", flush=True)
print("  " + "-" * 76, flush=True)
for name, _g in GROUPS:
    for a, _ in ARMS:
        per = items[(name, a)]
        if not per:
            continue
        # 다른 팔에서 그 항목을 더 낮춘 적이 있으면 '여지가 있었다' 는 증거다.
        best = {}
        for a2, _ in ARMS:
            for k, vals in items[(name, a2)].items():
                for cur, _cap in vals:
                    best[k] = min(best.get(k, cur), cur)
        gaps = []
        for k, vals in per.items():
            cur = med([c for c, _ in vals])
            if best.get(k) is not None and cur > best[k]:
                gaps.append(f"{k} {cur:.0f}→{best[k]}가능")
        if gaps:
            print(f"  {name:<9}{a:<10}{', '.join(gaps[:4])}", flush=True)

# ── 항목별 절대값 덤프 ──
#   ★ 30만·10만 가중 항목(isolated_off_slack 등)이 0 인 병동(실측: 시화중환1 = 총 70)은
#     작은 항목(off_quota·week_off·pattern)만 남아, 다른 병동에서 가중 항목에 묻히는
#     **S4-①(작은 항목 간 무제한 교환) 질문을 정면으로 볼 수 있는 표본**이다.
#     ITEMS=1 로 켜서 그 병동만 따로 본다.
if os.getenv("ITEMS") == "1":
    print("\n  (2c) 항목별 stage3 최종값 — 작은 항목 교환을 직접 본다\n", flush=True)
    print("  ★★ 판정 전에 **노이즈 폭부터** 잡는다. 현행 팔 3회 안에서 항목값이 흔들리는 폭이\n"
          "     기준선이고, 다른 팔의 차이가 **그 폭을 넘을 때만** '구성이 흔들린다' 로 읽는다.\n"
          "     작은 항목은 절대값이 한 자릿수라 1~2 차이가 노이즈다 — 기준선 없이 읽으면\n"
          "     이 세션 초반의 오판(5회 표본으로 회귀 단정 → 8회에서 근거 소멸)으로 돌아간다.\n"
          "     표기: 중앙[최소~최대] · 현행 폭을 넘는 차이에만 ★\n", flush=True)
    _keys = sorted({k for name, _g in GROUPS for a, _ in ARMS for k in items[(name, a)]})
    for name, _g in GROUPS:
        if not any(items[(name, a)] for a, _ in ARMS):
            continue
        print(f"  ── {name}", flush=True)
        print(f"     {'항목':<22}" + "".join(f"{a:>18}" for a, _ in ARMS), flush=True)
        _dir = defaultdict(lambda: {"up": [], "down": []})     # 팔별 유의 항목의 방향
        for k in _keys:
            base = [c for c, _ in (items[(name, ARMS[0][0])].get(k) or [])]
            span = (max(base) - min(base)) if base else 0      # 현행 팔의 노이즈 폭
            bmed = med(base) if base else None
            cells = []
            for a, _ in ARMS:
                vals = items[(name, a)].get(k)
                if not vals:
                    cells.append("—")
                    continue
                cs = [c for c, _ in vals]
                m_, lo, hi = med(cs), min(cs), max(cs)
                # ★ 현행 중앙과의 차이가 현행 폭을 넘을 때만 유의 표시
                sig = " "
                if bmed is not None and abs(m_ - bmed) > span:
                    sig = "★"
                    _dir[a]["up" if m_ > bmed else "down"].append(k)
                cells.append(f"{m_:,.0f}[{lo}~{hi}]{sig}")
            if any(c != "—" for c in cells):
                print(f"     {k:<22}" + "".join(f"{c:>18}" for c in cells)
                      + f"   현행폭={span}", flush=True)
        # ★★ 방향 판정 — ★ 가 났다고 다 '교환' 이 아니다.
        #   한 항목 ↑ · 다른 항목 ↓ 가 섞여야 **구성이 흔들린 것**(S4-① 착수 근거)이고,
        #   전부 한 방향으로 올라가면 그건 교환이 아니라 그 팔의 lex 값이 나빠진 것이라
        #   처방이 다르다(총합을 지키는 문제가 아니라 총합 자체가 나빠진 것).
        for a, _ in ARMS[1:]:
            up, dn = _dir[a]["up"], _dir[a]["down"]
            if not up and not dn:
                continue
            if up and dn:
                v = f"★★ 구성 흔들림(교환) — ↑{','.join(up[:3])} / ↓{','.join(dn[:3])}  → S4-① 착수 근거"
            elif up:
                v = f"단방향 악화 — ↑{','.join(up[:3])}  → 교환 아님. 그 팔의 lex 값이 나빠진 것"
            else:
                v = f"단방향 개선 — ↓{','.join(dn[:3])}  → 그냥 이득"
            print(f"       [{a}] {v}", flush=True)

print("\n  (2f) stage1 동결값 — **stage2 가 매번 같은 문제를 푸는가**\n", flush=True)
print("  ★★ stage2 는 stage1 결과를 동결로 물려받는다(`coverage == best_short`).\n"
      "     이 값이 회차마다 다르면 stage2 는 **매번 다른 문제**를 풀고,\n"
      "     그러면 stage2 gap 을 아무리 조여도 값이 안 모인다(최적값 자체가 다르므로).\n"
      "     → 그때 처방은 stage2 가 아니라 **stage1 gap 조이기**(AIDE_S1_GAP=0)다.\n", flush=True)
print(f"  {'병동':<9}{'팔':<11}{'부족 분포':>20}{'과잉 분포':>20}{'attempt수':>10}"
      f"{'고정?':>8}", flush=True)
print("  " + "-" * 80, flush=True)
for name, _g in GROUPS:
    for a, _ in ARMS:
        d = acc[(name, a)]
        sh, ov = d.get("s1_short") or [], d.get("s1_over") or []
        if not sh:
            continue
        f = lambda xs: "·".join(str(x) for x in xs) if xs else "—"      # noqa: E731
        fixed = "고정" if len(set(sh)) == 1 and len(set(ov)) == 1 else "★변동"
        # ★ attempt 수가 회차마다 다르면 stage1 이 **다른 경로로** 값에 도달한 것이다.
        #   값이 같아도 경로가 다르면 뒤 단계 힌트·시드가 달라진다.
        print(f"  {name:<9}{a:<11}{f(sh):>20}{f(ov):>20}"
              f"{f(d.get('s1_n') or []):>10}{fixed:>8}", flush=True)

print("\n  (2e) 고립OFF 건수 — 진입 → lex후 → stage3 · **분포**로 본다\n", flush=True)
print("  ★ 목표가 평균이 아니라 **편차**다. '0 으로 모이는가' 를 5회 중 몇 회가 0 인지로 읽는다.\n"
      "  ★ 진입 대비로 읽는다 — 진입 2건 → lex후 0건이면 성공, 진입 0건 → 0건은 정보 없음.\n"
      "  ★ iso_off 패스가 **아무것도 못 해도 유효한 결과**다: 항목별 동결 아래에서 다른 항목을\n"
      "     하나도 안 늘리고 고립OFF 만 줄이는 게 불가능하면 진입값 그대로 끝난다.\n"
      "     그러면 결론은 '교환 없이는 못 걷어낸다' 이고, 6c 의 다음 카드는 best-of-N 또는 레벨 정책.\n", flush=True)
print(f"  {'병동':<9}{'팔':<11}{'진입 분포':>18}{'lex후 분포':>18}{'0건 회차':>10}"
      f"{'미달 분포':>16}", flush=True)
print("  " + "-" * 84, flush=True)
for name, _g in GROUPS:
    for a, _ in ARMS:
        d = acc[(name, a)]
        ie = d.get("iso_entry") or []
        # lex후 = S4-2 계측의 '상한'(= lex 가 확정해 stage3 에 물려준 값)
        il = [cap // ISO_UNIT for _c, cap in (items[(name, a)].get("isolated_off_slack") or [])]
        if not ie and not il:
            continue
        fmt = lambda xs: "·".join(str(x) for x in sorted(xs)) if xs else "—"   # noqa: E731
        zero = f"{sum(1 for x in il if x == 0)}/{len(il)}" if il else "—"
        # ★ 등급미달도 함께 본다 — 가중치 변경은 "미달을 줄이는 대신 고립OFF 를 받는"
        #   방향이라, 고립OFF 만 보면 회귀로 오판하고 미달만 보면 대가를 못 본다.
        g0 = d.get("g0") or []
        print(f"  {name:<9}{a:<11}{fmt(ie):>18}{fmt(il):>18}{zero:>10}{fmt(g0):>16}", flush=True)

print("\n  (2d) lex 패스별 동결값 — 이 값이 다음 패스에 고정된다(낮을수록 좋음)\n", flush=True)
print("  ★ S4-①의 대가: 항목별 동결이 운신 폭을 줄여 **상위 패스 값이 나빠지면** 그게 비용이다.\n"
      "  ★ iso_off 재시험: 고립OFF 개선이 grade·team 에서 **옮겨온 것**이면 순이득이 아니다\n"
      "     (2026-09-04 기각 사유가 정확히 그것 — 7B grade 36→40 · team 32→36).\n", flush=True)
_lexkeys = sorted({p for name, _g in GROUPS for a, _ in ARMS for p in lexvals[(name, a)]})
for name, _g in GROUPS:
    if not any(lexvals[(name, a)] for a, _ in ARMS):
        continue
    print(f"  ── {name}", flush=True)
    print(f"     {'패스':<18}" + "".join(f"{a:>16}" for a, _ in ARMS), flush=True)
    for p in _lexkeys:
        base = lexvals[(name, ARMS[0][0])].get(p) or []
        span = (max(base) - min(base)) if base else 0
        bmed = med(base) if base else None
        cells = []
        for a, _ in ARMS:
            v = lexvals[(name, a)].get(p)
            if not v:
                cells.append("—")
                continue
            m_ = med(v)
            sig = "★" if (bmed is not None and abs(m_ - bmed) > span) else ""
            cells.append(f"{m_:,.0f}[{min(v)}~{max(v)}]{sig}")
        if any(c != "—" for c in cells):
            print(f"     {p:<18}" + "".join(f"{c:>16}" for c in cells)
                  + f"  현행폭={span}", flush=True)

print(f"\n  (3) 선호 반영 — (가)의 실패 조건(선호가 뒷전)\n", flush=True)
print(f"  {'병동':<9}" + "".join(f"{a:>14}" for a, _ in ARMS), flush=True)
print("  " + "-" * (9 + 14 * len(ARMS)), flush=True)
for name, _g in GROUPS:
    cells = []
    for a, _ in ARMS:
        h, t = acc[(name, a)]["want_hit"], acc[(name, a)]["want_tot"]
        cells.append(f"{sum(h)}/{sum(t)}" if t else "—")
    if any(c != "—" for c in cells):
        print(f"  {name:<9}" + "".join(f"{c:>14}" for c in cells), flush=True)

print(f"\n  (4) 소요 중앙 · stage3 커밋률\n", flush=True)
for a, _ in ARMS:
    secs = [x for name, _g in GROUPS for x in acc[(name, a)]["sec"]]
    cs = [x for name, _g in GROUPS for x in acc[(name, a)]["commit_s3"]]
    ds = [x for name, _g in GROUPS for x in acc[(name, a)]["delta"]]
    print(f"  {a:<10} 소요중앙 {med(secs) or 0:>5.0f}s · 최대 {max(secs) if secs else 0:>5.0f}s · "
          f"stage3 커밋 {sum(cs)}/{len(cs)} · Δ합 {sum(ds) if ds else 0:,}", flush=True)
print("\n  ※ (1) 악화가 gap0 에서 나오면 → S4-①(항목별 동결) 착수. relative 복원은 덮는 것뿐이다.\n"
      "  ※ (2) Δ 가 (가) < 현행이면 → (가)는 W 를 낮추거나 철회.", flush=True)
