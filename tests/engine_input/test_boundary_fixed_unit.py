"""경계 하드락 단위 시험(EI-11 11w).

11w-1 — 1일 복귀(전월 말일에 끝난 파견) 배정 나누기 · 내부 값 요청 400.
  `roster_create_service._assignments_with_day1_returns` 는 `assignment_service.get_active_assignments_for_month` 의 조건을 복제해
  실효 종료일만 하루 넓혀 **한 번에** 조회한다(assignment_service 쓰기 금지 · 따로 조회하면 골든 쿼리 순서가 깨진다 — 지시서
  `ei11/w11/W11_SPEC.md` §2). 두 조건이 갈라지면 생성 명단·차단일이 바뀌므로, 같은 시드에서 '이번 달 배정'이 원 함수와 행·순서까지
  같은지와 '1일 복귀'가 정확히 그 배정만인지 본다. 시드만 넣고 생성 서비스는 부르지 않는다(바깥 트랜잭션째 롤백).
11w-2 — 출발 경계 순수 함수(`engine_input.boundary_fixed`) · 월중 경계 목록(조회 없이) · 생성 결과 안내 묶기(D38). 기대값은 손으로
  적었다(생성 코드를 쓰지 않는다).
11w-n — 안내 묶음은 성공 응답 `boundary_notices` 칸에 실린다(D39 · 주휴 경고 칸과 분리 — 응답 쪽 시험은 `test_boundary_notice.py`).
"""

from __future__ import annotations

import ast
import contextlib
import textwrap
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from .boundary_cases import BoundaryCase, d, n07_out, return_day1, scenario_for
from .scenario_defs import SCENARIOS, _assignment
from .ward import GROUP_ID, OTHER_GROUP_ID


def _split_edges() -> BoundaryCase:
    """나누기 경계 재료(G1 기준 · 11월 생성)."""
    g1, g2 = GROUP_ID, OTHER_GROUP_ID
    rows = [
        n07_out(d(10, 20), d(10, 31), status="completed"),                                  # 1일 복귀(예상 종료일 · completed)
        _assignment("N03", g1, g2, d(10, 25), d(11, 5), "파견", end_date=d(10, 31)),          # 1일 복귀(실제 종료일이 우선)
        _assignment("N04", g1, g2, d(10, 25), d(10, 31), "파견"),                             # 1일 복귀(active)
        _assignment("N05", g1, g2, d(10, 20), d(10, 30), "파견"),                             # 전월 말일 전 종료 → 조회 밖
        _assignment("N05", g1, g2, d(10, 1), d(10, 31), "파견", end_date=d(10, 30)),          # 실제 종료일이 앞섬 → 조회 밖
        _assignment("X01", g2, g1, d(10, 20), d(10, 31), "파견"),                             # 이 병동이 target → 1일 복귀 아님
        _assignment("N06", g1, g2, d(10, 1), d(10, 31), "병동이동"),                           # 병동이동 → 1일 복귀 아님
        _assignment("N08", g1, g2, d(10, 20), d(10, 31), "파견", status="cancelled"),         # 취소 → 조회 밖
        _assignment("N09", g1, None, d(10, 1), d(10, 31), "휴직"),                            # 휴직 → 1일 복귀 아님
        _assignment("N10", g1, g2, d(10, 25), d(11, 3), "파견"),                              # 이번 달(월 넘김)
        _assignment("N02", g1, g2, d(11, 10), None, "파견"),                                  # 이번 달(종료일 없음)
        _assignment("N01", g1, g2, d(12, 1), d(12, 5), "파견"),                               # 다음 달 시작 → 조회 밖
        _assignment("X01", g2, g1, d(11, 20), d(11, 25), "파견"),                             # 이번 달(파견 in)
    ]
    return BoundaryCase(name="split-edges", focus="N07", boundaries=(), assignments=rows)


#: 이름 → (시나리오, 이번 달 배정 수, 1일 복귀 간호사). S6 재료(이번 달 배정 5 · 1일 복귀 없음) · 1일 복귀 재료 · 경계 재료.
MATERIALS = {
    "S6": (SCENARIOS["S6"], 5, []),
    "ret1": (scenario_for(return_day1("OOOONN", "Y")), 0, ["N07"]),
    "edges": (scenario_for(_split_edges()), 3, ["N03", "N04", "N07"]),
}


