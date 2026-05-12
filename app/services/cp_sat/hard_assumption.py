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

        # 결론 텍스트 정교화 (member 수에 따라 자연어 분기)
        def _quantifier(n: int) -> str:
            return "둘이" if n == 2 else "셋이" if n == 3 else f"{n}개가"

        for core in grouped.values():
            members = core["members"]
            if members:
                names = ", ".join(m.get("label") or m.get("assumption_name") for m in members)
                core["conclusion"] = f"CP-SAT MUS: {{ {names} }} {_quantifier(len(members))} 동시 만족 불가"

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
                deduped.extend(group)
                continue
            # 같은 패턴 그룹 합치기 — 대표 core를 골라 affected_nurse_ids 추가
            rep = group[0]
            affected = [c.get("nurse_id") for c in group if c.get("nurse_id")]
            affected = sorted(set(str(a) for a in affected if a is not None))
            rep_member_types = sorted(set(str(m.get("type")) for m in rep["members"]))
            collapsed_core_id = f"conflict:cpsat:group:{rep.get('pattern')}"
            collapsed = {
                "core_id": collapsed_core_id,
                "scope": "multi_nurse",
                "pattern": rep.get("pattern"),
                "nurse_id": None,
                "solver_phase": solver_phase,
                "source": f"cpsat_mus_{solver_phase}",
                "affected_nurse_ids": affected,
                "affected_count": len(affected),
                # 멤버는 type 단위로 1개씩만 (대표) — 자세한 nurse별 멤버는
                # per_nurse_cores 리스트에 보존
                "members": [
                    {
                        "node_id": f"member_type_summary:{t}",
                        "type": t,
                        "label": f"{t} × {len(affected)} nurses",
                        "value": None,
                        "human_message_ko": f"{t} 제약이 {len(affected)} 명의 nurse 에 동시 binding",
                    }
                    for t in rep_member_types
                ],
                "derivation": [
                    {"step": 1, "from": "CP-SAT solver",
                     "infer": f"같은 멤버 타입 셋 ({', '.join(rep_member_types)})이 {len(affected)} 명에 동시 unsat"},
                    {"step": 99, "conclusion":
                     f"affected: {', '.join(affected[:5])}"
                     + (f" (+{len(affected)-5}명)" if len(affected) > 5 else "")},
                ],
                "conclusion": (
                    f"CP-SAT MUS: 같은 패턴 ({', '.join(rep_member_types)}) 이 "
                    f"{len(affected)} 명에 동시 binding"
                ),
                "resolution_hints": rep.get("resolution_hints") or [],
                "human_message_ko": (
                    f"이 충돌은 {len(affected)} 명의 간호사에 동시 발생. "
                    "공통 제약(전역 cap 또는 같은 정책)을 풀면 일괄 해소될 가능성."
                ),
                "source": "cpsat_mus",
                "per_nurse_cores": [c["core_id"] for c in group],
            }
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
