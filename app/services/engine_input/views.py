"""EI-10a·10b 소비자 전환 — 탐지기·precheck 입력을 단계 A(`PrecheckStage`) view 로(호출처는 rcs 뿐).

`EI10AB_SPEC.md` §2.1 을 그대로 따른다. 이 모듈이 하는 일은 세 가지뿐이다.

  - `stage_a_at_precheck` — 운영 전용(D27). 이 자리 지역 변수로 `PreludeState` 를 지어
    `build_precheck_stage(live=...)` 를 부른다. 조회는 0 이다(`Lazy` thunk 만 연다).
  - `presolve_view` — 나이트 과공급 탐지기 입력(`PresolveView`)을 `stage_a` + 그 자리
    `config_dict`(고정근무 차감 뒤)로 짓는다. `services.precheck.presolve_block.
    assemble_presolve_view` 제자리 호출이다.
  - `to_precheck_input` — `run_runtime_precheck` 호출 한 벌(`PrecheckCall`)을 `stage_a` +
    그 자리 `config_dict` 로 짓는다. 등급 → 주말휴무 → 투영 → 팀(`team_min_by_team` 이
    없을 때만) 순서로 getter 를 연다 — 조회 자리·순서·예외 처리는 전환 전과 같다(§3.2).
    고정 칸은 EI-10d 부터 번역본이다(`precheck.fixed_cell_codes` — 엔진 색인·원코드를
    `nurse_id`·메인코드로, 조회 0).

★ `precheck/presolve_block.py` 모듈 docstring '순환 import 주의'와 같은 규약 — rcs(
  `services.roster_create_service`)는 이 모듈을 함수 **안에서만** 지연 import 한다(여기도
  반대로 rcs 를 최상단에서 끌어오지 않는다).
"""
from __future__ import annotations

import calendar
import contextlib
import copy
import io
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from services.precheck.fixed_cell_codes import precheck_fixed_cells
from services.precheck.presolve_block import PresolveView, assemble_presolve_view
from sqlalchemy.orm import Session

from .builder import PrecheckLive, PrecheckStage, _as_nurse_views, build_precheck_stage
from .model import Ctx, PreludeState

#: [온톨로지 데이터 단일 소스] precheck 투영 26칸 — rcs 옛 `_ONTOLOGY_NURSE_FIELDS`(§2.1)
#: 원문 그대로 이동. 정찰/온톨로지에 넘길 nurse 속성은 손으로 고르지 않고 solver 가 보는
#: 전체를 포괄한다 — 개별 속성이 누락되면 온톨로지가 그 제약을 못 보는 사각지대(예:
#: n_exact 누락으로 야간 모순 미검출)가 생기므로, 새 속성 추가 시 이 목록 한 곳만 고치면
#: 되게 한다(`_precheck_profile` 의 `getattr` 가 lazy-load 도 강제 로드한다, U6). 세부(옛
#: rcs 주석 원문, 리뷰 지적 LOW — 되살림):
#:   - 월 한도(d/e/n/o × min/max/exact): 오버레이로 엔진 nurse 에 실린다(없으면 None)
#:   - fixed_shift/allowed_shifts: 대상월 as-of 가 반영된 값
#:   - is_weekend_off: 이 목록이 아니라 period SSOT(as-of)로 별도 확정(`_precheck_profile`)
ONTOLOGY_NURSE_FIELDS: tuple[str, ...] = (
    "nurse_id", "grade", "team_id", "active", "sequence",
    "joining_date", "resignation_date",
    "allowed_shifts", "work_shifts", "fixed_shift",
    "weekly_off_enabled", "weekly_off_weekday",
    "personal_off_adjustment", "preceptor_id",
    "d_min", "d_max", "d_exact", "e_min", "e_max", "e_exact",
    "n_min", "n_max", "n_exact", "o_min", "o_max", "o_exact",
)


def _is_orm_value(value: Any) -> bool:
    """값이 ORM 인스턴스(또는 그런 원소를 담은 list/tuple/set)인가 — `_sa_instance_state`
    로 판별한다. model.py 의 'ORM 금지'(평범한 dict/list 만)를 지키고, 뒤에 오는
    `deepcopy` 가 SQLAlchemy `InstanceState` 를 건드리지 않게 하기 위해서다."""
    if hasattr(value, "_sa_instance_state"):
        return True
    if isinstance(value, (list, tuple, set)):
        return any(hasattr(item, "_sa_instance_state") for item in value)
    return False


def _plain_vars(obj: Any) -> dict[str, Any]:
    """`vars(obj)` 에서 `_sa_*` 키와 ORM 값(관계 속성)을 뺀 평범한 dict.

    `vars()` 는 `__dict__` 를 그대로 읽을 뿐이라 지연 로딩을 일으키지 않는다(조회 0).
    """
    return {
        key: value for key, value in vars(obj).items()
        if not str(key).startswith("_sa_") and not _is_orm_value(value)
    }


