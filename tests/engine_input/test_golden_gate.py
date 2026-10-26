"""골든 문지기(`golden_gate`) 테스트 — D29(2026-10-20 · 골든 JSON 은 커밋하지 않는 로컬 전용).

네 부분
  1) 문지기 단위 테스트 — `tmp_path` 에 임시 `GoldenSet` 을 만들어 판정표 그대로 건너뜀·실패·통과를 본다(모듈 상수는 건드리지
     않는다). 이 브랜치 표준 실행은 `REQUIRE_GOLDEN=1` 이라, 단위 테스트는 문지기가 읽는 플래그를 먼저 지운다(`clean_env`).
  2) 이 브랜치의 파일 수 단언 — `ALL_GOLDEN_SETS` 각각에 문지기를 부른다. 로컬(골든 있음)에서는 통과하고, 골든이 없는
     체크아웃에서는 D29 사유로 건너뛴다 — 사람이 결과에서 바로 보는 집계다. 옛 상수가 정본의 별칭인지도 본다.
  3) 실제 읽는 함수가 문지기를 거치는지(동적) — `golden.GOLDEN`·`model_fingerprint.GOLDEN_MODEL` 을 빈 임시 세트로 바꿔
     끼우고 `read_golden`·`read_golden_model` 이 D29 사유로 건너뛰는지 본다(두 함수는 부를 때마다 모듈 전역을 읽는다).
  4) 정적 감사(AST) — `tests/**` 의 함수(중첩·메서드·lambda 포함) 가운데 골든을 가리키는 이름·문자열과 읽기 연산이 함께 있는
     함수는 docstring 다음 **첫 문장**에서 그 세트의 `require_golden(...)` 을 불러야 한다. 모듈 수준(import 때 평가되는 문장 ·
     데코레이터 · 기본값 · 클래스 본문)에서는 골든을 읽지도, 문지기를 거치는 함수를 부르지도 않는다 — 골든이 없는 체크아웃에서
     수집 오류가 된다(`allow_module_level=True` 로 부르는 함수만 된다). 예외는 이유와 함께 `AUDIT_EXCEPTIONS` 에 두고, 예외가
     실제로 걸리는지(죽은 예외 금지)와 감사가 위반을 실제로 잡는지(가짜 소스 자기 검사)도 단언한다.
"""

from __future__ import annotations

import ast
import functools
import importlib
import os
import re
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

import pytest

from . import golden as golden_module
from . import model_fingerprint as model_fingerprint_module
from .golden_gate import (
    ALL_GOLDEN_SETS,
    GOLDEN,
    GOLDEN_BASE,
    GOLDEN_MODEL,
    GOLDEN_PRESOLVE,
    REQUIRE_ENV,
    SKIP_PREFIX,
    GoldenSet,
    json_count,
    require_golden,
    skip_reason,
)

_HERE = Path(__file__).resolve().parent
_TESTS_ROOT = _HERE.parent
#: 단위 테스트 전용 갱신 플래그 — 실제 플래그(UPDATE_GOLDEN 등)와 겹치지 않는 이름.
_TEST_UPDATE_ENV = "D29_GATE_TEST_UPDATE"
_GATE_ENV_NAMES = (REQUIRE_ENV, _TEST_UPDATE_ENV, *(gs.update_env for gs in ALL_GOLDEN_SETS if gs.update_env))


@pytest.fixture
def clean_env(monkeypatch):
    """문지기가 읽는 플래그(REQUIRE_GOLDEN · 갱신 플래그 전부)를 지운다 — 단위 테스트가 실행 환경에 흔들리지 않게."""
    for name in _GATE_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _tmp_set(tmp_path: Path, *, expected: int = 2, update_env: str | None = _TEST_UPDATE_ENV) -> GoldenSet:
    return GoldenSet("tmp_golden", tmp_path / "tmp_golden", expected, update_env)


def _put_json(directory: Path, names) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        (directory / f"{name}.json").write_text("{}\n", encoding="utf-8")


# ── 1) 문지기 단위 테스트 ───────────────────────────────────────────────


def test_missing_dir_skips_with_the_exact_reason(tmp_path, clean_env):
    """폴더가 없으면 건너뜀 — 사유는 `skip_reason(gs)` 와 정확히 같고 `SKIP_PREFIX` 로 시작하며 `REQUIRE_GOLDEN=1` 을 담는다."""
    gs = _tmp_set(tmp_path)
    assert not gs.path.exists()
    with pytest.raises(pytest.skip.Exception) as excinfo:
        require_golden(gs)
    reason = excinfo.value.msg
    assert reason == skip_reason(gs)
    assert reason.startswith(SKIP_PREFIX)
    assert f"{REQUIRE_ENV}=1" in reason
    assert "tests/engine_input/tmp_golden/" in reason
    assert excinfo.value.allow_module_level is False


def test_readme_only_skips(tmp_path, clean_env):
    """README.md 만 있는 폴더(골든 없는 체크아웃의 golden_model·golden_base 모양)도 '없음'이라 건너뛴다."""
    gs = _tmp_set(tmp_path)
    gs.path.mkdir(parents=True)
    (gs.path / "README.md").write_text("# README\n", encoding="utf-8")
    assert json_count(gs) == 0
    with pytest.raises(pytest.skip.Exception) as excinfo:
        require_golden(gs)
    assert excinfo.value.msg == skip_reason(gs)


def test_require_golden_turns_missing_into_failure(tmp_path, clean_env):
    """`REQUIRE_GOLDEN=1` 이고 없으면 실패(추적 없이 사유만)."""
    gs = _tmp_set(tmp_path)
    clean_env.setenv(REQUIRE_ENV, "1")
    with pytest.raises(pytest.fail.Exception) as excinfo:
        require_golden(gs)
    assert excinfo.value.msg.startswith(skip_reason(gs))
    assert excinfo.value.msg.endswith(f"{REQUIRE_ENV}=1 이라 실패")
    assert excinfo.value.pytrace is False


@pytest.mark.parametrize("value", ["0", "", "true"])
def test_require_golden_is_on_only_for_exactly_one(tmp_path, clean_env, value):
    """`REQUIRE_GOLDEN` 은 값이 `"1"` 일 때만 켜진다 — `"0"`·빈 값·다른 값이면 그대로 건너뛴다."""
    gs = _tmp_set(tmp_path)
    clean_env.setenv(REQUIRE_ENV, value)
    with pytest.raises(pytest.skip.Exception):
        require_golden(gs)


