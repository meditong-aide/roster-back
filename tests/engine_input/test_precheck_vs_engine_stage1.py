"""C8' 실모델 대조 — 같은 입력을 **실제 엔진 모델**(lex stage1[hard] 첫 풀이)과 생성 경로 precheck 에 넣어 판정을 맞춘다.

왜 있는가(리뷰 지적, 독립 검토 HIGH): `tests/engine_contract/test_precheck_hard_lock_contract.py` 의 엔진 값은 EI-8
`effective_hard_lock` 이고 precheck 값도 같은 함수가 실어 준 것이라, 키 대응(예: `ban_e_to_d` ↔ `ban_n_to_e` 뒤바뀜)과 읽기
배관을 빼면 자기 자신과의 비교였다. 이 파일은 엔진이 **모델에 실제로 거는 제약**의 판정(INFEASIBLE/OPTIMAL)을 기준으로 삼는다.

방법: `CPSATBasicEngine().generate_roster(...)` 를 부르되 `fallback_lex._solve_traced` 를 stage1 첫 풀이에서 `BaseException` 으로
끊어 하드 모델의 가능/불가능만 받는다(건당 약 0.3~0.6초). DB·네트워크 접속은 없다(엔진은 메모리 입력만 쓴다 — 이 패키지의
외부 접촉 차단 가드 아래서 돈다). precheck 쪽은 운영과 같은 길을 탄다 — 엔진 형식 칸을 `views` 의 번역(`_precheck_cells_or_empty`)
으로 바꾼 뒤 `run_runtime_precheck`(10c 하드락 값 실림 포함).

비교 규칙은 한 방향이다: **엔진이 가능하면 precheck 는 막지 않는다**(오차단 금지). 엔진이 불가능한 사례는 precheck 가 막는 것을
확인하되 그 사례들은 precheck 가 알고 있는 규칙(전이 금지·연속 N·1N·용량)으로만 고른다(모르는 규칙에서 놓치는 것은 '느슨한 쪽').
"""

from __future__ import annotations

import contextlib
import copy
import functools
import io
import json
from types import SimpleNamespace

import pytest
from services.cp_sat import fallback_lex as fallback_lex_module
from services.cp_sat_basic import CPSATBasicEngine
from services.engine_input.views import _attach_stage_b, _precheck_cells_or_empty
from services.precheck import has_blocking_issues, run_runtime_precheck
from services.precheck.runtime_bridge import prepare_precheck_input
from services.precheck.team_grade_precheck import _working_capacity

from .golden import GOLDEN_DIR
from .golden_gate import GOLDEN, require_golden

SEQ = "PER_NURSE_SEQUENCE_INFEASIBLE"
ALLOWED = "FIXED_ASSIGN_VIOLATES_ALLOWED"
CAP = "CAPACITY_TOTAL_SHORTAGE"
FEASIBLE = {"OPTIMAL", "FEASIBLE"}


@pytest.fixture(autouse=True)
def _no_consecutive_work_env(monkeypatch):
    """연속근무 상한 env(`AIDE_MAX_CONSEC_WORK` — A/B 주입구)는 엔진·precheck 양쪽이 읽는다. 용량 경계 기대값(설정 값 6 → 26 등)이 환경에 흔들리지 않게 끈다."""
    monkeypatch.delenv("AIDE_MAX_CONSEC_WORK", raising=False)


class _Stop(BaseException):
    """stage1 첫 풀이 상태를 받고 엔진 실행을 끊는 표지(`Exception` 이 아니라 엔진의 광범위 except 에 안 잡힌다)."""


@functools.cache
def _template() -> dict:
    """골든 S7 의 엔진 입력(L1) 한 벌 — 간호사 1명·설정·근무코드표의 모양을 빌린다(내용은 아래에서 덮어쓴다).

    처음 쓰는 테스트에서 한 번 읽는다(모듈 import 때 읽지 않는다 — 10-20). 골든이 없는 체크아웃에서는 이 파일의 테스트가 하나씩
    D29 사유로 건너뛰고(`REQUIRE_GOLDEN=1` 이면 실패), `golden/` 의 JSON 수가 기준(16)과 다르면 그 테스트들만 실패한다.
    import 때 읽으면 같은 경우가 수집 오류가 되어 pytest 가 세션 전체를 멈춘다(Interrupted — 나머지 테스트 결과까지 가려진다).
    대상 달은 2026-11(30일, 1일이 일요일)이다 — 아래 `MONDAYS`·`WEEKEND` 가 이 달을 전제한다.
    """
    require_golden(GOLDEN)
    return json.loads((GOLDEN_DIR / "S7.json").read_text(encoding="utf-8"))["layers"]["L1"][0]


def _year_month() -> tuple[int, int]:
    template = _template()
    return template["year"], template["month"]


def _shift_manage() -> list[dict]:
    return _template()["shift_manage_data"]

#: 자동 완화 사다리 하드·월경계·OFF 상한은 끈다 — 비교 대상은 하드락·고정 칸·근무 구간·주휴·주말휴무뿐이다.
NEUTRAL = {
    "daily_shift_requirements_by_day": [], "daily_shift_requirements_max_by_day": [],
    "isolated_work_hard": False, "same_shift_hard_k": 0, "n2n_min_gap": 0, "off_first": False,
    "max_extra_off_days": 30, "weekly_off_map": {}, "initial_constraints": {}, "off_window_constraints": {},
    "prev_month_n_tail": {}, "prev_month_n_offs_after": {}, "prev_month_last_main": {}, "prev_month_last_n_k": {},
    "prev_month_off_tail": {}, "cross_month_hard_rules_enable": False, "ban_night_before_fixed_wanted_off": False,
    "ban_night_before_fixed_off": False, "fixed_wanted_use_yn": True, "two_offs_per_week": False,
    "not_one_night": False, "two_offs_after_two_nig": False, "two_offs_after_three_nig": False,
}


def config(**over) -> dict:
    cfg = copy.deepcopy(_template()["config_data"])
    cfg.update(copy.deepcopy(NEUTRAL))
    cfg["daily_shift_requirements"] = {"D": 1, "E": 1, "N": 1}
    cfg.update(over)
    return cfg


def nurse(nid: str = "N01", **over) -> dict:
    row = copy.deepcopy(_template()["nurses_data"][0])
    row.update({"nurse_id": nid, "name": nid, "sequence": 0, "experience": 0, "allowed_shifts": [],
                "is_weekend_off": False, "resignation_date": None, "joining_date": "2024-03-01T00:00:00",
                "preceptor_id": None, "personal_off_adjustment": 0})
    row.update(over)
    return row


