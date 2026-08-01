"""Deletion-based MCS 추적기 — 복합 하드 모순을 조건 단위로 쫓아간다 (MUS·reify 없음).

핵심: 하나의 최소 수선집합(MCS)을 찾는 건 NP-hard 가 아니라 **선형 재solve**다.
전부 완화(→feasible)에서 시작해, 각 항목을 하나씩 '다시 강제'해보며 그래도 feasible
이면 그 항목은 충돌에 불필요 → 제거. 끝나면 남은 것이 최소 수선집합.

  relaxed = 전체
  for it in items:                      # ← 조합(2^N) 아님, 항목당 1번(선형)
      if resolve(relaxed - {it}): relaxed -= {it}
  return relaxed                        # 이걸 풀면 feasible = 추적된 복합 원인

2단계 입도: (1) family 단위로 싸게 얽힌 그룹 지목 → (2) 그 family 안에서만 instance
(간호사·날) 단위 drill-down → 실제 조합의 셀까지 추적. 완전성 손실 없음(family 가
얽혔다 → 그 안에서 좁힘). resolve_fn 은 엔진 비의존(주입식)이라 mock 으로 검증 가능.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Hashable, Iterable


@dataclass
class ConflictTrace:
    families: list[str]                               # 최소 수선 family 집합
    instances_by_family: dict[str, list[Any]]         # family → 지목된 instance(간호사/날)
    solve_count: int                                  # 총 재solve 횟수(선형 확인용)
    feasible_when_all_relaxed: bool                   # 전부 완화 시 feasible 였나
    certificate: str = ""


def minimal_correction_set(
    resolve_fn: Callable[[frozenset], bool],
    items: Iterable[Hashable],
    *,
    logger: Callable[[str], None] | None = None,
) -> tuple[frozenset, int] | None:
    """deletion-based MCS. resolve_fn(relaxed:frozenset)->feasible. (min_set, solve_count).

    전부 완화해도 infeasible 이면 None(카탈로그 밖 원인). 재solve = len(items)+1 (선형).
    """
    items = list(items)
    solves = 1
    if not resolve_fn(frozenset(items)):
        return None                                   # 전부 완화도 실패 → 이 축 밖
    relaxed = set(items)
    for it in items:
        trial = frozenset(relaxed - {it})
        solves += 1
        if resolve_fn(trial):                         # it 안 풀어도 feasible → 불필요
            relaxed = set(trial)
            if logger:
                logger(f"  [MCS] '{it}' 불필요(제거)")
        elif logger:
            logger(f"  [MCS] '{it}' 필수(유지)")
    return frozenset(relaxed), solves


def trace_conflict(
    family_resolve: Callable[[frozenset], bool],
    families: Iterable[str],
    *,
    instance_resolve: Callable[[str, frozenset], bool] | None = None,
    instances_by_family: dict[str, list[Any]] | None = None,
    logger: Callable[[str], None] | None = None,
) -> ConflictTrace:
    """복합 충돌 추적: family MCS → (선택) family 별 instance drill-down.

    family_resolve(relaxed_families) -> feasible.
    instance_resolve(family, relaxed_instances) -> feasible  (그 family 만 부분완화).
    """
    fams = list(families)
    total_solves = 0

    # 1) family 단위 최소 수선집합
    res = minimal_correction_set(family_resolve, fams, logger=logger)
    if res is None:
        return ConflictTrace(families=[], instances_by_family={}, solve_count=1,
                             feasible_when_all_relaxed=False,
                             certificate="지금 설정으론 만들 수 없어요 — 고정셀·데이터를 확인하세요.")
    min_fams, solves = res
    total_solves += solves

    # 2) 각 family 안에서 instance drill-down (culprit 간호사/날 좁히기)
    inst_by_fam: dict[str, list[Any]] = {}
    if instance_resolve and instances_by_family:
        for f in min_fams:
            insts = instances_by_family.get(f) or []
            if not insts:
                continue
            sub = minimal_correction_set(
                lambda relaxed, _f=f: instance_resolve(_f, relaxed), insts,
                logger=(lambda m, _f=f: logger(f"  [{_f}] {m}")) if logger else None)
            if sub is not None:
                culprits, ss = sub
                total_solves += ss
                inst_by_fam[f] = sorted(culprits, key=lambda x: str(x))

    fam_list = sorted(min_fams)
    parts = []
    for f in fam_list:
        ci = inst_by_fam.get(f)
        parts.append(_FAMILY_KO.get(f, f) + (f"({', '.join(str(c) for c in ci)})" if ci else ""))
    cert = ("다음을 함께 조정하면 만들 수 있어요: " + ", ".join(parts)
            if fam_list else "충돌 원인을 못 찾았어요.")

    return ConflictTrace(families=fam_list, instances_by_family=inst_by_fam,
                         solve_count=total_solves, feasible_when_all_relaxed=True,
                         certificate=cert)


# ── 실 엔진 배선: family → 완화 적용(config override / nurse 속성 override) ──────────
#   config 계열은 dict merge, per-nurse 계열은 nurse dict 변형. probe_feasibility 등
#   status 함수를 주입받아(엔진 비의존) family/instance resolve 를 만든다.
FAMILY_CONFIG_RELAX: dict[str, dict[str, Any]] = {
    "off_budget":    {"off_days": 1},   # 0 은 off-first 정책이 되레 깨져 과완화 → 1
    "night_cap":     {"max_nig_per_month": 40},
    "2n2off":        {"two_offs_after_two_nig": False},
    "night_recovery":{"two_offs_after_two_nig": False, "two_offs_after_three_nig": False},
    "not_one_night": {"not_one_night": False},
    "consecutive":   {"max_conseq_work": 40},
    "transition":    {"banned_day_after_eve": False, "ban_n_to_d": False, "ban_n_to_e": False},
}
_NURSE_LIMIT_KEYS = ("n_exact", "n_max", "n_min", "d_exact", "d_max", "e_exact", "e_max")


def _relax_nurse(nu: dict, family: str) -> dict:
    """per-nurse 계열 완화를 nurse dict 에 적용."""
    m = dict(nu)
    if family == "weekend_off":
        m["is_weekend_off"] = False
    elif family == "monthly_limit":
        for k in _NURSE_LIMIT_KEYS:
            m.pop(k, None)
    return m


def build_family_resolver(
    nurses: list[dict], config: dict, year: int, month: int, *,
    resolve_status_fn: Callable[[list[dict], dict, int, int], str],
    feasible_statuses: tuple[str, ...] = ("OPTIMAL", "FEASIBLE"),
    nurse_id_key: str = "nurse_id",
):
    """(family_resolve, instance_resolve, all_families) 생성.

    resolve_status_fn(nurses, config, year, month) -> status str.
    family_resolve(relaxed_families)/instance_resolve(family, relaxed_ids) -> feasible bool.
    """
    per_nurse_fams = {"weekend_off", "monthly_limit"}
    present = set(FAMILY_CONFIG_RELAX) | per_nurse_fams

    def _apply(relaxed_families, per_family_instances=None):
        cfg = dict(config)
        nn = [dict(n) for n in nurses]
        for f in relaxed_families:
            if f in FAMILY_CONFIG_RELAX:
                cfg.update(FAMILY_CONFIG_RELAX[f])
            elif f in per_nurse_fams:
                ids = None if per_family_instances is None else set(per_family_instances.get(f) or [])
                nn = [(_relax_nurse(n, f) if (ids is None or str(n.get(nurse_id_key)) in ids) else n)
                      for n in nn]
        return nn, cfg

    def family_resolve(relaxed_families):
        nn, cfg = _apply(set(relaxed_families))
        return resolve_status_fn(nn, cfg, year, month) in feasible_statuses

    def instance_resolve(family, relaxed_ids):
        # 그 family 는 지정 instance 만 완화, 나머지 family 는 전부 완화(고립 검증).
        others = set(present) - {family}
        nn, cfg = _apply(others | {family}, {family: list(relaxed_ids)})
        return resolve_status_fn(nn, cfg, year, month) in feasible_statuses

    return family_resolve, instance_resolve, sorted(present)


# ── 표시 정책: family → 유저 표현 버킷 (product 결정 반영) ────────────────────────
#   action   : "이 설정 바꾸기 [적용]" (관리자 조정 가능)
#   tradeoff : "풀리지만 대가 X" 경고 후 선택 (품질·안전 규칙)
#   advisory : auto-apply 없이 "확인하세요" (환자안전/데이터 — 함부로 안 낮춤)
_FAMILY_KO = {
    "off_budget": "OFF 일수", "night_cap": "야간 상한", "monthly_limit": "월 야간 한도",
    "weekend_off": "주말 휴무", "2n2off": "야간 후 2일 휴식", "night_recovery": "야간 회복 휴무",
    "not_one_night": "고립 야간 금지", "consecutive": "연속근무 상한", "transition": "근무 전환 규칙",
    "preceptee": "프리셉티 동반", "cross_month": "전월 경계", "coverage": "필요 인원",
    "grade": "등급 최소", "team": "팀 최소", "fixed_cell": "고정 근무(원티드/휴가)",
}
FAMILY_PRESENTATION: dict[str, dict[str, Any]] = {
    "monthly_limit": {"bucket": "action", "title": "{who} 월 야간 한도 조정",
                      "where": "간호사 관리 > 월 근무 한도(야간)"},
    "night_cap":     {"bucket": "action", "title": "{who} 월 야간 상한 조정",
                      "where": "간호사 관리 > 월 근무 한도(야간)"},
    "weekend_off":   {"bucket": "action", "title": "{who} 주말 휴무 해제",
                      "where": "간호사 관리 > 주말 휴무"},
    "off_budget":    {"bucket": "action", "title": "월 OFF 요구일수 낮추기",
                      "where": "설정 > OFF 일수"},
    "fixed_cell":    {"bucket": "action", "title": "특정 시프트에 몰린 고정셀(원티드/휴가) 조정",
                      "where": "원티드 조정판"},
    "2n2off":        {"bucket": "tradeoff", "tradeoff": "야간 후 휴식(2OFF) 보장이 약화됩니다."},
    "night_recovery":{"bucket": "tradeoff", "tradeoff": "야간 후 회복 휴무 보장이 약화됩니다."},
    "not_one_night": {"bucket": "tradeoff", "tradeoff": "하루짜리 고립 야간이 생길 수 있습니다."},
    "consecutive":   {"bucket": "tradeoff", "tradeoff": "연속근무가 늘어 피로가 누적될 수 있습니다."},
    "transition":    {"bucket": "tradeoff", "tradeoff": "야간 다음날 주간 전환이 생길 수 있습니다."},
    "preceptee":     {"bucket": "tradeoff", "tradeoff": "프리셉티 교육 동반이 약화됩니다."},
    "cross_month":   {"bucket": "tradeoff", "tradeoff": "전월 경계 회복 보장이 약화됩니다."},
    "coverage":      {"bucket": "advisory",
                      "advisory": "필요 인원을 낮추면 그 시프트 인원이 부족해집니다 — 환자안전상 직접 확인하세요."},
    "grade":         {"bucket": "advisory",
                      "advisory": "Grade 최소 인원을 낮추면 등급 인계 품질이 저하됩니다 — 직접 확인하세요."},
    "team":          {"bucket": "advisory",
                      "advisory": "팀 최소 인원을 낮추면 팀 인계 품질이 저하됩니다 — 직접 확인하세요."},
    "night_coverage":{"bucket": "advisory",
                      "advisory": "야간 가능 인원이 부족해 회복 규칙상 매일 다 못 섭니다 — "
                                  "야간 가능 인원을 늘리거나(근무유형에 N 추가) 야간 필요 인원을 "
                                  "줄여 주세요."},
}
_FAMILY_KO["night_coverage"] = "야간 커버리지"


def cause_to_resolution_options(
    classification: str, top_family: str | None, targets: list[dict] | None,
) -> list[dict[str, Any]]:
    """explain_infeasibility 결과 → 모달 resolution_options **단일 통합 카드**.

    문제 간호사 전원의 조정(야간 한도 초과 → 각자 상한으로 낮춤 + 주말휴무 병목 →
    해당 간호사만 해제)을 **한 카드**에 담아 원클릭 auto_apply. 간호사별 자기 축만
    처리(주말휴무 아닌 간호사는 야간만). per-nurse target 없으면(coupled/policy 등)
    family 안내 카드 1장.
    """
    targets = targets or []

    # ── 금지근무(banned-OFF)×강제OFF 모순 → 대안 2개(HN 선택) ──────────────────
    #   OFF 금지가 필수 휴무와 겹쳐 안 됨. (A) 그 금지근무 해제  또는  (B) 근무유형에
    #   D/E 추가(N전담 병목 해소). "또는"이라 한 카드로 묶지 않고 두 옵션으로 제시.
    bw = [t for t in targets if t.get("family") == "banned_wanted" and t.get("nurse_id")]
    if bw:
        n_nurses = len({t["nurse_id"] for t in bw})
        rel = [{"nurse_id": t["nurse_id"], "days": t.get("clash_days") or t.get("banned_off_days") or []}
               for t in bw]
        rel_changes = [{"nurse_id": t["nurse_id"], "config_key": "banned_wanted",
                        "label_ko": f"{t.get('name') or t['nurse_id']} 금지근무",
                        "from": None, "to": "해제"} for t in bw]
        add = [{"nurse_id": t["nurse_id"], "add": ["D", "E"]} for t in bw]
        add_changes = [{"nurse_id": t["nurse_id"], "config_key": "allowed_shifts",
                        "label_ko": f"{t.get('name') or t['nurse_id']} 근무유형",
                        "from": "".join(t.get("allowed_shifts") or ["N"]), "to": "D·E 추가"}
                       for t in bw]
        return [
            {"option_id": "cause:banned_release", "kind": "relax_constraint",
             "source": "cause", "verified": False,
             "title_ko": f"문제 간호사 {n_nurses}명 금지근무 해제하고 다시 만들기",
             "trade_off_ko": "겹치는 날의 금지근무(OFF 금지)를 풀어 필수 휴무가 가능해집니다.",
             "changes": rel_changes, "banned_wanted_release": rel,
             "fix": {"mode": "auto_apply", "where": "nurse.banned_wanted",
                     "where_label_ko": "원티드 조정판 > 금지근무"}},
            {"option_id": "cause:allowed_add", "kind": "relax_constraint",
             "source": "cause", "verified": False,
             "title_ko": f"문제 간호사 {n_nurses}명 근무유형에 D/E 추가하고 다시 만들기",
             "trade_off_ko": "야간 전담 대신 주간/이브닝도 가능해져 병목이 풀립니다(역할 변경).",
             "changes": add_changes, "allowed_shift_add": add,
             "fix": {"mode": "auto_apply", "where": "nurse.allowed_shifts",
                     "where_label_ko": "간호사 관리 > 근무 유형"}},
        ]

    ml = [t for t in targets
          if t.get("family") in ("monthly_limit", "night_cap")
          and t.get("nurse_id") and t.get("cap") is not None]
    wk = [t for t in targets if t.get("family") == "weekend_off" and t.get("nurse_id")]

    if ml or wk:
        changes: list[dict[str, Any]] = []
        monthly_limit_release: list[dict[str, Any]] = []
        weekend_off_release: list[str] = []
        for t in ml:
            nm = t.get("name") or t["nurse_id"]
            # n_max(상한)로 완화: 야간을 '정확히 N'이 아니라 '최대 N 까지'로 묶는다.
            # 솔버가 그 아래로 자유롭게 배정 → 근무표 품질 유리. 적용 경로가 하한(n_min)이
            # 새 상한보다 크면 함께 해제하므로 하한충돌(예: min13 > max7)도 풀린다.
            monthly_limit_release.append({"nurse_id": t["nurse_id"], "field": "n_max",
                                          "value": int(t["cap"])})
            changes.append({"nurse_id": t["nurse_id"], "config_key": "n_max",
                            "label_ko": f"{nm} 월 야간 최대",
                            "from": t.get("current"), "to": int(t["cap"])})
        for t in wk:
            nm = t.get("name") or t["nurse_id"]
            weekend_off_release.append(t["nurse_id"])
            changes.append({"nurse_id": t["nurse_id"], "config_key": "weekend_off",
                            "label_ko": f"{nm} 주말 휴무", "from": None, "to": "해제"})
        n_nurses = len({t["nurse_id"] for t in ml + wk})
        return [{
            "option_id": "cause:fix_all_personal",
            "kind": "relax_constraint", "source": "cause", "verified": False,
            "title_ko": f"문제 간호사 {n_nurses}명 설정 조정하고 다시 만들기",
            "trade_off_ko": "해당 간호사들의 야간/주말 설정이 아래대로 바뀝니다.",
            "changes": changes,
            "monthly_limit_release": monthly_limit_release,
            "weekend_off_release": weekend_off_release,
            "fix": {"mode": "auto_apply", "where": "nurse.monthly_limit+weekend_off",
                    "where_label_ko": "간호사 관리 > 월 근무 한도 · 주말 휴무"},
        }]

    # per-nurse target 없음(coupled/policy/coverage) → family 안내 카드 1장.
    if not top_family:
        return []
    pres = FAMILY_PRESENTATION.get(top_family, {"bucket": "advisory",
                                                "advisory": f"'{top_family}' 설정을 확인하세요."})
    bucket = pres["bucket"]
    opt: dict[str, Any] = {"option_id": f"cause:{top_family}", "kind": "relax_constraint",
                           "source": "cause", "verified": False,
                           "family": top_family, "bucket": bucket}
    if bucket == "tradeoff":
        opt["title_ko"] = f"{_FAMILY_KO.get(top_family, top_family)} 완화"
        opt["trade_off_ko"] = pres["tradeoff"]
        opt["fix"] = {"mode": "manual", "where": f"설정 > 근무 규칙 > {top_family}"}
    elif bucket == "action":
        opt["title_ko"] = pres["title"].format(who="해당")
        opt["where_label_ko"] = pres.get("where")
        opt["fix"] = {"mode": "manual", "where": pres.get("where")}
    else:
        opt["title_ko"] = pres["advisory"]
        opt["fix"] = {"mode": "manual", "where": None}
    return [opt]


def trace_to_user_options(trace: ConflictTrace) -> list[dict[str, Any]]:
    """ConflictTrace → 유저 표시 옵션 리스트. 완화(내부)는 숨기고 행동·대가·안내로 번역."""
    opts: list[dict[str, Any]] = []
    for fam in trace.families:
        pres = FAMILY_PRESENTATION.get(fam, {"bucket": "advisory",
                                             "advisory": f"'{fam}' 관련 설정을 확인하세요."})
        who_list = trace.instances_by_family.get(fam) or []
        who = ", ".join(str(w) for w in who_list) if who_list else ""
        bucket = pres["bucket"]
        opt: dict[str, Any] = {"family": fam, "bucket": bucket, "targets": who_list}
        if bucket == "action":
            opt["title_ko"] = pres["title"].format(who=who or "해당")
            opt["where_ko"] = pres.get("where")
            opt["actionable"] = True
        elif bucket == "tradeoff":
            opt["title_ko"] = (f"{_FAMILY_KO.get(fam, fam)} 완화" + (f" ({who})" if who else ""))
            opt["trade_off_ko"] = pres["tradeoff"]
            opt["actionable"] = True
        else:  # advisory
            opt["title_ko"] = pres["advisory"]
            opt["actionable"] = False
        opts.append(opt)
    return opts
