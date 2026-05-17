# Ontology Harness Checklist — 새 하드제약 / cause / treatment 추가 시

> 사용자 ralph 요구 (2026-05-17): "새로운 하드제약이 있거나 제약사항이 수정될 때 이걸 반영하도록 하네스를 철저히 구축."

이 체크리스트는 **새 constraint_family / cause / treatment / config_key 가 추가·수정될 때 정합성이 끊기지 않도록** 자동 강제하는 가이드.

`tests/test_ontology_consistency.py` 가 9 개 invariant (I1~I9) 를 검증. **어느 하나라도 fail = 새 항목 추가 시 누락된 wiring 이 있다는 신호.**

라이브 audit: `GET /ontology/audit` — JSON 응답으로 fail 항목 + 위치 + 보강 방법 노출.

---

## A. 새 `cause` 추가 워크플로우 (예: `cause:fatigue:burnout_risk`)

### 1. ontology.yaml 의 `causes` 섹션에 entry 추가

```yaml
causes:
  cause:fatigue:burnout_risk:
    label: "피로 누적 위험"
    category: fatigue                    # I9: KNOWN_CAUSE_CATEGORIES 에 등재 필요시 ontology_audit.py 수정
    causal_layer: structural
    tier: T2
    is_hard: true
    aliases: [BURNOUT_RISK_DETECTED]
    problem_template_ko: "간호사 {nurse_id}의 야간 누적 {n_count}회로 피로 위험."   # I1 필수
```

### 2. 매칭되는 `treatment` 1+ 등재 (I2)

기존 treatment 의 `applies_to_causes` 에 추가:

```yaml
treatments:
  treatment:disable:night_recovery:
    applies_to_causes:
      - cause:capacity:monthly_night_shortage
      - cause:fatigue:burnout_risk      # ← 추가
```

또는 manual-only treatment 신규:

```yaml
treatments:
  treatment:data:rebalance_nights:
    label: "야간 분배 재조정 (manual)"
    action_type: data_correction_required
    target_family: AssignmentWindow
    config_key: null
    direction: manual
    rationale_ko: "..."
    trade_off_ko: "..."
    applies_to_causes:
      - cause:fatigue:burnout_risk
```

### 3. 새 category 면 `ontology_audit.KNOWN_CAUSE_CATEGORIES` 에 추가 (I9)

```python
# app/services/semantics/ontology_audit.py
KNOWN_CAUSE_CATEGORIES = frozenset({
    ...,
    "fatigue",     # 신규
})
```

### 4. (선택) `payload_graph.py` color token 추가 + dashboard `--cat-fatigue` CSS 변수

### 5. `pytest tests/test_ontology_consistency.py` 통과 확인

---

## B. 새 `constraint_family` 추가 (CP-SAT 솔버 hard 제약 신규)

### 1. ontology.yaml `constraints` 섹션에 등재

```yaml
constraints:
  MaxWeekendShifts:
    parent: CapacityConstraint
    is_hard: true
    causal_layer: policy
    tier: T2
    cp_sat_pattern: "MaxWeekendShifts:nurse_{n}"
    explanation_template: "주말 시프트 한도 초과"
```

### 2. CP-SAT 솔버 코드의 `add_hard()` 호출에서 동일 이름 사용

```python
# app/services/cp_sat_basic.py
registry.add_hard(
    model,
    name=f"MaxWeekendShifts:nurse_{n}",       # ← family name 와 prefix 일치
    expr=sum_weekend_X <= cfg.max_weekend,
    meta={"type": "MaxWeekendShifts", "nurse_id": n},
)
```

### 3. `cause_inferer._MEMBER_PREFIX_TO_CAUSE` 에 prefix → cause 매핑 추가

```python
# app/services/precheck/cause_inferer.py
_MEMBER_PREFIX_TO_CAUSE: dict[str, str] = {
    ...,
    "MaxWeekendShifts": "cause:fatigue:burnout_risk",   # 솔버가 MUS 잡을 때 자동 cause 변환
}
```

### 4. `ontology_audit._FAMILY_TO_MUS_TOKENS` 에 매핑 등재 (I7)

```python
# app/services/semantics/ontology_audit.py
_FAMILY_TO_MUS_TOKENS: dict[str, list[str]] = {
    ...,
    "MaxWeekendShifts": ["MaxWeekendShifts", "max_weekend_shifts"],
}
```

> ⚠ I7 가 fail = audit harness 가 새 family 의 솔버 MUS 매핑이 등재 안 됐다고 경고. **이 단계 빼먹으면 솔버가 infeasibility 잡아도 사용자가 cause 못 봄.**

### 5. (선택) `fallback_lex.py` 에도 동일 family wrap (primary + fallback 양쪽 MUS 추출)

### 6. `pytest tests/test_ontology_consistency.py::test_each_invariant_passes[I7*]` 통과 확인

---

## C. 새 `treatment` 추가 (조정 가능한 lever 신규)

### 1. ontology.yaml `treatments` 섹션에 entry

