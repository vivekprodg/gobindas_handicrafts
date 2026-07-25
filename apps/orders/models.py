from __future__ import annotations

import hashlib
import secrets
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

DEFAULT_CURRENCY_CODE: str = "NPR"
DEFAULT_LOW_STOCK_THRESHOLD: int = 5
DEFAULT_ORDER_PAGE_SIZE: int = 25
DEFAULT_PAYMENT_METHOD: str = "manual"
DEFAULT_CARRIER_NAME: str = "Unknown"
DEFAULT_ORDER_ACTIVE_STATE: bool = True
DEFAULT_INVOICE_EXTENSION: str = ".pdf"
DEFAULT_SHIPPING_LABEL_EXTENSION: str = ".pdf"
DEFAULT_BINARY_EXTENSION: str = ".bin"
DEFAULT_LEGACY_EMAIL_PLACEHOLDER: str = "legacy-noemail@unknown.invalid"

_phone_validator = RegexValidator(
    regex=r"^\+?[0-9\s\-\(\)]{7,20}$",
    message=_("Phone number must be 7-20 characters with digits, spaces, hyphens, parentheses, and optional +."),
)

def _safe_suffix(filename: str, default_extension: str = DEFAULT_BINARY_EXTENSION) -> str:
    try:
        suffix = Path(str(filename or "")).suffix.lower()
    except Exception:
        suffix = ""
    if not suffix or len(suffix) > 10:
        suffix = default_extension
    if not suffix.startswith("."):
        suffix = f".{suffix}"
    return suffix

def _resolve_scope_id(instance: Any, fallback: str = "unknown") -> str:
    if instance is None:
        return fallback
    for attr in ("order_id", "return_request_id", "return_id", "shipment_id"):
        val = getattr(instance, attr, None)
        if val is not None:
            return str(val)
    nested_order = getattr(instance, "order", None)
    if nested_order and getattr(nested_order, "pk", None):
        return str(nested_order.pk)
    pk = getattr(instance, "pk", None)
    return str(pk) if pk is not None else fallback

def _upload_to_order_attachment(instance: Any, filename: str) -> str:
    suffix = _safe_suffix(filename, default_extension=DEFAULT_BINARY_EXTENSION)
    return f"orders/attachments/{_resolve_scope_id(instance)}/{uuid.uuid4().hex}{suffix}"

def _order_attachment_upload_path(instance: Any, filename: str) -> str:
    return _upload_to_order_attachment(instance, filename)

def _order_attachment_path(instance: Any, filename: str) -> str:
    return _upload_to_order_attachment(instance, filename)

def _upload_to_order_invoice(instance: Any, filename: str) -> str:
    suffix = _safe_suffix(filename, default_extension=DEFAULT_INVOICE_EXTENSION)
    return f"orders/invoices/{_resolve_scope_id(instance)}/{uuid.uuid4().hex}{suffix}"

def _order_invoice_upload_path(instance: Any, filename: str) -> str:
    return _upload_to_order_invoice(instance, filename)

def _upload_to_order_shipping_label(instance: Any, filename: str) -> str:
    suffix = _safe_suffix(filename, default_extension=DEFAULT_SHIPPING_LABEL_EXTENSION)
    return f"orders/shipping_labels/{_resolve_scope_id(instance)}/{uuid.uuid4().hex}{suffix}"

def _order_shipping_label_upload_path(instance: Any, filename: str) -> str:
    return _upload_to_order_shipping_label(instance, filename)

def _return_image_upload_path(instance: Any, filename: str) -> str:
    suffix = _safe_suffix(filename, default_extension=".webp")
    res_id = "unknown"
    if instance and getattr(instance, "return_item", None):
        res_id = _resolve_scope_id(instance.return_item)
    return f"orders/returns/{res_id}/{uuid.uuid4().hex}{suffix}"

def _return_image_path(instance: Any, filename: str) -> str:
    return _return_image_upload_path(instance, filename)

class OrderAddressSnapshot(models.Model):
    full_name = models.CharField(max_length=255, verbose_name=_("Full Name"))
    phone_number = models.CharField(max_length=50, verbose_name=_("Phone Number"))
    company = models.CharField(max_length=255, blank=True, default="", verbose_name=_("Company"))
    address_line_1 = models.CharField(max_length=255, verbose_name=_("Address Line 1"))
    address_line_2 = models.CharField(max_length=255, blank=True, default="", verbose_name=_("Address Line 2"))
    city = models.CharField(max_length=100, verbose_name=_("City"))
    state_or_province = models.CharField(max_length=100, verbose_name=_("State or Province"))
    postal_code = models.CharField(max_length=50, verbose_name=_("Postal Code"))
    country = models.CharField(max_length=100, verbose_name=_("Country"))
    country_code = models.CharField(max_length=2, blank=True, null=True, verbose_name=_("ISO Country Code"))
    phone_e164 = models.CharField(max_length=20, blank=True, null=True, verbose_name=_("Phone (E.164)"))
    latitude = models.DecimalField(max_digits=9, decimal_places=6, blank=True, null=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, blank=True, null=True)
    delivery_notes = models.TextField(blank=True, null=True, verbose_name=_("Delivery Notes"))
    address_hash = models.CharField(max_length=64, blank=True, null=True, db_index=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("Order Address Snapshot")
        verbose_name_plural = _("Order Address Snapshots")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["country", "city"]),
            models.Index(fields=["postal_code", "country"]),
        ]

    def __str__(self) -> str:
        return f"{self.full_name} - {self.city}, {self.country}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self.address_hash:
            parts = [
                (self.full_name or "").strip().lower(),
                (self.phone_number or "").strip().lower(),
                (self.address_line_1 or "").strip().lower(),
                (self.address_line_2 or "").strip().lower(),
                (self.city or "").strip().lower(),
                (self.state_or_province or "").strip().lower(),
                (self.postal_code or "").strip().lower(),
                (self.country or "").strip().lower(),
            ]
            self.address_hash = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
        super().save(*args, **kwargs)

