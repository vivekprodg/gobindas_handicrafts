from __future__ import annotations

from django.db.models.signals import post_delete, post_save

from .models import (
    ContactEmail,
    ContactOfficeHour,
    ContactPage,
    ContactPhone,
    ContactSocialLink,
    FooterLink,
    FooterPaymentMethod,
    FooterSection,
    FooterSettings,
    FooterSocialLink,
    FooterTrustBadge,
    HeaderAnnouncement,
    HeaderBar,
    HeaderCountry,
    HeaderCurrency,
    HeaderLanguage,
    HeaderUtilityLink,
    NavbarItem,
    NavbarMegaMenuColumn,
    NavbarMegaMenuLink,
    NavbarSettings,
    SiteSettings,
)
from .services import invalidate_foundation_cms_cache

MODELS_TO_INVALIDATE = [
    SiteSettings, HeaderBar, HeaderAnnouncement, HeaderCurrency, HeaderLanguage,
    HeaderCountry, HeaderUtilityLink, NavbarSettings, NavbarItem, NavbarMegaMenuColumn,
    NavbarMegaMenuLink, FooterSettings, FooterSection, FooterLink, FooterSocialLink,
    FooterPaymentMethod, FooterTrustBadge, ContactPage, ContactPhone, ContactEmail,
    ContactSocialLink, ContactOfficeHour,
]

def clear_cache_handler(sender, **kwargs):
    invalidate_foundation_cms_cache()

for model in MODELS_TO_INVALIDATE:
    post_save.connect(clear_cache_handler, sender=model, dispatch_uid=f"foundation_signal_save_{model.__name__}")
    post_delete.connect(clear_cache_handler, sender=model, dispatch_uid=f"foundation_signal_delete_{model.__name__}")