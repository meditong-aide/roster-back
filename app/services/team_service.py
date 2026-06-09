from typing import Any, Dict, List, Optional
from sqlalchemy.orm import Session
from db.models import Team, Nurse, NurseTeamPeriod
from services.precheck.team_min_shift_capacity_validator import validate_team_min_shift_capacity


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


def _coerce_handoff_policy(raw: Any) -> Optional[Dict[str, Any]]:
    """payload 의 handoff_policy 를 정제.
    None → None (변경 없음 시그널과 별개로, 클리어 결정 후 호출됨)
    {}   → None (정책 없음)
    {"restrictions":[{...}, ...]} → 유효 규칙만 남긴 dict. 규칙 0개면 None.

    규칙 스키마(v1):
        {"grades":[int,...], "block_same_shift":bool, "block_adjacent":bool}
    미래 확장(v2, 파싱만 허용하고 유지; CP-SAT 쪽에서 처리):
        {"from":[...], "to":[...], "bidirectional":bool, "block_adjacent":bool}
    """
    if raw is None or not isinstance(raw, dict):
        return None
    rules_in = raw.get("restrictions")
    if not isinstance(rules_in, list):
        return None
    cleaned_rules: List[Dict[str, Any]] = []
    for r in rules_in:
        if not isinstance(r, dict):
            continue
        out: Dict[str, Any] = {}
        # v1: symmetric set
        if "grades" in r:
            grades = [int(g) for g in (r.get("grades") or []) if _safe_int(g) is not None]
            grades = sorted(set(g for g in grades if g is not None))
            if not grades:
                continue
            out["grades"] = grades
        # v2: directional (현재 CP-SAT 에서는 대칭만 처리. 스키마는 보존.)
        elif "from" in r and "to" in r:
            frs = sorted(set(g for g in (_safe_int(x) for x in (r.get("from") or [])) if g is not None))
            tos = sorted(set(g for g in (_safe_int(x) for x in (r.get("to") or [])) if g is not None))
            if not frs or not tos:
                continue
            out["from"] = frs
            out["to"] = tos
            if r.get("bidirectional"):
                out["bidirectional"] = True
        else:
            continue
        out["block_same_shift"] = bool(r.get("block_same_shift", False))
        out["block_adjacent"] = bool(r.get("block_adjacent", True))
        if not out["block_same_shift"] and not out["block_adjacent"]:
            continue
        cleaned_rules.append(out)
    if not cleaned_rules:
        return None
    return {"restrictions": cleaned_rules}


def _safe_int(v: Any) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


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
            'handoff_policy': t.handoff_policy if isinstance(t.handoff_policy, dict) else None,
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

        # handoff_policy 시그널 (min_shift 와 동일한 3-상태):
        #   None  → 변경 없음, {} → 클리어(NULL), {...} → 정제 후 저장
        hp_raw = item.get('handoff_policy')
        update_hp = hp_raw is not None
        handoff_policy_value = _coerce_handoff_policy(hp_raw) if update_hp else None

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
            if update_hp:
                team.handoff_policy = handoff_policy_value
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
            if update_hp:
                team.handoff_policy = handoff_policy_value

        # min_shift 저장 전 검증 (현재 요청의 add/remove 반영한 projected members 기준)
        if update_ms:
            current_member_ids = {
                str(x[0])
                for x in db.query(Nurse.nurse_id)
                .filter(Nurse.group_id == group_id, Nurse.team_id == team.team_id)
                .all()
            }
            projected_ids = (current_member_ids | set(add_ids)) - set(remove_ids)
            vr = validate_team_min_shift_capacity(
                db,
                office_id=office_id,
                group_id=group_id,
                team_id=team.team_id,
                new_min_shift=ms_raw if isinstance(ms_raw, dict) else {},
                projected_member_ids=projected_ids,
                team_name_hint=team.team_name,
            )
            if not vr.get("saveable", True):
                hard_issues = [i for i in (vr.get("issues") or []) if i.get("severity") == "hard"]
                first = hard_issues[0] if hard_issues else (vr.get("issues") or [{}])[0]
                code = first.get("code", "TEAM_MIN_SHIFT_INVALID")
                msg = first.get("message", "팀 최소 인원 설정이 저장 불가능한 값입니다.")
                raise ValueError(f"[{code}] {msg}")

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
        # 시점 모델 정합: 삭제된 팀을 가리키는 nurse_team_period 도 제거.
        #   안 하면 캐시(team_id)는 None인데 period 는 옛 팀을 가리켜 resolve 가 '없는 팀'을
        #   반환(고아). 캐시 None 처리와 동일 의미로 period 행을 삭제한다.
        db.query(NurseTeamPeriod).filter(
            NurseTeamPeriod.group_id == group_id,
            NurseTeamPeriod.team_id.in_(delete_team_ids),
        ).delete(synchronize_session=False)
        db.query(Team).filter(Team.office_id == office_id, Team.group_id == group_id, Team.team_id.in_(delete_team_ids)).delete(synchronize_session=False)

    db.commit()
    return list_teams_with_members(db, office_id, group_id)

