"""EI-9b 빌더 — `PreludeState` + `db` 로 단계 A(precheck)·단계 B(solve)·합본을 짓는다(호출처 0).

`EI9_SPEC.md` §2.4(코드 지도는 §1.2·§1.3)를 그대로 따른다.

단계 A(`build_precheck_stage`) — precheck 블록이 지금 돌면서 읽는 등급·주말휴무·팀·근무코드
네 조회를 **지연 getter**(`Lazy`)로 묶어 `PrecheckStage` 에 담는다. 호출 자체는 DB 를 전혀
읽지 않는다 — 네 개의 thunk(클로저)를 만들 뿐이다. 실제 조회는 **부르는 쪽이 `Lazy.get()` 을
처음 부를 때** 그 자리에서 1회 일어난다.

★ 조회 순서는 이 모듈이 정하지 않는다. `build_precheck_stage` 는 네 getter 를 동시에
  준비해 둘 뿐이고, 어느 것을 먼저 읽을지는 부르는 쪽(지금은 9c 가 재현할 production 순서 —
  등급 → 주말휴무 → 팀 → 근무코드)이 정한다(§1.2 '조회 순서는 부르는 쪽 순서다').
★ getter 는 자기 예외를 삼키지 않는다(§1.2). `Lazy.get()` 이 실패하면 그 예외를 그대로
  다시 올린다 — 실패를 `load_report.errors` 에 담는 것은 이 모듈이 아니라 이를 부르는
  `build_engine_input`의 몫이다(설계서 §2.4 '라이즈 하지 않는다. 실패는 load_report 에
  담는다' — 그 규칙은 전체 빌더 파사드에 있고, 낱개 getter 는 투명하게 전파하기만 한다).

단계 B(`build_solve_stage`) — 허용근무 기간표·전월 꼬리 원자료·금지 원티드(§1.3 ④⑤⑥)를
지금 서명 그대로 제자리 호출하고, 9-0 의 차감 함수로 차감 뒤 수요를 다시 계산하고(§3.8),
post 6종(§1.5, D20 — 기록만)·solver_params·env_effective 를 기록한다. `build_engine_input`
이 단계 A + B 를 `EngineInput` 한 벌로 합친다 — raise 하지 않고 실패는 `load_report.errors`
에 남긴다(§2.4-2).

★ 모든 rcs·관련 서비스 함수는 **함수 안에서** 지연 import 한다(순환 방지 — `fixed_cut.py`·
  `raw.py` 와 같은 가드). 빌더는 읽기 전용이다 — commit 은 어디에서도 하지 않는다.
"""
from __future__ import annotations

import calendar
import contextlib
import copy
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date
from types import SimpleNamespace
from typing import Any, Literal

from sqlalchemy.orm import Session

from .fixed_cut import compute_fixed_cut_demand, read_fixed_cut_inputs
from .model import Ctx, DemandPart, DemandSnapshot, EngineInput, LoadReport, PreludeState
from .raw import RAW_SETTING_KEYS, read_raw_setting
from .snapshot import canonical


@dataclass
class Lazy[T]:
    """지연 getter — 첫 `get()` 에 `loader` 를 1회만 부른다(성공이든 실패든 다시 부르지 않는다).

    실패도 캐시한다: 첫 호출이 예외를 내면 그 예외 객체를 들고 있다가 이후 모든 `get()` 이
    **다시 조회하지 않고** 같은 예외를 재전파한다 — '처음 읽힐 때 1회 조회'를 성공·실패
    양쪽에서 지키기 위해서다.
    """

    _loader: Callable[[], T]
    _loaded: bool = field(default=False, init=False, repr=False)
    _value: T | None = field(default=None, init=False, repr=False)
    _error: Exception | None = field(default=None, init=False, repr=False)

    def get(self) -> T:
        """값을 돌려준다(처음이면 조회, 그 뒤는 캐시). 예외는 삼키지 않고 그대로 올린다.

        캐시하는 것은 `Exception` 뿐이다 — `KeyboardInterrupt`·`SystemExit`·하네스의
        `GoldenStop`(`BaseException`)까지 캐시하면 무관한 자리에서 같은 객체가 다시 올라오고
        traceback 이 계속 쌓인다(리뷰 지적). 그런 신호는 `_loaded` 를 되돌려 다음 `get()` 이
        다시 시도하게 둔다 — '처음 읽힐 때 1회 조회'는 `Exception` 범위 안에서만 보장한다.
        """
        if not self._loaded:
            self._loaded = True
            try:
                self._value = self._loader()
            except Exception as exc:  # noqa: BLE001 — 삼키지 않는다, 캐시만 한다
                self._error = exc
            except BaseException:
                self._loaded = False  # 캐시하지 않고 그대로 전파(재시도 가능 상태로 되돌린다)
                raise
        if self._error is not None:
            raise self._error
        return self._value  # type: ignore[return-value]

    @property
    def loaded(self) -> bool:
        """`get()` 이 한 번이라도 불렸는가(성공·실패 무관) — '안 읽힌 getter 는 쿼리 0' 단언에 쓴다."""
        return self._loaded


def _days_filled_ctx(ctx: Ctx) -> Ctx:
    """`PreludeState.ctx` 는 `days` 가 없다 — `days` 를 채운 사본(§2.2·§6 O-22).

    `EngineInput.ctx` 뿐 아니라 `PrecheckStage.ctx` 도 이 사본을 쓴다(리뷰 지적 MEDIUM —
    `to_precheck_input`(`run_runtime_precheck` 의 `year`·`month`)·`presolve_view`(탐지기의
    `num_days`)가 `stage_a` 만으로 지어지려면 `days` 가 있는 `Ctx` 가 필요하다).
    """
    days = calendar.monthrange(ctx.year, ctx.month)[1]
    return Ctx(office_id=ctx.office_id, group_id=ctx.group_id, year=ctx.year, month=ctx.month,
              config_id=ctx.config_id, job_id=ctx.job_id, runtime=ctx.runtime, days=days)


@dataclass(frozen=True)
class PrecheckLive:
    """운영 전용 실물 참조(§0-1, EI-10a) — 사본이 아니라 그 자리 객체 그대로다.

    골든 L0′ 의 간호사는 ORM `__dict__` 기록이라 `"__orm__": "Nurse"` 표지가 붙는다
    (snapshot.py:105-112) — `PrecheckStage` 의 사본(dict)을 탐지기에 넘기면 그 표지가
    없어져 골든이 깨진다(재생성 금지). 그래서 탐지기·precheck 가 쓰는 운영 소비자는
    이 실물 참조를 쓰고, 오프라인(9c)만 사본을 쓴다(`views._sources`).

    `config_dict` 는 **일부러 넣지 않는다**(§0-2) — 고정근무 차감이 그 자리 `config_dict`
    에 제자리 키 대입을 하므로(fixed_cut.py:240-243), 여기 실물 참조를 두면 '읽는 시점'에
    따라 차감 전/뒤가 갈리는 함정이 된다. config 는 호출부가 그 자리 `config_dict` 를
    인자로 넘긴다(`views.presolve_view(stage_a, config_dict)`).
    """

    nurses_for_engine: Sequence[Any]
    combined_fixed_cells: Sequence[Mapping[str, Any]]
    shift_manage_data: Sequence[Mapping[str, Any]]


@dataclass
class PrecheckStage:
    """정석 4종(§1.2) — 등급·주말휴무·팀(원본 행)·근무코드. 전부 지연, 조회 0 상태로 돌아온다.

    §2.4 '메모리에 이미 있는 객체' 네 칸(B — 리뷰 MEDIUM)도 함께 든다 — EI-10a/b 의 view 가
    `prelude` 를 따로 받지 않고 `stage_a` 만으로 지을 수 있게 한다. `build_precheck_stage` 가
    `prelude` 에서 deepcopy 로 떠 온다(BR-6 — `stage_a` 를 받은 쪽이 값을 바꿔도 `prelude`
    원본·캐시는 그대로다). 조회가 아니라 사본이라 `Lazy` 로 감싸지 않는다 — 이미 메모리에
    있는 값을 다시 '지연'시킬 이유가 없다(§2.4-R '프렐류드 캡처형'과 같은 전제). `ctx` 는
    `days` 가 채워진 사본이다(리뷰 지적 — `prelude.ctx` 는 `days` 가 없어 `to_precheck_input`/
    `presolve_view` 가 요구하는 연·월·일수 묶음을 `stage_a` 만으로 못 지었다).
    """

    grade: Lazy[dict[str, Any]]
    weekend_ids: Lazy[set[str]]
    team_rows: Lazy[list[Any]]
    shift_lookup: Lazy[dict[str, Any]]
    #: `prelude.ctx` 의 `days` 보강 사본(`EngineInput.ctx` 와 같은 모양).
    ctx: Ctx = field(default_factory=lambda: Ctx(office_id="", group_id="", year=0, month=0))
    #: precheck 시점 `config_dict` 사본(차감 전) — `prelude.config_dict` 의 deepcopy.
    config_dict: dict[str, Any] = field(default_factory=dict)
    #: 엔진 정렬 그대로 — `prelude.nurses_for_engine` 의 deepcopy.
    nurses_for_engine: list[dict[str, Any]] = field(default_factory=list)
    #: `prelude.combined_fixed_cells` 의 deepcopy.
    combined_fixed_cells: list[dict[str, Any]] = field(default_factory=list)
    #: `prelude.shift_manage_data` 의 deepcopy.
    shift_manage_data: list[dict[str, Any]] = field(default_factory=list)
    #: 운영 전용 실물 참조(EI-10a §0-1) — `build_precheck_stage(live=...)` 로만 채워진다.
    #: 오프라인(9c)은 `None` — view 소비자(`views._sources`)가 그때는 위 사본 4칸을
    #: `_as_nurse_views` 로 감싸 쓴다. repr·`==` 에서 뺀다(ORM 객체를 들고 있어 비교·출력이
    #: 비싸거나 실패할 수 있다).
    live: PrecheckLive | None = field(default=None, repr=False, compare=False)


