from datalayer.common import roster_db


class sticker:
    @staticmethod
    def get_list():
        _queryString = f"""
               select OfficeCode, EmpSeqNo, stcker_date,sticker_contents from {roster_db()}.dbo.sticker where OfficeCode = %s and EmpSeqNo = %s and stcker_date =%s ;
               """
        return _queryString

    @staticmethod
    def upsert_sticker():
        """(사무소·사번·월) 한 행을 갱신하거나 없으면 만든다 — **한 배치**로.

        ★ 이전 구현은 `delete_sticker()` → `insert_sticker()` 를 각각
          `msdb_manager.execute()` 로 호출했다. 그 메서드는 호출마다
          **연결을 새로 열고 commit 하고 닫는다**. 즉 DELETE 가 먼저 커밋된 뒤
          INSERT 가 실패하면 **기존 스티커가 사라진 채 끝난다.**
          실제로 dev 에서 그 상태가 났다(regdate DEFAULT 부재로 INSERT 만 실패).
        ★ 그래서 DELETE 를 아예 없앴다. 한 문장 배치라 실패 시 원래 행이 그대로 남는다.
        ★ `UPDLOCK, HOLDLOCK` 이 필요한 이유 — 이 테이블에는
          (OfficeCode, empseqno, stcker_date) UNIQUE 제약이 **없다**(PK 는 identity `idx`).
          락이 없으면 두 요청이 동시에 UPDATE 0행을 보고 **둘 다 INSERT** 해 중복이 생긴다.
          운영 테이블 DDL 은 건드리지 않기로 했으므로 제약 대신 락으로 막는다.
        ★ 모바일은 삭제 API 를 쓰지 않는다. 해당 칸을 0 으로 만든 **배열 전체**를 다시
          보내므로(`sticker.ts` `stickerArray.join(",")`), 이 upsert 하나로 추가·교체·삭제가 다 된다.

        ★★ `idx`·`regdate` 를 INSERT 에 **적지 않는 것이 맞다.** 운영은 둘 다 자동
          (`idx` IDENTITY(1,1) · `regdate` DEFAULT getdate())이라 적을 필요가 없고,
          `idx` 는 **적으면 오히려 깨진다**(IDENTITY_INSERT OFF 상태에서 명시하면 오류).
          dev 에서 나던 `Cannot insert the value NULL into column 'regdate'` 는
          **개발 DB 스키마가 운영과 달라서**지 이 쿼리 탓이 아니다.
          `regdate` 만 채워 보면 곧바로 `... column 'idx'` 로 같은 오류가 난다(실측).
          즉 **코드로는 못 고친다** — 양쪽에서 동작하는 INSERT 문이 존재하지 않는다.
          해법은 dev 스키마를 운영에 맞추는 마이그레이션 하나뿐이다.

        params 순서: (contents, office, emp, date, office, emp, date, contents)
        """
        _queryString = f"""
        UPDATE {roster_db()}.dbo.sticker WITH (UPDLOCK, HOLDLOCK)
           SET sticker_contents = %s, regdate = getdate()
         WHERE OfficeCode = %s and empseqno = %s and stcker_date = %s ;

        IF @@ROWCOUNT = 0
            INSERT INTO {roster_db()}.dbo.sticker (OfficeCode, empseqno, stcker_date, sticker_contents)
            VALUES (%s , %s , %s , %s ) ;
        """
        return _queryString

    @staticmethod
    def delete_sticker():
        _queryString = f"""
         delete {roster_db()}.dbo.sticker where OfficeCode = %s and EmpSeqNo = %s and stcker_date =%s ;
         """
        return _queryString
