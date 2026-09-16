from typing import List, Dict, Any


import os
import base64
import json
import datetime as _dt

from db.models import Shift, Nurse, Group, Office, ScheduleEntry, ShiftManage
from services.shift_manage_defaults import ensure_default_shift_manage
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy import create_engine, func, or_, and_, case
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
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


def _LOG_TRIM(exc: BaseException, limit: int = 300) -> str:
    """예외 문자열을 로그용으로 자른다.

    DB 예외 문자열에는 SQL 문 전체와 바인딩 파라미터가 통째로 들어간다. 그대로 찍으면
    반복 실패 시 운영 로그가 순식간에 불어나고, 파라미터에 섞인 값이 로그로도 퍼진다.
    """
    s = str(exc).replace("\n", " ")
    return s if len(s) <= limit else s[:limit] + "…(생략)"


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
    commit: bool = True,
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

    if (removed_any or added_any) and commit:
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

        # ★★ office_id 는 **여기서 한 번만** 정하고, 아래 모든 조회·INSERT·shift_manage
        #   동기화가 같은 값을 쓴다. 예전엔 조회는 `current_user.office_id`(토큰)로 하고
        #   기본코드 INSERT 는 `groups.office_id` 로 해서 **읽는 키와 쓰는 키가 달랐다.**
        #   병동 이동이 `group_id` 만 갱신하고 `nurses.office_id` 를 두는 경로가 실재해
        #   토큰 office 가 낡으면, 조회는 0건인데 쓰기는 기존 행과 같은 키가 되어
        #   매 호출 같은 자리에서 충돌한다(UNIQUE 부착 후엔 영구 500).
        #   권위는 `groups.office_id` 다 — 토큰은 낡을 수 있어도 그룹 행은 사실이다.
        office_id = None
        group = session.query(Group).filter(Group.group_id == group_id).first() if group_id else None
        if group and group.office_id:
            office_id = group.office_id
        elif getattr(current_user, "office_id", None):
            office_id = current_user.office_id
        else:
            nurse = session.query(Nurse).filter(Nurse.nurse_id == current_user.nurse_id).first()
            if nurse and nurse.group and nurse.group.office_id:
                office_id = nurse.group.office_id

        def _list_shifts():
            return (
                session.query(Shift)
                .filter(Shift.office_id == office_id, Shift.group_id == group_id)
                .order_by(*SHIFT_LIST_ORDER)
                .all()
            )

        def _ensure_manage_rows(required_main_codes: tuple[str, ...] = ()) -> None:
            """`shift_manage` 슬롯 **행 자체**를 보장한다(codes 채우기 전 단계).

            ★★ `_append_shift_manage_code` 는 **기존 행의 codes 를 UPDATE 할 뿐 행을 만들지
              않는다.** 그래서 슬롯이 0건인 신규 병동에서는 루프가 한 번도 안 돌고, 아무
              것도 안 바뀐 채 조용히 성공한다 — 근무코드는 깔렸는데 `shift_manage` 는 빈
              상태로 200 이 나가고, 그 병동은 **근무표 생성에서야** 막힌다.
              `GET /shifts` 는 이 함수를 부른 적이 없었다(부르는 곳은 `/shift-manage` 와
              `daily_shift` 뿐이라, 근무코드 화면을 먼저 연 병동은 골격 없이 남았다).
            ★ nurse_class 는 AN 병동도 'RN' 이다(슬롯 테이블의 관례).
            ★ 자체 commit + IntegrityError 흡수라 여러 번 불러도 안전하고,
              행이 하나라도 있으면 no-op 이다.
            """
            if not office_id:
                # ★★ 여기서 조용히 넘어가면 안 된다. `shifts.office_id` 는 nullable 이라
                #   `_mk` 가 그대로 None 을 넣어 **어느 오피스에도 안 붙은 시프트**가
                #   커밋되고, `_append_shift_manage_code` 도 office 가 없어 no-op 이 되어
                #   shift_manage 는 빈 채 200 이 나간다 — 이 패치가 막으려던 반쪽 시딩이
                #   그대로 재현된다. UNIQUE(office_id, group_id, shift_id) 도 NULL 이 섞이면
                #   테넌트 귀속을 보장하지 못한다.
                #   groups.office_id · 토큰 · nurse 폴백이 모두 비는 degraded 경로이므로
                #   **아무 것도 만들지 않고 중단**한다.
                print(f"[get_shifts_service_mssql][ERROR] office_id 확정 실패 — "
                      f"group={group_id}. 시프트를 만들지 않고 중단한다.")
                raise RuntimeError(
                    "근무코드 설정을 준비하지 못했습니다. 관리자에게 문의해 주세요."
                )
            ensure_default_shift_manage(session, office_id, group_id, "RN")
            if not required_main_codes:
                return
            # ★★ `ensure_default_shift_manage` 는 commit 의 IntegrityError 를 **무조건
            #   흡수하고 정상 반환**한다(그 docstring 이 "재조회는 호출측 책임" 이라고
            #   명시한다). 경쟁이면 남이 만들어 뒀겠지만 FK·NOT NULL·스키마 불일치면
            #   슬롯이 없는 채로 넘어오고, 그러면 아래 `_stage_manage` 가 갱신할 행이
            #   없어 조용히 no-op 이 된다 — 시프트만 커밋되고 shift_manage 는 빈 채
            #   200 이 나가는 그 상태다. **필요한 슬롯이 실제로 생겼는지 확인한다.**
            #   확인은 씨딩 경로에서만 한다 — 이미 운영 중인 부분등록 병동의 단순 조회를
            #   막으면 정작 고칠 화면조차 못 연다.
            _have = {
                r[0]
                for r in session.query(ShiftManage.main_code)
                .filter(
                    ShiftManage.office_id == office_id,
                    ShiftManage.group_id == group_id,
                    ShiftManage.nurse_class == "RN",
                )
                .all()
            }
            _lack = [c for c in required_main_codes if c not in _have]
            if _lack:
                # ★ 식별자는 **로그에만** 남긴다. 라우터 catch-all 이 `str(e)` 를 그대로
                #   detail 에 실어 내보내므로, 예외 문구에 office/group 을 넣으면
                #   클라이언트 응답으로 새어 나간다.
                print(f"[get_shifts_service_mssql][ERROR] shift_manage 슬롯 생성 실패 — "
                      f"office={office_id} group={group_id} 없는 슬롯={_lack}")
                raise RuntimeError(
                    "근무코드 설정을 준비하지 못했습니다. 관리자에게 문의해 주세요."
                )

        def _stage_manage(code_gb_pairs) -> None:
            """`shift_manage.codes` 변경을 **커밋하지 않고 세션에 올린다.**

            ★ 근무코드 INSERT 와 **같은 커밋**에 묶기 위해 `commit=False` 로 부른다.
              따로 커밋하면 "근무코드는 생겼는데 shift_manage 는 비어 있는" 반쪽 상태가
              200 뒤에 남고, 그 병동은 **근무표 생성 단계에 가서야** 막힌다
              (신규 그룹 shift_manage 0건 사고). 실패하면 둘 다 되돌아가야 한다.
            ★ `_append_shift_manage_code` 는 이미 있으면 안 넣는다 — 여러 번 불러도 안전하다.
            """
            for _sid, _gb in code_gb_pairs:
                _append_shift_manage_code(
                    session=session,
                    office_id=office_id,
                    group_id=group_id,
                    shift_id=_sid,
                    shift_gb=_gb,
                    commit=False,
                )

        def _commit_or_recover(code_gb_pairs) -> bool:
            """시프트 INSERT + shift_manage 변경을 한 번에 커밋한다.

            충돌(경쟁 요청이 먼저 깔았다)이면 되돌리고, **이미 있는 행 기준으로
            shift_manage 만 다시 맞춰** 커밋한다. 두 번째도 실패하면 예외를 올린다 —
            조용히 200 을 돌려주면 반쪽 상태가 그대로 굳는다.
            반환: True=이 요청이 만들었다 / False=경쟁에서 져서 남의 것을 쓴다.
            """
            try:
                session.commit()
                return True
            except IntegrityError as _exc:
                session.rollback()
                # ★★ 모든 IntegrityError 를 "경쟁에서 졌다" 로 읽으면 안 된다.
                #   FK·NOT NULL 같은 **다른 위반도 같은 예외 타입**으로 온다. 그 경우
                #   시프트는 통째로 되돌아갔는데 codes 만 쓰면, 존재하지 않는 시프트를
                #   가리키는 **고아 코드**가 shift_manage 에 남고 200 이 나간다.
                #   드라이버 에러코드를 파싱하는 대신 **원하는 결과가 실제로 이뤄졌는지**
                #   를 본다 — 경쟁이면 남이 만들어 뒀을 것이고, 다른 위반이면 없다.
                # ★★ shift_gb 까지 함께 읽는다. 코드 문자만 확인하면, 경쟁 승자가 같은
                #   shift_id 를 **다른 분류로** 만든 경우(예: 사용자가 /shifts/add 로 'M' 을
                #   shift_gb='고정' 으로 생성)를 "기본 시딩 성공" 으로 오인해, 내가 들고 있던
                #   기본 pair(M·미드)로 슬롯 5 에 등록해 버린다 — 실제 분류와 다른 슬롯에
                #   코드가 박히거나 같은 코드가 여러 슬롯에 남는다.
                _rows = (
                    session.query(Shift.shift_id, Shift.shift_gb)
                    .filter(Shift.office_id == office_id, Shift.group_id == group_id)
                    .all()
                )
                _present = {r[0] for r in _rows}
                _gb_by_id = {r[0]: r[1] for r in _rows}
                _missing = [sid for sid, _ in code_gb_pairs if sid not in _present]
                if _missing:
                    # ★ 원래 예외를 **그대로 올리지 않는다.** 라우터 catch-all 이
                    #   `str(e)` 를 HTTP detail 에 싣는데, MSSQL IntegrityError 문자열에는
                    #   SQL 문·제약명·바인딩 파라미터가 들어 있어 office_id·group_id 가
                    #   그대로 새어 나간다 — 위에서 문구를 일반화한 조치가 무의미해진다.
                    #   진단은 로그에 남기고 바깥으로는 일반 문구만 보낸다.
                    #   (라우터가 더 이상 재시도하지 않으므로 타입을 바꿔도 오분류는 없다.)
                    print(f"[get_shifts_service_mssql][ERROR] 시프트 생성 실패 — "
                          f"office={office_id} group={group_id} 없는 코드={_missing} "
                          f"원인={type(_exc).__name__}: {_LOG_TRIM(_exc)}")
                    raise RuntimeError(
                        "근무코드를 준비하지 못했습니다. 관리자에게 문의해 주세요."
                    ) from _exc
                # ★ 내가 들고 있던 기본 pair 가 아니라 **실제 저장된 shift_gb** 로 동기화한다.
                #   경쟁 승자가 다른 분류로 만들었으면 그 분류를 따르는 것이 맞다 —
                #   DB 에 있는 행이 사실이고 내 기본값은 추정일 뿐이다.
                _actual_pairs = [(sid, _gb_by_id.get(sid)) for sid, _ in code_gb_pairs]
                _diff = [(sid, gb, _gb_by_id.get(sid))
                         for sid, gb in code_gb_pairs if _gb_by_id.get(sid) != gb]
                if _diff:
                    print(f"[get_shifts_service_mssql][WARN] 경쟁 승자의 분류가 기본값과 다름 — "
                          f"office={office_id} group={group_id} "
                          f"{[(s, f'기본={g}', f'실제={a}') for s, g, a in _diff]}")
                _stage_manage(_actual_pairs)
                # ★ 두 번째 커밋도 감싼다. 감싸지 않으면 여기서 난 DB 예외가 라우터
                #   catch-all 의 `str(e)` 를 타고 SQL 문·제약명·바인딩 파라미터째
                #   응답에 실린다 — 위에서 문구를 일반화한 것이 무의미해진다.
                try:
                    session.commit()
                except Exception as _exc2:
                    session.rollback()
                    print(f"[get_shifts_service_mssql][ERROR] 복구 커밋 실패 — "
                          f"office={office_id} group={group_id} "
                          f"원인={type(_exc2).__name__}: {_LOG_TRIM(_exc2)}")
                    raise RuntimeError(
                        "근무코드를 준비하지 못했습니다. 관리자에게 문의해 주세요."
                    ) from _exc2
                return False

        shifts = _list_shifts()

        # ★★ 이 함수는 조회 엔드포인트(GET /shifts)인데 아래에서 **INSERT + commit 을 한다.**
        #   그래서 대상 병동이 정해지지 않은 호출(ADM 이 병동 지정 없이 목록을 여는 경우
        #   `current_user.group_id` 가 빈 문자열이다)에서도 그 빈 값으로 근무코드가 깔렸다.
        #   실측: 운영에 `group_id=''` 행이 **12개 오피스에 4~6개씩, 합 62행** 쌓여 있다.
        #   어느 병동에도 속하지 않아 화면에 안 보이고 지울 경로도 없는 유령 행이다.
        #   병동이 정해지지 않았으면 있는 것만 돌려주고 **만들지 않는다.**
        if not group_id:
            return [_shift_row_to_dict(s) for s in shifts]

        # ★ 슬롯 골격은 **시프트를 세션에 올리기 전에** 보장한다(각 씨딩 분기 첫 줄).
        #   `ensure_default_shift_manage` 는 자체 commit 을 하므로, 시프트 add 뒤에 부르면
        #   그 add 까지 함께 커밋돼 "시프트+codes 한 커밋" 원자성이 깨진다.

        if shifts:
            # ★★ 이 씨딩은 **사전 세팅을 깔아 주는 것**이지 'M' 이라는 이름을 강제하는 게
            #   아니다. 병원이 자기 EMR 코드로 바꾸면(예: 성남시의료원 M→D2) 이름은
            #   달라져도 MID 자리는 이미 있으므로 **다시 만들 이유가 없다.**
            # ★ 판정은 **두 축을 모두** 본다 — 어느 쪽으로든 MID 가 있으면 안 만든다.
            #   예전엔 판정만 `default_shift == 'M'` 이고 삽입은 `shift_id='M'` 이라
            #   축이 어긋나, shift_id='M' 인데 default_shift 가 M 이 아닌 병동에서
            #   **목록을 열 때마다 'M' 이 한 행씩 늘었다.** 한쪽만 보면 그 사고가 되살아나고,
            #   shift_id 만 보면 코드명을 바꾼 병동에서 유령 'M' 이 생긴다.
            has_mid = any(
                str(getattr(s, "shift_id", "") or "").upper() == "M"
                or str(getattr(s, "default_shift", "") or "").upper() == "M"
                for s in shifts
            )
            if not has_mid:
                _ensure_manage_rows(("M",))     # add 보다 앞. 슬롯 없으면 여기서 중단
                mid_shift = Shift(
                    shift_id="M",
                    name="미드",
                    office_id=office_id,
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
                # ★ check-then-insert 다 — `has_mid` 는 위에서 뜬 스냅샷이고 잠금이 없다.
                #   같은 병동 목록 요청이 겹치면 둘 다 "M 없음" 을 읽고 둘 다 넣는다.
                #   프론트가 `["SHIFTS", groupId]` 를 키로 쓰는데 비관리자는 groupId=null 로,
                #   다른 화면은 실제 gid 로 불러 중복제거가 안 걸린다. 반면 서버는 falsy
                #   group_id 를 `current_user.group_id` 로 떨어뜨려 같은 병동으로 수렴한다.
                #   경쟁 요청이 이미 만들어 둔 상태가 곧 원하던 결과다 — 실패가 아니라
                #   되돌리고 다시 읽으면 된다. 조회가 경쟁 때문에 500 이 되면 안 된다.
                _stage_manage([("M", "미드")])          # 같은 커밋에 함께 올린다
                _commit_or_recover([("M", "미드")])
                shifts = _list_shifts()
            else:
                # ★ 이미 근무코드가 있는 병동도 **매번 멱등 복구**를 한다.
                #   과거에 시딩은 됐는데 shift_manage 동기화만 실패한 병동이 있으면,
                #   여기를 지나지 않는 한 스스로 낫지 못하고 근무표 생성에서야 막힌다.
                # ★★ 여기서 `shift_manage` 자가치유를 **하지 않는다.**
                #   한때 매 GET 마다 기존 코드들을 다시 동기화하게 만들었는데,
                #   `codes` 는 JSON 배열을 파이썬에서 읽어 통째로 다시 쓰는 구조라
                #   행 잠금이 없다. 그러면 **조회가 쓰기 경쟁자가 되어**, 같은 슬롯을
                #   고치는 사용자 요청과 겹칠 때 나중에 커밋한 쪽이 상대 코드를
                #   조용히 지운다(lost update). 조회 한 번이 남의 편집을 날리는 건
                #   고치려던 문제보다 나쁘다.
                #   시딩 실패는 이제 위에서 예외로 막으므로 **새로 깨진 병동은 안 생긴다.**
                #   이미 깨진 병동은 `/shift-manage` 화면이나 일회성 보정으로 고친다 —
                #   데이터 보정은 조회 엔드포인트가 할 일이 아니다.
                pass
            # print('shifts', [s.__dict__ for s in shifts])

            return [_shift_row_to_dict(s) for s in shifts]


        # 2) 기본값 생성 (오피스/그룹은 존재한다고 가정; 없으면 office_id=None로 저장)
        #   ★ office_id 는 함수 머리에서 이미 정했다(위 주석 참조). 여기서 다시 구하면
        #     조회와 다른 값이 될 수 있어 옛 비대칭이 되살아난다.
        print('[get_shifts_service_mssql] group_id', group_id, 'office_id', office_id)

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
        # ★ 삽입 직전에 **실제 유일키**로 한 번 더 거른다. 위 조회가 0건이었어도
        #   그 사이 다른 요청이 깔았을 수 있고, `import_shifts_to_group` 이 이미 쓰는
        #   (office_id, group_id, shift_id) 기준과 맞춘다.
        _ensure_manage_rows(("D", "E", "N", "M"))   # add 보다 앞. 슬롯 없으면 여기서 중단
        _existing_ids = {
            r[0]
            for r in session.query(Shift.shift_id)
            .filter(Shift.office_id == office_id, Shift.group_id == group_id)
            .all()
        }
        for args in defaults:
            if args[0] in _existing_ids:
                continue
            session.add(_mk(*args))
        # ★ 신규 병동 첫 진입에서 목록 요청이 겹치면 양쪽이 0건을 읽고 각자 6행을 넣는다
        #   (온보딩마다 열리는 창이다). shift_manage 까지 한 커밋에 묶어,
        #   지면 통째로 되돌리고 남이 깔아 둔 것 기준으로 동기화만 다시 맞춘다.
        _pairs = [(a[0], a[-1]) for a in defaults]
        _stage_manage(_pairs)
        _commit_or_recover(_pairs)

        shifts = _list_shifts()
        return [_shift_row_to_dict(s) for s in shifts]
    except RuntimeError:
        # 위에서 이미 로그를 남기고 일반 문구로 바꾼 것 — 그대로 통과시킨다.
        raise
    except SQLAlchemyError as _db_exc:
        # ★★ **DB 예외 그물.** 이 함수는 조회이면서 시딩·트랜잭션·테넌트 귀속을 함께 지고
        #   있어서, 커밋뿐 아니라 **암시적 flush(autoflush)·헬퍼 내부 쿼리**에서도 DB 예외가
        #   난다. 문장을 하나씩 감싸는 방식으로는 매번 인접한 자리가 새로 열렸다.
        #   여기서 한 번에 받아 **되돌리고 일반 문구로 바꾼다** — 라우터 catch-all 이
        #   `str(e)` 를 응답 detail 에 싣기 때문에, 원문이 나가면 SQL 문·제약명·바인딩
        #   파라미터가 그대로 노출된다.
        #   (원래 이 자리는 `finally: pass` 였다 — 예외를 잡지도 되돌리지도 않는 죽은
        #    블록이라 모듈 머리의 부활 체크리스트 7번이 정리 대상으로 지목해 두었다.)
        try:
            session.rollback()
        except Exception:
            pass
        print(f"[get_shifts_service_mssql][ERROR] DB 오류 — group={group_id} "
              f"원인={type(_db_exc).__name__}: {_LOG_TRIM(_db_exc)}")
        raise RuntimeError(
            "근무코드를 준비하지 못했습니다. 관리자에게 문의해 주세요."
        ) from _db_exc


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
