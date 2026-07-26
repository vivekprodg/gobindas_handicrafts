from decimal import Decimal
from typing import Final, Tuple

class ShippingRateType:
    FLAT_RATE: Final[str] = "flat_rate"
    WEIGHT_BASED: Final[str] = "weight_based"
    PRICE_BASED: Final[str] = "price_based"
    FREE_SHIPPING: Final[str] = "free_shipping"
    LOCAL_PICKUP: Final[str] = "local_pickup"

    CHOICES: Final[Tuple[Tuple[str, str], ...]] = (
        (FLAT_RATE, "Flat Rate Shipping"),
        (WEIGHT_BASED, "Weight-Based Calculation"),
        (PRICE_BASED, "Order Subtotal-Based Calculation"),
        (FREE_SHIPPING, "Free Shipping Threshold"),
        (LOCAL_PICKUP, "Local Gallery / Store Pickup"),
    )

class CarrierCode:
    NEPAL_POST: Final[str] = "nepal_post"
    DHL: Final[str] = "dhl"
    FEDEX: Final[str] = "fedex"
    UPS: Final[str] = "ups"
    ARAMEX: Final[str] = "aramex"
    LOCAL_COURIER: Final[str] = "local_courier"

    CHOICES: Final[Tuple[Tuple[str, str], ...]] = (
        (NEPAL_POST, "Nepal Post / EMS"),
        (DHL, "DHL Express International"),
        (FEDEX, "FedEx Express"),
        (UPS, "UPS Worldwide"),
        (ARAMEX, "Aramex Express"),
        (LOCAL_COURIER, "Local Kathmandu / Domestic Courier"),
    )

LOGGER_NAME: Final[str] = "apps.shipping"
CACHE_NAMESPACE: Final[str] = "shipping"
CACHE_KEY_GLOBAL_SETTINGS: Final[str] = "{ns}:global_settings:v1"
CACHE_KEY_SHIPPING_METHODS: Final[str] = "{ns}:methods_list:v1"
CACHE_TIMEOUT_SHIPPING: Final[int] = 1800  # 30 Minutes

DEFAULT_SHIPPING_CURRENCY: Final[str] = "USD"
DEFAULT_WEIGHT_UNIT: Final[str] = "kg"
ZERO_DECIMAL: Final[Decimal] = Decimal("0.00")