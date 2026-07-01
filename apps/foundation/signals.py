from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from .models import (
    SiteSettings,
    HeaderBar,
    HeaderAnnouncement,
    HeaderCurrency,
    HeaderLanguage,
    HeaderUtilityLink,
    NavbarItem,
    FooterSettings,
    FooterSection,
    FooterLink,
    FooterSocialLink,
    FooterPaymentMethod,
    FooterTrustBadge
)
from .services import invalidate_foundation_cms_cache

MODELS_TO_INVALIDATE = [
    SiteSettings,
    HeaderBar,
    HeaderAnnouncement,
    HeaderCurrency,
    HeaderLanguage,
    HeaderUtilityLink,
    NavbarItem,
    FooterSettings,
    FooterSection,
    FooterLink,
    FooterSocialLink,
    FooterPaymentMethod,
    FooterTrustBadge
]

def clear_cache_handler(sender, **kwargs):
    invalidate_foundation_cms_cache()

for model in MODELS_TO_INVALIDATE:
    post_save.connect(clear_cache_handler, sender=model)
    post_delete.connect(clear_cache_handler, sender=model)