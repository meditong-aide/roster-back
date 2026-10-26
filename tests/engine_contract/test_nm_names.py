"""NM — 설정 칸 이름(바깥 이름 ↔ 파이썬 이름) 계약 시험.

- 표 자체(14쌍·1:1·새 이름 중복 없음)와 변환 함수의 뜻(값 그대로·None 보존·순서 보존·충돌 오류).
- 안전망: strict 에서 옛 이름은 예외, 운영 모드는 새 이름으로 이어 동작 유지.
- 정적 검사: app 코드 안쪽에 옛 이름이 남지 않았는지(바깥 계약 자리는 `# nm:external` 표지나 허용 범위).
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path

import pytest

from services.engine_contract import names
from services.engine_contract.names import (
    EXTERNAL_TO_INTERNAL,
    INTERNAL_TO_EXTERNAL,
    OldConfigNameError,
    OldNameDescriptor,
    check_engine_config_keys,
    external_key,
    internal_key,
    to_external,
    to_external_deep,
    to_internal,
)

APP = Path(__file__).resolve().parents[2] / "app"
TOOLS = Path(__file__).resolve().parents[2] / "tools"


# ── 표 ────────────────────────────────────────────────────────────────

def test_table_is_fourteen_one_to_one_pairs():
    assert len(EXTERNAL_TO_INTERNAL) == 14
    assert len(set(EXTERNAL_TO_INTERNAL.values())) == 14
    assert dict(INTERNAL_TO_EXTERNAL) == {v: k for k, v in EXTERNAL_TO_INTERNAL.items()}
    assert not set(EXTERNAL_TO_INTERNAL) & set(INTERNAL_TO_EXTERNAL), "옛 이름과 새 이름이 겹치면 변환이 되돌아간다"


def test_key_helpers_pass_through_other_names():
    assert internal_key("off_first") == "staffing_first"
    assert internal_key("max_conseq_work") == "max_conseq_work"
    assert external_key("ban_single_night") == "not_one_night"
    assert external_key("two_offs_after_two_nig") == "two_offs_after_two_nig"


# ── 얕은 변환 ─────────────────────────────────────────────────────────

def test_to_internal_keeps_values_order_and_none():
    src = {"max_conseq_work": 5, "two_offs_per_week": None, "off_first": True, "three_seq_nig": False}
    out = to_internal(src)
    assert list(out) == ["max_conseq_work", "prefer_two_offs_per_7days", "staffing_first",
                         "allow_three_consecutive_nights"]
    assert out["prefer_two_offs_per_7days"] is None, "None 은 키 부재와 뜻이 달라 버리면 안 된다"
    assert out["staffing_first"] is True and out["allow_three_consecutive_nights"] is False
    assert src == {"max_conseq_work": 5, "two_offs_per_week": None, "off_first": True, "three_seq_nig": False}


def test_to_internal_returns_same_object_when_nothing_to_rename():
    src = {"max_conseq_work": 5}
    assert to_internal(src) is src
    assert to_internal(None) is None
    assert to_internal([("off_first", 1)]) == [("off_first", 1)]


def test_to_internal_merges_equal_duplicates_and_rejects_conflicts():
    assert to_internal({"not_one_night": True, "ban_single_night": True}) == {"ban_single_night": True}
    with pytest.raises(ValueError):
        to_internal({"not_one_night": True, "ban_single_night": False})


def test_to_internal_conflict_message_names_both_names():
    with pytest.raises(ValueError) as exc_info:
        to_internal({"off_first": True, "staffing_first": False})
    assert "off_first" in str(exc_info.value) and "staffing_first" in str(exc_info.value)


@pytest.mark.parametrize("order", ["internal_first", "external_first"])
def test_outbound_conflict_keeps_internal_value_and_warns(order, caplog):
    """나가는 쪽은 저장·생성이 끝난 뒤라 실패시키지 않는다 — 파이썬 이름 쪽(안쪽의 현재 값)을 남긴다."""
    items = [("ban_single_night", True), ("not_one_night", False)]
    src = dict(items if order == "internal_first" else items[::-1])
    with caplog.at_level(logging.WARNING, logger=names.__name__):
        assert to_external(src) == {"not_one_night": True}
        assert to_external_deep({"cfg": src}) == {"cfg": {"not_one_night": True}}
    assert "not_one_night" in caplog.text


def test_to_external_is_inverse_of_to_internal():
    src = {"off_first": True, "nod_noe": None, "max_nig_per_month": 15, "fixed_wanted_use_yn": False}
    assert to_external(to_internal(src)) == src


# ── 깊은 바깥 변환 ────────────────────────────────────────────────────

def test_to_external_deep_renames_keys_and_exact_string_values_only():
    payload = {
        "cards": [{"config_key": "ban_single_night", "apply": {"ban_single_night": True, "max_conseq_work": 5}}],
        "message": "ban_single_night 문장 안 부분 문자열은 그대로",
        "keys": ("staffing_first", "max_conseq_work"),
        "violation_type": "three_seq_nig",
        "count": 3,
    }
    out = to_external_deep(payload)
    assert out["cards"][0] == {"config_key": "not_one_night", "apply": {"not_one_night": True, "max_conseq_work": 5}}
    assert out["message"] == payload["message"]
    assert out["keys"] == ["off_first", "max_conseq_work"]
    assert out["violation_type"] == "three_seq_nig", "옛 이름 값(위반 유형 등 바깥 식별자)은 건드리지 않는다"
    assert out["count"] == 3


# ── 엔진 입구 검사·호환 심 ───────────────────────────────────────────

def test_engine_entry_check_strict_raises(monkeypatch):
    monkeypatch.setenv(names.STRICT_ENV, "1")
    assert check_engine_config_keys({"staffing_first": True}, where="t") == {"staffing_first": True}
    with pytest.raises(OldConfigNameError):
        check_engine_config_keys({"off_first": True}, where="t")


def test_engine_entry_check_lenient_moves_and_warns(monkeypatch, caplog):
    monkeypatch.delenv(names.STRICT_ENV, raising=False)
    monkeypatch.setattr(names, "_warned", set())
    with caplog.at_level(logging.WARNING, logger=names.__name__):
        out = check_engine_config_keys({"not_one_night": True, "max_conseq_work": 5}, where="t")
    assert out == {"ban_single_night": True, "max_conseq_work": 5}
    assert "not_one_night" in caplog.text


class _Holder:
    ban_single_night = False
    not_one_night = OldNameDescriptor("not_one_night")


def test_descriptor_strict_raises_even_through_getattr_default(monkeypatch):
    monkeypatch.setenv(names.STRICT_ENV, "1")
    h = _Holder()
    with pytest.raises(OldConfigNameError):
        getattr(h, "not_one_night", "기본값")
    with pytest.raises(OldConfigNameError):
        h.not_one_night = True


def test_descriptor_lenient_bridges_to_new_name(monkeypatch):
    monkeypatch.delenv(names.STRICT_ENV, raising=False)
    h = _Holder()
    h.not_one_night = True
    assert h.ban_single_night is True and h.not_one_night is True


# ── 정적 검사 — app 안쪽에 옛 이름이 남지 않았다 ─────────────────────

#: 파일 전체가 바깥 계약이거나 표 정의인 곳.
_ALLOW_FILES = {
    "services/engine_contract/names.py",
}
#: (파일, 클래스·함수 이름) — 그 범위 안의 옛 이름은 바깥 계약이다(프론트가 보내는 요청 모델 필드).
_ALLOW_SCOPES: set[tuple[str, str]] = {
    ("schemas/roster_schema.py", "RosterConfigBase"),
    ("schemas/roster_schema.py", "RosterRequest"),
}
_MARK = "nm:external"


def _is_column_physical_name(node, parents) -> bool:
    """`Column(TYPE, name="<옛>", key="<새>")` 의 name 값 — 물리 컬럼 이름이라 바깥 이름이 맞다."""
    kw = parents.get(node)
    call = parents.get(kw)
    return (isinstance(kw, ast.keyword) and kw.arg == "name" and isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name) and call.func.id == "Column")


def _old_name_uses(path: Path, root: Path = APP) -> list[tuple[int, str, str]]:
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    lines = src.splitlines()
    parents = {c: p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}
    rel = str(path.relative_to(root))

    def scopes(node):
        cur = node
        while cur in parents:
            cur = parents[cur]
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                yield cur.name

    def allowed(node) -> bool:
        first, last = node.lineno, getattr(node, "end_lineno", None) or node.lineno
        if any(_MARK in lines[i - 1] for i in range(first, last + 1)):
            return True
        return any((rel, s) in _ALLOW_SCOPES for s in scopes(node))

    found: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        name = kind = None
        if isinstance(node, ast.Attribute) and node.attr in EXTERNAL_TO_INTERNAL:
            name, kind = node.attr, "속성"
        elif (isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in EXTERNAL_TO_INTERNAL
              and not _is_column_physical_name(node, parents)):
            name, kind = node.value, "문자열"
        elif isinstance(node, ast.keyword) and node.arg in EXTERNAL_TO_INTERNAL:
            name, kind = node.arg, "키워드 인자"
        elif isinstance(node, ast.Name) and node.id in EXTERNAL_TO_INTERNAL:
            name, kind = node.id, "이름"
        elif isinstance(node, ast.arg) and node.arg in EXTERNAL_TO_INTERNAL:
            name, kind = node.arg, "매개변수"
        if name and not allowed(node if not isinstance(node, ast.keyword) else node.value):
            found.append((node.lineno if hasattr(node, "lineno") else node.value.lineno, kind, name))
    return found


def test_no_old_config_names_inside_app_code():
    leftovers: list[str] = []
    scanned = 0
    for path in sorted(APP.rglob("*.py")):
        rel_path = path.relative_to(APP)
        rel = str(rel_path)
        # 걸러 내기는 상대 경로로 본다 — 사본이 .omc 아래에 있어도 전부 건너뛰는 빈 통과가 되지 않게.
        if "__pycache__" in rel_path.parts or rel in _ALLOW_FILES or "dev_queries" in rel_path.parts:
            continue
        scanned += 1
        for line, kind, name in _old_name_uses(path):
            leftovers.append(f"app/{rel}:{line} {kind} {name}")
    assert scanned > 100, f"검사한 app 파일이 {scanned}개뿐이다 — 걸러 내기 조건이 잘못됐다"
    assert not leftovers, (
        "파이썬 안쪽에 옛 설정 이름이 남았다 — 새 이름으로 바꾸거나, 바깥 계약(API·DB·저장 JSON·위반 유형)이면 "
        f"줄 끝에 '# {_MARK}' 를 단다:\n" + "\n".join(leftovers[:80])
    )


def test_no_old_config_names_inside_tools():
    """도구(하네스·사례 재현)도 설정 dict 를 `.get(옛 이름)` 으로 읽으면 strict 예외 없이 기본값으로 조용히 틀린다 — 같은 검사를 건다."""
    leftovers: list[str] = []
    scanned = 0
    for path in sorted(TOOLS.rglob("*.py")):
        rel_path = path.relative_to(TOOLS)
        # 걸러 내기는 상대 경로로 본다 — 절대 경로로 보면 .omc 아래 사본에서 53개 전부를 건너뛰어 빈 통과가 됐다(NM 최종 검토).
        if "__pycache__" in rel_path.parts or ".omc" in rel_path.parts:
            continue
        scanned += 1
        for line, kind, name in _old_name_uses(path, TOOLS):
            leftovers.append(f"tools/{rel_path}:{line} {kind} {name}")
    assert scanned > 20, f"검사한 tools 파일이 {scanned}개뿐이다 — 걸러 내기 조건이 잘못됐다"
    assert not leftovers, (
        "도구 안쪽에 옛 설정 이름이 남았다 — 새 이름으로 바꾸거나, HTTP 본문·저장 기록·규칙 id 처럼 바깥 이름이 맞으면 "
        f"줄 끝에 '# {_MARK}' 를 단다:\n" + "\n".join(leftovers[:80])
    )


def test_to_internal_config_slots_touches_only_apply_and_config_key():
    from services.engine_contract.names import to_internal_config_slots

    stored = {"options": [{"apply": {"not_one_night": True, "max_conseq_work": 5}, "config_key": "off_first",
                           "rule_id": "not_one_night", "family": {"not_one_night": 3}}]}
    out = to_internal_config_slots(stored)
    option = out["options"][0]
    assert option["apply"] == {"ban_single_night": True, "max_conseq_work": 5}
    assert option["config_key"] == "staffing_first"
    assert option["rule_id"] == "not_one_night" and option["family"] == {"not_one_night": 3}, "다른 자리는 바깥 식별자"