@pytest.mark.parametrize(("present", "expected"), [(1, 2), (3, 2)], ids=["partial-1of2", "extra-3of2"])
def test_count_mismatch_fails_even_without_require(tmp_path, clean_env, present, expected):
    """일부만 있거나(1/2) 남으면(3/2) `REQUIRE_GOLDEN` 없이도 실패한다 — 파일 단위로 건너뛰지 않는다."""
    gs = _tmp_set(tmp_path, expected=expected)
    _put_json(gs.path, [f"S{i}" for i in range(present)])
    with pytest.raises(pytest.fail.Exception) as excinfo:
        require_golden(gs)
    message = excinfo.value.msg
    assert f"{present}개" in message and f"기준 {expected}개" in message, message
    assert "건너뛰지 않고 실패" in message and "golden_gate 의 개수" in message, message
    assert excinfo.value.pytrace is False


def test_complete_set_passes(tmp_path, clean_env):
    """다 있으면(2/2) 그대로 돌아간다."""
    gs = _tmp_set(tmp_path, expected=2)
    _put_json(gs.path, ["S0", "S1"])
    assert require_golden(gs) is None


def test_update_flag_allows_a_partial_set(tmp_path, clean_env):
    """갱신 플래그가 켜져 있으면 일부만 있어도 통과한다(재생성 중에 먼저 쓴 파일을 읽는 자리)."""
    gs = _tmp_set(tmp_path, expected=2)
    _put_json(gs.path, ["S0"])
    clean_env.setenv(_TEST_UPDATE_ENV, "1")
    assert require_golden(gs) is None
    clean_env.setenv(_TEST_UPDATE_ENV, "0")  # 갱신 플래그도 "1" 일 때만 켜진다
    with pytest.raises(pytest.fail.Exception):
        require_golden(gs)


def test_update_flag_with_nothing_still_skips(tmp_path, clean_env):
    """갱신 플래그가 켜져 있어도 하나도 없으면 건너뛴다(읽는 쪽은 대조할 것이 없다) · `REQUIRE_GOLDEN=1` 이면 실패다."""
    gs = _tmp_set(tmp_path)
    clean_env.setenv(_TEST_UPDATE_ENV, "1")
    with pytest.raises(pytest.skip.Exception) as excinfo:
        require_golden(gs)
    assert excinfo.value.msg == skip_reason(gs)
    clean_env.setenv(REQUIRE_ENV, "1")
    with pytest.raises(pytest.fail.Exception):
        require_golden(gs)


def test_frozen_set_ignores_other_update_flags(tmp_path, clean_env):
    """`update_env=None`(동결 사본)이면 `UPDATE_GOLDEN=1` 이 켜져 있어도 일부만 있을 때 실패한다."""
    gs = _tmp_set(tmp_path, expected=2, update_env=None)
    _put_json(gs.path, ["S0"])
    clean_env.setenv("UPDATE_GOLDEN", "1")
    with pytest.raises(pytest.fail.Exception):
        require_golden(gs)


def test_allow_module_level_is_carried_on_the_skip(tmp_path, clean_env):
    """`allow_module_level=True` 가 건너뜀 예외에 실린다(모듈 import 때 부르는 자리 — 모듈 전체를 건너뛴다)."""
    gs = _tmp_set(tmp_path)
    with pytest.raises(pytest.skip.Exception) as excinfo:
        require_golden(gs, allow_module_level=True)
    assert excinfo.value.allow_module_level is True
    assert excinfo.value.msg == skip_reason(gs)


def test_json_in_subfolders_is_not_counted(tmp_path, clean_env):
    """하위 폴더의 JSON(옛 기준 폴더 등)과 이름이 `.json` 인 폴더는 세지 않는다 — 바로 아래 파일만 센다."""
    gs = _tmp_set(tmp_path, expected=2)
    _put_json(gs.path / "older", ["S0", "S1", "S2"])
    (gs.path / "folder.json").mkdir()
    assert json_count(gs) == 0
    with pytest.raises(pytest.skip.Exception):
        require_golden(gs)
    _put_json(gs.path, ["S0", "S1"])
    assert json_count(gs) == 2
    assert require_golden(gs) is None


# ── 2) 이 브랜치의 파일 수 단언 · 정본 표 · 별칭 ─────────────────────────


@pytest.mark.parametrize("gs", ALL_GOLDEN_SETS, ids=[gs.key for gs in ALL_GOLDEN_SETS])
def test_branch_golden_count(gs):
    """이 브랜치 로컬의 골든 JSON 수가 기준과 같다(D29 파일 수 단언 — 16·3·9·16). 골든이 없는 체크아웃에서는 D29 사유로
    건너뛰고(`REQUIRE_GOLDEN=1` 이면 실패), 일부만 있으면 실패한다."""
    require_golden(gs)
    count = json_count(gs)
    assert count == gs.expected or os.environ.get(gs.update_env or "") == "1", (gs.key, count, gs.expected)


def test_golden_set_table_is_consistent():
    """정본 표의 모양 — key 가 곧 `tests/engine_input/` 아래 경로이고, 갱신 플래그는 서로 다르며(같으면 한 번에 함께 덮어써진다)
    동결 사본(golden_base)만 플래그가 없다. 플래그 이름은 리터럴로 고정한다(상수끼리 비교하면 함께 바뀌어도 통과한다)."""
    assert ALL_GOLDEN_SETS == (GOLDEN, GOLDEN_PRESOLVE, GOLDEN_MODEL, GOLDEN_BASE)
    for gs in ALL_GOLDEN_SETS:
        assert gs.path == _HERE / gs.key, gs
        assert gs.expected > 0, gs
    assert [gs.update_env for gs in ALL_GOLDEN_SETS] == [
        "UPDATE_GOLDEN", "UPDATE_GOLDEN_PRESOLVE", "UPDATE_GOLDEN_MODEL", None]
    assert GOLDEN_BASE.key.startswith("golden_base/")
    assert REQUIRE_ENV == "REQUIRE_GOLDEN"


