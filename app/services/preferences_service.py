"""
간호사 선호도(Preferences) 관련 서비스 로직 모듈
- DB 쿼리, 데이터 가공 등 라우터에서 분리
- 모든 함수는 한글 docstring, 한글 print/logging, PEP8 스타일 적용
"""
import calendar
import json
import pprint
from sqlalchemy.orm import Session
from sqlalchemy import String, and_, cast, extract, inspect as sa_inspect, or_
from db.models import WantedRequest, Nurse, NurseShiftRequest, NursePairRequest, ShiftPreference, Shift, WantedConfig, Wanted, WantedMonthlyMemo
from schemas.roster_schema import PreferenceData, PreferenceSubmit
from schemas.auth_schema import User as UserSchema
from services.group_access import resolve_home_group_id
from datetime import datetime, timezone, timedelta, date


# ──────────────────────────── 도메인 예외 ────────────────────────────
# 라우터에서 HTTP 상태로 매핑한다(403/409/422). 그 외 예외는 기존대로 500.
class PreferenceForbiddenError(Exception):
    """역할 또는 그룹 불일치 (403)."""


class PreferenceConflictError(Exception):
    """마감되었거나 이미 제출된 원티드 (409)."""


class PreferenceValidationError(Exception):
    """원티드 엔트리 검증 실패 (422). code 로 사유를 구분한다."""

    def __init__(self, message: str, code: str = "invalid_entry", detail=None):
        super().__init__(message)
        self.code = code
        #: 화면이 어디를 짚어 줄지 알 수 있게 하는 부가 정보(위반 날짜 등). 없으면 None.
        self.detail = detail


def _now_kst() -> datetime:
    """KST 기준 naive 현재시각.

    wanted.exp_date 는 KST naive 로 저장되는데 API 컨테이너는 TZ 미설정(UTC)이라
    datetime.now() 로 비교하면 마감이 9시간 느슨해진다. close-expired 라우터와
    동일하게 UTC+9 로 맞춘다.
    """
    return (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=9))


def _carry_forward_pair_data(db: Session, nurse_id: str, current_request_id: int, month_str: str, *, group_id: str):
    """
    이전 request_id의 pair(선호/비선호) 데이터를 현재 request_id로 복사합니다.
    현재 request_id에 pair 데이터가 이미 있으면 복사하지 않습니다.

    인자:
        db: DB 세션
        nurse_id: 간호사 ID
        current_request_id: 현재(새) request_id
        month_str: 'YYYY-MM'

    반환:
        복사된 pair 건수
    """
    # 현재 request_id에 이미 pair 데이터가 있으면 복사 불필요
    existing_count = db.query(NursePairRequest).filter(
        NursePairRequest.nurse_id == nurse_id,
        NursePairRequest.request_id == current_request_id,
        NursePairRequest.month == month_str,
    ).count()
    if existing_count > 0:
        print(f"[pair carry-forward] 현재 request_id={current_request_id}에 이미 {existing_count}건 존재 → 스킵")
        return 0

    # 같은 nurse_id/month에서 현재보다 작은 request_id 중 pair 데이터가 있는 가장 큰 것을 찾기
    prev_request_ids = (
        db.query(WantedRequest.request_id)
        .filter(
            WantedRequest.nurse_id == nurse_id,
            WantedRequest.month == month_str,
            WantedRequest.request_id < current_request_id,
        )
        .order_by(WantedRequest.request_id.desc())
        .all()
    )

    for (prev_rid,) in prev_request_ids:
        pair_rows = db.query(NursePairRequest).filter(
            NursePairRequest.nurse_id == nurse_id,
            NursePairRequest.request_id == prev_rid,
            NursePairRequest.month == month_str,
        ).all()

        if pair_rows:
            detailed_id = 1
            for row in pair_rows:
                db.add(NursePairRequest(
                    nurse_id=nurse_id,
                    request_id=current_request_id,
                    month=month_str,
                    detailed_request_id=detailed_id,
                    target_id=row.target_id,
                    group_id=group_id,
                    score=row.score,
                    partial_request=row.partial_request,
                ))
                detailed_id += 1
            print(f"[pair carry-forward] request_id {prev_rid} → {current_request_id}: {detailed_id - 1}건 복사 완료")
            return detailed_id - 1

    print(f"[pair carry-forward] 복사할 이전 pair 데이터 없음 (nurse_id={nurse_id}, month={month_str})")
    return 0


# ──────────────────── wanted_entries 단일 원본 경로 ────────────────────
# 임시저장(POST /preferences) 1회 · 제출(POST /preferences/submit) 1회로 저장과
# 제출을 원자 처리한다. /wanted/invoke(AIDE 자연어 분석) 를 거치지 않는다.


def _resolve_write_group_id(db: Session, current_user: UserSchema, requested_group_id) -> str:
    """저장·제출 대상 그룹. 본인 원티드이므로 home group 이 유일한 정답이다."""
    home_gid = resolve_home_group_id(db, current_user)
    if not home_gid:
        raise PreferenceForbiddenError("소속 그룹을 확인할 수 없습니다.")
    if requested_group_id and str(requested_group_id) != str(home_gid):
        raise PreferenceForbiddenError("본인 소속 그룹의 원티드만 저장할 수 있습니다.")
    return str(home_gid)


def _resolve_read_group_id(db: Session, current_user: UserSchema, requested_group_id) -> str:
    """조회 대상 그룹.

    /preferences/latest 는 '본인' 원티드를 돌려주므로 마감·한도 스코프는 항상 home
    group 이다. requested_group_id 는 접근 가능한 그룹인지 확인용으로만 쓴다
    (HN 이 관리 그룹으로 전환한 채 화면을 여는 경우가 있어 403 을 걸지 않는다).
    """
    from services.group_access import assert_caller_can_access_group

    if requested_group_id:
        assert_caller_can_access_group(db, current_user, str(requested_group_id))
    home_gid = resolve_home_group_id(db, current_user)
    if not home_gid:
        raise PreferenceForbiddenError("소속 그룹을 확인할 수 없습니다.")
    return str(home_gid)


def _load_wanted_window(db: Session, group_id: str, year: int, month: int):
    """해당 그룹/월의 원티드 요청(Wanted) 행. 없으면 None."""
    return (
        db.query(Wanted)
        .filter(Wanted.group_id == group_id, Wanted.year == year, Wanted.month == month)
        .first()
    )


def _is_wanted_closed(wanted) -> bool:
    """마감 여부 — status='closed' 이거나 exp_date 경과."""
    if wanted is None:
        return False
    if str(wanted.status or "") == "closed":
        return True
    return bool(wanted.exp_date and wanted.exp_date < _now_kst())


def _latest_submitted_request(db: Session, nurse_id: str, month_str: str):
    """해당 간호사/월의 최신 제출본. 없으면 None."""
    return (
        db.query(WantedRequest)
        .filter(
            WantedRequest.nurse_id == nurse_id,
            WantedRequest.month == month_str,
            WantedRequest.is_submitted == True,
        )
        .order_by(WantedRequest.submitted_at.desc())
        .first()
    )


def _assert_wanted_writable(wanted, year: int, month: int, submitted_wr) -> None:
    """마감·중복제출 게이트. 위반 시 409."""
    if wanted is None:
        raise PreferenceConflictError(
            f"{year}년 {month}월 원티드 요청이 아직 생성되지 않았습니다."
        )
    if _is_wanted_closed(wanted):
        raise PreferenceConflictError(f"{year}년 {month}월 원티드가 마감되었습니다.")
    if submitted_wr is not None:
        raise PreferenceConflictError(
            "이미 제출된 원티드입니다. 제출을 철회한 뒤 다시 저장해주세요."
        )


