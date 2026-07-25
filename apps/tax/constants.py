from decimal import Decimal
from typing import Final, Tuple

class TaxCalculationMode:
    EXCLUSIVE: Final[str] = "exclusive"
    INCLUSIVE: Final[str] = "inclusive"

    CHOICES: Final[Tuple[Tuple[str, str], ...]] = (
        (EXCLUSIVE, "Tax-Exclusive (Tax added on top of item price at checkout)"),
        (INCLUSIVE, "Tax-Inclusive (Item display price already includes tax)"),
    )

class TaxType:
    VAT: Final[str] = "vat"
    GST: Final[str] = "gst"
    SALES_TAX: Final[str] = "sales_tax"
    SERVICE_TAX: Final[str] = "service_tax"
    SURCHARGE: Final[str] = "surcharge"
    EXEMPT: Final[str] = "exempt"

    CHOICES: Final[Tuple[Tuple[str, str], ...]] = (
        (VAT, "Value Added Tax (VAT)"),
        (GST, "Goods & Services Tax (GST)"),
        (SALES_TAX, "State / Regional Sales Tax"),
        (SERVICE_TAX, "Service Charge / Tax"),
        (SURCHARGE, "Additional Surcharge / Luxury Tax"),
        (EXEMPT, "Tax-Exempt / Zero-Rated"),
    )

class TaxRateType:
    PERCENTAGE: Final[str] = "percentage"
    FLAT_AMOUNT: Final[str] = "flat_amount"

    CHOICES: Final[Tuple[Tuple[str, str], ...]] = (
        (PERCENTAGE, "Percentage Rate (%)"),
        (FLAT_AMOUNT, "Flat Fixed Fee Per Item"),
    )

LOGGER_NAME: Final[str] = "apps.tax"
CACHE_NAMESPACE: Final[str] = "tax"
CACHE_KEY_GLOBAL_SETTINGS: Final[str] = "{ns}:global_settings:v1"
CACHE_KEY_TAX_CLASSES: Final[str] = "{ns}:tax_classes:v1"
CACHE_KEY_TAX_ZONES: Final[str] = "{ns}:tax_zones:v1"
CACHE_TIMEOUT_TAX_CONFIG: Final[int] = 3600  # 30 Minutes

DEFAULT_TAX_CURRENCY: Final[str] = "NPR"
DEFAULT_NEPAL_VAT_RATE: Final[Decimal] = Decimal("13.00")
ZERO_DECIMAL: Final[Decimal] = Decimal("0.00")
MAX_TAX_RATE_PERCENTAGE: Final[Decimal] = Decimal("100.00")