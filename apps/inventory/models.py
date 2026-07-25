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
      originate from an InventoryTransaction record, ensuring a complete
      audit trail for compliance, analytics, and financial integration.
    * Every field is optional where technically possible to support gradual
      CMS-driven configuration.
    * Foreign keys use PROTECT to prevent accidental data loss.
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
# MODULE-LEVEL CONSTANTS FOR DATABASE CONSTRAINTS
# ==============================================================================
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
# VALIDATORS
# ==============================================================================
_phone_validator = RegexValidator(
    regex=r"^\+?[0-9\s\-\(\)]{7,20}$",
    message=_(
        "Phone number must be 7-20 characters and may only contain digits, "
        "spaces, hyphens, parentheses, and an optional leading +."
    ),
)

# ==============================================================================
# 1. WAREHOUSE MODEL
# ==============================================================================
class Warehouse(CMSBaseModel):
    """
    Physical or virtual warehouse / fulfillment center.
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
        if self.code:
            self.code = self.code.strip().upper()
        self.clean()
        super().save(*args, **kwargs)

    @property
    def display_name(self) -> str:
        """Returns the most user-friendly display name."""
        if self.name and self.code:
            return f"{self.name} ({self.code})"
        return self.name or self.code or f"Warehouse #{self.pk}"

# ==============================================================================
# 2. INVENTORY MODEL
# ==============================================================================
class Inventory(CMSBaseModel):
    """
    Single source of truth for stock levels per Product or ProductVariant
    per Warehouse.
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
        help_text=_("Stock currently held in active reservations."),
    )
    damaged_quantity = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        verbose_name=_("Damaged Quantity"),
        help_text=_("Stock removed from sale due to damage."),
    )
    incoming_quantity = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        verbose_name=_("Incoming Quantity"),
        help_text=_("Stock expected from suppliers in transit."),
    )

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
        help_text=_("Replenishment threshold."),
    )

    location_bin = models.CharField(
        max_length=120,
        blank=True,
        null=True,
        verbose_name=_("Location Bin"),
        help_text=_("Physical rack/shelf/bin code inside warehouse."),
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
            models.UniqueConstraint(
                fields=["warehouse", "product_variant"],
                condition=models.Q(product_variant__isnull=False),
                name="inventory_unique_variant_per_warehouse",
            ),
            models.UniqueConstraint(
                fields=["warehouse", "product"],
                condition=models.Q(product_variant__isnull=True, product__isnull=False),
                name="inventory_unique_product_no_variant_per_warehouse",
            ),
            models.CheckConstraint(
                check=(
                    (models.Q(product_variant__isnull=True) & models.Q(product__isnull=False))
                    | (models.Q(product_variant__isnull=False) & models.Q(product__isnull=True))
                ),
                name="inventory_must_have_exactly_one_target",
            ),
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
            models.CheckConstraint(
                check=models.Q(reserved_quantity__lte=models.F("available_quantity")),
                name="inventory_reserved_lte_available",
            ),
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

    @property
    def total_stock(self) -> Decimal:
        """Total physical stock including damaged units."""
        return self.available_quantity + self.damaged_quantity

    @property
    def free_stock(self) -> Decimal:
        """Stock available for new sales (available minus reserved)."""
        return max(Decimal("0.00"), self.available_quantity - self.reserved_quantity)

    @property
    def usable_stock(self) -> Decimal:
        """Alias for free_stock."""
        return self.free_stock

    @property
    def needs_reorder(self) -> bool:
        """True if available stock is at or below reorder level."""
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
        """Returns the target ProductVariant or Product."""
        return self.product_variant or self.product

# ==============================================================================
# 3. INVENTORY TRANSACTION MODEL
# ==============================================================================
class InventoryTransaction(CMSBaseModel):
    """
    Immutable audit ledger recording every stock movement.
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
    )
    quantity = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        verbose_name=_("Quantity"),
    )

    available_before = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        blank=True,
        null=True,
        validators=[MinValueValidator(Decimal("0.00"))],
        verbose_name=_("Available Quantity Before"),
    )
    available_after = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        blank=True,
        null=True,
        validators=[MinValueValidator(Decimal("0.00"))],
        verbose_name=_("Available Quantity After"),
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

    unit_cost = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        blank=True,
        null=True,
        validators=[MinValueValidator(Decimal("0.00"))],
        verbose_name=_("Unit Cost"),
    )
    total_cost = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        blank=True,
        null=True,
        validators=[MinValueValidator(Decimal("0.00"))],
        verbose_name=_("Total Cost"),
    )
    currency = models.CharField(
        max_length=10,
        blank=True,
        null=True,
        default="NPR",
        verbose_name=_("Currency"),
    )

    reference_number = models.CharField(
        max_length=120,
        blank=True,
        null=True,
        db_index=True,
        verbose_name=_("Reference Number"),
    )
    reference_model = models.CharField(
        max_length=80,
        blank=True,
        null=True,
        verbose_name=_("Reference Model"),
    )
    reference_id = models.CharField(
        max_length=80,
        blank=True,
        null=True,
        verbose_name=_("Reference ID"),
    )

    destination_warehouse = models.ForeignKey(
        Warehouse,
        on_delete=models.PROTECT,
        related_name="incoming_transfers",
        blank=True,
        null=True,
        verbose_name=_("Destination Warehouse"),
    )
    transfer_group_id = models.UUIDField(
        default=uuid.uuid4,
        editable=False,
        db_index=True,
        verbose_name=_("Transfer Group ID"),
    )

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
            models.CheckConstraint(
                check=models.Q(quantity__gte=0),
                name="invtx_quantity_gte_0",
            ),
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
        if self.transaction_type == self.TransactionType.TRANSFER:
            if not self.destination_warehouse_id:
                raise ValidationError(
                    {"destination_warehouse": _("Transfer transactions require a destination warehouse.")}
                )
            if self.destination_warehouse_id == self.inventory.warehouse_id:
                raise ValidationError(
                    {"destination_warehouse": _("Destination warehouse must differ from source warehouse.")}
                )
        elif self.destination_warehouse_id:
            raise ValidationError(
                {"destination_warehouse": _("Destination warehouse is only valid for TRANSFER transactions.")}
            )

    def save(self, *args: Any, **kwargs: Any) -> None:
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
        if self.direction == self.FlowDirection.INBOUND:
            return self.quantity
        if self.direction == self.FlowDirection.OUTBOUND:
            return -self.quantity
        return Decimal("0.00")

# ==============================================================================
# 4. STOCK RESERVATION MODEL
# ==============================================================================
class StockReservation(CMSBaseModel):
    """
    Temporary stock hold created during cart operations or manual holds.
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

    reservation_token = models.UUIDField(
        default=uuid.uuid4,
        editable=False,
        unique=True,
        db_index=True,
        verbose_name=_("Reservation Token"),
    )

    cart = models.ForeignKey(
        "cart.Cart",
        on_delete=models.CASCADE,
        related_name="stock_reservations",
        blank=True,
        null=True,
        verbose_name=_("Cart"),
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
    )

    inventory = models.ForeignKey(
        Inventory,
        on_delete=models.PROTECT,
        related_name="reservations",
        blank=True,
        null=True,
        verbose_name=_("Inventory"),
    )
    warehouse = models.ForeignKey(
        Warehouse,
        on_delete=models.PROTECT,
        related_name="stock_reservations",
        blank=True,
        null=True,
        verbose_name=_("Warehouse"),
    )

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
    )

    expires_at = models.DateTimeField(
        blank=True,
        null=True,
        db_index=True,
        verbose_name=_("Expires At"),
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

    session_key = models.CharField(
        max_length=64,
        blank=True,
        null=True,
        db_index=True,
        verbose_name=_("Session Key"),
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="stock_reservations",
        blank=True,
        null=True,
        verbose_name=_("User"),
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
            models.CheckConstraint(
                check=(
                    (models.Q(product_variant__isnull=True) & models.Q(product__isnull=False))
                    | (models.Q(product_variant__isnull=False) & models.Q(product__isnull=True))
                ),
                name="reservation_must_have_exactly_one_target",
            ),
            models.CheckConstraint(
                check=models.Q(quantity__gt=0),
                name="reservation_quantity_gt_0",
            ),
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
                _("StockReservation must reference exactly one of: product_variant or product.")
            )
        if not self.inventory_id and not self.warehouse_id:
            raise ValidationError(
                _("StockReservation must reference either an Inventory row or a Warehouse.")
            )

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.is_active = self.status == self.ReservationStatus.ACTIVE
        self.clean()
        super().save(*args, **kwargs)

    @property
    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return self.expires_at <= timezone.now() and self.status == self.ReservationStatus.ACTIVE

    @property
    def is_terminal(self) -> bool:
        return self.status in [
            self.ReservationStatus.CONVERTED,
            self.ReservationStatus.RELEASED,
            self.ReservationStatus.EXPIRED,
            self.ReservationStatus.CANCELLED,
        ]

    @property
    def is_orphan(self) -> bool:
        return not (self.cart_id or self.user_id or self.session_key)

    @property
    def age_minutes(self) -> int:
        delta = timezone.now() - self.created_at
        return int(delta.total_seconds() // 60)

    @property
    def minutes_until_expiry(self) -> Optional[int]:
        if self.expires_at is None:
            return None
        delta = self.expires_at - timezone.now()
        return max(0, int(delta.total_seconds() // 60))

    def get_target(self) -> Any:
        return self.product_variant or self.product

# ==============================================================================
# 5. STOCK ADJUSTMENT MODEL
# ==============================================================================
class StockAdjustment(CMSBaseModel):
    """
    Manual stock correction request with multi-step approval workflow.
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

    adjustment_number = models.CharField(
        max_length=50,
        unique=True,
        blank=True,
        null=True,
        db_index=True,
        verbose_name=_("Adjustment Number"),
    )

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
    )
    supporting_documents = models.JSONField(
        default=list,
        blank=True,
        verbose_name=_("Supporting Documents"),
    )

    old_quantity = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        blank=True,
        null=True,
        validators=[MinValueValidator(Decimal("0.00"))],
        verbose_name=_("Old Quantity (Available)"),
    )
    new_quantity = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        blank=True,
        null=True,
        validators=[MinValueValidator(Decimal("0.00"))],
        verbose_name=_("New Quantity (Available)"),
    )
    difference = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        blank=True,
        null=True,
        verbose_name=_("Difference"),
    )

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
    )

    applied_transaction = models.ForeignKey(
        InventoryTransaction,
        on_delete=models.SET_NULL,
        related_name="originating_adjustment",
        blank=True,
        null=True,
        verbose_name=_("Applied Transaction"),
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
            models.CheckConstraint(
                check=(
                    ~models.Q(status=ADJUSTMENT_STATUS_APPLIED)
                    | models.Q(applied_at__isnull=False)
                ),
                name="stockadj_applied_requires_timestamp",
            ),
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
        if not self.adjustment_number:
            ts = timezone.now().strftime("%y%m%d")
            self.adjustment_number = f"ADJ-{ts}-{secrets.token_hex(3).upper()}"
        if self.old_quantity is not None and self.new_quantity is not None:
            self.difference = (self.new_quantity - self.old_quantity).quantize(Decimal("0.01"))
        super().save(*args, **kwargs)

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
        if self.is_positive:
            return _("Increase")
        if self.is_negative:
            return _("Decrease")
        return _("No Change")

__all__ = [
    "RESERVATION_STATUS_ACTIVE",
    "RESERVATION_STATUS_CONVERTED",
    "RESERVATION_STATUS_RELEASED",
    "RESERVATION_STATUS_EXPIRED",
    "RESERVATION_STATUS_CANCELLED",
    "ADJUSTMENT_STATUS_DRAFT",
    "ADJUSTMENT_STATUS_PENDING_APPROVAL",
    "ADJUSTMENT_STATUS_APPROVED",
    "ADJUSTMENT_STATUS_REJECTED",
    "ADJUSTMENT_STATUS_APPLIED",
    "ADJUSTMENT_STATUS_CANCELLED",
    "INVENTORY_FLOW_INBOUND",
    "INVENTORY_FLOW_OUTBOUND",
    "INVENTORY_FLOW_NEUTRAL",
    "Warehouse",
    "Inventory",
    "InventoryTransaction",
    "StockReservation",
    "StockAdjustment",
]