def _normalize_wanted_entries(entries, year: int, month: int, allowed_shift_ids: set) -> list[dict]:
    """wanted_entries 를 검증·정규화한다(날짜 오름차순, 날짜별 1건).

    intent 는 'wanted'(선호) 또는 'avoid'(기피). 날짜당 한 건이므로 같은 날 선호와
    기피를 동시에 낼 수 없다(프론트 편집기도 같은 규칙).

    Raises:
        PreferenceValidationError: 월 불일치 / 날짜 중복 / 미허용 근무코드.
    """
    normalized: dict = {}
    for item in entries or []:
        entry_date = item.date
        if entry_date.year != year or entry_date.month != month:
            raise PreferenceValidationError(
                f"{year}년 {month}월에 속하지 않는 날짜입니다: {entry_date.isoformat()}",
                code="date_out_of_range",
            )
        if entry_date in normalized:
            raise PreferenceValidationError(
                f"같은 날짜가 중복되었습니다: {entry_date.isoformat()}",
                code="duplicate_date",
            )
        shift_id = (item.shift_id or "").strip()
        if shift_id not in allowed_shift_ids:
            raise PreferenceValidationError(
                f"원티드에 사용할 수 없는 근무코드입니다: {item.shift_id}",
                code="invalid_shift_id",
            )
        normalized[entry_date] = {
            "date": entry_date,
            "shift_id": shift_id,
            "intent": item.intent,
            "comment": item.comment or "",
        }
    return [normalized[key] for key in sorted(normalized)]


# ── 기피근무(avoid) — banned_wanted_entries 재사용 ─────────────────────────
# 저장소·솔버 하드제약(initial_constraints.forbidden → X==0)을 금지 원티드와 공유한다.
# 구분은 source='nurse'. HN 조정판 저장/리셋은 source='hn' 만 건드리므로 서로 안 지운다.
#
# ★★ 간호사 기피는 **요청**이고, 확정 선택자는 수간호사다.
#   source='nurse' 인 동안에는 **생성에 반영되지 않는다**
#   (roster_create_service._load_banned_wanted 가 hn 스코프만 읽는다).
#   수간호사가 조정판에서 저장하면 그 셀이 source='hn' 으로 **승격**되고,
#   그때부터 솔버에 걸린다. 즉 "조정판 저장" 이 확정 행위다.
#   ★ 게이트를 is_applied 가 아니라 source 에 건 이유 — 조정판은 출처 구분 없이
#     전부 노출하고 프론트는 is_applied 로 X 아이콘만 그린다. is_applied 로 막으면
#     "간호사가 새로 낸 것" 과 "수간호사가 끈 것" 이 같은 X 로 보여 구분이 안 된다.
#   ★ 승격 후에는 간호사가 그 셀을 못 고친다(hn_cells 스킵 → avoid_blocked 통지).


_AVOID_TABLE_READY = False


def _avoid_storage_ready(db: Session) -> bool:
    """banned_wanted_entries 저장소가 쓸 수 있는 상태인지.

    테이블 존재만으로는 부족하다 — source 컬럼(출처 구분)이 없으면 모든 쿼리가
    'Invalid column name' 으로 깨진다. 실제로 dev 에 컬럼 없는 테이블이 먼저 생겼다.
    DDL 미적용 환경에서 '기피근무가 없는' 저장까지 깨지지 않도록 미리 확인한다.
    실패한 문장은 MSSQL 트랜잭션을 오염시켜 이후 문장까지 막으므로, 예외를 삼키는
    방식으로는 해결되지 않는다(그래서 사후 try/except 가 아니라 사전 확인이다).

    True 는 한 번 확인되면 캐시한다. False 는 캐시하지 않는다 — DDL 적용 후 서버
    재기동 없이 바로 반영되도록.
    """
    global _AVOID_TABLE_READY
    if _AVOID_TABLE_READY:
        return True
    try:
        inspector = sa_inspect(db.get_bind())
        if not inspector.has_table("banned_wanted_entries"):
            return False
        columns = {c["name"] for c in inspector.get_columns("banned_wanted_entries")}
        _AVOID_TABLE_READY = "source" in columns
        if not _AVOID_TABLE_READY:
            print("[avoid] banned_wanted_entries.source 컬럼 없음 — 기피근무 저장 비활성")
    except Exception as e:  # 메타데이터 조회 실패 — 기피근무 미사용으로 취급
        print(f"[avoid] 저장소 확인 실패(미사용 처리): {e}")
        return False
    return _AVOID_TABLE_READY


def _resolve_main_code(db: Session, group_id: str, shift_id: str) -> str:
    """근무코드를 솔버가 아는 main code(D/E/N/M/O ...)로 정규화한다.

    shifts.default_shift 가 main code 이고, 없으면 shift_id 자신이 main code 이다.
    (예: 'D1' → default_shift 'D')
    """
    row = (
        db.query(Shift.default_shift)
        .filter(Shift.group_id == group_id, Shift.shift_id == shift_id)
        .first()
    )
    main = (row[0] if row and row[0] else shift_id) or ""
    return str(main).strip().upper()


def _validate_avoid_entries(db: Session, nurse_id: str, group_id: str, avoid: list[dict]) -> list[dict]:
    """기피근무 검증. 반환은 [{date, main_code, shift_id, comment}].

    규칙은 HN 조정판의 금지 원티드와 **같은 소스**를 쓴다
    (`wanted_service._ward_main_codes` — 병동에 실존하는 근무형, OFF 포함).
    - 병동에 없는 코드는 거부. 있는 코드는 OFF 도 금지 가능하다.
    - 금지 + 개인 허용근무(allowed_shifts) 제한 후에도 배정 옵션이 **하나는 남아야** 한다.
      OFF 는 금지하지 않는 한 항상 옵션이다.
    """
    from services.wanted_service import _ward_main_codes

    if not avoid:
        return []
    nurse_row = db.query(Nurse).filter(Nurse.nurse_id == nurse_id).first()
    allowed = {
        str(x).strip().upper()
        for x in (getattr(nurse_row, "allowed_shifts", None) or [])
    }
    ward_mains = _ward_main_codes(db, group_id)
    work_mains = ward_mains - {"O"}
    avail_work = (work_mains & allowed) if allowed else set(work_mains)

    resolved: list[dict] = []
    for entry in avoid:
        main_code = _resolve_main_code(db, group_id, entry["shift_id"])
        if main_code not in ward_mains:
            raise PreferenceValidationError(
                f"병동에 없는 근무코드입니다: {entry['shift_id']}",
                code="unknown_shift_code",
            )
        # 날짜당 한 건이므로 이 날 금지되는 코드는 이 하나뿐이다.
        if not ((avail_work | {"O"}) - {main_code}):
            raise PreferenceValidationError(
                f"{entry['date'].isoformat()}: 이 근무를 피하면 배정 가능한 근무/OFF 가 없습니다.",
                code="no_option_left",
            )
        resolved.append({**entry, "main_code": main_code})
    return resolved


