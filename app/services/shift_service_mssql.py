from typing import List, Dict, Any


import os
import base64
import json
import datetime as _dt

from db.models import Shift, Nurse, Group, Office, ScheduleEntry, ShiftManage
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy import create_engine, func, or_, and_, case
_MSSQL_SESSION_MAKER: sessionmaker | None = None




# def _to_time_str(value: Any) -> str | None:
#     """TIME 컬럼값을 HH:MM:SS 문자열로 변환합니다."""
#     if value is None:
#         return None
#     if isinstance(value, _dt.time):
#         return value.strftime("%H:%M")
#     s = str(value)
#     return s



# ══════════════════════════════════════════════════════════════════════════
# ★★★ 이 모듈의 쓰기 함수 4종은 **현재 라우터에 배선돼 있지 않다** (동면 상태)
#
#   `add_shift_service` · `update_shift_service` · `remove_shift_service` ·
#   `move_shift_service` 는 아무데서도 import 되지 않는다. `app/routers/shifts.py` 는
#   이 넷을 전부 `services.shift_service` 에서 가져가고, 이 모듈에서는
#   `get_shifts_service` · `get_shifts_paged_service` 만(그리고 roster_service 가
#   `_to_time_str` 만) 쓴다.
#
#   왜 이렇게 됐나 — 2025-10 MySQL→MSSQL 전환 때 "MSSQL 전용 세션" 을 따로 두려고
#   이 모듈이 생겼다. 전환이 끝나 앱 전체가 MSSQL 이 되면서(`db/client2.py` 의
#   DATABASE_URL 이 `mssql+pymssql://`) 분리 이유가 사라졌다. 흔적: 이 파일의
#   `_MSSQL_SESSION_MAKER` 는 선언만 되고 쓰이지 않으며, 함수들은 전부 `session = db` 로
#   라우터가 넘긴 같은 세션을 쓴다. `client2.py` 에 그 계획이 주석으로 남아 있다.
#   **보존은 의도된 결정이다**(언제 되살릴지 모른다). 지우지 말 것.
#
# ──────────────────────────── 부활 체크리스트 ────────────────────────────
#   되살릴 때 아래를 **순서대로** 처리하라. 배선된 `services/shift_service.py` 가 정답지다.
#
#   1. 시그니처에 `override_group_id: str | None = None` 를 4번째 인자로 추가하라.
#      라우터는 `add_shift_service(req, current_user, db, group_id)` 로 **4인자**를 넘긴다.
#      지금 이 모듈은 3인자라 import 만 바꾸면 첫 호출에서 TypeError 로 즉사한다.
#      ★ 이때 라우터에서 4번째 인자를 빼는 쪽으로 고치지 마라 — 에러는 사라지지만
#        아래 2번(그룹 스코프)이 조용히 뚫린다.
#   2. 대상 병동을 `resolve_effective_group(db, current_user, override_group_id or ...)` 로
#      해석하라. 지금은 `current_user.group_id` 를 그대로 쓴다.
#   3. 권한을 `caller_is_head_nurse(db, current_user) or is_master_admin` 으로 바꿔라.
#      지금의 `current_user.is_head_nurse` 는 토큰 클레임이라 승급·강등 후 만료까지 stale 하고,
#      nurse 행이 없는 ADM 은 False 라 전면 차단된다.
#   4. add/update 에 `off_swap_target` · `health_leave_target` · `sleep_off_target` 대입과
#      `_assert_off_swap_target_valid` 등 검증 3종을 **함께** 넣어라. 지금은 둘 다 없어
#      일관되다. 대입만 넣으면 그룹당 1건 제약이 검증 없이 뚫린다.
#   5. add/update 에 `shift_code_taken(...)` 중복·개명 충돌 검사를 넣어라. 유일성 키는
#      (office_id, group_id, shift_id) 다.
#   6. Shift 쓰기와 `shift_manage` 갱신을 한 트랜잭션으로 묶어라 —
#      `flush()` → `_append_shift_manage_code(..., commit=False)` → `commit()` 1회 → 실패 시 rollback.
#      지금은 2단 commit 이라 중간 실패 시 슬롯 등록만 누락되고 재시도로 복구되지 않는다.
#   7. `try: ... finally: pass` 죽은 블록을 정리하라. 예외를 잡지도 되돌리지도 않는데
#      "여긴 에러 처리가 있다" 는 오독을 부른다.
#   8. `_append` 클로저(아래)가 인자 `target_id` 대신 바깥 `shift_id` 를 쓴다. 호출이 한 곳뿐이라
#      지금은 값이 같지만, 다른 인자로 재사용하는 순간 조용히 틀린다.
# ══════════════════════════════════════════════════════════════════════════

