import os
from django.conf import settings
from django.core.validators import MinValueValidator
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _

def customer_avatar_upload_path(instance, filename: str) -> str:
    """
    Generates a deterministic and isolated file path for a customer's profile avatar upload.
    """
    ext = os.path.splitext(filename)[1]
    return f"customers/avatars/profile_{instance.user.id}{ext}"


class CustomerProfile(models.Model):
    """
    Extends authentication structure by storing non-auth, ecommerce-specific 
    customer demographics and behavior configurations.
    """
    class GenderChoices(models.TextChoices):
        MALE = 'MALE', _('Male')
        FEMALE = 'FEMALE', _('Female')
        OTHER = 'OTHER', _('Other')
        PREFER_NOT_TO_SAY = 'PREFER_NOT_TO_SAY', _('Prefer Not To Say')

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='customer_profile',
        verbose_name=_("User Reference")
    )
    phone_number = models.CharField(
        max_length=32,
        blank=True,
        null=True,
        db_index=True,
        verbose_name=_("Phone Number"),
        help_text=_("Primary contact number for order fulfillment and notifications.")
    )
    avatar = models.ImageField(
        upload_to=customer_avatar_upload_path,
        blank=True,
        null=True,
        verbose_name=_("Avatar Image"),
        help_text=_("Customer profile portrait photograph.")
    )
    date_of_birth = models.DateField(
        blank=True,
        null=True,
        verbose_name=_("Date of Birth"),
        help_text=_("Used for age verification and personalized marketing offers.")
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
        db_index=True,
        default='en',
        verbose_name=_("Preferred Language"),
        help_text=_("ISO language code for localized messaging and emails.")
    )
    newsletter_subscribed = models.BooleanField(
        default=False,
        verbose_name=_("Newsletter Subscribed"),
        help_text=_("Indicates whether the customer opted into marketing email distributions.")
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("Created At"))
    updated_at = models.DateTimeField(auto_now=True, verbose_name=_("Updated At"))

    class Meta:
        verbose_name = _("Customer Profile")
        verbose_name_plural = _("Customer Profiles")
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['phone_number']),
            models.Index(fields=['preferred_language']),
        ]

    def __str__(self) -> str:
        return self.user.username

    @property
    def display_name(self) -> str:
        """
        Returns full customer name if provided, falling back to the standard system username.
        """
        full_name = f"{self.user.first_name} {self.user.last_name}".strip()
        return full_name if full_name else self.user.username


class CustomerAddress(models.Model):
    """
    Handles multiple physical operational addresses associated with a unique CustomerProfile.
    Implements normalization and automated single-default address logic.
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
    full_name = models.CharField(
        max_length=255, 
        verbose_name=_("Full Name"),
        help_text=_("Recipient full name for delivery/billing identification.")
    )
    phone_number = models.CharField(
        max_length=50, 
        verbose_name=_("Phone Number"),
        help_text=_("Specific delivery point contact telephone number.")
    )
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
        verbose_name=_("Is Default Address"),
        help_text=_("Designates this node as the primary automatic address selector.")
    )
    is_active = models.BooleanField(
        default=True, 
        verbose_name=_("Is Active"),
        help_text=_("Soft delete indicator to protect transactional integrity across past orders.")
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

    def clean(self) -> None:
        super().clean()
        if self.is_default and not self.is_active:
            raise ValidationError(_("An inactive address record cannot simultaneously serve as a default address."))

    def save(self, *args, **kwargs) -> None:
        """
        Enforces strict database business criteria: normalizes and automatically shifts 
        pre-existing default flags to prevent overlapping types per customer record.
        """
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
    Enables tracking of customer product tracking lists. Uses isolated 
    lazy string references to eliminate risk of circular cross-application dependencies.
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
        return f"{self.customer.user.username} - Wishlist item ID: {self.product_id}"


class SavedCart(models.Model):
    """
    Maintains persistent customer checkout carts allowing multi-session retrieval, 
    cart reference naming, and analytical summary operations.
    """
    customer = models.ForeignKey(
        CustomerProfile,
        on_delete=models.CASCADE,
        related_name='saved_carts',
        verbose_name=_("Customer Profile")
    )
    name = models.CharField(
        max_length=150,
        verbose_name=_("Cart Name"),
        help_text=_("A custom identifiable or auto-assigned name for this persistent cart instance.")
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
        """
        Returns the overall distinct number of structural product rows contained inside this cart.
        """
        return self.items.count()

    @property
    def total_quantity(self) -> int:
        """
        Aggregates and delivers the summary sum of physical elements held within item boundaries.
        """
        return self.items.aggregate(total=models.Sum('quantity'))['total'] or 0


class SavedCartItem(models.Model):
    """
    Line items belonging specifically to a SavedCart instance.
    Guarantees validation and uniqueness on product and variant intersections.
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
        verbose_name=_("Product Variant"),
        help_text=_("Optional specific model SKU variation matrix pointer.")
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
            raise ValidationError(_("Cart item allocation quantity must be equal to or greater than 1."))
        
        duplicate_check = SavedCartItem.objects.filter(
            saved_cart=self.saved_cart, 
            product=self.product, 
            variant=self.variant
        )
        if self.pk:
            duplicate_check = duplicate_check.exclude(pk=self.pk)
        if duplicate_check.exists():
            raise ValidationError(_("This specific product and variant combination already exists within this cart."))


class SocialAccountMetadata(models.Model):
    """
    Provides secure storage and tracking references for federated single sign-on (SSO) OAuth metadata mapping.
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
        verbose_name=_("Provider Unique ID"),
        help_text=_("The permanent unique remote key identifier returned by the auth backend ecosystem.")
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