def engine_cell(day: int, shift: str, *, source: str = "fixed_wanted", shift_type: str | None = None) -> dict:
    """엔진 형식 고정 칸(간호사 색인 0 — 단일 간호사 시나리오)."""
    return {"nurse_index": 0, "day_index": day, "shift": shift, "fixed_source": source,
            "shift_type": shift_type or ("휴무" if shift in ("O", "OFF") else "근무")}


def weekly_off_cells(days) -> list[dict]:
    return [engine_cell(day, "O", source="weekly_off", shift_type="주휴") for day in days]


#: 패치 전 원본 — 한 테스트 안에서 `engine_status` 를 여러 번 불러도 패치가 겹쳐 감싸지 않게 import 때 한 번 잡는다.
_ORIGINAL_SOLVE = fallback_lex_module._solve_traced


def engine_status(monkeypatch, nurses: list[dict], cfg: dict, cells: list[dict], shift_manage=None) -> str:
    """실제 엔진 lex stage1 첫 풀이의 상태 — OPTIMAL/FEASIBLE 이면 하드 모델이 가능, INFEASIBLE 이면 불가능.

    stage1 풀이에 닿지 못하면(엔진이 그 전에 끝났다) 단언 실패다 — '불가능과 다른' 값이 조용히 불가능으로 읽히지 않게 한다.
    """
    state: dict[str, str] = {}
    year, month = _year_month()

    def patched(solver, model, prefix, phase):
        status = _ORIGINAL_SOLVE(solver, model, prefix, phase)
        if str(phase).startswith("stage1["):
            state["status"] = fallback_lex_module._cp_sat_status_to_text(status)
            raise _Stop
        return status

    monkeypatch.setattr(fallback_lex_module, "_solve_traced", patched)
    run_cfg = copy.deepcopy(cfg)
    run_cfg["fixed_cells"] = copy.deepcopy(cells)
    with contextlib.redirect_stdout(io.StringIO()), contextlib.suppress(_Stop):
        CPSATBasicEngine().generate_roster(
            copy.deepcopy(nurses), [], run_cfg, year, month, copy.deepcopy(shift_manage or _shift_manage()),
            grade_strategy=_template()["grade_strategy"], grade_config=None, time_limit_seconds=20,
            randomize=False, seed=1)
    assert "status" in state, "엔진이 stage1 풀이에 닿지 못했다(입력이 엔진의 사전 검사에서 걸렸거나 설정이 틀렸다)"
    return state["status"]


def _precheck_cells(nurses: list[dict], cfg: dict, cells: list[dict], shift_manage=None) -> list[dict]:
    """운영과 같은 번역(`views._precheck_cells_or_empty` — 차단일 제외 포함) 결과."""
    src = {
        "nurses": [SimpleNamespace(nurse_id=n["nurse_id"], sequence=n.get("sequence", 0), experience=n.get("experience", 0))
                   for n in nurses],
        "combined_fixed_cells": copy.deepcopy(cells), "shift_manage_data": shift_manage or _shift_manage(),
    }
    return _precheck_cells_or_empty(src, cfg, lambda _msg: None)


def precheck(nurses: list[dict], cfg: dict, cells: list[dict], shift_manage=None) -> tuple[dict, set[str]]:
    year, month = _year_month()
    with contextlib.redirect_stdout(io.StringIO()):
        result = run_runtime_precheck(
            nurses_dict=copy.deepcopy(nurses), config_dict=copy.deepcopy(cfg), grade_config=None,
            fixed_cells=_precheck_cells(nurses, cfg, cells, shift_manage), year=year, month=month)
    return result, {issue["reason_code"] for issue in result["issues"]}


def precheck_input(nurses: list[dict], cfg: dict, cells: list[dict]):
    year, month = _year_month()
    with contextlib.redirect_stdout(io.StringIO()):
        return prepare_precheck_input(
            nurses_dict=copy.deepcopy(nurses), config_dict=copy.deepcopy(cfg), grade_config=None,
            fixed_cells=_precheck_cells(nurses, cfg, cells), year=year, month=month)


def assert_agree(monkeypatch, nurses, cfg, cells, *, engine_ok: bool, code: str = SEQ, shift_manage=None) -> None:
    """엔진 판정이 가능이면 precheck 가 `code` 를 내지 않고, 불가능이면 `code` 를 하드로 낸다.

    단일 간호사 시나리오라 일자별·월 용량 이슈는 늘 함께 난다 — 비교하는 것은 고정 칸·시퀀스 규칙 이슈(`code`) 하나뿐이다.
    """
    status = engine_status(monkeypatch, nurses, cfg, cells, shift_manage)
    assert (status in FEASIBLE) if engine_ok else (status == "INFEASIBLE"), f"엔진 판정이 기대와 다르다: {status}"
    _result, codes = precheck(nurses, cfg, cells, shift_manage)
    if engine_ok:
        assert code not in codes, f"오차단: 엔진은 가능({status})인데 precheck 가 {code} 로 막는다"
    else:
        assert code in codes, f"놓침: 엔진은 불가능({status})인데 precheck 가 {code} 를 내지 않는다"


# ── 하드락 값 · 전이 금지 · 연속 N · 1N — 개별 키 대응이 엔진 모델과 같다 ──────────────────────────────


def test_s1_ban_n_to_e_off_only_allows_forced_night_then_evening(monkeypatch):
    """N→E 금지만 끈 병동 — 1N 금지가 강제하는 N(5일) 다음 고정 E(6일)는 엔진 가능. 키 대응이 뒤바뀌면 precheck 가 막는다.

    (독립 리뷰 변이: effective.py 반환값의 `ban_e_to_d` 와 `ban_n_to_e` 를 뒤바꾸면 기존 테스트 1488건은 모두 초록이었다.)
    """
    cfg = config(not_one_night=True, ban_n_to_e=False, ban_e_to_d=True, ban_n_to_d=True)
    cells = [engine_cell(3, "OFF"), engine_cell(4, "N"), engine_cell(6, "E")]
    assert_agree(monkeypatch, [nurse()], cfg, cells, engine_ok=True)


def test_s1b_control_all_transition_bans_on_blocks_the_same_cells(monkeypatch):
    """(가드) 대조군 — 전이 금지 3종이 모두 켜져 있으면 같은 칸은 엔진도 불가능(N→E)이고 precheck 도 막는다."""
    cfg = config(not_one_night=True)
    cells = [engine_cell(3, "OFF"), engine_cell(4, "N"), engine_cell(6, "E")]
    assert_agree(monkeypatch, [nurse()], cfg, cells, engine_ok=False)


