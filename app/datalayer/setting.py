
class Setting:

    @staticmethod
    def insert_division():
        _queryString = """
        Insert Into bizwiz20db.TB_EasySetting_Division_Tmp(Num, OfficeCode, EmpSeqNo, Depth1, Depth2, Depth3, RegDate) VALUES(%s, %s, %s, %s, %s, %s, %s);
        """
        return _queryString

    @staticmethod
    def list_division():
        _queryString = """
        select Num, OfficeCode, EmpSeqNo, Depth1, Depth2, Depth3, RegDate from bizwiz20db.TB_EasySetting_Division_Tmp where OfficeCode = %s and EmpSeqNo = %s;
        """
        return _queryString

    @staticmethod
    def list_division_exsist():
        _queryString = """
            Select b.big_kind, b.middle_kind, b.small_kind, b.mb_part, b.mb_partName, b.sort
            From
            (
                Select a.big_kind, a.middle_kind, a. small_kind, a.mb_part, a.sort, 
                Case When len(a.mb_part) = 4 Then
                     (Select name From bizwiz20db.T_Team with(nolock) Where mb_part=a.mb_part And OfficeCode=a.OfficeCode and t_use='Y')
                     When len(a.mb_part) = 9 Then
                     (Select name From bizwiz20db.T_Team with(nolock) Where mb_part=Convert(VarChar(4), a.mb_part, 120) And OfficeCode=a.OfficeCode and t_use='Y') + ',' +
                     (Select name From bizwiz20db.T_Team with(nolock) Where mb_part=a.mb_part And OfficeCode=a.OfficeCode and t_use='Y')
                     When len(a.mb_part) = 14 Then
                     (Select name From bizwiz20db.T_Team with(nolock) Where mb_part=Convert(VarChar(4), a.mb_part, 120) And OfficeCode=a.OfficeCode and t_use='Y') + ',' +
                     (Select name From bizwiz20db.T_Team with(nolock) Where mb_part=Convert(VarChar(9), a.mb_part, 120) And OfficeCode=a.OfficeCode and t_use='Y') + ',' +
                     (Select name From bizwiz20db.T_Team with(nolock) Where mb_part=a.mb_part And OfficeCode=a.OfficeCode and t_use='Y')
                Else '' End As mb_partName
                From bizwiz20db.T_Team as a with(nolock) Where a.OfficeCode=  %s
            ) as b
        """
        return _queryString

    @staticmethod
    def delete_division():
        _queryString = """
        delete from bizwiz20db.TB_EasySetting_Division_Tmp where OfficeCode = %s and EmpSeqNo = %s;
        """
        return _queryString


    @staticmethod
    def select_division_depth1():
        _queryString = """
        select name as depth1 from eun_gw.bizwiz20db.T_Team where officecode = %s and depth = '1'
        """
        return _queryString

    @staticmethod
    def select_division_depth2():
        _queryString = """
        select name as depth2 from eun_gw.bizwiz20db.T_Team where officecode = %s and depth = '2'
        """
        return _queryString

    @staticmethod
    def select_division_depth3():
        _queryString = """
        select name as depth3 from eun_gw.bizwiz20db.T_Team where officecode = %s and depth = '3'
        """
        return _queryString

    @staticmethod
    def insert_member():
        _queryString = """
        Insert Into bizwiz20db.TB_EasySetting_Member_Tmp(Num, OfficeCode, EmpSeqNo, EmpNum, MemberID, EmployeeName, Gender, Birthday, JoinDate, Tel, PortableTel, Email, Address, Manager, Depth1, Depth2, Depth3, Posin, RegDate, career, duty, headnurse, nightkeep) 
        VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
        """
        return _queryString

    @staticmethod
    def list_member():
        _queryString = """
        select Num, OfficeCode, EmpSeqNo, EmpNum, MemberID, EmployeeName, Gender, Birthday, JoinDate, Tel, PortableTel, Email, Address, Manager, Depth1, Depth2, Depth3, Posin, RegDate
          from bizwiz20db.TB_EasySetting_Member_Tmp where OfficeCode = %s and EmpSeqNo = %s;
        """
        return _queryString

    @staticmethod
    def member_id_check():
        _queryString = """
        select count(*) as cnt
          from bizwiz20db.Member_Login WITH(NOLOCK) where MemberID = %s ;
        """
        return _queryString

    @staticmethod
    def delete_member():
        _queryString = """
        delete from bizwiz20db.TB_EasySetting_Member_Tmp where OfficeCode = %s and EmpSeqNo = %s;
        """
        return _queryString

    @staticmethod
    def insert_position():
        _queryString = """
        Insert Into bizwiz20db.TB_EasySetting_Position_Tmp(Num, OfficeCode, EmpSeqNo, Title, RegDate) VALUES(%s, %s, %s, %s, %s);
        """
        return _queryString

    @staticmethod
    def list_position():
        _queryString = """
        select * from bizwiz20db.TB_EasySetting_Position_Tmp where OfficeCode = %s and EmpSeqNo = %s;
        """
        return _queryString

    @staticmethod
    def position_check():
        _queryString = """
        select name as positionTitle from bizwiz20db.T_Part where officecode = %s;
        """
        return _queryString

    @staticmethod
    def delete_position():
        _queryString = """
                       delete \
                       from bizwiz20db.TB_EasySetting_Position_Tmp \
                       where OfficeCode = %s \
                         and EmpSeqNo = %s; \
                       """
        return _queryString
