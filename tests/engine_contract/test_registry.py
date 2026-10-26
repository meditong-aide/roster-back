"""설정·간호사 속성 레지스트리 계약 테스트 — 설계서 §2.6 · §5.3 EI-6.

C1(ORM 컬럼 커버리지 + raw SQL 컬럼 문자열 스캔) · C2'(하드락·인접·영향 키 엔진 AST 기본값 +
순수 함수 실측) · env(엔진 경로 env ⊆ ENV 표, 좁은 스캔·넓은 스캔 양방향 + EnvSpec 구조) ·
정합(키 중복·별칭 충돌·hard_lock 번호·editor 어휘·hidden_no 범위·rules.py governing_keys
커버리지·SETTINGS_BY_RULE_KEY 유일 소유) 을 본다. 레지스트리 자신
(`services.engine_contract.*`)은 순수 데이터라 이 테스트가 유일한 '현재 엔진 코드와 맞는지'
확인 지점이다.

C2' AST 부분은 DB·ortools·엔진을 실행하지 않는다 — 대상 파일의 텍스트를 열어 `ast.parse`
할 뿐이다. `test_c2_pure_function_*` 류는 예외다(EI-6 반박 검토 MEDIUM 반영) — DB·ortools 는
여전히 안 쓰지만, `CPSATBasicEngine.create_config_from_db`·`build_cross_month_constraints`·
`build_mid_month_boundary_constraints` 를 실제로 호출해 흡수 테스트(순수 함수, 0.1초대)로
AST 가 못 잡는 합성 효과(여러 자리의 기본값이 서로 맞물려 만드는 실제 동작)를 잡는다.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest
from engine_input import canonical
from engine_input.engine_env import engine_env_names
from services.engine_contract import env as E
from services.engine_contract import nurse_attrs as N
from services.engine_contract import settings as S
from services.engine_contract.rules import RULE_PREFIXES, RULES

APP_ROOT = Path(__file__).resolve().parents[2] / "app"


def _read(relpath: str) -> str:
    return (APP_ROOT / relpath).read_text(encoding="utf-8")


# ── C1: roster_config ORM 컬럼 + raw 컬럼(실제 코드 스캔) ⊆ SETTINGS ───────


def test_c1_orm_columns_subset_of_settings():
    """roster_config 의 모든 ORM 매핑 컬럼은 SETTINGS 에 행이 있어야 한다(DEAD 포함)."""
    import db.models as models

    orm_cols = {c.name for c in models.RosterConfig.__table__.columns}
    missing = orm_cols - set(S.SETTINGS_BY_KEY)
    assert not missing, f"roster_config ORM 컬럼이 레지스트리에 없다: {sorted(missing)}"


def test_c1_orm_column_count_matches_investigation():
    """조사 당시 41개(ei6/inv_settings.json counts.orm_roster_config_columns) — 드리프트 감지용."""
    import db.models as models

    orm_cols = {c.name for c in models.RosterConfig.__table__.columns}
    assert len(orm_cols) == 41, (
        f"roster_config ORM 컬럼 수가 조사 시점(41)과 다르다({len(orm_cols)}) — "
        "새 컬럼이면 SETTINGS 에 행을 추가했는지 확인하라"
    )


#: roster_config 의 raw SQL 전용 컬럼명을 코드에서 직접 뽑는 정규식 3종(EI-6 반박 검토(MEDIUM)
#: 반영 — 예전 `test_c1_raw_columns_registered` 는 `S.RAW_COLUMNS`(=SETTINGS 에서 source==DB_RAW
#: 인 키를 역으로 뽑은 것)가 SETTINGS 에 있는지만 봐서 항진명제였다. 이제 코드 문자열 리터럴
#: 쪽에서 출발한다). 사본 변이 실측(C1b·C1c)으로 확인: 컬럼명을 바꾸면 이 스캔이 못 찾아 아래
#: 테스트가 못 잡는 게 아니라 **스캔된 이름이 바뀌어 미등록으로 걸린다**.
import re as _re  # noqa: E402

_RAW_COL_PATTERNS: tuple[_re.Pattern[str], ...] = (
    _re.compile(r"TABLE_NAME\s*=\s*'roster_config'\s+AND\s+COLUMN_NAME\s*=\s*'([A-Za-z_][A-Za-z0-9_]*)'"),
    _re.compile(r"SELECT\s+(?:TOP\s+\d+\s+)?([A-Za-z_][A-Za-z0-9_]*)\s+FROM\s+roster_config\b"),
    _re.compile(r"""_read_config_flag\(\s*db\s*,\s*group_id\s*,\s*["']([A-Za-z_][A-Za-z0-9_]*)["']"""),
)


def _scan_raw_roster_config_columns() -> set[str]:
    """`app/services/` 전체에서 raw SQL 로 `roster_config` 컬럼을 읽는 문자열 리터럴을 모은다."""
    found: set[str] = set()
    for path in (APP_ROOT / "services").rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pattern in _RAW_COL_PATTERNS:
            for match in pattern.finditer(text):
                found.add(match.group(1))
    return found


def test_c1_raw_columns_registered():
    """raw SQL 로 `roster_config` 컬럼을 읽는 모든 리터럴 이름이 SETTINGS 에 있어야 한다.

    (코드 스캔 → SETTINGS, 예전처럼 SETTINGS → SETTINGS 가 아니다.)
    """
    scanned = _scan_raw_roster_config_columns()
    assert scanned, "raw SQL 컬럼 스캔이 하나도 못 찾았다 — 패턴이 깨졌을 수 있다"
    missing = scanned - set(S.SETTINGS_BY_KEY)
    assert not missing, (
        f"raw SQL 이 읽는 roster_config 컬럼이 레지스트리에 없다: {sorted(missing)} — "
        "settings.py 에 행을 추가하라(ORM 비매핑이면 source=DB_RAW)"
    )


def test_c1_raw_columns_source_not_meta_or_derived():
    """raw SQL 로 읽는 컬럼은 META·DERIVED·REQUEST 출처로 잘못 등록되지 않았는지 본다. DB_RAW
    (ORM 비매핑)·DB_COLUMN(ORM 매핑과 병행 raw 읽기)이 보통이지만, `ban_night_before_fixed_off`
    처럼 ORM 매핑을 일부러 뺀(DDL DROP 대상, db/models.py:862 주석) 뒤 엔진에서는 ENGINE_KNOB
    으로 다루면서 후처리(oncall_postprocess.py)만 옛 raw 컬럼을 계속 읽는 과도기 패턴도 있다
    — 그 경우까지 DB_COLUMN 으로 강제하지 않는다."""
    disallowed = {S.META, S.DERIVED, S.REQUEST}
    for key in _scan_raw_roster_config_columns():
        spec = S.SETTINGS_BY_KEY[key]
        assert spec.source not in disallowed, f"{key}: source={spec.source!r} 가 raw SQL 읽기 출처로 부적절하다"


def test_c1_raw_columns_includes_known_dual_path_keys():
    """조사(EI-6)로 확인한 raw 읽기 5종이 전부 스캔에 걸려야 한다(드리프트 감지)."""
    expected = {
        "fixed_holiday_off_yn", "n2n_min_gap",
        "ban_night_before_fixed_off", "ban_night_before_fixed_wanted_off", "fixed_wanted_use_yn",
    }
    scanned = _scan_raw_roster_config_columns()
    missing = expected - scanned
    assert not missing, f"알려진 raw 읽기 컬럼이 스캔에서 빠졌다(패턴이 깨졌을 수 있다): {missing}"


# ── C2': 하드락·인접·영향 키 — 엔진 AST get/getattr 기본값 = current_default ──

#: 키 → ((app/ 상대경로, 정의 줄), ...) — 한 키를 실제로 결정짓는 자리가 둘 이상이면(예:
#: `.get(key, default)` 리터럴 자리 + 그 값을 미리 박아 넣는 `setdefault` 자리) 전부 적는다.
#: 조사 `ei6/inv_settings.json` 의 `current_default_site` 에서 가져왔다. `n2n_min_gap` 은
#: 그 필드가 가리키는 raw 헬퍼(`_n2n_min_gap_setting`, DB 필요) 대신, `create_config_from_db`
#: 안에서 실제로 `config_data.get("n2n_min_gap", 0)` 리터럴 기본값을 정의하는
#: `cp_sat_basic.py:635` 를 쓴다(§2.2 `current_default` — "키가 없을 때 현행 엔진 값"). ★raw
#: 헬퍼가 production 경로의 진짜 소스이고 이 AST 자리는 그 경로에서는 늘 덮인 뒤라 죽은
#: 기본값이라는 사실은 settings.py 의 n2n_min_gap notes 에 적어 둔다(EI-6 반박 검토(MEDIUM) —
#: C2' 는 DB 를 실행하지 않는다는 원칙을 지키기로 하고 raw 헬퍼 검증은 의도적으로 보류했다).
_C2_SITES: dict[str, tuple[tuple[str, int], ...]] = {
    "max_conseq_work": (("services/cp_sat_basic.py", 456),),
    "banned_day_after_eve": (("services/cp_sat_basic.py", 464),),
    "three_seq_nig": (("services/cp_sat_basic.py", 465),),
    "two_offs_after_three_nig": (("services/cp_sat_basic.py", 466),),
    "two_offs_after_two_nig": (("services/cp_sat_basic.py", 467),),
    "max_nig_per_month": (("services/cp_sat_basic.py", 479),),
    "not_one_night": (("services/cp_sat_basic.py", 468),),
    # EI-6 반박 검토(LOW) 반영 — 611 은 `max_same_shift` 를 읽는 자리였다(use_mid 읽기가
    # 아니다). 가장 가까운 실제 use_mid 자리인 595 로 정정.
    "use_mid": (("services/cp_sat_basic.py", 589),),
    "off_days": (("services/cp_sat_basic.py", 573),),
    "nod_noe": (("services/cp_sat_basic.py", 569),),
    "preceptee_on": (("services/cp_sat_basic.py", 587),),
    "fixed_wanted_use_yn": (("services/roster_create_service.py", 6078),),
    "ban_night_before_fixed_wanted_off": (("services/cp_sat_basic.py", 473),),
    "n2n_min_gap": (("services/cp_sat_basic.py", 629),),
    "ban_e_to_d": (("services/cp_sat_basic.py", 663),),
    "ban_n_to_d": (("services/cp_sat_basic.py", 666),),
    "ban_n_to_e": (("services/cp_sat_basic.py", 664),),
    "ban_d_to_n": (("services/cp_sat_basic.py", 665),),
    "ban_night_before_fixed_off": (("services/cp_sat_basic.py", 469),),
    "same_shift_hard_k": (("services/cp_sat_basic.py", 613),),
    "isolated_work_hard": (("services/cp_sat_basic.py", 615),),
    # EI-6 반박 검토(MEDIUM) 반영 — ban_night_minimize 가 새로 c2_target=True 가 됨(사다리 형제
    # same_shift_hard_k·isolated_work_hard 와 같은 대접).
    "ban_night_minimize": (("services/cp_sat_basic.py", 617),),
    # EI-6 반박 검토(HIGH) 반영 — 생성 경로는 `build_cross_month_constraints`(:2317-2318)의
    # `.get(key, default)` 보다 **먼저** `config_dict.setdefault(...)`(:5778-5779)가 돈 뒤라,
    # setdefault 자리가 실제 운영 기본값을 고정하는 자리다. 두 자리 모두 검증해 어느 한쪽만
    # 바뀌어도(둘이 벌어져도) 이 테스트가 잡는다.
    "cross_month_hard_rules_enable": (
        ("services/roster_create_service.py", 2317),
        ("services/roster_create_service.py", 5778),
    ),
    "cross_month_lookback_days": (
        ("services/roster_create_service.py", 2318),
        ("services/roster_create_service.py", 5779),
    ),
}


def _default_call_args(call: ast.Call) -> tuple[ast.expr, ast.expr] | None:
    """`.get(key, default)`·`.setdefault(key, default)`·`getattr(obj, key, default)` 에서
    (key 노드, default 노드) — 아니면 None."""
    func = call.func
    if isinstance(func, ast.Attribute) and func.attr in ("get", "setdefault") and call.args:
        key_node = call.args[0]
        default_node = call.args[1] if len(call.args) >= 2 else ast.Constant(value=None)
        return key_node, default_node
    if isinstance(func, ast.Name) and func.id == "getattr" and len(call.args) >= 2:
        key_node = call.args[1]
        default_node = call.args[2] if len(call.args) >= 3 else ast.Constant(value=None)
        return key_node, default_node
    return None


def _find_default(tree: ast.Module, key: str, near_line: int) -> tuple[object, int] | None:
    """`key` 리터럴을 읽는 get/getattr/setdefault 호출 중 `near_line` 에 가장 가까운 것의
    (기본값, 실제 줄). 기본값이 리터럴이 아니면 `ast.unparse` 문자열을 돌려준다."""
    best: tuple[int, ast.expr, int] | None = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        parsed = _default_call_args(node)
        if parsed is None:
            continue
        key_node, default_node = parsed
        if not (isinstance(key_node, ast.Constant) and key_node.value == key):
            continue
        distance = abs(node.lineno - near_line)
        if best is None or distance < best[0]:
            best = (distance, default_node, node.lineno)
    if best is None:
        return None
    _distance, default_node, found_line = best
    try:
        value = ast.literal_eval(default_node)
    except ValueError:
        value = f"<non-literal:{ast.unparse(default_node)}>"
    return value, found_line


def _c2_site_cases() -> list[tuple[str, str, int]]:
    return [(key, relpath, near_line) for key, sites in _C2_SITES.items() for relpath, near_line in sites]


@pytest.mark.parametrize("key,relpath,near_line", _c2_site_cases())
def test_c2_hard_lock_default_matches_engine_ast(key: str, relpath: str, near_line: int):
    spec = S.SETTINGS_BY_KEY[key]
    assert spec.c2_target, f"{key} 가 settings.py 에서 c2_target=True 가 아니다"
    tree = ast.parse(_read(relpath))
    found = _find_default(tree, key, near_line)
    assert found is not None, (
        f"{key}: {relpath}:{near_line} 근처에서 '{key}' 를 읽는 get/getattr/setdefault 호출을 "
        "못 찾았다(코드가 바뀌었으면 _C2_SITES 를 갱신하라)"
    )
    value, found_line = found
    current = spec.current_default
    assert str(value) == str(current), (
        f"{key}: 엔진 기본값 {value!r}({relpath}:{found_line}) != 레지스트리 current_default "
        f"{current!r} — 하드락 키의 기본값이 바뀌었다. 코드가 맞으면 settings.py 를, 레지스트리가 "
        "맞으면 코드를 고쳐라(완화 방향이면 EI-7 판정 대상)"
    )


def test_c2_targets_cover_exactly_the_registered_sites():
    """c2_target=True 인 키는 정확히 `_C2_SITES` 와 같아야 한다(빠뜨림·초과 둘 다 감지)."""
    assert set(S.c2_targets()) == set(_C2_SITES)
    assert len(S.c2_targets()) == 24


def test_c2_target_hard_lock_breakdown():
    """§4.2 CLAUDE.md 표 — EI-6 반박 검토(HIGH+MEDIUM) 반영: 하드락 13(+2 월경계 boundary_switch)
    · 인접 9(+1 ban_night_minimize) · 영향 2(−2 월경계가 하드락으로 이동) = 24."""
    hardlock = [k for k in S.c2_targets() if isinstance(S.SETTINGS_BY_KEY[k].hard_lock, S.HardLock)]
    adjacent = [k for k in S.c2_targets() if S.SETTINGS_BY_KEY[k].hard_lock == S.ADJACENT]
    affects = [k for k in S.c2_targets() if S.SETTINGS_BY_KEY[k].hard_lock == S.AFFECTS]
    assert len(hardlock) == 13
    assert len(adjacent) == 9
    assert len(affects) == 2


def test_hard_lock_boundary_switch_rule_nos_match_notes():
    """`kind='boundary_switch'` HardLock 은 rule_nos 가 비어 있지 않아야 한다(단일 rule_no 가
    아니라 여러 규칙을 한꺼번에 켜고 끄는 스위치라서)."""
    for s in S.SETTINGS:
        hl = s.hard_lock
        if isinstance(hl, S.HardLock) and hl.kind == "boundary_switch":
            assert hl.rule_nos, f"{s.key}: boundary_switch 인데 rule_nos 가 비었다"
            assert all(1 <= n <= 8 for n in hl.rule_nos), f"{s.key}: rule_nos={hl.rule_nos}"


# ── C2' 순수 함수 실측(EI-6 반박 검토 MEDIUM 반영) — DB·ortools 없이 absent 키 결과를 흡수 ──

#: 키 → (`NurseRosterConfig` 결과 필드명, absent(키 자체가 없을 때) 기대값). AST 자리 하나만
#: 보는 것과 달리 `create_config_from_db` 를 실제로 호출해 여러 지역변수·파생 로직이 섞인
#: 최종 결과를 본다(순수 함수 — DB·ortools 호출 없음, `tests/engine_input/ladder.py` 가 이미
#: 같은 엔진 클래스를 직접 import 하는 선례와 같은 방식).
_PURE_FN_ABSENT_EXPECT: dict[str, tuple[str, object]] = {
    "max_conseq_work": ("max_consecutive_work_days", 5),
    "banned_day_after_eve": ("banned_day_after_eve", True),
    "three_seq_nig": ("max_consecutive_nights", 3),
    "two_offs_after_three_nig": ("two_offs_after_three_nig", True),
    "two_offs_after_two_nig": ("two_offs_after_two_nig", False),
    "not_one_night": ("not_one_night", False),
    "max_nig_per_month": ("max_night_shifts_per_month", 15),
    "use_mid": ("use_mid", False),
    "ban_night_before_fixed_off": ("ban_night_before_fixed_off", True),
    "ban_night_before_fixed_wanted_off": ("ban_night_before_fixed_wanted_off", False),
}


@pytest.fixture(scope="module")
def _absent_key_cfg(monkeypatch_module_env):
    """`config_data` 에 하드락 키를 **하나도 안 넣고** `create_config_from_db` 를 부른 결과."""
    from services.cp_sat_basic import CPSATBasicEngine

    engine = CPSATBasicEngine()
    return engine.create_config_from_db({"daily_shift_requirements": {"D": 1, "E": 1, "N": 1}})


@pytest.fixture(scope="module")
def monkeypatch_module_env():
    """모듈 스코프 monkeypatch — AIDE_MAX_CONSEC_WORK 가 세팅값을 흔들지 않게 지운다."""
    mp = pytest.MonkeyPatch()
    mp.delenv("AIDE_MAX_CONSEC_WORK", raising=False)
    yield mp
    mp.undo()


@pytest.mark.parametrize("key", sorted(_PURE_FN_ABSENT_EXPECT))
def test_c2_pure_function_absent_matches_current_default(key: str, _absent_key_cfg):
    """`current_default` 가 AST 자리 문서화뿐 아니라 실제 `create_config_from_db` 호출 결과와도
    맞는지 — 순수 함수라 DB·ortools 없이 0.1초대(EI-6 반박 검토 MEDIUM)."""
    attr, expected = _PURE_FN_ABSENT_EXPECT[key]
    actual = getattr(_absent_key_cfg, attr)
    assert actual == expected, (
        f"{key}: config_data 에 키가 없을 때 cfg.{attr}={actual!r} != 기대값 {expected!r} "
        f"(current_default={S.SETTINGS_BY_KEY[key].current_default!r})"
    )


def test_c2_pure_function_cross_month_disabled_short_circuits_without_db():
    """`cross_month_hard_rules_enable=False` 또는 `cross_month_lookback_days<=0` 이면
    `build_cross_month_constraints` 가 db 를 건드리기 전에 빈 결과로 끝난다(EI-6 반박 검토
    HIGH 반영 — 월경계 하드락 #1·#2·#3·#4·#5·#7 통째 해제 경로를 순수 호출로 고정한다)."""
    from services.roster_create_service import build_cross_month_constraints

    empty = {"forced_off": {}, "forbidden": {}, "prev_month_n_offs_after": {}}
    assert build_cross_month_constraints(
        None, None, None, None, {"cross_month_hard_rules_enable": False}, [],
    ) == empty
    assert build_cross_month_constraints(
        None, None, None, None, {"cross_month_lookback_days": 0}, [],
    ) == empty


#: EI-6 반박 검토(MEDIUM — C2' 범위) 반영 — `HardLock.cap`(5·3·15)은 `create_config_from_db`
#: 쪽은 순수 함수 실측(위)으로 걸리지만, 월경계·월중 경계 폴백(roster_create_service.py 안,
#: 별도의 하드코딩 리터럴)은 그 쪽과 다른 자리라 따로 AST 로 고정한다.
_BOUNDARY_K_FALLBACK_LINES: tuple[int, ...] = (2461, 2667)  # `K = 5`(연속근무 상한 cap, #1)
_BOUNDARY_THREE_SEQ_DEFAULT_LINES: tuple[int, ...] = (2473, 2668)  # `three_seq_nig` 부재 기본값


def test_c2_boundary_k_fallback_is_5():
    """월경계·월중 경계의 `K`(연속근무 상한) 폴백이 하드락 #1 cap(5) 과 같은 리터럴인지 AST 로
    고정한다(roster_create_service.py:2461·2667 `if K is None: K = 5`)."""
    tree = ast.parse(_read("services/roster_create_service.py"))
    for near_line in _BOUNDARY_K_FALLBACK_LINES:
        best: tuple[int, int, object] | None = None
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "K"
                and isinstance(node.value, ast.Constant)
            ):
                distance = abs(node.lineno - near_line)
                if best is None or distance < best[0]:
                    best = (distance, node.lineno, node.value.value)
        assert best is not None, f"{near_line} 근처에서 `K = <literal>` 대입을 못 찾았다"
        _distance, found_line, value = best
        assert value == 5, f"K 폴백({found_line}) = {value!r} != 5(하드락 #1 cap)"


