from __future__ import annotations

import io
import uuid
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import models
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _


def _upload_to_site_logo(instance, filename: str) -> str:
    suffix = Path(filename).suffix.lower() or ".png"
    return f"foundation/site-settings/logo/{uuid.uuid4().hex}{suffix}"


def _upload_to_navbar_media(instance, filename: str) -> str:
    suffix = Path(filename).suffix.lower() or ".png"
    return f"foundation/navbar/media/{uuid.uuid4().hex}{suffix}"


def _upload_to_contact_media(instance, filename: str) -> str:
    suffix = Path(filename).suffix.lower() or ".png"
    return f"foundation/contact/media/{uuid.uuid4().hex}{suffix}"


def _upload_to_qr_codes(instance, filename: str) -> str:
    return f"foundation/qr_codes/{instance.slug}.png"


def _upload_to_card_avatars(instance, filename: str) -> str:
    suffix = Path(filename).suffix.lower() or ".jpg"
    return f"foundation/card_avatars/{instance.slug}/{uuid.uuid4().hex}{suffix}"


def _validate_json_structure(value: tuple | list | dict | None, field_name: str, expected_types: tuple[type, ...]) -> None:
    if value is None:
        return
    if not isinstance(value, expected_types):
        expected = ", ".join(t.__name__ for t in expected_types)
        raise ValidationError({field_name: f"Expected {expected}."})

class CMSBaseModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

class SingletonCMSModel(CMSBaseModel):
    class Meta:
        abstract = True

    def clean(self):
        super().clean()
        qs = self.__class__.objects.all()
        if self.pk:
            qs = qs.exclude(pk=self.pk)
        if qs.exists():
            raise ValidationError(
                {
                    "__all__": (
                        f"Only one {self._meta.verbose_name} record is allowed. "
                        f"Edit the existing record instead of creating a new one."
                    )
                }
            )

