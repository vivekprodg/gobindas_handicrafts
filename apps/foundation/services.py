from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Iterable

from PIL import Image, ImageOps, UnidentifiedImageError
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import UploadedFile
from django.core.cache import cache
from django.urls import reverse, NoReverseMatch

from .models import (
    HeaderBar,
    HeaderAnnouncement,
    HeaderCurrency,
    HeaderLanguage,
    HeaderCountry,
    HeaderUtilityLink,
    NavbarSettings,
    NavbarItem,
    SiteSettings,
    FooterSettings,
    FooterSection,
    FooterLink,
    FooterSocialLink,
    FooterPaymentMethod,
    FooterTrustBadge,
    ContactPage,
    ContactPhone,
    ContactEmail,
    ContactSocialLink,
    ContactOfficeHour,
)

# Allow large inputs to be processed by the optimization pipeline.
# The service still validates and compresses aggressively before saving.
Image.MAX_IMAGE_PIXELS = None

FOUNDATION_CMS_CACHE_VERSION = 1
FOUNDATION_CMS_CACHE_TIMEOUT = 60 * 30  # 30 minutes

SITE_SETTINGS_CACHE_KEY = f"foundation:cms:v{FOUNDATION_CMS_CACHE_VERSION}:site_settings"
HEADER_BAR_CACHE_KEY = f"foundation:cms:v{FOUNDATION_CMS_CACHE_VERSION}:header_bar"
NAVBAR_TREE_CACHE_KEY = f"foundation:cms:v{FOUNDATION_CMS_CACHE_VERSION}:navbar_tree"
NAVBAR_SETTINGS_CACHE_KEY = f"foundation:cms:v{FOUNDATION_CMS_CACHE_VERSION}:navbar_settings"
FOOTER_DATA_CACHE_KEY = f"foundation:cms:v{FOUNDATION_CMS_CACHE_VERSION}:footer_data"
CONTACT_PAGE_CACHE_KEY = f"foundation:cms:v{FOUNDATION_CMS_CACHE_VERSION}:contact_page"

def _cache_key(base_key: str) -> str:
    return base_key

def invalidate_foundation_cms_cache() -> None:
    """
    Clears all foundation CMS caches.
    Call this from admin save/delete signals.
    """
    cache.delete(SITE_SETTINGS_CACHE_KEY)
    cache.delete(HEADER_BAR_CACHE_KEY)
    cache.delete(NAVBAR_TREE_CACHE_KEY)
    cache.delete(NAVBAR_SETTINGS_CACHE_KEY)
    cache.delete(FOOTER_DATA_CACHE_KEY)
    cache.delete(CONTACT_PAGE_CACHE_KEY)

def _singleton_instance(model_class):
    try:
        return model_class.objects.get()
    except model_class.DoesNotExist as exc:
        raise ImproperlyConfigured(
            f"{model_class.__name__} is required. Create the singleton record in Django Admin first."
        ) from exc
    except model_class.MultipleObjectsReturned as exc:
        raise ImproperlyConfigured(
            f"More than one {model_class.__name__} record exists. Only one singleton record is allowed."
        ) from exc

def get_site_settings_cached(*, use_cache: bool = True) -> SiteSettings:
    """
    Returns the mandatory singleton SiteSettings record.
    The record must exist because the logo is mandatory in your CMS design.
    """
    if use_cache:
        cached = cache.get(SITE_SETTINGS_CACHE_KEY)
        if cached is not None:
            return cached

    obj = _singleton_instance(SiteSettings)

    if use_cache:
        cache.set(SITE_SETTINGS_CACHE_KEY, obj, FOUNDATION_CMS_CACHE_TIMEOUT)

    return obj

class HeaderBarDict(dict):
    """
    A dictionary subclass that allows dot-notation access to its keys.
    This preserves backwards compatibility with existing templates/views
    that access header_bar attributes using dot-notation.
    """
    def __getattr__(self, name):
        if name in self:
            return self[name]
        return None

