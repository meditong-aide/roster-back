# app/utils/roster_quality.py
"""생성된 근무표(roster_data)에서 하네스 스타일 품질 지표를 추출/계산한다.

설계:
    - 의존성 zero (표준 라이브러리만). worker.py 의 stdlib-only/lazy 정책 호환.
    - tools/harness 를 import 하지 않는다. 하네스는 API payload 형태를 가정하고
      무거운 의존성을 끌어오므로 Lambda 워커에 부적합. 대신 다음 두 출처를 합친다:
        1) 엔진이 이미 roster_data 에 계산해 둔 권위 지표
           (coverage_gaps=shortage, hard_violation_count, severity, applied_relaxations)
        2) nurses[].schedule 에서 직접 계산하는 공정성(y축) / N 블록 퀄리티
    - best-effort: 어떤 섹션이 깨져도 예외를 전파하지 않고 가능한 부분만 채운다.

용어:
    - y축(공정성): 간호사 간 근무량/야간 쏠림. 하네스 C_* 규칙 대응.
    - N 블록: 야간 연속/회복(2N→2OFF) 패턴. 하네스 A_* 규칙 대응.

심볼:
    schedule 배열은 raw shift_id("D1","E1","N","O","-")를 담으므로 main code 로 정규화한다.
    OFF="O", 야간="N", 근무={D,E,N,M,W}, "-"=미배정/패딩(런을 끊되 OFF로 세지 않음).

하네스 임계값(초과 시 ⚠️):
    fairness.total_work_spread   <= 4
    fairness.n_shift_skew_ratio  <= 0.35
    fairness.den_spread_ratio    <= 0.25
"""
from __future__ import annotations

WORK_CODES = {"D", "E", "N", "M", "W"}

# 하네스 checklist_core.yaml 기준 임계값(N 쏠림 경고)
TH_N_SKEW_RATIO = 0.35


def _main_code(raw) -> str:
    """raw shift_id 를 main code(D/E/N/M/W/O/'-')로 정규화."""
    s = str(raw or "").strip().upper()
    if s in ("", "-"):
        return "-"
    if s in ("O", "OFF", "주", "휴", "휴가", "공가"):
        return "O"
    if s.startswith("N"):
        return "N"
    if s.startswith("D"):
        return "D"
    if s.startswith("E"):
        return "E"
    if s.startswith("M"):
        return "M"
    if s.startswith("W"):
        return "W"
    return "?"  # 알 수 없는 근무성 코드


def _night_runs(seq: list[str]) -> list[tuple[int, int]]:
    """연속 N 런을 (start_idx, length) 리스트로 반환."""
    runs, i, n = [], 0, len(seq)
    while i < n:
        if seq[i] == "N":
            j = i
            while j < n and seq[j] == "N":
                j += 1
            runs.append((i, j - i))
            i = j
        else:
            i += 1
    return runs


def _trailing_off(seq: list[str], start: int) -> int:
    """start 인덱스부터 연속 OFF('O') 개수."""
    cnt = 0
    for k in range(start, len(seq)):
        if seq[k] == "O":
            cnt += 1
        else:
            break
    return cnt


def _n_block_quality(norm_schedules: list[list[str]]) -> dict:
    """N 블록(연속야간) 회복/overflow — 온톨로지 꺼졌을 때의 fallback 전용."""
    m = {
        "recovery_2n2o_violation": 0,
        "recovery_3n2o_violation": 0,
        "max_consec_night_overflow": 0,
    }
    for seq in norm_schedules:
        for start, length in _night_runs(seq):
            if length >= 4:
                m["max_consec_night_overflow"] += 1
            after = start + length
            # 월말에 걸린 런은 다음 달 연계 사안이므로 회복 판정에서 제외
            if after < len(seq):
                off_after = _trailing_off(seq, after)
                if length == 2 and off_after < 2:
                    m["recovery_2n2o_violation"] += 1
                elif length >= 3 and off_after < 2:
                    m["recovery_3n2o_violation"] += 1
    return m


def _run_len1_count(seq: list[str], member) -> int:
    """seq 에서 member(코드 또는 코드집합) 런의 길이가 정확히 1인 횟수.

    하네스 single_e_run / isolated_work 와 동일 의미(run length == 1).
    """
    is_in = (lambda c: c in member) if isinstance(member, (set, frozenset, tuple)) else (lambda c: c == member)
    cnt, k, L = 0, 0, len(seq)
    while k < L:
        if is_in(seq[k]):
            j = k
            while j < L and is_in(seq[j]):
                j += 1
            if j - k == 1:
                cnt += 1
            k = j
        else:
            k += 1
    return cnt


