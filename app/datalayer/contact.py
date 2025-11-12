class Contact:
    @staticmethod
    def set_contact():
        _queryString = """
            INSERT INTO bizwiz20db.Manage_Work
            (ManageNo, WriteDate, Writer, WriterID, Feedback, NextManager, Category,EmpSeqName, Tel , context , UsingTime, Filename,Route,CategorySub,CateType,Manager,JobState,JobDate,Info,BoardFile,Comment,wEmail, title) 
            VALUES
            (%s, %s, %s, %s, 0, 0, %s, %s, %s, %s, 0, %s, 5, '상담문의', '문의', '', '접수', '', '', '', '', %s, %s) ;
            """
        return _queryString

    @staticmethod
    def get_contact_list_cnt():
        _queryString = """
            select count(*) as total_count
            from bizwiz20db.Manage_Work
            where WriterID = %s
            """
        return _queryString

    @staticmethod
    def get_contact_list(page: int, pagesize: int):
        _queryString = """
            select No, title, context, Writer, WriterID, writeDate, filename, Tel, wEmail, isnull(replycontent,'') as replycontent, case when jobState = '완료' then '완료' else '접수' end as jobState
            from bizwiz20db.Manage_Work
            where WriterID = %s
            order by No desc
            """
        _queryString = _queryString + "OFFSET " + str((page - 1) * pagesize) + " ROWS "
        _queryString = _queryString + "FETCH NEXT  " + str(pagesize) + " ROW ONLY "

        return _queryString