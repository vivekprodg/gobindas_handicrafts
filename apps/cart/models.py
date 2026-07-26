"""
Enterprise-grade cart data model for the handicraft e-commerce platform.
Supports anonymous (session-key) and authenticated (customer) carts,
with full merge capabilities, save-for-later, and inventory reservation.
"""

from __future__ import annotations

import secrets
from decimal import Decimal
from typing import Any, Optional

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

DEFAULT_CART_RESERVATION_MINUTES: int = 30
DEFAULT_CART_EXPIRATION_DAYS: int = 30
DEFAULT_CART_ABANDONMENT_THRESHOLD_HOURS: int = 24

class CartQuerySet(models.QuerySet["Cart"]):
    def active(self) -> CartQuerySet:
        return self.filter(is_active=True, status=Cart.CartStatus.ACTIVE)

    def abandoned(self, threshold_hours: int = DEFAULT_CART_ABANDONMENT_THRESHOLD_HOURS) -> CartQuerySet:
        cutoff = timezone.now() - timezone.timedelta(hours=threshold_hours)
        return self.active().filter(last_activity_at__lt=cutoff)

    def for_customer(self, customer: Any) -> CartQuerySet:
        if not customer or not getattr(customer, "is_authenticated", False):
            return self.none()
        return self.active().filter(customer=customer)

    def for_session(self, session_key: Optional[str]) -> CartQuerySet:
        if not session_key:
            return self.none()
        return self.active().filter(session_key=session_key, customer__isnull=True)

    def for_request(self, request: Any) -> CartQuerySet:
        user = getattr(request, "user", None)
        if user and getattr(user, "is_authenticated", False):
            return self.for_customer(user)
        session = getattr(request, "session", None)
        session_key = getattr(session, "session_key", "") if session else ""
        return self.for_session(session_key)

    def with_totals(self) -> CartQuerySet:
        return self.annotate(
            computed_subtotal=models.Sum(
                models.F("items__quantity") * models.F("items__unit_price_snapshot"),
                filter=models.Q(items__status=CartItem.ItemStatus.ACTIVE),
                output_field=models.DecimalField(max_digits=14, decimal_places=2),
            )
        )

    def with_item_counts(self) -> CartQuerySet:
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

class CartManager(models.Manager.from_queryset(CartQuerySet)):
    def get_queryset(self) -> CartQuerySet:
        return CartQuerySet(self.model, using=self._db)

    def active(self) -> CartQuerySet:
        return self.get_queryset().active()

    def abandoned(
        self, threshold_hours: int = DEFAULT_CART_ABANDONMENT_THRESHOLD_HOURS
    ) -> CartQuerySet:
        return self.get_queryset().abandoned(threshold_hours)

    def get_for_customer(self, customer: Any) -> Optional[Cart]:
        if not customer or not getattr(customer, "is_authenticated", False):
            return None
        try:
            cart = self.for_customer(customer).order_by("-last_activity_at").first()
            if cart is not None:
                return cart
            return self.create(customer=customer, status=Cart.CartStatus.ACTIVE)
        except Exception:
            return None

    def get_for_session(self, session_key: Optional[str]) -> Optional[Cart]:
        if not session_key:
            return None
        try:
            cart = self.for_session(session_key).order_by("-last_activity_at").first()
            if cart is not None:
                return cart
            return self.create(session_key=session_key, status=Cart.CartStatus.ACTIVE)
        except Exception:
            return None

    def get_or_create_for_request(self, request: Any) -> tuple[Optional[Cart], bool]:
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