class SiteSettings(SingletonCMSModel):
    logo = models.ImageField(
        upload_to=_upload_to_site_logo,
        verbose_name="Logo",
        help_text="Mandatory site logo upload.",
    )
    mobile_logo = models.ImageField(
        upload_to=_upload_to_site_logo,
        blank=True,
        null=True,
        verbose_name="Mobile Logo",
        help_text="Optional separate logo optimized for mobile devices.",
    )
    logo_link = models.CharField(
        max_length=500,
        blank=True,
        null=True,
        default="/",
        verbose_name="Logo Link URL",
    )
    logo_alt_text = models.CharField(
        max_length=160,
        blank=True,
        null=True,
        verbose_name="Logo Alt Text",
    )
    brand_title = models.CharField(
        max_length=120,
        blank=True,
        null=True,
        verbose_name="Brand Title",
    )
    brand_subtitle = models.CharField(
        max_length=120,
        blank=True,
        null=True,
        verbose_name="Brand Subtitle",
    )
    brand_url = models.URLField(
        blank=True,
        null=True,
        verbose_name="Brand Link",
    )

    # Email & Notification Configurations
    company_notification_email = models.EmailField(
        blank=True,
        null=True,
        default="admin@gobindashandicrafts.com",
        verbose_name="Company / Owner Notification Email ID",
        help_text="Email address where new registration alerts and order notifications are delivered.",
    )
    sender_email_address = models.EmailField(
        blank=True,
        null=True,
        default="noreply@gobindashandicrafts.com",
        verbose_name="System Sender Email Address (From)",
        help_text="Automated 'From' email address used for customer communications.",
    )
    sender_display_name = models.CharField(
        max_length=120,
        default="Gobindas Handicrafts",
        blank=True,
        null=True,
        verbose_name="Sender Display Name",
        help_text="Friendly sender name displayed in customer inboxes.",
    )

    search_placeholder = models.CharField(
        max_length=255,
        default="Find artisan rugs...",
        blank=True,
        null=True,
        verbose_name="Search Placeholder",
    )
    search_button_label = models.CharField(
        max_length=120,
        default="Search",
        blank=True,
        null=True,
        verbose_name="Search Button Label",
    )
    cart_button_label = models.CharField(
        max_length=120,
        default="Shopping Bag",
        blank=True,
        null=True,
        verbose_name="Cart Button Label",
    )
    cart_badge_count = models.PositiveIntegerField(
        default=2,
        blank=True,
        null=True,
        verbose_name="Cart Badge Count",
    )
    default_featured_image = models.ImageField(
        upload_to=_upload_to_navbar_media,
        blank=True,
        null=True,
        verbose_name="Default Mega Menu Featured Image",
    )
    default_featured_title = models.CharField(
        max_length=160,
        default="Meet the Artisans",
        blank=True,
        null=True,
        verbose_name="Default Mega Menu Featured Title",
    )
    default_featured_text = models.TextField(
        default="Explore the workshop lineages of India.",
        blank=True,
        null=True,
        verbose_name="Default Mega Menu Featured Text",
    )
    enable_customer_registration = models.BooleanField(
        default=True,
        verbose_name="Enable Customer Registration",
    )
    enable_guest_checkout = models.BooleanField(
        default=True,
        verbose_name="Enable Guest Checkout",
    )
    enable_social_login = models.BooleanField(
        default=True,
        verbose_name="Enable Social Login",
    )
    enable_google_login = models.BooleanField(
        default=True,
        verbose_name="Enable Google Login",
    )
    enable_facebook_login = models.BooleanField(
        default=True,
        verbose_name="Enable Facebook Login",
    )
    enable_github_login = models.BooleanField(
        default=False,
        verbose_name="Enable GitHub Login",
    )

    class Meta:
        verbose_name = "Site Settings"
        verbose_name_plural = "Site Settings"
        ordering = ["-updated_at"]

    def clean(self):
        super().clean()
        if not self.logo:
            raise ValidationError({"logo": "Logo upload is required."})

    def save(self, *args: Any, **kwargs: Any):
        if self.logo:
            from .services import prepare_transparent_logo_upload
            try:
                result = prepare_transparent_logo_upload(
                    self.logo.file,
                    target_max_bytes=500 * 1024,
                    max_width=1200,
                    min_width=200,
                    filename_prefix="foundation/site-settings/logo",
                )
                self.logo.save(result.filename, result.file, save=False)
            except Exception:
                pass

        if self.mobile_logo:
            from .services import prepare_transparent_logo_upload
            try:
                result = prepare_transparent_logo_upload(
                    self.mobile_logo.file,
                    target_max_bytes=500 * 1024,
                    max_width=1200,
                    min_width=200,
                    filename_prefix="foundation/site-settings/logo",
                )
                self.mobile_logo.save(result.filename, result.file, save=False)
            except Exception:
                pass

        if self.default_featured_image and not getattr(self.default_featured_image, "_committed", True):
            from .services import prepare_navbar_media_upload
            try:
                result = prepare_navbar_media_upload(self.default_featured_image.file)
                self.default_featured_image.save(result.filename, result.file, save=False)
            except Exception:
                pass

        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return "Site Settings"

class HeaderBar(SingletonCMSModel):
    is_enabled = models.BooleanField(default=True, verbose_name="Enable Header Bar")
    is_sticky = models.BooleanField(default=False, verbose_name="Sticky Header Bar")
    show_on_desktop = models.BooleanField(default=True, verbose_name="Show on Desktop Devices")
    show_on_mobile = models.BooleanField(default=True, verbose_name="Show on Mobile Devices")
    currency_label = models.CharField(max_length=20, blank=True, null=True, verbose_name="Currency Label")
    language_label = models.CharField(max_length=20, blank=True, null=True, verbose_name="Language Label")
    announcement_messages = models.JSONField(blank=True, null=True, verbose_name="Announcement Messages")
    rotator_interval_ms = models.PositiveIntegerField(blank=True, null=True, verbose_name="Rotator Interval (ms)")
    left_utilities = models.JSONField(blank=True, null=True, verbose_name="Left Utility Items")
    right_utilities = models.JSONField(blank=True, null=True, verbose_name="Right Utility Items")

    class Meta:
        verbose_name = "Header Bar"
        verbose_name_plural = "Header Bar"
        ordering = ["-updated_at"]

    def clean(self):
        super().clean()
        _validate_json_structure(self.announcement_messages, "announcement_messages", (list,))
        _validate_json_structure(self.left_utilities, "left_utilities", (list,))
        _validate_json_structure(self.right_utilities, "right_utilities", (list,))

        if self.rotator_interval_ms is not None and self.rotator_interval_ms <= 0:
            raise ValidationError({"rotator_interval_ms": "Rotator interval must be greater than 0."})

    def __str__(self) -> str:
        return "Header Bar"