def _plain_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """dict 행에서 `_sa_*` 키와 ORM 값(관계 속성)을 뺀다 — `shift_manage_data` 원소는
    `s.__dict__` 라 `_sa_instance_state` 를 들고 있고(rcs:943), office·group 관계가 로드돼
    있으면 값으로도 ORM 객체를 들고 있을 수 있다(리뷰 지적 LOW — `_plain_vars` 와 같은
    필터를 빠뜨리면 `PrecheckStage` 사본에 ORM 객체가 들어간다)."""
    return {
        key: value for key, value in dict(row).items()
        if not str(key).startswith("_sa_") and not _is_orm_value(value)
    }


def stage_a_at_precheck(
    db: Session,
    *,
    office_id: str,
    group_id: str,
    year: int,
    month: int,
    config_id: int | None,
    job_id: str | None,
    config_dict: Mapping[str, Any],
    nurses_for_engine: Sequence[Any],
    combined_fixed_cells: Sequence[Mapping[str, Any]],
    shift_manage_data: Sequence[Mapping[str, Any]],
) -> PrecheckStage:
    """단계 A — 운영 전용(D27, 이 자리 지역 변수로 `PreludeState` 를 짓는다).

    `PreludeState` 는 단계 A(`build_precheck_stage`)가 읽는 5칸(`ctx`·`config_dict`·
    `nurses_for_engine`·`combined_fixed_cells`·`shift_manage_data`)만 채운다 — 나머지
    칸(`fixed_nurses` 등)은 EI-13a(단계 B 운영 첫 호출)의 몫이다. 이 호출 자체는 DB 를
    읽지 않는다(`vars()`·얕은 사본·`deepcopy`뿐 — `Lazy` 네 thunk 만 연다).

    `live=PrecheckLive(...)` 를 함께 넘겨 탐지기·precheck 가 **실물 참조**(사본 아님)를
    쓰게 한다(§0-1) — 호출부가 넘긴 그 객체(리스트) 그대로를 들고 있는다(`list()` 등으로
    다시 감싸지 않는다 — 그러면 원소는 같아도 리스트 객체 정체성이 깨진다. `view.nurses is
    nurses_for_engine` 를 지키려면 이 자리부터 같은 객체여야 한다). `config_dict` 는
    `PrecheckLive` 에 넣지 않는다(§0-2, 차감 전/뒤가 읽는 시점에 따라 갈리는 함정을 막기
    위해서다).
    """
    ctx = Ctx(office_id=office_id, group_id=group_id, year=year, month=month,
             config_id=config_id, job_id=job_id)
    prelude = PreludeState(
        ctx=ctx,
        config_dict={k: v for k, v in config_dict.items() if not str(k).startswith("_sa_")},
        nurses_for_engine=[_plain_vars(n) for n in nurses_for_engine],
        combined_fixed_cells=[_plain_row(r) for r in combined_fixed_cells],
        shift_manage_data=[_plain_row(r) for r in shift_manage_data],
    )
    live = PrecheckLive(
        nurses_for_engine=nurses_for_engine,
        combined_fixed_cells=combined_fixed_cells,
        shift_manage_data=shift_manage_data,
    )
    return build_precheck_stage(db, prelude, live=live)


def _require_stage(stage_a: PrecheckStage | None) -> PrecheckStage:
    """`stage_a` 가 없으면(단계 A 를 짓지 않은 자리에서 불렸다는 뜻) `RuntimeError`."""
    if stage_a is None:
        raise RuntimeError("단계 A(stage_a) 없음 — presolve_view 는 stage_a_at_precheck 뒤에만 부른다")
    return stage_a


def _sources(stage_a: PrecheckStage) -> dict[str, Any]:
    """`stage_a.live` 가 있으면 운영 실물 참조, 없으면(오프라인) 사본을 `_as_nurse_views` 로 감싼다."""
    if stage_a.live is not None:
        return {
            "nurses": stage_a.live.nurses_for_engine,
            "shift_manage_data": stage_a.live.shift_manage_data,
            "combined_fixed_cells": stage_a.live.combined_fixed_cells,
        }
    return {
        "nurses": _as_nurse_views(stage_a.nurses_for_engine),
        "shift_manage_data": stage_a.shift_manage_data,
        "combined_fixed_cells": stage_a.combined_fixed_cells,
    }


def presolve_view(stage_a: PrecheckStage | None, config_dict: dict[str, Any]) -> PresolveView:
    """나이트 과공급 탐지기 입력 — 단계 A(간호사·고정 칸·근무코드) + 이 자리 `config_dict`
    (고정근무 차감 뒤)로 짓는다(`EI10AB_SPEC.md` §2.1).

    운영(`stage_a.live` 있음): `view.nurses is` 그 자리 엔진 간호사 실물 리스트 ·
    `view.config_dict is config_dict` — 전환 전 나이트 블록의 옛 `build_presolve_view`
    호출(지금은 삭제됨, §2.1)과 같은 객체·같은 값이다. 오프라인(`live` 없음): 간호사는
    `_as_nurse_views` 로 감싼 사본이다(`_build_engine_nurse_index_map` 이 `n.nurse_id`
    속성 접근을 요구해서다).

    `config_dict` 는 읽기만 한다 — 바꾸지 않는다.
    """
    stage = _require_stage(stage_a)
    src = _sources(stage)
    return assemble_presolve_view(
        nurses=src["nurses"],
        shift_manage_data=src["shift_manage_data"],
        combined_fixed_cells=src["combined_fixed_cells"],
        config_dict=config_dict,
        load_shift_lookup=stage.shift_lookup.get,
        num_days=stage.ctx.days,
        year=stage.ctx.year,
        month=stage.ctx.month,
    )


