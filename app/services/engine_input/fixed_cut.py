"""고정근무 차감 — 생성 경로가 솔버 호출 전에 고정근무자 몫을 커버리지 요구에서 빼는 블록(EI-9-0).

★★ 고정근무자(`fixed_shift`)는 `_split_fixed_nurses` 로 갈려 **솔버를 타지 않고** 나중에
  `fixed_roster` 로 병합된다. 그래서 솔버의 커버리지 식은 이들을 못 본다. 요구 D=4 인 날
  솔버가 4명을 채우고 거기에 고정근무자 1명이 얹혀 **5명**이 됐다(성남ICU-RN 2026-10 실측:
  평일 11일 전부 정확히 +1 · E/N 은 0건 — 고정 코드가 D 뿐이라 D 에만 생겼다). 일자별로
  빼야 한다 — 고정근무자는 평일만 근무하고 주말·공휴일은 OFF 다.
★★ 오버레이까지 적용한 **최종본**으로 차감해야 한다. `_build_fixed_shift_roster` 는 평일을
  기계적으로 fixed_shift 로 채우는데, 그 뒤 확정 원티드·특수요청이 덮어써서 실제 근무일이
  줄어든다. 기계적 결과로 빼면 그 차이만큼 **과다 차감**되어 반대로 인원 부족이 난다.
★★ max(상한) 맵도 같이 내려야 한다. min 만 빼면 솔버는 [min-1, max] 창을 보게 되고,
  상한까지 채운 날에 고정근무자가 얹혀 **설정 max 를 초과**한다(성남ICU-RN 2026-10 VER37
  실측: min 4/4/4·max 5/5/5 일 때 6일 D 솔버 5 + 고정 D 1명 = 6 > max 5).

이 모듈은 `app/services/roster_create_service.py`(이하 rcs) 의 고정근무 차감 블록(호출
자리는 rcs 의 "고정근무자 몫을 커버리지 요구에서 뺀다" 주석 참고)을 '읽기 → 계산 → 대입'
세 함수로 나눈 것이다(EI9_SPEC.md §3). rcs 는 그대로 쓰던 자리에서 이 세
함수를 부른다(동작·쿼리 수·순서·대입 값·예외 처리 불변). rcs 세 로더
(`_holiday_off_enabled`·`_load_shift_lookup`·`_build_fixed_shift_roster`)와 행
읽기·덮기(`_read_fixed_wanted_rows`·`_apply_fixed_wanted_rows`·
`_overlay_fixed_roster_with_special_requests`)는 rcs 에 그대로 두고(D23), 이 모듈은
함수 **안에서** 지연 import 로 부른다(순환 방지).

★ 기초 로스터 빌드(`_build_fixed_shift_roster`, DB 0)는 설계서(§2.4)와 달리 **계산이 아니라
  읽기 쪽**에 둔다 — 근무코드 읽기와 확정 원티드 읽기 사이. 모르는 고정 코드(400)면 여기서
  `HTTPException` 이 나므로 그 뒤의 확정 원티드 조회가 아예 안 열린다. 설계서 순서(빌드를
  계산 쪽에)로 나누면 이 경로에서 조회가 1회 늘어난다(S5-Z 실측, EI9_SPEC.md §3.2·§3.3).
"""
from __future__ import annotations

import calendar
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from db.models import FixedWantedEntry, Nurse

#: `compute_fixed_cut_demand` 가 `demand_config` 에서 읽는 키(그 이상은 보지 않는다).
FIXED_CUT_DEMAND_KEYS: tuple[str, ...] = (
    "daily_shift_requirements", "day_req", "eve_req", "nig_req",
    "daily_shift_requirements_by_day", "daily_shift_requirements_max_by_day",
)


@dataclass(frozen=True)
class FixedCutInputs:
    """고정근무 차감의 입력 — `read_fixed_cut_inputs` 가 지금 순서로 채운다.

    오프라인(9b)에서 직접 채울 때도 이 순서(공휴일 OFF → 근무코드 → 기초 로스터 빌드 →
    확정 원티드)로 지어야 한다 — `base_roster` 가 비면 `wanted_rows` 는 `()` 다.
    """

    holiday_off: bool
    base_roster: dict[str, list[str]]
    wanted_rows: tuple[FixedWantedEntry, ...]
    #: 읽은 근무코드 맵(EI-13c) — 고정근무 병합이 다시 읽지 않고 쓴다. 오프라인이 직접 채울 때는 없어도 된다.
    shift_lookup: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class FixedCutDemand:
    """차감 결과. `by_day`·`max_by_day` 가 `None` 이면 그 키를 바꾸지 않는다(원문 `if _fx_cut:`)."""

    by_day: list[dict[str, int]] | None
    max_by_day: list[dict[str, int]] | None
    cut_min: int
    cut_max: int
    raised: int
    final_roster: dict[str, list[str]]


