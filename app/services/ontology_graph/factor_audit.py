"""Factor 완전성 audit — factor graph 에 **모든 제약이 빠짐없이 들어갔는지** 꼼꼼히 검사.

누락 factor 는 조용한 오판(feasible 인데 infeasible, 혹은 반대)을 낳으므로 3층으로 못 박는다:

  ① 구조(structural): 기대 factor 체크리스트 — 모든 x 변수가 factor 에 덮였는가, per-nurse
     시퀀스 factor 가 인원수만큼, 수요>0 인 날마다 커버리지 factor, banned/forced 가 도메인에.
  ② 규칙 인코딩(rule-encoding probe): config 의 각 활성 규칙마다 **위반 배열**을 만들어
     해당 factor 가 그 배열을 실제로 **거부**하는지 확인 → 규칙이 factor 에 실려있음을 증명.
  ③ 의미 재구성(semantic reconstruction): factor graph solver 판정 == 독립 oracle. 어떤
     factor 라도 누락/오류면 무작위 인스턴스에서 반드시 불일치.

audit_factor_graph 는 report(dict) 반환 + strict=True 면 구조/규칙 결함에 AssertionError.
"""

from __future__ import annotations

from services.ontology_graph.frontier_dp import _prep, _req
from services.ontology_graph.hypergraph_conditioning import (
    build_factor_graph,
    diagnose_conditioning,
)
from services.ontology_graph.lagrangian import _night_rules


def _structural(fg, nurses, config, num_days) -> dict:
    prep = _prep(nurses, config)
    k = len(prep)
    reqs = (_req(config, "D"), _req(config, "E"), _req(config, "N"))
    problems = []

    seq = [f for f in fg.factors if f.kind == "sequence"]
    cov = [f for f in fg.factors if f.kind == "coverage"]

    # per-nurse 시퀀스 factor: 인원수만큼, 각자 전체 날짜 덮음
    if len(seq) != k:
        problems.append(f"시퀀스 factor {len(seq)} ≠ 간호사 {k}")
    covered_by_seq = set()
    for f in seq:
        covered_by_seq |= set(f.scope)
        if len(f.scope) != num_days:
            problems.append(f"시퀀스 factor(간호사 {f.meta.get('nurse')}) 날짜 {len(f.scope)} ≠ {num_days}")

    # 커버리지 factor: 수요>0 인 날마다 정확히 1개, 전원 덮음
    days_with_req = num_days if any(reqs) else 0
    if len(cov) != days_with_req:
        problems.append(f"커버리지 factor {len(cov)} ≠ 수요일수 {days_with_req}")
    for f in cov:
        # scope = 그날 근무 가능(강제OFF 아닌) 간호사. 강제OFF 는 항상 0 기여라 제외가 정당.
        d = f.meta.get("day")
        expect = sum(1 for i in range(k) if fg.variables.get(("x", i, d)) != ("O",))
        if len(f.scope) != expect:
            problems.append(f"커버리지 factor(day {d}) 멤버 {len(f.scope)} ≠ 근무가능 {expect}")

    # 모든 x 변수가 시퀀스 factor 에 덮였는가
    all_x = set(fg.variables)
    uncovered = all_x - covered_by_seq
    if uncovered:
        problems.append(f"시퀀스 factor 에 안 덮인 변수 {len(uncovered)}개: {list(uncovered)[:3]}")

    # banned/forced 가 도메인에 반영됐는가(강제OFF→{O}, 강제근무→O 제외)
    ic = config.get("initial_constraints") or {}
    fb, fo = ic.get("forbidden") or {}, ic.get("forced_off") or {}
    for i, n in enumerate(prep):
        nid = n["nid"]
        for d in (int(x) for x in (fo.get(nid) or [])):
            if 0 <= d < num_days and fg.variables.get(("x", i, d)) not in ((), ("O",)):
                problems.append(f"강제OFF 미반영 x[{i},{d}]={fg.variables.get(('x',i,d))}")
        for d, codes in (fb.get(nid) or {}).items():
            d = int(d)
            up = {str(c).upper() for c in codes}
            dom = fg.variables.get(("x", i, d), ())
            if "O" in up and "O" in dom:
                problems.append(f"강제근무(OFF금지) 미반영 x[{i},{d}] 도메인에 O 존재")
            if "N" in up and "N" in dom:
                problems.append(f"N금지 미반영 x[{i},{d}] 도메인에 N 존재")
    return {"seq_factors": len(seq), "cov_factors": len(cov),
            "vars": len(all_x), "problems": problems}