@contextlib.contextmanager
def _seeded(engine, scenario):
    """시나리오 시드만 넣은 세션 — 끝나면 바깥 트랜잭션째 롤백한다(`test_dual_build` 의 '생성 앞' 스냅숏과 같은 방식)."""
    conn = engine.connect()
    outer = conn.begin()
    try:
        conn.exec_driver_sql("DELETE FROM roster_config WHERE 1 = 0")   # pysqlite 가 DDL 앞에서 BEGIN 을 내게 한다(harness 와 같은 이유)
        for ddl in scenario.ddl:
            conn.exec_driver_sql(ddl)
        session = Session(bind=conn, join_transaction_mode="create_savepoint")
        try:
            scenario.seed(session)
            session.commit()
            yield session
        finally:
            session.close()
    finally:
        outer.rollback()
        conn.close()


@pytest.mark.parametrize("name", list(MATERIALS))
def test_day1_return_split_matches_month_query(name, engine, engine_input_tables):
    """'이번 달 배정' = `get_active_assignments_for_month` 와 같은 행·같은 순서 · '1일 복귀' = 전월 말일에 끝난 이 병동 → 다른 병동
    파견만(실효 종료일 = `end_date` 없으면 `expected_end_date` · 취소·병동이동·휴직·이 병동이 target 인 것 제외)."""
    from services.assignment_service import get_active_assignments_for_month
    from services.roster_create_service import _assignments_with_day1_returns

    scenario, n_month, want_returns = MATERIALS[name]
    with _seeded(engine, scenario) as db:
        this_month, returns = _assignments_with_day1_returns(db, GROUP_ID, 2026, 11)
        original = get_active_assignments_for_month(db, GROUP_ID, 2026, 11)
        assert [a.id for a in this_month] == [a.id for a in original]
        assert len(this_month) == n_month, [(a.nurse_id, a.start_date, a.expected_end_date) for a in this_month]
        assert sorted(a.nurse_id for a in returns) == want_returns


def test_day1_return_source_map_in_request_is_400():
    """`_return_source_map` 은 서버가 1일 복귀 배정에서 계산해 넣는 내부 값이다 — 요청으로 실으면 월경계 꼬리를 바꿔(예: 근무표가 없는
    병동을 가리켜 꼬리를 지움) 하드락이 풀리므로 값과 무관하게 400(레지스트리 `RUNTIME_DATA_NOT_SETTINGS` · 월경계 내부 키)."""
    from services.engine_contract.settings import RUNTIME_DATA_NOT_SETTINGS
    from services.policy.hard_lock import _FORBIDDEN_INTERNAL_KEYS
    from services.roster_create_service import assert_night_caps_within_hard_lock

    assert "_return_source_map" in {key for key, _part in RUNTIME_DATA_NOT_SETTINGS}
    assert "_return_source_map" in _FORBIDDEN_INTERNAL_KEYS
    for value in ({"N07": ["G2", "2026-10-20"]}, {}, "x", None):
        with pytest.raises(HTTPException) as exc:
            assert_night_caps_within_hard_lock({"_return_source_map": value}, None)
        assert exc.value.status_code == 400 and "_return_source_map" in str(exc.value.detail), (value, exc.value.detail)


# ── 11w-2 출발 경계 순수 함수 ─────────────────────────────────────────────────────


def _rules(**over):
    from services.engine_input.boundary_fixed import BoundaryRules

    base = {"K": 5, "L": 3, "two2": True, "two3": True, "ban_nd": True, "ban_ed": True, "ban_ne": True}
    return BoundaryRules(**{**base, **over})


