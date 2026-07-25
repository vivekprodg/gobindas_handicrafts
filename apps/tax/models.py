from decimal import Decimal
from typing import Any, List, Optional

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.foundation.models import CMSBaseModel, SingletonCMSModel
from .constants import (
    DEFAULT_NEPAL_VAT_RATE,
    DEFAULT_TAX_CURRENCY,
    MAX_TAX_RATE_PERCENTAGE,
    TaxCalculationMode,
    TaxRateType,
    TaxType,
    ZERO_DECIMAL,
)

class TaxSettings(SingletonCMSModel):
    """
    Global CMS-driven controls for site-wide tax calculations and default rates.
    """
    enable_tax_calculation = models.BooleanField(
        default=True,
        verbose_name=_("Enable Tax System System-Wide"),
        help_text=_("Toggle to enable or bypass all dynamic tax calculations."),
    )
    default_calculation_mode = models.CharField(
        max_length=20,
        choices=TaxCalculationMode.CHOICES,
        default=TaxCalculationMode.EXCLUSIVE,
        verbose_name=_("Default Tax Calculation Mode"),
    )
    fallback_tax_rate = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        default=DEFAULT_NEPAL_VAT_RATE,
        validators=[MinValueValidator(ZERO_DECIMAL), MaxValueValidator(MAX_TAX_RATE_PERCENTAGE)],
        verbose_name=_("Fallback Global Tax Rate (%)"),
        help_text=_("Applied when no specific regional or product tax rule matches (e.g. 13.00 for Nepal VAT)."),
    )
    apply_tax_to_shipping = models.BooleanField(
        default=False,
        verbose_name=_("Apply Tax to Shipping Fees"),
        help_text=_("Check if delivery charges should be subject to tax."),
    )
    prices_include_tax_in_catalog = models.BooleanField(
        default=False,
        verbose_name=_("Display Store Prices as Tax-Inclusive"),
        help_text=_("If checked, product catalog prices are treated as already including tax."),
    )
    round_tax_at_subtotal_level = models.BooleanField(
        default=True,
        verbose_name=_("Round Tax at Grand Total Level"),
        help_text=_("If false, line-item taxes are rounded individually before summing."),
    )

    class Meta:
        verbose_name = _("Tax Engine Settings")
        verbose_name_plural = _("Tax Engine Settings")

    def __str__(self) -> str:
        return "Global Tax Engine Configuration"

class TaxClass(CMSBaseModel):
    """
    Categorizes products or services into tax buckets (e.g. Standard 13% VAT, Exempt Handicraft, Service Surcharge).
    """
    name = models.CharField(
        max_length=100,
        verbose_name=_("Tax Class Name"),
        help_text=_("Human-readable title (e.g., 'Standard Handicraft VAT', 'Tax Exempt')."),
    )
    code = models.CharField(
        max_length=50,
        unique=True,
        db_index=True,
        verbose_name=_("Tax Class Code"),
        help_text=_("Unique identifier code (e.g. STANDARD, EXEMPT, LUXURY, SERVICE). Auto-uppercased."),
    )
    description = models.TextField(
        blank=True,
        null=True,
        verbose_name=_("Description / Tax Regulations"),
    )
    is_default = models.BooleanField(
        default=False,
        verbose_name=_("Is Default Tax Class"),
        help_text=_("Used for catalog items when no specific tax class is assigned."),
    )
    is_active = models.BooleanField(
        default=True,
        db_index=True,
        verbose_name=_("Is Active"),
    )

    class Meta:
        verbose_name = _("Tax Class")
        verbose_name_plural = _("Tax Classes")
        ordering = ["-is_default", "name"]
        indexes = [
            models.Index(fields=["code", "is_active"]),
        ]

    def clean(self) -> None:
        super().clean()
        if self.code:
            self.code = self.code.strip().upper()

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.clean()
        if self.is_default:
            TaxClass.objects.filter(is_default=True).exclude(pk=self.pk).update(is_default=False)
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.name} ({self.code})"

