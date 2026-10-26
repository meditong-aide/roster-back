"""엔진 AST get/getattr 키 전수 vs 레지스트리 차이 리포트 — 설계서 §2.6·§5.3 EI-6.

테스트가 아니다(강제 0). `tests/engine_contract/test_registry.py` 의 C2' 는 하드락·인접·
영향 키 23개만 강제하고, 나머지 약 100여 개 설정 키는 이 스크립트가 사람이 읽을 보고서로만
보여준다(§2.6 'C2'' 1차 설계와 차이 — 158키 전부 강제 → 20키 강제 + 리포트').

무엇을 본다
  - 대상 파일(엔진·생성 서비스·공용 제약 모듈)에서 `config_data.get(...)`·`cfg.get(...)`·
    `config_dict.get(...)`·`getattr(cfg/config_data/..., "키", 기본)`·`.setdefault(...)` 형태로
    설정류 이름의 키를 읽는 자리 전부(수신자 이름이 config 류일 때만 — req_map.get() 같은
    무관한 dict 접근은 뺀다).
  - **미등록 키**: SETTINGS·CODE_CONSTS·HARDCODED_CONSTS 어디에도 없는 키.
  - **기본값 불일치**: SETTINGS 에 등록됐는데 코드의 리터럴 기본값이 `current_default` 와
    다른 자리(참고용 — C2' 가 강제하는 23개는 이미 통과한 것만 여기 남는다).
  - **죽은 키**: `CODE_CONSTS`(생산자 없음, §3.3 MS-3) 전부 — 참고용 나열.

범위 밖 — ontology_graph·precheck 쪽 소비는 다른 표(§2.2 '레지스트리 밖으로 뺀 것')가
맡는다. 이 스크립트는 엔진·생성 서비스 쪽 소비만 본다. 이름 해소는 과대/과소 근사가 섞인
휴리스틱이다(진짜 타입 추론이 아니다) — 출력은 사람이 읽고 판단하는 참고 자료다.

usage: (roster-back cwd) .venv/bin/python tools/engine_contract_report.py [--out FILE]
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
sys.path.insert(0, str(APP))

from services.engine_contract import nurse_attrs as NA  # noqa: E402
from services.engine_contract import settings as S  # noqa: E402

#: 설정류로 간주하는 수신자 이름(휴리스틱 — 과소 근사 쪽으로 좁혀서 잡음을 줄인다).
_CONFIG_RECEIVER_NAMES = frozenset({
    "cfg", "config", "config_data", "config_dict", "config_override", "latest_config",
    "config_dict_patch", "relaxed_cfg", "_relaxed_cfg",
})

#: 스캔 대상 — 엔진·생성 서비스·공용 제약 모듈(§2.3 치환 대상과 거의 같다).
_SCAN_FILES = (
    "services/cp_sat_basic.py",
    "services/roster_create_service.py",
    "services/cp_sat/fallback_lex.py",
    "services/cp_sat/fallback_objectives.py",
    "services/cp_sat/objective_terms.py",
    "services/cp_sat/lookahead_constraints.py",
    "services/constraints/grade_constraints.py",
    "services/constraints/monthly_limit_constraints.py",
    "services/constraints/team_constraints.py",
    "services/constraints/team_grade_handoff_constraints.py",
)


@dataclass
class Hit:
    key: str
    default_repr: str
    site: str


def _receiver_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _default_repr(node: ast.expr) -> str:
    try:
        return repr(ast.literal_eval(node))
    except ValueError:
        return f"<non-literal:{ast.unparse(node)}>"


def _scan_file(relpath: str) -> list[Hit]:
    path = APP / relpath
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[Hit] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in ("get", "setdefault") and node.args:
            recv = _receiver_name(func.value)
            if recv not in _CONFIG_RECEIVER_NAMES:
                continue
            key_node = node.args[0]
            default_node = node.args[1] if len(node.args) >= 2 else ast.Constant(value=None)
        elif isinstance(func, ast.Name) and func.id == "getattr" and len(node.args) >= 2:
            recv = _receiver_name(node.args[0])
            if recv not in _CONFIG_RECEIVER_NAMES:
                continue
            key_node = node.args[1]
            default_node = node.args[2] if len(node.args) >= 3 else ast.Constant(value=None)
        else:
            continue
        if not (isinstance(key_node, ast.Constant) and isinstance(key_node.value, str)):
            continue
        hits.append(Hit(key=key_node.value, default_repr=_default_repr(default_node),
                         site=f"{relpath}:{node.lineno}"))
    return hits


def scan_all() -> list[Hit]:
    hits: list[Hit] = []
    for relpath in _SCAN_FILES:
        hits.extend(_scan_file(relpath))
    return hits


def _is_known_non_setting(key: str) -> bool:
    """SettingSpec 가 아니어도 '미등록'으로 치지 않는 키(CODE_CONST·간호사 속성·EngineInput
    런타임 부품·별칭)인가."""
    code_const_keys = {c.key for c in S.CODE_CONSTS}
    hardcoded_names = {c.name for c in S.HARDCODED_CONSTS}
    runtime_data_keys = {k for k, _part in S.RUNTIME_DATA_NOT_SETTINGS}
    alias_owner = {alias for s in S.SETTINGS for alias in s.aliases}
    return (
        key in code_const_keys
        or key in hardcoded_names
        or key in NA.NURSE_ATTRS_BY_KEY
        or key in runtime_data_keys
        or key in alias_owner
    )


def _classify(by_key: dict[str, list[Hit]]) -> tuple[list[str], list[str]]:
    """(미등록 키 설명 목록, 기본값 불일치 설명 목록)."""
    unregistered: list[str] = []
    mismatches: list[str] = []
    for key in sorted(by_key):
        spec = S.SETTINGS_BY_KEY.get(key)
        sites = ", ".join(h.site for h in by_key[key])
        if spec is None:
            if not _is_known_non_setting(key):
                defaults = sorted({h.default_repr for h in by_key[key]})
                unregistered.append(f"  {key}  defaults={defaults}  @ {sites}")
            continue
        found_defaults = {h.default_repr for h in by_key[key]}
        stripped = {d.strip("'\"") for d in found_defaults}
        if repr(spec.current_default) not in found_defaults and str(spec.current_default) not in stripped:
            mismatches.append(
                f"  {key}  registry.current_default={spec.current_default!r}  "
                f"code_defaults={sorted(found_defaults)}  @ {sites}"
            )
    return unregistered, mismatches


def build_report() -> str:
    hits = scan_all()
    by_key: dict[str, list[Hit]] = {}
    for h in hits:
        by_key.setdefault(h.key, []).append(h)
    unregistered, mismatches = _classify(by_key)

    lines = [
        "# engine_contract_report — AST get/getattr 전수 vs 레지스트리",
        f"# 스캔 대상 {len(_SCAN_FILES)}개 파일, 설정류 수신자 {len(_CONFIG_RECEIVER_NAMES)}종, "
        f"찾은 고유 키 {len(by_key)}개 (등록 {len(by_key) - len(unregistered)}  미등록 {len(unregistered)})",
        "",
        f"## 미등록 키 ({len(unregistered)}) — SETTINGS·CODE_CONSTS·HARDCODED_CONSTS 어디에도 없음",
    ]
    lines.extend(unregistered or ["  (없음)"])
    lines.append("")
    lines.append(f"## 기본값 불일치 ({len(mismatches)}) — 참고용(C2' 강제 23개는 이미 통과)")
    lines.extend(mismatches or ["  (없음)"])
    lines.append("")
    lines.append(f"## 죽은 키 — CODE_CONSTS(생산자 없음, §3.3) {len(S.CODE_CONSTS)}개")
    for c in S.CODE_CONSTS:
        lines.append(f"  {c.key}  default={list(c.default)}  rule_key={c.rule_key}  note={c.note}")
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=None, help="출력 파일(기본: 표준출력)")
    args = parser.parse_args(argv)
    report = build_report()
    if args.out:
        args.out.write_text(report, encoding="utf-8")
        print(f"리포트를 {args.out} 에 썼다")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