def _memory_copies(prelude: PreludeState) -> dict[str, Any]:
    """`PrecheckStage` 의 '메모리에 이미 있는 객체' 네 칸을 `prelude` 에서 deepcopy 로 뜬다.

    `build_precheck_stage` 본문을 30줄 이하로 유지하려고 뗀 헬퍼다(리뷰 지적 LOW).
    """
    return {
        "ctx": _days_filled_ctx(prelude.ctx),
        "config_dict": copy.deepcopy(prelude.config_dict),
        "nurses_for_engine": copy.deepcopy(prelude.nurses_for_engine),
        "combined_fixed_cells": copy.deepcopy(prelude.combined_fixed_cells),
        "shift_manage_data": copy.deepcopy(prelude.shift_manage_data),
    }


def _weekend_target_ids(prelude: PreludeState, live: PrecheckLive | None) -> list[Any]:
    """주말휴무 조회 대상 `nurse_id` 목록 — `live` 있으면 실물(getattr)에서, 없으면(오프라인)
    프렐류드 사본(`.get`)에서 읽는다. `build_precheck_stage` 본문을 30줄 이하로 유지하려고
    뗀 헬퍼다(리뷰 지적 LOW — `live` 분기 추가로 기존 길이를 넘었다)."""
    if live is not None:
        return [getattr(n, "nurse_id", None) for n in live.nurses_for_engine]
    return [nurse.get("nurse_id") for nurse in prelude.nurses_for_engine]


def build_precheck_stage(
    db: Session, prelude: PreludeState, *, live: PrecheckLive | None = None,
) -> PrecheckStage:
    """`PrecheckStage` 를 짓는다 — 이 호출 자체는 DB 를 읽지 않는다(네 thunk 를 만들 뿐).

    각 thunk 의 자리·조건은 §1.2 그대로다.
      - 등급: `_fetch_grade_config_dict(db, office_id, group_id)` (rcs:6459 자리)
      - 주말휴무: `weekend_off_ids_asof(db, nurse_ids, year, month)` — 대상은
        `_weekend_target_ids`(`live` 있으면 실물, 없으면 `prelude.nurses_for_engine` 사본)
        전부의 `nurse_id` (rcs:6463-6466 자리)
      - 팀: `db.query(Team).filter(office_id, group_id, active==1).all()` — 원본 행만(팀
        최소인원으로 가공하는 것은 단계 B 의 몫) (rcs:6505 자리)
      - 근무코드: `_load_shift_lookup(db, office_id, group_id)` (나이트 블록 자리, rcs:468 정의)
    """
    office_id, group_id = prelude.ctx.office_id, prelude.ctx.group_id
    year, month = prelude.ctx.year, prelude.ctx.month

    def _load_grade() -> dict[str, Any]:
        from services.roster_create_service import _fetch_grade_config_dict

        swallowed: list[Exception] = []
        grade = _fetch_grade_config_dict(db, office_id, group_id, swallowed)
        if swallowed:  # 삼킨 읽기 실패 → 기본값을 정상값으로 굳히지 않는다(AttemptSource 불가, 시도마다 재조회)
            raise RuntimeError(f"grade config read swallowed: {swallowed[0]}")
        return grade

    def _load_weekend_ids() -> set[str]:
        from services.nurse_period_resolver import weekend_off_ids_asof

        return weekend_off_ids_asof(db, _weekend_target_ids(prelude, live), year, month)

    def _load_team_rows() -> list[Any]:
        from db.models import Team

        return (
            db.query(Team)
            .filter(Team.office_id == office_id, Team.group_id == group_id, Team.active == 1)
            .all()
        )

    def _load_shift_lookup() -> dict[str, Any]:
        from services.roster_create_service import _load_shift_lookup as _rcs_load_shift_lookup

        return _rcs_load_shift_lookup(db, office_id, group_id)

    return PrecheckStage(
        grade=Lazy(_load_grade),
        weekend_ids=Lazy(_load_weekend_ids),
        team_rows=Lazy(_load_team_rows),
        shift_lookup=Lazy(_load_shift_lookup),
        live=live,
        **_memory_copies(prelude),
    )


# ══════════════════════════════════════════════════════════════════════════
# EI-9b-2 단계 B · post · 합본(EI9_SPEC.md §2.4-2) — 아래부터 이번 차례 추가분.
# ══════════════════════════════════════════════════════════════════════════


def _as_nurse_views(nurses: Sequence[Mapping[str, Any]]) -> list[SimpleNamespace]:
    """딕셔너리 간호사를 getattr 접근 가능한 뷰로 감싼다(제자리 호출 어댑터 — ORM 아님, 사본일 뿐).

    rcs 의 ④⑤⑥·post 헬퍼는 전부 `getattr(n, "attr", default)`(또는 `n.nurse_id`, 키가 항상
    있다) 로만 읽는다 — `nurses_for_engine`(dict 사본)을 그대로 넣으면 `getattr` 이 기본값으로
    떨어져 전부 빈 값이 된다. `SimpleNamespace` 는 속성 접근만 흉내 내고 DB 세션을 갖지 않는다.
    """
    return [SimpleNamespace(**n) for n in nurses]


def _safe_call(errors: dict[str, str], key: str, fn: Callable[..., Any], *args: Any,
              **kwargs: Any) -> Any:
    """`fn` 을 호출하고 실패하면 `errors[key]` 에 담아 `None` 을 돌려준다.

    빌더는 raise 하지 않는다(§0 지시) — 부품 하나가 실패해도 나머지는 그대로 돈다(D20 의
    '기록만' 정신과 같다). 어떤 예외든 잡는다 — 레코딩 전용 경로라 삼켜도 생성을 막지 않는다.
    """
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 — 부품별 기록(기존 rcs 의 숱한 `except Exception` 과 같은 폭)
        errors[key] = f"{type(exc).__name__}: {exc}"
        return None


# ── 수요 스냅샷(pre) + 차감 뒤 수요(post, 9-0 재사용) ───────────────────────


def _demand_base(config: Mapping[str, Any]) -> dict[str, int]:
    """수요 기본값 — `fixed_cut._demand_lists` 원문 1~4행과 같은 폴백식."""
    base = config.get("daily_shift_requirements")
    if isinstance(base, dict) and base:
        return dict(base)
    return {
        "D": int(config.get("day_req") or 0),
        "E": int(config.get("eve_req") or 0),
        "N": int(config.get("nig_req") or 0),
    }


def _demand_snapshot(config: Mapping[str, Any]) -> DemandSnapshot:
    """`config` 에 있는 그대로의 수요 스냅샷(정규화 없음 — 차감 전 `pre`/운영 패스스루 전용).

    `by_day`/`max_by_day` 는 **깊은 사본**이다(BR-6, 리뷰 지적 — 이전엔 바깥 리스트만
    `list()` 로 복사해 안의 일자별 `dict` 는 `config` 와 같은 객체였다. 운영 모드에서
    `config` 가 `first_attempt_config`(EI-13a 에서는 production 이 쓸 그 config_dict 자체)일
    때 `DemandSnapshot` 을 받은 쪽이 값을 고치면 운영 config 까지 같이 바뀌었다).
    """
    by_day = config.get("daily_shift_requirements_by_day")
    max_by_day = config.get("daily_shift_requirements_max_by_day")
    return DemandSnapshot(
        base=_demand_base(config),
        by_day=copy.deepcopy(by_day) if isinstance(by_day, list) else None,
        max_by_day=copy.deepcopy(max_by_day) if isinstance(max_by_day, list) else None,
    )


def _fixed_cut_summary(demand: Any, *, applied: bool, reason: str) -> dict[str, Any]:
    """`FixedCutDemand`(또는 `None`) → `load_report.fixed_cut` 요약 dict(§2.2)."""
    if demand is None:
        return {"applied": applied, "reason": reason, "cut_min": 0, "cut_max": 0, "raised": 0}
    return {"applied": applied, "reason": reason, "cut_min": demand.cut_min,
            "cut_max": demand.cut_max, "raised": demand.raised}


