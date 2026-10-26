"""EI-3 이름표 래퍼·RULES 표 계약 테스트(설계서 §2.3 · §2.6 A1 전제).

앱 코드는 고치지 않는다. 네 갈래를 본다.
  1. 래퍼 동작 동일성 — `hard_add`/`aux_add`/`hard_call`/`aux_call` 이 ortools `CpModel` 에
     거는 제약이 직접 호출과 proto 단위로 같은지(§2.3 "모델이 바뀌지 않는다").
  2. RULES 표 정합 — id·strength·module·하드락 번호·freezes·RULE_PREFIXES 의 구조 검사.
  3. governing_keys 문법 — 접두 없음(roster_config 속성·raw 허용 목록) · "테이블.컬럼"
     (SQLAlchemy 메타데이터, KEY_ALIASES 반영) · "runtime."/"env."/"const." 비DB 접두.
  4. `rules.py` 의 import — 엔진·DB·ortools 를 쓰지 않는 순수 데이터 모듈인지(AST).

이름표 없는 제약 호출 전수 스캔(A1 감사)은 별도 파일(`test_rule_audit.py`, 설계서 §5.3 EI-3
"보류 중인 테스트 몫")이다 — 이 파일의 범위가 아니다.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from ortools.sat.python import cp_model
from sqlalchemy import inspect as sa_inspect

from db.models import Base, RosterConfig
from services.cp_sat.rule_tag import aux_add, aux_call, hard_add, hard_call
from services.engine_contract.rules import (
    AUX,
    HARD,
    HARD_AUTO_RELAX,
    KEY_ALIASES,
    RULE_PREFIXES,
    RULES,
    is_registered,
    lookup,
)
from tests.engine_input.proto_compat import model_to_pb

_RULES_PY = Path(__file__).resolve().parents[2] / "app" / "services" / "engine_contract" / "rules.py"


# ── 공용 헬퍼 ────────────────────────────────────────────────────────────


def _bool_pair(n: int):
    """이름이 같은 BoolVar n 개를 가진 모델 둘(직접 호출용·래퍼용)."""
    direct, wrapped = cp_model.CpModel(), cp_model.CpModel()
    vd = [direct.NewBoolVar(f"b{i}") for i in range(n)]
    vw = [wrapped.NewBoolVar(f"b{i}") for i in range(n)]
    return direct, vd, wrapped, vw


def _assert_same_model(direct: cp_model.CpModel, wrapped: cp_model.CpModel) -> None:
    """두 모델의 proto 직렬화가 같다 — 래퍼가 모델을 한 비트도 바꾸지 않았다.

    ortools 9.13+(설치판 9.15.6755)의 `model.Proto()` 는 pybind 래퍼라 `SerializeToString`
    이 없다(2026-10-14 실측) — `tests.engine_input.proto_compat.model_to_pb` 로 표준
    `cp_model_pb2.CpModelProto` 로 바꾼 뒤 비교한다(판과 무관)."""
    assert model_to_pb(direct).SerializeToString() == model_to_pb(wrapped).SerializeToString()


def _assert_same_constraint(direct_ct, wrapped_ct) -> None:
    """반환값이 Constraint 이고, 직접 호출과 같은 Index — 같은 자리에 같은 제약이 걸렸다."""
    assert isinstance(direct_ct, cp_model.Constraint)
    assert isinstance(wrapped_ct, cp_model.Constraint)
    assert wrapped_ct.Index() == direct_ct.Index()


# ── 1. 래퍼 동작 동일성 ─────────────────────────────────────────────────


def test_hard_add_linear_coefficient_inequality_matches_direct_add():
    """Add(선형, 계수 있는 부등식) — hard_add 가 직접 Add 와 같은 모델을 만든다."""
    direct, vd, wrapped, vw = _bool_pair(2)
    d_ct = direct.Add(2 * vd[0] + 3 * vd[1] <= 4)
    w_ct = hard_add(wrapped, "coverage_max", 2 * vw[0] + 3 * vw[1] <= 4)
    _assert_same_constraint(d_ct, w_ct)
    _assert_same_model(direct, wrapped)


def test_hard_add_plain_inequality_matches_direct_add():
    """Add(부등식, >=) — coverage_min 류가 거는 하한 형태."""
    direct, vd, wrapped, vw = _bool_pair(3)
    d_ct = direct.Add(vd[0] + vd[1] + vd[2] >= 1)
    w_ct = hard_add(wrapped, "coverage_min", vw[0] + vw[1] + vw[2] >= 1)
    _assert_same_constraint(d_ct, w_ct)
    _assert_same_model(direct, wrapped)


def test_aux_add_equality_matches_direct_add():
    """Add(==) — aux_add(보조 변수 정의용)도 같은 proto 를 낸다."""
    direct, vd, wrapped, vw = _bool_pair(2)
    d_ct = direct.Add(vd[0] + vd[1] == 1)
    w_ct = aux_add(wrapped, "coverage_short", vw[0] + vw[1] == 1)
    _assert_same_constraint(d_ct, w_ct)
    _assert_same_model(direct, wrapped)


def test_hard_add_only_enforce_if_single_literal_chain_matches_direct():
    """Add(...).OnlyEnforceIf(b) 연쇄 — 반환된 Constraint 에 그대로 이어 걸린다.

    ortools 9.13+(설치판 9.15.6755)부터 `Constraint.OnlyEnforceIf` 의 반환값이 `None` 이다
    (9.12 는 `OnlyEnforceIf = only_enforce_if` 클래스 별칭이라 Constraint 를 돌려줬다 —
    2026-10-14 실측). app/ 안의 실사용은 전부 식 문장이라(반환값을 쓰는 자리 0곳) 의미
    차이는 없다 — 테스트만 연쇄를 끊어 `Add(...)` 가 돌려준 Constraint 를 직접 비교한다."""
    direct, vd, wrapped, vw = _bool_pair(3)
    d_ct = direct.Add(vd[0] + vd[1] <= 1)
    d_ct.OnlyEnforceIf(vd[2])
    w_ct = hard_add(wrapped, "carryover_boundary", vw[0] + vw[1] <= 1)
    w_ct.OnlyEnforceIf(vw[2])
    _assert_same_constraint(d_ct, w_ct)
    _assert_same_model(direct, wrapped)


def test_hard_add_only_enforce_if_literal_list_matches_direct():
    """OnlyEnforceIf 리스트(선택 리터럴 묶음) — `carryover_boundary` 실제 호출부(:2645 등)와 같은 형태.

    위 테스트와 같은 이유(9.15 `OnlyEnforceIf` 반환값 `None`)로 연쇄를 끊는다."""
    direct, vd, wrapped, vw = _bool_pair(3)
    d_ct = direct.Add(vd[0] + vd[1] <= 1)
    d_ct.OnlyEnforceIf([vd[2], vd[0].Not()])
    w_ct = hard_add(wrapped, "carryover_boundary", vw[0] + vw[1] <= 1)
    w_ct.OnlyEnforceIf([vw[2], vw[0].Not()])
    _assert_same_constraint(d_ct, w_ct)
    _assert_same_model(direct, wrapped)


def test_hard_call_add_exactly_one_generator_in_parens_matches_direct():
    """AddExactlyOne(제너레이터) — fallback_lex:1585 · cp_sat_basic:3173 실제 형태.
    `hard_call` 호출에서는 제너레이터가 유일 인자가 아니라서 괄호로 감싸야 한다(설계서 §5.3
    EI-3 "치환 문법" · rewrite_hint). 직접 호출(유일 인자, 괄호 불필요)과 같은 모델이 나와야 한다.
    """
    direct, vd, wrapped, vw = _bool_pair(3)
    d_ct = direct.AddExactlyOne(vd[i] for i in range(3))
    w_ct = hard_call(wrapped, "exactly_one_shift", "AddExactlyOne", (vw[i] for i in range(3)))
    _assert_same_constraint(d_ct, w_ct)
    _assert_same_model(direct, wrapped)


def test_hard_call_lowercase_alias_matches_direct_pascalcase():
    """ortools 9.12 소문자 별칭(`add_exactly_one`) — `getattr` 가 대소문자를 그대로 받는다."""
    direct, vd, wrapped, vw = _bool_pair(3)
    d_ct = direct.AddExactlyOne(vd[i] for i in range(3))
    w_ct = hard_call(wrapped, "exactly_one_shift", "add_exactly_one", (vw[i] for i in range(3)))
    _assert_same_constraint(d_ct, w_ct)
    _assert_same_model(direct, wrapped)


def test_aux_call_add_min_equality_matches_direct():
    """AddMinEquality — `max_cov_off_equalize`(fallback_lex:1841) 실제 형태(aux_call)."""
    direct, wrapped = cp_model.CpModel(), cp_model.CpModel()
    td, xd0, xd1 = direct.NewIntVar(0, 10, "t"), direct.NewIntVar(0, 5, "x0"), direct.NewIntVar(0, 5, "x1")
    tw, xw0, xw1 = wrapped.NewIntVar(0, 10, "t"), wrapped.NewIntVar(0, 5, "x0"), wrapped.NewIntVar(0, 5, "x1")
    d_ct = direct.AddMinEquality(td, [xd0, xd1])
    w_ct = aux_call(wrapped, "max_cov_off_equalize", "AddMinEquality", tw, [xw0, xw1])
    _assert_same_constraint(d_ct, w_ct)
    _assert_same_model(direct, wrapped)


def test_aux_call_add_linear_constraint_with_keyword_args_matches_direct():
    """AddLinearConstraint + 키워드 인자 — `hard_call`/`aux_call` 의 `**kwargs` 전달 확인.

    ortools 9.13+(설치판 9.15.6755)부터 `add_max_equality`/`add_min_equality` 의 둘째
    매개변수 이름이 `exprs`(목록 하나)에서 `*expressions`(가변 인자)로 바뀌었다(2026-10-14
    실측 — `exprs=` 키워드를 주면 TypeError. app/ 안에 `exprs=` 키워드 사용은 0곳이라 앱
    영향은 없다). 이 테스트의 목적은 "**kwargs 전달이 되는지"이므로, 두 판에서 매개변수
    이름이 그대로인 `AddLinearConstraint(linear_expr, lb, ub)` 로 옮긴다(위치 인자 형태의
    AddMaxEquality/AddMinEquality 확인은 위 두 테스트가 이미 따로 본다)."""
    direct, wrapped = cp_model.CpModel(), cp_model.CpModel()
    xd0, xd1 = direct.NewIntVar(0, 10, "x0"), direct.NewIntVar(0, 10, "x1")
    xw0, xw1 = wrapped.NewIntVar(0, 10, "x0"), wrapped.NewIntVar(0, 10, "x1")
    d_ct = direct.AddLinearConstraint(xd0 + xd1, 1, 7)
    w_ct = aux_call(wrapped, "max_cov_off_equalize", "AddLinearConstraint", linear_expr=xw0 + xw1, lb=1, ub=7)
    _assert_same_constraint(d_ct, w_ct)
    _assert_same_model(direct, wrapped)


def test_hard_call_add_bool_or_matches_direct():
    """AddBoolOr — `.Add(` 밖의 또 다른 제약 메서드도 `hard_call` 로 그대로 받는다."""
    direct, vd, wrapped, vw = _bool_pair(3)
    d_ct = direct.AddBoolOr([vd[0], vd[1].Not(), vd[2]])
    w_ct = hard_call(wrapped, "transition_ban", "AddBoolOr", [vw[0], vw[1].Not(), vw[2]])
    _assert_same_constraint(d_ct, w_ct)
    _assert_same_model(direct, wrapped)


def test_hard_call_add_implication_matches_direct():
    """AddImplication — 2항 제약도 인자 그대로 넘어간다."""
    direct, vd, wrapped, vw = _bool_pair(2)
    d_ct = direct.AddImplication(vd[0], vd[1])
    w_ct = hard_call(wrapped, "night_block_end", "AddImplication", vw[0], vw[1])
    _assert_same_constraint(d_ct, w_ct)
    _assert_same_model(direct, wrapped)


# ── 2. RULES 표 정합 ────────────────────────────────────────────────────


def test_rule_dict_keys_match_their_own_id():
    for key, spec in RULES.items():
        assert key == spec.id


def test_rule_ids_are_unique():
    ids = [spec.id for spec in RULES.values()]
    assert len(ids) == len(set(ids))


def test_exact_rule_ids_are_snake_case():
    """'정확 id'(RULES, 접두 규칙 제외)는 snake_case([a-z0-9_]+)."""
    pattern = re.compile(r"^[a-z0-9_]+$")
    bad = [rule_id for rule_id in RULES if not pattern.fullmatch(rule_id)]
    assert not bad, f"snake_case 위반 id: {bad}"


def test_rule_prefix_ids_carry_wildcard_marker():
    """RULE_PREFIXES 의 id 는 접두 + '*' — '정확 id' snake_case 검사 대상이 아니다."""
    for prefix, spec in RULE_PREFIXES.items():
        assert spec.id == f"{prefix}*"


def test_rule_strength_is_one_of_three_known_values():
    allowed = {HARD, HARD_AUTO_RELAX, AUX}
    bad = {spec.id: spec.strength for spec in RULES.values() if spec.strength not in allowed}
    assert not bad, bad


def test_ladder_stage_set_iff_hard_auto_relax_and_in_range_1_to_5():
    """사다리 1~5단계 — team_min(1)→same_shift_hard_k(2)→isolated_work(3)→
    ban_night_before_fixed_off(4, 2026-10-12 dev c1fd5d5 신규)→n2n_min_gap(5, 종전 4에서
    밀림). roster_create_service.py `_ladder_retry` 블록의 실행 순서와 같다(2026-10-12
    사용자 결정 — 사다리 번호는 실행 순서대로 매긴다)."""
    for spec in RULES.values():
        has_ladder = spec.ladder_stage is not None
        assert has_ladder == (spec.strength == HARD_AUTO_RELAX), spec.id
        if has_ladder:
            assert 1 <= spec.ladder_stage <= 5, (spec.id, spec.ladder_stage)


def test_module_is_one_of_three_known_values():
    allowed = {"lex", "primary", "common"}
    bad = {spec.id: spec.module for spec in RULES.values() if spec.module not in allowed}
    assert not bad, bad


def test_hard_lock_rule_nos_are_subset_of_1_to_8():
    for spec in list(RULES.values()) + list(RULE_PREFIXES.values()):
        assert all(1 <= n <= 8 for n in spec.hard_lock_rule_nos), (spec.id, spec.hard_lock_rule_nos)


def test_hard_lock_indirect_are_subset_of_1_to_8():
    for spec in list(RULES.values()) + list(RULE_PREFIXES.values()):
        assert all(1 <= n <= 8 for n in spec.hard_lock_indirect), (spec.id, spec.hard_lock_indirect)


def test_aux_rules_have_no_hard_lock_numbers():
    """AUX(목적식·보조 변수 정의)는 해를 좁히지 않으므로 하드락 번호를 달지 않는다."""
    for spec in RULES.values():
        if spec.strength == AUX:
            assert not spec.hard_lock_rule_nos, spec.id
            assert not spec.hard_lock_indirect, spec.id


def test_freezes_targets_are_registered_aux_rules():
    for spec in RULES.values():
        for tag in spec.freezes:
            target = RULES.get(tag)
            assert target is not None, f"{spec.id}.freezes 의 '{tag}' 가 RULES 에 없음"
            assert target.strength == AUX, f"{spec.id}.freezes 의 '{tag}' 는 AUX 가 아님({target.strength})"


def test_dead_keys_are_disjoint_from_governing_keys():
    """dead_keys 는 '코드가 읽지만 효과가 없는 키' 라 governing_keys 에서 뺀다
    (ei3/RULES_FINAL.md §3.2 10-09 — `runtime._2n2off_pre_injected` 사례: "governing_keys 에서 뺐다").
    그래서 두 집합은 겹치지 않아야 한다(부분집합이 아니라 배제)."""
    for spec in RULES.values():
        overlap = set(spec.dead_keys) & set(spec.governing_keys)
        assert not overlap, f"{spec.id}: dead_keys 가 governing_keys 에도 있음 {overlap}"


def test_hard_lock_1_to_7_each_have_a_direct_rulespec():
    """하드락 1~7 은 직접 구현(hard_lock_rule_nos) RuleSpec 이 최소 1개 있다.
    #8(MID)은 제약식이 아니라 변수 집합(use_mid → shift_types 에 M)으로 구현돼 붙일 지점이
    없다(ei3/RULES_FINAL.md §2 하드락 매핑표 8행 — "제약식이 아니라 근무 종류 집합")."""
    covered: set[int] = set()
    for spec in RULES.values():
        covered.update(spec.hard_lock_rule_nos)
    missing = set(range(1, 8)) - covered
    assert not missing, f"직접 구현 RuleSpec 이 없는 하드락: {missing}"
    assert 8 not in covered


def test_rule_prefix_keys_match_their_rulespec_id_prefix():
    for prefix, spec in RULE_PREFIXES.items():
        assert spec.id.startswith(prefix), (prefix, spec.id)


def test_lookup_exact_match():
    assert lookup("off_cap") is RULES["off_cap"]
    assert is_registered("off_cap")


def test_lookup_registered_prefix_match():
    assert lookup("fixed_assignment:fixed_wanted") is RULE_PREFIXES["fixed_assignment:"]
    assert is_registered("fixed_assignment:fixed_wanted")
    assert lookup("monthly_limit_n") is RULE_PREFIXES["monthly_limit_"]
    assert is_registered("monthly_limit_n")


def test_lookup_unregistered_id_returns_none():
    assert lookup("이런_id는_RULES에_없다") is None
    assert not is_registered("이런_id는_RULES에_없다")
    # 접두와 글자가 비슷해도 실제로 그 접두로 "시작"하지 않으면 미등록이다.
    assert not is_registered("fixed_assignmentX")


# ── 3. governing_keys 문법 ──────────────────────────────────────────────

#: DB 컬럼이 아닌 값의 허용 접두(D9). ei3/RULES_FINAL.md §3.2 · rule_tag.py·rules.py docstring.
_ALLOWED_NON_DB_PREFIXES = frozenset({"runtime", "env", "const"})

#: roster_config 의 실제(raw) 컬럼이지만 ORM 매핑이 없는 허용 목록.
#: 근거: ei3/RULES_FINAL.md §3.2 "접두 없음 = roster_config 컬럼(raw n2n_min_gap 포함)".
#: `n2n_min_gap` 은 DDL 로만 존재하고 `app/db/models.py` 의 `RosterConfig` 에는 매핑돼 있지
#: 않다(raw 컬럼 헬퍼로만 읽는다 — 설계서 §5.3 EI-9 9d).
_RAW_ROSTER_CONFIG_COLUMNS = frozenset({"n2n_min_gap"})


def _all_governing_keys():
    for spec in list(RULES.values()) + list(RULE_PREFIXES.values()):
        yield spec.id, spec.governing_keys


def _roster_config_orm_attrs() -> set[str]:
    return set(sa_inspect(RosterConfig).columns.keys())


def _table_has_column(table_name: str, column_name: str) -> bool:
    table = Base.metadata.tables.get(table_name)
    if table is None:
        return False
    return column_name in {c.key for c in table.columns} or column_name in {c.name for c in table.columns}


def _resolve_table_column_key(key: str) -> bool:
    """'table.column' 꼴 governing_key 가 SQLAlchemy 메타데이터에 있는지(KEY_ALIASES 반영)."""
    table_name, _, column_name = key.partition(".")
    if _table_has_column(table_name, column_name):
        return True
    alias = KEY_ALIASES.get(key)
    if alias:
        alt_table, _, alt_column = alias.partition(".")
        return _table_has_column(alt_table, alt_column)
    return False


def test_governing_keys_follow_known_grammar():
    """모든 governing_key 는 셋 중 하나다 — 접두 없음(roster_config ORM 속성 또는 raw 허용
    목록) · '테이블.컬럼'(Base.metadata, KEY_ALIASES 반영) · 'runtime.'/'env.'/'const.' 접두.
    그 밖의 형태는 없다(§5.3 EI-3 governing_keys 문법)."""
    roster_attrs = _roster_config_orm_attrs()
    bad: list[str] = []
    for rule_id, keys in _all_governing_keys():
        for key in keys:
            prefix, sep, _rest = key.partition(".")
            if not sep:
                if key not in roster_attrs and key not in _RAW_ROSTER_CONFIG_COLUMNS:
                    bad.append(f"{rule_id}: '{key}' — roster_config 속성도 raw 허용 목록도 아님")
                continue
            if prefix in _ALLOWED_NON_DB_PREFIXES:
                continue
            if not _resolve_table_column_key(key):
                bad.append(f"{rule_id}: '{key}' — 테이블·컬럼 메타데이터에 없음")
    assert not bad, "\n".join(bad)


def test_key_aliases_physical_targets_exist_in_metadata():
    for canonical_key, physical_key in KEY_ALIASES.items():
        table_name, _, column_name = physical_key.partition(".")
        assert _table_has_column(table_name, column_name), (canonical_key, physical_key)


def test_raw_roster_config_allowlist_is_actually_used_and_stays_outside_orm():
    """허용 목록이 죽은 허용이 되지 않게 — RULES 가 실제로 쓰고, ORM 밖임도 다시 확인한다."""
    used_no_prefix_keys = {key for _rid, keys in _all_governing_keys() for key in keys if "." not in key}
    roster_attrs = _roster_config_orm_attrs()
    for raw_key in _RAW_ROSTER_CONFIG_COLUMNS:
        assert raw_key in used_no_prefix_keys, raw_key
        assert raw_key not in roster_attrs, raw_key


# ── 4. rules.py 는 순수 데이터 모듈 ─────────────────────────────────────

_FORBIDDEN_IMPORT_ROOTS = frozenset({"ortools", "db", "sqlalchemy"})
_FORBIDDEN_IMPORT_SUBSTRINGS = (
    "cp_sat",
    "roster_create_service",
    "roster_system",
    "assignment_service",
    "engine_input",
)


def test_rules_module_imports_no_engine_db_or_ortools():
    """`rules.py` 는 엔진·DB·ortools 를 import 하지 않는다(순수 데이터 — 모듈 docstring)."""
    tree = ast.parse(_RULES_PY.read_text(encoding="utf-8"))
    bad: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module] if node.module else []
        else:
            continue
        for name in names:
            root = name.split(".")[0]
            if root in _FORBIDDEN_IMPORT_ROOTS or any(sub in name for sub in _FORBIDDEN_IMPORT_SUBSTRINGS):
                bad.append(name)
    assert not bad, f"rules.py 가 순수 데이터 밖의 모듈을 import: {bad}"
