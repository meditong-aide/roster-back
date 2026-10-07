"""재마감 변경 확인 — 재마감 때 바뀐 칸·확인 대상자를 남기고, 간호사의 [확인]을 받는다(성남 ②).

설계: 9-28 "근무표 변경 확인·버전 관리 설계" + 10-02 기본안·저장 구조 확정.

★ 새 테이블은 확인 기록(`roster_change_ack`) 하나다(10-02 사용자 결정).
  - 알림 = 바뀐 칸이 있는 재마감의 **마감 스냅샷**. 알림 번호 = 그 스냅샷 번호.
  - 비교 기준·요약은 그 스냅샷 `meta_json.change` 에 둔다.
  - 바뀐 칸은 저장하지 않고 두 스냅샷(기준 → 이번)을 비교해 그때그때 계산한다.
  - 상태는 계산한다 — 스냅샷이 활성이면 확인 중/완료(남은 대상자 전원 확인), 비활성이면
    뒤에 다시 마감된 스냅샷이 있으면 대체됨, 없으면 철회 중. 단 **완료가 처음 보인 순간**(확인·조회·재마감)은
    `meta_json.change.closed_at` 에 남긴다(완료 뒤 대상자가 다시 활성이 돼도 되살아나지 않게).
★ 비교 기준은 **간호사들이 마지막으로 본 마감본**이다 — 그 달 가장 최근 스냅샷(철회돼 비활성이어도).
  흐름이 "마감 철회 → 수정 → 재마감" 이라 재마감 시점엔 활성 스냅샷이 없는 게 보통이다.
★ 그 스냅샷이 아직 확인 중인 알림이었으면 그 **기준을 이어받아** 누적한다(VER3→4 를 확인하기 전
  VER5 로 마감하면 봐야 할 것은 VER3→5 전체). 확인 기록은 새 스냅샷에 새로 만든다(초기화).
★ 바뀐 칸이 0 이면 알림을 만들지 않는다(재마감 푸시만). D→E→D 처럼 결국 같아진 칸도 변경이 아니다.
  근무가 하나도 없는 **빈 행**이 생기거나 빠지는 것도 변경이 아니다 — 아무의 근무도 바뀌지 않았는데
  병동 전원에게 다시 확인을 받는 것은 소음이다(10-02 결정 · Codex 8회차 지적을 받고 의도로 남김).
  명단 변경은 칸이 하나라도 있는 간호사가 한쪽에만 있을 때다(`schedule_compare_service.diff_cell_maps`).
★ 확인 대상 = 재마감 시점 그 달 소속(파견 들어온 사람 포함) − 휴직·퇴사 − 마감한 사람.
  완료 판정은 **지금도 확인할 수 있는 대상자** 기준이다 — 퇴사·비활성이 됐거나 그 병동을 더는 볼 수 없게 된
  사람(파견 취소·종료, 병동 이동)은 빠지고 남은 전원이 확인하면 끝난다. 상세·확인은 늘 **지금 권한**을 본다.
"""
from __future__ import annotations

import calendar
from collections import Counter
from datetime import date, datetime
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from db.models import Group, IssuedRosterSnapshot, Nurse, RosterChangeAck
from services.schedule_compare_service import diff_cells_payload, diff_rows, shown_codes

#: 확인 대상에서 빼는 그 달 소속 상태(`group_members_in_month.membership_status`)
_NOT_TARGET_STATUSES = {"leave", "resigned"}
#: 스냅샷 meta_json 안의 변경 알림 키
CHANGE_KEY = "change"


# ───────── 스냅샷 → 칸 ─────────

def snapshot_cells(snapshot: IssuedRosterSnapshot) -> tuple[dict, dict]:
    """마감 스냅샷 → (칸 지도 `(nurse_id, 날짜) → (코드, shifts.id)`, 칸 색 `(nurse_id, 날짜) → 색`).

    스냅샷 `roster_json.nurses[].schedule` 은 날짜순 `{code, color}`(빈 칸 '-'). 옛 모양(문자열)도 받는다.
    id 는 같은 순서의 `schedule_ids`(없거나 숫자가 아니면 None) — 마감 뒤 이름만 바뀐 근무를 같은 근무로 보는 데 쓴다
    (`schedule_compare_service.shown_codes`).
    색은 **그 마감 시점의 색**을 쓴다 — 병동이 나중에 색을 바꿔도 이전/이후 색이 그대로다.
    """
    rj = snapshot.roster_json or {}
    year, month = int(snapshot.year or rj.get("year")), int(snapshot.month or rj.get("month"))
    days = calendar.monthrange(year, month)[1]
    cells: dict = {}
    colors: dict = {}
    for n in rj.get("nurses") or []:
        nurse_id = str(n.get("nurse_id") or n.get("id") or "")
        if not nurse_id:
            continue
        ids = n.get("schedule_ids") or []
        for i, item in enumerate((n.get("schedule") or [])[:days]):
            code = item.get("code") if isinstance(item, dict) else item
            if not code or str(code).strip() in ("", "-"):
                continue
            key = (nurse_id, date(year, month, i + 1))
            cells[key] = (str(code), _int_or_none(ids[i] if i < len(ids) else None))
            if isinstance(item, dict) and item.get("color"):
                colors[key] = item["color"]
    return cells, colors


