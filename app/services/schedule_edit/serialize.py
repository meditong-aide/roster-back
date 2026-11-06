"""결과를 스킬·화면이 쓰는 JSON 으로. 날짜는 사람이 보는 1일부터, 칸 위치는 nurse_id + 날짜(ISO).

레버 허용량의 열쇠(규칙 위치 튜플)는 JSON 키가 될 수 없어 [열쇠 목록, 양] 쌍 목록으로 바꾼다.
레버 id 는 허용량 내용의 해시 — 같은 레버면 같은 id(화면이 고른 레버를 그대로 돌려보낸다).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from services.schedule_edit.context import EditContext
from services.schedule_edit.requests import (
    AvoidCell, BalanceTarget, CountTarget, CoverTarget, EditRequest, GiveUpWish, GradeFloor, LeaveCell, SetCell,
    ShapeTarget, StaffingPlan, StaffRule, WishTarget,
)


def allow_to_json(allow: Dict[Tuple, int]) -> List[List[Any]]:
    return [[list(key), int(amount)] for key, amount in sorted(allow.items(), key=lambda kv: str(kv[0]))]


def allow_from_json(items: List[List[Any]]) -> Dict[Tuple, int]:
    return {tuple(key): int(amount) for key, amount in items or []}


def requests_to_json(requests: Sequence[EditRequest]) -> List[Dict[str, Any]]:
    """기록·재기동 복원용 — day 는 0부터(모델과 같게)."""
    out = []
    for r in requests:
        if isinstance(r, CountTarget):
            out.append({"kind": "count", "nurse_id": r.nurse_id, "codes": sorted(r.codes), "target": r.target,
                        "first_day": r.first_day, "last_day": r.last_day, "transfer": r.transfer, **_asked(r),
                        **({"total": True} if r.total else {})})
        elif isinstance(r, SetCell):
            out.append({"kind": "set", "nurse_id": r.nurse_id, "day": r.day, "code": r.code})
        elif isinstance(r, LeaveCell):
            out.append({"kind": "leave", "nurse_id": r.nurse_id, "day": r.day, "code": r.code})
        elif isinstance(r, CoverTarget):
            out.append({"kind": "cover", "day": r.day, "code": r.code, "target": r.target, "grade": r.grade})
        elif isinstance(r, BalanceTarget):
            out.append({"kind": "balance", "codes": sorted(r.codes), "members": list(r.members),
                        "first_day": r.first_day, "last_day": r.last_day, "spread": r.spread, "target": r.target})
        elif isinstance(r, WishTarget):
            out.append({"kind": "wanted", "nurses": list(r.nurses), "first_day": r.first_day, "last_day": r.last_day,
                        "at_least": r.at_least, "ranged": r.ranged, **_asked(r),
                        **({"codes": sorted(r.codes)} if r.codes is not None else {}),
                        **({"skip": [list(c) for c in r.skip]} if r.skip else {})})
        elif isinstance(r, ShapeTarget):
            out.append({"kind": "shape", "shape": r.kind, "nurses": list(r.nurses), "first_day": r.first_day,
                        "last_day": r.last_day, "at_most": r.at_most, "ranged": r.ranged, "said": r.said, **_asked(r)})
        elif isinstance(r, StaffingPlan):
            out.append({"kind": "staffing", "alternatives": [[_rule_to_json(x) for x in alt] for alt in r.alternatives],
                        "first_day": r.first_day, "last_day": r.last_day, "chosen": r.chosen, "short": r.short,
                        "count_fixed": r.count_fixed})
        elif isinstance(r, GradeFloor):
            out.append({"kind": "grade_floor", "grade": r.grade, "count": r.count, "first_day": r.first_day,
                        "last_day": r.last_day, "codes": list(r.codes), "m_days": list(r.m_days), "short": r.short,
                        "count_fixed": r.count_fixed})
        elif isinstance(r, GiveUpWish):
            out.append({"kind": "give_up_wish", "cells": [list(c) for c in r.cells]})
        else:
            out.append({"kind": "avoid", "nurse_id": r.nurse_id, "day": r.day, "codes": sorted(r.codes)})
    return out


def _rule_to_json(r: StaffRule) -> Dict[str, Any]:
    return {"scope": r.scope, "code": r.code, "low": r.low, "high": r.high, "days": list(r.days), "exact": r.exact}


def _rule_from_json(i: Dict[str, Any]) -> StaffRule:
    return StaffRule(i["scope"], i["code"], int(i["low"]), None if i.get("high") is None else int(i["high"]),
                     tuple(int(d) for d in i.get("days") or ()), i.get("exact"))


def _asked(r) -> Dict[str, Any]:
    """사용자가 정한 날로 줄이기 전 원래 첫날·목표 — 줄인 요청에만 싣는다(기존 기록 모양은 그대로)."""
    out = {}
    if r.asked_first is not None:
        out["asked_first"] = r.asked_first
    if getattr(r, "asked_target", None) is not None:
        out["asked_target"] = r.asked_target
    return out


def requests_from_json(items: Sequence[Dict[str, Any]]) -> List[EditRequest]:
    out: List[EditRequest] = []
    for i in items or []:
        if i["kind"] == "count":
            out.append(CountTarget(i["nurse_id"], frozenset(i["codes"]), int(i["target"]), int(i["first_day"]),
                                   int(i["last_day"]), i.get("transfer"), i.get("asked_first"), i.get("asked_target"),
                                   bool(i.get("total"))))
        elif i["kind"] == "set":
            out.append(SetCell(i["nurse_id"], int(i["day"]), i["code"]))
        elif i["kind"] == "leave":
            out.append(LeaveCell(i["nurse_id"], int(i["day"]), i["code"]))
        elif i["kind"] == "cover":
            out.append(CoverTarget(int(i["day"]), i["code"], int(i["target"]), i.get("grade")))
        elif i["kind"] == "balance":
            out.append(BalanceTarget(frozenset(i["codes"]), tuple(i["members"]), int(i["first_day"]),
                                     int(i["last_day"]), i.get("spread"), i.get("target")))
        elif i["kind"] == "wanted":
            out.append(WishTarget(tuple(i.get("nurses") or ()), int(i["first_day"]), int(i["last_day"]),
                                  i.get("at_least"), bool(i.get("ranged")), i.get("asked_first"),
                                  frozenset(i["codes"]) if i.get("codes") is not None else None,
                                  tuple((str(n), int(d)) for n, d in i.get("skip") or ())))
        elif i["kind"] == "shape":
            out.append(ShapeTarget(i["shape"], tuple(i.get("nurses") or ()), int(i["first_day"]), int(i["last_day"]),
                                   i.get("at_most"), bool(i.get("ranged")), bool(i.get("said")), i.get("asked_first")))
        elif i["kind"] == "staffing":
            out.append(StaffingPlan(tuple(tuple(_rule_from_json(x) for x in alt) for alt in i.get("alternatives") or ()),
                                    int(i["first_day"]), int(i["last_day"]), i.get("chosen"), i.get("short"),
                                    i.get("count_fixed")))
        elif i["kind"] == "grade_floor":
            out.append(GradeFloor(int(i["grade"]), int(i["count"]), int(i["first_day"]), int(i["last_day"]),
                                  tuple(i.get("codes") or ("D", "E", "N")), tuple(int(d) for d in i.get("m_days") or ()),
                                  i.get("short"), i.get("count_fixed")))
        elif i["kind"] == "give_up_wish":
            out.append(GiveUpWish(tuple((str(n), int(d)) for n, d in i.get("cells") or ())))
        else:
            out.append(AvoidCell(i["nurse_id"], int(i["day"]), frozenset(i["codes"])))
    return out


def lever_id(allow: Dict[Tuple, int]) -> str:
    raw = json.dumps(allow_to_json(allow), ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _day_iso(ctx: EditContext, day: int) -> str:
    return ctx.day_date(day).isoformat()


def candidate_to_dict(ctx: EditContext, cand) -> Dict[str, Any]:
    return {
        "rank": cand.rank,
        "changed_count": cand.changed_count,
        "loss": cand.loss,
        "span": {"from": _day_iso(ctx, cand.span[0]), "to": _day_iso(ctx, cand.span[1]),
                 "days": cand.span[1] - cand.span[0]},
        "optimal": cand.optimal,
        "affected": [{"nurse_id": n, "name": ctx.names.get(n, n)} for n in cand.affected],
        "changes": [{
            "nurse_id": c.nurse_id, "name": c.name, "date": _day_iso(ctx, c.day), "day": c.day + 1,
            "before": c.before, "after": c.after, "before_main": c.before_main, "after_main": c.after_main,
        } for c in cand.changes],
        "counts": {n: {code: list(pair) for code, pair in by.items()} for n, by in cand.counts.items()},
        "quality": list(cand.quality),
    }


def lever_to_dict(lever) -> Dict[str, Any]:
    return {
        "lever_id": lever_id(lever.allow),
        "lose": list(lever.lose),
        "changes": {"now": lever.changes[0], "after": lever.changes[1]},
        "span_days": {"now": lever.span[0], "after": lever.span[1]},
        "quality": list(lever.quality),
        "gain": getattr(lever, "gain", None),
        "allow": allow_to_json(lever.allow),
    }


def result_to_dict(ctx: EditContext, result) -> Dict[str, Any]:
    return {
        "status": result.status,
        "reasons": list(result.reasons),
        "notes": list(result.notes),
        "blocking": list(result.blocking),
        "candidates": [candidate_to_dict(ctx, c) for c in result.candidates],
        "levers": [lever_to_dict(lv) for lv in result.levers],
        "source": getattr(result, "source", None),
    }


# ── 대화 상태 저장·복원(S10a) — 서버 메모리 밖(AIDE 저장소)에 싣기 위한 무손실 JSON ────────────────────

#: 직렬화한 상태 JSON(utf-8)의 상한 — 넘으면 후보를 빼고 싣는다(`candidates_dropped`)
SESSION_JSON_LIMIT = 128 * 1024
#: 마지막 결과·넘긴 결과에 싣는 후보 수
KEEP_CANDIDATES = 3


class SessionStateTooLarge(ValueError):
    """후보·레버를 다 빼고도 상태 JSON 이 SESSION_JSON_LIMIT 를 넘는다 — 저장소(S10b)가 이 예외를 받으면 싣지 않고
    기록(`schedule_edit_log`) 복원으로 떨어진다."""


def candidate_to_json(cand) -> Dict[str, Any]:
    """`solver.Candidate` 전 칸 — 화면용 `candidate_to_dict` 와 달리 날짜는 0부터 그대로, ctx 가 필요 없다."""
    return {
        "rank": cand.rank,
        "changes": [{"nurse_id": c.nurse_id, "name": c.name, "day": c.day, "before": c.before, "after": c.after,
                     "before_main": c.before_main, "after_main": c.after_main, "vacation": c.vacation}
                    for c in cand.changes],
        "affected": list(cand.affected), "optimal": cand.optimal, "window": list(cand.window),
        "counts": {n: {code: list(pair) for code, pair in by.items()} for n, by in cand.counts.items()},
        "span": list(cand.span), "loss": cand.loss, "quality": list(cand.quality),
    }


def candidate_from_json(data: Dict[str, Any]):
    from services.schedule_edit.solver import Candidate, CellChange    # solver 가 serialize 를 부르므로 지연 import
    return Candidate(
        rank=int(data["rank"]),
        changes=[CellChange(c["nurse_id"], c["name"], int(c["day"]), c["before"], c["after"], c["before_main"],
                            c["after_main"], bool(c.get("vacation"))) for c in data.get("changes") or []],
        affected=list(data.get("affected") or []), optimal=bool(data["optimal"]),
        window=tuple(data["window"]),
        counts={n: {code: tuple(pair) for code, pair in by.items()} for n, by in (data.get("counts") or {}).items()},
        span=tuple(data["span"]), loss=int(data["loss"]), quality=list(data.get("quality") or []))


def _lever_to_json(lv) -> Dict[str, Any]:
    return {"allow": allow_to_json(lv.allow), "lose": list(lv.lose), "changes": list(lv.changes),
            "span": list(lv.span), "quality": list(lv.quality), "gain": lv.gain}


def _lever_from_json(data: Dict[str, Any]):
    from services.schedule_edit.levers import Lever    # levers·precheck 가 serialize 를 부르므로 지연 import
    return Lever(allow=allow_from_json(data.get("allow")), lose=list(data.get("lose") or []),
                 changes=tuple(data["changes"]), span=tuple(data["span"]), quality=list(data.get("quality") or []),
                 gain=data.get("gain"))


def result_to_json(result) -> Dict[str, Any]:
    """`solver.EditResult` 전 칸 — 후보·레버 포함."""
    return {"status": result.status, "candidates": [candidate_to_json(c) for c in result.candidates],
            "reasons": list(result.reasons), "notes": list(result.notes), "blocking": list(result.blocking),
            "levers": [_lever_to_json(lv) for lv in result.levers], "source": result.source,
            "extra": list(result.extra)}


def result_from_json(data: Dict[str, Any]):
    from services.schedule_edit.solver import EditResult
    return EditResult(status=data["status"], candidates=[candidate_from_json(c) for c in data.get("candidates") or []],
                      reasons=list(data.get("reasons") or []), notes=list(data.get("notes") or []),
                      blocking=list(data.get("blocking") or []),
                      levers=[_lever_from_json(lv) for lv in data.get("levers") or []],
                      source=data.get("source"), extra=list(data.get("extra") or []))


def _kept_result(result) -> Optional[Dict[str, Any]]:
    """저장할 결과 — 후보는 앞 KEEP_CANDIDATES 개만."""
    if result is None:
        return None
    out = result_to_json(result)
    out["candidates"] = out["candidates"][:KEEP_CANDIDATES]
    return out


def _json_bytes(data: Dict[str, Any]) -> int:
    return len(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _results(out: Dict[str, Any]):
    return [r for r in (out["last"], (out["held"] or {}).get("result")) if r]


def session_state_to_json(session, *, edit_from: Optional[int] = None) -> Dict[str, Any]:
    """대화 상태(`EditSession`) → JSON. 판정 재료(ctx·rules·screen)와 시작 해(hint)는 싣지 않는다 — 복원 때 다시 짓는다.

    edit_from: 고칠 수 있는 첫날(0부터) — 안 주면 판정 재료(ctx)에 얹힌 값.
    직렬화 크기가 SESSION_JSON_LIMIT 를 넘으면 단계마다 다시 재며 줄인다 — ① 후보(마지막 결과·넘긴 결과)를 빼고
    `candidates_dropped: true` ② 그래도 넘으면 레버도 빼고 `levers_dropped: true` ③ 그래도 넘으면 SessionStateTooLarge.
    표시는 실제로 뭔가를 뺐을 때만 true — 화면이 '다시 찾기'를 낸다.
    """
    from services.schedule_edit.precheck import finding_to_json
    held = session.held
    out: Dict[str, Any] = {
        "schedule_id": session.schedule_id, "stamp": session.stamp,
        "requests": requests_to_json(session.requests), "allow": allow_to_json(session.allow),
        "avoid_people": [sorted(p) for p in session.avoid_people],
        "confirmed": sorted(session.confirmed), "asking": [finding_to_json(f) for f in session.asking],
        "held": None if held is None else {"requests": requests_to_json(held[0]), "result": _kept_result(held[1]),
                                           "avoid": [sorted(p) for p in held[2]]},
        "shown_notes": sorted(session.shown_notes),
        "edit_from": edit_from if edit_from is not None else getattr(session.ctx, "edit_from", None),
        "last": _kept_result(session.last),
        "candidates_dropped": bool(getattr(session, "candidates_dropped", False)),
        "levers_dropped": bool(getattr(session, "levers_dropped", False)),
    }
    if _json_bytes(out) > SESSION_JSON_LIMIT:
        for result in _results(out):
            if result["candidates"]:
                result["candidates"] = []
                out["candidates_dropped"] = True
    if _json_bytes(out) > SESSION_JSON_LIMIT:
        for result in _results(out):
            if result["levers"]:
                result["levers"] = []
                out["levers_dropped"] = True
    if _json_bytes(out) > SESSION_JSON_LIMIT:
        raise SessionStateTooLarge(f"대화 상태가 {SESSION_JSON_LIMIT} 바이트를 넘습니다(후보·레버를 빼고도).")
    return out


def session_state_from_json(data: Dict[str, Any], *, rebuild: Callable[[str], Any]):
    """JSON → `EditSession`. rebuild(schedule_id) 가 판정 재료(ctx·rules·screen)와 지금 칸 지문(stamp)을 다시 지어 준다.

    `agent.session_for` 가 다시 열 때와 다른 점이 하나 있다. 같은 점: 요청·허용량·넘긴 묶음·동의·묻는 중인 확인·보인
    문구·고칠 첫날은 이어 가고, held 는 요청·넘긴 묶음만 되살린다(결과는 언제나 버린다 — 옛 판정 재료로 찾은 후보이고
    칸 지문은 설정·원티드 변경을 못 본다). 다른 점: 마지막 결과(last)의 후보는 칸 지문이 저장본과 같을 때만 이어 간다
    (다르면 근무표가 바뀐 것이라 버린다). 적용은 잠금 → 최신 확인 → 재검증을 다시 하므로 이어 가도 안전하다.
    """
    from services.schedule_edit.precheck import finding_from_json
    session = rebuild(data["schedule_id"])
    stale = session.stamp != data.get("stamp")
    session.requests = requests_from_json(data.get("requests"))
    session.allow = allow_from_json(data.get("allow"))
    session.avoid_people = [frozenset(p) for p in data.get("avoid_people") or []]
    session.confirmed = set(data.get("confirmed") or [])
    session.asking = [finding_from_json(f) for f in data.get("asking") or []]
    session.shown_notes = set(data.get("shown_notes") or [])
    held = data.get("held")
    if held:
        session.held = (requests_from_json(held.get("requests")), None, [frozenset(p) for p in held.get("avoid") or []])
    last = data.get("last")
    session.last = None if stale or not last else result_from_json(last)
    session.candidates_dropped = bool(data.get("candidates_dropped")) and not stale
    session.levers_dropped = bool(data.get("levers_dropped")) and not stale
    if data.get("edit_from") is not None:
        session.ctx.edit_from = int(data["edit_from"])
    return session
