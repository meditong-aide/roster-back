from pydantic import BaseModel
from typing import Optional

class TokenData(BaseModel):
    account_id: Optional[str] = None

class User(BaseModel):
    nurse_id: str
    account_id: str
<<<<<<< HEAD
    office_id: str
    group_id: str
    is_head_nurse: str
    name: str
    # EmpSeqNo: str = None
    EmpAuthGbn: str = None
=======
    office_id: Optional[str] = None
    group_id: str
    is_head_nurse: bool
    name: str
    emp_auth_gbn: Optional[str] = None  # 'ADM'이면 마스터 관리자
    is_master_admin: bool = False       # 편의 플래그
>>>>>>> integ/admin-from-mysql

    class Config:
        from_attributes = True  