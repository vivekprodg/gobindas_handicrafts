import os
from typing import Any
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

def customer_avatar_upload_path(instance, filename: str) -> str:
    """
    Generates isolated file upload path for customer profile avatars.
    """
    ext = os.path.splitext(filename)[1].lower() or ".jpg"
    user_id = instance.user_id or "new"
    return f"customers/avatars/profile_{user_id}{ext}"

class CustomerProfile(models.Model):
    """
    Stores customer demographics, B2B wholesale metadata, and account configurations linked 1-to-1 with User.
    """
    class GenderChoices(models.TextChoices):
        MALE = 'MALE', _('Male')
        FEMALE = 'FEMALE', _('Female')
        OTHER = 'OTHER', _('Other')
        PREFER_NOT_TO_SAY = 'PREFER_NOT_TO_SAY', _('Prefer Not To Say')

    class AccountType(models.TextChoices):
        INDIVIDUAL = 'INDIVIDUAL', _('Individual Person')
        WHOLESALE = 'WHOLESALE', _('Wholeseller / Retailer')
        ORGANIZATION = 'ORGANIZATION', _('Organization / Corporate / Bulk Supplier')

    class BusinessType(models.TextChoices):
        RETAILER = 'RETAILER', _('Retail Store / Boutique')
        WHOLESALER = 'WHOLESALER', _('Wholesale Distributor')
        HOTEL_RESORT = 'HOTEL_RESORT', _('Hotel / Resort / Hospitality')
        GOVERNMENT_NGO = 'GOVERNMENT_NGO', _('Government / Non-Profit / NGO')
        PRIVATE_CORP = 'PRIVATE_CORP', _('Private Enterprise / Corporate')
        OTHER = 'OTHER', _('Other Entity Type')

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='customer_profile',
        verbose_name=_("User Reference")
    )
    account_type = models.CharField(
        max_length=32,
        choices=AccountType.choices,
        default=AccountType.INDIVIDUAL,
        db_index=True,
        verbose_name=_("Account Classification")
    )
    phone_number = models.CharField(
        max_length=32,
        blank=True,
        null=True,
        db_index=True,
        verbose_name=_("Phone Number")
    )
    avatar = models.ImageField(
        upload_to=customer_avatar_upload_path,
        blank=True,
        null=True,
        verbose_name=_("Avatar Image")
    )
    date_of_birth = models.DateField(
        blank=True,
        null=True,
        verbose_name=_("Date of Birth")
    )
    gender = models.CharField(
        max_length=24,
        choices=GenderChoices.choices,
        blank=True,
        null=True,
        verbose_name=_("Gender Profile")
    )
    preferred_language = models.CharField(
        max_length=16,
        blank=True,
        null=True,
        default='en',
        db_index=True,
        verbose_name=_("Preferred Language")
    )
    newsletter_subscribed = models.BooleanField(
        default=False,
        verbose_name=_("Newsletter Subscribed")
    )

    # --------------------------------------------------------------------------
    # B2B / Wholesale / Bulk Supplier / Organization Profile Fields
    # --------------------------------------------------------------------------
    company_name = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        db_index=True,
        verbose_name=_("Registered Company / Organization Name")
    )
    business_type = models.CharField(
        max_length=32,
        choices=BusinessType.choices,
        blank=True,
        null=True,
        verbose_name=_("Industry / Business Category")
    )
    tax_id_number = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        db_index=True,
        verbose_name=_("Tax Identification Number (Nepal PAN/VAT or International EIN/VAT)")
    )
    business_registration_number = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        verbose_name=_("Company Registration / License Number")
    )
    country_of_incorporation = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        default="Nepal",
        verbose_name=_("Country of Registration / Incorporation")
    )
    business_website = models.URLField(
        max_length=255,
        blank=True,
        null=True,
        verbose_name=_("Business Website URL")
    )
    is_approved_b2b = models.BooleanField(
        default=True,
        db_index=True,
        verbose_name=_("Approved B2B / Wholesale Account")
    )

    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("Created At"))
    updated_at = models.DateTimeField(auto_now=True, verbose_name=_("Updated At"))

    class Meta:
        verbose_name = _("Customer Profile")
        verbose_name_plural = _("Customer Profiles")
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['phone_number']),
            models.Index(fields=['account_type']),
            models.Index(fields=['company_name']),
            models.Index(fields=['tax_id_number']),
            models.Index(fields=['country_of_incorporation']),
        ]

    def __str__(self) -> str:
        if self.account_type != self.AccountType.INDIVIDUAL and self.company_name:
            return f"{self.company_name} ({self.user.username})"
        return self.user.username

    @property
    def is_business_account(self) -> bool:
        return self.account_type in [self.AccountType.WHOLESALE, self.AccountType.ORGANIZATION]

    @property
    def is_nepal_entity(self) -> bool:
        c = (self.country_of_incorporation or "").strip().lower()
        return c in ["nepal", "np"]

    @property
    def display_name(self) -> str:
        if self.is_business_account and self.company_name:
            return self.company_name
        full_name = f"{self.user.first_name} {self.user.last_name}".strip()
        return full_name if full_name else self.user.username

    @property
    def is_premium_member(self) -> bool:
        return self.is_business_account or self.newsletter_subscribed or self.user.is_staff

