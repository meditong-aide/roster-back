# OR Evaluation of the Hypergraph Design

**Date:** 2026-05-15
**Status:** Evaluation
**Companion documents:**
- `/tmp/ontology_audit_track_c_or_evaluation.md` — academic literature survey across LP/CO/IP/DP/NF (38 KB, 517 lines, 17 references)
- `/tmp/ontology_audit_track_d_codebase_or_fit.md` — current codebase OR paradigm mapping + fit analysis
- `docs/ONTOLOGY_HYPERGRAPH_DESIGN.md` — the design being evaluated

**Question evaluated:** Is the proposed *directed hypergraph + dynamic hitting set + OCUS cost ranking + per-ward preference profile* direction sound under OR scrutiny, or is there a better alternative? Specifically for *our* codebase and goal.

---

## 0. One-line conclusion

**Keep the direction. Add 3 reinforcements. Reject 4 OR alternatives.** Two independent investigations (literature survey + codebase audit) converge on the same answer.

---

## 1. What's already in the codebase (Track D)

The system already implements 4 of 6 OR paradigms strongly:

| Paradigm | Where | Status |
|---|---|---|
| Constraint Programming | `cp_sat_basic.py:2285-4549` `_build_full_model` | **Strong** — 22+ hard constraints, reified literals |
| Combinatorial Optimization | `hard_assumption.py:54-92` causal_layer + `fallback_lex.py:129-164` 3-stage lex | **Strong** — already ATMS-like + goal-programming-like |
| Integer Programming | reified `OnlyEnforceIf` + slack patterns in `grade_constraints.py`, `team_constraints.py` | **Strong** |
| Linear Programming (slack) | `fallback_lex.py:537-538` coverage slack vars | **Medium** |
| Network Flow | absent | **Weak** — only arithmetic capacity checks |
| Dynamic Programming | `feasibility_alerts.py:110-124` subset-sum only | **Weak** |

Key existing assets that the hypergraph design can *reuse* without modification:
- `HardAssumptionRegistry.extract_conflict_cores()` — MUS extraction by scope, already 4-layer causal taxonomy (policy/data/personal/structural). **This is already an ATMS prototype.**
- `fallback_lex.py` 3-stage (coverage > safety > quality) — **already goal programming preemptive lex** (Charnes & Cooper).
- `constraint_impact/control.py` 4 actions (disable / force_soft / set_threshold / narrow_scope) — **already programmatic treatment vocabulary**.
- `ontology.yaml` `relaxation_priority` (1-5) + `scope_explosion` + `tier` (T0-T3) — **already 2D cost basis for ranking**.
- `conflict_probe.py:66-123` — heuristic ranking by `6 - priority + explosion_penalty + scenario_hits` — **already an OCUS-cost proxy** (formula simpler than full OCUS).
- `cp_sat_basic_lagrangian.py` — Lagrangian relaxation **already tried, abandoned** because LNS outperformed it. Strong negative evidence: decomposition is not our path.

**Roughly half of the hypergraph design's machinery already exists** — what we are building is a *formalization* and *unification* of patterns already operational in the codebase.

---

## 2. What the literature says (Track C)

Five OR sub-fields surveyed (17 references). Each is mapped onto our specific situation.

### 2.1 The direction is supported

- **MUS ↔ MCS hitting-set duality** (Liffiton & Sakallah 2008) — formally grounds our `(cause set) ──treatment──▶` arc as the dual of MUS enumeration. Reiter '87 HS-tree is the canonical algorithm.
- **OCUS** (Gamba/Bogaerts/Guns 2021) — gives an *optimization-grade* version of the hitting set with a cost function. Cost-ranked bundle recommendation maps directly to our `OCUS-style cost ranking`.
- **Implicit Hitting Set (IHS)** (Davies & Bacchus 2011) — for problems with few MUSes (NRP usually has ≤ 5 cores per infeasibility), IHS via SAT-oracle (`pysathq.github.io` `hitman`) is faster than explicit HS-tree.
- **Boolean Lexicographic Optimization** — independently confirms the soundness of our 3-stage fallback's preemptive ordering.
- **Constraint Hierarchies** (Borning 1992) — formally supports our `forbid/avoid/neutral/prefer` 4-level ward profile as a discretization of a strength hierarchy.
- **Lagrangian dual multipliers** — same 4-level profile is *also* a discretized Lagrange multiplier (Charnes-Cooper preemptive Goal Programming), giving it dual theoretical support.
- **WCSP / per-ward weight customization** (Smet et al. 2020, KU Leuven) — empirically shown to improve NRP quality in production deployments.

### 2.2 Concrete reinforcements proposed