class Cart(models.Model):
    class CartStatus(models.TextChoices):
        ACTIVE = "active", _("Active")
        MERGED = "merged", _("Merged Into Another Cart")
        ABANDONED = "abandoned", _("Marked Abandoned")
        CONVERTED = "converted", _("Converted To Order")
        EXPIRED = "expired", _("Expired")

    class CurrencyChoices(models.TextChoices):
        USD = "USD", _("US Dollar")
        NPR = "NPR", _("Nepalese Rupee")
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
        max_length=64,
        null=True,
        blank=True,
        db_index=True,
        unique=True,
        verbose_name=_("Session Key"),
        help_text=_("Anonymous session identifier for guest carts."),
    )
    anonymous_token = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        unique=True,
        verbose_name=_("Anonymous Cart Token"),
        help_text=_("Persistent token used to recover abandoned guest carts across devices."),
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
        default=CurrencyChoices.USD,
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
    preferred_warehouse = models.ForeignKey(
        "inventory.Warehouse",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="preferred_carts",
        verbose_name=_("Preferred Warehouse (optional)"),
        help_text=_("Optional fulfillment preference. Inventory remains the single source of truth."),
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
            models.CheckConstraint(
                check=(
                    models.Q(customer__isnull=True)
                    | models.Q(session_key__isnull=True)
                ),
                name="cart_customer_session_xor",
            ),
        ]

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self.anonymous_token:
            self.anonymous_token = secrets.token_urlsafe(32)
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        owner = (
            "Customer"
            if self.customer_id
            else f"Guest {self.anonymous_token[:8] if self.anonymous_token else 'N/A'}"
        )
        return f"Cart #{self.id} ({owner})"

    def clean(self) -> None:
        super().clean()
        if self.customer_id and self.session_key:
            raise ValidationError(
                _("A cart cannot simultaneously be tied to both a customer and a session key.")
            )

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
        rate = getattr(settings, "DEFAULT_TAX_RATE", Decimal("0.00"))
        try:
            rate = Decimal(str(rate))
            if rate < 0:
                rate = Decimal("0")
        except Exception:
            rate = Decimal("0.00")
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

    def touch(self) -> None:
        type(self).objects.filter(pk=self.pk).update(last_activity_at=timezone.now())

    def clear(self) -> None:
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
        self.coupon_discount_amount = min(Decimal(discount_amount or 0), self.subtotal)
        self.save(update_fields=["coupon_code", "coupon_discount_amount", "updated_at"])

    def clear_coupon(self) -> None:
        self.coupon_code = None
        self.coupon_discount_amount = Decimal("0.00")
        self.save(update_fields=["coupon_code", "coupon_discount_amount", "updated_at"])

