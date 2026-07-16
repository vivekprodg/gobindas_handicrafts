"""
Enterprise-grade inventory management models for the handicraft e-commerce ERP.

Provides:
    * Multi-warehouse stock management (Warehouse)
    * Single source of truth for current stock (Inventory)
    * Immutable audit ledger of every stock movement (InventoryTransaction)
    * Temporary cart-level stock holds (StockReservation)
    * Manual stock correction with approval workflow (StockAdjustment)

Core design principles:
    * Stock is NEVER mutated directly on Inventory rows. Every change must
      originate from a new InventoryTransaction record, ensuring a complete
      audit trail for compliance, analytics, and financial integration.
    * Every field is optional (blank=True / null=True) where technically
      possible to support gradual CMS-driven configuration.
    * All foreign keys to critical entities (Warehouse, Product, ProductVariant)
      use PROTECT to prevent accidental data loss; soft deletion is managed
      via is_active flags.
    * Database-level constraints (CheckConstraint, UniqueConstraint) enforce
      business rules independent of application code.
"""

from __future__ import annotations

import secrets
import uuid
from decimal import Decimal
from typing import Any, Optional

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, RegexValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.foundation.models import CMSBaseModel

# ==============================================================================
# MODULE-LEVEL CONSTANTS
# ==============================================================================
# These constants mirror the inner TextChoices classes below.
# They are declared at module level so they can be safely referenced inside
# CheckConstraint Q objects (which are lazy-evaluated and would otherwise
# trigger static analyzer / Pylance "undefined variable" warnings when
# referencing nested class attributes defined later in the same class body).

RESERVATION_STATUS_ACTIVE = "active"
RESERVATION_STATUS_CONVERTED = "converted"
RESERVATION_STATUS_RELEASED = "released"
RESERVATION_STATUS_EXPIRED = "expired"
RESERVATION_STATUS_CANCELLED = "cancelled"

ADJUSTMENT_STATUS_DRAFT = "draft"
ADJUSTMENT_STATUS_PENDING_APPROVAL = "pending_approval"
ADJUSTMENT_STATUS_APPROVED = "approved"
ADJUSTMENT_STATUS_REJECTED = "rejected"
ADJUSTMENT_STATUS_APPLIED = "applied"
ADJUSTMENT_STATUS_CANCELLED = "cancelled"

INVENTORY_FLOW_INBOUND = "inbound"
INVENTORY_FLOW_OUTBOUND = "outbound"
INVENTORY_FLOW_NEUTRAL = "neutral"

# ==============================================================================
# MODULE-LEVEL VALIDATORS
# ==============================================================================
_phone_validator = RegexValidator(
    regex=r"^\+?[0-9\s\-\(\)]{7,20}$",
    message=_(
        "Phone number must be 7-20 characters and may only contain digits, "
        "spaces, hyphens, parentheses, and an optional leading +."
    ),
)

# ==============================================================================
# 1. WAREHOUSE
# ==============================================================================
class Warehouse(CMSBaseModel):
    """
    Physical or virtual warehouse / fulfillment center.

    Supports:
        * Multi-warehouse architectures
        * Default warehouse designation (exactly one active default)
        * Soft deactivation via is_active (preserves historical transactions)
        * Full contact metadata per location
    """

    name = models.CharField(
        max_length=150,
        blank=True,
        null=True,
        verbose_name=_("Warehouse Name"),
        help_text=_("Display name of the warehouse / fulfillment center."),
    )
    code = models.CharField(
        max_length=50,
        blank=True,
        null=True,
        unique=True,
        verbose_name=_("Warehouse Code"),
        help_text=_(
            "Unique short code used in references and reports (e.g. 'KTM-01'). "
            "Auto-uppercased on save."
        ),
    )
    location = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        verbose_name=_("Location"),
        help_text=_("Free-form location description (city, district, etc.)."),
    )
    phone = models.CharField(
        max_length=20,
        blank=True,
        null=True,
        validators=[_phone_validator],
        verbose_name=_("Phone"),
    )
    email = models.EmailField(
        max_length=254,
        blank=True,
        null=True,
        verbose_name=_("Email"),
    )
    is_default = models.BooleanField(
        default=False,
        blank=True,
        null=True,
        db_index=True,
        verbose_name=_("Is Default Warehouse"),
        help_text=_(
            "Designates this warehouse as the primary automatic fulfillment source. "
            "Only one active default warehouse is supported at a time."
        ),
    )
    is_active = models.BooleanField(
        default=True,
        blank=True,
        null=True,
        db_index=True,
        verbose_name=_("Is Active"),
        help_text=_("Soft deactivation flag. Inactive warehouses are hidden from selection UIs."),
    )

    class Meta:
        verbose_name = _("Warehouse")
        verbose_name_plural = _("Warehouses")
        ordering = ["-is_default", "name", "id"]
        db_table = "inventory_warehouse"
        indexes = [
            models.Index(fields=["is_active"]),
            models.Index(fields=["is_default"]),
            models.Index(fields=["is_active", "is_default"]),
        ]
        constraints = [
            # A default warehouse must be active.
            models.CheckConstraint(
                check=~models.Q(is_default=True) | models.Q(is_active=True),
                name="warehouse_default_must_be_active",
            ),
        ]

    def __str__(self) -> str:
        return self.display_name

    def clean(self) -> None:
        super().clean()
        if self.is_default and not self.is_active:
            raise ValidationError(
                {"is_default": _("A default warehouse cannot be inactive.")}
            )

    def save(self, *args: Any, **kwargs: Any) -> None:
        # Normalize code to uppercase for consistency
        if self.code:
            self.code = self.code.strip().upper()
        # Trigger validation hooks
        self.clean()
        super().save(*args, **kwargs)

    @property
    def display_name(self) -> str:
        """Returns the most user-friendly name available."""
        if self.name and self.code:
            return f"{self.name} ({self.code})"
        return self.name or self.code or f"Warehouse #{self.pk}"

