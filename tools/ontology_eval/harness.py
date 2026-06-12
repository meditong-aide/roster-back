"""오프라인 병동 Eval 하네스 (US-005) — 프로덕션 DB 무변경.

전략:
  1. generate_roster_service 를 호출하되, 엔진 진입점 generate_roster_cp_sat 을
     패치해 '엔진 입력(nurses/prefs/config/...)'을 캡처하고 즉시 중단(_CaptureDone).
     → 실제 서비스가 쓰는 것과 100% 동일한 엔진 입력을 얻는다.
  2. db.commit 을 no-op 으로 무력화 → 어떤 경로도 프로덕션에 영속화 못 함.
  3. 캡처한 입력으로 raw 엔진 generate_roster_cp_sat 을 직접 호출(스케줄 DB 쓰기 0).
  4. 하드위반 주입은 config_dict 의 deepcopy 에만 적용 → 원본 불변, 실행 후 폐기.

병동: 9B(10135890c287), ICU(10135857f9f9). 토큰은 기존 run_june_gen_*.py 에서
AST 로 read-only 추출(verify_exp=False 오프라인 디코드).
"""

from __future__ import annotations

import ast
import copy
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

import dotenv  # noqa: E402
dotenv.load_dotenv(ROOT / ".env")
dotenv.load_dotenv(APP / ".env")

WARD_SCRIPT = {"9B": "run_june_gen_9b", "ICU": "run_june_gen_icu"}
WARD_GROUP_ID = {"9B": "10135890c287", "ICU": "10135857f9f9"}


class _CaptureDone(Exception):
    """엔진 입력 캡처 후 service 를 조기 중단시키는 신호."""


