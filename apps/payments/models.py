from decimal import Decimal
from typing import Any, Dict, Optional

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.foundation.models import CMSBaseModel, SingletonCMSModel
from .constants import DEFAULT_CURRENCY, PaymentGatewayCode, PaymentStatus, ZERO_DECIMAL

class PaymentSettings(SingletonCMSModel):
    """
    CMS-driven configurations and merchant credentials for all supported payment gateways.
    """
    # eSewa Config
    enable_esewa = models.BooleanField(
        default=True,
        verbose_name=_("Enable eSewa Wallet (Nepal)"),
    )
    esewa_merchant_code = models.CharField(
        max_length=100,
        default="EPAYTEST",
        verbose_name=_("eSewa Merchant Code"),
    )
    esewa_secret_key = models.CharField(
        max_length=255,
        default="8gBm9-W3B2-yA4z",
        verbose_name=_("eSewa Secret Key (HMAC SHA256)"),
    )
    esewa_is_sandbox = models.BooleanField(
        default=True,
        verbose_name=_("eSewa Sandbox Mode"),
    )

    # Khalti Config
    enable_khalti = models.BooleanField(
        default=True,
        verbose_name=_("Enable Khalti Wallet (Nepal)"),
    )
    khalti_public_key = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        verbose_name=_("Khalti Public Key"),
    )
    khalti_secret_key = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        verbose_name=_("Khalti Secret Key"),
    )
    khalti_is_sandbox = models.BooleanField(
        default=True,
        verbose_name=_("Khalti Sandbox Mode"),
    )

    # Stripe Config
    enable_stripe = models.BooleanField(
        default=True,
        verbose_name=_("Enable Stripe Credit/Debit Card (Global)"),
    )
    stripe_publishable_key = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        verbose_name=_("Stripe Publishable Key"),
    )
    stripe_secret_key = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        verbose_name=_("Stripe Secret Key"),
    )
    stripe_webhook_secret = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        verbose_name=_("Stripe Webhook Secret"),
    )

    # COD Config
    enable_cod = models.BooleanField(
        default=True,
        verbose_name=_("Enable Cash on Delivery (COD)"),
    )
    cod_instructions = models.TextField(
        default="Pay cash directly to our delivery representative upon order arrival.",
        blank=True,
        null=True,
        verbose_name=_("COD Instructions for Customer"),
    )

    # Direct Bank Transfer Config
    enable_bank_transfer = models.BooleanField(
        default=True,
        verbose_name=_("Enable Direct Bank Wire Transfer"),
    )
    bank_transfer_instructions = models.TextField(
        default="Bank Name: Nabil Bank Ltd.\nAccount Name: Gobindas Handicrafts Pvt. Ltd.\nAccount No: 0120017500101\nBranch: Kathmandu",
        blank=True,
        null=True,
        verbose_name=_("Bank Transfer Instructions"),
    )

    class Meta:
        verbose_name = _("Payment Gateway Settings")
        verbose_name_plural = _("Payment Gateway Settings")

    def __str__(self) -> str:
        return "Global Payment Gateway Settings"

class PaymentTransaction(CMSBaseModel):
    """
    Immutable payment transaction record matching customer order checkout payments.
    """
    transaction_id = models.CharField(
        max_length=100,
        unique=True,
        db_index=True,
        verbose_name=_("Transaction Reference ID"),
    )
    order = models.ForeignKey(
        "orders.Order",
        on_delete=models.CASCADE,
        related_name="payment_transactions",
        verbose_name=_("Order"),
    )
    gateway = models.CharField(
        max_length=50,
        choices=PaymentGatewayCode.CHOICES,
        default=PaymentGatewayCode.COD,
        db_index=True,
        verbose_name=_("Payment Gateway"),
    )
    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(ZERO_DECIMAL)],
        verbose_name=_("Amount Paid"),
    )
    currency = models.CharField(
        max_length=10,
        default=DEFAULT_CURRENCY,
        verbose_name=_("Currency"),
    )
    status = models.CharField(
        max_length=30,
        choices=PaymentStatus.CHOICES,
        default=PaymentStatus.PENDING,
        db_index=True,
        verbose_name=_("Transaction Status"),
    )
    gateway_reference_id = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        verbose_name=_("Gateway Unique Reference / PIDX / Intent ID"),
    )
    raw_request_payload = models.JSONField(
        default=dict,
        blank=True,
        verbose_name=_("Gateway Request Payload Snapshot"),
    )
    raw_response_payload = models.JSONField(
        default=dict,
        blank=True,
        verbose_name=_("Gateway Response Payload Snapshot"),
    )
    paid_at = models.DateTimeField(
        blank=True,
        null=True,
        verbose_name=_("Paid Timestamp"),
    )
    ip_address = models.GenericIPAddressField(
        blank=True,
        null=True,
        verbose_name=_("Customer IP Address"),
    )

    class Meta:
        verbose_name = _("Payment Transaction")
        verbose_name_plural = _("Payment Transactions")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["transaction_id", "status"]),
            models.Index(fields=["order", "status"]),
        ]

    def mark_successful(self, gateway_ref: str = "", payload: Optional[Dict[str, Any]] = None) -> None:
        self.status = PaymentStatus.SUCCESS
        self.paid_at = timezone.now()
        if gateway_ref:
            self.gateway_reference_id = gateway_ref
        if payload:
            self.raw_response_payload = payload
        self.save(update_fields=["status", "paid_at", "gateway_reference_id", "raw_response_payload", "updated_at"])

    def mark_failed(self, payload: Optional[Dict[str, Any]] = None) -> None:
        self.status = PaymentStatus.FAILED
        if payload:
            self.raw_response_payload = payload
        self.save(update_fields=["status", "raw_response_payload", "updated_at"])

    def __str__(self) -> str:
        return f"Transaction #{self.transaction_id} [{self.get_gateway_display()}] - {self.get_status_display()}"

class PaymentWebhookLog(CMSBaseModel):
    """
    Raw audit trail for incoming gateway webhook notifications.
    """
    gateway = models.CharField(
        max_length=50,
        choices=PaymentGatewayCode.CHOICES,
        verbose_name=_("Gateway Source"),
    )
    payload = models.JSONField(
        default=dict,
        verbose_name=_("Webhook Request Body"),
    )
    is_processed = models.BooleanField(
        default=False,
        verbose_name=_("Is Processed Successfully"),
    )
    error_message = models.TextField(
        blank=True,
        null=True,
        verbose_name=_("Error Details (if any)"),
    )

    class Meta:
        verbose_name = _("Payment Webhook Log")
        verbose_name_plural = _("Payment Webhook Logs")
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Webhook [{self.gateway}] at {self.created_at.strftime('%Y-%m-%d %H:%M:%S')}"