@dataclass(frozen=True)
class PrecheckCall:
    """`run_runtime_precheck` 인자 한 벌 — 지금 rcs 의 그 호출과 같은 객체들(§2.1).

    `fixed_cells` 만 예외다(EI-10d): 단계 A 의 엔진 형식 고정 칸 실물이 아니라 precheck 가 읽는 번역본
    `[{nurse_id, day_index, shift}]` 이다 — 실물을 그대로 넘기는 것이 결함 ②(칸을 전부 버림)의 원인이었다.
    """

    nurses_dict: list[dict[str, Any]]
    config_dict: dict[str, Any]
    grade_config: dict[str, Any]
    fixed_cells: Sequence[Mapping[str, Any]]
    year: int
    month: int

    def kwargs(self) -> dict[str, Any]:
        """`run_runtime_precheck(**call.kwargs())` 로 — 키워드 호출이어야 캡처 층의
        `bind()`(9c `_PrecheckNursesCapture`)가 깨지지 않는다(§2.5 M16)."""
        return {
            "nurses_dict": self.nurses_dict,
            "config_dict": self.config_dict,
            "grade_config": self.grade_config,
            "fixed_cells": self.fixed_cells,
            "year": self.year,
            "month": self.month,
            "stop_on_config_error": False,
        }


def _weekend_ids_or_empty(stage_a: PrecheckStage) -> set[str]:
    """주말휴무 getter — 실패하면(로그 없이) `set()`(지금 rcs 의 자체 `except Exception`과 같다)."""
    try:
        return stage_a.weekend_ids.get()
    except Exception:  # noqa: BLE001 — 지금과 같은 폭(로그 없이 삼킨다)
        return set()


def _precheck_profile(nurse: Any, weekend_ids: set[str]) -> dict[str, Any]:
    """간호사 한 명 → precheck 투영 dict — `ONTOLOGY_NURSE_FIELDS` + `db_id`·`name`·
    `is_weekend_off`(이 삽입 순서까지 지금과 같다)."""
    profile = {field: getattr(nurse, field, None) for field in ONTOLOGY_NURSE_FIELDS}
    profile["db_id"] = profile.get("nurse_id")
    profile["name"] = getattr(nurse, "name", None)
    profile["is_weekend_off"] = str(profile.get("nurse_id")) in weekend_ids
    return profile


def _inject_team_min(
    stage_a: PrecheckStage, precheck_config: dict[str, Any], nurses_dict: list[dict[str, Any]],
    log: Callable[[str], None],
) -> None:
    """팀 getter — `team_min_by_team` 이 이미 있으면 **읽지 않고** 바로 반환한다(§2.1 5단계).

    `build_team_min_by_team` 은 **생성 경로와 같은 함수**를 쓴다(옛 rcs 주석 원문, 리뷰
    지적 LOW — 되살림). 예전엔 precheck 가 여기서만 `teams.min_shift` 를 무시하고 디폴트만
    넣어서, 사전검증과 실제 생성이 서로 다른 제약을 검사했다(저장값이 엄하면 통과 후 솔버
    실패, D 를 0 으로 둔 팀이면 없는 제약으로 오블로킹).

    실패(조회·빌드 어느 쪽이든)는 지금과 같은 문구로 로그만 남기고 삼킨다 — 생성을 막지
    않는다. `build_team_min_by_team` 은 rcs 모듈 속성으로 지연 import 한다(패치가 먹게,
    R4).
    """
    if "team_min_by_team" in precheck_config:
        return
    try:
        from services.roster_create_service import build_team_min_by_team

        team_rows = stage_a.team_rows.get()
        member_team_ids = {
            str(n.get("team_id")) for n in nurses_dict
            if n.get("team_id") not in (None, "", 0)
        }
        team_min_by_team = build_team_min_by_team(
            team_rows, member_team_ids, bool(precheck_config.get("use_mid", False)),
        )
        if team_min_by_team:
            precheck_config["team_min_by_team"] = team_min_by_team
    except Exception as exc:  # noqa: BLE001 — 지금과 같은 폭(team_min 실패는 생성을 막지 않는다)
        log(f"[Precheck] team_min 로딩 실패(무시): {exc}")