class Order(models.Model):
    class OrderStatus(models.TextChoices):
        PENDING = "pending", _("Pending")
        PROCESSING = "processing", _("Processing")
        SHIPPED = "shipped", _("Shipped")
        DELIVERED = "delivered", _("Delivered")
        CANCELLED = "cancelled", _("Cancelled")
        REFUNDED = "refunded", _("Refunded")
        ON_HOLD = "on_hold", _("On Hold")
        PARTIALLY_SHIPPED = "partially_shipped", _("Partially Shipped")
        PARTIALLY_DELIVERED = "partially_delivered", _("Partially Delivered")
        BACKORDERED = "backordered", _("Backordered")
        COMPLETED = "completed", _("Completed")
        FAILED = "failed", _("Failed")
        AWAITING_PAYMENT = "awaiting_payment", _("Awaiting Payment")
        PARTIALLY_REFUNDED = "partially_refunded", _("Partially Refunded")
        DISPUTED = "disputed", _("Disputed")
        DRAFT = "draft", _("Draft")

    class PaymentStatus(models.TextChoices):
        PENDING = "pending", _("Pending")
        PARTIALLY_PAID = "partially_paid", _("Partially Paid")
        PAID = "paid", _("Paid")
        FAILED = "failed", _("Failed")
        REFUNDED = "refunded", _("Refunded")
        PARTIALLY_REFUNDED = "partially_refunded", _("Partially Refunded")
        AUTHORIZED = "authorized", _("Authorized")
        CAPTURED = "captured", _("Captured")
        VOIDED = "voided", _("Voided")
        DISPUTED = "disputed", _("Disputed")
        EXPIRED = "expired", _("Expired")
        PENDING_PAYMENT = "pending_payment", _("Pending Payment")
        PROCESSING = "processing", _("Processing")

    class Source(models.TextChoices):
        WEB = "web", _("Web Storefront")
        ADMIN = "admin", _("Admin / Staff")
        API = "api", _("API")
        IMPORT = "import", _("Bulk Import")
        PHONE = "phone", _("Phone Order")
        MARKETPLACE = "marketplace", _("Marketplace")
        SUBSCRIPTION = "subscription", _("Subscription Renewal")
        POS = "pos", _("Point of Sale")
        MIGRATION = "migration", _("Data Migration")
        OTHER = "other", _("Other")

    class FraudCheckStatus(models.TextChoices):
        NOT_CHECKED = "not_checked", _("Not Checked")
        PENDING = "pending", _("Pending")
        PASSED = "passed", _("Passed")
        FAILED = "failed", _("Failed")
        MANUAL_REVIEW = "manual_review", _("Under Manual Review")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order_number = models.CharField(max_length=50, unique=True, db_index=True, verbose_name=_("Order Number"))
    customer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, related_name="orders", null=True, blank=True)
    email = models.EmailField(null=True, blank=True, verbose_name=_("Order Email"))
    shipping_address = models.OneToOneField(OrderAddressSnapshot, on_delete=models.PROTECT, related_name="+", null=True, blank=True)
    billing_address = models.OneToOneField(OrderAddressSnapshot, on_delete=models.PROTECT, related_name="+", null=True, blank=True)

    status = models.CharField(max_length=20, choices=OrderStatus.choices, default=OrderStatus.PENDING, db_index=True)
    payment_status = models.CharField(max_length=20, choices=PaymentStatus.choices, default=PaymentStatus.PENDING, db_index=True)
    payment_method = models.CharField(max_length=100, blank=True)
    transaction_id = models.CharField(max_length=255, blank=True)
    currency = models.CharField(max_length=10, default=DEFAULT_CURRENCY_CODE)

    subtotal = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"), validators=[MinValueValidator(Decimal("0.00"))])
    discount_total = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"), validators=[MinValueValidator(Decimal("0.00"))])
    shipping_cost = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"), validators=[MinValueValidator(Decimal("0.00"))])
    tax_total = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"), validators=[MinValueValidator(Decimal("0.00"))])
    total = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"), validators=[MinValueValidator(Decimal("0.00"))])

    coupon_code = models.CharField(max_length=50, blank=True)
    customer_note = models.TextField(blank=True)
    has_invoice = models.BooleanField(default=False)
    invoice_url = models.URLField(max_length=500, blank=True)
    tracking_number = models.CharField(max_length=100, blank=True)
    carrier = models.CharField(max_length=100, blank=True)
    delivery_instructions = models.TextField(blank=True)
    is_active = models.BooleanField(default=DEFAULT_ORDER_ACTIVE_STATE, db_index=True)
    json_metadata = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    currency_symbol = models.CharField(max_length=10, blank=True, null=True)
    exchange_rate = models.DecimalField(max_digits=18, decimal_places=8, default=Decimal("1.00000000"), validators=[MinValueValidator(Decimal("0.00000001"))])
    base_currency = models.CharField(max_length=10, blank=True, null=True)
    base_currency_total = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0.00"), validators=[MinValueValidator(Decimal("0.00"))])

    customer_ip = models.GenericIPAddressField(null=True, blank=True)
    customer_user_agent = models.TextField(blank=True, null=True)
    referrer_url = models.URLField(max_length=500, blank=True, null=True)
    customer_locale = models.CharField(max_length=16, blank=True, null=True)
    customer_timezone = models.CharField(max_length=64, blank=True, null=True)

    is_gift = models.BooleanField(default=False, db_index=True)
    gift_message = models.TextField(blank=True, null=True)
    gift_wrapping = models.CharField(max_length=120, blank=True, null=True)
    personalization_data = models.JSONField(default=dict, blank=True)

    source = models.CharField(max_length=32, choices=Source.choices, default=Source.WEB, db_index=True)
    external_order_id = models.CharField(max_length=120, blank=True, null=True, db_index=True)
    external_platform = models.CharField(max_length=64, blank=True, null=True)
    fraud_check_status = models.CharField(max_length=32, choices=FraudCheckStatus.choices, default=FraudCheckStatus.NOT_CHECKED, db_index=True)
    risk_score = models.DecimalField(max_digits=5, decimal_places=2, blank=True, null=True, validators=[MinValueValidator(Decimal("0.00")), MaxValueValidator(Decimal("100.00"))])
    tags = models.JSONField(default=list, blank=True)
    expected_delivery_date = models.DateField(null=True, blank=True)
    abandoned_at = models.DateTimeField(null=True, blank=True)
    abandoned_recovery_sent_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True, null=True)
    tags_text = models.TextField(blank=True, null=True)

    class Meta:
        verbose_name = _("Order")
        verbose_name_plural = _("Orders")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "payment_status"]),
            models.Index(fields=["customer", "-created_at"]),
            models.Index(fields=["order_number"]),
            models.Index(fields=["email"]),
            models.Index(fields=["-created_at"]),
            models.Index(fields=["status", "-created_at"]),
            models.Index(fields=["payment_status", "-created_at"]),
            models.Index(fields=["source", "-created_at"]),
            models.Index(fields=["fraud_check_status", "-created_at"]),
            models.Index(fields=["is_gift", "-created_at"]),
            models.Index(fields=["customer_ip", "-created_at"]),
            models.Index(fields=["external_order_id", "external_platform"]),
        ]
        constraints = [
            models.UniqueConstraint(fields=["customer", "order_number"], name="unique_customer_order_number"),
        ]

    def __str__(self) -> str:
        return f"Order {self.order_number}"

    def get_email(self) -> str:
        return (self.email or "").strip().lower() or DEFAULT_LEGACY_EMAIL_PLACEHOLDER

    def has_real_email(self) -> bool:
        c = (self.email or "").strip().lower()
        return bool(c) and c != DEFAULT_LEGACY_EMAIL_PLACEHOLDER

    def clean(self) -> None:
        super().clean()
        if self.subtotal < 0 or self.total < 0:
            raise ValidationError(_("Subtotal and Total cannot be negative."))
        if self.discount_total and self.discount_total > self.subtotal:
            raise ValidationError(_("Discount cannot exceed subtotal."))
        if not self.email or not str(self.email).strip():
            raise ValidationError({"email": _("A valid email address is required.")})

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.full_clean()
        super().save(*args, **kwargs)

    @property
    def grand_total(self) -> Decimal:
        disc = self.discount_total or Decimal("0.00")
        ship = self.shipping_cost or Decimal("0.00")
        tax = self.tax_total or Decimal("0.00")
        return max(Decimal("0.00"), self.subtotal - disc + ship + tax)

    @property
    def is_paid(self) -> bool:
        return self.payment_status == self.PaymentStatus.PAID

    @property
    def is_completed(self) -> bool:
        return self.status in (self.OrderStatus.DELIVERED, self.OrderStatus.COMPLETED)

    @property
    def can_be_cancelled(self) -> bool:
        return self.status in (self.OrderStatus.PENDING, self.OrderStatus.AWAITING_PAYMENT, self.OrderStatus.ON_HOLD)

    @property
    def can_be_refunded(self) -> bool:
        return self.is_paid and self.status != self.OrderStatus.REFUNDED

    def mark_completed(self) -> None:
        self.status = self.OrderStatus.COMPLETED
        self.completed_at = timezone.now()
        self.save(update_fields=["status", "completed_at", "updated_at"])

