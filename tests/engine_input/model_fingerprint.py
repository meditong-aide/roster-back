"""모델 지문 하네스 — 설계서 §7.1 보강(EI-1c, 10-09 리뷰 실험 반영).

`golden.py`(기존 L0~L3 골든)는 **손대지 않는다**. 이 모듈은 기존 시나리오(`scenario_defs.py`)
를 그대로 재사용하되, 멈추는 자리를 L2 가 아니라 **첫 CpSolver.Solve 직전**으로 밀어 모델
자체의 지문을 낸다. 기존 `Capture`(capture.py)도 고치지 않는다 — `stop_at` 을 `"L2"`·
`"primary_build"`·사다리 모드(`ladder.py` `RECORDER_MODES`) 어느 것도 아닌 값으로 두면
`Capture` 는 L0·L0'·L1·L2 를 그대로 기록만 하고 지나가므로, 그 뒤를 이 모듈이 직접 가로챈다.

멈추는 자리(variant)
  "lex"      — `fallback_lex._solve_traced` 의 **첫 호출**. phase 가 `"stage1["` 로 시작해야
               정상(기본 경로의 실제 첫 Solve — fallback_lex.py 전수 확인: 이 지문 대상인
               **첫** 호출에 한해서는 모든 `solver.Solve`/`SolveWithSolutionCallback` 호출이
               `_solve_traced` 를 거친다). 단 `_solve_traced` 자체를 거치지 않는 직접 `Solve`
               호출이 하나 있다(:5447, stage3 진단 `DiagS3` — stage1 **뒤**라 첫 호출 판정에는
               영향이 없고, capture.py 의 `CpSolver.solve` 차단이 안전망으로 작동한다).
               아니면 '예상 밖 probe' 로 보고 지문 없이 멈춘다(그래도 solve 는 안 부른다).
               ★ 위치 재확인(설계서 §7.1 '대략 :324 — 실제 위치 재확인'의 답): solve 도우미
               자체(`_solve_with_stall_stop`)는 fallback_lex.py:301, 그걸 감싼 `_solve_traced`
               는 :398 이지만, **실제 첫 호출 자리**(`optimize_fallback_lex_hard_first` 안의
               stage1 본 solve)는 fallback_lex.py:4031 이다. 거기까지 가는 동안(이 모듈이
               대상으로 삼은 lex 8시나리오 전수 실측) 다른 Solve 는 불리지 않는다
               (`probe_calls` 가 늘 빈다 — `test_stage_matches_variant`).
  "primary"  — `cp_sat_basic._build_full_model` 의 **첫 호출**(S1-P, SKIP_PRIMARY=0). 원본을
               그대로 불러 모델을 받은 뒤 지문을 내고 멈춘다 — `_quick_initial_solve` 의 실제
               `solver.Solve`(:2293 부근)는 닿지 않는다.
  두 경로 모두 원본 `solver.Solve`/`SolveWithSolutionCallback` 호출 전에 GoldenStop 을 던진다
  (solve 0 원칙 — capture.py 의 `CpSolver.solve` 차단은 안전장치로 그대로 작동한다).

이름표 호출 수(설계서 §7.1 '이름표별 제약 수')
  대상 7모듈(치환 범위 — 설계서 §2.3)의 `hard_add`/`aux_add`/`hard_call`/`aux_call` 를 기록
  래퍼로 바꾼다. 원본을 그대로 부르므로 모델은 한 비트도 바뀌지 않는다. 모듈마다 `from ... import`
  로 이름을 들여왔기 때문에(바인딩이 독립적이다), `rule_tag` 모듈이 아니라 **들여온 그 모듈**의
  이름을 바꾼다.

★ 사각지대(2026-10-13 재검토·golden_model/*.json rule_counts 실측, 아직 안 고침 — 명시만 해
  둔다) — 대상 9개(lex 8 + primary 1)는 stage1[hard] 의 **첫** 모델만 본다. 하드락 8종 중
  `enforce_4o`(#없음·연속 4O 금지) 는 이 9개 조합 전부에서 한 번도 안 불린다(실측 — rule_counts
  에 0건). `enforce_4o_hard` 기본값이 False 이고 DB 컬럼도 아니라서, 켜려면 `config_dict`(ORM
  `.__dict__`) 를 만드는 roster_create_service.py 지점 자체를 패치해야 하는데, 이 모듈의
  가로채기 지점(`_build_full_model`·`_solve_traced` 첫 호출)은 그보다 **뒤**라 거기서 가로채도
  이미 늦다 — 새 가로채기 지점이 더 필요하다(아직 안 했다). 다른 하드락(#5 `recovery_2n2off`·
  #7 `not_one_night` 은 S7 · #8 MID `mid_transition` 은 S4)은 이미 대상 9개 안에서 실측으로
  켜진다 — 사각지대는 `enforce_4o` 하나다.
  그 밖에 RuleSpec 80여 개 중 HARD 계열 21개(`grade_max`·`grade_min`·`handoff_adj`·
  `handoff_same`·`lex_freeze_*`(8개)·`n2n_min_gap`·`off_first_coverage_cap`·`preceptee_follow`·
  `zero_demand_block`·`monthly_limit_*` 등)·AUX 19개도 이 9개 조합에서는 안 불린다(설정·데이터
  조합이 안 맞아서다 — 조건부로 생기는 지점이라 "틀렸다"는 뜻이 아니다). 이 docstring 이 남기는
  사실은 "stage1 의 첫 모델만 보는 구조적 한계가 있고, 그래서 커버리지가 전부가 아니다"는
  것이다 — rules.py 와 이 9개의 rule_counts 를 대조하면 언제든 같은 목록을 다시 낼 수 있다.

근무 색인 재표기(2026-10-14 보강 — EI-1c 마무리, `model_canon.model_fingerprint` 의
  `shift_types` 인자)
  `cp_sat_basic.py` 의 `_normalize_requirements` 가 근무 요구치 키를 **set** 으로 모아
  `NurseRosterConfig.shift_types` 순서가 `PYTHONHASHSEED` 에 따라 프로세스마다 바뀐다(실측 —
  EI-16 결함 큐 '근무 순서 결정론', `shift_order/REPORT.md`). 앱 로직 수정은 이 작업 범위
  (tests/) 밖이라 EI-16 이 나중에 고친다 — 그동안 지문이 흔들리지 않게, `_install_shift_types_
  capture` 가 그 실행이 실제로 쓴 `shift_types` 를 `CPSATBasicEngine.create_config_from_db`
  (호출처 유일)에서 그대로 받아 `holder.shift_types` 에 담고, `model_fingerprint(model,
  holder.shift_types)` 로 넘긴다. `model_canon` 이 변수 이름 속 근무 색인을 근무 코드
  알파벳순으로 다시 매겨 정규형을 다시 정렬하므로, 대상 9개(lex 8 + primary 1) 전부에서
  **완전 지문(digest)까지** 시드 무관해진다(`test_model_fingerprint.py` 의 '실측 결정론 한계'
  문단 참고).
"""