class TaxZone(CMSBaseModel):
    """
    Defines geographical tax jurisdictions (e.g. Nepal Domestic, USA States, EU Member States).
    """
    name = models.CharField(
        max_length=120,
        verbose_name=_("Tax Zone Name"),
        help_text=_("Title for this geographic tax zone (e.g., 'Nepal Bagmati', 'USA California')."),
    )
    code = models.CharField(
        max_length=50,
        unique=True,
        db_index=True,
        verbose_name=_("Zone Code"),
        help_text=_("Unique code (e.g. ZONE_NP, ZONE_US_CA, ZONE_EU). Auto-uppercased."),
    )
    countries = models.JSONField(
        default=list,
        blank=True,
        verbose_name=_("Country Codes List (ISO 2-letter)"),
        help_text=_("List of ISO country codes (e.g. ['NP', 'US', 'DE', 'FR']). Empty applies globally."),
    )
    states_or_provinces = models.JSONField(
        default=list,
        blank=True,
        verbose_name=_("States / Provinces List"),
        help_text=_("List of state codes or province names (e.g. ['Bagmati', 'CA', 'NY'])."),
    )
    postal_code_pattern = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        verbose_name=_("Postal Code Pattern / Wildcard"),
        help_text=_("Wildcard or prefix matching for zip codes (e.g., '446*' or '90001-90099')."),
    )
    priority = models.PositiveIntegerField(
        default=10,
        verbose_name=_("Matching Priority"),
        help_text=_("Lower numbers take higher precedence during location matching."),
    )
    is_active = models.BooleanField(
        default=True,
        db_index=True,
        verbose_name=_("Is Active"),
    )

    class Meta:
        verbose_name = _("Tax Zone")
        verbose_name_plural = _("Tax Zones")
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

class TaxRule(CMSBaseModel):
    """
    Connects TaxClass and TaxZone with specific rates, supporting stacked or compound surcharges.
    """
    name = models.CharField(
        max_length=150,
        verbose_name=_("Tax Rule Title"),
        help_text=_("Title shown on customer invoices (e.g., 'Nepal VAT 13%', 'Local Craft Surcharge 5%')."),
    )
    tax_class = models.ForeignKey(
        TaxClass,
        on_delete=models.CASCADE,
        related_name="tax_rules",
        verbose_name=_("Product Tax Class"),
    )
    tax_zone = models.ForeignKey(
        TaxZone,
        on_delete=models.CASCADE,
        related_name="tax_rules",
        verbose_name=_("Geographic Tax Zone"),
    )
    tax_type = models.CharField(
        max_length=30,
        choices=TaxType.CHOICES,
        default=TaxType.VAT,
        verbose_name=_("Tax Type"),
    )
    rate_type = models.CharField(
        max_length=20,
        choices=TaxRateType.CHOICES,
        default=TaxRateType.PERCENTAGE,
        verbose_name=_("Rate Type"),
    )
    rate_value = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        validators=[MinValueValidator(ZERO_DECIMAL)],
        verbose_name=_("Tax Rate Value"),
        help_text=_("Percentage (e.g., 13.00 for 13% or 5.00 for 5%) or flat currency value."),
    )
    is_compound = models.BooleanField(
        default=False,
        verbose_name=_("Is Compound Tax"),
        help_text=_("If true, this tax is calculated on top of prior applicable tax line amounts."),
    )
    priority = models.PositiveIntegerField(
        default=10,
        verbose_name=_("Execution Order"),
        help_text=_("Lower priority numbers execute first before compound calculations."),
    )
    is_active = models.BooleanField(
        default=True,
        db_index=True,
        verbose_name=_("Is Active"),
    )

    class Meta:
        verbose_name = _("Tax Rule")
        verbose_name_plural = _("Tax Rules")
        ordering = ["priority", "name"]
        indexes = [
            models.Index(fields=["tax_class", "tax_zone", "is_active"]),
            models.Index(fields=["priority"]),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.tax_class.code} @ {self.tax_zone.code}: {self.rate_value}%)"

class CustomerTaxExemption(CMSBaseModel):
    """
    Tracks verified B2B or organizational tax exemption certificates.
    """
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="tax_exemptions",
        verbose_name=_("Customer Account"),
    )
    exemption_number = models.CharField(
        max_length=100,
        verbose_name=_("Tax Exemption / VAT ID Number"),
    )
    reason = models.CharField(
        max_length=200,
        blank=True,
        null=True,
        verbose_name=_("Reason / Category"),
        help_text=_("e.g. Wholesale Reseller, Export Entity, Non-Profit."),
    )
    is_verified = models.BooleanField(
        default=False,
        db_index=True,
        verbose_name=_("Is Verified by Admin"),
    )
    valid_until = models.DateTimeField(
        blank=True,
        null=True,
        verbose_name=_("Exemption Expiration Date"),
    )

    class Meta:
        verbose_name = _("Customer Tax Exemption")
        verbose_name_plural = _("Customer Tax Exemptions")
        ordering = ["-created_at"]

    @property
    def is_valid_now(self) -> bool:
        if not self.is_verified:
            return False
        if self.valid_until and timezone.now() > self.valid_until:
            return False
        return True

    def __str__(self) -> str:
        status = "Verified" if self.is_valid_now else "Pending/Expired"
        return f"{self.user} - Exemption #{self.exemption_number} [{status}]"