# ── 읽기 ────────────────────────────────────────────────────────────────


def read_fixed_cut_inputs(
    db: Session,
    fixed_nurses: Sequence[Nurse],
    *,
    office_id: str,
    group_id: str,
    year: int,
    month: int,
    weekly_off_active: bool,
    shift_lookup_getter: Callable[[], Mapping[str, Any]] | None = None,
) -> FixedCutInputs:
    """공휴일 OFF → 근무코드 → 기초 로스터 빌드(DB 0) → 확정 원티드 행(순서 고정, 예외 전파).

    네 호출의 순서는 키워드 인자 평가 순서로 고정돼 있던 원문(rcs 의 차감 블록 앞머리)과 같다.
    기초 로스터 빌드가 `HTTPException`(모르는 고정 코드)을 내면 그 자리에서 멈춰 확정
    원티드 조회가 열리지 않는다 — 이 함수는 그 예외를 삼키지 않고 그대로 올린다.
    """
    from services.roster_create_service import (
        _build_fixed_shift_roster,
        _holiday_off_enabled,
        _load_shift_lookup,
        _read_fixed_wanted_rows,
    )

    holiday_off = _holiday_off_enabled(db, group_id)
    # (EI-13c) 단계 A 의 근무코드 getter 가 있으면 그것을 쓴다 — precheck·나이트 블록이 이미 읽었으면 조회 0, 아니면 여기서 1회.
    shift_lookup = shift_lookup_getter() if shift_lookup_getter is not None else _load_shift_lookup(db, office_id, group_id)
    base_roster = _build_fixed_shift_roster(
        list(fixed_nurses), year, month, weekday_off_code="O", sunday_code="주",
        holiday_off=holiday_off, shift_lookup=shift_lookup, weekly_off_active=weekly_off_active,
    )
    wanted_rows = (
        _read_fixed_wanted_rows(db, list(base_roster.keys()), group_id, year, month)
        if base_roster else []
    )
    return FixedCutInputs(holiday_off=holiday_off, base_roster=base_roster,
                          wanted_rows=tuple(wanted_rows), shift_lookup=shift_lookup)


# ── 계산(DB 0 · print 없음) ──────────────────────────────────────────────


def _overlay_cut_roster(
    base_roster: Mapping[str, list[str]],
    wanted_rows: Sequence[FixedWantedEntry],
    special_requests: Sequence[dict[str, Any]],
    year: int,
    month: int,
    days_in_month: int,
) -> dict[str, list[str]]:
    """로스터 사본에 확정 원티드 → 특수요청을 순서대로 덮어씌운다(원문 — 덮기 단계)."""
    from services.roster_create_service import (
        _apply_fixed_wanted_rows,
        _overlay_fixed_roster_with_special_requests,
    )

    roster = {k: list(v) for k, v in base_roster.items()}
    roster = _apply_fixed_wanted_rows(roster, list(wanted_rows), days_in_month)
    return _overlay_fixed_roster_with_special_requests(
        fixed_roster=roster, requests=list(special_requests), year=year, month=month)


def _demand_lists(
    demand_config: Mapping[str, Any], days_in_month: int,
) -> tuple[list[dict], list[dict] | None, list[set] | None]:
    """수요 기본값·by_day·max_by_day·max_active 사본을 만든다(원문 — 수요 사본 구성 단계)."""
    base_req = dict(demand_config.get("daily_shift_requirements") or {}) or {
        "D": int(demand_config.get("day_req") or 0),
        "E": int(demand_config.get("eve_req") or 0),
        "N": int(demand_config.get("nig_req") or 0),
    }
    by_day = demand_config.get("daily_shift_requirements_by_day")
    if not (isinstance(by_day, list) and len(by_day) == days_in_month):
        by_day = [dict(base_req) for _ in range(days_in_month)]
    else:
        by_day = [dict(dm or {}) for dm in by_day]
    by_day_max = demand_config.get("daily_shift_requirements_max_by_day")
    max_active: list[set] | None = None
    # ★ isinstance 검사를 `if` 에 바로 둔다(중간 bool 변수로 빼면 pyright 가 분기 안에서
    #   `by_day_max` 를 list 로 좁히지 못해 reportOptionalIterable 이 난다 — 리뷰 LOW).
    if isinstance(by_day_max, list) and len(by_day_max) == days_in_month:
        by_day_max = [dict(dm or {}) for dm in by_day_max]
        # 원래 상한이 걸려 있던 (일, 코드). 엔진은 max 0 을 '상한 없음' 으로 읽으므로
        # 차감으로 0 이 된 칸을 그냥 두면 상한이 통째로 사라진다. `_raise_max_to_min` 이 되살핀다.
        max_active = [{k for k, v in dm.items() if int(v or 0) > 0} for dm in by_day_max]
    else:
        by_day_max = None
    return by_day, by_day_max, max_active


