from typing import Any, Dict, List
from .models import CustomerTaxExemption, TaxClass, TaxRule, TaxSettings, TaxZone

class TaxSerializer:
    @staticmethod
    def serialize_tax_class(tax_class: TaxClass) -> Dict[str, Any]:
        if not tax_class:
            return {}
        return {
            "id": tax_class.pk,
            "name": tax_class.name,
            "code": tax_class.code,
            "description": tax_class.description or "",
            "is_default": tax_class.is_default,
        }

    @staticmethod
    def serialize_tax_zone(tax_zone: TaxZone) -> Dict[str, Any]:
        if not tax_zone:
            return {}
        return {
            "id": tax_zone.pk,
            "name": tax_zone.name,
            "code": tax_zone.code,
            "countries": tax_zone.countries,
            "states_or_provinces": tax_zone.states_or_provinces,
            "priority": tax_zone.priority,
        }

    @staticmethod
    def serialize_tax_rule(tax_rule: TaxRule) -> Dict[str, Any]:
        if not tax_rule:
            return {}
        return {
            "id": tax_rule.pk,
            "name": tax_rule.name,
            "tax_type": tax_rule.tax_type,
            "rate_type": tax_rule.rate_type,
            "rate_value": str(tax_rule.rate_value),
            "is_compound": tax_rule.is_compound,
            "priority": tax_rule.priority,
        }

    @classmethod
    def serialize_tax_rules_many(cls, rules: List[TaxRule]) -> List[Dict[str, Any]]:
        return [cls.serialize_tax_rule(r) for r in rules if r]