@pytest.mark.parametrize(("off_key", "next_shift"), [("ban_n_to_e", "E"), ("ban_n_to_d", "D")])
def test_s1c_each_transition_ban_switch_is_independent(monkeypatch, off_key, next_shift):
    """전이 금지는 개별 스위치다 — N→E 만 끄거나 N→D 만 끄면 그 전이를 가진 고정 칸은 엔진 가능, 다 켜면 불가능.

    1N 금지가 강제하는 N(5일) 바로 다음 날(6일)에 고정 E/D 를 둔다. 세 스위치 중 어느 둘을 서로 바꿔 읽어도(키 뒤바뀜)
    두 사례 중 하나가 오차단으로 빨개진다(`ban_e_to_d` 는 N→E 사례의 바뀐 짝으로 잡힌다).
    """
    cells = [engine_cell(3, "OFF"), engine_cell(4, "N"), engine_cell(6, next_shift)]
    switches = {key: key != off_key for key in ("ban_e_to_d", "ban_n_to_d", "ban_n_to_e")}
    assert_agree(monkeypatch, [nurse()], config(not_one_night=True, **switches), cells, engine_ok=True)
    assert_agree(monkeypatch, [nurse()], config(not_one_night=True), cells, engine_ok=False)


# ── 연속 N 상한(`three_seq_nig`) · 회복(2N→2OFF·3N→2OFF) ─────────────────────────────────────────────────


def _nights(*days: int) -> list[dict]:
    return [engine_cell(day, "N") for day in days]


def test_s2_three_seq_nig_allows_fixed_nnn(monkeypatch):
    """3연속 N 허용(`three_seq_nig`)이면 고정 NNN 은 엔진 가능 — precheck 가 연속 N 상한을 3 으로 읽어야 한다."""
    cfg = config(three_seq_nig=True, two_offs_after_three_nig=False)
    assert_agree(monkeypatch, [nurse()], cfg, _nights(10, 11, 12), engine_ok=True)


def test_s2b_two_night_cap_blocks_fixed_nnn(monkeypatch):
    """(가드) `three_seq_nig` 가 꺼져 있으면 연속 N 상한 2 라 같은 NNN 은 엔진 불가능 — precheck 도 막는다."""
    cfg = config(three_seq_nig=False, two_offs_after_three_nig=False)
    assert_agree(monkeypatch, [nurse()], cfg, _nights(10, 11, 12), engine_ok=False)


def test_s2c_four_nights_block_even_with_three_seq_nig(monkeypatch):
    """(가드) 4연속 N 은 어느 설정에서도 엔진 불가능 — 상한을 늘려 읽으면 놓친다."""
    cfg = config(three_seq_nig=True, two_offs_after_three_nig=False)
    assert_agree(monkeypatch, [nurse()], cfg, _nights(10, 11, 12, 13), engine_ok=False)


def test_s3_two_offs_after_two_with_three_night_run_allows_nnn_then_oo(monkeypatch):
    """2N→2OFF + 3연속 N 허용 — NNN 뒤 OO 는 엔진 가능(회복은 블록이 끝날 때 걸린다). 옛 DP 는 2개에서 끊어 막았다."""
    cfg = config(three_seq_nig=True, two_offs_after_two_nig=True)
    cells = _nights(10, 11, 12) + [engine_cell(13, "OFF"), engine_cell(14, "OFF")]
    assert_agree(monkeypatch, [nurse()], cfg, cells, engine_ok=True)


def test_s3b_recovery_blocks_a_work_day_right_after_the_night_block(monkeypatch):
    """(가드) 같은 설정에서 NNN 바로 다음 날 고정 D 는 엔진 불가능(OO 가 필요) — precheck 도 막는다."""
    cfg = config(three_seq_nig=True, two_offs_after_two_nig=True)
    assert_agree(monkeypatch, [nurse()], cfg, _nights(10, 11, 12) + [engine_cell(13, "D")], engine_ok=False)


def test_s3c_three_offs_after_three_only_binds_blocks_of_three(monkeypatch):
    """3N→2OFF — 2연속 N 뒤 고정 D 는 엔진 가능(블록 길이 2 < 3), 3연속 N 뒤 고정 D 는 불가능."""
    cfg = config(three_seq_nig=True, two_offs_after_three_nig=True, two_offs_after_two_nig=False)
    assert_agree(monkeypatch, [nurse()], cfg, _nights(10, 11) + [engine_cell(12, "D")], engine_ok=True)
    assert_agree(monkeypatch, [nurse()], cfg, _nights(10, 11, 12) + [engine_cell(13, "D")], engine_ok=False)


# ── 1N 금지와 면제 ───────────────────────────────────────────────────────────────────────────────────


def test_s4_not_one_night_blocks_an_isolated_fixed_night(monkeypatch):
    """1N 금지 — 앞뒤가 고정 O 인 단독 N 은 엔진 불가능, precheck 도 막는다."""
    cells = [engine_cell(9, "OFF"), engine_cell(10, "N"), engine_cell(11, "OFF")]
    assert_agree(monkeypatch, [nurse()], config(not_one_night=True), cells, engine_ok=False)


def test_s4b_monthly_night_cap_one_exempts_the_single_night(monkeypatch):
    """월 N 상한 1(`n_exact=1`) 간호사는 1N 금지를 엔진이 면제한다 — 같은 칸이 가능, precheck 도 막지 않는다."""
    cells = [engine_cell(9, "OFF"), engine_cell(10, "N"), engine_cell(11, "OFF")]
    assert_agree(monkeypatch, [nurse(n_exact=1)], config(not_one_night=True), cells, engine_ok=True)


def test_s4c_day_one_fixed_night_is_exempt(monkeypatch):
    """그 달 1일 고정 N 은 1N 금지를 엔진이 면제한다(fallback_lex.py:2136) — 2일이 고정 O 여도 가능."""
    cells = [engine_cell(0, "N"), engine_cell(1, "OFF")]
    assert_agree(monkeypatch, [nurse()], config(not_one_night=True), cells, engine_ok=True)


def test_s4d_a_joiners_first_day_is_not_exempt(monkeypatch):
    """11/10 입사자의 첫날(입사일) 고정 N 은 면제가 아니다 — 앞날에 N 을 이을 수 없어 엔진 불가능. precheck 도 막는다.

    (`join_day == 0` 조건을 지우는 변이를 엔진 모델로 직접 잡는다 — 리뷰 지적 '검증 공백'.)
    """
    cells = [engine_cell(9, "N"), engine_cell(10, "OFF")]
    joiner = nurse(joining_date="2026-11-10T00:00:00")
    assert_agree(monkeypatch, [joiner], config(not_one_night=True), cells, engine_ok=False)


