from pydantic import BaseModel
from typing import Optional

class TokenData(BaseModel):
    account_id: Optional[str] = None

class User(BaseModel):
    nurse_id: str
    account_id: str
    office_id: str
    group_id: str
    is_head_nurse: bool = False
    is_master_admin: bool = False
    name: str
    # EmpSeqNo: str = None
    EmpAuthGbn: str = None

    class Config:
        from_attributes = True  