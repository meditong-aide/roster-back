"""manage-daily-shift skill — 시프트별 필요인원(커버리지) 설정.

엔진 커버리지의 진짜 소스는 **DailyShift(일자별)**다(1순위) — RosterConfig.day_req 가
아니다(엔진 무시, test_coverage_source 로 실측 고정). 그래서 이 스킬은 DailyShift 를 건드린다.

두 모드를 구분해 각각 다른 서비스 경로로 캐치한다:
  - scope="day"   특정일  → get_or_init_month 로 현재 배열 읽고 그 날만 patch → update_daily
  - scope="month" 월 일괄 → apply_bulk_to_days(apply_globally=True): 전 날짜 + ShiftManage.manpower(fallback) 동기

group_id 스코프는 미들웨어가 주입하는 group_id 로 지킨다(엔드포인트 resolve_effective_group 대체).
"""

from __future__ import annotations

from datetime import date as _date
from typing import Any

from sqlalchemy.orm import Session

from agents_v2.grounding.internal import resolve_date, resolve_pattern_days
from agents_v2.skills.manifest import skill
from agents_v2.verify import VerifyResult, readback
from services import daily_shift_service

MANAGE_DAILY_SHIFT_SCHEMA: dict = {
    "name": "manage_daily_shift",
    "description": (
        "**시프트별 필요인원(커버리지)** 을 설정합니다 — 그 날/그 달 D·E·N 각각 몇 명 필요한지. (HN/ADM 전용)\n\n"
        "엔진이 근무표 생성 시 실제로 읽는 커버리지는 **일자별(DailyShift)** 이다. "
        "'데이 필요인원'을 병동 정책(update_constraint)으로 바꾸려 하지 마라 — 그건 엔진이 무시한다.\n\n"
        "─────────── scope ───────────\n"
        "- `day` — **특정일**. 예: '8월 7일 D 8, E 7, N 3'. date 필요.\n"
        "- `weekend` — **주말(토·일) 전부**. 예: '8월 주말 322'. 날짜 계산은 시스템이 함(넌 scope만).\n"
        "- `weekday` — **평일(월~금) 전부**. 예: '8월 평일 데이 5명'.\n"
        "- `month` — **월 전체 일괄**. 예: '8월 데이 전부 5명', '매일 나이트 3명'.\n\n"
        "⚠️ '주말'/'평일' 은 **날짜를 네가 세지 마라** — scope=weekend/weekday 만 주면 시스템이 그 달의 "
        "정확한 날짜를 계산한다. (LLM 캘린더 추론 금지)\n\n"
        "─────────── 파라미터 ───────────\n"
        "- `scope` — day / weekend / weekday / month. 날짜 있으면 day, '주말'이면 weekend, '평일'이면 weekday, '전부/매일'이면 month.\n"
        "- `date` — 대상 일자 YYYY-MM-DD (scope=day). '8월 7일'=2026-08-07.\n"
        "- `d_count`/`e_count`/`n_count` — 각 시프트 필요인원(정수). 준 것만 바꾸고 나머지는 유지.\n\n"
        "예) '8월 7일 데이 8명 이브닝 7명 나이트 3명' → scope=day, date=2026-08-07, d_count=8, e_count=7, n_count=3\n"
        "예) '8월 주말 322' → scope=weekend, d_count=3, e_count=2, n_count=2 (date 없음 — 시스템이 주말 날짜 계산)\n"
        "예) '8월 나이트 전부 3명으로' → scope=month, n_count=3\n"
        "⚠️ 등록은 preview_only=true 로 먼저 호출해 미리보기를 만들고 사용자 확인 후 실행됩니다."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "scope": {"type": "string", "enum": ["day", "weekend", "weekday", "month"],
                      "description": "day=특정일 / weekend=주말 / weekday=평일 / month=월 일괄"},
            "date": {"type": "string", "description": "대상 일자 YYYY-MM-DD (scope=day). '8월 7일'=2026-08-07"},
            "d_count": {"type": "integer", "description": "데이(D) 필요인원"},
            "e_count": {"type": "integer", "description": "이브닝(E) 필요인원"},
            "n_count": {"type": "integer", "description": "나이트(N) 필요인원"},
            "preview_only": {"type": "boolean", "default": True},
        },
        "required": [],
    },
}


def _iso(value: Any, params: dict) -> str | None:
    if not value:
        return None
    s = str(value)
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    return resolve_date(s, params.get("year", _date.today().year), params.get("month", 1))


