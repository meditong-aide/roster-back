"""골든 사다리 모드 · 성공 모드(L3) 기록기 — 설계서 §7.1 EI-1b · §4.6 · §3.2 · §3.7.

엔진 입구 대역(`EngineRecorder`)
  `roster_create_service.generate_roster_cp_sat`(`_run_cp_sat_basic` 안 :3694 호출)를 대신한다.
  **원본 엔진을 부르지 않는다**(solve 0). 그래서 L2(엔진 조립 결과)는 비어 있다. 시도마다 다음을
  남기고 정해진 결과를 돌려준다.
    - 첫 시도: L1 인자 전체(`layers["L1"][0]` — 다른 시나리오의 L1 과 같은 모양)
    - 모든 시도(`layers["attempts"]`): 호출 지점 라벨 · 결과(fail/success) · 그때까지 쿼리 수
      (`queries_before` — 시도 사이 재조회 수를 셀 수 있다) · 첫 시도 대비 L1 차이(config·간호사·그 밖
      인자) · 그 순간의 엔진 env(`env_effective` — 실행 중 토글 포함, 기록만)
  (`queries_before` 는 골든 기록에 남지만 digest 에서는 뺀다 — 엔진 입력이 아니라 쿼리 위치다. canonical.py)
  ladder 모드(S1-L·S1-LT) — 기본은 **실패 결과**를 돌려준다. 앱이 그 결과를 검증해 실패로 보고
    자동 완화 사다리(:6978·:7088·:7151·:7226)와 탐색(:7341·:7440·:7680·:7707·:7895)으로 넘어간다.
    단 탐색이 `max_nig_per_month` 를 첫 시도보다 올린 시도만 성공으로 돌려준다 — 그래야 탐색 결과에
    '검증된 월 야간 상한 완화' 옵션이 생겨 야간한도 개인 지목(:7861-7865 → :7895)에 닿는다.
    기준 설정 그대로의 재계산은 실패라 `baseline_feasible`·`timed_out` 은 거짓이다(:7659).
  post 모드(S1-L3) — 첫 시도에 **성공 결과**(결정론 소형 근무표)를 돌려줘 후처리까지 간다.
결과 모양(코드에서 확인)
  `_run_cp_sat_basic` 은 dict 의 `roster`·`satisfaction_data`·`roster_system` 을 꺼낸다(:3708·:3782-3786).
  - roster: {nurse_id: [근무코드 id × 일수]} — 엔진처럼 대표코드를 `_build_shift_normalizer` 의
    main→id 로 바꾼다(OFF 행이면 'OFF').
  - 실패: 전원 OFF. 검증(`_validate_generated_roster` :4379)이 '실근무 0건'으로 보고 `_infeasible_empty`
    를 세운다(사다리 1~3차 트리거 :6970). 고정근무자가 있으면 실근무가 생기므로 대역 config 의
    일자별 요구로 '특정일 커버리지 0'(:4439) 판정을 받는다 — 실제 RosterSystem 처럼 config 를 둔다.
    `_n2n_min_gap_stage2_failed`(4차 트리거 :7218)는 그 시도 config 의 n2n_min_gap 이 켜져 있으면 참.
    ★ 설계 §7.1 의 11곳 순회를 위한 기록기 정책이다. 운영 엔진은 1단계 INFEASIBLE 일 때 stage2 에
      들어가지 않아(cp_sat/fallback_lex.py:3989-3991 `best_short is None` → return False) 이 조합(실근무 0 +
      stage2 실패 플래그)이 생기지 않는다 — 플래그는 stage2 에서만 세워진다(:4173-4176). 앱은 1~3차가
      실패하면 roster_system 을 바꾸지 않으므로(:7032·:7119·:7182 은 성공일 때만 교체) :7218 은 첫 시도의
      이 플래그를 읽고 :7226 을 지난다. 그래서 S1-L 탐색 3곳(:7680·:7707·:7895)의 n2n_min_gap=0 은
      **하네스가 만든 값**이다(운영 동작의 근거로 읽지 않는다).
  - 성공: 결정론 패턴(D D E E N N O O O O, 간호사 순번만큼 밀기)을 엔진 마지막 단계와 같게
    `CPSATBasicEngine._apply_work_shift_overrides`(cp_sat_basic.py:1900)에 통과시킨다. 그 호출도 L3 에 남는다.
  - roster_system: `HarnessRosterSystem`(앱이 getattr·setattr 로 쓰는 칸만). constraint_impact 분석은
    이 대역으로 성립하지 않아 `enabled=False` 로 떨어진다(앱이 예외를 삼킨다 :4751).
L3 후처리 입력(post 모드, §3.7 · 코드 :8053-8120 + 엔진 :1900)
  `_apply_work_shift_overrides` · `postprocess_health_leave` · `postprocess_sleep_off` ·
  `postprocess_off_swap` · `postprocess_leave_mask` · `postprocess_oncall` 의 인자를 **부르기 직전**에 남기고
  원본을 부른다. db 는 뺀다. schedule 은 실행마다 달라지는 id(uuid4 :8315)와 시각 칸을 뺀 값,
  current_user·req 는 pydantic dump, 큰 객체(latest_config·nurses_data)는 내용 주소(sha) 한 벌로 묶는다.
호출 지점 라벨
  `roster_create_service.py` 의 `_run_cp_sat_basic(` 호출 줄을 소스 순서로 찾아 순번 → 라벨로 정한다.
  골든에는 줄 번호를 쓰지 않는다(위쪽 코드가 바뀌면 줄이 밀린다). 지점 수가 12 가 아니면 테스트가 실패한다.
  ★ 2026-10-13(dev `55dacd4` 재수신) — dev 가 `_ladder_retry` 헬퍼(4차 완화 "전날 N 금지 최소화"가
    쓰는 공용 재시도 함수)를 **`if validation_error:` 블록보다 앞**(소스상 `first_attempt` 바로
    다음)에 정의하면서, 그 헬퍼 **본문** 안의 `_run_cp_sat_basic(` 호출이 소스 순서로는 2번째
    호출 지점이 됐다(11→12). 라벨은 이 모듈이 소스 "줄 번호" 로만 매긴다(아래 LADDER_SITE_LABELS
    주석) — 그런데 이 자리는 함수 **정의**일 뿐이라 실제로 불리는 시점(실행 순서)은 `_ladder_retry`
    가 호출되는 곳(사다리 4차 "ban_night_minimize", `isolated_work_soft` 완화가 실패한 **뒤**)이
    결정한다. 즉 "라벨 소스 위치(2번째) == 실행 순서(2번째)"라는 종전 11곳의 암묵 전제가 이
    지점에서는 깨진다 — `test_ladder.py` 의 `EXPECTED_LADDER_SITES`(실행 순서 단언)는 이 지점을
    별도로 다룬다(그 파일 주석 참고). 이름은 소스 위치가 아니라 **의미**로 붙였다
    (`ban_night_minimize_retry`).
"""

