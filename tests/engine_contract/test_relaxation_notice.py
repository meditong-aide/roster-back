"""자동 완화 안내(EI-14c) 계약 — 기존 알림 접두 유지·예외 0·결정론·근거 합계·표 갱신.

알림은 주 골든에 안 잡히므로 이 시험이 전후 표다(SPEC §6-A): 케이스마다 before(옛 detail)는 after 의 접두다.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from services.cp_sat.fallback_lex import count_n2n_gap_violations
from services.cp_sat.rule_card import NONE_REASONS, RULE_CARD
from services.precheck.relaxation_notice import build_relaxation_notice

APP = Path(__file__).resolve().parents[2] / "app"
ST = ["D", "E", "N", "O"]
D, E, N, O = 0, 1, 2, 3  # noqa: E741


def _nurse(i: int, name: str, allowed=None):
    return SimpleNamespace(nurse_id=f"N{i}", name=name, allowed_shifts=allowed)


def _rs(grid: list[str], nurses, **extra):
    """grid: 간호사별 문자열(D/E/N/O 한 글자씩) → roster[n, d, s] one-hot."""
    code = {"D": D, "E": E, "N": N, "O": O}
    days = len(grid[0])
    r = np.zeros((len(grid), days, 4), dtype=int)
    for n, row in enumerate(grid):
        for d, ch in enumerate(row):
            r[n, d, code[ch]] = 1
    return SimpleNamespace(roster=r, config=SimpleNamespace(shift_types=list(ST), use_mid=False),
                           nurses=nurses, num_days=days, **extra)


def _base(t: str, d: str = "원문 알림입니다.") -> dict:
    return {"type": t, "detail": d}


def _call(kind, rs, nurses, param, base=None, month=11):
    b = base or _base("x_applied")
    return b, build_relaxation_notice(kind, b, roster_system=rs, nurses=nurses, year=2026, month=month,
                                      param=param)


NURSES = [_nurse(1, "홍길동"), _nurse(2, "김철수"), _nurse(3, "이영희")]

# ── 공통 계약 ────────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", [None, SimpleNamespace(), SimpleNamespace(roster=None),
                                 SimpleNamespace(roster=np.zeros((0, 0, 0)), config=SimpleNamespace(shift_types=[])),
                                 SimpleNamespace(config=SimpleNamespace(shift_types=ST), nurses=[], num_days=3)])
@pytest.mark.parametrize("kind", ["n2n_released", "n2n_partial", "same_shift", "isolated_work", "unknown"])
def test_fake_inputs_return_base_unchanged(bad, kind) -> None:
    b, out = _call(kind, bad, [], 5)
    assert out == b
    assert out is not b


def test_garbage_nurses_and_param_never_raise() -> None:
    rs = _rs(["NNOOO"], NURSES)
    for param in (None, "x", -1, 0):
        b, out = _call("n2n_released", rs, None, param)
        assert out["type"] == b["type"] and out["detail"].startswith(b["detail"])


def test_module_imports_no_db_env_or_print() -> None:
    src = (APP / "services/precheck/relaxation_notice.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            mods.add((node.module or "").split(".")[0])
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id != "print"
    assert not mods & {"sqlalchemy", "db", "os", "ortools"}, mods
    assert "environ" not in src


def test_no_stdout(capsys) -> None:
    _call("n2n_released", _rs(["NNOOONNOOO"], NURSES), None, 5)
    assert capsys.readouterr().out == ""


# ── n2n 값 ───────────────────────────────────────────────────────────

GAP5_CASES = [
    # (이름, 배치, 간호사, 기대 위반 칸 수)
    ("위반 없음", ["NNOOOONNOO"], NURSES[:1], 0),
    ("간격 짧음 2곳", ["NNOONNOOOO"], NURSES[:1], 2),  # 블록 끝 d=1 → d=2,3,4 금지: d=4·5(0기준 4·5) N
    ("N전담 제외", ["NNOONNOOOO"], [_nurse(1, "홍길동", ["N"])], 0),
]


@pytest.mark.parametrize("label,grid,nurses,expect", GAP5_CASES, ids=[c[0] for c in GAP5_CASES])
def test_n2n_released_counts(label, grid, nurses, expect) -> None:
    rs = _rs(grid, nurses)
    b, out = _call("n2n_released", rs, nurses, 5, _base("n2n_min_gap_released_applied"))
    assert out["type"] == b["type"]
    assert out["detail"].startswith(b["detail"] + " ")
    if expect == 0:
        assert "실제로 간격이 짧아진 곳은 없었어요." in out["detail"]
        assert out["evidence"] == [] and "adjust_ko" not in out
    else:
        assert "홍길동 11/5·11/6" in out["detail"]
        assert sum(len(e["days"]) for e in out["evidence"]) == expect
        assert out["adjust_ko"].startswith("간격을 지킨 채 만들려면")
    json.dumps(out)


def test_n2n_partial_matches_engine_counter_incl_cross_month_pair() -> None:
    grid = ["NNOONNOOOO", "OONNOONNOO"]
    nurses = NURSES[:2]
    # 엔진 금지쌍 모양: (n, 금지일, ((조건일, N여부), …)) — 둘째는 월경계 쌍(조건 일부 없음)
    pairs = [(0, 3, ((1, 1), (2, 0))), (0, 4, ((1, 1), (2, 0))), (1, 4, ((3, 1), (4, 0))),
             (1, 2, ()), (1, 3, ((2, 1),))]
    rs = _rs(grid, nurses, _n2n_pair_idx=pairs, _n2n_night_idx=N)
    expected = count_n2n_gap_violations(rs)
    assert expected == 3  # (0,4) · 월경계형 무조건 쌍 (1,2) · (1,3)
    b, out = _call("n2n_partial", rs, nurses, 5, _base("n2n_min_gap_partial_applied", "부분 원문."))
    assert sum(len(e["days"]) for e in out["evidence"]) == expected
    assert out["cause_ko"] == "나이트와 나이트 사이를 5일 이상 띄우는 규칙을 모두 지킬 수는 없었어요."
    assert out["detail"].startswith("부분 원문. 나이트와 나이트 사이를 5일 이상")


def test_n2n_partial_without_pairs_keeps_base() -> None:
    rs = _rs(["NNOONNOOOO"], NURSES[:1])
    b, out = _call("n2n_partial", rs, NURSES[:1], 5)
    assert out == b


def test_determinism_and_no_mutation() -> None:
    rs = _rs(["NNOONNOOOO", "NOONNOONNO"], NURSES[:2])
    before = rs.roster.copy()
    _, a = _call("n2n_released", rs, NURSES[:2], 5)
    _, c = _call("n2n_released", rs, NURSES[:2], 5)
    assert a == c
    assert (rs.roster == before).all()


def test_evidence_cap_20() -> None:
    nurses = [_nurse(i, f"간호사{i}") for i in range(30)]
    rs = _rs(["NNOONNOOOO"] * 30, nurses)
    _, out = _call("n2n_released", rs, nurses, 5)
    assert len(out["evidence"]) == 20
    assert "외 54곳" in out["detail"]  # 위반 60칸 중 예시 3명 x 2칸 = 6
    json.dumps(out)


def test_n2n_wording_verbatim() -> None:
    rs = _rs(["NNOONNOOOO"], NURSES[:1])
    b, out = _call("n2n_released", rs, NURSES[:1], 5, _base("n2n_min_gap_released_applied", "기존."))
    assert out["detail"] == (
        "기존. 나이트와 나이트 사이를 5일 이상 띄우는 규칙을 다른 조건(필요 인원·확정 근무 등)과 함께 지킬 수 없어, "
        "이 규칙을 풀고 근무표를 만들었어요. 실제로 간격이 짧아진 곳: 홍길동 11/5·11/6. "
        "간격을 지킨 채 만들려면 나이트를 설 수 있는 인원을 늘리거나, 그 날짜 앞뒤의 확정 근무·휴무 신청을 "
        "조정해 주세요."
    )


def test_example_boundary_three_plus_rest() -> None:
    nurses = [_nurse(i, f"가{i}") for i in range(4)]
    rs = _rs(["NNOONNOOOO"] * 4, nurses)
    _, out = _call("n2n_released", rs, nurses, 5)
    assert "실제로 간격이 짧아진 곳: 가0 11/5·11/6, 가1 11/5·11/6, 가2 11/5·11/6 외 2곳." in out["detail"]


# ── 표·자리 ──────────────────────────────────────────────────────────


def test_tables_and_vocab() -> None:
    assert '없음: 자동 완화' in NONE_REASONS
    assert RULE_CARD["n2n_min_gap"] == "없음: 자동 완화"


def test_rcs_sites_use_builder() -> None:
    src = (APP / "services/roster_create_service.py").read_text(encoding="utf-8")
    for t in ("n2n_min_gap_released_applied", "n2n_min_gap_partial_applied"):
        i = src.index(f'"type": "{t}"')
        assert "build_relaxation_notice(" in src[max(0, i - 120):i], t
    assert 'weekly_off_warnings.append({\n                "type": "n2n_min_gap_' not in src
    assert 'weekly_off_warnings.append({\n            "type": "n2n_min_gap_' not in src


# ── same_shift_hard_k (14c-2) ────────────────────────────────────────

SS_OLD = ("같은 시프트 연속 하드 제한이 infeasible 로 자동 soft 전환되어 재생성됐습니다. "
          "일부 간호사에게 같은 시프트가 4회 이상 연속될 수 있습니다.")


def _ss(grid, nurses, k=3, base=None):
    return _call("same_shift", _rs(grid, nurses), nurses, k,
                 base or _base("same_shift_hard_to_soft_applied", SS_OLD))


SS_CASES = [
    # (이름, 배치, 간호사, 예상 근거 문장 일부)
    ("D 4일", ["OODDDDOOOO"], NURSES[:1], "홍길동 11/3~11/6(D 4일)"),
    ("E 5일", ["OEEEEEOOOO"], NURSES[:1], "홍길동 11/2~11/6(E 5일)"),
    ("월말에 걸침", ["OOOOOODDDD"], NURSES[:1], "홍길동 11/7~11/10(D 4일)"),
]


@pytest.mark.parametrize("label,grid,nurses,frag", SS_CASES, ids=[c[0] for c in SS_CASES])
def test_same_shift_segments(label, grid, nurses, frag) -> None:
    b, out = _ss(grid, nurses)
    assert out["type"] == b["type"] and out["detail"].startswith(SS_OLD + " ")
    assert "실제로 4일 이상 이어진 곳: " + frag + "." in out["detail"]
    assert out["adjust_ko"].startswith("규칙을 지킨 채 만들려면")
    assert len(out["evidence"]) == 1
    json.dumps(out)


def test_same_shift_none_and_k_boundary() -> None:
    b, out = _ss(["ODDDOEEEON"], NURSES[:1])  # 3일 연속은 K=3 이내
    assert "실제로 규칙을 넘은 곳은 없었어요." in out["detail"]
    assert "adjust_ko" not in out and out["evidence"] == []


def test_same_shift_single_shift_nurse_excluded() -> None:
    nurses = [_nurse(1, "홍길동", ["D"])]
    _, out = _ss(["OODDDDOOOO"], nurses)
    assert "실제로 규칙을 넘은 곳은 없었어요." in out["detail"]


def test_same_shift_example_limit_and_rest() -> None:
    nurses = [_nurse(i, f"가{i}") for i in range(5)]
    _, out = _ss(["ODDDDOOOOO"] * 5, nurses)
    assert ("실제로 4일 이상 이어진 곳: 가0 11/2~11/5(D 4일), 가1 11/2~11/5(D 4일), "
            "가2 11/2~11/5(D 4일) 외 2명.") in out["detail"]
    assert len(out["evidence"]) == 5


def test_same_shift_wording_verbatim() -> None:
    _, out = _ss(["ODDDDOOOOO"], NURSES[:1], base=_base("same_shift_hard_to_soft_applied", "기존."))
    assert out["detail"] == (
        "기존. 같은 근무를 3일까지만 이어 배정하는 규칙을 다른 조건(필요 인원·확정 근무 등)과 함께 지킬 수 없어, "
        "이 규칙을 풀고 근무표를 만들었어요. 실제로 4일 이상 이어진 곳: 홍길동 11/2~11/5(D 4일). "
        "규칙을 지킨 채 만들려면 해당 간호사의 확정 근무·원티드 날짜를 나누어 주거나, 그 근무의 필요 인원을 줄여 주세요."
    )


def test_same_shift_k_zero_keeps_base() -> None:
    b, out = _ss(["ODDDDOOOOO"], NURSES[:1], k=0)
    assert out == b


def test_carried_without_extra_args_is_legacy() -> None:
    from services.roster_create_service import _carried_hardlock_relaxations as carried
    cfg = SimpleNamespace(same_shift_hard_k=0, isolated_work_hard=False)
    out = carried(cfg, True, True)
    assert [k for k, _ in out] == ["same_shift_hard_to_soft", "isolated_work_hard_to_soft"]
    assert out[0][1] == {"type": "same_shift_hard_to_soft_applied", "detail": SS_OLD}
    assert out[1][1] == {
        "type": "isolated_work_hard_to_soft_applied",
        "detail": ("고립근무 금지가 infeasible 로 자동 soft 전환되어 재생성됐습니다. "
                   "일부 간호사에게 휴무 사이 단일 근무가 생길 수 있습니다.")}


def test_carried_with_args_adds_fields_keeps_shape() -> None:
    from services.roster_create_service import _carried_hardlock_relaxations as carried
    cfg = SimpleNamespace(same_shift_hard_k=0, isolated_work_hard=True)
    rs = _rs(["ODDDDOOOOO"], NURSES[:1])
    out = carried(cfg, True, False, roster_system=rs, nurses=NURSES[:1], year=2026, month=11, ss_orig_k=3)
    assert len(out) == 1 and out[0][0] == "same_shift_hard_to_soft"
    w = out[0][1]
    assert w["type"] == "same_shift_hard_to_soft_applied" and w["detail"].startswith(SS_OLD)
    assert "cause_ko" in w and w["evidence"]


def test_same_shift_rule_card_and_rcs_site() -> None:
    assert RULE_CARD["same_shift_hard_k"] == "없음: 자동 완화"
    src = (APP / "services/roster_create_service.py").read_text(encoding="utf-8")
    i = src.index('"type": "same_shift_hard_to_soft_applied"')
    assert 'build_relaxation_notice("same_shift"' in src[max(0, i - 140):i]


# ── isolated_work (14c-3) ────────────────────────────────────────────

IW_OLD = ("고립근무 금지가 infeasible 로 자동 soft 전환되어 재생성됐습니다. "
          "일부 간호사에게 휴무 사이 단일 근무가 생길 수 있습니다.")


def _iw(grid, nurses, base=None):
    return _call("isolated_work", _rs(grid, nurses), nurses, None,
                 base or _base("isolated_work_hard_to_soft_applied", IW_OLD))


@pytest.mark.parametrize("code", ["D", "E"])
def test_isolated_work_middle_codes(code) -> None:
    b, out = _iw([f"DO{code}OOODDDD"], NURSES[:1])
    assert out["type"] == b["type"] and out["detail"].startswith(IW_OLD + " ")
    assert f"실제로 생긴 곳: 홍길동 11/3({code})." in out["detail"]
    assert out["adjust_ko"].startswith("그 날 앞뒤의 휴무나 확정 근무를")
    assert out["evidence"] == [{"nurse_id": "N1", "name": "홍길동", "days": [3], "detail_ko": f"{code} 하루만 근무"}]
    json.dumps(out)


def test_isolated_work_excludes_night_and_w() -> None:
    # N 가운데 · 월 첫날/말일 · 이틀 근무는 대상 밖
    _, out = _iw(["DOUOO".replace("U", "N") + "OODOO"], NURSES[:1])
    # 위 배치: D O N O O O O D O O → 가운데 N(2)은 제외, 가운데 D(7)는 O-D-O 라 1건
    assert "11/8(D)" in out["detail"] and "11/3" not in out["detail"]
    _, out2 = _iw(["ODOOODDOOO"], NURSES[:1])  # 첫날 O 다음 D(1) 앞 O(0) 뒤 O(2) → 1건
    assert "홍길동 11/2(D)" in out2["detail"]
    _, out3 = _iw(["ODDOOODDOO"], NURSES[:1])  # 이틀 근무
    assert "실제로 생긴 곳은 없었어요." in out3["detail"]
    assert "adjust_ko" not in out3 and out3["evidence"] == []


def test_isolated_work_first_and_last_day_out_of_scope() -> None:
    _, out = _iw(["DOOOOOOOOD"], NURSES[:1])
    assert "실제로 생긴 곳은 없었어요." in out["detail"]


def test_isolated_work_w_middle_excluded() -> None:
    cfg_st = ["D", "E", "N", "O", "W"]
    r = np.zeros((1, 5, 5), dtype=int)
    for d, c in enumerate("OWOOO"):
        r[0, d, cfg_st.index(c)] = 1
    rs = SimpleNamespace(roster=r, config=SimpleNamespace(shift_types=cfg_st), nurses=NURSES[:1], num_days=5)
    _, out = _call("isolated_work", rs, NURSES[:1], None, _base("isolated_work_hard_to_soft_applied", IW_OLD))
    assert "실제로 생긴 곳은 없었어요." in out["detail"]


def test_isolated_work_example_limit_and_wording() -> None:
    nurses = [_nurse(i, f"가{i}") for i in range(5)]
    _, out = _iw(["ODOOOOOOOO"] * 5, nurses, base=_base("isolated_work_hard_to_soft_applied", "기존."))
    assert out["detail"] == (
        "기존. 쉬는 날 사이에 하루만 끼는 근무(쉼-근무-쉼)를 막는 규칙을 다른 조건과 함께 지킬 수 없어, "
        "이 규칙을 풀고 근무표를 만들었어요. 실제로 생긴 곳: 가0 11/2(D), 가1 11/2(D), 가2 11/2(D) 외 2곳. "
        "그 날 앞뒤의 휴무나 확정 근무를 조정해 근무가 이틀 이상 이어지게 하면 규칙을 지킨 채 만들 수 있어요."
    )


def test_isolated_work_carried_and_rule_card() -> None:
    from services.roster_create_service import _carried_hardlock_relaxations as carried
    cfg = SimpleNamespace(same_shift_hard_k=3, isolated_work_hard=False)
    rs = _rs(["ODOOOOOOOO"], NURSES[:1])
    out = carried(cfg, False, True, roster_system=rs, nurses=NURSES[:1], year=2026, month=11, ss_orig_k=3)
    assert [k for k, _ in out] == ["isolated_work_hard_to_soft"]
    w = out[0][1]
    assert w["type"] == "isolated_work_hard_to_soft_applied" and w["detail"].startswith(IW_OLD) and w["evidence"]
    assert RULE_CARD["isolated_work"] == "없음: 자동 완화"
    src = (APP / "services/roster_create_service.py").read_text(encoding="utf-8")
    assert src.count('build_relaxation_notice("isolated_work"') == 2


# ── 14c-R 보강(리뷰 지적) ────────────────────────────────────────────


def _real_nurse(i: int, name: str, db_id: str, allowed=None):
    from db.nurse_config import Nurse
    return Nurse(id=i, name=name, experience_years=0, db_id=db_id, allowed_shifts=allowed)


def test_evidence_nurse_id_is_db_id_for_real_engine_nurse() -> None:
    nurses = [_real_nurse(0, "홍길동", "DBID-AAA"), _real_nurse(1, "김철수", "DBID-BBB")]
    _, n2n = _call("n2n_released", _rs(["NNOONNOOOO", "OOOOOOOOOO"], nurses), nurses, 5)
    assert [e["nurse_id"] for e in n2n["evidence"]] == ["DBID-AAA"]
    _, ss = _call("same_shift", _rs(["ODDDDOOOOO", "OOOOOOOOOO"], nurses), nurses, 3)
    assert [e["nurse_id"] for e in ss["evidence"]] == ["DBID-AAA"]
    _, iw = _call("isolated_work", _rs(["OOOOOOOOOO", "ODOOOOOOOO"], nurses), nurses, None)
    assert [e["nurse_id"] for e in iw["evidence"]] == ["DBID-BBB"]


def test_adjust_sentences_never_tell_to_loosen_hard_lock_settings() -> None:
    rs = _rs(["NNOONNOOOO"], NURSES[:1])
    outs = [_call("n2n_released", rs, NURSES[:1], 5)[1],
            _call("same_shift", _rs(["ODDDDOOOOO"], NURSES[:1]), NURSES[:1], 3)[1],
            _call("isolated_work", _rs(["ODOOOOOOOO"], NURSES[:1]), NURSES[:1], None)[1]]
    for o in outs:
        adj = o["adjust_ko"]
        assert adj
        for bad in ("낮춰", "값을", "상한", "설정", "끄", "해제", "풀어"):
            assert bad not in adj, (bad, adj)


def test_n2n_partial_n_only_nurse_with_no_violation_says_none() -> None:
    nurses = [_nurse(1, "홍길동", ["N"])]
    rs = _rs(["NNOOOONNOO"], nurses, _n2n_pair_idx=[(0, 3, ((1, 1), (2, 0)))], _n2n_night_idx=N)
    _, out = _call("n2n_partial", rs, nurses, 5, _base("n2n_min_gap_partial_applied", "부분."))
    assert "실제로 간격이 짧아진 곳은 없었어요." in out["detail"]
    assert out["evidence"] == [] and "adjust_ko" not in out


def test_same_shift_mid_code_counts() -> None:
    st = ["D", "E", "N", "O", "M"]
    r = np.zeros((1, 8, 5), dtype=int)
    for d, c in enumerate("OMMMMOOO"):
        r[0, d, st.index(c)] = 1
    rs = SimpleNamespace(roster=r, config=SimpleNamespace(shift_types=st, use_mid=True), nurses=NURSES[:1], num_days=8)
    _, out = _call("same_shift", rs, NURSES[:1], 3, _base("same_shift_hard_to_soft_applied", SS_OLD))
    # 같은 근무 구간은 엔진이 보는 D·E·N 만 센다(M 은 대상 밖) — 현재 계약을 고정한다.
    assert "실제로 규칙을 넘은 곳은 없었어요." in out["detail"]


def test_isolated_work_mid_middle_code() -> None:
    st = ["D", "E", "N", "O", "M"]
    r = np.zeros((1, 6, 5), dtype=int)
    for d, c in enumerate("OOMOOO"):
        r[0, d, st.index(c)] = 1
    rs = SimpleNamespace(roster=r, config=SimpleNamespace(shift_types=st, use_mid=True), nurses=NURSES[:1], num_days=6)
    _, out = _call("isolated_work", rs, NURSES[:1], None, _base("isolated_work_hard_to_soft_applied", IW_OLD))
    assert "실제로 생긴 곳: 홍길동 11/3(M)." in out["detail"]


def test_n2n_gap_uses_engine_effective_value(monkeypatch) -> None:
    monkeypatch.setenv("AIDE_N2N_MIN_GAP", "6")
    rs = _rs(["NNOONNOOOO"], NURSES[:1])
    _, out = _call("n2n_released", rs, NURSES[:1], 5)
    assert "6일 이상 띄우는" in out["cause_ko"]


def _calls_in_rcs():
    """rcs 의 build_relaxation_notice 호출 → [(감싼 함수 또는 '', kind, {키워드: 식})]."""
    src = (APP / "services/roster_create_service.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    found = []

    def walk(node, fn):
        for ch in ast.iter_child_nodes(node):
            cur = ch.name if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)) else fn
            if isinstance(ch, ast.Call) and getattr(ch.func, "id", "") == "build_relaxation_notice":
                found.append((fn, ch.args[0].value, {k.arg: ast.unparse(k.value) for k in ch.keywords}))
            walk(ch, cur)

    walk(tree, "")
    return tree, found


def test_rcs_call_arguments_are_pinned() -> None:
    _, found = _calls_in_rcs()
    by = {}
    for fn, kind, kw in found:
        by.setdefault((kind, fn == "_carried_hardlock_relaxations"), []).append(kw)
    assert len(found) == 6
    common = {"nurses": "nurses_for_engine", "year": "req.year", "month": "req.month"}
    (n2n_rel,) = by[("n2n_released", False)]
    assert n2n_rel == {**common, "roster_system": "roster_system", "param": "_mg_on"}
    (n2n_par,) = by[("n2n_partial", False)]
    assert n2n_par == {**common, "roster_system": "roster_system", "param": "int(config_dict.get('n2n_min_gap') or 0)"}
    (ss,) = by[("same_shift", False)]
    assert ss == {**common, "roster_system": "roster_system", "param": "_ss_orig_k"}
    (iw,) = by[("isolated_work", False)]
    assert iw == {**common, "roster_system": "roster_system", "param": "None"}
    (css,) = by[("same_shift", True)]
    assert css == {"roster_system": "roster_system", "nurses": "nurses", "year": "year", "month": "month",
                   "param": "ss_orig_k"}
    (ciw,) = by[("isolated_work", True)]
    assert ciw == {"roster_system": "roster_system", "nurses": "nurses", "year": "year", "month": "month",
                   "param": "None"}


def test_rcs_carried_call_passes_final_state() -> None:
    tree, _ = _calls_in_rcs()
    calls = [c for c in ast.walk(tree) if isinstance(c, ast.Call)
             and getattr(c.func, "id", "") == "_carried_hardlock_relaxations"]
    assert len(calls) == 1
    kw = {k.arg: ast.unparse(k.value) for k in calls[0].keywords}
    assert kw == {"roster_system": "roster_system", "nurses": "nurses_for_engine", "year": "req.year",
                  "month": "req.month", "ss_orig_k": "_ss_orig_k"}
    assert ast.unparse(calls[0].args[0]) == "getattr(roster_system, 'config', None)"
