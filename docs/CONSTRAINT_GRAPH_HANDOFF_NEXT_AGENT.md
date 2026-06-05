# Constraint Graph 작업 인수인계 (다음 에이전트용)

## 0. 이 문서의 목적

이 문서는 Constraint Impact Graph (CIG) + Agent Control + Conflict Diagnosis 작업의 **현재 상태 + 다음 우선 작업** 을 다음 에이전트에게 인수인계하기 위한 것이다. 30분 안에 picking up 가능한 수준으로 작성됨.

## 1. 원래 목표 (변하지 않음)

> "수많은 hard 제약 선택들로 인해 옵션이 많아지면서 발생하는 hard 제약 충돌을 최소화"
> "에이전트가 모듈 단위로 hard 충돌 검색·조절 가능"

검색은 부수적, **조절(adjustment) 가 핵심**.

## 2. 지금까지 누적된 청사진/문서 (읽을 순서)

```
1. CONSTRAINT_IMPACT_GRAPH_ACTIVE_BLUEPRINT.md   — 5계층 청사진
2. CONSTRAINT_IMPACT_GRAPH_SCHEMA_AND_API.md     — dataclass + 함수 시그니처
3. ONTOLOGY_GROUNDED_CONSTRAINT_IMPACT_GRAPH_PLAN.md — ontology catalog
4. CONSTRAINT_AGENT_CONTROL_DRAFT.md             — module 단위 adjustment 표면
5. CONSTRAINT_HARD_CONFLICT_DIAGNOSIS_PLAN.md    — 4 layer 모델 + retention 정책
6. **이 문서 (HANDOFF)** — 현재 상태 + 다음 작업
```

## 3. 4 Layer 모델 (지금 머릿속에 가지고 있어야 할 그림)

```
┌─────────────────────────────────────────────────────────────┐
│ 4. Conflict Probe (reasoning)                               │
│    app/services/constraint_impact/conflict_probe.py         │
│    - rank_relaxation_candidates                             │
│    - match_known_conflict_scenarios                         │
│    - build_probe_plan                                       │
└─────────────────────────────────────────────────────────────┘
                          ▲
┌─────────────────────────────────────────────────────────────┐
│ 3. Solver Emit (event log)                                  │
│    app/services/constraint_impact/solver_emit.py            │
│    - ConstraintEmitRecorder (in-memory per roster_system)   │
│    - 현재 emit 적용된 family 4개:                          │
│      BoundaryTransitionBan / AllowedShiftMask /             │
│      NotOneNight / BanNightBeforeFixedOff                   │
│    - 솔버 (cp_sat_basic.py) 의 m.Add 옆에서 호출            │
└─────────────────────────────────────────────────────────────┘
                          ▲
┌─────────────────────────────────────────────────────────────┐
│ 2. Runtime Graph (A-box, instances)                         │
│    app/services/constraint_impact/graph_builder.py          │
│    - build_constraint_nodes (5-7 family, atoms 의존)        │
│    - build_constraint_edges                                 │
└─────────────────────────────────────────────────────────────┘
                          ▲
┌─────────────────────────────────────────────────────────────┐
│ 1. Ontology (T-box, KB)                                     │
│    app/services/semantics/ontology.yaml + ontology.py       │
│    - 21 family 등록 완료                                    │
│    - relaxation_priority + scope_explosion 모든 family      │
│    - 6 conflict_scenarios (구체적 trigger / why / fix)      │
│    - FixedWanted override (bypasses 정정 완료)              │
└─────────────────────────────────────────────────────────────┘
```

## 4. 현재 상태 (완료된 것 / 남은 것)

### 4.1 Ontology Layer — 거의 완성

**완료** (21 family + 6 scenarios):

