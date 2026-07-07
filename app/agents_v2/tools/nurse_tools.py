"""Nurse data tools — search, filter, read nurse info.

[Phase 0 / Group Scope] year/month 가 주어지면 시점 기반 SSOT 를 사용한다.
- 멤버십: services.assignment_service.group_members_in_month (전입/전출 반영)
- 팀: services.team_period.resolve_team_for_roster (NurseTeamPeriod SSOT)
year/month 가 None 이면 기존 캐시 컬럼(Nurse.group_id / Nurse.team_id) 동작 보존 —
호출부가 시점 컨텍스트 없이 부르는 경우의 후방 호환.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session
from sqlalchemy import func as sa_func

from db.models import Nurse, Team


def _month_member_ids(
    db: Session, group_id: str, year: int | None, month: int | None
) -> set[str] | None:
    """year/month 가 모두 주어지면 그 달 effective 멤버 nurse_id set 반환.

    소속 외(전출/휴직/퇴사) 는 제외 — group_members_in_month 정책 그대로 위임.
    한쪽이라도 None 이면 None 반환 → 호출부는 캐시 경로 사용.
    """
    if year is None or month is None:
        return None
    try:
        from services.assignment_service import group_members_in_month

        snap = group_members_in_month(db, group_id, int(year), int(month))
        return {m["nurse_id"] for m in snap.get("members", [])}
    except Exception:
        # SSOT 헬퍼가 실패하면 캐시 경로로 폴백 (silent — 안전).
        return None


def _resolve_month_team(
    db: Session, nurse_id: str, group_id: str, year: int | None, month: int | None
) -> int | None:
    """year/month 가 있으면 NurseTeamPeriod SSOT 로 effective team_id."""
    if year is None or month is None:
        return None
    try:
        from services.team_period import resolve_team_for_roster

        return resolve_team_for_roster(db, nurse_id, group_id, int(year), int(month))
    except Exception:
        return None


def search_nurses_by_name(
    db: Session,
    group_id: str,
    name_query: str,
    *,
    year: int | None = None,
    month: int | None = None,
) -> list[dict]:
    """Search nurses in a group by name (partial match).

    year/month 가 주어지면 그 달 effective 멤버로 한정 (전입/전출 반영).
    Returns list of candidates with nurse_id, name, team, grade, experience.
    """
    rows = (
        db.query(Nurse)
        .filter(
            Nurse.group_id == group_id,
            Nurse.active == 1,
            Nurse.name.contains(name_query),
        )
        .order_by(Nurse.sequence)
        .all()
    )
    month_ids = _month_member_ids(db, group_id, year, month)
    if month_ids is not None:
        rows = [r for r in rows if r.nurse_id in month_ids]
    return [_nurse_summary(r) for r in rows]


def get_nurses_in_group(
    db: Session,
    group_id: str,
    *,
    year: int | None = None,
    month: int | None = None,
) -> list[dict]:
    """Return all active nurses in a group.

    year/month 가 주어지면 그 달 effective 멤버만 반환(전입/전출 반영).
    """
    rows = (
        db.query(Nurse)
        .filter(Nurse.group_id == group_id, Nurse.active == 1)
        .order_by(Nurse.sequence)
        .all()
    )
    month_ids = _month_member_ids(db, group_id, year, month)
    if month_ids is not None:
        rows = [r for r in rows if r.nurse_id in month_ids]
    return [_nurse_summary(r) for r in rows]


def get_nurse_by_id(db: Session, nurse_id: str, group_id: str) -> dict | None:
    """Get a single nurse by ID, scoped to group_id (RBAC guard)."""
    r = db.query(Nurse).filter(
        Nurse.nurse_id == nurse_id, Nurse.group_id == group_id
    ).first()
    if not r:
        return None
    return _nurse_detail(r)


def filter_nurses(
    db: Session,
    group_id: str,
    *,
    grade: int | None = None,
    night_only: bool | None = None,
    team_id: int | None = None,
    has_preceptor: bool | None = None,
    joined_after: str | None = None,
    joined_before: str | None = None,
    year: int | None = None,
    month: int | None = None,
) -> list[dict]:
    """Filter nurses by various attributes.

    year/month 주어지면:
      - 멤버십: group_members_in_month (전입/전출 반영)
      - team_id 필터: NurseTeamPeriod SSOT 시점 매칭 (캐시 컬럼 의존 해소)
    """
    q = db.query(Nurse).filter(Nurse.group_id == group_id, Nurse.active == 1)
    if grade is not None:
        q = q.filter(Nurse.grade == grade)
    if team_id is not None:
        # 팀 소속은 시점(period) 기준 — nurses.team_id 는 NULL 이행 대상이라 캐시 비교 불가.
        # year/month 가 주어지면 그 달 1일, 아니면 오늘 기준 SSOT 조회.
        from datetime import date as _d
        from services.team_period import resolve_teams_for_month as _rtfm
        on_date = (
            _d(int(year), int(month), 1)
            if year is not None and month is not None
            else _d.today()
        )
        _tids = {
            nid for nid, tv in _rtfm(db, group_id, on_date).items()
            if tv is not None and int(tv) == int(team_id)
        }
        q = q.filter(Nurse.nurse_id.in_(_tids or [""]))
    if has_preceptor is True:
        q = q.filter(Nurse.preceptor_id.isnot(None))
    elif has_preceptor is False:
        q = q.filter(Nurse.preceptor_id.is_(None))
    if joined_after:
        q = q.filter(Nurse.joining_date >= joined_after)
    if joined_before:
        q = q.filter(Nurse.joining_date <= joined_before)
    rows = q.order_by(Nurse.sequence).all()

    # 멤버십 시점 필터 (전입/전출 반영)
    month_ids = _month_member_ids(db, group_id, year, month)
    if month_ids is not None:
        rows = [r for r in rows if r.nurse_id in month_ids]

    result = [_nurse_summary(r) for r in rows]
    if night_only is not None:
        # N전담 = allowed_shifts == ["N"] (리스트 기준). Python 측 필터.
        result = [
            n for n in result
            if ((n.get("allowed_shifts") or []) == ["N"]) == night_only
        ]
    return result


UPDATABLE_FIELDS = {
    "grade", "experience", "role",
    "is_head_nurse", "allowed_shifts",
    "preceptor_id", "fixed_shift",
    "is_weekend_off", "nurse_memo",
    "enable_aide", "wanted_max_requests",
    "weekly_off_enabled", "weekly_off_weekday",
    "personal_off_adjustment",
    "team_id", "group_id",
}


# ── Value normalization (field-specific) ────────────────────

_VALID_SHIFT_CODES = {"D", "E", "N", "M"}

_KOREAN_PHRASE_ALIASES = {
    # Korean substring matches (compound phrases like '야간 전담').
    # 2026-06-01: '주간'/'저녁'/'낮'/'밤' 추가 — _shift_category.SHIFT_CATEGORY 와 정렬.
    "야간": "N", "나이트": "N", "밤": "N",
    "데이": "D", "주간": "D", "낮": "D",
    "이브닝": "E", "저녁": "E", "초번": "E",
    "미드": "M", "미들": "M", "중간": "M",
}
_LATIN_TOKEN_ALIASES = {
    # Exact lowercase match (avoids false positives like 'invalid' → 'n')
    "n": "N", "night": "N",
    "d": "D", "day": "D",
    "e": "E", "evening": "E",
    "m": "M", "mid": "M",
}

_CLEAR_TOKENS = {"해제", "없음", "off", "none", "전담해제", "해지", ""}

_BOOL_FIELDS = {"is_head_nurse", "is_weekend_off", "weekly_off_enabled", "enable_aide"}
_INT_FIELDS = {
    "grade", "experience", "wanted_max_requests",
    "personal_off_adjustment", "weekly_off_weekday", "team_id",
}
_VALID_FIXED_SHIFT_CODES = {"D", "E", "N", "M", "O"}


_FIXED_SHIFT_OFF_ALIASES = {"오프", "휴무", "쉼", "쉬는날", "off", "o"}


def _normalize_fixed_shift(value):
    """fixed_shift는 단일 enum (D/E/N/M/O). 한글 별칭도 직접 수용.

    값 매핑:
      None / '' / '해제' / '없음' → '' (해제)
      'D'/'데이'/'주간'           → 'D'
      'E'/'이브닝'/'저녁'         → 'E'
      'N'/'나이트'/'야간'         → 'N'
      'M'/'미드'                  → 'M'
      'O'/'오프'/'휴무'/'쉼'      → 'O'

    2026-06-01: LLM 위임에서 직접 수용으로 전환 — [[skill-internal-grounding]] 원칙
    일관화. is_night_nurse 는 _coerce_shift_code 로 이미 한글 받음, fixed_shift 만 누락.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        return None
    s = value.strip()
    if s == "":
        return ""
    low = s.lower()
    if low in _CLEAR_TOKENS:
        return ""
    up = s.upper()
    if up in _VALID_FIXED_SHIFT_CODES:
        return up
    # 'O' alias (fixed_shift 전용 — is_night_nurse 엔 'O' 없음)
    if low in _FIXED_SHIFT_OFF_ALIASES:
        return "O"
    # D/E/N/M 한글 별칭 — _coerce_shift_code 재사용
    coerced = _coerce_shift_code(s)
    if coerced in _VALID_FIXED_SHIFT_CODES:
        return coerced
    return None


