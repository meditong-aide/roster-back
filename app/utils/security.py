import os
from datetime import date
from datetime import datetime, timedelta, timezone
from typing import Any
from typing import Optional

from fastapi import HTTPException
from jose import jwt, JWTError
from passlib.context import CryptContext

from datalayer.token import Token
from db.client2 import msdb_manager

# 오늘 날짜 객체 가져오기
today = date.today()
current_date = today.strftime('%Y-%m-%d')

# Configuration
SECRET_KEY = os.getenv("SECRET_KEY")
ALGORITHM = os.getenv("ALGORITHM")
ACCESS_TOKEN_EXPIRE_MINUTES = 3600

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# def verify_password(plain_password: str, hashed_password: str) -> bool:
#     """Verifies a plain password against a hashed one."""
#     return pwd_context.verify(plain_password, hashed_password)
#
# def get_password_hash(password: str) -> str:
#     """Hashes a password."""
#     return pwd_context.hash(password)

def create_access_token(data: dict, expires_delta: timedelta | None = None) -> str:
    """Creates a JWT access token."""
    clientId = data['clientId']
    clientSecret = data['clientSecret']

    try:
        rows = msdb_manager.fetch_all(Token.Get_Token(), params=(clientId, clientSecret, current_date))
    except Exception:
        raise HTTPException(status_code=500, detail=f"DB Error")

    if not rows:
        #print("딕셔너리가 비어있습니다.")
        to_encode = data.copy()
        if expires_delta:
            expire = datetime.now(tz=timezone.utc) + expires_delta
        else:
            expire = datetime.now(tz=timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
        to_encode.update({"exp": expire})
        encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

        try:
            #print("딕셔너리가 있습니다.")
            new_id = msdb_manager.execute(Token.Set_Token(), params=(clientId, clientSecret, encoded_jwt, current_date))
            if new_id is None:
                raise HTTPException(status_code=500, detail=f"DB Error")
        except Exception:
            raise HTTPException(status_code=500, detail=f"DB Error")
    else :

        encoded_jwt = rows[0]['token']

    return encoded_jwt

def decode_access_token(token: str) -> Any:
    """Decodes a JWT token."""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except JWTError:
        return None

def create_login_token(data: dict, expires_delta: Optional[timedelta] = None):

    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(tz=timezone.utc) + expires_delta
    else:
        expire = datetime.now(tz=timezone.utc) + timedelta(minutes=15)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt