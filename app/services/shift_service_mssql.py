from typing import List, Dict, Any


import os
import datetime as _dt

from db.models import Shift, Nurse, Group, Office, ScheduleEntry, ShiftManage
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy import create_engine, func
_MSSQL_SESSION_MAKER: sessionmaker | None = None




# def _to_time_str(value: Any) -> str | None:
#     """TIME 컬럼값을 HH:MM:SS 문자열로 변환합니다."""
#     if value is None:
#         return None
#     if isinstance(value, _dt.time):
#         return value.strftime("%H:%M")
#     s = str(value)
#     return s


# def _to_time_str(value: Any) -> str | None:
#     """TIME 컬럼값을 HH:MM 문자열로 변환합니다."""
#     if value is None:
#         return None
#     if isinstance(value, _dt.time):
#         return value.strftime("%H:%M")
#     if isinstance(value, str):
#         try:
#             parts = value.split(":")
#             if len(parts) >= 2:
#                 return f"{parts[0]}:{parts[1]}"  # HH:MM:SS → HH:MM
#         except Exception as e:
#             print(f"_to_time_str error: {str(e)}, value: {value}")
#             return None
#     print(f"_to_time_str unexpected type: {type(value)}, value: {value}")
#     return None  # 안전하게 None 반환


def _to_time_str(value: Any) -> str | None:
    """TIME 컬럼값을 HH:MM:SS 문자열로 변환합니다."""
    if value is None:
        return None
    
    # datetime.time 객체 처리: HH:MM:SS 형식으로 변환
    if isinstance(value, _dt.time):
        return value.strftime("%H:%M:%S")
    
    # datetime.datetime 객체 처리: 시간 부분만 추출해 HH:MM:SS 형식으로 변환
    if isinstance(value, _dt.datetime):
        return value.time().strftime("%H:%M:%S")
    
    # 문자열 처리: :로 분리해 HH:MM:SS 형식으로 변환, 초가 없으면 00 추가
    if isinstance(value, str):
        try:
            parts = value.split(":")
            if len(parts) >= 2:
                hours = parts[0].zfill(2)  # 시간 2자리로 맞춤 (예: "6" → "06")
                minutes = parts[1].zfill(2)  # 분 2자리로 맞춤
                seconds = parts[2].zfill(2) if len(parts) > 2 else "00"  # 초 없으면 00
                return f"{hours}:{minutes}:{seconds}"
            else:
                print(f"_to_time_str invalid format: {value}")
                return None
        except Exception as e:
            print(f"_to_time_str error: {str(e)}, value: {value}")
            return None
    
    # 예상치 못한 타입 처리: 디버깅 로그 추가 후 None 반환
    print(f"_to_time_str unexpected type: {type(value)}, value: {value}")
    return None


def _append_shift_manage_code(
    session: Session,
    office_id: str | None,
    group_id: str,
    shift_id: str,
    shift_gb: str | None,
    old_shift_id: str | None = None,
    old_shift_gb: str | None = None,
) -> None:
    """
    shift_manage.codes에 근무코드를 중복 없이 추가합니다.

    - shift_gb가 D/E/N일 때만 슬롯(1/2/3)에 매핑합니다.
    - 이미 존재하면 추가하지 않습니다.
    - update 시 shift_id 또는 shift_gb가 바뀌면 기존 슬롯에서 제거 후 새 슬롯에 추가합니다.
    """
    slot_map = {"D": 1, "E": 2, "N": 3}
    if not office_id:
        return

    def _remove(target_gb: str | None, target_id: str | None) -> bool:
        if target_gb not in slot_map or not target_id:
            return False
        target_slot = slot_map[target_gb]
        shift_manages = (
            session.query(ShiftManage)
            .filter(
                ShiftManage.office_id == office_id,
                ShiftManage.group_id == group_id,
                ShiftManage.shift_slot == target_slot,
                ShiftManage.main_code == target_gb,
            )
            .all()
        )
        removed = False
        for shift_manage in shift_manages:
            codes = shift_manage.codes or []
            if target_id in codes:
                shift_manage.codes = [code for code in codes if code != target_id]
                removed = True
        return removed

    def _append(target_gb: str | None, target_id: str) -> bool:
        if target_gb not in slot_map:
            return False
        target_slot = slot_map[target_gb]
        shift_manages = (
            session.query(ShiftManage)
            .filter(
                ShiftManage.office_id == office_id,
                ShiftManage.group_id == group_id,
                ShiftManage.shift_slot == target_slot,
                ShiftManage.main_code == target_gb,
            )
            .all()
        )
        added = False
        for shift_manage in shift_manages:
            codes = shift_manage.codes or []
            if shift_id not in codes:
                shift_manage.codes = codes + [shift_id]
                added = True
        return added

    removed_any = False
    if old_shift_id and (old_shift_id != shift_id or old_shift_gb != shift_gb):
        removed_any = _remove(old_shift_gb, old_shift_id)

    added_any = _append(shift_gb, shift_id)

    if removed_any or added_any:
        session.commit()