def _coerce_shift_code(raw) -> str | None:
    """Token → 'D'/'E'/'N'/'M', or '' for clear, None if unrecognized."""
    if raw is None:
        return ""
    s = str(raw).strip()
    if not s:
        return ""
    low = s.lower()
    if low in _CLEAR_TOKENS:
        return ""
    # Korean substring (compound phrases like '야간 전담', '데이 전담')
    for k, code in _KOREAN_PHRASE_ALIASES.items():
        if k in s:
            return code
    # Latin: exact match only (avoids 'invalid' → 'n' substring false-positive)
    if low in _LATIN_TOKEN_ALIASES:
        return _LATIN_TOKEN_ALIASES[low]
    upper = s.upper()
    if upper in _VALID_SHIFT_CODES:
        return upper
    # middleware 가 이미 해석한 shift_id (예: "N_GRP001", "OFF_GRP001", "N2_GRP001") 통과.
    # group_id 마커("_GRP") 를 명시 인장으로 사용 — 사용자 입력엔 나오지 않는 패턴.
    if "_GRP" in upper:
        return upper
    return None


# ── Shift-grounding helpers (B4/B5 공용) ────────────────────────
# query_schedule / bulk_mutation 등에서 LLM 이 보내는 shift 코드·이름을 정규화하고
# 모르는 값은 needs_clarification 으로 즉시 반환. silent skip 회귀 차단.

