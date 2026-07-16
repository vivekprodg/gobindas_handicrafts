from django.contrib import admin
from django.db import models
from django.utils.translation import gettext_lazy as _
from apps.customers.models import (
    CustomerProfile,
    CustomerAddress,
    Wishlist,
    SavedCart,
    SavedCartItem,
    SocialAccountMetadata,
)

class CustomerAddressInline(admin.TabularInline):
    """
    Inline rendering of customer addresses directly within the CustomerProfile administrative page.
    """
    model = CustomerAddress
    extra = 0
    fields = ['full_name', 'phone_number', 'address_type', 'city', 'country', 'is_default', 'is_active']
    readonly_fields = ['created_at', 'updated_at']
    can_delete = True
    show_change_link = True


class WishlistInline(admin.TabularInline):
    """
    Inline representation of customer wishlist entries on the profile page.
    """
    model = Wishlist
    extra = 0
    fields = ['product', 'created_at']
    readonly_fields = ['created_at']
    raw_id_fields = ['product']
    can_delete = True


class SavedCartInline(admin.TabularInline):
    """
    Inline overview of multiple saved carts associated with a customer profile.
    """
    model = SavedCart
    extra = 0
    fields = ['name', 'created_at', 'updated_at']
    readonly_fields = ['created_at', 'updated_at']
    show_change_link = True
    can_delete = True


class SocialAccountInline(admin.TabularInline):
    """
    Inline listing of linked third-party social provider identities.
    """
    model = SocialAccountMetadata
    extra = 0
    fields = ['provider', 'provider_uid', 'linked_at', 'last_synced_at']
    readonly_fields = ['linked_at', 'last_synced_at']
    can_delete = True


class SavedCartItemInline(admin.TabularInline):
    """
    Inline rendering of detailed cart item elements within a specific SavedCart workspace.
    """
    model = SavedCartItem
    extra = 0
    fields = ['product', 'variant', 'quantity', 'created_at', 'updated_at']
    readonly_fields = ['created_at', 'updated_at']
    raw_id_fields = ['product', 'variant']
    can_delete = True


@admin.register(CustomerProfile)
class CustomerProfileAdmin(admin.ModelAdmin):
    """
    Comprehensive administrative control system for the core customer account demographic profiles.
    Optimized to eliminate N+1 queries via dynamic queryset annotations for metadata aggregations.
    """
    list_display = [
        'full_name',
        'get_username',
        'get_email',
        'phone_number',
        'gender',
        'preferred_language',
        'newsletter_subscribed',
        'address_count',
        'wishlist_count',
        'saved_cart_count',
        'social_account_count',
        'created_at',
    ]
    list_filter = ['gender', 'newsletter_subscribed', 'preferred_language', 'created_at', 'updated_at']
    search_fields = ['user__first_name', 'user__last_name', 'user__username', 'user__email', 'phone_number']
    raw_id_fields = ['user']
    list_select_related = ['user']
    list_per_page = 50
    date_hierarchy = 'created_at'
    ordering = ['-created_at']
    readonly_fields = ['created_at', 'updated_at']

    inlines = [CustomerAddressInline, WishlistInline, SavedCartInline, SocialAccountInline]

    fieldsets = (
        (_("General Information"), {
            'fields': ('user', 'phone_number', 'avatar', 'date_of_birth', 'gender')
        }),
        (_("Preferences"), {
            'fields': ('preferred_language', 'newsletter_subscribed')
        }),
        (_("Metadata"), {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',),
        }),
    )

    def get_queryset(self, request):
        """
        Overrides the base admin queryset to inject subquery annotations, protecting the database 
        against N+1 lookup cycles when rendering summary relationship indicators.
        """
        qs = super().get_queryset(request)
        return qs.select_related('user').annotate(
            _address_count=models.Count('addresses', distinct=True),
            _wishlist_count=models.Count('wishlist_items', distinct=True),
            _saved_cart_count=models.Count('saved_carts', distinct=True),
            _social_account_count=models.Count('social_accounts', distinct=True),
        )

    @admin.display(description=_("Full Name"), ordering='user__first_name')
    def full_name(self, obj: CustomerProfile) -> str:
        return f"{obj.user.first_name} {obj.user.last_name}".strip() or obj.user.username

    @admin.display(description=_("Username"), ordering='user__username')
    def get_username(self, obj: CustomerProfile) -> str:
        return obj.user.username

    @admin.display(description=_("Email"), ordering='user__email')
    def get_email(self, obj: CustomerProfile) -> str:
        return obj.user.email

    @admin.display(description=_("Addresses"), ordering='_address_count')
    def address_count(self, obj: CustomerProfile) -> int:
        return getattr(obj, '_address_count', 0)

    @admin.display(description=_("Wishlist Items"), ordering='_wishlist_count')
    def wishlist_count(self, obj: CustomerProfile) -> int:
        return getattr(obj, '_wishlist_count', 0)

    @admin.display(description=_("Saved Carts"), ordering='_saved_cart_count')
    def saved_cart_count(self, obj: CustomerProfile) -> int:
        return getattr(obj, '_saved_cart_count', 0)

    @admin.display(description=_("Social Accounts"), ordering='_social_account_count')
    def social_account_count(self, obj: CustomerProfile) -> int:
        return getattr(obj, '_social_account_count', 0)


