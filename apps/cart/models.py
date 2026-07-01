from django.db import models
from django.conf import settings
from django.core.validators import MinValueValidator
from django.utils.translation import gettext_lazy as _
from django.utils import timezone


class CartQuerySet(models.QuerySet):
    """
    Custom QuerySet for Cart model providing optimized filters 
    for active, expired, and potentially abandoned carts.
    """
    def active(self):
        return self.filter(is_active=True)

    def expired(self):
        return self.filter(is_active=True, expires_at__lt=timezone.now())

    def abandoned(self, threshold_hours=24):
        cutoff = timezone.now() - timezone.timedelta(hours=threshold_hours)
        return self.filter(is_active=True, last_activity_at__lt=cutoff)


class CartManager(models.Manager):
    """
    Manager for the Cart model with specific lookups for active 
    customer and session-based guest carts.
    """
    def get_queryset(self):
        return CartQuerySet(self.model, using=self._db)

    def active(self):
        return self.get_queryset().active()

    def get_active_cart_for_customer(self, customer):
        if not customer or customer.is_anonymous:
            return None
        return self.active().filter(customer=customer).first()

    def get_active_cart_for_session(self, session_key):
        if not session_key:
            return None
        return self.active().filter(session_key=session_key).first()


class Cart(models.Model):
    """
    Main Cart representation supporting both guest sessions and 
    authenticated customer profiles with persistence across devices.
    """
    customer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='carts',
        verbose_name=_("Customer"),
        help_text=_("The authenticated customer associated with this persistent cart.")
    )
    session_key = models.CharField(
        _("Session Key"),
        max_length=40,
        null=True,
        blank=True,
        db_index=True,
        help_text=_("The anonymous session key mapped to a guest cart context.")
    )
    is_active = models.BooleanField(
        _("Is Active"),
        default=True,
        db_index=True,
        help_text=_("Designates whether this cart is active, or already checked-out/archived.")
    )
    created_at = models.DateTimeField(_("Created At"), auto_now_add=True)
    updated_at = models.DateTimeField(_("Updated At"), auto_now=True)
    last_activity_at = models.DateTimeField(_("Last Activity At"), auto_now=True, db_index=True)
    recovered_at = models.DateTimeField(_("Recovered At"), null=True, blank=True)
    expires_at = models.DateTimeField(_("Expires At"), null=True, blank=True, db_index=True)

    objects = CartManager()

    class Meta:
        verbose_name = _("Cart")
        verbose_name_plural = _("Carts")
        ordering = ['-last_activity_at']
        indexes = [
            models.Index(fields=['customer', 'is_active']),
            models.Index(fields=['session_key', 'is_active']),
        ]

    def __str__(self):
        if self.customer:
            return f"Cart {self.id} - Customer: {self.customer.get_username()}"
        return f"Cart {self.id} - Guest Session: {self.session_key[:8] if self.session_key else 'Anonymous'}"

    @property
    def total_price(self):
        """Calculates total price of all active items in the cart."""
        return sum(item.subtotal for item in self.items.filter(status=CartItem.StatusChoices.ACTIVE))

    @property
    def total_items_count(self):
        """Calculates collective item quantities inside active state."""
        return sum(item.quantity for item in self.items.filter(status=CartItem.StatusChoices.ACTIVE))

    @property
    def unique_items_count(self):
        """Calculates active unique lines matching selected configurations."""
        return self.items.filter(status=CartItem.StatusChoices.ACTIVE).count()

    def merge_cart(self, guest_cart):
        """
        Merges an anonymous guest cart into this active customer cart 
        intelligently, combining matching product/variant rows.
        """
        if not guest_cart or guest_cart == self:
            return

        for guest_item in guest_cart.items.all():
            existing_item = self.items.filter(
                product=guest_item.product,
                variant=guest_item.variant,
                status=guest_item.status
            ).first()

            if existing_item:
                existing_item.quantity += guest_item.quantity
                existing_item.save()
                guest_item.delete()
            else:
                guest_item.cart = self
                guest_item.save()

        # Disable the old guest cart to prevent accidental checkout loops
        guest_cart.is_active = False
        guest_cart.save()


class CartItem(models.Model):
    """
    Individual line items mapping specific quantities, products, options 
    and states like 'Active' versus 'Save for Later'.
    """
    class StatusChoices(models.TextChoices):
        ACTIVE = 'active', _('Active')
        SAVED = 'saved', _('Save for Later')

    cart = models.ForeignKey(
        Cart,
        on_delete=models.CASCADE,
        related_name='items',
        verbose_name=_("Cart")
    )
    product = models.ForeignKey(
        'catalog.Product',
        on_delete=models.CASCADE,
        related_name='cart_items',
        verbose_name=_("Product")
    )
    variant = models.ForeignKey(
        'catalog.ProductVariant',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='cart_items',
        verbose_name=_("Product Variant")
    )
    quantity = models.PositiveIntegerField(
        _("Quantity"),
        default=1,
        validators=[MinValueValidator(1)]
    )
    status = models.CharField(
        _("Status"),
        max_length=10,
        choices=StatusChoices.choices,
        default=StatusChoices.ACTIVE,
        db_index=True
    )
    created_at = models.DateTimeField(_("Created At"), auto_now_add=True)
    updated_at = models.DateTimeField(_("Updated At"), auto_now=True)

    class Meta:
        verbose_name = _("Cart Item")
        verbose_name_plural = _("Cart Items")
        ordering = ['created_at']
        indexes = [
            models.Index(fields=['cart', 'status']),
            models.Index(fields=['cart', 'product', 'variant']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['cart', 'product', 'variant', 'status'],
                name='unique_cart_product_variant_status'
            ),
            models.UniqueConstraint(
                fields=['cart', 'product', 'status'],
                condition=models.Q(variant__isnull=True),
                name='unique_cart_product_status_no_variant'
            )
        ]

    def __str__(self):
        variant_str = f" ({self.variant})" if self.variant else ""
        return f"{self.quantity} x {self.product.title}{variant_str} [{self.get_status_display()}]"

    @property
    def unit_price(self):
        """Returns the appropriate pricing matrix value considering active variant selections."""
        if self.variant and hasattr(self.variant, 'price') and self.variant.price is not None:
            return self.variant.price
        return self.product.price

    @property
    def subtotal(self):
        """Calculates row-level financial aggregates."""
        return self.unit_price * self.quantity

    def move_to_save_for_later(self):
        """Shifts an item to a saved checkout workflow state without full line removal."""
        existing_saved_item = CartItem.objects.filter(
            cart=self.cart,
            product=self.product,
            variant=self.variant,
            status=self.StatusChoices.SAVED
        ).first()

        if existing_saved_item:
            existing_saved_item.quantity += self.quantity
            existing_saved_item.save()
            self.delete()
            return existing_saved_item
        else:
            self.status = self.StatusChoices.SAVED
            self.save()
            return self

    def move_to_active_cart(self):
        """Restores a saved item directly back into checkout processing routines."""
        existing_active_item = CartItem.objects.filter(
            cart=self.cart,
            product=self.product,
            variant=self.variant,
            status=self.StatusChoices.ACTIVE
        ).first()

        if existing_active_item:
            existing_active_item.quantity += self.quantity
            existing_active_item.save()
            self.delete()
            return existing_active_item
        else:
            self.status = self.StatusChoices.ACTIVE
            self.save()
            return self