_SHIFT_CLARIFY_OPTIONS = ["D (데이)", "E (이브닝)", "N (나이트)", "M (미드)"]


def normalize_shift_codes(codes: Any) -> tuple[list[str] | None, dict | None]:
    """codes(list/str/None) → 정규화된 ['D','N',...] 또는 (None, clarification).

    None → (None, None) passthrough. 빈 list → ([], None).
    한글/별칭/대소문자 수용. 인식 불가 1개라도 있으면 즉시 clarification.
    """
    if codes is None:
        return None, None
    if not isinstance(codes, (list, tuple)):
        codes = [codes]
    if len(codes) == 0:
        return [], None
    out: list[str] = []
    unrecognized: list[str] = []
    for raw in codes:
        norm = _coerce_shift_code(raw)
        if norm in (None, ""):
            unrecognized.append(str(raw))
        elif norm not in out:
            out.append(norm)
    if unrecognized:
        return None, {
            "needs_clarification": True,
            "question": (
                f"'{', '.join(unrecognized)}' 근무 코드를 인식하지 못했습니다. "
                "어떤 근무인가요?"
            ),
            "options": _SHIFT_CLARIFY_OPTIONS,
        }
    return out, None


def normalize_single_shift_code(value: Any) -> tuple[str | None, dict | None]:
    """단일 shift → 'D' 등 또는 (None, clarification). None passthrough.

    allowed_shifts/fixed_shift 가 아닌, 단일 '근무 코드'(예: bulk_mutation 의 new_shift_code,
    add_shift 의 shift)용. 'O'(off) 도 허용.
    """
    if value is None or value == "":
        return None, None
    norm = _coerce_shift_code(value)
    if norm in (None, ""):
        return None, {
            "needs_clarification": True,
            "question": f"'{value}' 근무 코드를 인식하지 못했습니다. 어떤 근무인가요?",
            "options": _SHIFT_CLARIFY_OPTIONS,
        }
    return norm, None


