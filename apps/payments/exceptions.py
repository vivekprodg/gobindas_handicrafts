from typing import Optional
from django.utils.translation import gettext_lazy as _

class PaymentError(Exception):
    """Base exception for payment domain errors."""
    default_code: str = "payment_error"
    default_message: str = _("A payment processing error occurred.")

    def __init__(self, message: Optional[str] = None, code: Optional[str] = None):
        self.message = str(message or self.default_message)
        self.code = str(code or self.default_code)
        super().__init__(self.message)

class PaymentGatewayError(PaymentError):
    default_code = "payment_gateway_error"
    default_message = _("The payment gateway responded with an error or invalid payload.")

class PaymentVerificationError(PaymentError):
    default_code = "payment_verification_error"
    default_message = _("Failed to verify transaction signature or authenticity.")

class DuplicateTransactionError(PaymentError):
    default_code = "duplicate_transaction"
    default_message = _("This payment transaction has already been processed.")

class PaymentMethodDisabledError(PaymentError):
    default_code = "payment_method_disabled"
    default_message = _("The selected payment method is currently disabled.")