# ==============================================================================
# 2. INVENTORY
# ==============================================================================
class Inventory(CMSBaseModel):
    """
    Stock level for a single (ProductVariant + Warehouse) or
    (Product + Warehouse) combination.

    Also supports product-level (variant-less) stock when variant is NULL.

    Stock quantities are NEVER mutated directly. They are always changed
    by creating an InventoryTransaction record. This provides a complete
    audit trail for compliance and analytics.

    Designed to scale to millions of records with deep indexing and
    minimal N+1 queries.
    """

    product_variant = models.ForeignKey(
        "catalog.ProductVariant",
        on_delete=models.PROTECT,
        related_name="inventory_records",
        blank=True,
        null=True,
        verbose_name=_("Product Variant"),
        help_text=_("Variant for which stock is tracked. NULL for product-level stock."),
    )
    product = models.ForeignKey(
        "catalog.Product",
        on_delete=models.PROTECT,
        related_name="inventory_records",
        blank=True,
        null=True,
        verbose_name=_("Product"),
        help_text=_("Product for which stock is tracked (when no variant is selected)."),
    )
    warehouse = models.ForeignKey(
        Warehouse,
        on_delete=models.PROTECT,
        related_name="inventory_records",
        verbose_name=_("Warehouse"),
    )

    # --------------------------------------------------------------
    # Stock Quantities
    # --------------------------------------------------------------
    available_quantity = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        verbose_name=_("Available Quantity"),
        help_text=_("Current sellable stock. Never negative."),
    )
    reserved_quantity = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        verbose_name=_("Reserved Quantity"),
        help_text=_("Stock currently held in active reservations (carts, holds, etc.)."),
    )
    damaged_quantity = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        verbose_name=_("Damaged Quantity"),
        help_text=_("Stock removed from sale due to damage. Tracked separately for accounting."),
    )
    incoming_quantity = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        verbose_name=_("Incoming Quantity"),
        help_text=_("Stock expected from suppliers / POs in transit."),
    )

    # --------------------------------------------------------------
    # Reorder Thresholds
    # --------------------------------------------------------------
    minimum_stock = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        blank=True,
        null=True,
        validators=[MinValueValidator(Decimal("0.00"))],
        verbose_name=_("Minimum Stock Level"),
        help_text=_("Low-stock alert threshold."),
    )
    maximum_stock = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        blank=True,
        null=True,
        validators=[MinValueValidator(Decimal("0.00"))],
        verbose_name=_("Maximum Stock Level"),
        help_text=_("Storage capacity ceiling for this warehouse."),
    )
    reorder_level = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        blank=True,
        null=True,
        validators=[MinValueValidator(Decimal("0.00"))],
        verbose_name=_("Reorder Level"),
        help_text=_("When available stock falls to or below this level, replenishment should be initiated."),
    )

    # --------------------------------------------------------------
    # Metadata
    # --------------------------------------------------------------
    location_bin = models.CharField(
        max_length=120,
        blank=True,
        null=True,
        verbose_name=_("Location Bin"),
        help_text=_("Physical location inside the warehouse (rack/shelf/bin code)."),
    )
    notes = models.TextField(
        blank=True,
        null=True,
        verbose_name=_("Notes"),
    )
    is_active = models.BooleanField(
        default=True,
        blank=True,
        null=True,
        db_index=True,
        verbose_name=_("Is Active"),
    )

    class Meta:
        verbose_name = _("Inventory")
        verbose_name_plural = _("Inventory Records")
        ordering = ["warehouse", "product_variant", "product", "id"]
        db_table = "inventory_inventory"
        indexes = [
            models.Index(fields=["warehouse", "product_variant"]),
            models.Index(fields=["warehouse", "product"]),
            models.Index(fields=["is_active"]),
            models.Index(fields=["product_variant", "is_active"]),
            models.Index(fields=["product", "is_active"]),
        ]
        constraints = [
            # Unique (warehouse, product_variant) when variant is set
            models.UniqueConstraint(
                fields=["warehouse", "product_variant"],
                condition=models.Q(product_variant__isnull=False),
                name="inventory_unique_variant_per_warehouse",
            ),
            # Unique (warehouse, product) when no variant is set
            models.UniqueConstraint(
                fields=["warehouse", "product"],
                condition=models.Q(product_variant__isnull=True, product__isnull=False),
                name="inventory_unique_product_no_variant_per_warehouse",
            ),
            # Must reference exactly one target
            models.CheckConstraint(
                check=(
                    (models.Q(product_variant__isnull=True) & models.Q(product__isnull=False))
                    | (models.Q(product_variant__isnull=False) & models.Q(product__isnull=True))
                ),
                name="inventory_must_have_exactly_one_target",
            ),
            # All stock quantities are non-negative
            models.CheckConstraint(
                check=models.Q(available_quantity__gte=0),
                name="inventory_available_gte_zero",
            ),
            models.CheckConstraint(
                check=models.Q(reserved_quantity__gte=0),
                name="inventory_reserved_gte_zero",
            ),
            models.CheckConstraint(
                check=models.Q(damaged_quantity__gte=0),
                name="inventory_damaged_gte_zero",
            ),
            models.CheckConstraint(
                check=models.Q(incoming_quantity__gte=0),
                name="inventory_incoming_gte_zero",
            ),
            # Reserved cannot exceed available (sanity check)
            models.CheckConstraint(
                check=models.Q(reserved_quantity__lte=models.F("available_quantity")),
                name="inventory_reserved_lte_available",
            ),
            # Min <= Max when both are set
            models.CheckConstraint(
                check=(
                    models.Q(minimum_stock__isnull=True)
                    | models.Q(maximum_stock__isnull=True)
                    | models.Q(minimum_stock__lte=models.F("maximum_stock"))
                ),
                name="inventory_min_lte_max",
            ),
        ]

    def __str__(self) -> str:
        target = self.product_variant or self.product
        return f"{self.warehouse.display_name} / {target}"

    def clean(self) -> None:
        super().clean()
        if bool(self.product_variant) == bool(self.product):
            raise ValidationError(
                _(
                    "Inventory must reference exactly one of: product_variant or "
                    "product (not both, not neither)."
                )
            )

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.clean()
        super().save(*args, **kwargs)

    # ==========================================================
    # Computed Properties
    # ==========================================================
    @property
    def total_stock(self) -> Decimal:
        """Total physical stock, including damaged units."""
        return self.available_quantity + self.damaged_quantity

    @property
    def free_stock(self) -> Decimal:
        """Stock available for new sales (available minus reserved)."""
        return max(Decimal("0.00"), self.available_quantity - self.reserved_quantity)

    @property
    def usable_stock(self) -> Decimal:
        """Alias for free_stock, named for cart/inventory-check semantics."""
        return self.free_stock

    @property
    def needs_reorder(self) -> bool:
        """True if stock has dropped to or below the reorder level."""
        if self.reorder_level is None:
            return False
        return self.available_quantity <= self.reorder_level

    @property
    def is_out_of_stock(self) -> bool:
        return self.free_stock <= Decimal("0.00")

    @property
    def is_low_stock(self) -> bool:
        if self.minimum_stock is None:
            return False
        return (
            self.available_quantity <= self.minimum_stock
            and not self.is_out_of_stock
        )

    @property
    def is_overstock(self) -> bool:
        if self.maximum_stock is None:
            return False
        return self.available_quantity > self.maximum_stock

    def get_target(self) -> Any:
        """Returns the related Product or ProductVariant for this record."""
        return self.product_variant or self.product

