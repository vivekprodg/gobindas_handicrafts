"""
Re-order workflow: duplicates items from a past order into the current active cart.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Union

from django.db import transaction
from django.utils.translation import gettext_lazy as _

from ..models import Cart, CartItem
from .cart_items import CartItemService

class CartReorderService:
    """Re-populates an active cart from a past order's items."""

    @staticmethod
    @transaction.atomic
    def reorder_items_into_cart(
        cart: Optional[Cart] = None,
        order: Optional[Any] = None,
        items: Optional[Iterable[Dict[str, Any]]] = None,
        user: Optional[Any] = None,
        session_key: Optional[str] = None,
        order_reference: str = "",
    ) -> Union[Cart, Dict[str, Any]]:
        """
        Duplicates line items into the specified active cart.
        Compatible with Order model instances, custom item lists,
        direct Cart objects, or API response dict expectations.
        """
        target_cart = cart

        if target_cart is None:
            if user and getattr(user, "is_authenticated", False):
                target_cart, _ = Cart.objects.get_or_create(customer=user, status=Cart.CartStatus.ACTIVE, is_active=True)
            elif session_key:
                target_cart, _ = Cart.objects.get_or_create(session_key=session_key, status=Cart.CartStatus.ACTIVE, is_active=True)

        if target_cart is None:
            return {
                "success": False,
                "code": "cart_not_found",
                "message": str(_("Target cart could not be resolved.")),
            }

        skipped: List[str] = []
        added_count = 0

        if order and hasattr(order, "items"):
            order_items = order.items.all().select_related("product", "variant")
            for o_item in order_items:
                p = getattr(o_item, "product", None)
                v = getattr(o_item, "variant", None)
                qty = getattr(o_item, "quantity", 1) or 1

                if not p and not v:
                    skipped.append(str(_("Product for line item is no longer available.")))
                    continue

                res = CartItemService.add_item(cart=target_cart, product=p, variant=v, quantity=qty)
                if res.get("success"):
                    added_count += 1
                else:
                    skipped.append(res.get("message") or "Failed to add item")

        elif items:
            for raw in items:
                if not isinstance(raw, dict):
                    continue
                p_id = raw.get("product_id")
                v_id = raw.get("variant_id")
                qty = raw.get("quantity", 1) or 1

                p = None
                v = None
                try:
                    from apps.catalog.models import Product, ProductVariant
                    if v_id:
                        v = ProductVariant.objects.filter(pk=v_id).first()
                        if v:
                            p = v.product
                    elif p_id:
                        p = Product.objects.filter(pk=p_id).first()
                except Exception:
                    pass

                if not p and not v:
                    skipped.append(str(_("Item not found.")))
                    continue

                res = CartItemService.add_item(cart=target_cart, product=p, variant=v, quantity=qty)
                if res.get("success"):
                    added_count += 1
                else:
                    skipped.append(res.get("message") or "Failed to add item")

        target_cart.touch()

        return {
            "success": len(skipped) == 0 or added_count > 0,
            "code": "reorder_processed" if len(skipped) == 0 else "reorder_partial",
            "message": str(_("Reordered %(count)d item(s).") % {"count": added_count}),
            "cart_id": target_cart.pk,
            "added_count": added_count,
            "skipped_reasons": skipped,
        }

__all__ = ["CartReorderService"]