class HeaderAnnouncement(CMSBaseModel):
    header_bar = models.ForeignKey(HeaderBar, related_name="announcements", on_delete=models.CASCADE, verbose_name="Header Bar Settings")
    text = models.CharField(max_length=255, verbose_name="Announcement Text")
    start_date = models.DateTimeField(blank=True, null=True, verbose_name="Activation Start Date")
    end_date = models.DateTimeField(blank=True, null=True, verbose_name="Activation End Date")
    priority = models.PositiveIntegerField(default=0, verbose_name="Priority Sequence")
    position = models.PositiveIntegerField(default=0, verbose_name="Display Order Position")
    is_visible = models.BooleanField(default=True, verbose_name="Is Visible")

    class Meta:
        verbose_name = "Header Announcement"
        verbose_name_plural = "Header Announcements"
        ordering = ["position", "id"]

    def __str__(self) -> str:
        return self.text

class HeaderCurrency(CMSBaseModel):
    header_bar = models.ForeignKey(HeaderBar, related_name="currencies", on_delete=models.CASCADE, verbose_name="Header Bar Settings")
    label = models.CharField(max_length=50, verbose_name="Currency Name / Label")
    code = models.CharField(max_length=10, verbose_name="Currency Code")
    symbol = models.CharField(max_length=10, blank=True, null=True, verbose_name="Currency Symbol")
    link_url = models.CharField(max_length=500, blank=True, null=True, verbose_name="Change Currency URL / Path")
    position = models.PositiveIntegerField(default=0, verbose_name="Display Order Position")
    is_visible = models.BooleanField(default=True, verbose_name="Is Visible")

    class Meta:
        verbose_name = "Header Currency"
        verbose_name_plural = "Header Currencies"
        ordering = ["position", "id"]

    def __str__(self) -> str:
        return f"{self.code} ({self.label})"

class HeaderLanguage(CMSBaseModel):
    header_bar = models.ForeignKey(HeaderBar, related_name="languages", on_delete=models.CASCADE, verbose_name="Header Bar Settings")
    label = models.CharField(max_length=50, verbose_name="Language Name / Label")
    code = models.CharField(max_length=10, verbose_name="Language Code")
    link_url = models.CharField(max_length=500, blank=True, null=True, verbose_name="Change Language URL / Path")
    position = models.PositiveIntegerField(default=0, verbose_name="Display Order Position")
    is_visible = models.BooleanField(default=True, verbose_name="Is Visible")

    class Meta:
        verbose_name = "Header Language"
        verbose_name_plural = "Header Languages"
        ordering = ["position", "id"]

    def __str__(self) -> str:
        return f"{self.code.upper()} ({self.label})"

class HeaderCountry(CMSBaseModel):
    header_bar = models.ForeignKey(HeaderBar, related_name="countries", on_delete=models.CASCADE, verbose_name="Header Bar Settings")
    name = models.CharField(max_length=100, verbose_name="Country Name")
    code = models.CharField(max_length=10, verbose_name="Country Code")
    link_url = models.CharField(max_length=500, blank=True, null=True, verbose_name="Change Country URL / Path")
    position = models.PositiveIntegerField(default=0, verbose_name="Display Order Position")
    is_visible = models.BooleanField(default=True, verbose_name="Is Visible")

    class Meta:
        verbose_name = "Header Country"
        verbose_name_plural = "Header Countries"
        ordering = ["position", "id"]

    def __str__(self) -> str:
        return f"{self.name} ({self.code.upper()})"

class HeaderUtilityLink(CMSBaseModel):
    class UtilityType(models.TextChoices):
        CUSTOM = "custom", "Custom Link"
        PHONE = "phone", "Phone Number"
        EMAIL = "email", "Email Address"
        STORE_LOCATOR = "store_locator", "Store Locator"
        ACCOUNT = "account", "Account Link"

    header_bar = models.ForeignKey(HeaderBar, related_name="utilities", on_delete=models.CASCADE, verbose_name="Header Bar Settings")
    utility_type = models.CharField(max_length=20, choices=UtilityType.choices, default=UtilityType.CUSTOM, verbose_name="Utility Type")
    label = models.CharField(max_length=120, verbose_name="Link Label / Text")
    link_url = models.CharField(max_length=500, blank=True, null=True, verbose_name="Link Target URL / Path")
    side = models.CharField(max_length=10, choices=[("left", "Left Side"), ("right", "Right Side")], default="left", verbose_name="Header Placement Side")
    icon_key = models.CharField(max_length=50, blank=True, null=True, verbose_name="Icon Library Key")
    show_dropdown_icon = models.BooleanField(default=False, verbose_name="Show Dropdown Caret")
    position = models.PositiveIntegerField(default=0, verbose_name="Display Order Position")
    is_visible = models.BooleanField(default=True, verbose_name="Is Visible")

    class Meta:
        verbose_name = "Header Utility Link"
        verbose_name_plural = "Header Utility Links"
        ordering = ["position", "id"]

    def __str__(self) -> str:
        return f"[{self.side.upper()}] {self.label}"