class OrderItem(models.Model):
    class ItemStatus(models.TextChoices):
        ACTIVE = "active", _("Active")
        SAVED = "saved", _("Saved For Later")
        REMOVED = "removed", _("Removed")
        EXPIRED = "expired", _("Expired")
        RETURNED = "returned", _("Returned")
        REFUNDED = "refunded", _("Refunded")
        CANCELLED = "cancelled", _("Cancelled")
        PARTIALLY_RETURNED = "partially_returned", _("Partially Returned")
        PARTIALLY_SHIPPED = "partially_shipped", _("Partially Shipped")

    class SavedForLaterReason(models.TextChoices):
        MANUAL = "manual", _("Manually Saved")
        REPLACED = "replaced", _("Replaced By Newer Variant")
        STOCK_OUT = "out_of_stock", _("Variant Out Of Stock")

    id = models.BigAutoField(primary_key=True)
    order = models.ForeignKey(Order, related_name="items", on_delete=models.CASCADE)
    product = models.ForeignKey("catalog.Product", on_delete=models.SET_NULL, null=True, blank=True, related_name="order_item_product_set")
    variant = models.ForeignKey("catalog.ProductVariant", on_delete=models.SET_NULL, null=True, blank=True, related_name="order_item_variant_set")

    product_name_snapshot = models.CharField(max_length=255, blank=True, null=True)
    product_sku_snapshot = models.CharField(max_length=100, blank=True, null=True)
    variant_name_snapshot = models.CharField(max_length=255, blank=True, null=True)

    unit_price = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"), validators=[MinValueValidator(Decimal("0.00"))])
    discount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"), validators=[MinValueValidator(Decimal("0.00"))])
    tax = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"), validators=[MinValueValidator(Decimal("0.00"))])
    line_total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"), validators=[MinValueValidator(Decimal("0.00"))])
    weight = models.DecimalField(max_digits=10, decimal_places=3, default=Decimal("0.000"), validators=[MinValueValidator(Decimal("0.000"))])

    attributes = models.JSONField(default=dict, blank=True)
    personalization = models.JSONField(default=dict, blank=True)
    quantity = models.PositiveIntegerField(default=1, validators=[MinValueValidator(1)])
    status = models.CharField(max_length=20, choices=ItemStatus.choices, default=ItemStatus.ACTIVE, db_index=True)
    saved_reason = models.CharField(max_length=32, choices=SavedForLaterReason.choices, blank=True, null=True)

    product_image_snapshot_url = models.URLField(max_length=500, blank=True, null=True)
    product_slug_snapshot = models.SlugField(max_length=255, blank=True, null=True)
    product_meta_title_snapshot = models.CharField(max_length=255, blank=True, null=True)
    product_meta_description_snapshot = models.TextField(blank=True, null=True)
    product_brand_snapshot = models.CharField(max_length=120, blank=True, null=True)
    product_origin_snapshot = models.CharField(max_length=120, blank=True, null=True)
    variant_sku_snapshot = models.CharField(max_length=100, blank=True, null=True)
    variant_barcode_snapshot = models.CharField(max_length=100, blank=True, null=True)
    variant_image_snapshot_url = models.URLField(max_length=500, blank=True, null=True)
    variant_weight_snapshot = models.DecimalField(max_digits=10, decimal_places=3, blank=True, null=True, validators=[MinValueValidator(Decimal("0.000"))])

    inventory = models.ForeignKey("inventory.Inventory", on_delete=models.SET_NULL, null=True, blank=True, related_name="order_items")
    inventory_reservation = models.ForeignKey("inventory.StockReservation", on_delete=models.SET_NULL, null=True, blank=True, related_name="order_items")
    warehouse = models.ForeignKey("inventory.Warehouse", on_delete=models.PROTECT, null=True, blank=True, related_name="order_items")
    warehouse_name_snapshot = models.CharField(max_length=120, blank=True, null=True)
    warehouse_code_snapshot = models.CharField(max_length=50, blank=True, null=True)

    is_gift = models.BooleanField(default=False, db_index=True)
    gift_message = models.TextField(blank=True, null=True)
    gift_wrapping = models.CharField(max_length=120, blank=True, null=True)
    expected_ship_date = models.DateField(blank=True, null=True)
    promised_delivery_date = models.DateField(blank=True, null=True)

    supplier_name_snapshot = models.CharField(max_length=255, blank=True, null=True)
    supplier_order_id = models.CharField(max_length=120, blank=True, null=True)

    quantity_shipped = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"), validators=[MinValueValidator(Decimal("0.00"))])
    quantity_returned = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"), validators=[MinValueValidator(Decimal("0.00"))])
    quantity_refunded = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"), validators=[MinValueValidator(Decimal("0.00"))])

    metadata = models.JSONField(default=dict, blank=True)
    added_at = models.DateTimeField(null=True, blank=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    saved_at = models.DateTimeField(null=True, blank=True)
    moved_to_save_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = _("Order Item")
        verbose_name_plural = _("Order Items")
        ordering = ["added_at"]
        indexes = [
            models.Index(fields=["order", "status"]),
            models.Index(fields=["order", "product", "variant", "status"]),
            models.Index(fields=["order", "status", "updated_at"]),
            models.Index(fields=["status", "added_at"]),
            models.Index(fields=["order", "is_gift"]),
            models.Index(fields=["order", "warehouse"]),
            models.Index(fields=["order", "inventory"]),
            models.Index(fields=["order", "expected_ship_date"]),
        ]
        constraints = [
            models.UniqueConstraint(fields=["order", "product", "variant", "status"], condition=~models.Q(variant__isnull=True), name="order_unique_product_variant_active"),
            models.UniqueConstraint(fields=["order", "product", "status"], condition=models.Q(variant__isnull=True), name="order_unique_product_no_variant_active"),
            models.CheckConstraint(check=models.Q(quantity__gte=1), name="orderitem_quantity_gte_1"),
            models.CheckConstraint(check=models.Q(unit_price__gte=0), name="orderitem_unit_price_gte_0"),
            models.CheckConstraint(check=models.Q(quantity_shipped__lte=models.F("quantity")), name="orderitem_quantity_shipped_lte_quantity"),
            models.CheckConstraint(check=models.Q(quantity_returned__lte=models.F("quantity")), name="orderitem_quantity_returned_lte_quantity"),
            models.CheckConstraint(check=models.Q(quantity_refunded__lte=models.F("quantity")), name="orderitem_quantity_refunded_lte_quantity"),
        ]

    def __str__(self) -> str:
        v = f" ({self.variant_name_snapshot})" if self.variant_name_snapshot else ""
        return f"{self.quantity} x {self.product_name_snapshot or 'Unnamed'}{v}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        if self.pk is None and self.added_at is None:
            self.added_at = timezone.now()
        self.full_clean()
        super().save(*args, **kwargs)

class OrderStatusHistory(models.Model):
    id = models.BigAutoField(primary_key=True)
    order = models.ForeignKey(Order, related_name="status_history", on_delete=models.CASCADE)
    old_status = models.CharField(max_length=50)
    new_status = models.CharField(max_length=50)
    remarks = models.TextField(blank=True)
    is_customer_notified = models.BooleanField(default=False, db_index=True)
    notification_method = models.CharField(max_length=32, blank=True, null=True)
    metadata = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True, null=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = _("Order Status History")
        verbose_name_plural = _("Order Status Histories")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["order", "-created_at"]),
            models.Index(fields=["new_status", "-created_at"]),
        ]

