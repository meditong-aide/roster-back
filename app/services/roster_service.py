"""
근무표 관련 서비스 로직 모듈.

- DB 쿼리, 데이터 가공, 엔진 호출 등 라우터에서 분리합니다.
- 모든 함수는 한글 docstring, 한글 print/logging, PEP8 스타일을 지향합니다.
"""

from datetime import date, datetime, timedelta
import calendar
import re
import uuid

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from db.models import (
    RosterConfig as RosterConfigModel,
    Schedule,
    ShiftPreference,
    Nurse,
    NurseAssignment,
    ScheduleEntry,
    Shift,
    Group,
    RosterConfig,
    Wanted,
    IssuedRoster,
    ShiftManage,
    IssuedRosterSnapshot,
    WeeklyOffSetting,
    RosterGradeConfig,
    WantedRequest,
    NurseShiftRequest,
)
from db.roster_config import NurseRosterConfig
from db.nurse_config import Nurse as NurseEngine
from routers.utils import get_days_in_month
from schemas.roster_schema import RosterConfigCreate, PublishRequest, RosterRequest
from services.roster_system import RosterSystem
from services.group_access import caller_is_head_nurse, resolve_home_group_id
from services.shift_service_mssql import _to_time_str
def _next_config_version(db: Session, office_id: str, group_id: str) -> int:
    """그룹(office+group)별 다음 프리셋 version (0부터). 동시 충돌은 호출측 재시도로 처리."""
    cur = (
        db.query(func.max(RosterConfigModel.version))
        .filter(
            RosterConfigModel.office_id == office_id,
            RosterConfigModel.group_id == group_id,
        )
        .scalar()
    )
    return 0 if cur is None else int(cur) + 1


def apply_config_side_effects(db, *, office_id, group_id, weekly_off_group, use_mid):
    """선택된 설정을 라이브 테이블에 반영(=적용). 생성 시점에만 호출.

    솔버가 roster_config 가 아닌 라이브 테이블(WeeklyOffSetting/ShiftManage/Nurse/
    RosterGradeConfig)에서 weekly_off·use_mid 를 읽으므로, 생성 직전 선택 설정 값으로
    동기화한다. 저장(프리셋 북마크)에서는 호출하지 않는다(순수).
    """
    # weekly_off 동기화
    db.query(WeeklyOffSetting).filter(
        WeeklyOffSetting.office_id == office_id,
        WeeklyOffSetting.group_id == group_id,
    ).update({'activate': 1 if weekly_off_group else 0})
    if weekly_off_group is not None:
        db.query(Nurse).filter(
            Nurse.group_id == group_id
        ).update(
            {Nurse.weekly_off_enabled: 1 if weekly_off_group else 0},
            synchronize_session=False,
        )

    # use_mid (M근무) 동기화 — 생성/저장 공용 헬퍼로 위임
    _apply_use_mid_live(db, office_id=office_id, group_id=group_id, use_mid=use_mid)


def _apply_use_mid_live(db, *, office_id, group_id, use_mid):
    """use_mid(M근무) 값을 라이브 테이블(RosterGradeConfig/ShiftManage/Nurse)에 반영.

    솔버·daily-shift 가 라이브 테이블에서 M 근무를 읽으므로, use_mid 가 바뀌면
    이 값을 즉시 라이브에 동기화한다. 생성 시점(apply_config_side_effects)과
    저장 시점(save_roster_config_service) 양쪽에서 재사용한다.

    - True: grade constraints_json/default_shifts_json 에 'M' 을 추가(없으면).
    - False: ShiftManage M 슬롯(5) manpower=0, Nurse.allowed_shifts 에서 M 제거,
      grade constraints_json/default_shifts_json 에서 M 제거.
    NOTE: True 로 전환해도 ShiftManage M 슬롯 manpower 는 복원하지 않는다
      (인력값은 근무설정(daily-shift) 인력 편집기가 관리 — 기존 생성 동작과 동일).
    """
    if use_mid:
        grade_cfg = db.query(RosterGradeConfig).filter(
            RosterGradeConfig.office_id == office_id,
            RosterGradeConfig.group_id == group_id,
        ).first()
        if grade_cfg and isinstance(grade_cfg.constraints_json, dict):
            cj = dict(grade_cfg.constraints_json)
            if 'M' not in cj and cj:
                sample = cj.get('D') or cj.get('E') or cj.get('N') or {}
                cj['M'] = {g: 0 for g in sample}
                grade_cfg.constraints_json = cj
        if grade_cfg:
            ds = list(grade_cfg.default_shifts_json or [])
            if not any(
                isinstance(it, dict) and str(it.get('code', '')).upper() == 'M'
                for it in ds
            ):
                ds.append({'code': 'M', 'shift_table_id': None})
                grade_cfg.default_shifts_json = ds
    else:
        db.query(ShiftManage).filter(
            ShiftManage.office_id == office_id,
            ShiftManage.group_id == group_id,
            ShiftManage.nurse_class == 'RN',
            ShiftManage.shift_slot == 5,
        ).update({ShiftManage.manpower: 0}, synchronize_session=False)

        nurses = db.query(Nurse).filter(Nurse.group_id == group_id).all()
        for nurse_row in nurses:
            raw_types = getattr(nurse_row, 'allowed_shifts', None)
            if isinstance(raw_types, list) and raw_types:
                nurse_row.allowed_shifts = [
                    t for t in raw_types if str(t).strip().upper() != 'M'
                ]

        grade_cfg = db.query(RosterGradeConfig).filter(
            RosterGradeConfig.office_id == office_id,
            RosterGradeConfig.group_id == group_id,
        ).first()
        if grade_cfg and isinstance(grade_cfg.constraints_json, dict):
            cleaned = dict(grade_cfg.constraints_json)
            if 'M' in cleaned:
                cleaned.pop('M', None)
                grade_cfg.constraints_json = cleaned
        if grade_cfg:
            ds = list(grade_cfg.default_shifts_json or [])
            filtered = [
                it for it in ds
                if not (isinstance(it, dict) and str(it.get('code', '')).upper() == 'M')
            ]
            if len(filtered) != len(ds):
                grade_cfg.default_shifts_json = filtered


def _group_canonical_use_mid(db, office_id, group_id) -> bool:
    """그룹의 정본 use_mid — 최신 config 기준(저장이 전체 전파하므로 모든 config 동일).

    생성 포크는 이 값을 준수하고 payload 의 use_mid 는 무시한다(그룹 단일 진실).
    """
    row = (
        db.query(RosterConfigModel.use_mid)
        .filter(
            RosterConfigModel.office_id == office_id,
            RosterConfigModel.group_id == group_id,
        )
        .order_by(RosterConfigModel.config_id.desc())
        .first()
    )
    return bool(row[0]) if row and row[0] is not None else False


def _propagate_use_mid_all_configs(db, office_id, group_id, use_mid) -> None:
    """그룹의 모든 config use_mid 를 정본값으로 통일(전파). 저장/생성 공용.

    use_mid 는 병동 단위 설정이므로 per-config 로 갈리면 안 된다. 어떤 config 를
    조회·포크·생성하더라도 동일 값이 되도록 그룹 전체를 한 번에 맞춘다.

    """
    db.query(RosterConfigModel).filter(
        RosterConfigModel.office_id == office_id,
        RosterConfigModel.group_id == group_id,
    ).update({RosterConfigModel.use_mid: bool(use_mid)}, synchronize_session=False)


# materialize 비교에서 제외: 메타 + ShiftManage 파생(day/eve/nig) + use_mid(그룹 정본)
_COMPARE_EXCLUDE = {
    'config_id', 'config_name', 'config_memo',
    'day_req', 'eve_req', 'nig_req', 'use_mid',
}


def _next_auto_config_name(db, office_id, group_id) -> str:
    """'새로운 설정n' 다음 이름 — 기존 최대 n+1 (없으면 1)."""
    rows = (
        db.query(RosterConfigModel.config_name)
        .filter(
            RosterConfigModel.office_id == office_id,
            RosterConfigModel.group_id == group_id,
            RosterConfigModel.config_name.like('새로운 설정%'),
        )
        .all()
    )
    max_n = 0
    for (name,) in rows:
        m = re.match(r'^새로운 설정(\d+)$', (name or '').strip())
        if m:
            max_n = max(max_n, int(m.group(1)))
    return f'새로운 설정{max_n + 1}'


def materialize_generation_config(
    db, payload: RosterConfigCreate, user, *, override_group_id=None
):
    """생성 시점: payload 를 config row 로 굳히고(변경 시 '새로운 설정n') 라이브 동기화.

    - payload 가 baseline(config_id) 과 동일 → 기존 row 재사용(새 row 없음).
    - 다르거나 baseline 없음 → '새로운 설정n' 신규 row(version=MAX+1).
    - 어느 경우든 결정된 설정으로 라이브 동기화(apply_config_side_effects)를 항상 수행.
    반환: 결정된 RosterConfig row (호출측은 row.config_id 를 솔버에 전달).
    """
    if override_group_id:
        grp = db.query(Group).filter(Group.group_id == override_group_id).first()
        if not grp:
            raise Exception("지정한 그룹을 찾을 수 없습니다.")
        office_id, group_id = grp.office_id, grp.group_id
    else:
        nurse = db.query(Nurse).filter(Nurse.nurse_id == user.nurse_id).first()
        office_id, group_id = nurse.office_id, user.group_id

    # use_mid 정본을 포크 생성 전에 읽어둔다(그룹 최신 config 기준). 생성은 이 값을 준수.
    canonical_use_mid = _group_canonical_use_mid(db, office_id, group_id)

    baseline = None
    if payload.config_id is not None:
        baseline = db.query(RosterConfigModel).filter(
            RosterConfigModel.config_id == payload.config_id,
            RosterConfigModel.office_id == office_id,
            RosterConfigModel.group_id == group_id,
        ).first()

    pdict = {
        k: v for k, v in payload.model_dump().items() if k not in _COMPARE_EXCLUDE
    }
    # 미전송(None) 보존 필드는 **baseline 값으로 정규화한 뒤** 비교한다.
    #   이 필드를 모르는 화면은 None 을 보내는데, 그걸 baseline 의 True/False 와 그대로
    #   견주면 "바뀌었다" 로 읽혀 매 생성마다 프리셋이 포크된다. docstring 이 명시한
    #   "동일하면 기존 row 재사용" 과 어긋나고, 포크된 row 는 save 경로에서 **직전 최신**
    #   config 값을 승계하므로 사용자가 고른 baseline 과 다른 값이 실릴 수 있다.
    #   저장 루프의 _PRESERVE_IF_NONE 가드와 같은 규약을 비교에도 적용하는 것이다.
    if baseline is not None:
        for _k in _PRESERVE_IF_NONE:
            if _k in pdict and pdict[_k] is None:
                pdict[_k] = getattr(baseline, _k, None)
    differs = True
    if baseline is not None:
        differs = any(getattr(baseline, k, None) != v for k, v in pdict.items())

    if baseline is not None and not differs:
        resolved = baseline
    else:
        auto_name = _next_auto_config_name(db, office_id, group_id)
        # ★ 포크에도 **정규화한 값**을 실어야 한다. None 인 채로 넘기면
        #   save_roster_config_service 의 _PRESERVE_IF_NONE 승계가 **직전 최신** config 에서
        #   값을 가져오므로, 사용자가 고른 baseline 이 오래된 프리셋일 때 엉뚱한 값이 실린다.
        #   baseline 이 없으면 pdict 도 정규화되지 않아 None 그대로다(승계할 기준이 없으니 정상).
        _preserved = {k: pdict[k] for k in _PRESERVE_IF_NONE if k in pdict}
        new_payload = payload.model_copy(
            update={'config_id': None, 'config_name': auto_name, **_preserved}
        )
        # ★ 승계 기준을 baseline 으로 못박는다. 안 넘기면 save 가 **직전 최신** 프리셋에서
        #   미전송 필드를 승계해, 오래된 baseline 을 고른 사용자가 최신의 값을 얻는다.
        res = save_roster_config_service(
            new_payload, user, db, override_group_id=group_id, inherit_from=baseline
        )
        resolved = db.query(RosterConfigModel).filter(
            RosterConfigModel.config_id == res['config_id']
        ).first()

    # use_mid 는 그룹 정본을 준수 — payload/포크의 값 무시, 전체 config 로 전파.
    resolved.use_mid = bool(canonical_use_mid)
    _propagate_use_mid_all_configs(db, office_id, group_id, canonical_use_mid)

    # 항상 라이브 동기화(적용) — use_mid 는 정본값으로
    apply_config_side_effects(
        db, office_id=office_id, group_id=group_id,
        weekly_off_group=resolved.weekly_off_group,
        use_mid=canonical_use_mid,
    )
    db.commit()
    db.refresh(resolved)
    return resolved


# 미전송(None) 시 값을 덮어쓰지 않고 보존할 설정 키.
#   저장 요청은 model_dump() 를 일괄 setattr 하므로, 이 필드를 모르는 화면이 저장하면
#   기존 설정이 꺼진다. 신규 프리셋에서는 직전 최신 config 값을 승계한다.
_PRESERVE_IF_NONE = (
    "health_leave_enabled", "health_leave_weekend",
    "sleep_off_enabled", "sleep_off_cycle",
    # 확정 원티드 O 직전일 N 금지. 켜 둔 병동이 이 필드를 모르는 화면에서 저장하면
    # 조용히 꺼지므로 보존 대상이다.
    "ban_night_before_fixed_wanted_off",
)