# ── 배정 차단일 위 고정 칸 · 근무 구간 밖 고정 칸 — 엔진은 활동일 밖 칸을 건너뛴다 ───────────────────────────


def test_s5_fixed_day_on_a_blocked_day_is_skipped_by_engine_and_precheck(monkeypatch):
    """E1a — N 전담 + 차단일 {1,2,3} 위 확정 원티드 D(2일): 엔진 가능. 차단일이 없으면(대조군) 엔진도 불가능이다."""
    night_only = nurse(allowed_shifts=["N"])
    cells = [engine_cell(2, "D")]
    assert_agree(monkeypatch, [night_only], config(blocked_by_nurse_id={"N01": {1, 2, 3}}), cells,
                 engine_ok=True, code=ALLOWED)
    assert_agree(monkeypatch, [night_only], config(), cells, engine_ok=False, code=ALLOWED)


def test_s5b_isolated_night_on_blocked_days_is_skipped(monkeypatch):
    """E1b — 1N 금지, 차단일 {9,10,11} 위 O·N·O 고정: 엔진 가능. 차단일이 없으면 단독 N 이라 불가능이다."""
    cells = [engine_cell(9, "OFF"), engine_cell(10, "N"), engine_cell(11, "OFF")]
    blocked = config(not_one_night=True, blocked_by_nurse_id={"N01": {9, 10, 11}})
    assert_agree(monkeypatch, [nurse()], blocked, cells, engine_ok=True)
    assert_agree(monkeypatch, [nurse()], config(not_one_night=True), cells, engine_ok=False)


def test_s5c_one_blocked_day_inside_a_night_run_breaks_it(monkeypatch):
    """E1c — 연속 N 상한 3 에서 NNNN(10~13일) 중 12일이 차단일이면 엔진은 그 칸을 건너뛰어 가능하다(대조군은 불가능)."""
    cfg = config(three_seq_nig=True, two_offs_after_three_nig=False)
    cells = _nights(10, 11, 12, 13)
    assert_agree(monkeypatch, [nurse()], {**cfg, "blocked_by_nurse_id": {"N01": {12}}}, cells, engine_ok=True)
    assert_agree(monkeypatch, [nurse()], cfg, cells, engine_ok=False)


def test_s6_fixed_cell_after_the_resignation_span_is_skipped(monkeypatch):
    """11/25 퇴사자(구간 0~23)의 11/27(26일) 고정 N(허용 D·E 밖) — 엔진은 활동일 밖 칸을 건너뛰어 가능. 구간 안 같은 칸은 불가능."""
    resigner = nurse(allowed_shifts=["D", "E"], resignation_date="2026-11-25T00:00:00")
    assert_agree(monkeypatch, [resigner], config(), [engine_cell(26, "N")], engine_ok=True, code=ALLOWED)
    assert_agree(monkeypatch, [resigner], config(), [engine_cell(19, "N")], engine_ok=False, code=ALLOWED)


# ── 용량 — precheck 의 한 달 최대 근무일이 엔진 모델의 최대 근무일과 같다(d_exact 경계) ─────────────────────


MONDAYS = [1, 8, 15, 22, 29]
WEEKEND = {0, 6, 7, 13, 14, 20, 21, 27, 28}  # 2026-11 토·일(1일이 일요일)


def _capacity_config(work_days, **over) -> dict:
    """D 전담 한 명의 용량 시나리오 설정 — D 요구를 `work_days` 에만 둔다(엔진이 그 날들에 근무하도록 모양을 고정한다).

    OFF 하한(min_off)은 off_first=False 병동에서만 엔진이 하드로 거는 별개 제약이라(precheck 는 일부러 뺀다) 여기서는 끈다.
    """
    days = set(work_days)
    need = [{"D": 1 if day in days else 0, "E": 0, "N": 0} for day in range(30)]
    return {"off_first": True, "max_conseq_work": 6, "daily_shift_requirements": {"D": 1, "E": 0, "N": 0},
            "daily_shift_requirements_by_day": need, **over}


def _assert_capacity_matches_engine(monkeypatch, cfg_kwargs, nurse_kwargs, cells, expected: int) -> None:
    """precheck 용량이 `expected` 이고, 엔진은 정확히 그만큼까지 근무 가능(`d_exact`=expected 가능 · +1 불가능)하다."""
    def status(days: int) -> str:
        return engine_status(monkeypatch, [nurse(allowed_shifts=["D"], d_exact=days, **nurse_kwargs)],
                             config(**cfg_kwargs), cells)

    inp = precheck_input([nurse(allowed_shifts=["D"], **nurse_kwargs)], config(**cfg_kwargs), cells)
    assert _working_capacity(inp.nurses[0], inp.roster_config) == expected
    assert status(expected) in FEASIBLE, f"엔진이 {expected}일 근무를 못 한다"
    assert status(expected + 1) == "INFEASIBLE", f"엔진이 {expected + 1}일 근무를 한다 — precheck 용량이 엔진보다 작다"


@pytest.mark.parametrize("personal_adjustment", [0, 2])
def test_s7_weekly_off_capacity_does_not_depend_on_the_engine_ignored_adjustment(monkeypatch, personal_adjustment):
    """월요일 주휴 5일 + 이월 보정 p(엔진이 안 쓴다) — 엔진 최대 근무일은 25 이고 precheck 용량도 p 와 무관하게 25 다.

    (전에는 p=2 면 p 와 주휴 5 를 더한 7 을 빼 23 이었다 — 엔진이 푸는 달을 막는 리뷰 HIGH.)
    """
    cfg = _capacity_config(day for day in range(30) if day not in MONDAYS)
    cfg["weekly_off_map"] = {"N01": MONDAYS}
    _assert_capacity_matches_engine(
        monkeypatch, cfg, {"personal_off_adjustment": personal_adjustment}, weekly_off_cells(MONDAYS), 25)


def test_s7b_weekend_off_resigner_capacity_is_the_weekdays_inside_the_span(monkeypatch):
    """주말휴무자가 11/20 퇴사(구간 0~18) — 엔진은 평일 근무·주말 OFF 를 강제해 구간 안 평일 14일이 정확한 근무일이다.

    precheck 용량도 14(구간 19일 − 구간 안 주말 5일)다. 전에는 그 달 주말 9일을 빼 10 이었다(리뷰 HIGH).
    """
    weekdays = [day for day in range(19) if day not in WEEKEND]
    assert len(weekdays) == 14
    nurse_kwargs = {"is_weekend_off": True, "resignation_date": "2026-11-20T00:00:00"}
    # 주말휴무자는 엔진 OFF 하한(min_off)에서 빠지므로 off_first=False(엔진 기본)로 돌린다 — 독립 리뷰가 잰 설정과 같다.
    _assert_capacity_matches_engine(monkeypatch, _capacity_config(weekdays, off_first=False), nurse_kwargs, [], 14)