def _precheck_cells_or_empty(
    src: dict[str, Any], config_dict: Mapping[str, Any], log: Callable[[str], None],
) -> list[dict[str, Any]]:
    """고정 칸 번역(EI-10d) — 실패하면 로그 한 줄을 남기고 빈 목록(= 10d 이전과 같은 '칸 없이 판정').

    번역은 조회 0 인 순수 함수뿐이라 이론상 실패하지 않는다. 그래도 생성을 막지 않는다 — 실패하면
    precheck 전체가 건너뛰어지는 쪽(바깥 except)보다 칸만 빠진 판정이 낫다(팀 getter 와 같은 폭).
    배정 차단일(`config_dict["blocked_by_nurse_id"]` — 이미 메모리에 있다) 위의 칸은 번역에서 버린다: 엔진은 활동일 밖
    고정 칸을 건너뛰므로(fallback_lex.py:1311) 남기면 오차단이다.
    """
    try:
        return precheck_fixed_cells(
            src["nurses"], src["combined_fixed_cells"], src["shift_manage_data"],
            use_mid=bool(config_dict.get("use_mid", False)),
            blocked_by_nurse_id=config_dict.get("blocked_by_nurse_id"),
        )
    except Exception as exc:  # noqa: BLE001 — 번역 실패는 생성을 막지 않는다(칸 없이 판정)
        log(f"[Precheck] 고정 칸 변환 실패(무시 — 고정 칸 없이 판정): {type(exc).__name__}: {exc}")
        return []


def _engine_fixed_days(nurses: list, combined_fixed_cells: Any) -> set[tuple[str, int]]:
    """엔진 형식 고정 칸이 있는 (간호사 id, 날) — 엔진은 그 날의 금지·강제 OFF 를 무시한다(고정이 이긴다). 사전·사후가 같이 쓴다(EI-10g4·Dr)."""
    from services.precheck.presolve_block import _fixed_cells_by_nurse_id

    return {
        (str(row["nurse_id"]), int(row["day_index"]))
        for row in _fixed_cells_by_nurse_id(list(nurses), combined_fixed_cells)
        if row.get("nurse_id") is not None and isinstance(row.get("day_index"), int)
    }


def _without_fixed_days(
    ic: dict[str, Any], fixed_days: set[tuple[str, int]],
) -> dict[str, Any]:
    """`initial_constraints` 에서 고정 칸이 있는 (간호사, 날)의 forced_off·forbidden 을 뺀 새 dict(원본 불변) — `{"forced_off", "forbidden"}` 두 칸 항상."""
    forced_off: dict[str, list[int]] = {}
    for nid, days in (ic.get("forced_off") or {}).items():
        kept = sorted({int(d) for d in days if (str(nid), int(d)) not in fixed_days})
        if kept:
            forced_off[str(nid)] = kept
    forbidden: dict[str, dict[int, list[str]]] = {}
    for nid, by_day in (ic.get("forbidden") or {}).items():
        kept_days = {int(d): list(codes) for d, codes in (by_day or {}).items()
                     if (str(nid), int(d)) not in fixed_days and codes}
        if kept_days:
            forbidden[str(nid)] = kept_days
    return {"forced_off": forced_off, "forbidden": forbidden}


def _attach_stage_b(
    precheck_config: dict[str, Any], ei: Any, src: dict[str, Any], log: Callable[[str], None],
) -> None:
    """단계 B 가 짓은 제약(허용근무 기간표 ④ · 전월 경계 ⑤ · 금지 원티드 ⑥ · 월중 경계 병합본)을 엔진 키 이름 그대로 싣는다(EI-10g4 · D-D3).

    `precheck_config["initial_constraints"]` = `{"forced_off": {id: [일]}, "forbidden": {id: {일: [코드]}}}` — 엔진이 쓰는 모양 그대로.
    - 단계 B 부품이 하나라도 실패했으면(`engine_blocking_errors`) 싣지 않는다 — 엔진도 그 경우 시도마다 재조회해 같은 오류를 겪는다(D-D7).
    - 엔진은 **고정 칸이 있는 날**의 금지·강제 OFF 를 무시한다(고정이 이긴다 — `fallback_lex.py:1594`·`cp_sat_basic.py:1395`). 엔진 형식 고정 칸
      (`ei.cells["combined_fixed_cells"]`)이 하나라도 있는 (간호사, 날)은 여기서 뺀다 — precheck 번역은 모델링 못 하는 코드의 칸을 버리므로
      그 날을 비어 있다고 오해하지 않게 원본 칸으로 거른다.
    - 비어 있으면 키를 만들지 않는다(골든 L0 변동 최소화). 다만 값이 든 키가 이미 있으면(월중 경계 몫 — rcs 가 precheck 앞에서 심고 병합본도
      그것을 담는다) 걸러진 결과가 비어도 그것으로 바꾼다 — 남기면 엔진이 무시하는 고정 칸 날의 강제 OFF·금지가 거짓 부족을 낸다(12y).
      예외는 삼킨다(precheck 는 단계 B 몫 없이 옛 판정).
    """
    try:
        from services.engine_input.builder import engine_blocking_errors
        if ei is None or engine_blocking_errors(getattr(getattr(ei, "load_report", None), "errors", None) or {}):
            return
        merged = (getattr(ei, "cells", None) or {}).get("initial_constraints") or {}
        kept_ic = _without_fixed_days(merged, _engine_fixed_days(list(src["nurses"]), (ei.cells or {}).get("combined_fixed_cells")))
        forced_off = kept_ic["forced_off"]
        forbidden = kept_ic["forbidden"]
        if forced_off or forbidden or precheck_config.get("initial_constraints"):
            precheck_config["initial_constraints"] = {"forced_off": forced_off, "forbidden": forbidden}
    except Exception as exc:  # noqa: BLE001 — 단계 B 몫을 못 싣는 것은 생성을 막지 않는다(옛 판정으로)
        log(f"[Precheck] 단계 B 제약 싣기 실패(무시): {type(exc).__name__}: {exc}")


