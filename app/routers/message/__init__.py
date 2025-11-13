# routers/message/__init__.py
from fastapi import APIRouter
from . import list, write, view, delete, reply

# Create a new APIRouter instance for the message module
message_router = APIRouter(
    prefix="/message",
    tags=["message"]
)

# Include the router from list.py into the message_router
message_router.include_router(list.router)
message_router.include_router(write.router)
message_router.include_router(view.router)
message_router.include_router(delete.router)
message_router.include_router(reply.router)