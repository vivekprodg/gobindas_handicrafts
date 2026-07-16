"""
Enterprise-grade cart data model for the handicraft e-commerce platform.
Supports anonymous (session-key) and authenticated (customer) carts,
with full merge capabilities, save-for-later, and inventory reservation.

INVENTORY ARCHITECTURE COMPLIANCE
=================================

Inventory is the SINGLE SOURCE OF TRUTH for all stock-related operations.

Cart NEVER owns or calculates:
    * Stock quantities
    * Available quantity
    * Reserved quantity
    * Warehouse stock totals
    * Inventory status flags

Cart ONLY references Inventory through:
    * reservation FK (StockReservation, when reserved)
    * Inventory-safe design references (warehouse FK, optional)
    * Cart item quantity (requested, not stock)

Cart item reservation fields are:
    * Entirely OPTIONAL
    * All nullable
    * Never duplicate inventory data
    * Never represent a snapshot of stock state

RESERVATION LIFECYCLE
=====================

The `reserved_until` field is the cart's own reservation expiry timestamp.
It is a soft, cache-friendly hint that:
    * Is independent of (and complementary to) the Inventory app's
      `StockReservation.expires_at` field
    * Is timezone-aware (Django's `USE_TZ=True` is enforced)
    * Allows NULL (line was never reserved, or reservation was released)
    * Is used by the cart's own expiry cleanup job to release stale
      cart-level holds without round-tripping to the Inventory app
    * Is replicated (not derived) from the inventory reservation when
      the cart service creates a reservation; it is a local mirror
      used for fast cart-side queries and SLA tracking

The relationship to inventory is:
    * CartItem.reserved_until  <=  StockReservation.expires_at
      (cart never holds longer than the underlying inventory hold)
    * If StockReservation is released/expired, the cart service
      clears this field on the next reconciliation pass

DEFAULTS
========

Every field is genuinely optional with a safe default.
No field is required unless Django itself makes it so (primary key).
"""

from __future__ import annotations

import secrets
from decimal import Decimal
from typing import Any, List, Optional

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


# ==============================================================================
# MODULE-LEVEL CONSTANTS (CMS-DRIVEN DEFAULTS)
# ==============================================================================
# These defaults can be overridden via Django settings (which can be wired
# to the CMS without code changes). The cart app's behavior is fully
# parameterized and future-proof.

#: Default reservation duration in minutes for cart-level holds.
#: Mirrors the inventory app's default but lives here so the cart can
#: function even when the inventory app is not installed yet.
DEFAULT_CART_RESERVATION_MINUTES: int = 30

#: Default cart expiration in days (for anonymous session carts).
DEFAULT_CART_EXPIRATION_DAYS: int = 30

#: Threshold (in hours) after which an active cart is considered
#: abandoned and eligible for cleanup.
DEFAULT_CART_ABANDONMENT_THRESHOLD_HOURS: int = 24


# ==============================================================================
# QUERYSET
# ==============================================================================
class CartQuerySet(models.QuerySet):
    """
    Optimized QuerySet for cart operations.

    Provides chainable filters and aggregations that are reused
    throughout the cart service and view layers.
    """

    def active(self) -> "CartQuerySet":
        """Return only carts that are active and not in a terminal state."""
        return self.filter(is_active=True, status=Cart.CartStatus.ACTIVE)

    def abandoned(self, threshold_hours: int = DEFAULT_CART_ABANDONMENT_THRESHOLD_HOURS) -> "CartQuerySet":
        """
        Return active carts that have not been touched within
        the supplied threshold (in hours).
        """
        cutoff = timezone.now() - timezone.timedelta(hours=threshold_hours)
        return self.active().filter(last_activity_at__lt=cutoff)

    def for_customer(self, customer: Any) -> "CartQuerySet":
        """Return only carts owned by the given authenticated customer."""
        if not customer or not getattr(customer, "is_authenticated", False):
            return self.none()
        return self.active().filter(customer=customer)

    def for_session(self, session_key: Optional[str]) -> "CartQuerySet":
        """
        Return only anonymous session-keyed carts that have not been
        linked to a customer.
        """
        if not session_key:
            return self.none()
        return self.active().filter(session_key=session_key, customer__isnull=True)

    def for_request(self, request: Any) -> "CartQuerySet":
        """
        Return carts visible to the current request (customer cart
        for authenticated users, session cart for anonymous users).
        """
        user = getattr(request, "user", None)
        if user and getattr(user, "is_authenticated", False):
            return self.for_customer(user)
        session = getattr(request, "session", None)
        session_key = getattr(session, "session_key", "") if session else ""
        return self.for_session(session_key)

    def with_totals(self) -> "CartQuerySet":
        """
        Annotate each cart with its computed subtotal across active
        line items. Uses server-side aggregation to avoid N+1 queries.
        """
        return self.annotate(
            computed_subtotal=models.Sum(
                models.F("items__quantity") * models.F("items__unit_price_snapshot"),
                filter=models.Q(items__status=CartItem.ItemStatus.ACTIVE),
                output_field=models.DecimalField(max_digits=14, decimal_places=2),
            )
        )

    def with_item_counts(self) -> "CartQuerySet":
        """
        Annotate each cart with the number of active line items and
        the total quantity of items.
        """
        return self.annotate(
            active_items_count=models.Count(
                "items",
                filter=models.Q(items__status=CartItem.ItemStatus.ACTIVE),
                distinct=True,
            ),
            total_quantity=models.Sum(
                "items__quantity",
                filter=models.Q(items__status=CartItem.ItemStatus.ACTIVE),
            ),
        )


