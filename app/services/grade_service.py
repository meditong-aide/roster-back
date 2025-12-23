from typing import Dict, Any

from sqlalchemy.orm import Session

from db.models import Group, RosterGradeConfig
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

    if not config:
        # 기본값 반환
        return GradeConfigResponse(
            config_id=None,
            office_id=group.office_id,
            group_id=group.group_id,
            null_grade_policy="LOWEST",
            use_dynamic_scaling=True,
            constraints={},
            updated_by=None,
        )

    return GradeConfigResponse(
        config_id=config.config_id,
        office_id=config.office_id,
        group_id=config.group_id,
        null_grade_policy=config.null_grade_policy or "LOWEST",
        use_dynamic_scaling=bool(config.use_dynamic_scaling),
        constraints=config.constraints_json or {},
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

    # Grade 제약은 메인 코드(D/E/N)만 지원한다(사용자 합의).
    _validate_constraints(payload.constraints)

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
    config.constraints_json = payload.constraints or {}
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
        created_at=config.created_at,
        updated_at=config.updated_at,
        updated_by=config.updated_by,
    )


def _validate_constraints(constraints: Dict[str, Dict[int, int]]) -> None:
    """Shift/Grade 키와 값의 유효성을 검증합니다.

    - Shift 키는 'D','E','N'만 허용합니다.
    - Grade 키는 1~3만 허용합니다. -> 바뀌어야 함. 가변적으로.
    """
    if not constraints:
        return

    # Shift 코드 검증 (D/E/N만)
    allowed = {"D", "E", "N"}
    # invalid_shifts = [s for s in constraints.keys() if str(s).upper() not in allowed]
    # if invalid_shifts:
    #     raise ValueError(f"허용되지 않은 Shift 코드(허용: D/E/N): {invalid_shifts}")

    for shift_code, grades_map in constraints.items():
        if not isinstance(grades_map, dict):
            raise ValueError(f"Shift '{shift_code}' 값은 객체 형태여야 합니다.")
        for g_key, count in grades_map.items():
            try:
                g_int = int(g_key)
            except Exception:
                raise ValueError(f"Grade 키는 정수여야 합니다: {g_key}")
            # if g_int not in (1, 2, 3):
            #     raise ValueError(f"Grade 키는 1,2,3만 허용: {g_key}")
            if int(count) < 0:
                raise ValueError(f"필요 인원은 0 이상이어야 합니다: {shift_code}-{g_key}")