def _load_token(ward: str) -> str:
    stem = WARD_SCRIPT[ward]
    p = ROOT / "tools" / f"{stem}.py"
    tree = ast.parse(p.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "TOKEN" for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise RuntimeError(f"{ward}: TOKEN not found in {stem}.py")


def _build_user(token: str):
    from jose import jwt
    secret = os.getenv("SECRET_KEY")
    if not secret:
        raise SystemExit("SECRET_KEY env var missing.")
    payload = jwt.decode(token, secret, algorithms=["HS256"], options={"verify_exp": False})
    from schemas.auth_schema import User  # type: ignore
    return User(
        nurse_id=str(payload["nurse_id"]),
        account_id=str(payload["account_id"]),
        office_id=str(payload["office_id"]),
        group_id=str(payload["group_id"]),
        is_head_nurse=bool(payload.get("is_head_nurse", False)),
        is_master_admin=bool(payload.get("is_master_admin", False)),
        name=str(payload.get("name", "")),
        EmpSeqNo=str(payload.get("EmpSeqNo") or ""),
        EmpAuthGbn=str(payload.get("EmpAuthGbn") or "MEM"),
        mb_part=str(payload.get("mb_part", "")),
        office_name=str(payload.get("office_name", "")),
        mb_part_name=str(payload.get("mb_part_name", "")),
        gw_useYN=str(payload.get("gw_useYN", "Y")),
        qpis_useYN=str(payload.get("qpis_useYN", "Y")),
        official_title_name=payload.get("official_title_name"),
        hn_auth=payload.get("hn_auth"),
        original_group_id=payload.get("original_group_id"),
    )


class _ReadOnlyGuard:
    """db.commit 을 무력화해 프로덕션 영속화를 차단. write 시도 카운트 기록."""
    def __init__(self):
        self.commit_calls = 0
        self.flush_calls = 0


def capture_engine_inputs(ward: str, year: int, month: int, *, grade_strategy: str = "COMBINED") -> dict[str, Any]:
    """실제 service 가 엔진에 넘기는 입력을 캡처(프로덕션 무변경)."""
    token = _load_token(ward)
    user = _build_user(token)

    import services.cp_sat_basic as cpb
    import services.roster_create_service as rcs
    from db.client2 import SessionLocal
    from schemas.roster_schema import RosterRequest

    captured: dict[str, Any] = {"ward": ward, "year": year, "month": month}
    guard = _ReadOnlyGuard()
    # _run_cp_sat_basic 은 rcs 모듈 글로벌에 바인딩된 generate_roster_cp_sat 을 호출한다.
    # 따라서 rcs 네임스페이스를 패치해야 가로챌 수 있다(cpb 만 패치하면 우회됨).
    orig_engine_rcs = getattr(rcs, "generate_roster_cp_sat", None)
    orig_engine_cpb = cpb.generate_roster_cp_sat

    def _cap(nurses_data, prefs_data, config_data, y, m, shift_manage_data,
             time_limit_seconds=60, grade_strategy="BASE", grade_config=None, **kw):
        captured.update(
            nurses_data=nurses_data,
            prefs_data=prefs_data,
            config_data=config_data,
            shift_manage_data=shift_manage_data,
            grade_strategy=grade_strategy,
            grade_config=grade_config,
        )
        raise _CaptureDone()

    rcs.generate_roster_cp_sat = _cap  # type: ignore
    cpb.generate_roster_cp_sat = _cap

    # ── 사이드채널 차단(F1) ──────────────────────────────────────────────────
    # flush_pending_transfers / flush_expired_preceptees 가 set_app_push 류를 통해
    # msdb_manager(세션 rollback 밖의 독립 pymssql 연결)로 푸시 발송 + 프로덕션 write
    # 를 한다. session 패치로는 못 막으므로 (a) push 함수 no-op (b) msdb_manager.execute
    # (raw write 경로; 읽기는 fetch_* 별도 메서드라 영향 없음) no-op 로 이중 차단.
    import utils.utils as _uu
    import db.client2 as _c2
    _push_names = ("set_app_push", "send_transfer_completed_push",
                   "send_assignment_created_push", "send_assignment_cancelled_push")
    _orig_push = {n: getattr(_uu, n, None) for n in _push_names}
    for n in _push_names:
        if hasattr(_uu, n):
            setattr(_uu, n, (lambda *a, **k: None))
    _orig_msdb_execute = getattr(_c2.msdb_manager, "execute", None)
    try:
        _c2.msdb_manager.execute = lambda *a, **k: 0  # type: ignore
    except Exception:
        pass

    db = SessionLocal()

    # commit → flush 로 치환: 객체는 트랜잭션 내에서 persistent 가 되어 service 흐름이
    # 정상 진행되지만(은 'is not persistent' 회피), 마지막 rollback 으로 전부 폐기 →
    # 프로덕션에는 아무것도 영속화되지 않는다(autocommit=False 세션).
    def _commit_as_flush(*a, **k):
        guard.commit_calls += 1
        try:
            db.flush()
        except Exception:
            pass

    db.commit = _commit_as_flush  # type: ignore

    req = RosterRequest(year=year, month=month, grade_strategy=grade_strategy)
    try:
        rcs.generate_roster_service(req, user, db)
    except _CaptureDone:
        pass
    except Exception as exc:  # 로딩 단계 오류는 기록(엔진 입력 캡처 실패)
        import traceback
        captured["load_error"] = f"{type(exc).__name__}: {exc}"
        captured["load_tb"] = traceback.format_exc()[-1500:]
    finally:
        if orig_engine_rcs is not None:
            rcs.generate_roster_cp_sat = orig_engine_rcs
        cpb.generate_roster_cp_sat = orig_engine_cpb
        for n, fn in _orig_push.items():
            if fn is not None:
                setattr(_uu, n, fn)
        if _orig_msdb_execute is not None:
            try:
                _c2.msdb_manager.execute = _orig_msdb_execute  # type: ignore
            except Exception:
                pass
        try:
            db.rollback()  # flush 된 모든 write 폐기 → 프로덕션 무변경
        except Exception:
            pass
        try:
            db.close()
        except Exception:
            pass
    captured["_commit_calls_blocked"] = guard.commit_calls
    captured["captured_ok"] = "config_data" in captured
    return captured


def clone(captured: dict[str, Any]) -> dict[str, Any]:
    """config_data / grade_config 만 deepcopy 한 captured 사본(원본 불변).
    nurses_data/prefs/shift_manage 는 read-only 공유(ORM 객체 deepcopy 회피)."""
    c = dict(captured)
    c["config_data"] = copy.deepcopy(captured["config_data"])
    c["grade_config"] = copy.deepcopy(captured.get("grade_config"))
    return c


def run_engine(captured: dict[str, Any], *, time_limit_seconds: int = 25) -> dict[str, Any]:
    """captured(또는 inject/apply 로 변형된 사본)로 raw 엔진 직접 호출. DB write 없음."""
    from services.cp_sat_basic import generate_roster_cp_sat

    result = generate_roster_cp_sat(
        captured["nurses_data"],
        captured["prefs_data"],
        captured["config_data"],
        captured["year"],
        captured["month"],
        captured["shift_manage_data"],
        time_limit_seconds=time_limit_seconds,
        grade_strategy=captured.get("grade_strategy", "BASE"),
        grade_config=captured.get("grade_config"),
    )
    return _summarize(result)


def _summarize(result: Any) -> dict[str, Any]:
    """엔진 결과 → feasibility 요약."""
    roster = None
    rs = None
    if isinstance(result, dict):
        roster = result.get("roster")
        rs = result.get("roster_system")
    else:
        roster = result
    work_cells = 0
    total_cells = 0
    if isinstance(roster, dict):
        for shifts in roster.values():
            if isinstance(shifts, list):
                for c in shifts:
                    total_cells += 1
                    code = str(c).strip().upper()
                    if code not in {"-", "O", "OFF", "주", "", "NONE"}:
                        work_cells += 1
    status = getattr(rs, "_solve_status_text", None) if rs is not None else None
    feasible = work_cells > 0
    return {
        "feasible": feasible,
        "work_cells": work_cells,
        "total_cells": total_cells,
        "status": status,
        "roster": roster,
        "roster_system": rs,
    }


# ── 하드위반 주입 (captured 사본에만 — config_data/grade_config) ───────────────
def inject_c1_coverage_overload(captured: dict, *, day_index: int = 0, shift: str = "N",
                                amount: int = 99) -> dict:
    """C1: 특정일 coverage demand 를 가용 한참 초과로 — daily_shift_requirements 과다."""
    c = clone(captured)
    cfg = c["config_data"]
    baseline = int((cfg.get("daily_shift_requirements") or {}).get(shift, 2) or 2)
    by_day = cfg.get("daily_shift_requirements_by_day")
    if isinstance(by_day, list) and 0 <= day_index < len(by_day) and isinstance(by_day[day_index], dict):
        by_day[day_index][shift] = amount
    req = dict(cfg.get("daily_shift_requirements") or {})
    req[shift] = amount
    cfg["daily_shift_requirements"] = req
    c["_injected"] = {"case": "C1", "day": day_index, "shift": shift, "amount": amount,
                      "family": "CoverageMin", "knob": "daily_shift_requirements", "baseline": baseline}
    return c


def inject_c2_team_min_overload(captured: dict, **_kw) -> dict:
    """C2: team 교차시프트 과구독 — 한 팀의 D/E/N team_min 을 각각 팀원 수로 설정.
    각 min 이 팀 가용(==)이라 skip 되지 않고 enforce 되지만, 한 사람이 하루 1시프트만
    가능하므로 동시 충족 불가 → 진짜 infeasible (skip 회피)."""
    from collections import Counter
    c = clone(captured)
    cfg = c["config_data"]
    counts = Counter(str(n.get("team_id")) for n in c["nurses_data"] if n.get("team_id") is not None)
    tmin = {k: dict(v or {}) for k, v in (cfg.get("team_min_by_team") or {}).items()}
    team = sorted(counts, key=lambda t: -counts[t])[0] if counts else "1"
    size = counts.get(team, 0) or 1
    baseline = dict(tmin.get(team, {}))
    tmin[team] = {"D": size, "E": size, "N": size}     # 합=3×size > size 팀원 → 과구독
    cfg["team_min_by_team"] = tmin
    c["_injected"] = {"case": "C2", "team": team, "team_size": size, "family": "TeamMin",
                      "knob": "team_min_by_team", "baseline": baseline,
                      "shifts": ["D", "E", "N"]}
    return c


def inject_c3_grade_max_too_low(captured: dict, *, shift: str = "N") -> dict:
    """C3: grade_max 과소 — 각 등급 상한을 그 등급의 grade_min 수준으로 눌러
    총 cap(=Σmin) < 필요 가 되게 한다. grade_min ≤ grade_max 를 유지하므로
    min>max 모순이 아니라 '순수 grade_max 용량 부족' 을 만든다."""
    from collections import Counter
    c = clone(captured)
    gc = c.get("grade_config") or {}
    grade_min_shift = (gc.get("constraints_json") or {}).get(shift, {})
    grades = {str(g) for g in Counter(n.get("grade") for n in c["nurses_data"]) if g is not None}
    cmax = dict(gc.get("constraints_max_json") or {})
    baseline = dict(cmax.get(shift, {}))
    # 각 등급 max = 그 등급의 min(없으면 0) → 합이 필요보다 작아짐, 모순 없음
    cmax[shift] = {g: int(grade_min_shift.get(g, 0) or 0) for g in grades}
    gc["constraints_max_json"] = cmax
    c["grade_config"] = gc
    c["_injected"] = {"case": "C3", "shift": shift, "family": "GradeMax",
                      "knob": "constraints_max_json", "baseline": baseline}
    return c


def inject_c4_night_cap_too_low(captured: dict, *, cap: int = 1) -> dict:
    """C4: 월 N 상한(per-nurse)을 극소로 — 엔진 하드 키는 max_nig_per_month(기본 15).
    capacity = nurses × cap < 월 N 수요 → infeasible."""
    c = clone(captured)
    cfg = c["config_data"]
    baseline = int(cfg.get("max_nig_per_month", 15) or 15)
    cfg["max_nig_per_month"] = cap
    c["_injected"] = {"case": "C4", "cap": cap, "family": "MonthlyNightCap",
                      "knob": "max_nig_per_month", "baseline": baseline}
    return c


INJECTORS = {
    "C1": inject_c1_coverage_overload,
    "C2": inject_c2_team_min_overload,
    "C3": inject_c3_grade_max_too_low,
    "C4": inject_c4_night_cap_too_low,
}


if __name__ == "__main__":
    # 데모: 9B 2026-06 baseline 캡처 + 1회 구동
    ward = sys.argv[1] if len(sys.argv) > 1 else "9B"
    year = int(sys.argv[2]) if len(sys.argv) > 2 else 2026
    month = int(sys.argv[3]) if len(sys.argv) > 3 else 6
    print(f"[harness] capture {ward} {year}-{month:02d} ...")
    cap = capture_engine_inputs(ward, year, month)
    print(f"[harness] captured_ok={cap.get('captured_ok')} commit_blocked={cap.get('_commit_calls_blocked')} "
          f"load_error={cap.get('load_error')}")
    if cap.get("captured_ok"):
        print(f"[harness] nurses={len(cap['nurses_data'])} config_keys={len(cap['config_data'])}")
        base = run_engine(cap, time_limit_seconds=int(os.getenv('HARNESS_TL', '20')))
        print(f"[harness] baseline feasible={base['feasible']} work_cells={base['work_cells']} status={base['status']}")