class NavbarSettings(SingletonCMSModel):
    is_enabled = models.BooleanField(default=True, verbose_name="Enable Main Navigation")
    is_sticky = models.BooleanField(default=True, verbose_name="Sticky Navigation")
    desktop_behavior = models.CharField(max_length=50, choices=[("hover", "Open on Hover"), ("click", "Open on Click")], default="hover", verbose_name="Desktop Navigation Behavior")
    mobile_behavior = models.CharField(max_length=50, choices=[("accordion", "Accordion Menu"), ("offcanvas", "Offcanvas Menu")], default="offcanvas", verbose_name="Mobile Navigation Behavior")

    class Meta:
        verbose_name = "Navbar Settings"
        verbose_name_plural = "Navbar Settings"

    def __str__(self) -> str:
        return "Global Navbar Settings"

class NavbarItem(CMSBaseModel):
    class MenuType(models.TextChoices):
        LINK = "link", "Link"
        DROPDOWN = "dropdown", "Dropdown"
        MEGA_MENU = "mega_menu", "Mega Menu"

    class VisibilityScope(models.TextChoices):
        ALL = "all", "All Devices"
        DESKTOP = "desktop", "Desktop Only"
        MOBILE = "mobile", "Mobile Only"

    parent = models.ForeignKey("self", related_name="children", on_delete=models.CASCADE, blank=True, null=True, verbose_name="Parent Item")
    label = models.CharField(max_length=120, verbose_name="Label")
    slug = models.SlugField(max_length=120, blank=True, null=True, verbose_name="Slug")
    link_url = models.CharField(max_length=500, blank=True, null=True, verbose_name="Link URL")
    open_in_new_tab = models.BooleanField(default=False, verbose_name="Open in New Tab")
    menu_type = models.CharField(max_length=20, choices=MenuType.choices, verbose_name="Menu Type")
    position = models.PositiveIntegerField(blank=True, null=True, verbose_name="Display Order")
    icon_key = models.CharField(max_length=50, blank=True, null=True, verbose_name="Icon Key")
    badge_text = models.CharField(max_length=50, blank=True, null=True, verbose_name="Badge Text")
    badge_style = models.CharField(max_length=50, blank=True, null=True, verbose_name="Badge Style")
    requires_authentication = models.BooleanField(default=False, verbose_name="Requires Authentication")
    visibility_scope = models.CharField(max_length=20, choices=VisibilityScope.choices, blank=True, null=True, verbose_name="Visibility Scope")
    start_date = models.DateTimeField(blank=True, null=True, verbose_name="Activation Start Date")
    end_date = models.DateTimeField(blank=True, null=True, verbose_name="Activation End Date")

    featured_is_visible = models.BooleanField(default=True, verbose_name="Featured Media Block Visibility")
    featured_image = models.ImageField(upload_to=_upload_to_navbar_media, blank=True, null=True, verbose_name="Featured Image")
    featured_title = models.CharField(max_length=160, blank=True, null=True, verbose_name="Featured Title")
    featured_text = models.TextField(blank=True, null=True, verbose_name="Featured Text")
    featured_cta_text = models.CharField(max_length=120, blank=True, null=True, verbose_name="Featured CTA Text")
    featured_cta_url = models.CharField(max_length=500, blank=True, null=True, verbose_name="Featured CTA URL")
    featured_start_date = models.DateTimeField(blank=True, null=True, verbose_name="Featured Media Start Date")
    featured_end_date = models.DateTimeField(blank=True, null=True, verbose_name="Featured Media End Date")

    class Meta:
        verbose_name = "Navbar Item"
        verbose_name_plural = "Navbar Items"
        ordering = ["position", "label", "id"]
        constraints = [models.UniqueConstraint(fields=["parent", "label"], name="uniq_navbar_item_parent_label")]
        indexes = [models.Index(fields=["parent", "position"]), models.Index(fields=["menu_type"])]

    def clean(self):
        super().clean()
        if self.visibility_scope is not None and self.visibility_scope not in self.VisibilityScope.values:
            raise ValidationError({"visibility_scope": "Invalid visibility scope."})

    def save(self, *args, **kwargs):
        if self.featured_image and not getattr(self.featured_image, "_committed", True):
            from .services import prepare_navbar_media_upload
            try:
                result = prepare_navbar_media_upload(self.featured_image.file)
                self.featured_image.save(result.filename, result.file, save=False)
            except Exception:
                pass
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return self.label