def get_header_bar_cached(*, use_cache: bool = True) -> HeaderBarDict | None:
    """
    Returns the cached top HeaderBar data package with dot-notation compatibility.
    Serves as the single source of truth for all globally shared Header CMS data.
    """
    if use_cache:
        cached = cache.get(HEADER_BAR_CACHE_KEY)
        if cached is not None:
            return cached

    try:
        obj = HeaderBar.objects.prefetch_related(
            "currencies", "languages", "countries", "announcements", "utilities"
        ).get()
    except HeaderBar.DoesNotExist:
        return None
    except HeaderBar.MultipleObjectsReturned as exc:
        raise ImproperlyConfigured(
            "More than one HeaderBar record exists. Only one singleton record is allowed."
        ) from exc

    # Compile all related items natively from the database with display ordering
    currencies = list(obj.currencies.filter(is_visible=True).order_by("position", "id"))
    languages = list(obj.languages.filter(is_visible=True).order_by("position", "id"))
    countries = list(obj.countries.filter(is_visible=True).order_by("position", "id"))
    announcements = list(obj.announcements.filter(is_visible=True).order_by("-priority", "position", "id"))
    left_utils = list(obj.utilities.filter(is_visible=True, side="left").order_by("position", "id"))
    right_utils = list(obj.utilities.filter(is_visible=True, side="right").order_by("position", "id"))

    data = HeaderBarDict({
        "id": obj.pk,
        "rotator_interval_ms": obj.rotator_interval_ms or 4000,
        "is_enabled": obj.is_enabled,
        "is_sticky": obj.is_sticky,
        "show_on_desktop": obj.show_on_desktop,
        "show_on_mobile": obj.show_on_mobile,
        "currencies": [
            {
                "id": c.pk,
                "label": c.label,
                "code": c.code,
                "symbol": c.symbol,
                "url": c.link_url,
            } for c in currencies
        ],
        "languages": [
            {
                "id": l.pk,
                "label": l.label,
                "code": l.code,
                "url": l.link_url,
            } for l in languages
        ],
        "countries": [
            {
                "id": c.pk,
                "name": c.name,
                "code": c.code,
                "url": c.link_url,
            } for c in countries
        ],
        "left_utilities": [
            {
                "id": u.pk,
                "label": u.label,
                "url": resolve_navigation_url(u.link_url),
                "show_dropdown_icon": u.show_dropdown_icon,
                "icon_key": u.icon_key,
                "utility_type": getattr(u, 'utility_type', 'custom'),
            } for u in left_utils
        ],
        "right_utilities": [
            {
                "id": u.pk,
                "label": u.label,
                "url": resolve_navigation_url(u.link_url),
                "show_dropdown_icon": u.show_dropdown_icon,
                "icon_key": u.icon_key,
                "utility_type": getattr(u, 'utility_type', 'custom'),
            } for u in right_utils
        ],
        "announcements": [
            {
                "id": a.pk,
                "text": a.text,
                "start_date": a.start_date.isoformat() if a.start_date else None,
                "end_date": a.end_date.isoformat() if a.end_date else None,
                "priority": a.priority,
                "position": a.position,
            } for a in announcements
        ],
        # String list fallback for backward compatibility with older campaign rotators:
        "announcement_messages": [a.text for a in announcements],
        # Fallbacks for backwards compatibility:
        "currency_label": currencies[0].code if currencies else obj.currency_label,
        "language_label": languages[0].code.upper() if languages else obj.language_label,
    })

    if use_cache:
        cache.set(HEADER_BAR_CACHE_KEY, data, FOUNDATION_CMS_CACHE_TIMEOUT)

    return data