def save_roster_config_service(
    config_data: RosterConfigCreate,
    user,
    db: Session,
    override_group_id: str | None = None,
    sync_use_mid_live: bool = False,
    inherit_from=None,
):
    """근무표 설정 저장 (프리셋 upsert).

    - config_id 있으면 해당 프리셋 in-place 수정(version 유지), 없으면 신규 INSERT(version=MAX+1).
    - 관리자(ADM)는 `override_group_id`로 저장 대상 그룹을 지정.
    - sync_use_mid_live=True: 저장 즉시 use_mid 를 라이브 테이블에 동기화(사용자 대면 저장 경로).
      생성 시점(materialize)의 apply_config_side_effects 는 자체 동기화하므로 기본 False.
    NOTE: version 유일성 인덱스는 개발 마무리 후 추가 예정 — 추가 시 IntegrityError 재시도 가드 복원.
    """
    try:
        # 1) 저장 대상 그룹/오피스 결정
        target_group_id: str
        target_office_id: str

        if override_group_id:
            group_row = db.query(Group).filter(Group.group_id == override_group_id).first()
            if not group_row:
                raise Exception("지정한 그룹을 찾을 수 없습니다.")
            target_group_id = group_row.group_id
            target_office_id = group_row.office_id
        else:
            
            nurse = db.query(Nurse).filter(Nurse.nurse_id == user.nurse_id).first()
            target_group_id = user.group_id
            target_office_id = nurse.office_id

        # 2) ShiftManage 기준으로 기본 일/저/야 요구 인원 계산
        shift_manages = db.query(ShiftManage).filter(
            ShiftManage.office_id == target_office_id,
            ShiftManage.group_id == target_group_id,
            ShiftManage.nurse_class == 'RN',
        ).order_by(ShiftManage.id.asc()).all()  # 중복행 결정성: 슬롯별 최대 id(최근) 행 채택
        day_req = eve_req = nig_req = 0
        if shift_manages:
            for sm in shift_manages:
                if sm.shift_slot == 1:
                    day_req = sm.manpower or 0
                elif sm.shift_slot == 2:
                    eve_req = sm.manpower or 0
                elif sm.shift_slot == 3:
                    nig_req = sm.manpower or 0
        else:
            day_req = eve_req = nig_req = 3

        # 3) 설정 저장 (프리셋 upsert)
        config_dict = config_data.model_dump()
        # 메타 필드는 컬럼 직접매핑에서 분리 — version 은 서버가 할당
        req_config_id = config_dict.pop('config_id', None)
        config_name = config_dict.pop('config_name', None)
        config_memo = config_dict.pop('config_memo', None)
        config_dict.update({
            'day_req': day_req,
            'eve_req': eve_req,
            'nig_req': nig_req
        })

        if req_config_id is not None:
            # 기존 프리셋 수정 (in-place upsert) — config_id·version 유지
            db_config = db.query(RosterConfigModel).filter(
                RosterConfigModel.config_id == req_config_id,
                RosterConfigModel.office_id == target_office_id,
                RosterConfigModel.group_id == target_group_id,
            ).first()
            if db_config is None:
                raise Exception("수정할 설정(config_id)을 찾을 수 없습니다.")
            # ★ 생성 결과 기록(last_generate_status)을 리셋하지 않는다 — 그게 방침이다.
            #   한 번 실패로 기록된 설정은 **편집해 저장해도 되살리지 않는다** —
            #   저장은 검증이 아니기 때문이다(재생성해서 성공하면 그때 복귀한다).
            #   새로 쓰려면 config_id 없이 저장해 신규 행을 만든다
            #   (신규 행은 NULL 에서 시작하므로 목록에 보인다).
            #   ※ 여기에 db_config.last_generate_status = None 을 넣지 말 것.
            for _key, _val in config_dict.items():
                # ★ 미전송(None) 시 기존 값 유지 — 이 필드를 모르는 기존 저장 화면이
                #   저장할 때마다 설정을 꺼버리는 사고를 막는다(스키마 기본값도 None).
                if _val is None and _key in _PRESERVE_IF_NONE:
                    continue
                setattr(db_config, _key, _val)
            if config_name is not None:
                db_config.config_name = config_name
            if config_memo is not None:
                db_config.config_memo = config_memo
            # ★version 이 NULL(레거시/default-seed 프리셋)이면 저장 시 채번 —
            #   업데이트 경로엔 원래 채번이 없어 NULL 프리셋이 저장해도 계속 NULL(=/config/versions 목록 누락)이었음.
            if db_config.version is None:
                db_config.version = _next_config_version(db, target_office_id, target_group_id)
        else:
            # 신규 프리셋 — 그룹(office+group)별 version = MAX+1 (0부터)
            # ★ 미전송 필드는 기준 config 값을 승계 — 생성은 created_at DESC 로 config 를
            #   고르므로, 승계하지 않으면 새 프리셋을 저장하는 순간 기능이 꺼진 채로 생성된다.
            # ★★ 기준은 `inherit_from` 이 있으면 그것, 없으면 직전 최신이다.
            #   생성 시 포크(materialize_generation_config)는 **사용자가 고른 baseline** 을 넘긴다.
            #   안 넘기면 오래된 프리셋을 골라 다른 설정만 바꿔도 최신 프리셋의 값이 딸려 들어와,
            #   baseline 이 NULL(꺼짐)인 설정이 조용히 켜진 채로 생성된다.
            if any(config_dict.get(_k) is None for _k in _PRESERVE_IF_NONE):
                _prev = inherit_from or (
                    db.query(RosterConfigModel)
                    .filter(
                        RosterConfigModel.office_id == target_office_id,
                        RosterConfigModel.group_id == target_group_id,
                    )
                    .order_by(RosterConfigModel.created_at.desc())
                    .first()
                )
                for _k in _PRESERVE_IF_NONE:
                    if config_dict.get(_k) is None:
                        config_dict[_k] = getattr(_prev, _k, None) if _prev else None
            db_config = RosterConfigModel(
                **config_dict,
                office_id=target_office_id,
                group_id=target_group_id,
                version=_next_config_version(db, target_office_id, target_group_id),
                config_name=config_name,
                config_memo=config_memo,
            )
            db.add(db_config)
        # NOTE: weekly_off 등 나머지 라이브 동기화는 생성 시점(apply_config_side_effects)에
        #   수행. 단, use_mid 는 daily-shift/솔버가 라이브에서 읽어 저장-생성 사이 stale 이
        #   되므로, 사용자 대면 저장(sync_use_mid_live=True)에서는 즉시 동기화한다.
        if sync_use_mid_live:
            # 저장 = use_mid 의 유일한 변경 지점. 저장값을 그룹 정본으로 삼아
            #   전체 config 전파 + 라이브 동기화 → 이후 조회·생성이 모두 이 값을 따른다.
            _um = bool(getattr(db_config, 'use_mid', False))
            _propagate_use_mid_all_configs(db, target_office_id, target_group_id, _um)
            _apply_use_mid_live(
                db,
                office_id=target_office_id,
                group_id=target_group_id,
                use_mid=_um,
            )
        db.commit()
        db.refresh(db_config)
        return {
            "message": "Configuration saved successfully",
            "config_id": db_config.config_id,
            "version": db_config.version,
            "config_name": db_config.config_name,
            "config_memo": db_config.config_memo,
        }
    except Exception as e:
        print(f'설정 저장 오류: {str(e)}')
        db.rollback()
        raise


def unsave_roster_config_service(
    config_id: int,
    current_user,
    db: Session,
    override_group_id: str | None = None,
):
    """저장 설정(프리셋) 미노출 — version 을 NULL 로 되돌려 일반(ad-hoc) 설정으로 변경.

    row/config_id/설정값은 그대로 유지(FK·근무표 이력·재사용 영향 없음). /config/versions
    프리셋 목록에서만 빠진다. 다시 저장하면 _next_config_version 로 재채번되어 목록에 복귀.
    """
    # 권한은 엔드포인트(DELETE /config/{id})에서 검증(HN 본인그룹 / ADM group_id) — save 서비스와 동일 패턴(서비스 무검증).
    if override_group_id:
        group_row = db.query(Group).filter(Group.group_id == override_group_id).first()
        if not group_row:
            raise HTTPException(status_code=404, detail="지정한 그룹을 찾을 수 없습니다.")
        target_group_id, target_office_id = group_row.group_id, group_row.office_id
    else:
        nurse = db.query(Nurse).filter(Nurse.nurse_id == current_user.nurse_id).first()
        target_group_id, target_office_id = current_user.group_id, nurse.office_id
    cfg = db.query(RosterConfigModel).filter(
        RosterConfigModel.config_id == config_id,
        RosterConfigModel.office_id == target_office_id,
        RosterConfigModel.group_id == target_group_id,
    ).first()
    if cfg is None:
        raise HTTPException(status_code=404, detail="해당 설정(config_id)을 찾을 수 없습니다.")
    cfg.version = None
    db.commit()
    return {"message": "저장 설정 해제 — 일반 설정으로 변경", "config_id": config_id}


def get_latest_schedule_service(current_user, db: Session, override_group_id: str | None = None):
    """
    최신 스케줄 정보 조회 서비스 함수.

    관리자(ADM)는 `override_group_id`로 대상 그룹을 지정할 수 있습니다.
    """
    if not current_user:
        raise Exception("Not authenticated")
    if not (caller_is_head_nurse(db, current_user) or getattr(current_user, 'is_master_admin', False)):
        raise Exception("Permission denied")

    target_group_id = override_group_id or resolve_home_group_id(db, current_user)
    if not target_group_id:
        raise Exception("대상 그룹이 없습니다.")

    latest_schedule = db.query(Schedule).filter(
        Schedule.group_id == target_group_id,
        Schedule.dropped == False
    ).order_by(
        Schedule.year.desc(),
        Schedule.month.desc(),
        Schedule.version.desc()
    ).first()
    if not latest_schedule:
        return None
    return {
        "year": latest_schedule.year,
        "month": latest_schedule.month,
        "version": latest_schedule.version,
        "status": latest_schedule.status,
        "schedule_id": latest_schedule.schedule_id
    }

def get_issued_schedules_service(current_user, db: Session, target_group_id: str | None = None):
    """
    발행된(issued) 모든 스케줄 정보 조회 서비스 함수.

    관리자(ADM)는 `target_group_id`로 대상 그룹을 지정할 수 있습니다.
    """
    if not current_user:
        raise Exception("Not authenticated")
    # if not (getattr(current_user, 'is_head_nurse', False) or getattr(current_user, 'is_master_admin', False)):
    #     raise Exception("Permission denied")

 
    try:
        schedules_query = db.query(Schedule.schedule_id, Schedule.year, Schedule.month).filter(
            Schedule.group_id == target_group_id,
            Schedule.status == 'issued',
            Schedule.dropped == False
        ).distinct().order_by(Schedule.year.desc(), Schedule.month.desc()).all()
        schedules = [{"year": r.year, "month": r.month, "schedule_id": r.schedule_id} for r in schedules_query]
    except Exception as e:
        print('[get_issued_schedules_service] error', e)
        print('[get_issued_schedules_service] target_group_id', target_group_id)
        raise HTTPException(status_code=500, detail=f"Failed to get issued schedules: {str(e)}")
    return schedules

def get_schedule_status_service(year: int, month: int, current_user, db: Session, override_group_id: str | None = None):
    """
    특정 월의 스케줄 상태 조회 서비스 함수.

    관리자(ADM)는 `override_group_id`로 대상 그룹을 지정할 수 있습니다.
    """
    if not current_user:
        raise Exception("Not authenticated")

    # HN/ADM 그룹 요약
    if caller_is_head_nurse(db, current_user) or getattr(current_user, 'is_master_admin', False):
        target_group_id = override_group_id or resolve_home_group_id(db, current_user)
        if not target_group_id:
            raise Exception("대상 그룹이 없습니다.")
        schedules = db.query(Schedule).filter(
            Schedule.group_id == target_group_id,
            Schedule.year == year,
            Schedule.month == month,
            Schedule.dropped == False
        ).all()
        has_schedules = len(schedules) > 0
        latest_status = schedules[0].status if schedules else None
        return {
            "has_schedules": has_schedules,
            "latest_status": latest_status,
            "schedule_count": len(schedules)
        }

    # 일반 간호사 개인 선호도/상태
    schedule = db.query(Schedule).filter(
        Schedule.group_id == current_user.group_id,
        Schedule.year == year,
        Schedule.month == month,
        Schedule.dropped == False
    ).order_by(Schedule.version.desc()).first()
    submitted_preference = db.query(ShiftPreference).filter(
        ShiftPreference.nurse_id == current_user.nurse_id,
        ShiftPreference.year == year,
        ShiftPreference.month == month,
        ShiftPreference.is_submitted == True
    ).order_by(ShiftPreference.submitted_at.desc()).first()
    if submitted_preference:
        return {
            "schedule_status": schedule.status if schedule else None,
            "preference_is_submitted": True,
            "preference_data": submitted_preference.data,
            "has_schedules": schedule is not None,
            "created_at": submitted_preference.created_at,
            "submitted_at": submitted_preference.submitted_at
        }
    draft_preference = db.query(ShiftPreference).filter(
        ShiftPreference.nurse_id == current_user.nurse_id,
        ShiftPreference.year == year,
        ShiftPreference.month == month,
        ShiftPreference.is_submitted == False
    ).order_by(ShiftPreference.created_at.desc()).first()
    if draft_preference:
        return {
            "schedule_status": schedule.status if schedule else None,
            "preference_is_submitted": False,
            "preference_data": draft_preference.data,
            "has_schedules": schedule is not None,
            "created_at": draft_preference.created_at,
            "submitted_at": None
        }
    return {
        "schedule_status": schedule.status if schedule else None,
        "preference_is_submitted": False,
        "preference_data": None,
        "has_schedules": schedule is not None,
        "created_at": None,
        "submitted_at": None
    }


def _build_inbound_prev_tail_nurses(
    db: Session,
    ref_schedule_id,
    group_nurse_ids: set,
    tail_day_list: list,
    load_target_prev_tail,
) -> list:
    """현재월 schedule 의 인바운드(타 그룹 소속) 간호사들의 home 그룹 전월 tail 구성.

    아웃바운드(_load_target_prev_tail 로 파견 간 병동 조회)의 거울상.
    인바운드는 본인 home 그룹(Nurse.group_id)의 전월 발행 근무표 tail 을 채워
    월 경계 연속성(연속근무·ND/NE·나이트 회복 OFF 등)을 볼 수 있게 한다.
    """
    if not ref_schedule_id:
        return []
    ref_entry_ids = {
        row.nurse_id
        for row in db.query(ScheduleEntry.nurse_id)
        .filter(ScheduleEntry.schedule_id == ref_schedule_id)
        .distinct()
        .all()
    }
    inbound_ids = [nid for nid in ref_entry_ids if nid not in group_nurse_ids]
    if not inbound_ids:
        return []
    inbound_nurses = (
        db.query(Nurse.nurse_id, Nurse.name, Nurse.group_id)
        .filter(Nurse.nurse_id.in_(inbound_ids))
        .all()
    )
    result = []
    for inb in inbound_nurses:
        home_map = {}
        if inb.group_id:
            home_payload = load_target_prev_tail(inb.group_id)
            home_map = (home_payload.get("entries_by_nurse") or {}).get(
                inb.nurse_id, {}
            )
        result.append({
            "nurse_id": inb.nurse_id,
            "name": inb.name,
            "shifts": {str(d): home_map.get(d) for d in tail_day_list},
            "assignments": [],
            "is_inbound": True,
            "home_group_id": inb.group_id,
        })
    return result


