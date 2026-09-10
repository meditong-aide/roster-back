import datetime
import os
import time

import numpy as np
import pandas as pd
import requests
from fastapi import APIRouter, Depends, Request, UploadFile, File, HTTPException
from fastapi.templating import Jinja2Templates
from starlette.responses import FileResponse

from datalayer.setting import Setting
from db.client2 import msdb_manager
from routers.auth import require_current_user
from schemas.auth_schema import User as UserSchema
from utils import utils
from utils.security import create_access_token

router = APIRouter()

templates = Jinja2Templates(directory="templates")

#: 한 번에 IN 절에 넣을 MemberID 수. MSSQL 파라미터 상한(2100)에 걸리지 않게 끊는다.
_ID_CHUNK = 500

#: 생성 대조 조회의 대기 상한(초). ASP 가 계정을 만든 **뒤**에 도는 조회라, 공용 그룹웨어
#  DB 가 느려져도 요청이 매달리지 않게 반드시 끊는다. 무한 대기가 기본이다(client2.py:28).
_VERIFY_TIMEOUT_SEC = 30

#: 그룹웨어 처리 페이지(ASP) 호출의 (연결, 응답) 대기 상한(초). `requests` 기본은 무한이다.
#  스테이징이 이미 커밋된 뒤 도는 호출이라, 응답이 안 오면 생성 여부가 불확정해진다.
#  계정 수만큼 처리가 길어질 수 있어 응답 쪽은 넉넉히 잡는다.
_ASP_TIMEOUT_SEC = (10, 120)

#: 비-200 응답 뒤 재조회 간격(초). 502/503 은 앞단 프록시가 끊은 것일 수 있어 원 요청이
#  뒤에서 계속 돌 수 있다. 그 경우 즉시 조회하면 아직 INSERT 전이라 빈 결과가 나온다.
#  ★ 200 경로에는 쓰지 않는다 — 처리가 끝났다는 신호를 받았으므로 응답만 느려진다.
_ASP_RECHECK_DELAYS_SEC = (2, 5)

#: 스테이징 쓰기(삭제·대량삽입)의 대기 상한(초). `execute`/`bulk_execute` 도 기본은 무한이다.
#  ★ `def` 라우터 전환은 **이벤트 루프만** 지킨다. 쓰기가 락에 물리면 그동안 Starlette 공용
#    스레드풀 토큰을 점유하고, 쌓이면 다른 동기 엔드포인트까지 실행되지 못한다.
#    읽기보다 락 경합에 걸리기 쉬우므로 상한을 조금 더 넉넉히 잡되 반드시 끊는다.
_STAGING_TIMEOUT_SEC = 60


def _norm_id(v) -> str:
    """아이디 대조용 정규화 — 앞뒤 공백 제거 + 소문자.

    ★ `bizwiz20db.Member_Login.MemberID` 의 컬레이션은 `Korean_Wansung_CI_AS` 다.
      SQL 의 `IN` 은 대소문자를 가리지 않고 뒤 공백도 무시하는데, 파이썬 비교만
      대소문자를 가리면 `User01` 로 올린 계정이 DB 에 `user01` 로 저장돼 있을 때
      **만들어졌는데 실패로** 보고된다. 양쪽을 같은 기준으로 맞춘다.
    """
    return str(v or "").strip().lower()


def _created_member_ids(office_code: str, member_ids: list[str]) -> set[str]:
    """주어진 아이디 중 **실제로 그룹웨어 계정이 만들어진 것**의 집합(정규화된 키).

    ★ 그룹웨어 처리 페이지가 200 을 줘도 일부는 생성되지 않는다. 그 차이를 여기서 잡는다.
      상세는 `Setting.member_created_check` 주석 참조.
    ★ 조회 실패를 빈 집합으로 눕히지 않는다 — 그러면 전원 실패로 보고돼 실제로 생성된
      계정까지 안 만들어진 것처럼 보인다. 예외는 호출부로 올린다.
    ★★ 타임아웃을 반드시 건다. 이 조회는 **ASP 가 계정을 이미 만든 뒤**에 돈다.
      `fetch_all` 은 timeout 을 안 주면 무한 대기가 기본이라(`db/client2.py:28`),
      공용 그룹웨어 DB 가 느려지면 요청이 영영 안 끝나고 **어떤 계정이 만들어졌는지
      사용자가 알 수 없는 채로** 매달린다. 그 상태에서 재시도하면 부분 성공이 겹친다.
      끊고 예외를 올려 "생성은 됐는데 확인 실패" 로 보고되게 하는 편이 낫다.
    """
    found: set[str] = set()
    uniq = [m for m in dict.fromkeys(member_ids) if m]
    for i in range(0, len(uniq), _ID_CHUNK):
        chunk = uniq[i:i + _ID_CHUNK]
        rows = msdb_manager.fetch_all(
            Setting.member_created_check(len(chunk)),
            params=(office_code,) + tuple(chunk),      # ★ 내 오피스로 스코프
            timeout=_VERIFY_TIMEOUT_SEC,
        )
        found.update(_norm_id(r["MemberID"]) for r in rows if r.get("MemberID"))
    return found