from __future__ import annotations

import dataclasses
import importlib
import json
import traceback
from collections import Counter
from pathlib import Path

from .canonical import canonical
from .capture import GoldenStop
from .engine_env import env_effective, isolate_engine_env
from .golden_gate import GOLDEN_MODEL, require_golden
from .harness import Scenario, run_production
from .model_canon import fingerprint_digest, fingerprint_summary, model_fingerprint
from .scenario_defs import SCENARIOS

#: 지문 전용 골든 디렉터리 — 기존 `golden/` 과 다른 자리다(설계서 §7.1 보강 '기존 골든 불변').
#: 정본은 `golden_gate.GOLDEN_MODEL` 이다 — 이 이름은 import·grep 이 그대로 통하게 남긴 별칭이다(D29).
GOLDEN_MODEL_DIR = GOLDEN_MODEL.path

#: EI-1c 대상 lex 시나리오(설계서 §7.1 보강) — 엔진 입구에 실측으로 닿는 기본 시나리오만.
#: S3(L2 TypeError 로 못 지남)·S9·S9-F(나이트 차단 L0' 에서 끝남 — L1 에 닿지 않음)·사다리·성공
#: 모드(S1-L 류, 대역 엔진)는 뺀다.
LEX_SCENARIO_NAMES = ("S1", "S1-D", "S2", "S4", "S5", "S6", "S7", "S8")
#: variant → 시나리오 이름(primary 는 S1-P 하나).
FINGERPRINT_TARGETS: tuple[tuple[str, str], ...] = (
    tuple(("lex", name) for name in LEX_SCENARIO_NAMES) + (("primary", "S1-P"),)
)