| Category | Families |
|---|---|
| Coverage | CoverageMin, CoverageMax, TeamMin, GradeMin, GradeMax, TeamGradeHandoff |
| Nurse-local | ConsecutiveWorkLimit, ConsecutiveNightLimit, NightRecovery, MonthlyNightCap, BoundaryTransitionBan, NotOneNight, AllowedShiftMask, OffCap, OffWindow, WeekendOffOnly, BanNightBeforeFixedOff |
| Coupling | PrecepteeSync, AssignmentWindow, CarryoverBoundary |
| Override | FixedWanted (overrides section) |
| Meta | ConfigIntegrity |

**남은 후보 1개**:
- `ConsecutiveOffLimit` (4 OFF 연속 금지) — `roster_system._find_violations` 에서 post-validation 으로만 enforce. types.py 의 `ConstraintFamily` Literal 에는 이미 등록됨. ontology.yaml 추가 필요.
- 이건 **다음 작업과 별도로 가볍게 추가 가능** (5분 작업).

### 4.2 Control Layer — 완성

`app/services/constraint_impact/control.py`:
- 11 family 에 대한 `_FAMILY_ACTION_MATRIX` 등록
- 4 action: `disable_module`, `force_soft_mode`, `set_threshold`, `narrow_scope`
- `apply_adjustments_to_config(config_dict, adjustments)` 가 deepcopy 후 변형 반환
- 라이브 라우터: `app/routers/constraint_impact.py` (GET /modules, GET /instances, POST /preview_adjustments)

### 4.3 Solver Emit Layer — 부분

`app/services/constraint_impact/solver_emit.py` + `cp_sat_basic.py` 의 4 위치 emit:

| Family | 위치 (cp_sat_basic.py) | bypass mode 처리 |
|---|---|---|
| BoundaryTransitionBan | line 3266-3298 | bypassed_by_fixed |
| AllowedShiftMask | line 3308-3350 | (bypass 없음) |
| NotOneNight | line 3370 | (bypass 없음) |
| BanNightBeforeFixedOff | line 3395-3408 | bypassed_by_fixed |

**남은 emit 후보 (가치 큰 순)**:
1. TeamMin (constraints/team_constraints.py) — ★★★
2. GradeMin/Max (constraints/grade_constraints.py) — ★★★
3. CoverageMin/Max (cp_sat_basic line ~2937) — ★★
4. OffCap (line 2997-3016) — ★★
5. NightRecovery (별도 함수) — ★★
6. ConsecutiveWorkLimit (line 3260) — ★

emit 추가 패턴은 BoundaryTransitionBan 케이스 그대로 복사:
```python
m.Add(<expression>)
_emit_rec.emit(
    family="<OntologyId>",
    scope={...},
    target=<value>,
    mode="enforced" | "bypassed_by_fixed" | ...,
    related_atom_keys=[(n, d), ...],
)
```

### 4.4 Conflict Probe Layer — 완성

`conflict_probe.py`:
- `ConflictProbeReport(ranked_candidates, matched_scenarios, probe_plan, notes)`
- ranking score: `(6 - relaxation_priority) + scope_explosion penalty + scenario_hits * 0.5`
- 출력: ranked candidates list + matched ontology scenarios + ProbeStep plan

### 4.5 Outcome-Conditional Retention — 완성 (단, 단일 attempt 만)

`_build_constraint_impact_payload` (roster_create_service.py):
- SAT outcome → solver_emitted_summary + interesting_events (bypass) only
- UNSAT outcome → 전체 records (per-family 50 cap) + ConflictProbeReport

**현재 한계**: 응답이 **최종 attempt 만** 반영. multi-attempt fallback 케이스의 진단 데이터 손실.

## 5. 발견된 핵심 갭 — Multi-Attempt Lineage (= 다음 우선 작업)

### 5.1 문제

지금 솔버 호출 흐름:
```
Primary attempt (_run_cp_sat_basic)
  ↓
  새 RosterSystem + 새 emit_recorder
  Solve → INFEASIBLE
  ↓
Retry/fallback attempt (_run_cp_sat_basic with _force_grade_max_soft_fallback=True)
  ↓
  또 새 RosterSystem + 새 emit_recorder
  Solve → SAT
  ↓
_build_constraint_impact_payload(final_rs, req)  ← 최종 rs 만
  → 응답: outcome=sat, conflict_probe=None (None이라 진단 못함!)
```