def _normalize_allowed_shifts(value) -> list[str] | None:
    """Normalize various inputs to a list of valid shift codes.

    Returns None if input is uninterpretable.
    Examples:
      None / False / [] / '해제'  → []
      True / 'N' / '야간'          → ['N']
      'D' / '데이 전담'            → ['D']
      ['D', 'E'] / 'D,E'           → ['D', 'E']
    """
    if value is None or value is False or value == 0:
        return []
    if value is True:
        return ["N"]  # best-effort: bool True → 야간 전담
    if isinstance(value, str):
        # Multi-token string ("D,E" / "[D,E]" / "['D','E']") → recurse as list
        if any(ch in value for ch in (",", "[")):
            parts = [
                p.strip().strip("'\"")
                for p in value.replace("[", "").replace("]", "").split(",")
                if p.strip().strip("'\"")
            ]
            if len(parts) > 1:
                return _normalize_allowed_shifts(parts)
            value = parts[0] if parts else ""
        coerced = _coerce_shift_code(value)
        if coerced is None:
            return None
        if coerced == "":
            return []
        return [coerced]
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        seen: set[str] = set()
        for item in value:
            c = _coerce_shift_code(item)
            if c in (None, ""):
                continue
            if c not in seen:
                seen.add(c)
                out.append(c)
        return out
    return None


def _coerce_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        s = value.strip().lower()
        if s in {"true", "yes", "y", "활성화", "활성", "on", "1", "지정"}:
            return True
        if s in {"false", "no", "n", "해제", "비활성화", "비활성", "off", "0"}:
            return False
    return None


def _coerce_int(value):
    if isinstance(value, bool):
        return None  # bool→int 의도치 않은 변환 방지
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        s = value.strip()
        if s.lstrip("-").isdigit():
            return int(s)
    return None


def _normalize_value(field: str, value):
    """Normalize value per field. Returns (normalized, error_msg)."""
    if field == "allowed_shifts":
        norm = _normalize_allowed_shifts(value)
        if norm is None:
            return None, (
                f"allowed_shifts 값 '{value}'을(를) 해석할 수 없습니다. "
                "예: ['N'](야간 전담), ['D'](데이 전담), ['D','E'](N 제외), [](해제)."
            )
        return norm, None
    if field == "fixed_shift":
        norm = _normalize_fixed_shift(value)
        if norm is None:
            return None, (
                f"fixed_shift는 'D','E','N','M','O' 중 하나 또는 빈 문자열이어야 합니다 "
                f"(받은 값: {value!r})."
            )
        return norm, None
    if field in _BOOL_FIELDS:
        b = _coerce_bool(value)
        if b is None:
            return None, f"{field}는 true/false 값이어야 합니다 (받은 값: {value!r})."
        return b, None
    if field in _INT_FIELDS:
        n = _coerce_int(value)
        if n is None:
            return None, f"{field}는 정수여야 합니다 (받은 값: {value!r})."
        return n, None
    return value, None


def compute_coupled_changes(attribute: str, normalized_value, current_summary: dict) -> list[dict]:
    """필드 변경 시 자동 동반되는 부수 변경 목록.

    fixed_shift: 코드 설정 → is_weekend_off=True / 해제(빈 값) → is_weekend_off=False.
    """
    coupled: list[dict] = []
    if attribute == "fixed_shift":
        new_weo = bool(normalized_value)  # 'D'/'E'/'N'/'M'/'O' truthy, '' falsy
        cur_weo = bool(current_summary.get("is_weekend_off"))
        if cur_weo != new_weo:
            coupled.append({
                "field": "is_weekend_off",
                "current_value": cur_weo,
                "new_value": new_weo,
                "reason": "fixed_shift 변경에 따른 자동 동반",
            })
    return coupled