def _int_or_none(v) -> Optional[int]:
    if v is None or isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def change_rows_between(
    db: Session, base: IssuedRosterSnapshot, snapshot: IssuedRosterSnapshot,
) -> tuple[list[dict], object]:
    """기준 → 이번 스냅샷의 바뀐 칸 행과 차이(`CellDiff`).

    행 = 바뀐 칸(kind='cell') + 명단에 생기거나 빠진 간호사의 칸(kind='added'|'removed').
    ★ 양쪽 칸을 `shown_codes` 로 맞춘다 — 마감 뒤 근무코드 **이름만** 바뀐 근무(같은 shifts.id)는 실제 근무가 바뀐 게
      아니라 알림 대상이 아니다(10-06 사용자 결정). 버전 변경 조회와 같은 규칙.
    """
    before, b_colors = snapshot_cells(base)
    after, a_colors = snapshot_cells(snapshot)
    before = shown_codes(db, snapshot.group_id, before)
    after = shown_codes(db, snapshot.group_id, after)
    # 행 모양은 버전 변경 조회(`schedule_changes`)와 공용 — 화면 컴포넌트 하나로 그린다.
    return diff_rows(before, b_colors, after, a_colors)


def change_meta(snapshot) -> Optional[dict]:
    """스냅샷의 변경 알림 요약(없으면 None — 첫 마감·바뀐 칸 0 인 재마감)."""
    return (getattr(snapshot, "meta_json", None) or {}).get(CHANGE_KEY)


# ───────── 재마감 때: 알림 만들기 ─────────

def confirm_targets(db: Session, group_id: str, year: int, month: int, publisher_id: Optional[str]) -> list[str]:
    """확인 대상 nurse_id — 그 달 소속(파견 들어온 사람 포함) − 휴직·퇴사 − 마감한 사람."""
    from services.assignment_service import group_members_in_month

    members = group_members_in_month(db, group_id, year, month)["members"]
    out = []
    for m in members:
        nid = str(m.get("nurse_id"))
        if m.get("membership_status") in _NOT_TARGET_STATUSES or nid == str(publisher_id or ""):
            continue
        if nid not in out:
            out.append(nid)
    excluded = _not_working(db, out, year, month)
    return [n for n in out if n not in excluded]


def _not_working(db: Session, nurse_ids: list[str], year: int, month: int) -> set[str]:
    """그 달 일하지 않는 사람 — 비활성 · 그 달 말일까지 퇴사 · 그 달과 겹치는 휴직.

    ★ `group_members_in_month` 는 파견 들어온 사람을 원소속 상태와 무관하게 inbound 로 돌려준다.
      그대로 쓰면 원소속에서 비활성·퇴사·휴직 처리된 파견자에게 마감·변경 확인 푸시가 가고 확인 대상이
      되는데, 완료 집계(`ack_counts`)에서는 비활성이 빠져 둘이 어긋난다(Codex 18회차).
      판정은 그 함수의 원소속(home) 규칙과 같다 — 퇴사月부터 `resigned`, 휴직은 active·completed 가
      그 달과 겹치면 `leave`(끝 = end_date, 없으면 expected_end_date).
    """
    from db.models import NurseAssignment

    if not nurse_ids:
        return set()
    m_start = date(year, month, 1)
    m_end = date(year, month, calendar.monthrange(year, month)[1])
    rows = {str(n): (active, rd) for n, active, rd in db.query(
        Nurse.nurse_id, Nurse.active, Nurse.resignation_date).filter(Nurse.nurse_id.in_(nurse_ids)).all()}
    out = {nid for nid in nurse_ids if rows.get(nid) is None or rows[nid][0] != 1
           or (_as_date(rows[nid][1]) is not None and _as_date(rows[nid][1]) <= m_end)}
    on_leave = db.query(NurseAssignment.nurse_id, NurseAssignment.end_date, NurseAssignment.expected_end_date).filter(
        NurseAssignment.nurse_id.in_(nurse_ids), NurseAssignment.reason == "휴직",
        NurseAssignment.status.in_(["active", "completed"]), NurseAssignment.start_date <= m_end,
    ).all()
    for nid, end_d, exp_end in on_leave:
        end = _as_date(end_d if end_d is not None else exp_end)
        if end is None or end >= m_start:
            out.add(str(nid))
    return out


