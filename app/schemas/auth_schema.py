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
    EmpSeqNo: str = None
    EmpAuthGbn: str = None
    mb_part: str
    office_name: str
    mb_part_name: str
    gw_useYN: str
    qpis_useYN: str
    official_title_name: str | None  # 추가 필드
    
    # 추가
    is_nurse_registered: bool = False
    hn_auth: str | None = None  # 그룹 관리자 권한 ('HN' 또는 None)


    class Config:
        from_attributes = True  