from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured

from .models import (
    FooterSettings, 
    HeaderBar, 
    NavbarItem, 
    SiteSettings, 
    NavbarSettings
)
from .services import (
    build_navbar_tree,
    get_footer_data,
    get_header_bar_cached,
    get_site_settings_cached,
)
from apps.catalog.models import Category, Product, Artisan

def foundation_cms_context(request):
    """
    Global CMS context for the foundation app.

    Exposes:
    - site_settings: singleton record (optional/gracefully handled)
    - header_bar: optional singleton record
    - header_bar_announcements_json: JSON serialized list of announcement campaigns
    - navbar_items: nested CMS-driven navbar tree
    - menu_categories: dynamically retrieved categories matching the catalog structure
    - navbar_queryset: raw queryset for advanced template usage if needed
    - footer_data: dynamic compiled footer structures for global render pipelines
    - ALL Expanded Header CMS data (utilities, config, visibility, datasets, contacts, selectors)
    - ALL Expanded Navbar CMS data (navigation structure, settings, features, branding)
    """
    # =========================================================
    # 1. CORE CACHED PAYLOADS (MANDATORY BACKWARD COMPATIBILITY)
    # =========================================================
    
    # 1A. Site Settings (Branding, Features, Global)
    try:
        site_settings = get_site_settings_cached()
    except Exception:
        site_settings = None

    # 1B. Header Bar Data Payload (Utilities, Announcements, Selectors)
    try:
        header_bar = get_header_bar_cached()
    except Exception:
        header_bar = None

    # 1C. Navbar Tree (Navigation structure, Mega Menus, Featured Content)
    try:
        navbar_items = build_navbar_tree()
    except Exception:
        navbar_items = []

    # 1D. Footer Data (Links, Brand, Trust, Payments)
    try:
        footer_data = get_footer_data()
    except Exception:
        footer_data = {}

    # =========================================================
    # 2. EXPANDED SETTINGS (CACHED DIRECTLY FOR PERFORMANCE)
    # =========================================================

    # 2A. Header Config & Visibility Settings
    try:
        header_settings = cache.get("foundation:cms:v1:header_settings")
        if header_settings is None:
            hb = HeaderBar.objects.first()
            if hb:
                header_settings = {
                    "is_enabled": hb.is_enabled,
                    "is_sticky": hb.is_sticky,
                    "show_on_desktop": hb.show_on_desktop,
                    "show_on_mobile": hb.show_on_mobile,
                }
            else:
                header_settings = {}
            cache.set("foundation:cms:v1:header_settings", header_settings, 60 * 30)
    except Exception:
        header_settings = {}

    # 2B. Global Navigation Settings
    try:
        navbar_settings_data = cache.get("foundation:cms:v1:navbar_settings")
        if navbar_settings_data is None:
            ns = NavbarSettings.objects.first()
            if ns:
                navbar_settings_data = {
                    "is_enabled": ns.is_enabled,
                    "is_sticky": ns.is_sticky,
                    "desktop_behavior": ns.desktop_behavior,
                    "mobile_behavior": ns.mobile_behavior,
                }
            else:
                navbar_settings_data = {}
            cache.set("foundation:cms:v1:navbar_settings", navbar_settings_data, 60 * 30)
    except Exception:
        navbar_settings_data = {}

    # =========================================================
    # 3. LEGACY & RAW DATA FALLBACKS
    # =========================================================

    # 3A. Menu Categories (Dynamic catalog structure)
    try:
        menu_categories = (
            Category.objects
            .filter(
                show_in_menu=True,
                is_active=True,
                parent=None
            )
            .prefetch_related("children")
        )
    except Exception:
        menu_categories = Category.objects.none()

    # 3B. Serialized JSON for client-side campaign rotators
    header_bar_announcements_json = "[]"
    if header_bar and header_bar.get('announcement_messages'):
        try:
            header_bar_announcements_json = json.dumps(header_bar.get('announcement_messages'))
        except Exception:
            header_bar_announcements_json = "[]"

    # 3C. Raw Navbar Queryset
    try:
        navbar_qs = NavbarItem.objects.select_related("parent").order_by(
            "parent_id", "position", "label", "id"
        )
    except Exception:
        navbar_qs = NavbarItem.objects.none()

    # =========================================================
    # 4. GRANULAR EXTRACTIONS FOR TEMPLATE CONVENIENCE
    # =========================================================

    # 4A. Header Utility Extraction & Semantic Mapping
    header_left_utils = header_bar.get("left_utilities", []) if header_bar else []
    header_right_utils = header_bar.get("right_utilities", []) if header_bar else []
    all_header_utils = header_left_utils + header_right_utils

    header_phone_links = [u for u in all_header_utils if u.get("icon_key") == "phone"]
    header_email_links = [u for u in all_header_utils if u.get("icon_key") in ("email", "envelope")]
    header_store_locator_links = [u for u in all_header_utils if u.get("icon_key") in ("map-pin", "marker")]
    header_account_links = [u for u in all_header_utils if isinstance(u, dict) and ( "user" in str(u.get("icon_key") or "").lower() or "account" in str(u.get("url") or "").lower() ) ]

    # 4B. Safe Brand Extractions
    brand_title = site_settings.brand_title if site_settings else "Gobindas"
    brand_subtitle = site_settings.brand_subtitle if site_settings else "Handicrafts"
    brand_url = site_settings.brand_url if site_settings else "/"
    brand_logo_url = site_settings.logo.url if site_settings and getattr(site_settings, "logo", None) else None
    brand_mobile_logo_url = site_settings.mobile_logo.url if site_settings and getattr(site_settings, "mobile_logo", None) else brand_logo_url

    # =========================================================
    # 5. GLOBAL CONTEXT PAYLOAD ASSEMBLY
    # =========================================================
    return {
        # --- CORE EXISTING KEYS (BACKWARD COMPATIBILITY) ---
        "site_settings": site_settings,
        "header_bar": header_bar,
        "header_bar_announcements_json": header_bar_announcements_json,
        "navbar_items": navbar_items,
        "menu_categories": menu_categories,
        "navbar_queryset": navbar_qs,
        "footer_data": footer_data,

        # --- BRANDING & IDENTITY ---
        "brand_title": brand_title,
        "brand_subtitle": brand_subtitle,
        "brand_url": brand_url,
        "brand_logo_url": brand_logo_url,
        "brand_mobile_logo_url": brand_mobile_logo_url,

        # --- EXPANDED HEADER: CONFIGURATION & VISIBILITY ---
        "header_is_enabled": header_settings.get("is_enabled", True),
        "header_is_sticky": header_settings.get("is_sticky", False),
        "header_show_on_desktop": header_settings.get("show_on_desktop", True),
        "header_show_on_mobile": header_settings.get("show_on_mobile", True),
        "header_rotator_interval": header_bar.get("rotator_interval_ms", 4000) if header_bar else 4000,

        # --- EXPANDED HEADER: ANNOUNCEMENTS ---
        "header_announcements": header_bar.get("announcement_messages", []) if header_bar else [],
        
        # --- EXPANDED HEADER: SELECTORS ---
        "header_currencies": header_bar.get("currencies", []) if header_bar else [],
        "header_languages": header_bar.get("languages", []) if header_bar else [],
        "header_currency_label": header_bar.get("currency_label", "") if header_bar else "",
        "header_language_label": header_bar.get("language_label", "") if header_bar else "",
        
        # --- EXPANDED HEADER: UTILITIES & CONTACT INFO ---
        "header_left_utilities": header_left_utils,
        "header_right_utilities": header_right_utils,
        "header_phone_links": header_phone_links,
        "header_email_links": header_email_links,
        "header_store_locator_links": header_store_locator_links,
        "header_account_links": header_account_links,

        # --- EXPANDED NAVBAR: GLOBAL NAVIGATION SETTINGS ---
        "navbar_is_enabled": navbar_settings_data.get("is_enabled", True),
        "navbar_is_sticky": navbar_settings_data.get("is_sticky", True),
        "navbar_desktop_behavior": navbar_settings_data.get("desktop_behavior", "hover"),
        "navbar_mobile_behavior": navbar_settings_data.get("mobile_behavior", "offcanvas"),
    }

def footer_logo(request):
    """
    Retrieves the Singleton FooterSettings object and exposes its logo URL 
    to be rendered automatically across all application templates.
    """
    try:
        # Optimization: Attempt to leverage the highly optimized and cached payload
        # from get_footer_data() first, bypassing the database completely.
        footer_payload = get_footer_data()
        if footer_payload and "brand" in footer_payload:
            logo_url = footer_payload["brand"].get("logo_url")
        else:
            raise ValueError("Footer data cache empty or misconfigured.")
    except Exception:
        # Fallback to direct DB query if cache is cold or structurally invalid
        try:
            settings = FooterSettings.objects.first()
            logo_url = settings.logo.url if settings and settings.logo else None
        except Exception:
            logo_url = None

    return {
        'footer_logo': logo_url
    }