def _as_date(v):
    return v.date() if isinstance(v, datetime) else v


def latest_snapshot_before(db: Session, group_id: str, year: int, month: int) -> Optional[IssuedRosterSnapshot]:
    """그 달 가장 최근 마감 스냅샷(활성 여부 무관) — 새 스냅샷을 넣기 **전에** 부를 것."""
    return (
        db.query(IssuedRosterSnapshot)
        .filter(IssuedRosterSnapshot.group_id == group_id,
                IssuedRosterSnapshot.year == year, IssuedRosterSnapshot.month == month)
        .order_by(IssuedRosterSnapshot.snapshot_id.desc())
        .first()
    )


def _pick_base_id(db: Session, prev: Optional[IssuedRosterSnapshot]) -> Optional[int]:
    """비교 기준 스냅샷 번호 — 직전 스냅샷이 아직 확인 중인 알림이면 그 기준을 이어받는다."""
    if prev is None:
        return None
    meta = change_meta(prev)
    if not meta:
        return prev.snapshot_id
    counts = ack_counts(db, [prev])[prev.snapshot_id]
    # ★ 완료 여부는 **잠근 뒤 다시 센 결과**를 따른다 — 처음 센 뒤 대상자가 복구됐으면 아직 확인 중이라
    #   기준을 이어받아야 한다(Codex 12회차). 완료면 그 순간을 남긴다(마감 트랜잭션과 같이 커밋).
    if _is_closed(meta, counts) and _record_closed(db, prev.snapshot_id):
        return prev.snapshot_id
    return int(meta["base_snapshot_id"])


def record_change_notice(
    db: Session, *, group_id: str, year: int, month: int, new_snapshot: IssuedRosterSnapshot,
    prev_snapshot: Optional[IssuedRosterSnapshot], publisher_id: Optional[str],
) -> Optional[dict]:
    """재마감 시 변경 요약(스냅샷 meta)과 확인 대상자 행을 남긴다. 바뀐 것이 없거나 첫 마감이면 None.

    ★ `new_snapshot` 은 flush 돼 snapshot_id 가 있어야 한다. commit 은 호출부(마감 트랜잭션).
    반환: `{notice_id(=snapshot_id), base_snapshot_id, changed_cells, affected_nurses, added_nurses,
    removed_nurses, total_cells, target_count, published_by, published_at,
    my_changed: {nurse_id: 건수}, targets: [nurse_id]}`
    """
    base_id = _pick_base_id(db, prev_snapshot)
    base = db.get(IssuedRosterSnapshot, base_id) if base_id else None
    if base is None:
        return None
    rows, diff = change_rows_between(db, base, new_snapshot)
    if not rows:
        return None
    targets = confirm_targets(db, group_id, year, month, publisher_id)
    my_changed = Counter(r["nurse_id"] for r in rows)
    summary = {
        "base_snapshot_id": base.snapshot_id,
        "changed_cells": len(diff.changed), "affected_nurses": len(diff.affected_nurses),
        "added_nurses": len(diff.added_nurses), "removed_nurses": len(diff.removed_nurses),
        "total_cells": len(rows), "target_count": len(targets),
        "published_by": str(publisher_id) if publisher_id else None,
        "published_at": datetime.now().isoformat(timespec="seconds"),
    }
    # JSON 컬럼은 같은 dict 를 고쳐도 변경으로 안 잡힌다 — 새 dict 로 바꿔 끼운다.
    new_snapshot.meta_json = {**(new_snapshot.meta_json or {}), CHANGE_KEY: summary}
    db.add_all([RosterChangeAck(snapshot_id=new_snapshot.snapshot_id, nurse_id=t,
                                my_changed_cells=my_changed.get(t, 0)) for t in targets])
    return dict(summary, notice_id=new_snapshot.snapshot_id,
                my_changed={t: my_changed.get(t, 0) for t in targets}, targets=targets)


# ───────── 확인 현황·상태 ─────────