def get_prev_month_tail_service(
    year: int,
    month: int,
    schedule_id: str | None,
    tail_days: int,
    group_id: str | None,
    current_user,
    db: Session,
):
    if caller_is_head_nurse(db, current_user) and current_user.group_id:
        target_group_id = current_user.group_id
        # HN multi-group 통합보기: managed group 이면 param 허용, 아니면 403
        if group_id and str(group_id) != str(target_group_id):
            from services.group_access import resolve_managed_group_ids
            _managed = {str(g) for g in resolve_managed_group_ids(db, current_user)}
            if str(group_id) in _managed:
                target_group_id = group_id
            else:
                raise HTTPException(
                    status_code=403,
                    detail="해당 그룹 근무표 조회 권한이 없습니다.",
                )
    else:
        if not group_id:
            raise HTTPException(status_code=400, detail="group_id is required for admin")
        g = db.query(Group).filter(Group.group_id == group_id).first()
        if not g:
            raise HTTPException(status_code=404, detail="Group not found")
        if (
            getattr(current_user, "office_id", None)
            and current_user.office_id != g.office_id
        ):
            raise HTTPException(
                status_code=403, detail="Group does not belong to your office"
            )
        target_group_id = g.group_id

    if month == 1:
        prev_year, prev_month = year - 1, 12
    else:
        prev_year, prev_month = year, month - 1

    ref_schedule_id = schedule_id
    if not ref_schedule_id:
        cur_issued = (
            db.query(Schedule.schedule_id)
            .filter(
                Schedule.group_id == target_group_id,
                Schedule.year == year,
                Schedule.month == month,
                Schedule.status == "issued",
                Schedule.dropped == False,
            )
            .scalar()
        )
        ref_schedule_id = cur_issued

    nurses = (
        db.query(Nurse.nurse_id, Nurse.name, Nurse.sequence)
        .filter(
            Nurse.group_id == target_group_id,
            Nurse.active == 1,
        )
        .order_by(Nurse.sequence.asc(), Nurse.nurse_id.asc())
        .all()
    )

    prev_schedule = (
        db.query(Schedule)
        .filter(
            Schedule.group_id == target_group_id,
            Schedule.year == prev_year,
            Schedule.month == prev_month,
            Schedule.status == "issued",
            Schedule.dropped == False,
        )
        .first()
    )

    if not prev_schedule:
        prev_schedule = (
            db.query(Schedule)
            .filter(
                Schedule.group_id == target_group_id,
                Schedule.year == prev_year,
                Schedule.month == prev_month,
                Schedule.dropped == False,
            )
            .order_by(Schedule.created_at.desc())
            .first()
        )

    if not prev_schedule:
        return {"prev_year": prev_year, "prev_month": prev_month, "data": None}

    days_in_prev_month = get_days_in_month(prev_year, prev_month)
    tail_day_list = list(
        range(max(1, days_in_prev_month - tail_days + 1), days_in_prev_month + 1)
    )

    start_date = date(prev_year, prev_month, tail_day_list[0])
    end_date = date(prev_year, prev_month, tail_day_list[-1])

    entries = (
        db.query(ScheduleEntry)
        .filter(
            ScheduleEntry.schedule_id == prev_schedule.schedule_id,
            ScheduleEntry.work_date >= start_date,
            ScheduleEntry.work_date <= end_date,
        )
        .all()
    )

    entries_by_nurse = {}
    for entry in entries:
        nid = entry.nurse_id
        if nid not in entries_by_nurse:
            entries_by_nurse[nid] = {}
        entries_by_nurse[nid][entry.work_date.day] = entry.shift_id

    # 전월 assignment 치환 (파견/병동이동/휴직)
    from services.assignment_service import get_roster_assignments
    _prev_assignments = get_roster_assignments(
        db, group_id=target_group_id, year=prev_year, month=prev_month,
    )

    # 변경(target) 병동의 전월 발행 근무표 사전 적재 (caching).
    # target_gid → {schedule_id, schedule_name, entries_by_nurse: {nurse_id: {day: shift_id}}}
    _target_prev_cache: dict[str, dict] = {}

    def _load_target_prev_tail(_t_gid: str) -> dict:
        if _t_gid in _target_prev_cache:
            return _target_prev_cache[_t_gid]
        _t_sched = (
            db.query(Schedule)
            .filter(
                Schedule.group_id == _t_gid,
                Schedule.year == prev_year,
                Schedule.month == prev_month,
                Schedule.status == "issued",
                Schedule.dropped == False,
            )
            .first()
        )
        if not _t_sched:
            _t_sched = (
                db.query(Schedule)
                .filter(
                    Schedule.group_id == _t_gid,
                    Schedule.year == prev_year,
                    Schedule.month == prev_month,
                    Schedule.dropped == False,
                )
                .order_by(Schedule.created_at.desc())
                .first()
            )
        if not _t_sched:
            _out = {"schedule_id": None, "schedule_name": None, "entries_by_nurse": {}}
            _target_prev_cache[_t_gid] = _out
            return _out
        _t_entries = (
            db.query(ScheduleEntry)
            .filter(
                ScheduleEntry.schedule_id == _t_sched.schedule_id,
                ScheduleEntry.work_date >= start_date,
                ScheduleEntry.work_date <= end_date,
            )
            .all()
        )
        _t_by_nurse: dict = {}
        for _e in _t_entries:
            _t_by_nurse.setdefault(_e.nurse_id, {})[_e.work_date.day] = _e.shift_id
        _out = {
            "schedule_id": _t_sched.schedule_id,
            "schedule_name": _t_sched.name,
            "entries_by_nurse": _t_by_nurse,
        }
        _target_prev_cache[_t_gid] = _out
        return _out

    def _coerce_date(v):
        """date/datetime/문자열(날짜 또는 'YYYY-MM-DD HH:MM:SS' 등) → date. None 안전."""
        if v is None:
            return None
        if isinstance(v, datetime):
            return v.date()
        if isinstance(v, date):
            return v
        # 문자열: 시간 포함 datetime 문자열도 앞 10자(YYYY-MM-DD)로 파싱
        return date.fromisoformat(str(v)[:10])

    nurse_list = []
    for nurse in nurses:
        shifts = {
            str(d): entries_by_nurse.get(nurse.nurse_id, {}).get(d)
            for d in tail_day_list
        }
        # assignment 기간의 shift는 None으로 마스킹 + reason/target 메타 + target_shifts 동봉
        _a_list = _prev_assignments.get(nurse.nurse_id, [])
        nurse_assignments: list[dict] = []
        for _a in _a_list:
            _a_start = _coerce_date(_a.get("start_date"))
            _a_end = _coerce_date(_a.get("end_date"))
            if _a_start is None:
                # start_date 없는 비정상 행은 건너뜀(비교 불가)
                continue
            _overlap_days: list[int] = []
            for d in tail_day_list:
                _cell_date = date(prev_year, prev_month, d)
                if _cell_date >= _a_start and (not _a_end or _cell_date <= _a_end):
                    shifts[str(d)] = None
                    _overlap_days.append(d)
            if _overlap_days:
                _t_gid = _a.get("target_group_id") or ""
                _target_shifts: dict[str, str | None] = {}
                _target_schedule_id = None
                if _t_gid and _a.get("reason") in ("파견", "병동이동"):
                    _t_payload = _load_target_prev_tail(_t_gid)
                    _target_schedule_id = _t_payload.get("schedule_id")
                    _t_nurse_map = (_t_payload.get("entries_by_nurse") or {}).get(nurse.nurse_id, {})
                    for d in _overlap_days:
                        _target_shifts[str(d)] = _t_nurse_map.get(d)
                nurse_assignments.append({
                    "reason": _a.get("reason"),
                    "target_group_id": _t_gid,
                    "target_group_name": _a.get("target_group_name") or "",
                    "start_day": _overlap_days[0],
                    "end_day": _overlap_days[-1],
                    "start_date": _a.get("start_date"),
                    "end_date": _a.get("end_date"),
                    "target_schedule_id": _target_schedule_id,
                    "target_shifts": _target_shifts,
                })
        nurse_list.append(
            {
                "nurse_id": nurse.nurse_id,
                "name": nurse.name,
                "shifts": shifts,
                "assignments": nurse_assignments,
            }
        )

    # 인바운드(타 그룹 소속) 간호사: home 그룹 전월 tail 로 경계 연속성 채움
    nurse_list.extend(
        _build_inbound_prev_tail_nurses(
            db,
            ref_schedule_id,
            {n.nurse_id for n in nurses},
            tail_day_list,
            _load_target_prev_tail,
        )
    )

    return {
        "prev_year": prev_year,
        "prev_month": prev_month,
        "data": {
            "schedule_id": prev_schedule.schedule_id,
            "schedule_name": prev_schedule.name,
            "schedule_status": prev_schedule.status,
            "tail_days": tail_day_list,
            "nurses": nurse_list,
        },
    }


def get_issued_roster_snapshot_service(
    year: int,
    month: int,
    current_user,
    db: Session,
    target_group_id: str | None = None,
    _expand_target_rosters: bool = True,
) -> dict | None:
    """
    특정 연월에 대해 활성 발행본(is_active_issued=True)의 근무표 스냅샷을 조회합니다.

    관리자(ADM)는 `target_group_id`로 대상 그룹을 지정할 수 있습니다.
    `_expand_target_rosters=True`일 때 응답의 `target_rosters` 필드에 변경(파견/병동이동)
    병동의 동월 발행 스냅샷 body 를 동봉합니다 (재귀 차단을 위해 내부 호출은 False).
    """
    if not current_user:
        raise Exception("Not authenticated")

    if not target_group_id:
        target_group_id = getattr(current_user, "group_id", None)
    if not target_group_id:
        raise Exception("대상 그룹이 없습니다.")

    # office_id 결정: 토큰의 office_id 우선, 없으면 그룹 조회
    office_id = getattr(current_user, "office_id", None)
    if not office_id:
        group_row = db.query(Group).filter(Group.group_id == target_group_id).first()
        if not group_row:
            raise Exception("그룹 정보를 찾을 수 없습니다.")
        office_id = group_row.office_id

    # 오피스/그룹 기준 활성 스냅샷 조회 후, year/month는 meta_json으로 필터링
    snapshots = (
        db.query(IssuedRosterSnapshot)
        .filter(
            IssuedRosterSnapshot.office_id == office_id,
            IssuedRosterSnapshot.group_id == target_group_id,
            IssuedRosterSnapshot.is_active_issued == True,
        )
        .order_by(IssuedRosterSnapshot.created_at.desc())
        .all()
    )

    matched_snapshot: IssuedRosterSnapshot | None = None
    for snap in snapshots:
        meta = snap.meta_json or {}
        if meta.get("year") == year and meta.get("month") == month:
            matched_snapshot = snap
            break

    if not matched_snapshot:
        return None

    _meta = matched_snapshot.meta_json or {}
    _nurses_json = matched_snapshot.nurses_json or []

    # 관련 병동(group) 목록: 발행 그룹 + 조회 월과 strict overlap 되는 모든 파견/병동이동.
    # N_tail 버퍼는 auth gate 에서만 사용, groups 집계는 월내 strict.
    # 예: 4/14~6/15 파견 → 4/5/6월 모두 포함. 5/1~5/8 파견 → 5월만 포함 (4월·6월 제외).
    _m_start = date(year, month, 1)
    _m_end = date(year, month, calendar.monthrange(year, month)[1])

    def _parse_iso_date(v):
        if not isinstance(v, str) or not v:
            return None
        try:
            return date.fromisoformat(v[:10])
        except ValueError:
            return None

    _gid_set: set[str] = set()
    if matched_snapshot.group_id:
        _gid_set.add(matched_snapshot.group_id)
    _name_frozen: dict[str, str] = {}

    def _absorb_window(_block) -> None:
        """nurses_json 내 inbound 블록의 월 overlap 만 _gid_set 에 흡수."""
        if isinstance(_block, dict):
            _block = _block.get("inbound_list") or []
        for _entry in _block or []:
            if not isinstance(_entry, dict):
                continue
            _s = _parse_iso_date(_entry.get("startDate") or _entry.get("start_date"))
            _e = _parse_iso_date(_entry.get("endDate") or _entry.get("end_date"))
            if _s is None or _s > _m_end:
                continue
            if _e is not None and _e < _m_start:
                continue
            _tgid = _entry.get("target_group_id") or _entry.get("targetGroupId")
            if _tgid:
                _gid_set.add(_tgid)
                _tname = _entry.get("target_group_name") or _entry.get("targetGroupName")
                if _tname:
                    _name_frozen[_tgid] = _tname

    # 주의: 간호사의 group_id 는 자동으로 _gid_set 에 추가하지 않는다.
    # - home 간호사의 group_id 는 publishing group 과 동일(이미 추가됨).
    # - inbound 간호사(타 병동 home)의 home group 은 현재 월과 무관한 과거/고아 기록일 수
    #   있으므로, 아래 live assignment 쿼리로 실제 해당 월 overlap 파견만 집계한다.

    # 응답 시점 overlay: inbound / current_assignment 를 현재 DB 상태로 덮어쓴다.
    # 스냅샷 생성 시점(roster 발행 시) 이후 발생한 파견/병동이동/휴직/퇴사/프리셉티 변경은
    # snapshot.nurses_json 에 반영되지 않으므로, 조회 시점에 _build_inbound_blocks 로
    # NurseProfile 응답과 동일한 결과를 즉석 합성한다.
    from services.nurse_service import _build_inbound_blocks as _live_inbound_blocks

    _live_nurse_ids = [
        _n.get("nurse_id")
        for _n in _nurses_json
        if isinstance(_n, dict) and _n.get("nurse_id")
    ]
    _live_blocks = _live_inbound_blocks(db, _live_nurse_ids) if _live_nurse_ids else {}

    for _n in _nurses_json:
        if not isinstance(_n, dict):
            continue
        _n_gid = _n.get("group_id")
        _n_gname = _n.get("group_name")
        if _n_gid and _n_gname:
            _name_frozen[_n_gid] = _n_gname
        # revert 이전 발행된 stale snapshot 의 outbound/is_outbound 키 제거
        _n.pop("outbound", None)
        _n.pop("is_outbound", None)
        _block = _live_blocks.get(_n.get("nurse_id") or "") or {}
        _n["inbound"] = _block.get("inbound_list") or []
        _n["current_assignment"] = _block.get("current_assignment")
        _absorb_window(_n.get("inbound"))

    # Live 집계: inbound (target=발행그룹) 만. source_group_id 를 groups 에 추가.
    if matched_snapshot.group_id:
        from services.assignment_service import (
            get_active_assignments_for_month as _get_assigns_for_month,
        )
        _live_assigns = _get_assigns_for_month(
            db, matched_snapshot.group_id, year, month
        )
        for _a in _live_assigns:
            if _a.reason not in ("파견", "병동이동"):
                continue
            if (
                _a.target_group_id == matched_snapshot.group_id
                and _a.source_group_id
                and _a.source_group_id != matched_snapshot.group_id
            ):
                _gid_set.add(_a.source_group_id)
    _meta_gname = _meta.get("group_name")
    if matched_snapshot.group_id and _meta_gname:
        _name_frozen.setdefault(matched_snapshot.group_id, _meta_gname)
    _missing = _gid_set - set(_name_frozen.keys())
    if _missing:
        for gid, gname in (
            db.query(Group.group_id, Group.group_name)
            .filter(Group.group_id.in_(_missing))
            .all()
        ):
            _name_frozen[gid] = gname or ""
    groups_out = [
        {"group_id": _gid, "group_name": _name_frozen.get(_gid, "")}
        for _gid in _gid_set
    ]

    _group_name = _name_frozen.get(matched_snapshot.group_id or "", "") or (
        _meta_gname or ""
    )

    # 변경(파견/병동이동) 병동의 동월 발행 스냅샷 body 동봉.
    # 재귀 차단: 내부 호출은 _expand_target_rosters=False.
    target_rosters: dict[str, dict] = {}
    if _expand_target_rosters:
        _self_gid = matched_snapshot.group_id or ""
        for _t_gid in _gid_set:
            if not _t_gid or _t_gid == _self_gid:
                continue
            try:
                _t_snap = get_issued_roster_snapshot_service(
                    year=year,
                    month=month,
                    current_user=current_user,
                    db=db,
                    target_group_id=_t_gid,
                    _expand_target_rosters=False,
                )
            except Exception:
                _t_snap = None
            if not _t_snap:
                target_rosters[_t_gid] = {
                    "group_id": _t_gid,
                    "group_name": _name_frozen.get(_t_gid, ""),
                    "snapshot_id": None,
                    "schedule_id": None,
                    "nurses": [],
                    "shifts": [],
                    "shift_manage": [],
                    "roster": {},
                }
                continue
            target_rosters[_t_gid] = {
                "group_id": _t_snap.get("group_id"),
                "group_name": _t_snap.get("group_name") or _name_frozen.get(_t_gid, ""),
                "snapshot_id": _t_snap.get("snapshot_id"),
                "schedule_id": _t_snap.get("schedule_id"),
                "nurses": _t_snap.get("nurses") or [],
                "shifts": _t_snap.get("shifts") or [],
                "shift_manage": _t_snap.get("shift_manage") or [],
                "roster": _t_snap.get("roster") or {},
            }

    return {
        "snapshot_id": matched_snapshot.snapshot_id,
        "office_id": matched_snapshot.office_id,
        "group_id": matched_snapshot.group_id,
        "group_name": _group_name,
        "schedule_id": matched_snapshot.schedule_id,
        "version": matched_snapshot.version,
        "created_at": matched_snapshot.created_at,
        "is_active_issued": matched_snapshot.is_active_issued,
        "meta": _meta,
        "config": matched_snapshot.config_json or {},
        "nurses": _nurses_json,
        "shifts": matched_snapshot.shifts_json or [],
        "shift_manage": matched_snapshot.shift_manage_json or [],
        "roster": matched_snapshot.roster_json or {},
        "violations": matched_snapshot.violations_json
        or {"messages": [], "details": []},
        "groups": groups_out,
        "target_rosters": target_rosters,
    }


