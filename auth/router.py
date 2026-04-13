"""
Auth router — user registration flow.

GET  /auth/         — serve the mobile registration page
POST /auth/register — receive tokens from the browser, exchange and persist
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import requests as http_requests
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from bot.db import upsert_user
from bot.session import invalidate_client

router = APIRouter()

RAPPI_BASE = "https://services.rappi.cl"

_HTML_PATH = Path(__file__).parent / "static" / "index.html"


@router.get("/", response_class=HTMLResponse)
async def auth_page() -> str:
    return _HTML_PATH.read_text(encoding="utf-8")


@router.post("/register")
async def register(request: Request) -> JSONResponse:
    """
    Called by the browser after extracting Rappi cookies.

    Expected body:
    {
        "phone":         "+56912345678",
        "refresh_token": "ft.xxx...",
        "device_id":     "abc123",
        "user_id":       52884168
    }
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    required = ["phone", "refresh_token", "device_id", "user_id"]
    missing = [k for k in required if not body.get(k)]
    if missing:
        return JSONResponse({"error": f"missing fields: {missing}"}, status_code=400)

    # Exchange refresh token for a fresh access token
    try:
        resp = http_requests.post(
            f"{RAPPI_BASE}/api/rocket/refresh-token",
            json={"refresh_token": body["refresh_token"]},
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        resp.raise_for_status()
        tokens = resp.json()
    except Exception as e:
        return JSONResponse({"error": f"token exchange failed: {e}"}, status_code=400)

    phone = body["phone"].strip()
    user_id = int(body["user_id"])

    upsert_user(
        phone=phone,
        user_id=user_id,
        device_id=str(body["device_id"]),
        access_token=tokens["access_token"],
        refresh_token=tokens.get("refresh_token", body["refresh_token"]),
    )
    invalidate_client(phone)  # force reload on next message

    return JSONResponse({"status": "ok", "user_id": user_id, "phone": phone})