def ack_counts(db: Session, snaps: list) -> dict:
    """알림별 확인 현황 — **지금도 확인할 수 있는 대상자**만 센다.

    빠지는 사람: 퇴사·비활성·삭제(`nurses.active`), 그리고 그 병동을 더는 볼 수 없게 된 사람
    (파견 취소·종료, 다른 병동으로 이동 — 확인 API 가 지금 권한을 다시 보므로 확인할 길이 없다).
    `snaps` = snapshot_id·group_id·year·month 를 가진 스냅샷(또는 같은 컬럼의 행).
    반환: `{snapshot_id: {"target_count", "acked_count", "done"}}`
    """
    out = {s.snapshot_id: {"target_count": 0, "acked_count": 0, "done": True} for s in snaps}
    if not snaps:
        return out
    by_id = {s.snapshot_id: s for s in snaps}
    rows = (
        db.query(RosterChangeAck.snapshot_id, RosterChangeAck.nurse_id, RosterChangeAck.acked_at,
                 Nurse.group_id, Nurse.hn_auth)
        .join(Nurse, Nurse.nurse_id == RosterChangeAck.nurse_id)
        .filter(RosterChangeAck.snapshot_id.in_(list(by_id)), Nurse.active == 1)
        .all()
    )
    managers = _group_managers(db, {s.group_id for s in snaps})
    for sid, nurse_id, acked_at, home, hn_auth in rows:
        if not _can_still_access(db, nurse_id, home, by_id[sid], hn_auth, managers):
            continue
        c = out[sid]
        c["target_count"] += 1
        c["acked_count"] += 1 if acked_at else 0
    for c in out.values():
        c["done"] = c["acked_count"] >= c["target_count"]
    return out


def _group_managers(db: Session, group_ids: set) -> dict[str, set[str]]:
    """병동별 그룹관리자 nurse_id(`groups.hn_id`). 값은 JSON 목록이고 원소는 문자열이다."""
    import json as _json

    out: dict[str, set[str]] = {}
    if not group_ids:
        return out
    for gid, hn_ids in db.query(Group.group_id, Group.hn_id).filter(Group.group_id.in_(list(group_ids))).all():
        if isinstance(hn_ids, str):
            try:
                hn_ids = _json.loads(hn_ids)
            except ValueError:
                hn_ids = []
        out[str(gid)] = {str(x) for x in (hn_ids or [])}
    return out


def _can_still_access(db: Session, nurse_id: str, home_group_id, snap, hn_auth=None,
                      managers: Optional[dict] = None) -> bool:
    """그 간호사가 지금도 알림의 병동·달을 볼 수 있는가 — 상세·확인 API(`resolve_effective_group`
    + `allow_assignment_target`)와 **같은 기준**: 소속 병동 · 그룹관리자로 관리하는 병동(`hn_auth='HN'` 이고
    `groups.hn_id` 에 있음) · 그 달 파견·병동이동으로 들어옴.

    ★ 그룹관리자 관리병동을 빼면, 소속이 바뀌었지만 옛 병동을 계속 관리하는(그래서 확인할 수 있는) 사람이
      집계에서 빠져 미확인인데도 알림이 완료된다(Codex 16회차).
    """
    from services.group_access import _has_active_inbound_assignment

    if str(home_group_id or "") == str(snap.group_id):
        return True
    if str(hn_auth or "").upper() == "HN":
        mgrs = managers if managers is not None else _group_managers(db, {snap.group_id})
        if str(nurse_id) in mgrs.get(str(snap.group_id), set()):
            return True
    return _has_active_inbound_assignment(db, str(nurse_id), snap.group_id, (int(snap.year), int(snap.month)))


def _record_closed(db: Session, snapshot_id: int) -> bool:
    """완료가 **처음 보인 순간**을 스냅샷 meta 에 남긴다(커밋은 호출부). 반환 = 잠근 뒤 판정한 완료 여부.

    ★ 완료는 셈으로 판정하는데, 남은 미확인자가 비활성·권한 상실로 빠져서 끝난 경우 그 사람이 돌아오면
      셈이 다시 늘어 끝난 알림이 되살아난다. 확인·조회·재마감 어디서든 완료를 처음 본 곳에서 남긴다.
    ★ 스냅샷 행을 잠그고(UPDLOCK) 다시 읽어 쓴다 — 같은 meta 를 고치는 확인 요청과 겹치지 않게.
    """
    db.query(IssuedRosterSnapshot.snapshot_id).with_hint(
        IssuedRosterSnapshot, "WITH (UPDLOCK, ROWLOCK)", "mssql").filter(
        IssuedRosterSnapshot.snapshot_id == snapshot_id).one()
    snap = db.get(IssuedRosterSnapshot, snapshot_id)
    db.refresh(snap, attribute_names=["meta_json"])
    meta = change_meta(snap)
    if not meta:
        return True
    if meta.get("closed_at"):
        return True
    # ★ 잠근 뒤 **다시 센다** — 앞에서 센 뒤 대상자가 복구(재활성·파견 복원)됐으면 아직 완료가 아니다
    #   (Codex 7회차: 판정과 기록 사이 경쟁으로 미확인자가 남은 알림이 영구 완료되던 것).
    if not ack_counts(db, [snap])[snap.snapshot_id]["done"]:
        return False
    snap.meta_json = {**(snap.meta_json or {}),
                      CHANGE_KEY: {**meta, "closed_at": datetime.now().isoformat(timespec="seconds")}}
    return True