def get_my_issued_roster_service(
    year: int,
    month: int,
    current_user,
    db: Session,
) -> dict | None:
    """
    로그인 사용자 본인의 발행된 근무표만 조회합니다.
    snapshot의 roster_json에서 nurse_id 기준으로 추출.
    """
    # 토큰 group_id 대신 nurse_id→DB home group 으로 스냅샷 조회(그룹전환/소속변경 안전).
    from services.group_access import resolve_home_group_id

    home_gid = resolve_home_group_id(db, current_user)
    snapshot_data = get_issued_roster_snapshot_service(
        year=year, month=month, current_user=current_user, db=db,
        target_group_id=home_gid,
    )
    if not snapshot_data:
        return None

    roster = snapshot_data.get("roster") or {}
    nurse_id = getattr(current_user, "nurse_id", None)
    if not nurse_id:
        return None

    roster_nurses = roster.get("nurses") or []
    my_roster = next(
        (n for n in roster_nurses if str(n.get("nurse_id")) == str(nurse_id)),
        None,
    )
    if not my_roster:
        return None

    from calendar import monthrange
    from datetime import date
    from db.models import Group
    from services.assignment_service import get_active_assignments_for_month

    days_in_month = monthrange(year, month)[1]
    m_start = date(year, month, 1)
    m_end = date(year, month, days_in_month)
    src_gid = home_gid or ""
    src_group_row = (
        db.query(Group).filter(Group.group_id == src_gid).first() if src_gid else None
    )
    src_group_name = src_group_row.group_name if src_group_row else ""

    shift_colors: dict[str, str] = dict(roster.get("shift_colors") or {})
    src_cells = my_roster.get("schedule") or []
    src_ids = my_roster.get("schedule_ids") or []

    def _cell_code(_cell) -> str:
        if isinstance(_cell, dict):
            return str(_cell.get("code", "") or "")
        return str(_cell or "")

    def _cell_color(_cell, _code: str) -> str:
        if isinstance(_cell, dict) and _cell.get("color"):
            return str(_cell.get("color") or "")
        return str(shift_colors.get(_code, "") or "")

    # 일자별 병합 배열 초기화(=source 기준)
    schedule_days: list[dict] = []
    for _i in range(days_in_month):
        _cell = src_cells[_i] if _i < len(src_cells) else None
        _code = _cell_code(_cell)
        schedule_days.append({
            "day": _i + 1,
            "code": _code,
            "color": _cell_color(_cell, _code),
            "schedule_id": (src_ids[_i] if _i < len(src_ids) else None),
            "group_id": src_gid,
            "group_name": src_group_name,
            "is_source": True,
            "reason": None,
        })

    # 파견/병동이동: target 근무표 overlay (복수 assignment 지원)
    # 본인 nurse_id 기반으로 month 와 overlap 되는 모든 assignment 수집
    # (영구이동 발효 후엔 src_gid 가 변경되므로 source/target group 필터 사용 금지)
    from db.models import NurseAssignment as _NurseAssignment
    _all_my_asgs = (
        db.query(_NurseAssignment)
        .filter(
            _NurseAssignment.nurse_id == nurse_id,
            _NurseAssignment.status.in_(["active", "completed"]),
            _NurseAssignment.reason.in_(["파견", "병동이동"]),
        )
        .all()
    )
    assignments = [
        a for a in _all_my_asgs
        if a.start_date is not None
        and a.start_date <= m_end
        and (a.end_date or a.expected_end_date or m_end) >= m_start
    ]
    # outbound (현재 home 외부로 나간 케이스): target != src_gid
    my_transfers = [
        a for a in assignments
        if a.target_group_id and a.target_group_id != src_gid
    ]
    # inbound (영구이동으로 src_gid 에 들어온 케이스): target == src_gid AND source != src_gid
    my_inbound_transfers = [
        a for a in assignments
        if a.reason == "병동이동"
        and a.target_group_id == src_gid
        and a.source_group_id
        and a.source_group_id != src_gid
    ]
    transfers_out: list[dict] = []

    if my_transfers:
        _tgt_gids = {a.target_group_id for a in my_transfers}
        _grows = db.query(Group).filter(Group.group_id.in_(list(_tgt_gids))).all()
        tgid_to_name = {g.group_id: g.group_name for g in _grows}

        _tgt_cache: dict[str, dict | None] = {}

        def _load_my_target(_tgid: str) -> dict | None:
            if _tgid in _tgt_cache:
                return _tgt_cache[_tgid]
            _snap = get_issued_roster_snapshot_service(
                year=year, month=month, current_user=current_user, db=db,
                target_group_id=_tgid,
            )
            _out: dict | None = None
            if _snap:
                _t_roster = _snap.get("roster") or {}
                _t_nurses = _t_roster.get("nurses") or []
                _t_my = next(
                    (n for n in _t_nurses if str(n.get("nurse_id")) == str(nurse_id)),
                    None,
                )
                if _t_my:
                    _out = {
                        "schedule": _t_my.get("schedule") or [],
                        "schedule_ids": _t_my.get("schedule_ids") or [],
                        "shift_colors": _t_roster.get("shift_colors") or {},
                    }
            _tgt_cache[_tgid] = _out
            return _out

        for _a in my_transfers:
            _a_start = _a.start_date
            _a_end = _a.end_date or _a.expected_end_date
            # 월내 실제 overlap 구간 계산 (N_tail 버퍼 only 인 파견은 in_month=False → overlay 스킵)
            _overlap_start = max(_a_start, m_start)
            _overlap_end = min(_a_end, m_end) if _a_end else m_end
            _in_month = _overlap_start <= _overlap_end
            _p_start = _overlap_start.day if _in_month else 0
            _p_end = _overlap_end.day if _in_month else 0
            _tgid = _a.target_group_id
            _tgt_name = tgid_to_name.get(_tgid, "")
            _tgt = _load_my_target(_tgid)
            _target_issued = _tgt is not None

            if _in_month and _target_issued:
                shift_colors.update(_tgt.get("shift_colors") or {})
                _t_cells = _tgt["schedule"]
                _t_ids = _tgt["schedule_ids"]
                for d in range(_p_start, _p_end + 1):
                    idx = d - 1
                    if idx >= days_in_month:
                        break
                    _t_cell = _t_cells[idx] if idx < len(_t_cells) else None
                    _code = _cell_code(_t_cell)
                    schedule_days[idx].update({
                        "code": _code,
                        "color": _cell_color(_t_cell, _code),
                        "schedule_id": (_t_ids[idx] if idx < len(_t_ids) else None),
                        "group_id": _tgid,
                        "group_name": _tgt_name,
                        "is_source": False,
                        "reason": _a.reason,
                    })
            elif _in_month:
                for d in range(_p_start, _p_end + 1):
                    idx = d - 1
                    if idx >= days_in_month:
                        break
                    schedule_days[idx].update({
                        "schedule_id": None,
                        "group_id": _tgid,
                        "group_name": _tgt_name,
                        "is_source": False,
                        "reason": _a.reason,
                    })
            elif _target_issued:
                # 버퍼 내 파견이라도 target shift_colors 는 머지하여 후속 조회/렌더에 활용.
                shift_colors.update(_tgt.get("shift_colors") or {})

            transfers_out.append({
                "reason": _a.reason,
                "target_group_id": _tgid,
                "target_group_name": _tgt_name,
                "start_date": str(_a_start),
                "end_date": str(_a_end) if _a_end else None,
                "period_start_day": _p_start,
                "period_end_day": _p_end,
                "target_issued": _target_issued,
            })

    # 과거 home overlay (영구 병동이동 inbound):
    # 본인이 src_gid 로 이동해 들어온 경우, transfer.start_date 이전 일자는
    # 이전 home(source_group_id) 근무표를 참조해 채운다.
    if my_inbound_transfers:
        _src_home_gids = {a.source_group_id for a in my_inbound_transfers}
        _src_grows = db.query(Group).filter(Group.group_id.in_(list(_src_home_gids))).all()
        _src_gid_to_name = {g.group_id: g.group_name for g in _src_grows}
        _prev_home_cache: dict[str, dict | None] = {}

        def _load_my_prev_home(_pgid: str) -> dict | None:
            if _pgid in _prev_home_cache:
                return _prev_home_cache[_pgid]
            _snap = get_issued_roster_snapshot_service(
                year=year, month=month, current_user=current_user, db=db,
                target_group_id=_pgid,
            )
            _out: dict | None = None
            if _snap:
                _p_roster = _snap.get("roster") or {}
                _p_nurses = _p_roster.get("nurses") or []
                _p_my = next(
                    (n for n in _p_nurses if str(n.get("nurse_id")) == str(nurse_id)),
                    None,
                )
                if _p_my:
                    _out = {
                        "schedule": _p_my.get("schedule") or [],
                        "schedule_ids": _p_my.get("schedule_ids") or [],
                        "shift_colors": _p_roster.get("shift_colors") or {},
                    }
            _prev_home_cache[_pgid] = _out
            return _out

        for _a in my_inbound_transfers:
            _a_start = _a.start_date
            # 이전 home 구간: 월 시작 ~ transfer 시작일 전날 (월 내 strict overlap)
            if _a_start <= m_start:
                continue  # 월 시작 이전에 이미 이동 완료 — 이전 home 표시 불필요
            _prev_end = min(_a_start - timedelta(days=1), m_end)
            _prev_start = m_start
            if _prev_start > _prev_end:
                continue
            _pgid = _a.source_group_id
            _pgname = _src_gid_to_name.get(_pgid, "")
            _prev = _load_my_prev_home(_pgid)
            _prev_issued = _prev is not None

            _p_start = _prev_start.day
            _p_end = _prev_end.day

            if _prev_issued:
                shift_colors.update(_prev.get("shift_colors") or {})
                _p_cells = _prev["schedule"]
                _p_ids = _prev["schedule_ids"]
                for d in range(_p_start, _p_end + 1):
                    idx = d - 1
                    if idx >= days_in_month:
                        break
                    _p_cell = _p_cells[idx] if idx < len(_p_cells) else None
                    _code = _cell_code(_p_cell)
                    schedule_days[idx].update({
                        "code": _code,
                        "color": _cell_color(_p_cell, _code),
                        "schedule_id": (_p_ids[idx] if idx < len(_p_ids) else None),
                        "group_id": _pgid,
                        "group_name": _pgname,
                        "is_source": False,
                        "reason": _a.reason,
                    })
            else:
                # 이전 home snapshot 미발행 → cell 비우고 group/reason 만 표시
                for d in range(_p_start, _p_end + 1):
                    idx = d - 1
                    if idx >= days_in_month:
                        break
                    schedule_days[idx].update({
                        "code": "",
                        "schedule_id": None,
                        "group_id": _pgid,
                        "group_name": _pgname,
                        "is_source": False,
                        "reason": _a.reason,
                    })

            # 이전 home: 프론트가 별도 분기 없이도 식별 가능하도록 라벨에 접두어
            _label = f"이전: {_pgname}" if _pgname else "이전 병동"
            transfers_out.append({
                "reason": _a.reason,
                "target_group_id": _pgid,
                "target_group_name": _label,
                "start_date": str(_prev_start),
                "end_date": str(_prev_end),
                "period_start_day": _p_start,
                "period_end_day": _p_end,
                "target_issued": _prev_issued,
                "is_prev_home": True,
            })

    # counts 최종 재계산
    counts: dict[str, int] = {code: 0 for code in shift_colors}
    for _d in schedule_days:
        _c = _d["code"]
        if _c:
            counts[_c] = counts.get(_c, 0) + 1

    # 관련 병동(group) 목록: 본인 소속 + 파견/이동 target 전체
    _groups_map: dict[str, str] = {}
    if src_gid:
        _groups_map[src_gid] = src_group_name or ""
    for _t in transfers_out:
        _tgid = _t.get("target_group_id")
        if _tgid and _tgid not in _groups_map:
            _groups_map[_tgid] = _t.get("target_group_name") or ""
    groups_out = [
        {"group_id": _gid, "group_name": _gname}
        for _gid, _gname in _groups_map.items()
    ]

    return {
        "year": roster.get("year"),
        "month": roster.get("month"),
        "nurse_id": my_roster.get("nurse_id"),
        "name": my_roster.get("name"),
        "source_group_id": src_gid,
        "source_group_name": src_group_name,
        "issued_at": snapshot_data.get("created_at"),
        "shift_colors": shift_colors,
        "schedule": schedule_days,
        "counts": counts,
        "transfers": transfers_out,
        "groups": groups_out,
    }


#: 주 시작 요일 = 일요일. `date.weekday()` 는 월=0…일=6 이므로 +1 후 7 로 나눈 나머지가
#: '그 주 일요일로부터 며칠째' 가 된다(일→0, 월→1, … 토→6).
_WEEK_START_SUNDAY_OFFSET = 1


