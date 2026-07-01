from __future__ import annotations

import uuid
from pathlib import Path

from django.core.exceptions import ValidationError
from django.db import models

def _upload_to_site_logo(instance, filename: str) -> str:
    """
    Stores site logo files in a dedicated CMS path.
    The actual compression / 500KB optimization should be handled
    in the service layer, not in the model layer.
    """
    suffix = Path(filename).suffix.lower() or ".png"
    return f"foundation/site-settings/logo/{uuid.uuid4().hex}{suffix}"

def _upload_to_navbar_media(instance, filename: str) -> str:
    suffix = Path(filename).suffix.lower() or ".png"
    return f"foundation/navbar/media/{uuid.uuid4().hex}{suffix}"

def _validate_json_structure(value, field_name: str, expected_types: tuple[type, ...]) -> None:
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
    """
    Enforces a single global record for CMS configuration models.
    """

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
    """
    Global branding / site identity and feature flags.
    Logo is mandatory.
    Everything else is optional and can remain empty until configured in CMS.
    """

    logo = models.ImageField(
        upload_to=_upload_to_site_logo,
        verbose_name="Logo",
        help_text="Mandatory site logo upload. Optimization to <= 500KB is handled in the service layer.",
    )
    mobile_logo = models.ImageField(
        upload_to=_upload_to_site_logo,
        blank=True,
        null=True,
        verbose_name="Mobile Logo",
        help_text="Optional separate logo optimized for mobile devices."
    )
    logo_link = models.CharField(
        max_length=500,
        blank=True,
        null=True,
        default="/",
        verbose_name="Logo Link URL",
        help_text="The destination URL when the brand logo is clicked."
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
    search_placeholder = models.CharField(
        max_length=255,
        default="Find artisan rugs...",
        blank=True,
        null=True,
        verbose_name="Search Placeholder",
        help_text="Placeholder text shown inside the global header search input."
    )
    search_button_label = models.CharField(
        max_length=120,
        default="Search",
        blank=True,
        null=True,
        verbose_name="Search Button Label",
        help_text="Accessibility label/text for the search button."
    )
    cart_button_label = models.CharField(
        max_length=120,
        default="Shopping Bag",
        blank=True,
        null=True,
        verbose_name="Cart Button Label",
        help_text="Accessibility label/text for the cart button."
    )
    cart_badge_count = models.PositiveIntegerField(
        default=2,
        blank=True,
        null=True,
        verbose_name="Cart Badge Count",
        help_text="Dynamic count badge shown on the header cart button."
    )
    default_featured_image = models.ImageField(
        upload_to=_upload_to_navbar_media,
        blank=True,
        null=True,
        verbose_name="Default Mega Menu Featured Image",
        help_text="Fallback featured image for mega menus when no specific image is provided."
    )
    default_featured_title = models.CharField(
        max_length=160,
        default="Meet the Artisans",
        blank=True,
        null=True,
        verbose_name="Default Mega Menu Featured Title",
        help_text="Fallback featured title for mega menus."
    )
    default_featured_text = models.TextField(
        default="Explore the workshop lineages of India.",
        blank=True,
        null=True,
        verbose_name="Default Mega Menu Featured Text",
        help_text="Fallback featured description text for mega menus."
    )

    # --------------------------------------------------------------------------
    # Feature Flags
    # --------------------------------------------------------------------------
    enable_customer_registration = models.BooleanField(
        default=True,
        verbose_name="Enable Customer Registration",
        help_text="Globally enable or disable new customer account registration pages and buttons."
    )
    enable_guest_checkout = models.BooleanField(
        default=True,
        verbose_name="Enable Guest Checkout",
        help_text="Globally allow or restrict customers from checking out without creating an account."
    )
    enable_social_login = models.BooleanField(
        default=True,
        verbose_name="Enable Social Login",
        help_text="Globally enable or disable social authentication (SSO) flows."
    )
    enable_google_login = models.BooleanField(
        default=True,
        verbose_name="Enable Google Login",
        help_text="Allow customers to log in using their Google accounts (if Social Login is enabled)."
    )
    enable_facebook_login = models.BooleanField(
        default=True,
        verbose_name="Enable Facebook Login",
        help_text="Allow customers to log in using their Facebook accounts (if Social Login is enabled)."
    )
    enable_github_login = models.BooleanField(
        default=False,
        verbose_name="Enable GitHub Login",
        help_text="Allow customers to log in using their GitHub accounts (if Social Login is enabled)."
    )

    class Meta:
        verbose_name = "Site Settings"
        verbose_name_plural = "Site Settings"
        ordering = ["-updated_at"]

    def clean(self):
        super().clean()
        if not self.logo:
            raise ValidationError({"logo": "Logo upload is required."})

    def save(self, *args, **kwargs):
        if self.logo and not getattr(self.logo, '_committed', True):
            from .services import prepare_logo_upload
            try:
                result = prepare_logo_upload(self.logo.file)
                self.logo.save(result.filename, result.file, save=False)
            except Exception:
                pass

        if self.default_featured_image and not getattr(self.default_featured_image, '_committed', True):
            from .services import prepare_navbar_media_upload
            try:
                result = prepare_navbar_media_upload(self.default_featured_image.file)
                self.default_featured_image.save(result.filename, result.file, save=False)
            except Exception:
                pass

        super().save(*args, **kwargs)

    def __str__(self):
        return "Site Settings"

class HeaderBar(SingletonCMSModel):
    """
    CMS-driven top header bar.

    Designed for:
    - announcement/rotator messages
    - left-side utility items
    - right-side utility links
    - configurable rotator speed
    """
    
    is_enabled = models.BooleanField(
        default=True,
        verbose_name="Enable Header Bar",
        help_text="Globally toggle the visibility of the entire top header bar."
    )
    is_sticky = models.BooleanField(
        default=False,
        verbose_name="Sticky Header Bar",
        help_text="Pin the header bar to the top of the viewport when scrolling."
    )
    show_on_desktop = models.BooleanField(
        default=True,
        verbose_name="Show on Desktop Devices"
    )
    show_on_mobile = models.BooleanField(
        default=True,
        verbose_name="Show on Mobile Devices"
    )
    currency_label = models.CharField(
        max_length=20,
        blank=True,
        null=True,
        verbose_name="Currency Label",
        help_text="Example: USD",
    )
    language_label = models.CharField(
        max_length=20,
        blank=True,
        null=True,
        verbose_name="Language Label",
        help_text="Example: EN",
    )
    announcement_messages = models.JSONField(
        blank=True,
        null=True,
        verbose_name="Announcement Messages",
        help_text="JSON list of campaign/announcement strings shown in the rotator.",
    )
    rotator_interval_ms = models.PositiveIntegerField(
        blank=True,
        null=True,
        verbose_name="Rotator Interval (ms)",
        help_text="Example: 4000",
    )
    left_utilities = models.JSONField(
        blank=True,
        null=True,
        verbose_name="Left Utility Items",
        help_text="JSON list of left-side utility objects (dropdowns/labels).",
    )
    right_utilities = models.JSONField(
        blank=True,
        null=True,
        verbose_name="Right Utility Items",
        help_text="JSON list of right-side utility objects (links/buttons).",
    )

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

    def __str__(self):
        return "Header Bar"

class HeaderAnnouncement(CMSBaseModel):
    header_bar = models.ForeignKey(
        HeaderBar,
        related_name="announcements",
        on_delete=models.CASCADE,
        verbose_name="Header Bar Settings"
    )
    text = models.CharField(
        max_length=255,
        verbose_name="Announcement Text",
        help_text="Message text shown in the announcement rotator."
    )
    start_date = models.DateTimeField(
        blank=True,
        null=True,
        verbose_name="Activation Start Date",
        help_text="Date and time when this announcement should start displaying."
    )
    end_date = models.DateTimeField(
        blank=True,
        null=True,
        verbose_name="Activation End Date",
        help_text="Date and time when this announcement should stop displaying."
    )
    priority = models.PositiveIntegerField(
        default=0,
        verbose_name="Priority Sequence",
        help_text="Higher numbers take precedence if multiple announcements overlap."
    )
    position = models.PositiveIntegerField(
        default=0,
        verbose_name="Display Order Position",
        help_text="Lower numbers display first."
    )
    is_visible = models.BooleanField(
        default=True,
        verbose_name="Is Visible",
        help_text="Control if this announcement is active and visible."
    )

    class Meta:
        verbose_name = "Header Announcement"
        verbose_name_plural = "Header Announcements"
        ordering = ["position", "id"]

    def __str__(self):
        return self.text

class HeaderCurrency(CMSBaseModel):
    header_bar = models.ForeignKey(
        HeaderBar,
        related_name="currencies",
        on_delete=models.CASCADE,
        verbose_name="Header Bar Settings"
    )
    label = models.CharField(
        max_length=50,
        verbose_name="Currency Name / Label",
        help_text="Example: US Dollar"
    )
    code = models.CharField(
        max_length=10,
        verbose_name="Currency Code",
        help_text="Example: USD"
    )
    symbol = models.CharField(
        max_length=10,
        blank=True,
        null=True,
        verbose_name="Currency Symbol",
        help_text="Example: $"
    )
    link_url = models.CharField(
        max_length=500,
        blank=True,
        null=True,
        verbose_name="Change Currency URL / Path",
        help_text="The path/URL triggered when users click this currency."
    )
    position = models.PositiveIntegerField(
        default=0,
        verbose_name="Display Order Position"
    )
    is_visible = models.BooleanField(
        default=True,
        verbose_name="Is Visible"
    )

    class Meta:
        verbose_name = "Header Currency"
        verbose_name_plural = "Header Currencies"
        ordering = ["position", "id"]

    def __str__(self):
        return f"{self.code} ({self.label})"

class HeaderLanguage(CMSBaseModel):
    header_bar = models.ForeignKey(
        HeaderBar,
        related_name="languages",
        on_delete=models.CASCADE,
        verbose_name="Header Bar Settings"
    )
    label = models.CharField(
        max_length=50,
        verbose_name="Language Name / Label",
        help_text="Example: English"
    )
    code = models.CharField(
        max_length=10,
        verbose_name="Language Code",
        help_text="Example: EN"
    )
    link_url = models.CharField(
        max_length=500,
        blank=True,
        null=True,
        verbose_name="Change Language URL / Path",
        help_text="The path/URL triggered when users click this language."
    )
    position = models.PositiveIntegerField(
        default=0,
        verbose_name="Display Order Position"
    )
    is_visible = models.BooleanField(
        default=True,
        verbose_name="Is Visible"
    )

    class Meta:
        verbose_name = "Header Language"
        verbose_name_plural = "Header Languages"
        ordering = ["position", "id"]

    def __str__(self):
        return f"{self.code.upper()} ({self.label})"

class HeaderCountry(CMSBaseModel):
    header_bar = models.ForeignKey(
        HeaderBar,
        related_name="countries",
        on_delete=models.CASCADE,
        verbose_name="Header Bar Settings"
    )
    name = models.CharField(
        max_length=100,
        verbose_name="Country Name",
        help_text="Example: United States"
    )
    code = models.CharField(
        max_length=10,
        verbose_name="Country Code",
        help_text="Example: US"
    )
    link_url = models.CharField(
        max_length=500,
        blank=True,
        null=True,
        verbose_name="Change Country URL / Path",
        help_text="The path/URL triggered when users click this country."
    )
    position = models.PositiveIntegerField(
        default=0,
        verbose_name="Display Order Position"
    )
    is_visible = models.BooleanField(
        default=True,
        verbose_name="Is Visible"
    )

    class Meta:
        verbose_name = "Header Country"
        verbose_name_plural = "Header Countries"
        ordering = ["position", "id"]

    def __str__(self):
        return f"{self.name} ({self.code.upper()})"

class HeaderUtilityLink(CMSBaseModel):
    
    class UtilityType(models.TextChoices):
        CUSTOM = "custom", "Custom Link"
        PHONE = "phone", "Phone Number"
        EMAIL = "email", "Email Address"
        STORE_LOCATOR = "store_locator", "Store Locator"
        ACCOUNT = "account", "Account Link"

    header_bar = models.ForeignKey(
        HeaderBar,
        related_name="utilities",
        on_delete=models.CASCADE,
        verbose_name="Header Bar Settings"
    )
    utility_type = models.CharField(
        max_length=20,
        choices=UtilityType.choices,
        default=UtilityType.CUSTOM,
        verbose_name="Utility Type",
        help_text="Defines the semantic purpose of this link."
    )
    label = models.CharField(
        max_length=120,
        verbose_name="Link Label / Text"
    )
    link_url = models.CharField(
        max_length=500,
        blank=True,
        null=True,
        verbose_name="Link Target URL / Path"
    )
    side = models.CharField(
        max_length=10,
        choices=[("left", "Left Side"), ("right", "Right Side")],
        default="left",
        verbose_name="Header Placement Side"
    )
    icon_key = models.CharField(
        max_length=50,
        blank=True,
        null=True,
        verbose_name="Icon Library Key",
        help_text="Match keys like: phone, email, map-pin, truck, info."
    )
    show_dropdown_icon = models.BooleanField(
        default=False,
        verbose_name="Show Dropdown Caret",
        help_text="Display a small down arrow next to the link text."
    )
    position = models.PositiveIntegerField(
        default=0,
        verbose_name="Display Order Position"
    )
    is_visible = models.BooleanField(
        default=True,
        verbose_name="Is Visible"
    )

    class Meta:
        verbose_name = "Header Utility Link"
        verbose_name_plural = "Header Utility Links"
        ordering = ["position", "id"]

    def __str__(self):
        return f"[{self.side.upper()}] {self.label}"

class NavbarSettings(SingletonCMSModel):
    """
    Global configuration for navigation bar behavior.
    """
    is_enabled = models.BooleanField(
        default=True,
        verbose_name="Enable Main Navigation"
    )
    is_sticky = models.BooleanField(
        default=True,
        verbose_name="Sticky Navigation",
        help_text="Keep navigation pinned to top while scrolling."
    )
    desktop_behavior = models.CharField(
        max_length=50,
        choices=[("hover", "Open on Hover"), ("click", "Open on Click")],
        default="hover",
        verbose_name="Desktop Navigation Behavior"
    )
    mobile_behavior = models.CharField(
        max_length=50,
        choices=[("accordion", "Accordion Menu"), ("offcanvas", "Offcanvas Menu")],
        default="offcanvas",
        verbose_name="Mobile Navigation Behavior"
    )

    class Meta:
        verbose_name = "Navbar Settings"
        verbose_name_plural = "Navbar Settings"

    def __str__(self):
        return "Global Navbar Settings"

class NavbarItem(CMSBaseModel):
    """
    CMS-driven navbar item system.
    Supports:
    - simple links
    - dropdown parents
    - mega menu parents
    - nested children
    - optional badge text
    - optional featured media for mega menu presentation
    """

    class MenuType(models.TextChoices):
        LINK = "link", "Link"
        DROPDOWN = "dropdown", "Dropdown"
        MEGA_MENU = "mega_menu", "Mega Menu"

    class VisibilityScope(models.TextChoices):
        ALL = "all", "All Devices"
        DESKTOP = "desktop", "Desktop Only"
        MOBILE = "mobile", "Mobile Only"

    parent = models.ForeignKey(
        "self",
        related_name="children",
        on_delete=models.CASCADE,
        blank=True,
        null=True,
        verbose_name="Parent Item",
        help_text="Leave empty for top-level items.",
    )
    label = models.CharField(
        max_length=120,
        verbose_name="Label",
        help_text="Visible menu label.",
    )
    slug = models.SlugField(
        max_length=120,
        blank=True,
        null=True,
        verbose_name="Slug",
        help_text="Optional identifier for referencing within codebase."
    )
    link_url = models.CharField(
        max_length=500,
        blank=True,
        null=True,
        verbose_name="Link URL",
        help_text="Optional. Can be empty for mega menu parents or section labels.",
    )
    open_in_new_tab = models.BooleanField(
        default=False,
        verbose_name="Open in New Tab"
    )
    menu_type = models.CharField(
        max_length=20,
        choices=MenuType.choices,
        verbose_name="Menu Type",
        help_text="Controls how the item renders in the frontend.",
    )
    position = models.PositiveIntegerField(
        blank=True,
        null=True,
        verbose_name="Display Order",
        help_text="Lower numbers appear first.",
    )
    icon_key = models.CharField(
        max_length=50,
        blank=True,
        null=True,
        verbose_name="Icon Key",
        help_text="Optional icon identifier for display next to label."
    )
    badge_text = models.CharField(
        max_length=50,
        blank=True,
        null=True,
        verbose_name="Badge Text",
        help_text="Example: Eco-Friendly, New, Hot",
    )
    badge_style = models.CharField(
        max_length=50,
        blank=True,
        null=True,
        verbose_name="Badge Style",
        help_text="Optional CSS hook for badge styling.",
    )
    requires_authentication = models.BooleanField(
        default=False,
        verbose_name="Requires Authentication",
        help_text="If checked, item is only visible to logged-in users."
    )
    visibility_scope = models.CharField(
        max_length=20,
        choices=VisibilityScope.choices,
        blank=True,
        null=True,
        verbose_name="Visibility Scope",
        help_text="Optional device visibility rule.",
    )
    start_date = models.DateTimeField(
        blank=True,
        null=True,
        verbose_name="Activation Start Date"
    )
    end_date = models.DateTimeField(
        blank=True,
        null=True,
        verbose_name="Activation End Date"
    )

    # -- Featured Media Block Controls --
    featured_is_visible = models.BooleanField(
        default=True,
        verbose_name="Featured Media Block Visibility"
    )
    featured_image = models.ImageField(
        upload_to=_upload_to_navbar_media,
        blank=True,
        null=True,
        verbose_name="Featured Image",
        help_text="Optional image for mega menu media block.",
    )
    featured_title = models.CharField(
        max_length=160,
        blank=True,
        null=True,
        verbose_name="Featured Title",
    )
    featured_text = models.TextField(
        blank=True,
        null=True,
        verbose_name="Featured Text",
    )
    featured_cta_text = models.CharField(
        max_length=120,
        blank=True,
        null=True,
        verbose_name="Featured CTA Text",
        help_text="Button text for the featured media block."
    )
    featured_cta_url = models.CharField(
        max_length=500,
        blank=True,
        null=True,
        verbose_name="Featured CTA URL",
        help_text="Button URL target for the featured media block."
    )
    featured_start_date = models.DateTimeField(
        blank=True,
        null=True,
        verbose_name="Featured Media Start Date",
        help_text="Schedule when to start showing the featured block."
    )
    featured_end_date = models.DateTimeField(
        blank=True,
        null=True,
        verbose_name="Featured Media End Date",
        help_text="Schedule when to stop showing the featured block."
    )

    class Meta:
        verbose_name = "Navbar Item"
        verbose_name_plural = "Navbar Items"
        ordering = ["position", "label", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["parent", "label"],
                name="uniq_navbar_item_parent_label",
            )
        ]
        indexes = [
            models.Index(fields=["parent", "position"]),
            models.Index(fields=["menu_type"]),
        ]

    def clean(self):
        super().clean()
        if self.visibility_scope is not None and self.visibility_scope not in self.VisibilityScope.values:
            raise ValidationError({"visibility_scope": "Invalid visibility scope."})

    def save(self, *args, **kwargs):
        if self.featured_image and not getattr(self.featured_image, '_committed', True):
            from .services import prepare_navbar_media_upload
            try:
                result = prepare_navbar_media_upload(self.featured_image.file)
                self.featured_image.save(result.filename, result.file, save=False)
            except Exception:
                pass
        super().save(*args, **kwargs)

    def __str__(self):
        return self.label