def test_c2_boundary_three_seq_nig_default_is_false():
    """월경계·월중 경계의 `three_seq_nig` 부재 기본값이 False(더 엄격한 2연속)인지 AST 로
    고정한다 — settings.py 의 three_seq_nig.null_behavior '월경계·월중 경계 키 부재→False(2)'
    서술의 근거(roster_create_service.py:2473·2668)."""
    tree = ast.parse(_read("services/roster_create_service.py"))
    for near_line in _BOUNDARY_THREE_SEQ_DEFAULT_LINES:
        found = _find_default(tree, "three_seq_nig", near_line)
        assert found is not None, f"{near_line} 근처에서 'three_seq_nig' get 호출을 못 찾았다"
        value, found_line = found
        assert value is False, f"three_seq_nig 폴백({found_line}) = {value!r} != False"


def test_c2_pure_function_mid_month_lookback_none_raises_before_db():
    """`cross_month_lookback_days=None` 이면 `build_mid_month_boundary_constraints` 가 db 를
    쓰기 전, 첫 줄 `int(None)` 에서 TypeError 를 낸다 — null_behavior 서술의 전제(EI-6 반박 검토 MEDIUM).
    예전엔 두 호출부가 이를 `except Exception` 으로 삼켜 월중 경계 하드가 조용히 빠졌고, EI-11 11q(G5)
    뒤로는 호출부가 생성을 멈춘다(`test_boundary_hard_lock_engine.test_boundary_builder_failure_stops_generation`)."""
    from services.roster_create_service import build_mid_month_boundary_constraints

    with pytest.raises(TypeError):
        build_mid_month_boundary_constraints(
            None, [], "g1", 2026, 1, {"cross_month_lookback_days": None}, {},
            outbound_assignments=None,
        )


