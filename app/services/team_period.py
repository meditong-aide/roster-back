"""team 시점 구간 해석/기록 (Phase 1).

`nurse_team_period` 가 진실, `nurses.team_id` 는 현재값 캐시. 모든 화면/생성기는
시점 team 을 이 모듈을 통해서만 읽어 SSOT 를 유지한다.

규칙(docs/TEMPORAL_NURSE_MODEL_DESIGN.md v3):
- 변경 = close-before-open(옛 구간 valid_to 닫고 새 구간 open), 삭제 금지.
- 겹침 금지, gap(미지정) 허용, valid_to=null 은 열린 구간.
- 폴백 ward-aware: 구간 없으면 nurses.group_id==group 일 때만 nurses.team_id, 아니면 None.
"""

from __future__ import annotations

from calendar import monthrange
from datetime import date
from typing import Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from db.models import Nurse as NurseModel
from db.models import NurseTeamPeriod


def get_team_period_on(
    db: Session, nurse_id: str, group_id: str, on_date: date
) -> Optional[NurseTeamPeriod]:
    """그 날짜를 덮는 구간(없으면 None). [valid_from, valid_to) 반쪽열림."""
    return (
        db.query(NurseTeamPeriod)
        .filter(
            NurseTeamPeriod.nurse_id == nurse_id,
            NurseTeamPeriod.group_id == group_id,
            NurseTeamPeriod.valid_from <= on_date,
            or_(
                NurseTeamPeriod.valid_to.is_(None),
                NurseTeamPeriod.valid_to > on_date,
            ),
        )
        .order_by(NurseTeamPeriod.valid_from.desc())
        .first()
    )


def _coerce_team_int(value) -> Optional[int]:
    """team_id 를 int 로 정규화(실패 시 None).

    MSSQL(pymssql)이 nurses.team_id 를 문자열 '1'/'2' 로 돌려주는 트랩 방어 —
    period(INT 컬럼)는 int, 캐시 폴백은 str 라 resolve 결과 타입이 섞이면 프론트의
    Map 키(team.team_id 숫자) 매칭이 깨져 '팀 비어 보임' 버그가 난다. 항상 int 로 통일.
    """
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _ward_aware_fallback(db: Session, nurse_id: str, group_id: str) -> Optional[int]:
    """구간 없을 때: nurses.group_id==group 일 때만 nurses.team_id, 아니면 None."""
    row = (
        db.query(NurseModel.group_id, NurseModel.team_id)
        .filter(NurseModel.nurse_id == nurse_id)
        .first()
    )
    # MSSQL CHAR 컬럼 트레일링 공백/포맷 차이 방어: strip 후 비교.
    if row and str(row[0] or "").strip() == str(group_id or "").strip():
        return _coerce_team_int(row[1])
    return None


def resolve_team(
    db: Session, nurse_id: str, group_id: str, on_date: date
) -> Optional[int]:
    """그 날짜의 유효 team_id. 구간 우선, 없으면 ward-aware 폴백."""
    p = get_team_period_on(db, nurse_id, group_id, on_date)
    if p is not None:
        return _coerce_team_int(p.team_id)
    return _ward_aware_fallback(db, nurse_id, group_id)