from __future__ import annotations

import calendar
import functools
import inspect
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from .canonical import canonical, sha256_digest
from .engine_env import env_effective

#: 기록기 모드(Capture.stop_at 값).
RECORDER_MODES = ("ladder", "post")
#: `_run_cp_sat_basic(` 호출 지점 — 소스 순서(49f14bd 기준 :6866·:6978·:7088·:7151·:7226·:7341·
#: :7440·:7680·:7707·:7895·:8204). ★ dev `55dacd4` 가 2번째 자리(`_ladder_retry` 헬퍼 본문)를
#: 새로 끼워 넣었다(11→12, 55dacd4 기준 :6945·:7068·:7104·:7215·:7279·:7418·:7601·:7700·:7940·
#: :7967·:8155·:8464). 새 지점(`ban_night_minimize_retry`)은 소스상 `first_attempt` 다음이지만,
#: 그건 헬퍼 **정의** 줄일 뿐이고 실제 호출은 `isolated_work_soft` 완화가 실패한 **뒤** 사다리
#: 4차("ban_night_minimize")에서 일어난다 — 라벨 순서(소스 위치)와 실행 순서가 이 지점만 어긋난다
#: (모듈 docstring ★ 참고). `test_ladder.py` 의 `EXPECTED_LADDER_SITES`가 이 지점을 실행 순서
#: 쪽에서 별도로 다룬다.
LADDER_SITE_LABELS = (
    "first_attempt", "ban_night_minimize_retry", "team_min_soft", "same_shift_soft",
    "isolated_work_soft", "n2n_min_gap_release", "mus_diag", "undiag_probe", "weekend_multi",
    "weekend_single", "night_limit_single", "a1_probe",
)
_RCS_FILE = "roster_create_service.py"
#: 성공 결과 패턴(대표코드) — 간호사 순번만큼 밀어 날마다 근무가 있게 한다.
SUCCESS_PATTERN = ("D", "D", "E", "E", "N", "N", "O", "O", "O", "O")
_MISSING = object()