def resolve_navigation_url(url_value: str | None) -> str:
    """
    Resolves a navigation URL dynamically.
    Supports:
    - Named Django URLs (e.g. 'foundation:store_locator', 'catalog:ceramics')
    - Internal paths (e.g. '/ceramics', '/category/ceramics')
    - External URLs (e.g. 'https://bcorporation.net', 'mailto:', 'tel:')
    """
    if not url_value:
        return "#"
    
    url_value_strip = url_value.strip()
    if (
        url_value_strip.startswith("http://")
        or url_value_strip.startswith("https://")
        or url_value_strip.startswith("mailto:")
        or url_value_strip.startswith("tel:")
        or url_value_strip.startswith("#")
        or url_value_strip.startswith("//")
    ):
        return url_value_strip

    # Named Django URLs (e.g. "foundation:store_locator", "catalog:ceramics")
    try:
        return reverse(url_value_strip)
    except NoReverseMatch:
        pass

    # If reverse matching failed, treat it as an internal path and normalize it.
    if url_value_strip.startswith("/"):
        if not url_value_strip.endswith("/") and not "." in url_value_strip.split("/")[-1]:
            return url_value_strip + "/"
        return url_value_strip
    else:
        path = f"/{url_value_strip}"
        if not path.endswith("/") and not "." in path.split("/")[-1]:
            path += "/"
        return path

def _normalize_menu_type(menu_type: str) -> str:
    allowed = {choice[0] for choice in NavbarItem.MenuType.choices}
    if menu_type not in allowed:
        raise ValidationError({"menu_type": "Invalid menu type."})
    return menu_type

def _normalize_visibility_scope(scope: str | None) -> str | None:
    if scope is None:
        return None
    allowed = {choice[0] for choice in NavbarItem.VisibilityScope.choices}
    if scope not in allowed:
        raise ValidationError({"visibility_scope": "Invalid visibility scope."})
    return scope

