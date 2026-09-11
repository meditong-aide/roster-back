"""11곳 전량 생성 + RVERIFY 규칙대로 검증.

설계 근거는 scratchpad/design/*.txt (4축 조사 + 반증). 요점만 여기 옮긴다.

★ RVERIFY-02 — 기준을 하드코딩하지 않는다. 병동마다 `_fetch_latest_config` 로 활성 제약을,
  `_build_shift_manage_and_requirements` 로 일자별 커버리지 목표를 **생성기와 같은 경로**로 읽는다.
★ RVERIFY-03 — shift_id→대표코드는 `build_shift_normalizer` 가 정본. 매핑 실패는 None 으로
  두고 **반드시 카운트해 출력**한다. 조용히 통과시키면 오탐·과소평가가 동시에 생긴다.
  `db.query(Shift).all()` 금지 — shift_id 가 병동 간 중복이라 코드 집합이 오염된다.
★ RVERIFY-04 — 전월 꼬리는 `build_cross_month_constraints` 로 얻는다. 생성 직후 바로 검증하므로
  "검증 시점 전월" 과 "생성 당시 전월" 이 같다.
★ RVERIFY-05 — SKIP_PRIMARY 기본 "1" 이라 실제 도는 제약은 fallback_lex 뿐이다.
★ RVERIFY-06 — 고정근무자는 후처리가 채우고 roster_data 에 이미 반영돼 있다. 따로 합치지 않는다.

★★ 판정 구간은 **간호사별 활동구간 [T0,T1]** 이다(fallback_lex.py:454-473 의 join/leave).
   월초·월말로 하드코딩하면 중도 입·퇴사자에서 오탐이 난다.
★★ 하네스(`tools/harness/runner.py`)의 `_analyze_hard_patterns` 는 쓰지 않는다 —
   1N 을 "월 N 총합==1" 로 세고, 연속근무 6·월나이트 15 가 하드코딩이라 설정을 안 읽는다.
★★ 생성이 실패한 병동은 "위반 0" 이 아니라 **검사 불가**로 분리한다. 섞으면 판정이 뒤집힌다.
"""
import contextlib, io, json, os, sys, time
from collections import Counter
from datetime import timedelta

sys.path.insert(0, "app")
from sqlalchemy import text  # noqa: E402
from db.client2 import SessionLocal, engine  # noqa: E402
from db.models import Group, Nurse  # noqa: E402
from schemas.auth_schema import User  # noqa: E402
from schemas.roster_schema import RosterRequest  # noqa: E402
import services.roster_create_service as RCS  # noqa: E402
from services.cp_sat.shift_normalizer import build_shift_normalizer  # noqa: E402
from services.cp_sat.off_policy import resolve_effective_off_days  # noqa: E402

assert engine.url.database == "eun_roster_dev", engine.url.database