def _run_fixed_cut(db: Session, prelude: PreludeState) -> tuple[Any, str | None]:
    """읽기+계산만 수행(대입 없음, §3.8) — 예외를 잡아 `(None, 메시지)` 로 돌려준다.

    공휴일 OFF 는 `read_fixed_cut_inputs` 내부가 `_holiday_off_enabled` 로 읽는다 — 9d
    `fixed_holiday_off_yn` 과 같은 선택 규칙이라 따로 조회하지 않는다(§3.8).

    `prelude.fixed_nurses` 는 dict 사본이다(§2.4-1) — `_build_fixed_shift_roster` 는
    `getattr(nurse, "fixed_shift"/"nurse_id"/"name", …)` 로 읽으므로(9-0 원문) 어댑터로 감싼다.
    """
    ctx = prelude.ctx
    try:
        fixed_views = _as_nurse_views(prelude.fixed_nurses)
        inputs = read_fixed_cut_inputs(
            db, fixed_views, office_id=ctx.office_id, group_id=ctx.group_id,
            year=ctx.year, month=ctx.month,
            weekly_off_active=bool(prelude.config_dict.get("weekly_off_settings_activate", False)),
        )
        demand = compute_fixed_cut_demand(
            inputs, prelude.fixed_special_requests, prelude.config_dict,
            year=ctx.year, month=ctx.month,
        )
        return demand, None
    except Exception as exc:  # noqa: BLE001 — 9-0 원문과 같은 폭(차감 실패가 생성을 막지 않는다)
        return None, f"{type(exc).__name__}: {exc}"


def _compute_demand_post(
    db: Session, prelude: PreludeState, demand_pre: DemandSnapshot,
) -> tuple[DemandSnapshot, dict[str, Any], str | None]:
    """차감 뒤 수요(§3.8) — 조건 미충족·예외면 `pre` 그대로."""
    if not (prelude.fixed_nurses and prelude.nurses_for_engine):
        return demand_pre, _fixed_cut_summary(None, applied=False, reason="no_fixed_nurses"), None
    demand, error = _run_fixed_cut(db, prelude)
    if demand is None:
        return demand_pre, _fixed_cut_summary(None, applied=False, reason="error"), error
    post = DemandSnapshot(
        base=demand_pre.base,
        by_day=demand.by_day if demand.by_day is not None else demand_pre.by_day,
        max_by_day=demand.max_by_day if demand.max_by_day is not None else demand_pre.max_by_day,
    )
    applied = bool(demand.cut_min or demand.cut_max)
    return post, _fixed_cut_summary(demand, applied=applied, reason="ok"), None


# ── 단계 B 재조회 3종(§1.3 ④⑤⑥) ─────────────────────────────────────────


def _query_allowed_constraints(
    db: Session, prelude: PreludeState, config: Mapping[str, Any], nurses: Sequence[SimpleNamespace],
    reads: dict | None = None,
) -> dict[str, Any]:
    """④ 허용근무 기간표 — `build_allowed_shift_type_constraints` 제자리 호출(§1.3)."""
    from services.roster_create_service import build_allowed_shift_type_constraints

    ctx = prelude.ctx
    fixed_cells = prelude.combined_fixed_cells if prelude.combined_fixed_cells else None
    return build_allowed_shift_type_constraints(
        nurses_in_group=list(nurses), year=ctx.year, month=ctx.month,
        shift_manage_data=prelude.shift_manage_data, fixed_cells=fixed_cells,
        use_mid=bool(config.get("use_mid", False)), db=db, reads=reads,
    )


def _query_cross_month_constraints(
    db: Session, prelude: PreludeState, config: dict[str, Any], nurse_ids: list[str],
    stage_a: PrecheckStage | None = None, reads: dict | None = None,
) -> dict[str, Any]:
    """⑤ 전월 꼬리 원자료 — `build_cross_month_constraints` 제자리 호출(§1.3).

    확정 칸(`prelude.combined_fixed_cells`)은 운영과 같은 헬퍼(`_fixed_main_by_nurse`)로 `fixed_by_nurse` 를 지어 넘긴다
    (EI-11 11w-3 · D32). 근무코드 정의는 `stage_a.shift_lookup`(이미 캐시된 Lazy — `_build_codebook` 과 같은 원천), 없으면
    `config["shift_definitions"]`.

    `req`·`current_user` 는 진짜 ORM/요청 객체가 아니다 — 함수가 쓰는 4칸(office_id·group_id·
    year·month)을 `Ctx` 가 전부 갖고 있어 그대로 두 자리에 넘긴다(실측 — §1 코드 지도).
    """
    from services.roster_create_service import _fixed_main_by_nurse, build_cross_month_constraints

    ctx = prelude.ctx
    if stage_a is not None:
        shift_defs = [_shift_definition_row(s) for s in stage_a.shift_lookup.get().values()]
    else:
        shift_defs = config.get("shift_definitions") or []
    fixed_by_nurse = _fixed_main_by_nurse(prelude.combined_fixed_cells, _as_nurse_views(prelude.nurses_for_engine),
                                          prelude.shift_manage_data, shift_defs)
    return build_cross_month_constraints(db, ctx, ctx, prelude.shift_manage_data, config, nurse_ids,
                                         fixed_by_nurse=fixed_by_nurse, reads=reads)


def _query_banned_constraints(
    db: Session, prelude: PreludeState, nurses: Sequence[SimpleNamespace],
) -> dict[str, Any]:
    """⑥ 금지 원티드 — `_build_banned_wanted_constraints` 제자리 호출(§1.3)."""
    from services.roster_create_service import _build_banned_wanted_constraints

    ctx = prelude.ctx
    swallowed: list[Exception] = []
    banned = _build_banned_wanted_constraints(db, ctx, ctx, list(nurses), swallowed)
    if swallowed:  # 삼킨 읽기 실패 → 빈 값을 정상값으로 굳히지 않는다(errors 기록 → AttemptSource 불가)
        raise RuntimeError(f"banned wanted read swallowed: {swallowed[0]}")
    return banned


#: EI-13a 계약(EI-11 11q 리뷰 LOW) — 이 부품 가운데 하나라도 `errors` 에 있으면 그 합본은 엔진 입력으로 쓰지 않는다.
#:   ④⑤⑥ 은 하드 칸(허용 근무·월경계 꼬리·금지 원티드)이라 빈 값으로 이어 가면 운영의 G5(경계 빌더 예외를 삼키고 하드락 없이
#:   생성 — 11q 에서 막았다)와 같은 꼴이 된다. 빌더는 지금처럼 raise 하지 않고 기록만 한다 — 판정은 엔진 입력을 쓰는 쪽이 한다.
ENGINE_BLOCKING_ERROR_KEYS: tuple[str, ...] = ("stage_b.allowed", "stage_b.cross_month", "stage_b.banned")


def engine_blocking_errors(errors: Mapping[str, str]) -> list[str]:
    """`errors` 가운데 엔진 입력으로 쓰면 안 되게 하는 키(하드 칸 부품 실패) — 비어 있어야 엔진 입력으로 쓸 수 있다."""
    return [key for key in ENGINE_BLOCKING_ERROR_KEYS if key in errors]


def _build_stage_b_queries(
    db: Session, prelude: PreludeState, config: dict[str, Any], stage_a: PrecheckStage | None = None,
    reads: tuple[dict, dict] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, str]]:
    """④⑤⑥(§1.3) 를 부품별로 호출한다 — 하나가 실패해도 나머지는 돌아온다(기록 목적)."""
    nurses = _as_nurse_views(prelude.nurses_for_engine)
    nurse_ids = [str(n.get("nurse_id")) for n in prelude.nurses_for_engine]
    errors: dict[str, str] = {}
    allowed_reads, cross_reads = reads if reads is not None else (None, None)
    allowed = _safe_call(errors, "stage_b.allowed", _query_allowed_constraints,
                        db, prelude, config, nurses, allowed_reads) or {}
    cross_month = _safe_call(errors, "stage_b.cross_month", _query_cross_month_constraints,
                             db, prelude, config, nurse_ids, stage_a, cross_reads) or {}
    banned = _safe_call(errors, "stage_b.banned", _query_banned_constraints,
                        db, prelude, nurses) or {}
    return allowed, cross_month, banned, errors


# ── cells.initial_constraints · carryover(리뷰 지적 — 단계 B 가 짓고 버리던 결과를 싣는다) ──

#: `cross_month_constraints`(§1.3 ⑤)에서 `carryover` 로 옮기는 키 — rcs :3610-:3625 가
#: `config_dict` 에 조건부로 심는 것과 같은 집합(`if prev_month_x:` 패턴 그대로).
#:
#: `off_window_constraints` 는 이 튜플에 **없다** — ⑤ 하나만으로는 못 채우는 값이라 별도
#: 공식(`_effective_off_window_constraints`, 아래)으로 합친다(O-18).
_CARRYOVER_KEYS: tuple[str, ...] = (
    "prev_month_last_main", "prev_month_n_tail", "prev_month_n_offs_after",
    "prev_month_last_n_k", "prev_month_off_tail",
)