# ==============================================================================
# 3. INVENTORY TRANSACTION
# ==============================================================================
class InventoryTransaction(CMSBaseModel):
    """
    Immutable record of every stock movement.

    Stock is NEVER changed directly on Inventory. It is always modified
    by creating a new InventoryTransaction record. This provides a complete
    audit trail for compliance, analytics, and financial integration.

    Designed to support millions of transactions with deep indexing and
    efficient reporting.
    """

    class TransactionType(models.TextChoices):
        PURCHASE = "purchase", _("Purchase / Receiving")
        SALE = "sale", _("Sale / Fulfillment")
        RETURN = "return", _("Customer Return")
        CANCEL = "cancel", _("Order Cancellation")
        TRANSFER = "transfer", _("Warehouse Transfer")
        DAMAGE = "damage", _("Damaged / Write-off")
        ADJUSTMENT = "adjustment", _("Manual Adjustment")
        OPENING = "opening", _("Opening Balance")
        RECOUNT = "recount", _("Physical Recount")
        RESERVATION_RELEASE = "reservation_release", _("Reservation Release")

    class FlowDirection(models.TextChoices):
        """Indicates whether the transaction increases or decreases stock."""
        INBOUND = INVENTORY_FLOW_INBOUND, _("Inbound (+)")
        OUTBOUND = INVENTORY_FLOW_OUTBOUND, _("Outbound (−)")
        NEUTRAL = INVENTORY_FLOW_NEUTRAL, _("Neutral (no quantity change)")

    inventory = models.ForeignKey(
        Inventory,
        on_delete=models.PROTECT,
        related_name="transactions",
        verbose_name=_("Inventory Record"),
    )
    transaction_type = models.CharField(
        max_length=24,
        choices=TransactionType.choices,
        db_index=True,
        verbose_name=_("Transaction Type"),
    )
    direction = models.CharField(
        max_length=16,
        choices=FlowDirection.choices,
        default=FlowDirection.NEUTRAL,
        db_index=True,
        verbose_name=_("Flow Direction"),
        help_text=_(
            "Whether the transaction increases (+) or decreases (-) available stock. "
            "Derived from transaction_type at the service layer."
        ),
    )

    quantity = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        verbose_name=_("Quantity"),
        help_text=_("Quantity moved. Direction is recorded separately in 'direction'."),
    )

    # --------------------------------------------------------------
    # Immutable Stock Snapshots
    # --------------------------------------------------------------
    available_before = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        blank=True,
        null=True,
        validators=[MinValueValidator(Decimal("0.00"))],
        verbose_name=_("Available Quantity Before"),
        help_text=_("Snapshot of available_quantity immediately before this transaction."),
    )
    available_after = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        blank=True,
        null=True,
        validators=[MinValueValidator(Decimal("0.00"))],
        verbose_name=_("Available Quantity After"),
        help_text=_("Snapshot of available_quantity immediately after this transaction."),
    )
    reserved_before = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        blank=True,
        null=True,
        validators=[MinValueValidator(Decimal("0.00"))],
        verbose_name=_("Reserved Quantity Before"),
    )
    reserved_after = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        blank=True,
        null=True,
        validators=[MinValueValidator(Decimal("0.00"))],
        verbose_name=_("Reserved Quantity After"),
    )

    # --------------------------------------------------------------
    # Cost Tracking (for future financial integration)
    # --------------------------------------------------------------
    unit_cost = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        blank=True,
        null=True,
        validators=[MinValueValidator(Decimal("0.00"))],
        verbose_name=_("Unit Cost"),
        help_text=_("Per-unit cost at the time of this transaction. Used for COGS calculations."),
    )
    total_cost = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        blank=True,
        null=True,
        validators=[MinValueValidator(Decimal("0.00"))],
        verbose_name=_("Total Cost"),
        help_text=_("Auto-computed: quantity × unit_cost. Stored for fast reporting."),
    )
    currency = models.CharField(
        max_length=10,
        blank=True,
        null=True,
        default="NPR",
        verbose_name=_("Currency"),
        help_text=_("ISO 4217 currency code for the unit_cost and total_cost."),
    )

    # --------------------------------------------------------------
    # Cross-Module Traceability
    # --------------------------------------------------------------
    reference_number = models.CharField(
        max_length=120,
        blank=True,
        null=True,
        db_index=True,
        verbose_name=_("Reference Number"),
        help_text=_("External reference number (PO, GRN, Order #, Return RMA, etc.)."),
    )
    reference_model = models.CharField(
        max_length=80,
        blank=True,
        null=True,
        verbose_name=_("Reference Model"),
        help_text=_(
            "App label and model name of the referenced record "
            "(e.g. 'orders.Order', 'purchases.PurchaseOrder')."
        ),
    )
    reference_id = models.CharField(
        max_length=80,
        blank=True,
        null=True,
        verbose_name=_("Reference ID"),
        help_text=_("String representation of the referenced record's primary key."),
    )

    # --------------------------------------------------------------
    # Transfer-Specific Fields
    # --------------------------------------------------------------
    destination_warehouse = models.ForeignKey(
        Warehouse,
        on_delete=models.PROTECT,
        related_name="incoming_transfers",
        blank=True,
        null=True,
        verbose_name=_("Destination Warehouse"),
        help_text=_("For TRANSFER transactions: the warehouse receiving the stock."),
    )
    transfer_group_id = models.UUIDField(
        default=uuid.uuid4,
        editable=False,
        db_index=True,
        verbose_name=_("Transfer Group ID"),
        help_text=_(
            "Pairs the outbound and inbound transactions of a single transfer. "
            "Auto-generated on first save."
        ),
    )

    # --------------------------------------------------------------
    # Audit Fields
    # --------------------------------------------------------------
    remarks = models.TextField(
        blank=True,
        null=True,
        verbose_name=_("Remarks"),
    )
    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="inventory_transactions",
        blank=True,
        null=True,
        verbose_name=_("Performed By"),
    )
    transaction_at = models.DateTimeField(
        default=timezone.now,
        db_index=True,
        verbose_name=_("Transaction Timestamp"),
        help_text=_("When the stock movement physically occurred. Defaults to record creation time."),
    )

    class Meta:
        verbose_name = _("Inventory Transaction")
        verbose_name_plural = _("Inventory Transactions")
        ordering = ["-transaction_at", "-id"]
        db_table = "inventory_transaction"
        indexes = [
            models.Index(fields=["inventory", "-transaction_at"]),
            models.Index(fields=["transaction_type", "-transaction_at"]),
            models.Index(fields=["direction", "-transaction_at"]),
            models.Index(fields=["reference_number"]),
            models.Index(fields=["reference_model", "reference_id"]),
            models.Index(fields=["transaction_at"]),
            models.Index(fields=["-created_at"]),
            models.Index(fields=["transfer_group_id"]),
            models.Index(fields=["performed_by", "-transaction_at"]),
        ]
        constraints = [
            # Quantity is non-negative (sign is encoded in 'direction')
            models.CheckConstraint(
                check=models.Q(quantity__gte=0),
                name="invtx_quantity_gte_0",
            ),
            # Total cost is non-negative
            models.CheckConstraint(
                check=models.Q(total_cost__isnull=True) | models.Q(total_cost__gte=0),
                name="invtx_total_cost_gte_0",
            ),
        ]

    def __str__(self) -> str:
        sign = (
            "+" if self.direction == self.FlowDirection.INBOUND
            else ("-" if self.direction == self.FlowDirection.OUTBOUND else "±")
        )
        ts = self.transaction_at.strftime("%Y-%m-%d %H:%M")
        return f"{ts} {sign}{self.quantity} {self.get_transaction_type_display()}"

    def clean(self) -> None:
        super().clean()
        # Transfer transactions must specify a destination warehouse
        if self.transaction_type == self.TransactionType.TRANSFER:
            if not self.destination_warehouse_id:
                raise ValidationError(
                    {
                        "destination_warehouse": _(
                            "Transfer transactions require a destination warehouse."
                        )
                    }
                )
            if self.destination_warehouse_id == self.inventory.warehouse_id:
                raise ValidationError(
                    {
                        "destination_warehouse": _(
                            "Destination warehouse must differ from the source warehouse."
                        )
                    }
                )
        else:
            if self.destination_warehouse_id:
                raise ValidationError(
                    {
                        "destination_warehouse": _(
                            "Destination warehouse is only valid for TRANSFER transactions."
                        )
                    }
                )

    def save(self, *args: Any, **kwargs: Any) -> None:
        # Auto-compute total_cost if possible
        if self.unit_cost is not None and self.quantity is not None:
            self.total_cost = (self.unit_cost * self.quantity).quantize(Decimal("0.01"))
        self.clean()
        super().save(*args, **kwargs)

    @property
    def is_inbound(self) -> bool:
        return self.direction == self.FlowDirection.INBOUND

    @property
    def is_outbound(self) -> bool:
        return self.direction == self.FlowDirection.OUTBOUND

    @property
    def is_neutral(self) -> bool:
        return self.direction == self.FlowDirection.NEUTRAL

    @property
    def signed_quantity(self) -> Decimal:
        """
        Returns quantity with sign based on direction
        (inbound +, outbound -, neutral 0).
        """
        if self.direction == self.FlowDirection.INBOUND:
            return self.quantity
        if self.direction == self.FlowDirection.OUTBOUND:
            return -self.quantity
        return Decimal("0.00")

