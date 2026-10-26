"""EI-11 11e — 탐색 결과 세 칸을 payload 로(`.omc/aide_docs/v2/engine_input/ei11/EI11_SPEC_v2.md` §2.6·§4.1).

`undiagnosed_probe.probe_status_fields(res)` 가 세 칸(`probe_found`·`probe_baseline_feasible`·`probe_timed_out`)을 짓는다 —
탐색을 돌렸으면 bool(결과에 키가 없으면 False), 돌리지 않았으면(`res` 가 None 이거나 원인 진단이 건너뛰게 한 `skipped_by_cause`)
세 칸 모두 None 이다. 생성 서비스는 탐색 단계가 있는 두 경로(사후 실패 · A1)에서 단계에 들어서자마자 None 으로 싣고, 탐색을
돌리면 결과로 덮는다. 칸이 아예 없으면 그 경로에 탐색 단계가 없다는 뜻이다(precheck 차단·A1 요청 안 함).
  ① 값 — 실제 `probe_relaxations` 결과로 본다: 기준 재현 실패 · 시간 상한(느린 가짜 재계산) · 정상(found 반영) · 원인 스킵·None → null
  ② 원문 검사 — 생성 서비스의 네 자리(사후 2 · A1 2) · 옛 `["probe_found"] =` 대입 0 · 자리 순서
  ③ 원문 블록 실행(지시서 목록 밖) — 서비스 원문의 탐색 try 블록(사후 · A1)을 그대로 떼어 가짜 값으로 돌린다. 돌리지 않은
     경우의 null 과 A1 조건 나누기를 실행으로 본다. 탐색을 돌린 사후 경로는 사다리 골든(S1-L·S1-LT·S1-LB)이 덮는다.
탐색 기계(`probe_relaxations`)는 무수정이다(P-3). DB·엔진·LLM 없이 돈다.
"""

from __future__ import annotations

import ast
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from services.cp_sat import undiagnosed_probe as probe
from services.cp_sat.undiagnosed_probe import probe_relaxations, probe_status_fields

RCS = Path(__file__).resolve().parents[1] / "app" / "services" / "roster_create_service.py"
_GEN = "_generate_roster_service_impl"
FIELDS = ("probe_found", "probe_baseline_feasible", "probe_timed_out")
NULLS = dict.fromkeys(FIELDS)
#: 생성 서비스가 원인 진단으로 탐색을 건너뛸 때 만드는 결과 — 원문 리터럴과 같은지 ② 가 본다.
SKIPPED_BY_CAUSE = {"found": False, "resolutions": [], "all_probed": [], "probed_count": 0, "skipped_by_cause": True}
#: 서비스가 세 칸을 싣는 네 문장(원문 순서) — 사후: 탐색 try 첫 문장 · 탐색 결과 / A1: 조건 첫 단계 · 탐색 결과.
SITES = (
    "unrecoverable.setdefault('infeasibility', {}).update(probe_status_fields(None))",
    "unrecoverable['infeasibility'].update(probe_status_fields(_probe_res))",
    "_inf_a1.update(probe_status_fields(None))",
    "_inf_a1.update(probe_status_fields(_a1_res))",
)


def _fields(found: bool, baseline: bool, timed_out: bool) -> dict:
    return dict(zip(FIELDS, (found, baseline, timed_out), strict=True))


def _quiet(_msg: str) -> None:
    """탐색 로그를 버린다."""


def _toggle(item_id: str, key: str, value: object) -> dict:
    """하드락 밖 bool 토글 한 항목(탐색 없음 — 고정 delta)."""
    return {"id": item_id, "family": item_id, "label_ko": item_id, "apply": lambda _cfg: {key: value}}


#: 실제 카탈로그의 하드락 밖 토글 두 키로 지은 가짜 카탈로그 — 탐색 기계만 본다.
CATALOG = (_toggle("soften_team_min", "team_min_soft_fallback", True),
           _toggle("disable_preceptee_sync", "preceptee_on", False))