def to_precheck_input(
    stage_a: PrecheckStage | None, config_dict: dict[str, Any], *,
    ei: Any = None, log: Callable[[str], None] = print,
) -> PrecheckCall:
    """`run_runtime_precheck` 호출 한 벌 — 단계 A(간호사·등급·주말·팀·고정 칸) + 이 자리 `config_dict`
    (고정근무 차감 뒤)로 짓는다(`EI10AB_SPEC.md` §2.1). `ei`(첫 시도 직전 빌더 결과 — EI-10g3)가 있으면 단계 B 제약을 엔진 키 이름
    그대로 `precheck_config` 사본에 싣는다(`_attach_stage_b` — 원본 `config_dict` 는 건드리지 않는다).

    순서가 곧 쿼리 순서다 — 등급 → 주말휴무 → 투영 → `config_dict` 얕은 사본 → 팀
    (`team_min_by_team` 없을 때만) → 고정 칸 번역(조회 0). 등급 getter 의 예외는 그대로 전파한다(지금 rcs 의
    바깥 `except Exception` 과 같은 범위) — 나머지(주말·팀·고정 칸)는 삼킨다(지금과 같다).
    """
    stage = _require_stage(stage_a)
    src = _sources(stage)
    grade = stage.grade.get()
    weekend_ids = _weekend_ids_or_empty(stage)
    nurses_dict = [_precheck_profile(n, weekend_ids) for n in src["nurses"]]
    precheck_config = dict(config_dict)
    _inject_team_min(stage, precheck_config, nurses_dict, log)
    _attach_stage_b(precheck_config, ei, src, log)
    return PrecheckCall(
        nurses_dict=nurses_dict, config_dict=precheck_config, grade_config=grade,
        fixed_cells=_precheck_cells_or_empty(src, config_dict, log),
        year=stage.ctx.year, month=stage.ctx.month,
    )


@dataclass(frozen=True)
class RosterIRInput:
    """roster_ir·lagrangian·presolve 소비자가 받는 입력(precheck 시점) — 조회 0 (EI-14b).

    겹침이 없으면 `nurses`·`config` 는 `pin` 의 **같은 객체**다(복사 0 — 불변을 구조로 보장).
    겹침이 있으면 `config` 만 사본이다(`pin.config_dict` 는 스냅숏·precheck 입력이라 제자리 수정 금지).
    """

    nurses: list[dict[str, Any]]
    config: dict[str, Any]
    year: int
    month: int
    num_days: int
    applied: tuple[str, ...] = ()   # 실제로 값을 바꾼 겹침 이름
    notes: tuple[str, ...] = ()     # 건너뛴 이유 — 로그·시험용
    ei: Any = None                  # 사후 소비자(EI-14b-1b)가 겹침 원천으로 쓰는 첫 시도 직전 엔진 입력 — 사전(`to_roster_ir`)은 None


_STDOUT_LOCK = threading.Lock()


def _overlay_engine_keys(cfg: dict[str, Any], pin: PrecheckCall, num_days: int, notes: list[str]) -> bool:
    """정본 키 → 그래프가 읽는 이름(EI-14b2a1).

    그래프(`roster_ir`·`frontier_dp`·`lagrangian`)는 `max_consecutive_work`·`max_consecutive_nights`·`forbid_night_to_day` 를
    읽는데 DB 정본 키는 `max_conseq_work`·`three_seq_nig`·`ban_n_to_d` 라 운영 config 에서 그 규칙이 안 켜졌다.
    번역은 새로 쓰지 않고 EI-8 `engine_hard_lock`(엔진 실효값)을 한 곳에서만 쓴다. 엔진 `create_config_from_db` 의
    print 7~9줄은 삼킨다(precheck 마다 한 번 더 나오지 않게). 못 읽는 설정은 번역 없이 기본 입력(fail-open).
    """
    from services.precheck.hard_lock_view import engine_hard_lock  # 지연 import(precheck 패키지 순환 방지)

    # redirect_stdout 은 프로세스 전역이라 스레드풀의 동시 호출이 엇갈려 진입·복원하면 sys.stdout 이 StringIO 로 남는다 —
    # 이 호출끼리는 락으로 직렬화한다(EI-14C1r). 켜 있는 동안(수 ms) 다른 스레드 로그가 삼켜질 수 있는 점은 남는다.
    with _STDOUT_LOCK, contextlib.redirect_stdout(io.StringIO()):
        eff = engine_hard_lock(cfg)
    if not eff or any(k not in eff for k in ("max_conseq_work", "three_seq_nig", "ban_n_to_d")):
        notes.append("engine_hard_lock 못 읽음 — 정본 키 번역 생략")
        return False
    cfg["max_consecutive_work"] = eff["max_conseq_work"]
    cfg["max_consecutive_nights"] = eff["three_seq_nig"]      # 엔진 값 3|2 정수
    cfg["forbid_night_to_day"] = bool(eff["ban_n_to_d"])      # banned_day_after_eve 마스킹이 이미 반영됨
    return True


_WORK_CODES = ("D", "E", "N")