def _pattern_counts(norm_schedules: list[list[str]]) -> dict:
    """그리드 패턴 개수(하네스 정의 그대로):

    - single_e_count       : E 런 길이 1 (미페어 E / "단일E", 하네스 single_e_run_count)
    - single_shift_count   : 근무 런 길이 1 (퐁당퐁당, 하네스 isolated_work_count)
    - single_n_month_count : 월 N 총합이 1 (단독 N, 하네스 not_one_night_single_n_count)
    """
    m = {"single_e_count": 0, "single_shift_count": 0, "single_n_month_count": 0}
    for seq in norm_schedules:
        if sum(1 for c in seq if c == "N") == 1:
            m["single_n_month_count"] += 1
        m["single_e_count"] += _run_len1_count(seq, "E")
        m["single_shift_count"] += _run_len1_count(seq, WORK_CODES)
    return m


def _fairness(norm_schedules: list[list[str]]) -> dict:
    """x축(개인별 D·E) / y축(개인별 N·O) 분포 — 하네스 fairness 스타일(스프레드 = max-min)."""
    rows: list[dict] = []
    for seq in norm_schedules:
        # 전부 미배정('-')인 행(비활성/타그룹 등)은 평가에서 제외
        if all(c == "-" for c in seq):
            continue
        rows.append({
            "D": sum(1 for c in seq if c == "D"),
            "E": sum(1 for c in seq if c == "E"),
            "N": sum(1 for c in seq if c == "N"),
            "O": sum(1 for c in seq if c == "O"),
            "W": sum(1 for c in seq if c in WORK_CODES),
        })

    def spread(key: str) -> int:
        vals = [r[key] for r in rows]
        return (max(vals) - min(vals)) if vals else 0

    out = {
        "eval_nurse_count": len(rows),
        "D_spread": spread("D"),
        "E_spread": spread("E"),
        "N_spread": spread("N"),
        "O_spread": spread("O"),
        "total_work_spread": spread("W"),
        "n_shift_skew_ratio": 0.0,
    }
    ns = [r["N"] for r in rows]
    total_n = sum(ns)
    if total_n > 0:
        out["n_shift_skew_ratio"] = round(max(ns) / total_n, 3)
    return out


def _coverage(roster_data: dict) -> dict:
    """엔진이 계산한 coverage_gaps(shortage)를 시프트별로 집계."""
    ci = roster_data.get("constraint_impact") or {}
    gaps = ci.get("coverage_gaps") or []
    by_shift: dict[str, int] = {}
    worst: list[dict] = []
    for g in gaps:
        sh = _main_code(g.get("shift"))
        short = int(g.get("short") or 0)
        if short <= 0:
            continue
        by_shift[sh] = by_shift.get(sh, 0) + short
        worst.append(g)
    worst.sort(key=lambda g: int(g.get("short") or 0), reverse=True)
    return {
        "under_by_shift": by_shift,
        "under_total": sum(by_shift.values()),
        "gap_cell_count": len([g for g in gaps if int(g.get("short") or 0) > 0]),
        "hard_violation_count": int(ci.get("hard_violation_count") or 0),
        "worst": worst[:3],
    }


# ---------------------------------------------------------------------------
# 온톨로지(constraint_impact) 기반 하드 위반 해석
#
# violated_constraints 의 node_id 는 자기설명적 taxonomy 다.
# prefix 가 곧 위반 family → 한글 라벨로 매핑한다. coverage:min 은 이미
# coverage_gaps 로 별도 보고하므로 여기선 제외(중복 방지).
# ---------------------------------------------------------------------------
_FAMILY_LABELS: list[tuple[str, str | None]] = [
    ("coverage:min", None),                 # coverage_gaps 로 별도 보고 → 스킵
    ("coverage:max", "커버리지 초과"),
    ("team_min", "팀 최소인원 부족"),
    ("grade_min", "등급 최소인원 부족"),
    ("preceptee_sync", "프리셉티 동기화 위반"),
    ("consecutive_work", "연속근무 초과"),
    ("consecutive_night", "연속야간 초과"),
    ("monthly_night_cap", "월 야간상한 초과"),
    ("transition_ban:n_to_d", "금지전이 N→D"),
    ("transition_ban:e_to_d", "금지전이 E→D"),
    ("transition_ban:n_to_e", "금지전이 N→E"),
    ("recovery_debt", "야간 회복 위반(N→OFF)"),
    ("fatigue", "피로 위험"),
]