BASE = {"team_min_soft_fallback": False, "preceptee_on": True}


def _team(cfg: dict) -> bool:
    return cfg.get("team_min_soft_fallback") is True


def _both(cfg: dict) -> bool:
    return _team(cfg) and cfg.get("preceptee_on") is False


def _never(_cfg: dict) -> bool:
    return False


def _always(_cfg: dict) -> bool:
    return True


def _probe(feasible, *, budget_sec: float = 0, calls: list | None = None) -> dict:
    """가짜 재계산(`feasible(cfg)`)으로 탐색을 돌린다 — 환경변수를 건드리지 않게 verify 를 끄고, 시간 상한은 기본 끔(0)."""
    def resolve(cfg: dict) -> tuple[bool, dict]:
        if calls is not None:
            calls.append(dict(cfg))
        return feasible(cfg), {}

    return probe_relaxations(dict(BASE), resolve, catalog=list(CATALOG), verify=False, budget_sec=budget_sec,
                             logger=_quiet)


# ── ① 값 ─────────────────────────────────────────────────────────────────────


def test_baseline_feasible_is_reported():
    """기준 재현 실패 — 기준 설정 그대로의 재계산이 풀리면 해결책 없이 끝나고 `probe_baseline_feasible` 이 True 다."""
    calls: list = []
    res = _probe(_always, calls=calls)
    assert calls == [BASE], "기준 재계산 1회 뒤 완화를 재계산하지 않는다"
    assert probe_status_fields(res) == _fields(False, True, False)


@pytest.mark.parametrize(("feasible", "kind", "want_found"), [
    (_team, "resolutions", True),
    (_both, "combo", True),
    (_never, None, False),
], ids=["single", "combo", "nothing"])
def test_normal_probe_reports_found_and_two_false_flags(feasible, kind, want_found):
    """정상 — 기준 재현 성공·시간 상한 없음이면 두 칸은 False 이고 `probe_found` 는 탐색 결과(단일·조합·없음)를 따른다."""
    res = _probe(feasible)
    assert bool(res["resolutions"]) is (kind == "resolutions") and bool(res["combo"]) is (kind == "combo")
    assert probe_status_fields(res) == _fields(want_found, False, False)


def test_slow_resolve_hits_the_time_budget():
    """시간 상한 — 상한을 아주 작게 두고 재계산이 느리면 기준 확인만으로 상한을 넘겨 `probe_timed_out` 이 True 다."""
    calls: list = []

    def slow(cfg: dict) -> bool:
        time.sleep(0.02)
        return False

    res = _probe(slow, budget_sec=0.001, calls=calls)
    assert len(calls) <= 1, "기준 확인 뒤 상한에 걸려 완화를 재계산하지 않는다"
    assert probe_status_fields(res) == _fields(False, False, True)


