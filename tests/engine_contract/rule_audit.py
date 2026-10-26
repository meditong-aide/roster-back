"""A1 이름표 감사 — 정적 스캐너(설계서 §2.6 A1 · §5.3 EI-3 '구현 결과' 보류 몫).

엔진이 거는 CP-SAT 제약은 전부 `services.cp_sat.rule_tag` 의 이름표 래퍼
(hard_add·aux_add·hard_call·aux_call)를 거쳐야 한다. 이 모듈은 그 규칙을 어긴
자리를 정적으로 찾는다. 순수 함수(`scan`)만 공개한다 — 파일을 읽고 AST 를
파싱할 뿐, DB·ortools 모델을 만들거나 실행하지 않는다.

제약 메서드 집합은 하드코딩하지 않고 매번 `dir(cp_model.CpModel)` 에서 모은다
(이름이 add/Add 로 시작하는 것). 그중 힌트(AddHint)·결정전략(AddDecisionStrategy)·
가정(AddAssumption(s))은 "제약"이 아니라 따로 다룬다(§5.3 EI-3 지시 1).

★2026-10-14 사고·수정: ortools 9.13+(설치판 9.15.6755)부터 `CpModel` **클래스**에는
snake_case 이름(add_exactly_one 등)만 남고, 대문자(pre-pep8) 이름(Add·AddExactlyOne…)
은 `CpModel.__init__` 이 `_add_pre_pep8_methods()`(cp_model.py:1684-1716)로 **인스턴스**에만
단다. `dir(cp_model.CpModel)` 만 보면 9.15 에서 대문자 이름이 통째로 빠져, 이름표 없는
`m.Add(...)`·`m.AddExactlyOne(...)` 를 못 잡는 사실상 꺼진 감사가 된다(§모호성 판정
아래 갱신 참고). 그래서 지금은 ① `dir(CpModel)` ② `dir(CpModel())`(인스턴스 속성 —
그 판이 별칭을 여기 달면 잡힌다) ③ ①의 snake_case 를 그 판의 변환 규칙으로 직접
CamelCase 로 바꾼 이름(별칭 메커니즘 자체가 사라질 미래 판 대비) ④ 9.12/9.15 양쪽에서
실측한 대문자 이름 고정 목록(안전망) 네 가지를 합집합해 판과 무관하게 모은다
(`_collect_add_names()`). 가져오는 즉시 최소 기대 이름이 들어왔는지 assert 한다 —
넷 다 동시에 깨지면(이름 자체가 바뀌면) 조용히 넘어가지 않고 즉시 터진다.

검사 항목(설계서 §5.3 EI-3 '보류 중인 테스트 몫' + 이번 지시 1~9 + 2026-10-13 변이
보강 지시 1~9, x01~x20):
  1) 제약 메서드 집합을 `dir(CpModel)`(+인스턴스+변환 규칙+안전망, 위 2026-10-14 수정
     참고)에서 동적으로 모으고, 힌트·결정전략·가정은 뺀다.
  2) 모호한 이름("add" — set.add 와 충돌, 실측 근거는 §모호성 판정)은 "모델 수신자"일
     때만 잡는다. "모델 수신자"는 닫힌 집합(모델 별칭·CpModel() 생성·주석)과, 대상/면제
     모듈·cp_model import 모듈에서 "컨테이너로 증명되지 않은 모든 수신자"(§기본값-의심
     스코프) 두 갈래로 판정한다 — 그래야 `_mm = m` 같은 모델 별칭(x01)·타입주석 없는
     매개변수(x02)·`m.Proto().constraints.add()` 같은 proto 직접조작(x08)·
     `cp_model.CpModel.add(m, ...)` 같은 미바인드 클래스 호출(x16)이 전부 걸린다.
  3) 대상 모듈(7개) 0 위반.
  4) 면제 모듈은 실측 호출 "수" + 정규화 호출 지문(ast.unparse 다중집합) 해시 고정(수가
     같아도 내용이 바뀌면 실패 — x13). 그 밖의 app/ 모듈은 호출 0.
  5) 우회 형태(속성/Name 값으로 꺼내기·getattr 리터럴/비리터럴·__getattribute__·래퍼
     재정의/재대입/alias import(절대·상대 경로)·모듈 별칭 속성 호출·functools.partial)
     차단(x01~x08·x13·x15·x20).
  6) 래퍼 id 인자 등록 여부(리터럴·f-string 접두·허용된 동적 변수 — 그 변수의 **모든**
     바인딩 형태: 단순대입·AnnAssign·튜플풀기·walrus·함수매개변수, x09~x12).
  7) hard_*/aux_* 와 RuleSpec.strength 일치(별칭·모듈 속성 호출로 불러도 같다).
  8) add_hard(...) 의 meta["pattern"] 도 6) 과 같은 규칙 — dict 리터럴 하나가 아니라
     그 변수에 대한 모든 "pattern" 쓰기 이벤트(리터럴·구독 대입·update()) 를 호출 줄
     이전까지 모아 가장 나중 값을 검증한다(dict 를 만든 뒤 덮어쓰는 형태, x14).
  9) RULES·RULE_PREFIXES 의 모든 id/접두가 실제로 쓰인다(죽은 id 없음).
  10) 문법 오류로 파싱 못 하는 모듈은 조용히 건너뛰지 않고 그 자체를 위반으로
      보고한다(x18 — 감사가 못 본 파일이 있다는 사실이 사라지면 안 된다).
  11) `scan(app_root)` 는 RULES 레지스트리를 **그 app_root 자신의** rules.py 에서
      경로로 로드한다 — 모듈 top-level 의 `from services.engine_contract.rules import
      ...` (sys.path 전역) 에 기대면 `scan()` 의 `app_root` 인자가 장식일 뿐이라
      다른 app_root 를 줘도 항상 "지금 프로세스에 깔린" 레지스트리로 판정해 밀폐되지
      않는다(2026-10-13 지시 "17"). 합성 테스트 트리처럼 그 파일이 없으면 전역 import
      로 돌아간다(테스트 편의 — 그 자체를 보는 테스트는 파일을 직접 둔다).

분류 근거(실측)는 이 모듈 맨 아래 주석에 남긴다.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ortools.sat.python import cp_model

# 전역(sys.path) 레지스트리 — 합성 테스트 트리(app_root 에 rules.py 가 없는 tmp_path)의
# 대비책으로만 쓴다. 실제 스캔은 `_load_registry(app_root)` 가 app_root 자신의 rules.py
# 를 경로로 로드한 모듈을 쓴다(§지시 11 — "17"). 아래 세 이름(RULES·RULE_PREFIXES·lookup)
# 은 기존 테스트가 `ra.RULES` 식으로 직접 참조하므로 하위호환으로 남긴다.
import services.engine_contract.rules as _global_rules_module
from services.engine_contract.rules import RULE_PREFIXES, RULES, lookup  # noqa: F401 — 하위호환 재노출(ra.RULES 등)

# ── 1) 제약/힌트/결정전략/가정 메서드 집합 — ortools 판과 무관하게 동적으로 ─────


def _camel_case_pre_pep8(name: str) -> str:
    """ortools 의 pre-pep8 변환 규칙(snake_case → CamelCase, cp_model.py:227-234 실측,
    add_exactly_one → AddExactlyOne · add → Add · add_no_overlap_2d → AddNoOverlap2D ·
    add_bool_xor → AddBoolXOr). 설치된 ortools 에 그 함수가 있으면 그대로 쓰고(판이
    바뀌어도 늘 "그 판의 실제 규칙"과 같다), 없으면(과거/미래 판) 자체 구현으로
    대체한다."""
    converter = getattr(cp_model, "snake_case_to_camel_case", None)
    if converter is not None:
        return converter(name)
    words = name.split("_")
    return "".join(w.capitalize() for w in words).replace("2d", "2D").replace("Xor", "XOr")


#: pre-pep8(대문자) 별칭을 받는 접두 · 접두 없이 통째로 별칭이 붙는 이름 — ortools
#: `CpModel._add_pre_pep8_methods()`(cp_model.py:1684-1716, 9.15 실측)가 쓰는 것과 같은
#: 집합이다. ③ 번 출처(아래 `_collect_add_names`)가 이 규칙으로 직접 CamelCase 이름을
#: 만들 때 쓴다.
_PRE_PEP8_PREFIXES: tuple[str, ...] = ("add_", "new_", "clear_")
_PRE_PEP8_BARE_NAMES: frozenset[str] = frozenset(
    {
        "add", "clone", "get_bool_var_from_proto_index", "get_int_var_from_proto_index",
        "get_interval_var_from_proto_index", "minimize", "maximize", "has_objective",
        "model_stats", "validate", "export_to_file",
    }
)

#: 안전망(④ 번 출처) — 9.12.4544 와 9.15.6755 양쪽에서 실측한 대문자(pre-pep8) 이름
#: 전체(2026-10-14, 9.15 `dir(CpModel())` 전수). 9.12 는 이 이름들이 **클래스**에 직접
#: 있었고, 9.15 는 **인스턴스**에만 있다 — 그래서 메커니즘이 또 바뀌어도(①~③ 이 전부
#: 비게 돼도) 이 목록은 항상 CONSTRAINT_NAMES 후보에 합집합한다("9.12 의 대문자 목록이
#: 있으면 그것까지 합집합" 지시). `AddMapDomain` 은 9.12 클래스에는 없었고 9.15 에서 새로
#: 생긴 이름이지만, app/ 전체에 사용처가 0 곳이라 EXEMPT 지문·대상 모듈 판정에 영향이
#: 없다(실측).
_KNOWN_PRE_PEP8_NAMES: frozenset[str] = frozenset(
    {
        "Add", "AddAbsEquality", "AddAllDifferent", "AddAllowedAssignments",
        "AddAssumption", "AddAssumptions", "AddAtLeastOne", "AddAtMostOne",
        "AddAutomaton", "AddBoolAnd", "AddBoolOr", "AddBoolXOr", "AddCircuit",
        "AddCumulative", "AddDecisionStrategy", "AddDivisionEquality", "AddElement",
        "AddExactlyOne", "AddForbiddenAssignments", "AddHint", "AddImplication",
        "AddInverse", "AddLinearConstraint", "AddLinearExpressionInDomain",
        "AddMapDomain", "AddMaxEquality", "AddMinEquality", "AddModuloEquality",
        "AddMultipleCircuit", "AddMultiplicationEquality", "AddNoOverlap",
        "AddNoOverlap2D", "AddReservoirConstraint", "AddReservoirConstraintWithActive",
    }
)

#: import 시점 가드 — 이 이름들이 안 잡히면 ①~④ 수집 로직 자체가 그 판에서
#: 깨졌다는 뜻이다(2026-10-14 사고 재발 방지, 조용히 넘어가지 않고 즉시 터뜨린다).
_MUST_HAVE_ADD_NAMES: frozenset[str] = frozenset({"add", "Add", "AddExactlyOne", "add_exactly_one"})


def _collect_add_names() -> frozenset[str]:
    """제약/힌트/결정전략/가정 후보 이름 전체를 ortools 판과 무관하게 모은다
    (2026-10-14 재작성 — 이전에는 `dir(cp_model.CpModel)` 하나만 봐서 9.15 에서
    대문자 이름이 전부 빠졌다). 네 출처의 합집합:
      ① `dir(CpModel)` — 그 판이 클래스에 직접 정의한 이름.
      ② `dir(CpModel())` — 인스턴스 속성. 9.13+ 는 `__init__` 이 여기서 대문자
         별칭을 단다 — 9.15 는 Add·AddExactlyOne 등 34개가 여기에만 있다.
      ③ ①의 snake_case 이름을 `_camel_case_pre_pep8` 로 직접 변환한 이름 — ②가
         사라질 미래 판(별칭 메커니즘 자체 폐지) 대비.
      ④ `_KNOWN_PRE_PEP8_NAMES`(9.12/9.15 실측 고정 목록, 안전망).
    """
    class_names = set(dir(cp_model.CpModel))
    try:
        instance_names = set(dir(cp_model.CpModel()))
    except Exception:
        instance_names = set()
    derived_camel = {
        _camel_case_pre_pep8(n)
        for n in class_names
        if n.startswith(_PRE_PEP8_PREFIXES) or n in _PRE_PEP8_BARE_NAMES
    }
    names = class_names | instance_names | derived_camel | _KNOWN_PRE_PEP8_NAMES
    add_like = frozenset(n for n in names if n.lower().startswith("add"))
    missing = _MUST_HAVE_ADD_NAMES - add_like
    if missing:
        raise RuntimeError(
            "rule_audit._collect_add_names() 가 알려진 제약 메서드 이름을 못 모았다: "
            f"{sorted(missing)} — 설치된 ortools 판이 바뀌어 dir(CpModel)/dir(CpModel())/"
            "변환 규칙이 전부 달라졌다는 뜻이다(2026-10-14 사고와 같은 종류). 이 파일의 "
            "수집 로직을 그 판에 맞게 다시 실측하고 고쳐라(§모호성 판정 실측 갱신 포함)."
        )
    return add_like


_ALL_ADD_NAMES: frozenset[str] = _collect_add_names()
#: 힌트 — 해를 좁히지 않는다. ortools 에 ClearHints 도 있으나 add* 가 아니라 후보에 없다.
HINT_NAMES: frozenset[str] = frozenset(n for n in _ALL_ADD_NAMES if "hint" in n.lower())
#: 결정 전략 — 탐색 순서 힌트일 뿐 제약이 아니다.
DECISION_STRATEGY_NAMES: frozenset[str] = frozenset(
    n for n in _ALL_ADD_NAMES if "decisionstrategy" in n.lower().replace("_", "")
)
#: 가정 — MUS/MCS 진단 인프라 전용. hard_assumption.py 밖에서 쓰면 그 자체가 위반(§가정 위치).
ASSUMPTION_NAMES: frozenset[str] = frozenset(n for n in _ALL_ADD_NAMES if "assumption" in n.lower())
#: 나머지 = 제약 메서드(해를 실제로 제한하거나 보조 변수를 정의한다).
CONSTRAINT_NAMES: frozenset[str] = _ALL_ADD_NAMES - HINT_NAMES - DECISION_STRATEGY_NAMES - ASSUMPTION_NAMES

# "add" 는 부트스트랩(모델 수신자 집합을 모으는 1차 수집)에서 뺀다 — set.add 와 이름이 같아
# 모델이 아닌 수신자가 압도적이다(이 파일 맨 아래 §모호성 판정 실측 참고). 그래서 "add" 를
# 넣으면 모델 수신자 집합 자체가 오염된다. 나머지 제약 이름은 실측상 전부 모델에서만 쓰였다.
_BOOTSTRAP_CONSTRAINT_NAMES: frozenset[str] = CONSTRAINT_NAMES - {"add"}

# ── rule_tag 래퍼·hard_assumption.add_hard ──────────────────────────────────

RULE_TAG_MODULE = "services.cp_sat.rule_tag"
HARD_ASSUMPTION_MODULE = "services.cp_sat.hard_assumption"
HARD_ASSUMPTION_MODULE_PATH = "services/cp_sat/hard_assumption.py"
RULE_TAG_MODULE_PATH = "services/cp_sat/rule_tag.py"
ADD_WRAPPERS = frozenset({"hard_add", "aux_add"})
CALL_WRAPPERS = frozenset({"hard_call", "aux_call"})
WRAPPER_NAMES = ADD_WRAPPERS | CALL_WRAPPERS
_HARD_WRAPPERS = frozenset({"hard_add", "hard_call"})
_AUX_WRAPPERS = frozenset({"aux_add", "aux_call"})

#: 모듈 단위로 "모델 수신자"로 보는 식의 바탕값(§모델 수신자 정의). CpModel() 생성·주석·
#: 래퍼 첫 인자로 더 늘어난다(`_collect_model_names`).
_BASE_MODEL_NAMES: frozenset[str] = frozenset({"m", "m2", "m3", "model", "self.model"})

#: 모델이 아니라고 "증명"할 수 있는 컨테이너 생성자 — set()/dict()/list()/… 로 만든
#: 수신자는 §기본값-의심 스코프(아래)에 들어와도 제외한다(오탐 방지, §모호성 판정 2).
_CONTAINER_CONSTRUCTORS: frozenset[str] = frozenset(
    {"set", "frozenset", "list", "dict", "tuple", "Counter", "OrderedDict", "defaultdict", "deque"}
)

#: 변수 id 허용 목록 — (app 기준 상대경로, 변수명). 그 변수에 대한 모든 대입이
#: `_fixed_pattern_from_source(...)` 호출이고, 그 함수의 return 이 "fixed_assignment:"
#: 로 시작하는 f-string 일 때만 유효하다(검증은 `_check_dynamic_id_vars`). "모든 대입"은
#: 단순 대입뿐 아니라 AnnAssign·walrus·튜플풀기·함수매개변수 바인딩도 포함한다
#: (§지시 x09~x12 — 전부 "바인딩 형태"로 취급해 순수성을 본다).
DYNAMIC_ID_VARS: frozenset[tuple[str, str]] = frozenset(
    {
        ("services/cp_sat/fallback_lex.py", "_fixed_pattern"),
        ("services/cp_sat_basic.py", "_fixed_pattern"),
    }
)

#: 대상 모듈(7개) — 이름표 없는 제약 메서드 호출 0(§5.3 EI-3 3b 범위, D14=② primary 포함).
TARGET_MODULES: frozenset[str] = frozenset(
    {
        "services/cp_sat/fallback_lex.py",
        "services/cp_sat_basic.py",
        "services/constraints/grade_constraints.py",
        "services/constraints/team_constraints.py",
        "services/constraints/team_grade_handoff_constraints.py",
        "services/constraints/monthly_limit_constraints.py",
        "services/cp_sat/lookahead_constraints.py",
    }
)

#: 면제 모듈 — 실측 호출 수 + 정규화 호출 지문(ast.unparse 다중집합) 해시(2026-10-13, 이
#: 저장소 기준). 수만 보면 호출 "내용"을 바꿔치기해도(총합 유지) 못 잡는다(x13) — 그래서
#: 다중집합을 정렬해 이은 문자열의 sha256 을 같이 고정한다. 둘 다 그대로면 통과, 수가
#: 다르면 "늘었다/줄었다", 수는 같은데 해시가 다르면 "내용이 바뀌었다"로 따로 보고한다.
#: (사유, 기대 호출 수, 기대 지문 해시).
EXEMPT: dict[str, tuple[str, int, str]] = {
    "services/cp_sat/objective_terms.py": (
        "목적식 보조(primary 분 build_main_objective_terms 포함). "
        ":460 의 floor>D 보강은 EI-16 결함 큐 후보로 별도 추적(동작은 그대로 둔다)",
        56,
        "d626f141bc407bb7e7f42d689629b868e720ec908c2882129be99cd267fc283e",
    ),
    "services/cp_sat/fallback_objectives.py": (
        "목적식 보조", 43,
        "1e800bbea643401155d33fc415aa13474d65f4117ff24b1f18753b868e2f3c75",
    ),
    "services/ontology_graph/core_guided.py": (
        "진단용 별도 모델", 8,
        "00e5117aa936cfa199392873ffda8356cac3067be9ba432a940d0b7b3f9de5c9",
    ),
    "services/ontology_graph/lagrangian.py": (
        "진단용 별도 모델", 8,
        "c2c54cf51db3e62b1b2877278fa97953c59ccd1af3bff7a22010f540085f529f",
    ),
    "services/ontology_graph/roster_ir.py": (
        "진단용 별도 모델", 7,
        "37f34ac51ef577699d7579a7c07f8ec2651cdb1972c074fab20e11cb2e2fb99b",
    ),
    "services/cp_sat/hard_assumption.py": (
        "add_hard 래퍼 자체 구현(내부 model.Add)", 2,
        "7ea7457f0ff55b2532aec41902feaf12387caf509c3b4b865362ed01942f8e3a",
    ),
    "services/cp_sat_simple_test.py": (
        "솔버 스모크 테스트", 4,
        "a303e042044a3ba27d8eccbad77b2f8bdcb98fa0fd3c8ee31ecce0e3e406d894",
    ),
    "services/cp_sat/rule_tag.py": (
        "hard_add/aux_add 래퍼 자체 구현", 2,
        "3ccf0325de9474a1012f02a6201bef5edee522562d594077d0b80f200d833b1d",
    ),
}

#: 가정(assumption) 관리의 정본 자리.
ASSUMPTION_HOME = "services/cp_sat/hard_assumption.py"

#: 가정 메서드를 정본 밖에서 쓰는 실측 기준선(2026-10-11). 늘거나(새 자리) 줄면 실패한다.
#: 둘 다 "진단용 별도 모델/알고리즘"(MCS 는 MUS 의 쌍대, core_guided 는 ontology_graph 자체
#: 모델)이라 §EXEMPT 의 같은 근거를 공유하지만, 설계서는 이 자리를 명시적으로 면제하지
#: 않았다 — 그래서 0 으로 두지 않고 "측정해서 고정"(§EXEMPT 와 같은 방식)으로만 다룬다.
#: 레지스트리로 옮길지 여부는 Fix 단계 결정 사항이다(open_ko 로 보고).
ASSUMPTION_BASELINE_ELSEWHERE: dict[str, int] = {
    "services/cp_sat/mcs.py": 1,
    "services/ontology_graph/core_guided.py": 1,
}


@dataclass(frozen=True)
class Violation:
    """한 건의 위반. `message` 는 'file:line — 무엇 — 어떻게 고치나' 형태(한국어)."""

    file: str
    line: int
    kind: str
    message: str


@dataclass
class _ModuleInfo:
    """파일 하나를 한 번만 파싱해 재사용하는 캐시."""

    relpath: str
    tree: ast.Module
    source: str
    model_names: set[str] = field(default_factory=set)
    container_names: set[str] = field(default_factory=set)
    imports_cp_model: bool = False
    #: 로컬 이름 → 정본 rule_tag 래퍼 이름(별칭 체인 해소, x04·x05).
    wrapper_aliases: dict[str, str] = field(default_factory=dict)
    #: 로컬 이름 → "add_hard"(별칭 체인 해소).
    add_hard_aliases: dict[str, str] = field(default_factory=dict)
    #: `import a.b.c as X` 의 로컬 이름 → 정본 dotted module path(x03·x15).
    module_aliases: dict[str, str] = field(default_factory=dict)


# ── 모델 수신자 수집(§2 "모호한 메서드는 모델 수신자일 때만") ─────────────────


def _unparse(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        return "<?>"


def _is_cpmodel_construction(node: ast.AST) -> bool:
    """`cp_model.CpModel(...)` 류 호출인가(마지막 속성/이름이 CpModel)."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr == "CpModel"
    if isinstance(func, ast.Name):
        return func.id == "CpModel"
    return False