def _replace_nurse_avoid_entries(
    db: Session,
    nurse_id: str,
    group_id: str,
    year: int,
    month: int,
    avoid: list[dict],
) -> list:
    """해당 간호사/월의 기피근무를 전량 교체한다(source='nurse' 스코프).

    ★ 여기서 만든 행은 아직 **미확정**이다(source='nurse'). 생성에 반영되려면
      수간호사가 조정판에서 저장해 hn 으로 승격시켜야 한다. is_applied 는 조정판의
      표시/토글 축이므로 True 로 둔다 — False 로 넣으면 수간호사 화면에서
      "간호사가 새로 낸 것" 이 "내가 끈 것" 과 같은 X 로 보인다.

    ★ 한 셀에 hn 행과 nurse 행이 **공존하지 못하게** 막는다. 조정판은 셀당 한 건만
      그리므로(`wanted_service._banned_by_date` 가 최신 id 하나만 남김), 공존하면
      둘 중 하나는 화면에 안 뜨는데 솔버는 둘 다 하드로 건다(컨버터가 코드 합집합).
      즉 **수간호사가 보지도 못하고 지우지도 못하는 금지**가 생긴다.
      수간호사가 이미 지정한 셀이면 간호사 기피는 만들지 않고 건너뛴다
      (반대 방향은 `save_banned_wanted_service` 의 nurse 소유 셀 가드가 막는다).

    반환: 수간호사 지정과 겹쳐 반영하지 못한 날짜 목록.
    """
    from db.models import BannedWantedEntry
    from services.wanted_service import (
        BANNED_SOURCE_HN,
        BANNED_SOURCE_NURSE,
        banned_source_filter,
        precheck_forced_off_runs,
    )

    hn_rows = db.query(BannedWantedEntry).filter(
        BannedWantedEntry.group_id == group_id,
        BannedWantedEntry.year == year,
        BannedWantedEntry.month == month,
        BannedWantedEntry.nurse_id == nurse_id,
        banned_source_filter(BANNED_SOURCE_HN),
    ).all()
    hn_cells = {r.shift_date for r in hn_rows}

    # ── 사전 점검: 저장 후 상태로 본다 ──────────────────────────────────────
    # 이번 요청분만 보면 기존 3일 구간에 하루 붙이는 경우를 놓친다. 삭제 전에 본다.
    # ★ 미확정(승격 전)이라도 센다. 어차피 승격되면 걸릴 조합이라, 여기서 안 막으면
    #   수간호사가 저장을 누르는 순간 422 가 터지고 "누가 뭘 냈길래" 를 되짚어야 한다.
    post_state = {
        r.shift_date: [str(c).strip().upper() for c in (r.banned_shift_ids or [])]
        for r in hn_rows if r.is_applied
    }
    for entry in avoid:
        if entry["date"] in hn_cells:
            continue          # 수간호사 지정 셀은 아래에서 어차피 건너뛴다
        post_state[entry["date"]] = [str(entry["main_code"]).strip().upper()]
    # 확정근무가 있는 셀은 금지가 무효(생성에서 확정이 우선)라 휴무로 세면 안 된다.
    from db.models import FixedWantedEntry
    fixed_cells = {
        (str(r[0]), r[1]) for r in db.query(
            FixedWantedEntry.nurse_id, FixedWantedEntry.shift_date
        ).filter(
            FixedWantedEntry.group_id == group_id,
            FixedWantedEntry.year == year,
            FixedWantedEntry.month == month,
            FixedWantedEntry.nurse_id == nurse_id,
            FixedWantedEntry.is_applied == True,  # noqa: E712
        ).all()
    }
    violations = precheck_forced_off_runs(
        db, group_id, year, month, {nurse_id: post_state}, fixed_cells=fixed_cells
    )
    if violations:
        raise PreferenceValidationError(
            violations[0]["message"],
            code="forced_off_run",
            detail={"violations": violations},
        )

    db.query(BannedWantedEntry).filter(
        BannedWantedEntry.group_id == group_id,
        BannedWantedEntry.year == year,
        BannedWantedEntry.month == month,
        BannedWantedEntry.nurse_id == nurse_id,
        banned_source_filter(BANNED_SOURCE_NURSE),
    ).delete(synchronize_session=False)

    blocked = []
    for entry in avoid:
        if entry["date"] in hn_cells:
            blocked.append(entry["date"])
            continue
        db.add(BannedWantedEntry(
            group_id=group_id,
            year=year,
            month=month,
            nurse_id=nurse_id,
            shift_date=entry["date"],
            banned_shift_ids=[entry["main_code"]],
            # 조정판의 표시/토글 축. 확정 여부는 source 가 가른다(모듈 상단 주석).
            is_applied=True,
            source=BANNED_SOURCE_NURSE,
            reason=entry["comment"] or None,
            created_by=nurse_id,
        ))

    if blocked:
        print(f"[wanted_entries] 수간호사 지정과 겹쳐 기피 미반영: "
              f"{[d.isoformat() for d in blocked]}")
    return blocked


def _load_nurse_avoid_entries(
    db: Session, nurse_id: str, group_id: str, year: int, month: int
) -> list[dict]:
    """저장된 기피근무를 wanted_entries 형태로 되돌린다. 테이블 미생성이면 빈 목록.

    ★ **승격분(source='hn' 이지만 본인이 낸 것)도 함께 읽는다.** 수간호사가 조정판에서
      저장하면 그 셀은 hn 으로 바뀌는데, nurse 스코프만 읽으면 간호사 화면에서
      자기가 낸 기피가 **통째로 사라진다**(지운 적 없는데 없어진 것으로 보인다).
      최초 작성자는 `created_by` 에 남으므로 그걸로 되찾는다.

    ★ `approved` 를 함께 싣는다 — 승격 전에는 생성에 반영되지 않으므로,
      이걸 안 내려주면 화면엔 "반영됨"으로 보이는데 근무표에는 안 걸려
      나중에 어긋난 이유를 아무도 못 찾는다(선호에는 없는 축이라 avoid 에만 붙인다).
    """
    from db.models import BannedWantedEntry
    from services.wanted_service import BANNED_SOURCE_HN, BANNED_SOURCE_NURSE, banned_source_filter

    if not _avoid_storage_ready(db):
        return []

    rows = db.query(BannedWantedEntry).filter(
        BannedWantedEntry.group_id == group_id,
        BannedWantedEntry.year == year,
        BannedWantedEntry.month == month,
        BannedWantedEntry.nurse_id == nurse_id,
        or_(
            banned_source_filter(BANNED_SOURCE_NURSE),
            # 승격분 — 수간호사가 확정한 내 기피. created_by 로만 가려낸다.
            and_(banned_source_filter(BANNED_SOURCE_HN),
                 BannedWantedEntry.created_by == nurse_id),
        ),
    ).all()

    entries: list[dict] = []
    for row in rows:
        codes = row.banned_shift_ids
        if isinstance(codes, str):
            try:
                codes = json.loads(codes)
            except (ValueError, TypeError):
                codes = []
        for code in (codes or []):
            entries.append({
                "date": row.shift_date.isoformat(),
                "shift_id": str(code),
                "intent": "avoid",
                "comment": row.reason or "",
                #: 근무표에 실제로 걸리는 상태인가. 조정판 저장으로 hn 승격되고
                #: 적용까지 켜져 있어야 True — 승격됐어도 수간호사가 껐으면(반려) False.
                "approved": (
                    str(row.source or BANNED_SOURCE_HN) == BANNED_SOURCE_HN
                    and bool(row.is_applied)
                ),
            })
    return entries


def _assert_off_limit(entries: list[dict], off_shift_ids: set, max_requests) -> None:
    """휴무/휴가 요청 개수 상한 검증. 초과 시 422(잘라내지 않고 거절)."""
    if max_requests is None:
        return
    off_used = sum(1 for e in entries if e["shift_id"] in off_shift_ids)
    if off_used > max_requests:
        raise PreferenceValidationError(
            f"휴무/휴가 요청 가능 수({max_requests}개)를 초과했습니다. 요청 {off_used}개.",
            code="off_limit_exceeded",
        )


#: (간호사, 월) 당 보관할 미제출 draft 수. 초과분은 오래된 것부터 지운다.
#: 발화마다 스냅샷을 남기면 draft 가 계속 쌓이므로 상한이 필요하다.
_MAX_DRAFT_HISTORY = 20


def _prune_draft_history(db: Session, nurse_id: str, month_str: str) -> int:
    """미제출 draft 가 상한을 넘으면 오래된 것부터 자식 행까지 지운다.

    ★ `request_id` 는 (간호사, 월) 스코프로 채번된다 — 다른 달에 같은 번호가 있다.
      그래서 자식 행을 지울 때 **반드시 월로도 좁힌다.** (nurse_id, request_id) 만
      보면 다른 달 데이터가 같이 지워진다.
    """
    drafts = (
        db.query(WantedRequest)
        .filter(
            WantedRequest.nurse_id == nurse_id,
            WantedRequest.month == month_str,
            WantedRequest.is_submitted == False,
        )
        .order_by(WantedRequest.request_id.desc())
        .all()
    )
    stale = drafts[_MAX_DRAFT_HISTORY:]
    if not stale:
        return 0

    ids = [d.request_id for d in stale]
    year, month = int(month_str[:4]), int(month_str[5:7])
    start = date(year, month, 1)
    end = date(year, month, calendar.monthrange(year, month)[1])
    db.query(NurseShiftRequest).filter(
        NurseShiftRequest.nurse_id == nurse_id,
        NurseShiftRequest.request_id.in_(ids),
        NurseShiftRequest.shift_date >= start,
        NurseShiftRequest.shift_date <= end,
    ).delete(synchronize_session=False)
    db.query(NursePairRequest).filter(
        NursePairRequest.nurse_id == nurse_id,
        NursePairRequest.request_id.in_(ids),
        NursePairRequest.month == month_str,
    ).delete(synchronize_session=False)
    for d in stale:
        db.delete(d)
    print(f"[wanted_entries] draft 이력 정리: {len(stale)}건 삭제 "
          f"(상한 {_MAX_DRAFT_HISTORY})")
    return len(stale)


