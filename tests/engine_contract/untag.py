"""이름표 역변환 — `hard_add(m, "id", X)` → `m.Add(X)` (설계서 §2.3 · EI-3 동작 불변 검증).

이름표 래퍼(`services.cp_sat.rule_tag`)는 받은 호출을 그대로 넘기므로, 래퍼를 원래 메서드 호출로
되돌린 AST 가 치환 전 파일의 AST 와 같으면 모델은 한 비트도 바뀌지 않았다.

  - hard_add / aux_add(recv, id, expr)            → recv.Add(expr)
  - hard_call / aux_call(recv, id, "Method", *a)  → recv.Method(*a)
  - `from services.cp_sat.rule_tag import ...` 줄은 지운다.

CLI(치환 커밋 때 일회성 대조 — 동결본은 저장소 밖에 둔다):
    python -m tests.engine_contract.untag <동결본 루트> <파일 상대경로>...
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

RULE_TAG_MODULE = "services.cp_sat.rule_tag"
ADD_WRAPPERS = frozenset({"hard_add", "aux_add"})
CALL_WRAPPERS = frozenset({"hard_call", "aux_call"})


class _Untag(ast.NodeTransformer):
    """래퍼 호출을 원래 메서드 호출로 되돌린다."""

    def visit_Call(self, node: ast.Call) -> ast.AST:
        self.generic_visit(node)
        func = node.func
        if not isinstance(func, ast.Name):
            return node
        if func.id in ADD_WRAPPERS and len(node.args) == 3 and not node.keywords:
            recv, _tag, expr = node.args
            return ast.Call(func=ast.Attribute(value=recv, attr="Add", ctx=ast.Load()), args=[expr], keywords=[])
        if (func.id in CALL_WRAPPERS and len(node.args) >= 3
                and isinstance(node.args[2], ast.Constant) and isinstance(node.args[2].value, str)):
            recv, _tag, method, *rest = node.args
            return ast.Call(func=ast.Attribute(value=recv, attr=method.value, ctx=ast.Load()),
                            args=rest, keywords=node.keywords)
        return node


def _drop_rule_tag_import(tree: ast.Module) -> ast.Module:
    """`from services.cp_sat.rule_tag import ...` 문을 모듈 어디에 있든 뺀다."""

    class _Drop(ast.NodeTransformer):
        def visit_ImportFrom(self, node: ast.ImportFrom):
            return None if node.module == RULE_TAG_MODULE else node

    return _Drop().visit(tree)


def untag_tree(source: str) -> ast.Module:
    """소스 → 래퍼를 되돌리고 rule_tag import 를 뺀 AST."""
    tree = _drop_rule_tag_import(ast.parse(source))
    return ast.fix_missing_locations(_Untag().visit(tree))


def same_as_base(current: str, base: str) -> bool:
    """역변환한 현재 소스와 치환 전 소스의 AST(위치 정보 제외)가 같은가."""
    return ast.dump(untag_tree(current)) == ast.dump(ast.parse(base))


def count_tags(source: str) -> dict[str, int]:
    """래퍼 호출 수(종류별)."""
    counts = dict.fromkeys(sorted(ADD_WRAPPERS | CALL_WRAPPERS), 0)
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in counts:
            counts[node.func.id] += 1
    return counts


def main(argv: list[str]) -> int:
    """동결본 루트와 상대경로들을 받아 파일마다 AST 일치를 보고한다. 하나라도 다르면 1."""
    base_root, *rels = argv
    bad = 0
    for rel in rels:
        ok = same_as_base(Path(rel).read_text(encoding="utf-8"),
                          (Path(base_root) / rel).read_text(encoding="utf-8"))
        bad += not ok
        print(f"{'같음' if ok else '다름'}  {rel}  {count_tags(Path(rel).read_text(encoding='utf-8'))}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