# ★★ 근무코드 목록의 **표준 정렬**. 여기를 바꾸면 아래 두 곳도 같이 바꿔야 한다.
#   - roster_create_service._persist_entries (근무표 셀이 가리킬 shifts.id 를 고르는 곳)
#   - roster_create_service 의 파견/병동이동 전달 매핑
#   `shifts` 에 (office_id, group_id, shift_id) UNIQUE 가 없어 같은 코드가 여러 행일 수 있고,
#   그 경우 **먼저 오는 행이 대표**다. `sequence` 만으로는 부족하다 — 같은 병동 안에서
#   sequence 가 겹치는 행이 실재해서(예: 한 병동의 M 과 검진이 둘 다 5), 동점일 때 SQL 이
#   돌려주는 순서는 보장되지 않는다. 그러면 화면이 보여 주는 행과 근무표가 가리키는 행이
#   갈려서 "코드를 고쳤는데 근무표에 반영이 안 된다" 가 다시 난다. `id` 로 동점을 끊는다.
SHIFT_LIST_ORDER = (Shift.sequence.asc(), Shift.id.asc())


def _to_time_str(value: Any) -> str | None:
    """TIME 컬럼값을 HH:MM 문자열로 변환합니다."""
    if value is None:
        return None
    if isinstance(value, _dt.time):
        return value.strftime("%H:%M")
    if isinstance(value, str):
        try:
            parts = value.split(":")
            if len(parts) >= 2:
                return f"{parts[0]}:{parts[1]}"  # HH:MM:SS → HH:MM
        except Exception as e:
            print(f"_to_time_str error: {str(e)}, value: {value}")
            return None
    print(f"_to_time_str unexpected type: {type(value)}, value: {value}")
    return None  # 안전하게 None 반환


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
    slot_map = {"D": 1, "E": 2, "N": 3, "M": 5, "데이": 1, "이브닝": 2, "나이트": 3, "미드": 5}
    # shift_gb 한글 → ShiftManage.main_code 영문 변환
    _gb_to_main = {"데이": "D", "이브닝": "E", "나이트": "N", "미드": "M"}
    if not office_id:
        return

    def _resolve_main(gb: str | None) -> str | None:
        return _gb_to_main.get(gb, gb)

    def _remove(target_gb: str | None, target_id: str | None) -> bool:
        if target_gb not in slot_map or not target_id:
            return False
        target_slot = slot_map[target_gb]
        main_code = _resolve_main(target_gb)
        shift_manages = (
            session.query(ShiftManage)
            .filter(
                ShiftManage.office_id == office_id,
                ShiftManage.group_id == group_id,
                ShiftManage.shift_slot == target_slot,
                ShiftManage.main_code == main_code,
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
        main_code = _resolve_main(target_gb)
        shift_manages = (
            session.query(ShiftManage)
            .filter(
                ShiftManage.office_id == office_id,
                ShiftManage.group_id == group_id,
                ShiftManage.shift_slot == target_slot,
                ShiftManage.main_code == main_code,
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
            .order_by(*SHIFT_LIST_ORDER)
            .all()
        )

        # ★★ 이 함수는 조회 엔드포인트(GET /shifts)인데 아래에서 **INSERT + commit 을 한다.**
        #   그래서 대상 병동이 정해지지 않은 호출(ADM 이 병동 지정 없이 목록을 여는 경우
        #   `current_user.group_id` 가 빈 문자열이다)에서도 그 빈 값으로 근무코드가 깔렸다.
        #   실측: 운영에 `group_id=''` 행이 **12개 오피스에 4~6개씩, 합 62행** 쌓여 있다.
        #   어느 병동에도 속하지 않아 화면에 안 보이고 지울 경로도 없는 유령 행이다.
        #   병동이 정해지지 않았으면 있는 것만 돌려주고 **만들지 않는다.**
        if not group_id:
            return [_shift_row_to_dict(s) for s in shifts]

        if shifts:
            # ★ 판정은 shift_id 로 한다. 예전엔 `default_shift == 'M'` 로 판정하면서 삽입은
            #   `shift_id='M'` 으로 해서, shift_id='M' 이 있는데 default_shift 가 M 이 아닌
            #   병동에서는 **목록을 열 때마다 'M' 이 한 행씩 늘었다.**
            has_mid = any(str(getattr(s, "shift_id", "") or "").upper() == "M" for s in shifts)
            if not has_mid:
                mid_shift = Shift(
                    shift_id="M",
                    name="미드",
                    office_id=current_user.office_id,
                    color="#E6A817",
                    group_id=group_id,
                    start_time="09:00:00",
                    end_time="17:00:00",
                    type="근무",
                    allday=0,
                    auto_schedule=1,
                    duration=None,
                    sequence=5,
                    default_shift="M",
                    shift_gb="미드",
                    show_in_preference=True,
                )
                session.add(mid_shift)
                session.commit()
                _append_shift_manage_code(
                    session=session,
                    office_id=current_user.office_id,
                    group_id=group_id,
                    shift_id="M",
                    shift_gb="미드",
                )
                shifts = (
                    session.query(Shift)
                    .filter(Shift.office_id == current_user.office_id, Shift.group_id == group_id)
                    .order_by(*SHIFT_LIST_ORDER)
                    .all()
                )
            # print('shifts', [s.__dict__ for s in shifts])

            return [_shift_row_to_dict(s) for s in shifts]


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
                # 추가
                show_in_preference=True if shift_id in ["D", "E", "N", "M", "O"] else False
            )
        defaults = [
            # shift_id, name, color, start, end, type, allday, auto_schedule, duration, sequence, default_shift, shift_gb
            ("O", "Off", "#ffa0d2", None, None, "휴무", 1, 1, None, 5, "O", "O"),
            ("E", "Evening", "#72bfff", "14:00:00", "22:00:00", "근무", 0, 1, None, 2, "E", "이브닝"),
            ("N", "Night", "#bab0f0", "22:00:00", "06:00:00", "근무", 0, 1, None, 3, "N", "나이트"),
            ("D", "Day", "#59dbd7", "06:00:00", "14:00:00", "근무", 0, 1, None, 1, "D", "데이"),
            ("M", "미드", "#E6A817", "09:00:00", "17:00:00", "근무", 0, 1, None, 4, "M", "미드"),
            # 주휴(표시용): 엔진/검증에서는 O로 정규화하여 휴무로 카운트한다.
            # sequence는 중복을 피하기 위해 기본 4개 뒤로 배치한다.
            ("주", "주휴", "#ff977b", None, None, "휴무", 1, 0, None, 6, "주", "O"),
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
            .filter(Shift.office_id == office_id, Shift.group_id == group_id)
            .order_by(*SHIFT_LIST_ORDER)
            .all()
        )
        return [_shift_row_to_dict(s) for s in shifts]
    finally:
        pass


# ── 근무코드 목록 cursor 페이징(읽기 전용) ──────────────────────────────────
# get_shifts_service 와 달리 기본코드/MID 자동생성 등 부수효과 없이 순수 조회만 한다.
# 정렬은 기본코드 상단고정(default_rank) → sequence ASC → id ASC 안정정렬.
# 커서는 마지막 행의 (default_rank, sequence, id) 를 base64url(json) 으로 인코딩한 불투명 토큰이다.
def _encode_shift_cursor(rank: int, sequence: int | None, row_id: int | None) -> str:
    raw = json.dumps({"d": rank, "sequence": sequence, "id": row_id}, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_shift_cursor(cursor: str | None):
    """커서 → (default_rank, sequence, id). 손상/형식오류면 None(=처음부터)."""
    if not cursor:
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8"))
        return (data.get("d"), data.get("sequence"), data.get("id"))
    except (ValueError, TypeError):
        return None


def _shift_row_to_dict(s: Shift) -> Dict[str, Any]:
    """get_shifts_service 와 동일한 항목 형태(시간은 _to_time_str 문자열)."""
    return {
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
        "show_in_preference": s.show_in_preference,
        "off_swap_target": bool(getattr(s, "off_swap_target", False)),
        "health_leave_target": bool(getattr(s, "health_leave_target", False)),
        "sleep_off_target": bool(getattr(s, "sleep_off_target", False)),
        "description": getattr(s, "description", None),
    }


def get_shifts_paged_service(
    current_user,
    db: Session,
    group_id: str,
    cursor: str | None = None,
    limit: int = 20,
    q: str | None = None,
) -> Dict[str, Any]:
    """근무코드 목록 cursor 페이징(읽기 전용).

    - 정렬: 기본코드 상단고정(default_rank) → sequence ASC → id ASC(안정정렬).
    - cursor: 직전 응답 nextCursor 그대로 전달(불투명). 없으면 첫 페이지.
    - q: shift_id 또는 name 부분일치(대소문자 무시).
    - 반환: {items, nextCursor, total}. nextCursor=None 이면 마지막 페이지.
    - get_shifts_service 와 달리 기본코드/MID 자동생성 부수효과 없음.
    """
    if not current_user:
        raise Exception("Not authenticated")
    limit = max(1, min(int(limit or 20), 100))  # 방어적 클램프 1..100
    base = db.query(Shift).filter(
        Shift.office_id == current_user.office_id,
        Shift.group_id == group_id,
    )
    if q and q.strip():
        like = f"%{q.strip()}%"
        base = base.filter(or_(Shift.shift_id.ilike(like), Shift.name.ilike(like)))

    total = base.count()

    # 기본코드(default_shift NOT NULL) 상단고정 → default_rank ASC, sequence ASC, id ASC.
    #   일부 그룹은 기본코드 sequence 가 일반코드보다 커서 sequence 만으론 밀리므로 rank 를 1순위로 둔다.
    _default_rank = case((Shift.default_shift.is_(None), 1), else_=0)
    page_q = base.order_by(_default_rank.asc(), Shift.sequence.asc(), Shift.id.asc())
    decoded = _decode_shift_cursor(cursor)
    if decoded is not None and all(v is not None for v in decoded):
        c_d, c_seq, c_id = decoded
        # keyset: (default_rank, sequence, id) > (c_d, c_seq, c_id)
        page_q = page_q.filter(
            or_(
                _default_rank > c_d,
                and_(_default_rank == c_d, Shift.sequence > c_seq),
                and_(_default_rank == c_d, Shift.sequence == c_seq, Shift.id > c_id),
            )
        )
    rows = page_q.limit(limit + 1).all()

    has_next = len(rows) > limit
    page_rows = rows[:limit]
    items = [_shift_row_to_dict(s) for s in page_rows]
    next_cursor = None
    if has_next and page_rows:
        last = page_rows[-1]
        last_rank = 0 if getattr(last, "default_shift", None) is not None else 1
        next_cursor = _encode_shift_cursor(last_rank, last.sequence, getattr(last, "id", None))
    return {"items": items, "nextCursor": next_cursor, "total": int(total)}


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
            description=getattr(req, "description", None),
            # ★ 이 둘은 정책 검증이 걸리지 않아 단독으로 넣어도 안전하다. 빠져 있으면
            #   부활 시 화면에서 켠 값이 **오류도 경고도 없이 삼켜진다.**
            #   `default_shift` 는 주휴 식별 SSOT 라 NULL 이면 주휴 코드를 새로 만들어도
            #   주휴로 인식되지 않고 MID 판정도 어긋난다.
            #   ★ off_swap_target · health_leave_target · sleep_off_target 3종은 **일부러
            #     넣지 않았다.** 이 모듈에는 짝이 되는 `_assert_*_target_valid` 검증이 없어서,
            #     대입만 추가하면 그룹당 1건 제약이 검증 없이 뚫린다. 부활할 때 대입과 검증을
            #     **함께** 넣어야 한다(아래 부활 체크리스트 참조).
            default_shift=getattr(req, "default_shift", None),
            show_in_preference=getattr(req, "show_in_preference", False),
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
                "description": new_shift.description,
            },
        }
    finally:
        pass


def update_shift_service(req, current_user, db: Session | None = None):
    """시프트 수정 서비스(MSSQL).

    ★★ **현재 라우터에 배선돼 있지 않다.** `app/routers/shifts.py` 는 add/update/remove/move 를
      `services.shift_service` 에서 가져오고, 이 모듈에서는 `get_shifts_service` ·
      `get_shifts_paged_service` · `_to_time_str` 만 쓴다(전수 확인).
      같은 이름의 쓰기 함수가 두 모듈에 있어 실제로 **엉뚱한 쪽을 고치는 사고가 난 적이 있다.**
      고칠 일이 생기면 먼저 어느 쪽이 배선돼 있는지 확인할 것. 배선된 쪽은 `shift_service.py` 다.
    """
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

        # 필수 필드(스키마상 항상 실려 온다).
        existing_shift.shift_id = req.shift_id
        existing_shift.name = req.name
        existing_shift.color = req.color
        existing_shift.type = req.type
        # ★ Optional 필드는 `services.shift_service.update_shift_service` 와 **같은 계약**을
        #   따른다 — 미전송이면 기존 값 유지, 명시적 null 이면 해제. 무조건 대입하면 필수
        #   필드만 실은 부분 수정 요청이 근무시간·설명을 지우고, `shift_gb` 는 값이 달라지는
        #   바람에 `shift_manage` 에서 코드까지 사라진다.
        #   두 구현의 계약이 갈리면 이 쪽이 재배선되는 순간 같은 사고가 되살아난다.
        _sent = getattr(req, "model_fields_set", ())
        for _f in ("start_time", "end_time", "duration", "allday",
                   "auto_schedule", "shift_gb", "description", "default_shift"):
            if _f in _sent:
                setattr(existing_shift, _f, getattr(req, _f))
        # 배선본(shift_service.update_shift_service)과 같은 형태로 맞춘다.
        if getattr(req, "show_in_preference", None) is not None:
            existing_shift.show_in_preference = req.show_in_preference
        # ★ 타깃 3종(off_swap · health_leave · sleep_off)은 add 와 같은 이유로 일부러 뺐다 —
        #   이 모듈에 검증이 없어 대입만 넣으면 그룹당 1건 제약이 뚫린다.
        effective_shift_gb = existing_shift.shift_gb

        session.commit()
        session.refresh(existing_shift)
        _append_shift_manage_code(
        session=session,
        office_id=existing_shift.office_id,
        group_id=current_user.group_id,
        shift_id=existing_shift.shift_id,
        shift_gb=effective_shift_gb,
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
            "description": existing_shift.description,
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
        # ★ 삭제 시 `shift_manage.codes` 에서도 빼야 한다. 없으면 지워진 근무코드 문자열이
        #   배열에 **영구 고아**로 남는다 — FK 가 없어 DB 오류도 안 나고, 남은 고아는 그 슬롯의
        #   대체코드로 읽혀 커버리지·수요 계산과 솔버 입력에 조용히 섞인다. 지울 경로가 따로
        #   없어 사후 복구가 어렵다.
        #   ★ 이 헬퍼를 이 모듈에 **복사하지 말 것.** 복사하면 '슬롯 한정' 옛 버전이 되살아나
        #     배선본이 이미 되돌린 고아 버그가 다시 생긴다. 배선본 것을 그대로 쓴다.
        #   커밋하지 않고 dirty 만 만들므로 아래 `session.commit()` 하나에 함께 묶인다.
        from services.shift_service import _remove_shift_manage_code
        _remove_shift_manage_code(
            session,
            existing_shift.office_id,
            current_user.group_id,
            existing_shift.shift_id,
            getattr(existing_shift, "shift_gb", None),
        )
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
