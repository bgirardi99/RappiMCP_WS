"""
Claude API agentic loop.

run_agent(phone, user_message) → reply_text

Handles multi-round tool_use cycles, conversation history, and formats
checkout links for WhatsApp delivery.
"""

import json
import os
import anthropic

from bot.db import (
    append_message,
    get_recent_history,
    set_session_state,
    trim_history,
)
from bot.session import get_client
from bot.tools import TOOLS, WRITE_TOOLS, dispatch_tool

MAX_TOOL_ROUNDS = 12
HISTORY_KEEP = 40  # rows stored in DB per user

AUTH_URL = os.environ.get("AUTH_URL", "https://tu-servidor.com/auth/")

SYSTEM_PROMPT = f"""Eres un asistente de compras para Rappi Chile. Ayudas a los usuarios a buscar \
productos, gestionar su carrito y preparar el pago, todo por WhatsApp.

REGLAS OBLIGATORIAS:
1. Busca siempre productos en *español*.
2. ANTES de llamar rappi_checkout: llama rappi_get_cart, muestra el resumen de productos \
y precios al usuario, y espera confirmación explícita ("sí", "dale", "confirmo", "ok", etc.).
3. ANTES de llamar rappi_clear_cart: confirma con el usuario.
4. Usa store_type EXACTAMENTE como retorna rappi_list_stores (sensible a mayúsculas/minúsculas).
5. Si un tool retorna error 401 o contiene "auth_required": informa al usuario que debe \
re-registrarse en {AUTH_URL}
6. El checkout es por tienda. Si hay productos en múltiples tiendas, pregunta cuál quiere \
pagar primero.
7. Responde siempre en español, de forma concisa y amigable (estás en WhatsApp).
8. Usa emoji con moderación para estructurar la información (listas, confirmaciones, errores).
9. Cuando rappi_checkout retorne los links, dile al usuario que los recibirá en el mensaje siguiente \
— no los escribas tú en el texto, el sistema los enviará por separado.
10. Si el usuario dice "la tienda de antes" o "el mismo supermercado", infiere el store_id/store_type \
del contexto de la conversación."""


def _extract_text(content_blocks: list) -> str:
    """Extract plain text from Claude response content blocks."""
    parts = []
    for block in content_blocks:
        if hasattr(block, "text"):
            parts.append(block.text)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(block["text"])
    return "\n".join(parts).strip()


def _content_to_dict(content_blocks) -> list:
    """Serialize Claude response content blocks for DB storage."""
    result = []
    for block in content_blocks:
        if hasattr(block, "model_dump"):
            result.append(block.model_dump())
        elif isinstance(block, dict):
            result.append(block)
    return result


def _detect_store_context(content_blocks: list) -> tuple[int | None, str | None, str | None]:
    """
    Scan tool_use blocks for add_to_cart calls to extract the active store context.
    Returns (store_id, store_type, store_name) or (None, None, None).
    """
    for block in content_blocks:
        name = getattr(block, "name", None) or (block.get("name") if isinstance(block, dict) else None)
        inp = getattr(block, "input", None) or (block.get("input") if isinstance(block, dict) else {})
        if name in ("rappi_add_to_cart", "rappi_remove_from_cart", "rappi_clear_cart") and inp:
            return inp.get("store_id"), inp.get("store_type"), None
    return None, None, None


def _format_checkout_message(app_link: str, web_link: str) -> str:
    return (
        f"📱 *Abrir en la app Rappi:*\n{app_link}\n\n"
        f"🌐 *O en el navegador:*\n{web_link}"
    )


def run_agent(phone: str, user_message: str) -> str | list[str]:
    """
    Process one user message and return the bot reply.
    Returns a list of strings if multiple WhatsApp messages should be sent
    (e.g., text reply + checkout link).
    """
    sdk = anthropic.Anthropic()
    rappi = get_client(phone)

    if rappi is None:
        return (
            f"👋 Hola! No estás registrado aún.\n\n"
            f"Para conectar tu cuenta Rappi, abre este enlace desde tu teléfono:\n"
            f"{AUTH_URL}\n\n"
            f"Solo toma un minuto y lo necesitas hacer una sola vez. 🙌"
        )

    # Build messages list from DB history
    history = get_recent_history(phone, limit=HISTORY_KEEP)
    # Ensure history starts with a user message (never mid-round)
    while history and history[0]["role"] != "user":
        history = history[1:]

    messages = list(history)
    messages.append({"role": "user", "content": user_message})

    checkout_links: dict | None = None
    store_id_detected: int | None = None
    store_type_detected: str | None = None
    final_response = None

    for _round in range(MAX_TOOL_ROUNDS):
        try:
            response = sdk.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=messages,
            )
        except anthropic.APIError as e:
            return f"⚠️ Error al procesar tu mensaje. Intenta de nuevo en un momento. ({e})"

        messages.append({"role": "assistant", "content": _content_to_dict(response.content)})
        final_response = response

        # Detect store context from any tool calls in this round
        sid, stype, _ = _detect_store_context(response.content)
        if sid:
            store_id_detected, store_type_detected = sid, stype

        if response.stop_reason == "end_turn":
            break

        if response.stop_reason == "tool_use":
            tool_results = []

            # Separate read-only and write tools
            read_blocks = []
            write_blocks = []
            for block in response.content:
                if getattr(block, "type", None) != "tool_use":
                    continue
                if block.name in WRITE_TOOLS:
                    write_blocks.append(block)
                else:
                    read_blocks.append(block)

            # Execute read-only tools (safe to process together)
            for block in read_blocks:
                try:
                    result = dispatch_tool(block.name, block.input, rappi)
                except Exception as e:
                    result = {"error": str(e)}
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result, ensure_ascii=False),
                })

            # Execute write tools sequentially to avoid cache races
            for block in write_blocks:
                try:
                    result = dispatch_tool(block.name, block.input, rappi)
                    # Capture checkout links for separate WhatsApp message
                    if block.name == "rappi_checkout" and isinstance(result, dict):
                        checkout_links = result
                except Exception as e:
                    result = {"error": str(e)}
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result, ensure_ascii=False),
                })

            messages.append({"role": "user", "content": tool_results})
        else:
            break  # unexpected stop_reason

    # Persist conversation to DB
    append_message(phone, "user", user_message)
    if final_response:
        append_message(phone, "assistant", _content_to_dict(final_response.content))
        # Also persist intermediate tool rounds (everything between user and final assistant)
        # messages[len(history)+1:] contains all the intermediate turns
        for i, msg in enumerate(messages[len(history) + 1: -1]):  # skip first user + last assistant
            append_message(phone, msg["role"], msg["content"])
    trim_history(phone, keep=HISTORY_KEEP)

    # Persist store context if detected
    if store_id_detected and store_type_detected:
        set_session_state(phone, store_id_detected, store_type_detected, None)

    # Build reply
    reply_text = _extract_text(final_response.content) if final_response else ""
    if not reply_text:
        reply_text = "Lo siento, no pude procesar tu solicitud. Intenta de nuevo."

    if checkout_links:
        return [
            reply_text,
            _format_checkout_message(checkout_links["app_link"], checkout_links["web_link"]),
        ]

    return reply_text
