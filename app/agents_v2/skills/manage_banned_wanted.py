"""manage-banned-wanted skill — 금지 원티드(그 날 이 근무는 주지 마) 조회·추가·해제.

`BannedWantedEntry` 는 조정판 UI 로만 열려 있었고 에이전트 통로가 없었다. "김민지
8월 15일은 나이트 빼줘" 같은 발화가 bulk_mutation 이나 update_person_attr 로 새면
근무표를 직접 고치거나(확정 전이라 무의미) 아무 일도 안 일어난다.

★ 저장 서비스(save_banned_wanted_service)는 **스냅샷 replace** 다 — 넘긴 목록이 그 달
  HN 금지의 전부가 되고 나머지는 지워진다. 그래서 이 스킬은 add/remove 를 델타로 받되
  **기존 HN 스냅샷을 읽어 합친 전체 목록**을 넘긴다. 부분 목록을 그대로 넘기면 다른
  금지가 조용히 사라진다(이 API 의 가장 큰 함정).
★ 간호사 본인이 원티드에 낸 기피근무(source='nurse')는 건드리지 않는다. 소유권이
  간호사에게 있어 서비스가 승격을 차단하며, 에이전트도 적용 여부만 말한다.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from agents_v2.grounding.internal import resolve_date, resolve_shift
from agents_v2.skills.manifest import skill
from agents_v2.verify import VerifyResult, readback


MANAGE_BANNED_WANTED_SCHEMA: dict = {
    "name": "manage_banned_wanted",
    "description": (
        "**금지 원티드**를 조회·추가·해제합니다 — 특정 간호사에게 **그 날 그 근무를 주지 말라**는 "
        "수간호사 지정입니다. (HN/ADM 전용)\n\n"
        "확정 원티드(그 날 이 근무를 **줘라**)의 반대다. 근무표를 직접 고치는 게 아니라 "
        "**생성 전에 거는 제약**이므로, 아직 안 만든 달에도 걸 수 있다.\n"
        "⚠️ '김민지 15일 나이트 빼줘/주지 마/금지' 같은 요청은 이 스킬이다. "
        "이미 만들어진 근무표의 셀을 바꾸는 것(bulk_mutation)과 혼동하지 마라.\n"
        "⚠️ 추가/해제는 preview_only=true 로 먼저 호출해 미리보기를 만들고, 사용자 확인 후 실행된다.\n\n"

        "─────────── operation ───────────\n"
        "- `list` — 그 달 금지 현황. 간호사 이름을 주면 그 사람만.\n"
        "- `add` — 금지 추가. nurse_name + dates + banned_shifts 필요.\n"
        "- `remove` — 그 간호사·날짜의 금지 해제.\n"
        "- `clear` — 그 달 수간호사 지정 금지를 전부 해제. (간호사 본인 기피근무는 남는다)\n\n"

        "─────────── 파라미터 ───────────\n"
        "- `nurse_name` — 간호사 이름(그대로). 내부에서 id 로 해석.\n"
        "- `dates` — 금지할 날짜들. YYYY-MM-DD 배열. '15일'=['2026-08-15'], '15,16일'=2개.\n"
        "- `banned_shifts` — 금지할 근무. 이름 그대로 배열('나이트', 'D', '이브닝'). 내부에서 코드로 해석.\n"
        "- `reason` — 사유 메모(선택).\n\n"

        "예) '김민지 8월 15일 나이트 주지 마' → operation=add, nurse_name=김민지, "
        "dates=['2026-08-15'], banned_shifts=['나이트']\n"
        "예) '박지은 20,21일 데이 이브닝 금지' → operation=add, dates=['2026-08-20','2026-08-21'], "
        "banned_shifts=['데이','이브닝']\n"
        "예) '김민지 15일 금지 풀어줘' → operation=remove, nurse_name=김민지, dates=['2026-08-15']\n"
        "예) '이번 달 금지 걸린 사람 보여줘' → operation=list"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["list", "add", "remove", "clear"],
                "description": "list=조회, add=금지 추가, remove=해제, clear=그 달 전체 해제. 기본 list",
            },
            "nurse_name": {"type": "string", "description": "간호사 이름 (한글). 내부에서 id 로 해석"},
            "dates": {
                "type": "array",
                "items": {"type": "string"},
                "description": "대상 날짜 YYYY-MM-DD 배열 (예: ['2026-08-15','2026-08-16'])",
            },
            "banned_shifts": {
                "type": "array",
                "items": {"type": "string"},
                "description": "금지할 근무 이름/코드 배열 (예: ['나이트'], ['D','E']). add 에 필수",
            },
            "reason": {"type": "string", "description": "사유 메모 (선택)"},
            "year": {"type": "integer", "description": "대상 연도"},
            "month": {"type": "integer", "description": "대상 월 (1-12)"},
            "preview_only": {"type": "boolean", "default": True},
        },
        "required": ["operation"],
    },
}


# ── grounding ────────────────────────────────────────────────


def _ward_mains(db: Session, group_id: str) -> set[str]:
    from services.wanted_service import _ward_main_codes

    return _ward_main_codes(db, group_id)


def _main_code_index(db: Session, group_id: str) -> dict[str, str]:
    """'나이트'/'N'/'N_GRP001' 등 어떤 표기로 와도 main code 로 가는 색인."""
    from db.models import Shift

    idx: dict[str, str] = {}
    for s in db.query(Shift).filter(Shift.group_id == group_id).all():
        main = str(s.default_shift or "").strip().upper()
        if not main:
            continue
        for token in (s.shift_id, s.name, s.shift_gb):
            if token:
                idx.setdefault(str(token).strip().upper(), main)
    return idx


def _resolve_main_codes(db: Session, group_id: str, tokens: list[str]) -> tuple[list[str], str | None]:
    """근무 이름/코드 목록 → 병동 main code 목록. 실패 시 (부분결과, 에러메시지)."""
    mains = _ward_mains(db, group_id)
    idx = _main_code_index(db, group_id)
    out: list[str] = []
    unknown: list[str] = []
    for raw in tokens:
        t = str(raw).strip().upper()
        code = t if t in mains else idx.get(t)
        if code is None:
            # 별칭·부분일치는 공용 grounder 에 맡기고, 그 결과(shift_id)를 다시 main 으로.
            r = resolve_shift(db, group_id, str(raw))
            if r.resolved and not isinstance(r.value, list):
                code = idx.get(str(r.value).strip().upper())
        if code is None or code not in mains:
            unknown.append(str(raw))
        elif code not in out:
            out.append(code)
    if unknown:
        return out, (
            f"어떤 근무인지 모르겠습니다: {', '.join(unknown)}. "
            f"이 병동 근무형은 {', '.join(sorted(mains))} 입니다."
        )
    return out, None


def _resolve_dates(params: dict) -> tuple[list[date], str | None]:
    raw = params.get("dates")
    if not raw and params.get("date"):
        raw = [params["date"]]
    if not raw:
        return [], None
    year = params.get("year") or date.today().year
    month = params.get("month") or 1
    out: list[date] = []
    bad: list[str] = []
    for item in raw:
        s = str(item).strip()
        iso = s if (len(s) >= 10 and s[4] == "-" and s[7] == "-") else resolve_date(s, year, month)
        try:
            out.append(date.fromisoformat(str(iso)[:10]))
        except (TypeError, ValueError):
            bad.append(s)
    if bad:
        return out, f"날짜를 이해하지 못했습니다: {', '.join(bad)}"
    return out, None


# ── snapshot ─────────────────────────────────────────────────


def _rows(db: Session, group_id: str, year: int, month: int, *, hn_only: bool = False):
    from db.models import BannedWantedEntry
    from services.wanted_service import BANNED_SOURCE_HN, banned_source_filter

    q = db.query(BannedWantedEntry).filter(
        BannedWantedEntry.group_id == group_id,
        BannedWantedEntry.year == int(year),
        BannedWantedEntry.month == int(month),
    )
    if hn_only:
        q = q.filter(banned_source_filter(BANNED_SOURCE_HN))
    return q.order_by(BannedWantedEntry.shift_date.asc()).all()


def _snapshot(db: Session, group_id: str, year: int, month: int) -> dict[tuple[str, date], dict]:
    """현재 HN 금지 스냅샷 — (nurse_id, 날짜) → {codes, is_applied, reason}.

    ★ 스냅샷 replace API 라 델타 연산 전에 반드시 이걸 읽어 합쳐야 한다.
    """
    return {
        (str(r.nurse_id), r.shift_date): {
            "codes": [str(c).strip().upper() for c in (r.banned_shift_ids or [])],
            "is_applied": bool(r.is_applied),
            "reason": r.reason,
        }
        for r in _rows(db, group_id, year, month, hn_only=True)
    }


def _save(db: Session, params: dict, snapshot: dict, *, validate_only: bool = False):
    """스냅샷 전체를 저장 서비스에 넘긴다. HTTPException 은 호출부가 문자열로 변환."""
    from schemas.roster_schema import BannedWantedEntryCreate, FixedWantedCreate
    from services.wanted_service import save_banned_wanted_service

    entries = [
        BannedWantedEntryCreate(
            nurse_id=nid,
            shift_date=day,
            banned_shift_ids=v["codes"],
            is_applied=v["is_applied"],
            reason=v.get("reason"),
        )
        for (nid, day), v in sorted(snapshot.items(), key=lambda kv: (kv[0][1], kv[0][0]))
    ]
    req = FixedWantedCreate(
        year=int(params["year"]), month=int(params["month"]),
        entries=[], banned_entries=entries,
    )
    return save_banned_wanted_service(
        db, params["group_id"], str(params.get("acting_user_id") or ""), req,
        validate_only=validate_only,
    )


def _detail(exc: Exception) -> str:
    d = getattr(exc, "detail", None)
    if isinstance(d, dict):
        errs = d.get("errors") or []
        msgs = [str(e.get("message")) for e in errs if isinstance(e, dict) and e.get("message")]
        if msgs:
            return " / ".join(msgs)
    return str(d) if d else str(exc)


# ── operations ───────────────────────────────────────────────


def _nurse_names(db: Session, group_id: str) -> dict[str, str]:
    from db.models import Nurse

    return {
        str(n.nurse_id): n.name
        for n in db.query(Nurse).filter(Nurse.group_id == group_id).all()
    }


_SOURCE_LABEL = {"nurse": "간호사 기피근무", "hn": "수간호사 지정"}


def _list(db: Session, params: dict) -> Any:
    group_id, year, month = params["group_id"], params["year"], params["month"]
    names = _nurse_names(db, group_id)
    wanted_ids = {str(n) for n in (params.get("nurse_ids") or [])}

    out = []
    for r in _rows(db, group_id, year, month):
        nid = str(r.nurse_id)
        if wanted_ids and nid not in wanted_ids:
            continue
        out.append({
            "nurse": names.get(nid, nid),
            "date": r.shift_date.isoformat() if r.shift_date else None,
            "banned": [str(c).strip().upper() for c in (r.banned_shift_ids or [])],
            "적용": bool(r.is_applied),
            "출처": _SOURCE_LABEL.get(str(r.source or "hn"), "수간호사 지정"),
            "사유": r.reason,
        })
    return {
        "operation": "list",
        "year": year,
        "month": month,
        "count": len(out),
        "entries": out,
        "message": (
            f"{year}년 {month}월 금지 원티드 {len(out)}건입니다."
            if out else f"{year}년 {month}월에 걸린 금지 원티드가 없어요."
        ),
    }


def _add(db: Session, params: dict) -> Any:
    group_id, year, month = params["group_id"], params["year"], params["month"]
    nurse_ids = params.get("nurse_ids") or []
    if not nurse_ids:
        return {"needs_clarification": True,
                "question": "어느 간호사에게 금지를 거나요? (예: '김민지')", "options": []}
    nurse_id = str(nurse_ids[0])

    days, derr = _resolve_dates(params)
    if derr:
        return {"error": derr}
    if not days:
        return {"needs_clarification": True,
                "question": "며칠에 금지를 거나요? (예: 8월 15일)", "options": []}

    tokens = params.get("banned_shifts") or []
    if not tokens:
        return {"needs_clarification": True,
                "question": "어떤 근무를 금지하나요? (예: '나이트')", "options": []}
    codes, cerr = _resolve_main_codes(db, group_id, tokens)
    if cerr:
        return {"error": cerr}

    off_month = [d.isoformat() for d in days if (d.year, d.month) != (int(year), int(month))]
    if off_month:
        return {"error": f"{year}년 {month}월이 아닌 날짜가 섞였습니다: {', '.join(off_month)}"}

    snapshot = _snapshot(db, group_id, year, month)
    names = _nurse_names(db, group_id)
    added, merged = [], []
    for d in days:
        cur = snapshot.get((nurse_id, d))
        if cur is None:
            snapshot[(nurse_id, d)] = {"codes": list(codes), "is_applied": True,
                                       "reason": params.get("reason")}
            added.append(d.isoformat())
        else:
            before = set(cur["codes"])
            cur["codes"] = sorted(before | set(codes))
            (merged if before != set(cur["codes"]) else added).append(d.isoformat())

    summary = {
        "nurse": names.get(nurse_id, nurse_id),
        "dates": [d.isoformat() for d in days],
        "banned": codes,
        "기존_금지에_추가됨": merged,
    }
    if params.get("preview_only", True):
        try:  # 미리보기 단계에서 실현가능성/관할 검증까지 돌려 본다(쓰기 전 차단).
            _save(db, params, snapshot, validate_only=True)
        except Exception as e:  # noqa: BLE001
            return {"error": _detail(e)}
        return {"preview": True, "operation": "add", "summary": summary}

    try:
        _rows_saved, warnings = _save(db, params, snapshot)
    except Exception as e:  # noqa: BLE001
        return {"error": _detail(e)}
    return {
        "ok": True,
        "operation": "add",
        "summary": summary,
        "warnings": [w.get("message") for w in warnings if w.get("message")],
        "message": (
            f"{summary['nurse']} 간호사 {', '.join(summary['dates'])} 에 "
            f"{', '.join(codes)} 금지를 걸었습니다."
        ),
    }


def _remove(db: Session, params: dict) -> Any:
    group_id, year, month = params["group_id"], params["year"], params["month"]
    nurse_ids = params.get("nurse_ids") or []
    if not nurse_ids:
        return {"needs_clarification": True,
                "question": "어느 간호사의 금지를 푸나요? (예: '김민지')", "options": []}
    nurse_id = str(nurse_ids[0])

    days, derr = _resolve_dates(params)
    if derr:
        return {"error": derr}

    snapshot = _snapshot(db, group_id, year, month)
    targets = [k for k in snapshot if k[0] == nurse_id and (not days or k[1] in days)]
    names = _nurse_names(db, group_id)
    if not targets:
        # 간호사 소유 기피근무만 있는 경우를 구분해 알려준다(해제 대상이 아니다).
        own = [r for r in _rows(db, group_id, year, month)
               if str(r.nurse_id) == nurse_id and str(r.source or "hn") == "nurse"]
        if own:
            return {"error": (f"{names.get(nurse_id, nurse_id)} 간호사의 금지는 본인이 원티드에 낸 "
                              "기피근무라 여기서 해제할 수 없습니다.")}
        return {"error": f"{names.get(nurse_id, nurse_id)} 간호사에게 해제할 금지가 없습니다."}

    summary = {"nurse": names.get(nurse_id, nurse_id),
               "dates": sorted(d.isoformat() for _, d in targets)}
    if params.get("preview_only", True):
        return {"preview": True, "operation": "remove", "summary": summary}

    for k in targets:
        snapshot.pop(k, None)
    try:
        _save(db, params, snapshot)
    except Exception as e:  # noqa: BLE001
        return {"error": _detail(e)}
    return {
        "ok": True,
        "operation": "remove",
        "summary": summary,
        "message": f"{summary['nurse']} 간호사 {', '.join(summary['dates'])} 금지를 해제했습니다.",
    }


def _clear(db: Session, params: dict) -> Any:
    group_id, year, month = params["group_id"], params["year"], params["month"]
    snapshot = _snapshot(db, group_id, year, month)
    if not snapshot:
        return {"error": f"{year}년 {month}월에 해제할 수간호사 지정 금지가 없습니다."}
    summary = {"count": len(snapshot), "year": year, "month": month}
    if params.get("preview_only", True):
        return {"preview": True, "operation": "clear", "summary": summary}
    try:
        _save(db, params, {})
    except Exception as e:  # noqa: BLE001
        return {"error": _detail(e)}
    return {
        "ok": True,
        "operation": "clear",
        "summary": summary,
        "message": (f"{year}년 {month}월 수간호사 지정 금지 {summary['count']}건을 전부 해제했습니다. "
                    "간호사 본인이 낸 기피근무는 그대로 있습니다."),
    }


@skill(
    "manage_banned_wanted",
    MANAGE_BANNED_WANTED_SCHEMA,
    # mutate(주) + read('금지 걸린 사람' 조회가 read 로 분류돼도 스코프에 남도록).
    categories=["mutate", "read"],
    mutation=True,
    hn_only=True,
    grounds=["nurse_name", "dates", "banned_shifts"],
    trigger_hint=(
        "금지 원티드 — 특정 간호사에게 그 날 특정 근무를 주지 말라는 지정, "
        "'나이트 빼줘/주지 마/금지', 기피근무 현황 조회·해제"
    ),
    postcondition=lambda d: isinstance(d, dict) and (d.get("ok") is True or "entries" in d),
)
def manage_banned_wanted(db: Session, params: dict) -> Any:
    op = (params.get("operation") or "list").lower()
    if params.get("year") is None or params.get("month") is None:
        return {"error": "year/month required"}
    if op == "list":
        return _list(db, params)
    if op == "add":
        return _add(db, params)
    if op == "remove":
        return _remove(db, params)
    if op == "clear":
        return _clear(db, params)
    return {"error": f"지원하지 않는 작업입니다: {op}"}


# ── L1 read-back 검증 ────────────────────────────────────────
@readback("manage_banned_wanted")
def _verify_manage_banned_wanted(db: Session, params: dict, result: Any) -> VerifyResult:
    if not (isinstance(result, dict) and result.get("ok") is True):
        return VerifyResult(True)
    group_id, year, month = params["group_id"], params["year"], params["month"]
    snapshot = _snapshot(db, group_id, year, month)
    op = result.get("operation")

    if op == "clear":
        return VerifyResult(not snapshot, "금지 해제가 반영되지 않았습니다 (행이 남아 있음).")

    nurse_ids = params.get("nurse_ids") or []
    if not nurse_ids:
        return VerifyResult(True)
    nurse_id = str(nurse_ids[0])
    days = [date.fromisoformat(d) for d in (result.get("summary", {}).get("dates") or [])]

    if op == "remove":
        left = [d.isoformat() for d in days if (nurse_id, d) in snapshot]
        return VerifyResult(not left, f"금지가 남아 있습니다: {', '.join(left)}" if left else None)

    codes = set(result.get("summary", {}).get("banned") or [])
    for d in days:
        cur = snapshot.get((nurse_id, d))
        if cur is None or not codes <= set(cur["codes"]):
            # fixed 셀이라 서비스가 의도적으로 드롭한 경우는 warning 으로 이미 통지됐다.
            if any("확정 근무" in str(w) for w in (result.get("warnings") or [])):
                continue
            return VerifyResult(
                False, f"{d.isoformat()} 금지가 DB 에 반영되지 않았습니다."
            )
    return VerifyResult(True)
