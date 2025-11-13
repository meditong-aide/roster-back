
class Token:
    """게시판 리스트"""

    @staticmethod
    def Get_Token () :
        _queryString = """
            select token from dbo.Tokens where clientId = %s and clientSecret = %s and expireddate >= %s
        """
        return _queryString

    @staticmethod
    def Set_Token():
        _queryString = """
            INSERT INTO [dbo].[Tokens] ([clientId], [clientSecret], [token], [expireddate])
            VALUES(%s, %s, %s, %s)
        """
        return _queryString
