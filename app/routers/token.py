import os
from datetime import date
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, status, Response, Request, Form, Depends
from sqlalchemy.orm import Session
from fastapi.responses import RedirectResponse, JSONResponse
from starlette.status import HTTP_301_MOVED_PERMANENTLY
from datalayer.member import Member
from datalayer.token import Token
# from db.client import get_db
from db.client2 import get_db, msdb_manager
from db.models import Nurse, Group
from schemas.auth_schema import User as UserSchema
from utils.security import create_access_token
from utils.security import create_login_token

ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 # 1 day

# ── 시연 계정 진입 ──────────────────────────────────────────────
# 메디통 내부(엔투에이아이) 직원이 SSO 로 들어올 때, 자기 계정 대신 **데모병원(102560)
# 계정**으로 진입시켜 역할별 화면을 보여주기 위한 설정.
#
# ★ 역할 → 계정 매핑은 **백엔드에만** 둔다. 프론트에는 role 키(`admin`/`hn`/`nurse`)와
#   라벨만 내려보내고 계정 ID 는 노출하지 않는다. 계정이 바뀌어도 여기만 고치면 된다.
# ★ 대상 판정은 **요청마다** 한다. 모드 조회(`/sso-mode`)에서 통과했다고 로그인
#   (`/login`)에서 믿으면 안 된다 — 두 요청은 독립이고 폼 값은 조작 가능하다.
_DEMO_OFFICE_CODE = "100723"
_DEMO_ROLE_ACCOUNTS = {
    "admin": "yadmin",    # 통합관리자   — EmpAuthGbn='ADM' (nurses 미등록이지만 ADM 은 게이트 면제)
    "hn": "yss0401",      # 근무표관리자 — nurses.is_head_nurse=1 · hn_auth='HN'
    "nurse": "yss0414",   # 근무자       — nurses 일반
}
_DEMO_ROLE_LABELS = {
    "admin": "통합관리자",
    "hn": "근무표관리자",
    "nurse": "근무자",
}

# Configuration
router = APIRouter(
    prefix="/token",
    tags=["token"]
)


def _verify_sso_token(token: str) -> bool:
    """메디통이 발급한 그날의 SSO 토큰인지 대조한다.

    ★ 개인별 토큰이 아니라 (clientId, clientSecret, 날짜) 로 만들어지는 **공통 토큰**이다.
      따라서 이 검증만으로는 "누구인지" 가 증명되지 않는다 — MemberID 는 별도로
      대상 여부를 따져야 한다(`_is_demo_office_member`).
    """
    clientId = os.getenv("CLIENT_ID")
    clientSecret = os.getenv("CLIENT_SECRET")
    current_date = date.today().strftime('%Y-%m-%d')
    rows = msdb_manager.fetch_all(Token.Get_Token(), params=(clientId, clientSecret, current_date))
    if not rows:
        return False
    return rows[0]['token'] == token


def _is_demo_office_member(member_id: str) -> bool:
    """시연 계정 진입 대상인지 — 내부 office 소속의 **재직**(ADM·MEM) 계정만.

    NMM(탈퇴)·DEL(삭제)은 쿼리의 화이트리스트에서 자연히 빠진다. 로그인 자체는
    NMM 도 통과하지만(`login_check_token`) 시연 진입은 재직자에게만 연다.
    """
    if not member_id:
        return False
    try:
        rows = msdb_manager.fetch_all(
            Member.office_member_check(), params=(member_id, _DEMO_OFFICE_CODE)
        )
        return bool(rows)
    except Exception as exc:  # noqa: BLE001 — 판정 실패는 "대상 아님" 으로 떨어뜨린다
        print(f"[SsoMode] 시연 대상 판정 실패 — member_id={member_id!r}: {exc}")
        return False

def get_extra_data_from_nurses(db: Session, account_id: str) -> dict:
    """간단 조회: nurses 테이블에서 토큰에 보강할 필드를 가져온다.

    인자
    - account_id: 조회할 계정 ID

    반환
    - {'office_id', 'account_id', 'is_head_nurse', 'group_id'} 중 존재하는 값만 담은 dict
    """
    try:
        nurse = db.query(Nurse).filter(Nurse.account_id == account_id).first()
        if not nurse:
            return {}
        result = {
            "office_id": getattr(nurse, "office_id", None),
            "account_id": getattr(nurse, "account_id", None),
            "is_head_nurse": bool(getattr(nurse, "is_head_nurse", False)),
            "group_id": getattr(nurse, "group_id", None),
        }
        # group_id 기준으로 groups 테이블에서 group_name 조회
        if result["group_id"]:
            group = db.query(Group).filter(Group.group_id == result["group_id"]).first()
            if group:
                result["group_name"] = group.group_name
        return result
    except Exception:
        return {}

