"""Production-ready FastAPI application example using cashcov.

Features demonstrated:
* CacheManager lifecycle integration via FastAPI lifespan
* Typed dependency injection with Annotated + Depends
* Per-endpoint @cached decorator
* Health-check endpoint that probes Redis connectivity
* Test overrides via app.dependency_overrides

Run with:
    cd cashcov-py
    uv run uvicorn examples.fastapi_app:app --reload

(Requires a Redis server on localhost:6379, or override REDIS_URL env var.)
"""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel

from cashcov import AsyncCacheHandler
from cashcov.ext.fastapi import CacheManager

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")

# ---------------------------------------------------------------------------
# Cache manager — one per application module.
# The dependency callable `cache_dep` MUST be module-level so that
# `app.dependency_overrides[cache_dep] = mock_fn` works in tests.
# ---------------------------------------------------------------------------

cache_manager = CacheManager(
    redis_url=REDIS_URL,
    prefix="example",
    ttl=300,
)

cache_dep = cache_manager.get_dependency()

# Convenience type alias — use this in route signatures for clean annotations.
CacheDep = Annotated[AsyncCacheHandler, Depends(cache_dep)]


# ---------------------------------------------------------------------------
# FastAPI app with lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Wire cashcov into the app lifecycle."""
    async with cache_manager.lifespan(app):
        yield


app = FastAPI(
    title="cashcov FastAPI Example",
    description="Redis-backed caching with background refresh and stampede protection.",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class Product(BaseModel):
    id: str
    name: str
    price: float
    in_stock: bool = True


class User(BaseModel):
    id: str
    name: str
    email: str


# ---------------------------------------------------------------------------
# Simulated data layer
# ---------------------------------------------------------------------------

_PRODUCTS = {
    "p1": Product(id="p1", name="Widget", price=9.99),
    "p2": Product(id="p2", name="Gadget", price=24.99),
}

_USERS = {
    "u1": User(id="u1", name="Alice", email="alice@example.com"),
    "u2": User(id="u2", name="Bob", email="bob@example.com"),
}


async def db_get_product(product_id: str) -> Product:
    """Simulated DB call (replace with your ORM / HTTP client)."""
    if product_id not in _PRODUCTS:
        raise HTTPException(status_code=404, detail=f"Product {product_id!r} not found")
    return _PRODUCTS[product_id]


async def db_get_user(user_id: str) -> User:
    if user_id not in _USERS:
        raise HTTPException(status_code=404, detail=f"User {user_id!r} not found")
    return _USERS[user_id]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/products/{product_id}", response_model=Product)
async def get_product(product_id: str, cache: CacheDep):
    """Fetch a product — cached for 5 minutes, refreshed in background."""

    async def generate() -> str:
        product = await db_get_product(product_id)
        return product.model_dump_json()

    result = await cache.get_or_refresh(f"product:{product_id}", generate)
    return Product.model_validate_json(result.value)


@app.get("/users/{user_id}", response_model=User)
async def get_user(user_id: str, cache: CacheDep):
    """Fetch a user — with per-call TTL override (10 minutes for users)."""

    async def generate() -> str:
        user = await db_get_user(user_id)
        return user.model_dump_json()

    result = await cache.get_or_refresh(f"user:{user_id}", generate, ttl=600)
    return User.model_validate_json(result.value)


@app.delete("/products/{product_id}/cache")
async def invalidate_product_cache(product_id: str, cache: CacheDep):
    """Manually invalidate a cached product (e.g. after an update)."""
    await cache.delete(f"product:{product_id}")
    return {"invalidated": f"product:{product_id}"}


@app.get("/health")
async def health(cache: CacheDep):
    """Health-check endpoint that probes Redis connectivity."""
    try:
        await cache._redis.ping()
        return {"status": "ok", "redis": "connected"}
    except Exception as exc:
        return {"status": "degraded", "redis": str(exc)}


# ---------------------------------------------------------------------------
# Optional: @cached decorator usage on a standalone function
# ---------------------------------------------------------------------------
# Note: for this to work cache_dep must be resolved first (i.e. inside a request).
# This pattern is more natural for service-layer code that receives the handler.


def make_report_fetcher(cache: AsyncCacheHandler) -> ...:
    """Factory that returns a cached report-fetcher tied to a specific handler."""

    @cache.cached(key_fn=lambda report_id: f"report:{report_id}", ttl=120)
    async def fetch_report(report_id: str) -> str:
        # Expensive aggregation ...
        return json.dumps({"report_id": report_id, "data": "..."})

    return fetch_report


@app.get("/reports/{report_id}")
async def get_report(report_id: str, cache: CacheDep):
    fetcher = make_report_fetcher(cache)
    return {"data": json.loads(await fetcher(report_id))}