def _collect_model_names(info: _ModuleInfo) -> set[str]:
    """이 모듈에서 CP-SAT 모델을 가리키는 것으로 보이는 식(소스 텍스트) 집합.

    모듈 단위로 모은다(함수 경계를 넘어도 된다). 근거 넷:
      1) `<target> = cp_model.CpModel()` 대입의 좌변.
      2) `CpModel` 로 타입주석된 매개변수 이름.
      3) rule_tag 래퍼·add_hard(및 그 별칭)의 첫 인자로 쓰인 식.
      4) 모호하지 않은 제약 메서드의 수신자로 쓰인 식(부트스트랩, "add" 는 제외).
    """
    names = set(_BASE_MODEL_NAMES)
    wrapper_like = WRAPPER_NAMES | {"add_hard"} | set(info.wrapper_aliases) | set(info.add_hard_aliases)
    for node in ast.walk(info.tree):
        if isinstance(node, ast.Assign) and _is_cpmodel_construction(node.value):
            for tgt in node.targets:
                if isinstance(tgt, (ast.Name, ast.Attribute)):
                    names.add(_unparse(tgt))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names |= _cpmodel_annotated_params(node)
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in wrapper_like and node.args:
                first = node.args[0]
                if isinstance(first, (ast.Name, ast.Attribute)):
                    names.add(_unparse(first))
            elif isinstance(node.func, ast.Attribute) and node.func.attr in _BOOTSTRAP_CONSTRAINT_NAMES:
                names.add(_unparse(node.func.value))
    _propagate_alias_chain(info.tree, names)
    return names


def _propagate_alias_chain(tree: ast.Module, names: set[str]) -> None:
    """`Y = X` 단순 대입 체인을 고정점까지 넣는다(r01·r02 — `_mm = m` 뒤 `_mm.add(...)` 는
    §기본값-의심 스코프(대상 모듈 전체를 의심)가 잡지만, **좁은** model_names 판정만 쓰는
    getattr 비리터럴(x06)·dunder(x07) 검사는 이 별칭 체인 자체를 model_names 에 넣어야
    잡는다 — 전에는 이 전파가 없어 `_mm = m; getattr(_mm, "Add"+"BoolOr")(...)` 류가
    완전히 안 보였다. 모듈 전체를 본다(함수 경계를 넘어도 된다 — 기존 수집과 같은 태도)."""
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
                continue
            tgt = node.targets[0]
            if not isinstance(tgt, (ast.Name, ast.Attribute)):
                continue
            if not isinstance(node.value, (ast.Name, ast.Attribute)):
                continue
            if _unparse(node.value) in names:
                dst = _unparse(tgt)
                if dst not in names:
                    names.add(dst)
                    changed = True