@router.post("/", summary="Token 생성")
async def get_token(response: Response,
                    clientId: str = Form(...),
                    clientSecret: str = Form(...)):
    """
        토큰은 1일 단위로 생성되며, 중복 호출 시 DB에 저장된 값으로 반환함.
    """
    _clientId = os.getenv("CLIENT_ID")
    _clientSecret = os.getenv("CLIENT_SECRET")

    if clientId == _clientId and clientSecret == _clientSecret :
        token = create_access_token(data={"clientSecret": clientSecret, "clientId": clientId})

    else :
        raise HTTPException(status_code=401, detail=f"Invalid client ID or secret provided.")
    return {"token" : token}

@router.post("/sso-mode", summary="SSO 진입 모드 — 시연 역할 선택이 필요한 계정인지")
async def get_sso_mode(token: str = Form(...), MemberID: str = Form(...)):
    """SSO 진입 직후 프론트가 역할 선택 모달을 띄울지 판단하기 위한 조회.

    반환
      - `{"mode": "normal"}`  기존 흐름 그대로 진행한다.
      - `{"mode": "demo", "roles": [{"role","label"}, ...]}`  선택 모달을 띄운다.

    ★ SSO 토큰을 먼저 검증한다. 토큰 없이 MemberID 만으로 "내부 소속인지" 를 알 수 있으면
      계정 열거 수단이 된다.
    ★ 여기서 `demo` 가 나왔다고 로그인이 되는 건 아니다 — `/login` 이 같은 판정을 다시 한다.
    """
    if not _verify_sso_token(token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        )

    if not _is_demo_office_member(MemberID):
        return {"mode": "normal"}

    return {
        "mode": "demo",
        "roles": [
            {"role": key, "label": _DEMO_ROLE_LABELS[key]}
            for key in _DEMO_ROLE_ACCOUNTS
        ],
    }


