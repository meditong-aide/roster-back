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
    def set_push_receiver_member_id():
        _queryString = "Select MemberID From eun_gw.bizwiz20db.Member_Login Where EmpSeqNo= %s "
        return _queryString

    @staticmethod
    def set_push_receiver_pushyn():
        _queryString = " Select Top 1 PushYN,pushTimeYn,stime,etime From eun_gw.bizwiz20db.TB_Mobile_User_Setting_List Where MemberID = %s Order By RegDate Desc "
        return _queryString

    @staticmethod
    def get_user_device_key():
        _queryString = " Select top 1 DeviceKey From eun_gw.bizwiz20db.TB_Mobile_User_Device_List Where MemberID = %s Order By Idx Desc"
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
    def get_organization_member():
        _queryString = """
        select a.num, a.name, a.big_kind, a.middle_kind, a.small_kind, a.mb_part, a.[depth], a.sort, a.ref_num, b.EmpSeqNo, c.MemberID, b.EmployeeName, d.name as part_name, e.name as position_name, e.name as rank_name
          from bizwiz20db.T_Team a
               inner join bizwiz20db.member b on a.OfficeCode = b.OfficeCode and a.mb_part = b.mb_part and b.EmpAuthGbn in ('MEM','ADM')
               left join bizwiz20db.member_login c on b.OfficeCode = c.OfficeCode and b.EmpSeqNo = c.EmpSeqNo 
               left join bizwiz20db.T_Part d on a.OfficeCode = d.OfficeCode and b.OfficialTitleCode  = d.code 
               left join bizwiz20db.T_Position e on a.OfficeCode = e.OfficeCode and b.OfficialPositionCode   = e.code
               left join bizwiz20db.T_rank f on a.OfficeCode = f.OfficeCode and b.OfficialRankCode    = f.code
         where a.OfficeCode  = %s and a.t_use = 'Y'
         order by a.sort 
        """
        return _queryString