**3가지 손실**:
1. Primary 가 INFEASIBLE 됐던 진단 데이터 사라짐
2. Conflict probe 가 절대 발화 안 함 (모든 attempt fail 시에만)
3. 어느 fallback 플래그가 발화돼서 SAT 됐는지 모름

### 5.2 채택된 해결안: 옵션 C (Per-Attempt Summary + Auto Diff)

응답 구조:
```json
"constraint_impact": {
  "outcome": "sat",
  "solver_status": "fallback",
  "attempts": [
    {
      "label": "primary",
      "outcome": "unsat",
      "applied_relaxations": [],
      "summary_by_family": {
        "TeamMin":               {"enforced": 360, "mode": "enforced"},
        "GradeMax":              {"enforced": 126, "mode": "enforced"},
        "BoundaryTransitionBan": {"enforced": 8820, "mode": "enforced"}
      },
      "conflict_probe": {
        "ranked_candidates": [{"family":"GradeMax", ...}],
        "matched_scenarios": ["TEAM_MIN_VS_GRADE_MAX_INTERSECTION"]
      }
    },
    {
      "label": "grade_max_retry",
      "outcome": "sat",
      "applied_relaxations": ["_force_grade_max_soft_fallback=True"],
      "summary_by_family": {
        "TeamMin":               {"enforced": 360, "mode": "enforced"},
        "GradeMax":              {"soft_fallback": 126, "mode": "soft_fallback"},
        "BoundaryTransitionBan": {"enforced": 8820, "mode": "enforced"}
      }
    }
  ],
  "delta_primary_to_final": {
    "mode_changes": [
      {"family":"GradeMax","before":"enforced","after":"soft_fallback",
       "instances_affected":126,"trigger":"_force_grade_max_soft_fallback flag"}
    ],
    "applied_relaxations_total": ["_force_grade_max_soft_fallback=True"],
    "human_summary": "Primary 가 GradeMax × TeamMin 교차로 INFEASIBLE → fallback 이 GradeMax 만 soft 강등 후 SAT."
  }
}
```

### 5.3 옵션 C 구현 작업 분해

#### Step 1 — `EmittedConstraint` 에 attempt_label 필드 추가

파일: `app/services/constraint_impact/solver_emit.py`

```python
@dataclass(slots=True)
class EmittedConstraint:
    family: str
    scope: dict[str, Any]
    target: Any
    mode: str = "enforced"
    related_atom_keys: list[tuple[int, int]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    attempt_label: str = "primary"   # NEW
```

#### Step 2 — `ConstraintEmitRecorder` 에 attempt 추적

```python
class ConstraintEmitRecorder:
    def __init__(self):
        self._records: list[EmittedConstraint] = []
        self._current_attempt: str = "primary"
    
    def set_attempt(self, label: str) -> None:
        self._current_attempt = label
    
    def emit(self, *, family, scope, target, mode="enforced",
             related_atom_keys=None, metadata=None, **extra_metadata):
        merged = dict(metadata or {})
        merged.update(extra_metadata or {})
        self._records.append(EmittedConstraint(
            family=family, scope=dict(scope), target=target, mode=mode,
            related_atom_keys=list(related_atom_keys or []),
            metadata=merged,
            attempt_label=self._current_attempt,   # NEW
        ))
    
    def summary_by_attempt(self) -> dict[str, dict[str, dict[str, int]]]:
        """{attempt_label: {family: {mode: count}}} 반환."""
        out: dict[str, dict[str, dict[str, int]]] = {}
        for r in self._records:
            atp = out.setdefault(r.attempt_label, {})
            fam = atp.setdefault(r.family, {})
            fam[r.mode] = fam.get(r.mode, 0) + 1
        return out
    
    def records_for_attempt(self, label: str) -> list[EmittedConstraint]:
        return [r for r in self._records if r.attempt_label == label]
```