def check_preceptor_team_consistency(
    db: Session, nurse_id: str, group_id: str, new_team_id: int | None
) -> dict | None:
    """팀 변경 시 프리셉터-프리셉티 매칭이 깨지는지 검사.

    group_id-scoped: preceptor/preceptee 모두 같은 group_id 내에서만 검사.

    Returns None if no conflict. Otherwise dict describing the mismatch:
      {"as_preceptee": {...}, "as_preceptor": [...]}
    """
    nurse = (
        db.query(Nurse)
        .filter(Nurse.nurse_id == nurse_id, Nurse.group_id == group_id)
        .first()
    )
    if not nurse:
        return None
    if nurse.team_id == new_team_id:
        return None  # no actual team change

    issues: dict = {}

    # (a) 본인이 프리셉티인 경우 → 프리셉터 팀과 비교
    if nurse.preceptor_id:
        preceptor = (
            db.query(Nurse)
            .filter(
                Nurse.nurse_id == nurse.preceptor_id,
                Nurse.group_id == group_id,
                Nurse.active == 1,
            )
            .first()
        )
        if preceptor and preceptor.team_id != new_team_id:
            issues["as_preceptee"] = {
                "preceptor_id": preceptor.nurse_id,
                "preceptor_name": preceptor.name,
                "preceptor_team_id": preceptor.team_id,
            }

    # (b) 본인이 프리셉터인 경우 → 모든 프리셉티들의 팀과 비교
    preceptees = (
        db.query(Nurse)
        .filter(
            Nurse.preceptor_id == nurse_id,
            Nurse.group_id == group_id,
            Nurse.active == 1,
        )
        .all()
    )
    mismatched = [
        {"nurse_id": p.nurse_id, "name": p.name, "team_id": p.team_id}
        for p in preceptees
        if p.team_id != new_team_id
    ]
    if mismatched:
        issues["as_preceptor"] = mismatched

    return issues or None


def detect_state_contradictions(state: dict) -> list[str]:
    """최종 state에서 의미적 모순을 검출.

    예: fixed_shift 코드가 설정되어 있는데 is_weekend_off=False
        → 평일 고정근무는 주말 휴무가 강제됨 (사용자 정의)
    """
    issues: list[str] = []
    fs = state.get("fixed_shift")
    weo = state.get("is_weekend_off")
    if fs and not weo:
        issues.append(
            f"fixed_shift='{fs}' (평일 고정근무)는 주말 휴무가 강제됩니다. "
            "is_weekend_off=False를 동시에 설정할 수 없습니다."
        )
    return issues