@pytest.mark.parametrize(("module", "name", "source"), [
    ("engine_input.golden", "GOLDEN_DIR", GOLDEN.path),
    ("engine_input.golden", "UPDATE_ENV", GOLDEN.update_env),
    ("engine_input.model_fingerprint", "GOLDEN_MODEL_DIR", GOLDEN_MODEL.path),
    ("engine_input.test_model_fingerprint", "UPDATE_ENV", GOLDEN_MODEL.update_env),
    ("engine_input.test_presolve_block_characterization", "GOLDEN_DIR", GOLDEN_PRESOLVE.path),
    ("engine_input.test_presolve_block_characterization", "UPDATE_PRESOLVE_ENV", GOLDEN_PRESOLVE.update_env),
    ("engine_input.test_expected_diff", "GOLDEN_BASE_DIR", GOLDEN_BASE.path),
    ("engine_contract.test_effective", "GOLDEN_DIR", GOLDEN.path),
])
def test_old_constants_are_aliases_of_the_single_source(module, name, source):
    """옛 상수(경로·플래그 이름)는 `golden_gate` 정본의 별칭이다 — 정본 한 곳만 고치면 모두 따라간다."""
    assert getattr(importlib.import_module(module), name) == source


# ── 3) 실제 읽는 함수가 문지기를 거치는지(동적) ─────────────────────────


@pytest.mark.parametrize(("module", "attr", "reader"), [
    (golden_module, "GOLDEN", "read_golden"),
    (model_fingerprint_module, "GOLDEN_MODEL", "read_golden_model"),
], ids=["read_golden", "read_golden_model"])
def test_readers_go_through_the_gate(tmp_path, clean_env, module, attr, reader):
    """모듈의 골든 세트를 빈 임시 세트로 바꾸면 읽는 함수가 D29 사유로 건너뛴다(`REQUIRE_GOLDEN=1` 이면 실패)."""
    real = getattr(module, attr)
    empty = GoldenSet(real.key, tmp_path / "empty", real.expected, real.update_env)
    clean_env.setattr(module, attr, empty)
    with pytest.raises(pytest.skip.Exception) as excinfo:
        getattr(module, reader)("S1")
    assert excinfo.value.msg == skip_reason(empty)
    clean_env.setenv(REQUIRE_ENV, "1")
    with pytest.raises(pytest.fail.Exception):
        getattr(module, reader)("S1")


# ── 4) 정적 감사(AST) ───────────────────────────────────────────────────
#
# 이름표 기반 근사다. 골든을 가리키는 이름(별칭 import · 모듈 수준 대입 · 골든 경로를 돌려주는 모듈 함수까지 따라간다)이나 디렉터리
# 조각이 든 문자열이, 읽기 연산(골든 경로를 다른 함수에 넘기는 호출 포함)과 한 함수에 함께 있으면 '골든을 읽는 함수'로 본다.
# 못 잡는 것: 지역 변수에 담은 경로만 다른 함수로 넘기는 흐름(`p = GOLDEN_DIR / "S1.json"; helper(p)`), 모듈을 건너간 파생 별칭
# (`from .x import 그_모듈이_만든_별칭`), 쪼개 이어 붙인 문자열 경로. 그런 모양은 리뷰에서 본다.

_NONE: frozenset[str] = frozenset()


def _set_id(gs: GoldenSet) -> str:
    """세트를 가리키는 짧은 이름 — `tests/engine_input/` 바로 아래 디렉터리 이름(`golden_base/dev4ec454e` → `golden_base`)."""
    return gs.key.split("/", 1)[0]


#: 세트 이름(GoldenSet — 문지기의 인자) → 세트. 다른 함수에 넘겨도 '읽기'로 치지 않는다(받는 쪽이 문지기를 부른다).
SET_REFS: dict[str, frozenset[str]] = {
    "GOLDEN": frozenset({_set_id(GOLDEN)}),
    "GOLDEN_PRESOLVE": frozenset({_set_id(GOLDEN_PRESOLVE)}),
    "GOLDEN_MODEL": frozenset({_set_id(GOLDEN_MODEL)}),
    "GOLDEN_BASE": frozenset({_set_id(GOLDEN_BASE)}),
    "ALL_GOLDEN_SETS": frozenset(_set_id(gs) for gs in ALL_GOLDEN_SETS),
}
#: 경로 이름(디렉터리 상수 · 경로를 돌려주는 함수) → 세트. 모듈이 같은 이름에 다른 골든 경로를 대입하면 그 모듈에서는 대입한 값을
#: 따른다(`test_presolve_block_characterization.GOLDEN_DIR` 는 golden_presolve).
PATH_REFS: dict[str, frozenset[str]] = {
    "GOLDEN_DIR": SET_REFS["GOLDEN"], "golden_path": SET_REFS["GOLDEN"],
    "GOLDEN_MODEL_DIR": SET_REFS["GOLDEN_MODEL"], "golden_model_path": SET_REFS["GOLDEN_MODEL"],
    "GOLDEN_BASE_DIR": SET_REFS["GOLDEN_BASE"], "_golden_path": SET_REFS["GOLDEN_PRESOLVE"],
}
#: 지시서 §1-4 의 골든 참조 이름 11개(`ast.Name` — 같은 이름의 속성 `x.GOLDEN_DIR` 도 센다).
GOLDEN_REF_NAMES = frozenset(SET_REFS) | frozenset(PATH_REFS)
#: 문자열 속 골든 디렉터리 조각 — `"golden"` · `"tests/engine_input/golden/S1.json"` · f"golden/{name}.json" 의 상수 조각.
GOLDEN_DIR_PATTERN = re.compile(r"(?:^|/)(golden(?:_presolve|_model|_base)?)(?=/|$)")
#: 읽기 연산 — 이 이름의 속성(받는 쪽 무관).
READ_ATTRS = frozenset({"read_text", "read_bytes", "glob", "rglob", "iterdir"})
#: 읽기 함수 — `open`(받는 쪽 무관: `open` · `x.open` · `io.open` · 그 별칭)과, (모듈, 이름)이 이 표에 있는 호출(별칭 포함).
READ_FUNCS = frozenset({
    ("os", "listdir"), ("os", "scandir"), ("os", "walk"),
    ("shutil", "copy"), ("shutil", "copy2"), ("shutil", "copyfile"), ("shutil", "copytree"),
})
#: 경로를 만들기만 하는 호출 — 골든 경로를 넘겨도 '읽기'가 아니고, 결과는 여전히 골든 경로다.
PATH_BUILDERS = frozenset({
    "Path", "PurePath", "PosixPath", "joinpath", "with_name", "with_suffix", "with_stem", "resolve", "absolute",
    "fspath", "str",
})
GATE_NAME = "require_golden"