#: 이름표 래퍼를 쓰는 7개 모듈(설계서 §2.3 EI-3 치환 대상 — fallback_lex 145·cp_sat_basic 85·
#: lookahead_constraints 2·constraints/ 4파일 18).
RULE_TAG_MODULES = (
    "services.cp_sat.fallback_lex",
    "services.cp_sat_basic",
    "services.cp_sat.lookahead_constraints",
    "services.constraints.grade_constraints",
    "services.constraints.monthly_limit_constraints",
    "services.constraints.team_constraints",
    "services.constraints.team_grade_handoff_constraints",
)
_ADD_WRAPPERS = ("hard_add", "aux_add")
_CALL_WRAPPERS = ("hard_call", "aux_call")


class _Holder:
    """실행 한 번의 결과를 담는 가변 상자 — patch 클로저가 채우고, 끝에 기록으로 옮긴다."""

    def __init__(self) -> None:
        self.fingerprint: dict | None = None
        self.summary: dict | None = None
        self.phase: str | None = None
        self.stage: str | None = None
        #: 지문 대상보다 먼저 불린 Solve(예상 밖 probe/진단) — 비면 정상.
        self.probe_calls: list[dict] = []
        self.rule_counts: Counter = Counter()
        #: 지문을 뜬 그 순간의 엔진 env(격리 뒤 — 2026-10-13 보강). 개발자 셸의 실험
        #: 플래그가 골든에 섞이는지 기록에 남겨 드러나게 한다.
        self.env: dict[str, str] | None = None
        #: 이 실행이 실제로 쓴 `NurseRosterConfig.shift_types`(2026-10-14 보강 — EI-1c 마무리).
        #: `create_config_from_db` 한 곳(호출처 유일 — `shift_order/REPORT.md` 확인)에서 받아만
        #: 둔다. `model_canon.model_fingerprint` 에 그대로 넘겨 근무 색인을 코드 기준으로
        #: 재표기해, EI-16 '근무 순서 결정론' 결함이 지문을 흔들지 못하게 한다.
        self.shift_types: list[str] | None = None


# ── 이름표 호출 카운터 ───────────────────────────────────────────────────


def _install_rule_counters(mp, counts: Counter) -> None:
    """7모듈 네임스페이스의 hard_add/aux_add/hard_call/aux_call 를 기록 래퍼로 바꾼다.

    `from services.cp_sat.rule_tag import hard_add, ...` 로 들여온 이름은 그 모듈의 독립된
    바인딩이라, `rule_tag.hard_add` 가 아니라 **들여온 모듈 자신**의 속성을 바꿔야 효과가 있다.
    """
    for mod_name in RULE_TAG_MODULES:
        module = importlib.import_module(mod_name)
        for fname in _ADD_WRAPPERS:
            if not hasattr(module, fname):
                continue
            original = getattr(module, fname)

            def wrapped_add(m, rule_id, expr, _orig=original, _counts=counts):
                _counts[rule_id] += 1
                return _orig(m, rule_id, expr)

            mp.setattr(module, fname, wrapped_add)
        for fname in _CALL_WRAPPERS:
            if not hasattr(module, fname):
                continue
            original = getattr(module, fname)

            def wrapped_call(m, rule_id, method, *args, _orig=original, _counts=counts, **kwargs):
                _counts[rule_id] += 1
                return _orig(m, rule_id, method, *args, **kwargs)

            mp.setattr(module, fname, wrapped_call)


# ── 근무 색인 실측(EI-1c 마무리, 2026-10-14 보강) ───────────────────────────


def _install_shift_types_capture(mp, holder: _Holder) -> None:
    """`CPSATBasicEngine.create_config_from_db`(호출처 유일 — `shift_order/REPORT.md`
    부록 '사실 확인' 참고)가 만든 `NurseRosterConfig.shift_types` 를 그대로 받아만 둔다.
    원본을 그대로 부르고 반환값만 훔쳐보므로 앱 동작은 한 비트도 바뀌지 않는다 —
    `shift_order/exp/fp_order_entry.py`(조사 2) 와 같은 기법."""
    import services.cp_sat_basic as cp_sat_basic

    original = cp_sat_basic.CPSATBasicEngine.create_config_from_db

    def wrapped(self, config_data):
        cfg = original(self, config_data)
        holder.shift_types = list(cfg.shift_types)
        return cfg

    mp.setattr(cp_sat_basic.CPSATBasicEngine, "create_config_from_db", wrapped)