def _counts(params: dict) -> dict[str, int]:
    """준 시프트 카운트만 뽑는다(D/E/N)."""
    out = {}
    for key, col in (("d_count", "D"), ("e_count", "E"), ("n_count", "N")):
        v = params.get(key)
        if v is not None:
            out[col] = int(v)
    return out


def _scope_of(params: dict) -> str:
    s = str(params.get("scope") or "").strip().lower()
    if s in ("weekend", "주말"):
        return "weekend"
    if s in ("weekday", "평일"):
        return "weekday"
    if s in ("month", "monthly", "all", "bulk", "일괄", "전체"):
        return "month"
    if s == "day":
        return "day"
    # 미지정: date 있으면 day, 없으면 month(일괄로 해석)
    return "day" if params.get("date") else "month"


@skill(
    "manage_daily_shift",
    MANAGE_DAILY_SHIFT_SCHEMA,
    categories=["settings_rules", "mutate"],  # 커버리지=병동규칙, 근데 '인원 바꿔'가 mutate 로도 분류됨
    mutation=True,
    hn_only=True,
    trigger_hint=("특정일/월 필요인원(커버리지) 설정, 'N월 N일 D/E/N 몇 명', "
                  "'데이 필요인원', '나이트 전부 N명', 일자별 근무 인원"),
    postcondition=lambda d: isinstance(d, dict) and (d.get("ok") is True or d.get("preview") is True),
)
def manage_daily_shift(db: Session, params: dict) -> Any:
    office_id, group_id = params.get("office_id"), params.get("group_id")
    counts = _counts(params)
    if not counts:
        return {"needs_clarification": True,
                "question": "각 근무 필요인원을 알려주세요 (예: 데이 8, 이브닝 7, 나이트 3).", "options": []}
    scope = _scope_of(params)
    if scope == "month":
        return _apply_month(db, office_id, group_id, params, counts)
    if scope in ("weekend", "weekday"):
        return _apply_pattern(db, office_id, group_id, params, counts, scope)
    return _apply_day(db, office_id, group_id, params, counts)


def _apply_pattern(db, office_id, group_id, params, counts, pattern) -> Any:
    # LLM 은 'weekend/weekday' 만 식별, 실제 날짜는 여기서 결정적으로 계산.
    y, m = params.get("year"), params.get("month")
    if not (y and m):
        return {"needs_clarification": True,
                "question": f"몇 월 {'주말' if pattern == 'weekend' else '평일'}인가요? (예: 8월)", "options": []}
    days = resolve_pattern_days(pattern, int(y), int(m))
    if not days:
        return {"error": f"'{pattern}' 날짜를 계산할 수 없습니다."}

    data = daily_shift_service.get_or_init_month(db, office_id, group_id, int(y), int(m))
    dd = data["date"]
    lists = {"D": list(dd["D_count"]), "E": list(dd["E_count"]),
             "N": list(dd["N_count"]), "M": list(dd["M_count"])}
    for day in days:
        idx = day - 1
        if 0 <= idx < len(lists["D"]):
            for k, v in counts.items():
                lists[k][idx] = v

    label = "주말" if pattern == "weekend" else "평일"
    if params.get("preview_only", True):
        return {"preview": True, "operation": "set_daily_shift", "scope": pattern,
                "summary": {"month": f"{y}-{int(m):02d}", "label": label,
                            "days": days, "to": counts}}
    daily_shift_service.update_daily(
        db, office_id=office_id, group_id=group_id, year=int(y), month=int(m),
        d_list=lists["D"], e_list=lists["E"], n_list=lists["N"], m_list=lists["M"],
        max_enabled=bool(data.get("max_enabled", False)),
    )
    return {"ok": True, "scope": pattern, "year": int(y), "month": int(m),
            "days": days, "applied": counts,
            "message": f"{y}년 {m}월 {label}({len(days)}일)의 필요인원을 "
                       + "·".join(f"{k} {v}" for k, v in counts.items()) + "(으)로 설정했습니다."}


