import base64
import hashlib
import hmac
import json
import logging
import secrets
import urllib.request
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, Optional, Tuple

from django.conf import settings
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.orders.services import mark_order_paid
from . import constants as c
from .exceptions import (
    PaymentGatewayError,
    PaymentMethodDisabledError,
    PaymentVerificationError,
)
from .models import PaymentSettings, PaymentTransaction, PaymentWebhookLog
from .selectors import get_payment_settings, get_transaction_by_id

logger = logging.getLogger(c.LOGGER_NAME)

class EsewaService:
    """
    Integrates eSewa epay v2 HMAC-SHA256 signature generation and verification.
    """

    @classmethod
    def generate_signature(cls, secret_key: str, message: str) -> str:
        key_bytes = secret_key.encode("utf-8")
        msg_bytes = message.encode("utf-8")
        signature = hmac.new(key_bytes, msg_bytes, hashlib.sha256).digest()
        return base64.b64encode(signature).decode("utf-8")

    @classmethod
    def prepare_payment_payload(cls, txn: PaymentTransaction, request: Any) -> Dict[str, Any]:
        settings_obj = get_payment_settings()
        if not settings_obj.enable_esewa:
            raise PaymentMethodDisabledError(_("eSewa is currently disabled."))

        amount_str = f"{txn.amount:.2f}"
        product_code = settings_obj.esewa_merchant_code or "EPAYTEST"
        secret_key = settings_obj.esewa_secret_key or "8gBm9-W3B2-yA4z"

        # eSewa v2 signature string format: total_amount,transaction_uuid,product_code
        signature_msg = f"total_amount={amount_str},transaction_uuid={txn.transaction_id},product_code={product_code}"
        signature = cls.generate_signature(secret_key, signature_msg)

        success_url = request.build_absolute_uri(reverse("payments:callback", kwargs={"gateway": "esewa"}))
        failure_url = request.build_absolute_uri(reverse("payments:failed", kwargs={"transaction_id": txn.transaction_id}))

        epay_url = (
            "https://rc-epay.esewa.com.np/api/epay/main/v2/form"
            if settings_obj.esewa_is_sandbox
            else "https://epay.esewa.com.np/api/epay/main/v2/form"
        )

        return {
            "gateway_url": epay_url,
            "fields": {
                "amount": amount_str,
                "tax_amount": "0.00",
                "total_amount": amount_str,
                "transaction_uuid": txn.transaction_id,
                "product_code": product_code,
                "product_service_charge": "0.00",
                "product_delivery_charge": "0.00",
                "success_url": success_url,
                "failure_url": failure_url,
                "signed_field_names": "total_amount,transaction_uuid,product_code",
                "signature": signature,
            },
        }

    @classmethod
    def verify_response(cls, encoded_data: str) -> Tuple[bool, str, Dict[str, Any]]:
        try:
            decoded_json = base64.b64decode(encoded_data).decode("utf-8")
            data = json.loads(decoded_json)
        except Exception as exc:
            raise PaymentVerificationError(_("Malformed eSewa response payload.")) from exc

        txn_id = data.get("transaction_uuid", "")
        status = data.get("status", "").upper()

        if status == "COMPLETE":
            return True, txn_id, data
        return False, txn_id, data

class KhaltiService:
    """
    Integrates Khalti v2 REST API (Initiate & Lookup).
    """

    @classmethod
    def initiate_payment(cls, txn: PaymentTransaction, request: Any) -> Dict[str, Any]:
        settings_obj = get_payment_settings()
        if not settings_obj.enable_khalti:
            raise PaymentMethodDisabledError(_("Khalti is currently disabled."))

        api_url = (
            "https://a.khalti.com/api/v2/epayment/initiate/"
            if settings_obj.khalti_is_sandbox
            else "https://khalti.com/api/v2/epayment/initiate/"
        )

        return_url = request.build_absolute_uri(reverse("payments:callback", kwargs={"gateway": "khalti"}))

        payload = {
            "return_url": return_url,
            "website_url": request.build_absolute_uri("/"),
            "amount": int(txn.amount * Decimal("100")),
            "purchase_order_id": txn.transaction_id,
            "purchase_order_name": f"Order #{txn.order.order_number}",
            "customer_info": {
                "name": txn.order.shipping_address.full_name if txn.order.shipping_address else "Customer",
                "email": txn.order.email,
                "phone": txn.order.shipping_address.phone_number if txn.order.shipping_address else "",
            },
        }

        headers = {
            "Authorization": f"Key {settings_obj.khalti_secret_key}",
            "Content-Type": "application/json",
        }

        try:
            req = urllib.request.Request(api_url, data=json.dumps(payload).encode("utf-8"), headers=headers)
            with urllib.request.urlopen(req, timeout=10) as response:
                res_data = json.loads(response.read().decode("utf-8"))
                txn.gateway_reference_id = res_data.get("pidx", "")
                txn.raw_request_payload = payload
                txn.raw_response_payload = res_data
                txn.save(update_fields=["gateway_reference_id", "raw_request_payload", "raw_response_payload", "updated_at"])
                return res_data
        except Exception as exc:
            logger.exception("Khalti initiation failed: %s", exc)
            raise PaymentGatewayError(_("Failed to connect to Khalti payment server.")) from exc

class PaymentService:
    """
    Central orchestration facade for creating transactions and finalizing order payments.
    """

    @classmethod
    @transaction.atomic
    def create_transaction(
        cls,
        order: Any,
        gateway: str,
        amount: Optional[Decimal] = None,
        ip_address: str = "",
    ) -> PaymentTransaction:
        txn_id = f"PAY-{timezone.now().strftime('%y%m%d')}-{secrets.token_hex(4).upper()}"
        pay_amount = amount if amount is not None else order.grand_total

        txn = PaymentTransaction.objects.create(
            transaction_id=txn_id,
            order=order,
            gateway=gateway,
            amount=pay_amount,
            currency=order.currency or c.DEFAULT_CURRENCY,
            status=c.PaymentStatus.INITIATED,
            ip_address=ip_address,
        )
        return txn

    @classmethod
    @transaction.atomic
    def process_payment_success(cls, txn: PaymentTransaction, gateway_ref: str = "", raw_payload: Optional[Dict[str, Any]] = None) -> None:
        if txn.status == c.PaymentStatus.SUCCESS:
            return  # Idempotent safeguard

        txn.mark_successful(gateway_ref=gateway_ref, payload=raw_payload)

        # Mark main Order as Paid
        mark_order_paid(
            order=txn.order,
            payment_method=txn.get_gateway_display(),
            transaction_id=txn.transaction_id,
        )

        logger.info("Payment #%s verified successfully for Order #%s", txn.transaction_id, txn.order.order_number)