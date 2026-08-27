from datalayer.common import roster_db


class sticker:
    @staticmethod
    def get_list():
        _queryString = f"""
               select OfficeCode, EmpSeqNo, stcker_date,sticker_contents from {roster_db()}.dbo.sticker where OfficeCode = %s and EmpSeqNo = %s and stcker_date =%s ;
               """
        return _queryString

    @staticmethod
    def insert_sticker():
        _queryString = f"""
          insert into {roster_db()}.dbo.sticker (OfficeCode, empseqno, stcker_date, sticker_contents ) VALUES (%s , %s , %s , %s ) ;
          """
        return _queryString

    @staticmethod
    def delete_sticker():
        _queryString = f"""
         delete {roster_db()}.dbo.sticker where OfficeCode = %s and EmpSeqNo = %s and stcker_date =%s ;
         """
        return _queryString