def resolve_team_for_roster(
    db: Session, nurse_id: str, group_id: str, year: int, month: int
) -> Optional[int]:
    """로스터(병동·월)용 단일 team — Phase1(gap만): 월초를 덮는 구간 우선,
    없으면 그 달에 겹치는 첫 구간(중간 합류 대비), 그것도 없으면 ward-aware 폴백.
    """
    m_start = date(year, month, 1)
    m_end = date(year, month, monthrange(year, month)[1])
    periods = (
        db.query(NurseTeamPeriod)
        .filter(
            NurseTeamPeriod.nurse_id == nurse_id,
            NurseTeamPeriod.group_id == group_id,
            NurseTeamPeriod.valid_from <= m_end,
            or_(
                NurseTeamPeriod.valid_to.is_(None),
                NurseTeamPeriod.valid_to > m_start,
            ),
        )
        .order_by(NurseTeamPeriod.valid_from.asc())
        .all()
    )
    if periods:
        covering = [
            p for p in periods
            if p.valid_from <= m_start and (p.valid_to is None or p.valid_to > m_start)
        ]
        # ★ 월초를 덮는 구간이 여럿이면(겹침 — 원래 없어야 하지만 실측 운영 20쌍) **가장 늦게
        #   시작한** 것을 쓴다. 화면(`get_team_period_on`·`group_members_in_month`)과 같은 선택이다.
        #   예전엔 `covering[0]`(가장 먼저 시작)이라 생성기만 다른 팀으로 팀 제약을 걸었다(2026-10-02).
        return _coerce_team_int((covering[-1] if covering else periods[0]).team_id)
    return _ward_aware_fallback(db, nurse_id, group_id)


def resolve_teams_for_month(
    db: Session, group_id: str, on_date: date
) -> dict:
    """그룹 전체 활성 간호사의 on_date 시점 팀 — period 우선 + ward-aware 캐시 폴백.

    배치(2쿼리)로 N+1 회피. 반환 {nurse_id(str): team_id(int)|None}.
    nurses.team_id 일괄 NULL 이행 후엔 캐시가 비어 자동으로 period-only 가 된다.
    """
    cache = {
        str(nid): _coerce_team_int(tid)
        for nid, tid in db.query(NurseModel.nurse_id, NurseModel.team_id)
        .filter(NurseModel.group_id == group_id, NurseModel.active == 1)
        .all()
    }
    period: dict[str, Optional[int]] = {}
    for p in (
        db.query(NurseTeamPeriod)
        .filter(
            NurseTeamPeriod.group_id == group_id,
            NurseTeamPeriod.valid_from <= on_date,
            or_(
                NurseTeamPeriod.valid_to.is_(None),
                NurseTeamPeriod.valid_to > on_date,
            ),
        )
        .order_by(NurseTeamPeriod.valid_from.desc())
        .all()
    ):
        pid = str(p.nurse_id)
        if pid not in period:  # valid_from desc 첫 등장 = 구간 우선
            period[pid] = _coerce_team_int(p.team_id)
    return {
        nid: (period[nid] if nid in period else cache[nid])
        for nid in cache
    }


def sort_team_ids_by_name(team_name_map: dict) -> list:
    """팀 id 들을 근무표 만들기 화면(team-sort-util.sortTeamGroupsByTeamName)과 동일
    순서로 정렬한다. 1순위 그룹: 한글(0) < 영문(1) < 숫자(2) < 기타(3). 2순위: 자연
    정렬(숫자 런은 int 로 비교)+소문자. 3순위: team_id.
    (활성 실팀만 넘어오므로 미배정/임시팀 분기는 불필요. 엑셀 내보내기·마감본 팀 목록 공용.)
    """
    import re as _re

    def _group_rank(name: str) -> int:
        first = (name or "").strip()[:1]
        if not first:
            return 3
        if "가" <= first <= "힣":
            return 0
        if first.isascii() and first.isalpha():
            return 1
        if first.isdigit():
            return 2
        return 3

    def _natural_key(name: str) -> list:
        # Intl.Collator({numeric:true}) 근사: 숫자 런은 (0,int), 그 외는 (1,소문자).
        # 토큰 타입을 앞에 둬 int/str 직접 비교(TypeError)를 원천 차단.
        out = []
        for tok in _re.findall(r"\d+|\D+", name or ""):
            out.append((0, int(tok)) if tok.isdigit() else (1, tok.casefold()))
        return out

    def _key(tid):
        name = team_name_map.get(tid) or ""
        return (_group_rank(name), _natural_key(name), tid)

    return sorted(team_name_map.keys(), key=_key)