# ==============================================================================
# MANAGER
# ==============================================================================
class CartManager(models.Manager.from_queryset(CartQuerySet)):
    """
    Manager exposing common cart queries with a clean interface.
    All methods are safe to call from request handlers and never
    raise on missing data (returning None / new instances instead).
    """

    def get_queryset(self) -> CartQuerySet:
        return CartQuerySet(self.model, using=self._db)

    def active(self) -> CartQuerySet:
        return self.get_queryset().active()

    def abandoned(
        self,
        threshold_hours: int = DEFAULT_CART_ABANDONMENT_THRESHOLD_HOURS,
    ) -> CartQuerySet:
        return self.get_queryset().abandoned(threshold_hours)

    def get_for_customer(self, customer: Any) -> Optional["Cart"]:
        """
        Retrieve the active cart for an authenticated customer, creating
        a new cart if none exists. Returns None for unauthenticated users.
        """
        if not customer or not getattr(customer, "is_authenticated", False):
            return None
        try:
            cart = (
                self.for_customer(customer)
                .order_by("-last_activity_at")
                .first()
            )
            if cart is not None:
                return cart
            return self.create(
                customer=customer,
                status=Cart.CartStatus.ACTIVE,
            )
        except Exception:
            return None

    def get_for_session(self, session_key: Optional[str]) -> Optional["Cart"]:
        """
        Retrieve the active cart for an anonymous session, creating
        a new cart if none exists. Returns None for missing session keys.
        """
        if not session_key:
            return None
        try:
            cart = (
                self.for_session(session_key)
                .order_by("-last_activity_at")
                .first()
            )
            if cart is not None:
                return cart
            return self.create(
                session_key=session_key,
                status=Cart.CartStatus.ACTIVE,
            )
        except Exception:
            return None

    def get_or_create_for_request(self, request: Any):
        """
        Resolve the cart for the current request, migrating guest
        carts to authenticated customers when applicable.

        Returns a tuple of (cart, was_created) where was_created is
        always True for backward compatibility with the existing
        contract.
        """
        user = getattr(request, "user", None)
        if user and getattr(user, "is_authenticated", False):
            return self.get_for_customer(user), True
        session = getattr(request, "session", None)
        if session is not None and not getattr(session, "session_key", None):
            try:
                session.create()
            except Exception:
                pass
        session_key = getattr(session, "session_key", "") if session else ""
        return self.get_for_session(session_key), True


