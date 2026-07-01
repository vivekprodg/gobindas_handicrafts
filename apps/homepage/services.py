from __future__ import annotations

from typing import Any

from django.core.cache import cache

from apps.catalog.models import Category
from apps.foundation.services import resolve_navigation_url

from .models import (
    ArtisanStorySection,
    CategorySection,
    HeroSection,
    HomepageSettings,
    SocialProofSection,
    TrendingSection,
    TrustBarSection,
)

# =========================================
# CACHE CONFIGURATION
# =========================================
HOMEPAGE_CMS_CACHE_VERSION = 1
HOMEPAGE_CMS_CACHE_TIMEOUT = 60 * 30  # 30 minutes
HOMEPAGE_PAYLOAD_CACHE_KEY = (
    f"homepage:cms:v{HOMEPAGE_CMS_CACHE_VERSION}:payload"
)


def invalidate_homepage_cache() -> None:
    """
    Clears the homepage CMS cache.
    Call this from Django Admin save/delete signals for homepage models.
    """
    cache.delete(HOMEPAGE_PAYLOAD_CACHE_KEY)


def get_homepage_payload(*, use_cache: bool = True) -> dict[str, Any]:
    """
    Compiles and returns the fully dynamic CMS homepage payload.
    Homepage categories are sourced directly from catalog.Category.
    """

    if use_cache:
        cached_payload = cache.get(HOMEPAGE_PAYLOAD_CACHE_KEY)
        if cached_payload is not None:
            return cached_payload

    # =========================================
    # Global Settings
    # =========================================
    settings = HomepageSettings.objects.first()

    if settings and not settings.is_active:
        return {
            "page_id": "homepage_global",
            "page_title": settings.page_title,
            "modules": [],
        }

    modules: list[dict[str, Any]] = []

    # =========================================
    # Dynamic Hero Module
    # =========================================
    hero = HeroSection.objects.prefetch_related("ctas").first()

    if hero:
        modules.append(
            {
                "type": "dynamic_hero",
                "parameters": {
                    "background_media": (
                        hero.background_media.url
                        if hero.background_media
                        else ""
                    ),
                    "subtitle": hero.subtitle or "",
                    "title": hero.title or "",
                    "ctas": [
                        {
                            "label": cta.label or "",
                            "url": (
                                resolve_navigation_url(cta.url)
                                if cta.url
                                else ""
                            ),
                            "style": cta.style or "btn-primary",
                        }
                        for cta in hero.ctas.all()
                    ],
                },
            }
        )

    # =========================================
    # Trust Bar Module
    # =========================================
    trust_bar = TrustBarSection.objects.prefetch_related(
        "signals"
    ).first()

    if trust_bar and trust_bar.is_active:
        modules.append(
            {
                "type": "trust_bar",
                "parameters": {
                    "signals": [
                        {
                            "icon": signal.icon or "",
                            "text": signal.text or "",
                        }
                        for signal in trust_bar.signals.all()
                    ]
                },
            }
        )

    # =========================================
    # Visual Discovery (Homepage Categories)
    # =========================================
    discovery = CategorySection.objects.first()

    if discovery and discovery.is_active:
        homepage_categories = (
            Category.objects.filter(
                show_on_homepage=True,
                is_active=True,
            )
            .order_by("sort_order", "name")
        )

        modules.append(
            {
                "type": "visual_discovery",
                "parameters": {
                    "heading": discovery.heading or "",
                    "categories": [
                        {
                            "name": category.name,
                            "image": (
                                category.image.url
                                if category.image
                                else ""
                            ),
                            "link": f"/category/{category.slug}/",
                        }
                        for category in homepage_categories
                    ],
                },
            }
        )

    # =========================================
    # Merchandising Carousel Module
    # =========================================
    merch = TrendingSection.objects.prefetch_related(
        "items__product"
    ).first()

    if merch:
        modules.append(
            {
                "type": "merchandising_carousel",
                "parameters": {
                    "heading": merch.heading or "",
                    "items": [
                        {
                            "title": (
                                item.product.title
                                if item.product
                                else (item.title or "")
                            ),
                            "price": (
                                f"NPR {item.product.price:,.0f}"
                                if item.product
                                else (item.price or "")
                            ),
                            "image": (
                                item.product.primary_image.url
                                if (
                                    item.product
                                    and item.product.primary_image
                                )
                                else (
                                    item.image.url
                                    if item.image
                                    else ""
                                )
                            ),
                            "badge": (
                                item.product.badge_text
                                if item.product
                                else item.badge
                            ),
                        }
                        for item in merch.items.all()
                    ],
                },
            }
        )

    # =========================================
    # Meet The Maker Module
    # =========================================
    story = ArtisanStorySection.objects.select_related(
        "artisan"
    ).first()

    if story:
        modules.append(
            {
                "type": "story_split",
                "parameters": {
                    "artisan_name": (
                        story.artisan.name
                        if story.artisan
                        else (story.artisan_name or "")
                    ),
                    "image": (
                        story.artisan.image.url
                        if (
                            story.artisan
                            and story.artisan.image
                        )
                        else (
                            story.image.url
                            if story.image
                            else ""
                        )
                    ),
                    "quote": (
                        story.artisan.quote
                        if story.artisan
                        else (story.quote or "")
                    ),
                    "bio": (
                        story.artisan.bio
                        if story.artisan
                        else (story.bio or "")
                    ),
                    "button_text": story.button_text or "",
                    "target_url": (
                        f"/artisans/{story.artisan.slug}/"
                        if story.artisan
                        else (
                            resolve_navigation_url(
                                story.target_url
                            )
                            if story.target_url
                            else ""
                        )
                    ),
                },
            }
        )

    # =========================================
    # Social Proof Module
    # =========================================
    social = SocialProofSection.objects.prefetch_related(
        "images"
    ).first()

    if social:
        modules.append(
            {
                "type": "social_proof",
                "parameters": {
                    "heading": social.heading or "",
                    "images": [
                        img.image.url
                        for img in social.images.all()
                        if img.image
                    ],
                },
            }
        )

    # =========================================
    # Final Payload
    # =========================================
    payload = {
        "page_id": "homepage_global",
        "page_title": (
            settings.page_title
            if settings and settings.page_title
            else "Gobindas Handicrafts - Enterprise Homepage"
        ),
        "modules": modules,
    }

    if use_cache:
        cache.set(
            HOMEPAGE_PAYLOAD_CACHE_KEY,
            payload,
            HOMEPAGE_CMS_CACHE_TIMEOUT,
        )

    return payload