def test_s7c_weekend_off_flag_decides_whether_weekends_are_forced_off(monkeypatch):
    """`weekend_off_only_enable` — 꺼지면 엔진은 주말휴무자의 주말 OFF 를 강제하지 않아 최대 근무일이 연속근무 상한 닫힌형(26)이고,
    켜지면(엔진 기본) 평일 근무·주말 OFF 라 21 이다. precheck 용량이 두 경우 모두 같다.

    전에는 플래그와 무관하게 주말 9일을 빼 항상 21 이었다(리뷰 MEDIUM).
    """
    every_seventh_off = [day for day in range(30) if day % 7 != 6]  # 4일 쉬고 26일 근무하는 모양
    assert len(every_seventh_off) == 26
    weekdays = [day for day in range(30) if day not in WEEKEND]
    _assert_capacity_matches_engine(
        monkeypatch, _capacity_config(every_seventh_off, weekend_off_only_enable=False), {"is_weekend_off": True}, [], 26)
    _assert_capacity_matches_engine(
        monkeypatch, _capacity_config(weekdays, weekend_off_only_enable=True), {"is_weekend_off": True}, [], 21)


def test_s7d_consecutive_work_cap_reaches_precheck_capacity(monkeypatch):
    """연속근무 상한 4 — 엔진 최대 근무일은 30 − 30//5 = 24 이고 precheck 용량도 24 다(`max_conseq_work` 가 실린다)."""
    cfg = _capacity_config((day for day in range(30) if day % 5 != 4), max_conseq_work=4)
    _assert_capacity_matches_engine(monkeypatch, cfg, {}, [], 24)


def test_s7e_off_hard_floor_min_off_reaches_precheck_capacity(monkeypatch):
    """off_first=False · off_days=8 — 엔진은 OFF 하드 하한(min_off 8)을 걸어 최대 근무일이 22 이고(23 불가능) precheck 용량도 22 다.

    (EI-14r3 — 전에는 precheck 가 이 하한을 빼 연속근무 닫힌형 26 이었다. 엔진 22 ≤ precheck 26 은 오차단은 아니나 놓침이었다.)
    """
    cfg = _capacity_config(range(30), off_first=False, off_days=8)
    _assert_capacity_matches_engine(monkeypatch, cfg, {}, [], 22)


def test_s7e2_weekly_off_days_are_inside_the_min_off_not_added_on_top(monkeypatch):
    """월요일 주휴 5일 + off_days=8 — 엔진 하드 O 5일은 OFF 8 안에 들어가므로 최대 근무일은 22(= 30−8)이고 precheck 도 22 다
    (바닥 `max(5, 8)` — 5+8=13 으로 이중 감산하면 17 로 엔진이 푸는 달을 막는다)."""
    cfg = _capacity_config((day for day in range(30) if day not in MONDAYS), off_first=False, off_days=8)
    cfg["weekly_off_map"] = {"N01": MONDAYS}
    _assert_capacity_matches_engine(monkeypatch, cfg, {}, weekly_off_cells(MONDAYS), 22)


# ── MID(M) 전이 — 개인 DP 가 엔진의 mid_transition·mid_to_day_ban 을 따른다(EI-14r2) ─────────────────────────────────


def _mid_shift_manage() -> list[dict]:
    """근무코드표에 M(MID) 행을 더한 사본 — MID 병동."""
    shift_manage = _shift_manage()
    return copy.deepcopy(shift_manage) + [{**copy.deepcopy(shift_manage[0]), "id": 99, "codes": ["M"], "main_code": "M",
                                           "shift_slot": 9, "manpower": 1}]


def _mid_config() -> dict:
    return config(use_mid=True, daily_shift_requirements={"D": 1, "E": 1, "N": 1, "M": 1})


@pytest.mark.parametrize("before", ["N", "E"])
def test_s8_fixed_night_or_evening_then_fixed_mid_is_blocked_and_infeasible_for_engine(monkeypatch, before):
    """MID 병동에서 고정 N→고정 M · 고정 E→고정 M 은 엔진 mid_transition(M 은 전날이 D·O·M 일 때만, 고정 면제 없음)으로
    INFEASIBLE 이고 개인 DP 도 같은 전이를 막는다(EI-14r2 — 전에는 DP 가 M 을 몰라 통과 = 알려진 놓침이었다)."""
    cells = [engine_cell(4, before), engine_cell(5, "M")]
    assert_agree(monkeypatch, [nurse()], _mid_config(), cells, engine_ok=False, shift_manage=_mid_shift_manage())


@pytest.mark.parametrize(("cells", "engine_ok"), [
    ([engine_cell(4, "D"), engine_cell(5, "M")], True),    # D→M 정상
    ([engine_cell(4, "M"), engine_cell(5, "D")], True),    # M→D 는 양끝 고정이면 엔진이 면제한다
    ([engine_cell(4, "M"), engine_cell(5, "M")], True),    # M→M 허용(2026-10-12)
])
def test_s8b_mid_cells_the_engine_accepts_are_not_blocked(monkeypatch, cells, engine_ok):
    """(가드) 엔진이 받아들이는 MID 고정 칸(D→M · M→M · 양끝 고정 M→D)은 precheck 도 막지 않는다 — 오차단 없음."""
    assert_agree(monkeypatch, [nurse()], _mid_config(), cells, engine_ok=engine_ok, shift_manage=_mid_shift_manage())


def test_s8c_fixed_mid_then_free_day_is_not_blocked(monkeypatch):
    """(가드) 고정 M 다음 날이 자유 칸이면 DP 가 그날을 OFF 로 보낼 수 있다 — M→D 금지가 자유 칸을 막지 않는다."""
    assert_agree(monkeypatch, [nurse()], _mid_config(), [engine_cell(4, "M")], engine_ok=True,
                 shift_manage=_mid_shift_manage())


def test_s8d_non_mid_ward_ignores_mid_rules():
    """use_mid 가 아닌 병동은 M 규칙의 영향이 0 이다 — 같은 DP 호출이 use_mid 기본값과 같다."""
    from services.precheck.per_nurse_sequence_feasibility import nurse_sequence_infeasible

    kwargs = {"num_days": 30, "allowed": {"D", "E", "N"}, "fixed": {4: "N", 5: "D"}, "ban_n_to_d": False}
    assert nurse_sequence_infeasible(**kwargs) is nurse_sequence_infeasible(**kwargs, use_mid=False)


