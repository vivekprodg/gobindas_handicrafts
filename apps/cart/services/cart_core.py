"""
apps/cart/services/cart_core.py

Core cart service - handles cart creation, lookup, merging, and
lifecycle operations. Pure business logic, framework-agnostic.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from django.db import transaction
from django.utils import timezone

from ..models import Cart, CartItem


class CartService:
    """
    Centralized service for cart operations.
    All cart mutations flow through this class to guarantee
    consistent state, consistent pricing snapshots, and atomicity.
    """

    # ---------------------------------------------------------------
    # 1. Cart Lookup / Creation
    # ---------------------------------------------------------------
    @staticmethod
    def get_or_create_cart(request) -> Tuple[Cart, bool]:
        """
        Returns the active cart for the current request, creating one
        if necessary. Performs guest-to-authenticated merge transparently.
        """
        return Cart.objects.get_or_create_for_request(request)

    @staticmethod
    def get_active_cart(request) -> Optional[Cart]:
        cart, _ = CartService.get_or_create_cart(request)
        return cart

    @staticmethod
    def get_cart_for_customer(customer) -> Optional[Cart]:
        return Cart.objects.get_for_customer(customer)

    @staticmethod
    def get_cart_by_token(token: str) -> Optional[Cart]:
        if not token:
            return None
        return Cart.objects.filter(anonymous_token=token, is_active=True).first()

    # ---------------------------------------------------------------
    # 2. Guest-to-Customer Migration
    # ---------------------------------------------------------------
    @staticmethod
    @transaction.atomic
    def merge_guest_cart_into_customer(guest_cart: Cart, customer) -> Cart:
        """
        Merges the line items of an anonymous guest cart into the
        authenticated customer's cart. Quantities of identical
        product/variant lines are combined.
        """
        if not guest_cart or guest_cart.customer_id:
            return CartService.get_cart_for_customer(customer)

        customer_cart, _ = Cart.objects.get_or_create(customer=customer)

        for guest_item in guest_cart.items.filter(status=CartItem.ItemStatus.ACTIVE):
            match_qs = customer_cart.items.filter(
                product_id=guest_item.product_id,
                variant_id=guest_item.variant_id,
                status=CartItem.ItemStatus.ACTIVE,
            )
            existing = match_qs.first()
            if existing:
                existing.quantity = existing.quantity + guest_item.quantity
                existing.touch_parent() if hasattr(existing, "touch_parent") else None
                existing.save(update_fields=["quantity", "updated_at"])
                guest_item.delete()
            else:
                guest_item.cart = customer_cart
                guest_item.save(update_fields=["cart", "updated_at"])

        # Disable the old guest cart to prevent duplicate-checkout loops
        guest_cart.status = Cart.CartStatus.MERGED
        guest_cart.is_active = False
        guest_cart.save(update_fields=["status", "is_active", "updated_at"])

        customer_cart.touch()
        return customer_cart

    # ---------------------------------------------------------------
    # 3. Cart Activity Maintenance
    # ---------------------------------------------------------------
    @staticmethod
    def touch(cart: Cart) -> None:
        cart.touch()

    @staticmethod
    def mark_abandoned(cart: Cart) -> None:
        cart.mark_abandoned()

    @staticmethod
    def mark_converted(cart: Cart) -> None:
        cart.mark_converted()

    # ---------------------------------------------------------------
    # 4. Helper for templates / mini-cart
    # ---------------------------------------------------------------
    @staticmethod
    def build_mini_cart_payload(cart: Cart) -> List[Dict[str, Any]]:
        items = []
        for item in cart.items.filter(status=CartItem.ItemStatus.ACTIVE).select_related("product", "variant").order_by("added_at"):
            image_url = ""
            if item.product_image_snapshot:
                try:
                    image_url = item.product_image_snapshot.url
                except Exception:
                    image_url = ""
            elif getattr(item.product, "primary_image", None):
                try:
                    image_url = item.product.primary_image.url
                except Exception:
                    image_url = ""
            items.append(
                {
                    "id": item.id,
                    "product_id": item.product_id,
                    "variant_id": item.variant_id,
                    "name": item.product_name_snapshot,
                    "variant_name": item.variant_name_snapshot or "",
                    "image": image_url,
                    "quantity": item.quantity,
                    "unit_price": str(item.unit_price_snapshot),
                    "line_subtotal": str(item.line_subtotal),
                    "currency": item.currency_snapshot,
                    "url": f"/product/{getattr(item.product, 'slug', '')}/" if hasattr(item.product, "slug") else "#",
                }
            )
        return items