def _verify_after_asp(office_code: str, member_ids: list, reason: str | None = None,
                      pre_existing: set[str] | None = None) -> dict:
    """ASP 호출 **뒤** 실제 생성분을 대조해 결과를 가른다.

    ★★ 동기 함수인 것이 의도다. 호출부 `create_upload_file` 은 **`def` 라우터**라 FastAPI 가
      스레드풀에서 돌린다 — 여기서 자거나 동기 DB 를 불러도 이벤트 루프를 막지 않는다.
      예전엔 그 라우터가 `async def` 여서 동기 DB 호출 10여 곳과 이 대기가 전부 루프를
      세웠다. 특히 재조회 대기는 프록시·ASP 장애로 비-200 이 연속될 때 가장 자주 도는
      경로라, 외부 장애가 워커 전면 지연으로 증폭됐다.
      ★ 이 함수를 async 로 되돌린다면 호출부의 동기 DB 호출도 함께 옮겨야 한다.

    ★★ 200 을 성공으로 확정하지 않는다. 그룹웨어가 200 을 주더라도 일부만 생성될 수 있다 —
      실측(2025-11-15 업로드분): 10건 중 6건만 생성되고 4건은 같은 사람이 이미 다른
      아이디로 존재해 거부됐는데, 종전 코드는 전원 성공으로 보고했다.
    ★★ **비-200 도 같은 대조를 거친다.** ASP 가 계정을 만든 뒤 내부 오류로 500 을 내거나
      프록시가 502/503 을 줄 수 있어, HTTP 상태만으로 "부작용 없음" 을 보장할 수 없다.
      예전엔 비-200 을 곧장 실패로 돌려줘 프론트가 안전한 실패로 읽고 재업로드했고,
      그러면 중복·부분 성공이 겹쳤다. `reason` 이 있으면 그 맥락을 결과에 실어 준다.
    ★ 대조 대상은 중복을 턴 아이디 목록이다. 그래야 `created_count` 가 "올린 행 수" 와
      같은 뜻이 된다 — 겹친 아이디를 그대로 세면 생성 1건인데 2행 성공으로 읽힌다.
    ★★ `pre_existing` — **ASP 호출 직전에 이미 존재하던** 아이디 집합이다. 이 조회는 계정의
      존재만 볼 수 있어서, 사전 중복검사 뒤 다른 요청이 같은 아이디를 먼저 만들면 그것을
      이번 배치의 성공으로 잘못 귀속한다. 기준 상태를 빼서 **이번 호출로 새로 생긴 것만**
      센다.
      ※ 완전한 해법은 아니다 — ASP 가 도는 **중에** 다른 요청이 만들면 여전히 섞인다.
        그건 batch ID 를 스테이징과 ASP 계약에 넣어야 닫히고, ASP 는 이 저장소 밖이다.
        그래서 `succeed` 가 "이 배치가 만들었다" 를 보장한다고 말하면 안 된다 —
        "요청한 아이디가 지금 모두 존재한다" 까지가 이 함수가 아는 전부다.
    """
    uploaded, seen_norm = [], set()
    for m in member_ids:
        m = str(m or "").strip()
        k = _norm_id(m)
        if k and k not in seen_norm:
            seen_norm.add(k)
            uploaded.append(m)          # 표기는 사용자가 올린 그대로 보존한다

    # 이 조회가 실패해도 **계정은 이미 만들어진 뒤**다. 예외를 그대로 올리면 라우터
    # catch-all 이 일반 500 으로 바꿔 버리고, 사용자는 무엇이 만들어졌는지 모른 채
    # 재시도해 부분 성공을 겹치게 된다. 불확정 결과로 명시해 돌려준다.
    # ★ `None` 은 "기준을 못 구했다" 는 뜻이다(빈 집합 = "아무도 없었다" 와 구분한다).
    #   이때는 차집합이 남이 만든 계정까지 삼키므로 귀속 계산을 신뢰할 수 없다.
    _base = pre_existing or set()
    try:
        present = _created_member_ids(office_code, uploaded)
    except Exception as exc:
        return {
            "result": "outcome_unknown",
            "reason": (reason + " " if reason else "") + f"생성 여부 확인에 실패했습니다: {type(exc).__name__}",
            "retry_safe": False,
            "uploaded_member_ids": uploaded,
        }

    # ★★ 기준을 못 구했으면(`None`) **어떤 귀속도 하지 않는다.** 예전엔 이 검사를 "누락이
    #   없을 때" 에만 걸어서, 200 응답 뒤 일부만 존재하는 경로가 그대로 `partial_success` 로
    #   빠졌다 — 경쟁 요청이 만든 계정을 이번 배치가 만든 1건으로 확정하는 셈이다.
    #   존재/미존재 목록은 참고로 주되, `created_count`·`failed_member_ids` 처럼 **이번 배치의
    #   결과로 단정하는 필드는 쓰지 않는다.**
    if pre_existing is None:
        return {
            "result": "outcome_unknown",
            "reason": (reason + " " if reason else "")
                      + "사전 기준 조회에 실패해 이번 업로드로 생성된 것인지 가릴 수 없습니다.",
            "retry_safe": False,
            "uploaded_member_ids": uploaded,
            # 참고용 — 이번 배치가 만들었다는 뜻이 아니라 "지금 존재한다" 는 사실만 담는다.
            "present_member_ids": [m for m in uploaded if _norm_id(m) in present],
            "absent_member_ids": [m for m in uploaded if _norm_id(m) not in present],
        }

    created = present - _base
    failed = [m for m in uploaded if _norm_id(m) not in created]

    # ★★ 비-200 경로는 **미생성을 확정하지 않는다.** 200 은 ASP 가 처리를 끝냈다는 신호지만,
    #   502/503 은 앞단 프록시가 끊은 것일 뿐 원 요청이 뒤에서 계속 돌고 있을 수 있다.
    #   그 순간 조회하면 아직 INSERT 전이라 정상적으로 빈 결과가 나온다 — NOLOCK 제거는
    #   미커밋 행을 읽는 **거짓 양성**만 막았고, 이 **거짓 음성**은 그대로다.
    #   여기서 partial_success 로 굳히면 실제로는 곧 생기는 계정을 "실패" 로 알려 주고
    #   운영자가 불필요한 수동 복구를 하게 된다.
    #   그래서 비-200 에서만 짧게 재조회하고, 그래도 안 보이면 outcome_unknown 이다.
    #   (200 경로는 이 지연 가정이 없으므로 응답 시간을 늘리지 않는다.)
    if failed and reason:
        for _delay in _ASP_RECHECK_DELAYS_SEC:
            time.sleep(_delay)
            try:
                created = _created_member_ids(office_code, uploaded) - _base
            except Exception:
                break       # 재조회 실패는 아래 outcome_unknown 으로 흡수한다
            failed = [m for m in uploaded if _norm_id(m) not in created]
            if not failed:
                break
        if failed:
            return {
                "result": "outcome_unknown",
                "reason": reason + " 재조회 후에도 일부 계정이 보이지 않습니다."
                                   " 그룹웨어에서 처리가 진행 중일 수 있습니다.",
                "retry_safe": False,
                "created_count": len(created),
                "pending_member_ids": failed,
                "uploaded_member_ids": uploaded,
            }

    if not failed:
        # (기준 미확보는 위에서 이미 outcome_unknown 으로 빠졌으므로 여기 도달하지 않는다.)
        # 비-200 이었는데 전원 생성돼 있으면, 실패로 보고하면 안 된다(재업로드를 부른다).
        out = {"result": "succeed", "created_count": len(created)}
        if reason:
            out["message"] = reason + " 다만 대조 결과 요청한 계정이 모두 생성돼 있습니다."
        return out

    # 200 을 받았는데 일부가 없는 경우 = 그룹웨어가 실제로 거부한 것이다(처리는 끝났다).
    # 이미 만들어진 계정을 되돌리지는 않는다(그룹웨어 소관) —
    # 대신 누가 빠졌는지 그대로 돌려줘 사용자가 조치할 수 있게 한다.
    return {
        "result": "partial_success",
        "created_count": len(created),
        "failed_count": len(failed),
        "failed_member_ids": failed,
        "retry_safe": False,            # 이미 만들어진 계정이 있으므로 통째 재업로드 금지
        # 거부 사유는 그룹웨어만 안다. 여기서 단정하지 않고 가장 흔한 원인만 안내한다.
        "message": "일부 계정이 생성되지 않았습니다. 같은 사람이 이미 다른 아이디로 등록돼 있는지 확인하세요.",
    }