class Shipment(models.Model):
    class ShipmentStatus(models.TextChoices):
        PENDING = "pending", _("Pending")
        DISPATCHED = "dispatched", _("Dispatched")
        IN_TRANSIT = "in_transit", _("In Transit")
        DELIVERED = "delivered", _("Delivered")
        RETURNED = "returned", _("Returned")
        EXCEPTION = "exception", _("Exception")
        OUT_FOR_DELIVERY = "out_for_delivery", _("Out for Delivery")
        FAILED_ATTEMPT = "failed_attempt", _("Failed Delivery Attempt")
        AWAITING_PICKUP = "awaiting_pickup", _("Awaiting Pickup")
        PICKED_UP = "picked_up", _("Picked Up")

    id = models.BigAutoField(primary_key=True)
    order = models.ForeignKey(Order, related_name="shipments", on_delete=models.CASCADE)
    shipment_number = models.CharField(max_length=100, unique=True, db_index=True)
    carrier = models.CharField(max_length=100)
    tracking_number = models.CharField(max_length=150, blank=True, null=True, db_index=True)
    tracking_url = models.URLField(max_length=500, blank=True, null=True)
    warehouse = models.ForeignKey("inventory.Warehouse", on_delete=models.PROTECT, null=True, blank=True, related_name="shipments")
    status = models.CharField(max_length=30, choices=ShipmentStatus.choices, default=ShipmentStatus.PENDING, db_index=True)
    shipping_cost = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"), validators=[MinValueValidator(Decimal("0.00"))])
    dispatch_date = models.DateTimeField(null=True, blank=True)
    delivery_date = models.DateTimeField(null=True, blank=True)
    picked_up_at = models.DateTimeField(null=True, blank=True)
    estimated_delivery_date = models.DateField(null=True, blank=True)
    actual_delivery_date = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True, null=True)
    carrier_api_integration_id = models.CharField(max_length=120, blank=True, null=True)
    carrier_service_level = models.CharField(max_length=64, blank=True, null=True)
    total_weight = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0.000"), validators=[MinValueValidator(Decimal("0.000"))])
    dimensions = models.JSONField(default=dict, blank=True)
    shipping_cost_breakdown = models.JSONField(default=dict, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Shipment")
        verbose_name_plural = _("Shipments")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["order", "-created_at"]),
            models.Index(fields=["status", "dispatch_date"]),
            models.Index(fields=["carrier", "tracking_number"]),
            models.Index(fields=["warehouse", "status"]),
        ]