def compute_batch_changeset(
    db: Session, nurse_id: str, group_id: str, mutations: list[dict]
) -> dict:
    """순차 시뮬레이션으로 최종 변경 세트 계산. DB 미수정.

    group_id-scoped: nurse lookup 시 group_id 격리 (cross-group mutation 방어).

    Returns dict with:
      - ok: bool
      - error / details (when not ok)
      - pre_summary, final_state, normalized_mutations, coupled_log,
        changed_fields (when ok)
    """
    if not mutations:
        return {"ok": False, "error": "mutations required"}

    normalized_muts: list[dict] = []
    for m in mutations:
        f = m.get("field")
        v = m.get("value")
        if not f:
            return {"ok": False, "error": "mutation.field required", "mutation": m}
        if f not in UPDATABLE_FIELDS:
            return {
                "ok": False,
                "error": f"Field '{f}' is not modifiable via this skill.",
                "allowed_fields": sorted(UPDATABLE_FIELDS),
            }
        norm, err = _normalize_value(f, v)
        if err:
            return {"ok": False, "error": err, "field": f, "received": v}
        normalized_muts.append({"field": f, "value": norm})

    nurse = (
        db.query(Nurse)
        .filter(Nurse.nurse_id == nurse_id, Nurse.group_id == group_id)
        .first()
    )
    if not nurse:
        return {"ok": False, "error": f"Nurse {nurse_id} not found in group {group_id}"}

    pre_summary = _nurse_summary(nurse)
    state = dict(pre_summary)
    coupled_log: list[dict] = []

    for mut in normalized_muts:
        f, v = mut["field"], mut["value"]
        coupled = compute_coupled_changes(f, v, state)
        state[f] = v
        for c in coupled:
            coupled_log.append({**c, "triggered_by": f})
            state[c["field"]] = c["new_value"]

    contradictions = detect_state_contradictions(state)
    if contradictions:
        return {
            "ok": False,
            "error": "contradictory_state",
            "details": contradictions,
            "guidance": (
                "동시에 변경하는 필드들이 의미적으로 충돌합니다. "
                "관련 필드를 함께 조정해서 다시 시도해주세요."
            ),
        }

    if state.get("team_id") != pre_summary.get("team_id"):
        conflict = check_preceptor_team_consistency(db, nurse_id, group_id, state["team_id"])
        if conflict:
            return {
                "ok": False,
                "error": "preceptor_team_mismatch",
                "nurse_id": nurse_id,
                "nurse_name": nurse.name,
                "current_team_id": pre_summary.get("team_id"),
                "new_team_id": state["team_id"],
                "conflict": conflict,
                "guidance": (
                    "프리셉터-프리셉티 매칭이 같은 팀이어야 합니다. "
                    "(1) 관계 해제, (2) 함께 이동, (3) 취소 중 선택해주세요."
                ),
            }

    explicit_fields = {m["field"] for m in normalized_muts}
    for c in coupled_log:
        if c["field"] in explicit_fields:
            c["overridden_by_explicit"] = True

    changed = {
        f: v
        for f, v in state.items()
        if f in UPDATABLE_FIELDS and v != pre_summary.get(f)
    }

    return {
        "ok": True,
        "pre_summary": pre_summary,
        "final_state": state,
        "normalized_mutations": normalized_muts,
        "coupled_log": coupled_log,
        "changed_fields": changed,
    }