def _apply_day(db, office_id, group_id, params, counts) -> Any:
    iso = _iso(params.get("date"), params)
    if not iso:
        return {"needs_clarification": True,
                "question": "어느 날짜의 필요인원을 바꿀까요? (예: 8월 7일)", "options": []}
    y, m, day = int(iso[:4]), int(iso[5:7]), int(iso[8:10])

    data = daily_shift_service.get_or_init_month(db, office_id, group_id, y, m)
    dd = data["date"]
    lists = {"D": list(dd["D_count"]), "E": list(dd["E_count"]),
             "N": list(dd["N_count"]), "M": list(dd["M_count"])}
    idx = day - 1
    if not (0 <= idx < len(lists["D"])):
        return {"error": f"{m}월 {day}일이 유효하지 않습니다."}
    old = {k: lists[k][idx] for k in ("D", "E", "N")}
    for k, v in counts.items():
        lists[k][idx] = v
    new = {k: lists[k][idx] for k in ("D", "E", "N")}

    if params.get("preview_only", True):
        return {"preview": True, "operation": "set_daily_shift", "scope": "day",
                "summary": {"date": iso, "from": old, "to": new}}
    daily_shift_service.update_daily(
        db, office_id=office_id, group_id=group_id, year=y, month=m,
        d_list=lists["D"], e_list=lists["E"], n_list=lists["N"], m_list=lists["M"],
        max_enabled=bool(data.get("max_enabled", False)),
    )
    return {"ok": True, "scope": "day", "date": iso, "applied": new,
            "message": f"{y}년 {m}월 {day}일 필요인원을 D {new['D']}·E {new['E']}·N {new['N']}(으)로 설정했습니다."}


def _apply_month(db, office_id, group_id, params, counts) -> Any:
    y, m = params.get("year"), params.get("month")
    if not (y and m):
        return {"needs_clarification": True, "question": "몇 월 일괄로 설정할까요? (예: 8월)", "options": []}
    data = daily_shift_service.get_or_init_month(db, office_id, group_id, int(y), int(m))
    cur = data["month_summary"]
    # 준 시프트만 갱신, 나머지는 현재 월 요약값 유지.
    bulk = {
        "D_count": counts.get("D", int(cur.get("D_count", 0) or 0)),
        "E_count": counts.get("E", int(cur.get("E_count", 0) or 0)),
        "N_count": counts.get("N", int(cur.get("N_count", 0) or 0)),
        "M_count": int(cur.get("M_count", 0) or 0),
        "max_enabled": bool(cur.get("max_enabled", False)),
    }
    new = {"D": bulk["D_count"], "E": bulk["E_count"], "N": bulk["N_count"]}
    if params.get("preview_only", True):
        return {"preview": True, "operation": "set_daily_shift", "scope": "month",
                "summary": {"month": f"{y}-{int(m):02d}", "to": new, "note": "전 날짜 일괄 + 기본 인원(manpower) 동기"}}
    # apply_globally=True → DailyShift 전 날짜 + ShiftManage.manpower(fallback) 동기.
    daily_shift_service.apply_bulk_to_days(
        db, office_id=office_id, group_id=group_id, year=int(y), month=int(m),
        bulk=bulk, apply_globally=True,
    )
    return {"ok": True, "scope": "month", "year": int(y), "month": int(m), "applied": new,
            "message": f"{y}년 {m}월 필요인원을 전 날짜 D {new['D']}·E {new['E']}·N {new['N']}(으)로 일괄 설정했습니다."}


# ── L1 read-back ─────────────────────────────────────────────
@readback("manage_daily_shift")
def _verify_daily_shift(db: Session, params: dict, result: Any) -> VerifyResult:
    if not (isinstance(result, dict) and result.get("ok") is True):
        return VerifyResult(True)
    from db.models import DailyShift

    office_id, group_id = params.get("office_id"), params.get("group_id")
    applied = result.get("applied") or {}
    scope = result.get("scope")

    # 대조할 (year, month, day) 목록.
    if scope == "day":
        iso = result.get("date")
        if not iso:
            return VerifyResult(True)
        y, m = int(iso[:4]), int(iso[5:7])
        targets = [int(iso[8:10])]
    elif scope in ("weekend", "weekday"):
        y, m = int(result.get("year")), int(result.get("month"))
        targets = list(result.get("days") or [])  # 계산된 날짜 전부
    else:  # month: 표본 1일(apply_bulk 은 전 날짜 동일값)
        y, m = int(result.get("year")), int(result.get("month"))
        targets = [1]
    if not targets:
        return VerifyResult(True)

    for day in targets:
        row = (
            db.query(DailyShift)
            .filter(DailyShift.office_id == office_id, DailyShift.group_id == group_id,
                    DailyShift.year == y, DailyShift.month == m, DailyShift.day == day)
            .first()
        )
        if row is None:
            continue  # 못 읽으면 통과(오탐 방지)
        for k, col in (("D", "d_count"), ("E", "e_count"), ("N", "n_count")):
            if k in applied and int(getattr(row, col, -1) or 0) != int(applied[k]):
                return VerifyResult(
                    False,
                    f"{m}월 {day}일 {k} 필요인원이 반영되지 않았습니다 (기대 {applied[k]}, 실제 {getattr(row, col, None)}).",
                )
    return VerifyResult(True)