# ★★ 판정 모수 정본 (2026-09-11 기준) — **채택 판정은 ACTIVE 로만 한다.**
#   이 구분이 왜 코드에 있는가: 판정이 다 끝난 뒤 모수가 바뀌면 결론이 통째로 흔들린다.
#   실제로 그랬다 — gapABS 11곳 판정에서 **유일한 악화가 세브2** 였는데 그 병동이
#   방치된 테스트 데이터였다. 빼자 악화 0 이 되어 승격 근거가 오히려 깨끗해졌고,
#   반대로 S4-①(항목별 동결)은 **착수 명분 자체가 "세브2 악화를 잡는다"** 였어서
#   모수에서 빠지자 stage3 이득이 시화중환2 하나만 남고 대가(lex5:n2n 악화 3곳)는
#   그대로인 상태가 됐다. 같은 실측인데 모수 하나로 채택/기각이 뒤집힌 것이다.
ACTIVE = [
    ("시화9A", "101358f6de7b"), ("시화9B", "10135890c287"),
    ("시화중환1", "10135857f9f9"), ("시화중환2", "10135834e48b"),
    ("인천41RN", "1022438ea001"), ("인천별관1", "1022432916e6"),
    # ★ 나사렛7B — **운영 활성 확정**(2026-09-11 · 사용자 확인).
    #   등급미달이 42~43 으로 지속적으로 크다 → 이건 데이터 문제가 아니라
    #   **실제 품질 문제이므로 별도 안건**이다. 모수에서 빼지 말 것.
    ("나사렛7B", "1025768b9f1c"),
]
# ★★ 선호(stage3 품질) 축은 **활성 7곳에서 현재 측정 불가**다.
#   9A 54/54 · 9B 111/111 · 중환1 258/258 · 중환2 195/195 · 41RN 240/240 · 별관1 105/105
#   으로 전부 **포화**이고, 나사렛7B 는 `[PrefRate]` 로그 자체가 안 찍힌다(원티드 없음).
#   유일한 미포화 병동이던 메디통10(26/39)이 방치 데이터로 확인돼 빠졌다.
#   → (가)-safety(AIDE_S3_SAFETY)와 정체종료(AIDE_LEX_STALL)의 **실패 조건**
#     ("선호가 뒷전으로 밀리는가")은 지금 볼 수 있는 자리가 없다. 두 처치는
#     **"무해 확인" 까지만 된 상태**로 두고, 선호가 실제로 안 차는 활성 병동이
#     생기면(원티드 많은 달) 그때 재판정한다.
# ★ 레거시 — **기전 관찰용. 빈도 추정·채택 판정에서 제외.**
#   "엔진이 이렇게 동작할 수 있다" 는 관찰에는 유효하다(세브2 의 진입 고립OFF 0 → lex후 2건은
#   데이터 품질과 무관한 엔진 동작이다). 다만 **얼마나 자주 일어나는지**에는 쓸 수 없다.
#   같은 성격: 강남베드로 · 삼육(목록에 없음 — 이미 걸러짐).
LEGACY = [
    ("메디통10", "102527b5de4e"),          # 방치 확인(2026-09-11)
    ("세브2", "102560184a40"), ("세브3RN", "1025604f8279"),
    ("세브7", "1025603e0efd"),
]
ALL = ACTIVE + LEGACY                       # G= 로 지정하면 레거시도 돌릴 수 있다
only = [x.strip() for x in (os.getenv("G", "") or "").split(",") if x.strip()]
# ★ 기본 모수는 ACTIVE 뿐이다. 레거시를 재려면 G 로 **명시**해야 한다 —
#   그래야 "전량 배치" 가 조용히 레거시를 섞어 판정을 오염시키지 못한다.
# ★ 임시 검증용 — **판정 모수에 넣지 않고** 한 번만 검사할 병동. `이름:group_id` 형식.
#   온보딩 중이거나 타 세션이 보고한 병동을 ACTIVE 에 섞으면 모수 정본이 오염되고,
#   그러면 이후 A/B 의 부호검정 기준이 조용히 달라진다(세브2 사례).
EXTRA = [tuple(x.split(":", 1)) for x in (os.getenv("EXTRA", "") or "").split(",")
         if ":" in x]
_pool = (ALL if only else ACTIVE) + EXTRA
GROUPS = [(n, g) for n, g in _pool if not only or g in only or n in only]
REPS = int(os.getenv("R", "3"))
MAIN_SET = {"D", "E", "N", "M", "O", "주", "W"}
WORK = {"D", "E", "N", "M", "W"}          # '근무' = O 가 아닌 모든 시프트(fallback_lex.py:2028)


# ───────────────────────────── 기준 로딩 (RVERIFY-02) ─────────────────────────────

