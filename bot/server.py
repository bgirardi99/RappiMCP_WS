"""
FastAPI webhook server.

Endpoints:
  POST /webhook/whatsapp  — Twilio WhatsApp webhook (TwiML response)
  GET  /health            — liveness check
  /auth/*                 — registration flow (mounted from auth.router)

Run:
  uvicorn bot.server:app --host 0.0.0.0 --port 8000
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi import FastAPI, Form, Response
from fastapi.middleware.cors import CORSMiddleware

from bot.agent import run_agent
from bot.db import init_db
from auth.router import router as auth_router

app = FastAPI(title="RappiBot")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router, prefix="/auth")


@app.on_event("startup")
def startup() -> None:
    init_db(os.environ.get("DB_PATH", "bot.db"))


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/webhook/whatsapp")
async def whatsapp_webhook(
    From: str = Form(...),
    Body: str = Form(...),
) -> Response:
    """
    Twilio WhatsApp webhook. Returns TwiML XML.
    Twilio calls this URL when a user sends a WhatsApp message.

    For multiple replies (e.g. text + checkout link), we send the first
    via TwiML and enqueue the rest via Twilio REST API.
    """
    phone = From.replace("whatsapp:", "").strip()
    text = Body.strip()

    if not text:
        return Response(content=_twiml(""), media_type="application/xml")

    reply = await _process(phone, text)

    if isinstance(reply, list):
        # First message via TwiML, subsequent ones via REST API
        twiml_text = reply[0]
        for extra in reply[1:]:
            _send_whatsapp(From, extra)
    else:
        twiml_text = reply

    return Response(content=_twiml(twiml_text), media_type="application/xml")


async def _process(phone: str, text: str) -> str | list[str]:
    """Run agent in a thread to avoid blocking the event loop."""
    import asyncio
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, run_agent, phone, text)


def _twiml(message: str) -> str:
    safe = message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{safe}</Message></Response>'


def _send_whatsapp(to: str, message: str) -> None:
    """Send an additional WhatsApp message via Twilio REST API."""
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
    from_number = os.environ.get("TWILIO_WHATSAPP_NUMBER", "whatsapp:+14155238886")

    if not account_sid or not auth_token:
        return  # Skip if Twilio credentials not configured (e.g. dev mode)

    try:
        from twilio.rest import Client
        client = Client(account_sid, auth_token)
        # Ensure 'to' has the whatsapp: prefix
        to_number = to if to.startswith("whatsapp:") else f"whatsapp:{to}"
        client.messages.create(body=message, from_=from_number, to=to_number)
    except Exception as e:
        print(f"[warn] Failed to send extra WhatsApp message: {e}")
