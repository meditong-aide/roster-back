class Contact:
    @staticmethod
    def set_contact():
        _queryString = """
            INSERT INTO bizwiz20db.Manage_Work
            (ManageNo, WriteDate, Writer, WriterID, Feedback, NextManager, Category,EmpSeqName, Tel , context , UsingTime, Filename,Route,CategorySub,CateType,Manager,JobState,JobDate,Info,BoardFile,Comment,wEmail, title) 
            VALUES
            (%s, %s, %s, %s, 0, 0, %s, %s, %s, %s, 0, %s, 5, '이용문의', '문의', '', '접수', '', '', '', '', %s, %s) ;
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
            select No, title, context, Writer, WriterID, writeDate, filename, Tel, wEmail,
                   isnull(replycontent,'') as replycontent,
                   isnull(Comment,'') as Comment,
                   isnull(Manager,'') as Manager,
                   isnull(jobState,'') as jobState
            from bizwiz20db.Manage_Work
            where WriterID = %s
            order by No desc
            """
        _queryString = _queryString + "OFFSET " + str((page - 1) * pagesize) + " ROWS "
        _queryString = _queryString + "FETCH NEXT  " + str(pagesize) + " ROW ONLY "

        return _queryString

    @staticmethod
    def delete_contact():
        _queryString = """
            DELETE FROM bizwiz20db.Manage_Work
            WHERE No = %s AND WriterID = %s
            """
        return _queryString