class CustomerAddress(models.Model):
    """
    Physical shipping and billing locations for a customer.
    """
    class AddressType(models.TextChoices):
        BILLING = 'BILLING', _('Billing')
        SHIPPING = 'SHIPPING', _('Shipping')
        BOTH = 'BOTH', _('Both')

    customer = models.ForeignKey(
        CustomerProfile,
        on_delete=models.CASCADE,
        related_name='addresses',
        verbose_name=_("Customer Profile")
    )
    full_name = models.CharField(max_length=255, verbose_name=_("Full Name"))
    phone_number = models.CharField(max_length=50, verbose_name=_("Phone Number"))
    address_line_1 = models.CharField(max_length=255, verbose_name=_("Address Line 1"))
    address_line_2 = models.CharField(max_length=255, blank=True, null=True, verbose_name=_("Address Line 2"))
    city = models.CharField(max_length=100, verbose_name=_("City"))
    state_or_province = models.CharField(max_length=100, verbose_name=_("State or Province"))
    postal_code = models.CharField(max_length=20, verbose_name=_("Postal Code"))
    country = models.CharField(max_length=100, verbose_name=_("Country"))
    address_type = models.CharField(
        max_length=20,
        choices=AddressType.choices,
        default=AddressType.BOTH,
        verbose_name=_("Address Type")
    )
    is_default = models.BooleanField(
        default=False,
        verbose_name=_("Is Default Address")
    )
    is_active = models.BooleanField(
        default=True,
        verbose_name=_("Is Active")
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("Created At"))
    updated_at = models.DateTimeField(auto_now=True, verbose_name=_("Updated At"))

    class Meta:
        verbose_name = _("Customer Address")
        verbose_name_plural = _("Customer Addresses")
        ordering = ['-is_default', '-updated_at']
        indexes = [
            models.Index(fields=['customer', 'is_active']),
            models.Index(fields=['customer', 'address_type']),
        ]

    def __str__(self) -> str:
        return f"{self.full_name} - {self.city}, {self.country} ({self.get_address_type_display()})"

    @property
    def first_name(self) -> str:
        parts = self.full_name.split()
        return parts[0] if parts else ""

    @property
    def last_name(self) -> str:
        parts = self.full_name.split()
        return " ".join(parts[1:]) if len(parts) > 1 else ""

    @property
    def address_line1(self) -> str:
        return self.address_line_1

    @property
    def address_line2(self) -> str:
        return self.address_line_2 or ""

    @property
    def state(self) -> str:
        return self.state_or_province

    @property
    def is_default_shipping(self) -> bool:
        return self.is_default and self.address_type in [self.AddressType.SHIPPING, self.AddressType.BOTH]

    @property
    def is_default_billing(self) -> bool:
        return self.is_default and self.address_type in [self.AddressType.BILLING, self.AddressType.BOTH]

    def clean(self) -> None:
        super().clean()
        if self.is_default and not self.is_active:
            raise ValidationError(_("An inactive address record cannot be designated as default."))

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.full_clean()
        if self.is_default and self.is_active:
            base_qs = CustomerAddress.objects.filter(customer=self.customer, is_default=True, is_active=True)
            if self.pk:
                base_qs = base_qs.exclude(pk=self.pk)
            if self.address_type == self.AddressType.BOTH:
                base_qs.update(is_default=False)
            elif self.address_type == self.AddressType.BILLING:
                base_qs.filter(address_type=self.AddressType.BOTH).update(address_type=self.AddressType.SHIPPING)
                base_qs.filter(address_type=self.AddressType.BILLING).update(is_default=False)
            elif self.address_type == self.AddressType.SHIPPING:
                base_qs.filter(address_type=self.AddressType.BOTH).update(address_type=self.AddressType.BILLING)
                base_qs.filter(address_type=self.AddressType.SHIPPING).update(is_default=False)
        super().save(*args, **kwargs)