# ── 배정 차단일(blocked_by_nurse_id) — 용량·개인 DP 가 엔진과 같다(EI-10g1) ─────────────────────────────────────────


def _blocked(*ranges: range) -> set[int]:
    return {day for r in ranges for day in r}


def test_s10_blocked_days_reduce_capacity_exactly_like_the_engine(monkeypatch):
    """12일(5~16일) 차단 — 엔진은 그 날들에 근무 변수가 없어 최대 근무일이 18 이고(19 불가능) precheck 용량도 18 이다."""
    blocked = _blocked(range(5, 17))
    cfg = _capacity_config((day for day in range(30) if day not in blocked), max_conseq_work=30,
                           blocked_by_nurse_id={"N01": blocked})
    _assert_capacity_matches_engine(monkeypatch, cfg, {}, [], 18)


def test_s10b_blocked_days_with_min_off_floor_use_the_engine_avail_days(monkeypatch):
    """off_first=False · off_days=8 · 12일 차단 — 엔진 OFF 하한은 `avail_days = 30 − 12` 기준이다. precheck 용량이 엔진 최대 근무일과 같다
    (차단일을 안 빼고 min_off 를 구하면 엔진보다 커져 오차단이다)."""
    blocked = _blocked(range(5, 17))
    cfg = _capacity_config((day for day in range(30) if day not in blocked), off_first=False, off_days=8,
                           max_conseq_work=30, blocked_by_nurse_id={"N01": blocked})
    inp = precheck_input([nurse(allowed_shifts=["D"])], config(**cfg), [])
    expected = _working_capacity(inp.nurses[0], inp.roster_config)
    assert 0 < expected <= 18
    _assert_capacity_matches_engine(monkeypatch, cfg, {}, [], expected)


def test_s10c_blocked_day_that_is_also_a_weekend_is_counted_once(monkeypatch):
    """주말휴무자 + 차단일이 주말과 겹침 — 주말 OFF 와 차단일을 두 번 빼면 오차단이다. 엔진 최대 근무일과 precheck 용량이 같다."""
    blocked = {6, 7, 13, 14}  # 2026-11 의 주말 일부
    weekdays = [day for day in range(30) if day not in WEEKEND and day not in blocked]
    cfg = _capacity_config(weekdays, off_first=False, blocked_by_nurse_id={"N01": blocked})
    inp = precheck_input([nurse(allowed_shifts=["D"], is_weekend_off=True)], config(**cfg), [])
    expected = _working_capacity(inp.nurses[0], inp.roster_config)
    _assert_capacity_matches_engine(monkeypatch, cfg, {"is_weekend_off": True}, [], expected)
    assert expected == len(weekdays)


def test_s10d_blocked_day_inside_a_recovery_run_is_not_blocked_by_precheck(monkeypatch):
    """2N→2OFF 병동 — 고정 NN(10·11일) 중 11일이 차단일이면 그 칸은 버려져 N 은 10일 하나다. 엔진 가능이고 개인 DP 도 막지 않는다.

    (측정: 차단일이 N 블록 **바로 뒤**(12일)면 엔진이 오히려 불가능이다 — 회복 OFF 칸 변수가 없어서다. precheck 는 그 경우를 못 막는다 = 느슨한
    쪽이라 오차단이 아니다.)
    """
    cfg = config(two_offs_after_two_nig=True, three_seq_nig=False, blocked_by_nurse_id={"N01": {11}})
    assert_agree(monkeypatch, [nurse()], cfg, _nights(10, 11), engine_ok=True)


def test_s10e_blocked_days_next_to_an_isolated_night_do_not_make_precheck_stricter(monkeypatch):
    """1N 금지 · 차단일 {11} 옆 고정 N(10일) — 엔진 가능 여부와 상관없이 precheck 는 엔진이 가능한 모든 모양을 통과시킨다(오차단 0)."""
    cfg = config(not_one_night=True, blocked_by_nurse_id={"N01": {11}})
    for cells in (_nights(10), _nights(10, 12), _nights(9, 10)):
        status = engine_status(monkeypatch, [nurse()], cfg, cells)
        _result, codes = precheck([nurse()], cfg, cells)
        if status in FEASIBLE:
            assert SEQ not in codes, f"오차단: 엔진은 가능({status})인데 precheck 가 막는다 — {[c['day_index'] for c in cells]}"


def test_s10f_blocked_days_make_a_daily_shortage_precheck_now_reports(monkeypatch):
    """X-BLK-1 — D 전담 3명 · D 수요 3 · 한 명이 12일 차단 — 차단일엔 가용 2명이라 일자 용량 부족을 낸다(D28 새로 막히는 조건).

    엔진은 이 달을 **미달 근무표(슬랙)** 로 푼다(stage1 OPTIMAL) — 인원이 2명인 병동이 이미 precheck 에 막히는 것과 같은 판정이다
    (오차단이 아니라 '차단일만큼 인원이 적다'는 사실을 반영). 차단일이 없으면(X-BLK-2) 막지 않는다.
    """
    nurses = [nurse(nid, allowed_shifts=["D"]) for nid in ("N01", "N02", "N03")]
    cfg = config(off_first=True, max_conseq_work=30, daily_shift_requirements={"D": 3, "E": 0, "N": 0})
    blocked_cfg = {**cfg, "blocked_by_nurse_id": {"N01": _blocked(range(5, 17))}}
    result, codes = precheck(nurses, blocked_cfg, [])
    day_issues = [i for i in result["issues"] if i["reason_code"] == "GLOBAL_DAY_CAPACITY_SHORTAGE"]
    assert {i["evidence"]["day"] for i in day_issues} == set(range(5, 17)), codes
    # 같은 부족을 인원 2명으로 만든 병동도 같은 이유로 막힌다(대조군)
    result2, _ = precheck(nurses[1:], cfg, [])
    assert any(i["reason_code"] == "GLOBAL_DAY_CAPACITY_SHORTAGE" for i in result2["issues"])
    assert engine_status(monkeypatch, nurses, blocked_cfg, []) in FEASIBLE
    _result, codes = precheck(nurses, cfg, [])
    assert "GLOBAL_DAY_CAPACITY_SHORTAGE" not in codes, codes