class _Clock:
    """탐색 모듈이 보는 시계 — 가짜 재계산 한 번에 10초가 흐른다."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now


@pytest.mark.parametrize(("feasible", "budget", "want"), [
    (_always, 5, _fields(False, True, True)),
    (_team, 15, _fields(True, False, True)),
    (_never, 5, _fields(False, False, True)),
], ids=["baseline_feasible_and_late", "found_then_late", "late_after_baseline"])
def test_time_budget_with_a_slow_fake_clock(monkeypatch, feasible, budget, want):
    """시간 상한(결정론 시계) — 상한은 기준 확인에도 걸리고, 상한 전에 찾은 해결책은 `probe_found` 로 남는다."""
    clock = _Clock()
    monkeypatch.setattr(probe, "time", SimpleNamespace(monotonic=clock.monotonic))

    def slow(cfg: dict) -> bool:
        clock.now += 10
        return feasible(cfg)

    assert probe_status_fields(_probe(slow, budget_sec=budget)) == want


@pytest.mark.parametrize("res", [None, SKIPPED_BY_CAUSE], ids=["not_run", "skipped_by_cause"])
def test_not_run_gives_three_nulls(res):
    """탐색을 돌리지 않았으면(None · 원인 진단 스킵) 세 칸 모두 null 이다."""
    assert probe_status_fields(res) == NULLS


@pytest.mark.parametrize(("res", "want"), [
    ({}, _fields(False, False, False)),
    ({"found": 1, "baseline_feasible": 0, "timed_out": None}, _fields(True, False, False)),
    ({"found": True, "baseline_feasible": True, "timed_out": True, "skipped_by_cause": False}, _fields(True, True, True)),
], ids=["missing_keys", "coerced", "all_true"])
def test_ran_gives_bools(res, want):
    """돌린 결과는 bool 이다 — 키가 없으면 False · 값은 bool 로 바꾼다 · 입력은 그대로 둔다."""
    before = dict(res)
    fields = probe_status_fields(res)
    assert fields == want and all(type(value) is bool for value in fields.values())
    assert res == before


# ── ② 원문 검사 ──────────────────────────────────────────────────────────────


def _service(tree: ast.Module | None = None) -> ast.FunctionDef:
    tree = tree or ast.parse(RCS.read_text(encoding="utf-8"))
    (func,) = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == _GEN]
    return func


def _is_status_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "probe_status_fields"


def _probe_try(func: ast.FunctionDef, alias: str) -> ast.Try:
    """`import os as <alias>` 로 시작하는 탐색 try 블록(사후 `_os_undiag` · A1 `_os_a1`)."""
    (block,) = [n for n in ast.walk(func) if isinstance(n, ast.Try) and n.body and isinstance(n.body[0], ast.Import)
                and [a.asname for a in n.body[0].names] == [alias]]
    return block


def _first_non_import(body: list[ast.stmt]) -> ast.stmt:
    return next(s for s in body if not isinstance(s, (ast.Import, ast.ImportFrom)))


def _only(body: list[ast.stmt], text: str) -> ast.stmt:
    (stmt,) = [s for s in body if ast.unparse(s) == text]
    return stmt


def _assign_lines(node: ast.AST, target: str) -> list[int]:
    return [n.lineno for n in ast.walk(node) if isinstance(n, ast.Assign) and [ast.unparse(t) for t in n.targets] == [target]]


def test_service_writes_the_three_fields_at_four_sites():
    """`probe_status_fields(` 4곳 — 모두 `<칸>.update(...)` 문장이고 원문 순서가 SITES 다. 세 칸을 직접 대입하는 줄은 0 이다."""
    src = RCS.read_text(encoding="utf-8")
    tree = ast.parse(src)
    func = _service(tree)
    assert src.count("probe_status_fields(") == 4
    calls = [n for n in ast.walk(tree) if _is_status_call(n)]
    assert len(calls) == 4 and all(func.lineno <= c.lineno <= func.end_lineno for c in calls)
    stmts = sorted((n for n in ast.walk(func) if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
                    and any(_is_status_call(a) for a in n.value.args)), key=lambda n: n.lineno)
    assert tuple(ast.unparse(s) for s in stmts) == SITES
    assert '["probe_found"] =' not in src and "['probe_found'] =" not in src
    subscripts = [t for n in ast.walk(tree) if isinstance(n, ast.Assign) for t in n.targets
                  if isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant) and t.slice.value in FIELDS]
    assert subscripts == []


def test_post_path_marks_the_stage_before_the_gate():
    """사후 — 탐색 try 의 첫 문장(import 뒤)이 None 이고, 결과 문장은 게이트 안에서 원인 스킵·탐색 두 갈래를 지난 뒤다."""
    block = _probe_try(_service(), "_os_undiag")
    assert ast.unparse(_first_non_import(block.body)) == SITES[0]
    (gate,) = [s for s in block.body if isinstance(s, ast.If) and "UNDIAG_PROBE_DISABLE" in ast.unparse(s.test)]
    result = _only(gate.body, SITES[1])
    branches = _assign_lines(gate, "_probe_res")
    assert len(branches) == 2 and max(branches) < result.lineno
    (skip,) = [n for n in ast.walk(gate) if isinstance(n, ast.Assign) and isinstance(n.value, ast.Dict)
               and [ast.unparse(t) for t in n.targets] == ["_probe_res"]]
    assert ast.literal_eval(skip.value) == SKIPPED_BY_CAUSE


def test_a1_splits_the_condition_and_marks_the_stage_first():
    """A1 — 바깥 조건(요청·위반·dict)에 끔 스위치가 없고, 그 안 첫 문장이 None 이며, 끔·스냅샷 조건은 안쪽에서 본다."""
    block = _probe_try(_service(), "_os_a1")
    (outer,) = [s for s in block.body if isinstance(s, ast.If)]
    assert ast.unparse(outer.test) == ("bool(getattr(req, 'suggest_fixes', False)) and _hv_cnt > 0 "
                                       "and isinstance(_inf_a1, dict)")
    first = _first_non_import(outer.body)
    assert ast.unparse(first) == SITES[2]
    (inner,) = [s for s in outer.body if isinstance(s, ast.If)]
    assert "UNDIAG_PROBE_DISABLE" in ast.unparse(inner.test) and "_a1_base" in ast.unparse(inner.test)
    assert first.lineno < inner.lineno
    result = _only(inner.body, SITES[3])
    (a1_res,) = _assign_lines(inner, "_a1_res")
    assert a1_res < result.lineno


# ── ③ 원문 블록 실행 ─────────────────────────────────────────────────────────


def _run_block(alias: str, namespace: dict) -> None:
    """서비스 원문의 탐색 try 블록 하나를 그대로 떼어 `namespace` 에서 실행한다(복사본이 아니라 지금 원문)."""
    module = ast.Module(body=[_probe_try(_service(), alias)], type_ignores=[])
    exec(compile(module, str(RCS), "exec"), namespace)


@pytest.mark.parametrize(("inf", "disabled"), [
    ({"resolution_options": [{"option_id": "x", "title_ko": "손볼 카드"}]}, False),
    ({}, True),
], ids=["gate_closed", "probe_disabled"])
def test_post_path_leaves_three_nulls_when_probe_does_not_run(monkeypatch, capsys, inf, disabled):
    """사후 — 탐색 단계에 들어섰으나 돌리지 않으면(게이트 닫힘·UNDIAG_PROBE_DISABLE) 세 칸이 null 로 남는다.
    (오버레이 소실 경로는 EI-13b 에서 없어졌다 — rollback 이 나도 탐색은 평범한 사본 입력으로 돈다.)"""
    if disabled:
        monkeypatch.setenv("UNDIAG_PROBE_DISABLE", "1")
    else:
        monkeypatch.delenv("UNDIAG_PROBE_DISABLE", raising=False)
    unrecoverable = {"infeasibility": dict(inf)}
    _run_block("_os_undiag", {"unrecoverable": unrecoverable})
    assert {field: unrecoverable["infeasibility"][field] for field in FIELDS} == NULLS
    assert "failed (ignore)" not in capsys.readouterr().out


#: A1 기준 스냅샷 — 실제 카탈로그에서 '팀 최소 soft 완화'만 풀리게 둔다(월N·연속근무는 이미 상한이라 건너뜀).
_A1_BASE = {"team_min_soft_fallback": False, "max_nig_per_month": 15, "max_conseq_work": 5, "off_days": 8,
            "preceptee_on": True, "ban_night_before_fixed_off": True, "ban_night_before_fixed_wanted_off": True}
_A1_UNUSED = ("db", "current_user", "nurses_for_engine", "preferences", "latest_config", "shift_manage_data",
              "combined_fixed_cells", "_assignments", "_inbound_assignments", "_outbound_assignments", "_attempt_src")


def _run_a1(*, suggest_fixes: bool = True, hv: int = 2, snapshot: dict | None = None, feasible=_never) -> tuple[dict, list]:
    """A1 블록을 가짜 엔진으로 돌린다 — 재계산은 `feasible(설정)` 이면 위반 0. (infeasibility, 재계산 설정 목록)."""
    solves: list = []

    def run_cp_sat_basic(*_args, config_override=None, **_kwargs):
        solves.append(dict(config_override or {}))
        return None, None, dict(config_override or {})

    def build_constraint_impact(cfg, _req):
        return {"hard_violation_count": 0 if feasible(cfg) else 1}

    inf: dict = {}
    namespace = {
        "roster_data": {"constraint_impact": {"hard_violation_count": hv}, "infeasibility": inf},
        "req": SimpleNamespace(suggest_fixes=suggest_fixes),
        "roster_system": SimpleNamespace(_effective_config_snapshot=snapshot),
        "_run_cp_sat_basic": run_cp_sat_basic, "_build_constraint_impact_payload": build_constraint_impact,
        "_probe_time_limit": lambda: 1, **dict.fromkeys(_A1_UNUSED),
    }
    _run_block("_os_a1", namespace)
    return inf, solves


@pytest.fixture
def a1_env(monkeypatch):
    """A1 실행 환경 — 끔 스위치 없음 · 탐색 시간 상한 끔(결정론)."""
    monkeypatch.delenv("UNDIAG_PROBE_DISABLE", raising=False)
    monkeypatch.setenv("AIDE_PROBE_BUDGET_SEC", "0")
    return monkeypatch


@pytest.mark.parametrize(("feasible", "want", "card"), [
    (_team, _fields(True, False, False), ["probe:soften_team_min"]),
    (_always, _fields(False, True, False), []),
], ids=["found", "baseline_feasible"])
def test_a1_probe_run_writes_its_result(a1_env, capsys, feasible, want, card):
    """A1 — 탐색을 돌리면 세 칸이 그 결과다(기준 재현 실패면 해결 카드 없이 `probe_baseline_feasible` True)."""
    inf, solves = _run_a1(snapshot=dict(_A1_BASE), feasible=feasible)
    assert {field: inf[field] for field in FIELDS} == want
    assert [o["option_id"] for o in inf["resolution_options"]] == card and solves
    assert "failed (ignore)" not in capsys.readouterr().out


@pytest.mark.parametrize(("disabled", "snapshot"), [(True, dict(_A1_BASE)), (False, None)],
                         ids=["probe_disabled", "no_snapshot"])
def test_a1_stage_without_a_run_leaves_three_nulls(a1_env, capsys, disabled, snapshot):
    """A1 — 요청·위반이 있으면 탐색 단계다. 끔(UNDIAG_PROBE_DISABLE)·기준 스냅샷 없음이면 재계산 0 · 카드 칸 0 · 세 칸 null."""
    if disabled:
        a1_env.setenv("UNDIAG_PROBE_DISABLE", "1")
    inf, solves = _run_a1(snapshot=snapshot)
    assert inf == NULLS and solves == []
    assert "failed (ignore)" not in capsys.readouterr().out


@pytest.mark.parametrize(("suggest_fixes", "hv"), [(False, 2), (True, 0)], ids=["not_requested", "no_violation"])
def test_a1_without_the_stage_adds_no_fields(a1_env, suggest_fixes, hv):
    """A1 — 요청이 없거나 위반이 0 이면 탐색 단계가 없다: 칸을 싣지 않는다(칸 없음 = 탐색 단계 없음)."""
    inf, solves = _run_a1(suggest_fixes=suggest_fixes, hv=hv, snapshot=dict(_A1_BASE))
    assert inf == {} and solves == []
