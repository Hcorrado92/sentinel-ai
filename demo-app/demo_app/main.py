"""Demo shop API with three intentional faults (for Sentinel AI demonstrations).

Faults (active only when ``DEMO_FAULTS_ENABLED`` is true; never in production):

1. ``GET /users/{user_id}/profile``: a user without the optional ``nickname``
   (``None``) has it used like a string (``.upper()``) -> unhandled
   ``AttributeError`` -> 500.
2. ``POST /orders``: ``quantity`` may arrive as a string and is converted with
   ``int()`` without validation; a non-numeric value raises an unhandled
   ``ValueError`` -> 500 instead of a 422.
3. ``GET /orders/{order_id}/total?coupon=...``: a fixed-amount coupon larger
   than the subtotal yields a negative total, which raises the business
   exception ``DiscountError`` -> 500.

When the faults are disabled the endpoints follow the *correct behaviour*
(not a 503): the profile falls back to the user's name, a non-numeric quantity
returns 422, and the discounted total is clamped at zero.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from demo_app.config import DemoSettings, load_settings
from demo_app.sentinel_client import CORRELATION_HEADER, SentinelReporter

USERS: dict[int, dict[str, str | None]] = {
    1: {"name": "Ada Example", "email": "ada@example.test", "nickname": "ada"},
    2: {"name": "Bruno Sample", "email": "bruno@example.test", "nickname": None},
}

PRODUCTS: dict[str, float] = {"widget": 10.0, "gadget": 25.0}

# Coupon code -> (kind, value). "percent" discounts a percentage of the
# subtotal; "fixed" subtracts a fixed amount.
COUPONS: dict[str, tuple[str, float]] = {
    "SAVE10": ("percent", 10.0),
    "FIVEOFF": ("fixed", 5.0),
    "BIGDEAL": ("fixed", 500.0),
}


class DiscountError(Exception):
    """Business rule violation while applying a discount."""


class OrderIn(BaseModel):
    product: str
    quantity: int | str


def _display_name(user: dict[str, str | None], faults_enabled: bool) -> str:
    if faults_enabled:
        # FAULT 1: nickname is optional and may be None.
        return user["nickname"].upper()  # type: ignore[union-attr]
    return (user["nickname"] or user["name"] or "").upper()


def _parse_quantity(raw: int | str, faults_enabled: bool) -> int:
    if faults_enabled:
        # FAULT 2: unvalidated conversion; ValueError escapes as a 500.
        quantity = int(raw)
    else:
        try:
            quantity = int(raw)
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="quantity must be an integer")
    if quantity <= 0:
        raise HTTPException(status_code=422, detail="quantity must be positive")
    return quantity


def apply_discount(subtotal: float, coupon: str | None, faults_enabled: bool) -> float:
    if coupon is None:
        return round(subtotal, 2)
    if coupon not in COUPONS:
        raise HTTPException(status_code=422, detail="unknown coupon")
    kind, value = COUPONS[coupon]
    if kind == "percent":
        total = subtotal - subtotal * value / 100
    else:
        total = subtotal - value
    if faults_enabled:
        # FAULT 3: a fixed discount larger than the subtotal is not capped.
        if total < 0:
            raise DiscountError(f"discount produced a negative total: {total}")
    else:
        total = max(total, 0.0)
    return round(total, 2)


def create_app(
    settings: DemoSettings | None = None,
    reporter: SentinelReporter | None = None,
) -> FastAPI:
    settings = settings if settings is not None else load_settings()
    if reporter is None:
        reporter = SentinelReporter.from_env(environment=settings.environment)
    faults = settings.faults_enabled

    app = FastAPI(title="Sentinel Demo Shop")
    app.state.settings = settings
    app.state.sentinel_reporter = reporter

    @app.exception_handler(Exception)
    async def report_unhandled_exception(request: Request, exc: Exception) -> PlainTextResponse:
        # Report to Sentinel (best effort, never raises) and keep the plain 500.
        try:
            await run_in_threadpool(
                reporter.report_exception,
                exc,
                method=request.method,
                path=request.url.path,
                correlation_id=request.headers.get(CORRELATION_HEADER),
            )
        except Exception:  # noqa: BLE001 - reporting must never change the response
            pass
        return PlainTextResponse("Internal Server Error", status_code=500)
    orders: dict[int, dict[str, object]] = {
        1: {"id": 1, "product": "widget", "quantity": 3, "subtotal": 30.0},
    }
    app.state.orders = orders

    @app.get("/health")
    def health() -> dict[str, object]:
        return {
            "status": "ok",
            "environment": settings.environment,
            "faults_enabled": faults,
        }

    @app.get("/users/{user_id}/profile")
    def user_profile(user_id: int) -> dict[str, object]:
        user = USERS.get(user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="user not found")
        return {
            "id": user_id,
            "name": user["name"],
            "email": user["email"],
            "display_name": _display_name(user, faults),
        }

    @app.post("/orders", status_code=201)
    def create_order(payload: OrderIn) -> dict[str, object]:
        price = PRODUCTS.get(payload.product)
        if price is None:
            raise HTTPException(status_code=422, detail="unknown product")
        quantity = _parse_quantity(payload.quantity, faults)
        order_id = max(orders, default=0) + 1
        order = {
            "id": order_id,
            "product": payload.product,
            "quantity": quantity,
            "subtotal": round(price * quantity, 2),
        }
        orders[order_id] = order
        return order

    @app.get("/orders/{order_id}/total")
    def order_total(order_id: int, coupon: str | None = Query(default=None)) -> dict[str, object]:
        order = orders.get(order_id)
        if order is None:
            raise HTTPException(status_code=404, detail="order not found")
        subtotal = float(order["subtotal"])  # type: ignore[arg-type]
        return {
            "id": order_id,
            "subtotal": subtotal,
            "coupon": coupon,
            "total": apply_discount(subtotal, coupon, faults),
        }

    return app