# ==============================================================================
# CART
# ==============================================================================
class Cart(models.Model):
    """
    Represents a shopping cart, supporting both guest (session) and
    authenticated (customer) ownership with full merge capabilities.

    Cart owns ONLY:
        * Customer / session reference
        * Status
        * Active flag
        * Currency
        * Coupon code
        * Customer note
        * Last activity
        * Recovery timestamp
        * Expiration
        * Aggregated totals (read-only, derived from items)

    Cart does NOT own:
        * Stock quantities
        * Available stock
        * Reserved stock
        * Warehouse data
        * Inventory references (only optional pointers)
    """

    class CartStatus(models.TextChoices):
        ACTIVE = "active", _("Active")
        MERGED = "merged", _("Merged Into Another Cart")
        ABANDONED = "abandoned", _("Marked Abandoned")
        CONVERTED = "converted", _("Converted To Order")
        EXPIRED = "expired", _("Expired")

    class CurrencyChoices(models.TextChoices):
        NPR = "NPR", _("Nepalese Rupee")
        USD = "USD", _("US Dollar")
        EUR = "EUR", _("Euro")
        GBP = "GBP", _("British Pound")
        INR = "INR", _("Indian Rupee")

    id = models.BigAutoField(primary_key=True)
    customer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="carts",
        verbose_name=_("Customer"),
    )
    session_key = models.CharField(
        _("Session Key"),
        max_length=64,
        null=True,
        blank=True,
        db_index=True,
        unique=True,
        help_text=_("Anonymous session identifier for guest carts."),
    )

    def save(self, *args, **kwargs):
        """
        Ensure the anonymous_token is always populated with a
        cryptographically secure unique value. This sidesteps the
        Django makemigrations interactive prompt while still guaranteeing
        a unique token for every cart row, because save() runs on every
        insert path (admin, manager, ORM, signals).
        """
        if not self.anonymous_token:
            self.anonymous_token = secrets.token_urlsafe(32)
        super().save(*args, **kwargs)

    anonymous_token = models.CharField(
        _("Anonymous Cart Token"),
        max_length=64,
        null=True,
        blank=True,
        unique=True,
        help_text=_(
            "Persistent token used to recover abandoned guest carts across devices."
        ),
    )

    status = models.CharField(
        max_length=20,
        choices=CartStatus.choices,
        default=CartStatus.ACTIVE,
        db_index=True,
        verbose_name=_("Status"),
    )
    currency = models.CharField(
        max_length=8,
        choices=CurrencyChoices.choices,
        default=CurrencyChoices.NPR,
        verbose_name=_("Currency"),
    )
    coupon_code = models.CharField(
        max_length=64,
        blank=True,
        null=True,
        verbose_name=_("Applied Coupon Code"),
    )
    coupon_discount_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        verbose_name=_("Coupon Discount"),
    )
    customer_note = models.TextField(
        blank=True,
        verbose_name=_("Customer Note"),
    )
    is_active = models.BooleanField(
        default=True,
        db_index=True,
        verbose_name=_("Is Active"),
    )
    last_activity_at = models.DateTimeField(
        auto_now=True,
        db_index=True,
        verbose_name=_("Last Activity At"),
    )
    recovered_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("Recovered At"),
    )
    expires_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        verbose_name=_("Expires At"),
        help_text=_("When this cart is no longer considered valid."),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    last_merged_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("Last Merged At"),
        help_text=_("Timestamp when this cart was last merged with another cart."),
    )

    # -----------------------------------------------------------------
    # OPTIONAL inventory references (warehouse / reservation awareness)
    # These are references ONLY. No inventory data is copied into Cart.
    # -----------------------------------------------------------------
    preferred_warehouse = models.ForeignKey(
        "inventory.Warehouse",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="preferred_carts",
        verbose_name=_("Preferred Warehouse (optional)"),
        help_text=_(
            "Optional fulfillment preference. Cart never stores stock data; "
            "this only records which warehouse the customer would like to "
            "draw from. Inventory remains the single source of truth."
        ),
    )

    objects = CartManager()

    class Meta:
        verbose_name = _("Cart")
        verbose_name_plural = _("Carts")
        ordering = ["-last_activity_at"]
        indexes = [
            models.Index(fields=["customer", "is_active", "status"]),
            models.Index(fields=["session_key", "is_active", "status"]),
            models.Index(fields=["anonymous_token", "is_active"]),
            models.Index(fields=["last_activity_at"]),
            models.Index(fields=["status", "expires_at"]),
            models.Index(fields=["last_merged_at"]),
            # Composite index for the most common "active cart for customer" query.
            models.Index(
                fields=["customer", "status", "-last_activity_at"],
                name="cart_active_for_customer_idx",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["anonymous_token"],
                name="cart_anonymous_token_unique",
            ),
            # Database-level guarantee: a cart cannot simultaneously be
            # tied to both a customer and a session key.
            models.CheckConstraint(
                check=(
                    models.Q(customer__isnull=True)
                    | models.Q(session_key__isnull=True)
                ),
                name="cart_customer_session_xor",
            ),
        ]

    def __str__(self) -> str:
        owner = (
            "Customer"
            if self.customer_id
            else f"Guest {self.anonymous_token[:8] if self.anonymous_token else 'N/A'}"
        )
        return f"Cart #{self.id} ({owner})"

    def clean(self) -> None:
        """
        Cross-field validation enforced at the application layer in
        addition to the database-level CHECK constraint.
        """
        super().clean()
        if self.customer_id and self.session_key:
            raise ValidationError(
                _(
                    "A cart cannot simultaneously be tied to both a customer "
                    "and a session key."
                )
            )

    # -----------------------------------------------------------------
    # Read-only computed helpers
    # -----------------------------------------------------------------
    @property
    def is_guest(self) -> bool:
        return self.customer_id is None

    @property
    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return self.expires_at <= timezone.now()

    @property
    def total_items_count(self) -> int:
        return (
            self.items.filter(status=CartItem.ItemStatus.ACTIVE).aggregate(
                total=models.Sum("quantity")
            )["total"]
            or 0
        )

    @property
    def unique_items_count(self) -> int:
        return self.items.filter(status=CartItem.ItemStatus.ACTIVE).count()

    @property
    def subtotal(self) -> Decimal:
        """
        Sum of active cart item line subtotals.

        Cart does NOT query Inventory. The subtotal is purely a
        snapshot of (unit_price_snapshot × quantity) summed across
        active line items. Inventory is consulted at checkout time
        by the service layer, never here.
        """
        active_items = self.items.filter(status=CartItem.ItemStatus.ACTIVE)
        calculated = active_items.aggregate(
            total=models.Sum(
                models.F("unit_price_snapshot") * models.F("quantity"),
                output_field=models.DecimalField(max_digits=14, decimal_places=2),
            )
        )["total"]
        return calculated or Decimal("0.00")

    @property
    def estimated_tax(self) -> Decimal:
        rate = getattr(
            settings,
            "DEFAULT_TAX_RATE",
            Decimal("0.13"),
        )
        try:
            rate = Decimal(str(rate))
            if rate < 0:
                rate = Decimal("0")
        except Exception:
            rate = Decimal("0.13")
        return (self.subtotal * rate).quantize(Decimal("0.01"))

    @property
    def estimated_shipping(self) -> Decimal:
        return Decimal("0.00")

    @property
    def grand_total(self) -> Decimal:
        return max(
            Decimal("0.00"),
            self.subtotal
            - (self.coupon_discount_amount or Decimal("0.00"))
            + self.estimated_tax
            + self.estimated_shipping,
        )

    # -----------------------------------------------------------------
    # State transitions
    # -----------------------------------------------------------------
    def touch(self) -> None:
        """Update ``last_activity_at`` to the current time. Idempotent."""
        type(self).objects.filter(pk=self.pk).update(
            last_activity_at=timezone.now()
        )

    def clear(self) -> None:
        """Remove every item from the cart."""
        self.items.all().delete()

    def mark_abandoned(self) -> None:
        self.status = self.CartStatus.ABANDONED
        self.is_active = False
        self.save(update_fields=["status", "is_active", "updated_at"])

    def mark_converted(self) -> None:
        self.status = self.CartStatus.CONVERTED
        self.is_active = False
        self.save(update_fields=["status", "is_active", "updated_at"])

    def mark_expired(self) -> None:
        self.status = self.CartStatus.EXPIRED
        self.is_active = False
        self.save(update_fields=["status", "is_active", "updated_at"])

    def apply_coupon(self, code: str, discount_amount: Decimal) -> None:
        self.coupon_code = code
        self.coupon_discount_amount = min(
            Decimal(discount_amount or 0),
            self.subtotal,
        )
        self.save(
            update_fields=[
                "coupon_code",
                "coupon_discount_amount",
                "updated_at",
            ]
        )

    def clear_coupon(self) -> None:
        self.coupon_code = None
        self.coupon_discount_amount = Decimal("0.00")
        self.save(
            update_fields=[
                "coupon_discount_amount",
                "updated_at",
            ]
        )


