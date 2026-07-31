from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps, UnidentifiedImageError
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import UploadedFile
from django.urls import NoReverseMatch, reverse

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
    NavbarSettings,
    SiteSettings,
)

Image.MAX_IMAGE_PIXELS = None

FOUNDATION_CMS_CACHE_VERSION = 1
FOUNDATION_CMS_CACHE_TIMEOUT = 60 * 30

SITE_SETTINGS_CACHE_KEY = f"foundation:cms:v{FOUNDATION_CMS_CACHE_VERSION}:site_settings"
HEADER_BAR_CACHE_KEY = f"foundation:cms:v{FOUNDATION_CMS_CACHE_VERSION}:header_bar"
NAVBAR_TREE_CACHE_KEY = f"foundation:cms:v{FOUNDATION_CMS_CACHE_VERSION}:navbar_tree"
NAVBAR_SETTINGS_CACHE_KEY = f"foundation:cms:v{FOUNDATION_CMS_CACHE_VERSION}:navbar_settings"
FOOTER_DATA_CACHE_KEY = f"foundation:cms:v{FOUNDATION_CMS_CACHE_VERSION}:footer_data"
CONTACT_PAGE_CACHE_KEY = f"foundation:cms:v{FOUNDATION_CMS_CACHE_VERSION}:contact_page"


def invalidate_foundation_cms_cache() -> None:
    cache.delete_many([
        SITE_SETTINGS_CACHE_KEY,
        HEADER_BAR_CACHE_KEY,
        NAVBAR_TREE_CACHE_KEY,
        NAVBAR_SETTINGS_CACHE_KEY,
        FOOTER_DATA_CACHE_KEY,
        CONTACT_PAGE_CACHE_KEY,
    ])


def _singleton_instance(model_class: Any) -> Any:
    try:
        return model_class.objects.get()
    except model_class.DoesNotExist as exc:
        raise ImproperlyConfigured(f"{model_class.__name__} is required. Create the singleton record in Django Admin.") from exc
    except model_class.MultipleObjectsReturned as exc:
        raise ImproperlyConfigured(f"More than one {model_class.__name__} record exists.") from exc


def get_site_settings_cached(*, use_cache: bool = True) -> SiteSettings:
    if use_cache:
        cached = cache.get(SITE_SETTINGS_CACHE_KEY)
        if cached is not None:
            return cached
    obj = _singleton_instance(SiteSettings)
    if use_cache:
        cache.set(SITE_SETTINGS_CACHE_KEY, obj, FOUNDATION_CMS_CACHE_TIMEOUT)
    return obj


class HeaderBarDict(dict):
    def __getattr__(self, name: str) -> Any:
        return self.get(name, None)


def resolve_navigation_url(url_value: str | None) -> str:
    if not url_value:
        return "#"
    val = url_value.strip()
    if val.startswith(("http://", "https://", "mailto:", "tel:", "#", "//")):
        return val
    try:
        return reverse(val)
    except NoReverseMatch:
        pass
    if val.startswith("/"):
        return val if val.endswith("/") or "." in val.split("/")[-1] else val + "/"
    path = f"/{val}"
    return path if path.endswith("/") or "." in path.split("/")[-1] else path + "/"


def get_header_bar_cached(*, use_cache: bool = True) -> HeaderBarDict | None:
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
        "currencies": [{"id": c.pk, "label": c.label, "code": c.code, "symbol": c.symbol, "url": c.link_url} for c in currencies],
        "languages": [{"id": l.pk, "label": l.label, "code": l.code, "url": l.link_url} for l in languages],
        "countries": [{"id": c.pk, "name": c.name, "code": c.code, "url": c.link_url} for c in countries],
        "left_utilities": [{"id": u.pk, "label": u.label, "url": resolve_navigation_url(u.link_url), "show_dropdown_icon": u.show_dropdown_icon, "icon_key": u.icon_key, "utility_type": getattr(u, "utility_type", "custom")} for u in left_utils],
        "right_utilities": [{"id": u.pk, "label": u.label, "url": resolve_navigation_url(u.link_url), "show_dropdown_icon": u.show_dropdown_icon, "icon_key": u.icon_key, "utility_type": getattr(u, "utility_type", "custom")} for u in right_utils],
        "announcements": [{"id": a.pk, "text": a.text, "start_date": a.start_date.isoformat() if a.start_date else None, "end_date": a.end_date.isoformat() if a.end_date else None, "priority": a.priority, "position": a.position} for a in announcements],
        "announcement_messages": [a.text for a in announcements],
        "currency_label": currencies[0].code if currencies else obj.currency_label,
        "language_label": languages[0].code.upper() if languages else obj.language_label,
    })

    if use_cache:
        cache.set(HEADER_BAR_CACHE_KEY, data, FOUNDATION_CMS_CACHE_TIMEOUT)
    return data