def _acquire_draft_request(
    db: Session, nurse_id: str, month_str: str, group_id: str,
    *, new_snapshot: bool = False,
):
    """미제출 draft WantedRequest 를 확보한다. 없으면 새로 만든다.

    wanted_requests.request_id 는 IDENTITY 가 아니고 복합 PK 라 SQLAlchemy 가
    자동 채번하지 않는다. 기존 채번기(_next_request_id)를 그대로 쓴다.

    ★ `new_snapshot=True` 면 기존 draft 를 재사용하지 않고 **새 request_id** 를 만든다.
      자연어 발화마다 스냅샷을 남기기 위한 것이다(예전 `/wanted/invoke` 동작).
      `/preferences` 로 경로가 합쳐지면서 draft 를 재사용하게 됐고, 그 결과
      **발화 이력이 사라졌다.** 다만 캘린더 클릭 저장(디바운스)까지 새로 만들면
      행이 폭증하므로, 호출자가 **자연어가 실려온 경우에만** 켠다.
      이전 분을 복사할 필요는 없다 — `/preferences` 는 replace-all 계약이라
      페이로드가 이미 그 달 전체 상태다.
    """
    if not new_snapshot:
        draft = (
            db.query(WantedRequest)
            .filter(
                WantedRequest.nurse_id == nurse_id,
                WantedRequest.month == month_str,
                WantedRequest.is_submitted == False,
            )
            .order_by(WantedRequest.created_at.desc())
            .first()
        )
        if draft is not None:
            return draft

    from services.wanted_service import _next_request_id

    draft = WantedRequest(
        nurse_id=nurse_id,
        request_id=_next_request_id(db, nurse_id, month_str),
        month=month_str,
        group_id=group_id,
        request="",
        is_submitted=False,
        created_at=datetime.now(),
    )
    db.add(draft)
    db.flush()
    if new_snapshot:
        _prune_draft_history(db, nurse_id, month_str)
    return draft


def _replace_shift_requests(
    db: Session,
    nurse_id: str,
    request_id: int,
    entries: list[dict],
    *,
    group_id: str,
    year: int,
    month: int,
) -> None:
    """해당 월의 nurse_shift_requests 를 entries 로 전량 교체한다."""
    start_date = date(year, month, 1)
    end_date = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    db.query(NurseShiftRequest).filter(
        NurseShiftRequest.nurse_id == nurse_id,
        NurseShiftRequest.request_id == request_id,
        NurseShiftRequest.shift_date >= start_date,
        NurseShiftRequest.shift_date < end_date,
    ).delete(synchronize_session=False)

    shift_table_ids = {
        sid: tid
        for sid, tid in db.query(Shift.shift_id, Shift.id)
        .filter(Shift.group_id == group_id)
        .all()
    }
    for detailed_id, entry in enumerate(entries, start=1):
        db.add(NurseShiftRequest(
            nurse_id=nurse_id,
            request_id=request_id,
            detailed_request_id=detailed_id,
            shift_date=entry["date"],
            group_id=group_id,
            shift=entry["shift_id"],
            score=10.0,  # 사용자 직접 입력 — /wanted/invoke 의 case 우선순위와 동일
            partial_request="",
            comment=entry["comment"],
            shifts_table_id=shift_table_ids.get(entry["shift_id"]),
        ))


def save_wanted_entries_service(
    req: PreferenceData,
    current_user: UserSchema,
    db: Session,
    *,
    is_draft: bool,
):
    """wanted_entries 를 단일 원본으로 저장한다. is_draft=False 면 제출까지 원자 처리.

    Returns:
        canonical wanted snapshot (get_latest_preference_service 와 동일 형태).
    """
    from services.wanted_service import _compute_weekly_off_days, _get_off_shift_ids

    year, month = req.year, req.month
    month_str = f"{year}-{month:02d}"
    nurse_id = current_user.nurse_id
    group_id = _resolve_write_group_id(db, current_user, req.group_id)

    wanted = _load_wanted_window(db, group_id, year, month)
    _assert_wanted_writable(
        wanted, year, month, _latest_submitted_request(db, nurse_id, month_str)
    )

    allowed_shift_ids = {
        row[0]
        for row in db.query(Shift.shift_id)
        .filter(Shift.group_id == group_id, Shift.show_in_preference == True)
        .all()
    }
    normalized = _normalize_wanted_entries(req.wanted_entries, year, month, allowed_shift_ids)

    # 주휴일은 원티드 대상이 아니다 — 기존 /wanted/invoke 와 동일하게 조용히 제외.
    weekly_off_days = _compute_weekly_off_days(db, nurse_id, group_id, year, month)
    if weekly_off_days:
        dropped = [e["date"].day for e in normalized if e["date"].day in weekly_off_days]
        if dropped:
            print(f"[wanted_entries] 주휴일 엔트리 제외: {sorted(dropped)}")
        normalized = [e for e in normalized if e["date"].day not in weekly_off_days]

    # 선호(wanted)는 nurse_shift_requests, 기피(avoid)는 banned_wanted_entries 로 갈린다.
    entries = [e for e in normalized if e["intent"] == "wanted"]
    avoid = _validate_avoid_entries(
        db, nurse_id, group_id, [e for e in normalized if e["intent"] == "avoid"]
    )

    nurse_row = db.query(Nurse).filter(Nurse.nurse_id == nurse_id).first()
    _assert_off_limit(
        entries,
        set(_get_off_shift_ids(db, group_id)),
        nurse_row.wanted_max_requests if nurse_row else None,
    )

    # 기피근무 저장소(banned_wanted_entries)가 없으면 — 기피가 없을 땐 그냥 건너뛰고,
    # 기피를 실제로 보냈다면 조용히 버리지 않고 명시적으로 거절한다.
    # 기피를 관리하지 않는 클라이언트가 보낸 avoid 는 애초에 없다(0건). 그런데
    # 0건을 replace 로 받아들이면 **간호사가 낸 기피근무가 통째로 삭제**된다.
    # 그래서 관리 의사를 명시한 요청만 기피를 건드린다.
    manages_avoid = bool(getattr(req, "manages_avoid", False)) or bool(avoid)
    avoid_ready = _avoid_storage_ready(db)
    if avoid and not avoid_ready:
        raise PreferenceValidationError(
            "피하고 싶은 근무를 저장할 수 없습니다. banned_wanted_entries 저장소가 준비되지 "
            "않았습니다(테이블 또는 source 컬럼 누락 — 마이그레이션 "
            "2026_07_28_add_banned_wanted_entries.sql 적용 필요).",
            code="avoid_storage_unavailable",
        )

    # ★ 자연어가 실려온 저장 = **발화 1건**이므로 새 request_id 로 스냅샷을 남긴다.
    #   캘린더만 조작한 저장(디바운스로 자주 들어온다)은 기존 draft 를 재사용한다 —
    #   안 그러면 클릭 한 번마다 draft 가 쌓인다.
    _utterance = (getattr(req, "request", None) or "").strip()
    draft = _acquire_draft_request(
        db, nurse_id, month_str, group_id, new_snapshot=bool(_utterance)
    )
    # 자연어 원문 보존 — 화면 입력창에 다시 그려지는 값이다(`preference_data.request`).
    # 예전에는 `/wanted/invoke` 가 채웠는데, 그 호출을 없애고 저장 경로로 합치면서
    # 여기서 갱신하지 않으면 **새로 쓴 문장이 저장돼도 옛 문장이 계속 보인다**
    # (실측: '12일 데이 싫어' 가 새 문장 저장 후에도 그대로 남았다).
    # None 이면 미지정 — 기존 값을 지우지 않는다(캘린더만 고친 저장과 구분).
    if getattr(req, "request", None) is not None:
        draft.request = req.request
    _replace_shift_requests(
        db, nurse_id, draft.request_id, entries,
        group_id=group_id, year=year, month=month,
    )
    avoid_blocked = []
    if avoid_ready and manages_avoid:
        avoid_blocked = _replace_nurse_avoid_entries(
            db, nurse_id, group_id, year, month, avoid
        )
    elif avoid_ready:
        print("[wanted_entries] manages_avoid=False — 기피근무 보존(손대지 않음)")

    # ── 같은 날짜에 선호와 기피가 공존하지 못하게 한다 ──────────────────────
    # ★ 한 날짜는 요청이거나 금지이거나 **하나**다. `_normalize_wanted_entries` 가
    #   intent 를 안 보고 날짜만으로 중복을 막으므로(duplicate_date), 둘이 남으면
    #   다음 저장이 422 로 통째 실패한다. 솔버 관점에서도 기피는 하드,
    #   선호는 소프트라 하드가 이겨 "요청이 안 먹는" 것으로 보인다.
    # ★ manages_avoid=False 경로가 특히 위험하다 — 선호만 교체되고 기피는 그대로
    #   남는다. 그래서 여기서 **저장된 선호 날짜의 기피를 지운다.**
    #   (manages_avoid=True 면 위 replace 가 이미 payload 기준으로 맞춰 놓아
    #    이 정리는 대개 no-op 이다.)
    conflict_resolved = {"cleared": [], "blocked_by_hn": []}
    if avoid_ready:
        from services.wanted_service import _clear_nurse_avoid_on_wanted
        conflict_resolved = _clear_nurse_avoid_on_wanted(
            db, nurse_id, group_id, year, month, draft.request_id, commit=False
        )
    if not is_draft:
        draft.is_submitted = True
        draft.submitted_at = datetime.now()

    db.commit()
    print(
        f"[wanted_entries] {'제출' if not is_draft else '임시저장'} 완료: "
        f"nurse={nurse_id}, {month_str}, request_id={draft.request_id}, "
        f"선호 {len(entries)}건 · 기피 {len(avoid)}건"
        + ("" if manages_avoid else " (기피 미관리)")
    )
    result = get_latest_preference_service(
        year, month, current_user, db, override_group_id=group_id
    )
    if avoid_blocked and isinstance(result, dict):
        # 조용히 버리면 사용자는 반영된 줄 안다.
        result["avoid_blocked"] = {
            "dates": [d.isoformat() for d in avoid_blocked],
            "message": "수간호사가 이미 지정한 날짜라 피하고 싶은 근무로 반영하지 "
                       "못했습니다.",
        }
    # 같은 날짜 상충 해소 결과 — 같은 이유로 조용히 넘기지 않는다.
    if isinstance(result, dict) and conflict_resolved["cleared"]:
        result["avoid_cleared"] = {
            "dates": [c["date"] for c in conflict_resolved["cleared"]],
            "message": "같은 날짜에 희망 근무를 신청해, 기존에 피하고 싶던 근무를 "
                       "해제했습니다.",
        }
    if isinstance(result, dict) and conflict_resolved["blocked_by_hn"]:
        # 선호는 저장된다. 다만 수간호사 확정 기피가 하드 제약이라 근무표에는 안 걸린다.
        result["wanted_blocked_by_hn"] = {
            "dates": [c["date"] for c in conflict_resolved["blocked_by_hn"]],
            "message": "수간호사가 피하고 싶은 근무로 확정한 날짜입니다. 신청은 "
                       "저장했지만 근무표에는 반영되지 않습니다.",
        }
    return result