AUDIT_ROOT = _TESTS_ROOT
AUDIT_SKIP = frozenset({_HERE / "golden_gate.py", _HERE / "test_golden_gate.py"})
_AUDIT_SKIP_PARTS = frozenset({"__pycache__", ".omc"})

#: 문지기 없이 골든을 읽어도 되는 함수 — `(tests/ 기준 파일, 함수 이름)` → 이유.
AUDIT_EXCEPTIONS: dict[tuple[str, str], str] = {
    ("engine_input/test_model_fingerprint.py", "_existing_golden_hashes"):
        "golden/ 을 건드리지 않았는지 실행 전후 sha256 을 비교한다 — 골든이 없으면 빈 사전끼리 같아 그대로 의미가 있다",
}


class AuditHit(NamedTuple):
    """감사 위반 한 건 — 파일(tests/ 기준) · 자리(함수 이름, 중첩은 `바깥.안` · 모듈 수준은 `<module>`) · 줄 · 무엇이 문제인가."""

    file: str
    where: str
    line: int
    problem: str


@dataclass
class _Module:
    """감사할 모듈 하나 — 구문 나무와 이름표."""

    file: str
    tree: ast.Module
    imports: dict[str, tuple[str, str]]  # import 로 생긴 지역 이름 → (모듈, 원래 이름) — `import os as o` 는 ("os", "")
    refs: dict[str, frozenset[str]]  # 골든을 가리키는 이름 → 세트(별칭·모듈 대입·경로 함수까지)
    set_names: set[str]  # refs 가운데 세트 객체(GoldenSet) 이름
    docstrings: frozenset[int]  # 문서 문자열 노드의 id — 문자열 조각 검사에서 뺀다


def _imports(tree: ast.Module) -> dict[str, tuple[str, str]]:
    """import 로 생긴 지역 이름 → (모듈, 원래 이름) — 함수 안 import 까지 본다."""
    found: dict[str, tuple[str, str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".", 1)[0]
                found[alias.asname or top] = (alias.name if alias.asname else top, "")
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                found[alias.asname or alias.name] = (node.module or "", alias.name)
    return found


def _docstring_ids(tree: ast.Module) -> frozenset[int]:
    owners = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    return frozenset(
        id(node.body[0].value) for node in ast.walk(tree)
        if isinstance(node, owners) and node.body and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant) and isinstance(node.body[0].value.value, str))


def _callee(call: ast.Call, mod: _Module) -> tuple[str, str]:
    """호출 대상 (모듈, 이름) — 별칭을 푼다(`io_open` → ("io", "open")). 속성 호출은 받는 쪽이 import 한 모듈이면 그 모듈이다."""
    func = call.func
    if isinstance(func, ast.Name):
        module, name = mod.imports.get(func.id, ("", func.id))
        return module, name or func.id
    if isinstance(func, ast.Attribute):
        owner = mod.imports.get(func.value.id) if isinstance(func.value, ast.Name) else None
        return (owner[0] if owner and not owner[1] else ""), func.attr
    return "", ""


def _is_gate(node: ast.AST, mod: _Module) -> bool:
    return isinstance(node, ast.Call) and _callee(node, mod)[1] == GATE_NAME


def _import_safe_gate(call: ast.Call) -> bool:
    """`require_golden(..., allow_module_level=True)` 인가 — import 때 불려도 골든이 비었으면 모듈 전체를 건너뛴다."""
    return any(kw.arg == "allow_module_level" and isinstance(kw.value, ast.Constant) and kw.value.value is True
               for kw in call.keywords)


def _dir_sets(text: str) -> frozenset[str]:
    return frozenset(match.group(1) for match in GOLDEN_DIR_PATTERN.finditer(text))


def _set_sets(node: ast.AST | None, mod: _Module) -> frozenset[str]:
    """`node` 가 세트 객체(`GOLDEN` 등 · 그 별칭 · `golden_gate.GOLDEN`)면 그 세트."""
    name = node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else None
    return mod.refs[name] if name in mod.set_names else _NONE


def _path_sets(node: ast.AST, mod: _Module) -> frozenset[str]:
    """`node` 가 골든 경로(디렉터리·파일)로 계산되면 그 세트 — 세트 객체 자체는 경로가 아니다(`GOLDEN.path` 는 경로다)."""
    if isinstance(node, (ast.Name, ast.Attribute)):
        name = node.id if isinstance(node, ast.Name) else node.attr
        if isinstance(node, ast.Attribute) and name == "path":
            return _set_sets(node.value, mod)
        return _NONE if name in mod.set_names else mod.refs.get(name, _NONE)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return _path_sets(node.left, mod) | _path_sets(node.right, mod)
    if isinstance(node, ast.Constant):
        return _dir_sets(node.value) if isinstance(node.value, str) else _NONE
    if isinstance(node, ast.JoinedStr):
        parts = (part.value if isinstance(part, ast.FormattedValue) else part for part in node.values)
        return _NONE.union(*(_path_sets(part, mod) for part in parts))
    if isinstance(node, ast.Call):
        name = _callee(node, mod)[1]
        if name in PATH_BUILDERS:
            owner = [node.func.value] if isinstance(node.func, ast.Attribute) else []
            parts = (*owner, *node.args, *(kw.value for kw in node.keywords))
            return _NONE.union(*(_path_sets(part, mod) for part in parts))
        return _NONE if name in mod.set_names else mod.refs.get(name, _NONE)
    return _NONE


def _ref_sets(node: ast.AST, mod: _Module) -> frozenset[str]:
    """`node` 가 골든을 가리키면 그 세트 — 이름·속성(별칭 포함), 디렉터리 조각이 든 문자열(문서 문자열 제외), 참조 이름 그대로의
    문자열(`getattr(golden, "GOLDEN_DIR")`)."""
    if isinstance(node, ast.Name):
        return mod.refs.get(node.id, _NONE)
    if isinstance(node, ast.Attribute):
        return mod.refs.get(node.attr, _NONE)
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in mod.docstrings:
        return _dir_sets(node.value) | mod.refs.get(node.value, _NONE)
    return _NONE


