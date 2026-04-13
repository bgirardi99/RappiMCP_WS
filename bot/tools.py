"""
Claude API tool definitions and dispatcher.

TOOLS   — JSON schemas sent to claude in every request
dispatch_tool(name, args, client) — executes one tool call, returns JSON-serializable result

Cart-mutating tools (add, remove, clear) are identified in WRITE_TOOLS so the
agent loop can serialize them while parallelizing read-only calls.
"""

import json
from rappi_client import RappiClient

# Tools that mutate the cart — must NOT be run concurrently
WRITE_TOOLS = {"rappi_add_to_cart", "rappi_remove_from_cart", "rappi_clear_cart"}

TOOLS = [
    {
        "name": "rappi_list_addresses",
        "description": (
            "List all saved delivery addresses for this Rappi account. "
            "Returns id, tag, address, active (bool), lat, lng for each. "
            "Use rappi_set_address to switch to a different one."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "rappi_set_address",
        "description": (
            "Switch the active delivery address by its numeric ID (from rappi_list_addresses). "
            "The change affects all subsequent store/product searches. "
            "Client-side only — no server call needed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "address_id": {
                    "type": "integer",
                    "description": "Numeric address ID from rappi_list_addresses",
                }
            },
            "required": ["address_id"],
        },
    },
    {
        "name": "rappi_list_stores",
        "description": (
            "List stores available for delivery, filtered by category. "
            "Returns store_id, name, store_type, eta, shipping_cost. "
            "Use store_id with rappi_search_products and store_type with cart tools."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "description": (
                        'Store category. Common values: "market" (supermarkets, default), '
                        '"restaurant", "farma" (pharmacies), "licores", "express-big".'
                    ),
                    "default": "market",
                },
                "query": {
                    "type": "string",
                    "description": "Optional name filter (e.g. 'lider', 'unimarc').",
                    "default": "",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results to return.",
                    "default": 20,
                },
            },
            "required": [],
        },
    },
    {
        "name": "rappi_get_store",
        "description": "Look up store metadata by numeric store_id. Returns store_type needed for cart tools.",
        "input_schema": {
            "type": "object",
            "properties": {
                "store_id": {"type": "integer", "description": "Numeric store ID"}
            },
            "required": ["store_id"],
        },
    },
    {
        "name": "rappi_search_products",
        "description": (
            "Search products within a Rappi store. Always search in Spanish. "
            "Returns composite_id, name, trademark, price, real_price, discount, "
            "quantity, unit_type, sale_type, in_stock. "
            "composite_id and sale_type are required for rappi_add_to_cart."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "store_id": {"type": "integer", "description": "Numeric store ID"},
                "query": {"type": "string", "description": "Search term in Spanish"},
                "size": {
                    "type": "integer",
                    "description": "Max results (default 8, max 40).",
                    "default": 8,
                },
                "offset": {
                    "type": "integer",
                    "description": "Pagination offset.",
                    "default": 0,
                },
            },
            "required": ["store_id", "query"],
        },
    },
    {
        "name": "rappi_get_cart",
        "description": (
            "Return the current cart contents for a store type (e.g. 'lider'). "
            "Always call this before rappi_checkout to show the user a summary."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "store_type": {
                    "type": "string",
                    "description": "Store type slug, exactly as returned by rappi_list_stores.",
                }
            },
            "required": ["store_type"],
        },
    },
    {
        "name": "rappi_add_to_cart",
        "description": (
            "Add an item to the cart (or increment its quantity if already present). "
            "Use composite_id and sale_type from rappi_search_products."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "store_id": {"type": "integer", "description": "Numeric store ID"},
                "store_type": {
                    "type": "string",
                    "description": "Store type slug (e.g. 'lider'). Must match exactly.",
                },
                "composite_id": {
                    "type": "string",
                    "description": "Product composite ID from rappi_search_products.",
                },
                "units": {"type": "integer", "description": "Number of units to add."},
                "sale_type": {
                    "type": "string",
                    "description": '"U" (unit) or "WP" (by weight), from search results.',
                },
            },
            "required": ["store_id", "store_type", "composite_id", "units", "sale_type"],
        },
    },
    {
        "name": "rappi_remove_from_cart",
        "description": "Remove an item from the cart by its composite_id.",
        "input_schema": {
            "type": "object",
            "properties": {
                "store_id": {"type": "integer"},
                "store_type": {"type": "string"},
                "composite_id": {"type": "string", "description": "Product composite ID to remove."},
            },
            "required": ["store_id", "store_type", "composite_id"],
        },
    },
    {
        "name": "rappi_clear_cart",
        "description": "Empty the entire cart for a store. Ask the user for confirmation before calling this.",
        "input_schema": {
            "type": "object",
            "properties": {
                "store_id": {"type": "integer"},
                "store_type": {"type": "string"},
            },
            "required": ["store_id", "store_type"],
        },
    },
    {
        "name": "rappi_checkout",
        "description": (
            "Prepare checkout for the user. "
            "IMPORTANT: Always call rappi_get_cart first and show the summary, "
            "then wait for explicit user confirmation before calling this. "
            "Returns deep links to open the Rappi app directly at checkout."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "store_type": {
                    "type": "string",
                    "description": "Store type slug (e.g. 'lider').",
                }
            },
            "required": ["store_type"],
        },
    },
]


