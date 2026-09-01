"""Infeasible 케이스 캡처 — 라이브 진단이 실제로 보는 입력을 그대로 JSON 으로.

목적: 외부(오프라인)에서 진단·연구를 재현·실험할 수 있게, 생성 파이프가 진단에
넘기는 **효과적 입력 전부**(config 관련키 + per-nurse 속성 + initial_constraints)를
스냅샷한다. `AIDE_DUMP_CASE=<dir>` 환경변수로 게이팅(미설정=no-op).

replay: tools/infeasible_cases/replay.py 가 이 JSON 을 그대로 진단 스택에 먹인다.
"""

from __future__ import annotations

import json
import os
from typing import Any

# 진단·솔버 시퀀스가 읽는 config 키(값이 없으면 생략). 코퍼스를 얇게 유지.
_CONFIG_KEYS = (
    "daily_shift_requirements", "day_req", "eve_req", "nig_req",
    "off_days", "global_monthly_off_days", "standard_personal_off_days",
    "max_nig_per_month", "max_night_shifts_per_month",
    "max_consecutive_nights", "max_consec_nights", "max_conseq_work",
    "two_offs_after_two_nig", "two_offs_after_three_nig", "not_one_night",
    "ban_n_to_d", "ban_n_to_e", "banned_day_after_eve", "ban_night_before_fixed_off",
    "ban_night_before_fixed_wanted_off",
    "use_mid", "off_first", "preceptee_on",
)
# per-nurse 속성(진단이 읽는 것).
_NURSE_KEYS = (
    "nurse_id", "name", "grade", "team_id", "allowed_shifts", "is_night_only",
    "is_weekend_off", "n_exact", "n_min", "n_max", "d_exact", "e_exact",
    "d_min", "e_min", "fixed_shift", "joining_date", "resignation_date",
)


def _jsonable(v: Any) -> Any:
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, (list, tuple, set)):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if hasattr(v, "isoformat"):          # date/datetime
        try:
            return v.isoformat()
        except Exception:
            return str(v)
    return str(v)


def _attr(nu, k):
    return getattr(nu, k, None) if not isinstance(nu, dict) else nu.get(k)


def build_case(*, year: int, month: int, group_id: str, num_days: int,
               nurses: list, config: dict, cause=None) -> dict:
    """진단 입력 → 정규화된 케이스 dict(순수, 파일 불필요)."""
    cfg = {k: _jsonable(config.get(k)) for k in _CONFIG_KEYS if config.get(k) is not None}
    # initial_constraints(금지/강제OFF)는 통째로(진단 시퀀스의 핵심 입력).
    ic = config.get("initial_constraints") or {}
    cfg["initial_constraints"] = {
        "forbidden": _jsonable(ic.get("forbidden") or {}),
        "forced_off": _jsonable(ic.get("forced_off") or {}),
    }
    ns = []
    for nu in nurses or []:
        row = {k: _jsonable(_attr(nu, k)) for k in _NURSE_KEYS if _attr(nu, k) is not None}
        if row.get("nurse_id") is not None:
            ns.append(row)
    case: dict[str, Any] = {
        "meta": {"group_id": str(group_id), "source": "live-capture"},
        "year": int(year), "month": int(month), "num_days": int(num_days),
        "config": cfg, "nurses": ns,
    }
    if cause is not None:
        case["expected"] = {
            "classification": getattr(cause, "classification", None),
            "top_family": getattr(cause, "top_family", None),
            "certificate": getattr(cause, "certificate", None),
        }
    return case


def dump_case(out_dir: str, *, year: int, month: int, group_id: str, num_days: int,
              nurses: list, config: dict, cause=None) -> str | None:
    """AIDE_DUMP_CASE 경로에 케이스 JSON 저장. 실패 내성(응답 무영향)."""
    try:
        os.makedirs(out_dir, exist_ok=True)
        case = build_case(year=year, month=month, group_id=group_id,
                          num_days=num_days, nurses=nurses, config=config, cause=cause)
        cls = (case.get("expected") or {}).get("classification") or "unknown"
        fname = f"case-{year}{int(month):02d}-{group_id}-{cls}.json"
        path = os.path.join(out_dir, fname)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(case, f, ensure_ascii=False, indent=2)
        print(f"[CaseDump] 저장: {path} (nurses={len(case['nurses'])}, cls={cls})")
        return path
    except Exception as exc:
        print(f"[CaseDump] 실패(무시): {exc}")
        return None
