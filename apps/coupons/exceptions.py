"""
Domain exceptions for coupon validation, evaluation, and redemption operations.
"""
from __future__ import annotations

from typing import Optional
from django.utils.translation import gettext_lazy as _
from . import constants as c

class CouponError(Exception):
    """Base exception for all coupon domain errors."""
    default_code: str = "coupon_error"
    default_message: str = _("A coupon processing error occurred.")

    def __init__(self, message: Optional[str] = None, code: Optional[str] = None):
        self.message = str(message or self.default_message)
        self.code = str(code or self.default_code)
        super().__init__(self.message)

class CouponNotFound(CouponError):
    default_code = c.ERROR_CODE_NOT_FOUND
    default_message = _("The specified coupon code does not exist.")

class CouponInactive(CouponError):
    default_code = c.ERROR_CODE_INACTIVE
    default_message = _("This coupon is currently inactive.")

class CouponExpired(CouponError):
    default_code = c.ERROR_CODE_EXPIRED
    default_message = _("This coupon has expired.")

class CouponNotYetValid(CouponError):
    default_code = c.ERROR_CODE_NOT_STARTED
    default_message = _("This coupon is not yet active for use.")

class CouponUsageLimitReached(CouponError):
    default_code = c.ERROR_CODE_USAGE_EXCEEDED
    default_message = _("This promotional coupon has reached its maximum usage limit.")

class CouponUserLimitReached(CouponError):
    default_code = c.ERROR_CODE_USER_LIMIT_EXCEEDED
    default_message = _("You have reached the maximum redemption limit for this coupon.")

class CouponMinSubtotalNotMet(CouponError):
    default_code = c.ERROR_CODE_MIN_SUBTOTAL
    default_message = _("Cart subtotal does not meet the minimum requirement for this coupon.")

class CouponNotApplicableToItems(CouponError):
    default_code = c.ERROR_CODE_NO_QUALIFYING_ITEMS
    default_message = _("None of the items in your bag qualify for this coupon discount.")

class CouponCustomerNotEligible(CouponError):
    default_code = c.ERROR_CODE_FIRST_TIME_ONLY
    default_message = _("Your account is not eligible for this targeted discount.")