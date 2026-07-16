"""
apps/cart/services/cart_reorder.py

Re-order workflow: duplicates items from a past order into the
current active cart. Implements the missing `reorder_items_into_cart`
referenced by apps/customers and apps/orders views.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Iterable, Optional

from django.db import transaction
from django.utils.translation import gettext_lazy as _

from ..models import Cart, CartItem

class CartReorderService:
    """Re-populates an active cart from a past order's items."""

    @staticmethod
    @transaction.atomic
    def reorder_items_into_cart(
        order: Any,
        user: Optional[Any] = None,
        session_key: Optional[str] = None,
    ) -> Cart:
        """
        Duplicates the line items of the given order into the active cart
        of the user (or session). Returns the resulting cart.
        """
        from .cart_core import CartService
        from .cart_items import CartItemService
        from ..models import Cart as CartModel

        # Resolve target cart
        if user and getattr(user, "is_authenticated", False):
            cart, _ = CartModel.objects.get_or_create(customer=user)
        elif session_key:
            cart, _ = CartModel.objects.get_or_create(session_key=session_key)
        else:
            raise ValueError(_("Either an authenticated user or a session key is required."))

        skipped: list[str] = []
        added_count = 0
        for order_item in order.items.filter(status="active").select_related("product", "variant"):
            if not order_item.product:
                skipped.append(_(f"Product for '{order_item.product_name_snapshot}' is no longer available."))
                continue
            try:
                CartItemService.add_item(
                    cart,
                    product=order_item.product,
                    variant=order_item.variant,
                    quantity=order_item.quantity,
                )
                added_count += 1
            except Exception as exc:
                skipped.append(
                    _(f"Could not add '{order_item.product_name_snapshot}': {exc}")
                )

        if skipped:
            # Surface warnings via messages framework (caller is expected to consume this list)
            from django.contrib import messages
            for msg in skipped:
                messages.warning(None, msg)

        cart.touch()
        return cart