#: (상대 첫 칸, 출발일 s, 규칙 덮기, 기대 금지칸 {날: 코드}, 기대 OFF 창) — 손으로 구한 값(K=5).
DEPARTURE_CELLS = [
    ("DDODD", 9, {}, {8: {"N", "E"}}, [[5, 8]]),             # D 다음: N·E 금지 · 상대 연속 2 → 앞 4일(5~8) OFF ≥1
    ("NNOOD", 9, {}, {8: {"N"}}, [[5, 8]]),                  # N 다음: N 이어 붙이기 금지(#3·합친 블록 회복)
    ("EEOOD", 9, {}, {8: {"N"}}, [[5, 8]]),                  # E 다음: N 금지(N→E)
    ("ODDOO", 9, {}, {}, []),                                # 상대 첫 칸 OFF — 칸 없음(회복은 엔진 몫)
    ("DDDDD", 9, {}, {8: {"N", "E"}}, [[8, 8]]),             # 상대 연속 5 → 마지막 날 OFF
    ("DDDDDD", 9, {}, {8: {"N", "E"}}, [[8, 8]]),            # 상대만으로 6 — 이 병동은 마지막 날 OFF(K-h+1 ≤ 0)
    ("-DD", 9, {}, {}, []),                                  # 첫 칸 모름 — 연속 끊김
    ("D", 2, {}, {1: {"N", "E"}}, [[0, 1]]),                 # 월초 근처 — 창은 1일까지(전월 칸은 안 읽는다 · 막는 쪽)
    ("MD", 9, {}, {}, [[5, 8]]),                             # M 은 전이 금지 대상이 아니다 · 근무로 센다
    ("DDODD", 9, {"ban_nd": False, "ban_ed": False, "ban_ne": False}, {}, [[5, 8]]),   # 전이 금지 꺼짐 — 창만
    ("NNOOD", 9, {"ban_nd": False, "ban_ed": False, "ban_ne": False}, {8: {"N"}}, [[5, 8]]),  # N 이어 붙이기는 #3 몫
    ("DDODD", 9, {"K": 0}, {8: {"N", "E"}}, []),             # 연속근무 상한 없음 — 창 없음
    ("DD", 0, {}, {}, []),                                   # 1일 출발은 앞 경계가 전월 쪽 — 칸 없음
]


@pytest.mark.parametrize("head,s,over,forbidden,windows", DEPARTURE_CELLS)
def test_departure_cells_table(head, s, over, forbidden, windows):
    from services.engine_input.boundary_fixed import departure_cells

    cells = departure_cells(list(head), s, _rules(**over))
    assert (cells.forbidden, cells.windows, cells.forced) == (forbidden, windows, set())


#: (상대 칸, s, 상대 칸 마지막 날, 일수, departure_cells 창, 기대 창) — 짧은 파견 앞뒤 · 월말 · 월초(K=5).
DEPARTURE_WINDOWS = [
    ("DDODD", 9, 13, 30, [[5, 8]], [[5, 8]]),                                    # S6 N07 — 창 하나
    ("DD", 9, 10, 30, [[5, 8]], [[5, 8], [6, 11], [7, 12], [8, 13]]),           # 2일 파견 — 앞뒤를 잇는 창(차단일 9·10 은 엔진이 뺀다)
    ("DDD", 27, 29, 30, [[24, 26]], [[24, 26]]),                                 # 월말 — 다음 달로 넘는 창은 없다
    ("DO" + "-" * 26, 2, 29, 30, [[0, 1]], [[0, 1]]),                           # 월초 — 이번 달 몫만
    ("DOD", 9, 11, 30, [[4, 8]], [[4, 8]]),                                      # 상대 칸에 OFF — 앞뒤가 안 이어진다
    ("DD", 9, 29, 30, [[5, 8]], [[5, 8]]),                                       # 상대 칸이 모자라면 모름('-') — 끊김
]


@pytest.mark.parametrize("away,s,away_end,days,base,want", DEPARTURE_WINDOWS)
def test_departure_windows_are_minimal(away, s, away_end, days, base, want):
    from services.engine_input.boundary_fixed import departure_windows

    assert departure_windows(list(away), s, away_end, days, 5, base) == want


def test_departure_boundary_marks_return_days():
    """이 달 안에 돌아오면 돌아온 날은 '#'(엔진 칸) — 상대 연속 근무를 거기서 끊는다(이 병동 날은 창 후보가 따로 본다)."""
    from services.engine_input.boundary_fixed import departure_boundary

    cells = departure_boundary(list("DD"), 9, 10, True, 30, _rules(two2=False))
    assert (cells.forbidden, cells.windows) == ({8: {"N", "E"}}, [[5, 8], [6, 11], [7, 12], [8, 13]])
    assert departure_boundary(list("DD") + ["-"] * 19, 9, 29, False, 30, _rules()).windows == [[5, 8]]


def _builder_rules(config_dict: dict) -> dict:
    """월중 경계 빌더 원문의 설정 해석 줄(`def _safe_int` 부터 전이 금지 끄기까지)을 떼어 실행한 값(test_d13 방식)."""
    src = Path(__file__).resolve().parents[2].joinpath("app/services/roster_create_service.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef)
              and n.name == "build_mid_month_boundary_constraints")
    lines = src.splitlines()
    start = next(i for i in range(fn.lineno - 1, fn.end_lineno) if lines[i].strip().startswith("def _safe_int"))
    end = next(i for i in range(start, fn.end_lineno) if lines[i].strip().startswith("forced_off:"))
    ns: dict = {"config_dict": config_dict}
    exec(textwrap.dedent("\n".join(lines[start:end])), ns)  # noqa: S102 — 빌더 원문 그대로 실행
    return {k: ns[k] for k in ("K", "L", "two_after_two", "two_after_three", "ban_n_to_d", "ban_e_to_d", "ban_n_to_e")}