# ── 가로채기(variant 별) ─────────────────────────────────────────────────


def _stack_summary(depth: int = 8) -> list[str]:
    """`func@file:line` 꼴로 호출 스택 일부를 적는다(예상 밖 probe 보고용 — §7.1 '어느 함수')."""
    frames = traceback.extract_stack()[:-1]
    return [f"{f.name}@{f.filename.rsplit('/', 1)[-1]}:{f.lineno}" for f in frames[-depth:]]


def _lex_intercept(mp, holder: _Holder) -> None:
    """`fallback_lex._solve_traced` 첫 호출을 가로챈다 — 원본은 부르지 않는다(solve 0)."""
    import services.cp_sat.fallback_lex as fallback_lex

    called: list[str] = []

    def spy(solver, model, logger_prefix, phase):
        if called:
            raise GoldenStop("모델 지문 기록 뒤 — 추가 _solve_traced 호출 차단(solve 0)")
        called.append(phase)
        if not phase.startswith("stage1["):
            holder.stage = "unexpected_probe"
            holder.probe_calls.append({"phase": phase, "stack": _stack_summary()})
            raise GoldenStop(f"지문 대상(stage1) 전에 다른 Solve(phase={phase}) — 지문 없이 멈춤")
        holder.phase = phase
        holder.stage = "stage1"
        holder.fingerprint = model_fingerprint(model, holder.shift_types)
        holder.summary = fingerprint_summary(model)
        holder.env = env_effective()
        raise GoldenStop("lex stage1 모델 지문 기록 완료 — solve 전 멈춤")

    mp.setattr(fallback_lex, "_solve_traced", spy)


def _primary_intercept(mp, holder: _Holder) -> None:
    """`cp_sat_basic._build_full_model` 첫 호출을 가로챈다 — 원본을 불러 모델을 받은 뒤 멈춘다."""
    import services.cp_sat_basic as cp_sat_basic

    original = cp_sat_basic._build_full_model
    called: list[bool] = []

    def spy(*args, **kwargs):
        if called:
            raise GoldenStop("모델 지문 기록 뒤 — 추가 _build_full_model 호출 차단(solve 0)")
        called.append(True)
        built = original(*args, **kwargs)
        holder.phase = "primary_build"
        holder.stage = "primary_build"
        holder.fingerprint = model_fingerprint(built[0], holder.shift_types)
        holder.summary = fingerprint_summary(built[0])
        holder.env = env_effective()
        raise GoldenStop("primary 빌드 모델 지문 기록 완료 — solve 전 멈춤")

    mp.setattr(cp_sat_basic, "_build_full_model", spy)


def _isolate_env_patch(mp) -> None:
    """지문을 뜨기 전에 개발자 셸·`.env` 의 엔진 실험 flag 를 지운다(2026-10-13 보강).

    `base.patches` **앞**에 와야 한다 — 뒤에 두면 S1-P 의 `skip_primary_off`(SKIP_PRIMARY=0)
    가 base.patches 안에서 setenv 한 값을 이 격리가 지워버려(SKIP_PRIMARY 는
    `engine_env_names()` 에 잡히는 이름이다) primary 경로가 아니라 lex 경로로 새 버린다.
    이름표 호출 카운터·variant 가로채기(`_fingerprint_patch`)는 env 가 아니라 모듈 속성을
    바꾸는 것이라 순서 영향이 없다."""
    isolate_engine_env(mp)


def _fingerprint_patch(variant: str, holder: _Holder):
    """시나리오 `patches` 에 더할 함수 — 이름표 호출 카운터 + variant 별 가로채기."""
    def patch(mp) -> None:
        _install_rule_counters(mp, holder.rule_counts)
        _install_shift_types_capture(mp, holder)
        if variant == "primary":
            _primary_intercept(mp, holder)
        else:
            _lex_intercept(mp, holder)
    return patch


