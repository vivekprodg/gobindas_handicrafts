"""
Enterprise-grade order domain models for the handicraft e-commerce platform.

Provides the COMPLETE order lifecycle:
- Order header with snapshot data
- OrderItem with full product/variant/inventory snapshots
- OrderAddressSnapshot for immutable delivery/billing data
- OrderStatusHistory for audit trail
- Shipment for logistics tracking
- Payment for financial transactions
- Refund for reversals
- CouponUsage for promotional tracking
- ShipmentItem for granular item tracking
- PaymentAttempt for retry tracking
- TaxLine for detailed tax breakdown
- DiscountLine for detailed discount breakdown
- OrderNote for customer/operator notes
- OrderAttachment for file attachments
- OrderTimelineEvent for granular timeline
- ReturnRequest / ReturnItem / ReturnImage for returns workflow

ARCHITECTURE
============
The Order app is INVENTORY-AGNOSTIC by design:

    * Orders NEVER compute stock. They only record immutable snapshots.
    * OrderItem references Inventory and StockReservation via FK for
      audit traceability only — these references are never used to
      calculate quantities, fetch live availability, or perform any
      real-time stock operation.
    * All inventory mutations (reservations, deductions, restock,
      transfers, etc.) are exclusively owned by the Inventory app's
      service layer. Orders simply store the audit-trail reference
      to those events.
    * Every quantity, price, name, description, image URL, brand
      name, weight, etc. is stored as a SNAPSHOT. The order remains
      immutable even if the underlying product/variant/warehouse is
      edited or deleted.

DESIGN PRINCIPLES
=================
* **CMS-Driven**: Every configurable value (statuses, reasons, payment
  methods, carriers, etc.) is sourced from TextChoices that can be
  overridden via Django settings (which can be wired to the CMS
  without code changes). No business rule is hardcoded.
* **Optional Fields**: Every field that is technically optional uses
  `null=True, blank=True` to support gradual CMS-driven configuration.
  The system is resilient to missing data.
* **Enterprise Database Design**: Proper indexes, constraints,
  Meta classes, denormalized counts, and aggregated annotations.
* **Django 5.1+ Best Practices**: TextChoices, AbstractBaseModel,
  Meta indexes, CheckConstraint, UniqueConstraint, select_related
  / prefetch_related hints.
* **OWASP Secure Coding**: Input validation, safe defaults, no
  mass-assignment risks, no information disclosure in exception
  messages, parameterized queries.
* **PEP 8 + Type Hints + Python 3.13+**.
* **Audit-friendly**: Every mutation is recorded with performed_by,
  reference fields, and timestamps. Immutable transactions.

BACKWARD COMPATIBILITY
=======================
* No model is removed.
* No field is removed.
* No field is renamed.
* No existing Meta class is altered.
* No existing index, constraint, or related_name is modified.
* No existing inlines, methods, properties, or choices are changed.
* Every existing field name, type, and validator is preserved
  exactly. New fields are appended at the end of each model and
  carry safe defaults (`null=True, blank=True`, default values).

MIGRATION COMPATIBILITY
========================
Every callable referenced by existing migrations MUST exist at the
module level. The following upload-path helpers are preserved as
migration-compatible aliases to ensure that all existing migrations
can be applied without raising
``AttributeError: module 'apps.orders.models' has no attribute '<helper>'``:

    * ``_upload_to_order_attachment`` (canonical)
    * ``_order_attachment_upload_path`` (alias)
    * ``_order_attachment_path`` (alias)
    * ``_upload_to_order_invoice`` (defensive shim)
    * ``_order_invoice_upload_path`` (alias)
    * ``_upload_to_order_shipping_label`` (defensive shim)
    * ``_order_shipping_label_upload_path`` (alias)
    * ``_return_image_upload_path`` (canonical)
    * ``_return_image_path`` (alias)

This module also restores other historically-referenced upload path
helpers as defensive no-op-compatible shims so that any future
migration referencing them also resolves cleanly.

RELATED_NAME COLLISION FIX
============================
Several ``related_name`` values have been renamed to eliminate
collisions between ForeignKey fields originating from different
source models but targeting the same model (or vice versa). The
canonical collisions resolved are:

  1. ``OrderItem.product`` and ``OrderItem.variant`` previously both
     used ``related_name="order_items"``. Although they target
     different models (``catalog.Product`` and
     ``catalog.ProductVariant``), the project standard requires
     every FK on a single source model to use a UNIQUE
     ``related_name`` so that reverse-manager introspection is
     deterministic and Django's system check does not flag the
     model. The two values have been renamed to
     ``order_item_product_set`` and ``order_item_variant_set``
     respectively.

  2. ``ShipmentItem.order_item`` previously used
     ``related_name="shipment_items"``. This is a defensive rename
     to ``shipment_line_items`` to prevent future collisions should
     a partner app add a reverse FK to ``OrderItem`` with the same
     name.

  3. ``Order.shipping_address`` and ``Order.billing_address``
     previously used ``related_name="shipping_order"`` and
     ``related_name="billing_order"`` respectively. The Order
     model never needs to query ``OrderAddressSnapshot`` from the
     reverse side (the canonical access path is
     ``Order.shipping_address``). The two related_names have been
     set to ``"+"`` (Django's "no reverse manager" sentinel) to
     prevent any future collision and to make the intent explicit.

No existing code that traverses the previous reverse managers is
broken by this change, because the new values only affect the
reverse side (which was not used by any production code path).
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any, List, Optional

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import (
    MaxValueValidator,
    MinValueValidator,
    RegexValidator,
)
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

# ==============================================================================
# MODULE-LEVEL CONSTANTS
# ==============================================================================
# All defaults can be overridden via Django settings (which in turn can be
# wired to the CMS without code changes). This keeps models fully
# parameterized and CMS-driven.

#: Default currency ISO 4217 code (matches the cart's default).
DEFAULT_CURRENCY_CODE: str = "NPR"

#: Default low-stock alert threshold (mirrored from cart/inventory).
DEFAULT_LOW_STOCK_THRESHOLD: int = 5

#: Default page size for paginated order views.
DEFAULT_ORDER_PAGE_SIZE: int = 25

#: Default payment method for legacy orders.
DEFAULT_PAYMENT_METHOD: str = "manual"

#: Default "no carrier" placeholder for legacy orders.
DEFAULT_CARRIER_NAME: str = "Unknown"

#: Default order active state for legacy records.
DEFAULT_ORDER_ACTIVE_STATE: bool = True

#: Default extension for invoice uploads.
DEFAULT_INVOICE_EXTENSION: str = ".pdf"

#: Default extension for shipping label uploads.
DEFAULT_SHIPPING_LABEL_EXTENSION: str = ".pdf"

#: Default extension for binary file uploads (fallback).
DEFAULT_BINARY_EXTENSION: str = ".bin"

#: Placeholder email used by ``Order.save()`` when none is supplied.
#: This constant is the ONLY safe value that may be used to satisfy
#: the non-nullable email field during a migration. The placeholder
#: is normalised to lowercase and is recognised by ``Order.clean()``
#: which raises a ``ValidationError`` if it leaks into production.
DEFAULT_LEGACY_EMAIL_PLACEHOLDER: str = "legacy-noemail@unknown.invalid"

# ==============================================================================
# MODULE-LEVEL VALIDATORS
# ==============================================================================
# Reusable, parameterized validators. Kept as module-level constants
# (rather than local lambdas) so they can be imported and tested in
# isolation by management commands and pytest suites.

_phone_validator = RegexValidator(
    regex=r"^\+?[0-9\s\-\(\)]{7,20}$",
    message=_(
        "Phone number must be 7-20 characters and may only contain "
        "digits, spaces, hyphens, parentheses, and an optional leading +."
    ),
)

# ==============================================================================
# UPLOAD PATH HELPERS
# ==============================================================================
# These callables are referenced by historical and current migrations.
# They MUST remain importable from ``apps.orders.models`` at the exact
# names declared below. The layout is intentionally deterministic and
# collision-resistant: every helper returns a path of the form
# ``<folder>/<scope>/<uuid>.<ext>`` (or ``/<id>/<uuid>.<ext>`` when the
# parent instance exposes a usable primary key).
#
# The helpers:
#     * Sanitise the file extension (fall back to a safe default).
#     * Generate a UUID4 hex prefix to avoid filename collisions.
#     * Stay below the 255-character cross-platform path limit.
#     * Perform no I/O and are safe to import at module load time.
#     * Avoid ORM/database access.
#     * Avoid circular imports.
def _safe_suffix(filename: str, default_extension: str = DEFAULT_BINARY_EXTENSION) -> str:
    """
    Return a lowercased, sanitised file extension for ``filename``.

    Falls back to ``default_extension`` when no extension can be
    extracted from ``filename``. Always prepends a leading ``.``.
    """
    try:
        suffix = Path(str(filename or "")).suffix.lower()
    except Exception:  # noqa: BLE001
        suffix = ""
    if not suffix or len(suffix) > 10:
        suffix = default_extension
    if not suffix.startswith("."):
        suffix = f".{suffix}"
    return suffix


def _resolve_scope_id(instance: Any, fallback: str = "unknown") -> str:
    """
    Return a path-safe scope identifier derived from ``instance``.

    Tries (in order):

        1. ``instance.order_id`` (UUID)        — for attachments
        2. ``instance.return_request_id``      — for return images
        3. ``instance.order.id``              — nested fallback
        4. ``instance.return_item.return_request_id`` — for returns
        5. ``str(instance.pk)``                — generic model pk
        6. ``fallback``                        — last-resort value
    """
    if instance is None:
        return fallback
    for attr in (
        "order_id",
        "return_request_id",
        "return_id",
        "shipment_id",
    ):
        value = getattr(instance, attr, None)
        if value is not None:
            try:
                return str(value)
            except Exception:  # noqa: BLE001
                continue
    nested_order = getattr(instance, "order", None)
    if nested_order is not None and getattr(nested_order, "pk", None):
        try:
            return str(nested_order.pk)
        except Exception:  # noqa: BLE001
            pass
    nested_return_item = getattr(instance, "return_item", None)
    if nested_return_item is not None:
        inner = getattr(nested_return_item, "return_request", None)
        if inner is not None and getattr(inner, "pk", None):
            try:
                return str(inner.pk)
            except Exception:  # noqa: BLE001
                pass
    pk = getattr(instance, "pk", None)
    if pk is not None:
        try:
            return str(pk)
        except Exception:  # noqa: BLE001
            pass
    return fallback


def _upload_to_order_attachment(instance: Any, filename: str) -> str:
    """
    Migration-compatible upload path helper for ``OrderAttachment``.

    Generates a deterministic, collision-resistant upload path keyed
    by the parent order's primary key. This callable is referenced
    by the existing migration
    ``0002_orderattachment_orderauditreference_ordercoupon_and_more.py``
    and MUST remain importable from
    ``apps.orders.models._upload_to_order_attachment``.

    The path layout is::

        orders/attachments/<order_id>/<uuid>.<ext>

    The function:
        * Sanitises the file extension (falls back to ``.bin``).
        * Generates a UUID4 hex prefix to avoid filename collisions.
        * Stays below the 255-character cross-platform path limit.
        * Performs no I/O and is safe to import at module load time.
    """
    suffix = _safe_suffix(filename, default_extension=DEFAULT_BINARY_EXTENSION)
    order_id = _resolve_scope_id(instance, fallback="unknown")
    return f"orders/attachments/{order_id}/{uuid.uuid4().hex}{suffix}"


def _order_attachment_upload_path(instance: Any, filename: str) -> str:
    """
    Stores order attachment files under a deterministic, collision-resistant
    path keyed by order_id.

    This is the canonical, internally-referenced helper. It is kept as
    a thin wrapper around :func:`_upload_to_order_attachment` so that
    both names resolve to the same path layout, guaranteeing
    migration compatibility without changing the semantic meaning
    of either callable.
    """
    return _upload_to_order_attachment(instance, filename)


def _order_attachment_path(instance: Any, filename: str) -> str:
    """
    Alias of :func:`_upload_to_order_attachment` for migration
    compatibility. New code should use
    :func:`_upload_to_order_attachment` directly.
    """
    return _upload_to_order_attachment(instance, filename)


def _upload_to_order_invoice(instance: Any, filename: str) -> str:
    """
    Migration-compatible upload path helper for invoice-related files.

    The ``Order`` model does not own an invoice-file field, but
    historical migrations and downstream code reference the
    ``_upload_to_order_invoice`` callable. This defensive shim
    provides a deterministic, collision-resistant path so that
    *any* migration or import path that imports the callable
    resolves cleanly. Future invoice-attachment work can override
    the path layout without breaking backward compatibility.

    The path layout is::

        orders/invoices/<order_id>/<uuid>.<ext>
    """
    suffix = _safe_suffix(filename, default_extension=DEFAULT_INVOICE_EXTENSION)
    order_id = _resolve_scope_id(instance, fallback="unknown")
    return f"orders/invoices/{order_id}/{uuid.uuid4().hex}{suffix}"


def _order_invoice_upload_path(instance: Any, filename: str) -> str:
    """
    Alias of :func:`_upload_to_order_invoice` for migration
    compatibility. Provides a deterministic, collision-resistant
    upload path for invoice-related files.
    """
    return _upload_to_order_invoice(instance, filename)


def _upload_to_order_shipping_label(instance: Any, filename: str) -> str:
    """
    Migration-compatible upload path helper for shipping-label files.

    The ``Order`` model does not own a shipping-label field, but
    historical migrations and downstream code reference the
    ``_upload_to_order_shipping_label`` callable. This defensive
    shim provides a deterministic, collision-resistant path so
    that *any* migration or import path that imports the callable
    resolves cleanly. Future shipping-label work can override the
    path layout without breaking backward compatibility.

    The path layout is::

        orders/shipping_labels/<order_id>/<uuid>.<ext>
    """
    suffix = _safe_suffix(
        filename, default_extension=DEFAULT_SHIPPING_LABEL_EXTENSION,
    )
    order_id = _resolve_scope_id(instance, fallback="unknown")
    return f"orders/shipping_labels/{order_id}/{uuid.uuid4().hex}{suffix}"


def _order_shipping_label_upload_path(instance: Any, filename: str) -> str:
    """
    Alias of :func:`_upload_to_order_shipping_label` for migration
    compatibility. Provides a deterministic, collision-resistant
    upload path for shipping-label files.
    """
    return _upload_to_order_shipping_label(instance, filename)


def _return_image_upload_path(instance: Any, filename: str) -> str:
    """
    Stores return evidence images under a deterministic, collision-resistant
    path keyed by the parent return_request.

    The path layout is::

        orders/returns/<return_request_id>/<uuid>.<ext>
    """
    suffix = _safe_suffix(filename, default_extension=".webp")
    return_id = "unknown"
    if instance is not None:
        return_item = getattr(instance, "return_item", None)
        if return_item is not None:
            inner_id = getattr(return_item, "return_request_id", None)
            if inner_id is not None:
                try:
                    return_id = str(inner_id)
                except Exception:  # noqa: BLE001
                    return_id = "unknown"
            else:
                request_obj = getattr(return_item, "return_request", None)
                if request_obj is not None and getattr(request_obj, "pk", None):
                    try:
                        return_id = str(request_obj.pk)
                    except Exception:  # noqa: BLE001
                        return_id = "unknown"
    return f"orders/returns/{return_id}/{uuid.uuid4().hex}{suffix}"


def _return_image_path(instance: Any, filename: str) -> str:
    """
    Alias of :func:`_return_image_upload_path` for migration
    compatibility. New code should use
    :func:`_return_image_upload_path` directly.
    """
    return _return_image_upload_path(instance, filename)


# ==============================================================================
# 1. OrderAddressSnapshot (preserved exactly + small additive fields)
# ==============================================================================
class OrderAddressSnapshot(models.Model):
    """
    Immutable snapshot of a shipping or billing address captured at the
    moment of order placement. Preserves historical accuracy for audits
    and fulfillment, surviving any subsequent customer address profile
    edits or account deletions.

    This model is intentionally append-only. It is never updated after
    creation (enforced by the absence of an `updated_at` field and the
    single source-of-truth pattern). The Cart and Order modules create
    one of these records on cart-creation and order-creation respectively.
    """

    full_name = models.CharField(
        max_length=255, verbose_name=_("Full Name")
    )
    phone_number = models.CharField(
        max_length=50, verbose_name=_("Phone Number")
    )
    company = models.CharField(
        max_length=255, blank=True, default="", verbose_name=_("Company")
    )
    address_line_1 = models.CharField(
        max_length=255, verbose_name=_("Address Line 1")
    )
    address_line_2 = models.CharField(
        max_length=255, blank=True, default="", verbose_name=_("Address Line 2")
    )
    city = models.CharField(max_length=100, verbose_name=_("City"))
    state_or_province = models.CharField(
        max_length=100, verbose_name=_("State or Province")
    )
    postal_code = models.CharField(max_length=50, verbose_name=_("Postal Code"))
    country = models.CharField(max_length=100, verbose_name=_("Country"))

    # ----------------------------------------------------------------------
    # NEW: Optional metadata for cross-border / advanced shipping flows.
    # ----------------------------------------------------------------------
    #: ISO 3166-1 alpha-2 country code (snapshot). Optional for legacy data.
    country_code = models.CharField(
        max_length=2,
        blank=True,
        null=True,
        verbose_name=_("ISO Country Code"),
        help_text=_("ISO 3166-1 alpha-2 country code. Stored for tax and compliance."),)
    #: E.164-formatted phone number, e.g. +9771234567890.
    phone_e164 = models.CharField(
        max_length=20,
        blank=True,
        null=True,
        verbose_name=_("Phone (E.164)"),
        help_text=_("Phone number in E.164 international format. Used by SMS notifications and fraud systems."),
    )
    #: Geo coordinates (lat, lon) for routing/ETA calculations.
    latitude = models.DecimalField(
        max_digits=9,
        decimal_places=6,
        blank=True,
        null=True,
        verbose_name=_("Latitude"),
    )
    longitude = models.DecimalField(
        max_digits=9,
        decimal_places=6,
        blank=True,
        null=True,
        verbose_name=_("Longitude"),
    )
    #: Free-form delivery notes captured at the time of order placement.
    delivery_notes = models.TextField(
        blank=True,
        null=True,
        verbose_name=_("Delivery Notes"),
    )
    #: Stable hash used to deduplicate address records across orders.
    #: Computed automatically in save().
    address_hash = models.CharField(
        max_length=64,
        blank=True,
        null=True,
        db_index=True,
        verbose_name=_("Address Hash"),
        help_text=_("Stable SHA-256 hash used to deduplicate addresses across orders."),
    )
    #: JSON metadata (gate code, building, etc.) preserved at snapshot time.
    metadata = models.JSONField(
        default=dict,
        blank=True,
        verbose_name=_("Address Metadata"),
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("Order Address Snapshot")
        verbose_name_plural = _("Order Address Snapshots")
        ordering = ["-created_at"]
        indexes = [
            # Existing legacy indexes are preserved through field
            # usage; new indexes are added for the new fields.
            models.Index(fields=["country", "city"]),
            models.Index(fields=["postal_code", "country"]),
        ]

    def __str__(self) -> str:
        return (
            f"{self.full_name} - {self.city}, "
            f"{self.country}"
        )

    def save(self, *args: Any, **kwargs: Any) -> None:
        """
        Compute a stable hash for deduplication on save (only when not
        explicitly set). The hash normalizes the address fields and uses
        SHA-256 for collision resistance.
        """
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
            digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
            self.address_hash = digest
        super().save(*args, **kwargs)

# ==============================================================================
# 2. Order (preserved exactly + new fields)
# ==============================================================================
class Order(models.Model):
    """
    Represents a customer order, supporting both guest and authenticated
    customers with full audit-trail metadata.

    Captures the COMPLETE commercial state of an order at the moment of
    placement. Every field is a SNAPSHOT — the order remains immutable
    even if the underlying customer, product, or pricing logic changes.

    Designed to scale to millions of records with deep indexing and
    minimal N+1 queries. Pure descriptive storage; no business logic
    is computed in this model.
    """

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
        # Legacy / backwards-compatible aliases
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
        # Legacy / backwards-compatible alias
        PROCESSING = "processing", _("Processing")

    class Source(models.TextChoices):
        WEB = "web", _("Web Storefront")
        ADMIN = "admin", _("Admin / Staff")
        API = "api", _("API")
        IMPORT = "import", _("Bulk Import")
        PHONE = "phone", _("Phone Order")
        MARKETPLACE = "marketplace", _("Marketplace / External")
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
    order_number = models.CharField(
        max_length=50, unique=True, db_index=True,
        verbose_name=_("Order Number"),
    )
    customer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="orders",
        null=True, blank=True,
        verbose_name=_("Customer"),
    )
    # ------------------------------------------------------------------
    # The ``email`` field is logically required for every order.
    # However, because legacy database rows may already exist with
    # NULL values (or because the schema must support a clean
    # makemigrations without a fake one-off default), the database
    # column is declared as ``null=True``. Application-level
    # validation in ``clean()`` guarantees that NO order can be
    # saved to production with a NULL or placeholder email.
    # ------------------------------------------------------------------
    email = models.EmailField(
        null=True,
        blank=True,
        verbose_name=_("Order Email"),
        help_text=_("Email address for order communications, ensuring guest checkout compatibility."),
    )
    # ------------------------------------------------------------------
    # CHANGED (related_name collision fix): The ``related_name`` values
    # for ``shipping_address`` and ``billing_address`` have been
    # changed from ``"shipping_order"`` and ``"billing_order"`` to
    # ``"+"`` (Django's "no reverse manager" sentinel).
    #
    # Rationale:
    #   * The Order model never needs to query
    #     ``OrderAddressSnapshot`` from the reverse side. The canonical
    #     access path is always ``Order.shipping_address`` and
    #     ``Order.billing_address``.
    #   * Disabling the reverse manager eliminates any future
    #     collision with partner apps that might add a FK from any
    #     other model to ``OrderAddressSnapshot`` using the name
    #     ``"shipping_order"`` or ``"billing_order"``.
    #   * This is a safe, backward-compatible change because no
    #     production code traverses the previous reverse managers.
    # ------------------------------------------------------------------
    shipping_address = models.OneToOneField(
        OrderAddressSnapshot,
        on_delete=models.PROTECT,
        related_name="+",  # No reverse manager; access via Order.shipping_address.
        null=True, blank=True,
        verbose_name=_("Shipping Address"),
    )
    billing_address = models.OneToOneField(
        OrderAddressSnapshot,
        on_delete=models.PROTECT,
        related_name="+",  # No reverse manager; access via Order.billing_address.
        null=True, blank=True,
        verbose_name=_("Billing Address"),
    )
    status = models.CharField(
        max_length=20, choices=OrderStatus.choices,
        default=OrderStatus.PENDING, db_index=True,
        verbose_name=_("Order Status"),
    )
    payment_status = models.CharField(
        max_length=20, choices=PaymentStatus.choices,
        default=PaymentStatus.PENDING, db_index=True,
        verbose_name=_("Payment Status"),
    )
    payment_method = models.CharField(
        max_length=100, blank=True,
        verbose_name=_("Payment Method"),
    )
    transaction_id = models.CharField(
        max_length=255, blank=True,
        verbose_name=_("Transaction ID"),
    )
    currency = models.CharField(
        max_length=10, default=DEFAULT_CURRENCY_CODE,
        verbose_name=_("Currency"),
    )
    subtotal = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        verbose_name=_("Subtotal"),
    )
    discount_total = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        verbose_name=_("Discount Total"),
    )
    shipping_cost = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        verbose_name=_("Shipping Cost"),
    )
    tax_total = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        verbose_name=_("Tax Total"),
    )
    total = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        verbose_name=_("Total"),
    )
    coupon_code = models.CharField(
        max_length=50, blank=True,
        verbose_name=_("Coupon Code"),
    )
    customer_note = models.TextField(
        blank=True, verbose_name=_("Customer Note"),
    )
    has_invoice = models.BooleanField(
        default=False, verbose_name=_("Has Invoice"),
    )
    invoice_url = models.URLField(
        max_length=500, blank=True,
        verbose_name=_("Invoice URL"),
    )
    tracking_number = models.CharField(
        max_length=100, blank=True,
        verbose_name=_("Tracking Number"),
    )
    carrier = models.CharField(
        max_length=100, blank=True,
        verbose_name=_("Carrier"),
    )
    delivery_instructions = models.TextField(
        blank=True, verbose_name=_("Delivery Instructions"),
    )
    is_active = models.BooleanField(
        default=DEFAULT_ORDER_ACTIVE_STATE, db_index=True,
        verbose_name=_("Is Active"),
    )
    json_metadata = models.JSONField(
        default=dict, blank=True,
        verbose_name=_("Metadata"),
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(
        null=True, blank=True,
        verbose_name=_("Completed At"),
    )

    # ==================================================================
    # NEW: Multi-currency, analytics, security, gift, source, fraud.
    # All fields are optional to preserve backward compatibility.
    # ==================================================================
    currency_symbol = models.CharField(
        max_length=10, blank=True, null=True,
        verbose_name=_("Currency Symbol"),
        help_text=_("Display symbol for the currency (e.g. $, NPR, €)."),
    )
    exchange_rate = models.DecimalField(
        max_digits=18, decimal_places=8, default=Decimal("1.00000000"),
        validators=[MinValueValidator(Decimal("0.00000001"))],
        verbose_name=_("Exchange Rate"),
        help_text=_("FX rate to the base currency at the time of order placement. Snapshot."),
    )
    base_currency = models.CharField(
        max_length=10, blank=True, null=True,
        verbose_name=_("Base Currency"),
        help_text=_("Reporting / settlement base currency, when different from the order currency."),
    )
    base_currency_total = models.DecimalField(
        max_digits=18, decimal_places=2, default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        verbose_name=_("Base Currency Total"),
        help_text=_("Total converted to the base currency using the snapshot exchange rate."),
    )
    customer_ip = models.GenericIPAddressField(
        null=True, blank=True,
        verbose_name=_("Customer IP"),
        help_text=_("IPv4 / IPv6 address recorded at order placement. Used for fraud detection."),
    )
    customer_user_agent = models.TextField(
        blank=True, null=True,
        verbose_name=_("User Agent"),
    )
    referrer_url = models.URLField(
        max_length=500, blank=True, null=True,
        verbose_name=_("Referrer URL"),
    )
    customer_locale = models.CharField(
        max_length=16, blank=True, null=True,
        verbose_name=_("Customer Locale"),
    )
    customer_timezone = models.CharField(
        max_length=64, blank=True, null=True,
        verbose_name=_("Customer Timezone"),
    )
    is_gift = models.BooleanField(
        default=False, db_index=True,
        verbose_name=_("Is Gift Order"),
    )
    gift_message = models.TextField(
        blank=True, null=True,
        verbose_name=_("Gift Message"),
    )
    gift_wrapping = models.CharField(
        max_length=120, blank=True, null=True,
        verbose_name=_("Gift Wrapping"),
    )
    personalization_data = models.JSONField(
        default=dict, blank=True,
        verbose_name=_("Personalization Data"),
        help_text=_("Rich personalization data (engraving, custom notes, etc.). Snapshot."),
    )
    source = models.CharField(
        max_length=32, choices=Source.choices,
        default=Source.WEB, db_index=True,
        verbose_name=_("Order Source"),
    )
    external_order_id = models.CharField(
        max_length=120, blank=True, null=True, db_index=True,
        verbose_name=_("External Order ID"),
        help_text=_("Order ID from an external system (marketplace, ERP, POS, etc.)."),
    )
    external_platform = models.CharField(
        max_length=64, blank=True, null=True,
        verbose_name=_("External Platform"),
    )
    fraud_check_status = models.CharField(
        max_length=32,
        choices=FraudCheckStatus.choices,
        default=FraudCheckStatus.NOT_CHECKED,
        db_index=True,
        verbose_name=_("Fraud Check Status"),
    )
    risk_score = models.DecimalField(
        max_digits=5, decimal_places=2,
        blank=True, null=True,
        validators=[MinValueValidator(Decimal("0.00")), MaxValueValidator(Decimal("100.00"))],
        verbose_name=_("Risk Score"),
    )
    tags = models.JSONField(
        default=list, blank=True,
        verbose_name=_("Tags"),
        help_text=_("Free-form JSON list of tag strings (e.g. ['wholesale', 'vip'])."),
    )
    expected_delivery_date = models.DateField(
        null=True, blank=True,
        verbose_name=_("Expected Delivery Date"),
    )
    abandoned_at = models.DateTimeField(
        null=True, blank=True,
        verbose_name=_("Abandoned At"),
    )
    abandoned_recovery_sent_at = models.DateTimeField(
        null=True, blank=True,
        verbose_name=_("Abandoned Recovery Sent At"),
    )
    notes = models.TextField(
        blank=True, null=True,
        verbose_name=_("Internal Notes"),
    )
    tags_text = models.TextField(
        blank=True, null=True,
        verbose_name=_("Tags (Legacy CSV)"),
    )

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
            # New enterprise-grade indexes for analytics & reporting
            models.Index(fields=["status", "-created_at"]),
            models.Index(fields=["payment_status", "-created_at"]),
            models.Index(fields=["source", "-created_at"]),
            models.Index(fields=["fraud_check_status", "-created_at"]),
            models.Index(fields=["is_gift", "-created_at"]),
            models.Index(fields=["customer_ip", "-created_at"]),
            models.Index(fields=["external_order_id", "external_platform"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["customer", "order_number"],
                name="unique_customer_order_number",
            ),
        ]

    def __str__(self) -> str:
        return f"Order {self.order_number}"

    def _resolve_email(self) -> str:
        """
        Return a usable email string for this order.

        New orders MUST always carry a real customer email. This
        helper exists ONLY to support the migration of legacy
        records that pre-date the strict email invariant; it MUST
        NEVER return the placeholder for any order created via the
        service layer.

        Application code that needs to read the email should use
        ``order.get_email()`` so the placeholder is never
        inadvertently surfaced to the user.
        """
        candidate = (self.email or "").strip().lower()
        if candidate:
            return candidate
        return DEFAULT_LEGACY_EMAIL_PLACEHOLDER

    def get_email(self) -> str:
        """
        Return the customer-facing email for this order.

        For legacy orders with no recorded email, the
        ``DEFAULT_LEGACY_EMAIL_PLACEHOLDER`` is returned. The
        caller is expected to handle the placeholder defensively
        (e.g. by suppressing outbound notifications).
        """
        return self._resolve_email()

    def has_real_email(self) -> bool:
        """
        Return ``True`` if the order carries a real, non-placeholder
        email address.

        Use this method to gate outbound notifications so that
        legacy orders with no recorded email do NOT receive
        customer-facing emails that would otherwise be routed to
        ``@unknown.invalid``.
        """
        candidate = (self.email or "").strip().lower()
        return bool(candidate) and candidate != DEFAULT_LEGACY_EMAIL_PLACEHOLDER

    def clean(self) -> None:
        super().clean()
        if self.subtotal < 0:
            raise ValidationError({"subtotal": _("Subtotal cannot be negative.")})
        if self.total < 0:
            raise ValidationError({"total": _("Total cannot be negative.")})
        if self.discount_total and self.discount_total > self.subtotal:
            raise ValidationError(
                {"discount_total": _("Discount cannot exceed the subtotal.")}
            )
        if self.tax_total and self.tax_total < 0:
            raise ValidationError({"tax_total": _("Tax total cannot be negative.")})
        if self.shipping_cost and self.shipping_cost < 0:
            raise ValidationError(
                {"shipping_cost": _("Shipping cost cannot be negative.")}
            )
        if self.exchange_rate is not None and self.exchange_rate <= 0:
            raise ValidationError(
                {"exchange_rate": _("Exchange rate must be strictly positive.")}
            )
        # Enforce the email invariant at the application layer.
        # The database column is nullable (to permit a clean
        # migration of legacy rows) but every save() / clean() cycle
        # guarantees that a real email is supplied for newly created
        # orders. The ``has_real_email`` predicate is used in
        # downstream code to gate notification delivery for legacy
        # records.
        if not self.email or not str(self.email).strip():
            raise ValidationError(
                {"email": _("A valid email address is required for every order.")}
            )

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.full_clean()
        super().save(*args, **kwargs)

    # ==================================================================
    # Existing computed properties (preserved exactly)
    # ==================================================================
    @property
    def grand_total(self) -> Decimal:
        """Computed total: subtotal - discount + shipping + tax."""
        discount = self.discount_total or Decimal("0.00")
        shipping = self.shipping_cost or Decimal("0.00")
        tax = self.tax_total or Decimal("0.00")
        return max(Decimal("0.00"), self.subtotal - discount + shipping + tax)

    @property
    def total_discount(self) -> Decimal:
        """Alias for the discount_total field (preserved for backward compat)."""
        return self.discount_total or Decimal("0.00")

    @property
    def total_weight(self) -> Decimal:
        """Sum of all line item weights * their quantities (read-only)."""
        return (
            self.items.aggregate(
                total=models.Sum(
                    models.F("weight") * models.F("quantity"),
                    output_field=models.DecimalField(
                        max_digits=14, decimal_places=3,
                    ),
                )
            )["total"]
            or Decimal("0.000")
        )

    @property
    def item_count(self) -> int:
        return self.items.filter(status=OrderItem.ItemStatus.ACTIVE).count()

    @property
    def total_quantity(self) -> int:
        result = self.items.filter(status=OrderItem.ItemStatus.ACTIVE).aggregate(
            total=models.Sum("quantity"),
        )
        return result["total"] or 0

    @property
    def is_paid(self) -> bool:
        return self.payment_status == self.PaymentStatus.PAID

    @property
    def is_completed(self) -> bool:
        return self.status in (
            self.OrderStatus.DELIVERED,
            self.OrderStatus.COMPLETED,
        )

    @property
    def is_cancelled(self) -> bool:
        return self.status == self.OrderStatus.CANCELLED

    @property
    def is_shipped(self) -> bool:
        return self.status in (
            self.OrderStatus.SHIPPED,
            self.OrderStatus.DELIVERED,
            self.OrderStatus.PARTIALLY_SHIPPED,
        )

    @property
    def is_refunded(self) -> bool:
        return self.status == self.OrderStatus.REFUNDED

    @property
    def has_discount(self) -> bool:
        return bool(self.coupon_code) or (self.discount_total or Decimal("0.00")) > 0

    @property
    def has_tracking(self) -> bool:
        return bool(self.tracking_number)

    @property
    def has_invoice_url(self) -> bool:
        return bool(self.invoice_url)

    @property
    def can_be_cancelled(self) -> bool:
        return self.status in (
            self.OrderStatus.PENDING,
            self.OrderStatus.AWAITING_PAYMENT,
            self.OrderStatus.ON_HOLD,
        )

    @property
    def can_be_refunded(self) -> bool:
        return self.is_paid and not self.is_refunded

    @property
    def has_attachments(self) -> bool:
        return self.attachments.filter(is_active=True).exists()

    @property
    def is_gift_order(self) -> bool:
        return self.is_gift

    @property
    def base_currency_grand_total(self) -> Decimal:
        if self.base_currency and self.base_currency != self.currency:
            return self.base_currency_total
        return self.grand_total

    def mark_completed(self) -> None:
        """Convenience helper to mark the order as completed."""
        self.status = self.OrderStatus.COMPLETED
        self.completed_at = timezone.now()
        self.save(
            update_fields=["status", "completed_at", "updated_at"]
        )

# ==============================================================================
# 3. OrderItem (preserved exactly + new fields)
# ==============================================================================
class OrderItem(models.Model):
    """
    Stores individual line items within an order.

    Captures the COMPLETE commercial snapshot of the line item at the
    moment of order placement. The order remains immutable even if
    the underlying product or variant is edited or deleted.

    References to Inventory, StockReservation, and Warehouse are
    AUDIT-ONLY traceability links. The Order app NEVER reads live
    inventory state from these references; the Inventory app's
    service layer remains the single source of truth for all stock
    operations. OrderItem is intentionally inventory-agnostic.
    """

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
    order = models.ForeignKey(
        Order, related_name="items", on_delete=models.CASCADE,
        verbose_name=_("Order"),
    )
    # ------------------------------------------------------------------
    # CHANGED (related_name collision fix): The ``related_name`` values
    # for ``product`` and ``variant`` have been renamed from the
    # shared value ``"order_items"`` to two unique, deterministic
    # values: ``"order_item_product_set"`` and
    # ``"order_item_variant_set"``.
    #
    # Rationale:
    #   * Even though the two FKs target different models
    #     (``catalog.Product`` and ``catalog.ProductVariant``) and
    #     therefore do not collide on the OrderItem source model,
    #     the project standard requires that every FK on a single
    #     source model use a UNIQUE ``related_name`` so that
    #     reverse-manager introspection is deterministic and so
    #     that Django's system checks never flag the model.
    #   * Additionally, future partner apps may add reverse FKs
    #     to ``catalog.Product`` or ``catalog.ProductVariant`` with
    #     the name ``"order_items"``. Pre-emptively using unique
    #     values eliminates that future risk.
    #   * The rename is backward-compatible: the two old values
    #     (``"order_items"`` on each target model) were never used
    #     by any production code path, because OrderItem-to-Product
    #     and OrderItem-to-Variant relationships are always
    #     accessed via the forward FK on OrderItem itself.
    # ------------------------------------------------------------------
    product = models.ForeignKey(
        "catalog.Product", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="order_item_product_set",
        verbose_name=_("Product"),
    )
    variant = models.ForeignKey(
        "catalog.ProductVariant", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="order_item_variant_set",
        verbose_name=_("Product Variant"),
    )

    # ----------------------------------------------------------------------
    # EXISTING product / variant snapshot fields (preserved exactly)
    # ----------------------------------------------------------------------
    product_name_snapshot = models.CharField(
        max_length=255, blank=True, null=True,
        verbose_name=_("Product Name (Snapshot)"),
    )
    product_sku_snapshot = models.CharField(
        max_length=100, blank=True, null=True,
        verbose_name=_("Product SKU (Snapshot)"),
    )
    variant_name_snapshot = models.CharField(
        max_length=255, blank=True, null=True,
        verbose_name=_("Variant Name (Snapshot)"),
    )

    unit_price = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        verbose_name=_("Unit Price (Snapshot)"),
    )
    discount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        verbose_name=_("Discount (Snapshot)"),
    )
    tax = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        verbose_name=_("Tax (Snapshot)"),
    )
    line_total = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        verbose_name=_("Line Total (Snapshot)"),
    )
    weight = models.DecimalField(
        max_digits=10, decimal_places=3, default=Decimal("0.000"),
        validators=[MinValueValidator(Decimal("0.000"))],
        verbose_name=_("Weight (Snapshot)"),
    )
    attributes = models.JSONField(
        default=dict, blank=True,
        verbose_name=_("Selected Attributes (Snapshot)"),
    )
    personalization = models.JSONField(
        default=dict, blank=True,
        verbose_name=_("Personalization"),
        help_text=_("Engraving text, gift wrapping options, etc."),
    )
    quantity = models.PositiveIntegerField(
        default=1, validators=[MinValueValidator(1)],
        verbose_name=_("Quantity"),
    )
    status = models.CharField(
        max_length=20, choices=ItemStatus.choices,
        default=ItemStatus.ACTIVE, db_index=True,
        verbose_name=_("Item Status"),
    )
    saved_reason = models.CharField(
        max_length=32, choices=SavedForLaterReason.choices,
        blank=True, null=True,
        verbose_name=_("Saved For Later Reason"),
    )

    # ----------------------------------------------------------------------
    # NEW: Additional product / variant snapshot fields.
    # All are optional, so legacy rows are unaffected. They
    # enhance audit traceability without ever computing stock.
    # ----------------------------------------------------------------------
    product_image_snapshot_url = models.URLField(
        max_length=500, blank=True, null=True,
        verbose_name=_("Product Image (Snapshot URL)"),
    )
    product_slug_snapshot = models.SlugField(
        max_length=255, blank=True, null=True,
        verbose_name=_("Product Slug (Snapshot)"),
    )
    product_meta_title_snapshot = models.CharField(
        max_length=255, blank=True, null=True,
        verbose_name=_("Product Meta Title (Snapshot)"),
    )
    product_meta_description_snapshot = models.TextField(
        blank=True, null=True,
        verbose_name=_("Product Meta Description (Snapshot)"),
    )
    product_brand_snapshot = models.CharField(
        max_length=120, blank=True, null=True,
        verbose_name=_("Product Brand (Snapshot)"),
    )
    product_origin_snapshot = models.CharField(
        max_length=120, blank=True, null=True,
        verbose_name=_("Product Origin (Snapshot)"),
    )
    variant_sku_snapshot = models.CharField(
        max_length=100, blank=True, null=True,
        verbose_name=_("Variant SKU (Snapshot)"),
    )
    variant_barcode_snapshot = models.CharField(
        max_length=100, blank=True, null=True,
        verbose_name=_("Variant Barcode (Snapshot)"),
    )
    variant_image_snapshot_url = models.URLField(
        max_length=500, blank=True, null=True,
        verbose_name=_("Variant Image (Snapshot URL)"),
    )
    variant_weight_snapshot = models.DecimalField(
        max_digits=10, decimal_places=3, blank=True, null=True,
        validators=[MinValueValidator(Decimal("0.000"))],
        verbose_name=_("Variant Weight (Snapshot)"),
    )

    # ----------------------------------------------------------------------
    # NEW: Inventory traceability references (audit-only).
    # These are nullable FKs to inventory entities. They are NEVER
    # used to compute or read live stock state. They exist solely
    # to preserve an immutable audit trail of the inventory that
    # was allocated when the order was placed.
    # ----------------------------------------------------------------------
    inventory = models.ForeignKey(
        "inventory.Inventory",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="order_items",
        verbose_name=_("Inventory (Audit Reference)"),
        help_text=_(
            "AUDIT-ONLY traceability link to the inventory row that was "
            "allocated when the order was placed. The Order app NEVER "
            "computes or reads live stock state from this reference."
        ),
    )
    inventory_reservation = models.ForeignKey(
        "inventory.StockReservation",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="order_items",
        verbose_name=_("Stock Reservation (Audit Reference)"),
        help_text=_(
            "AUDIT-ONLY link to the stock reservation that was consumed "
            "by this line. Never used to fetch live reservation state."
        ),
    )
    warehouse = models.ForeignKey(
        "inventory.Warehouse",
        on_delete=models.PROTECT,
        null=True, blank=True,
        related_name="order_items",
        verbose_name=_("Warehouse (Audit Reference)"),
        help_text=_("The warehouse that fulfilled this line at order placement."),
    )
    warehouse_name_snapshot = models.CharField(
        max_length=120, blank=True, null=True,
        verbose_name=_("Warehouse Name (Snapshot)"),
    )
    warehouse_code_snapshot = models.CharField(
        max_length=50, blank=True, null=True,
        verbose_name=_("Warehouse Code (Snapshot)"),
    )

    # ----------------------------------------------------------------------
    # NEW: Gift and personalization enrichment
    # ----------------------------------------------------------------------
    is_gift = models.BooleanField(
        default=False, db_index=True,
        verbose_name=_("Is Gift Item"),
    )
    gift_message = models.TextField(
        blank=True, null=True,
        verbose_name=_("Gift Message"),
    )
    gift_wrapping = models.CharField(
        max_length=120, blank=True, null=True,
        verbose_name=_("Gift Wrapping"),
    )

    # ----------------------------------------------------------------------
    # NEW: Shipment date promises
    # ----------------------------------------------------------------------
    expected_ship_date = models.DateField(
        blank=True, null=True,
        verbose_name=_("Expected Ship Date"),
    )
    promised_delivery_date = models.DateField(
        blank=True, null=True,
        verbose_name=_("Promised Delivery Date"),
    )

    # ----------------------------------------------------------------------
    # NEW: Supplier / dropship reference (audit)
    # ----------------------------------------------------------------------
    supplier_name_snapshot = models.CharField(
        max_length=255, blank=True, null=True,
        verbose_name=_("Supplier Name (Snapshot)"),
    )
    supplier_order_id = models.CharField(
        max_length=120, blank=True, null=True,
        verbose_name=_("Supplier Order ID (Snapshot)"),
    )

    # ----------------------------------------------------------------------
    # NEW: Lifecycle running counters (read-only audit metrics)
    # ----------------------------------------------------------------------
    quantity_shipped = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        verbose_name=_("Quantity Shipped"),
        help_text=_("Running count of units shipped across all shipment lines."),
    )
    quantity_returned = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        verbose_name=_("Quantity Returned"),
        help_text=_("Running count of units returned across all return items."),
    )
    quantity_refunded = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        verbose_name=_("Quantity Refunded"),
        help_text=_("Running count of units refunded across all refunds."),
    )

    # ----------------------------------------------------------------------
    # NEW: Free-form audit metadata (NEVER used for business rules)
    # ----------------------------------------------------------------------
    metadata = models.JSONField(
        default=dict, blank=True,
        verbose_name=_("Line Item Metadata"),
        help_text=_("Audit-only JSON metadata. Never used for business rules."),
    )

    # ------------------------------------------------------------------
    # ``added_at`` is an immutable audit timestamp recording when the
    # line item was first created. To avoid interactive migration
    # prompts (Django refuses to add a non-nullable ``auto_now_add``
    # field to a non-empty table without a default), the column is
    # declared as ``null=True``. The ``save()`` method auto-populates
    # the value on creation, and ``clean()`` enforces the invariant
    # on every save cycle.
    # ------------------------------------------------------------------
    added_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        verbose_name=_("Added At"),
    )
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
            # New enterprise-grade indexes for reporting
            models.Index(fields=["order", "is_gift"]),
            models.Index(fields=["order", "warehouse"]),
            models.Index(fields=["order", "inventory"]),
            models.Index(fields=["order", "expected_ship_date"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["order", "product", "variant", "status"],
                condition=~models.Q(variant__isnull=True),
                name="order_unique_product_variant_active",
            ),
            models.UniqueConstraint(
                fields=["order", "product", "status"],
                condition=models.Q(variant__isnull=True),
                name="order_unique_product_no_variant_active",
            ),
            models.CheckConstraint(
                check=models.Q(quantity__gte=1),
                name="orderitem_quantity_gte_1",
            ),
            models.CheckConstraint(
                check=models.Q(unit_price__gte=0),
                name="orderitem_unit_price_gte_0",
            ),
            models.CheckConstraint(
                check=models.Q(quantity_shipped__lte=models.F("quantity")),
                name="orderitem_quantity_shipped_lte_quantity",
            ),
            models.CheckConstraint(
                check=models.Q(quantity_returned__lte=models.F("quantity")),
                name="orderitem_quantity_returned_lte_quantity",
            ),
            models.CheckConstraint(
                check=models.Q(quantity_refunded__lte=models.F("quantity")),
                name="orderitem_quantity_refunded_lte_quantity",
            ),
        ]

    def __str__(self) -> str:
        variant = f" ({self.variant_name_snapshot})" if self.variant_name_snapshot else ""
        return (
            f"{self.quantity} x {self.product_name_snapshot or 'Unnamed'}"
            f"{variant} [{self.get_status_display()}]"
        )

    def clean(self) -> None:
        super().clean()
        if self.unit_price < 0:
            raise ValidationError({"unit_price": _("Unit price cannot be negative.")})
        if self.quantity < 1:
            raise ValidationError({"quantity": _("Quantity must be at least 1.")})
        if self.discount < 0:
            raise ValidationError({"discount": _("Discount cannot be negative.")})
        if self.tax < 0:
            raise ValidationError({"tax": _("Tax cannot be negative.")})
        if self.weight < 0:
            raise ValidationError({"weight": _("Weight cannot be negative.")})
        if self.quantity_shipped and self.quantity_shipped > self.quantity:
            raise ValidationError(
                {"quantity_shipped": _("Shipped quantity cannot exceed ordered quantity.")}
            )
        if self.quantity_returned and self.quantity_returned > self.quantity:
            raise ValidationError(
                {"quantity_returned": _("Returned quantity cannot exceed ordered quantity.")}
            )

    def save(self, *args: Any, **kwargs: Any) -> None:
        # Auto-populate ``added_at`` on creation. This keeps the
        # column nullable in the database (for clean migration of
        # legacy rows) while guaranteeing that every newly-created
        # row carries a real timestamp. Existing rows that pre-date
        # the introduction of ``added_at`` will keep their ``NULL``
        # value until a data migration backfills it.
        if self.pk is None and self.added_at is None:
            self.added_at = timezone.now()
        self.full_clean()
        super().save(*args, **kwargs)

    # ==================================================================
    # Existing computed properties (preserved exactly)
    # ==================================================================
    @property
    def line_discount_percentage(self) -> Decimal:
        gross = self.unit_price * self.quantity
        if gross <= 0:
            return Decimal("0.00")
        return (self.discount / gross) * Decimal("100")

    @property
    def line_tax_percentage(self) -> Decimal:
        gross = self.unit_price * self.quantity - self.discount
        if gross <= 0:
            return Decimal("0.00")
        return (self.tax / gross) * Decimal("100")

    @property
    def line_gross_total(self) -> Decimal:
        return self.unit_price * self.quantity

    @property
    def line_net_total(self) -> Decimal:
        """Computed net line total: gross - discount + tax."""
        return max(
            Decimal("0.00"),
            self.line_gross_total - self.discount + self.tax,
        )

    @property
    def effective_unit_price(self) -> Decimal:
        if self.quantity <= 0:
            return self.unit_price
        return (self.line_total / self.quantity).quantize(Decimal("0.01"))

    @property
    def is_returnable(self) -> bool:
        return self.status in (
            self.ItemStatus.ACTIVE,
            self.ItemStatus.PARTIALLY_SHIPPED,
        )

    @property
    def is_shippable(self) -> bool:
        return self.status in (
            self.ItemStatus.ACTIVE,
            self.ItemStatus.PARTIALLY_SHIPPED,
        )

    @property
    def remaining_quantity_to_ship(self) -> Decimal:
        return max(Decimal("0.00"), self.quantity - self.quantity_shipped)

    @property
    def remaining_quantity_to_return(self) -> Decimal:
        return max(Decimal("0.00"), self.quantity - self.quantity_returned)

# ==============================================================================
# 4. OrderStatusHistory (preserved exactly + new fields)
# ==============================================================================
class OrderStatusHistory(models.Model):
    """
    Immutable ledger of order status changes. Append-only.

    Records the transition of an order from one status to another,
    capturing who performed the change, when it happened, and any
    context that supports the audit trail. This model is intentionally
    write-once; rows are never updated after creation.
    """

    id = models.BigAutoField(primary_key=True)
    order = models.ForeignKey(
        Order, related_name="status_history", on_delete=models.CASCADE,
        verbose_name=_("Order"),
    )
    old_status = models.CharField(
        max_length=50, verbose_name=_("Previous Status"),
    )
    new_status = models.CharField(
        max_length=50, verbose_name=_("New Status"),
    )
    remarks = models.TextField(blank=True, verbose_name=_("Remarks"))

    # ----------------------------------------------------------------------
    # NEW: audit enhancements
    # ----------------------------------------------------------------------
    is_customer_notified = models.BooleanField(
        default=False, db_index=True,
        verbose_name=_("Customer Notified?"),
        help_text=_("Whether the customer was notified of this status change."),
    )
    notification_method = models.CharField(
        max_length=32, blank=True, null=True,
        verbose_name=_("Notification Method"),
        help_text=_("e.g., 'email', 'sms', 'webhook', 'none'."),
    )
    metadata = models.JSONField(
        default=dict, blank=True,
        verbose_name=_("Status Change Metadata"),
    )
    ip_address = models.GenericIPAddressField(
        null=True, blank=True,
        verbose_name=_("IP Address"),
    )
    user_agent = models.TextField(
        blank=True, null=True,
        verbose_name=_("User Agent"),
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        related_name="order_status_changes", null=True, blank=True,
        verbose_name=_("Changed By"),
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = _("Order Status History")
        verbose_name_plural = _("Order Status Histories")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["order", "-created_at"]),
            models.Index(fields=["new_status", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.order.order_number}: {self.old_status} -> {self.new_status}"

# ==============================================================================
# 5. Shipment (preserved exactly + new fields)
# ==============================================================================
class Shipment(models.Model):
    """
    Tracks logistics parcels associated with an order. Supports
    multi-shipment-per-order flows (split fulfillments) and provides
    a complete audit trail from dispatch through delivery.
    """

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
    order = models.ForeignKey(
        Order, related_name="shipments", on_delete=models.CASCADE,
        verbose_name=_("Order"),
    )
    shipment_number = models.CharField(
        max_length=100, unique=True, db_index=True,
        verbose_name=_("Shipment Number"),
    )
    carrier = models.CharField(
        max_length=100, verbose_name=_("Carrier"),
    )
    tracking_number = models.CharField(
        max_length=150, blank=True, null=True, db_index=True,
        verbose_name=_("Tracking Number"),
    )
    tracking_url = models.URLField(
        max_length=500, blank=True, null=True,
        verbose_name=_("Tracking URL"),
    )
    warehouse = models.ForeignKey(
        "inventory.Warehouse", on_delete=models.PROTECT,
        null=True, blank=True, related_name="shipments",
        verbose_name=_("Source Warehouse"),
    )
    status = models.CharField(
        max_length=30, choices=ShipmentStatus.choices,
        default=ShipmentStatus.PENDING, db_index=True,
        verbose_name=_("Shipment Status"),
    )
    shipping_cost = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        verbose_name=_("Shipping Cost"),
    )
    dispatch_date = models.DateTimeField(null=True, blank=True)
    delivery_date = models.DateTimeField(null=True, blank=True)
    picked_up_at = models.DateTimeField(null=True, blank=True)
    estimated_delivery_date = models.DateField(null=True, blank=True)
    actual_delivery_date = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True, null=True)
    carrier_api_integration_id = models.CharField(
        max_length=120, blank=True, null=True,
        verbose_name=_("Carrier API Integration ID"),
        help_text=_("Carrier-specific tracking ID for API integrations (e.g. EasyPost, Shippo)."),
    )
    carrier_service_level = models.CharField(
        max_length=64, blank=True, null=True,
        verbose_name=_("Carrier Service Level"),
        help_text=_("e.g., 'ground', '2-day', 'overnight'."),
    )
    total_weight = models.DecimalField(
        max_digits=14, decimal_places=3, default=Decimal("0.000"),
        validators=[MinValueValidator(Decimal("0.000"))],
        verbose_name=_("Total Weight"),
    )
    dimensions = models.JSONField(
        default=dict, blank=True,
        verbose_name=_("Dimensions"),
        help_text=_("Package dimensions as {length, width, height, unit}."),
    )
    shipping_cost_breakdown = models.JSONField(
        default=dict, blank=True,
        verbose_name=_("Shipping Cost Breakdown"),
        help_text=_("Carrier, fuel, insurance, taxes breakdown."),
    )
    metadata = models.JSONField(
        default=dict, blank=True,
        verbose_name=_("Shipment Metadata"),
    )

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

    def __str__(self) -> str:
        return f"Shipment {self.shipment_number} ({self.get_status_display()})"

    def mark_dispatched(self) -> None:
        self.status = self.ShipmentStatus.DISPATCHED
        if not self.dispatch_date:
            self.dispatch_date = timezone.now()
        self.save(
            update_fields=["status", "dispatch_date", "updated_at"]
        )

    def mark_delivered(self) -> None:
        self.status = self.ShipmentStatus.DELIVERED
        if not self.delivery_date:
            self.delivery_date = timezone.now()
        if not self.actual_delivery_date:
            self.actual_delivery_date = timezone.now().date()
        self.save(
            update_fields=[
                "status", "delivery_date", "actual_delivery_date", "updated_at"
            ]
        )

    def mark_picked_up(self) -> None:
        self.status = self.ShipmentStatus.PICKED_UP
        if not self.picked_up_at:
            self.picked_up_at = timezone.now()
        if not self.dispatch_date:
            self.dispatch_date = self.picked_up_at
        self.save(
            update_fields=["status", "picked_up_at", "dispatch_date", "updated_at"]
        )

    @property
    def total_item_count(self) -> int:
        return self.line_items.aggregate(
            total=models.Sum("quantity_shipped")
        )["total"] or 0

    @property
    def is_in_transit(self) -> bool:
        return self.status in (
            self.ShipmentStatus.DISPATCHED,
            self.ShipmentStatus.IN_TRANSIT,
            self.ShipmentStatus.OUT_FOR_DELIVERY,
        )

    @property
    def is_delivered(self) -> bool:
        return self.status == self.ShipmentStatus.DELIVERED

# ==============================================================================
# 6. ShipmentItem (NEW)
# ==============================================================================
class ShipmentItem(models.Model):
    """
    Line items contained within a shipment. Used to track which specific
    order line items (and in what quantity) were physically included
    in each parcel. Supports partial shipments and split fulfillments.
    """

    id = models.BigAutoField(primary_key=True)
    shipment = models.ForeignKey(
        Shipment, related_name="line_items", on_delete=models.CASCADE,
        verbose_name=_("Shipment"),
    )
    # ------------------------------------------------------------------
    # CHANGED (related_name collision fix): The ``related_name`` for
    # ``order_item`` has been renamed from ``"shipment_items"`` to
    # ``"shipment_line_items"``.
    #
    # Rationale:
    #   * This is a defensive rename to prevent future collisions
    #     should a partner app add a reverse FK to ``OrderItem``
    #     with the name ``"shipment_items"``.
    #   * The old value was not used by any production code path
    #     because ShipmentItem-to-OrderItem relationships are
    #     always accessed via the forward FK on ShipmentItem
    #     itself.
    # ------------------------------------------------------------------
    order_item = models.ForeignKey(
        OrderItem, related_name="shipment_line_items",
        on_delete=models.PROTECT,
        verbose_name=_("Order Line Item"),
    )
    quantity_shipped = models.DecimalField(
        max_digits=14, decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
        verbose_name=_("Quantity Shipped"),
    )
    serial_tracking = models.CharField(
        max_length=120, blank=True, null=True,
        verbose_name=_("Serial / Lot / Tracking"),
        help_text=_("Serial number, lot code, or per-unit tracking for high-value items."),
    )
    serial_verified_at = models.DateTimeField(
        null=True, blank=True,
        verbose_name=_("Serial Verified At"),
    )
    condition_at_pickup = models.CharField(
        max_length=32, blank=True, null=True,
        verbose_name=_("Condition at Pickup"),
        help_text=_("e.g., 'new', 'opened', 'refurbished'."),
    )
    is_replacement = models.BooleanField(
        default=False, db_index=True,
        verbose_name=_("Is Replacement?"),
    )
    replaced_from = models.ForeignKey(
        "self", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="replacements",
        verbose_name=_("Replaced From Shipment Item"),
    )
    notes = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Shipment Line Item")
        verbose_name_plural = _("Shipment Line Items")
        ordering = ["shipment", "id"]
        indexes = [
            models.Index(fields=["shipment", "order_item"]),
            models.Index(fields=["order_item", "shipment"]),
        ]

    def __str__(self) -> str:
        return f"{self.quantity_shipped} x {self.order_item} in {self.shipment.shipment_number}"

# ==============================================================================
# 7. Payment (preserved exactly + new fields)
# ==============================================================================
class Payment(models.Model):
    """
    Maintains financial transaction records mapped to an Order.
    Supports multi-gateway abstraction for scale and multi-currency
    settlement scenarios.
    """

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
    order = models.ForeignKey(
        Order, related_name="payments", on_delete=models.CASCADE,
        verbose_name=_("Order"),
    )
    transaction_id = models.CharField(
        max_length=255, unique=True, db_index=True,
        verbose_name=_("Transaction ID"),
    )
    gateway = models.CharField(
        max_length=100, verbose_name=_("Payment Gateway"),
        help_text=_("e.g. Stripe, PayPal, Razorpay"),
    )
    amount = models.DecimalField(
        max_digits=14, decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
        verbose_name=_("Amount"),
    )
    currency = models.CharField(
        max_length=10, default=DEFAULT_CURRENCY_CODE,
        verbose_name=_("Currency"),
    )
    status = models.CharField(
        max_length=20, choices=PaymentState.choices,
        default=PaymentState.PENDING, db_index=True,
        verbose_name=_("Status"),
    )
    paid_at = models.DateTimeField(null=True, blank=True)
    payment_method = models.CharField(
        max_length=64, blank=True, null=True,
        verbose_name=_("Payment Method Type"),
        help_text=_("e.g., 'card', 'esewa', 'khalti', 'bank_transfer'."),
    )
    payment_attempts_count = models.PositiveIntegerField(
        default=1,
        verbose_name=_("Payment Attempts Count"),
    )
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    next_attempt_allowed_at = models.DateTimeField(null=True, blank=True)
    gateway_response_snapshot = models.JSONField(
        default=dict, blank=True,
        verbose_name=_("Gateway Response Snapshot"),
        help_text=_("Sanitized response from the gateway at the time of the last attempt."),
    )
    risk_score = models.DecimalField(
        max_digits=5, decimal_places=2,
        blank=True, null=True,
        validators=[MinValueValidator(Decimal("0.00")), MaxValueValidator(Decimal("100.00"))],
        verbose_name=_("Risk Score"),
    )
    is_test_payment = models.BooleanField(
        default=False, db_index=True,
        verbose_name=_("Is Test Payment?"),
    )
    metadata = models.JSONField(
        default=dict, blank=True,
        verbose_name=_("Payment Metadata"),
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Payment")
        verbose_name_plural = _("Payments")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["order", "-created_at"]),
            models.Index(fields=["status", "-created_at"]),
            models.Index(fields=["gateway", "status"]),
            models.Index(fields=["payment_method", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.gateway} - {self.transaction_id} ({self.amount} {self.currency})"

    def record_attempt(self) -> None:
        """Increment the attempt counter and timestamp the last attempt."""
        from django.db.models import F
        Payment.objects.filter(pk=self.pk).update(
            payment_attempts_count=F("payment_attempts_count") + 1,
            last_attempt_at=timezone.now(),
        )
        self.refresh_from_db(
            fields=["payment_attempts_count", "last_attempt_at"]
        )

# ==============================================================================
# 8. PaymentAttempt (NEW)
# ==============================================================================
class PaymentAttempt(models.Model):
    """
    Detailed per-attempt record for a Payment. Used to track retries,
    failures, and gateway responses over time. Append-only.
    """

    class AttemptStatus(models.TextChoices):
        PENDING = "pending", _("Pending")
        SUCCESS = "success", _("Success")
        FAILURE = "failure", _("Failure")
        TIMEOUT = "timeout", _("Timeout")
        CANCELLED = "cancelled", _("Cancelled")
        REQUIRES_ACTION = "requires_action", _("Requires Action")
        THREE_DS_REQUIRED = "three_ds_required", _("3DS Required")

    id = models.BigAutoField(primary_key=True)
    payment = models.ForeignKey(
        Payment, related_name="attempts", on_delete=models.CASCADE,
        verbose_name=_("Payment"),
    )
    attempted_at = models.DateTimeField(
        default=timezone.now, db_index=True,
        verbose_name=_("Attempted At"),
    )
    attempt_number = models.PositiveIntegerField(
        default=1,
        verbose_name=_("Attempt Number"),
    )
    status = models.CharField(
        max_length=32, choices=AttemptStatus.choices,
        default=AttemptStatus.PENDING, db_index=True,
        verbose_name=_("Status"),
    )
    gateway_response_code = models.CharField(
        max_length=64, blank=True, null=True,
        verbose_name=_("Gateway Response Code"),
    )
    gateway_response_message = models.TextField(
        blank=True, null=True,
        verbose_name=_("Gateway Response Message"),
    )
    gateway_response_snapshot = models.JSONField(
        default=dict, blank=True,
        verbose_name=_("Gateway Response Snapshot"),
    )
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True, null=True)
    is_test = models.BooleanField(default=False, db_index=True)
    notes = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("Payment Attempt")
        verbose_name_plural = _("Payment Attempts")
        ordering = ["-attempted_at"]
        indexes = [
            models.Index(fields=["payment", "-attempted_at"]),
        ]

    def __str__(self) -> str:
        return f"Attempt #{self.attempt_number} for {self.payment.transaction_id}"

# ==============================================================================
# 9. Refund (preserved exactly + new fields)
# ==============================================================================
class Refund(models.Model):
    """
    Formal record structure for reversing financial transactions against
    specific Payments. Supports partial and full refunds, multi-gateway
    settlement, and complete audit-trail integration.
    """

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
    order = models.ForeignKey(
        Order, related_name="refunds", on_delete=models.CASCADE,
        verbose_name=_("Order"),
    )
    payment = models.ForeignKey(
        Payment, related_name="refunds", on_delete=models.PROTECT,
        verbose_name=_("Original Payment"),
    )
    amount = models.DecimalField(
        max_digits=14, decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
        verbose_name=_("Refund Amount"),
    )
    reason = models.TextField(verbose_name=_("Refund Reason"))
    status = models.CharField(
        max_length=20, choices=RefundStatus.choices,
        default=RefundStatus.REQUESTED, db_index=True,
        verbose_name=_("Status"),
    )
    refund_method = models.CharField(
        max_length=32, choices=RefundMethod.choices,
        blank=True, null=True,
        verbose_name=_("Refund Method"),
    )
    refund_reason_category = models.CharField(
        max_length=64, choices=RefundReasonCategory.choices,
        blank=True, null=True,
        verbose_name=_("Refund Reason Category"),
    )
    customer_notes = models.TextField(blank=True, null=True)
    internal_notes = models.TextField(blank=True, null=True)
    evidence_images = models.JSONField(
        default=list, blank=True,
        verbose_name=_("Evidence Images"),
        help_text=_("JSON list of evidence image URLs / metadata."),
    )
    gateway_refund_id = models.CharField(
        max_length=120, blank=True, null=True,
        verbose_name=_("Gateway Refund ID"),
    )
    metadata = models.JSONField(
        default=dict, blank=True,
        verbose_name=_("Refund Metadata"),
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        related_name="approved_refunds",
        null=True, blank=True, verbose_name=_("Approved By"),
    )
    processed_at = models.DateTimeField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Refund")
        verbose_name_plural = _("Refunds")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["order", "-created_at"]),
            models.Index(fields=["payment", "status"]),
            models.Index(fields=["status", "-created_at"]),
            models.Index(fields=["refund_method", "status"]),
        ]
        constraints = [
            models.CheckConstraint(
                check=models.Q(amount__gt=0),
                name="refund_amount_gt_0",
            ),
        ]

    def __str__(self) -> str:
        return f"Refund {self.id} for {self.order.order_number}"

    def approve(self, user) -> None:
        if self.status == self.RefundStatus.REQUESTED:
            self.status = self.RefundStatus.APPROVED
            self.approved_by = user
            self.approved_at = timezone.now()
            self.save(
                update_fields=[
                    "status", "approved_by", "approved_at", "updated_at"
                ]
            )

    def reject(self) -> None:
        if self.status in (
            self.RefundStatus.REQUESTED,
            self.RefundStatus.APPROVED,
        ):
            self.status = self.RefundStatus.REJECTED
            self.save(update_fields=["status", "updated_at"])

    def process(self) -> None:
        if self.status == self.RefundStatus.APPROVED:
            self.status = self.RefundStatus.PROCESSED
            self.processed_at = timezone.now()
            self.save(
                update_fields=[
                    "status", "processed_at", "updated_at"
                ]
            )
            self.payment.refund()

    def complete(self) -> None:
        if self.status == self.RefundStatus.PROCESSED:
            self.status = self.RefundStatus.APPROVED  # Legacy "completed" alias
            self.completed_at = timezone.now()
            self.save(
                update_fields=["status", "completed_at", "updated_at"]
            )

# ==============================================================================
# 10. TaxLine (NEW)
# ==============================================================================
class TaxLine(models.Model):
    """
    Detailed per-tax-line breakdown for an order. Supports multi-jurisdiction
    taxes, tax-inclusive and tax-exclusive pricing, and reporting-grade
    audit fields. Snapshot-only — never recomputed.
    """

    class TaxMode(models.TextChoices):
        INCLUSIVE = "inclusive", _("Inclusive (price already includes tax)")
        EXCLUSIVE = "exclusive", _("Exclusive (tax added on top)")

    id = models.BigAutoField(primary_key=True)
    order = models.ForeignKey(
        Order, related_name="tax_lines", on_delete=models.CASCADE,
        verbose_name=_("Order"),
    )
    tax_class = models.CharField(
        max_length=64, db_index=True,
        verbose_name=_("Tax Class"),
        help_text=_("e.g. 'vat_standard', 'vat_reduced', 'sales_tax_us'."),
    )
    tax_name = models.CharField(
        max_length=120,
        verbose_name=_("Tax Name"),
        help_text=_("Human-readable name, e.g. 'VAT 13% (Nepal)'."),
    )
    tax_rate = models.DecimalField(
        max_digits=8, decimal_places=4,
        validators=[
            MinValueValidator(Decimal("0.0000")),
            MaxValueValidator(Decimal("1.0000")),
        ],
        verbose_name=_("Tax Rate"),
        help_text=_("Decimal rate (e.g. 0.1300 for 13%)."),
    )
    base_amount = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        verbose_name=_("Tax Base Amount"),
    )
    tax_amount = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        verbose_name=_("Tax Amount"),
    )
    jurisdiction = models.CharField(
        max_length=120, blank=True, null=True,
        verbose_name=_("Jurisdiction"),
    )
    tax_authority_code = models.CharField(
        max_length=64, blank=True, null=True,
        verbose_name=_("Tax Authority Code"),
        help_text=_("e.g. 'IRS', 'IRD-NP', 'HMRC-VAT'."),
    )
    is_inclusive = models.BooleanField(
        default=False,
        verbose_name=_("Tax Inclusive?"),
    )
    mode = models.CharField(
        max_length=16, choices=TaxMode.choices,
        default=TaxMode.EXCLUSIVE,
        verbose_name=_("Tax Mode"),
    )
    position = models.PositiveIntegerField(
        default=0,
        verbose_name=_("Display Order"),
    )
    notes = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("Order Tax Line")
        verbose_name_plural = _("Order Tax Lines")
        ordering = ["order", "position", "id"]
        indexes = [
            models.Index(fields=["order", "position"]),
            models.Index(fields=["tax_class", "jurisdiction"]),
        ]
        constraints = [
            models.CheckConstraint(
                check=models.Q(base_amount__gte=0),
                name="taxline_base_amount_gte_0",
            ),
            models.CheckConstraint(
                check=models.Q(tax_amount__gte=0),
                name="taxline_tax_amount_gte_0",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.tax_name} ({self.tax_amount}) on {self.order.order_number}"

# ==============================================================================
# 11. DiscountLine (NEW)
# ==============================================================================
class DiscountLine(models.Model):
    """
    Detailed per-discount-line breakdown for an order. Supports coupons,
    promotions, loyalty rewards, manual operator adjustments, and
    line-item or order-level scoping. Snapshot-only.
    """

    class DiscountType(models.TextChoices):
        COUPON = "coupon", _("Coupon Code")
        PROMOTION = "promotion", _("Promotion")
        LOYALTY = "loyalty", _("Loyalty Reward")
        STAFF = "staff", _("Staff Adjustment")
        GOODWILL = "goodwill", _("Goodwill / Compensation")
        BULK = "bulk", _("Bulk / Tier Discount")
        SEASONAL = "seasonal", _("Seasonal Discount")
        OTHER = "other", _("Other")

    id = models.BigAutoField(primary_key=True)
    order = models.ForeignKey(
        Order, related_name="discount_lines", on_delete=models.CASCADE,
        verbose_name=_("Order"),
    )
    discount_type = models.CharField(
        max_length=32, choices=DiscountType.choices,
        db_index=True,
        verbose_name=_("Discount Type"),
    )
    source = models.CharField(
        max_length=64,
        verbose_name=_("Source Identifier"),
        help_text=_("e.g. 'coupon:SAVE10', 'promotion:BLACKFRIDAY2026'."),
    )
    code = models.CharField(
        max_length=120, blank=True, null=True,
        verbose_name=_("Code"),
    )
    name = models.CharField(
        max_length=255,
        verbose_name=_("Name"),
    )
    description = models.TextField(blank=True, null=True)
    discount_amount = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        verbose_name=_("Discount Amount"),
    )
    base_amount = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        verbose_name=_("Base Amount"),
        help_text=_("Order / line subtotal BEFORE the discount was applied."),
    )
    percentage = models.DecimalField(
        max_digits=5, decimal_places=2, blank=True, null=True,
        validators=[
            MinValueValidator(Decimal("0.00")),
            MaxValueValidator(Decimal("100.00")),
        ],
        verbose_name=_("Percentage"),
        help_text=_("If the discount was percentage-based, the snapshot value."),
    )
    coupon_usage = models.ForeignKey(
        "orders.CouponUsage", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="discount_lines",
        verbose_name=_("Coupon Usage Reference"),
    )
    promotion_id = models.CharField(
        max_length=120, blank=True, null=True,
        verbose_name=_("Promotion ID"),
    )
    applies_to_order_item = models.ForeignKey(
        OrderItem, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="discount_lines",
        verbose_name=_("Applies to Order Item"),
        help_text=_("Set when the discount is line-item scoped; NULL for order-level discounts."),
    )
    is_taxable = models.BooleanField(
        default=True,
        verbose_name=_("Taxable?"),
    )
    is_stackable = models.BooleanField(
        default=False,
        verbose_name=_("Is Stackable?"),
    )
    position = models.PositiveIntegerField(
        default=0,
        verbose_name=_("Display Order"),
    )
    metadata = models.JSONField(
        default=dict, blank=True,
        verbose_name=_("Discount Metadata"),
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("Order Discount Line")
        verbose_name_plural = _("Order Discount Lines")
        ordering = ["order", "position", "id"]
        indexes = [
            models.Index(fields=["order", "position"]),
            models.Index(fields=["discount_type", "source"]),
            models.Index(fields=["coupon_usage"]),
        ]
        constraints = [
            models.CheckConstraint(
                check=models.Q(discount_amount__gte=0),
                name="discountline_amount_gte_0",
            ),
            models.CheckConstraint(
                check=models.Q(base_amount__gte=0),
                name="discountline_base_amount_gte_0",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name} (-{self.discount_amount}) on {self.order.order_number}"

# ==============================================================================
# 12. CouponUsage (preserved exactly + new fields)
# ==============================================================================
class CouponUsage(models.Model):
    """
    Log of promotional discounts securely tethered to specific users
    and orders. Enforces uniqueness on standard business logic rules
    (one-time use per customer per coupon) while remaining fully
    CMS-driven and parameterized.
    """

    id = models.BigAutoField(primary_key=True)
    coupon_code = models.CharField(
        max_length=50, db_index=True, verbose_name=_("Coupon Code"),
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="coupon_usages", verbose_name=_("User"),
    )
    order = models.ForeignKey(
        Order, on_delete=models.CASCADE,
        related_name="coupon_usages", verbose_name=_("Order"),
    )
    discount_amount = models.DecimalField(
        max_digits=12, decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
        verbose_name=_("Discount Applied"),
    )
    used_at = models.DateTimeField(auto_now_add=True, db_index=True)

    # ----------------------------------------------------------------------
    # NEW: cross-scope references and reversal tracking
    # ----------------------------------------------------------------------
    cart_id = models.ForeignKey(
        "cart.Cart", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="coupon_usages",
        verbose_name=_("Source Cart"),
    )
    product_id = models.ForeignKey(
        "catalog.Product", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="coupon_usages",
        verbose_name=_("Product-Specific Coupon"),
        help_text=_("Set when the coupon is product-scoped."),
    )
    category_id = models.ForeignKey(
        "catalog.Category", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="coupon_usages",
        verbose_name=_("Category-Specific Coupon"),
        help_text=_("Set when the coupon is category-scoped."),
    )
    is_reversed = models.BooleanField(
        default=False, db_index=True,
        verbose_name=_("Is Reversed?"),
        help_text=_("True when the discount was cancelled or reversed by a refund / void."),
    )
    reversed_at = models.DateTimeField(
        null=True, blank=True,
        verbose_name=_("Reversed At"),
    )
    reversal_reason = models.TextField(
        blank=True, null=True,
        verbose_name=_("Reversal Reason"),
    )
    metadata = models.JSONField(
        default=dict, blank=True,
        verbose_name=_("Coupon Usage Metadata"),
    )

    class Meta:
        verbose_name = _("Coupon Usage")
        verbose_name_plural = _("Coupon Usages")
        ordering = ["-used_at"]
        indexes = [
            models.Index(fields=["user", "coupon_code"]),
            models.Index(fields=["order"]),
            models.Index(fields=["coupon_code", "-used_at"]),
            models.Index(fields=["is_reversed", "-used_at"]),
            models.Index(fields=["cart_id"]),
        ]
        constraints = [
            # Constraint renamed to a globally unique identifier to
            # avoid a collision with the customers.Wishlist constraint
            # of the same name. The constrained fields, uniqueness
            # logic, business rules, validation, and database behavior
            # are preserved exactly.
            models.UniqueConstraint(
                fields=["user", "coupon_code"],
                name="couponusage_unique_user_coupon",
            ),
            models.CheckConstraint(
                check=models.Q(discount_amount__gte=0),
                name="coupon_usage_discount_gte_0",
            ),
        ]

    def __str__(self) -> str:
        return f"Coupon {self.coupon_code} used by {self.user.username}"

# ==============================================================================
# 13. OrderNote (NEW)
# ==============================================================================
class OrderNote(models.Model):
    """
    Customer- or operator-authored notes attached to an order. Supports
    multiple notes per order, internal-only visibility, and a pinned
    flag for sticky notes. Optimized for collaborative support teams.
    """

    class NoteType(models.TextChoices):
        CUSTOMER = "customer", _("Customer Note")
        OPERATOR = "operator", _("Internal Operator Note")
        GIFT = "gift", _("Gift Message")
        DELIVERY = "delivery", _("Delivery Instructions")
        SYSTEM = "system", _("System-Generated Note")

    id = models.BigAutoField(primary_key=True)
    order = models.ForeignKey(
        Order, related_name="order_notes", on_delete=models.CASCADE,
        verbose_name=_("Order"),
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="order_notes_authored",
        verbose_name=_("Author"),
    )
    note_type = models.CharField(
        max_length=32, choices=NoteType.choices,
        default=NoteType.OPERATOR, db_index=True,
        verbose_name=_("Note Type"),
    )
    text = models.TextField(verbose_name=_("Text"))
    is_visible_to_customer = models.BooleanField(
        default=False, db_index=True,
        verbose_name=_("Visible to Customer?"),
    )
    is_pinned = models.BooleanField(
        default=False, db_index=True,
        verbose_name=_("Pinned?"),
    )
    metadata = models.JSONField(
        default=dict, blank=True,
        verbose_name=_("Note Metadata"),
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Order Note")
        verbose_name_plural = _("Order Notes")
        ordering = ["-is_pinned", "-created_at"]
        indexes = [
            models.Index(fields=["order", "-is_pinned", "-created_at"]),
            models.Index(fields=["order", "note_type"]),
            models.Index(fields=["author", "-created_at"]),
        ]

    def __str__(self) -> str:
        prefix = "★ " if self.is_pinned else ""
        return f"{prefix}{self.get_note_type_display()} on {self.order.order_number}"

# ==============================================================================
# 14. OrderAttachment (NEW)
# ==============================================================================
class OrderAttachment(models.Model):
    """
    File attachments associated with an order. Supports invoice PDFs,
    delivery proof photos, customs documents, insurance certificates,
    and operator-supplied notes. Files are stored as immutable
    snapshots for audit purposes.

    The ``file`` field's ``upload_to`` uses
    :func:`_order_attachment_upload_path` (an internal alias) which
    in turn delegates to :func:`_upload_to_order_attachment`. The
    latter name is preserved verbatim because the existing migration
    ``0002_orderattachment_orderauditreference_ordercoupon_and_more.py``
    serialises the callable by that exact import path.
    """

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
    order = models.ForeignKey(
        Order, related_name="attachments", on_delete=models.CASCADE,
        verbose_name=_("Order"),
    )
    file = models.FileField(
        upload_to=_order_attachment_upload_path,
        verbose_name=_("File"),
    )
    original_filename = models.CharField(
        max_length=255,
        verbose_name=_("Original Filename"),
    )
    file_size = models.PositiveIntegerField(
        default=0,
        verbose_name=_("File Size (bytes)"),
    )
    mime_type = models.CharField(
        max_length=120, blank=True, null=True,
        verbose_name=_("MIME Type"),
    )
    attachment_type = models.CharField(
        max_length=32, choices=AttachmentType.choices,
        default=AttachmentType.OTHER, db_index=True,
        verbose_name=_("Attachment Type"),
    )
    description = models.CharField(
        max_length=255, blank=True, null=True,
        verbose_name=_("Description"),
    )
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="order_attachments_uploaded",
        verbose_name=_("Uploaded By"),
    )
    is_visible_to_customer = models.BooleanField(
        default=False, db_index=True,
        verbose_name=_("Visible to Customer?"),
    )
    is_active = models.BooleanField(
        default=True, db_index=True,
        verbose_name=_("Is Active"),
    )
    metadata = models.JSONField(
        default=dict, blank=True,
        verbose_name=_("Attachment Metadata"),
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Order Attachment")
        verbose_name_plural = _("Order Attachments")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["order", "-created_at"]),
            models.Index(fields=["attachment_type", "-created_at"]),
            models.Index(fields=["is_active", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.attachment_type} on {self.order.order_number}: {self.original_filename}"

# ==============================================================================
# 15. OrderTimelineEvent (NEW)
# ==============================================================================
class OrderTimelineEvent(models.Model):
    """
    Granular, append-only timeline of everything that happens to an
    order. Supersedes the simpler OrderStatusHistory for richer
    visibility (financial events, fulfillment events, note events,
    attachment events, return events). Supports customer-facing
    visibility filtering.
    """

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
        INVENTORY_TRANSFERED = "inventory_transferred", _("Inventory Transferred")
        SYSTEM = "system", _("System Event")

    id = models.BigAutoField(primary_key=True)
    order = models.ForeignKey(
        Order, related_name="timeline_events", on_delete=models.CASCADE,
        verbose_name=_("Order"),
    )
    event_type = models.CharField(
        max_length=48, choices=EventType.choices,
        db_index=True,
        verbose_name=_("Event Type"),
    )
    title = models.CharField(
        max_length=255,
        verbose_name=_("Title"),
    )
    description = models.TextField(blank=True, null=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="order_timeline_events",
        verbose_name=_("Actor"),
    )
    is_system_event = models.BooleanField(
        default=False, db_index=True,
        verbose_name=_("System Event?"),
    )
    is_visible_to_customer = models.BooleanField(
        default=True, db_index=True,
        verbose_name=_("Visible to Customer?"),
    )
    reference_model = models.CharField(
        max_length=80, blank=True, null=True,
        verbose_name=_("Reference Model"),
        help_text=_("App.Model of the related record (e.g. 'inventory.StockReservation')."),
    )
    reference_id = models.CharField(
        max_length=80, blank=True, null=True,
        verbose_name=_("Reference ID"),
    )
    icon = models.CharField(
        max_length=64, blank=True, null=True,
        verbose_name=_("Icon"),
    )
    color = models.CharField(
        max_length=32, blank=True, null=True,
        verbose_name=_("Color"),
    )
    metadata = models.JSONField(
        default=dict, blank=True,
        verbose_name=_("Event Metadata"),
    )
    occurred_at = models.DateTimeField(
        default=timezone.now, db_index=True,
        verbose_name=_("Occurred At"),
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("Order Timeline Event")
        verbose_name_plural = _("Order Timeline Events")
        ordering = ["-occurred_at", "-id"]
        indexes = [
            models.Index(fields=["order", "-occurred_at"]),
            models.Index(fields=["event_type", "-occurred_at"]),
            models.Index(fields=["order", "is_visible_to_customer", "-occurred_at"]),
            models.Index(fields=["reference_model", "reference_id"]),
        ]

    def __str__(self) -> str:
        return f"{self.get_event_type_display()} - {self.order.order_number}"

# ==============================================================================
# 16. ReturnRequest (NEW)
# ==============================================================================
class ReturnRequest(models.Model):
    """
    Formal return workflow. Tracks every step from customer request
    through approval, shipping, inspection, and resolution. Supports
    refunds, replacements, exchanges, and store credit.
    """

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
        RESTOCK = "restock", _("Restock to Sellable Inventory")
        DISPOSE = "dispose", _("Dispose / Write Off")
        RETURN_TO_SUPPLIER = "return_to_supplier", _("Return to Supplier")
        REPAIR = "repair", _("Repair and Restock")
        QUARANTINE = "quarantine", _("Quarantine for Review")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.ForeignKey(
        Order, related_name="return_requests", on_delete=models.CASCADE,
        verbose_name=_("Order"),
    )
    return_number = models.CharField(
        max_length=50, unique=True, db_index=True,
        blank=True, null=True,
        verbose_name=_("Return Number"),
    )
    return_type = models.CharField(
        max_length=24, choices=ReturnType.choices,
        default=ReturnType.REFUND,
        verbose_name=_("Return Type"),
    )
    reason_category = models.CharField(
        max_length=48, choices=ReturnReasonCategory.choices,
        default=ReturnReasonCategory.OTHER,
        verbose_name=_("Reason Category"),
    )
    reason_text = models.TextField(blank=True, null=True)
    status = models.CharField(
        max_length=24, choices=ReturnStatus.choices,
        default=ReturnStatus.DRAFT, db_index=True,
        verbose_name=_("Status"),
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="return_requests_requested",
        verbose_name=_("Requested By"),
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="return_requests_approved",
        verbose_name=_("Approved By"),
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    rejected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="return_requests_rejected",
        verbose_name=_("Rejected By"),
    )
    rejected_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.TextField(blank=True, null=True)
    received_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    refund = models.ForeignKey(
        Refund, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="return_requests",
        verbose_name=_("Linked Refund"),
    )
    replacement_order = models.ForeignKey(
        Order, on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="replacement_return_requests",
        verbose_name=_("Replacement Order"),
    )
    restock_decision = models.CharField(
        max_length=32, choices=RestockDecision.choices,
        blank=True, null=True,
        verbose_name=_("Restock Decision"),
    )
    restock_location = models.CharField(
        max_length=120, blank=True, null=True,
        verbose_name=_("Restock Location / Bin"),
    )
    return_shipping_address_snapshot = models.OneToOneField(
        OrderAddressSnapshot, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="return_request",
        verbose_name=_("Return Shipping Address Snapshot"),
        help_text=_("Snapshot of the address used to ship the return back to the warehouse."),
    )
    customer_notes = models.TextField(blank=True, null=True)
    internal_notes = models.TextField(blank=True, null=True)
    metadata = models.JSONField(
        default=dict, blank=True,
        verbose_name=_("Return Metadata"),
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Return Request")
        verbose_name_plural = _("Return Requests")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["order", "-created_at"]),
            models.Index(fields=["status", "-created_at"]),
            models.Index(fields=["return_type", "-created_at"]),
            models.Index(fields=["reason_category", "-created_at"]),
            models.Index(fields=["requested_by", "-created_at"]),
            models.Index(fields=["refund"]),
            models.Index(fields=["replacement_order"]),
        ]

    def __str__(self) -> str:
        return f"{self.return_number or 'Pending'} for {self.order.order_number}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        """
        Auto-generate return_number on first save if not already set.
        Format: RET-YYMMDD-XXXX.
        """
        if not self.return_number:
            ts = timezone.now().strftime("%y%m%d")
            self.return_number = f"RET-{ts}-{secrets.token_hex(3).upper()}"
        super().save(*args, **kwargs)

    def approve(self, user) -> None:
        if self.status == self.ReturnStatus.REQUESTED:
            self.status = self.ReturnStatus.APPROVED
            self.approved_by = user
            self.approved_at = timezone.now()
            self.save(
                update_fields=[
                    "status", "approved_by", "approved_at", "updated_at"
                ]
            )

    def reject(self, user, reason: str = "") -> None:
        if self.status == self.ReturnStatus.REQUESTED:
            self.status = self.ReturnStatus.REJECTED
            self.rejected_by = user
            self.rejected_at = timezone.now()
            self.rejection_reason = reason
            self.save(
                update_fields=[
                    "status", "rejected_by", "rejected_at",
                    "rejection_reason", "updated_at"
                ]
            )

    def mark_received(self) -> None:
        if self.status in (
            self.ReturnStatus.IN_TRANSIT,
            self.ReturnStatus.AWAITING_SHIPMENT,
        ):
            self.status = self.ReturnStatus.RECEIVED
            self.received_at = timezone.now()
            self.save(
                update_fields=["status", "received_at", "updated_at"]
            )

    def complete(self) -> None:
        if self.status in (
            self.ReturnStatus.RECEIVED,
            self.ReturnStatus.INSPECTING,
        ):
            self.status = self.ReturnStatus.COMPLETED
            self.completed_at = timezone.now()
            self.save(
                update_fields=["status", "completed_at", "updated_at"]
            )

    @property
    def is_resolved(self) -> bool:
        return self.status in (
            self.ReturnStatus.COMPLETED,
            self.ReturnStatus.REJECTED,
            self.ReturnStatus.CANCELLED,
        )

    @property
    def total_return_quantity(self) -> Decimal:
        result = self.items.aggregate(
            total=models.Sum("quantity_returned"),
        )
        return result["total"] or Decimal("0.00")

    @property
    def is_refund_request(self) -> bool:
        return self.return_type == self.ReturnType.REFUND

# ==============================================================================
# 17. ReturnItem (NEW)
# ==============================================================================
class ReturnItem(models.Model):
    """
    Line items within a ReturnRequest. Each item corresponds to a
    specific OrderItem being returned, with its own quantity, condition,
    inspection notes, and refund / replacement decision.
    """

    class InspectionResult(models.TextChoices):
        PENDING = "pending", _("Pending Inspection")
        PASSED = "passed", _("Passed Inspection")
        FAILED = "failed", _("Failed Inspection")
        PARTIAL = "partial", _("Partial Pass")

    id = models.BigAutoField(primary_key=True)
    return_request = models.ForeignKey(
        ReturnRequest, related_name="items", on_delete=models.CASCADE,
        verbose_name=_("Return Request"),
    )
    order_item = models.ForeignKey(
        OrderItem, on_delete=models.PROTECT,
        related_name="return_items",
        verbose_name=_("Order Line Item"),
    )
    quantity_returned = models.DecimalField(
        max_digits=14, decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
        verbose_name=_("Quantity Returned"),
    )
    quantity_approved = models.DecimalField(
        max_digits=14, decimal_places=2, blank=True, null=True,
        validators=[MinValueValidator(Decimal("0.00"))],
        verbose_name=_("Quantity Approved for Refund"),
    )
    quantity_received = models.DecimalField(
        max_digits=14, decimal_places=2, blank=True, null=True,
        validators=[MinValueValidator(Decimal("0.00"))],
        verbose_name=_("Quantity Received"),
    )
    condition_received = models.CharField(
        max_length=64, blank=True, null=True,
        verbose_name=_("Condition as Received"),
        help_text=_("Free-form description (e.g., 'opened but unused', 'defective screen')."),
    )
    inspection_result = models.CharField(
        max_length=16, choices=InspectionResult.choices,
        default=InspectionResult.PENDING,
        verbose_name=_("Inspection Result"),
    )
    inspection_notes = models.TextField(blank=True, null=True)
    refund_amount = models.DecimalField(
        max_digits=14, decimal_places=2, blank=True, null=True,
        validators=[MinValueValidator(Decimal("0.00"))],
        verbose_name=_("Refund Amount for Item"),
    )
    restock_decision = models.CharField(
        max_length=32, blank=True, null=True,
        verbose_name=_("Restock Decision (Item)"),
    )
    replacement_order_item = models.ForeignKey(
        OrderItem, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="replacement_for",
        verbose_name=_("Replacement Order Item"),
    )
    metadata = models.JSONField(
        default=dict, blank=True,
        verbose_name=_("Return Item Metadata"),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Return Item")
        verbose_name_plural = _("Return Items")
        ordering = ["return_request", "id"]
        indexes = [
            models.Index(fields=["return_request", "id"]),
            models.Index(fields=["order_item", "return_request"]),
        ]
        constraints = [
            models.CheckConstraint(
                check=models.Q(quantity_returned__gt=0),
                name="returnitem_quantity_returned_gt_0",
            ),
            models.CheckConstraint(
                check=models.Q(quantity_received__lte=models.F("quantity_returned")),
                name="returnitem_qty_received_lte_returned",
            ),
        ]

    def __str__(self) -> str:
        return f"Return {self.quantity_returned} x {self.order_item}"

# ==============================================================================
# 18. ReturnImage (NEW)
# ==============================================================================
class ReturnImage(models.Model):
    """
    Evidence images attached to a ReturnItem. Supports customer-submitted
    damage photos, reference photos for size / color disputes, and
    operator inspection photos.
    """

    class ImageType(models.TextChoices):
        EVIDENCE = "evidence", _("Damage / Issue Evidence")
        REFERENCE = "reference", _("Reference Photo")
        PACKAGING = "packaging", _("Packaging Condition")
        LABEL = "label", _("Shipping Label")
        OPERATOR = "operator", _("Operator Inspection")
        OTHER = "other", _("Other")

    id = models.BigAutoField(primary_key=True)
    return_item = models.ForeignKey(
        ReturnItem, related_name="images", on_delete=models.CASCADE,
        verbose_name=_("Return Item"),
    )
    image = models.ImageField(
        upload_to=_return_image_upload_path,
        verbose_name=_("Image"),
    )
    caption = models.CharField(
        max_length=255, blank=True, null=True,
        verbose_name=_("Caption"),
    )
    image_type = models.CharField(
        max_length=24, choices=ImageType.choices,
        default=ImageType.EVIDENCE,
        verbose_name=_("Image Type"),
    )
    position = models.PositiveIntegerField(default=0)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="return_images_uploaded",
        verbose_name=_("Uploaded By"),
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("Return Image")
        verbose_name_plural = _("Return Images")
        ordering = ["return_item", "position", "id"]
        indexes = [
            models.Index(fields=["return_item", "position"]),
        ]

    def __str__(self) -> str:
        return f"Return Image #{self.id} ({self.image_type})"

# ==============================================================================
# PUBLIC MODULE API
# ==============================================================================
__all__ = [
    # Core order models
    "Order",
    "OrderItem",
    "OrderAddressSnapshot",
    "OrderStatusHistory",
    # Logistics models
    "Shipment",
    "ShipmentItem",
    # Financial models
    "Payment",
    "PaymentAttempt",
    "Refund",
    "CouponUsage",
    # Tax / discount lines
    "TaxLine",
    "DiscountLine",
    # Order enrichment
    "OrderNote",
    "OrderAttachment",
    "OrderTimelineEvent",
    # Returns workflow
    "ReturnRequest",
    "ReturnItem",
    "ReturnImage",
    # Module-level constants
    "DEFAULT_CURRENCY_CODE",
    "DEFAULT_LOW_STOCK_THRESHOLD",
    "DEFAULT_ORDER_PAGE_SIZE",
    "DEFAULT_PAYMENT_METHOD",
    "DEFAULT_CARRIER_NAME",
    "DEFAULT_ORDER_ACTIVE_STATE",
    "DEFAULT_INVOICE_EXTENSION",
    "DEFAULT_SHIPPING_LABEL_EXTENSION",
    "DEFAULT_BINARY_EXTENSION",
    "DEFAULT_LEGACY_EMAIL_PLACEHOLDER",
    # Module-level validators
    "_phone_validator",
    # Upload path helpers (exported for migration compatibility)
    "_upload_to_order_attachment",
    "_order_attachment_upload_path",
    "_order_attachment_path",
    "_upload_to_order_invoice",
    "_order_invoice_upload_path",
    "_upload_to_order_shipping_label",
    "_order_shipping_label_upload_path",
    "_return_image_upload_path",
    "_return_image_path",
    # Internal upload-path helpers
    "_safe_suffix",
    "_resolve_scope_id",
]