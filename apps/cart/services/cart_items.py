"""
Enterprise-grade Cart Item Service Layer for the Cart application.
Manages cart line item workflows while delegating inventory and
reservation rules to the Inventory application.
"""
from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional, Tuple

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from ..models import Cart, CartItem

logger = logging.getLogger(__name__)

_DEFAULT_MAX_ITEMS_PER_CART = 200
_DEFAULT_MAX_QUANTITY_PER_ITEM = 99

def get_max_items_per_cart() -> int:
    try:
        return max(1, int(getattr(settings, "CART_MAX_ITEMS", _DEFAULT_MAX_ITEMS_PER_CART)))
    except (TypeError, ValueError):
        return _DEFAULT_MAX_ITEMS_PER_CART

def get_max_quantity_per_item() -> int:
    try:
        return max(1, int(getattr(settings, "CART_MAX_QUANTITY", _DEFAULT_MAX_QUANTITY_PER_ITEM)))
    except (TypeError, ValueError):
        return _DEFAULT_MAX_QUANTITY_PER_ITEM

def _structured_response(
    success: bool,
    *,
    code: str = "",
    message: str = "",
    payload: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
) -> Dict[str, Any]:
    res: Dict[str, Any] = {
        "success": bool(success),
        "code": str(code),
        "message": str(message),
    }
    if payload and isinstance(payload, dict):
        res.update(payload)
    if error is not None:
        res["error"] = str(error)
    return res

def _serialize_cart_item(item: Optional[CartItem]) -> Dict[str, Any]:
    if item is None:
        return {}
    return {
        "id": item.pk,
        "cart_id": getattr(item, "cart_id", None),
        "product_id": getattr(item, "product_id", None),
        "variant_id": getattr(item, "variant_id", None),
        "quantity": int(getattr(item, "quantity", 0) or 0),
        "status": str(getattr(item, "status", "")),
        "saved_reason": str(getattr(item, "saved_reason", "") or ""),
        "unit_price_snapshot": str(getattr(item, "unit_price_snapshot", Decimal("0.00"))),
        "currency_snapshot": str(getattr(item, "currency_snapshot", "USD")),
        "line_subtotal": str(getattr(item, "line_subtotal", Decimal("0.00"))),
    }

def _get_inventory_services() -> Optional[Any]:
    try:
        from apps.inventory import services
        return services
    except Exception:
        return None

def _safe_inventory_check_availability(product: Any, product_variant: Any, quantity: int) -> Dict[str, Any]:
    services = _get_inventory_services()
    if services is None:
        return {"is_available": True, "source": "inventory_service_unavailable"}
    try:
        return services.check_stock(
            product=product,
            product_variant=product_variant,
            quantity=Decimal(str(quantity)),
            include_all_warehouses=True,
        )
    except Exception as exc:
        logger.debug("Inventory check failed: %s", exc)
        return {"is_available": True, "source": "inventory_service_error"}

def _safe_inventory_reserve(quantity: int, product: Any, product_variant: Any, cart: Cart) -> Dict[str, Any]:
    services = _get_inventory_services()
    if services is None:
        return {"success": False, "error": "Inventory service unavailable"}
    try:
        return services.reserve_stock(
            quantity=Decimal(str(quantity)),
            product=product,
            product_variant=product_variant,
            cart=cart,
            user=cart.customer if cart.customer_id else None,
            session_key=cart.session_key or "",
            reservation_type="cart",
        )
    except Exception as exc:
        logger.debug("Inventory reserve failed: %s", exc)
        return {"success": False, "error": str(exc)}

def _safe_inventory_release(reservation_id: Optional[int], reason: str = "") -> Dict[str, Any]:
    if not reservation_id:
        return {"success": True, "released": False}
    services = _get_inventory_services()
    if services is None:
        return {"success": False, "error": "Inventory service unavailable"}
    try:
        return services.release_stock(reservation_id=reservation_id, reason=reason, is_automatic=True)
    except Exception as exc:
        logger.debug("Inventory release failed: %s", exc)
        return {"success": False, "error": str(exc)}

