from schemas.roster_schema import PreferenceData, PreferenceSubmit, WantedEntryItem
from services.wanted_service import WantedAnalysisError, analyze_wanted_text
from routers.auth import get_current_user_from_cookie
from services.group_access import (
    resolve_home_group_id,
    caller_is_head_nurse,
    assert_caller_can_access_group,
)
from db.client2 import get_db
from db.models import ShiftPreference, Nurse, Shift
from schemas.auth_schema import User as UserSchema
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from datetime import datetime
from services.preferences_service import (
    # save_preference_draft_service,
    PreferenceConflictError,
    PreferenceForbiddenError,
    PreferenceValidationError,
    submit_preferences_service,
    submit_empty_preferences_service,
    retract_submission_service,
    get_latest_preference_service,
    get_all_preferences_service,
    get_monthly_memo_service,
    save_monthly_memo_service,
    list_group_monthly_memos_service,
    _resolve_write_group_id,
    assert_writable,
)
from pydantic import BaseModel, Field
from typing import Optional


def _summarize_rejections(rejections) -> Optional[str]:
    """미반영 통지를 토스트 한 줄로 요약한다. 보여줄 게 없으면 None.

    ★ `blocking=False` 는 **저장은 된** 정보성 통지라 뺀다(선호 신청으로 기피가
      자동 해제된 경우 등). 토스트로 띄우면 정상 동작마다 경고가 뜬다.
    ★ 분석 실패는 `analysis.message` 로 이미 나가므로 중복해서 싣지 않는다.
    """
    from services.wanted_service import REJECT_ANALYSIS_FAILED

    _msgs: list[str] = []
    for _r in (rejections or []):
        if not isinstance(_r, dict) or not _r.get("blocking"):
            continue
        if _r.get("code") == REJECT_ANALYSIS_FAILED:
            continue
        _reason = str(_r.get("reason") or "").strip()
        if _reason and _reason not in _msgs:
            _msgs.append(_reason)
    if not _msgs:
        return None
    if len(_msgs) <= 2:
        return " ".join(_msgs)
    return f"{_msgs[0]} {_msgs[1]} 외 {len(_msgs) - 2}건이 반영되지 않았습니다."


def _finalize_save_response(result, analysis: Optional[dict]):
    """저장 응답을 마무리한다 — `save_status` 한 축 + 표시용 `analysis.message`.

    ★★ 미반영 통지(`rejections`)가 있으면 그 요약을 `analysis.message` **에도**
      싣는다. `rejections` 가 본 계약이지만, 화면이 그것을 읽기 전까지는 사용자가
      아무것도 못 보기 때문이다. 특히 휴무/휴가 한도 초과는 종전에 **422 로 거절**
      돼 에러 토스트가 떴는데, 지금은 초과분만 잘라내고 200 으로 나간다. 화면이
      `rejections` 를 모르면 **일부가 잘린 채 "저장 완료" 로만 보인다.**
      화면이 `rejections` 를 제대로 읽게 되면 이 합성은 걷어낸다.
    ★ `rejections` 자체는 손대지 않는다 — 합성은 표시용 사본일 뿐이다.
    """
    from services.wanted_service import (
        SAVE_OK, SAVE_WITH_REJECTIONS, SAVE_SNAPSHOT_UNAVAILABLE,
    )

    if not isinstance(result, dict):
        return result
    if analysis:
        result["analysis"] = analysis
    _summary = _summarize_rejections(result.get("rejections"))
    if _summary:
        _base = result.get("analysis") or {"ok": True, "code": "partially_rejected"}
        _prev = str(_base.get("message") or "").strip()
        result["analysis"] = {
            **_base,
            "message": f"{_prev} {_summary}".strip() if _prev else _summary,
        }
    # ★ 저장 결과 단일 축. 스냅샷을 못 만든 쪽이 더 급하다 — 화면이 낡은 캐시를
    #   그대로 두면 미반영 통지를 띄워도 무엇이 빠졌는지 확인할 수가 없다.
    #   세부 사유는 `rejections[]` 에 그대로 있으므로 이 값이 하나여도 정보는
    #   잃지 않는다.
    # ★★ 판정은 `_summary`(표시용)가 **아니라** blocking 통지의 존재로 한다.
    #   `_summary` 는 토스트 중복을 피하려고 분석 실패를 빼는데, 그 필터를 판정에
    #   그대로 쓰면 **문장이 통째로 반영 안 된 경우가 `saved` 로 나간다.**
    #   정보성(blocking=False)만 있을 때는 `saved` 다 — 정상 동작마다 부분 실패로
    #   읽히지 않게 한다.
    _has_blocking = any(
        isinstance(_r, dict) and _r.get("blocking")
        for _r in (result.get("rejections") or [])
    )
    if result.get("snapshot_unavailable"):
        result["save_status"] = SAVE_SNAPSHOT_UNAVAILABLE
    elif _has_blocking:
        result["save_status"] = SAVE_WITH_REJECTIONS
    else:
        result["save_status"] = SAVE_OK
    return result

