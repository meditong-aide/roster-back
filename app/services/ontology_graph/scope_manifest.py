"""Exact scope manifest — 엔진이 **모델링하는 제약**을 명시하고, 미지원 hard constraint 가
활성이면 판정을 **UNKNOWN 으로 강등**한다(false feasible 방지).

핵심(피드백): component-wise FEASIBLE 을 전체 FEASIBLE 로 합치려면 **모든 전역 결합 제약이
factor graph 에 있어야** 한다. 하나라도 빠지면 두 component 를 잘못 독립으로 보고 false
feasible 이 난다. 따라서 "지원 제약 목록"을 두고, 목록 밖 hard constraint 가 켜지면 exact 를
주장하지 않고 UNKNOWN 을 낸다. 그래야 fuzz 의 false-feasible 0 이 **생성기 제약에만 국한**되지
않는다.

지원(모델링됨):
  · allowed_shifts / forbidden(N·O) / forced_off
  · 야간 run 상한(max_consecutive_nights) · 회복 OFF(2N2OFF·3N2OFF) · not_one_night
  · max_consecutive_work · forbid_night_to_day
  · per-day D/E/N coverage (daily_shift_requirements / nig_req)

미지원(활성 시 UNKNOWN):
  · 월 N quota(n_exact/n_min/n_max, max/min_nig_per_month) · 월 OFF quota(off_days)
  · 주말휴무(is_weekend_off) · 야간전담 월한도 · Grade/Team 최소인원 · 프리셉터 · 상호배제
  · 보건휴가(health_leave_enabled → 대상자 OFF 하한 +1 HARD) — 그래프 미모델
  (수면OFF sleep_off_enabled 은 순수 post-process=솔브 feasibility 무영향 → 여기 불포함)
"""

from __future__ import annotations

from services.ontology_graph.lagrangian import _nurse_attr

# config 레벨 미지원 hard constraint 마커(활성=truthy/양수면 미지원)
_UNSUPPORTED_CONFIG = (
    "max_nig_per_month", "min_nig_per_month", "nig_max_month", "nig_min_month",
    "off_days", "min_off_days", "weekend_off", "weekend_off_ids",
    "grade_requirements", "grade_min", "team_requirements", "team_min",
    "preceptor_pairs", "mutual_exclusion", "pairing",
    # 보건휴가: 활성 시 대상자 OFF 하한 +1(HARD, 솔브 전) — 그래프가 OFF 하한 미모델 → 미지원.
    "health_leave_enabled",
)
# per-nurse 미지원 마커
_UNSUPPORTED_NURSE = ("n_exact", "n_min", "n_max", "is_weekend_off", "is_night_only",
                      "health_leave_extra_off")   # 보건휴가 대상자 OFF 하한 가산(HARD)


def _active(v) -> bool:
    if v is None or v is False:
        return False
    if isinstance(v, (int, float)):
        return v > 0
    if isinstance(v, (list, dict, tuple, set, str)):
        return len(v) > 0
    return bool(v)


def unmodeled_active(nurses: list, config: dict) -> list[str]:
    """활성인 **미지원 hard constraint** 목록. 비어있지 않으면 exact 주장 금지(→UNKNOWN)."""
    out: list[str] = []
    for key in _UNSUPPORTED_CONFIG:
        if key in config and _active(config.get(key)):
            out.append(f"config.{key}")
    seen_nurse: set = set()
    for nu in nurses:
        for key in _UNSUPPORTED_NURSE:
            if key in seen_nurse:
                continue
            val = _nurse_attr(nu, key)
            # is_night_only 는 allowed_shifts=["N"] 로 모델되므로, 별도 플래그가 켜져도
            # allowed_shifts 가 있으면 지원(중복). allowed_shifts 없이 플래그만이면 미지원.
            if key == "is_night_only" and (_nurse_attr(nu, "allowed_shifts")):
                continue
            if _active(val):
                out.append(f"nurse.{key}")
                seen_nurse.add(key)
    return out


def exact_or_unknown(nurses: list, config: dict) -> str | None:
    """미지원 제약 활성이면 사유 문자열, 아니면 None(=지원 범위 안, exact 주장 가능)."""
    um = unmodeled_active(nurses, config)
    if um:
        return "미지원 hard constraint 활성: " + ", ".join(sorted(set(um)))
    return None


def gate_feasible(status: str, nurses: list, config: dict) -> str:
    """**비대칭 scope**: 지원 subset 의 INFEASIBLE 은 항상 sound(제약 추가로 feasible 안 됨) →
    그대로 반환. FEASIBLE 주장만 미지원 제약 있으면 UNKNOWN 으로 강등. UNKNOWN 은 그대로.

    subset ⊆ full 이므로 subset infeasible ⟹ full infeasible(certificate 유효). 반대로 subset
    feasible 은 미지원 제약이 깰 수 있어 full feasible 을 보장 못 함.
    """
    if status == "INFEASIBLE_CERTIFIED":
        return status
    if status == "FEASIBLE_WITNESS" and unmodeled_active(nurses, config):
        return "UNKNOWN"
    return status
