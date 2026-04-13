"""
Rappi API client with automatic token refresh.

Auth flow (from reverse engineering notes):
  - Every API response may include `x-refresh-token: true`
  - On that header: POST /api/rocket/refresh-token with current refresh_token
  - Both tokens rotate — store the new pair immediately
  - Retry the original request with the new access_token
"""

import time
import uuid
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv(Path.home() / ".rappi-mcp" / ".env")

BASE_URL = "https://services.rappi.cl"
APP_VERSION = "web_v1.220.2"


class RappiClient:
    def __init__(
        self,
        access_token: str,
        refresh_token: str,
        device_id: str,
        user_id: int,
    ):
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.device_id = device_id
        self.user_id = user_id
        self.session = requests.Session()
        # In-memory cache keyed by store_type.
        # The v1/all/get endpoint doesn't return web-API carts, so we maintain
        # our own authoritative state updated after every set_cart call.
        self._cart_cache: dict[str, list[dict]] = {}
        # Tracks the last store_id used per store_type to detect store switches.
        self._last_store_id: dict[str, int] = {}
        # Active address override (client-side only, mirrors Rappi web app behavior).
        self._active_address: dict | None = None

    # ------------------------------------------------------------------ #
    #  Internal helpers                                                    #
    # ------------------------------------------------------------------ #

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
            "app-version": APP_VERSION,
            "deviceid": self.device_id,
            "accept-language": "es-CL",
            "needAppsFlyerId": "false",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "*",
        }

    def _do_refresh(self) -> None:
        resp = self.session.post(
            f"{BASE_URL}/api/rocket/refresh-token",
            json={"refresh_token": self.refresh_token},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        self.access_token = data["access_token"]
        self.refresh_token = data["refresh_token"]

    def _request(self, method: str, path: str, **kwargs) -> Any:
        """Make a request; refresh token once if the server asks."""
        kwargs.setdefault("headers", self._headers())
        kwargs.setdefault("timeout", 20)

        resp = self.session.request(method, f"{BASE_URL}{path}", **kwargs)

        if resp.headers.get("x-refresh-token") == "true":
            self._do_refresh()
            kwargs["headers"] = self._headers()
            resp = self.session.request(method, f"{BASE_URL}{path}", **kwargs)

        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------ #
    #  Public API methods                                                  #
    # ------------------------------------------------------------------ #

    def get_active_address(self) -> dict | None:
        """Return the active delivery address (with lat/lng) from the user's saved addresses."""
        if self._active_address is not None:
            return self._active_address
        data = self._request("GET", "/api/ms/users-address/addresses")
        addresses = data.get("addresses", [])
        for addr in addresses:
            if addr.get("active"):
                return addr
        return addresses[0] if addresses else None

    def list_addresses(self) -> list[dict]:
        """Return all saved delivery addresses."""
        data = self._request("GET", "/api/ms/users-address/addresses")
        return data.get("addresses", [])

    def set_active_address(self, address_id: int) -> dict:
        """Select a saved address by ID (client-side only — mirrors Rappi web app behavior)."""
        addresses = self.list_addresses()
        for addr in addresses:
            if addr.get("id") == address_id:
                self._active_address = addr
                return addr
        raise ValueError(f"Address {address_id} not found")

    def list_stores(
        self, category: str = "market", query: str = "", limit: int = 50
    ) -> list[dict]:
        """
        List stores available for delivery by category.

        category: Rappi store category — "market", "restaurant", "farma",
                  "licores", "express-big", etc.
        """
        if category == "restaurant":
            # The stores-router endpoint doesn't work for restaurants;
            # use the unified-search endpoint which requires explicit lat/lng.
            addr = self.get_active_address()
            if not addr:
                return []
            data = self._request(
                "POST",
                "/api/pns-global-search-api/v1/unified-search?is_prime=true&unlimited_shipping=true",
                json={
                    "tiered_stores": [],
                    "lat": addr["lat"],
                    "lng": addr["lng"],
                    "query": query or "pizza",
                    "options": {"parent_store_type": "restaurant", "vertical": "restaurants"},
                },
            )
            return data.get("stores", [])[:limit]

        addr = self.get_active_address()
        if not addr:
            return []
        data = self._request(
            "POST",
            "/api/pns-global-search-api/v1/unified-search?is_prime=true&unlimited_shipping=true",
            json={
                "tiered_stores": [],
                "lat": addr["lat"],
                "lng": addr["lng"],
                "query": query or "pollo",
                "options": {"parent_store_type": category, "vertical": "cpgs"},
            },
        )
        return data.get("stores", [])[:limit]

    def get_store(self, store_id: int) -> dict:
        """Return store metadata for a given store_id."""
        data = self._request(
            "POST",
            "/api/web-gateway/web/stores-router/available/stores/with-store-types/",
            json={"store_ids": [store_id]},
        )
        return data[0]

    def search_products(
        self, store_id: int, query: str, size: int = 40, offset: int = 0
    ) -> list[dict]:
        """Search products within a store. Returns list of product objects.
        Falls back to unified-search for restaurants (CPGS endpoint returns 404 for them)."""
        try:
            data = self._request(
                "POST",
                f"/api/cpgs/search/v2/store/{store_id}/products",
                json={"from": offset, "query": query, "size": size, "attributes": []},
            )
            return data.get("products", [])
        except Exception:
            pass

        # CPGS failed — this is a restaurant store; use unified-search
        addr = self.get_active_address()
        if not addr:
            return []
        data = self._request(
            "POST",
            "/api/pns-global-search-api/v1/unified-search?is_prime=true&unlimited_shipping=true",
            json={
                "tiered_stores": [],
                "lat": addr["lat"],
                "lng": addr["lng"],
                "query": query,
                "options": {"parent_store_type": "restaurant", "vertical": "restaurants"},
            },
        )
        for store in data.get("stores", []):
            if store.get("store_id") == store_id:
                return store.get("products", [])
        return []

    def get_cart(self, store_type: str) -> list[dict]:
        """Return all products currently in the cart for a store type.

        Tries the v1/all/get API first; falls back to the in-memory cache
        populated by set_cart (the v1 endpoint doesn't return web-API carts).
        """
        try:
            data = self._request(
                "POST",
                "/api/ms/shopping-cart/v1/all/get",
                json={},
            )
            # API returns a list of store objects directly (not a {"stores": [...]} wrapper)
            stores = data if isinstance(data, list) else data.get("stores", [])
            for store in stores:
                st = store.get("store_type") or store.get("type") or ""
                if st == store_type:
                    products = store.get("products", [])
                    if products:
                        # Keep cache in sync with live data
                        self._cart_cache[store_type] = [
                            {"id": p["id"], "units": p["units"], "sale_type": p.get("sale_type", "U")}
                            for p in products
                        ]
                        return self._cart_cache[store_type]
        except Exception:
            pass
        # Fall back to in-memory cache (populated by set_cart)
        return list(self._cart_cache.get(store_type, []))

    def set_cart(
        self,
        store_id: int,
        store_type: str,
        products: list[dict],
    ) -> dict:
        """
        Replace the entire cart for a store (full PUT, not a delta).

        products: list of {"id": composite_id, "units": int, "sale_type": str}
        """
        body = [
            {
                "id": store_id,
                "products": [
                    {**p, "toppings": p.get("toppings", [])} for p in products
                ],
                "vendor": {
                    "id": f"{self.user_id}_{int(time.time() * 1000)}",
                    "type": "rappi",
                    "flow_type": "rappi-web",
                    "sideBarSections": ["products"],
                },
            }
        ]
        result = self._request(
            "PUT",
            f"/api/ms/shopping-cart/v2/{store_type}/store",
            json=body,
        )
        if isinstance(result, list):
            result = {"stores": result}

        # Update in-memory cache from the confirmed server response
        for store in result.get("stores", []):
            if store.get("type") == store_type:
                self._cart_cache[store_type] = [
                    {"id": p["id"], "units": p["units"], "sale_type": p.get("sale_type", "U")}
                    for p in store.get("products", [])
                ]
                break
        else:
            # Response didn't include matching store — cache what we sent
            self._cart_cache[store_type] = [
                {"id": p["id"], "units": p["units"], "sale_type": p.get("sale_type", "U")}
                for p in products
            ]

        return result

    def add_to_cart(
        self,
        store_id: int,
        store_type: str,
        composite_id: str,
        units: int,
        sale_type: str,
    ) -> dict:
        """Read current cart, append item, write back."""
        # Auto-clear cache when switching to a different store_id under the same store_type.
        if self._last_store_id.get(store_type) != store_id:
            self._cart_cache.pop(store_type, None)
            self._last_store_id[store_type] = store_id
        current = self.get_cart(store_type)
        # Update quantity if already in cart, otherwise append
        for p in current:
            if p["id"] == composite_id or p.get("composite_id") == composite_id:
                p["units"] = p.get("units", 0) + units
                break
        else:
            current.append(
                {"id": composite_id, "units": units, "sale_type": sale_type}
            )
        return self.set_cart(store_id, store_type, current)

    def remove_from_cart(
        self, store_id: int, store_type: str, composite_id: str
    ) -> dict:
        """Read current cart, remove item, write back."""
        current = self.get_cart(store_type)
        current = [
            p
            for p in current
            if p.get("id") != composite_id and p.get("composite_id") != composite_id
        ]
        return self.set_cart(store_id, store_type, current)

    def clear_cart(self, store_id: int, store_type: str) -> dict:
        """Empty the cart for a store."""
        return self.set_cart(store_id, store_type, [])