class NavbarMegaMenuColumn(CMSBaseModel):
    """
    Structured columns for mega menus attached to a specific NavbarItem.
    """
    parent_item = models.ForeignKey(
        NavbarItem,
        related_name="mega_menu_columns",
        on_delete=models.CASCADE,
        verbose_name="Parent Navbar Item",
    )
    heading = models.CharField(
        max_length=120,
        blank=True,
        null=True,
        verbose_name="Column Heading",
        help_text="Optional heading displayed above the column links.",
    )
    position = models.PositiveIntegerField(
        default=0,
        verbose_name="Display Order",
        help_text="Lower numbers appear further to the left.",
    )
    visibility_scope = models.CharField(
        max_length=20,
        choices=NavbarItem.VisibilityScope.choices,
        blank=True,
        null=True,
        verbose_name="Visibility Scope"
    )
    is_active = models.BooleanField(
        default=True,
        verbose_name="Is Active",
    )

    class Meta:
        verbose_name = "Mega Menu Column"
        verbose_name_plural = "Mega Menu Columns"
        ordering = ["position", "id"]

    def __str__(self):
        return f"{self.heading or 'Untitled Column'} (Parent: {self.parent_item.label})"

class NavbarMegaMenuLink(CMSBaseModel):
    """
    Individual links positioned within a specific Mega Menu Column.
    """
    parent_column = models.ForeignKey(
        NavbarMegaMenuColumn,
        related_name="links",
        on_delete=models.CASCADE,
        verbose_name="Parent Column",
    )
    label = models.CharField(
        max_length=120,
        verbose_name="Link Label",
    )
    link_url = models.CharField(
        max_length=500,
        blank=True,
        null=True,
        verbose_name="Link URL",
    )
    icon_key = models.CharField(
        max_length=50,
        blank=True,
        null=True,
        verbose_name="Icon Key"
    )
    open_in_new_tab = models.BooleanField(
        default=False,
        verbose_name="Open in New Tab",
    )
    is_featured = models.BooleanField(
        default=False,
        verbose_name="Is Featured",
        help_text="Apply visual emphasis to this specific link."
    )
    position = models.PositiveIntegerField(
        default=0,
        verbose_name="Display Order",
        help_text="Lower numbers appear higher in the column.",
    )
    visibility_scope = models.CharField(
        max_length=20,
        choices=NavbarItem.VisibilityScope.choices,
        blank=True,
        null=True,
        verbose_name="Visibility Scope"
    )
    is_active = models.BooleanField(
        default=True,
        verbose_name="Is Active",
    )

    class Meta:
        verbose_name = "Mega Menu Link"
        verbose_name_plural = "Mega Menu Links"
        ordering = ["position", "id"]

    def __str__(self):
        return self.label

