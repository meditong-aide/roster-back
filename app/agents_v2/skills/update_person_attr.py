"""update-person-attr skill — modify nurse attributes (single or multi-field)."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from agents_v2.skills.registry import register
from agents_v2.tools import nurse_tools
from agents_v2.tools.nurse_tools import compute_batch_changeset
from agents_v2.verify import VerifyResult, readback
from services.team_service import list_teams_with_members


def _extract_mutations(params: dict) -> list[dict]:
    """params에서 mutations 배열을 추출. 단일 field/value도 mutations로 정규화.

    LLM/grounding 변형 모두 수용:
      - {mutations: [{field, value}, ...]}                 # 권장 (다중)
      - {field, value}                                     # 단일 (backward-compat)
      - {mutation: {target_field, target_value}}           # 구식 grounding
    """
    raw = params.get("mutations")
    if isinstance(raw, list) and raw:
        return [
            {"field": m.get("field") or m.get("target_field"),
             "value": m.get("value") if "value" in m else m.get("target_value")}
            for m in raw
        ]
    nested = params.get("mutation") or {}
    field = params.get("field") or nested.get("target_field")
    if field:
        value = params.get("value") if params.get("value") is not None else nested.get("target_value")
        return [{"field": field, "value": value}]
    return []


def _resolve_team_value(value: Any, teams: list[dict]) -> tuple[Any, dict | None]:
    """team_id mutation 값을 정수 team_id 로 해석. int/숫자문자열은 그대로, 팀 이름은 매핑.

    Returns (resolved_value, clarification_or_None).
    """
    if isinstance(value, bool) or value is None:
        return value, None
    if isinstance(value, int):
        return value, None
    if isinstance(value, str):
        s = value.strip()
        if s.lstrip("-").isdigit():
            return int(s), None
        key = s.lower()
        matches = [t for t in teams if str(t["team_name"]).strip().lower() == key]
        if len(matches) == 1:
            return matches[0]["team_id"], None
        names = [t["team_name"] for t in teams]
        if len(matches) > 1:
            return None, {
                "needs_clarification": True,
                "question": f"'{s}'에 해당하는 팀이 여러 개입니다. 어느 팀인가요?",
                "options": names,
            }
        return None, {
            "needs_clarification": True,
            "question": f"'{s}' 팀을 찾지 못했습니다. 어느 팀인가요?",
            "options": names,
        }
    return value, None


def _ground_team_mutations(
    db: Session, office_id: str | None, group_id: str, mutations: list[dict]
) -> tuple[list[dict], dict | None]:
    """team_id mutation 의 value 가 팀 이름이면 내부 DB 조회로 team_id 매핑."""
    if not any(m.get("field") == "team_id" for m in mutations):
        return mutations, None
    if not office_id:
        return mutations, None
    teams = list_teams_with_members(db, office_id, group_id)
    grounded: list[dict] = []
    for m in mutations:
        if m.get("field") != "team_id":
            grounded.append(m)
            continue
        resolved, clar = _resolve_team_value(m.get("value"), teams)
        if clar is not None:
            return mutations, clar
        grounded.append({"field": "team_id", "value": resolved})
    return grounded, None


@register("update-person-attr")
def update_person_attr(db: Session, params: dict) -> Any:
    """Update one or more nurse attributes (transactional per nurse)."""
    nurse_ids = params.get("nurse_ids", [])
    if not nurse_ids:
        return {"error": "nurse_id required"}

    group_id = params.get("group_id")
    if not group_id:
        return {"error": "group_id required (RBAC scope)"}

    mutations = _extract_mutations(params)
    if not mutations:
        return {"error": "mutations (or field+value) required"}

    # 병동이동(소속 group_id 변경)은 이 스킬이 아니라 assignment transfer(월 발효·팀/등급 이관).
    # 여기서 group_id 를 직접 덮어쓰면 그라운딩·RBAC·월 개념이 없어 실패/거짓완료가 난다 → 차단.
    if any(m.get("field") == "group_id" for m in mutations):
        return {
            "error": "병동이동(소속 병동 변경)은 이 기능으로 처리하지 않습니다. "
                     "병동이동 처리(manage_assignment)로 해주세요 — "
                     "예: '신솔희 8월부터 중환자실2로 병동이동'.",
        }

    # 퇴사 처리는 반드시 퇴사일이 있어야 한다 — 값 없이 오면 날짜를 되묻는다.
    # (명시적 '해제'/'취소' 는 값이 있으므로 통과 → 퇴사 취소로 처리)
    for m in mutations:
        if m.get("field") == "resignation_date":
            v = m.get("value")
            if v is None or (isinstance(v, str) and v.strip() == ""):
                return {
                    "needs_clarification": True,
                    "question": "퇴사일을 알려주세요. (예: 2026-08-31)",
                }

    mutations, clar = _ground_team_mutations(
        db, params.get("office_id"), group_id, mutations
    )
    if clar is not None:
        return clar

    preview_only = params.get("preview_only", False)

    results = []
    for nid in nurse_ids:
        if preview_only:
            cs = compute_batch_changeset(db, nid, group_id, mutations)
            if not cs.get("ok"):
                results.append({"nurse_id": nid, **{k: v for k, v in cs.items() if k != "ok"}})
                continue
            preview = {
                "preview": True,
                "nurse_id": nid,
                "applied_mutations": [
                    {
                        "field": m["field"],
                        "current_value": cs["pre_summary"].get(m["field"]),
                        "new_value": m["value"],
                    }
                    for m in cs["normalized_mutations"]
                ],
                "changed_fields": cs["changed_fields"],
            }
            if cs["coupled_log"]:
                preview["coupled_changes"] = cs["coupled_log"]
            results.append(preview)
        else:
            result = nurse_tools.update_nurse_attributes_batch(
                db, nid, group_id, mutations,
                year=params.get("year"), month=params.get("month"),
            )
            results.append(result)

    # 단일 간호사 + 단일 mutation이면 평탄화
    if len(results) == 1:
        return results[0]
    return {"affected_count": len(results), "results": results}


# ── L1 read-back 검증 ────────────────────────────────────────
# 속성 변경(ok)을 보고했으면 간호사를 되읽어 실제로 값이 바뀌었는지 대조.
# period 필드(grade/allowed_shifts/fixed_shift/is_weekend_off/team_id)는 '시점 발효'라
# 캐시 투영이 지연될 수 있어 스킵(오탐 방지). 나머지 직접 컬럼만 검사.
# false-fail-safe: '변경 요청했는데 여전히 이전값'일 때만 실패, 애매하면 통과.
_READBACK_SKIP_FIELDS = frozenset(
    {"grade", "allowed_shifts", "fixed_shift", "is_weekend_off", "team_id"}
)


@readback("update_person_attr")
def _verify_update_person_attr(db: Session, params: dict, result: Any) -> VerifyResult:
    from db.models import Nurse

    # 적용 결과 아이템 목록화(단일 평탄화 / 다중 results 모두 수용).
    items = result.get("results") if isinstance(result.get("results"), list) else [result]
    for item in items:
        if not isinstance(item, dict):
            continue
        muts = item.get("applied_mutations")
        nid = item.get("nurse_id")
        if not muts or not nid:  # preview·비적용 결과는 대상 아님
            continue
        fresh = db.query(Nurse).filter(Nurse.nurse_id == nid).first()
        if fresh is None:
            continue
        summ = nurse_tools._nurse_summary(fresh)
        for m in muts:
            f, frm, to = m.get("field"), m.get("from"), m.get("to")
            if f in _READBACK_SKIP_FIELDS or f not in summ:
                continue
            cur = summ[f]
            if cur == to:  # 반영됨
                continue
            if frm != to and cur == frm:  # 변경 요청했는데 여전히 이전값 → 미반영
                return VerifyResult(
                    False, f"{nid}의 {f} 변경이 반영되지 않았습니다 ({frm}→{to})."
                )
            # 그 외(값 강제변환 등 애매) → 통과(오탐 방지)
    return VerifyResult(True)
