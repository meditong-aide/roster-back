"""가로축(근무자 한 줄) 분배 상태 — 생성 엔진이 마지막에 맞추는 균등 지표를 그대로 잰다.

엔진 기본 lex 순서(fallback_lex `LEX_PASS_ORDER_DEFAULT`) 중 사람 간 분배를 보는 셋:
  · off_range — 사람 간 OFF 수 격차(max-min). 신청 휴가·공가 칸은 세지 않는다(FL:4792).
  · n_range   — 사람 간 N 수 격차. N 이 구조적으로 고정인 사람은 뺀다(FL:4847).
  · de        — 사람마다 |D-E| 가 허용폭(기본 2)을 넘는 만큼의 합(FL:5020 · 'D/E per-nurse 균등(X축)').
격차(range) 두 지표는 엔진처럼 N 전담·주말휴무·그 달 일부만 근무하는 사람을 뺀다(FL:4669).

수정 모델에서는 이 셋이 **지금보다 나빠진 만큼**을 품질 단계 맨 앞에 둔다(사용자 지시 11-02:
근무표 스냅샷의 일자별 상태·근무자 가로축 분배 상태를 고려).
"""

from __future__ import annotations

from typing import Dict, List

from services.schedule_edit.context import EditContext

DE_TOLERANCE_DEFAULT = 2
#: 바꿀 수 없는 칸이 있어 '그 달 전체 근무자'로 보지 않는 이유들(엔진의 부분근무 제외와 같은 축)
_PARTIAL_REASONS = {"관할 밖(파견·이동·휴직·프리셉티)", "근무표에 칸 없음", "프리셉티 동행"}


def range_members(ctx: EditContext) -> List[str]:
    """격차 지표 대상 — N 전담·주말휴무·고정근무자·그 달 일부만 근무하는 사람을 뺀다."""
    out = []
    for nid in ctx.nurse_ids:
        if nid in ctx.n_only or nid in ctx.weekend_off:
            continue
        reasons = {ctx.frozen.get((nid, d)) for d in range(ctx.days)}
        if "고정근무자" in reasons or reasons & _PARTIAL_REASONS:
            continue
        if all((nid, d) in ctx.vacation for d in range(ctx.days)):
            continue   # 한 달 내내 휴가 — OFF 0 상수가 min 을 묶는다(FL:4800 과 같이 뺀다)
        out.append(nid)
    return out


def night_range_members(ctx: EditContext) -> List[str]:
    """N 격차 대상 — 격차 대상 중 N 을 아예 못 하는 사람(모든 날 N 금지)은 뺀다."""
    return [n for n in range_members(ctx)
            if not all("N" in ctx.forbidden.get((n, d), set()) for d in range(ctx.days))]


def de_members(ctx: EditContext) -> List[str]:
    """D/E 균형 대상 — N 전담과 D·E 중 한쪽만 하는 사람은 뺀다(줄일 방법이 없다)."""
    return [n for n in ctx.nurse_ids
            if n not in ctx.n_only and ctx.single_shift.get(n) not in ("D", "E")]


def de_tolerance(ctx: EditContext) -> int:
    """0 도 유효한 값이다(엔진 `getattr(cfg, …, 2)` 와 같게) — 비었을 때만 기본값."""
    value = ctx.config.get("de_balance_tolerance")
    return DE_TOLERANCE_DEFAULT if value is None else int(value)


def de_enabled(ctx: EditContext) -> bool:
    """엔진 `_prep_de` 처럼 설정이 꺼진 병동은 D/E 균형을 보지 않는다(FL:5022)."""
    return bool(ctx.config.get("de_balance_enable", True))


def off_count(ctx: EditContext, nid: str, row: List) -> int:
    return sum(1 for d, m in enumerate(row) if m == "O" and (nid, d) not in ctx.vacation)


def metrics(ctx: EditContext, grid: Dict[str, List]) -> Dict[str, int]:
    """세 지표 값. 대상이 2명 미만이면 격차는 0."""
    members = range_members(ctx)
    offs = [off_count(ctx, n, grid[n]) for n in members]
    nights = [sum(1 for m in grid[n] if m == "N") for n in night_range_members(ctx)]
    tol = de_tolerance(ctx)
    de = sum(max(0, abs(sum(1 for m in grid[n] if m == "D") - sum(1 for m in grid[n] if m == "E")) - tol)
             for n in de_members(ctx)) if de_enabled(ctx) else 0
    return {
        "off_range": (max(offs) - min(offs)) if len(offs) >= 2 else 0,
        "n_range": (max(nights) - min(nights)) if len(nights) >= 2 else 0,
        "de": de,
    }


METRIC_LABEL = {"off_range": "사람마다 OFF 수 차이", "n_range": "사람마다 나이트 수 차이",
                "de": "D·E 쏠림 점수"}


def is_metric_note(text: str) -> bool:
    """품질 문구(`solver.quality_notes`)가 분배 지표인가 — 화면·답에서 '근무 모양'이 아니라 '분배'로 부른다(사용자 결정 11-05)."""
    return text.startswith(tuple(METRIC_LABEL.values()))