RULE_CONFIGS = [
    {}, {"max_conseq_work": "6"}, {"max_conseq_work": None, "max_consecutive_work_days": 4}, {"max_conseq_work": " "},
    {"max_conseq_work": "x"}, {"allow_three_consecutive_nights": True}, {"ban_shift_transitions": True},
    {"ban_shift_transitions": True, "ban_n_to_e": False, "ban_e_to_d": 0}, {"ban_shift_transitions": False, "ban_e_to_d": True},
    {"two_offs_after_two_nig": 1, "two_offs_after_three_nig": True},
]


@pytest.mark.parametrize("cfg", RULE_CONFIGS)
def test_rules_from_config_matches_builder(cfg):
    """출발 경계 규칙 해석(`rules_from_config`)이 같은 빌더의 합류·복귀 해석과 같다 — 갈라지면 한 간호사의 앞뒤 경계가 다른 규칙으로
    걸린다."""
    from services.engine_input.boundary_fixed import rules_from_config

    r = rules_from_config(cfg)
    got = {"K": r.K, "L": r.L, "two_after_two": r.two2, "two_after_three": r.two3,
           "ban_n_to_d": r.ban_nd, "ban_e_to_d": r.ban_ed, "ban_n_to_e": r.ban_ne}
    assert got == _builder_rules(cfg)


@pytest.mark.parametrize("two2,two3,want", [(True, True, 2), (True, False, 2), (False, True, 3), (False, False, None)])
def test_rules_recovery_threshold(two2, two3, want):
    threshold = _rules(two2=two2, two3=two3).T
    assert threshold == want


# ── 11w-2 월중 경계 목록(조회 없이) ─────────────────────────────────────────────────


def _a(nid, src, dst, start, end, reason="파견", real_end=None):
    return SimpleNamespace(nurse_id=nid, source_group_id=src, target_group_id=dst, start_date=start,
                           expected_end_date=end, end_date=real_end, reason=reason)


def test_mid_month_boundaries_listed_without_queries():
    """합류·복귀(앞 경계)와 출발(뒤 경계)을 배정만으로 고른다 — 1일 시작·1일 출발·월말 종료·다음 달 종료·병동이동 인바운드 끝은 없다."""
    from services.roster_create_service import _departure_boundaries, _mid_month_starts

    m0 = date(2026, 11, 1)
    inbound = [_a("X01", "G2", "G1", date(2026, 11, 20), date(2026, 11, 25)),               # 합류 19 · 26일 원 병동 복귀(출발 25)
               _a("X02", "G2", "G1", date(2026, 11, 1), date(2026, 11, 30)),                # 1일 합류(월경계) · 월말 종료
               _a("X03", "G2", "G1", date(2026, 11, 5), None, reason="병동이동"),           # 병동이동 합류 4 · 돌아가지 않음
               _a("X04", "G3", "G1", date(2026, 10, 20), date(2026, 11, 3), real_end=date(2026, 11, 2))]  # 실제 종료 우선 → 출발 2
    outbound = [_a("N07", "G1", "G2", date(2026, 11, 10), date(2026, 11, 14)),              # 출발 9 · 복귀 14
                _a("N08", "G1", "G2", date(2026, 11, 16), None, reason="병동이동"),          # 출발 15 · 월말까지
                _a("N09", "G1", "G3", date(2026, 11, 1), date(2026, 11, 5)),                # 1일 출발(범위 밖) · 복귀 5
                _a("N10", "G1", "G2", date(2026, 11, 25), date(2026, 12, 3)),               # 출발 24 · 다음 달 종료 → 월말까지
                _a("N11", "G1", "G2", date(2026, 11, 29), date(2026, 11, 30))]              # 출발 28 · 월말 종료 → 복귀는 다음 달
    starts = [tuple(b) for b in _mid_month_starts(inbound, outbound, m0, 30)]
    deps = [tuple(b) for b in _departure_boundaries(inbound, outbound, m0, 30)]
    assert starts == [("X01", "G2", 19, "join"), ("X03", "G2", 4, "join"), ("N07", "G2", 14, "return"),
                      ("N09", "G3", 5, "return")]
    assert deps == [("N07", "G2", 9, 13, True), ("N08", "G2", 15, 29, False), ("N10", "G2", 24, 29, False),
                    ("N11", "G2", 28, 29, False), ("X01", "G2", 25, 29, False), ("X04", "G3", 2, 29, False)]


