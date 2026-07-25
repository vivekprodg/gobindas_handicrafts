import logging
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Tuple

from django.conf import settings
from django.utils.translation import gettext_lazy as _

from . import constants as c
from .models import TaxClass, TaxRule, TaxSettings
from .selectors import (
    get_applicable_tax_rules,
    get_default_tax_class,
    get_tax_class_by_code,
    get_tax_settings,
    is_customer_tax_exempt,
    match_tax_zone,
)

logger = logging.getLogger(c.LOGGER_NAME)

class TaxCalculationService:
    """
    Core engine calculating precise, multi-rule tax breakdowns for line items,
    carts, and checkout orders across international jurisdictions and B2B exemptions.
    """

    @classmethod
    def calculate_line_item_tax(
        cls,
        line_subtotal: Decimal,
        tax_class_code: Optional[str] = None,
        country_code: str = "NP",
        state_or_province: str = "",
        postal_code: str = "",
        user: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """
        Calculates item-level tax lines considering exemptions, zones, and compound surcharges.
        """
        tax_settings = get_tax_settings()
        if not tax_settings.enable_tax_calculation:
            return cls._build_zero_tax_result(_("Tax calculations disabled."))

        # Check B2B / Customer Verified Tax Exemption
        if cls.is_user_b2b_or_tax_exempt(user):
            return cls._build_zero_tax_result(_("Verified B2B account / Customer is tax exempt."), is_exempt=True)

        if line_subtotal <= c.ZERO_DECIMAL:
            return cls._build_zero_tax_result(_("Line item subtotal is zero."))

        tax_class = get_tax_class_by_code(tax_class_code or "")
        tax_zone = match_tax_zone(country_code, state_or_province, postal_code)

        rules = list(get_applicable_tax_rules(tax_class, tax_zone)) if (tax_class and tax_zone) else []

        # Fallback to default Nepal VAT or global fallback if no specific rules match
        if not rules:
            fallback_rate = tax_settings.fallback_tax_rate or c.DEFAULT_NEPAL_VAT_RATE
            fallback_amount = (line_subtotal * (fallback_rate / Decimal("100.00"))).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            return {
                "tax_total": fallback_amount,
                "base_subtotal": line_subtotal,
                "is_exempt": False,
                "tax_lines": [
                    {
                        "name": str(_("Value Added Tax (VAT)")),
                        "tax_class_code": tax_class.code if tax_class else "STANDARD",
                        "rate_percentage": fallback_rate,
                        "tax_amount": fallback_amount,
                        "is_compound": False,
                    }
                ],
            }

        tax_lines: List[Dict[str, Any]] = []
        accumulated_base = line_subtotal
        total_tax = c.ZERO_DECIMAL

        for rule in rules:
            if rule.rate_type == c.TaxRateType.PERCENTAGE:
                calculation_base = accumulated_base if rule.is_compound else line_subtotal
                rule_tax = (calculation_base * (rule.rate_value / Decimal("100.00"))).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
            else:
                rule_tax = rule.rate_value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

            total_tax += rule_tax
            accumulated_base += rule_tax

            tax_lines.append({
                "rule_id": rule.pk,
                "name": rule.name,
                "tax_class_code": tax_class.code if tax_class else "STANDARD",
                "rate_percentage": rule.rate_value if rule.rate_type == c.TaxRateType.PERCENTAGE else None,
                "tax_amount": rule_tax,
                "is_compound": rule.is_compound,
            })

        return {
            "tax_total": total_tax,
            "base_subtotal": line_subtotal,
            "is_exempt": False,
            "tax_lines": tax_lines,
        }

    @classmethod
    def is_user_b2b_or_tax_exempt(cls, user: Optional[Any]) -> bool:
        """
        Determines if an authenticated user qualifies for tax exemption via verified tax certificates or approved B2B status.
        """
        if not user or not getattr(user, "is_authenticated", False):
            return False

        if is_customer_tax_exempt(user):
            return True

        profile = getattr(user, "customer_profile", None)
        if profile and profile.is_business_account and profile.is_approved_b2b:
            # Foreign international business entities purchasing for export
            if not profile.is_nepal_entity:
                return True

        return False

    @classmethod
    def calculate_cart_tax(
        cls,
        cart: Any,
        country_code: str = "NP",
        state_or_province: str = "",
        postal_code: str = "",
        user: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """
        Calculates aggregate tax for an active shopping cart.
        """
        if not cart or not getattr(cart, "pk", None):
            return cls._build_zero_tax_result(_("Cart empty."))

        tax_settings = get_tax_settings()
        if not tax_settings.enable_tax_calculation:
            return cls._build_zero_tax_result(_("Tax system disabled."))

        active_items = cart.items.filter(status="active").select_related("product")
        if not active_items.exists():
            return cls._build_zero_tax_result(_("No active cart items."))

        cart_customer = user or (cart.customer if getattr(cart, "customer_id", None) else None)

        if cls.is_user_b2b_or_tax_exempt(cart_customer):
            return cls._build_zero_tax_result(_("Customer is tax exempt."), is_exempt=True)

        total_cart_tax = c.ZERO_DECIMAL
        all_lines: List[Dict[str, Any]] = []

        for item in active_items:
            line_subtotal = item.line_subtotal
            product_tax_class = getattr(item.product, "tax_class", None)

            line_tax_result = cls.calculate_line_item_tax(
                line_subtotal=line_subtotal,
                tax_class_code=product_tax_class,
                country_code=country_code,
                state_or_province=state_or_province,
                postal_code=postal_code,
                user=cart_customer,
            )

            total_cart_tax += line_tax_result["tax_total"]
            all_lines.extend(line_tax_result["tax_lines"])

        if tax_settings.apply_tax_to_shipping and cart.estimated_shipping > c.ZERO_DECIMAL:
            shipping_tax = (cart.estimated_shipping * (tax_settings.fallback_tax_rate / Decimal("100.00"))).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            total_cart_tax += shipping_tax
            all_lines.append({
                "name": str(_("Shipping Tax")),
                "tax_class_code": "SHIPPING",
                "rate_percentage": tax_settings.fallback_tax_rate,
                "tax_amount": shipping_tax,
                "is_compound": False,
            })

        return {
            "tax_total": total_cart_tax,
            "calculation_mode": tax_settings.default_calculation_mode,
            "is_exempt": False,
            "tax_lines": all_lines,
        }

    @staticmethod
    def _build_zero_tax_result(message: str = "", is_exempt: bool = False) -> Dict[str, Any]:
        return {
            "tax_total": c.ZERO_DECIMAL,
            "base_subtotal": c.ZERO_DECIMAL,
            "is_exempt": is_exempt,
            "message": str(message),
            "tax_lines": [],
        }