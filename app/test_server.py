"""Standalone test server for the agent chat UI.

Runs only the test chat router without the full production app.
Stubs out db.client2 so it works on environments without MSSQL drivers.

Usage:
    cd roster-back/app && python test_server.py
    → http://localhost:8000/agent/test
"""

import sys
import types
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

# ── Stub db.client2 before anything imports models ──
# Same approach as tests/conftest.py — intercept the module so
# db.models imports our SQLite-backed Base instead of pymssql.
_stub_base = declarative_base()
_stub_engine = create_engine("sqlite:///:memory:")

_fake_client2 = types.ModuleType("db.client2")
_fake_client2.Base = _stub_base
_fake_client2.engine = _stub_engine
_fake_client2.SessionLocal = sessionmaker(bind=_stub_engine)
_fake_client2.get_db = lambda: None
sys.modules["db.client2"] = _fake_client2

import uvicorn
from fastapi import FastAPI

from agents_v2.test_chat_router import router as agent_test_router

app = FastAPI(title="Agent Test Server")
app.include_router(agent_test_router)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
