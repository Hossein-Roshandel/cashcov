"""typed_values.py — Working with rich Python types via JSON.

cashcov exchanges values as JSON strings.  This example shows a thin helper
pattern that handles serialisation and deserialisation so the rest of the
application works with plain Python objects.

Run (with Redis on localhost:6379):
    python examples/typed_values.py
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Callable, TypeVar

from cashcov import CacheHandler

REDIS_ADDR = "localhost:6379"

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Thin typed wrapper — keeps JSON handling in one place
# ---------------------------------------------------------------------------


class TypedCache:
    """Wraps CacheHandler to handle JSON encode/decode automatically."""

    def __init__(self, handler: CacheHandler) -> None:
        self._h = handler

    def get_or_refresh(
        self,
        key: str,
        generator: Callable[[str], Any],
    ) -> Any:
        """Return a deserialised Python object; call generator on a miss."""

        def _gen(k: str) -> str:
            return json.dumps(generator(k))

        return json.loads(self._h.get_or_refresh(key, _gen))

    def set(self, key: str, value: Any, ttl: int = 0) -> None:
        self._h.set(key, json.dumps(value), ttl)


# ---------------------------------------------------------------------------
# Example domain types
# ---------------------------------------------------------------------------


@dataclass
class Product:
    id: str
    name: str
    price: float
    in_stock: bool


def fetch_product(key: str) -> dict:
    """Simulate fetching a product from a remote API."""
    print(f"  [generator] loading product {key!r}")
    return asdict(Product(id=key, name="Widget Pro", price=29.99, in_stock=True))


def main() -> None:
    with CacheHandler(redis_addr=REDIS_ADDR, prefix="example:typed", ttl=120) as h:
        cache = TypedCache(h)

        print("Fetching product (miss — generator runs):")
        data = cache.get_or_refresh("product:42", fetch_product)
        product = Product(**data)
        print(f"  {product}")

        print("\nFetching product again (hit — generator skipped):")
        data = cache.get_or_refresh("product:42", fetch_product)
        product = Product(**data)
        print(f"  {product}")

        print("\nManually overwriting with a discounted price:")
        updated = asdict(Product(id="product:42", name="Widget Pro", price=19.99, in_stock=True))
        cache.set("product:42", updated, ttl=30)

        data = cache.get_or_refresh("product:42", fetch_product)
        print(f"  after set: {Product(**data)}")


if __name__ == "__main__":
    main()