class CartItemService:
    @staticmethod
    def _resolve_unit_price(*, product: Any, variant: Any) -> Decimal:
        if variant is not None and getattr(variant, "price_override", None) is not None:
            return variant.price_override
        return getattr(product, "price", Decimal("0.00")) or Decimal("0.00")

    @staticmethod
    def _resolve_compare_at_price(*, product: Any, variant: Any) -> Optional[Decimal]:
        if variant is not None and getattr(variant, "compare_price", None):
            return variant.compare_price
        return getattr(product, "original_price", None)

    @staticmethod
    def add_item(
        *,
        cart: Optional[Cart],
        product: Any = None,
        variant: Any = None,
        quantity: int = 1,
        unit_price_snapshot: Optional[Decimal] = None,
        currency: str = "",
        personalization: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if cart is None or not getattr(cart, "pk", None):
            return _structured_response(False, code="cart_not_found", message=str(_("Cart not found")))

        qty = max(1, int(quantity or 1))
        max_qty = get_max_quantity_per_item()
        if qty > max_qty:
            return _structured_response(
                False,
                code="quantity_limit_exceeded",
                message=str(_("Quantity exceeds maximum of %(max)d") % {"max": max_qty}),
            )

        if product is None and variant is None:
            return _structured_response(
                False, code="missing_product", message=str(_("Product or variant is required."))
            )

        current_count = cart.items.filter(status=CartItem.ItemStatus.ACTIVE).count()
        match_qs = cart.items.filter(status=CartItem.ItemStatus.ACTIVE)
        if variant is not None:
            match_qs = match_qs.filter(variant=variant)
        elif product is not None:
            match_qs = match_qs.filter(product=product, variant__isnull=True)
        existing_match = match_qs.first()

        if existing_match is None and current_count >= get_max_items_per_cart():
            return _structured_response(
                False,
                code="cart_limit_exceeded",
                message=str(_("Cart has reached maximum distinct items limit.")),
            )

        try:
            with transaction.atomic():
                if existing_match is not None:
                    new_qty = existing_match.quantity + qty
                    if new_qty > max_qty:
                        return _structured_response(
                            False,
                            code="quantity_limit_exceeded",
                            message=str(_("Total quantity exceeds maximum allowed.")),
                        )
                    CartItem.objects.filter(pk=existing_match.pk).update(
                        quantity=new_qty, updated_at=timezone.now()
                    )
                    existing_match.refresh_from_db(fields=["quantity", "updated_at"])
                    result_item = existing_match
                else:
                    unit_price = (
                        unit_price_snapshot
                        if unit_price_snapshot is not None
                        else CartItemService._resolve_unit_price(product=product, variant=variant)
                    )
                    compare_at = CartItemService._resolve_compare_at_price(product=product, variant=variant)

                    result_item = CartItem.objects.create(
                        cart=cart,
                        product=product,
                        variant=variant,
                        quantity=qty,
                        unit_price_snapshot=unit_price,
                        compare_at_price_snapshot=compare_at,
                        product_name_snapshot=getattr(product, "title", "") if product else "",
                        product_sku_snapshot=getattr(variant or product, "sku", "") or "",
                        variant_name_snapshot=getattr(variant, "name", "") if variant else "",
                        currency_snapshot=currency or cart.currency or "USD",
                        status=CartItem.ItemStatus.ACTIVE,
                        personalization=personalization or {},
                    )
                cart.touch()
        except Exception as exc:
            logger.exception("add_item failed: %s", exc)
            return _structured_response(False, code="add_item_failed", message=str(exc) or "Add failed")

        inv_check = _safe_inventory_check_availability(product, variant, result_item.quantity)
        reservation = _safe_inventory_reserve(result_item.quantity, product, variant, cart)

        if isinstance(reservation, dict) and reservation.get("success") and reservation.get("reservation_id"):
            CartItem.objects.filter(pk=result_item.pk).update(
                reservation_id=reservation["reservation_id"],
                reservation_token=reservation.get("reservation_token", ""),
                reservation_status="active",
            )
            result_item.refresh_from_db(fields=["reservation_id", "reservation_token", "reservation_status"])

        return _structured_response(
            True,
            code="item_added",
            message=str(_("Item added to cart")),
            payload={
                "item": _serialize_cart_item(result_item),
                "inventory_check": inv_check,
                "reservation": reservation,
            },
        )

    @staticmethod
    def update_quantity(*, cart: Optional[Cart], item_id: Optional[int], quantity: Any = 1) -> Dict[str, Any]:
        if cart is None or not getattr(cart, "pk", None):
            return _structured_response(False, code="cart_not_found", message=str(_("Cart not found")))

        new_qty = int(quantity or 1)
        if new_qty < 1:
            return CartItemService.remove_item(cart=cart, item_id=item_id)

        max_qty = get_max_quantity_per_item()
        if new_qty > max_qty:
            return _structured_response(
                False,
                code="quantity_limit_exceeded",
                message=str(_("Quantity exceeds maximum of %(max)d") % {"max": max_qty}),
            )

        item = cart.items.filter(pk=item_id, status=CartItem.ItemStatus.ACTIVE).first()
        if item is None:
            return _structured_response(False, code="item_not_found", message=str(_("Item not found in cart")))

        try:
            with transaction.atomic():
                if item.quantity == new_qty:
                    return _structured_response(
                        True, code="no_change", message=str(_("No change")), payload={"item": _serialize_cart_item(item)}
                    )
                CartItem.objects.filter(pk=item.pk).update(quantity=new_qty, updated_at=timezone.now())
                item.refresh_from_db(fields=["quantity", "updated_at"])
                cart.touch()
        except Exception as exc:
            logger.exception("update_quantity failed: %s", exc)
            return _structured_response(False, code="update_failed", message=str(exc))

        return _structured_response(
            True, code="quantity_updated", message=str(_("Quantity updated")), payload={"item": _serialize_cart_item(item)}
        )

    @staticmethod
    def remove_item(*, cart: Optional[Cart], item_id: Optional[int]) -> Dict[str, Any]:
        if cart is None or not getattr(cart, "pk", None):
            return _structured_response(False, code="cart_not_found", message=str(_("Cart not found")))

        item = cart.items.filter(pk=item_id).first()
        if item is None:
            return _structured_response(False, code="item_not_found", message=str(_("Item not found in cart")))

        try:
            with transaction.atomic():
                res_id = getattr(item, "reservation_id", None)
                CartItem.objects.filter(pk=item.pk).delete()
                _safe_inventory_release(res_id, reason="Removed from cart")
                cart.touch()
        except Exception as exc:
            logger.exception("remove_item failed: %s", exc)
            return _structured_response(False, code="remove_failed", message=str(exc))

        return _structured_response(True, code="item_removed", message=str(_("Item removed from cart")))

    @staticmethod
    def clear_cart(*, cart: Optional[Cart]) -> Dict[str, Any]:
        if cart is None or not getattr(cart, "pk", None):
            return _structured_response(False, code="cart_not_found", message=str(_("Cart not found")))

        try:
            with transaction.atomic():
                active_items = list(cart.items.filter(status=CartItem.ItemStatus.ACTIVE).only("id", "reservation_id"))
                cart.items.all().delete()
                for item in active_items:
                    _safe_inventory_release(getattr(item, "reservation_id", None), reason="Cart cleared")
                cart.touch()
        except Exception as exc:
            logger.exception("clear_cart failed: %s", exc)
            return _structured_response(False, code="clear_failed", message=str(exc))

        return _structured_response(True, code="cart_cleared", message=str(_("Cart cleared")))

    @staticmethod
    def save_for_later(*, cart: Optional[Cart], item_id: Optional[int], reason: str = "") -> Dict[str, Any]:
        if cart is None or not getattr(cart, "pk", None):
            return _structured_response(False, code="cart_not_found", message=str(_("Cart not found")))

        item = cart.items.filter(pk=item_id, status=CartItem.ItemStatus.ACTIVE).first()
        if item is None:
            return _structured_response(False, code="item_not_found", message=str(_("Item not found in cart")))

        try:
            with transaction.atomic():
                CartItem.objects.filter(pk=item.pk).update(
                    status=CartItem.ItemStatus.SAVED,
                    saved_reason=reason or CartItem.SavedForLaterReason.MANUAL,
                    moved_to_save_at=timezone.now(),
                    updated_at=timezone.now(),
                )
                _safe_inventory_release(getattr(item, "reservation_id", None), reason="Saved for later")
                cart.touch()
            item.refresh_from_db()
        except Exception as exc:
            logger.exception("save_for_later failed: %s", exc)
            return _structured_response(False, code="save_failed", message=str(exc))

        return _structured_response(
            True, code="item_saved", message=str(_("Item saved for later")), payload={"item": _serialize_cart_item(item)}
        )

    @staticmethod
    def move_to_cart(*, cart: Optional[Cart], item_id: Optional[int]) -> Dict[str, Any]:
        if cart is None or not getattr(cart, "pk", None):
            return _structured_response(False, code="cart_not_found", message=str(_("Cart not found")))

        item = cart.items.filter(pk=item_id, status=CartItem.ItemStatus.SAVED).first()
        if item is None:
            return _structured_response(False, code="item_not_found", message=str(_("Saved item not found")))

        try:
            with transaction.atomic():
                CartItem.objects.filter(pk=item.pk).update(
                    status=CartItem.ItemStatus.ACTIVE,
                    saved_reason=None,
                    moved_to_save_at=None,
                    updated_at=timezone.now(),
                )
                cart.touch()
            item.refresh_from_db()
        except Exception as exc:
            logger.exception("move_to_cart failed: %s", exc)
            return _structured_response(False, code="move_failed", message=str(exc))

        return _structured_response(
            True, code="item_activated", message=str(_("Item moved to active cart")), payload={"item": _serialize_cart_item(item)}
        )

__all__ = ["CartItemService"]