"""
Domain constants, choices, cache keys, and defaults for the Coupons app.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Final, FrozenSet, Tuple

class DiscountType:
    PERCENTAGE: Final[str] = "percentage"
    FIXED_AMOUNT: Final[str] = "fixed_amount"
    FREE_SHIPPING: Final[str] = "free_shipping"
    BUY_X_GET_Y: Final[str] = "buy_x_get_y"

    CHOICES: Final[Tuple[Tuple[str, str], ...]] = (
        (PERCENTAGE, "Percentage Discount (%)"),
        (FIXED_AMOUNT, "Fixed Amount Discount"),
        (FREE_SHIPPING, "Free Shipping Discount"),
        (BUY_X_GET_Y, "Buy X Get Y Free"),
    )

class TargetScope:
    ALL_PRODUCTS: Final[str] = "all"
    SPECIFIC_PRODUCTS: Final[str] = "products"
    SPECIFIC_CATEGORIES: Final[str] = "categories"
    SPECIFIC_ARTISANS: Final[str] = "artisans"
    SPECIFIC_COLLECTIONS: Final[str] = "collections"

    CHOICES: Final[Tuple[Tuple[str, str], ...]] = (
        (ALL_PRODUCTS, "Entire Store / All Products"),
        (SPECIFIC_PRODUCTS, "Specific Products Only"),
        (SPECIFIC_CATEGORIES, "Specific Categories Only"),
        (SPECIFIC_ARTISANS, "Specific Master Artisans Only"),
        (SPECIFIC_COLLECTIONS, "Specific Craft Collections Only"),
    )

class CustomerScope:
    ALL_CUSTOMERS: Final[str] = "all"
    FIRST_TIME_BUYERS: Final[str] = "first_time"
    PREMIUM_MEMBERS: Final[str] = "premium"
    SPECIFIC_CUSTOMERS: Final[str] = "specific"

    CHOICES: Final[Tuple[Tuple[str, str], ...]] = (
        (ALL_CUSTOMERS, "All Customers & Guests"),
        (FIRST_TIME_BUYERS, "First-Time Buyers Only"),
        (PREMIUM_MEMBERS, "Subscribed / Premium Members"),
        (SPECIFIC_CUSTOMERS, "Specific Customer Accounts"),
    )

LOGGER_NAME: Final[str] = "apps.coupons"
CACHE_NAMESPACE: Final[str] = "coupons"
CACHE_KEY_PUBLIC_COUPONS: Final[str] = "{ns}:public_list:v1"
CACHE_KEY_CMS_SETTINGS: Final[str] = "{ns}:cms_settings:v1"
CACHE_KEY_COUPON_DETAIL: Final[str] = "{ns}:code:{code}"
CACHE_TIMEOUT_PUBLIC: Final[int] = 1800  # 30 Minutes

DEFAULT_CURRENCY_CODE: Final[str] = "NPR"
ZERO_DECIMAL: Final[Decimal] = Decimal("0.00")
MAX_DISCOUNT_PERCENTAGE: Final[Decimal] = Decimal("100.00")

ERROR_CODE_NOT_FOUND: Final[str] = "coupon_not_found"
ERROR_CODE_INACTIVE: Final[str] = "coupon_inactive"
ERROR_CODE_EXPIRED: Final[str] = "coupon_expired"
ERROR_CODE_NOT_STARTED: Final[str] = "coupon_not_started"
ERROR_CODE_USAGE_EXCEEDED: Final[str] = "usage_limit_exceeded"
ERROR_CODE_USER_LIMIT_EXCEEDED: Final[str] = "user_limit_exceeded"
ERROR_CODE_MIN_SUBTOTAL: Final[str] = "min_subtotal_not_met"
ERROR_CODE_FIRST_TIME_ONLY: Final[str] = "first_time_buyers_only"
ERROR_CODE_NO_QUALIFYING_ITEMS: Final[str] = "no_qualifying_items"