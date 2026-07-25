from decimal import Decimal
from typing import Final, Tuple

class PaymentGatewayCode:
    ESEWA: Final[str] = "esewa"
    KHALTI: Final[str] = "khalti"
    STRIPE: Final[str] = "stripe"
    COD: Final[str] = "cod"
    BANK_TRANSFER: Final[str] = "bank_transfer"

    CHOICES: Final[Tuple[Tuple[str, str], ...]] = (
        (ESEWA, "eSewa Mobile Wallet (Nepal)"),
        (KHALTI, "Khalti Digital Wallet (Nepal)"),
        (STRIPE, "Stripe Credit/Debit Card (International)"),
        (COD, "Cash on Delivery (COD)"),
        (BANK_TRANSFER, "Direct Bank Wire Transfer"),
    )

class PaymentStatus:
    PENDING: Final[str] = "pending"
    INITIATED: Final[str] = "initiated"
    PROCESSING: Final[str] = "processing"
    SUCCESS: Final[str] = "success"
    FAILED: Final[str] = "failed"
    REFUNDED: Final[str] = "refunded"
    PARTIALLY_REFUNDED: Final[str] = "partially_refunded"
    CANCELLED: Final[str] = "cancelled"

    CHOICES: Final[Tuple[Tuple[str, str], ...]] = (
        (PENDING, "Pending Payment"),
        (INITIATED, "Payment Initiated at Gateway"),
        (PROCESSING, "Processing Verification"),
        (SUCCESS, "Payment Successful / Captured"),
        (FAILED, "Payment Failed"),
        (REFUNDED, "Fully Refunded"),
        (PARTIALLY_REFUNDED, "Partially Refunded"),
        (CANCELLED, "Transaction Cancelled"),
    )

LOGGER_NAME: Final[str] = "apps.payments"
CACHE_NAMESPACE: Final[str] = "payments"
CACHE_KEY_GATEWAY_SETTINGS: Final[str] = "{ns}:gateway_settings:v1"
CACHE_TIMEOUT_PAYMENTS: Final[int] = 1800  # 30 Minutes

DEFAULT_CURRENCY: Final[str] = "NPR"
ZERO_DECIMAL: Final[Decimal] = Decimal("0.00")