router = APIRouter(
    prefix="/preferences",
    tags=["preferences"]
)


def _raise_domain_error(exc: Exception) -> None:
    """원티드 도메인 예외를 HTTP 상태로 매핑한다.

    - 403: 역할/그룹 불일치
    - 409: 마감 또는 이미 제출됨
    - 422: 날짜 중복 · 유효하지 않은 shift_id · 미지원 intent
      (휴무/휴가 한도 초과는 **거절하지 않는다** — 초과분만 잘라내고 200 +
       응답 `rejections` 로 알린다. `/wanted/invoke` 와 같은 정책.)
    매핑 대상이 아니면 그대로 재전파해 호출부가 500 으로 처리하게 둔다.
    """
    # ★★ 세 예외 모두 `detail` 을 **같은 모양의 객체**로 내린다.
    #   종전에는 422 만 `{code, message}` 였고 403·409 는 문자열이라, 화면이 같은
    #   저장 실패를 어떤 건 코드로 어떤 건 문구로 갈라 봐야 했다. 문구는 바뀌면
    #   분기가 조용히 깨진다.
    #   ※ 프론트 `config/axios.ts` 는 객체 `detail` 에서 `message` 를 꺼내 쓰므로
    #     (422 를 그렇게 처리해 왔다) 문자열을 기대하던 화면도 그대로 동작한다.
    _pairs = (
        (PreferenceForbiddenError, 403, "forbidden"),
        (PreferenceConflictError, 409, "conflict"),
        (PreferenceValidationError, 422, "invalid_entry"),
    )
    for _cls, _status, _default_code in _pairs:
        if isinstance(exc, _cls):
            body = {
                "code": getattr(exc, "code", None) or _default_code,
                "message": str(exc),
            }
            # 화면이 어느 날짜를 짚어 줄지 알 수 있게 부가 정보를 함께 내린다.
            if getattr(exc, "detail", None):
                body["detail"] = exc.detail
            raise HTTPException(status_code=_status, detail=body)
    raise exc