class NavbarMegaMenuColumn(CMSBaseModel):
    parent_item = models.ForeignKey(NavbarItem, related_name="mega_menu_columns", on_delete=models.CASCADE, verbose_name="Parent Navbar Item")
    heading = models.CharField(max_length=120, blank=True, null=True, verbose_name="Column Heading")
    position = models.PositiveIntegerField(default=0, verbose_name="Display Order")
    visibility_scope = models.CharField(max_length=20, choices=NavbarItem.VisibilityScope.choices, blank=True, null=True, verbose_name="Visibility Scope")
    is_active = models.BooleanField(default=True, verbose_name="Is Active")

    class Meta:
        verbose_name = "Mega Menu Column"
        verbose_name_plural = "Mega Menu Columns"
        ordering = ["position", "id"]

    def __str__(self) -> str:
        return f"{self.heading or 'Untitled Column'} (Parent: {self.parent_item.label})"

class NavbarMegaMenuLink(CMSBaseModel):
    parent_column = models.ForeignKey(NavbarMegaMenuColumn, related_name="links", on_delete=models.CASCADE, verbose_name="Parent Column")
    label = models.CharField(max_length=120, verbose_name="Link Label")
    link_url = models.CharField(max_length=500, blank=True, null=True, verbose_name="Link URL")
    icon_key = models.CharField(max_length=50, blank=True, null=True, verbose_name="Icon Key")
    open_in_new_tab = models.BooleanField(default=False, verbose_name="Open in New Tab")
    is_featured = models.BooleanField(default=False, verbose_name="Is Featured")
    position = models.PositiveIntegerField(default=0, verbose_name="Display Order")
    visibility_scope = models.CharField(max_length=20, choices=NavbarItem.VisibilityScope.choices, blank=True, null=True, verbose_name="Visibility Scope")
    is_active = models.BooleanField(default=True, verbose_name="Is Active")

    class Meta:
        verbose_name = "Mega Menu Link"
        verbose_name_plural = "Mega Menu Links"
        ordering = ["position", "id"]

    def __str__(self) -> str:
        return self.label

class FooterSettings(SingletonCMSModel):
    logo = models.ImageField(upload_to="footer/", blank=True, null=True, verbose_name="Footer Logo")
    brand_name = models.CharField(max_length=120, blank=True, null=True, verbose_name="Brand Name")
    fair_trade_statement = models.TextField(blank=True, null=True, verbose_name="Fair Trade / Trust Statement")
    newsletter_heading = models.CharField(max_length=120, blank=True, null=True, verbose_name="Newsletter Heading")
    newsletter_subtext = models.TextField(blank=True, null=True, verbose_name="Newsletter Subtext")
    newsletter_endpoint = models.CharField(max_length=500, blank=True, null=True, verbose_name="Newsletter Submission Endpoint")
    newsletter_placeholder = models.CharField(max_length=120, blank=True, null=True, verbose_name="Newsletter Input Placeholder")
    copyright_template = models.CharField(max_length=255, blank=True, null=True, verbose_name="Copyright Template")

    class Meta:
        verbose_name = "Footer Settings"
        verbose_name_plural = "Footer Settings"

    def save(self, *args: Any, **kwargs: Any):
        if self.logo:
            from .services import prepare_transparent_logo_upload
            try:
                result = prepare_transparent_logo_upload(
                    self.logo.file,
                    target_max_bytes=500 * 1024,
                    max_width=1200,
                    min_width=200,
                    filename_prefix="foundation/footer/logo",
                )
                self.logo.save(result.filename, result.file, save=False)
            except Exception:
                pass
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return "Global Footer Settings"

class FooterSection(CMSBaseModel):
    title = models.CharField(max_length=120, blank=True, null=True, verbose_name="Section Column Title")
    position = models.PositiveIntegerField(blank=True, null=True, verbose_name="Display Order Position")

    class Meta:
        verbose_name = "Footer Section"
        verbose_name_plural = "Footer Sections"
        ordering = ["position", "id"]

    def __str__(self) -> str:
        return self.title or f"Footer Section Column (ID: {self.id})"

