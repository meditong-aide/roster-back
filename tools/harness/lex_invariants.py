"""lex 체인 불변식 검사 — 단계 간 발산과 힌트 수용을 실제 병동으로 확인한다.

왜 필요한가 (2026-09-10 · 이 세션에서 확정된 결함 2건이 같은 종류였다):
  · S4-② `sum(safety3) == sum(safety2)` — 양변이 변수식이라 계수 상쇄로 `0==0` 빈 제약
  · grade 중복 등록 — `_grade_cell_spec` 이 두 벌 쌓여 동결식 상한이 절반
  둘 다 **m2≠m3 발산이고 둘 다 무증상**이었다. 오류로 안 보이고 근무표 품질만 조용히 나빠진다.
  grade 쪽은 11곳 중 6곳에서 stage3 를 통째로 죽이고 있었는데도 로그상 정상이었다.

검사 둘 — 같은 실행에서 나오므로 추가 비용이 없다.

  [A] 발산 — 직전 단계 해가 다음 모델에서 정말 feasible 한가
      `fix_variables_to_their_hinted_value=True` 로 힌트를 고정하고 푼다.
        INFEASIBLE   → 발산 확정. 그 단계의 동결·무회귀 제약이 직전 해와 모순이다
        FEASIBLE/OPT → 정상
        UNKNOWN      → **판정 불능**. 리밋을 늘려 재실행한다. **통과로 세지 않는다**
      ★ UNKNOWN 을 pass 로 세면 이 검사가 조용히 무력화된다.

  [B] 힌트 수용 — 준 힌트를 CP-SAT 이 실제로 첫 해로 받는가
      판정축은 프리솔브의 complete/incomplete 문구가 **아니라** 첫 해 로그의 `[hint]` 태그다.
      부분 힌트여도 완성에 성공하면 `#1 ... [hint]` 로 찍힌다.
      (`"complete" in "incomplete"` 가 True 라 문구 매칭은 정반대 결론을 낳는다.)
      실측: d5-lex 는 4/4 완성 실패(해 자체가 없음)인데 발산은 아니었다 —
      **발산은 없는데 힌트만 안 먹는 경우**가 실재하므로 두 축을 함께 본다.

사용법 (반드시 저장소 루트에서):
    cd roster-back
    uv run python tools/harness/lex_invariants.py                 # 기본 4곳 × 1회
    G=<gid>,<gid> R=3 FIXLIMIT=30 uv run python tools/harness/lex_invariants.py
    PHASES=stage3,stage3:d5-lex uv run python tools/harness/lex_invariants.py

종료 코드: 발산 또는 판정불능이 1건이라도 있으면 **1**. CI 게이트로 쓸 수 있다.
"""
import argparse
import contextlib
import io
import os
import re
import sys
import time
from collections import Counter, defaultdict

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_ROOT, "app"))

from sqlalchemy import text  # noqa: E402
from db.client2 import SessionLocal  # noqa: E402
from db.models import Group  # noqa: E402
from schemas.auth_schema import User  # noqa: E402
from schemas.roster_schema import RosterRequest  # noqa: E402
import services.roster_create_service as RCS  # noqa: E402
import services.cp_sat.fallback_lex as FL  # noqa: E402
from ortools.sat.python import cp_model  # noqa: E402

DEFAULT_GROUPS = [
    ("시화9A", "101358f6de7b"), ("시화9B", "10135890c287"),
    ("인천별관1", "1022432916e6"), ("나사렛7B", "1025768b9f1c"),
]

# ★ 프리솔브가 찍는 힌트 판정 줄만 정확히 잡는다.
#   `hint` 나 `solution hint` 로 넓히면 solve 종료 통계의 `'fj solution hints'` 행
#   (Solution repositories 표)이 걸려 **힌트를 주지 않은 패스가 '미수용' 으로 오탐된다.**
HINT_LINE = re.compile(r"The solution hint is", re.I)
FIRST_SOL = re.compile(r"^#(\d+)\s+([\d.]+)s")


