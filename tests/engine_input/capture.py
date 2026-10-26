"""골든 하네스 캡처층 — 설계서 §7.1 '캡처 층'·'쿼리 수·순서 카운터'.

캡처 층(모두 monkeypatch 로 감싸고 **원본을 그대로 부른다** — 동작 불변)
  L0   precheck 입력      `services.precheck.run_runtime_precheck`
                          (호출: roster_create_service.py:6532, 함수 안 지연 import :6457)
  L0'  탐지기 인자        `services.ontology_graph.lagrangian.detect_night_forced_oversupply`
                          (호출: roster_create_service.py:6797, 지연 import :6770)
  L1   엔진 인자          `services.roster_create_service.generate_roster_cp_sat`
                          (모듈 전역 import :60, 호출 `_run_cp_sat_basic` :3694)
  L2   엔진 조립 결과     `CPSATBasicEngine.create_config_from_db`·`create_nurses_from_db`
                          (cp_sat_basic.py:446·:744, `generate_roster` 안 :992·:1041)
                          ★ EI-10c — precheck 가 엔진 하드락 실효값을 읽느라 부르는 `create_config_from_db`
                          1회(`hard_lock_view.engine_hard_lock` 안)는 'L1 밖 L2 호출'이 아니라
                          `precheck_hard_lock_calls` 로 따로 센다(설계서 §5.3 EI-8 이 정한 소비자 호출).
멈춤(`stop_at`)
  "L2"(기본) — L1 이 원본을 부르면 엔진은 L2 두 함수를 지나 RosterSystem(:1055)을 짓기 전에 L2
    기록기를 만난다. `create_nurses_from_db` 결과를 기록한 직후 GoldenStop(BaseException)을 던진다 —
    엔진 입구부터 그 자리까지(cp_sat_basic.py:937-1041) DB 조회·solve 는 없다. 래퍼(:5173-5187)는
    `except HTTPException`·`except Exception` 만 받으므로 GoldenStop 은 실패 기록 없이 나온다.
  "primary_build"(변형 S1-P, SKIP_PRIMARY=0) — L2 는 기록만 하고 계속 간다. primary 분기
    (`_optimize_with_enhanced_constraints` :1537 → `_quick_initial_solve` :2075 → `_build_full_model`
    :2278)에서 `_build_full_model` 원본이 돌려준 모델의 **빌드 요약**(층 "P")을 남기고 GoldenStop.
    그래서 `solver.Solve`(:2300)는 돌지 않는다. 요약은 변수 수·제약 종류별 수·목적식 항 수다
    (모델 proto 직렬화 해시는 쓰지 않는다 — 설계서 §7.1 S1-P).
  "ladder"·"post"(EI-1b — ladder.py) — L1 자리에 엔진 입구 대역(`EngineRecorder`)을 건다. 원본 엔진을
    부르지 않고 시도마다 층 "attempts" 를 남긴 뒤 실패(ladder)·성공(post) 결과를 돌려준다. 멈추지 않고
    서비스 끝(UNRECOVERABLE HTTP 500 · 정상 반환)까지 간다. post 는 L3 후처리 입력 기록기도 건다.
안전장치(BaseException 이라 app 의 `except Exception` 에 삼켜지지 않는다)
  - SolveBlocked: `CpSolver.solve`(Solve·SolveWithSolutionCallback 도 이리로 온다)가 불리면 즉시
  - SeparateSessionBlocked: `db.client2.SessionLocal()` — 같은 StaticPool 연결이라 닫힐 때
    하네스 트랜잭션을 롤백(또는 commit)해 버린다
쿼리 카운터
  `after_cursor_execute` 로 하네스 연결이 실행한 문장을 '동사:테이블' 로 정규화해 순서대로 남긴다.
  SAVEPOINT·RELEASE·PRAGMA 같은 하네스 몫은 따로 센다(운영 MSSQL 에는 없는 문장).
"""

from __future__ import annotations

import inspect
import re
import sys
from collections import Counter
from dataclasses import dataclass, field

import pytest
from sqlalchemy import event

from .canonical import canonical
from .ladder import RECORDER_MODES, EngineRecorder, install_l3_spies
from .proto_compat import model_to_pb