#### Step 3 — Recorder 를 attempt 간 공유 (가장 까다로움)

현재 `_run_cp_sat_basic` 가 호출될 때마다 새 RosterSystem 이 생기고 새 recorder 가 부착됨. 이걸 **orchestrator 레벨 master recorder** 로 변경.

파일: `app/services/roster_create_service.py`

위치: `generate_roster_service` 안, 첫 `_run_cp_sat_basic` 호출 직전.

패턴:
```python
def generate_roster_service(req, current_user, db):
    ...
    # Master recorder 생성 (한 번만)
    from services.constraint_impact.solver_emit import ConstraintEmitRecorder
    master_recorder = ConstraintEmitRecorder()
    # req 에 attach (모든 attempt 가 보도록)
    setattr(req, "_master_emit_recorder", master_recorder)
    
    # Primary attempt
    master_recorder.set_attempt("primary")
    generated, satisfaction_data, roster_system = _run_cp_sat_basic(...)
    
    # 만약 retry 발생 시
    if validation_error and need_retry:
        master_recorder.set_attempt("grade_max_retry")
        retry_generated, _, retry_rs = _run_cp_sat_basic(..., config_override={..., "_force_grade_max_soft_fallback": True})
    
    # 최종 rs 의 _constraint_impact_solver_emit_recorder 자리에 master 부착
    setattr(final_rs, "_constraint_impact_solver_emit_recorder", master_recorder)
```

`_run_cp_sat_basic` 내부에서는:
```python
def _run_cp_sat_basic(..., req, ...):
    ...
    # roster_system 생성 직후
    roster_system = ...
    # master recorder 가 있으면 그걸 attach, 없으면 새로
    master = getattr(req, "_master_emit_recorder", None)
    if master is not None:
        setattr(roster_system, "_constraint_impact_solver_emit_recorder", master)
    # else: get_or_attach_recorder 로 새로 만듦 (기존 동작)
```

이러면 cp_sat_basic 의 emit 호출 (`_emit_rec.emit(...)`) 은 변경 없이 자동으로 master 에 누적됨.

#### Step 4 — `_run_cp_sat_basic` 가 attempt label 자동 설정

위 Step 3 의 패턴이 깨지지 않도록, `_run_cp_sat_basic` 진입 시:
```python
if master := getattr(req, "_master_emit_recorder", None):
    # config_override 의 플래그로 attempt 종류 추론
    if bool(config_override.get("_force_grade_max_soft_fallback") if config_override else False):
        master.set_attempt("grade_max_retry")
    elif bool(config_override.get("_lex_fallback") if config_override else False):
        master.set_attempt("lex_fallback")
    else:
        master.set_attempt("primary")
```

또는 더 명시적으로 orchestrator 가 `set_attempt` 호출 (Step 3 의 패턴).

#### Step 5 — `_build_constraint_impact_payload` 가 attempts[] + delta 생성

파일: `app/services/roster_create_service.py` (line ~3679)

기존 `solver_emitted_summary` / `solver_emitted_nodes` / `conflict_probe` 응답을 다음으로 대체:

```python
emit_rec = getattr(roster_system, "_constraint_impact_solver_emit_recorder", None)
attempts_payload: list[dict] = []
delta_payload: dict | None = None

if emit_rec is not None:
    by_attempt = emit_rec.summary_by_attempt()
    for label, fam_summary in by_attempt.items():
        attempt_records = emit_rec.records_for_attempt(label)
        # outcome 추론: SAT/UNSAT — 마지막 attempt 가 success 면 그 attempt SAT, 그 외는 UNSAT
        attempt_outcome = "sat" if (label == final_attempt_label and analysis.valid_under_current_semantics) else "unsat"
        applied_relaxations = _infer_applied_relaxations(label)  # config flag → 자연어
        
        attempt_entry = {
            "label": label,
            "outcome": attempt_outcome,
            "applied_relaxations": applied_relaxations,
            "summary_by_family": _normalize_family_summary(fam_summary),  # mode 추론 추가
        }
        if attempt_outcome == "unsat":
            from services.constraint_impact.conflict_probe import build_conflict_probe_report
            probe = build_conflict_probe_report(emit_records=attempt_records)
            attempt_entry["conflict_probe"] = _serialize_probe(probe)
        attempts_payload.append(attempt_entry)
    
    # delta 자동 계산: primary vs final
    if len(attempts_payload) >= 2:
        delta_payload = _compute_delta(attempts_payload[0], attempts_payload[-1])
```