def _rule_probes(nurses, config, num_days) -> dict:
    """활성 규칙마다 위반 배열을 만들어 시퀀스 factor 가 거부하는지 확인."""
    fg = build_factor_graph(nurses, config, num_days)
    seq0 = next((f for f in fg.factors if f.kind == "sequence" and f.meta.get("nurse") == 0), None)
    if seq0 is None or num_days < 4:
        return {"probes": [], "note": "프로브 생략(간호사/일수 부족)"}
    max_run, rec_trig, min_run = _night_rules(config)

    def assign_seq(shifts):
        return {("x", 0, d): shifts[d] for d in range(num_days)}

    probes = []

    def probe(name, shifts, expect_reject):
        got_reject = not seq0.pred(assign_seq(shifts))
        ok = (got_reject == expect_reject)
        probes.append({"rule": name, "reject": got_reject, "expected": expect_reject, "ok": ok})

    O = "O"
    # not_one_night: 고립 1야간
    if config.get("not_one_night"):
        s = [O] * num_days
        s[1] = "N"                                    # 고립 N (양옆 O)
        probe("not_one_night(고립1N)", s, True)
    # max run: max_run+1 연속 N (N 가능 가정)
    if num_days >= max_run + 1:
        s = [O] * num_days
        for d in range(max_run + 1):
            s[d] = "N"
        probe(f"max_run({max_run})초과", s, True)
    # 회복=실제 OFF: 2연속N 뒤 D(근무)로 회복 대체 시도
    if config.get("two_offs_after_two_nig") or config.get("two_offs_after_three_nig"):
        run = 3 if config.get("two_offs_after_three_nig") else 2
        if num_days >= run + 1:
            s = [O] * num_days
            for d in range(run):
                s[d] = "N"
            s[run] = "D"                              # OFF 대신 근무 → 회복 위반
            probe("recovery=실제OFF(D로 대체 불가)", s, True)
    # forbid_night_to_day: N 다음날 D
    if config.get("forbid_night_to_day") and num_days >= 3:
        s = [O] * num_days
        s[0] = "N"; s[1] = "N"; s[2] = "D"            # run2 후 D (회복 위반 겸) — 전이 자체도 금지
        probe("forbid_night_to_day", s, True)
    # 정상 배열은 통과해야(거짓 거부 없음): 전부 OFF
    probe("정상(전부OFF)", [O] * num_days, False)
    return {"probes": probes, "all_ok": all(p["ok"] for p in probes)}


def _semantic_reconstruction(seed=99, n=200, budget=300_000) -> dict:
    """factor graph solver 판정 == 독립 oracle (누락 factor 면 불일치). 무작위 소형."""
    import random
    import sys
    sys.path.insert(0, __file__.rsplit("/app/", 1)[0] + "/tools/infeasible_cases")
    import exact_oracle
    exact_oracle._BUDGET = budget
    from exact_oracle import is_feasible
    from fuzz_crossval import _rand_case
    rng = random.Random(seed)
    mism = 0; checked = 0; ex = None
    for _ in range(n):
        nurses, cfg, days = _rand_case(rng)
        orc = is_feasible(nurses, cfg, days)
        if orc is None:
            continue
        r = diagnose_conditioning(nurses, cfg, days, budget=budget)
        if r.status == "UNKNOWN":
            continue
        checked += 1
        eng_inf = (r.status == "INFEASIBLE_CERTIFIED")
        if eng_inf != (orc is False):
            mism += 1
            ex = ex or (nurses, cfg, days, r.status, orc)
    return {"checked": checked, "mismatch": mism, "example": ex}


def audit_factor_graph(nurses, config, num_days, *, strict=False,
                       reconstruction=False) -> dict:
    """3층 완전성 audit. reconstruction=True 면 무작위 재구성 검사도(느림)."""
    fg = build_factor_graph(nurses, config, num_days)
    report = {"structural": _structural(fg, nurses, config, num_days),
              "rule_probes": _rule_probes(nurses, config, num_days)}
    if reconstruction:
        report["reconstruction"] = _semantic_reconstruction()
    struct_ok = not report["structural"]["problems"]
    rules_ok = report["rule_probes"].get("all_ok", True)
    report["complete"] = struct_ok and rules_ok and \
        (report.get("reconstruction", {}).get("mismatch", 0) == 0)
    if strict:
        assert struct_ok, f"구조 factor 누락: {report['structural']['problems']}"
        assert rules_ok, f"규칙 인코딩 누락: {report['rule_probes']['probes']}"
    return report