def _family_label(node_id: str) -> tuple[str, str | None]:
    """node_id prefix → (family_key, 한글 라벨). 라벨 None 이면 보고 제외."""
    nid = str(node_id or "")
    for prefix, label in _FAMILY_LABELS:  # 구체적 prefix 우선(리스트 순서 유지)
        if nid.startswith(prefix):
            return prefix, label
    return (nid.split(":", 1)[0] or "기타", nid.split(":", 1)[0] or "기타")


def _fmt_example(family: str, details: dict) -> str:
    """family 별 details 를 짧은 예시 문자열로."""
    d = details or {}
    day = d.get("day")
    if family == "consecutive_night":
        return f"day{day} 연속{d.get('consecutive_nights')}/한도{d.get('limit')}"
    if family == "consecutive_work":
        return f"day{day} 연속{d.get('consecutive_work')}/한도{d.get('limit')}"
    if family == "monthly_night_cap":
        return f"N{d.get('assigned_nights')}/한도{d.get('limit')}"
    if family.startswith("transition_ban"):
        return f"day{day} {d.get('transition') or ''}".strip()
    if family in ("team_min", "grade_min"):
        return f"day{day} {d.get('shift')} 필요{d.get('need')}/배치{d.get('assigned')}"
    if family == "coverage:max":
        return f"day{day} {d.get('shift')} 상한{d.get('max_need')}/배치{d.get('assigned')}"
    return f"day{day}" if day is not None else ""


def _ontology_violations(roster_data: dict) -> dict:
    """constraint_impact.violated_constraints 를 family 별로 집계.

    반환:
        available: 온톨로지 분석이 수행됐는지(enabled). False 면 스케줄 fallback 사용.
        by_family: {라벨: count}
        examples:  {라벨: "예시 문자열"}
        total:     집계 대상 위반 수(coverage:min 제외)
    """
    ci = roster_data.get("constraint_impact") or {}
    available = bool(ci.get("enabled"))
    vlist = ci.get("violated_constraints") or []
    by_family: dict[str, int] = {}
    examples: dict[str, str] = {}
    for v in vlist:
        family, label = _family_label(v.get("node_id"))
        if label is None:  # coverage:min → 별도 보고
            continue
        by_family[label] = by_family.get(label, 0) + 1
        if label not in examples:
            ex = _fmt_example(family, v.get("details") or {})
            if ex:
                examples[label] = ex
    return {
        "available": available,
        "by_family": by_family,
        "examples": examples,
        "total": sum(by_family.values()),
    }