def _normalize_store(s: dict) -> dict:
    st = s.get("store_type")
    return {
        "store_id": s.get("store_id"),
        "name": s.get("store_name") or s.get("name"),
        "store_type": st.get("id") if isinstance(st, dict) else st,
        "eta": s.get("eta"),
        "shipping_cost": s.get("shipping_cost"),
        "rating": s.get("store_rating_score"),
    }


def dispatch_tool(name: str, args: dict, client: RappiClient) -> object:
    """
    Execute one tool call synchronously and return a JSON-serializable result.
    Cart-mutating tools must be called sequentially (see WRITE_TOOLS).
    """
    match name:
        case "rappi_list_addresses":
            addresses = client.list_addresses()
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

        case "rappi_set_address":
            addr = client.set_active_address(args["address_id"])
            return {
                "status": "active",
                "id": addr.get("id"),
                "tag": addr.get("tag") or addr.get("title"),
                "address": addr.get("subtitle") or addr.get("address"),
            }

        case "rappi_list_stores":
            stores = client.list_stores(
                category=args.get("category", "market"),
                query=args.get("query", ""),
                limit=args.get("limit", 20),
            )
            return [_normalize_store(s) for s in stores]

        case "rappi_get_store":
            store = client.get_store(args["store_id"])
            return {
                "store_id": store["store_id"],
                "name": store["name"],
                "store_type": store["store_type"]["id"] if isinstance(store.get("store_type"), dict) else store.get("store_type"),
            }

        case "rappi_search_products":
            products = client.search_products(
                args["store_id"],
                args["query"],
                args.get("size", 8),
                args.get("offset", 0),
            )
            return [
                {
                    "composite_id": p["id"],
                    "name": p["name"],
                    "trademark": p.get("trademark", ""),
                    "price": p["price"],
                    "real_price": p.get("real_price", p["price"]),
                    "discount": round(p.get("discount", 0.0) * 100),
                    "quantity": p.get("quantity"),
                    "unit_type": p.get("unit_type", ""),
                    "sale_type": p.get("sale_type", "U"),
                    "in_stock": p.get("in_stock", True),
                }
                for p in products
                if p.get("in_stock", True)
            ]

        case "rappi_get_cart":
            return client.get_cart(args["store_type"])

        case "rappi_add_to_cart":
            result = client.add_to_cart(
                args["store_id"],
                args["store_type"],
                args["composite_id"],
                args["units"],
                args["sale_type"],
            )
            # Return a compact summary instead of the full response payload
            stores = result.get("stores", [])
            if stores:
                store = stores[0]
                return {
                    "status": "ok",
                    "product_total": store.get("product_total"),
                    "items": [
                        {"name": p["name"], "units": p["units"], "price": p.get("real_price", p["price"])}
                        for p in store.get("products", [])
                    ],
                }
            return {"status": "ok"}

        case "rappi_remove_from_cart":
            client.remove_from_cart(args["store_id"], args["store_type"], args["composite_id"])
            return {"status": "ok"}

        case "rappi_clear_cart":
            client.clear_cart(args["store_id"], args["store_type"])
            return {"status": "ok"}

        case "rappi_checkout":
            store_type = args["store_type"]
            return {
                "app_link": f"rappi://checkout/{store_type}",
                "web_link": f"https://www.rappi.cl/checkout/{store_type}",
            }

        case _:
            raise ValueError(f"Unknown tool: {name}")