def _fixed_cells_by_nurse(
    pin: PrecheckCall, known_ids: set[str], num_days: int,
) -> dict[str, dict[int, str]]:
    """`pin.fixed_cells`(EI-10d 번역본 `{nurse_id, day_index, shift}`) → 간호사별 `{날: 메인코드}` (이 병동 간호사·이 달 안만)."""
    out: dict[str, dict[int, str]] = {}
    for cell in pin.fixed_cells or ():
        try:
            nid, day, shift = str(cell["nurse_id"]), int(cell["day_index"]), str(cell["shift"]).strip().upper()
        except (KeyError, TypeError, ValueError):
            continue
        if nid in known_ids and 0 <= day < num_days:
            out.setdefault(nid, {})[day] = shift
    return out


def _exempt_adjacent(days: dict[int, str], not_one_night: bool) -> bool:
    """엔진이 전이 금지를 면제하는 인접 고정 쌍을 가진 간호사인가.

    (a) 고정 N 바로 다음 날이 고정 D/E — 두 칸이 모두 고정이면 N→D/E 를 면제한다(`fallback_lex.py:2000·2026·2052`).
    (b) 월 1일(day 0) 고정 N 이고 not_one_night — 엔진은 1일 고정 N 의 1N 금지를 면제한다.
    그래프 DP 는 N 뒤를 N/O 로만 허용해 이 쌍을 표현하지 못한다 — 겹치면 엔진이 푸는 병동에 INFEASIBLE_CERTIFIED 가 나온다.
    """
    for day, shift in days.items():
        if shift == "N" and days.get(day + 1) in ("D", "E"):
            return True
    return bool(not_one_night) and days.get(0) == "N"


def _overlay_fixed_cells(cfg: dict[str, Any], pin: PrecheckCall, num_days: int, notes: list[str]) -> bool:
    """고정 셀(주휴·휴가·확정 원티드 …)을 `initial_constraints` 에 겹친다(EI-14b2a2).

    `O` → `forced_off`, `D/E/N` → `forbidden`(나머지 근무코드 + O). `M`·`W` 등은 그래프가 모르므로 건너뛴다.
    면제 인접 간호사(`_exempt_adjacent`)는 셀을 전부 제외한다(오늘 판정 유지). 원 `initial_constraints` 는 깊은 사본에 합친다.
    """
    known = {str(n.get("nurse_id")) for n in pin.nurses_dict if isinstance(n, dict)}
    by_nurse = _fixed_cells_by_nurse(pin, known, num_days)
    if not by_nurse:
        return False
    ic = copy.deepcopy(cfg.get("initial_constraints") or {})
    forbidden = ic.setdefault("forbidden", {})
    forced_off = ic.setdefault("forced_off", {})
    not_one_night = bool(cfg.get("not_one_night"))
    exempt = 0
    changed = False
    for nid, days in by_nurse.items():
        if _exempt_adjacent(days, not_one_night):
            exempt += 1
            continue
        for day, shift in days.items():
            if shift == "O":
                offs = {int(d) for d in forced_off.get(nid, ())}
                if day not in offs:
                    forced_off[nid] = sorted(offs | {day})
                    changed = True
            elif shift in _WORK_CODES:
                cur = {int(d): list(v) for d, v in (forbidden.get(nid) or {}).items()}
                merged = sorted(set(cur.get(day, ())) | ({*_WORK_CODES, "O"} - {shift}))
                if cur.get(day) != merged:
                    cur[day] = merged
                    forbidden[nid] = cur
                    changed = True
    if exempt:
        notes.append(f"면제 인접 {exempt}명 제외")
    if changed:
        cfg["initial_constraints"] = ic
    return changed


def _overlay_by_day(cfg: dict[str, Any], pin: PrecheckCall, num_days: int, notes: list[str]) -> bool:
    """일자별 수요 → 뷰 사설 키 `_ir_by_day`(EI-14b2a3).

    `daily_shift_requirements_by_day`(list)를 길이 `num_days` 로 맞춘 `[{D,E,N}]` 로 정규화해 쓴다 — 원소가 dict 가 아니면 그날은
    평탄 수요 행, `M` 키는 버린다(그래프는 M 을 모른다), 값은 `int(v or 0)`. 모든 날이 평탄과 같으면 정보가 없으니 키를 만들지
    않는다(엔진은 평탄 경로 = 지금과 같다). raw 키는 그대로 둔다(스냅숏·precheck 용). list 가 아니면 건드리지 않는다.
    """
    from services.ontology_graph.demand import IR_BY_DAY_KEY, flat_demand

    raw = cfg.get("daily_shift_requirements_by_day")
    if not isinstance(raw, list) or not raw:
        return False
    flat = flat_demand(cfg)
    rows: list[dict[str, int]] = []
    for day in range(num_days):
        item = raw[day] if day < len(raw) else None
        if isinstance(item, Mapping):
            rows.append({s: int(item.get(s, 0) or 0) for s in _WORK_CODES})
        else:
            rows.append(dict(flat))
    if all(row == flat for row in rows):
        return False
    cfg[IR_BY_DAY_KEY] = rows
    return True