def build_navbar_tree(*, use_cache: bool = True) -> list[dict[str, Any]]:
    """
    Builds a nested navbar tree from NavbarItem rows.

    The tree mirrors the current CMS model structure exactly:
    - top-level items
    - nested children
    - optional mega menu payload
    - optional badge fields
    - optional featured media fields
    - authentication and scheduling requirements
    """
    if use_cache:
        cached = cache.get(NAVBAR_TREE_CACHE_KEY)
        if cached is not None:
            return cached

    items = list(
        NavbarItem.objects.select_related("parent").prefetch_related(
            "mega_menu_columns", "mega_menu_columns__links",
        ).order_by(
            "parent_id",
            "position",
            "label",
            "id",
        )
    )

    nodes: dict[int, dict[str, Any]] = {}
    roots: list[dict[str, Any]] = []

    for item in items:
        # Resolve mega menu columns from relational tables
        resolved_mega_menu_columns = []
        for col in item.mega_menu_columns.all():
            if not getattr(col, 'is_active', True):
                continue
                
            links = []
            for lnk in col.links.all():
                if not getattr(lnk, 'is_active', True):
                    continue
                    
                links.append({
                    "id": lnk.pk,
                    "label": lnk.label,
                    "url": resolve_navigation_url(lnk.link_url),
                    "icon_key": getattr(lnk, 'icon_key', None),
                    "open_in_new_tab": getattr(lnk, 'open_in_new_tab', False),
                    "is_featured": getattr(lnk, 'is_featured', False),
                    "position": getattr(lnk, 'position', 0),
                    "visibility_scope": getattr(lnk, 'visibility_scope', None),
                })
                
            resolved_mega_menu_columns.append({
                "id": col.pk,
                "title": getattr(col, 'heading', getattr(col, 'title', '')),  # Fallback for templates using 'title'
                "heading": getattr(col, 'heading', ''),
                "position": col.position,
                "visibility_scope": getattr(col, 'visibility_scope', None),
                "links": links,
            })

        nodes[item.pk] = {
            "id": item.pk,
            "parent_id": item.parent_id,
            "label": item.label,
            "slug": getattr(item, 'slug', None),
            "link_url": resolve_navigation_url(item.link_url) if item.link_url else None,
            "open_in_new_tab": getattr(item, 'open_in_new_tab', False),
            "menu_type": _normalize_menu_type(item.menu_type),
            "menu_type_label": item.get_menu_type_display(),
            "position": item.position,
            "icon_key": getattr(item, 'icon_key', None),
            "badge_text": item.badge_text,
            "badge_style": item.badge_style,
            "visibility_scope": _normalize_visibility_scope(item.visibility_scope),
            "visibility_scope_label": (
                item.get_visibility_scope_display() if item.visibility_scope else None
            ),
            "requires_authentication": getattr(item, 'requires_authentication', False),
            "start_date": item.start_date.isoformat() if getattr(item, 'start_date', None) else None,
            "end_date": item.end_date.isoformat() if getattr(item, 'end_date', None) else None,
            
            # Featured Media Payload
            "featured_is_visible": getattr(item, 'featured_is_visible', True),
            "featured_image": item.featured_image,
            "featured_image_url": item.featured_image.url if item.featured_image else None,
            "featured_title": item.featured_title,
            "featured_text": item.featured_text,
            "featured_cta_text": getattr(item, 'featured_cta_text', None),
            "featured_cta_url": resolve_navigation_url(item.featured_cta_url) if getattr(item, 'featured_cta_url', None) else None,
            "featured_start_date": item.featured_start_date.isoformat() if getattr(item, 'featured_start_date', None) else None,
            "featured_end_date": item.featured_end_date.isoformat() if getattr(item, 'featured_end_date', None) else None,

            "mega_menu_columns": resolved_mega_menu_columns if resolved_mega_menu_columns else None,
            "children": [],
        }

    for item in items:
        node = nodes[item.pk]
        if item.parent_id and item.parent_id in nodes:
            nodes[item.parent_id]["children"].append(node)
        else:
            roots.append(node)

    if use_cache:
        cache.set(NAVBAR_TREE_CACHE_KEY, roots, FOUNDATION_CMS_CACHE_TIMEOUT)

    return roots

def get_navbar_settings_cached(*, use_cache: bool = True) -> dict[str, Any]:
    """
    Retrieves global navigation settings for structural behavior.
    """
    if use_cache:
        cached = cache.get(NAVBAR_SETTINGS_CACHE_KEY)
        if cached is not None:
            return cached
            
    try:
        ns = NavbarSettings.objects.first()
        if ns:
            data = {
                "is_enabled": ns.is_enabled,
                "is_sticky": ns.is_sticky,
                "desktop_behavior": ns.desktop_behavior,
                "mobile_behavior": ns.mobile_behavior,
            }
        else:
            data = {
                "is_enabled": True,
                "is_sticky": True,
                "desktop_behavior": "hover",
                "mobile_behavior": "offcanvas",
            }
    except Exception:
        data = {}

    if use_cache:
        cache.set(NAVBAR_SETTINGS_CACHE_KEY, data, FOUNDATION_CMS_CACHE_TIMEOUT)
        
    return data

