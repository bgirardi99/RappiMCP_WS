"""
Rappi MCP Server

Tools:
  rappi_auth               — extract tokens from running Chrome and write .env
  rappi_list_addresses     — list saved delivery addresses
  rappi_set_address        — switch active delivery address (or open Chrome to add a new one)
  rappi_list_stores        — list/search stores by category (market, restaurant, farma, …)
  rappi_get_store          — look up store metadata by store_id
  rappi_search_products    — search products within a store
  rappi_get_cart           — read current cart for a store type
  rappi_add_to_cart        — add (or increment) an item in the cart
  rappi_remove_from_cart   — remove an item from the cart
  rappi_clear_cart         — empty the cart

Config (via .env or environment):
  RAPPI_ACCESS_TOKEN
  RAPPI_REFRESH_TOKEN
  RAPPI_DEVICE_ID
  RAPPI_USER_ID
"""

import base64
import importlib
import json
import os
import re
import subprocess
import sys
import time

import requests
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from rappi_client import RappiClient, BASE_URL

load_dotenv()

mcp = FastMCP("rappi")

_client: RappiClient | None = None


def get_client() -> RappiClient:
    global _client
    if _client is None:
        _client = RappiClient(
            access_token=os.environ["RAPPI_ACCESS_TOKEN"],
            refresh_token=os.environ["RAPPI_REFRESH_TOKEN"],
            device_id=os.environ["RAPPI_DEVICE_ID"],
            user_id=int(os.environ["RAPPI_USER_ID"]),
        )
    return _client


# ------------------------------------------------------------------ #
#  Auth helper — extracts tokens from a running Chrome via CDP        #
# ------------------------------------------------------------------ #

ENV_PATH = os.path.join(os.path.dirname(__file__), ".env")
CDP_PORT = 9223  # dedicated port so we don't clash with other sessions


def _find_chrome() -> str:
    candidates = [
        # Windows
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        # macOS
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        # Linux
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    raise FileNotFoundError(
        "Chrome not found. Install Google Chrome or set the CHROME_PATH "
        "environment variable to your Chrome executable path."
    )


def _launch_chrome_with_cdp() -> subprocess.Popen:
    chrome = os.environ.get("CHROME_PATH") or _find_chrome()
    import tempfile
    profile_dir = os.path.join(tempfile.gettempdir(), "rappi_auth_profile")
    return subprocess.Popen([
        chrome,
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={profile_dir}",
        "--remote-allow-origins=*",
        "--no-first-run",
        "--no-default-browser-check",
        "https://www.rappi.cl",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _cdp_get(path: str) -> dict | list:
    return requests.get(f"http://127.0.0.1:{CDP_PORT}{path}", timeout=5).json()


def _cdp_eval(ws_url: str, expression: str):
    import websocket
    ws = websocket.create_connection(ws_url, timeout=10, origin="http://127.0.0.1")
    ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                        "params": {"expression": expression, "returnByValue": True}}))
    result = json.loads(ws.recv())
    ws.close()
    return result.get("result", {}).get("result", {}).get("value")


def _get_rappi_ws(navigate: bool = False) -> str:
    """Return the WebSocket debugger URL for the rappi.cl tab, navigating if needed."""
    import websocket

    tabs = _cdp_get("/json")
    tab = next(
        (t for t in tabs if "rappi.cl" in t.get("url", "") and "sw.js" not in t.get("url", "")),
        None,
    )
    if tab:
        return tab["webSocketDebuggerUrl"]

    # No rappi.cl tab — navigate the first available tab there
    tab = next((t for t in tabs if t.get("webSocketDebuggerUrl")), None)
    if not tab:
        raise RuntimeError("No usable Chrome tab found.")
    ws = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=10, origin="http://127.0.0.1")
    ws.send(json.dumps({"id": 1, "method": "Page.navigate", "params": {"url": "https://www.rappi.cl"}}))
    ws.recv()
    ws.close()
    time.sleep(3)
    tabs = _cdp_get("/json")
    tab = next(
        (t for t in tabs if "rappi.cl" in t.get("url", "") and "sw.js" not in t.get("url", "")),
        tabs[0],
    )
    return tab["webSocketDebuggerUrl"]


