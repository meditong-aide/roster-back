# routers/message/__init__.py
from fastapi import APIRouter
from . import list, write

# Create a new APIRouter instance for the contact module
contact_router = APIRouter(
    prefix="/contact",
    tags=["contact"]
)

# Include the router from list.py into the contact_router
contact_router.include_router(list.router)
contact_router.include_router(write.router)
