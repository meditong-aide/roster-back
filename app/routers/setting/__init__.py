# routers/message/__init__.py
from fastapi import APIRouter
from . import division, member, position

# Create a new APIRouter instance for the message module
setting_router = APIRouter(
    prefix="/setting",
    tags=["setting"]
)

# Include the router from list.py into the message_router
setting_router.include_router(division.router)
setting_router.include_router(member.router)
setting_router.include_router(position.router)
