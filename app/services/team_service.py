from typing import Any, Dict, List, Optional
from sqlalchemy.orm import Session
from db.models import Team, Nurse


_ALLOWED_SHIFT_KEYS = ("D", "E", "N", "M")


def _coerce_min_shift(raw: Any) -> Optional[Dict[str, int]]:
    """payload 의 min_shift 를 정제. 허용 키만 남기고 비음수 정수로 변환.
    None  → None (변경 없음 시그널과 별개로, 클리어 결정 후 호출됨)
    {}    → None (제약 없음)
    {...} → 정제된 dict (비어있으면 None)
    """
    if raw is None or not isinstance(raw, dict):
        return None
    cleaned: Dict[str, int] = {}
    for k, v in raw.items():
        if k not in _ALLOWED_SHIFT_KEYS:
            continue
        try:
            iv = int(v)
        except (TypeError, ValueError):
            continue
        if iv < 0:
            continue
        cleaned[k] = iv
    return cleaned or None


def list_teams_with_members(db: Session, office_id: str, group_id: str) -> List[Dict]:
    """팀 목록과 각 팀 멤버 nurse_id 리스트를 반환한다."""
    teams = db.query(Team).filter(Team.office_id == office_id, Team.group_id == group_id, Team.active == 1).all()
    result = []
    for t in teams:
        members = db.query(Nurse.nurse_id).filter(Nurse.group_id == group_id, Nurse.team_id == t.team_id).all()
        result.append({
            'team_id': t.team_id,
            'team_name': t.team_name,
            'team_members': [m[0] for m in members],
            'min_shift': t.min_shift if isinstance(t.min_shift, dict) else None,
        })
    return result


def apply_team_ops(db: Session, office_id: str, group_id: str, payload: List[Dict], delete_team_ids: List[int] | None = None) -> List[Dict]:
    """증분 오퍼레이션을 적용한다: create/rename/add/remove/delete."""
    existing = db.query(Team).filter(Team.office_id == office_id, Team.group_id == group_id).all()
    by_id = {t.team_id: t for t in existing}
    by_name = {t.team_name: t for t in existing if t.active == 1}
    print('payload', payload)
    # 1) 팀별 ops 처리
    for item in payload:
        team_id = item.get('team_id')
        team_name = item.get('team_name')
        add_ids = list(dict.fromkeys(item.get('add') or []))
        remove_ids = list(dict.fromkeys(item.get('remove') or []))
        # min_shift 시그널 (pydantic .dict()는 항상 키를 포함하므로 값으로 구분):
        #   None  → 변경 없음 (skip)
        #   {}    → 클리어(NULL)
        #   {...} → 정제 후 저장
        ms_raw = item.get('min_shift')
        update_ms = ms_raw is not None
        min_shift_value = _coerce_min_shift(ms_raw) if update_ms else None

        # upsert team (team_id 없으면 그룹 내 next team_id 할당)
        team = None
        if team_id:
            team = by_id.get(team_id)
        if team is None and team_name:
            team = by_name.get(team_name)
        if team is None:
            if not team_name:
                # team_name 없이 신규 생성 불가
                continue
            # 그룹 내 최대 team_id + 1 부여
            max_id_row = db.query(Team.team_id).filter(Team.office_id == office_id, Team.group_id == group_id).order_by(Team.team_id.desc()).first()
            next_team_id = (max_id_row[0] + 1) if max_id_row else 1
            team = Team(office_id=office_id, group_id=group_id, team_id=next_team_id, team_name=team_name, active=1)
            if update_ms:
                team.min_shift = min_shift_value
            db.add(team)
            db.flush()
            by_id[team.team_id] = team
            by_name[team.team_name] = team
        else:
            if team_name:
                team.team_name = team_name
            team.active = 1
            if update_ms:
                # {} → None(클리어), {...} → 정제값
                team.min_shift = min_shift_value

        # add: 타깃 팀으로 이동(원팀 자동 해제)
        if add_ids:
            db.query(Nurse).filter(Nurse.group_id == group_id, Nurse.nurse_id.in_(add_ids)).update({Nurse.team_id: team.team_id}, synchronize_session=False)

        # remove: 미배정 처리
        if remove_ids:
            db.query(Nurse).filter(Nurse.group_id == group_id, Nurse.nurse_id.in_(remove_ids)).update({Nurse.team_id: None}, synchronize_session=False)

    # 2) 팀 삭제(soft) + 멤버 해제
    if delete_team_ids:
        print('delete_team_ids', delete_team_ids)
        # 멤버 해제 후 팀 행 삭제(하드 삭제)
        db.query(Nurse).filter(Nurse.group_id == group_id, Nurse.team_id.in_(delete_team_ids)).update({Nurse.team_id: None}, synchronize_session=False)
        db.query(Team).filter(Team.office_id == office_id, Team.group_id == group_id, Team.team_id.in_(delete_team_ids)).delete(synchronize_session=False)

    db.commit()
    return list_teams_with_members(db, office_id, group_id)