def _later_snapshot_id(db: Session, snap) -> Optional[int]:
    """같은 병동·같은 달에서 이 스냅샷 뒤에 다시 마감된 스냅샷(가장 최근)."""
    return (
        db.query(IssuedRosterSnapshot.snapshot_id)
        .filter(IssuedRosterSnapshot.group_id == snap.group_id, IssuedRosterSnapshot.year == snap.year,
                IssuedRosterSnapshot.month == snap.month, IssuedRosterSnapshot.snapshot_id > snap.snapshot_id)
        .order_by(IssuedRosterSnapshot.snapshot_id.desc())
        .limit(1)
        .scalar()
    )


def _close_newly_done(db: Session, snaps: list) -> None:
    """조회에서 완료를 처음 본 알림의 완료 시각을 남기고 커밋한다(이미 남긴 것은 건너뜀)."""
    todo = [s.snapshot_id for s in snaps if not (change_meta(s) or {}).get("closed_at")]
    for sid in todo:
        _record_closed(db, sid)
    if todo:
        db.commit()


def _is_closed(meta: dict, counts: dict) -> bool:
    """완료 — 마지막 확인으로 닫힌 기록(`closed_at`)이 있거나, 지금 활성인 대상자가 전원 확인함.

    ★ 닫힌 순간을 남기는 이유: 완료 뒤 대상자가 다시 활성이 되면(복직·실수로 비활성 처리 후 복구)
      셈만으로는 미확인자가 생겨 끝난 알림이 되살아난다(실측 2026-10-02).
    """
    return bool(meta.get("closed_at")) or counts["done"]


def _later_notice_id(db: Session, later_id: Optional[int]) -> Optional[int]:
    """뒤에 다시 마감된 스냅샷이 **변경 알림이면** 그 번호, 아니면 None.

    ★ 철회 후 원래대로 되돌려 다시 마감하면 뒤 스냅샷에는 바뀐 칸이 없어 알림이 없다. 그 번호를
      `superseded_by` 로 주면 프론트가 상세를 열다 404 를 받는다(Codex 5회차).
    """
    if not later_id:
        return None
    meta = db.query(IssuedRosterSnapshot.meta_json).filter(IssuedRosterSnapshot.snapshot_id == later_id).scalar()
    return later_id if (meta or {}).get(CHANGE_KEY) else None


def _status(snap, counts: dict, later_id: Optional[int]) -> str:
    """open(확인 중) · closed(완료) · superseded(다시 마감됨) · withdrawn(마감 철회 중)."""
    if not snap.is_active_issued:
        return "superseded" if later_id else "withdrawn"
    return "closed" if _is_closed(change_meta(snap) or {}, counts) else "open"


#: 요약용 스냅샷 컬럼 — 근무표 본문(roster_json 등 큰 JSON)은 읽지 않는다.
_BRIEF_COLS = (IssuedRosterSnapshot.snapshot_id, IssuedRosterSnapshot.group_id, IssuedRosterSnapshot.year,
               IssuedRosterSnapshot.month, IssuedRosterSnapshot.is_active_issued, IssuedRosterSnapshot.meta_json)


# ───────── 조회·확인 API 본체 ─────────

def _names(db: Session, ids: set) -> dict:
    ids = {str(i) for i in ids if i}
    if not ids:
        return {}
    return {str(n): name for n, name in db.query(Nurse.nurse_id, Nurse.name).filter(Nurse.nurse_id.in_(list(ids))).all()}


