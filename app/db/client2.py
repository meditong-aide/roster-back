import logging
import os
from contextlib import contextmanager
from typing import Generator, Any
import dotenv
dotenv.load_dotenv()

import pymssql
import pymysql

# 로깅 설정
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class MssqlDatabasemanager:
    def __init__(self):
        self.host = os.getenv("MS_DB_HOST")
        self.port = os.getenv("MS_DB_PORT")
        self.database = os.getenv("MS_DB_NAME")
        self.user = os.getenv("MS_DB_USER")
        self.password = os.getenv("MS_DB_PASSWORD")

    def get_connection(self, database: str | None = None, charset: str | None = 'EUC-KR',
                       timeout: int | None = None) -> pymssql.Connection:
        """데이터베이스 연결을 생성합니다. 선택적으로 데이터베이스명을 지정할 수 있습니다.

        Args:
            timeout: 쿼리 실행 대기 상한(초). None 이면 **무한 대기**(pymssql 기본 0).

        ★ 기본을 무한으로 남겨 둔 이유: 여기는 근무표 생성 배치도 함께 쓰는 통로다.
          전역으로 상한을 걸면 오래 걸리는 배치 쿼리가 조용히 끊긴다. 그래서
          **호출부가 필요할 때만** 건다 — 특히 그룹웨어(`bizwiz20db`) 를 보는 경로.
          그쪽은 21개 DB 가 한 인스턴스에 얹힌 공용 서버라, 남의 락 경합에 물리면
          이 프로세스가 함께 멈춘다(uvicorn 워커가 그 대기에 잡힌다).
        """
        try:
            kwargs = {
                "server": self.host,
                "port": self.port,
                "database": (database or self.database),
                "user": self.user,
                "password": self.password,
                "charset": charset,
            }
            if timeout is not None:
                kwargs["timeout"] = timeout
                # 로그인 자체가 물리는 것도 막는다 — 쿼리 상한만 걸면 핸드셰이크에서 샌다.
                kwargs["login_timeout"] = min(int(timeout), 15)
            connection = pymssql.connect(**kwargs)
            return connection
        except Exception as e:
            logger.error(f"데이터베이스 연결 실패: {e}")
            raise

    @contextmanager
    def connection(self, database: str | None = None, charset: str | None = 'EUC-KR',
                   timeout: int | None = None) -> Generator[pymssql.Connection, None, None]:
        """컨텍스트 매니저로 연결을 열고 자동으로 닫습니다."""
        conn = self.get_connection(database, charset, timeout=timeout)
        try:
            yield conn
        finally:
            conn.close()

    def fetch_all(self, query: str, params: tuple | None = None, database: str | None = None,
                  charset: str | None = 'EUC-KR', timeout: int | None = None) -> list[dict]:
        """SELECT 쿼리를 실행하고 결과를 리스트[dict]로 반환합니다.

        `timeout` 을 주면 그 초를 넘길 때 예외가 난다. 안 주면 무한 대기다
        (`get_connection` 도크스트링 참조).
        """
        with self.connection(database, charset=charset, timeout=timeout) as conn:
            cursor = conn.cursor()
            cursor.execute(query, params or ())
            rows = cursor.fetchall()
            columns = [column[0] for column in cursor.description]
            return [dict(zip(columns, row)) for row in rows]

    def fetch_one(self, query: str, params: tuple | None = None, database: str | None = None) -> list[dict]:
        """SELECT COUNT(*) 쿼리를 실행하고 결과를 반환합니다."""
        with self.connection(database) as conn:
            cursor = conn.cursor()
            cursor.execute(query, params or ())
            rows = cursor.fetchone()

            rows = rows[0] if rows else ""

            return rows

    def execute(self, query: str, params: tuple | None = None, database: str | None = None) -> int:
        """INSERT/UPDATE/DELETE 쿼리를 실행하고 영향 받은 행 수를 반환합니다."""
        charset = 'UTF-8'

        with self.connection(database, charset) as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            conn.commit()
            return cursor.rowcount

    def bulk_execute(self, query: str, params: list, database: str | None = None) -> int:
        if not isinstance(params, list) or not all(isinstance(item, (tuple, list)) for item in params):
            raise TypeError("params argument must be a list of tuples or lists.")
        charset = 'UTF-8'

        with self.connection(database, charset) as conn:
            cursor = conn.cursor()
            cursor.executemany(query, params)
            conn.commit()
            return cursor.rowcount
        
# 전역 데이터베이스 매니저 인스턴스
msdb_manager = MssqlDatabasemanager()