def test_s10g_cut_demand_is_what_the_engine_nurses_must_fill(monkeypatch):
    """X-CUT-1 (EI-10g2) — 고정근무자 1명(엔진 밖) + 엔진 간호사 3명, D 수요 4 → 고정근무자 몫을 뺀 3 이 엔진이 채울 수요다.

    차감 전 수요(4)를 precheck 에 주면 '인원 부족'으로 막는다(전 차단) — 차감 뒤 수요(3)를 주면 통과하고 엔진은 가능이다(후 통과).
    """
    nurses = [nurse(nid, allowed_shifts=["D"]) for nid in ("N01", "N02", "N03")]
    base = dict(off_first=True, max_conseq_work=30)
    pre_cut = config(**base, daily_shift_requirements={"D": 4, "E": 0, "N": 0})
    post_cut = config(**base, daily_shift_requirements={"D": 3, "E": 0, "N": 0})
    _result, before = precheck(nurses, pre_cut, [])
    _result, after = precheck(nurses, post_cut, [])
    assert "GLOBAL_DAY_CAPACITY_SHORTAGE" in before
    assert "GLOBAL_DAY_CAPACITY_SHORTAGE" not in after
    assert engine_status(monkeypatch, nurses, post_cut, []) in FEASIBLE


# ── 단계 B 제약(forced_off·forbidden — 금지 원티드·허용근무·전월 경계) — 개인 DP 가 엔진과 같다(EI-10g4) ───────────────────


def _ic(forced_off=None, forbidden=None) -> dict:
    """`config_dict["initial_constraints"]` — 엔진이 읽는 모양({forced_off: {id: [일]}, forbidden: {id: {일: [코드]}}})."""
    return {"initial_constraints": {"forced_off": forced_off or {}, "forbidden": forbidden or {}}}


def test_s11_banned_neighbours_make_a_forced_single_night_impossible(monkeypatch):
    """1N 금지 · 고정 N(10일) · 양옆(9·11일) N 금지(금지 원티드) — 단독 N 이라 엔진 불가능이고 개인 DP 도 막는다."""
    cfg = config(not_one_night=True, **_ic(forbidden={"N01": {9: ["N"], 11: ["N"]}}))
    assert_agree(monkeypatch, [nurse()], cfg, _nights(10), engine_ok=False)
    # 대조군 — 금지가 한쪽뿐이면 반대편 N 으로 이어 갈 수 있다(엔진 가능 · precheck 도 통과)
    cfg1 = config(not_one_night=True, **_ic(forbidden={"N01": {9: ["N"]}}))
    assert_agree(monkeypatch, [nurse()], cfg1, _nights(10), engine_ok=True)


def test_s11b_forced_off_neighbours_make_a_forced_single_night_impossible(monkeypatch):
    """1N 금지 · 고정 N(10일) · 양옆(9·11일) 강제 OFF(전월 경계·허용근무 쉬는 날) — 엔진 불가능, 개인 DP 도 막는다."""
    cfg = config(not_one_night=True, **_ic(forced_off={"N01": [9, 11]}))
    assert_agree(monkeypatch, [nurse()], cfg, _nights(10), engine_ok=False)


def test_s11c_fixed_cell_beats_forbidden_on_the_same_day(monkeypatch):
    """고정 N(10일)이 같은 날 N 금지와 겹침 — 고정이 이겨 엔진 가능(오차단 금지). 1N 금지라 11일이 N 으로 이어진다."""
    cfg = config(not_one_night=True, **_ic(forbidden={"N01": {10: ["N"]}}))
    assert_agree(monkeypatch, [nurse()], cfg, _nights(10), engine_ok=True)


def test_s11d_fixed_work_beats_forced_off_on_the_same_day(monkeypatch):
    """고정 D(10일)이 같은 날 강제 OFF 와 겹침 — 엔진은 강제 OFF 를 무시한다(유저 고정 우선). 엔진 가능이고 개인 DP 도 막지 않는다."""
    cfg = config(**_ic(forced_off={"N01": [10]}))
    assert_agree(monkeypatch, [nurse()], cfg, [engine_cell(10, "D")], engine_ok=True)


def test_s11e_forbidden_off_inside_a_recovery_window(monkeypatch):
    """2N→2OFF · 고정 NN(10·11일) 뒤 12일에 OFF 금지(O 가 금지 코드) — 엔진 불가능 여부를 그대로 따라간다(오차단 금지)."""
    cfg = config(two_offs_after_two_nig=True, three_seq_nig=False, **_ic(forbidden={"N01": {12: ["O"]}}))
    cells = _nights(10, 11)
    status = engine_status(monkeypatch, [nurse()], cfg, cells)
    _result, codes = precheck([nurse()], cfg, cells)
    if status in FEASIBLE:
        assert SEQ not in codes, f"오차단: 엔진은 가능({status})인데 precheck 가 막는다"
    # 놓침은 허용(느슨한 쪽)이지만 측정값을 남긴다 — 엔진이 불가능이면 개인 DP 도 같은 판정이어야 한다
    elif status == "INFEASIBLE":
        assert SEQ in codes


def test_s11f_empty_or_malformed_constraints_leave_the_verdict_alone(monkeypatch):
    """빈 initial_constraints · 모양이 틀린 값 — 판정이 그대로다(키 없음과 같다)."""
    cells = [engine_cell(3, "OFF"), engine_cell(4, "N"), engine_cell(6, "E")]
    base = config(not_one_night=True, ban_n_to_e=False, ban_e_to_d=True, ban_n_to_d=True)
    for extra in ({}, _ic(), {"initial_constraints": "garbage"}, {"initial_constraints": {"forbidden": {"N01": {"x": ["N"]}}}}):
        _result, codes = precheck([nurse()], {**base, **extra}, cells)
        assert SEQ not in codes, extra


def test_s11g_constraints_outside_the_active_span_or_on_preceptees_are_ignored(monkeypatch):
    """퇴사로 구간이 0~18 인 간호사 — 구간 밖(25일) 강제 OFF·금지는 무시한다(엔진은 활동일 밖 칸을 건너뛴다)."""
    resigner = nurse(resignation_date="2026-11-20T00:00:00")
    cfg = config(not_one_night=True, **_ic(forced_off={"N01": [24, 26]}, forbidden={"N01": {24: ["N"], 26: ["N"]}}))
    assert_agree(monkeypatch, [resigner], cfg, _nights(25), engine_ok=True)


# ── 단계 B 제약(forced_off·forbidden)을 일자·월 용량이 소비 — 엔진이 쓸 수 있는 날 수보다 크지 않다(EI-10g5) ─────────────


