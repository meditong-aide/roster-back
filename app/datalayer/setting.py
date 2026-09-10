
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
    def member_ids_taken(count: int):
        """업로드 전 아이디 중복 검사 — **여러 건을 한 번에**.

        ★ 예전엔 행마다 `member_id_check` 를 따로 던졌다. 500행이면 왕복 500번이라
          정상 상황에서도 지연이 쌓이고, 락 경합이 있으면 그만큼 오래 물린다.
          아이디는 오피스 무관 전역 유일이라 오피스 조건이 필요 없다(`member_id_check` 와 같은 전제).
        ★ `WITH(NOLOCK)` 을 쓰지 않는 이유는 `member_created_check` 주석 참조.
        ★ MSSQL 파라미터 상한(2100) 때문에 호출측이 끊어서 넣는다.

        params: MemberID 튜플 (count 개)
        """
        placeholders = ",".join(["%s"] * count)
        return f"""
        select MemberID from bizwiz20db.Member_Login
         where MemberID in ({placeholders})
        """

    @staticmethod
    def member_id_check():
        """업로드 전 아이디 중복 검사(단건).

        ★ 일괄 검사는 `member_ids_taken` 을 쓴다. 이 단건 버전은 남은 호출부 호환용이다.

        ★ `member_created_check` 와 같은 이유로 `WITH(NOLOCK)` 을 쓰지 않는다.
          미커밋 행을 읽으면 **아직 확정되지도 않은(그리고 롤백될) 아이디 때문에 멀쩡한
          신청을 거부**한다. 사용자는 왜 막혔는지 알 수 없고, 다시 올려도 같은 결과가 난다.
          아이디 1건짜리 인덱스 조회라 커밋된 데이터만 읽어도 부담이 없다.
        """
        _queryString = """
        select count(*) as cnt
          from bizwiz20db.Member_Login where MemberID = %s ;
        """
        return _queryString

    @staticmethod
    def member_created_check(count: int):
        """주어진 MemberID 중 **실제로 계정이 만들어진 것**만 돌려준다.

        ★ 왜 필요한가 — 그룹웨어 처리 페이지(`member_excel_ai_ok.asp`)가 200 을 주는 것과
          계정이 실제로 생긴 것은 다르다. 실측(2025-11-15 업로드분): 10건 중 6건만
          생성되고 4건은 **같은 사람이 이미 다른 아이디로 존재**해 거부됐는데, 응답은
          200 이라 전원 성공으로 보고됐다. 그래서 호출 뒤 이 쿼리로 대조해야 한다.
        ★★ **내 오피스에 생겼는지**까지 본다. `MemberID` 자체는 오피스 무관 전역 유일이지만
          (`member_id_check` 와 같은 전제), 아이디가 어딘가 존재한다는 것과 **이번 업로드가
          내 병원에 계정을 만들었다**는 것은 다르다. OfficeCode 를 빼면 ASP 가 엉뚱한
          오피스에 만들었거나 남의 오피스에 이미 있던 아이디를 "생성됨" 으로 읽는다.
        ★ MSSQL 파라미터 상한(2100) 때문에 호출측이 끊어서 넣는다.
        ★★ **`WITH(NOLOCK)` 을 쓰지 않는다.** 이 조회 하나가 "계정이 실제로 생겼는가" 의
          유일한 근거이고, 특히 ASP 가 비-200 을 준 경우를 성공으로 뒤집는 판단까지 여기에
          걸려 있다. NOLOCK 은 커밋 전 행을 읽는다 — ASP 트랜잭션이 아직 커밋 전인 순간을
          관측한 뒤 그게 롤백되면 **존재하지도 않는 계정을 생성됨으로 확정**하고, 반대로
          스캔 중 행을 놓치면 정상 생성을 부분 실패로 오판한다. 둘 다 사용자가 재업로드하게
          만들어 중복·부분 성공을 겹치게 한다.
          조회 대상이 아이디 목록(최대 500개)뿐이라 커밋된 데이터만 읽어도 부담이 없다.

        params: (OfficeCode,) + MemberID 튜플 (count 개)
        """
        placeholders = ",".join(["%s"] * count)
        return f"""
        select MemberID from bizwiz20db.Member_Login
         where OfficeCode = %s
           and MemberID in ({placeholders})
        """

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
        """엑셀 일괄등록 경로의 모바일 설정 기본행 생성.

        ★★ `WHERE NOT EXISTS` 가 필수다. 이 테이블의 PK 는 `Idx`(identity)이고 `MemberID` 는
          **non-unique 인덱스**라 그냥 INSERT 하면 같은 아이디로 행이 여러 개 쌓인다.
          업로드 경로에서 특히 잘 생긴다 — ASP 가 일부 계정을 거부해도 이 행은 이미
          들어가 있고, 사용자가 실패분을 고쳐 **다시 올리면 그때마다 또 쌓인다.**
          `get_push_yn()` 은 `row[0]` 만 보므로 그 순간부터 조회값이 어느 행을 집는지에
          따라 갈린다. 같은 이유로 `insert_push_yn_if_absent()` 도 같은 형태다.
        ※ UNIQUE 제약으로 막는 방법도 있으나 **그룹웨어 운영 테이블 DDL** 이라 기존 중복이
          있으면 생성이 실패하고 타 시스템 영향도 알 수 없어 택하지 않았다.

        params: (MemberID, RegDate, MemberID)
        """
        _queryString = """
        INSERT INTO bizwiz20db.TB_Mobile_User_Setting_List
               (MemberID, AutoYN, WifiYN, PushYN, DeviceKey, RegDate)
        SELECT %s, 'Y', 'Y', 'Y', '', %s
         WHERE NOT EXISTS (
               SELECT 1 FROM bizwiz20db.TB_Mobile_User_Setting_List WITH (UPDLOCK, HOLDLOCK)
                WHERE MemberID = %s)
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