def summarize(roster_data: dict) -> dict:
    """roster_data → {"metrics": {...}, "lines": [슬랙용 한글 라인...]}.

    절대 예외를 raise 하지 않는다. 깨진 섹션은 건너뛰고 가능한 만큼 채운다.
    """
    metrics: dict = {}
    lines: list[str] = []
    if not isinstance(roster_data, dict):
        return {"metrics": metrics, "lines": lines}

    # 0) severity / 완화
    try:
        infeas = roster_data.get("infeasibility") or {}
        severity = infeas.get("severity")
        relaxations = infeas.get("applied_relaxations") or []
        metrics["severity"] = severity
        metrics["applied_relaxations"] = list(relaxations)
        sev_mark = "🟢" if severity in (None, "ok") else "🟡"
        lines.append(f"📊 품질 요약  {sev_mark} severity={severity or 'ok'}")
        if relaxations:
            lines.append(f"• 적용된 완화: {', '.join(map(str, relaxations))}")
    except Exception:
        pass

    # 1) 커버리지(shortage) + 하드 위반 — 엔진 권위 지표
    try:
        cov = _coverage(roster_data)
        metrics["coverage"] = {k: v for k, v in cov.items() if k != "worst"}
        if cov["under_total"] > 0:
            parts = " · ".join(f"{sh} -{n}" for sh, n in sorted(cov["under_by_shift"].items()))
            lines.append(f"• 🔴 커버리지 부족: {parts}  (부족셀 {cov['gap_cell_count']}개)")
            for g in cov["worst"]:
                lines.append(
                    f"   └ day{g.get('day')} {_main_code(g.get('shift'))} "
                    f"필요{g.get('need')}/배치{g.get('assigned')} ({-int(g.get('short') or 0)})"
                )
        else:
            lines.append("• 🟢 커버리지: 부족 없음")
    except Exception:
        pass

    # 2) 정규화 스케줄 준비
    norm_schedules: list[list[str]] = []
    try:
        for nu in roster_data.get("nurses") or []:
            sched = nu.get("schedule") or []
            norm_schedules.append([_main_code(c) for c in sched])
    except Exception:
        norm_schedules = []

    # 3) 하드 위반 — 온톨로지(constraint_impact) 우선, 꺼져있으면 스케줄 추정 fallback
    try:
        onto = _ontology_violations(roster_data)
        metrics["ontology"] = {
            "available": onto["available"],
            "by_family": onto["by_family"],
            "total": onto["total"],
        }
        if onto["available"]:
            # 온톨로지 분석 수행됨 → 권위 있는 하드 위반 내역
            if onto["total"] == 0:
                lines.append("• 🟢 하드 위반(온톨로지): 없음")
            else:
                lines.append(f"• 🔴 하드 위반(온톨로지) 총 {onto['total']}건")
                for label, cnt in sorted(onto["by_family"].items(), key=lambda kv: -kv[1]):
                    ex = onto["examples"].get(label)
                    lines.append(f"   └ {label}: {cnt}건" + (f"  (예: {ex})" if ex else ""))
        else:
            # constraint_impact 비활성 → 스케줄 시퀀스로 N 블록 추정
            nb = _n_block_quality(norm_schedules)
            metrics["n_block"] = nb
            nb_flags = []
            if nb["recovery_2n2o_violation"]:
                nb_flags.append(f"2N→2OFF위반 {nb['recovery_2n2o_violation']}")
            if nb["recovery_3n2o_violation"]:
                nb_flags.append(f"3N+→2OFF위반 {nb['recovery_3n2o_violation']}")
            if nb["max_consec_night_overflow"]:
                nb_flags.append(f"4N+연속 {nb['max_consec_night_overflow']}")
            mark = "  🟡" if nb_flags else ""
            lines.append(
                "• N 블록(추정): " + (" · ".join(nb_flags) if nb_flags else "위반 없음") + mark
            )
    except Exception:
        pass

    # 4) x축(개인별 D·E) / y축(개인별 N·O) 분포 — 항상 스케줄 계산(온톨로지 비모델링)
    try:
        fr = _fairness(norm_schedules)
        metrics["fairness"] = fr
        skew_warn = "⚠️" if fr["n_shift_skew_ratio"] > TH_N_SKEW_RATIO else ""
        lines.append(
            f"• x축(개인 D·E): D 스프레드 {fr['D_spread']} · E 스프레드 {fr['E_spread']}"
        )
        lines.append(
            f"• y축(개인 N·O): N 스프레드 {fr['N_spread']}(쏠림 {fr['n_shift_skew_ratio']}{skew_warn}) · "
            f"O 스프레드 {fr['O_spread']}"
        )
    except Exception:
        pass

    # 4b) 그리드 패턴 개수 — 항상 스케줄 계산
    try:
        pc = _pattern_counts(norm_schedules)
        metrics["patterns"] = pc
        pmark = "  🟡" if (pc["single_e_count"] or pc["single_shift_count"] or pc["single_n_month_count"]) else ""
        lines.append(
            f"• 패턴: 단일E {pc['single_e_count']} · 퐁당퐁당(단일근무) {pc['single_shift_count']} · "
            f"단독N {pc['single_n_month_count']}" + pmark
        )
    except Exception:
        pass

    # 5) 주말/주간 OFF 충돌(엔진 산출)
    try:
        conflicts = roster_data.get("weekly_off_conflicts") or []
        warnings = roster_data.get("weekly_off_warnings") or []
        if conflicts or warnings:
            metrics["weekly_off"] = {"conflicts": len(conflicts), "warnings": len(warnings)}
            lines.append(f"• 주간 OFF: 충돌 {len(conflicts)} · 경고 {len(warnings)}")
    except Exception:
        pass

    return {"metrics": metrics, "lines": lines}
