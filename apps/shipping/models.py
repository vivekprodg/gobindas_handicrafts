from decimal import Decimal
from typing import Any, List, Optional

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.foundation.models import CMSBaseModel, SingletonCMSModel
from .constants import (
    CarrierCode,
    DEFAULT_SHIPPING_CURRENCY,
    DEFAULT_WEIGHT_UNIT,
    ShippingRateType,
    ZERO_DECIMAL,
)

class ShippingSettings(SingletonCMSModel):
    """
    Global CMS configurations for shipping calculations, default origin, and fallback rates.
    """
    enable_shipping_calculation = models.BooleanField(
        default=True,
        verbose_name=_("Enable Shipping Rate Calculation System-Wide"),
    )
    default_fallback_rate = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("15.00"),
        validators=[MinValueValidator(ZERO_DECIMAL)],
        verbose_name=_("Fallback Delivery Rate (US$)"),
        help_text=_("Applied if no matching zone or shipping method is configured."),
    )
    free_shipping_subtotal_threshold = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        blank=True,
        null=True,
        validators=[MinValueValidator(ZERO_DECIMAL)],
        verbose_name=_("Global Free Shipping Threshold (US$)"),
        help_text=_("Cart subtotal amount at which standard shipping becomes free storewide."),
    )
    default_weight_unit = models.CharField(
        max_length=10,
        default=DEFAULT_WEIGHT_UNIT,
        verbose_name=_("Default Weight Unit"),
    )
    origin_country_code = models.CharField(
        max_length=2,
        default="NP",
        verbose_name=_("Origin Country (ISO 2-letter)"),
    )
    origin_city = models.CharField(
        max_length=100,
        default="Kathmandu",
        verbose_name=_("Origin Dispatch City"),
    )

    class Meta:
        verbose_name = _("Shipping Engine Settings")
        verbose_name_plural = _("Shipping Engine Settings")

    def __str__(self) -> str:
        return "Global Shipping Engine Configuration"

class ShippingZone(CMSBaseModel):
    """
    Defines geographical delivery regions (e.g., Nepal Domestic, SAARC Region, North America, Europe).
    """
    name = models.CharField(
        max_length=120,
        verbose_name=_("Zone Name"),
        help_text=_("Title (e.g. 'Nepal Kathmandu Valley', 'USA & Canada', 'EU Member States')."),
    )
    code = models.CharField(
        max_length=50,
        unique=True,
        db_index=True,
        verbose_name=_("Zone Code"),
        help_text=_("Unique identifier (e.g. ZONE_NP_KTM, ZONE_US_CA, ZONE_GLOBAL). Auto-uppercased."),
    )
    countries = models.JSONField(
        default=list,
        blank=True,
        verbose_name=_("Target Country ISO Codes"),
        help_text=_("JSON list of 2-letter ISO country codes (e.g. ['NP', 'US', 'DE']). Empty applies globally."),
    )
    states_or_provinces = models.JSONField(
        default=list,
        blank=True,
        verbose_name=_("Target States / Provinces"),
        help_text=_("Optional list of specific states or provinces (e.g. ['Bagmati', 'California'])."),
    )
    priority = models.PositiveIntegerField(
        default=10,
        verbose_name=_("Matching Priority"),
        help_text=_("Lower priority numbers execute first during location lookup."),
    )
    is_active = models.BooleanField(
        default=True,
        db_index=True,
        verbose_name=_("Is Active"),
    )

    class Meta:
        verbose_name = _("Shipping Zone")
        verbose_name_plural = _("Shipping Zones")
        ordering = ["priority", "name"]
        indexes = [
            models.Index(fields=["code", "is_active"]),
            models.Index(fields=["priority"]),
        ]

    def clean(self) -> None:
        super().clean()
        if self.code:
            self.code = self.code.strip().upper()

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.clean()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.name} [{self.code}]"

