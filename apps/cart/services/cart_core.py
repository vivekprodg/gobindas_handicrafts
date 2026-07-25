"""
Core cart service - handles cart creation, lookup, merging, and
lifecycle operations. Pure business logic, framework-agnostic.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from django.conf import settings
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
    def get_or_create_cart(request: Any) -> Tuple[Cart, bool]:
        """
        Returns the active cart for the current request, creating one
        if necessary. Performs guest-to-authenticated merge transparently.
        """
        return Cart.objects.get_or_create_for_request(request)

    @staticmethod
    def get_or_create_for_request(request: Any) -> Tuple[Optional[Cart], bool]:
        """Alias matching view conventions across API and Page views."""
        return Cart.objects.get_or_create_for_request(request)

    @staticmethod
    def get_active_cart(request: Any) -> Optional[Cart]:
        cart, _ = CartService.get_or_create_cart(request)
        return cart

    @staticmethod
    def get_cart_for_customer(customer: Any) -> Optional[Cart]:
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
    def merge_guest_cart_into_customer(guest_cart: Optional[Cart], customer: Any) -> Optional[Cart]:
        """
        Merges line items of an anonymous guest cart into the
        authenticated customer's cart. Quantities of identical
        product/variant lines are combined up to max limits.
        """
        if not customer or not getattr(customer, "is_authenticated", False):
            return None
        if not guest_cart or guest_cart.customer_id:
            return CartService.get_cart_for_customer(customer)

        customer_cart = Cart.objects.get_for_customer(customer)
        if not customer_cart:
            customer_cart = Cart.objects.create(customer=customer, status=Cart.CartStatus.ACTIVE)

        guest_items = list(
            guest_cart.items.filter(status=CartItem.ItemStatus.ACTIVE).select_related("product", "variant")
        )

        for guest_item in guest_items:
            match_qs = customer_cart.items.filter(
                product_id=guest_item.product_id,
                variant_id=guest_item.variant_id,
                status=CartItem.ItemStatus.ACTIVE,
            )
            existing = match_qs.first()
            if existing:
                existing.quantity = existing.quantity + guest_item.quantity
                existing.save(update_fields=["quantity", "updated_at"])
                guest_item.delete()
            else:
                guest_item.cart = customer_cart
                guest_item.save(update_fields=["cart", "updated_at"])

        # Disable the old guest cart
        guest_cart.status = Cart.CartStatus.MERGED
        guest_cart.is_active = False
        guest_cart.last_merged_at = timezone.now()
        guest_cart.save(update_fields=["status", "is_active", "last_merged_at", "updated_at"])

        customer_cart.touch()
        return customer_cart

    # ---------------------------------------------------------------
    # 3. Totals Calculation Helper
    # ---------------------------------------------------------------
    @staticmethod
    def compute_totals(
        cart: Optional[Cart],
        tax_rate: Optional[Decimal] = None,
        shipping_flat: Optional[Decimal] = None,
    ) -> Dict[str, Any]:
        """
        Computes structured cart totals snapshot using Cart model properties.
        """
        if cart is None or not getattr(cart, "pk", None):
            return {
                "subtotal": Decimal("0.00"),
                "tax": Decimal("0.00"),
                "shipping": Decimal("0.00"),
                "discount": Decimal("0.00"),
                "grand_total": Decimal("0.00"),
                "total_items": 0,
                "unique_items": 0,
            }

        subtotal = cart.subtotal
        discount = cart.coupon_discount_amount or Decimal("0.00")
        tax = cart.estimated_tax
        shipping = shipping_flat if shipping_flat is not None else cart.estimated_shipping
        grand_total = cart.grand_total

        return {
            "subtotal": subtotal,
            "tax": tax,
            "shipping": shipping,
            "discount": discount,
            "grand_total": grand_total,
            "total_items": cart.total_items_count,
            "unique_items": cart.unique_items_count,
        }

    # ---------------------------------------------------------------
    # 4. Cart Activity Maintenance
    # ---------------------------------------------------------------
    @staticmethod
    def touch(cart: Cart) -> None:
        if cart and getattr(cart, "pk", None):
            cart.touch()

    @staticmethod
    def mark_abandoned(cart: Cart) -> None:
        if cart and getattr(cart, "pk", None):
            cart.mark_abandoned()

    @staticmethod
    def mark_converted(cart: Cart) -> None:
        if cart and getattr(cart, "pk", None):
            cart.mark_converted()

    # ---------------------------------------------------------------
    # 5. Helper for templates / mini-cart
    # ---------------------------------------------------------------
    @staticmethod
    def build_mini_cart_payload(cart: Cart) -> List[Dict[str, Any]]:
        if not cart or not getattr(cart, "pk", None):
            return []

        items = []
        active_items = (
            cart.items.filter(status=CartItem.ItemStatus.ACTIVE)
            .select_related("product", "variant")
            .order_by("added_at")
        )

        for item in active_items:
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

            product_slug = getattr(item.product, "slug", "")
            item_url = f"/catalog/products/{product_slug}/" if product_slug else "#"

            items.append({
                "id": item.id,
                "product_id": item.product_id,
                "variant_id": item.variant_id,
                "name": item.product_name_snapshot or getattr(item.product, "title", "Product"),
                "variant_name": item.variant_name_snapshot or "",
                "image": image_url,
                "quantity": item.quantity,
                "unit_price": str(item.unit_price_snapshot),
                "line_subtotal": str(item.line_subtotal),
                "currency": item.currency_snapshot or cart.currency,
                "url": item_url,
            })
        return items

__all__ = ["CartService"]