class ShipmentItem(models.Model):
    id = models.BigAutoField(primary_key=True)
    shipment = models.ForeignKey(Shipment, related_name="line_items", on_delete=models.CASCADE)
    order_item = models.ForeignKey(OrderItem, related_name="shipment_line_items", on_delete=models.PROTECT)
    quantity_shipped = models.DecimalField(max_digits=14, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])
    serial_tracking = models.CharField(max_length=120, blank=True, null=True)
    serial_verified_at = models.DateTimeField(null=True, blank=True)
    condition_at_pickup = models.CharField(max_length=32, blank=True, null=True)
    is_replacement = models.BooleanField(default=False, db_index=True)
    replaced_from = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True, related_name="replacements")
    notes = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Shipment Line Item")
        verbose_name_plural = _("Shipment Line Items")
        ordering = ["shipment", "id"]

class Payment(models.Model):
    class PaymentState(models.TextChoices):
        PENDING = "pending", _("Pending")
        COMPLETED = "completed", _("Completed")
        FAILED = "failed", _("Failed")
        REFUNDED = "refunded", _("Refunded")
        AUTHORIZED = "authorized", _("Authorized")
        CAPTURED = "captured", _("Captured")
        PARTIALLY_REFUNDED = "partially_refunded", _("Partially Refunded")
        VOIDED = "voided", _("Voided")
        EXPIRED = "expired", _("Expired")
        DISPUTED = "disputed", _("Disputed")

    id = models.BigAutoField(primary_key=True)
    order = models.ForeignKey(Order, related_name="payments", on_delete=models.CASCADE)
    transaction_id = models.CharField(max_length=255, unique=True, db_index=True)
    gateway = models.CharField(max_length=100)
    amount = models.DecimalField(max_digits=14, decimal_places=2, validators=[MinValueValidator(Decimal("0.00"))])
    currency = models.CharField(max_length=10, default=DEFAULT_CURRENCY_CODE)
    status = models.CharField(max_length=20, choices=PaymentState.choices, default=PaymentState.PENDING, db_index=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    payment_method = models.CharField(max_length=64, blank=True, null=True)
    payment_attempts_count = models.PositiveIntegerField(default=1)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    next_attempt_allowed_at = models.DateTimeField(null=True, blank=True)
    gateway_response_snapshot = models.JSONField(default=dict, blank=True)
    risk_score = models.DecimalField(max_digits=5, decimal_places=2, blank=True, null=True)
    is_test_payment = models.BooleanField(default=False, db_index=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Payment")
        verbose_name_plural = _("Payments")
        ordering = ["-created_at"]

class PaymentAttempt(models.Model):
    class AttemptStatus(models.TextChoices):
        PENDING = "pending", _("Pending")
        SUCCESS = "success", _("Success")
        FAILURE = "failure", _("Failure")
        TIMEOUT = "timeout", _("Timeout")
        CANCELLED = "cancelled", _("Cancelled")

    id = models.BigAutoField(primary_key=True)
    payment = models.ForeignKey(Payment, related_name="attempts", on_delete=models.CASCADE)
    attempted_at = models.DateTimeField(default=timezone.now, db_index=True)
    attempt_number = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=32, choices=AttemptStatus.choices, default=AttemptStatus.PENDING, db_index=True)
    gateway_response_code = models.CharField(max_length=64, blank=True, null=True)
    gateway_response_message = models.TextField(blank=True, null=True)
    gateway_response_snapshot = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True, null=True)
    is_test = models.BooleanField(default=False, db_index=True)
    notes = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("Payment Attempt")
        verbose_name_plural = _("Payment Attempts")
        ordering = ["-attempted_at"]

class Refund(models.Model):
    class RefundStatus(models.TextChoices):
        REQUESTED = "requested", _("Requested")
        APPROVED = "approved", _("Approved")
        PROCESSED = "processed", _("Processed")
        REJECTED = "rejected", _("Rejected")
        PENDING = "pending", _("Pending Gateway")
        FAILED = "failed", _("Gateway Failed")
        CANCELLED = "cancelled", _("Cancelled")

    class RefundMethod(models.TextChoices):
        ORIGINAL = "original", _("Original Payment Method")
        STORE_CREDIT = "store_credit", _("Store Credit")
        BANK_TRANSFER = "bank_transfer", _("Bank Transfer")
        CHECK = "check", _("Check")
        CASH = "cash", _("Cash")
        OTHER = "other", _("Other")

    class RefundReasonCategory(models.TextChoices):
        CUSTOMER_REQUEST = "customer_request", _("Customer Request")
        DEFECTIVE_PRODUCT = "defective_product", _("Defective Product")
        WRONG_ITEM = "wrong_item", _("Wrong Item Shipped")
        NOT_AS_DESCRIBED = "not_as_described", _("Not as Described")
        DUPLICATE_CHARGE = "duplicate_charge", _("Duplicate Charge")
        FRAUD = "fraud", _("Fraudulent Transaction")
        GOODWILL = "goodwill", _("Goodwill Gesture")
        OTHER = "other", _("Other")

    id = models.BigAutoField(primary_key=True)
    order = models.ForeignKey(Order, related_name="refunds", on_delete=models.CASCADE)
    payment = models.ForeignKey(Payment, related_name="refunds", on_delete=models.PROTECT)
    amount = models.DecimalField(max_digits=14, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])
    reason = models.TextField()
    status = models.CharField(max_length=20, choices=RefundStatus.choices, default=RefundStatus.REQUESTED, db_index=True)
    refund_method = models.CharField(max_length=32, choices=RefundMethod.choices, blank=True, null=True)
    refund_reason_category = models.CharField(max_length=64, choices=RefundReasonCategory.choices, blank=True, null=True)
    customer_notes = models.TextField(blank=True, null=True)
    internal_notes = models.TextField(blank=True, null=True)
    evidence_images = models.JSONField(default=list, blank=True)
    gateway_refund_id = models.CharField(max_length=120, blank=True, null=True)
    metadata = models.JSONField(default=dict, blank=True)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Refund")
        verbose_name_plural = _("Refunds")
        ordering = ["-created_at"]