class CartItem(models.Model):
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
        max_length=255, blank=True, null=True, verbose_name=_("Product Name (Snapshot)")
    )
    product_sku_snapshot = models.CharField(
        max_length=100, blank=True, null=True, verbose_name=_("Product SKU (Snapshot)")
    )
    variant_name_snapshot = models.CharField(
        max_length=255, blank=True, null=True, verbose_name=_("Variant Name (Snapshot)")
    )
    product_image_snapshot = models.ImageField(
        upload_to="cart/snapshots/", blank=True, null=True, verbose_name=_("Product Image (Snapshot)")
    )
    unit_price_snapshot = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00"), verbose_name=_("Unit Price (Snapshot)")
    )
    compare_at_price_snapshot = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True, verbose_name=_("Compare At Price (Snapshot)")
    )
    currency_snapshot = models.CharField(
        max_length=8, default="USD", blank=True, null=True, verbose_name=_("Currency (Snapshot)")
    )
    quantity = models.PositiveIntegerField(
        default=1, validators=[MinValueValidator(1)], verbose_name=_("Quantity")
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
        default=dict, blank=True, verbose_name=_("Selected Attributes (Snapshot)")
    )
    personalization = models.JSONField(
        default=dict, blank=True, verbose_name=_("Personalization Data")
    )
    reserved_until = models.DateTimeField(
        null=True, blank=True, db_index=True, verbose_name=_("Reserved Until")
    )
    inventory = models.ForeignKey(
        "inventory.Inventory",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="cart_items",
        verbose_name=_("Inventory (optional reference)"),
    )
    reservation = models.ForeignKey(
        "inventory.StockReservation",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="cart_items",
        verbose_name=_("Stock Reservation (optional)"),
    )
    warehouse = models.ForeignKey(
        "inventory.Warehouse",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="cart_items",
        verbose_name=_("Warehouse (optional preference)"),
    )
    reservation_token = models.CharField(
        max_length=64, blank=True, null=True, verbose_name=_("Reservation Token (opaque)")
    )
    reservation_status = models.CharField(
        max_length=20, blank=True, null=True, verbose_name=_("Reservation Status (mirror)")
    )
    reservation_expires_at = models.DateTimeField(
        null=True, blank=True, db_index=True, verbose_name=_("Reservation Expires At (mirror)")
    )
    reservation_quantity = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True, verbose_name=_("Reservation Quantity (mirror)")
    )
    reservation_source = models.CharField(
        max_length=40, blank=True, null=True, verbose_name=_("Reservation Source (mirror)")
    )
    reservation_metadata = models.JSONField(
        default=dict, blank=True, verbose_name=_("Reservation Metadata (mirror)")
    )
    reservation_notes = models.TextField(
        blank=True, null=True, verbose_name=_("Reservation Notes (mirror)")
    )
    reservation_version = models.PositiveIntegerField(
        default=0, verbose_name=_("Reservation Version (mirror)")
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
            models.Index(fields=["status", "reserved_until"], name="cartitem_status_resv_idx"),
            models.Index(fields=["cart", "status", "reserved_until"], name="cartitem_cart_stat_resv_idx"),
            models.Index(fields=["reservation_expires_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["cart", "product", "variant", "status"],
                condition=~models.Q(variant__isnull=True),
                name="cart_unique_product_variant_active",
            ),
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
        variant = f" ({self.variant_name_snapshot})" if self.variant_name_snapshot else ""
        return f"{self.quantity} x {self.product_name_snapshot or 'Unnamed'}{variant} [{self.get_status_display()}]"

    def clean(self) -> None:
        super().clean()
        if self.unit_price_snapshot is not None and self.unit_price_snapshot < 0:
            raise ValidationError({"unit_price_snapshot": _("Unit price cannot be negative.")})
        if self.quantity is not None and self.quantity < 1:
            raise ValidationError({"quantity": _("Quantity must be at least 1.")})
        if (
            self.compare_at_price_snapshot
            and self.unit_price_snapshot
            and self.compare_at_price_snapshot < self.unit_price_snapshot
        ):
            raise ValidationError(
                {"compare_at_price_snapshot": _("Compare-at price must be greater than or equal to unit price.")}
            )

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
            return (self.compare_at_price_snapshot - self.unit_price_snapshot) * self.quantity
        return Decimal("0.00")

    @property
    def effective_unit_price(self) -> Decimal:
        return self.unit_price_snapshot

    @property
    def is_available(self) -> bool:
        if self.variant_id:
            return bool(getattr(self.variant, "is_active", False))
        if self.product_id and not getattr(self.product, "is_active", True):
            return False
        return True

    @property
    def is_reservation_active(self) -> bool:
        if self.reserved_until is None:
            return False
        return self.reserved_until > timezone.now()

    @property
    def is_reservation_expired(self) -> bool:
        if self.reserved_until is None:
            return False
        return self.reserved_until <= timezone.now()

    @property
    def minutes_until_reservation_expiry(self) -> Optional[int]:
        if self.reserved_until is None:
            return None
        delta = self.reserved_until - timezone.now()
        if delta.total_seconds() <= 0:
            return 0
        return int(delta.total_seconds() // 60)

    def move_to_save_for_later(self, reason: str = SavedForLaterReason.MANUAL) -> None:
        self.status = self.ItemStatus.SAVED
        self.saved_reason = reason
        self.moved_to_save_at = timezone.now()
        self.save(update_fields=["status", "saved_reason", "moved_to_save_at", "updated_at"])

    def restore_to_active(self) -> None:
        self.status = self.ItemStatus.ACTIVE
        self.saved_reason = None
        self.moved_to_save_at = None
        self.save(update_fields=["status", "saved_reason", "moved_to_save_at", "updated_at"])

    def increment_quantity(self, delta: int = 1) -> None:
        if delta == 0:
            return
        type(self).objects.filter(pk=self.pk).update(
            quantity=models.F("quantity") + delta,
            updated_at=timezone.now(),
        )
        self.refresh_from_db(fields=["quantity", "updated_at"])
        if self.quantity < 1:
            type(self).objects.filter(pk=self.pk).update(quantity=1)
            self.refresh_from_db(fields=["quantity"])
            raise ValidationError({"quantity": _("Quantity cannot fall below 1.")})

    def set_quantity(self, quantity: int) -> None:
        if quantity is None or int(quantity) < 1:
            raise ValidationError({"quantity": _("Quantity must be at least 1.")})
        self.quantity = int(quantity)
        self.save(update_fields=["quantity", "updated_at"])

def get_default_reservation_minutes() -> int:
    return getattr(settings, "CART_DEFAULT_RESERVATION_MINUTES", DEFAULT_CART_RESERVATION_MINUTES)

__all__ = [
    "DEFAULT_CART_RESERVATION_MINUTES",
    "DEFAULT_CART_EXPIRATION_DAYS",
    "DEFAULT_CART_ABANDONMENT_THRESHOLD_HOURS",
    "CartQuerySet",
    "CartManager",
    "Cart",
    "CartItem",
    "get_default_reservation_minutes",
]