def test_s12_forced_off_days_reduce_capacity_exactly_like_the_engine(monkeypatch):
    """D 전담 한 명 · 강제 OFF 6일(5~10일) — 엔진은 그 날들을 O 로 박아 최대 근무일이 24 이고(25 불가능) precheck 용량도 24 다."""
    forced = list(range(5, 11))
    work_days = [day for day in range(30) if day not in forced]
    cfg = _capacity_config(work_days, max_conseq_work=30, **_ic(forced_off={"N01": forced}))
    _assert_capacity_matches_engine(monkeypatch, cfg, {}, [], 24)


def test_s12b_forbidden_days_never_make_the_night_availability_smaller_than_what_the_engine_can_do(monkeypatch):
    """N 전담 · N 금지 8일(금지 원티드) — precheck '야간 가능일'(활동일 − 금지일)은 엔진이 실제로 설 수 있는 N 수 이상이다(오차단 0).
    엔진의 최대 N 은 연속 N·회복 규칙이 더 깎으므로 같거나 작다."""
    banned = list(range(3, 11))
    cfg = config(max_nig_per_month=30, **_ic(forbidden={"N01": {d: ["N"] for d in banned}}))
    nurses = [nurse(allowed_shifts=["N"], n_exact=0)]
    inp = precheck_input(nurses, cfg, [])
    from services.precheck.team_grade_precheck import _apply_shifts, _shift_days_available

    available = _shift_days_available(inp.nurses[0], "N", _apply_shifts(False))
    assert available == 30 - len(banned)

    def status(k: int) -> str:
        return engine_status(monkeypatch, [nurse(allowed_shifts=["N"], n_exact=k)], cfg, [])

    assert status(available + 1) == "INFEASIBLE", "엔진이 금지일 밖 날 수보다 많은 N 을 선다 — precheck 가용일이 엔진보다 작다(오차단)"
    assert status(1) in FEASIBLE


def test_s12c_forbidden_shift_on_a_day_is_a_day_shortage_but_a_fixed_cell_wins(monkeypatch):
    """D 전담 3명 · D 수요 3 · 한 명이 7일에 D 금지 — 그날 D 가용 2 < 3 이라 일자 부족을 낸다(D28 새로 막히는 조건). 같은 날 그 간호사에게
    고정 D 칸이 있으면 금지는 무시된다(고정이 이긴다) — 부족이 없다."""
    nurses = [nurse(nid, allowed_shifts=["D"]) for nid in ("N01", "N02", "N03")]
    base = dict(off_first=True, max_conseq_work=30, daily_shift_requirements={"D": 3, "E": 0, "N": 0})
    cfg = config(**base, **_ic(forbidden={"N01": {7: ["D"]}}))
    result, codes = precheck(nurses, cfg, [])
    shortage_days = {i["evidence"]["day"] for i in result["issues"] if i["reason_code"] == "GLOBAL_SHIFT_ALLOWED_SHORTAGE"}
    assert shortage_days == {7 + 1}, shortage_days   # 이 이슈의 day 는 1-based
    _result, codes = precheck(nurses, cfg, [engine_cell(7, "D")])
    assert "GLOBAL_SHIFT_ALLOWED_SHORTAGE" not in codes
    # 엔진은 둘 다 가능(수요는 슬랙) — 고정 칸이 금지를 이기는 것을 엔진도 확인한다
    assert engine_status(monkeypatch, nurses, cfg, [engine_cell(7, "D")]) in FEASIBLE


def test_s12d_forced_off_day_is_a_day_capacity_shortage(monkeypatch):
    """D 전담 3명 · D 수요 3 · 한 명이 9일 강제 OFF(전월 경계·허용근무 쉬는 날) — 그날 가용 2 < 3 → 일자 용량 부족."""
    nurses = [nurse(nid, allowed_shifts=["D"]) for nid in ("N01", "N02", "N03")]
    base = dict(off_first=True, max_conseq_work=30, daily_shift_requirements={"D": 3, "E": 0, "N": 0})
    result, codes = precheck(nurses, config(**base, **_ic(forced_off={"N01": [9]})), [])
    days = {i["evidence"]["day"] for i in result["issues"] if i["reason_code"] == "GLOBAL_DAY_CAPACITY_SHORTAGE"}
    assert days == {9}, days
    # 대조군 — 제약이 없으면 부족 없음
    _result, codes = precheck(nurses, config(**base), [])
    assert "GLOBAL_DAY_CAPACITY_SHORTAGE" not in codes


def test_s12e_mid_month_forced_off_under_a_fixed_cell_precheck_skips_is_not_a_day_shortage(monkeypatch):
    """12y(Codex 4차 P2) — 월중 경계 강제 OFF(9일)가 config 에 이미 있고 같은 날 precheck 가 번역하지 않는 고정 근무 칸(W)이 있다.

    엔진은 고정 칸이 이겨 그 강제 OFF 를 무시하고 W 는 버린다 — 9일에도 D 를 선다(30일 근무 가능 · 고정 칸이 없으면 30일은 불가능).
    운영 경로(`views._attach_stage_b` — 병합본은 같은 config 로 지어 월중 몫을 담는다)에서 고정 칸 걸름이 병합본을 전부 지우면
    precheck 입력의 걸러지기 전 강제 OFF 도 지워져야 한다 — 남으면 9일이 거짓 일자 부족(GLOBAL_DAY_CAPACITY_SHORTAGE)으로 막힌다.
    """
    cfg = config(**_capacity_config(range(30), max_conseq_work=30, **_ic(forced_off={"N01": [9]})))
    cells = [engine_cell(9, "W")]
    full = [nurse(allowed_shifts=["D"], d_exact=30)]
    assert engine_status(monkeypatch, full, cfg, cells) in FEASIBLE
    assert engine_status(monkeypatch, full, cfg, []) == "INFEASIBLE"   # 대조군 — 고정 칸이 없으면 강제 OFF 가 걸린다
    pre_cfg = dict(cfg)
    ei = SimpleNamespace(load_report=SimpleNamespace(errors={}),
                         cells={"initial_constraints": copy.deepcopy(cfg["initial_constraints"]), "combined_fixed_cells": cells})
    _attach_stage_b(pre_cfg, ei, {"nurses": [SimpleNamespace(nurse_id="N01", sequence=0, experience=0)]}, lambda _msg: None)
    result, codes = precheck(full, pre_cfg, cells)
    assert "GLOBAL_DAY_CAPACITY_SHORTAGE" not in codes, [(i["reason_code"], i.get("evidence")) for i in result["issues"]]
    assert not has_blocking_issues(result), codes   # 엔진이 받는 입력 — precheck 는 막지 않는다
    assert cfg["initial_constraints"] == {"forced_off": {"N01": [9]}, "forbidden": {}}   # 엔진 입력(원 config)은 그대로
