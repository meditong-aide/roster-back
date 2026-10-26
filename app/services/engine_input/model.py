"""EngineInput 타입 — 생성 경로 입력의 구조화된 스냅샷(EI-9a, 호출처 0).

설계서(`DESIGN_ENGINE_INPUT.md`) §2.4 의 표와 `EI9_SPEC.md` §2.2 를 그대로 따른다. 이 모듈은
**타입만** 정의한다 — 아직 아무도 이 타입으로 값을 짓지 않는다(9b 가 빌더에서 채운다).

부품은 전부 `@dataclass(frozen=True)` 다. 안의 값은 평범한 dict/list 다 — ORM 객체를
그대로 담지 않는다(간호사는 `__dict__` 사본에서 `_sa_*` 를 뺀 dict). 그래서 `snapshot.py` 의
`canonical()` 이 지연 로딩을 일으키지 않고 결정론적으로 직렬화할 수 있다(BR-6).

`demand`(차감 전·후)의 `post` 는 고정근무 차감 뒤 수요(`DemandSnapshot`)이고, 최상위
`EngineInput.post` 는 §1.5 의 후처리 입력 6종 '읽은 값' 기록이다 — 이름이 같은 자리에
나오지만 서로 다른 것이다(§2.2 digest 표 참고).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Ctx:
    """실행 맥락 — office·group·year·month·config_id·job_id·runtime(+ EngineInput 전용 days).

    `PreludeState.ctx` 는 `days` 를 안 쓴다(연·월에서 파생 가능 — §2.4-1). 두 자리가 같은
    타입을 공유하므로 `days` 는 기본값 `None` 으로 둔다.
    """

    office_id: str
    group_id: str
    year: int
    month: int
    config_id: int | None = None
    job_id: str | None = None
    runtime: str | None = None
    days: int | None = None


@dataclass(frozen=True)
class DemandSnapshot:
    """수요 한 벌 — 기본 요구·일자별·일자별 상한(§2.2 `demand` 행)."""

    base: dict[str, int] = field(default_factory=dict)
    by_day: list[dict[str, int]] | None = None
    max_by_day: list[dict[str, int]] | None = None


@dataclass(frozen=True)
class DemandPart:
    """차감 전(`pre`)·차감 뒤(`post`) 수요 — 차감 뒤는 EI-9-0 차감 함수 결과를 담는다.

    `mode="offline"`(기본)인 정상 경로에서 `post` 는 **절대 `None` 이 아니다** — 차감 조건
    미충족(고정근무자 없음)이거나 차감이 실패하면 `post` 에 `pre` 와 **같은 객체**를
    넣는다('차감 안 함' = post == pre, 값으로 구분한다. 리뷰 지적 — 이전 docstring 은 이
    경우 `post is None` 이라고 적어 실제 동작과 달랐다). 어느 쪽이든 이유는
    `EngineInput.load_report.fixed_cut`(`applied`·`reason`)에 남는다(§2.2).

    `post=None` 이 되는 경우는 둘이다(둘 다 `load_report` 에 이유가 남는다, 리뷰 지적 HIGH로
    둘째 경우가 추가됨):
      1. `build_solve_stage` 자체를 못 돈 경우(`_assemble_engine_input` 의 `stage_b is None`
         조기 반환) — '차감을 안 했다'가 아니라 '단계 B 를 안 지었다'는 뜻이다.
      2. `mode="production"`(C, D20) 인데 `first_attempt_config` 를 안 넘긴 경우 —
         `errors["demand.post"]` 에 '필수' 메시지가 남는다. 차감 전 값으로 조용히 떨어지는
         대신(D20 위반을 숨기지 않기 위해) 미확정임을 그대로 드러낸다.
    """

    pre: DemandSnapshot
    post: DemandSnapshot | None = None


@dataclass(frozen=True)
class LoadReport:
    """빌더가 남기는 기록 전용 칸 — digest 에서 통째로 뺀다(§2.2·§2.4 digest 제외 목록).

    `raw_effective`(프로덕션 13a 가 호출 자리에서 받은 실효값)와 `raw_column`(오프라인 9d 가
    컬럼에서 바로 읽은 값)은 서로 다른 값일 수 있어 대조하지 않는다(설계서 §2.4 raw 항목).
    """

    errors: dict[str, str] = field(default_factory=dict)
    defaults_fallen: list[str] = field(default_factory=list)
    raw_effective: dict[str, Any] | None = None
    raw_column: dict[str, Any] | None = None
    #: 고정근무 차감 결과 요약 — {"applied": bool, "reason": str, "cut_min": int,
    #: "cut_max": int, "raised": int}.
    fixed_cut: dict[str, Any] = field(default_factory=dict)
    daily_rows_missing: int | None = None
    off_swap_target_missing: bool | None = None
    queries: int | None = None


@dataclass(frozen=True)
class EngineInput:
    """엔진 입력 한 벌 — §2.2 표 그대로. 읽기 전용 스냅샷이다(빌더는 이 타입을 만들 뿐 쓰지 않는다).

    `schema_version` 은 이 타입의 모양이 바뀔 때만 올린다(지금은 1).
    """

    schema_version: int = 1
    ctx: Ctx = field(default_factory=lambda: Ctx(office_id="", group_id="", year=0, month=0))
    #: `config_dict` 사본(`_sa_*` 제거) — 메타 칸(`version`·`config_name`·`config_memo`·
    #: `created_at`·`updated_at`·`last_generate_status`)은 그대로 들어 있다(digest 에서만 뺀다).
    settings_raw: dict[str, Any] = field(default_factory=dict)
    #: `create_config_from_db` 결과(해석된 설정) 사본.
    settings_eff: dict[str, Any] = field(default_factory=dict)
    #: 근무코드 1벌 — `shift_definitions`·`code2main`·`id_to_main`.
    codebook: dict[str, Any] = field(default_factory=dict)
    demand: DemandPart = field(default_factory=lambda: DemandPart(pre=DemandSnapshot()))
    #: 엔진 대상 명단 + 엔진 정렬 + `nurse_id ↔ engine_index` 맵(MS-7 포함).
    roster: dict[str, Any] = field(default_factory=dict)
    #: 고정 셀·금지 셀·배정 차단일(nurse_id 키·engine_index 키 둘 다).
    cells: dict[str, Any] = field(default_factory=dict)
    #: 전월 꼬리·n_tail·꼬리 뒤 OFF·월중 파견 경계.
    carryover: dict[str, Any] = field(default_factory=dict)
    grade: dict[str, Any] = field(default_factory=dict)
    team: dict[str, Any] = field(default_factory=dict)
    preferences: dict[str, Any] = field(default_factory=dict)
    #: 후처리 입력 6종의 '읽은 값' 기록(§1.5, D20 — 기록만). digest 에서 통째로 뺀다.
    post: dict[str, Any] = field(default_factory=dict)
    #: time_limit·단계별 workers·seed·probe 시간(기록만).
    solver_params: dict[str, Any] = field(default_factory=dict)
    #: 엔진 계약·실험 env 의 실제 값(기록만, BR-4 — 빌드 시점에 굳히지 않는다).
    env_effective: dict[str, Any] = field(default_factory=dict)
    load_report: LoadReport = field(default_factory=LoadReport)


@dataclass(frozen=True)
class PreludeState:
    """precheck 직전 메모리 객체의 사본 — '프렐류드 캡처형'(EI9_SPEC.md §2.4-R·§2.4-1).

    `_generate_roster_service_impl` 인라인 조립(:5360-:6361 부근, 함수가 아니다)의 결과를
    캡처한 값이다. 9b 빌더는 이 사본 + `db` 를 받아 단계 A·B 를 다시 짓는다. 조회 0 이다 —
    이미 메모리에 있는 값이기 때문이다.

    `ctx` 는 `days` 를 담지 않는다(연·월로 충분 — `_build_fixed_shift_roster` 등은
    `calendar.monthrange` 로 직접 구한다).
    """

    ctx: Ctx
    #: precheck 직전 `config_dict` 의 얕은 사본(`_sa_*` 제거, 차감 **전**).
    config_dict: dict[str, Any] = field(default_factory=dict)
    #: 엔진 정렬 그대로 — 간호사 `__dict__` 사본 dict 의 리스트.
    nurses_for_engine: list[dict[str, Any]] = field(default_factory=list)
    fixed_nurses: list[dict[str, Any]] = field(default_factory=list)
    #: 고정근무자 특수요청(:5772 값).
    fixed_special_requests: list[dict[str, Any]] = field(default_factory=list)
    #: 고정 셀 — rcs `combined_fixed_cells`(:6085-:6090, `list[dict]`)와 같은 모양이다
    #: (리뷰 지적 — 이전엔 `dict` 로 잘못 적었다. 실제 소비자(9b-2·9c)도 리스트로 다룬다).
    combined_fixed_cells: list[dict[str, Any]] = field(default_factory=list)
    #: `ShiftManage` 원문 dict 리스트 — `_build_code_to_main_map`/`build_allowed_shift_type_
    #: constraints` 등의 `shift_manage_data` 인자와 같은 모양이다(리뷰 지적 — 이전엔
    #: `dict[str, Any]` 로 잘못 적었다. 실제 소비자는 전부 `list[dict]` 로 다룬다 —
    #: `PrecheckStage.shift_manage_data` 도 처음부터 `list[dict[str, Any]]` 였다).
    shift_manage_data: list[dict[str, Any]] = field(default_factory=list)
    #: 선호 테이블 — rcs `prefs_data`(`list[dict]`)와 같은 모양이다(리뷰 지적, 위와 같은 이유).
    preferences: list[dict[str, Any]] = field(default_factory=list)
    #: 인바운드·아웃바운드 id 목록 — `{"inbound": [...], "outbound": [...]}`.
    assignments: dict[str, list[str]] = field(default_factory=dict)
    #: 운영 전체 특수요청(§1.5 ① O-11) — `_collect_nurses_and_preferences` 가 돌려주는
    #: `special_shift_requests` 사본(소속+인바운드 전원, `fixed_special_requests` 는 이 중
    #: 고정근무자만 거른 부분집합이다). 이전엔 `_post_health_leave` 가 `fixed_special_requests`
    #: 로 근사해, 보건휴가 선점(`existing_requests`) 판정에서 일반 간호사 몫이 빠졌었다.
    special_shift_requests: list[dict[str, Any]] = field(default_factory=list)
