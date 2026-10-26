"""경계 연속근무 상한 K 는 정본 `max_conseq_work` 만 읽는다 — EI-12 12a(D9 · 설계 §5.3 'EI-12 경계 별칭 읽기').

옛 폴백(정본이 비면 별칭 `max_consecutive_work_days` → 5)은 별칭이 생성 경로에 닿을 수 있던 때의 안전망이었다. 요청 별칭은 생성 진입에서 정본으로
바뀌므로(`engine_contract.aliases`) 경계 세 곳(월경계·월중 경계 빌더·출발 경계 규칙)은 정본 → 5 만 본다.
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

import pytest
from services.engine_input.boundary_fixed import rules_from_config

_SRC = Path(__file__).resolve().parents[2] / "app" / "services" / "roster_create_service.py"
_BUILDERS = ("build_cross_month_constraints", "build_mid_month_boundary_constraints")


def _builder_k(name: str, config_dict: dict) -> int:
    """빌더 원문의 `def _safe_int` 부터 `K = 5` 줄까지를 떼어 실행한 K(`test_d13_runtime_key._boundary_l` 방식)."""
    src = _SRC.read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef) and n.name == name)
    lines = src.splitlines()
    start = next(i for i in range(fn.lineno - 1, fn.end_lineno) if lines[i].strip().startswith("def _safe_int"))
    end = next(i for i in range(start, fn.end_lineno) if lines[i].strip() == "K = 5")
    ns: dict = {"config_dict": config_dict}
    exec(textwrap.dedent("\n".join(lines[start:end + 1])), ns)  # noqa: S102 — 경계 원문 그대로 실행
    return ns["K"]


@pytest.mark.parametrize("name", _BUILDERS)
def test_builders_read_the_canonical_key_only(name):
    assert _builder_k(name, {"max_conseq_work": 3}) == 3
    assert _builder_k(name, {"max_consecutive_work_days": 3}) == 5  # 별칭은 읽지 않는다 → 기본 5
    assert _builder_k(name, {"max_conseq_work": None, "max_consecutive_work_days": 3}) == 5
    assert _builder_k(name, {}) == 5


def test_departure_rules_read_the_canonical_key_only():
    assert rules_from_config({"max_conseq_work": 3}).K == 3
    assert rules_from_config({"max_consecutive_work_days": 3}).K == 5
    assert rules_from_config({}).K == 5


@pytest.mark.parametrize("name", _BUILDERS)
def test_builders_and_departure_rules_agree(name):
    for cfg in ({"max_conseq_work": 4}, {"max_conseq_work": "2"}, {"max_conseq_work": None}, {"max_consecutive_work_days": 4}, {}):
        assert _builder_k(name, cfg) == rules_from_config(cfg).K, cfg
