"""
Core business logic for coupon validation, discount calculation, cart application,
stale-discount re-validation, and order redemption/reversal lifecycle.
"""
from __future__ import annotations

import logging
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, Optional, Tuple, List

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.cart.models import Cart, CartItem
from apps.orders.models import CouponUsage, DiscountLine, Order
from . import constants as c
from .exceptions import (
    CouponCustomerNotEligible,
    CouponError,
    CouponExpired,
    CouponInactive,
    CouponMinSubtotalNotMet,
    CouponNotApplicableToItems,
    CouponNotFound,
    CouponNotYetValid,
    CouponUsageLimitReached,
    CouponUserLimitReached,
)
from .models import Coupon, CouponUsageRecord
from .selectors import (
    get_auto_applicable_coupons,
    get_coupon_by_code,
    get_coupon_cms_settings,
    get_user_coupon_redemption_count,
)

logger = logging.getLogger(c.LOGGER_NAME)

class CouponValidationService:
    """
    Evaluates coupon rules against a cart and customer to calculate exact discount amounts.
    """

    @classmethod
    def validate_and_calculate_discount(
        cls,
        coupon: Coupon,
        cart: Cart,
        customer: Optional[Any] = None,
    ) -> Tuple[Decimal, str]:
        """
        Validates all constraints and calculates the applicable discount amount.
        Returns tuple: (calculated_discount_amount, success_message)
        """
        if not coupon or not coupon.pk:
            raise CouponNotFound()

        now = timezone.now()
        if not coupon.is_active:
            raise CouponInactive()
        if coupon.valid_from and now < coupon.valid_from:
            raise CouponNotYetValid()
        if coupon.valid_to and now > coupon.valid_to:
            raise CouponExpired()
        if coupon.usage_limit_total and coupon.times_used >= coupon.usage_limit_total:
            raise CouponUsageLimitReached()

        user = customer if (customer and getattr(customer, "is_authenticated", False)) else getattr(cart, "customer", None)

        # 1. User Usage Limit Check
        if user and getattr(user, "is_authenticated", False):
            user_redemptions = get_user_coupon_redemption_count(user, coupon)
            if user_redemptions >= coupon.usage_limit_per_user:
                raise CouponUserLimitReached()

        # 2. Customer Scope Eligibility Check
        cls._verify_customer_eligibility(coupon, user)

        # 3. Minimum Cart Subtotal Check
        cart_subtotal = cart.subtotal
        if cart_subtotal < coupon.min_subtotal:
            raise CouponMinSubtotalNotMet(
                message=str(
                    _("Cart subtotal of NPR %(subtotal)s is below the minimum required NPR %(min)s for coupon '%(code)s'.")
                    % {"subtotal": cart_subtotal, "min": coupon.min_subtotal, "code": coupon.code}
                )
            )

        # 4. Product Target Scope & Items Qualification Check
        qualifying_items, qualifying_subtotal = cls._get_qualifying_cart_items(coupon, cart)
        if not qualifying_items or qualifying_subtotal <= c.ZERO_DECIMAL:
            raise CouponNotApplicableToItems()

        # 5. Calculate Discount Value Based on Type
        discount = cls._calculate_discount_amount(coupon, cart, qualifying_subtotal)
        discount = min(discount, cart_subtotal)
        discount = discount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        message = str(_("Coupon '%(code)s' applied successfully!") % {"code": coupon.code})
        return discount, message

    @classmethod
    def revalidate_cart_coupon(cls, cart: Cart) -> None:
        """
        Dynamically checks if an already-applied cart coupon code remains valid after item/quantity mutations.
        Adjusts or strips discount amounts automatically if requirements are no longer met.
        """
        if not cart or not getattr(cart, "pk", None) or not cart.coupon_code:
            return

        coupon = get_coupon_by_code(cart.coupon_code)
        if not coupon:
            cart.clear_coupon()
            return

        try:
            discount, _ = cls.validate_and_calculate_discount(coupon, cart, cart.customer)
            if discount != cart.coupon_discount_amount:
                cart.coupon_discount_amount = discount
                cart.save(update_fields=["coupon_discount_amount", "updated_at"])
        except CouponError:
            # Coupon constraints no longer met (e.g. subtotal dropped below min spend) -> Clear coupon
            cart.clear_coupon()

    @classmethod
    def _verify_customer_eligibility(cls, coupon: Coupon, user: Optional[Any]) -> None:
        if coupon.customer_scope == c.CustomerScope.ALL_CUSTOMERS:
            return

        if coupon.customer_scope == c.CustomerScope.FIRST_TIME_BUYERS:
            if not user or not getattr(user, "is_authenticated", False):
                raise CouponCustomerNotEligible(message=_("Sign in to verify eligibility for first-time buyer discount."))
            order_count = getattr(user, "orders", Order.objects.none()).filter(is_active=True).count()
            if order_count > 0:
                raise CouponCustomerNotEligible(message=_("This coupon is restricted to first-time buyers only."))

        elif coupon.customer_scope == c.CustomerScope.PREMIUM_MEMBERS:
            if not user or not getattr(user, "is_authenticated", False):
                raise CouponCustomerNotEligible(message=_("Sign in required to access member-exclusive coupons."))
            profile = getattr(user, "customer_profile", None)
            if not profile or not getattr(profile, "is_premium_member", False):
                raise CouponCustomerNotEligible(message=_("This coupon is reserved for subscribed / premium members."))

        elif coupon.customer_scope == c.CustomerScope.SPECIFIC_CUSTOMERS:
            if not user or not getattr(user, "is_authenticated", False):
                raise CouponCustomerNotEligible(message=_("Sign in to access assigned personal coupons."))
            if not coupon.target_customers.filter(pk=user.pk).exists():
                raise CouponCustomerNotEligible(message=_("Your account is not assigned to use this personal coupon."))

    @classmethod
    def _get_qualifying_cart_items(cls, coupon: Coupon, cart: Cart) -> Tuple[List[CartItem], Decimal]:
        active_items = list(cart.items.filter(status=CartItem.ItemStatus.ACTIVE).select_related("product", "product__category", "product__artisan"))
        if not active_items:
            return [], c.ZERO_DECIMAL

        if coupon.exclude_sale_items:
            active_items = [item for item in active_items if not getattr(item.product, "is_on_sale", False)]

        if coupon.target_scope == c.TargetScope.ALL_PRODUCTS:
            total = sum((item.unit_price_snapshot or c.ZERO_DECIMAL) * item.quantity for item in active_items)
            return active_items, Decimal(total)

        qualifying_items = []
        qualifying_subtotal = c.ZERO_DECIMAL

        for item in active_items:
            product = item.product
            if not product:
                continue

            matches = False
            if coupon.target_scope == c.TargetScope.SPECIFIC_PRODUCTS:
                matches = coupon.target_products.filter(pk=product.pk).exists()

            elif coupon.target_scope == c.TargetScope.SPECIFIC_CATEGORIES:
                if product.category_id:
                    matches = coupon.target_categories.filter(pk=product.category_id).exists()

            elif coupon.target_scope == c.TargetScope.SPECIFIC_ARTISANS:
                if product.artisan_id:
                    matches = coupon.target_artisans.filter(pk=product.artisan_id).exists()

            elif coupon.target_scope == c.TargetScope.SPECIFIC_COLLECTIONS:
                matches = coupon.target_collections.filter(products__pk=product.pk).exists()

            if matches:
                qualifying_items.append(item)
                qualifying_subtotal += (item.unit_price_snapshot or c.ZERO_DECIMAL) * item.quantity

        return qualifying_items, Decimal(qualifying_subtotal)

    @classmethod
    def _calculate_discount_amount(cls, coupon: Coupon, cart: Cart, qualifying_subtotal: Decimal) -> Decimal:
        if coupon.discount_type == c.DiscountType.PERCENTAGE:
            discount = qualifying_subtotal * (coupon.discount_value / Decimal("100.00"))
            if coupon.max_discount_amount and coupon.max_discount_amount > c.ZERO_DECIMAL:
                discount = min(discount, coupon.max_discount_amount)
            return discount

        elif coupon.discount_type == c.DiscountType.FIXED_AMOUNT:
            return min(coupon.discount_value, qualifying_subtotal)

        elif coupon.discount_type == c.DiscountType.FREE_SHIPPING:
            return cart.estimated_shipping

        elif coupon.discount_type == c.DiscountType.BUY_X_GET_Y:
            return min(coupon.discount_value, qualifying_subtotal)

        return c.ZERO_DECIMAL