# ==============================================================================
# 4. STOCK RESERVATION
# ==============================================================================
class StockReservation(CMSBaseModel):
    """
    Temporary stock hold typically created when a customer adds an item
    to a cart. Reservations are released on:
        * Order placement (converted to a SALE transaction)
        * Reservation expiration (cron-driven cleanup)
        * Explicit release by the user or system

    Reservations DO NOT directly reduce available stock. They increment
    the reserved_quantity on the Inventory record. Free stock =
    available_quantity - reserved_quantity.

    Designed to support:
        * Multiple carts and anonymous (session-key) carts
        * Expiry-based cleanup
        * Future workflows (manual holds, promotional holds, backorders)
    """
    class ReservationType(models.TextChoices):
        CART = "cart", _("Cart")
        MANUAL_HOLD = "manual_hold", _("Manual Hold")
        PROMOTIONAL = "promotional", _("Promotional Reservation")
        BACKORDER = "backorder", _("Backorder")
        OTHER = "other", _("Other")

    class ReservationStatus(models.TextChoices):
        ACTIVE = RESERVATION_STATUS_ACTIVE, _("Active")
        CONVERTED = RESERVATION_STATUS_CONVERTED, _("Converted to Order")
        RELEASED = RESERVATION_STATUS_RELEASED, _("Released")
        EXPIRED = RESERVATION_STATUS_EXPIRED, _("Expired")
        CANCELLED = RESERVATION_STATUS_CANCELLED, _("Cancelled")

    # --------------------------------------------------------------
    # Identity
    # --------------------------------------------------------------
    reservation_token = models.UUIDField(
        default=uuid.uuid4,
        editable=False,
        unique=True,
        db_index=True,
        verbose_name=_("Reservation Token"),
        help_text=_("Unique opaque identifier for external / API references."),
    )

    # --------------------------------------------------------------
    # Source (one of: cart, or session/user + product)
    # --------------------------------------------------------------
    cart = models.ForeignKey(
        "cart.Cart",
        on_delete=models.CASCADE,
        related_name="stock_reservations",
        blank=True,
        null=True,
        verbose_name=_("Cart"),
        help_text=_("Source cart. May be NULL for manual holds or anonymous API holds."),
    )
    product_variant = models.ForeignKey(
        "catalog.ProductVariant",
        on_delete=models.PROTECT,
        related_name="stock_reservations",
        blank=True,
        null=True,
        verbose_name=_("Product Variant"),
    )
    product = models.ForeignKey(
        "catalog.Product",
        on_delete=models.PROTECT,
        related_name="stock_reservations",
        blank=True,
        null=True,
        verbose_name=_("Product"),
        help_text=_("Used when no variant is selected."),
    )

    # --------------------------------------------------------------
    # Inventory Binding
    # --------------------------------------------------------------
    inventory = models.ForeignKey(
        Inventory,
        on_delete=models.PROTECT,
        related_name="reservations",
        blank=True,
        null=True,
        verbose_name=_("Inventory"),
        help_text=_(
            "Specific inventory row being reserved. Resolved automatically by the "
            "service layer if left blank."
        ),
    )
    warehouse = models.ForeignKey(
        Warehouse,
        on_delete=models.PROTECT,
        related_name="stock_reservations",
        blank=True,
        null=True,
        verbose_name=_("Warehouse"),
        help_text=_("Warehouse where the stock is reserved. Required if no inventory row is provided."),
    )

    # --------------------------------------------------------------
    # Reservation Details
    # --------------------------------------------------------------
    quantity = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("1.00"),
        validators=[MinValueValidator(Decimal("0.01"))],
        verbose_name=_("Quantity"),
    )
    reservation_type = models.CharField(
        max_length=24,
        choices=ReservationType.choices,
        default=ReservationType.CART,
        db_index=True,
        verbose_name=_("Reservation Type"),
    )
    status = models.CharField(
        max_length=20,
        choices=ReservationStatus.choices,
        default=ReservationStatus.ACTIVE,
        db_index=True,
        verbose_name=_("Status"),
    )
    is_active = models.BooleanField(
        default=True,
        blank=True,
        null=True,
        db_index=True,
        verbose_name=_("Is Active"),
        help_text=_("Quick boolean for the cron cleanup filter. Mirrors status == ACTIVE."),
    )

    # --------------------------------------------------------------
    # Lifecycle
    # --------------------------------------------------------------
    expires_at = models.DateTimeField(
        blank=True,
        null=True,
        db_index=True,
        verbose_name=_("Expires At"),
        help_text=_("When this reservation automatically becomes eligible for cleanup."),
    )
    released_at = models.DateTimeField(
        blank=True,
        null=True,
        verbose_name=_("Released At"),
    )
    converted_at = models.DateTimeField(
        blank=True,
        null=True,
        verbose_name=_("Converted At"),
    )
    converted_to_order = models.ForeignKey(
        "orders.Order",
        on_delete=models.SET_NULL,
        related_name="originating_reservations",
        blank=True,
        null=True,
        verbose_name=_("Converted To Order"),
    )

    # --------------------------------------------------------------
    # Anonymous / Authenticated Source
    # --------------------------------------------------------------
    session_key = models.CharField(
        max_length=64,
        blank=True,
        null=True,
        db_index=True,
        verbose_name=_("Session Key"),
        help_text=_("For anonymous carts - the session key owning this reservation."),
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="stock_reservations",
        blank=True,
        null=True,
        verbose_name=_("User"),
        help_text=_("Authenticated user owning this reservation. NULL for anonymous carts."),
    )
    notes = models.TextField(
        blank=True,
        null=True,
        verbose_name=_("Notes"),
    )

    class Meta:
        verbose_name = _("Stock Reservation")
        verbose_name_plural = _("Stock Reservations")
        ordering = ["-created_at"]
        db_table = "inventory_stock_reservation"
        indexes = [
            models.Index(fields=["status", "expires_at"]),
            models.Index(fields=["cart", "status"]),
            models.Index(fields=["session_key", "status"]),
            models.Index(fields=["user", "status"]),
            models.Index(fields=["inventory", "status"]),
            models.Index(fields=["product_variant", "status"]),
            models.Index(fields=["product", "status"]),
            models.Index(fields=["is_active", "expires_at"]),
        ]
        constraints = [
            # Must reference exactly one target
            models.CheckConstraint(
                check=(
                    (models.Q(product_variant__isnull=True) & models.Q(product__isnull=False))
                    | (models.Q(product_variant__isnull=False) & models.Q(product__isnull=True))
                ),
                name="reservation_must_have_exactly_one_target",
            ),
            # Quantity must be positive
            models.CheckConstraint(
                check=models.Q(quantity__gt=0),
                name="reservation_quantity_gt_0",
            ),
            # Active reservations must be ACTIVE in status
            models.CheckConstraint(
                check=models.Q(is_active=False) | models.Q(status=RESERVATION_STATUS_ACTIVE),
                name="reservation_active_status_match",
            ),
        ]

    def __str__(self) -> str:
        target = self.product_variant or self.product
        return (
            f"Reservation {self.reservation_token.hex[:8]}: "
            f"{self.quantity} x {target} ({self.get_status_display()})"
        )

    def clean(self) -> None:
        super().clean()
        if bool(self.product_variant) == bool(self.product):
            raise ValidationError(
                _(
                    "StockReservation must reference exactly one of: "
                    "product_variant or product."
                )
            )
        if not self.inventory_id and not self.warehouse_id:
            raise ValidationError(
                _(
                    "StockReservation must reference either an Inventory row or "
                    "a Warehouse."
                )
            )

    def save(self, *args: Any, **kwargs: Any) -> None:
        # Keep is_active synchronized with status for fast filtering
        self.is_active = self.status == self.ReservationStatus.ACTIVE
        self.clean()
        super().save(*args, **kwargs)

    @property
    def is_expired(self) -> bool:
        """True if expires_at has passed and the reservation is still active."""
        if self.expires_at is None:
            return False
        return self.expires_at <= timezone.now() and self.status == self.ReservationStatus.ACTIVE

    @property
    def is_terminal(self) -> bool:
        """True if the reservation has reached a final state."""
        return self.status in [
            self.ReservationStatus.CONVERTED,
            self.ReservationStatus.RELEASED,
            self.ReservationStatus.EXPIRED,
            self.ReservationStatus.CANCELLED,
        ]

    @property
    def is_orphan(self) -> bool:
        """True if reservation has no cart, user, or session key (cleanup candidate)."""
        return not (self.cart_id or self.user_id or self.session_key)

    @property
    def age_minutes(self) -> int:
        """Minutes since this reservation was created."""
        delta = timezone.now() - self.created_at
        return int(delta.total_seconds() // 60)

    @property
    def minutes_until_expiry(self) -> Optional[int]:
        """Minutes until expiry, or None if no expiry is set / already expired."""
        if self.expires_at is None:
            return None
        delta = self.expires_at - timezone.now()
        return max(0, int(delta.total_seconds() // 60))

    def get_target(self) -> Any:
        """Returns the related Product or ProductVariant for this reservation."""
        return self.product_variant or self.product

# ==============================================================================
# 5. STOCK ADJUSTMENT
# ==============================================================================
class StockAdjustment(CMSBaseModel):
    """
    Manual stock correction with an approval workflow.

    Used for cycle counts, damage write-offs, found stock, lost stock,
    supplier corrections, and system reconciliations.

    The difference is automatically computed from old_quantity and new_quantity.
    The approval workflow is managed via the status field and approved_by FK.
    When the adjustment is approved and applied, an InventoryTransaction is
    created (and reverse-linked via applied_transaction) to update stock.

    The model is designed to be the single source of truth for ALL manual
    stock corrections. Periodic counts and audit reconciliations should also
    flow through this model.
    """

    class AdjustmentReason(models.TextChoices):
        CYCLE_COUNT = "cycle_count", _("Cycle Count")
        DAMAGE_WRITEOFF = "damage_writeoff", _("Damage Write-off")
        FOUND_STOCK = "found_stock", _("Found Stock")
        LOST_STOCK = "lost_stock", _("Lost Stock")
        SUPPLIER_CORRECTION = "supplier_correction", _("Supplier Correction")
        SYSTEM_RECONCILIATION = "system_reconciliation", _("System Reconciliation")
        OTHER = "other", _("Other")

    class AdjustmentStatus(models.TextChoices):
        DRAFT = ADJUSTMENT_STATUS_DRAFT, _("Draft")
        PENDING_APPROVAL = ADJUSTMENT_STATUS_PENDING_APPROVAL, _("Pending Approval")
        APPROVED = ADJUSTMENT_STATUS_APPROVED, _("Approved")
        REJECTED = ADJUSTMENT_STATUS_REJECTED, _("Rejected")
        APPLIED = ADJUSTMENT_STATUS_APPLIED, _("Applied")
        CANCELLED = ADJUSTMENT_STATUS_CANCELLED, _("Cancelled")

    # --------------------------------------------------------------
    # Identity
    # --------------------------------------------------------------
    adjustment_number = models.CharField(
        max_length=50,
        unique=True,
        blank=True,
        null=True,
        db_index=True,
        verbose_name=_("Adjustment Number"),
        help_text=_(
            "Auto-generated audit reference. Format: ADJ-YYMMDD-XXXX. "
            "Generated automatically on first save."
        ),
    )

    # --------------------------------------------------------------
    # Target & Reason
    # --------------------------------------------------------------
    inventory = models.ForeignKey(
        Inventory,
        on_delete=models.PROTECT,
        related_name="adjustments",
        verbose_name=_("Inventory Record"),
    )
    reason = models.CharField(
        max_length=32,
        choices=AdjustmentReason.choices,
        default=AdjustmentReason.OTHER,
        db_index=True,
        verbose_name=_("Reason"),
    )
    description = models.TextField(
        blank=True,
        null=True,
        verbose_name=_("Description"),
        help_text=_("Detailed description of the adjustment context (counted, found, lost, etc.)."),
    )
    supporting_documents = models.JSONField(
        default=list,
        blank=True,
        verbose_name=_("Supporting Documents"),
        help_text=_(
            "JSON list of supporting document references (URLs, photos, PDFs, etc.). "
            "Example: [{\"url\": \"...\", \"type\": \"photo\"}]"
        ),
    )

    # --------------------------------------------------------------
    # Quantities (Difference is auto-computed)
    # --------------------------------------------------------------
    old_quantity = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        blank=True,
        null=True,
        validators=[MinValueValidator(Decimal("0.00"))],
        verbose_name=_("Old Quantity (Available)"),
        help_text=_("Snapshot of available_quantity at the time the adjustment was drafted."),
    )
    new_quantity = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        blank=True,
        null=True,
        validators=[MinValueValidator(Decimal("0.00"))],
        verbose_name=_("New Quantity (Available)"),
        help_text=_("Desired new value of available_quantity after this adjustment is applied."),
    )
    difference = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        blank=True,
        null=True,
        verbose_name=_("Difference"),
        help_text=_("Auto-computed: new_quantity - old_quantity. May be negative."),
    )

    # --------------------------------------------------------------
    # Workflow State
    # --------------------------------------------------------------
    status = models.CharField(
        max_length=24,
        choices=AdjustmentStatus.choices,
        default=AdjustmentStatus.DRAFT,
        db_index=True,
        verbose_name=_("Status"),
    )
    initiated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="initiated_stock_adjustments",
        blank=True,
        null=True,
        verbose_name=_("Initiated By"),
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="approved_stock_adjustments",
        blank=True,
        null=True,
        verbose_name=_("Approved By"),
    )
    approved_at = models.DateTimeField(
        blank=True,
        null=True,
        verbose_name=_("Approved At"),
    )
    rejected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="rejected_stock_adjustments",
        blank=True,
        null=True,
        verbose_name=_("Rejected By"),
    )
    rejected_at = models.DateTimeField(
        blank=True,
        null=True,
        verbose_name=_("Rejected At"),
    )
    rejection_reason = models.TextField(
        blank=True,
        null=True,
        verbose_name=_("Rejection Reason"),
    )
    applied_at = models.DateTimeField(
        blank=True,
        null=True,
        verbose_name=_("Applied At"),
        help_text=_("When the adjustment was committed to inventory (and the InventoryTransaction was created)."),
    )

    # --------------------------------------------------------------
    # Reverse Link
    # --------------------------------------------------------------
    applied_transaction = models.ForeignKey(
        InventoryTransaction,
        on_delete=models.SET_NULL,
        related_name="originating_adjustment",
        blank=True,
        null=True,
        verbose_name=_("Applied Transaction"),
        help_text=_("The InventoryTransaction that was created when this adjustment was applied."),
    )

    class Meta:
        verbose_name = _("Stock Adjustment")
        verbose_name_plural = _("Stock Adjustments")
        ordering = ["-created_at"]
        db_table = "inventory_stock_adjustment"
        indexes = [
            models.Index(fields=["inventory", "-created_at"]),
            models.Index(fields=["status", "-created_at"]),
            models.Index(fields=["reason", "-created_at"]),
            models.Index(fields=["approved_by", "-approved_at"]),
            models.Index(fields=["initiated_by", "-created_at"]),
        ]
        constraints = [
            # Approved adjustments must have an approver
            models.CheckConstraint(
                check=(
                    models.Q(
                        status__in=[
                            ADJUSTMENT_STATUS_DRAFT,
                            ADJUSTMENT_STATUS_PENDING_APPROVAL,
                            ADJUSTMENT_STATUS_REJECTED,
                            ADJUSTMENT_STATUS_CANCELLED,
                        ]
                    )
                    | models.Q(approved_by__isnull=False)
                ),
                name="stockadj_approved_requires_approver",
            ),
            # Applied adjustments must have an applied_at timestamp
            models.CheckConstraint(
                check=(
                    ~models.Q(status=ADJUSTMENT_STATUS_APPLIED)
                    | models.Q(applied_at__isnull=False)
                ),
                name="stockadj_applied_requires_timestamp",
            ),
            # Rejected adjustments must have rejection metadata
            models.CheckConstraint(
                check=(
                    ~models.Q(status=ADJUSTMENT_STATUS_REJECTED)
                    | (models.Q(rejected_by__isnull=False) & models.Q(rejected_at__isnull=False))
                ),
                name="stockadj_rejected_requires_metadata",
            ),
        ]

    def __str__(self) -> str:
        adj_num = self.adjustment_number or f"#{self.pk}"
        return f"Stock Adjustment {adj_num}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        # Auto-generate adjustment_number on first save
        if not self.adjustment_number:
            ts = timezone.now().strftime("%y%m%d")
            self.adjustment_number = f"ADJ-{ts}-{secrets.token_hex(3).upper()}"
        # Auto-compute difference if both quantities are set
        if self.old_quantity is not None and self.new_quantity is not None:
            self.difference = (self.new_quantity - self.old_quantity).quantize(Decimal("0.01"))
        super().save(*args, **kwargs)

    # ==========================================================
    # Computed Properties
    # ==========================================================
    @property
    def is_positive(self) -> bool:
        return (self.difference or Decimal("0.00")) > Decimal("0.00")

    @property
    def is_negative(self) -> bool:
        return (self.difference or Decimal("0.00")) < Decimal("0.00")

    @property
    def is_neutral(self) -> bool:
        return (self.difference or Decimal("0.00")) == Decimal("0.00")

    @property
    def is_pending(self) -> bool:
        return self.status == self.AdjustmentStatus.PENDING_APPROVAL

    @property
    def is_approved(self) -> bool:
        return self.status == self.AdjustmentStatus.APPROVED

    @property
    def is_applied(self) -> bool:
        return self.status == self.AdjustmentStatus.APPLIED

    @property
    def is_terminal(self) -> bool:
        return self.status in [
            self.AdjustmentStatus.APPLIED,
            self.AdjustmentStatus.REJECTED,
            self.AdjustmentStatus.CANCELLED,
        ]

    @property
    def is_draft(self) -> bool:
        return self.status == self.AdjustmentStatus.DRAFT

    @property
    def is_rejected(self) -> bool:
        return self.status == self.AdjustmentStatus.REJECTED

    @property
    def is_cancelled(self) -> bool:
        return self.status == self.AdjustmentStatus.CANCELLED

    @property
    def direction_label(self) -> str:
        """Human-readable direction indicator for UI display."""
        if self.is_positive:
            return _("Increase")
        if self.is_negative:
            return _("Decrease")
        return _("No Change")