#: 겹침 목록 — `(이름, fn(cfg, pin, num_days, notes) -> 바꿨나)`. 14b2a 단계가 순서대로 더한다.
_OVERLAYS: tuple[tuple[str, Callable[..., bool]], ...] = (
    ("keys", _overlay_engine_keys),
    ("cells", _overlay_fixed_cells),
    ("by_day", _overlay_by_day),
)


def to_roster_ir(
    stage_a: PrecheckStage | None, config_dict: dict[str, Any], *,
    pin: PrecheckCall | None = None, log: Callable[[str], None] = print,
) -> RosterIRInput:
    """그래프 소비자(presolve·섀도·원인 브리지)용 입력 — `pin`(= `to_precheck_input` 결과)을 재사용해 조회 0.

    `pin` 이 없으면 `to_precheck_input(stage_a, config_dict)` 를 부른다(오프라인·시험 전용 — getter 를 연다).
    절대 예외를 내지 않는다(fail-open): 겹침마다 실패하면 그 겹침만 건너뛰고 로그 한 줄. rcs 바깥 `except` 가
    precheck 전체를 꺼 버리면 안 되기 때문이다(그래서 호출도 `run_runtime_precheck` 뒤에서 한다).
    """
    call = pin if pin is not None else to_precheck_input(_require_stage(stage_a), config_dict, log=log)
    days = getattr(getattr(stage_a, "ctx", None), "days", None)
    num_days = days if isinstance(days, int) and days > 0 else calendar.monthrange(call.year, call.month)[1]
    cfg: dict[str, Any] | None = None
    applied: list[str] = []
    notes: list[str] = []
    # 사설 키는 겹침(`by_day`)이 만든 값만 믿는다 — 요청 config_override 로 들어온 값은 버린다(EI-14C1r).
    from services.ontology_graph.demand import IR_BY_DAY_KEY

    injected = IR_BY_DAY_KEY in call.config_dict
    for name, overlay in _OVERLAYS:
        try:
            if cfg is None:
                cfg = dict(call.config_dict)
                cfg.pop(IR_BY_DAY_KEY, None)
            if overlay(cfg, call, num_days, notes):
                applied.append(name)
        except Exception as exc:  # noqa: BLE001 — 겹침 실패는 기본 입력으로 진행
            log(f"[Precheck] roster_ir {name} 겹침 실패(무시 — 기본 입력): {type(exc).__name__}: {exc}")
    return RosterIRInput(
        nurses=call.nurses_dict,
        config=cfg if (applied or injected) and cfg is not None else call.config_dict,
        year=call.year, month=call.month, num_days=num_days,
        applied=tuple(applied), notes=tuple(notes),
    )


def _overlay_engine_keys_post(
    cfg: dict[str, Any], ei: Any, nurses: Any, num_days: int, notes: list[str], shift_manage_data: Any,
) -> bool:
    """사후: 정본 키 → 그래프가 읽는 이름(EI-14b2b1) — 사전과 **같은 함수**(`_overlay_engine_keys`)를 쓴다.

    입력 `cfg` 는 엔진 유효 설정 스냅숏의 사본이라 `engine_hard_lock(cfg)` 가 엔진이 실제로 쓴 값을 정확히 낸다(precheck 시점 사본보다 정확).
    번역은 사본에만 얹고 원본 스냅숏은 건드리지 않는다(`_a1_base`·`_mus_base` 도 그 스냅숏을 읽는다).
    """
    return _overlay_engine_keys(cfg, None, num_days, notes)  # type: ignore[arg-type] — pin 을 읽지 않는다


def _overlay_fixed_cells_post(
    cfg: dict[str, Any], ei: Any, nurses: Any, num_days: int, notes: list[str], shift_manage_data: Any,
) -> bool:
    """사후: 고정 셀(주휴·휴가·확정 원티드 …)을 `initial_constraints` 에 겹친다(EI-14b2b2).

    엔진 유효 설정 스냅숏은 고정 칸을 담지 않는다(엔진은 `fixed_cells` 인자로 받는다) — 원천은 첫 시도 직전 엔진 입력 `ei.cells` 의
    `combined_fixed_cells` 다(`_run_cp_sat_basic` 12곳 모두 같은 칸을 넘긴다). 번역은 사전 precheck 와 **같은 함수**(`precheck_fixed_cells` —
    차단일 위 칸은 버림)로 하고, 겹침은 사전 `_overlay_fixed_cells`(면제 인접 간호사 제외 포함)를 그대로 재사용한다 — 사전·사후 의미가 같다.
    `ei` 가 없거나(빌더 실패·단계 A 조립 실패) 고정 칸이 없으면 건너뛴다.
    """
    cells = (getattr(ei, "cells", None) or {}).get("combined_fixed_cells") if ei is not None else None
    if not cells:
        notes.append("고정 칸 없음(또는 엔진 입력 없음) — 고정 셀 겹침 생략")
        return False
    ic0 = cfg.get("initial_constraints")
    stripped = False
    if ic0:
        # 엔진은 고정 칸이 있는 날의 금지·강제 OFF 를 무시한다(고정이 이긴다) — 사전(`_attach_stage_b`)과 같은 기준으로 스냅숏 쪽을 먼저 뺀다(Dr).
        kept = _without_fixed_days(ic0, _engine_fixed_days(list(nurses), cells))
        stripped = kept != _without_fixed_days(ic0, set())
        if stripped:
            cfg["initial_constraints"] = kept
    translated = precheck_fixed_cells(
        list(nurses), cells, shift_manage_data, use_mid=bool(cfg.get("use_mid", False)),
        blocked_by_nurse_id=cfg.get("blocked_by_nurse_id"))
    if not translated:
        return stripped
    adapter = SimpleNamespace(
        nurses_dict=[{"nurse_id": str(getattr(n, "nurse_id", ""))} for n in nurses], fixed_cells=translated)
    return _overlay_fixed_cells(cfg, adapter, num_days, notes) or stripped  # type: ignore[arg-type] — pin 에서 두 칸만 읽는다