class Wishlist(models.Model):
    """
    Stores saved customer product wishlists.
    """
    customer = models.ForeignKey(
        CustomerProfile,
        on_delete=models.CASCADE,
        related_name='wishlist_items',
        verbose_name=_("Customer Profile")
    )
    product = models.ForeignKey(
        "catalog.Product",
        on_delete=models.CASCADE,
        related_name='wishlisted_by',
        verbose_name=_("Product")
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("Created At"))

    class Meta:
        verbose_name = _("Wishlist Item")
        verbose_name_plural = _("Wishlist Items")
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['customer', 'product'],
                name='unique_customer_product_wishlist'
            )
        ]
        indexes = [
            models.Index(fields=['customer', 'product']),
        ]

    def __str__(self) -> str:
        return f"{self.customer.user.username} - Product ID: {self.product_id}"

class SavedCart(models.Model):
    """
    Persistent shopping carts saved by the customer for later access.
    """
    customer = models.ForeignKey(
        CustomerProfile,
        on_delete=models.CASCADE,
        related_name='saved_carts',
        verbose_name=_("Customer Profile")
    )
    name = models.CharField(
        max_length=150,
        verbose_name=_("Cart Name")
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("Created At"))
    updated_at = models.DateTimeField(auto_now=True, verbose_name=_("Updated At"))

    class Meta:
        verbose_name = _("Saved Cart")
        verbose_name_plural = _("Saved Carts")
        ordering = ['-updated_at']
        indexes = [
            models.Index(fields=['customer', 'updated_at']),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.customer.user.username})"

    @property
    def total_items(self) -> int:
        return self.items.count()

    @property
    def total_quantity(self) -> int:
        return self.items.aggregate(total=models.Sum('quantity'))['total'] or 0

class SavedCartItem(models.Model):
    """
    Individual items contained inside a SavedCart instance.
    """
    saved_cart = models.ForeignKey(
        SavedCart,
        on_delete=models.CASCADE,
        related_name='items',
        verbose_name=_("Saved Cart")
    )
    product = models.ForeignKey(
        "catalog.Product",
        on_delete=models.CASCADE,
        related_name='saved_cart_items',
        verbose_name=_("Product")
    )
    variant = models.ForeignKey(
        "catalog.ProductVariant",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name='saved_cart_items',
        verbose_name=_("Product Variant")
    )
    quantity = models.PositiveIntegerField(
        default=1,
        validators=[MinValueValidator(1)],
        verbose_name=_("Quantity")
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("Created At"))
    updated_at = models.DateTimeField(auto_now=True, verbose_name=_("Updated At"))

    class Meta:
        verbose_name = _("Saved Cart Item")
        verbose_name_plural = _("Saved Cart Items")
        ordering = ['created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['saved_cart', 'product', 'variant'],
                name='unique_cart_product_variant',
                condition=models.Q(variant__isnull=False)
            ),
            models.UniqueConstraint(
                fields=['saved_cart', 'product'],
                name='unique_cart_product_no_variant',
                condition=models.Q(variant__isnull=True)
            )
        ]
        indexes = [
            models.Index(fields=['saved_cart', 'product']),
        ]

    def __str__(self) -> str:
        variant_desc = f" [{self.variant}]" if self.variant else ""
        return f"{self.quantity} x Product ID {self.product_id}{variant_desc}"

    def clean(self) -> None:
        super().clean()
        if self.quantity < 1:
            raise ValidationError(_("Quantity must be at least 1."))

class SocialAccountMetadata(models.Model):
    """
    Third-party OAuth federated single sign-on metadata.
    """
    class ProviderChoices(models.TextChoices):
        GOOGLE = 'GOOGLE', _('Google')
        FACEBOOK = 'FACEBOOK', _('Facebook')
        GITHUB = 'GITHUB', _('GitHub')
        APPLE = 'APPLE', _('Apple')
        OTHER = 'OTHER', _('Other')

    customer = models.ForeignKey(
        CustomerProfile,
        on_delete=models.CASCADE,
        related_name='social_accounts',
        verbose_name=_("Customer Profile")
    )
    provider = models.CharField(
        max_length=48,
        choices=ProviderChoices.choices,
        verbose_name=_("Authentication Provider")
    )
    provider_uid = models.CharField(
        max_length=255,
        db_index=True,
        verbose_name=_("Provider Unique ID")
    )
    linked_at = models.DateTimeField(auto_now_add=True, verbose_name=_("Linked At"))
    last_synced_at = models.DateTimeField(auto_now=True, verbose_name=_("Last Synced At"))

    class Meta:
        verbose_name = _("Social Account Metadata")
        verbose_name_plural = _("Social Account Metadata Records")
        ordering = ['-linked_at']
        constraints = [
            models.UniqueConstraint(
                fields=['provider', 'provider_uid'],
                name='unique_provider_account'
            )
        ]
        indexes = [
            models.Index(fields=['provider_uid']),
            models.Index(fields=['customer', 'provider']),
        ]

    def __str__(self) -> str:
        return f"{self.customer.user.username} - {self.get_provider_display()} ({self.provider_uid})"