@router.post("/login", summary="Token, 회원아이디로 sso")
async def login_for_access_token(response: Response,
                                 request: Request,
                                 token: str = Form(...),
                                 MemberID: str = Form(...),
                                 demo_role: str | None = Form(None),
                                 db: Session = Depends(get_db)):
    """
        redirectUrl이 있는 경우 처리하고 값이 없는 경우 결과값과 아이디 반환
    """

    clientId = os.getenv("CLIENT_ID")
    clientSecret = os.getenv("CLIENT_SECRET")
    today = date.today()
    current_date = today.strftime('%Y-%m-%d')

    rows = msdb_manager.fetch_all(Token.Get_Token(), params=(clientId, clientSecret, current_date))
    _token = rows[0]['token']

    if _token == token :
        client_ip = request.client.host
        try:
            # ── 시연 계정 진입 ─────────────────────────────────────
            # 내부(100723) 직원이 역할을 골라 들어온 경우, 그 역할에 해당하는
            # **데모병원 계정으로 갈아끼운다**. 비밀번호는 쓰지 않는다 — 이 경로는
            # 애초에 SSO 토큰만 보고 MemberID 로 사람을 정하기 때문이다.
            #
            # ★ 대상 판정을 여기서 **다시** 한다. `/sso-mode` 통과는 근거가 되지 않는다.
            # ★ 스왑하면 이후 `mworks_access_token` 이 남기는 그룹웨어 로그인 로그가
            #   데모 계정으로 찍혀 **원래 누가 들어왔는지 사라진다**. 스왑 직전에
            #   원본을 남겨야 최소한의 추적이 된다.
            #
            # ★★ 알려진 제약 — 이 판정은 "그 MemberID 가 100723 소속" 임을 증명할 뿐
            #   "호출자가 그 사람" 임을 증명하지 못한다. SSO 토큰이 개인별이 아니라
            #   (clientId, clientSecret, 날짜) 기반 **공통 토큰**이기 때문이다.
            #   다만 이는 이 경로의 **기존 성질**이다 — `demo_role` 없이도 `MemberID`
            #   에 데모 계정을 그대로 넣으면 같은 세션이 나온다(ADM 은 nurses 게이트도
            #   면제). 오히려 이 분기는 "100723 계정 ID 를 알아야 한다" 는 조건이 하나
            #   더 붙는다. 노출 범위도 데모병원(102560) 데이터로 한정된다 — 스코프가
            #   토큰의 office_id 를 따르기 때문이다.
            #   근본 해결은 **개인별 SSO 토큰**이고 발급 주체가 메디통 ASP
            #   (`airoster_token_api.asp`)라 roster 밖이다. 별도 과제로 남긴다.
            if demo_role:
                if not _is_demo_office_member(MemberID):
                    print(
                        f"[TokenLogin][demo][denied] 시연 대상 아님 — "
                        f"member_id={MemberID!r} role={demo_role!r} ip={client_ip}"
                    )
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="시연 계정 진입 대상이 아닙니다.",
                    )
                _target = _DEMO_ROLE_ACCOUNTS.get(demo_role)
                if _target is None:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="알 수 없는 시연 역할입니다.",
                    )
                print(
                    f"[TokenLogin][demo] {MemberID} → {_target} "
                    f"(role={demo_role}) ip={client_ip}"
                )
                MemberID = _target

            # user = get_user(db, form_data.username)
            users = mworks_access_token(MemberID, client_ip)


            if not users:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Incorrect account ID",
                    headers={"WWW-Authenticate": "Bearer"},
                )

            for row in users:
                office_id = row['office_id']
                EmpSeqNo = row['EmpSeqNo']
                nurse_id = row['nurse_id']
                account_id = row['account_id']
                EmpAuthGbn = row['EmpAuthGbn']
                name = row['name']
                # nurse_id = row['nurse_id']
                group_id = row['group_id']
                is_head_nurse = row['is_head_nurse']
                mb_part = row['mb_part']
                office_name = row['office_name']
                mb_part_name = row['mb_partName']
                gw_useYN = row['gw_useYN']
                qpis_useYN = row['qpis_useYN']
                official_title_name = row['OfficialTitleName']  # 추가 필드 추출
            # ADM 여부는 EmpAuthGbn으로 판정
            is_master_admin = True if str(EmpAuthGbn).upper() == 'ADM' else False

            # ── 본사(엔투에이아이) 직원은 관리자 권한으로 둔다 ────────────────
            # ★ 왜 필요한가 — 아래 게이트는 `nurses` 미등록자를 501 로 막는데,
            #   ADM 만 면제된다. 본사 재직 39명 중 roster 에 등록된 건 24명뿐이라
            #   **15명이 자기 계정으로는 들어오지 못한다**(2026-08-27 실측).
            #   본사 직원은 운영·지원 목적으로 접근하므로 관리자 권한이 맞다.
            #
            # ★ `demo_role` 이 있을 때는 **적용하지 않는다.** 그 경우 MemberID 가 이미
            #   데모 계정(102560)으로 갈아끼워져 있어 판정도 False 지만, 의도를 명시해 둔다 —
            #   시연 진입의 권한은 고른 역할(통합관리자/근무표관리자/근무자)이 정한다.
            #
            # ★ 노출 범위는 본사 office 안으로 한정된다. 모든 스코프가 토큰의
            #   `office_id` 를 따르므로 다른 병원 데이터에는 닿지 않는다.
            #
            # ★★ 알려진 제약 — 이 경로의 `MemberID` 는 호출자 본인임이 증명되지 않는다.
            #   SSO 토큰이 개인별이 아니라 (clientId, clientSecret, 날짜) 기반 **공통
            #   토큰**이기 때문이다. 따라서 본사 MemberID 를 아는 사람은 이 승격을 얻을 수
            #   있다. 감수하고 두는 근거:
            #     · 뿌리는 이 경로의 **기존 성질**이고 근본 해결은 개인별 토큰인데,
            #       발급 주체가 메디통 ASP(`airoster_token_api.asp`)라 roster 밖이다.
            #     · 사칭으로 관리자 세션을 얻는 것 자체는 이미 가능하다 —
            #       `MemberID=yadmin`(102560 ADM)을 그대로 넣으면 된다.
            #     · 열리는 범위가 본사 office 뿐이고, 그 안은 1~4병동·test44444 로 이뤄진
            #       **내부 테스트 데이터**다(실제 환자 정보 없음).
            #   ★ 병원 office 에는 이 규칙을 확장하지 말 것. 그 순간 성질이 달라진다.
            if not demo_role and _is_demo_office_member(MemberID):
                is_master_admin = True

            access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)

            # ── AI근무표 대상자 게이트 ────────────────────────────────
            # SSO 진입 조건인 `aiuseyn`(=M_Office.ade_sch)은 **병원 단위** 플래그라
            # AI근무표를 쓰는 병원의 의사·행정직까지 전부 통과한다. `/auth/login` 은
            # nurses 미등록을 501 로 막는데(auth.py) 여기엔 같은 검사가 없어
            # 비대상 직원이 "로그인은 됐는데 전부 빈 화면" 상태로 들어왔다.
            #
            # ★ 쿠키를 **주지 않고 기존 쿠키도 지운다.** 모바일 `tokenlogin()` 이
            #   상태코드를 보지 않고 `response.json()` 을 그대로 돌려주기 때문에
            #   501 만으로는 호출부가 성공으로 오인한다. 로그인 상태 자체를 없애야
            #   화면이 비로그인 흐름으로 떨어진다.
            # ★ ADM(마스터 관리자)은 nurses 에 없어도 통과 — auth.py 와 같은 규약.
            if not is_master_admin:
                _nurse_row = db.query(Nurse).filter(Nurse.account_id == account_id).first()
                if _nurse_row is None:
                    print(
                        f"[TokenLogin][blocked] AI근무표 미대상 — account_id={account_id} "
                        f"office_id={office_id} office_name={office_name!r} "
                        f"EmpSeqNo={EmpSeqNo} EmpAuthGbn={EmpAuthGbn} "
                        f"name={name!r} ip={client_ip}"
                    )
                    # ★ HTTPException 을 던지면 FastAPI 가 **별도 에러 응답을 새로 만들어**
                    #   주입된 `response` 의 delete_cookie 헤더가 클라이언트에 닿지 않는다.
                    #   그러면 이전 계정으로 로그인해 둔 단말은 쿠키가 살아남아 차단이 무의미해진다.
                    #   삭제 헤더를 실어 보내려면 응답 객체를 직접 반환해야 한다.
                    _blocked = JSONResponse(
                        status_code=501,
                        content={
                            "detail": "AI근무표 이용 대상이 아닙니다. 병동 수간호사에게 등록을 요청하세요."
                        },
                    )
                    # set_cookie 와 같은 path 로 지워야 실제로 제거된다(기본 "/").
                    _blocked.delete_cookie(key="access_token", path="/")
                    return _blocked

            # nurses 테이블의 값으로 보강/덮어쓰기
            extra_data = get_extra_data_from_nurses(db, account_id)
            office_id = extra_data.get("office_id") or office_id
            group_id = extra_data.get("group_id") or group_id
            mb_part_name = extra_data.get("group_name") or mb_part_name
            if "is_head_nurse" in extra_data:
                is_head_nurse = extra_data["is_head_nurse"]

            access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
            access_token = create_login_token(
                data={
                    "office_id": office_id,
                    "EmpSeqNo": EmpSeqNo,
                    "account_id": account_id,
                    "EmpAuthGbn": EmpAuthGbn,
                    "is_master_admin": is_master_admin,
                    "nurse_id": nurse_id,
                    "group_id": group_id,
                    "is_head_nurse": is_head_nurse,
                    "name": name,
                    "mb_part": mb_part,
                    "office_name": office_name,
                    "mb_part_name": mb_part_name,
                    "gw_useYN": gw_useYN,
                    "qpis_useYN": qpis_useYN,
                    "official_title_name": official_title_name,  # 추가 필드
                    "original_group_id": group_id,  # 로그인 시 DB 기준 원래 소속 그룹
                },
                expires_delta=access_token_expires,
            )

            response.set_cookie(
                key="access_token",
                value=f"Bearer {access_token}",
                httponly=True,
                samesite="lax"
            )

            return UserSchema(
                nurse_id=nurse_id,
                account_id=account_id,
                office_id=office_id,  # This should now work with eager loading
                group_id=group_id,
                is_head_nurse=is_head_nurse,
                is_master_admin=(
                    bool(is_master_admin) if is_master_admin is not None else (str(EmpAuthGbn).upper() == 'ADM')),
                name=name,
                EmpSeqNo=EmpSeqNo,
                EmpAuthGbn=EmpAuthGbn,
                mb_part=mb_part,
                office_name=office_name,
                mb_part_name=mb_part_name,
                gw_useYN=gw_useYN,
                qpis_useYN=qpis_useYN,
                official_title_name=official_title_name,  # 추가 필드
            )

            # return {"result": "succeed", "message": "Login successful", "account_id": MemberID}

        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Invalid login : {str(e)}")
    else :
        raise HTTPException(status_code=401, detail=f"Invalid login.")

def mworks_access_token (account_id: str, client_ip: str) :
    rows = msdb_manager.fetch_all(Member.login_check_token(), params=(account_id))
    for row in rows :
       EmpSeqNo = row['EmpSeqNo']
       OfficeCode = row['OfficeCode']
       EmpAuthGbn = row['EmpAuthGbn']
       aiuseyn = row['aiuseyn']
    LogType = 'W'
    RegDate = datetime.now()

    if aiuseyn != 'Y' :
        raise HTTPException(status_code=500, detail=f"AI근무표 서비스에 가입되지 않았습니다.")

    params = (account_id, RegDate, client_ip, EmpSeqNo, OfficeCode, LogType)

    new_id = msdb_manager.execute(Member.login_log(), params=params)

    if new_id is None:
        new_id = msdb_manager.execute(Member.login_update(), params=(EmpSeqNo))

    try :
        user_info = msdb_manager.fetch_all(Member.member_view(), params=(account_id))
        return user_info

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Login failed: {str(e)}")