class RecorderFailure(BaseException):
    """기록기 자체 오류 — 앱의 `except Exception` 에 삼켜져 경로가 조용히 바뀌지 않게 BaseException 이다."""


# ── 호출 지점 ───────────────────────────────────────────────────────────


@functools.lru_cache(maxsize=1)
def call_sites() -> tuple[int, ...]:
    """`_run_cp_sat_basic(` 호출 줄 번호(주석·정의 줄 제외, 소스 순)."""
    import services.roster_create_service as rcs

    sites = []
    for number, line in enumerate(Path(rcs.__file__).read_text(encoding="utf-8").splitlines(), 1):
        code = line.split("#", 1)[0]
        if "_run_cp_sat_basic(" in code and not code.lstrip().startswith("def "):
            sites.append(number)
    return tuple(sites)


def site_lines() -> dict[str, int]:
    """라벨 → 호출 줄(보고용). 지점 수가 라벨 수와 다르면 `site<순번>` 라벨을 쓴다(테스트가 잡는다)."""
    sites = call_sites()
    labels = (LADDER_SITE_LABELS if len(sites) == len(LADDER_SITE_LABELS)
              else tuple(f"site{i}" for i in range(1, len(sites) + 1)))
    return dict(zip(labels, sites, strict=True))


def _label_for_line(line: int) -> str:
    """호출 프레임의 줄 → 그 줄 이하에서 가장 가까운 호출 지점 라벨."""
    by_line = {v: k for k, v in site_lines().items()}
    starts = [s for s in by_line if s <= line]
    return by_line[max(starts)] if starts else f"unknown:{line}"


def caller_site() -> str:
    """엔진 입구를 부른 `_run_cp_sat_basic` 의 호출 지점 라벨."""
    frame = sys._getframe(1)
    while frame is not None:
        code = frame.f_code
        if code.co_name == "_run_cp_sat_basic" and code.co_filename.endswith(_RCS_FILE):
            back = frame.f_back
            if back is not None and back.f_code.co_filename.endswith(_RCS_FILE):
                return _label_for_line(back.f_lineno)
            return "unknown"
        frame = frame.f_back
    return "outside_run_cp_sat_basic"


# ── 결과 대역 ───────────────────────────────────────────────────────────


class HarnessRosterSystem:
    """엔진 결과 `roster_system` 대역 — 앱이 getattr·setattr 로 다루는 칸만 둔다."""

    def __init__(self, **attrs):
        self.__dict__.update(attrs)


def _num_days(bound: dict) -> int:
    """대상 달 일수."""
    return calendar.monthrange(int(bound["year"]), int(bound["month"]))[1]


def _main_to_shift_id(config_data: dict) -> dict[str, str]:
    """엔진과 같은 대표코드 → 근무코드 id 매핑(cp_sat_basic `_build_shift_normalizer`)."""
    from services.cp_sat_basic import _build_shift_normalizer

    return _build_shift_normalizer(config_data.get("shift_definitions"))[1] or {}