def get_shifts_service(current_user, db: Session | None = None, override_group_id: str | None = None) -> List[Dict[str, Any]]:
    """MSSQL 전용 ORM 기반 시프트 조회 서비스.
    - 항상 MSSQL 세션을 사용합니다(파라미터 db 무시).
    - 존재하면 반환, 없으면 기본 4개(O,E,N,D) 생성 후 반환
    - 그룹/오피스 자동 생성 로직은 수행하지 않습니다.
    """
    if not current_user:
        raise Exception("Not authenticated")
    session = db
    
    try:
        # 1) 조회
        if override_group_id:
            group_id = override_group_id
        else:
            group_id = current_user.group_id
        shifts = (
            session.query(Shift)
            .filter(Shift.office_id == current_user.office_id, Shift.group_id == group_id)
            .order_by(Shift.sequence.asc())
            .all()
        )
        
        if shifts:
            print('shifts', [s.__dict__ for s in shifts])
            
            return [
                {
                    "shift_id": s.shift_id,
                    "name": s.name,
                    "color": s.color,
                    "start_time": _to_time_str(s.start_time),
                    "end_time": _to_time_str(s.end_time),
                    "type": s.type,
                    "allday": s.allday,
                    "auto_schedule": s.auto_schedule,
                    "duration": s.duration,
                    "sequence": s.sequence,
                    "shift_gb": getattr(s, "shift_gb", None),
                    "default_shift": getattr(s, "default_shift", s.shift_id),
                    "id": getattr(s, "id", None),
                }
                for s in shifts
            ]
        
        # 2) 기본값 생성 (오피스/그룹은 존재한다고 가정; 없으면 office_id=None로 저장)
        # 기본값 생성
        office_id = None
        group = session.query(Group).filter(Group.group_id == group_id).first()
        print('[get_shifts_service_mssql] group_id', group_id)
        if group and group.office_id:
            office_id = group.office_id
        elif getattr(current_user, "office_id", None):
            office_id = current_user.office_id
        else:
            nurse = session.query(Nurse).filter(Nurse.nurse_id == current_user.nurse_id).first()
            if nurse and nurse.group and nurse.group.office_id:
                office_id = nurse.group.office_id

        def _mk(shift_id: str, name: str, color: str, st: str | None, et: str | None, typ: str, allday: int, auto_s: int, dur: int | None, seq: int, default_shift: str, shift_gb: str | None) -> Shift:
            return Shift(
                shift_id=shift_id,
                name=name,
                office_id=office_id,
                color=color,
                group_id=group_id,
                start_time=st,
                end_time=et,
                type=typ,
                allday=allday,
                auto_schedule=auto_s,
                duration=dur,
                sequence=seq,
                default_shift=default_shift,
                shift_gb=shift_gb,
            )
        defaults = [
            # shift_id, name, color, start, end, type, allday, auto_schedule, duration, sequence, default_shift, shift_gb
            ("O", "Off", "#ffa0d2", None, None, "휴무", 1, 1, None, 4, "O", "O"),
            ("E", "Evening", "#72bfff", "14:00:00", "22:00:00", "근무", 0, 1, None, 2, "E", "E"),
            ("N", "Night", "#bab0f0", "22:00:00", "06:00:00", "근무", 0, 1, None, 3, "N", "N"),
            ("D", "Day", "#59dbd7", "06:00:00", "14:00:00", "근무", 0, 1, None, 1, "D", "D"),
            # 주휴(표시용): 엔진/검증에서는 O로 정규화하여 휴무로 카운트한다.
            # sequence는 중복을 피하기 위해 기본 4개 뒤로 배치한다.
            ("주", "주휴", "#ff977b", None, None, "휴무", 1, 0, None, 5, "주", "O"),
        ]
        for args in defaults:
            session.add(_mk(*args))
        session.commit()
        for args in defaults:
            _append_shift_manage_code(
                session=session,
                office_id=office_id,
                group_id=group_id,
                shift_id=args[0],
                shift_gb=args[-1],
            )

        shifts = (
            session.query(Shift)
            .filter(Shift.group_id == group_id)
            .order_by(Shift.sequence.asc())
            .all()
        )
        return [
            {
                "shift_id": s.shift_id,
                "name": s.name,
                "color": s.color,
                "start_time": _to_time_str(s.start_time),
                "end_time": _to_time_str(s.end_time),
                "type": s.type,
                "allday": s.allday,
                "auto_schedule": s.auto_schedule,
                "duration": s.duration,
                "sequence": s.sequence,
                "shift_gb": getattr(s, "shift_gb", None),
                "default_shift": getattr(s, "default_shift", s.shift_id),
                "id": getattr(s, "id", None),
            }
            for s in shifts
        ]
    finally:
        pass