def submit_preferences_service(
    req: PreferenceData,
    current_user: UserSchema,
    db: Session,
    is_draft: bool = False
):
    """
    희망근무 저장/제출 통합 서비스

    req.wanted_entries 가 오면 단일 원본 경로로 위임한다(신규 계약).
    없으면 기존 data 기반 경로로 동작한다(AIDE·모바일 하위호환).
    """
    if req.wanted_entries is not None:
        return save_wanted_entries_service(req, current_user, db, is_draft=is_draft)

    month_str = f"{req.year}-{req.month:02d}"

    # 본인 소속 그룹은 토큰 대신 DB(nurses.group_id)에서 — 토큰 stale 시에도 정합.
    home_gid = resolve_home_group_id(db, current_user)

    # data가 None이거나 없으면 빈 딕셔너리로 안전하게 처리
    preference_data = req.data if req.data is not None else {}
    
    # draft 찾기/생성
    draft = db.query(WantedRequest).filter(
        WantedRequest.nurse_id == current_user.nurse_id,
        WantedRequest.month == month_str,
        WantedRequest.is_submitted == False
    ).order_by(WantedRequest.created_at.desc()).first()
    
    if draft:
        request_id = draft.request_id
    else:
        # request_id 는 IDENTITY 가 아니고 복합 PK 라 SQLAlchemy 가 자동 채번하지
        # 않는다. 생략하면 NULL 로 INSERT 되어 실패하므로 기존 채번기를 쓴다.
        draft = _acquire_draft_request(
            db, current_user.nurse_id, month_str, home_gid
        )
        request_id = draft.request_id
    
    # ==================== 핵심: 항상 data_to_save 초기화 (에러 방지) ====================
    data_to_save = {}
    
    if isinstance(preference_data, dict):
        # AIDE 스타일 중첩 구조인지 확인
        shift_data = preference_data.get("shift", {})
        if isinstance(shift_data, dict):
            for shift_id, days in shift_data.items():
                if isinstance(days, dict):
                    for day_str in days.keys():
                        try:
                            day_num = int(day_str)
                            full_date = f"{req.year}-{req.month:02d}-{day_num:02d}"
                            data_to_save[full_date] = shift_id
                        except (ValueError, TypeError):
                            continue
        else:
            # 평평한 {날짜: shift} 형식
            data_to_save = preference_data

    # ==================== WantedConfig 검증 (최종 제출 시에만) ====================
    # 주의: 과거에는 `and data_to_save` 조건이 붙어 있어, 프론트가 {year, month} 만
    # 보내는 제출(=data 빈 dict)에서는 마감 검증이 통째로 스킵됐다. 마감 이후에도
    # 제출이 통과하던 버그라 조건에서 제거한다.
    if not is_draft:
        group_id = home_gid
        nurse_id = current_user.nurse_id

        # # 1. GLOBAL 설정 확인 - 더 이상 사용하지 않음 (nurses 테이블로 이동됨)
        # global_config = db.query(WantedConfig).filter(
        #     WantedConfig.group_id == group_id,
        #     WantedConfig.config_type == 'GLOBAL'
        # ).first()

        # 2. Wanted 테이블 상태 확인 (해당 월의 원티드 요청이 생성되었는지, 마감되었는지)
        wanted = _load_wanted_window(db, group_id, req.year, req.month)

        # 2-1. Wanted가 존재하지 않으면 (수간호사가 아직 원티드 요청을 생성하지 않음)
        if not wanted:
            print(f"[검증 실패] Wanted 요청이 생성되지 않음: group_id={group_id}, {req.year}-{req.month:02d}")
            raise PreferenceConflictError(
                f"{req.year}년 {req.month}월 원티드 요청이 아직 생성되지 않았습니다."
            )

        # 2-2. 마감 상태(status='closed' 또는 exp_date 경과)
        if _is_wanted_closed(wanted):
            print(f"[검증 실패] Wanted 마감: group_id={group_id}, {req.year}-{req.month:02d}, "
                  f"status={wanted.status}, "
                  f"마감일={wanted.exp_date.strftime('%Y-%m-%d %H:%M') if wanted.exp_date else 'None'}")
            raise PreferenceConflictError(f"{req.year}년 {req.month}월 원티드가 마감되었습니다.")

        print(f"[검증 통과] Wanted 상태 확인: status={wanted.status}, "
              f"exp_date={wanted.exp_date.strftime('%Y-%m-%d %H:%M') if wanted.exp_date else 'None'}")

        # 3. 재제출 여부 확인
        existing_submitted = db.query(WantedRequest).filter(
            WantedRequest.nurse_id == nurse_id,
            WantedRequest.month == month_str,
            WantedRequest.is_submitted == True
        ).first()

        is_resubmit = existing_submitted is not None

        print(f"[검증 통과] GLOBAL 설정 확인 완료: is_resubmit={is_resubmit}")

        # # 4. NURSE_LIMIT 검증 - 프론트에서 검증, nurses 테이블로 이동됨
        # nurse = db.query(Nurse).filter(Nurse.nurse_id == nurse_id).first()
        # if nurse and nurse.wanted_max_requests is not None:
        #     start_date = date(req.year, req.month, 1)
        #     if req.month == 12:
        #         end_date = date(req.year + 1, 1, 1)
        #     else:
        #         end_date = date(req.year, req.month + 1, 1)
        #
        #     query = db.query(NurseShiftRequest).filter(
        #         NurseShiftRequest.nurse_id == nurse_id,
        #         NurseShiftRequest.shift_date >= start_date,
        #         NurseShiftRequest.shift_date < end_date
        #     )
        #     if is_resubmit and existing_submitted:
        #         query = query.filter(NurseShiftRequest.request_id != existing_submitted.request_id)
        #
        #     nurse_current_count = query.count()
        #     additional_count = len(data_to_save)
        #     total_count = nurse_current_count + additional_count
        #
        #     if total_count > nurse.wanted_max_requests:
        #         print(f"[검증 실패] NURSE_LIMIT 초과: nurse_id={nurse_id}, "
        #               f"현재={nurse_current_count}, 추가={additional_count}, "
        #               f"제한={nurse.wanted_max_requests}")
        #         raise Exception(
        #             f"간호사별 최대 요청 개수를 초과했습니다. "
        #             f"(현재: {nurse_current_count}개, 추가: {additional_count}개, 제한: {nurse.wanted_max_requests}개)"
        #         )
        #
        #     print(f"[검증 통과] NURSE_LIMIT: 현재={nurse_current_count}, 추가={additional_count}, "
        #           f"제한={nurse.wanted_max_requests}")

        # # 5. DAILY_LIMIT 검증 - 프론트에서 검증
        # case_dates = set()
        # for date_str in data_to_save.keys():
        #     try:
        #         parsed_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        #         case_dates.add(parsed_date)
        #     except ValueError:
        #         continue
        #
        # if case_dates:
        #     daily_limit_configs = db.query(WantedConfig).filter(
        #         WantedConfig.group_id == group_id,
        #         WantedConfig.target_date.in_(case_dates),
        #         WantedConfig.shift_type.is_(None)
        #     ).all()
        #
        #     daily_limit_map = {config.target_date: config.max_requests for config in daily_limit_configs}
        #
        #     if daily_limit_map:
        #         group_nurse_ids = [n[0] for n in db.query(Nurse.nurse_id).filter(Nurse.group_id == group_id).all()]
        #
        #         exceeded_dates = []
        #         for check_date in case_dates:
        #             if check_date in daily_limit_map:
        #                 daily_limit = daily_limit_map[check_date]
        #
        #                 query = db.query(NurseShiftRequest).filter(
        #                     NurseShiftRequest.nurse_id.in_(group_nurse_ids),
        #                     NurseShiftRequest.shift_date == check_date
        #                 )
        #
        #                 if is_resubmit and existing_submitted:
        #                     query = query.filter(
        #                         ~((NurseShiftRequest.nurse_id == nurse_id) &
        #                           (NurseShiftRequest.request_id == existing_submitted.request_id))
        #                     )
        #
        #                 daily_current_count = query.count()
        #
        #                 if daily_current_count + 1 > daily_limit:
        #                     exceeded_dates.append({
        #                         "date": check_date.strftime('%Y-%m-%d'),
        #                         "current": daily_current_count,
        #                         "limit": daily_limit
        #                     })
        #
        #         if exceeded_dates:
        #             exceeded_info = ", ".join([
        #                 f"{d['date']}({d['current']}/{d['limit']})"
        #                 for d in exceeded_dates
        #             ])
        #             print(f"[검증 실패] DAILY_LIMIT 초과: {exceeded_info}")
        #             raise Exception(
        #                 f"다음 날짜의 일자별 최대 요청 개수를 초과했습니다: {exceeded_info}"
        #             )
        #
        #         print(f"[검증 통과] DAILY_LIMIT: case 날짜 {len(case_dates)}개 확인 완료")

    # 최종 제출 시에만 이전 데이터 삭제 (data_to_save가 있을 때만)
    if not is_draft and data_to_save:  # ← 이제 안전하게 참조 가능
        db.query(NurseShiftRequest).filter(
            NurseShiftRequest.nurse_id == current_user.nurse_id,
            NurseShiftRequest.request_id == request_id
        ).delete()
        db.query(NursePairRequest).filter(
            NursePairRequest.nurse_id == current_user.nurse_id,
            NursePairRequest.request_id == request_id
        ).delete()
    
    # ==================== 상세 데이터 저장 ====================
    # shifts.id 매핑
    _nurse_row = db.query(Nurse).filter(Nurse.nurse_id == current_user.nurse_id).first()
    _grp_id = _nurse_row.group_id if _nurse_row else None
    _shift_id_to_table_id = {}
    if _grp_id:
        _shift_id_to_table_id = {
            s.shift_id: s.id
            for s in db.query(Shift.shift_id, Shift.id).filter(Shift.group_id == _grp_id).all()
        }

    detailed_id = 1
    for date_str, shift_id in data_to_save.items():
        if shift_id:  # 빈 값은 저장하지 않음
            # 사유작성
            comment = ""
            item = preference_data.get(date_str)
            if isinstance(item, dict):
                comment = item.get("comment", "")
            elif isinstance(item, str):
                pass
            db.add(NurseShiftRequest(
                nurse_id=current_user.nurse_id,
                request_id=request_id,
                detailed_request_id=detailed_id,
                shift_date=date_str,
                group_id=home_gid,
                shift=shift_id,
                score=1.0,
                partial_request="",
                comment=comment, # 사유작성
                shifts_table_id=_shift_id_to_table_id.get(shift_id),
            ))
            detailed_id += 1
    
    # ==================== pair(선호/비선호) 데이터 처리 ====================
    # data 내부 또는 top-level 어디에든 preference가 있으면 인식
    preference_list = preference_data.get("preference", None) if isinstance(preference_data, dict) else None
    if preference_list is None and req.preference is not None:
        preference_list = req.preference

    if preference_list is not None and isinstance(preference_list, list):
        # 프론트에서 preference 배열을 명시적으로 보낸 경우 → 해당 항목만 저장
        # (제거된 항목은 배열에 미포함되어 자연스럽게 새 request_id에 저장되지 않음)
        db.query(NursePairRequest).filter(
            NursePairRequest.nurse_id == current_user.nurse_id,
            NursePairRequest.request_id == request_id,
            NursePairRequest.month == month_str,
        ).delete()

        pair_detailed_id = 1
        for pair in preference_list:
            target_id = pair.get("id")
            weight = pair.get("weight")
            if target_id is None or weight is None:
                continue
            db.add(NursePairRequest(
                nurse_id=current_user.nurse_id,
                request_id=request_id,
                month=month_str,
                detailed_request_id=pair_detailed_id,
                target_id=str(target_id),
                group_id=home_gid,
                score=float(weight),
                partial_request=pair.get("request", ""),
            ))
            pair_detailed_id += 1
        print(f"[pair 저장] preference 배열 기반 {pair_detailed_id - 1}건 저장 (request_id={request_id})")
    else:
        # preference 배열이 없는 경우 → 이전 request_id에서 pair 데이터 carry-forward
        # 초기화 시 carry-forward 스킵
        if data_to_save or preference_list: # shift나 preference가 있으면 carry-forward 진행
            _carry_forward_pair_data(db, current_user.nurse_id, request_id, month_str, group_id=home_gid)
        else:
            print(f"[pair 초기화] 빈 데이터로 인해 carry-forward 스킵 (request_id={request_id})")

    # 제출 처리
    if not is_draft:
        draft.is_submitted = True
        draft.submitted_at = datetime.now()

    db.commit()

    return {
        "message": "임시 저장되었습니다." if is_draft else "제출이 완료되었습니다."
    }


