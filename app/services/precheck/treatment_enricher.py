"""Treatment recommendation enrichment — cause.details.member_sample 기반 동적 정밀화.

기존 `propose_bundles(...)` 가 반환하는 treatment 의 `rationale_ko` 는 템플릿
수준 ("일별 시프트 요구 인원을 1~2명 감소") 이라 운영자가 *어디서* *얼마나*
풀어야 하는지 즉시 파악할 수 없다.

이 모듈은 각 cause 의 `details.member_sample` (typed node list) 를 node type
별 renderer 로 풀어 treatment 의 `rationale_ko / specific_evidence /
evidence_summary_ko` 에 구체 값 (day, shift, 숫자, nurse_id) 을 합성한다.

generic 동작: 새 cause 가 추가돼도 그 cause 의 member_sample 노드 type 이
아래 RENDER_BY_TYPE 에 등록돼 있으면 자동으로 구체화. 미등록 type 은
`_render_default` 로 fallback (label + value 또는 human_message_ko).

HardCase (multi-domain 충돌, manual_investigation bundle) 도 자동 처방은
불가능하지만 **evidence 는 그대로 surface** 해서 운영자에게 "복합 문제" 임을
명시한다.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional


_NURSE_IDX_RE = re.compile(r"nurse_(\d+)")


# ── Treatment magnitude sizing ────────────────────────────────────────────
# aggregate precheck 는 원인을 잡을 때 이미 정확한 숫자(요구/공급/가용인원)를
# 계산해 cause.details(=issue evidence)에 담는다. ontology treatment 는 방향
# ("상향")만 알 뿐 *얼마나* 는 정적으로 모른다 → 여기서 그 숫자로 실효 목표값을
# 계산해 treatment 에 붙인다. 단일축(산술로 증명된) 케이스에서만 성립 —
# 조합(coupled) 케이스는 값 None 을 두고 probe 재solve 검증에 맡긴다.


def _needed_max_nig(need: int, caps: List[int]) -> Optional[int]:
    """월 N 요구(need)를 충족하는 최소 개인 월-야간 상한 m.

    공급 = Σ_n min(working_cap[n], m). m 에 대해 단조증가하므로 최소 m 을 스캔.
    최대 working_cap 로도 need 미달이면 None(= 상한 조정만으로 불가, 증원 필요).
    """
    caps = [int(c) for c in caps if c is not None and int(c) >= 0]
    if need is None or need <= 0 or not caps:
        return None
    max_cap = max(caps)
    for m in range(1, max_cap + 1):
        if sum(min(c, m) for c in caps) >= need:
            return m
    return None


def _size_from_cause_details(
    config_key: Optional[str], details: Optional[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    """config_key + precheck 숫자(details)로 treatment 목표값 계산.

    Returns None 이면 사이징 불가(방향만 유지). insufficient=True 면 그 노브
    단독으로는 해소 불가(예: 인원 부족) → 잘못된 값 대신 명시.
    """
    if not config_key or not isinstance(details, dict):
        return None
    if config_key == "max_nig_per_month":
        need = details.get("n_required") or details.get("monthly_N_need")
        caps = [
            c.get("capacity_days")
            for c in (details.get("night_capable_nurses") or [])
            if isinstance(c, dict)
        ]
        m = _needed_max_nig(int(need) if need else 0, caps)
        n_capable = len([c for c in caps if c is not None])
        if m is None:
            return {
                "config_key": config_key, "target_value": None, "insufficient": True,
                "reason_ko": (
                    f"개인 월 야간 상한을 최대로 올려도 월 요구 {need} 를 채울 수 없습니다 "
                    f"(야간 가능 {n_capable}명). 야간 가능 인원 증원이 필요합니다."
                ),
            }
        return {
            "config_key": config_key, "target_value": m, "insufficient": False,
            "reason_ko": (
                f"야간 가능 {n_capable}명 기준 월 요구 {need} 충족에 필요한 "
                f"개인 월 야간 상한 = {m} (현재값 기준 상향)."
            ),
        }
    if config_key == "daily_shift_requirements":
        # 특정 일자 총 시프트 수요 > 그 날 가용 간호사. 수요는 DailyShift(일자·시프트별
        # 중첩)에서 오므로 스칼라 자동 apply 는 위험 → 정확한 초과 숫자만 안내(message-only).
        req = details.get("required_total") or details.get("required")
        avail = details.get("available_nurses")
        day = details.get("day")
        if req is None or avail is None:
            return None
        over = int(req) - int(avail)
        if over <= 0:
            return None
        _day_ko = f"{int(day) + 1}일차 " if isinstance(day, int) else ""
        return {
            "config_key": config_key, "target_value": None, "insufficient": False,
            "reason_ko": (
                f"{_day_ko}총 시프트 수요 {req} > 가용 간호사 {avail}명 — "
                f"그 날 총 수요를 {avail}명 이하로 {over}명 감축해야 합니다."
            ),
        }
    return None


def _extract_nurse_idx(node_id: Optional[str]) -> Optional[int]:
    """node_id (예: 'off_cap:nurse_5') 에서 nurse 인덱스 정수 추출."""
    if not node_id:
        return None
    m = _NURSE_IDX_RE.search(str(node_id))
    if not m:
        return None
    try:
        return int(m.group(1))
    except (TypeError, ValueError):
        return None


def _resolve_nurse_label_from_idx(
    idx: Optional[int],
    nurse_index_map: Optional[Dict[str, Any]],
) -> Optional[str]:
    """nurse_index_map 에서 idx → '이름(사번)' 라벨 변환."""
    if idx is None or not nurse_index_map:
        return None
    info = (nurse_index_map or {}).get(str(idx)) or (nurse_index_map or {}).get(idx)
    if not isinstance(info, dict):
        return None
    name = info.get("name") or ""
    nid = info.get("nurse_id") or ""
    if name and nid:
        return f"{name}({nid})"
    return name or str(nid) or None


# ── Node type 별 한국어 1줄 renderer ──────────────────────────────────────


def _render_coverage_min(n: Dict[str, Any]) -> str:
    label = n.get("label") or n.get("node_id") or "coverage_min"
    v = n.get("value")
    if v is not None:
        return f"{label} (최소 {v}명)"
    return str(label)


def _render_coverage_max(n: Dict[str, Any]) -> str:
    label = n.get("label") or n.get("node_id") or "coverage_max"
    v = n.get("value")
    if v is not None:
        return f"{label} (최대 {v}명)"
    return str(label)


def _render_grade_min(n: Dict[str, Any]) -> str:
    val = n.get("value")
    if isinstance(val, dict):
        return (
            f"Grade {val.get('grade')} 의 {val.get('shift')} 시프트 "
            f"최소 인원 ≥ {val.get('min')}명"
        )
    return n.get("label") or n.get("node_id") or "grade_min"


def _render_grade_max(n: Dict[str, Any]) -> str:
    val = n.get("value")
    if isinstance(val, dict):
        return (
            f"Grade {val.get('grade')} 의 {val.get('shift')} 시프트 "
            f"최대 인원 ≤ {val.get('max')}명"
        )
    return n.get("label") or n.get("node_id") or "grade_max"


def _render_off_cap(n: Dict[str, Any]) -> str:
    nid = n.get("nurse_id") or ""
    v = n.get("value")
    msg = n.get("human_message_ko") or ""
    if msg:
        return f"간호사 {nid}: {msg}".strip()
    if v is not None:
        return f"간호사 {nid}: OFF 상한 {v}일"
    return f"간호사 {nid}: OFF 상한"


def _render_monthly_n_exact(n: Dict[str, Any]) -> str:
    nid = n.get("nurse_id") or ""
    v = n.get("value")
    return f"간호사 {nid}: 월 N 시프트 정확히 {v}회로 강제됨"


def _render_monthly_off(n: Dict[str, Any]) -> str:
    nid = n.get("nurse_id") or ""
    v = n.get("value")
    return f"간호사 {nid}: 월 OFF 한도 {v}일"


def _render_night_cap(n: Dict[str, Any]) -> str:
    v = n.get("value")
    return f"전역 정책: 월간 N 시프트 상한 {v}일"


def _render_team_min(n: Dict[str, Any]) -> str:
    val = n.get("value")
    if isinstance(val, dict):
        return f"팀 {val.get('team_id')} 의 {val.get('shift')} 시프트 최소 ≥ {val.get('min')}명"
    return n.get("label") or "team_min"


def _render_team_max(n: Dict[str, Any]) -> str:
    val = n.get("value")
    if isinstance(val, dict):
        return f"팀 {val.get('team_id')} 의 {val.get('shift')} 시프트 최대 ≤ {val.get('max')}명"
    return n.get("label") or "team_max"


def _render_nurse(n: Dict[str, Any]) -> str:
    nid = n.get("nurse_id") or n.get("label") or "?"
    return f"간호사 {nid}"


def _render_default(n: Dict[str, Any]) -> str:
    msg = n.get("human_message_ko")
    if msg:
        return msg
    label = n.get("label") or n.get("node_id") or n.get("type") or "?"
    v = n.get("value")
    if v is not None:
        return f"{label}: {v}"
    return str(label)


RENDER_BY_TYPE: Dict[str, Any] = {
    "CoverageMinNode": _render_coverage_min,
    "CoverageMaxNode": _render_coverage_max,
    "GradeMinNode": _render_grade_min,
    "GradeMaxNode": _render_grade_max,
    "OffCapNode": _render_off_cap,
    "MonthlyNExactNode": _render_monthly_n_exact,
    "MonthlyOffNode": _render_monthly_off,
    "NightCapNode": _render_night_cap,
    "TeamMinNode": _render_team_min,
    "TeamMaxNode": _render_team_max,
    "NurseNode": _render_nurse,
    "FixedConstraintNode": _render_default,
    "WantedNode": _render_default,
    "WeeklyOffNode": _render_default,
    "OffWindowNode": _render_default,
    "PrecepteeNode": _render_default,
    "ConstraintNode": _render_default,
}


# ── 간호사 idx → 이름 resolver (optional) ──────────────────────────────


def _resolve_nurse_label(
    n: Dict[str, Any],
    nurse_index_map: Optional[Dict[str, Any]],
) -> Optional[str]:
    """member_sample 노드에서 nurse_id 또는 idx 가 보이면 이름 보강."""
    if not nurse_index_map:
        return None
    nid = n.get("nurse_id")
    if nid:
        for _idx, info in (nurse_index_map or {}).items():
            if str(info.get("nurse_id")) == str(nid):
                name = info.get("name") or ""
                if name:
                    return f"{name}({nid})"
    return None


def render_node(
    n: Dict[str, Any],
    nurse_index_map: Optional[Dict[str, Any]] = None,
) -> str:
    """단일 typed 노드 → 자연문 1줄.

    `간호사 :` 빈자리 보강: node 자체에 nurse_id 가 없으면 node_id (예:
    `off_cap:nurse_5`) 에서 idx 를 추출하고 nurse_index_map 으로 이름+사번 변환.
    nurse_index_map 없으면 적어도 `간호사 idx=5` 형태로 표기.
    """
    ntype = str(n.get("type") or "")
    fn = RENDER_BY_TYPE.get(ntype, _render_default)
    line = fn(n)

    # nurse_id 가 노드에 직접 있으면 nurse_index_map 으로 이름 보강
    nurse_label = _resolve_nurse_label(n, nurse_index_map)
    if nurse_label and "간호사" in line and nurse_label not in line:
        nid = n.get("nurse_id")
        if nid:
            line = line.replace(f"간호사 {nid}", f"간호사 {nurse_label}", 1)

    # nurse_id 없는데 "간호사 :" / "간호사 :" 같은 빈자리 → node_id 에서 idx 추출
    if "간호사 :" in line or "간호사 ?" in line or "간호사  " in line:
        idx = _extract_nurse_idx(n.get("node_id"))
        if idx is not None:
            label = _resolve_nurse_label_from_idx(idx, nurse_index_map)
            replacement = f"간호사 {label}" if label else f"간호사 idx={idx}"
            line = line.replace("간호사 :", f"{replacement}:", 1)
            line = line.replace("간호사 ?", replacement, 1)
            line = line.replace("간호사  ", f"{replacement} ", 1)

    return line


# ── 메인 enrich 함수 ───────────────────────────────────────────────────


def _build_cause_narrative(
    cause: Dict[str, Any],
    nurse_index_map: Optional[Dict[str, Any]] = None,
    limit: int = 5,
) -> str:
    """member_sample 의 typed 노드를 풀어 cause 별 evidence narrative 생성."""
    samples = ((cause.get("details") or {}).get("member_sample")) or []
    lines: List[str] = []
    for s in samples[:limit]:
        try:
            rendered = render_node(s, nurse_index_map)
        except Exception:
            continue
        if rendered:
            lines.append(rendered)
    return " · ".join(lines)


def enrich_causes(
    causes: List[Dict[str, Any]],
    nurse_index_map: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """cause.human_message_ko 의 `{day}/{shift}/{min_sum}` 등 미치환 placeholder 를
    member_sample 기반 evidence narrative 로 대체.

    원본 template 에 `{...}` 가 남아있으면 evidence narrative 로 덮어쓰기.
    placeholder 가 없는 정상 메시지면 evidence 를 suffix 로 추가.

    부작용: cause dict 에 새 필드 추가
        - cause["details"]["evidence_narrative_ko"]: 항상 추가
        - cause["human_message_ko"]: placeholder 있을 시 대체
    """
    for cause in (causes or []):
        if not isinstance(cause, dict):
            continue
        narrative = _build_cause_narrative(cause, nurse_index_map)
        if not narrative:
            continue
        details = cause.get("details")
        if not isinstance(details, dict):
            details = {}
            cause["details"] = details
        details["evidence_narrative_ko"] = narrative

        orig = cause.get("human_message_ko") or ""
        if "{" in orig and "}" in orig:
            # placeholder 미치환 — evidence narrative 로 덮어쓰기
            cause["human_message_ko"] = narrative
        elif orig:
            cause["human_message_ko"] = f"{orig}\n증거: {narrative}"
        else:
            cause["human_message_ko"] = narrative
    return causes


def enrich_treatment_recommendations(
    treatments: List[Dict[str, Any]],
    causes: List[Dict[str, Any]],
    nurse_index_map: Optional[Dict[str, Any]] = None,
    sample_limit_per_treatment: int = 8,
    summary_limit: int = 10,
) -> List[Dict[str, Any]]:
    """treatment_recommendations 의 각 bundle 에 evidence 합성.

    부작용: bundle dict / inner treatment dict 에 새 필드 추가:
        - treatment.specific_evidence: 매핑된 typed 노드 리스트
        - treatment.rationale_ko: "적용 대상: ..." 라인 append
        - bundle.evidence_summary_ko: bundle 단위 요약 (UI 가 한눈에 보여줄)
        - bundle.hard_case_note_ko: manual_investigation bundle 표식

    HardCase (manual_investigation bundle) 는 causes 전체의 member_sample 을
    surface 해 "복합 문제 + evidence" 형태로 표시.
    """
    if not treatments:
        return treatments

    # cause 의 식별자는 `node_id` (예: "cause:eligibility:role_only_oversupply").
    # treatment.covers 가 이 형식이라 node_id 로 매핑. fallback 으로 reason_code 도 등록.
    cause_by_id: Dict[str, Dict[str, Any]] = {}
    # treatment.covers 는 ontology 정식 cause_id(예: "cause:capacity:monthly_night_shortage").
    # precheck cause 는 reason_code alias(예: "MONTHLY_NIGHT_CAPACITY_SHORTAGE")/node_id=None 이라
    # 그대로면 매칭 실패 → resolve_cause_alias 로 정식 id 도 등록해 covers 와 맞춘다.
    _resolve = None
    try:
        from services.semantics.ontology import get_default_ontology
        _resolve = get_default_ontology().resolve_cause_alias
    except Exception:
        _resolve = None
    for c in (causes or []):
        nid = c.get("node_id")
        if nid:
            cause_by_id[nid] = c
        rc = c.get("reason_code")
        if rc and rc not in cause_by_id:
            cause_by_id[rc] = c
        if _resolve:
            for _key in (nid, rc):
                if not _key:
                    continue
                try:
                    _cid = _resolve(_key)
                except Exception:
                    _cid = None
                if _cid and _cid not in cause_by_id:
                    cause_by_id[_cid] = c

    for bundle in treatments:
        if not isinstance(bundle, dict):
            continue
        bundle_evidence_lines: List[str] = []

        for t in (bundle.get("treatments") or []):
            covered = t.get("covers") or []
            t_evidence: List[Dict[str, Any]] = []
            # magnitude sizing: 단일축 precheck 숫자로 실효 목표값 계산(가능한 경우).
            # 가장 먼저 사이징 가능한 cause 를 채택. 조합/미지원이면 None(방향만 유지).
            if t.get("suggested_value") is None and not t.get("sizing_insufficient"):
                for cause_id in covered:
                    cause = cause_by_id.get(cause_id)
                    if not cause:
                        continue
                    sized = _size_from_cause_details(
                        t.get("config_key"), cause.get("details")
                    )
                    if sized is None:
                        continue
                    t["sizing_ko"] = sized.get("reason_ko")
                    if sized.get("insufficient"):
                        t["sizing_insufficient"] = True
                    elif sized.get("target_value") is not None:
                        t["suggested_value"] = sized.get("target_value")
                    break
            for cause_id in covered:
                cause = cause_by_id.get(cause_id)
                if not cause:
                    continue
                samples = ((cause.get("details") or {}).get("member_sample")) or []
                for s in samples:
                    try:
                        rendered = render_node(s, nurse_index_map)
                    except Exception:
                        rendered = str(s.get("label") or s.get("node_id") or "?")
                    if not rendered:
                        continue
                    t_evidence.append({
                        "from_cause": cause_id,
                        "node_type": s.get("type"),
                        "node_id": s.get("node_id"),
                        "rendered_ko": rendered,
                        "raw_value": s.get("value"),
                    })

            if t_evidence:
                t["specific_evidence"] = t_evidence[:sample_limit_per_treatment]
                head_lines = [e["rendered_ko"] for e in t_evidence[:3]]
                head = " / ".join(head_lines)
                base = (t.get("rationale_ko") or "").rstrip()
                # idempotent guard — 두 번 호출돼도 "적용 대상" 라인이 중복 append 되지 않음
                if "적용 대상:" not in base:
                    t["rationale_ko"] = (
                        f"{base}\n적용 대상: {head}"
                        if base else f"적용 대상: {head}"
                    )
                bundle_evidence_lines.extend(head_lines)

        # HardCase: manual_investigation bundle 에는 causes 전체 evidence surface
        bundle_id = str(bundle.get("bundle_id") or "")
        is_hard_case = "manual_investigation" in bundle_id
        if is_hard_case:
            extra_lines: List[str] = []
            for cause in (causes or []):
                samples = ((cause.get("details") or {}).get("member_sample")) or []
                for s in samples[:3]:
                    try:
                        extra_lines.append(render_node(s, nurse_index_map))
                    except Exception:
                        continue
            bundle_evidence_lines.extend(extra_lines)
            bundle["hard_case_note_ko"] = (
                "복합 문제로 자동 처방이 불가능합니다 — 여러 도메인(cap·grade·"
                "eligibility 등) 이 동시에 충돌해 단일 조치로 해소되지 않습니다. "
                "아래 evidence 를 참고해 운영자 manual 검토가 필요합니다."
            )

        if bundle_evidence_lines:
            # dedup + 길이 제한
            seen = set()
            dedup: List[str] = []
            for line in bundle_evidence_lines:
                if line in seen:
                    continue
                seen.add(line)
                dedup.append(line)
            bundle["evidence_summary_ko"] = dedup[:summary_limit]

    return treatments