`_compute_delta` 함수 — family 별 mode 비교 + applied_relaxations 합집합 + human_summary 생성.

#### Step 6 — 단위 테스트

`tests/test_attempt_lineage.py` 신규:

1. `test_recorder_tags_records_with_attempt_label` — set_attempt 호출 후 emit 한 record 가 그 label 갖는지
2. `test_summary_by_attempt_groups_correctly` — 두 attempt 의 emit 이 분리됨
3. `test_records_for_attempt_filters_correctly`
4. `test_payload_attempts_array_includes_primary_unsat_with_probe` — mocked
5. `test_payload_delta_detects_mode_change` — primary GradeMax=enforced → final GradeMax=soft_fallback 인지 검증

#### Step 7 — 라이브 검증 (2026-05 token)

token (만료 시 새 요청):
```
eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJvZmZpY2VfaWQiOiIxMDEzNTgi...
```

Cookie 파일 패턴:
```bash
TOKEN_RAW='<token>'
cat > /tmp/cookies.txt <<EOF
# Netscape HTTP Cookie File
localhost	FALSE	/	FALSE	0	access_token	Bearer ${TOKEN_RAW}
EOF
```

검증 시나리오:
- POST /roster_create/generate (no adjustments) — 응답에 attempts[] 가 있는지
- 만약 SAT 단번에 풀리는 케이스면 attempts.length == 1 (primary only)
- Fallback 발화 시나리오 강제 (e.g., 의도적으로 grade_max 빠듯한 config) — attempts.length >= 2 + delta_primary_to_final populated

DB 변경 추적 + 종료 시 원복 (이미 패턴 확립됨, .omc/9b_test_db_changes.md 참고):
```python
# 신규 schedules 찾기
SELECT schedule_id FROM schedules WHERE group_id='10135890c287' AND year=2026 AND month=5 AND created_at >= '<test_start>'
# 삭제
DELETE FROM schedule_entries WHERE schedule_id IN (...)
DELETE FROM schedules        WHERE schedule_id IN (...)
```

## 6. 코드베이스 진입점 빠른 참조

| 파일 | 역할 |
|---|---|
| `app/services/semantics/ontology.yaml` | KB catalog (21 family + 6 scenarios + FixedWanted override) |
| `app/services/semantics/ontology.py` | Loader + lookup API |
| `app/services/constraint_impact/solver_emit.py` | EmittedConstraint + Recorder (← Step 1, 2 변경) |
| `app/services/constraint_impact/conflict_probe.py` | ranking + scenario matching + plan |
| `app/services/constraint_impact/control.py` | adjustment dispatch (11 family handlers) |
| `app/services/constraint_impact/graph_builder.py` | A-box node builder (5-7 family, atoms 의존) |
| `app/services/cp_sat_basic.py` | 솔버 본체. emit 4곳 (line 3266, 3308, 3370, 3395) |
| `app/services/roster_create_service.py` | orchestrator. `_run_cp_sat_basic` (line 2566), `generate_roster_service` (line 4214), `_build_constraint_impact_payload` (line 3679) |
| `app/routers/constraint_impact.py` | GET /modules, GET /instances, POST /preview_adjustments |
| `app/main.py` | 라우터 등록 (line 192-194) |

## 7. 테스트 패턴

