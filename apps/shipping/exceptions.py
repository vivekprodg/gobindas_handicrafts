from typing import Optional
from django.utils.translation import gettext_lazy as _

class ShippingError(Exception):
    """Base exception for shipping domain errors."""
    default_code: str = "shipping_error"
    default_message: str = _("A shipping rate or fulfillment error occurred.")

    def __init__(self, message: Optional[str] = None, code: Optional[str] = None):
        self.message = str(message or self.default_message)
        self.code = str(code or self.default_code)
        super().__init__(self.message)

class ShippingZoneNotFoundError(ShippingError):
    default_code = "shipping_zone_not_found"
    default_message = _("No active shipping zone matches the destination address.")

class ShippingMethodNotFoundError(ShippingError):
    default_code = "shipping_method_not_found"
    default_message = _("No active delivery method is available for this zone.")

class WeightLimitExceededError(ShippingError):
    default_code = "weight_limit_exceeded"
    default_message = _("Package weight exceeds maximum carrier allowance.")

class TrackingNotFoundError(ShippingError):
    default_code = "tracking_not_found"
    default_message = _("The specified tracking reference number could not be found.")