def _engine_config(config_data: dict) -> SimpleNamespace:
    """검증기가 읽는 엔진 config 칸(근무 종류·일자별 요구·근무코드 정의)."""
    shift_types = ["D", "E", "N", "O"] + (["M"] if config_data.get("use_mid") else [])
    return SimpleNamespace(
        shift_types=shift_types,
        daily_shift_requirements=dict(config_data.get("daily_shift_requirements") or {}),
        daily_shift_requirements_by_day=list(config_data.get("daily_shift_requirements_by_day") or []),
        shift_definitions=list(config_data.get("shift_definitions") or []),
    )


def _roster_system(bound: dict, *, failed: bool) -> HarnessRosterSystem:
    """결과의 roster_system 대역.

    실패 결과의 `_n2n_min_gap_stage2_failed` 는 기록기 정책이다(운영 엔진은 1단계 해가 없으면 stage2 에
    가지 않아 이 플래그를 세우지 않는다 — 모듈 docstring '결과 모양' 참고).
    """
    config_data = bound["config_data"]
    return HarnessRosterSystem(
        config=_engine_config(config_data), num_days=_num_days(bound), nurses=[],
        _primary_solver_status="INFEASIBLE" if failed else "OPTIMAL",
        _n2n_min_gap_stage2_failed=failed and int(config_data.get("n2n_min_gap") or 0) > 0,
    )


def failure_result(bound: dict) -> dict:
    """실패(INFEASIBLE 류) 결과 — 전원 OFF."""
    off = _main_to_shift_id(bound["config_data"]).get("O", "O")
    days = _num_days(bound)
    roster = {str(n.get("nurse_id")): [off] * days for n in bound["nurses_data"]}
    return {"roster": roster, "satisfaction_data": {}, "roster_system": _roster_system(bound, failed=True)}


def deterministic_roster(bound: dict) -> dict[str, list[str]]:
    """결정론 소형 근무표 — 패턴을 간호사 순번만큼 밀고 대표코드를 근무코드 id 로 바꾼다."""
    to_id = _main_to_shift_id(bound["config_data"])
    days = _num_days(bound)
    size = len(SUCCESS_PATTERN)
    return {str(n.get("nurse_id")): [to_id.get(c, c) for c in
                                     (SUCCESS_PATTERN[(i + d) % size] for d in range(days))]
            for i, n in enumerate(bound["nurses_data"])}


def success_result(bound: dict) -> dict:
    """성공 결과 — 결정론 근무표를 엔진 마지막 단계(근무코드 덮기)에 통과시킨다."""
    from services.cp_sat_basic import cp_sat_engine

    roster = cp_sat_engine._apply_work_shift_overrides(
        roster_map=deterministic_roster(bound), nurses_data=bound["nurses_data"],
        shift_definitions=bound["config_data"].get("shift_definitions"))
    return {"roster": roster, "satisfaction_data": {}, "roster_system": _roster_system(bound, failed=False)}


# ── 시도 차이 ───────────────────────────────────────────────────────────


def _dict_delta(first: dict, current: dict) -> dict:
    """dict 두 개의 최상위 키 차이 — 바뀌거나 새로 생긴 값 · 사라진 키."""
    changed = {k: current[k] for k in sorted(current) if first.get(k, _MISSING) != current[k]}
    removed = sorted(k for k in first if k not in current)
    return {"changed": changed, "removed": removed}


def _nurses_delta(first: list, current: list) -> dict:
    """간호사 목록 차이 — nurse_id 별 바뀐 칸 · 순서 변화 · 빠지거나 생긴 사람."""
    base = {n.get("nurse_id"): n for n in first}
    out: dict = {}
    for nurse in current:
        nid = nurse.get("nurse_id")
        if nid not in base:
            out[nid] = {"added": nurse}
            continue
        delta = _dict_delta(base[nid], nurse)
        if delta["changed"] or delta["removed"]:
            out[nid] = delta
    for nid in sorted(set(base) - {n.get("nurse_id") for n in current}):
        out[nid] = {"removed_nurse": True}
    order = [n.get("nurse_id") for n in current]
    if order != [n.get("nurse_id") for n in first] and set(order) == set(base):
        out["__order__"] = order
    return out


