"""model_canon.py 단위 테스트 — 2026-10-13 보강(제약 "안"의 (vars,coeffs)/literals/exprs
순서 흡수) · 2026-10-14 보강(근무 색인 재표기, EI-1c 마무리). 장난감 CP-SAT 모델만 쓴다 —
엔진 시나리오 하네스를 거치지 않아 빠르고, "변수 생성 순서만 바뀜" 같은 정밀한 조건을 직접
통제할 수 있다.

실측 배경(review_fingerprint/exp/e1_toy_order.py 재현) — ortools 9.12 는 linear 제약의
`vars` 를 변수 "색인"(= NewBoolVar/NewIntVar 를 부른 순서) 순으로 저장한다. 그래서 전에는
제약을 쌓는 순서만 흡수하고 제약 "안"의 나열 순서는 그대로 둬서, 변수 생성 순서만 바뀐
무해한 리팩터에도 digest 가 달라졌다. 이 파일은 그 무해함을 직접 확인한다.
"""

from __future__ import annotations

import pytest
from ortools.sat.python import cp_model

from .model_canon import (
    _EXPR_SET_KINDS,
    _LINEAR_PAIR_KINDS,
    _LITERAL_SET_KINDS,
    _ORDER_SENSITIVE_KINDS,
    _known_constraint_kinds,
    _relabel_name,
    _relabel_shift_indices,
    fingerprint_digest,
    model_fingerprint,
)


def test_constraint_kind_classification_is_exhaustive():
    """ortools 가 아는 모든 제약 종류가 넷 중 하나로 분류돼 있다 — 새 ortools 버전에서
    종류가 늘면(미분류) 이 테스트가 먼저 깨져서 알려준다(조용히 잘못 정렬하거나 조용히
    안 정렬하는 쪽보다 낫다)."""
    classified = _LITERAL_SET_KINDS | _LINEAR_PAIR_KINDS | _EXPR_SET_KINDS | _ORDER_SENSITIVE_KINDS
    assert classified == _known_constraint_kinds()


def test_linear_constraint_var_creation_order_does_not_change_digest():
    """linear 제약 하나(x+y+z<=2) — 변수를 x,y,z 순으로 만든 모델과 z,y,x 순으로 만든
    모델(제약 자체는 변수 이름 기준으로 같은 식)이 같은 digest 를 내야 한다."""
    m1 = cp_model.CpModel()
    x = m1.NewBoolVar("x")
    y = m1.NewBoolVar("y")
    z = m1.NewBoolVar("z")
    m1.Add(x + y + z <= 2)

    m2 = cp_model.CpModel()
    z2 = m2.NewBoolVar("z")
    y2 = m2.NewBoolVar("y")
    x2 = m2.NewBoolVar("x")
    m2.Add(x2 + y2 + z2 <= 2)

    fp1 = model_fingerprint(m1)
    fp2 = model_fingerprint(m2)
    assert fingerprint_digest(fp1) == fingerprint_digest(fp2)


def test_exactly_one_literal_order_does_not_change_digest():
    """exactly_one(a,b,c) — 리터럴을 선언한 순서가 달라도 같은 digest."""
    m1 = cp_model.CpModel()
    a, b, c = (m1.NewBoolVar(n) for n in "abc")
    m1.AddExactlyOne([a, b, c])

    m2 = cp_model.CpModel()
    c2, b2, a2 = (m2.NewBoolVar(n) for n in "cba")
    m2.AddExactlyOne([a2, b2, c2])

    assert fingerprint_digest(model_fingerprint(m1)) == fingerprint_digest(model_fingerprint(m2))


def test_lin_max_expr_order_does_not_change_digest():
    """lin_max(target, [a, b]) 대 (target, [b, a]) — max 는 교환법칙이 성립하므로 같은 digest."""
    m1 = cp_model.CpModel()
    a = m1.NewIntVar(0, 10, "a")
    b = m1.NewIntVar(0, 10, "b")
    t = m1.NewIntVar(0, 10, "t")
    m1.AddMaxEquality(t, [a, b])

    m2 = cp_model.CpModel()
    a2 = m2.NewIntVar(0, 10, "a")
    b2 = m2.NewIntVar(0, 10, "b")
    t2 = m2.NewIntVar(0, 10, "t")
    m2.AddMaxEquality(t2, [b2, a2])

    assert fingerprint_digest(model_fingerprint(m1)) == fingerprint_digest(model_fingerprint(m2))