# =========================================
# CONTACT PAGE CMS SERVICE IMPLEMENTATION
# =========================================
def get_contact_page_data_cached(*, use_cache: bool = True) -> dict[str, Any]:
    """
    Retrieves, structures, and caches the Contact Page CMS data.
    Acts as the single source of truth for all contact information.
    """
    if use_cache:
        cached = cache.get(CONTACT_PAGE_CACHE_KEY)
        if cached is not None:
            return cached

    try:
        contact_obj = ContactPage.objects.prefetch_related(
            "phones", "emails", "social_links", "office_hours"
        ).get()
    except ContactPage.DoesNotExist:
        contact_obj = None
    except ContactPage.MultipleObjectsReturned as exc:
        raise ImproperlyConfigured(
            "More than one ContactPage record exists. Only one singleton record is allowed."
        ) from exc

    phones_list = []
    emails_list = []
    social_links_list = []
    office_hours_list = []

    if contact_obj:
        phones_qs = contact_obj.phones.filter(is_visible=True).order_by("position", "id")
        emails_qs = contact_obj.emails.filter(is_visible=True).order_by("position", "id")
        social_links_qs = contact_obj.social_links.filter(is_visible=True).order_by("position", "id")
        office_hours_qs = contact_obj.office_hours.filter(is_visible=True).order_by("position", "id")

        phones_list = [
            {
                "id": p.pk,
                "label": p.label,
                "phone_number": p.phone_number,
                "position": p.position,
            } for p in phones_qs
        ]

        emails_list = [
            {
                "id": e.pk,
                "label": e.label,
                "email_address": e.email_address,
                "position": e.position,
            } for e in emails_qs
        ]

        social_links_list = [
            {
                "id": s.pk,
                "platform": s.platform,
                "url": s.url,
                "icon_key": s.icon_key,
                "icon_class": getattr(s, 'icon_class', None),
                "position": s.position,
                "is_visible": s.is_visible,
            } for s in social_links_qs
        ]

        office_hours_list = [
            {
                "id": oh.pk,
                "day": oh.day,
                "opening_time": oh.opening_time,
                "closing_time": oh.closing_time,
                "status": oh.status,
                "status_label": oh.get_status_display() if oh.status else None,
                "position": oh.position,
            } for oh in office_hours_qs
        ]

    # Extract primary contact fields safely
    primary_phone = phones_list[0]["phone_number"] if phones_list else None
    primary_email = emails_list[0]["email_address"] if emails_list else None
    primary_address = contact_obj.physical_address if contact_obj else None
    primary_contact_person = None  # No contact person field defined in ContactPage model.

    data = {
        "id": contact_obj.pk if contact_obj else None,
        "hero_title": contact_obj.hero_title if contact_obj else None,
        "hero_subtitle": contact_obj.hero_subtitle if contact_obj else None,
        "hero_description": contact_obj.hero_description if contact_obj else None,
        "hero_image_url": contact_obj.hero_image.url if contact_obj and contact_obj.hero_image else None,
        "intro_heading": contact_obj.intro_heading if contact_obj else None,
        "intro_text": contact_obj.intro_text if contact_obj else None,
        "address_heading": contact_obj.address_heading if contact_obj else None,
        "physical_address": primary_address,
        "map_heading": contact_obj.map_heading if contact_obj else None,
        "map_embed_url": contact_obj.map_embed_url if contact_obj else None,
        "hours_heading": contact_obj.hours_heading if contact_obj else None,
        "hours_description": contact_obj.hours_description if contact_obj else None,
        "form_heading": contact_obj.form_heading if contact_obj else None,
        "form_subheading": contact_obj.form_subheading if contact_obj else None,
        "form_submit_button_label": contact_obj.form_submit_button_label if contact_obj else None,
        "form_success_message": contact_obj.form_success_message if contact_obj else None,
        "seo_meta_title": contact_obj.seo_meta_title if contact_obj else None,
        "seo_meta_description": contact_obj.seo_meta_description if contact_obj else None,
        "seo_meta_keywords": contact_obj.seo_meta_keywords if contact_obj else None,
        
        # Related structures
        "phones": phones_list,
        "emails": emails_list,
        "social_links": social_links_list,
        "office_hours": office_hours_list,

        # Centralized primary hooks for global consumers
        "primary_phone": primary_phone,
        "primary_email": primary_email,
        "primary_address": primary_address,
        "primary_contact_person": primary_contact_person,
    }

    if use_cache:
        cache.set(CONTACT_PAGE_CACHE_KEY, data, FOUNDATION_CMS_CACHE_TIMEOUT)

    return data

