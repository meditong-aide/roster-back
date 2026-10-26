"""`untag.py` 역변환 검증 — 이름표 치환 동작 불변의 기계적 근거(설계서 §2.3·§5.3 EI-3).

`untag.py`(`services.cp_sat.rule_tag` 래퍼 → 원래 CpModel 메서드 호출)는 EI-3 가 '모델이 한
비트도 바뀌지 않았다'고 주장하는 유일한 기계적 증거다. 이 파일은 그 증거 장치 자체를 검증한다.

구성
  ① 합성 왕복 — 이 파일 안에서만 쓰는 작은 순방향 태거(`_Tagger`/`_tag_source`, untag 의
     반대 방향)로 원본을 태그한 뒤 `untag.same_as_base` 로 되돌려 원본과 AST 가 같은지 본다.
     태거는 운영 EI-3 치환(사용자가 손으로 한 '같은 줄 수정')을 흉내만 낸 별개 구현이다 —
     같은 버그를 양쪽에 베껴 쓰면 왕복이 거짓으로 통과하므로, 일부러 `untag.py`·`rule_tag.py`
     와 전혀 다른 경로(AST 변환 + `ast.unparse`)로 짰다.
  ② 다름 판정 — 래퍼 안 식·메서드 이름·수신자가 바뀌거나, `rule_tag` 가 아닌 import 가
     실수로 함께 빠지면 `same_as_base` 가 False 를 내는지.
  ③ `rule_tag` import 제거 범위와 **한계** — 모듈 최상단·중간·함수 안 어디에 있어도 지우는지.
     ★한계: `_Untag`(`untag.py`)는 `ast.Name.id` 문자열만 보고 import 출처를 추적하지 않는다.
     그래서 `rule_tag` 와 무관한 로컬 함수가 우연히 `hard_add` 란 이름을 쓰면 그 호출도 똑같이
     되돌린다. 이 한계는 버그가 아니라 설계다 — 치환 대상 7개 파일 안에 그런 동명 함수가
     없다는 전제로 단순함을 산다(§2.3). 전제가 깨지면(새 파일에 동명 로컬 함수가 생기면)
     이 테스트가 보여주는 그대로 조용히 잘못 되돌린다 — A1 감사가 아니라 이 한계가 잡는다.
  ④ CLI(`main`) — 같음 0 · 다름 1.
  ⑤ 실치환 대상 7개 파일 — untag 하면 래퍼 호출이 완전히 0 이 되는지(동결본은 저장소 밖이라
     대조는 하지 않는다 — 대신 '완전히 되돌려짐'이라는 더 약하지만 저장소 안에서 잴 수 있는
     성질을 본다).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from .untag import RULE_TAG_MODULE, count_tags, main, same_as_base, untag_tree

_REPO_ROOT = Path(__file__).resolve().parents[2]

# 치환 대상 7개 파일(설계서 §2.3 — fallback_lex 145 · cp_sat_basic 85 · constraints 4파일 18 ·
# lookahead_constraints 2 = EI-3 동결본 기준 250곳). origin/dev 재수신(55dacd4) 이 새 제약 호출
# 11곳을 추가해 **261곳**이 정본이다(Tag 단계 2026-10-12 — ban_night_before_fixed_off_slack·
# lex_freeze_ban_night 3곳 · n2n_min_gap_slack·lex_freeze_n2n_gap(_forbid_n 리팩터로 옛 1곳이
# 3곳 되고 동결 2곳 추가 = net+4) · lex_freeze_coverage(stage1 서브솔브 재사용) 2곳 ·
# monthly_cap_slack·lex_freeze_monthly_cap 2곳 — 아래 test_real_tagged_files_are_actually_tagged
# 참고).
_TAGGED_FILES = (
    "app/services/cp_sat/fallback_lex.py",
    "app/services/cp_sat_basic.py",
    "app/services/constraints/grade_constraints.py",
    "app/services/constraints/team_grade_handoff_constraints.py",
    "app/services/constraints/team_constraints.py",
    "app/services/constraints/monthly_limit_constraints.py",
    "app/services/cp_sat/lookahead_constraints.py",
)

_WRAPPER_NAMES = frozenset({"hard_add", "aux_add", "hard_call", "aux_call"})
_MODEL_NAMES = frozenset({"m", "m2", "m3"})


# ── 작은 순방향 태거(이 파일 전용, untag 의 반대 방향) ──────────────────────


class _Tagger(ast.NodeTransformer):
    """`<model>.Method(...)` 호출에 이름표를 입힌다 — `untag_tree` 의 반대 방향.

    receiver 가 `model_names` 에 속한 단순 이름(Name)일 때만 건드린다. 그래서
    `hard_add(m, "rid", expr).OnlyEnforceIf([...])` 처럼 래퍼 호출 결과에 체인된
    `.OnlyEnforceIf(...)` 는(수신자가 Call 이라 Name 이 아니다) 건드리지 않는다 — 실제
    `hard_add`/`hard_call` 도 OnlyEnforceIf 를 감싸지 않는다(§2.3).
    """

    def __init__(self, rule_id: str, add_method: str, model_names: frozenset[str]):
        self.rule_id = rule_id
        self.add_method = add_method
        self.model_names = model_names

    def visit_Call(self, node: ast.Call) -> ast.AST:
        self.generic_visit(node)
        func = node.func
        if not isinstance(func, ast.Attribute) or not isinstance(func.value, ast.Name):
            return node
        if func.value.id not in self.model_names:
            return node
        recv, method, tag = func.value, func.attr, ast.Constant(value=self.rule_id)
        if method == self.add_method:
            new_args = [recv, tag, *node.args]
            wrapper = "hard_add"
        else:
            new_args = [recv, tag, ast.Constant(value=method), *node.args]
            wrapper = "hard_call"
        return ast.Call(func=ast.Name(id=wrapper, ctx=ast.Load()), args=new_args, keywords=node.keywords)


def _tag_source(source: str, *, rule_id: str = "rid", add_method: str = "Add",
                 model_names: frozenset[str] = _MODEL_NAMES) -> str:
    """`source` 안 모델 메서드 호출에 이름표를 입힌 소스 텍스트(합성 '치환 후')."""
    tree = ast.fix_missing_locations(_Tagger(rule_id, add_method, model_names).visit(ast.parse(source)))
    return ast.unparse(tree)


# ── ① 왕복 동일 ──────────────────────────────────────────────────────────


_ROUND_TRIP_CASES = {
    "simple_add": "m.Add(x <= 1)\n",
    "onlyenforceif_chain": "m.Add(expr).OnlyEnforceIf([a, b])\n",
    "onlyenforceif_chain_two_literals": "m.Add(expr).OnlyEnforceIf([lit1]).OnlyEnforceIf([lit2])\n",
    "call_wrapper_addexactlyone": "m.AddExactlyOne(a, b, c)\n",
    "multiline_call": "m.Add(\n    a + b <= c\n)\n",
    # 제너레이터가 단일 인자가 아니게 되면(hard_call 에 m·id·method 가 앞에 붙으므로) 명시
    # 괄호가 필요하다 — `ast.unparse` 가 이를 스스로 보장하는지를 본다.
    "generator_single_arg_needs_parens": "m.AddBoolOr(x for x in range(3))\n",
    # 소문자 별칭(ortools 의 `m.add` 등) — hard_add 는 메서드 이름을 "Add" 로 못 박으므로
    # add_method="Add" 와 글자가 다른 "add" 는 hard_call 로 빠져 원래 글자가 보존된다.
    "lowercase_alias_via_hard_call": "m.add(x <= 1)\n",
    "multiple_receivers_m2_m3": "m2.AddExactlyOne(a for a in choices)\nm3.Add(p + q <= r).OnlyEnforceIf([lit])\n",
    "receiver_not_a_model_untouched": "helper.Add(z)\n",
}


@pytest.mark.parametrize("base", _ROUND_TRIP_CASES.values(), ids=_ROUND_TRIP_CASES.keys())
def test_round_trip_tag_then_untag_matches_original(base: str) -> None:
    """합성 태거로 태그한 뒤 untag 하면 원본과 AST 가 같다."""
    assert same_as_base(_tag_source(base), base)


def test_round_trip_tagger_actually_produced_wrapper_calls() -> None:
    """위 라운드트립이 '아무것도 안 바뀌어서' 우연히 통과한 게 아님을 확인(새너티 체크).

    `receiver_not_a_model_untouched` 은 일부러 `_MODEL_NAMES` 밖의 수신자를 써 태거가
    아무것도 안 건드리는 것 자체가 기대 동작이라 이 사니티 체크에서 뺀다.
    """
    for case_id, base in _ROUND_TRIP_CASES.items():
        if case_id == "receiver_not_a_model_untouched":
            continue
        tagged = _tag_source(base)
        assert any(count_tags(tagged).values())
        assert tagged != base


# ── ② 다름 판정 ─────────────────────────────────────────────────────────


def test_detects_difference_when_wrapped_expr_changes() -> None:
    """래퍼 안의 식이 치환 전과 달라지면(오타·손으로 고치며 실수) 다르다고 판정한다."""
    base = 'm.Add(x <= 1)\n'
    tagged_with_changed_expr = 'hard_add(m, "rid", x <= 2)\n'
    assert not same_as_base(tagged_with_changed_expr, base)


def test_detects_difference_when_method_name_changes() -> None:
    """`hard_call` 의 메서드 이름 인자가 바뀌면 다르다고 판정한다."""
    base = 'm.AddExactlyOne(a, b)\n'
    tagged_with_changed_method = 'hard_call(m, "rid", "AddAtMostOne", a, b)\n'
    assert not same_as_base(tagged_with_changed_method, base)


def test_detects_difference_when_receiver_changes() -> None:
    """수신자(모델 객체)가 바뀌면(예: m → m2) 다르다고 판정한다."""
    base = 'm.Add(expr)\n'
    tagged_with_changed_receiver = 'hard_add(m2, "rid", expr)\n'
    assert not same_as_base(tagged_with_changed_receiver, base)


def test_detects_difference_when_unrelated_import_missing() -> None:
    """`rule_tag` 가 아닌 import 가 태깅 중 실수로 함께 빠지면 다르다고 판정한다.

    `_drop_rule_tag_import` 는 `services.cp_sat.rule_tag` 모듈만 지운다(§ 모듈 docstring).
    다른 import 를 지우는 것은 untag 의 몫이 아니므로, 빠진 채로 들어오면 그대로 다름으로
    잡혀야 한다 — 조용히 삼키면 안 된다.
    """
    base = 'import math\nm.Add(x)\n'
    tagged_missing_math = 'hard_add(m, "rid", x)\n'
    assert not same_as_base(tagged_missing_math, base)


def test_hard_add_cannot_safely_stand_in_for_lowercase_alias() -> None:
    """`hard_add` 는 메서드 이름을 "Add" 로 못 박는다 — 소문자 별칭 원본엔 못 쓴다.

    `m.add(x)`(소문자) 를 `hard_call(m, "rid", "add", x)` 로 태그해야 ①에서 왕복한다.
    실수로 `hard_add(m, "rid", x)` 로 태그하면 untag 가 `m.Add(x)`(대문자)로 복원해 원본과
    달라진다 — 이 차이가 바로 ①의 "lowercase_alias_via_hard_call" 케이스가 피해야 하는 함정.
    """
    base = 'm.add(x <= 1)\n'
    wrongly_tagged_with_hard_add = 'hard_add(m, "rid", x <= 1)\n'
    assert not same_as_base(wrongly_tagged_with_hard_add, base)


# ── ③ rule_tag import 제거 범위와 한계 ─────────────────────────────────


@pytest.mark.parametrize("tagged", [
    pytest.param(
        'from services.cp_sat.rule_tag import hard_add\n'
        'import math\n'
        'hard_add(m, "rid", x)\n',
        id="top_of_module",
    ),
    pytest.param(
        'import math\n'
        'x_flag = 1\n'
        'from services.cp_sat.rule_tag import hard_add\n'
        'hard_add(m, "rid", x_flag)\n',
        id="middle_of_module",
    ),
    pytest.param(
        'import math\n'
        'def f(m, x):\n'
        '    from services.cp_sat.rule_tag import hard_add\n'
        '    return hard_add(m, "rid", x)\n',
        id="inside_function_body",
    ),
])
def test_rule_tag_import_removed_regardless_of_location(tagged: str) -> None:
    """`rule_tag` import 는 최상단·중간·함수 안 어디에 있어도 지워진다."""
    tree = untag_tree(tagged)
    assert not any(
        isinstance(node, ast.ImportFrom) and node.module == RULE_TAG_MODULE
        for node in ast.walk(tree)
    )


def test_local_function_with_same_name_is_also_untagged_limitation() -> None:
    """한계(모듈 docstring ③): `rule_tag` 와 무관한 동명 로컬 함수도 되돌려진다.

    `hard_add` 를 로컬에서 직접 정의해 다른 일을 시키는 함수가 있어도, `_Untag` 는
    `ast.Name.id == "hard_add"` 라는 표면 이름만 보고 호출을 되돌린다. 치환 대상 7개
    파일에 그런 동명 함수가 없다는 전제 위에서만 안전하다(§2.3) — 이 테스트는 그 전제가
    깨졌을 때 조용히 틀린 결과를 낸다는 사실 자체를 실측한다. 버그로 고치지 않는다.
    """
    source = (
        "def hard_add(model, tag, expr):\n"
        "    return model.CustomMethod(expr)\n"
        "\n"
        "def g(fake_model, some_expr):\n"
        '    return hard_add(fake_model, "not-a-rule-tag-call", some_expr)\n'
    )
    tree = untag_tree(source)
    g_body = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "g")
    returned_call = g_body.body[0].value
    assert isinstance(returned_call, ast.Call)
    assert isinstance(returned_call.func, ast.Attribute)
    # 되돌려진 호출은 `fake_model.Add(some_expr)` 이 됐다 — 로컬 `hard_add` 의 실제 동작
    # (`model.CustomMethod(expr)`)과는 무관하다. 이름만 보고 뒤집었다는 뜻이다.
    assert returned_call.func.attr == "Add"
    assert returned_call.func.value.id == "fake_model"


# ── ④ CLI(main) ─────────────────────────────────────────────────────────


def _write_pair(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rel: str, *, base_src: str, current_src: str) -> Path:
    """base_root(동결본 흉내) 와 CWD(현재 파일) 에 짝을 심고 base_root 경로를 돌려준다."""
    base_root = tmp_path / "base_root"
    cwd_dir = tmp_path / "cwd"
    base_root.mkdir(exist_ok=True)
    cwd_dir.mkdir(exist_ok=True)
    (base_root / rel).parent.mkdir(parents=True, exist_ok=True)
    (cwd_dir / rel).parent.mkdir(parents=True, exist_ok=True)
    (base_root / rel).write_text(base_src, encoding="utf-8")
    (cwd_dir / rel).write_text(current_src, encoding="utf-8")
    monkeypatch.chdir(cwd_dir)
    return base_root


def test_cli_main_returns_zero_when_same(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                          capsys: pytest.CaptureFixture[str]) -> None:
    """같은 파일 쌍이면 0 을 돌려주고 '같음' 을 찍는다."""
    base_root = _write_pair(tmp_path, monkeypatch, "mod.py",
                             base_src="m.Add(x <= 1)\n",
                             current_src='hard_add(m, "rid", x <= 1)\n')
    rc = main([str(base_root), "mod.py"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "같음" in out and "mod.py" in out


def test_cli_main_returns_one_when_different(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                              capsys: pytest.CaptureFixture[str]) -> None:
    """다른 파일 쌍이면 1 을 돌려주고 '다름' 을 찍는다."""
    base_root = _write_pair(tmp_path, monkeypatch, "mod.py",
                             base_src="m.Add(x <= 1)\n",
                             current_src='hard_add(m, "rid", x <= 2)\n')
    rc = main([str(base_root), "mod.py"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "다름" in out and "mod.py" in out


def test_cli_main_mixed_files_returns_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                           capsys: pytest.CaptureFixture[str]) -> None:
    """여러 파일 중 하나라도 다르면 전체가 1 이고, 파일별 줄이 각각 찍힌다."""
    base_root = tmp_path / "base_root"
    cwd_dir = tmp_path / "cwd"
    base_root.mkdir()
    cwd_dir.mkdir()
    (base_root / "same.py").write_text("m.Add(x)\n", encoding="utf-8")
    (cwd_dir / "same.py").write_text('hard_add(m, "rid", x)\n', encoding="utf-8")
    (base_root / "diff.py").write_text("m.Add(y)\n", encoding="utf-8")
    (cwd_dir / "diff.py").write_text('hard_add(m, "rid", z)\n', encoding="utf-8")
    monkeypatch.chdir(cwd_dir)

    rc = main([str(base_root), "same.py", "diff.py"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "같음  same.py" in out
    assert "다름  diff.py" in out


# ── ⑤ 실치환 대상 7개 파일 — 완전히 되돌려지는지 ────────────────────────


@pytest.mark.parametrize("rel_path", _TAGGED_FILES)
def test_real_tagged_file_fully_reverts(rel_path: str) -> None:
    """untag 하면 `rule_tag` import·래퍍 호출이 전부 사라진다(동결본 대조 없이 보는 성질).

    동결본(`ei3/base/`)은 저장소 밖이라 여기서 값까지 대조하지 못한다. 대신 '7개 파일 전부
    래퍼 호출 0 곳' — 즉 치환 당시 한 곳도 `hard_call`/`aux_call` 인자 모양(위치 인자·문자열
    메서드 이름)을 벗어나지 않았다는, 저장소 안에서 잴 수 있는 더 약한 성질을 본다.
    """
    source = (_REPO_ROOT / rel_path).read_text(encoding="utf-8")
    tree = untag_tree(source)
    remaining_wrappers = [
        node.func.id for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _WRAPPER_NAMES
    ]
    assert remaining_wrappers == []
    assert not any(
        isinstance(node, ast.ImportFrom) and node.module == RULE_TAG_MODULE
        for node in ast.walk(tree)
    )


def test_real_tagged_files_are_actually_tagged() -> None:
    """위 ⑤가 '처음부터 래퍼가 없어서' 통과한 게 아님을 확인(새너티 체크).

    EI-3 동결본 기준은 250곳(§2.3)이었으나, origin/dev 재수신(55dacd4)이 추가한 새 제약
    호출 11곳에 이름표를 달아(Tag 단계 2026-10-12) 261곳이 됐다 — 늘어난 근거는 위
    `_TAGGED_FILES` 주석.
    """
    total = 0
    for rel_path in _TAGGED_FILES:
        source = (_REPO_ROOT / rel_path).read_text(encoding="utf-8")
        total += sum(count_tags(source).values())
    assert total == 261