def add_shift_service(req, current_user, db: Session | None = None):
    """시프트 등록 서비스(MSSQL)."""
    if not current_user or not current_user.is_head_nurse:
        raise Exception("Permission denied")

    session = db
    try:
        nurse = session.query(Nurse).filter(Nurse.nurse_id == current_user.nurse_id).first()
        if not nurse or not nurse.group:
            raise Exception("User group information not found")

        existing_shift = session.query(Shift).filter(
            Shift.shift_id == req.shift_id,
            Shift.group_id == current_user.group_id
        ).first()
        if existing_shift:
            raise Exception("이미 존재하는 근무코드입니다.")

        max_sequence = session.query(func.max(Shift.sequence)).filter(
            Shift.group_id == current_user.group_id
        ).scalar() or 0

        new_shift = Shift(
            shift_id=req.shift_id,
            office_id=nurse.group.office_id if nurse.group else None,
            group_id=current_user.group_id,
            name=req.name,
            color=req.color,
            start_time=req.start_time,
            end_time=req.end_time,
            type=req.type,
            duration=req.duration,
            allday=req.allday,
            auto_schedule=req.auto_schedule,
            sequence=max_sequence + 1,
            shift_gb=req.shift_gb,
        )
        session.add(new_shift)
        session.commit()
        session.refresh(new_shift)
        _append_shift_manage_code(
            session=session,
            office_id=nurse.group.office_id if nurse.group else None,
            group_id=current_user.group_id,
            shift_id=new_shift.shift_id,
            shift_gb=req.shift_gb,
        )
        return {
            "message": "근무코드가 성공적으로 추가되었습니다.",
            "shift": {
                "shift_id": new_shift.shift_id,
                "name": new_shift.name,
                "color": new_shift.color,
                "sequence": new_shift.sequence,
                "shift_gb": new_shift.shift_gb,
            },
        }
    finally:
        pass