def _overlay_by_day_post(
    cfg: dict[str, Any], ei: Any, nurses: Any, num_days: int, notes: list[str], shift_manage_data: Any,
) -> bool:
    """사후: 일자별 수요 → 뷰 사설 키 `_ir_by_day`(EI-14b2b3) — 사전과 **같은 함수**(`_overlay_by_day`).

    원천은 스냅숏 자신의 `daily_shift_requirements_by_day`(= 엔진이 첫 시도에 푼 차감 뒤 수요)다. 첫 시도 직전 엔진 입력의
    `demand.post.by_day` 와 같아야 한다(시험이 단언). 요청이 준 `_ir_by_day` 는 `to_roster_ir_post` 가 겹침 전에 이미 버린다(C1r).
    모든 날이 평탄과 같으면 키를 만들지 않는다(사다리 골든 S1-L 계열이 그렇다).
    """
    return _overlay_by_day(cfg, None, num_days, notes)  # type: ignore[arg-type] — pin 을 읽지 않는다


#: 사후 겹침(EI-14b-2b) — 이름 → 함수 `(cfg, ei, nurses, num_days, notes, shift_manage_data) -> 바꿨는가`.
_OVERLAYS_POST: tuple[tuple[str, Callable[..., bool]], ...] = (
    ("keys", _overlay_engine_keys_post),
    ("cells", _overlay_fixed_cells_post),
    ("by_day", _overlay_by_day_post),
)


def to_roster_ir_post(
    ei: Any, nurses: Any, config: dict[str, Any], *, year: int, month: int,
    shift_manage_data: Any = None, log: Callable[[str], None] = print,
) -> RosterIRInput:
    """사후 소비자(완화 사다리 실패 뒤 λ 우선순위·원인 진단·N축 branch-infer·케이스 덤프)용 입력 — 조회 0 (EI-14b-1b).

    입력은 ORM 간호사(`nurses_for_engine`)와 **엔진 유효 설정 스냅숏**(`_probe_base`)이다 — 사전용 `to_roster_ir(ei)` 로 대체하면 값이 달라져
    불변이 깨진다(사후 입력은 엔진이 실제로 쓴 값). 겹침이 없으면(`_OVERLAYS_POST` 가 비었거나 아무것도 안 바꾸면) `nurses`·`config` 는 받은
    **같은 객체**다(복사 0). 겹침이 값을 바꾸면 `config` 만 사본이다(원본 스냅숏은 `_a1_base`·`_mus_base` 도 읽는다 — 제자리 수정 금지).
    `ei`(첫 시도 직전 엔진 입력)는 겹침의 원천이다 — 없으면(빌더 실패·단계 A 조립 실패) 겹침이 건너뛴다. 절대 예외를 내지 않는다(fail-open).
    """
    try:
        num_days = calendar.monthrange(year, month)[1]
    except Exception:  # noqa: BLE001 — 달 값이 깨져도 사다리 진단을 막지 않는다
        num_days = 0
    cfg: dict[str, Any] | None = None
    applied: list[str] = []
    notes: list[str] = []
    # 사설 키는 겹침(`by_day`)이 만든 값만 믿는다 — 요청 config_override 로 들어온 값은 버린다(EI-14C1r · 사전과 같다).
    from services.ontology_graph.demand import IR_BY_DAY_KEY

    injected = IR_BY_DAY_KEY in config
    for name, overlay in _OVERLAYS_POST:
        try:
            if cfg is None:
                cfg = dict(config)
                cfg.pop(IR_BY_DAY_KEY, None)
            if overlay(cfg, ei, nurses, num_days, notes, shift_manage_data):
                applied.append(name)
        except Exception as exc:  # noqa: BLE001 — 겹침 실패는 기본 입력으로 진행
            log(f"[UndiagProbe] roster_ir_post {name} 겹침 실패(무시 — 기본 입력): {type(exc).__name__}: {exc}")
    return RosterIRInput(
        nurses=nurses, config=cfg if ((applied or injected) and cfg is not None) else config, year=year, month=month, num_days=num_days,
        applied=tuple(applied), notes=tuple(notes), ei=ei,
    )


__all__ = ["stage_a_at_precheck", "presolve_view", "to_precheck_input", "PrecheckCall",
           "ONTOLOGY_NURSE_FIELDS", "RosterIRInput", "to_roster_ir", "to_roster_ir_post"]