def _effective_off_window_constraints(
    config: Mapping[str, Any], cross_month: Mapping[str, Any],
) -> dict[str, Any]:
    """`off_window_constraints` 실효값(O-18) — rcs `_run_cp_sat_basic` 의 합치기(`_merge_off_window_constraints`)를 그대로 쓴다.

    rcs 는 이 키를 두 군데에서 채운다 — ⑤ `build_cross_month_constraints`(꼬리 연속근무 기반,
    이 모듈이 재조회하는 바로 그 함수) **와** `build_mid_month_boundary_constraints`(병동이동/파견 —
    precheck **앞**에서 돌아, `config` 캡처 시점에 이미 그 결과가 들어 있다). 실효값은 **⑤ 의 창과
    `config` 의 창을 간호사별로 합친 것**이다(EI-11 11r — 예전 rcs 는 ⑤ 의 값이 있으면 `config` 의 값을
    통째로 바꿔 월중 경계 하드락 창이 엔진에 안 닿았고, 이 함수도 그 조건부 대입을 재현했다).
    `config` 는 단계 B 의 다른 계산(④⑤·team·off_first)과 같은 원천이다(리뷰 지적 L16 — production 모드에선
    `first_attempt_config`, offline 모드는 `config == prelude.config_dict`). S6(병동이동, X01·N07)으로 실측:
    ⑤ 만 재조회하면 `{}` 인데 `config`(precheck 캡처)는 `{'X01': [[19, 19]], 'N07': [[14, 17]]}` 를 이미 담고 있다.
    반환은 **깊은 사본**이다(BR-6, 리뷰 지적 — 이전엔 `config`/`cross_month` 의 리스트를 그대로 돌려줘
    `carryover` 를 고치면 원본까지 같이 바뀌었다).
    """
    from services.roster_create_service import _merge_off_window_constraints

    return copy.deepcopy(_merge_off_window_constraints(
        cross_month.get("off_window_constraints"), config.get("off_window_constraints")))