class TaxLine(models.Model):
    class TaxMode(models.TextChoices):
        INCLUSIVE = "inclusive", _("Inclusive")
        EXCLUSIVE = "exclusive", _("Exclusive")

    id = models.BigAutoField(primary_key=True)
    order = models.ForeignKey(Order, related_name="tax_lines", on_delete=models.CASCADE)
    tax_class = models.CharField(max_length=64, db_index=True)
    tax_name = models.CharField(max_length=120)
    tax_rate = models.DecimalField(max_digits=8, decimal_places=4)
    base_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    tax_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    jurisdiction = models.CharField(max_length=120, blank=True, null=True)
    tax_authority_code = models.CharField(max_length=64, blank=True, null=True)
    is_inclusive = models.BooleanField(default=False)
    mode = models.CharField(max_length=16, choices=TaxMode.choices, default=TaxMode.EXCLUSIVE)
    position = models.PositiveIntegerField(default=0)
    notes = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("Order Tax Line")
        verbose_name_plural = _("Order Tax Lines")
        ordering = ["order", "position", "id"]

class DiscountLine(models.Model):
    class DiscountType(models.TextChoices):
        COUPON = "coupon", _("Coupon Code")
        PROMOTION = "promotion", _("Promotion")
        LOYALTY = "loyalty", _("Loyalty Reward")
        STAFF = "staff", _("Staff Adjustment")
        GOODWILL = "goodwill", _("Goodwill")
        BULK = "bulk", _("Bulk Discount")
        SEASONAL = "seasonal", _("Seasonal Discount")
        OTHER = "other", _("Other")

    id = models.BigAutoField(primary_key=True)
    order = models.ForeignKey(Order, related_name="discount_lines", on_delete=models.CASCADE)
    discount_type = models.CharField(max_length=32, choices=DiscountType.choices, db_index=True)
    source = models.CharField(max_length=64)
    code = models.CharField(max_length=120, blank=True, null=True)
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, null=True)
    discount_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    base_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    percentage = models.DecimalField(max_digits=5, decimal_places=2, blank=True, null=True)
    coupon_usage = models.ForeignKey("orders.CouponUsage", on_delete=models.SET_NULL, null=True, blank=True, related_name="discount_lines")
    promotion_id = models.CharField(max_length=120, blank=True, null=True)
    applies_to_order_item = models.ForeignKey(OrderItem, on_delete=models.SET_NULL, null=True, blank=True, related_name="discount_lines")
    is_taxable = models.BooleanField(default=True)
    is_stackable = models.BooleanField(default=False)
    position = models.PositiveIntegerField(default=0)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("Order Discount Line")
        verbose_name_plural = _("Order Discount Lines")
        ordering = ["order", "position", "id"]