class CouponApplicationService:
    """
    Applies and removes coupon codes from carts, handles auto-apply logic, and manages redemption records during checkout.
    """

    @classmethod
    @transaction.atomic
    def apply_coupon_to_cart(
        cls,
        cart: Cart,
        code: str,
        customer: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """
        Validates code and updates cart coupon properties.
        """
        clean_code = str(code or "").strip().upper()
        if not clean_code:
            return {
                "success": False,
                "code": c.ERROR_CODE_NOT_FOUND,
                "message": str(_("A coupon code is required.")),
            }

        coupon = get_coupon_by_code(clean_code)
        if not coupon:
            return {
                "success": False,
                "code": c.ERROR_CODE_NOT_FOUND,
                "message": str(_("Invalid coupon code '%(code)s'.") % {"code": clean_code}),
            }

        try:
            discount, message = CouponValidationService.validate_and_calculate_discount(coupon, cart, customer)
            cart.apply_coupon(code=coupon.code, discount_amount=discount)
            return {
                "success": True,
                "code": "coupon_applied",
                "message": message,
                "coupon_code": coupon.code,
                "discount_amount": str(discount),
                "grand_total": str(cart.grand_total),
            }
        except CouponError as exc:
            return {
                "success": False,
                "code": exc.code,
                "message": exc.message,
            }
        except Exception as exc:
            logger.exception("Unexpected error applying coupon '%s': %s", clean_code, exc)
            return {
                "success": False,
                "code": "system_error",
                "message": str(_("Failed to apply coupon due to a system error.")),
            }

    @classmethod
    @transaction.atomic
    def evaluate_and_auto_apply_best_coupon(cls, cart: Cart) -> Optional[Coupon]:
        """
        Scans all active auto-apply coupons and applies the one providing maximum savings if no manual coupon is set.
        """
        if not cart or not getattr(cart, "pk", None) or cart.coupon_code:
            return None

        cms_settings = get_coupon_cms_settings()
        if not cms_settings.enable_coupon_system or not cms_settings.auto_apply_best_coupon:
            return None

        auto_coupons = get_auto_applicable_coupons()
        best_coupon: Optional[Coupon] = None
        best_discount: Decimal = c.ZERO_DECIMAL

        for coupon in auto_coupons:
            try:
                discount, _ = CouponValidationService.validate_and_calculate_discount(coupon, cart, cart.customer)
                if discount > best_discount:
                    best_discount = discount
                    best_coupon = coupon
            except CouponError:
                continue

        if best_coupon and best_discount > c.ZERO_DECIMAL:
            cart.apply_coupon(code=best_coupon.code, discount_amount=best_discount)
            return best_coupon

        return None

    @classmethod
    @transaction.atomic
    def remove_coupon_from_cart(cls, cart: Cart) -> Dict[str, Any]:
        """
        Clears applied coupon from cart.
        """
        if not cart or not getattr(cart, "pk", None):
            return {"success": False, "message": str(_("Cart not found."))}

        cart.clear_coupon()
        return {
            "success": True,
            "code": "coupon_removed",
            "message": str(_("Coupon removed from shopping cart.")),
            "grand_total": str(cart.grand_total),
        }

    @classmethod
    @transaction.atomic
    def record_coupon_redemption_for_order(cls, order: Order, user: Any) -> Optional[CouponUsageRecord]:
        """
        Finalizes coupon redemption when an order is placed.
        """
        if not order or not order.coupon_code:
            return None

        coupon = get_coupon_by_code(order.coupon_code, use_cache=False)
        if not coupon:
            return None

        discount_amount = order.discount_total or c.ZERO_DECIMAL

        # Create Ledger Record
        record = CouponUsageRecord.objects.create(
            coupon=coupon,
            user=user,
            order=order,
            discount_amount=discount_amount,
        )

        # Create Order CouponUsage Link
        CouponUsage.objects.create(
            coupon_code=coupon.code,
            user=user,
            order=order,
            discount_amount=discount_amount,
        )

        # Create Order Discount Line
        DiscountLine.objects.create(
            order=order,
            discount_type=DiscountLine.DiscountType.COUPON,
            source="coupons_app",
            code=coupon.code,
            name=coupon.title,
            discount_amount=discount_amount,
            base_amount=order.subtotal,
        )

        # Increment Atomic Counter
        coupon.increment_usage(commit=True)
        return record

    @classmethod
    @transaction.atomic
    def reverse_coupon_redemption_for_order(cls, order: Order, reason: str = "") -> bool:
        """
        Reverses coupon redemption on order cancellation or refund.
        """
        records = CouponUsageRecord.objects.filter(order=order, is_reversed=False)
        if not records.exists():
            return False

        for record in records:
            record.is_reversed = True
            record.reversal_reason = reason or "Order cancelled/refunded."
            record.save(update_fields=["is_reversed", "reversal_reason", "updated_at"])
            record.coupon.decrement_usage(commit=True)

        CouponUsage.objects.filter(order=order).update(
            is_reversed=True,
            reversal_reason=reason or "Order cancelled/refunded.",
            reversed_at=timezone.now(),
        )

        return True

class CouponService:
    """
    Unified Facade providing backwards and cross-app compatibility for Order checkout views.
    """

    @classmethod
    def validate_coupon(cls, code: str, subtotal: Decimal, user: Any = None, cart: Optional[Cart] = None) -> Dict[str, Any]:
        coupon = get_coupon_by_code(code)
        if not coupon:
            return {"valid": False, "message": str(_("Invalid coupon code.")), "coupon": None}

        if cart:
            try:
                discount, message = CouponValidationService.validate_and_calculate_discount(coupon, cart, user)
                return {"valid": True, "discount_amount": discount, "message": message, "coupon": coupon}
            except CouponError as exc:
                return {"valid": False, "message": exc.message, "coupon": coupon}
        
        if coupon.is_valid_now and subtotal >= coupon.min_subtotal:
            return {"valid": True, "coupon": coupon}

        return {"valid": False, "message": str(_("Coupon requirements not met.")), "coupon": coupon}

    @classmethod
    def record_redemption(cls, coupon: Coupon, order: Order, user: Any, discount_amount: Decimal) -> Optional[CouponUsageRecord]:
        return CouponApplicationService.record_coupon_redemption_for_order(order=order, user=user)

    @classmethod
    def revalidate_cart(cls, cart: Cart) -> None:
        CouponValidationService.revalidate_cart_coupon(cart)