def get_my_issued_week_service(
    current_user,
    db: Session,
    base_date: date | None = None,
) -> dict:
    """본인의 **발행(마감)** 근무표 중 기준일이 속한 주(일~토) 7일을 돌려준다.

    ★ 주는 달을 넘는다. 8/30(일)~9/5(토) 처럼 걸치면 8월·9월 **두 발행본**을 각각 읽어
      이어붙여야 한다. 한 달만 읽으면 주의 절반이 조용히 빈다.
    ★ 월별 조회는 `get_my_issued_roster_service` 를 그대로 쓴다. 파견/병동이동 overlay,
      shift_colors, home group 해석이 전부 그 안에 있어 여기서 다시 구현하면 갈린다.
    ★ 발행 안 된 달은 `code=None, issued=False` 로 채운다. 빼버리면 프론트가 7칸
      요일 격자를 못 그린다. "미발행" 은 오류가 아니라 정상 상태다.

    Args:
        base_date: 기준일(기본=오늘). 프론트가 주를 앞뒤로 넘길 때 쓴다.

    Returns:
        days 는 **항상 7개**(일→토). 조회 가능한 발행본이 하나도 없어도 형태는 유지된다.
        ★ `issued` 는 "그 달이 발행됐는가" 가 아니라 **"그 날 내가 조회 가능한 근무표가
          있는가"** 다. 그 달이 미발행이어도, 발행됐지만 내가 그 달 소속이 아니어도
          똑같이 False 다. 간호사 화면에서는 두 경우 모두 "내 근무 없음" 으로 같게
          표시되므로 굳이 가르지 않는다(가르려면 스냅샷 존재 여부를 따로 조회해야 해
          왕복이 는다).

    Raises:
        ValueError: 기준일이 `date` 표현 범위의 양 끝 6일 안쪽일 때. 라우터가 400 으로 옮긴다.
    """
    today = date.today()
    anchor = base_date or today
    # 주 계산은 기준일에서 앞뒤로 최대 6일 움직인다. date.min/max 코앞에서는 그 이동이
    # 표현 범위를 넘어 OverflowError 가 나고, 라우터의 포괄 except 가 그것을 **500** 으로
    # 바꾼다 — 클라이언트 입력 오류인데 서버 장애로 보고되는 셈이다.
    # 실측(2026-08-31): 0001-01-01~06 은 week_start 에서, 9999-12-26~31 은 week_end 에서 터진다.
    _span = timedelta(days=6)
    if not (date.min + _span <= anchor <= date.max - _span):
        raise ValueError(
            f"기준일이 주 계산 가능 범위를 벗어났습니다: {anchor.isoformat()} "
            f"(허용 {(date.min + _span).isoformat()} ~ {(date.max - _span).isoformat()})"
        )
    week_start = anchor - timedelta(days=(anchor.weekday() + _WEEK_START_SUNDAY_OFFSET) % 7)
    week_days = [week_start + timedelta(days=i) for i in range(7)]

    # 주가 걸친 달만 조회한다(최대 2개). 같은 달을 두 번 읽지 않는다.
    months = list(dict.fromkeys((d.year, d.month) for d in week_days))
    by_month: dict[tuple[int, int], dict | None] = {}
    for y, m in months:
        # ★ 예외를 삼키지 않는다. 조회 실패(인가·DB·스냅샷 파싱)를 `None` 으로 눕히면
        #   "아직 발행 안 됨" 과 구분이 사라져, 장애 중에도 200 으로 **빈 주**가 나간다.
        #   간호사는 근무표가 안 나온 줄 알지만 실제로는 시스템이 고장난 것이다.
        #   진짜 미발행은 이 함수가 이미 `None` 을 돌려주므로 따로 감쌀 이유가 없다.
        by_month[(y, m)] = get_my_issued_roster_service(
            year=y, month=m, current_user=current_user, db=db
        )

    _WD = ["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"]
    days_out: list[dict] = []
    for i, d in enumerate(week_days):
        data = by_month.get((d.year, d.month))
        # ★ 색 폴백은 **그 날이 속한 달**의 색표로만 본다. 두 달 색표를 하나로 합치면
        #   같은 코드가 달마다 다른 색일 때 뒤 달이 앞 달을 덮어써 2월 칸이 3월 색으로
        #   칠해진다(주중 병동이동이면 실제로 갈린다). 그래서 병합하지 않는다.
        colors_m = (data or {}).get("shift_colors") or {}
        cell = None
        if data:
            cell = next(
                (c for c in (data.get("schedule") or []) if int(c.get("day") or 0) == d.day),
                None,
            )
        code = str((cell or {}).get("code") or "") or None
        days_out.append({
            "date": d.isoformat(),
            "weekday": _WD[i],
            "day": d.day,
            "code": code,
            # ★ 코드가 없으면 색도 없다. 근무가 없는 칸에 색만 남으면 화면에는 '무언가
            #   배정된 칸'으로 보인다. 실측(발행본 12건·셀 5,653개)에 그런 셀은 없지만,
            #   code 와 color 가 따로 놀 수 있는 구조라 여기서 묶어 둔다.
            "color": ((cell or {}).get("color") or colors_m.get(code) or None) if code else None,
            "group_id": (cell or {}).get("group_id"),
            "group_name": (cell or {}).get("group_name"),
            "is_today": d == today,
            "issued": data is not None,
        })

    today_cell = next((c for c in days_out if c["is_today"]), None)
    me = next((v for v in by_month.values() if v), None) or {}
    return {
        "week_start": week_start.isoformat(),
        "week_end": week_days[-1].isoformat(),
        "today": today.isoformat(),
        # 기준일이 이번 주 밖이면(프론트가 다른 주를 조회) today_* 는 비운다.
        "today_code": (today_cell or {}).get("code"),
        "today_color": (today_cell or {}).get("color"),
        "nurse_id": me.get("nurse_id") or getattr(current_user, "nurse_id", None),
        # ★ 한 달도 발행본이 없으면 `me` 가 비어 이름이 통째로 null 이 된다. 신원 표시는
        #   근무표 유무와 무관하므로 로그인 사용자에서 채운다(nurse_id 와 같은 규칙).
        "name": me.get("name") or getattr(current_user, "name", None),
        # ★ 응답에 통합 `shift_colors` 를 두지 않는다. 주가 두 달에 걸치면 같은 코드가
        #   서로 다른 색일 수 있어 하나로 합치는 순간 한쪽이 틀린다. 칠할 색은
        #   `days[].color` 에 칸마다 이미 정확히 들어 있으므로 그것이 정본이다.
        "days": days_out,
    }


# ─────────────────── 모바일: 본인 발행 근무표 파생 조회 ───────────────────
#: 다음 OFF 탐색 상한(일). 연속근무 5일이 하드 제약이라 정상 근무표면 6일 안에 나온다.
#: 그보다 길면 데이터 이상이거나 미발행이므로, 달을 무한정 넘겨 읽지 않고 끊는다.
_NEXT_OFF_HORIZON_DAYS = 14


def _raw_cell_code(cell) -> str:
    """스냅샷/월조회 셀 → 근무코드 문자열.

    셀은 문자열이거나 `{"code": ...}` dict 두 형태로 들어온다
    (`get_my_issued_roster_service._cell_code` 와 같은 규칙).
    """
    if isinstance(cell, dict):
        return str(cell.get("code") or "").strip()
    return str(cell or "").strip()


def _cell_of_day(my_month: dict | None, day: int) -> dict:
    """월 조회 결과(`get_my_issued_roster_service`)에서 해당 일자 셀. 없으면 빈 dict."""
    days = (my_month or {}).get("schedule") or []
    for cell in days:
        if int(cell.get("day") or 0) == day:
            return cell
    return {}


def _shift_meta_by_code(snapshot: dict | None) -> dict[str, dict]:
    """발행 스냅샷의 `shifts` → `{코드: {name, color, start/end_time, default_shift, is_work}}`.

    ★ 현재 `shifts` 테이블이 아니라 **스냅샷**을 본다. `shifts` 는 PK 가 없고 코드
      문자열이 실제로 교체되므로(`N`→`N1`, `O`→`OFF` 실측), 지금 정의로 과거 발행본을
      해석하면 이름·색·시간대가 그 달 화면과 어긋난다. 스냅샷은 발행 시점 정의라
      코드가 나중에 바뀌어도 흔들리지 않는다.
    ★ OFF 판정은 `type != '근무'` — `_get_off_shift_ids` 와 같은 기준이다.
      휴무·휴가·보건휴가 등이 모두 여기 걸린다.
    """
    out: dict[str, dict] = {}
    for row in (snapshot or {}).get("shifts") or []:
        code = str(row.get("shift_id") or "").strip()
        if not code:
            continue
        out[code] = {
            "name": row.get("name") or code,
            "color": row.get("color") or "",
            "start_time": row.get("start_time"),
            "end_time": row.get("end_time"),
            "default_shift": str(row.get("default_shift") or code).strip() or code,
            "is_work": str(row.get("type") or "") == "근무",
        }
    return out


def _cell_is_unknown(code: str, meta: dict[str, dict]) -> bool:
    """그 셀의 배정을 **알 수 없는가** — 파견지 발행본이 없는 날.

    ★ 이때 셀에는 코드가 남아 있지만 그건 **홈 병동의 잔여값**이다
      (`get_my_issued_roster_service` 의 미발행 분기가 `group_id` 만 바꾸고 `code` 는
      안 건드린다). `_meta_for_cell` 이 그 경우 빈 메타를 돌려주므로 여기서 가려낸다.
    ★★ 이 판정을 **모든 소비처가 함께** 써야 한다. 한 곳만 막으면 나머지가 낡은 코드를
      진짜 배정처럼 내보낸다(실제로 그렇게 반쪽만 막았다가 지적받았다).
    """
    return bool(code) and not meta


def _code_view(code: str, meta: dict[str, dict]) -> dict:
    """근무코드 표시용 최소 형태. 스냅샷에 없는 코드는 코드 자체를 이름으로 쓴다."""
    m = meta.get(code) or {}
    return {"code": code, "name": m.get("name") or code, "color": m.get("color") or ""}


def _code_detail(code: str, meta: dict[str, dict]) -> dict:
    """근무코드 상세(시간·시간대 포함).

    ★ `is_work` 는 스냅샷에 없는 코드일 때 `None` 이다. `False` 로 눕히면 화면이
      OFF 로 읽는데, 실제로는 '이 코드의 정의를 모른다' 는 다른 상태다.
    """
    m = meta.get(code)
    return {
        "code": code,
        "name": (m or {}).get("name") or code,
        "color": (m or {}).get("color") or "",
        "start_time": (m or {}).get("start_time"),
        "end_time": (m or {}).get("end_time"),
        "default_shift": (m or {}).get("default_shift") or code,
        "is_work": m.get("is_work") if m else None,
    }


def _load_group_snapshot(
    db: Session, current_user, year: int, month: int, group_id: str | None
) -> dict | None:
    """특정 병동의 그 달 활성 발행 스냅샷. 파견지 병동 조회에도 쓴다.

    ★ `_expand_target_rosters=False` 가 중요하다. 기본값(True)은 관련 병동 스냅샷을
      재귀로 전부 끌어오는데, 여기서 필요한 건 그 병동의 `shifts`·`nurses`·`roster`
      뿐이라 조회가 몇 배로 늘어날 이유가 없다.
    """
    if not group_id:
        return None
    return get_issued_roster_snapshot_service(
        year=year,
        month=month,
        current_user=current_user,
        db=db,
        target_group_id=group_id,
        _expand_target_rosters=False,
    )


def _snapshot_cached(
    db: Session, current_user, cache: dict, year: int, month: int, group_id: str | None
) -> dict | None:
    """`_load_group_snapshot` 의 캐시 래퍼. 한 요청 안에서 같은 (연월·병동)을 두 번 읽지 않는다.

    한 호출에서 같은 스냅샷을 코드 메타·동료 목록·다음 OFF 가 각각 필요로 한다.
    """
    key = ("snap", year, month, group_id)
    if key not in cache:
        cache[key] = _load_group_snapshot(db, current_user, year, month, group_id)
    return cache[key]


def _month_view(
    db: Session, current_user, cache: dict, year: int, month: int
) -> tuple[dict | None, dict[str, dict]]:
    """`(그 달 본인 근무표, 그 달 코드 메타)`. 같은 달을 두 번 읽지 않도록 캐시한다.

    다음 OFF 탐색이 달을 넘길 때 재사용된다.
    """
    key = ("month", year, month)
    if key not in cache:
        my_month = get_my_issued_roster_service(
            year=year, month=month, current_user=current_user, db=db
        )
        snapshot = None
        if my_month:
            snapshot = _snapshot_cached(
                db, current_user, cache, year, month, _home_gid(db, current_user, cache)
            )
        cache[key] = (my_month, _shift_meta_by_code(snapshot))
    return cache[key]


def _meta_for_cell(
    db: Session,
    current_user,
    cache: dict,
    year: int,
    month: int,
    cell: dict,
    home_meta: dict[str, dict],
) -> dict[str, dict]:
    """그 **셀이 속한 병동** 기준 코드 메타. 홈 메타 위에 대상 병동 정의를 덮는다.

    ★★ 파견/병동이동 날은 셀의 코드가 **대상 병동 것**이라 홈 병동 `shifts` 에 없다.
      홈 메타만 보면 그 코드가 '정의를 모르는 코드' 로 떨어져 근무로도 OFF 로도
      세지지 않는다 — 다음 OFF 가 실제보다 뒤 날짜로 나오거나 연속근무일이 어긋나고,
      파견이 길면 `not_found` 가 된다.
    ★ 홈 병동 날이면 추가 조회 없이 홈 메타를 그대로 쓴다. 병동별로 캐시해
      같은 파견 병동을 여러 날 스캔해도 스냅샷은 한 번만 읽는다.
    """
    gid = cell.get("group_id")
    if not gid or gid == _home_gid(db, current_user, cache):
        return home_meta
    key = ("meta", year, month, gid)
    if key not in cache:
        snapshot = _snapshot_cached(db, current_user, cache, year, month, gid)
        # ★★ 대상 병동이 **미발행**이면 빈 메타를 돌려준다 — 홈 메타로 채우면 안 된다.
        #   그 경우 `get_my_issued_roster_service` 는 셀의 `group_id` 만 대상 병동으로
        #   바꾸고 **`code` 는 홈 병동 값을 그대로 남긴다**(미발행 분기가 code 를
        #   안 건드린다). 홈 메타를 씌우면 그 잔여 코드가 진짜 근무/OFF 로 해석돼
        #   **모르는 날을 안다고 답하게 된다** — 다음 OFF·연속근무일이 조용히 틀린다.
        #   빈 메타로 두면 호출부가 이미 '정의 모름' 으로 처리한다
        #   (`_code_detail` → `is_work: None`, `_next_off_from` → unknown).
        cache[key] = (
            {**home_meta, **_shift_meta_by_code(snapshot)} if snapshot else {}
        )
    return cache[key]


def _home_gid(db: Session, current_user, cache: dict) -> str | None:
    """본인 home 병동 id. 토큰이 아니라 DB 기준(그룹전환·소속변경 시에도 정합)."""
    if "home_gid" not in cache:
        cache["home_gid"] = resolve_home_group_id(db, current_user)
    return cache["home_gid"]


def _require_nurse_id(current_user) -> str:
    """간호사 계정만 통과. 관리자(ADM)는 nurses 행이 없어 본인 근무표가 성립하지 않는다."""
    nurse_id = getattr(current_user, "nurse_id", None)
    if not nurse_id:
        raise HTTPException(status_code=403, detail="간호사 계정만 조회할 수 있습니다.")
    return str(nurse_id)


def _latest_submitted_wanted(db: Session, nurse_id: str, month_str: str):
    """그 달 원티드 제출본(최신 1건). 없으면 None.

    판정 기준은 `preferences_service._latest_submitted_request` 와 동일하다
    (`is_submitted=1` 중 `submitted_at` 최신). 사설 헬퍼를 가져다 쓰지 않고 여기서
    다시 쓴 이유는 모듈 간 결합을 늘리지 않기 위해서다 — 기준이 바뀌면 양쪽을 함께 고친다.
    """
    return (
        db.query(WantedRequest)
        .filter(
            WantedRequest.nurse_id == nurse_id,
            WantedRequest.month == month_str,
            WantedRequest.is_submitted == True,  # noqa: E712
        )
        .order_by(WantedRequest.submitted_at.desc())
        .first()
    )


def _submitted_shift_requests(
    db: Session, nurse_id: str, request_id: int, year: int, month: int
) -> list:
    """제출본에 실린 그 달 선호(원티드) 항목. 날짜 오름차순."""
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return (
        db.query(NurseShiftRequest)
        .filter(
            NurseShiftRequest.nurse_id == nurse_id,
            NurseShiftRequest.request_id == request_id,
            NurseShiftRequest.shift_date >= start,
            NurseShiftRequest.shift_date < end,
        )
        .order_by(NurseShiftRequest.shift_date.asc())
        .all()
    )


