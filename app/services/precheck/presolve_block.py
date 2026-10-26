"""EI-5 배관 헬퍼 — presolve 차단(사전 판정) 공통 처리.

설계서 §5.3 'EI-5 배관 헬퍼'(DESIGN_ENGINE_INPUT.md). `roster_create_service.py` 의
두 차단 블록 — (가) precheck 차단(`run_runtime_precheck`/`has_blocking_issues` 결과로
막는 블록) · (나) 나이트 강제 과공급 차단(`detect_night_forced_oversupply` 블록) — 이
각자 손으로 하던 "카드·메시지 채우기 → 기록 → 초안 삭제 → HTTP 500" 배관을 하나로
묶는다.

두 블록의 배관은 **같지 않다**(아래 표) — 합치되 차이는 호출 쪽 선택지로 보존한다.
이 모듈 자체는 동작을 바꾸지 않는다(EI-5 는 불변 단계).

| 차이       | precheck 차단                              | 나이트 과공급 차단           | 선택지 |
|------------|---------------------------------------------|-------------------------------|--------|
| 기록 실패   | 바깥 except 로 빠져 **솔버 진행**             | 로그만 남기고 **그대로 차단**  | `on_record_fail="proceed"`/`"block"` |
| 순서       | 기록 → payload                              | payload → 기록                | `record_first=True`/`False` |
| 카드       | 원인 브리지 카드를 기존 카드 **앞에 붙임**     | 카드 **하나만**(덮어씀)        | `card_merge="prepend"`/`"exclusive"` |

(기록 실패 로그 접두어도 두 블록이 다르다 — precheck 는 `on_record_fail="proceed"`
라 이 로그 자체를 안 찍는다. 나이트는 `[NightOversupply]` 접두어를 그대로 쓴다
— `PresolveBlockContext.log_tag`.)

순환 import 주의: `roster_create_service.py` 가 이 모듈(`raise_presolve_block` 등)을
쓰고, 이 모듈의 `assemble_presolve_view` 는 반대로 `roster_create_service.py` 의
헬퍼(`_build_code_to_main_map` 등)가 필요하다. 모듈 맨 위에서 서로를 import 하면
순환이 생기므로, 그 쪽만 함수 **안에서** 지연 import 한다(호출 시점엔 두 모듈 다
이미 로드가 끝난 뒤라 순환이 생기지 않는다). `build_blocking_payload` 도 같은 이유로
지연 import 한다 — 기존 두 블록이 이미 그렇게 했고(호출마다 모듈에서 다시
끌어옴), 특성화 테스트가 그 모듈 속성(`services.precheck.build_blocking_payload`)
몽키패치로 호출을 가로챈다. 여기서 모듈 상단에서 미리 bind 해 두면 그 패치를
못 받는다.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, NoReturn

from fastapi import HTTPException
from sqlalchemy.orm import Session

OnRecordFail = Literal["proceed", "block"]
CardMerge = Literal["prepend", "exclusive"]
# "proceed" 는 "헬퍼가 솔버로 진행시킨다"는 뜻이 아니다 — 기록 실패 예외를 그대로
# 올릴 뿐이다. '솔버 진행'이 되는 것은 **호출부에 그 예외를 삼키는 넓은 except 가
# 있을 때뿐**이다(precheck 블록을 감싼 `except Exception as _pre_exc`). 그런 except
# 없이 "proceed" 를 쓰면 기록 실패가 그대로 미분류 500 으로 올라간다.


@dataclass(frozen=True)
class PresolveFinding:
    """탐지 결과 하나 — 사전 판정 '이슈' 또는 카드·메시지 오버레이(또는 둘 다).

    `reason_code` 가 있으면 `to_issue()` 가 그것을 `build_blocking_payload` 로 가는
    issue 로 만든다(나이트 블록처럼 탐지 자체가 곧 issue 1건인 경우).
    `reason_code` 가 없으면(`None`) issue 로는 안 가고 `cards`·`message_ko` 로만
    기존 결과를 보강한다(precheck 블록의 원인 브리지처럼, 이미 있는 issue 목록
    위에 카드만 얹는 경우 — 그 issue 목록 자체는 `raise_presolve_block` 의
    `base_issues` 로 손실 없이 그대로 넘긴다).
    """

    reason_code: str | None
    severity: str | None = None
    evidence: dict[str, Any] | None = None
    cards: list[dict[str, Any]] | None = None
    message_ko: str | None = None
    exclusive_cards: bool = False

    def to_issue(self) -> dict[str, Any] | None:
        """`build_blocking_payload` 가 읽는 issue 모양으로. 오버레이 전용이면 None."""
        if self.reason_code is None:
            return None
        return {
            "reason_code": self.reason_code,
            "severity": self.severity,
            "evidence": self.evidence or {},
        }


@dataclass
class PresolveBlockContext:
    """차단 기록(`roster_config_attempt` 등)·초안 삭제에 필요한 값 + 기록 함수 2개.

    기록 함수(`record_status`·`build_snapshot`)는 `roster_create_service.py` 안의
    전역(`_mark_config_generate_status`·`_build_generate_snapshot`)을 그대로
    받는다 — 호출부가 그 전역을 참조하는 **시점**(이 bctx 를 짓는 시점, 즉 요청
    처리 중)에 평가되므로, 특성화 테스트가 그 전역을 몽키패치해도 그대로
    반영된다(순환 import 없이도 성립 — 같은 모듈 안 전역 참조일 뿐이다).
    """

    db: Session
    config: Any
    year: int
    month: int
    job_id: str | None
    persistent_fix: dict | None
    intervened: bool
    nurses_dict: list[dict] | None
    config_dict: dict | None
    schedule: Any
    record_status: Callable[..., None]
    build_snapshot: Callable[..., str | None]
    ctx: dict | None = None
    log: Callable[[str], None] = print
    log_tag: str = "[PresolveBlock]"


def _record_once(bctx: PresolveBlockContext) -> None:
    """스냅샷을 먼저 짓고 기록한다 — 기존 두 블록과 같은 인자 평가 순서."""
    snapshot = bctx.build_snapshot(
        bctx.db, bctx.config, "blocked", bctx.year, bctx.month,
        bctx.nurses_dict, bctx.config_dict, bctx.job_id, bctx.persistent_fix,
    )
    bctx.record_status(
        bctx.db, bctx.config, "blocked", snapshot,
        bctx.year, bctx.month, bctx.job_id, bctx.intervened,
    )
    if bctx.ctx is not None:
        bctx.ctx["recorded"] = True


def _record_with_policy(bctx: PresolveBlockContext, on_record_fail: OnRecordFail) -> None:
    """`"proceed"` 는 예외를 그대로 올린다(호출자의 바깥 except 가 '기록 실패 →
    솔버 진행' 으로 받게). `"block"` 은 여기서 삼키고 로그만 남긴다(차단은 기록
    성패와 무관하게 계속된다)."""
    if on_record_fail == "proceed":
        _record_once(bctx)
        return
    try:
        _record_once(bctx)
    except Exception as exc:  # noqa: BLE001 — 기록 실패가 차단 자체를 막으면 안 된다
        # 접두어는 bctx.log_tag — 전환 전 나이트 블록은 `[NightOversupply]` 를 그대로
        #   썼다(모듈 docstring 표). 호출부가 지정하지 않으면 공용 기본값으로 찍는다.
        bctx.log(f"{bctx.log_tag} 차단 기록 실패(무시): {exc}")


def _collect_issues(
    findings: list[PresolveFinding], base_issues: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """`build_blocking_payload` 로 보낼 issue 목록.

    `base_issues` 가 주어지면 그걸 **가공 없이** 그대로 쓴다 — precheck 가 이미
    만든 issue 는 `reason_code`/`severity`/`evidence` 외에도 온톨로지 태그 등
    부가 필드를 가질 수 있어, `PresolveFinding` 으로 쪼갰다 되돌리면 그 필드가
    샌다. `findings` 중 `reason_code` 가 있는 것(오버레이가 아니라 그 자체로
    issue 인 것)은 **뒤에 덧붙인다** — 지금 두 호출부는 base_issues·reason_code
    finding 을 동시에 쓰지 않아 결과가 그대로지만, 둘을 함께 쓰는 호출이 생겨도
    issue 가 조용히 버려지지 않게 한다. 없으면(`base_issues is None`) findings
    에서 issue 를 가진 것만 모은다(나이트 블록처럼 finding 자체가 곧 issue 인
    경우).
    """
    extra = [issue for issue in (f.to_issue() for f in findings) if issue is not None]
    if base_issues is not None:
        return list(base_issues) + extra
    return extra


def _merge_findings_into_payload(
    payload: dict[str, Any], findings: list[PresolveFinding], card_merge: CardMerge,
) -> None:
    """findings 의 `cards`·`message_ko` 를 payload 위에 얹는다(모듈 docstring 표의
    '카드' 행). `card_merge="prepend"` 는 추가 카드를 기존 카드 **앞에** 붙인다
    (precheck). `"exclusive"` 는 추가 카드가 기존 카드를 **덮어쓴다**(나이트).
    finding 자신이 `exclusive_cards=True` 면 호출측 기본값과 무관하게 배타가 된다.
    """
    inf = payload.setdefault("infeasibility", {})
    base_options = list(inf.get("resolution_options") or [])
    overlay: list[dict[str, Any]] = []
    message_ko: str | None = None
    exclusive = card_merge == "exclusive"
    for finding in findings:
        if finding.cards:
            overlay.extend(finding.cards)
        # `is not None` — 나이트 블록은 전환 전에 메시지를 조건 없이 덮어썼다
        #   (`_inf_nfo["summary_message_ko"] = _msg_nfo`, 빈 문자열이어도 덮음).
        #   truthy 체크면 빈 문자열이 '메시지 없음'과 같아져 그 덮어쓰기가 샌다.
        if finding.message_ko is not None:
            message_ko = finding.message_ko
        exclusive = exclusive or finding.exclusive_cards
    if overlay:
        inf["resolution_options"] = overlay if exclusive else overlay + base_options
    if message_ko is not None:
        inf["summary_message_ko"] = message_ko
        fix_suggestions = list(inf.get("fix_suggestions_ko") or [])
        inf["fix_suggestions_ko"] = (
            [message_ko] if exclusive
            else ([message_ko] + fix_suggestions if message_ko not in fix_suggestions else fix_suggestions)
        )


def _drop_summary_echo_without_cards(inf: dict[str, Any]) -> None:
    """카드가 0장이면 프론트가 '조정 제안'(`fix_suggestions_ko`)을 그린다 — 요약(`summary_message_ko`)과 같은 줄은 빼서
    같은 문구가 '생성이 막힌 조건'·'조정 제안'에 두 번 뜨지 않게 한다(EI-11 11r · 리뷰 MEDIUM — 원인 브리지 카드가 하드락
    필터에 모두 걸리면 D30 문구가 두 칸에 겹쳤다). 카드가 남아 있으면 그대로다('조정 제안'을 그리지 않는다)."""
    if inf.get("resolution_options"):
        return
    summary = inf.get("summary_message_ko")
    fixes = inf.get("fix_suggestions_ko")
    if summary and isinstance(fixes, list) and summary in fixes:
        inf["fix_suggestions_ko"] = [s for s in fixes if s != summary]


def _delete_draft(bctx: PresolveBlockContext) -> None:
    """차단 전에 미리 만들어 둔 초안(`schedules` 행)을 지운다. 실패하면 롤백."""
    try:
        bctx.db.delete(bctx.schedule)
        bctx.db.commit()
    except Exception:  # noqa: BLE001 — 삭제 실패로 차단 응답 자체를 막지 않는다
        bctx.db.rollback()


def raise_presolve_block(
    bctx: PresolveBlockContext,
    findings: list[PresolveFinding],
    *,
    record_first: bool,
    on_record_fail: OnRecordFail,
    card_merge: CardMerge,
    base_issues: list[dict[str, Any]] | None = None,
    extra_findings: Callable[[], list[PresolveFinding]] | None = None,
    log_after_merge: Callable[[dict[str, Any]], None] | None = None,
) -> NoReturn:
    """사전 판정 결과로 생성을 막는다 — 카드·메시지 채우기(`build_blocking_payload`)
    → 기록(`_mark_config_generate_status`+`_build_generate_snapshot`) → 초안 삭제
    → `HTTPException(500)`(설계서 §5.3). `record_first`/`on_record_fail`/
    `card_merge` 로 두 기존 블록의 차이를 그대로 보존한다(모듈 docstring 표 참조).

    `extra_findings` 는 **payload 가 선 지어진 뒤**(record_first=True 면 기록도
    끝난 뒤 — 기록이 실패하면 전혀 불리지 않는다) 평가하는 지연 finding
    생성자다 — precheck 의 원인 브리지(`explain_infeasibility_from_config`)처럼,
    기록이 실패해 솔버로 진행하는 경로에서는 돌 필요가 없는(돌면 안 되는)
    계산을 여기로 넘긴다. **오버레이 전용이다** — `issues`(그래서
    `build_blocking_payload` 가 보는 기본 payload)에는 안 들어간다. 지금 두
    호출부 다 그렇다(precheck 의 원인 브리지는 `reason_code=None`). `findings`
    에 issue 가 필요한 것(나이트처럼 finding 자체가 issue)은 `extra_findings`
    가 아니라 `findings` 인자로 즉시 넘긴다. `log_after_merge(payload)` 는
    merge 가 끝난 뒤 — 그래서 카드·payload·원인 브리지 중 무엇이 실패해도 로그는
    안 찍힌다 — 기록 **전에**(`record_first=False` 인 나이트 쪽도 같다)
    불린다. 둘 다 기존 두 블록의 "기록·payload·카드 합류가 먼저, BLOCKING
    로그는 그 결과를 보고" 순서를 그대로 지키기 위한 자리다 — 전환 때 이
    순서가 한 번 깨졌었다(역방향 감사로 재현·고정, 2026-10-14).

    merge 바로 뒤 EI-11 카드 하드락 필터(`policy.card_filter.finalize_infeasibility`)가 카드 칸을
    거른다 — 원인 브리지·나이트 카드도 `build_blocking_payload` 의 카드와 같은 판정을 지난다(로그 전).
    """
    if record_first:
        _record_with_policy(bctx, on_record_fail)

    from services.policy.card_filter import finalize_infeasibility  # 지연 import(모듈 docstring)
    from services.precheck import build_blocking_payload  # 지연 import(모듈 docstring)

    issues = _collect_issues(findings, base_issues)
    payload = build_blocking_payload({"issues": issues})

    all_findings = list(findings)
    if extra_findings is not None:
        all_findings += extra_findings()
    _merge_findings_into_payload(payload, all_findings, card_merge)
    finalize_infeasibility(payload.setdefault("infeasibility", {}))  # EI-11 싱크 c — 합친 카드까지 거른다
    _drop_summary_echo_without_cards(payload["infeasibility"])

    if log_after_merge is not None:
        log_after_merge(payload)

    if not record_first:
        _record_with_policy(bctx, on_record_fail)

    _delete_draft(bctx)
    raise HTTPException(status_code=500, detail=payload)


@dataclass(frozen=True)
class PresolveView:
    """사전 판정 탐지기가 보는 입력 — 나이트 블록이 손으로 짓던 것을 한 번만 짓는다.

    `year`·`month` 는 탐지기가 만드는 Finding 의 메시지·카드(`night_forced_oversupply_*`)
    가 "몇 월" 문구를 넣는 데 필요하다(EI-5b) — `num_days` 와 겹치지만(둘 다
    `req.year`·`req.month` 에서 나온다), `num_days` 만으로는 월을 되돌릴 수 없어
    따로 들고 간다.
    """

    nurses: list
    config_dict: dict[str, Any]
    num_days: int
    code2main: dict[str, str]
    id_to_main: dict
    fixed_cells: list[dict[str, Any]]
    year: int
    month: int


@dataclass(frozen=True)
class Detector:
    """탐지기 등록 단위 — 탐지(`detect`)와 Finding 조립(`build`) 두 단계로 나뉜다(EI-5b 수정).

    `detect(view)` 는 모순이 있는지만 본다(산술 비교 등 — 카드·메시지는 아직 안
    짓는다). 실패(예외)는 `run_presolve_detectors` 가 로그만 남기고 다음 탐지기로
    넘어간다(전환 전 '탐지 호출' 실패와 같다). 모순이 없으면(`None`) 이 탐지기는
    그걸로 끝이다.

    `build(raw, view)` 는 `detect` 가 돌려준 원시 결과로 카드·메시지까지 채운
    `PresolveFinding` 을 짓는다. 여기서 실패하면 **삼키지 않고 그대로 올린다** —
    전환 전에는 카드·메시지 생성이 '탐지 호출' try 밖(`roster_create_service.py` 의
    예전 `if _nfo:` 블록)에 있어 예외가 그대로 올라갔다. EI-5 전환 때 탐지와
    조립이 한 함수로 합쳐지면서 이 경계가 한 번 사라져 조립 실패까지 삼켜졌는데,
    역방향 감사로 재현해 되돌렸다(EI-5b, 2026-10-14).
    """

    id: str
    detect: Callable[[PresolveView], Any | None]
    build: Callable[[Any, PresolveView], PresolveFinding]


def _detect_night_forced_oversupply_raw(view: PresolveView) -> dict[str, Any] | None:
    """나이트 강제 과공급 산술 탐지(`4f00db0`) — `Detector.detect` 단계.

    `detect_night_forced_oversupply`(간호사별 월 나이트 하한 합 > 그 달 나이트 칸 —
    산술 증명)만 부른다. 카드·메시지는 아직 짓지 않는다(`_build_night_forced_oversupply_finding`
    몫). 모순이 없으면(빈 결과) `None`.

    입력(`view`, 고정근무자 차감 뒤 config_dict — 설계서 :289)은 전환 전과 같다.
    """
    from services.ontology_graph.lagrangian import detect_night_forced_oversupply

    return detect_night_forced_oversupply(
        view.nurses, view.config_dict, view.num_days,
        fixed_cells=view.fixed_cells, code2main=view.code2main, id_to_main=view.id_to_main,
    ) or None


def _build_night_forced_oversupply_finding(nfo: dict[str, Any], view: PresolveView) -> PresolveFinding:
    """탐지 결과(raw dict)로 산술 evidence·카드·메시지를 채운 `PresolveFinding` 을
    짓는다 — `Detector.build` 단계.

    `roster_create_service.py` 의 나이트 블록이 손으로 하던 일(산술 evidence 조립 →
    카드·메시지 생성 → `PresolveFinding` 조립)을 그대로 여기로 옮겼다 — evidence
    모양·카드·메시지 전부 전환 전과 같다. 이 단계의 실패는 `run_presolve_detectors`
    가 삼키지 않는다(`Detector` docstring 참조) — 전환 전과 같은 동작이다.
    """
    from services.ontology_graph.mcs_trace import (
        night_forced_oversupply_card,
        night_forced_oversupply_message,
    )

    arithmetic = {
        "forced_n_sum": nfo["forced_n_sum"], "n_slots": nfo["n_slots"],
        "excess": nfo["excess"], "year": view.year, "month": view.month,
    }
    return PresolveFinding(
        reason_code="MONTHLY_NIGHT_FORCED_OVERSUPPLY", severity="error",
        evidence={**arithmetic,
                  "nurses": [{"nurse_id": t["nurse_id"], "name": t["name"],
                              "current": t["current"], "field": t["field"]}
                             for t in nfo["targets"]]},
        cards=[night_forced_oversupply_card(nfo["targets"], arithmetic)],
        message_ko=night_forced_oversupply_message(nfo["targets"], arithmetic),
        exclusive_cards=True,
    )


#: 등록표(EI-5b) — 지금은 나이트 과공급 하나뿐이다. 입력(`PresolveView`)은 이제
#: 단계 A view(`services.engine_input.views.presolve_view`, EI-10a)가 짓는다 —
#: 이 모듈이 조립 함수(`assemble_presolve_view`)만 들고 있고, 어디서 짓는지는
#: 호출부(rcs 나이트 블록)의 몫이다.
PRESOLVE_DETECTORS: tuple[Detector, ...] = (
    Detector("night_forced_oversupply",
             _detect_night_forced_oversupply_raw, _build_night_forced_oversupply_finding),
)


def run_presolve_detectors(
    view: PresolveView, log: Callable[[str], None] = print, log_tag: str = "[PresolveBlock]",
) -> list[PresolveFinding]:
    """등록된 탐지기를 전부 돌려 finding 을 모은다.

    탐지(`detector.detect`) 실패는 로그만 남기고 다음 탐지기로 진행한다(§1.3 '사용자
    불만 → 장치' 표 — EI-5 행). **Finding 조립(`detector.build`) 실패는 삼키지
    않는다** — `Detector` docstring 참조(EI-5b 수정: 역방향 감사가 전환 전 '카드
    생성 실패는 로그 없이 전파'를 재현해, 전환 때 한 번 생긴 차이를 되돌렸다).

    `log_tag` 는 탐지 실패 로그의 접두어다(기본 `"[PresolveBlock]"`) — 호출부가
    지정하면 그 값이 메시지에 그대로 박힌다(나이트 호출부는 `"[NightOversupply]"`
    를 넘겨 전환 전 grep 태그를 보존한다). 메시지에는 탐지기 id 와 예외 타입
    이름(`type(exc).__name__`)도 함께 남긴다 — 운영 로그에서 어떤 탐지기가 왜
    실패했는지 구분하기 위해서다.

    여러 탐지기 의미(EI-5b) — **첫 차단만이 아니라 전부 모아서**: 실패(예외)한
    탐지기만 건너뛰고 나머지는 끝까지 돈다. `None` 을 돌려준 탐지기는 그냥
    빠진다. Finding 을 돌려준 탐지기는 전부 이 목록에 쌓여 호출부로 간다 —
    호출부는 그 목록을 통째로 `raise_presolve_block` 에 넘기고(지금 나이트
    호출부가 그렇다), 그 쪽은 여러 finding 의 issue·카드는 전부 병합한다
    (`_collect_issues`·`_merge_findings_into_payload`). **메시지
    (`summary_message_ko`/`fix_suggestions_ko`)는 병합되지 않는다** — 마지막
    finding 의 메시지가 이긴다(`_merge_findings_into_payload` 가 매 finding 마다
    `message_ko` 를 덮어쓴다). `exclusive` 가 되면 `fix_suggestions_ko` 도 그
    마지막 메시지 하나로 줄어 앞선 finding 의 메시지가 사라진다. 탐지기가
    1개뿐인 지금은 findings 가 0개 또는 1개라 이 정책이 지금 동작과 다르지
    않다 — 탐지기가 늘어도 "첫 번째가 차단하면 뒤는 평가도 안 한다" 로 바뀌는
    일은 없지만(끝까지 돈다), 메시지는 마지막 것만 남는다는 점은 그대로다.
    """
    findings: list[PresolveFinding] = []
    for detector in PRESOLVE_DETECTORS:
        try:
            raw = detector.detect(view)
        except Exception as exc:  # noqa: BLE001 — 탐지 실패는 생성을 막지 않는다
            log(f"{log_tag} 탐지기 {detector.id} 실패(무시): {type(exc).__name__}: {exc}")
            continue
        if raw is None:
            continue
        findings.append(detector.build(raw, view))  # 조립 실패는 삼키지 않는다(위 docstring)
    return findings


def _shift_defs_from_lookup(lookup: Mapping[str, Any]) -> list[dict[str, Any]]:
    """근무코드 정의를 `_build_shift_normalizer`(cp_sat_basic) 가 받는 모양으로(지금
    `_shift_defs_for_normalizer` 행 모양 원문 — `lookup` 을 조회 대신 인자로 받을 뿐이다).

    여기 모양은 `_run_cp_sat_basic` 이 `shift_definitions` 를 만드는 방식(엔진과
    같은 두 매핑)과 **반드시 같아야 한다** — 탐지기가 엔진과 다른 근무코드 판정을
    쓰면 판정이 갈린다.
    """
    return [
        {
            "shift_id": getattr(s, "shift_id", None),
            "default_shift": getattr(s, "default_shift", None) or getattr(s, "shift_id", None),
            "shift_gb": getattr(s, "shift_gb", None),
            "type": getattr(s, "type", None),
            "show_in_preference": getattr(s, "show_in_preference", None),
        }
        for s in lookup.values()
    ]


def _fixed_cells_by_nurse_id(
    nurses_for_engine: list, combined_fixed_cells: list[dict] | None,
    extra_keys: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    """고정 칸을 엔진 색인(`nurse_index`)에서 `nurse_id` 로 역변환한다.

    못 바꾸는 칸은 `nurse_id=None` 으로 남긴다 — 판정 쪽이 그 칸을 '모름'으로
    건너뛰게 하기 위해서다(엔진 색인에 없는 칸을 억지로 추측해 틀린 판정을
    내는 쪽보다 안전하다).

    `extra_keys`(EI-10d): 칸에서 그대로 옮길 칸 이름 — 기본 `()` 라 탐지기가 받는 모양은 그대로다.
    precheck 의 고정 칸 번역(`fixed_cell_codes`)이 `shift_type`·`fixed_source` 를 읽으려고 같은 함수를 쓴다.
    """
    from services.roster_create_service import _build_engine_nurse_index_map

    idx_to_nurse_id = {
        idx: nid for nid, idx in _build_engine_nurse_index_map(nurses_for_engine).items()
    }
    return [
        {"nurse_id": idx_to_nurse_id.get(cell.get("nurse_index")),
         "day_index": cell.get("day_index"), "shift": cell.get("shift"),
         **{key: cell.get(key) for key in extra_keys}}
        for cell in (combined_fixed_cells or [])
    ]


def assemble_presolve_view(
    *,
    nurses: list,
    shift_manage_data: list[dict] | None,
    combined_fixed_cells: list[dict] | None,
    config_dict: dict[str, Any],
    load_shift_lookup: Callable[[], Mapping[str, Any]],
    num_days: int,
    year: int,
    month: int,
) -> PresolveView:
    """나이트 과공급 탐지기용 입력 — 근무코드 정규화 두 매핑 + 고정 칸의
    `nurse_index` → `nurse_id` 역변환을 한 함수로(기존엔 나이트 블록이 매번
    손으로 지었다 — `4f00db0`). `code2main` 을 먼저 만들고 그 다음에
    `load_shift_lookup()`(근무코드 조회)을 불러 정규화한다 — 이 순서여야
    `code2main` 이 실패할 때 근무코드 조회가 나가지 않는다(지금과 같은
    쿼리 수, EI-10a C5). `year`·`month` 는 그대로 `PresolveView` 에 실어
    탐지기(카드·메시지 생성)로 넘긴다 — 쿼리를 추가로 내지 않는다.

    `load_shift_lookup` 은 조회 함수(0인자 thunk)를 받는다 — 호출부(EI-10a
    `views.presolve_view`)가 `PrecheckStage.shift_lookup.get` 을 그대로 넘겨
    같은 조회를 한 번만(캐시) 쓴다.
    """
    from services.cp_sat_basic import _build_shift_normalizer
    from services.roster_create_service import _build_code_to_main_map

    code2main = _build_code_to_main_map(shift_manage_data)
    id_to_main, _ = _build_shift_normalizer(_shift_defs_from_lookup(load_shift_lookup()))
    fixed_cells = _fixed_cells_by_nurse_id(nurses, combined_fixed_cells)
    return PresolveView(
        nurses=nurses, config_dict=config_dict, num_days=num_days,
        code2main=code2main, id_to_main=id_to_main, fixed_cells=fixed_cells,
        year=year, month=month,
    )