# =========================================
# CMS DYNAMIC FOOTER ARCHITECTURE
# =========================================
class FooterSettings(SingletonCMSModel):
    """
    Global configuration settings for the structural footer layout, 
    brand statements, newsletter integration, and copyrights.
    """
    
    logo = models.ImageField(
        upload_to='footer/',
        blank=True,
        null=True,
        verbose_name="Footer Logo",
        help_text="Optional logo image dedicated to the footer section."
    )
    brand_name = models.CharField(
        max_length=120,
        blank=True,
        null=True,
        verbose_name="Brand Name",
        help_text="Main brand title visible inside the footer brand block."
    )
    fair_trade_statement = models.TextField(
        blank=True,
        null=True,
        verbose_name="Fair Trade / Trust Statement",
        help_text="A short paragraph highlighting ethical commitments and trust value propositions."
    )
    newsletter_heading = models.CharField(
        max_length=120,
        blank=True,
        null=True,
        verbose_name="Newsletter Heading",
        help_text="Heading text over the newsletter email input field."
    )
    newsletter_subtext = models.TextField(
        blank=True,
        null=True,
        verbose_name="Newsletter Subtext",
        help_text="Descriptive helper text beneath or above the newsletter input field."
    )
    newsletter_endpoint = models.CharField(
        max_length=500,
        blank=True,
        null=True,
        verbose_name="Newsletter Submission Endpoint",
        help_text="API action URL target route where newsletter forms execute submissions."
    )
    newsletter_placeholder = models.CharField(
        max_length=120,
        blank=True,
        null=True,
        verbose_name="Newsletter Input Placeholder",
        help_text="Default context text visibility inside empty email fields."
    )
    copyright_template = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        verbose_name="Copyright Template",
        help_text="Template pattern string. Accepts tags like {current_year} and {brand_name}."
    )

    class Meta:
        verbose_name = "Footer Settings"
        verbose_name_plural = "Footer Settings"

    def save(self, *args, **kwargs):
        if self.logo and not getattr(self.logo, '_committed', True):
            from .services import optimize_uploaded_image
            try:
                result = optimize_uploaded_image(
                    self.logo.file,
                    target_max_bytes=500 * 1024,
                    max_width=2000,
                    min_width=640,
                    filename_prefix="foundation/footer/logo",
                )
                self.logo.save(result.filename, result.file, save=False)
            except Exception:
                pass
        super().save(*args, **kwargs)

    def __str__(self):
        return "Global Footer Settings"

