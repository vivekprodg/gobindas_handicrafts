"""
Serializers for formatting coupon domain objects and API responses.
"""
from __future__ import annotations

from typing import Any, Dict, List
from .models import Coupon, CouponCMSSetting

class CouponSerializer:
    """
    Serializes Coupon ORM model to dictionary payload for REST endpoints & UI templates.
    """

    @staticmethod
    def serialize(coupon: Coupon) -> Dict[str, Any]:
        if not coupon:
            return {}

        return {
            "id": coupon.pk,
            "code": coupon.code,
            "title": coupon.title,
            "description": coupon.description or "",
            "promo_badge_text": coupon.promo_badge_text or "",
            "discount_type": coupon.discount_type,
            "discount_value": str(coupon.discount_value),
            "max_discount_amount": str(coupon.max_discount_amount) if coupon.max_discount_amount else None,
            "min_subtotal": str(coupon.min_subtotal),
            "target_scope": coupon.target_scope,
            "customer_scope": coupon.customer_scope,
            "valid_from": coupon.valid_from.isoformat() if coupon.valid_from else None,
            "valid_to": coupon.valid_to.isoformat() if coupon.valid_to else None,
            "is_expired": coupon.is_expired,
            "is_valid_now": coupon.is_valid_now,
            "auto_apply": coupon.auto_apply,
            "stackable": coupon.stackable,
        }

    @classmethod
    def serialize_many(cls, coupons: List[Coupon]) -> List[Dict[str, Any]]:
        return [cls.serialize(coupon) for coupon in coupons if coupon]

class CouponCMSSettingSerializer:
    """
    Serializes Coupon CMS settings to dict.
    """

    @staticmethod
    def serialize(setting: CouponCMSSetting) -> Dict[str, Any]:
        if not setting:
            return {}

        return {
            "enable_coupon_system": setting.enable_coupon_system,
            "show_public_coupons_in_cart": setting.show_public_coupons_in_cart,
            "public_section_title": setting.public_section_title,
            "public_section_subtitle": setting.public_section_subtitle or "",
            "banner_message": setting.banner_message or "",
            "auto_apply_best_coupon": setting.auto_apply_best_coupon,
        }