def submit_empty_preferences_service(req: PreferenceSubmit, current_user, db: Session):
    """
    빈 선호도 최종 제출 서비스 함수
    """
    KST = timezone(timedelta(hours=9))
    if not current_user:
        raise Exception("Not authenticated")
    empty_data = {"shift": {}, "preference": []}
    preference = ShiftPreference(
        nurse_id=current_user.nurse_id,
        year=req.year,
        month=req.month,
        data=empty_data,
        is_submitted=True,
        submitted_at=datetime.now(KST).replace(tzinfo=None)
    )
    db.add(preference)
    db.commit()
    return {"message": "Empty preferences submitted successfully"}

def retract_submission_service(req: PreferenceSubmit, current_user, db: Session):
    """
    선호도 제출 철회 서비스 함수
    """
    if not current_user:
        raise Exception("Not authenticated")
    print('current_user', current_user.__dict__)
    print('req', req.__dict__)
    month_str = f"{req.year}-{req.month:02d}"
    preference = db.query(WantedRequest).filter(
        WantedRequest.nurse_id == current_user.nurse_id,
        WantedRequest.month == month_str,
        WantedRequest.is_submitted == True
    ).order_by(WantedRequest.submitted_at.desc()).first()
    if not preference:
        raise Exception("No submitted preference found to retract")
    preference.is_submitted = False
    preference.submitted_at = None
    db.commit()
    return {"message": "Submission retracted successfully"}