def l1_delta(first: dict, current: dict) -> dict:
    """첫 시도 L1 대비 이 시도 L1 의 차이(config_data·nurses_data 는 키 단위, 그 밖은 새 값)."""
    out: dict = {}
    for key in sorted(set(first) | set(current)):
        before, after = first.get(key, _MISSING), current.get(key, _MISSING)
        if before == after:
            continue
        if key == "config_data" and isinstance(before, dict) and isinstance(after, dict):
            out[key] = _dict_delta(before, after)
        elif key == "nurses_data" and isinstance(before, list) and isinstance(after, list):
            out[key] = _nurses_delta(before, after)
        else:
            out[key] = None if after is _MISSING else after
    return out


# ── 엔진 입구 대역 ──────────────────────────────────────────────────────


def bind_arguments(original, args, kwargs) -> dict:
    """원본 시그니처로 인자를 묶는다(기본값 포함)."""
    bound = inspect.signature(original).bind(*args, **kwargs)
    bound.apply_defaults()
    return dict(bound.arguments)


def _raised_night_cap(first: dict, config_data: dict) -> bool:
    """이 시도 config 의 월 야간 상한이 첫 시도보다 높은가(탐색의 상한 완화)."""
    try:
        before = int(first["config_data"].get("max_nig_per_month") or 0)
        return int(config_data.get("max_nig_per_month") or 0) > before > 0
    except (TypeError, ValueError):
        return False


class EngineRecorder:
    """엔진 입구 대역 — 시도마다 L1·차이·env 를 남기고 실패/성공 결과를 돌려준다."""

    def __init__(self, capture, mode: str):
        if mode not in RECORDER_MODES:
            raise ValueError(f"모르는 기록기 모드: {mode}")
        self.capture = capture
        self.mode = mode
        self.first: dict | None = None

    def _decide(self, bound: dict) -> str:
        """post 는 늘 성공, ladder 는 월 야간 상한을 올린 탐색 시도만 성공."""
        if self.mode == "post":
            return "success"
        return "success" if _raised_night_cap(self.first, bound["config_data"]) else "fail"

    def _record(self, bound: dict, site: str, env: dict) -> str:
        """L1(첫 시도 전체) · 시도 기록을 남기고 결과 종류를 정한다."""
        record = canonical(bound)
        layers = self.capture.layers
        if self.first is None:
            self.first = record
            layers["L1"].append(record)
        kind = self._decide(bound)
        layers["attempts"].append({"seq": len(layers["attempts"]), "site": site, "result": kind,
                                   "queries_before": len(self.capture.queries.statements),
                                   "delta": l1_delta(self.first, record), "env": env})
        return kind

    def spy(self, original):
        """엔진 입구 대역 함수(원본은 부르지 않는다). 기록기 오류는 RecorderFailure 로 올린다."""
        def recorder(*args, **kwargs):
            env = env_effective()
            site = caller_site()
            self.capture.queries.mark("L1")
            try:
                bound = bind_arguments(original, args, kwargs)
                kind = self._record(bound, site, env)
                return success_result(bound) if kind == "success" else failure_result(bound)
            except Exception as exc:  # noqa: BLE001 — 기록기 오류는 앱이 삼키지 못하게 바꿔 올린다
                raise RecorderFailure(f"엔진 입구 기록기 오류({site}): {type(exc).__name__}: {exc}") from exc
        return recorder


# ── L3 후처리 입력 ──────────────────────────────────────────────────────

