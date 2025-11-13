
class Member:

    def login_check():
        _queryString = """
         select pwdcompare(%s,a.MemberPassEncrypt) as IsPWCorrect, a.EmpSeqNo, a.OfficeCode, a.EmpAuthGbn, isnull(mo.ade_sch,'N') as aiuseyn
          from bizwiz20db.Member_Login a inner join bizwiz20db.M_Office mo on a.OfficeCode = mo.OfficeCode 
         where a.MemberID = %s and a.EmpAuthGbn in ('ADM', 'MEM')
        """
        return _queryString

    def login_log():
        _queryString = """
        INSERT INTO bizwiz20db.Member_LoginLog
        (LogID, LogDate, LogIP, LogView, EmpSeqNo, OfficeCode, LogType)
        VALUES
        (%s, %s, %s, '', %s, %s, %s)
        """
        return _queryString

    def login_update():
        _queryString = """
        Update bizwiz20db.Member_Login Set LastLogin = getdate() Where EmpSeqNo = %s
        """
        return _queryString

    def member_view() :
        _queryString = """
        Select A.OfficeCode as office_id
                , C.OfficeName as office_name
                , B.MemberID as account_id
                , A.EmployeeName as name
                , a.gender
                , CONVERT(VARCHAR(10), a.DateOfBirth, 23)as DateOfBirth
                , a.JoinDate, a.Tel, a.PortableTel
                , isnull(A.EmpAuthGbn,'') as EmpAuthGbn
                , CASE WHEN ISNULL(a.EmpAuthGbn, '') = 'ADM' THEN CAST(1 AS BIT) ELSE CAST(0 AS BIT) END AS is_master_admin
                , isnull(A.Email,'') as Email
                , isnull(A.mb_part,'') as mb_part
                ,  a.zipcode, a.Address1, a.Address2
                , isnull(mb_part_managerYN,'') as mb_part_managerYN
                , isnull(D.name,'') As mb_partName
                , isnull(E.name,'') As OfficialTitleName
                , A.EmpSeqNo as nurse_id
                , A.EmpSeqNo
                , '' as group_id
                , isnull(a.career,'') as career
                , isnull(a.duty,'') as duty
                , isnull(a.headnurse, 0) as is_head_nurse
                , isnull(a.nightkeep,'') as nightkeep
                , isnull(F.no,'') as manageno
            From bizwiz20db.Member A
            Inner Join bizwiz20db.Member_Login B On A.OfficeCode=B.OfficeCode And A.EmpSeqNo=B.EmpSeqNo
            Inner Join bizwiz20db.M_Office C On A.OfficeCode=C.OfficeCode
            Left Join bizwiz20db.T_Team D On A.mb_part=D.mb_part And A.OfficeCode=D.OfficeCode
            Left Join bizwiz20db.T_Part E On A.OfficialTitleCode=E.code And A.OfficeCode=E.OfficeCode
            left join bizwiz20db.Manage_Office F on A.OfficeCode = F.OfficeCode 
            Where B.MemberID = %s AND a.EmpAuthGbn !='DEL' and C.ade_sch = 'Y'
        
        """
        return _queryString

    def login_check_token():
        _queryString = """
        select a.EmpSeqNo, a.OfficeCode, a.EmpAuthGbn, isnull(mo.ade_sch,'N') as aiuseyn
          from bizwiz20db.Member_Login a inner join bizwiz20db.M_Office mo on a.OfficeCode = mo.OfficeCode 
         where a.MemberID = %s and a.EmpAuthGbn in ('ADM', 'MEM')
        """
        return _queryString

    def member_update():
        _queryString = """
        update bizwiz20db.Member set gender = %s, DateOfBirth = CONVERT(datetime, %s, 120), JoinDate = %s, Tel = %s, PortableTel =%s, Email = %s, zipcode = %s, Address1 = %s, Address2 = %s
        where empseqno = %s
        """
        return _queryString

    def find_id(auth_method: str):
        _queryString = """
        select b.memberid from bizwiz20db.Member a inner join bizwiz20db.Member_Login b on a.empseqno = b.empseqno
        """
        if auth_method == "bio":
            _queryString = _queryString + " where a.EmployeeName  = %s and CONVERT(VARCHAR(10), a.DateOfBirth, 23) = %s and a.Gender = %s "
        elif auth_method == "phone":
            _queryString = _queryString + " where a.EmployeeName  = %s and a.PortableTel = %s  "
        elif auth_method == "email":
            _queryString = _queryString + " where a.EmployeeName  = %s and a.Email = %s  "

        return _queryString

    def member_pwd_update():
        _queryString = """
        Update bizwiz20db.Member_Login Set MemberPass='', MemberpassEncrypt = pwdencrypt(%s), LinkLoginCode = dbo.ftnEncrypt(%s) where officecode = %s and empseqno = %s
        """
        return _queryString

    def qpis_member_update(chg_pwd_YN: str):
        _queryString = " Exec eun_qpis.dbo.Eun_SP_OpenKeys; Update eun_qpis.dbo.TB_Member Set "

        if chg_pwd_YN == 'Y':
            _queryString = _queryString + " MemPW = pwdencrypt(%s), "

        _queryString = _queryString + """
            MemHP = eun_qpis.dbo.fn_Encrypt(%s),
            Tel = %s, 
            ZipCode = %s, 
            Address1 = %s, 
            Address2 = %s, 
            MemName = %s, 
            MemBirth = eun_qpis.dbo.fn_Encrypt(%s),
            MemEmail = eun_qpis.dbo.fn_Encrypt(%s) 
            Where MemID = %s; Exec eun_qpis.dbo.Eun_SP_CloseKeys; 
        """
        return _queryString

    def find_pw_chk():
        _queryString = """
        select m.PortableTel, m.Email, m.EmpSeqNo, isnull(mpr.idx,0) as idx
          from bizwiz20db.[Member] m inner join bizwiz20db.Member_Login ml on m.EmpSeqNo = ml.EmpSeqNo 
               left join bizwiz20db.Member_PW_Reset mpr on m.EmpSeqNo = mpr.EmpSeqNo 
         where ml.MemberID = %s
           and m.EmployeeName  = %s
        """

        return _queryString

    def member_pwd_reset():
        _queryString = """
        Update bizwiz20db.Member_Login Set MemberPass='', MemberpassEncrypt = pwdencrypt(%s), LinkLoginCode = dbo.ftnEncrypt(%s) where empseqno = %s
        """

        return _queryString

    def member_pwd_reset_history(user_pw_reset: int):
        if user_pw_reset == 0:
            _queryString = """
            insert into bizwiz20db.Member_PW_Reset (empseqno, search_method) values (%s, 1)
            """
        else:
            _queryString = """
            update bizwiz20db.Member_PW_Reset
               set search_method = '1', reg_date = getdate(), use_state = 0
             where empseqno = %s
            """

        return _queryString

    def member_accounts_by_office():
        _queryString = """
        SELECT 
            i.MemberID AS account_id,
            m.EmployeeName AS name,
            m.EmpAuthGbn AS EmpAuthGbn,
            m.EmpSeqNo AS nurse_id
        FROM bizwiz20db.member_login AS i
        LEFT JOIN bizwiz20db.member AS m 
            ON i.EmpSeqNo = m.EmpSeqNo 
        WHERE m.OfficeCode = %s
          AND m.EmpAuthGbn IN ('ADM','MEM')
        """
        return _queryString

    def member_export_by_office():
        _queryString = """
        select a.EmpSeqNo, c.big_kind
             , isnull((select name from bizwiz20db.T_Team where OfficeCode = c.OfficeCode and big_kind = c.big_kind and depth = '1'), '') as big_kind_name
             , c.middle_kind
             , isnull((select name from bizwiz20db.T_Team where OfficeCode = c.OfficeCode and big_kind = c.big_kind and middle_kind = c.middle_kind and depth = '2'), '') as middle_kind_name
             , c.small_kind
             , isnull((select name from bizwiz20db.T_Team where OfficeCode = c.OfficeCode and big_kind = c.big_kind and middle_kind = c.middle_kind and small_kind = c.small_kind and depth = '3'), '') as small_kind_name
             , c.mb_part, c.name as mb_part_name, a.OfficeEmpNum, a.EmployeeName, b.MemberID, a.duty, a.career, a.headnurse
          from bizwiz20db.Member a
               Inner Join bizwiz20db.Member_Login b On a.OfficeCode=b.OfficeCode And a.EmpSeqNo=b.EmpSeqNo
               Left Join bizwiz20db.T_Team c On a.mb_part=c.mb_part And a.OfficeCode=c.OfficeCode
         where b.officecode = %s and a.EmpAuthGbn in ('ADM','MEM')
        """
        return _queryString