# =========================================
# CMS DYNAMIC FOOTER IMPLEMENTATION
# =========================================
def get_footer_data(*, use_cache: bool = True) -> dict[str, Any]:
    """
    Compiles, structures, and returns the fully dynamic, completely optional 
    and parameterized CMS footer configuration data payload.
    """
    if use_cache:
        cached_data = cache.get(FOOTER_DATA_CACHE_KEY)
        if cached_data is not None:
            return cached_data

    # Safely look up structural singleton options
    settings_obj = FooterSettings.objects.first()
    
    brand_dict = {}
    newsletter_dict = {}
    
    if settings_obj:
        # Evaluate safely to ensure file existence avoids ValueError
        footer_logo_url = settings_obj.logo.url if bool(settings_obj.logo) else ""
        
        brand_dict = {
            "brand_name": settings_obj.brand_name,
            "fair_trade_statement": settings_obj.fair_trade_statement,
            "copyright_template": settings_obj.copyright_template,
            "logo": footer_logo_url,
            "logo_url": footer_logo_url, # Included to align with both potential template calls
        }
        newsletter_dict = {
            "newsletter_heading": settings_obj.newsletter_heading,
            "newsletter_subtext": settings_obj.newsletter_subtext,
            "newsletter_endpoint": settings_obj.newsletter_endpoint,
            "newsletter_placeholder": settings_obj.newsletter_placeholder,
        }

    # Compile link layout grid columns maps
    sections_list = []
    sections_queryset = FooterSection.objects.prefetch_related("links").order_by("position", "id")
    
    for section in sections_queryset:
        links_data = [
            {
                "id": link.id,
                "label": link.label,
                "route": resolve_navigation_url(link.route),
                "link_type": link.link_type,
                "action": link.action,
                "position": link.position,
            }
            for link in section.links.all()
        ]
        sections_list.append({
            "id": section.id,
            "title": section.title,
            "position": section.position,
            "links": links_data,
        })

    # Compile external social network tracking links
    social_links_list = [
        {
            "id": social.id,
            "platform": social.platform,
            "url": social.url,
            "icon_key": social.icon_key,
            "icon_class": getattr(social, 'icon_class', None),
            "position": social.position,
            "is_visible": social.is_visible,
        }
        for social in FooterSocialLink.objects.all().order_by("position", "id")
    ]

    # Compile clearance billing payment options
    payment_methods_list = [
        {
            "id": payment.id,
            "method_name": payment.method_name,
            "icon_key": payment.icon_key,
            "position": payment.position,
        }
        for payment in FooterPaymentMethod.objects.all().order_by("position", "id")
    ]

    # Compile dynamic trust certifications metrics
    trust_badges_list = [
        {
            "id": badge.id,
            "badge_name": badge.badge_name,
            "icon_key": badge.icon_key,
            "position": badge.position,
        }
        for badge in FooterTrustBadge.objects.all().order_by("position", "id")
    ]

    # Sourced directly from Contact Page CMS as single source of truth
    contact_data = get_contact_page_data_cached(use_cache=use_cache)

    result_payload = {
        "brand": brand_dict,
        "newsletter": newsletter_dict,
        "sections": sections_list,
        "social_links": social_links_list,
        "payment_methods": payment_methods_list,
        "trust_badges": trust_badges_list,
        # Contact CMS values exposed in footer contexts
        "contact_info": {
            "primary_phone": contact_data.get("primary_phone"),
            "primary_email": contact_data.get("primary_email"),
            "primary_address": contact_data.get("primary_address"),
            "primary_contact_person": contact_data.get("primary_contact_person"),
            "phones": contact_data.get("phones", []),
            "emails": contact_data.get("emails", []),
            "social_links": contact_data.get("social_links", []),
            "office_hours": contact_data.get("office_hours", []),
        }
    }

    if use_cache:
        cache.set(FOOTER_DATA_CACHE_KEY, result_payload, FOUNDATION_CMS_CACHE_TIMEOUT)

    return result_payload

