"""해결책 → "어디서 어떻게 고치는가"(fix 위치/방식) 구조화.

infeasibility 해결 옵션 각각에 프론트가 렌더링할 `fix` 객체를 붙인다:
  - where: 프론트가 실제 화면으로 라우팅할 **의미 키**(front 계약)
  - where_label_ko: 사람이 읽는 위치 경로(브레드크럼)
  - mode: auto_apply(원클릭 적용) | manual_edit(그 화면서 직접 수정) | manual_navigate(대상으로 이동해 수정)
  - how_ko: 무엇을 얼마로 바꾸는지 한 줄
  - target: 딥링크 컨텍스트(nurse_id/day 등) — 없으면 None

config_key(자동 노브)는 _FIX_BY_KEY 로, config 로 못 고치는 데이터성 원인은
_FIX_BY_REASON 로 매핑. where 의미 키는 프론트가 라우팅 테이블로 해석한다.
"""
from __future__ import annotations

from typing import Any, Optional

# config_key → (where, where_label_ko, mode, toggle?)  ── 대부분 원클릭 자동적용
_FIX_BY_KEY: dict[str, tuple[str, str, str, bool]] = {
    "max_nig_per_month":        ("roster_config.night_month_limit", "설정 > 근무 규칙 > 한 달 밤 근무 최대 횟수", "auto_apply", False),
    "max_consecutive_nights":   ("roster_config.max_consec_night",  "설정 > 근무 규칙 > 밤 근무 연달아 최대 일수", "auto_apply", False),
    "max_conseq_work":          ("roster_config.max_consec_work",   "설정 > 근무 규칙 > 연달아 일하는 최대 날수", "auto_apply", False),
    "off_days":                 ("roster_config.off_days",          "설정 > 근무 규칙 > 한 달 필수 휴무 날수",   "auto_apply", False),
    "two_offs_after_two_nig":   ("roster_config.recovery_2n",       "설정 > 근무 규칙 > 밤 2번 뒤 2일 쉬기",     "auto_apply", True),
    "two_offs_after_three_nig": ("roster_config.recovery_3n",       "설정 > 근무 규칙 > 밤 3번 뒤 2일 쉬기",     "auto_apply", True),
    "not_one_night":            ("roster_config.not_one_night",     "설정 > 근무 규칙 > 밤 근무 하루만 서기 금지", "auto_apply", True),
    "ban_n_to_d":               ("roster_config.transition",        "설정 > 근무 규칙 > 밤 다음날 낮 근무 금지", "auto_apply", True),
    "ban_n_to_e":               ("roster_config.transition",        "설정 > 근무 규칙 > 밤 다음날 저녁 근무 금지", "auto_apply", True),
    "banned_day_after_eve":     ("roster_config.transition",        "설정 > 근무 규칙 > 저녁 다음날 낮 근무 금지", "auto_apply", True),
    "ban_night_before_fixed_off": ("roster_config.transition",      "설정 > 근무 규칙 > 쉬는 날 전날 밤 근무 금지", "auto_apply", True),
    "weekend_off_only_enable":  ("roster_config.weekend_off",       "설정 > 근무 규칙 > 주말 무조건 휴무",       "auto_apply", True),
    "team_min_soft_fallback":   ("roster_config.team_min",          "설정 > 팀 최소 인원",                      "auto_apply", True),
    "preceptee_on":             ("roster_config.preceptee",         "설정 > 교육(프리셉티) 함께 근무",           "auto_apply", True),
    "daily_shift_requirements": ("roster_config.daily_need",        "설정 > 날짜별 필요 인원",                   "manual_edit", False),
}