async def _merge_analyzed_request(
    req: PreferenceData, current_user, db, rejections: list
) -> Optional[dict]:
    """`req.request`(자연어)가 있으면 분석해 `req.wanted_entries` 에 병합한다.

    프론트가 `/wanted/invoke` → `/preferences` → `/preferences/submit` 로 나눠 부르던
    것을 한 번으로 합치기 위한 전처리다. invoke 자체는 모바일이 아직 쓰고 있어
    그대로 둔다(제거하면 동시 배포 없이 회귀).

    **캘린더로 찍은 항목이 우선**이다. 같은 날짜가 겹치면 분석 결과를 버린다 —
    사용자가 직접 고른 것이 문장 해석보다 확실하다.

    ★★ **단, 극성이 반대면 문장이 이긴다.**
      `wanted_entries` 로 실려오는 캘린더 상태는 "이번에 찍은 것" 이 아니라
      **이전에 저장돼 다시 실려온 것**이다. 그래서 날짜만 보고 버리면
      "N 금지" 뒤에 "N 주세요" 라고 말해도 옛 기피가 이겨 **문장이 통째로 무시된다**
      (실측: 분석은 want 8건을 정확히 뽑았는데 "8건 중 0건 반영"으로 전량 폐기).
      방금 한 말이 이전 상태보다 최신 의사이므로, 요청↔금지가 뒤집히는 경우에는
      분석 결과로 교체한다. 같은 극성이면(코드만 다름) 기존대로 캘린더 우선이다.
    분석이 실패하면 저장은 그대로 진행하되 **상태를 돌려준다**(호출자가 응답에 실어
    화면이 "문장 해석에 실패했습니다" 를 띄우게). 조용히 넘기면 사용자는 저장된 줄
    안다. 성공이면 None.

    해석은 됐지만 반영하지 못한 항목은 `rejections` 에 담는다 — 분석 자체가
    실패한 것(위 반환값)과, 일부만 못 들어간 것은 사용자에게 다르게 보여야 한다.
    """
    from services.wanted_service import (
        make_rejection, REJECT_ANALYSIS_FAILED, REJECT_OVERRIDDEN_BY_CALENDAR,
        REJECT_NOTHING_TO_APPLY,
    )
    text = (getattr(req, "request", None) or "").strip()
    if not text:
        return
    # ★★ **인가를 분석보다 먼저** 한다. 예전에는 `req.group_id` 를 그대로 믿고
    #   분석부터 돌렸는데, 분석은 그 그룹의 시프트와 **간호사 전원의 id·이름**을
    #   읽어 외부 LLM 으로 보낸다(`analyze_wanted_text` 의 `schema`).
    #   저장 단계에서 `_resolve_write_group_id` 가 403 을 내더라도 그때는 이미
    #   남의 병동 명단이 조회돼 밖으로 나간 뒤다. 저장이 쓰는 것과 **같은 해석기**로
    #   먼저 그룹을 확정해, 분석과 저장이 언제나 같은 그룹을 보게 한다.
    group_id = _resolve_write_group_id(db, current_user, req.group_id)
    try:
        analyzed = await analyze_wanted_text(
            db, getattr(current_user, "nurse_id", None), group_id, text,
            req.year, req.month, rejections=rejections,
        )
    except WantedAnalysisError as exc:
        print(f"[preferences] 자연어 분석 실패 — 저장은 계속: {exc}")
        _msg = ("문장을 해석하지 못했습니다. 달력에서 직접 선택하거나 "
                "다시 시도해 주세요.")
        rejections.append(make_rejection(REJECT_ANALYSIS_FAILED, _msg))
        return {"ok": False, "code": "analysis_failed", "message": _msg}
    if not analyzed:
        # ★★ 문장은 왔는데 반영할 게 한 건도 없다 = **변경 없음**이다.
        #   종전에는 그냥 `None`(성공)을 돌려줘 `wanted_entries` 가 None 으로 남았고,
        #   호출자가 레거시 `data` 경로로 빠져 **스냅샷 없는 `{message}` 응답**을
        #   내보냈다. 화면이 그걸 캐시에 얹으면 찍어 둔 달력이 사라진다
        #   (`useSaveWantedRequest` 주석의 그 현상). 게다가 아무것도 저장하지
        #   않았는데 `save_status` 는 `saved` 로 나갔다.
        #   호출자가 **저장을 건너뛰고 현재 상태를 돌려주도록** 신호를 준다.
        _msg = "문장에서 반영할 내용을 찾지 못했습니다. 달력에서 직접 선택해 주세요."
        if req.wanted_entries is None:
            rejections.append(make_rejection(REJECT_NOTHING_TO_APPLY, _msg))
            return {"ok": False, "code": REJECT_NOTHING_TO_APPLY, "message": _msg}
        # 달력 상태가 함께 왔다면 그것만으로 저장할 것이 있다 — 정식 경로 그대로.
        return None
    picked = list(req.wanted_entries or [])

    def _key(d) -> str:
        return d.isoformat() if hasattr(d, "isoformat") else str(d)

    by_date = {_key(e.date): e for e in picked}
    added = flipped = 0
    for item in analyzed:
        existing = by_date.get(item["date"])
        if existing is None:
            by_date[item["date"]] = WantedEntryItem(**item)
            added += 1
        elif getattr(existing, "intent", "wanted") != item.get("intent", "wanted"):
            # 요청↔금지가 뒤집힌 경우 — 방금 한 말이 이전 저장분보다 최신 의사다.
            by_date[item["date"]] = WantedEntryItem(**item)
            flipped += 1
        elif getattr(existing, "shift_id", None) != item.get("shift_id"):
            # 같은 극성이면 캘린더(직접 선택) 우선 — 기존 규칙 유지.
            # ★ 다만 **코드가 다르면** 문장이 통째로 무시된 것처럼 보인다.
            #   버리는 건 유지하되 무엇이 밀렸는지는 알린다.
            rejections.append(make_rejection(
                REJECT_OVERRIDDEN_BY_CALENDAR,
                f"{item['date']} 은 달력에서 고른 "
                f"'{getattr(existing, 'shift_id', '')}' 을 그대로 두어 문장의 "
                f"'{item.get('shift_id')}' 은 반영하지 않았습니다.",
                date=item["date"], shift_id=item.get("shift_id"),
                intent=item.get("intent", "wanted"),
            ))

    req.wanted_entries = [by_date[k] for k in sorted(by_date)]
    print(f"[preferences] 자연어 분석 병합: {len(analyzed)}건 중 "
          f"신규 {added}건 · 극성전환 {flipped}건 반영 "
          f"(캘린더 {len(picked)}건 중 {len(picked) - flipped}건 유지)")
    return None