DOWNLOAD_FOLDER = "downloads"


@router.get("/member_upload", summary="회원 엑셀 업로드 화면을 출력합니다.")
def excelupload_form(request: Request, current_user: UserSchema = Depends(require_current_user)):
    OfficeCode = current_user.office_id
    EmpSeqNo = current_user.EmpSeqNo

    filename = 'easysetting_member.xls'
    rows = msdb_manager.fetch_all(Setting.list_member(), params=(OfficeCode, EmpSeqNo))

    return templates.TemplateResponse("member_excel.html", {"request": request, "filename": filename})


@router.post("/member_upload", summary="회원 엑셀을 DB에 저장합니다.")
def create_upload_file(
    current_user: UserSchema = Depends(require_current_user),
    file: UploadFile = File(...)
):
    OfficeCode = current_user.office_id
    EmpSeqNo = current_user.EmpSeqNo
    RegDate = datetime.datetime.now()
    # 엑셀 → pandas 변환
    df = utils.excel_to_pandas_sync(file)
    df['num'] = range(1, len(df) + 1)
    # 컬럼명 매핑
    # df = df.rename(columns={
    #     '사번': 'EmpNum', '회원 아이디': 'MemberID', '이름': 'EmployeeName', '성별': 'Gender',
    #     '생년월일': 'Birthday', '입사년월': 'JoinDate', '전화번호': 'Tel', '휴대폰 번호': 'PortableTel',
    #     '이메일': 'Ck_Email', '주소': 'address', '부서장': 'Manager', '상위부서': 'Depth1',
    #     '하위부서1': 'Depth2', '하위부서2': 'Depth3', '직위': 'position', '경력': 'career',
    #     '직무': 'duty', '수간호사여부': 'headnurse', '킵여부': 'nightkeep'
    # })
    # 엑셀컬럼을 영문으로 수정
    df = df.rename(columns={'사번': 'EmpNum', '회원 아이디': 'MemberID', '이름': 'EmployeeName', '성별': 'Gender', '생년월일': 'Birthday', '입사년월': 'JoinDate', '전화번호': 'Tel'
        , '휴대폰 번호': 'PortableTel', '이메일': 'Ck_Email', '주소': 'address', '부서장': 'Manager', '상위부서': 'Depth1', '하위부서1': 'Depth2', '하위부서2': 'Depth3', '직위': 'position'
        , '경력': 'career', '직무': 'duty', '수간호사여부': 'headnurse', '킵여부': 'nightkeep'})
    # ★★ MemberID 를 **읽은 직후 한 번만** 다듬는다. 이후 파일내 중복검사 · DB 중복조회 ·
    #   스테이징 저장 · 생성 대조가 전부 이 값을 쓴다.
    #   예전엔 비교하는 자리에서만 strip 했고 SQL 파라미터와 스테이징에는 원본이 갔다.
    #   컬레이션(`Korean_Wansung_CI_AS`)이 대소문자는 같게 보지만 **앞뒤 공백은 다르게 본다** —
    #   `" user01"` 은 사전 중복검사에서 기존 `user01` 을 놓치고, ASP 가 trim 해서 저장하면
    #   사후 조회가 공백 붙은 값으로 찾아 **실제로 만들어진 계정을 실패로 보고**한다.
    #   ★ 대소문자는 바꾸지 않는다. 그룹웨어에 실제로 저장되는 아이디라 소문자로 눕히면
    #     사용자가 받은 아이디가 달라진다. 대소문자 무시는 컬레이션과 `_norm_id` 가 맡는다.
    if 'MemberID' in df.columns:
        # ★★ `astype(str)` 은 결측을 문자열 `'nan'` 으로 바꾼다. 그대로 두면 아래 필수값
        #   검사(`isna()`)를 통과해 **`nan` 이라는 아이디로 계정이 만들어진다.**
        #   (headnurse 에서 같은 함정을 막아 놓고 여기서 되풀이했던 자리다.)
        #   다듬은 뒤 빈 값·`'nan'` 을 결측으로 되돌려 필수값 검사가 잡게 한다.
        #   공백만 넣은 칸도 같은 취급이다 — 아이디가 될 수 없다.
        #   ★ 결측 판정은 **변환 전에** 한다. 변환 후 문자열로 거르면 `None` → `'None'`,
        #     `NaN` → `'nan'`, `NaT` → `'NaT'` 로 표기가 갈려 하나씩 빠뜨린다.
        #     반대로 사용자가 실제로 입력한 `'nan'` 같은 문자열은 존중한다(아이디일 수 있다).
        _mid_missing = df['MemberID'].isna()
        df['MemberID'] = df['MemberID'].astype(str).str.strip()
        df.loc[_mid_missing | df['MemberID'].eq(''), 'MemberID'] = np.nan

    # ★ 형변환은 **검증 뒤로** 옮겼다. 여기서 astype(int) 를 하면 경력·생년월일이 빈칸일 때
    #   검증표 대신 ValueError → 500 이 난다(사용자는 어느 행이 문제인지 알 수 없다).
    #   ★★ 다만 미루기만 하면 안 된다. 엑셀 숫자열에 빈 칸이 하나라도 있으면 pandas 가 그
    #     열을 float 로 추론해 정상값도 `19900101.0` 이 되고 `^\d{8}$` 에 걸린다 —
    #     **빈 칸 하나 때문에 같은 파일의 멀쩡한 행까지 막힌다.** 검증용 문자열을 따로 만든다.
    def _digits_for_check(v) -> str:
        """검증용 문자열. 유한한 정수형 숫자는 소수점을 떼고, 결측은 빈 문자열로."""
        if pd.isna(v):
            return ''
        if isinstance(v, (int, np.integer)):
            return str(int(v))
        if isinstance(v, (float, np.floating)):
            return str(int(v)) if float(v).is_integer() else str(v)
        return str(v).strip()

    # 오류 수집을 위한 리스트
    error_rows = []

    # 헬퍼 함수 — 오류 row + 라벨 append
    def add_error(df_error, label):
        if df_error.empty:
            return
        temp = df_error.copy()
        temp["errorType"] = label
        error_rows.append(temp)

    # ===== 1. 중복 데이터 =====
    duplicates = df[df.duplicated()]
    add_error(duplicates, "중복데이터")
    # ===== 1-2. 파일 안에서 회원아이디가 겹치는 행 =====
    # ★ 위 `df.duplicated()` 는 **모든 칸이 똑같은 행**만 잡는다. 아이디는 같은데 사번이나
    #   이름이 다르면 통과하는데, 그룹웨어는 아이디 하나당 계정을 하나만 만든다.
    #   그러면 생성 대조(`_created_member_ids`)에서 두 행이 같은 아이디를 보고 **둘 다
    #   생성됨으로 읽혀** 전원 성공으로 보고된다. 올리기 전에 막는다.
    # ★ `keep=False` 로 겹친 행을 **전부** 보여준다 — 사용자가 어느 쪽이 맞는지 골라야 한다.
    # ★ 컬레이션이 CI 라 `User01` 과 `user01` 은 같은 계정 하나가 된다. 소문자로 맞춰 본다.
    member_id_norm = df['MemberID'].astype(str).str.strip().str.lower()
    dup_id_mask = member_id_norm.duplicated(keep=False) & df['MemberID'].notna()
    add_error(df[dup_id_mask], "회원아이디파일내중복")
    # ===== 2. 필수값 누락 =====
    required_cols = [
        'EmpNum', 'MemberID', 'EmployeeName', 'Gender', 'Birthday', 'Ck_Email', 'address', 'Depth1', 'position'
    ]
    null_mask = df[required_cols].isna().any(axis=1)
    add_error(df[null_mask], "필수값누락")
    # ===== 3. 전화번호 오류 =====
    pattern_tel = r'^\d{3}-\d{4}-\d{4}$'
    tel_mask = ~(df[['PortableTel']].astype(str).apply(lambda x: x.str.match(pattern_tel)).all(axis=1)) & ~(df[['PortableTel']].isnull().all(axis=1))
    add_error(df[tel_mask], "전화번호오류")
    # ===== 4. 이메일 오류 =====
    pattern_email = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
    email_mask = ~df['Ck_Email'].astype(str).str.match(pattern_email)
    add_error(df[email_mask], "이메일오류")
    # ===== 5. 생년월일 오류 =====
    pattern_birth = r'^\d{8}$'
    # float 로 추론된 열(`19900101.0`)도 정상 판정되도록 `_digits_for_check` 를 거친다.
    birth_mask = ~df['Birthday'].map(_digits_for_check).str.match(pattern_birth)
    add_error(df[birth_mask], "생년월일패턴오류")
    # ===== 6. 날짜패턴 오류 =====
    pattern_date = r'^\d{4}-\d{2}-\d{2}$'
    date_mask = ~(df['JoinDate'].astype(str).str.match(pattern_date)) & ~(df['JoinDate'].isnull())
    add_error(df[date_mask], "입사일패턴오류")
    # ===== 7. 성별 오류 =====
    # ★ 예전 조건 `~(isin([...]) | ~isnull())` 은 **항상 오류 0건**이었다. `~isnull()` 이
    #   값이 있는 행마다 True 라 OR 이 전부 True 가 되고, 부정하면 전부 False 가 된다.
    #   즉 '남'/'여' 가 아닌 어떤 값도 그대로 통과해 그룹웨어에 들어갔다.
    #   빈 값은 위 필수값 검사가 잡으므로 여기서는 "값이 있는데 허용값이 아닌" 것만 본다.
    gender_mask = ~df['Gender'].isin(['남', '여']) & df['Gender'].notna()
    add_error(df[gender_mask], "성별값오류")
    # ===== 8. 부서장 값 오류 =====
    manager_mask = ~(df['Manager'].isin(['부서장']) | df['Manager'].isnull())
    add_error(df[manager_mask], "부서장값오류")
    # ===== 9. headnurse Y/N 오류 =====
    # ★★ 순서가 중요하다. `astype(str)` 을 먼저 하면 결측이 문자열 `'NAN'` 이 되어
    #   "값이 있는 행" 으로 둔갑한다. 그래서 예전엔 빈칸이 검증을 통과하고 그대로 저장돼,
    #   다른 오피스에 `headnurse='NAN'` 이 **156건** 쌓였다(실측).
    #   결측 여부를 먼저 붙잡아 두고, 빈 값은 빈 문자열로 되돌려 저장한다.
    _hn_missing = df['headnurse'].isna()
    df['headnurse'] = df['headnurse'].astype(str).str.strip().str.upper()
    df.loc[_hn_missing, 'headnurse'] = ''
    # 수간호사 여부는 필수가 아니다 — 비워 두면 통과, 값이 있는데 Y/N 이 아니면 오류.
    head_mask = ~df['headnurse'].isin(['Y', 'N']) & ~_hn_missing
    add_error(df[head_mask], "수간호사값오류")
    # ===== 10. career 숫자형 오류 =====
    # 생년월일과 같은 이유로 `_digits_for_check` 를 거친다(빈 칸 하나에 열이 float 가 된다).
    num_mask = ~df['career'].map(_digits_for_check).str.match(r'^\d+$') & df['career'].notna()
    add_error(df[num_mask], "경력숫자형오류")
    # ===== 11. Depth1 / Depth2 / Depth3 에러 =====
    # Depth1
    depth1_list = [x['depth1'] for x in msdb_manager.fetch_all(Setting.select_division_depth1(), params=OfficeCode, timeout=_VERIFY_TIMEOUT_SEC)]
    depth1_mask = ~df['Depth1'].isin(depth1_list)
    add_error(df[depth1_mask], "상위부서오류")
    # Depth2
    depth2_list = [x['depth2'] for x in msdb_manager.fetch_all(Setting.select_division_depth2(), params=OfficeCode, timeout=_VERIFY_TIMEOUT_SEC)]
    depth2_mask = ~(df['Depth2'].isin(depth2_list) | df['Depth2'].isna())
    add_error(df[depth2_mask], "중간부서오류")
    # Depth3
    depth3_list = [x['depth3'] for x in msdb_manager.fetch_all(Setting.select_division_depth3(), params=OfficeCode, timeout=_VERIFY_TIMEOUT_SEC)]
    depth3_mask = ~(df['Depth3'].isin(depth3_list) | df['Depth3'].isna())
    add_error(df[depth3_mask], "하위부서오류")
    # ===== 12. MemberID DB 중복 =====
    # ★ 예전엔 행마다 조회를 던졌다(500행이면 왕복 500번). 한 번에 IN 으로 묶는다 —
    #   NOLOCK 을 뺀 뒤로는 직렬 조회가 락 경합에 그대로 노출되므로 왕복 수가 곧 위험이다.
    _ids = [str(v).strip() for v in df['MemberID'].tolist() if str(v or "").strip()]
    _taken: set[str] = set()
    for _i in range(0, len(_ids), _ID_CHUNK):
        _chunk = _ids[_i:_i + _ID_CHUNK]
        _rows = msdb_manager.fetch_all(
            Setting.member_ids_taken(len(_chunk)),
            params=tuple(_chunk),
            timeout=_VERIFY_TIMEOUT_SEC,
        )
        _taken.update(_norm_id(r["MemberID"]) for r in _rows if r.get("MemberID"))
    dup_db_mask = df['MemberID'].map(lambda v: _norm_id(v) in _taken if pd.notna(v) else False)
    add_error(df[dup_db_mask], "회원아이디중복")
    # ======= 최종 오류 테이블 생성 =======
    if error_rows:
        error_df = pd.concat(error_rows, ignore_index=True).replace({np.nan: ''})
        error_df.drop_duplicates(inplace=True)
        return error_df.to_dict(orient='records')   # ← 배열 그대로 반환 (프론트가 테이블로 렌더링)
    # ======= 오류 없음 → DB 저장 =======
    df['OfficeCode'] = OfficeCode
    df['EmpSeqNo'] = EmpSeqNo
    df['RegDate'] = RegDate
    df = df.replace({np.nan: ''})

    # ★★ 정수화는 **빈값 치환 뒤**에 한다. 순서가 셋 다 이유가 있다.
    #   ① 검증보다 먼저 `astype(int)` 를 하면 빈칸 하나에 요청 전체가 500 으로 죽는다.
    #   ② 검증 뒤로 미루기만 해도 부족하다 — `career` 는 **선택값**이라(required_cols 에 없고
    #      숫자 검사도 `notna()` 로 결측을 건너뛴다) 빈 칸이 검증을 통과해 여기까지 오고,
    #      `astype(int)` 가 IntCastingNaNError 를 던진다.
    #   ③ 결측을 남긴 채 `map(int)` 를 하면 pandas 가 int+NaN 을 **다시 float 로 추론**해
    #      `5.0` 이 그룹웨어에 저장된다(`astype(object)` 를 먼저 걸어도 마찬가지다 — 실측).
    #   그래서 `replace` 로 결측을 빈 문자열로 바꾼 뒤 정수화한다. 그러면 int 와 '' 가 섞인
    #   object 열이 되어 정수는 정수로, 빈 칸은 빈 문자열로 저장된다.
    _int_or_blank = lambda v: int(v) if v != '' and pd.notna(v) else ''
    df['Birthday'] = df['Birthday'].map(_int_or_blank)
    df['career'] = df['career'].map(_int_or_blank)

    insert_cols = [
        'num','OfficeCode', 'EmpSeqNo', 'EmpNum', 'MemberID', 'EmployeeName',
        'Gender', 'Birthday', 'JoinDate', 'Tel', 'PortableTel', 'Ck_Email',
        'address', 'Manager', 'Depth1', 'Depth2', 'Depth3', 'position',
        'RegDate', 'career', 'duty', 'headnurse', 'nightkeep'
    ]
    
    df = df[insert_cols]
    data_to_insert = [tuple(r) for r in df.itertuples(index=False)]
    # ★★ 넣을 게 없으면 **지우지도 않는다.** `delete` 와 `insert` 는 각각 다른 연결에서
    #   따로 커밋되므로, 빈 파일로 delete 만 돌면 사용자가 앞서 준비해 둔 스테이징이
    #   영구히 사라진다(빈 엑셀은 위 검증을 오류 없이 통과한다 — 검사할 행 자체가 없다).
    #   ★ 이건 그 경로 하나만 막는 것이다. insert 가 락 타임아웃·연결 오류로 실패하는
    #     경우의 손실은 delete+insert 를 한 트랜잭션으로 묶어야 없어지는데, 그러려면
    #     `msdb_manager` 에 전용 연결·트랜잭션 API 가 필요하다(동시 업로드 직렬화와 같은
    #     구조 작업이라 함께 다뤄야 한다).
    if not data_to_insert:
        return {"result": "empty_file", "message": "업로드할 행이 없습니다. 파일을 확인하세요."}
    msdb_manager.execute(Setting.delete_member(), params=(OfficeCode, EmpSeqNo),
                         timeout=_STAGING_TIMEOUT_SEC)
    rows = msdb_manager.bulk_execute(Setting.insert_member(), data_to_insert,
                                     timeout=_STAGING_TIMEOUT_SEC)
    if not rows:
        return {"result": "insert_fail"}
    # 모바일 설정 정보 저장
    # ★ 쿼리가 `WHERE NOT EXISTS` 라 파라미터가 (MemberID, RegDate, MemberID) 셋이다.
    #   이미 설정 행이 있는 아이디는 건너뛴다 — 실패분을 고쳐 다시 올려도 중복이 안 쌓인다.
    member_ids = df["MemberID"].astype(str).tolist()
    mobile_setting_params = [(member_id, RegDate, member_id) for member_id in member_ids]
    msdb_manager.bulk_execute(Setting.insert_mobile_user_setting_list(), mobile_setting_params,
                              timeout=_STAGING_TIMEOUT_SEC)
    # ===== 외부 API 호출 =====
    # ★★ ASP 를 부르기 **직전**의 기준 상태를 찍어 둔다. 사후 대조는 계정의 존재만 볼 수
    #   있어서, 사전 중복검사 뒤 같은 오피스의 다른 요청이 같은 아이디를 먼저 만들면
    #   그걸 이번 배치의 성공으로 잘못 귀속한다. 차집합으로 **이번 호출로 새로 생긴 것**만 센다.
    #   ★ 조회가 실패하면 **빈 집합으로 눕히면 안 된다.** 빈 집합은 "아무도 없었다" 라는
    #     사실 주장이라, 사후 조회만 회복되면 남이 만든 계정까지 차집합에 들어와 조용히
    #     succeed 가 난다 — 이 기준을 넣은 이유가 그대로 무효가 된다.
    #     `None` 으로 넘겨 "기준을 모른다" 를 구분하고, 그 경우 결과를 succeed 로 굳히지 않는다.
    _pre_existing: set[str] | None = None
    for _attempt in range(2):
        try:
            _pre_existing = _created_member_ids(OfficeCode, [str(m or "").strip() for m in member_ids])
            break
        except Exception:
            if _attempt == 0:
                time.sleep(1)
    token = create_access_token(data={"clientSecret": os.getenv("CLIENT_SECRET"), "clientId": os.getenv("CLIENT_ID")})
    # ★★ 타임아웃을 반드시 건다. `requests` 는 기본이 **무한 대기**다.
    #   이 호출 시점엔 스테이징·모바일설정이 이미 커밋돼 있고, 그룹웨어가 요청을 처리했는데
    #   응답만 못 끝내면 워커와 사용자가 무기한 매달린다. 그 상태에서 사용자가 다시 올리면
    #   **첫 호출이 무엇을 만들었는지 모르는 채로 같은 배치를 재실행**하게 된다.
    #   끊고 "결과를 모른다" 를 명시적으로 돌려주는 편이 낫다 — 아래 outcome_unknown.
    # ★ `requests` 는 동기라 그대로 부르면 응답 상한(120초)만큼 이벤트 루프를 막는다.
    #   이 라우터가 async 이므로 스레드로 뺀다.
    try:
        response = requests.post(
            "https://gw.meditong.com/bizadmin/setting/member_excel_ai_ok.asp",
            data=f"officeCode={OfficeCode}&EmpSeqNo={EmpSeqNo}&Token={token}",
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
            timeout=_ASP_TIMEOUT_SEC,
        )
    except requests.RequestException as exc:
        # ★★ 응답을 못 받았을 뿐, ASP 가 계정을 만들었을 수도 있다. 비-200 과 **똑같이**
        #   사후 대조를 거친다 — 예전엔 여기서 곧장 전원 unknown 으로 돌려줘, 실제로는
        #   만들어진 계정까지 "확인 불가" 로 부풀렸고 운영자가 수동 복구를 하게 됐다.
        #   재조회까지 돌려서 전원 확인되면 succeed, 남는 것만 pending 으로 준다.
        return _verify_after_asp(
            OfficeCode, member_ids,
            reason=f"그룹웨어 처리 응답을 받지 못했습니다({type(exc).__name__}).",
            pre_existing=_pre_existing,
        )
    # ★★ 비-200 도 **부작용 여부는 타임아웃과 똑같이 불확정**이다. ASP 가 계정을 일부·전부
    #   만든 뒤 내부 오류로 500 을 내거나, 앞단 프록시가 502/503 을 줄 수 있다. HTTP 상태만
    #   보고 "아무 일도 없었다" 고 단정할 수 없다.
    #   예전엔 여기서 곧장 external_api_fail 을 돌려줘, 프론트가 안전한 실패로 읽고
    #   재업로드하면 중복·부분 성공이 겹쳤다. 성공 경로와 **같은 사후 대조**를 먼저 돌린다.
    if response.status_code != 200:
        return _verify_after_asp(
            OfficeCode, member_ids,
            reason=f"그룹웨어 처리가 실패로 응답했습니다(HTTP {response.status_code}).",
            pre_existing=_pre_existing,
        )

    return _verify_after_asp(OfficeCode, member_ids, pre_existing=_pre_existing)

