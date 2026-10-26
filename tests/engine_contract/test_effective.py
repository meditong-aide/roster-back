"""`effective`·`effective_hard_lock` 계약 테스트 — EI-8 설계서 §4.1·§4.8·§4.9.

이 파일은 계약 자체(예외·별칭·공유·래퍼 성질·가벼움·DB 0·골든 대표값)를 본다. 63키 전체에
걸친 데이터 주도 검증(키 표·요구인원·env·범위·§7.3)은
`tests/engine_contract/test_effective_key_table.py` 에 있다.
"""

from __future__ import annotations

import ast
import contextlib
import io
import json
import subprocess
import sys
import textwrap
import types
from pathlib import Path

import pytest
from engine_input.golden_gate import GOLDEN, require_golden
from services.engine_contract import settings as S
from services.engine_contract.effective import (
    HARD_LOCK_CFG_KEYS,
    HARD_LOCK_NON_CFG_KEYS,
    NotInConfig,
    UnknownSettingKey,
    canonical_key,
    effective,
    effective_hard_lock,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = REPO_ROOT / "app"
#: 골든 경로의 정본은 `engine_input.golden_gate.GOLDEN` 이다 — 이 이름은 별칭이다(D29 · 골든은 로컬 전용).
GOLDEN_DIR = GOLDEN.path
BASE: dict[str, object] = {"daily_shift_requirements": {"D": 1, "E": 1, "N": 1}}


@pytest.fixture(autouse=True)
def _no_mcw_env(monkeypatch):
    """`AIDE_MAX_CONSEC_WORK` 가 dotenv 로 이미 들어왔을 수 있어 매 테스트 지운다."""
    monkeypatch.delenv("AIDE_MAX_CONSEC_WORK", raising=False)


@pytest.fixture(scope="module")
def _engine():
    from services.cp_sat_basic import CPSATBasicEngine

    return CPSATBasicEngine()


@pytest.fixture
def base_cfg(_engine):
    with contextlib.redirect_stdout(io.StringIO()):
        return _engine.create_config_from_db(dict(BASE))


def _build(engine, config_data: dict) -> tuple[object, type[BaseException] | None]:
    """`create_config_from_db` 를 print 억제 하에 호출하고 (cfg, 예외타입) 을 돌려준다."""
    with contextlib.redirect_stdout(io.StringIO()):
        try:
            return engine.create_config_from_db(config_data), None
        except Exception as ex:  # noqa: BLE001 — 예외 타입 자체가 기대값이다
            return None, type(ex)


def _load_golden_config_data(name: str) -> dict:
    require_golden(GOLDEN)  # D29 — 골든이 없는 체크아웃에서는 건너뜀(REQUIRE_GOLDEN=1 이면 실패) · 일부만 있으면 실패
    data = json.loads((GOLDEN_DIR / f"{name}.json").read_text(encoding="utf-8"))
    return data["layers"]["L1"][0]["config_data"]


# ── `effective()` 계약 (§4.8) ────────────────────────────────────────────


@pytest.mark.parametrize(
    "bad_key",
    ["no_such_key", "global_monthly_off_days", "runtime.team_handoff_policy_by_team", "shift_definitions"],
)
def test_unknown_setting_key_rejects_non_registry_spellings(base_cfg, bad_key):
    """`rules.py` 철자·`CODE_CONSTS`·`RUNTIME_DATA_NOT_SETTINGS` 키는 정본·별칭이 아니다."""
    with pytest.raises(UnknownSettingKey):
        effective(base_cfg, bad_key)
    with pytest.raises(KeyError):  # UnknownSettingKey 는 KeyError 로도 잡힌다
        effective(base_cfg, bad_key)


@pytest.mark.parametrize(
    "key",
    ["cross_month_hard_rules_enable", "version", "off_swap_enabled", "day_req"],
)
def test_not_in_config_for_out_of_scope_keys(base_cfg, key):
    with pytest.raises(NotInConfig) as exc_info:
        effective(base_cfg, key)
    assert exc_info.value.key == key
    assert exc_info.value.source == S.SETTINGS_BY_KEY[key].source


def test_not_in_config_for_grade_row_alias(base_cfg):
    # GRADE_ROW 는 별도 테이블 로더가 채우는 키라 cfg 에 대응 속성이 없다.
    with pytest.raises(NotInConfig):
        effective(base_cfg, "roster_grade_config.constraints_json")


def test_not_in_config_default_short_circuits(base_cfg):
    sentinel = object()
    assert effective(base_cfg, "version", default=sentinel) is sentinel
    assert effective(base_cfg, "day_req", default=None) is None


def test_cfg_mapping_input_raises_type_error():
    with pytest.raises(TypeError, match="effective_hard_lock"):
        effective(dict(BASE), "max_conseq_work")


def test_alias_resolves_to_canonical_key_for_every_registry_alias():
    assert canonical_key("max_consecutive_work_days") == "max_conseq_work"
    for spec in S.SETTINGS:
        for alias in spec.aliases:
            assert canonical_key(alias) == spec.key


#: 레지스트리를 레지스트리로 검증하는 순환을 피한다 — 실제 소비자 철자를 리터럴로 고정한다
#: (리뷰 반영, review_m/probe_misuse_env.py (3)·roster_ir.py:135·constraint_impact/control.py:348,408-409).
_EXTERNAL_ALIAS_SPELLINGS: tuple[tuple[str, str], ...] = (
    ("max_night_shifts_per_month", "max_nig_per_month"),  # constraint_impact/control.py:348
    ("max_consecutive_work_days", "max_conseq_work"),  # constraint_impact/control.py:408
    ("max_consecutive_nights", "three_seq_nig"),  # constraint_impact/control.py:409 · roster_ir.py:133
    ("max_consecutive_work", "max_conseq_work"),  # ontology_graph/roster_ir.py:135
)


@pytest.mark.parametrize("alias,expected_key", _EXTERNAL_ALIAS_SPELLINGS)
def test_canonical_key_resolves_real_consumer_alias_spellings(alias, expected_key):
    assert canonical_key(alias) == expected_key


def test_effective_rejects_non_nurse_roster_config_objects():
    """ORM 행·pydantic 모델·SimpleNamespace 는 정본 키 이름 속성이 있어도 거절한다 — 별칭·
    하드락 변환을 거치지 않은 원시값이 '실효값'으로 둔갑하는 것을 막는다(리뷰 반영,
    review_m/probe_misuse_env.py (1))."""
    from db.models import RosterConfig
    from schemas.roster_schema import RosterConfigCreate

    row = {"max_conseq_work": 7, "three_seq_nig": True}
    orm = RosterConfig(**row)
    ns = types.SimpleNamespace(**row)
    pyd = RosterConfigCreate(
        min_exp_per_shift=3, req_exp_nurses=1, two_offs_per_week=True, max_nig_per_month=0,
        three_seq_nig=True, two_offs_after_three_nig=True, two_offs_after_two_nig=False,
        banned_day_after_eve=False, max_conseq_work=7, off_days=8, shift_priority=0.7,
        sequential_offs=True, nod_noe=True,
    )
    for obj in (orm, ns, pyd):
        with pytest.raises(TypeError, match="create_config_from_db"):
            effective(obj, "max_conseq_work")


def test_effective_is_read_only(base_cfg):
    before = dict(vars(base_cfg))
    effective(base_cfg, "max_conseq_work")
    effective(base_cfg, "day_req", default=None)
    assert vars(base_cfg) == before


def test_effective_shares_mutable_objects_not_copies(_engine):
    """현행 고정 — `create_config_from_db` 가 일부 입력을 복사 없이 그대로 가리킨다(§1.6)."""
    cd = {**BASE, "team_min_by_team": {"1": {"D": 1}}}
    with contextlib.redirect_stdout(io.StringIO()):
        cfg = _engine.create_config_from_db(cd)
    assert effective(cfg, "team_min_by_team") is cd["team_min_by_team"]


def test_direct_nurse_roster_config_lacks_setattr_only_fields():
    """`effective` 는 `create_config_from_db` 결과를 전제한다 — 직접 만든 cfg 는 setattr
    전용 속성이 없어 `NotInConfig` 가 난다. dataclass 필드는 그 필드 기본값이 그대로 나온다."""
    from db.roster_config import NurseRosterConfig

    cfg = NurseRosterConfig()
    with pytest.raises(NotInConfig):
        effective(cfg, "ban_e_to_d")
    assert effective(cfg, "ban_n_to_d") is True


# ── `effective_hard_lock()` 모양·범위 ─────────────────────────────────────


def test_hard_lock_key_shape():
    assert HARD_LOCK_CFG_KEYS == (
        "max_conseq_work",
        "banned_day_after_eve",
        "three_seq_nig",
        "two_offs_after_three_nig",
        "two_offs_after_two_nig",
        "max_nig_per_month",
        "not_one_night",
        "use_mid",
        "ban_e_to_d",
        "ban_n_to_d",
        "ban_n_to_e",
    )
    assert HARD_LOCK_NON_CFG_KEYS == ("cross_month_hard_rules_enable", "cross_month_lookback_days")
    assert set(HARD_LOCK_CFG_KEYS).isdisjoint(HARD_LOCK_NON_CFG_KEYS)
    all_hard_lock_keys = {s.key for s in S.SETTINGS if isinstance(s.hard_lock, S.HardLock)}
    assert set(HARD_LOCK_CFG_KEYS) | set(HARD_LOCK_NON_CFG_KEYS) == all_hard_lock_keys
    assert len(all_hard_lock_keys) == 13


_EMPTY_CONFIG_EXPECTED = {
    "max_conseq_work": 5,
    "banned_day_after_eve": True,
    "three_seq_nig": 3,
    "two_offs_after_three_nig": True,
    "two_offs_after_two_nig": False,
    "max_nig_per_month": 15,
    "not_one_night": False,
    "use_mid": False,
    "ban_e_to_d": True,
    "ban_n_to_d": True,
    "ban_n_to_e": True,
}


def test_empty_config_gives_engine_default_hard_lock():
    """DESIGN_V2 '신규 config before = 빈 config 의 엔진 실효값'(:1991·:1294)."""
    with contextlib.redirect_stdout(io.StringIO()):
        assert effective_hard_lock({}) == _EMPTY_CONFIG_EXPECTED


def test_requirement_independence_matches_direct_cfg(_engine):
    """요구인원이 무엇이든(자리표시로 덮이기 전 원본 기준) 래퍼 결과가 **그 요구인원으로 직접
    만든 cfg** 의 effective 11키와 같다(§4.9④). '래퍼 vs 래퍼'는 자리표시가 늘 같은 값으로
    덮어써 동어반복이 된다(리뷰 반영) — E6(use_mid 를 요구인원에서 추론)·E11(N 요구치로
    월 N 상한을 바꿈) 같은 변이가 있으면 이 비교가 깨진다."""
    variants = [
        {"daily_shift_requirements": {"D": 1}},
        {"daily_shift_requirements": {"D": 5, "E": 4, "N": 3, "M": 2}},  # use_mid 꺼짐인데 M 요구
        {**BASE, "daily_shift_requirements_by_day": [{"D": 2, "E": 2, "N": 2}]},
        {"daily_shift_requirements": {"D": 9, "E": 9, "N": 9}},  # 구 필드 day_req 등 폴백은 EI-13d 에서 사라졌다
    ]
    with contextlib.redirect_stdout(io.StringIO()):
        for variant in variants:
            wrapped = effective_hard_lock(variant)
            cfg, err = _build(_engine, dict(variant))
            assert err is None
            direct = {k: effective(cfg, k) for k in HARD_LOCK_CFG_KEYS}
            assert wrapped == direct


@pytest.mark.parametrize(
    "config_data",
    [
        {"daily_shift_requirements": None},
        {"daily_shift_requirements": {}},
        {"daily_shift_requirements": {"D": 0}},
        {"day_req": 0, "eve_req": 0, "nig_req": 0},
    ],
)
def test_wrapper_always_overrides_empty_or_zero_requirements(config_data):
    """래퍼는 요구인원이 비었거나 0 이어도(엔진이라면 ValueError 를 낼 입력) 자리표시로
    덮어써 늘 성공한다 — 이 성질 자체를 고정한다(리뷰 반영, review_m/probe_placeholder2.py).
    신규 병동(요구인원 미설정)의 'before 실효값' 계산이 이 성질에 기댄다(DESIGN_V2:1991)."""
    with contextlib.redirect_stdout(io.StringIO()):
        assert effective_hard_lock(dict(config_data)) == _EMPTY_CONFIG_EXPECTED


def test_null_preserved_not_coerced_to_false():
    """Q16 'NULL→꺼짐 현행 유지' — bool() 로 접지 않고 None 을 그대로 낸다."""
    with contextlib.redirect_stdout(io.StringIO()):
        r1 = effective_hard_lock({"two_offs_after_three_nig": None})
        r2 = effective_hard_lock({"banned_day_after_eve": None})
    assert r1["two_offs_after_three_nig"] is None
    assert r2["banned_day_after_eve"] is None
    assert r2["ban_e_to_d"] is False and r2["ban_n_to_d"] is False and r2["ban_n_to_e"] is False


def test_input_alias_not_normalized():
    """별칭 정규화는 생성 진입(`engine_contract.aliases`)이 하고 `create_config_from_db` 는 정본만 읽는다 — 이 거울은 정규화하지 않는다."""
    with contextlib.redirect_stdout(io.StringIO()):
        r1 = effective_hard_lock({"max_consecutive_nights": 7})  # three_seq_nig 의 별칭
        r2 = effective_hard_lock({"max_consecutive_work_days": 7})  # max_conseq_work 의 별칭
        r3 = effective_hard_lock({"max_night_shifts_per_month": 20})  # max_nig_per_month 의 별칭
    assert r1["three_seq_nig"] == 3  # 별칭을 무시하고 정본 키 부재 기본값(3)
    assert r2["max_conseq_work"] == 5
    assert r3["max_nig_per_month"] == 15


def test_input_dict_is_not_mutated():
    cd = {"max_conseq_work": 4}
    snapshot = dict(cd)
    with contextlib.redirect_stdout(io.StringIO()):
        effective_hard_lock(cd)
    assert cd == snapshot


def test_exception_propagates_not_swallowed():
    with contextlib.redirect_stdout(io.StringIO()), pytest.raises(TypeError):
        effective_hard_lock({"max_conseq_work": None})


def test_soft_max_truthy_shields_none_max_conseq_work():
    """R-1 — `soft_max_consecutive_work_days` 가 있으면 `max_conseq_work=None` 이 TypeError
    없이 cfg 에 그대로 들어간다(현행 결함, 고치지 않는다)."""
    with contextlib.redirect_stdout(io.StringIO()):
        r = effective_hard_lock({"max_conseq_work": None, "soft_max_consecutive_work_days": 4})
    assert r["max_conseq_work"] is None


def test_hard_lock_outside_key_none_also_raises():
    """Q4 — 하드락 밖 키의 None 도 래퍼를 실패시킨다(하드락 입력 키만 거르지 않는다)."""
    with contextlib.redirect_stdout(io.StringIO()), pytest.raises(TypeError):
        effective_hard_lock({"shift_priority": None})


def test_env_is_read_at_call_time_not_frozen(monkeypatch):
    monkeypatch.setenv("AIDE_MAX_CONSEC_WORK", "4")
    with contextlib.redirect_stdout(io.StringIO()):
        r_env = effective_hard_lock({})
    assert r_env["max_conseq_work"] == 4
    monkeypatch.delenv("AIDE_MAX_CONSEC_WORK", raising=False)
    with contextlib.redirect_stdout(io.StringIO()):
        r_no_env = effective_hard_lock({})
    assert r_no_env["max_conseq_work"] == 5  # env 를 지우면 굳지 않고 되돌아온다


def _boundary_hard_lock_snippet() -> str:
    """월경계 빌더(`build_cross_month_constraints`)의 하드락 해석 구간만 원문 그대로 떼어
    돌려준다(리뷰 반영, review_m/probe_boundary_vs_effective.py — DB 는 그 구간 뒤에 나와
    여기선 0 이다)."""
    src = (APP_ROOT / "services" / "roster_create_service.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "build_cross_month_constraints")
    lines = src.splitlines()
    start = next(i for i in range(fn.lineno - 1, fn.end_lineno) if lines[i].strip().startswith("def _safe_int"))
    end = next(
        i for i in range(start, fn.end_lineno)
        if lines[i].strip().startswith("L = 3 if bool(config_dict.get('three_seq_nig', False)) else 2")
    )
    return textwrap.dedent("\n".join(lines[start : end + 1]))


def _boundary_hard_lock(config_dict: dict) -> dict:
    ns: dict = {"config_dict": config_dict}
    exec(_boundary_hard_lock_snippet(), ns)  # noqa: S102 — 설계 §4.9 보강, 빌더 원문 그대로 비교
    return {"max_conseq_work": ns["K"], "three_seq_nig": ns["L"]}


def test_boundary_builder_ignores_alias_and_diverges_from_effective_only_on_default():
    """월경계 빌더는 부재=꺼짐(2)이라 cfg 단계 `effective`(부재=3)와 다르다(docstring §5.3 보강 근거 — 리뷰 반영,
    review_m/probe_boundary_vs_effective.py). D13 ①(EI-11 11b) 뒤 빌더는 별칭 `max_consecutive_nights` 를 읽지 않아
    별칭 7 의 경계 값이 7 이 아니라 2 다(옛 이름 `…_on_defaults_and_alias` — 별칭 우선이던 때). 정본이 있으면 둘이 같다."""
    with contextlib.redirect_stdout(io.StringIO()):
        empty = effective_hard_lock({})
        aliased = effective_hard_lock({"max_consecutive_nights": 7})
        canon_on = effective_hard_lock({"three_seq_nig": True, "max_consecutive_nights": 7})
        canon_off = effective_hard_lock({"three_seq_nig": False, "max_consecutive_nights": 7})
    boundary_empty = _boundary_hard_lock({})
    boundary_aliased = _boundary_hard_lock({"max_consecutive_nights": 7})
    assert (empty["three_seq_nig"], boundary_empty["three_seq_nig"]) == (3, 2)
    assert (aliased["three_seq_nig"], boundary_aliased["three_seq_nig"]) == (3, 2)
    assert (canon_on["three_seq_nig"], _boundary_hard_lock({"three_seq_nig": True, "max_consecutive_nights": 7})["three_seq_nig"]) == (3, 3)
    assert (canon_off["three_seq_nig"], _boundary_hard_lock({"three_seq_nig": False, "max_consecutive_nights": 7})["three_seq_nig"]) == (2, 2)


def test_print_not_suppressed(capsys):
    """로그 억제 안 함을 고정 — `contextlib.redirect_stdout` 은 쓰지 않는다(docstring 근거)."""
    effective_hard_lock({})
    captured = capsys.readouterr()
    assert len(captured.out.splitlines()) == 7


def _block_db_calls() -> tuple[list, list[str]]:
    """소켓·sqlalchemy Engine·pymssql 접속 지점을 막고 (원복용 목록, 호출 기록)을 돌려준다.

    소켓만 막으면 pymssql(FreeTDS C 소켓)은 파이썬 레벨을 거치지 않아 새어나간다(리뷰 반영,
    review_m/probe_socket_patch.py) — sqlalchemy Engine·pymssql.connect 도 같이 막는다.
    """
    import socket as socket_module

    import sqlalchemy.engine.base as seb

    targets = [(socket_module.socket, "connect"), (seb.Engine, "connect"), (seb.Engine, "raw_connection")]
    try:
        import pymssql

        targets.append((pymssql, "connect"))
    except ImportError:
        pass
    originals = [(owner, attr, getattr(owner, attr)) for owner, attr in targets]
    calls: list[str] = []

    def _boom(label):
        def _f(*a, **k):
            calls.append(label)
            raise AssertionError(f"effective_hard_lock 이 {label} 를 호출했다")

        return _f

    for owner, attr in targets:
        setattr(owner, attr, _boom(attr))
    return originals, calls


def test_effective_hard_lock_never_touches_db():
    """DB·네트워크 접속을 막은 상태에서도 성공해야 한다(순수 함수 — EI8_SPEC §3)."""
    originals, calls = _block_db_calls()
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            result = effective_hard_lock({})
    finally:
        for owner, attr, original in originals:
            setattr(owner, attr, original)
    assert calls == []
    assert result == _EMPTY_CONFIG_EXPECTED


def test_import_stays_light_in_fresh_subprocess():
    """모듈 수준 import 만으로는 ortools·numpy·sqlalchemy·dotenv·엔진이 전혀 올라오지 않는다."""
    code = (
        "import sys; sys.path.insert(0, 'app'); "
        "import services.engine_contract.effective; "
        "print(sorted(m for m in "
        "('ortools','numpy','sqlalchemy','dotenv','db.client2','services.cp_sat_basic') "
        "if m in sys.modules))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()
    assert out == "[]"


def test_import_shape_only_one_lazy_engine_import():
    """모듈 수준 import 는 `__future__`·`collections.abc`·상대 `.settings` 뿐이고, 함수 안
    import 는 `effective_hard_lock` 안의 `services.cp_sat_basic` 하나뿐이다."""
    src = (APP_ROOT / "services" / "engine_contract" / "effective.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    module_level_modules: set[str] = set()
    lazy_imports: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        is_module_level = node in tree.body
        if isinstance(node, ast.ImportFrom):
            module = "." * node.level + (node.module or "")
            if is_module_level:
                module_level_modules.add(module)
            else:
                lazy_imports.append((_enclosing_function(tree, node), module))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if is_module_level:
                    module_level_modules.add(alias.name)
                else:
                    lazy_imports.append((_enclosing_function(tree, node), alias.name))
    assert module_level_modules <= {"__future__", "collections.abc", ".settings"}
    assert lazy_imports == [("effective_hard_lock", "services.cp_sat_basic")]


def _enclosing_function(tree: ast.Module, target: ast.AST) -> str | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and target in ast.walk(node):
            return node.name
    return None


# ── 골든 L1 14건 — 래퍼 = 원본 요구인원 cfg(§4.9 ⑨) ───────────────────────

_GOLDEN_L1_NAMES = (
    "S1", "S1-D", "S1-L", "S1-L3", "S1-LB", "S1-LT", "S1-P",
    "S2", "S3", "S4", "S5", "S6", "S7", "S8",
)

_GOLDEN_FIXED_EXPECTED = {
    "S1": _EMPTY_CONFIG_EXPECTED,
    "S2": {
        "max_conseq_work": 5, "banned_day_after_eve": None, "three_seq_nig": 2,
        "two_offs_after_three_nig": None, "two_offs_after_two_nig": None,
        "max_nig_per_month": 15, "not_one_night": False, "use_mid": False,
        "ban_e_to_d": False, "ban_n_to_d": False, "ban_n_to_e": False,
    },
    "S4": {**_EMPTY_CONFIG_EXPECTED, "use_mid": True},
    "S7": {**_EMPTY_CONFIG_EXPECTED, "two_offs_after_two_nig": True, "not_one_night": True},
}


@pytest.mark.parametrize("name", _GOLDEN_L1_NAMES)
def test_golden_equivalence_with_direct_cfg(name, _engine):
    """래퍼(`effective_hard_lock`, 요구인원 자리표시) == 원본 요구인원으로 직접 만든 cfg 의
    `effective` 11키. S3 은 둘 다 TypeError 다."""
    config_data = _load_golden_config_data(name)
    with contextlib.redirect_stdout(io.StringIO()):
        try:
            wrapped = effective_hard_lock(config_data)
            wrapped_err = None
        except TypeError:
            wrapped = None
            wrapped_err = TypeError
        try:
            cfg = _engine.create_config_from_db(dict(config_data))
            direct = {k: effective(cfg, k) for k in HARD_LOCK_CFG_KEYS}
            direct_err = None
        except TypeError:
            direct = None
            direct_err = TypeError
    assert wrapped_err == direct_err
    if wrapped_err is None:
        assert wrapped == direct
    if name in _GOLDEN_FIXED_EXPECTED:
        assert wrapped == _GOLDEN_FIXED_EXPECTED[name]