def _is_granted(row, cell: dict, req_code: str, asg_code: str) -> bool:
    """요청 1건이 발행본에 반영됐는가. **같은 근무코드여야** 반영이다(D 요청에 D1 은 아님).

    ★★ 판정 키는 코드 문자열이 아니라 **`shifts.id`** 다. `shifts` 는 PK 가 없고
      코드 문자열이 실제로 교체된다(실측 5건: 같은 `shifts.id` 에 `N`→`N1`,
      `O`→`OFF`, `D`→`Dㅇ`). 제출과 발행 사이에 개명되면 같은 근무인데 문자열이
      달라 **미반영으로 잡힌다.** 요청은 `shifts_table_id`, 발행본 셀은
      `schedule_ids`(이름과 달리 담긴 값이 `shifts.id` 다)로 같은 행을 가리킨다.
    ★★ 단, id 비교는 **같은 병동일 때만** 유효하다. `shifts.id` 는 병동 간 유일하지
      않다(실측: id 1874 가 동탄시티 'OFF' 와 시화 '반반반' 양쪽에 있다). 파견 나간
      날은 셀이 다른 병동 id 를 담고 있어, 그대로 비교하면 우연히 같은 번호에
      **거짓 반영**이 난다. 그런 날은 코드 문자열로 떨어뜨린다.
    """
    if not asg_code:
        return False
    req_tid = getattr(row, "shifts_table_id", None)
    cell_tid = cell.get("schedule_id")
    same_ward = bool(cell.get("group_id")) and cell.get("group_id") == getattr(row, "group_id", None)
    if same_ward and req_tid is not None and cell_tid is not None:
        return int(req_tid) == int(cell_tid)
    return asg_code == req_code


def _reflection_entries(
    db: Session,
    current_user,
    cache: dict,
    requests: list,
    my_month: dict,
    home_meta: dict[str, dict],
    year: int,
    month: int,
) -> list[dict]:
    """요청 행 → 반영 판정 엔트리. 판정 규칙은 `_is_granted` 참조.

    ★ 이름·색은 **그 항목을 소유한 병동** 기준으로 뽑는다 — 요청은 `row.group_id`,
      배정은 셀의 `group_id`. 홈 메타 하나로 둘 다 그리면 파견/병동이동 날에
      ① 대상 병동 전용 코드가 색 없는 raw 코드로 뜨고 ② **양쪽에 다 있는 코드는
      홈 병동의 이름·색으로 잘못 칠해진다**(달력에서 다른 근무처럼 보인다).
      `granted` 판정 자체는 `_is_granted` 가 병동을 가리므로 이 문제와 무관하다.
    """
    out: list[dict] = []
    for row in requests:
        req_date = row.shift_date
        cell = _cell_of_day(my_month, req_date.day)
        req_code = str(row.shift or "").strip()
        asg_code = _raw_cell_code(cell)
        req_meta = _meta_for_cell(
            db, current_user, cache, year, month,
            {"group_id": getattr(row, "group_id", None)}, home_meta,
        )
        asg_meta = _meta_for_cell(db, current_user, cache, year, month, cell, home_meta)
        # 파견지 미발행 날은 셀의 코드가 홈 병동 잔여값이다. 그대로 쓰면 문자열 폴백이
        # **반영으로 오판**하고(요청도 홈 코드라 곧잘 맞는다) 배정까지 표시돼 반영률이
        # 부풀려진다. 알 수 없는 날은 배정 없음(`assigned: null`)·미반영으로 둔다.
        unknown = _cell_is_unknown(asg_code, asg_meta)
        shown_code = "" if unknown else asg_code
        out.append({
            "date": req_date.isoformat(),
            "requested": _code_view(req_code, req_meta),
            "assigned": _code_view(shown_code, asg_meta) if shown_code else None,
            "granted": _is_granted(row, cell, req_code, shown_code),
            "unknown": unknown,
            "comment": row.comment or None,
        })
    return out


def _reflection_summary(entries: list[dict]) -> dict:
    """반영률 집계.

    ★ 요청 0건이면 `rate` 는 `null` 이다. `0.0` 을 내리면 화면이 '0% 반영' 으로
      읽는데, 애초에 낸 게 없는 것과 전부 거절된 것은 전혀 다른 상태다.
    ★★ 판정 불가(파견지 미발행)는 **분모에서 뺀다**. `rejected` 로 세면 간호사에게
      "요청이 반려됐다" 고 잘못 말하는 셈이다 — 실제로는 아직 아무도 판단하지 않았다.
      대신 `unknown` 으로 따로 세어 화면이 "n건은 판정 대기" 를 말할 수 있게 한다.
      `total = granted + rejected + unknown` 이 항상 성립한다.
    """
    total = len(entries)
    unknown = sum(1 for e in entries if e.get("unknown"))
    granted = sum(1 for e in entries if e["granted"])
    judged = total - unknown
    return {
        "total": total,
        "granted": granted,
        "rejected": judged - granted,
        "unknown": unknown,
        "rate": round(granted * 100 / judged, 1) if judged else None,
    }


def get_my_wanted_reflection_service(
    current_user,
    db: Session,
    year: int | None = None,
    month: int | None = None,
) -> dict:
    """본인이 제출한 원티드가 **발행(마감) 근무표**에 얼마나 반영됐는지.

    ★ 기피(`banned_wanted_entries`)는 분모에 넣지 않는다. 그 테이블은 제출 스냅샷 축이
      없어(간호사·병동·연월당 1행) '그때 낸 기피' 를 복원할 수 없다 — 넣으면 지난달
      수치가 **지금** 기피 설정에 따라 흔들린다. 선호(`nurse_shift_requests`)만 센다.
    ★ 미발행·미제출은 오류가 아니다. 404 를 쓰면 CloudFront 가 `/api/*` 404 를
      `index.html` **200** 으로 바꿔 보내 모바일이 HTML 을 JSON 으로 파싱하다 하얗게
      뜬다(`/issued_roster/me` 주석 참조). 200 + 상태 필드로 내린다.

    Args:
        year, month: 생략하면 **지난달**. 홈 카드는 생략, 근무표 화면은 보고 있는 달을 지정.
    """
    if year is None or month is None:
        prev = date.today().replace(day=1) - timedelta(days=1)
        year, month = prev.year, prev.month

    nurse_id = _require_nurse_id(current_user)
    result = {
        "year": year, "month": month,
        "issued": False, "submitted": False, "submitted_at": None,
        "summary": None, "entries": [],
    }

    # ★ 발행 여부를 **제출 여부보다 먼저** 확정한다. 순서를 뒤집으면 미제출자에게
    #   `issued: false` 가 나가는데, 근무표는 나왔고 원티드만 안 낸 상태와 근무표가
    #   아직 안 나온 상태가 같은 값이 된다. 프론트가 "근무표 준비 중" 을 잘못 띄운다
    #   (실측: 김지영 2026-08 은 발행본이 있는데 미제출이라 false 로 나갔다).
    #   두 플래그는 서로 독립이므로 각각 정확해야 한다.
    cache: dict = {}
    my_month, meta = _month_view(db, current_user, cache, year, month)
    result["issued"] = my_month is not None

    submitted = _latest_submitted_wanted(db, nurse_id, f"{year}-{month:02d}")
    if not submitted:
        return result
    result["submitted"] = True
    result["submitted_at"] = submitted.submitted_at
    if not my_month:
        return result

    entries = _reflection_entries(
        db, current_user, cache,
        _submitted_shift_requests(db, nurse_id, submitted.request_id, year, month),
        my_month, meta, year, month,
    )
    result["entries"] = entries
    result["summary"] = _reflection_summary(entries)
    return result


def _nurse_profiles(db: Session, snapshot: dict | None) -> dict[str, dict]:
    """`{nurse_id: 최소 프로필}` — 스냅샷을 바탕으로 **사람 속성은 라이브 값으로 덮는다.**

    ★ 근무 배정은 그 시점 사실이라 스냅샷이 정본이지만, 이름·경력 같은 **사람 속성은
      현재 값이 맞다**. 화면에서 "이 사람 몇 년차야" 는 지금 기준으로 묻는 질문이다.
    ★★ 스냅샷 값만 쓰면 실제로 빈다 — 실측(snapshot 300, 2026-08-03 발행): 송혜영은
      `nurses.experience=30` 인데 `nurses_json.experience` 는 **null** 이다. 발행 당시
      비어 있었고 나중에 채워졌기 때문이다. 스냅샷만 보면 경력이 영영 안 나온다.
    ★ 라이브 조회는 한 번의 `IN` 이다. 간호사마다 조회하면 병동 인원만큼 쿼리가 는다.
    ★ 연락처·생년월일·이메일은 싣지 않는다. 같은 병동이라도 근무 확인 화면이
      개인정보 조회 창구가 되면 안 된다.
    ★ `experience` 가 끝내 없으면 `None` 그대로 둔다. 0 으로 눕히면 화면이
      '0년차' 로 읽는데 실제로는 미입력이다.
    """
    out: dict[str, dict] = {}
    for row in (snapshot or {}).get("nurses") or []:
        nurse_id = str(row.get("nurse_id") or "")
        if not nurse_id:
            continue
        out[nurse_id] = {
            "name": row.get("name") or "",
            "experience": row.get("experience"),
            "role": row.get("role"),
            "is_head_nurse": bool(row.get("is_head_nurse")),
            "sequence": int(row.get("sequence") or 0),
        }
    if not out:
        return out

    live = db.query(
        Nurse.nurse_id, Nurse.name, Nurse.experience, Nurse.role,
        Nurse.is_head_nurse, Nurse.sequence,
    ).filter(Nurse.nurse_id.in_(list(out.keys()))).all()
    for row in live:
        profile = out.get(str(row.nurse_id))
        if profile is None:
            continue
        # 라이브에 값이 있을 때만 덮는다 — 퇴사 등으로 비워진 값이 스냅샷을 지우지 않게.
        if row.name:
            profile["name"] = row.name
        if row.experience is not None:
            profile["experience"] = row.experience
        if row.role:
            profile["role"] = row.role
        profile["is_head_nurse"] = bool(row.is_head_nurse)
        profile["sequence"] = int(row.sequence or 0)
    return out


def _coworkers_of_day(
    db: Session,
    snapshot: dict | None,
    meta: dict[str, dict],
    my_nurse_id: str,
    day: int,
    my_code: str,
) -> list[dict]:
    """같은 날 **같은 시간대**(`default_shift`)로 배정된 동료. 본인 제외.

    ★ 정확일치가 아니라 시간대로 묶는다. 병동마다 `D`/`D1`/`반반` 같은 파생코드가
      있어 코드로 묶으면 실제로 붙어 일하는 사람이 목록에서 빠진다.
    ★ 본인이 OFF·휴가·미배정이면 빈 목록이다 — 같이 일하는 사람이 없다.
    """
    my_meta = meta.get(my_code) or {}
    if not my_code or not my_meta.get("is_work"):
        return []
    my_slot = my_meta.get("default_shift") or my_code

    profiles = _nurse_profiles(db, snapshot)
    out: list[dict] = []
    for row in ((snapshot or {}).get("roster") or {}).get("nurses") or []:
        nurse_id = str(row.get("nurse_id") or "")
        if not nurse_id or nurse_id == my_nurse_id:
            continue
        cells = row.get("schedule") or []
        code = _raw_cell_code(cells[day - 1] if 0 < day <= len(cells) else None)
        cell_meta = meta.get(code) or {}
        if not code or not cell_meta.get("is_work"):
            continue
        if (cell_meta.get("default_shift") or code) != my_slot:
            continue
        profile = profiles.get(nurse_id, {})
        out.append({
            "nurse_id": nurse_id,
            "name": profile.get("name") or row.get("name") or "",
            "experience": profile.get("experience"),
            "role": profile.get("role"),
            "is_head_nurse": bool(profile.get("is_head_nurse")),
            "shift_code": code,
            "shift_name": cell_meta.get("name") or code,
            "_seq": profile.get("sequence", 0),
        })
    out.sort(key=lambda r: (r["_seq"], r["name"]))
    for row in out:
        row.pop("_seq", None)
    return out


def _next_off_result(status: str, day, days_until, work_days: int, code) -> dict:
    """다음 OFF 응답 한 형태로 고정 — 상태만 달라지고 키는 항상 같다."""
    return {
        "status": status,
        "date": day.isoformat() if day else None,
        "days_until": days_until,
        "consecutive_work_days": work_days,
        "code": code,
    }


def _next_off_from(db: Session, current_user, cache: dict, target: date) -> dict:
    """기준일부터 앞으로 스캔해 첫 OFF 를 찾는다.

    ★ 달을 넘기면 **다음 달 발행본**을 한 번 더 읽는다(`get_my_issued_week_service`
      가 주 경계에서 쓰는 것과 같은 패턴). 다음 달이 아직 미발행이면 알 수 없으므로
      `unknown_not_issued` 로 내린다 — `null`/`0` 으로 눕히면 'OFF 가 없다' 와
      구분이 사라진다.
    ★ 코드가 비었거나(미배정) 스냅샷에 정의가 없는 코드는 근무로도 OFF 로도 세지
      않고 지나간다. 모르는 것을 근무로 세면 연속근무일이 부풀려진다.
    ★ 코드 정의는 **그 날 셀이 속한 병동** 기준이다(`_meta_for_cell`). 파견 날은
      홈 병동 `shifts` 에 없는 코드가 오므로 홈 메타만 보면 통째로 건너뛴다.
    ★ 상한을 `date.max` 로도 자른다. 안 자르면 `date=9999-12-31` 같은 유효 입력에서
      `timedelta` 덧셈이 OverflowError 를 내고, 라우터의 포괄 except 가 그것을
      **500** 으로 바꾼다 — 클라이언트 입력이 서버 장애로 보고되는 셈이다.
    """
    work_days = 0
    max_offset = min(_NEXT_OFF_HORIZON_DAYS, (date.max - target).days)
    for offset in range(max_offset + 1):
        day = target + timedelta(days=offset)
        my_month, home_meta = _month_view(db, current_user, cache, day.year, day.month)
        if my_month is None:
            return _next_off_result("unknown_not_issued", None, None, work_days, None)
        cell = _cell_of_day(my_month, day.day)
        code = _raw_cell_code(cell)
        meta = _meta_for_cell(db, current_user, cache, day.year, day.month, cell, home_meta)
        # 파견지 미발행 날 — 배정이 있는데도 근무인지 OFF 인지 알 수 없다. 건너뛰고
        # 계속 세면 그 뒤 연속근무일이 통째로 틀리므로 여기서 '모름' 으로 끊는다.
        if _cell_is_unknown(code, meta):
            return _next_off_result("unknown_not_issued", None, None, work_days, None)
        cell_meta = meta.get(code)
        if not code or not cell_meta:
            continue
        if not cell_meta.get("is_work"):
            status = "today_is_off" if offset == 0 else "found"
            return _next_off_result(status, day, offset, work_days, code)
        work_days += 1
    return _next_off_result("not_found", None, None, work_days, None)


def get_my_today_service(
    current_user,
    db: Session,
    base_date: date | None = None,
    include_coworkers: bool = True,
    include_next_off: bool = True,
) -> dict:
    """오늘(또는 지정일) 본인 근무 + 같은 시간대 동료 + 다음 OFF 까지 남은 일수.

    ★ 그 날 소속 병동은 토큰이 아니라 **본인 근무표 셀의 `group_id`** 로 정한다.
      파견/병동이동 중이면 그 날은 다른 병동이고 동료도 그쪽에서 찾아야 한다.
      셀은 `get_my_issued_roster_service` 가 overlay 를 이미 적용해 돌려준 값이다.
    ★ 미발행은 오류가 아니다 — `issued: false` + `my_shift: null` 로 내린다.
    """
    target = base_date or date.today()
    nurse_id = _require_nurse_id(current_user)

    cache: dict = {}
    my_month, home_meta = _month_view(db, current_user, cache, target.year, target.month)
    cell = _cell_of_day(my_month, target.day)
    code = _raw_cell_code(cell)
    day_gid = cell.get("group_id") or _home_gid(db, current_user, cache)

    # 코드 메타는 그 날 병동 기준(`_meta_for_cell`) — 다음 OFF 스캔과 같은 규칙을 쓴다.
    meta = _meta_for_cell(db, current_user, cache, target.year, target.month, cell, home_meta)
    day_snapshot = _snapshot_cached(db, current_user, cache, target.year, target.month, day_gid)

    # 파견지 미발행 날은 셀에 홈 병동 잔여 코드가 남아 있다. 그걸 `my_shift` 로 내보내면
    # 실제로는 모르는 날에 화면이 D/E/N/O 를 단정해 보여준다. 알 수 없으면 비운다.
    unknown = _cell_is_unknown(code, meta)
    result = {
        "date": target.isoformat(),
        "issued": my_month is not None,
        "group_id": day_gid,
        "group_name": cell.get("group_name") or "",
        # 파견/병동이동이면 사유. `my_shift` 가 비어도 화면이 이유를 말할 수 있다.
        "reason": cell.get("reason"),
        "shift_unknown": unknown,
        "my_shift": None if unknown else (_code_detail(code, meta) if code else None),
    }
    if include_coworkers:
        result["coworkers"] = _coworkers_of_day(
            db, day_snapshot, meta, nurse_id, target.day, code
        )
    if include_next_off:
        result["next_off"] = _next_off_from(db, current_user, cache, target)
    return result


