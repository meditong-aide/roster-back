"""EI-9d 단위 테스트 — raw 컬럼 헬퍼(설계서 EI9_SPEC.md §2.3).

`read_raw_setting` 은 오프라인 전용이라(호출처 0) 골든 시나리오 하네스를 쓰지 않는다 —
`shim_db`(test_mssql_shim.py 재사용) 위에서 직접 컬럼·행을 심어 운영 함수와 교차 확인한다.
공휴일 OFF·n2n_min_gap 은 위임 대상 운영 함수(`_holiday_off_enabled`·`_n2n_min_gap_setting`)와
**같은 db 상태에서 같은 값**인지를 본다. 주휴 activate 는 위임할 함수가 없어(rcs 인라인) 오라클
(운영 inline 원문 사본, 고치지 말 것 — O-7 불변의 기준)과 차분한다.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest
from sqlalchemy import text

from .test_mssql_shim import _add_config_rows, shim_db  # noqa: F401 — 픽스처 재사용
from .ward import GROUP_ID, OFFICE_ID

APP_ROOT = Path(__file__).resolve().parents[2] / "app"


def _raw():
    """`services.engine_input.raw` 모듈(지연 import — 가드 규약, conftest.py 머릿말 참조)."""
    from services.engine_input import raw

    return raw


def _rcs():
    """`services.roster_create_service` 모듈(지연 import — 가드 규약)."""
    from services import roster_create_service

    return roster_create_service


# ── 모르는 키 ────────────────────────────────────────────────────────────


def test_unknown_key_raises_key_error(shim_db):  # noqa: F811 — 픽스처 재사용(이름 일치 필요)
    raw = _raw()
    with pytest.raises(KeyError):
        raw.read_raw_setting(shim_db, "no_such_key", office_id=OFFICE_ID, group_id=GROUP_ID)


# ── fixed_holiday_off_yn — 운영 함수와 교차 ─────────────────────────────


def test_holiday_off_column_missing_is_false(shim_db):  # noqa: F811 — 픽스처 재사용(이름 일치 필요)
    rcs = _rcs()
    raw = _raw()
    _add_config_rows(shim_db, [{"config_id": 1, "group_id": GROUP_ID}])
    result = raw.read_raw_setting(shim_db, "fixed_holiday_off_yn",
                                  office_id=OFFICE_ID, group_id=GROUP_ID)
    assert (result.key, result.reader) == ("fixed_holiday_off_yn", "_holiday_off_enabled")
    assert result.value is False
    assert result.value == rcs._holiday_off_enabled(shim_db, GROUP_ID)


@pytest.mark.parametrize("stored, expected", [(1, True), (0, False), (None, False)])
def test_holiday_off_delegates_to_production_and_reads_the_column_value(shim_db, stored, expected):  # noqa: F811, E501
    """이름 정정(F, 리뷰 지적 LOW) — `read_raw_setting` 이 `_holiday_off_enabled` 에 **위임**
    하므로(raw.py) 둘째 단언(`==`)은 동어반복이다. 독립적인 쪽은 `expected`(리터럴, 컬럼 값
    1/0/NULL 에 대한 손으로 적은 기대값)와 비교하는 첫 단언이다."""
    rcs = _rcs()
    raw = _raw()
    _add_config_rows(shim_db, [{"config_id": 1, "group_id": GROUP_ID}])
    shim_db.execute(text("ALTER TABLE roster_config ADD COLUMN fixed_holiday_off_yn INTEGER"))
    if stored is not None:
        shim_db.execute(text("UPDATE roster_config SET fixed_holiday_off_yn = :v WHERE config_id = 1"),
                        {"v": stored})
    result = raw.read_raw_setting(shim_db, "fixed_holiday_off_yn",
                                  office_id=OFFICE_ID, group_id=GROUP_ID)
    assert result.value is expected
    assert result.value == rcs._holiday_off_enabled(shim_db, GROUP_ID)  # 위임 배선 확인(동어반복)


# ── n2n_min_gap — 운영 함수와 교차 ───────────────────────────────────────


def test_n2n_column_missing_is_zero(shim_db):  # noqa: F811 — 픽스처 재사용(이름 일치 필요)
    rcs = _rcs()
    raw = _raw()
    _add_config_rows(shim_db, [{"config_id": 1, "group_id": GROUP_ID}])
    result = raw.read_raw_setting(shim_db, "n2n_min_gap", office_id=OFFICE_ID, group_id=GROUP_ID)
    assert result.reader == "_n2n_min_gap_setting"
    assert result.value == 0
    assert result.value == rcs._n2n_min_gap_setting(shim_db, GROUP_ID)


@pytest.mark.parametrize("stored, expected", [(4, 4), (5, 5), (None, 0)])
def test_n2n_delegates_to_production_and_reads_the_column_value(shim_db, stored, expected):  # noqa: F811, E501
    """이름 정정(F, 리뷰 지적 LOW) — `read_raw_setting` 이 `_n2n_min_gap_setting` 에 **위임**
    하므로 둘째 단언(`==`)은 동어반복이다. 독립적인 쪽은 `expected`(리터럴)와 비교하는 첫 단언이다."""
    rcs = _rcs()
    raw = _raw()
    _add_config_rows(shim_db, [{"config_id": 1, "group_id": GROUP_ID}])
    shim_db.execute(text("ALTER TABLE roster_config ADD COLUMN n2n_min_gap INTEGER"))
    if stored is not None:
        shim_db.execute(text("UPDATE roster_config SET n2n_min_gap = :v WHERE config_id = 1"),
                        {"v": stored})
    result = raw.read_raw_setting(shim_db, "n2n_min_gap", office_id=OFFICE_ID, group_id=GROUP_ID)
    assert result.value == expected
    assert result.value == rcs._n2n_min_gap_setting(shim_db, GROUP_ID)  # 위임 배선 확인(동어반복)


def test_n2n_raw_column_not_overlaid_by_config_override():
    """S8 류 — 컬럼 값(4)과 프로덕션이 `config_override` 로 덮는 실효값(0)은 서로 다른 칸이다
    (설계서 §2.4 'raw_column 과 settings_raw 실효값을 대조하지 않는다'). `read_raw_setting` 은
    config_override 를 전혀 받지 않으므로 이 사실은 서명 자체로 보장된다 — 실효값을 만드는
    쪽(config_dict 병합)과 섞이지 않는다는 뜻을 명시적으로 고정해 둔다."""
    import inspect

    raw = _raw()
    params = inspect.signature(raw.read_raw_setting).parameters
    assert "config_override" not in params
    assert "config_dict" not in params


# ── weekly_off_settings_activate — 오라클(운영 inline 사본) 차분 ────────


def _oracle_weekly_off_activate(db, office_id: str, group_id: str) -> bool:
    """3ede458 원문 사본 — 고치지 말 것(O-7 교차 테스트의 기준). rcs :6008-:6026 그대로."""
    from services.roster_create_service import _table_exists

    try:
        if _table_exists(db, "weekly_off_settings"):
            setting = db.execute(
                text(
                    "SELECT TOP 1 activate "
                    "FROM weekly_off_settings "
                    "WHERE office_id = :office_id AND group_id = :group_id"
                ),
                {"office_id": office_id, "group_id": group_id},
            ).fetchone()
            if setting:
                return bool(setting.activate)
            return False
        return False
    except Exception:
        return False


def test_weekly_activate_table_missing_is_false(shim_db):  # noqa: F811 — 픽스처 재사용(이름 일치 필요)
    raw = _raw()
    shim_db.execute(text("DROP TABLE weekly_off_settings"))
    result = raw.read_raw_setting(shim_db, "weekly_off_settings_activate",
                                  office_id=OFFICE_ID, group_id=GROUP_ID)
    assert result.reader == "_weekly_off_settings_activate"
    assert result.value is False
    assert result.value == _oracle_weekly_off_activate(shim_db, OFFICE_ID, GROUP_ID)


def test_weekly_activate_row_missing_is_false(shim_db):  # noqa: F811 — 픽스처 재사용(이름 일치 필요)
    raw = _raw()
    result = raw.read_raw_setting(shim_db, "weekly_off_settings_activate",
                                  office_id=OFFICE_ID, group_id=GROUP_ID)
    assert result.value is False
    assert result.value == _oracle_weekly_off_activate(shim_db, OFFICE_ID, GROUP_ID)


@pytest.mark.parametrize("stored, expected", [(1, True), (0, False)])
def test_weekly_activate_row_present_matches_oracle(shim_db, stored, expected):  # noqa: F811 — 픽스처 재사용(이름 일치 필요)
    from db.models import WeeklyOffSetting

    raw = _raw()
    shim_db.add(WeeklyOffSetting(office_id=OFFICE_ID, group_id=GROUP_ID, activate=stored))
    shim_db.flush()
    result = raw.read_raw_setting(shim_db, "weekly_off_settings_activate",
                                  office_id=OFFICE_ID, group_id=GROUP_ID)
    assert result.value is expected
    assert result.value == _oracle_weekly_off_activate(shim_db, OFFICE_ID, GROUP_ID)


def test_weekly_activate_other_group_row_is_ignored(shim_db):  # noqa: F811 — 픽스처 재사용(이름 일치 필요)
    """WHERE 가 office_id·group_id 둘 다 걸어야 한다 — 다른 그룹 행이 섞이면 안 된다."""
    from db.models import Group, WeeklyOffSetting

    raw = _raw()
    shim_db.add(Group(group_id="G9", office_id=OFFICE_ID, group_name="합성병동G9", hn_id=[]))
    shim_db.add(WeeklyOffSetting(office_id=OFFICE_ID, group_id="G9", activate=1))
    shim_db.flush()
    result = raw.read_raw_setting(shim_db, "weekly_off_settings_activate",
                                  office_id=OFFICE_ID, group_id=GROUP_ID)
    assert result.value is False
    assert result.value == _oracle_weekly_off_activate(shim_db, OFFICE_ID, GROUP_ID)


def test_weekly_activate_query_exception_is_false(shim_db):  # noqa: F811 — 픽스처 재사용(이름 일치 필요)
    """`activate` 컬럼이 없는 동명 표로 바꿔 SELECT 가 실패하는 경로(바깥 except)."""
    raw = _raw()
    shim_db.execute(text("DROP TABLE weekly_off_settings"))
    shim_db.execute(text("CREATE TABLE weekly_off_settings (office_id TEXT, group_id TEXT)"))
    result = raw.read_raw_setting(shim_db, "weekly_off_settings_activate",
                                  office_id=OFFICE_ID, group_id=GROUP_ID)
    assert result.value is False
    assert result.value == _oracle_weekly_off_activate(shim_db, OFFICE_ID, GROUP_ID)


# ── import 모양 ──────────────────────────────────────────────────────────


def test_raw_has_no_top_level_rcs_import():
    """fixed_cut.py 와 같은 가드 — `roster_create_service` 최상위 import 금지(순환 방지)."""
    import services.engine_input.raw as raw_module

    tree = ast.parse(Path(raw_module.__file__).read_text())
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module != "services.roster_create_service", ast.dump(node)
        elif isinstance(node, ast.Import):
            assert all("roster_create_service" not in alias.name for alias in node.names)


def _iter_app_py_files():
    for path in APP_ROOT.rglob("*.py"):
        # engine_input 패키지 자신은 제외 — 9b builder.py 가 나중에 여기서 부른다.
        if "engine_input" in path.relative_to(APP_ROOT).parts:
            continue
        yield path


def _imports_raw_module(tree: ast.Module) -> str | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "services.engine_input.raw":
                    return alias.name
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "services.engine_input.raw":
                return module
            if module in ("services.engine_input", "..engine_input", ".engine_input"):
                for alias in node.names:
                    if alias.name == "raw":
                        return f"{module}.raw"
    return None


def test_no_app_code_imports_raw_yet():
    """9d 는 '호출처 0' 상태다 — engine_input 패키지 밖 그 누구도 아직 `raw` 를 안 쓴다."""
    offenders = []
    for path in _iter_app_py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        hit = _imports_raw_module(tree)
        if hit:
            offenders.append(f"{path.relative_to(APP_ROOT)}: {hit}")
    assert offenders == [], offenders