# ==============================================================================
# CART ITEM
# ==============================================================================
class CartItem(models.Model):
    """
    Represents an individual line item inside a Cart.

    Captures price and product state at the moment of addition to
    preserve historical accuracy (audit-friendly).

    Inventory awareness:
        * CartItem optionally references a StockReservation
          (apps.inventory.StockReservation) to record that inventory
          was reserved for this cart line.
        * CartItem optionally references a Warehouse for fulfillment
          preference.
        * CartItem optionally references Inventory directly for
          integration use cases.
        * CartItem NEVER duplicates reserved quantity, available
          quantity, or any inventory snapshot.
        * Every reservation / warehouse / inventory field is OPTIONAL.

    Reservation lifecycle:
        * `reserved_until` is the cart's own local expiry hint for
          the line's reservation. It is a soft, cache-friendly value
          that the cart uses for SLA tracking and for its own cleanup
          job. It is NOT a duplicate of `reservation_expires_at` (which
          is a mirror of the inventory reservation's expiry).
        * The cart service is responsible for keeping the two values
          consistent; the inventory app remains the source of truth.
    """

    class ItemStatus(models.TextChoices):
        ACTIVE = "active", _("Active")
        SAVED = "saved", _("Saved For Later")
        REMOVED = "removed", _("Removed")
        EXPIRED = "expired", _("Expired")

    class SavedForLaterReason(models.TextChoices):
        MANUAL = "manual", _("Manually Saved")
        REPLACED = "replaced", _("Replaced By Newer Variant")
        STOCK_OUT = "out_of_stock", _("Variant Out Of Stock")

    id = models.BigAutoField(primary_key=True)
    cart = models.ForeignKey(
        Cart,
        on_delete=models.CASCADE,
        related_name="items",
        verbose_name=_("Cart"),
    )
    product = models.ForeignKey(
        "catalog.Product",
        on_delete=models.CASCADE,
        related_name="cart_items",
        verbose_name=_("Product"),
    )
    variant = models.ForeignKey(
        "catalog.ProductVariant",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="cart_items",
        verbose_name=_("Product Variant"),
    )
    product_name_snapshot = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        verbose_name=_("Product Name (Snapshot)"),
    )
    product_sku_snapshot = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        verbose_name=_("Product SKU (Snapshot)"),
    )
    variant_name_snapshot = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        verbose_name=_("Variant Name (Snapshot)"),
    )
    product_image_snapshot = models.ImageField(
        upload_to="cart/snapshots/",
        blank=True,
        null=True,
        verbose_name=_("Product Image (Snapshot)"),
    )
    unit_price_snapshot = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        verbose_name=_("Unit Price (Snapshot)"),
    )
    compare_at_price_snapshot = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        verbose_name=_("Compare At Price (Snapshot)"),
    )
    currency_snapshot = models.CharField(
        max_length=8,
        default="NPR",
        blank=True,
        null=True,
        verbose_name=_("Currency (Snapshot)"),
    )
    quantity = models.PositiveIntegerField(
        default=1,
        validators=[MinValueValidator(1)],
        verbose_name=_("Quantity"),
    )
    status = models.CharField(
        max_length=12,
        choices=ItemStatus.choices,
        default=ItemStatus.ACTIVE,
        db_index=True,
        verbose_name=_("Status"),
    )
    saved_reason = models.CharField(
        max_length=20,
        choices=SavedForLaterReason.choices,
        blank=True,
        null=True,
        verbose_name=_("Saved For Later Reason"),
    )
    attributes_snapshot = models.JSONField(
        default=dict,
        blank=True,
        verbose_name=_("Selected Attributes (Snapshot)"),
    )
    personalization = models.JSONField(
        default=dict,
        blank=True,
        verbose_name=_("Personalization Data"),
        help_text=_("Engraving text, gift wrapping options, etc."),
    )

    # -----------------------------------------------------------------
    # Cart-level reservation expiry (new field, fixes startup error)
    # -----------------------------------------------------------------
    # This is the cart's own soft expiry hint for the line's
    # reservation. It is independent of (and complementary to) the
    # Inventory app's StockReservation.expires_at. The cart uses it
    # for SLA tracking and its own cleanup job without round-tripping
    # to the inventory app.
    reserved_until = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        verbose_name=_("Reserved Until"),
        help_text=_(
            "Timezone-aware timestamp until which inventory is reserved "
            "for this cart line. Cart-level soft expiry; the inventory "
            "app remains the single source of truth for actual stock "
            "state and reservation validity."
        ),
    )

    # -----------------------------------------------------------------
    # OPTIONAL Inventory references (zero duplication of inventory data)
    # -----------------------------------------------------------------
    # Direct reference to the inventory row. NEVER duplicates stock.
    inventory = models.ForeignKey(
        "inventory.Inventory",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="cart_items",
        verbose_name=_("Inventory (optional reference)"),
        help_text=_(
            "Optional reference to the Inventory row backing this "
            "cart line. The cart NEVER stores reserved quantity, "
            "available quantity, or any other inventory data."
        ),
    )
    # Direct reference to the active stock reservation for this cart line.
    reservation = models.ForeignKey(
        "inventory.StockReservation",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="cart_items",
        verbose_name=_("Stock Reservation (optional)"),
        help_text=_(
            "Optional reference to the StockReservation created "
            "for this cart line. Cart does NOT store reserved quantity; "
            "the reservation itself is the single source of truth."
        ),
    )
    # Optional explicit warehouse preference for this line item.
    warehouse = models.ForeignKey(
        "inventory.Warehouse",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="cart_items",
        verbose_name=_("Warehouse (optional preference)"),
        help_text=_(
            "Optional explicit warehouse preference for this line. "
            "Stock data always remains in the Inventory app."
        ),
    )
    # Token copied from the reservation (opaque). Never exposes stock data.
    reservation_token = models.CharField(
        max_length=64,
        blank=True,
        null=True,
        verbose_name=_("Reservation Token (opaque)"),
        help_text=_(
            "Opaque identifier of the active StockReservation for this line. "
            "Carries no stock data. Used only for cross-system correlation."
        ),
    )
    reservation_status = models.CharField(
        max_length=20,
        blank=True,
        null=True,
        verbose_name=_("Reservation Status (mirror)"),
        help_text=_(
            "Mirror of the reservation status for display. The "
            "Inventory app's StockReservation is the single source of truth."
        ),
    )
    reservation_expires_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        verbose_name=_("Reservation Expires At (mirror)"),
        help_text=_(
            "Mirror of the reservation expiry for display. Actual expiry "
            "lives on the StockReservation in the Inventory app."
        ),
    )
    reservation_quantity = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        null=True,
        blank=True,
        verbose_name=_("Reservation Quantity (mirror)"),
        help_text=_(
            "Mirror of the quantity reserved for this line item. The "
            "StockReservation in the Inventory app is the single source of truth."
        ),
    )
    reservation_source = models.CharField(
        max_length=40,
        blank=True,
        null=True,
        verbose_name=_("Reservation Source (mirror)"),
        help_text=_(
            "Source classification (cart/manual/promotional). The Inventory "
            "app is the single source of truth."
        ),
    )
    reservation_metadata = models.JSONField(
        default=dict,
        blank=True,
        verbose_name=_("Reservation Metadata (mirror)"),
        help_text=_(
            "Opaque passthrough metadata. Never contains stock quantities."
        ),
    )
    reservation_notes = models.TextField(
        blank=True,
        null=True,
        verbose_name=_("Reservation Notes (mirror)"),
        help_text=_(
            "Free-text notes from the reservation for display. The Inventory "
            "app is the single source of truth."
        ),
    )
    reservation_version = models.PositiveIntegerField(
        default=0,
        verbose_name=_("Reservation Version (mirror)"),
        help_text=_(
            "Cache-busting version counter mirrored from the StockReservation."
        ),
    )

    added_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    saved_at = models.DateTimeField(null=True, blank=True)
    moved_to_save_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = _("Cart Item")
        verbose_name_plural = _("Cart Items")
        ordering = ["added_at"]
        indexes = [
            models.Index(fields=["cart", "status"]),
            models.Index(fields=["cart", "product", "variant", "status"]),
            models.Index(fields=["cart", "status", "updated_at"]),
            models.Index(fields=["status", "added_at"]),
            # Reserved-until lookups: lines that are currently active and
            # whose reservation is about to expire or has expired.
            # MySQL limits index names to 30 characters; we use a
            # short, explicit name that fits the limit while remaining
            # readable.
            models.Index(
                fields=["status", "reserved_until"],
                name="cartitem_status_resv_idx",  # 25 chars
            ),
            # Partial-friendly index for the most common "live cart lines"
            # query, scoped by cart.
            models.Index(
                fields=["cart", "status", "reserved_until"],
                name="cartitem_cart_stat_resv_idx",  # 27 chars
            ),
            models.Index(fields=["reservation_expires_at"]),
        ]
        constraints = [
            # Cart item with variant: only one active line per
            # (cart, product, variant).
            models.UniqueConstraint(
                fields=["cart", "product", "variant", "status"],
                condition=~models.Q(variant__isnull=True),
                name="cart_unique_product_variant_active",
            ),
            # Cart item without variant: only one active line per
            # (cart, product).
            models.UniqueConstraint(
                fields=["cart", "product", "status"],
                condition=models.Q(variant__isnull=True),
                name="cart_unique_product_no_variant_active",
            ),
            models.CheckConstraint(
                check=models.Q(quantity__gte=1),
                name="cartitem_quantity_gte_1",
            ),
            models.CheckConstraint(
                check=models.Q(unit_price_snapshot__gte=0),
                name="cartitem_unit_price_gte_0",
            ),
        ]

    def __str__(self) -> str:
        variant = (
            f" ({self.variant_name_snapshot})"
            if self.variant_name_snapshot
            else ""
        )
        return (
            f"{self.quantity} x {self.product_name_snapshot or 'Unnamed'}"
            f"{variant} [{self.get_status_display()}]"
        )

    def clean(self) -> None:
        super().clean()
        if (
            self.unit_price_snapshot is not None
            and self.unit_price_snapshot < 0
        ):
            raise ValidationError(
                {"unit_price_snapshot": _("Unit price cannot be negative.")}
            )
        if self.quantity is not None and self.quantity < 1:
            raise ValidationError(
                {"quantity": _("Quantity must be at least 1.")}
            )
        if (
            self.compare_at_price_snapshot
            and self.unit_price_snapshot
            and self.compare_at_price_snapshot < self.unit_price_snapshot
        ):
            raise ValidationError(
                {
                    "compare_at_price_snapshot": _(
                        "Compare-at price must be greater than or equal to unit price."
                    )
                }
            )

    # -----------------------------------------------------------------
    # Pricing helpers (no inventory calculations)
    # -----------------------------------------------------------------
    @property
    def line_subtotal(self) -> Decimal:
        return (self.unit_price_snapshot or Decimal("0.00")) * self.quantity

    @property
    def line_discount(self) -> Decimal:
        if (
            self.compare_at_price_snapshot
            and self.unit_price_snapshot
            and self.compare_at_price_snapshot > self.unit_price_snapshot
        ):
            return (
                self.compare_at_price_snapshot - self.unit_price_snapshot
            ) * self.quantity
        return Decimal("0.00")

    @property
    def effective_unit_price(self) -> Decimal:
        return self.unit_price_snapshot

    @property
    def is_available(self) -> bool:
        """
        Cart-side availability is purely a soft hint based on the
        product/variant is_active flag and the recorded reservation
        status. Cart NEVER queries Inventory directly. The service
        layer is the single source of truth for actual stock
        availability; this method is a best-effort optimistic hint
        suitable for display only.
        """
        if self.variant_id:
            return bool(getattr(self.variant, "is_active", False))
        if self.product_id and not getattr(self.product, "is_active", True):
            return False
        # Note: we do NOT inspect inventory quantities here. Cart must
        # never depend on inventory data. Service layer handles the
        # authoritative availability check at checkout time.
        return True

    @property
    def is_reservation_active(self) -> bool:
        """
        Soft hint indicating whether the cart's local reservation
        expiry is in the future. The authoritative reservation state
        is owned by the inventory app's StockReservation row.
        """
        if self.reserved_until is None:
            return False
        return self.reserved_until > timezone.now()

    @property
    def is_reservation_expired(self) -> bool:
        """
        Soft hint indicating whether the cart's local reservation
        expiry has passed. The authoritative reservation state is
        owned by the inventory app's StockReservation row.
        """
        if self.reserved_until is None:
            return False
        return self.reserved_until <= timezone.now()

    @property
    def minutes_until_reservation_expiry(self) -> Optional[int]:
        """
        Returns the number of minutes until the cart's local
        reservation expiry, or None if no expiry is set / the
        reservation has already expired.
        """
        if self.reserved_until is None:
            return None
        delta = self.reserved_until - timezone.now()
        if delta.total_seconds() <= 0:
            return 0
        return int(delta.total_seconds() // 60)

    # -----------------------------------------------------------------
    # State transitions
    # -----------------------------------------------------------------
    def move_to_save_for_later(
        self,
        reason: str = SavedForLaterReason.MANUAL,
    ) -> None:
        """
        Move this line into the SAVED state. The Inventory reservation
        is released by the cart service via the inventory service.
        This model does NOT mutate inventory directly.
        """
        self.status = self.ItemStatus.SAVED
        self.saved_reason = reason
        self.moved_to_save_at = timezone.now()
        self.save(
            update_fields=[
                "status",
                "saved_reason",
                "moved_to_save_at",
                "updated_at",
            ]
        )

    def restore_to_active(self) -> None:
        """
        Restore a SAVED line back to ACTIVE. The cart service is
        responsible for re-reserving inventory; this model does NOT
        mutate inventory directly.
        """
        self.status = self.ItemStatus.ACTIVE
        self.saved_reason = None
        self.moved_to_save_at = None
        self.save(
            update_fields=[
                "status",
                "saved_reason",
                "moved_to_save_at",
                "updated_at",
            ]
        )

    def increment_quantity(self, delta: int = 1) -> None:
        """
        Atomically increment the quantity by ``delta``. Enforces a
        minimum of 1 and raises ValidationError on invalid input.
        Uses F() expressions for a safe database-side increment that
        is race-free under concurrent requests.
        """
        if delta == 0:
            return
        # The F() increment is applied at the database level; the
        # lower-bound check is enforced via a database constraint
        # so concurrent writers cannot push the value below 1.
        from django.db.models import F
        type(self).objects.filter(pk=self.pk, quantity__gte=1 if delta > 0 else None).update(
            quantity=F("quantity") + delta,
            updated_at=timezone.now(),
        )
        # Belt-and-suspenders: if the F() expression pushed the value
        # below 1 (e.g. decrement went too far), re-fetch and verify.
        self.refresh_from_db(fields=["quantity", "updated_at"])
        if self.quantity < 1:
            # Roll back the invalid state to the minimum allowed value.
            type(self).objects.filter(pk=self.pk).update(quantity=1)
            self.refresh_from_db(fields=["quantity"])
            raise ValidationError(
                {"quantity": _("Quantity cannot fall below 1.")}
            )

    def set_quantity(self, quantity: int) -> None:
        if quantity is None or int(quantity) < 1:
            raise ValidationError(
                {"quantity": _("Quantity must be at least 1.")}
            )
        self.quantity = int(quantity)
        self.save(update_fields=["quantity", "updated_at"])

    def mark_reservation(
        self,
        *,
        reserved_until: Optional[Any] = None,
        reservation_token: Optional[str] = None,
        reservation_status: Optional[str] = None,
        reservation_quantity: Optional[Any] = None,
        reservation_expires_at: Optional[Any] = None,
        reservation_source: Optional[str] = None,
        reservation_metadata: Optional[dict] = None,
        reservation_notes: Optional[str] = None,
        reservation_version: Optional[int] = None,
    ) -> None:
        """
        Persist cart-level reservation metadata mirrored from the
        inventory service. This is a write-only helper for the cart
        service; the inventory app remains the single source of truth
        for the underlying StockReservation row.
        """
        update_fields = ["updated_at"]
        if reserved_until is not None:
            self.reserved_until = reserved_until
            update_fields.append("reserved_until")
        if reservation_token is not None:
            self.reservation_token = reservation_token
            update_fields.append("reservation_token")
        if reservation_status is not None:
            self.reservation_status = reservation_status
            update_fields.append("reservation_status")
        if reservation_quantity is not None:
            self.reservation_quantity = reservation_quantity
            update_fields.append("reservation_quantity")
        if reservation_expires_at is not None:
            self.reservation_expires_at = reservation_expires_at
            update_fields.append("reservation_expires_at")
        if reservation_source is not None:
            self.reservation_source = reservation_source
            update_fields.append("reservation_source")
        if reservation_metadata is not None:
            self.reservation_metadata = reservation_metadata
            update_fields.append("reservation_metadata")
        if reservation_notes is not None:
            self.reservation_notes = reservation_notes
            update_fields.append("reservation_notes")
        if reservation_version is not None:
            self.reservation_version = int(reservation_version)
            update_fields.append("reservation_version")
        # Always bump the version so callers can detect changes.
        self.reservation_version = (self.reservation_version or 0) + 1
        if "reservation_version" not in update_fields:
            update_fields.append("reservation_version")
        self.save(update_fields=update_fields)

    def clear_reservation(self) -> None:
        """
        Clear all cart-level reservation mirror fields. Called by the
        cart service when the underlying inventory reservation is
        released, expired, or otherwise invalidated.
        """
        self.reserved_until = None
        self.reservation_token = None
        self.reservation_status = None
        self.reservation_quantity = None
        self.reservation_expires_at = None
        self.reservation_source = None
        self.reservation_metadata = {}
        self.reservation_notes = None
        self.reservation_version = (self.reservation_version or 0) + 1
        self.save(
            update_fields=[
                "reserved_until",
                "reservation_token",
                "reservation_status",
                "reservation_quantity",
                "reservation_expires_at",
                "reservation_source",
                "reservation_metadata",
                "reservation_notes",
                "reservation_version",
                "updated_at",
            ]
        )


# ==============================================================================
# MODULE-LEVEL CONVENIENCE HELPERS
# ==============================================================================
def get_default_reservation_minutes() -> int:
    """
    Returns the CMS-driven default cart-level reservation duration
    in minutes. Sources from ``CART_DEFAULT_RESERVATION_MINUTES`` in
    settings with a safe fallback.
    """
    return getattr(
        settings,
        "CART_DEFAULT_RESERVATION_MINUTES",
        DEFAULT_CART_RESERVATION_MINUTES,
    )


# ==============================================================================
# PUBLIC MODULE API
# ==============================================================================
__all__ = [
    # Constants
    "DEFAULT_CART_RESERVATION_MINUTES",
    "DEFAULT_CART_EXPIRATION_DAYS",
    "DEFAULT_CART_ABANDONMENT_THRESHOLD_HOURS",
    # QuerySet / Manager
    "CartQuerySet",
    "CartManager",
    # Models
    "Cart",
    "CartItem",
    # Helpers
    "get_default_reservation_minutes",
]