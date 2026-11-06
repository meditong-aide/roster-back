"""결과를 스킬·화면이 쓰는 JSON 으로. 날짜는 사람이 보는 1일부터, 칸 위치는 nurse_id + 날짜(ISO).

레버 허용량의 열쇠(규칙 위치 튜플)는 JSON 키가 될 수 없어 [열쇠 목록, 양] 쌍 목록으로 바꾼다.
레버 id 는 허용량 내용의 해시 — 같은 레버면 같은 id(화면이 고른 레버를 그대로 돌려보낸다).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Sequence, Tuple

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
