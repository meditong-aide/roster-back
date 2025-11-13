# routers/setting/__init__.py
from fastapi import APIRouter
from . import division, member, position

# Create a new APIRouter instance for the message module
router = APIRouter(
    prefix="/setting",
    tags=["setting"]
)

# Include the router from list.py into the message_router
router.include_router(division.router)
router.include_router(member.router)
router.include_router(position.router)