def make_user(db, gid):
    """병동마다 새로 만든다 — generate_roster_service 가 user.group_id 를 in-place 로 덮는다."""
    raw = db.execute(text("SELECT hn_id FROM groups WHERE group_id=:g"), {"g": gid}).scalar()
    mgr = json.loads(raw) if isinstance(raw, str) else list(raw or [])
    hn = None
    if mgr:
        ph = ", ".join(f":m{i}" for i in range(len(mgr)))
        hn = db.execute(text("SELECT TOP 1 nurse_id, account_id, name FROM nurses "
                             f"WHERE nurse_id IN ({ph}) AND is_head_nurse=1 AND active=1"),
                        {f"m{i}": v for i, v in enumerate(mgr)}).fetchone()
    if hn is None:
        # ★ `groups.hn_id` 가 NULL 인 병동이 있다(실측: 메디통10). 관리자 미등록이라
        #   생성 주체를 못 잡는데, 검증 목적으로는 그룹 내 수간호사면 충분하다.
        #   `hn_auth=='HN'` 도 본다 — multi-group 은 그쪽이 판정축이다.
        hn = db.execute(text("SELECT TOP 1 nurse_id, account_id, name FROM nurses "
                             "WHERE group_id=:g AND active=1 AND (is_head_nurse=1 OR hn_auth='HN') "
                             "ORDER BY is_head_nurse DESC"), {"g": gid}).fetchone()
    if hn is None:
        return None
    office = db.query(Group.office_id).filter(Group.group_id == gid).scalar()
    return User(nurse_id=str(hn[0]), account_id=str(hn[1]), office_id=str(office),
                group_id=str(gid), is_head_nurse=True, hn_auth="HN", name=str(hn[2] or "hn"),
                EmpSeqNo=str(hn[0]), mb_part="", office_name="", mb_part_name="",
                gw_useYN="Y", qpis_useYN="N", official_title_name=None)


def load_criteria(db, gid, y, m, user):
    """생성기와 **같은 경로**로 병동별 검증 기준을 읽는다."""
    req = RosterRequest(year=y, month=m, group_id=gid)
    cfg = RCS._fetch_latest_config(db, req, user)
    cd = dict(cfg.__dict__) if cfg is not None else {}

    # 전원 고정근무 병동은 요구치가 0 이라 기본 가드에 걸린다 → 검증에선 끈다.
    _sm, base_req, by_day, max_by_day = RCS._build_shift_manage_and_requirements(
        db, user, cfg, req, require_nonzero_requirements=False)

    lookup = RCS._load_shift_lookup(db, user.office_id, gid)   # ★ 전역 .all() 금지
    shift_defs = [{"shift_id": s.shift_id,
                   "default_shift": (s.default_shift or s.shift_id),   # NULL → shift_id 폴백
                   "shift_gb": s.shift_gb, "type": s.type} for s in lookup.values()]
    id_to_main, _ = build_shift_normalizer(shift_defs)

    def g(k, d):
        # 엔진과 같은 시맨틱 — 컬럼이 있고 NULL 이면 default 가 아니라 None 이 실린다.
        return cd.get(k, d)

    _mn = g("max_nig_per_month", 15)
    if _mn is None or _mn <= 0:
        _mn = 15                                    # cp_sat_basic.py:471-478 과 동일 보정
    _bde = g("banned_day_after_eve", True)          # ★ 이 하나가 ND/NE/ED 셋을 다 끈다
    return {
        "cfg_id": getattr(cfg, "config_id", None),
        "max_conseq": g("max_conseq_work", 5) or 5,
        "trans_on": bool(_bde),                     # falsy(NULL 포함)면 전이 3종 미적용
        # ★ three_seq_nig 가 NULL 이면 L=3 이 아니라 L=2 다(falsy).
        "n_run_max": 3 if g("three_seq_nig", True) else 2,
        "rec2": bool(g("two_offs_after_two_nig", False)),
        "rec3": bool(g("two_offs_after_three_nig", True)),
        "one_n": bool(g("not_one_night", False)),
        "max_night": int(_mn),
        "use_mid": bool(g("use_mid", False)),
        "off_days": resolve_effective_off_days(cd),
        "cov_min": by_day, "cov_max": max_by_day, "cov_base": base_req,
        "id_to_main": id_to_main,
        "shift_rows": len(shift_defs),
    }


def to_main(code, id_to_main, unmapped):
    """RVERIFY-03 — 실패는 None. 호출부가 반드시 센다."""
    s = str(code or "").strip()
    if s in ("", "-"):
        return "-"
    u = s.upper()
    if u == "OFF":
        return "O"
    mm = id_to_main.get(u) or (u if u in MAIN_SET else None)
    if mm is None:
        unmapped[s] += 1
        return None
    return "O" if mm == "주" else mm      # 주휴는 OFF 로 센다(_normalize_to_main 과 동일)


# ───────────────────────────── 하드제약 판정 ─────────────────────────────