def _cut_fixed_codes(
    roster: Mapping[str, list[str]],
    by_day: list[dict],
    by_day_max: list[dict] | None,
    days_in_month: int,
) -> tuple[int, int]:
    """고정근무 칸마다 수요 min·max 를 1씩 뺀다(원문 — 차감 루프). `by_day`·`by_day_max` 제자리 변경."""
    cut_min = 0
    cut_max = 0
    for codes in roster.values():
        for day_idx, code in enumerate(codes or []):
            if day_idx >= days_in_month:
                break
            normalized = str(code or "").strip().upper()
            if normalized in by_day[day_idx] and int(by_day[day_idx][normalized] or 0) > 0:
                by_day[day_idx][normalized] = int(by_day[day_idx][normalized]) - 1
                cut_min += 1
            if by_day_max is not None and int(by_day_max[day_idx].get(normalized) or 0) > 0:
                by_day_max[day_idx][normalized] = int(by_day_max[day_idx][normalized]) - 1
                cut_max += 1
    return cut_min, cut_max


def _raise_max_to_min(
    by_day: list[dict], by_day_max: list[dict], max_active: list[set], days_in_month: int,
) -> int:
    """min 이 max 를 넘은 활성 칸을 min 으로 되올린다(원문 — 되올림 루프). `by_day_max` 제자리 변경.

    차감 결과 max 가 0 이 된 칸의 처리(EI-9-0 전 인라인 블록 주석 그대로):
      · min 도 같이 0 이면 그대로 둔다 — 엔진이 `req_raw == 0` 을 보고 `assigned == 0` 하드로
        막는다(fallback_lex.py `zero_demand_block_codes`). 상한이 새지 않는다.
      · min 이 남아 있는데 max 만 0 이 된 칸은 원설정이 max < min 인 모순이다. 0 을 그대로
        두면 엔진이 '상한 없음' 으로 읽어 초과가 다시 샌다. min 까지 되올려 max == min 으로 닫는다.
    """
    raised = 0
    for day_idx in range(days_in_month):
        for code in max_active[day_idx]:
            min_value = int(by_day[day_idx].get(code) or 0)
            if int(by_day_max[day_idx].get(code) or 0) < min_value:
                by_day_max[day_idx][code] = min_value
                raised += 1
    return raised


def compute_fixed_cut_demand(
    inputs: FixedCutInputs,
    special_requests: Sequence[dict[str, Any]],
    demand_config: Mapping[str, Any],
    *,
    year: int,
    month: int,
) -> FixedCutDemand:
    """덮기(확정 원티드 → 특수요청) → 차감 → 되올림(DB 0, print 없음). 원문 그대로(차감 블록 전체).

    `demand_config`(읽는 키는 `FIXED_CUT_DEMAND_KEYS`)·`inputs.base_roster` 는 바꾸지 않는다.
    """
    days_in_month = calendar.monthrange(year, month)[1]
    roster = _overlay_cut_roster(
        inputs.base_roster, inputs.wanted_rows, special_requests, year, month, days_in_month)
    by_day, by_day_max, max_active = _demand_lists(demand_config, days_in_month)
    cut_min, cut_max = _cut_fixed_codes(roster, by_day, by_day_max, days_in_month)
    raised = 0
    if cut_max and by_day_max is not None and max_active is not None:
        raised = _raise_max_to_min(by_day, by_day_max, max_active, days_in_month)
    return FixedCutDemand(
        by_day=by_day if cut_min else None,
        max_by_day=by_day_max if cut_max else None,
        cut_min=cut_min, cut_max=cut_max, raised=raised, final_roster=roster,
    )


# ── 대입(+ 로그) ─────────────────────────────────────────────────────────


def apply_fixed_cut_demand(config_dict: dict, demand: FixedCutDemand, *, n_fixed: int) -> None:
    """by_day·max_by_day 를 `config_dict` 에 대입하고 같은 문구·순서로 로그를 남긴다(원문 — 대입·로그 단계)."""
    if demand.by_day is not None:
        config_dict["daily_shift_requirements_by_day"] = demand.by_day
    if demand.max_by_day is not None:
        config_dict["daily_shift_requirements_max_by_day"] = demand.max_by_day
        if demand.raised:
            print(f"[FixedShiftCoverage] max < min 이 된 {demand.raised}칸을 min 으로 되올림 "
                  f"(원설정이 max < min 인 칸)")
    if demand.cut_min or demand.cut_max:
        print(f"[FixedShiftCoverage] 고정근무자 {n_fixed}명 · "
              f"요구 차감 min {demand.cut_min}셀 / max {demand.cut_max}셀 — 솔버는 남은 인원만 채운다")
