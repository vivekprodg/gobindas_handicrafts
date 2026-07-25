"""
Enterprise ORM models for dynamic, CMS-driven coupons and promotional vouchers.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Optional

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import F, Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.foundation.models import CMSBaseModel, SingletonCMSModel
from . import constants as c

class CouponQuerySet(models.QuerySet["Coupon"]):
    def active(self) -> CouponQuerySet:
        now = timezone.now()
        return self.filter(
            is_active=True
        ).filter(
            Q(valid_from__isnull=True) | Q(valid_from__lte=now)
        ).filter(
            Q(valid_to__isnull=True) | Q(valid_to__gte=now)
        )

    def public(self) -> CouponQuerySet:
        return self.active().filter(is_public=True)

    def auto_applicable(self) -> CouponQuerySet:
        return self.active().filter(auto_apply=True)

class CouponManager(models.Manager["Coupon"]):
    def get_queryset(self) -> CouponQuerySet:
        return CouponQuerySet(self.model, using=self._db)

    def active(self) -> CouponQuerySet:
        return self.get_queryset().active()

    def public(self) -> CouponQuerySet:
        return self.get_queryset().public()

    def auto_applicable(self) -> CouponQuerySet:
        return self.get_queryset().auto_applicable()

class CouponCMSSetting(SingletonCMSModel):
    """
    Global CMS configurations for display widgets, promo bars, and coupon behavior.
    """
    enable_coupon_system = models.BooleanField(
        default=True,
        verbose_name=_("Enable Coupon System System-Wide")
    )
    show_public_coupons_in_cart = models.BooleanField(
        default=True,
        verbose_name=_("Show Available Coupons List in Cart/Checkout")
    )
    public_section_title = models.CharField(
        max_length=150,
        default="Available Craft Offers & Vouchers",
        verbose_name=_("Public Coupon Widget Title")
    )
    public_section_subtitle = models.CharField(
        max_length=255,
        default="Apply a voucher code below to enjoy exclusive artisan savings.",
        blank=True,
        null=True,
        verbose_name=_("Public Coupon Widget Subtitle")
    )
    banner_message = models.TextField(
        blank=True,
        null=True,
        verbose_name=_("Cart Coupon Promotional Announcement Banner")
    )
    auto_apply_best_coupon = models.BooleanField(
        default=False,
        verbose_name=_("Auto-Apply Best Qualifying Coupon Automatically")
    )

    class Meta:
        verbose_name = _("Coupon CMS Settings")
        verbose_name_plural = _("Coupon CMS Settings")

    def __str__(self) -> str:
        return "Global Coupon CMS Settings"

class Coupon(CMSBaseModel):
    """
    Master coupon data model defining discount rules, targeting, and restrictions.
    """
    code = models.CharField(
        max_length=50,
        unique=True,
        db_index=True,
        verbose_name=_("Coupon Code"),
        help_text=_("Case-insensitive uppercase coupon code (e.g., HANDICRAFT10).")
    )
    title = models.CharField(
        max_length=200,
        verbose_name=_("Offer Title / Headline")
    )
    description = models.TextField(
        blank=True,
        null=True,
        verbose_name=_("Terms & Conditions Description")
    )
    promo_badge_text = models.CharField(
        max_length=60,
        blank=True,
        null=True,
        verbose_name=_("Public Badge Text"),
        help_text=_("Short tag displayed on product cards/cart (e.g., '15% OFF').")
    )

    discount_type = models.CharField(
        max_length=20,
        choices=c.DiscountType.CHOICES,
        default=c.DiscountType.PERCENTAGE,
        db_index=True,
        verbose_name=_("Discount Calculation Type")
    )
    discount_value = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(c.ZERO_DECIMAL)],
        verbose_name=_("Discount Value"),
        help_text=_("Percentage (0-100) or fixed amount value depending on calculation type.")
    )
    max_discount_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        blank=True,
        null=True,
        validators=[MinValueValidator(c.ZERO_DECIMAL)],
        verbose_name=_("Maximum Discount Cap (NPR)"),
        help_text=_("Optional cap for percentage discounts.")
    )
    min_subtotal = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=c.ZERO_DECIMAL,
        validators=[MinValueValidator(c.ZERO_DECIMAL)],
        verbose_name=_("Minimum Order Subtotal (NPR)")
    )

    target_scope = models.CharField(
        max_length=20,
        choices=c.TargetScope.CHOICES,
        default=c.TargetScope.ALL_PRODUCTS,
        db_index=True,
        verbose_name=_("Product Targeting Scope")
    )
    target_categories = models.ManyToManyField(
        "catalog.Category",
        blank=True,
        related_name="targeted_coupons",
        verbose_name=_("Target Categories")
    )
    target_products = models.ManyToManyField(
        "catalog.Product",
        blank=True,
        related_name="targeted_coupons",
        verbose_name=_("Target Products")
    )
    target_artisans = models.ManyToManyField(
        "catalog.Artisan",
        blank=True,
        related_name="targeted_coupons",
        verbose_name=_("Target Master Artisans")
    )
    target_collections = models.ManyToManyField(
        "catalog.ProductCollection",
        blank=True,
        related_name="targeted_coupons",
        verbose_name=_("Target Craft Collections")
    )

    customer_scope = models.CharField(
        max_length=20,
        choices=c.CustomerScope.CHOICES,
        default=c.CustomerScope.ALL_CUSTOMERS,
        db_index=True,
        verbose_name=_("Customer Eligibility Scope")
    )
    target_customers = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        blank=True,
        related_name="assigned_coupons",
        verbose_name=_("Specific Eligible Customers")
    )

    valid_from = models.DateTimeField(
        blank=True,
        null=True,
        db_index=True,
        verbose_name=_("Activation Start Date")
    )
    valid_to = models.DateTimeField(
        blank=True,
        null=True,
        db_index=True,
        verbose_name=_("Expiration End Date")
    )

    usage_limit_total = models.PositiveIntegerField(
        blank=True,
        null=True,
        verbose_name=_("Global Total Usage Limit"),
        help_text=_("Maximum total times this coupon can be redeemed platform-wide.")
    )
    usage_limit_per_user = models.PositiveIntegerField(
        default=1,
        validators=[MinValueValidator(1)],
        verbose_name=_("Usage Limit Per Customer")
    )
    times_used = models.PositiveIntegerField(
        default=0,
        verbose_name=_("Total Times Redeemed Counter")
    )

    is_active = models.BooleanField(
        default=True,
        db_index=True,
        verbose_name=_("Is Active")
    )
    is_public = models.BooleanField(
        default=True,
        db_index=True,
        verbose_name=_("Is Publicly Displayed in Store")
    )
    auto_apply = models.BooleanField(
        default=False,
        verbose_name=_("Auto-Apply to Cart when Qualified")
    )
    stackable = models.BooleanField(
        default=False,
        verbose_name=_("Can Stack With Other Coupons")
    )
    exclude_sale_items = models.BooleanField(
        default=False,
        verbose_name=_("Exclude Sale / Discounted Products")
    )

    objects: CouponManager = CouponManager()

    class Meta:
        verbose_name = _("Coupon Voucher")
        verbose_name_plural = _("Coupon Vouchers")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["code", "is_active"]),
            models.Index(fields=["is_active", "is_public"]),
            models.Index(fields=["valid_from", "valid_to"]),
            models.Index(fields=["target_scope", "customer_scope"]),
        ]

    def clean(self) -> None:
        super().clean()
        if self.code:
            self.code = self.code.strip().upper()

        if self.discount_type == c.DiscountType.PERCENTAGE:
            if self.discount_value > c.MAX_DISCOUNT_PERCENTAGE:
                raise ValidationError(
                    {"discount_value": _("Percentage discount cannot exceed 100%.")}
                )

        if self.valid_from and self.valid_to and self.valid_from >= self.valid_to:
            raise ValidationError(
                {"valid_to": _("Expiration end date must be strictly after the activation start date.")}
            )

    def save(self, *args: Any, **kwargs: Any) -> None:
        if self.code:
            self.code = self.code.strip().upper()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.code} - {self.title}"

    @property
    def is_expired(self) -> bool:
        if self.valid_to is None:
            return False
        return timezone.now() > self.valid_to

    @property
    def is_valid_now(self) -> bool:
        if not self.is_active:
            return False
        now = timezone.now()
        if self.valid_from and now < self.valid_from:
            return False
        if self.valid_to and now > self.valid_to:
            return False
        if self.usage_limit_total and self.times_used >= self.usage_limit_total:
            return False
        return True

    def increment_usage(self, commit: bool = True) -> None:
        self.times_used = F("times_used") + 1
        if commit:
            self.save(update_fields=["times_used", "updated_at"])
            self.refresh_from_db(fields=["times_used"])

    def decrement_usage(self, commit: bool = True) -> None:
        self.times_used = models.Case(
            models.When(times_used__gt=0, then=F("times_used") - 1),
            default=0,
            output_field=models.PositiveIntegerField(),
        )
        if commit:
            self.save(update_fields=["times_used", "updated_at"])
            self.refresh_from_db(fields=["times_used"])

class CouponUsageRecord(CMSBaseModel):
    """
    Detailed ledger tracking individual coupon redemptions per order and customer.
    """
    coupon = models.ForeignKey(
        Coupon,
        on_delete=models.PROTECT,
        related_name="usage_records",
        verbose_name=_("Coupon")
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="coupon_redemptions",
        verbose_name=_("Customer")
    )
    order = models.ForeignKey(
        "orders.Order",
        on_delete=models.CASCADE,
        related_name="coupon_redemption_records",
        verbose_name=_("Order")
    )
    discount_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(c.ZERO_DECIMAL)],
        verbose_name=_("Discount Amount Applied (NPR)")
    )
    used_at = models.DateTimeField(
        default=timezone.now,
        db_index=True,
        verbose_name=_("Redemption Timestamp")
    )
    is_reversed = models.BooleanField(
        default=False,
        db_index=True,
        verbose_name=_("Is Reversed / Cancelled")
    )
    reversal_reason = models.TextField(
        blank=True,
        null=True,
        verbose_name=_("Reversal Reason")
    )

    class Meta:
        verbose_name = _("Coupon Usage Ledger Record")
        verbose_name_plural = _("Coupon Usage Ledger Records")
        ordering = ["-used_at"]
        indexes = [
            models.Index(fields=["coupon", "user"]),
            models.Index(fields=["order", "is_reversed"]),
            models.Index(fields=["used_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.user.username} redeemed {self.coupon.code} on Order #{self.order_id}"