# @router.post("/member_upload", summary="회원 엑셀을 DB에 저장합니다.")
# async def create_upload_file(current_user: UserSchema = Depends(get_current_user_from_cookie), file: UploadFile = File(...)):
#     """
#     **회원 엑셀파일을 업로드 하여 DB에 저장합니다.**
#     - 양식 엑셀파일 : easysetting_member.xls
#     - 사번, 회원 아이디, 이름, 성별, 생년월일, 입사년월, 전화번호, 휴대폰 번호, 이메일, 주소, 부서장, 상위부서, 하위부서1, 하위부서2, 직위, 경력, 직무, 수간호사여부, 킵여부 으로 구성된 엑셀파일을 업로드 합니다.
#     - 사번 중복체크 하여 첫번째 내용을 제외하고 나머지는 제거
#     - 회원 아이디 중복체크 하여 첫번째 내용을 제외하고 나머지는 제거
#     """

#     # 쿠키값에서 가져오도록 수정
#     OfficeCode = current_user.office_id
#     EmpSeqNo = current_user.EmpSeqNo
#     RegDate = datetime.datetime.now()

#     # excel type을 확인해서 pandas로 변환해주는 함수 : excel_to_pandas
#     df = utils.excel_to_pandas_sync(file)
#     df['num'] = range(1, len(df) + 1)

