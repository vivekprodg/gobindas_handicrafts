from typing import Any, Dict, List
from .models import ShipmentTrackingRecord, ShippingMethod, ShippingZone

class ShippingSerializer:
    @staticmethod
    def serialize_method(method: ShippingMethod, calculated_fee: Any = None) -> Dict[str, Any]:
        if not method:
            return {}

        return {
            "id": method.pk,
            "name": method.name,
            "code": method.code,
            "carrier": method.carrier,
            "carrier_display": method.get_carrier_display(),
            "rate_type": method.rate_type,
            "fee": str(calculated_fee if calculated_fee is not None else method.flat_rate),
            "delivery_time_text": method.estimated_delivery_text,
            "min_days": method.estimated_delivery_days_min,
            "max_days": method.estimated_delivery_days_max,
        }

    @classmethod
    def serialize_methods_many(cls, methods_with_fees: List[tuple]) -> List[Dict[str, Any]]:
        return [
            cls.serialize_method(m, fee) for m, fee in methods_with_fees if m
        ]

    @staticmethod
    def serialize_tracking(record: ShipmentTrackingRecord) -> Dict[str, Any]:
        if not record:
            return {}

        return {
            "tracking_number": record.tracking_number,
            "carrier": record.get_carrier_display(),
            "status": record.current_status,
            "details": record.status_description or "",
            "estimated_delivery": record.estimated_delivery.isoformat() if record.estimated_delivery else None,
        }