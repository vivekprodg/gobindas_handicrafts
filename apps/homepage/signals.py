from __future__ import annotations

from django.db.models.signals import post_save, post_delete

from .models import (
    HomepageSettings,
    HeroSection,
    HeroCTA,
    TrustBarSection,
    TrustBarItem,
    CategorySection,
    TrendingSection,
    TrendingProduct,
    ArtisanStorySection,
    SocialProofSection,
    SocialProofImage,
)

from apps.catalog.models import Category
from .services import invalidate_homepage_cache

# Registry of all CMS models that construct the homepage payload
HOMEPAGE_CMS_MODELS = [
    HomepageSettings,
    HeroSection,
    HeroCTA,
    TrustBarSection,
    TrustBarItem,
    CategorySection,
    TrendingSection,
    TrendingProduct,
    ArtisanStorySection,
    SocialProofSection,
    SocialProofImage,
]

def trigger_homepage_cache_invalidation(sender, instance, **kwargs):
    """
    Invalidates the homepage payload cache whenever a CMS model is created, updated, or deleted.
    """
    invalidate_homepage_cache()

# Dynamically connect the post_save and post_delete signals to all homepage CMS models
for model in HOMEPAGE_CMS_MODELS:
    post_save.connect(trigger_homepage_cache_invalidation, sender=model)
    post_delete.connect(trigger_homepage_cache_invalidation, sender=model)