#: 탐지기가 config 에서 실제로 읽는 키(lagrangian.py:582·:583·:533-537). 설계서 §7.1 은 '수요 3키'
#: 라고 적었지만 코드는 off_first·파견 제외·프리셉티 맵도 읽는다 — 코드 사실을 따른다.
DETECTOR_CONFIG_KEYS = (
    "daily_shift_requirements", "daily_shift_requirements_by_day",
    "daily_shift_requirements_max_by_day", "off_first",
    "coverage_exclude_nurse_days", "preceptee_period_by_nurse_id",
)


class GoldenStop(BaseException):
    """L2 기록 뒤 solve 전에 프로덕션 경로를 멈춘다(`except Exception` 에 걸리지 않는다)."""


class SolveBlocked(BaseException):
    """하네스 안에서 CpSolver.solve 가 불렸다 — 하네스는 solve 를 돌리지 않는다."""


class SeparateSessionBlocked(BaseException):
    """생성 경로가 별도 세션(`SessionLocal()`)을 열려 했다 — 하네스 트랜잭션이 깨진다."""


# ── 쿼리 정규화 ─────────────────────────────────────────────────────────

_IDENT = r'(?:\[[^\]]+\]|"[^"]+"|`[^`]+`|[A-Za-z_#][\w$#]*)'
_TABLE_RE = re.compile(rf"\b(?:FROM|JOIN|INTO|UPDATE)\s+({_IDENT}(?:\s*\.\s*{_IDENT})*)", re.IGNORECASE)
_VERB_RE = re.compile(r"^\s*([A-Za-z]+)")
_HARNESS_RE = re.compile(r"^\s*(?:SAVEPOINT|RELEASE|ROLLBACK|PRAGMA|BEGIN|COMMIT)\b", re.IGNORECASE)
_LITERAL_RE = re.compile(r"'(?:[^']|'')*'|--[^\n]*|/\*.*?\*/", re.DOTALL)


def _table_name(dotted: str) -> str:
    """`[db].dbo."t"` → `t`. INFORMATION_SCHEMA 는 접두를 남긴다."""
    parts = [p.strip().strip('[]"`').lower() for p in re.split(r"\s*\.\s*", dotted)]
    if "information_schema" in parts[:-1]:
        return f"information_schema.{parts[-1]}"
    return parts[-1]


def normalize_statement(sql: str) -> str:
    """SQL 한 문장 → '동사:테이블[+테이블…]'(등장 순·중복 제거). 테이블이 없으면 '동사:-'."""
    masked = _LITERAL_RE.sub(" ", sql)
    verb_match = _VERB_RE.match(masked)
    verb = verb_match.group(1).lower() if verb_match else "?"
    tables: list[str] = []
    for match in _TABLE_RE.finditer(masked):
        name = _table_name(match.group(1))
        if name not in tables:
            tables.append(name)
    return f"{verb}:{'+'.join(tables) if tables else '-'}"


@dataclass
class QueryLog:
    """하네스 연결이 실행한 문장 기록 — 켜져(armed) 있을 때만 센다."""

    statements: list[str] = field(default_factory=list)
    harness_statements: int = 0
    marks: dict[str, int] = field(default_factory=dict)
    armed: bool = False
    _engine: object = None

    def attach(self, engine) -> None:
        """엔진에 after_cursor_execute 리스너를 건다."""
        event.listen(engine, "after_cursor_execute", self._after)
        self._engine = engine

    def detach(self) -> None:
        """붙인 리스너가 있으면 뗀다(두 번 불러도 된다)."""
        if self._engine is not None:
            event.remove(self._engine, "after_cursor_execute", self._after)
            self._engine = None

    def _after(self, _conn, _cursor, statement, _params, _context, _many) -> None:
        """문장 하나를 정규화해 남긴다(하네스 몫은 따로 센다)."""
        if not self.armed:
            return
        if _HARNESS_RE.match(statement):
            self.harness_statements += 1
            return
        self.statements.append(normalize_statement(statement))

    def mark(self, name: str) -> None:
        """그 지점까지의 문장 수를 처음 한 번만 적는다."""
        self.marks.setdefault(name, len(self.statements))

    def summary(self, path: str) -> dict:
        """경로별 기록 — 수·순서열·지점 표지·하네스 몫."""
        return {"path": path, "count": len(self.statements), "tables": list(self.statements),
                "marks": dict(self.marks), "harness_statements": self.harness_statements}


# ── primary 빌드 요약 ──────────────────────────────────────────────────


