# 발효 cron 설계 (Phase 2)

작성: 2026-06-01
관련: [NURSE_GROUP_CHANGE_MODEL.md](./NURSE_GROUP_CHANGE_MODEL.md), [NURSE_ASSIGNMENT_DDL_PLAN.md](./NURSE_ASSIGNMENT_DDL_PLAN.md)
전제: Phase 1 헬퍼 + DDL(kind/payload 컬럼) 적용 완료.

---

## 0. 기존 인프라 (이미 갖춰진 것)
- `app/main.py:_daily_flush_scheduler` — 일일 백그라운드 cron
- `app/services/assignment_service.py`:
  - `flush_pending_transfers(db, group_id)` — 병동이동 발효 (lazy, generate 호출 시)
  - `flush_expired_dispatches(db)` — 파견 만료 자동 처리 (cron)
  - `flush_expired_preceptees(db)` — 프리셉티 만료 (cron)
  - 휴직/퇴사 자동 처리 잡 존재
- 즉 **transfer 발효·dispatch/leave 만료는 이미 자동 처리.** 신규로 필요한 건 `permanent_change` 처리 + reconcile + 알림.

---

## 1. cron 구조 (확장)

### 1.1. 기존 함수 보강
| 함수 | 보강 내용 |
|---|---|
| `flush_pending_transfers(db, group_id)` | DDL 적용 후 `kind='transfer'`로 필터. NML group_id 자동 update 보강 (이미 되고 있을 가능성 — 확인 필요). |
| `flush_expired_dispatches(db)` | `kind='dispatch'` 필터. dispatch_override 동반 row도 같이 만료 처리 (linked). |

### 1.2. 신규 cron 함수
```python
# app/services/assignment_service.py

def flush_pending_permanent_changes(db: Session) -> int:
    """영구속성 변경(kind='permanent_change') 발효.
    
    조건: kind='permanent_change' AND status='active' AND start_date <= today
    동작: payload JSON 풀어서 Nurses의 해당 attribute 업데이트 → status='completed'.
    """
    rows = db.query(NurseAssignment).filter(
        NurseAssignment.kind == 'permanent_change',
        NurseAssignment.status == 'active',
        NurseAssignment.start_date <= date.today(),
    ).all()
    
    count = 0
    for row in rows:
        try:
            with db.begin_nested():
                nurse = db.query(Nurse).filter_by(nurse_id=row.nurse_id).one()
                payload = json.loads(row.payload or "{}")
                for attr, value in payload.items():
                    if hasattr(nurse, attr):
                        setattr(nurse, attr, value)
                row.status = 'completed'
                count += 1
        except Exception as e:
            logger.error(f"permanent_change 발효 실패: row {row.id} — {e}")
            # transaction rolled back, continue with next row
    db.commit()
    return count


def reconcile_nurse_attrs(db: Session, sample_size: int = 100) -> dict:
    """야간 reconcile: Nurses와 latest NurseAssignment 비교.
    
    sample_size명씩 비교 → 불일치 시 동기화 + 경고 로그.
    """
    # 활성 간호사 샘플링 → 각자 effective attr 계산 → Nurses 값과 비교
    # 불일치 카운트 + Nurses 동기화 (선택) + 로그
    ...
```

### 1.3. main.py cron 등록
```python
# app/main.py:_daily_flush_scheduler 안에 추가
pc_count = flush_pending_permanent_changes(db)
if pc_count > 0:
    _scheduler_logger.info("[Scheduler] 영구속성 변경 자동 발효: %d건", pc_count)

recon = reconcile_nurse_attrs(db)
if recon.get("mismatches"):
    _scheduler_logger.warning("[Scheduler] reconcile 불일치: %s", recon)
```

---

## 2. 트랜잭션 정책

### 2.1. 멱등성 (idempotency)
- cron 실패 후 재실행 시 같은 row를 두 번 처리하면 안 됨.
- 처리된 row는 즉시 `status='completed'` 마크. 다음 cron이 활성만 보므로 자연 스킵.
- 단일 트랜잭션 안에서 `Nurses 업데이트 + status 변경`을 묶음. 중간 실패 시 rollback.

### 2.2. 실패 catch-up
- 한 row 실패는 다른 row 처리를 막지 않음 (per-row nested transaction).
- 실패 row는 `status='active'` 유지 → 다음 cron이 재시도.
- 같은 row가 N회 연속 실패 시 알림 (failure_count 컬럼 추가는 후속 고려).

### 2.3. 동시성
- cron은 일일 1회 또는 매시간. 동시 실행 방지 lock 필요 — 기존 main.py에 이미 단일 task로 등록돼 있어 OK.
- `flush_pending_transfers`는 generate 호출 시 lazy 발효도 함 → cron과 동시 발생 시 같은 row 처리 race. **per-row update 시 `WHERE status='active'`로 조건부 update**해서 race 방지.

---

## 3. 취소·롤백

### 3.1. 발효 전 취소
- 신규 API: `POST /api/assignments/{id}/cancel`
- 동작: `status='active'` → `'cancelled'`. cron이 무시.
- dry-run에서 미리 보였던 영향(NML group_id 변경 등)은 아직 일어나지 않았으므로 환원 불요.