# reason_code(원인) → (where, where_label_ko, mode, target_field)  ── 데이터 직접 수정
_FIX_BY_REASON: dict[str, tuple[str, str, str, Optional[str]]] = {
    "ALLOWED_SHIFTS_ISOLATES_NURSE":  ("nurse.allowed_shifts", "간호사 관리 > 해당 간호사 > 설 수 있는 근무 종류", "manual_navigate", "nurse_id"),
    "GLOBAL_SHIFT_ALLOWED_SHORTAGE":  ("nurse.allowed_shifts", "간호사 관리 > 설 수 있는 근무 종류(대상 확대)",   "manual_navigate", "nurse_id"),
    "N_ONLY_ROLE_OVERSUPPLY":         ("nurse.allowed_shifts", "간호사 관리 > 설 수 있는 근무 종류",             "manual_navigate", "nurse_id"),
    "CAPACITY_TOTAL_SHORTAGE":        ("nurse.roster",         "간호사 관리 > 인원 추가  또는  설정 > 날짜별 필요 인원", "manual", None),
    "GLOBAL_DAY_CAPACITY_SHORTAGE":   ("roster_config.daily_need", "설정 > 날짜별 필요 인원(해당 날 감축)",      "manual_edit", "day"),
    "N_CAPACITY_SHORTAGE":            ("roster_config.daily_need", "설정 > 날짜별 필요 인원(밤 근무 감축)",       "manual_edit", "day"),
    "GRADE_MIN_EXCEEDS_MAX":          ("roster_config.grade",  "설정 > 직급별 인원(최소/최대)",                 "manual_edit", None),
    "GRADE_MIN_SUM_EXCEEDS_NEED":     ("roster_config.grade",  "설정 > 직급별 최소 인원",                       "manual_edit", None),
    "GRADE_MAX_SUM_BELOW_NEED":       ("roster_config.grade",  "설정 > 직급별 최대 인원",                       "manual_edit", None),
    "MONTHLY_LIMIT_MIN_EXCEEDS_MAX":  ("nurse.monthly_limit",  "간호사 관리 > 해당 간호사 > 월 근무 한도",       "manual_navigate", "nurse_id"),
    "MONTHLY_LIMIT_N_EXACT_UNATTAINABLE": ("nurse.monthly_limit", "간호사 관리 > 해당 간호사 > 월 밤 근무 정확 횟수", "manual_navigate", "nurse_id"),
    "TEAM_SIZE_INSUFFICIENT":         ("roster_config.team_min", "팀 인원 보강  또는  설정 > 팀 최소 인원",       "manual", None),
    "TEAM_MIN_EXCEEDS_GLOBAL_NEED":   ("roster_config.team_min", "설정 > 팀 최소 인원",                          "manual_edit", None),
    "MID_REQUIRED_MISSING":           ("roster_config.daily_need", "설정 > 날짜별 필요 인원(중간 근무 추가)",     "manual_edit", None),
    "MID_DISABLED_BUT_USED":          ("roster_config.mid",    "설정 > 중간 근무 사용 여부",                    "manual_edit", None),
    "FIXED_ASSIGN_EXCEEDS_NEED":      ("fixed_shift.edit",     "고정 근무 편집 > 해당 날짜",                    "manual_navigate", "day"),
    "FIXED_ASSIGN_VIOLATES_ALLOWED":  ("fixed_shift.edit",     "고정 근무 편집 > 해당 간호사",                  "manual_navigate", "nurse_id"),
    "FIXED_OFF_EXCEEDS_SPAN":         ("fixed_shift.edit",     "고정 근무/휴무 편집 > 해당 간호사",             "manual_navigate", "nurse_id"),
    "PER_NURSE_SEQUENCE_INFEASIBLE":  ("fixed_shift.edit",     "고정 근무 편집 > 해당 간호사",                  "manual_navigate", "nurse_id"),
    "INITIAL_FORBIDDEN_CONCENTRATION": ("nurse.allowed_shifts", "간호사 관리 > 막아둔 근무 종류 풀기",          "manual_navigate", "nurse_id"),
    "DISPATCH_INBOUND_WINDOW_NARROW": ("dispatch",             "파견 관리 > 파견 기간/대상",                    "manual_navigate", "nurse_id"),
    "DISPATCH_OUTBOUND_SOURCE_SHORT": ("dispatch",             "파견 관리 > 송출 기간/인원",                    "manual_navigate", "nurse_id"),
    "DISPATCH_INBOUND_SHIFT_INCOMPAT": ("dispatch",            "파견 관리 > 대상/근무 재선정",                  "manual_navigate", "nurse_id"),
    "PRECEPTEE_SYNC_MISMATCH":        ("roster_config.preceptee", "설정 > 교육(프리셉티) 함께 근무",            "auto_apply", None),
}


def _how_ko(config_key: str, change: Optional[dict], toggle: bool) -> str:
    """무엇을 얼마로 바꾸는지 한 줄. change(from/to/suggested_value) 있으면 값까지."""
    if toggle:
        return "이 규칙을 잠시 꺼주세요 (아래 버튼으로 바로 적용됩니다)."
    to = None
    if isinstance(change, dict):
        to = change.get("to")
        if to is None:
            to = change.get("suggested_value")
    if to is not None:
        return f"{to}(으)로 바꾸세요 (아래 버튼으로 바로 적용됩니다)."
    return "값을 조정하세요 (아래 버튼으로 바로 적용됩니다)."


def fix_for_option(opt: dict[str, Any]) -> Optional[dict[str, Any]]:
    """resolution_option → fix 객체. config_key 기반(자동/probe/사이징). 매핑 없으면 None."""
    changes = opt.get("changes") or []
    # 첫 config_key 채택(대개 단일 노브)
    ck = None
    _change = None
    for c in changes:
        if c.get("config_key"):
            ck = c.get("config_key")
            _change = c
            break
    if not ck:
        _apply = opt.get("apply") or {}
        ck = next(iter(_apply.keys()), None)
    if not ck or ck not in _FIX_BY_KEY:
        return None
    where, label, mode, toggle = _FIX_BY_KEY[ck]
    return {
        "mode": mode,
        "where": where,
        "where_label_ko": label,
        "how_ko": _how_ko(ck, _change, toggle),
        "config_key": ck,
        "target": None,
    }


def fix_for_reason(reason_code: str, evidence: Optional[dict] = None) -> Optional[dict[str, Any]]:
    """config 로 못 고치는 데이터성 원인 → fix(수동 위치 + 딥링크 target)."""
    m = _FIX_BY_REASON.get(str(reason_code or "").upper())
    if not m:
        return None
    where, label, mode, target_field = m
    target = None
    if target_field and isinstance(evidence, dict):
        val = evidence.get(target_field) or evidence.get(target_field + "s")
        if val is not None:
            target = {target_field: val}
    return {
        "mode": mode,
        "where": where,
        "where_label_ko": label,
        "how_ko": None,
        "target": target,
    }


def attach_fix_to_options(resolution_options: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """각 옵션에 fix(config_key 기반) 부착. idempotent."""
    for opt in resolution_options or []:
        if isinstance(opt, dict) and "fix" not in opt:
            f = fix_for_option(opt)
            if f is not None:
                opt["fix"] = f
    return resolution_options


def attach_fix_to_causes(causes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """각 cause 에 fix(reason_code 기반, 데이터성 수정 위치+딥링크) 부착. idempotent."""
    for c in causes or []:
        if not isinstance(c, dict) or "fix" in c:
            continue
        rc = c.get("reason_code") or c.get("node_id")
        ev = c.get("details") if isinstance(c.get("details"), dict) else c.get("evidence")
        f = fix_for_reason(rc, ev if isinstance(ev, dict) else None)
        if f is not None:
            c["fix"] = f
    return causes