def _is_nothing_to_apply(analysis) -> bool:
    """문장만 왔는데 반영할 내용이 없어 **저장을 건너뛰어야 하는** 경우."""
    from services.wanted_service import REJECT_NOTHING_TO_APPLY

    return bool(analysis) and analysis.get("code") == REJECT_NOTHING_TO_APPLY


def _skip_save_and_return_current(req, current_user, db, analysis, rejections):
    """저장을 건너뛰고 **현재 상태 스냅샷**을 돌려준다.

    ★ 문장만 왔는데 반영할 게 한 건도 없는 경우다. 변경이 없으므로 쓰지 않는다 —
      같은 값을 다시 저장하면 자연어 저장으로 취급돼 쓸데없는 draft 가 쌓인다
      (`save_wanted_entries_service` 가 발화 1건마다 새 request_id 를 남긴다).
    ★ 응답 모양은 정상 저장과 **같아야** 한다. 화면이 이 응답을 캐시에 얹기 때문에,
      스냅샷 없는 `{message}` 를 주면 찍어 둔 달력이 사라진다.
    """
    result = get_latest_preference_service(
        year=req.year, month=req.month, current_user=current_user, db=db,
        override_group_id=req.group_id,
    )
    # ★ 조회 응답에는 `rejections` 가 없다. 저장 경로와 **같은 자리**에 실어야
    #   `_finalize_save_response` 가 save_status 를 제대로 매긴다 — 안 실으면
    #   아무것도 반영 못 했는데 `saved` 로 나간다(고치려던 바로 그 증상).
    if isinstance(result, dict):
        result["rejections"] = list(rejections or [])
    return _finalize_save_response(result, analysis)


@router.post("")
async def save_preference_draft(
    req: PreferenceData,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    """
    희망근무 초안 저장 (임시 저장)

    req.wanted_entries 를 보내면 이 호출 1회로 저장이 완결되고 canonical wanted
    snapshot 을 반환한다(/wanted/invoke 불필요).
    """
    try:
        rejections: list = []
        analysis = await _merge_analyzed_request(req, current_user, db, rejections)
        if _is_nothing_to_apply(analysis):
            # 제출과 같은 게이트를 탄다 — 정상 경로도 임시저장에서 마감을 막는다.
            assert_writable(req, current_user, db)
            return _skip_save_and_return_current(
                req, current_user, db, analysis, rejections
            )
        result = submit_preferences_service(
            req, current_user, db, is_draft=True, rejections=rejections
        )
        return _finalize_save_response(result, analysis)
    except HTTPException:
        raise
    except (PreferenceForbiddenError, PreferenceConflictError, PreferenceValidationError) as e:
        db.rollback()
        _raise_domain_error(e)
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"임시 저장 실패: {str(e)}")


