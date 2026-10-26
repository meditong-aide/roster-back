"""env 레지스트리 readers 완전성 계약 — 같은 함수 안의 읽기 횟수까지 맞춘다 (설계서 §2.6 · 재수신 흡수 `4ec454e` 리뷰).

왜 필요한가
  `EnvSpec.readers` 는 '이 env 를 어디서 읽는가' 를 손으로 적은 목록이다. 이름 집합만 보는
  `test_registry.py` 의 env 계약(엔진 경로 env ⊆ ENV 표)은 **이미 등록된 이름의 읽는 자리가 늘어도**
  못 잡는다 — 2026-10-20 dev `1bedd4d` 가 `SKIP_PRIMARY` 의 둘째 읽기를 같은 함수에 넣었을 때 readers 가
  하나만 남았는데도 모든 계약이 초록이었다. 이 테스트는 읽는 자리를 AST 로 다시 모아, 같은
  (파일, 함수, 방식, env 이름) 마다 **읽은 횟수**가 readers 와 같은지 본다.
  (파일, 함수, env 이름) 의 집합만 맞추면 위 사례도 통과한다 — 같은 함수 안의 둘째 읽기는 키가 같다.

비교하지 않는 것
  - 줄 번호: 읽기 자리 위의 코드가 바뀌면 밀린다(재수신마다 24~40줄씩 밀렸다). readers 의 줄은 참고 값이다.
  - 쓰기: `environ.pop`·`setdefault`·대입·삭제는 토글을 켜고 되돌리는 쓰기라 읽기로 세지 않는다.
  - `OUTSIDE_ENGINE` 행(worker·lambda 입구): 엔진 경로 밖이라 이 스캔 범위에 없다.

범위
  `test_registry.py` 의 넓은 스캔(`_broad_env_source_files`)과 같은 파일들이다 — 한쪽 범위가 바뀌면 같이 따라간다.
  읽기: `getenv(이름)` · `environ.get(이름)` · `environ[이름]`(읽기) · `이름 in environ`. 변수·상수로 넘긴 이름도
  `NAME = "X"` 대입을 따라가 푼다.

빨개지면
  새 읽기: `EnvSpec.readers` 에 `'services/<경로>:<줄>:<함수>:<방식>'` 을 더한다(그 env 의 `meaning` 에 효과도 적는다).
  사라진 읽기: 해당 항목을 지운다. 새 env 이름이면 `test_registry.py` 의 env 계약이 먼저 빨개진다.
"""

from __future__ import annotations

import ast
import importlib.util
from collections import Counter
from pathlib import Path

from services.engine_contract import env as E

from .test_registry import (
    _broad_env_source_files,
    _const_str_map,
    _getenv_aliases,
    _is_environ_like,
    _resolve_str_literal,
)

#: (경로 — services/ 아래 상대, 함수 경로, 방식, env 이름)
ReadKey = tuple[str, str, str, str]


class _ReadVisitor(ast.NodeVisitor):
    """env 읽기 자리를 (함수 경로, 방식, 이름) 으로 모은다 — 중첩 함수·클래스는 점으로 잇는다."""

    def __init__(self, const_map: dict[str, str], getenv_aliases: set[str]):
        self.const_map = const_map
        self.getenv_aliases = getenv_aliases
        self.scope: list[str] = []
        self.reads: list[tuple[str, str, str]] = []

    def _record(self, name_node: ast.expr, how: str) -> None:
        name = _resolve_str_literal(name_node, self.const_map)
        if name and name.isupper():
            self.reads.append((".".join(self.scope) or "<module>", how, name))

    def _enter_scope(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _enter_scope

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if node.args:
            if (isinstance(func, ast.Name) and func.id in self.getenv_aliases) or (
                isinstance(func, ast.Attribute) and func.attr == "getenv"
            ):
                self._record(node.args[0], "getenv")
            elif isinstance(func, ast.Attribute) and func.attr == "get" and _is_environ_like(func.value):
                self._record(node.args[0], "environ.get")
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if _is_environ_like(node.value) and isinstance(node.ctx, ast.Load):
            self._record(node.slice, "environ[]")
        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> None:
        if (len(node.ops) == 1 and isinstance(node.ops[0], ast.In)
                and node.comparators and _is_environ_like(node.comparators[0])):
            self._record(node.left, "in")
        self.generic_visit(node)


def _code_reads() -> Counter[ReadKey]:
    """엔진 경로 코드의 env 읽기 횟수 — 키별."""
    spec = importlib.util.find_spec("services")
    root = Path(next(iter(spec.submodule_search_locations)))
    reads: Counter[ReadKey] = Counter()
    for path in _broad_env_source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        visitor = _ReadVisitor(_const_str_map(tree), _getenv_aliases(tree))
        visitor.visit(tree)
        rel = path.relative_to(root).as_posix()
        for scope, how, name in visitor.reads:
            reads[(rel, scope, how, name)] += 1
    return reads


def _registry_reads() -> Counter[ReadKey]:
    """레지스트리 readers(`'services/<경로>:<줄>:<함수>:<방식>'`)의 항목 수 — 키별. 줄 번호는 버린다."""
    reads: Counter[ReadKey] = Counter()
    for spec in E.ENV:
        if spec.klass == E.OUTSIDE_ENGINE:
            continue
        for reader in spec.readers:
            path, _line, scope, how = reader.split(":", 3)
            reads[(path.removeprefix("services/"), scope, how, spec.name)] += 1
    return reads


def _describe(label: str, extra: Counter[ReadKey]) -> list[str]:
    """어긋난 키를 사람이 읽을 줄로 — 횟수가 1보다 크면 같이 적는다."""
    lines = [label]
    for (path, scope, how, name), count in sorted(extra.items()):
        lines.append(f"  {name}  {path}  {scope}  {how}" + (f"  ×{count}" if count > 1 else ""))
    return lines


def test_env_readers_match_code_reads():
    """ENV 표 `readers` 의 (파일, 함수, 방식, 이름)별 항목 수 = 코드의 같은 키 읽기 횟수 (줄 번호 제외)."""
    code, registry = _code_reads(), _registry_reads()
    missing_in_registry, missing_in_code = code - registry, registry - code
    report: list[str] = []
    if missing_in_registry:
        report += _describe("코드에는 있는데 readers 에 없는 읽기(EnvSpec.readers 에 더하라):", missing_in_registry)
    if missing_in_code:
        report += _describe("readers 에는 있는데 코드에 없는 읽기(지워진 읽기 — 항목을 지우라):", missing_in_code)
    assert not report, "\n".join(report)
