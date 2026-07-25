from typing import Optional
from django.utils.translation import gettext_lazy as _

class TaxError(Exception):
    """Base domain exception for tax evaluation errors."""
    default_code: str = "tax_error"
    default_message: str = _("A tax evaluation error occurred.")

    def __init__(self, message: Optional[str] = None, code: Optional[str] = None):
        self.message = str(message or self.default_message)
        self.code = str(code or self.default_code)
        super().__init__(self.message)

class TaxCalculationError(TaxError):
    default_code = "tax_calculation_error"
    default_message = _("Failed to compute tax rate for the specified item or address.")

class TaxClassNotFoundError(TaxError):
    default_code = "tax_class_not_found"
    default_message = _("The specified product tax class was not found.")

class TaxZoneNotFoundError(TaxError):
    default_code = "tax_zone_not_found"
    default_message = _("No tax zone or destination rule matches the given location.")

class InvalidTaxRateError(TaxError):
    default_code = "invalid_tax_rate"
    default_message = _("The configured tax rate percentage or flat amount is invalid.")

class TaxExemptionError(TaxError):
    default_code = "tax_exemption_error"
    default_message = _("Unable to verify customer tax exemption credentials.")