def create_issued_roster_snapshot(
    schedule: Schedule,
    current_user,
    year: int,
    month: int,
    office_id: str,
    group_id: str,
    db: Session,
) -> IssuedRosterSnapshot:
    """
    근무표 발행 시점의 스냅샷 레코드를 생성합니다.

    DB 세션에는 추가만 수행하고, 커밋은 호출자가 직접 처리하도록 합니다.
    """
    # 동일 그룹/연월의 기존 발행 스냅샷 is_active_issued 플래그 비활성화
    (
        db.query(IssuedRosterSnapshot)
        .filter(
            IssuedRosterSnapshot.office_id == office_id,
            IssuedRosterSnapshot.group_id == group_id,
            IssuedRosterSnapshot.year == schedule.year,
            IssuedRosterSnapshot.month == schedule.month,
            IssuedRosterSnapshot.is_active_issued == True,
        )
        .update(
            {"is_active_issued": False},
            synchronize_session=False,
        )
    )
    # 메타 정보 구성
    _main_group_row = (
        db.query(Group).filter(Group.group_id == group_id).first()
        if group_id
        else None
    )
    _main_group_name = (
        _main_group_row.group_name if _main_group_row else ""
    ) or ""
    meta_json: dict = {
        "office_id": office_id,
        "group_id": group_id,
        "group_name": _main_group_name,
        "schedule_id": schedule.schedule_id,
        "year": schedule.year,
        "month": schedule.month,
        "version": schedule.version,
        "schedule_name": schedule.name,
        "memo": schedule.memo,
        "issued_by_nurse_id": getattr(current_user, "nurse_id", None),
        "issued_by_account_id": getattr(current_user, "account_id", None),
    }

    # 설정 스냅샷 구성 (RosterConfig)
    config_json = None
    if schedule.config_id:
        cfg = (
            db.query(RosterConfigModel)
            .filter(RosterConfigModel.config_id == schedule.config_id)
            .first()
        )
        if cfg:
            config_json = {
                "config_id": cfg.config_id,
                "office_id": cfg.office_id,
                "group_id": cfg.group_id,
                "day_req": cfg.day_req,
                "eve_req": cfg.eve_req,
                "nig_req": cfg.nig_req,
                "min_exp_per_shift": cfg.min_exp_per_shift,
                "req_exp_nurses": cfg.req_exp_nurses,
                "two_offs_per_week": cfg.two_offs_per_week,
                "max_nig_per_month": cfg.max_nig_per_month,
                "three_seq_nig": cfg.three_seq_nig,
                "two_offs_after_three_nig": cfg.two_offs_after_three_nig,
                "two_offs_after_two_nig": cfg.two_offs_after_two_nig,
                "banned_day_after_eve": cfg.banned_day_after_eve,
                "max_conseq_work": cfg.max_conseq_work,
                "off_days": cfg.off_days,
                "shift_priority": cfg.shift_priority,
                "sequential_offs": cfg.sequential_offs,
                "nod_noe": cfg.nod_noe,
                "preceptee_on": cfg.preceptee_on,
                "preceptee_shift_count": cfg.preceptee_shift_count,
                "created_at": cfg.created_at.isoformat()
                if getattr(cfg, "created_at", None)
                else None,
            }

    # 간호사 리스트 및 정보 스냅샷 (인바운드 포함)
    nurses = list(
        db.query(Nurse)
        .filter(Nurse.group_id == group_id)
        .order_by(Nurse.experience.desc(), Nurse.nurse_id.asc())
        .all()
    )
    # 인바운드 간호사: schedule_entries에 존재하지만 group에 없는 간호사
    _group_nids = {n.nurse_id for n in nurses}
    _entry_nids = {
        row.nurse_id for row in
        db.query(ScheduleEntry.nurse_id)
        .filter(ScheduleEntry.schedule_id == schedule.schedule_id)
        .distinct()
        .all()
    }
    _inbound_nids = _entry_nids - _group_nids
    if _inbound_nids:
        _inbound_nurses = (
            db.query(Nurse)
            .filter(Nurse.nurse_id.in_(_inbound_nids))
            .all()
        )
        nurses.extend(_inbound_nurses)

    # inbound assignment 블록 스냅샷 (파견/병동이동 이력 포함)
    _all_nids = [n.nurse_id for n in nurses]
    _inbound_blocks: dict[str, dict] = {}
    if _all_nids:
        _asg_rows = (
            db.query(NurseAssignment)
            .filter(
                NurseAssignment.nurse_id.in_(_all_nids),
                NurseAssignment.target_group_id == group_id,
                NurseAssignment.status == "active",
                NurseAssignment.reason.in_(("파견", "병동이동")),
            )
            .order_by(NurseAssignment.start_date.asc())
            .all()
        )
        if _asg_rows:
            _gid_set: set[str] = set()
            for r in _asg_rows:
                if r.target_group_id:
                    _gid_set.add(r.target_group_id)
                if r.source_group_id:
                    _gid_set.add(r.source_group_id)
            _name_map: dict[str, str] = {}
            if _gid_set:
                for gid, gname in (
                    db.query(Group.group_id, Group.group_name)
                    .filter(Group.group_id.in_(_gid_set))
                    .all()
                ):
                    _name_map[gid] = gname or ""
            for r in _asg_rows:
                if r.source_group_id == group_id:
                    continue
                entry = {
                    "startDate": r.start_date.isoformat() if r.start_date else None,
                    "endDate": r.expected_end_date.isoformat() if r.expected_end_date else None,
                    "reason": r.reason,
                    "target_group_id": r.target_group_id,
                    "target_group_name": _name_map.get(r.target_group_id, ""),
                    "source_group_id": r.source_group_id,
                    "source_group_name": _name_map.get(r.source_group_id, ""),
                }
                _inbound_blocks.setdefault(r.nurse_id, {"inbound_list": []})["inbound_list"].append(entry)

    # 간호사별 group_name 조회 (인바운드 소스 그룹 포함)
    _nurse_gids = {n.group_id for n in nurses if n.group_id}
    _nurse_group_name_map: dict[str, str] = {}
    if _nurse_gids:
        for gid, gname in (
            db.query(Group.group_id, Group.group_name)
            .filter(Group.group_id.in_(_nurse_gids))
            .all()
        ):
            _nurse_group_name_map[gid] = gname or ""
    if group_id and group_id not in _nurse_group_name_map:
        _nurse_group_name_map[group_id] = _main_group_name

    nurses_json = []
    for n in nurses:
        _block = _inbound_blocks.get(n.nurse_id)
        nurses_json.append(
            {
                "nurse_id": n.nurse_id,
                "group_id": n.group_id,
                "group_name": _nurse_group_name_map.get(n.group_id, ""),
                "office_id": n.office_id,
                "account_id": n.account_id,
                "emp_num": n.emp_num,
                "name": n.name,
                "experience": n.experience,
                "role": n.role,
                "level_": n.level_,
                "is_head_nurse": n.is_head_nurse,
                "emp_auth_gbn": n.emp_auth_gbn,
                "allowed_shifts": n.allowed_shifts,
                "personal_off_adjustment": n.personal_off_adjustment,
                "preceptor_id": n.preceptor_id,
                "joining_date": n.joining_date.isoformat()
                if getattr(n, "joining_date", None)
                else None,
                "resignation_date": n.resignation_date.isoformat()
                if getattr(n, "resignation_date", None)
                else None,
                "sequence": n.sequence,
                "active": n.active,
                "team_id": n.team_id,
                "is_inbound": n.group_id != group_id,
                "inbound": list(_block.get("inbound_list", [])) if _block else [],
            }
        )

    # 근무표(로스터) 스냅샷
    days_in_month = calendar.monthrange(schedule.year, schedule.month)[1]

    # 시프트 메타데이터 전체 스냅샷
    shift_rows = db.query(Shift).filter(Shift.group_id == group_id).all()
    shifts_json = [
        {
            "shift_id": s.shift_id,
            "office_id": s.office_id,
            "group_id": s.group_id,
            "name": s.name,
            "color": s.color,
            "start_time": _to_time_str(s.start_time),
            "end_time": _to_time_str(s.end_time),
            "type": s.type,
            "allday": s.allday,
            "auto_schedule": s.auto_schedule,
            "duration": s.duration,
            "sequence": s.sequence,
            "default_shift": s.default_shift,
            "id": s.id,
        }
        for s in shift_rows
    ]
    shift_colors = {s.shift_id: s.color for s in shift_rows}

    entries = (
        db.query(ScheduleEntry)
        .filter(ScheduleEntry.schedule_id == schedule.schedule_id)
        .all()
    )
    # shifts.id → 현재 shift_id 매핑 (schedule_entries의 shift_id가 구 코드일 수 있으므로)
    _int_id_to_shift_id: dict[int, str] = {
        s.id: s.shift_id for s in shift_rows
    }

    entries_by_nurse: dict = {}
    entry_ids_by_nurse: dict = {}
    for entry in entries:
        nurse_id = entry.nurse_id
        day = entry.work_date.day
        if nurse_id not in entries_by_nurse:
            entries_by_nurse[nurse_id] = {}
            entry_ids_by_nurse[nurse_id] = {}
        # entry.id(shifts.id)가 있으면 현재 shift_id로 복원, 없으면 기존 값 사용
        if entry.id and entry.id in _int_id_to_shift_id:
            entries_by_nurse[nurse_id][day] = _int_id_to_shift_id[entry.id]
        else:
            entries_by_nurse[nurse_id][day] = entry.shift_id
        entry_ids_by_nurse[nurse_id][day] = entry.id

    roster_nurses = []
    for n in nurses:
        schedule_list = []
        for day in range(1, days_in_month + 1):
            code = entries_by_nurse.get(n.nurse_id, {}).get(day, "-")
            schedule_list.append({
                "code": code,
                "color": shift_colors.get(code, ""),
            })
        schedule_ids = [
            entry_ids_by_nurse.get(n.nurse_id, {}).get(day)
            for day in range(1, days_in_month + 1)
        ]
        counts = {
            shift_id: sum(1 for item in schedule_list if item["code"] == shift_id)
            for shift_id in shift_colors.keys()
        }
        roster_nurses.append(
            {
                "nurse_id": n.nurse_id,
                "name": n.name,
                "experience": n.experience,
                "schedule": schedule_list,
                "schedule_ids": schedule_ids,
                "counts": counts,
            }
        )

    roster_json = {
        "year": schedule.year,
        "month": schedule.month,
        "days_in_month": days_in_month,
        "shift_colors": shift_colors,
        "nurses": roster_nurses,
    }

    # 시프트 관리(ShiftManage) 스냅샷 - RN 포함 전체 클래스 저장
    shift_manage_rows = (
        db.query(ShiftManage)
        .filter(
            ShiftManage.office_id == office_id,
            ShiftManage.group_id == group_id,
        )
        .order_by(ShiftManage.nurse_class.asc(), ShiftManage.shift_slot.asc())
        .all()
    )
    shift_manage_json = [
        {
            "nurse_class": sm.nurse_class,
            "shift_slot": sm.shift_slot,
            "main_code": sm.main_code,
            "codes": sm.codes if sm.codes else [],
            "manpower": sm.manpower,
        }
        for sm in shift_manage_rows
    ]

    # 위반사항은 우선 빈 구조로 저장하고, 이후 검증 로직 연동 시 확장합니다.
    violations_json: dict = {
        "messages": [],
        "details": [],
    }

    snapshot = IssuedRosterSnapshot(
        office_id=office_id,
        group_id=group_id,
        schedule_id=schedule.schedule_id,
        version=schedule.version,
        is_active_issued=True,
        meta_json=meta_json,
        config_json=config_json,
        nurses_json=nurses_json,
        shifts_json=shifts_json,
        shift_manage_json=shift_manage_json,
        roster_json=roster_json,
        violations_json=violations_json,
        year=schedule.year,
        month=schedule.month,
    )
    return snapshot



def _share_now() -> datetime:
    return datetime.now()



def _share_build_s3_client(region: str):
    import os
    import boto3

    access_key = os.getenv("AWS_ACCESS_KEY_ID") or os.getenv("AWS_ACCESS_KEY")
    secret_key = os.getenv("AWS_SECRET_ACCESS_KEY") or os.getenv("AWS_SECRET_KEY")
    session_token = os.getenv("AWS_SESSION_TOKEN")
    profile_name = os.getenv("AWS_PROFILE")

    if access_key and secret_key:
        return boto3.client(
            "s3",
            region_name=region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            aws_session_token=session_token,
        )

    if profile_name:
        session = boto3.Session(profile_name=profile_name, region_name=region)
        creds = session.get_credentials()
        if not creds or not creds.access_key or not creds.secret_key:
            raise ValueError("AWS credentials not found for AWS_PROFILE")
        return session.client("s3")

    session = boto3.Session(region_name=region)
    creds = session.get_credentials()
    if not creds or not creds.access_key or not creds.secret_key:
        raise ValueError("AWS credentials are missing. Set AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY or AWS_PROFILE")
    return session.client("s3")


def _share_public_base_url(fallback_base_url: str) -> str:
    import os

    configured = os.getenv("SHARE_PUBLIC_BASE_URL")
    if configured and str(configured).strip():
        return str(configured).strip().rstrip("/")
    return (fallback_base_url or "").rstrip("/")


def _share_fetch_s3_image_bytes(image_url: str) -> tuple[bytes, str]:
    import os
    from urllib.parse import urlparse

    parsed = urlparse(str(image_url or "").strip())
    if not parsed.scheme or not parsed.netloc:
        raise ValueError("invalid image_url")

    object_key = parsed.path.lstrip("/")
    if not object_key:
        raise ValueError("invalid image_url path")

    region = os.getenv("AWS_REGION", "ap-northeast-2")
    bucket_name = parsed.netloc.split(".s3.")[0] if ".s3." in parsed.netloc else os.getenv("AWS_SHARE_S3_BUCKET") or os.getenv("SHARE_S3_BUCKET") or os.getenv("S3_SHARE_BUCKET")
    if not bucket_name:
        raise ValueError("share S3 bucket env is not configured")

    s3_client = _share_build_s3_client(region)
    obj = s3_client.get_object(Bucket=bucket_name, Key=object_key)
    content_type = str(obj.get("ContentType") or "image/png")
    image_bytes = obj["Body"].read()
    return image_bytes, content_type