#: (모듈, 클래스 또는 None, 함수) — 기록 순서는 실제 호출 순서를 따른다.
L3_TARGETS = (
    ("services.cp_sat_basic", "CPSATBasicEngine", "_apply_work_shift_overrides"),
    ("services.leave.health_leave_postprocess", None, "postprocess_health_leave"),
    ("services.leave.sleep_off_postprocess", None, "postprocess_sleep_off"),
    ("services.roster_create_service", None, "postprocess_off_swap"),
    ("services.leave_mask_postprocess", None, "postprocess_leave_mask"),
    ("services.oncall_postprocess", None, "postprocess_oncall"),
)
#: 내용 주소로 한 벌만 남기는 큰 인자.
_L3_BLOB_ARGS = frozenset({"latest_config", "nurses_data"})
_SCHEDULE_FIELDS = ("office_id", "group_id", "year", "month", "version", "config_id",
                    "created_by", "status", "dropped", "name")


def _schedule_view(schedule) -> dict:
    """근무표 행 — 실행마다 다른 id(uuid4)·시각 칸을 뺀 값."""
    return canonical({f: getattr(schedule, f, None) for f in _SCHEDULE_FIELDS})


def _l3_value(name: str, value, objects: dict):
    """L3 인자 하나 → 기록값(큰 객체는 내용 주소)."""
    if name == "schedule":
        return _schedule_view(value)
    if hasattr(value, "model_dump"):
        return canonical(value.model_dump())
    record = canonical(value)
    if name in _L3_BLOB_ARGS:
        key = sha256_digest(record)[:16]
        objects.setdefault(key, record)
        return {"__object__": key}
    return record


def _l3_spy(original, func_name: str, layer: dict):
    """후처리 함수 인자를 부르기 직전에 남기고 원본을 부른다(원본 예외는 그대로 나간다)."""
    def spy(*args, **kwargs):
        try:
            bound = bind_arguments(original, args, kwargs)
            args_record = {k: _l3_value(k, v, layer["objects"]) for k, v in bound.items()
                           if k not in ("db", "self")}
        except Exception as exc:  # noqa: BLE001 — 기록기 오류는 앱이 삼키지 못하게 바꿔 올린다
            raise RecorderFailure(f"L3 기록기 오류({func_name}): {type(exc).__name__}: {exc}") from exc
        layer["calls"].append({"func": func_name, "args": args_record})
        return original(*args, **kwargs)
    spy.__name__ = func_name
    return spy


def install_l3_spies(mp: pytest.MonkeyPatch, layers: dict) -> None:
    """L3 기록기를 건다(mp 가 끝나면 원상 복구)."""
    import importlib

    layer = layers.setdefault("L3", {"calls": [], "objects": {}})
    for module_name, class_name, func_name in L3_TARGETS:
        owner = importlib.import_module(module_name)
        if class_name:
            owner = getattr(owner, class_name)
        mp.setattr(owner, func_name, _l3_spy(getattr(owner, func_name), func_name, layer))


# ── 디스크 쓰기 격리(live graph) ────────────────────────────────────────

#: 실행 중 만든 live graph 임시 폴더 — 테스트가 세고 지운다.
LIVE_GRAPH_DIRS: list[Path] = []


def redirect_live_graph(mp: pytest.MonkeyPatch) -> None:
    """UNRECOVERABLE 경로의 `dump_live_graph_export`(저장소 `tools/harness/reports/` 쓰기)를 임시 폴더로."""
    import services.live_graph_export as live_graph_export

    target = Path(tempfile.mkdtemp(prefix="engine_input_live_graph_"))
    LIVE_GRAPH_DIRS.append(target)
    mp.setattr(live_graph_export, "_REPORTS_DIR", target)


def drain_live_graph_dirs() -> list[int]:
    """임시 폴더마다 쓰인 graph_export 수를 세고 폴더를 지운다(목록도 비운다)."""
    counts = []
    for path in LIVE_GRAPH_DIRS:
        counts.append(len(list(path.rglob("graph_export.json"))))
        shutil.rmtree(path, ignore_errors=True)
    LIVE_GRAPH_DIRS.clear()
    return counts