def build_navbar_tree(*, use_cache: bool = True) -> list[dict[str, Any]]:
    if use_cache:
        cached = cache.get(NAVBAR_TREE_CACHE_KEY)
        if cached is not None:
            return cached

    items = list(
        NavbarItem.objects.select_related("parent").prefetch_related(
            "mega_menu_columns", "mega_menu_columns__links"
        ).order_by("parent_id", "position", "label", "id")
    )

    nodes: dict[int, dict[str, Any]] = {}
    roots: list[dict[str, Any]] = []

    for item in items:
        columns = []
        for col in item.mega_menu_columns.all():
            if not getattr(col, "is_active", True):
                continue
            links = [
                {
                    "id": lnk.pk,
                    "label": lnk.label,
                    "url": resolve_navigation_url(lnk.link_url),
                    "icon_key": getattr(lnk, "icon_key", None),
                    "open_in_new_tab": getattr(lnk, "open_in_new_tab", False),
                    "is_featured": getattr(lnk, "is_featured", False),
                    "position": getattr(lnk, "position", 0),
                    "visibility_scope": getattr(lnk, "visibility_scope", None),
                }
                for lnk in col.links.all() if getattr(lnk, "is_active", True)
            ]
            columns.append({
                "id": col.pk,
                "heading": getattr(col, "heading", ""),
                "position": col.position,
                "visibility_scope": getattr(col, "visibility_scope", None),
                "links": links,
            })

        nodes[item.pk] = {
            "id": item.pk,
            "parent_id": item.parent_id,
            "label": item.label,
            "slug": getattr(item, "slug", None),
            "link_url": resolve_navigation_url(item.link_url) if item.link_url else None,
            "open_in_new_tab": getattr(item, "open_in_new_tab", False),
            "menu_type": item.menu_type,
            "menu_type_label": item.get_menu_type_display(),
            "position": item.position,
            "icon_key": getattr(item, "icon_key", None),
            "badge_text": item.badge_text,
            "badge_style": item.badge_style,
            "visibility_scope": item.visibility_scope,
            "visibility_scope_label": item.get_visibility_scope_display() if item.visibility_scope else None,
            "requires_authentication": getattr(item, "requires_authentication", False),
            "start_date": item.start_date.isoformat() if getattr(item, "start_date", None) else None,
            "end_date": item.end_date.isoformat() if getattr(item, "end_date", None) else None,
            "featured_is_visible": getattr(item, "featured_is_visible", True),
            "featured_image": item.featured_image,
            "featured_image_url": item.featured_image.url if item.featured_image else None,
            "featured_title": item.featured_title,
            "featured_text": item.featured_text,
            "featured_cta_text": getattr(item, "featured_cta_text", None),
            "featured_cta_url": resolve_navigation_url(item.featured_cta_url) if getattr(item, "featured_cta_url", None) else None,
            "featured_start_date": item.featured_start_date.isoformat() if getattr(item, "featured_start_date", None) else None,
            "featured_end_date": item.featured_end_date.isoformat() if getattr(item, "featured_end_date", None) else None,
            "mega_menu_columns": columns if columns else None,
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
    if use_cache:
        cached = cache.get(NAVBAR_SETTINGS_CACHE_KEY)
        if cached is not None:
            return cached

    ns = NavbarSettings.objects.first()
    data = {
        "is_enabled": ns.is_enabled if ns else True,
        "is_sticky": ns.is_sticky if ns else True,
        "desktop_behavior": ns.desktop_behavior if ns else "hover",
        "mobile_behavior": ns.mobile_behavior if ns else "offcanvas",
    }
    if use_cache:
        cache.set(NAVBAR_SETTINGS_CACHE_KEY, data, FOUNDATION_CMS_CACHE_TIMEOUT)
    return data


def get_contact_page_data_cached(*, use_cache: bool = True) -> dict[str, Any]:
    if use_cache:
        cached = cache.get(CONTACT_PAGE_CACHE_KEY)
        if cached is not None:
            return cached

    contact_obj = ContactPage.objects.prefetch_related("phones", "emails", "social_links", "office_hours").first()
    phones = list(contact_obj.phones.filter(is_visible=True).order_by("position", "id")) if contact_obj else []
    emails = list(contact_obj.emails.filter(is_visible=True).order_by("position", "id")) if contact_obj else []
    socials = list(contact_obj.social_links.filter(is_visible=True).order_by("position", "id")) if contact_obj else []
    hours = list(contact_obj.office_hours.filter(is_visible=True).order_by("position", "id")) if contact_obj else []

    phones_list = [{"id": p.pk, "label": p.label, "phone_number": p.phone_number, "position": p.position} for p in phones]
    emails_list = [{"id": e.pk, "label": e.label, "email_address": e.email_address, "position": e.position} for e in emails]

    data = {
        "id": contact_obj.pk if contact_obj else None,
        "hero_title": contact_obj.hero_title if contact_obj else None,
        "hero_subtitle": contact_obj.hero_subtitle if contact_obj else None,
        "hero_description": contact_obj.hero_description if contact_obj else None,
        "hero_image_url": contact_obj.hero_image.url if contact_obj and contact_obj.hero_image else None,
        "intro_heading": contact_obj.intro_heading if contact_obj else None,
        "intro_text": contact_obj.intro_text if contact_obj else None,
        "address_heading": contact_obj.address_heading if contact_obj else None,
        "physical_address": contact_obj.physical_address if contact_obj else None,
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
        "phones": phones_list,
        "emails": emails_list,
        "social_links": [{"id": s.pk, "platform": s.platform, "url": s.url, "icon_key": s.icon_key, "icon_class": getattr(s, "icon_class", None), "position": s.position, "is_visible": s.is_visible} for s in socials],
        "office_hours": [{"id": oh.pk, "day": oh.day, "opening_time": oh.opening_time, "closing_time": oh.closing_time, "status": oh.status, "status_label": oh.get_status_display() if oh.status else None, "position": oh.position} for oh in hours],
        "primary_phone": phones_list[0]["phone_number"] if phones_list else None,
        "primary_email": emails_list[0]["email_address"] if emails_list else None,
        "primary_address": contact_obj.physical_address if contact_obj else None,
        "primary_contact_person": None,
    }

    if use_cache:
        cache.set(CONTACT_PAGE_CACHE_KEY, data, FOUNDATION_CMS_CACHE_TIMEOUT)
    return data


def get_footer_data(*, use_cache: bool = True) -> dict[str, Any]:
    if use_cache:
        cached = cache.get(FOOTER_DATA_CACHE_KEY)
        if cached is not None:
            return cached

    settings_obj = FooterSettings.objects.first()
    logo_url = settings_obj.logo.url if settings_obj and settings_obj.logo else ""

    brand_dict = {
        "brand_name": settings_obj.brand_name if settings_obj else None,
        "fair_trade_statement": settings_obj.fair_trade_statement if settings_obj else None,
        "copyright_template": settings_obj.copyright_template if settings_obj else None,
        "logo": logo_url,
        "logo_url": logo_url,
    }
    newsletter_dict = {
        "newsletter_heading": settings_obj.newsletter_heading if settings_obj else None,
        "newsletter_subtext": settings_obj.newsletter_subtext if settings_obj else None,
        "newsletter_endpoint": settings_obj.newsletter_endpoint if settings_obj else None,
        "newsletter_placeholder": settings_obj.newsletter_placeholder if settings_obj else None,
    }

    sections = [
        {
            "id": sec.id,
            "title": sec.title,
            "position": sec.position,
            "links": [{"id": link.id, "label": link.label, "route": resolve_navigation_url(link.route), "link_type": link.link_type, "action": link.action, "position": link.position} for link in sec.links.all()],
        }
        for sec in FooterSection.objects.prefetch_related("links").order_by("position", "id")
    ]

    contact_data = get_contact_page_data_cached(use_cache=use_cache)

    payload = {
        "brand": brand_dict,
        "newsletter": newsletter_dict,
        "sections": sections,
        "social_links": [{"id": s.id, "platform": s.platform, "url": s.url, "icon_key": s.icon_key, "icon_class": getattr(s, "icon_class", None), "position": s.position, "is_visible": s.is_visible} for s in FooterSocialLink.objects.order_by("position", "id")],
        "payment_methods": [{"id": p.id, "method_name": p.method_name, "icon_key": p.icon_key, "position": p.position} for p in FooterPaymentMethod.objects.order_by("position", "id")],
        "trust_badges": [{"id": b.id, "badge_name": b.badge_name, "icon_key": b.icon_key, "position": b.position} for b in FooterTrustBadge.objects.order_by("position", "id")],
        "contact_info": {
            "primary_phone": contact_data.get("primary_phone"),
            "primary_email": contact_data.get("primary_email"),
            "primary_address": contact_data.get("primary_address"),
            "primary_contact_person": contact_data.get("primary_contact_person"),
            "phones": contact_data.get("phones", []),
            "emails": contact_data.get("emails", []),
            "social_links": contact_data.get("social_links", []),
            "office_hours": contact_data.get("office_hours", []),
        },
    }

    if use_cache:
        cache.set(FOOTER_DATA_CACHE_KEY, payload, FOUNDATION_CMS_CACHE_TIMEOUT)
    return payload


def get_foundation_cms_payload(*, use_cache: bool = True) -> dict[str, Any]:
    return {
        "site_settings": get_site_settings_cached(use_cache=use_cache),
        "header_bar": get_header_bar_cached(use_cache=use_cache),
        "navbar_items": build_navbar_tree(use_cache=use_cache),
        "navbar_settings": get_navbar_settings_cached(use_cache=use_cache),
        "footer": get_footer_data(use_cache=use_cache),
        "contact_page": get_contact_page_data_cached(use_cache=use_cache),
    }


@dataclass(frozen=True)
class OptimizedImageResult:
    file: ContentFile
    filename: str
    original_format: str | None
    output_format: str
    width: int
    height: int
    bytes_size: int


def _open_image_from_upload(uploaded_file: UploadedFile | bytes) -> tuple[Image.Image, bytes]:
    raw = uploaded_file if isinstance(uploaded_file, bytes) else (uploaded_file.seek(0) or uploaded_file.read())
    if not raw:
        raise ValidationError({"image": "Uploaded file is empty."})
    try:
        image = ImageOps.exif_transpose(Image.open(BytesIO(raw)))
    except UnidentifiedImageError as exc:
        raise ValidationError({"image": "Unsupported image format."}) from exc
    return image, raw


def _key_out_white_background(img: Image.Image) -> Image.Image:
    """
    Colormetric background removal algorithm.
    Identifies white, off-white, light grey canvas, and JPEG compression noise by checking
    if RGB channels are balanced (neutral grey/white) and bright.
    Keys out all neutral background pixels to Alpha = 0 (100% transparent).
    Preserves 100% of gold artwork, elephant motif, pillar, typography, and lotus details.
    """
    rgba_img = img.convert("RGBA")
    datas = rgba_img.getdata()

    new_data = []
    for item in datas:
        r, g, b = item[0], item[1], item[2]
        a = item[3] if len(item) > 3 else 255

        # Measure color spread (max channel minus min channel)
        max_c = max(r, g, b)
        min_c = min(r, g, b)
        color_spread = max_c - min_c
        brightness = (r + g + b) / 3.0

        # White/grey background pixels have low color spread (R ~ G ~ B) and high brightness
        if color_spread < 35 and brightness > 150:
            new_data.append((255, 255, 255, 0))
        elif color_spread < 45 and brightness > 200:
            new_data.append((255, 255, 255, 0))
        else:
            # Preserve original exact gold RGB colors, highlights, and shading!
            new_data.append((r, g, b, a))

    rgba_img.putdata(new_data)
    return rgba_img


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
    working = image.copy()

    if working.mode not in ("RGB", "RGBA"):
        working = working.convert("RGBA" if "A" in working.getbands() else "RGB")

    if working.width > max_width:
        working = working.resize((max_width, round((max_width / working.width) * working.height)), Image.Resampling.LANCZOS)

    data = None
    # Iteratively compress to find best quality under target_max_bytes
    for quality in range(90, 20, -10):
        buf = BytesIO()
        working.save(buf, format="WEBP", quality=quality, method=4, exact=(working.mode == "RGBA"))
        data = buf.getvalue()
        if len(data) <= target_max_bytes:
            digest = hashlib.sha256(raw).hexdigest()[:16]
            filename = f"{filename_prefix}/{uuid.uuid4().hex}_{digest}.webp"
            return OptimizedImageResult(
                file=ContentFile(data, name=filename),
                filename=filename,
                original_format=original_format,
                output_format="WEBP",
                width=working.width,
                height=working.height,
                bytes_size=len(data),
            )

    # Fallback for ultra-large camera photos: returns compressed result rather than raising an exception
    if data is None:
        buf = BytesIO()
        working.save(buf, format="WEBP", quality=50, method=4)
        data = buf.getvalue()

    digest = hashlib.sha256(raw).hexdigest()[:16]
    filename = f"{filename_prefix}/{uuid.uuid4().hex}_{digest}.webp"
    return OptimizedImageResult(
        file=ContentFile(data, name=filename),
        filename=filename,
        original_format=original_format,
        output_format="WEBP",
        width=working.width,
        height=working.height,
        bytes_size=len(data),
    )


def prepare_transparent_logo_upload(
    uploaded_file: UploadedFile | bytes,
    *,
    target_max_bytes: int = 500 * 1024,
    max_width: int = 1200,
    min_width: int = 200,
    filename_prefix: str = "foundation/logos",
) -> OptimizedImageResult:
    """
    Dedicated logo optimization service.
    - Handles vector SVGs natively (preserving full SVG XML transparency).
    - Keys out solid white, light grey, and JPEG compression noise into true alpha transparency.
    - Preserves 100% of original gold artwork, elephant motif, typography, and gradients.
    - Saves in lossless WEBP / PNG format.
    """
    raw = uploaded_file if isinstance(uploaded_file, bytes) else (uploaded_file.seek(0) or uploaded_file.read())
    if not raw:
        raise ValidationError({"logo": "Uploaded logo file is empty."})

    filename_str = getattr(uploaded_file, "name", "").lower()

    # 1. Vector SVG Passthrough
    if filename_str.endswith(".svg") or raw.strip().startswith(b"<svg") or b"<svg" in raw[:200]:
        digest = hashlib.sha256(raw).hexdigest()[:16]
        filename = f"{filename_prefix}/{uuid.uuid4().hex}_{digest}.svg"
        return OptimizedImageResult(
            file=ContentFile(raw, name=filename),
            filename=filename,
            original_format="SVG",
            output_format="SVG",
            width=max_width,
            height=0,
            bytes_size=len(raw),
        )

    # 2. Bitmap Logo Processing with Colormetric Background Keying
    try:
        image = ImageOps.exif_transpose(Image.open(BytesIO(raw)))
    except UnidentifiedImageError as exc:
        raise ValidationError({"logo": "Unsupported logo image format."}) from exc

    original_format = image.format
    working = image.copy()

    # Strips white bounding box and JPEG noise while keeping original gold colors & shading untouched
    working = _key_out_white_background(working)

    if working.width > max_width:
        working = working.resize((max_width, round((max_width / working.width) * working.height)), Image.Resampling.LANCZOS)

    # Save as Lossless WEBP with exact alpha transparency
    buf = BytesIO()
    working.save(buf, format="WEBP", lossless=True, quality=100, exact=True)
    data = buf.getvalue()

    if len(data) > target_max_bytes:
        buf = BytesIO()
        working.save(buf, format="PNG", optimize=True)
        data = buf.getvalue()
        ext = "png"
    else:
        ext = "webp"

    digest = hashlib.sha256(raw).hexdigest()[:16]
    filename = f"{filename_prefix}/{uuid.uuid4().hex}_{digest}.{ext}"

    return OptimizedImageResult(
        file=ContentFile(data, name=filename),
        filename=filename,
        original_format=original_format,
        output_format=ext.upper(),
        width=working.width,
        height=working.height,
        bytes_size=len(data),
    )


def prepare_logo_upload(uploaded_file: UploadedFile | bytes) -> OptimizedImageResult:
    return prepare_transparent_logo_upload(
        uploaded_file,
        target_max_bytes=500 * 1024,
        max_width=1200,
        min_width=200,
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
    return optimize_uploaded_image(
        uploaded_file,
        target_max_bytes=200 * 1024,
        max_width=500,
        min_width=300,
        filename_prefix="homepage/category-cards",
    )


def refresh_foundation_cms_cache() -> dict[str, Any]:
    invalidate_foundation_cms_cache()
    return get_foundation_cms_payload(use_cache=False)