class FooterLink(CMSBaseModel):
    section = models.ForeignKey(FooterSection, related_name="links", on_delete=models.CASCADE, blank=True, null=True, verbose_name="Parent Section Column")
    label = models.CharField(max_length=120, blank=True, null=True, verbose_name="Link Text Label")
    route = models.CharField(max_length=500, blank=True, null=True, verbose_name="Link Target Route / URL")
    link_type = models.CharField(max_length=50, blank=True, null=True, verbose_name="Link Action Type System")
    action = models.CharField(max_length=120, blank=True, null=True, verbose_name="Trigger Action ID Hook")
    position = models.PositiveIntegerField(blank=True, null=True, verbose_name="Link Vertical Sort Index Position")

    class Meta:
        verbose_name = "Footer Link"
        verbose_name_plural = "Footer Links"
        ordering = ["position", "id"]

    def __str__(self) -> str:
        return self.label or f"Footer Link Item (ID: {self.id})"

class FooterSocialLink(CMSBaseModel):
    platform = models.CharField(max_length=50, blank=True, null=True, verbose_name="Platform Name ID Reference")
    url = models.CharField(max_length=500, blank=True, null=True, verbose_name="Profile URL Destination Path")
    icon_key = models.CharField(max_length=50, blank=True, null=True, verbose_name="Icon Library Key Lookup Reference")
    icon_class = models.CharField(max_length=255, blank=True, null=True, verbose_name="Icon CSS Class")
    position = models.PositiveIntegerField(blank=True, null=True, verbose_name="Layout Placement Sort Index Position")
    is_visible = models.BooleanField(default=True, verbose_name="Is Visible")

    class Meta:
        verbose_name = "Footer Social Link"
        verbose_name_plural = "Footer Social Links"
        ordering = ["position", "id"]

    def __str__(self) -> str:
        return self.platform or f"Social Network Link (ID: {self.id})"

class FooterPaymentMethod(CMSBaseModel):
    method_name = models.CharField(max_length=50, blank=True, null=True, verbose_name="Payment Gateway Platform Name")
    icon_key = models.CharField(max_length=50, blank=True, null=True, verbose_name="Icon Library Token Lookup Match")
    position = models.PositiveIntegerField(blank=True, null=True, verbose_name="Horizontal Layout Render Alignment Index Order")

    class Meta:
        verbose_name = "Footer Payment Method"
        verbose_name_plural = "Footer Payment Methods"
        ordering = ["position", "id"]

    def __str__(self) -> str:
        return self.method_name or f"Payment Platform Badge (ID: {self.id})"

class FooterTrustBadge(CMSBaseModel):
    badge_name = models.CharField(max_length=120, blank=True, null=True, verbose_name="Trust Certification Title / Tag")
    icon_key = models.CharField(max_length=50, blank=True, null=True, verbose_name="Icon Library Vector ID Token Key")
    position = models.PositiveIntegerField(blank=True, null=True, verbose_name="Display Sorting Priority Sequence Order")

    class Meta:
        verbose_name = "Footer Trust Badge"
        verbose_name_plural = "Footer Trust Badges"
        ordering = ["position", "id"]

    def __str__(self) -> str:
        return self.badge_name or f"Trust Certificate Seal (ID: {self.id})"