def active_range(gid_join, gid_leave, D):
    return max(0, gid_join), min(D - 1, gid_leave)


def check_nurse(seq, T0, T1, c, n_tail, offs_after):
    """간호사 1명의 하드제약 위반을 센다. seq 는 대표코드 리스트(0-index=1일).

    ★ 전부 활동구간 [T0,T1] 안에서만 판정한다. 밖은 솔버가 변수를 안 만든 자리다.
    """
    v = Counter()
    if T1 < T0:
        return v, 0
    win = seq[T0:T1 + 1]
    holes = sum(1 for x in win if x == "-")        # 구간 내 미배정 — 별도 보고
    N = lambda d: 0 <= d < len(seq) and seq[d] == "N"      # noqa: E731

    # A1. 1N 단독 — 해당일 N 이면 앞/뒤 중 최소 한 쪽이 N (fallback_lex 하드식)
    if c["one_n"] and T1 > T0:
        for d in range(T0, T1 + 1):
            if seq[d] != "N":
                continue
            if N(d - 1) and d - 1 >= T0:
                continue
            if N(d + 1) and d + 1 <= T1:
                continue
            # 면제: d==0(1일) 이고 전월 꼬리 N 이 이어짐. ★ 'T0' 아니라 리터럴 0 이다.
            if d == 0 and n_tail > 0:
                continue
            v["A1_1N단독"] += 1

    # A2. 2N→2OFF — d ∈ [T0+1, T1-2] (월말 2일에서 끝나는 블록은 제약 자체가 없다)
    if c["rec2"]:
        for d in range(T0 + 1, T1 - 1):
            if N(d - 1) and N(d) and not N(d + 1):
                if not (seq[d + 1] == "O" and seq[d + 2] == "O"):
                    v["A2_2N후2OFF"] += 1

    # A3. 3N→2OFF — d ∈ [T0+2, T1-2]
    if c["rec3"]:
        for d in range(T0 + 2, T1 - 1):
            if N(d - 2) and N(d - 1) and N(d):
                if not (seq[d + 1] == "O" and seq[d + 2] == "O"):
                    v["A3_3N후2OFF"] += 1

    # A4. N 연속 상한 — 임의 (L+1)일 창에서 N 합 <= L
    L = c["n_run_max"]
    for d in range(T0, T1 - L + 1):
        if all(N(d + t) for t in range(L + 1)):
            v[f"A4_N{L + 1}연속"] += 1
    # 월경계: 전월 꼬리 N 뒤에 OFF 가 하나라도 있으면(offs_after>0) 제약을 통째로 스킵한다.
    if T0 == 0 and n_tail > 0 and offs_after == 0:
        for w in range(1, n_tail + 1):
            span = L - w + 1
            if span <= 0:
                break
            if sum(1 for t in range(span) if N(t)) > L - w:
                v["A4_N연속_월경계"] += 1
                break

    # A5. 연속 근무일 상한 — 연속 (K+1)일 창마다 O 가 최소 1회. '근무'=O 아닌 모든 시프트.
    K = c["max_conseq"]
    for d in range(T0, T1 - K + 1):
        wnd = seq[d:d + K + 1]
        if len(wnd) < K + 1 or "-" in wnd:
            continue                                    # 미배정이 낀 창은 판정 불가
        if all(x in WORK for x in wnd):
            v[f"A5_연속근무{K + 1}일"] += 1

    # A6~A8. 전이 금지 — ★ banned_day_after_eve 하나가 셋을 다 끈다(NULL 이어도 꺼짐)
    if c["trans_on"]:
        for d in range(T0 + 1, T1 + 1):
            p, q = seq[d - 1], seq[d]
            if p == "N" and q == "D":
                v["A6_ND"] += 1
            elif p == "N" and q == "E":
                v["A7_NE"] += 1
            elif p == "E" and q == "D":
                v["A8_ED"] += 1

    # A9. 월 나이트 상한
    if sum(1 for x in win if x == "N") > c["max_night"]:
        v["A9_월N상한"] += 1
    return v, holes