def get_foundation_cms_payload(*, use_cache: bool = True) -> dict[str, Any]:
    """
    Returns the full CMS payload for the foundation frontend.
    Use this when a template or JavaScript payload needs all CMS data at once.
    """
    return {
        "site_settings": get_site_settings_cached(use_cache=use_cache),
        "header_bar": get_header_bar_cached(use_cache=use_cache),
        "navbar_items": build_navbar_tree(use_cache=use_cache),
        "navbar_settings": get_navbar_settings_cached(use_cache=use_cache),
        "footer": get_footer_data(use_cache=use_cache),
        "contact_page": get_contact_page_data_cached(use_cache=use_cache),
    }

def filter_navbar_tree_by_scope(
    navbar_tree: list[dict[str, Any]],
    *,
    scope: str | None,
) -> list[dict[str, Any]]:
    """
    Optional helper for device-specific rendering.
    Scope values must match NavbarItem.VisibilityScope:
    - all
    - desktop
    - mobile

    This function does not invent defaults. If scope is None, the tree is returned unchanged.
    """
    normalized_scope = _normalize_visibility_scope(scope)
    if normalized_scope is None:
        return navbar_tree

    def _allowed(node: dict[str, Any]) -> bool:
        node_scope = node.get("visibility_scope")
        return node_scope in (None, NavbarItem.VisibilityScope.ALL, normalized_scope)

    def _filter_nodes(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for node in nodes:
            if not _allowed(node):
                continue
            filtered = dict(node)
            filtered["children"] = _filter_nodes(node.get("children", []))
            result.append(filtered)
        return result

    return _filter_nodes(navbar_tree)

@dataclass(frozen=True)
class OptimizedImageResult:
    file: ContentFile
    filename: str
    original_format: str | None
    output_format: str
    width: int
    height: int
    bytes_size: int

def _content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def _open_image_from_upload(uploaded_file: UploadedFile | bytes) -> tuple[Image.Image, bytes]:
    if isinstance(uploaded_file, bytes):
        raw = uploaded_file
    else:
        uploaded_file.seek(0)
        raw = uploaded_file.read()

    if not raw:
        raise ValidationError({"image": "Uploaded file is empty."})

    try:
        image = Image.open(BytesIO(raw))
        image = ImageOps.exif_transpose(image)
    except UnidentifiedImageError as exc:
        raise ValidationError({"image": "Unsupported or unreadable image format."}) from exc

    return image, raw

def _convert_to_webp_bytes(
    image: Image.Image,
    *,
    max_bytes: int,
    max_width: int,
    min_width: int,
    initial_quality: int = 95,
    quality_floor: int = 40,
) -> tuple[bytes, int, int]:
    """
    Converts an image to WEBP and compresses it to be at or under max_bytes.

    Strategy:
    1) Preserve dimensions initially.
    2) Reduce quality step-by-step.
    3) If still too large, reduce dimensions gradually.
    4) Raise ValidationError if it still cannot satisfy the size target.
    """
    working = image.copy()

    if working.mode not in ("RGB", "RGBA"):
        if "A" in working.getbands():
            working = working.convert("RGBA")
        else:
            working = working.convert("RGB")

    if working.width > max_width:
        new_height = round((max_width / working.width) * working.height)
        working = working.resize((max_width, new_height), Image.Resampling.LANCZOS)

    quality_steps = list(range(initial_quality, quality_floor - 1, -5))

    while True:
        for quality in quality_steps:
            buffer = BytesIO()
            working.save(
                buffer,
                format="WEBP",
                quality=quality,
                method=6,
                exact=True if working.mode == "RGBA" else False,
            )
            data = buffer.getvalue()

            if len(data) <= max_bytes:
                return data, working.width, working.height

        if working.width <= min_width:
            break

        next_width = max(min_width, int(working.width * 0.85))
        if next_width >= working.width:
            break

        next_height = round((next_width / working.width) * working.height)
        working = working.resize((next_width, next_height), Image.Resampling.LANCZOS)

    raise ValidationError(
        {
            "image": (
                "The uploaded image could not be optimized to the required size limit "
                "without exceeding the allowed compression thresholds."
            )
        }
    )

def optimize_uploaded_image(
    uploaded_file: UploadedFile | bytes,
    *,
    target_max_bytes: int = 500 * 1024,
    max_width: int = 2000,
    min_width: int = 640,
    filename_prefix: str = "foundation",
) -> OptimizedImageResult:
    image, raw = _open_image_from_upload(uploaded_file)
    original_format = image.format
    optimized_bytes, width, height = _convert_to_webp_bytes(
        image,
        max_bytes=target_max_bytes,
        max_width=max_width,
        min_width=min_width,
    )

    digest = _content_hash(raw)[:16]
    filename = f"{filename_prefix}/{uuid.uuid4().hex}_{digest}.webp"

    content_file = ContentFile(optimized_bytes, name=filename)

    return OptimizedImageResult(
        file=content_file,
        filename=filename,
        original_format=original_format,
        output_format="WEBP",
        width=width,
        height=height,
        bytes_size=len(optimized_bytes),
    )

def prepare_logo_upload(uploaded_file: UploadedFile | bytes) -> OptimizedImageResult:
    return optimize_uploaded_image(
        uploaded_file,
        target_max_bytes=500 * 1024,
        max_width=2000,
        min_width=640,
        filename_prefix="foundation/site-settings/logo",
    )

def prepare_navbar_media_upload(uploaded_file: UploadedFile | bytes) -> OptimizedImageResult:
    return optimize_uploaded_image(
        uploaded_file,
        target_max_bytes=500 * 1024,
        max_width=2000,
        min_width=640,
        filename_prefix="foundation/navbar/media",
    )

def prepare_category_card_upload(uploaded_file: UploadedFile | bytes) -> OptimizedImageResult:
    """
    Tighter constraints for category cards to prevent massive rendering.
    Max width 500px is sufficient for retina-ready cards.
    """
    return optimize_uploaded_image(
        uploaded_file,
        target_max_bytes=200 * 1024, # 200KB limit for cards
        max_width=500,               # Constrain to 500px instead of 2000px
        min_width=300,
        filename_prefix="homepage/category-cards",
    )

def cms_image_upload_name(prefix: str, original_filename: str) -> str:
    suffix = Path(original_filename).suffix.lower() or ".webp"
    return f"{prefix}/{uuid.uuid4().hex}{suffix}"

def warm_foundation_cms_cache() -> dict[str, Any]:
    payload = get_foundation_cms_payload(use_cache=False)
    cache.set(SITE_SETTINGS_CACHE_KEY, payload["site_settings"], FOUNDATION_CMS_CACHE_TIMEOUT)
    cache.set(HEADER_BAR_CACHE_KEY, payload["header_bar"], FOUNDATION_CMS_CACHE_TIMEOUT)
    cache.set(NAVBAR_TREE_CACHE_KEY, payload["navbar_items"], FOUNDATION_CMS_CACHE_TIMEOUT)
    cache.set(NAVBAR_SETTINGS_CACHE_KEY, payload["navbar_settings"], FOUNDATION_CMS_CACHE_TIMEOUT)
    cache.set(FOOTER_DATA_CACHE_KEY, payload["footer"], FOUNDATION_CMS_CACHE_TIMEOUT)
    cache.set(CONTACT_PAGE_CACHE_KEY, payload["contact_page"], FOUNDATION_CMS_CACHE_TIMEOUT)
    return payload

def refresh_foundation_cms_cache() -> dict[str, Any]:
    invalidate_foundation_cms_cache()
    return warm_foundation_cms_cache()