```yaml
treatments:
  treatment:threshold:max_weekend:
    label: "주말 시프트 한도 상향"
    action_type: set_threshold
    target_family: MaxWeekendShifts
    config_key: max_weekend_shifts          # ← I5: 친화 라벨 필요
    direction: increase                     # ← I6: 친화 라벨 필요 (등재됨)
    rationale_ko: "주말 시프트 한도를 1~2 증가."     # I4 필수
    trade_off_ko: "주말 부담 ↑ — 다음달 분산 권장."   # I4 필수
    applies_to_causes:
      - cause:fatigue:burnout_risk
```

### 2. `CONFIG_KEY_LABELS_KO` 에 친화 라벨 추가 (I5)

```python
# app/services/semantics/ontology.py
CONFIG_KEY_LABELS_KO: dict[str, str] = {
    ...,
    "max_weekend_shifts": "월 주말 시프트 한도",
}
```

### 3. `treatment_applicator._ACTION_MAP` 검토 — 새 action_type 이면 등재 (현재는 5종 모두 cover)

### 4. (auto-apply 가능하면) `constraint_impact/control.py` 의 action handler 보강

### 5. `pytest tests/test_friendly_labels.py tests/test_ontology_consistency.py` 통과 확인

---

## D. matrix 50 cases 갱신 (새 cause 가 어떤 spec case 에 나오는지)

### 1. `tools/harness/matrix_50_cases.py` 의 cause factory shortcut 추가

```python
def burnout(**ev):   return _cs("cause:fatigue:burnout_risk", "BURNOUT_RISK_DETECTED",
                                nurse_id="N007", n_count=8, **ev)
```

### 2. CASES_50 의 적절한 case 의 `causes` 에 추가 또는 신규 case 등재

### 3. I8 audit 통과 확인 — factory cause_id 가 ontology.yaml 등재됨

---

## E. CI / 자동화 흐름

| 트리거 | 명령 | 차단 효과 |
|---|---|---|
| 매 PR | `pytest tests/test_ontology_consistency.py -v` | 9 invariant 중 하나라도 fail → PR merge 차단 |
| 매 PR | `pytest tests/test_friendly_labels.py -v` | 친화 라벨 누락 차단 |
| 매 PR | `pytest tests/test_matrix_full_50_cases.py -v` | 50 cases 회귀 + 카테고리 분포 |
| 운영 | `GET /ontology/audit` (브라우저/모니터) | 라이브 audit 결과 — 운영자가 즉시 확인 |
| 개발 | `PYTHONPATH=app python -c "from services.semantics.ontology_audit import audit_all; print(audit_all())"` | 로컬 빠른 audit |

---

## F. 9 Invariants 요약

| ID | 검증 대상 | severity | 누락 시 영향 |
|---|---|---|---|
| **I1** | 모든 cause 에 `problem_template_ko` | critical | dashboard 빈 메시지 노출 |
| **I2** | 모든 cause 에 ≥1 treatment | critical | hitter 가 추천 불가 |
| **I3** | treatment.applies_to_causes 가 valid cause_id | critical | dangling reference, runtime KeyError 가능 |
| **I4** | treatment 에 rationale_ko + trade_off_ko | major | dashboard "해결책" / "부작용" column 비어보임 |
| **I5** | config_key 친화 라벨 등재 | minor | raw setting key 가 jargon 으로 노출 |
| **I6** | direction 친화 라벨 등재 | minor | enable/disable 가 영문 노출 |
| **I7** | constraint_family ↔ MUS token mapping | major | 솔버 MUS 잡아도 cause 변환 실패 |
| **I8** | matrix factory cause_id 정합 | critical | 50-case 회귀 fail |
| **I9** | cause.category 가 알려진 도메인 | major | dashboard 색상/필터 누락 |

---

## G. 트러블슈팅

**Q. I7 가 fail 했어요 — 어디 보강?**
A. `app/services/precheck/cause_inferer.py` 의 `_MEMBER_PREFIX_TO_CAUSE` 또는 `_PATTERN_TO_CAUSE` 에 솔버 변수 이름 → cause_id 매핑 추가. 매핑 후 `_FAMILY_TO_MUS_TOKENS` 에도 등재.

**Q. 새 family 추가 안 했는데 I7 가 fail.**
A. 새 family 가 `_FAMILY_TO_MUS_TOKENS` 에 없으면 fail. 솔버 MUS 미발생 family (precheck arithmetic only) 면 빈 list `[]` 로 등재.

**Q. I2 가 fail — manual-only cause 도 PASS 시키려면?**
A. `treatment:data:fix_config` 또는 `treatment:undiagnosed` 같은 manual treatment 의 `applies_to_causes` 에 추가. (manual 도 1+ treatment 로 인정.)

**Q. category 명을 바꿨더니 I9 fail.**
A. `ontology_audit.KNOWN_CAUSE_CATEGORIES` 에 새 카테고리 추가.

---

## H. 참고

- audit 모듈: `app/services/semantics/ontology_audit.py`
- 테스트: `tests/test_ontology_consistency.py` (10 tests — full audit + per-invariant)
- 라이브 endpoint: `GET /ontology/audit`
- 이 문서: `docs/ONTOLOGY_HARNESS_CHECKLIST.md`
