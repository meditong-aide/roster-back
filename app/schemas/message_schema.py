from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime


class MessageMemberItem(BaseModel):
    nurse_id: str
    name: str
    role: Optional[str] = None
    level_: Optional[str] = None
    group_id: str
    group_name: str
    # 프로필 사진 URL. 미등록이면 null — 프론트가 기본 아바타로 대체한다.
    profile_image_url: Optional[str] = None

    class Config:
        from_attributes = True


class MessageCreateRequest(BaseModel):
    receiver_nurse_ids: List[str]
    message: Optional[str] = None
    message_img: Optional[str] = None


class MessageItem(BaseModel):
    id: int
    sender_nurse_id: str
    sender_name: str
    sender_role: Optional[str] = None
    # 프로필 사진 URL. 미등록이면 null — 프론트가 기본 아바타로 대체한다.
    sender_profile_image_url: Optional[str] = None
    receiver_nurse_id: str
    receiver_name: str
    receiver_role: Optional[str] = None
    receiver_profile_image_url: Optional[str] = None
    message: Optional[str] = None
    message_img: Optional[str] = None
    is_read: bool
    created_at: datetime
    read_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class MessageCountResponse(BaseModel):
    total_count: int