class FooterSection(CMSBaseModel):
    """
    Represents standalone navigation link column blocks arranged 
    within the flexible grid container structure.
    """
    title = models.CharField(
        max_length=120,
        blank=True,
        null=True,
        verbose_name="Section Column Title",
        help_text="Header layout title showing over lists of custom links."
    )
    position = models.PositiveIntegerField(
        blank=True,
        null=True,
        verbose_name="Display Order Position",
        help_text="Lower numeric positions dictate layout prioritization ordering left-to-right."
    )

    class Meta:
        verbose_name = "Footer Section"
        verbose_name_plural = "Footer Sections"
        ordering = ["position", "id"]

    def __str__(self):
        return self.title or f"Footer Section Column (ID: {self.id})"

class FooterLink(CMSBaseModel):
    """
    Individual navigation anchors assigned structurally into corresponding 
    parent layout column blocks.
    """
    section = models.ForeignKey(
        FooterSection,
        related_name="links",
        on_delete=models.CASCADE,
        blank=True,
        null=True,
        verbose_name="Parent Section Column"
    )
    label = models.CharField(
        max_length=120,
        blank=True,
        null=True,
        verbose_name="Link Text Label"
    )
    route = models.CharField(
        max_length=500,
        blank=True,
        null=True,
        verbose_name="Link Target Route / URL",
        help_text="The destination target pathname reference context or full web path link URL."
    )
    link_type = models.CharField(
        max_length=50,
        blank=True,
        null=True,
        verbose_name="Link Action Type System",
        help_text="Configured types like 'internal_route', 'external_url', or 'action_trigger'."
    )
    action = models.CharField(
        max_length=120,
        blank=True,
        null=True,
        verbose_name="Trigger Action ID Hook",
        help_text="Optional identifier parsed to trigger specialized UI events like modal windows."
    )
    position = models.PositiveIntegerField(
        blank=True,
        null=True,
        verbose_name="Link Vertical Sort Index Position",
        help_text="Ascending numeric configurations control programmatic layout sorting rules top-to-bottom."
    )

    class Meta:
        verbose_name = "Footer Link"
        verbose_name_plural = "Footer Links"
        ordering = ["position", "id"]

    def __str__(self):
        return self.label or f"Footer Link Item (ID: {self.id})"