def _cpmodel_annotated_params(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    names: set[str] = set()
    all_args = (*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs)
    for arg in all_args:
        if arg.annotation is not None and "CpModel" in _unparse(arg.annotation):
            names.add(arg.arg)
    return names


def _is_container_construction(node: ast.AST) -> bool:
    """set()/{1,2}/{x for x in …} 류 — "모델이 아님"을 정적으로 증명할 수 있는 형태.
    dict/list 도 넣는다(오탐 방지 범위를 넓게 잡아 손해볼 게 없다 — 그 수신자들은 어차피
    "add" 이름의 모델 메서드가 아니므로 모델로 잘못 볼 위험만 줄인다)."""
    if isinstance(node, (ast.Set, ast.SetComp, ast.List, ast.ListComp, ast.Dict, ast.DictComp, ast.Tuple)):
        return True
    if isinstance(node, ast.Call):
        func = node.func
        name = func.id if isinstance(func, ast.Name) else (func.attr if isinstance(func, ast.Attribute) else None)
        return name in _CONTAINER_CONSTRUCTORS
    return False


def _container_annotated_params(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    names: set[str] = set()
    all_args = (*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs)
    for arg in all_args:
        if arg.annotation is None:
            continue
        ann = _unparse(arg.annotation).lower()
        if "set" in ann or "dict" in ann or "list" in ann:
            names.add(arg.arg)
    return names


def _collect_container_names(info: _ModuleInfo) -> set[str]:
    """`_is_container_construction` 으로 만들어졌다고 증명되는 수신자 식 집합
    (§기본값-의심 스코프에서 빼는 목록 — "set()/{...}/set 컴프리헨션 등으로 만든 것이
    확실한 수신자는 제외")."""
    names: set[str] = set()
    for node in ast.walk(info.tree):
        if isinstance(node, ast.Assign) and _is_container_construction(node.value):
            for tgt in node.targets:
                if isinstance(tgt, (ast.Name, ast.Attribute)):
                    names.add(_unparse(tgt))
        elif isinstance(node, ast.AnnAssign) and node.value is not None and _is_container_construction(node.value):
            if isinstance(node.target, (ast.Name, ast.Attribute)):
                names.add(_unparse(node.target))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names |= _container_annotated_params(node)
    return names


def _imports_cp_model(tree: ast.Module) -> bool:
    """이 모듈이 ortools.sat.python.cp_model 을 import 하는가."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and (
            "cp_model" in node.module or "ortools" in node.module
        ):
            return True
        if isinstance(node, ast.Import):
            for alias in node.names:
                if "cp_model" in alias.name or "ortools" in alias.name:
                    return True
    return False


#: §기본값-의심 스코프를 "새 파일"에도 적용하는 디렉터리(x02 보강, 2026-10-13) —
#: `services/constraints/` 의 실제 4개 파일은 전부 이미 TARGET_MODULES 라 지금은 효과가
#: 없다(과탐 0, 실측). 새 constraints 모듈(10-11 원형 — cp_model import 없이 매개변수
#: 이름 `cp` 로 받아 소문자 `cp.add(...)` 를 부르는 형태)이 추가되는 순간부터 걸린다.
#: `services/cp_sat/` 는 넣지 않는다 — 그 디렉터리의 기존 파일 다수(off_swap.py·
#: mid_feasibility.py 등 15개, 2026-10-13 실측)가 cp_model 을 import 하지 않고 평범한
#: set/dict 컨테이너에 `.add(...)` 를 광범위하게 쓰는데, 그 수신자 중 일부(`forced_off[key]`
#: 같은 구독식)는 컨테이너 증명이 베이스 이름에만 걸려 있어 디렉터리째 넣으면 과탐이
#: 생긴다 — 그 디렉터리의 안전한 확장은 별도 하드닝(컨테이너 증명 보강)이 선행돼야 한다.
_NEW_MODULE_SUSPECT_DIR_PREFIXES: tuple[str, ...] = ("services/constraints/",)


def _looks_like_model_receiver(receiver_src: str, info: _ModuleInfo) -> bool:
    """§기본값-의심 스코프 — 닫힌 model_names 집합이 아니어도, 대상/면제 모듈이거나
    cp_model 을 import 하는 모듈, 또는 `services/constraints/` 아래 새로 생긴 모듈에서는
    "컨테이너로 증명되지 않은" 수신자를 전부 모델로 의심한다(실측 근거: 이 범위 안의
    app/ 전체에 lowercase `.add(` 호출이 모델 메서드 말고는 하나도 없다 — 2026-10-11·
    2026-10-13 재실측, §모호성 판정 뒤에 갱신). 이 넓은 판정은 ambiguous attr("add")
    전용이다 — getattr/__getattribute__ 우회 검사(x06·x07)처럼 attr 이름이 임의일 때
    쓰면 `objective_terms.py` 의 `getattr(nu, f"{prefix}_exact", None)` 같은 비-모델
    실사용을 과탐한다(실측). 그런 검사는 `_is_recognized_model_receiver`(닫힌 집합만)를
    따로 쓴다."""
    if receiver_src in info.model_names:
        return True
    if receiver_src in info.container_names:
        return False
    base = receiver_src.split("[", 1)[0]
    if "[" in receiver_src and base in info.container_names:
        return False  # 증명된 컨테이너의 구독식(`forced_off[key]` 류) — 오탐 방지.
    return (
        info.relpath in TARGET_MODULES
        or info.relpath in EXEMPT
        or info.imports_cp_model
        or info.relpath.startswith(_NEW_MODULE_SUSPECT_DIR_PREFIXES)
    )


def _is_recognized_model_receiver(receiver_src: str, info: _ModuleInfo) -> bool:
    """좁은 판정 — 닫힌 model_names 집합에만 있을 때 True. getattr 비리터럴·
    __getattribute__ 우회처럼 속성 "이름" 이 임의인 검사가 쓴다(오탐 방지, 위 설명 참고)."""
    return receiver_src in info.model_names


def _is_ambiguous_site(attr: str, receiver_src: str, info: _ModuleInfo) -> bool:
    """attr 가 모호한 이름("add")이면 §기본값-의심 스코프로 판정. 그 외는 항상 True."""
    if attr != "add":
        return True
    return _looks_like_model_receiver(receiver_src, info)


# ── 후보 사이트 수집 ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Site:
    file: str
    line: int
    receiver: str
    attr: str
    call_node: ast.Call


def _candidate_sites(info: _ModuleInfo, names: frozenset[str]) -> list[_Site]:
    sites = []
    for node in ast.walk(info.tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in names:
            receiver = _unparse(node.func.value)
            if _is_ambiguous_site(node.func.attr, receiver, info):
                sites.append(_Site(info.relpath, node.lineno, receiver, node.func.attr, node))
    return sites


# ── 3·4) 대상 모듈 0 / 면제 모듈 고정(수 + 지문 해시) / 그 밖 모듈 0 ──────────


def _call_fingerprint(call_node: ast.Call) -> str:
    """정규화된 호출 "지문" — ast.unparse 는 공백·줄바꿈 차이를 지우지만 호출 내용
    (수신자·인자 식)은 그대로 남긴다(x13 — 수만 같고 내용이 바뀌는 바꿔치기를 잡는다)."""
    return _unparse(call_node)


def _multiset_digest(fingerprints: list[str]) -> str:
    """지문 다중집합(순서 무관) 의 sha256 — 정렬해 이어붙인 뒤 해시한다."""
    return hashlib.sha256("\n".join(sorted(fingerprints)).encode("utf-8")).hexdigest()


def _check_module_call_counts(modules: list[_ModuleInfo]) -> list[Violation]:
    out: list[Violation] = []
    for info in modules:
        sites = _candidate_sites(info, CONSTRAINT_NAMES)
        if info.relpath in TARGET_MODULES:
            out.extend(_violate_raw_sites_in_target(sites))
        elif info.relpath in EXEMPT:
            out.extend(_violate_exempt_mismatch(info.relpath, sites))
        else:
            out.extend(_violate_raw_sites_in_other(sites))
    return out


def _violate_raw_sites_in_target(sites: list[_Site]) -> list[Violation]:
    return [
        Violation(
            s.file, s.line, "raw_call_in_target",
            f"{s.file}:{s.line} — 대상 모듈에서 {s.receiver}.{s.attr}(...) 를 이름표 없이 직접 "
            f"호출했다 — hard_add(m, \"규칙id\", expr) 또는 hard_call(m, \"규칙id\", "
            f"\"{s.attr}\", ...)로 바꾸고, 새 규칙이면 engine_contract/rules.py 에 "
            f"RuleSpec 1줄을 추가하라(설계서 §2.7 체크리스트).",
        )
        for s in sites
    ]


def _violate_raw_sites_in_other(sites: list[_Site]) -> list[Violation]:
    return [
        Violation(
            s.file, s.line, "raw_call_in_new_module",
            f"{s.file}:{s.line} — 대상 모듈도 면제 목록(EXEMPT)도 아닌 모듈에서 "
            f"{s.receiver}.{s.attr}(...) 를 호출했다(새 파일로 이름표 감사를 우회) — "
            f"rule_tag 래퍼로 감싸거나, 대상 모듈로 옮기거나, 진단용 별도 모델이면 "
            f"tests/engine_contract/rule_audit.py 의 EXEMPT 에 사유와 실측 호출 수를 추가하라.",
        )
        for s in sites
    ]


def _violate_exempt_mismatch(relpath: str, sites: list[_Site]) -> list[Violation]:
    _reason, expected, expected_digest = EXEMPT[relpath]
    actual = len(sites)
    if actual != expected:
        lines = ", ".join(str(s.line) for s in sites) or "(없음)"
        direction = "늘었다" if actual > expected else "줄었다"
        return [
            Violation(
                relpath, sites[0].line if sites else 1, "exempt_count_mismatch",
                f"{relpath}:1 — 면제 모듈의 이름표 없는 제약 호출 수가 {expected}곳에서 "
                f"{actual}곳으로 {direction}(실제 줄: {lines}) — 늘었으면 새 호출에 이름표를 "
                f"달거나(hard_add/hard_call) 의도적 추가라면 EXEMPT 의 기대 호출 수와 지문 "
                f"해시를 같이 올리고 사유를 적어라. 줄었으면 기대 호출 수와 해시를 낮춰라.",
            )
        ]
    digest = _multiset_digest([_call_fingerprint(s.call_node) for s in sites])
    if digest != expected_digest:
        lines = ", ".join(str(s.line) for s in sites) or "(없음)"
        return [
            Violation(
                relpath, sites[0].line if sites else 1, "exempt_content_swapped",
                f"{relpath}:1 — 면제 모듈의 이름표 없는 제약 호출 수는 {actual}곳으로 같지만 "
                f"호출 내용이 바뀌었다(정규화 지문 다중집합 해시 불일치, 실제 줄: {lines}) — "
                f"수를 유지한 채 다른 호출로 바꿔치기하면 이렇게 잡힌다(x13). 의도적 변경이면 "
                f"EXEMPT 의 기대 해시를 다시 계산해 갱신하고 사유를 적어라. 아니면 원래 코드로 "
                f"되돌려라.",
            )
        ]
    return []


# ── 가정(assumption) 위치 — hard_assumption.py 밖에서 쓰면 측정·고정 ─────────


def _check_assumption_locations(modules: list[_ModuleInfo]) -> list[Violation]:
    out: list[Violation] = []
    for info in modules:
        if info.relpath == ASSUMPTION_HOME:
            continue
        sites = _candidate_sites(info, ASSUMPTION_NAMES)
        expected = ASSUMPTION_BASELINE_ELSEWHERE.get(info.relpath, 0)
        if len(sites) == expected:
            continue
        lines = ", ".join(str(s.line) for s in sites) or "(없음)"
        out.append(
            Violation(
                info.relpath, sites[0].line if sites else 1, "assumption_outside_home",
                f"{info.relpath}:1 — 가정(assumption) 메서드 호출이 {ASSUMPTION_HOME} 밖에서 "
                f"기준선 {expected}곳과 다르게 {len(sites)}곳 나왔다(실제 줄: {lines}) — "
                f"가정 관리는 {ASSUMPTION_HOME} 한 곳에 모아야 한다(설계서 §2.3). 새 자리면 "
                f"그 파일의 레지스트리로 옮기거나, 의도적이면 이 파일의 "
                f"ASSUMPTION_BASELINE_ELSEWHERE 에 사유와 실측 호출 수를 추가하라.",
            )
        )
    return out


# ── 5) 우회 형태 ─────────────────────────────────────────────────────────


def _check_bypass_forms(modules: list[_ModuleInfo]) -> list[Violation]:
    out: list[Violation] = []
    for info in modules:
        out.extend(_bypass_attribute_extraction(info))
        out.extend(_bypass_getattr_literal(info))
        out.extend(_bypass_getattr_dynamic(info))
        out.extend(_bypass_dunder_getattr(info))
        out.extend(_bypass_proto_manipulation(info))
        out.extend(_bypass_eval_exec_reflection(info))
        out.extend(_bypass_wrapper_rebind(info))
        out.extend(_bypass_wrapper_import_alias(info))
        out.extend(_bypass_module_alias_import(info))
        out.extend(_bypass_dynamic_module_import(info))
        out.extend(_bypass_local_wrapper_alias(info))
        out.extend(_bypass_name_extraction(info))
        out.extend(_bypass_constraint_name_literal(info))
    return out


_WATCHED_METHOD_LITERALS = CONSTRAINT_NAMES | ASSUMPTION_NAMES


def _call_wrapper_method_arg_ids(info: _ModuleInfo) -> set[int]:
    """hard_call/aux_call 호출의 "메서드 이름"(셋째 인자, `node.args[2]`) 자리 — 이 자리만
    제약/가정 메서드 이름 리터럴이 나와도 정당하다(그 호출이 바로 이름표 래퍼이므로)."""
    ids: set[int] = set()
    for node in ast.walk(info.tree):
        if not isinstance(node, ast.Call):
            continue
        wname = _resolve_wrapper_call(node, info)
        if wname in CALL_WRAPPERS and len(node.args) >= 3:
            ids.add(id(node.args[2]))
    return ids


def _bypass_constraint_name_literal(info: _ModuleInfo) -> list[Violation]:
    """대상 모듈(§기본값-의심 스코프와 같은 범위)에서 제약/가정 메서드 이름과 글자 그대로
    같은 문자열 리터럴이 hard_call/aux_call 의 메서드 자리 밖에 나타나는 자리(r09·r03~r08
    류 반사(reflection) 형태의 공통 방어선). 수신자가 무엇이든(모델 별칭·함수 매개변수·
    헬퍼 경유 등) 결국
    메서드 "이름"을 문자열로 들고 있어야 하므로, 그 리터럴이 정당한 자리(hard_call/aux_call
    셋째 인자) 밖에 나타난 것 자체를 막으면 수신자를 추적하지 않고도 반사(reflection) 형태
    전반을 잡는다 — `getattr`/`__getattribute__`/`operator.methodcaller`/`type(m).__dict__[...]`
    등 어떤 우회 형태든 메서드 이름 리터럴은 코드 어딘가에 그대로 남기 때문이다.
    `services.cp_sat/`·`services/constraints/` 바깥(오탐 실측이 없는 범위 밖)에는 적용하지
    않는다(§기본값-의심 스코프와 같은 근거 — 대상/면제 모듈·cp_model importer)."""
    if not (info.relpath in TARGET_MODULES or info.relpath in EXEMPT or info.imports_cp_model):
        return []
    exempt_ids = _call_wrapper_method_arg_ids(info)
    out = []
    for node in ast.walk(info.tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if node.value not in _WATCHED_METHOD_LITERALS:
            continue
        if id(node) in exempt_ids:
            continue
        out.append(
            Violation(
                info.relpath, node.lineno, "bypass_constraint_name_literal",
                f"{info.relpath}:{node.lineno} — 제약/가정 메서드 이름과 같은 문자열 리터럴 "
                f"\"{node.value}\" 이 hard_call/aux_call 의 메서드 자리(셋째 인자) 밖에 "
                f"나타났다 — getattr·operator.methodcaller/attrgetter·클래스 __dict__ 첨자·"
                f"헬퍼 함수 경유 등으로 제약 메서드를 반사 호출하는 자리일 수 있다. "
                f"hard_add/hard_call(또는 aux_add/aux_call)로 직접 호출하라.",
            )
        )
    return out


_WATCHED_FOR_EXTRACTION = CONSTRAINT_NAMES | ASSUMPTION_NAMES


def _bypass_attribute_extraction(info: _ModuleInfo) -> list[Violation]:
    """`f = m.Add` · `functools.partial(m.Add)` 처럼 호출 없이 속성만 꺼내는 자리."""
    consumed: set[int] = set()
    for node in ast.walk(info.tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            consumed.add(id(node.func))
    out = []
    for node in ast.walk(info.tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr in _WATCHED_FOR_EXTRACTION
            and id(node) not in consumed
            and _is_ambiguous_site(node.attr, _unparse(node.value), info)
        ):
            recv = _unparse(node.value)
            out.append(
                Violation(
                    info.relpath, node.lineno, "bypass_extracted",
                    f"{info.relpath}:{node.lineno} — {recv}.{node.attr} 를 호출하지 않고 "
                    f"속성으로 꺼냈다(변수 대입·인자 전달 등) — A1 은 직접 호출만 본다. "
                    f"hard_add/hard_call 래퍼로 즉시 호출하도록 고쳐라.",
                )
            )
    return out


def _bypass_getattr_literal(info: _ModuleInfo) -> list[Violation]:
    """`getattr(x, \"Add\")` 처럼 문자열 리터럴로 제약 메서드를 꺼내는 자리."""
    watched = CONSTRAINT_NAMES | ASSUMPTION_NAMES | HINT_NAMES | DECISION_STRATEGY_NAMES
    out = []
    for node in ast.walk(info.tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr"):
            continue
        if len(node.args) < 2 or not isinstance(node.args[1], ast.Constant):
            continue
        name = node.args[1].value
        if isinstance(name, str) and name in watched:
            out.append(
                Violation(
                    info.relpath, node.lineno, "bypass_getattr",
                    f"{info.relpath}:{node.lineno} — getattr(..., \"{name}\") 로 제약 메서드를 "
                    f"리터럴 이름으로 꺼냈다 — hard_add/hard_call 래퍼를 쓰라(래퍼 내부의 "
                    f"getattr(m, method) 는 method 가 변수라 이 검사에 걸리지 않는다).",
                )
            )
    return out


def _call_wrapper_impl_descendant_ids(info: _ModuleInfo) -> set[int]:
    """rule_tag.py 자신 안에서, **hard_call/aux_call 로 이름 붙은 함수 정의의 본문 안**
    자손 노드 id 집합(r11 보강, 2026-10-13 — 전에는 모듈 전체를 면제해서, 같은 모듈에
    새 함수(예: `plain_call`)를 추가해도 그 안의 `getattr(m, method)` 가 똑같이 면제돼
    뚫렸다). 이 함수 본문 **안**의 getattr 호출만 "정당한 비-리터럴 getattr" 다 — 다른
    함수(새로 추가된 것이든 기존 것이든)는 더 이상 예외가 아니다."""
    ids: set[int] = set()
    if info.relpath != RULE_TAG_MODULE_PATH:
        return ids
    for node in ast.walk(info.tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in CALL_WRAPPERS:
            for sub in ast.walk(node):
                ids.add(id(sub))
    return ids


def _bypass_getattr_dynamic(info: _ModuleInfo) -> list[Violation]:
    """`getattr(m, \"Add\" + \"BoolOr\")` 처럼 비-리터럴 식으로 메서드 이름을 만들어
    제약 메서드를 꺼내는 자리(x06). rule_tag.py 자신의 `getattr(m, method)`(method 는
    hard_call/aux_call 매개변수) 는 그 정의 본문 **안**에서만 제외한다(r11 — 모듈 전체를
    면제하면 새 함수를 몰래 추가해도 똑같이 면제된다). 그 패턴이 이 검사의 유일한 "정당한"
    비-리터럴 getattr 실사용이다(실측, §모호성 판정 참고). 수신자 판정은 **좁은**
    `_is_recognized_model_receiver` 를 쓴다 — attr 이름이 고정이 아니라서(어떤 속성 이름이든
    꺼낼 수 있어서) 넓은 §기본값-의심 스코프를 쓰면 `objective_terms.py` 의
    `getattr(nu, f"{prefix}_exact", None)` 같은 비-모델 실사용을 과탐한다(실측, 2026-10-13)."""
    exempt_ids = _call_wrapper_impl_descendant_ids(info)
    out = []
    for node in ast.walk(info.tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr"):
            continue
        if id(node) in exempt_ids:
            continue
        if len(node.args) < 2 or isinstance(node.args[1], ast.Constant):
            continue  # 리터럴은 _bypass_getattr_literal 이 본다
        receiver = _unparse(node.args[0])
        if not _is_recognized_model_receiver(receiver, info):
            continue
        out.append(
            Violation(
                info.relpath, node.lineno, "bypass_getattr_dynamic",
                f"{info.relpath}:{node.lineno} — getattr({receiver}, {_unparse(node.args[1])}) 로 "
                f"메서드 이름을 비-리터럴 식으로 만들어 제약 메서드를 꺼낸다 — 정적 감사가 "
                f"어떤 메서드인지 알 수 없다. hard_add/hard_call 래퍼를 쓰거나 리터럴 메서드 "
                f"이름으로 바꿔라.",
            )
        )
    return out


_DUNDER_ATTR_ACCESSORS = frozenset({"__getattribute__", "__getattr__"})


def _bypass_dunder_getattr(info: _ModuleInfo) -> list[Violation]:
    """`m.__getattribute__(\"Add\")` 처럼 dunder 로 속성 접근을 우회하는 자리(x07).
    이 형태는 어떤 모듈에도 정당한 쓰임이 없어(실측 — app/ 전체에 __getattribute__/
    __getattr__ 호출이 하나도 없다) 예외 없이 좁은 모델 수신자 판정만으로 막는다."""
    out = []
    for node in ast.walk(info.tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr not in _DUNDER_ATTR_ACCESSORS:
            continue
        receiver = _unparse(node.func.value)
        if not _is_recognized_model_receiver(receiver, info):
            continue
        out.append(
            Violation(
                info.relpath, node.lineno, "bypass_dunder_getattr",
                f"{info.relpath}:{node.lineno} — {receiver}.{node.func.attr}(...) 로 제약 메서드를 "
                f"우회해서 꺼낸다 — hard_add/hard_call 래퍼를 쓰라.",
            )
        )
    return out


_PROTO_MUTATING_ATTRS = frozenset({"Proto", "proto"})


def _bypass_proto_manipulation(info: _ModuleInfo) -> list[Violation]:
    """`m.Proto().constraints.extend(...)`·`m.Proto().MergeFrom(m.Clone().Proto())` 처럼
    ortools 래퍼를 거치지 않고 protobuf 를 직접 조작하는 자리(x08 의 이웃 형태 — p08b·
    p08c). x08(`m.Proto().constraints.add()`)은 메서드 이름이 소문자 `add` 라서 잡혔을
    뿐이다 — `.extend`·`MergeFrom`·`CopyFrom`·`ParseFromString`·`Merge` 는 "제약 메서드"
    이름 집합 밖이라 그 검사로는 못 잡는다. 그래서 메서드 이름과 무관하게, 모델(좁은
    model_names 판정)의 `.Proto()`/`.proto` 접근 자체를 막는다 — IntVar 등 비-모델 수신자의
    정당한 `.Proto()` 실사용(fallback_lex.py:4555 `_v4.Proto().domain`)은 narrow 판정이라
    과탐하지 않는다(실측)."""
    out = []
    for node in ast.walk(info.tree):
        if not (isinstance(node, ast.Attribute) and node.attr in _PROTO_MUTATING_ATTRS):
            continue
        receiver = _unparse(node.value)
        if not _is_recognized_model_receiver(receiver, info):
            continue
        out.append(
            Violation(
                info.relpath, node.lineno, "bypass_proto_direct_access",
                f"{info.relpath}:{node.lineno} — 모델 {receiver} 의 .{node.attr} 에 직접 "
                f"접근했다(protobuf 직접 조작 — .extend/.MergeFrom/.CopyFrom 등으로 이름표 "
                f"없이 제약을 더할 수 있다) — hard_add/hard_call 래퍼를 쓰라.",
            )
        )
    return out


_EVAL_EXEC_NAMES = frozenset({"eval", "exec", "compile"})


def _bypass_eval_exec_reflection(info: _ModuleInfo) -> list[Violation]:
    """`eval(\"m.Add(_w_ban_expr)\")` 처럼 문자열을 조립해 eval/exec 로 실행하는 자리
    (r10). 메서드 이름 리터럴이 호출 인자가 아니라 **더 큰 문자열의 부분문자열**로
    들어가므로, 리터럴 전체 일치만 보는 `_bypass_constraint_name_literal` 로는 못 잡는다
    — 그래서 따로 부분문자열(`.<제약메서드>(`)을 찾는다."""
    out = []
    for node in ast.walk(info.tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else (func.attr if isinstance(func, ast.Attribute) else None)
        if name not in _EVAL_EXEC_NAMES or not node.args:
            continue
        arg0 = node.args[0]
        if not (isinstance(arg0, ast.Constant) and isinstance(arg0.value, str)):
            continue
        hit = next((m for m in CONSTRAINT_NAMES if f".{m}(" in arg0.value), None)
        if hit is None:
            continue
        out.append(
            Violation(
                info.relpath, node.lineno, "bypass_eval_exec_reflection",
                f"{info.relpath}:{node.lineno} — {name}(...) 에 넘긴 문자열 안에 제약 메서드 "
                f"호출(\".{hit}(\")이 그대로 들어 있다 — 문자열을 조립해 실행하는 방식으로 "
                f"이름표 감사를 피할 수 있다. hard_add/hard_call 래퍼로 직접 호출하라.",
            )
        )
    return out


def _bypass_wrapper_rebind(info: _ModuleInfo) -> list[Violation]:
    """rule_tag.py 밖에서 hard_add 등을 다시 정의/대입하는 자리."""
    if info.relpath == RULE_TAG_MODULE_PATH:
        return []
    out = []
    for node in ast.walk(info.tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in WRAPPER_NAMES:
            out.append(_wrapper_rebind_violation(info.relpath, node.lineno, node.name, "다시 정의"))
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id in WRAPPER_NAMES:
                    out.append(_wrapper_rebind_violation(info.relpath, node.lineno, tgt.id, "다시 대입"))
    return out


def _wrapper_rebind_violation(relpath: str, line: int, name: str, verb: str) -> Violation:
    return Violation(
        relpath, line, "bypass_wrapper_rebind",
        f"{relpath}:{line} — 래퍼 이름 {name} 을 {verb}했다 — "
        f"{RULE_TAG_MODULE.replace('.', '/')}.py 가 유일한 정의처다. 이 줄을 지우고 "
        f"'from {RULE_TAG_MODULE} import {name}' 로 가져오라.",
    )


def _bypass_local_wrapper_alias(info: _ModuleInfo) -> list[Violation]:
    """`_h = hard_add; _h(...)` 처럼 래퍼(또는 add_hard)를 새 로컬 이름으로 복사하는
    자리(x04 — 대입 체인으로 이름표 감사를 피할 수 있다). `_bypass_wrapper_rebind` 는
    "래퍼 이름 자체"를 다른 값으로 재대입하는 반대 방향을 보고, 이 함수는 "래퍼를 다른
    이름으로" 복사하는 방향을 본다 — 둘은 서로 배타적이라 중복 보고가 없다."""
    if info.relpath == RULE_TAG_MODULE_PATH:
        return []
    direct_names = WRAPPER_NAMES | {"add_hard"}
    out = []
    for node in ast.walk(info.tree):
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
            continue
        tgt = node.targets[0]
        if not (
            isinstance(tgt, ast.Name)
            and isinstance(node.value, ast.Name)
            and node.value.id in direct_names
            and tgt.id not in direct_names
        ):
            continue
        out.append(
            Violation(
                info.relpath, node.lineno, "bypass_local_wrapper_alias",
                f"{info.relpath}:{node.lineno} — {node.value.id} 을 새 이름 {tgt.id} 로 대입해 "
                f"복사했다(대입 체인으로 이름표 감사를 피할 수 있다) — {node.value.id} 를 직접 "
                f"호출하라.",
            )
        )
    return out


def _bypass_module_alias_import(info: _ModuleInfo) -> list[Violation]:
    """모듈(rule_tag·hard_assumption) 자체를 이름에 묶어 속성 호출로 이름표 감사를 피하는
    자리 — 두 형태 모두 본다(2026-10-13 재보강 — 전에는 ①만, 그것도 별칭(asname)이 있을
    때만 봐서 x03 의 10-11 원형(ImportFrom)·p03b(별칭 없음)·r12(상대경로)가 전부 안 보였다):
      ① `import services.cp_sat.rule_tag [as _rt]` — 별칭 없어도(`services` 로 시작하는
         완전 점 경로 호출(p03c)의 전제 조건이라) 위반이다.
      ② `from services.cp_sat import rule_tag [as _rt]`(상대경로 `from . import rule_tag`
         포함) — 모듈 자체를 이름(또는 별칭)으로 들여오는 형태. 함수를 직접 들여오는 정상
         형태(`from services.cp_sat.rule_tag import hard_add`)는 dotted 후보가 달라 안 걸린다."""
    out = []
    for node in ast.walk(info.tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in (RULE_TAG_MODULE, HARD_ASSUMPTION_MODULE):
                    local = alias.asname or alias.name
                    out.append(_module_import_violation(info.relpath, node.lineno, alias.name, local))
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_relative_module(info.relpath, node)
            if not base:
                continue
            for alias in node.names:
                candidate = f"{base}.{alias.name}"
                if candidate in (RULE_TAG_MODULE, HARD_ASSUMPTION_MODULE):
                    local = alias.asname or alias.name
                    out.append(_module_import_violation(info.relpath, node.lineno, candidate, local))
    return out


def _module_import_violation(relpath: str, line: int, module: str, local: str) -> Violation:
    return Violation(
        relpath, line, "bypass_module_alias_import",
        f"{relpath}:{line} — {module} 을 모듈 객체로 가져와 이름 {local} 에 묶었다(속성 호출로 "
        f"부르면 이름표 호출로 안 보여 감사를 피할 수 있다) — 'from {module} import <이름>' "
        f"형태(함수를 직접, 별칭 없이)로 바꿔라.",
    )


def _bypass_dynamic_module_import(info: _ModuleInfo) -> list[Violation]:
    """`importlib.import_module("services.cp_sat.rule_tag")`·`__import__(...)` 처럼 import
    문 없이 런타임에 모듈 객체를 만들어내는 자리(r24) — 어떤 import 문도 안 남아서
    `_bypass_module_alias_import`(import 문 자체를 보는 검사)가 못 보는 형태다. 결과를
    바로 속성 호출로 이어 쓰는지(`....hard_add(...)`)와 무관하게, 이 리터럴 호출 자체가
    감사를 피해가는 통로이므로 발견 즉시 위반으로 본다."""
    out = []
    for node in ast.walk(info.tree):
        if not isinstance(node, ast.Call):
            continue
        literal = _dynamic_import_literal(node)
        if literal in (RULE_TAG_MODULE, HARD_ASSUMPTION_MODULE):
            out.append(
                Violation(
                    info.relpath, node.lineno, "bypass_dynamic_module_import",
                    f"{info.relpath}:{node.lineno} — {literal} 을 import 문 없이 동적으로"
                    f"({_unparse(node)}) 가져왔다(어떤 import 문도 안 남아 이름표 감사가 더 "
                    f"어려워진다) — 'from {literal} import <이름>' 형태(함수를 직접)로 바꿔라.",
                )
            )
    return out


def _bypass_name_extraction(info: _ModuleInfo) -> list[Violation]:
    """`functools.partial(hard_add, m, \"x\")` 처럼 래퍼(또는 add_hard) 를 호출하지
    않고 Name 값으로 다른 호출에 넘기는 자리(x20). 단순 재대입(`_h = hard_add`)은
    `_bypass_local_wrapper_alias` 가 따로 보고하므로 여기서는 제외해 중복 보고를
    피한다."""
    if info.relpath == RULE_TAG_MODULE_PATH:
        return []
    canonical = WRAPPER_NAMES | {"add_hard"}
    call_func_ids: set[int] = set()
    simple_assign_value_ids: set[int] = set()
    for node in ast.walk(info.tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            call_func_ids.add(id(node.func))
        elif (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Name)
        ):
            simple_assign_value_ids.add(id(node.value))
    out = []
    for node in ast.walk(info.tree):
        if not isinstance(node, ast.Name) or node.id not in canonical:
            continue
        if id(node) in call_func_ids or id(node) in simple_assign_value_ids:
            continue
        if isinstance(node.ctx, ast.Store):
            continue
        out.append(
            Violation(
                info.relpath, node.lineno, "bypass_name_extracted",
                f"{info.relpath}:{node.lineno} — {node.id} 을 호출하지 않고 값으로 꺼내 "
                f"넘겼다(functools.partial 등으로 감싸면 호출로 안 보여 감사를 피할 수 있다) — "
                f"{node.id}(...) 형태로 즉시 호출하라.",
            )
        )
    return out


def _resolve_relative_module(relpath: str, node: ast.ImportFrom) -> str | None:
    """상대경로 import(`from .rule_tag import X`·`from ..cp_sat import X`)의 정본
    dotted module path. 절대경로(level=0)면 node.module 그대로 돌려준다(x05 —
    상대경로라 `node.module == RULE_TAG_MODULE` 같은 문자열 비교가 전혀 안 맞아서
    감사를 통째로 피해가던 자리)."""
    if node.level == 0:
        return node.module
    parent = Path(relpath).parent.as_posix()
    package_parts: list[str] = [] if parent in ("", ".") else parent.split("/")
    up = node.level - 1
    if up > len(package_parts):
        return None
    base_parts = package_parts[: len(package_parts) - up]
    if node.module:
        return ".".join([*base_parts, node.module])
    return ".".join(base_parts) if base_parts else None


def _module_import_aliases(tree: ast.Module, relpath: str) -> dict[str, str]:
    """로컬 이름(단일 Name) → 정본 dotted module path. 두 형태를 본다(x03·x15·p03b·r12,
    2026-10-13 재보강 — 전에는 ①만 봐서 `from services.cp_sat import rule_tag as _rt` 류
    (ImportFrom 으로 모듈 자체를 들여오는 형태)가 전부 안 보였다):
      ① `import a.b.c as X` — 별칭 X(기존).
      ② `from a.b import c [as X]` — 서브모듈 c 자체를 이름(또는 별칭) X 로 들여오는 형태.
         상대경로(`from . import c`)도 `_resolve_relative_module` 로 해소해서 본다(r12).
         "이름 c 로 **함수**를 들여오는" 정상 형태(`from services.cp_sat.rule_tag import
         hard_add`)는 후보 dotted path 가 두 모듈 이름과 다르므로 섞이지 않는다."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    aliases[alias.asname] = alias.name
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_relative_module(relpath, node)
            if not base:
                continue
            for alias in node.names:
                candidate = f"{base}.{alias.name}"
                if candidate in (RULE_TAG_MODULE, HARD_ASSUMPTION_MODULE):
                    aliases[alias.asname or alias.name] = candidate
    return aliases


def _name_alias_chain(
    tree: ast.Module, relpath: str, canonical_names: frozenset[str] | set[str], home_module: str,
) -> dict[str, str]:
    """canonical_names(예: WRAPPER_NAMES) 의 로컬 별칭 → 정본 이름. 근거:
      ① `from home_module import X [as Y]`(상대경로 포함, `_resolve_relative_module` 로
         해소 — x05).
      ② `Y = X`(단순 재대입) 체인을 고정점까지 푼다(x04 — `_h = hard_add; _h2 = _h` 같은
         다단 별칭도 전부 정본으로 돌아간다)."""
    aliases: dict[str, str] = {n: n for n in canonical_names}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        resolved = _resolve_relative_module(relpath, node)
        if resolved != home_module:
            continue
        for alias in node.names:
            if alias.name in canonical_names:
                aliases[alias.asname or alias.name] = alias.name
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
                continue
            tgt = node.targets[0]
            if not (isinstance(tgt, ast.Name) and isinstance(node.value, ast.Name)):
                continue
            if node.value.id in aliases and tgt.id not in aliases:
                aliases[tgt.id] = aliases[node.value.id]
                changed = True
    return aliases


def _dynamic_import_literal(node: ast.Call) -> str | None:
    """`importlib.import_module(\"X\")` 또는 `__import__(\"X\")` 호출의 리터럴 모듈 경로
    (r24 — 모듈 객체를 어떤 import 문도 없이 런타임에 만들어내는 형태). 인자가 문자열
    리터럴이 아니면(동적 경로) 해소하지 않는다 — 그런 형태는 더 근본적으로 추적 불가능해
    이 검사의 범위 밖이다."""
    if not node.args or not isinstance(node.args[0], ast.Constant) or not isinstance(node.args[0].value, str):
        return None
    func = node.func
    if isinstance(func, ast.Name) and func.id == "__import__":
        return node.args[0].value
    if isinstance(func, ast.Attribute) and func.attr == "import_module":
        return node.args[0].value
    return None


def _resolved_module_of(value_node: ast.expr, info: _ModuleInfo) -> str | None:
    """이 식이 가리키는 모듈이 RULE_TAG_MODULE·HARD_ASSUMPTION_MODULE 중 하나면 그 정본
    문자열을, 아니면 None 을 돌려준다 — 세 형태를 전부 본다:
      ① Name(모듈 별칭) — `import ... as _rt`·`from pkg import rule_tag [as _rt]`(x03·p03b·r12).
      ② 완전 점 경로(Attribute 체인) — `services.cp_sat.rule_tag`(별칭 없이 그대로, p03c).
      ③ `importlib.import_module(...)`/`__import__(...)` 동적 호출(r24)."""
    if isinstance(value_node, ast.Name):
        resolved = info.module_aliases.get(value_node.id)
        return resolved if resolved in (RULE_TAG_MODULE, HARD_ASSUMPTION_MODULE) else None
    if isinstance(value_node, ast.Call):
        literal = _dynamic_import_literal(value_node)
        return literal if literal in (RULE_TAG_MODULE, HARD_ASSUMPTION_MODULE) else None
    candidate = _unparse(value_node)
    return candidate if candidate in (RULE_TAG_MODULE, HARD_ASSUMPTION_MODULE) else None


def _resolve_wrapper_call(node: ast.Call, info: _ModuleInfo) -> str | None:
    """node 가 (직접 호출·대입 체인 별칭·모듈 별칭/점경로/동적 import 속성 호출 중 어느
    형태로든) rule_tag 래퍼를 부르는 호출이면 정본 래퍼 이름을 돌려준다 — x03(모듈 별칭
    속성 호출)·x04(로컬 재대입 체인)·x05(상대경로 import 별칭)·p03b/p03c(별칭 없는
    ImportFrom·완전 점 경로)·r12(`from . import rule_tag`)·r24(importlib 동적 import) 가
    뚫은 간접 호출 형태를 하나로 모은다."""
    if isinstance(node.func, ast.Name):
        return info.wrapper_aliases.get(node.func.id)
    if isinstance(node.func, ast.Attribute) and node.func.attr in WRAPPER_NAMES:
        if _resolved_module_of(node.func.value, info) == RULE_TAG_MODULE:
            return node.func.attr
    return None


def _resolve_add_hard_call(node: ast.Call, info: _ModuleInfo) -> bool:
    """node 가 (직접·별칭·점경로·동적 import 속성 호출 중 어느 형태로든) add_hard 를
    부르는가(x15 — `_ha.add_hard(...)` 모듈 속성 호출 포함, p03b/p03c/r24 동일 확장)."""
    if isinstance(node.func, ast.Name):
        return node.func.id in info.add_hard_aliases
    if isinstance(node.func, ast.Attribute) and node.func.attr == "add_hard":
        return _resolved_module_of(node.func.value, info) == HARD_ASSUMPTION_MODULE
    return False


def _bypass_wrapper_import_alias(info: _ModuleInfo) -> list[Violation]:
    """래퍼 이름을 alias import 로 바꾸거나, 다른 모듈의 이름을 래퍼 이름으로 가져오는
    자리. 상대경로 import 도 `_resolve_relative_module` 로 해소해서 본다(x05)."""
    out = []
    for node in ast.walk(info.tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        resolved_module = _resolve_relative_module(info.relpath, node)
        for alias in node.names:
            local = alias.asname or alias.name
            original_is_wrapper = alias.name in WRAPPER_NAMES and resolved_module == RULE_TAG_MODULE
            local_is_wrapper = local in WRAPPER_NAMES
            if not original_is_wrapper and not local_is_wrapper:
                continue
            if original_is_wrapper and alias.asname is None:
                continue  # 정상: `from services.cp_sat.rule_tag import hard_add`
            out.append(_import_alias_violation(info.relpath, node.lineno, local, resolved_module, alias))
    return out


def _import_alias_violation(relpath: str, line: int, local: str, module: str | None, alias: ast.alias) -> Violation:
    if module == RULE_TAG_MODULE:
        detail = f"{alias.name} 을 별칭 {local} 로 가져왔다(이름을 숨겨 감사를 피할 수 있다)"
        # 제안 import 문은 "원래 이름"(alias.name — 예: hard_add)을 써야 한다. 별칭(local —
        # 예: h)을 그대로 추천하면 고치라고 한 말이 다시 같은 별칭을 만든다(2026-10-13 지적).
        suggestion = f"'from {RULE_TAG_MODULE} import {alias.name}'"
    else:
        detail = f"다른 모듈({module})의 {alias.name} 을 래퍼 이름 {local} 으로 가져왔다(위장)"
        # 위장 케이스는 local 자체가 이미 정본 래퍼 이름이므로(그 이름을 사칭했으므로) 그대로
        # 쓰는 것이 맞다.
        suggestion = f"'from {RULE_TAG_MODULE} import {local}'"
    return Violation(
        relpath, line, "bypass_import_alias",
        f"{relpath}:{line} — {detail} — 대상 모듈은 반드시 {suggestion} 형태(별칭 없이, 그 "
        f"모듈에서)로 가져와야 한다.",
    )


def _check_missing_rule_tag_import(modules: list[_ModuleInfo]) -> list[Violation]:
    """hard_add 등을 쓰면서(직접·별칭·모듈 속성 호출 어느 형태로든) rule_tag import 문이
    없는 모듈."""
    out = []
    for info in modules:
        used = {
            _resolve_wrapper_call(n, info)
            for n in ast.walk(info.tree)
            if isinstance(n, ast.Call)
        } - {None}
        if not used:
            continue
        has_from_import = any(
            isinstance(n, ast.ImportFrom) and _resolve_relative_module(info.relpath, n) == RULE_TAG_MODULE
            for n in ast.walk(info.tree)
        )
        has_module_import = any(
            isinstance(n, ast.Import) and any(a.name == RULE_TAG_MODULE for a in n.names)
            for n in ast.walk(info.tree)
        )
        if not (has_from_import or has_module_import):
            out.append(
                Violation(
                    info.relpath, 1, "missing_rule_tag_import",
                    f"{info.relpath}:1 — {sorted(used)} 를 쓰지만 "
                    f"'from {RULE_TAG_MODULE} import ...' import 문이 없다 — 이 형태로 추가하라.",
                )
            )
    return out


# ── 6·7·8) id/pattern 등록 여부 + strength 일치 ──────────────────────────


def _validate_id_expr(
    id_arg: ast.expr, *, relpath: str, line: int, context: str, registry,
) -> tuple[list[Violation], object | None]:
    """id 인자(또는 add_hard meta pattern)를 검증한다. (위반 목록, 매칭된 RuleSpec).

    `registry` 는 §지시 11("17")에 따라 `scan(app_root)` 가 그 app_root 자신의 rules.py
    를 경로로 로드한 모듈(또는 합성 트리면 전역 모듈)이다 — 여기서 쓰는 `.lookup`·
    `.RULE_PREFIXES` 는 전부 그 레지스트리를 거친다."""
    if isinstance(id_arg, ast.Constant) and isinstance(id_arg.value, str):
        return _validate_literal_id(id_arg.value, relpath, line, context, registry)
    if isinstance(id_arg, ast.JoinedStr):
        return _validate_fstring_id(id_arg, relpath, line, context, registry)
    if isinstance(id_arg, ast.Name):
        return _validate_name_id(id_arg.id, relpath, line, context, registry)
    return (
        [
            Violation(
                relpath, line, "id_form_unsupported",
                f"{relpath}:{line} — {context} 의 id 가 리터럴·f-string·허용 변수 중 어느 것도 "
                f"아니다({_unparse(id_arg)}) — 리터럴 문자열이나 등록된 접두로 시작하는 "
                f"f-string 으로 바꿔라.",
            )
        ],
        None,
    )


def _validate_literal_id(rid: str, relpath: str, line: int, context: str, registry) -> tuple[list[Violation], object | None]:
    spec = registry.lookup(rid)
    if spec is None:
        return (
            [
                Violation(
                    relpath, line, "id_unregistered",
                    f"{relpath}:{line} — {context} 가 id=\"{rid}\" 를 쓰는데 "
                    f"engine_contract/rules.py 의 RULES·RULE_PREFIXES 어디에도 없다 — "
                    f"RuleSpec(id=\"{rid}\", ...) 를 추가하거나 기존 id 로 바꿔라.",
                )
            ],
            None,
        )
    return [], spec


def _validate_fstring_id(
    node: ast.JoinedStr, relpath: str, line: int, context: str, registry,
) -> tuple[list[Violation], object | None]:
    leading = ""
    for v in node.values:
        if isinstance(v, ast.Constant):
            leading += v.value
        else:
            break
    matched = next((p for p in registry.RULE_PREFIXES if leading.startswith(p)), None)
    if matched is None:
        return (
            [
                Violation(
                    relpath, line, "id_fstring_prefix_unregistered",
                    f"{relpath}:{line} — {context} 의 f-string id(앞부분 {leading!r})가 "
                    f"RULE_PREFIXES 의 어느 접두로도 시작하지 않는다 — RULE_PREFIXES 에 접두를 "
                    f"추가하거나 등록된 접두로 시작하도록 고쳐라.",
                )
            ],
            None,
        )
    return [], registry.RULE_PREFIXES[matched]


def _validate_name_id(
    varname: str, relpath: str, line: int, context: str, registry,
) -> tuple[list[Violation], object | None]:
    if (relpath, varname) not in DYNAMIC_ID_VARS:
        return (
            [
                Violation(
                    relpath, line, "id_name_not_allowed",
                    f"{relpath}:{line} — {context} 가 리터럴이 아닌 변수 {varname} 을 id 로 "
                    f"쓰는데 DYNAMIC_ID_VARS 허용 목록에 없다 — 리터럴/f-string 으로 바꾸거나, "
                    f"그 변수가 등록 접두로 시작하는 f-string 만 반환하는 함수에서만 오는지 "
                    f"확인한 뒤 DYNAMIC_ID_VARS 에 추가하라.",
                )
            ],
            None,
        )
    # fixed_assignment:* 전용으로 실측됨(§DYNAMIC_ID_VARS). 대입 형태는 _check_dynamic_id_vars 가 본다.
    return [], registry.RULE_PREFIXES["fixed_assignment:"]


def _check_wrapper_ids(modules: list[_ModuleInfo], registry) -> list[Violation]:
    out: list[Violation] = []
    for info in modules:
        for node in ast.walk(info.tree):
            if not isinstance(node, ast.Call):
                continue
            wname = _resolve_wrapper_call(node, info)
            if wname is None:
                continue
            out.extend(_check_one_wrapper_call(info.relpath, node, wname, registry))
    return out


def _check_one_wrapper_call(relpath: str, node: ast.Call, wname: str, registry) -> list[Violation]:
    min_args = 2 if wname in ADD_WRAPPERS else 3
    if len(node.args) < min_args:
        return [
            Violation(
                relpath, node.lineno, "wrapper_arity",
                f"{relpath}:{node.lineno} — {wname}(...) 호출 인자가 {min_args}개보다 적다.",
            )
        ]
    id_arg = node.args[1]
    violations, spec = _validate_id_expr(id_arg, relpath=relpath, line=node.lineno, context=f"{wname}(...)", registry=registry)
    if spec is None:
        return violations
    out = list(violations)
    out.extend(_check_strength_match(relpath, node.lineno, wname, spec.id, spec))
    if wname in CALL_WRAPPERS:
        out.extend(_check_call_wrapper_method_name(relpath, node, wname))
    return out


def _check_call_wrapper_method_name(relpath: str, node: ast.Call, wname: str) -> list[Violation]:
    """hard_call/aux_call 의 메서드 인자(3번째)는 CONSTRAINT_NAMES 안의 문자열 리터럴이어야
    한다 — 가정(assumption)·힌트·결정전략 메서드나 비-리터럴 식을 거기 넣으면, 가정 관리를
    {ASSUMPTION_HOME} 한 곳에 모은다는 규칙(§가정 위치)이 래퍼 경유로 우회된다(r22 — 예:
    `hard_call(m, "forbidden_shift", "AddAssumptions", [...])`)."""
    method_arg = node.args[2]
    if (
        isinstance(method_arg, ast.Constant)
        and isinstance(method_arg.value, str)
        and method_arg.value in CONSTRAINT_NAMES
    ):
        return []
    return [
        Violation(
            relpath, node.lineno, "wrapper_call_method_not_constraint",
            f"{relpath}:{node.lineno} — {wname}(...) 의 메서드 인자가 제약 메서드"
            f"(CONSTRAINT_NAMES) 문자열 리터럴이 아니다({_unparse(method_arg)}) — 가정·힌트·"
            f"결정전략 메서드는 {ASSUMPTION_HOME} 한 곳에서만 쓴다. 리터럴 제약 메서드 "
            f"이름으로 바꿔라.",
        )
    ]


def _check_strength_match(relpath: str, line: int, wname: str, matched: str, spec) -> list[Violation]:
    is_aux_wrapper = wname in _AUX_WRAPPERS
    is_aux_spec = spec.strength == "AUX"
    if is_aux_wrapper == is_aux_spec:
        return []
    want = "AUX" if is_aux_wrapper else "AUX 가 아닌(HARD/HARD_AUTO_RELAX)"
    return [
        Violation(
            relpath, line, "strength_mismatch",
            f"{relpath}:{line} — {wname}(...) 가 id=\"{matched}\"(strength={spec.strength}) 를 "
            f"쓰는데 래퍼 종류와 안 맞는다 — hard_add/hard_call 은 AUX 가 아닌 id, "
            f"aux_add/aux_call 은 AUX id 만 받는다. {want} 가 되도록 래퍼나 rules.py 의 "
            f"strength 를 고쳐라.",
        )
    ]


def _check_add_hard_patterns(modules: list[_ModuleInfo], registry) -> list[Violation]:
    """add_hard(및 그 별칭·모듈 속성 호출) 호출의 meta[\"pattern\"] — 모든 app/ 모듈
    대상(지시 8)."""
    out: list[Violation] = []
    for info in modules:
        parents = _parent_map(info.tree)
        for node in ast.walk(info.tree):
            if isinstance(node, ast.Call) and _resolve_add_hard_call(node, info):
                out.extend(_check_one_add_hard_call(info.relpath, info.tree, node, registry, parents))
    return out


def _parent_map(tree: ast.Module) -> dict[int, ast.AST]:
    """노드 id → 부모 노드. 모듈 전체에서 한 번만 만들어 재사용한다(함수 경계 탐색용)."""
    parents: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node
    return parents


def _enclosing_function_or_module(tree: ast.Module, node: ast.AST, parents: dict[int, ast.AST]) -> ast.AST:
    """node 를 감싸는 가장 안쪽 FunctionDef/AsyncFunctionDef. 없으면 모듈 자체(scope 삼음)."""
    cur = parents.get(id(node))
    while cur is not None:
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return cur
        cur = parents.get(id(cur))
    return tree


def _dict_pattern_value(dict_node: ast.Dict) -> tuple[ast.expr | None, bool]:
    """dict 리터럴의 "pattern" 값과 `**` 펼침 존재 여부. 같은 리터럴 키가 여럿이면
    **마지막** 값을 쓴다(실제 파이썬 dict 리터럴 의미 — p14c, 전에는 `next()` 로 첫
    매치를 써서 틀렸다). `**` 펼침(키가 None)이 있으면 그 안에 숨은 "pattern" 을 정적으로
    알 수 없으므로 별도로 호출자에게 알린다(p14c)."""
    has_spread = any(k is None for k in dict_node.keys)
    value: ast.expr | None = None
    for k, v in zip(dict_node.keys, dict_node.values):
        if isinstance(k, ast.Constant) and k.value == "pattern":
            value = v
    return value, has_spread


def _is_pattern_key(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value == "pattern"


def _update_call_pattern_value(node: ast.Call) -> tuple[ast.expr | None, bool]:
    """`meta.update(pattern=X)` 또는 `meta.update({\"pattern\": X})` 의 X, `**` 펼침 인자
    존재 여부(있으면 안에 숨은 "pattern" 을 정적으로 알 수 없다, p14c 이웃 형태)."""
    has_spread = any(kw.arg is None for kw in node.keywords)
    for kw in node.keywords:
        if kw.arg == "pattern":
            return kw.value, has_spread
    for arg in node.args:
        if isinstance(arg, ast.Dict):
            v, arg_spread = _dict_pattern_value(arg)
            if v is not None:
                return v, has_spread or arg_spread
    return None, has_spread


def _resolve_pattern_writes(
    scope: ast.AST, varname: str, before_line: int, relpath: str, call_line: int,
) -> tuple[ast.expr | None, list[Violation]]:
    """`scope`(add_hard 호출을 감싼 가장 안쪽 함수, 없으면 모듈 전체 — x14 보강 2026-10-13)
    **안에서만** varname 에 대한 "pattern" 쓰기 이벤트를 모아 호출 줄 이전까지 줄 번호 순
    가장 나중 것을 쓴다. 전에는 모듈 전체를 봐서, 같은 변수명을 쓰는 다른 함수의 이벤트를
    엉뚱하게 가져왔다(r16 — grade_constraints.py `_add_minimum_constraints`/
    `_add_maximum_constraints` 가 둘 다 지역변수 `_meta` 를 쓴다).

    다루지 못하는 쓰기 형태(dict 리터럴이 아닌 재대입·AugAssign·다른 이름으로의 별칭·
    update()/구독 대입이 아닌 메서드 호출·`**` 펼침)를 만나면 그 뒤로 이벤트를 더 믿지
    않고 **보수적으로 거부**한다(r17·r18·r19·p14a·p14b·p14c — "값을 추적할 수 없다"는
    사실 자체를 위반으로 알린다. 조용히 넘어가거나 엉뚱한 이전 값으로 fallback 하지 않는다)."""
    events: list[tuple[int, ast.expr]] = []
    violations: list[Violation] = []

    def reject(line: int, why: str) -> None:
        violations.append(
            Violation(
                relpath, call_line, "add_hard_pattern_untrackable",
                f"{relpath}:{call_line} — add_hard(...) 의 meta 변수 {varname} 을 {line}줄에서 "
                f"{why} — 정적으로 최종 \"pattern\" 값을 신뢰할 수 없다. {varname} 을 dict "
                f"리터럴로만 만들고, 갱신은 구독 대입(`{varname}[\"pattern\"] = ...`)이나 "
                f"`{varname}.update(...)`(`**` 없이)로만 하라.",
            )
        )

    for node in ast.walk(scope):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and node.lineno <= before_line:
            tgt = node.targets[0]
            if isinstance(tgt, ast.Name) and tgt.id == varname:
                if isinstance(node.value, ast.Dict):
                    v, has_spread = _dict_pattern_value(node.value)
                    if has_spread:
                        reject(node.lineno, "dict 리터럴에 `**` 펼침이 있다(p14c)")
                    elif v is not None:
                        events.append((node.lineno, v))
                else:
                    reject(node.lineno, "dict 리터럴이 아닌 값으로 재대입됐다(r17)")
            elif isinstance(tgt, ast.Name) and isinstance(node.value, ast.Name) and node.value.id == varname:
                reject(node.lineno, f"다른 이름({tgt.id})에 대입돼 별칭이 생겼다(p14a)")
            elif (
                isinstance(tgt, ast.Subscript)
                and isinstance(tgt.value, ast.Name)
                and tgt.value.id == varname
                and _is_pattern_key(tgt.slice)
            ):
                events.append((node.lineno, node.value))
        elif (
            isinstance(node, ast.AugAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == varname
            and node.lineno <= before_line
        ):
            reject(node.lineno, "복합 대입 연산자(AugAssign, 예: `|=`)로 바뀌었다(p14b)")
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == varname
            and node.lineno <= before_line
        ):
            if node.func.attr != "update":
                reject(node.lineno, f"{node.func.attr}(...) — update()·구독 대입이 아닌 메서드로 바뀌었다(r18)")
            else:
                v, has_spread = _update_call_pattern_value(node)
                if has_spread:
                    reject(node.lineno, "update() 에 `**` 펼침 인자가 있다(r19)")
                elif v is not None:
                    events.append((node.lineno, v))
    if violations:
        return None, violations
    if not events:
        return None, []
    events.sort(key=lambda e: e[0])
    return events[-1][1], []


def _check_one_add_hard_call(
    relpath: str, tree: ast.Module, node: ast.Call, registry, parents: dict[int, ast.AST],
) -> list[Violation]:
    meta_kw = next((kw for kw in node.keywords if kw.arg == "meta"), None)
    if meta_kw is None:
        return [
            Violation(
                relpath, node.lineno, "add_hard_meta_missing",
                f"{relpath}:{node.lineno} — add_hard(...) 호출에 meta 딕셔너리 리터럴이 없다 "
                f"— pattern 키로 규칙 id 를 등록하라(설계서 §2.6 A1 C3 전제).",
            )
        ]
    pattern_expr, untrackable = _resolve_effective_pattern(tree, meta_kw.value, node.lineno, relpath, parents)
    if untrackable:
        return untrackable
    if pattern_expr is None:
        return [
            Violation(
                relpath, node.lineno, "add_hard_pattern_missing",
                f"{relpath}:{node.lineno} — add_hard(...) 의 meta 에 \"pattern\" 키가 없다 — "
                f"추가하라.",
            )
        ]
    violations, _matched = _validate_id_expr(
        pattern_expr, relpath=relpath, line=node.lineno, context="add_hard(...) 의 meta[\"pattern\"]",
        registry=registry,
    )
    return violations


def _resolve_effective_pattern(
    tree: ast.Module, meta_value: ast.expr, before_line: int, relpath: str, parents: dict[int, ast.AST],
) -> tuple[ast.expr | None, list[Violation]]:
    """add_hard(...) 의 meta 가 호출 시점에 최종적으로 들고 가는 "pattern" 값 — 리터럴
    `meta={...}` 는 그 자리에서, 변수 바인딩(`meta=_meta`)은 호출을 감싼 함수 범위 안에서만
    (x14·r16 보강)."""
    if isinstance(meta_value, ast.Dict):
        v, has_spread = _dict_pattern_value(meta_value)
        if has_spread:
            return None, [
                Violation(
                    relpath, before_line, "add_hard_pattern_untrackable",
                    f"{relpath}:{before_line} — add_hard(...) 의 meta dict 리터럴에 `**` 펼침이 "
                    f"있다 — 정적으로 최종 \"pattern\" 값을 신뢰할 수 없다(p14c). 펼침 없이 "
                    f"리터럴 키로만 써라.",
                )
            ]
        return v, []
    if not isinstance(meta_value, ast.Name):
        return None, []
    scope = _enclosing_function_or_module(tree, meta_value, parents)
    return _resolve_pattern_writes(scope, meta_value.id, before_line, relpath, before_line)


# ── 6-보강) 변수 id(_fixed_pattern) 의 모든 바인딩 형태 검증(x09~x12) ────────


_PRODUCER_NAME = "_fixed_pattern_from_source"
#: Python 3.10+ match 패턴 바인딩 노드 — 없는 런타임(3.9 이하)에서도 안전하게 빈 튜플.
_MATCH_NODE_TYPES: tuple[type, ...] = tuple(
    t for t in (getattr(ast, "MatchAs", None), getattr(ast, "MatchStar", None), getattr(ast, "MatchMapping", None))
    if t is not None
)


def _check_dynamic_id_vars(modules: list[_ModuleInfo]) -> list[Violation]:
    by_path = {info.relpath: info for info in modules}
    out: list[Violation] = []
    for relpath, varname in DYNAMIC_ID_VARS:
        info = by_path.get(relpath)
        if info is None:
            continue
        out.extend(_check_one_dynamic_id_var(info, varname))
    return out


def _is_pure_producer_call(value: ast.expr | None, producer_names: set[str]) -> bool:
    return (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id in producer_names
    )


def _match_pattern_bound_name(node: ast.AST) -> str | None:
    """MatchAs.name·MatchStar.name·MatchMapping.rest 중 이 노드가 바인딩하는 이름."""
    for attr in ("name", "rest"):
        bound = getattr(node, attr, None)
        if isinstance(bound, str):
            return bound
    return None


def _binding_site_description(parent: ast.AST | None, node: ast.AST) -> str:
    """불순으로 판정된 바인딩 자리의 사람이 읽을 설명(열거가 아니라 부모 노드 종류로
    분류한다 — r13·r14·r15·p09f 보강, 2026-10-13)."""
    if isinstance(parent, ast.Assign):
        if len(parent.targets) != 1:
            return "단일 대입이 아닌 형태(다중 대입)로 바인딩됐다"
        return "_fixed_pattern_from_source(...) 호출이 아닌 값으로 대입됐다"
    if isinstance(parent, (ast.Tuple, ast.List, ast.Starred)):
        return "튜플/리스트 풀기로 바인딩됐다(각 원소의 순수성을 정적으로 증명할 수 없다)"
    if isinstance(parent, ast.AnnAssign):
        return "타입주석 대입(AnnAssign)으로 바인딩됐다"
    if isinstance(parent, ast.NamedExpr):
        return "walrus(:=)로 바인딩됐다"
    if isinstance(parent, ast.AugAssign):
        return "복합 대입 연산자(AugAssign)로 바인딩됐다"
    if isinstance(parent, ast.For):
        return "for 루프 대상으로 바인딩됐다(p09f)"
    if isinstance(parent, ast.withitem):
        return "with...as 로 바인딩됐다(r15)"
    if isinstance(parent, ast.comprehension):
        return "컴프리헨션 대상으로 바인딩됐다"
    return "정적으로 순수성을 증명할 수 없는 자리에서 바인딩됐다"


def _fixed_pattern_binding_sites(
    tree: ast.Module, varname: str, producer_names: set[str], parents: dict[int, ast.AST],
) -> list[tuple[int, bool, str]]:
    """varname 이 바인딩되는 **모든** 자리(줄, 순수한가, 설명) — 바인딩 "형태"를 목록으로
    나열하지 않고, 파이썬이 그 이름을 묶는 자리 자체(Store/Del Name·함수/람다 매개변수·
    import 별칭·except...as·global/nonlocal·match 패턴)를 전수로 모은다(x09~x12 +
    r13·r14·r15·p09f 보강, 2026-10-13 — 전에는 "형태 목록"에 없는 바인딩(컴프리헨션·
    for·with·except·import·global/nonlocal·match)이 전부 안 보였다). "단일 Assign 이고
    값이 생산자(`_fixed_pattern_from_source`) 직접 호출"인 것만 순수하다 — 그 나머지는
    (AnnAssign·walrus·for·with·튜플풀기·함수매개변수 포함) 전부 불순으로 본다."""
    sites: list[tuple[int, bool, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)) and node.id == varname:
            parent = parents.get(id(node))
            if (
                isinstance(parent, ast.Assign)
                and len(parent.targets) == 1
                and parent.targets[0] is node
                and _is_pure_producer_call(parent.value, producer_names)
            ):
                sites.append((node.lineno, True, "_fixed_pattern_from_source(...) 직접 호출로 대입됐다"))
            else:
                sites.append((node.lineno, False, _binding_site_description(parent, node)))
        elif isinstance(node, ast.arg) and node.arg == varname:
            sites.append((node.lineno, False, "함수/람다 매개변수로 바인딩됐다(호출자가 주는 값의 순수성을 정적으로 증명할 수 없다)"))
        elif isinstance(node, ast.alias) and (node.asname or node.name) == varname:
            sites.append((getattr(node, "lineno", 1), False, "import 별칭으로 바인딩됐다"))
        elif isinstance(node, ast.ExceptHandler) and node.name == varname:
            sites.append((node.lineno, False, "except...as 로 바인딩됐다"))
        elif isinstance(node, (ast.Global, ast.Nonlocal)) and varname in node.names:
            sites.append((node.lineno, False, "global/nonlocal 선언으로 바인딩됐다"))
        elif _MATCH_NODE_TYPES and isinstance(node, _MATCH_NODE_TYPES) and _match_pattern_bound_name(node) == varname:
            sites.append((node.lineno, False, "match 패턴으로 바인딩됐다"))
    return sites


def _check_one_dynamic_id_var(info: _ModuleInfo, varname: str) -> list[Violation]:
    out = []
    parents = _parent_map(info.tree)
    producer_names = _fixed_pattern_producer_names(info.tree)
    for line, pure, verb in _fixed_pattern_binding_sites(info.tree, varname, producer_names, parents):
        if not pure:
            out.append(
                Violation(
                    info.relpath, line, "dynamic_id_var_not_pure",
                    f"{info.relpath}:{line} — 허용 변수 id {varname} 이 {verb} — 이 변수를 id "
                    f"로 쓰는 것은 그 함수가 늘 'fixed_assignment:' 로 시작하는 f-string만 "
                    f"반환할 때만 유효하다. 바인딩을 그 함수의 단순 호출로만 제한하라.",
                )
            )
    out.extend(_check_producer_returns(info, producer_names))
    out.extend(_check_producer_name_integrity(info, parents))
    return out


def _fixed_pattern_producer_names(tree: ast.Module) -> set[str]:
    return {
        n.name
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == _PRODUCER_NAME
    }


def _check_producer_name_integrity(info: _ModuleInfo, parents: dict[int, ast.AST]) -> list[Violation]:
    """생산자 이름(`_fixed_pattern_from_source`) 자체가 FunctionDef 로 **정확히 한 번만**
    바인딩되는지 본다(r13 — 원래 `def` 가 물리적으로 그대로 남아 있어도, 그 뒤에 같은
    이름으로 `lambda`/다른 값을 재대입하면 런타임에는 그 재대입이 이긴다. 이름 집합만
    보는 "순수 호출" 판정은 FunctionDef 가 소스에 남아 있기만 하면 그 뒤의 재대입을
    전혀 못 본다 — 그래서 이름 자체의 바인딩 개수·종류를 따로 센다)."""
    sites = _fixed_pattern_binding_sites(info.tree, _PRODUCER_NAME, set(), parents)
    functiondef_lines = {
        n.lineno for n in ast.walk(info.tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == _PRODUCER_NAME
    }
    if not functiondef_lines:
        return []  # 이 모듈에 생산자 정의 자체가 없다 — 다른 검사(사용되지 않음)의 몫.
    extra = [(line, verb) for line, _pure, verb in sites if line not in functiondef_lines]
    out = []
    for line, verb in extra:
        out.append(
            Violation(
                info.relpath, line, "dynamic_id_producer_rebound",
                f"{info.relpath}:{line} — 생산자 {_PRODUCER_NAME} 이 {verb} — 원래 함수 정의가 "
                f"소스에 남아 있어도 이렇게 재대입되면 런타임에는 이 값이 이긴다. 이 이름은 "
                f"FunctionDef 로 정확히 한 번만 바인딩돼야 한다.",
            )
        )
    if len(functiondef_lines) > 1:
        for line in sorted(functiondef_lines)[1:]:
            out.append(
                Violation(
                    info.relpath, line, "dynamic_id_producer_rebound",
                    f"{info.relpath}:{line} — 생산자 {_PRODUCER_NAME} 가 같은 모듈에 두 번 "
                    f"이상 def 로 정의됐다 — 어느 정의가 쓰이는지 정적으로 보장할 수 없다.",
                )
            )
    return out


def _check_producer_returns(info: _ModuleInfo, producer_names: set[str]) -> list[Violation]:
    out = []
    for fn in ast.walk(info.tree):
        if not (isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name in producer_names):
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.Return) and node.value is not None:
                if not _returns_fixed_assignment_fstring(node.value):
                    out.append(
                        Violation(
                            info.relpath, node.lineno, "dynamic_id_producer_not_pure",
                            f"{info.relpath}:{node.lineno} — _fixed_pattern_from_source 가 "
                            f"'fixed_assignment:' 로 시작하지 않는 값을 반환할 수 있다 — "
                            f"이 함수의 모든 return 은 그 접두로 시작하는 f-string 이어야 한다.",
                        )
                    )
    return out


def _returns_fixed_assignment_fstring(value: ast.expr) -> bool:
    if not isinstance(value, ast.JoinedStr) or not value.values:
        return False
    first = value.values[0]
    return isinstance(first, ast.Constant) and isinstance(first.value, str) and first.value.startswith("fixed_assignment:")


def _returns_fixed_assignment_fstring(value: ast.expr) -> bool:
    if not isinstance(value, ast.JoinedStr) or not value.values:
        return False
    first = value.values[0]
    return isinstance(first, ast.Constant) and isinstance(first.value, str) and first.value.startswith("fixed_assignment:")


# ── 9) 죽은 id 없음 ──────────────────────────────────────────────────────


def _check_no_dead_ids(modules: list[_ModuleInfo], registry) -> list[Violation]:
    used_ids: set[str] = set()
    for info in modules:
        used_ids |= _used_ids_in_module(info)
    out: list[Violation] = []
    for rid in registry.RULES:
        if rid not in used_ids:
            out.append(
                Violation(
                    "services/engine_contract/rules.py", 1, "dead_rule_id",
                    f"services/engine_contract/rules.py:1 — RuleSpec id=\"{rid}\" 가 app/ "
                    f"어디에서도 쓰이지 않는다(죽은 id) — 실제 호출의 id 를 맞추거나 "
                    f"rules.py 에서 지워라.",
                )
            )
    for prefix in registry.RULE_PREFIXES:
        if not any(u.startswith(prefix) for u in used_ids):
            out.append(
                Violation(
                    "services/engine_contract/rules.py", 1, "dead_rule_prefix",
                    f"services/engine_contract/rules.py:1 — RULE_PREFIXES 의 접두 "
                    f"\"{prefix}\" 가 app/ 어디에서도 쓰이지 않는다(죽은 접두) — 실제 호출의 "
                    f"id 를 맞추거나 rules.py 에서 지워라.",
                )
            )
    return out


def _used_ids_in_module(info: _ModuleInfo) -> set[str]:
    used: set[str] = set()
    parents = _parent_map(info.tree)
    for node in ast.walk(info.tree):
        if not isinstance(node, ast.Call):
            continue
        wname = _resolve_wrapper_call(node, info)
        if wname is not None and len(node.args) >= 2:
            used |= _literal_ids_from_expr(node.args[1], info.relpath)
            continue
        if _resolve_add_hard_call(node, info):
            meta_kw = next((kw for kw in node.keywords if kw.arg == "meta"), None)
            if meta_kw is not None:
                pattern_expr, _untrackable = _resolve_effective_pattern(
                    info.tree, meta_kw.value, node.lineno, info.relpath, parents,
                )
                if pattern_expr is not None:
                    used |= _literal_ids_from_expr(pattern_expr, info.relpath)
    return used


def _literal_ids_from_expr(id_arg: ast.expr, relpath: str) -> set[str]:
    if isinstance(id_arg, ast.Constant) and isinstance(id_arg.value, str):
        return {id_arg.value}
    if isinstance(id_arg, ast.JoinedStr):
        leading = ""
        for v in id_arg.values:
            if isinstance(v, ast.Constant):
                leading += v.value
            else:
                break
        return {leading} if leading else set()
    if isinstance(id_arg, ast.Name) and (relpath, id_arg.id) in DYNAMIC_ID_VARS:
        return {"fixed_assignment:placeholder"}
    return set()


# ── 레지스트리 로드(§지시 11 — "17": scan(app_root) 를 app_root 자신에 밀폐) ──────


def _load_registry(app_root: Path):
    """app_root 자신의 `services/engine_contract/rules.py` 를 경로로 로드한다.

    모듈 top-level 의 `from services.engine_contract.rules import ...` 는 sys.path
    전역 상태를 본다 — `scan(app_root)` 에 다른 app_root(예: 다른 버전의 사본)를 줘도
    항상 "지금 프로세스에 깔린" 레지스트리로 판정해버려 `app_root` 인자가 장식이 된다
    (2026-10-13 지시 "17"). 그 파일이 없는 합성 테스트 트리(tmp_path)는 전역 모듈로
    돌아간다 — 밀폐 자체를 보는 테스트는 그 파일을 tmp_path 에 직접 둔다."""
    rules_path = app_root / "services" / "engine_contract" / "rules.py"
    if not rules_path.is_file():
        return _global_rules_module
    spec = importlib.util.spec_from_file_location("_rule_audit_sandboxed_rules", rules_path)
    if spec is None or spec.loader is None:
        return _global_rules_module
    module = importlib.util.module_from_spec(spec)
    # frozen dataclass(§RuleSpec) 는 타입 해석 때 `sys.modules[cls.__module__]` 를 되짚는다
    # (PEP 563 지연 평가) — 등록 없이 exec 하면 AttributeError 로 깨진다.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


# ── 공개 진입점 ───────────────────────────────────────────────────────────


def _load_modules(app_root: Path) -> tuple[list[_ModuleInfo], list[Violation]]:
    modules: list[_ModuleInfo] = []
    parse_violations: list[Violation] = []
    for path in sorted(app_root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        relpath = path.relative_to(app_root).as_posix()
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=relpath)
        except SyntaxError as e:
            # x18 — 파싱 못 하는 모듈을 조용히 건너뛰면 "감사가 이 파일을 못 봤다"는
            # 사실 자체가 사라진다. 건너뛰지 않고 위반으로 보고한다.
            parse_violations.append(
                Violation(
                    relpath, e.lineno or 1, "parse_error",
                    f"{relpath}:{e.lineno or 1} — 파일을 AST 로 파싱할 수 없다({e.msg}) — "
                    f"감사가 이 파일을 건너뛰지 않도록 문법을 고쳐라.",
                )
            )
            continue
        info = _ModuleInfo(relpath=relpath, tree=tree, source=source)
        info.module_aliases = _module_import_aliases(tree, relpath)
        info.wrapper_aliases = _name_alias_chain(tree, relpath, WRAPPER_NAMES, RULE_TAG_MODULE)
        add_hard_aliases = _name_alias_chain(tree, relpath, frozenset({"add_hard"}), HARD_ASSUMPTION_MODULE)
        if not any(v == "add_hard" for v in add_hard_aliases.values()) and relpath == HARD_ASSUMPTION_MODULE_PATH:
            add_hard_aliases = {"add_hard": "add_hard"}
        info.add_hard_aliases = add_hard_aliases
        info.container_names = _collect_container_names(info)
        info.imports_cp_model = _imports_cp_model(tree)
        info.model_names = _collect_model_names(info)
        modules.append(info)
    return modules, parse_violations


def scan(app_root: Path) -> list[Violation]:
    """`app_root`(앱 패키지 루트, 예: roster-back/app) 를 스캔해 위반 목록을 돌려준다.

    읽기 전용·순수 함수다 — DB·ortools 모델을 만들거나 실행하지 않는다. id 레지스트리는
    `app_root` 자신의 rules.py 에서 로드한다(§지시 11 — "17").
    """
    modules, violations = _load_modules(app_root)
    registry = _load_registry(app_root)
    violations.extend(_check_module_call_counts(modules))
    violations.extend(_check_assumption_locations(modules))
    violations.extend(_check_bypass_forms(modules))
    violations.extend(_check_missing_rule_tag_import(modules))
    violations.extend(_check_wrapper_ids(modules, registry))
    violations.extend(_check_add_hard_patterns(modules, registry))
    violations.extend(_check_dynamic_id_vars(modules))
    violations.extend(_check_no_dead_ids(modules, registry))
    return violations


# ── §모호성 판정 실측(2026-10-11·2026-10-13 재실측, 이 저장소 app/ 전체 AST 스캔) ──
#
# dir(cp_model.CpModel)(ortools 9.12.4544) 에서 add/Add 로 시작하는 이름 67개를 모으고,
# 힌트(AddHint·add_hint) 2 · 결정전략(AddDecisionStrategy·add_decision_strategy) 2 ·
# 가정(AddAssumption·AddAssumptions·add_assumption·add_assumptions) 4 를 뺀 제약 메서드
# 59개로 app/ 전체(루프 스캔, __pycache__ 제외)의 모든 `<recv>.<name>(...)` 호출을
# 받아 수신자 텍스트를 모았다. 결과:
#   - "add"(소문자 단독)만 수신자가 압도적으로 비모델(set/list/dict/db 세션 등, 381건
#     중 모델(m/model) 0건) — set.add 와 이름이 같다. → 모호한 이름으로 분류.
#   - "Add"(대문자 단독)는 124건 전부 모델(m 119·model 5) — add_map_domain 처럼 소문자만
#     있는 별칭도 포함해 그 밖의 58개 이름은 app/ 안에 비모델 수신자 사례가 0건(이름
#     자체가 쓰이지 않는 경우 포함) → 모호하지 않은 이름으로 분류(§2 번 규칙).
#   - AddAssumptions 의 비-home 수신자는 'model'(mcs.py)·관련 없음(core_guided.py 는
#     AddAssumption, 수신자 'm') — 전부 cp_model.CpModel 매개변수로 타입주석된 식이다.
#
# §기본값-의심 스코프(2026-10-13, x01·x02·x08·x16 보강 근거) — TARGET_MODULES(7) ∪
# EXEMPT(8) ∪ "cp_model/ortools 를 import 하는 모듈" 의 합집합은 실측 16개 파일이다
# (TARGET·EXEMPT 가 겹치지 않는 cp_model importer 는 mcs.py 1개뿐). 이 16개 파일
# 전체에서 ".add(" 로 끝나는 호출(대문자 Add 변형 제외) 을 전수 grep 했을 때 **0건**
# (이미 "Add" 로 잡히는 호출을 빼면 완전히 없다) — 그래서 이 스코프 안에서는 "add"
# 수신자가 컨테이너(set() 등)로 증명되지 않으면 전부 모델로 의심해도 새 과탐이 생기지
# 않는다. 이 스코프를 getattr 비리터럴(x06)·__getattribute__(x07) 검사에는 **쓰지
# 않는다** — 그 검사들은 attr 이름이 "add" 로 고정이 아니라서(`getattr(x, 아무이름)`),
# 같은 16개 파일 안에도 비-모델 수신자의 정당한 동적 getattr 실사용이 있다(실측:
# `objective_terms.py:497` `getattr(nu, f"{prefix}_exact", None)`,
# `monthly_limit_constraints.py:72` 등 — 수신자 'nu' 는 간호사 객체, 둘 다 EXEMPT·
# TARGET_MODULES 안이다). 그래서 그 두 검사는 좁은 model_names 집합만 쓴다
# (`_is_recognized_model_receiver`).
#
# 측정된 EXEMPT·ASSUMPTION_BASELINE_ELSEWHERE 호출 수(+ EXEMPT 의 정규화 호출 지문
# 다중집합 해시, x13)와 TARGET_MODULES 의 0 은 이 실측과
# tests/engine_contract/test_rule_audit.py 의 `test_scan_real_app_zero_violations`
# 로 고정된다 — 그 테스트가 실패하면 이 주석의 전제가 깨졌다는 뜻이다.
#
# ★2026-10-14 추가 실측(ortools 9.15.6755, uv.lock 고정판): 위 "67개"는 9.12.4544 에서
# `CpModel` **클래스**에 직접 정의돼 있던 이름이다. 9.15 는 클래스에 snake_case 34개만
# 있고, 대문자(pre-pep8) 34개(Add·AddExactlyOne·AddHint… + 9.12 에 없던 AddMapDomain
# 1개)는 `CpModel.__init__` 이 `_add_pre_pep8_methods()`(cp_model.py:1684-1716)로
# **인스턴스**에만 단다 — `dir(cp_model.CpModel())` 전수로 확인(68 = 9.12 의 67 ∪
# {AddMapDomain}). 그래서 `dir(CpModel)` 한 곳만 보면 9.15 에서 대문자 제약 메서드가
# 통째로 빠져 이름표 없는 `m.Add`·`m.AddExactlyOne` 호출을 못 잡는다(감사가 사실상
# 꺼진다 — EXEMPT 실측 수도 43→0·2→0 으로 "줄었다"고 오판했다). 수정은
# `_collect_add_names()`(위) — 클래스+인스턴스+변환 규칙+고정 안전망 네 출처를
# 합집합해 판과 무관하게 모은다. 이 변경으로 EXEMPT·ASSUMPTION_BASELINE_ELSEWHERE 의
# 기존 수치·해시는 그대로 유지된다(9.12 때 이미 대문자 이름까지 포함해 측정된 값이라,
# 9.15 에서도 같은 값으로 다시 맞아떨어진다 — 실측 확인됨).
