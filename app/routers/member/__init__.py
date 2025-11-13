# routers/message/__init__.py
from fastapi import APIRouter
from . import edit

# Create a new APIRouter instance for the message module
member_router = APIRouter(
    prefix="/member",
    tags=["member"]
)

# Include the router from list.py into the message_router
member_router.include_router(edit.router)
