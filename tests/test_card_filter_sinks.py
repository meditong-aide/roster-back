"""EI-11 11a-1 — 카드 싱크 6자리 배선(`.omc/aide_docs/v2/engine_input/ei11/EI11_SPEC_v2.md` §2.2·§4.1).

해결 카드 칸(`resolution_options`·`_sole_option`)은 payload 가 나가는 싱크 6자리에서
`policy.card_filter.finalize_infeasibility` 로 거른다(P-2) — a `build_blocking_payload` · b `build_unrecoverable_payload` ·
c `raise_presolve_block` · d 생성 서비스 탐색 요약 앞 · e 최종 안전망 · f A1 탐색. 이 파일은
  ① 원문 검사 — 세 파일의 카드 칸 쓰기 13곳(양방향)과 finalize 호출 자리 순서(+ 같은 도구로 Q16 N전담 판정 정렬)
  ② 두 번 == 한 번 · ③ `_sole_option` · ④ 무효과 카드 무변경 · ⑤ 사후 탐색 게이트(Q14) · ⑥ 싱크마다 고장 주입
을 본다. DB·엔진·LLM 없이 돈다. 생성 서비스 안 싱크(d·e·f)는 원문 검사로 자리를 고정한다 — 실행 경로는 사다리 골든
(S1-L·S1-LT·S1-LB)이 덮는다. 11a-2 부터 처방 set cover 가 금지 처방을 후보에서 빼므로(생산자 제외) 빌더가 금지 처방
번들 카드를 만들지 않는다 — ②·⑤ 는 옛 처방 카드·생산자 제외를 끈 빌더로 싱크 쪽을 함께 본다.
"""

from __future__ import annotations

import ast
import copy
from collections import Counter
from pathlib import Path

import pytest
from fastapi import HTTPException
from services.cp_sat.undiagnosed_probe import treatments_to_resolution_options
from services.policy import card_filter as cf
from services.precheck.payload import build_blocking_payload, build_unrecoverable_payload
from services.precheck.presolve_block import PresolveBlockContext, PresolveFinding, raise_presolve_block

APP = Path(__file__).resolve().parents[1] / "app"
_SOURCES = {
    "payload": APP / "services/precheck/payload.py",
    "presolve": APP / "services/precheck/presolve_block.py",
    "rcs": APP / "services/roster_create_service.py",
}
_CARD_KEYS = ("resolution_options", "_sole_option")
_GEN = "_generate_roster_service_impl"
_FIX_WIDTH = 40  # 값 코드 조각 길이(ast.unparse 앞부분)

#: 카드 칸 쓰기 13곳 — (파일, 감싼 함수, 칸, 값 코드 앞부분). 줄 번호 대신 코드 조각으로 찾는다(줄이 밀려도 그대로).
#: 새 쓰기가 생기면 그 값이 싱크(finalize_infeasibility)를 지나는지 확인한 뒤 여기 더한다.
EXPECTED_CARD_WRITES: Counter = Counter({
    ("payload", "build_blocking_payload", "resolution_options", "<dict> _resolution_options"): 1,  # 싱크 a
    ("payload", "build_unrecoverable_payload", "resolution_options", "<dict> _resolution_options"): 1,  # 싱크 b
    ("presolve", "_merge_findings_into_payload", "resolution_options",
     "overlay if exclusive else overlay + base"): 1,  # 싱크 c
    # 생성 서비스 사후 경로 8곳 — 싱크 d(탐색 요약 앞)·e(최종 안전망)가 덮는다.
    ("rcs", _GEN, "resolution_options", "_cards + _ex"): 1,
    ("rcs", _GEN, "_sole_option", "_cards[0]"): 1,
    ("rcs", _GEN, "_sole_option", "list(_cards)"): 1,
    ("rcs", _GEN, "resolution_options", "[{'option_id': f'cause:{_cause.classific"): 1,
    ("rcs", _GEN, "resolution_options", "_probe_opts + _exist_opts"): 1,
    ("rcs", _GEN, "resolution_options", "[_wk_opt] + _opts_now"): 2,
    ("rcs", _GEN, "resolution_options", "[_nc_opt] + _opts_now"): 1,
    # 거른 `_sole_option` 되붙이기 — 싱크 e 뒤라 이미 거른 값이다.
    ("rcs", _GEN, "resolution_options", "_so if isinstance(_so, list) else [_so]"): 1,
    ("rcs", _GEN, "resolution_options", "_a1_opts"): 1,  # 싱크 f
})


