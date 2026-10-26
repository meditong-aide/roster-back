"""EI-5 1단계 — presolve 차단 두 블록의 특성화 테스트(설계서 §5.3 'EI-5 배관 헬퍼').

**앱 코드는 고치지 않는다.** 이 파일은 `app/services/roster_create_service.py` 의
두 차단 블록 — (가) precheck 차단(`run_runtime_precheck`/`has_blocking_issues` 결과로
막는 블록) · (나) 나이트 강제 과공급 차단(`detect_night_forced_oversupply` 블록) — 의
**지금 동작**을 고정한다. EI-5 가 두 블록을 `raise_presolve_block` 헬퍼로 합칠 때, 이
테스트가 그대로(또는 설계서가 말한 선택지 그대로) 통과해야 한다.

설계서 표(§5.3)가 말하는 세 차이 — 코드로 재확인(2026-10-14):
  기록 실패 시 동작
    precheck: `_mark_config_generate_status` 호출에 자체 보호가 없다. 실패하면 블록을
      감싼 바깥 `except Exception` 으로 빠져 `precheck_result = None` 으로 **솔버 진행**.
    나이트: 기록 호출이 `try/except` 로 감싸여 있어 실패해도 로그만 찍고 **그대로 차단**
      (`db.delete(schedule)` → `HTTPException(500)`).
  순서
    precheck: 기록(`_mark_config_generate_status`) → payload(`build_blocking_payload`).
    나이트:   payload(`build_blocking_payload`) → 기록(`_mark_config_generate_status`).
  카드 병합
    precheck: 원인 브리지 카드를 **기존 카드 앞에 붙인다**
      (`_cards + (기존 resolution_options or [])`).
    나이트:   카드 **하나만** 내고 기존 값을 덮어쓴다(`[night_forced_oversupply_card(...)]`).

이 세 가지를 코드 읽기로 확인한 결과는 설계서와 **일치한다** — 아래 테스트는 그 사실을
실행으로 다시 확인하고 고정한다(코드 읽기 → 실측 재확인 → 고정, 셋 다 통과).

캡처는 골든 하네스(`harness.run_with_output` · `scenario_defs.SCENARIOS`)를 그대로 쓴다.
기존 시나리오·`tests/engine_input/golden/*.json` 은 건드리지 않는다 — precheck 차단을
보여줄 시나리오가 골든에 없어서(아래 `test_no_existing_scenario_hits_precheck_block` 가
확인한다) 이 파일 전용으로 하나 추가했을 뿐, `scenario_defs.SCENARIOS` 레지스트리에는
넣지 않았다(그 레지스트리는 `test_scenarios.py` 의 `PATH_CHECKS`·`golden/` 전수 커버
전제라, 넣으면 그 테스트들까지 건드리게 된다).

이 파일만의 골든은 `tests/engine_input/golden_presolve/*.json` 에 둔다(기존
`golden/` 과 다른 디렉터리). **전용 플래그 `UPDATE_GOLDEN_PRESOLVE=1` 로만 다시 쓴다** — 기존
`golden/` 의 `UPDATE_GOLDEN=1` 은 이 디렉터리를 건드리지 않는다(2026-10-19 리뷰 지적: 같은 플래그를 읽어
`UPDATE_GOLDEN=1 pytest tests/engine_input` 한 번에 조용히 흡수됐다). 파일 내용은 sha256 으로도 고정돼 있다
(`test_golden_presolve_files_are_frozen`) — 의도해서 다시 쓸 때는 같은 커밋에서 고정값도 함께 고친다.

D29(2026-10-20) — 이 골든도 커밋하지 않는 로컬 전용이다. 골든을 여는 자리(`_read_golden` · 동결 sha256 대조 ·
플래그 분리 확인)는 첫 줄에서 문지기(`golden_gate.require_golden(GOLDEN_PRESOLVE)`)를 거친다 — 디렉터리가 비었으면
D29 사유로 건너뛰고(`REQUIRE_GOLDEN=1` 이면 실패), 일부만 있으면 실패한다.
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import os
import re
import sys
from collections.abc import Callable
from functools import partial
from pathlib import Path

import pytest
from sqlalchemy import event

from .canonical import canonical
from .expected_diff import (
    EI10_DONE,
    EI10_STAGES,
    EI11_DONE,
    EI11_STAGES,
    EI12X_DONE,
    EI12X_STAGES,
    EI13C_DONE,
    EI13C_STAGES,
    EI14C1_DONE,
    EI14C1_STAGES,
    check_record,
)
from .golden import _mask_volatile
from .golden import read_golden as _read_main_golden
from .golden_gate import GOLDEN_PRESOLVE, require_golden
from .harness import CapturedRun, Scenario, run_with_output
from .scenario_defs import BASE_DDL, SCENARIOS, _request
from .scenarios import make_head_nurse
from .ward import base_ward, seed_ward

#: 경로·플래그 이름의 정본은 `golden_gate.GOLDEN_PRESOLVE` 다 — 아래 둘은 별칭이다(D29). `GOLDEN_DIR` 은 테스트가
#: 임시 폴더로 바꿔 끼우는 자리라(`test_generic_update_flag_does_not_rewrite_golden_presolve`) 모듈 전역으로 남긴다.
GOLDEN_DIR = GOLDEN_PRESOLVE.path

#: 이 디렉터리를 다시 쓰는 **전용** 플래그 — `golden/` 의 `UPDATE_GOLDEN` 과 다르다(그 플래그는 여기서 읽지 않는다).
UPDATE_PRESOLVE_ENV = GOLDEN_PRESOLVE.update_env

#: golden_presolve/*.json 의 sha256 — 이 단계(EI-10c~10f)가 다시 쓰지 않은 동결본이다.
#: ★2026-10-23(EI-10g2·갈래 D) — S9-F 만 다시 썼다: 동결본 위에 '이동 전 코드로 다시 뜬 파일 대 이동 후 파일'의 차이만 얹었다
#:   (`p2/D/tools/presolve_overlay.py` — EI-10c~14r3 허용 차이를 섞지 않는다). 변한 곳: `snapshot.config.daily_shift_requirements_by_day[*].N`
#:   1→0 21곳 · `queries.marks.L0` 84→88 · `queries.tables` 순서(차감 읽기가 precheck 입력 조회 앞으로).
#: ★2026-10-23(EI-10g3·갈래 D) — 세 파일 모두 동결본 위에 단계 B 앞당김 몫(`queries.count`·`marks`·`tables`: EI5 +7 · S9 +6 · S9-F +6)만 얹었다.
#: ★2026-10-20(dev `4ec454e` 재수신) — `ei10/base_10c`(옛 값: EI5-precheck-block `efd7c30d…` · S9-F `d413472d…` · S9 `3e4e5923…`)에
#:   **순수 dev 가 바꾼 만큼**만 얹었다: 파일마다 `snapshot.grade_config_effective.configured_constraints_json` 1곳(dev `863e0f2` 의
#:   병원 원본 등급 최소 키). 작업 코드를 다시 돌려 쓴 것이 아니다 — 돌려 쓰면 EI-10c 회복 축(8곳)이 골든에 섞여 `must_change` 가 깨진다.
#:   근거: 순수 3ede458→4ec454e 의 같은 파일 차이가 정확히 그 1곳 · 얹은 파일과 코드 재생성물의 차이가 EI-10c 회복 축 8곳뿐
#:   (`redev_4ec454e/STATUS_redev_4ec454e.md` §흡수 차례).
FROZEN_SHA256 = {
    "EI5-precheck-block.json": "a776ce067d65e611a15af64c979d96eb2820cb0bc2a470b21d702e0d0f976419",   # 10g3 — 동결본 + 쿼리 +7(단계 B 앞당김)
    "S9-F.json": "75b2dbda19442d91f2ecd5a3df8494005b7109795052814e2d1b4dbaaa3376c6",   # 10g2(N 21곳 0 · 쿼리 순서) + 10g3(쿼리 +6)
    "S9.json": "1e171c036d00c68999fe0e3d66246676906b362e2af47a82495e9392971b37ed",   # 10g3 — 동결본 + 쿼리 +6
}


def presolve_update_requested() -> bool:
    """golden_presolve 를 다시 쓸 차례인가(`UPDATE_GOLDEN_PRESOLVE=1`) — `UPDATE_GOLDEN` 은 보지 않는다."""
    return os.environ.get(UPDATE_PRESOLVE_ENV) == "1"


# ── 이 파일 전용 골든 I/O(디렉터리만 다르다 — 나머지 규칙은 `.golden` 과 같다) ──────


def _golden_path(name: str) -> Path:
    return GOLDEN_DIR / f"{name}.json"


def _read_golden(name: str) -> dict | None:
    require_golden(GOLDEN_PRESOLVE)
    path = _golden_path(name)
    if not path.exists():
        return None
    import json

    return json.loads(path.read_text(encoding="utf-8"))


def _write_golden(name: str, record: dict) -> None:
    import json

    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    text = json.dumps(record, ensure_ascii=False, sort_keys=True, indent=1)
    _golden_path(name).write_text(text + "\n", encoding="utf-8")


def _assert_matches_golden(name: str, record: dict) -> None:
    """골든과 비교(`UPDATE_GOLDEN_PRESOLVE=1` 일 때만 다시 쓴다 — `UPDATE_GOLDEN` 은 이 디렉터리를 쓰지 않는다)."""
    record = canonical(record)
    if presolve_update_requested():
        _write_golden(name, record)
        return
    golden = _read_golden(name)
    assert golden is not None, f"골든 없음: {_golden_path(name)} — {UPDATE_PRESOLVE_ENV}=1 로 만든다"
    # EI-10(precheck 결함 수정 — 판정이 바뀌는 단계)·EI-11(카드·처방 하드락 필터)·EI-14r3(OFF 하한 용량 증거)이 허용한 차이만 받는다. 이 디렉터리는
    # 다시 쓰지 않는다(EI-10c~f·EI-11 지시) — 허용 목록 밖의 변화는 EI-2 단계 허용 없이 그대로 위반이다.
    violations = check_record(f"presolve/{name}", golden, record, done=EI10_DONE + EI11_DONE + EI14C1_DONE + EI13C_DONE + EI12X_DONE,
                              stages={**EI10_STAGES, **EI11_STAGES, **EI14C1_STAGES, **EI13C_STAGES, **EI12X_STAGES})
    assert violations == [], "\n".join(violations)
    # `check_record` 는 쿼리를 수·부분열로만 본다 — 지점 표지(L0·L0'·L1 위치)는 여기서 따로 고정한다.
    #   EI-13c 가 줄인 쿼리는 그 지점 앞에서 사라진 만큼 표지를 앞당긴다. 13c2(팀 N+1)는 L0 앞이다. 13c3(차감의 근무코드 재조회)은
    #   원래 L0 와 L0p 사이였지만 EI-10g2 가 고정근무 차감을 precheck(L0) 앞으로 옮긴 뒤로는 L0 앞이라 모든 표지를 옮긴다(통합 2).
    _mark_not_shifted: set[tuple[str, str]] = set()
    _marks = {k: v - sum(EI13C_STAGES[d].query_delta.get(f"presolve/{name}", 0) for d in EI13C_DONE
                         if (d, k) not in _mark_not_shifted)
              for k, v in golden["queries"]["marks"].items()}
    assert _marks == record["queries"]["marks"], "쿼리 지점 표지가 달라졌다"
    _assert_no_forbidden_treatment_in_scope(name, record)
    _assert_ei5_legit_cards_kept(name, golden, record)


#: EI-11 뒤 EI5 카드 목록의 유일한 변화 — night_recovery 가 든 번들(11a-1 이 거름)이 coverage_min 단독 번들(11a-2 set cover)로
#: 바뀐다. 나머지 카드는 동결본 그대로여야 한다.
_EI5_CARD_SWAP = {"bundle:treatment:disable:night_recovery,treatment:threshold:coverage_min":
                  "bundle:treatment:threshold:coverage_min"}


def _card_ids(record: dict) -> list:
    inf = (record.get("http_detail") or {}).get("infeasibility") or {}
    return [o.get("option_id") for o in inf.get("resolution_options") or [] if isinstance(o, dict)]


def _assert_ei5_legit_cards_kept(name: str, golden: dict, record: dict) -> None:
    """EI-11 11r(리뷰 MEDIUM) — 정당 카드 보존 의미 단언. 카드 id 목록 == 동결 카드 − night_recovery 번들 + coverage_min 단독
    번들(같은 자리). 허용 패턴이 실제로 바뀐 칸만 열어도, 정당 카드가 기록 없이 사라지거나 순서가 바뀌는 것을 직접 잡는다."""
    if name != "EI5-precheck-block" or "11a2" not in EI11_DONE:
        return
    assert _card_ids(record) == [_EI5_CARD_SWAP.get(i, i) for i in _card_ids(golden)], "EI5 정당 카드 목록이 바뀌었다"


def _drop_marked_mapped_treatments(narrative) -> None:
    """11c 범위 — problem_list 에서 `policy_forbidden: true` 표지가 붙은 `mapped_treatments` 항목만 뺀다(진단 기록)."""
    for item in (narrative.get("problem_list") or []) if isinstance(narrative, dict) else []:
        if isinstance(item, dict) and isinstance(item.get("mapped_treatments"), list):
            item["mapped_treatments"] = [m for m in item["mapped_treatments"]
                                         if not (isinstance(m, dict) and m.get("policy_forbidden") is True)]


def _assert_no_forbidden_treatment_in_scope(name: str, record: dict) -> None:
    """EI-11 의미 단언 — 금지 처방 id 가 범위 안에 0(지시서 §4.3). 범위는 `EI11_DONE` 으로 고른다:
    "11a1" → `http_detail.infeasibility.resolution_options`(카드 칸) · "11a2" → `http_detail` 전체에서 `policy_forbidden`
    (막은 기록)·`resolution_narrative.problem_list`(set cover 와 무관하게 원인의 처방 전부를 싣는다 — 11c 가 표지한다)를 뺀 곳 ·
    "11c" → `http_detail` 전체에서 `policy_forbidden` 과 `policy_forbidden: true` 표지가 붙은 `mapped_treatments` 항목만 뺀 곳.
    금지 처방 id 는 카드 필터와 같은 판정 함수로 구한다(하드코딩 안 함)."""
    if "11a1" not in EI11_DONE:
        return
    from services.policy.card_filter import forbidden_treatments

    detail = copy.deepcopy(record.get("http_detail") or {})
    inf = detail.get("infeasibility") if isinstance(detail, dict) else None
    inf = inf if isinstance(inf, dict) else {}
    if "11c" in EI11_DONE:
        inf.pop("policy_forbidden", None)
        _drop_marked_mapped_treatments(inf.get("resolution_narrative"))
        scope, label = detail, "http_detail(policy_forbidden·표지 붙은 mapped_treatments 밖)"
    elif "11a2" in EI11_DONE:
        inf.pop("policy_forbidden", None)
        narrative = inf.get("resolution_narrative")
        if isinstance(narrative, dict):
            narrative.pop("problem_list", None)
        scope, label = detail, "http_detail(policy_forbidden·problem_list 밖)"
    else:
        scope, label = inf.get("resolution_options") or [], "resolution_options"
    text = json.dumps(scope, ensure_ascii=False)
    hits = sorted(tid for tid in forbidden_treatments() if tid in text)
    assert hits == [], f"presolve/{name}: {label} 에 금지 처방 {hits}"


# ── precheck 차단 전용 시나리오(이 파일만 쓴다 — SCENARIOS 에 넣지 않는다) ──────────
#
# RN 2명 + 기본 요구 D2/E2/N2(6명/일) → `check_global_day_capacity_shortage` 가
# 매일 GLOBAL_DAY_CAPACITY_SHORTAGE(블로킹 코드, payload.py `_BLOCKING_CODES`)를
# 낸다 — precheck 가 솔버 호출 전에 막는 가장 작은 재현.

PRECHECK_BLOCK_SCENARIO = Scenario(
    name="EI5-precheck-block",
    seed=partial(seed_ward, base_ward(2)),
    make_request=_request(2026, 11),
    make_user=make_head_nurse,
    ddl=BASE_DDL,
    description="precheck 차단 최소 재현 — RN 2명 vs 요구 D2/E2/N2(6명/일)",
)


def _with_patches(scenario: Scenario, *extra_patches: Callable) -> Scenario:
    """기존 시나리오를 바꾸지 않고 패치만 덧붙인 사본을 만든다(frozen dataclass)."""
    return dataclasses.replace(scenario, patches=scenario.patches + tuple(extra_patches))


# ── 스파이 — 기록·payload 호출 순서 + 카드 병합 방식을 '코드가 실제로 하는 일'로 잡는다 ──
#
# 두 블록 다 같은 함수(`services.roster_create_service._mark_config_generate_status` ·
# `services.precheck.build_blocking_payload`)를 부른다. 호출마다 함수 안에서
# `from X import Y` 로 다시 끌어오므로(지연 import), 모듈 속성을 몽키패치하면 다음 호출이
# 그 패치를 그대로 쓴다.


def _spy_mark_and_payload(order: list[str], captured: dict) -> Callable[[pytest.MonkeyPatch], None]:
    """기록·payload 호출 순서(`order`)와 payload 의 '기존 카드'(`captured["base_options"]`)를 남긴다."""

    def patch(mp: pytest.MonkeyPatch) -> None:
        import services.precheck as precheck
        import services.roster_create_service as rcs

        orig_mark = rcs._mark_config_generate_status
        orig_payload = precheck.build_blocking_payload

        def mark_spy(*args, **kwargs):
            order.append("mark")
            return orig_mark(*args, **kwargs)

        def payload_spy(*args, **kwargs):
            result = orig_payload(*args, **kwargs)
            order.append("payload")
            captured["base_options"] = list(
                ((result.get("infeasibility") or {}).get("resolution_options")) or [])
            return result

        mp.setattr(rcs, "_mark_config_generate_status", mark_spy)
        mp.setattr(precheck, "build_blocking_payload", payload_spy)

    return patch


def _spy_cause_bridge_cards(captured: dict) -> Callable[[pytest.MonkeyPatch], None]:
    """precheck 원인 브리지가 만든 카드(`captured["cause_cards"]`)를 남긴다(안 불리면 키가 없다)."""

    def patch(mp: pytest.MonkeyPatch) -> None:
        import services.ontology_graph.mcs_trace as mcs_trace

        orig_cards = mcs_trace.cause_to_resolution_options

        def cards_spy(*args, **kwargs):
            result = orig_cards(*args, **kwargs)
            captured["cause_cards"] = list(result or [])
            return result

        mp.setattr(mcs_trace, "cause_to_resolution_options", cards_spy)

    return patch


def _spy_night_card(captured: dict) -> Callable[[pytest.MonkeyPatch], None]:
    """나이트 차단이 낸 단일 카드 객체(`captured["night_card"]`)를 남긴다."""

    def patch(mp: pytest.MonkeyPatch) -> None:
        import services.ontology_graph.mcs_trace as mcs_trace

        orig_card = mcs_trace.night_forced_oversupply_card

        def card_spy(*args, **kwargs):
            result = orig_card(*args, **kwargs)
            captured["night_card"] = result
            return result

        mp.setattr(mcs_trace, "night_forced_oversupply_card", card_spy)

    return patch


class _InjectedCardBuildFailure(KeyError):
    """`night_forced_oversupply_card` 를 예외로 바꿔 끼운 자리(review 지적 1 — 카드·메시지
    생성 실패는 전환 전처럼 삼키지 않고 그대로 올라가야 한다)."""


def _raise_on_night_card_build(mp: pytest.MonkeyPatch) -> None:
    """나이트 탐지기의 Finding 조립(카드 생성) 단계만 예외로 바꿔 끼운다 — 산술 탐지
    (`detect_night_forced_oversupply`) 자체는 그대로 둔다. 전환 전에는 이 단계가 try 밖이라
    실패하면 예외가 그대로 올라갔다(`roster_create_service.py` 의 옛 `if _nfo:` 블록) — EI-5
    전환 때 등록표 호출(`run_presolve_detectors`)이 조립까지 같은 try 안에 들어가며 한 번
    삼켜졌던 것을 역방향 감사로 재현해 되돌렸다(EI-5b)."""
    import services.ontology_graph.mcs_trace as mcs_trace

    def _raiser(*_args, **_kwargs):
        raise _InjectedCardBuildFailure("characterization: 카드 생성 실패 주입")

    mp.setattr(mcs_trace, "night_forced_oversupply_card", _raiser)


class _InjectedRecordFailure(RuntimeError):
    """`_mark_config_generate_status` 를 예외로 바꿔 끼운 자리(설계서 §5.3·§7.1 '기록 실패 경로')."""


def _raise_on_mark(mp: pytest.MonkeyPatch) -> None:
    """기록 함수 자체를 예외로 바꿔 끼운다 — 함수 안의 자체 try/except 로 삼켜지는 DB 실패가
    아니라 설계서가 말하는 '`_mark_config_generate_status` 를 예외로 바꿔 끼움' 그대로다."""
    import services.roster_create_service as rcs

    def _raiser(*_args, **_kwargs):
        raise _InjectedRecordFailure("characterization: 기록 실패 주입")

    mp.setattr(rcs, "_mark_config_generate_status", _raiser)


# ── SQL 파라미터 캡처 — roster_config_attempt 행 내용(설계서 §5.3 '기록') ───────────
#
# 하네스의 쿼리 카운터(`capture.QueryLog`)는 '동사:테이블' 만 남긴다(값은 안 남긴다).
# 행 내용(status·intervened·snapshot 유무)을 보려면 같은 `after_cursor_execute` 에
# 독립된 리스너를 하나 더 걸어 바인드 파라미터까지 받는다 — 하네스 파일은 고치지 않는다.


def _run_with_sql_params(engine, scenario: Scenario) -> tuple[CapturedRun, list[tuple[str, object]]]:
    captured: list[tuple[str, object]] = []

    def _after(_conn, _cursor, statement, parameters, _context, _many) -> None:
        captured.append((statement, parameters))

    event.listen(engine, "after_cursor_execute", _after)
    try:
        run = run_with_output(engine, scenario)
    finally:
        event.remove(engine, "after_cursor_execute", _after)
    return run, captured


def _find_params(captured: list[tuple[str, object]], needle: str) -> dict | None:
    """SQL 문 본문에 `needle` 이 있는 **마지막** 실행의 파라미터를 컬럼명 → 값으로 돌려준다.

    pysqlite 는 바인드를 위치 인자(튜플)로 받는다(SQLAlchemy 가 `:name` 을 `?` 로 컴파일) —
    그래서 `INSERT INTO t (a, b, …) VALUES (…)` 의 컬럼 목록을 그대로 읽어 위치로 짝짓는다.
    """
    import re

    for statement, parameters in reversed(captured):
        if needle not in statement:
            continue
        match = re.search(r"\(([^()]+)\)\s*VALUES", statement, re.IGNORECASE)
        if not match or not parameters:
            return {}
        columns = [c.strip() for c in match.group(1).split(",")]
        return dict(zip(columns, parameters, strict=False))
    return None


# ── 스냅샷 내용 정규화 — 골든에 고정(설계서 §5.3 '기록') ───────────────────────────
#
# review 지적 — 기존엔 `attempt_params["snapshot"] is not None` 까지만 봐서, 스냅샷에
# 실제로 뭐가 실렸는지는 안 봤다. 그래서 내용이 바뀌는 변이가 안 잡혔다(M16 — 기록
# 함수에 `config_dict` 대신 `{}` 를 넘김 · M18 — 나이트 bctx 의 `config_dict` 를
# `precheck_config`(차감 전)에서 `config_dict`(차감 후)로 바꿔치기). 이제 내용을
# 정규화해 골든에 고정한다 — 두 변이 다 `config` 칸이 달라져 빨개진다.
#
# 가리는 것 둘(둘 다 "지금 동작"은 그대로 둔다 — 골든 비교에서만 가린다):
#   `at`(생성 시각)       하네스가 시계를 고정해(FIXED_NOW) 이미 결정론적이지만,
#                         "시각"이라는 성격 자체를 숨겨 둔다.
#   `_sa_instance_state`  `config` 에 섞여 들어오는 ORM `InstanceState` 의 repr —
#                         `_build_generate_snapshot` 의 `default=str` 폴백이 그대로
#                         문자열로 박아 넣는다(메모리 주소 포함, 실행마다 달라진다).
#                         ★(리뷰 반영 — EI-5b) 이 키 자체는 `_mask_volatile` 이
#                         아니라 `_assert_matches_golden` 이 부르는 `canonical()`
#                         (`canonical.py:96-99`, `_sa_*` 키 제거)이 지운다 — 그래서
#                         골든 3개 어디에도 `_sa_`·`0x` 가 없다. `.golden._mask_volatile`
#                         의 `0x…` 패턴(메인 골든의 `swallowed_failures` 와 같은 수단)은
#                         canonical() 이 못 지우는 **값 안의** 잔여 16진 주소용이고,
#                         같은 정규식의 12자리 16진 패턴(`<schedule_id>`)은 이 키와
#                         무관하게 스냅샷 전체에 걸린다 — 지금 픽스처(G1)엔 그런
#                         값이 없어 영향이 없지만, 앞으로 12-hex 형식 값(운영
#                         group_id 등)이 스냅샷에 들어오면 그 변화를 가릴 수 있다.


def _normalize_snapshot(raw: str | None) -> dict | None:
    """스냅샷 JSON 문자열 → 골든 비교용 정규화 dict. `None` 이면 `None` 그대로."""
    if raw is None:
        return None
    data = json.loads(raw)
    if isinstance(data, dict) and "at" in data:
        data["at"] = "<masked>"
    masked_text = _mask_volatile(json.dumps(data, ensure_ascii=False, sort_keys=True))
    return json.loads(masked_text)


# ── 0) 전제 확인 — 지금 골든 어디에도 precheck_block 경로가 없다 ───────────────────


def test_no_existing_scenario_hits_precheck_block():
    """기존 골든(S1~S9·변형)에 precheck_block 경로가 하나도 없다 — 그래서 시나리오를 추가했다.

    실행 없이 기존 `golden/*.json`(이미 두 번 실행해 결정론까지 확인된 기록)만 읽는다 —
    이 파일이 그 골든을 건드리지 않는다는 것도 같이 보인다.
    """
    paths = {name: _read_main_golden(name)["path"] for name in SCENARIOS}
    assert "precheck_block" not in paths.values(), paths


# ── 1) 정상 경로 — HTTP 상태·payload·기록·삭제·쿼리(골든 핀) ───────────────────────


def _night_scenario_names() -> list[str]:
    return ["S9", "S9-F"]


@pytest.mark.parametrize("name", _night_scenario_names())
def test_night_block_characterization(name, engine, engine_input_tables):
    """나이트 차단 — HTTP 500 · 카드 1장(덮어쓰기) · 기록 행(blocked) · 초안 삭제 · 쿼리 핀."""
    order: list[str] = []
    captured: dict = {}
    scenario = _with_patches(
        SCENARIOS[name], _spy_mark_and_payload(order, captured), _spy_night_card(captured))
    run, sql_params = _run_with_sql_params(engine, scenario)
    result = run.result

    assert result.outcome == "http_exception"
    assert result.error == "HTTP 500"
    assert result.path == "night_block"

    # 설계서 §5.3 표 '순서' 행 — 나이트는 precheck 와 반대로 payload → 기록이다.
    #   (review 지적 — 이 축이 특성화 테스트에 없어 순서 뒤바뀜 변이가 안 잡혔다.)
    assert order.index("payload") < order.index("mark"), "나이트 차단은 payload → 기록 순서여야 한다"

    detail = result.http_detail
    options = detail["infeasibility"]["resolution_options"]
    assert options == [captured["night_card"]], "나이트 차단은 카드 1장으로 기존 값을 덮어써야 한다"

    assert "insert:roster_config_attempt" in result.queries["tables"]
    assert "delete:schedules" in result.queries["tables"]

    attempt_params = _find_params(sql_params, "INSERT INTO roster_config_attempt")
    assert attempt_params is not None, "차단 기록 INSERT 가 없다"
    assert attempt_params["status"] == "blocked"
    assert attempt_params["intervened"] in (0, False)
    assert attempt_params["snapshot"] is not None

    _assert_matches_golden(name, {
        "outcome": result.outcome, "path": result.path, "error": result.error,
        "http_detail": detail,
        "queries": {"count": result.queries["count"], "tables": result.queries["tables"],
                    "marks": result.queries["marks"]},
        "snapshot": _normalize_snapshot(attempt_params["snapshot"]),
    })


def test_precheck_block_characterization(engine, engine_input_tables):
    """precheck 차단 — HTTP 500 · 기존 카드 앞에 원인 브리지 카드 prepend · 기록 · 삭제 · 쿼리 핀."""
    order: list[str] = []
    captured: dict = {}
    scenario = _with_patches(
        PRECHECK_BLOCK_SCENARIO,
        _spy_mark_and_payload(order, captured),
        _spy_cause_bridge_cards(captured),
    )
    run, sql_params = _run_with_sql_params(engine, scenario)
    result = run.result

    assert result.outcome == "http_exception"
    assert result.error == "HTTP 500"
    assert result.path == "precheck_block"

    assert order == ["mark", "payload"], "precheck 는 기록 → payload 순서여야 한다"

    detail = result.http_detail
    options = detail["infeasibility"]["resolution_options"]
    expected = captured.get("cause_cards", []) + captured["base_options"]
    assert options == expected, "precheck 는 원인 브리지 카드를 기존 카드 앞에 붙여야 한다"

    assert "insert:roster_config_attempt" in result.queries["tables"]
    assert "delete:schedules" in result.queries["tables"]

    attempt_params = _find_params(sql_params, "INSERT INTO roster_config_attempt")
    assert attempt_params is not None, "차단 기록 INSERT 가 없다"
    assert attempt_params["status"] == "blocked"
    assert attempt_params["intervened"] in (0, False)
    assert attempt_params["snapshot"] is not None

    _assert_matches_golden("EI5-precheck-block", {
        "outcome": result.outcome, "path": result.path, "error": result.error,
        "http_detail": detail,
        "queries": {"count": result.queries["count"], "tables": result.queries["tables"],
                    "marks": result.queries["marks"]},
        "snapshot": _normalize_snapshot(attempt_params["snapshot"]),
    })


# ── 2) 기록 실패 경로 — precheck=솔버 진행 / 나이트=그래도 차단(설계서 §5.3·§7.1) ───────


def _night_capable_containers(node, path: str = "$"):
    """payload 안의 `night_capable_nurses` 목록과 그 경로를 전부 찾는다(`(경로, 목록)`)."""
    if isinstance(node, dict):
        for key, value in node.items():
            sub = f"{path}.{key}"
            if key == "night_capable_nurses" and isinstance(value, list):
                yield sub, value
            else:
                yield from _night_capable_containers(value, sub)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _night_capable_containers(value, f"{path}[{index}]")


#: 회복 축이 실리는 payload 자리 네 종류(인덱스는 `[*]`) — golden_presolve 허용 차이가 가리키는 8곳(자리 4 × 간호사 2)의 자리다.
RECOVERY_AXIS_CONTAINERS = {
    "causes[*].details", "causes[*].evidence", "preflight_issues[*].evidence",
    "resolution_narrative.problem_list[*].raw_evidence",
}


def test_precheck_block_payload_carries_recovery_axis(engine, engine_input_tables):
    """EI-10c — 엔진 하드락 값이 실리면 월 나이트 용량 증거에 회복 축이 붙는다(30일 · 3N→2OFF · L=3 → 21).

    골든(`golden_presolve`)은 다시 쓰지 않고 허용 차이로만 대조하므로 값 자체는 여기서 고정한다. 허용 차이의 8곳이
    `causes[*].details`·`causes[*].evidence`·`preflight_issues[*].evidence`·`resolution_narrative.problem_list[*].raw_evidence`
    네 자리 × 간호사 2명이고, 네 자리 **전부**·간호사 **전원**이 21 이어야 한다(한 자리만 고정하면 나머지 여섯 곳이 다른 값이 되거나
    사라져도 통과한다 — 리뷰 지적). 21 은 월 상한 15 보다 커서 대표 병목(`capped_by`)과 차단 여부는 그대로다.
    """
    run = run_with_output(engine, PRECHECK_BLOCK_SCENARIO)
    found: dict[str, list[dict]] = {}
    for path, nurses in _night_capable_containers(run.result.http_detail["infeasibility"]):
        kind = re.sub(r"\[\d+\]", "[*]", path.removeprefix("$.").removesuffix(".night_capable_nurses"))
        found.setdefault(kind, []).extend(nurses)
    assert set(found) == RECOVERY_AXIS_CONTAINERS, sorted(found)
    assert sum(len(nurses) for nurses in found.values()) == 8, {kind: len(nurses) for kind, nurses in found.items()}
    for kind, nurses in found.items():
        assert nurses, kind
        assert all(nurse["cap_by_axis"].get("recovery") == 21 for nurse in nurses), (kind, nurses)
        assert all(nurse["night_cap"] == 15 and nurse["capped_by"] == "config" for nurse in nurses), (kind, nurses)


def test_golden_presolve_files_are_frozen():
    """골든 `golden_presolve/*.json` 3개는 다시 쓰지 않은 동결본이다 — sha256 이 고정값과 같다.

    S9·S9-F 가 잘못된 값으로 다시 써져도 비교 모드가 못 잡던 빈틈을 막는다(리뷰 지적). 의도해서 다시 쓸 때는 같은 커밋에서
    `FROZEN_SHA256` 도 함께 고친다(그 어긋남이 이 테스트가 켜는 신호다).
    """
    require_golden(GOLDEN_PRESOLVE)
    got = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(GOLDEN_DIR.glob("*.json"))}
    assert got == FROZEN_SHA256, "golden_presolve 가 동결본과 다르다 — 다시 썼다면 FROZEN_SHA256 도 같은 커밋에서 고친다"


def test_generic_update_flag_does_not_rewrite_golden_presolve(tmp_path, monkeypatch):
    """`UPDATE_GOLDEN=1`(기존 `golden/` 용 플래그)은 이 디렉터리를 다시 쓰지 않는다 — 전용 `UPDATE_GOLDEN_PRESOLVE=1` 만 쓴다.

    `GOLDEN_DIR` 을 임시 폴더(S9 사본)로 바꿔 확인한다 — 실제 동결본은 건드리지 않는다.
    """
    require_golden(GOLDEN_PRESOLVE)  # 동결본 S9 를 읽는다 — env·monkeypatch 보다 먼저(D29)
    from .golden import UPDATE_ENV as GENERIC_UPDATE_ENV

    # 전용 플래그 이름은 범용 플래그와 달라야 한다(리터럴로 고정 — 상수를 읽어 비교하면 같은 이름으로 바뀌어도 통과한다)
    assert GENERIC_UPDATE_ENV == "UPDATE_GOLDEN"
    assert UPDATE_PRESOLVE_ENV == "UPDATE_GOLDEN_PRESOLVE"

    frozen = (GOLDEN_DIR / "S9.json").read_bytes()
    monkeypatch.setattr(sys.modules[__name__], "GOLDEN_DIR", tmp_path)
    (tmp_path / "S9.json").write_bytes(frozen)
    record = json.loads(frozen)
    record["path"] = "changed_by_a_regression"  # 골든과 다른 값 — 비교 모드면 위반, 다시 쓰기 모드면 파일이 바뀐다

    monkeypatch.setenv("UPDATE_GOLDEN", "1")
    monkeypatch.delenv("UPDATE_GOLDEN_PRESOLVE", raising=False)
    with pytest.raises(AssertionError):  # 다시 쓰지 않고 비교 → 값이 달라 실패
        _assert_matches_golden("S9", record)
    assert (tmp_path / "S9.json").read_bytes() == frozen

    monkeypatch.delenv("UPDATE_GOLDEN", raising=False)
    monkeypatch.setenv("UPDATE_GOLDEN_PRESOLVE", "1")
    _assert_matches_golden("S9", record)  # 전용 플래그면 다시 쓴다(임시 폴더)
    assert (tmp_path / "S9.json").read_bytes() != frozen


def test_precheck_block_record_failure_proceeds_to_solver(engine, engine_input_tables):
    """`_mark_config_generate_status` 가 예외를 내면 precheck 차단은 **솔버로 진행**한다(HTTP 500 없음)."""
    scenario = _with_patches(PRECHECK_BLOCK_SCENARIO, _raise_on_mark)
    run = run_with_output(engine, scenario)
    result = run.result

    assert result.outcome == "golden_stop", (result.outcome, result.error)
    assert result.path == "first_attempt", "기록 실패를 삼키고 솔버(L1)까지 가야 한다"
    assert "L1" in result.queries["marks"]
    # 기록이 예외로 막혀 실제 INSERT 는 없었다(바깥 except 가 삼킨 뒤 계속 진행했을 뿐).
    assert "insert:roster_config_attempt" not in result.queries["tables"]
    # precheck 블록 안에서 멈췄으므로 그 블록의 `db.delete(schedule)` 도 실행되지 않았다.
    assert "delete:schedules" not in result.queries["tables"]


def test_night_block_record_failure_still_blocks(engine, engine_input_tables):
    """`_mark_config_generate_status` 가 예외를 내도 나이트 차단은 **그대로 HTTP 500 차단**한다."""
    scenario = _with_patches(SCENARIOS["S9"], _raise_on_mark)
    run = run_with_output(engine, scenario)
    result = run.result

    assert result.outcome == "http_exception"
    assert result.error == "HTTP 500"
    assert result.path == "night_block"
    # 기록 함수 자체가 예외만 냈으므로 실제 기록 행은 없다(설계서 '기록 행 없음').
    assert "insert:roster_config_attempt" not in result.queries["tables"]
    assert "update:roster_config" not in result.queries["tables"]
    # 차단은 기록 성패와 무관하게 일어난다 — 초안 삭제까지 그대로 실행된다.
    assert "delete:schedules" in result.queries["tables"]


# ── 3) Finding 조립(카드 생성) 실패 경로 — 삼키지 않고 전파(review 지적 1) ──────────────


def test_night_block_card_build_failure_propagates_not_swallowed(engine, engine_input_tables):
    """카드 생성(`night_forced_oversupply_card`) 실패는 삼켜서 솔버로 조용히 진행하면 안
    되고, 전환 전처럼 예외로 그대로 올라가야 한다. 직전 판(편집 역적용)·현재 판 비교로
    재현된 회귀(review 지적 1)를 역방향으로 고정한다 — 반대로 이 테스트가 다시 삼키는
    쪽으로 돌아가면 빨개진다."""
    scenario = _with_patches(SCENARIOS["S9"], _raise_on_night_card_build)
    run = run_with_output(engine, scenario)
    result = run.result

    assert result.outcome == "exception", (
        "카드 생성 실패를 삼켜 솔버로 진행하면 안 된다 — 예외로 전파돼야 한다", result.outcome)
    assert result.path == "error"
    assert "characterization: 카드 생성 실패 주입" in (result.error or "")
    # 산술 탐지(detect) 자체는 성공했으므로 솔버 호출(L1)까지 가지 않았다 — 조립 단계에서
    #   멈췄다(view 조립 쿼리는 이미 끝난 뒤이므로 그 쿼리들은 찍혀 있을 수 있다).
    assert "L1" not in result.queries["marks"], "솔버까지 가면 안 된다 — 조립 실패가 전파돼야 한다"