def check_coverage(rows, c, unmapped, D):
    """RVERIFY-01 D — 일자별 최소 인원 미달. cov_min 은 0-index=1일."""
    under = Counter()
    keys = ["D", "E", "N"] + (["M"] if c["use_mid"] else [])
    per_day = [Counter() for _ in range(D)]
    for _nid, seq in rows:
        for d, code in enumerate(seq):
            if d < D and code in keys:
                per_day[d][code] += 1
    cmin = c["cov_min"] or {}
    for k in keys:
        need = cmin.get(k) if isinstance(cmin, dict) else None
        if not need:
            continue
        for d in range(min(D, len(need))):
            want = int(need[d] or 0)
            if want and per_day[d][k] < want:
                under[f"D_{k}미달"] += 1
    return under


# ───────────────────────────── 실행 ─────────────────────────────

def run():
    print(f"  {len(GROUPS)}곳 x {REPS}회 · 직렬 · AIDE_D5_LEX={os.environ.get('AIDE_D5_LEX', '(기본=0)')}\n",
          flush=True)
    agg, blocked = {}, []
    for name, gid in GROUPS:
        db = SessionLocal()
        try:
            user = make_user(db, gid)
            if user is None:
                print(f"  {name:<10} 수간호사 없음 — 건너뜀", flush=True)
                blocked.append((name, "수간호사 없음"))
                continue
            tm = db.execute(text("SELECT TOP 1 year, month FROM schedules WHERE group_id=:g "
                                 "AND dropped=0 ORDER BY created_at DESC"), {"g": gid}).fetchone()
            if tm is None:
                print(f"  {name:<10} 대상월 없음 — 건너뜀", flush=True)
                blocked.append((name, "대상월 없음"))
                continue
            Y, M = int(tm[0]), int(tm[1])
            try:
                crit = load_criteria(db, gid, Y, M, user)
            except Exception as e:
                print(f"  {name:<10} 기준 로딩 실패: {type(e).__name__}: {e}", flush=True)
                blocked.append((name, f"기준 로딩 실패 {type(e).__name__}"))
                continue
            # 활동구간 재현용 (fallback_lex.py:454-473 과 동일 규약)
            first_day = __import__("datetime").date(Y, M, 1)
            D = __import__("calendar").monthrange(Y, M)[1]
            jl = {}
            for nu in db.query(Nurse).filter(Nurse.group_id == gid).all():
                jd = nu.joining_date.date() if getattr(nu, "joining_date", None) else None
                rd = nu.resignation_date.date() if getattr(nu, "resignation_date", None) else None
                j = (jd - first_day).days if jd else 0
                if rd:
                    lw = rd - timedelta(days=1)
                    lo = (lw - first_day).days if lw >= first_day else -1
                else:
                    lo = D - 1
                jl[str(nu.nurse_id)] = (max(j, 0), min(lo, D - 1))
            print(f"  ── {name} {Y}-{M:02d}  연속근무<={crit['max_conseq']} · N연속<={crit['n_run_max']} · "
                  f"전이금지={'ON' if crit['trans_on'] else 'OFF'} · 1N금지={'ON' if crit['one_n'] else 'OFF'} · "
                  f"월N<={crit['max_night']} · 2N후OFF={'ON' if crit['rec2'] else 'OFF'} · "
                  f"shifts {crit['shift_rows']}행", flush=True)
        finally:
            db.close()

        for r in range(1, REPS + 1):
            buf, out = io.StringIO(), None
            db2 = SessionLocal()
            t0 = time.perf_counter()
            err = None
            try:
                with contextlib.redirect_stdout(buf):
                    out = RCS.generate_roster_service(
                        RosterRequest(year=Y, month=M, group_id=gid), user, db2)
            except Exception as e:
                err = f"{type(e).__name__}: {str(e)[:70]}"
            finally:
                db2.close()
            dt = time.perf_counter() - t0
            if not isinstance(out, dict) or not out.get("nurses"):
                print(f"  {name:<10}#{r} {dt:>5.0f}s  ✗ 생성실패 — {err or '빈 결과'}", flush=True)
                blocked.append((f"{name}#{r}", err or "빈 결과"))
                continue

            unmapped = Counter()
            rows = []
            for nu in out["nurses"]:
                nid = str(nu.get("id"))
                seq = [to_main(x, crit["id_to_main"], unmapped) or "-" for x in (nu.get("schedule") or [])]
                rows.append((nid, seq))
            # 전월 꼬리 (RVERIFY-04) — 생성 직후라 '생성 당시 전월' 과 같다
            tails, offs = {}, {}
            db3 = SessionLocal()
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    cm = RCS.build_cross_month_constraints(
                        db3, RosterRequest(year=Y, month=M, group_id=gid), user,
                        None, {}, [nid for nid, _ in rows])
                tails = cm.get("prev_month_n_tail") or {}
                offs = cm.get("prev_month_n_offs_after") or {}
            except Exception:
                pass                     # 실패해도 진행 — 월경계 면제만 못 쓴다
            finally:
                db3.close()

            tot, holes_tot = Counter(), 0
            for nid, seq in rows:
                T0, T1 = jl.get(nid, (0, D - 1))
                T0, T1 = active_range(T0, T1, D)
                v, h = check_nurse(seq, T0, T1, crit,
                                   int(tails.get(nid, 0) or 0), int(offs.get(nid, 0) or 0))
                tot.update(v)
                holes_tot += h
            tot.update(check_coverage(rows, crit, unmapped, D))

            bad = sum(v for k, v in tot.items() if k.startswith("A"))
            tag = "✓ 위반0" if bad == 0 else f"✗ 하드위반 {bad}"
            extra = []
            if unmapped:
                extra.append(f"매핑실패 {sum(unmapped.values())}({','.join(list(unmapped)[:3])})")
            if holes_tot:
                extra.append(f"미배정 {holes_tot}")
            cov = sum(v for k, v in tot.items() if k.startswith("D_"))
            if cov:
                extra.append(f"커버리지미달 {cov}")
            detail = " ".join(f"{k}={v}" for k, v in sorted(tot.items()) if k.startswith("A"))
            print(f"  {name:<10}#{r} {dt:>5.0f}s  {tag}  {' · '.join(extra)}  {detail}", flush=True)
            key = name
            agg.setdefault(key, {"runs": 0, "viol": Counter(), "unmapped": Counter(),
                                 "holes": 0, "cov": 0, "sec": []})
            a = agg[key]
            a["runs"] += 1
            a["viol"].update({k: v for k, v in tot.items() if k.startswith("A")})
            a["unmapped"].update(unmapped)
            a["holes"] += holes_tot
            a["cov"] += cov
            a["sec"].append(dt)

    # ── 종합 ──
    print(f"\n{'=' * 78}\n  종합 — 생성 성공 병동만. 실패는 아래 '검사 불가' 로 분리한다.\n", flush=True)
    print(f"  {'병동':<11}{'회':>3}{'하드위반':>9}{'매핑실패':>9}{'미배정':>8}{'커버리지':>9}{'소요중앙':>9}", flush=True)
    print("  " + "-" * 62, flush=True)
    clean = 0
    for name, a in agg.items():
        bad = sum(a["viol"].values())
        if bad == 0:
            clean += 1
        med = sorted(a["sec"])[len(a["sec"]) // 2] if a["sec"] else 0
        print(f"  {name:<11}{a['runs']:>3}{bad:>9}{sum(a['unmapped'].values()):>9}"
              f"{a['holes']:>8}{a['cov']:>9}{med:>8.0f}s", flush=True)
    print(f"\n  하드위반 0 인 병동: {clean}/{len(agg)}", flush=True)
    allv = Counter()
    for a in agg.values():
        allv.update(a["viol"])
    if allv:
        print("  위반 내역:", ", ".join(f"{k}={v}" for k, v in sorted(allv.items())), flush=True)
    allu = Counter()
    for a in agg.values():
        allu.update(a["unmapped"])
    if allu:
        print(f"  ★ 매핑 실패 코드(RVERIFY-03): {dict(allu)}", flush=True)
    if blocked:
        print(f"\n  ★ 검사 불가 {len(blocked)}건 — '위반 0' 아님:", flush=True)
        for nm, why in blocked:
            print(f"      {nm}: {why}", flush=True)


if __name__ == "__main__":
    run()