def test_month_start_refs_keep_only_nurses_working_early():
    """월경계 꼬리를 상대 전월 근무표에서 읽은 기록 — 월초 6일이 모두 차단일인 간호사(월중 합류)는 뺀다 · 차단일 모양이 어긋나면 남긴다."""
    from services.roster_create_service import _month_start_refs

    refs = [{"nurse_id": n} for n in ("A", "B", "C", "D")]
    blocked = {"A": set(range(19)), "B": {0, 1}, "D": "x"}
    assert [r["nurse_id"] for r in _month_start_refs(refs, blocked, 6)] == ["B", "C", "D"]
    assert [r["nurse_id"] for r in _month_start_refs(refs, None, 6)] == ["A", "B", "C", "D"]


# ── 11w-2 생성 결과 안내 묶기(D38) · 성공 응답 boundary_notices 칸(11w-n D39) ──────────────────────────


class _GroupDb:
    """`Group` 조회만 답하는 가짜 세션 — 몇 번 불렸는지 센다."""

    def __init__(self, names: dict):
        self.names, self.calls = names, 0

    def query(self, _model):
        self.calls += 1
        rows = [SimpleNamespace(group_id=g, group_name=n) for g, n in self.names.items()]
        return SimpleNamespace(filter=lambda *_a, **_k: SimpleNamespace(all=lambda: rows))


def _ref(kind, nid, gid="G2", month=11, basis="latest", sid="s11"):
    return {"kind": kind, "nurse_id": nid, "group_id": gid, "year": 2026, "month": month, "schedule_id": sid,
            "basis": basis}


def test_draft_notices_one_per_ward_month():
    """작성 중 근무표만 · 병동·달마다 1건 · 같은 쓰임은 한 번 · 마감본은 빠진다 · 문구는 쓰임에 따라(경계/나이트 상한/둘 다).
    이 목록이 성공 응답 `boundary_notices` 칸에 그대로 실린다(D39) — 항목 칸은 프론트 전달서(U2)가 읽는 모양 그대로다."""
    from services.roster_create_service import _other_ward_draft_notices

    db = _GroupDb({"G2": "내과", "G3": "외과"})
    refs = [_ref("departure", "N07"), _ref("night_sum", "N07"), _ref("departure", "N07"), _ref("join", "X01"),
            _ref("night_sum", "N08", gid="G3", sid="t11"), _ref("return_day1", "N09", month=10, sid="s10"),
            _ref("join", "X05", gid="G4", basis="issued", sid="u11")]
    out = _other_ward_draft_notices(db, refs)
    assert db.calls == 1
    assert [(n["group_id"], n["month"], n["schedule_id"]) for n in out] == [("G2", 11, "s11"), ("G3", 11, "t11"),
                                                                           ("G2", 10, "s10")]
    assert out[0]["uses"] == [{"kind": "departure", "nurse_id": "N07"}, {"kind": "night_sum", "nurse_id": "N07"},
                              {"kind": "join", "nurse_id": "X01"}]
    assert [n["detail"].split("를 기준으로 ")[1].split(" 맞췄습니다")[0] for n in out] == [
        "경계와 월 나이트 상한을", "월 나이트 상한을", "경계를"]
    assert out[0]["detail"].startswith("내과 11월 근무표(작성 중") and out[2]["detail"].startswith("내과 10월 근무표(작성 중")
    assert all(n["type"] == "boundary_other_ward_reference" and n["schedule_status"] == "draft" for n in out)
    assert all(set(n) == {"type", "detail", "group_id", "group_name", "year", "month", "schedule_id", "schedule_status",
                          "uses"} for n in out)


def test_draft_notices_without_drafts_read_nothing():
    """안내할 것이 없으면(마감본만 · 기록 없음) 병동 이름도 읽지 않는다 · 이름이 없는 병동은 id 로 안내한다."""
    from services.roster_create_service import _other_ward_draft_notices

    db = _GroupDb({})
    assert _other_ward_draft_notices(db, [_ref("join", "X01", basis="issued")]) == []
    assert _other_ward_draft_notices(db, []) == [] and db.calls == 0
    out = _other_ward_draft_notices(db, [_ref("join", "X01", gid="G9")])
    assert out[0]["group_name"] == "G9" and out[0]["detail"].startswith("G9 11월") and db.calls == 1