def active_team_names(db: Session, group_id: str) -> dict[int, str]:
    """그 병동의 활성 팀 `{team_id: team_name}`.

    ★ `office_id` 는 걸지 않는다 — `group_id` 가 병원을 확정한다. 호출자 병원을 겹쳐 걸면 타 병원
      관리병동을 마감할 때 0건이 되어 전원 미등록으로 **고정**된다(Codex 2026-10-02 · 발행 라우터의
      2026-09-01 office_id 제거와 같은 이유). 실측 dev: `(group_id, team_id)` 중복 0 ·
      `teams.office_id ≠ groups.office_id` 0.
    """
    from db.models import Team

    return {
        int(tid): name
        for tid, name in db.query(Team.team_id, Team.team_name).filter(
            Team.group_id == group_id, Team.active == 1,
        )
    }


def ordered_teams(team_names: dict[int, str]) -> list[dict]:
    """`[{team_id, team_name}]` 팀명순(PC `sortTeamsByName` 과 같은 순서)."""
    return [{"team_id": tid, "team_name": team_names[tid]} for tid in sort_team_ids_by_name(team_names)]


def month_team_layout(
    db: Session, group_id: str, year: int, month: int,
) -> tuple[dict[str, int | None], list[dict]]:
    """그 달 팀 배치 — 근무자관리 화면과 같은 기준. 반환 `({nurse_id: team_id|None}, 팀 목록)`.

    팀 = `group_members_in_month` 의 `as_of_team`(월초를 덮는 구간 우선, 없으면 이 병동 소속일
    때만 `nurses.team_id`). 파견 온 사람도 이 병동 구간이 있으면 그 팀이다. 활성 팀이 아니거나
    그 달 명단에 없는 사람은 None(미등록).
    ★ PC 마감본 팀별 보기가 쓰던 `/teams`(`resolve_teams_for_month`)는 이 병동 재직자만 세서
      파견 온 사람을 팀에 넣어 두어도 미등록으로 보였다. 이쪽은 그 경우도 팀으로 묶는다.
    """
    from services.assignment_service import group_members_in_month

    # ★ 명단을 **먼저**, 팀 목록을 **나중에** 읽는다. 반대 순서면 그 사이 새 팀을 만들고 사람을
    #   옮겼을 때 그 사람 팀이 목록에 없어 미등록으로 굳는다(마감은 이 값을 고정한다 — Codex 2026-10-02).
    #   이 순서면 사이에 만든 팀은 목록에 들어 있고, 사이에 없앤 팀은 미등록이 맞다.
    # ★ 한계(사용자 결정 2026-10-02 "그대로 둔다"): 두 조회는 같은 시점이 아니다(RCSI 꺼진 READ
    #   COMMITTED). 마감 처리 도중 몇 ms 사이에 그 병동 팀 편집이 커밋되면 그 사람은 '편집 직전 팀'
    #   (드물게 미등록)으로 마감에 박힌다 — 다시 마감하면 고쳐진다. 마감 스냅샷의 다른 부분(명단·근무
    #   행·시프트)도 같은 구조이고, 팀을 바꾸는 경로가 여럿이라 병동 잠금·격리 수준 변경은 하지 않았다.
    members = group_members_in_month(db, group_id, year, month)["members"]
    names = active_team_names(db, group_id)
    team_of: dict[str, int | None] = {}
    for m in members:
        tid = _coerce_team_int(m.get("as_of_team"))
        team_of[str(m["nurse_id"])] = tid if tid in names else None
    return team_of, ordered_teams(names)