def _is_read(node: ast.AST, mod: _Module) -> bool:
    """읽기 연산인가 — 읽기 속성, `open`·os/shutil 읽기 함수, 골든 경로를 다른 함수에 넘기는 호출(넘겨받은 함수가 읽을 수 있다 ·
    문지기와 경로 만들기는 빼고)."""
    if isinstance(node, ast.Attribute):
        return node.attr in READ_ATTRS
    if not isinstance(node, ast.Call):
        return False
    module, name = _callee(node, mod)
    if name == "open" or (module, name) in READ_FUNCS:
        return True
    if name == GATE_NAME or name in PATH_BUILDERS:
        return False
    return any(_path_sets(arg, mod) for arg in (*node.args, *(kw.value for kw in node.keywords)))


def _learn(stmt: ast.stmt, mod: _Module) -> None:
    """모듈 수준 문장 하나에서 골든 참조 이름을 배운다 — 대입(`D = GOLDEN.path` · `G = GOLDEN`)과 골든 경로를 돌려주는 함수."""
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
        returns = (node.value for node in ast.walk(stmt) if isinstance(node, ast.Return) and node.value is not None)
        sets = _NONE.union(*(_path_sets(value, mod) for value in returns))
        if sets:
            mod.refs[stmt.name] = sets
        return
    if not isinstance(stmt, (ast.Assign, ast.AnnAssign)) or stmt.value is None:
        return
    as_set, as_path = _set_sets(stmt.value, mod), _path_sets(stmt.value, mod)
    for target in stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]:
        if isinstance(target, ast.Name) and (as_set or as_path):
            mod.refs[target.id] = as_set or as_path
            if as_set:
                mod.set_names.add(target.id)


def _module(source: str, file: str) -> _Module:
    """소스 하나의 이름표를 만든다 — 기본 이름표에 별칭 import 를 더하고, 모듈 수준 문장에서 더 배울 것이 없을 때까지 돈다."""
    tree = ast.parse(source)
    mod = _Module(file, tree, _imports(tree), {**PATH_REFS, **SET_REFS}, set(SET_REFS), _docstring_ids(tree))
    for local, (_, name) in mod.imports.items():  # `from .golden import GOLDEN_DIR as GD`
        if local != name and name in mod.refs:
            mod.refs[local] = mod.refs[name]
            if name in mod.set_names:
                mod.set_names.add(local)
    for _ in range(len(tree.body) + 1):  # 앞 문장이 뒤에 정의된 이름을 쓸 수 있다
        before = (dict(mod.refs), set(mod.set_names))
        for stmt in tree.body:
            _learn(stmt, mod)
        if (mod.refs, mod.set_names) == before:
            break
    return mod


def _functions(tree: ast.Module) -> list[tuple[ast.AST, str]]:
    """함수 노드와 이름 — 중첩은 `바깥.안`, 메서드는 `클래스.메서드`, lambda 는 `<lambda:줄>`."""
    found: list[tuple[ast.AST, str]] = []

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = f"{prefix}{child.name}"
            elif isinstance(child, ast.Lambda):
                name = f"{prefix}<lambda:{child.lineno}>"
            elif isinstance(child, ast.ClassDef):
                visit(child, f"{prefix}{child.name}.")
                continue
            else:
                visit(child, prefix)
                continue
            found.append((child, name))
            visit(child, f"{name}.")

    visit(tree, "")
    return found


def _body_nodes(func: ast.AST) -> list[ast.AST]:
    """함수가 불릴 때 도는 하위 트리(본문 — 중첩 함수 포함). 데코레이터·기본값은 정의 시점이라 바깥 자리가 본다."""
    roots = func.body if isinstance(func.body, list) else [func.body]
    return [node for root in roots for node in ast.walk(root)]


def _eager_nodes(stmt: ast.AST) -> list[ast.AST]:
    """정의(import) 시점에 평가되는 노드 — 함수·lambda 본문은 빼고 데코레이터·기본값은 넣는다(클래스 본문은 넣는다)."""
    out: list[ast.AST] = []
    stack = [stmt]
    while stack:
        node = stack.pop()
        out.append(node)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            if not isinstance(node, ast.Lambda):
                stack.extend(node.decorator_list)
            stack.extend(node.args.defaults)
            stack.extend(default for default in node.args.kw_defaults if default is not None)
        else:
            stack.extend(ast.iter_child_nodes(node))
    return out


def _leading_gates(func: ast.AST, mod: _Module) -> list[ast.Call]:
    """docstring 다음 첫 문장부터 이어지는 `require_golden(...)` 호출 문장들(lambda 는 문장이 없어 늘 빈 목록)."""
    body = func.body if isinstance(func.body, list) else []
    if body and isinstance(body[0], ast.Expr) and id(body[0].value) in mod.docstrings:
        body = body[1:]
    gates: list[ast.Call] = []
    for stmt in body:
        if not (isinstance(stmt, ast.Expr) and _is_gate(stmt.value, mod)):
            break
        gates.append(stmt.value)
    return gates


def _gate_sets(call: ast.Call, mod: _Module) -> frozenset[str]:
    """문지기 호출이 막는 세트 — 첫 인자(또는 `gs=`)가 세트 이름일 때만 안다(모르면 아무것도 막지 않는 것으로 본다)."""
    target = call.args[0] if call.args else next((kw.value for kw in call.keywords if kw.arg == "gs"), None)
    return _set_sets(target, mod)


def _function_problem(func: ast.AST, mod: _Module) -> str | None:
    """함수가 골든을 읽는데 docstring 다음 첫 문장에서 그 세트의 문지기를 부르지 않으면 무엇이 문제인지(아니면 None)."""
    nodes = _body_nodes(func)
    sets = _NONE.union(*(_ref_sets(node, mod) for node in nodes))
    if not sets or not any(_is_read(node, mod) for node in nodes):
        return None
    gates = _leading_gates(func, mod)
    if not gates:
        if isinstance(func, ast.Lambda):
            return "lambda 는 첫 문장에 문지기를 둘 수 없다 — def 로 바꾸고 첫 문장에서 require_golden 을 부른다"
        if any(_is_gate(node, mod) for node in nodes):
            return "require_golden 이 첫 문장이 아니다(읽은 뒤·조건 안·중첩 함수 안) — docstring 다음 첫 문장으로 옮긴다"
        return "골든을 읽는데 require_golden 이 없다 — docstring 다음 첫 문장에서 부른다"
    covered = _NONE.union(*(_gate_sets(gate, mod) for gate in gates))
    if not sets <= covered:
        return f"첫 문장의 문지기 세트가 가리키는 골든을 덮지 않는다 — 가리킴 {sorted(sets)} · 막음 {sorted(covered)}"
    return None