class ContactPage(SingletonCMSModel):
    hero_title = models.CharField(max_length=255, blank=True, null=True, verbose_name="Hero Title")
    hero_subtitle = models.CharField(max_length=255, blank=True, null=True, verbose_name="Hero Subtitle")
    hero_description = models.TextField(blank=True, null=True, verbose_name="Hero Description")
    hero_image = models.ImageField(upload_to=_upload_to_contact_media, blank=True, null=True, verbose_name="Hero Background Image")
    intro_heading = models.CharField(max_length=255, blank=True, null=True, verbose_name="Intro Heading")
    intro_text = models.TextField(blank=True, null=True, verbose_name="Intro Description Text")
    address_heading = models.CharField(max_length=255, blank=True, null=True, verbose_name="Address Heading")
    physical_address = models.TextField(blank=True, null=True, verbose_name="Physical Address")
    map_heading = models.CharField(max_length=255, blank=True, null=True, verbose_name="Map Section Heading")
    map_embed_url = models.TextField(blank=True, null=True, verbose_name="Google Map Embed URL")
    hours_heading = models.CharField(max_length=255, blank=True, null=True, verbose_name="Office Hours Heading")
    hours_description = models.TextField(blank=True, null=True, verbose_name="Office Hours Description Text")
    form_heading = models.CharField(max_length=255, blank=True, null=True, verbose_name="Form Section Heading")
    form_subheading = models.CharField(max_length=255, blank=True, null=True, verbose_name="Form Section Subheading")
    form_submit_button_label = models.CharField(max_length=120, blank=True, null=True, verbose_name="Form Submit Button Text")
    form_success_message = models.TextField(blank=True, null=True, verbose_name="Form Success Feedback Message")
    seo_meta_title = models.CharField(max_length=255, blank=True, null=True, verbose_name="SEO Meta Title")
    seo_meta_description = models.TextField(blank=True, null=True, verbose_name="SEO Meta Description")
    seo_meta_keywords = models.CharField(max_length=255, blank=True, null=True, verbose_name="SEO Meta Keywords")

    class Meta:
        verbose_name = "Contact Page Settings"
        verbose_name_plural = "Contact Page Settings"

    def save(self, *args: Any, **kwargs: Any):
        if self.hero_image and not getattr(self.hero_image, "_committed", True):
            from .services import optimize_uploaded_image
            try:
                result = optimize_uploaded_image(
                    self.hero_image.file,
                    target_max_bytes=500 * 1024,
                    max_width=2000,
                    min_width=640,
                    filename_prefix="foundation/contact/media",
                )
                self.hero_image.save(result.filename, result.file, save=False)
            except Exception:
                pass
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return "Global Contact Page Settings"

class ContactPhone(CMSBaseModel):
    contact_page = models.ForeignKey(ContactPage, related_name="phones", on_delete=models.CASCADE, verbose_name="Parent Contact Page Settings")
    label = models.CharField(max_length=120, blank=True, null=True, verbose_name="Phone Label/Department")
    phone_number = models.CharField(max_length=50, blank=True, null=True, verbose_name="Phone Number")
    position = models.PositiveIntegerField(blank=True, null=True, verbose_name="Display Order Position")
    is_visible = models.BooleanField(default=True, verbose_name="Is Visible")

    class Meta:
        verbose_name = "Contact Phone"
        verbose_name_plural = "Contact Phones"
        ordering = ["position", "id"]

    def __str__(self) -> str:
        return f"{self.label or 'Phone'}: {self.phone_number or 'Empty'}"

class ContactEmail(CMSBaseModel):
    contact_page = models.ForeignKey(ContactPage, related_name="emails", on_delete=models.CASCADE, verbose_name="Parent Contact Page Settings")
    label = models.CharField(max_length=120, blank=True, null=True, verbose_name="Email Label/Department")
    email_address = models.EmailField(blank=True, null=True, verbose_name="Email Address")
    position = models.PositiveIntegerField(blank=True, null=True, verbose_name="Display Order Position")
    is_visible = models.BooleanField(default=True, verbose_name="Is Visible")

    class Meta:
        verbose_name = "Contact Email"
        verbose_name_plural = "Contact Emails"
        ordering = ["position", "id"]

    def __str__(self) -> str:
        return f"{self.label or 'Email'}: {self.email_address or 'Empty'}"

class ContactSocialLink(CMSBaseModel):
    contact_page = models.ForeignKey(ContactPage, related_name="social_links", on_delete=models.CASCADE, verbose_name="Parent Contact Page Settings")
    platform = models.CharField(max_length=50, blank=True, null=True, verbose_name="Platform Name")
    url = models.CharField(max_length=500, blank=True, null=True, verbose_name="Profile URL Destination Path")
    icon_key = models.CharField(max_length=50, blank=True, null=True, verbose_name="Icon Library Key Lookup Reference")
    icon_class = models.CharField(max_length=255, blank=True, null=True, verbose_name="Icon CSS Class")
    position = models.PositiveIntegerField(blank=True, null=True, verbose_name="Display Order Position")
    is_visible = models.BooleanField(default=True, verbose_name="Is Visible")

    class Meta:
        verbose_name = "Contact Social Link"
        verbose_name_plural = "Contact Social Links"
        ordering = ["position", "id"]

    def __str__(self) -> str:
        return f"{self.platform or 'Social Link'} ({self.url or 'No URL'})"