### 3.2. 발효 후 되돌리기
- 반대 방향 새 NurseAssignment 이벤트 append.
- 예: 박혜미 7/1자 A→B 발효 완료. 7/10에 되돌리기 → 7/10자 B→A 새 transfer 이벤트.
- 이미 발효된 cron 효과(NML group_id=B)는 새 이벤트 발효 시 다시 update됨.

### 3.3. 같은 attribute 충돌 처리
- 같은 nurse·같은 attribute에 미래 발효 active row가 2건 이상 입력되면 거부 (validation):
  ```python
  conflict = db.query(NurseAssignment).filter(
      NurseAssignment.nurse_id == new.nurse_id,
      NurseAssignment.status == 'active',
      NurseAssignment.kind == new.kind,
      NurseAssignment.start_date >= today,
  ).count()
  if conflict > 0:
      raise ValueError("미래 발효 이동/변경이 이미 존재합니다. 먼저 취소하세요.")
  ```

---

## 4. 알림

### 4.1. 트리거 지점
| 시점 | 대상 | 내용 |
|---|---|---|
| 예약 직후 | 본인·옛 그룹 HN·새 그룹 HN | "박혜미 7/1자 A→B 이동 예약. dry-run 영향: NML 1건 자동 이전" |
| 발효 직후 (cron) | 본인·옛 HN·새 HN | "이동 완료. 새 그룹에서 wanted 작성 가능" |
| 취소 시 | 본인·옛 HN·새 HN | "이동 예약 취소" |

### 4.2. 구현
- 기존 `app/routers/messages.py` 메시지 시스템 활용 권장 — 추가 인프라 불요.
- cron 안에서 push:
  ```python
  send_message(to_nurse_id=row.nurse_id, type='transfer_applied', payload={...})
  send_message(to_role='head_nurse', group_id=row.source_group_id, ...)
  send_message(to_role='head_nurse', group_id=row.target_group_id, ...)
  ```

---

## 5. Dry-run 영향 분석 API

### 5.1. 신규 엔드포인트
```
POST /api/assignments/preview
{
  "nurse_id": "...",
  "kind": "transfer",
  "start_date": "2026-07-01",
  "target_group_id": "B"
}
→
{
  "affected": {
    "nml": [{"year": 2026, "month": 7, "current_group": "A", "will_become": "B"}, ...],
    "wanted": {"count_to_orphan": 5, "year_month_range": ["2026-07", "2026-09"]},
    "schedules": {"to_regenerate": ["2026-07"]},
    "notifications": ["self", "A_HN", "B_HN"]
  }
}
```

### 5.2. 사용
- UI에서 이동 입력 시 확정 직전에 호출 → 운영자가 영향 확인 후 진행/취소.

---

## 6. 우선순위 + 의존성

| 단계 | 작업 | 의존 |
|---|---|---|
| 2.1 | `flush_pending_permanent_changes` 신규 함수 + 기존 `flush_pending_transfers` 보강 | DDL 완료 |
| 2.2 | `_daily_flush_scheduler`에 등록 | 2.1 |
| 2.3 | 취소 API + 충돌 검증 | 2.1 |
| 2.4 | 알림 큐 push (메시지 시스템 연계) | 2.1, 2.3 |
| 2.5 | dry-run 영향 API | 2.1 |
| 2.6 | reconcile job | 2.1 |

**예상 소요**: 1.5~2주 (기존 인프라 활용으로 단축).

---

## 7. 짚을 점

1. **기존 `flush_pending_transfers`가 우리 모델과 동작이 같은지** 검증 필요.
   - NML group_id 자동 update 동작 여부 확인.
   - status 전이 룰(active → completed/applied) 통일.

2. **generate 호출 시 lazy 발효 vs cron의 시점 차이**:
   - generate는 그룹 단위 호출 → 호출 안 된 그룹의 발효는 cron에 의존.
   - 즉 두 경로 다 작동해야 빈틈 없음. 두 경로 모두 멱등성 보장.

3. **payload JSON 처리 시 누락 컬럼**:
   - Nurses 테이블에 없는 attribute가 payload에 들어가면 `hasattr` 체크로 건너뛰어 안전.
   - 단 운영자가 잘못 입력한 attribute(예: 오타)는 조용히 무시되므로 입력 단계 validation 필요.

4. **알림 시스템 부하**:
   - 매월 자동 클러스터링이 활성화되면 다량의 발효 알림. 본인 알림은 묶음(digest)으로 보내는 것 고려.

5. **외부 시스템 동기화**:
   - HR/qpis hook은 Phase 4. cron 처리 후 외부 push는 별도 모듈에서.

---

## 8. 다음 작업
1. ✅ 본 설계 검토 완료
2. ⏳ DDL 적용 (Phase 1.4)
3. ⏳ Phase 2.1 구현 (`flush_pending_permanent_changes` 신규 + 기존 보강)
4. ⏳ 단계별 진행 (2.2 → 2.6)