def _defined_names(mod: _Module) -> frozenset[str]:
    return frozenset(where.rsplit(".", 1)[-1] for _, where in _functions(mod.tree))


def _call_target(call: ast.Call, mod: _Module, defined: frozenset[str]) -> tuple[str, bool]:
    """호출 대상 이름과, 이 모듈이 정의한 함수를 부르는가(import 하지 않은 맨 이름 호출이고 이 모듈에 같은 이름의 def 가 있다)."""
    func = call.func
    local = isinstance(func, ast.Name) and func.id not in mod.imports and func.id in defined
    return _callee(call, mod)[1], local


def _gated(modules: list[_Module], outside: frozenset[str] = _NONE) -> tuple[frozenset[str], dict[str, frozenset[str]]]:
    """import 때 부르면 수집 오류가 나는 함수 — (import·속성 호출로 부를 때 보는 이름 전체, 모듈마다 그 모듈이 정의한 이름).

    문지기를 `allow_module_level=True` 없이 부르는 함수와, 그런 함수를 (거슬러) 부르는 함수다. 같은 모듈이 정의한 함수를 부르면
    그 정의로 잇고, import·속성 호출은 이름으로 잇는다(다른 모듈의 같은 이름 함수도 함께 걸리는 보수적 근사). `outside` 는 이
    모듈들 밖에서 이미 아는 이름이다(자기 검사가 실제 감사 대상에서 모은 이름을 넘긴다)."""
    rows: dict[str, list[tuple[str, list[tuple[str, bool]]]]] = {}
    local: dict[str, set[str]] = {}
    for mod in modules:
        defined = _defined_names(mod)
        rows[mod.file], local[mod.file] = [], set()
        for func, where in _functions(mod.tree):
            name, calls = where.rsplit(".", 1)[-1], [node for node in _body_nodes(func) if isinstance(node, ast.Call)]
            if any(_is_gate(call, mod) and not _import_safe_gate(call) for call in calls):
                local[mod.file].add(name)
            rows[mod.file].append((name, [_call_target(call, mod, defined) for call in calls]))
    while True:
        names = outside.union(*local.values())
        changed = False
        for file, functions in rows.items():
            for name, targets in functions:
                if name not in local[file] and any(
                        (callee in local[file]) if is_local else (callee in names) for callee, is_local in targets):
                    local[file].add(name)
                    changed = True
        if not changed:
            return frozenset(names), {file: frozenset(found) for file, found in local.items()}


def _module_problems(mod: _Module, names: frozenset[str], local: frozenset[str]) -> list[AuditHit]:
    """모듈 수준(import 때 평가되는 문장·데코레이터·기본값·클래스 본문)의 위반 — 골든을 읽거나, 문지기를 거치는 함수를 부른다."""
    hits: list[AuditHit] = []
    defined = _defined_names(mod)
    for stmt in mod.tree.body:
        nodes = _eager_nodes(stmt)
        if any(_ref_sets(node, mod) for node in nodes) and any(_is_read(node, mod) for node in nodes):
            hits.append(AuditHit(mod.file, "<module>", stmt.lineno,
                                 "모듈 수준(import 때)에서 골든을 읽는다 — 함수 안으로 옮기고 import 때 부르지 않는다"))
            continue
        called: set[str] = set()
        for call in (node for node in nodes if isinstance(node, ast.Call)):
            if _is_gate(call, mod):
                if not _import_safe_gate(call):
                    called.add(GATE_NAME)
                continue
            callee, is_local = _call_target(call, mod, defined)
            if (callee in local) if is_local else (callee in names):
                called.add(callee)
        if called:
            hits.append(AuditHit(mod.file, "<module>", stmt.lineno,
                                 f"import 때 문지기를 거치는 함수를 부른다({', '.join(sorted(called))}) — 골든이 없는 "
                                 f"체크아웃에서는 allow_module_level 없는 skip 이라 수집 오류다"))
    return hits


def _audit_module(mod: _Module, names: frozenset[str], local: frozenset[str]) -> list[AuditHit]:
    hits = [AuditHit(mod.file, where, func.lineno, problem)
            for func, where in _functions(mod.tree) if (problem := _function_problem(func, mod))]
    return hits + _module_problems(mod, names, local)


def audit_source(source: str, file: str, outside: frozenset[str] = _NONE) -> list[AuditHit]:
    """소스 하나를 감사한다 — `outside` 는 다른 모듈의 '문지기를 거치는 함수' 이름(이 소스가 정의한 것은 여기서 따진다)."""
    mod = _module(source, file)
    names, local = _gated([mod], outside)
    return _audit_module(mod, names, local[file])


def _audit_files() -> list[Path]:
    return [path for path in sorted(AUDIT_ROOT.rglob("*.py"))
            if not _AUDIT_SKIP_PARTS & set(path.relative_to(AUDIT_ROOT).parts) and path not in AUDIT_SKIP]


@functools.lru_cache(maxsize=1)
def _audited() -> tuple[tuple[_Module, ...], frozenset[str], dict[str, frozenset[str]]]:
    """감사 대상 전부의 이름표와, 그 전체에서 모은 '문지기를 거치는 함수'(이름 전체 · 모듈마다 정의한 이름)."""
    modules = tuple(_module(path.read_text(encoding="utf-8"), path.relative_to(_TESTS_ROOT).as_posix())
                    for path in _audit_files())
    return (modules, *_gated(list(modules)))


def _audit_all() -> list[AuditHit]:
    modules, names, local = _audited()
    return [hit for mod in modules for hit in _audit_module(mod, names, local[mod.file])]


def test_static_audit_covers_the_whole_tests_tree():
    """감사 대상은 `tests/**` 의 .py 전부다(문지기·이 파일·`__pycache__`·`.omc` 제외). 지시서는 engine_input·engine_contract 두
    폴더였는데, 두 폴더 밖 헬퍼가 골든을 읽는 경우까지 잡도록 넓혔다."""
    files = {path.relative_to(_TESTS_ROOT).as_posix() for path in _audit_files()}
    assert {"conftest.py", "engine_input/golden.py", "engine_input/model_fingerprint.py",
            "engine_contract/test_effective.py"} <= files
    assert "engine_input/golden_gate.py" not in files and "engine_input/test_golden_gate.py" not in files
    assert not any(_AUDIT_SKIP_PARTS & set(name.split("/")) for name in files)


