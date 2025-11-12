class sticker:
    @staticmethod
    def get_list():
        _queryString = """
               select OfficeCode, EmpSeqNo, stcker_date,sticker_contents from eun_roster.dbo.sticker where OfficeCode = %s and EmpSeqNo = %s and stcker_date =%s ;
               """
        return _queryString

    @staticmethod
    def insert_sticker():
        _queryString = """
          insert into eun_roster.dbo.sticker (OfficeCode, empseqno, stcker_date, sticker_contents ) VALUES (%s , %s , %s , %s ) ;
          """
        return _queryString

    @staticmethod
    def delete_sticker():
        _queryString = """
         delete eun_roster.dbo.sticker where OfficeCode = %s and EmpSeqNo = %s and stcker_date =%s ;
         """
        return _queryString
