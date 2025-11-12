# routers/message/__init__.py
from fastapi import APIRouter
from . import list,sticker_proc

# Create a new APIRouter instance for the message module
sticker_router = APIRouter(
    prefix="/sticker",
    tags=["sticker"]
)

# Include the router from list.py into the message_router
sticker_router.include_router(list.router)
sticker_router.include_router(sticker_proc.router)