@router.post("/submit")
async def submit_preferences(
    req: PreferenceData,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    """
    희망근무 최종 제출

    req.wanted_entries 를 보내면 저장과 제출을 한 트랜잭션으로 원자 처리하고
    canonical wanted snapshot 을 반환한다.
    """
    try:
        # 자연어가 함께 오면 분석해 병합한다 — 이 호출 하나로 분석·저장·제출이 끝난다.
        rejections: list = []
        analysis = await _merge_analyzed_request(req, current_user, db, rejections)
        # 허용 근무코드 검증 (기존 data 기반 경로 전용).
        # wanted_entries 경로는 서비스에서 422 로 검증한다.
        preferences = (
            [shift_id for shift_id in req.data.values() if shift_id]
            if req.wanted_entries is None else []
        )
        if preferences:
            allowed_shifts = {
                row[0] for row in db.query(Shift.shift_id).filter(
                    Shift.group_id == resolve_home_group_id(db, current_user),
                    Shift.show_in_preference == True
                ).all()
            }
            invalid_shifts = [s for s in preferences if s not in allowed_shifts]
            if invalid_shifts:
                raise HTTPException(
                    status_code=400,
                    detail=f"허용되지 않은 근무코드: {', '.join(set(invalid_shifts))}"
                )

        if _is_nothing_to_apply(analysis):
            # ★★ 쓸 내용이 없어 저장은 건너뛰더라도 **마감·중복제출 게이트는
            #   반드시 태운다.** 안 그러면 마감된 달에 눌러도 200 이 나가 처리된
            #   줄 안다. 응답의 `is_submitted` 가 false 로 남고 `rejections` 에
            #   사유가 실려 "제출 안 됨" 이 드러난다.
            assert_writable(req, current_user, db)
            return _skip_save_and_return_current(
                req, current_user, db, analysis, rejections
            )
        result = submit_preferences_service(
            req, current_user, db, is_draft=False, rejections=rejections
        )
        return _finalize_save_response(result, analysis)
    except HTTPException:
        raise
    except (PreferenceForbiddenError, PreferenceConflictError, PreferenceValidationError) as e:
        db.rollback()
        _raise_domain_error(e)
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"제출 실패: {str(e)}")


# [Preferences] - 빈 선호도 최종 제출
@router.post("/submit/empty")
async def submit_empty_preferences(
    req: PreferenceSubmit,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    try:
        return submit_empty_preferences_service(req, current_user, db)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"빈 선호도 제출 실패: {str(e)}")