# ── env: 엔진 경로가 읽는 env ⊆ ENV 표(좁은 스캔 + 넓은 스캔 양방향) ────────


def test_env_engine_scan_subset_of_registry():
    """`engine_env.py` 의 좁은 스캔(§3.1 — cp_sat·cp_sat_basic.py·roster_create_service.py
    만, 현재 80종)이 찾은 모든 이름은 이 ENV 표에 있어야 한다. 새 플래그가 생기면 이 테스트가
    빨개진다 — env.py 에 분류 1줄을 추가하라."""
    scanned = set(engine_env_names())
    missing = scanned - E.ENGINE_PATH_NAMES
    assert not missing, (
        f"엔진 경로가 읽는 env 가 ENV 표에 없다(분류 1줄을 추가하라): {sorted(missing)}"
    )


#: EI-6 반박 검토(HIGH) 반영 — `engine_env.py` 의 좁은 정규식 스캔(문자열 리터럴만, cp_sat·
#: cp_sat_basic.py·roster_create_service.py 만)은 constraints/·precheck/·ontology_graph/·
#: roster_system.py 에 새 env 가 생겨도, 변수로 넘긴 이름도, getenv 별칭도 못 잡는다(사본
#: 변이 실측 M1~M8 전부 거짓 녹색이었다). 아래는 그 파일들까지 AST 로 스캔하고 변수 상수·
#: getenv 별칭·`in os.environ` 까지 해석한다. `engine_env.py` 자신은 바꾸지 않는다(그 모듈의
#: 정확한 좁은 동작에 golden·모델지문 테스트가 기대고 있다 — 동작 불변).
_BROAD_ENV_SOURCES: tuple[str, ...] = (
    "cp_sat", "cp_sat_basic.py", "roster_create_service.py",
    "constraints", "precheck", "ontology_graph", "roster_system.py",
)
_ENVIRON_METHOD_ATTRS: frozenset[str] = frozenset({"get", "setdefault", "pop"})