def _summary(snap, status: str, counts: dict, my: Optional[RosterChangeAck], names: dict,
             later_id: Optional[int] = None) -> dict:
    meta = change_meta(snap) or {}
    return {
        "notice_id": snap.snapshot_id, "group_id": snap.group_id, "year": snap.year, "month": snap.month,
        "status": status,
        "base_snapshot_id": meta.get("base_snapshot_id"),
        "published_at": meta.get("published_at"),
        "published_by": meta.get("published_by"),
        "published_by_name": names.get(str(meta.get("published_by"))) if meta.get("published_by") else None,
        "changed_cells": meta.get("changed_cells", 0), "affected_nurses": meta.get("affected_nurses", 0),
        "added_nurses": meta.get("added_nurses", 0), "removed_nurses": meta.get("removed_nurses", 0),
        "total_cells": meta.get("total_cells", 0),
        "closed_at": meta.get("closed_at"),
        "confirm": {"target_count": counts["target_count"], "acked_count": counts["acked_count"]},
        "my": {
            "is_target": my is not None,
            "my_changed_cells": my.my_changed_cells if my else 0,
            "acked_at": my.acked_at.isoformat() if my and my.acked_at else None,
        },
        # 대체한 **변경 알림** 번호(뒤 마감에 바뀐 칸이 없어 알림이 없으면 null)
        "superseded_by": later_id if status == "superseded" else None,
    }


def _rows_for(db: Session, snap: IssuedRosterSnapshot) -> list[dict]:
    """알림의 바뀐 칸 — 기준 스냅샷과 이번 스냅샷을 비교해 계산(저장하지 않는다)."""
    meta = change_meta(snap) or {}
    base = db.get(IssuedRosterSnapshot, int(meta["base_snapshot_id"])) if meta.get("base_snapshot_id") else None
    return change_rows_between(db, base, snap)[0] if base is not None else []


def _my_acks(db: Session, snapshot_ids: list[int], nurse_id: Optional[str]) -> dict:
    """내 확인 행. 비활성(퇴사·삭제)이면 없다 — 완료 집계(`ack_counts`)와 같은 사람 집합을 본다.
    그래서 `my.is_target` 은 거짓이고 확인 API 는 403 이다(확인해도 완료 숫자에 안 들어가므로)."""
    if not nurse_id or not snapshot_ids:
        return {}
    rows = (db.query(RosterChangeAck)
            .join(Nurse, Nurse.nurse_id == RosterChangeAck.nurse_id)
            .filter(RosterChangeAck.snapshot_id.in_(snapshot_ids), RosterChangeAck.nurse_id == str(nurse_id),
                    Nurse.active == 1)
            .all())
    return {r.snapshot_id: r for r in rows}


def _caller_is_manager_of(db: Session, current_user, group_id: str) -> bool:
    from services.group_access import caller_is_manager, resolve_managed_group_ids

    if bool(getattr(current_user, "is_master_admin", False)):
        return True
    return str(group_id) in {str(g) for g in resolve_managed_group_ids(db, current_user)} \
        and caller_is_manager(db, current_user)


def month_notices(db: Session, current_user, year: int, month: int, group_id: Optional[str]) -> dict:
    """그 달 **확인 중인** 변경 알림과 바뀐 칸(화면 칸 표시용). 전원 확인·철회 중이면 비어 있다."""
    from services.group_access import resolve_effective_group, resolve_home_group_id

    target = resolve_effective_group(
        db, current_user, group_id, require_group=False,
        allow_assignment_target=True, assignment_window=(year, month),
    ) or resolve_home_group_id(db, current_user)
    if not target:
        raise HTTPException(status_code=400, detail="대상 그룹이 없습니다.")
    active = (
        db.query(IssuedRosterSnapshot)
        .filter(IssuedRosterSnapshot.group_id == target, IssuedRosterSnapshot.year == year,
                IssuedRosterSnapshot.month == month, IssuedRosterSnapshot.is_active_issued == True)  # noqa: E712
        .order_by(IssuedRosterSnapshot.snapshot_id.desc())
        .all()
    )
    snaps = [s for s in active if change_meta(s)]
    counts = ack_counts(db, snaps)
    _close_newly_done(db, [s for s in snaps if counts[s.snapshot_id]["done"]])
    snaps = [s for s in snaps if _status(s, counts[s.snapshot_id], None) == "open"]
    mine = _my_acks(db, [s.snapshot_id for s in snaps], getattr(current_user, "nurse_id", None))
    rows_by = {s.snapshot_id: _rows_for(db, s) for s in snaps}
    names = _names(db, {(change_meta(s) or {}).get("published_by") for s in snaps}
                   | {r["nurse_id"] for rows in rows_by.values() for r in rows})
    return {
        "group_id": target, "year": year, "month": month,
        "is_manager": _caller_is_manager_of(db, current_user, target),
        "notices": [dict(_summary(s, "open", counts[s.snapshot_id], mine.get(s.snapshot_id), names),
                         cells=diff_cells_payload(rows_by[s.snapshot_id], names)) for s in snaps],
    }