#     # 엑셀컬럼을 영문으로 수정
#     df = df.rename(columns={'사번': 'EmpNum', '회원 아이디': 'MemberID', '이름': 'EmployeeName', '성별': 'Gender', '생년월일': 'Birthday', '입사년월': 'JoinDate', '전화번호': 'Tel'
#         , '휴대폰 번호': 'PortableTel', '이메일': 'Ck_Email', '주소': 'address', '부서장': 'Manager', '상위부서': 'Depth1', '하위부서1': 'Depth2', '하위부서2': 'Depth3', '직위': 'position'
#         , '경력': 'career', '직무': 'duty', '수간호사여부': 'headnurse', '킵여부': 'nightkeep'})
#     df['Birthday'] = df['Birthday'].astype(int)
#     df['career'] = df['career'].astype(int)

#     # 중복데이터 체크
#     duplicates = df[df.duplicated()]
#     # 필수값 Null 체크
#     null_df = df[['EmpNum', 'MemberID', 'EmployeeName', 'Gender', 'Birthday', 'JoinDate', 'Tel', 'PortableTel', 'Ck_Email',
#          'address', 'Depth1', 'position', 'career', 'duty', 'headnurse']].isna().any(axis=1)
#     filtered_df = df[null_df]

#     # 전화번호 패턴
#     pattern_tel = r'^\d{3}-\d{4}-\d{4}$'
#     no_phone_mask = ~df[['Tel', 'PortableTel']].astype(str).apply(lambda x: x.str.match(pattern_tel)).any(axis=1)
#     filtered_phone_df = df[no_phone_mask]