기존 통과 테스트 (65/65):
- `tests/test_constraint_control.py` (24 tests)
- `tests/test_semantics_ontology.py` (15 tests)
- `tests/test_solver_emit.py` (7 tests)
- `tests/test_conflict_probe.py` (10 tests)
- `tests/test_outcome_retention.py` (3 tests)
- `tests/test_constraint_impact_parity_matrix.py` (legacy, 별도)

새 테스트 추가 시:
- import path: `sys.path.insert(0, 'app')` 헤더 패턴 그대로 사용
- 테스트 파일은 모두 `tests/test_*.py`
- 실행: `python -m pytest tests/test_*.py -q`

## 8. 라이브 검증 시 체크리스트

다음 에이전트가 라이브 검증할 때 반드시 확인:

- [ ] cookie 파일 (Netscape format) 준비 — 위 7단계 참조
- [ ] uvicorn 가 reload 모드로 살아있는지 (`lsof -i :8000`)
- [ ] auth/me 200 으로 토큰 valid 확인
- [ ] /constraint_impact/modules 200 + 21+ modules
- [ ] DB count baseline 기록 (group=10135890c287, year=2026, month=5)
- [ ] generate 호출 후 attempts[] 응답 확인
- [ ] DB 신규 schedules 기록 (`.omc/9b_test_db_changes.md` 추가)
- [ ] 종료 시 신규 schedules + entries DELETE
- [ ] 최종 DB count == baseline 검증

## 9. 하지 말아야 할 것 (이전 작업에서 학습된 함정)

1. **`m.Add` 옆 emit 시 솔버 동작 변경 금지** — emit 은 부수효과만, 절대 m.Add 를 대체/수정 안 함
2. **Linter 가 가끔 `app/main.py` 의 router include 줄을 unused import 로 오인** — 제거됐으면 다시 추가 필요
3. **JWT 토큰 1일 유효** — 만료되면 새 토큰 받아야 함
4. **DB 는 MSSQL** — `sqlite_master` 같은 SQLite 전용 쿼리 사용 금지. `INFORMATION_SCHEMA.COLUMNS` 사용
5. **OffWindow / ConfigIntegrity 의 supported_actions 는 비어있음** — 직접 disable 안 됨, derived 거나 precheck-only 라서. 새 family 추가 시 같은 분류면 같은 패턴 따르기

## 10. 종료 후 무엇이 가능해지는가

옵션 C 완료 시:
- 에이전트가 fallback 발생 케이스에서 **"primary 에서 무엇이 binding 이었고 fallback 에서 무엇이 풀려서 SAT 됐다"** 를 한 응답으로 인지
- 다음 generate 호출에 사전 adjustment 추천 가능 (예: "GradeMax 를 처음부터 soft 로 둘까요?")
- LangSmith 같은 trace viewer 의 기반 데이터 완성 (per-attempt summary + diff)
- 진정한 "agent-driven hard 충돌 해소" 의 full loop 완성

## 11. 작업 단위 요약

| Step | 파일 | 작업량 | 검증 |
|---|---|---|---|
| 1 | solver_emit.py | 5분 | 단위 테스트 |
| 2 | solver_emit.py | 15분 | 단위 테스트 |
| 3 | roster_create_service.py | 30분 | 통합 테스트 |
| 4 | roster_create_service.py | 10분 | 통합 테스트 |
| 5 | roster_create_service.py | 30분 | 단위 + 통합 |
| 6 | tests/test_attempt_lineage.py | 30분 | pytest pass |
| 7 | 라이브 | 30분 | 응답 검증 + DB 원복 |

총 예상: 2-3 시간.

## 12. 호출 시 첫 명령

```bash
# 환경 확인
cd /Users/david/Desktop/assignments/DataEngine/meditong/nurse_rostering_back/roster-back
git status -s
python -m pytest tests/test_constraint_control.py tests/test_semantics_ontology.py \
  tests/test_solver_emit.py tests/test_conflict_probe.py tests/test_outcome_retention.py -q

# 작업 시작 — 위 Step 1 부터
```

성공.
