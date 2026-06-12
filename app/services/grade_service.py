from typing import Dict, Any, List, Optional

from sqlalchemy.orm import Session

from db.models import Group, RosterConfig, RosterGradeConfig, Shift
from schemas.grade_schema import (
    DefaultShiftItem,
    GradeConfigUpsert,
    GradeConfigResponse,
)


def _resolve_grade_names(
    grade_names_raw: Any,
    constraints: Optional[Dict[str, Dict[Any, int]]],
) -> Optional[Dict[str, Optional[str]]]:
    """모든 grade slot 키를 응답에 포함하고, 명시값 없는 키는 None 으로 채운다.

    Slot 키 수집:
      - constraints_json 각 shift 의 grade key 합집합
      - grade_names_json 자체 키
      위 두 집합의 합집합을 응답 키 목록으로 사용.
    값:
      - grade_names_json 의 값이 비어있지 않은 문자열이면 그대로 사용
      - 그 외는 None
    Slot 자체가 하나도 없으면 None 반환.
    """
    raw_dict: Dict[str, Any] = {}
    if isinstance(grade_names_raw, dict):
        raw_dict = {str(k): v for k, v in grade_names_raw.items()}

    grade_keys: set[str] = set()
    if isinstance(constraints, dict):
        for grades_map in constraints.values():
            if not isinstance(grades_map, dict):
                continue
            for g_key in grades_map.keys():
                try:
                    grade_keys.add(str(int(g_key)))
                except (TypeError, ValueError):
                    continue
    for k in raw_dict.keys():
        try:
            grade_keys.add(str(int(k)))
        except (TypeError, ValueError):
            grade_keys.add(str(k))

    if not grade_keys:
        return None

    def _sort_key(x: str) -> tuple[int, int, str]:
        try:
            return (0, int(x), "")
        except (TypeError, ValueError):
            return (1, 0, x)

    result: Dict[str, Optional[str]] = {}
    for key in sorted(grade_keys, key=_sort_key):
        v = raw_dict.get(key)
        if isinstance(v, str) and v.strip():
            result[key] = v
        else:
            result[key] = None
    return result


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
            allow_soft_fallback=False,
            constraints={},
            constraints_max={},
            grade_names=None,
            use_mid=_use_mid,
            default_shifts=[],
            updated_by=None,
        )

    # constraints = config.constraints_json or {}
    return GradeConfigResponse(
        config_id=config.config_id,
        office_id=config.office_id,
        group_id=config.group_id,
        null_grade_policy=config.null_grade_policy or "LOWEST",
        use_dynamic_scaling=bool(config.use_dynamic_scaling),
        allow_soft_fallback=bool(getattr(config, "allow_soft_fallback", False)),
        constraints=config.constraints_json or {},
        constraints_max=config.constraints_max_json or {},
        grade_names=_resolve_grade_names(
            config.grade_names_json, config.constraints_json or {}
        ),
        use_mid=_use_mid,
        default_shifts=_normalize_default_shifts(config.default_shifts_json, _use_mid),
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

    provided_fields = set(payload.model_fields_set)

    policy = str(payload.null_grade_policy or "LOWEST").upper()
    allowed_policies = {"LOWEST", "AVERAGE", "RANDOM", "EXCLUDE"}
    if policy not in allowed_policies:
        raise ValueError(
            f"허용되지 않은 null_grade_policy: {payload.null_grade_policy} "
            f"(허용: {sorted(allowed_policies)})"
        )

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

    if "null_grade_policy" in provided_fields or not config.null_grade_policy:
        config.null_grade_policy = policy

    if "use_dynamic_scaling" in provided_fields or config.use_dynamic_scaling is None:
        config.use_dynamic_scaling = 1 if payload.use_dynamic_scaling else 0

    if "allow_soft_fallback" in provided_fields or getattr(config, "allow_soft_fallback", None) is None:
        config.allow_soft_fallback = 1 if payload.allow_soft_fallback else 0

    if "constraints" in provided_fields:
        _validate_constraints(payload.constraints, use_mid=use_mid)
        cleaned = dict(payload.constraints or {})
        if not use_mid:
            cleaned.pop("M", None)
        # [GRADE_MIN_ONLY_G1] grade 수와 무관하게 min 은 grade "1" 만 1, 나머지 등급은 0.
        #   (빈/미등록 상위 등급에 min 이 남아 precheck GRADE_MIN_AVAILABLE_SHORTAGE 로
        #    infeasible 되는 것을 원천 차단. 등급 cascade/soft 는 엔진단에서 처리.)
        for _shift, _per_grade in list(cleaned.items()):
            if not isinstance(_per_grade, dict):
                continue
            cleaned[_shift] = {
                str(_g): (1 if str(_g) == "1" else 0)
                for _g in _per_grade.keys()
            }
        config.constraints_json = cleaned

    if "constraints_max" in provided_fields:
        _validate_constraints(payload.constraints_max, use_mid=use_mid, allow_negative_as_unset=True)
        cleaned_max = dict(payload.constraints_max or {})
        if not use_mid:
            cleaned_max.pop("M", None)
        config.constraints_max_json = cleaned_max

    if "grade_names" in provided_fields:
        config.grade_names_json = payload.grade_names

    if "default_shifts" in provided_fields and payload.default_shifts is not None:
        # use_mid=False 면 M 항목 제거 후 저장
        config.default_shifts_json = [
            item.model_dump() for item in payload.default_shifts
            if use_mid or str(item.code).upper() != "M"
        ]
    config.updated_by = user_id

    db.commit()
    db.refresh(config)

    # constraints_out = config.constraints_json or {}
    return GradeConfigResponse(
        config_id=config.config_id,
        office_id=config.office_id,
        group_id=config.group_id,
        null_grade_policy=config.null_grade_policy,
        use_dynamic_scaling=bool(config.use_dynamic_scaling),
        allow_soft_fallback=bool(getattr(config, "allow_soft_fallback", False)),
        constraints=config.constraints_json or {},
        constraints_max=config.constraints_max_json or {},
        grade_names=_resolve_grade_names(
            config.grade_names_json, config.constraints_json or {}
        ),
        use_mid=use_mid,
        default_shifts=_normalize_default_shifts(config.default_shifts_json, use_mid),
        created_at=config.created_at,
        updated_at=config.updated_at,
        updated_by=config.updated_by,
    )


def _validate_constraints(
    constraints: Dict[str, Dict[int, int]],
    use_mid: bool = False,
    allow_negative_as_unset: bool = False,
) -> None:
    """Shift/Grade 키와 값의 유효성을 검증합니다.

    allow_negative_as_unset=True(max용): 음수는 '제한 없음' 시그널로 허용.
    """
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
            if not allow_negative_as_unset and int(count) < 0:
                raise ValueError(f"필요 인원은 0 이상이어야 합니다: {shift_code}-{g_key}")


def _validate_default_shifts(
    db: Session,
    group_id: str,
    items: List[DefaultShiftItem],
    use_mid: bool = False,
) -> None:
    """default_shifts 항목의 코드/매핑 ID 유효성을 검증합니다."""
    if not items:
        return

    allowed_codes = {"D", "E", "N"}
    if use_mid:
        allowed_codes.add("M")

    seen_codes: set[str] = set()
    candidate_ids: List[int] = []
    for item in items:
        code = str(item.code).upper()
        if code not in allowed_codes:
            raise ValueError(
                f"허용되지 않은 default_shifts 코드(허용: {sorted(allowed_codes)}): {item.code}"
            )
        if code in seen_codes:
            raise ValueError(f"중복된 default_shifts 코드: {item.code}")
        seen_codes.add(code)
        if item.shift_table_id is not None:
            candidate_ids.append(int(item.shift_table_id))

    if not candidate_ids:
        return

    rows = (
        db.query(Shift.id)
        .filter(Shift.group_id == group_id, Shift.id.in_(candidate_ids))
        .all()
    )
    found = {int(r[0]) for r in rows}
    missing = [i for i in candidate_ids if i not in found]
    if missing:
        raise ValueError(f"shifts 테이블에 존재하지 않는 shift_table_id: {missing}")


def _normalize_default_shifts(raw: Any, use_mid: bool) -> List[DefaultShiftItem]:
    """저장된 default_shifts_json 을 응답용 객체 리스트로 정규화합니다."""
    if not raw:
        return []
    if isinstance(raw, str):
        import json
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return []
    if not isinstance(raw, list):
        return []

    allowed = {"D", "E", "N"} | ({"M"} if use_mid else set())
    result: List[DefaultShiftItem] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        code = str(entry.get("code", "")).upper()
        if code not in allowed or code in seen:
            continue
        seen.add(code)
        sid_raw = entry.get("shift_table_id")
        sid = int(sid_raw) if isinstance(sid_raw, (int, str)) and str(sid_raw).strip() else None
        result.append(DefaultShiftItem(code=code, shift_table_id=sid))
    return result
