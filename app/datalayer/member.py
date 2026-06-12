
class Member:

    def login_check():
        _queryString = """
         select pwdcompare(%s,a.MemberPassEncrypt) as IsPWCorrect, a.EmpSeqNo, a.OfficeCode, a.EmpAuthGbn, isnull(mo.ade_sch,'N') as aiuseyn
          from bizwiz20db.Member_Login a inner join bizwiz20db.M_Office mo on a.OfficeCode = mo.OfficeCode 
         where a.MemberID = %s and a.EmpAuthGbn in ('ADM', 'MEM', 'NMM')
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
                , isnull(c.OfficeName,'') as office_name
                , isnull(c.UseOption,'N') as gw_useYN
                , isnull(c.UseQpisOption,'N') as qpis_useYN
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
            Where B.MemberID = %s AND a.EmpAuthGbn in ('ADM','MEM','NMM') and C.ade_sch = 'Y'
        
        """
        return _queryString

    def login_check_token():
        _queryString = """
        select a.EmpSeqNo, a.OfficeCode, a.EmpAuthGbn, isnull(mo.ade_sch,'N') as aiuseyn
          from bizwiz20db.Member_Login a inner join bizwiz20db.M_Office mo on a.OfficeCode = mo.OfficeCode 
         where a.MemberID = %s and a.EmpAuthGbn in ('ADM', 'MEM', 'NMM')
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

    def team_by_name_and_office():
        _queryString = """
        SELECT mb_part, name AS mb_partName
          FROM bizwiz20db.T_Team
         WHERE OfficeCode = %s AND name = %s
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
          AND m.EmpAuthGbn IN ('ADM','MEM')  -- NMM(탈퇴자) 제외
        """
        return _queryString

    def member_export_by_office():
        # 대분류/중분류/소분류 이름을 기존엔 행마다 상관 스칼라 서브쿼리 3개로 조회했으나,
        # 대형 오피스(수천 명)에서 첫 로드가 느림(1000ms+). depth별 T_Team 을 ROW_NUMBER 로
        # (office,kind...)당 1행만 남긴 파생테이블과 LEFT JOIN 하도록 교체.
        # - 성능: 4081명 오피스 1159ms→265ms(실측), 출력 byte-identical(48k행 포함 검증).
        # - 안전성: 파생테이블이 partition 당 1행을 보장하므로 T_Team 에 UNIQUE 제약이
        #   없어 중복이 들어와도 JOIN fan-out(멤버 행 증식)이 구조적으로 발생하지 않는다.
        #   (단순 LEFT JOIN 은 빠르지만 fan-out 미방지, OUTER APPLY 는 안전하나 느림 → 둘 다 배제)
        _queryString = """
        select a.EmpSeqNo, c.big_kind
             , isnull(t1.name, '') as big_kind_name
             , c.middle_kind
             , isnull(t2.name, '') as middle_kind_name
             , c.small_kind
             , isnull(t3.name, '') as small_kind_name
             , c.mb_part, c.name as mb_part_name, a.OfficeEmpNum, a.EmployeeName, b.MemberID, a.duty, a.career, a.headnurse, a.joindate
             , LEFT(CONVERT(VARCHAR(10), a.DateOfBirth, 23), 10) as DateOfBirth
             , a.PortableTel
             , CASE WHEN UPPER(TRIM(COALESCE(NULLIF(GENDER, N' '),N'기타'))) IN (N'남',N'Y') THEN N'남'
                    WHEN UPPER(TRIM(COALESCE(NULLIF(GENDER, N' '),N'기타'))) IN (N'여',N'N') THEN N'여'
                    ELSE TRIM(COALESCE(NULLIF(GENDER, N' '),N'기타'))
                     END Gender
          from bizwiz20db.Member a
               Inner Join bizwiz20db.Member_Login b On a.OfficeCode=b.OfficeCode And a.EmpSeqNo=b.EmpSeqNo
               Left Join bizwiz20db.T_Team c On a.mb_part=c.mb_part And a.OfficeCode=c.OfficeCode
               Left Join (
                   select OfficeCode, big_kind, name from (
                       select OfficeCode, big_kind, name,
                              row_number() over (partition by OfficeCode, big_kind order by name) rn
                         from bizwiz20db.T_Team where depth='1'
                   ) z where rn=1
               ) t1 On t1.OfficeCode=c.OfficeCode And t1.big_kind=c.big_kind
               Left Join (
                   select OfficeCode, big_kind, middle_kind, name from (
                       select OfficeCode, big_kind, middle_kind, name,
                              row_number() over (partition by OfficeCode, big_kind, middle_kind order by name) rn
                         from bizwiz20db.T_Team where depth='2'
                   ) z where rn=1
               ) t2 On t2.OfficeCode=c.OfficeCode And t2.big_kind=c.big_kind And t2.middle_kind=c.middle_kind
               Left Join (
                   select OfficeCode, big_kind, middle_kind, small_kind, name from (
                       select OfficeCode, big_kind, middle_kind, small_kind, name,
                              row_number() over (partition by OfficeCode, big_kind, middle_kind, small_kind order by name) rn
                         from bizwiz20db.T_Team where depth='3'
                   ) z where rn=1
               ) t3 On t3.OfficeCode=c.OfficeCode And t3.big_kind=c.big_kind And t3.middle_kind=c.middle_kind And t3.small_kind=c.small_kind
         where b.officecode = %s and a.EmpAuthGbn in ('ADM','MEM')  -- NMM(탈퇴자) 제외
        """
        return _queryString