def pending_for_me(db: Session, current_user) -> dict:
    """내가 아직 확인하지 않은 확인 중 알림(앱 홈 배너용) — 철회 중·대체됨·전원 확인된 것은 빼고.

    ★ 근무표 본문은 읽지 않는다(앱을 열 때마다 불린다) — 요약은 스냅샷 meta 에 있다.
    """
    nurse_id = getattr(current_user, "nurse_id", None)
    if not nurse_id:
        return {"notices": []}
    me_row = db.query(Nurse.group_id, Nurse.hn_auth, Nurse.active).filter(Nurse.nurse_id == str(nurse_id)).first()
    # ★ 비활성(퇴사·삭제)이면 완료 집계(`ack_counts`)에서 빠지므로 배너도 비운다 — 토큰은 아직 살아 있을 수 있다(Codex 20회차).
    if me_row is None or me_row[2] != 1:
        return {"notices": []}
    home, my_hn_auth = me_row[0], me_row[1]
    mine = {a.snapshot_id: a for a in db.query(RosterChangeAck).filter(
        RosterChangeAck.nurse_id == str(nurse_id), RosterChangeAck.acked_at.is_(None)).all()}
    if not mine:
        return {"notices": []}
    snaps = (
        db.query(*_BRIEF_COLS)
        .filter(IssuedRosterSnapshot.snapshot_id.in_(list(mine)),
                IssuedRosterSnapshot.is_active_issued == True)  # noqa: E712
        .order_by(IssuedRosterSnapshot.snapshot_id.desc())
        .all()
    )
    counts = ack_counts(db, snaps)
    _close_newly_done(db, [s for s in snaps if counts[s.snapshot_id]["done"]])
    managers = _group_managers(db, {s.group_id for s in snaps})
    # ★ 지금 그 병동을 볼 수 없게 된 알림(파견 취소·종료, 병동 이동)은 배너에서도 뺀다 — 상세·확인이 403 이다.
    snaps = [s for s in snaps if _status(s, counts[s.snapshot_id], None) == "open"
             and _can_still_access(db, nurse_id, home, s, my_hn_auth, managers)]
    groups = dict(db.query(Group.group_id, Group.group_name).filter(
        Group.group_id.in_(list({s.group_id for s in snaps}))).all()) if snaps else {}
    names = _names(db, {(change_meta(s) or {}).get("published_by") for s in snaps})
    return {"notices": [
        dict(_summary(s, "open", counts[s.snapshot_id], mine[s.snapshot_id], names), group_name=groups.get(s.group_id))
        for s in snaps
    ]}


def _load_notice_for_caller(db: Session, current_user, notice_id: int):
    """알림(= 변경 요약이 있는 스냅샷) + 내 확인 행. 지금 그 병동(그 달 파견 포함)을 볼 수 있는 사람만."""
    from services.group_access import resolve_effective_group

    snap = db.get(IssuedRosterSnapshot, int(notice_id))
    if snap is None or not change_meta(snap):
        raise HTTPException(status_code=404, detail="변경 알림을 찾을 수 없습니다.")
    # ★ 확인 대상이어도 **지금 권한**을 본다. 대상 행만으로 통과시키면 파견 취소·종료나 병동 이동으로
    #   권한을 잃은 사람이 병동 전체 변경 칸·이름을 계속 보고 확인까지 한다(Codex 1회차 HIGH).
    resolve_effective_group(db, current_user, snap.group_id, allow_assignment_target=True,
                            assignment_window=(snap.year, snap.month))   # 아니면 403
    mine = _my_acks(db, [snap.snapshot_id], getattr(current_user, "nurse_id", None)).get(snap.snapshot_id)
    return snap, mine


def notice_detail(db: Session, current_user, notice_id: int) -> dict:
    """알림 1건의 바뀐 칸 + 내 확인 여부. 관리자에게는 대상자별 확인 시각도."""
    snap, mine = _load_notice_for_caller(db, current_user, notice_id)
    counts = ack_counts(db, [snap])[snap.snapshot_id]
    if snap.is_active_issued:
        _close_newly_done(db, [snap] if counts["done"] else [])
    later = _later_snapshot_id(db, snap)
    rows = _rows_for(db, snap)
    is_manager = _caller_is_manager_of(db, current_user, snap.group_id)
    acks = db.query(RosterChangeAck).filter(RosterChangeAck.snapshot_id == snap.snapshot_id).all() if is_manager else []
    names = _names(db, {r["nurse_id"] for r in rows} | {(change_meta(snap) or {}).get("published_by")}
                   | {a.nurse_id for a in acks})
    out = dict(_summary(snap, _status(snap, counts, later), counts, mine, names, _later_notice_id(db, later)),
               cells=diff_cells_payload(rows, names), is_manager=is_manager)
    if is_manager:
        active = {str(n) for (n,) in db.query(Nurse.nurse_id).filter(
            Nurse.nurse_id.in_([a.nurse_id for a in acks]), Nurse.active == 1).all()} if acks else set()
        out["targets"] = sorted([{
            "nurse_id": a.nurse_id, "nurse_name": names.get(str(a.nurse_id)),
            "my_changed_cells": a.my_changed_cells, "active": str(a.nurse_id) in active,
            "acked_at": a.acked_at.isoformat() if a.acked_at else None, "ack_via": a.ack_via,
        } for a in acks], key=lambda t: (t["acked_at"] is not None, not t["active"], t["nurse_name"] or ""))
    return out