def test_static_audit_reference_table():
    """골든 참조 이름은 지시서 §1-4 의 11개이고, 세트 이름·디렉터리 조각 무늬가 세트마다 같은 디렉터리를 가리킨다."""
    assert {
        "GOLDEN_DIR", "GOLDEN_MODEL_DIR", "GOLDEN_BASE_DIR", "golden_path", "golden_model_path", "_golden_path",
        "GOLDEN", "GOLDEN_PRESOLVE", "GOLDEN_MODEL", "GOLDEN_BASE", "ALL_GOLDEN_SETS",
    } == GOLDEN_REF_NAMES
    named = {"GOLDEN": GOLDEN, "GOLDEN_PRESOLVE": GOLDEN_PRESOLVE, "GOLDEN_MODEL": GOLDEN_MODEL, "GOLDEN_BASE": GOLDEN_BASE}
    for name, gs in named.items():
        assert SET_REFS[name] == _dir_sets(f"tests/engine_input/{gs.key}/S1.json") == {_set_id(gs)}, name
    assert SET_REFS["ALL_GOLDEN_SETS"] == {_set_id(gs) for gs in ALL_GOLDEN_SETS}
    assert _dir_sets("golden_stop") == _dir_sets("golden_presolve 가 동결본과 다르다") == _NONE


def test_static_audit_every_golden_read_is_gated():
    """골든을 읽는 함수는 전부 첫 문장에서 그 세트의 `require_golden` 을 거치고, import 때는 골든을 읽거나 문지기를 거치는 함수를
    부르지 않는다 — 예외 목록 밖의 위반이 0이다."""
    unexpected = [hit for hit in _audit_all() if (hit.file, hit.where) not in AUDIT_EXCEPTIONS]
    assert unexpected == [], (
        "골든을 읽는 함수는 docstring 다음 첫 문장에서 require_golden(<읽는 세트>) 를 부른다. 모듈 수준(import 때)에서는 "
        "골든을 읽지도 문지기를 거치는 함수를 부르지도 않는다 — 함수 안으로 옮기고 import 때 부르지 않는다(매개변수 목록은 "
        "코드가 정의한 이름에서 뽑는다). import 때 꼭 불러야 하면 require_golden(..., allow_module_level=True) — 그래도 골든 "
        "개수가 어긋나면 수집 오류다.\n" + "\n".join(f"{hit.file}:{hit.line} {hit.where} — {hit.problem}" for hit in unexpected))


def test_static_audit_exceptions_are_live():
    """예외 목록의 항목은 실제로 존재하고 감사에 걸린다(죽은 예외 금지) — 이유도 비어 있지 않다."""
    hit_keys = {(hit.file, hit.where) for hit in _audit_all()}
    for key, reason in AUDIT_EXCEPTIONS.items():
        assert reason.strip(), key
        assert key in hit_keys, f"죽은 예외(그 함수가 없거나 더는 골든을 읽지 않는다): {key}"


def test_static_audit_knows_the_real_gated_readers():
    """'문지기를 거치는 함수'(import 때 부르면 안 되는 함수)에 실제 읽는 함수가 다 들어 있다 — stage1 대조의 `_template` 도
    들어 있다(10-20 오케스트레이터: import 때 읽던 것을 처음 쓸 때 읽게 바꿨다)."""
    gated = _audited()[1]
    assert {"read_golden", "read_golden_model", "_read_golden", "_read_base", "_load_golden_config_data", "_template"} <= gated


def test_no_import_time_gate_in_test_modules():
    """테스트 모듈은 `require_golden(..., allow_module_level=True)` 를 쓰지 않는다(0곳) — 그 자리는 골든 수가 어긋나거나
    `REQUIRE_GOLDEN=1` 이면 수집 오류가 되고, pytest 는 수집 오류에서 세션 전체를 멈춘다(나머지 테스트 결과까지 가려진다).
    골든 데이터는 처음 쓰는 테스트에서 읽는다(`test_precheck_vs_engine_stage1._template` — `functools.cache`)."""
    modules = _audited()[0]
    used = [f"{mod.file}:{node.lineno}" for mod in modules for node in ast.walk(mod.tree)
            if _is_gate(node, mod) and _import_safe_gate(node)]
    assert used == [], used