#     # 이메일 패턴
#     pattern_email = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
#     no_email_mask = ~df[['Ck_Email']].astype(str).apply(lambda x: x.str.match(pattern_email)).any(axis=1)
#     filtered_email_df = df[no_email_mask]

#     # 생일패턴
#     pattern_birth = r"^\d{8}$"
#     no_birth_mask = ~df[['Birthday']].astype(str).apply(lambda x: x.str.match(pattern_birth)).any(axis=1)
#     filtered_birth_df = df[no_birth_mask]

#     # 날짜패턴
#     pattern_date = r'^\d{4}-\d{2}-\d{2}$'
#     no_date_mask = ~df[['JoinDate']].astype(str).apply(lambda x: x.str.match(pattern_date)).any(axis=1)
#     filtered_date_df = df[no_date_mask]

#     # 성별
#     no_gender_mask = ~df['Gender'].isin(['남', '여'])
#     filtered_gender_df = df[no_gender_mask]

#     # 부서장
#     manager_mask = ~(df['Manager'].isin(['부서장']) | df['Manager'].isnull())
#     filtered_manager_df = df[manager_mask]

#     # Y,N만
#     df['headnurse'] = df['headnurse'].str.upper()
#     no_headnurse_mask = ~df['headnurse'].isin(['Y', 'N'])
#     filtered_headnurse_df = df[no_headnurse_mask]