def update_nurse_attributes_batch(
    db: Session,
    nurse_id: str,
    group_id: str,
    mutations: list[dict],
    *,
    year: int | None = None,
    month: int | None = None,
) -> dict:
    """단일 간호사에 여러 mutation을 트랜잭션으로 적용.

    group_id-scoped: cross-group mutation 차단.

    시점(period) 일원화:
      - grade/allowed_shifts/fixed_shift/is_weekend_off 는 각 nurse_*_period(SSOT)로
        upsert. 물리 컬럼은 upsert_period 가 단방향 투영(직접 setattr 금지).
      - team_id 는 NurseTeamPeriod SSOT 로 기록(캐시 컬럼도 함께 갱신, 구 화면 호환).
      - valid_from 은 year/month 가 주어지면 그 달 1일(월 셀렉터 발효 정합),
        미지정 시 오늘 날짜.
    """
    cs = compute_batch_changeset(db, nurse_id, group_id, mutations)
    if not cs["ok"]:
        return {k: v for k, v in cs.items() if k != "ok"}

    nurse = (
        db.query(Nurse)
        .filter(Nurse.nurse_id == nurse_id, Nurse.group_id == group_id)
        .first()
    )
    from datetime import date as _date

    # 공유 valid_from: 선택월 1일 우선, 없으면 오늘 (period 쓰기 전 경로 일관).
    if year is not None and month is not None:
        valid_from = _date(int(year), int(month), 1)
    else:
        valid_from = _date.today()

    changed_fields = cs["changed_fields"]
    team_change = "team_id" in changed_fields
    for f, v in changed_fields.items():
        if f == "allowed_shifts":
            # 허용 근무형 → nurse_allowed_shift_period 일원화. 컬럼은 단방향 투영(직접쓰기 금지).
            from db.models import NurseAllowedShiftPeriod
            from services.nurse_period_resolver import upsert_period
            upsert_period(
                db, NurseAllowedShiftPeriod, nurse.nurse_id, valid_from,
                "allowed_shifts", v if isinstance(v, list) else [],
                nurse=nurse, cache_attr="allowed_shifts", source="edited",
            )
        elif f == "grade":
            # grade → nurse_grade_period 일원화(병동귀속). 컬럼은 단방향 투영(직접쓰기 금지).
            from db.models import NurseGradePeriod
            from services.nurse_period_resolver import upsert_period
            upsert_period(
                db, NurseGradePeriod, nurse.nurse_id, valid_from,
                "grade", v, group_id=group_id,
                nurse=nurse, cache_attr="grade", source="edited",
            )
        elif f == "fixed_shift":
            # 고정 근무형 → nurse_allowed_shift_period(통합 satellite)의 fixed_shift 컬럼.
            from db.models import NurseAllowedShiftPeriod
            from services.nurse_period_resolver import upsert_period
            upsert_period(
                db, NurseAllowedShiftPeriod, nurse.nurse_id, valid_from,
                "fixed_shift", v, nurse=nurse, cache_attr="fixed_shift",
                carry_attrs=["allowed_shifts"], source="edited",
            )
        elif f == "is_weekend_off":
            # 주말휴무 → nurse_weekendoff_period. 컬럼은 단방향 투영(직접쓰기 금지).
            from db.models import NurseWeekendOffPeriod
            from services.nurse_period_resolver import upsert_period
            upsert_period(
                db, NurseWeekendOffPeriod, nurse.nurse_id, valid_from,
                "weekend_off", 1 if v else 0,
                nurse=nurse, cache_attr="is_weekend_off", source="edited",
            )
        else:
            setattr(nurse, f, v)

    if team_change:
        try:
            from services.team_period import set_team_period

            set_team_period(
                db,
                nurse_id=nurse_id,
                group_id=group_id,
                valid_from=valid_from,
                team_id=changed_fields["team_id"],
                source="agent",
                commit=False,
            )
        except Exception:
            # SSOT 기록 실패해도 캐시는 이미 갱신됨 — 부분 안전. 로그만.
            import logging as _log
            _log.getLogger(__name__).warning(
                "[nurse_tools] set_team_period SSOT 기록 실패 nurse=%s group=%s",
                nurse_id, group_id,
            )

    db.commit()
    db.refresh(nurse)

    out = _nurse_summary(nurse)
    out["applied_mutations"] = [
        {
            "field": m["field"],
            "from": cs["pre_summary"].get(m["field"]),
            "to": m["value"],
        }
        for m in cs["normalized_mutations"]
    ]
    if cs["coupled_log"]:
        out["coupled_changes"] = cs["coupled_log"]
    return out


def update_nurse_attribute(
    db: Session, nurse_id: str, group_id: str, attribute: str, value
) -> dict:
    """단일 필드 변경. 내부적으로 batch로 위임."""
    return update_nurse_attributes_batch(
        db, nurse_id, group_id, [{"field": attribute, "value": value}]
    )


# ── private helpers ──────────────────────────────────────────


def _nurse_summary(r: Nurse) -> dict:
    return {
        "nurse_id": r.nurse_id,
        "name": r.name,
        "grade": r.grade,
        "experience": r.experience,
        "role": r.role,
        "team_id": r.team_id,
        "is_head_nurse": bool(r.is_head_nurse),
        "allowed_shifts": r.allowed_shifts,
        "preceptor_id": r.preceptor_id,
        "fixed_shift": r.fixed_shift,
        "is_weekend_off": bool(r.is_weekend_off),
        "joining_date": str(r.joining_date) if r.joining_date else None,
        "work_shifts": r.work_shifts,
    }


def _nurse_detail(r: Nurse) -> dict:
    d = _nurse_summary(r)
    d.update({
        "account_id": r.account_id,
        "emp_num": r.emp_num,
        "office_id": r.office_id,
        "group_id": r.group_id,
        "level_": r.level_,
        "personal_off_adjustment": r.personal_off_adjustment,
        "weekly_off_enabled": bool(r.weekly_off_enabled),
        "weekly_off_weekday": r.weekly_off_weekday,
        "nurse_memo": r.nurse_memo,
        "active": r.active,
        "sequence": r.sequence,
        "enable_aide": bool(r.enable_aide) if r.enable_aide is not None else True,
        "wanted_max_requests": r.wanted_max_requests,
        "hn_auth": r.hn_auth,
    })
    return d