def test_order_sensitive_kind_not_reordered():
    """no_overlap — 순서 의미가 있는 종류는 그대로 둔다(정렬 대상이 아님을 확인하는
    회귀 — 참조 색인이 있는 종류를 건드리면 의미가 깨진다는 설계 전제의 직접 검증은
    아니지만, 적어도 이 분류의 하위 필드가 "정렬되지 않았다"는 것만이라도 고정한다)."""
    m = cp_model.CpModel()
    i1 = m.NewIntervalVar(0, 3, 3, "iv1")
    i2 = m.NewIntervalVar(5, 2, 7, "iv2")
    m.AddNoOverlap([i1, i2])
    fp = model_fingerprint(m)
    no_overlap = next(c for c in fp["constraints"] if c["kind"] == "no_overlap")
    # intervals 필드는 제약 색인이다 — 이 테스트는 그 필드가 존재하고 두 항목을 담고
    # 있음을 확인한다(정렬 여부와 무관하게 내용 보존).
    assert len(no_overlap["body"].get("intervals", [])) == 2


def test_int_div_operand_order_preserved():
    """int_div(target, num, denom) — 피제수/제수 순서는 의미가 있으니 정렬되면 안 된다.
    두 피연산자를 바꾼 모델은 **다른** digest 를 내야 한다(정렬돼서 같아지면 안 됨)."""
    m1 = cp_model.CpModel()
    num = m1.NewIntVar(1, 10, "num")
    denom = m1.NewIntVar(1, 10, "denom")
    t = m1.NewIntVar(0, 10, "t")
    m1.AddDivisionEquality(t, num, denom)

    m2 = cp_model.CpModel()
    num2 = m2.NewIntVar(1, 10, "num")
    denom2 = m2.NewIntVar(1, 10, "denom")
    t2 = m2.NewIntVar(0, 10, "t")
    m2.AddDivisionEquality(t2, denom2, num2)  # 피연산자를 바꿔치기

    assert fingerprint_digest(model_fingerprint(m1)) != fingerprint_digest(model_fingerprint(m2))


# ── 근무 색인 재표기(2026-10-14 보강 — EI-1c 마무리) ───────────────────────
#
# shift_types=["E", "N", "D"] 실측 상황을 가정한 고정 맵 — 정본(코드 알파벳순)은 D=0·E=1·N=2,
# 이 실행은 색인 0=E·1=N·2=D 로 지었으니 재매김은 0→1·1→2·2→0.
_SHIFT_INDEX_MAP = {0: 1, 1: 2, 2: 0}

#: golden_model/*.json 변수 이름 106가지 모양 전수에서, 근무 색인 재표기 "대상이 아닌" 쪽의
#: 대표 샘플(간호사·일자·주·등급·코드 "문자"만 들어있어 안전해야 하는 이름들).
_NON_TARGET_NAMES = [
    "EOD_0_1", "NOD_3_2", "NOE_5_0", "cnight_exc_0_4", "d5_0_11",
    "end_2n_hard_0_1", "end_2n_prev_soft_3", "end_2n_soft_b0_2", "eod_1_3",
    "expShort_4_D", "fb_xmonth_eod_2", "grade_monthly_slack_D_g1", "iso_0_5",
    "iso_off_cost_1_2", "iso_off_slack_2_1", "isow_0_9", "kld_D_dH1_메인_0",
    "kld_tw_r3_메인", "lone_e_1_8", "lookahead_max_off", "max_same_shift_N_2_3",
    "min_off_miss_7", "n2n_0_0_2", "nod_1_2", "noe_3_4", "off_cap_slack_2",
    "off_cap_slack_weighted_2", "off_quota_excess_1", "off_quota_excess_cost_1",
    "off_quota_short_1", "over_3_D", "sc_over_D_메인_0", "sc_under_N_메인_2",
    "seq_off_0_1", "short_2_M", "soft_coff_over_0_1", "soft_cwork_miss_2_3",
    "sr_cnt_E_메인_0", "sr_max_DE_메인_1", "sr_min_DN_메인_2", "sr_viol_EN_메인_0",
    "weekSlack_0_2", "week_miss_1_3", "__unnamed_7",
]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("x_3_7_0", "x_3_7_1"),
        ("x_3_7_2", "x_3_7_0"),
        ("gcasc_d4_s1_g2_c1", "gcasc_d4_s2_g2_c1"),
        ("tmin_present_t5_d2_s2", "tmin_present_t5_d2_s0"),
        ("tmin_cover_slack_d2_s0", "tmin_cover_slack_d2_s1"),
        ("hoff_same_t3_r0_d1_s1", "hoff_same_t3_r0_d1_s2"),
        ("not(x_0_0_2)", "not(x_0_0_0)"),
    ],
)
def test_relabel_name_remaps_shift_index_patterns(name, expected):
    """다섯 색인 패턴(x_·gcasc_·tmin_present_·tmin_cover_slack_·hoff_same_) + `not()` 래핑이
    `_SHIFT_INDEX_MAP` 대로 다시 매겨진다."""
    assert _relabel_name(name, _SHIFT_INDEX_MAP) == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("ov_diff_5_E_D", "ov_diff_5_D_E"),
        ("ov_diff_5_D_E", "ov_diff_5_D_E"),  # 이미 정렬된 경우 — 그대로(idempotent).
        ("ov_diff_fb_3_N_D", "ov_diff_fb_3_D_N"),
    ],
)
def test_relabel_name_sorts_ov_diff_pair_symmetrically(name, expected):
    """`ov_diff_{d}_{c1}_{c2}` 는 색인이 아니라 코드 문자가 직접 박히므로, 재표기 대신
    알파벳순 정렬로 대칭을 맞춘다(색인 맵은 쓰지 않음을 같이 확인 — 빈 맵을 줘도 동작)."""
    assert _relabel_name(name, {}) == expected