def _broad_env_source_files() -> list[Path]:
    spec = importlib.util.find_spec("services")
    root = Path(next(iter(spec.submodule_search_locations)))
    files: list[Path] = []
    for rel in _BROAD_ENV_SOURCES:
        path = root / rel
        if path.is_dir():
            files.extend(sorted(path.rglob("*.py")))
        elif path.is_file():
            files.append(path)
    return files


def _const_str_map(tree: ast.Module) -> dict[str, str]:
    """이름 → 문자열 리터럴(단순 `NAME = "X"` 대입 전수 — getenv 호출에 변수로 넘기는 경우를
    해석하는 데 쓴다, M4 패턴)."""
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
            out[node.targets[0].id] = node.value.value
    return out


def _getenv_aliases(tree: ast.Module) -> set[str]:
    """`from os import getenv as X` 별칭(M5 패턴)."""
    aliases = {"getenv"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "os":
            for alias in node.names:
                if alias.name == "getenv":
                    aliases.add(alias.asname or alias.name)
    return aliases


def _resolve_str_literal(node: ast.expr, const_map: dict[str, str]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and node.id in const_map:
        return const_map[node.id]
    return None


def _is_environ_like(node: ast.expr) -> bool:
    if isinstance(node, ast.Name):
        return node.id == "environ"
    if isinstance(node, ast.Attribute):
        return node.attr == "environ"
    return False


def _scan_env_names_in_file(path: Path) -> set[str]:
    names: set[str] = set()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return names
    const_map = _const_str_map(tree)
    getenv_aliases = _getenv_aliases(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            matched = (isinstance(func, ast.Name) and func.id in getenv_aliases) or (
                isinstance(func, ast.Attribute)
                and (func.attr == "getenv" or (func.attr in _ENVIRON_METHOD_ATTRS and _is_environ_like(func.value)))
            )
            if matched and node.args:
                value = _resolve_str_literal(node.args[0], const_map)
                if value:
                    names.add(value)
        elif isinstance(node, ast.Subscript) and _is_environ_like(node.value):
            value = _resolve_str_literal(node.slice, const_map)
            if value:
                names.add(value)
        elif isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], ast.In):
            if node.comparators and _is_environ_like(node.comparators[0]):
                value = _resolve_str_literal(node.left, const_map)
                if value:
                    names.add(value)
    return {n for n in names if n.isupper()}


def broad_engine_env_names() -> frozenset[str]:
    """엔진 경로 전체(cp_sat·cp_sat_basic.py·roster_create_service.py·constraints/·
    precheck/·ontology_graph/·roster_system.py)의 AST 스캔 — 변수·별칭·`in` 연산자 포함."""
    out: set[str] = set()
    for path in _broad_env_source_files():
        out |= _scan_env_names_in_file(path)
    return frozenset(out)


def test_env_broad_ast_scan_subset_of_registry():
    """넓은 AST 스캔(constraints/·precheck/·ontology_graph/·roster_system.py 포함)의 결과가
    전부 ENV 표에 있어야 한다. 사본 변이 실측(M1~M8)으로 좁은 정규식 스캔은 이 8 가지를 전부
    놓친다는 것을 확인했다(EI-6 반박 검토 HIGH)."""
    scanned = broad_engine_env_names()
    missing = scanned - E.ENGINE_PATH_NAMES
    assert not missing, (
        f"엔진 경로(넓은 스캔)가 읽는 env 가 ENV 표에 없다: {sorted(missing)}"
    )


def test_env_registry_names_found_by_broad_scan():
    """역방향(EI-6 반박 검토 LOW) — ENV 표의 엔진 경로 이름(OUTSIDE_ENGINE 제외)은 전부 넓은
    스캔에서 다시 찾아져야 한다. 못 찾으면 코드에서 지워진(죽은) 행 후보다."""
    scanned = broad_engine_env_names()
    dead_candidates = E.ENGINE_PATH_NAMES - scanned
    assert not dead_candidates, (
        f"ENV 표에는 있는데 코드에서 더는 안 읽는 것 같은 이름(죽은 행 후보, 확인 필요): "
        f"{sorted(dead_candidates)}"
    )


def test_env_no_duplicate_names():
    names = [e.name for e in E.ENV]
    assert len(names) == len(set(names))


# ── env: EnvSpec 구조 정합(EI-6 반박 검토 MEDIUM) ─────────────────────────


def test_env_klass_vocabulary():
    for e in E.ENV:
        assert e.klass in E.KLASSES, f"{e.name}: klass={e.klass!r} 가 어휘 밖"


def test_env_digest_matches_klass():
    """digest 는 klass 에서 파생돼야 한다 — ENGINE_CONTRACT·EXPERIMENT 만 digest=True.
    실측(현재 88종) 결과는 이미 0 건 불일치라, 이 테스트는 드리프트 감지용이다."""
    for e in E.ENV:
        expected = e.klass in (E.ENGINE_CONTRACT, E.EXPERIMENT)
        assert e.digest == expected, (
            f"{e.name}: digest={e.digest} 가 klass={e.klass!r} 에서 기대하는 {expected} 와 다르다"
        )


def test_env_hard_lock_rule_nos_range():
    for e in E.ENV:
        assert all(1 <= n <= 8 for n in e.hard_lock_rule_nos), f"{e.name}: hard_lock_rule_nos={e.hard_lock_rule_nos}"


def test_env_hard_lock_affects_false_implies_no_rule_nos():
    for e in E.ENV:
        if not e.hard_lock_affects:
            assert e.hard_lock_rule_nos == (), (
                f"{e.name}: hard_lock_affects=False 인데 hard_lock_rule_nos={e.hard_lock_rule_nos} (비어야 한다)"
            )


def test_env_overrides_setting_points_to_registered_key():
    """`overrides_setting` 이 있으면 SETTINGS 에 실존하는 키를 가리켜야 한다(EI-6 반박 검토
    LOW — EI-7·EI-8 이 env→설정 대응을 기계로 읽을 수 있게)."""
    linked = [e for e in E.ENV if e.overrides_setting is not None]
    assert linked, "overrides_setting 이 하나도 없다 — AIDE_MAX_CONSEC_WORK 등에 채워야 한다"
    for e in linked:
        assert e.overrides_setting in S.SETTINGS_BY_KEY, (
            f"{e.name}: overrides_setting={e.overrides_setting!r} 가 SETTINGS 에 없다"
        )


# ── 하네스 파생 불변: canonical.py META 손 목록 = settings.META_KEYS() ──────


def test_canonical_meta_keys_derived_from_registry():
    assert S.META_KEYS() == canonical.SETTINGS_META_KEYS
    assert S.META_KEYS() == frozenset(
        {"last_generate_status", "created_at", "updated_at", "version", "config_name", "config_memo"}
    )


def test_canonical_digest_exclusion_derived_from_env_registry():
    """EI-6 반박 검토(MEDIUM) 반영 — `canonical._is_digest_excluded_env` 가 ENV 레지스트리의
    `digest` 칸에서 파생됐는지 확인한다. 실측으로 좁은 스캔 범위 안에서 손 목록
    (`engine_env.is_runtime_toggle`)과 갈렸던 5종은 레지스트리 기준으로 전부 '제외'(토글)여야
    한다 — 바뀌기 전에는 이 5종이 digest 에 섞여 들어갔다."""
    for name in (
        "AIDE_CAUSE_GATE", "AIDE_LEX_DIAG_S3", "CONFLICT_CORE_MEMBER_BUDGET",
        "ROSTER_GEN_BUDGET_SEC", "ROSTER_LADDER_MIN_LEFT_SEC",
    ):
        assert canonical._is_digest_excluded_env(name) is True, f"{name}: RUNTIME_TOGGLE 인데 digest 에서 안 빠짐"
    # 대조군 — 엔진 계약 env 는 digest 에 들어가야 한다(제외되면 안 된다).
    assert canonical._is_digest_excluded_env("AIDE_MAX_CONSEC_WORK") is False
    # 레지스트리에 없는 이름은 손 목록(engine_env.is_runtime_toggle) 으로 폴백한다.
    assert canonical._is_digest_excluded_env("NOT_A_REGISTERED_ENV_NAME") is False


# ── 정합: 키 중복·별칭 충돌·hard_lock 번호·editor 어휘·hidden_no 범위 ──────


def test_no_duplicate_keys():
    settings_keys = [s.key for s in S.SETTINGS]
    assert len(settings_keys) == len(set(settings_keys))
    nurse_keys = [a.key for a in N.NURSE_ATTRS]
    assert len(nurse_keys) == len(set(nurse_keys))


def test_settings_by_rule_key_has_unique_owners():
    """`rule_keys` 표기 하나를 SETTINGS 두 행 이상이 같이 쓰면 `SETTINGS_BY_RULE_KEY` 딕셔너리
    컴프리헨션이 선언 순서로 조용히 덮어쓴다(EI-6 반박 검토 LOW). 겹치면 한쪽에서 빼야 한다."""
    owner: dict[str, list[str]] = {}
    for s in S.SETTINGS:
        for rk in s.rule_keys:
            owner.setdefault(rk, []).append(s.key)
    dup = {rk: keys for rk, keys in owner.items() if len(keys) > 1}
    assert not dup, f"rule_keys 표기를 둘 이상의 SETTINGS 행이 같이 쓴다(유일 소유자가 아니다): {dup}"


def test_aliases_do_not_collide_with_other_keys():
    all_keys = set(S.SETTINGS_BY_KEY)
    collisions = [
        (s.key, alias) for s in S.SETTINGS for alias in s.aliases
        if alias in all_keys and alias != s.key
    ]
    assert not collisions, f"별칭이 다른 정본 키와 겹친다: {collisions}"

    owner: dict[str, set[str]] = {}
    for s in S.SETTINGS:
        for alias in s.aliases:
            owner.setdefault(alias, set()).add(s.key)
    shared = {alias: sorted(keys) for alias, keys in owner.items() if len(keys) > 1}
    assert not shared, f"같은 별칭을 둘 이상의 키가 쓴다: {shared}"


def test_derived_inputs_schema():
    """`derived_inputs`(EI-6 반박 검토 MEDIUM 신설 필드) 가 있으면 필수 칸 4개를 갖춰야 한다.

    선택 칸 `runtime`(EI-11 11b · D13 ③)은 있으면 `'forbidden'` 만 허용한다 — `policy.hard_lock.override_violations`
    가 그 별칭을 요청에서 값과 무관하게 막는 표지다. 판정은 하드락 행(`HardLock`)의 표지만 읽으므로, 표지가 조용히
    무시되지 않게 그 행은 `HardLock` 이어야 한다."""
    required = {"domain", "to_canonical", "precedence", "relax_if"}
    valid_precedence = {"alias_wins_at_boundary", "canonical_wins"}
    for s in S.SETTINGS:
        if not s.derived_inputs:
            continue
        for alias_name, spec in s.derived_inputs.items():
            missing = required - set(spec)
            assert not missing, f"{s.key}.derived_inputs[{alias_name!r}] 에 칸이 빠짐: {missing}"
            assert spec["precedence"] in valid_precedence, (
                f"{s.key}.derived_inputs[{alias_name!r}].precedence={spec['precedence']!r} 가 어휘 밖"
            )
            if "runtime" in spec:
                assert spec["runtime"] == "forbidden", (
                    f"{s.key}.derived_inputs[{alias_name!r}].runtime={spec['runtime']!r} 는 'forbidden' 만 허용"
                )
                assert isinstance(s.hard_lock, S.HardLock), f"{s.key}: runtime 표지는 하드락 행에만 둔다"
    marked = {alias for s in S.SETTINGS for alias, spec in (s.derived_inputs or {}).items() if "runtime" in spec}
    assert marked == {"max_consecutive_nights"}, marked


def test_hard_lock_rule_no_in_range_1_to_8():
    for s in S.SETTINGS:
        if isinstance(s.hard_lock, S.HardLock):
            assert 1 <= s.hard_lock.rule_no <= 8, f"{s.key}: hard_lock.rule_no={s.hard_lock.rule_no}"
    for a in N.NURSE_ATTRS:
        for rule_no in a.hard_lock_rule_nos:
            assert 1 <= rule_no <= 8, f"{a.key}: hard_lock_rule_nos 에 범위 밖 번호 {rule_no}"


def test_editor_vocabulary():
    for s in S.SETTINGS:
        assert s.editor in S.EDITORS, f"{s.key}: editor={s.editor!r} 가 어휘 밖"
    for a in N.NURSE_ATTRS:
        assert a.editor in S.EDITORS, f"{a.key}: editor={a.editor!r} 가 어휘 밖"


def _flatten_hidden_no(value: object) -> list[int]:
    if value is None:
        return []
    if isinstance(value, tuple):
        return list(value)
    return [value]  # type: ignore[list-item]


def test_hidden_no_within_hidden_settings_range():
    """`scope/hidden_settings.md` 는 번호 1~41 이다."""
    for s in S.SETTINGS:
        for no in _flatten_hidden_no(s.hidden_no):
            assert 1 <= no <= 41, f"{s.key}: hidden_no={no} 가 1~41 밖"
    for a in N.NURSE_ATTRS:
        for no in _flatten_hidden_no(a.hidden_no):
            assert 1 <= no <= 41, f"{a.key}: hidden_no={no} 가 1~41 밖"


def test_source_vocabulary():
    for s in S.SETTINGS:
        assert s.source in S.SOURCES, f"{s.key}: source={s.source!r} 가 어휘 밖"


#: EI-6 반박 검토(LOW) 반영 — `engine_field['path']` 는 자유 문자열이 아니라 §2.2/D14 의
#: 경로 표지 어휘(lex/primary/common/none)로 고정한다.
_NURSE_ENGINE_PATH_VOCAB: frozenset[str] = frozenset({"lex", "primary", "common", "none"})


def test_nurse_attrs_engine_field_path_vocabulary():
    for a in N.NURSE_ATTRS:
        path = (a.engine_field or {}).get("path")
        assert path in _NURSE_ENGINE_PATH_VOCAB, f"{a.key}: engine_field['path']={path!r} 가 어휘 밖"


# ── 정합: rules.py governing_keys 의 DB 키가 SETTINGS/NURSE_ATTRS/ENV 에 있음 ──

#: rules.py 의 governing_keys 중 SettingSpec·NurseAttrSpec·EnvSpec 어느 쪽도 아닌 것 —
#: EngineInput 런타임 부품(§2.4 cells·carryover, `RUNTIME_DATA_NOT_SETTINGS` 밖의 나머지)과
#: 근무코드 codebook 테이블 키(shifts.*)·고정/금지 원티드 셀 출처 컬럼이다. 설정도 간호사
#: 속성도 아니라 레지스트리 대상이 아니다(§2.4 EngineInput cells·codebook). 새 governing_key
#: 가 이 셋 중 무엇도 아니면 이 테스트가 그 이름을 짚는다 — settings.py/nurse_attrs.py 에
#: 등록하거나, 정말 EngineInput 런타임 부품이면 이 허용목록에 사유를 적어 추가하라.
_NON_REGISTRY_RUNTIME_ALLOWLIST: frozenset[str] = frozenset({
    # carryover(월경계 꼬리) — RUNTIME_DATA_NOT_SETTINGS 는 다른 철자(접미 없음)로 적어 겹치지 않는다.
    "runtime.prev_month_last_n_k_by_idx",
    "runtime.prev_month_n_offs_after_by_idx",
    "runtime.prev_month_n_tail_by_idx",
    "runtime.prev_month_off_tail_by_idx",
    # cells(정렬·탐색 보조)
    "runtime._pref_cells",
    # EngineInput 조립 파라미터(룩어헤드·LNS·커버리지 등식/부등식 플래그)
    "runtime.K_lookahead",
    "runtime.coverage_eq",
    "runtime.over_le",
    "runtime.lns_neighbourhood",
    "runtime.pair_matrix",
    "runtime.stage2_zero_locks",
    # cells(고정/금지 원티드 출처 컬럼) — NurseAttrSpec·SettingSpec 어느 쪽도 아니다.
    "banned_wanted_entries.banned_shift_ids",
    "banned_wanted_entries.is_applied",
    "fixed_wanted_entries.shift_id",
    # codebook(근무코드 분류축) — EngineInput.codebook 몫, 간호사 속성이 아니다.
    "shifts.default_shift",
    "shifts.shift_gb",
    "shifts.type",
})


def _all_rules_governing_keys() -> set[str]:
    keys: set[str] = set()
    for spec in RULES.values():
        keys |= set(spec.governing_keys)
    for spec in RULE_PREFIXES.values():
        keys |= set(spec.governing_keys)
    return keys


def test_rules_governing_keys_covered_by_registry():
    covered = set(S.SETTINGS_BY_RULE_KEY) | set(N.NURSE_ATTRS_BY_RULE_KEY)
    covered |= {f"env.{name}" for name in E.ENV_BY_NAME}
    covered |= {c.rule_key for c in S.CODE_CONSTS}
    covered |= {f"const.{o.name}" for o in S.OTHER_CONSTS}
    covered |= {f"runtime.{key}" for key, _part in S.RUNTIME_DATA_NOT_SETTINGS}
    covered |= _NON_REGISTRY_RUNTIME_ALLOWLIST

    missing = sorted(_all_rules_governing_keys() - covered)
    assert not missing, (
        "rules.py governing_keys 가 레지스트리 어디에도 없다 — settings.py/nurse_attrs.py 에 "
        f"등록하거나 '새 설정을 읽으면' 규칙(§2.7)대로 처리하라: {missing}"
    )