1. **Min-cost flow precheck overlay (rank 1 in ROI)** — Coverage-only bipartite/tripartite flow graph using OR-Tools' built-in `min_cost_flow` API, run *before* CP-SAT. Min-cut on the residual graph identifies the exact bottleneck location (which day, which shift, which team×grade intersection) in O(VE²). This is a *formal* version of our `_build_infeasible_diagnosis` arithmetic check, and the min-cut output maps 1:1 onto our 4-axis NO_ASSIGNMENT decomposition.
2. **IHS via `pysat.hitman` (rank 2)** — replace `conflict_probe.py:66-123` ad-hoc ranking with formal weighted-hitman algorithm. ~50-80 line refactor; pysat is a small dep but optional (we can also use a pure-Python greedy weighted set cover for v1).
3. **EvidenceNode metadata fields** (rank 3, ≤ a day) — explicit `is_minimal: false`, `core_size: N`, `proof_type: cp_sat_unsat_core_heuristic` to *document* that our unsat-core-based evidence is "minimized but not necessarily minimal." This honestly communicates the limit relative to a true MUS or Farkas certificate, without pretending to a stronger guarantee.

### 2.3 Rejections (with reasons)

| Alternative | Why rejected (academic) | Why rejected (codebase) |
|---|---|---|
| **Benders / LBBD decomposition** | Strong infeasibility cuts theoretically, but requires master/subproblem split | `cp_sat_basic.py` is monolithic (~5000 lines). 4-6 month refactor risk for marginal gain. CP-SAT unsat core post-grouping by nurse is a lightweight approximation. |
| **Full Network Flow rewrite** | Coverage is naturally NF, but sequential constraints (transition_ban, recovery, consecutive_nights) cannot be expressed in flow models without auxiliary variables that negate the benefit | Sequential constraints are 30%+ of `_build_full_model`. Replacing CP-SAT with NF is not viable. But **NF as a precheck overlay** (above) is high-value. |
| **MaxSAT (WPMS) full replacement** | Pure SAT layer, no global constraint propagators | Loses CP-SAT's `AllDifferent`, `Cumulative`, `Circuit` propagation. Strictly worse for NRP. |
| **Farkas certificate / DRAT proof for EvidenceNode** | Stronger theoretical guarantee | CP-SAT does not expose Farkas (it's an LP dual concept). DRAT is SAT-level and also not exposed. Switching to Gurobi/CPLEX adds a paid dependency. Not worth it. |
| **Lagrangian decomposition** (re-introduce) | Theoretically attractive | Already tried in `cp_sat_basic_lagrangian.py`. LNS outperformed it. Empirical negative evidence in our own codebase. |

---

## 3. The two tracks' convergence and minor divergence

**Convergence (high confidence — both tracks independently agree):**

1. ✅ Keep the directed hypergraph + dynamic hitting set + OCUS cost + ward profile direction.
2. ✅ Greedy/IHS weighted hitting set replaces ad-hoc ranking. Pure Python implementable; optional pysat upgrade.
3. ✅ Ward profile multiplies into **stage-3 weights only**, preserving lexicographic safety invariant (Track D explicit + Track C reasons about goal-programming preemptive priority being preserved).
4. ✅ Benders / MaxSAT / Farkas / Lagrangian re-intro all rejected.
5. ✅ `forbid/avoid/neutral/prefer` 4-level ward profile is theoretically valid (Lagrangian dual / Constraint Hierarchy / WCSP all agree).

**Minor divergence (resolved):**

- **Track D:** "Don't introduce Network Flow — sequential constraints break flow structure."
- **Track C:** "Min-cost flow precheck **overlay** is the top ROI improvement, OR-Tools API ready, 2-3 weeks."
- **Resolution:** Both correct at their level. Track D rejects *replacing* CP-SAT with NF (correct). Track C recommends *augmenting* the precheck layer with NF (correct — non-invasive, high diagnostic value, no risk to CP-SAT model).

**Final note:** The biggest risk identified (Track D) is unchanged: **CP-SAT statelessness — every re-solve is a full model rebuild (2-3s build + 5-30s solve)**. This means bundle apply → re-solve iterations take 6-33 seconds each. Fine for async SQS pipeline (current architecture). Slow for an interactive agent UI. Mitigation = future `_build_full_model` partial-rebuild refactor; not blocking for the current design.

---

## 4. Final recommendation — execution plan

Priorities and effort estimates (combining both tracks):

### Tier 1 — Highest ROI (do these)

| # | Item | Effort | Why |
|---|---|---|---|
| 1 | **Greedy weighted hitting set** over `extract_conflict_cores()` output, using `relaxation_priority` + `ward_profile_multiplier` as cost | 1 week (~80 LOC pure Python) | Formalizes ad-hoc `conflict_probe.py:66-123` ranking. Zero new deps. Replaces "ranking-by-score" with "hitting-set-by-cost." Both tracks recommend. |
| 2 | **Min-cost flow precheck overlay** using OR-Tools `min_cost_flow` | 2-3 weeks | Adds formal bottleneck diagnosis via min-cut. Maps onto our 4-axis NO_ASSIGNMENT decomposition. Runs before CP-SAT, reduces CP-SAT calls for clear-cut capacity cases. Track C strongly recommends; Track D considers it safe as an overlay. |
| 3 | **EvidenceNode metadata** (`is_minimal: false`, `core_size: N`, `proof_type: "cp_sat_unsat_core_heuristic"`) | ≤ 1 day | Honest claim about the strength of our evidence. Cheap, opens upgrade path. Track C recommends. |
| 4 | **Ward profile injection into `create_config_from_db`** at `cp_sat_basic.py:443-691`, multiplying into stage-3 weights only | 1 week (DB schema + config loader) | Implements per-ward preference profile cleanly. Preserves lex-stage invariant. Both tracks agree on the *where* and the *constraint*. |
| 5 | **Treatment rationale_ko + trade_off_ko catalogue** for 14 atomic treatments + 6 conflict_scenarios | 2-3 days | The "tell the user 'team min may not always be satisfied but we'll do our best'" feature. Pure yaml authoring. |

### Tier 2 — Optional upgrades (defer until v1 ships)

| # | Item | Effort | When |
|---|---|---|---|
| 6 | Upgrade hitting set → IHS via `pysat.hitman` | 1-2 weeks | Only if greedy approximation quality insufficient in production |
| 7 | OCUS warm-MIP solver reuse (3-5× speedup per Track C) | 1-2 weeks | Optional, only if user-facing latency becomes a problem |
| 8 | `_build_full_model` partial rebuild refactor | High (weeks) | Only if interactive (sub-second) bundle-apply UX becomes a requirement |
| 9 | Phase-5 Lagrangian-subgradient learning of `ward_profile` weights from acceptance/rejection feedback | High | Long-term Phase 5 work |

### Tier 3 — Rejected (do not pursue)

- Benders / LBBD decomposition
- Network flow as a *replacement* for CP-SAT (overlay is fine, replacement is not)
- MaxSAT replacement of CP-SAT
- Farkas / DRAT certificate for EvidenceNode (use unsat-core-heuristic with honest metadata instead)
- Re-introduce Lagrangian decomposition (already tried, LNS wins)

---

## 5. Hypergraph Design diff — what changes in `docs/ONTOLOGY_HYPERGRAPH_DESIGN.md`

The design is *not* invalidated. Two small additions are recommended:

1. **§3.4 EvidenceNode** — add explicit `is_minimal`, `core_size`, `proof_type` fields. Document the gap between our unsat-core-heuristic and a true MUS/Farkas certificate.
2. **§5.1 New treatments to add** — also list `treatment:precheck:run_min_cost_flow` as a *diagnostic* (not corrective) action that produces a min-cut breakdown of which (day, shift, team, grade) cells are bottlenecks. Treatment outputs feed CauseNode evidence.
3. **§6 Traversal** — add a step 0 "Run min_cost_flow precheck. If infeasible at flow level, the min-cut directly produces the CauseNode set without invoking CP-SAT MUS." This *short-circuits* clear-cut capacity cases.
4. **§3.3 Bundle semantics** — note that the hitting set algorithm in production is greedy weighted set cover (pure Python) initially, with `pysat.hitman` IHS as the Tier-2 upgrade. Document approximation ratio limit (O(log n)).

---

## 6. References

### Track C (literature, 17 references)
See `/tmp/ontology_audit_track_c_or_evaluation.md` for full bibliography. Most impactful for our design:
- Liffiton & Sakallah 2008 — MUS/MCS hitting set duality
- Davies & Bacchus 2011 — Implicit Hitting Set (IHS)
- Gamba/Bogaerts/Guns 2021 — OCUS optimal subset extraction
- Borning 1992 — Constraint Hierarchies (4-level strength)
- Charnes & Cooper — Preemptive Goal Programming
- Smet et al. 2020 — Behind-the-Scenes Weight Tuning for NRP
- OR-Tools min_cost_flow API documentation
- pysat.hitman API
- Cyclic Preference Scheduling of Nurses via Lagrangian Heuristic
- Integer Multicommodity Flow Model for Nurse Rerostering

### Track D (codebase audit)
See `/tmp/ontology_audit_track_d_codebase_or_fit.md` for full file:line catalog. Key existing assets reused:
- `app/services/cp_sat/hard_assumption.py:54-95` causal layer taxonomy
- `app/services/cp_sat/hard_assumption.py:176-557` MUS extraction with scope grouping + dedup
- `app/services/cp_sat/fallback_lex.py:129-164` 3-stage lex
- `app/services/constraint_impact/control.py:20-458` 4-action treatment vocabulary
- `app/services/constraint_impact/conflict_probe.py:66-123` heuristic OCUS proxy
- `app/services/semantics/ontology.yaml` priority + tier + scope_explosion 2D cost basis

---

## 7. Bottom line for the user

The directed hypergraph + dynamic hitting set + OCUS + ward profile direction is **not just defensible — it's the natural formalization of what your codebase is already doing in an ad-hoc way.** Two independent audits arrived at the same answer.

The most valuable single improvement beyond the current design is **adding a min-cost-flow coverage precheck overlay** (Tier 1 item 2). It is the only OR alternative that *adds* to the design rather than competing with it. Everything else (Benders, full NF, MaxSAT, Farkas, re-Lagrangian) is either already tried-and-failed in the codebase or would require a refactor disproportionate to the gain.

Proceed with Tier 1 items (1-5). Defer Tier 2.