# [Preferences] - 최종 제출 철회 (수정)
@router.post("/retract")
async def retract_submission(
    req: PreferenceSubmit,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    try:
        return retract_submission_service(req, current_user, db)
    except HTTPException:
        raise
    except Exception as e:
        print('[preferences.py] error', e)
        raise HTTPException(status_code=500, detail=f"제출 철회 실패: {str(e)}")

# [Preferences] - 최신 선호도 데이터 조회
@router.get("/latest")
def get_latest_preference(
    year: int,
    month: int,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    """
    본인 원티드의 canonical snapshot 조회.

    작성 데이터(preference_data.wanted_entries), 제출/마감 상태(submission),
    마감시각(submission.deadline_at), 요청 한도(request_limit)를 한 번에 반환한다.
    """
    try:
        return get_latest_preference_service(
            year, month, current_user, db, override_group_id=group_id
        )
    except HTTPException:
        raise
    except (PreferenceForbiddenError, PreferenceConflictError, PreferenceValidationError) as e:
        _raise_domain_error(e)
    except Exception as e:
        print('error', e)
        raise HTTPException(status_code=500, detail=f"최신 선호도 조회 실패: {str(e)}")

# [Preferences] - 모든 간호사의 희망사항 현황 조회
@router.get("/all")
def get_all_preferences(
    year: int,
    month: int,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    try:
        return get_all_preferences_service(year, month, current_user, db, override_group_id=group_id)
    except HTTPException:
        raise
    except Exception as e:
        print('[preferences.py] error', e)
        raise HTTPException(status_code=500, detail=f"전체 선호도 조회 실패: {str(e)}")


# ──────────────────────────────────────────────────────────────
# [Preferences] 원티드 월별 메모
#   ★ 원티드 저장 경로(POST /preferences)와 **완전히 분리된 통로**다.
#     그 경로는 저장 한 번에 BannedWantedEntry · NurseShiftRequest ·
#     NursePairRequest 를 delete-then-insert 한다. 메모는 입력 중 디바운스로 자주
#     저장되므로 같이 태우면 원티드가 통째로 지워질 위험이 크다.
#   ★ year·month 는 항상 요청에서 받는다. 서버가 현재 월 등으로 추론하지 않는다.
# ──────────────────────────────────────────────────────────────
class MonthlyMemoUpdate(BaseModel):
    year: int
    month: int
    group_id: Optional[str] = None
    monthly_memo: Optional[str] = Field(
        default=None,
        description="월별 메모. null 또는 공백만이면 삭제로 처리한다.",
    )


@router.get("/monthly-memo")
async def get_monthly_memo(
    year: int,
    month: int,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """본인의 그 달 원티드 메모. 저장된 적 없으면 monthly_memo=null."""
    try:
        return get_monthly_memo_service(
            year, month, current_user, db, override_group_id=group_id
        )
    except HTTPException:
        raise
    except (PreferenceForbiddenError, PreferenceConflictError, PreferenceValidationError) as e:
        _raise_domain_error(e)
    except Exception as e:
        print('[preferences.py] monthly-memo 조회 실패', e)
        raise HTTPException(status_code=500, detail=f"월별 메모 조회 실패: {str(e)}")


@router.patch("/monthly-memo")
async def patch_monthly_memo(
    req: MonthlyMemoUpdate,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """월별 메모 저장. wanted_monthly_memo 외의 어떤 테이블도 건드리지 않는다."""
    # ★ 미전송과 명시적 null 을 가른다. PATCH 이므로 안 보낸 필드는 "그대로 두라"는
    #   뜻이고, 삭제는 null 을 **명시**해야 한다. 안 가르면 {year, month} 만 보낸
    #   요청이 메모를 지운다.
    if "monthly_memo" not in req.model_fields_set:
        raise HTTPException(
            status_code=400,
            detail="monthly_memo 를 명시해야 합니다(삭제는 null).",
        )
    try:
        return save_monthly_memo_service(
            req.year, req.month, req.monthly_memo, current_user, db,
            override_group_id=req.group_id,
        )
    except HTTPException:
        raise
    except (PreferenceForbiddenError, PreferenceConflictError, PreferenceValidationError) as e:
        db.rollback()
        _raise_domain_error(e)
    except Exception as e:
        db.rollback()
        print('[preferences.py] monthly-memo 저장 실패', e)
        raise HTTPException(status_code=500, detail=f"월별 메모 저장 실패: {str(e)}")


@router.get("/monthly-memo/group")
async def list_group_monthly_memos(
    year: int,
    month: int,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """관리보드용 — 그룹에서 메모를 쓴 사람만 모아 돌려준다.

    ★ 개인이 쓴 내용이라 수간호사·관리자만 볼 수 있다.
    ★ 메모가 없는 사람은 응답에서 제외한다. 화면이 이름 위 호버로 보여 주므로
      "메모가 있는가" 가 곧 표시 조건이다.
    """
    if not (caller_is_head_nurse(db, current_user)
            or getattr(current_user, "is_master_admin", False)):
        raise HTTPException(status_code=403, detail="Permission denied")
    if group_id:
        assert_caller_can_access_group(db, current_user, group_id)
    elif getattr(current_user, "is_master_admin", False):
        # ★ 관리자는 홈 그룹이 없을 수 있다. 그때 group_id 없이 부르면 대상 그룹을
        #   정할 수 없으므로 400 으로 명확히 돌려준다(500 방지).
        raise HTTPException(status_code=400, detail="group_id 가 필요합니다.")
    try:
        return list_group_monthly_memos_service(
            year, month, current_user, db, override_group_id=group_id
        )
    except HTTPException:
        raise
    except (PreferenceForbiddenError, PreferenceConflictError, PreferenceValidationError) as e:
        _raise_domain_error(e)
    except Exception as e:
        print('[preferences.py] monthly-memo/group 조회 실패', e)
        raise HTTPException(status_code=500, detail=f"월별 메모 목록 조회 실패: {str(e)}")