class CouponUsage(models.Model):
    id = models.BigAutoField(primary_key=True)
    coupon_code = models.CharField(max_length=50, db_index=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="coupon_usages")
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="coupon_usages")
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2)
    used_at = models.DateTimeField(auto_now_add=True, db_index=True)

    cart_id = models.ForeignKey("cart.Cart", on_delete=models.SET_NULL, null=True, blank=True, related_name="coupon_usages")
    product_id = models.ForeignKey("catalog.Product", on_delete=models.SET_NULL, null=True, blank=True, related_name="coupon_usages")
    category_id = models.ForeignKey("catalog.Category", on_delete=models.SET_NULL, null=True, blank=True, related_name="coupon_usages")
    is_reversed = models.BooleanField(default=False, db_index=True)
    reversed_at = models.DateTimeField(null=True, blank=True)
    reversal_reason = models.TextField(blank=True, null=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        verbose_name = _("Coupon Usage")
        verbose_name_plural = _("Coupon Usages")
        ordering = ["-used_at"]
        constraints = [
            models.UniqueConstraint(fields=["user", "coupon_code"], name="couponusage_unique_user_coupon"),
        ]

class OrderNote(models.Model):
    class NoteType(models.TextChoices):
        CUSTOMER = "customer", _("Customer Note")
        OPERATOR = "operator", _("Internal Operator Note")
        GIFT = "gift", _("Gift Message")
        DELIVERY = "delivery", _("Delivery Instructions")
        SYSTEM = "system", _("System-Generated Note")

    id = models.BigAutoField(primary_key=True)
    order = models.ForeignKey(Order, related_name="order_notes", on_delete=models.CASCADE)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    note_type = models.CharField(max_length=32, choices=NoteType.choices, default=NoteType.OPERATOR, db_index=True)
    text = models.TextField()
    is_visible_to_customer = models.BooleanField(default=False, db_index=True)
    is_pinned = models.BooleanField(default=False, db_index=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Order Note")
        verbose_name_plural = _("Order Notes")
        ordering = ["-is_pinned", "-created_at"]

class OrderAttachment(models.Model):
    class AttachmentType(models.TextChoices):
        INVOICE = "invoice", _("Invoice PDF")
        PACKING_SLIP = "packing_slip", _("Packing Slip")
        DELIVERY_PROOF = "delivery_proof", _("Delivery Proof")
        CUSTOMS = "customs", _("Customs Declaration")
        INSURANCE = "insurance", _("Insurance Certificate")
        CUSTOMER_DOC = "customer_doc", _("Customer Document")
        OPERATOR_DOC = "operator_doc", _("Operator Document")
        RETURN_LABEL = "return_label", _("Return Label")
        REPLACEMENT_LABEL = "replacement_label", _("Replacement Label")
        SIGNATURE = "signature", _("Signed Delivery Proof")
        OTHER = "other", _("Other")

    id = models.BigAutoField(primary_key=True)
    order = models.ForeignKey(Order, related_name="attachments", on_delete=models.CASCADE)
    file = models.FileField(upload_to=_order_attachment_upload_path)
    original_filename = models.CharField(max_length=255)
    file_size = models.PositiveIntegerField(default=0)
    mime_type = models.CharField(max_length=120, blank=True, null=True)
    attachment_type = models.CharField(max_length=32, choices=AttachmentType.choices, default=AttachmentType.OTHER, db_index=True)
    description = models.CharField(max_length=255, blank=True, null=True)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    is_visible_to_customer = models.BooleanField(default=False, db_index=True)
    is_active = models.BooleanField(default=True, db_index=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Order Attachment")
        verbose_name_plural = _("Order Attachments")
        ordering = ["-created_at"]

class OrderTimelineEvent(models.Model):
    class EventType(models.TextChoices):
        ORDER_PLACED = "order_placed", _("Order Placed")
        ORDER_UPDATED = "order_updated", _("Order Updated")
        ORDER_CANCELLED = "order_cancelled", _("Order Cancelled")
        ORDER_COMPLETED = "order_completed", _("Order Completed")
        PAYMENT_INITIATED = "payment_initiated", _("Payment Initiated")
        PAYMENT_AUTHORIZED = "payment_authorized", _("Payment Authorized")
        PAYMENT_CAPTURED = "payment_captured", _("Payment Captured")
        PAYMENT_FAILED = "payment_failed", _("Payment Failed")
        PAYMENT_REFUNDED = "payment_refunded", _("Payment Refunded")
        SHIPMENT_CREATED = "shipment_created", _("Shipment Created")
        SHIPMENT_PICKED = "shipment_picked", _("Shipment Picked Up")
        SHIPMENT_IN_TRANSIT = "shipment_in_transit", _("Shipment In Transit")
        SHIPMENT_OUT_FOR_DELIVERY = "shipment_out_for_delivery", _("Out for Delivery")
        SHIPMENT_DELIVERED = "shipment_delivered", _("Shipment Delivered")
        SHIPMENT_FAILED = "shipment_failed", _("Shipment Failed")
        SHIPMENT_RETURNED = "shipment_returned", _("Shipment Returned")
        REFUND_INITIATED = "refund_initiated", _("Refund Initiated")
        REFUND_APPROVED = "refund_approved", _("Refund Approved")
        REFUND_REJECTED = "refund_rejected", _("Refund Rejected")
        REFUND_COMPLETED = "refund_completed", _("Refund Completed")
        NOTE_ADDED = "note_added", _("Note Added")
        ATTACHMENT_ADDED = "attachment_added", _("Attachment Added")
        RETURN_REQUESTED = "return_requested", _("Return Requested")
        RETURN_APPROVED = "return_approved", _("Return Approved")
        RETURN_REJECTED = "return_rejected", _("Return Rejected")
        RETURN_RECEIVED = "return_received", _("Return Received")
        RETURN_COMPLETED = "return_completed", _("Return Completed")
        DISCOUNT_APPLIED = "discount_applied", _("Discount Applied")
        DISCOUNT_REVERSED = "discount_reversed", _("Discount Reversed")
        FRAUD_CHECK_PASSED = "fraud_check_passed", _("Fraud Check Passed")
        FRAUD_CHECK_FAILED = "fraud_check_failed", _("Fraud Check Failed")
        FRAUD_CHECK_REVIEW = "fraud_check_review", _("Fraud Check Under Review")
        INVENTORY_ALLOCATED = "inventory_allocated", _("Inventory Allocated")
        INVENTORY_DEDUCTED = "inventory_deducted", _("Inventory Deducted")
        INVENTORY_RESTOCKED = "inventory_restocked", _("Inventory Restocked")
        INVENTORY_TRANSFERRED = "inventory_transferred", _("Inventory Transferred")
        SYSTEM = "system", _("System Event")

    id = models.BigAutoField(primary_key=True)
    order = models.ForeignKey(Order, related_name="timeline_events", on_delete=models.CASCADE)
    event_type = models.CharField(max_length=48, choices=EventType.choices, db_index=True)
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, null=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    is_system_event = models.BooleanField(default=False, db_index=True)
    is_visible_to_customer = models.BooleanField(default=True, db_index=True)
    reference_model = models.CharField(max_length=80, blank=True, null=True)
    reference_id = models.CharField(max_length=80, blank=True, null=True)
    icon = models.CharField(max_length=64, blank=True, null=True)
    color = models.CharField(max_length=32, blank=True, null=True)
    metadata = models.JSONField(default=dict, blank=True)
    occurred_at = models.DateTimeField(default=timezone.now, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("Order Timeline Event")
        verbose_name_plural = _("Order Timeline Events")
        ordering = ["-occurred_at", "-id"]

class ReturnRequest(models.Model):
    class ReturnType(models.TextChoices):
        REFUND = "refund", _("Refund")
        REPLACEMENT = "replacement", _("Replacement")
        EXCHANGE = "exchange", _("Exchange")
        STORE_CREDIT = "store_credit", _("Store Credit")
        REPAIR = "repair", _("Repair")

    class ReturnReasonCategory(models.TextChoices):
        DEFECTIVE = "defective", _("Defective / Damaged")
        WRONG_ITEM = "wrong_item", _("Wrong Item Shipped")
        NOT_AS_DESCRIBED = "not_as_described", _("Not as Described")
        SIZE_ISSUE = "size_issue", _("Size or Fit Issue")
        COLOR_ISSUE = "color_issue", _("Color Discrepancy")
        QUALITY_ISSUE = "quality_issue", _("Quality Issue")
        DAMAGED_IN_TRANSIT = "damaged_in_transit", _("Damaged in Transit")
        LATE_DELIVERY = "late_delivery", _("Late Delivery")
        CHANGED_MIND = "changed_mind", _("Changed Mind")
        DUPLICATE_ORDER = "duplicate_order", _("Duplicate Order")
        BETTER_PRICE_FOUND = "better_price_found", _("Better Price Found")
        OTHER = "other", _("Other")

    class ReturnStatus(models.TextChoices):
        DRAFT = "draft", _("Draft")
        REQUESTED = "requested", _("Requested")
        UNDER_REVIEW = "under_review", _("Under Review")
        APPROVED = "approved", _("Approved")
        REJECTED = "rejected", _("Rejected")
        AWAITING_SHIPMENT = "awaiting_shipment", _("Awaiting Return Shipment")
        IN_TRANSIT = "in_transit", _("Return In Transit")
        RECEIVED = "received", _("Received")
        INSPECTING = "inspecting", _("Inspecting")
        REFUND_INITIATED = "refund_initiated", _("Refund Initiated")
        COMPLETED = "completed", _("Completed")
        CANCELLED = "cancelled", _("Cancelled")

    class RestockDecision(models.TextChoices):
        RESTOCK = "restock", _("Restock")
        DISPOSE = "dispose", _("Dispose")
        RETURN_TO_SUPPLIER = "return_to_supplier", _("Return to Supplier")
        REPAIR = "repair", _("Repair")
        QUARANTINE = "quarantine", _("Quarantine")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.ForeignKey(Order, related_name="return_requests", on_delete=models.CASCADE)
    return_number = models.CharField(max_length=50, unique=True, db_index=True, blank=True, null=True)
    return_type = models.CharField(max_length=24, choices=ReturnType.choices, default=ReturnType.REFUND)
    reason_category = models.CharField(max_length=48, choices=ReturnReasonCategory.choices, default=ReturnReasonCategory.OTHER)
    reason_text = models.TextField(blank=True, null=True)
    status = models.CharField(max_length=24, choices=ReturnStatus.choices, default=ReturnStatus.DRAFT, db_index=True)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="return_requests_requested")
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="return_requests_approved")
    approved_at = models.DateTimeField(null=True, blank=True)
    rejected_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="return_requests_rejected")
    rejected_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.TextField(blank=True, null=True)
    received_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    refund = models.ForeignKey(Refund, on_delete=models.SET_NULL, null=True, blank=True, related_name="return_requests")
    replacement_order = models.ForeignKey(Order, on_delete=models.SET_NULL, null=True, blank=True, related_name="replacement_return_requests")
    restock_decision = models.CharField(max_length=32, choices=RestockDecision.choices, blank=True, null=True)
    restock_location = models.CharField(max_length=120, blank=True, null=True)
    return_shipping_address_snapshot = models.OneToOneField(OrderAddressSnapshot, on_delete=models.SET_NULL, null=True, blank=True, related_name="return_request")
    customer_notes = models.TextField(blank=True, null=True)
    internal_notes = models.TextField(blank=True, null=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Return Request")
        verbose_name_plural = _("Return Requests")
        ordering = ["-created_at"]

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self.return_number:
            ts = timezone.now().strftime("%y%m%d")
            self.return_number = f"RET-{ts}-{secrets.token_hex(3).upper()}"
        super().save(*args, **kwargs)

class ReturnItem(models.Model):
    class InspectionResult(models.TextChoices):
        PENDING = "pending", _("Pending Inspection")
        PASSED = "passed", _("Passed Inspection")
        FAILED = "failed", _("Failed Inspection")
        PARTIAL = "partial", _("Partial Pass")

    id = models.BigAutoField(primary_key=True)
    return_request = models.ForeignKey(ReturnRequest, related_name="items", on_delete=models.CASCADE)
    order_item = models.ForeignKey(OrderItem, on_delete=models.PROTECT, related_name="return_items")
    quantity_returned = models.DecimalField(max_digits=14, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])
    quantity_approved = models.DecimalField(max_digits=14, decimal_places=2, blank=True, null=True, validators=[MinValueValidator(Decimal("0.00"))])
    quantity_received = models.DecimalField(max_digits=14, decimal_places=2, blank=True, null=True, validators=[MinValueValidator(Decimal("0.00"))])
    condition_received = models.CharField(max_length=64, blank=True, null=True)
    inspection_result = models.CharField(max_length=16, choices=InspectionResult.choices, default=InspectionResult.PENDING)
    inspection_notes = models.TextField(blank=True, null=True)
    refund_amount = models.DecimalField(max_digits=14, decimal_places=2, blank=True, null=True, validators=[MinValueValidator(Decimal("0.00"))])
    restock_decision = models.CharField(max_length=32, blank=True, null=True)
    replacement_order_item = models.ForeignKey(OrderItem, on_delete=models.SET_NULL, null=True, blank=True, related_name="replacement_for")
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Return Item")
        verbose_name_plural = _("Return Items")
        ordering = ["return_request", "id"]

class ReturnImage(models.Model):
    class ImageType(models.TextChoices):
        EVIDENCE = "evidence", _("Evidence")
        REFERENCE = "reference", _("Reference")
        PACKAGING = "packaging", _("Packaging")
        LABEL = "label", _("Label")
        OPERATOR = "operator", _("Operator")
        OTHER = "other", _("Other")

    id = models.BigAutoField(primary_key=True)
    return_item = models.ForeignKey(ReturnItem, related_name="images", on_delete=models.CASCADE)
    image = models.ImageField(upload_to=_return_image_upload_path)
    caption = models.CharField(max_length=255, blank=True, null=True)
    image_type = models.CharField(max_length=24, choices=ImageType.choices, default=ImageType.EVIDENCE)
    position = models.PositiveIntegerField(default=0)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("Return Image")
        verbose_name_plural = _("Return Images")
        ordering = ["return_item", "position", "id"]

__all__ = [
    "Order", "OrderItem", "OrderAddressSnapshot", "OrderStatusHistory",
    "Shipment", "ShipmentItem", "Payment", "PaymentAttempt", "Refund",
    "CouponUsage", "TaxLine", "DiscountLine", "OrderNote", "OrderAttachment",
    "OrderTimelineEvent", "ReturnRequest", "ReturnItem", "ReturnImage",
    "DEFAULT_CURRENCY_CODE", "DEFAULT_LOW_STOCK_THRESHOLD", "DEFAULT_ORDER_PAGE_SIZE",
    "DEFAULT_PAYMENT_METHOD", "DEFAULT_CARRIER_NAME", "DEFAULT_ORDER_ACTIVE_STATE",
    "DEFAULT_INVOICE_EXTENSION", "DEFAULT_SHIPPING_LABEL_EXTENSION",
    "DEFAULT_BINARY_EXTENSION", "DEFAULT_LEGACY_EMAIL_PLACEHOLDER",
    "_phone_validator", "_upload_to_order_attachment", "_order_attachment_upload_path",
    "_order_attachment_path", "_upload_to_order_invoice", "_order_invoice_upload_path",
    "_upload_to_order_shipping_label", "_order_shipping_label_upload_path",
    "_return_image_upload_path", "_return_image_path", "_safe_suffix", "_resolve_scope_id",
]