class MariaDatabasemanager:
    def __init__(self):
        self.host = os.getenv("DB_HOST")
        self.port = os.getenv("DB_PORT")
        self.database = os.getenv("DB_NAME")
        self.user = os.getenv("DB_USER")
        self.password = os.getenv("DB_PASSWORD")
    def get_connection(self, database: str | None = None) -> pymysql.connections.Connection:
        """데이터베이스 연결을 생성합니다. 선택적으로 데이터베이스명을 지정할 수 있습니다."""
        try:
            connection = pymysql.connect(
                host=self.host,
                port=int(self.port),
                db=(database or self.database),
                user=self.user,
                password=self.password,
                charset='utf8mb4',
                autocommit=False,
                cursorclass=pymysql.cursors.Cursor,
            )
            return connection
        except Exception as e:
            logger.error(f"마리아DB 연결 실패: {e}")
            raise

    @contextmanager
    def connection(self, database: str | None = None) -> Generator[pymysql.connections.Connection, None, None]:
        """컨텍스트 매니저로 연결을 열고 자동으로 닫습니다."""
        conn = self.get_connection(database)
        try:
            yield conn
        finally:
            conn.close()

    def fetch_all(self, query: str, params: tuple | None = None, database: str | None = None) -> list[dict[str, Any]]:
        """SELECT 쿼리를 실행하고 결과를 리스트[dict]로 반환합니다."""
        with self.connection(database) as conn:
            cursor = conn.cursor()
            cursor.execute(query, params or ())
            rows = cursor.fetchall()
            columns = [desc[0] for desc in cursor.description]
            return [dict(zip(columns, row)) for row in rows]

    def fetch_one(self, query: str, params: tuple | None = None, database: str | None = None) -> Any:
        """SELECT COUNT(*)와 같은 단일 값을 반환하는 쿼리를 실행하고 결과를 반환합니다."""
        with self.connection(database) as conn:
            cursor = conn.cursor()
            cursor.execute(query, params or ())
            row = cursor.fetchone()
            return row if row else None

    def execute(self, query: str, params: tuple | None = None, database: str | None = None) -> int:
        """INSERT/UPDATE/DELETE 쿼리를 실행하고 영향 받은 행 수를 반환합니다."""

        with self.connection(database) as conn:
            cursor = conn.cursor()
            cursor.execute(query, params or ())
            conn.commit()
            return cursor.rowcount


# 전역 마리아DB 매니저 인스턴스
mariadb_manager = MariaDatabasemanager()


from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy import create_engine, func
_MSSQL_SESSION_MAKER: sessionmaker | None = None


import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.declarative import declarative_base
import dotenv

dotenv.load_dotenv()

DB_HOST = os.getenv("MS_DB_HOST")
DB_PORT = os.getenv("MS_DB_PORT")
DB_USER = os.getenv("MS_DB_USER")
DB_PASSWORD = os.getenv("MS_DB_PASSWORD")
EUN_DB_NAME = os.getenv("EUN_DB_NAME")

DATABASE_URL = f"mssql+pymssql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{EUN_DB_NAME}"

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=1800,
    # 원격 DB(49.50.163.159) 콜드커넥트 ~276ms(로그인 핸드셰이크) 회피 — warm 연결을
    # 더 많이 유지해 동시 요청이 재로그인 대신 재사용하도록 풀 확대(env 로 조정).
    pool_size=int(os.getenv("DB_POOL_SIZE", "20")),
    max_overflow=int(os.getenv("DB_MAX_OVERFLOW", "30")),
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close() 


# def _get_mssql_session() -> Session:
#     """MSSQL 전용 SQLAlchemy 세션을 반환합니다. .env의 MS_DB_* 값을 사용합니다."""
#     global _MSSQL_SESSION_MAKER
#     if _MSSQL_SESSION_MAKER is None:
#         host = os.getenv("MS_DB_HOST")
#         port = os.getenv("MS_DB_PORT")
#         dbname = os.getenv("MS_DB_NAME")
#         user = os.getenv("MS_DB_USER")
#         password = os.getenv("MS_DB_PASSWORD")
#         database_url = f"mssql+pymssql://{user}:{password}@{host}:{port}/{dbname}"
#         engine = create_engine(database_url, pool_pre_ping=True)
#         _MSSQL_SESSION_MAKER = sessionmaker(autocommit=False, autoflush=False, bind=engine)
#     return _MSSQL_SESSION_MAKER()

# engine = create_engine(DATABASE_URL)
# SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
# Base = declarative_base()

# def get_db():
#     db = _MSSQL_SESSION_MAKER()
#     try:
#         yield db
#     finally:
#         db.close() 