def _resolve_submission_status(wanted, target_wr) -> str:
    """submission.status 판정.

    - submitted: 본인 제출 완료(마감 여부와 무관)
    - empty:     해당 월 원티드 요청(Wanted) 자체가 생성되지 않아 작성 불가
    - closed:    요청은 있으나 마감(status='closed' 또는 exp_date 경과)
    - requested: 작성 가능(미제출)
    """
    if target_wr is not None and bool(target_wr.is_submitted):
        return "submitted"
    if wanted is None:
        return "empty"
    if _is_wanted_closed(wanted):
        return "closed"
    return "requested"


def _build_request_limit(db: Session, nurse_id: str, group_id: str | None, entries: list[dict]) -> dict:
    """휴무/휴가 요청 한도 사용량. max 는 nurses.wanted_max_requests(없으면 None)."""
    from services.wanted_service import _get_off_shift_ids

    nurse_row = db.query(Nurse).filter(Nurse.nurse_id == nurse_id).first()
    max_requests = nurse_row.wanted_max_requests if nurse_row else None
    off_shift_ids = set(_get_off_shift_ids(db, group_id)) if group_id else set()
    used = sum(1 for e in entries if e["shift_id"] in off_shift_ids)
    return {"used": used, "max": max_requests}


def get_latest_preference_service(
    year: int,
    month: int,
    current_user,
    db: Session,
    override_group_id: str | None = None,
):
    """
    최신 선호도 데이터 조회 서비스 함수 (WantedRequest 기반)

    canonical wanted snapshot 을 한 번에 반환한다:
        preference_data.wanted_entries / submission / request_limit
    기존 필드(preference_data.shift/preference, is_submitted, created_at,
    submitted_at)는 모바일·기존 프론트 하위호환을 위해 그대로 유지한다.
    """
    if not current_user:
        raise Exception("Not authenticated")
    nurse_id = current_user.nurse_id
    month_str = f"{year}-{month:02d}"
    group_id = _resolve_read_group_id(db, current_user, override_group_id)
    wanted = _load_wanted_window(db, group_id, year, month)
    # 1️⃣ 제출된 요청 중 최신 데이터
    submitted_wr = (
        db.query(WantedRequest)
        .filter(
            WantedRequest.nurse_id == nurse_id,
            WantedRequest.month == month_str,
            WantedRequest.is_submitted == True,
        )
        .order_by(WantedRequest.submitted_at.desc())
        .first()
    )
    # 2️⃣ 없으면 가장 오래된(created_at.asc) 임시 요청 선택
    target_wr = submitted_wr or (
        db.query(WantedRequest)
        .filter(
            WantedRequest.nurse_id == nurse_id,
            WantedRequest.month == month_str,
        )
        .order_by(WantedRequest.created_at.desc())
        .first()
    )
    if not target_wr:
        # 아무 데이터도 없을 때. preference_data=None 은 기존 프론트/모바일의
        # '빈 제출' 분기 조건이라 유지한다. 신규 계약은 submission.status 로 판단.
        return {
            "year": year,
            "month": month,
            "preference_data": None,
            "is_submitted": False,
            "created_at": None,
            "submitted_at": None,
            "submission": {
                "status": _resolve_submission_status(wanted, None),
                "submitted_at": None,
                "deadline_at": wanted.exp_date.isoformat() if wanted and wanted.exp_date else None,
            },
            "request_limit": _build_request_limit(db, nurse_id, group_id, []),
        }
    # 3️⃣ 해당 request_id로 shift / pair 데이터 가져오기
    shift_rows = (
        db.query(NurseShiftRequest)
        .filter(
            NurseShiftRequest.nurse_id == nurse_id,
            NurseShiftRequest.request_id == target_wr.request_id,
            cast(NurseShiftRequest.shift_date, String).like(f"{month_str}-%"),
        )
        .all()
    )

    pair_rows = (
        db.query(NursePairRequest)
        .filter(
            NursePairRequest.nurse_id == nurse_id,
            NursePairRequest.request_id == target_wr.request_id,
            NursePairRequest.month == month_str,
        )
        .all()
    )
    # 4️⃣ shift 데이터 구조화 -> 여기만 나중에 바꿀것
    # 예: {'N': {'1': {'request': '주말은 N로 줘', 'score': 1.7}}, 'O': {...}}
    shift_data = {}
    for s in shift_rows:
        shift_code = s.shift.upper()
        day = str(int(s.shift_date.day))
        if shift_code not in shift_data:
            shift_data[shift_code] = {}
        shift_data[shift_code][day] = {
            "request": s.partial_request,
            "score": float(s.score) if s.score is not None else None,
            "comment": s.comment, # 사유작성
            "shifts_table_id": s.shifts_table_id,
        }
    
    # 5️⃣ pair 데이터 구조화 -> 여기만 나중에 바꿀것
    # 예: [{'id': '간호사ID', 'request': '같이해주세요', 'weight': -1.5}]
    pair_data = []
    for p in pair_rows:
        pair_data.append({
            "id": p.target_id,
            "request": p.partial_request,
            "weight": float(p.score) if p.score is not None else 0.0,
        })
    # 6️⃣ wanted_entries — 날짜별 한 건. 저장·조회·제출의 단일 원본.
    #    이름/색상은 담지 않는다(프론트가 shift_id 로 근무코드 룩업과 조인).
    #    선호는 nurse_shift_requests, 기피는 banned_wanted_entries(source='nurse').
    wanted_entries = [
        {
            "date": s.shift_date.isoformat(),
            "shift_id": s.shift,
            "intent": "wanted",
            "comment": s.comment or "",
        }
        for s in shift_rows
    ]
    # ★ 같은 날짜에 선호가 있으면 기피는 싣지 않는다.
    #   `_normalize_wanted_entries` 가 intent 를 안 보고 **날짜만으로** 중복을 막으므로,
    #   둘을 함께 내리면 프론트가 그대로 되보낼 때 `duplicate_date` 422 로 저장이
    #   통째 실패한다. 저장 경로가 nurse 기피는 지우지만, **수간호사 확정(hn) 기피**는
    #   남겨 두기 때문에 이 조합이 실제로 생긴다.
    #   기피 자체는 DB 에 살아 있고 솔버에도 계속 걸린다 — 화면 표시만 양보한다.
    _wanted_dates = {e["date"] for e in wanted_entries}
    wanted_entries += [
        a for a in _load_nurse_avoid_entries(db, nurse_id, group_id, year, month)
        if a["date"] not in _wanted_dates
    ]
    wanted_entries.sort(key=lambda e: e["date"])

    # 7️⃣ 최종 JSON 구성 (Front 기대 형식)
    preference_data = {
        "request": target_wr.request,  # 상위 텍스트 그대로
        "shift": shift_data,
        "preference": pair_data,
        "wanted_entries": wanted_entries,
    }
    return {
        "year": year,
        "month": month,
        "preference_data": preference_data,
        "is_submitted": bool(target_wr.is_submitted),
        "created_at": target_wr.created_at,
        "submitted_at": target_wr.submitted_at,
        "submission": {
            "status": _resolve_submission_status(wanted, target_wr),
            "submitted_at": target_wr.submitted_at.isoformat() if target_wr.submitted_at else None,
            "deadline_at": wanted.exp_date.isoformat() if wanted and wanted.exp_date else None,
        },
        "request_limit": _build_request_limit(
            db, nurse_id, group_id,
            [{"shift_id": e["shift_id"]} for e in wanted_entries],
        ),
    }