def _build_cells_and_carryover(
    prelude: PreludeState, config: Mapping[str, Any], allowed: Mapping[str, Any],
    cross_month: Mapping[str, Any], banned: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """`cells.initial_constraints`(④⑤⑥ + 월중 경계 병합)·`carryover`(⑤의 전월 꼬리 6종 + off_window).

    rcs `_run_cp_sat_basic` 과 같은 순서로 병합한다(`_merge_initial_constraints` 제자리 호출 —
    base=cross_month, extra=allowed 부터, 그 결과에 banned, 마지막에 `config` 의 금지칸·강제 OFF(월중 경계 —
    precheck 앞에서 심긴 값)를 합친다 — EI-11 11r). 이전엔 단계 B 가 allowed·cross_month·banned 를 조회만 하고
    버려 `EngineInput.cells`·`carryover` 가 늘 비어 있었다(리뷰 지적 — O-15). `cross_month` 가 비거나 실패해도
    (`{}`) 빈 병합·빈 carryover 로 떨어질 뿐 raise 하지 않는다 — `_merge_initial_constraints` 자체가
    `isinstance(dict)` 로 방어한다. `off_window_constraints` 는 O-18 — 별도 공식
    (`_effective_off_window_constraints`)으로 채운다(⑤ 단독 재조회로는 불완전해서).
    """
    from services.roster_create_service import _merge_initial_constraints

    merged = _merge_initial_constraints(base=cross_month, extra=allowed)
    merged = _merge_initial_constraints(base=merged, extra=banned)
    initial_constraints = _merge_initial_constraints(base=merged, extra=config.get("initial_constraints"))
    carryover = {key: cross_month[key] for key in _CARRYOVER_KEYS if cross_month.get(key)}
    off_window = _effective_off_window_constraints(config, cross_month)
    if off_window:
        carryover["off_window_constraints"] = off_window
    cells = {"combined_fixed_cells": copy.deepcopy(prelude.combined_fixed_cells),
             "initial_constraints": initial_constraints}
    return cells, carryover


# ── team_min_by_team(§2.4-1 '단계 B 의 몫') ──────────────────────────────


def _build_team_dict(
    prelude: PreludeState, stage_a: PrecheckStage, config: Mapping[str, Any],
) -> dict[str, Any]:
    """`team_min_by_team`·`team_handoff_policy_by_team` — `_run_cp_sat_basic` :3504-3546 재현.

    이미 값이 있으면(override) 덮지 않는다 — '런타임 주입이 이긴다' 규칙 그대로.
    """
    from services.roster_create_service import build_team_min_by_team

    team_rows = stage_a.team_rows.get()
    member_team_ids = {
        str(n.get("team_id")) for n in prelude.nurses_for_engine
        if n.get("team_id") not in (None, "", 0)
    }
    out: dict[str, Any] = {}
    if "team_min_by_team" in config:
        out["team_min_by_team"] = config["team_min_by_team"]
    else:
        built = build_team_min_by_team(team_rows, member_team_ids, bool(config.get("use_mid", False)))
        if built:
            out["team_min_by_team"] = built
    policy = {
        str(t.team_id): t.handoff_policy for t in team_rows
        if isinstance(getattr(t, "handoff_policy", None), dict)
        and isinstance(t.handoff_policy.get("restrictions"), list) and t.handoff_policy["restrictions"]
    }
    if policy:
        out["team_handoff_policy_by_team"] = policy
    return out


# ── codebook · roster · settings_eff(O-15 — 이전엔 빈 채로 두었던 세 부품) ──

def _shift_definition_row(shift: Any) -> dict[str, Any]:
    """근무코드 한 행 → `shift_definitions` 모양(rcs :3545-3551·presolve_block.py:402-408 과 같은 칸).

    `getattr` 기본값·`or` 폴백 자리까지 원문 그대로다 — `show_in_preference` 처럼 `False` 가
    유효값인 칸에 `or` 를 넣으면 조용히 `None` 으로 바뀌는 사고가 난다(각 칸을 따로 적는 이유).
    """
    return {
        "shift_id": getattr(shift, "shift_id", None),
        "default_shift": getattr(shift, "default_shift", None) or getattr(shift, "shift_id", None),
        "shift_gb": getattr(shift, "shift_gb", None),
        "type": getattr(shift, "type", None),
        "show_in_preference": getattr(shift, "show_in_preference", None),
    }


def _build_codebook(prelude: PreludeState, stage_a: PrecheckStage) -> dict[str, Any]:
    """`codebook` — `shift_definitions`·`code2main`·`id_to_main`(§2.4 표 '사례 ⑤의 사본 3벌을 하나로').

    셋 다 **조회 하나**(`stage_a.shift_lookup` — 이미 캐시된 Lazy, 다시 쿼리하지 않는다)에서
    짓는다. 프로덕션은 이 셋을 세 자리에서 각각 쓴다 — `shift_definitions` 는 `_run_cp_sat_basic`
    (:3544-3563, `config_dict["shift_definitions"]`로 대입), `code2main`·`id_to_main` 은 나이트
    블록(EI-10a `views.presolve_view` → `presolve_block.assemble_presolve_view` →
    `_build_code_to_main_map`+`_build_shift_normalizer`). `code2main` 은
    `prelude.shift_manage_data`(DB 0, 순수 함수)에서,
    `id_to_main` 은 같은 `shift_definitions` 리스트에서 뽑는다(엔진과 같은 판정이어야 한다는
    presolve_block.py 의 docstring 그대로).
    """
    from services.cp_sat_basic import _build_shift_normalizer
    from services.roster_create_service import _build_code_to_main_map

    shift_defs = [_shift_definition_row(s) for s in stage_a.shift_lookup.get().values()]
    code2main = _build_code_to_main_map(prelude.shift_manage_data)
    id_to_main, _main_to_id = _build_shift_normalizer(shift_defs)
    return {"shift_definitions": shift_defs, "code2main": code2main, "id_to_main": id_to_main}


def _build_roster(
    prelude: PreludeState, stage_a: PrecheckStage, config: Mapping[str, Any],
) -> dict[str, Any]:
    """`roster` — 엔진 대상 명단(`_run_cp_sat_basic` :3464-3492 재현) + `nurse_id↔engine_index` 맵.

    `is_weekend_off` 는 `stage_a.weekend_ids`(이미 캐시된 Lazy — §1.3 ① '주말휴무 … A getter
    재사용' 그대로)로 주입한다. 실패하면 rcs :3490-3491 처럼 **삼키고** 명단은 그대로 돌려준다
    (리뷰 지적 LOW — 이전엔 바깥 `_safe_call` 이 함수 전체를 감싸 명단까지 함께 비었다. 운영은
    주말휴무 주입만 못 할 뿐 명단·index 맵은 그대로 돈다). `nid` 는 `nurse_id or db_id`(rcs
    :3481·:3484 그대로 — 이전엔 `nurse_id` 만 봤다). `off_first` 면 개인 OFF 한도(`o_min`/
    `o_max`/`o_exact`)를 지운다(:3491-3492 그대로, 새 dict 로 — 간호사 객체 자체는 건드리지
    않는다). `work_shifts`·`health_leave_extra_off`·`is_inbound`(MS-7)는 이미 각 간호사 dict
    에 들어 있다 — 전부 프렐류드 캡처 **이전**에 ORM `__dict__` 에 직접 쓰인 값이라(rcs
    :5488·:5693 등) 따로 짓지 않는다.
    """
    from services.roster_create_service import _build_engine_nurse_index_map

    nurses = [dict(n) for n in prelude.nurses_for_engine]
    try:
        weekend_ids = stage_a.weekend_ids.get()
        for nurse_dict in nurses:
            nid = str(nurse_dict.get("nurse_id") or nurse_dict.get("db_id") or "")
            if nid:
                nurse_dict["is_weekend_off"] = nid in weekend_ids
    except Exception:  # noqa: BLE001 — rcs 원문과 같은 폭("삼키고 무시", :3490-3491)
        pass
    if bool(config.get("off_first", False)):
        nurses = [{**n, "o_min": None, "o_max": None, "o_exact": None} for n in nurses]
    views = _as_nurse_views(prelude.nurses_for_engine)
    index_map = _build_engine_nurse_index_map(views)
    return {"nurses": nurses, "nurse_id_to_engine_index": {str(k): v for k, v in index_map.items()}}


def _build_settings_eff_input(
    config: Mapping[str, Any], demand: DemandPart, team: Mapping[str, Any], codebook: Mapping[str, Any],
    fixed_cells: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """`create_config_from_db` 가 받는 `config_data` 모양으로 병합한다(§1.3 L1 `config_data` 재현).

    프로덕션은 `_run_cp_sat_basic` 안에서 같은 `config_dict` 위에 차감 뒤 수요(앞 블록이 이미
    대입)·`team_min_by_team`/`team_handoff_policy_by_team`·`shift_definitions` 를 차례로
    얹는다 — 여기서도 같은 세 겹을 같은 순서로 합친다. `fixed_cells` 는 `create_config_from_db`
    자체는 안 읽지만(`.get()` 기반이라 모르는 키는 무해) `[WInjectFix]`(cp_sat_basic.py:947-985,
    리뷰 지적 M-G)가 **그 앞에서** 읽는 바로 그 키다 — 재료만 실어 둔다(주입은 D27 근사로
    남긴다, `_needs_w_inject_fix` 참고).
    """
    data = dict(config)
    post = demand.post
    if post is not None:
        if post.by_day is not None:
            data["daily_shift_requirements_by_day"] = post.by_day
        if post.max_by_day is not None:
            data["daily_shift_requirements_max_by_day"] = post.max_by_day
    data.update(team)
    shift_defs = codebook.get("shift_definitions")
    if shift_defs:
        data["shift_definitions"] = shift_defs
    if fixed_cells:
        data["fixed_cells"] = list(fixed_cells)
    return data


def _needs_w_inject_fix(
    fixed_cells: Sequence[Mapping[str, Any]], shift_defs: Sequence[Mapping[str, Any]],
    id_to_main: Mapping[str, str],
) -> bool:
    """`[WInjectFix]`(cp_sat_basic.py:947-971) 발동 조건만 재현한다 — 주입 자체는 안 한다(D27 근사,
    §6 O-19 — 전체 복제는 EI-13 이전으로 미룬다).

    `shift_definitions` 의 `type`(원문과 같은 '권위 있는 정의' 우선 — 고정 셀의 `shift_type` 은
    일괄 '근무'로 오라벨된다는 원문 주석 그대로)과 `id_to_main`(codebook, 같은
    `_build_shift_normalizer` 결과)로 같은 판정을 한다: '근무'형인데 D/E/N/O 로 정규화되지
    않는 고정 셀 코드(예: MID)가 있으면 발동 조건이다. `_normalize_shift_code` 를 제자리
    호출해(원문과 같은 함수) 정규화 자체는 복제하지 않는다.
    """
    from services.cp_sat_basic import _normalize_shift_code

    shift_type = {str(r.get("shift_id") or "").strip().upper(): str(r.get("type") or "").strip()
                 for r in shift_defs}
    for cell in fixed_cells:
        raw = str(cell.get("shift") or "").strip()
        if not raw or shift_type.get(raw.upper(), "") != "근무":
            continue
        normalized = _normalize_shift_code(raw, dict(id_to_main))
        if not normalized or str(normalized).upper() not in {"D", "E", "N", "O"}:
            return True
    return False


def _build_settings_eff(config_data: Mapping[str, Any]) -> dict[str, Any]:
    """`settings_eff` — `CPSATBasicEngine.create_config_from_db` 제자리 호출(§1.3 L2, 기록만).

    엔진 인스턴스는 `logger_prefix` 하나만 든다(`__init__` 이 그것뿐) — DB·세션이 없어 빌더의
    읽기 전용 원칙을 그대로 지킨다. 결과(`NurseRosterConfig`)는 `canonical()` 로 dict 화해 싣는다
    (§2.2 — `EngineInput` 부품은 평범한 dict/list 만, ORM·엔진 객체를 그대로 담지 않는다).
    """
    from services.cp_sat_basic import CPSATBasicEngine

    engine = CPSATBasicEngine()
    cfg = engine.create_config_from_db(dict(config_data))
    return canonical(cfg)


# ── post 6종(§1.5, D20 — 기록만) ─────────────────────────────────────────

#: 입력이 인라인(✘)이거나 이번 범위 밖이라 값을 채우지 않은 자리(§2.4-2 '인라인 ✘ 칸은
#: load_report.post_partial 로 표시'. `LoadReport` 에 그 칸이 없어 `post["_partial"]` 에 둔다).
_POST_PARTIAL_ALWAYS: tuple[str, ...] = (
    "leave_mask.resolve_code",       # 덮을 코드 해석 — Shift 조회가 인라인(§1.5 ④ ✘)
    "oncall.carry_helped",           # `_resolve_start_team` 앵커 체인 — 이번 범위 밖(§6)
    "work_shift_overrides.inputs",   # `roster.nurses[*].work_shifts` 메모리(§1.5 ⑥) — post 가
                                     # 실제로 읽는 **그 시점** 값은 따로 기록하지 않는다(roster
                                     # 부품 자체는 이제 있다 — O-15)
    "health_leave.active_range_map",  # ①의 재료 — `_active_range_in_month` 로 매 간호사마다
                                     # 다시 계산한다. 운영은 인바운드 기간·휴직/퇴사·영구
                                     # 전출로 깎은 `active_range_candidates`(rcs :5536-:5587·
                                     # :5681-:5686)를 넘긴다 — 근사(리뷰 지적 M-C, §6 O-20)
)


def _active_range(nurse: SimpleNamespace, month_start: date, days: int) -> tuple[int, int] | None:
    """`_active_range_in_month` 제자리 호출(①의 `active_range_map` 재료)."""
    from services.roster_create_service import _active_range_in_month

    return _active_range_in_month(nurse, month_start, days)


def _post_health_leave(db: Session, prelude: PreludeState) -> dict[str, Any]:
    """① 보건휴가 대상 집합 — `select_health_leave_targets` 제자리 호출(§1.5).

    `existing_requests` 는 `prelude.special_shift_requests`(운영 전체 — 소속+인바운드 전원)를
    쓴다(O-11 — 이전엔 `fixed_special_requests`(고정근무자만)로 근사해, 이미 그 달 보건휴가
    코드를 선점(원티드 제출)한 **일반** 간호사가 `already` 판정에서 빠지는 근사 오차가 있었다).
    """
    from services.leave.health_leave_planner import select_health_leave_targets

    ctx = prelude.ctx
    month_start = date(ctx.year, ctx.month, 1)
    days = calendar.monthrange(ctx.year, ctx.month)[1]
    nurses = _as_nurse_views(prelude.nurses_for_engine + prelude.fixed_nurses)
    active_range_map = {
        str(getattr(n, "nurse_id", "")): _active_range(n, month_start, days) for n in nurses
    }
    targets = select_health_leave_targets(
        db, group_id=ctx.group_id, year=ctx.year, month=ctx.month,
        engine_nurses=nurses, active_range_map=active_range_map,
        existing_requests=prelude.special_shift_requests,
    )
    return {"target_ids": sorted(targets)}


def _post_sleep_off(db: Session, prelude: PreludeState) -> dict[str, Any]:
    """② 수면OFF 입력 — `_load_requirements`·`_load_prev_month_tail`(앵커) 제자리 호출(§1.5)."""
    from services.leave.sleep_off_postprocess import _load_prev_month_tail, _load_requirements

    ctx = prelude.ctx
    requirements = _load_requirements(db, ctx)
    tail = _load_prev_month_tail(db, ctx.group_id, ctx.year, ctx.month)
    return {"requirements_days": len(requirements), "prev_month_tail": dict(tail)}


def _post_off_swap(db: Session, prelude: PreludeState) -> dict[str, Any]:
    """③ off_swap 입력 — 5 helper 제자리 호출(§1.5). 꼬리는 간호사마다 조회(N+1, 오프라인 허용)."""
    from services.cp_sat.off_swap import (
        _load_fixed_wanted_set,
        _load_nurses,
        _load_prev_month_tail,
        _resolve_shift_code_sets,
        _resolve_target_shift,
    )

    ctx = prelude.ctx
    target = _resolve_target_shift(db, ctx.group_id)
    n_codes, o_only_codes, weekly_off_codes = _resolve_shift_code_sets(db, ctx.group_id)
    fixed_wanted = _load_fixed_wanted_set(db, ctx.group_id, ctx.year, ctx.month)
    nurses = _load_nurses(db, ctx.group_id)
    tails = {
        nid: _load_prev_month_tail(db, nid, ctx.year, ctx.month)
        for nid in (str(n.get("nurse_id")) for n in prelude.nurses_for_engine)
        if nid in nurses
    }
    return {
        "target_shift_id": getattr(target, "shift_id", None),
        "n_codes": sorted(n_codes), "o_only_codes": sorted(o_only_codes),
        "weekly_off_codes": sorted(weekly_off_codes),
        "fixed_wanted_count": len(fixed_wanted), "nurse_count": len(nurses),
        "prev_month_tail": tails,
    }


def _post_leave_mask(db: Session, prelude: PreludeState) -> dict[str, Any]:
    """④ 휴직 가림 입력 — `_leave_spans` 만 제자리 호출(§1.5). 코드 해석은 ✘(Shift 인라인)."""
    from services.leave_mask_postprocess import _leave_spans

    ctx = prelude.ctx
    days = calendar.monthrange(ctx.year, ctx.month)[1]
    m_start, m_end = date(ctx.year, ctx.month, 1), date(ctx.year, ctx.month, days)
    nurse_ids = [str(n.get("nurse_id")) for n in prelude.nurses_for_engine]
    spans = _leave_spans(db, ctx.group_id, nurse_ids, m_start, m_end)
    return {"spans": {nid: [(lo.isoformat(), hi.isoformat()) for lo, hi in v]
                       for nid, v in spans.items()}}


def _post_oncall(db: Session, prelude: PreludeState) -> dict[str, Any]:
    """⑤ 콜 입력 — `call_code_map` 이 비면 그 그룹은 미사용(§1.5, 운영과 같은 게이트)."""
    from services.oncall_assign import load_call_code_map
    from services.oncall_postprocess import (
        _call_banned_eves,
        _members_asof,
        _protected_days,
        _team_map_asof,
    )

    ctx = prelude.ctx
    call_codes = load_call_code_map(db, ctx.group_id)
    if not call_codes:
        return {"call_codes": {}}
    month_start = date(ctx.year, ctx.month, 1)
    team_of = _team_map_asof(db, ctx.group_id, month_start)
    members = [asdict(m) for m in _members_asof(db, ctx.group_id, month_start)]
    protected = {n: sorted(d) for n, d in _protected_days(db, ctx.group_id, ctx.year, ctx.month).items()}
    banned_eves = {n: sorted(d) for n, d in
                   _call_banned_eves(db, ctx.group_id, ctx.year, ctx.month, config_id=ctx.config_id).items()}
    return {"call_codes": call_codes, "team_of": team_of, "members": members,
            "protected_days": protected, "banned_eves": banned_eves}


def _post_work_shift_overrides() -> dict[str, Any]:
    """⑥ 근무코드 덮기 — DB 조회 없음(메모리 `roster.nurses[*].work_shifts`, §1.5). 기록만.

    `roster` 부품은 이제 있다(O-15) — 다만 post 가 **그 시점**(ladder 를 거친 뒤)에 실제로
    읽는 값은 따로 스냅샷하지 않는다는 뜻에서 이 marker 는 그대로 둔다.
    """
    return {"source": "roster.work_shifts",
            "note": "엔진 입력 roster.nurses[*].work_shifts 를 읽는다(post 시점 값은 따로 기록하지 않는다)"}


def _record_post(db: Session, prelude: PreludeState) -> tuple[dict[str, Any], dict[str, str]]:
    """post 6종(§1.5, D20 — 기록만) — 부품 하나가 실패해도 나머지는 담는다."""
    errors: dict[str, str] = {}
    post: dict[str, Any] = {
        "health_leave": _safe_call(errors, "post.health_leave", _post_health_leave, db, prelude) or {},
        "sleep_off": _safe_call(errors, "post.sleep_off", _post_sleep_off, db, prelude) or {},
        "off_swap": _safe_call(errors, "post.off_swap", _post_off_swap, db, prelude) or {},
        "leave_mask": _safe_call(errors, "post.leave_mask", _post_leave_mask, db, prelude) or {},
        "oncall": _safe_call(errors, "post.oncall", _post_oncall, db, prelude) or {},
        "work_shift_overrides": _post_work_shift_overrides(),
        "_partial": list(_POST_PARTIAL_ALWAYS),
    }
    return post, errors


# ── solver_params · env_effective(기록만) ────────────────────────────────


def _solver_params(*, advanced_inference: bool) -> dict[str, Any]:
    """time_limit = 180(advanced_inference)/60(기본) — rcs :6794 식 그대로(기록만).

    키 이름은 설계서(DESIGN_ENGINE_INPUT.md §2.4 표 `solver_params`)의 `time_limit` 을
    따른다(리뷰 지적 — 이전엔 `time_limit_seconds` 였다). `distribution_mode` 는 이미
    `settings_raw`(= `config_dict`)에 있는 값이라 여기 또 넣으면 digest 에 중복 반영된다
    — 빼고 `settings_raw` 쪽만 정본으로 둔다.
    """
    return {"time_limit": 180 if advanced_inference else 60}


def _env_effective() -> dict[str, str]:
    """`ENGINE_PATH_NAMES` 의 지금 값(기록만, BR-4 — 호출 시점에 `os.environ` 을 읽는다)."""
    from services.engine_contract.env import ENGINE_PATH_NAMES

    return {name: os.environ[name] for name in ENGINE_PATH_NAMES if name in os.environ}


# ── D20 모드 스위치(C — 오프라인/운영 구분) ──────────────────────────────

#: "offline"(기본) — 지금까지의 동작 그대로(post 6종 직접 읽기 + raw 컬럼 읽기 + 차감 뒤
#: demand 를 9-0 함수로 재조회). "production" — D20("운영 단계 B 는 post·raw 를 읽지 않는다")
#: 을 지키는 모드. 호출처가 아직 0 이라 지금은 어느 쪽을 불러도 골든에 영향이 없다 — 이
#: 스위치는 EI-13a 가 `build_solve_stage`/`build_engine_input` 을 실제 프로덕션 자리에
#: 연결할 때 바로 쓸 수 있도록 지금 고정해 둔다(§0 지시 C, 리뷰 HIGH).
EngineInputMode = Literal["offline", "production"]


# ── 단계 B 산출물 ─────────────────────────────────────────────────────────


@dataclass
class SolveStage:
    """단계 B 산출물(§2.4-2) — 재조회 3종(§1.3 ④⑤⑥) + 차감 뒤 수요 + post 6종 + solver_params·env_effective."""

    allowed_constraints: dict[str, Any]
    cross_month_constraints: dict[str, Any]
    banned_constraints: dict[str, Any]
    #: ④⑤⑥ 병합(`EngineInput.cells["initial_constraints"]`) + 고정 셀 사본(리뷰 지적).
    cells: dict[str, Any]
    #: ⑤의 전월 꼬리 6종(`EngineInput.carryover`, 리뷰 지적 — 이전엔 버렸다).
    carryover: dict[str, Any]
    demand: DemandPart
    fixed_cut_summary: dict[str, Any]
    team: dict[str, Any]
    #: 근무코드 1벌(O-15) — `EngineInput.codebook`.
    codebook: dict[str, Any]
    #: 엔진 대상 명단 + `nurse_id↔engine_index` 맵(O-15) — `EngineInput.roster`.
    roster: dict[str, Any]
    #: `create_config_from_db` 결과(O-15) — `EngineInput.settings_eff`.
    settings_eff: dict[str, Any]
    post: dict[str, Any]
    solver_params: dict[str, Any]
    env_effective: dict[str, str]
    errors: dict[str, str]


def _applied_by_comparing(post: DemandSnapshot, pre: DemandSnapshot) -> bool:
    """`post` 가 `pre` 와 실제로 다른가(리뷰 지적 L15) — production 패스스루의 `applied` 를
    고정 `False` 로 적으면 '이미 차감된 값을 그대로 실었다'는 사실을 오도한다. 값으로 판정한다."""
    return post.by_day != pre.by_day or post.max_by_day != pre.max_by_day


def _demand_post_for_mode(
    db: Session, prelude: PreludeState, demand_pre: DemandSnapshot,
    first_attempt_config: Mapping[str, Any] | None, mode: EngineInputMode,
    errors: dict[str, str],
) -> tuple[DemandSnapshot | None, dict[str, Any]]:
    """차감 뒤 demand — 모드별로 출처가 다르다(C, D20). 실패·누락은 `errors` 에 직접 남긴다.

    "offline": 지금까지처럼 9-0 함수(`_compute_demand_post`)로 **다시 계산**한다(고정근무
    재조회 포함). "production": 재계산·재조회를 하지 않는다 — `first_attempt_config`(그 자리
    `config_dict`, production 호출부가 차감을 이미 끝낸 뒤 넘기는 값)의 수요 칸을 그대로
    스냅샷한다. D20 이 "운영 단계 B 는 post·raw 를 읽지 않는다"고 정한 것과 같은 이유다.

    `mode="production"` 인데 `first_attempt_config` 가 없으면(리뷰 지적 HIGH) **차감 전**
    `prelude.config_dict` 로 조용히 떨어지지 않는다 — `errors["demand.post"]` 에 남기고
    `post=None`(미확정)을 돌려준다. '단계 B 를 안 지은 경우'(`stage_b is None`)와 똑같은
    의미의 `None` 이라 `DemandPart.post` 의 기존 계약과 충돌하지 않는다(model.py docstring 갱신).
    """
    if mode == "production":
        if first_attempt_config is None:
            errors["demand.post"] = "production 모드는 first_attempt_config 필수"
            return None, _fixed_cut_summary(None, applied=False, reason="production_missing_config")
        post = _demand_snapshot(first_attempt_config)
        return post, _fixed_cut_summary(
            None, applied=_applied_by_comparing(post, demand_pre), reason="production_mode_passthrough")
    demand_post, fixed_cut_summary, cut_error = _compute_demand_post(db, prelude, demand_pre)
    if cut_error:
        errors["fixed_cut"] = cut_error
    return demand_post, fixed_cut_summary


def build_solve_stage(
    db: Session,
    prelude: PreludeState,
    stage_a: PrecheckStage,
    *,
    first_attempt_config: dict[str, Any] | None = None,
    advanced_inference: bool = False,
    mode: EngineInputMode = "offline",
    reads: tuple[dict, dict] | None = None,
) -> SolveStage:
    """단계 B(§2.4-2). `stage_a` 는 `team_min_by_team` 가공에 쓴다(§2.4-1 '그건 단계 B 의 몫').
    설계서 축약 서명(`db, stage_a, *, first_attempt_config`)에 `prelude` 를 더했다 — `stage_a`
    만으로는 ④⑤⑥·demand·post 가 쓰는 프렐류드 재료(간호사·설정·특수요청)에 닿지 못한다
    (호출처 0, §6 으로 남김). `first_attempt_config` 생략 시 `prelude.config_dict`(차감 **전**)
    로 근사한다 — 참값은 9c(§2.5) 가 프로덕션 첫 시도에서 캡처해 넘긴다.

    `mode="production"`(C, D20) 이면 post 6종·raw 컬럼·고정근무 차감 재조회를 하지 않는다 —
    ④⑤⑥ 재조회(§1.3)는 모드와 무관하게 그대로 돈다(이건 D20 이 가리키는 'post·raw' 가 아니라
    EI-13a 가 프로덕션 자리에서도 재조회하기로 정한 몫이다). raw 컬럼은 `build_engine_input`
    이 모드를 보고 건너뛴다(이 함수는 raw 를 모른다).

    조회 순서는 production `_run_cp_sat_basic` 의 순서(①주말 :3479 → ②팀 :3507 → ③근무코드
    :3549 → ④⑤⑥ :3593-:3638)를 따른다(리뷰 지적 M-F) — `stage_a` 의 세 getter 를 ④⑤⑥
    재조회보다 **먼저** 읽어 둔다(실패해도 삼키고 지나간다 — 각자 쓰는 자리(`team`·`codebook`·
    `roster`)가 다시 부르면 캐시된 예외가 그 자리에서 다시 기록된다, 격리 불변). 등급(⑦,
    :3700)은 `build_engine_input` 이 `build_solve_stage` **뒤**에 읽는다(§6 O-21).
    """
    config = first_attempt_config if first_attempt_config is not None else prelude.config_dict
    # 순서만 보존한다 — 실패는 삼키고(각자 소비처가 다시 호출해 자기 키로 기록한다) 지나간다.
    for prefetch in (stage_a.weekend_ids.get, stage_a.team_rows.get, stage_a.shift_lookup.get):
        with contextlib.suppress(Exception):
            prefetch()
    allowed, cross_month, banned, errors = _build_stage_b_queries(db, prelude, config, stage_a, reads)
    cells, carryover = _build_cells_and_carryover(prelude, config, allowed, cross_month, banned)
    demand_pre = _demand_snapshot(prelude.config_dict)
    demand_post, fixed_cut_summary = _demand_post_for_mode(
        db, prelude, demand_pre, first_attempt_config, mode, errors)
    demand = DemandPart(pre=demand_pre, post=demand_post)
    team = _safe_call(errors, "team", _build_team_dict, prelude, stage_a, config) or {}
    codebook = _safe_call(errors, "codebook", _build_codebook, prelude, stage_a) or {}
    roster = _safe_call(errors, "roster", _build_roster, prelude, stage_a, config) or {}
    settings_eff_input = _build_settings_eff_input(config, demand, team, codebook,
                                                   prelude.combined_fixed_cells)
    settings_eff = _safe_call(errors, "settings_eff", _build_settings_eff, settings_eff_input) or {}
    if _needs_w_inject_fix(prelude.combined_fixed_cells, codebook.get("shift_definitions") or (),
                           codebook.get("id_to_main") or {}):
        errors["settings_eff.pre_winject"] = (
            "cp_sat_basic [WInjectFix] 발동 조건 감지 — settings_eff 는 주입 전 config_data 로 "
            "지어졌다(D27 근사, §6 O-19)")
    if mode == "production":
        post, post_errors = {}, {}
    else:
        post, post_errors = _record_post(db, prelude)
    errors.update(post_errors)
    return SolveStage(
        allowed_constraints=allowed, cross_month_constraints=cross_month, banned_constraints=banned,
        cells=cells, carryover=copy.deepcopy(carryover),
        demand=copy.deepcopy(demand), fixed_cut_summary=fixed_cut_summary,
        team=copy.deepcopy(team), codebook=copy.deepcopy(codebook), roster=copy.deepcopy(roster),
        settings_eff=copy.deepcopy(settings_eff), post=post,
        solver_params=_solver_params(advanced_inference=advanced_inference),
        env_effective=_env_effective(), errors=errors,
    )


# ── 합본(단계 A + B) ──────────────────────────────────────────────────────


def _read_raw_column(
    db: Session, prelude: PreludeState, raw_reader: Callable[..., Any],
) -> tuple[dict[str, Any], dict[str, str]]:
    """9d raw 컬럼 3종을 전부 기록한다(load_report.raw_column) — 부품별 실패 허용."""
    ctx = prelude.ctx
    out: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for key in RAW_SETTING_KEYS:
        result = _safe_call(errors, f"raw.{key}", raw_reader, db, key,
                            office_id=ctx.office_id, group_id=ctx.group_id)
        if result is not None:
            out[key] = result.value
    return out, errors


def _assemble_engine_input(
    ctx: Ctx, prelude: PreludeState, grade: dict[str, Any],
    stage_b: SolveStage | None, raw_column: dict[str, Any], errors: dict[str, str],
) -> EngineInput:
    """stage_a·stage_b 산출물을 `EngineInput` 한 벌로 합친다(실패분은 기본값으로 비운다).

    `settings_raw`·`preferences`·`grade`는 `prelude`/`stage_a`가 들고 있는 바로 그 객체가
    아니라 **깊은 사본**이다(BR-6, 리뷰 지적 — 이전엔 참조를 그대로 실어 `EngineInput` 을 받은
    쪽이 값을 바꾸면 prelude·캐시까지 같이 바뀌었다). `cells`는 `stage_b`가 이미 깊은 사본으로
    지어 둔 것을 그대로 쓴다(`_build_cells_and_carryover`).
    """
    base = dict(settings_raw=copy.deepcopy(prelude.config_dict), grade=copy.deepcopy(grade),
                preferences=copy.deepcopy(prelude.preferences))
    if stage_b is None:
        cells = {"combined_fixed_cells": copy.deepcopy(prelude.combined_fixed_cells)}
        return EngineInput(ctx=ctx, demand=DemandPart(pre=DemandSnapshot()), cells=cells,
                           load_report=LoadReport(errors=errors, raw_column=raw_column), **base)
    load_report = LoadReport(errors={**errors, **stage_b.errors}, raw_column=raw_column,
                             fixed_cut=stage_b.fixed_cut_summary)
    return EngineInput(
        ctx=ctx, team=stage_b.team, codebook=stage_b.codebook, roster=stage_b.roster,
        settings_eff=stage_b.settings_eff, demand=stage_b.demand, post=stage_b.post,
        cells=stage_b.cells, carryover=stage_b.carryover,
        solver_params=stage_b.solver_params, env_effective=stage_b.env_effective,
        load_report=load_report, **base,
    )


def build_engine_input(
    db: Session,
    prelude: PreludeState,
    *,
    first_attempt_config: dict[str, Any] | None = None,
    advanced_inference: bool = False,
    raw_reader: Callable[..., Any] = read_raw_setting,
    mode: EngineInputMode = "offline",
    stage_a: PrecheckStage | None = None,
    reads: tuple[dict, dict] | None = None,
    stage_out: dict[str, Any] | None = None,
) -> EngineInput:
    """단계 A + B 합본(§2.4-2) — raise 하지 않는다. 실패는 `load_report.errors` 에 남긴다.

    부품별 실패는 이미 `_safe_call`(등급·단계 B·raw 컬럼 각각)이 잡는다. 이 함수는 그 부품별
    방어선 **바깥**에서 터질 수 있는 것(조립 자체의 예상 못 한 예외 등)에 대한 마지막
    방어선이다(리뷰 지적 — §0 '빌더는 … raise 하지 않음' 을 함수 전체에 걸쳐 보장한다).

    `mode="production"`(C, D20) 이면 `raw_column` 을 읽지 않는다(`raw_reader` 호출 0) —
    `build_solve_stage` 로 넘긴 같은 `mode` 가 post·고정근무 재조회도 0 으로 만든다. 기본
    `mode="offline"` 은 지금까지의 동작과 바이트 같다(9c·골든이 쓰는 경로).

    `mode` 가 `{"offline", "production"}` 밖이면(오타 등, 리뷰 지적 LOW) 조용히 `"offline"`
    으로 떨어지지 않는다 — D20 위반이 소리 없이 일어나는 쪽(조회가 생기는 쪽)보다 안전한
    `"production"`(조회 0)으로 처리하고 `errors["mode"]` 에 남긴다.

    `stage_a`(EI-13a-2)를 주면 새로 짓지 않고 그것을 쓴다(지연 getter 재사용 — 이미 읽힌 getter 는 조회 0).
    `reads=(허용근무 메모, 월경계 메모)` 를 주면 ④⑤ 가 읽은 행을 거기에 담는다(rcs `_memo_read`).
    `stage_out` 을 주면 단계 B 산출물을 `stage_out["stage_b"]` 에 실어 준다(`AttemptSource` 가 ⑥ 을 가져간다).
    셋 다 기본값이면 지금과 바이트 같다(오프라인·골든·9c).
    """
    ctx = _days_filled_ctx(prelude.ctx)
    errors: dict[str, str] = {}
    safe_mode = mode if mode in ("offline", "production") else "production"
    if safe_mode != mode:
        errors["mode"] = f"알 수 없는 mode={mode!r} — 조회 0(production)으로 처리"
    try:
        stage_a = stage_a if stage_a is not None else build_precheck_stage(db, prelude)
        # 등급(⑦, rcs :3700)은 단계 B(①②③④⑤⑥) **뒤**에 읽는다(리뷰 지적 M-F — production
        # 순서와 맞춘다. 호출 순서만 바뀔 뿐 값·실패 격리는 그대로다).
        stage_b = _safe_call(errors, "solve_stage", build_solve_stage, db, prelude, stage_a,
                             first_attempt_config=first_attempt_config,
                             advanced_inference=advanced_inference, mode=safe_mode, reads=reads)
        if stage_out is not None:
            stage_out["stage_b"] = stage_b
        grade = _safe_call(errors, "grade", stage_a.grade.get) or {}
        if safe_mode == "production":
            raw_column, raw_errors = {}, {}
        else:
            raw_column, raw_errors = _read_raw_column(db, prelude, raw_reader)
        errors.update(raw_errors)
        return _assemble_engine_input(ctx, prelude, grade, stage_b, raw_column, errors)
    except Exception as exc:  # noqa: BLE001 — 마지막 방어선, 폭은 `_safe_call` 과 같다
        errors["build"] = f"{type(exc).__name__}: {exc}"
        return EngineInput(ctx=ctx, demand=DemandPart(pre=DemandSnapshot()),
                           load_report=LoadReport(errors=errors))


# ══════════════════════════════════════════════════════════════════════════
# EI-13a-2 — 프로덕션 첫 시도 직전 1회 호출 · 시도마다 재조회 7종 제거
# ══════════════════════════════════════════════════════════════════════════

#: 이 키가 하나라도 `errors` 에 있으면 `AttemptSource` 를 만들지 않는다(A7) — DB 부품(허용근무·월경계·금지 원티드·팀·근무코드·
#: 명단·등급·단계 B·합본) 오류는 지금 경로(`_run_cp_sat_basic` 재조회)가 같은 자리에서 같은 오류를 내게 둔다.
#: `settings_eff`·`settings_eff.pre_winject` 는 DB 부품이 아니라 계산 기록이라 막지 않는다.
_ATTEMPT_SOURCE_BLOCKING_KEYS: tuple[str, ...] = (
    *ENGINE_BLOCKING_ERROR_KEYS, "team", "codebook", "roster", "grade", "solve_stage", "build", "demand.post",
)


@dataclass
class AttemptSource:
    """첫 시도 직전 빌더가 읽은 DB 부품 — 시도마다 `_run_cp_sat_basic` 이 재조회 대신 쓴다(평범한 값만).

    ①~⑦ 가운데 읽기만 담는다. 시도마다 달라지는 계산(월경계 꼬리 자르기·허용근무 일자 해석)은 시도 config 로 다시 돌고,
    여기서는 그 재료(행)만 공유한다 — 사다리가 `n2n_min_gap` 을 바꾸면 꼬리 길이가 달라지기 때문이다(SPEC A3).
    `nurse_ids` 는 이 부품이 지어진 명단이다 — 다른 명단으로 부르면 쓰지 않는다(`matches`).
    """

    nurse_ids: tuple[str, ...]
    weekend_ids: frozenset[str]
    team_rows: list[SimpleNamespace]
    shift_defs: list[dict[str, Any]]
    allowed_reads: dict[Any, Any]
    cross_month_reads: dict[Any, Any]
    banned: dict[str, Any]
    grade: dict[str, Any]

    def matches(self, nurses_in_group: Sequence[Any]) -> bool:
        """이 부품이 `nurses_in_group` 명단용인가 — 주말휴무·금지 원티드는 명단에 따라 달라진다."""
        return tuple(str(getattr(n, "nurse_id", None)) for n in nurses_in_group) == self.nurse_ids


def _plain_team_row(team: Any) -> SimpleNamespace:
    """팀 행 → 평범한 값. 엔진 쪽이 읽는 세 칸은 `getattr` 로 명시해 만료된 행에서도 값이 빠지지 않게 한다."""
    from .views import _plain_vars

    explicit = {key: getattr(team, key) for key in ("team_id", "min_shift", "handoff_policy")}
    return SimpleNamespace(**{**_plain_vars(team), **explicit})


def _attempt_source(stage_a: PrecheckStage, ei: EngineInput, stage_b: SolveStage | None,
                    reads: tuple[dict, dict]) -> AttemptSource | None:
    """`AttemptSource` 를 짓는다 — DB 부품 오류가 하나라도 있으면 `None`(A7)."""
    if stage_b is None or any(key in ei.load_report.errors for key in _ATTEMPT_SOURCE_BLOCKING_KEYS):
        return None
    try:
        weekend_ids = frozenset(stage_a.weekend_ids.get())
        team_rows = [_plain_team_row(t) for t in stage_a.team_rows.get()]
    except Exception:  # noqa: BLE001 — 읽지 못한 부품은 지금 경로(재조회)가 같은 자리에서 다시 겪는다
        return None
    return AttemptSource(
        nurse_ids=tuple(str(n.get("nurse_id")) for n in stage_a.nurses_for_engine),
        weekend_ids=weekend_ids, team_rows=team_rows,
        shift_defs=copy.deepcopy(ei.codebook.get("shift_definitions") or []),
        allowed_reads=reads[0], cross_month_reads=reads[1],
        banned=copy.deepcopy(stage_b.banned_constraints), grade=copy.deepcopy(ei.grade),
    )


def build_engine_input_at_first_attempt(
    db: Session, stage_a: PrecheckStage, *, preferences: Sequence[Any],
    first_attempt_config: dict[str, Any], advanced_inference: bool = False,
) -> tuple[EngineInput, AttemptSource | None]:
    """첫 `_run_cp_sat_basic` 직전 1회 — production 모드 합본 + 시도마다 쓸 `AttemptSource`.

    단계 A 는 `stage_a` 의 사본(precheck 가 이미 읽은 getter 는 조회 0)으로 `PreludeState` 를 다시 묶는다.
    조회 순서는 지금 첫 시도 안 순서(①주말 → ②팀 → ③근무코드 → ④ → ⑤ → ⑥ → ⑦등급)의 부분열이다.
    raise 하지 않는다 — 실패하면 `(빈 EngineInput, None)` 이고, 호출처는 지금 경로(재조회)로 간다.
    """
    allowed_reads: dict = {}
    cross_reads: dict = {}
    try:
        prelude = PreludeState(
            ctx=stage_a.ctx, config_dict=copy.deepcopy(stage_a.config_dict),
            nurses_for_engine=copy.deepcopy(stage_a.nurses_for_engine),
            combined_fixed_cells=copy.deepcopy(stage_a.combined_fixed_cells),
            shift_manage_data=copy.deepcopy(stage_a.shift_manage_data),
            preferences=copy.deepcopy(list(preferences)),
        )
        out: dict[str, Any] = {}
        ei = build_engine_input(
            db, prelude, first_attempt_config=copy.deepcopy(first_attempt_config),
            advanced_inference=advanced_inference, mode="production", stage_a=stage_a,
            reads=(allowed_reads, cross_reads), stage_out=out)
        return ei, _attempt_source(stage_a, ei, out.get("stage_b"), (allowed_reads, cross_reads))
    except Exception as exc:  # noqa: BLE001 — 마지막 방어선(빌더는 raise 하지 않는다)
        ctx = _days_filled_ctx(stage_a.ctx)
        return (EngineInput(ctx=ctx, demand=DemandPart(pre=DemandSnapshot()),
                            load_report=LoadReport(errors={"build": f"{type(exc).__name__}: {exc}"})), None)
