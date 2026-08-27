import os


def _roster_db() -> str:
    """환경별 roster DB명: 운영=eun_roster, 개발=eun_roster_dev.

    ★ 하드코딩하면 dev 백엔드가 **운영 데이터**를 본다.
      (sticker.py·message.py 는 `eun_roster.dbo.*` 로 박혀 있어 지금 그 상태다 — 별건)
    ★ 모듈 상수로 두지 않고 **호출 시점**에 읽는다. 상수로 두면 이 모듈이 `.env` 로딩보다
      먼저 import 될 때 기본값이 박혀 버리고, 그러면 dev 가 운영 nurses 를 조인한다.
    """
    return os.getenv("EUN_DB_NAME", "eun_roster")


class Common:

    @staticmethod
    def get_mlink_sender_chk():
        _queryString = """
        select memberID from bizwiz20db.Member_Login  WITH(NOLOCK) where OfficeCode = %s and EmpSeqNo = %s and EmpAppYN = 'Y' 
        """
        return _queryString

    @staticmethod
    def get_mlink_receiver_chk(send_all_yn: str):
        _queryString = " select memberID from bizwiz20db.Member_Login WITH(NOLOCK) where EmpAppYN = 'Y' "

        if send_all_yn == "Y":
            _queryString = _queryString + " and EmpSeqNo in (%s) "


        _queryString = _queryString + " and OfficeCode = %s and EmpAuthGbn in ('MEM', 'ADM', 'NMM') and isnull(LinkAlim,'Y') <> 'N' "

        return _queryString

    @staticmethod
    def set_push_master():
        _queryString = """
        Insert into eun_gw.bizwiz20db.TB_Mobile_Push_History_Master(Message, OfficeCode, EmpSeqNo, SendID, SendUserType, PushCode, PushSubCode, SendType, LinkUrl, RegDate, LinkCode)
        Values(%s, %s, %s, %s, 'U', %s, %s, 'F', %s, GetDate(), %s)
        """
        return _queryString

    @staticmethod
    def get_push_max_id():
        _queryString = """
        Select Top 1 Idx From eun_gw.bizwiz20db.TB_Mobile_Push_History_Master Order By Idx Desc
        """
        return _queryString

    @staticmethod
    def set_push_receiver():
        _queryString = """
        insert into eun_gw.bizwiz20db.TB_Mobile_Push_History_User(Fk_Idx, OfficeCode, EmpSeqNo) 
        Values(%s, %s, %s)
        """
        return _queryString

    @staticmethod
    def get_user_device_key(receiveEmpSeqNo: str):
        _queryString = f"""
        select distinct z.DeviceKey
          from (
                select a.MemberID, b.PushYN, b.pushTimeYn, c.DeviceKey
                  from bizwiz20db.Member_Login a left join eun_gw.bizwiz20db.TB_Mobile_User_Setting_List b on a.MemberID = b.MemberID
                       left join eun_gw.bizwiz20db.TB_Mobile_User_Device_List c on a.MemberID = c.MemberID
                where a.EmpSeqNo in ({receiveEmpSeqNo})
                  and b.PushYN = 'Y' and ((b.stime is null or b.stime = '') and (b.etime is null or b.etime = ''))
        
                Union all
        
                select a.MemberID, b.PushYN, b.pushTimeYn, c.DeviceKey
                  from bizwiz20db.Member_Login a left join eun_gw.bizwiz20db.TB_Mobile_User_Setting_List b on a.MemberID = b.MemberID
                       left join eun_gw.bizwiz20db.TB_Mobile_User_Device_List c on a.MemberID = c.MemberID
                where a.EmpSeqNo in ({receiveEmpSeqNo})
                  and b.PushYN = 'Y' and b.PushTimeYN = 'Y' and (b.stime is not null and b.stime <> '') and (b.etime is not null and b.etime <> '')
                  and CONVERT(time, b.stime + ':00') <= CONVERT(time, GETDATE()) 
                  and CONVERT(time, b.etime + ':00') >= CONVERT(time, GETDATE())
               ) z
        where z.DeviceKey is not null and z.DeviceKey <> ''
        """
        return _queryString

    @staticmethod
    def set_push_message():
        _queryString = """
        insert into bizwiz20db.TB_FCM(EmpSeqNo, OfficeCode, M_Title, M_Key, pushidx, PushCode, PushSubCode, m_status, regdate)
        Values(%s, %s, %s, %s, %s, %s, %s, %s, GetDate())
        """
        return _queryString

    @staticmethod
    def set_sms_message():
        _queryString = """
        insert into bizwiz20db.sc_tran(suniquetaskid,suniqueid,tr_senddate,tr_sendstat,tr_msgtype,tr_phone,tr_callback,tr_msg,OfficeCode,EmpSeqNo) 
        Values(%s, %s, GetDate(), '0', '0', %s, %s, %s, '000000', '000000')
        """
        return _queryString

    @staticmethod
    def get_organization_member(deptyn: str):
        _queryString = """
        select a.num, a.name, a.big_kind, a.middle_kind, a.small_kind, a.mb_part, a.[depth], a.sort, a.ref_num, b.EmpSeqNo, c.MemberID, b.EmployeeName, d.name as part_name, e.name as position_name, e.name as rank_name
          from bizwiz20db.T_Team a
               inner join bizwiz20db.member b on a.OfficeCode = b.OfficeCode and a.mb_part = b.mb_part and b.EmpAuthGbn in ('MEM','ADM','NMM')
               left join bizwiz20db.member_login c on b.OfficeCode = c.OfficeCode and b.EmpSeqNo = c.EmpSeqNo 
               left join bizwiz20db.T_Part d on a.OfficeCode = d.OfficeCode and b.OfficialTitleCode = d.code 
               left join bizwiz20db.T_Position e on a.OfficeCode = e.OfficeCode and b.OfficialPositionCode = e.code
               left join bizwiz20db.T_rank f on a.OfficeCode = f.OfficeCode and b.OfficialRankCode = f.code
         where a.OfficeCode  = %s and a.t_use = 'Y'
        """
        if deptyn == 'Y' :
            _queryString = _queryString + " and a.mb_part = %s "
            _queryString = _queryString + " order by d.name, b.EmployeeName "
        else:
            _queryString = _queryString + " order  by a.sort "

        return _queryString

    @staticmethod
    def get_push_cnt():
        # get_push_list()와 동일한 dedup 로직 적용 — 노출되는 알림 수와 카운트 일치
        _queryString = """
        WITH Base AS (
            Select a.Idx, a.PushCode,
                   ISNULL(a.LinkCode, '') as LinkCode,
                   a.pushsubcode, a.Message
              From bizwiz20db.TB_Mobile_Push_History_Master a WITH(NOLOCK)
             Inner Join bizwiz20db.TB_Mobile_Push_History_User b WITH(NOLOCK) On a.officeCode = b.officeCode and a.Idx=b.Fk_Idx
             Where b.OfficeCode = %s And b.EmpSeqNo = %s And a.PushCode = 'P30' And b.DelYN = 'N'
               And b.ReadYN = 'N'
               And Convert(VarChar(10), b.RegDate, 120) >= '2016-04-01'
        ),
        WithKey AS (
            Select *,
                   CASE
                       WHEN LinkCode <> '' THEN LinkCode
                       WHEN pushsubcode IN ('S01','S04') AND CHARINDEX(N'년', Message) > 0 AND CHARINDEX(N'월', Message) > 0
                       THEN CONCAT(
                           'ROSTER:',
                           LEFT(Message, CHARINDEX(N'년', Message) - 1),
                           ':',
                           RIGHT('0' + LTRIM(RTRIM(SUBSTRING(
                               Message,
                               CHARINDEX(N'년 ', Message) + 2,
                               CHARINDEX(N'월', Message) - CHARINDEX(N'년 ', Message) - 2
                           ))), 2)
                       )
                       WHEN pushsubcode = 'S02' AND CHARINDEX(N'년', Message) > 0 AND CHARINDEX(N'월', Message) > 0
                       THEN CONCAT(
                           'WANTED:',
                           LEFT(Message, CHARINDEX(N'년', Message) - 1),
                           ':',
                           RIGHT('0' + LTRIM(RTRIM(SUBSTRING(
                               Message,
                               CHARINDEX(N'년 ', Message) + 2,
                               CHARINDEX(N'월', Message) - CHARINDEX(N'년 ', Message) - 2
                           ))), 2)
                       )
                       ELSE CAST(Idx AS VARCHAR(20))
                   END AS DerivedLinkCode
              From Base
        ),
        Ranked AS (
            Select PushCode,
                   ROW_NUMBER() OVER (
                       PARTITION BY DerivedLinkCode
                       ORDER BY Idx DESC
                   ) AS rn
              From WithKey
        )
        Select PushCode, COUNT(*) As PushCnt
          From Ranked
         Where rn = 1
        Group By PushCode
        """
        return _queryString

    @staticmethod
    def get_push_list():
        # LinkCode 기준 최신 1건만 노출 (동일 year/month 재마감 시 중복 제거)
        # LinkCode가 빈값인 기존 데이터는 Message에서 year/month 파싱하여 파티션 키 생성
        # params 순서: (OfficeCode, EmpSeqNo, listsize)
        #
        # ★ senderduty(발신자 직함) — roster 의 `nurses.level_` 을 쓴다.
        #   그룹웨어 `Member.duty` 는 실측상 거의 비어 있어(재직 1,796명 중 NULL 1,792) 못 쓴다.
        #   그룹웨어 직위(`OfficialTitleCode`→`T_Part.name`)도 후보였으나, 직함을 우리가
        #   직접 고칠 수 있는 roster 컬럼으로 가기로 했다(사용자 결정).
        # ★ LEFT JOIN 이어야 한다 — roster 미등록 발신자도 알림 목록에서 빠지면 안 된다.
        #   실측상 발신자 중 nurses 에 없는 사람이 있다(근무표 알림을 보내는 사람이 반드시
        #   간호 인력으로 등록돼 있지는 않다). 그 경우 senderduty 는 null 이 된다.
        # ★ 값이 없으면 **빈 문자열이 아니라 null** 로 내려보낸다(프론트 계약).
        # ★ DB명은 `_roster_db()` 로 호출 시점에 주입한다(모듈 상수면 import 순서에 취약).
        # ★ `nurses.nurse_id` 는 중복이 없어(실측) 조인으로 행이 늘지 않는다.
        _queryString = f"""
        WITH Base AS (
            Select
                   a.Idx, a.pushcode, a.pushsubcode, a.officecode,
                   a.EmpSeqNo as senderEmpSeqNo, c.EmployeeName as sendername,
                   NULLIF(d.level_, '') as senderduty,
                   a.Message, Convert(VarChar(10), b.RegDate, 120) as regdate,
                   b.ReadYN, b.Fk_Idx,
                   ISNULL(a.LinkUrl, '') as LinkUrl,
                   ISNULL(a.LinkCode, '') as LinkCode
              From bizwiz20db.TB_Mobile_Push_History_Master a WITH(NOLOCK)
             Inner Join bizwiz20db.TB_Mobile_Push_History_User b WITH(NOLOCK) On a.officeCode = b.officeCode and a.Idx=b.Fk_Idx
             Inner Join bizwiz20db.Member c WITH(NOLOCK) On a.officeCode = c.officeCode and a.EmpSeqNo=c.EmpSeqNo
              Left Join {_roster_db()}.dbo.nurses d WITH(NOLOCK) On d.office_id = a.officecode and d.nurse_id = a.EmpSeqNo
             Where b.OfficeCode = %s And b.EmpSeqNo = %s And a.PushCode = 'P30' And b.DelYN = 'N'
               And Convert(VarChar(10), b.RegDate, 120) >= '2016-04-01'
        ),
        WithKey AS (
            Select *,
                   CASE
                       -- 신규 데이터: LinkCode 그대로 사용
                       WHEN LinkCode <> '' THEN LinkCode
                       -- 기존 데이터: Message에서 year/month 파싱하여 파티션 키 생성
                       WHEN pushsubcode IN ('S01','S04') AND CHARINDEX(N'년', Message) > 0 AND CHARINDEX(N'월', Message) > 0
                       THEN CONCAT(
                           'ROSTER:',
                           LEFT(Message, CHARINDEX(N'년', Message) - 1),
                           ':',
                           RIGHT('0' + LTRIM(RTRIM(SUBSTRING(
                               Message,
                               CHARINDEX(N'년 ', Message) + 2,
                               CHARINDEX(N'월', Message) - CHARINDEX(N'년 ', Message) - 2
                           ))), 2)
                       )
                       WHEN pushsubcode = 'S02' AND CHARINDEX(N'년', Message) > 0 AND CHARINDEX(N'월', Message) > 0
                       THEN CONCAT(
                           'WANTED:',
                           LEFT(Message, CHARINDEX(N'년', Message) - 1),
                           ':',
                           RIGHT('0' + LTRIM(RTRIM(SUBSTRING(
                               Message,
                               CHARINDEX(N'년 ', Message) + 2,
                               CHARINDEX(N'월', Message) - CHARINDEX(N'년 ', Message) - 2
                           ))), 2)
                       )
                       ELSE CAST(Idx AS VARCHAR(20))
                   END AS DerivedLinkCode
              From Base
        ),
        Ranked AS (
            Select *,
                   ROW_NUMBER() OVER (
                       PARTITION BY DerivedLinkCode
                       ORDER BY Idx DESC
                   ) AS rn
              From WithKey
        )
        Select Top %s
               Idx, pushcode, pushsubcode, officecode, senderEmpSeqNo, sendername, senderduty,
               Message, regdate, ReadYN, Fk_Idx, LinkUrl,
               DerivedLinkCode AS LinkCode
          From Ranked
         Where rn = 1
         Order By Idx desc
        """
        return _queryString

    @staticmethod
    def update_push_read_one():
        # 단건 읽음 처리: Fk_Idx(=TB_Mobile_Push_History_User.Fk_Idx) + EmpSeqNo 기준
        # params 순서: (Fk_Idx, EmpSeqNo, OfficeCode)
        _queryString = """
        Update bizwiz20db.TB_Mobile_Push_History_User
           Set ReadYN = 'Y'
         Where Fk_Idx = %s And EmpSeqNo = %s And OfficeCode = %s And ReadYN = 'N'
        """
        return _queryString

    @staticmethod
    def update_push_read_by_code():
        # 웹 프론트에서 pushcode + pushsubcode + officecode 기준 읽음 처리
        # params 순서: (EmpSeqNo, OfficeCode, PushCode, PushSubCode, OfficeCode)
        _queryString = """
        Update bizwiz20db.TB_Mobile_Push_History_User
           Set ReadYN = 'Y'
         Where EmpSeqNo = %s And OfficeCode = %s And ReadYN = 'N'
           And Fk_Idx In (
               Select Idx From bizwiz20db.TB_Mobile_Push_History_Master WITH(NOLOCK)
                Where PushCode = %s And PushSubCode = %s And OfficeCode = %s
           )
        """
        return _queryString

    @staticmethod
    def update_push_read_all():
        _queryString = """
        Update bizwiz20db.TB_Mobile_Push_History_User
           Set ReadYN = 'Y'
         Where EmpSeqNo = %s And OfficeCode = %s And ReadYN = 'N'
        """
        return _queryString