def _resolve_user(db, group_id: str):
    """생성 주체(수간호사)를 찾는다. `groups.hn_id` 가 NULL 인 병동이 있어 폴백을 둔다."""
    import json

    raw = db.execute(text("SELECT hn_id FROM groups WHERE group_id=:g"), {"g": group_id}).scalar()
    mgr = json.loads(raw) if isinstance(raw, str) else list(raw or [])
    row = None
    if mgr:
        ph = ", ".join(f":m{i}" for i in range(len(mgr)))
        row = db.execute(
            text("SELECT TOP 1 nurse_id, account_id, name FROM nurses "
                 f"WHERE nurse_id IN ({ph}) AND is_head_nurse=1 AND active=1"),
            {f"m{i}": v for i, v in enumerate(mgr)},
        ).fetchone()
    if row is None:
        row = db.execute(
            text("SELECT TOP 1 nurse_id, account_id, name FROM nurses "
                 "WHERE group_id=:g AND active=1 AND (is_head_nurse=1 OR hn_auth='HN') "
                 "ORDER BY is_head_nurse DESC"),
            {"g": group_id},
        ).fetchone()
    if row is None:
        return None
    office = db.query(Group.office_id).filter(Group.group_id == group_id).scalar()
    return User(
        nurse_id=str(row[0]), account_id=str(row[1]), office_id=str(office),
        group_id=str(group_id), is_head_nurse=True, hn_auth="HN", name=str(row[2] or "hn"),
        EmpSeqNo=str(row[0]), mb_part="", office_name="", mb_part_name="",
        gw_useYN="Y", qpis_useYN="N", official_title_name=None,
    )


def _install_probe(targets: set[str], fix_limit: int, observations: list,
                   repair_hint: bool = False, conflict_limit: int = 0):
    """`_solve_traced` 를 감싸 표적 패스에 힌트 고정을 걸고 로그를 수집한다.

    `repair_hint` / `conflict_limit` 은 **힌트 수용 개선 A/B 용 처치**다(기본 off).
    표적이 아닌 **모든 패스**에 적용해 "힌트가 실제로 받아들여지는가" 를 바꿔 본다.
      · repair_hint       — 힌트가 infeasible 해도 복구를 시도한다
      · hint_conflict_limit — 완성 탐색 예산(기본 10). 부분 힌트를 채우는 데 쓰인다
    """
    original = FL._solve_traced

    def traced(solver, model, logger_prefix, phase):
        lines: list[str] = []
        solver.parameters.log_search_progress = True
        solver.parameters.log_to_stdout = False   # 콜백으로만 받아 stdout 을 오염시키지 않는다
        try:
            solver.log_callback = lines.append
        except Exception:
            pass
        fixed = phase in targets
        if fixed:
            solver.parameters.fix_variables_to_their_hinted_value = True
            solver.parameters.max_time_in_seconds = fix_limit
        else:
            # ★ 표적 패스에는 걸지 않는다 — 거기선 힌트가 이미 고정이라 의미가 없고,
            #   [B] 판정에서도 제외된다.
            if repair_hint:
                solver.parameters.repair_hint = True
            if conflict_limit:
                solver.parameters.hint_conflict_limit = conflict_limit
        started = time.perf_counter()
        status = original(solver, model, logger_prefix, phase)
        observations.append({
            "phase": phase,
            "fixed": fixed,
            "status": status,
            "status_text": FL._cp_sat_status_to_text(status),
            "seconds": time.perf_counter() - started,
            "hints": [ln.strip() for ln in lines if HINT_LINE.search(ln)],
            "firsts": [ln.strip() for ln in lines if FIRST_SOL.match(ln.strip())][:1],
        })
        return status

    FL._solve_traced = traced
    return original


def _hint_verdict(obs: dict) -> str:
    # ★★ [A] 발산 검사가 표적 패스에 `fix_variables_to_their_hinted_value` 를 걸므로
    #   그 패스는 힌트가 **고정**된다 — 수용은 자명하고 [B] 의 판정 대상이 아니다.
    #   이걸 빼지 않으면 [A] 가 [B] 를 오염시켜 "d5-lex 도 힌트를 잘 받는다" 는
    #   정반대 결론이 나온다(실측으로 한 번 겪었다).
    if obs["fixed"]:
        return "고정(판정제외)"
    if not obs["hints"]:
        return "힌트없음"
    joined = " ".join(obs["hints"]).lower()
    first = " ".join(obs["firsts"])
    if "infeasible" in joined:
        return "힌트infeasible"
    # ★★ 힌트 유래 해의 태그는 **둘**이다.
    #   `[hint]`     힌트를 그대로(또는 완성해서) 받은 경우
    #   `[repaired]` `repair_hint=True` 로 힌트를 복구해 받은 경우
    #   `[hint]` 만 보면 repair_hint 처치 조건에서 **전부 미수용으로 오판**한다
    #   (실측: 처치 A/B 에서 수용 47 → 0 이라는 거짓 결과가 나왔다).
    if "[hint]" in first or "[repaired]" in first:
        return "수용"
    if not first:
        return "해없음"
    return "미수용"