def _fingerprint_scenario(name: str, variant: str, holder: _Holder) -> Scenario:
    """`SCENARIOS[name]` 을 복제해 env 격리 + 지문 가로채기 patch 를 더한다.

    patch 순서는 `(env 격리, *base.patches, 지문 가로채기)` 다 — 격리가 **가장 먼저** 와야
    base.patches 가 그 뒤에 설정하는 값(S1-P 의 SKIP_PRIMARY=0 등)을 지우지 않는다(위
    `_isolate_env_patch` 참고).

    `stop_at` 은 `"model_fingerprint"`(임의의 새 값)로 둔다 — `Capture` 는 `"L2"`·
    `"primary_build"`·사다리 모드 외의 값을 전부 '안 멈추고 지나감'으로 다루므로(capture.py
    `install()`), 기존 코드를 한 글자도 고치지 않고 L2 를 지나 여기까지 올 수 있다. S1-P 의
    기존 `stop_at="primary_build"` 도 여기서 덮어써 `Capture` 가 `_build_full_model` 을 건드리지
    않게 한다(그래야 우리 가로채기와 안 겹친다) — 기존 `golden/S1-P.json` 골든은 그대로
    `scenario_defs.SCENARIOS["S1-P"]` 원본으로 돈다(이 모듈이 복제본만 쓴다).
    """
    base = SCENARIOS[name]
    return dataclasses.replace(
        base,
        patches=(_isolate_env_patch, *base.patches, _fingerprint_patch(variant, holder)),
        stop_at="model_fingerprint",
    )


def run_fingerprint(engine, name: str, variant: str = "lex"):
    """시나리오 한 번 실행 + 지문 기록. `(RunResult, _Holder)` 를 돌려준다."""
    holder = _Holder()
    scenario = _fingerprint_scenario(name, variant, holder)
    result = run_production(engine, scenario)
    return result, holder


def build_fingerprint_record(name: str, variant: str, result, holder: _Holder) -> dict:
    """골든 기록 — digest·요약·이름표 호출 수·안전장치 횟수(설계서 §7.1)."""
    record = {
        "scenario": name,
        "variant": variant,
        "outcome": result.outcome,
        "stage": holder.stage,
        "phase": holder.phase,
        "probe_calls": holder.probe_calls,
        "fingerprint": holder.fingerprint,
        "digest": fingerprint_digest(holder.fingerprint) if holder.fingerprint else None,
        "summary": holder.summary,
        "rule_counts": dict(sorted(holder.rule_counts.items())),
        "safety": {"solve_calls": result.solve_calls, "separate_sessions": result.separate_sessions},
        "env": holder.env or {},
    }
    return canonical(record)


# ── 지문 골든(golden_model/) ────────────────────────────────────────────


def golden_model_path(name: str) -> Path:
    """시나리오 이름 → 지문 골든 파일 경로(`golden/` 이 아니라 `golden_model/`)."""
    return GOLDEN_MODEL_DIR / f"{name}.json"


def write_golden_model(name: str, record: dict) -> Path:
    """지문 골든을 쓴다(`UPDATE_GOLDEN_MODEL=1` 일 때만 부른다 — golden.py `write_golden` 과 같은 규칙이지만 플래그 이름은 다르다(기존 `golden/` 과 같은 플래그를 쓰면 그쪽도 같이 덮어써진다, 2026-10-13 보강))."""
    GOLDEN_MODEL_DIR.mkdir(parents=True, exist_ok=True)
    path = golden_model_path(name)
    text = json.dumps(record, ensure_ascii=False, sort_keys=True, indent=1)
    path.write_text(text + "\n", encoding="utf-8")
    return path


def read_golden_model(name: str) -> dict | None:
    """지문 골든을 읽는다. 없으면 None.

    첫 줄 문지기(D29): `golden_model/` 에 JSON 이 하나도 없으면(README.md 만 있는 체크아웃 포함) 건너뜀
    (`REQUIRE_GOLDEN=1` 이면 실패), 수가 기준(9)과 다르면 실패다. `GOLDEN_MODEL` 은 부를 때마다 모듈 전역에서 읽는다.
    """
    require_golden(GOLDEN_MODEL)
    path = golden_model_path(name)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))