def update_shift_service(req, current_user, db: Session | None = None):
    """시프트 수정 서비스(MSSQL)."""
    if not current_user or not current_user.is_head_nurse:
        raise Exception("Permission denied")

    session = db
    try:
        existing_shift = session.query(Shift).filter(
            Shift.id == req.id,
            Shift.group_id == current_user.group_id
        ).first()
        if not existing_shift:
            raise Exception("해당 근무코드를 찾을 수 없습니다.")
        old_shift_id = existing_shift.shift_id
        old_shift_gb = getattr(existing_shift, "shift_gb", None)

        existing_shift.shift_id = req.shift_id
        existing_shift.name = req.name
        existing_shift.color = req.color
        existing_shift.start_time = req.start_time
        existing_shift.end_time = req.end_time
        existing_shift.type = req.type
        existing_shift.duration = req.duration
        existing_shift.allday = req.allday
        existing_shift.auto_schedule = req.auto_schedule
        existing_shift.shift_gb = req.shift_gb

        session.commit()
        session.refresh(existing_shift)
        _append_shift_manage_code(
        session=session,
        office_id=existing_shift.office_id,
        group_id=current_user.group_id,
        shift_id=existing_shift.shift_id,
        shift_gb=req.shift_gb,
            old_shift_id=old_shift_id,
            old_shift_gb=old_shift_gb,
    )
        return {
            "message": "근무코드가 성공적으로 수정되었습니다.",
            "shift": {
                "shift_id": existing_shift.shift_id,
                "name": existing_shift.name,
                "color": existing_shift.color,
                "sequence": existing_shift.sequence,
            "shift_gb": existing_shift.shift_gb,
            }
        }
    finally:
        pass


def remove_shift_service(req, current_user, db: Session | None = None):
    """시프트 삭제 서비스(MSSQL)."""
    if not current_user or not current_user.is_head_nurse:
        raise Exception("Permission denied")

    session = db
    try:
        existing_shift = session.query(Shift).filter(
            Shift.shift_id == req.shift_id,
            Shift.group_id == current_user.group_id
        ).first()
        if not existing_shift:
            raise Exception("해당 근무코드를 찾을 수 없습니다.")

        schedule_entries_count = session.query(ScheduleEntry).filter(
            ScheduleEntry.shift_id == req.shift_id
        ).count()
        if schedule_entries_count > 0:
            raise Exception("해당 근무코드는 현재 사용 중이므로 삭제할 수 없습니다.")

        deleted_sequence = existing_shift.sequence
        session.delete(existing_shift)
        session.query(Shift).filter(
            Shift.group_id == current_user.group_id,
            Shift.sequence > deleted_sequence
        ).update({"sequence": Shift.sequence - 1})
        session.commit()
        return {"message": "근무코드가 성공적으로 삭제되었습니다."}
    finally:
        pass


def move_shift_service(req, current_user, db: Session | None = None):
    """시프트 순서 이동 서비스(MSSQL)."""
    if not current_user or not current_user.is_head_nurse:
        raise Exception("Permission denied")

    session = db
    try:
        shift_to_move = session.query(Shift).filter(
            Shift.shift_id == req.shift_id,
            Shift.group_id == current_user.group_id
        ).first()
        if not shift_to_move:
            raise Exception("해당 근무코드를 찾을 수 없습니다.")

        old_sequence = shift_to_move.sequence
        new_sequence = req.new_sequence
        if old_sequence == new_sequence:
            return {"message": "변경사항이 없습니다."}

        if old_sequence < new_sequence:
            session.query(Shift).filter(
                Shift.group_id == current_user.group_id,
                Shift.sequence > old_sequence,
                Shift.sequence <= new_sequence
            ).update({"sequence": Shift.sequence - 1})
        else:
            session.query(Shift).filter(
                Shift.group_id == current_user.group_id,
                Shift.sequence >= new_sequence,
                Shift.sequence < old_sequence
            ).update({"sequence": Shift.sequence + 1})
        shift_to_move.sequence = new_sequence
        session.commit()
        return {"message": "근무코드 순서가 성공적으로 변경되었습니다."}
    finally:
        pass
