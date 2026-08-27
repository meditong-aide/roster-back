
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
    def insert_mobile_user_setting_list():
        _queryString = """
        INSERT INTO bizwiz20db.TB_Mobile_User_Setting_List(MemberID, AutoYN, WifiYN, PushYN, DeviceKey, RegDate) VALUES (%s, 'Y', 'Y', 'Y', '', %s);
        """
        return _queryString

    @staticmethod
    def get_push_yn():
        _queryString = """
        SELECT PushYN FROM bizwiz20db.TB_Mobile_User_Setting_List WHERE MemberID = %s
        """
        return _queryString

    @staticmethod
    def update_push_yn():
        _queryString = """
        UPDATE bizwiz20db.TB_Mobile_User_Setting_List SET PushYN = %s WHERE MemberID = %s
        """
        return _queryString

    @staticmethod
    def insert_push_yn_if_absent():
        """설정 행이 없는 계정에 한해 행을 만들며 PushYN 을 지정값으로 넣는다.

        ★ `WHERE NOT EXISTS` 가 필수다 — 이 테이블의 PK 는 `Idx`(identity)이고
          `MemberID` 는 **non-unique 인덱스**라 그냥 INSERT 하면 같은 MemberID 로
          행이 여러 개 쌓인다. `get_push_yn()` 은 `row[0]` 만 보므로 그 순간부터
          조회값이 어느 행을 집는지에 따라 갈린다.

        ★★ `WITH (UPDLOCK, HOLDLOCK)` 도 필수다. 존재검사와 INSERT 는 read-committed
          에서 **원자적이지 않다** — 두 요청이 같은 MemberID 로 동시에 첫 토글을 하면
          둘 다 NOT EXISTS 를 통과해 **둘 다 INSERT** 한다(DB 에 unique 제약이 없어
          막아주지도 않는다). 이 힌트가 키 범위를 잠가 두 번째를 대기시킨다.
          `MemberID` 인덱스가 있어 잠금 범위는 해당 키뿐이고 트랜잭션도 짧다.
          ※ UNIQUE 제약으로 막는 방법도 있으나 **그룹웨어 운영 테이블 DDL** 이라
            기존 중복이 있으면 생성이 실패하고 타 시스템 영향도 알 수 없어 택하지 않았다.

        ★ 기본값은 `insert_mobile_user_setting_list()`(엑셀 일괄등록 경로)와 맞춘다 —
          AutoYN='Y' · WifiYN='Y' · DeviceKey=''. 다른 건 PushYN 을 인자로 받는 것뿐이다.

        params: (MemberID, PushYN, RegDate, MemberID)
        """
        _queryString = """
        INSERT INTO bizwiz20db.TB_Mobile_User_Setting_List
               (MemberID, AutoYN, WifiYN, PushYN, DeviceKey, RegDate)
        SELECT %s, 'Y', 'Y', %s, '', %s
         WHERE NOT EXISTS (
               SELECT 1 FROM bizwiz20db.TB_Mobile_User_Setting_List WITH (UPDLOCK, HOLDLOCK)
                WHERE MemberID = %s)
        """
        return _queryString

    @staticmethod
    def delete_position():
        _queryString = """
                       delete \
                       from bizwiz20db.TB_EasySetting_Position_Tmp  \
                       where OfficeCode = %s \
                         and EmpSeqNo = %s; \
                       """
        return _queryString
