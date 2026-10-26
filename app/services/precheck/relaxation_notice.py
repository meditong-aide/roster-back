"""자동 완화 안내 문구 — 사다리가 풀고 간 규칙의 '원인 → 할 수 있는 조정' 과 실제로 넘은 곳(EI-14c).

사다리(`roster_create_service`)가 규칙을 풀고 성공했을 때 `weekly_off_warnings` 의 **기존 항목**
(`*_applied`)에 쉬운 말 문구와 근거를 더한다. 새 항목을 만들지 않고 `type` 도 바꾸지 않는다
(이 칸 개수는 Slack 품질 요약이 센다 — EI-11 D39). 기존 `detail` 은 한 글자도 안 바꾸고 뒤에 잇는다.

실패 계약: 이 모듈은 DB·env·print 에 닿지 않고 어떤 입력에도 예외를 내지 않는다. 근거를 못 만들면
기존 알림 원문 그대로 돌려준다(가짜 엔진 결과로 도는 사다리 시험·주 골든의 `logs`·`swallowed_failures`
불변을 지키기 위해서다). 근거는 **최종 배정에서 실제로 센 곳**이다(엔진이 제약을 건 범위와 다를 수 있다).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: kind — 알림 종류.
KINDS = ("n2n_released", "n2n_partial", "same_shift", "isolated_work")

MAX_EVIDENCE = 20  # 응답 크기 상한(간호사 단위 항목)
MAX_EXAMPLES = 3   # 문장에 풀어 쓰는 예시 수


def build_relaxation_notice(kind: str, base: dict, *, roster_system, nurses, year: int, month: int,
                            param: int | None) -> dict:
    """기존 알림 `base`({"type","detail"})에 원인·근거·조정을 더한 새 dict. 실패하면 `dict(base)`."""
    try:
        built = _build(kind, roster_system, nurses, month, param)
        if built is None:
            return dict(base)
        cause, found, adjust, evidence = built
        parts = [str(base.get("detail") or ""), cause, found]
        if adjust:
            parts.append(adjust)
        out = dict(base)
        out["detail"] = " ".join(p for p in parts if p)
        out["cause_ko"] = cause
        if adjust:
            out["adjust_ko"] = adjust
        out["evidence"] = evidence[:MAX_EVIDENCE]
        return out
    except Exception as exc:  # noqa: BLE001 — 안내는 보조 정보라 어떤 실패도 생성을 막지 않는다
        logger.debug("relaxation_notice %s 실패(원문 유지): %s", kind, exc)
        return dict(base)


# ── 내부 ─────────────────────────────────────────────────────────────

def _build(kind, roster_system, nurses, month, param):
    """(원인 문장, 근거 문장, 조정 문장 또는 '', evidence 목록) 또는 None."""
    roster = getattr(roster_system, "roster", None)
    shift_types = getattr(getattr(roster_system, "config", None), "shift_types", None)
    if roster is None or not shift_types:
        return None
    shift_types = list(shift_types)
    cells = _ctx_nurses(roster_system, nurses, roster)
    if not cells:
        return None
    if kind in ("n2n_released", "n2n_partial"):
        return _n2n(kind, roster_system, roster, shift_types, cells, month, param)
    if kind == "same_shift":
        return _same_shift(roster_system, roster, shift_types, cells, month, param)
    if kind == "isolated_work":
        return _isolated_work(roster, shift_types, cells, month)
    return None


def _ctx_nurses(roster_system, nurses, roster):
    """간호사 색인 → 객체. 엔진 결과의 간호사를 먼저, 없으면 인자."""
    rs_n = list(getattr(roster_system, "nurses", None) or [])
    src = rs_n if rs_n else list(nurses or [])
    n_rows = int(roster.shape[0])
    return src[:n_rows] if src else []


def _who(nurse, n: int) -> tuple[str, str]:
    # 엔진 Nurse(`db/nurse_config.py`)는 `id`=엔진 색인, `db_id`=DB id 라 db_id 를 먼저 본다.
    nid = str(getattr(nurse, "db_id", None) or getattr(nurse, "nurse_id", None) or getattr(nurse, "id", None) or n)
    name = str(getattr(nurse, "name", None) or nid)
    return nid, name


def _date(month: int, day0: int) -> str:
    return f"{int(month)}/{int(day0) + 1}"


def _example_cells(groups: list[tuple[str, list[int]]], month: int, total: int) -> str:
    """groups = [(이름, [0기준 일…])] 앞 3명(각 3일까지)을 '이름 m/d·m/d' 로, 못 보인 칸은 '외 N곳'."""
    shown_n = 0
    texts = []
    for name, days in groups[:MAX_EXAMPLES]:
        ds = days[:MAX_EXAMPLES]
        shown_n += len(ds)
        texts.append(f"{name} " + "·".join(_date(month, d) for d in ds))
    rest = total - shown_n
    text = ", ".join(texts)
    return f"{text} 외 {rest}곳" if rest > 0 else text


def _n2n_partial_cells(roster_system, roster):
    """부분 알림 — 엔진 금지쌍(`_n2n_pair_idx`)으로 `count_n2n_gap_violations` 와 같은 식으로 센다."""
    pairs = getattr(roster_system, "_n2n_pair_idx", None)
    if not pairs:
        return None
    ni = int(roster_system._n2n_night_idx)
    out = [(int(n), int(dk)) for n, dk, conds in pairs
           if roster[n, dk, ni] and all(int(roster[n, d, ni]) == e for d, e in conds)]
    return sorted(out)


def _n2n_released_cells(roster_system, roster, ni, gap, nurses_list):
    """해제 알림 — 간격 0 으로 다시 푼 결과라 금지쌍이 없다. 최종 배정에서 월 안 위반을 직접 센다
    (월경계는 근거에 넣지 않는다). N전담은 엔진이 제외하므로 근거에서도 뺀다."""
    from services.cp_sat.allowed_shift_types import is_n_only_profile  # 지연 import(읽기만)
    use_mid = bool(getattr(getattr(roster_system, "config", None), "use_mid", False))
    days = int(roster.shape[1])
    out: list[tuple[int, int]] = []
    for n, nurse in enumerate(nurses_list):
        if is_n_only_profile(getattr(nurse, "allowed_shifts", None), use_mid=use_mid):
            continue
        for d in range(days):
            if not roster[n, d, ni] or (d + 1 < days and roster[n, d + 1, ni]):
                continue  # 나이트가 아니거나 블록 끝이 아니다
            for k in range(2, gap):
                if d + k >= days:
                    break
                if roster[n, d + k, ni]:
                    out.append((n, d + k))
    return sorted(out)


def _n2n(kind, roster_system, roster, shift_types, nurses_list, month, param):
    from services.cp_sat.fallback_lex import effective_n2n_min_gap  # 지연 import(읽기만) — env 주입값까지 엔진과 같게
    gap = effective_n2n_min_gap(param)
    if "N" not in shift_types:
        return None
    if kind == "n2n_partial":
        viol = _n2n_partial_cells(roster_system, roster)
        if viol is None:
            return None
        cause = (f"나이트와 나이트 사이를 {gap}일 이상 띄우는 규칙을 모두 지킬 수는 없었어요."
                 if gap else "나이트와 나이트 사이 최소 간격 규칙을 모두 지킬 수는 없었어요.")
    else:
        viol = _n2n_released_cells(roster_system, roster, shift_types.index("N"), gap, nurses_list)
        cause = (f"나이트와 나이트 사이를 {gap}일 이상 띄우는 규칙을 다른 조건(필요 인원·확정 근무 등)과 "
                 "함께 지킬 수 없어, 이 규칙을 풀고 근무표를 만들었어요.")
    evidence: list[dict] = []
    groups: list[tuple[str, list[int]]] = []
    by_nurse: dict[int, list[int]] = {}
    for n, dk in viol:
        by_nurse.setdefault(n, []).append(dk)
    for n in sorted(by_nurse):
        nid, name = _who(nurses_list[n], n) if n < len(nurses_list) else (str(n), str(n))
        ds = sorted(set(by_nurse[n]))
        groups.append((name, ds))
        evidence.append({"nurse_id": nid, "name": name, "days": [int(d) + 1 for d in ds],
                         "detail_ko": "나이트 간격이 짧아진 날: " + ", ".join(_date(month, d) for d in ds)})
    if not viol:
        return cause, "실제로 간격이 짧아진 곳은 없었어요.", "", []
    found = "실제로 간격이 짧아진 곳: " + _example_cells(groups, month, len(viol)) + "."
    adjust = ("간격을 지킨 채 만들려면 나이트를 설 수 있는 인원을 늘리거나, 그 날짜 앞뒤의 확정 근무·휴무 신청을 "
              "조정해 주세요.")
    return cause, found, adjust, evidence


def _same_shift(roster_system, roster, shift_types, nurses_list, month, param):
    """같은 근무(D·E·N)가 K+1일 이상 이어진 구간 — 월 안만, 단일 시프트 전담은 엔진처럼 제외."""
    from services.cp_sat.allowed_shift_types import normalize_allowed_shift_codes  # 지연 import(읽기만)
    k = int(param or 0)
    if k <= 0:
        return None
    use_mid = bool(getattr(getattr(roster_system, "config", None), "use_mid", False))
    days = int(roster.shape[1])
    segs: list[tuple[int, str, int, int]] = []  # (간호사 색인, 코드, 시작 0기준, 끝 0기준)
    for n, nurse in enumerate(nurses_list):
        for code in ("D", "E", "N"):
            if code not in shift_types:
                continue
            allowed = normalize_allowed_shift_codes(getattr(nurse, "allowed_shifts", None), use_mid=use_mid)
            if allowed and allowed == {code}:
                continue
            si = shift_types.index(code)
            d = 0
            while d < days:
                if not roster[n, d, si]:
                    d += 1
                    continue
                e = d
                while e + 1 < days and roster[n, e + 1, si]:
                    e += 1
                if e - d + 1 >= k + 1:
                    segs.append((n, code, d, e))
                d = e + 1
    segs.sort(key=lambda t: (t[0], t[2], t[1]))
    cause = (f"같은 근무를 {k}일까지만 이어 배정하는 규칙을 다른 조건(필요 인원·확정 근무 등)과 "
             "함께 지킬 수 없어, 이 규칙을 풀고 근무표를 만들었어요.")
    if not segs:
        return cause, "실제로 규칙을 넘은 곳은 없었어요.", "", []
    evidence = []
    first_by_nurse: dict[int, str] = {}
    for n, code, d, e in segs:
        nid, name = _who(nurses_list[n], n)
        label = f"{name} {_date(month, d)}~{_date(month, e)}({code} {e - d + 1}일)"
        first_by_nurse.setdefault(n, label)
        evidence.append({"nurse_id": nid, "name": name, "days": [x + 1 for x in range(d, e + 1)],
                         "detail_ko": f"{code} {e - d + 1}일 연속"})
    shown = list(first_by_nurse.values())[:MAX_EXAMPLES]
    rest = len(first_by_nurse) - len(shown)
    text = ", ".join(shown) + (f" 외 {rest}명" if rest > 0 else "")
    found = f"실제로 {k + 1}일 이상 이어진 곳: {text}."
    adjust = ("규칙을 지킨 채 만들려면 해당 간호사의 확정 근무·원티드 날짜를 나누어 주거나, "
              "그 근무의 필요 인원을 줄여 주세요.")
    return cause, found, adjust, evidence


def _isolated_work(roster, shift_types, nurses_list, month):
    """쉼-근무-쉼 — 가운데가 O·N·W 가 아닌 근무(엔진 `_ih_mid` 와 같은 기준). 월 첫날·말일은 대상 밖."""
    if "O" not in shift_types:
        return None
    off = shift_types.index("O")
    skip = {off} | {shift_types.index(c) for c in ("N", "W") if c in shift_types}
    days = int(roster.shape[1])
    found: list[tuple[int, int, str]] = []  # (간호사 색인, 0기준 일, 코드)
    for n in range(len(nurses_list)):
        for d in range(1, days - 1):
            if not (roster[n, d - 1, off] and roster[n, d + 1, off]):
                continue
            if roster[n, d, off]:
                continue
            code = next((shift_types[si] for si in range(len(shift_types))
                         if si not in skip and roster[n, d, si]), None)
            if code is not None:
                found.append((n, d, code))
    cause = ("쉬는 날 사이에 하루만 끼는 근무(쉼-근무-쉼)를 막는 규칙을 다른 조건과 함께 지킬 수 없어, "
             "이 규칙을 풀고 근무표를 만들었어요.")
    if not found:
        return cause, "실제로 생긴 곳은 없었어요.", "", []
    evidence = []
    for n, d, code in found:
        nid, name = _who(nurses_list[n], n)
        evidence.append({"nurse_id": nid, "name": name, "days": [d + 1], "detail_ko": f"{code} 하루만 근무"})
    shown = [f"{_who(nurses_list[n], n)[1]} {_date(month, d)}({code})" for n, d, code in found[:MAX_EXAMPLES]]
    rest = len(found) - len(shown)
    text = ", ".join(shown) + (f" 외 {rest}곳" if rest > 0 else "")
    adjust = "그 날 앞뒤의 휴무나 확정 근무를 조정해 근무가 이틀 이상 이어지게 하면 규칙을 지킨 채 만들 수 있어요."
    return cause, f"실제로 생긴 곳: {text}.", adjust, evidence