@pytest.mark.parametrize("name", _NON_TARGET_NAMES)
def test_relabel_name_leaves_non_target_names_unchanged(name):
    """재표기 대상이 아닌 이름(golden_model 실제 모양 대표 샘플)은, 색인을 완전히 뒤섞는
    변환(derangement)을 줘도 그대로여야 한다 — 정규식이 엉뚱한 자리에 잘못 걸리지 않는지
    보는 것이 이 테스트의 목적이다."""
    derangement = {i: (i + 1) % 13 for i in range(13)}
    assert _relabel_name(name, derangement) == name


def test_relabel_name_out_of_range_shift_index_raises():
    """색인 패턴에 걸렸는데 그 색인이 맵(=그 실행의 shift_types 길이) 밖이면, 조용히
    넘기지 않고 예외를 던진다(설계서 '조용히 넘어가지 않는다' 태도와 동일)."""
    with pytest.raises(ValueError, match="근무 색인"):
        _relabel_name("x_0_0_5", {0: 0, 1: 1, 2: 2})


def test_relabel_shift_indices_rejects_duplicate_shift_types():
    """`shift_types` 에 같은 코드가 두 번 있으면(비정상 입력) 예외를 던진다 — 알파벳순 랭크가
    유일하지 않으면 재매김 자체가 성립하지 않는다."""
    fp = {"variables": [], "constraints": [], "objective": {},
          "unnamed_variable_count": 0, "duplicate_names": []}
    with pytest.raises(ValueError, match="중복"):
        _relabel_shift_indices(fp, ["D", "E", "D"])


def _build_toy_shift_model(order: list[str]) -> cp_model.CpModel:
    """x_{n}_{d}_{s} 토이 모델 — 의미(코드 기준 제약)는 고정하고, 물리적 색인만 `order`(그
    실행의 shift_types, PYTHONHASHSEED 가 흔드는 그 순서를 흉내)에 따라 달라지게 짓는다.
    간호사 0 은 0일에 'D' 금지 + 매일 'E' 는 최대 1명."""
    nurses, days = 2, 2
    m = cp_model.CpModel()
    xv = {}
    for n in range(nurses):
        for d in range(days):
            for s in range(len(order)):
                xv[n, d, s] = m.NewBoolVar(f"x_{n}_{d}_{s}")
    d_idx, e_idx = order.index("D"), order.index("E")
    m.Add(xv[0, 0, d_idx] == 0)
    for d in range(days):
        m.Add(sum(xv[n, d, e_idx] for n in range(nurses)) <= 1)
    return m


def test_model_fingerprint_digest_invariant_under_shift_type_permutation():
    """같은 '의미'(코드 기준 제약)를 다른 물리적 shift_types 순서로 지은 두 모델 —
    `model_fingerprint(model, shift_types=...)` 에 그 실행의 실제 순서를 주면 digest 가
    같아야 한다(EI-1c 마무리 — EI-16 '근무 순서 결정론' 결함이 흔드는 자리를 흡수하는지
    직접 확인하는 핵심 테스트)."""
    order_a, order_b = ["D", "E"], ["E", "D"]
    fp_a = model_fingerprint(_build_toy_shift_model(order_a), order_a)
    fp_b = model_fingerprint(_build_toy_shift_model(order_b), order_b)
    assert fingerprint_digest(fp_a) == fingerprint_digest(fp_b)


def test_model_fingerprint_digest_differs_without_relabel_for_permuted_shift_types():
    """대조군 — `shift_types` 를 안 주면(재표기 없음) 위와 같은 두 모델의 digest 가
    다르다. 이 테스트가 실패(= 같아짐)하면 위 '불변' 테스트가 아무것도 증명하지 못하는
    상태(우연히 같아지는 장난감 모델)라는 뜻이다."""
    fp_a = model_fingerprint(_build_toy_shift_model(["D", "E"]))
    fp_b = model_fingerprint(_build_toy_shift_model(["E", "D"]))
    assert fingerprint_digest(fp_a) != fingerprint_digest(fp_b)