def set_team_period(
    db: Session,
    *,
    nurse_id: str,
    group_id: str,
    valid_from: date,
    team_id: Optional[int],
    source: str = "edited",
    note: Optional[str] = None,
    commit: bool = True,
) -> NurseTeamPeriod:
    """close-before-open 으로 team 구간을 기록한다.

    - valid_from 과 같은 시작일 구간이 이미 있으면 그 행을 갱신(재설정).
    - 아니면 valid_from 을 덮는 직전 열린/겹침 구간들을 valid_to=valid_from 으로 닫고,
      [valid_from, null) 새 구간을 연다. (삭제 없음 — 완전 타임라인)
    """
    # 같은 txn 의 직전 pending 쓰기를 same/covering 쿼리가 보도록 강제 flush.
    #   SessionLocal(autoflush=False)에서 apply_team_ops 가 한 nurse 를 두 번 처리하면
    #   (payload 중복 item / 이동을 두 op 로 표현 등) 두 번째 호출의 same 쿼리가 첫 INSERT 를
    #   못 봐서 동일행을 또 INSERT → 완전중복 행. upsert_period 와 동일하게 flush 로 차단.
    db.flush()
    base = db.query(NurseTeamPeriod).filter(
        NurseTeamPeriod.nurse_id == nurse_id,
        NurseTeamPeriod.group_id == group_id,
    )
    same = base.filter(NurseTeamPeriod.valid_from == valid_from).first()
    if same is not None:
        same.team_id = team_id
        same.source = source
        if note is not None:
            same.note = note
        row = same
    else:
        covering = base.filter(
            NurseTeamPeriod.valid_from < valid_from,
            or_(
                NurseTeamPeriod.valid_to.is_(None),
                NurseTeamPeriod.valid_to > valid_from,
            ),
        ).all()
        # ★ 새 구간의 끝 = 덮던 구간의 끝(분할)과 뒤에 이미 있는 구간의 시작(빈 칸에 열 때) 중
        #   빠른 날. 예전엔 늘 `valid_to=None` 으로 열어, 10월 팀을 먼저 저장하고 9월 팀을 나중에
        #   저장하면 [9/1~열림] 이 [10/1~] 을 덮었다(실측 운영 성남 61병동-RN 20쌍 · 그중 2명은
        #   생성기와 화면 팀이 달랐다 — 2026-10-02 데이터 정리). `upsert_period` 가 같은 버그를 이미
        #   고친 방식과 같다.
        from services.nurse_period_resolver import _next_span_start

        ends = [c.valid_to for c in covering if c.valid_to is not None]
        nxt = _next_span_start(db, NurseTeamPeriod, nurse_id, valid_from, group_id)
        if nxt is not None:
            ends.append(nxt)
        for c in covering:
            c.valid_to = valid_from
        row = NurseTeamPeriod(
            nurse_id=nurse_id, group_id=group_id, valid_from=valid_from,
            valid_to=min(ends) if ends else None, team_id=team_id, source=source, note=note,
        )
        db.add(row)
    # 단일 병동 불변식: 한 간호사는 한 시점에 한 병동에만 속한다. 새 구간을 열 때
    #   다른 그룹의 열린/겹침 구간(valid_from 이전 시작)을 valid_from 에서 닫고,
    #   같은 날 시작하는 다른 그룹 구간은 모순이므로 제거한다.
    #   (병동이동 시 출발 병동 구간 자동 종료 + 재분배 재실행 시 옛 구간 정리 — group-scoped
    #    close 만으로는 cross-ward stale 구간이 영구히 열려 resolve_team 오염되던 버그 차단.)
    others = (
        db.query(NurseTeamPeriod)
        .filter(
            NurseTeamPeriod.nurse_id == nurse_id,
            NurseTeamPeriod.group_id != group_id,
            NurseTeamPeriod.valid_from <= valid_from,
            or_(
                NurseTeamPeriod.valid_to.is_(None),
                NurseTeamPeriod.valid_to > valid_from,
            ),
        )
        .all()
    )
    for o in others:
        if o.valid_from == valid_from:
            db.delete(o)
        else:
            o.valid_to = valid_from
    if commit:
        db.commit()
        db.refresh(row)
    return row
