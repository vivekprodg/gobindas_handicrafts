from __future__ import annotations

import json
from typing import Any

from django.core.cache import cache

from apps.catalog.models import Category
from .models import HeaderBar, NavbarItem, NavbarSettings
from .services import (
    build_navbar_tree,
    get_contact_page_data_cached,
    get_footer_data,
    get_header_bar_cached,
    get_site_settings_cached,
)

def foundation_cms_context(request: Any) -> dict[str, Any]:
    site_settings = get_site_settings_cached()
    header_bar = get_header_bar_cached()
    navbar_items = build_navbar_tree()
    footer_data = get_footer_data()
    contact_page_data = get_contact_page_data_cached()

    header_settings = cache.get("foundation:cms:v1:header_settings")
    if header_settings is None:
        hb = HeaderBar.objects.first()
        header_settings = {
            "is_enabled": hb.is_enabled if hb else True,
            "is_sticky": hb.is_sticky if hb else False,
            "show_on_desktop": hb.show_on_desktop if hb else True,
            "show_on_mobile": hb.show_on_mobile if hb else True,
        }
        cache.set("foundation:cms:v1:header_settings", header_settings, 1800)

    navbar_settings_data = cache.get("foundation:cms:v1:navbar_settings")
    if navbar_settings_data is None:
        ns = NavbarSettings.objects.first()
        navbar_settings_data = {
            "is_enabled": ns.is_enabled if ns else True,
            "is_sticky": ns.is_sticky if ns else True,
            "desktop_behavior": ns.desktop_behavior if ns else "hover",
            "mobile_behavior": ns.mobile_behavior if ns else "offcanvas",
        }
        cache.set("foundation:cms:v1:navbar_settings", navbar_settings_data, 1800)

    try:
        menu_categories = Category.objects.filter(show_in_menu=True, is_active=True, parent=None).prefetch_related("children")
    except Exception:
        menu_categories = Category.objects.none()

    header_bar_announcements_json = "[]"
    if header_bar and header_bar.get("announcement_messages"):
        try:
            header_bar_announcements_json = json.dumps(header_bar.get("announcement_messages"))
        except Exception:
            pass

    try:
        navbar_qs = NavbarItem.objects.select_related("parent").order_by("parent_id", "position", "label", "id")
    except Exception:
        navbar_qs = NavbarItem.objects.none()

    header_left_utils = header_bar.get("left_utilities", []) if header_bar else []
    header_right_utils = header_bar.get("right_utilities", []) if header_bar else []
    all_header_utils = header_left_utils + header_right_utils

    return {
        "site_settings": site_settings,
        "header_bar": header_bar,
        "header_bar_announcements_json": header_bar_announcements_json,
        "navbar_items": navbar_items,
        "menu_categories": menu_categories,
        "navbar_queryset": navbar_qs,
        "footer_data": footer_data,
        "contact_page_data": contact_page_data,
        "brand_title": site_settings.brand_title if site_settings else "Gobindas",
        "brand_subtitle": site_settings.brand_subtitle if site_settings else "Handicrafts",
        "brand_url": site_settings.brand_url if site_settings else "/",
        "brand_logo_url": site_settings.logo.url if site_settings and getattr(site_settings, "logo", None) else None,
        "brand_mobile_logo_url": site_settings.mobile_logo.url if site_settings and getattr(site_settings, "mobile_logo", None) else (site_settings.logo.url if site_settings and getattr(site_settings, "logo", None) else None),
        "header_is_enabled": header_settings.get("is_enabled", True),
        "header_is_sticky": header_settings.get("is_sticky", False),
        "header_show_on_desktop": header_settings.get("show_on_desktop", True),
        "header_show_on_mobile": header_settings.get("show_on_mobile", True),
        "header_rotator_interval": header_bar.get("rotator_interval_ms", 4000) if header_bar else 4000,
        "header_announcements": header_bar.get("announcement_messages", []) if header_bar else [],
        "header_currencies": header_bar.get("currencies", []) if header_bar else [],
        "header_languages": header_bar.get("languages", []) if header_bar else [],
        "header_currency_label": header_bar.get("currency_label", "") if header_bar else "",
        "header_language_label": header_bar.get("language_label", "") if header_bar else "",
        "header_left_utilities": header_left_utils,
        "header_right_utilities": header_right_utils,
        "header_phone_links": [u for u in all_header_utils if u.get("icon_key") == "phone"],
        "header_email_links": [u for u in all_header_utils if u.get("icon_key") in ("email", "envelope")],
        "header_store_locator_links": [u for u in all_header_utils if u.get("icon_key") in ("map-pin", "marker")],
        "navbar_is_enabled": navbar_settings_data.get("is_enabled", True),
        "navbar_is_sticky": navbar_settings_data.get("is_sticky", True),
        "navbar_desktop_behavior": navbar_settings_data.get("desktop_behavior", "hover"),
        "navbar_mobile_behavior": navbar_settings_data.get("mobile_behavior", "offcanvas"),
    }

def footer_logo(request: Any) -> dict[str, Any]:
    try:
        footer_payload = get_footer_data()
        logo_url = footer_payload["brand"].get("logo_url") if footer_payload and "brand" in footer_payload else None
    except Exception:
        logo_url = None
    return {"footer_logo": logo_url}