def _divergence_verdict(obs: dict) -> str:
    if not obs["fixed"]:
        return ""
    if obs["status"] == cp_model.INFEASIBLE:
        return "발산"
    if obs["status"] in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return "정상"
    return "판정불능"


def main() -> int:
    parser = argparse.ArgumentParser(description="lex 체인 불변식 검사")
    parser.add_argument("--groups", default=os.getenv("G", ""),
                        help="쉼표 구분 group_id. 비우면 기본 4곳")
    parser.add_argument("--reps", type=int, default=int(os.getenv("R", "1")))
    parser.add_argument("--phases", default=os.getenv("PHASES", "stage3,stage3:d5-lex"),
                        help="발산 검사 표적 패스(쉼표 구분)")
    parser.add_argument("--fix-limit", type=int, default=int(os.getenv("FIXLIMIT", "30")))
    # ── 힌트 수용 개선 A/B 용 처치(기본 off = 현행) ──
    parser.add_argument("--repair-hint", action="store_true",
                        default=os.getenv("REPAIR_HINT") == "1",
                        help="repair_hint=True — 힌트가 infeasible 해도 복구 시도")
    parser.add_argument("--hint-conflict-limit", type=int,
                        default=int(os.getenv("HINT_CONFLICT_LIMIT", "0")),
                        help="hint_conflict_limit(기본 10) 상향. 0 이면 건드리지 않음")
    args = parser.parse_args()

    only = [x.strip() for x in args.groups.split(",") if x.strip()]
    groups = [(n, g) for n, g in DEFAULT_GROUPS if not only or g in only or n in only]
    if only and not groups:
        groups = [(g, g) for g in only]
    targets = {p.strip() for p in args.phases.split(",") if p.strip()}

    observations: list = []
    _install_probe(targets, args.fix_limit, observations,
                   repair_hint=args.repair_hint, conflict_limit=args.hint_conflict_limit)
    os.environ.setdefault("AIDE_D5_LEX", "1")   # 표적에 넣었으면 켜야 관찰된다

    treat = []
    if args.repair_hint:
        treat.append("repair_hint")
    if args.hint_conflict_limit:
        treat.append(f"conflict_limit={args.hint_conflict_limit}")
    print(f"  lex 불변식 검사 — {len(groups)}곳 × {args.reps}회 · "
          f"발산 표적 {sorted(targets)} · 힌트고정 {args.fix_limit}s · "
          f"처치={'+'.join(treat) if treat else '없음(현행)'}\n", flush=True)

    div = Counter()
    hint = defaultdict(Counter)
    rows: list = []
    pass_seconds: list = []      # 힌트 판정 대상 패스의 소요 — A/B 에서 시간 효과를 본다
    for name, gid in groups:
        for rep in range(1, args.reps + 1):
            db = SessionLocal()
            try:
                user = _resolve_user(db, gid)
                tm = db.execute(
                    text("SELECT TOP 1 year, month FROM schedules WHERE group_id=:g "
                         "AND dropped=0 ORDER BY created_at DESC"), {"g": gid}).fetchone()
            finally:
                db.close()
            if user is None or tm is None:
                print(f"  {name} #{rep} 대상 아님(수간호사 또는 대상월 없음)", flush=True)
                continue
            observations.clear()
            db2 = SessionLocal()
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    RCS.generate_roster_service(
                        RosterRequest(year=int(tm[0]), month=int(tm[1]), group_id=gid), user, db2)
            except Exception as exc:
                print(f"  {name} #{rep} 생성실패: {type(exc).__name__}: {str(exc)[:60]}", flush=True)
                continue
            finally:
                try:
                    db2.rollback()
                except Exception:
                    pass
                db2.close()

            for obs in observations:
                hv = _hint_verdict(obs)
                dv = _divergence_verdict(obs)
                hint[obs["phase"]][hv] += 1
                if hv not in ("힌트없음", "고정(판정제외)"):
                    pass_seconds.append(obs["seconds"])
                bad_h = hv in ("해없음", "미수용", "힌트infeasible")
                if dv:
                    div[dv] += 1
                    rows.append((name, rep, obs["phase"], obs["status_text"], dv, hv))
                # 발산 표적이거나 힌트가 안 먹은 회차는 **어느 병동 어느 회차인지** 남긴다.
                # 집계만 보면 "8회 중 1회" 가 특정 회차에 몰린 것인지 흩어진 것인지 모른다.
                if dv or bad_h:
                    flag = "★★" if (dv in ("발산", "판정불능") or bad_h) else "  "
                    print(f"  {flag}{name:<9}#{rep} {obs['phase']:<20}"
                          f"{obs['status_text']:>12} 발산={dv or '—'} 힌트={hv}"
                          f"  {obs['seconds']:.1f}s", flush=True)

    print("\n  ── [A] 발산 검사 ──", flush=True)
    print(f"  정상 {div['정상']} · **발산 {div['발산']}** · **판정불능 {div['판정불능']}**", flush=True)
    print("\n  ── [B] 힌트 수용 ──", flush=True)
    print(f"  {'패스':<24}판정 분포", flush=True)
    print("  " + "-" * 64, flush=True)
    for phase, counter in hint.items():
        print(f"  {phase:<24}{dict(counter)}", flush=True)

    # ── A/B 비교용 한 줄 요약 ── 처치 전후를 이 줄만으로 대조할 수 있게 한다.
    tot_ok = sum(v for c in hint.values() for k, v in c.items() if k == "수용")
    tot_bad = sum(v for c in hint.values() for k, v in c.items()
                  if k in ("미수용", "해없음", "힌트infeasible"))
    tot_sec = sum(o for o in pass_seconds)
    print(f"\n  ▶ 요약  수용 {tot_ok} · 미수용·해없음 {tot_bad} "
          f"({tot_bad / max(1, tot_ok + tot_bad) * 100:.1f}%) · "
          f"힌트대상 패스 소요합 {tot_sec:.1f}s · 처치={'+'.join(treat) if treat else '없음'}",
          flush=True)

    bad_hint = {p: dict(c) for p, c in hint.items()
                if any(k in ("해없음", "미수용", "힌트infeasible") for k in c)}
    if div["발산"]:
        print("\n  ★★ 발산이 있다 — 그 단계의 동결·무회귀 제약이 직전 해와 모순이다.", flush=True)
        print("     MUS 로 제약을 특정하려면 AIDE_ENABLE_MUS_REGISTRY=1 로 재실행한다.", flush=True)
        print("     ※ MUS 를 켜면 solve 가 4~6배 느려져 INFEASIBLE 이 UNKNOWN 으로 바뀔 수 있다"
              "(관측이 대상을 바꾼다). 발산이 확인된 뒤에만 켠다.", flush=True)
    if div["판정불능"]:
        print(f"\n  ★ 판정불능 {div['판정불능']}건 — --fix-limit 을 늘려 재실행한다"
              f"(현재 {args.fix_limit}s). '발산 없음' 으로 읽지 말 것.", flush=True)
    if bad_hint:
        print("\n  ★ 힌트가 수용되지 않는 패스:", flush=True)
        for phase, counter in bad_hint.items():
            print(f"      {phase:<24}{counter}", flush=True)
        print("     고칠 곳: repair_hint · hint_conflict_limit 상향 · "
              "(프리솔브 쪽이면) keep_all_feasible_solutions_in_presolve", flush=True)
    if not div["발산"] and not div["판정불능"] and not bad_hint:
        print("\n  이상 없음.", flush=True)

    # ★ exit 0 으로 끝나면 아무도 안 본다. 발산·판정불능은 실패로 만든다.
    return 1 if (div["발산"] or div["판정불능"]) else 0


if __name__ == "__main__":
    sys.exit(main())