#     #숫자만
#     pattern_num = r"^\d+$"
#     no_num_mask = ~df[['career']].astype(str).apply(lambda x: x.str.match(pattern_num)).any(axis=1)
#     filtered_num_df = df[no_num_mask]

#     # depth1 체크
#     depth1_data = msdb_manager.fetch_all(Setting.select_division_depth1(), params=OfficeCode, timeout=_VERIFY_TIMEOUT_SEC)
#     depth1_chdata = [item['depth1'] for item in depth1_data]
#     no_depth1_mask = ~df['Depth1'].isin(depth1_chdata)
#     filtered_depth1_df = df[no_depth1_mask]

#     # depth2 체크
#     depth2_data = msdb_manager.fetch_all(Setting.select_division_depth2(), params=OfficeCode, timeout=_VERIFY_TIMEOUT_SEC)
#     depth2_chdata = [item['depth2'] for item in depth2_data]
#     no_depth2_mask = ~df['Depth2'].isin(depth2_chdata)
#     filtered_depth2_df = df[no_depth2_mask]
#     filtered_depth2_df = filtered_depth2_df[~filtered_depth2_df['Depth2'].isna()]

#     # depth3 체크
#     depth3_data = msdb_manager.fetch_all(Setting.select_division_depth3(), params=OfficeCode, timeout=_VERIFY_TIMEOUT_SEC)
#     depth3_chdata = [item['depth3'] for item in depth3_data]
#     no_depth3_mask = ~df['Depth3'].isin(depth3_chdata)
#     filtered_depth3_df = df[no_depth3_mask]
#     filtered_depth3_df = filtered_depth3_df[~filtered_depth3_df['Depth3'].isna()]