def get_all_preferences_service(year: int, month: int, current_user, db: Session, override_group_id: str | None = None):
    """
    모든 간호사의 최신 선호도 데이터 조회 서비스 함수 (새 구조 기반)
    - WantedRequest, NurseShiftRequest, NursePairRequest 조합
    - Output은 기존 ShiftPreference.data 구조와 동일하게 유지

    관리자(ADM)는 `override_group_id`로 대상 그룹을 지정할 수 있습니다.
    """
    if not current_user:
        raise Exception("Not authenticated")
    month_str = f"{year}-{month:02d}"

    # override_group_id 미지정 시 호출자 home group. (과거 정의되지 않은 home_gid 를
    # 참조해 group_id 없이 호출하면 NameError → 500 이었다.)
    target_group_id = override_group_id or resolve_home_group_id(db, current_user)
    if not target_group_id:
        raise Exception("대상 그룹이 없습니다.")
    # ✅ 1️⃣ 그룹 내 간호사 목록 가져오기
    nurse_ids = [
        n.nurse_id
        for n in db.query(Nurse.nurse_id)
        .filter(Nurse.group_id == target_group_id)
        .all()
    ]
    # ✅ 2️⃣ 각 간호사별 최신 요청(WantedRequest) 찾기
    wanted_requests = (
        db.query(WantedRequest)
        .filter(
            WantedRequest.nurse_id.in_(nurse_ids),
            WantedRequest.month == month_str,
            WantedRequest.is_submitted == True,
        )
        .order_by(WantedRequest.nurse_id, WantedRequest.submitted_at.desc())
        .all()
    )
    # ✅ 3️⃣ nurse_id별 최신 submitted request만 남기기
    latest_wr_map = {}
    for wr in wanted_requests:
        if wr.nurse_id not in latest_wr_map:
            latest_wr_map[wr.nurse_id] = wr
    results = []

    # ✅ 4️⃣ 각 nurse_id에 대해 shift/pair 요청 조회 및 JSON 구성
    for nurse_id, wr in latest_wr_map.items():
        # shift 요청들
        shift_rows = (
            db.query(NurseShiftRequest)
            .filter(
                NurseShiftRequest.nurse_id == nurse_id,
                NurseShiftRequest.request_id == wr.request_id,
                cast(NurseShiftRequest.shift_date, String).like(f"{month_str}-%"),
            )
            .all()
        )
        
        allowed_shifts = {
            row[0].upper()
            for row in db.query(Shift.shift_id).filter(
                Shift.group_id == target_group_id,
                Shift.show_in_preference == True
            ).all()
        }
        
        shift_data = {}
        
        for s in shift_rows:
            shift_type = (s.shift or "").upper().strip()
            if not shift_type or shift_type not in allowed_shifts:
                continue
            
            day = str(s.shift_date.day).lstrip("0")
            
            if shift_type not in shift_data:
                shift_data[shift_type] = {}
            
            # shift_data[shift_type][day] = (
            #     int(s.score) if s.score is not None else 1
            # )
            shift_data[shift_type][day] = {
                "score": float(s.score) if s.score is not None else 1.0,
                "comment": s.comment, # 사유작성
                "shifts_table_id": s.shifts_table_id,
            }

        # pair 요청들
        pair_rows = (
            db.query(NursePairRequest)
            .filter(
                NursePairRequest.nurse_id == nurse_id,
                NursePairRequest.request_id == wr.request_id,
                NursePairRequest.month == month_str,
            )
            .all()
        )
        pair_data = [
            {"id": p.target_id, "weight": p.score}
            for p in pair_rows
        ]
        # ✅ data JSON 구성
        data_json = {
            "request": wr.request,
            "shift": {k: v for k, v in shift_data.items() if v},
            "preference": pair_data,
        }
        results.append({
            "nurse_id": nurse_id,
            "year": year,
            "month": month,
            "is_submitted": bool(wr.is_submitted),
            "created_at": wr.created_at,
            "submitted_at": wr.submitted_at,
            "data": data_json,
        })
    return results

# ──────────────────────────────────────────────────────────────
# 원티드 월별 메모
#   ★ 원티드 저장 경로(submit_preferences_service)와 **완전히 분리**한다.
#     그 경로는 저장 한 번에 BannedWantedEntry · NurseShiftRequest ·
#     NursePairRequest 를 delete-then-insert 하는데, 메모는 입력 중 디바운스로
#     자주 저장되므로 같이 태우면 원티드가 통째로 지워질 위험이 크다.
#   ★ year·month 는 호출자가 항상 명시한다. 서버가 현재 월 등으로 추론하지 않는다.
# ──────────────────────────────────────────────────────────────
def _memo_scope(current_user: UserSchema, db: Session, override_group_id=None):
    """(nurse_id, group_id) 해석.

    ★ 본인 메모이므로 대상 그룹은 **home group 하나뿐**이다. 원티드 저장
      (_resolve_write_group_id)과 같은 규칙이다. 요청이 다른 group_id 를 보내면
      403 — 안 막으면 남의 그룹에 자기 메모를 심을 수 있다.
    """
    nurse_id = str(getattr(current_user, "nurse_id", "") or "")
    if not nurse_id:
        raise PreferenceForbiddenError("간호사 계정이 아닙니다.")
    home_gid = resolve_home_group_id(db, current_user)
    if not home_gid:
        raise PreferenceForbiddenError("소속 그룹을 확인할 수 없습니다.")
    if override_group_id and str(override_group_id) != str(home_gid):
        raise PreferenceForbiddenError("본인 소속 그룹의 메모만 다룰 수 있습니다.")
    return nurse_id, str(home_gid)


def get_monthly_memo_service(year: int, month: int, current_user: UserSchema,
                             db: Session, override_group_id=None) -> dict:
    """그 달의 메모를 돌려준다. 행이 없으면 monthly_memo=None."""
    nurse_id, group_id = _memo_scope(current_user, db, override_group_id)
    row = (
        db.query(WantedMonthlyMemo)
        .filter(WantedMonthlyMemo.nurse_id == nurse_id,
                WantedMonthlyMemo.group_id == group_id,
                WantedMonthlyMemo.year == int(year),
                WantedMonthlyMemo.month == int(month))
        .first()
    )
    return {
        "year": int(year), "month": int(month), "group_id": group_id,
        "monthly_memo": (row.memo if row else None),
        "updated_at": (row.updated_at if row else None),
    }


def save_monthly_memo_service(year: int, month: int, memo, current_user: UserSchema,
                              db: Session, override_group_id=None) -> dict:
    """메모를 upsert 한다. None 또는 공백만이면 삭제로 처리한다.

    ★ 이 함수는 wanted_monthly_memo 외의 어떤 테이블도 건드리지 않는다.
    """
    nurse_id, group_id = _memo_scope(current_user, db, override_group_id)
    text_value = None if memo is None else str(memo)
    if text_value is not None and not text_value.strip():
        text_value = None                      # 빈 문자열도 삭제로 정규화

    row = (
        db.query(WantedMonthlyMemo)
        .filter(WantedMonthlyMemo.nurse_id == nurse_id,
                WantedMonthlyMemo.group_id == group_id,
                WantedMonthlyMemo.year == int(year),
                WantedMonthlyMemo.month == int(month))
        .first()
    )
    now = datetime.now()
    if text_value is None:
        # 삭제 = 행 제거. 조회는 행 부재를 monthly_memo=None 으로 돌려주므로 동일하다.
        if row is not None:
            db.delete(row)
    elif row is None:
        db.add(WantedMonthlyMemo(nurse_id=nurse_id, group_id=group_id,
                                 year=int(year), month=int(month),
                                 memo=text_value, updated_at=now))
    else:
        row.memo = text_value
        row.updated_at = now
    db.commit()
    return {"year": int(year), "month": int(month), "group_id": group_id,
            "monthly_memo": text_value,
            "updated_at": (now if text_value is not None else None)}


def list_group_monthly_memos_service(year: int, month: int, current_user: UserSchema,
                                     db: Session, override_group_id=None) -> dict:
    """관리보드용 — 그룹 안에서 **메모를 쓴 사람만** 모아 돌려준다.

    ★ 메모가 없는 사람은 아예 빼서 보낸다. 관리보드는 이름 위 호버로 보여 주므로
      "메모가 있는가" 자체가 표시 여부의 조건이고, 인원이 많은 그룹에서 빈 값을
      전부 실어 보낼 이유가 없다.
    ★ 개인이 쓴 내용이라 수간호사·관리자만 볼 수 있다(라우터에서 검증).
    """
    group_id = str(override_group_id or "") or resolve_home_group_id(db, current_user)
    if not group_id:
        raise PreferenceValidationError("group_id 를 확인할 수 없습니다.")

    rows = (
        db.query(WantedMonthlyMemo, Nurse.name)
        .join(Nurse, Nurse.nurse_id == WantedMonthlyMemo.nurse_id)
        .filter(WantedMonthlyMemo.group_id == group_id,
                WantedMonthlyMemo.year == int(year),
                WantedMonthlyMemo.month == int(month),
                WantedMonthlyMemo.memo.isnot(None))
        .order_by(Nurse.name.asc())
        .all()
    )
    return {
        "year": int(year), "month": int(month), "group_id": group_id,
        "count": len(rows),
        "memos": [
            {"nurse_id": m.nurse_id, "name": name,
             "monthly_memo": m.memo, "updated_at": m.updated_at}
            for m, name in rows
        ],
    }
