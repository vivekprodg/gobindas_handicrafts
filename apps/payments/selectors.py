from decimal import Decimal
from typing import Any, Dict, List, Optional
from django.core.cache import cache
from django.db.models import QuerySet

from . import constants as c
from .models import PaymentSettings, PaymentTransaction

def get_payment_settings() -> PaymentSettings:
    """
    Fetches singleton PaymentSettings with caching.
    """
    key = c.CACHE_KEY_GATEWAY_SETTINGS.format(ns=c.CACHE_NAMESPACE)
    settings_obj = cache.get(key)
    if settings_obj is None:
        settings_obj, _ = PaymentSettings.objects.get_or_create(id=1)
        cache.set(key, settings_obj, c.CACHE_TIMEOUT_PAYMENTS)
    return settings_obj

def get_available_payment_gateways() -> List[Dict[str, Any]]:
    """
    Returns list of currently active payment options for checkout step 2 display.
    """
    settings_obj = get_payment_settings()
    gateways = []

    if settings_obj.enable_esewa:
        gateways.append({
            "code": c.PaymentGatewayCode.ESEWA,
            "title": "eSewa Mobile Wallet",
            "description": "Pay securely using your eSewa account (Nepal).",
            "is_instant": True,
        })

    if settings_obj.enable_khalti:
        gateways.append({
            "code": c.PaymentGatewayCode.KHALTI,
            "title": "Khalti Digital Wallet",
            "description": "Fast & secure digital payment via Khalti app/web.",
            "is_instant": True,
        })

    if settings_obj.enable_stripe:
        gateways.append({
            "code": c.PaymentGatewayCode.STRIPE,
            "title": "Credit / Debit Card (Stripe)",
            "description": "International card processing via Visa, MasterCard, Amex.",
            "is_instant": True,
        })

    if settings_obj.enable_cod:
        gateways.append({
            "code": c.PaymentGatewayCode.COD,
            "title": "Cash on Delivery (COD)",
            "description": settings_obj.cod_instructions or "Pay upon arrival.",
            "is_instant": False,
        })

    if settings_obj.enable_bank_transfer:
        gateways.append({
            "code": c.PaymentGatewayCode.BANK_TRANSFER,
            "title": "Direct Bank Wire Transfer",
            "description": settings_obj.bank_transfer_instructions or "Transfer directly to bank account.",
            "is_instant": False,
        })

    return gateways

def get_transaction_by_id(transaction_id: str) -> Optional[PaymentTransaction]:
    if not transaction_id:
        return None
    return PaymentTransaction.objects.filter(
        transaction_id=str(transaction_id).strip(),
    ).select_related("order").first()

def get_transactions_for_order(order: Any) -> QuerySet[PaymentTransaction]:
    if not order or not getattr(order, "pk", None):
        return PaymentTransaction.objects.none()
    return PaymentTransaction.objects.filter(order=order).order_by("-created_at")