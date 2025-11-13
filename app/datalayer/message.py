
class Message:
    @staticmethod
    def set_message():
        _queryString = """
            INSERT INTO eun_roster.dbo.message (officecode, sendempseqno, receptionempseqno, message, messageimg)
            VALUES(%s, %s, %s, %s, %s);
            """
        return _queryString

    @staticmethod
    def get_message_view () :
        _queryString = """
            select a.idx, a.sendempseqno, b.EmployeeName as sendername, b.duty as senderduty, a.receptionempseqno, c.EmployeeName as receptionname, c.duty as receptionduty, a.message, a.messageimg, a.readyn, convert(char(10), a.regdate,2) as regdate, convert(char(10), a.readdate,2) as readdate
              from eun_roster.dbo.message a
              inner join eun_gw.bizwiz20db.Member b on a.officecode = b.OfficeCode and a.sendempseqno = b.EmpSeqNo  
              inner join eun_gw.bizwiz20db.Member c on a.officecode = c.OfficeCode and a.receptionempseqno = c.EmpSeqNo
            where a.idx = %s
        """
        return _queryString

    @staticmethod
    def set_message_read () :
        _queryString = """
            update eun_roster.dbo.message set readyn = 'Y', readdate = getdate() where idx = %s
        """
        return _queryString

    @staticmethod
    def set_message_delete () :
        _queryString = """
            delete eun_roster.dbo.message where idx = %s
        """
        return _queryString

    @staticmethod
    def get_message_list (page: int, pagesize: int, list_type: str):

        _queryString = """
            select a.idx, a.sendempseqno, b.EmployeeName as sendername, b.duty as senderduty, a.receptionempseqno, c.EmployeeName as receptionname, c.duty as receptionduty, a.message, a.messageimg, a.readyn, convert(char(10), a.regdate,2) as regdate, convert(char(10), a.readdate,2) as readdate
              from eun_roster.dbo.message a
              inner join eun_gw.bizwiz20db.Member b on a.officecode = b.OfficeCode and a.sendempseqno = b.EmpSeqNo  
              inner join eun_gw.bizwiz20db.Member c on a.officecode = c.OfficeCode and a.receptionempseqno = c.EmpSeqNo
            where 1=1
        """
        if list_type == "send":
            _queryString = _queryString + " and a.sendempseqno = %s "
        else:
            _queryString = _queryString + " and a.receptionempseqno = %s "

        _queryString = _queryString + "order by a.idx desc "
        # _queryString = _queryString + "OFFSET " + str((page - 1) * pagesize) + " ROWS "
        _queryString = _queryString + "OFFSET " + str(page) + " ROWS "
        _queryString = _queryString + "FETCH NEXT  " + str(pagesize) + " ROW ONLY "

        return _queryString

    @staticmethod
    def get_message_list_cnt (list_type: str):

        _queryString = """
            select count(*) as total_count
              from eun_roster.dbo.message a
              inner join eun_gw.bizwiz20db.Member b on a.officecode = b.OfficeCode and a.sendempseqno = b.EmpSeqNo  
              inner join eun_gw.bizwiz20db.Member c on a.officecode = c.OfficeCode and a.receptionempseqno = c.EmpSeqNo
            where 1=1
        """
        if list_type == "send":
            _queryString = _queryString + " and a.sendempseqno = %s "
        else:
            _queryString = _queryString + " and a.receptionempseqno = %s "

        return _queryString