def _share_resolve_target_scope(current_user, db: Session, override_group_id: str | None = None):
    # 그룹 스코프 단일 해석점(resolve_effective_group) 사용.
    # 토큰 group_id 가 아니라 nurse_id→DB + groups.hn_id 로 해석한다
    # (2026-06 토큰→DB 그룹스코프 마이그와 정합. 프론트가 group_id 를 상시 전송하므로
    #  일반/HN 도 관리 그룹이면 본인 근무표 공유 가능, ADM 은 office 내 임의 그룹 지정 가능).
    from fastapi import HTTPException
    from services.group_access import resolve_effective_group

    try:
        target_group_id = resolve_effective_group(
            db, current_user, override_group_id, require_group=True
        )
    except HTTPException as e:
        if e.status_code == 403:
            raise PermissionError(e.detail)
        if e.status_code == 404:
            raise LookupError(e.detail)
        raise ValueError(e.detail)

    group_row = db.query(Group).filter(Group.group_id == target_group_id).first()
    if not group_row:
        raise LookupError("Group not found")
    return group_row.group_id, group_row.office_id




def _share_build_object_prefix(office_id: str, group_id: str, nurse_id: str | None, year: int, month: int) -> str:
    safe_office_id = str(office_id or "unknown")
    safe_group_id = str(group_id or "unknown")
    safe_nurse_id = str(nurse_id or "unknown")
    return f"og-images/{safe_office_id}/{safe_group_id}/{safe_nurse_id}/{int(year):04d}/{int(month):02d}"


def _share_find_by_token(db: Session, token: str) -> dict | None:
    from db.models import ShareLink

    row = db.query(ShareLink).filter(ShareLink.token == token).first()
    if not row:
        return None
    return {
        "token": row.token,
        "schedule_id": row.schedule_id,
        "office_id": row.office_id,
        "group_id": row.group_id,
        "image_url": row.image_url,
        "title": row.title,
        "description": row.description,
        "created_by_nurse_id": row.created_by_nurse_id,
        "expires_at": row.expires_at,
        "revoked_at": row.revoked_at,
        "created_at": row.created_at,
    }


def create_schedule_share_link_service(
    db: Session,
    current_user,
    schedule_id: str,
    fallback_base_url: str,
    image_url: str,
    title: str | None,
    description: str | None,
    expires_in_days: int,
    override_group_id: str | None = None,
) -> dict:
    import secrets
    from datetime import timedelta

    target_group_id, target_office_id = _share_resolve_target_scope(
        current_user=current_user,
        db=db,
        override_group_id=override_group_id,
    )

    schedule = db.query(Schedule).filter(
        Schedule.schedule_id == schedule_id,
        Schedule.group_id == target_group_id,
        Schedule.office_id == target_office_id,
    ).first()
    if not schedule:
        raise LookupError("Schedule not found for your scope")

    if not image_url or not str(image_url).strip():
        raise ValueError("image_url is required")
    image_url = str(image_url).strip()

    token = secrets.token_hex(24)
    try:
        expires_days = max(1, min(int(expires_in_days), 365))
    except (TypeError, ValueError):
        raise ValueError("expires_in_days must be integer")
    expires_at = _share_now() + timedelta(days=expires_days)
    now = _share_now()

    from db.models import ShareLink

    share_row = ShareLink(
        token=token,
        schedule_id=schedule_id,
        office_id=target_office_id,
        group_id=target_group_id,
        image_url=image_url,
        title=title,
        description=description,
        created_by_nurse_id=getattr(current_user, "nurse_id", None),
        expires_at=expires_at,
        created_at=now,
        updated_at=now,
    )
    db.add(share_row)
    db.commit()

    base_url = _share_public_base_url(fallback_base_url)
    return {
        "token": token,
        "share_url": f"{base_url}/roster/s/{token}",
        "image_url": f"{base_url}/roster/s/{token}/image",
        "expires_at": expires_at,
        "schedule_id": schedule_id,
        "group_id": target_group_id,
        "office_id": target_office_id,
    }



def upload_schedule_share_image_and_create_link_service(
    db: Session,
    current_user,
    schedule_id: str,
    fallback_base_url: str,
    image_file,
    title: str | None,
    description: str | None,
    expires_in_days: int,
    override_group_id: str | None = None,
) -> dict:
    import os
    import secrets
    import boto3

    allowed_types = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/webp": ".webp",
    }
    content_type = str(getattr(image_file, "content_type", "") or "").lower()
    if content_type not in allowed_types:
        raise ValueError("지원하지 않는 이미지 형식입니다. (png, jpg, jpeg, webp)")

    image_bytes = image_file.file.read()
    if not image_bytes:
        raise ValueError("image file is empty")
    if len(image_bytes) > 5 * 1024 * 1024:
        raise ValueError("image file size must be <= 5MB")

    bucket_name = os.getenv("AWS_SHARE_S3_BUCKET") or os.getenv("SHARE_S3_BUCKET") or os.getenv("S3_SHARE_BUCKET")
    if not bucket_name:
        raise ValueError("share S3 bucket env is not configured")
    region = os.getenv("AWS_REGION", "ap-northeast-2")

    target_group_id, target_office_id = _share_resolve_target_scope(
        current_user=current_user,
        db=db,
        override_group_id=override_group_id,
    )
    schedule = db.query(Schedule).filter(
        Schedule.schedule_id == schedule_id,
        Schedule.group_id == target_group_id,
        Schedule.office_id == target_office_id,
    ).first()
    if not schedule:
        raise LookupError("Schedule not found for your scope")

    ext = allowed_types[content_type]
    object_prefix = _share_build_object_prefix(
        office_id=target_office_id,
        group_id=target_group_id,
        nurse_id=getattr(current_user, "nurse_id", None),
        year=int(schedule.year),
        month=int(schedule.month),
    )
    object_key = f"{object_prefix}/{secrets.token_hex(16)}{ext}"

    try:
        s3_client = _share_build_s3_client(region)
        s3_client.put_object(
            Bucket=bucket_name,
            Key=object_key,
            Body=image_bytes,
            ContentType=content_type,
            CacheControl="max-age=31536000",
        )
    except Exception as e:
        raise RuntimeError(f"S3 upload failed: {str(e)}")

    image_url = f"https://{bucket_name}.s3.{region}.amazonaws.com/{object_key}"

    return create_schedule_share_link_service(
        db=db,
        current_user=current_user,
        schedule_id=schedule_id,
        fallback_base_url=fallback_base_url,
        image_url=image_url,
        title=title,
        description=description,
        expires_in_days=expires_in_days,
        override_group_id=override_group_id,
    )



def auto_generate_schedule_share_image_and_create_link_service(
    db: Session,
    current_user,
    schedule_id: str,
    fallback_base_url: str,
    title: str | None,
    description: str | None,
    expires_in_days: int,
    override_group_id: str | None = None,
) -> dict:
    import os
    import io
    import secrets
    import calendar
    import boto3
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from db.models import Nurse, ScheduleEntry

    target_group_id, target_office_id = _share_resolve_target_scope(
        current_user=current_user,
        db=db,
        override_group_id=override_group_id,
    )

    schedule = db.query(Schedule).filter(
        Schedule.schedule_id == schedule_id,
        Schedule.group_id == target_group_id,
        Schedule.office_id == target_office_id,
    ).first()
    if not schedule:
        raise LookupError("Schedule not found for your scope")

    year = int(schedule.year)
    month = int(schedule.month)
    days_in_month = calendar.monthrange(year, month)[1]

    nurses = db.query(Nurse.nurse_id, Nurse.name, Nurse.sequence).filter(
        Nurse.group_id == target_group_id,
        Nurse.active == 1,
    ).order_by(Nurse.sequence.asc(), Nurse.nurse_id.asc()).all()

    entries = db.query(ScheduleEntry.nurse_id, ScheduleEntry.work_date, ScheduleEntry.shift_id).filter(
        ScheduleEntry.schedule_id == schedule_id,
    ).all()

    by_nurse = {}
    for e in entries:
        by_nurse.setdefault(str(e.nurse_id), {})[int(e.work_date.day)] = str(e.shift_id) if e.shift_id else "-"

    col_labels = ["이름"] + [str(d) for d in range(1, days_in_month + 1)]
    table_rows = []
    for n in nurses:
        row = [str(n.name)]
        day_map = by_nurse.get(str(n.nurse_id), {})
        for d in range(1, days_in_month + 1):
            row.append(day_map.get(d, "-"))
        table_rows.append(row)

    if not table_rows:
        table_rows = [["데이터 없음"] + ["-" for _ in range(days_in_month)]]

    fig_w = max(14, 1 + days_in_month * 0.42)
    fig_h = max(4, 1.5 + len(table_rows) * 0.35)
    fig, ax = plt.subplots(figsize=(fig_w, fig_h))
    ax.axis("off")
    ax.set_title(f"{year}년 {month}월 근무표", fontsize=16, pad=18)

    table = ax.table(
        cellText=table_rows,
        colLabels=col_labels,
        loc="center",
        cellLoc="center",
        colLoc="center",
    )
    table.auto_set_font_size(False)
    table.set_fontsize(8)
    table.scale(1.0, 1.35)

    image_buffer = io.BytesIO()
    fig.savefig(image_buffer, format="png", dpi=180, bbox_inches="tight")
    plt.close(fig)
    image_buffer.seek(0)
    image_bytes = image_buffer.getvalue()
    if not image_bytes:
        raise RuntimeError("failed to generate roster image")

    bucket_name = os.getenv("AWS_SHARE_S3_BUCKET") or os.getenv("SHARE_S3_BUCKET") or os.getenv("S3_SHARE_BUCKET")
    if not bucket_name:
        raise ValueError("share S3 bucket env is not configured")
    region = os.getenv("AWS_REGION", "ap-northeast-2")
    object_prefix = _share_build_object_prefix(
        office_id=target_office_id,
        group_id=target_group_id,
        nurse_id=getattr(current_user, "nurse_id", None),
        year=year,
        month=month,
    )
    object_key = f"{object_prefix}/auto-{secrets.token_hex(16)}.png"

    try:
        s3_client = _share_build_s3_client(region)
        s3_client.put_object(
            Bucket=bucket_name,
            Key=object_key,
            Body=image_bytes,
            ContentType="image/png",
            CacheControl="max-age=31536000",
        )
    except Exception as e:
        raise RuntimeError(f"S3 upload failed: {str(e)}")

    image_url = f"https://{bucket_name}.s3.{region}.amazonaws.com/{object_key}"

    return create_schedule_share_link_service(
        db=db,
        current_user=current_user,
        schedule_id=schedule_id,
        fallback_base_url=fallback_base_url,
        image_url=image_url,
        title=title,
        description=description,
        expires_in_days=expires_in_days,
        override_group_id=override_group_id,
    )



def capture_schedule_share_image_and_create_link_service(
    db: Session,
    current_user,
    schedule_id: str,
    fallback_base_url: str,
    image_data_url: str,
    title: str | None,
    description: str | None,
    expires_in_days: int,
    override_group_id: str | None = None,
) -> dict:
    import os
    import base64
    import secrets
    import binascii
    import boto3

    target_group_id, target_office_id = _share_resolve_target_scope(
        current_user=current_user,
        db=db,
        override_group_id=override_group_id,
    )

    schedule = db.query(Schedule).filter(
        Schedule.schedule_id == schedule_id,
        Schedule.group_id == target_group_id,
        Schedule.office_id == target_office_id,
    ).first()
    if not schedule:
        raise LookupError("Schedule not found for your scope")

    if not image_data_url or not str(image_data_url).strip():
        raise ValueError("image_data_url is required")

    raw_data = str(image_data_url).strip()
    if not raw_data.startswith("data:"):
        raise ValueError("image_data_url must be data URL")

    try:
        header, b64_data = raw_data.split(",", 1)
    except ValueError:
        raise ValueError("invalid image_data_url format")

    if ";base64" not in header:
        raise ValueError("image_data_url must be base64 data URL")

    mime_type = header[5:].split(";", 1)[0].lower()
    allowed_types = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/webp": ".webp",
    }
    if mime_type not in allowed_types:
        raise ValueError("지원하지 않는 이미지 형식입니다. (png, jpg, jpeg, webp)")

    try:
        image_bytes = base64.b64decode(b64_data, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("invalid base64 image data")

    if not image_bytes:
        raise ValueError("image data is empty")
    if len(image_bytes) > 5 * 1024 * 1024:
        raise ValueError("image data size must be <= 5MB")

    bucket_name = os.getenv("AWS_SHARE_S3_BUCKET") or os.getenv("SHARE_S3_BUCKET") or os.getenv("S3_SHARE_BUCKET")
    if not bucket_name:
        raise ValueError("share S3 bucket env is not configured")
    region = os.getenv("AWS_REGION", "ap-northeast-2")

    ext = allowed_types[mime_type]
    object_prefix = _share_build_object_prefix(
        office_id=target_office_id,
        group_id=target_group_id,
        nurse_id=getattr(current_user, "nurse_id", None),
        year=int(schedule.year),
        month=int(schedule.month),
    )
    object_key = f"{object_prefix}/capture-{secrets.token_hex(16)}{ext}"

    try:
        s3_client = _share_build_s3_client(region)
        s3_client.put_object(
            Bucket=bucket_name,
            Key=object_key,
            Body=image_bytes,
            ContentType=mime_type,
            CacheControl="max-age=31536000",
        )
    except Exception as e:
        raise RuntimeError(f"S3 upload failed: {str(e)}")

    image_url = f"https://{bucket_name}.s3.{region}.amazonaws.com/{object_key}"

    return create_schedule_share_link_service(
        db=db,
        current_user=current_user,
        schedule_id=schedule_id,
        fallback_base_url=fallback_base_url,
        image_url=image_url,
        title=title,
        description=description,
        expires_in_days=expires_in_days,
        override_group_id=override_group_id,
    )


def get_public_share_link_service(db: Session, token: str) -> dict | None:
    share_row = _share_find_by_token(db, token)
    if not share_row:
        return None
    if share_row.get("revoked_at") is not None:
        return None
    expires_at = share_row.get("expires_at")
    if expires_at is not None and expires_at < _share_now():
        return None
    return share_row



def get_public_share_image_service(db: Session, token: str) -> tuple[bytes, str]:
    share_row = get_public_share_link_service(db, token)
    if not share_row:
        raise LookupError("Share link not found or expired")
    image_url = share_row.get("image_url")
    if not image_url:
        raise LookupError("Share image not found")
    return _share_fetch_s3_image_bytes(str(image_url))


def revoke_schedule_share_link_service(db: Session, current_user, token: str) -> dict:

    share_row = _share_find_by_token(db, token)
    if not share_row:
        raise LookupError("Share link not found")

    is_master_admin = bool(getattr(current_user, "is_master_admin", False))
    is_head_nurse = caller_is_head_nurse(db, current_user)
    if not (is_master_admin or is_head_nurse):
        raise PermissionError("Permission denied")
    if is_master_admin and getattr(current_user, "office_id", None) and share_row.get("office_id") != getattr(current_user, "office_id", None):
        raise PermissionError("Share link does not belong to your office")
    if not is_master_admin and share_row.get("group_id") != getattr(current_user, "group_id", None):
        raise PermissionError("You can only revoke links in your group")

    from db.models import ShareLink

    revoked_at = _share_now()
    db.query(ShareLink).filter(ShareLink.token == token).update(
        {
            ShareLink.revoked_at: revoked_at,
            ShareLink.updated_at: revoked_at,
        },
        synchronize_session=False,
    )
    db.commit()

    return {"success": True, "token": token, "revoked_at": revoked_at}
