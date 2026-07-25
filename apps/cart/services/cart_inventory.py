"""
Enterprise-grade Inventory Orchestration Layer for the Cart application.
Delegates exclusively to the Inventory application's service layer.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple, Union

from django.conf import settings
from django.utils.translation import gettext_lazy as _

from ..models import Cart, CartItem

logger = logging.getLogger(__name__)

_DEFAULT_RESERVATION_MINUTES = 30
_DEFAULT_MAX_QUANTITY_PER_ITEM = 99
_DEFAULT_LOW_STOCK_THRESHOLD = 5
_DEFAULT_CHECK_TIMEOUT_SECONDS = 8
_DEFAULT_RENEWAL_BATCH_SIZE = 200

def get_default_reservation_minutes() -> int:
    try:
        return max(1, int(getattr(settings, "CART_INVENTORY_RESERVATION_MINUTES", _DEFAULT_RESERVATION_MINUTES)))
    except (TypeError, ValueError):
        return _DEFAULT_RESERVATION_MINUTES

def get_max_quantity_per_item() -> int:
    try:
        return max(1, int(getattr(settings, "CART_INVENTORY_MAX_QUANTITY_PER_ITEM", _DEFAULT_MAX_QUANTITY_PER_ITEM)))
    except (TypeError, ValueError):
        return _DEFAULT_MAX_QUANTITY_PER_ITEM

def get_low_stock_threshold() -> int:
    try:
        return max(0, int(getattr(settings, "CART_INVENTORY_LOW_STOCK_THRESHOLD", _DEFAULT_LOW_STOCK_THRESHOLD)))
    except (TypeError, ValueError):
        return _DEFAULT_LOW_STOCK_THRESHOLD

def get_check_timeout_seconds() -> int:
    try:
        return max(1, int(getattr(settings, "CART_INVENTORY_CHECK_TIMEOUT_SECONDS", _DEFAULT_CHECK_TIMEOUT_SECONDS)))
    except (TypeError, ValueError):
        return _DEFAULT_CHECK_TIMEOUT_SECONDS

def get_renewal_batch_size() -> int:
    try:
        return max(1, min(5000, int(getattr(settings, "CART_INVENTORY_RENEWAL_BATCH_SIZE", _DEFAULT_RENEWAL_BATCH_SIZE))))
    except (TypeError, ValueError):
        return _DEFAULT_RENEWAL_BATCH_SIZE

def get_include_damaged_default() -> bool:
    return bool(getattr(settings, "CART_INVENTORY_INCLUDE_DAMAGED", False))

def get_backorder_default() -> bool:
    return bool(getattr(settings, "CART_INVENTORY_BACKORDER_DEFAULT", False))

def get_low_stock_global() -> bool:
    return bool(getattr(settings, "CART_INVENTORY_LOW_STOCK_GLOBAL", True))

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

def _empty_inventory_context() -> Dict[str, Any]:
    return {
        "exists": False,
        "inventory_id": None,
        "warehouse_id": None,
        "warehouse_name": None,
        "is_active": False,
        "is_out_of_stock": True,
        "is_low_stock": False,
        "available_quantity": "0.00",
        "reserved_quantity": "0.00",
        "free_stock": "0.00",
        "total_stock": "0.00",
        "source": "cart_orchestrator_empty",
    }

def _get_inventory_services() -> Optional[Any]:
    try:
        from apps.inventory import services
        return services
    except Exception:
        return None

def _get_inventory_selectors() -> Optional[Any]:
    try:
        from apps.inventory import selectors
        return selectors
    except Exception:
        return None

def _safe_inventory_check(
    *,
    product: Any = None,
    product_variant: Any = None,
    warehouse: Any = None,
    quantity: Any = 1,
    include_all_warehouses: bool = True,
) -> Dict[str, Any]:
    services = _get_inventory_services()
    if services is None:
        return {"is_available": True, "free_stock": "0.00", "source": "inventory_service_unavailable"}
    try:
        qty = Decimal(str(quantity or 1))
        return services.check_stock(
            product=product,
            product_variant=product_variant,
            warehouse=warehouse,
            quantity=qty,
            include_all_warehouses=include_all_warehouses,
        )
    except Exception as exc:
        logger.debug("Safe inventory check failed: %s", exc)
        return {"is_available": True, "free_stock": "0.00", "source": "inventory_service_error"}

def _safe_inventory_reserve(
    *,
    quantity: Any,
    product: Any = None,
    product_variant: Any = None,
    warehouse: Any = None,
    cart: Any = None,
    user: Any = None,
    session_key: str = "",
    expires_in_minutes: Optional[int] = None,
    reference_number: str = "",
    notes: str = "",
) -> Dict[str, Any]:
    services = _get_inventory_services()
    if services is None:
        return {"success": False, "error": "Inventory service unavailable"}
    try:
        mins = expires_in_minutes or get_default_reservation_minutes()
        expires_in = timedelta(minutes=max(1, int(mins)))
        return services.reserve_stock(
            quantity=Decimal(str(quantity)),
            product=product,
            product_variant=product_variant,
            warehouse=warehouse,
            cart=cart,
            user=user,
            session_key=session_key,
            expires_in=expires_in,
            reservation_type="cart",
            reference_number=reference_number,
            notes=notes,
        )
    except Exception as exc:
        logger.debug("Safe inventory reserve failed: %s", exc)
        return {"success": False, "error": str(exc)}

def _safe_inventory_release(
    *,
    reservation_token: Optional[str] = None,
    reservation_id: Optional[int] = None,
    reason: str = "",
    is_automatic: bool = False,
) -> Dict[str, Any]:
    services = _get_inventory_services()
    if services is None:
        return {"success": False, "error": "Inventory service unavailable"}
    try:
        return services.release_stock(
            reservation_token=reservation_token,
            reservation_id=reservation_id,
            reason=reason,
            is_automatic=is_automatic,
        )
    except Exception as exc:
        logger.debug("Safe inventory release failed: %s", exc)
        return {"success": False, "error": str(exc)}

def _safe_inventory_get_summary(*, product: Any = None, product_variant: Any = None, warehouse: Any = None) -> Dict[str, Any]:
    selectors = _get_inventory_selectors()
    if selectors is None:
        return _empty_inventory_context()
    try:
        res = selectors.get_inventory_summary_for_target(
            product=product, product_variant=product_variant, warehouse=warehouse
        )
        if isinstance(res, dict):
            ctx = _empty_inventory_context()
            ctx.update(res)
            return ctx
    except Exception as exc:
        logger.debug("Safe inventory summary failed: %s", exc)
    return _empty_inventory_context()

class CartInventoryService:
    @staticmethod
    def check_availability(
        *,
        product: Any,
        variant: Optional[Any] = None,
        quantity: int = 1,
        exclude_cart_item_id: Optional[int] = None,
    ) -> Tuple[bool, str]:
        if quantity < 1:
            return False, str(_("Quantity must be at least 1."))
        if product is None and variant is None:
            return False, str(_("A product or variant is required."))

        check = _safe_inventory_check(
            product=product, product_variant=variant, quantity=quantity, include_all_warehouses=True
        )

        try:
            free_stock = Decimal(str(check.get("free_stock", "0.00")))
        except Exception:
            free_stock = Decimal("0.00")

        if free_stock < Decimal(str(quantity)):
            return False, str(_("Only %(available)s item(s) available.") % {"available": str(free_stock)})

        return True, ""

    @staticmethod
    def validate_for_checkout(*, cart: Optional[Cart]) -> Dict[str, Any]:
        issues: List[Dict[str, Any]] = []

        if cart is None or not getattr(cart, "pk", None):
            return {
                "ready_for_checkout": False,
                "issues": [{"code": "cart_not_found", "message": str(_("Cart not found"))}],
                "totals": {"subtotal": "0.00", "grand_total": "0.00"},
                "cart": {},
            }

        if not getattr(cart, "is_active", False):
            issues.append({"code": "cart_inactive", "message": str(_("Cart is not active"))})

        active_items = list(cart.items.filter(status=CartItem.ItemStatus.ACTIVE))
        if not active_items:
            issues.append({"code": "cart_empty", "message": str(_("Cart has no active items"))})

        max_qty = get_max_quantity_per_item()
        for item in active_items:
            if item.quantity < 1:
                issues.append({"code": "invalid_quantity", "item_id": item.pk, "message": str(_("Quantity must be >= 1"))})
            elif item.quantity > max_qty:
                issues.append({"code": "quantity_limit_exceeded", "item_id": item.pk, "message": str(_("Quantity exceeds max"))})

            inv_summary = _safe_inventory_get_summary(
                product=item.product,
                product_variant=item.variant,
                warehouse=getattr(cart, "preferred_warehouse", None),
            )
            if inv_summary.get("is_out_of_stock", False):
                issues.append({"code": "out_of_stock", "item_id": item.pk, "message": str(_("Item is out of stock"))})

        totals = CartInventoryService.compute_totals(cart)
        return {
            "ready_for_checkout": len(issues) == 0,
            "issues": issues,
            "totals": totals,
            "cart": {"id": cart.pk, "is_active": cart.is_active, "subtotal": str(cart.subtotal)},
        }

    @staticmethod
    def get_inventory_context(*, product: Any = None, product_variant: Any = None, warehouse: Any = None) -> Dict[str, Any]:
        if product is None and product_variant is None:
            return _empty_inventory_context()
        return _safe_inventory_get_summary(product=product, product_variant=product_variant, warehouse=warehouse)

    @staticmethod
    def reserve_for_cart(
        *,
        cart: Optional[Cart],
        cart_item: Optional[CartItem] = None,
        product: Any = None,
        product_variant: Any = None,
        warehouse: Any = None,
        quantity: Any = 1,
        expires_in_minutes: Optional[int] = None,
        user: Any = None,
        session_key: str = "",
        reference_number: str = "",
        notes: str = "",
    ) -> Dict[str, Any]:
        return _safe_inventory_reserve(
            quantity=quantity,
            product=product,
            product_variant=product_variant,
            warehouse=warehouse,
            cart=cart,
            user=user,
            session_key=session_key or str(getattr(cart, "session_key", "") or ""),
            expires_in_minutes=expires_in_minutes,
            reference_number=reference_number or getattr(cart_item, "product_sku_snapshot", "") or "cart-item",
            notes=notes,
        )

    @staticmethod
    def release_for_cart(
        *, reservation_id: Optional[int] = None, reservation_token: Optional[str] = None, reason: str = "", is_automatic: bool = False
    ) -> Dict[str, Any]:
        if reservation_id is None and not reservation_token:
            return _structured_response(False, code="missing_reservation", message="Reservation ID or token required.")
        return _safe_inventory_release(
            reservation_token=reservation_token, reservation_id=reservation_id, reason=reason, is_automatic=is_automatic
        )

    @staticmethod
    def renew_for_cart(
        *, reservation_id: Optional[int] = None, reservation_token: Optional[str] = None, expires_in_minutes: Optional[int] = None
    ) -> Dict[str, Any]:
        rel = _safe_inventory_release(reservation_id=reservation_id, reservation_token=reservation_token, reason="Renew", is_automatic=True)
        if not rel.get("success"):
            return _structured_response(False, code="renewal_release_failed", message=rel.get("error", "Release failed"))
        return _structured_response(True, code="renewal_released", message="Reservation released for renewal.")

    @staticmethod
    def convert_for_cart(
        *, cart: Optional[Cart], cart_item: Optional[CartItem] = None, order_reference: str = "", user: Any = None
    ) -> Dict[str, Any]:
        if cart_item is None:
            return _structured_response(False, code="missing_cart_item", message="Cart item is required.")
        services = _get_inventory_services()
        if services is None:
            return _structured_response(False, code="inventory_unavailable", message="Inventory service unavailable.")
        try:
            return services.deduct_stock(
                quantity=cart_item.quantity,
                product=cart_item.product,
                product_variant=cart_item.variant,
                warehouse=getattr(cart, "preferred_warehouse", None),
                reservation_id=getattr(cart_item, "reservation_id", None),
                reference_number=order_reference or "cart-conversion",
                performed_by=user,
            )
        except Exception as exc:
            return _structured_response(False, code="convert_failed", message=str(exc))

    @staticmethod
    def validate_reservation_ownership(
        *, cart: Optional[Cart], reservation_id: Optional[int] = None, reservation_token: Optional[str] = None
    ) -> bool:
        if cart is None:
            return False
        selectors = _get_inventory_selectors()
        if selectors is None:
            return False
        try:
            if reservation_token:
                res = selectors.get_reservation_by_token(str(reservation_token))
            else:
                from apps.inventory.models import StockReservation
                res = StockReservation.objects.filter(pk=reservation_id).first()
            return getattr(res, "cart_id", None) == getattr(cart, "id", None)
        except Exception:
            return False

    @staticmethod
    def refresh_inventory_context(*, product: Any = None, product_variant: Any = None, warehouse: Any = None) -> Dict[str, Any]:
        return _safe_inventory_get_summary(product=product, product_variant=product_variant, warehouse=warehouse)

    @staticmethod
    def select_warehouse_for_cart(cart: Optional[Cart]) -> Any:
        if cart is None:
            return None
        if cart.preferred_warehouse:
            return cart.preferred_warehouse
        selectors = _get_inventory_selectors()
        return selectors.get_default_warehouse() if selectors else None

    @staticmethod
    def cleanup_expired_reservations_for_cart(*, batch_size: Optional[int] = None) -> Dict[str, Any]:
        services = _get_inventory_services()
        if services is None:
            return {"released": 0, "failed": 0, "processed": 0, "source": "inventory_service_unavailable"}
        try:
            return services.release_expired_reservations(batch_size=batch_size or get_renewal_batch_size())
        except Exception as exc:
            return {"released": 0, "failed": 0, "processed": 0, "error": str(exc)}

    @staticmethod
    def compute_totals(cart: Optional[Cart]) -> Dict[str, Any]:
        if cart is None:
            return {"subtotal": "0.00", "tax": "0.00", "shipping": "0.00", "discount": "0.00", "grand_total": "0.00", "total_items": 0, "unique_items": 0}
        return {
            "subtotal": str(cart.subtotal),
            "tax": str(cart.estimated_tax),
            "shipping": str(cart.estimated_shipping),
            "discount": str(cart.coupon_discount_amount or Decimal("0.00")),
            "grand_total": str(cart.grand_total),
            "total_items": cart.total_items_count,
            "unique_items": cart.unique_items_count,
        }

# Module-level aliases
def check_availability(*, product: Any, variant: Optional[Any] = None, quantity: int = 1, exclude_cart_item_id: Optional[int] = None) -> Tuple[bool, str]:
    return CartInventoryService.check_availability(product=product, variant=variant, quantity=quantity, exclude_cart_item_id=exclude_cart_item_id)

def validate_for_checkout(cart: Optional[Cart]) -> Dict[str, Any]:
    return CartInventoryService.validate_for_checkout(cart=cart)

def get_inventory_context(*, product: Any = None, product_variant: Any = None, warehouse: Any = None) -> Dict[str, Any]:
    return CartInventoryService.get_inventory_context(product=product, product_variant=product_variant, warehouse=warehouse)

def reserve_for_cart(**kwargs: Any) -> Dict[str, Any]:
    return CartInventoryService.reserve_for_cart(**kwargs)

def release_for_cart(**kwargs: Any) -> Dict[str, Any]:
    return CartInventoryService.release_for_cart(**kwargs)

def renew_for_cart(**kwargs: Any) -> Dict[str, Any]:
    return CartInventoryService.renew_for_cart(**kwargs)

def convert_for_cart(**kwargs: Any) -> Dict[str, Any]:
    return CartInventoryService.convert_for_cart(**kwargs)

def validate_reservation_ownership(**kwargs: Any) -> bool:
    return CartInventoryService.validate_reservation_ownership(**kwargs)

def refresh_inventory_context(**kwargs: Any) -> Dict[str, Any]:
    return CartInventoryService.refresh_inventory_context(**kwargs)

def select_warehouse_for_cart(cart: Optional[Cart]) -> Any:
    return CartInventoryService.select_warehouse_for_cart(cart)

def cleanup_expired_reservations_for_cart(**kwargs: Any) -> Dict[str, Any]:
    return CartInventoryService.cleanup_expired_reservations_for_cart(**kwargs)

def compute_cart_totals(cart: Optional[Cart]) -> Dict[str, Any]:
    return CartInventoryService.compute_totals(cart)

__all__ = [
    "CartInventoryService",
    "get_default_reservation_minutes",
    "get_max_quantity_per_item",
    "get_low_stock_threshold",
    "get_check_timeout_seconds",
    "get_renewal_batch_size",
    "get_include_damaged_default",
    "get_backorder_default",
    "get_low_stock_global",
    "check_availability",
    "validate_for_checkout",
    "get_inventory_context",
    "reserve_for_cart",
    "release_for_cart",
    "renew_for_cart",
    "convert_for_cart",
    "validate_reservation_ownership",
    "refresh_inventory_context",
    "select_warehouse_for_cart",
    "cleanup_expired_reservations_for_cart",
    "compute_cart_totals",
]