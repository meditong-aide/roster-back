"""CP-SAT hard-assumption registry — INFEASIBLE 시 MUS(최소 충돌 코어)를
사용자가 조절 가능한 정책 단위로 추출하기 위한 인프라.

사용 패턴:
    from services.cp_sat.hard_assumption import HardAssumptionRegistry, add_hard

    registry = HardAssumptionRegistry(model)
    # 정책 hard 제약을 wrap
    add_hard(
        model, registry,
        name=f"OffCap:nurse_{n}",
        constraint_expr=(off_sum <= max_off),
        meta={
            "node_id": f"off_cap:nurse_{n}",
            "type": "OffCapNode",
            "label": "max_off (effective)",
            "value": max_off,
            "human_message_ko": "...",
            "scope": "nurse",
            "scope_key": f"nurse_{n}",
            "pattern": "off_cap",
        },
    )

    # 모델 build 끝나면
    registry.attach_to_model()

    # solver.Solve(model) 후
    if status == cp_model.INFEASIBLE:
        cores = registry.extract_conflict_cores(solver)
        # cores → roster_system._cpsat_conflict_cores

scope_key는 같은 group의 assumption들을 dashboard에서 묶기 위한 키.
예: nurse_4 의 OFF cap, max_night, n_exact assumption이 같이 binding이면
같은 ConflictCore의 members 가 되도록 scope_key = "nurse_4" 로 통일.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Causal layer taxonomy
#
# 각 ConflictCore 의 root vs cascade 판별을 위한 분류 체계. dashboard 는
# `causal_layer` 가 ``policy`` 인 core 를 root 후보로 핀하고, ``structural`` /
# ``personal`` 인 core 는 cascade 로 접어둔다.
# ---------------------------------------------------------------------------

#: ``meta.type`` (member node type) → causal layer 매핑. 등록 안 된 타입은
#: ``"unknown"`` 으로 처리되어 cascade 보다도 아래로 떨어진다.
TYPE_TO_LAYER: Dict[str, str] = {
    # policy — 사용자가 직접 설정한 정책 hard 제약
    "TeamMinNode": "policy",
    "TeamMaxNode": "policy",
    "GradeMinNode": "policy",
    "GradeMaxNode": "policy",
    "CoverageMinNode": "policy",
    "CoverageMaxNode": "policy",
    "MonthlyLimitDMinNode": "policy",
    "MonthlyLimitDMaxNode": "policy",
    "MonthlyLimitEMinNode": "policy",
    "MonthlyLimitEMaxNode": "policy",
    "MonthlyLimitNMinNode": "policy",
    "MonthlyLimitNMaxNode": "policy",
    "MonthlyLimitOMinNode": "policy",
    "MonthlyLimitOMaxNode": "policy",
    # data — 입력 마스터 데이터 fact (사용자 데이터 입력 단계)
    "ForbiddenCellNode": "data",
    "AllowedShiftMaskNode": "data",
    # personal — 개별 nurse 에 박힌 cap / 강제
    "OffCapNode": "personal",
    "NightCapNode": "personal",
    "MonthlyNExactNode": "personal",
    "MonthlyDExactNode": "personal",
    "MonthlyEExactNode": "personal",
    "MonthlyOExactNode": "personal",
    "FixedWantedNode": "personal",
    "WantedSubmissionNode": "personal",
    "WantedApplyNode": "personal",
    "PrecepteeSyncNode": "personal",
    # structural — 모델 규칙 (모든 nurse 에 자동 적용)
    "ConsecutiveWorkNode": "structural",
    "ConsecutiveNightCapNode": "structural",
    "RecoveryOffNode": "structural",
    "TransitionBanNode": "structural",
    "NotOneNightNode": "structural",
    "OffWindowNode": "structural",
    "CarryoverTransitionNode": "structural",
}

#: 우선순위 (작은 인덱스 = 더 root 다움).
LAYER_PRIORITY: List[str] = ["policy", "data", "personal", "structural", "unknown"]


def classify_member_type(node_type: Optional[str]) -> str:
    """Member 의 ``type`` 으로 causal layer 결정. 미등록 타입은 ``"unknown"``."""
    if not node_type:
        return "unknown"
    return TYPE_TO_LAYER.get(str(node_type), "unknown")


def derive_core_layer(members: List[Dict[str, Any]]) -> str:
    """Member 들 중 가장 root-y 한 layer 를 코어 대표 layer 로 반환."""
    if not members:
        return "unknown"
    layers = {classify_member_type(m.get("type")) for m in members}
    for lyr in LAYER_PRIORITY:
        if lyr in layers:
            return lyr
    return "unknown"


def per_layer_counts(members: List[Dict[str, Any]]) -> Dict[str, int]:
    """Member type 들의 layer 별 빈도. dashboard 가 breakdown 표시할 때 사용."""
    counts: Dict[str, int] = {lyr: 0 for lyr in LAYER_PRIORITY}
    for m in members or []:
        lyr = classify_member_type(m.get("type"))
        counts[lyr] = counts.get(lyr, 0) + 1
    # 0 짜리는 제거해 dict 가 가벼워지도록
    return {k: v for k, v in counts.items() if v > 0}


class HardAssumption:
    __slots__ = ("lit", "name", "meta")

    def __init__(self, lit, name: str, meta: dict):
        self.lit = lit
        self.name = name
        self.meta = meta


class HardAssumptionRegistry:
    """모델에 assumption literal로 묶인 hard 제약들을 관리.

    동일 (name) 이 반복 add 되면 같은 literal을 재사용. 같은 정책의 여러
    `m.Add(...)` 호출이 하나의 literal로 묶일 수 있다.
    """

    def __init__(self, model):
        self.model = model
        self._by_name: Dict[str, HardAssumption] = {}
        self._by_index: Dict[int, HardAssumption] = {}
        self._attached = False

    def create_literal(self, name: str, meta: Optional[dict] = None) -> Any:
        """assumption literal 가져오기 — 같은 name이면 기존 literal 반환.

        meta 는 첫 등록 시 저장. 이후 추가 호출의 meta는 무시 (보강하려면
        merge_meta=True 같은 별도 분기 필요. 지금은 단순 first-wins.)
        """
        existing = self._by_name.get(name)
        if existing is not None:
            return existing.lit
        lit = self.model.NewBoolVar(f"assume__{name}")
        a = HardAssumption(lit, name, dict(meta or {}))
        self._by_name[name] = a
        self._by_index[lit.Index()] = a
        return lit

    def attach_to_model(self) -> None:
        """모든 assumption literal을 model의 AddAssumptions 에 등록.

        OR-Tools CP-SAT 의 model.AddAssumptions(...) 는 solver가
        제약을 강제로 True 로 가정하게 함. INFEASIBLE 시
        SufficientAssumptionsForInfeasibility() 로 충돌 코어 literal
        인덱스를 받아올 수 있다.
        """
        if self._attached:
            return
        try:
            self.model.AddAssumptions([a.lit for a in self._by_name.values()])
            self._attached = True
        except Exception as e:
            # CP-SAT 버전에 따라 AddAssumptions 미지원 시 silent
            print(f"[HardAssumption] AddAssumptions failed (ignore): {e}")

    def extract_conflict_cores(self, solver, *, solver_phase: str = "primary") -> List[Dict[str, Any]]:
        """solver.SufficientAssumptionsForInfeasibility() 결과를 받아
        scope_key 단위로 그룹핑해 ConflictCore 형식으로 반환.

        같은 scope_key (예: "nurse_4") 의 assumption 들이 한 ConflictCore의
        members가 된다 — dashboard에서 "이 nurse의 충돌 구성" 처럼 묶여 노출.

        Args:
            solver_phase: "primary" | "fallback" — 이 MUS가 어느 솔버 단계에서
                추출됐는지. dashboard가 시각·해석을 분기하는 데 사용한다.
                fallback 단계는 일부 hard 제약이 soft로 풀린 상태라 충돌 집합이
                primary 와 다를 수 있음.
        """
        try:
            indices = list(solver.SufficientAssumptionsForInfeasibility())
        except Exception as e:
            print(f"[HardAssumption] SufficientAssumptionsForInfeasibility failed: {e}")
            return []

        if not indices:
            return []

        # scope_key 별로 묶기
        grouped: Dict[str, Dict[str, Any]] = {}
        for idx in indices:
            a = self._by_index.get(idx)
            if a is None:
                continue
            scope_key = str(a.meta.get("scope_key") or a.name)
            scope = str(a.meta.get("scope") or "global")
            pattern = str(a.meta.get("pattern") or "cpsat_mus")
            core_id = f"conflict:cpsat:{scope_key}"

            if core_id not in grouped:
                grouped[core_id] = {
                    "core_id": core_id,
                    "scope": scope,
                    "pattern": f"cpsat_mus:{pattern}",
                    "nurse_id": a.meta.get("nurse_id"),
                    "members": [],
                    "derivation": [
                        {"step": 1, "from": f"CP-SAT solver ({solver_phase})",
                         "infer": "model.AddAssumptions(...) 로 묶인 hard 제약 중 다음 셋이 동시 만족 불가"},
                    ],
                    "conclusion": "CP-SAT MUS: 다음 제약이 동시에 만족될 수 없습니다",
                    "resolution_hints": [],
                    "human_message_ko": (
                        f"CP-SAT 솔버({solver_phase})가 식별한 최소 충돌 집합 (deletion-MUS)."
                        " 이 중 하나를 풀면 feasible 해질 가능성이 높습니다."
                    ),
                    "source": f"cpsat_mus_{solver_phase}",
                    "solver_phase": solver_phase,
                }

            grouped[core_id]["members"].append({
                "node_id": str(a.meta.get("node_id") or a.name),
                "type": str(a.meta.get("type") or "ConstraintNode"),
                "label": str(a.meta.get("label") or a.name),
                "value": a.meta.get("value"),
                "human_message_ko": a.meta.get("human_message_ko"),
                "assumption_name": a.name,
            })

            # resolution hint: 각 member 자체가 후보. 첫 등록 시 한 번씩만.
            hint_msg = a.meta.get("resolution_hint")
            if hint_msg and not any(h.get("human_message_ko") == hint_msg
                                    for h in grouped[core_id]["resolution_hints"]):
                grouped[core_id]["resolution_hints"].append({
                    "action": f"relax_{a.meta.get('pattern') or a.name}",
                    "human_message_ko": hint_msg,
                })

        # ── min/max pairing: 같은 (grade, shift) 의 grade_min ∧ grade_max 가
        #    같은 MUS 안에 동시 등장하면 한 코어로 병합한다 (reversed config 등).
        #    scope_key 명명은 wrap 측에서 "grade_{g}_{shift}_{min|max}" 로 고정.
        import re as _re
        _pair_re = _re.compile(r"^grade_(\d+)_([a-z]+)_(min|max)$")
        _by_base: Dict[str, Dict[str, str]] = {}
        for cid in list(grouped.keys()):
            sk = cid.replace("conflict:cpsat:", "")
            mm = _pair_re.match(sk)
            if not mm:
                continue
            base = f"grade_{mm.group(1)}_{mm.group(2)}"
            _by_base.setdefault(base, {})[mm.group(3)] = cid
        for base, sides in _by_base.items():
            if "min" not in sides or "max" not in sides:
                continue
            min_cid, max_cid = sides["min"], sides["max"]
            min_core, max_core = grouped[min_cid], grouped[max_cid]
            merged_id = f"conflict:cpsat:{base}_minmax"
            _parts = base.split("_")  # ["grade", "<g>", "<shift>"]
            _g, _sc = _parts[1], _parts[2].upper()
            grouped[merged_id] = {
                "core_id": merged_id,
                "scope": "grade",
                "pattern": "cpsat_mus:grade_minmax_conflict",
                "nurse_id": None,
                "members": list(min_core["members"]) + list(max_core["members"]),
                "derivation": [
                    {"step": 1, "from": f"CP-SAT solver ({solver_phase})",
                     "infer": (
                         f"같은 (Grade {_g}, {_sc} 시프트) 의 min·max 가 "
                         "동시에 만족 불가 — 설정 반전(min > max) 또는 "
                         "다른 hard 제약과의 결합 가능성"
                     )},
                ],
                "conclusion": (
                    f"CP-SAT MUS: Grade {_g} {_sc} 의 min 과 max 가 동시 충돌"
                ),
                "resolution_hints": (
                    list(min_core.get("resolution_hints") or [])
                    + list(max_core.get("resolution_hints") or [])
                ),
                "human_message_ko": (
                    f"Grade {_g} 등급의 {_sc} 시프트는 min 과 max 가 동시에 "
                    "충돌합니다. 두 정책 중 하나를 완화하거나 (min ≤ max) 가 "
                    "되도록 재설정하세요."
                ),
                "source": f"cpsat_mus_{solver_phase}",
                "solver_phase": solver_phase,
            }
            grouped.pop(min_cid, None)
            grouped.pop(max_cid, None)

        # 결론 텍스트 정교화 (member 수에 따라 자연어 분기)
        def _quantifier(n: int) -> str:
            return "둘이" if n == 2 else "셋이" if n == 3 else f"{n}개가"

        for core in grouped.values():
            members = core["members"]
            if members:
                names = ", ".join(m.get("label") or m.get("assumption_name") for m in members)
                core["conclusion"] = f"CP-SAT MUS: {{ {names} }} {_quantifier(len(members))} 동시 만족 불가"
            # causal layer (root vs cascade 판정)
            core["causal_layer"] = derive_core_layer(members)
            core["per_layer_counts"] = per_layer_counts(members)

        # ── Dedupe: 같은 (pattern + member type signature) 의 nurse 단위 cores를
        # 1개 group core 로 묶는다. 동일 패턴이 여러 nurse에 동시 발생할 때
        # dashboard에 N개 카드 대신 "n명에 동일 패턴" 1개로 요약.
        per_signature: Dict[str, List[Dict[str, Any]]] = {}
        for core in grouped.values():
            sig_types = sorted(str(m.get("type") or "?") for m in core["members"])
            sig = f"{core.get('pattern')}|{':'.join(sig_types)}"
            per_signature.setdefault(sig, []).append(core)

        deduped: List[Dict[str, Any]] = []
        for sig, group in per_signature.items():
            if len(group) <= 1:
                # singleton: affected_count=1 + affected_nurse_ids=[nurse_id] 보강
                for c in group:
                    if c.get("affected_count") is None:
                        c["affected_count"] = 1
                        nid = c.get("nurse_id")
                        c["affected_nurse_ids"] = [nid] if nid else []
                    # singleton 도 causal_layer 보존되어 있어야 함 (위에서 set 됨)
                    if "causal_layer" not in c:
                        c["causal_layer"] = derive_core_layer(c.get("members") or [])
                        c["per_layer_counts"] = per_layer_counts(c.get("members") or [])
                deduped.extend(group)
                continue
            # 같은 패턴 그룹 합치기.
            # nurse-scoped (nurse_id 존재) → 기존대로 "n명에 동시 binding" 요약.
            # 비-nurse scope (예: grade, global) → scope_key 별 멤버를 보존해
            # "Grade 1 D min, Grade 2 D min …" 처럼 구체 정책 단위로 노출.
            rep = group[0]
            affected_nurses = sorted(set(
                str(c.get("nurse_id")) for c in group if c.get("nurse_id")
            ))
            rep_member_types = sorted(set(str(m.get("type")) for m in rep["members"]))
            is_nurse_scoped = bool(affected_nurses)

            if is_nurse_scoped:
                collapsed_core_id = f"conflict:cpsat:group:{rep.get('pattern')}"
                collapsed = {
                    "core_id": collapsed_core_id,
                    "scope": "multi_nurse",
                    "pattern": rep.get("pattern"),
                    "nurse_id": None,
                    "solver_phase": solver_phase,
                    "source": f"cpsat_mus_{solver_phase}",
                    "affected_nurse_ids": affected_nurses,
                    "affected_count": len(affected_nurses),
                    # 멤버는 type 단위로 1개씩만 (대표) — 자세한 nurse별 멤버는
                    # per_nurse_cores 리스트에 보존
                    "members": [
                        {
                            "node_id": f"member_type_summary:{t}",
                            "type": t,
                            "label": f"{t} × {len(affected_nurses)} nurses",
                            "value": None,
                            "human_message_ko": f"{t} 제약이 {len(affected_nurses)} 명의 nurse 에 동시 binding",
                        }
                        for t in rep_member_types
                    ],
                    "derivation": [
                        {"step": 1, "from": "CP-SAT solver",
                         "infer": f"같은 멤버 타입 셋 ({', '.join(rep_member_types)})이 {len(affected_nurses)} 명에 동시 unsat"},
                        {"step": 99, "conclusion":
                         f"affected: {', '.join(affected_nurses[:5])}"
                         + (f" (+{len(affected_nurses)-5}명)" if len(affected_nurses) > 5 else "")},
                    ],
                    "conclusion": (
                        f"CP-SAT MUS: 같은 패턴 ({', '.join(rep_member_types)}) 이 "
                        f"{len(affected_nurses)} 명에 동시 binding"
                    ),
                    "resolution_hints": rep.get("resolution_hints") or [],
                    "human_message_ko": (
                        f"이 충돌은 {len(affected_nurses)} 명의 간호사에 동시 발생. "
                        "공통 제약(전역 cap 또는 같은 정책)을 풀면 일괄 해소될 가능성."
                    ),
                    "source": "cpsat_mus",
                    "per_nurse_cores": [c["core_id"] for c in group],
                }
            else:
                # 비-nurse scope (grade 등): 각 core 의 members 를 펼쳐 모아
                # scope_key 단위로 dedupe (같은 (grade, shift) 의 여러 day 가
                # 한 literal 로 묶여 있어 보통 1 member). resolution_hints 도 합산.
                merged_members: List[Dict[str, Any]] = []
                seen_member_keys: set = set()
                merged_hints: List[Dict[str, Any]] = []
                seen_hint_msgs: set = set()
                affected_scope_keys: List[str] = []
                for c in group:
                    sk = str(c.get("core_id") or "").replace("conflict:cpsat:", "")
                    if sk:
                        affected_scope_keys.append(sk)
                    for m in c.get("members") or []:
                        key = (m.get("node_id"), m.get("type"), m.get("label"))
                        if key in seen_member_keys:
                            continue
                        seen_member_keys.add(key)
                        merged_members.append(m)
                    for h in c.get("resolution_hints") or []:
                        msg = h.get("human_message_ko") or ""
                        if msg in seen_hint_msgs:
                            continue
                        seen_hint_msgs.add(msg)
                        merged_hints.append(h)
                affected_scope_keys = sorted(set(affected_scope_keys))
                collapsed_core_id = f"conflict:cpsat:group:{rep.get('pattern')}"
                _scope = str(rep.get("scope") or "group")
                collapsed = {
                    "core_id": collapsed_core_id,
                    "scope": _scope,
                    "pattern": rep.get("pattern"),
                    "nurse_id": None,
                    "solver_phase": solver_phase,
                    "source": f"cpsat_mus_{solver_phase}",
                    "affected_scope_keys": affected_scope_keys,
                    "affected_count": len(affected_scope_keys),
                    "members": merged_members,
                    "derivation": [
                        {"step": 1, "from": f"CP-SAT solver ({solver_phase})",
                         "infer": (
                             f"같은 패턴({rep.get('pattern')}) 의 정책 "
                             f"{len(affected_scope_keys)} 건이 동시에 만족 불가"
                         )},
                        {"step": 99, "conclusion":
                         f"affected policies: {', '.join(affected_scope_keys[:5])}"
                         + (f" (+{len(affected_scope_keys)-5}건)" if len(affected_scope_keys) > 5 else "")},
                    ],
                    "conclusion": (
                        f"CP-SAT MUS: {len(affected_scope_keys)} 개의 {_scope} "
                        f"정책이 동시 만족 불가"
                    ),
                    "resolution_hints": merged_hints,
                    "human_message_ko": (
                        f"{len(affected_scope_keys)} 건의 {_scope} 정책이 동시에 만족 불가. "
                        "아래 정책 중 하나를 완화하면 해소될 가능성이 높습니다."
                    ),
                    "source": "cpsat_mus",
                    "per_member_cores": [c["core_id"] for c in group],
                }
            # collapsed core 의 causal_layer 는 멤버 타입(또는 그룹 첫 core)에서 derive
            collapsed_members = collapsed.get("members") or []
            collapsed["causal_layer"] = derive_core_layer(collapsed_members)
            collapsed["per_layer_counts"] = per_layer_counts(collapsed_members)
            deduped.append(collapsed)

        return deduped


def add_hard(model, registry: HardAssumptionRegistry, *, name: str,
             constraint_expr, meta: Optional[dict] = None) -> Any:
    """정책 hard 제약을 assumption literal로 감싸 모델에 추가.

    Args:
        model: CP-SAT cp_model.CpModel
        registry: HardAssumptionRegistry
        name: assumption literal 식별자. 같은 name 반복 호출 시 같은 literal 재사용
              (한 정책의 여러 m.Add(...)를 묶을 때 유용).
        constraint_expr: m.Add() 에 넘길 표현식 (예: off_sum <= 16)
        meta: dashboard / agent 가 읽을 메타데이터. 권장 키:
              node_id, type, label, value, scope, scope_key, pattern,
              nurse_id, human_message_ko, resolution_hint

    Returns:
        assumption literal (BoolVar)
    """
    lit = registry.create_literal(name, meta)
    try:
        model.Add(constraint_expr).OnlyEnforceIf(lit)
    except Exception as e:
        # OnlyEnforceIf 미지원 표현식이면 단순 Add 로 fallback (assumption 미적용)
        print(f"[HardAssumption] OnlyEnforceIf failed for {name}, falling back: {e}")
        model.Add(constraint_expr)
    return lit