def primary_build_summary(model) -> dict:
    """CP-SAT 모델 → 변수 수 · 제약 종류별 수 · 목적식 항 수(순회 순서와 무관한 값만)."""
    proto = model_to_pb(model)
    kinds = Counter(c.WhichOneof("constraint") for c in proto.constraints)
    enforced = sum(1 for c in proto.constraints if len(c.enforcement_literal) > 0)
    return {
        "variables": len(proto.variables),
        "constraints": len(proto.constraints),
        "constraint_kinds": dict(sorted(kinds.items())),
        "enforced_constraints": enforced,
        "objective_terms": len(proto.objective.vars),
        "has_objective": proto.HasField("objective"),
    }


# ── 캡처 ────────────────────────────────────────────────────────────────


def _bound_arguments(original, args, kwargs) -> dict:
    """원본 시그니처로 인자를 묶어 이름 → 값(기본값 포함)으로 돌려준다."""
    bound = inspect.signature(original).bind(*args, **kwargs)
    bound.apply_defaults()
    return dict(bound.arguments)


@dataclass
class Capture:
    """L0·L0'·L1·L2(+S1-P 의 P · 사다리·성공 모드의 attempts·L3) 기록기 + 멈춤·안전장치.

    `install(mp, engine)` 로 건다.
    """

    queries: QueryLog = field(default_factory=QueryLog)
    stop_at: str = "L2"
    layers: dict = field(default_factory=lambda: {"L0": [], "L0p": [], "L1": [], "L2": {}})
    solve_calls: int = 0
    separate_sessions: int = 0
    l2_calls_outside_l1: int = 0
    #: EI-10c — precheck 의 엔진 하드락 조회 횟수. 안전장치 기록(`safety`)에는 넣지 않는다(골든 불변).
    precheck_hard_lock_calls: int = 0
    stopped_at: str | None = None
    #: EI-13a-2 — 프로덕션 첫 시도 직전 빌더 호출의 `(EngineInput, AttemptSource | None)` 기록. 골든 기록(`layers`·`safety`·`RunResult`)에는
    #: 넣지 않는다(골든 모양 불변) — `test_ei13a_first_attempt_source.py` 가 읽는다.
    built_engine_inputs: list = field(default_factory=list)
    _in_l1: bool = False
    _in_precheck_hl: bool = False
    _in_builder: bool = False

    def install(self, mp: pytest.MonkeyPatch, engine) -> None:
        """기록기·안전장치를 mp 로 건다(mp 가 끝나면 원상 복구). 쿼리 리스너는 `queries.detach()` 로 뗀다."""
        import services.cp_sat_basic as cp_sat_basic
        import services.ontology_graph.lagrangian as lagrangian
        import services.precheck as precheck
        import services.precheck.hard_lock_view as hard_lock_view
        import services.roster_create_service as rcs
        from ortools.sat.python import cp_model

        engine_cls = cp_sat_basic.CPSATBasicEngine
        stop_l2 = self.stop_at == "L2"
        mp.setattr(precheck, "run_runtime_precheck", self._spy_precheck(precheck.run_runtime_precheck))
        mp.setattr(rcs, "build_engine_input_at_first_attempt",
                   self._spy_builder(rcs.build_engine_input_at_first_attempt))
        mp.setattr(hard_lock_view, "engine_hard_lock",
                   self._spy_precheck_hard_lock(hard_lock_view.engine_hard_lock))
        mp.setattr(lagrangian, "detect_night_forced_oversupply",
                   self._spy_detector(lagrangian.detect_night_forced_oversupply))
        mp.setattr(rcs, "generate_roster_cp_sat", self._engine_entry(mp, rcs.generate_roster_cp_sat))
        mp.setattr(engine_cls, "create_config_from_db",
                   self._spy_l2(engine_cls.create_config_from_db, "settings_eff", stop=False))
        mp.setattr(engine_cls, "create_nurses_from_db",
                   self._spy_l2(engine_cls.create_nurses_from_db, "nurses", stop=stop_l2))
        if self.stop_at == "primary_build":
            mp.setattr(cp_sat_basic, "_build_full_model",
                       self._spy_primary_build(cp_sat_basic._build_full_model))
        mp.setattr(cp_model.CpSolver, "solve", self._blocked_solve)
        mp.setattr(sys.modules["db.client2"], "SessionLocal", self._blocked_session)
        self.queries.attach(engine)

    def _engine_entry(self, mp: pytest.MonkeyPatch, original):
        """L1 자리 — 기본은 기록 후 원본 호출, ladder·post 는 엔진 입구 대역(+post 는 L3 기록기)."""
        if self.stop_at not in RECORDER_MODES:
            return self._spy_engine(original)
        self.layers["attempts"] = []
        if self.stop_at == "post":
            install_l3_spies(mp, self.layers)
        return EngineRecorder(self, self.stop_at).spy(original)

    # ── 기록기 ──

    def _spy_precheck(self, original):
        """L0 — precheck 입력을 기록하고 원본을 부른다."""
        def spy(*args, **kwargs):
            self.queries.mark("L0")
            self.layers["L0"].append(canonical(_bound_arguments(original, args, kwargs)))
            return original(*args, **kwargs)
        return spy

    def _spy_precheck_hard_lock(self, original):
        """precheck 의 엔진 하드락 조회(EI-10c) — 세고, 그 안의 create_config_from_db 는 L1 밖 L2 로 세지 않는다."""
        def spy(*args, **kwargs):
            self.precheck_hard_lock_calls += 1
            self._in_precheck_hl = True
            try:
                return original(*args, **kwargs)
            finally:
                self._in_precheck_hl = False
        return spy

    def _spy_builder(self, original):
        """EI-13a-2 — 첫 시도 직전 빌더 호출을 기록한다. 빌더 안 `_build_settings_eff` 의 `create_config_from_db`(기록만·DB 0)는
        L1 밖 L2 로 세지 않는다(`precheck` 하드락 조회와 같은 처리 — 세면 골든 safety 가 0→1 이 된다)."""
        def spy(*args, **kwargs):
            self._in_builder = True
            try:
                result = original(*args, **kwargs)
            finally:
                self._in_builder = False
            self.built_engine_inputs.append(result)
            return result
        return spy

    def _spy_detector(self, original):
        """L0' — 탐지기 인자(간호사·config 의 탐지기 키·일수·고정 칸·매핑 둘)를 기록하고 원본을 부른다."""
        def spy(*args, **kwargs):
            self.queries.mark("L0p")
            bound = _bound_arguments(original, args, kwargs)
            config = bound.pop("config") or {}
            bound["config"] = {k: config[k] for k in DETECTOR_CONFIG_KEYS if k in config}
            self.layers["L0p"].append(canonical(bound))
            return original(*args, **kwargs)
        return spy

    def _spy_engine(self, original):
        """L1 — 엔진 인자를 기록하고 원본을 부른다(L2 기록기가 solve 전에 멈춘다)."""
        def spy(*args, **kwargs):
            self.queries.mark("L1")
            self.layers["L1"].append(canonical(_bound_arguments(original, args, kwargs)))
            self._in_l1 = True
            try:
                return original(*args, **kwargs)
            finally:
                self._in_l1 = False
        return spy

    def _spy_l2(self, original, key: str, *, stop: bool):
        """L2 — L1 안에서 불린 조립 함수의 결과를 기록한다. stop 이면 기록 뒤 GoldenStop."""
        def spy(engine_self, *args, **kwargs):
            result = original(engine_self, *args, **kwargs)
            if not self._in_l1:
                if not (self._in_precheck_hl or self._in_builder):
                    self.l2_calls_outside_l1 += 1
                return result
            self.layers["L2"][key] = canonical(result)
            if stop:
                self.stopped_at = "L2"
                raise GoldenStop("L2 기록 완료 — solve 전 멈춤")
            return result
        return spy

    def _spy_primary_build(self, original):
        """P — primary `_build_full_model` 원본을 부르고 빌드 요약을 남긴 뒤 GoldenStop(solve 전)."""
        def spy(*args, **kwargs):
            built = original(*args, **kwargs)
            self.layers["P"] = primary_build_summary(built[0])
            self.stopped_at = "P"
            raise GoldenStop("primary 빌드 요약 완료 — solve 전 멈춤")
        return spy

    # ── 안전장치 ──

    def _blocked_solve(self, *_args, **_kwargs):
        """CpSolver.solve 대역 — 세고 곧바로 SolveBlocked."""
        self.solve_calls += 1
        raise SolveBlocked("하네스는 CpSolver.solve 를 돌리지 않는다")

    def _blocked_session(self, *_args, **_kwargs):
        """SessionLocal 대역 — 세고 곧바로 SeparateSessionBlocked."""
        self.separate_sessions += 1
        raise SeparateSessionBlocked("생성 경로가 별도 세션을 열려 했다(하네스 트랜잭션 보호)")
