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


        _queryString = _queryString + " and OfficeCode = %s and EmpAuthGbn in ('MEM', 'ADM') and isnull(LinkAlim,'Y') <> 'N' "

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
               inner join bizwiz20db.member b on a.OfficeCode = b.OfficeCode and a.mb_part = b.mb_part and b.EmpAuthGbn in ('MEM','ADM')
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
        _queryString = """
        WITH Base AS (
            Select
                   a.Idx, a.pushcode, a.pushsubcode, a.officecode,
                   a.EmpSeqNo as senderEmpSeqNo, c.EmployeeName as sendername,
                   a.Message, Convert(VarChar(10), b.RegDate, 120) as regdate,
                   b.ReadYN, b.Fk_Idx,
                   ISNULL(a.LinkUrl, '') as LinkUrl,
                   ISNULL(a.LinkCode, '') as LinkCode
              From bizwiz20db.TB_Mobile_Push_History_Master a WITH(NOLOCK)
             Inner Join bizwiz20db.TB_Mobile_Push_History_User b WITH(NOLOCK) On a.officeCode = b.officeCode and a.Idx=b.Fk_Idx
             Inner Join bizwiz20db.Member c WITH(NOLOCK) On a.officeCode = c.officeCode and a.EmpSeqNo=c.EmpSeqNo
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
               Idx, pushcode, pushsubcode, officecode, senderEmpSeqNo, sendername,
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