#     # 아이디 중복체크
#     failed_id_list = []
#     for row in df.itertuples():
#         id_check = msdb_manager.fetch_one(Setting.member_id_check(), params=row.MemberID)

#         if id_check:
#             failed_id_list.append(row)
#     failed_id_df = pd.DataFrame(failed_id_list)

#     if not failed_id_df.empty:
#         failed_id_df['Birthday'] = failed_id_df['Birthday'].astype(int)
#         failed_id_df['career'] = failed_id_df['career'].astype(int)
#         failed_id_df.drop('Index', axis=1, inplace=True)
#     #위반사항 내용 전체 체크하기 위해서 병합 및 중복값 정리
#     failed_combined_df = pd.concat(
#         [duplicates, filtered_df, filtered_phone_df, filtered_email_df, filtered_birth_df, filtered_date_df,
#          filtered_gender_df, filtered_manager_df, filtered_headnurse_df, filtered_num_df, filtered_depth1_df,
#          filtered_depth2_df, filtered_depth3_df, failed_id_df], ignore_index=True)
#     failed_combined_df = failed_combined_df.replace({np.nan: ''})
#     failed_combined_df.drop_duplicates(inplace=True)

#     # Bulk insert를 위해서 데이터 변환
#     df = utils.clean_non_printable_chars(df)
#     df = df.replace({np.nan: ''})
#     df['OfficeCode'] = OfficeCode
#     df['EmpSeqNo'] = EmpSeqNo
#     df['RegDate'] = RegDate

#     df = df[['num','OfficeCode', 'EmpSeqNo', 'EmpNum', 'MemberID', 'EmployeeName', 'Gender', 'Birthday', 'JoinDate',
#        'Tel', 'PortableTel', 'Ck_Email', 'address', 'Manager', 'Depth1',
#        'Depth2', 'Depth3', 'position', 'RegDate', 'career', 'duty', 'headnurse',
#        'nightkeep']]

#     data_to_insert = [tuple(row) for row in df.itertuples(index=False)]
#     print('failed_combined_df : ')
#     import pprint
#     pprint.pprint(failed_combined_df)
#     params = (OfficeCode, EmpSeqNo)

#     if not failed_combined_df.empty:
#         json_string = failed_combined_df.to_json(orient='records', force_ascii=False)
#     else:
#         # 기존에 Temp에 들어간 데이터 삭제
#         delete_rows = msdb_manager.execute(Setting.delete_member(), params=params)
#         if delete_rows is not None:
#             rows_affected = msdb_manager.bulk_execute(Setting.insert_member(), data_to_insert)

#             if rows_affected is not None:
#                 # 토큰 발행
#                 _clientId = os.getenv("CLIENT_ID")
#                 _clientSecret = os.getenv("CLIENT_SECRET")
#                 token = create_access_token(data={"clientSecret": _clientSecret, "clientId": _clientId})

#                 # API 호출 (엠웍스)
#                 #url = "http://localwgw.meditong.com/bizadmin/setting/member_excel_ai_ok.asp"
#                 url = "http://gw.meditong.com/bizadmin/setting/member_excel_ai_ok.asp"
#                 payload = "officeCode=" + OfficeCode + "&EmpSeqNo=" + EmpSeqNo + "&Token=" + token

#                 headers = {'Content-Type': 'application/x-www-form-urlencoded'}
#                 response = requests.post(url, data=payload, headers=headers)

#                 #결과 내용 확인
#                 print("test : ", response.text)
#                 if response.status_code == 200:
#                     json_string = '{"result": "succeed"}'
#                 else:
#                     json_string = '{"result": tmp inserted}'
#             else:
#                 json_string = '{"result": insert fail}'
#         else:
#             json_string = '{"result": delete fail}'

#     print(json_string)
#     return json_string

@router.get("/member_download/{filename}", summary="회원 엑셀양식을 다운로드 합니다.")
async def download_file(filename: str):
    file_path = os.path.join(DOWNLOAD_FOLDER, filename)
    if os.path.exists(file_path):
        return FileResponse(
            path=file_path,
            media_type="application/octet-stream", # A generic binary file type
            filename=filename  # The name the file will be saved as
        )
    return HTTPException(status_code=404, detail=f"File not found")