class ContactOfficeHour(CMSBaseModel):
    class StatusChoices(models.TextChoices):
        OPEN = "open", "Open"
        CLOSED = "closed", "Closed"
        BY_APPOINTMENT = "by_appointment", "By Appointment Only"

    contact_page = models.ForeignKey(ContactPage, related_name="office_hours", on_delete=models.CASCADE, verbose_name="Parent Contact Page Settings")
    day = models.CharField(max_length=120, blank=True, null=True, verbose_name="Day/Day Range")
    opening_time = models.CharField(max_length=120, blank=True, null=True, verbose_name="Opening Time")
    closing_time = models.CharField(max_length=120, blank=True, null=True, verbose_name="Closing Time")
    status = models.CharField(max_length=50, choices=StatusChoices.choices, default=StatusChoices.OPEN, blank=True, null=True, verbose_name="Operating Status")
    position = models.PositiveIntegerField(blank=True, null=True, verbose_name="Display Order Position")
    is_visible = models.BooleanField(default=True, verbose_name="Is Visible")

    class Meta:
        verbose_name = "Contact Office Hour"
        verbose_name_plural = "Contact Office Hours"
        ordering = ["position", "id"]

    def __str__(self) -> str:
        return f"{self.day or 'Schedule'}: {self.opening_time or ''} - {self.closing_time or ''}"

class DigitalBusinessCard(CMSBaseModel):
    """
    Client / Master Artisan / Representative Digital Business Card profile
    with automated QR code generation for physical visiting cards.
    """
    full_name = models.CharField(max_length=150, verbose_name=_("Full Name"))
    title_or_role = models.CharField(max_length=150, blank=True, null=True, verbose_name=_("Title / Designation"))
    company_name = models.CharField(max_length=200, default="Gobindas Handicrafts", verbose_name=_("Company Name"))
    slug = models.SlugField(max_length=200, unique=True, db_index=True, help_text=_("URL identifier (e.g., 'rajesh-shrestha')"))
    
    avatar = models.ImageField(upload_to=_upload_to_card_avatars, blank=True, null=True, verbose_name=_("Profile Photo / Company Logo"))
    phone_number = models.CharField(max_length=50, blank=True, null=True, verbose_name=_("Phone Number"))
    whatsapp_number = models.CharField(max_length=50, blank=True, null=True, verbose_name=_("WhatsApp Number"))
    email = models.EmailField(blank=True, null=True, verbose_name=_("Email Address"))
    website = models.URLField(blank=True, null=True, verbose_name=_("Website URL"))
    
    address = models.TextField(blank=True, null=True, verbose_name=_("Physical Address"))
    bio = models.TextField(blank=True, null=True, verbose_name=_("Short Bio / Description"))
    
    facebook_url = models.URLField(blank=True, null=True, verbose_name=_("Facebook URL"))
    instagram_url = models.URLField(blank=True, null=True, verbose_name=_("Instagram URL"))
    linkedin_url = models.URLField(blank=True, null=True, verbose_name=_("LinkedIn URL"))
    
    qr_code_image = models.ImageField(upload_to=_upload_to_qr_codes, blank=True, null=True, verbose_name=_("Generated QR Code PNG"))
    is_active = models.BooleanField(default=True, verbose_name=_("Is Card Active"))

    class Meta:
        verbose_name = _("Digital Business Card")
        verbose_name_plural = _("Digital Business Cards")
        ordering = ["full_name"]

    def __str__(self) -> str:
        return f"{self.full_name} - {self.company_name}"

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.full_name)
        
        super().save(*args, **kwargs)
        self.generate_qr_code()

    def generate_qr_code(self):
        """Generates a high-resolution PNG QR Code pointing to the public card page."""
        try:
            import qrcode
        except ImportError:
            return

        site_domain = getattr(settings, "SITE_DOMAIN", "https://gobindashandicraft.com")
        card_url = f"{site_domain}/card/{self.slug}/"

        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_H,
            box_size=12,
            border=2,
        )
        qr.add_data(card_url)
        qr.make(fit=True)

        img = qr.make_image(fill_color="#1A1512", back_color="#FFFFFF")
        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        
        file_name = f"qr_{self.slug}.png"
        
        DigitalBusinessCard.objects.filter(pk=self.pk).update(qr_code_image=None)
        self.qr_code_image.save(file_name, ContentFile(buffer.getvalue()), save=False)
        DigitalBusinessCard.objects.filter(pk=self.pk).update(qr_code_image=self.qr_code_image.name)