class FooterSocialLink(CMSBaseModel):
    """
    Individual profiles connecting brand layout environments across external social media spaces.
    """
    platform = models.CharField(
        max_length=50,
        blank=True,
        null=True,
        verbose_name="Platform Name ID Reference"
    )
    url = models.CharField(
        max_length=500,
        blank=True,
        null=True,
        verbose_name="Profile URL Destination Path"
    )
    icon_key = models.CharField(
        max_length=50,
        blank=True,
        null=True,
        verbose_name="Icon Library Key Lookup Reference",
        help_text="Identical key matched internally inside standard lookup arrays (e.g., 'instagram', 'pinterest')."
    )
    position = models.PositiveIntegerField(
        blank=True,
        null=True,
        verbose_name="Layout Placement Sort Index Position"
    )

    class Meta:
        verbose_name = "Footer Social Link"
        verbose_name_plural = "Footer Social Links"
        ordering = ["position", "id"]

    def __str__(self):
        return self.platform or f"Social Network Link (ID: {self.id})"

class FooterPaymentMethod(CMSBaseModel):
    """
    Renders supported billing validation badge layout blocks aligned directly inside foot utility menus.
    """
    method_name = models.CharField(
        max_length=50,
        blank=True,
        null=True,
        verbose_name="Payment Gateway Platform Name"
    )
    icon_key = models.CharField(
        max_length=50,
        blank=True,
        null=True,
        verbose_name="Icon Library Token Lookup Match",
        help_text="Identical lookup key strings matching asset mappings (e.g., 'visa', 'mastercard', 'paypal')."
    )
    position = models.PositiveIntegerField(
        blank=True,
        null=True,
        verbose_name="Horizontal Layout Render Alignment Index Order"
    )

    class Meta:
        verbose_name = "Footer Payment Method"
        verbose_name_plural = "Footer Payment Methods"
        ordering = ["position", "id"]

    def __str__(self):
        return self.method_name or f"Payment Platform Badge (ID: {self.id})"

class FooterTrustBadge(CMSBaseModel):
    """
    Global compliance verifications displayed structurally next to corporate messaging profiles.
    """
    badge_name = models.CharField(
        max_length=120,
        blank=True,
        null=True,
        verbose_name="Trust Certification Title / Tag"
    )
    icon_key = models.CharField(
        max_length=50,
        blank=True,
        null=True,
        verbose_name="Icon Library Vector ID Token Key",
        help_text="System programmatic map token ID index keys (e.g., 'fair-trade', 'climate-neutral')."
    )
    position = models.PositiveIntegerField(
        blank=True,
        null=True,
        verbose_name="Display Sorting Priority Sequence Order"
    )

    class Meta:
        verbose_name = "Footer Trust Badge"
        verbose_name_plural = "Footer Trust Badges"
        ordering = ["position", "id"]

    def __str__(self):
        return self.badge_name or f"Trust Certificate Seal (ID: {self.id})"