class ShippingMethod(CMSBaseModel):
    """
    Delivery service options offered within a ShippingZone (e.g. Standard, Express, Local Pickup).
    """
    name = models.CharField(
        max_length=150,
        verbose_name=_("Method Title"),
        help_text=_("Title shown to customers at checkout (e.g. 'Nepal Express Courier', 'DHL Worldwide')."),
    )
    code = models.CharField(
        max_length=50,
        unique=True,
        db_index=True,
        verbose_name=_("Method Code"),
        help_text=_("Unique code (e.g. KTM_STD, DHL_EXPRESS, LOCAL_PICKUP). Auto-uppercased."),
    )
    carrier = models.CharField(
        max_length=50,
        choices=CarrierCode.CHOICES,
        default=CarrierCode.LOCAL_COURIER,
        verbose_name=_("Delivery Carrier"),
    )
    zone = models.ForeignKey(
        ShippingZone,
        on_delete=models.CASCADE,
        related_name="shipping_methods",
        verbose_name=_("Geographic Shipping Zone"),
    )
    rate_type = models.CharField(
        max_length=30,
        choices=ShippingRateType.CHOICES,
        default=ShippingRateType.FLAT_RATE,
        verbose_name=_("Calculation Method"),
    )
    flat_rate = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=ZERO_DECIMAL,
        validators=[MinValueValidator(ZERO_DECIMAL)],
        verbose_name=_("Flat Rate Base Amount (US$)"),
    )
    min_order_subtotal = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=ZERO_DECIMAL,
        validators=[MinValueValidator(ZERO_DECIMAL)],
        verbose_name=_("Minimum Order Subtotal"),
    )
    max_weight_kg = models.DecimalField(
        max_digits=10,
        decimal_places=3,
        blank=True,
        null=True,
        validators=[MinValueValidator(ZERO_DECIMAL)],
        verbose_name=_("Maximum Package Weight (kg)"),
    )
    estimated_delivery_days_min = models.PositiveIntegerField(
        default=2,
        verbose_name=_("Est. Min Delivery Days"),
    )
    estimated_delivery_days_max = models.PositiveIntegerField(
        default=5,
        verbose_name=_("Est. Max Delivery Days"),
    )
    is_active = models.BooleanField(
        default=True,
        db_index=True,
        verbose_name=_("Is Active"),
    )
    position = models.PositiveIntegerField(
        default=0,
        verbose_name=_("Display Position"),
    )

    class Meta:
        verbose_name = _("Shipping Method")
        verbose_name_plural = _("Shipping Methods")
        ordering = ["position", "name"]
        indexes = [
            models.Index(fields=["code", "is_active"]),
            models.Index(fields=["zone", "is_active"]),
        ]

    def clean(self) -> None:
        super().clean()
        if self.code:
            self.code = self.code.strip().upper()
        if self.estimated_delivery_days_min > self.estimated_delivery_days_max:
            raise ValidationError(
                {"estimated_delivery_days_max": _("Max delivery days must be greater than or equal to min delivery days.")}
            )

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.clean()
        super().save(*args, **kwargs)

    @property
    def estimated_delivery_text(self) -> str:
        if self.estimated_delivery_days_min == self.estimated_delivery_days_max:
            return _("%(days)d Business Day(s)") % {"days": self.estimated_delivery_days_min}
        return _("%(min)d–%(max)d Business Days") % {
            "min": self.estimated_delivery_days_min,
            "max": self.estimated_delivery_days_max,
        }

    def __str__(self) -> str:
        return f"{self.name} ({self.zone.code} - {self.get_rate_type_display()})"

class WeightTierRate(CMSBaseModel):
    """
    Tiered weight pricing brackets for a specific ShippingMethod.
    """
    shipping_method = models.ForeignKey(
        ShippingMethod,
        on_delete=models.CASCADE,
        related_name="weight_tiers",
        verbose_name=_("Shipping Method"),
    )
    min_weight_kg = models.DecimalField(
        max_digits=10,
        decimal_places=3,
        default=ZERO_DECIMAL,
        validators=[MinValueValidator(ZERO_DECIMAL)],
        verbose_name=_("Min Weight Bracket (kg)"),
    )
    max_weight_kg = models.DecimalField(
        max_digits=10,
        decimal_places=3,
        validators=[MinValueValidator(ZERO_DECIMAL)],
        verbose_name=_("Max Weight Bracket (kg)"),
    )
    rate_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(ZERO_DECIMAL)],
        verbose_name=_("Tier Delivery Fee (US$)"),
    )

    class Meta:
        verbose_name = _("Weight Bracket Rate")
        verbose_name_plural = _("Weight Bracket Rates")
        ordering = ["min_weight_kg"]

    def clean(self) -> None:
        super().clean()
        if self.min_weight_kg >= self.max_weight_kg:
            raise ValidationError({"max_weight_kg": _("Max weight must be greater than min weight.")})

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.clean()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.shipping_method.name}: {self.min_weight_kg}kg - {self.max_weight_kg}kg -> US$ {self.rate_amount}"

class ShipmentTrackingRecord(CMSBaseModel):
    """
    Live tracking status updates from carriers (Nepal Post, DHL, FedEx, Local Couriers).
    """
    tracking_number = models.CharField(
        max_length=100,
        unique=True,
        db_index=True,
        verbose_name=_("Tracking Number"),
    )
    carrier = models.CharField(
        max_length=50,
        choices=CarrierCode.CHOICES,
        default=CarrierCode.LOCAL_COURIER,
        verbose_name=_("Carrier"),
    )
    order = models.ForeignKey(
        "orders.Order",
        on_delete=models.CASCADE,
        related_name="shipping_tracking_records",
        blank=True,
        null=True,
        verbose_name=_("Associated Order"),
    )
    current_status = models.CharField(
        max_length=100,
        default="In Transit",
        verbose_name=_("Current Status"),
    )
    status_description = models.TextField(
        blank=True,
        null=True,
        verbose_name=_("Status Details / Checkpoint Notes"),
    )
    estimated_delivery = models.DateTimeField(
        blank=True,
        null=True,
        verbose_name=_("Estimated Delivery Time"),
    )

    class Meta:
        verbose_name = _("Shipment Tracking Record")
        verbose_name_plural = _("Shipment Tracking Records")
        ordering = ["-updated_at"]

    def __str__(self) -> str:
        return f"Tracking #{self.tracking_number} [{self.carrier}] - {self.current_status}"