@admin.register(CustomerAddress)
class CustomerAddressAdmin(admin.ModelAdmin):
    """
    Administration configuration handling customer delivery addresses and billing locations.
    """
    list_display = [
        'customer',
        'full_name',
        'phone_number',
        'address_type',
        'city',
        'country',
        'is_default',
        'is_active',
        'created_at',
    ]
    list_filter = ['address_type', 'is_default', 'is_active', 'country', 'created_at']
    search_fields = [
        'customer__user__first_name',
        'customer__user__last_name',
        'customer__user__email',
        'customer__phone_number',
        'full_name',
        'phone_number',
        'address_line_1',
        'address_line_2',
        'city',
        'state_or_province',
        'postal_code',
        'country',
    ]
    raw_id_fields = ['customer']
    list_select_related = ['customer', 'customer__user']
    list_per_page = 50
    date_hierarchy = 'created_at'
    ordering = ['-created_at']
    readonly_fields = ['created_at', 'updated_at']
    actions = ['mark_active', 'mark_inactive', 'mark_default']

    @admin.action(description=_("Mark selected addresses as active"))
    def mark_active(self, request, queryset):
        updated = queryset.update(is_active=True)
        self.message_user(request, _(f"Successfully marked {updated} address records as active."))

    @admin.action(description=_("Mark selected addresses as inactive"))
    def mark_inactive(self, request, queryset):
        updated = queryset.update(is_active=False, is_default=False)
        self.message_user(request, _(f"Successfully deactivated {updated} addresses and removed default flags."))

    @admin.action(description=_("Mark selected addresses as default (Processes sequentially)"))
    def mark_default(self, request, queryset):
        processed_count = 0
        for address in queryset:
            if address.is_active:
                address.is_default = True
                address.save()  # Triggers model-level normalization code
                processed_count += 1
        self.message_user(request, _(f"Successfully processed default assignment logic for {processed_count} active address lines."))


@admin.register(Wishlist)
class WishlistAdmin(admin.ModelAdmin):
    """
    Administration view allowing observation and analysis of customer wishlist trends and insights.
    """
    list_display = ['customer', 'customer_email', 'product', 'product_information', 'created_at']
    list_filter = ['created_at']
    search_fields = [
        'customer__user__first_name',
        'customer__user__last_name',
        'customer__user__email',
        'product__name',
    ]
    raw_id_fields = ['customer', 'product']
    list_select_related = ['customer', 'customer__user', 'product']
    list_per_page = 50
    date_hierarchy = 'created_at'
    ordering = ['-created_at']
    readonly_fields = ['created_at']

    @admin.display(description=_("Customer Email"), ordering='customer__user__email')
    def customer_email(self, obj: Wishlist) -> str:
        return obj.customer.user.email

    @admin.display(description=_("Product Information"))
    def product_information(self, obj: Wishlist) -> str:
        return f"{obj.product.name} (ID: {obj.product_id})"


@admin.register(SavedCart)
class SavedCartAdmin(admin.ModelAdmin):
    """
    Administration view tracking saved, multi-session shopping cart buckets.
    """
    list_display = ['customer', 'name', 'total_items_count', 'total_quantity_count', 'created_at', 'updated_at']
    list_filter = ['created_at', 'updated_at']
    search_fields = ['customer__user__first_name', 'customer__user__last_name', 'customer__user__email', 'name']
    raw_id_fields = ['customer']
    list_select_related = ['customer', 'customer__user']
    list_per_page = 50
    date_hierarchy = 'updated_at'
    ordering = ['-updated_at']
    readonly_fields = ['total_items_count', 'total_quantity_count', 'created_at', 'updated_at']

    inlines = [SavedCartItemInline]

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.annotate(
            _items_count=models.Count('items', distinct=True),
            _quantity_sum=models.Sum('items__quantity')
        )

    @admin.display(description=_("Total Items"), ordering='_items_count')
    def total_items_count(self, obj: SavedCart) -> int:
        return getattr(obj, '_items_count', 0)

    @admin.display(description=_("Total Quantity"), ordering='_quantity_sum')
    def total_quantity_count(self, obj: SavedCart) -> int:
        return getattr(obj, '_quantity_sum', 0) or 0


@admin.register(SavedCartItem)
class SavedCartItemAdmin(admin.ModelAdmin):
    """
    Detailed standalone view for specific item selections assigned inside consumer saved carts.
    """
    list_display = ['saved_cart', 'get_customer', 'product', 'variant', 'quantity', 'created_at']
    list_filter = ['created_at']
    search_fields = [
        'saved_cart__name',
        'saved_cart__customer__user__first_name',
        'saved_cart__customer__user__last_name',
        'saved_cart__customer__user__email',
        'product__name',
    ]
    raw_id_fields = ['saved_cart', 'product', 'variant']
    list_select_related = ['saved_cart', 'saved_cart__customer', 'saved_cart__customer__user', 'product', 'variant']
    list_per_page = 50
    date_hierarchy = 'created_at'
    ordering = ['-created_at']
    readonly_fields = ['created_at', 'updated_at']

    @admin.display(description=_("Customer"), ordering='saved_cart__customer__user__username')
    def get_customer(self, obj: SavedCartItem) -> str:
        return obj.saved_cart.customer.user.username


@admin.register(SocialAccountMetadata)
class SocialAccountMetadataAdmin(admin.ModelAdmin):
    """
    Secured dashboard view displaying mapped remote credentials linked via federated SSO social providers.
    """
    list_display = ['customer', 'provider', 'provider_uid', 'linked_at', 'last_synced_at']
    list_filter = ['provider', 'linked_at', 'last_synced_at']
    search_fields = ['customer__user__first_name', 'customer__user__last_name', 'customer__user__email', 'provider_uid']
    raw_id_fields = ['customer']
    list_select_related = ['customer', 'customer__user']
    list_per_page = 50
    date_hierarchy = 'linked_at'
    ordering = ['-linked_at']
    readonly_fields = ['linked_at', 'last_synced_at']