#: 가짜 소스 → (기대 위반 자리, 위반 사유에 들어 있어야 할 말). 문지기를 제대로 거친 소스는 위반 0건이다.
_SELF_CHECK_SOURCES = {
    "ungated-function": ("""
        from .golden import GOLDEN_DIR

        def leak(name):
            return (GOLDEN_DIR / f"{name}.json").read_text(encoding="utf-8")
        """, ["leak"], "require_golden 이 없다"),
    "gated-function": ("""
        from .golden import GOLDEN_DIR
        from .golden_gate import GOLDEN, require_golden

        def ok(name):
            require_golden(GOLDEN)
            return (GOLDEN_DIR / f"{name}.json").read_text(encoding="utf-8")
        """, [], ""),
    "module-level-glob": ("""
        from .golden import GOLDEN_DIR

        NAMES = sorted(path.stem for path in GOLDEN_DIR.glob("*.json"))
        """, ["<module>"], "골든을 읽는다"),
    "assembled-path-open": ("""
        from pathlib import Path

        def leak():
            with open(Path(__file__).parent / "golden" / "S1.json", encoding="utf-8") as fh:
                return fh.read()
        """, ["leak"], "require_golden 이 없다"),
    "decorator-at-import": ("""
        import pytest
        from .golden import GOLDEN_DIR
        from .golden_gate import GOLDEN, require_golden

        @pytest.mark.parametrize("name", [path.stem for path in GOLDEN_DIR.glob("*.json")])
        def test_x(name):
            require_golden(GOLDEN)
        """, ["<module>"], "골든을 읽는다"),
    "nested-without-own-gate": ("""
        from .golden_gate import GOLDEN, require_golden

        def outer():
            require_golden(GOLDEN)

            def inner():
                return GOLDEN.path.iterdir()
            return inner
        """, ["outer.inner"], "require_golden 이 없다"),
    "method-and-attribute-ref": ("""
        from . import model_fingerprint

        class Reader:
            def read(self):
                return model_fingerprint.GOLDEN_MODEL_DIR.rglob("*.json")
        """, ["Reader.read"], "require_golden 이 없다"),
    "module-level-lambda": ("""
        from .golden import golden_path

        LOAD = lambda name: golden_path(name).read_bytes()
        """, ["<lambda:4>"], "lambda"),
    # ── 이름표가 따라가는 별칭·문자열 경로(리뷰 지적 — 2026-10-20) ──
    "import-alias": ("""
        import json
        from .golden import GOLDEN_DIR as GD

        def leak(name):
            return json.loads((GD / f"{name}.json").read_text(encoding="utf-8"))
        """, ["leak"], "require_golden 이 없다"),
    "module-alias": ("""
        from .golden import GOLDEN_DIR

        GD2 = GOLDEN_DIR

        def leak(name):
            return (GD2 / f"{name}.json").read_text(encoding="utf-8")
        """, ["leak"], "require_golden 이 없다"),
    "set-alias": ("""
        from .golden_gate import GOLDEN as G

        def leak():
            return sorted(G.path.glob("*.json"))
        """, ["leak"], "require_golden 이 없다"),
    "full-string-path": ("""
        from pathlib import Path

        def leak():
            return Path("tests/engine_input/golden/S1.json").read_text(encoding="utf-8")
        """, ["leak"], "require_golden 이 없다"),
    "fstring-path": ("""
        from pathlib import Path

        HERE = Path(__file__).resolve().parent

        def leak(name):
            return (HERE / f"golden/{name}.json").read_text(encoding="utf-8")
        """, ["leak"], "require_golden 이 없다"),
    "getattr-string": ("""
        from . import golden

        def leak():
            return list(getattr(golden, "GOLDEN_DIR").glob("*.json"))
        """, ["leak"], "require_golden 이 없다"),
    # ── 읽기 연산: os/shutil · open 별칭 · 골든 경로를 다른 함수에 넘기기 ──
    "module-level-listdir": ("""
        import os
        from .golden import GOLDEN_DIR

        NAMES = sorted(name[:-5] for name in os.listdir(GOLDEN_DIR) if name.endswith(".json"))
        """, ["<module>"], "골든을 읽는다"),
    "shutil-copy": ("""
        import shutil
        from .golden import GOLDEN_DIR

        def leak(tmp):
            source = GOLDEN_DIR / "S1.json"
            shutil.copy(source, tmp)
        """, ["leak"], "require_golden 이 없다"),
    "io-open-alias": ("""
        import json
        from io import open as io_open
        from .golden import GOLDEN_DIR

        def leak():
            source = GOLDEN_DIR / "S1.json"
            with io_open(source, encoding="utf-8") as fh:
                return json.load(fh)
        """, ["leak"], "require_golden 이 없다"),
    "path-passed-to-helper": ("""
        import json
        from .golden import GOLDEN_DIR

        def _load_json(directory, name):
            return json.loads((directory / f"{name}.json").read_text(encoding="utf-8"))

        def leak(name):
            return _load_json(GOLDEN_DIR, name)
        """, ["leak"], "require_golden 이 없다"),
    # ── 문지기 자리·세트(첫 문장 · 읽는 세트와 같은 세트) ──
    "wrong-set": ("""
        from .golden import GOLDEN_DIR
        from .golden_gate import GOLDEN_MODEL, require_golden

        def leak(name):
            require_golden(GOLDEN_MODEL)
            return (GOLDEN_DIR / f"{name}.json").read_text(encoding="utf-8")
        """, ["leak"], "덮지 않는다"),
    "gate-after-read": ("""
        from .golden import GOLDEN_DIR
        from .golden_gate import GOLDEN, require_golden

        def leak(name):
            text = (GOLDEN_DIR / f"{name}.json").read_text(encoding="utf-8")
            require_golden(GOLDEN)
            return text
        """, ["leak"], "첫 문장이 아니다"),
    "dead-gate": ("""
        from .golden import GOLDEN_DIR
        from .golden_gate import GOLDEN, require_golden

        def leak(name):
            if False:
                require_golden(GOLDEN)
            return (GOLDEN_DIR / f"{name}.json").read_text(encoding="utf-8")
        """, ["leak"], "첫 문장이 아니다"),
    # ── import 때 문지기를 거치는 함수를 부른다(골든 없는 체크아웃에서 수집 오류) ──
    "import-time-gated-reader": ("""
        from .golden import read_golden

        RECORD = read_golden("S1")
        """, ["<module>"], "문지기를 거치는 함수"),
    "import-time-gated-helper": ("""
        from .golden import GOLDEN_DIR
        from .golden_gate import GOLDEN, require_golden

        def _names():
            require_golden(GOLDEN)
            return sorted(path.stem for path in GOLDEN_DIR.glob("*.json"))

        NAMES = _names()
        """, ["<module>"], "문지기를 거치는 함수"),
    "import-time-bare-gate": ("""
        from .golden_gate import GOLDEN, require_golden

        require_golden(GOLDEN)
        """, ["<module>"], "문지기를 거치는 함수"),
    "import-time-module-level-gate": ("""
        from .golden import GOLDEN_DIR
        from .golden_gate import GOLDEN, require_golden

        def _names():
            require_golden(GOLDEN, allow_module_level=True)
            return sorted(path.stem for path in GOLDEN_DIR.glob("*.json"))

        NAMES = _names()
        """, [], ""),
    "import-time-local-name-shadows": ("""
        def read_golden(name):
            return {"name": name}

        RECORD = read_golden("S1")
        """, [], ""),
}


@pytest.mark.parametrize("case", sorted(_SELF_CHECK_SOURCES))
def test_static_audit_self_check(case):
    """감사가 실제로 위반을 잡는다 — 가짜 소스마다 위반 자리와 사유가 기대와 같다(위반 모양은 1건, 제대로 거친 소스는 0건).
    다른 모듈의 '문지기를 거치는 함수'(`read_golden` 등)는 실제 감사 대상에서 모은 이름을 쓴다."""
    source, expected, keyword = _SELF_CHECK_SOURCES[case]
    hits = audit_source(textwrap.dedent(source), "fake.py", _audited()[1])
    assert [hit.where for hit in hits] == expected, hits
    assert len(hits) == len(expected) <= 1
    assert all(keyword in hit.problem for hit in hits), hits