def _require_my_ack(db: Session, current_user, notice_id: int):
    """지금 그 병동을 볼 수 있고(아니면 403) 활성인 확인 대상(아니면 403)의 알림·내 확인 행."""
    snap, mine = _load_notice_for_caller(db, current_user, notice_id)
    if mine is None:
        raise HTTPException(status_code=403, detail="이 변경 알림의 확인 대상이 아닙니다.")
    return snap, mine


def ack_notice(db: Session, current_user, notice_id: int, via: str) -> dict:
    """내 확인 기록. 다시 눌러도 같은 결과.

    ★ **스냅샷 행을 먼저 잠근다**(UPDLOCK, ROWLOCK). 재마감·철회가 스냅샷을 비활성으로 바꾸는 것과
      겹칠 때, 잠근 뒤 다시 읽어 이미 대체·철회된 알림에는 확인을 남기지 않는다.
      같은 알림의 확인끼리도 줄을 세워, 마지막 두 사람이 동시에 눌렀을 때 서로의 미커밋 행을 기다려
      교착(1205)에 빠지지 않게 한다.
    """
    snap, mine = _require_my_ack(db, current_user, notice_id)
    db.query(IssuedRosterSnapshot.snapshot_id).with_hint(
        IssuedRosterSnapshot, "WITH (UPDLOCK, ROWLOCK)", "mssql").filter(
        IssuedRosterSnapshot.snapshot_id == snap.snapshot_id).one()
    # ★ 잠금을 기다리는 사이 비활성·파견 취소·병동 이동이 커밋됐을 수 있다 — 잠근 뒤 자격을 다시 본다.
    #   잠금 전 판정을 그대로 쓰면 집계에서 빠진 사람의 확인이 200 으로 남는다(Codex 21회차).
    #   세션에 올라온 간호사·스냅샷 행은 다시 조회해도 옛 값이 그대로라 먼저 비운다(아직 바꾼 것 없음).
    db.expire_all()
    snap, mine = _require_my_ack(db, current_user, notice_id)
    db.refresh(snap, attribute_names=["is_active_issued"])
    if not snap.is_active_issued:
        later = _later_snapshot_id(db, snap)
        later_notice = _later_notice_id(db, later)
        if later and later_notice:
            message = "그 사이 근무표가 다시 마감됐습니다. 새 변경 내용을 확인해 주세요."
        elif later:
            message = "그 사이 근무표가 다시 마감됐습니다. 바뀐 칸이 없어 새로 확인할 내용은 없습니다."
        else:
            message = "근무표가 마감 철회돼 지금은 확인할 수 없습니다."
        raise HTTPException(status_code=409, detail={"message": message, "superseded_by": later_notice})
    now = datetime.now()
    db.query(RosterChangeAck).filter(
        RosterChangeAck.snapshot_id == snap.snapshot_id, RosterChangeAck.nurse_id == mine.nurse_id,
        RosterChangeAck.acked_at.is_(None),
    ).update({"acked_at": now, "ack_via": via}, synchronize_session=False)
    db.flush()
    counts = ack_counts(db, [snap])[snap.snapshot_id]
    if counts["done"]:
        _record_closed(db, snap.snapshot_id)   # 마지막 확인으로 닫힌 순간(스냅샷 행은 위에서 잠가 둠)
    db.commit()
    acked = db.query(RosterChangeAck.acked_at).filter(
        RosterChangeAck.snapshot_id == snap.snapshot_id, RosterChangeAck.nurse_id == mine.nurse_id).scalar()
    return {"notice_id": snap.snapshot_id, "acked_at": acked.isoformat() if acked else None,
            "status": _status(snap, counts, None),
            "confirm": {"target_count": counts["target_count"], "acked_count": counts["acked_count"]}}
