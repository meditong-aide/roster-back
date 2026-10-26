"""EI-15 Br — 대체근무 추천 호출 지점별 동작 시험(설정이 비었을 때 레지스트리 기본이 실제로 쓰인다).

15a 의 AST 시험은 보조다. 여기서는 각 호출 지점이 config=None 일 때 엔진 기본(연속 5 · 월 N 15 · E→D 금지 · 3N 후 OFF2)을
실제로 적용하는지, 그리고 키를 바꿔치거나 상수로 고정하면 빨개지는지를 값으로 본다.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from services import replacement_recommend_service as rrs
from services.cp_sat import feasibility_alerts


def _steps(nid: str = "C", day: int = 1, shift: str = "D"):
    return [SimpleNamespace(candidate=SimpleNamespace(nurse_id=nid), slot=SimpleNamespace(date=date(2026, 11, day), shift=shift))]


def _violations(row, config=None):
    n = len(row)
    return rrs._count_path_violations(
        path_steps=_steps("C", 1, row[0]),
        schedule_map={"C": list(row), "T": ["O"] * n},
        schedule_shift_pk_map={},
        nurse_by_id={},
        shift_meta={},
        shift_meta_by_pk={},
        config=config,
        prev_tail_by_nurse={},
        target_nurse_id="T",
        include_target_nurse=False,
    ).by_type


def _month(**at):
    row = ["O"] * 31
    for d, s in at.items():
        row[int(d[1:]) - 1] = s
    return row


def test_path_violations_consecutive_boundary_is_five():
    five = _month(**{f"d{i}": "D" for i in range(1, 6)})
    six = _month(**{f"d{i}": "D" for i in range(1, 7)})
    assert "consecutive_work" not in _violations(five)
    assert "consecutive_work" in _violations(six)  # 옛 기본 6 에서는 통과
    # 설정 행 값이 있으면 그 값
    assert "consecutive_work" not in _violations(six, SimpleNamespace(max_conseq_work=6))


def test_path_violations_eve_to_day_and_3n2o_default_on():
    ed = _month(d1="E", d2="D")
    assert "eve_ed" in _violations(ed)
    assert "eve_ed" not in _violations(ed, SimpleNamespace(banned_day_after_eve=False))
    nnn_o = _month(d1="N", d2="N", d3="N", d4="O", d5="D")
    assert "rec_3n2o" in _violations(nnn_o)
    assert "rec_3n2o" not in _violations(nnn_o, SimpleNamespace(two_offs_after_three_nig=False))
    # 2N 후 OFF2 는 레지스트리 기본이 꺼짐 -> 켜지 않는다
    nn_o = _month(d1="N", d2="N", d3="O", d4="D")
    assert "rec_2n2o" not in _violations(nn_o)


def test_path_violations_monthly_night_limit_is_fifteen():
    def nights(k):
        return _month(**{f"d{2 * i + 1}": "N" for i in range(k)})

    assert "night_month_limit" not in _violations(nights(9))  # 옛 한도 8 에서는 경고
    assert "night_month_limit" not in _violations(nights(15))
    assert "night_month_limit" in _violations(nights(16))
    assert "night_month_limit" in _violations(nights(9), SimpleNamespace(max_nig_per_month=8))


def test_recovery_off_risk_flags_3n_then_one_off_by_default():
    sched = ["N", "N", "N", "O", "D"]
    _risk, tags = rrs._recovery_off_risk(sched, 3, {}, None)
    assert "protected_off_3n2o_day1_risk" in tags
    assert not any("2n2o" in t for t in tags)
    _risk, tags = rrs._recovery_off_risk(sched, 3, {}, SimpleNamespace(two_offs_after_three_nig=False))
    assert not any("3n2o" in t for t in tags)
    _risk, tags = rrs._recovery_off_risk(["N", "N", "O", "D"], 2, {}, SimpleNamespace(two_offs_after_two_nig=True))
    assert "protected_off_2n2o_day1_risk" in tags


def _tags(config, schedule, day_idx, code):
    return rrs._rule_safety_score(
        nurse=SimpleNamespace(), schedule=schedule, day_idx=day_idx, target_shift_code=code,
        shift_meta={}, config=config,
    )[2]


def test_rule_safety_eve_to_day_and_night_limit_defaults():
    assert "eve_to_day_risk" in _tags(None, ["E", "D"], 1, "D")
    assert "lookahead_eve_to_day_risk" in _tags(None, ["E", "D"], 0, "E")
    assert "eve_to_day_risk" not in _tags(SimpleNamespace(banned_day_after_eve=False), ["E", "D"], 1, "D")

    def row(k):
        r = ["O"] * 30
        for i in range(k):
            r[2 * i] = "N"
        return r  # 홀수 일자 N k 개, 29일(idx 28 이후) 대상 칸은 OFF

    # 이미 N 14개 + 대상 1 = 15 -> 한도 안 / N 15개 + 1 = 16 -> 한도 초과
    assert "monthly_night_limit_risk" not in _tags(None, row(14), 29, "N")
    assert "monthly_night_limit_risk" in _tags(None, row(15), 29, "N")
    # 옛 기본 8 이면 9번째에서 경고했다 -> 지금은 없다
    assert "monthly_night_limit_risk" not in _tags(None, row(8), 29, "N")
    assert "monthly_night_limit_risk" in _tags(SimpleNamespace(max_nig_per_month=8), row(8), 29, "N")


def test_segment_prefilter_cut_at_default_five(monkeypatch):
    calls = []
    monkeypatch.setattr(rrs, "_evaluate_single_slot", lambda **kw: [("C", object(), 1.0)])
    monkeypatch.setattr(rrs, "_ctx_to_candidate_model", lambda *a, **k: SimpleNamespace(tags=[], nurse_id="C"))
    monkeypatch.setattr(rrs, "BulkPathStep", lambda **kw: SimpleNamespace(**kw))
    monkeypatch.setattr(rrs, "_build_coverage_req", lambda c: {})
    monkeypatch.setattr(rrs, "_is_candidate_allowed_by_scope", lambda *a, **k: True)

    def fake_count(**kw):
        calls.append(kw)
        return SimpleNamespace(total_count=0)

    monkeypatch.setattr(rrs, "_count_path_violations", fake_count)

    def run(work_days, config=None):
        # 1..work_days 일 근무, 그 뒤 첫 OFF 칸이 대체 구간(day work_days+1) -> 채우면 연속 work_days+1
        row = ["D"] * work_days + ["O"] * (30 - work_days)
        slot = SimpleNamespace(date=date(2026, 11, work_days + 1), shift="D")
        req = SimpleNamespace(target_nurse_id="T", options=SimpleNamespace(include_explanations=False))
        calls.clear()
        rrs._evaluate_segment_single_nurse(
            [slot], req, {"C": row, "T": ["D"] * 30}, {}, {}, {}, {}, config, {}, {}, {}, None, "all", 10, set(),
        )
        return len(calls)

    assert run(4) == 1  # 연속 5 -> 통과(후보 시뮬레이션까지 간다)
    assert run(5) == 0  # 연속 6 -> 사전 컷(옛 기본 6 에서는 통과)
    assert run(5, SimpleNamespace(max_conseq_work=6)) == 1  # 설정 행 값이 있으면 그 값


def test_feasibility_alert_reads_preceptee_default_from_registry(monkeypatch):
    seen = []
    real = feasibility_alerts.registry_default
    monkeypatch.setattr(feasibility_alerts, "registry_default", lambda k: (seen.append(k), real(k))[1])
    nurses = [SimpleNamespace(name="a", id="a", preceptor_id="", nurse_id="a")]
    feasibility_alerts.run_preflight_feasibility_alerts(
        nurses_in_group=nurses, config_dict={"use_mid": True}, year=2026, month=11,
    )
    assert "preceptee_on" in seen
    assert "max_conseq_work" not in seen and "use_mid" not in seen
