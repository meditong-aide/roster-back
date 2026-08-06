"""manage-leave-targets skill — 휴가 자동부여 대상(보건휴가·수면OFF·임신) 3-state 관리.

dev 에서 들어온 `nurse_leave_period` 3-state 는 REST(/nurse-period/leave-flags)로만
열려 있었고 에이전트에는 통로가 없었다. "김민지 보건휴가 대상에서 빼줘" 같은 발화가
update_person_attr 로 조용히 오폴백해 거짓완료가 되는 것을 막기 위해 전용 스킬로 연다
(병동이동 때와 같은 교훈 — 미구현 기능은 인접 스킬로 새어 나간다).

3-state 의미(services.leave.leave_eligibility):
    None = 자동판정에 맡김 / True = 강제포함 / False = 제외

★ 사용자에게는 raw 3-state 만 보여주면 쓸모가 없다("자동" 이 결국 대상인지 아닌지를
  모른다). 그래서 자동판정 규칙을 **같은 소스로 재현**해 실효 결과까지 함께 낸다.
★ 자동판정 입력(allowed_shifts·fixed_shift)은 as-of-TODAY 컬럼이라 미래월에 stale 하다
  (allowed-shift as-of 사고와 같은 함정). 대상월 period 로 오버레이한 뒤 판정한다.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from agents_v2.skills.manifest import skill
from agents_v2.verify import VerifyResult, readback
from services.leave.leave_eligibility import (
    LEAVE_VALUE_COLS,
    fetch_leave_flags,
    is_health_leave_eligible,
    is_sleep_off_eligible,
    upsert_leave_period,
)

# 사용자 어휘 → 3-state. "자동" 은 명시적 None(= 자동판정으로 되돌림)이다.
_STATE_WORDS: dict[str, bool | None] = {"포함": True, "제외": False, "자동": None}

# 스킬 파라미터명 → DB 컬럼명.
_FIELD_COLS = {
    "health_leave": "health_leave_eligible",
    "sleep_off": "sleep_off_eligible",
    "pregnant": "pregnant",
}
_COL_LABEL = {
    "health_leave_eligible": "보건휴가",
    "sleep_off_eligible": "수면오프",
    "pregnant": "임신",
}


MANAGE_LEAVE_TARGETS_SCHEMA: dict = {
    "name": "manage_leave_targets",
    "description": (
        "휴가 **자동부여 대상자**를 조회·설정합니다 — 보건휴가(생리휴가), 수면OFF(야간 근무 후 수면 오프), "
        "임신 여부. (HN/ADM 전용)\n\n"
        "근무표를 생성하면 보건휴가·수면OFF 가 자동으로 부여되는데, 누구에게 줄지는 "
        "**자동판정 + 개인별 예외**로 정해진다. 이 스킬은 그 개인별 예외를 관리한다.\n"
        "⚠️ 이 세 가지는 간호사 속성(update_person_attr)이 아니다. '보건휴가 대상/수면오프 대상/임산부' 는 "
        "반드시 이 스킬로 처리하라.\n\n"

        "─────────── 3-state ───────────\n"
        "각 항목은 세 상태를 가진다.\n"
        "- `자동` — 규칙대로 판정(기본). 보건휴가 자동판정 = 여성 · N전담 아님 · 고정근무 아님. 수면OFF 는 전원 대상.\n"
        "- `포함` — 규칙상 부적격이어도 **준다**. (예: 고정근무자인데 보건휴가를 주는 경우 — 실무에서 흔하다)\n"
        "- `제외` — 규칙상 적격이어도 **안 준다**.\n"
        "임신은 `포함`(임신 중) / `제외`(아님) / `자동`(미설정). 임신 중이면 보건휴가는 자동으로 빠진다.\n\n"

        "─────────── operation ───────────\n"
        "- `list` — 병동 전원의 대상 여부 조회. 간호사 이름을 주면 그 사람만. "
        "각 항목의 설정값과 **실제 적용 결과**를 함께 보여준다.\n"
        "- `set` — 한 간호사의 항목을 바꾼다. health_leave / sleep_off / pregnant 중 1개 이상 지정.\n\n"

        "─────────── 파라미터 ───────────\n"
        "- `health_leave` — 보건휴가 대상: '포함' / '제외' / '자동'\n"
        "- `sleep_off` — 수면OFF 대상: '포함' / '제외' / '자동'\n"
        "- `pregnant` — 임신 여부: '포함'(임신 중) / '제외'(아님) / '자동'(미설정)\n"
        "- `filter` — list 전용 좁히기. '임산부' / '보건휴가대상' / '보건휴가제외' / '수동설정'(예외가 걸린 사람만).\n"
        "- year/month — 대상월. 설정은 그 달 1일부터 발효된다.\n\n"

        "예) '김민지 보건휴가 대상에서 빼줘' → operation=set, health_leave=제외\n"
        "예) '이영희 임산부로 등록' → operation=set, pregnant=포함\n"
        "예) '박수진 고정근무인데 보건휴가는 주자' → operation=set, health_leave=포함\n"
        "예) '이번 달 보건휴가 받는 사람 누구야' → operation=list, filter=보건휴가대상\n"
        "예) '수면오프 대상 설정 원래대로' → operation=set, sleep_off=자동"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["list", "set"],
                "description": "list=조회, set=설정 변경. 기본 list",
            },
            "nurse_name": {"type": "string", "description": "간호사 이름 (한글). 내부에서 id 로 해석"},
            "health_leave": {
                "type": "string",
                "enum": ["포함", "제외", "자동"],
                "description": "보건휴가(생리휴가) 대상 여부. 자동=규칙 판정으로 되돌림",
            },
            "sleep_off": {
                "type": "string",
                "enum": ["포함", "제외", "자동"],
                "description": "수면OFF(야간 후 수면 오프) 대상 여부. 자동=규칙 판정으로 되돌림",
            },
            "pregnant": {
                "type": "string",
                "enum": ["포함", "제외", "자동"],
                "description": "임신 여부. 포함=임신 중, 제외=아님, 자동=미설정",
            },
            "filter": {
                "type": "string",
                "enum": ["전체", "임산부", "보건휴가대상", "보건휴가제외", "수동설정"],
                "description": "list 결과 좁히기. 기본 전체",
            },
            "year": {"type": "integer", "description": "대상 연도"},
            "month": {"type": "integer", "description": "대상 월 (1-12)"},
            "preview_only": {"type": "boolean", "default": True},
        },
        "required": ["operation"],
    },
}


# ── 자동판정 재현 ────────────────────────────────────────────


def _asof_profile(db: Session, nurses: list, year: int, month: int) -> dict[str, dict]:
    """대상월 1일 기준 allowed_shifts / fixed_shift 를 period 로 해석.

    두 값 모두 `nurse_allowed_shift_period` 한 테이블에 있어 1쿼리로 끝난다. 구간이
    없으면 컬럼 캐시를 그대로 쓴다(비회귀 — 오버레이는 '있으면 이긴다' 규칙).
    """
    from datetime import timedelta

    from db.models import NurseAllowedShiftPeriod
    from services.nurse_period_resolver import fetch_periods, resolve_asof

    ids = [str(n.nurse_id) for n in nurses]
    out = {
        nid: {
            "allowed_shifts": getattr(n, "allowed_shifts", None),
            "fixed_shift": getattr(n, "fixed_shift", None),
        }
        for nid, n in zip(ids, nurses)
    }
    if not ids:
        return out
    start = date(year, month, 1)
    try:
        rows = fetch_periods(db, NurseAllowedShiftPeriod, ids, start, start + timedelta(days=1))
    except Exception:  # noqa: BLE001 — period 테이블 부재 등은 캐시 폴백
        return out
    _SENT = object()
    for nid in ids:
        r = (rows or {}).get(nid)
        al = resolve_asof(r, start, "allowed_shifts", default=_SENT)
        if al is not _SENT:
            out[nid]["allowed_shifts"] = al
        fx = resolve_asof(r, start, "fixed_shift", default=_SENT)
        if fx is not _SENT:
            out[nid]["fixed_shift"] = fx
    return out


def _health_auto(nurse: Any, prof: dict) -> bool:
    """보건휴가 자동판정 — health_leave_planner.eligible_nurses 와 같은 규칙."""
    from services.cp_sat.allowed_shift_types import is_n_only_profile

    return (
        str(getattr(nurse, "gender", "") or "").strip() == "여"
        and not is_n_only_profile(prof.get("allowed_shifts"))
        and not str(prof.get("fixed_shift") or "").strip()
    )


def _state_word(value: bool | None) -> str:
    return "자동" if value is None else ("포함" if value else "제외")


def _describe(explicit: bool | None, effective: bool) -> str:
    """설정값 + 실효 결과를 한 문자열로. '자동' 일 때 결국 대상인지를 드러낸다."""
    if explicit is None:
        return f"자동 → {'대상' if effective else '비대상'}"
    return f"{'포함' if explicit else '제외'}(수동) → {'대상' if effective else '비대상'}"


def _row_view(nurse: Any, flags: dict | None, prof: dict) -> dict:
    f = flags or {}
    hl_auto = _health_auto(nurse, prof)
    hl_eff = is_health_leave_eligible(f or None, hl_auto)
    so_eff = is_sleep_off_eligible(f or None)
    preg = f.get("pregnant")
    return {
        "nurse": getattr(nurse, "name", None),
        "보건휴가": _describe(f.get("health_leave_eligible"), hl_eff),
        "수면오프": _describe(f.get("sleep_off_eligible"), so_eff),
        "임신": {True: "임신 중", False: "아님"}.get(preg, "미설정"),
        # 필터/검증용 raw (사용자 노출은 위 3개로 충분)
        "_state": {
            "health_leave": _state_word(f.get("health_leave_eligible")),
            "sleep_off": _state_word(f.get("sleep_off_eligible")),
            "pregnant": _state_word(preg),
            "health_leave_effective": hl_eff,
            "sleep_off_effective": so_eff,
        },
    }


_FILTERS = {
    "임산부": lambda v: v["_state"]["pregnant"] == "포함",
    "보건휴가대상": lambda v: v["_state"]["health_leave_effective"],
    "보건휴가제외": lambda v: not v["_state"]["health_leave_effective"],
    "수동설정": lambda v: any(
        v["_state"][k] != "자동" for k in ("health_leave", "sleep_off", "pregnant")
    ),
}


# ── operations ───────────────────────────────────────────────


def _target_month(params: dict) -> tuple[int, int]:
    today = date.today()
    return int(params.get("year") or today.year), int(params.get("month") or today.month)


def _nurses(db: Session, params: dict) -> list:
    from db.models import Nurse

    nurse_ids = params.get("nurse_ids") or []
    q = db.query(Nurse).filter(Nurse.group_id == params.get("group_id"))
    if nurse_ids:
        return q.filter(Nurse.nurse_id.in_(nurse_ids)).all()
    return q.filter(Nurse.active == True).all()  # noqa: E712


def _list(db: Session, params: dict) -> Any:
    year, month = _target_month(params)
    nurses = _nurses(db, params)
    if not nurses:
        return {"error": "대상 간호사를 찾을 수 없습니다."}

    ids = [str(n.nurse_id) for n in nurses]
    flags = fetch_leave_flags(db, ids, year, month)
    prof = _asof_profile(db, nurses, year, month)
    rows = [_row_view(n, flags.get(str(n.nurse_id)), prof.get(str(n.nurse_id), {})) for n in nurses]

    key = params.get("filter") or "전체"
    if key in _FILTERS:
        rows = [r for r in rows if _FILTERS[key](r)]

    return {
        "operation": "list",
        "year": year,
        "month": month,
        "filter": key,
        "count": len(rows),
        "rows": rows,
        "note": (
            "'자동' 은 규칙 판정에 맡긴다는 뜻이고, → 뒤가 그 달에 실제 적용되는 결과입니다."
        ),
    }


def _requested_values(params: dict) -> dict[str, bool | None] | None:
    """스킬 파라미터에서 3-state 값 추출. 미지정 항목은 아예 담지 않는다.

    ★ '미전송' 과 '자동(None)' 을 반드시 갈라야 한다 — 안 가르면 보건휴가만 바꾸려던
      요청이 수면오프·임신까지 자동으로 되돌린다(REST 쪽 exclude_unset 과 같은 이유).
    """
    out: dict[str, bool | None] = {}
    for pname, col in _FIELD_COLS.items():
        raw = params.get(pname)
        if raw is None:
            continue
        word = str(raw).strip()
        if word not in _STATE_WORDS:
            return None
        out[col] = _STATE_WORDS[word]
    return out


def _set(db: Session, params: dict) -> Any:
    nurse_ids = params.get("nurse_ids") or []
    if not nurse_ids:
        return {
            "needs_clarification": True,
            "question": "어느 간호사의 휴가 대상 설정을 바꾸나요? (예: '김민지')",
            "options": [],
        }

    values = _requested_values(params)
    if values is None:
        return {"error": "값은 '포함' / '제외' / '자동' 중 하나여야 합니다."}
    if not values:
        return {
            "error": "바꿀 항목이 없습니다.",
            "hint": "health_leave / sleep_off / pregnant 중 하나 이상을 '포함'·'제외'·'자동' 으로 지정하세요.",
        }

    from db.models import Nurse

    nurse_id = str(nurse_ids[0])
    nurse = db.query(Nurse).filter(Nurse.nurse_id == nurse_id).first()
    if nurse is None:
        return {"error": "간호사를 찾을 수 없습니다."}

    year, month = _target_month(params)
    valid_from = date(year, month, 1)  # 판정 단위가 '월' 이므로 그 달 1일부터 발효
    cur = fetch_leave_flags(db, [nurse_id], year, month).get(nurse_id) or {}
    changes = {
        _COL_LABEL[col]: {"old": _state_word(cur.get(col)), "new": _state_word(val)}
        for col, val in values.items()
    }

    if params.get("preview_only", True):
        return {
            "preview": True,
            "operation": "set",
            "summary": {
                "nurse": nurse.name,
                "effective_from": f"{year}년 {month}월",
                "changes": changes,
            },
        }

    try:
        upsert_leave_period(db, nurse_id, valid_from, source="edited", **values)
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        from services.leave.leave_eligibility import _is_missing_table

        if _is_missing_table(exc):
            # 조회는 테이블 부재를 '전원 자동판정'으로 삼키지만(비회귀), 저장은 삼키면
            # 안 된다 — 저장했다고 답하고 아무 일도 안 일어난다.
            return {"error": "휴가 대상 예외 테이블(nurse_leave_period)이 아직 없어 저장할 수 없습니다. "
                             "DB 적용 후 다시 시도해 주세요."}
        return {"error": f"휴가 대상 저장 실패: {exc}"}

    after = fetch_leave_flags(db, [nurse_id], year, month).get(nurse_id) or {}
    prof = _asof_profile(db, [nurse], year, month)
    applied = ", ".join(
        f"{_COL_LABEL[col]} {_state_word(val)}" for col, val in values.items()
    )
    return {
        "ok": True,
        "message": f"{nurse.name} 간호사의 {applied} 설정을 {year}년 {month}월부터 적용했습니다.",
        "row": _row_view(nurse, after, prof.get(nurse_id, {})),
    }


@skill(
    "manage_leave_targets",
    MANAGE_LEAVE_TARGETS_SCHEMA,
    # settings_people(주) + mutate('빼줘/제외해줘' 가 mutate 로 분류돼도 스코프에 남도록).
    categories=["settings_people", "mutate"],
    mutation=True,
    hn_only=True,
    grounds=["nurse_name"],
    trigger_hint=(
        "보건휴가/생리휴가 대상자 포함·제외, 수면오프(야간 후 수면 OFF) 대상 설정, "
        "임신·임산부 등록, 휴가 자동부여 대상 조회"
    ),
    postcondition=lambda d: isinstance(d, dict) and (d.get("ok") is True or "rows" in d),
)
def manage_leave_targets(db: Session, params: dict) -> Any:
    op = (params.get("operation") or "list").lower()
    if op == "list":
        return _list(db, params)
    if op == "set":
        return _set(db, params)
    return {"error": f"지원하지 않는 작업입니다: {op}"}


# ── L1 read-back 검증 ────────────────────────────────────────
# 저장 성공(ok=True)을 보고했으면 DB 를 되읽어 의도한 3-state 가 실재하는지 대조한다.
@readback("manage_leave_targets")
def _verify_manage_leave_targets(db: Session, params: dict, result: Any) -> VerifyResult:
    if not (isinstance(result, dict) and result.get("ok") is True):
        return VerifyResult(True)
    nurse_ids = params.get("nurse_ids") or []
    values = _requested_values(params)
    if not nurse_ids or not values:
        return VerifyResult(True)
    nurse_id = str(nurse_ids[0])
    year, month = _target_month(params)
    cur = fetch_leave_flags(db, [nurse_id], year, month).get(nurse_id) or {}
    for col, want in values.items():
        if cur.get(col) is not want:
            return VerifyResult(
                False,
                f"{_COL_LABEL[col]} 설정이 반영되지 않았습니다 "
                f"(의도 {_state_word(want)}, 실제 {_state_word(cur.get(col))}).",
            )
    return VerifyResult(True)


assert set(_FIELD_COLS.values()) == set(LEAVE_VALUE_COLS), (
    "leave_eligibility.LEAVE_VALUE_COLS 와 스킬 필드 매핑이 어긋났습니다."
)