# ── ① 원문 검사 ───────────────────────────────────────────────────────────────


class _CardWriteScan(ast.NodeVisitor):
    """카드 칸 쓰기(`x["resolution_options"] = …`·`x["_sole_option"] = …`·dict 키·`setdefault`)를 감싼 함수와 함께 모은다."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.stack = ["<module>"]
        self.writes: list[tuple[str, str, str, str]] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def _add(self, key: str, value: ast.AST, prefix: str = "") -> None:
        self.writes.append((self.label, self.stack[-1], key, (prefix + ast.unparse(value))[:_FIX_WIDTH]))

    def _target(self, target: ast.AST, value: ast.AST) -> None:
        if isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant) and target.slice.value in _CARD_KEYS:
            self._add(target.slice.value, value)

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self._target(target, node.value)
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self._target(node.target, node.value)
        self.generic_visit(node)

    def visit_Dict(self, node: ast.Dict) -> None:
        for key, value in zip(node.keys, node.values, strict=True):
            if isinstance(key, ast.Constant) and key.value in _CARD_KEYS:
                self._add(key.value, value, "<dict> ")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if (_callee(node) == "setdefault" and len(node.args) == 2 and isinstance(node.args[0], ast.Constant)
                and node.args[0].value in _CARD_KEYS):
            self._add(node.args[0].value, node.args[1], "<setdefault> ")
        self.generic_visit(node)


def _callee(call: ast.Call) -> str:
    func = call.func
    return func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""


def _tree(label: str) -> ast.Module:
    return ast.parse(_SOURCES[label].read_text(encoding="utf-8"))


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    (func,) = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name]
    return func


def _call_lines(node: ast.AST, name: str) -> list[int]:
    return sorted(c.lineno for c in ast.walk(node) if isinstance(c, ast.Call) and _callee(c) == name)


def test_card_writes_are_exactly_the_known_sites():
    """① 카드 칸 쓰기 = 기대 13곳(양방향). 새 쓰기가 생기면 싱크(finalize)를 지나는지 확인 뒤 목록에 더한다."""
    found: Counter = Counter()
    for label in _SOURCES:
        scan = _CardWriteScan(label)
        scan.visit(_tree(label))
        found.update(scan.writes)
    extra, missing = found - EXPECTED_CARD_WRITES, EXPECTED_CARD_WRITES - found
    assert not extra, f"새 카드 칸 쓰기 — 그 값이 싱크(finalize_infeasibility)를 지나는지 확인한 뒤 목록에 추가: {sorted(extra)}"
    assert not missing, f"기대한 카드 칸 쓰기가 없다(코드가 옮겨졌으면 목록을 고친다): {sorted(missing)}"
    assert sum(EXPECTED_CARD_WRITES.values()) == 13


@pytest.mark.parametrize("builder", ["build_blocking_payload", "build_unrecoverable_payload"])
def test_payload_builders_filter_lever_before_fix_and_finalize_last(builder):
    """싱크 a·b — 빌더마다 finalize 1회, 마지막 `return payload` 바로 앞. 레버 거름은 원인 `fix` 를 지우기 전이다."""
    func = _function(_tree("payload"), builder)
    assert _call_lines(func, "finalize_infeasibility") == [func.body[-2].lineno]
    assert ast.unparse(func.body[-2]) == "finalize_infeasibility(payload['infeasibility'])"
    assert ast.unparse(func.body[-1]) == "return payload"
    (lever,) = _call_lines(func, "filter_options")
    (fix_pop,) = [c.lineno for c in ast.walk(func) if isinstance(c, ast.Call) and ast.unparse(c) == "_c.pop('fix', None)"]
    assert lever < fix_pop


def test_presolve_sink_sits_between_merge_and_log():
    """싱크 c — `_merge_findings_into_payload(` 줄 < finalize 줄 < `if log_after_merge` 줄."""
    func = _function(_tree("presolve"), "raise_presolve_block")
    (merge,) = _call_lines(func, "_merge_findings_into_payload")
    (finalize,) = _call_lines(func, "finalize_infeasibility")
    (log_if,) = [n.lineno for n in func.body if isinstance(n, ast.If) and ast.unparse(n.test) == "log_after_merge is not None"]
    assert merge < finalize < log_if


def _assign_lines(func: ast.AST, unparsed_target: str) -> list[int]:
    return sorted(n.lineno for n in ast.walk(func) if isinstance(n, ast.Assign)
                  and any(ast.unparse(t) == unparsed_target for t in n.targets))


def test_service_sinks_d_e_f_sit_where_the_spec_says():
    """싱크 d 는 `_all_opts =` 줄 바로 앞, e 는 `_fin =` 줄 바로 앞, f 는 A1 카드 대입과 '검증된 옵션' 문구 사이다.
    rcs 는 싱크 자리마다 지역 import 로 부른다(파일 머리 import 를 늘리지 않는다)."""
    tree = _tree("rcs")
    func = _function(tree, _GEN)
    calls = [c for c in ast.walk(func) if isinstance(c, ast.Call) and _callee(c) == "_finalize_cards"]
    d, e, f = sorted(calls, key=lambda c: c.lineno)
    assert [ast.unparse(c) for c in (d, e, f)] == [
        "_finalize_cards(unrecoverable['infeasibility'])", "_finalize_cards(unrecoverable.get('infeasibility'))",
        "_finalize_cards(_inf_a1)"]
    assert _assign_lines(func, "_all_opts") == [d.lineno + 1]
    assert _assign_lines(func, "_fin") == [e.lineno + 1]
    (a1_cards,) = _assign_lines(func, "_inf_a1['resolution_options']")
    (a1_msg,) = [n.lineno for n in ast.walk(func) if isinstance(n, ast.If)
                 and ast.unparse(n.test) == "any((o.get('verified') for o in _a1_opts))"]
    assert a1_cards < f.lineno < a1_msg
    assert not [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "services.policy.card_filter"]


def test_card_n_only_judgement_matches_the_guard():
    """Q16(11a-1) — 카드 생산 두 자리(주말 해제 범인·야간 한도 후보)의 N전담 판정이 가드(`n_only_release_targets`)와 같은
    `use_mid=False` 다. MID 병동 [N,M] 간호사 카드가 가드 400 막다른 길이 되지 않는다(싱크와 별개 — 같은 원문 검사 도구로 본다)."""
    tree = _tree("rcs")
    calls = [c for c in ast.walk(_function(tree, _GEN)) if isinstance(c, ast.Call) and _callee(c) == "_is_n_only"]
    assert len(calls) == 2
    for call in calls:
        (use_mid,) = [kw.value for kw in call.keywords if kw.arg == "use_mid"]
        assert isinstance(use_mid, ast.Constant) and use_mid.value is False, ast.unparse(call)
    guard = _function(tree, "n_only_release_targets")
    assert "use_mid = False" in [ast.unparse(n) for n in ast.walk(guard) if isinstance(n, ast.Assign)]


# ── 표본 ─────────────────────────────────────────────────────────────────────


def _violation(cause_id: str, alias: str, **details) -> dict:
    """원인 위반 표본 — `tools/harness/matrix_50_cases.py` `_cs` 모양."""
    return {"reason_code": alias, "node_id": cause_id, "details": dict(details),
            "human_message_ko": f"matrix synthesis: {cause_id}"}


_REC_2N2OFF = ("cause:recovery:two_n_two_off_blocks_demand", "RECOVERY_2N2OFF_BLOCKS")
_NIGHT_RECOVERY_BUNDLE = "bundle:treatment:disable:night_recovery"


def _capacity_issue(suggested_value: int) -> dict:
    """연속근무 상한이 용량을 누르는 precheck 이슈(`conseq_cap_binding` — team_grade_precheck 모양)."""
    return {"reason_code": "CAPACITY_TOTAL_SHORTAGE", "severity": "hard",
            "evidence": {"conseq_cap_binding": {"config_key": "max_conseq_work", "current": 3,
                                                "suggested_value": suggested_value, "capacity_uncapped": 420}}}


class _FakeDb:
    """`db.delete`/`commit`/`rollback` 호출만 받는 가짜 세션."""

    def delete(self, _obj) -> None:
        return None

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None


def _bctx() -> PresolveBlockContext:
    """가짜 차단 맥락 — `tests/test_presolve_block.py` `_make_bctx` 방식(기록·스냅샷은 호출만 받는다)."""
    return PresolveBlockContext(
        db=_FakeDb(), config=object(), year=2026, month=11, job_id="job-1", persistent_fix=None, intervened=False,
        nurses_dict=[], config_dict={}, schedule=object(), record_status=lambda *_a, **_k: None,
        build_snapshot=lambda *_a, **_k: "snap", log=lambda _msg: None)


def _post_path_payload() -> dict:
    """사후 경로 모양 — 빌더(싱크 b) 뒤 서비스가 탐색 카드·원인 카드(별칭 `_sole_option`)를 앞에 붙인 payload.
    11a-2 부터 빌더는 금지 처방 번들을 만들지 않는다(set cover 제외) — 처방 통로도 거르는지 보려고 옛 처방 카드
    (배포 전 저장본 모양)를 뒤에 붙인다."""
    inf = build_unrecoverable_payload(violated_constraints=[_violation(*_REC_2N2OFF)])["infeasibility"]
    personal = {"option_id": "cause:fix_all_personal", "source": "cause", "apply": {"off_days": 6}}
    probes = [{"option_id": "probe:lower_off_days", "source": "probe", "apply": {"off_days": 5}},
              {"option_id": "probe:old_disable_3n2off", "source": "probe", "apply": {"two_offs_after_three_nig": False}}]
    stale = {"option_id": _NIGHT_RECOVERY_BUNDLE, "kind": "treatment_bundle", "source": "ontology",
             "treatment_ids": ["treatment:disable:night_recovery"]}
    inf["resolution_options"] = [probes[0], personal, probes[1], *(inf.get("resolution_options") or []), stale]
    inf["_sole_option"] = personal
    return inf


# ── ② 두 번 == 한 번 · ③ `_sole_option` · ④ 무효과 카드 ─────────────────────────


def test_finalize_twice_equals_once_on_post_path_payload():
    """② 사후 경로는 같은 객체를 b→d→e 로 세 번 거른다 — 한 번 거른 것과 같다(별칭·표지 덮어쓰기 포함)."""
    once = _post_path_payload()
    cf.finalize_infeasibility(once)
    twice = _post_path_payload()
    cf.finalize_infeasibility(twice)
    cf.finalize_infeasibility(twice)
    assert copy.deepcopy(twice) == copy.deepcopy(once)
    assert once["_sole_option"] is once["resolution_options"][1], "별칭이 같은 객체로 남아 표지가 두 칸에 같게 걸린다"
    assert [m["key"] for m in once["_sole_option"]["hard_lock_relax_marks"]] == ["off_days"]
    # 11c — 빌더가 원인(2N→2OFF 회복)을 덮는 금지 처방 기록(`kind="treatment"`)을 먼저 싣고, 싱크가 거른 카드가 뒤에 붙는다.
    assert [(p["kind"], p["id"]) for p in once["policy_forbidden"]] == [
        ("treatment", "treatment:disable:night_recovery"), ("card", "probe:old_disable_3n2off"),
        ("card", _NIGHT_RECOVERY_BUNDLE)]
    assert (once["blocked_by_hard_lock"], once["policy_filter_error"]) == (3, 0)


def _reattach_sole(inf: dict) -> None:
    """생성 서비스 최종 안전망 뒤 되붙이기(roster_create_service `_so = … .pop("_sole_option", None)` 자리) 그대로."""
    so = inf.pop("_sole_option", None)
    if so:
        inf["resolution_options"] = so if isinstance(so, list) else [so]


def _blocked_personal() -> dict:
    return {"option_id": "cause:fix_all_personal", "source": "cause",
            "monthly_limit_release": [{"nurse_id": "N1", "field": "n_max", "value": 16}]}


def _blocked_allowed_add() -> dict:
    return {"option_id": "cause:allowed_add", "source": "cause", "apply": {"use_mid": False}}


@pytest.mark.parametrize("shape", ["dict", "list"])
def test_sole_option_all_blocked_leaves_no_cards(shape):
    """③ `_sole_option` 이 다 걸리면 키를 지우고 `resolution_options = []` — 개인 지목 카드가 되살아나지 않는다(Q15)."""
    other = {"option_id": "release_weekend_off:N2", "source": "probe", "weekend_off_release": ["N2"]}
    if shape == "dict":
        sole = _blocked_personal()
        inf = {"resolution_options": [sole, other], "_sole_option": sole}
    else:
        sole = [_blocked_personal(), _blocked_allowed_add()]
        inf = {"resolution_options": [*sole, other], "_sole_option": sole}
    cf.finalize_infeasibility(inf)
    _reattach_sole(inf)
    assert inf["resolution_options"] == [] and "_sole_option" not in inf
    assert inf["blocked_by_hard_lock"] == len(inf["policy_forbidden"]) >= 1


def test_sole_option_list_partially_blocked_keeps_the_rest():
    """③ 대안 2카드 중 하나만 걸리면 남은 카드만 '유일 옵션'으로 되붙는다."""
    ok = {"option_id": "cause:banned_release", "source": "cause", "banned_wanted_release": [{"nurse_id": "N1", "days": [3]}]}
    bad = _blocked_allowed_add()
    inf = {"resolution_options": [ok, bad, {"option_id": "x"}], "_sole_option": [ok, bad]}
    cf.finalize_infeasibility(inf)
    _reattach_sole(inf)
    assert inf["resolution_options"] == [ok] and "_sole_option" not in inf
    assert [(p["kind"], p["id"]) for p in inf["policy_forbidden"]] == [("cause_card", "cause:allowed_add")]


def test_no_effect_cards_pass_the_presolve_sink_unchanged(monkeypatch):
    """④ 효과 통로가 없는 합성 카드는 키 추가 0·순서 그대로 — `tests/test_presolve_block.py` 의 정확 비교가 그대로다."""
    import services.precheck as precheck

    overlay, base = {"id": "overlay"}, {"id": "base"}
    monkeypatch.setattr(precheck, "build_blocking_payload",
                        lambda _result: {"infeasibility": {"resolution_options": [base]}})
    with pytest.raises(HTTPException) as exc_info:
        raise_presolve_block(_bctx(), [PresolveFinding(reason_code=None, cards=[overlay])],
                             record_first=True, on_record_fail="block", card_merge="prepend")
    inf = exc_info.value.detail["infeasibility"]
    assert inf["resolution_options"] == [{"id": "overlay"}, {"id": "base"}]
    assert inf["resolution_options"][0] is overlay and inf["resolution_options"][1] is base
    assert (inf["policy_forbidden"], inf["blocked_by_hard_lock"], inf["policy_filter_error"]) == ([], 0, 0)


# ── ⑤ 사후 탐색 게이트(Q14) ───────────────────────────────────────────────────


def _probe_gate_opens(inf: dict) -> bool:
    """사후 탐색 게이트(roster_create_service `_ui_options_pre` 자리) 그대로 — 손볼 카드도 수동 안내(fix)도 없으면 연다."""
    codes = ((inf.get("structural_diagnosis") or {}).get("signals") or {}).get("reason_codes") or []
    options = inf.get("resolution_options") or []
    causes_fix = [c for c in (inf.get("causes") or []) if isinstance(c, dict) and c.get("fix")]
    return ("UNDIAGNOSED" in codes) or not (bool(options) or bool(causes_fix))


def test_probe_gate_mirror_reads_the_same_fields_as_the_service():
    """위 게이트 사본이 서비스 원문과 같은 칸을 읽는다(원문이 바뀌면 사본도 고친다)."""
    src = _SOURCES["rcs"].read_text(encoding="utf-8")
    for line in ('_ui_options_pre = _inf_pre.get("resolution_options") or []',
                 "_has_actionable = bool(_ui_options_pre) or bool(_ui_causes_fix)",
                 '_is_undiag = ("UNDIAGNOSED" in _codes_pre) or not _has_actionable'):
        assert line in src, line


def test_probe_gate_opens_when_only_hard_lock_cards_were_filtered(monkeypatch):
    """⑤ 하드락 처방 카드만 있던 원인(2N→2OFF 회복)은 카드 0·수동 안내 0 → 탐색 게이트가 열린다(Q14 수용).
    11a-2 부터는 set cover 가 금지 처방을 후보에서 빼 카드가 처음부터 없다(1순위 번들 `bundle:empty`). 생산자 제외가
    빠져도(`_excluded_treatment_ids` 를 빈 집합으로 — 11a-1 상태) 싱크 b 가 같은 카드를 거른다(두 겹).
    11c 부터 `policy_forbidden` 맨 앞에 원인을 덮는 금지 처방 기록(`kind="treatment"`)이 실린다 — 카드가 아니라 게이트와 무관하다."""
    from services import cause_treatment_hitter as hitter

    treatment_record = ("treatment", "treatment:disable:night_recovery")
    inf = build_unrecoverable_payload(violated_constraints=[_violation(*_REC_2N2OFF)])["infeasibility"]
    assert [b["bundle_id"] for b in inf["treatment_recommendations"]][:1] == ["bundle:empty"]
    assert treatments_to_resolution_options(inf["treatment_recommendations"]) == []
    assert inf["resolution_options"] == [] and not [c for c in inf["causes"] if c.get("fix")]
    assert [(p["kind"], p["id"]) for p in inf["policy_forbidden"]] == [treatment_record]
    assert inf["blocked_by_hard_lock"] == 1
    assert "UNDIAGNOSED" not in inf["structural_diagnosis"]["signals"]["reason_codes"]
    assert _probe_gate_opens(inf)

    monkeypatch.setattr(hitter, "_excluded_treatment_ids", lambda _onto: frozenset())
    inf = build_unrecoverable_payload(violated_constraints=[_violation(*_REC_2N2OFF)])["infeasibility"]
    before = treatments_to_resolution_options(inf["treatment_recommendations"])
    assert [o["option_id"] for o in before] == [_NIGHT_RECOVERY_BUNDLE], "생산자 제외가 없으면 이 카드가 게이트를 닫았다"
    assert inf["resolution_options"] == [] and not [c for c in inf["causes"] if c.get("fix")]
    assert [(p["kind"], p["id"]) for p in inf["policy_forbidden"]] == [treatment_record, ("card", _NIGHT_RECOVERY_BUNDLE)]
    assert _probe_gate_opens(inf)


# ── ⑥ 싱크마다 고장 주입 ───────────────────────────────────────────────────────

_CONFIG_CHANNELS = ("apply", "treatment_ids", "monthly_limit_release", "weekend_off_release",
                    "banned_wanted_release", "allowed_shift_add")


def _boom(_delta):
    raise RuntimeError("주입 — 하드락 판정 실패")


def _changes_settings(card) -> bool:
    return isinstance(card, dict) and (any(card.get(ch) for ch in _CONFIG_CHANNELS)
                                       or (card.get("source") == "cause" and card.get("bucket") == "tradeoff"))


def _assert_fail_closed(inf: dict) -> None:
    assert inf["severity"] == "blocking" and isinstance(inf["causes"], list), "구조화 payload 가 그대로 나온다"
    assert inf["policy_filter_error"] >= 1
    assert [c for c in inf["resolution_options"] if _changes_settings(c)] == []
    assert inf["blocked_by_hard_lock"] == len(inf["policy_forbidden"]) >= 1


def _fault_issues() -> list[dict]:
    return [_capacity_issue(5), {"reason_code": _REC_2N2OFF[1], "severity": "hard", "evidence": {}}]


def test_fault_in_sink_a_blocking_payload(monkeypatch):
    """⑥ 싱크 a — 판정이 터져도 구조화 payload 가 나오고 설정을 바꾸는 카드는 0, `policy_filter_error ≥ 1`."""
    monkeypatch.setattr(cf, "override_violations", _boom)
    _assert_fail_closed(build_blocking_payload({"issues": _fault_issues()})["infeasibility"])


def test_fault_in_sink_b_unrecoverable_payload(monkeypatch):
    """⑥ 싱크 b — 위와 같다(라우터 폴백·대시보드도 이 빌더를 지난다)."""
    monkeypatch.setattr(cf, "override_violations", _boom)
    inf = build_unrecoverable_payload(precheck_result={"issues": [_capacity_issue(5)]},
                                      violated_constraints=[_violation(*_REC_2N2OFF)])["infeasibility"]
    _assert_fail_closed(inf)


def test_fault_in_sink_c_presolve_block(monkeypatch):
    """⑥ 싱크 c — 차단 응답(HTTP 500) detail 이 dict 그대로이고 수동 카드만 남는다."""
    monkeypatch.setattr(cf, "override_violations", _boom)
    cards = [{"option_id": "overlay:apply", "apply": {"off_days": 6}}, {"option_id": "overlay:manual", "fix": {"mode": "manual"}}]
    with pytest.raises(HTTPException) as exc_info:
        raise_presolve_block(_bctx(), [PresolveFinding(reason_code=None, cards=cards)], record_first=True,
                             on_record_fail="block", card_merge="prepend", base_issues=_fault_issues())
    assert exc_info.value.status_code == 500 and isinstance(exc_info.value.detail, dict)
    inf = exc_info.value.detail["infeasibility"]
    _assert_fail_closed(inf)
    assert "overlay:manual" in [c.get("option_id") for c in inf["resolution_options"]]
