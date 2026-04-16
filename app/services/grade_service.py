from typing import Dict, Any

from sqlalchemy.orm import Session

from db.models import Group, RosterConfig, RosterGradeConfig
from schemas.grade_schema import GradeConfigUpsert, GradeConfigResponse


def get_grade_config_service(db: Session, group_id: str) -> GradeConfigResponse:
    """특정 그룹의 Grade 설정을 조회합니다."""
    group = db.query(Group).filter(Group.group_id == group_id).first()
    if not group:
        raise ValueError("존재하지 않는 그룹입니다.")

    config = (
        db.query(RosterGradeConfig)
        .filter(RosterGradeConfig.group_id == group_id)
        .first()
    )

    # roster_config 에서 use_mid 조회
    roster_cfg = (
        db.query(RosterConfig)
        .filter(RosterConfig.group_id == group_id)
        .order_by(RosterConfig.config_id.desc())
        .first()
    )
    _use_mid = bool(getattr(roster_cfg, "use_mid", False)) if roster_cfg else False

    if not config:
        return GradeConfigResponse(
            config_id=None,
            office_id=group.office_id,
            group_id=group.group_id,
            null_grade_policy="LOWEST",
            use_dynamic_scaling=True,
            constraints={},
            grade_names=None,
            use_mid=_use_mid,
            updated_by=None,
        )

    return GradeConfigResponse(
        config_id=config.config_id,
        office_id=config.office_id,
        group_id=config.group_id,
        null_grade_policy=config.null_grade_policy or "LOWEST",
        use_dynamic_scaling=bool(config.use_dynamic_scaling),
        constraints=config.constraints_json or {},
        grade_names=config.grade_names_json,
        use_mid=_use_mid,
        created_at=config.created_at,
        updated_at=config.updated_at,
        updated_by=config.updated_by,
    )


def upsert_grade_config_service(
    db: Session,
    office_id: str,
    group_id: str,
    payload: GradeConfigUpsert,
    user_id: str,
) -> GradeConfigResponse:
    """Grade 설정을 생성 또는 갱신합니다."""
    group = db.query(Group).filter(Group.group_id == group_id).first()
    if not group:
        raise ValueError("존재하지 않는 그룹입니다.")
    if group.office_id != office_id:
        raise ValueError("그룹과 오피스가 일치하지 않습니다.")

    # roster_config 에서 use_mid 여부 조회
    roster_cfg = (
        db.query(RosterConfig)
        .filter(RosterConfig.group_id == group_id)
        .order_by(RosterConfig.config_id.desc())
        .first()
    )
    use_mid = bool(getattr(roster_cfg, "use_mid", False)) if roster_cfg else False

    _validate_constraints(payload.constraints, use_mid=use_mid)

    # use_mid=False 면 M 키 제거
    cleaned = dict(payload.constraints or {})
    if not use_mid:
        cleaned.pop("M", None)

    config = (
        db.query(RosterGradeConfig)
        .filter(RosterGradeConfig.group_id == group_id)
        .first()
    )

    if not config:
        config = RosterGradeConfig(
            office_id=office_id,
            group_id=group_id,
        )
        db.add(config)

    config.null_grade_policy = payload.null_grade_policy or "LOWEST"
    config.use_dynamic_scaling = 1 if payload.use_dynamic_scaling else 0
    config.constraints_json = cleaned
    config.grade_names_json = payload.grade_names
    config.updated_by = user_id

    db.commit()
    db.refresh(config)

    return GradeConfigResponse(
        config_id=config.config_id,
        office_id=config.office_id,
        group_id=config.group_id,
        null_grade_policy=config.null_grade_policy,
        use_dynamic_scaling=bool(config.use_dynamic_scaling),
        constraints=config.constraints_json or {},
        grade_names=config.grade_names_json,
        use_mid=use_mid,
        created_at=config.created_at,
        updated_at=config.updated_at,
        updated_by=config.updated_by,
    )


def _validate_constraints(
    constraints: Dict[str, Dict[int, int]],
    use_mid: bool = False,
) -> None:
    """Shift/Grade 키와 값의 유효성을 검증합니다."""
    if not constraints:
        return

    allowed = {"D", "E", "N"}
    if use_mid:
        allowed.add("M")
    invalid_shifts = [s for s in constraints.keys() if str(s).upper() not in allowed]
    if invalid_shifts:
        raise ValueError(f"허용되지 않은 Shift 코드(허용: {sorted(allowed)}): {invalid_shifts}")

    for shift_code, grades_map in constraints.items():
        if not isinstance(grades_map, dict):
            raise ValueError(f"Shift '{shift_code}' 값은 객체 형태여야 합니다.")
        for g_key, count in grades_map.items():
            try:
                int(g_key)
            except Exception:
                raise ValueError(f"Grade 키는 정수여야 합니다: {g_key}")
            if int(count) < 0:
                raise ValueError(f"필요 인원은 0 이상이어야 합니다: {shift_code}-{g_key}")