_COOKIE_SCRIPT = r"""
(function() {
  const c = document.cookie;
  let userId = null, deviceId = null, refreshToken = null;
  try { userId = JSON.parse(atob(c.match(/rappi\.data=([^;]+)/)[1])).id; } catch(e) {}
  try { deviceId = c.match(/deviceid=([^;]+)/)[1]; } catch(e) {}
  try {
    const raw = c.match(/rappi_refresh_token=([^;]+)/)[1];
    refreshToken = raw.startsWith('ft.') ? raw : atob(raw);
  } catch(e) {}
  return JSON.stringify({ userId, deviceId, refreshToken });
})()
"""


def _poll_for_login(ws_url: str, timeout: int = 120) -> dict:
    """
    Poll rappi.cl cookies every 2 s until both refreshToken and userId appear.
    Raises TimeoutError if not logged in within `timeout` seconds.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        raw = _cdp_eval(ws_url, _COOKIE_SCRIPT)
        data = json.loads(raw) if raw else {}
        if data.get("refreshToken") and data.get("userId"):
            return data
        time.sleep(2)
    raise TimeoutError(f"Rappi login not detected within {timeout} s.")


def _exchange_refresh_token(refresh_token: str) -> tuple[str, str]:
    """POST to Rappi refresh endpoint. Returns (new_access_token, new_refresh_token)."""
    resp = requests.post(
        f"{BASE_URL}/api/rocket/refresh-token",
        json={"refresh_token": refresh_token},
        headers={"Content-Type": "application/json"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["access_token"], data.get("refresh_token", refresh_token)


def _extract_tokens_via_cdp(wait_for_login: bool = False) -> dict:
    """Connect to Chrome CDP, wait for login if requested, exchange tokens."""
    ws_url = _get_rappi_ws()

    if wait_for_login:
        cookies = _poll_for_login(ws_url, timeout=120)
    else:
        raw = _cdp_eval(ws_url, _COOKIE_SCRIPT)
        cookies = json.loads(raw) if raw else {}

    refresh_token = cookies.get("refreshToken") or ""
    if not refresh_token:
        raise RuntimeError("No refresh token found. Make sure you are logged into rappi.cl.")

    access_token, refresh_token = _exchange_refresh_token(refresh_token)

    return {
        "RAPPI_ACCESS_TOKEN": access_token,
        "RAPPI_REFRESH_TOKEN": refresh_token,
        "RAPPI_DEVICE_ID": cookies.get("deviceId") or "",
        "RAPPI_USER_ID": str(cookies.get("userId") or ""),
    }


def _write_env(values: dict) -> None:
    existing = {}
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    existing[k.strip()] = v.strip()
    existing.update({k: v for k, v in values.items() if v})
    with open(ENV_PATH, "w") as f:
        for k, v in existing.items():
            f.write(f"{k}={v}\n")


# ------------------------------------------------------------------ #
#  Tools                                                               #
# ------------------------------------------------------------------ #


@mcp.tool()
def rappi_reload() -> dict:
    """Reload tokens from .env and rappi_client code into the running server (no Chrome needed)."""
    global _client
    import rappi_client
    importlib.reload(rappi_client)
    # Re-import symbols that were bound at import time
    global RappiClient, BASE_URL
    from rappi_client import RappiClient, BASE_URL  # noqa: F811
    load_dotenv(ENV_PATH, override=True)
    _client = None
    return {"status": "reloaded", "user_id": os.environ.get("RAPPI_USER_ID", "?")}


@mcp.tool()
def rappi_auth(use_existing_chrome: bool = True) -> dict:
    """
    Extract Rappi credentials from Chrome and save them to .env.

    Flow:
      - use_existing_chrome=True  (default): attach to a Chrome already running
        with --remote-debugging-port=9223. Reads cookies immediately — user must
        already be logged into rappi.cl.
      - use_existing_chrome=False: launches a dedicated Chrome window pointed at
        rappi.cl, then waits up to 2 minutes for the user to log in. Once the
        auth cookies appear, exchanges the refresh token for a fresh access token
        and writes everything to .env automatically — no second call needed.

    Returns the extracted values (tokens truncated for display).
    """
    global _client

    cdp_alive = False
    try:
        requests.get(f"http://127.0.0.1:{CDP_PORT}/json", timeout=2)
        cdp_alive = True
    except Exception:
        pass

    if not cdp_alive:
        if use_existing_chrome:
            return {
                "error": (
                    f"Chrome is not listening on CDP port {CDP_PORT}. "
                    "Restart Chrome with --remote-debugging-port=9223, or call "
                    "rappi_auth(use_existing_chrome=False) to launch a fresh window."
                )
            }
        _launch_chrome_with_cdp()
        # Wait for Chrome to start
        for _ in range(10):
            time.sleep(1)
            try:
                requests.get(f"http://127.0.0.1:{CDP_PORT}/json", timeout=1)
                break
            except Exception:
                pass

    try:
        # When we launched Chrome ourselves, wait for the user to log in.
        # When attaching to an existing Chrome, read cookies immediately.
        wait = not use_existing_chrome
        values = _extract_tokens_via_cdp(wait_for_login=wait)
    except TimeoutError:
        return {"error": "Login timeout (2 min). Please log into rappi.cl and try again."}
    except Exception as e:
        return {"error": str(e)}

    missing = [k for k, v in values.items() if not v]
    if missing:
        return {"error": f"Could not extract: {missing}. Make sure you are logged into rappi.cl."}

    _write_env(values)
    load_dotenv(ENV_PATH, override=True)
    _client = None  # force re-init with new tokens

    return {k: (v[:30] + "...") if len(v) > 30 else v for k, v in values.items()}


def _normalize_store(s: dict) -> dict:
    st = s.get("store_type")
    # Restaurant stores (from unified-search) use "store_name" and a plain string store_type.
    # Market stores use "name" and a {"id": ..., "name": ...} dict for store_type.
    return {
        "store_id": s.get("store_id"),
        "name": s.get("store_name") or s.get("name"),
        "store_type": st.get("id") if isinstance(st, dict) else st,
        "lat": s.get("lat"),
        "lng": s.get("lng"),
        "eta": s.get("eta"),
        "shipping_cost": s.get("shipping_cost"),
        "rating": s.get("store_rating_score"),
    }


@mcp.tool()
def rappi_list_addresses() -> list[dict]:
    """
    List all saved delivery addresses for this Rappi account.
    Use rappi_set_address to switch to a different one.
    Returns id, tag, address, active, lat, lng for each.
    """
    addresses = get_client().list_addresses()
    return [
        {
            "id": a.get("id"),
            "tag": a.get("tag") or a.get("title"),
            "address": a.get("subtitle") or a.get("address"),
            "active": a.get("active", False),
            "lat": a.get("lat"),
            "lng": a.get("lng"),
        }
        for a in addresses
    ]


@mcp.tool()
def rappi_set_address(address_id: int | None = None) -> dict:
    """
    Switch the active delivery address by its ID (from rappi_list_addresses).
    If address_id is None, opens Rappi address settings in Chrome so you can add a new one.
    The change is in-memory and affects all subsequent store/product searches.

    Args:
        address_id: Numeric address ID from rappi_list_addresses, or None to open Chrome.
    """
    if address_id is None:
        try:
            ws_url = _get_rappi_ws()
        except Exception:
            _launch_chrome_with_cdp()
            time.sleep(2)
            ws_url = _get_rappi_ws()
        _cdp_eval(ws_url, "window.location.href = 'https://www.rappi.cl/mi-cuenta/direcciones';")
        return {
            "status": "opened",
            "url": "https://www.rappi.cl/mi-cuenta/direcciones",
            "message": "Opened Rappi address settings in Chrome. Add your address there, then call rappi_list_addresses() and rappi_set_address(id) to select it.",
        }

    addr = get_client().set_active_address(address_id)
    return {
        "status": "active",
        "id": addr.get("id"),
        "tag": addr.get("tag") or addr.get("title"),
        "address": addr.get("subtitle") or addr.get("address"),
        "lat": addr.get("lat"),
        "lng": addr.get("lng"),
    }


@mcp.tool()
def rappi_list_stores(
    category: str = "market", query: str = "", limit: int = 50
) -> list[dict]:
    """
    List stores available for delivery, filtered by category.

    Args:
        category: Store category to list. Common values:
                    "market"      — supermarkets (default)
                    "restaurant"  — restaurants
                    "farma"       — pharmacies
                    "licores"     — liquor stores
                    "express-big" — express / convenience stores
                  Any other Rappi store_type slug is also accepted.
        query:    Optional name filter (case-insensitive).
        limit:    Max results to return (default 50).

    Returns a list of stores with store_id, name, store_type, lat, lng.
    Use store_id with rappi_search_products and store_type with cart tools.
    """
    stores = get_client().list_stores(category=category, query=query, limit=limit)
    return [_normalize_store(s) for s in stores]


@mcp.tool()
def rappi_get_store(store_id: int) -> dict:
    """
    Look up store metadata by numeric store_id.

    Returns store_id, name, lat, lng, store_type (id + name).
    The store_type.id (e.g. "lider") is required by other tools.
    """
    store = get_client().get_store(store_id)
    return {
        "store_id": store["store_id"],
        "name": store["name"],
        "lat": store["lat"],
        "lng": store["lng"],
        "store_type": store["store_type"]["id"],
        "store_type_name": store["store_type"]["name"],
    }


@mcp.tool()
def rappi_search_products(
    store_id: int,
    query: str,
    size: int = 40,
    offset: int = 0,
) -> list[dict]:
    """
    Search products within a Rappi store.

    Args:
        store_id: Numeric store ID (e.g. 900024799 for Lider).
        query:    Search term in Spanish.
        size:     Max results to return (default 40, max 40).
        offset:   Pagination offset (increment by `size` to get next page).

    Returns a list of products with:
        composite_id, name, trademark, price, real_price, discount,
        quantity, unit_type, sale_type, in_stock.
    """
    products = get_client().search_products(store_id, query, size, offset)
    return [
        {
            "composite_id": p["id"],
            "name": p["name"],
            "trademark": p.get("trademark", ""),
            "price": p["price"],
            "real_price": p.get("real_price", p["price"]),
            "discount": p.get("discount", 0.0),
            "quantity": p.get("quantity"),
            "unit_type": p.get("unit_type", ""),
            "sale_type": p.get("sale_type", "U"),
            "in_stock": p.get("in_stock", True),
        }
        for p in products
    ]


@mcp.tool()
def rappi_get_cart(store_type: str) -> list[dict]:
    """
    Return the current cart contents for a store type (e.g. "lider").

    Returns a list of cart items with composite_id, units, sale_type, name, price.
    """
    return get_client().get_cart(store_type)


@mcp.tool()
def rappi_add_to_cart(
    store_id: int,
    store_type: str,
    composite_id: str,
    units: int,
    sale_type: str,
) -> dict:
    """
    Add an item to the cart (or increment its quantity if already present).

    Args:
        store_id:     Numeric store ID (e.g. 900024799).
        store_type:   Store type slug (e.g. "lider").
        composite_id: Product composite ID from rappi_search_products.
        units:        Number of units to add.
        sale_type:    "U" (unit) or "WP" (by weight), from search results.

    Returns the updated cart response from Rappi.
    """
    return get_client().add_to_cart(store_id, store_type, composite_id, units, sale_type)


@mcp.tool()
def rappi_remove_from_cart(
    store_id: int,
    store_type: str,
    composite_id: str,
) -> dict:
    """
    Remove an item from the cart by its composite_id.

    Args:
        store_id:     Numeric store ID (e.g. 900024799).
        store_type:   Store type slug (e.g. "lider").
        composite_id: Product composite ID to remove.
    """
    return get_client().remove_from_cart(store_id, store_type, composite_id)


@mcp.tool()
def rappi_clear_cart(store_id: int, store_type: str) -> dict:
    """
    Empty the entire cart for a store.

    Args:
        store_id:   Numeric store ID (e.g. 900024799).
        store_type: Store type slug (e.g. "lider").
    """
    return get_client().clear_cart(store_id, store_type)


@mcp.tool()
def rappi_checkout(store_type: str) -> str:
    """
    Open the Rappi checkout page in Chrome so the user can complete payment.

    Navigates Chrome directly to https://www.rappi.cl/checkout/{store_type}.
    Each store has its own checkout URL — pass the same store_type used in
    rappi_add_to_cart. The user must finish the order manually.

    Args:
        store_type: Store type slug from rappi_list_stores or rappi_add_to_cart
                    (e.g. "turbo_rappidrinks_nc", "lider", "expresslider").
    """
    checkout_url = f"https://www.rappi.cl/checkout/{store_type}"
    try:
        ws_url = _get_rappi_ws()
    except Exception:
        _launch_chrome_with_cdp()
        time.sleep(2)
        ws_url = _get_rappi_ws()

    _cdp_eval(ws_url, f"window.location.href = '{checkout_url}';")
    return (
        f"Chrome has been navigated to the Rappi checkout page ({checkout_url}). "
        "Please complete your payment there."
    )


def main():
    mcp.run()


if __name__ == "__main__":
    main()
