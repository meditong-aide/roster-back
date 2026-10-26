"""A1 이름표 감사 스캐너 테스트(설계서 §2.6 A1 · EI-3 '보류 중인 테스트 몫').

두 갈래다.
  ① 실제 app/ 을 스캔해 위반 0(치환 250곳·EXEMPT 실측·가정 위치 기준선이 전부 맞는지).
  ② 합성 소스로 규칙 하나씩을 잡는지(각 위반 종류가 실제로 빨개지는지 — 안 그러면
     감사가 있어도 아무 의미가 없다).

합성 테스트는 `tmp_path` 를 app_root 로 써서 실제 app/ 과 분리한다. `scan()` 은 AST 만
읽으므로 합성 파일은 문법만 맞으면 되고, import 가 실제로 풀려야 하는 건 아니다.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from tests.engine_contract import rule_audit as ra


def _write(root: Path, relpath: str, content: str) -> Path:
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dedent(content), encoding="utf-8")
    return path


def _by_kind(violations: list[ra.Violation], kind: str) -> list[ra.Violation]:
    return [v for v in violations if v.kind == kind]


_DEAD_ID_KINDS = {"dead_rule_id", "dead_rule_prefix"}


def _non_dead(violations: list[ra.Violation]) -> list[ra.Violation]:
    """합성(tmp_path) 트리는 app/ 전체가 아니라 RULES 의 대부분이 '안 쓰였다'로 나온다.
    9) 죽은 id 검사 자체는 별도 테스트(`test_dead_rule_id_detected_on_near_empty_tree`)로
    보고, 다른 검사를 보는 테스트에서는 이 잡음을 뺀다."""
    return [v for v in violations if v.kind not in _DEAD_ID_KINDS]


# ── ① 실제 app/ ──────────────────────────────────────────────────────────


def test_scan_real_app_zero_violations():
    """EI-3 치환 250곳 + EXEMPT 실측 + 가정 위치 기준선이 전부 맞으면 위반 0."""
    app_root = Path(__file__).resolve().parents[2] / "app"
    violations = ra.scan(app_root)
    assert violations == [], "\n".join(f"{v.kind}: {v.message}" for v in violations)


# ── ② 1) 제약/힌트/결정전략/가정 분류 ────────────────────────────────────


def test_hint_decision_assumption_excluded_from_constraint_names():
    assert "AddHint" in ra.HINT_NAMES and "add_hint" in ra.HINT_NAMES
    assert "AddDecisionStrategy" in ra.DECISION_STRATEGY_NAMES
    assert "AddAssumption" in ra.ASSUMPTION_NAMES and "AddAssumptions" in ra.ASSUMPTION_NAMES
    assert ra.HINT_NAMES.isdisjoint(ra.CONSTRAINT_NAMES)
    assert ra.DECISION_STRATEGY_NAMES.isdisjoint(ra.CONSTRAINT_NAMES)
    assert ra.ASSUMPTION_NAMES.isdisjoint(ra.CONSTRAINT_NAMES)
    assert "Add" in ra.CONSTRAINT_NAMES and "AddExactlyOne" in ra.CONSTRAINT_NAMES
    assert "add_map_domain" in ra.CONSTRAINT_NAMES  # 소문자만 있는 별칭(ortools 9.12)


def test_add_hint_not_flagged_even_in_target_module(tmp_path):
    """AddHint 는 해 힌트일 뿐이라 대상 모듈이라도 이름표 없이 써도 된다."""
    _write(tmp_path, "services/cp_sat/fallback_lex.py", """
        def f(m2):
            m2.AddHint([x], [1])
    """)
    violations = ra.scan(tmp_path)
    assert _non_dead(violations) == []


# ── ② 3) 대상 모듈 0 ───────────────────────────────────────────────────────


def test_raw_add_call_in_target_module_detected(tmp_path):
    _write(tmp_path, "services/cp_sat/fallback_lex.py", """
        def f(m):
            m.Add(x <= 1)
    """)
    violations = ra.scan(tmp_path)
    hits = _by_kind(violations, "raw_call_in_target")
    assert len(hits) == 1
    assert "services/cp_sat/fallback_lex.py:" in hits[0].message
    assert "m.Add(...)" in hits[0].message


def test_raw_add_exactly_one_call_in_target_module_detected(tmp_path):
    """.Add 뿐 아니라 AddExactlyOne 같은 *_call 대상 메서드도 잡는다."""
    _write(tmp_path, "services/constraints/team_constraints.py", """
        def f(m):
            m.AddExactlyOne([a, b])
    """)
    violations = ra.scan(tmp_path)
    assert len(_by_kind(violations, "raw_call_in_target")) == 1


# ── ② 2) 모호한 이름("add")은 모델 수신자일 때만 ────────────────────────────


def test_plain_set_add_in_target_module_not_flagged(tmp_path):
    """`collected.add(x)` 는 set.add 다 — 대상 모듈이라도 위반이 아니다."""
    _write(tmp_path, "services/cp_sat/fallback_lex.py", """
        def f():
            collected = set()
            collected.add(1)
    """)
    violations = ra.scan(tmp_path)
    assert _non_dead(violations) == []


def test_lowercase_add_alias_on_model_receiver_detected(tmp_path):
    """ortools 의 소문자 별칭 m.add(...) 로 A1 을 우회하는 자리도 잡는다."""
    _write(tmp_path, "services/cp_sat/fallback_lex.py", """
        def f(m):
            m.add(x <= 1)
    """)
    violations = ra.scan(tmp_path)
    hits = _by_kind(violations, "raw_call_in_target")
    assert len(hits) == 1
    assert "m.add(...)" in hits[0].message


# ── ② 4) 면제 모듈 실측 고정 / 그 밖 모듈 0(새 파일 우회 차단) ───────────────


def test_raw_call_in_unlisted_module_detected(tmp_path):
    _write(tmp_path, "services/cp_sat/brand_new_rule.py", """
        def f(m):
            m.Add(x <= 1)
    """)
    violations = ra.scan(tmp_path)
    hits = _by_kind(violations, "raw_call_in_new_module")
    assert len(hits) == 1


def test_exempt_module_over_baseline_detected(tmp_path):
    """services/cp_sat/rule_tag.py 의 실측 기준(2)보다 많으면 실패."""
    _write(tmp_path, "services/cp_sat/rule_tag.py", """
        def hard_add(m, rule_id, expr):
            return m.Add(expr)

        def aux_add(m, tag, expr):
            return m.Add(expr)

        def extra(m, expr):
            return m.Add(expr)
    """)
    violations = ra.scan(tmp_path)
    hits = _by_kind(violations, "exempt_count_mismatch")
    assert len(hits) == 1
    assert "2곳에서" in hits[0].message or "2" in hits[0].message


def test_exempt_module_at_baseline_is_clean(tmp_path):
    _write(tmp_path, "services/cp_sat/rule_tag.py", """
        def hard_add(m, rule_id, expr):
            return m.Add(expr)

        def aux_add(m, tag, expr):
            return m.Add(expr)
    """)
    violations = ra.scan(tmp_path)
    assert _by_kind(violations, "exempt_count_mismatch") == []


# ── ② 5) 우회 형태 ─────────────────────────────────────────────────────────


def test_bypass_extracted_as_value_detected(tmp_path):
    _write(tmp_path, "services/cp_sat/fallback_lex.py", """
        import functools

        def f(m):
            g = m.Add
            return functools.partial(m.Add)
    """)
    violations = ra.scan(tmp_path)
    hits = _by_kind(violations, "bypass_extracted")
    assert len(hits) == 2


def test_bypass_getattr_literal_detected(tmp_path):
    _write(tmp_path, "services/cp_sat/fallback_lex.py", """
        def f(m, expr):
            return getattr(m, "Add")(expr)
    """)
    violations = ra.scan(tmp_path)
    assert len(_by_kind(violations, "bypass_getattr")) == 1


def test_wrapper_getattr_with_variable_method_not_flagged(tmp_path):
    """rule_tag 래퍼 자신의 getattr(m, method) (method 가 변수) 는 안 잡힌다."""
    _write(tmp_path, "services/cp_sat/rule_tag.py", """
        def hard_call(m, rule_id, method, *args, **kwargs):
            return getattr(m, method)(*args, **kwargs)

        def hard_add(m, rule_id, expr):
            return m.Add(expr)

        def aux_add(m, tag, expr):
            return m.Add(expr)
    """)
    violations = ra.scan(tmp_path)
    assert _by_kind(violations, "bypass_getattr") == []
    assert _by_kind(violations, "exempt_count_mismatch") == []


def test_wrapper_redefinition_outside_rule_tag_detected(tmp_path):
    _write(tmp_path, "services/cp_sat/sneaky.py", """
        def hard_add(m, rule_id, expr):
            return m.Add(expr)
    """)
    violations = ra.scan(tmp_path)
    assert len(_by_kind(violations, "bypass_wrapper_rebind")) == 1


def test_wrapper_rebind_assignment_detected(tmp_path):
    _write(tmp_path, "services/cp_sat/sneaky.py", """
        from services.cp_sat.rule_tag import hard_add

        hard_add = None
    """)
    violations = ra.scan(tmp_path)
    assert len(_by_kind(violations, "bypass_wrapper_rebind")) == 1


def test_wrapper_import_renamed_detected(tmp_path):
    """real 래퍼를 별칭으로 숨기는 import."""
    _write(tmp_path, "services/cp_sat/sneaky.py", """
        from services.cp_sat.rule_tag import hard_add as ha

        def f(m, expr):
            return ha(m, "forbidden_shift", expr)
    """)
    violations = ra.scan(tmp_path)
    hits = _by_kind(violations, "bypass_import_alias")
    assert len(hits) == 1
    assert "별칭" in hits[0].message


def test_wrapper_import_impersonation_detected(tmp_path):
    """다른 모듈의 이름을 래퍼 이름으로 가져오는 위장."""
    _write(tmp_path, "services/cp_sat/sneaky.py", """
        from some.other.module import something_else as hard_add
    """)
    violations = ra.scan(tmp_path)
    hits = _by_kind(violations, "bypass_import_alias")
    assert len(hits) == 1
    assert "위장" in hits[0].message


def test_plain_rule_tag_import_not_flagged(tmp_path):
    """`from services.cp_sat.rule_tag import hard_add` (별칭 없음) 은 정상."""
    _write(tmp_path, "services/constraints/team_constraints.py", """
        from services.cp_sat.rule_tag import hard_add

        def f(m, x):
            hard_add(m, "forbidden_shift", x == 1)
    """)
    violations = ra.scan(tmp_path)
    assert _by_kind(violations, "bypass_import_alias") == []
    assert _by_kind(violations, "missing_rule_tag_import") == []


def test_missing_rule_tag_import_detected(tmp_path):
    _write(tmp_path, "services/constraints/team_constraints.py", """
        def f(m, x):
            hard_add(m, "forbidden_shift", x == 1)
    """)
    violations = ra.scan(tmp_path)
    assert len(_by_kind(violations, "missing_rule_tag_import")) == 1


# ── ② 6·7) id 등록·strength 일치 ────────────────────────────────────────


def test_unregistered_literal_id_detected(tmp_path):
    _write(tmp_path, "services/constraints/team_constraints.py", """
        from services.cp_sat.rule_tag import hard_add

        def f(m, x):
            hard_add(m, "no_such_rule_id", x == 1)
    """)
    violations = ra.scan(tmp_path)
    hits = _by_kind(violations, "id_unregistered")
    assert len(hits) == 1
    assert "no_such_rule_id" in hits[0].message


def test_unregistered_fstring_prefix_detected(tmp_path):
    _write(tmp_path, "services/constraints/team_constraints.py", """
        from services.cp_sat.rule_tag import hard_add

        def f(m, x, src):
            hard_add(m, f"no_such_prefix:{src}", x == 1)
    """)
    violations = ra.scan(tmp_path)
    assert len(_by_kind(violations, "id_fstring_prefix_unregistered")) == 1


def test_registered_fstring_prefix_not_flagged(tmp_path):
    _write(tmp_path, "services/constraints/monthly_limit_constraints.py", """
        from services.cp_sat.rule_tag import hard_add

        def f(m, x, code):
            hard_add(m, f"monthly_limit_{code}", x == 1)
    """)
    violations = ra.scan(tmp_path)
    assert _non_dead(violations) == []


def test_disallowed_name_id_detected(tmp_path):
    _write(tmp_path, "services/constraints/team_constraints.py", """
        from services.cp_sat.rule_tag import hard_add

        def f(m, x, rid):
            hard_add(m, rid, x == 1)
    """)
    violations = ra.scan(tmp_path)
    assert len(_by_kind(violations, "id_name_not_allowed")) == 1


def test_allowed_dynamic_name_id_not_flagged(tmp_path):
    """DYNAMIC_ID_VARS 허용 목록의 (fallback_lex.py, _fixed_pattern) — 순수한 형태면 통과."""
    _write(tmp_path, "services/cp_sat/fallback_lex.py", """
        from services.cp_sat.rule_tag import hard_add

        def _fixed_pattern_from_source(source):
            return f"fixed_assignment:{source or 'manual'}"

        def f(m, x, src):
            _fixed_pattern = _fixed_pattern_from_source(src)
            hard_add(m, _fixed_pattern, x == 1)
    """)
    violations = ra.scan(tmp_path)
    assert _non_dead(violations) == []


def test_strength_mismatch_aux_wrapper_with_hard_id_detected(tmp_path):
    """max_night 은 HARD 다 — aux_add 로 달면 strength 불일치."""
    _write(tmp_path, "services/constraints/team_constraints.py", """
        from services.cp_sat.rule_tag import aux_add

        def f(m, x):
            aux_add(m, "max_night", x <= 15)
    """)
    violations = ra.scan(tmp_path)
    hits = _by_kind(violations, "strength_mismatch")
    assert len(hits) == 1
    assert "max_night" in hits[0].message


def test_strength_mismatch_hard_wrapper_with_aux_id_detected(tmp_path):
    """coverage_short 는 AUX 다 — hard_add 로 달면 strength 불일치."""
    _write(tmp_path, "services/constraints/team_constraints.py", """
        from services.cp_sat.rule_tag import hard_add

        def f(m, x):
            hard_add(m, "coverage_short", x >= 0)
    """)
    violations = ra.scan(tmp_path)
    assert len(_by_kind(violations, "strength_mismatch")) == 1


def test_matching_strength_not_flagged(tmp_path):
    _write(tmp_path, "services/constraints/team_constraints.py", """
        from services.cp_sat.rule_tag import hard_add, aux_add

        def f(m, x, y):
            hard_add(m, "max_night", x <= 15)
            aux_add(m, "coverage_short", y >= 0)
    """)
    violations = ra.scan(tmp_path)
    assert _non_dead(violations) == []


# ── ② 8) add_hard(...) meta["pattern"] ──────────────────────────────────


def test_add_hard_pattern_unregistered_detected(tmp_path):
    _write(tmp_path, "services/cp_sat_basic.py", """
        def f(m, registry):
            try:
                from services.cp_sat.hard_assumption import add_hard as _add_hard
            except ImportError:
                _add_hard = None
            if _add_hard is not None:
                _add_hard(
                    m, registry, name="n",
                    constraint_expr=(1 == 1),
                    meta={"pattern": "not_a_registered_pattern"},
                )
    """)
    violations = ra.scan(tmp_path)
    hits = _by_kind(violations, "id_unregistered")
    assert len(hits) == 1
    assert "not_a_registered_pattern" in hits[0].message


def test_add_hard_meta_missing_detected(tmp_path):
    _write(tmp_path, "services/cp_sat_basic.py", """
        from services.cp_sat.hard_assumption import add_hard as _add_hard

        def f(m, registry):
            _add_hard(m, registry, name="n", constraint_expr=(1 == 1))
    """)
    violations = ra.scan(tmp_path)
    assert len(_by_kind(violations, "add_hard_meta_missing")) == 1


def test_add_hard_variable_bound_meta_dict_is_resolved(tmp_path):
    """meta=_meta 처럼 변수로 넘겨도 그 대입을 되짚어 pattern 을 검증한다(그래서 '없음'으로
    잘못 넘어가지 않는다 — grade_constraints.py 실제 모양)."""
    _write(tmp_path, "services/cp_sat_basic.py", """
        from services.cp_sat.hard_assumption import add_hard as _add_hard

        def f(m, registry):
            for g in (1, 2):
                _meta = {"pattern": "not_a_registered_pattern", "scope_key": g}
                _add_hard(m, registry, name="n", constraint_expr=(1 == 1), meta=_meta)
    """)
    violations = ra.scan(tmp_path)
    hits = _by_kind(violations, "id_unregistered")
    assert len(hits) == 1
    assert "not_a_registered_pattern" in hits[0].message
    assert _by_kind(violations, "add_hard_meta_missing") == []


def test_add_hard_registered_pattern_not_flagged(tmp_path):
    _write(tmp_path, "services/cp_sat_basic.py", """
        from services.cp_sat.hard_assumption import add_hard as _add_hard

        def f(m, registry):
            _add_hard(m, registry, name="n", constraint_expr=(1 == 1), meta={"pattern": "max_night"})
    """)
    violations = ra.scan(tmp_path)
    assert _non_dead(violations) == []


# ── ② 6-보강) 동적 변수 id 의 대입/함수 순수성 ───────────────────────────


def test_dynamic_id_var_impure_assignment_detected(tmp_path):
    _write(tmp_path, "services/cp_sat/fallback_lex.py", """
        from services.cp_sat.rule_tag import hard_add

        def _fixed_pattern_from_source(source):
            return f"fixed_assignment:{source or 'manual'}"

        def f(m, x):
            _fixed_pattern = "literal_not_from_producer"
            hard_add(m, _fixed_pattern, x == 1)
    """)
    violations = ra.scan(tmp_path)
    assert len(_by_kind(violations, "dynamic_id_var_not_pure")) == 1


def test_dynamic_id_producer_impure_return_detected(tmp_path):
    _write(tmp_path, "services/cp_sat/fallback_lex.py", """
        from services.cp_sat.rule_tag import hard_add

        def _fixed_pattern_from_source(source):
            return source or "manual"

        def f(m, x):
            _fixed_pattern = _fixed_pattern_from_source("manual")
            hard_add(m, _fixed_pattern, x == 1)
    """)
    violations = ra.scan(tmp_path)
    assert len(_by_kind(violations, "dynamic_id_producer_not_pure")) == 1


# ── ② 9) 죽은 id 없음 ────────────────────────────────────────────────────


def test_dead_rule_id_detected_on_near_empty_tree(tmp_path):
    """아무것도 안 쓰는 트리를 스캔하면 RULES·RULE_PREFIXES 전부가 죽은 id 로 잡힌다
    (= 탐지 자체가 동작한다는 뜻 — 실제 app/ 스캔(①)이 전부 쓰인다는 걸 보장한다)."""
    _write(tmp_path, "services/empty.py", "x = 1\n")
    violations = ra.scan(tmp_path)
    dead_ids = {v.message for v in _by_kind(violations, "dead_rule_id")}
    dead_prefixes = _by_kind(violations, "dead_rule_prefix")
    assert any("max_night" in m for m in dead_ids)
    assert len(dead_ids) == len(ra.RULES)
    assert len(dead_prefixes) == len(ra.RULE_PREFIXES)


def test_rule_id_used_only_via_add_hard_pattern_not_dead(tmp_path):
    """RULES id 가 wrapper 가 아니라 add_hard meta pattern 으로만 쓰여도 죽은 id 가 아니다."""
    _write(tmp_path, "services/cp_sat_basic.py", """
        from services.cp_sat.hard_assumption import add_hard as _add_hard

        def f(m, registry):
            _add_hard(m, registry, name="n", constraint_expr=(1 == 1), meta={"pattern": "max_night"})
    """)
    violations = ra.scan(tmp_path)
    dead_ids = {v.message for v in _by_kind(violations, "dead_rule_id")}
    assert not any("\"max_night\"" in m for m in dead_ids)


# ── ② 가정(assumption) 위치 — hard_assumption.py 밖은 측정·고정 ─────────────


def test_assumption_in_home_file_always_allowed(tmp_path):
    _write(tmp_path, "services/cp_sat/hard_assumption.py", """
        def add_hard(model, registry, *, name, constraint_expr, meta=None):
            model.Add(constraint_expr)
            model.Add(constraint_expr)

        class R:
            def solve(self, model):
                model.AddAssumptions([1, 2, 3])
    """)
    violations = ra.scan(tmp_path)
    assert _non_dead(violations) == []


def test_assumption_outside_home_in_new_file_detected(tmp_path):
    _write(tmp_path, "services/cp_sat/brand_new_diagnosis.py", """
        def run(model):
            model.AddAssumptions([1, 2, 3])
    """)
    violations = ra.scan(tmp_path)
    hits = _by_kind(violations, "assumption_outside_home")
    assert len(hits) == 1
    assert ra.ASSUMPTION_HOME in hits[0].message


@pytest.mark.parametrize("relpath", sorted(ra.TARGET_MODULES))
def test_every_target_module_path_is_distinct_file(relpath):
    """TARGET_MODULES 상수 자체의 형태 점검(오타로 같은 파일을 두 번 적지 않았는지)."""
    assert relpath.endswith(".py")
    assert not relpath.startswith("app/")


def test_exempt_and_target_modules_disjoint():
    assert ra.TARGET_MODULES.isdisjoint(set(ra.EXEMPT))


# ── ③ 2026-10-13 변이 보강(x01~x20·"17"·메시지 수정) — 10-11 변이 시험이 못 잡은 형태 ──
#
# 각 테스트 이름 끝의 (xNN)은 지시문의 변이 번호다. x17 은 정적 감사의 본질적 한계로
# 남기고(모델 지문은 별도 갈래 B 가 본다) 여기서 다루지 않는다.


# ── x01 — 모델 별칭(`_mm = m`) + 소문자 add ──────────────────────────────────


def test_model_alias_lowercase_add_detected(tmp_path):
    """x01 — `_mm = m` 로 모델을 별칭에 옮긴 뒤 `_mm.add(...)` 로 부르는 자리.
    §기본값-의심 스코프(대상 모듈 안의 "add" 는 컨테이너로 증명되지 않으면 전부 의심)로
    별도 별칭 추적 없이도 잡힌다."""
    _write(tmp_path, "services/cp_sat/fallback_lex.py", """
        def f(m, x):
            _mm = m
            _mm.add(x == 1)
    """)
    violations = ra.scan(tmp_path)
    hits = _by_kind(violations, "raw_call_in_target")
    assert len(hits) == 1
    assert "_mm.add(...)" in hits[0].message


# ── x02 — 타입주석 없는 매개변수(새 모듈) ────────────────────────────────────


def test_unannotated_model_param_new_module_detected(tmp_path):
    """x02 — 새 모듈에서 타입주석 없는 매개변수(cp)로 `.add(...)` 를 부르는 자리.
    cp_model 을 import 하는 모듈이면 매개변수 이름·주석과 무관하게 §기본값-의심
    스코프로 잡힌다."""
    _write(tmp_path, "services/cp_sat/brand_new_rule.py", """
        from ortools.sat.python import cp_model

        def f(cp, x):
            cp.add(x == 1)
    """)
    violations = ra.scan(tmp_path)
    hits = _by_kind(violations, "raw_call_in_new_module")
    assert len(hits) == 1
    assert "cp.add(...)" in hits[0].message


def test_new_constraints_module_without_cp_model_import_detected(tmp_path):
    """x02 10-11 원형 — 새 constraints 모듈이 cp_model 을 아예 import 하지 않고(기존
    services/constraints/*.py 4개·lookahead_constraints.py 의 실제 관례와 같다), 모델을
    `cp` 라는 매개변수로 받아 소문자 `cp.add(...)` 를 부르는 자리. 전에는 §기본값-의심
    스코프가 TARGET_MODULES∪EXEMPT∪imports_cp_model 뿐이라, 이 새 파일(아직 TARGET_
    MODULES 에 없다)이 cp_model import 도 없으면 완전히 안 보였다."""
    _write(tmp_path, "services/constraints/brand_new_rule.py", """
        def f(cp, x):
            cp.add(x == 1)
    """)
    violations = ra.scan(tmp_path)
    hits = _by_kind(violations, "raw_call_in_new_module")
    assert len(hits) == 1
    assert "cp.add(...)" in hits[0].message


def test_container_subscript_of_proven_container_not_flagged(tmp_path):
    """오탐 방지 회귀 — 증명된 컨테이너(딕셔너리)의 구독식(`forced_off[key].add(...)`)은
    §기본값-의심 스코프 안에서도 모델로 의심하면 안 된다(실제 app 모양 —
    mid_feasibility.py·feasibility_alerts.py)."""
    _write(tmp_path, "services/constraints/brand_new_rule.py", """
        def f(key, d):
            forced_off: dict = {}
            forced_off[key] = set()
            forced_off[key].add(int(d))
    """)
    violations = ra.scan(tmp_path)
    assert _non_dead(violations) == []


# ── x16 — `cp_model.CpModel.add(m, ...)` 바인드 안 된 클래스 메서드 호출 ──────


def test_unbound_classmethod_add_call_detected(tmp_path):
    """x16 — 인스턴스가 아니라 클래스 자체에서 `.add` 를 꺼내 첫 인자로 모델을 넘기는
    자리(수신자 텍스트가 'cp_model.CpModel' 이라 닫힌 model_names 집합에는 없지만,
    대상 모듈 §기본값-의심 스코프가 잡는다)."""
    _write(tmp_path, "services/cp_sat/fallback_lex.py", """
        from ortools.sat.python import cp_model

        def f(m, x):
            cp_model.CpModel.add(m, x == 1)
    """)
    violations = ra.scan(tmp_path)
    hits = _by_kind(violations, "raw_call_in_target")
    assert len(hits) == 1


# ── x08 — `m.Proto().constraints.add()` protobuf 직접 조작 ──────────────────


def test_proto_direct_manipulation_add_detected(tmp_path):
    """x08 — ortools 래퍼를 거치지 않고 protobuf 를 직접 건드려 제약을 추가하는 자리."""
    _write(tmp_path, "services/cp_sat_basic.py", """
        def f(m):
            m.Proto().constraints.add()
    """)
    violations = ra.scan(tmp_path)
    hits = _by_kind(violations, "raw_call_in_target")
    assert len(hits) == 1


# ── x06 — getattr 비-리터럴(문자열 결합 등)로 메서드를 동적으로 꺼내기 ────────


def test_getattr_dynamic_concat_on_model_detected(tmp_path):
    """x06 — `getattr(m, "Add" + "BoolOr")` 처럼 비-리터럴 식으로 제약 메서드를 꺼내는
    자리(기존 `_bypass_getattr_literal` 은 리터럴만 봐서 이 형태를 완전히 놓쳤다)."""
    _write(tmp_path, "services/cp_sat/fallback_lex.py", """
        def f(m, x):
            return getattr(m, "Add" + "BoolOr")([x])
    """)
    violations = ra.scan(tmp_path)
    hits = _by_kind(violations, "bypass_getattr_dynamic")
    assert len(hits) == 1


def test_getattr_dynamic_on_non_model_receiver_not_flagged(tmp_path):
    """오탐 방지 회귀 — `getattr(nu, f"{prefix}_min", None)` 처럼 모델이 아닌 수신자의
    동적 getattr 는 대상 모듈 안에서도 잡히면 안 된다(실측: 실제 app/ 의
    monthly_limit_constraints.py·objective_terms.py 에 이런 호출이 실재한다 — 그래서 이
    검사는 넓은 §기본값-의심 스코프가 아니라 좁은 model_names 집합만 쓴다)."""
    _write(tmp_path, "services/constraints/monthly_limit_constraints.py", """
        def f(nu, prefix):
            return getattr(nu, f"{prefix}_min", None)
    """)
    violations = ra.scan(tmp_path)
    assert _by_kind(violations, "bypass_getattr_dynamic") == []


def test_wrapper_getattr_dynamic_in_rule_tag_home_not_flagged(tmp_path):
    """rule_tag.py 자신의 `getattr(m, method)`(method 는 hard_call/aux_call 매개변수)는
    그 모듈이 유일한 정의처이므로 비-리터럴 getattr 검사에서도 제외된다."""
    _write(tmp_path, "services/cp_sat/rule_tag.py", """
        def hard_call(m, rule_id, method, *args, **kwargs):
            return getattr(m, method)(*args, **kwargs)

        def hard_add(m, rule_id, expr):
            return m.Add(expr)

        def aux_add(m, tag, expr):
            return m.Add(expr)
    """)
    violations = ra.scan(tmp_path)
    assert _by_kind(violations, "bypass_getattr_dynamic") == []


# ── x07 — `__getattribute__`/`__getattr__` dunder 우회 ──────────────────────


def test_dunder_getattribute_on_model_detected(tmp_path):
    """x07 — `m.__getattribute__("Add")` 로 제약 메서드를 우회해서 꺼내는 자리."""
    _write(tmp_path, "services/cp_sat/fallback_lex.py", """
        def f(m, x):
            return m.__getattribute__("Add")(x == 1)
    """)
    violations = ra.scan(tmp_path)
    assert len(_by_kind(violations, "bypass_dunder_getattr")) == 1


# ── x03·x15 — 모듈 별칭 import 뒤 속성 호출(`_rt.hard_add(...)`·`_ha.add_hard(...)`) ──


def test_module_alias_import_wrapper_call_unregistered_id_detected(tmp_path):
    """x03 — `import services.cp_sat.rule_tag as _rt` 뒤 `_rt.hard_add(...)` 로 부르는
    자리도 id 등록 검증이 걸려야 한다(전에는 Name 호출만 봐서 완전히 안 보였다).
    모듈 별칭 import 자체도 별도 우회 형태로 같이 잡힌다."""
    _write(tmp_path, "services/constraints/team_constraints.py", """
        import services.cp_sat.rule_tag as _rt

        def f(m, x):
            _rt.hard_add(m, "brand_new_rule", x == 1)
    """)
    violations = ra.scan(tmp_path)
    hits = _by_kind(violations, "id_unregistered")
    assert len(hits) == 1
    assert "brand_new_rule" in hits[0].message
    assert len(_by_kind(violations, "bypass_module_alias_import")) == 1


def test_module_alias_add_hard_pattern_checked(tmp_path):
    """x15 — `_ha.add_hard(...)` 모듈 별칭 속성 호출도 meta["pattern"] 검증이 걸린다."""
    _write(tmp_path, "services/cp_sat_basic.py", """
        import services.cp_sat.hard_assumption as _ha

        def f(m, registry):
            _ha.add_hard(
                m, registry, name="n",
                constraint_expr=(1 == 1),
                meta={"pattern": "not_a_registered_pattern"},
            )
    """)
    violations = ra.scan(tmp_path)
    hits = _by_kind(violations, "id_unregistered")
    assert len(hits) == 1
    assert "not_a_registered_pattern" in hits[0].message
    assert len(_by_kind(violations, "bypass_module_alias_import")) == 1


# ── 2026-10-13 재보강 — x03/x15 의 10-11 원형(ImportFrom)·이웃 형태(p03b/p03c/r12/r24) ──
# 아래 다섯 테스트는 전에 위반 0 이었다(리뷰 지적) — `_module_import_aliases`·
# `_resolve_wrapper_call`·`_resolve_add_hard_call`·`_bypass_module_alias_import` 가 Import
# 별칭(asname)만 보고 ImportFrom·완전 점 경로·동적 import 를 놓쳤기 때문이다.


def test_module_from_import_alias_wrapper_call_unregistered_id_detected(tmp_path):
    """x03 10-11 원형 — `from services.cp_sat import rule_tag as _rt` (ImportFrom, Import 가
    아니다) 뒤 `_rt.hard_add(...)`. 이전 보강 테스트(위)는 `import ... .rule_tag as _rt`
    (Import) 형태라서 이 원형과 다른 형태를 시험했었다."""
    _write(tmp_path, "services/constraints/team_constraints.py", """
        from services.cp_sat import rule_tag as _rt

        def f(m, x):
            _rt.hard_add(m, "brand_new_rule", x == 1)
    """)
    violations = ra.scan(tmp_path)
    hits = _by_kind(violations, "id_unregistered")
    assert len(hits) == 1
    assert "brand_new_rule" in hits[0].message
    assert len(_by_kind(violations, "bypass_module_alias_import")) == 1


def test_module_from_import_no_alias_wrapper_call_unregistered_id_detected(tmp_path):
    """p03b — 별칭 없는 `from services.cp_sat import rule_tag; rule_tag.hard_add(...)`."""
    _write(tmp_path, "services/constraints/team_constraints.py", """
        from services.cp_sat import rule_tag

        def f(m, x):
            rule_tag.hard_add(m, "brand_new_rule", x == 1)
    """)
    violations = ra.scan(tmp_path)
    hits = _by_kind(violations, "id_unregistered")
    assert len(hits) == 1
    assert "brand_new_rule" in hits[0].message
    assert len(_by_kind(violations, "bypass_module_alias_import")) == 1


def test_fully_dotted_wrapper_call_unregistered_id_detected(tmp_path):
    """p03c — 완전 점 경로 `services.cp_sat.rule_tag.hard_add(...)`(별칭 없이, `import
    services.cp_sat.rule_tag` 로 `services` 이름만 바인딩된 뒤 체인으로 접근)."""
    _write(tmp_path, "services/constraints/team_constraints.py", """
        import services.cp_sat.rule_tag

        def f(m, x):
            services.cp_sat.rule_tag.hard_add(m, "brand_new_rule", x == 1)
    """)
    violations = ra.scan(tmp_path)
    hits = _by_kind(violations, "id_unregistered")
    assert len(hits) == 1
    assert "brand_new_rule" in hits[0].message
    assert len(_by_kind(violations, "bypass_module_alias_import")) == 1


def test_relative_from_import_module_object_wrapper_call_detected(tmp_path):
    """r12 — `from . import rule_tag` (상대경로, services/cp_sat 안의 이웃 모듈에서) 뒤
    `rule_tag.hard_add(...)`."""
    _write(tmp_path, "services/cp_sat/sneaky_r12.py", """
        from . import rule_tag

        def f(m, x):
            rule_tag.hard_add(m, "brand_new_rule", x == 1)
    """)
    violations = ra.scan(tmp_path)
    hits = _by_kind(violations, "id_unregistered")
    assert len(hits) == 1
    assert "brand_new_rule" in hits[0].message
    assert len(_by_kind(violations, "bypass_module_alias_import")) == 1


def test_importlib_dynamic_import_wrapper_call_detected(tmp_path):
    """r24 — `importlib.import_module("services.cp_sat.rule_tag").hard_add(...)` — 어떤
    import 문도 안 남는 형태. id 등록 검증과, import 문 없이 모듈을 만들어낸 것 자체에
    대한 전용 위반(`bypass_dynamic_module_import`) 둘 다 걸려야 한다."""
    _write(tmp_path, "services/constraints/team_constraints.py", """
        import importlib

        def f(m, x):
            importlib.import_module("services.cp_sat.rule_tag").hard_add(m, "brand_new_rule", x == 1)
    """)
    violations = ra.scan(tmp_path)
    hits = _by_kind(violations, "id_unregistered")
    assert len(hits) == 1
    assert "brand_new_rule" in hits[0].message
    assert len(_by_kind(violations, "bypass_dynamic_module_import")) == 1


def test_model_alias_chain_getattr_dynamic_concat_detected(tmp_path):
    """r01 — `_mm = m` 로 별칭을 만든 뒤 `getattr(_mm, "Add" + "BoolOr")(...)`(비-리터럴
    식). 전에는 model_names 가 대입 체인을 추적하지 않아 `_mm` 이 좁은 model_names 판정
    (`_is_recognized_model_receiver`)에 없어 완전히 안 보였다."""
    _write(tmp_path, "services/cp_sat/fallback_lex.py", """
        def f(m, x):
            _mm = m
            return getattr(_mm, "Add" + "BoolOr")([x])
    """)
    violations = ra.scan(tmp_path)
    assert len(_by_kind(violations, "bypass_getattr_dynamic")) == 1


def test_model_alias_chain_dunder_getattribute_detected(tmp_path):
    """r02 — `_mm = m` 별칭 뒤 `_mm.__getattribute__("Add")(x)`."""
    _write(tmp_path, "services/cp_sat/fallback_lex.py", """
        def f(m, x):
            _mm = m
            return _mm.__getattribute__("Add")(x == 1)
    """)
    violations = ra.scan(tmp_path)
    assert len(_by_kind(violations, "bypass_dunder_getattr")) == 1


def test_helper_indirection_method_name_literal_detected(tmp_path):
    """r09 — 메서드 이름을 "편의 헬퍼"에 넘기는 형태(`_mut_r(m, "Add", expr)`). 수신자·
    메서드 둘 다 헬퍼 함수 매개변수라 정적으로 추적할 수 없다 — 그래서 "Add" 리터럴이
    hard_call/aux_call 의 메서드 자리(3번째 인자) 밖에 나타난 것 자체를 본다."""
    _write(tmp_path, "services/constraints/team_constraints.py", """
        def _mut_r(mm, meth, *a):
            return getattr(mm, meth)(*a)

        def f(m, expr):
            _mut_r(m, "Add", expr)
    """)
    violations = ra.scan(tmp_path)
    assert len(_by_kind(violations, "bypass_constraint_name_literal")) == 1


@pytest.mark.parametrize(
    "src",
    [
        'object.__getattribute__(m, "Add")(x == 1)',  # r03
        'builtins.getattr(m, "AddBoolOr")([x == 1])',  # r04
        'operator.methodcaller("AddBoolOr", [x == 1])(m)',  # r06
        'operator.attrgetter("Add")(m)(x == 1)',  # r07
        'type(m).__dict__["Add"](m, x == 1)',  # r08
    ],
)
def test_reflection_literal_method_name_variants_detected(tmp_path, src):
    """r03·r04·r06·r07·r08 — 반사(reflection) 형태 전부 수신자가 제각각이지만, 메서드 이름
    리터럴 "Add"/"AddBoolOr" 는 hard_call/aux_call 3번째 인자 자리 밖에 그대로 남는다."""
    _write(tmp_path, "services/cp_sat/fallback_lex.py", f"""
        import builtins
        import operator

        def f(m, x):
            return {src}
    """)
    violations = ra.scan(tmp_path)
    assert len(_by_kind(violations, "bypass_constraint_name_literal")) == 1


def test_rule_tag_new_passthrough_function_detected(tmp_path):
    """r11 — rule_tag.py 에 `plain_call`(hard_call과 같은 getattr 패턴) 을 몰래 추가하고
    대상 모듈에서 그 이름으로 불러 쓰는 자리. 전에는 모듈 전체가 getattr 비-리터럴
    검사에서 면제돼 rule_tag.py 쪽은 조용했고, 호출부(`plain_call(m, "Add", ...)`)도
    `.Add` 속성 호출이 아니라서 raw_call_in_target 에 안 잡혔다 — 두 가지 다 잡혀야 한다:
    ① 호출부의 리터럴 "Add" 가 hard_call/aux_call 자리 밖에 나타난 것(constraint_name_literal).
    ② rule_tag.py 자신의 새 함수 본문도 이제 getattr 비-리터럴 검사에서 면제되지 않는다."""
    _write(tmp_path, "services/cp_sat/rule_tag.py", """
        def hard_add(m, rule_id, expr):
            return m.Add(expr)

        def aux_add(m, tag, expr):
            return m.Add(expr)

        def hard_call(m, rule_id, method, *args, **kwargs):
            return getattr(m, method)(*args, **kwargs)

        def aux_call(m, tag, method, *args, **kwargs):
            return getattr(m, method)(*args, **kwargs)

        def plain_call(m, method, *args, **kwargs):
            return getattr(m, method)(*args, **kwargs)
    """)
    _write(tmp_path, "services/cp_sat/fallback_lex.py", """
        from services.cp_sat.rule_tag import plain_call

        def f(m, x):
            plain_call(m, "Add", x == 1)
    """)
    violations = ra.scan(tmp_path)
    assert len(_by_kind(violations, "bypass_constraint_name_literal")) == 1
    assert len(_by_kind(violations, "bypass_getattr_dynamic")) == 1


def test_hard_call_assumption_method_detected(tmp_path):
    """r22 — `hard_call(m, "forbidden_shift", "AddAssumptions", [...])` 처럼 hard_call 의
    메서드 자리에 가정(assumption) 메서드를 넣어 가정 관리를 hard_assumption.py 밖으로
    끌어내는 자리."""
    _write(tmp_path, "services/cp_sat/fallback_lex.py", """
        from services.cp_sat.rule_tag import hard_call

        def f(m, x):
            hard_call(m, "forbidden_shift", "AddAssumptions", [x])
    """)
    violations = ra.scan(tmp_path)
    assert len(_by_kind(violations, "wrapper_call_method_not_constraint")) == 1


def test_proto_extend_manipulation_detected(tmp_path):
    """p08b — `m.Proto().constraints.extend(...)` 처럼 add 가 아닌 메서드로 protobuf 를
    직접 조작하는 자리."""
    _write(tmp_path, "services/cp_sat_basic.py", """
        def f(m, c):
            m.Proto().constraints.extend([c])
    """)
    violations = ra.scan(tmp_path)
    assert len(_by_kind(violations, "bypass_proto_direct_access")) == 1


def test_proto_mergefrom_manipulation_detected(tmp_path):
    """p08c — `m.Proto().MergeFrom(m.Clone().Proto())`."""
    _write(tmp_path, "services/cp_sat_basic.py", """
        def f(m):
            m.Proto().MergeFrom(m.Clone().Proto())
    """)
    violations = ra.scan(tmp_path)
    assert len(_by_kind(violations, "bypass_proto_direct_access")) == 1


def test_proto_access_on_non_model_receiver_not_flagged(tmp_path):
    """오탐 방지 회귀 — IntVar 등 비-모델 수신자의 `.Proto()` 실사용(실제 app 모양,
    fallback_lex.py:4555)은 잡히면 안 된다."""
    _write(tmp_path, "services/cp_sat/fallback_lex.py", """
        def f(arr):
            return max(int(v.Proto().domain[-1]) for v in arr)
    """)
    violations = ra.scan(tmp_path)
    assert _by_kind(violations, "bypass_proto_direct_access") == []


def test_eval_string_assembled_constraint_call_detected(tmp_path):
    """r10 — `eval("m.Add(_w_ban_expr)")` 처럼 문자열을 조립해 실행하는 자리."""
    _write(tmp_path, "services/cp_sat/fallback_lex.py", """
        def f(m, _w_ban_expr):
            return eval("m.Add(_w_ban_expr)")
    """)
    violations = ra.scan(tmp_path)
    assert len(_by_kind(violations, "bypass_eval_exec_reflection")) == 1


def test_getattr_rebind_reflection_literal_detected(tmp_path):
    """r05 — `_ga = getattr; _ga(m, "AddBoolOr")(...)` 처럼 `getattr` 자체를 다른 이름에
    복사해 부르는 자리. 리터럴 "AddBoolOr" 는 호출 형태와 무관하게 그대로 남는다."""
    _write(tmp_path, "services/cp_sat/fallback_lex.py", """
        def f(m, x):
            _ga = getattr
            return _ga(m, "AddBoolOr")([x == 1])
    """)
    violations = ra.scan(tmp_path)
    assert len(_by_kind(violations, "bypass_constraint_name_literal")) == 1


def test_hard_call_method_arg_literal_not_flagged(tmp_path):
    """오탐 방지 회귀 — hard_call/aux_call 의 정당한 메서드 자리(3번째 인자)는
    `bypass_constraint_name_literal` 이 잡으면 안 된다."""
    _write(tmp_path, "services/cp_sat/fallback_lex.py", """
        from services.cp_sat.rule_tag import hard_call

        def f(m, x):
            hard_call(m, "forbidden_shift", "AddBoolOr", [x == 1])
    """)
    violations = ra.scan(tmp_path)
    assert _by_kind(violations, "bypass_constraint_name_literal") == []


def test_dunder_import_dynamic_wrapper_call_detected(tmp_path):
    """r24 이웃 형태 — `__import__("services.cp_sat.hard_assumption").add_hard(...)`."""
    _write(tmp_path, "services/cp_sat_basic.py", """
        def f(m, registry):
            __import__("services.cp_sat.hard_assumption").add_hard(
                m, registry, name="n",
                constraint_expr=(1 == 1),
                meta={"pattern": "not_a_registered_pattern"},
            )
    """)
    violations = ra.scan(tmp_path)
    hits = _by_kind(violations, "id_unregistered")
    assert len(hits) == 1
    assert "not_a_registered_pattern" in hits[0].message
    assert len(_by_kind(violations, "bypass_dynamic_module_import")) == 1


# ── x04 — 로컬 재대입 체인(`_h = hard_add; _h(...)`) ─────────────────────────


def test_local_reassignment_alias_call_detected(tmp_path):
    """x04 — `_h = hard_add` 로 복사한 뒤 `_h(...)` 로 부르는 자리. id 검증이 걸리고,
    재대입 자체도 별도 우회 형태로 같이 잡힌다."""
    _write(tmp_path, "services/constraints/team_constraints.py", """
        from services.cp_sat.rule_tag import hard_add

        def f(m, x):
            _h = hard_add
            _h(m, "no_such_rule_id", x == 1)
    """)
    violations = ra.scan(tmp_path)
    assert len(_by_kind(violations, "id_unregistered")) == 1
    assert len(_by_kind(violations, "bypass_local_wrapper_alias")) == 1


# ── x05 — 상대경로 import 별칭(`from .rule_tag import hard_add as _h2`) ──────


def test_relative_import_alias_detected(tmp_path):
    """x05 — 상대경로 import 는 `node.module` 이 'rule_tag' 뿐이라 예전엔 절대경로
    문자열 비교(`node.module == RULE_TAG_MODULE`)에 안 걸려 완전히 안 보였다."""
    _write(tmp_path, "services/cp_sat/sneaky.py", """
        from .rule_tag import hard_add as _h2

        def f(m, x):
            _h2(m, "no_such_rule_id", x == 1)
    """)
    violations = ra.scan(tmp_path)
    assert len(_by_kind(violations, "bypass_import_alias")) == 1
    assert len(_by_kind(violations, "id_unregistered")) == 1


def test_wrapper_import_alias_message_suggests_canonical_name_not_alias(tmp_path):
    """메시지 수정 — 별칭 import 위반의 제안 import 문은 별칭(ha)이 아니라 원래 이름
    (hard_add)을 가져오도록 권해야 한다(전에는 'from rule_tag import ha' 를 권해 고치라고
    한 말이 같은 별칭을 다시 만들었다 — 2026-10-13 지적)."""
    _write(tmp_path, "services/cp_sat/sneaky.py", """
        from services.cp_sat.rule_tag import hard_add as ha

        def f(m, expr):
            return ha(m, "forbidden_shift", expr)
    """)
    violations = ra.scan(tmp_path)
    hits = _by_kind(violations, "bypass_import_alias")
    assert len(hits) == 1
    assert "import hard_add'" in hits[0].message
    assert "import ha'" not in hits[0].message


# ── x20 — `functools.partial(hard_add, m, "x")` 값으로 꺼내 넘기기 ──────────


def test_functools_partial_name_extraction_detected(tmp_path):
    """x20 — 래퍼를 호출하지 않고 Name 값으로 다른 호출(functools.partial)에 넘기는
    자리 — 호출이 아니라서 `_bypass_attribute_extraction`(Attribute 전용)도
    `_check_wrapper_ids`(직접 호출만 봄)도 못 잡았다."""
    _write(tmp_path, "services/constraints/team_constraints.py", """
        import functools

        from services.cp_sat.rule_tag import hard_add

        def f(m):
            return functools.partial(hard_add, m, "max_night")
    """)
    violations = ra.scan(tmp_path)
    assert len(_by_kind(violations, "bypass_name_extracted")) == 1


def test_direct_wrapper_call_not_flagged_as_name_extraction(tmp_path):
    """회귀 — 정상적인 직접 호출(`hard_add(...)`)은 Name 추출로 오판되면 안 된다."""
    _write(tmp_path, "services/constraints/team_constraints.py", """
        from services.cp_sat.rule_tag import hard_add

        def f(m, x):
            hard_add(m, "max_night", x <= 15)
    """)
    violations = ra.scan(tmp_path)
    assert _by_kind(violations, "bypass_name_extracted") == []


# ── x13 — 면제 모듈: 호출 "수" 는 같게, 내용만 바꿔치기 ──────────────────────


def test_exempt_content_swap_same_count_detected(tmp_path):
    """x13 — 면제 모듈(rule_tag.py, 실측 기준 2곳)의 호출 수를 2로 유지한 채 호출
    내용을 실측과 다르게 바꾸면, 수만 보는 낡은 검사는 못 잡지만 정규화 호출 지문
    다중집합 해시는 달라져 잡힌다."""
    _write(tmp_path, "services/cp_sat/rule_tag.py", """
        def hard_add(m, rule_id, expr):
            return m.Add(expr)

        def aux_add(m, tag, expr):
            return m.Add(expr != 0)
    """)
    violations = ra.scan(tmp_path)
    assert _by_kind(violations, "exempt_count_mismatch") == []
    hits = _by_kind(violations, "exempt_content_swapped")
    assert len(hits) == 1


def test_exempt_content_at_real_baseline_is_clean(tmp_path):
    """회귀 — 실측 기준과 글자 그대로 같은 내용이면(공백만 달라도) 지문 해시가
    맞아 통과한다(ast.unparse 로 정규화하므로 포맷 차이는 무시된다)."""
    _write(tmp_path, "services/cp_sat/rule_tag.py", """
        def hard_add(m,    rule_id,
                     expr):
            return m.Add(expr)

        def aux_add(m, tag, expr):
            return m.Add(
                expr
            )
    """)
    violations = ra.scan(tmp_path)
    assert _by_kind(violations, "exempt_count_mismatch") == []
    assert _by_kind(violations, "exempt_content_swapped") == []


# ── x14 — add_hard meta dict 를 만든 뒤 덮어쓰기 ─────────────────────────────


def test_add_hard_meta_pattern_overwritten_via_subscript_detected(tmp_path):
    """x14 — `meta = {"pattern": "max_night"}` 뒤 `meta["pattern"] = "..."` 로
    덮어쓰면, 처음 리터럴만 보던 낡은 검사는 등록된 "max_night" 만 보고 통과시키지만
    최종(나중) 값을 보는 검사는 덮어쓴 미등록 id 를 잡는다."""
    _write(tmp_path, "services/cp_sat_basic.py", """
        from services.cp_sat.hard_assumption import add_hard as _add_hard

        def f(m, registry):
            _meta = {"pattern": "max_night"}
            _meta["pattern"] = "not_a_registered_pattern"
            _add_hard(m, registry, name="n", constraint_expr=(1 == 1), meta=_meta)
    """)
    violations = ra.scan(tmp_path)
    hits = _by_kind(violations, "id_unregistered")
    assert len(hits) == 1
    assert "not_a_registered_pattern" in hits[0].message


def test_add_hard_meta_pattern_overwritten_via_update_detected(tmp_path):
    """x14 — `meta.update({"pattern": ...})` 로 덮어써도 마찬가지로 나중 값을 본다."""
    _write(tmp_path, "services/cp_sat_basic.py", """
        from services.cp_sat.hard_assumption import add_hard as _add_hard

        def f(m, registry):
            _meta = {"pattern": "max_night"}
            _meta.update({"pattern": "not_a_registered_pattern"})
            _add_hard(m, registry, name="n", constraint_expr=(1 == 1), meta=_meta)
    """)
    violations = ra.scan(tmp_path)
    hits = _by_kind(violations, "id_unregistered")
    assert len(hits) == 1
    assert "not_a_registered_pattern" in hits[0].message


def test_add_hard_pattern_cross_function_same_varname_not_confused(tmp_path):
    """r16 — 같은 지역변수 이름(`_meta`)을 쓰는 두 함수에서, 한쪽의 pattern 키를 지워도
    **다른 함수**의 이벤트로 잘못 판정하면 안 된다(grade_constraints.py `_add_minimum_
    constraints`/`_add_maximum_constraints` 실제 모양 — 둘 다 `_meta` 를 쓴다). 전에는
    모듈 전체를 훑어 '가장 나중 줄'을 썼기 때문에, 아래쪽 함수에서 pattern 키를 지우면
    위쪽 함수의 "grade_min" 으로 잘못 판정해 위반 0 이었다."""
    _write(tmp_path, "services/constraints/grade_constraints.py", """
        from services.cp_sat.hard_assumption import add_hard as _add_hard_fn

        def _add_minimum_constraints(m, registry, vars_sum, t):
            _meta = {"pattern": "grade_min"}
            _add_hard_fn(m, registry, name="n1", constraint_expr=(vars_sum >= t), meta=_meta)

        def _add_maximum_constraints(m, registry, vars_sum, mt):
            _meta = {"value": mt}
            _add_hard_fn(m, registry, name="n2", constraint_expr=(vars_sum <= mt), meta=_meta)
    """)
    violations = ra.scan(tmp_path)
    hits = _by_kind(violations, "add_hard_pattern_missing")
    assert len(hits) == 1
    # grade_min(등록된 id) 으로 잘못 넘어가면 안 된다 — id_unregistered 가 아니라 missing 이어야 한다.
    assert _by_kind(violations, "id_unregistered") == []


def test_add_hard_pattern_reassigned_non_dict_literal_untrackable(tmp_path):
    """r17 — `_meta = dict(_meta, pattern="unregistered_x")` 처럼 dict 리터럴이 아닌 값으로
    재대입하면 조용히 넘어가지 않고 'untrackable' 로 거부해야 한다."""
    _write(tmp_path, "services/cp_sat_basic.py", """
        from services.cp_sat.hard_assumption import add_hard as _add_hard

        def f(m, registry):
            _meta = {"pattern": "grade_max"}
            _meta = dict(_meta, pattern="unregistered_x")
            _add_hard(m, registry, name="n", constraint_expr=(1 == 1), meta=_meta)
    """)
    violations = ra.scan(tmp_path)
    assert len(_by_kind(violations, "add_hard_pattern_untrackable")) == 1
    assert _by_kind(violations, "id_unregistered") == []


def test_add_hard_pattern_alias_assignment_untrackable(tmp_path):
    """p14a — `_meta_alias = _meta` 로 별칭을 만든 뒤 그 별칭으로 덮어쓰는 자리. 별칭이
    생긴 시점 자체를 보수적으로 거부한다(별칭을 통한 이후 쓰기를 추적하지 않는다)."""
    _write(tmp_path, "services/cp_sat_basic.py", """
        from services.cp_sat.hard_assumption import add_hard as _add_hard

        def f(m, registry):
            _meta = {"pattern": "max_night"}
            _meta_alias = _meta
            _meta_alias["pattern"] = "unregistered_x"
            _add_hard(m, registry, name="n", constraint_expr=(1 == 1), meta=_meta)
    """)
    violations = ra.scan(tmp_path)
    assert len(_by_kind(violations, "add_hard_pattern_untrackable")) == 1


def test_add_hard_pattern_augassign_untrackable(tmp_path):
    """p14b — `_meta |= {...}` 복합 대입 연산자로 바뀌는 자리."""
    _write(tmp_path, "services/cp_sat_basic.py", """
        from services.cp_sat.hard_assumption import add_hard as _add_hard

        def f(m, registry):
            _meta = {"pattern": "max_night"}
            _meta |= {"pattern": "unregistered_x"}
            _add_hard(m, registry, name="n", constraint_expr=(1 == 1), meta=_meta)
    """)
    violations = ra.scan(tmp_path)
    assert len(_by_kind(violations, "add_hard_pattern_untrackable")) == 1


def test_add_hard_pattern_dunder_setitem_untrackable(tmp_path):
    """r18 — update()·구독 대입이 아닌 메서드(`__setitem__`)로 바뀌는 자리."""
    _write(tmp_path, "services/cp_sat_basic.py", """
        from services.cp_sat.hard_assumption import add_hard as _add_hard

        def f(m, registry):
            _meta = {"pattern": "max_night"}
            _meta.__setitem__("pattern", "unregistered_x")
            _add_hard(m, registry, name="n", constraint_expr=(1 == 1), meta=_meta)
    """)
    violations = ra.scan(tmp_path)
    assert len(_by_kind(violations, "add_hard_pattern_untrackable")) == 1


def test_add_hard_pattern_update_spread_untrackable(tmp_path):
    """r19 — `_meta.update(**other)` 처럼 update() 에 `**` 펼침 인자를 쓰는 자리 — 안에
    숨은 "pattern" 을 정적으로 알 수 없다."""
    _write(tmp_path, "services/cp_sat_basic.py", """
        from services.cp_sat.hard_assumption import add_hard as _add_hard

        def f(m, registry, other):
            _meta = {"pattern": "max_night"}
            _meta.update(**other)
            _add_hard(m, registry, name="n", constraint_expr=(1 == 1), meta=_meta)
    """)
    violations = ra.scan(tmp_path)
    assert len(_by_kind(violations, "add_hard_pattern_untrackable")) == 1


def test_add_hard_pattern_dict_literal_spread_untrackable(tmp_path):
    """p14c — 리터럴 안의 나중 `**` 펼침 `{"pattern": "max_night", **{"pattern": "x"}}`."""
    _write(tmp_path, "services/cp_sat_basic.py", """
        from services.cp_sat.hard_assumption import add_hard as _add_hard

        def f(m, registry):
            _add_hard(
                m, registry, name="n", constraint_expr=(1 == 1),
                meta={"pattern": "max_night", **{"pattern": "unregistered_x"}},
            )
    """)
    violations = ra.scan(tmp_path)
    assert len(_by_kind(violations, "add_hard_pattern_untrackable")) == 1


def test_add_hard_pattern_duplicate_literal_key_uses_last(tmp_path):
    """p14c 이웃 — 같은 리터럴 "pattern" 키가 두 번 나오면(``** `` 없이) 실제 파이썬 dict
    리터럴처럼 **마지막** 값을 써야 한다."""
    _write(tmp_path, "services/cp_sat_basic.py", """
        from services.cp_sat.hard_assumption import add_hard as _add_hard

        def f(m, registry):
            _add_hard(
                m, registry, name="n", constraint_expr=(1 == 1),
                meta={"pattern": "max_night", "pattern": "unregistered_x"},
            )
    """)
    violations = ra.scan(tmp_path)
    hits = _by_kind(violations, "id_unregistered")
    assert len(hits) == 1
    assert "unregistered_x" in hits[0].message


def test_add_hard_meta_pattern_not_overwritten_still_clean(tmp_path):
    """회귀 — 덮어쓰기가 없으면(또는 등록된 id 로만 덮어쓰면) 그대로 통과한다."""
    _write(tmp_path, "services/cp_sat_basic.py", """
        from services.cp_sat.hard_assumption import add_hard as _add_hard

        def f(m, registry):
            _meta = {"pattern": "not_a_registered_pattern"}
            _meta["pattern"] = "max_night"
            _add_hard(m, registry, name="n", constraint_expr=(1 == 1), meta=_meta)
    """)
    violations = ra.scan(tmp_path)
    assert _by_kind(violations, "id_unregistered") == []


# ── x09~x12 — 허용 동적 id 변수(_fixed_pattern)의 모든 바인딩 형태 ───────────


def test_dynamic_id_var_annassign_impure_detected(tmp_path):
    """x09 — AnnAssign(타입주석 대입)으로 바인딩되면(옛 검사는 plain Assign 만 봐서
    이 형태를 완전히 놓쳤다)."""
    _write(tmp_path, "services/cp_sat/fallback_lex.py", """
        from services.cp_sat.rule_tag import hard_add

        def _fixed_pattern_from_source(source):
            return f"fixed_assignment:{source or 'manual'}"

        def f(m, x):
            _fixed_pattern: str = "literal_not_from_producer"
            hard_add(m, _fixed_pattern, x == 1)
    """)
    violations = ra.scan(tmp_path)
    assert len(_by_kind(violations, "dynamic_id_var_not_pure")) == 1


def test_dynamic_id_var_tuple_unpack_detected(tmp_path):
    """x10 — 튜플 풀기로 바인딩되면(원소가 생산자 호출이어도) 각 원소의 순수성을
    정적으로 증명할 수 없어 보수적으로 늘 잡는다."""
    _write(tmp_path, "services/cp_sat/fallback_lex.py", """
        from services.cp_sat.rule_tag import hard_add

        def _fixed_pattern_from_source(source):
            return f"fixed_assignment:{source or 'manual'}"

        def f(m, x, src):
            _fixed_pattern, _other = _fixed_pattern_from_source(src), None
            hard_add(m, _fixed_pattern, x == 1)
    """)
    violations = ra.scan(tmp_path)
    assert len(_by_kind(violations, "dynamic_id_var_not_pure")) == 1


def test_dynamic_id_var_walrus_impure_detected(tmp_path):
    """x11 — walrus(:=)로 바인딩되면(옛 검사는 plain Assign 만 봐서 완전히 놓쳤다)."""
    _write(tmp_path, "services/cp_sat/fallback_lex.py", """
        from services.cp_sat.rule_tag import hard_add

        def _fixed_pattern_from_source(source):
            return f"fixed_assignment:{source or 'manual'}"

        def f(m, x):
            if (_fixed_pattern := "literal_not_from_producer"):
                hard_add(m, _fixed_pattern, x == 1)
    """)
    violations = ra.scan(tmp_path)
    assert len(_by_kind(violations, "dynamic_id_var_not_pure")) == 1


def test_dynamic_id_var_function_parameter_detected(tmp_path):
    """x12 — _fixed_pattern 이 함수 매개변수로 바인딩되면 호출자가 실제로 주는 값을
    정적으로 알 수 없어 늘 잡는다."""
    _write(tmp_path, "services/cp_sat/fallback_lex.py", """
        from services.cp_sat.rule_tag import hard_add

        def _fixed_pattern_from_source(source):
            return f"fixed_assignment:{source or 'manual'}"

        def f(m, x, _fixed_pattern):
            hard_add(m, _fixed_pattern, x == 1)
    """)
    violations = ra.scan(tmp_path)
    assert len(_by_kind(violations, "dynamic_id_var_not_pure")) == 1


# ── x18 — 문법 오류 모듈을 조용히 건너뛰지 않음 ──────────────────────────────


def test_dynamic_id_var_comprehension_target_detected(tmp_path):
    """p09f 이웃 — 리스트 컴프리헨션 대상으로 바인딩되는 자리
    (`[hard_add(m, _fixed_pattern, expr) for _fixed_pattern in ["anything"]]`)."""
    _write(tmp_path, "services/cp_sat/fallback_lex.py", """
        from services.cp_sat.rule_tag import hard_add

        def _fixed_pattern_from_source(source):
            return f"fixed_assignment:{source or 'manual'}"

        def f(m, expr):
            return [hard_add(m, _fixed_pattern, expr) for _fixed_pattern in ["anything"]]
    """)
    violations = ra.scan(tmp_path)
    assert len(_by_kind(violations, "dynamic_id_var_not_pure")) == 1


def test_dynamic_id_var_for_loop_target_detected(tmp_path):
    """p09f — for 루프 대상으로 바인딩되는 자리(`for _fixed_pattern in ["anything"]:`)."""
    _write(tmp_path, "services/cp_sat/fallback_lex.py", """
        from services.cp_sat.rule_tag import hard_add

        def _fixed_pattern_from_source(source):
            return f"fixed_assignment:{source or 'manual'}"

        def f(m, expr):
            for _fixed_pattern in ["anything"]:
                hard_add(m, _fixed_pattern, expr)
    """)
    violations = ra.scan(tmp_path)
    assert len(_by_kind(violations, "dynamic_id_var_not_pure")) == 1


def test_dynamic_id_var_with_as_target_detected(tmp_path):
    """r15 — `with open(__file__) as _fixed_pattern:` 로 바인딩되는 자리."""
    _write(tmp_path, "services/cp_sat/fallback_lex.py", """
        from services.cp_sat.rule_tag import hard_add

        def _fixed_pattern_from_source(source):
            return f"fixed_assignment:{source or 'manual'}"

        def f(m, expr):
            with open(__file__) as _fixed_pattern:
                hard_add(m, _fixed_pattern, expr)
    """)
    violations = ra.scan(tmp_path)
    assert len(_by_kind(violations, "dynamic_id_var_not_pure")) == 1


def test_dynamic_id_producer_rebound_via_lambda_detected(tmp_path):
    """r13 — 원래 `def _fixed_pattern_from_source(...)` 가 소스에 그대로 남아 있어도, 그
    뒤에 같은 이름으로 `lambda` 를 재대입하면 런타임에는 그 쪽이 이긴다. 이름 집합만 보는
    "순수 호출" 판정은 이 재대입을 완전히 놓쳤었다."""
    _write(tmp_path, "services/cp_sat/fallback_lex.py", """
        from services.cp_sat.rule_tag import hard_add

        def _fixed_pattern_from_source(source):
            return f"fixed_assignment:{source or 'manual'}"

        _fixed_pattern_from_source = lambda _s: "anything"

        def f(m, expr, src):
            _fixed_pattern = _fixed_pattern_from_source(src)
            hard_add(m, _fixed_pattern, expr)
    """)
    violations = ra.scan(tmp_path)
    assert len(_by_kind(violations, "dynamic_id_producer_rebound")) == 1


def test_syntax_error_module_reported_not_skipped(tmp_path):
    """x18 — 전에는 SyntaxError 가 나면 그 파일을 조용히 건너뛰어 "감사가 이 파일을
    못 봤다"는 사실 자체가 사라졌다. 이제는 그 자체를 위반으로 보고한다."""
    _write(tmp_path, "services/cp_sat/broken.py", """
        def f(m:
            pass
    """)
    violations = ra.scan(tmp_path)
    hits = _by_kind(violations, "parse_error")
    assert len(hits) == 1
    assert "broken.py" in hits[0].message


def test_syntax_error_module_does_not_block_scanning_other_modules(tmp_path):
    """회귀 — 문법 오류 모듈이 있어도 나머지 모듈은 정상적으로 스캔된다."""
    _write(tmp_path, "services/cp_sat/broken.py", """
        def f(m:
            pass
    """)
    _write(tmp_path, "services/cp_sat/fallback_lex.py", """
        def f(m):
            m.Add(x <= 1)
    """)
    violations = ra.scan(tmp_path)
    assert len(_by_kind(violations, "parse_error")) == 1
    assert len(_by_kind(violations, "raw_call_in_target")) == 1


# ── "17"(접두 없음) — scan(app_root) 는 app_root 자신의 rules.py 를 쓴다 ──────


def test_scan_uses_app_roots_own_registry_not_global(tmp_path):
    """17 — `scan(app_root)` 가 top-level `from services.engine_contract.rules import
    ...`(sys.path 전역) 에 기대면 app_root 인자가 장식일 뿐이다. 이 tmp_path 자신의
    rules.py 에만 있는 id("custom_only_rule")가 전역(실제 app/) 레지스트리에는 없는데도
    "등록됨"으로 올바르게 판정되는지로 밀폐를 확인한다."""
    _write(tmp_path, "services/engine_contract/rules.py", """
        from __future__ import annotations
        from dataclasses import dataclass

        @dataclass(frozen=True)
        class RuleSpec:
            id: str
            strength: str
            module: str = "common"
            governing_keys: tuple = ()

        RULES = {"custom_only_rule": RuleSpec(id="custom_only_rule", strength="HARD")}
        RULE_PREFIXES: dict = {}

        def lookup(rule_id):
            return RULES.get(rule_id)
    """)
    _write(tmp_path, "services/constraints/team_constraints.py", """
        from services.cp_sat.rule_tag import hard_add

        def f(m, x):
            hard_add(m, "custom_only_rule", x == 1)
    """)
    violations = ra.scan(tmp_path)
    assert _by_kind(violations, "id_unregistered") == []
    assert _by_kind(violations, "dead_rule_id") == []


def test_scan_sandboxed_registry_rejects_id_not_in_its_own_rules(tmp_path):
    """17 의 반대쪽 — 밀폐가 실제로 "그 app_root 의" rules.py 만 보는지. 이 tmp_path 의
    rules.py 에 없는 id("max_night" — 실제 app/ 전역 레지스트리에는 있다)를 쓰면,
    전역으로 새던 옛 구현이면 통과하겠지만 밀폐된 구현은 "등록 안 됨"으로 잡는다."""
    _write(tmp_path, "services/engine_contract/rules.py", """
        from __future__ import annotations
        from dataclasses import dataclass

        @dataclass(frozen=True)
        class RuleSpec:
            id: str
            strength: str
            module: str = "common"
            governing_keys: tuple = ()

        RULES: dict = {}
        RULE_PREFIXES: dict = {}

        def lookup(rule_id):
            return RULES.get(rule_id)
    """)
    _write(tmp_path, "services/constraints/team_constraints.py", """
        from services.cp_sat.rule_tag import hard_add

        def f(m, x):
            hard_add(m, "max_night", x <= 15)
    """)
    violations = ra.scan(tmp_path)
    hits = _by_kind(violations, "id_unregistered")
    assert len(hits) == 1
    assert "max_night" in hits[0].message
